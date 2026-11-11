"""
Tests for rq2/tool_cooccurrence.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - All the arithmetic is inside `main()`, which runs in-process over a small repos.csv under
    a temporary directory with DATA_DIR and OUTPUT_DIR replaced. The tests check the numbers
    in the CSV and the LaTeX table that `main()` writes.
  - One subprocess run over the fixture under testdata/output/, with the tool counts and
    pair counts stated here from the fixture.

Requirements: Python 3.14 (pandas, matplotlib and seaborn, no network)
Run with:  python3 -m unittest test_rq2_tool_cooccurrence -v
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

import pandas as pd
from agentcontext import logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_tool_cooccurrence_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP
os.environ.setdefault("MPLBACKEND", "Agg")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("tool_cooccurrence", RQ2 / "tool_cooccurrence.py")
_shared = sys.modules["_shared"]


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


HEADER = (FIXTURE / "repos.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
TOOLS = mod.TOOLS


def repo(name: str, *flags: str) -> dict:
    return {"repo_name": name, **{flag: "TRUE" for flag in flags}}


def write_repos_csv(path: Path, rows: list) -> None:
    """A repos.csv with the fixture's columns, every flag FALSE unless the row names it."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            full = {column: "FALSE" for column in HEADER}
            full.update({"scanned_at": "2026-09-12T00:00:00Z", "branch": "main",
                         "head_sha": "0" * 40, "head_commit_date": "2026-09-01T00:00:00Z",
                         "github_link": "https://github.com/" + row["repo_name"],
                         "dataset_version": "2.0.0"})
            full.update(row)
            writer.writerow(full)


# Six repositories: Claude in four, Copilot in two (one of them with Claude), Cursor in one
# with Claude, one with no specific tool, Gemini and Codex in none.
ROWS = [
    repo("a/1", "claude"),
    repo("a/2", "claude"),
    repo("a/3", "claude", "copilot"),
    repo("a/4", "claude", "cursor"),
    repo("a/5", "copilot"),
    repo("a/6", "no_specific_tool"),
]


class _Run(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.out = self.tmp / "out"
        self.data.mkdir()
        self.out.mkdir()
        for name, value in (("DATA_DIR", self.data), ("OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)

    def run_main(self, rows: list) -> str:
        write_repos_csv(self.data / "repos.csv", rows)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.main()
        return logs.messages(buffer.getvalue())

    def results(self) -> pd.DataFrame:
        return pd.read_csv(self.out / "tool_cooccurrence.csv")

    def tex_rows(self) -> dict:
        """The seven tool rows of the TeX table, by tool label, each a list of cells."""
        rows = {}
        for line in (self.out / "tool_cooccurrence.tex").read_text(encoding="utf-8").splitlines():
            if line.startswith("\\textbf{") and line.endswith(" \\\\") and "$n$" in line:
                cells = line[: -len(" \\\\")].split(" & ")
                rows[cells[0][len("\\textbf{"):-1]] = cells[1:]
        return rows




class TestTheCsv(_Run):

    def test_one_row_per_pair(self):
        self.run_main(ROWS)
        results = self.results()
        self.assertEqual(len(results), 21)
        self.assertEqual(list(results.columns), ["tool_a", "tool_b", "both", "only_a", "only_b", "neither"])
        self.assertEqual(len(set(zip(results["tool_a"], results["tool_b"]))), 21)
        self.assertEqual(set(results["tool_a"]) | set(results["tool_b"]),
                         {"claude", "copilot", "cursor", "gemini", "codex", "opencode",
                          "no_specific_tool"})

    def test_the_four_cells_sum_to_the_population(self):
        self.run_main(ROWS)
        results = self.results()
        self.assertTrue((results[["both", "only_a", "only_b", "neither"]].sum(axis=1) == len(ROWS)).all())

    def test_the_cells_of_a_pair(self):
        self.run_main(ROWS)
        row = self.results().set_index(["tool_a", "tool_b"]).loc[("claude", "copilot")]
        self.assertEqual(tuple(int(row[c]) for c in ("both", "only_a", "only_b", "neither")), (1, 3, 1, 1))


class TestTheTex(_Run):

    def test_the_diagonal_is_each_tools_total(self):
        self.run_main(ROWS)
        rows = self.tex_rows()
        self.assertEqual(list(rows), [mod.TOOL_LABELS[t] for t in TOOLS])
        self.assertEqual(rows["Claude"][0], "($n$\\,=\\,4)")
        self.assertEqual(rows["Copilot"][1], "($n$\\,=\\,2)")
        self.assertEqual(rows["No specific tool"][2], "($n$\\,=\\,1)")
        self.assertEqual(rows["Codex"][5], "($n$\\,=\\,0)")

    def test_the_upper_triangle_is_the_count_and_the_lower_the_share_of_the_smaller(self):
        self.run_main(ROWS)
        rows = self.tex_rows()
        # Claude (4) and Copilot (2) share one repository: 50% of Copilot's two
        self.assertEqual(rows["Claude"][1], "1")
        self.assertEqual(rows["Copilot"][0], "50.0\\%")
        # Claude and Cursor (1) share the one Cursor repository
        self.assertEqual(rows["Claude"][3], "1")
        self.assertEqual(rows["Cursor"][0], "100.0\\%")

    def test_a_tool_with_no_repository_gives_zero_percent_rather_than_a_division(self):
        self.run_main(ROWS)
        rows = self.tex_rows()
        self.assertEqual(rows["Gemini"][0], "0.0\\%")
        self.assertEqual(rows["Codex"][4], "0.0\\%")
        self.assertEqual(rows["Claude"][4], "0")

    def test_the_note_names_the_population(self):
        self.run_main(ROWS)
        tex = (self.out / "tool_cooccurrence.tex").read_text(encoding="utf-8")
        self.assertIn(f"$N$\\,=\\,{len(ROWS)} repositories", tex)
        self.assertTrue(tex.startswith("\\begin{table}[t]\n"))
        self.assertTrue(tex.endswith("\\end{table}\n"))


class TestTheRun(_Run):

    def test_the_three_files_are_written_and_the_top_pairs_printed(self):
        out = self.run_main(ROWS)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["tool_cooccurrence.csv", "tool_cooccurrence.pdf", "tool_cooccurrence.tex"])
        self.assertTrue((self.out / "tool_cooccurrence.pdf").read_bytes().startswith(b"%PDF"))
        self.assertIn("Repositories per tool (N=6).\n", out)
        self.assertIn("1 repositories configure Claude and Copilot (50.0% of the smaller tool's "
                      "repositories)", out)

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_main(ROWS)
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestOverTheFixture(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(cls.tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        cls.result = subprocess.run([sys.executable, str(RQ2 / "tool_cooccurrence.py")],
                                    cwd=str(RQ2), env=env, capture_output=True, text=True)
        cls.out = cls.tmp / "rq2"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_run_succeeds(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["tool_cooccurrence.csv", "tool_cooccurrence.pdf", "tool_cooccurrence.tex"])

    def test_the_counts_of_the_fixture(self):
        # Of the 32 repositories, 22 configure Claude and 12 Copilot, and 9 configure both.
        results = pd.read_csv(self.out / "tool_cooccurrence.csv")
        self.assertEqual(len(results), 21)
        self.assertTrue((results[["both", "only_a", "only_b", "neither"]].sum(axis=1) == 32).all())
        row = results.set_index(["tool_a", "tool_b"]).loc[("claude", "copilot")]
        self.assertEqual(tuple(int(row[c]) for c in ("both", "only_a", "only_b", "neither")), (9, 13, 3, 7))

    def test_the_totals_of_the_fixture(self):
        tex = (self.out / "tool_cooccurrence.tex").read_text(encoding="utf-8")
        for label, n in (("Claude", 22), ("Copilot", 12), ("No specific tool", 3), ("Cursor", 11),
                         ("Gemini", 10), ("Codex", 7)):
            with self.subTest(tool=label):
                line = next(l for l in tex.splitlines() if l.startswith(f"\\textbf{{{label}}} &"))
                self.assertIn(f"($n$\\,=\\,{n})", line)
        self.assertIn("$N$\\,=\\,32 repositories", tex)
        self.assertIn("Repositories per tool (N=32).\n", logs.messages(self.result.stderr))


if __name__ == "__main__":
    unittest.main()
