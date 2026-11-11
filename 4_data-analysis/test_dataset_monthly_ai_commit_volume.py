"""
Tests for dataset/monthly_ai_commit_volume.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. _parse_args is
    tested on its own. The tests check the month bucketing, the start filter and the dropped
    rows by running the function over a commit CSV and a repos.csv built in a temporary
    directory. Pyplot's `subplots` is replaced by a recording stand-in from which the plotted
    months, the counts and the title can be read back. The real pyplot is used once to check
    the PDF written at the given path.
  - The fixture's archive under testdata/output is read through _shared.read_commits_csv,
    which needs 7zz. The cases over the fixture skip without 7zz, and their commit and
    repository counts are computed from the fixture by hand.

Requirements: Python 3.14 (pandas, matplotlib and seaborn, and 7zz on PATH for the fixture cases)
Run with:  python3 -m unittest test_dataset_monthly_ai_commit_volume -v
"""

import contextlib
import csv
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import warnings
from pathlib import Path
from unittest import mock
from agentcontext import SEVEN_ZIP, logs

HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE / "dataset"
SCRIPT = SCRIPT_DIR / "monthly_ai_commit_volume.py"
FIXTURE = HERE / "testdata" / "output"
for _p in (HERE, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
os.environ.setdefault("MPLBACKEND", "Agg")


def _has(*names) -> bool:
    for name in names:
        if importlib.util.find_spec(name) is None:
            return False
    return True


DEPS = _has("pandas", "matplotlib", "seaborn")
HAS_7Z = shutil.which(SEVEN_ZIP) is not None


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("monthly_ai_commit_volume_under_test", SCRIPT) if DEPS else None
if DEPS:
    import pandas as pd

COLUMNS = ["repo_name", "commit_timestamp", "branch", "commit_sha", "github_link",
           "commit_message", "ai_tool", "matched_heuristics", "attributions"]


def _write_commits(path: Path, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        for repo, stamp in rows:
            writer.writerow([repo, stamp, "main", "abc", "https://x", "msg", "Claude", "r"])


def _write_repos(path: Path, scanned: list) -> None:
    """A repos.csv with one row per `scanned_at` value, the column that the script reads."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["repo_name", "scanned_at", "branch", "head_sha", "head_commit_date"])
        for i, stamp in enumerate(scanned):
            writer.writerow([f"o/r{i}", stamp, "main", "a" * 40, "2025-01-01T00:00:00Z"])


class _Recording:
    """pyplot's subplots, tight_layout and close replaced for one call."""

    def __enter__(self):
        self.fig, self.ax = mock.MagicMock(name="fig"), mock.MagicMock(name="ax")
        self.patchers = [
            mock.patch("matplotlib.pyplot.subplots", return_value=(self.fig, self.ax)),
            mock.patch("matplotlib.pyplot.tight_layout"),
            mock.patch("matplotlib.pyplot.close"),
        ]
        for p in self.patchers:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patchers:
            p.stop()
        return False

    @property
    def title(self) -> str:
        return self.ax.set_title.call_args.args[0]

    @property
    def plotted(self) -> tuple:
        months, counts = self.ax.plot.call_args.args[:2]
        return [m.strftime("%Y-%m") for m in months], [int(c) for c in counts]


@unittest.skipUnless(DEPS, "pandas, matplotlib and seaborn are needed")
class TestParseArgs(unittest.TestCase):

    def test_the_defaults(self):
        args = mod._parse_args([])
        self.assertEqual(args.data_dir, str(mod.DATA_DIR))
        self.assertEqual(args.output_path, str(mod.OUTPUT_DIR / "monthly_ai_commit_volume.pdf"))
        self.assertEqual(args.plot_start, "2025-01-01")
        self.assertFalse(args.show)

    def test_every_flag(self):
        args = mod._parse_args(["--data-dir", "/d", "--output-path", "/o/x.pdf",
                                "--plot-start", "2024-06-01", "--show"])
        self.assertEqual((args.data_dir, args.output_path, args.plot_start, args.show),
                         ("/d", "/o/x.pdf", "2024-06-01", True))

    def test_an_abbreviated_flag_is_refused(self):
        """The script takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with self.assertRaises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()):
            mod._parse_args(["--plot-s", "2024-06-01"])
        self.assertEqual(stop.exception.code, 2)


@unittest.skipUnless(DEPS, "pandas, matplotlib and seaborn are needed")
class TestTheCounts(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        _write_commits(self.tmp / "all_ai_commits.csv", [
            ("o/a", "2024-12-31T23:59:59Z"),
            ("o/a", "2025-01-15T10:00:00Z"),
            ("o/b", "2025-01-20T10:00:00Z"),
            ("o/a", "2025-03-01T00:00:00Z"),
            ("o/d", "2025-04-02T00:00:00Z"),
            ("o/c", "not a date"),
            ("o/c", ""),
        ])
        # The collection scanned its repositories on two days of April 2025.
        _write_repos(self.tmp / "repos.csv", ["2025-04-04T08:00:00Z", "2025-04-05T00:00:00Z"])

    def _run(self, **kwargs) -> _Recording:
        with _Recording() as rec, contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            # The script's own warning: pandas warns that the period conversion drops the zone.
            warnings.simplefilter("ignore")
            mod.monthly_ai_commit_volume(self.tmp, output_path=self.tmp / "x.pdf", **kwargs)
        return rec

    def test_rows_before_the_start_and_rows_without_a_date_are_left_out(self):
        """February has no commit and is plotted as a zero, because the line would otherwise
        run from January straight to March."""
        rec = self._run()
        self.assertEqual(rec.title, "Monthly Volume of AI Commits Since 2025 Through "
                                    "March 2025 (n = 3 commits from 2 repositories)")
        self.assertEqual(rec.plotted, (["2025-01", "2025-02", "2025-03"], [2, 0, 1]))

    def test_the_month_of_the_latest_scan_is_left_out(self):
        """The month of the latest scan is complete only for the repositories scanned after
        that month ended, because the collection reads a repository's commits when it scans
        the repository."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rec = self._run()
        self.assertNotIn("2025-04", rec.plotted[0])
        self.assertNotIn("o/d", rec.title)
        self.assertIn("INFO dataset_monthly_ai_commit_volume: 0 commits dated after the latest "
                      "scan (2025-04-05 00:00:00 UTC) were left out", err.getvalue())
        self.assertIn("INFO dataset_monthly_ai_commit_volume: 1 commits of April 2025, the month "
                      "of the latest scan, were left out", err.getvalue())

    def test_the_months_before_the_month_of_the_latest_scan_are_plotted_with_their_zeros(self):
        """The window ends at the month before the month of the latest scan. April and May
        have no commit and are plotted as zeros."""
        _write_commits(self.tmp / "all_ai_commits.csv", [
            ("o/a", "2025-01-15T10:00:00Z"), ("o/a", "2025-03-01T00:00:00Z"),
            ("o/b", "2025-06-10T00:00:00Z")])
        _write_repos(self.tmp / "repos.csv", ["2025-06-20T00:00:00Z"])
        rec = self._run()
        self.assertEqual(rec.plotted, (["2025-01", "2025-02", "2025-03", "2025-04", "2025-05"],
                                       [1, 0, 1, 0, 0]))
        self.assertIn("Since 2025 Through May 2025 (n = 2 commits from 1 repositories)", rec.title)

    def test_a_commit_dated_after_the_latest_scan_does_not_move_the_window(self):
        """`commit_timestamp` is the author date, which the author sets. A commit dated months
        after the collection would end the window in the month before its own, with the
        incomplete month of the collection and zeros after it. The script leaves such a commit
        out with a count, and the window ends at the month before the month of the latest
        scan."""
        _write_commits(self.tmp / "all_ai_commits.csv",
                       [("o/a", f"2026-{m:02d}-10T10:00:00Z") for m in range(1, 10)]
                       + [("o/b", "2026-10-02T10:00:00Z"), ("o/c", "2027-03-01T00:00:00Z")])
        _write_repos(self.tmp / "repos.csv", ["2026-10-05T06:00:00Z", "2026-10-06T22:38:36Z"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rec = self._run(plot_start="2026-01-01")
        self.assertEqual(rec.plotted, ([f"2026-{m:02d}" for m in range(1, 10)], [1] * 9))
        self.assertIn("Since 2026 Through September 2026 (n = 9 commits from 1 repositories)",
                      rec.title)
        self.assertIn("1 commits dated after the latest scan (2026-10-06 22:38:36 UTC) were left "
                      "out", err.getvalue())
        self.assertIn("1 commits of October 2026, the month of the latest scan, were left out",
                      err.getvalue())

    def test_a_scan_without_a_time_bounds_the_commits_at_the_end_of_its_day(self):
        """The collection of March 2026 recorded the date alone. Read as midnight, that date
        would place the commits of the same day after the scan."""
        _write_repos(self.tmp / "repos.csv", ["2025-04-02", "2025-03-30T12:00:00Z"])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rec = self._run()
        self.assertEqual(rec.plotted, (["2025-01", "2025-02", "2025-03"], [2, 0, 1]))
        self.assertIn("0 commits dated after the latest scan (2025-04-02 23:59:59 UTC)",
                      err.getvalue())
        self.assertIn("1 commits of April 2025, the month of the latest scan, were left out",
                      err.getvalue())

    def test_repos_without_a_readable_scan_are_refused(self):
        """Without a scan, the month in which the collection ended is unknown, and no window
        could end before it."""
        _write_repos(self.tmp / "repos.csv", ["", "not a date"])
        with self.assertRaises(SystemExit) as caught:
            self._run()
        self.assertIn("has no readable scanned_at", str(caught.exception))

    def test_the_rows_without_a_date_are_counted_in_the_log(self):
        """The rows without a date are logged with a count, as the adoption timeline logs its
        undated rows."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self._run()
        self.assertIn("INFO dataset_monthly_ai_commit_volume: 2 commits without a readable timestamp "
                      "were left out", err.getvalue())

    def test_an_earlier_start_admits_the_earlier_row(self):
        """The title names the start rather than "Since 2025" whatever the start."""
        rec = self._run(plot_start="2024-01-01")
        self.assertIn("Since 2024 Through March 2025 (n = 4 commits from 2 repositories)", rec.title)
        months = [f"2024-{m:02d}" for m in range(1, 13)] + ["2025-01", "2025-02", "2025-03"]
        self.assertEqual(rec.plotted, (months, [0] * 11 + [1, 2, 0, 1]))

    def test_a_start_inside_a_month_leaves_that_whole_month_out(self):
        """Each row is truncated to its month before the comparison. January's rows are then
        dated the first of January, and a start on the 20th is after them."""
        rec = self._run(plot_start="2025-01-20")
        self.assertIn("Since February 2025 Through March 2025 (n = 1 commits from 1 repositories)",
                      rec.title)
        self.assertEqual(rec.plotted, (["2025-02", "2025-03"], [0, 1]))

    def test_the_figure_is_written_at_the_given_path(self):
        target = self.tmp / "figures" / "volume.pdf"
        target.parent.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mod.monthly_ai_commit_volume(self.tmp, output_path=target)
        self.assertTrue(target.read_bytes().startswith(b"%PDF"))

    def test_the_commit_messages_are_not_held_in_memory(self):
        """The figure needs only the repository and the date of each commit, while the commit
        messages make up most of the commit CSV. The run therefore allocates less than half of
        the size of the messages. A first run over the small CSV loads the plotting modules,
        and their allocations are no part of the measured run."""
        self._run()
        count, length = 2_000, 10_000
        with open(self.tmp / "all_ai_commits.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(COLUMNS)
            for i in range(count):
                message = f"{i:06d}" + "m" * (length - 6)
                writer.writerow([f"o/r{i % 7}", "2025-02-10T10:00:00Z", "main", f"{i:040x}",
                                 "https://x", message, "Claude", "r", "standard"])
        tracemalloc.start()
        try:
            rec = self._run()
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(rec.plotted, (["2025-01", "2025-02", "2025-03"], [0, count, 0]))
        self.assertLess(peak, count * length // 2)

    def test_a_missing_commit_csv_is_refused(self):
        (self.tmp / "all_ai_commits.csv").unlink()
        with self.assertRaises(SystemExit):
            mod.monthly_ai_commit_volume(self.tmp, output_path=self.tmp / "x.pdf")


@unittest.skipUnless(DEPS and HAS_7Z, "pandas, matplotlib, seaborn and 7zz are needed")
class TestOverTheFixture(unittest.TestCase):
    """The fixture's all_ai_commits.csv, counted by hand, has 6,897 rows over 27
    repositories. One row is dated before 2025, in September 2024, none in January 2025, and
    the last on 2026-09-12. The latest scan of the fixture's repos.csv is on 2026-09-15, and
    the 202 rows of September 2026, the month of the latest scan, are left out."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.before = {p.name for p in SCRIPT_DIR.iterdir()}

    def test_the_window_since_2025(self):
        with _Recording() as rec, contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mod.monthly_ai_commit_volume(FIXTURE, output_path=self.tmp / "x.pdf")
        self.assertIn("Since 2025 Through August 2026 (n = 6,694 commits from 27 repositories)",
                      rec.title)
        months, counts = rec.plotted
        self.assertEqual(sum(counts), 6694)
        self.assertEqual(months, [f"2025-{m:02d}" for m in range(1, 13)]
                         + [f"2026-{m:02d}" for m in range(1, 9)])
        self.assertEqual(counts[:3], [0, 1, 13])

    def test_main_returns_0_and_writes_the_figure(self):
        """main honors --plot-start, beyond returning 0 and writing a PDF."""
        argv = ["--data-dir", str(FIXTURE), "--output-path", str(self.tmp / "unused.pdf"),
                "--plot-start", "2024-06-01"]
        with _Recording() as rec, contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            self.assertEqual(mod.main(argv), 0)
        self.assertIn("Since June 2024 Through August 2026 (n = 6,695 commits from 27 repositories)",
                      rec.title)
        months, counts = rec.plotted
        self.assertEqual(months[:5], ["2024-06", "2024-07", "2024-08", "2024-09", "2024-10"])
        self.assertEqual(counts[:5], [0, 0, 0, 1, 0])
        self.assertEqual((len(months), months[-1], sum(counts)), (27, "2026-08", 6695))
        target = self.tmp / "volume.pdf"
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            code = mod.main(["--data-dir", str(FIXTURE), "--output-path", str(target)])
        self.assertEqual(code, 0)
        self.assertTrue(target.read_bytes().startswith(b"%PDF"))

    def test_the_environment_variables_route_a_run(self):
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(self.tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(SCRIPT_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        pdf = self.tmp / "dataset" / "monthly_ai_commit_volume.pdf"
        self.assertIn(f"Wrote {pdf}", logs.messages(result.stderr))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)
        self.assertFalse((FIXTURE / "all_ai_commits.csv").exists(), "the archive was extracted beside itself")


if __name__ == "__main__":
    unittest.main()
