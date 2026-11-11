"""Tests for agentcontext.caches: the file history cache, the AI authorship cache and the
submodules CSV, and how each is read, written, merged and refused.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_caches -v
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentcontext import caches as _facts, heuristics, planning_schema
from agentcontext.caches import FileHistory, FileHistoryCache, FileHistoryKey, write_submodules

def _key(repo="owner/repo", head="a" * 40, mechanism="skills", artifact="x.md"):
    return FileHistoryKey(repo, head, mechanism, artifact)


def _facts_for(n="1"):
    return FileHistory("2026-01-02T03:04:05Z", n, "b" * 40, "c" * 40)


HEAD_A = "a" * 40

HEAD_B = "b" * 40

KEY = _facts.AiAuthorshipKey("o/r", HEAD_A, "CLAUDE.md")

MEASURED = _facts.AiAuthorship("TRUE", "Claude", "3", "Claude,Copilot")

NONE_FOUND = _facts.AiAuthorship("FALSE", "", "0", "")


class _CacheCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)


class TestColumns(unittest.TestCase):
    def test_the_key_is_the_four_columns_the_plan_names(self):
        self.assertEqual(_facts.FILE_HISTORY_KEY_COLUMNS,
                         ("repo_name", "head_sha", "mechanism", "artifact"))

    def test_the_git_columns_are_what_git_log_returns(self):
        self.assertEqual(_facts.HISTORY_COLUMNS,
                         ("created_at", "#commits", "first_commit_sha", "last_commit_sha"))

    def test_the_columns_are_the_heuristics_list_rather_than_a_second_copy(self):
        """Two lists of the same four names could differ. The output CSVs publish the names
        and the cache stores them, and a column added to one list and not the other would reach
        the join as a blank."""
        self.assertIs(_facts.HISTORY_COLUMNS, heuristics.HISTORY_COLUMNS)

    def test_the_file_header_is_the_key_then_the_history(self):
        self.assertEqual(_facts.FILE_HISTORY_COLUMNS, _facts.FILE_HISTORY_KEY_COLUMNS + _facts.HISTORY_COLUMNS)

    def test_the_commit_count_keeps_its_published_column_name(self):
        """`#commits` is not a Python identifier. The field is therefore `commits`, and the
        column name is written down in one place rather than at every use."""
        row = _facts_for("7").as_row()
        self.assertEqual(row["#commits"], "7")
        self.assertNotIn("commits", row)


class TestFileHistory(unittest.TestCase):
    def test_a_row_round_trips(self):
        facts = _facts_for("3")
        self.assertEqual(FileHistory.from_row(facts.as_row()), facts)

    def test_a_missing_column_reads_as_blank_rather_than_none(self):
        self.assertEqual(FileHistory.from_row({}), FileHistory("", "", "", ""))

    def test_a_none_reads_as_blank(self):
        """csv.DictReader gives None for a short row, and None would be written back as
        the string 'None'."""
        self.assertEqual(FileHistory.from_row({"created_at": None}).created_at, "")

    def test_all_four_git_values_present_is_complete(self):
        self.assertTrue(_facts_for().is_complete())

    def test_one_missing_is_not_complete(self):
        self.assertFalse(FileHistory("2026-01-01T00:00:00Z", "1", "b" * 40, "").is_complete())

    def test_a_path_with_no_history_is_not_complete_either(self):
        """A row with `created_at` blank and `#commits` of 0 is a clone that succeeded over
        a path that git has no history for. A refresh queries such a row again, and it
        counts as incomplete."""
        self.assertFalse(FileHistory("", "0", "", "").is_complete())


class TestRecount(unittest.TestCase):
    """`recount` sets `#commits` from the path histories of the per-repository commit records, by
    repository, collected commit and path, whatever mechanism recorded the row."""

    def test_a_measured_row_takes_the_count_and_a_blank_one_keeps_its_blank(self):
        cache = FileHistoryCache()
        renamed = FileHistoryKey("o/r", "a" * 40, "", "CLAUDE.md")
        detected = FileHistoryKey("o/r", "a" * 40, "context_file", "CLAUDE.md")
        blank = FileHistoryKey("o/r", "a" * 40, "", "AGENTS.md")
        elsewhere = FileHistoryKey("o/r", "b" * 40, "", "CLAUDE.md")
        cache.put(renamed, FileHistory("2021-01-01T00:00:00Z", "2", "c1", "c3"))
        cache.put(detected, FileHistory("2021-01-01T00:00:00Z", "2", "c1", "c3"))
        cache.put(blank, FileHistory())
        cache.put(elsewhere, FileHistory("2021-01-01T00:00:00Z", "2", "c1", "c3"))
        changed = cache.recount({("o/r", "a" * 40, "CLAUDE.md"): 3,
                                 ("o/r", "a" * 40, "AGENTS.md"): 4})
        self.assertEqual(changed, 2)
        self.assertEqual([cache.get(k).commits for k in (renamed, detected, blank, elsewhere)],
                         ["3", "3", "", "2"])
        self.assertEqual(cache.get(renamed).first_commit_sha, "c1")
        self.assertEqual(cache.recount({("o/r", "a" * 40, "CLAUDE.md"): 3}), 0)


class TestRender(_CacheCase):
    def test_an_empty_cache_renders_the_header_alone(self):
        self.assertEqual(FileHistoryCache().render(),
                         "repo_name,head_sha,mechanism,artifact,created_at,#commits,"
                         "first_commit_sha,last_commit_sha\n")

    def test_rows_are_sorted_by_key_whatever_order_they_arrived_in(self):
        forwards, backwards = FileHistoryCache(), FileHistoryCache()
        keys = [_key(repo="z/z", artifact="b.md"), _key(repo="a/a", artifact="a.md"),
                _key(repo="a/a", artifact="b.md")]
        for k in keys:
            forwards.put(k, _facts_for())
        for k in reversed(keys):
            backwards.put(k, _facts_for())
        self.assertEqual(forwards.render(), backwards.render())
        names = [line.split(",")[0] for line in forwards.render().splitlines()[1:]]
        self.assertEqual(names, ["a/a", "a/a", "z/z"])

    def test_the_mechanism_separates_two_rows_for_one_path(self):
        """A (repository, path) pair can be published under two mechanisms. Without the
        mechanism in the key, the row of one would overwrite the row of the other."""
        cache = FileHistoryCache()
        cache.put(_key(mechanism="rules"), _facts_for("1"))
        cache.put(_key(mechanism="context_file"), _facts_for("2"))
        self.assertEqual(len(cache), 2)

    def test_lines_end_with_a_bare_newline(self):
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        self.assertNotIn("\r", cache.render())
        self.assertTrue(cache.render().endswith("\n"))

    def test_a_blank_row_renders_as_four_empty_fields(self):
        cache = FileHistoryCache()
        cache.put(_key(), FileHistory())
        self.assertEqual(cache.render().splitlines()[1],
                         f"owner/repo,{'a' * 40},skills,x.md,,,,")


class TestForArtifact(_CacheCase):
    def test_a_path_is_found_under_a_mechanism_it_is_no_longer_published_as(self):
        """A heuristic change moves rows between mechanisms and leaves their history where
        it is, and looking up a row by its new mechanism alone would clone the
        repository."""
        cache = FileHistoryCache()
        cache.put(_key(mechanism="commands", artifact=".claude/commands/x.md"),
                  _facts_for("4"))
        self.assertEqual(
            cache.for_artifact("owner/repo", "a" * 40, ".claude/commands/x.md"),
            _facts_for("4"))

    def test_two_mechanisms_that_agree_yield_one_result(self):
        cache = FileHistoryCache()
        for mechanism in ("rules", "context_file"):
            cache.put(_key(mechanism=mechanism), _facts_for("4"))
        self.assertEqual(cache.for_artifact("owner/repo", "a" * 40, "x.md"),
                         _facts_for("4"))

    def test_two_mechanisms_that_disagree_yield_no_result(self):
        """When `hooks` and `settings` publish different `#commits` for one path, there is
        no single value, which is why `mechanism` is in the key."""
        cache = FileHistoryCache()
        cache.put(_key(mechanism="hooks"), _facts_for("12"))
        cache.put(_key(mechanism="settings"), _facts_for("11"))
        self.assertIsNone(cache.for_artifact("owner/repo", "a" * 40, "x.md"))

    def test_a_path_that_was_never_recorded_is_not_found(self):
        self.assertIsNone(FileHistoryCache().for_artifact("o/r", "a" * 40, "x.md"))

    def test_a_row_added_after_the_first_lookup_is_found(self):
        """The lookup builds an index. A row put after that has to invalidate it."""
        cache = FileHistoryCache()
        self.assertIsNone(cache.for_artifact("owner/repo", "a" * 40, "x.md"))
        cache.put(_key(), _facts_for("4"))
        self.assertEqual(cache.for_artifact("owner/repo", "a" * 40, "x.md"),
                         _facts_for("4"))

    def test_a_row_of_another_commit_is_not_returned(self):
        cache = FileHistoryCache()
        cache.put(_key(head="a" * 40), _facts_for())
        self.assertIsNone(cache.for_artifact("owner/repo", "b" * 40, "x.md"))


class TestLoad(_CacheCase):
    def test_a_file_that_is_not_there_loads_as_an_empty_cache(self):
        cache = FileHistoryCache.load(self.dir / "absent.csv")
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.path, self.dir / "absent.csv")

    def test_a_saved_cache_loads_back_identical(self):
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for("5"))
        cache.put(_key(repo="b/b", mechanism="mcp", artifact=".mcp.json"), FileHistory())
        path = cache.save(self.dir / "file_history.csv")
        reloaded = FileHistoryCache.load(path)
        self.assertEqual(reloaded.render(), cache.render())
        self.assertEqual(len(reloaded), 2)

    def test_a_blank_row_survives_as_a_row_rather_than_disappearing(self):
        """A recorded blank records that the history could not be read, and an absent key
        records nothing. Reading the blank as an absent key would turn a known gap into a
        clone."""
        cache = FileHistoryCache()
        key = _key()
        cache.put(key, FileHistory())
        reloaded = FileHistoryCache.load(cache.save(self.dir / "file_history.csv"))
        self.assertIn(key, reloaded)
        self.assertEqual(reloaded.get(key), FileHistory())
        self.assertEqual(reloaded.incomplete(), [key])

    def test_a_key_that_was_never_recorded_is_none(self):
        self.assertIsNone(FileHistoryCache().get(_key()))

    def test_a_wrong_header_is_refused(self):
        path = self.dir / "file_history.csv"
        path.write_text("repo_name,head_sha,mechanism,artifact,created_at\nx,y,z,w,t\n",
                        encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            FileHistoryCache.load(path)
        self.assertIn("names the columns", str(caught.exception))
        self.assertIn("not the expected", str(caught.exception))

    def test_a_cache_that_still_has_the_content_columns_is_refused(self):
        """A header with content columns that the cache does not have. The four history
        columns have the same names. The file would load, and the next save would drop
        `is_empty` and `language` from a tracked file without a message. A column removed from
        `HISTORY_COLUMNS` requires writing the file again in the same commit rather than
        migrating it in place."""
        path = self.dir / "file_history.csv"
        path.write_text(
            "repo_name,head_sha,mechanism,artifact,created_at,#commits,"
            "first_commit_sha,last_commit_sha,is_empty,language\n"
            f"o/r,{'a' * 40},skills,x.md,2026-01-01T00:00:00Z,1,{'b' * 40},{'c' * 40},"
            "FALSE,English\n",
            encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            FileHistoryCache.load(path)
        self.assertIn("is_empty", str(caught.exception))

    def test_a_duplicate_key_is_refused(self):
        path = self.dir / "file_history.csv"
        header = ",".join(_facts.FILE_HISTORY_COLUMNS)
        row = "o/r,abc,skills,x.md,t,1,aa,bb"
        path.write_text(f"{header}\n{row}\n{row}\n", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            FileHistoryCache.load(path)
        self.assertRegex(str(caught.exception), r"has the key .* twice")


class TestSave(_CacheCase):
    def test_saving_creates_the_parent_directory(self):
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        path = cache.save(self.dir / "nested" / "file_history.csv")
        self.assertTrue(path.is_file())

    def test_saving_leaves_no_temporary_file_behind(self):
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        cache.save(self.dir / "file_history.csv")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["file_history.csv"])

    def _saved_then_failed(self, failure):
        """Save one cache, then save a second one to the same path under `failure`, and
        return the path and the bytes that the first save left there."""
        path = self.dir / "file_history.csv"
        good = FileHistoryCache()
        good.put(_key(), _facts_for())
        good.save(path)
        before = path.read_bytes()
        second = FileHistoryCache()
        for repo in ("b/b", "c/c", "d/d"):
            second.put(_key(repo=repo), _facts_for())
        with failure, self.assertRaises(OSError) as caught:
            second.save(path)
        self.assertEqual(caught.exception.errno, 28)
        return path, before

    def test_a_disk_that_fills_while_the_cache_is_written_leaves_the_previous_file_in_place(self):
        """The file history step reads the cache to decide what to clone, and a half-written
        cache would therefore cause a network run that nobody requested. The write fails after
        the temporary file exists, and a save that wrote the target in place would fail the
        test."""
        real_fdopen = os.fdopen

        def half_then_full(fd, *args, **kwargs):
            f = real_fdopen(fd, *args, **kwargs)
            real_write = f.write

            def write(text):
                real_write(text[:len(text) // 2])
                f.flush()
                raise OSError(28, "No space left on device")
            f.write = write
            return f

        path, before = self._saved_then_failed(mock.patch("os.fdopen", half_then_full))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["file_history.csv"])

    def test_a_failed_rename_leaves_the_previous_file_and_no_temporary_one(self):
        path, before = self._saved_then_failed(
            mock.patch("os.replace", side_effect=OSError(28, "No space left on device")))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["file_history.csv"])

    def test_a_temporary_file_a_killed_save_left_is_removed_by_the_next(self):
        """A SIGKILL between the write and the rename leaves the temporary file behind,
        beside a tracked file, where the next `git status` reports it. The save leaves the
        temporary file of another target in place."""
        stray = self.dir / "file_history.csv.k1ll3d00.tmp"
        stray.write_text("half")
        other = self.dir / "ai_authorship.csv.k1ll3d00.tmp"
        other.write_text("half")
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        cache.save(self.dir / "file_history.csv")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["ai_authorship.csv.k1ll3d00.tmp", "file_history.csv"])

    def test_a_save_leaves_the_temporary_file_of_a_longer_name_in_place(self):
        """The per-repository commit records of `o/a` and `o/a.json` are `o§a.json` and
        `o§a.json.json`, which two workers can write at the same time. A save of the first could
        remove the temporary file of the second, and the rename of the second would then fail,
        and its worker would record a `collector_error`."""
        other = self.dir / "o§a.json.json.0a1b2c3d.tmp"
        other.write_text("the record of o/a.json, being written")
        stale = self.dir / "o§a.json.0a1b2c3d.tmp"
        stale.write_text("half")
        _facts.replace_file(self.dir / "o§a.json", "{}\n")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         ["o§a.json", "o§a.json.json.0a1b2c3d.tmp"])

    def test_the_data_and_the_rename_are_synced_to_disk(self):
        """A rename that reaches the disk before the data leaves an empty or truncated file
        under the target's name after a power loss, such as the per-repository commit record
        that the commit reclassification then cannot parse."""
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            info = os.fstat(fd)
            events.append(("sync", stat.S_ISDIR(info.st_mode), info.st_ino, info.st_size))
            real_fsync(fd)

        def replace(source, destination):
            events.append(("rename",))
            real_replace(source, destination)

        target = self.dir / "o§a.json"
        text = '{"outcome": "history_read"}\n'
        with mock.patch("os.fsync", fsync), mock.patch("os.replace", replace):
            _facts.replace_file(target, text)
        written = target.stat()
        self.assertEqual(events, [("sync", False, written.st_ino, len(text.encode("utf-8"))),
                                  ("rename",),
                                  ("sync", True, self.dir.stat().st_ino, mock.ANY)])

    def test_a_new_cache_is_not_dirty_and_a_put_makes_it_so(self):
        """Saving on a count change alone would miss a row replaced rather than added,
        which is every row that a refresh measures again."""
        cache = FileHistoryCache()
        self.assertFalse(cache.dirty)
        cache.put(_key(), _facts_for("1"))
        self.assertTrue(cache.dirty)
        cache.save(self.dir / "file_history.csv")
        self.assertFalse(cache.dirty)
        cache.put(_key(), _facts_for("2"))
        self.assertEqual(len(cache), 1)
        self.assertTrue(cache.dirty)

    def test_saving_twice_writes_the_same_bytes(self):
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        path = self.dir / "file_history.csv"
        cache.save(path)
        first = path.read_bytes()
        cache.save(path)
        self.assertEqual(path.read_bytes(), first)

    def test_saving_a_cache_built_in_memory_needs_a_path(self):
        """Falling back to FILE_HISTORY_PATH here would replace the tracked cache with
        whatever the caller had put in it."""
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        with self.assertRaises(ValueError) as caught:
            cache.save()
        self.assertIn("needs a path", str(caught.exception))

    def test_save_records_where_it_wrote(self):
        cache = FileHistoryCache()
        cache.put(_key(), _facts_for())
        path = cache.save(self.dir / "file_history.csv")
        self.assertEqual(cache.path, path)
        cache.put(_key(repo="b/b"), _facts_for())
        cache.save()
        self.assertEqual(len(FileHistoryCache.load(path)), 2)


class TestSyncTree(unittest.TestCase):
    """`sync_tree` writes a repository's collection to disk before its line declares it
    finished, on a NAS where Synology DSM writes into the same directories."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tree = self.tmp / "owner§repo"
        for rel in (".claude/CLAUDE.md", "docs/a.md", ".github/@eaDir/x.png/SYNOFILE_THUMB_M.jpg"):
            (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / rel).write_text("x\n")

    def test_a_file_removed_after_the_listing_is_left_alone(self):
        """A file can be gone between the listing and its sync, because DSM removes its own
        files at any time, and a step can remove a file that it wrote."""
        walk = os.walk

        def removing(top, *args, **kwargs):
            for root, dirs, files in walk(top, *args, **kwargs):
                if "a.md" in files:
                    (Path(root) / "a.md").unlink()
                yield root, dirs, files

        with mock.patch.object(_facts.os, "walk", removing):
            _facts.sync_tree(self.tree)
        self.assertFalse((self.tree / "docs/a.md").exists())

    def test_the_directories_that_dsm_writes_are_not_synced(self):
        synced, real = set(), os.fsync

        def recorded(fd):
            synced.add((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return real(fd)

        with mock.patch.object(os, "fsync", recorded):
            _facts.sync_tree(self.tree)
        inode = lambda rel: ((self.tree / rel).stat().st_dev, (self.tree / rel).stat().st_ino)
        self.assertIn(inode(".claude/CLAUDE.md"), synced)
        self.assertNotIn(inode(".github/@eaDir/x.png/SYNOFILE_THUMB_M.jpg"), synced)


class TestTheCommittedCache(unittest.TestCase):
    """The file that the step reads. Skipped where the file does not exist."""

    @classmethod
    def setUpClass(cls):
        if not _facts.FILE_HISTORY_PATH.is_file():
            raise unittest.SkipTest(f"{_facts.FILE_HISTORY_PATH.name} does not exist")
        cls.cache = FileHistoryCache.load(_facts.FILE_HISTORY_PATH)

    def test_it_returns_the_history_published_for_every_current_row(self):
        import csv
        from agentcontext import STAGE_DIR
        checked = 0
        differences = []
        for mechanism, (filename, column) in heuristics.MECHANISM_CSVS.items():
            with (STAGE_DIR / 'output' / filename).open(newline='') as source:
                for row in csv.DictReader(source):
                    key = _facts.FileHistoryKey(row['repo_name'], row['head_sha'],
                                               mechanism, row[column])
                    facts = self.cache.get(key) or self.cache.for_artifact(
                        row['repo_name'], row['head_sha'], row[column])
                    checked += 1
                    if facts is None or facts.as_row() != {
                            name: row[name] for name in _facts.HISTORY_COLUMNS}:
                        differences.append(key)
        self.assertGreater(checked, 0)
        self.assertEqual(differences, [])

    def test_every_head_sha_is_a_full_commit_hash(self):
        for key in self.cache.keys():
            # \A and \Z, not ^ and $: `$` matches before a trailing newline.
            self.assertRegex(key.head_sha, r"\A[0-9a-f]{40}\Z")

    def test_every_mechanism_is_one_the_heuristics_names_or_blank(self):
        """A blank mechanism comes from the cloning step, which measures before a mechanism is
        assigned, and the CSV that a path is published in does not change its history. The
        other rows record the mechanism of the CSV that they were read from, or
        `planning_document` for a planning document, which belongs to no mechanism."""
        known = set(heuristics.MECHANISM_CSVS) | {planning_schema.HISTORY_KEY, ""}
        self.assertEqual({key.mechanism for key in self.cache.keys()} - known, set())

    def test_it_is_written_the_way_it_would_be_rendered(self):
        """Compared as bytes, because `read_text` translates CRLF to LF, and a cache with the
        wrong line endings would then compare equal to a cache with the right ones."""
        self.assertEqual(_facts.FILE_HISTORY_PATH.read_bytes(),
                         self.cache.render().encode("utf-8"))


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ai_authorship.csv"


class TestAuthorshipColumns(unittest.TestCase):
    def test_the_key_names_the_commit_and_not_the_mechanism(self):
        self.assertEqual(_facts.AI_AUTHORSHIP_KEY_COLUMNS, ("repo_name", "head_sha", "artifact"))

    def test_the_values_are_the_heuristics_ai_columns(self):
        self.assertEqual(_facts.AI_COLUMNS, heuristics.AI_COLUMNS)
        self.assertEqual(_facts.AI_AUTHORSHIP_COLUMNS, _facts.AI_AUTHORSHIP_KEY_COLUMNS + heuristics.AI_COLUMNS)

    def test_a_row_round_trips_through_the_published_column_names(self):
        row = MEASURED.as_row()
        self.assertEqual(list(row), list(heuristics.AI_COLUMNS))
        self.assertEqual(_facts.AiAuthorship.from_row(row), MEASURED)

    def test_a_missing_column_reads_as_blank(self):
        self.assertEqual(_facts.AiAuthorship.from_row({}), _facts.AiAuthorship())


class TestCompleteness(unittest.TestCase):
    def test_a_file_no_tool_touched_is_complete(self):
        self.assertTrue(NONE_FOUND.is_complete())

    def test_a_row_with_no_response_from_the_history_query_is_not_complete(self):
        self.assertFalse(_facts.AiAuthorship().is_complete())
        self.assertFalse(_facts.AiAuthorship(first_commit_ai_created="FALSE").is_complete())


class TestAuthorshipLoad(_TempCase):
    def test_a_missing_file_is_an_empty_cache_that_remembers_its_path(self):
        cache = _facts.AiAuthorshipCache.load(self.path)
        self.assertEqual(len(cache), 0)
        self.assertEqual(cache.path, self.path)

    def test_the_header_has_to_be_the_columns(self):
        self.path.write_text("repo_name,artifact,#ai_commits\no/r,CLAUDE.md,3\n")
        with self.assertRaises(ValueError):
            _facts.AiAuthorshipCache.load(self.path)

    def test_a_duplicate_key_is_refused(self):
        line = f"o/r,{HEAD_A},CLAUDE.md,TRUE,Claude,3,Claude\n"
        self.path.write_text(",".join(_facts.AI_AUTHORSHIP_COLUMNS) + "\n" + line + line)
        with self.assertRaises(ValueError):
            _facts.AiAuthorshipCache.load(self.path)

    def test_a_saved_cache_loads_back_equal(self):
        cache = _facts.AiAuthorshipCache(path=self.path)
        cache.put(KEY, MEASURED)
        cache.put(_facts.AiAuthorshipKey("o/r", HEAD_B, "CLAUDE.md"), NONE_FOUND)
        cache.save()
        loaded = _facts.AiAuthorshipCache.load(self.path)
        self.assertEqual(loaded.get(KEY), MEASURED)
        self.assertEqual(loaded.get(_facts.AiAuthorshipKey("o/r", HEAD_B, "CLAUDE.md")),
                         NONE_FOUND)


class TestLookup(_TempCase):
    def test_a_row_is_returned_only_for_its_own_commit(self):
        cache = _facts.AiAuthorshipCache()
        cache.put(KEY, MEASURED)
        self.assertIsNone(cache.get(_facts.AiAuthorshipKey("o/r", HEAD_B, "CLAUDE.md")))
        self.assertIn(KEY, cache)

    def test_incomplete_lists_the_rows_with_no_response_from_the_history_query(self):
        cache = _facts.AiAuthorshipCache()
        cache.put(KEY, MEASURED)
        blank = _facts.AiAuthorshipKey("o/r", HEAD_A, "AGENTS.md")
        cache.put(blank, _facts.AiAuthorship())
        self.assertEqual(cache.incomplete(), [blank])


class TestWriting(_TempCase):
    def test_rows_are_written_sorted_by_key_whatever_the_put_order(self):
        first, second = _facts.AiAuthorshipCache(), _facts.AiAuthorshipCache()
        later = _facts.AiAuthorshipKey("z/z", HEAD_A, "CLAUDE.md")
        for key in (KEY, later):
            first.put(key, MEASURED)
        for key in (later, KEY):
            second.put(key, MEASURED)
        self.assertEqual(first.render(), second.render())
        self.assertTrue(first.render().startswith(",".join(_facts.AI_AUTHORSHIP_COLUMNS) + "\n"))

    def test_the_file_uses_lf_and_the_published_column_names(self):
        cache = _facts.AiAuthorshipCache()
        cache.put(KEY, MEASURED)
        text = cache.render()
        self.assertNotIn("\r", text)
        self.assertIn("#ai_commits", text.splitlines()[0])

    def test_save_replaces_the_file_and_leaves_no_temporary_behind(self):
        self.path.write_text("stale")
        cache = _facts.AiAuthorshipCache(path=self.path)
        cache.put(KEY, MEASURED)
        cache.save()
        self.assertEqual(self.path.read_text(), cache.render())
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["ai_authorship.csv"])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.assertFalse(cache.dirty)

    def test_a_cache_built_in_memory_needs_a_path_to_save(self):
        cache = _facts.AiAuthorshipCache()
        with self.assertRaises(ValueError):
            cache.save()

    def test_put_marks_the_cache_dirty(self):
        cache = _facts.AiAuthorshipCache.load(self.path)
        self.assertFalse(cache.dirty)
        cache.put(KEY, MEASURED)
        self.assertTrue(cache.dirty)


class TestWriteSubmodules(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "submodules.csv"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, *triples):
        return [{"repo_name": r, "path": p, "submodule_repo": s} for r, p, s in triples]

    def test_it_writes_a_header_and_sorted_rows(self):
        write_submodules(self.path, self._rows(("o/b", "x", ""), ("o/a", "y", "u/v")), ["o/a", "o/b"])
        self.assertEqual(self.path.read_text(),
                         "repo_name,path,submodule_repo\no/a,y,u/v\no/b,x,\n")

    def test_a_repository_in_this_run_replaces_what_it_had(self):
        write_submodules(self.path, self._rows(("o/a", "old", "")), ["o/a"])
        write_submodules(self.path, self._rows(("o/a", "new", "")), ["o/a"])
        self.assertEqual([line.split(",")[1] for line in
                          self.path.read_text().splitlines()[1:]], ["new"])

    def test_a_repository_this_run_did_not_reach_keeps_its_rows(self):
        write_submodules(self.path, self._rows(("o/a", "dep", "")), ["o/a"])
        write_submodules(self.path, self._rows(("o/b", "dep", "")), ["o/b"])
        self.assertEqual([line.split(",")[0] for line in
                          self.path.read_text().splitlines()[1:]], ["o/a", "o/b"])

    def test_a_repository_that_lost_its_submodule_loses_its_rows(self):
        write_submodules(self.path, self._rows(("o/a", "dep", "")), ["o/a"])
        write_submodules(self.path, [], ["o/a"])
        self.assertEqual(self.path.read_text(), "repo_name,path,submodule_repo\n")


if __name__ == "__main__":
    unittest.main()
