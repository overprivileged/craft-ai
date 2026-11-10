"""Panel server: the in-app AI panel talks to this over 127.0.0.1 (JSON lines, `{"v":1,…}`, token).

Brain (default): the user's own Claude Code login, `claude -p … --resume <session> --output-format
stream-json`, with the guarded MCP proxy as its only tool server. No API key. The API-key provider
is an optional second brain. App-agnostic: everything app-specific comes from the Adapter.

Panel → agent: hello{token}, send{text}, model{model}, card_undo{n}, card_apply{n},
               held_allow{code}, held_deny{code}
Agent → panel: hello, user, ai_delta, ai_done, tool, card, banner, held, status, error
"""
from __future__ import annotations

import hmac
import json
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import recipes as rcp
from .proxy import hide_home

PROTOCOL = 1
MAX_LINE = 1 << 20
MODELS = ("sonnet", "opus", "haiku")


# ---------------------------------------------------------------- brains
class ClaudeCodeBrain:
    """One persistent conversation driven through `claude -p --resume`."""

    def __init__(self, app_name: str, mcp_config: Path, cwd: Path, model: str = "sonnet", claude: str = "claude"):
        self.server = f"craft-{app_name}"
        self.mcp_config = mcp_config
        self.cwd = cwd
        self.model = model
        self.claude = claude
        self.session: str | None = None
        self.models = MODELS
        self.proc: subprocess.Popen | None = None
        self.cancelled = False

    def cancel(self) -> None:
        """Stop the running turn (the user pressed Stop)."""
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            p.terminate()

    def argv(self, text: str) -> list[str]:
        a = [self.claude, "-p", text, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
             "--mcp-config", str(self.mcp_config), "--strict-mcp-config",
             "--allowedTools", f"mcp__{self.server}", "--model", self.model]
        if self.session:
            a += ["--resume", self.session]
        return a

    def run(self, text: str, on_delta, on_tool) -> dict:
        """Run one turn. Returns {"text", "tools": [(tool, args)], "error"}."""
        prefix = f"mcp__{self.server}__"
        self.cancelled = False
        try:
            proc = subprocess.Popen(self.argv(text), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, cwd=str(self.cwd), bufsize=1)
        except OSError as e:
            return {"text": "", "tools": [], "error": f"could not start Claude Code: {e}"}
        self.proc = proc
        tools: list[tuple[str, dict]] = []
        final, err, started_text = "", "", False
        assert proc.stdout
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if kind == "stream_event":
                e = ev.get("event") or {}
                if e.get("type") == "content_block_start" and (e.get("content_block") or {}).get("type") == "text":
                    if started_text:
                        on_delta("\n\n")
                elif e.get("type") == "content_block_delta" and (e.get("delta") or {}).get("type") == "text_delta":
                    started_text = True
                    on_delta(e["delta"].get("text", ""))
            elif kind == "assistant":
                for b in (ev.get("message") or {}).get("content", []):
                    if b.get("type") == "tool_use":
                        name = str(b.get("name", ""))
                        if not name.startswith(prefix):
                            continue  # Claude Code's own tools (ToolSearch…): not app actions
                        name = name[len(prefix):]
                        args = b.get("input") if isinstance(b.get("input"), dict) else {}
                        tools.append((name, args))
                        on_tool(name, args)
            elif kind == "result":
                self.session = ev.get("session_id") or self.session
                final = ev.get("result") or ""
                if ev.get("is_error") or ev.get("subtype") not in (None, "success"):
                    err = final or str(ev.get("subtype"))
        proc.wait()
        self.proc = None
        if self.cancelled:
            return {"text": final, "tools": tools, "error": "", "stopped": True}
        if proc.returncode and not err:
            err = (proc.stderr.read() if proc.stderr else "")[-400:] or f"Claude Code exited with {proc.returncode}"
        return {"text": final, "tools": tools, "error": err}


class ApiBrain:
    """Optional: craft-agent's own loop with an API-key provider, over the guarded proxy."""

    def __init__(self, agent, models=MODELS):
        self.agent = agent
        self.models = models

    @property
    def model(self) -> str:
        return getattr(self.agent.provider, "model", "")

    @model.setter
    def model(self, m: str) -> None:
        from .providers import ANTHROPIC_MODELS
        if hasattr(self.agent.provider, "model"):
            self.agent.provider.model = ANTHROPIC_MODELS.get(m, m)

    def cancel(self) -> None:
        """The API loop can't be interrupted mid-request; the turn finishes on its own."""

    def run(self, text: str, on_delta, on_tool) -> dict:
        old = self.agent.on_event
        self.agent.on_event = lambda kind, d: on_tool(d["name"], d["args"]) if kind == "tool" else None
        try:
            r = self.agent.turn(text)
            if r.reply:
                on_delta(r.reply)
            return {"text": r.reply, "tools": [(n, a) for n, a, _ in r.tool_calls], "error": ""}
        except Exception as e:
            return {"text": "", "tools": [], "error": str(e)}
        finally:
            self.agent.on_event = old


# ---------------------------------------------------------------- server
@dataclass
class Card:
    n: int
    title: str
    steps: list[str]
    undo_count: int
    state: str = "applied"

    def msg(self) -> dict:
        return {"type": "card", "n": self.n, "title": self.title, "steps": self.steps,
                "undo_count": self.undo_count, "state": self.state}


@dataclass
class Held:
    code: str
    summary: str
    reason: str
    state: str = "pending"
    commands: list[str] = field(default_factory=list)
    chat_allowable: bool = False

    def msg(self) -> dict:
        return {"type": "held", "code": self.code, "summary": self.summary, "reason": self.reason, "state": self.state,
                "chat_allowable": self.chat_allowable}


@dataclass
class _Client:
    sock: socket.socket
    lock: threading.Lock = field(default_factory=threading.Lock)


class PanelServer:
    def __init__(self, adapter, token: str, brain, app_mcp, approvals, events_path: Path,
                 folder_label: str, guard_label: str, port: int = 7600, host: str = "127.0.0.1", held_runner=None,
                 folder: Path | None = None, state_dir: Path | None = None, activity_log: Path | None = None,
                 scan=None, inside=None, ask: bool = True):
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("the panel server listens on loopback only")
        self.adapter = adapter
        self.held_runner = held_runner  # (tool, args) -> MCP result dict, through the guarded proxy
        self.folder = folder
        self.state_dir = state_dir or events_path.parent
        self.activity_log = activity_log or self.state_dir / "activity.log"
        self.chat_allow_path = self.state_dir / "chat-allow.json"
        self.chat_id = os.urandom(8).hex()
        self.chat_allow: set[str] = set()
        self.scan = scan or (lambda text: [])            # the Guard's hidden-instruction scan
        self.inside = inside or (lambda path: False)     # the Guard's fence
        self.ask = ask
        self.selection: dict | None = None
        self._write_chat_allow()  # a new chat starts with nothing allowed
        self.token = token
        self.brain = brain
        self.app_mcp = app_mcp  # direct app MCP client: history depth, undo/redo (the user's own clicks)
        self.app_lock = threading.Lock()
        self.approvals = approvals
        self.events_path = events_path
        self.folder_label = folder_label
        self.guard_label = guard_label
        self.port = port
        self.host = host
        self.clients: list[_Client] = []
        self.clients_lock = threading.Lock()
        self.history: list[dict] = []  # replayed to a panel that (re)connects
        self.cards: list[Card] = []
        self.held: dict[str, Held] = {}
        self.denied_notes: list[str] = []
        self.busy = False
        self.turn_id = 0
        self.stop = threading.Event()
        self.sock: socket.socket | None = None

    # ---- outgoing ----
    def broadcast(self, msg: dict, keep: bool = True) -> None:
        msg = {"v": PROTOCOL, **hide_home(msg)}
        if keep:
            self.history.append(msg)
            if len(self.history) > 2000:
                del self.history[:500]
        line = (json.dumps(msg, ensure_ascii=False) + "\n").encode()
        with self.clients_lock:
            clients = list(self.clients)
        for c in clients:
            try:
                with c.lock:
                    c.sock.sendall(line)
            except OSError:
                self._drop(c)

    def _send(self, c: _Client, msg: dict) -> None:
        with c.lock:
            c.sock.sendall((json.dumps({"v": PROTOCOL, **hide_home(msg)}, ensure_ascii=False) + "\n").encode())

    def _drop(self, c: _Client) -> None:
        with self.clients_lock:
            if c in self.clients:
                self.clients.remove(c)
        try:
            c.sock.close()
        except OSError:
            pass

    def hello(self) -> dict:
        return {"type": "hello", "app": self.adapter.name, "folder": self.folder_label, "guard": self.guard_label,
                "model": self.brain.model, "models": list(self.brain.models)}

    # ---- lifecycle ----
    def start(self) -> None:
        s = socket.socket(socket.AF_INET6 if ":" in self.host else socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(4)
        s.settimeout(0.5)
        self.port = s.getsockname()[1]
        self.sock = s
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._events_loop, daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        if self.sock:
            self.sock.close()
        with self.clients_lock:
            for c in list(self.clients):
                try:
                    c.sock.close()
                except OSError:
                    pass
            self.clients.clear()

    def _accept_loop(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._client_loop, args=(conn,), daemon=True).start()

    def _client_loop(self, conn: socket.socket) -> None:
        conn.settimeout(5)
        f = conn.makefile("rb")
        try:
            first = f.readline(MAX_LINE + 1)
            hello = json.loads(first or b"{}")
            ok = (isinstance(hello, dict) and hello.get("v") == PROTOCOL and hello.get("type") == "hello"
                  and isinstance(hello.get("token"), str) and hmac.compare_digest(hello["token"], self.token))
        except (OSError, ValueError):
            ok = False
        if not ok:
            try:
                conn.sendall(b'{"v":1,"type":"error","text":"bad token"}\n')
            except OSError:
                pass
            conn.close()
            return
        conn.settimeout(None)
        c = _Client(conn)
        try:
            self._send(c, self.hello())
            for m in list(self.history):
                self._send(c, {k: v for k, v in m.items() if k != "v"})
            self._send(c, {"type": "status", "busy": self.busy, "text": "Working…" if self.busy else ""})
            self._send(c, {"type": "activity", "reset": True, "lines": self._activity_tail(300)})
            self._send(c, self.recipes_msg())
            self._send(c, self.selection_msg())
        except OSError:
            conn.close()
            return
        with self.clients_lock:
            self.clients.append(c)
        while not self.stop.is_set():
            try:
                line = f.readline(MAX_LINE + 1)
            except OSError:
                break
            if not line:
                break
            if len(line) > MAX_LINE:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("v") == PROTOCOL:
                try:
                    self.handle(msg)
                except Exception as e:  # one bad message must not kill the connection
                    self.broadcast({"type": "error", "text": f"agent error: {e}"}, keep=False)
        self._drop(c)

    # ---- incoming ----
    def handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "send":
            text = str(msg.get("text", "")).strip()[:20000]
            if text:
                self.start_turn(text, context=self._selection_context() if msg.get("use_selection") else "")
        elif kind == "model":
            m = str(msg.get("model", ""))
            if m in self.brain.models:
                self.brain.model = m
                self.broadcast(self.hello(), keep=False)
        elif kind in ("card_undo", "card_apply"):
            self.card_action(kind, msg.get("n"))
        elif kind in ("held_allow", "held_deny"):
            self.held_action(kind, str(msg.get("code", "")), scope=str(msg.get("scope", "once")))
        elif kind == "stop":
            if self.busy:
                self.brain.cancel()
        elif kind == "save_chat":
            self.save_chat()
        elif kind == "recipe_preview":
            self.recipe_preview(str(msg.get("id", "")), msg.get("values"))
        elif kind == "recipe_run":
            self.recipe_run(str(msg.get("id", "")), msg.get("values"))
        elif kind == "save_recipe":
            self.save_recipe(msg.get("n"))

    def start_turn(self, text: str, echo: bool = True, context: str = "") -> bool:
        if self.busy:
            self.broadcast({"type": "error", "text": "Still working on the last request."}, keep=False)
            return False
        self.busy = True
        threading.Thread(target=self._turn, args=(text, echo, context), daemon=True).start()
        return True

    # ---- selection context ("fix THIS clip") ----
    def _read_selection(self) -> dict | None:
        try:
            with self.app_lock:
                return self.adapter.selection(self.app_mcp, self.scan)
        except Exception:
            return None

    def _selection_context(self) -> str:
        sel = self._read_selection()  # fresh: what is selected at the moment the request is sent
        if sel:
            self.selection = sel
        return (sel or {}).get("context", "")

    def selection_msg(self) -> dict:
        s = self.selection or {}
        return {"type": "selection", "text": str(s.get("text", "")), "count": int(s.get("count", 0))}

    def _labels(self) -> list[str] | None:
        try:
            with self.app_lock:
                return self.adapter.history_labels(self.app_mcp)
        except Exception:
            return None

    def _card_for(self, title: str, before: list[str] | None, after: list[str] | None, fallback_steps: list[str]) -> None:
        """A change card for what the undo stack gained (real, undoable edits only)."""
        if before is not None and after is not None:
            undo = max(0, len(after) - len(before))
            steps = after[len(before):] if after[:len(before)] == before else after[-undo:] if undo else []
        else:
            steps = fallback_steps
            undo = len(steps)
        if undo:
            card = Card(len(self.cards) + 1, title, steps, undo)
            self.cards.append(card)
            self.broadcast(card.msg())

    def _turn(self, text: str, echo: bool, context: str = "") -> None:
        self.turn_id += 1
        tid = self.turn_id
        try:
            if echo:
                self.broadcast({"type": "user", "id": tid, "text": text})
            self.broadcast({"type": "status", "busy": True, "text": "Working…"}, keep=False)
            prompt = f"{context}\n\n{text}" if context else text
            if self.denied_notes:
                prompt = " ".join(self.denied_notes) + "\n\n" + prompt
                self.denied_notes.clear()
            before = self._labels()
            res = self.brain.run(
                prompt,
                on_delta=lambda d: self.broadcast({"type": "ai_delta", "id": tid, "text": d}, keep=False),
                on_tool=lambda name, args: self.broadcast({"type": "tool", "id": tid, "summary": _tool_summary(name, args)}),
            )
            after = self._labels()
            self.broadcast({"type": "ai_done", "id": tid, "text": res["text"] or ("Stopped." if res.get("stopped") else "")})
            if res["error"]:
                self.broadcast({"type": "error", "text": res["error"][:400]}, keep=False)
            fallback = [s for name, args in res["tools"] for s in self.adapter.is_edit(name, args)]
            self._card_for(text if echo else "Follow-up", before, after, fallback)
        finally:
            self.busy = False
            self.broadcast({"type": "status", "busy": False, "text": ""}, keep=False)

    def card_action(self, kind: str, n) -> None:
        card = next((c for c in self.cards if c.n == n), None)
        if card is None or self.busy:
            return
        if kind == "card_undo":
            newer = [c for c in self.cards if c.n > card.n and c.state == "applied"]
            if card.state != "applied" or newer:
                self.broadcast({"type": "error", "text": "Undo the newer change first."}, keep=False)
                return
            cmd, new_state = self.adapter.undo_step, "undone"
        else:
            older_undone = [c for c in self.cards if c.n < card.n and c.state == "undone"]
            if card.state != "undone" or older_undone:
                self.broadcast({"type": "error", "text": "Apply the older change first."}, keep=False)
                return
            cmd, new_state = self.adapter.redo_step, "applied"
        errors = []
        with self.app_lock:
            for _ in range(card.undo_count):
                r = self.app_mcp.call_tool("command_run", {"id": cmd, "params": {}})
                if r["is_error"]:
                    errors.append(r["text"])
                    break
        if errors:
            self.broadcast({"type": "error", "text": f"{cmd}: {errors[0][:200]}"}, keep=False)
            return
        card.state = new_state
        self.broadcast(card.msg())

    def _write_chat_allow(self) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.chat_allow_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"chat": self.chat_id, "commands": sorted(self.chat_allow)}, f)
        except OSError:
            pass

    def held_action(self, kind: str, code: str, scope: str = "once") -> None:
        h = self.held.get(code)
        if h is None or h.state != "pending":
            return
        if kind == "held_allow":
            if self.busy:
                self.broadcast({"type": "error", "text": "Wait for the AI to finish, then Allow."}, keep=False)
                return
            try:
                call = self.approvals.call(code)
                summary = self.approvals.approve(code)
            except ValueError:
                return
            h.state = "allowed"
            if scope == "chat" and h.chat_allowable and h.commands:
                self.chat_allow.update(h.commands)
                self._write_chat_allow()
                h.state = "allowed_chat"
            self.broadcast(h.msg())
            if summary is None:
                return
            if call is None or self.held_runner is None:  # no exact call stored: ask the model to redo it
                self.start_turn(f"The user allowed this action in the AI panel: {h.summary}. Do it now, exactly once.", echo=False)
                return
            self.busy = True
            threading.Thread(target=self._run_held, args=(h, call), daemon=True).start()
        else:
            try:
                self.approvals.deny(code)
            except ValueError:
                return
            h.state = "denied"
            self.broadcast(h.msg())
            self.denied_notes.append(f"(The user denied this action in the AI panel: {h.summary}. Do not try it again.)")

    def _run_held(self, h: Held, call: tuple[str, dict]) -> None:
        """Allow: run exactly the held call once (through the guarded proxy, which consumes the approval),
        make a card for it, then tell the model what happened."""
        tool, args = call
        try:
            self.broadcast({"type": "status", "busy": True, "text": "Running the allowed action…"}, keep=False)
            before = self._labels()
            try:
                res = self.held_runner(tool, args)
                ok, text = not res.get("is_error"), str(res.get("text", ""))
            except Exception as e:
                ok, text = False, str(e)
            after = self._labels()
            self.broadcast({"type": "tool", "id": self.turn_id, "summary": f"allowed: {_tool_summary(tool, args)}"})
            self._card_for(f"Allowed: {h.summary}"[:200], before, after, self.adapter.is_edit(tool, args) if ok else [])
            if not ok:
                self.broadcast({"type": "error", "text": f"The allowed action failed: {text[:300]}"}, keep=False)
        finally:
            self.busy = False
        outcome = "It ran" if ok else "It failed"
        self.start_turn(f"The user allowed the held action in the AI panel and the panel ran it once: {h.summary}. "
                        f"{outcome}; result: {text[:800]}. Do not run it again. Finish the user's request if anything "
                        f"is left, otherwise say in one sentence what changed.", echo=False)

    # ---- Guard events from the proxy ----
    # ---- recipes (untrusted input: scanned, previewed, never able to change the Guard) ----
    def _log(self, line: str) -> None:
        try:
            with self.activity_log.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.adapter.name}: {hide_home(line)}\n")
        except OSError:
            pass

    def _recipe(self, rid: str) -> tuple[dict, list[str], str]:
        """(clean recipe, ignored keys, support note) for `builtin:<slug>` or `file:<slug>`."""
        kind, _, slug = rid.partition(":")
        if kind == "builtin":
            raw = self.adapter.builtin_recipes.get(slug)
            if raw is None:
                raise rcp.RecipeError("no such built-in recipe")
            clean, _ = rcp.validate({k: v for k, v in raw.items() if k != "support"})
            return clean, [], str(raw.get("support", ""))
        if kind == "file" and self.folder is not None:
            clean, ignored = rcp.load_file(self.folder, slug)
            return clean, ignored, ""
        raise rcp.RecipeError("unknown recipe")

    def recipes_msg(self) -> dict:
        items = []
        for slug, raw in self.adapter.builtin_recipes.items():
            items.append({"id": f"builtin:{slug}", "name": raw.get("name", slug), "description": raw.get("description", ""),
                          "params": raw.get("params", {}), "builtin": True, "support": raw.get("support", "")})
        for slug in rcp.list_files(self.folder):
            try:
                clean, _ignored = rcp.load_file(self.folder, slug)  # type: ignore[arg-type]
                items.append({"id": f"file:{slug}", "name": clean["name"], "description": clean["description"][:300],
                              "params": clean["params"], "builtin": False, "support": ""})
            except rcp.RecipeError as e:
                items.append({"id": f"file:{slug}", "name": slug, "description": f"Can't load: {e}", "params": {},
                              "builtin": False, "support": ""})
        return {"type": "recipes", "items": items[:100]}

    def recipe_preview(self, rid: str, values) -> dict | None:
        try:
            recipe, ignored, support = self._recipe(rid)
        except rcp.RecipeError as e:
            self.broadcast({"type": "error", "text": f"Recipe: {e}"}, keep=False)
            return None
        pv = rcp.preview(recipe, ignored, values if isinstance(values, dict) else None,
                         scan=self.scan, inside=self.inside, ask=self.ask, support=support)
        if pv["flagged"]:
            first = pv["warnings"][0]
            self.broadcast({"type": "banner", "text": "This recipe is talking to your AI",
                            "preview": f"{first['field']}: {first['text'][:200]}"})
            self._log(f"recipe \"{recipe['name']}\" has text that talks to an AI in its {first['field']} "
                      f"(\"{first['text'][:80]}\") → shown in the preview, not run")
        if ignored:
            self._log(f"recipe \"{recipe['name']}\" tried to set {', '.join(ignored)} → ignored "
                      f"(recipes can't change the Guard, the fence or the allow rule)")
        self.broadcast({"type": "recipe_preview", "id": rid, **pv}, keep=False)
        return pv

    def recipe_run(self, rid: str, values) -> None:
        try:
            recipe, _ignored, _support = self._recipe(rid)  # re-read: never trust text sent back by the panel
        except rcp.RecipeError as e:
            self.broadcast({"type": "error", "text": f"Recipe: {e}"}, keep=False)
            return
        request, _vals = rcp.render(recipe, values if isinstance(values, dict) else None)
        self._log(f"ran recipe \"{recipe['name']}\"")
        self.start_turn(f"[Recipe: {recipe['name']}] {request}")

    def save_recipe(self, n) -> None:
        card = next((c for c in self.cards if c.n == n), None)
        if card is None or self.folder is None:
            self.broadcast({"type": "error", "text": "Can't save that card as a recipe."}, keep=False)
            return
        title = card.title.removeprefix("Allowed: ")
        try:
            slug = rcp.save(self.folder, title[:60], f"Saved from a change: {title[:200]}", title, card.steps)
        except (rcp.RecipeError, OSError) as e:
            self.broadcast({"type": "error", "text": f"Couldn't save the recipe: {e}"}, keep=False)
            return
        self._log(f"saved recipe \"{title[:60]}\" ({slug}.json in the project folder)")
        self.broadcast({"type": "note", "text": f"Saved recipe \"{title[:60]}\" to .craft-ai/recipes/{slug}.json"}, keep=False)
        self.broadcast(self.recipes_msg(), keep=False)

    def _activity_tail(self, n: int) -> list[str]:
        try:
            with self.activity_log.open("rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 256 * 1024))
                lines = f.read().decode(errors="replace").splitlines()
        except OSError:
            return []
        mine = [l for l in lines if f" {self.adapter.name}: " in l]
        return mine[-n:]

    def save_chat(self) -> None:
        if self.folder is None:
            self.broadcast({"type": "error", "text": "No project folder to save the chat in."}, keep=False)
            return
        out = ["# AI chat", "", "_Unofficial. Not made by the ArtCraft team._", ""]
        for m in self.history:
            t = m.get("type")
            if t == "user":
                out += [f"**You:** {m.get('text', '')}", ""]
            elif t == "ai_done" and m.get("text"):
                out += [f"**AI:** {m.get('text', '')}", ""]
            elif t == "card":
                n = m.get("undo_count")
                steps = "1 undo step" if n == 1 else f"{n} undo steps"
                out += [f"> Change {m.get('n')}: {m.get('title')} ({steps}, {m.get('state')})", ""]
            elif t == "held":
                out += [f"> Held: {m.get('summary')} ({m.get('state')})", ""]
            elif t == "banner":
                out += [f"> ⚠ {m.get('text')}: {m.get('preview', '')}", ""]
        name = time.strftime("craft-ai-chat-%Y-%m-%d-%H%M%S.md")
        path = self.folder / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(hide_home("\n".join(out)))
        except OSError as e:
            self.broadcast({"type": "error", "text": f"Could not save the chat: {e}"}, keep=False)
            return
        self.broadcast({"type": "note", "text": f"Saved the chat to {path}"}, keep=False)

    def _events_loop(self) -> None:
        try:
            pos = self.events_path.stat().st_size
        except OSError:
            pos = 0
        try:
            apos = self.activity_log.stat().st_size
        except OSError:
            apos = 0
        abuf = b""
        buf = b""
        last_sel = 0.0
        while not self.stop.is_set():
            if time.time() - last_sel > 1.5 and self.clients and not self.busy:
                last_sel = time.time()
                sel = self._read_selection()
                if sel is not None and sel.get("text") != (self.selection or {}).get("text"):
                    self.selection = sel
                    self.broadcast(self.selection_msg(), keep=False)
            try:
                asize = self.activity_log.stat().st_size
                if asize < apos:
                    apos = 0
                if asize > apos:
                    with self.activity_log.open("rb") as f:
                        f.seek(apos)
                        abuf += f.read(min(asize - apos, MAX_LINE))
                        apos = f.tell()
                    *done, rest = abuf.split(b"\n")
                    abuf = rest
                    lines = [l.decode(errors="replace") for l in done if f" {self.adapter.name}: ".encode() in l]
                    if lines:
                        self.broadcast({"type": "activity", "lines": lines[-200:]}, keep=False)
            except OSError:
                pass
            try:
                size = self.events_path.stat().st_size
                if size < pos:
                    pos = 0
                if size > pos:
                    with self.events_path.open("rb") as f:
                        f.seek(pos)
                        buf += f.read(min(size - pos, MAX_LINE * 4))
                        pos = f.tell()
            except OSError:
                pass
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(ev, dict) or ev.get("app") != self.adapter.name:
                    continue
                if ev.get("type") == "banner":
                    self.broadcast({"type": "banner", "text": str(ev.get("text", "This file is talking to your AI")),
                                    "preview": str(ev.get("preview", ""))[:300]})
                elif ev.get("type") == "approval":
                    code = str(ev.get("code", ""))
                    if code and code not in self.held:
                        cmds = [c for c in ev.get("commands", []) if isinstance(c, str)][:32]
                        self.held[code] = Held(code, str(ev.get("summary", ""))[:500], str(ev.get("reason", ""))[:300],
                                               commands=cmds, chat_allowable=bool(ev.get("chat_allowable")) and bool(cmds))
                        self.broadcast(self.held[code].msg())
            time.sleep(0.25)


def _tool_summary(name: str, args: dict) -> str:
    if name == "command_run":
        return f"{args.get('id')}"
    if name == "command_batch":
        return ", ".join(str(s.get("id")) for s in args.get("steps", []) if isinstance(s, dict))[:200]
    return name


def write_mcp_config(path: Path, app: str, launcher: Path, folder: Path | None, guard: str, ask: str) -> Path:
    args = ["mcp", app, "--guard", guard, "--ask", ask, "--held-via", "panel"] + (["--project", str(folder)] if folder else [])
    cfg = {"mcpServers": {f"craft-{app}": {"command": str(launcher), "args": args}}}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f)
    return path
