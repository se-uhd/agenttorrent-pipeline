"""
Tests for rq3/count_distribution.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. build_distribution
    is tested over Series built here and count_per_repo over a CSV in a temporary directory.
  - main() runs in-process over small CSVs with DATA_DIR and OUTPUT_DIR replaced, and over
    the fixture under testdata/output, with pyplot's `subplots` wrapped so that the bar
    heights, the bin labels and the legend are read back off the axes. It also runs once as a
    subprocess over the fixture, where the repository counts and the largest count per
    repository are computed from the fixture by hand.

Requirements: Python 3.14 (pandas, numpy, matplotlib)
Run with:  python3 -m unittest test_rq3_count_distribution -v
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
SCRIPT = SCRIPT_DIR / "count_distribution.py"
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


DEPS = _has("pandas", "numpy", "matplotlib")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("count_distribution_under_test", SCRIPT) if DEPS else None
if DEPS:
    import pandas as pd


def _draw(data_dir: Path, out_dir: Path) -> tuple:
    """Run main() over the CSVs in `data_dir`, and return its log messages,
    {legend label: bar heights} and the x tick labels, read off the axes that it drew on."""
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
    ax = drawn["ax"]
    bars = {container.get_label(): [bar.get_height() for bar in container] for container in ax.containers}
    return logs.messages(out.getvalue()), bars, [label.get_text() for label in ax.get_xticklabels()]


def _write_csv(path: Path, header: list, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


@unittest.skipUnless(DEPS, "pandas, numpy and matplotlib are needed")
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


class TestBuildDistribution(_Tmp):

    def test_counts_above_seven_share_the_last_bin(self):
        dist = mod.build_distribution(pd.Series([1, 1, 3, 9, 12]))
        self.assertEqual(list(dist.index), list(range(1, 9)))
        self.assertEqual(dist.to_dict(), {1: 2, 2: 0, 3: 1, 4: 0, 5: 0, 6: 0, 7: 0, 8: 2})

    def test_every_bin_is_present_and_the_bins_sum_to_the_repositories(self):
        counts = pd.Series([7, 8, 2, 2, 2, 100])
        dist = mod.build_distribution(counts)
        self.assertEqual(len(dist), 8)
        self.assertEqual(dist.sum(), len(counts))
        self.assertEqual(dist[7], 1)
        self.assertEqual(dist[8], 2)

    def test_the_boundary_is_max_individual(self):
        self.assertEqual(mod.MAX_INDIVIDUAL, 7)
        dist = mod.build_distribution(pd.Series([mod.MAX_INDIVIDUAL, mod.MAX_INDIVIDUAL + 1]))
        self.assertEqual(dist[mod.MAX_INDIVIDUAL], 1)
        self.assertEqual(dist[mod.MAX_INDIVIDUAL + 1], 1)

    def test_no_repositories_is_all_zeros(self):
        dist = mod.build_distribution(pd.Series([], dtype="int64"))
        self.assertEqual(list(dist), [0] * 8)


class TestCountPerRepo(_Tmp):

    def test_rows_per_repository(self):
        _write_csv(self.data / "skills.csv", ["repo_name", "skills.md", "name"], [
            ("o/a", ".claude/skills/x/SKILL.md", "x"),
            ("o/a", ".claude/skills/y/SKILL.md", "y"),
            ("o/b", ".claude/skills/z/SKILL.md", "z"),
        ])
        counts = mod.count_per_repo(self.data / "skills.csv", "skills.md")
        self.assertEqual(counts.to_dict(), {"o/a": 2, "o/b": 1})

    def test_the_item_column_must_be_there(self):
        _write_csv(self.data / "skills.csv", ["repo_name", "path"], [("o/a", "x")])
        with self.assertRaises(ValueError):
            mod.count_per_repo(self.data / "skills.csv", "skills.md")


class TestMainOverSmallMechanismCsvs(_Tmp):

    def test_the_summary_and_the_figure(self):
        """The test reads back the bars, their bin labels and the legend, and it fails for
        shares left as fractions or for mislabeled bins."""
        _write_csv(self.data / "context-files.csv", ["repo_name", "context_file"],
                   [("o/a", "CLAUDE.md"), ("o/a", "AGENTS.md"), ("o/b", "CLAUDE.md")])
        _write_csv(self.data / "skills.csv", ["repo_name", "skills.md"],
                   [(f"o/a", f".claude/skills/{i}/SKILL.md") for i in range(9)])
        _write_csv(self.data / "subagents.csv", ["repo_name", "subagent"],
                   [("o/c", ".claude/agents/one.md")])
        printed, bars, labels = _draw(self.data, self.out)
        # o/a has two context files and o/b one. o/a has nine skills and o/c one subagent.
        self.assertEqual(labels, ["1", "2", "3", "4", "5", "6", "7", "8+"])
        self.assertEqual(bars, {"Context Files (n=2)": [50.0, 50.0, 0, 0, 0, 0, 0, 0],
                                "Skills (n=1)": [0, 0, 0, 0, 0, 0, 0, 100.0],
                                "Subagents (n=1)": [100.0, 0, 0, 0, 0, 0, 0, 0]})
        self.assertIn("Context Files (n=2)  min=1, max=2, median=2, mean=1.50", printed)
        self.assertIn("Skills (n=1)  min=9, max=9, median=9, mean=9.00", printed)
        self.assertIn("Subagents (n=1)  min=1,", printed)
        pdf = self.out / "count_distribution.pdf"
        self.assertTrue(pdf.is_file())
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))


@unittest.skipUnless(DEPS, "pandas, numpy and matplotlib are needed")
class TestMainOverTheFixture(unittest.TestCase):
    """The expected values are computed from the fixture by hand: the distinct repositories
    per CSV and the largest row count that one repository has in the CSV."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.before = {p.name for p in SCRIPT_DIR.iterdir()}

    def test_the_bars_over_the_fixture(self):
        """Rows per repository, counted from the fixture: context files 13, 3, 4, 3, 1, 2, 0
        and 5 repositories with 1 to 7 and 8 or more rows, of 31; skills 1, 2, 0, 3, 0, 0, 1
        and 10 of 17; subagents 1, 1, 3, 1, 1, 0, 0 and 2 of 9."""
        _, bars, labels = _draw(FIXTURE, self.tmp)
        self.assertEqual(labels, ["1", "2", "3", "4", "5", "6", "7", "8+"])
        expected = {"Context Files (n=31)": ([13, 3, 4, 3, 1, 2, 0, 5], 31),
                    "Skills (n=17)": ([1, 2, 0, 3, 0, 0, 1, 10], 17),
                    "Subagents (n=9)": ([1, 1, 3, 1, 1, 0, 0, 2], 9)}
        self.assertEqual(list(bars), list(expected))
        for label, (counts, n) in expected.items():
            with self.subTest(series=label):
                for height, count in zip(bars[label], counts, strict=True):
                    self.assertAlmostEqual(height, count / n * 100)

    def test_the_run(self):
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(self.tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(SCRIPT_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("Context Files (n=31)  min=1, max=39,", logs.messages(result.stderr))
        self.assertIn("Skills (n=17)  min=1, max=24,", logs.messages(result.stderr))
        self.assertIn("Subagents (n=9)  min=1, max=10,", logs.messages(result.stderr))
        pdf = self.tmp / "rq3" / "count_distribution.pdf"
        self.assertTrue(pdf.is_file(), logs.messages(result.stderr))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)


if __name__ == "__main__":
    unittest.main()
