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

DEFAULT_CONFIG = {"core": {"repositoryformatversion": "0", "filemode": "false", "bare": "false"}}


def _write_text(path: Path, text: str) -> None:
    # Always "\n" line endings: Git reads these files, and Windows would otherwise write "\r\n".
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


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

    def read_ref(self, ref: str) -> str | None:
        path = self.git_dir / ref
        return path.read_text(encoding="utf-8").strip() if path.is_file() else None

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
        names = [path.relative_to(heads).as_posix() for path in heads.rglob("*") if path.is_file()]
        return sorted(names)

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
        sha = self.read_ref(f"refs/heads/{name}")
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
        section, _, name = key.lower().rpartition(".")
        if not section or not name:
            raise PygitError(f"invalid key '{key}': use section.name, for example user.name")
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
        return name, email

    # --- the working folder --------------------------------------------------------------

    def is_ignored(self, relative: str) -> bool:
        """Whether a path matches a pattern in .pygitignore (simple wildcard patterns)."""
        ignore_file = self.worktree / ".pygitignore"
        if not ignore_file.is_file():
            return False
        parts = relative.split("/")
        for raw in ignore_file.read_text(encoding="utf-8").splitlines():
            pattern = raw.strip().rstrip("/")
            if not pattern or pattern.startswith("#"):
                continue
            if fnmatch.fnmatch(relative, pattern) or any(
                fnmatch.fnmatch(part, pattern) for part in parts
            ):
                return True
        return False

    def worktree_files(self, under: str = "") -> list[str]:
        """Paths of the files in the working folder (or below one folder), minus ignored ones."""
        root = self.worktree / under if under else self.worktree
        found: list[str] = []
        for folder, dirs, files in os.walk(root):
            relative_folder = Path(folder).relative_to(self.worktree).as_posix()
            prefix = "" if relative_folder == "." else relative_folder + "/"
            dirs[:] = sorted(d for d in dirs if d != GIT_DIR and not self.is_ignored(prefix + d))
            for name in sorted(files):
                path = prefix + name
                if not self.is_ignored(path) and not (Path(folder) / name).is_symlink():
                    found.append(path)
        return found
