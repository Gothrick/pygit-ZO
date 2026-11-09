import os

import pytest

from pygit import commands
from pygit.errors import PygitError


class TestInitAndConfig:
    def test_init_creates_the_repository_layout(self, tmp_path):
        message = commands.init(tmp_path)

        assert message.startswith("Initialized empty")
        assert (tmp_path / ".pygit" / "objects").is_dir()
        assert (tmp_path / ".pygit" / "refs" / "heads").is_dir()
        assert (tmp_path / ".pygit" / "HEAD").read_bytes() == b"ref: refs/heads/main\n"

    def test_init_twice_keeps_the_existing_repository(self, repo, tmp_path):
        message = commands.init(tmp_path)

        assert message.startswith("Reinitialized existing")

    def test_commands_outside_a_repository_explain_what_to_do(self, tmp_path, monkeypatch, pg):
        folder = tmp_path / "empty"
        folder.mkdir()
        monkeypatch.chdir(folder)

        assert "run 'pygit init'" in pg("status", expect=1)

    def test_a_setting_can_be_stored_and_read(self, pg, repo):
        pg("config", "user.name", "Ada Lovelace")

        assert pg("config", "user.name").strip() == "Ada Lovelace"

    def test_reading_an_unset_setting_fails(self, pg, repo):
        assert "not set" in pg("config", "user.email", expect=1)

    def test_commit_uses_the_stored_identity_when_there_is_no_environment_override(
        self, pg, repo, write, monkeypatch
    ):
        monkeypatch.delenv("PYGIT_AUTHOR_NAME")
        monkeypatch.delenv("PYGIT_AUTHOR_EMAIL")
        pg("config", "user.name", "Stored Name")
        pg("config", "user.email", "stored@example.com")
        write("a.txt", "a")
        pg("add", "a.txt")

        pg("commit", "-m", "first")

        assert "Author: Stored Name <stored@example.com>" in pg("log")

    def test_commit_asks_who_you_are_when_nothing_is_set(self, pg, repo, write, monkeypatch):
        monkeypatch.delenv("PYGIT_AUTHOR_NAME")
        monkeypatch.delenv("PYGIT_AUTHOR_EMAIL")
        write("a.txt", "a")
        pg("add", "a.txt")

        assert "pygit config user.name" in pg("commit", "-m", "first", expect=1)


class TestPlumbing:
    def test_hash_object_matches_real_git_and_can_store(self, pg, repo, write):
        write("hello.txt", "hello world\n")

        assert pg("hash-object", "hello.txt").strip() == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"
        assert not repo.objects.exists("3b18e512dba79e4c8300dd08aeb37f8e728b8dad")

        pg("hash-object", "-w", "hello.txt")
        assert repo.objects.exists("3b18e512dba79e4c8300dd08aeb37f8e728b8dad")

    def test_cat_file_shows_type_size_and_content(self, pg, repo, write):
        write("hello.txt", "hello world\n")
        sha = pg("hash-object", "-w", "hello.txt").strip()

        assert pg("cat-file", "-t", sha).strip() == "blob"
        assert pg("cat-file", "-s", sha).strip() == "12"
        assert pg("cat-file", "-p", sha).replace("\r\n", "\n") == "hello world\n"

    def test_cat_file_on_a_commit_tree_lists_its_entries(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "first")

        tree = repo.read_commit(repo.head_commit()).tree
        listing = pg("cat-file", "-p", tree)

        assert listing.startswith("100644 blob ")
        assert listing.strip().endswith("\ta.txt")

    def test_ls_files_lists_the_staged_files(self, pg, repo, write):
        write("b.txt", "b")
        write("a.txt", "a")
        pg("add", "b.txt", "a.txt")

        names = [line.split(" ")[-1] for line in pg("ls-files").splitlines()]

        assert names == ["a.txt", "b.txt"]

    def test_ls_tree_lists_a_commit_with_and_without_recursion(self, pg, repo, commit_file):
        commit_file("src/app.py", "app", "first")

        assert pg("ls-tree").strip().endswith("\tsrc")
        assert "040000 tree" in pg("ls-tree")
        assert pg("ls-tree", "-r").strip().endswith("\tsrc/app.py")


class TestAddAndStatus:
    def test_a_fresh_repository_reports_nothing_to_commit(self, pg, repo):
        output = pg("status")

        assert "On branch main" in output
        assert "No commits yet" in output
        assert "nothing to commit, working tree clean" in output

    def test_untracked_staged_and_modified_files_are_reported(self, pg, repo, write, commit_file):
        commit_file("kept.txt", "v1", "first")
        write("kept.txt", "v2")
        write("new.txt", "new")
        pg("add", "new.txt")
        write("loose.txt", "loose")

        output = pg("status")

        assert "Changes to be committed:\n\tnew file:   new.txt" in output
        assert "Changes not staged for commit:\n\tmodified:   kept.txt" in output
        assert "Untracked files:\n\tloose.txt" in output

    def test_adding_a_folder_stages_everything_below_it(self, pg, repo, write):
        write("src/a.py", "a")
        write("src/deep/b.py", "b")
        write("other.txt", "o")

        pg("add", "src")

        assert [line.split(" ")[-1] for line in pg("ls-files").splitlines()] == [
            "src/a.py",
            "src/deep/b.py",
        ]

    def test_a_deleted_file_is_staged_as_deleted(self, pg, repo, tmp_path, commit_file):
        commit_file("gone.txt", "x", "first")
        (tmp_path / "gone.txt").unlink()

        assert "deleted:    gone.txt" in pg("status")
        pg("add", "gone.txt")
        assert "Changes to be committed:\n\tdeleted:    gone.txt" in pg("status")

    def test_adding_a_missing_file_fails(self, pg, repo):
        assert "did not match any files" in pg("add", "nope.txt", expect=1)

    def test_adding_a_path_outside_the_repository_fails(self, pg, repo):
        assert "outside the repository" in pg("add", os.path.join("..", "x.txt"), expect=1)

    def test_ignored_files_are_left_out(self, pg, repo, write):
        write(".pygitignore", "*.log\nbuild/\n")
        write("app.py", "app")
        write("debug.log", "noise")
        write("build/output.bin", "bin")

        output = pg("status")

        assert "app.py" in output
        assert "debug.log" not in output
        assert "output.bin" not in output
        assert "is ignored" in pg("add", "debug.log", expect=1)

    def test_the_repository_folder_is_never_listed(self, pg, repo, write):
        write("a.txt", "a")

        assert ".pygit" not in pg("status")


class TestCommit:
    def test_the_first_commit_is_marked_as_the_root(self, pg, repo, commit_file):
        output = commit_file("a.txt", "a", "First commit")

        assert output.startswith("[main (root-commit) ")
        assert "] First commit" in output
        assert "1 file(s) changed" in output

    def test_a_commit_records_the_configured_author_and_date(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "First commit")
        first = repo.head_commit()

        assert len(first) == 40
        assert repo.read_commit(first).author == "Test Author <test@example.com> 1700000000 +0000"

    def test_a_second_commit_points_to_the_first(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        first = repo.head_commit()
        commit_file("b.txt", "b", "two")

        assert repo.read_commit(repo.head_commit()).parents == [first]

    def test_committing_with_nothing_staged_fails(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "nothing to commit" in pg("commit", "-m", "again", expect=1)

    def test_committing_an_empty_repository_fails(self, pg, repo):
        assert "nothing to commit" in pg("commit", "-m", "nothing", expect=1)

    def test_an_empty_message_is_refused(self, pg, repo, write):
        write("a.txt", "a")
        pg("add", "a.txt")

        assert "message is empty" in pg("commit", "-m", "  ", expect=1)

    def test_staged_changes_do_not_include_later_edits(self, pg, repo, write):
        write("a.txt", "staged version")
        pg("add", "a.txt")
        write("a.txt", "edited afterwards")
        pg("commit", "-m", "first")

        blob = repo.commit_files(repo.head_commit())["a.txt"][1]
        assert repo.objects.read(blob)[1] == b"staged version"
        assert "modified:   a.txt" in pg("status")


class TestLog:
    def test_log_lists_commits_newest_first(self, pg, repo, commit_file, monkeypatch):
        commit_file("a.txt", "a", "first")
        monkeypatch.setenv("PYGIT_DATE", "1700000100 +0000")
        commit_file("b.txt", "b", "second")

        output = pg("log")

        assert output.index("second") < output.index("first")
        assert "Author: Test Author <test@example.com>" in output
        assert "Date:   Tue Nov 14 22:15:00 2023 +0000" in output

    def test_oneline_and_limits(self, pg, repo, commit_file, monkeypatch):
        commit_file("a.txt", "a", "first")
        monkeypatch.setenv("PYGIT_DATE", "1700000100 +0000")
        commit_file("b.txt", "b", "second")

        lines = pg("log", "--oneline").splitlines()

        assert [line.split(" ", 1)[1] for line in lines] == ["second", "first"]
        assert len(pg("log", "--oneline", "-n", "1").splitlines()) == 1

    def test_log_can_start_from_another_revision(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "first")
        commit_file("b.txt", "b", "second")

        assert [
            line.split(" ", 1)[1] for line in pg("log", "--oneline", "HEAD~1").splitlines()
        ] == ["first"]

    def test_log_before_the_first_commit_fails(self, pg, repo):
        assert "does not point to a commit" in pg("log", expect=1)

    def test_time_zones_are_kept(self, pg, repo, commit_file, monkeypatch):
        monkeypatch.setenv("PYGIT_DATE", "1700000000 -0500")
        commit_file("a.txt", "a", "first")

        assert "Date:   Tue Nov 14 17:13:20 2023 -0500" in pg("log")


class TestRevisions:
    def test_head_tilde_and_abbreviations_resolve(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        first = repo.head_commit()
        commit_file("b.txt", "b", "two")
        second = repo.head_commit()

        assert repo.resolve("HEAD") == second
        assert repo.resolve("HEAD~1") == first
        assert repo.resolve("HEAD~") == first
        assert repo.resolve(first[:8]) == first

    def test_going_past_the_first_commit_fails(self, repo, pg, commit_file):
        commit_file("a.txt", "a", "one")

        with pytest.raises(PygitError, match="no such ancestor"):
            repo.resolve("HEAD~1")

    def test_unknown_names_fail(self, repo, pg, commit_file):
        commit_file("a.txt", "a", "one")

        with pytest.raises(PygitError, match="unknown revision"):
            repo.resolve("nope")


class TestDiff:
    def test_an_edit_shows_as_a_unified_diff(self, pg, repo, write, commit_file):
        commit_file("a.txt", "one\ntwo\nthree\n", "first")
        write("a.txt", "one\nTWO\nthree\n")

        assert pg("diff") == (
            "diff --pygit a/a.txt b/a.txt\n"
            "--- a/a.txt\n"
            "+++ b/a.txt\n"
            "@@ -1,3 +1,3 @@\n"
            " one\n"
            "-two\n"
            "+TWO\n"
            " three\n"
        )

    def test_diff_is_empty_when_nothing_changed(self, pg, repo, commit_file):
        commit_file("a.txt", "same\n", "first")

        assert pg("diff") == ""

    def test_a_staged_diff_compares_the_index_with_the_last_commit(
        self, pg, repo, write, commit_file
    ):
        commit_file("a.txt", "old\n", "first")
        write("a.txt", "new\n")
        pg("add", "a.txt")

        assert pg("diff") == ""
        staged = pg("diff", "--staged")
        assert "-old" in staged and "+new" in staged

    def test_new_and_deleted_files_use_dev_null(self, pg, repo, write, tmp_path, commit_file):
        commit_file("gone.txt", "bye\n", "first")
        (tmp_path / "gone.txt").unlink()
        write("added.txt", "hi\n")
        pg("add", "added.txt")

        unstaged = pg("diff")
        staged = pg("diff", "--staged")

        assert "deleted file" in unstaged and "+++ /dev/null" in unstaged
        assert "new file" in staged and "--- /dev/null" in staged

    def test_a_missing_final_newline_is_flagged(self, pg, repo, write, commit_file):
        commit_file("a.txt", "line\n", "first")
        write("a.txt", "line")

        assert "\\ No newline at end of file" in pg("diff")

    def test_binary_files_are_not_diffed_line_by_line(self, pg, repo, write, commit_file):
        commit_file("image.bin", "x", "first")
        write("image.bin", b"\x00\x01\x02")

        assert "Binary files differ" in pg("diff")

    def test_untracked_files_do_not_appear_in_a_diff(self, pg, repo, write, commit_file):
        commit_file("a.txt", "a\n", "first")
        write("untracked.txt", "u\n")

        assert pg("diff") == ""


class TestBranches:
    def test_branches_are_listed_with_the_current_one_marked(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "feature")

        assert pg("branch").splitlines() == ["  feature", "* main"]

    def test_a_new_branch_starts_at_head_or_at_a_given_commit(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        first = repo.head_commit()
        commit_file("b.txt", "b", "two")

        pg("branch", "here")
        pg("branch", "back", "HEAD~1")

        assert repo.read_ref("refs/heads/here") == repo.head_commit()
        assert repo.read_ref("refs/heads/back") == first

    def test_a_branch_cannot_be_created_before_the_first_commit(self, pg, repo):
        assert "before the first commit" in pg("branch", "feature", expect=1)

    @pytest.mark.parametrize("name", ["has space", "-dash", "a..b", "ends.lock", "a//b", "x~1"])
    def test_invalid_branch_names_are_rejected(self, repo, commit_file, name):
        commit_file("a.txt", "a", "one")

        with pytest.raises(PygitError, match="not a valid branch name"):
            commands.branch(repo, name, None, delete=False)

    def test_a_duplicate_branch_is_rejected(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "feature")

        assert "already exists" in pg("branch", "feature", expect=1)

    def test_a_branch_can_be_deleted_unless_it_is_checked_out(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "feature")

        assert "cannot delete" in pg("branch", "-d", "main", expect=1)
        pg("branch", "-d", "feature")
        assert pg("branch").splitlines() == ["* main"]


class TestCheckout:
    def test_switching_branches_swaps_the_files_in_the_working_folder(
        self, pg, repo, write, tmp_path, commit_file
    ):
        commit_file("shared.txt", "base", "base")
        pg("checkout", "-b", "feature")
        commit_file("feature.txt", "only on feature", "feature work")
        write("shared.txt", "changed on feature")
        pg("add", "shared.txt")
        pg("commit", "-m", "edit shared")

        pg("checkout", "main")

        assert not (tmp_path / "feature.txt").exists()
        assert (tmp_path / "shared.txt").read_text() == "base"

        pg("checkout", "feature")

        assert (tmp_path / "feature.txt").read_text() == "only on feature"
        assert (tmp_path / "shared.txt").read_text() == "changed on feature"

    def test_checkout_updates_head_and_reports_it(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")
        pg("branch", "other")

        assert pg("checkout", "other").strip() == "Switched to branch 'other'"
        assert repo.current_branch() == "other"
        assert pg("checkout", "other").strip() == "Already on 'other'"

    def test_checkout_b_creates_and_switches(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "Switched to a new branch 'topic'" in pg("checkout", "-b", "topic")
        assert repo.current_branch() == "topic"

    def test_checking_out_a_commit_detaches_head(self, pg, repo, tmp_path, commit_file):
        commit_file("a.txt", "v1", "one")
        first = repo.head_commit()
        commit_file("a.txt", "v2", "two")

        output = pg("checkout", first[:7])

        assert output.startswith(f"HEAD is now at {first[:7]} one")
        assert repo.current_branch() is None
        assert (tmp_path / "a.txt").read_text() == "v1"
        assert "HEAD detached at" in pg("status")
        assert "(HEAD detached at" in pg("branch")

    def test_commits_made_on_a_detached_head_move_head_only(self, pg, repo, commit_file):
        commit_file("a.txt", "v1", "one")
        pg("checkout", repo.head_commit())
        commit_file("b.txt", "b", "two")

        assert repo.read_ref("refs/heads/main") != repo.head_commit()

    def test_checkout_refuses_to_lose_uncommitted_changes(self, pg, repo, write, commit_file):
        commit_file("a.txt", "v1", "one")
        pg("branch", "other")
        write("a.txt", "unsaved work")

        assert "uncommitted changes" in pg("checkout", "other", expect=1)
        assert repo.current_branch() == "main"

    def test_checkout_refuses_to_overwrite_an_untracked_file(self, pg, repo, write, commit_file):
        commit_file("base.txt", "base", "base")
        pg("checkout", "-b", "feature")
        commit_file("clash.txt", "tracked on feature", "add clash")
        pg("checkout", "main")
        write("clash.txt", "my untracked file")

        assert "would be overwritten" in pg("checkout", "feature", expect=1)

    def test_folders_left_empty_are_removed(self, pg, repo, tmp_path, commit_file):
        commit_file("base.txt", "base", "base")
        pg("checkout", "-b", "feature")
        commit_file("deep/er/file.txt", "x", "nested")

        pg("checkout", "main")

        assert not (tmp_path / "deep").exists()

    def test_a_file_can_become_a_folder_between_commits(self, pg, repo, write, tmp_path):
        write("thing", "a plain file")
        pg("add", "thing")
        pg("commit", "-m", "file")
        first = repo.head_commit()
        (tmp_path / "thing").unlink()
        write("thing/inside.txt", "now a folder")
        pg("add", "thing")
        pg("commit", "-m", "folder")

        pg("checkout", first)
        assert (tmp_path / "thing").read_text() == "a plain file"
        pg("checkout", "main")
        assert (tmp_path / "thing" / "inside.txt").read_text() == "now a folder"

    def test_a_folder_can_become_a_file_between_commits(self, pg, repo, write, tmp_path):
        write("thing/inside.txt", "a folder")
        pg("add", "thing")
        pg("commit", "-m", "folder")
        first = repo.head_commit()
        (tmp_path / "thing" / "inside.txt").unlink()
        (tmp_path / "thing").rmdir()
        write("thing", "now a plain file")
        pg("add", "thing")
        pg("commit", "-m", "file")

        pg("checkout", first)
        assert (tmp_path / "thing" / "inside.txt").read_text() == "a folder"
        pg("checkout", "main")
        assert (tmp_path / "thing").read_text() == "now a plain file"

    def test_unknown_targets_fail(self, pg, repo, commit_file):
        commit_file("a.txt", "a", "one")

        assert "unknown revision" in pg("checkout", "nope", expect=1)

    @pytest.mark.skipif(os.name == "nt", reason="Windows has no executable bit")
    def test_the_executable_bit_survives_a_commit_and_checkout(self, pg, repo, write, tmp_path):
        write("run.sh", "#!/bin/sh\n")
        (tmp_path / "run.sh").chmod(0o755)
        pg("add", "run.sh")
        pg("commit", "-m", "script")
        pg("checkout", "-b", "other")
        (tmp_path / "run.sh").chmod(0o644)
        pg("add", "run.sh")
        pg("commit", "-m", "not executable")

        pg("checkout", "main")

        assert os.access(tmp_path / "run.sh", os.X_OK)
