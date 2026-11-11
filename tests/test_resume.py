"""Tests for agentcontext.resume: the list of finished repositories, one line per finished
repository, read back whole, partial and corrupt, and the record of the collection in progress.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_resume -v
"""

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentcontext import resume
from agentcontext.resume import append_finished, clone_needs_redo, read_finished
from agentcontext.steps.commit_reclassification import finished_copied


class TestFinished(unittest.TestCase):
    """The append and read primitives that the resume behavior is built on."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.finished = self.tmp / "list of finished repositories.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(read_finished(self.tmp / "nope.jsonl"), {})

    def test_a_torn_last_line_does_not_take_the_next_record_with_it(self):
        """A kill in the middle of a write leaves a line without its newline. Unless the next
        append writes a newline first, the next run's first record is written on that line, and
        the reader skips both."""
        append_finished(self.finished, "o/a", "row-a", None)
        torn = self.finished.read_bytes()[:-3]
        self.finished.write_bytes(torn)
        append_finished(self.finished, "o/b", "row-b", None)
        self.assertEqual(list(read_finished(self.finished)), ["o/b"])
        self.assertEqual(self.finished.read_bytes().count(b"\n"), 2)
        append_finished(self.finished, "o/c", "row-c", None)
        self.assertEqual(self.finished.read_bytes().count(b"\n"), 3, "no blank line added")
        self.assertEqual(list(read_finished(self.finished)), ["o/b", "o/c"])

    def test_each_line_is_synced_to_disk_before_the_append_returns(self):
        """The step removes or replaces a repository's files around its lines. A line that is
        only flushed to the operating system can be lost with a power loss, and the earlier line
        would then stay in force over files that it does not describe. The first line also syncs
        the directory, in which it created the list."""
        synced, real_fsync = [], os.fsync

        def fsync(fd):
            info = os.fstat(fd)
            synced.append((stat.S_ISDIR(info.st_mode), info.st_ino, info.st_size))
            real_fsync(fd)

        with mock.patch("os.fsync", fsync):
            append_finished(self.finished, "o/a", "row-a", None)
            first = self.finished.stat()
            append_finished(self.finished, "o/b", "row-b", None)
        second = self.finished.stat()
        self.assertEqual(synced, [(False, first.st_ino, first.st_size),
                                  (True, self.tmp.stat().st_ino, mock.ANY),
                                  (False, second.st_ino, second.st_size)])

    def test_append_then_read_round_trip(self):
        append_finished(self.finished, "o/a", "o/a,ts,main,TRUE", None)
        self.assertEqual(read_finished(self.finished),
                         {"o/a": ("o/a,ts,main,TRUE", None, [], [], False, None, None, False, False,
                                  None, None)})
        append_finished(self.finished, "o/b", "o/b,ts,main,TRUE", None, redo=True)
        self.assertTrue(read_finished(self.finished)["o/b"][4],
                        "a machine failure ended the reading of the commit history")

    def test_a_retry_that_began_is_read_back(self):
        append_finished(self.finished, "o/a", "o/a,ts,main,TRUE", None, redo=True, retrying=True)
        self.assertTrue(read_finished(self.finished)["o/a"][7])
        append_finished(self.finished, "o/a", "o/a,ts,main,TRUE", None)
        self.assertFalse(read_finished(self.finished)["o/a"][7])

    def test_a_head_that_git_did_not_return_is_read_back(self):
        append_finished(self.finished, "o/a", "o/a,ts,main,TRUE", None, head_no_response=True)
        self.assertTrue(read_finished(self.finished)["o/a"][8])
        append_finished(self.finished, "o/a", "o/a,ts,main,TRUE", None)
        self.assertFalse(read_finished(self.finished)["o/a"][8])

    def test_what_a_repository_was_collected_under_is_read_back(self):
        under = {"collector": "c" * 64, "heuristics": "h" * 64, "records": {"commit_history": True}}
        append_finished(self.finished, "o/a", "o/a,ts,main,TRUE", None, collected_under=under)
        self.assertEqual(read_finished(self.finished)["o/a"][6], under)

    def test_the_size_bound_a_clone_went_over_is_read_back(self):
        append_finished(self.finished, "o/b", "o/b,ts,,FALSE", "o/b,url,size_bound_exceeded",
                        size_bound=8 * 1024 ** 3)
        self.assertEqual(read_finished(self.finished)["o/b"][5], 8 * 1024 ** 3)

    def test_the_time_bound_and_the_collection_of_a_line_are_read_back(self):
        append_finished(self.finished, "o/b", "o/b,ts,,FALSE", "o/b,url,time_bound_exceeded",
                        time_bound=3600.0, collection="2026-10-18T120000Z-0a1b2c3d")
        line = read_finished(self.finished)["o/b"]
        self.assertEqual((line[9], line[10]), (3600.0, "2026-10-18T120000Z-0a1b2c3d"))

    def test_a_clone_that_the_time_bound_stopped_is_cloned_again_only_under_a_larger_one(self):
        """Two attempts past `--clone-timeout` recur under the same bound, and each would hold a
        worker for the whole bound again on every run."""
        stopped = "o/b,url,time_bound_exceeded"
        self.assertFalse(clone_needs_redo(stopped, None, 0, 3600, 3600))
        self.assertFalse(clone_needs_redo(stopped, None, 0, 3600, 1800))
        self.assertTrue(clone_needs_redo(stopped, None, 0, 3600, 7200))
        self.assertTrue(clone_needs_redo(stopped, None, 0, 3600, 0), "0 turns the bound off")
        self.assertTrue(clone_needs_redo(stopped, None, 0, None, 3600),
                        "a line without the bound is redone once")
        self.assertFalse(clone_needs_redo("o/b,url,size_bound_exceeded", 1024, 1024, None, 7200),
                         "a larger time bound does not redo a clone over the size bound")

    def test_a_time_bound_holds_for_the_rest_of_the_collection_that_recorded_it(self):
        """A new collection clones the repository again under the same bound, and so does a
        run of the collection in progress under a larger bound. The same holds for an ok line that
        kept the earlier collection after the time bound stopped its retry."""
        for fail_row in ("o/b,url,time_bound_exceeded", None):
            with self.subTest(fail_row=fail_row):
                self.assertFalse(clone_needs_redo(fail_row, None, 0, 3600, 3600, "C", "C"))
                self.assertFalse(clone_needs_redo(fail_row, None, 0, 3600, 1800, "C", "C"))
                self.assertTrue(clone_needs_redo(fail_row, None, 0, 3600, 7200, "C", "C"))
                self.assertTrue(clone_needs_redo(fail_row, None, 0, 3600, 3600, "C", "D"))
                self.assertTrue(clone_needs_redo(fail_row, None, 0, 3600, 3600, None, "D"),
                                "a line written before lines named their collection")
        self.assertFalse(clone_needs_redo(None, None, 0, None, 3600, "C", "D"),
                         "an ok line that records no time bound")
        self.assertFalse(resume.time_bound_holds(None, 3600, "C", "C"))

    def test_a_run_without_a_size_bound_clones_a_budget_exceeded_repository_again(self):
        """`--max-clone-mb 0` turns the bound off, which is larger than any size bound that a
        line records, and the function never selects an outcome other than `size_bound_exceeded`."""
        stopped = "o/b,url,size_bound_exceeded"
        self.assertTrue(clone_needs_redo(stopped, 8 * 1024 ** 3, 0))
        self.assertFalse(clone_needs_redo(stopped, 8 * 1024 ** 3, 4 * 1024 ** 3))
        self.assertFalse(clone_needs_redo("o/b,url,not_found_or_no_access", None, 0))
        self.assertFalse(clone_needs_redo(None, None, 0))

    def test_fail_row_preserved(self):
        append_finished(self.finished, "o/b", "o/b,ts,,FALSE", "o/b,url,clone_failed")
        self.assertEqual(read_finished(self.finished)["o/b"][1], "o/b,url,clone_failed")

    def test_multiple_appends_all_read_back(self):
        append_finished(self.finished, "o/a", "row-a", None)
        append_finished(self.finished, "o/b", "row-b", None)
        self.assertEqual(set(read_finished(self.finished)), {"o/a", "o/b"})

    def test_row_with_comma_and_quotes_survives(self):
        # Rows are stored as JSON strings, and CSV punctuation in them must survive the round
        # trip.
        row = 'o/a,2026-01-01T00:00:00Z,main,"quoted,value",TRUE'
        append_finished(self.finished, "o/a", row, None)
        self.assertEqual(read_finished(self.finished)["o/a"][0], row)

    def test_torn_last_line_is_skipped_not_fatal(self):
        # A crash mid-write can leave only the final line of an append-only file incomplete.
        append_finished(self.finished, "o/a", "row-a", None)
        with open(self.finished, "a", encoding="utf-8") as f:
            f.write('{"repo": "o/b", "row": "row-b"')  # no newline, unterminated
        done = read_finished(self.finished)
        self.assertEqual(set(done), {"o/a"})  # o/b is treated as not done and redone

    def test_blank_lines_ignored(self):
        append_finished(self.finished, "o/a", "row-a", None)
        with open(self.finished, "a", encoding="utf-8") as f:
            f.write("\n\n")
        self.assertEqual(set(read_finished(self.finished)), {"o/a"})

    def test_last_entry_wins_for_duplicate_repo(self):
        append_finished(self.finished, "o/a", "old-row", None)
        append_finished(self.finished, "o/a", "new-row", None)
        self.assertEqual(read_finished(self.finished)["o/a"][0], "new-row")


class TestTheCollectionInProgress(unittest.TestCase):
    """A run resumes the collection in progress over the same input list until a run closes it,
    and a run over another input list starts a new collection."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.finished = self.tmp / "finished_repositories.jsonl"

    def _open(self, repos, gone=frozenset()):
        with mock.patch.object(resume.logs, "info"):
            return resume.open_collection(self.finished, repos, gone)

    def _not_found(self, repo, collection):
        """A line of `collection` that records that GitHub refused the clone of `repo` as
        `not_found_or_no_access`."""
        append_finished(self.finished, repo, f"row-{repo}",
                        f"{repo},https://github.com/{repo},not_found_or_no_access",
                        collection=collection)

    def test_a_list_without_repositories_that_github_no_longer_serves_continues_it(self):
        """A labeling that ran again over the same sampling frame can find repositories gone,
        and the list that the selection writes then lacks them. The collection in progress
        continues over that list with what it collected and confirmed. o/b is a repository that
        the collection recorded as `not_found_or_no_access`, and o/c one that the labeling found
        gone before the collection reached it."""
        first = self._open(["o/a", "o/b", "o/c", "o/d"])
        first["taken_over"].add("o/a")
        resume.save_collection(self.finished, first)
        self._not_found("o/b", first["collection"])
        continued = self._open(["o/d", "o/a"], gone={"o/c"})
        self.assertEqual((continued["collection"], continued["taken_over"]),
                         (first["collection"], {"o/a"}))
        self.assertEqual(resume.collection_in_progress(self.finished, ["o/a", "o/d"]),
                         continued)
        self.assertEqual(self._open(["o/a", "o/d"])["collection"], first["collection"])

    def test_a_list_that_loses_a_served_repository_or_gains_one_starts_a_new_collection(self):
        """Only repositories that GitHub no longer serves leave the collection's list without a
        new collection. A list that lost a repository with another label, or that gained one, is
        another sample."""
        for repos, gone in ((["o/a"], set()), (["o/a", "o/e"], {"o/b"}),
                            (["o/a", "o/b", "o/e"], set())):
            with self.subTest(repos=repos):
                resume.forget_collections(self.finished)
                first = self._open(["o/a", "o/b"])
                self.assertNotEqual(self._open(repos, gone)["collection"], first["collection"])

    def test_two_shrinks_continue_the_collection_in_progress_and_the_one_that_ended(self):
        """Two labelings that each find a repository gone shrink the list twice. After the
        first shrink, the line of o/b still names the collection, and `reuse.remove_left`
        appends it again with `redo` when the step removes the files of o/b. The second shrink
        has to be read against the list of the record, which lacks o/b, and not against the
        lines, which would give the list of the collection before the first shrink."""
        from types import SimpleNamespace
        from agentcontext import reuse
        for ended in (False, True):
            with self.subTest(ended=ended):
                resume.forget_collections(self.finished)
                self.finished.unlink(missing_ok=True)
                repos = ["o/a", "o/b", "o/c", "o/d"]
                first = self._open(repos)
                for repo in repos:
                    append_finished(self.finished, repo, f"row-{repo}", None,
                                    collection=first["collection"])
                if ended:
                    resume.close_collection(self.finished)
                once = self._open(["o/a", "o/c", "o/d"], gone={"o/b"})
                self.assertEqual(once["collection"], first["collection"])
                config = SimpleNamespace(collected_dir=self.tmp / "collected", commits_dir=None,
                                         file_lists_dir=None, log_dir=self.tmp / "clone_logs",
                                         workflows_dir=None, imported_dir=None)
                with mock.patch.object(reuse.logs, "info"):
                    reuse.remove_left(config, ["o/a", "o/c", "o/d"],
                                      read_finished(self.finished), self.finished)
                self.assertTrue(read_finished(self.finished)["o/b"][4],
                                "remove_left appends the line of o/b again, marked to redo")
                self.assertEqual(read_finished(self.finished)["o/b"][10], first["collection"])
                if ended:
                    resume.close_collection(self.finished)
                twice = self._open(["o/a", "o/d"], gone={"o/b", "o/c"})
                self.assertEqual(twice["collection"], first["collection"])
                self.assertEqual(twice["repositories"], ["o/a", "o/d"])

    def test_a_record_without_its_list_continues_over_the_repositories_that_it_reached(self):
        """A record written before records listed their repositories gives the repositories
        that the collection wrote lines for or took over. A list that lacks a gone repository
        that the collection did not reach starts a new collection."""
        repos = ["o/a", "o/b", "o/c"]
        record = {"collection": "2026-11-04T120000Z-0123abcd",
                  "input_sha256": resume.list_digest(repos), "taken_over": set()}
        for gone, left, continues in ((set(), ["o/a", "o/c"], True),
                                      ({"o/c"}, ["o/a", "o/b"], False)):
            with self.subTest(left=left):
                self.finished.unlink(missing_ok=True)
                resume.save_collection(self.finished, record)
                append_finished(self.finished, "o/a", "row-o/a", None,
                                collection=record["collection"])
                self._not_found("o/b", record["collection"])
                opened = self._open(left, gone)
                self.assertEqual(opened["collection"] == record["collection"], continues)

    def test_the_end_of_a_collection_keeps_its_record_for_a_list_without_gone_repositories(
            self):
        """A labeling can also run again after the collection ended, and the cloning step then
        runs over the list without the repositories that it found gone. It continues the
        collection that ended rather than query every head again. So does a run over the same
        list, as after a change of the path heuristics alone. Only the mark of a replaced
        collection that `--from repository_cloning` makes starts a new collection over the
        list."""
        first = self._open(["o/a", "o/b"])
        first["taken_over"].add("o/a")
        resume.save_collection(self.finished, first)
        resume.close_collection(self.finished)
        self.assertFalse(resume.collection_path(self.finished).exists())
        self.assertTrue(resume.ended_path(self.finished).is_file())
        continued = self._open(["o/a"], gone={"o/b"})
        self.assertEqual((continued["collection"], continued["taken_over"]),
                         (first["collection"], {"o/a"}))
        self.assertFalse(resume.ended_path(self.finished).exists())
        resume.close_collection(self.finished)
        same = self._open(["o/a"])
        self.assertEqual((same["collection"], same["taken_over"]),
                         (first["collection"], {"o/a"}))
        resume.close_collection(self.finished)
        resume.forget_ended_collection(self.finished)
        new = self._open(["o/a"])
        self.assertNotEqual(new["collection"], first["collection"])
        self.assertEqual(new["taken_over"], set())
        self.assertFalse(resume.ended_path(self.finished).exists())

    def test_a_forgotten_collection_is_not_continued(self):
        """`--mark-complete` forgets the collections, because nothing shows that the tree that
        it records is theirs."""
        ended = self._open(["o/a", "o/b"])
        resume.close_collection(self.finished)
        resume.forget_collections(self.finished)
        self.assertFalse(resume.ended_path(self.finished).exists())
        self.assertNotEqual(self._open(["o/a"], gone={"o/b"})["collection"], ended["collection"])
        resume.forget_collections(self.finished)
        self.assertFalse(resume.collection_path(self.finished).exists())

    def test_a_run_resumes_the_collection_until_it_is_closed(self):
        first = self._open(["o/a", "o/b"])
        self.assertEqual(first["taken_over"], set())
        self.assertTrue((self.tmp / "finished_repositories.collection.json").is_file())
        first["taken_over"].add("o/a")
        resume.save_collection(self.finished, first)
        again = self._open(["o/b", "o/a"])
        self.assertEqual(again["collection"], first["collection"])
        self.assertEqual(again["taken_over"], {"o/a"})
        resume.close_collection(self.finished)
        resume.forget_ended_collection(self.finished)
        new = self._open(["o/a", "o/b"])
        self.assertNotEqual(new["collection"], first["collection"])
        self.assertEqual(new["taken_over"], set())

    def test_the_same_list_continues_the_collection_that_ended(self):
        """An image that changes the collector's code or the heuristics runs the cloning step
        over the list of the collection that ended. The collection continues rather than query
        every head again and collect again every repository with a head that moved."""
        first = self._open(["o/a", "o/b"])
        for repo in ("o/a", "o/b"):
            append_finished(self.finished, repo, f"row-{repo}", None,
                            collection=first["collection"])
        resume.close_collection(self.finished)
        again = self._open(["o/b", "o/a"])
        self.assertEqual(again["collection"], first["collection"])
        self.assertTrue(resume.collection_path(self.finished).is_file())
        self.assertFalse(resume.ended_path(self.finished).exists())

    def test_from_repository_cloning_marks_the_last_collection_as_replaced(self):
        """`--from repository_cloning` marks the last collection that ended as replaced
        (`forget_ended_collection`). The next run begins a new collection over the same list,
        and the labeling reads which collection the new snapshot replaces
        (`replaced_collection`). Where `--mark-complete` removed the record, the mark names the
        latest collection that a line names, by the time at the start of its name rather than
        by the order of the lines. A collection in progress that never ended is not marked,
        and a new collection removes the mark when it starts."""
        first = self._open(["o/a", "o/b"])
        append_finished(self.finished, "o/a", "row-o/a", None, collection=first["collection"])
        resume.close_collection(self.finished)
        resume.forget_ended_collection(self.finished)
        self.assertEqual(resume.replaced_collection(self.finished), first["collection"])
        self.assertIsNone(resume.read_collection(resume.ended_path(self.finished)))
        resume.forget_ended_collection(self.finished)
        self.assertEqual(resume.replaced_collection(self.finished), first["collection"])
        new = self._open(["o/a", "o/b"])
        self.assertNotEqual(new["collection"], first["collection"])
        self.assertIsNone(resume.replaced_collection(self.finished))
        self.assertFalse(resume.ended_path(self.finished).exists())
        append_finished(self.finished, "o/b", "row-o/b", None, collection=new["collection"])
        resume.forget_ended_collection(self.finished)
        self.assertEqual(resume.replaced_collection(self.finished), first["collection"],
                         "the collection in progress is not marked")
        resume.forget_collections(self.finished)
        self.finished.unlink()
        later, earlier = "2026-11-02T000000Z-00000000", "2026-11-01T000000Z-ffffffff"
        append_finished(self.finished, "o/a", "row-o/a", None, collection=later)
        append_finished(self.finished, "o/b", "row-o/b", None, collection=earlier)
        resume.forget_ended_collection(self.finished)
        self.assertEqual(resume.replaced_collection(self.finished), later)
        self.finished.unlink()
        resume.forget_collections(self.finished)
        resume.forget_ended_collection(self.finished)
        self.assertFalse(resume.ended_path(self.finished).exists())

    def test_from_repository_cloning_marks_a_continued_collection_as_replaced(self):
        """A continued collection, over its list or over its list without a repository that
        GitHub no longer serves, is the earlier snapshot. When a stop leaves it in progress,
        `--from repository_cloning` removes its record and marks it as replaced
        (`forget_ended_collection`), and the next run begins a new collection. Its record keeps
        the key `continued_after_end` through a resumed run and through the save of the step. A
        new collection in progress is not marked."""
        for left, gone in ((["o/a", "o/b"], frozenset()), (["o/a"], {"o/b"})):
            with self.subTest(left=left):
                resume.forget_collections(self.finished)
                first = self._open(["o/a", "o/b"])
                resume.close_collection(self.finished)
                self.assertEqual(self._open(left, gone)["collection"], first["collection"])
                resumed = self._open(left, gone)
                self.assertEqual(resumed["collection"], first["collection"])
                resume.save_collection(self.finished, resumed)
                resume.forget_ended_collection(self.finished)
                self.assertFalse(resume.collection_path(self.finished).exists())
                self.assertEqual(resume.replaced_collection(self.finished), first["collection"])
                new = self._open(left, gone)
                self.assertNotEqual(new["collection"], first["collection"])
                resume.forget_ended_collection(self.finished)
                self.assertEqual(self._open(left, gone)["collection"], new["collection"])

    def test_another_input_list_starts_a_new_collection(self):
        first = self._open(["o/a"])
        other = self._open(["o/a", "o/c"])
        self.assertNotEqual(other["collection"], first["collection"])

    def test_a_record_that_does_not_parse_starts_a_new_collection(self):
        resume.collection_path(self.finished).write_text("{not json", encoding="utf-8")
        self.assertEqual(self._open(["o/a"])["taken_over"], set())


class TestTheReductionOfTheList(unittest.TestCase):
    """A new collection keeps only the lines of the list of finished repositories that the readers
    of the list use. The commit reclassification and the cloning step, which decides from the
    list which repositories it takes over, read the same records from the reduced list."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.finished = self.tmp / "finished_repositories.jsonl"
        # o/a: a line with a count, then a line that records that git returned no head commit and
        # a line that marks a retry, both with the same row and without a count. The commit
        # reclassification keeps the count of the first line, and the cloning step reads the line
        # that marks the retry when it decides whether to take the repository over.
        append_finished(self.finished, "o/a", "row-a", None, measures={"copied": 3},
                        collection="c1")
        append_finished(self.finished, "o/a", "row-a", None, head_no_response=True,
                        collection="c1")
        append_finished(self.finished, "o/a", "row-a", None, retrying=True, collection="c1")
        # o/b: a line with a count, then a line with another row and no count. Neither reader uses
        # the first line.
        append_finished(self.finished, "o/b", "row-b1", None, measures={"copied": 2},
                        collection="c1")
        append_finished(self.finished, "o/b", "row-b2", "o/b,clone_failed", collection="c1")
        # o/c: two lines with a count. The readers use only the last.
        append_finished(self.finished, "o/c", "row-c", None, measures={"copied": 1},
                        facts=[{"path": "CLAUDE.md"}], collection="c1")
        append_finished(self.finished, "o/c", "row-c", None, measures={"copied": 4},
                        submodules=[{"path": "vendor/x"}], collection="c2")
        with open(self.finished, "a", encoding="utf-8") as f:
            f.write('{"repo": "o/d", "fail_row": null}\n')
            f.write("not json\n")
            f.write('{"repo": "o/c", "row": "row-c", "fail_row"')

    def _readers(self):
        return read_finished(self.finished), finished_copied(self.finished)

    def _open(self, repos):
        with mock.patch.object(resume.logs, "info"):
            return resume.open_collection(self.finished, repos)

    def test_a_new_collection_keeps_the_lines_that_its_readers_use(self):
        before = self._readers()
        self.assertEqual(before[1], {"o/a": 3, "o/b": None, "o/c": 4})
        self._open(["o/a", "o/b", "o/c"])
        self.assertEqual(self._readers(), before)
        lines = self.finished.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual([json.loads(line)["repo"] for line in lines],
                         ["o/a", "o/a", "o/b", "o/c"])

    def test_a_resumed_collection_leaves_the_list_as_it_is(self):
        self._open(["o/a", "o/b", "o/c"])
        append_finished(self.finished, "o/b", "row-b2", None, retrying=True)
        append_finished(self.finished, "o/b", "row-b3", None, measures={"copied": 5})
        reduced = self.finished.read_bytes()
        self._open(["o/c", "o/b", "o/a"])
        self.assertEqual(self.finished.read_bytes(), reduced)

    def test_a_list_that_cannot_be_written_stays_as_it_was(self):
        original = self.finished.read_bytes()
        with mock.patch.object(resume.caches.os, "replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self._open(["o/a"])
        self.assertEqual(self.finished.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [self.finished.name])

    def test_a_list_with_nothing_to_leave_out_is_not_written(self):
        self.finished.unlink()
        append_finished(self.finished, "o/a", "row-a", None, measures={"copied": 3})
        written = self.finished.stat()
        self._open(["o/a"])
        after = self.finished.stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (written.st_ino, written.st_mtime_ns))


if __name__ == "__main__":
    unittest.main()
