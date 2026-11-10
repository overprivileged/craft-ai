"""FilmCraft adapter: the only file that knows FilmCraft specifics."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

from core.adapter import Adapter

_REPO = Path(__file__).resolve().parents[2]
_BIN_DIR = Path(os.environ.get("CRAFT_AI_FILMCRAFT_BIN", _REPO / "vendor" / "filmcraft" / "target" / "release"))

_READ_ONLY_TOOLS = {
    "command_list", "doc_inspect", "project_inspect", "sequence_inspect", "render_frame", "render_preview",
    "ui_inspect", "ui_elements", "ui_screenshot", "doc_inspect",
}
# Held families: leave the project (export, save, import, open/close, files on disk, media relink /
# proxies / cache / render files), can't be undone (history, jobs, closing projects), or change the
# app instead of the project (settings, presets, shortcuts, LUT/preset libraries, plugins).
_HELD_PREFIXES = (
    "export.", "file.export", "file.save", "file.import", "file.open", "file.close", "file.revert", "file.recover",
    "file.discardRecovery", "file.autoSave", "file.projectManager", "file.replaceFonts", "file.reveal",
    "file.newProject", "file.projectSettings.scratchDisks", "edit.pasteMedia",
    "media.", "mediaBrowser.", "mediaCache.", "sequence.render", "sequence.deleteRender", "tts.render",
    "captions.import", "captions.export", "markers.export", "graphics.newFromFile", "graphics.template.",
    "lut.", "lumetri.export", "presets.", "labels.", "shortcuts.", "prefs.", "preferences.", "settings.",
    "plugins.", "fonts.", "perf.", "state.", "help.", "app.", "history.clear", "history.deleteRedoable",
    "jobs.cancel", "transcript.downloadModel", "tts.downloadVoices", "audio.voiceover.start", "project.delete", "project.ingestSettings", "project.viewPreset.",
    "essentialSound.savePreset", "essentialSound.deletePreset", "colors.removeCustom", "audio.voiceover.settings",
)
# read-only queries inside held families stay free (except browsing the file system)
_HELD_QUERY_OK = ("list", "get", "inspect", "info", "status", "formats", "search", "colorInfo", "proxyPresets")
_RISKY_PREFIXES = _HELD_PREFIXES  # kept for older imports


def _held_family(cmd: str) -> bool:
    if not cmd.startswith(_HELD_PREFIXES):
        return False
    last = cmd.rsplit(".", 1)[-1]
    if cmd.startswith(("mediaBrowser.", "file.")):
        return True
    return not last.startswith(_HELD_QUERY_OK)


class FilmCraft(Adapter):
    name = "filmcraft"
    port = 9876
    token_env = "FILMCRAFT_CONTROL_TOKEN"
    undo_step = "edit.undo"
    curated_commands = (
        "file.newSequence", "file.newBlackVideo", "file.newColorMatte", "file.newBarsAndTone",
        "file.newTransparentVideo", "file.newAdjustmentLayer", "file.import",
        "timeline.place", "timeline.delete", "timeline.select", "timeline.move", "timeline.trim",
        "source.open", "source.insert", "source.overwrite",
        "playhead.set", "sequence.addEdit", "sequence.addEditAllTracks", "markers.markIn", "markers.markOut",
        "sequence.inspect", "project.inspect",
        "edit.undo", "edit.redo", "edit.rippleDelete", "edit.clear", "edit.duplicate",
        "edit.selectAll", "edit.deselectAll", "edit.cut", "edit.copy", "edit.paste",
        "clip.speedDuration", "clip.audioGain", "clip.rename", "clip.enable",
        "effects.apply", "effects.setParam", "effects.list", "graphics.setText",
        "markers.add", "graphics.newText", "captions.list", "captions.setText", "history.list",
    )

    @property
    def builtin_recipes(self) -> dict:
        from .filmcraft_recipes import BUILTIN_RECIPES
        return BUILTIN_RECIPES

    def __init__(self, port: int | None = None):
        if port:
            self.port = port

    def launch(self, token: str, folder: Path | None, panel_token: str = "") -> subprocess.Popen:
        app = _BIN_DIR / "filmcraft"
        if not app.exists():
            raise FileNotFoundError(f"{app} not built yet (run build.sh)")
        env = dict(os.environ, FILMCRAFT_CONTROL_TOKEN=token)
        if panel_token:
            env["CRAFT_AI_TOKEN"] = panel_token  # never the control token: the panel must not drive the app directly
        return subprocess.Popen(
            [str(app), "--control", str(self.port)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            cwd=str(folder) if folder else None,
        )

    def wait_ready(self, timeout: float, token: str = "") -> bool:
        """Port open is not enough: wait for an authenticated control reply (the UI loop is serving)."""
        end = time.monotonic() + timeout
        req = (json.dumps({"id": 1, "token": token, "method": "ui.inspect"}) + "\n").encode()
        while time.monotonic() < end:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=2) as s:
                    s.settimeout(5)
                    s.sendall(req)
                    line = s.makefile("rb").readline()
                    if json.loads(line or b"{}").get("ok"):
                        return True
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        return False

    def mcp_command(self) -> list[str]:
        return [str(_BIN_DIR / "filmcraft-cli"), "mcp", "--bridge", f"127.0.0.1:{self.port}"]

    def mcp_env(self, token: str) -> dict[str, str]:
        return {"FILMCRAFT_CONTROL_TOKEN": token}

    def selection(self, mcp, scan=None) -> dict | None:
        res = mcp.call_tool("sequence_inspect", {})
        seq = res.get("json")
        if res["is_error"] or not isinstance(seq, dict):
            return None
        tick = 254_016_000_000

        def secs(t) -> str:
            return f"{t / tick:.2f} s" if isinstance(t, (int, float)) else "none"

        def safe(name) -> str:
            name = str(name)[:80]
            return "(name hidden by the Guard)" if scan and scan(name) else name

        chosen = set(x for x in seq.get("selection", []) if isinstance(x, int))
        parts = []
        for kind in ("video", "audio"):
            for track in seq.get(kind, []) if isinstance(seq.get(kind), list) else []:
                for it in track.get("items", []):
                    if it.get("clip") in chosen and len(parts) < 12:
                        parts.append(f"clip {it['clip']} \"{safe(it.get('name', ''))}\" on {safe(track.get('name', kind))} "
                                     f"({secs(it.get('start'))} to {secs(it.get('end'))})")
        gap = seq.get("gapSelection")
        extra = []
        if gap:
            extra.append(f"a gap is selected ({str(gap)[:120]})")
        if seq.get("transitionSelection"):
            extra.append(f"transitions selected: {str(seq['transitionSelection'])[:80]}")
        mask = None
        try:
            ui = (mcp.call_tool("ui_inspect", {}).get("json") or {}).get("ui", {})
            mask = next((f"{k}={str(v)[:60]}" for k, v in ui.items() if "mask" in k.lower() and "sel" in k.lower() and v), None)
        except Exception:
            pass
        if mask:
            extra.append(f"selected mask: {mask}")
        inout = f"In {secs(seq.get('in'))}, Out {secs(seq.get('out'))}" if seq.get("in") is not None or seq.get("out") is not None else "no In/Out"
        name = safe(seq.get("name", ""))
        context = (f"[Selection in the app, sequence \"{name}\": " + ("; ".join(parts) if parts else "no clips selected")
                   + ("; " + "; ".join(extra) if extra else "") + f"; playhead {secs(seq.get('playhead'))}; {inout}. "
                   f"\"this\"/\"these\" in the request means the selection.]")
        n = len(parts)
        text = (f"{n} clip{'s' if n != 1 else ''}" if n else "no clips") + f" · playhead {secs(seq.get('playhead'))}" \
            + ("" if inout == "no In/Out" else " · In/Out") + (" · gap" if gap else "") + (" · mask" if mask else "")
        return {"text": text, "context": context, "count": n}

    def history_labels(self, mcp) -> list[str] | None:
        res = mcp.call_tool("command_run", {"id": "history.list", "params": {}})
        data = res.get("json")
        if res["is_error"] or not isinstance(data, dict):
            return None
        undo = data.get("undo")
        return [str(x) for x in undo] if isinstance(undo, list) else None

    def history_depth(self, mcp) -> int | None:
        labels = self.history_labels(mcp)
        return None if labels is None else len(labels)

    def is_edit(self, tool: str, args: dict) -> list[str]:
        if tool in _READ_ONLY_TOOLS:
            return []
        if tool == "command_run":
            cid = args.get("id")
            return [cid] if isinstance(cid, str) and cid != "history.list" else []
        if tool == "command_batch":
            return [s.get("id") for s in args.get("steps", []) if isinstance(s, dict) and isinstance(s.get("id"), str)]
        if tool == "media_import":
            return ["media_import"]
        return []

    # ---------- Guard hooks ----------
    def actions(self, tool: str, args: dict) -> list[tuple[str, dict]]:
        if tool == "command_run":
            return [(str(args.get("id", "")), _dict(args.get("params")))]
        if tool == "command_batch":
            return [(str(st.get("id", "")), _dict(st.get("params"))) for st in args.get("steps", []) if isinstance(st, dict)]
        if tool == "media_import":
            return [("media_import", args)]
        if tool == "ui_control":
            method, params = str(args.get("method", "")), _dict(args.get("params"))
            if method == "engine.execute":  # a command in disguise: check it as one
                return [(str(params.get("command", "")), _dict(params.get("params")))]
            return [(f"ui:{method}", params)]
        if tool in _READ_ONLY_TOOLS:
            return [(tool, args)]
        return [(f"ui:{tool}", args)]  # ui_click / ui_key / ui_type / ui_drag can drive any dialog

    def set_catalog(self, ids) -> None:
        """The app's real command ids (from command_list). Commands outside it are unknown: held."""
        self.catalog = frozenset(i for i in ids if isinstance(i, str))

    def allowed(self, cmd: str) -> bool:
        """Runs without asking: a known app command (or read-only tool) outside the held families.
        Held families are in-app edits' opposites: they leave the project, can't be undone, or change
        the app rather than the project. Paths are checked separately (risky + the fence)."""
        if cmd in _READ_ONLY_TOOLS:
            return True
        if cmd.startswith("ui:") or _held_family(cmd):
            return False
        catalog = getattr(self, "catalog", None)
        if catalog:
            return cmd in catalog
        # no catalog (headless tests): fall back to the guide's commands and plain queries
        last = cmd.rsplit(".", 1)[-1]
        return cmd in self.curated_commands or ("." in cmd and last.startswith(_QUERY_VERBS))

    def risky(self, cmd: str, params: dict) -> bool:
        if cmd == "media_import" or _held_family(cmd):
            return True
        if isinstance(params, dict) and params.get("deleteFiles"):
            return True
        return bool(self.paths_in(cmd, params))  # anything that reads or writes files

    def blocked(self, tool: str, args: dict) -> str | None:
        """Hard blocks, never allowable: the AI must not drive the AI panel itself (it could press Allow)."""
        if not tool.startswith("ui_"):
            return None
        target = args.get("id") or args.get("from") or args.get("to") or ""
        params = _dict(args.get("params"))
        target = target or params.get("id") or params.get("from") or params.get("to") or ""
        if isinstance(target, str) and (target.startswith("ai.") or target.startswith("panel.tab.AiChat")):
            return "the AI panel's own controls are only for the user"
        if tool == "ui_control" and str(args.get("method", "")).startswith("ui.") and "ai." in str(params)[:2000]:
            return "the AI panel's own controls are only for the user"
        return None

    def chat_allowable(self, cmd: str) -> bool:
        """May the user allow this command for the rest of the chat? Never raw UI input."""
        return not cmd.startswith("ui:") and cmd != "media_import"

    def paths_in(self, cmd: str, params: dict) -> list[str]:
        out: list[str] = []
        for k in _PATH_KEYS:
            v = params.get(k) if isinstance(params, dict) else None
            if isinstance(v, str) and v:
                out.append(v)
            elif isinstance(v, list):
                out.extend(x for x in v if isinstance(x, str) and x)
        return out

    def untrusted_fields(self, tool: str, args: dict, data: object) -> list[tuple[tuple, str]]:
        if tool not in _UNTRUSTED_TOOLS:
            return []
        if isinstance(args, dict) and args.get("id") in ("command_list", "history.list"):
            return []
        if isinstance(data, str):
            return [((), data)] if data else []
        out: list[tuple[tuple, str]] = []
        _walk(data, (), False, out, 0)
        return out

    def instructions(self, catalog: dict[str, dict]) -> str:
        lines = [f"{cid} {catalog[cid].get('params', '{}')}" for cid in self.curated_commands if cid in catalog]
        from .filmcraft_features import guide_section
        # not .format: the guide has literal braces
        return INSTRUCTIONS.replace("{features}", guide_section()).replace("{commands}", "\n".join(lines))


INSTRUCTIONS = """FilmCraft video editor (live desktop app). Edit the open project for the user.
How to work:
- Read first: sequence_inspect (tracks under video[]/audio[], clips in items[] with start/end in ticks,
  254016000000 ticks = 1 s) or doc_inspect. Use real ids from there, never invented ones.
- Run edits with command_run {id, params} or command_batch {steps}. Times accept "seconds".
- Generated media (black video, color matte, bars) is created as a project item first, then placed
  with timeline.place {item, track, time|seconds, insert}.
- Every edit is undoable with edit.undo. Captions: captions.list {track:"C1"} reads them, captions.setText edits.
Defaults (use them instead of asking):
- New clips go at the end of Video 1 (V1), after the last clip, as an overwrite, unless the user says otherwise.
- Generated video has no audio: place it with audio:false.
- After edits, check the result with sequence_inspect before saying it's done.
- If no FilmCraft command can do what the user asks, say so plainly. Never fake it with other commands.
- Ask a question only when the request is really ambiguous; otherwise do it and say what you did in one or two sentences.
{features}
Common commands (id params):
{commands}"""

_PATH_KEYS = ("path", "paths", "output", "dir", "folder", "file", "files", "fileName", "outputPath", "destination")
# in-project, undoable edits that run without asking (ask mode still holds exports, file writes,
# imports and media deletes: those are `risky`, which is checked before the allow-list)
_QUERY_VERBS = ("inspect", "list", "get", "find", "search", "goTo", "next", "previous", "info")
_UNTRUSTED_TOOLS = {"command_run", "command_batch", "doc_inspect", "project_inspect", "sequence_inspect",
                    "media_import", "ui_inspect", "ui_elements", "ui_control"}
# keys whose string values come from users/files (names, caption text, markers, notes, metadata)
_USER_KEYS = {"name", "text", "comment", "comments", "notes", "note", "title", "speaker", "description",
              "metadata", "keywords", "transcript", "caption", "label", "tooltip", "value", "lines"}
_MAX_DEPTH = 64


def _dict(v: object) -> dict:
    return v if isinstance(v, dict) else {}


def _walk(node: object, path: tuple, user: bool, out: list, depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        for k, v in node.items():
            _walk(v, path + (k,), user or (isinstance(k, str) and k in _USER_KEYS), out, depth + 1)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _walk(v, path + (i,), user, out, depth + 1)
    elif isinstance(node, str) and user and node:
        out.append((path, node))
