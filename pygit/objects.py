"""Git's object database: blobs, trees, and commits.

Every object is stored zlib-compressed in a file named after its SHA-1, which is
computed over "<kind> <size>\\0" followed by the content. This is exactly Git's
loose-object format, so real Git can read what this module writes (and the reverse).
"""

from __future__ import annotations

import hashlib
import os
import re
import zlib
from dataclasses import dataclass
from pathlib import Path

from .errors import PygitError

BLOB = "blob"
TREE = "tree"
COMMIT = "commit"

MODE_FILE = "100644"
MODE_EXEC = "100755"
MODE_TREE = "40000"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def object_id(kind: str, data: bytes) -> str:
    """The SHA-1 that Git gives an object of this kind and content."""
    header = f"{kind} {len(data)}".encode() + b"\0"
    return hashlib.sha1(header + data).hexdigest()


class ObjectStore:
    """Reads and writes loose objects in a directory such as .pygit/objects."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, sha: str) -> Path:
        # The first two hex digits name a sub-folder, which keeps folders small.
        return self.directory / sha[:2] / sha[2:]

    def exists(self, sha: str) -> bool:
        return self._path(sha).is_file()

    def write(self, kind: str, data: bytes) -> str:
        """Store an object and return its SHA-1. Writing the same content twice does nothing."""
        sha = object_id(kind, data)
        path = self._path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = zlib.compress(f"{kind} {len(data)}".encode() + b"\0" + data)
            # Write to a temporary file first so a crash never leaves a half-written object.
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(payload)
            os.replace(temporary, path)
        return sha

    def read(self, sha: str) -> tuple[str, bytes]:
        """Return (kind, content) for an object."""
        path = self._path(sha)
        if not path.is_file():
            raise PygitError(f"object {sha} not found")
        raw = zlib.decompress(path.read_bytes())
        header, _, data = raw.partition(b"\0")
        kind, _, size = header.decode().partition(" ")
        if not size.isdigit() or int(size) != len(data):
            raise PygitError(f"object {sha} is corrupt")
        return kind, data

    def find_by_prefix(self, prefix: str) -> list[str]:
        """Full SHA-1s that start with an abbreviation (at least four characters)."""
        if len(prefix) < 4:
            return []
        folder = self.directory / prefix[:2]
        if not folder.is_dir():
            return []
        return sorted(
            prefix[:2] + name
            for name in os.listdir(folder)
            if name.startswith(prefix[2:]) and not name.endswith(".tmp")
        )


# --- trees ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    name: str
    sha: str

    @property
    def is_tree(self) -> bool:
        return self.mode == MODE_TREE


def encode_tree(entries: list[TreeEntry]) -> bytes:
    """Serialize a folder: "<mode> <name>\\0" and the 20 raw bytes of the SHA-1, per entry."""

    def sort_key(entry: TreeEntry) -> bytes:
        # Git orders entries by name, comparing a sub-folder as if its name ended with "/".
        return (entry.name + "/" if entry.is_tree else entry.name).encode()

    return b"".join(
        f"{entry.mode} {entry.name}".encode() + b"\0" + bytes.fromhex(entry.sha)
        for entry in sorted(entries, key=sort_key)
    )


def decode_tree(data: bytes) -> list[TreeEntry]:
    entries = []
    position = 0
    while position < len(data):
        end = data.index(b"\0", position)
        mode, _, name = data[position:end].decode().partition(" ")
        entries.append(TreeEntry(mode, name, data[end + 1 : end + 21].hex()))
        position = end + 21
    return entries


FileMap = dict[str, tuple[str, str]]  # path -> (mode, sha), with "/" separators


def flatten_tree(store: ObjectStore, tree_sha: str, prefix: str = "") -> FileMap:
    """Every file below a tree, keyed by its full path."""
    kind, data = store.read(tree_sha)
    if kind != TREE:
        raise PygitError(f"{tree_sha} is a {kind}, not a tree")
    files: FileMap = {}
    for entry in decode_tree(data):
        path = prefix + entry.name
        if entry.is_tree:
            files.update(flatten_tree(store, entry.sha, path + "/"))
        else:
            files[path] = (entry.mode, entry.sha)
    return files


def write_tree(store: ObjectStore, files: FileMap) -> str:
    """Turn a flat list of files into nested tree objects and return the root tree's SHA-1."""
    nested: dict = {}
    for path, entry in files.items():
        *folders, name = path.split("/")
        node = nested
        for folder in folders:
            node = node.setdefault(folder, {})
        node[name] = entry
    return _write_nested(store, nested)


def _write_nested(store: ObjectStore, node: dict) -> str:
    entries = []
    for name, value in node.items():
        if isinstance(value, dict):
            entries.append(TreeEntry(MODE_TREE, name, _write_nested(store, value)))
        else:
            mode, sha = value
            entries.append(TreeEntry(mode, name, sha))
    return store.write(TREE, encode_tree(entries))


# --- commits -------------------------------------------------------------------------------

IDENT = re.compile(r"^(?P<name>.*) <(?P<email>.*)> (?P<time>\d+) (?P<zone>[+-]\d{4})$")


@dataclass
class Ident:
    name: str
    email: str
    timestamp: int
    zone: str


def parse_ident(text: str) -> Ident:
    """Read "Name <email> 1700000000 +0100", the format of author and committer lines."""
    match = IDENT.match(text)
    if not match:
        raise PygitError(f"malformed identity line: {text!r}")
    return Ident(match["name"], match["email"], int(match["time"]), match["zone"])


@dataclass
class Commit:
    tree: str
    parents: list[str]
    author: str
    committer: str
    message: str

    @property
    def subject(self) -> str:
        return self.message.strip().split("\n", 1)[0]


def encode_commit(commit: Commit) -> bytes:
    lines = [f"tree {commit.tree}"]
    lines += [f"parent {parent}" for parent in commit.parents]
    lines += [f"author {commit.author}", f"committer {commit.committer}"]
    return ("\n".join(lines) + "\n\n" + commit.message.rstrip("\n") + "\n").encode()


def decode_commit(data: bytes) -> Commit:
    headers, _, message = data.decode(errors="replace").partition("\n\n")
    tree = ""
    parents: list[str] = []
    author = committer = ""
    for line in headers.split("\n"):
        key, _, value = line.partition(" ")
        if key == "tree":
            tree = value
        elif key == "parent":
            parents.append(value)
        elif key == "author":
            author = value
        elif key == "committer":
            committer = value
        # Other headers (such as signatures) are ignored.
    if not tree:
        raise PygitError("commit has no tree")
    return Commit(tree, parents, author, committer, message)
