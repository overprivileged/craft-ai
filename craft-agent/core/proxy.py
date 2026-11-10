"""Guarded MCP proxy: a stdio MCP server that forwards to the app's official MCP server.

Any MCP client (Claude Code, Claude Desktop, Cursor, LM Studio) talks to this; every tools/call goes
through Guard.before (may block) and every result through Guard.after (may rewrite). App-agnostic:
everything app-specific comes from the Adapter.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
from pathlib import Path

SESSION_DIR = Path.home() / ".craft-ai"


def _session_path(adapter) -> Path:
    return SESSION_DIR / f"{adapter.name}.session.json"


def load_session(adapter) -> dict | None:
    try:
        return json.loads(_session_path(adapter).read_text())
    except (OSError, ValueError):
        return None


def ensure_app(adapter, folder: Path | None = None, timeout: float = 60) -> dict:
    """Reuse the running app if its saved token still works, else launch it and save a fresh session.

    Launching is serialized with a lock file: several MCP clients (Claude Code reconnecting, the panel,
    a terminal) may start at the same moment, and without it each would launch its own app. After
    taking the lock the saved session is checked again, so the later ones reuse the first one's app.
    An app that never answers is stopped, never left running.
    The session file holds local session tokens only (never API keys), mode 0600.
    """
    import fcntl
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(SESSION_DIR / f"{adapter.name}.launch.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        s = load_session(adapter)
        if s and adapter.wait_ready(3, s.get("control_token", "")):
            return s
        s = {"control_token": secrets.token_hex(32), "panel_token": secrets.token_hex(32)}
        proc = adapter.launch(s["control_token"], folder, panel_token=s["panel_token"])
        s["pid"] = proc.pid if proc else None
        if not adapter.wait_ready(timeout, s["control_token"]):
            if proc is not None and proc.poll() is None:
                proc.terminate()
            raise RuntimeError(f"{adapter.name} did not answer on its control channel in {timeout:.0f}s "
                               f"(stopped the app it had started)")
        p = _session_path(adapter)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(s, f)
        return s
    finally:
        os.close(lock_fd)  # closing releases the lock


def fetch_catalog(adapter, env: dict) -> dict[str, dict]:
    """One command_list call through the app's MCP server, for the instructions' curated list."""
    from .agent import _command_index
    from .mcp import MCPClient
    m = MCPClient(adapter.mcp_command(), env)
    try:
        m.initialize()
        return _command_index(m.call_tool("command_list", {})["json"])
    finally:
        m.close()


HOME = str(Path.home())


def hide_home(obj, home: str = HOME):
    """Replace the user's home folder with `~` in every string (results going to the model or the panel),
    so a Mac user name never ends up in a chat, a screenshot or a saved transcript."""
    if not home or home == "/":
        return obj
    if isinstance(obj, str):
        return obj.replace(home, "~")
    if isinstance(obj, list):
        return [hide_home(x, home) for x in obj]
    if isinstance(obj, dict):
        return {k: hide_home(v, home) for k, v in obj.items()}
    return obj


def _err_result(rid, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def serve(adapter, guard, inp=None, out=None, child_argv: list[str] | None = None, child_env: dict | None = None,
          instructions: str = "") -> int:
    inp = inp or sys.stdin
    out = out or sys.stdout
    env = dict(os.environ)
    env.update(child_env or {})
    child = subprocess.Popen(
        child_argv or adapter.mcp_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1,
    )
    out_lock = threading.Lock()
    pending: dict = {}  # request id -> (tool, args) for tools/call
    pending_lock = threading.Lock()
    init_ids: set = set()

    def emit(obj: dict) -> None:
        with out_lock:
            out.write(json.dumps(obj, ensure_ascii=False) + "\n")
            out.flush()

    def from_child() -> None:
        assert child.stdout
        for line in child.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            rid = msg.get("id") if isinstance(msg, dict) else None
            with pending_lock:
                call = pending.pop(rid, None) if rid is not None and "method" not in msg else None
            if rid in init_ids and "method" not in msg and isinstance(msg.get("result"), dict) and instructions:
                prev = msg["result"].get("instructions") or ""
                msg["result"]["instructions"] = (prev + "\n\n" if prev else "") + instructions
                init_ids.discard(rid)
            if call and "result" in msg:
                try:
                    msg["result"] = guard.after(call[0], call[1], msg["result"])
                except Exception as e:  # a guard bug must fail closed, not leak the raw result
                    msg = _err_result(rid, f"guard error: {e}")
            if "result" in msg:  # privacy, independent of the Guard: `~` instead of the home folder
                msg["result"] = hide_home(msg["result"])
            emit(msg)

    t = threading.Thread(target=from_child, daemon=True)
    t.start()
    assert child.stdin
    for line in inp:
        try:
            msg = json.loads(line)
        except ValueError:
            emit({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        if isinstance(msg, dict) and msg.get("method") == "initialize":
            init_ids.add(msg.get("id"))
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            params = msg.get("params") or {}
            tool, args = params.get("name", ""), params.get("arguments") or {}
            try:
                verdict = guard.before(tool, args)
            except Exception as e:
                verdict = type("V", (), {"allow": False, "reason": f"guard error: {e}"})()
            if not verdict.allow:
                emit(_err_result(msg.get("id"), f"Blocked by Craft AI Guard: {verdict.reason}"))
                continue
            with pending_lock:
                pending[msg.get("id")] = (tool, args)
        try:
            child.stdin.write(json.dumps(msg) + "\n")
            child.stdin.flush()
        except (BrokenPipeError, OSError):
            break
    try:
        child.stdin.close()
        child.wait(timeout=5)
    except Exception:
        child.kill()
    t.join(timeout=2)
    return 0
