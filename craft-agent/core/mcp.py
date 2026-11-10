"""Minimal MCP client over stdio (newline-delimited JSON-RPC 2.0). Stdlib only."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPClient:
    def __init__(self, argv: list[str], env: dict[str, str] | None = None):
        full_env = dict(os.environ)
        full_env.update(env or {})
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=full_env, text=True, bufsize=1,
        )
        self._next = 1
        self._lock = threading.Lock()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        assert self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_tail = (self.stderr_tail + [line.rstrip()])[-20:]

    def _send(self, obj: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> Any:
        with self._lock:
            rid = self._next
            self._next += 1
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            assert self.proc.stdout
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise MCPError("MCP server exited: " + " | ".join(self.stderr_tail[-3:]))
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") != rid:
                    continue  # notifications / unrelated
                if "error" in msg:
                    raise MCPError(f"{method}: {msg['error'].get('message', msg['error'])}")
                return msg.get("result")

    def notify(self, method: str, params: dict | None = None) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> dict:
        res = self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "craft-agent", "version": "0.1"},
        })
        self.notify("notifications/initialized")
        return res

    def list_tools(self) -> list[dict]:
        return self.request("tools/list").get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Returns {"is_error": bool, "text": str, "json": parsed-or-None, "images": [...]}."""
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        texts, images = [], []
        for block in res.get("content", []):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "image":
                images.append(block)
        text = "\n".join(texts)
        parsed = res.get("structuredContent")
        if parsed is None:
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                parsed = None
        return {"is_error": bool(res.get("isError")), "text": text, "json": parsed, "images": images}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=3)
            for f in (self.proc.stdout, self.proc.stderr):
                if f:
                    f.close()
        except Exception:
            self.proc.kill()
