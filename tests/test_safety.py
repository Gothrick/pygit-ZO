"""Edge cases that could lose data, corrupt a repository, or crash: each one was a real bug."""

import shutil
import subprocess
from unittest.mock import patch

import pytest

from pygit import commands
from pygit.cli import main
from pygit.errors import PygitError
from pygit.objects import BLOB, COMMIT, Commit, encode_commit, write_tree
from pygit.repository import Repository, is_internal, valid_branch_name

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


class TestCheckoutNeverOverwritesYourFiles:
    def test_an_ignored_untracked_file_is_not_overwritten(self, pg, repo, write, tmp_path):
        write("base.txt", "base")
        pg("add", "base.txt")
        pg("commit", "-m", "base")
        pg("checkout", "-b", "feature")
        write("secret.txt", "tracked on feature")
        pg("add", "secret.txt")
        pg("commit", "-m", "track the secret")
        pg("checkout", "main")
        write(".pygitignore", "secret.txt\n")
        write("secret.txt", "MY LOCAL DATA")

        error = pg("checkout", "feature", expect=1)

        assert "would be overwritten" in error and "secret.txt" in error
        assert (tmp_path / "secret.txt").read_text() == "MY LOCAL DATA"
        assert repo.current_branch() == "main"

    def test_an_untracked_folder_where_a_file_goes_is_refused_cleanly(
        self, pg, repo, write, tmp_path
    ):
        write("base.txt", "base")
        pg("add", "base.txt")
        pg("commit", "-m", "base")
        pg("checkout", "-b", "feature")
        write("thing", "a file on feature")
        pg("add", "thing")
        pg("commit", "-m", "file")
        pg("checkout", "main")
        write("thing/notes.txt", "my notes")

        error = pg("checkout", "feature", expect=1)

        assert "would be overwritten" in error
        assert (tmp_path / "thing" / "notes.txt").read_text() == "my notes"
        assert repo.current_branch() == "main"

    def test_an_untracked_file_where_a_folder_goes_leaves_everything_untouched(
        self, pg, repo, write, tmp_path
    ):
        write("extra.txt", "tracked on main")
        pg("add", "extra.txt")
        pg("commit", "-m", "main work")
        pg("checkout", "-b", "feature")
        pg("checkout", "main")
        # The feature branch gets a folder "d"; main has a plain untracked file "d".
        pg("checkout", "feature")
        write("d/in.txt", "inside")
        pg("add", "d")
        pg("commit", "-m", "folder")
        pg("checkout", "main")
        write("d", "my plain file")

        error = pg("checkout", "feature", expect=1)

        assert "would be overwritten" in error
        # Nothing was deleted before the refusal: the tracked file is still there, status is sane.
        assert (tmp_path / "extra.txt").read_text() == "tracked on main"
        assert (tmp_path / "d").read_text() == "my plain file"
        assert "deleted:" not in pg("status")

    def test_checkout_head_is_a_no_op_that_keeps_the_branch(self, pg, repo, write, commit_file):
        commit_file("a.txt", "a", "one")
        write("a.txt", "dirty")

        assert pg("checkout", "HEAD").startswith("Already on 'main'")
        assert repo.current_branch() == "main"

    def test_checking_out_the_current_branch_works_even_with_changes(
        self, pg, repo, write, commit_file
    ):
        commit_file("a.txt", "a", "one")
        write("a.txt", "dirty")

        assert pg("checkout", "main").startswith("Already on 'main'")


class TestIgnoreRules:
    def test_a_tracked_file_that_becomes_ignored_stays_tracked(self, pg, repo, write, commit_file):
        commit_file("keep.txt", "v1", "one")
        write(".pygitignore", "keep.txt\n")

        assert "deleted" not in pg("status")
        pg("add", ".")
        assert "keep.txt" in pg("ls-files")

    def test_edits_to_a_tracked_but_ignored_file_are_still_staged_by_add_dot(
        self, pg, repo, write, commit_file
    ):
        commit_file("keep.txt", "v1", "one")
        write(".pygitignore", "keep.txt\n")
        write("keep.txt", "v2")

        assert "modified:   keep.txt" in pg("status")
        pg("add", ".")
        pg("commit", "-m", "update")
        blob = repo.commit_files(repo.head_commit())["keep.txt"][1]
        assert repo.objects.read(blob)[1] == b"v2"

    def test_adding_an_ignored_folder_is_an_error_not_a_silent_no_op(self, pg, repo, write):
        write(".pygitignore", "build/\n")
        write("build/out.bin", "x")

        assert "is ignored" in pg("add", "build", expect=1)

    def test_a_leading_slash_anchors_a_pattern_to_the_top_folder(self, pg, repo, write):
        write(".pygitignore", "/root.tmp\n")
        write("root.tmp", "ignored")
        write("sub/root.tmp", "kept")

        output = pg("status")

        assert "\troot.tmp" not in output
        assert "sub/root.tmp" in output

    def test_patterns_are_read_once_per_scan_not_once_per_file(self, repo, write):
        write(".pygitignore", "*.log\n")
        for number in range(40):
            write(f"src/file{number}.txt", "x")

        with patch.object(
            Repository, "_ignore_patterns", autospec=True, side_effect=Repository._ignore_patterns
        ) as spy:
            repo.worktree_files()

        assert spy.call_count == 1

    def test_a_byte_order_mark_does_not_disable_the_first_pattern(self, pg, repo, write):
        (repo.worktree / ".pygitignore").write_bytes(b"\xef\xbb\xbfsecret.txt\n")
        write("secret.txt", "x")

        assert "secret.txt" not in pg("status")


class TestRepositoryFoldersAreOffLimits:
    @pytest.mark.parametrize("path", [".pygit", ".pygit/HEAD", ".git", "a/.git/config", ".GIT"])
    def test_internal_paths_are_recognised(self, path):
        assert is_internal(path)

    def test_normal_paths_are_not_internal(self):
        assert not is_internal("src/.gitignore") and not is_internal("my.git/readme")

    def test_adding_the_repository_folder_is_refused(self, pg, repo, write):
        write("a.txt", "a")

        assert "cannot be added" in pg("add", ".pygit", expect=1)
        assert "cannot be added" in pg("add", ".pygit/HEAD", expect=1)
        assert pg("ls-files") == ""

    def test_a_nested_git_folder_is_skipped_by_add_dot(self, pg, repo, write):
        write("vendor/lib.py", "lib")
        write("vendor/.git/HEAD", "ref: refs/heads/main")
        write("vendor/.git/config", "[core]")

        pg("add", ".")

        assert [line.split(" ")[-1] for line in pg("ls-files").splitlines()] == ["vendor/lib.py"]
        assert ".git" not in pg("status")

    @needs_git
    def test_a_repository_with_a_vendored_git_folder_still_passes_fsck(self, pg, repo, write):
        write("vendor/lib.py", "lib")
        write("vendor/.git/HEAD", "ref: refs/heads/main")
        pg("add", ".")
        pg("commit", "-m", "vendored")

        result = subprocess.run(
            ["git", "--git-dir=.pygit", "fsck", "--strict"],
            cwd=repo.worktree,
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0, result.stderr


class TestBranchNames:
    @pytest.mark.parametrize(
        "name",
        [
            "has space",
            "-dash",
            "a..b",
            "ends.lock",
            "a//b",
            "x~1",
            "HEAD",
            "@",
            "x@{1}",
            ".hidden",
            "a/.hidden",
            "a|b",
            'q"uote',
            "a<b>",
            "aux",
            "feature/con",
            "NUL.txt",
            "trailing.",
            "abcd1234",  # looks like a commit id
            "line\nbreak",
            "",
        ],
    )
    def test_invalid_names_are_refused(self, name):
        assert not valid_branch_name(name)

    @pytest.mark.parametrize("name", ["main", "feature/login", "fix-42", "v1.2.3", "Résumé", "a_b"])
    def test_ordinary_names_are_accepted(self, name):
        assert valid_branch_name(name)

    def test_deleting_cannot_reach_outside_refs_heads(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        keep = repo.git_dir / "index.json"
        assert keep.exists()

        assert "not found" in pg("branch", "-d", "../../index.json", expect=1)
        assert keep.exists()

    def test_a_name_that_the_system_rejects_is_a_clean_error(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "not a valid branch name" in pg("branch", "a|b", expect=1)

    def test_branch_and_folder_names_cannot_collide(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "feature/x")

        assert "conflicts with the existing branch" in pg("branch", "feature", expect=1)
        pg("branch", "other")
        assert "conflicts with the existing branch" in pg("branch", "other/x/y", expect=1)

    def test_deleting_a_nested_branch_removes_the_empty_folders(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "feature/x")
        pg("branch", "-d", "feature/x")

        assert not (repo.git_dir / "refs" / "heads" / "feature").exists()
        pg("branch", "feature")  # used to crash: a leftover folder had that name
        assert "feature" in pg("branch")

    def test_a_branch_name_never_hides_a_commit_id(self, repo, pg, commit_file):
        commit_file("a.txt", "a", "one")
        sha = repo.head_commit()

        assert repo.resolve(sha[:8]) == sha
        assert "not a valid branch name" in pg("branch", sha[:8], expect=1)


class TestIdentity:
    @pytest.mark.parametrize("bad", ["Bad <x>", "two\nlines", "", "   ", "a>b"])
    def test_names_that_would_corrupt_a_commit_are_refused(self, pg, repo, write, monkeypatch, bad):
        monkeypatch.setenv("PYGIT_AUTHOR_NAME", bad)
        write("a.txt", "a")
        pg("add", "a.txt")

        error = pg("commit", "-m", "x", expect=1)

        assert "author name" in error or "tell pygit who you are" in error

    def test_an_email_with_angle_brackets_is_refused(self, pg, repo, write, monkeypatch):
        monkeypatch.setenv("PYGIT_AUTHOR_EMAIL", "<evil@example.com>")
        write("a.txt", "a")
        pg("add", "a.txt")

        assert "author email" in pg("commit", "-m", "x", expect=1)

    def test_config_refuses_a_bad_identity_up_front(self, pg, repo):
        assert "invalid user.name" in pg("config", "user.name", "Bad <name>", expect=1)

    def test_config_keys_must_be_section_dot_name(self, pg, repo):
        assert "invalid key" in pg("config", "a.b.c", "v", expect=1)
        assert "invalid key" in pg("config", "nodot", "v", expect=1)

    def test_surrounding_spaces_are_trimmed(self, pg, repo, write, monkeypatch):
        monkeypatch.setenv("PYGIT_AUTHOR_NAME", "  Padded Name  ")
        write("a.txt", "a")
        pg("add", "a.txt")
        pg("commit", "-m", "x")

        assert "Author: Padded Name <test@example.com>" in pg("log")


class TestRepositoryFormatSupport:
    def test_references_packed_by_git_gc_can_be_read(self, repo, pg, commit_file):
        commit_file("a.txt", "a", "one")
        sha = repo.head_commit()
        (repo.git_dir / "refs" / "heads" / "main").unlink()
        (repo.git_dir / "packed-refs").write_bytes(
            f"# pack-refs with: peeled\n{sha} refs/heads/main\n{sha} refs/heads/old\n".encode()
        )

        assert repo.head_commit() == sha
        assert "old" in pg("branch")
        assert "one" in pg("log", "--oneline")

    def test_a_branch_stored_only_in_packed_refs_cannot_be_deleted_by_mistake(
        self, repo, pg, commit_file
    ):
        commit_file("a.txt", "a", "one")
        sha = repo.head_commit()
        (repo.git_dir / "packed-refs").write_bytes(f"{sha} refs/heads/old\n".encode())

        assert "packed-refs" in pg("branch", "-d", "old", expect=1)

    def test_a_missing_object_mentions_pack_files_when_there_are_some(self, repo):
        pack = repo.git_dir / "objects" / "pack"
        pack.mkdir()
        (pack / "pack-123.pack").write_bytes(b"")

        with pytest.raises(PygitError, match="pack file"):
            repo.objects.read("a" * 40)

    def test_a_tree_with_an_unsafe_path_is_rejected(self, repo):
        from pygit.objects import TREE, TreeEntry, encode_tree, flatten_tree

        blob = repo.objects.write("blob", b"x")
        for name in ["..", ".", "a/b", ".git", ".PYGIT"]:
            tree = repo.objects.write(TREE, encode_tree([TreeEntry("100644", name, blob)]))
            with pytest.raises(PygitError, match="unsafe|forbidden"):
                flatten_tree(repo.objects, tree)


class TestSmallThings:
    def test_a_negative_log_limit_is_refused(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "zero or more" in pg("log", "-n", "-1", expect=1)

    def test_creating_a_branch_from_a_bad_start_point_is_refused(self, repo, commit_file):
        commit_file("a.txt", "a", "one")

        with pytest.raises(PygitError, match="unknown revision"):
            commands.branch(repo, "topic", "nope", delete=False)


class TestBranchNamesAcrossFileSystems:
    def test_names_differing_only_by_case_cannot_overwrite_each_other(
        self, pg, repo, commit_file, write
    ):
        commit_file("a.txt", "a", "one")
        pg("branch", "dev")
        first = repo.read_ref("refs/heads/dev")
        commit_file("b.txt", "b", "two")

        assert "conflicts with the existing branch 'dev'" in pg("branch", "Dev", expect=1)
        assert repo.read_ref("refs/heads/dev") == first

    def test_creating_and_switching_to_a_case_variant_is_refused(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("checkout", "-b", "dev")

        assert "conflicts" in pg("checkout", "-b", "Dev", expect=1)
        assert repo.current_branch() == "dev"

    def test_a_case_variant_of_a_folder_branch_is_refused(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "feature/x")

        assert "conflicts" in pg("branch", "Feature", expect=1)
        assert "conflicts" in pg("branch", "FEATURE/X", expect=1)

    def test_the_error_says_why_a_name_is_refused(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "looks like a commit id" in pg("branch", "cafe", expect=1)
        assert "reserved file name on Windows" in pg("branch", "aux", expect=1)
        assert "not allowed" in pg("branch", "a|b", expect=1)

    def test_a_loose_ref_and_a_packed_ref_of_the_same_branch_cannot_be_half_deleted(
        self, pg, repo, commit_file
    ):
        commit_file("a.txt", "a", "one")
        pg("branch", "topic")
        sha = repo.read_ref("refs/heads/topic")
        (repo.git_dir / "packed-refs").write_bytes(f"{sha} refs/heads/topic\n".encode())

        assert "packed-refs" in pg("branch", "-d", "topic", expect=1)
        assert "topic" in pg("branch")


class TestCheckoutEdgeCases:
    def test_empty_untracked_folders_where_a_file_goes_are_cleared_not_fatal(
        self, pg, repo, write, commit_file, tmp_path
    ):
        commit_file("a.txt", "main version", "main")
        pg("checkout", "-b", "f")
        write("a.txt", "f version")
        write("thing", "a file on f")
        pg("add", "a.txt", "thing")
        pg("commit", "-m", "f work")
        pg("checkout", "main")
        (tmp_path / "thing" / "sub").mkdir(parents=True)  # only empty folders

        pg("checkout", "f")

        assert (tmp_path / "thing").read_text() == "a file on f"
        assert (tmp_path / "a.txt").read_text() == "f version"
        assert repo.current_branch() == "f"
        assert "nothing to commit" in pg("status")

    def test_a_folder_with_a_file_inside_still_blocks_and_changes_nothing(
        self, pg, repo, write, commit_file, tmp_path
    ):
        commit_file("a.txt", "main version", "main")
        pg("checkout", "-b", "f")
        write("a.txt", "f version")
        write("thing", "a file on f")
        pg("add", "a.txt", "thing")
        pg("commit", "-m", "f work")
        pg("checkout", "main")
        write("thing/notes.txt", "keep me")

        assert "would be overwritten" in pg("checkout", "f", expect=1)
        assert (tmp_path / "a.txt").read_text() == "main version"  # untouched
        assert (tmp_path / "thing" / "notes.txt").read_text() == "keep me"

    def test_a_rename_that_only_changes_letter_case_can_be_checked_out(
        self, pg, repo, commit_file, tmp_path
    ):
        # Such a commit usually comes from a case-sensitive system (Linux): build it directly.
        commit_file("Readme.md", "docs", "one")
        first = repo.head_commit()
        blob = repo.objects.write(BLOB, b"docs")
        tree = write_tree(repo.objects, {"README.md": ("100644", blob)})
        ident = "Test Author <test@example.com> 1700000100 +0000"
        renamed = repo.objects.write(
            COMMIT, encode_commit(Commit(tree, [first], ident, ident, "rename to upper case"))
        )
        repo.write_ref("refs/heads/f", renamed)

        pg("checkout", "f")  # used to refuse on Windows: "untracked file would be overwritten"
        assert "README.md" in [p.name for p in tmp_path.iterdir()]
        assert "nothing to commit" in pg("status")

        pg("checkout", "main")
        assert "Readme.md" in [p.name for p in tmp_path.iterdir()]
        assert "nothing to commit" in pg("status")

    def test_leaving_a_detached_commit_behind_warns_how_to_keep_it(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("checkout", repo.head_commit())
        commit_file("b.txt", "b", "detached work")
        orphan = repo.head_commit()

        output = pg("checkout", "main")

        assert "Warning" in output and orphan[:7] in output and "pygit branch" in output
        # Keeping the commit on a branch silences the warning.
        pg("checkout", orphan)
        pg("branch", "rescue")
        assert "Warning" not in pg("checkout", "main")

    def test_no_warning_when_the_detached_commit_is_on_a_branch(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("checkout", repo.head_commit())

        assert "Warning" not in pg("checkout", "main")


class TestInputLimits:
    @pytest.mark.parametrize("value", ["99999999999999999999 +0000", "99999999999 +0000"])
    def test_a_date_that_real_git_would_reject_is_refused(
        self, pg, repo, write, monkeypatch, value
    ):
        monkeypatch.setenv("PYGIT_DATE", value)
        write("a.txt", "a")
        pg("add", "a.txt")

        assert "PYGIT_DATE" in pg("commit", "-m", "x", expect=1)

    @pytest.mark.parametrize("value", ["1700000000 +9999", "1700000000 +2460", "1700000000 -0099"])
    def test_a_time_zone_out_of_range_is_refused(self, pg, repo, write, monkeypatch, value):
        monkeypatch.setenv("PYGIT_DATE", value)
        write("a.txt", "a")
        pg("add", "a.txt")

        assert "out of range" in pg("commit", "-m", "x", expect=1)

    @pytest.mark.parametrize("content", ['{"a": 1}', "[]", '{"a": {"mode": "100644"}}', "{broken"])
    def test_a_damaged_index_is_a_clean_error(self, pg, repo, content):
        (repo.git_dir / "index.json").write_bytes(content.encode())

        assert "damaged" in pg("status", expect=1)

    def test_a_revision_suffix_must_use_plain_digits(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "unknown revision" in pg("log", "HEAD~²", expect=1)

    def test_hashing_a_folder_says_so(self, pg, repo):
        assert "is a folder" in pg("hash-object", ".pygit", expect=1)


class TestIgnorePatternEdges:
    def test_a_trailing_slash_means_folders_only(self, pg, repo, write):
        write(".pygitignore", "build/\n")
        write("build", "a file that happens to be called build")
        write("sub/build/out.bin", "inside a folder called build")
        write("keep.txt", "keep")

        listed = pg("status")

        assert "\tbuild\n" in listed + "\n"  # the plain file is not ignored
        assert "out.bin" not in listed  # the folder is

    def test_a_pattern_made_only_of_a_wildcard_and_slash_does_not_hide_everything(
        self, pg, repo, write
    ):
        write(".pygitignore", "*/\n")
        write("top.txt", "top")
        write("folder/inner.txt", "inner")

        listed = pg("status")

        assert "top.txt" in listed
        assert "inner.txt" not in listed


class TestWindowsAliasesAndErrorHandling:
    def test_the_short_name_of_a_git_folder_is_internal(self):
        assert is_internal("git~1/config") and is_internal("sub/GIT~2/x")
        assert not is_internal("gitignore-notes/x")

    def test_adding_a_short_git_folder_name_is_refused(self, pg, repo, write):
        write("GIT~1/config", "x")

        assert "cannot be added" in pg("add", "GIT~1", expect=1)
        pg("add", ".")
        assert "GIT~1" not in pg("ls-files")

    def test_an_unexpected_system_error_becomes_a_one_line_message(self, repo, monkeypatch, capsys):
        def boom(*_args):
            raise OSError("the disk went away")

        monkeypatch.setattr(commands, "status", boom)

        assert main(["status"]) == 1
        assert "error: the disk went away" in capsys.readouterr().err

    def test_debug_mode_shows_the_real_traceback_for_unexpected_errors(self, repo, monkeypatch):
        def boom(*_args):
            raise OSError("the disk went away")

        monkeypatch.setattr(commands, "status", boom)
        monkeypatch.setenv("PYGIT_DEBUG", "1")

        with pytest.raises(OSError, match="the disk went away"):
            main(["status"])

    def test_ctrl_c_ends_quietly_with_the_conventional_status(self, repo, monkeypatch, capsys):
        def interrupt(*_args):
            raise KeyboardInterrupt

        monkeypatch.setattr(commands, "status", interrupt)

        assert main(["status"]) == 130
        assert "Traceback" not in capsys.readouterr().err
