"""
Tests for dataset/planning_documents.py

Strategy:
  - The script runs as a subprocess over the fixture under testdata/output, with
    ANALYSIS_OUTPUT_DIR set to a temporary root, and its CSV and LaTeX table are read back.
    The expected counts come from the fixture's four planning documents, counted by hand.
  - Nothing is written beside the script.

Requirements: Python 3.14 (pandas)
Run with:  python3 -m unittest test_dataset_planning_documents -v
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
SCRIPT = HERE / "dataset" / "planning_documents.py"
FIXTURE = HERE / "testdata" / "output"

DEPS = importlib.util.find_spec("pandas") is not None


@unittest.skipUnless(DEPS, "requires pandas")
class TestThePlanningDocumentTable(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE),
                   ANALYSIS_OUTPUT_DIR=self.tmp.name)
        subprocess.run([sys.executable, str(SCRIPT)], env=env, check=True,
                       capture_output=True, text=True)
        self.out = Path(self.tmp.name) / "dataset"

    def test_the_documents_and_repositories_of_each_framework(self):
        """The rows list the two OpenSpec documents of 47ng/nuqs first, then the Cursor plan
        and the Spec Kit spec, one each, in the order of their names."""
        with (self.out / "planning_documents.csv").open(newline="") as f:
            rows = [(row["framework"], row["documents"], row["repositories"])
                    for row in csv.DictReader(f)]
        self.assertEqual(rows, [("openspec", "2", "1"), ("cursor", "1", "1"),
                                ("spec-kit", "1", "1")])

    def test_the_table_totals_the_documents_and_the_repositories(self):
        tex = (self.out / "planning_documents.tex").read_text()
        self.assertIn("spec-kit & 1 & 1 \\\\", tex)
        self.assertIn("\\textbf{Total} & \\textbf{4} & \\textbf{3} \\\\", tex)

    def test_nothing_is_written_beside_the_script(self):
        self.assertFalse((SCRIPT.parent / "planning_documents.csv").exists())
        self.assertFalse((SCRIPT.parent / "planning_documents.tex").exists())


if __name__ == "__main__":
    unittest.main()
