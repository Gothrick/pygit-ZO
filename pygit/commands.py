"""The commands. Each takes a Repository and returns the text to print, or raises PygitError."""

from __future__ import annotations

import contextlib
import heapq
import os
import re
import sys
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
from .repository import Repository, branch_name_problem, is_internal

DATE_OVERRIDE = re.compile(r"(\d{1,11})(?: ([+-]\d{4}))?")
LATEST_TIMESTAMP = 32503680000  # the year 3000: later dates make real Git reject the commit

# Windows and macOS file systems treat names that differ only by letter case as the same file.
CASE_INSENSITIVE_FILESYSTEM = os.name == "nt" or sys.platform == "darwin"


def _file_mode(path: Path) -> str:
    # Windows has no executable bit, so everything there is a regular file.
    if os.name != "nt" and path.stat().st_mode & 0o111:
        return MODE_EXEC
    return MODE_FILE


def _short(sha: str) -> str:
    return sha[:7]


# --- init, config, and the plumbing commands ---------------------------------------------------


def init(path: Path) -> str:
    if path.is_file():
        raise PygitError(f"'{path}' is a file, not a folder")
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
    if not Path(path).is_file():
        what = "is a folder, not a file" if Path(path).is_dir() else "no such file"
        raise PygitError(f"'{path.name}': {what}")
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
        if relative and is_internal(relative):
            raise PygitError(
                f"'{argument}' is repository data (.pygit or .git) and cannot be added"
            )

        if absolute.is_dir():
            prefix = relative + "/" if relative else ""
            tracked_here = [p for p in index.files if p.startswith(prefix)]
            if relative and repo.is_ignored(relative, True) and not tracked_here:
                raise PygitError(f"'{argument}' is ignored by .pygitignore")
            present = set(repo.worktree_files(relative))
            for tracked in tracked_here:
                if (repo.worktree / tracked).is_file():
                    present.add(tracked)  # a tracked file stays tracked even if it is now ignored
                else:
                    del index.files[tracked]  # deleted from the working folder since it was staged
            to_stage = sorted(present)
        elif absolute.is_file():
            if repo.is_ignored(relative) and relative not in index.files:
                raise PygitError(f"'{argument}' is ignored by .pygitignore")
            to_stage = [relative]
        elif relative in index.files:
            del index.files[relative]
            continue
        else:
            raise PygitError(f"'{argument}' did not match any files")

        for path in to_stage:
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

    # A tracked file is judged by what is on disk, even if .pygitignore now matches it.
    for path, (_, sha) in sorted(staged.items()):
        file = repo.worktree / path
        if not file.is_file():
            status.deleted.append(path)
        elif object_id(BLOB, file.read_bytes()) != sha:
            status.modified.append(path)
    # Untracked files are the ones found in the working folder, minus ignored ones.
    status.untracked = sorted(set(repo.worktree_files()) - set(staged))
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
        match = DATE_OVERRIDE.fullmatch(fixed.strip())
        if not match:
            raise PygitError("PYGIT_DATE must look like '1700000000 +0100'")
        seconds, zone = int(match[1]), match[2] or "+0000"
        if seconds > LATEST_TIMESTAMP or int(zone[1:3]) > 23 or int(zone[3:5]) > 59:
            raise PygitError("PYGIT_DATE is out of range")
        return seconds, zone
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
    if max_count is not None and max_count < 0:
        raise PygitError("-n must be zero or more")
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
        repo.delete_branch(name)
        return f"Deleted branch {name}"

    sha = _new_branch(repo, name, start)
    return f"Created branch {name} at {_short(sha)}"


def _new_branch(repo: Repository, name: str, start: str | None) -> str:
    problem = branch_name_problem(name)
    if problem:
        raise PygitError(f"'{name}' is not a valid branch name: {problem}")
    clash = repo.branch_conflict(name)
    if clash == name:
        raise PygitError(f"a branch named '{name}' already exists")
    if clash:
        raise PygitError(f"cannot create '{name}': it conflicts with the existing branch '{clash}'")
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


def _remove_empty_folders(folder: Path) -> None:
    """Remove a folder, and the folders inside it, as long as no file is inside."""
    for current, _dirs, _names in os.walk(folder, topdown=False):
        with contextlib.suppress(OSError):
            Path(current).rmdir()  # fails (and is skipped) when something is still inside


def _tracked_keys(old_files: FileMap) -> set[str]:
    """Tracked paths, compared the way the file system does (ignoring case where it does)."""
    return {p.casefold() for p in old_files} if CASE_INSENSITIVE_FILESYSTEM else set(old_files)


def _is_tracked(path: str, tracked: set[str]) -> bool:
    return (path.casefold() if CASE_INSENSITIVE_FILESYSTEM else path) in tracked


def _in_the_way(repo: Repository, path: str, tracked: set[str]) -> bool:
    """Whether something on disk at this path would be lost by writing a file there."""
    target = repo.worktree / path
    if target.is_symlink() or target.is_file():
        return True
    if target.is_dir():
        # A folder holding only tracked files, or nothing at all, is fine: tracked files are
        # removed first and empty folders are cleared away before the new file is written.
        for folder, _dirs, names in os.walk(target):
            for name in names:
                found = (Path(folder) / name).relative_to(repo.worktree).as_posix()
                if not _is_tracked(found, tracked):
                    return True
    return False


def _checkout_conflicts(repo: Repository, old_files: FileMap, new_files: FileMap) -> list[str]:
    """Paths the checkout would write over something that is not a clean tracked file.

    Anything already on disk at a new path counts: an untracked file, an ignored file, or a
    folder holding such files. So does a plain file where the new path needs a folder.
    """
    tracked = _tracked_keys(old_files)
    conflicts: set[str] = set()
    for path, entry in new_files.items():
        if old_files.get(path) == entry:
            continue  # the same tracked file is already there
        if not _is_tracked(path, tracked) and _in_the_way(repo, path, tracked):
            conflicts.add(path)
        parts = path.split("/")
        for length in range(1, len(parts)):
            parent = "/".join(parts[:length])
            if not _is_tracked(parent, tracked) and (repo.worktree / parent).is_file():
                conflicts.add(parent)
    return sorted(conflicts)


def _orphan_warning(repo: Repository, commit: str | None) -> str:
    """A warning when leaving a detached HEAD whose commit no branch can reach."""
    if commit is None:
        return ""
    for name in repo.branches():
        tip = repo.read_branch(name)
        if tip and any(sha == commit for sha, _ in _history(repo, tip)):
            return ""
    return (
        f"\nWarning: commit {_short(commit)} is not on any branch and can now only be reached "
        f"by its id.\nTo keep it: pygit branch <name> {_short(commit)}"
    )


def checkout(repo: Repository, target: str, create: bool) -> str:
    """Switch to a branch (or a commit, which detaches HEAD) and update the working folder."""
    current = repo.current_branch()
    if create:
        _new_branch(repo, target, None)
        repo.checkout_branch_head(target)
        return f"Switched to a new branch '{target}'"

    if target == "HEAD":  # nothing to switch to: stay where we are
        head = repo.head_commit()
        if head is None:
            raise PygitError("HEAD does not point to a commit yet")
        return f"Already on '{current}'" if current else f"HEAD is already at {_short(head)}"

    branch_sha = repo.read_branch(target)
    sha = branch_sha or repo.resolve(target)
    to_branch = target if branch_sha else None
    if to_branch and to_branch == current:
        return f"Already on '{to_branch}'"

    leaving = repo.head_commit() if current is None else None
    st = compute_status(repo)
    if st.has_tracked_changes:
        raise PygitError(
            "you have uncommitted changes that checkout would overwrite:\n"
            "commit them first (or restore the files), then try again"
        )

    old_files = repo.commit_files(repo.head_commit())
    new_files = repo.commit_files(sha)
    # Check every conflict before touching the disk, so a refusal never leaves a half-done switch.
    blocking = _checkout_conflicts(repo, old_files, new_files)
    if blocking:
        raise PygitError(
            "untracked or ignored files would be overwritten by checkout: "
            + ", ".join(blocking)
            + "\nmove or delete them first, then try again"
        )

    try:
        for path in sorted(set(old_files) - set(new_files)):
            _remove_file(repo, path)
        for path, (mode, blob) in sorted(new_files.items()):
            if old_files.get(path) == (mode, blob) and (repo.worktree / path).is_file():
                continue
            file = repo.worktree / path
            if file.is_dir():
                _remove_empty_folders(file)  # folders with no files where a file now goes
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(repo.objects.read(blob)[1])
            if os.name != "nt":
                file.chmod(0o755 if mode == MODE_EXEC else 0o644)
    except OSError as error:
        raise PygitError(
            f"checkout stopped while updating the working folder ({error}); "
            "run 'pygit status' to see what changed"
        ) from error

    index = repo.index
    index.files = dict(new_files)
    index.save()

    if to_branch:
        warning = _orphan_warning(repo, leaving) if leaving != sha else ""
        repo.checkout_branch_head(to_branch)
        return f"Switched to branch '{to_branch}'{warning}"
    warning = _orphan_warning(repo, leaving) if leaving != sha else ""
    repo.detach_head(sha)
    return f"HEAD is now at {_short(sha)} {repo.read_commit(sha).subject}{warning}"
