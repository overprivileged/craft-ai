"""Recipes v1: saved requests with parameters. App-agnostic.

A recipe file is UNTRUSTED input (someone may hand you one). On load it is size-capped, only known
keys are used, every string is put through the Guard's hidden-instruction scan, and the user sees a
preview before it runs. A recipe can never change Guard settings, the fence or the allow rule: it is
only ever turned into a chat request, and that request goes through the same guarded proxy as any other.

File: <project>/.craft-ai/recipes/<slug>.json
  {"v":1, "name", "description", "prompt" (with {param} placeholders), "params": {name: default},
   "created_from_steps": [undo-history labels]}
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

VERSION = 1
MAX_FILE = 64 * 1024
MAX_TEXT = 4000
MAX_PARAMS = 16
KNOWN_KEYS = ("v", "name", "description", "prompt", "params", "created_from_steps")
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,59}$")
PARAM_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_PATHISH = re.compile(r"(?:~|\.{1,2})?/[^\s\"'`,;)]+|[A-Za-z]:\\[^\s\"']+")
_FILE_WORDS = re.compile(r"\b(export|render|save as|save to|import|write|copy to|move to|delete file)", re.I)


class RecipeError(ValueError):
    pass


def recipes_dir(project: Path) -> Path:
    return project / ".craft-ai" / "recipes"


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60].strip("-")
    return s or "recipe"


def _clip(s: object, n: int = MAX_TEXT) -> str:
    s = s if isinstance(s, str) else ""
    return s if len(s) <= n else s[:n] + "…"


def validate(raw: object) -> tuple[dict, list[str]]:
    """Return (clean recipe, ignored keys). Never trusts types or sizes from the file."""
    if not isinstance(raw, dict):
        raise RecipeError("a recipe must be a JSON object")
    if raw.get("v") != VERSION:
        raise RecipeError(f"unsupported recipe version {raw.get('v')!r} (this is v{VERSION})")
    prompt = _clip(raw.get("prompt"))
    if not prompt.strip():
        raise RecipeError("the recipe has no prompt")
    params: dict[str, str] = {}
    rp = raw.get("params") if isinstance(raw.get("params"), dict) else {}
    for k, v in list(rp.items())[:MAX_PARAMS]:
        if isinstance(k, str) and PARAM_NAME.match(k) and isinstance(v, (str, int, float)) and not isinstance(v, bool):
            params[k] = _clip(str(v), 200)
    steps = raw.get("created_from_steps") if isinstance(raw.get("created_from_steps"), list) else []
    clean = {
        "v": VERSION,
        "name": _clip(raw.get("name"), 80) or "Untitled recipe",
        "description": _clip(raw.get("description"), 1000),
        "prompt": prompt,
        "params": params,
        "created_from_steps": [_clip(s, 120) for s in steps[:64] if isinstance(s, str)],
    }
    ignored = sorted(str(k)[:40] for k in raw if k not in KNOWN_KEYS)
    return clean, ignored


def load_file(project: Path, slug: str) -> tuple[dict, list[str]]:
    if not SLUG.match(slug):
        raise RecipeError("bad recipe name")
    base = recipes_dir(project)
    path = base / f"{slug}.json"
    real = Path(os.path.realpath(path))
    if os.path.commonpath([str(Path(os.path.realpath(base))), str(real)]) != str(Path(os.path.realpath(base))):
        raise RecipeError("recipe file is outside the project's recipes folder")
    try:
        if real.stat().st_size > MAX_FILE:
            raise RecipeError("recipe file is too big")
        raw = json.loads(real.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise RecipeError(f"can't read the recipe: {e}") from None
    return validate(raw)


def list_files(project: Path | None) -> list[str]:
    if project is None:
        return []
    d = recipes_dir(project)
    try:
        return sorted(p.stem for p in d.glob("*.json") if SLUG.match(p.stem))[:200]
    except OSError:
        return []


def render(recipe: dict, values: dict | None) -> tuple[str, dict]:
    """Fill {param} placeholders. Values are the recipe's defaults overridden by the user's edits;
    unknown names are dropped, values are single-line and capped."""
    vals = dict(recipe["params"])
    for k, v in (values or {}).items():
        if k in vals and isinstance(v, (str, int, float)) and not isinstance(v, bool):
            vals[k] = " ".join(str(v).split())[:200]
    text = recipe["prompt"]
    for k, v in vals.items():
        text = text.replace("{" + k + "}", v)
    return text, vals


def save(project: Path, name: str, description: str, prompt: str, steps: list[str], params: dict | None = None) -> str:
    d = recipes_dir(project)
    d.mkdir(parents=True, exist_ok=True)
    base = slugify(name)
    slug = base
    for i in range(2, 100):
        path = d / f"{slug}.json"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            break
        except FileExistsError:
            slug = f"{base[:55]}-{i}"
    else:
        raise RecipeError("too many recipes with that name")
    data = {"v": VERSION, "name": name[:80], "description": description[:1000], "prompt": prompt[:MAX_TEXT],
            "params": params or {}, "created_from_steps": steps[:64]}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return slug


def preview(recipe: dict, ignored: list[str], values: dict | None, *, scan, inside, ask: bool,
            support: str = "") -> dict:
    """What the user sees before a recipe runs. `scan(text) -> [rules]` is the Guard's hidden-instruction
    scan; `inside(path) -> bool` is the Guard's fence check."""
    request, vals = render(recipe, values)
    warnings = []
    for field_name, text in (("name", recipe["name"]), ("description", recipe["description"]), ("prompt", recipe["prompt"]),
                             *((f"param {k}", v) for k, v in vals.items()),
                             *((f"step {i + 1}", s) for i, s in enumerate(recipe["created_from_steps"]))):
        rules = scan(text)
        if rules:
            warnings.append({"field": field_name, "rules": rules, "text": text[:300]})
    paths = sorted(set(_PATHISH.findall(request)))
    files = []
    for p in paths[:20]:
        files.append(f"{p} ({'inside the project folder' if inside(p) else 'outside the project folder: will be blocked'})")
    will = [f"Ask the AI: {request}"]
    if recipe["created_from_steps"]:
        will.append("Saved from these steps: " + ", ".join(recipe["created_from_steps"][:12]))
    if support:
        will.append(support)
    return {
        "name": recipe["name"],
        "description": recipe["description"],
        "request": request,
        "params": vals,
        "will": will,
        "files": "none" if not files else "; ".join(files),
        "exports": ("ask (held for your OK)" if ask else "allowed by your settings") if _FILE_WORDS.search(request) else
                   ("none asked for; would be held for your OK" if ask else "none asked for"),
        "warnings": warnings,
        "ignored": ignored,
        "flagged": bool(warnings),
    }
