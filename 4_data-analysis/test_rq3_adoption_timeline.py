"""
Tests for rq3/adoption_timeline.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. The two pure
    steps, the earliest month per repository and the cumulative count, are tested over
    Series and CSVs built in a temporary directory, with the module's DATA_DIR and
    OUTPUT_DIR replaced so that nothing is read from or written beside the script.
  - The style guard is tested by removing a key from each style map.
  - main() runs in-process over small CSVs and over the fixture under testdata/output, with
    pyplot's `subplots` wrapped so that every plotted line and the title are read back off
    the axes. It also runs once as a subprocess over the fixture, with
    DATA_COLLECTION_OUTPUT and ANALYSIS_OUTPUT_DIR set. That run checks the per-series
    repository counts, computed from the fixture by hand, and the PDF written under the
    temporary root.

Requirements: Python 3.14 (pandas, matplotlib)
Run with:  python3 -m unittest test_rq3_adoption_timeline -v
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
import unittest
from pathlib import Path
from unittest import mock
from agentcontext import logs

HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE / "rq3"
SCRIPT = SCRIPT_DIR / "adoption_timeline.py"
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


DEPS = _has("pandas", "matplotlib")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("adoption_timeline_under_test", SCRIPT) if DEPS else None
if DEPS:
    import pandas as pd


def _run_main(data_dir: Path, out_dir: Path) -> tuple:
    """Run `main()` over the CSVs in `data_dir` with the real pyplot, and return its log
    messages and the axes that it drew on, which keep their lines and title after the figure
    is closed."""
    drawn = {}
    real_subplots = mod.plt.subplots

    def subplots(*args, **kwargs):
        fig, ax = real_subplots(*args, **kwargs)
        drawn["ax"] = ax
        return fig, ax

    out = io.StringIO()
    with mock.patch.object(mod, "DATA_DIR", data_dir), mock.patch.object(mod, "OUTPUT_DIR", out_dir), \
            mock.patch.object(mod.plt, "subplots", subplots), contextlib.redirect_stderr(out):
        mod.main()
    return logs.messages(out.getvalue()), drawn["ax"]


def _repositories_per_series(printed: str) -> dict:
    """{series: repositories} as the logged table of the unique repositories per series of the
    adoption timeline gives them."""
    table = printed.split("Unique repositories per series of the adoption timeline.\n", 1)[1]
    found = {}
    for line in table.splitlines():
        if not line.startswith("  "):
            break
        label, count = line.strip().rsplit(" ", 1)
        found[label.strip()] = int(count.replace(",", ""))
    return found


def _lines(ax) -> dict:
    """{label: [(month, cumulative count), ...]} for every line on the axes."""
    return {line.get_label(): [(pd.Timestamp(x).strftime("%Y-%m"), int(y))
                               for x, y in zip(line.get_xdata(), line.get_ydata())]
            for line in ax.get_lines()}


def _write_csv(path: Path, header: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


@unittest.skipUnless(DEPS, "pandas and matplotlib are needed")
class _Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()
        for name, value in (("DATA_DIR", self.data), ("OUTPUT_DIR", self.out)):
            patcher = mock.patch.object(mod, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def quietly(self, fn, *args):
        """Call a loader with its standard output kept out of the test output."""
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args)


class TestLoadFirstAdoption(_Tmp):

    def test_the_earliest_row_per_repository_floored_to_its_month(self):
        _write_csv(self.data / "skills.csv", ["repo_name", "created_at", "extra"], [
            ("o/a", "2025-03-20T10:00:00Z", "x"),
            ("o/a", "2025-01-15T08:00:00Z", "x"),
            ("o/b", "2025-06-30T23:59:59Z", "x"),
        ])
        first = self.quietly(mod.load_first_adoption, self.data / "skills.csv")
        self.assertEqual(list(first.index), ["o/a", "o/b"])
        self.assertEqual(first["o/a"], pd.Timestamp("2025-01-01"))
        self.assertEqual(first["o/b"], pd.Timestamp("2025-06-01"))

    def test_the_result_has_no_time_zone(self):
        _write_csv(self.data / "skills.csv", ["repo_name", "created_at"],
                   [("o/a", "2025-01-15T08:00:00+02:00")])
        first = self.quietly(mod.load_first_adoption, self.data / "skills.csv")
        self.assertIsNone(first.dt.tz)
        self.assertEqual(first["o/a"], pd.Timestamp("2025-01-01"))

    def test_an_offset_is_read_in_utc_before_the_month_is_taken(self):
        """A row written at 01:00 on the first with a +02:00 offset is 23:00 on the last of
        the previous month in UTC, and the row counts in that month."""
        _write_csv(self.data / "skills.csv", ["repo_name", "created_at"],
                   [("o/a", "2025-02-01T01:00:00+02:00")])
        first = self.quietly(mod.load_first_adoption, self.data / "skills.csv")
        self.assertEqual(first["o/a"], pd.Timestamp("2025-01-01"))


class TestLoadContextFileCategories(_Tmp):

    def test_each_category_is_its_own_series_and_an_empty_one_is_left_out(self):
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file", "created_at"], [
            ("o/a", "CLAUDE.md", "2025-02-10T00:00:00Z"),
            ("o/a", "docs/CLAUDE.md", "2025-01-10T00:00:00Z"),
            ("o/b", "AGENTS.md", "2025-03-10T00:00:00Z"),
            ("o/b", ".github/instructions/api.instructions.md", "2025-03-11T00:00:00Z"),
        ])
        found = self.quietly(mod.load_context_file_categories)
        self.assertEqual(list(found), ["CLAUDE.md", "AGENTS.md", "Copilot Instructions"])
        self.assertEqual(found["CLAUDE.md"]["o/a"], pd.Timestamp("2025-01-01"))
        self.assertEqual(list(found["AGENTS.md"].index), ["o/b"])

    def test_the_families_are_those_of_agentconfigs_1_0_0_in_figure_order(self):
        """The timeline keeps the five context file categories of the timeline of
        AgentConfigs 1.0.0. A Gemini style guide, a BUGBOT.md, a REVIEW.md and `.codex/CODEX.md`,
        which a configuration named, are context files of no plotted family."""
        rows = [(f"o/{i}", path, "2025-01-01T00:00:00Z")
                for i, path in enumerate(["CLAUDE.md", "AGENTS.md", "GEMINI.md",
                                          ".github/copilot-instructions.md", ".cursorrules",
                                          ".gemini/styleguide.md", ".cursor/BUGBOT.md",
                                          "REVIEW.md", ".codex/CODEX.md"])]
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file", "created_at"], rows)
        self.assertEqual(tuple(self.quietly(mod.load_context_file_categories)),
                         ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "Copilot Instructions",
                          ".cursorrules"))


class TestComputeCumulative(_Tmp):

    def test_one_entry_per_month_and_the_last_is_the_repository_count(self):
        first = pd.Series(pd.to_datetime(["2025-01-01", "2025-01-01", "2025-03-01", "2025-02-01"]),
                          index=["a", "b", "c", "d"])
        cumulative = mod.compute_cumulative(first)
        self.assertEqual(list(cumulative.index), list(pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"])))
        self.assertEqual(list(cumulative), [2, 3, 4])
        self.assertEqual(cumulative.iloc[-1], len(first))

    def test_a_month_no_repository_adopted_in_is_not_a_row(self):
        first = pd.Series(pd.to_datetime(["2025-01-01", "2025-04-01"]), index=["a", "b"])
        self.assertEqual(len(mod.compute_cumulative(first)), 2)

    def test_the_count_never_decreases(self):
        first = pd.Series(pd.to_datetime(["2025-05-01", "2024-12-01", "2025-05-01", "2025-01-01"]),
                          index=list("abcd"))
        values = list(mod.compute_cumulative(first))
        self.assertEqual(values, sorted(values))


class TestTheStyleGuard(_Tmp):

    def test_the_shipped_maps_cover_every_series(self):
        mod._assert_every_series_has_a_style()

    def test_a_series_without_a_marker_is_refused_before_plotting(self):
        with mock.patch.dict(mod.MARKERS):
            del mod.MARKERS["Skills"]
            with self.assertRaises(KeyError) as caught:
                mod._assert_every_series_has_a_style()
        self.assertIn("MARKERS", str(caught.exception))
        self.assertIn("Skills", str(caught.exception))

    def test_a_series_without_a_color_is_refused_before_plotting(self):
        with mock.patch.dict(mod.COLORS):
            del mod.COLORS["Planning Documents"]
            with self.assertRaises(KeyError) as caught:
                mod._assert_every_series_has_a_style()
        self.assertIn("COLORS", str(caught.exception))

    def test_every_mechanism_csv_has_a_style(self):
        for label in mod.MECHANISM_CSVS:
            self.assertIn(label, mod.MARKERS)
            self.assertIn(label, mod.COLORS)


class TestMainOverSmallMechanismCsvs(_Tmp):

    def run_main(self) -> tuple:
        return _run_main(self.data, self.out)

    def write_empty_families(self) -> None:
        for name in ("skills.csv", "subagents.csv", "planning-documents.csv"):
            _write_csv(self.data / name, ["repo_name", "created_at"], [])

    def test_a_row_without_a_date_is_left_out_and_counted(self):
        """A repository with only undated AGENTS.md rows is left out of the logged count of
        AGENTS.md repositories, the title's n and the plotted line alike, and the rows left out
        are counted."""
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file", "created_at"], [
            ("o/a", "AGENTS.md", "2025-01-10T00:00:00Z"),
            ("o/b", "AGENTS.md", ""),
            ("o/b", "CLAUDE.md", "2025-02-10T00:00:00Z"),
            ("o/c", "AGENTS.md", ""),
        ])
        self.write_empty_families()
        printed, ax = self.run_main()
        self.assertIn("2 rows of context-files.csv without created_at and 0 created before "
                      "2023-01-01 were left out", printed)
        self.assertIn("0 rows of skills.csv without created_at", printed)
        per_series = _repositories_per_series(printed)
        self.assertEqual((per_series["AGENTS.md"], per_series["CLAUDE.md"]), (1, 1))
        lines = {line.get_label(): list(line.get_ydata()) for line in ax.get_lines()}
        self.assertEqual(lines, {"CLAUDE.md": [1], "AGENTS.md": [1, 1]})
        self.assertTrue(ax.get_title().endswith("in Each Family (n = 2)"), ax.get_title())

    def test_a_file_created_before_the_tools_is_left_out_and_counted(self):
        """created_at is the date of the commit that created a file under whatever name it had
        then. kdeldycke/meta-package-manager created a development document in 2021 and renamed
        it to claude.md and later to AGENTS.md, and mathiasertl/django-ca has documentation from
        2016 that its configuration names. Such a file was not created as a configuration, and
        the timeline leaves it out: the repository is counted in no series and in no n, and the
        axis starts with the first file created from 2023 on."""
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file", "created_at"], [
            ("kdeldycke/meta-package-manager", "claude.md", "2021-10-08T20:16:15Z"),
            ("mathiasertl/django-ca", "docs/source/ca_examples.rst", "2016-04-22T15:55:17Z"),
            ("o/b", "CLAUDE.md", "2025-03-10T00:00:00Z"),
        ])
        self.write_empty_families()
        printed, ax = self.run_main()
        self.assertIn("0 rows of context-files.csv without created_at and 2 created before "
                      "2023-01-01 were left out", printed)
        self.assertEqual(_lines(ax), {"CLAUDE.md": [("2025-03", 1)]})
        self.assertTrue(ax.get_title().endswith("in Each Family (n = 1)"), ax.get_title())
        self.assertNotIn("Adoption", ax.get_title())
        self.assertEqual(ax.get_xlabel(),
                         "Month of the Commit That Created the Earliest File, Under Any "
                         "Earlier Name")

    def test_an_axis_over_years_has_at_most_twelve_labels(self):
        """From January 2023 to October 2026, a label for every month would overlap the next."""
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file", "created_at"], [
            ("o/a", ".cursorrules", "2023-01-20T00:00:00Z"),
            ("o/b", "CLAUDE.md", "2026-10-10T00:00:00Z"),
        ])
        self.write_empty_families()
        _, ax = self.run_main()
        # The locator also places a tick beyond each end of the axis, where matplotlib does not
        # draw it.
        low, high = ax.get_xlim()
        ticks = [mod.mdates.num2date(tick) for tick in ax.get_xticks() if low <= tick <= high]
        self.assertLessEqual(len(ticks), 12, ticks)

    def test_the_summary_and_the_figure(self):
        """The test reads back the plotted lines and the title. It fails for a line that drops
        to zero in a month without an adoption and for a title one repository too high. The
        months with an adoption are January to April 2025. CLAUDE.md has its two repositories from
        February on, AGENTS.md starts in March and Skills in April."""
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file", "created_at"], [
            ("o/a", "CLAUDE.md", "2025-02-10T00:00:00Z"),
            ("o/b", "CLAUDE.md", "2025-01-10T00:00:00Z"),
            ("o/b", "AGENTS.md", "2025-03-10T00:00:00Z"),
        ])
        _write_csv(self.data / "skills.csv", ["repo_name", "created_at"],
                   [("o/c", "2025-04-01T00:00:00Z")])
        _write_csv(self.data / "subagents.csv", ["repo_name", "created_at"], [])
        _write_csv(self.data / "planning-documents.csv", ["repo_name", "created_at"],
                   [("o/d", "2025-04-20T00:00:00Z")])
        printed, ax = self.run_main()
        self.assertEqual(_lines(ax), {
            "CLAUDE.md": [("2025-01", 1), ("2025-02", 2), ("2025-03", 2), ("2025-04", 2)],
            "AGENTS.md": [("2025-03", 1), ("2025-04", 1)],
            "Skills": [("2025-04", 1)],
            "Planning Documents": [("2025-04", 1)],
        })
        self.assertEqual(ax.get_title(), "Cumulative Repositories by the Creation Month of Their "
                                         "Earliest File in Each Family (n = 4)")
        self.assertEqual(_repositories_per_series(printed),
                         {"CLAUDE.md": 2, "AGENTS.md": 1, "Skills": 1, "Subagents": 0,
                          "Planning Documents": 1})
        pdf = self.out / "cumulative_repo_adoption.pdf"
        self.assertTrue(pdf.is_file())
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in self.data.iterdir()},
                         {"context-files.csv", "skills.csv", "subagents.csv",
                          "planning-documents.csv"})


@unittest.skipUnless(DEPS, "pandas and matplotlib are needed")
class TestMainOverTheFixture(unittest.TestCase):
    """The fixture's per-series counts, computed by hand: distinct repositories per plotted
    context file category in context-files.csv, and per CSV for Skills, Subagents and Planning
    Documents, over the dated rows. clickhouse/clickhouse's .cursor/BUGBOT.md is the one row
    without a created_at, and no row was created before 2023."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.before = {p.name for p in SCRIPT_DIR.iterdir()}

    def test_every_line_and_the_title(self):
        """Each series is counted from the fixture's created_at months: the earliest month per
        repository, cumulated over the seventeen months in which some series has an adoption,
        from March 2025 to August 2026 without April 2025, and cut before the series' first
        month. 30 repositories have a dated row in one of the eight series."""
        _, ax = _run_main(FIXTURE, self.tmp)
        lines = {label: [count for _, count in points] for label, points in _lines(ax).items()}
        self.assertEqual(lines, {
            "CLAUDE.md": [1, 2, 2, 5, 6, 7, 7, 8, 9, 11, 12, 14, 15, 17, 18, 18, 18],
            "AGENTS.md": [2, 2, 2, 6, 6, 7, 9, 13, 18, 21, 21, 22, 23, 24, 24],
            "GEMINI.md": [1, 1, 1, 1, 3, 3, 3, 3, 4, 4, 4, 4, 4, 4],
            "Copilot Instructions": [1, 2, 2, 3, 3, 4, 5, 5, 7, 7, 8, 8, 8, 8, 8, 8],
            ".cursorrules": [1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3],
            "Skills": [1, 1, 2, 2, 4, 10, 13, 14, 15, 17, 17, 17],
            "Subagents": [1, 1, 2, 3, 3, 3, 8, 9, 9, 9, 9, 9, 9],
            "Planning Documents": [1, 2, 3],
        })
        months = [month for month, _ in _lines(ax)["CLAUDE.md"]]
        self.assertEqual(months[:3], ["2025-03", "2025-05", "2025-06"])
        self.assertEqual(months[-1], "2026-08")
        self.assertEqual(ax.get_title(), "Cumulative Repositories by the Creation Month of Their "
                                         "Earliest File in Each Family (n = 30)")

    def test_the_run(self):
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(self.tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(SCRIPT_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        for line in ("1 rows of context-files.csv without created_at and 0 created before "
                     "2023-01-01 were left out",
                     "0 rows of skills.csv without created_at and 0 created before 2023-01-01 "
                     "were left out",
                     "0 rows of subagents.csv without created_at and 0 created before "
                     "2023-01-01 were left out",
                     "0 rows of planning-documents.csv without created_at and 0 created before "
                     "2023-01-01 were left out"):
            self.assertIn(line, logs.messages(result.stderr))
        per_series = _repositories_per_series(logs.messages(result.stderr))
        expected = {"CLAUDE.md": 18, "AGENTS.md": 24, "GEMINI.md": 4, "Copilot Instructions": 8,
                    ".cursorrules": 3, "Skills": 17, "Subagents": 9, "Planning Documents": 3}
        self.assertEqual({label: per_series[label] for label in expected}, expected)
        pdf = self.tmp / "rq3" / "cumulative_repo_adoption.pdf"
        self.assertTrue(pdf.is_file(), logs.messages(result.stderr))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)


if __name__ == "__main__":
    unittest.main()
