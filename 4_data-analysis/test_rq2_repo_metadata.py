"""
Tests for rq2/_repo_metadata.py

Strategy:
  - The module is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - `reference_date` is tested over a dict built here.
  - `build_repo_metadata` is tested over a two-input fixture written under a temporary
    directory: a small repos_annotated.csv and a small ghs_filtered.json, reached by
    replacing the module's CLASSIFICATION_DIR, SAMPLING_DIR and OUTPUT_DIR. Neither the
    tracked CSV nor the GHS sampling frame is read.

Requirements: Python 3.14 (pandas, no network)
Run with:  python3 -m unittest test_rq2_repo_metadata -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import pandas as pd
from agentcontext import SEVEN_ZIP, logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_repo_metadata_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rm = _load("_repo_metadata", RQ2 / "_repo_metadata.py")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


DOWNLOADED_AT = "2026-03-16T18:07:01.920742Z"

ITEMS = [
    {"name": "a/one", "mainLanguage": "Rust", "createdAt": "2020-01-01T00:00:00",
     "contributors": 3, "commits": 100, "size": 10},
    {"name": "b/two", "mainLanguage": "Python", "createdAt": "2025-03-06T12:00:00",
     "contributors": 7, "commits": 250, "size": 20},
    {"name": "c/ghs-only", "mainLanguage": "Go", "createdAt": "2021-06-01T00:00:00",
     "contributors": 1, "commits": 5, "size": 1},
    {"name": "d/no-language", "mainLanguage": None, "createdAt": "2022-01-01T00:00:00",
     "contributors": 2, "commits": 50, "size": 5},
]

ANNOTATED = [
    ("a/one", "main", "true"),
    ("b/two", "main", "false"),
    ("d/no-language", "main", "unclear"),
    ("e/annotated-only", "main", "true"),
]


def write_inputs(root: Path, items=ITEMS, annotated=ANNOTATED, downloaded_at=DOWNLOADED_AT) -> None:
    """A small classification CSV and GHS sampling frame under `root`, with the columns of the
    real files."""
    classification = root / "2_classification"
    sampling = root / "1_sampling"
    classification.mkdir(parents=True, exist_ok=True)
    sampling.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(annotated, columns=["repo_name", "default_branch", "engineered_project"])
    # The classification's columns, GitHub's id of each repository among them.
    table.insert(1, "github_id", range(1, len(table) + 1))
    table.assign(justification="text").to_csv(classification / "repos_annotated.csv", index=False)
    ghs = {"_metadata": {"input_source": {"downloaded_at": downloaded_at}}, "items": items}
    (sampling / "ghs_filtered.json").write_text(json.dumps(ghs), encoding="utf-8")


class _Redirected(unittest.TestCase):
    """A temporary directory with both inputs and the output, and the module pointed at it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        write_inputs(self.tmp)
        self.out = self.tmp / "out"
        self.out.mkdir()
        for name, value in (("CLASSIFICATION_DIR", self.tmp / "2_classification"),
                            ("SAMPLING_DIR", self.tmp / "1_sampling"),
                            ("OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, rm, name, getattr(rm, name))
            setattr(rm, name, value)

    def build(self) -> pd.DataFrame:
        with contextlib.redirect_stdout(io.StringIO()):
            return rm.build_repo_metadata()


class TestReferenceDate(unittest.TestCase):

    def test_the_download_day_of_the_snapshot(self):
        ghs = {"_metadata": {"input_source": {"downloaded_at": DOWNLOADED_AT}}}
        self.assertEqual(rm.reference_date(ghs), pd.Timestamp("2026-03-16"))

    def test_the_time_of_day_is_dropped(self):
        ghs = {"_metadata": {"input_source": {"downloaded_at": "2026-03-16T23:59:59Z"}}}
        self.assertEqual(rm.reference_date(ghs), pd.Timestamp("2026-03-16"))

    def test_a_sampling_frame_without_downloaded_at_is_refused(self):
        for ghs in ({}, {"_metadata": {}}, {"_metadata": {"input_source": {}}},
                    {"_metadata": {"input_source": {"downloaded_at": ""}}}):
            with self.subTest(ghs=ghs), self.assertRaises(SystemExit) as caught:
                rm.reference_date(ghs)
            self.assertIn("downloaded_at", str(caught.exception))


class TestBuildRepoMetadata(_Redirected):

    def test_a_join_that_lost_most_of_its_rows_is_refused(self):
        """Every annotated row matches the GHS sampling frame, because the annotated list is
        derived from it. A join that loses most of the rows therefore means that a key stopped
        matching rather than that the sample shrank, and the tracked CSV is left as it was."""
        items = [{"name": f"o/r{i}", "mainLanguage": "Rust",
                  "createdAt": "2020-01-01T00:00:00", "contributors": 1,
                  "commits": 1, "size": 1} for i in range(rm.MIN_ROWS_FOR_SHARE + 1)]
        # The same repositories, with names in another letter case on one side only, which is
        # one way for a real join to lose its rows.
        annotated = [(f"O/R{i}", "main", "true") for i in range(rm.MIN_ROWS_FOR_SHARE + 1)]
        write_inputs(self.tmp, items=items, annotated=annotated)
        target = self.out / "repo_metadata.csv"
        target.write_text("kept\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            rm.build_repo_metadata()
        self.assertIn(f"Only 0 of {rm.MIN_ROWS_FOR_SHARE + 1:,} rows of repos_annotated.csv have "
                      f"a repository name that ghs_filtered.json lists", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "kept\n",
                         "the tracked CSV was replaced by the short join")

    def test_the_repo_metadata_csv_is_replaced_in_one_step(self):
        """A kill inside `to_csv` would leave a truncated CSV that no check can distinguish
        from a short one, over a file that is tracked."""
        target = self.out / "repo_metadata.csv"
        target.write_text("the previous run\n", encoding="utf-8")

        def half_then_die(self_df, path, **kwargs):
            Path(path).write_text("half a tab", encoding="utf-8")
            raise KeyboardInterrupt

        with (mock.patch.object(pd.DataFrame, "to_csv", half_then_die),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaises(KeyboardInterrupt):
                rm.build_repo_metadata()
        self.assertEqual(target.read_text(encoding="utf-8"), "the previous run\n")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["repo_metadata.csv"])

    def test_the_columns_are_the_six_of_the_repo_metadata_csv(self):
        out = self.build()
        self.assertEqual(list(out.columns),
                         ["repo_name", "main_language", "repo_age_days", "contributors",
                          "commits", "size"])

    def test_a_repository_named_twice_is_refused_rather_than_counted_twice(self):
        """A name listed twice in either input would give the repository two rows."""
        write_inputs(self.tmp, items=ITEMS + [ITEMS[0]])
        with self.assertRaises(SystemExit) as stop:
            self.build()
        self.assertIn("do not name each repository once", str(stop.exception))
        write_inputs(self.tmp, annotated=ANNOTATED + [ANNOTATED[0]])
        with self.assertRaises(SystemExit):
            self.build()
        self.assertFalse((self.out / "repo_metadata.csv").exists())

    def test_the_join_is_inner(self):
        out = self.build()
        self.assertEqual(sorted(out["repo_name"]), ["a/one", "b/two", "d/no-language"])

    def test_every_annotated_row_is_kept_whatever_its_label(self):
        """The population is the classified table, not the engineered sample: a `false`
        and an `unclear` row are kept beside the `true` row."""
        out = self.build().set_index("repo_name")
        self.assertIn("b/two", out.index)
        self.assertIn("d/no-language", out.index)

    def test_the_age_is_measured_at_the_download_day(self):
        out = self.build().set_index("repo_name")
        expected = (pd.Timestamp("2026-03-16") - pd.Timestamp("2020-01-01")).days
        self.assertEqual(int(out.loc["a/one", "repo_age_days"]), expected)
        self.assertEqual(int(out.loc["b/two", "repo_age_days"]),
                         (pd.Timestamp("2026-03-16") - pd.Timestamp("2025-03-06T12:00:00")).days)

    def test_a_creation_time_in_utc_with_a_zone_gives_the_same_age(self):
        """The sampling stage writes GitHub's creation time, `2025-03-06T12:00:00Z`, and
        subtracting it from the reference date, which has no zone, would raise TypeError."""
        sampling_frame = self.tmp / "1_sampling" / "ghs_filtered.json"
        data = json.loads(sampling_frame.read_text())
        for item in data["items"]:
            item["createdAt"] += "Z"
        sampling_frame.write_text(json.dumps(data))
        out = self.build().set_index("repo_name")
        self.assertEqual(int(out.loc["b/two", "repo_age_days"]),
                         (pd.Timestamp("2026-03-16") - pd.Timestamp("2025-03-06T12:00:00")).days)

    def test_the_language_column_is_renamed_and_a_missing_language_is_nan(self):
        out = self.build().set_index("repo_name")
        self.assertEqual(out.loc["a/one", "main_language"], "Rust")
        self.assertTrue(pd.isna(out.loc["d/no-language", "main_language"]))
        self.assertNotIn("mainLanguage", out.columns)

    def test_the_ghs_columns_come_across_unchanged(self):
        out = self.build().set_index("repo_name")
        self.assertEqual(int(out.loc["b/two", "contributors"]), 7)
        self.assertEqual(int(out.loc["b/two", "commits"]), 250)
        self.assertEqual(int(out.loc["b/two", "size"]), 20)

    def test_the_csv_is_written_and_equals_the_returned_dataframe(self):
        """The test compares every value and fails for a CSV written with the contributors and
        the size zeroed."""
        out = self.build()
        written = pd.read_csv(self.out / "repo_metadata.csv")
        pd.testing.assert_frame_equal(written, out.reset_index(drop=True))

    def test_the_row_count_is_printed(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            rm.build_repo_metadata()
        self.assertIn("Wrote repo_metadata.csv (3 rows)", logs.messages(buffer.getvalue()))

    def test_a_snapshot_without_a_download_date_is_refused_before_writing(self):
        write_inputs(self.tmp, downloaded_at="")
        with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
            rm.build_repo_metadata()
        self.assertFalse((self.out / "repo_metadata.csv").exists())

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.build()
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestSampleNames(_Redirected):

    def test_the_sample_is_the_rows_labeled_true(self):
        """The sample is what the engineered project selection keeps: `a/one` and
        `e/annotated-only`, and neither the `false` nor the `unclear` row, whether the
        sampling frame lists the repository or not."""
        self.assertEqual(rm.sample_names(), {"a/one", "e/annotated-only"})


@unittest.skipUnless(shutil.which(SEVEN_ZIP) and shutil.which("git"), "7zz or git is not on PATH")
class TestTheTrackedArchives(_Redirected):
    """In a checkout, git tracks `repos_annotated.7z` and `ghs_filtered.json.7z`, and the
    extracted files stay when a pull replaces an archive. The archive here holds other labels or
    another sampling frame than the extracted file beside it."""

    def track(self, archive: Path, name: str, content: bytes) -> None:
        """Pack `content` as `name` into `archive` and add the archive to a git repository at
        the temporary directory."""
        staging = self.tmp / "staging"
        staging.mkdir(exist_ok=True)
        (staging / name).write_bytes(content)
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), name], cwd=str(staging),
                       check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True, capture_output=True)
        subprocess.run(["git", "add", str(archive.relative_to(self.tmp))], cwd=str(self.tmp),
                       check=True, capture_output=True)

    def test_labels_that_differ_from_their_tracked_archive_stop_both_readers(self):
        labels = self.tmp / "2_classification" / "repos_annotated.csv"
        self.track(self.tmp / "2_classification" / "repos_annotated.7z", labels.name,
                   labels.read_bytes().replace(b"false", b"true"))
        for read in (self.build, rm.sample_names):
            with self.subTest(read=read.__name__):
                with self.assertRaises(SystemExit) as caught:
                    read()
                self.assertIn("repos_annotated.7z", str(caught.exception.code))

    def test_a_sampling_frame_that_differs_from_its_tracked_archive_stops(self):
        frame = self.tmp / "1_sampling" / "ghs_filtered.json"
        self.track(self.tmp / "1_sampling" / "ghs_filtered.json.7z", frame.name,
                   frame.read_bytes().replace(b"Rust", b"Java"))
        with self.assertRaises(SystemExit) as caught:
            self.build()
        self.assertIn("ghs_filtered.json.7z", str(caught.exception.code))

    def test_files_that_match_their_tracked_archives_are_read(self):
        for directory, name, archive in (("2_classification", "repos_annotated.csv",
                                          "repos_annotated.7z"),
                                         ("1_sampling", "ghs_filtered.json",
                                          "ghs_filtered.json.7z")):
            path = self.tmp / directory / name
            self.track(path.parent / archive, name, path.read_bytes())
        self.assertEqual(len(self.build()), 3)
        self.assertEqual(rm.sample_names(), {"a/one", "e/annotated-only"})


class TestNoCommandLine(unittest.TestCase):

    def test_the_module_takes_no_arguments(self):
        self.assertNotIn("argparse", Path(rm.__file__).read_text(encoding="utf-8"))

    def test_the_output_directory_is_under_the_override(self):
        self.assertEqual(Path(rm.OUTPUT_DIR).resolve().parent, Path(_MODULE_TMP).resolve())


if __name__ == "__main__":
    unittest.main()
