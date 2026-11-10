"""Hidden-instruction scan. Deterministic, no AI: text from files/users that addresses an AI or asks for
file/network actions is flagged so the Guard can wrap it as untrusted data before the model sees it."""
from __future__ import annotations

import re
from dataclasses import dataclass

LONG_LINE = 500
HUGE_LINE = 8000  # longer than this is suspicious even as prose

_RULES: list[tuple[str, re.Pattern]] = [
    ("ignore-instructions", re.compile(
        r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any|your)\b.{0,30}"
        r"\b(instructions?|prompts?|rules?|messages?|guidelines?)\b", re.I | re.S)),
    ("role-switch", re.compile(r"\byou are now\b|\bact as (an?|the) (ai|assistant|agent)\b|\bnew instructions?\b", re.I)),
    ("speaker-tag", re.compile(r"(^|\n|\]|>)\s*(assistant|system|ai|claude|gpt|chatgpt|llm|agent|model)\s*:", re.I)),
    ("send-upload", re.compile(r"\b(send|upload|post|email|e-mail|exfiltrate|transmit|forward)\b.{0,60}\b(to|at|via)\b", re.I | re.S)),
    ("read-home", re.compile(r"\b(read|open|cat|list|import|load)\b.{0,20}(~/|/Users/|/home/|%USERPROFILE%|C:\\Users)", re.I)),
    ("file-action-outside", re.compile(
        r"\b(export|save|write|copy|move|render|import|delete|remove)\b.{0,60}(~/|/Users/|/home/|\.\./|%USERPROFILE%|C:\\Users)",
        re.I | re.S)),
    ("url", re.compile(r"\bhttps?://|\bwww\.[a-z0-9-]+\.[a-z]{2,}", re.I)),
    ("base64-blob", re.compile(r"[A-Za-z0-9+/]{48,}={0,2}")),
    ("hidden-unicode", re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")),
]


@dataclass
class Hit:
    rule: str
    text: str

    def preview(self, n: int = 80) -> str:
        visible = re.sub("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]", "·", self.text)
        visible = " ".join(visible.split())
        return visible if len(visible) <= n else visible[: n - 1] + "…"


def _blobby(line: str) -> bool:
    """A long line that is not ordinary prose. Speech transcripts and captions are long paragraphs of
    plain words and must not trip the Guard; a blob (few spaces) or an enormous line still does."""
    n = len(line)
    if n <= LONG_LINE:
        return False
    return n > HUGE_LINE or line.count(" ") * 40 < n


def scan_text(text: str) -> list[str]:
    """Names of the rules this text trips (empty = clean)."""
    if not isinstance(text, str) or not text:
        return []
    found = [name for name, pat in _RULES if pat.search(text)]
    if any(_blobby(line) for line in text.splitlines()):
        found.append("very-long-line")
    return found
