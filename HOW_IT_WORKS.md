# How pygit works

Git looks complicated from the outside, but its core is two small ideas:

1. **A store where the name of each item is a hash of its content.** Anything you save (a file, a folder, a commit) is stored under its SHA-1. Same content, same name; change one byte, get a different name.
2. **A few pointers** (branches and HEAD) that say which commit you are "on".

Everything else is built from those. This document follows one commit through pygit, with real output, then explains the other commands.

## 1. Objects: the content-addressed store

There are three kinds of objects (`pygit/objects.py`):

| Kind | Holds | Example |
| --- | --- | --- |
| **blob** | The bytes of one file (no name, no folder) | the text of `greet.py` |
| **tree** | One folder: a list of (mode, name, id) | `greet.py` pointing to a blob |
| **commit** | A root tree, parent commit(s), author, and message | "Greet by name" |

Every object is saved the same way:

1. Build the text `"<kind> <size>\0"` followed by the content.
2. The object's id is the **SHA-1 of that whole text**.
3. Compress it with zlib and write it to `.pygit/objects/<first 2 hex digits>/<remaining 38>`.

```text
.pygit/objects/79/6a37020799bbef2519a4df223d6bd724c9fb3e      <- a commit
```

This is Git's real loose-object format, which is why `git hash-object` and `pygit hash-object` print the same id for the same file, and why real Git can open these files.

Because the id depends only on content, saving a file twice stores it once, and an object can never silently change: if it did, its id would no longer match.

### What a commit looks like

`pygit cat-file -p HEAD` prints the content of the latest commit:

```text
tree 1d44c25b48d003a233ca7caf8aa1dc1c7f933e8d
parent 7826ba4fa3f051d4ae2aaa0c7b275367ac7cac49
author Ada Lovelace <ada@example.com> 1700003600 +0000
committer Ada Lovelace <ada@example.com> 1700003600 +0000

Greet by name
```

It does not contain any files. It points at a **tree** and at its **parent** commit. The parent link is what makes history a chain: each commit's id depends on its parent's id, so changing an old commit would change every id after it.

The tree it points to:

```text
$ pygit cat-file -p 1d44c25b48d003a233ca7caf8aa1dc1c7f933e8d
100644 blob f6bbfd7e57baf87503739a16929df68a5dfa6d42	greet.py
```

And the blob is the file's content, with no name attached (the name lives in the tree).

In code: `object_id()`, `ObjectStore.write()` and `ObjectStore.read()` do the hashing and storage; `encode_tree`/`decode_tree` and `encode_commit`/`decode_commit` do the byte formats.

> **A detail that matters:** Git sorts tree entries by name, but compares a sub-folder as if its name ended with `/`. So a folder `a` sorts *after* a file `a.txt` (`.` is before `/`). If trees are sorted any other way, the tree's id is different and real Git reports the repository as corrupt. `git fsck --strict` in the tests checks exactly this.

## 2. `add`: from the working folder to the index

`pygit add greet.py` (`commands.add`):

1. Reads the file and **writes it as a blob**, getting its SHA-1.
2. Records `path -> (mode, sha)` in the **index** (`pygit/index.py`), the staging area.

The index is the list of what the *next commit* will contain:

```json
{
  "greet.py": { "mode": "100644", "sha": "f6bbfd7e57baf87503739a16929df68a5dfa6d42" }
}
```

Real Git stores its index in a binary format; pygit uses JSON to stay readable (and names it `index.json` so real Git does not try to parse it).

If you edit the file after `add`, the commit still uses the version you staged, because the blob was already saved. That is why `status` can show a file as both staged and modified.

## 3. `commit`: from the index to a commit object

`commands.commit`:

1. **Build trees from the index.** The index is a flat list of paths such as `src/util/helpers.py`. `write_tree` nests them into one tree object per folder, from the bottom up, and returns the root tree's id. Identical folders get identical ids, so unchanged parts of a project are shared between commits.
2. **Write the commit object**: root tree, the current commit as parent (none for the first commit), author and committer lines, and the message.
3. **Move the branch**: write the new commit's id into `.pygit/refs/heads/main`.

If the root tree is identical to the parent's, nothing changed, and pygit refuses with "nothing to commit".

## 4. Branches and HEAD are just files

```text
.pygit/HEAD                  ref: refs/heads/main
.pygit/refs/heads/main       796a37020799bbef2519a4df223d6bd724c9fb3e
```

- A **branch** is a file that holds one commit id. Creating a branch writes a 41-byte file; that is why branches are cheap.
- **HEAD** says where you are. Normally it holds `ref: refs/heads/<branch>`, so committing moves that branch. When you `checkout` a commit directly, HEAD holds the commit id itself: a **detached HEAD**, and new commits move only HEAD.

`Repository.resolve()` turns what you type (`HEAD`, a branch, an abbreviated id, `HEAD~2`) into a full commit id, and `current_branch()`/`update_head()` handle the two HEAD modes.

## 5. `status`: comparing three snapshots

At any moment there are three versions of your project: the **last commit** (HEAD), the **index**, and the **working folder**. `compute_status` compares them in pairs:

| Compare | Finds |
| --- | --- |
| HEAD vs index | "Changes to be committed": new, modified, deleted |
| Index vs working folder | "Changes not staged": modified or deleted since `add` |
| Working folder vs index | "Untracked": files the index does not know |

To tell whether a file changed, pygit hashes it and compares the hash with the one stored in the index. It never compares file contents line by line.

## 6. `diff`

`diff` picks the two versions to compare (working folder vs index, or index vs HEAD with `--staged`), reads both as bytes, and hands them to Python's `difflib.unified_diff` (`pygit/diff.py`). Missing files are shown against `/dev/null`, binary files (any NUL byte) are not diffed, and a last line without a newline gets the usual `\ No newline at end of file` marker.

## 7. `log`

`log` starts at a commit and follows parent links. It uses a priority queue ordered by author time, so history that ever has several parents would still come out newest first. Each commit is read once, and a commit reached through two paths is shown once.

## 8. `checkout`: changing the working folder safely

Switching to another branch or commit means making the working folder look like that commit (`commands.checkout`):

1. **Refuse if it could lose work.** If any tracked file has uncommitted changes, stop with an error. Untracked files that the target commit would overwrite also stop it.
2. **Delete** files that exist in the current commit but not in the target, and remove folders that become empty.
3. **Write** files whose content differs in the target, from their blobs.
4. **Replace the index** with the target's files, so the project starts clean.
5. **Move HEAD**: to the branch, or to the bare commit id (detached).

Order matters when a path changes type. If `thing` was a file and becomes a folder (or the reverse), the old entry must be deleted before the new one is written; `add` also drops index entries that can no longer coexist (`_drop_conflicts`). Tests cover both directions.

## 9. Proving it works: testing against real Git

The most useful tests do not check pygit against itself. They run the real `git` program:

- `git fsck --strict` checks every object pygit wrote, including the tree sort order.
- `git log` and `git ls-tree -r` must print the same history and files as pygit.
- `git cat-file -p HEAD` must print the same commit text, and `git hash-object` the same ids.
- In the other direction, objects created by real Git are copied into a pygit repository and read back.

The tests were also checked by breaking the code on purpose (sorting trees the wrong way, removing the checkout safety check) to confirm that they fail when they should.

## Where to look

| Idea | File | Start with |
| --- | --- | --- |
| Hashing and storing objects | `pygit/objects.py` | `object_id`, `ObjectStore` |
| Trees from a flat file list | `pygit/objects.py` | `write_tree`, `flatten_tree` |
| HEAD, branches, revisions | `pygit/repository.py` | `resolve`, `update_head` |
| Staging | `pygit/commands.py` | `add` |
| Making a commit | `pygit/commands.py` | `commit` |
| Comparing snapshots | `pygit/commands.py` | `compute_status` |
| Switching branches | `pygit/commands.py` | `checkout` |
