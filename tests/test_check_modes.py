"""
Tests that a read-only verification command never reports success from inputs that it
could not read.

Background:
  - A checker with its verdict in its exit code must not report a check that could not
    run as a match. Otherwise `snapshot write --check` on a clean checkout, where the
    files behind its largest section are gitignored and absent, would report agreement
    after comparing nothing, and so would the documentation watch with an empty hash
    record.
  - The weekly doc-watch workflow and CI read these exit codes, and a false agreement
    passes both.

The suite tests both directions. A checker must exit 0 when its inputs are present and
unchanged, and 1 (could not check) when they are absent. A test of the second direction
alone would pass a script that always fails. A non-zero exit alone is not enough either,
because a checker that crashes with a traceback and an entry that names a subcommand that
its module does not have both exit non-zero without reading anything.

Requirements: Python 3.14 (stdlib only). The present-inputs direction needs the step's
real inputs. Checks that need a whole handover or the values of an earlier paper are
covered with fixtures in their own suites. The present direction checks the tracked
snapshot through its output sections, without the collected trees.
Run with:  python3 -m unittest tests.test_check_modes -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from agentcontext import REPO_ROOT, STAGE_DIR

OUTPUT_DIR = STAGE_DIR / "output"

# The checkers of the analysis stage are scripts, and every checker of the collection stage
# is a package module.
ANALYSIS_DIR = REPO_ROOT / "4_data-analysis"
SCANNED_DIRS = (ANALYSIS_DIR,)


def _key(script: Path) -> str:
    """How a script is named in CHECKERS: its path relative to the repository root."""
    return os.path.relpath(script, REPO_ROOT)


PACKAGE_DIRS = tuple(REPO_ROOT / "agentcontext" / name for name in ("steps", "checks", "tools"))


def _command(script: str) -> list:
    """How a checker is started: a script by its path, a package module through `-m`, with
    the subcommand that the key has after the module's name."""
    if script.endswith(".py"):
        return [str(REPO_ROOT / script)]
    module, *subcommand = script.split()
    return ["-m", module, *subcommand]


def _module_key(path: Path) -> str:
    """How a package module is named in CHECKERS: its dotted name."""
    relative = path.relative_to(REPO_ROOT).with_suffix("")
    return ".".join(relative.parts)


def _run(args: list, cwd: Path = None) -> subprocess.CompletedProcess:
    """Run one checker. The bound only catches a checker that hangs. The statistics check
    counts the commit CSVs, and on a slow or emulated machine it runs far longer than the other
    checkers."""
    return subprocess.run([sys.executable] + [str(a) for a in args],
                          cwd=str(cwd or STAGE_DIR), capture_output=True, text=True,
                          timeout=3600)


# Each entry gives the arguments that point the checker at an empty directory (`absent`)
# and the arguments of a real run (`present`). `absent` is formatted with a temporary
# directory that exists and is empty.
CHECKERS = {
    "agentcontext.steps.engineered_project_selection": {
        # The step cannot filter an annotated CSV that is not there and therefore reaches
        # nothing but the filesystem.
        "absent": ["--check", "--annotated-csv", "{tmp}/repos_annotated.csv",
                   "-o", "{tmp}/repos_annotated_included.csv"],
        "present": ["--check"],
        # Both CSVs are gitignored, and repos_annotated.7z is the tracked copy of
        # repos_annotated.csv.
        "needs": ["../classification/repos_annotated.csv",
                  "../classification/repos_annotated_included.csv"],
    },
    "agentcontext.checks.snapshot write": {
        "absent": ["--check", "--collected-csv", "{tmp}/data/collected/repos.csv",
                   "--output-dir", "{tmp}"],
        "present": ["--check", "--outputs-only"],
        "needs": ["output/repos.csv", "output/snapshot.json"],
    },
    "agentcontext.checks.regressions check": {
        "absent": ["--output-dir", "{tmp}", "--baseline", "{tmp}/baseline.json"],
        "present": [],
        "needs": ["output/repos.csv", "output/baseline.json"],
    },
    "agentcontext.checks.regressions compare": {
        # Two roots without output/ are refused before anything is read. The stage
        # directory compared with itself is the present direction, and it differs nowhere.
        "absent": ["{tmp}/first", "{tmp}/second"],
        "present": [".", "."],
        "needs": ["output/repos.csv"],
    },
    "agentcontext.steps.commit_reclassification": {
        # Without a directory of per-repository commit records and without the lists, the
        # step reaches nothing but the filesystem.
        "absent": ["--commits-dir", "{tmp}/collect_commits", "--finished", "{tmp}/finished.jsonl",
                   "--collected-csv", "{tmp}/repos.csv", "--fail-csv", "{tmp}/clone_failed.csv",
                   "--ai-commits-csv", "{tmp}/all_ai_commits.csv",
                   "--ai-authorship", "{tmp}/ai_authorship.csv",
                   "--file-history", "{tmp}/file_history.csv"],
        # The reclassification needs the per-repository commit records of a collection. Its own
        # suite covers complete, accepted incomplete and refused evidence.
        "present": None,
        "needs": [],
    },
    "agentcontext.tools.subsample": {
        # The tool checks the GHS file first and refuses an empty directory on that file
        # before it opens any CSV, with the same non-zero exit.
        "absent": ["--check", "--output-dir", "{tmp}", "--validated-dir", "{tmp}",
                   "--collected-dir", "{tmp}", "--submodules", "{tmp}/submodules.csv",
                   "--deletion", "{tmp}/deletion.csv", "--ghs", "{tmp}/ghs_filtered.json",
                   "--subsample", "{tmp}/dev_subsample.csv"],
        # The selection needs the trees of a complete handover. The command line tests of
        # its own suite cover a successful selection and --check over fixtures.
        "present": None,
        "needs": [],
    },
    "agentcontext.checks.unmatched paths": {
        "absent": ["--other-csv", "{tmp}/other.csv"],
        "present": [],
        "needs": ["output/other.csv"],
    },
    "agentcontext.checks.deletions": {
        # A missing log is refused before the trees or the output CSVs are opened.
        "absent": ["--deletion-csv", "{tmp}/deletion.csv", "--collected-dir", "{tmp}",
                   "--validated-dir", "{tmp}", "--output-dir", "{tmp}"],
        "present": [],
        # The deletion log is gitignored and rewritten by every run. Without a collected tree,
        # two of the assertions cannot be checked and the check exits 1. A checkout with only
        # the lists therefore skips the present direction.
        "needs": ["deletion.csv", "output/repos.csv", "data/collected/*§*"],
    },
    "agentcontext.steps.output_cross_check": {
        # An empty directory contains no mechanism CSV, and the step refuses it before
        # anything is written.
        "absent": ["--output-dir", "{tmp}"],
        # The step runs over a copy of the tracked output, because it writes its report on
        # every run. In the stage directory, it would change a file that other suites read
        # while they run.
        "present": ["--output-dir", "{tmp}/output"],
        "copy": ["output"],
        "needs": ["output/repos.csv"],
    },
    "agentcontext.checks.commits": {
        # Without the commit CSV, the check reaches nothing but the filesystem.
        "absent": ["-i", "{tmp}/all_ai_commits.csv", "--summary-only"],
        # The current heuristics can disagree with archived labels. test_checks_commits tests
        # the exits for a success and for a mismatch over fixtures with explicit messages.
        "present": None,
        "needs": [],
    },
    "4_data-analysis/check_reproduction.py": {
        # One script over CSVs that are not there fails with exit 1. The output root is moved,
        # and neither direction writes beside the tracked figures.
        "absent": ["--only", "rq3/count_distribution", "--data-dir", "{tmp}/output",
                   "--output-dir", "{tmp}/figures"],
        # Under a moved output root, nothing is on disk before the first run. The check
        # therefore tests only the reproduction, over one script that reads a tracked CSV.
        "present": ["--only", "rq3/count_distribution", "--output-dir", "{tmp}/figures"],
        "needs": ["output/repos.csv"],
    },
    "agentcontext.checks.unmatched commits": {
        # Without a CSV of the unmatched attributions, the check reaches nothing but the
        # filesystem.
        "absent": ["--unmatched-csv", "{tmp}/unmatched.csv"],
        "present": [],
        # The cloning step, which clones every configured repository, writes the CSV. A
        # checkout that has not run the step therefore skips this direction rather than
        # failing it.
        "needs": ["output/all_ai_commits-unmatched.csv"],
    },
    "agentcontext.tools.docs": {
        # A heuristics file that is not there cites no page, and the watch makes no network
        # request.
        "absent": ["--heuristics", "{tmp}/config_path_patterns.csv"],
        # A real run fetches every cited page, and test_tools_docs covers it.
        "present": None,
        "needs": [],
    },
    "agentcontext.tools.statistics": {
        # A checkout without a statistics file has nothing to check against.
        "absent": ["--check", "--root", "{tmp}"],
        "present": ["--check"],
        # Some statistics are counted from the commit CSVs, which setup extracts from their
        # archives.
        "needs": [str(REPO_ROOT / "docs" / "statistics.json"), "output/all_ai_commits.csv",
                  "output/other_ai_commits.csv"],
    },
    "agentcontext.tools.mirror_run": {
        # The run cannot reset a checkout that is not there and therefore reaches no mirror.
        "absent": ["--checkout", "{tmp}/checkout", "--repo-csv", "{tmp}/run_list.csv",
                   "--mirrors", "{tmp}/mirrors", "--reference", "{tmp}/reference"],
        # A real run runs the cloning step over the mirrors, and test_tools_mirror_run covers
        # it.
        "present": None,
        "needs": [],
    },
    "agentcontext.checks.snapshot validation": {
        "absent": ["--summarize-only", "--no-snapshot", "-o", "{tmp}/results.csv"],
        "present": ["--summarize-only", "--no-snapshot"],
        # --summarize-only reads the gitignored data/collected/repos.csv to tell a complete
        # result file from a truncated result file.
        "needs": ["data/collected/repos.csv", "data/collected",
                  "output/snapshot-validation.csv", "output/snapshot.json"],
    },
}


class TestAbsentInputsAreNotSuccess(unittest.TestCase):
    """A checker with nothing to read must not report agreement."""

    def _assert_refuses(self, script: str, spec: dict) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = _command(script) + [a.format(tmp=tmp) for a in spec["absent"]]
            result = _run(args)
        # argparse exits 2 on a flag that the script does not have, and the dispatcher of a
        # module with subcommands exits 2 on a subcommand that it does not have. Both exits
        # are non-zero although the checker never ran. An entry with a flag that does not
        # exist would therefore pass on a usage error.
        self.assertNotIn(
            "unrecognized arguments", result.stderr,
            f"{script} was run with a flag that it does not have, so this case proves nothing "
            f"about what the checker does with absent inputs.\nstderr: {result.stderr[-500:]}")
        # The test ignores case, because the dispatcher writes "Unknown subcommand". A usage
        # error exits 1 as an absent input does, and only this assertion tells a usage error
        # from an absent input.
        self.assertNotIn(
            "unknown subcommand", result.stderr.lower(),
            f"{script} names a subcommand that its module does not have, so this case proves "
            f"nothing about what the checker does with absent inputs.\n"
            f"stderr: {result.stderr[-500:]}")
        # The traceback tells a crash from a refusal, because a crash exits 1 too.
        self.assertNotIn(
            "Traceback", result.stderr,
            f"{script} crashed on its absent inputs instead of reporting that it could not check."
            f"\nstderr: {result.stderr[-1500:]}")
        # 1 is "could not check" and 2 is "found a difference". A checker that read nothing
        # and exits 2 reports a difference that it never compared.
        self.assertEqual(
            result.returncode, 1,
            f"{script} exited {result.returncode} with its inputs absent. Its exit code is its "
            f"verdict, and a run that read nothing has to exit 1, could not check.\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}")


class TestPresentInputsAreSuccess(unittest.TestCase):
    """The other direction, so that a checker that always fails cannot pass the suite."""

    def _assert_accepts(self, script: str, spec: dict) -> None:
        if spec["present"] is None:
            # The comment of each entry states why a fixture or a run over a handover covers
            # its present direction instead of the current checkout.
            self.skipTest(f"{script} has no present-inputs case here; see its CHECKERS entry")
        for name in spec["needs"]:
            # A glob stands for a tree of collected repositories, not for one named path, and
            # needs one match.
            if not (any(STAGE_DIR.glob(name)) if "*" in name else (STAGE_DIR / name).exists()):
                self.skipTest(f"{name} has not been generated")
        # This direction uses a temporary directory too, because the reproduction check
        # writes what it checked and needs a directory that is not beside the tracked figures.
        # Unformatted, `{tmp}` would be a directory of that name under the stage directory.
        with tempfile.TemporaryDirectory() as tmp:
            for name in spec.get("copy", ()):
                shutil.copytree(STAGE_DIR / name, Path(tmp) / name)
            result = _run(_command(script) + [a.format(tmp=tmp) for a in spec["present"]])
        self.assertEqual(result.returncode, 0,
                         f"{script} exited {result.returncode} against the committed output.\n"
                         f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}")


def _attach(case_class, method):
    """Attach one test method per entry of CHECKERS, so that every entry is run.

    The completeness check compares only CHECKERS with the filesystem. With methods written
    by hand, an entry added for the completeness check would satisfy it without ever running
    the script.
    """
    for script, spec in CHECKERS.items():
        def test(self, script=script, spec=spec):
            getattr(self, method)(script, spec)
        safe = re.sub(r"\W", "_", script[:-3] if script.endswith(".py") else script)
        test.__name__ = f"test_{safe}"
        setattr(case_class, test.__name__, test)
        case_class.COVERED = getattr(case_class, "COVERED", set()) | {script}


_attach(TestAbsentInputsAreNotSuccess, "_assert_refuses")
_attach(TestPresentInputsAreSuccess, "_assert_accepts")


class TestAUsageErrorIsNoVerdict(unittest.TestCase):
    """A flag that a checker does not have is a usage error, which exits 1 (could not check).
    Plain argparse exits 2 on it, a checker's exit code for a difference that it found, and a
    mistyped flag such as `snapshot write --chek` would then report a difference that the
    checker never looked for."""

    def test_every_checker_exits_1_on_an_unknown_flag(self):
        for script in CHECKERS:
            with self.subTest(checker=script):
                result = _run(_command(script) + ["--no-such-flag"])
                self.assertEqual(result.returncode, 1, result.stderr[-500:])
                self.assertNotIn("Traceback", result.stderr)


class TestEveryCheckerIsCovered(unittest.TestCase):
    """Fails in both directions, as test_heuristics.py does, on a new checker that is not in
    CHECKERS and on an entry of CHECKERS for a script that is no longer a checker."""

    # A script is a checker when its exit code is a verdict rather than only success or
    # failure, that is, when it offers a mode such as --check or its main() can return 2.
    SIGNATURES = (re.compile(r'add_argument\(\s*"--check"'),
                  re.compile(r'add_argument\(\s*"--summarize-only"'),
                  re.compile(r"^\s+return 2\b", re.MULTILINE))

    def _discovered(self) -> set:
        """Every script and package module with an exit code that is a verdict, by module name.

        A module with subcommands has one entry in CHECKERS per subcommand that is a checker,
        and the first word of each entry is the module. The two completeness checks therefore
        compare modules.
        """
        found = set()
        for directory in SCANNED_DIRS:
            for script in sorted(directory.glob("*.py")):
                if script.name.startswith("test_"):
                    continue
                source = script.read_text(encoding="utf-8")
                if any(pattern.search(source) for pattern in self.SIGNATURES):
                    found.add(_key(script))
        # The steps, checks and tools that are package modules, by their dotted name. A
        # module with subcommands ends in a dispatcher that returns 2 for a subcommand that it
        # does not have. That 2 is argparse's exit code for a usage error and not a verdict,
        # and the scan therefore stops before the dispatcher.
        for directory in PACKAGE_DIRS:
            for module in sorted(directory.glob("*.py")):
                if module.name == "__init__.py":
                    continue
                source = module.read_text(encoding="utf-8").split("\nSUBCOMMANDS = {")[0]
                if any(pattern.search(source) for pattern in self.SIGNATURES):
                    found.add(_module_key(module))
        return found

    def test_every_checker_in_the_step_is_in_the_table(self):
        missing = self._discovered() - {key.split()[0] for key in CHECKERS}
        self.assertEqual(missing, set(),
                         f"these scripts give a verdict in their exit code but are not "
                         f"covered by this suite: {sorted(missing)}")

    def test_the_table_names_no_script_that_stopped_being_one(self):
        stale = {key.split()[0] for key in CHECKERS} - self._discovered()
        self.assertEqual(stale, set(),
                         f"these are in CHECKERS but no longer have a checker's signature: "
                         f"{sorted(stale)}")

    def test_every_table_entry_got_a_test_method_in_both_directions(self):
        """This test checks that the methods are generated from CHECKERS. A list written by
        hand would let an entry of CHECKERS go untested while both completeness checks pass."""
        for case in (TestAbsentInputsAreNotSuccess, TestPresentInputsAreSuccess):
            self.assertEqual(getattr(case, "COVERED", set()), set(CHECKERS), case.__name__)

    def test_the_discovery_scans_the_tools_too(self):
        tools = REPO_ROOT / "agentcontext" / "tools"
        self.assertIn(tools, PACKAGE_DIRS)
        # Without this assertion, the test would restate the definition of PACKAGE_DIRS and
        # pass over a directory that is not there, and a rename of `tools/` would go
        # unreported.
        self.assertTrue(tools.is_dir())

    def test_the_discovery_finds_something(self):
        self.assertGreaterEqual(len(self._discovered()), 4)


if __name__ == "__main__":
    unittest.main()
