"""The index, also called the staging area: what the next commit will contain.

Real Git stores the index in a binary format. To keep this project readable, pygit
keeps it as a small JSON file mapping each path to its file mode and blob SHA-1.
Everything else (objects, refs, HEAD) uses Git's real formats.
"""

from __future__ import annotations

import json
from pathlib import Path

from .objects import FileMap


class Index:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.files: FileMap = {}
        if path.is_file():
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.files = {name: (entry["mode"], entry["sha"]) for name, entry in stored.items()}

    def save(self) -> None:
        stored = {
            name: {"mode": mode, "sha": sha} for name, (mode, sha) in sorted(self.files.items())
        }
        # Write bytes so the file keeps "\n" line endings on Windows too.
        self.path.write_bytes((json.dumps(stored, indent=2) + "\n").encode("utf-8"))
