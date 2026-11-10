"""Guarded proxy against the official headless MCP server (`filmcraft-cli mcp --demo`)."""
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.filmcraft import _BIN_DIR, FilmCraft  # noqa: E402
from core.proxy import serve  # noqa: E402
from guard.hook import Guard, Verdict  # noqa: E402

CLI = _BIN_DIR / "filmcraft-cli"


class Out(io.StringIO):
    def lines(self):
        return [json.loads(x) for x in self.getvalue().splitlines() if x.strip()]


class BlockingGuard(Guard):
    def __init__(self, adapter, log_path):
        super().__init__(adapter, log_path=log_path, state_dir=log_path.parent / "_state_test")
        self.seen_after = []

    def before(self, tool, args):
        if tool == "command_run" and args.get("id") == "file.newBlackVideo":
            return Verdict(False, "test block")
        return super().before(tool, args)

    def after(self, tool, args, result):
        self.seen_after.append(tool)
        return super().after(tool, args, result)


def rpc(i, method, params=None):
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})


@unittest.skipUnless(CLI.exists(), "filmcraft-cli not built")
class ProxyTests(unittest.TestCase):
    def test_forward_block_and_after_hook(self, tmp=Path(__file__).parent / "_activity_test.log"):
        lines = [
            rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            rpc(2, "tools/list"),
            rpc(3, "tools/call", {"name": "command_run", "arguments": {"id": "history.list", "params": {}}}),
            rpc(4, "tools/call", {"name": "command_run", "arguments": {"id": "file.newBlackVideo", "params": {"seconds": 3}}}),
            rpc(5, "tools/call", {"name": "command_run", "arguments": {"id": "history.list", "params": {}}}),
            "not json",
        ]
        guard = BlockingGuard(FilmCraft(), tmp)
        out = Out()
        try:
            serve(FilmCraft(), guard, inp=io.StringIO("\n".join(lines) + "\n"), out=out,
                  child_argv=[str(CLI), "mcp", "--demo"])
            by_id = {m.get("id"): m for m in out.lines()}
            self.assertEqual(by_id[1]["result"]["serverInfo"]["name"], "filmcraft")
            self.assertGreater(len(by_id[2]["result"]["tools"]), 5)
            self.assertTrue(by_id[4]["result"]["isError"])
            self.assertIn("Blocked by Craft AI Guard", by_id[4]["result"]["content"][0]["text"])
            # the blocked call never reached the app: the undo stack is still empty
            self.assertEqual(json.loads(by_id[5]["result"]["content"][0]["text"])["undo"], [])
            self.assertEqual(guard.seen_after, ["command_run", "command_run"])
            self.assertEqual(by_id[None]["error"]["code"], -32700)
            self.assertIn("ran history.list", tmp.read_text())
        finally:
            tmp.unlink(missing_ok=True)
            import shutil
            shutil.rmtree(tmp.parent / "_state_test", ignore_errors=True)


class EntryPointTests(unittest.TestCase):
    """The pieces `craft-agent mcp` / `panel` import at start-up. Their absence once crashed every start."""

    def test_cli_entry_points_import_and_parse(self):
        import subprocess
        from core import proxy
        for name in ("ensure_app", "fetch_catalog", "serve", "load_session", "hide_home"):
            self.assertTrue(callable(getattr(proxy, name)), name)
        launcher = str(ROOT / "craft-agent")
        for args in (["mcp", "--help"], ["panel", "--help"], ["chat", "--help"], ["approve", "--help"]):
            r = subprocess.run([sys.executable, launcher, *args], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{args}: {r.stderr[-300:]}")


class EnsureAppTests(unittest.TestCase):
    def test_concurrent_starts_launch_one_app_and_failures_clean_up(self):
        import tempfile
        import threading
        from unittest import mock

        from core import proxy

        class FakeProc:
            def __init__(self):
                self.pid, self.terminated = 4242, False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

        class FakeApp:
            name = "fakeapp"
            launches, token = 0, None

            def __init__(self, answers):
                self.answers, self.proc = answers, FakeProc()

            def launch(self, token, folder, panel_token=""):
                FakeApp.launches += 1
                FakeApp.token = token
                return self.proc

            def wait_ready(self, timeout, token=""):
                return self.answers and token == FakeApp.token

        with tempfile.TemporaryDirectory() as d, mock.patch.object(proxy, "SESSION_DIR", Path(d)):
            app = FakeApp(True)
            results = []
            threads = [threading.Thread(target=lambda: results.append(proxy.ensure_app(app))) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual(FakeApp.launches, 1, "six clients at once must launch one app")
            self.assertEqual(len({r["control_token"] for r in results}), 1)
            (Path(d) / "fakeapp.session.json").unlink()
            bad = FakeApp(False)
            with self.assertRaises(RuntimeError):
                proxy.ensure_app(bad, timeout=0.1)
            self.assertTrue(bad.proc.terminated, "an app that never answers is stopped, not left behind")


if __name__ == "__main__":
    unittest.main()
