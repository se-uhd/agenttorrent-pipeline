"""
Tests for rq2/upset_plot.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - `main(argv)` is run in-process over a small repos.csv under a temporary directory with
    DATA_DIR and OUTPUT_DIR replaced. The renderer is replaced by a recorder where the test
    checks the intersections handed to it, and left in place where the test checks the PDF.
  - Over the fixture, the largest intersection is 3, below MIN_SUBSET_SIZE. A run with the
    default draws nothing and exits 1, and a run with `--min-count 1` draws the fixture. Both
    runs are subprocesses.

Requirements: Python 3.14 (pandas and matplotlib, no network)
Run with:  python3 -m unittest test_rq2_upset_plot -v
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

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_upset_plot_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("upset_plot", RQ2 / "upset_plot.py")
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
                         "dataset_version": "1.0.0"})
            full.update(row)
            writer.writerow(full)


# Nine repositories: five with Claude alone, two with Claude and AGENTS.md, one with no specific tool
# (both `agents.md` and `no_specific_tool` set), and one with neither a tool nor AGENTS.md.
ROWS = [
    *(repo(f"a/{i}", "claude") for i in range(1, 6)),
    repo("b/1", "claude", "agents.md"),
    repo("b/2", "claude", "agents.md"),
    repo("c/1", "agents.md", "no_specific_tool"),
    repo("d/1"),
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
        write_repos_csv(self.data / "repos.csv", ROWS)

    def run_main(self, *argv: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.main(list(argv))
        return logs.messages(buffer.getvalue())

    def recorded(self, *argv: str):
        with mock.patch.object(mod, "plot_upset") as renderer:
            out = self.run_main(*argv)
        return out, renderer.call_args




class TestTheCommandLine(_Run):

    def test_the_default_renderer_threshold_is_five(self):
        _, call = self.recorded()
        self.assertEqual(call.kwargs["min_count"], 5)

    def test_min_count_reaches_the_renderer(self):
        _, call = self.recorded("--min-count", "2")
        self.assertEqual(call.kwargs["min_count"], 2)

    def test_an_unknown_flag_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main("--bogus")

    def test_an_abbreviated_flag_is_refused(self):
        """The script takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with self.assertRaises(SystemExit) as stop, contextlib.redirect_stderr(io.StringIO()):
            self.recorded("--min", "2")
        self.assertEqual(stop.exception.code, 2)


class TestTheIntersections(_Run):

    def test_the_bars_are_over_the_repositories_with_a_tool_or_agents_md(self):
        out, call = self.recorded()
        intersections, labels = call.args
        self.assertEqual(labels, ["Claude", "Copilot", "Cursor", "Gemini", "Codex", "OpenCode",
                                  "AGENTS.md"])
        self.assertEqual(sum(it.count for it in intersections), 8)
        self.assertIn("8 repositories use at least one of", out)
        self.assertIn("(n=8)", call.kwargs["title"])

    def test_agents_md_is_the_context_file_flag_and_not_the_no_specific_tool_flag(self):
        """The rename is scoped to the tools and `agents.md`. If `no_specific_tool` were
        renamed too, the DataFrame would have two AGENTS.md columns, and the b/ rows, which
        have the file and Claude, would not count as AGENTS.md."""
        _, call = self.recorded()
        intersections, labels = call.args
        by_members = {tuple(label for label, present in zip(labels, it.membership) if present): it.count
                      for it in intersections}
        self.assertEqual(by_members, {("Claude",): 5, ("Claude", "AGENTS.md"): 2, ("AGENTS.md",): 1})

    def test_the_colors_reach_the_renderer(self):
        _, call = self.recorded()
        self.assertEqual(call.kwargs["set_colors"], mod.CAT_COLORS)
        self.assertEqual(call.kwargs["output_path"], self.out / "upset-tool-usage.pdf")


class TestThePdf(_Run):

    def test_the_default_draws_when_an_intersection_reaches_five(self):
        out = self.run_main()
        pdf = self.out / "upset-tool-usage.pdf"
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertIn(f"Wrote {pdf}", out)

    def test_a_min_count_above_the_largest_intersection_raises_and_writes_nothing(self):
        with self.assertRaises(ValueError) as caught:
            self.run_main("--min-count", "6")
        self.assertIn("min-count", str(caught.exception))
        self.assertFalse((self.out / "upset-tool-usage.pdf").exists())

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_main()
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestOverTheFixture(unittest.TestCase):

    def _run(self, *argv: str) -> tuple:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(RQ2 / "upset_plot.py"), *argv],
                                cwd=str(RQ2), env=env, capture_output=True, text=True)
        return result, tmp / "rq2"

    def test_the_default_refuses_the_fixture(self):
        """The largest intersection over the 32 repositories is 3."""
        result, out = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No intersection has at least 5 repositories. Lower --min-count.", result.stderr)
        self.assertFalse((out / "upset-tool-usage.pdf").exists())

    def test_min_count_one_draws_the_fixture(self):
        result, out = self._run("--min-count", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("32 repositories use at least one of", logs.messages(result.stderr))
        self.assertTrue((out / "upset-tool-usage.pdf").read_bytes().startswith(b"%PDF"))
        self.assertEqual([p.name for p in out.iterdir()], ["upset-tool-usage.pdf"])


if __name__ == "__main__":
    unittest.main()
