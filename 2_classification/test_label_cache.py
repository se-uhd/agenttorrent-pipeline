"""
Tests for label_cache.py

Strategy:
  - Every case writes into its own temporary directory, and no case touches labels/.
  - The tests check a file's checksum and name by reading back what was written and by
    changing the file on disk.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_label_cache -v
"""

import json
import lzma
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import label_cache  # noqa: E402
from label_cache import CacheError, Decisions  # noqa: E402

RUN_A, RUN_B = "2026-10-06T080000Z", "2026-11-06T080000Z"


def _label(repository_id, sha, label="true", model="m", parameters="c1", when="x"):
    return {"id": repository_id, "outcome": label_cache.LABELED, "readme_sha": sha,
            "model": model, "parameters": parameters, "label": label, "decided_at": when}


def traced_peak(reader):
    """What `reader()` returns and the peak of the memory that it allocated meanwhile."""
    import tracemalloc
    tracemalloc.start()
    try:
        result = reader()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def _exclusion(repository_id, sha, reason="empty_readme", parameters="c1", when="x"):
    return {"id": repository_id, "outcome": label_cache.EXCLUDED, "readme_sha": sha,
            "parameters": parameters, "reason": reason, "decided_at": when}


class _CacheCase(unittest.TestCase):

    def setUp(self):
        self.cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.cache, ignore_errors=True)

    def finish(self, rid, decisions):
        count, sha = label_cache.write_decisions(self.cache, rid, decisions)
        label_cache.put_index_row({"run_id": rid, "outcome": "finished", "decisions": count,
                                   "decisions_sha256": sha}, self.cache)


class TestFiles(_CacheCase):

    def test_decisions_are_read_back_as_written(self):
        rows = [_label(1, "a"), _exclusion(2, "b")]
        count, sha = label_cache.write_decisions(self.cache, RUN_A, rows)
        self.assertEqual(count, 2)
        self.assertEqual(list(label_cache.read_decisions(self.cache, RUN_A, sha)), rows)

    def concurrently(self, first, second):
        """Run `first` and `second` in threads, as two processes may interleave. `second` runs
        after `first` has read the index, while `first` waits up to half a second for `second`
        to end."""
        read, ended = threading.Event(), threading.Event()
        original = label_cache.read_index

        def read_index(cache_dir=label_cache.CACHE_DIR):
            rows = original(cache_dir)
            if threading.current_thread().name == "first":
                read.set()
                ended.wait(0.5)
            return rows

        def then_second():
            read.wait(5)
            second()
            ended.set()
        with mock.patch.object(label_cache, "read_index", read_index):
            threads = [threading.Thread(target=first, name="first"),
                       threading.Thread(target=then_second, name="second")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

    def test_two_runs_that_update_their_rows_at_once_keep_both_updates(self):
        """The two models' runs over one check sample run at once. Without the lock, the later
        write of the index would undo the other run's change and leave a finished run recorded
        as running in the index."""
        for rid in (RUN_A, RUN_B):
            label_cache.put_index_row({"run_id": rid, "outcome": "running"}, self.cache)
        self.concurrently(
            lambda: label_cache.put_index_row({"run_id": RUN_A, "outcome": "finished"},
                                              self.cache),
            lambda: label_cache.put_index_row({"run_id": RUN_B, "outcome": "finished"},
                                              self.cache))
        self.assertEqual([(row["run_id"], row["outcome"]) for row in self.index()],
                         [(RUN_A, "finished"), (RUN_B, "finished")])

    def test_two_runs_started_at_once_in_one_second_get_an_id_each(self):
        now = datetime(2026, 10, 6, 8, 15, tzinfo=timezone.utc)
        self.concurrently(lambda: label_cache.add_run({"outcome": "running"}, self.cache, now),
                          lambda: label_cache.add_run({"outcome": "running"}, self.cache, now))
        self.assertEqual([row["run_id"] for row in self.index()],
                         ["2026-10-06T081500Z", "2026-10-06T081501Z"])

    def index(self):
        return label_cache.read_index(self.cache)

    def test_a_decision_with_a_line_separator_in_a_string_is_read_back_whole(self):
        """JSON keeps U+2028, U+2029 and U+0085 in a string as they are, and a reader that ended
        a line at each of these characters would refuse the run's decisions as not JSON."""
        rows = [{**_label(1, "a"), "justification": "One. Two. Three.\x85Four."},
                _label(2, "b")]
        _, sha = label_cache.write_decisions(self.cache, RUN_A, rows)
        self.assertEqual(list(label_cache.read_decisions(self.cache, RUN_A, sha)), rows)

    def test_a_line_that_a_stop_cut_short_is_dropped_before_the_next_is_appended(self):
        path = self.cache / f"{RUN_A}.partial.jsonl"
        for written, kept in ((b'{"id":1}\n{"id":2,"sta', b'{"id":1}\n'),
                              (b'{"id":1}\n', b'{"id":1}\n'), (b'{"id":2,"sta', b"")):
            with self.subTest(written=written):
                path.write_bytes(written)
                label_cache.end_partial(path)
                self.assertEqual(path.read_bytes(), kept)
        label_cache.end_partial(self.cache / "none.partial.jsonl")
        self.assertFalse((self.cache / "none.partial.jsonl").exists())

    def test_a_partial_file_is_read_by_status_without_a_line_that_a_stop_cut_short(self):
        """The reader filters the lines of a partial file by the status at their head before it
        parses them. A line with a head that differs from what the run writes is parsed and then
        filtered by its status. A line that is not JSON is left out, and so is the last line. A
        stop can cut the last line inside a UTF-8 character, and another run can read the file
        while the run is still writing it."""
        path = self.cache / f"{RUN_A}.partial.jsonl"
        path.write_bytes('{"repositories":"r"}\n'
                         '{"id":1,"status":"kept","response":{"status":"decided"}}\n'
                         '{"id":2,"status":"decided","decision":{"why":"für"}}\n'
                         '{"id": 3, "status": "decided", "decision": {}}\n'
                         '{"status":"decided","id":4,"decision":{}}\n'
                         'not json\n'
                         '{"spent":{"model_requests":2}}\n'
                         '{"id":5,"status":"decided","decision":{"why":"f'.encode("utf-8")
                         + "ü".encode("utf-8")[:1])
        self.assertEqual([line["id"] for line in label_cache.partial_lines(path, ("decided",))],
                         [2, 3, 4])
        self.assertEqual(len(list(label_cache.partial_lines(path))), 6)
        self.assertEqual(label_cache.spent_in(path, ("model_requests",)), {"model_requests": 2})
        self.assertEqual(list(label_cache.partial_lines(self.cache / "none.partial.jsonl")), [])

    def test_a_changed_file_is_refused(self):
        _, sha = label_cache.write_decisions(self.cache, RUN_A, [_label(1, "a")])
        label_cache.decisions_path(self.cache, RUN_A).write_bytes(
            lzma.compress(b'{"id": 1, "label": "false"}\n'))
        with self.assertRaises(CacheError):
            list(label_cache.read_decisions(self.cache, RUN_A, sha))

    def test_a_part_that_does_not_match_its_checksum_yields_none_of_its_decisions(self):
        """The reader checks a part against its checksum before it reads the part's first
        decision, and it never uses a decision of a changed part. A part written again with
        other decisions, a part cut short and a part with one byte changed are each refused at
        the first decision. A changed later part is refused after the decisions of the parts
        before it."""
        self.addCleanup(setattr, label_cache, "MAX_FILE_BYTES", label_cache.MAX_FILE_BYTES)
        label_cache.MAX_FILE_BYTES = 6_000
        rows = [{**_label(i, "a"), "noise": os.urandom(1_000).hex()} for i in range(10)]
        _, checksums = label_cache.write_decisions(self.cache, RUN_A, rows)
        first, second = (label_cache.decisions_path(self.cache, RUN_A, part) for part in (1, 2))
        written = first.read_bytes()
        other = lzma.compress(b'{"id": 1, "label": "false"}\n', preset=label_cache.XZ_PRESET)
        flipped = written[:-20] + bytes([written[-20] ^ 1]) + written[-19:]
        for changed in (other, written[:len(written) // 2], flipped):
            with self.subTest(size=len(changed)):
                first.write_bytes(changed)
                with self.assertRaises(CacheError):
                    next(label_cache.read_decisions(self.cache, RUN_A, checksums))
        first.write_bytes(written)
        second.write_bytes(second.read_bytes()[:-1])
        read = []
        with self.assertRaises(CacheError):
            for decision in label_cache.read_decisions(self.cache, RUN_A, checksums):
                read.append(decision)
        in_first = lzma.decompress(written).count(b"\n")
        self.assertEqual(read, rows[:in_first])

    def test_a_part_is_read_one_line_at_a_time(self):
        """Every run reads every part of the cache, and a part keeps every tool output of its
        decisions. Decompressed whole, a part's text takes four bytes a character once one
        character is outside ASCII, and its lines take as much again. The reader's peak over a
        part of one short decision holds the decompressor's state. Over a part more than a
        hundred times as long as its longest line, the reader's peak exceeds that peak by less
        than ten times the longest line, and the reader returns every decision in its order."""
        rows = [{**_label(number, "a"), "justification": "Ein Werkzeug \U0001f6e0 für Dateien.",
                 "tools": [{"output": "x" * 100_000}]} for number in range(1, 121)]
        lines = [len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                 + 1 for row in rows]
        self.assertGreater(sum(lines), 100 * max(lines))
        _, checksums = label_cache.write_decisions(self.cache, RUN_A, rows)
        _, short = label_cache.write_decisions(self.cache, RUN_B, [_label(1, "a")])
        ids, peak = traced_peak(lambda: [decision["id"] for decision in
                                         label_cache.read_decisions(self.cache, RUN_A, checksums)])
        _, state = traced_peak(lambda: list(label_cache.read_decisions(self.cache, RUN_B, short)))
        self.assertEqual(ids, list(range(1, 121)))
        self.assertLess(peak - state, 10 * max(lines))

    def test_a_run_id_is_the_utc_time_it_started(self):
        self.assertEqual(label_cache.check_id(RUN_A), RUN_A)
        for bad in ("2026-10-06", "../2026-10-06T080000Z", "2026-10-06T08:00:00Z"):
            with self.subTest(bad=bad), self.assertRaises(CacheError):
                label_cache.check_id(bad)

    def test_two_runs_started_in_one_second_get_an_id_each(self):
        from datetime import datetime, timezone
        moment = datetime(2026, 10, 6, 8, 0, 0, tzinfo=timezone.utc)
        label_cache.put_index_row({"run_id": label_cache.new_run_id(self.cache, moment)},
                                  self.cache)
        self.assertEqual(label_cache.new_run_id(self.cache, moment), "2026-10-06T080001Z")

    def test_classification_parameters_are_recorded_under_their_hash_once(self):
        sha = label_cache.record("parameters", {"prompt": "é", "tools": []}, self.cache)
        self.assertEqual(label_cache.record("parameters", {"tools": [], "prompt": "é"},
                                            self.cache), sha)
        self.assertEqual(label_cache.recorded("parameters", sha, self.cache),
                         {"prompt": "é", "tools": []})
        path = self.cache / "parameters" / f"{sha}.json"
        path.write_text('{"prompt": "changed", "tools": []}')
        with self.assertRaises(CacheError):
            label_cache.recorded("parameters", sha, self.cache)

    def test_the_index_keeps_one_row_per_run_in_run_order(self):
        label_cache.put_index_row({"run_id": RUN_B, "outcome": "running"}, self.cache)
        label_cache.put_index_row({"run_id": RUN_A, "outcome": "finished"}, self.cache)
        label_cache.put_index_row({"run_id": RUN_B, "outcome": "finished", "labeled": 3},
                                  self.cache)
        rows = label_cache.read_index(self.cache)
        self.assertEqual([(r["run_id"], r["outcome"], r["labeled"]) for r in rows],
                         [(RUN_A, "finished", ""), (RUN_B, "finished", "3")])
        with self.assertRaises(CacheError):
            label_cache.put_index_row({"run_id": RUN_A, "cost": 1}, self.cache)

    def written_in_one_step(self, write, target: Path):
        """Run `write`, which rewrites `target`, with the real `os.fsync` and `os.replace`
        recording each call, and return the calls in order: each sync with whether it was the
        directory and the file's inode, and the rename with the inode that it moved."""
        directory = os.stat(self.cache)
        calls = []
        sync, rename = os.fsync, os.replace

        def recorded_sync(handle):
            status = os.fstat(handle)
            if stat.S_ISDIR(status.st_mode):
                calls.append(("directory", (status.st_dev, status.st_ino)
                              == (directory.st_dev, directory.st_ino)))
            else:
                calls.append(("file", status.st_ino, target.read_bytes()))
            sync(handle)

        def recorded_rename(source, destination):
            calls.append(("rename", os.stat(source).st_ino, Path(destination) == target))
            rename(source, destination)

        with mock.patch("os.fsync", recorded_sync), mock.patch("os.replace", recorded_rename):
            write()
        return calls

    def test_a_rewrite_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """A power loss shortly after a rename that reached the disk before the data would leave
        an empty index, which no later run could read. The new index is synced to disk under a
        temporary name while the old index stays whole. The rename then replaces the old index
        with that file, and the directory is synced after the rename and before the write
        returns. The labels and the decisions are written in the same way."""
        label_cache.put_index_row({"run_id": RUN_A, "outcome": "running"}, self.cache)
        index = self.cache / label_cache.INDEX_NAME
        old, before = index.read_bytes(), index.stat().st_ino
        calls = self.written_in_one_step(
            lambda: label_cache.put_index_row({"run_id": RUN_B, "outcome": "running"},
                                              self.cache), index)
        after = index.stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(calls, [("file", after, old), ("rename", after, True),
                                 ("directory", True)])
        self.assertEqual([row["run_id"] for row in label_cache.read_index(self.cache)],
                         [RUN_A, RUN_B])
        self.assertEqual(sorted(path.name for path in self.cache.iterdir()),
                         [label_cache.INDEX_NAME])
        for path, write in ((label_cache.labels_path(self.cache, RUN_A),
                             lambda: label_cache.write_labels(self.cache, RUN_A, [])),
                            (label_cache.decisions_path(self.cache, RUN_A),
                             lambda: label_cache.write_decisions(self.cache, RUN_A, []))):
            with self.subTest(path=path.name):
                path.write_bytes(b"earlier")
                calls = self.written_in_one_step(write, path)
                self.assertEqual(calls, [("file", path.stat().st_ino, b"earlier"),
                                         ("rename", path.stat().st_ino, True),
                                         ("directory", True)])

    def test_a_run_past_the_limit_is_split_into_parts_under_it(self):
        """GitHub refuses a file over 100 MB, and a run over a whole sampling frame with every
        tool output comes near that size. The decisions go into as many parts as they need to
        stay under the limit, and the reader reads the parts back in their order. Random text,
        which xz cannot compress, lets a small limit split the decisions."""
        import os
        limit = 6_000
        self.addCleanup(setattr, label_cache, "MAX_FILE_BYTES", label_cache.MAX_FILE_BYTES)
        label_cache.MAX_FILE_BYTES = limit
        rows = [{**_label(i, "a"), "noise": os.urandom(1_000).hex()} for i in range(10)]
        count, checksums = label_cache.write_decisions(self.cache, RUN_A, rows)
        parts = sorted(self.cache.glob(f"{RUN_A}.decisions-*.jsonl.xz"))
        self.assertEqual(count, 10)
        self.assertGreater(len(parts), 1)
        self.assertEqual(len(checksums.split()), len(parts))
        self.assertTrue(all(part.stat().st_size < limit for part in parts))
        self.assertEqual(list(label_cache.read_decisions(self.cache, RUN_A, checksums)), rows)

    def test_a_rewrite_with_fewer_parts_removes_the_rest(self):
        self.addCleanup(setattr, label_cache, "MAX_FILE_BYTES", label_cache.MAX_FILE_BYTES)
        label_cache.MAX_FILE_BYTES = 6_000
        import os
        label_cache.write_decisions(self.cache, RUN_A, [{**_label(i, "a"),
                                                         "noise": os.urandom(1_000).hex()}
                                                        for i in range(10)])
        _, checksums = label_cache.write_decisions(self.cache, RUN_A, [_label(1, "a")])
        self.assertEqual([p.name for p in self.cache.glob(f"{RUN_A}.decisions-*")],
                         [f"{RUN_A}.decisions-001.jsonl.xz"])
        self.assertEqual(list(label_cache.read_decisions(self.cache, RUN_A, checksums)),
                         [_label(1, "a")])

    def test_one_decision_past_the_limit_is_refused(self):
        self.addCleanup(setattr, label_cache, "MAX_FILE_BYTES", label_cache.MAX_FILE_BYTES)
        label_cache.MAX_FILE_BYTES = 10
        with self.assertRaises(CacheError):
            label_cache.write_decisions(self.cache, RUN_A, [_label(1, "a")])
        self.assertFalse(label_cache.decisions_path(self.cache, RUN_A).exists())


class TestDecisions(_CacheCase):

    def test_a_later_run_replaces_an_earlier_decision_under_the_same_key(self):
        self.finish(RUN_A, [_label(1, "a", "true")])
        self.finish(RUN_B, [_label(1, "b", "false")])
        decisions = Decisions.load(self.cache, model="m", parameters="c1")
        self.assertEqual(decisions.label(1, "m", "c1")["label"], "false")

    def test_labels_are_kept_apart_by_model_and_classification_parameters(self):
        self.finish(RUN_A, [_label(1, "a", "true"), _label(1, "a", "false", model="n"),
                            _label(1, "a", "unclear", parameters="c2")])
        self.assertEqual([Decisions.load(self.cache, model=model, parameters=parameters)
                          .label(1, model, parameters)["label"]
                          for model, parameters in (("m", "c1"), ("n", "c1"), ("m", "c2"))],
                         ["true", "false", "unclear"])
        self.assertIsNone(Decisions.load(self.cache, model="n", parameters="c2")
                          .label(1, "n", "c2"))

    def test_only_the_decisions_that_the_run_can_reuse_are_held(self):
        """A run looks up only the labels under its model and classification parameters and the
        exclusions under its parameters. The decisions under another set of parameters or of
        another model, from decisions files and from the partial file of a killed run, are not
        held, because each keeps its tool outputs and the cache keeps every run."""
        self.finish(RUN_A, [_label(1, "a", when="t1"), _label(2, "b", parameters="c2", when="t2"),
                            _label(3, "c", model="n", when="t3"), _exclusion(4, "d", when="t4"),
                            _exclusion(5, "e", parameters="c2", when="t5")])
        label_cache.put_index_row({"run_id": RUN_B, "outcome": "running"}, self.cache)
        label_cache.partial_path(self.cache, RUN_B).write_text(RUN_B)
        partial = [_label(6, "f", when="t6"), _label(7, "g", parameters="c2", when="t7")]
        decisions = Decisions.load(self.cache, model="m", parameters="c1",
                                   partial=lambda path: partial)
        self.assertEqual(sorted(decisions.labels), [(1, "m", "c1"), (6, "m", "c1")])
        self.assertEqual(sorted(decisions.exclusions), [(4, "c1")])
        self.assertEqual(decisions.origins, {(1, "t1"): RUN_A, (4, "t4"): RUN_A,
                                             (6, "t6"): RUN_B})
        self.assertEqual(decisions.in_files, {(1, "t1"), (4, "t4")})
        self.assertIsNone(decisions.latest(2, "m", "c2"))
        self.assertIsNone(decisions.latest(5, "m", "c2"))
        self.assertEqual(decisions.origin(_label(2, "b", parameters="c2", when="t2")), "")
        # The other set of parameters holds its own decisions alone.
        other = Decisions.load(self.cache, model="m", parameters="c2",
                               partial=lambda path: partial)
        self.assertEqual(sorted(other.labels), [(2, "m", "c2"), (7, "m", "c2")])
        self.assertEqual(sorted(other.exclusions), [(5, "c2")])

    def test_the_latest_is_the_more_recent_of_label_and_exclusion(self):
        self.finish(RUN_A, [_label(1, "a"), _exclusion(2, "c")])
        self.finish(RUN_B, [_exclusion(1, "b"), _label(2, "d")])
        decisions = Decisions.load(self.cache, model="m", parameters="c1")
        self.assertEqual(decisions.latest(1, "m", "c1")["outcome"], label_cache.EXCLUDED)
        self.assertEqual(decisions.latest(2, "m", "c1")["outcome"], label_cache.LABELED)
        self.assertIsNone(decisions.latest(3, "m", "c1"))

    def test_an_exclusion_stands_for_every_model_under_its_classification_parameters(self):
        """No model decided the exclusion, and it therefore applies to every model under the
        same classification parameters."""
        self.finish(RUN_A, [_exclusion(1, "b")])
        decisions = Decisions.load(self.cache, model="n", parameters="c1")
        self.assertEqual(decisions.exclusion(1, "c1")["reason"], "empty_readme")
        self.assertEqual(decisions.latest(1, "n", "c1")["outcome"], label_cache.EXCLUDED)
        self.assertIsNone(decisions.exclusion(2, "c1"))
        other = Decisions.load(self.cache, model="m", parameters="c2")
        self.assertIsNone(other.exclusion(1, "c2"))
        self.assertIsNone(other.latest(1, "m", "c2"))

    def test_a_run_without_a_decisions_file_is_not_read(self):
        """A run that stopped has an index row and no file yet."""
        label_cache.put_index_row({"run_id": RUN_A, "outcome": "incomplete"}, self.cache)
        self.assertEqual(list(label_cache.all_decisions(self.cache)), [])

    def test_the_partial_file_of_a_killed_run_is_read_and_told_apart(self):
        """A kill ends a run before the run writes its decisions file. `Decisions.load` reads
        the run's partial file in run order and tells its decisions apart from the decisions that
        a decisions file contains. It skips the partial file of the run that loads the decisions
        and the partial file of a finished run."""
        self.finish(RUN_A, [_label(1, "a", "true", when="t1"), _label(2, "c", when="t2")])
        for rid, outcome in ((RUN_B, "running"), ("2026-12-06T080000Z", "running"),
                             ("2027-01-06T080000Z", "finished")):
            label_cache.put_index_row({"run_id": rid, "outcome": outcome}, self.cache)
            label_cache.partial_path(self.cache, rid).write_text(rid)
        partial = {RUN_B: [_label(1, "b", "false", when="t3")],
                   "2026-12-06T080000Z": [_label(3, "d", when="t4")],
                   "2027-01-06T080000Z": [_label(4, "e", when="t5")]}
        decisions = Decisions.load(self.cache, model="m", parameters="c1",
                                   partial=lambda path: partial[path.read_text()],
                                   skip="2026-12-06T080000Z")
        killed = decisions.label(1, "m", "c1")
        self.assertEqual((killed["label"], decisions.origin(killed), decisions.written(killed)),
                         ("false", RUN_B, False))
        self.assertTrue(decisions.written(decisions.label(2, "m", "c1")))
        self.assertIsNone(decisions.label(3, "m", "c1"))
        self.assertIsNone(decisions.label(4, "m", "c1"))
        # Without a reader of partial files, only the decisions files are read.
        self.assertEqual(Decisions.load(self.cache, model="m", parameters="c1")
                         .label(1, "m", "c1")["label"], "true")


# Classification parameters as the classification records them since 2026-11-04, without the
# provider of a model, and as it recorded the same parameters before, with the provider.
CURRENT = {"prompt": "Label it.",
           "classifying_model": {"model": "deepseek/deepseek-v4.1-flash",
                                 "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash",
                                 "quantization": "fp8", "reasoning_effort": "high",
                                 "temperature": 0.6},
           "summary": {"model": "deepseek/deepseek-v4.1-flash",
                       "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash",
                       "quantization": "fp8", "reasoning_effort": "low"}}
EARLIER = {**CURRENT,
           "classifying_model": {**CURRENT["classifying_model"], "provider": "morph/fp8"},
           "summary": {**CURRENT["summary"], "provider": "morph/fp8"}}


class TestParameterKeys(_CacheCase):
    """The maintainer decided on 2026-11-04 that the provider that serves a model is no
    classification parameter. The decisions, the kept responses and the partial files under
    parameters recorded before, which name the provider, count as recorded under the parameters
    that differ from them only in the provider, without a rewrite of a stored file."""

    def test_parameters_recorded_with_the_provider_have_the_key_of_the_same_parameters_without(
            self):
        earlier = label_cache.record("parameters", EARLIER, self.cache)
        current = label_cache.record("parameters", CURRENT, self.cache)
        self.assertNotEqual(earlier, current)
        keys = label_cache.ParameterKeys(self.cache)
        self.assertEqual(keys(earlier), current)
        self.assertEqual(keys(current), current)
        # Another provider of the same weights in the same quantization has the same key.
        other = label_cache.record("parameters", {
            **EARLIER, "classifying_model": {**EARLIER["classifying_model"],
                                             "provider": "deepinfra/fp8"}}, self.cache)
        self.assertEqual(keys(other), current)
        # A digest without a record is its own key.
        self.assertEqual(keys("c1"), "c1")
        self.assertEqual(label_cache.shared_parameters(self.cache, earlier),
                         label_cache.shared_parameters(self.cache, current))

    def test_parameters_that_differ_in_the_model_the_weights_the_quantization_or_a_setting_do_not(
            self):
        current = label_cache.record("parameters", CURRENT, self.cache)
        keys = label_cache.ParameterKeys(self.cache)
        for block, field, value in (("classifying_model", "model", "z-ai/glm-5.3"),
                                    ("classifying_model", "hugging_face_id", "d/Other"),
                                    ("classifying_model", "quantization", "mxfp4"),
                                    ("classifying_model", "temperature", 1.0),
                                    ("classifying_model", "reasoning_effort", "low"),
                                    ("summary", "quantization", "mxfp4"),
                                    ("summary", "reasoning_effort", "high")):
            with self.subTest(block=block, field=field):
                differing = {**EARLIER, block: {**EARLIER[block], field: value}}
                self.assertNotEqual(keys(label_cache.record("parameters", differing,
                                                            self.cache)), current)

    def test_a_run_under_the_current_parameters_reuses_the_decisions_and_kept_responses(self):
        """The decisions of a decisions file and of the partial file of a killed run, and the
        kept responses, under parameters recorded with the provider are loaded by a run under
        the same parameters without it, and a run under other parameters loads none of them."""
        earlier = label_cache.record("parameters", EARLIER, self.cache)
        current = label_cache.record("parameters", CURRENT, self.cache)
        self.finish(RUN_A, [_label(1, "a", parameters=earlier, when="t1"),
                            _exclusion(2, "b", parameters=earlier, when="t2")])
        label_cache.put_index_row({"run_id": RUN_B, "outcome": "running", "model": "m",
                                   "parameters": earlier}, self.cache)
        label_cache.partial_path(self.cache, RUN_B).write_text(RUN_B)
        partial = [_label(3, "c", parameters=earlier, when="t3")]
        decisions = Decisions.load(self.cache, model="m", parameters=current,
                                   partial=lambda path: partial)
        self.assertEqual(decisions.label(1, "m", current)["readme_sha"], "a")
        self.assertEqual(decisions.exclusion(2, current)["readme_sha"], "b")
        self.assertEqual(decisions.latest(3, "m", current)["readme_sha"], "c")
        self.assertEqual(decisions.label(1, "m", earlier)["readme_sha"], "a")
        self.assertEqual(Decisions.kept(self.cache, "m", current,
                                        partial=lambda path: {"k": {"label": "true"}}),
                         {"k": {"label": "true"}})
        other = label_cache.record("parameters", {
            **CURRENT, "classifying_model": {**CURRENT["classifying_model"],
                                             "quantization": "mxfp4"}}, self.cache)
        unrelated = Decisions.load(self.cache, model="m", parameters=other,
                                   partial=lambda path: partial)
        self.assertEqual((unrelated.labels, unrelated.exclusions), ({}, {}))
        self.assertEqual(Decisions.kept(self.cache, "m", other,
                                        partial=lambda path: {"k": {}}), {})


class TestKeptResponses(_CacheCase):
    """The model's responses that a run kept for repositories that it left without a decision,
    which a later run under the same model and classification parameters reads."""

    def test_the_responses_of_the_runs_under_the_same_model_and_parameters_are_read(self):
        runs = (("2026-08-06T080000Z", "finished", "m", "c1"), (RUN_A, "finished", "m", "c2"),
                (RUN_B, "finished", "n", "c1"), ("2026-12-06T080000Z", "incomplete", "m", "c1"),
                ("2027-01-06T080000Z", "running", "m", "c1"))
        for rid, outcome, model, parameters in runs:
            label_cache.put_index_row({"run_id": rid, "outcome": outcome, "model": model,
                                       "parameters": parameters}, self.cache)
            label_cache.write_kept(self.cache, rid, {f"k-{rid}": {"label": rid}})
            label_cache.partial_path(self.cache, rid).write_text(rid)
        kept = Decisions.kept(self.cache, "m", "c1",
                              partial=lambda path: {f"p-{path.read_text()}": {}},
                              skip="2027-01-06T080000Z")
        # The responses come from a finished run's kept file and from the partial file of an
        # incomplete run, because a later finish rewrites the incomplete run's kept file.
        self.assertEqual(kept, {"k-2026-08-06T080000Z": {"label": "2026-08-06T080000Z"},
                                "p-2026-12-06T080000Z": {}})

    def test_a_kept_file_that_fails_its_check_is_read_as_empty(self):
        label_cache.write_kept(self.cache, RUN_A, {"k1": {"label": "true"}, "k2": {}})
        path = label_cache.kept_path(self.cache, RUN_A)
        self.assertEqual(label_cache.read_kept(path), {"k1": {"label": "true"}, "k2": {}})
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        self.assertEqual(label_cache.read_kept(path), {})

    def test_no_response_to_keep_removes_the_file(self):
        label_cache.write_kept(self.cache, RUN_A, {"k1": {}})
        label_cache.write_kept(self.cache, RUN_A, {})
        self.assertFalse(label_cache.kept_path(self.cache, RUN_A).exists())


class TestRunLabels(_CacheCase):
    """The outcome of every repository of a run, which the comparison with the next run reads."""

    ROWS = [{"id": "1", "repo_name": "o/a", "outcome": "labeled", "label": "true", "reason": "",
             "decided_in": RUN_A},
            {"id": "2", "repo_name": "o/b", "outcome": "excluded", "label": "",
             "reason": "empty_readme", "decided_in": RUN_A},
            {"id": "3", "repo_name": "o/c", "outcome": "gone", "label": "", "reason": "",
             "decided_in": ""}]

    def test_a_runs_labels_are_read_back_as_written(self):
        sha = label_cache.write_labels(self.cache, RUN_A, self.ROWS)
        self.assertEqual(label_cache.read_labels(self.cache, RUN_A, sha), self.ROWS)

    def test_a_changed_labels_file_is_refused(self):
        sha = label_cache.write_labels(self.cache, RUN_A, self.ROWS)
        label_cache.write_labels(self.cache, RUN_A, self.ROWS[:1])
        with self.assertRaises(CacheError):
            label_cache.read_labels(self.cache, RUN_A, sha)

    def test_the_previous_run_is_the_last_finished_run_over_a_sampling_frame_before(self):
        trial, stopped, later = "2026-10-20T080000Z", "2026-10-25T080000Z", "2026-12-06T080000Z"
        for rid, outcome, scope in ((RUN_A, "finished", "sampling_frame"),
                                    (trial, "finished", "limit"),
                                    (stopped, "incomplete", "sampling_frame"),
                                    (RUN_B, "running", "sampling_frame"),
                                    (later, "finished", "sampling_frame")):
            label_cache.put_index_row({"run_id": rid, "outcome": outcome, "scope": scope,
                                       "labels_sha256": "x" if outcome == "finished" else ""},
                                      self.cache)
        self.assertEqual(label_cache.previous_run(self.cache, RUN_B)["run_id"], RUN_A)
        self.assertIsNone(label_cache.previous_run(self.cache, RUN_A))

    def test_the_comparison_is_written_in_parts_as_the_decisions_are(self):
        changes = [{"id": 1, "cause": "input_change"}, {"id": 2, "cause": "model_variation"}]
        count, sums = label_cache.write_changes(self.cache, RUN_A, changes)
        self.assertEqual(count, 2)
        self.assertEqual(list(label_cache.read_changes(self.cache, RUN_A, sums)), changes)
        self.assertTrue(label_cache.changes_path(self.cache, RUN_A).name.endswith(
            ".changes-001.jsonl.xz"))

    def test_a_decision_names_the_run_that_made_it(self):
        self.finish(RUN_A, [_label(1, "s1", when="t1")])
        self.finish(RUN_B, [_label(2, "s2", when="t2")])
        decisions = Decisions.load(self.cache, model="m", parameters="c1")
        self.assertEqual(decisions.origin(decisions.label(1, "m", "c1")), RUN_A)
        self.assertEqual(decisions.origin(decisions.label(2, "m", "c1")), RUN_B)
        found = label_cache.decisions_of(self.cache, {RUN_B: {2}})
        self.assertEqual(list(found), [(RUN_B, 2)])

    def test_a_decision_that_the_named_run_does_not_contain_is_refused(self):
        """A run's labels name the run that decided each repository. A decision missing from
        that run's files is refused with a message rather than a KeyError and its traceback."""
        self.finish(RUN_A, [_label(1, "s1")])
        with self.assertRaises(CacheError) as refused:
            label_cache.decisions_of(self.cache, {RUN_A: {1, 2}})
        self.assertIn(f"run {RUN_A} as the run that decided repository 2",
                      str(refused.exception))


class TestTokens(_CacheCase):
    """The tokens file of a run counts the tokens by the role, the model, the tier that the
    requests named and the tier that their responses reported."""

    FLEX = ("classifying_model", "gpt-6-luna", "flex", "flex")
    SUMMARY = ("summary_model", "gpt-6-luna", "auto", "default")

    def test_each_invocation_adds_its_tokens_to_the_run(self):
        """A resumed run and the comparison of its labels each add the tokens of their own
        responses. The file holds their sums, one row per role, model, requested tier and
        service tier, sorted by these keys."""
        label_cache.add_tokens(self.cache, RUN_A, {
            self.FLEX: {"requests": 2, "input_tokens": 300, "cached_tokens": 100,
                        "cache_write_tokens": 150, "output_tokens": 40, "reasoning_tokens": 9}})
        label_cache.add_tokens(self.cache, RUN_A, {
            self.SUMMARY: {"requests": 1, "input_tokens": 70, "output_tokens": 5},
            self.FLEX: {"requests": 1, "input_tokens": 50, "cached_tokens": 10,
                        "cache_write_tokens": 20, "output_tokens": 6, "reasoning_tokens": 1}})
        label_cache.add_tokens(self.cache, RUN_A, {})
        self.assertEqual(label_cache.read_tokens(self.cache, RUN_A), [
            {"role": "classifying_model", "model": "gpt-6-luna", "requested_tier": "flex",
             "service_tier": "flex", "requests": 3, "input_tokens": 350, "cached_tokens": 110,
             "cache_write_tokens": 170, "output_tokens": 46, "reasoning_tokens": 10},
            {"role": "summary_model", "model": "gpt-6-luna", "requested_tier": "auto",
             "service_tier": "default", "requests": 1, "input_tokens": 70, "cached_tokens": 0,
             "cache_write_tokens": 0, "output_tokens": 5, "reasoning_tokens": 0}])
        self.assertNotIn(b"\r\n", label_cache.tokens_path(self.cache, RUN_A).read_bytes())
        self.assertEqual(label_cache.read_tokens(self.cache, RUN_B), [])

    def test_a_tokens_file_with_other_columns_is_refused(self):
        label_cache.tokens_path(self.cache, RUN_A).write_text("role,model,tokens\nx,m,1\n",
                                                               encoding="utf-8")
        with self.assertRaises(CacheError):
            label_cache.read_tokens(self.cache, RUN_A)


if __name__ == "__main__":
    unittest.main()
