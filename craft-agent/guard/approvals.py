"""Ask mode approvals. A held call gets a short code; the user approves it (panel Apply, or
`craft-agent approve CODE`) and the next identical call goes through once."""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


class Approvals:
    def __init__(self, folder: Path):
        self.dir = folder

    @staticmethod
    def code_for(tool: str, args: dict) -> str:
        canon = json.dumps({"tool": tool, "args": args}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(canon.encode()).hexdigest()[:8]

    def _path(self, code: str) -> Path:
        if not (len(code) == 8 and all(c in "0123456789abcdef" for c in code)):
            raise ValueError("bad approval code")
        return self.dir / f"{code}.json"

    def hold(self, code: str, summary: str, tool: str | None = None, args: dict | None = None) -> None:
        """Hold a call. `tool`/`args` are kept so the AI panel can run exactly this call after Allow."""
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self._path(code)
        if not p.exists():
            p.write_text(json.dumps({"summary": summary, "approved": False, "at": time.time(),
                                     "tool": tool, "args": args}))

    def call(self, code: str) -> tuple[str, dict] | None:
        """The exact held (tool, args), if it was stored and still matches its code."""
        try:
            d = json.loads(self._path(code).read_text())
        except (OSError, ValueError):
            return None
        tool, args = d.get("tool"), d.get("args")
        if not isinstance(tool, str) or not isinstance(args, dict) or self.code_for(tool, args) != code:
            return None  # tampered or old entry: never run something other than what was held
        return tool, args

    def approve(self, code: str) -> str | None:
        p = self._path(code)
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return None
        d["approved"] = True
        p.write_text(json.dumps(d))
        return d.get("summary", "")

    def deny(self, code: str) -> bool:
        """Drop a held call; the same call will be held again if retried."""
        try:
            os.remove(self._path(code))
            return True
        except OSError:
            return False

    def consume(self, code: str) -> bool:
        """True (once) if this call was approved."""
        p = self._path(code)
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            return False
        if d.get("approved"):
            try:
                os.remove(p)
            except OSError:
                pass
            return True
        return False

    def pending(self) -> list[tuple[str, str]]:
        out = []
        for p in sorted(self.dir.glob("*.json")) if self.dir.exists() else []:
            try:
                d = json.loads(p.read_text())
            except (OSError, ValueError):
                continue
            if not d.get("approved"):
                out.append((p.stem, d.get("summary", "")))
        return out
