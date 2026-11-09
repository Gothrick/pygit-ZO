"""Checks against real Git: the whole point of using Git's own object format.

These tests are skipped when the `git` program is not installed.
"""

import os
import shutil
import subprocess

import pytest

from pygit import commands
from pygit.repository import Repository

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Real Git",
    "GIT_AUTHOR_EMAIL": "git@example.com",
    "GIT_COMMITTER_NAME": "Real Git",
    "GIT_COMMITTER_EMAIL": "git@example.com",
    "GIT_AUTHOR_DATE": "1700000000 +0000",
    "GIT_COMMITTER_DATE": "1700000000 +0000",
}


def git(*args: str, cwd, git_dir: str | None = None) -> str:
    command = ["git"] + ([f"--git-dir={git_dir}"] if git_dir else []) + list(args)
    result = subprocess.run(
        command, cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, f"{' '.join(command)}\n{result.stderr}"
    return result.stdout


@pytest.fixture
def populated(repo, pg, write):
    """A pygit repository with two commits, nested folders, and a name that sorts awkwardly."""
    write("README.md", "# demo\n")
    write("a.txt", "plain\n")
    write("a/inside.txt", "a folder named like a.txt\n")
    write("src/app.py", "print('hi')\n")
    write("src/util/helpers.py", "def helper(): ...\n")
    pg("add", ".")
    pg("commit", "-m", "First commit")
    write("README.md", "# demo\n\nSecond line.\n")
    pg("add", "README.md")
    pg("commit", "-m", "Update the readme")
    return repo


class TestRealGitReadsPygit:
    def test_git_fsck_finds_the_repository_healthy(self, populated):
        # --strict also checks that tree entries are sorted the way Git requires.
        git("fsck", "--strict", cwd=populated.worktree, git_dir=".pygit")

    def test_git_log_shows_the_same_history(self, populated, pg):
        real = git("log", "--format=%h %s", cwd=populated.worktree, git_dir=".pygit")

        assert real.strip().splitlines() == pg("log", "--oneline").strip().splitlines()

    def test_git_lists_the_same_files(self, populated, pg):
        real = git("ls-tree", "-r", "HEAD", cwd=populated.worktree, git_dir=".pygit")

        assert real.splitlines() == pg("ls-tree", "-r").splitlines()

    def test_git_prints_the_same_commit_text(self, populated):
        real = git("cat-file", "-p", "HEAD", cwd=populated.worktree, git_dir=".pygit")
        kind, data = populated.objects.read(populated.head_commit())

        assert kind == "commit"
        assert real == data.decode()

    def test_git_computes_the_same_object_ids(self, repo, write):
        write("sample.txt", "some text\nwith lines\n")

        ours = commands.hash_object(repo, repo.worktree / "sample.txt", write=False)

        assert ours == git("hash-object", "sample.txt", cwd=repo.worktree).strip()


class TestPygitReadsRealGit:
    @pytest.fixture
    def real_repo(self, tmp_path):
        folder = tmp_path / "real"
        folder.mkdir()
        git("init", "-q", "-b", "main", cwd=folder)
        (folder / "one.txt").write_text("first\n")
        (folder / "dir").mkdir()
        (folder / "dir" / "two.txt").write_text("second\n")
        git("add", ".", cwd=folder)
        git("commit", "-q", "-m", "Made by real git", cwd=folder)
        (folder / "one.txt").write_text("first, edited\n")
        git("commit", "-q", "-am", "Edit one", cwd=folder)
        return folder

    @pytest.fixture
    def adopted(self, real_repo, tmp_path, monkeypatch):
        """A pygit repository whose objects are copied straight from the real Git one."""
        folder = tmp_path / "adopted"
        folder.mkdir()
        monkeypatch.chdir(folder)
        commands.init(folder)
        repo = Repository.find(folder)
        # Real Git writes small repositories as loose objects, the format pygit reads.
        shutil.copytree(
            real_repo / ".git" / "objects", repo.git_dir / "objects", dirs_exist_ok=True
        )
        head = git("rev-parse", "HEAD", cwd=real_repo).strip()
        repo.write_ref("refs/heads/main", head)
        return repo

    def test_pygit_reads_the_history_real_git_wrote(self, adopted, pg):
        assert pg("log", "--oneline").strip().splitlines()[0].endswith("Edit one")
        assert len(pg("log", "--oneline").strip().splitlines()) == 2

    def test_pygit_lists_the_files_real_git_committed(self, adopted, pg):
        files = [line.split("\t")[1] for line in pg("ls-tree", "-r").splitlines()]

        assert files == ["dir/two.txt", "one.txt"]

    def test_pygit_reads_a_file_real_git_stored(self, adopted, pg):
        sha = adopted.commit_files(adopted.head_commit())["one.txt"][1]

        assert pg("cat-file", "-p", sha).replace("\r\n", "\n") == "first, edited\n"
