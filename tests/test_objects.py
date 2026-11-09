import zlib

import pytest

from pygit.errors import PygitError
from pygit.objects import (
    BLOB,
    EMPTY_TREE,
    MODE_FILE,
    MODE_TREE,
    TREE,
    Commit,
    ObjectStore,
    TreeEntry,
    decode_commit,
    decode_tree,
    encode_commit,
    encode_tree,
    flatten_tree,
    object_id,
    parse_ident,
    write_tree,
)


@pytest.fixture
def store(tmp_path) -> ObjectStore:
    return ObjectStore(tmp_path / "objects")


class TestObjectIds:
    def test_blob_ids_match_the_ones_real_git_gives(self):
        # Values from `git hash-object`.
        assert object_id(BLOB, b"hello world\n") == "3b18e512dba79e4c8300dd08aeb37f8e728b8dad"
        assert object_id(BLOB, b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"

    def test_the_empty_tree_has_gits_well_known_id(self):
        assert object_id(TREE, b"") == EMPTY_TREE


class TestObjectStore:
    def test_a_written_object_can_be_read_back(self, store):
        sha = store.write(BLOB, b"some content")

        assert store.read(sha) == (BLOB, b"some content")

    def test_objects_are_stored_compressed_under_their_id(self, store):
        sha = store.write(BLOB, b"hello world\n")

        path = store.directory / sha[:2] / sha[2:]
        assert zlib.decompress(path.read_bytes()) == b"blob 12\x00hello world\n"

    def test_writing_the_same_content_twice_gives_the_same_id(self, store):
        assert store.write(BLOB, b"same") == store.write(BLOB, b"same")

    def test_reading_a_missing_object_fails(self, store):
        with pytest.raises(PygitError, match="not found"):
            store.read("0" * 40)

    def test_a_corrupt_object_is_detected(self, store):
        sha = store.write(BLOB, b"data")
        path = store.directory / sha[:2] / sha[2:]
        path.write_bytes(zlib.compress(b"blob 99\x00data"))

        with pytest.raises(PygitError, match="corrupt"):
            store.read(sha)

    def test_an_abbreviation_finds_the_full_id(self, store):
        sha = store.write(BLOB, b"find me")

        assert store.find_by_prefix(sha[:6]) == [sha]
        assert store.find_by_prefix(sha[:3]) == []  # too short to be useful


class TestTrees:
    def test_entries_round_trip(self):
        entries = [TreeEntry(MODE_FILE, "a.txt", "ab" * 20), TreeEntry(MODE_TREE, "src", "cd" * 20)]

        assert decode_tree(encode_tree(entries)) == entries

    def test_a_folder_sorts_as_if_its_name_ended_with_a_slash(self):
        # "a.txt" < "a/" because "." (46) comes before "/" (47), though the folder is named "a".
        entries = [TreeEntry(MODE_TREE, "a", "11" * 20), TreeEntry(MODE_FILE, "a.txt", "22" * 20)]

        names = [entry.name for entry in decode_tree(encode_tree(entries))]

        assert names == ["a.txt", "a"]

    def test_files_are_nested_into_trees_and_flattened_back(self, store):
        files = {
            "README.md": (MODE_FILE, store.write(BLOB, b"readme")),
            "src/app.py": (MODE_FILE, store.write(BLOB, b"app")),
            "src/util/helpers.py": (MODE_FILE, store.write(BLOB, b"helpers")),
        }

        root = write_tree(store, files)

        assert flatten_tree(store, root) == files

    def test_the_same_files_always_give_the_same_tree(self, store):
        blob = store.write(BLOB, b"x")
        first = write_tree(store, {"a": (MODE_FILE, blob), "b/c": (MODE_FILE, blob)})
        second = write_tree(store, {"b/c": (MODE_FILE, blob), "a": (MODE_FILE, blob)})

        assert first == second

    def test_no_files_gives_the_empty_tree(self, store):
        assert write_tree(store, {}) == EMPTY_TREE


class TestCommits:
    def test_a_commit_round_trips(self):
        commit = Commit(
            tree="a" * 40,
            parents=["b" * 40],
            author="Ada <ada@example.com> 1700000000 +0100",
            committer="Ada <ada@example.com> 1700000000 +0100",
            message="Subject line\n\nMore detail.",
        )

        decoded = decode_commit(encode_commit(commit))

        assert decoded.tree == commit.tree
        assert decoded.parents == commit.parents
        assert decoded.message.strip() == commit.message
        assert decoded.subject == "Subject line"

    def test_the_encoding_matches_gits_layout(self):
        commit = Commit("a" * 40, [], "N <e@x> 1 +0000", "N <e@x> 1 +0000", "msg")

        assert encode_commit(commit) == (
            b"tree " + b"a" * 40 + b"\nauthor N <e@x> 1 +0000\ncommitter N <e@x> 1 +0000\n\nmsg\n"
        )

    def test_a_commit_without_a_tree_is_rejected(self):
        with pytest.raises(PygitError):
            decode_commit(b"author someone\n\nmessage")

    def test_the_identity_line_is_parsed(self):
        ident = parse_ident("Ada Lovelace <ada@example.com> 1700000000 -0500")

        assert (ident.name, ident.email, ident.timestamp, ident.zone) == (
            "Ada Lovelace",
            "ada@example.com",
            1700000000,
            "-0500",
        )

    def test_a_malformed_identity_is_rejected(self):
        with pytest.raises(PygitError):
            parse_ident("not an identity")
