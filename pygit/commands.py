"""The commands. Each takes a Repository and returns the text to print, or raises PygitError."""

from __future__ import annotations

import heapq
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .diff import file_diff
from .errors import PygitError
from .objects import (
    BLOB,
    COMMIT,
    MODE_EXEC,
    MODE_FILE,
    TREE,
    Commit,
    FileMap,
    TreeEntry,
    decode_tree,
    encode_commit,
    flatten_tree,
    object_id,
    parse_ident,
    write_tree,
)
from .repository import Repository

BRANCH_NAME_RULES = re.compile(
    r"^(?!-)(?!.*\.\.)(?!.*//)(?!/)(?!.*/$)(?!.*\.lock$)[^\s~^:?*\[\\]+$"
)


def _file_mode(path: Path) -> str:
    # Windows has no executable bit, so everything there is a regular file.
    if os.name != "nt" and path.stat().st_mode & 0o111:
        return MODE_EXEC
    return MODE_FILE


def _short(sha: str) -> str:
    return sha[:7]


# --- init, config, and the plumbing commands ---------------------------------------------------


def init(path: Path) -> str:
    repo, existed = Repository.init(path)
    verb = "Reinitialized existing" if existed else "Initialized empty"
    return f"{verb} pygit repository in {repo.git_dir}"


def config(repo: Repository, key: str, value: str | None) -> str:
    if value is None:
        found = repo.get_config(key)
        if found is None:
            raise PygitError(f"'{key}' is not set")
        return found
    repo.set_config(key, value)
    return ""


def hash_object(repo: Repository | None, path: Path, write: bool) -> str:
    data = Path(path).read_bytes()
    if write:
        if repo is None:
            raise PygitError("-w needs a repository")
        return repo.objects.write(BLOB, data)
    return object_id(BLOB, data)


def cat_file(repo: Repository, revision: str, mode: str) -> str | bytes:
    """Show an object: -t for its type, -s for its size, -p for its content."""
    kind, data = repo.objects.read(repo.resolve(revision))
    if mode == "-t":
        return kind
    if mode == "-s":
        return str(len(data))
    if kind == TREE:
        return "\n".join(_describe(entry) for entry in decode_tree(data))
    return data


def _describe(entry: TreeEntry) -> str:
    """One line of a tree listing, in the format of `git ls-tree`."""
    kind = "tree" if entry.is_tree else "blob"
    return f"{entry.mode.rjust(6, '0')} {kind} {entry.sha}\t{entry.name}"


def ls_files(repo: Repository) -> str:
    return "\n".join(
        f"{mode} {sha} {path}" for path, (mode, sha) in sorted(repo.index.files.items())
    )


def ls_tree(repo: Repository, revision: str, recursive: bool) -> str:
    tree = repo.read_commit(repo.resolve(revision)).tree
    if recursive:
        files = flatten_tree(repo.objects, tree)
        return "\n".join(
            f"{mode.rjust(6, '0')} blob {sha}\t{path}"
            for path, (mode, sha) in sorted(files.items())
        )
    _, data = repo.objects.read(tree)
    return "\n".join(_describe(entry) for entry in decode_tree(data))


# --- add -----------------------------------------------------------------------------------


def _drop_conflicts(files: FileMap, path: str) -> None:
    """Remove index entries that cannot coexist with a path: a file cannot also be a folder."""
    parts = path.split("/")
    for length in range(1, len(parts)):
        files.pop("/".join(parts[:length]), None)  # a file where the new path needs a folder
    prefix = path + "/"
    for existing in [name for name in files if name.startswith(prefix)]:
        del files[existing]  # a folder where the new path is a file


def add(repo: Repository, paths: list[Path], cwd: Path) -> str:
    """Stage files. A folder stages everything below it, and a deleted file is staged as deleted."""
    index = repo.index
    for argument in paths:
        absolute = (cwd / argument).resolve()
        try:
            relative = absolute.relative_to(repo.worktree).as_posix()
        except ValueError:
            raise PygitError(f"'{argument}' is outside the repository") from None
        relative = "" if relative == "." else relative

        if absolute.is_dir():
            present = repo.worktree_files(relative)
            prefix = relative + "/" if relative else ""
            for tracked in [p for p in index.files if p.startswith(prefix) and p not in present]:
                del index.files[tracked]  # deleted from the working folder since it was staged
        elif absolute.is_file():
            if repo.is_ignored(relative):
                raise PygitError(f"'{argument}' is ignored by .pygitignore")
            present = [relative]
        elif relative in index.files:
            del index.files[relative]
            continue
        else:
            raise PygitError(f"'{argument}' did not match any files")

        for path in present:
            full = repo.worktree / path
            sha = repo.objects.write(BLOB, full.read_bytes())
            _drop_conflicts(index.files, path)
            index.files[path] = (_file_mode(full), sha)
    index.save()
    return ""


# --- status ----------------------------------------------------------------------------------


@dataclass
class Status:
    staged_new: list[str] = field(default_factory=list)
    staged_modified: list[str] = field(default_factory=list)
    staged_deleted: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)

    @property
    def has_tracked_changes(self) -> bool:
        return bool(
            self.staged_new
            or self.staged_modified
            or self.staged_deleted
            or self.modified
            or self.deleted
        )


def compute_status(repo: Repository) -> Status:
    """Compare three snapshots: the last commit (HEAD), the index, and the working folder."""
    head = repo.commit_files(repo.head_commit())
    staged = repo.index.files
    status = Status()

    for path in sorted(set(head) | set(staged)):
        if path not in head:
            status.staged_new.append(path)
        elif path not in staged:
            status.staged_deleted.append(path)
        elif head[path] != staged[path]:
            status.staged_modified.append(path)

    present = set(repo.worktree_files())
    for path, (_, sha) in sorted(staged.items()):
        if path not in present:
            status.deleted.append(path)
        elif object_id(BLOB, (repo.worktree / path).read_bytes()) != sha:
            status.modified.append(path)
    status.untracked = sorted(present - set(staged))
    return status


def status(repo: Repository) -> str:
    st = compute_status(repo)
    branch = repo.current_branch()
    head = repo.head_commit()
    lines = [f"On branch {branch}" if branch else f"HEAD detached at {_short(head or '')}"]
    if head is None:
        lines += ["", "No commits yet"]

    def section(title: str, rows: list[tuple[str, list[str]]]) -> None:
        if any(paths for _, paths in rows):
            lines.extend(["", title])
            for label, paths in rows:
                lines.extend(f"\t{label}{path}" for path in paths)

    section(
        "Changes to be committed:",
        [
            ("new file:   ", st.staged_new),
            ("modified:   ", st.staged_modified),
            ("deleted:    ", st.staged_deleted),
        ],
    )
    section(
        "Changes not staged for commit:",
        [("modified:   ", st.modified), ("deleted:    ", st.deleted)],
    )
    section("Untracked files:", [("", st.untracked)])
    if not st.has_tracked_changes and not st.untracked:
        lines += ["", "nothing to commit, working tree clean"]
    return "\n".join(lines)


# --- diff ------------------------------------------------------------------------------------


def diff(repo: Repository, staged: bool) -> str:
    """Working folder versus index, or (with staged) index versus the last commit."""

    def stored(entry: tuple[str, str] | None) -> bytes | None:
        return None if entry is None else repo.objects.read(entry[1])[1]

    if staged:
        old_files = repo.commit_files(repo.head_commit())
        new_files = repo.index.files
        paths = sorted(set(old_files) | set(new_files))

        def new_version(path: str) -> bytes | None:
            return stored(new_files.get(path))

    else:
        old_files = repo.index.files
        paths = sorted(old_files)  # only tracked files: untracked ones do not appear in a diff

        def new_version(path: str) -> bytes | None:
            file = repo.worktree / path
            return file.read_bytes() if file.is_file() else None

    parts = []
    for path in paths:
        old, new = stored(old_files.get(path)), new_version(path)
        if old != new:
            parts.append(file_diff(path, old, new))
    return "".join(parts).rstrip("\n")


# --- commit and log --------------------------------------------------------------------------


def _now() -> tuple[int, str]:
    """(Unix time, "+HHMM" zone). PYGIT_DATE="1700000000 +0100" fixes it, which tests rely on."""
    fixed = os.environ.get("PYGIT_DATE")
    if fixed:
        seconds, _, zone = fixed.partition(" ")
        return int(seconds), zone or "+0000"
    local = datetime.now().astimezone()
    offset = int(local.utcoffset().total_seconds() // 60) if local.utcoffset() else 0
    sign = "+" if offset >= 0 else "-"
    return int(time.time()), f"{sign}{abs(offset) // 60:02d}{abs(offset) % 60:02d}"


def commit(repo: Repository, message: str) -> str:
    message = message.strip()
    if not message:
        raise PygitError("the commit message is empty")
    files = repo.index.files
    parent = repo.head_commit()
    parent_files = repo.commit_files(parent)
    if files == parent_files:
        raise PygitError("nothing to commit (use 'pygit add' to stage changes)")

    name, email = repo.author()
    seconds, zone = _now()
    ident = f"{name} <{email}> {seconds} {zone}"
    tree = write_tree(repo.objects, files)
    new_commit = Commit(tree, [parent] if parent else [], ident, ident, message)
    sha = repo.objects.write(COMMIT, encode_commit(new_commit))
    repo.update_head(sha)

    changed = len(
        {p for p in set(files) | set(parent_files) if files.get(p) != parent_files.get(p)}
    )
    where = repo.current_branch() or "detached HEAD"
    root = "(root-commit) " if parent is None else ""
    return f"[{where} {root}{_short(sha)}] {new_commit.subject}\n {changed} file(s) changed"


def _format_date(ident_text: str) -> str:
    ident = parse_ident(ident_text)
    sign = 1 if ident.zone[0] == "+" else -1
    offset = timedelta(hours=int(ident.zone[1:3]), minutes=int(ident.zone[3:5])) * sign
    moment = datetime.fromtimestamp(ident.timestamp, timezone(offset))
    return f"{moment:%a %b} {moment.day} {moment:%H:%M:%S %Y} {ident.zone}"


def _history(repo: Repository, start: str) -> list[tuple[str, Commit]]:
    """Commits reachable from start, newest first (by author time)."""
    queue: list[tuple[int, int, str]] = []
    seen = {start}
    counter = 0
    commits = {start: repo.read_commit(start)}

    def push(sha: str) -> None:
        nonlocal counter
        counter += 1
        time_of = parse_ident(commits[sha].author).timestamp
        heapq.heappush(queue, (-time_of, counter, sha))

    push(start)
    ordered = []
    while queue:
        _, _, sha = heapq.heappop(queue)
        ordered.append((sha, commits[sha]))
        for parent in commits[sha].parents:
            if parent not in seen:
                seen.add(parent)
                commits[parent] = repo.read_commit(parent)
                push(parent)
    return ordered


def log(repo: Repository, revision: str, oneline: bool, max_count: int | None) -> str:
    entries = _history(repo, repo.resolve(revision))
    if max_count is not None:
        entries = entries[:max_count]
    blocks = []
    for sha, entry in entries:
        if oneline:
            blocks.append(f"{_short(sha)} {entry.subject}")
            continue
        author = parse_ident(entry.author)
        body = "\n".join(f"    {line}" for line in entry.message.strip().split("\n"))
        blocks.append(
            f"commit {sha}\nAuthor: {author.name} <{author.email}>\n"
            f"Date:   {_format_date(entry.author)}\n\n{body}"
        )
    return ("\n" if oneline else "\n\n").join(blocks)


# --- branches and checkout -----------------------------------------------------------------------


def branch(repo: Repository, name: str | None, start: str | None, delete: bool) -> str:
    if name is None:
        current = repo.current_branch()
        lines = [f"{'*' if b == current else ' '} {b}" for b in repo.branches()]
        if current is None and repo.head_commit():
            lines.insert(0, f"* (HEAD detached at {_short(repo.head_commit() or '')})")
        return "\n".join(lines)

    if delete:
        if name == repo.current_branch():
            raise PygitError(f"cannot delete the branch '{name}' you are on")
        path = repo.git_dir / "refs" / "heads" / name
        if not path.is_file():
            raise PygitError(f"branch '{name}' not found")
        path.unlink()
        return f"Deleted branch {name}"

    sha = _new_branch(repo, name, start)
    return f"Created branch {name} at {_short(sha)}"


def _new_branch(repo: Repository, name: str, start: str | None) -> str:
    if not BRANCH_NAME_RULES.match(name):
        raise PygitError(f"'{name}' is not a valid branch name")
    if repo.read_ref(f"refs/heads/{name}"):
        raise PygitError(f"a branch named '{name}' already exists")
    sha = repo.resolve(start or "HEAD") if (start or repo.head_commit()) else None
    if sha is None:
        raise PygitError("cannot create a branch before the first commit")
    repo.write_ref(f"refs/heads/{name}", sha)
    return sha


def _remove_file(repo: Repository, path: str) -> None:
    file = repo.worktree / path
    if file.is_file() or file.is_symlink():
        file.unlink()
    # Remove folders left empty, but never the repository's root.
    folder = file.parent
    while folder != repo.worktree and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def checkout(repo: Repository, target: str, create: bool) -> str:
    """Switch to a branch (or a commit, which detaches HEAD) and update the working folder."""
    current = repo.current_branch()
    if create:
        _new_branch(repo, target, None)
        repo.checkout_branch_head(target)
        return f"Switched to a new branch '{target}'"

    branch_sha = repo.read_ref(f"refs/heads/{target}")
    sha = branch_sha or repo.resolve(target)
    to_branch = target if branch_sha else None

    st = compute_status(repo)
    if st.has_tracked_changes:
        raise PygitError(
            "you have uncommitted changes that checkout would overwrite:\n"
            "commit them first (or restore the files), then try again"
        )

    old_files = repo.commit_files(repo.head_commit())
    new_files = repo.commit_files(sha)
    blocking = [path for path in new_files if path in st.untracked]
    if blocking:
        raise PygitError(
            "untracked files would be overwritten by checkout: " + ", ".join(sorted(blocking))
        )

    for path in sorted(set(old_files) - set(new_files)):
        _remove_file(repo, path)
    for path, (mode, blob) in sorted(new_files.items()):
        if old_files.get(path) == (mode, blob) and (repo.worktree / path).is_file():
            continue
        file = repo.worktree / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(repo.objects.read(blob)[1])
        if os.name != "nt":
            file.chmod(0o755 if mode == MODE_EXEC else 0o644)

    index = repo.index
    index.files = dict(new_files)
    index.save()

    if to_branch:
        repo.checkout_branch_head(to_branch)
        return (
            f"Switched to branch '{to_branch}'"
            if to_branch != current
            else f"Already on '{to_branch}'"
        )
    repo.detach_head(sha)
    return f"HEAD is now at {_short(sha)} {repo.read_commit(sha).subject}"
