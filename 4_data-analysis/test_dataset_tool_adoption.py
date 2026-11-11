"""
Tests for dataset/tool_adoption.py

Strategy:
  - The script is loaded by path with the matplotlib backend set to Agg. Its arithmetic is
    in main(), which runs in-process with the module's pyplot replaced by a recording
    stand-in and DATA_DIR pointed at a repos.csv built here. The tests read back the bar
    labels, the tick labels and the title. main() also runs in-process over the fixture under
    testdata/output, with tool counts computed by hand.
  - One run as a subprocess over the fixture with ANALYSIS_OUTPUT_DIR set checks that the PDF
    is written under the temporary root and that nothing is written beside the script.

Requirements: Python 3.14 (pandas, numpy, matplotlib, seaborn)
Run with:  python3 -m unittest test_dataset_tool_adoption -v
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
SCRIPT_DIR = HERE / "dataset"
SCRIPT = SCRIPT_DIR / "tool_adoption.py"
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


mod = _load("tool_adoption_under_test", SCRIPT) if DEPS else None

TOOLS = ["claude", "copilot", "no_specific_tool", "cursor", "gemini", "codex", "opencode"]


class _Bar:
    def __init__(self, width):
        self.width = width

    def get_width(self):
        return self.width

    def get_y(self):
        return 0.0

    def get_height(self):
        return 0.5


def _recording_pyplot():
    plt = mock.MagicMock(name="plt")
    fig, ax = mock.MagicMock(name="fig"), mock.MagicMock(name="ax")
    ax.barh.side_effect = lambda y, widths, height, **kw: [_Bar(w) for w in widths]
    plt.subplots.return_value = (fig, ax)
    return plt, fig, ax


def _render(data_dir: Path) -> tuple:
    """main() over the output CSVs in data_dir with a recording pyplot, returning the labels by
    tool, the title and the tick labels. The bars are drawn in reverse TOOLS order, and
    reversing the labels reads them back in TOOLS order."""
    plt, fig, ax = _recording_pyplot()
    with mock.patch.object(mod, "DATA_DIR", data_dir), mock.patch.object(mod, "plt", plt), \
            contextlib.redirect_stdout(io.StringIO()):
        mod.main()
    texts = [call.args[2] for call in ax.text.call_args_list]
    ticks = list(ax.set_yticklabels.call_args.args[0])
    labels = dict(zip(reversed(ticks), reversed(texts)))
    return labels, ax.set_title.call_args.args[0], ticks


@unittest.skipUnless(DEPS, "pandas, numpy, matplotlib and seaborn are needed")
class TestTheCounts(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write_repos(self, rows: list) -> None:
        with open(self.tmp / "repos.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["repo_name"] + TOOLS + ["other"])
            writer.writerows(rows)

    def test_counts_and_shares_over_four_repositories(self):
        self._write_repos([
            ("o/a", "TRUE", "TRUE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "x"),
            ("o/b", "true", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "x"),
            ("o/c", "TRUE", "FALSE", "FALSE", "", "FALSE", "FALSE", "TRUE", "x"),
            ("o/d", "FALSE", "FALSE", "TRUE", "FALSE", "FALSE", "FALSE", "FALSE", "x"),
        ])
        labels, title, ticks = _render(self.tmp)
        self.assertEqual(labels, {"Claude": "3 (75.0%)", "Copilot": "1 (25.0%)", "No specific tool": "1 (25.0%)",
                                  "Cursor": "0 (0.0%)", "Gemini": "0 (0.0%)", "Codex": "0 (0.0%)",
                                  "OpenCode": "1 (25.0%)"})
        self.assertIn("(n = 4)", title)

    def test_the_bars_are_in_tool_order_largest_on_top(self):
        self._write_repos([("o/a", "TRUE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "FALSE", "x")])
        _, _, ticks = _render(self.tmp)
        self.assertEqual(ticks, ["OpenCode", "Codex", "Gemini", "Cursor", "No specific tool", "Copilot",
                                 "Claude"])

    def test_a_missing_tool_column_is_refused(self):
        with open(self.tmp / "repos.csv", "w", newline="", encoding="utf-8") as f:
            f.write("repo_name,claude\no/a,TRUE\n")
        with self.assertRaises(KeyError):
            _render(self.tmp)


@unittest.skipUnless(DEPS, "pandas, numpy, matplotlib and seaborn are needed")
class TestOverTheFixture(unittest.TestCase):
    """The expected counts are the TRUE cells per tool column of the fixture's repos.csv, 32
    rows, counted by hand. The fixture predates OpenCode, and the `opencode` column is FALSE
    in every row."""

    def test_the_counts_computed_by_hand(self):
        labels, title, _ = _render(FIXTURE)
        self.assertEqual(labels, {"Claude": "22 (68.8%)", "Copilot": "12 (37.5%)", "No specific tool": "3 (9.4%)",
                                  "Cursor": "11 (34.4%)", "Gemini": "10 (31.2%)", "Codex": "7 (21.9%)",
                                  "OpenCode": "0 (0.0%)"})
        self.assertIn("(n = 32)", title)

    def test_the_counts_agree_with_a_stdlib_count_of_the_flags(self):
        with open(FIXTURE / "repos.csv", newline="", encoding="utf-8") as f:
            repos = list(csv.DictReader(f))
        labels, _, _ = _render(FIXTURE)
        for tool, label in zip(TOOLS, ["Claude", "Copilot", "No specific tool", "Cursor", "Gemini", "Codex"]):
            with self.subTest(tool=tool):
                flagged = sum(1 for r in repos if r[tool].upper() == "TRUE")
                self.assertTrue(labels[label].startswith(f"{flagged} ("), labels[label])


@unittest.skipUnless(DEPS, "pandas, numpy, matplotlib and seaborn are needed")
class TestMainOverTheFixture(unittest.TestCase):

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
        pdf = self.tmp / "dataset" / "tool_adoption.pdf"
        self.assertIn(f"Wrote {pdf}", logs.messages(result.stderr))
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual({p.name for p in SCRIPT_DIR.iterdir()}, self.before)


if __name__ == "__main__":
    unittest.main()
