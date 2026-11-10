"""Model providers. Stdlib only (urllib). Keys come from env or the macOS Keychain, never files or logs."""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request

ANTHROPIC_MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}
# Server-side refusal fallback ("default" routing) is supported on these.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
KEYCHAIN_SERVICE = "craft-ai-anthropic"


class ProviderError(RuntimeError):
    pass


def get_key(env_var: str, keychain_service: str) -> str:
    key = os.environ.get(env_var, "").strip()
    if key:
        return key
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", keychain_service, "-w"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    raise ProviderError(
        f"No API key. Set {env_var}, or store it in the Keychain: "
        f"security add-generic-password -s {keychain_service} -a craft-ai -w"
    )


class AnthropicProvider:
    """Messages API, manual tool loop. Returns the raw response dict."""

    URL = "https://api.anthropic.com/v1/messages"
    VERSION = "2026-10-21"

    def __init__(self, model: str = "sonnet", effort: str = "medium"):
        self.model = ANTHROPIC_MODELS.get(model, model)
        self.effort = effort
        self._key = get_key("ANTHROPIC_API_KEY", KEYCHAIN_SERVICE)
        self.use_fallbacks = True  # turned off for the session if the API rejects the field/beta

    @property
    def label(self) -> str:
        return f"Anthropic {self.model}"

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        body: dict = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": self.effort},
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": self._key,
            "anthropic-version": self.VERSION,
        }
        if self.model in _FALLBACK_MODELS and self.use_fallbacks:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = "server-side-fallback-2026-07-01"
        data = json.dumps(body).encode()
        for attempt in range(4):
            req = urllib.request.Request(self.URL, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=600) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code == 400 and self.use_fallbacks and ("fallback" in detail or "anthropic-beta" in detail):
                    self.use_fallbacks = False
                    body.pop("fallbacks", None)
                    headers.pop("anthropic-beta", None)
                    data = json.dumps(body).encode()
                    continue
                if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                    time.sleep(float(e.headers.get("retry-after") or 2 ** attempt))
                    continue
                raise ProviderError(f"Anthropic HTTP {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                if attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ProviderError(f"Anthropic unreachable: {e.reason}") from None
        raise ProviderError("Anthropic: retries exhausted")


class OpenAICompatProvider:
    """OpenAI-compatible /chat/completions (OpenAI, Ollama, LM Studio). Takes and returns the same
    Anthropic-shaped messages as the agent loop, so the loop has one format.

    Context budget: small local models have small windows and servers like Ollama silently drop the
    *start* of an over-long prompt (the system prompt). We estimate tokens (chars/3.5) and trim old
    tool results first, then the newest, so the request always fits `ctx_tokens`.
    """

    CHARS_PER_TOKEN = 3.5

    def __init__(self, base_url: str, model: str, ctx_tokens: int = 8192, max_out: int = 1536,
                 key_env: str = "OPENAI_API_KEY", timeout: float = 600, think: bool = True):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.ctx_tokens = ctx_tokens
        self.max_out = max_out
        self.timeout = timeout
        self._key = os.environ.get(key_env, "").strip()  # optional: local servers need none
        self.trimmed = 0  # how many tool results were cut to fit (for logs)
        self.think = think  # False sends reasoning_effort "none" (Ollama honours it; `think:false` it ignores)

    @property
    def label(self) -> str:
        return f"OpenAI-compatible {self.model} @ {self.url.rsplit('/chat', 1)[0]}"

    # ---- format conversion ----
    @staticmethod
    def to_openai(system: str, messages: list[dict]) -> list[dict]:
        out: list[dict] = [{"role": "system", "content": system}]
        for m in messages:
            c = m["content"]
            if isinstance(c, str):
                out.append({"role": m["role"], "content": c})
                continue
            if m["role"] == "assistant":
                text = "".join(b.get("text", "") for b in c if b.get("type") == "text")
                calls = [{"id": b["id"], "type": "function",
                          "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                         for b in c if b.get("type") == "tool_use"]
                msg: dict = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for b in c:
                    if b.get("type") == "tool_result":
                        rc = b.get("content")
                        txt = rc if isinstance(rc, str) else "\n".join(
                            x.get("text", "") for x in rc if isinstance(x, dict) and x.get("type") == "text")
                        out.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": txt})
                    elif b.get("type") == "text":
                        out.append({"role": "user", "content": b["text"]})
        return out

    @staticmethod
    def from_openai(resp: dict) -> dict:
        choices = resp.get("choices") or [{}]
        msg = choices[0].get("message") or {}
        content: list[dict] = []
        text = _strip_think(msg.get("content") or "")
        if text:
            content.append({"type": "text", "text": text})
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                args = {"_invalid_json": str(raw)[:500]}
            if not isinstance(args, dict):
                args = {"_invalid_json": str(raw)[:500]}
            content.append({"type": "tool_use", "id": tc.get("id") or f"call_{i}", "name": fn.get("name", ""), "input": args})
        has_tools = any(b["type"] == "tool_use" for b in content)
        return {"content": content, "stop_reason": "tool_use" if has_tools else "end_turn",
                "usage": resp.get("usage", {})}

    @staticmethod
    def tools_openai(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                                  "parameters": t.get("input_schema") or {"type": "object"}}}
                for t in tools]

    def fit(self, msgs: list[dict], tools: list[dict]) -> list[dict]:
        budget = (self.ctx_tokens - self.max_out) * self.CHARS_PER_TOKEN - len(json.dumps(tools))
        size = lambda: sum(len(json.dumps(m)) for m in msgs)  # noqa: E731
        tool_idx = [i for i, m in enumerate(msgs) if m["role"] == "tool"]
        for i in tool_idx[:-1]:  # oldest results first, keep the newest whole if we can
            if size() <= budget:
                break
            msgs[i] = dict(msgs[i], content="[earlier result trimmed to fit the context window]")
            self.trimmed += 1
        if size() > budget and tool_idx:
            last = tool_idx[-1]
            body = msgs[last]["content"]
            keep = len(body)
            # size is measured JSON-escaped (quotes/newlines can double), so shrink until it really fits
            while size() > budget and keep > 400:
                keep = max(400, keep - int(size() - budget) - 200)
                msgs[last] = dict(msgs[last], content=body[:keep] + f"\n…[cut: {len(body) - keep} more chars]")
            self.trimmed += 1
        if size() > budget:
            raise ProviderError(f"request does not fit {self.ctx_tokens} tokens even after trimming "
                                f"(tools {len(json.dumps(tools))} chars); use fewer tools or a bigger context")
        return msgs

    def chat(self, system: str, messages: list[dict], tools: list[dict]) -> dict:
        otools = self.tools_openai(tools)
        msgs = self.fit(self.to_openai(system, messages), otools)
        body = {"model": self.model, "messages": msgs, "tools": otools, "max_tokens": self.max_out,
                "temperature": 0.2, "stream": False}
        if not self.think:
            body["reasoning_effort"] = "none"
        headers = {"content-type": "application/json"}
        if self._key:
            headers["authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return self.from_openai(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            raise ProviderError(f"{self.label}: HTTP {e.code}: {e.read().decode(errors='replace')[:400]}") from None
        except urllib.error.URLError as e:
            raise ProviderError(f"{self.label}: unreachable ({e.reason})") from None


def _strip_think(text: str) -> str:
    import re
    text = re.sub(r"<think>.*?(</think>|$)", "", text, flags=re.S)
    if "</think>" in text:  # reasoning without the opening tag (seen with reasoning_effort "none")
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


def make_provider(name: str, model: str, base_url: str = "", ctx: int = 8192, think: bool = True):
    if name == "anthropic":
        return AnthropicProvider(model)
    if name in ("openai", "ollama", "lmstudio"):
        default = {"ollama": "http://127.0.0.1:11434/v1", "lmstudio": "http://127.0.0.1:1234/v1",
                   "openai": "https://api.openai.com/v1"}[name]
        return OpenAICompatProvider(base_url or default, model, ctx_tokens=ctx, think=think)
    raise ProviderError(f"unknown provider {name!r} (anthropic | openai | ollama | lmstudio)")
