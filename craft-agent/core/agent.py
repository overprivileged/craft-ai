"""App-agnostic chat + tool loop. Knows nothing about any specific app: everything goes through Adapter."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from .adapter import Adapter
from .mcp import MCPClient

MAX_TOOL_ROUNDS = 25
MAX_RESULT_CHARS = 20000

SYSTEM_TEMPLATE = """You edit a project inside {app} for the user, by calling its official tools.
- Inspect first (doc_inspect / sequence_inspect) to get real ids; never invent ids.
- Prefer command_run / command_batch with the commands below. Use command_list(filter=...) to find others.
- Time params accept "seconds". Keep edits minimal and do exactly what was asked.
- After editing, reply in one or two short sentences saying what changed.

Common commands (id - label - params):
{commands}"""


@dataclass
class ChangeCard:
    title: str
    steps: list[str] = field(default_factory=list)
    undo_count: int = 0
    undone: bool = False

    def to_json(self) -> dict:
        return {"title": self.title, "steps": self.steps, "undo_count": self.undo_count}


@dataclass
class TurnResult:
    reply: str
    card: ChangeCard | None
    tool_calls: list[tuple[str, dict, bool]]  # (tool, args, is_error)


def _command_index(raw: object) -> dict[str, dict]:
    items = raw.get("commands", raw) if isinstance(raw, dict) else raw
    out: dict[str, dict] = {}
    if isinstance(items, list):
        for c in items:
            if isinstance(c, dict) and isinstance(c.get("id"), str):
                out[c["id"]] = c
    return out


class Agent:
    def __init__(self, adapter: Adapter, mcp: MCPClient, provider, on_event: Callable[[str, dict], None] | None = None,
                 system: str | None = None, tool_names: set[str] | None = None):
        self.adapter = adapter
        self.mcp = mcp
        self.provider = provider
        self.on_event = on_event or (lambda kind, data: None)
        self.messages: list[dict] = []
        self.cards: list[ChangeCard] = []
        tools = mcp.list_tools()
        if tool_names:
            tools = [t for t in tools if t["name"] in tool_names]
        self.tools = self._tools_for_model(tools)
        if system:  # e.g. the MCP server's instructions, so every client gets the same guide
            self.system, self.curated_found = system, system.count("\n")
        else:
            self.system = self._system_prompt()

    @staticmethod
    def _tools_for_model(mcp_tools: list[dict]) -> list[dict]:
        out = []
        for t in mcp_tools:
            schema = t.get("inputSchema") or {"type": "object", "properties": {}}
            out.append({
                "name": t["name"],
                "description": (t.get("description") or t.get("title") or t["name"])[:1000],
                "input_schema": schema,
            })
        return out

    def _system_prompt(self) -> str:
        lines = []
        try:
            res = self.mcp.call_tool("command_list", {})
            index = _command_index(res["json"])
        except Exception:
            index = {}
        for cid in self.adapter.curated_commands:
            c = index.get(cid)
            if c is None:
                continue
            lines.append(f"{cid} - {c.get('label', '')} - {c.get('params', '{}')}")
        self.curated_found = len(lines)
        return SYSTEM_TEMPLATE.format(app=self.adapter.name, commands="\n".join(lines) or "(use command_list)")

    def _tool_result_content(self, res: dict) -> list[dict] | str:
        text = res["text"]
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n…[truncated {len(res['text']) - MAX_RESULT_CHARS} chars]"
        if not res["images"]:
            return text or "(ok)"
        blocks: list[dict] = [{"type": "text", "text": text or "(image)"}]
        for img in res["images"][:2]:
            blocks.append({"type": "image", "source": {
                "type": "base64", "media_type": img.get("mimeType", "image/png"), "data": img.get("data", "")}})
        return blocks

    def turn(self, user_text: str) -> TurnResult:
        self.messages.append({"role": "user", "content": user_text})
        depth_before = self._depth()
        edits: list[str] = []
        calls: list[tuple[str, dict, bool]] = []
        reply = ""
        for _ in range(MAX_TOOL_ROUNDS):
            resp = self.provider.chat(self.system, self.messages, self.tools)
            content = resp.get("content", [])
            # append the full content (thinking blocks included) so the next request stays valid
            self.messages.append({"role": "assistant", "content": content})
            stop = resp.get("stop_reason")
            texts = [b.get("text", "") for b in content if b.get("type") == "text"]
            if texts:
                reply = "\n".join(t for t in texts if t)
            if stop == "refusal":
                reply = reply or "The model declined this request."
                break
            uses = [b for b in content if b.get("type") == "tool_use"]
            if stop != "tool_use" or not uses:
                break
            results = []
            for u in uses:
                name, args = u["name"], u.get("input") or {}
                self.on_event("tool", {"name": name, "args": args})
                try:
                    res = self.mcp.call_tool(name, args)
                except Exception as e:  # keep the loop alive; report to the model
                    res = {"is_error": True, "text": f"tool failed: {e}", "json": None, "images": []}
                calls.append((name, args, res["is_error"]))
                if not res["is_error"]:
                    edits.extend(self.adapter.is_edit(name, args))
                self.on_event("tool_result", {"name": name, "is_error": res["is_error"], "text": res["text"][:300]})
                results.append({
                    "type": "tool_result", "tool_use_id": u["id"],
                    "content": self._tool_result_content(res), "is_error": res["is_error"],
                })
            self.messages.append({"role": "user", "content": results})
        else:
            reply = reply or "(stopped: too many tool rounds)"

        card = None
        depth_after = self._depth()
        depth_known = depth_before is not None and depth_after is not None
        undo_count = max(0, depth_after - depth_before) if depth_known else len(edits)
        # a card is something the user can undo: reads, blocked calls and failed calls don't make one
        if undo_count or (edits and not depth_known):
            card = ChangeCard(title=user_text.strip()[:80], steps=edits, undo_count=undo_count)
            self.cards.append(card)
        return TurnResult(reply=reply, card=card, tool_calls=calls)

    def _depth(self) -> int | None:
        try:
            return self.adapter.history_depth(self.mcp)
        except Exception:
            return None

    def undo(self, card: ChangeCard) -> list[str]:
        """Undo a card. Only safe for the most recent card (undo is a stack)."""
        errors = []
        for _ in range(card.undo_count):
            res = self.mcp.call_tool("command_run", {"id": self.adapter.undo_step, "params": {}})
            if res["is_error"]:
                errors.append(res["text"])
        card.undone = not errors
        return errors


def dumps(obj: object) -> str:
    return json.dumps(obj, ensure_ascii=False)
