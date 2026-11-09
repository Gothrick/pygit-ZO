"""A repository: the working folder plus its .pygit directory (objects, refs, HEAD, config)."""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from .errors import PygitError
from .index import Index
from .objects import COMMIT, Commit, FileMap, ObjectStore, decode_commit, flatten_tree

GIT_DIR = ".pygit"
DEFAULT_BRANCH = "main"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
LOOKS_LIKE_SHA = re.compile(r"^[0-9a-f]{4,40}$")

DEFAULT_CONFIG = {"core": {"repositoryformatversion": "0", "filemode": "false", "bare": "false"}}

# Folders that belong to version control itself and must never be added as project files.
INTERNAL_NAMES = {GIT_DIR, ".git"}

# Characters Git forbids in branch names (plus "<>|" and quotes, which Windows forbids in paths).
FORBIDDEN_IN_BRANCH = set(' ~^:?*[\\"<>|')
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10))} | {
    f"lpt{n}" for n in range(1, 10)
}


def _write_text(path: Path, text: str) -> None:
    # Always "\n" line endings: Git reads these files, and Windows would otherwise write "\r\n".
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def valid_branch_name(name: str) -> bool:
    """Git's rules for reference names, plus the names Windows cannot store as files."""
    if (
        not name
        or name in ("HEAD", "@")
        or name.startswith(("-", "/"))
        or name.endswith(("/", "."))
    ):
        return False
    if ".." in name or "@{" in name or "//" in name:
        return False
    if any(ch in FORBIDDEN_IN_BRANCH or ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    if LOOKS_LIKE_SHA.match(name):
        return False  # it would be mistaken for an abbreviated commit id
    for part in name.split("/"):
        if part.startswith(".") or part.endswith(".lock"):
            return False
        if part.split(".")[0].lower() in WINDOWS_RESERVED:
            return False
    return True


def is_internal(relative: str) -> bool:
    """Whether a path is inside a .pygit or .git folder."""
    return any(part.lower() in INTERNAL_NAMES for part in relative.split("/"))


def _check_identity(value: str, what: str) -> None:
    # "<", ">" and line breaks would corrupt the author line of a commit.
    if not value.strip() or re.search(r"[<>\r\n]", value):
        raise PygitError(f"invalid {what}: it must not be empty or contain '<', '>' or line breaks")


class Repository:
    def __init__(self, worktree: Path) -> None:
        self.worktree = Path(worktree).resolve()
        self.git_dir = self.worktree / GIT_DIR
        self.objects = ObjectStore(self.git_dir / "objects")

    # --- creating and finding ------------------------------------------------------------

    @classmethod
    def init(cls, path: Path) -> tuple[Repository, bool]:
        """Create the repository layout. Returns (repository, already_existed)."""
        repo = cls(path)
        existed = repo.git_dir.is_dir()
        (repo.git_dir / "objects").mkdir(parents=True, exist_ok=True)
        (repo.git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        if not repo.head_path.exists():
            _write_text(repo.head_path, f"ref: refs/heads/{DEFAULT_BRANCH}\n")
        if not repo.config_path.exists():
            repo._write_config(DEFAULT_CONFIG)
        return repo, existed

    @classmethod
    def find(cls, start: Path) -> Repository:
        """The repository that contains a folder, looking in each parent folder in turn."""
        current = Path(start).resolve()
        for folder in (current, *current.parents):
            if (folder / GIT_DIR).is_dir():
                return cls(folder)
        raise PygitError("not a pygit repository (or any parent folder): run 'pygit init' first")

    @property
    def index(self) -> Index:
        # Not named "index": that name is where real Git keeps its own, different, binary index.
        return Index(self.git_dir / "index.json")

    # --- HEAD and references -------------------------------------------------------------

    @property
    def head_path(self) -> Path:
        return self.git_dir / "HEAD"

    def current_branch(self) -> str | None:
        """The checked-out branch name, or None when HEAD points straight at a commit."""
        content = self.head_path.read_text(encoding="utf-8").strip()
        prefix = "ref: refs/heads/"
        return content[len(prefix) :] if content.startswith(prefix) else None

    def head_commit(self) -> str | None:
        content = self.head_path.read_text(encoding="utf-8").strip()
        if content.startswith("ref: "):
            return self.read_ref(content[len("ref: ") :])
        return content or None

    def _packed_refs(self) -> dict[str, str]:
        """References that `git gc` moved into .pygit/packed-refs (read-only support)."""
        path = self.git_dir / "packed-refs"
        refs: dict[str, str] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line and line[0] not in "#^":
                    sha, _, name = line.partition(" ")
                    refs[name] = sha
        return refs

    def read_ref(self, ref: str) -> str | None:
        path = self.git_dir / ref
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
        return self._packed_refs().get(ref)

    def read_branch(self, name: str) -> str | None:
        """The commit a branch points to, or None (also for names that cannot be branches)."""
        return self.read_ref(f"refs/heads/{name}") if valid_branch_name(name) else None

    def write_ref(self, ref: str, sha: str) -> None:
        _write_text(self.git_dir / ref, sha + "\n")

    def update_head(self, sha: str) -> None:
        """Move the current branch (or a detached HEAD) to a commit."""
        branch = self.current_branch()
        if branch:
            self.write_ref(f"refs/heads/{branch}", sha)
        else:
            _write_text(self.head_path, sha + "\n")

    def checkout_branch_head(self, branch: str) -> None:
        _write_text(self.head_path, f"ref: refs/heads/{branch}\n")

    def detach_head(self, sha: str) -> None:
        _write_text(self.head_path, sha + "\n")

    def branches(self) -> list[str]:
        heads = self.git_dir / "refs" / "heads"
        names = {path.relative_to(heads).as_posix() for path in heads.rglob("*") if path.is_file()}
        prefix = "refs/heads/"
        names |= {ref[len(prefix) :] for ref in self._packed_refs() if ref.startswith(prefix)}
        return sorted(names)

    def branch_conflict(self, name: str) -> str | None:
        """An existing branch that cannot coexist with this name ("a" and "a/b" are exclusive)."""
        for existing in self.branches():
            if (
                existing == name
                or name.startswith(existing + "/")
                or existing.startswith(name + "/")
            ):
                return existing
        return None

    def delete_branch(self, name: str) -> None:
        heads = self.git_dir / "refs" / "heads"
        path = heads / name
        if not valid_branch_name(name):
            raise PygitError(f"branch '{name}' not found")
        if not path.is_file():
            if name in self.branches():
                raise PygitError(
                    f"branch '{name}' is stored in packed-refs, which pygit cannot edit"
                )
            raise PygitError(f"branch '{name}' not found")
        path.unlink()
        # Like Git, remove the folders a branch like "feature/x" leaves empty.
        folder = path.parent
        while folder != heads and not any(folder.iterdir()):
            folder.rmdir()
            folder = folder.parent

    # --- revisions -----------------------------------------------------------------------

    def resolve(self, revision: str) -> str:
        """Turn "HEAD", a branch name, a SHA-1 (or its start), or "<rev>~N" into a full SHA-1."""
        name, tilde, count = revision.partition("~")
        if tilde and count and not count.isdigit():
            raise PygitError(f"unknown revision '{revision}'")
        steps = (int(count) if count else 1) if tilde else 0

        sha = self._resolve_name(name)
        for _ in range(steps):
            parents = self.read_commit(sha).parents
            if not parents:
                raise PygitError(f"'{revision}': the commit has no such ancestor")
            sha = parents[0]
        return sha

    def _resolve_name(self, name: str) -> str:
        if name == "HEAD":
            sha = self.head_commit()
            if sha is None:
                raise PygitError("HEAD does not point to a commit yet")
            return sha
        sha = self.read_branch(name)
        if sha:
            return sha
        if FULL_SHA.match(name) and self.objects.exists(name):
            return name
        matches = self.objects.find_by_prefix(name)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise PygitError(f"'{name}' is ambiguous: several objects start with it")
        raise PygitError(f"unknown revision '{name}'")

    def read_commit(self, sha: str) -> Commit:
        kind, data = self.objects.read(sha)
        if kind != COMMIT:
            raise PygitError(f"{sha} is a {kind}, not a commit")
        return decode_commit(data)

    def commit_files(self, sha: str | None) -> FileMap:
        """The files in a commit, or nothing for "no commit yet"."""
        return {} if sha is None else flatten_tree(self.objects, self.read_commit(sha).tree)

    # --- configuration -------------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.git_dir / "config"

    def _read_config(self) -> dict[str, dict[str, str]]:
        sections: dict[str, dict[str, str]] = {}
        section = ""
        if not self.config_path.is_file():
            return sections
        for raw in self.config_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip().lower()
                sections.setdefault(section, {})
            elif "=" in line and section:
                key, _, value = line.partition("=")
                sections[section][key.strip().lower()] = value.strip()
        return sections

    def _write_config(self, sections: dict[str, dict[str, str]]) -> None:
        # The standard Git config layout, so real Git accepts this folder as a repository.
        text = ""
        for section, values in sections.items():
            text += f"[{section}]\n"
            text += "".join(f"\t{key} = {value}\n" for key, value in values.items())
        _write_text(self.config_path, text)

    def get_config(self, key: str) -> str | None:
        section, _, name = key.lower().rpartition(".")
        return self._read_config().get(section, {}).get(name)

    def set_config(self, key: str, value: str) -> None:
        section, _, name = key.lower().partition(".")
        if not section or not name or "." in name:
            raise PygitError(f"invalid key '{key}': use section.name, for example user.name")
        if key.lower() in ("user.name", "user.email"):
            _check_identity(value, key)
        sections = self._read_config()
        sections.setdefault(section, {})[name] = value
        self._write_config(sections)

    def author(self) -> tuple[str, str]:
        """Who is committing: environment variables first, then the repository config."""
        name = os.environ.get("PYGIT_AUTHOR_NAME") or self.get_config("user.name")
        email = os.environ.get("PYGIT_AUTHOR_EMAIL") or self.get_config("user.email")
        if not name or not email:
            raise PygitError(
                "tell pygit who you are first:\n"
                '  pygit config user.name "Your Name"\n'
                "  pygit config user.email you@example.com"
            )
        _check_identity(name, "author name")
        _check_identity(email, "author email")
        return name.strip(), email.strip()

    # --- the working folder --------------------------------------------------------------

    def _ignore_patterns(self) -> list[tuple[str, bool]]:
        """The (pattern, anchored) pairs of .pygitignore; a leading "/" anchors to the top."""
        ignore_file = self.worktree / ".pygitignore"
        if not ignore_file.is_file():
            return []
        patterns = []
        for raw in ignore_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                anchored = line.startswith("/")
                patterns.append((line.strip("/") if anchored else line.rstrip("/"), anchored))
        return patterns

    @staticmethod
    def _matches(patterns: list[tuple[str, bool]], relative: str) -> bool:
        parts = relative.split("/")
        prefixes = ["/".join(parts[:end]) for end in range(1, len(parts) + 1)]
        for pattern, anchored in patterns:
            if anchored:
                if any(fnmatch.fnmatchcase(prefix, pattern) for prefix in prefixes):
                    return True
            elif fnmatch.fnmatchcase(relative, pattern) or any(
                fnmatch.fnmatchcase(part, pattern) for part in parts
            ):
                return True
        return False

    def is_ignored(self, relative: str) -> bool:
        """Whether a path matches a pattern in .pygitignore (simple wildcards, no negation)."""
        return self._matches(self._ignore_patterns(), relative)

    def worktree_files(self, under: str = "") -> list[str]:
        """Paths of the files in the working folder (or below one folder), minus ignored ones."""
        patterns = self._ignore_patterns()  # read once, not once per file
        root = self.worktree / under if under else self.worktree
        found: list[str] = []
        for folder, dirs, files in os.walk(root):
            relative_folder = Path(folder).relative_to(self.worktree).as_posix()
            prefix = "" if relative_folder == "." else relative_folder + "/"
            dirs[:] = sorted(
                d
                for d in dirs
                if d.lower() not in INTERNAL_NAMES and not self._matches(patterns, prefix + d)
            )
            for name in sorted(files):
                path = prefix + name
                if (
                    name.lower() not in INTERNAL_NAMES
                    and not self._matches(patterns, path)
                    and not (Path(folder) / name).is_symlink()
                ):
                    found.append(path)
        return found
