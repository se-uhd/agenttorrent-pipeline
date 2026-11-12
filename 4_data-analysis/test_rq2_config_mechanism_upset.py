"""
Tests for rq2/config_mechanism_upset.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - The module's mechanism labels are compared with `_shared`'s.
  - `main()` reads `sys.argv`. It runs in-process with `sys.argv` replaced, over a small
    repos.csv under a temporary directory with DATA_DIR and OUTPUT_DIR replaced. The
    renderer is replaced by a recorder where the test checks the numbers handed to it, and
    left in place where the test checks the PDF.
  - One subprocess run over the fixture under testdata/output/.

Requirements: Python 3.14 (pandas and matplotlib, no network)
Run with:  python3 -m unittest test_rq2_config_mechanism_upset -v
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

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_config_mechanism_upset_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP
os.environ.setdefault("MPLBACKEND", "Agg")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("config_mechanism_upset", RQ2 / "config_mechanism_upset.py")
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


# Seven repositories: three with a context file alone, two with a context file and settings,
# one with skills alone, and one with no mechanism at all.
ROWS = [
    repo("a/1", "context_file"),
    repo("a/2", "context_file"),
    repo("a/3", "context_file"),
    repo("a/4", "context_file", "settings"),
    repo("a/5", "context_file", "settings"),
    repo("a/6", "skills"),
    repo("a/7"),
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
        with mock.patch.object(sys, "argv", ["config_mechanism_upset.py", *argv]), \
                contextlib.redirect_stderr(buffer):
            mod.main()
        return logs.messages(buffer.getvalue())




class TestTheCommandLine(_Run):

    def test_the_defaults_reach_the_renderer(self):
        with mock.patch.object(mod, "plot_upset") as renderer:
            self.run_main()
        kwargs = renderer.call_args.kwargs
        self.assertEqual((kwargs["top_n"], kwargs["min_count"], kwargs["show"]), (25, 1, False))
        self.assertTrue(kwargs["y_axis_log_scale"])
        self.assertEqual(kwargs["output_path"], self.out / "config_mechanism_upset.pdf")

    def test_the_flags_reach_the_renderer(self):
        with mock.patch.object(mod, "plot_upset") as renderer:
            self.run_main("--top-n", "3", "--min-count", "2", "--show")
        kwargs = renderer.call_args.kwargs
        self.assertEqual((kwargs["top_n"], kwargs["min_count"], kwargs["show"]), (3, 2, True))

    def test_an_unknown_flag_is_refused(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.run_main("--bogus")

    def test_an_abbreviated_flag_is_refused(self):
        """The script takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with mock.patch.object(mod, "plot_upset"), self.assertRaises(SystemExit) as stop, \
                contextlib.redirect_stderr(io.StringIO()):
            self.run_main("--top", "3")
        self.assertEqual(stop.exception.code, 2)


class TestTheNumbers(_Run):

    def test_the_intersections_are_over_the_repositories_with_a_mechanism(self):
        with mock.patch.object(mod, "plot_upset") as renderer:
            self.run_main()
        intersections, labels = renderer.call_args.args
        # Each of the ten mechanisms appears once. The labels are compared as a set, because
        # the script orders them itself.
        self.assertEqual(len(labels), 10)
        self.assertEqual(set(labels), {"Context Files", "Skills", "Subagents", "Commands",
                         "Rules", "Settings", "Hooks", "MCP", "Plugins", "LSP"})
        self.assertEqual(sum(it.count for it in intersections), 6)
        self.assertEqual([it.count for it in intersections], [3, 2, 1])
        largest = intersections[0]
        members = [label for label, present in zip(labels, largest.membership) if present]
        self.assertEqual(members, ["Context Files"])
        second = intersections[1]
        members = [label for label, present in zip(labels, second.membership) if present]
        self.assertEqual(members, ["Context Files", "Settings"])

    def test_the_title_counts_the_repositories_that_the_bars_partition(self):
        """`n` in the title counts the repositories in the bars. `a/7` has no mechanism and is
        in no bar, and the title therefore states 6 rather than the 7 rows."""
        with mock.patch.object(mod, "plot_upset") as renderer:
            self.run_main()
        intersections = renderer.call_args.args[0]
        self.assertIn("(n=6)", renderer.call_args.kwargs["title"])
        self.assertEqual(sum(it.count for it in intersections), 6)

    def test_a_min_count_above_the_largest_intersection_raises(self):
        with self.assertRaises(ValueError) as caught:
            self.run_main("--min-count", "4")
        self.assertIn("min-count", str(caught.exception))
        self.assertFalse((self.out / "config_mechanism_upset.pdf").exists())


class TestThePdf(_Run):

    def test_the_figure_is_written(self):
        out = self.run_main()
        pdf = self.out / "config_mechanism_upset.pdf"
        self.assertTrue(pdf.is_file())
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertIn(f"Wrote {pdf}", out)

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
        result = subprocess.run([sys.executable, str(RQ2 / "config_mechanism_upset.py")],
                                cwd=str(RQ2), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        pdf = tmp / "rq2" / "config_mechanism_upset.pdf"
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertEqual([p.name for p in (tmp / "rq2").iterdir()], ["config_mechanism_upset.pdf"])


if __name__ == "__main__":
    unittest.main()
