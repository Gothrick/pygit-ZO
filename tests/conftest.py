import pytest

from pygit import commands
from pygit.cli import main
from pygit.repository import Repository


@pytest.fixture(autouse=True)
def stable_environment(monkeypatch):
    """The same author and clock in every test, so commit ids are predictable."""
    monkeypatch.setenv("PYGIT_AUTHOR_NAME", "Test Author")
    monkeypatch.setenv("PYGIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("PYGIT_DATE", "1700000000 +0000")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Repository:
    """An empty repository, with the current folder set to it."""
    monkeypatch.chdir(tmp_path)
    commands.init(tmp_path)
    return Repository.find(tmp_path)


@pytest.fixture
def pg(capsys):
    """Run the command line in-process: pg("add", "a.txt") returns what it printed."""

    def run(*args: str, expect: int = 0) -> str:
        code = main(list(args))
        captured = capsys.readouterr()
        assert code == expect, captured.err
        return captured.out if expect == 0 else captured.err

    return run


@pytest.fixture
def write(tmp_path):
    """write("dir/file.txt", "text") creates a file in the working folder."""

    def run(name: str, content: str | bytes = "") -> None:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content, str) else content)

    return run


@pytest.fixture
def commit_file(pg, write):
    """commit_file("a.txt", "text", "message") writes, stages, and commits one file."""

    def run(name: str, content: str, message: str) -> str:
        write(name, content)
        pg("add", name)
        return pg("commit", "-m", message)

    return run
