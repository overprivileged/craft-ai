"""The Adapter interface. Core and Guard only ever talk to an app through this."""
from __future__ import annotations

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path


class Adapter(ABC):
    #: short app name used on the command line
    name: str = ""
    #: env var holding the app's control token (for attaching to an already running app)
    token_env: str = ""
    #: built-in recipes {slug: recipe dict (+ optional "support" note)}
    builtin_recipes: dict = {}
    #: command ids that undo / redo one edit step
    undo_step: str = "edit.undo"
    redo_step: str = "edit.redo"
    #: ids of common edit commands shown to the model up front (filtered to what the app really has)
    curated_commands: tuple[str, ...] = ()

    @abstractmethod
    def launch(self, token: str, folder: Path | None, panel_token: str = "") -> subprocess.Popen | None:
        """Start the app with its control channel (token) and the AI panel token (a different secret)."""

    @abstractmethod
    def wait_ready(self, timeout: float, token: str = "") -> bool:
        """Block until the app answers an authenticated request on its control channel."""

    @abstractmethod
    def mcp_command(self) -> list[str]:
        """argv for the app's official MCP server (stdio)."""

    def mcp_env(self, token: str) -> dict[str, str]:
        """Extra environment for the MCP server process."""
        return {}

    @abstractmethod
    def risky(self, cmd: str, params: dict) -> bool:
        """True when the command can write files, delete media or reach outside the project."""

    def actions(self, tool: str, args: dict) -> list[tuple[str, dict]]:
        """The (command id, params) pairs a tool call performs, for the Guard. Read-only tools return
        their own name so path params (e.g. a screenshot path) are still fenced."""
        return [(tool, args)]

    def allowed(self, cmd: str) -> bool:
        """In the allow-list (runs without asking in ask mode)."""
        return False

    def set_catalog(self, ids) -> None:
        """The app's real command ids, when known (lets `allowed` treat unknown commands as unknown)."""

    def blocked(self, tool: str, args: dict) -> str | None:
        """A reason if this call must never run, whatever the user allows (else None)."""
        return None

    def chat_allowable(self, cmd: str) -> bool:
        """May the user allow this command for the rest of a chat (vs. once)?"""
        return True

    def untrusted_fields(self, tool: str, args: dict, data: object) -> list[tuple[tuple, str]]:
        """(json path, string) for every string in a tool result that came from files/users, not the app."""
        return []

    def instructions(self, catalog: dict[str, dict]) -> str:
        """MCP `instructions` the proxy gives any client: a short editing guide + curated commands."""
        return ""

    def paths_in(self, cmd: str, params: dict) -> list[str]:
        """Every filesystem path a command would touch."""
        return []

    def selection(self, mcp, scan=None) -> dict | None:
        """What the user has selected in the app right now: {"text": short label for the panel chip,
        "context": one line added to a request, "count": n}. Names come from files: pass the Guard's
        `scan` so flagged names are left out. None if unknown."""
        return None

    def history_labels(self, mcp) -> list[str] | None:
        """The app's undo stack as names of the steps, oldest first (None if unknown)."""
        return None

    def history_depth(self, mcp) -> int | None:
        """Size of the app's undo stack (None if unknown). Used to count undo steps per change card."""
        labels = self.history_labels(mcp)
        return None if labels is None else len(labels)

    def is_edit(self, tool: str, args: dict) -> list[str]:
        """Command ids in this tool call that change the document (one undo step each)."""
        return []
