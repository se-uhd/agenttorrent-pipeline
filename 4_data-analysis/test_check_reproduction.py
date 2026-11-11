"""
Tests for check_reproduction.py

Strategy:
  - The check is loaded by path and its SCRIPTS and ROOT are replaced by a temporary tree of
    small scripts: one that writes the same bytes every run, one that writes other bytes
    every run, one with an absent input, one that exits non-zero, and one that writes the
    environment that it runs under. Each verdict of the exit code is then one run of main()
    over a selection of these scripts.
  - missing_inputs, output_dir_of and digests are tested directly.
  - The real list, SCRIPTS, is checked against the scripts on disk, and --list is run over
    it. No real analysis script runs in this suite, and tests/test_check_modes.py runs one.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_check_reproduction -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentcontext import logs

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


check = _load("check_reproduction_under_test", HERE / "check_reproduction.py")
REAL_ROOT = check.ROOT

# Writes where `_shared.output_dir` would: under the data root's analysis directory, or under
# ANALYSIS_OUTPUT_DIR, with the directory name kept. Restated rather than imported, so that the
# check is tested on its own.
_OUTPUT_DIR = """
import os
from pathlib import Path
here = Path(__file__).resolve()
root = os.environ.get("ANALYSIS_OUTPUT_DIR")
base = Path(root) if root else Path(os.environ["AGENTCONTEXT_DATA_ROOT"]) / "analysis"
out = base / here.parent.name
out.mkdir(parents=True, exist_ok=True)
"""

STEADY = _OUTPUT_DIR + '(out / "steady.txt").write_bytes(b"the same bytes\\n")\n'
DRIFTING = _OUTPUT_DIR + 'import os\n(out / "drifting.txt").write_bytes(os.urandom(8))\n'
NEEDY = _OUTPUT_DIR + '(out / "needy.txt").write_bytes(b"ran\\n")\n'
FAILING = 'import sys\nsys.exit(3)\n'
PROBE = _OUTPUT_DIR + """
import json
names = ["SOURCE_DATE_EPOCH", "PYTHONDONTWRITEBYTECODE", "MPLBACKEND",
         "DATA_COLLECTION_OUTPUT", "ANALYSIS_OUTPUT_DIR"]
(out / "probe.json").write_text(json.dumps({n: os.environ.get(n) for n in names}))
"""


class _Tree(unittest.TestCase):
    """A temporary root with the fake scripts under `4_data-analysis/fake/`, a data root with
    their outputs under `analysis/fake/`, a directory of output CSVs, and the check pointed at
    them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        self.scripts = self.root / "4_data-analysis" / "fake"
        self.scripts.mkdir(parents=True)
        self.data = self.tmp / "data"
        self.data.mkdir()
        for name, source in (("steady.py", STEADY), ("drifting.py", DRIFTING),
                             ("needy.py", NEEDY), ("failing.py", FAILING), ("probe.py", PROBE)):
            (self.scripts / name).write_text(source, encoding="utf-8")
        self.entries = {
            "steady": ("4_data-analysis/fake/steady.py", ()),
            "drifting": ("4_data-analysis/fake/drifting.py", ()),
            "needy": ("4_data-analysis/fake/needy.py", ("{data}/table.csv",)),
            "failing": ("4_data-analysis/fake/failing.py", ()),
            "probe": ("4_data-analysis/fake/probe.py", ()),
        }
        self.data_root = self.tmp / "data_root"
        self.outputs = self.data_root / "analysis" / "fake"
        for patcher in (mock.patch.object(check, "ROOT", self.root),
                        mock.patch.object(check, "DATA_ROOT", self.data_root),
                        mock.patch.object(check, "ANALYSIS_DIR", self.data_root / "analysis"),
                        mock.patch.dict(os.environ, {"AGENTCONTEXT_DATA_ROOT": str(self.data_root)})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_check(self, *names, args=()):
        """main() over the named fake scripts, returning the exit code and the log messages."""
        scripts = tuple(self.entries[n] for n in names)
        out = io.StringIO()
        with mock.patch.object(check, "SCRIPTS", scripts), contextlib.redirect_stderr(out):
            code = check.main(["--data-dir", str(self.data), *args])
        return code, logs.messages(out.getvalue())

    def beside(self) -> set:
        """What is in the scripts' output directory under the data root."""
        return {p.name for p in self.outputs.iterdir()} if self.outputs.is_dir() else set()

    def settle(self, name: str, content: bytes) -> None:
        """Put bytes into the scripts' output directory, as a tracked output would be."""
        self.outputs.mkdir(parents=True, exist_ok=True)
        (self.outputs / name).write_bytes(content)


class TestMissingInputs(_Tree):

    def test_no_needs_is_nothing_missing(self):
        self.assertEqual(check.missing_inputs((), self.data), [])

    def test_data_stands_for_the_data_directory(self):
        self.assertEqual(check.missing_inputs(("{data}/table.csv",), self.data), ["{data}/table.csv"])
        (self.data / "table.csv").write_text("a\n")
        self.assertEqual(check.missing_inputs(("{data}/table.csv",), self.data), [])

    def test_one_alternative_is_enough(self):
        need = "{data}/table.csv|{data}/table.csv.7z"
        self.assertEqual(check.missing_inputs((need,), self.data), [need])
        (self.data / "table.csv.7z").write_bytes(b"")
        self.assertEqual(check.missing_inputs((need,), self.data), [])

    def test_a_missing_group_is_reported_whole(self):
        """The log message names the alternatives as one string, the way the list gives them."""
        needs = ("{data}/a.csv|{data}/b.csv", "{data}/c.csv")
        (self.data / "c.csv").write_text("")
        self.assertEqual(check.missing_inputs(needs, self.data), ["{data}/a.csv|{data}/b.csv"])

    def test_a_relative_glob_resolves_against_the_data_root(self):
        (self.data_root / "collection" / "data" / "validated" / "o§r").mkdir(parents=True)
        self.assertEqual(check.missing_inputs(("collection/data/validated/*§*",), self.data), [])
        self.assertEqual(check.missing_inputs(("collection/data/collected/*§*",), self.data),
                         ["collection/data/collected/*§*"])

    def test_a_recursive_glob_finds_a_nested_file(self):
        (self.data_root / "sample" / "a" / "b").mkdir(parents=True)
        (self.data_root / "sample" / "a" / "b" / "_repo-tree.json").write_text("{}")
        self.assertEqual(check.missing_inputs(("sample/**/_repo-tree.json",), self.data), [])


class TestOutputDirAndDigests(_Tree):

    def test_without_a_root_a_script_writes_under_the_data_root(self):
        script = self.scripts / "steady.py"
        self.assertEqual(check.output_dir_of(script, None), self.outputs)

    def test_under_a_root_the_directory_name_is_kept(self):
        script = self.scripts / "steady.py"
        self.assertEqual(check.output_dir_of(script, self.tmp / "out"), self.tmp / "out" / "fake")

    def test_an_absent_directory_has_no_digests(self):
        self.assertEqual(check.digests(self.tmp / "nowhere"), {})

    def test_scripts_caches_and_dotfiles_are_not_outputs(self):
        (self.scripts / "figure.pdf").write_bytes(b"%PDF")
        (self.scripts / "steady.pyc").write_bytes(b"")
        (self.scripts / ".DS_Store").write_bytes(b"")
        found = check.digests(self.scripts)
        self.assertEqual({p.name for p in found}, {"figure.pdf"})
        self.assertEqual(len(found[self.scripts / "figure.pdf"]), 64)


class TestTheVerdicts(_Tree):

    def test_a_script_that_reproduces_and_moves_nothing_is_0(self):
        self.settle("steady.txt", b"the same bytes\n")
        code, out = self.run_check("steady")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    4_data-analysis/fake/steady.py wrote 1 file(s)", out)
        self.assertIn("1 script(s) ran twice, 0 skipped, 0 failed. 0 output(s) changed, 0 did not reproduce", out)
        self.assertNotIn("CHANGED", out)
        self.assertNotIn("DIFFERS", out)

    def test_second_run_failure_is_reported_even_when_output_bytes_match(self):
        source = STEADY + """
marker = here.parent / '.ran'
if marker.exists():
    raise SystemExit(3)
marker.write_text('first run finished')
"""
        (self.scripts / 'steady.py').write_text(source)
        self.settle('steady.txt', b'the same bytes\n')
        code, report = self.run_check('steady')
        self.assertEqual(code, 1, report)
        self.assertIn('exited 3 on the second run', report)
        self.assertIn('1 failed', report)

    def test_an_output_that_was_not_there_before_counts_as_changed(self):
        """The check logs a new file that a script starts writing in the same way as a
        changed file, because no earlier bytes are on disk to compare with."""
        code, out = self.run_check("steady")
        self.assertEqual(code, 2, out)
        self.assertIn("CHANGED  analysis/fake/steady.txt", out)

    def test_an_output_that_changed_is_2(self):
        self.settle("steady.txt", b"what the last run left\n")
        code, out = self.run_check("steady")
        self.assertEqual(code, 2, out)
        self.assertIn("CHANGED  analysis/fake/steady.txt", out)
        self.assertIn("1 output(s) changed, 0 did not reproduce", out)

    def test_a_figure_that_no_script_wrote_is_2(self):
        """The check compares only what the scripts write. Without this test, a figure that a
        script stopped writing could stay on disk unchecked while the check exits 0."""
        self.settle("steady.txt", b"the same bytes\n")
        self.settle("dropped.pdf", b"a figure no script writes any more\n")
        code, out = self.run_check("steady")
        self.assertEqual(code, 2, out)
        self.assertIn("UNWRITTEN analysis/fake/dropped.pdf (no script wrote it)", out)
        self.assertIn("1 written by no script", out)
        # Neither a selection nor --reproduce-only checks for unwritten outputs, because the
        # other scripts did not run.
        for args in (("--only", "steady"), ("--reproduce-only",)):
            with self.subTest(args=args):
                code, out = self.run_check("steady", args=args)
                self.assertEqual(code, 0, out)
                self.assertNotIn("UNWRITTEN", out)

    def test_a_csv_or_its_latex_that_no_script_wrote_is_2_and_another_file_is_not(self):
        """This case tests the suffixes of an output other than the figure's. Without it, a
        check that dropped `.tex` from the suffixes would log nothing about a LaTeX table that
        no script writes any more."""
        self.settle("steady.txt", b"the same bytes\n")
        self.settle("notes.txt", b"not a figure or a table\n")
        code, out = self.run_check("steady")
        self.assertEqual(code, 0, out)
        for name in ("dropped.csv", "dropped.tex"):
            with self.subTest(output=name):
                self.settle(name, b"a table no script writes any more\n")
                code, out = self.run_check("steady")
                self.assertEqual(code, 2, out)
                self.assertIn(f"UNWRITTEN analysis/fake/{name} (no script wrote it)", out)
                (self.outputs / name).unlink()

    def test_a_selector_that_matches_nothing_is_not_agreement(self):
        """Exit 0 means that every script ran and every output reproduced. A selector that
        matches no script checks nothing, and exit 0 would then pass a stale or mistyped
        selector as agreement."""
        out = err = io.StringIO()
        with (mock.patch.object(check, "SCRIPTS", (self.entries["steady"],)),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err)):
            code = check.main(["--data-dir", str(self.data), "--only", "rq9/nosuchscript"])
        self.assertEqual(code, 1, logs.messages(out.getvalue()))
        self.assertIn("matches none", err.getvalue())

    def test_reproduce_only_does_not_check_whether_an_output_changed(self):
        self.settle("steady.txt", b"what the last run left\n")
        code, out = self.run_check("steady", args=["--reproduce-only"])
        self.assertEqual(code, 0, out)
        self.assertNotIn("CHANGED", out)

    def test_an_output_that_does_not_reproduce_is_2(self):
        code, out = self.run_check("drifting")
        self.assertEqual(code, 2, out)
        self.assertIn("DIFFERS  analysis/fake/drifting.txt", out)
        self.assertIn("1 did not reproduce", out)

    def test_reproduce_only_still_reports_what_did_not_reproduce(self):
        code, out = self.run_check("drifting", args=["--reproduce-only"])
        self.assertEqual(code, 2, out)
        self.assertIn("DIFFERS", out)

    def test_a_missing_input_is_1(self):
        code, out = self.run_check("needy")
        self.assertEqual(code, 1, out)
        self.assertIn("SKIP  4_data-analysis/fake/needy.py (missing {data}/table.csv)", out)
        self.assertIn("0 script(s) ran twice, 1 skipped, 0 failed", out)
        self.assertNotIn("needy.txt", self.beside())

    def test_skip_missing_turns_it_into_a_skip(self):
        self.settle("steady.txt", b"the same bytes\n")
        code, out = self.run_check("steady", "needy", args=["--skip-missing"])
        self.assertEqual(code, 0, out)
        self.assertIn("1 script(s) ran twice, 1 skipped, 0 failed", out)

    def test_a_run_in_which_every_script_was_skipped_checked_nothing(self):
        """Exit 0 means that every script ran and reproduced. With --skip-missing and every
        input absent, the check skips every script, compares nothing and exits 1."""
        code, out = self.run_check("needy", args=["--skip-missing"])
        self.assertEqual(code, 1, out)
        self.assertIn("0 script(s) ran twice, 1 skipped, 0 failed", out)
        self.assertIn("All 1 selected script(s) were skipped. Nothing was checked.", out)

    def test_an_input_that_is_there_lets_the_script_run(self):
        (self.data / "table.csv").write_text("a\n")
        self.settle("needy.txt", b"ran\n")
        code, out = self.run_check("needy")
        self.assertEqual(code, 0, out)
        self.assertIn("ok    4_data-analysis/fake/needy.py", out)

    def test_a_script_that_fails_is_1(self):
        code, out = self.run_check("failing")
        self.assertEqual(code, 1, out)
        self.assertIn("FAIL  4_data-analysis/fake/failing.py exited 3", out)
        self.assertIn("0 script(s) ran twice, 0 skipped, 1 failed", out)

    def test_a_failure_outranks_a_difference(self):
        code, out = self.run_check("drifting", "failing")
        self.assertEqual(code, 1, out)
        self.assertIn("DIFFERS", out)

    def test_a_skip_that_is_allowed_does_not_hide_a_difference(self):
        code, out = self.run_check("drifting", "needy", args=["--skip-missing"])
        self.assertEqual(code, 2, out)


class TestSelectionAndListing(_Tree):

    def test_only_selects_by_substring_of_the_path(self):
        self.settle("steady.txt", b"the same bytes\n")
        code, out = self.run_check("steady", "failing", args=["--only", "steady"])
        self.assertEqual(code, 0, out)
        self.assertIn("steady.py", out)
        self.assertNotIn("failing.py", out)
        self.assertIn("1 script(s) ran twice", out)

    def test_list_names_every_script_and_runs_none(self):
        code, out = self.run_check("steady", "needy", args=["--list"])
        self.assertEqual(code, 0)
        self.assertIn("4_data-analysis/fake/steady.py can run here", out)
        self.assertIn("4_data-analysis/fake/needy.py cannot run here (missing {data}/table.csv)",
                      out)
        self.assertEqual(self.beside(), set())

    def test_list_respects_only(self):
        code, out = self.run_check("steady", "needy", args=["--list", "--only", "needy"])
        self.assertEqual(code, 0)
        self.assertNotIn("steady.py", out)
        self.assertIn("needy.py", out)


class TestTheOutputRoot(_Tree):

    def test_output_dir_keeps_the_tracked_directory_untouched(self):
        self.settle("steady.txt", b"the tracked bytes\n")
        out_root = self.tmp / "figures"
        code, out = self.run_check("steady", args=["--output-dir", str(out_root)])
        self.assertEqual(code, 0, out)
        self.assertEqual((self.outputs / "steady.txt").read_bytes(), b"the tracked bytes\n")
        self.assertEqual((out_root / "fake" / "steady.txt").read_bytes(), b"the same bytes\n")
        self.assertNotIn("CHANGED", out)

    def test_under_output_dir_a_difference_is_still_reported(self):
        code, out = self.run_check("drifting", args=["--output-dir", str(self.tmp / "figures")])
        self.assertEqual(code, 2, out)
        self.assertIn("DIFFERS", out)
        self.assertEqual(self.beside(), set())

    def test_the_environment_a_script_runs_under(self):
        code, _ = self.run_check("probe", args=["--output-dir", str(self.tmp / "figures")])
        self.assertEqual(code, 0)
        seen = json.loads((self.tmp / "figures" / "fake" / "probe.json").read_text())
        self.assertEqual(seen["SOURCE_DATE_EPOCH"], "0")
        self.assertEqual(seen["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(seen["MPLBACKEND"], "Agg")
        self.assertEqual(seen["DATA_COLLECTION_OUTPUT"], str(self.data.resolve()))
        self.assertEqual(seen["ANALYSIS_OUTPUT_DIR"], str((self.tmp / "figures").resolve()))

    def test_without_output_dir_the_override_is_cleared(self):
        """A root left in the caller's environment would make the tracked directory look
        untouched while the script wrote elsewhere."""
        with mock.patch.dict(os.environ, {"ANALYSIS_OUTPUT_DIR": str(self.tmp / "stale")}):
            code, _ = self.run_check("probe", args=["--reproduce-only"])
        self.assertEqual(code, 0)
        self.assertFalse((self.tmp / "stale").exists())
        seen = json.loads((self.outputs / "probe.json").read_text())
        self.assertIsNone(seen["ANALYSIS_OUTPUT_DIR"])


class TestTheRealList(unittest.TestCase):

    # The directories with the scripts that the check runs or names in NOT_RUN. A module with a
    # name that starts with `_` is a helper that the scripts import, and one that starts with
    # `test_` is a suite.
    SCRIPT_DIRS = ("4_data-analysis/rq2", "4_data-analysis/rq3", "4_data-analysis/dataset",
                   "2_classification/diagnostics")

    def test_every_script_is_run_or_named_as_not_run(self):
        """A script added to one of these directories and to neither list would run nowhere."""
        listed = {rel for rel, _ in check.SCRIPTS}
        self.assertFalse(listed & set(check.NOT_RUN), "a script is both run and not run")
        found = 0
        for directory in self.SCRIPT_DIRS:
            for script in sorted((REAL_ROOT / directory).glob("*.py")):
                if script.name.startswith(("_", "test_")):
                    continue
                found += 1
                rel = script.relative_to(REAL_ROOT).as_posix()
                with self.subTest(script=rel):
                    self.assertTrue(rel in listed or rel in check.NOT_RUN,
                                    "in neither SCRIPTS nor NOT_RUN")
        self.assertGreater(found, len(check.NOT_RUN))

    def test_every_script_not_run_is_documented_in_its_directorys_readme(self):
        """The reason is in NOT_RUN, and the directory's README states it too, because a
        reader of the script looks there."""
        for rel, reason in check.NOT_RUN.items():
            with self.subTest(script=rel):
                script = REAL_ROOT / rel
                self.assertTrue(script.is_file(), rel)
                self.assertTrue(reason)
                readme = (script.parent / "0_README.md").read_text(encoding="utf-8")
                self.assertIn(script.name, readme)

    def test_every_listed_script_is_on_disk(self):
        for rel, needs in check.SCRIPTS:
            with self.subTest(script=rel):
                self.assertTrue((REAL_ROOT / rel).is_file(), rel)
                self.assertIsInstance(needs, tuple)

    def test_the_commit_scripts_accept_the_archive(self):
        for rel, needs in check.SCRIPTS:
            if rel.endswith(("commit_message_first_words.py", "monthly_ai_commit_volume.py")):
                self.assertEqual(needs, ("{data}/all_ai_commits.csv|{data}/all_ai_commits.csv.7z.*|{data}/all_ai_commits.csv.7z",))

    def test_list_over_the_real_list_names_every_script(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = check.main(["--list"])
        self.assertEqual(code, 0)
        for rel, _ in check.SCRIPTS:
            self.assertIn(rel, logs.messages(out.getvalue()))


if __name__ == "__main__":
    unittest.main()
