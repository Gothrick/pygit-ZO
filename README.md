# pygit: Git, rebuilt in Python

![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![Tests](https://img.shields.io/badge/tests-91-2EA44F?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)

A small implementation of Git in Python (about 1,100 lines with comments), with no dependencies. It stores data in **Git's real object format**, so the two tools understand each other: a repository made by pygit passes `git fsck --strict`, and pygit can read the commits real Git wrote.

![pygit in a terminal](docs/screenshots/demo.jpg)

## What it does

| Command | What it does |
| --- | --- |
| `pygit init` | Create a repository in `.pygit/` |
| `pygit config user.name "Ada"` | Read or set your identity |
| `pygit add <paths>` | Stage files or whole folders (deletions too) |
| `pygit status` | Show staged, modified, and untracked files |
| `pygit diff [--staged]` | Unified diff of the working folder or the staged changes |
| `pygit commit -m "message"` | Record the staged changes |
| `pygit log [--oneline] [-n N] [rev]` | Show history, newest first |
| `pygit branch [-d] [name] [start]` | List, create, or delete branches |
| `pygit checkout [-b] <branch or commit>` | Switch branches, or detach HEAD at a commit |
| `pygit hash-object [-w] <file>` | Compute (and store) an object id |
| `pygit cat-file -p/-t/-s <object>` | Show an object's content, type, or size |
| `pygit ls-files`, `pygit ls-tree [-r] [rev]` | List the index, or the files of a commit |

Revisions can be `HEAD`, a branch name, a full or abbreviated SHA-1, or `<rev>~N` (for example `HEAD~2`). A `.pygitignore` file with simple wildcard patterns keeps files out of `add` and `status`.

## Try it

You need Python 3.10 or newer (developed and tested on 3.14).

```bash
git clone https://github.com/Gothrick/mini-git-python.git
cd mini-git-python
pip install -e .
```

Then, in any empty folder:

```bash
pygit init
pygit config user.name "Ada Lovelace"
pygit config user.email ada@example.com

echo "print('hello')" > hello.py
pygit add hello.py
pygit commit -m "First commit"
pygit log
```

You can also run it without installing: `python -m pygit <command>`.

### Check it against real Git

```bash
git --git-dir=.pygit log --oneline      # real Git reads your history
git --git-dir=.pygit fsck --strict      # and checks every object
```

The full session behind the screenshot is in [docs/screenshots/full-session.jpg](docs/screenshots/full-session.jpg).

## How it is built

```text
pygit/
  objects.py      Blobs, trees, commits: hashing, compression, and the exact byte formats
  repository.py   The .pygit folder: HEAD, branches, revisions, config, ignore rules
  index.py        The staging area
  commands.py     Every command, as a function that returns the text to print
  diff.py         Unified diffs
  cli.py          Argument parsing
tests/            91 tests, including checks against the real git program
```

[HOW_IT_WORKS.md](HOW_IT_WORKS.md) explains the ideas behind each part: how a file becomes a blob, how a commit is built from the index, what a branch really is, and how checkout works.

## Tests

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

The suite covers the object formats, every command (including error cases such as dirty checkouts and invalid branch names), and **interoperability**: real `git fsck --strict`, `git log`, and `git ls-tree` run on pygit repositories, and pygit reads objects written by real Git. The interop tests are skipped if `git` is not installed, and one test about the executable bit is skipped on Windows.

## How it differs from real Git

pygit is a learning project, not a replacement for Git.

- The repository folder is `.pygit`, and the **index** is a JSON file (`.pygit/index.json`). Git's index is a binary format; everything else (objects, refs, HEAD) is Git's real format.
- Only **loose objects** are supported, not pack files.
- No merge, rebase, tags, stash, or remotes. History is followed through all parents, but pygit never creates merge commits.
- `checkout` refuses to run when tracked files have uncommitted changes, instead of trying to carry them over.
- `diff` prints a `diff --pygit` header and has no rename detection.
- `.pygitignore` supports simple wildcard patterns only (no negation).

## Background

This follows the "build your own Git" idea from the [build-your-own-x](https://github.com/codecrafters-io/build-your-own-x) collection. The design comes from Git's documented internals (see the "Git Internals" chapter of [Pro Git](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)).

## License

[MIT](LICENSE)
