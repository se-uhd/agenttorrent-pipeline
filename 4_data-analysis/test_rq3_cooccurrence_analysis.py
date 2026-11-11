"""
Tests for rq3/cooccurrence_analysis.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. The three
    classifiers are tested against known paths and against _shared.context_file_category.
  - load_data and the three analyses run in-process over DataFrames and CSVs built here,
    with DATA_DIR and OUTPUT_DIR replaced, and the numbers that each logs are read off its log
    messages. The title of each figure is read off the axes that it was drawn on.
  - One run as a subprocess over the fixture under testdata/output checks the row,
    repository, reference and edge counts computed from the fixture by hand and the three
    PDFs written under the temporary root.

Requirements: Python 3.14 (pandas, numpy, matplotlib, seaborn)
Run with:  python3 -m unittest test_rq3_cooccurrence_analysis -v
"""

import contextlib
import csv
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock
from agentcontext import logs

HERE = Path(__file__).resolve().parent
SCRIPT_DIR = HERE / "rq3"
SCRIPT = SCRIPT_DIR / "cooccurrence_analysis.py"
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


DEPS = _has("pandas", "numpy", "matplotlib", "seaborn")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("cooccurrence_analysis_under_test", SCRIPT) if DEPS else None
if DEPS:
    import pandas as pd

COLUMNS = ["repo_name", "context_file", "created_at", "is_reference", "referenced_file"]


def _frame(rows: list) -> "pd.DataFrame":
    """A DataFrame with the columns that load_data returns: the five columns plus the
    category."""
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    df["category"] = df["context_file"].apply(mod.classify_row)
    return df


@unittest.skipUnless(DEPS, "pandas, numpy, matplotlib and seaborn are needed")
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

    def run_printing(self, fn, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            fn(*args)
        return logs.messages(out.getvalue())

    def run_drawing(self, fn, *args) -> tuple:
        """Run an analysis with the real pyplot, and return its log messages and the axes that
        it drew on, which keep their title and texts after the figure is closed."""
        drawn = {}
        real_subplots = mod.plt.subplots

        def subplots(*a, **kw):
            fig, ax = real_subplots(*a, **kw)
            drawn["ax"] = ax
            return fig, ax

        with mock.patch.object(mod.plt, "subplots", subplots):
            printed = self.run_printing(fn, *args)
        return printed, drawn["ax"]


class TestToBool(_Tmp):

    def test_true_in_any_case_and_nothing_else(self):
        for value, expected in (("TRUE", True), ("true", True), (True, True), ("False", False),
                                ("", False), (None, False), (float("nan"), False), (1, False)):
            with self.subTest(value=value):
                self.assertEqual(mod.to_bool(value), expected)


class TestTheClassifiers(_Tmp):

    CASES = {
        "CLAUDE.md": "CLAUDE.md", "docs/CLAUDE.md": "CLAUDE.md", "CLAUDE.local.md": "CLAUDE.md",
        "AGENTS.md": "AGENTS.md", "pkg/AGENTS.override.md": "AGENTS.md",
        "GEMINI.md": "GEMINI.md",
        ".github/copilot-instructions.md": "Copilot Instructions",
        ".github/instructions/x.instructions.md": "Copilot Instructions",
        ".cursorrules": ".cursorrules",
        ".gemini/styleguide.md": "Gemini Style Guide",
        ".cursor/BUGBOT.md": "Cursor BUGBOT",
    }

    def test_classify_row_gives_the_category(self):
        for path, label in self.CASES.items():
            with self.subTest(path=path):
                self.assertEqual(mod.classify_row(path), label)

    def test_a_path_no_rule_matches_is_named_for_a_row_and_other_for_a_reference(self):
        """The detection step lists a file that no path pattern matches only because the
        repository's configuration names it, and such a row is in the category Named in
        Configuration. A referenced file is not a row, and a referenced file that no path
        pattern matches is Other."""
        for path in ("README.md", "docs/copilot-instructions.md", ".github/instructions/notes.md"):
            with self.subTest(path=path):
                self.assertEqual(mod.classify_row(path), "Named in Configuration")
                self.assertEqual(mod.classify_referenced_file(path), "Other")




class TestLoadData(_Tmp):

    def _write(self, rows: list) -> None:
        with open(self.data / "context-files.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(COLUMNS + ["line_count"])
            writer.writerows(rows)

    def test_every_row_gets_its_category_and_a_utc_timestamp(self):
        self._write([("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", "", 3),
                     ("o/a", "AGENTS.md", "2025-01-11T00:00:00Z", "TRUE", "CLAUDE.md", 1)])
        df = mod.load_data()
        self.assertEqual(list(df["category"]), ["CLAUDE.md", "AGENTS.md"])
        self.assertEqual(str(df["created_at"].dt.tz), "UTC")
        self.assertEqual(list(df.columns), COLUMNS + ["category"])

    def test_a_value_no_path_rule_matches_is_a_file_the_configuration_named(self):
        """The detection step lists a file that a Codex or Gemini CLI configuration names,
        whatever it is called. Such a file is a row like any other rather than a reason to
        stop."""
        self._write([("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", "", 3),
                     ("o/a", "NOTES.md", "2025-01-11T00:00:00Z", "FALSE", "", 1)])
        df = mod.load_data()
        self.assertEqual(list(df["category"]), ["CLAUDE.md", "Named in Configuration"])


class TestCreationOrder(_Tmp):

    def test_sequences_by_date_with_same_day_categories_grouped(self):
        df = _frame([
            ("o/a", "CLAUDE.md", "2025-01-10T09:00:00Z", "FALSE", ""),
            ("o/a", "AGENTS.md", "2025-02-10T09:00:00Z", "FALSE", ""),
            ("o/b", "CLAUDE.md", "2025-01-10T09:00:00Z", "FALSE", ""),
            ("o/b", "AGENTS.md", "2025-02-10T09:00:00Z", "FALSE", ""),
            ("o/c", "AGENTS.md", "2025-03-01T08:00:00Z", "FALSE", ""),
            ("o/c", "CLAUDE.md", "2025-03-01T23:00:00Z", "FALSE", ""),
            ("o/d", "CLAUDE.md", "2025-01-01T00:00:00Z", "FALSE", ""),
            ("o/d", "docs/CLAUDE.md", "2025-05-01T00:00:00Z", "FALSE", ""),
        ])
        printed, ax = self.run_drawing(mod.analysis_creation_order, df)
        self.assertIn("3 repositories have dated context files of at least two categories", printed)
        self.assertIn("     2  CLAUDE.md then AGENTS.md", printed)
        self.assertIn("     1  {AGENTS.md, CLAUDE.md}", printed)
        self.assertTrue((self.out / "creation_order_sequences.pdf").is_file())
        # The title's n is the repositories with a sequence, three, not the two sequences. The
        # title states that a category is dated by the creation of its earliest file under any
        # earlier name, because a file renamed into a category keeps the date of the commit that
        # created it under its earlier name.
        self.assertEqual(ax.get_title(), "Top 5 Creation Order Sequences (n = 3 repositories)\n"
                                         "by the creation date of the earliest file of each "
                                         "category, under any earlier name")

    def test_a_row_without_a_date_is_left_out_and_counted(self):
        """o/b's BUGBOT file has no date and does not count toward o/b's two categories.
        Counted, it would leave o/b with the sequence CLAUDE.md alone once grouping by date
        drops the file."""
        df = _frame([
            ("o/a", "CLAUDE.md", "2025-01-10T09:00:00Z", "FALSE", ""),
            ("o/a", "AGENTS.md", "2025-02-10T09:00:00Z", "FALSE", ""),
            ("o/b", ".cursor/BUGBOT.md", "", "FALSE", ""),
            ("o/b", "CLAUDE.md", "2025-03-10T09:00:00Z", "FALSE", ""),
        ])
        printed = self.run_printing(mod.analysis_creation_order, df)
        self.assertIn("1 rows without created_at were left out of the sequences", printed)
        self.assertIn("1 repositories have dated context files of at least two categories", printed)
        sequences = printed.split("sequences and Other.\n", 1)[1].split("Wrote", 1)[0]
        self.assertEqual(sequences.strip(), "1  CLAUDE.md then AGENTS.md")

    def test_more_than_five_sequences_fold_into_other(self):
        rows = []
        others = ["AGENTS.md", "GEMINI.md", ".cursorrules", ".github/copilot-instructions.md",
                  ".gemini/styleguide.md", ".cursor/BUGBOT.md"]
        for i, second in enumerate(others):
            rows.append((f"o/{i}", "CLAUDE.md", "2025-01-01T00:00:00Z", "FALSE", ""))
            rows.append((f"o/{i}", second, f"2025-0{i + 2}-01T00:00:00Z", "FALSE", ""))
        printed = self.run_printing(mod.analysis_creation_order, _frame(rows))
        self.assertIn("6 repositories have dated context files of at least two categories", printed)
        self.assertIn("     1  Other", printed)


class TestReferenceNetwork(_Tmp):

    def test_edges_are_split_on_semicolons_and_classified(self):
        df = _frame([
            ("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "TRUE", "AGENTS.md; docs/README.md"),
            ("o/a", "AGENTS.md", "2025-01-11T00:00:00Z", "true", ".cursorrules"),
            ("o/b", "GEMINI.md", "2025-01-12T00:00:00Z", "FALSE", ""),
        ])
        printed, ax = self.run_drawing(mod.analysis_reference_network, df)
        self.assertIn("2 rows of context-files.csv are references", printed)
        self.assertIn("The reference network has 3 edges", printed)
        self.assertTrue((self.out / "reference_network.pdf").is_file())
        # The title's n is the edges, three, not the two reference rows.
        self.assertEqual(ax.get_title(), "Reference Network Between Context Files (n = 3 reference pairs)")
        # The cross-tabulation, as logged: source rows, target columns, Other last.
        table = printed.split("References by source and target category.\n", 1)[1]
        table = table.split("Wrote", 1)[0]
        claude_row = next(line for line in table.splitlines() if line.startswith("CLAUDE.md"))
        self.assertEqual(re.findall(r"\d+", claude_row),
                         ["0", "1", "0", "0", "0", "0", "0", "0", "0", "1"])

    def test_the_column_labels_of_both_heatmaps_are_rotated_so_that_they_do_not_overlap(self):
        """The category names under the columns of the reference network and of the
        co-occurrence matrix are rotated by 45 degrees and aligned right, so that long names
        such as "Named in Configuration" do not overlap."""
        df = _frame([
            ("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "TRUE", "AGENTS.md"),
            ("o/a", "AGENTS.md", "2025-01-11T00:00:00Z", "FALSE", ""),
        ])
        for draw in (mod.analysis_reference_network, mod.analysis_cooccurrence_matrix):
            with self.subTest(draw=draw.__name__):
                _, ax = self.run_drawing(draw, df)
                labels = ax.get_xticklabels()
                self.assertGreaterEqual(len(labels), len(mod.CATEGORY_ORDER))
                self.assertEqual({(label.get_rotation(), label.get_ha()) for label in labels},
                                 {(45.0, "right")})

    def test_a_dataframe_without_a_reference_row_draws_zeros(self):
        """The edge DataFrame has a `source` column but no reference row. The
        cross-tabulation draws zeros rather than raising a KeyError."""
        df = _frame([("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", "")])
        with warnings.catch_warnings():
            # seaborn finds no unmasked cell to scale the colors by, and warns
            warnings.simplefilter("ignore", RuntimeWarning)
            printed, ax = self.run_drawing(mod.analysis_reference_network, df)
        self.assertIn("0 rows of context-files.csv are references", printed)
        self.assertIn("The reference network has 0 edges", printed)
        table = printed.split("References by source and target category.\n", 1)[1]
        table = table.split("Wrote", 1)[0]
        self.assertEqual({int(n) for line in table.splitlines()[2:] for n in re.findall(r"\b\d+\b", line)},
                         {0})
        self.assertEqual(ax.get_title(), "Reference Network Between Context Files (n = 0 reference pairs)")
        cells = len(mod.CATEGORY_ORDER) * (len(mod.CATEGORY_ORDER) + 1)
        self.assertEqual([t.get_text() for t in ax.texts], ["0"] * cells)
        self.assertTrue((self.out / "reference_network.pdf").read_bytes().startswith(b"%PDF"))


class TestCooccurrenceMatrix(_Tmp):

    def _matrix(self, printed: str) -> dict:
        block = printed.split("Repositories per pair of categories.\n", 1)[1].split("Wrote", 1)[0]
        found = {}
        for line in block.splitlines():
            for label in mod.CATEGORY_ORDER:
                if line.startswith(label):
                    found[label] = [int(n) for n in re.findall(r"\b\d+\b", line[len(label):])]
        return found

    def test_symmetric_with_the_repository_count_on_the_diagonal(self):
        df = _frame([
            ("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", ""),
            ("o/a", "docs/CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", ""),
            ("o/a", "AGENTS.md", "2025-01-11T00:00:00Z", "FALSE", ""),
            ("o/b", "CLAUDE.md", "2025-01-12T00:00:00Z", "FALSE", ""),
            ("o/c", "GEMINI.md", "2025-01-12T00:00:00Z", "FALSE", ""),
        ])
        printed, ax = self.run_drawing(mod.analysis_cooccurrence_matrix, df)
        # The context file categories, not the configuration mechanisms.
        self.assertEqual(ax.get_title(), "Co-Occurrence of Context File Categories Across "
                                         f"Repositories (n = {df['repo_name'].nunique():,})")
        self.assertIn("3 repositories have a context file", printed)
        self.assertIn("1 repositories have context files of at least two categories", printed)
        self.assertRegex(printed, r"(?m)^  CLAUDE\.md +2$")
        matrix = self._matrix(printed)
        order = list(mod.CATEGORY_ORDER)
        for i, row_label in enumerate(order):
            for j, col_label in enumerate(order):
                self.assertEqual(matrix[row_label][j], matrix[col_label][i])
        self.assertEqual(matrix["CLAUDE.md"][order.index("CLAUDE.md")], 2)
        self.assertEqual(matrix["CLAUDE.md"][order.index("AGENTS.md")], 1)
        self.assertEqual(matrix["GEMINI.md"][order.index("GEMINI.md")], 1)
        self.assertEqual(matrix["Cursor BUGBOT"], [0] * len(order))
        self.assertTrue((self.out / "cooccurrence_matrix.pdf").is_file())


class TestPrintContextFileSummary(_Tmp):

    def test_files_share_and_repositories_per_category(self):
        df = _frame([
            ("o/a", "CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", ""),
            ("o/a", "docs/CLAUDE.md", "2025-01-10T00:00:00Z", "FALSE", ""),
            ("o/b", "AGENTS.md", "2025-01-11T00:00:00Z", "FALSE", ""),
            ("o/c", "AGENTS.md", "2025-01-11T00:00:00Z", "FALSE", ""),
        ])
        printed = self.run_printing(mod.print_context_file_summary, df)
        self.assertIn("CLAUDE.md                     2 files (50.0%),     1 repositories", printed)
        self.assertIn("AGENTS.md                     2 files (50.0%),     2 repositories", printed)
        self.assertIn("TOTAL                         4 files,     3 repositories", printed)


@unittest.skipUnless(DEPS, "pandas, numpy, matplotlib and seaborn are needed")
class TestMainOverTheFixture(unittest.TestCase):
    """The fixture's context-files.csv, counted by hand, has 160 rows over 31 repositories,
    35 rows with is_reference TRUE that name 36 files between them, and one row without a
    created_at, clickhouse/clickhouse's .cursor/BUGBOT.md."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.before = {p.name for p in SCRIPT_DIR.iterdir()}

    def test_the_run(self):
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(self.tmp),
                   MPLBACKEND="Agg", SOURCE_DATE_EPOCH="0", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(SCRIPT)], cwd=str(SCRIPT_DIR), env=env,
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertIn("Loaded 160 rows of 31 repositories from context-files.csv", logs.messages(result.stderr))
        self.assertIn("CLAUDE.md                    37 files (23.1%),    18 repositories",
                      logs.messages(result.stderr))
        self.assertIn("AGENTS.md                    89 files (55.6%),    24 repositories",
                      logs.messages(result.stderr))
        self.assertIn("1 rows without created_at were left out of the sequences", logs.messages(result.stderr))
        self.assertIn("35 rows of context-files.csv are references", logs.messages(result.stderr))
        self.assertIn("The reference network has 36 edges", logs.messages(result.stderr))
        for name in ("creation_order_sequences.pdf", "reference_network.pdf", "cooccurrence_matrix.pdf"):
            with self.subTest(figure=name):
                pdf = self.tmp / "rq3" / name
                self.assertTrue(pdf.is_file())
                self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)


if __name__ == "__main__":
    unittest.main()
