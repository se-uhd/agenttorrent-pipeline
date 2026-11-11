"""
Tests for ghs_cache.py

Strategy:
  - Every test works in a temporary directory and never reads or writes the tracked cache
    under ghs_retrievals/.
  - The tests of the volume split lower VOLUME_BYTES until a few hundred rows reach it, and
    they read the volumes back as one stream.
  - The import is tested over a small GHS search result in the format that the earlier download
    wrote.
  - `_script_loader.synced_writes` records each call of the real `os.fsync` and `os.replace`,
    and the tests read from these calls that a file is on disk before its rename.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_ghs_cache -v
"""

import contextlib
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghs_cache  # noqa: E402
from _script_loader import synced_writes  # noqa: E402
from ghs_cache import CacheError  # noqa: E402

RID = "2026-03-16T180701Z"


def _rows(n, start=0):
    return [{"id": i, "name": f"owner{i}/repo{i}", "metrics": []} for i in range(start, start + n)]


class _CacheCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "ghs_retrievals"


class TestIds(unittest.TestCase):

    def test_an_id_is_the_utc_start_to_the_second(self):
        moment = datetime(2026, 3, 16, 19, 7, 1, 920742, tzinfo=timezone(timedelta(hours=1)))
        self.assertEqual(ghs_cache.retrieval_id(moment), RID)

    def test_anything_else_is_refused(self):
        for value in ("2026-03-16", "2026-03-16T18:07:01Z", "../etc", "", None):
            with self.subTest(value=value), self.assertRaises(CacheError):
                ghs_cache.check_id(value)

    def test_a_path_cannot_leave_the_cache(self):
        with self.assertRaises(CacheError):
            ghs_cache.rows_path(Path("."), "../x", "ghs")
        with self.assertRaises(CacheError):
            ghs_cache.rows_path(Path("."), RID, "other")


class TestNormalize(unittest.TestCase):

    def test_metrics_are_sorted_by_language(self):
        item = {"name": "a/b", "metrics": [{"language": "Java"}, {"language": "C"}]}
        self.assertEqual([m["language"] for m in ghs_cache.normalize(item)["metrics"]],
                         ["C", "Java"])

    def test_the_key_ignores_case(self):
        self.assertEqual(ghs_cache.name_key(" Pagehelper-Org/Mybatis-PageHelper "),
                         "pagehelper-org/mybatis-pagehelper")


class TestRows(_CacheCase):

    def test_rows_round_trip_in_the_order_written(self):
        rows = _rows(50)
        random.Random(1).shuffle(rows)
        count, sha = ghs_cache.write_rows(self.cache, RID, "ghs", rows)
        self.assertEqual(count, 50)
        self.assertEqual(list(ghs_cache.read_rows(self.cache, RID, "ghs")), rows)
        self.assertEqual(ghs_cache.file_sha256(self.cache, RID, "ghs"), sha)

    def test_a_file_over_the_volume_size_is_split_and_read_back_whole(self):
        rows = [{"id": i, "noise": random.Random(i).randbytes(64).hex()} for i in range(400)]
        with mock.patch.object(ghs_cache, "VOLUME_BYTES", 10_000):
            count, sha = ghs_cache.write_rows(self.cache, RID, "github", rows)
        parts = ghs_cache.volumes(ghs_cache.rows_path(self.cache, RID, "github"))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(p.stat().st_size <= 10_000 for p in parts))
        self.assertEqual(list(ghs_cache.read_rows(self.cache, RID, "github", sha)), rows)
        self.assertEqual(count, 400)

    def test_rewriting_a_split_file_as_a_small_one_leaves_no_volume_behind(self):
        rows = [{"noise": random.Random(i).randbytes(64).hex()} for i in range(400)]
        with mock.patch.object(ghs_cache, "VOLUME_BYTES", 10_000):
            ghs_cache.write_rows(self.cache, RID, "github", rows)
        ghs_cache.write_rows(self.cache, RID, "github", rows[:2])
        self.assertEqual([p.name for p in self.cache.iterdir()], [f"{RID}.github.jsonl.xz"])
        self.assertEqual(list(ghs_cache.read_rows(self.cache, RID, "github")), rows[:2])

    def test_a_changed_file_is_refused_against_the_recorded_checksum(self):
        _, sha = ghs_cache.write_rows(self.cache, RID, "ghs", _rows(3))
        ghs_cache.write_rows(self.cache, RID, "ghs", _rows(4))
        with self.assertRaisesRegex(CacheError, "changed after it was written"):
            next(ghs_cache.read_rows(self.cache, RID, "ghs", sha))

    def test_a_gap_in_the_volumes_is_refused(self):
        path = ghs_cache.rows_path(self.cache, RID, "ghs")
        self.cache.mkdir()
        for n in (1, 3):
            path.with_name(f"{path.name}.{n:03d}").write_bytes(b"x")
        with self.assertRaisesRegex(CacheError, "without a gap"):
            ghs_cache.volumes(path)

    def test_failing_to_write_the_rows_leaves_the_previous_file_and_no_temporary(self):
        ghs_cache.write_rows(self.cache, RID, "ghs", _rows(2))

        def broken():
            yield {"id": 1}
            raise OSError("disk full")

        with self.assertRaises(OSError):
            ghs_cache.write_rows(self.cache, RID, "ghs", broken())
        self.assertEqual(len(list(ghs_cache.read_rows(self.cache, RID, "ghs"))), 2)
        self.assertEqual(list(self.cache.glob("*.tmp")), [])

    def test_writing_stopped_before_the_rename_leaves_the_previous_file_whole(self):
        """A stop before the new file is in place leaves the previous file whole
        (ghs-cache-spec.md). The test covers a single file and a file split into volumes, each
        replacing a single file and a split file."""
        small = _rows(2)
        large = [{"noise": random.Random(i).randbytes(64).hex()} for i in range(400)]
        replace = os.replace

        def stopped(source, target):
            if not str(source).endswith(".tmp"):
                return replace(source, target)
            raise OSError("stopped before the rename")

        for before, after in ((small, small[:1]), (small, large), (large, small), (large, large[:300])):
            with self.subTest(before=len(before), after=len(after)):
                shutil.rmtree(self.cache, ignore_errors=True)
                with mock.patch.object(ghs_cache, "VOLUME_BYTES", 10_000):
                    _, sha = ghs_cache.write_rows(self.cache, RID, "github", before)
                    with mock.patch.object(ghs_cache.os, "replace", stopped), \
                            self.assertRaises(OSError):
                        ghs_cache.write_rows(self.cache, RID, "github", after)
                    self.assertEqual(list(ghs_cache.read_rows(self.cache, RID, "github", sha)),
                                     before)
                self.assertEqual(list(self.cache.glob("*.tmp")), [])

    def test_the_rows_are_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """A power loss shortly after a rename that reached the disk before the data would leave
        an empty or cut file under the retrieval's name, and no reader could use that file.
        Before the rename, the new file is synced under a temporary name, or each volume under
        its own. The directory is synced after the renames and before the write returns."""
        path = ghs_cache.rows_path(self.cache, RID, "github")
        ghs_cache.write_rows(self.cache, RID, "github", _rows(2))
        before = path.stat().st_ino
        calls = synced_writes(lambda: ghs_cache.write_rows(self.cache, RID, "github", _rows(3)),
                              self.cache)
        after = path.stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(calls, [("file", after), ("rename", after, path.name),
                                 ("directory", True)])
        large = [{"noise": random.Random(i).randbytes(64).hex()} for i in range(400)]
        with mock.patch.object(ghs_cache, "VOLUME_BYTES", 10_000):
            calls = synced_writes(lambda: ghs_cache.write_rows(self.cache, RID, "github", large),
                                  self.cache)
        parts = ghs_cache.volumes(path)
        self.assertGreater(len(parts), 1)
        inodes = [part.stat().st_ino for part in parts]
        self.assertEqual(calls, [*(("file", inode) for inode in inodes),
                                 *(("rename", inode, part.name)
                                   for inode, part in zip(inodes, parts)),
                                 ("directory", True)])

    def test_the_error_for_missing_rows_names_the_source_and_the_retrieval(self):
        with self.assertRaisesRegex(CacheError, "no github rows for retrieval"):
            next(ghs_cache.read_rows(self.cache, RID, "github"))


class TestIndex(_CacheCase):

    def test_rows_are_added_updated_and_kept_in_id_order(self):
        ghs_cache.put_index_row({"retrieval_id": "2026-10-05T120000Z", "outcome": "retrieved",
                                 "query": {"b": 1, "a": True}}, self.cache)
        ghs_cache.put_index_row({"retrieval_id": RID, "outcome": "imported"}, self.cache)
        ghs_cache.put_index_row({"retrieval_id": RID, "github_rows": 7}, self.cache)
        rows = ghs_cache.read_index(self.cache)
        self.assertEqual([r["retrieval_id"] for r in rows], [RID, "2026-10-05T120000Z"])
        self.assertEqual(rows[0]["outcome"], "imported")
        self.assertEqual(rows[0]["github_rows"], "7")
        self.assertEqual(json.loads(rows[1]["query"]), {"a": True, "b": 1})

    def test_previous_and_latest_skip_failures(self):
        for rid, outcome in ((RID, "imported"), ("2026-10-01T000000Z", "failed"),
                             ("2026-10-02T000000Z", "retrieved")):
            ghs_cache.put_index_row({"retrieval_id": rid, "outcome": outcome}, self.cache)
        self.assertEqual(ghs_cache.latest_retrieval(self.cache)["retrieval_id"],
                         "2026-10-02T000000Z")
        self.assertEqual(ghs_cache.previous_retrieval("2026-10-02T000000Z", self.cache)
                         ["retrieval_id"], RID)
        self.assertIsNone(ghs_cache.previous_retrieval(RID, self.cache))

    def test_a_header_from_another_version_is_refused(self):
        self.cache.mkdir()
        (self.cache / "index.csv").write_text("retrieval_id,outcome\n")
        with self.assertRaisesRegex(CacheError, "has the columns"):
            ghs_cache.read_index(self.cache)

    def test_a_second_row_for_one_id_is_refused(self):
        ghs_cache.put_index_row({"retrieval_id": RID, "outcome": "imported"}, self.cache)
        path = self.cache / "index.csv"
        lines = path.read_text().splitlines()
        path.write_text("\n".join(lines + lines[1:]) + "\n")
        with self.assertRaisesRegex(CacheError, "second row"):
            ghs_cache.read_index(self.cache)

    def test_an_unknown_outcome_or_column_is_refused(self):
        with self.assertRaisesRegex(CacheError, "no column"):
            ghs_cache.put_index_row({"retrieval_id": RID, "outcome": "imported",
                                     "colour": "red"}, self.cache)
        ghs_cache.put_index_row({"retrieval_id": RID, "outcome": "lost"}, self.cache)
        with self.assertRaisesRegex(CacheError, "is not one of"):
            ghs_cache.read_index(self.cache)

    def test_the_index_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """An empty index after a power loss would stop every later download, because an empty
        index has no header."""
        ghs_cache.put_index_row({"retrieval_id": RID, "outcome": "imported"}, self.cache)
        path = self.cache / ghs_cache.INDEX_NAME
        before = path.stat().st_ino
        calls = synced_writes(lambda: ghs_cache.put_index_row(
            {"retrieval_id": "2026-10-05T120000Z", "outcome": "retrieved"}, self.cache),
            self.cache)
        after = path.stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(calls, [("file", after), ("rename", after, path.name),
                                 ("directory", True)])
        self.assertEqual([row["retrieval_id"] for row in ghs_cache.read_index(self.cache)],
                         [RID, "2026-10-05T120000Z"])
        self.assertEqual([p.name for p in self.cache.iterdir()], [ghs_cache.INDEX_NAME])

    def test_no_index_is_an_empty_cache(self):
        self.assertEqual(ghs_cache.read_index(self.cache), [])
        self.assertIsNone(ghs_cache.latest_retrieval(self.cache))


class TestPointer(_CacheCase):

    def test_the_pointer_contains_only_the_id(self):
        path = self.tmp / "ghs_retrieval.json"
        ghs_cache.write_pointer(RID, path)
        first = path.read_bytes()
        ghs_cache.write_pointer(RID, path)
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(ghs_cache.read_pointer(path), {"retrieval_id": RID})

    def test_the_pointer_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        path = self.tmp / "ghs_retrieval.json"
        ghs_cache.write_pointer(RID, path)
        before = path.stat().st_ino
        calls = synced_writes(lambda: ghs_cache.write_pointer("2026-10-05T120000Z", path),
                              self.tmp)
        after = path.stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(calls, [("file", after), ("rename", after, path.name),
                                 ("directory", True)])
        self.assertEqual(ghs_cache.read_pointer(path), {"retrieval_id": "2026-10-05T120000Z"})
        self.assertEqual([p.name for p in self.tmp.iterdir()], [path.name])

    def test_no_pointer_is_none_and_a_bad_one_is_refused(self):
        path = self.tmp / "ghs_retrieval.json"
        self.assertIsNone(ghs_cache.read_pointer(path))
        path.write_text('{"retrieval_id": "yesterday"}')
        with self.assertRaisesRegex(CacheError, r"^the pointer .+ names no retrieval \("):
            ghs_cache.read_pointer(path)
        with self.assertRaisesRegex(CacheError,
                                    r"^the pointer .+ does not exist\. Run the download, "
                                    r"1_download_ghs\.py, first$"):
            ghs_cache.pointed_retrieval(self.tmp / "absent.json", self.cache)


class TestImport(_CacheCase):

    def _search_result(self):
        path = self.tmp / "ghs_complete.json"
        path.write_text(json.dumps({
            "_metadata": {"downloaded_at": "2026-03-16T18:07:01.920742Z",
                          "download_duration_seconds": 4416.1,
                          "resolved_filters": {"contributors_min": 2, "created_max": "2024-09-16",
                                               "committed_min": "2025-09-16",
                                               "has_license": True}},
            "parameters": {"contributorsMin": 2},
            "items": [{"id": 0, "name": "a/b", "metrics": [{"language": "Z"}, {"language": "A"}]},
                      {"id": 5, "name": "c/d", "metrics": []}],
        }))
        return path

    def test_an_earlier_search_result_becomes_an_imported_retrieval(self):
        row = ghs_cache.import_search_result(self._search_result(), "2026-03-16", self.cache)
        self.assertEqual(row["retrieval_id"], RID)
        self.assertEqual(row["outcome"], "imported")
        self.assertEqual(row["finished_at"], "2026-03-16T19:20:37Z")
        self.assertEqual((row["created_max"], row["committed_min"]), ("2024-09-16", "2025-09-16"))
        self.assertEqual(json.loads(row["query"]), {"contributorsMin": 2, "createdMax": "2024-09-16",
                                                    "committedMin": "2025-09-16", "hasLicense": True})
        self.assertTrue(row["imported_from"].startswith("ghs_complete.json sha256:"))
        rows = list(ghs_cache.read_rows(self.cache, RID, "ghs", row["ghs_file_sha256"]))
        self.assertEqual([r["id"] for r in rows], [0, 5])
        self.assertEqual([m["language"] for m in rows[0]["metrics"]], ["A", "Z"])

    def test_a_search_result_is_not_imported_twice(self):
        ghs_cache.import_search_result(self._search_result(), "2026-03-16", self.cache)
        with self.assertRaisesRegex(CacheError, "already has retrieval"):
            ghs_cache.import_search_result(self._search_result(), "2026-03-16", self.cache)

    def test_the_command_line_reports_a_bad_end_date(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = ghs_cache.main(["import", str(self._search_result()), "--end-date", "March",
                                   "--cache-dir", str(self.cache)])
        self.assertEqual(code, 1)
        self.assertIn("ERROR ghs_cache: Could not import", err.getvalue())


if __name__ == "__main__":
    unittest.main()
