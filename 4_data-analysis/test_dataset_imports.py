"""
Tests for dataset/imports.py

Strategy:
  - The script runs as a subprocess over the fixture under testdata/output, with
    ANALYSIS_OUTPUT_DIR set to a temporary root, and its CSV and LaTeX table are read back.
    The expected counts come from the fixture's six imports, counted by hand.
  - Nothing is written beside the script.

Requirements: Python 3.14 (pandas)
Run with:  python3 -m unittest test_dataset_imports -v
"""

import csv
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "dataset" / "imports.py"
FIXTURE = HERE / "testdata" / "output"

DEPS = importlib.util.find_spec("pandas") is not None


@unittest.skipUnless(DEPS, "requires pandas")
class TestTheImportTable(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE),
                   ANALYSIS_OUTPUT_DIR=self.tmp.name)
        subprocess.run([sys.executable, str(SCRIPT)], env=env, check=True,
                       capture_output=True, text=True)
        self.out = Path(self.tmp.name) / "dataset"

    def test_the_imports_of_each_tool_by_status(self):
        with (self.out / "imports.csv").open(newline="") as f:
            rows = {row["tool"]: row for row in csv.DictReader(f)}
        # Claude has two found imports in 47ng/nuqs's CLAUDE.md and one import outside the
        # repository in Automattic/harper's. Copilot has one found and one missing import in
        # 47ng/nuqs, and Gemini has one found import.
        self.assertEqual(list(rows), ["claude", "copilot", "gemini"])
        self.assertEqual({tool: (row["imports"], row["found"], row["missing"], row["outside"])
                          for tool, row in rows.items()},
                         {"claude": ("3", "2", "0", "1"), "copilot": ("2", "1", "1", "0"),
                          "gemini": ("1", "1", "0", "0")})
        self.assertEqual({tool: (row["importing_files"], row["repositories"])
                          for tool, row in rows.items()},
                         {"claude": ("2", "2"), "copilot": ("1", "1"), "gemini": ("1", "1")})

    def test_the_total_counts_a_file_that_two_tools_read_once(self):
        """47ng/nuqs's CLAUDE.md starts a chain of Claude Code and a chain of Copilot CLI. The
        total row counts three importing files, not four."""
        total = [line for line in (self.out / "imports.tex").read_text().splitlines()
                 if line.startswith("\\textbf{Total}")]
        self.assertEqual(total, ["\\textbf{Total} & \\textbf{6} & \\textbf{4} & "
                                 "\\textbf{3} & \\textbf{2} \\\\"])

    def test_nothing_is_written_beside_the_script(self):
        self.assertFalse((SCRIPT.parent / "imports.csv").exists())
        self.assertFalse((SCRIPT.parent / "imports.tex").exists())


@unittest.skipUnless(DEPS, "requires pandas")
class TestAnImportsCsvWithoutRows(unittest.TestCase):
    """A release in which no configuration artifact imports a file has an `imports.csv` with its
    header alone, and the table has no tool row and a total row of zeros."""

    def test_the_tables_have_a_total_row_of_zeros(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        tmp = Path(directory.name)
        output = tmp / "output"
        output.mkdir()
        header = (FIXTURE / "imports.csv").read_text(encoding="utf-8").splitlines()[0]
        (output / "imports.csv").write_text(header + "\n", encoding="utf-8")
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(output),
                   ANALYSIS_OUTPUT_DIR=str(tmp / "analysis"))
        result = subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True,
                                text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = tmp / "analysis" / "dataset"
        with (out / "imports.csv").open(newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(list(reader), [])
        self.assertEqual(reader.fieldnames,
                         ["tool", "imports", "found", "missing", "outside", "not_read",
                          "over_limit", "pruned", "not_copied", "importing_files",
                          "repositories"])
        tex = (out / "imports.tex").read_text().splitlines()
        rows = tex[tex.index("\\midrule") + 1:tex.index("\\bottomrule")]
        self.assertEqual(rows, ["\\midrule", "\\textbf{Total} & \\textbf{0} & \\textbf{0} & "
                                             "\\textbf{0} & \\textbf{0} \\\\"])


if __name__ == "__main__":
    unittest.main()
