"""Run the command line as a separate process, with piped output, the way a user's shell would.

The other tests call the CLI inside the test process, where output goes to an in-memory
buffer. That hides problems that only appear with real, redirected output.
"""

import os
import subprocess
import sys
import zlib
from pathlib import Path

import pytest


@pytest.fixture
def cli(tmp_path):
    """cli("status") runs `python -m pygit status` in tmp_path: returns (code, stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[1]),  # works without installing the package
        PYGIT_AUTHOR_NAME="Test Author",
        PYGIT_AUTHOR_EMAIL="test@example.com",
        PYGIT_DATE="1700000000 +0000",
    )

    def run(*args: str, extra_env: dict | None = None) -> tuple[int, str, str]:
        result = subprocess.run(
            [sys.executable, "-m", "pygit", *args],
            cwd=tmp_path,
            env={**env, **(extra_env or {})},
            capture_output=True,
            check=False,
        )
        return result.returncode, result.stdout.decode("utf-8"), result.stderr.decode("utf-8")

    run("init")
    return run


class TestPipedOutput:
    def test_non_ascii_file_names_are_printed_as_utf8(self, cli, tmp_path):
        (tmp_path / "café ünï.txt").write_text("accents")
        (tmp_path / "日本語.txt").write_text("kanji")

        code, out, err = cli("status")
        assert (code, "Traceback" in err) == (0, False), err
        assert "café ünï.txt" in out and "日本語.txt" in out

        assert cli("add", ".")[0] == 0
        assert cli("commit", "-m", "Ajouter les fichiers")[0] == 0
        code, out, err = cli("ls-tree", "-r", "HEAD")
        assert code == 0, err
        assert "日本語.txt" in out and "café ünï.txt" in out

    def test_non_ascii_commit_messages_and_authors_are_printed(self, cli, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        cli("add", "a.txt")
        code, _, err = cli(
            "commit", "-m", "Réparer l'écran 🚀", extra_env={"PYGIT_AUTHOR_NAME": "Zoë Müller"}
        )
        assert code == 0, err

        code, out, err = cli("log")

        assert code == 0, err
        assert "Zoë Müller" in out and "Réparer l'écran" in out

    def test_unicode_branch_names_are_printed(self, cli, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        cli("add", "a.txt")
        cli("commit", "-m", "first")
        cli("branch", "fonctionnalité")

        code, out, err = cli("branch")

        assert code == 0, err
        assert "fonctionnalité" in out


class TestCleanErrors:
    """Problems are reported in one line with exit status 1, never as a Python traceback."""

    def assert_clean_error(self, result, message):
        code, out, err = result
        assert code == 1
        assert "Traceback" not in err
        assert message in err

    def test_hashing_a_missing_file(self, cli):
        self.assert_clean_error(cli("hash-object", "nofile.txt"), "no such file")

    def test_a_malformed_date_override(self, cli, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        cli("add", "a.txt")

        result = cli("commit", "-m", "x", extra_env={"PYGIT_DATE": "garbage"})

        self.assert_clean_error(result, "PYGIT_DATE")

    def test_initializing_over_a_file(self, cli, tmp_path):
        (tmp_path / "afile").write_text("x")

        self.assert_clean_error(cli("init", "afile"), "is a file")

    def test_a_corrupt_object(self, cli, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        cli("add", "a.txt")
        cli("commit", "-m", "first")
        head = (tmp_path / ".pygit" / "refs" / "heads" / "main").read_text().strip()
        (tmp_path / ".pygit" / "objects" / head[:2] / head[2:]).write_bytes(b"not zlib data")

        self.assert_clean_error(cli("log"), "corrupt")

    def test_an_object_with_a_broken_header(self, cli, tmp_path):
        (tmp_path / "a.txt").write_text("a")
        cli("add", "a.txt")
        cli("commit", "-m", "first")
        head = (tmp_path / ".pygit" / "refs" / "heads" / "main").read_text().strip()
        broken = zlib.compress(b"commit 99\x00short")
        (tmp_path / ".pygit" / "objects" / head[:2] / head[2:]).write_bytes(broken)

        self.assert_clean_error(cli("log"), "corrupt")

    def test_pygit_errors_are_reported_the_same_way_in_debug_mode(self, cli):
        code, _, err = cli("hash-object", "nofile.txt", extra_env={"PYGIT_DEBUG": "1"})

        assert code == 1  # a PygitError is reported cleanly; debug only affects other errors
        assert "no such file" in err
