"""The command line: parses arguments and calls the commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, commands
from .errors import PygitError
from .repository import Repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pygit", description="A small Git implementation in Python."
    )
    parser.add_argument("--version", action="version", version=f"pygit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    p = sub.add_parser("init", help="create an empty repository")
    p.add_argument("path", nargs="?", default=".")

    p = sub.add_parser("config", help="read or set a setting such as user.name")
    p.add_argument("key")
    p.add_argument("value", nargs="?")

    p = sub.add_parser("hash-object", help="compute (and optionally store) a file's object id")
    p.add_argument("-w", dest="write", action="store_true", help="store the object")
    p.add_argument("file")

    p = sub.add_parser("cat-file", help="show an object")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-p", dest="mode", action="store_const", const="-p", help="pretty-print the content"
    )
    group.add_argument("-t", dest="mode", action="store_const", const="-t", help="show the type")
    group.add_argument("-s", dest="mode", action="store_const", const="-s", help="show the size")
    p.add_argument("object")

    sub.add_parser("ls-files", help="list the files in the index")

    p = sub.add_parser("ls-tree", help="list the files in a commit")
    p.add_argument("-r", dest="recursive", action="store_true", help="recurse into folders")
    p.add_argument("revision", nargs="?", default="HEAD")

    p = sub.add_parser("add", help="stage files")
    p.add_argument("paths", nargs="+")

    sub.add_parser("status", help="show what is staged, modified, or untracked")

    p = sub.add_parser("diff", help="show changes as a unified diff")
    p.add_argument("--staged", action="store_true", help="compare the index with the last commit")

    p = sub.add_parser("commit", help="record the staged changes")
    p.add_argument("-m", "--message", required=True)

    p = sub.add_parser("log", help="show the commit history")
    p.add_argument("--oneline", action="store_true")
    p.add_argument("-n", dest="max_count", type=int, help="limit the number of commits")
    p.add_argument("revision", nargs="?", default="HEAD")

    p = sub.add_parser("branch", help="list, create, or delete branches")
    p.add_argument("-d", dest="delete", action="store_true", help="delete the branch")
    p.add_argument("name", nargs="?")
    p.add_argument("start", nargs="?", help="where the new branch starts (default HEAD)")

    p = sub.add_parser("checkout", help="switch branches or commits")
    p.add_argument("-b", dest="create", action="store_true", help="create the branch first")
    p.add_argument("target")

    return parser


def run(args: argparse.Namespace) -> str | bytes:
    cwd = Path.cwd()
    if args.command == "init":
        return commands.init((cwd / args.path).resolve())
    if args.command == "hash-object":
        repo = Repository.find(cwd) if args.write else None
        return commands.hash_object(repo, cwd / args.file, args.write)

    repo = Repository.find(cwd)
    match args.command:
        case "config":
            return commands.config(repo, args.key, args.value)
        case "cat-file":
            return commands.cat_file(repo, args.object, args.mode)
        case "ls-files":
            return commands.ls_files(repo)
        case "ls-tree":
            return commands.ls_tree(repo, args.revision, args.recursive)
        case "add":
            return commands.add(repo, [Path(p) for p in args.paths], cwd)
        case "status":
            return commands.status(repo)
        case "diff":
            return commands.diff(repo, args.staged)
        case "commit":
            return commands.commit(repo, args.message)
        case "log":
            return commands.log(repo, args.revision, args.oneline, args.max_count)
        case "branch":
            return commands.branch(repo, args.name, args.start, args.delete)
        case "checkout":
            return commands.checkout(repo, args.target, args.create)
    raise PygitError(f"unknown command '{args.command}'")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        output = run(args)
    except PygitError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    if isinstance(output, bytes):
        sys.stdout.flush()
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
    elif output:
        print(output)
    return 0
