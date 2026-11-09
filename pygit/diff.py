"""Unified diffs between two versions of a file."""

from __future__ import annotations

import difflib
import re

NO_NEWLINE = "\\ No newline at end of file\n"


def _lines(data: bytes | None) -> list[str]:
    if not data:
        return []
    # Split on "\n" only, keeping the line ending (str.splitlines also splits on other characters).
    return re.findall(r"[^\n]*\n|[^\n]+", data.decode("utf-8", errors="replace"))


def file_diff(path: str, old: bytes | None, new: bytes | None) -> str:
    """A diff for one file. None means the file does not exist on that side."""
    header = f"diff --pygit a/{path} b/{path}\n"
    if old is None:
        header += "new file\n"
    elif new is None:
        header += "deleted file\n"

    if b"\0" in (old or b"") or b"\0" in (new or b""):
        return header + "Binary files differ\n"

    changes = difflib.unified_diff(
        _lines(old),
        _lines(new),
        fromfile="/dev/null" if old is None else f"a/{path}",
        tofile="/dev/null" if new is None else f"b/{path}",
    )
    body = "".join(line if line.endswith("\n") else line + "\n" + NO_NEWLINE for line in changes)
    return header + body
