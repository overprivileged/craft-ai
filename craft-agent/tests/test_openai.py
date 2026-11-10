"""OpenAI-compatible provider against a local fake /chat/completions server (no network, no model)."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.agent import Agent  # noqa: E402
from core.providers import OpenAICompatProvider, ProviderError  # noqa: E402
from tests.test_core import FakeAdapter, FakeMCP  # noqa: E402


class FakeServer:
    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append(body)
                data = json.dumps(outer.replies.pop(0)).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()


def reply(content=None, calls=()):
    msg = {"role": "assistant", "content": content}
    if calls:
        msg["tool_calls"] = [{"id": f"c{i}", "type": "function", "function": {"name": n, "arguments": a}}
                             for i, (n, a) in enumerate(calls)]
    return {"choices": [{"message": msg, "finish_reason": "tool_calls" if calls else "stop"}]}


class OpenAITests(unittest.TestCase):
    def test_agent_turn_roundtrip(self):
        srv = FakeServer([
            reply("<think>plan…</think>", [("command_run", json.dumps({"id": "doc.addBox", "params": {}}))]),
            reply("<think>ok</think>Added a box."),
        ])
        try:
            prov = OpenAICompatProvider(srv.url, "qwen3:4b")
            agent = Agent(FakeAdapter(), FakeMCP(), prov, system="GUIDE")
            res = agent.turn("add a box")
            self.assertEqual(res.reply, "Added a box.")
            self.assertEqual(res.card.undo_count, 1)
            first, second = srv.requests
            self.assertEqual(first["messages"][0], {"role": "system", "content": "GUIDE"})
            self.assertEqual(first["tools"][0]["type"], "function")
            roles = [m["role"] for m in second["messages"]]
            self.assertEqual(roles, ["system", "user", "assistant", "tool"])
            self.assertEqual(second["messages"][2]["tool_calls"][0]["function"]["name"], "command_run")
            self.assertEqual(second["messages"][3]["tool_call_id"], "c0")
        finally:
            srv.close()

    def test_think_without_open_tag_is_stripped(self):
        from core.providers import _strip_think
        self.assertEqual(_strip_think("Okay, let me think...\n</think>\nDone."), "Done.")
        self.assertEqual(_strip_think("<think>a</think>B"), "B")

    def test_bad_tool_json_is_reported_not_crashed(self):
        out = OpenAICompatProvider.from_openai(reply(None, [("command_run", "{not json")]))
        self.assertEqual(out["stop_reason"], "tool_use")
        self.assertIn("_invalid_json", out["content"][0]["input"])

    def test_fit_trims_old_results_first_and_never_drops_system(self):
        p = OpenAICompatProvider("http://x/v1", "m", ctx_tokens=2000, max_out=200)
        msgs = [{"role": "system", "content": "GUIDE"}, {"role": "user", "content": "go"},
                {"role": "tool", "tool_call_id": "a", "content": "A" * 4000},
                {"role": "tool", "tool_call_id": "b", "content": "B" * 4000}]
        out = p.fit(msgs, [])
        self.assertEqual(out[0]["content"], "GUIDE")
        self.assertIn("trimmed", out[2]["content"])
        self.assertTrue(out[3]["content"].startswith("B"))
        self.assertLessEqual(sum(len(json.dumps(m)) for m in out), (2000 - 200) * 3.5)

    def test_fit_handles_escape_heavy_json(self):
        p = OpenAICompatProvider("http://x/v1", "m", ctx_tokens=3000, max_out=200)
        heavy = json.dumps({"items": [{"name": "a\"b", "x": "\n"} for _ in range(2000)]}, indent=2)
        out = p.fit([{"role": "system", "content": "G"}, {"role": "tool", "tool_call_id": "a", "content": heavy}], [])
        self.assertLessEqual(sum(len(json.dumps(m)) for m in out), (3000 - 200) * 3.5)
        self.assertIn("…[cut:", out[1]["content"])

    def test_fit_refuses_when_tools_alone_overflow(self):
        p = OpenAICompatProvider("http://x/v1", "m", ctx_tokens=500, max_out=100)
        with self.assertRaises(ProviderError):
            p.fit([{"role": "system", "content": "x"}], [{"x": "y" * 5000}])


if __name__ == "__main__":
    unittest.main()
