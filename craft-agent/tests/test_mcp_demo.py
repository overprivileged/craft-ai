"""Integration: the FilmCraft adapter against the official headless MCP server (`filmcraft-cli mcp --demo`)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from adapters.filmcraft import _BIN_DIR, FilmCraft  # noqa: E402
from core.mcp import MCPClient  # noqa: E402

TICKS = 254_016_000_000
CLI = _BIN_DIR / "filmcraft-cli"


@unittest.skipUnless(CLI.exists(), "filmcraft-cli not built")
class DemoMCP(unittest.TestCase):
    def setUp(self):
        self.mcp = MCPClient([str(CLI), "mcp", "--demo"])
        self.mcp.initialize()
        self.fc = FilmCraft()

    def tearDown(self):
        self.mcp.close()

    def run_cmd(self, cid, **params):
        res = self.mcp.call_tool("command_run", {"id": cid, "params": params})
        self.assertFalse(res["is_error"], res["text"])
        return res["json"]

    def test_instructions_render_with_real_catalog(self):
        from core.agent import _command_index
        catalog = _command_index(self.mcp.call_tool("command_list", {})["json"])
        text = self.fc.instructions(catalog)
        self.assertIn("timeline.place {", text)
        self.assertIn("end of Video 1", text)
        self.assertGreaterEqual(text.count("\n"), 40)
        for banned in ("inject", "hidden", "ignore", "untrusted"):
            self.assertNotIn(banned, text.lower())  # keep the Guard OFF test honest

    def test_tools_listed(self):
        names = {t["name"] for t in self.mcp.list_tools()}
        self.assertTrue({"command_list", "command_run", "command_batch", "media_import"} <= names)

    def test_black_clip_place_and_undo(self):
        depth0 = self.fc.history_depth(self.mcp)
        self.assertIsNotNone(depth0)
        item = self.run_cmd("file.newBlackVideo", seconds=3.0)
        item_id = item["item"]
        seq = self.mcp.call_tool("sequence_inspect", {})["json"]
        end = max((c.get("end", 0) for t in seq.get("video", []) for c in t.get("items", [])), default=0)
        self.run_cmd("timeline.place", item=item_id, track="V1", time=end, insert=False)
        seq = self.mcp.call_tool("sequence_inspect", {})["json"]
        black = [c for t in seq.get("video", []) for c in t.get("items", []) if c.get("start") == end]
        self.assertTrue(black, "no clip at the end of the sequence")
        # snapped to the sequence frame grid (29.97 fps -> 90 frames = 3.003 s)
        self.assertAlmostEqual(black[0]["end"] - black[0]["start"], 3 * TICKS, delta=TICKS // 24)
        depth1 = self.fc.history_depth(self.mcp)
        self.assertGreaterEqual(depth1 - depth0, 1)
        for _ in range(depth1 - depth0):
            self.run_cmd("edit.undo")
        self.assertEqual(self.fc.history_depth(self.mcp), depth0)


if __name__ == "__main__":
    unittest.main()
