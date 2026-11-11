"""
Tests for rq2/tool_count_distribution.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - All the arithmetic is inside `main()`, which runs in-process over a small repos.csv under
    a temporary directory with DATA_DIR and OUTPUT_DIR replaced. The tests check the numbers
    in the CSV and the LaTeX table that `main()` writes.
  - One subprocess run over the fixture under testdata/output/, with the distribution stated
    here from the fixture.

Requirements: Python 3.14 (pandas, matplotlib, seaborn and Poppler, no network)
Run with:  python3 -m unittest test_rq2_tool_count_distribution -v
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

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_tool_count_distribution_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP
os.environ.setdefault("MPLBACKEND", "Agg")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("tool_count_distribution", RQ2 / "tool_count_distribution.py")
_shared = sys.modules["_shared"]


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


HEADER = (FIXTURE / "repos.csv").read_text(encoding="utf-8").splitlines()[0].split(",")


def repo(name: str, *flags: str) -> dict:
    return {"repo_name": name, **{flag: "TRUE" for flag in flags}}


def write_repos_csv(path: Path, rows: list, header: list = HEADER) -> None:
    """A repos.csv with the fixture's columns, every flag FALSE unless the row names it."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            full = {column: "FALSE" for column in header}
            full.update({column: value for column, value in {
                "scanned_at": "2026-09-12T00:00:00Z", "branch": "main", "head_sha": "0" * 40,
                "head_commit_date": "2026-09-01T00:00:00Z",
                "github_link": "https://github.com/" + row["repo_name"],
                "dataset_version": "2.0.0"}.items() if column in header})
            full.update({column: value for column, value in row.items() if column in header})
            writer.writerow(full)


# Eight repositories: two with no specific tool, three with one tool, two with two, and one with
# five, every tool but OpenCode.
ROWS = [
    repo("a/1", "agents.md", "no_specific_tool"),
    repo("a/2", "agents.md", "no_specific_tool"),
    repo("a/3", "claude"),
    repo("a/4", "copilot"),
    repo("a/5", "cursor"),
    repo("a/6", "claude", "gemini"),
    repo("a/7", "claude", "codex"),
    repo("a/8", "claude", "copilot", "cursor", "gemini", "codex"),
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

    def run_main(self, rows: list, header: list = HEADER) -> str:
        write_repos_csv(self.data / "repos.csv", rows, header)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.main()
        return logs.messages(buffer.getvalue())

    def summary(self) -> pd.DataFrame:
        return pd.read_csv(self.out / "tool_count_distribution.csv")

    def tex(self) -> str:
        return (self.out / "tool_count_distribution.tex").read_text(encoding="utf-8")


class TestToolAttribution(_Run):

    def test_each_named_tool_counts_once_and_shared_instructions_count_zero(self):
        rows = [repo("o/" + tool, tool) for tool in
                ("claude", "copilot", "cursor", "gemini", "codex", "opencode")]
        rows.append(repo("o/shared", "agents.md", "no_specific_tool"))
        self.run_main(rows)
        counts = self.summary().set_index("num_tools")["num_repos"].to_dict()
        self.assertEqual(counts, {0: 1, 1: 6, 2: 0, 3: 0, 4: 0, 5: 0, 6: 0})


class TestPortableConfiguration(_Run):

    def test_shared_configuration_of_any_kind_has_no_specific_tool(self):
        """Portable skills and an AGENTS.md both leave a repository with no named tool. Both
        repositories count as having no specific tool, not only the repository with
        AGENTS.md."""
        rows = [repo("o/portable", "skills", "no_specific_tool"),
                repo("o/instructions", "agents.md", "context_file", "no_specific_tool"),
                repo("o/claude", "claude")]
        out = self.run_main(rows)
        self.assertEqual(list(self.summary()["num_repos"]), [2, 1, 0, 0, 0, 0, 0])
        self.assertIn("0 & 2 & 66.7", self.tex())
        self.assertIn("2 repositories have none of the six tool flags and only shared "
                      "configuration artifacts", out)
        pdf = subprocess.run(["pdftotext", str(self.out / "tool_count_distribution.pdf"), "-"],
                             check=True, capture_output=True, text=True).stdout
        self.assertNotIn("AGENTS.md", pdf)
        self.assertIn("Number of Named Tools Configured", pdf)


class TestTheDistribution(_Run):

    def test_seven_buckets_from_zero_to_six(self):
        self.run_main(ROWS)
        summary = self.summary()
        self.assertEqual(list(summary.columns), ["num_tools", "num_repos", "pct"])
        self.assertEqual(list(summary["num_tools"]), [0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(list(summary["num_repos"]), [2, 3, 2, 0, 0, 1, 0])

    def test_the_buckets_partition_the_population_and_the_shares_sum_to_one_hundred(self):
        self.run_main(ROWS)
        summary = self.summary()
        self.assertEqual(int(summary["num_repos"].sum()), len(ROWS))
        self.assertEqual(list(summary["pct"]), [25.0, 37.5, 25.0, 0.0, 0.0, 12.5, 0.0])
        self.assertAlmostEqual(summary["pct"].sum(), 100.0)

    def test_an_absent_no_specific_tool_column_is_read_as_false(self):
        header = [c for c in HEADER if c != "no_specific_tool"]
        out = self.run_main(ROWS, header)
        self.assertIn("0 repositories have none of the six tool flags and only shared "
                      "configuration artifacts", out)
        self.assertEqual(list(self.summary()["num_repos"]), [2, 3, 2, 0, 0, 1, 0])

    def test_no_specific_tool_adoption_is_reported_separately(self):
        out = self.run_main(ROWS)
        self.assertIn("2 repositories have none of the six tool flags and only shared "
                      "configuration artifacts", out)
        self.assertIn(f"Repositories by number of tools (N={len(ROWS)}).\n", out)


class TestTheTex(_Run):

    def test_a_row_per_bucket_and_a_total(self):
        self.run_main(ROWS)
        lines = self.tex().splitlines()
        self.assertIn("0 & 2 & 25.0\\% \\\\", lines)
        self.assertIn("1 & 3 & 37.5\\% \\\\", lines)
        self.assertIn("3 & 0 & 0.0\\% \\\\", lines)
        self.assertIn("5 & 1 & 12.5\\% \\\\", lines)
        self.assertIn("\\textbf{Total} & \\textbf{8} & \\textbf{100.0\\%} \\\\", lines)
        self.assertEqual(lines[0], "\\begin{table}[t]")
        self.assertEqual(lines[-1], "\\end{table}")


class TestTheRun(_Run):

    def test_the_three_files_are_written(self):
        self.run_main(ROWS)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["tool_count_distribution.csv", "tool_count_distribution.pdf",
                          "tool_count_distribution.tex"])
        self.assertTrue((self.out / "tool_count_distribution.pdf").read_bytes().startswith(b"%PDF"))

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_main(ROWS)
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestOverTheFixture(unittest.TestCase):

    def test_the_distribution_of_the_fixture(self):
        # 12 of the 32 repositories configure one tool, 7 two, 5 three, 4 four and 1 five, and 3
        # have no specific tool. The fixture's flags are the union of its rows'
        # `tool` column, which gives LMCache/LMCache Codex and yamadashy/repomix Gemini.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(RQ2 / "tool_count_distribution.py")],
                                cwd=str(RQ2), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = pd.read_csv(tmp / "rq2" / "tool_count_distribution.csv")
        self.assertEqual(list(zip(summary["num_tools"], summary["num_repos"])),
                         [(0, 3), (1, 12), (2, 7), (3, 5), (4, 4), (5, 1), (6, 0)])
        self.assertEqual(list(summary["pct"]), [9.4, 37.5, 21.9, 15.6, 12.5, 3.1, 0.0])
        tex = (tmp / "rq2" / "tool_count_distribution.tex").read_text(encoding="utf-8")
        self.assertIn("0 & 3 & 9.4\\% \\\\", tex)
        self.assertIn("\\textbf{Total} & \\textbf{32} & \\textbf{100.0\\%} \\\\", tex)


if __name__ == "__main__":
    unittest.main()
