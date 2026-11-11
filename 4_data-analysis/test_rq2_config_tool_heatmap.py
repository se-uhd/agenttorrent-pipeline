"""
Tests for rq2/config_tool_heatmap.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - All the arithmetic is inside `main()` and `_shared.mechanism_tool_counts`, and the only
    file that the script writes is the PDF. The tests therefore wrap `seaborn.heatmap` and
    check the numbers in the matrices that the script passes to it. They run over a small
    repos.csv and the ten mechanism CSVs under a temporary directory, with DATA_DIR and
    OUTPUT_DIR replaced. Each mechanism CSV row names its configuration artifact's tool, as the
    detection step writes it. The tick labels and the title are read back off the axes that
    the script drew on.
  - One subprocess run over the fixture under testdata/output/.

Requirements: Python 3.14 (pandas, matplotlib and seaborn, no network)
Run with:  python3 -m unittest test_rq2_config_tool_heatmap -v
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
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_config_tool_heatmap_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP
os.environ.setdefault("MPLBACKEND", "Agg")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("config_tool_heatmap", RQ2 / "config_tool_heatmap.py")
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


# Six repositories: Claude in four, Cursor in two, Copilot in one, one with shared files
# only, Gemini and Codex in none. `a/6` has Claude's context file and a Copilot command, and
# its command counts for Copilot, not for Claude.
ROWS = [
    repo("a/1", "claude", "context_file", "rules"),
    repo("a/2", "claude", "context_file"),
    repo("a/3", "claude", "cursor", "rules", "skills"),
    repo("a/4", "cursor", "context_file", "skills"),
    repo("a/5", "no_specific_tool", "context_file", "skills"),
    repo("a/6", "claude", "copilot", "context_file", "commands"),
]

# The configuration artifacts of those repositories, by mechanism: (repository, tool).
ARTIFACTS = {
    "context_file": [("a/1", "claude"), ("a/2", "claude"), ("a/2", "any"), ("a/4", "any"),
                     ("a/4", "cursor"), ("a/5", "any"), ("a/6", "claude")],
    "rules": [("a/1", "claude"), ("a/3", "cursor"), ("a/3", "claude")],
    "skills": [("a/3", "claude|cursor"), ("a/4", "cursor"), ("a/5", "any")],
    "commands": [("a/6", "copilot")],
}


def write_mechanism_csvs(directory: Path) -> None:
    """The ten mechanism CSVs, with the two columns that the script reads."""
    for mechanism, name in _shared.MECHANISM_CSV_NAMES.items():
        with open(directory / name, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["repo_name", "path", "tool"])
            for i, (repository, tool) in enumerate(ARTIFACTS.get(mechanism, [])):
                writer.writerow([repository, f"file{i}", tool])


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
        write_repos_csv(self.data / "repos.csv", ROWS)
        write_mechanism_csvs(self.data)

    def run_main(self) -> tuple:
        """Run `main()`, and return its log messages and the matrix and the annotations handed
        to seaborn. The axes that `main()` drew on are kept as `self.ax` and keep their tick
        labels and title after the figure is closed."""
        buffer = io.StringIO()
        real_subplots = mod.plt.subplots

        def subplots(*args, **kwargs):
            fig, self.ax = real_subplots(*args, **kwargs)
            return fig, self.ax

        with mock.patch.object(mod.sns, "heatmap", wraps=mod.sns.heatmap) as heatmap, \
                mock.patch.object(mod.plt, "subplots", subplots), \
                contextlib.redirect_stderr(buffer):
            mod.main()
        (pct_matrix,), kwargs = heatmap.call_args.args, heatmap.call_args.kwargs
        return logs.messages(buffer.getvalue()), pct_matrix, kwargs["annot"]




class TestTheMatrices(_Run):

    def test_the_axes_are_mechanisms_by_tools(self):
        _, pct, annot = self.run_main()
        self.assertEqual(set(pct.index), {"Context Files", "Skills", "Subagents", "Commands",
                         "Rules", "Settings", "Hooks", "MCP", "Plugins", "LSP"})
        self.assertEqual(list(pct.columns), ["Claude", "Copilot", "Cursor", "Gemini", "Codex",
                                             "OpenCode", "No specific tool"])
        self.assertEqual(list(annot.index), list(pct.index))
        self.assertEqual(list(annot.columns), list(pct.columns))

    def test_a_cell_is_the_share_of_the_tools_repositories(self):
        _, pct, annot = self.run_main()
        self.assertAlmostEqual(pct.loc["Rules", "Claude"], 50.0)
        self.assertAlmostEqual(pct.loc["Rules", "Cursor"], 50.0)
        self.assertAlmostEqual(pct.loc["Context Files", "Claude"], 75.0)
        self.assertAlmostEqual(pct.loc["Skills", "Cursor"], 100.0)
        self.assertEqual(annot.loc["Rules", "Claude"], "2\n(50.0%)")
        self.assertEqual(annot.loc["Skills", "Cursor"], "2\n(100.0%)")
        self.assertEqual(annot.loc["Subagents", "Claude"], "0\n(0.0%)")

    def test_a_tool_with_no_repository_gives_zero_not_a_division(self):
        _, pct, annot = self.run_main()
        for tool in ("Gemini", "Codex", "OpenCode"):
            with self.subTest(tool=tool):
                self.assertTrue((pct[tool] == 0).all())
                self.assertTrue((annot[tool] == "0\n(0.0%)").all())

    def test_a_cell_counts_the_files_attributed_to_the_tool(self):
        """A cell counts the repositories with a file of the mechanism attributed to the tool.
        Crossing the repository flags would count `a/6`, which has Claude's context file and a
        Copilot command, in the cell of Claude and Commands, although it has no Claude
        command."""
        _, _, annot = self.run_main()
        counts = annot.map(lambda s: int(s.split("\n")[0]))
        self.assertEqual(int(counts.loc["Commands", "Claude"]), 0)
        self.assertEqual(int(counts.loc["Commands", "Copilot"]), 1)
        self.assertEqual(int(counts.loc["Rules", "Claude"]), 2)
        self.assertEqual(int(counts.loc["Context Files", "Cursor"]), 1)
        self.assertEqual(int(counts.loc["Context Files", "Claude"]), 3)
        self.assertEqual(int(counts["Claude"].sum()), 3 + 2 + 1)   # context files, rules, skills

    def test_a_file_of_several_tools_counts_for_each(self):
        _, _, annot = self.run_main()
        self.assertEqual(annot.loc["Skills", "Claude"], "1\n(25.0%)")
        self.assertEqual(annot.loc["Skills", "Cursor"], "2\n(100.0%)")

    def test_shared_files_count_only_for_repositories_with_no_specific_tool(self):
        """The AGENTS.md of `a/2` and `a/4` belongs to no tool and does not change a tool's
        cell. The repository with only shared files fills the last column."""
        _, _, annot = self.run_main()
        self.assertEqual(annot.loc["Context Files", "No specific tool"], "1\n(100.0%)")
        self.assertEqual(annot.loc["Skills", "No specific tool"], "1\n(100.0%)")
        self.assertEqual(annot.loc["Rules", "No specific tool"], "0\n(0.0%)")


class TestTheLabels(_Run):

    def test_the_column_labels_show_each_tools_count_in_order(self):
        """The test reads the labels back from the axes and fails for labels in reverse order.
        Claude has four of the six repositories, Cursor two, Copilot one, and one repository
        has no specific tool."""
        self.run_main()
        self.assertEqual([label.get_text() for label in self.ax.get_xticklabels()],
                         ["Claude\n(n=4)", "Copilot\n(n=1)", "Cursor\n(n=2)",
                          "Gemini\n(n=0)", "Codex\n(n=0)", "OpenCode\n(n=0)",
                          "No specific tool\n(n=1)"])
        self.assertEqual([label.get_text() for label in self.ax.get_yticklabels()],
                         mod.MECHANISM_LABELS)

    def test_the_column_labels_are_turned_so_that_they_do_not_overlap(self):
        """The column labels, a name and a count each, are too wide to fit side by side under
        the columns, where "No specific tool" would run into "OpenCode"."""
        self.run_main()
        labels = self.ax.get_xticklabels()
        self.assertEqual(len(labels), 7)
        self.assertEqual({(label.get_rotation(), label.get_ha()) for label in labels},
                         {(45.0, "right")})

    def test_the_title_counts_the_rows_of_repos_csv(self):
        self.run_main()
        self.assertEqual(self.ax.get_title(), "Configuration Mechanisms per Agentic AI Tool (n = 6)")


class TestThePdf(_Run):

    def test_the_figure_is_written(self):
        out, _, _ = self.run_main()
        pdf = self.out / "config_tool_heatmap.pdf"
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertIn("Wrote config_tool_heatmap.pdf", out)

    def test_the_figure_embeds_truetype_fonts(self):
        """`_shared.save_figure` embeds TrueType fonts in the PDF rather than Type 3 fonts."""
        self.run_main()
        data = (self.out / "config_tool_heatmap.pdf").read_bytes()
        self.assertIn(b"/FontFile2", data)
        self.assertNotIn(b"/Type3", data)

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_main()
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestOverTheFixture(unittest.TestCase):

    def test_the_script_draws_the_fixture(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(RQ2 / "config_tool_heatmap.py")],
                                cwd=str(RQ2), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([p.name for p in (tmp / "rq2").iterdir()], ["config_tool_heatmap.pdf"])
        self.assertTrue((tmp / "rq2" / "config_tool_heatmap.pdf").read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
