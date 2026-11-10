"""Core tests with a fake adapter + fake MCP + scripted provider: no app, no network."""
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.adapter import Adapter  # noqa: E402
from core.agent import Agent  # noqa: E402


class FakeMCP:
    def __init__(self):
        self.history = []
        self.calls = []

    def list_tools(self):
        return [{"name": "command_list", "inputSchema": {"type": "object"}},
                {"name": "command_run", "inputSchema": {"type": "object"}}]

    def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "command_list":
            return {"is_error": False, "text": "", "images": [],
                    "json": [{"id": "doc.addBox", "label": "Add Box", "params": "{}"}]}
        cid = args.get("id")
        if cid == "history":
            return {"is_error": False, "text": "", "images": [], "json": {"undo": list(self.history)}}
        if cid == "undo":
            self.history.pop()
        else:
            self.history.append(cid)
        return {"is_error": False, "text": "ok", "images": [], "json": {}}


class FakeAdapter(Adapter):
    name = "fakeapp"
    undo_step = "undo"
    curated_commands = ("doc.addBox", "doc.missing")

    def launch(self, token, folder, panel_token=""):
        return None

    def wait_ready(self, timeout, token=""):
        return True

    def mcp_command(self):
        return ["true"]

    def risky(self, cmd, params):
        return False

    def history_depth(self, mcp):
        return len(mcp.call_tool("command_run", {"id": "history"})["json"]["undo"])

    def is_edit(self, tool, args):
        return [args["id"]] if tool == "command_run" else []


class ScriptedProvider:
    label = "scripted"

    def __init__(self, responses):
        self.responses = list(responses)
        self.seen = []

    def chat(self, system, messages, tools):
        self.seen.append((system, [m["role"] for m in messages]))
        return self.responses.pop(0)


def tool_use(i, cid):
    return {"type": "tool_use", "id": f"t{i}", "name": "command_run", "input": {"id": cid, "params": {}}}


class CoreTests(unittest.TestCase):
    def test_turn_makes_card_and_undo_reverts(self):
        mcp = FakeMCP()
        prov = ScriptedProvider([
            {"stop_reason": "tool_use", "content": [tool_use(1, "doc.addBox"), tool_use(2, "doc.addBox")]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Added two boxes."}]},
        ])
        agent = Agent(FakeAdapter(), mcp, prov)
        self.assertEqual(agent.curated_found, 1)  # doc.missing is filtered out
        self.assertIn("fakeapp", agent.system)
        res = agent.turn("add two boxes")
        self.assertEqual(res.reply, "Added two boxes.")
        self.assertEqual(res.card.undo_count, 2)
        self.assertEqual(res.card.steps, ["doc.addBox", "doc.addBox"])
        # tool results for parallel calls go back in ONE user message
        self.assertEqual(prov.seen[1][1], ["user", "assistant", "user"])
        self.assertEqual(agent.undo(res.card), [])
        self.assertEqual(mcp.history, [])

    def test_no_card_for_read_only_turn(self):
        prov = ScriptedProvider([
            {"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": "t1", "name": "command_run",
                                                       "input": {"id": "history", "params": {}}}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "looked"}]},
        ])
        self.assertIsNone(Agent(FakeAdapter(), FakeMCP(), prov).turn("look").card)

    def test_no_card_for_plain_chat(self):
        prov = ScriptedProvider([{"stop_reason": "end_turn", "content": [{"type": "text", "text": "hi"}]}])
        agent = Agent(FakeAdapter(), FakeMCP(), prov)
        self.assertIsNone(agent.turn("hello").card)

    def test_core_and_guard_never_name_an_app(self):
        pat = re.compile(r"filmcraft|vectorcraft|lightcraft", re.I)
        for d in ("core", "guard"):
            for f in (ROOT / d).glob("*.py"):
                text = f.read_text()
                if f.name == "cli.py":  # the adapter registry lives here
                    text = re.sub(r"ADAPTERS = \{.*?\}\n", "", text, flags=re.S)
                    text = text.replace('default="filmcraft"', "")
                self.assertIsNone(pat.search(text), f"{f.name} mentions an app by name")


if __name__ == "__main__":
    unittest.main()
