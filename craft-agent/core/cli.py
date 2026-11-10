"""craft-agent CLI: start an app adapter, bridge MCP, chat in the terminal."""
from __future__ import annotations

import argparse
import importlib
import os
import secrets
import sys
from pathlib import Path

from guard.fence import pretty

from .agent import Agent
from .mcp import MCPClient
from .providers import ProviderError, make_provider

ADAPTERS = {"filmcraft": ("adapters.filmcraft", "FilmCraft")}
LABEL = "Unofficial. Not made by the ArtCraft team."


def load_adapter(name: str):
    if name not in ADAPTERS:
        raise SystemExit(f"unknown app {name!r}; known: {', '.join(ADAPTERS)}")
    mod, cls = ADAPTERS[name]
    return getattr(importlib.import_module(mod), cls)()


def _print_event(kind: str, data: dict) -> None:
    if kind == "tool":
        print(f"  · {data['name']} {_short(data['args'])}", flush=True)
    elif kind == "tool_result" and data["is_error"]:
        print(f"    ✗ {data['text'][:200]}", flush=True)


def _short(obj: object, n: int = 140) -> str:
    import json
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[: n - 1] + "…"


def run_turn(agent: Agent, text: str) -> None:
    try:
        res = agent.turn(text)
    except ProviderError as e:
        print(f"[provider error] {e}")
        return
    print(f"AI: {res.reply}")
    if res.card:
        n = len(agent.cards)
        print(f"  [card {n}] {res.card.title} · {len(res.card.steps)} step(s) · undo ×{res.card.undo_count}  (/undo to revert)")


def cmd_start(a: argparse.Namespace) -> int:
    print(LABEL)
    adapter = load_adapter(a.app)
    token = secrets.token_hex(32)
    folder = Path(a.project).expanduser().resolve() if a.project else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    app = None
    if not a.no_launch:
        app = adapter.launch(token, folder, panel_token=secrets.token_hex(32))
        print(f"launched {adapter.name} (pid {app.pid}); waiting for control channel…")
        if not adapter.wait_ready(60, token):
            print("app did not open its control channel in 60 s")
            app.terminate()
            return 1
    else:
        import os
        token = os.environ.get(adapter.token_env) or token
    mcp = MCPClient(adapter.mcp_command(), adapter.mcp_env(token))
    try:
        info = mcp.initialize()
        server = info.get("serverInfo", {})
        print(f"MCP bridge up: {server.get('name', '?')} {server.get('version', '')}")
        try:
            provider = make_provider(a.provider, a.model)
        except ProviderError as e:
            print(e)
            return 2
        agent = Agent(adapter, mcp, provider, on_event=_print_event)
        print(f"{len(agent.tools)} tools · {agent.curated_found} curated commands · model {provider.label}")
        if a.say:
            for line in a.say:
                print(f"> {line}")
                run_turn(agent, line)
            if not a.interactive:
                return 0
        print("Type a request. /undo reverts the last card, /cards lists cards, /quit exits.")
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line in ("/quit", "/exit"):
                break
            if line == "/cards":
                for i, c in enumerate(agent.cards, 1):
                    print(f"  {i}. {'(undone) ' if c.undone else ''}{c.title} · {c.steps} · undo ×{c.undo_count}")
                continue
            if line == "/undo":
                live = [c for c in agent.cards if not c.undone]
                if not live:
                    print("nothing to undo")
                    continue
                errs = agent.undo(live[-1])
                print("undone" if not errs else f"undo errors: {errs}")
                continue
            run_turn(agent, line)
    finally:
        mcp.close()
        if app and not a.keep_open:
            app.terminate()
    return 0


def cmd_mcp(a: argparse.Namespace) -> int:
    from guard.hook import Guard

    from .proxy import ensure_app, fetch_catalog, serve
    adapter = load_adapter(a.app)
    folder = Path(a.project).expanduser().resolve() if a.project else None
    try:
        session = ensure_app(adapter, folder)
    except Exception as e:  # stdout is the MCP channel: report on stderr only
        print(f"craft-agent: {e}", file=sys.stderr)
        return 1
    env = adapter.mcp_env(session["control_token"])
    try:
        catalog = fetch_catalog(adapter, env)
        adapter.set_catalog(catalog)
        instructions = adapter.instructions(catalog)
    except Exception as e:  # visible in the activity log too: stderr is hidden by most MCP clients
        print(f"craft-agent: no command catalog ({e!r}); serving without instructions", file=sys.stderr)
        instructions = ""
        instr_error = repr(e)
    else:
        instr_error = ""
    guard = Guard(adapter, folder=folder, ask=a.ask == "on", enabled=a.guard == "on", held_via=a.held_via)
    folder_txt = pretty(str(folder)) if folder else "none"
    guard.log(f"proxy started · guard {a.guard} · ask mode {a.ask} · project folder {folder_txt} · "
              f"instructions {len(instructions)} chars" + (f" (FAILED: {instr_error})" if instr_error else ""))
    return serve(adapter, guard, child_env=env, instructions=instructions)


def cmd_chat(a: argparse.Namespace) -> int:
    """craft-agent's own chat loop, driving the app THROUGH the guarded proxy (same Guard as any MCP client)."""
    print(LABEL)
    proxy = [sys.executable, str(Path(__file__).resolve().parents[1] / "craft-agent"), "mcp", a.app,
             "--guard", a.guard, "--ask", a.ask] + (["--project", a.project] if a.project else [])
    mcp = MCPClient(proxy)
    try:
        info = mcp.initialize()
        try:
            provider = make_provider(a.provider, a.model, a.base_url, a.ctx, think=a.think == "on")
        except ProviderError as e:
            print(e)
            return 2
        names = set(a.tools.split(",")) if a.tools else None
        agent = Agent(load_adapter(a.app), mcp, provider, on_event=_print_event,
                      system=info.get("instructions") or None, tool_names=names)
        print(f"guard {a.guard} · ask {a.ask} · think {a.think} · {len(agent.tools)} tools · system {len(agent.system)} chars · {provider.label}")
        for line in a.say or []:
            print(f"> {line}")
            run_turn(agent, line)
        if getattr(provider, "trimmed", 0):
            print(f"(context: {provider.trimmed} tool result(s) trimmed to fit {a.ctx} tokens)")
    finally:
        mcp.close()
    return 0


def cmd_panel(a: argparse.Namespace) -> int:
    """Serve the in-app AI panel on 127.0.0.1. Brain: the user's Claude Code login (default) or an API key."""
    import time

    from guard.approvals import Approvals
    from guard.fence import inside
    from guard.hook import STATE_DIR
    from guard.scan import scan_text

    from .panel import ApiBrain, ClaudeCodeBrain, PanelServer, write_mcp_config
    from .proxy import ensure_app
    print(LABEL)
    adapter = load_adapter(a.app)
    folder = Path(a.project).expanduser().resolve() if a.project else None
    if folder:
        folder.mkdir(parents=True, exist_ok=True)
    session = ensure_app(adapter, folder)
    launcher = Path(__file__).resolve().parents[1] / "craft-agent"
    cfg = write_mcp_config(STATE_DIR / f"panel-mcp-{adapter.name}.json", adapter.name, launcher, folder, a.guard, a.ask)
    app_mcp = MCPClient(adapter.mcp_command(), adapter.mcp_env(session["control_token"]))
    app_mcp.initialize()
    if a.brain == "claude":
        brain = ClaudeCodeBrain(adapter.name, cfg, folder or Path.home(), model=a.model)
    else:
        proxy = MCPClient([sys.executable, str(launcher), "mcp", adapter.name, "--guard", a.guard, "--ask", a.ask]
                          + (["--project", str(folder)] if folder else []))
        info = proxy.initialize()
        try:
            provider = make_provider("anthropic", a.model)
        except ProviderError as e:
            print(e)
            return 2
        brain = ApiBrain(Agent(adapter, proxy, provider, system=info.get("instructions") or None))
    proxy_argv = [sys.executable, str(launcher), "mcp", adapter.name, "--guard", a.guard, "--ask", a.ask,
                  "--held-via", "panel"] + (["--project", str(folder)] if folder else [])

    def held_runner(tool: str, args: dict) -> dict:
        """Run one allowed call through a fresh guarded proxy (the Guard checks it again and consumes the OK)."""
        p = MCPClient(proxy_argv)
        try:
            p.initialize()
            return p.call_tool(tool, args)
        finally:
            p.close()

    server = PanelServer(adapter, session["panel_token"], brain, app_mcp, Approvals(STATE_DIR / "approvals"),
                         STATE_DIR / "events.jsonl", pretty(str(folder)) if folder else "none",
                         f"{a.guard} · ask {a.ask}", port=a.port, held_runner=held_runner, folder=folder,
                         state_dir=STATE_DIR, scan=scan_text if a.guard == "on" else None,
                         inside=lambda p: inside(p, folder), ask=a.ask == "on")
    server.start()
    print(f"AI panel server on 127.0.0.1:{server.port} · brain {a.brain} ({brain.model}) · guard {a.guard} · "
          f"folder {server.folder_label}. Open Window ▸ AI in {adapter.name}. Ctrl-C to stop.", flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
        app_mcp.close()
    return 0


def cmd_approve(a: argparse.Namespace) -> int:
    from guard.approvals import Approvals
    from guard.hook import STATE_DIR
    ap = Approvals(STATE_DIR / "approvals")
    if not a.code:
        for code, summary in ap.pending():
            print(f"{code}  {summary}")
        return 0
    try:
        summary = ap.approve(a.code)
    except ValueError as e:
        print(e)
        return 2
    print(f"approved {a.code}: {summary}" if summary is not None else f"no pending call {a.code}")
    return 0 if summary is not None else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="craft-agent", description=f"AI agent for Craft apps. {LABEL}")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start", help="launch the app, bridge MCP, chat")
    s.add_argument("app", nargs="?", default=next(iter(ADAPTERS)))
    s.add_argument("--project", help="project folder (the Guard fence, step 2)")
    s.add_argument("--provider", default="anthropic")
    s.add_argument("--model", default="sonnet", help="sonnet | opus | haiku | full model id")
    s.add_argument("--say", action="append", help="send this request non-interactively (repeatable)")
    s.add_argument("--interactive", action="store_true", help="stay in chat after --say")
    s.add_argument("--keep-open", action="store_true", help="leave the app running on exit")
    s.add_argument("--no-launch", action="store_true", help="attach to an app already running (token from env)")
    m = sub.add_parser("mcp", help="guarded MCP proxy on stdio (register it with any MCP client)")
    m.add_argument("app", nargs="?", default=next(iter(ADAPTERS)))
    m.add_argument("--project", help="project folder: the Guard's fence (no folder = every path is blocked)")
    m.add_argument("--guard", choices=("on", "off"), default=os.environ.get("CRAFT_AI_GUARD", "on"))
    m.add_argument("--ask", choices=("on", "off"), default="on", help="ask mode: risky commands wait for your OK")
    m.add_argument("--held-via", choices=("cli", "panel"), default="cli", help="where the user answers held calls")
    c = sub.add_parser("chat", help="craft-agent's own chat loop through the guarded proxy (any provider)")
    c.add_argument("app", nargs="?", default=next(iter(ADAPTERS)))
    c.add_argument("--provider", default="ollama", help="ollama | lmstudio | openai | anthropic")
    c.add_argument("--model", default="qwen3:4b")
    c.add_argument("--base-url", default="", help="OpenAI-compatible base URL (default per provider)")
    c.add_argument("--ctx", type=int, default=8192, help="context window in tokens (requests are trimmed to fit)")
    c.add_argument("--think", choices=("on", "off"), default="on", help="off: no reasoning (OpenAI-compatible only)")
    c.add_argument("--tools", default="", help="comma list of MCP tools to expose (default: all)")
    c.add_argument("--project")
    c.add_argument("--guard", choices=("on", "off"), default="on")
    c.add_argument("--ask", choices=("on", "off"), default="on")
    c.add_argument("--say", action="append", required=True, help="request to send (repeatable)")
    pn = sub.add_parser("panel", help="serve the in-app AI panel (brain: your Claude Code login, or an API key)")
    pn.add_argument("app", nargs="?", default=next(iter(ADAPTERS)))
    pn.add_argument("--project", help="project folder: the Guard's fence")
    pn.add_argument("--brain", choices=("claude", "api"), default="claude", help="claude = your Claude Code login (no key)")
    pn.add_argument("--model", default="sonnet", help="sonnet | opus | haiku")
    pn.add_argument("--guard", choices=("on", "off"), default="on")
    pn.add_argument("--ask", choices=("on", "off"), default="on")
    pn.add_argument("--port", type=int, default=7600)
    ap = sub.add_parser("approve", help="approve a held call by code (no code = list pending)")
    ap.add_argument("code", nargs="?")
    a = p.parse_args(argv)
    if a.cmd == "approve":
        return cmd_approve(a)
    if a.cmd == "chat":
        return cmd_chat(a)
    if a.cmd == "panel":
        return cmd_panel(a)
    if a.cmd == "start":
        return cmd_start(a)
    if a.cmd == "mcp":
        return cmd_mcp(a)
    return 1


if __name__ == "__main__":
    sys.exit(main())
