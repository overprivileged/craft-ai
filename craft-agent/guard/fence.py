"""Folder fence: every path a command touches must resolve (symlinks, `..`, `~`) inside the project folder."""
from __future__ import annotations

import os
from pathlib import Path


def resolve(p: str, base: Path | None) -> Path:
    raw = os.path.expanduser(os.path.expandvars(p))
    if not os.path.isabs(raw) and base is not None:
        raw = os.path.join(str(base), raw)
    # realpath resolves symlinks for every existing prefix and collapses `..` for the rest
    return Path(os.path.realpath(raw))


def inside(p: str, folder: Path | None) -> bool:
    if folder is None:
        return False
    root = Path(os.path.realpath(folder))
    target = resolve(p, root)
    try:
        return os.path.commonpath([str(root), str(target)]) == str(root)
    except ValueError:  # different drives
        return False


def pretty(p: str) -> str:
    """Path for logs/UI without the user's home directory spelled out."""
    home = str(Path.home())
    full = os.path.expanduser(p)
    return "~" + full[len(home):] if full == home or full.startswith(home + os.sep) else p
