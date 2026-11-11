"""
Tests for agentcontext.runner: the steps, their completion and resumption, and whether
the blocks of the CI workflow match the suites on disk.

Strategy:
  - The runner's paths are module-level constants. The tests reassign
    LOG_DIR, LOCK_PATH and STATE_PATH on the imported module and restore
    them in tearDown.
  - No real step runs. The tests cover the resume, lock and pre-checks paths
    by running the runner over trivial step lists, without cloning a
    repository.
  - The tests get a dead pid by starting a process and reaping it.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_runner -v
"""

import contextlib
import csv
import dataclasses
import errno
import fcntl
import hashlib
import importlib
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

from agentcontext import heuristics
_mod = importlib.import_module("agentcontext.runner")

# The runner's module constants that the cases below assign for a scenario. tearDownModule
# restores them, so that a suite that imports the runner after this one in the same process
# reads the runner's own values. test_steps_repository_cloning checks the disk minimum against
# these values.
_RUNNER_DEFAULTS = {name: getattr(_mod, name) for name in (
    "COMMIT_RECLASSIFICATION_STEPS", "BINARY_REQUIREMENTS", "CLONING_STEPS", "CORE_STEPS",
    "DEFAULT_SCRATCH_DIR", "LOCK_PATH", "LOG_DIR", "MIN_FREE_DISK_GB",
    "PYTHON_VERSION_FILE", "STAGE_DIR", "STATE_PATH", "VENV_PYTHON", "_CHILD", "_STOPPING")}


def tearDownModule():
    for name, value in _RUNNER_DEFAULTS.items():
        setattr(_mod, name, value)
from agentcontext import REPO_ROOT, STAGE_DIR
from tests._fixtures import file_system_sync_never_returns, git_version_fails

_flag_value = _mod._flag_value
_git_info = _mod._git_info
_step_argv = _mod._step_argv
COMPLETION_OUTPUT = _mod.COMPLETION_OUTPUT
COMPLETION_INPUTS = _mod.COMPLETION_INPUTS
COMPLETION_SCRIPT = _mod.COMPLETION_SCRIPT
Step = _mod.Step
acquire_lock = _mod.acquire_lock
apply_step_overrides = _mod.apply_step_overrides
apply_repo_csv = _mod.apply_repo_csv
resolve_step_python = _mod.resolve_step_python
completed_steps = _mod.completed_steps
drop_completed_from = _mod.drop_completed_from
forward_stop = _mod.forward_stop
invalidated_by = _mod.invalidated_by
log_path = _mod.log_path
main = _mod.main
modified_input_problems = _mod.modified_input_problems
parse_step_arg = _mod.parse_step_arg
pending_step_names = _mod.pending_step_names
pre_checks = _mod.pre_checks
read_state = _mod.read_state
write_execution_state = _mod.write_execution_state
release_lock = _mod.release_lock
run_step = _mod.run_step
fingerprint_subject = _mod.fingerprint_subject
counted_entries = _mod.counted_entries
manifest_lines = _mod.manifest_lines
mark_complete = _mod.mark_complete
resolve_produced = _mod.resolve_produced
UnreadableOutput = _mod.UnreadableOutput
output_fingerprint = _mod.output_fingerprint
script_fingerprint = _mod.script_fingerprint
step_fingerprint_or_stop = _mod.step_fingerprint_or_stop
step_fingerprint = _mod.step_fingerprint
step_names = _mod.step_names
write_state = _mod.write_state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# A straight chain, in which beta consumes alpha and gamma consumes beta. Because positional
# order and dependency order coincide, "everything after" a step and "everything computed
# from" it are the same steps.
STEPS = [
    Step("alpha", "python3", ["alpha.py"]),
    Step("beta", "python3", ["beta.py", "-w", "5"], consumes=("alpha",)),
    Step("gamma", "python3", ["gamma.py"], consumes=("beta",)),
]

# A list in which the two orders differ: `right` runs after `left` and reads
# nothing that `left` writes.
#
#     root --+-- left --- leaf
#            +-- right
BRANCHED = [
    Step("root", "python3", ["root.py"]),
    Step("left", "python3", ["left.py"], consumes=("root",)),
    Step("right", "python3", ["right.py"], consumes=("root",)),
    Step("leaf", "python3", ["leaf.py"], consumes=("left",)),
]

# `late` modifies the output of `base` in place and never restores it. After `late` has
# run, `mid` runs correctly again only once `base` has rebuilt its output. The list models
# the file history step, which prunes data/validated/, the output of the collected file
# cleanup.
MUTATING = [
    Step("base", "python3", ["base.py"]),
    Step("mid", "python3", ["mid.py"], consumes=("base",)),
    Step("late", "python3", ["late.py"], consumes=("mid",), mutates=("base",)),
]


def _dead_pid() -> int:
    """Start a process, wait for it to exit, and return its now-free pid."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


@contextlib.contextmanager
def _quiet():
    """Discard the runner's output for the duration of a call."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        yield


@contextlib.contextmanager
def _captured_stderr():
    """Discard stdout and return the stderr buffer for inspection."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
        yield buf


@contextlib.contextmanager
def _swap(obj, attr, value):
    """Replace obj.attr inside the block and restore it afterwards."""
    orig = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, orig)


# A process that holds the run lock as a live run does, with an flock on the file until the
# process is killed. The runner's refusal logs the text that the process writes into the
# file. The flock, not the text, decides whether the lock is held.
_HOLD_SCRIPT = ("import fcntl, os, sys, time\n"
                "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "os.ftruncate(fd, 0)\n"
                "os.write(fd, b'held by the test')\n"
                "print('held', flush=True)\n"
                "time.sleep(60)\n")


def _hold_from_another_process(case, lock: Path) -> subprocess.Popen:
    """Hold the flock on `lock` from a child process until the test ends."""
    holder = subprocess.Popen([sys.executable, "-c", _HOLD_SCRIPT, str(lock)],
                              stdout=subprocess.PIPE, text=True)
    case.addCleanup(holder.wait)
    case.addCleanup(holder.kill)
    case.assertEqual(holder.stdout.readline().strip(), "held")
    return holder


def _within(case, seconds: float, call, unblock):
    """Run `call()` on a thread and return its result. When the call has not returned
    after `seconds`, call `unblock` to let the thread finish and fail the test. A hang in
    the code under test then fails the test instead of blocking the suite."""
    returned, raised = [], []

    def target():
        try:
            returned.append(call())
        except BaseException as error:
            raised.append(error)

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        unblock()
        worker.join(10)
        case.fail(f"still running after {seconds} s")
    if raised:
        raise raised[0]
    return returned[0]


def _lock_is_free(lock: Path) -> bool:
    """Whether a new descriptor can take the flock, the test for a released lock."""
    if not lock.exists():
        return True
    fd = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


class _RunLogCase(unittest.TestCase):
    """Base class that points the runner's run_logs paths at a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.log_dir = self.tmp / "run_logs"
        self.log_dir.mkdir()
        self.lock = self.log_dir / "run.lock"
        self.state = self.log_dir / "state.json"
        self._orig = (_mod.LOG_DIR, _mod.LOCK_PATH, _mod.STATE_PATH,
                      _mod.DEFAULT_SCRATCH_DIR)
        _mod.LOG_DIR = self.log_dir
        _mod.LOCK_PATH = self.lock
        _mod.STATE_PATH = self.state
        _mod.DEFAULT_SCRATCH_DIR = self.tmp / "_scratch"

    def tearDown(self):
        (_mod.LOG_DIR, _mod.LOCK_PATH, _mod.STATE_PATH,
         _mod.DEFAULT_SCRATCH_DIR) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mark_done(self, *names) -> None:
        """Record steps as complete, as a finished run does.

        Because the fixture steps name scripts that do not exist, their
        fingerprint is the empty string both when recorded and when checked,
        and the steps keep counting as done.
        """
        recorded = read_state().get("completed", {})
        recorded.update({name: "" for name in names})
        write_state({"completed": recorded})

    def _completed(self) -> dict:
        return read_state().get("completed", {})

    def _is_done(self, name: str) -> bool:
        return name in self._completed()


# ---------------------------------------------------------------------------
# Step  (the declaration that every other helper reads)
# ---------------------------------------------------------------------------

class TestStep(unittest.TestCase):

    def test_fields_are_readable_by_name(self):
        step = Step("alpha", "python3", ["alpha.py", "-w", "5"])
        self.assertEqual((step.key, step.interpreter, step.args),
                         ("alpha", "python3", ["alpha.py", "-w", "5"]))

    def test_consumes_and_mutates_default_to_nothing(self):
        step = Step("alpha", "python3", ["alpha.py"])
        self.assertEqual((step.consumes, step.mutates), ((), ()))

    def test_reads_defaults_to_nothing_and_is_relative(self):
        self.assertEqual(Step("alpha", "python3", ["alpha.py"]).reads, ())
        for step in _mod.steps_for(True):
            for path in step.reads:
                with self.subTest(step=step.key, path=path):
                    self.assertFalse(Path(path).is_absolute())

    def test_detect_declares_both_caches_it_joins(self):
        """The detection step opens two tracked files that no step of a plain run writes.
        Undeclared, they would be the only input that the pre-checks never verify."""
        detect = next(s for s in _mod.CORE_STEPS if s.key == 'detection')
        self.assertEqual(set(detect.reads), {"file_history.csv", "ai_authorship.csv"})

    def test_the_file_history_step_declares_the_collected_list(self):
        """The step keeps the rows of its caches at the heads that the collected list names. The
        pre-checks verify that the list is on disk, because a regeneration over an earlier
        collection does not run the cloning step, which writes the list."""
        history = next(s for s in _mod.CORE_STEPS if s.key == 'file_history')
        self.assertEqual(history.reads, ("data/collected/repos.csv",))
        cloning = next(s for s in _mod.CORE_STEPS if s.key == 'repository_cloning')
        self.assertIn("data/collected/repos.csv", cloning.produces)

    def test_the_ai_authorship_cache_has_exactly_one_producer(self):
        """The cloning step fills `ai_authorship.csv`, beside `file_history.csv`, from the
        commit history of each copied path. No other step writes the cache."""
        producers = [s.key for s in _mod.steps_for(True)
                     if "ai_authorship.csv" in s.produces]
        self.assertEqual(producers, ['repository_cloning'])

    def test_reading_the_commit_history_is_outside_the_collect_manifest(self):
        """The cloning step's record does not show whether the commit history was read. The
        detection step rewrites the commit CSV in place, and a reconstruction can record the
        step from a tree that has the sample but not the CSV. docs/commits.md records the
        decision."""
        collect = next(s for s in _mod.CORE_STEPS if s.key == 'repository_cloning')
        for path in ("output/all_ai_commits.csv", "output/all_ai_commits-unmatched.csv",
                     "output/ai_commits-repos.csv", "collect_commits"):
            self.assertNotIn(path, collect.produces)
        document = (REPO_ROOT / "docs" / "commits.md").read_text(encoding="utf-8")
        self.assertIn("not in the cloning step's `produces`", document)

    def test_the_two_checks_read_what_the_cloning_step_writes(self):
        """The validation check consumes both steps, because the cloning step writes the
        commit CSV and the unmatched attributions and the detection step restricts the commit
        CSV in place."""
        collector = importlib.import_module("agentcontext.steps.repository_cloning")
        by_key = {s.key: s for s in _mod.COMMIT_RECLASSIFICATION_STEPS}
        self.assertEqual(list(by_key), ['commit_attribution_validation', 'unmatched_commits_report'])
        CSV = Path(collector.AI_COMMITS_CSV_NAME)
        validate = _mod._step_argv(_mod.COMMIT_RECLASSIFICATION_STEPS, 'commit_attribution_validation')
        self.assertEqual(_mod._flag_value(validate, "-i", None), str(CSV))
        self.assertEqual(by_key['commit_attribution_validation'].consumes, ('repository_cloning', 'detection'))
        check = _mod._step_argv(_mod.COMMIT_RECLASSIFICATION_STEPS, 'unmatched_commits_report')
        self.assertEqual(_mod._flag_value(check, "--unmatched-csv", None),
                         str(CSV.with_name(f"{CSV.stem}-unmatched{CSV.suffix}")))
        self.assertEqual(by_key['unmatched_commits_report'].consumes, ('repository_cloning',))

    def test_the_commit_heuristics_step_runs_between_collect_and_detect(self):
        """The detection step restricts the commit CSV that the commit_reclassification step
        writes. The reclassification runs before detection, and a changed commit heuristic
        runs the detection step again."""
        keys = [s.key for s in _mod.steps_for(True)]
        self.assertEqual(keys[keys.index('repository_cloning') + 1], 'commit_reclassification')
        self.assertLess(keys.index('commit_reclassification'), keys.index('detection'))
        self.assertNotIn('commit_reclassification', [s.key for s in _mod.steps_for(False)])
        steps = {s.key: s for s in _mod.steps_for(True)}
        self.assertIn('commit_reclassification', steps['detection'].consumes)
        self.assertIn('commit_reclassification', steps['unmatched_commits_report'].consumes)
        self.assertIn('detection', _mod.invalidated_by(_mod.steps_for(True), 'commit_reclassification'))
        self.assertNotIn('commit_reclassification', next(s for s in _mod.steps_for(False)
                                            if s.key == 'detection').consumes)
        self.assertEqual(_mod.COMMIT_HEURISTICS_STEP.heuristics,
                         ("ai_commit_patterns.csv", "other_ai_commit_patterns.csv"))

    def test_no_ai_commit_step_clones(self):
        self.assertEqual(_mod.CLONING_STEPS, {'repository_cloning', 'file_history'})
        self.assertEqual(_mod.BINARY_REQUIREMENTS["git"], _mod.CLONING_STEPS)

    def test_every_declared_edge_names_a_real_step(self):
        # A typo in consumes would drop an edge without an error. invalidated_by
        # would never reach the step, and --from would clear too few steps.
        steps = _mod.steps_for(True)
        known = {s.key for s in steps}
        for step in steps:
            for edge in step.consumes + step.mutates:
                with self.subTest(step=step.key, edge=edge):
                    self.assertIn(edge, known)

    def test_the_dependency_graph_has_no_cycles(self):
        # A cycle would make invalidated_by reach a step from itself, and
        # --from would then clear the record of a step that is meant to be
        # upstream of the named step.
        steps = _mod.steps_for(True)
        by_key = {s.key: s for s in steps}
        for step in steps:
            reached, frontier = set(), set(step.consumes)
            while frontier:
                reached |= frontier
                frontier = {c for k in frontier for c in by_key[k].consumes
                            if c not in reached}
            with self.subTest(step=step.key):
                self.assertNotIn(step.key, reached)

    def test_git_data_is_declared_as_modifying_validates_output(self):
        # The only `mutates` edge of the real list, and the reason that
        # modified_input_problems exists: agentcontext/steps/file_history.py deletes from
        # data/validated/, the output of agentcontext/steps/collected_file_cleanup.py.
        by_key = {s.key: s for s in _mod.CORE_STEPS}
        self.assertIn('collected_file_cleanup', by_key['file_history'].mutates)

    def test_every_step_that_opens_the_validated_tree_declares_validate(self):
        # The check for modified inputs finds only the pending steps that consume the
        # modified step directly. A step that reads data/validated/ without declaring the
        # edge would escape the check.
        by_key = {s.key: s for s in _mod.CORE_STEPS}
        for key in ('detection', 'file_history', 'collected_files_archive'):
            with self.subTest(step=key):
                self.assertIn('collected_file_cleanup', by_key[key].consumes)

    def test_both_csv_writing_steps_declare_every_output_csv(self):
        # The detection step writes the mechanism CSVs, and the file history step fills
        # their Git columns in place. Each step declares every CSV, because a step that
        # exited 0 after writing only some of them would otherwise be recorded as done.
        by_key = {s.key: s for s in _mod.CORE_STEPS}
        wanted = ["output/repos.csv"] + [f"output/{name}" for name
                                         in heuristics.csv_filenames()]
        for key in ('detection', 'file_history'):
            for rel in wanted:
                with self.subTest(step=key, output=rel):
                    self.assertIn(rel, by_key[key].produces)

    def test_every_step_that_needs_lingua_runs_under_the_probed_interpreter(self):
        # pre_checks() checks lingua in VENV_PYTHON and in no other interpreter. For a step
        # that needs lingua and runs under a bare "python3", the pre-checks would test for
        # the package in an interpreter that the step never uses. The run would then
        # record `n/a` for every language that it measures, without a warning.
        by_key = {s.key: s for s in _mod.steps_for(True)}
        for key in sorted(_mod.LINGUA_STEPS):
            with self.subTest(step=key):
                self.assertEqual(by_key[key].interpreter, _mod.VENV_PYTHON)

    def test_produces_defaults_to_nothing(self):
        # A step that only prints declares no output, and the runner must not fail it
        # for writing none.
        self.assertEqual(Step("alpha", "python3", ["alpha.py"]).produces, ())

    def test_every_declared_output_is_a_relative_path(self):
        # run_step resolves the declared outputs against the step's data directory. An
        # absolute path would bypass that resolution and check a file outside the run's
        # own tree.
        for step in _mod.steps_for(True):
            for rel in step.produces:
                with self.subTest(step=step.key, path=rel):
                    self.assertFalse(Path(rel).is_absolute())

    def test_each_ai_commits_output_is_what_that_script_derives(self):
        # The validation script derives its output name from its input and takes no flag
        # for it. The runner's `produces` is the only place that writes the name.
        # Because the step has not run here, the declared file is absent from
        # output/, and a typo in the name looks the same as a step that never ran. The
        # unmatched check names its file with `--out`.
        for step in _mod.COMMIT_RECLASSIFICATION_STEPS:
            if not step.produces:
                continue
            argv = _mod._step_argv(_mod.COMMIT_RECLASSIFICATION_STEPS, step.key)
            module = importlib.import_module(_mod.step_module(step))
            pinned_in = _mod._flag_value(argv, "-i", None)
            pinned_out = (_mod._flag_value(argv, "-o", None)
                          or _mod._flag_value(argv, "--out", None))
            expected = pinned_out or module.default_output_path(pinned_in)
            with self.subTest(step=step.key):
                self.assertIn(expected, step.produces)

    def test_the_three_default_repo_csv_definitions_agree(self):
        # The runner reads the path in the pre-checks, the collector uses it as its argparse
        # default, and the engineered_project_selection step writes the file. Each keeps
        # the path in a constant of its own. The test guards against a run in which the
        # pre-checks verify one path while the collector opens another, or the
        # engineered_project_selection step writes a third. The step's default is absolute
        # and the other two are relative to the stage directory, and the test compares the
        # resolved paths rather than the text.
        collector = importlib.import_module("agentcontext.steps.repository_cloning")
        frame = importlib.import_module("agentcontext.steps.engineered_project_selection")
        self.assertEqual(_mod.DEFAULT_REPO_CSV, collector.DEFAULT_REPO_CSV)
        self.assertEqual((_mod.STAGE_DIR / _mod.DEFAULT_REPO_CSV).resolve(),
                         frame.DEFAULT_OUTPUT.resolve())

    def test_the_default_repo_csv_exists_relative_to_the_stage_directory(self):
        # The pre-checks check and run_step both resolve DEFAULT_REPO_CSV against STAGE_DIR.
        # A bare file name would name a file in the stage directory, where the
        # classification stage never writes one.
        self.assertTrue((_mod.STAGE_DIR / _mod.DEFAULT_REPO_CSV).parent.is_dir())

    def test_a_step_is_frozen(self):
        # apply_step_overrides builds new steps instead of editing the steps of
        # CORE_STEPS, which is module-level state that every run shares. A frozen step
        # keeps an override from reaching the next call.
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Step("alpha", "python3", ["alpha.py"]).key = "beta"

    def test_steps_compare_by_value(self):
        # A test below asserts that apply_step_overrides with no overrides returns a list
        # equal to its input. That assertion means something only because steps compare
        # by value, not by identity.
        self.assertEqual(Step("alpha", "python3", ["a.py"]),
                         Step("alpha", "python3", ["a.py"]))


class TestTheEngineeredProjectSelectionStep(unittest.TestCase):
    """The step that writes the sample list, and the edge that it lacks on purpose.

    `engineered_project_selection` writes the list that `repository_cloning` reads, which
    suggests the declaration `consumes=("engineered_project_selection",)`. That edge would put
    `repository_cloning` in `invalidated_by("engineered_project_selection")`. The next edit to
    `agentcontext/steps/engineered_project_selection.py` would then drop `repository_cloning`
    from the recorded steps, and the following run would clone every repository of the list
    again, although the tree already holds what the step wrote. The edge would bring back the
    failure that recording the cloning step by its output (`COMPLETION_OUTPUT`) prevents.
    """

    def _step(self, key):
        return next(s for s in _mod.CORE_STEPS if s.key == key)

    def test_it_declares_the_list_the_collector_reads(self):
        # After the step, the runner checks whether the declared output changed. With this
        # declaration, an engineered_project_selection step that exits 0 without writing
        # anything fails. `test_the_default_repo_csv_exists_relative_to_the_stage_directory`
        # asserts only that the list's directory exists, which holds whether or not a step
        # ever wrote the list.
        self.assertEqual(self._step('engineered_project_selection').produces, (_mod.DEFAULT_REPO_CSV,))

    def test_the_cloning_step_is_recorded_by_the_repositories_of_the_list(self):
        """The list also carries the other columns of the labels. If the record covered the
        whole file, a rewrite of these columns with the same repositories would run the
        cloning step again, which continues the collection that ended, and every step after
        it."""
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "list.csv"

            def digest(rows) -> str:
                sample.write_text("repo_name,justification\n"
                                  + "".join(f"{name},{why}\n" for name, why in rows))
                return _mod._repositories_digest(sample, "repo_name")

            first = digest([("octo/alpha", "a library"), ("octo/beta", "a tool")])
            self.assertEqual(digest([("octo/beta", "a command line tool"),
                                     ("octo/alpha", "a library")]), first)
            self.assertNotEqual(digest([("octo/alpha", "a library")]), first)

    def test_it_runs_before_the_step_that_reads_the_list(self):
        keys = [s.key for s in _mod.CORE_STEPS]
        self.assertLess(keys.index('engineered_project_selection'), keys.index('repository_cloning'))

    def test_editing_it_does_not_invalidate_collect(self):
        self.assertNotIn('repository_cloning', invalidated_by(_mod.CORE_STEPS, 'engineered_project_selection'))

    def test_collect_consumes_nothing(self):
        # The second half of the property that the previous test checks. `invalidated_by`
        # reaches `repository_cloning` only through an edge. An assertion on reachability
        # alone would also pass for a list in which `engineered_project_selection` had been
        # renamed.
        self.assertEqual(self._step('repository_cloning').consumes, ())

    def test_it_is_recorded_by_its_script(self):
        # The step's output can be derived again from an input that is still on disk,
        # which is the criterion of COMPLETION_SCRIPT. With the declared output, the
        # runner's check after the step applies, and the step counts as done only when the
        # list changed.
        self.assertEqual(self._step('engineered_project_selection').completion, COMPLETION_SCRIPT)


class TestScratchDir(unittest.TestCase):
    """Every cloning step gets the same clone directory through its own --clone-dir."""

    def test_every_cloning_step_and_no_other_gets_the_flag(self):
        steps = _mod.apply_scratch_dir(_mod.steps_for(True),
                                        "/volume/scratch")
        for step in steps:
            with self.subTest(step=step.key):
                has_flag = "--clone-dir" in step.args
                self.assertEqual(has_flag, step.key in _mod.CLONING_STEPS)
                if has_flag:
                    self.assertEqual(step.args[-2:], ["--clone-dir", "/volume/scratch"])

    def test_the_default_is_under_the_stage_directory_and_ignored(self):
        self.assertEqual(_mod.DEFAULT_SCRATCH_DIR.parent, _mod.STAGE_DIR)
        ignored = (_mod.STAGE_DIR / ".gitignore").read_text().splitlines()
        self.assertIn(f"{_mod.DEFAULT_SCRATCH_DIR.name}/", ignored)

    def test_every_cloning_script_accepts_the_flag(self):
        for step in _mod.steps_for(True):
            if step.key not in _mod.CLONING_STEPS:
                continue
            with self.subTest(step=step.key):
                module = _mod.step_module(step)
                files = _mod.module_closure(module) if module else [_mod.STAGE_DIR / step.args[0]]
                source = "".join(path.read_text() for path in files)
                self.assertIn('"--clone-dir"', source)


class TestStepDeclaration(unittest.TestCase):
    """The declarations that a Step rejects. The completion policy decides what a
    resume compares. A declaration that the runner could never compare is therefore
    rejected where it is written, not in the run that relies on it."""

    def test_a_step_is_recorded_by_its_script_unless_it_declares_otherwise(self):
        self.assertEqual(Step("a", "python3", ["a.py"]).completion,
                         COMPLETION_SCRIPT)

    def test_output_completion_needs_a_declared_output(self):
        # Without a declared output, the fingerprint would be "", which is also the
        # fingerprint of a step with a missing script. The step would then count as done
        # for as long as its record survived.
        with self.assertRaises(ValueError) as caught:
            Step("a", "python3", ["a.py"], completion=COMPLETION_OUTPUT)
        self.assertIn("produces", str(caught.exception))

    def test_output_completion_with_an_output_is_accepted(self):
        step = Step("a", "python3", ["a.py"], produces=("out",),
                      completion=COMPLETION_OUTPUT)
        self.assertEqual(step.completion, COMPLETION_OUTPUT)

    def test_an_unknown_completion_rule_is_rejected(self):
        with self.assertRaises(ValueError):
            Step("a", "python3", ["a.py"], completion="whenever")

    def test_the_check_runs_again_on_dataclasses_replace(self):
        # apply_step_overrides and apply_repo_csv both build their steps with replace().
        # A replace() that removes the outputs has to be rejected, as the declaration
        # would be. An assertion that a valid step survives replace() would also pass
        # with __post_init__ deleted.
        step = Step("a", "python3", ["a.py"], produces=("out",),
                      completion=COMPLETION_OUTPUT)
        self.assertEqual(dataclasses.replace(step, args=["a.py", "-w", "2"]).completion,
                         COMPLETION_OUTPUT)
        with self.assertRaises(ValueError):
            dataclasses.replace(step, produces=())


# ---------------------------------------------------------------------------
# The step list against the filesystem
# ---------------------------------------------------------------------------

class TestTheStepTableNamesThingsThatExist(unittest.TestCase):
    """Every step key written outside the step list, and every script in it.

    The step keys are strings in four places: the step list,
    `BINARY_REQUIREMENTS`, `LINGUA_STEPS`, and whatever the user types after
    `--from`. Only the last is checked at runtime. A key misspelled in
    `BINARY_REQUIREMENTS` or `LINGUA_STEPS` is a requirement that applies to no
    step. `7zz` listed under a key that no step has would let a run without 7zz
    reach the collected_files_archive step and fail there, after every step before
    it has run. The pre-checks exist to prevent that failure.
    """

    def _steps(self) -> list:
        return _mod.steps_for(True)

    def test_step_keys_are_unique(self):
        keys = [s.key for s in self._steps()]
        self.assertEqual(sorted(keys), sorted(set(keys)))

    def test_publication_requires_current_collection_provenance(self):
        steps = {s.key: s for s in self._steps()}
        self.assertIn("collection_summary", steps['release'].consumes)
        snapshot = steps["collection_summary"]
        self.assertEqual(snapshot.args, ["-m", "agentcontext.checks.snapshot", "write"])
        self.assertEqual(snapshot.completion, COMPLETION_INPUTS)
        self.assertTrue({'repository_cloning', 'detection', 'file_history'}.issubset(snapshot.consumes))
        self.assertIn("data/collected/repos.csv", snapshot.inputs)
        self.assertIn("output/repos.csv", snapshot.inputs)

    def test_new_imports_write_the_snapshot_again(self):
        """`snapshot write` counts output/imports.csv in the snapshot's `imports` section."""
        steps = self._steps()
        self.assertIn("collection_summary", _mod.invalidated_by(steps, "import_detection"))
        self.assertIn("output/imports.csv", {s.key: s for s in steps}["collection_summary"].inputs)

    def test_every_binary_requirement_names_a_real_step(self):
        known = {s.key for s in self._steps()}
        for binary, needed_by in sorted(_mod.BINARY_REQUIREMENTS.items()):
            for key in sorted(needed_by):
                with self.subTest(binary=binary, step=key):
                    self.assertIn(key, known)

    def test_every_lingua_step_names_a_real_step(self):
        known = {s.key for s in self._steps()}
        for key in sorted(_mod.LINGUA_STEPS):
            with self.subTest(step=key):
                self.assertIn(key, known)

    def test_every_step_runs_a_script_or_module_that_is_there(self):
        # A step that names a renamed script or a module that the package does not contain
        # fails only when it runs, after the steps before it are done and recorded.
        # run_step resolves a script against STAGE_DIR, and the interpreter finds a module
        # by name. The test requires the script on disk, or a module with a non-empty
        # closure.
        for step in self._steps():
            with self.subTest(step=step.key):
                module = _mod.step_module(step)
                if module:
                    self.assertTrue(_mod.module_closure(module),
                                    f"{step.key} runs {module}, which the package does not "
                                    f"contain")
                else:
                    self.assertTrue((_mod.STAGE_DIR / step.args[0]).is_file(),
                                    f"{step.key} runs {step.args[0]}, which is not there")


# ---------------------------------------------------------------------------
# Recorded inputs  (the heuristics, and what decides the cloning step)
# ---------------------------------------------------------------------------

class TestTheHeuristicsAreARecordedInput(unittest.TestCase):
    """A step that reads the heuristics is recorded by its inputs, and an image that
    changes only a heuristic runs the step again. A step recorded by its script alone would
    be skipped after such an image, and the output CSVs would disagree with the heuristics
    shipped beside them."""

    READERS = {'collected_file_cleanup': ("config_path_patterns.csv", "vendored_dirs.txt",
                                          "planning_document_patterns.csv",
                                          "local_state_paths.csv"),
               'detection': ("config_path_patterns.csv", "vendored_dirs.txt",
                             "planning_document_patterns.csv", "local_state_paths.csv",
                             "import_patterns.csv"),
               "output_cross_check": ("config_path_patterns.csv",),
               'unmatched_paths_report': ("config_path_patterns.csv", "not_configuration.csv"),
               "deleted_files_report": ("config_path_patterns.csv", "not_configuration.csv"),
               'import_detection': ("import_patterns.csv", "config_path_patterns.csv"),
               'release': ("config_path_patterns.csv", "ai_commit_patterns.csv",
                           "other_ai_commit_patterns.csv", "import_patterns.csv",
                           "not_imports.csv", "planning_document_patterns.csv",
                           "not_configuration.csv", "not_ai_commits.csv",
                           "not_planning_documents.csv", "doc_hashes.json", "doc_quotes.json"),
               'commit_reclassification': ("ai_commit_patterns.csv", "other_ai_commit_patterns.csv"),
               'commit_attribution_validation': ("ai_commit_patterns.csv",
                                                 "other_ai_commit_patterns.csv"),
               'unmatched_commits_report': ("ai_commit_patterns.csv", "not_ai_commits.csv")}

    def test_every_reader_declares_the_file_it_reads(self):
        by_key = {s.key: s for s in _mod.steps_for(True)}
        for key, names in self.READERS.items():
            with self.subTest(step=key):
                self.assertEqual(by_key[key].completion, COMPLETION_INPUTS)
                self.assertEqual(set(by_key[key].heuristics), set(names))
                for name in names:
                    self.assertTrue((_mod.HEURISTICS_DIR / name).is_file(), name)

    def test_a_step_that_writes_what_the_release_publishes_runs_the_release_again(self):
        """The release copies `unmatched-paths.csv`, `deletions.csv`, `ai_commits-repos.csv` and
        `commit-reclassification.json` and packs `deletion.csv`. A step that wrote one of them
        anew and left the release out would leave the previous copy in `release/`."""
        steps = _mod.steps_for(True)
        for key in ("unmatched_paths_report", "deleted_files_report", "repository_cloning",
                    "commit_reclassification", "collected_file_cleanup"):
            with self.subTest(step=key):
                self.assertIn("release", _mod.invalidated_by(steps, key))

    def test_either_commit_heuristics_file_invalidates_reclassification(self):
        step = next(s for s in _mod.steps_for(True) if s.key == 'commit_reclassification')
        for name in ("ai_commit_patterns.csv", "other_ai_commit_patterns.csv"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp)
                for source in ("ai_commit_patterns.csv", "other_ai_commit_patterns.csv"):
                    shutil.copyfile(_mod.HEURISTICS_DIR / source, directory / source)
                with mock.patch.object(_mod, "HEURISTICS_DIR", directory):
                    before = _mod.step_fingerprint(step)
                    with (directory / name).open("a") as handle:
                        handle.write("\n# changed heuristic\n")
                    self.assertNotEqual(_mod.step_fingerprint(step), before)

    def test_the_code_of_the_two_modules_and_not_a_comment_reruns_the_reclassification(self):
        """`commits.CLASSIFIED_BY` hashes the columns of the two commit heuristics files that
        the classification reads and the code fingerprint of `commits.py` and `heuristics.py`.
        The release refuses a snapshot that records a digest other than its own. A comment in
        any module that the step imports changes neither the digest nor the step's record.
        Code in either of the two modules changes both, so that a step that stays recorded
        never makes the release refuse.
        Each case imports a copy of the package and the heuristics in a process of its own,
        because `commits` computes the digest when it is imported."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        shutil.copytree(REPO_ROOT / "agentcontext", tmp / "agentcontext",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(_mod.HEURISTICS_DIR, tmp / "heuristics")
        script = ("from agentcontext import commits, runner\n"
                  "print(runner.__file__)\n"
                  "print(runner.step_fingerprint(runner.COMMIT_HEURISTICS_STEP))\n"
                  "print(commits.CLASSIFIED_BY)\n")
        environment = {**os.environ, "PYTHONPATH": str(tmp),
                       "AGENTCONTEXT_DATA_ROOT": str(tmp / "data")}

        def recorded() -> tuple:
            lines = subprocess.run([sys.executable, "-c", script], cwd=tmp, env=environment,
                                   capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual(Path(lines[0]).resolve(), (tmp / "agentcontext" / "runner.py").resolve())
            return lines[1], lines[2]

        record, digest = recorded()
        self.assertTrue(record.startswith(f"{COMPLETION_INPUTS}:"), record)
        for module, added, changes_the_digest in (
                ("logs.py", "\n# A comment added.\n", False),
                ("heuristics.py", "\n# A comment added.\n", False),
                ("commits.py", "\n# A comment added.\n", False),
                ("heuristics.py", "\nADDED_CONSTANT = 1\n", True),
                ("commits.py", "\nADDED_CONSTANT = 1\n", True)):
            with self.subTest(module=module, added=added):
                with (tmp / "agentcontext" / module).open("a", encoding="utf-8") as handle:
                    handle.write(added)
                now_record, now_digest = recorded()
                self.assertEqual(now_digest != digest, changes_the_digest)
                self.assertEqual(now_record != record, changes_the_digest)
                record, digest = now_record, now_digest

    def test_the_heuristics_is_fingerprinted_under_its_directory_name(self):
        # The digest includes the label rather than the absolute path, which keeps the
        # fingerprint the same in a checkout and in the container, where the heuristics are
        # under the image's root and not under the volume.
        step = Step('detection', "python3", ["-m", "agentcontext.steps.detection"],
                      completion=COMPLETION_INPUTS, heuristics=("config_path_patterns.csv",))
        self.assertEqual(_mod.input_labels(step), ["heuristics/config_path_patterns.csv"])
        (label, path), = _mod.input_entries(step)
        self.assertEqual(path, _mod.HEURISTICS_DIR / "config_path_patterns.csv")

    def test_a_changed_heuristics_file_changes_the_fingerprint(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "detect.py").write_text("print()")
        (tmp / "config_path_patterns.csv").write_text("tool,mechanism\n")
        step = Step('detection', "python3", ["detect.py"], root=str(tmp),
                      completion=COMPLETION_INPUTS, heuristics=("config_path_patterns.csv",))
        with mock.patch.object(_mod, "HEURISTICS_DIR", tmp):
            before = _mod.inputs_fingerprint(step)
            (tmp / "config_path_patterns.csv").write_text("tool,mechanism\nclaude,rules\n")
            self.assertNotEqual(_mod.inputs_fingerprint(step), before)
            (tmp / "config_path_patterns.csv").unlink()
            self.assertEqual(_mod.inputs_fingerprint(step), "")

    def test_an_input_that_cannot_be_read_is_a_hard_stop_not_a_verdict(self):
        """`input_entries` checks `is_file()`, which accepts a file that can be stat'ed but
        not opened. A bare OSError would escape, and main would log a failure to write the
        state file that names the heuristics file as the file that it could not write."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "detect.py").write_text("print()")
        blocked = tmp / "config_path_patterns.csv"
        blocked.write_text("tool,mechanism\n")
        os.chmod(blocked, 0o000)
        step = Step('detection', "python3", ["detect.py"], root=str(tmp),
                      completion=COMPLETION_INPUTS, heuristics=("config_path_patterns.csv",))
        try:
            try:
                blocked.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            with mock.patch.object(_mod, "HEURISTICS_DIR", tmp):
                with self.assertRaises(UnreadableOutput) as caught:
                    _mod.inputs_fingerprint(step)
            self.assertEqual(caught.exception.rel, "heuristics/config_path_patterns.csv")
        finally:
            os.chmod(blocked, 0o644)

    def test_collect_stays_recorded_by_its_output(self):
        # The cloning step reads the heuristics too. It is recorded by COMPLETION_OUTPUT,
        # because a snapshot of GitHub cannot be derived again from a heuristic. `inputs_of`
        # adds the path heuristics to that record
        # (TestTheCloningStepIsRecordedByWhatDecidesIt).
        collect = next(s for s in _mod.CORE_STEPS if s.key == 'repository_cloning')
        self.assertEqual(collect.completion, COMPLETION_OUTPUT)

    def test_a_changed_input_changes_the_fingerprint(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "detect.py").write_text("print()")
        (tmp / "config_path_patterns.csv").write_text("tool,mechanism\n")
        step = Step('detection', "python3", ["detect.py"], root=str(tmp),
                      completion=COMPLETION_INPUTS, inputs=("config_path_patterns.csv",))
        before = _mod.inputs_fingerprint(step)
        (tmp / "config_path_patterns.csv").write_text("tool,mechanism\nclaude,rules\n")
        self.assertNotEqual(_mod.inputs_fingerprint(step), before)


class TestTheCloningStepIsRecordedByWhatDecidesIt(_RunLogCase):
    """`repository_cloning` is recorded by its tree and by what decides whether it takes a
    repository over: the collector's code, the version of git, the path heuristics, the import
    heuristics and its list. A record of the tree alone would let a Start skip the step after a
    change to the path heuristics or to the collector. The release would then publish CSVs of
    the new heuristics over files that the old heuristics chose. A Start after a new
    classification would also skip the step and keep the collection of the sample that the
    classification replaced."""

    def setUp(self):
        super().setUp()
        self._saved_root = (_mod.STAGE_DIR, _mod.HEURISTICS_DIR)
        self.stage = self.tmp / "collection"
        self.heuristics = self.tmp / "heuristics"
        self.heuristics.mkdir()
        for name in ("config_path_patterns.csv", "vendored_dirs.txt", "import_patterns.csv",
                     "planning_document_patterns.csv", "local_state_paths.csv"):
            shutil.copyfile(_mod.HEURISTICS_DIR / name, self.heuristics / name)
        _mod.STAGE_DIR, _mod.HEURISTICS_DIR = self.stage, self.heuristics
        collected = self.stage / "data" / "collected"
        (collected / "o\u00a7a").mkdir(parents=True)
        (collected / "o\u00a7a" / "CLAUDE.md").write_text("# a\n")
        (self.stage / "data" / "imported").mkdir(parents=True)
        (collected / "repos.csv").write_text("repo_name\no/a\n")
        for name in ("file_history.csv", "submodules.csv", "ai_authorship.csv"):
            (self.stage / name).write_text("repo_name\no/a\n")
        self.sample = self.stage.parent / "classification" / "repos_annotated_included.csv"
        self.sample.parent.mkdir(parents=True)
        self.sample.write_text("repo_name\no/a\n")
        (self.stage / "finished_repositories.jsonl").write_text(
            json.dumps({"repo": "o/a", "row": "o/a,x", "fail_row": None}) + "\n")
        self.step = next(s for s in _RUNNER_DEFAULTS["CORE_STEPS"] if s.key == "repository_cloning")

    def tearDown(self):
        _mod.STAGE_DIR, _mod.HEURISTICS_DIR = self._saved_root
        super().tearDown()

    def _edit_heuristics(self, column: str, value: str,
                         name: str = "config_path_patterns.csv") -> None:
        """Set `column` of the first row of the copied heuristics file `name` to `value`."""
        path = self.heuristics / name
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        rows[0][column] = value
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_a_changed_planning_document_heuristic_changes_the_record(self):
        """The heuristics of the planning documents decide what the step copies, as the path
        heuristics do. A new `doc_verified` date, written when a page is read again, changes
        nothing."""
        before = _mod.step_fingerprint(self.step)
        self._edit_heuristics("doc_verified", "2026-12-31", "planning_document_patterns.csv")
        self.assertEqual(_mod.step_fingerprint(self.step), before)
        self._edit_heuristics("pattern", r"plans/[^/]+\.md", "planning_document_patterns.csv")
        after = _mod.step_fingerprint(self.step)
        self.assertNotEqual(after, before)
        self._edit_heuristics("anchor", "header:^# Plan", "planning_document_patterns.csv")
        self.assertNotEqual(_mod.step_fingerprint(self.step), after,
                            "an anchor decides what the step copies, as the pattern does")
        self.assertNotEqual(after, before)
        self.assertEqual(after.split("+")[0], before.split("+")[0])

    def test_a_changed_place_of_local_state_changes_the_record(self):
        """The places of the tools' local state decide what the step leaves out. A new
        `doc_verified` date, written when a page is read again, changes nothing."""
        before = _mod.step_fingerprint(self.step)
        self._edit_heuristics("doc_verified", "2026-12-31", "local_state_paths.csv")
        self.assertEqual(_mod.step_fingerprint(self.step), before)
        self._edit_heuristics("path", ".claude/elsewhere/", "local_state_paths.csv")
        after = _mod.step_fingerprint(self.step)
        self.assertNotEqual(after, before)
        self.assertEqual(after.split("+")[0], before.split("+")[0])

    def test_a_changed_path_heuristic_changes_the_record_and_not_the_tree_part(self):
        before = _mod.step_fingerprint(self.step)
        self._edit_heuristics("path", "ELSEWHERE.md")
        after = _mod.step_fingerprint(self.step)
        self.assertNotEqual(after, before)
        self.assertEqual(after.split("+")[0], before.split("+")[0])

    def test_a_page_read_again_by_the_documentation_watch_changes_nothing(self):
        before = _mod.step_fingerprint(self.step)
        self._edit_heuristics("doc_verified", "2026-12-31")
        self._edit_heuristics("doc_verified", "2026-12-31", "import_patterns.csv")
        self.assertEqual(_mod.step_fingerprint(self.step), before)

    def test_a_changed_import_heuristic_changes_the_record_and_not_the_tree_part(self):
        """The import heuristics decide which files the cloning step copies to the imported
        tree. An image that changes only one import heuristic runs the step, and the step
        takes over the repositories that the change does not reach
        (`reuse.imports_reach`)."""
        before = _mod.step_fingerprint(self.step)
        self._edit_heuristics("max_depth", "9", "import_patterns.csv")
        after = _mod.step_fingerprint(self.step)
        self.assertNotEqual(after, before)
        self.assertEqual(after.split("+")[0], before.split("+")[0])

    def test_a_new_list_of_repositories_changes_the_record(self):
        before = _mod.step_fingerprint(self.step)
        self.sample.write_text("repo_name\no/a\no/b\n")
        self.assertNotEqual(_mod.step_fingerprint(self.step), before)

    def test_another_git_changes_the_record_and_not_the_tree_part(self):
        """The columns that come from the commit history can change with the version of git,
        and a new pin of git in `mise.toml` gives the image another git. The record of the
        step therefore names the version of git, as every line of a collection does, and the
        runner keeps the step after such a change only with a WARNING (`kept_on`)."""
        before = _mod.step_fingerprint(self.step)
        with self._another_git():
            after = _mod.step_fingerprint(self.step)
        self.assertNotEqual(after, before)
        self.assertEqual(after.split("+")[0], before.split("+")[0])

    def test_a_git_that_fails_to_start_once_gives_the_same_record(self):
        before = _mod.step_fingerprint(self.step)
        with git_version_fails(times=1) as failures:
            self.assertEqual(_mod.step_fingerprint(self.step), before)
        self.assertEqual(len(failures), 1)

    def test_a_git_that_prints_no_version_stops_the_run_rather_than_run_the_collection(self):
        """A process fork can fail on the NAS. If the runner read the missing version as an
        absent input, the step would count as not done, and the run would start the collection
        and mark every step after it to run again. The runner stops instead and leaves the
        state unchanged."""
        recorded = {"completed": {"repository_cloning": _mod.step_fingerprint(self.step)}}
        write_state(recorded)
        with git_version_fails(), self.assertRaises(SystemExit) as caught:
            _mod.completed_steps([self.step], read_state())
        self.assertIn("git --version printed no version", str(caught.exception))
        self.assertIn("decides whether repository_cloning is complete", str(caught.exception))
        self.assertEqual(read_state(), recorded)
        with git_version_fails(), self.assertRaises(SystemExit):
            _mod.changed_during_run(self.step, recorded["completed"]["repository_cloning"])

    def test_a_changed_collector_changes_the_record_and_a_comment_does_not(self):
        collector = self.tmp / "collector.py"
        collector.write_text("def collect():\n    return 1\n")
        with mock.patch("agentcontext.reuse.collector_files", return_value=[collector]):
            before = _mod.step_fingerprint(self.step)
            collector.write_text("def collect():\n    # a comment\n    return 1\n")
            self.assertEqual(_mod.step_fingerprint(self.step), before)
            collector.write_text("def collect():\n    return 2\n")
            self.assertNotEqual(_mod.step_fingerprint(self.step), before)

    def test_the_record_reads_the_list_that_repo_csv_names(self):
        other = self.stage / "other.csv"
        other.write_text("repo_name\no/b\n")
        overridden = _mod.apply_repo_csv([self.step], "other.csv")[0]
        before = _mod.step_fingerprint(overridden)
        self.sample.write_text("repo_name\no/c\n")
        self.assertEqual(_mod.step_fingerprint(overridden), before)
        other.write_text("repo_name\no/d\n")
        self.assertNotEqual(_mod.step_fingerprint(overridden), before)

    def test_a_changed_input_runs_the_step_and_does_not_offer_mark_complete(self):
        write_state({"completed": {"repository_cloning": _mod.step_fingerprint(self.step)}})
        self.assertIn("repository_cloning", _mod.completed_steps([self.step], read_state()))
        self._edit_heuristics("path", "ELSEWHERE.md")
        with _captured_stderr() as err:
            done = _mod.completed_steps([self.step], read_state())
        self.assertNotIn("repository_cloning", done)
        self.assertIn("An input of repository_cloning (the collector's code, the version of "
                      "git, the path heuristics, the import heuristics and the list of "
                      "repositories to collect) has changed", err.getvalue())
        self.assertNotIn("--mark-complete", err.getvalue())

    def _record_with_its_inputs(self) -> str:
        """Record the step as the runner records it after an exit 0, with the digest of each of
        its inputs, and return the record."""
        state = {}
        _mod.record_completion(state, "repository_cloning", _mod.step_fingerprint(self.step),
                               "ran", inputs=_mod.inputs_before(self.step))
        write_state(state)
        return state["completed"]["repository_cloning"]

    def _another_git(self):
        """A stand-in `git` first on PATH, as a context manager, that prints another version and
        otherwise runs the real git."""
        real = shutil.which("git")
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        (bin_dir / "git").write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = \"--version\" ]; then echo 'git version 0.0.0-test'; exit 0; fi\n"
            f"exec {real} \"$@\"\n")
        (bin_dir / "git").chmod(0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

    def _end_a_collection(self, repos: list) -> str:
        """End a collection over `repos` with a line for each, as the cloning step does when it
        exits 0, and return its name."""
        from agentcontext import resume
        finished = self.stage / resume.FINISHED_NAME
        finished.unlink()
        with contextlib.redirect_stderr(io.StringIO()):
            collection = resume.open_collection(finished, repos)
        for repo in repos:
            resume.append_finished(finished, repo, f"{repo},row", None,
                                   collection=collection["collection"])
        resume.close_collection(finished)
        return collection["collection"]

    def test_another_git_alone_keeps_the_step(self):
        """Every line of a collection records the version of git, and the step, run under
        another version, would collect every repository again, each at the head that it has
        now. Another version of git alone therefore keeps the step, with a WARNING that names
        the change, the reason and the remedy, without a collection over the list and while a
        collection over the list ended, and also when the runner reaches the step during the
        run. A changed list still runs the step."""
        for ended in (False, True):
            with self.subTest(collection_ended=ended):
                if ended:
                    self._end_a_collection(["o/a"])
                recorded = self._record_with_its_inputs()
                with self._another_git():
                    with _captured_stderr() as err:
                        self.assertIn("repository_cloning",
                                      _mod.completed_steps([self.step], read_state()))
                    self.assertFalse(_mod.changed_during_run(self.step, recorded, read_state()))
                self.assertIn("WARNING runner: The version of git changed since "
                              "repository_cloning ran. Counting repository_cloning as done with "
                              "the collection that the earlier inputs collected, because running "
                              "it under the changed inputs would collect every repository again. "
                              "Run --from repository_cloning to take a new snapshot under the "
                              "changed inputs.", err.getvalue())
                self.assertEqual(read_state()["completed"]["repository_cloning"], recorded)
        self.sample.write_text("repo_name\no/a\no/b\n")
        with self._another_git():
            self.assertTrue(_mod.changed_during_run(self.step, recorded, read_state()))
            with _captured_stderr() as err:
                self.assertNotIn("repository_cloning",
                                 _mod.completed_steps([self.step], read_state()))
        self.assertNotIn("WARNING", err.getvalue())

    def test_a_change_of_how_the_step_collects_runs_the_step_also_beside_another_git(self):
        """A change of the collector's code, the path heuristics or the import heuristics runs
        the step whatever the change reaches, also beside another version of git, and also
        where no collection over the list is in progress or ended, as after `--mark-complete`,
        so that the step begins a new collection. Kept, the step would leave the tree as the
        earlier code and heuristics collected it, and the steps after it would publish the rows
        of the new heuristics over that tree, with no row for a file that a new path heuristic
        matches. The INFO line names what changed, and no line offers `--mark-complete`, which
        would record the tree under the changed inputs."""
        collector = self.tmp / "collector.py"
        originals = {name: (self.heuristics / name).read_bytes()
                     for name in ("config_path_patterns.csv", "import_patterns.csv")}
        for change, git, changed in (
                ("path", False, "The path heuristics"),
                ("path", True, "The version of git and the path heuristics"),
                ("import", False, "The import heuristics"),
                ("import", True, "The version of git and the import heuristics"),
                ("collector", False, "The collector's code"),
                ("collector", True, "The collector's code and the version of git")):
            with self.subTest(change=change, another_git=git), \
                    mock.patch("agentcontext.reuse.collector_files", return_value=[collector]):
                for name, data in originals.items():
                    (self.heuristics / name).write_bytes(data)
                collector.write_text("def collect():\n    return 1\n")
                recorded = self._record_with_its_inputs()
                if change == "path":
                    self._edit_heuristics("path", "ELSEWHERE.md")
                elif change == "import":
                    self._edit_heuristics("max_depth", "9", "import_patterns.csv")
                else:
                    collector.write_text("def collect():\n    return 2\n")
                with contextlib.ExitStack() as stack:
                    if git:
                        stack.enter_context(self._another_git())
                    with _captured_stderr() as err:
                        done = _mod.completed_steps([self.step], read_state())
                    self.assertTrue(_mod.changed_during_run(self.step, recorded, read_state()))
                self.assertNotIn("repository_cloning", done)
                self.assertNotIn("WARNING", err.getvalue())
                self.assertIn(f"INFO runner: {changed} changed since repository_cloning ran. "
                              f"Running repository_cloning under the changed inputs.",
                              err.getvalue())
                self.assertNotIn("--mark-complete", err.getvalue())

    def test_a_change_of_the_heuristics_after_a_collection_over_the_list_ended_continues_it(
            self):
        """A collection over the list ended and keeps its record, and a later image changes only
        the path heuristics, the import heuristics or the collector's code. The step runs and
        continues that collection, which queries no head of a repository that it collected and
        collects again the repositories that the change reaches. After a collection over
        another list, the step begins a new collection."""
        from agentcontext import resume
        collector = self.tmp / "collector.py"
        originals = {name: (self.heuristics / name).read_bytes()
                     for name in ("config_path_patterns.csv", "import_patterns.csv")}
        for change, over in (("path", ["o/a"]), ("import", ["o/a"]), ("collector", ["o/a"]),
                             ("path", ["o/other"])):
            with self.subTest(change=change, collection_over=over), \
                    mock.patch("agentcontext.reuse.collector_files", return_value=[collector]):
                for name, data in originals.items():
                    (self.heuristics / name).write_bytes(data)
                collector.write_text("def collect():\n    return 1\n")
                ended = self._end_a_collection(over)
                self._record_with_its_inputs()
                if change == "path":
                    self._edit_heuristics("path", "ELSEWHERE.md")
                elif change == "import":
                    self._edit_heuristics("max_depth", "9", "import_patterns.csv")
                else:
                    collector.write_text("def collect():\n    return 2\n")
                with _captured_stderr() as err:
                    self.assertNotIn("repository_cloning",
                                     _mod.completed_steps([self.step], read_state()))
                self.assertNotIn("WARNING", err.getvalue())
                with contextlib.redirect_stderr(io.StringIO()) as log:
                    again = resume.open_collection(self.stage / resume.FINISHED_NAME, ["o/a"])
                self.assertEqual(again["collection"] == ended, over == ["o/a"], log.getvalue())

    def test_a_record_without_the_digest_of_each_input_gets_them_while_they_hold(self):
        """A runner that named no digest of each input, such as the runner of the image with the backup providers, recorded
        the step by the hash of its inputs alone. A run under the same inputs names them, and a
        later change of the version of git then keeps the step. Without the digests, the same
        change runs the step."""
        fingerprint = _mod.step_fingerprint(self.step)
        written = {"completed": {"repository_cloning": fingerprint},
                   "completed_by": {"repository_cloning": {
                       "fingerprint": fingerprint, "how": "ran",
                       "finished_at": "2026-11-05T10:00:00+00:00"}}}
        write_state(written)
        state = read_state()
        self.assertIsNone(_mod.recorded_inputs(state, "repository_cloning"))
        with self._another_git(), _captured_stderr():
            self.assertNotIn("repository_cloning", _mod.completed_steps([self.step], state))
        with _captured_stderr():
            done = _mod.completed_steps([self.step], state)
        self.assertTrue(_mod.name_recorded_inputs([self.step], state, done))
        self.assertEqual(_mod.recorded_inputs(state, "repository_cloning"),
                         [tuple(pair) for pair in _mod.inputs_before(self.step)])
        self.assertFalse(_mod.name_recorded_inputs([self.step], state, done))
        with self._another_git(), _captured_stderr():
            self.assertIn("repository_cloning", _mod.completed_steps([self.step], state))

    def _run_stand_in(self, *argv) -> list:
        """Run `main` with `argv` over a stand-in for the cloning step that opens the collection
        over the list of `setUp`, appends its name to `collections.txt`, closes it and rewrites
        the tree unchanged, and return the names written so far."""
        code = ("from pathlib import Path\n"
                "from agentcontext import resume\n"
                "finished = Path('finished_repositories.jsonl')\n"
                "collection = resume.open_collection(finished, ['o/a'])\n"
                "with open('collections.txt', 'a') as f:\n"
                "    f.write(collection['collection'] + '\\n')\n"
                "resume.close_collection(finished)\n"
                "for name in ('data/collected/repos.csv', 'file_history.csv', "
                "'submodules.csv', 'ai_authorship.csv'):\n"
                "    Path(name).write_text(Path(name).read_text())\n")
        stand_in = dataclasses.replace(self.step, interpreter=sys.executable, args=["-c", code])
        with contextlib.ExitStack() as stack:
            stack.enter_context(_swap(_mod, "CORE_STEPS", [stand_in]))
            stack.enter_context(_swap(_mod, "MIN_FREE_DISK_GB", 0))
            stack.enter_context(mock.patch.object(_mod, "pre_checks", return_value=True))
            stack.enter_context(_quiet())
            stack.enter_context(_swap(sys, "argv", ["agentcontext/runner.py", *argv]))
            self.assertEqual(_mod.main(list(argv)), 0)
        names = self.stage / "collections.txt"
        return names.read_text().split() if names.is_file() else []

    def test_from_repository_cloning_takes_a_new_snapshot_and_a_new_heuristic_continues(self):
        """A collection that ended and a new image that changes only the path heuristics: the
        step runs and continues the collection that ended. `--from repository_cloning` marks
        that collection as replaced, and the step begins a new collection over the same
        list."""
        first = self._run_stand_in()
        self.assertEqual(len(first), 1)
        self.assertIsNotNone(_mod.recorded_inputs(read_state(), "repository_cloning"))
        self._edit_heuristics("path", "ELSEWHERE.md")
        self.assertEqual(self._run_stand_in(), first * 2)
        from agentcontext import resume
        self.assertTrue(resume.ended_path(self.stage / resume.FINISHED_NAME).is_file())
        self.assertEqual(self._run_stand_in(), first * 2, "nothing changed since")
        again = self._run_stand_in("--from", "repository_cloning")
        self.assertEqual(len(again), 3)
        self.assertNotEqual(again[2], again[0])

    def test_a_changed_tree_still_offers_mark_complete(self):
        write_state({"completed": {"repository_cloning": _mod.step_fingerprint(self.step)}})
        (self.stage / "data" / "collected" / "o\u00a7a" / "CLAUDE.md").write_text("# b\n")
        with _captured_stderr() as err:
            done = _mod.completed_steps([self.step], read_state())
        self.assertNotIn("repository_cloning", done)
        self.assertIn("--mark-complete repository_cloning", err.getvalue())

    def test_mark_complete_records_the_tree_with_its_inputs_and_refuses_without_the_list(self):
        """The imported tree of `setUp` is empty, as after a collection in which no import
        reaches a file. `may_be_empty` lets the record cover an empty tree."""
        with _captured_stderr():
            self.assertEqual(_mod.mark_complete([self.step], "repository_cloning", time.time(),
                                                ["x"]), 0)
        self.assertEqual(read_state()["completed"]["repository_cloning"],
                         _mod.step_fingerprint(self.step))
        self.assertIn("+inputs:", read_state()["completed"]["repository_cloning"])
        self.sample.unlink()
        with _captured_stderr() as err:
            self.assertEqual(_mod.mark_complete([self.step], "repository_cloning", time.time(),
                                                ["x"]), 1)
        self.assertIn("because an input that it is recorded by is not on disk", err.getvalue())

    def _run_with_git_failing_after_the_step(self, failures) -> int:
        """Run `main` over a stand-in for the cloning step that rewrites the tree of `setUp`
        and exits 0. From the moment the step has exited, `git --version` fails to start
        `failures` times, or every time when `failures` is None, as when a process fork fails
        on the NAS."""
        rewrite = ("from pathlib import Path\n"
                   "for name in ('data/collected/repos.csv', 'file_history.csv', "
                   "'submodules.csv', 'ai_authorship.csv'):\n"
                   "    Path(name).write_text(Path(name).read_text())\n")
        stand_in = dataclasses.replace(self.step, interpreter=sys.executable,
                                       args=["-c", rewrite])
        real_run_step = _mod.run_step
        with contextlib.ExitStack() as stack:
            def run_step(step):
                ok = real_run_step(step)
                stack.enter_context(git_version_fails(failures))
                return ok

            stack.enter_context(_swap(_mod, "CORE_STEPS", [stand_in]))
            stack.enter_context(_swap(_mod, "MIN_FREE_DISK_GB", 0))
            stack.enter_context(mock.patch.object(_mod, "run_step", run_step))
            stack.enter_context(mock.patch.object(_mod, "pre_checks", return_value=True))
            stack.enter_context(_quiet())
            stack.enter_context(_swap(sys, "argv", ["agentcontext/runner.py"]))
            return _mod.main()

    def test_a_git_that_fails_once_the_step_has_run_does_not_lose_the_step(self):
        """The cloning step closes its collection when it exits 0. Because the runner computes
        the step's record from the inputs before the step runs, a git that cannot start after
        the step has exited leaves the step recorded. An unrecorded step would make the next
        Start run it again, which continues the collection that ended, and every step after
        it."""
        for failures in (1, None):
            with self.subTest(failures=failures):
                write_state({})
                self.assertEqual(self._run_with_git_failing_after_the_step(failures), 0)
                recorded = read_state()["completed"]["repository_cloning"]
                self.assertEqual(recorded, _mod.step_fingerprint(self.step))
                self.assertIn("repository_cloning",
                              _mod.completed_steps([self.step], read_state()))

    def test_mark_complete_ends_the_record_of_a_collection_over_another_list(self):
        """The record of a collection over another list names no collection of this list, and
        the next run over this list would begin a new collection anyway. Recording the tree as
        complete closes that record, as the step's own exit 0 does."""
        from agentcontext import resume
        finished = self.stage / resume.FINISHED_NAME
        resume.open_collection(finished, ["o/other"])
        with _captured_stderr():
            self.assertEqual(_mod.mark_complete([self.step], "repository_cloning", time.time(),
                                                ["x"]), 0)
        self.assertFalse(resume.collection_path(finished).exists())
        self.assertIn("repository_cloning", read_state()["completed"])


class TestMarkCompleteRefusesATreeFullOfFailures(_RunLogCase):
    """--mark-complete repository_cloning over a tree with a clone_failed.csv that has more
    clone_failed rows than the step itself accepts."""

    def setUp(self):
        super().setUp()
        self._saved = (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS, _mod.STAGE_DIR)
        # The stage directory is below the temporary directory, so that the list of the
        # sample, `../classification/` from the stage directory, is inside it as well.
        self.stage = self.tmp / "collection"
        _mod.STAGE_DIR = self.stage
        _mod.CORE_STEPS = [Step('repository_cloning', sys.executable, ["-c", "pass"],
                                  produces=("data/collected/repos.csv", "data/collected"),
                                  completion=COMPLETION_OUTPUT)]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        collected = self.stage / "data" / "collected"
        collected.mkdir(parents=True)
        (collected / "repos.csv").write_text("repo_name\no/a\n")
        self.failed = collected / "clone_failed.csv"
        self.listed = self.stage / _mod.DEFAULT_REPO_CSV
        self.listed.parent.mkdir(parents=True, exist_ok=True)
        self.listed.write_text("repo_name\n")

    def tearDown(self):
        _mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS, _mod.STAGE_DIR = self._saved
        super().tearDown()

    def _list(self, repositories: int) -> int:
        """Write a collected list of `repositories` repositories and return the clone failure
        ceiling for that number."""
        (self.stage / "data" / "collected" / "repos.csv").write_text(
            "repo_name\n" + "".join(f"o/r{i}\n" for i in range(repositories)))
        return _mod.clone_failure_ceiling(repositories)

    def _write_failures(self, transient: int, permanent: int = 0):
        rows = ["repo_name,url,outcome"]
        rows += [f"o/t{i},https://x,clone_failed" for i in range(transient)]
        rows += [f"o/p{i},https://x,not_found_or_no_access" for i in range(permanent)]
        self.failed.write_text("\n".join(rows) + "\n")

    def _mark(self) -> int:
        with _captured_stderr() as err:
            code = _mod.mark_complete(_mod.CORE_STEPS, 'repository_cloning', time.time(), ["x"])
        self.err = err.getvalue()
        return code

    def test_a_list_that_cannot_be_read_is_not_read_as_no_failures(self):
        """A list that the process cannot open does not count as a clean tree. The test calls
        the function directly, because `mark_complete` refuses such a tree anyway. The step
        declares `data/collected`, and `output_fingerprint` reaches the same unreadable file
        first. A test through the command would pass even without this check."""
        self._write_failures(2)
        os.chmod(self.failed, 0o000)
        try:
            try:
                self.failed.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            with self.assertRaises(OSError):
                _mod.transient_failures(self.failed)
        finally:
            os.chmod(self.failed, 0o644)

    def test_a_list_that_is_not_there_is_no_failures(self):
        self.assertEqual(_mod.transient_failures(self.failed.parent / "gone.csv"), 0)

    def test_a_tree_whose_list_cannot_be_read_is_never_recorded(self):
        """Whichever check reaches the unreadable list first, the step stays unrecorded."""
        self._write_failures(1)
        os.chmod(self.failed, 0o000)
        try:
            try:
                self.failed.read_bytes()
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            self.assertEqual(self._mark(), 1)
        finally:
            os.chmod(self.failed, 0o644)

    def test_over_the_ceiling_is_refused_with_the_reason(self):
        sample = 36_710
        self._write_failures(self._list(sample) + 1)
        self.assertEqual(self._mark(), 1)
        self.assertIn(f"That exceeds the ceiling of 51 for {sample:,} repositories", self.err)
        self.assertIn("clone_failed", self.err)
        self.assertIn("Run the step to retry them.", self.err)
        self.assertNotIn('repository_cloning', read_state().get("completed", {}))

    def test_at_the_ceiling_is_recorded(self):
        self._write_failures(self._list(36_710), permanent=5)
        self.assertEqual(self._mark(), 0)
        self.assertIn('repository_cloning', read_state()["completed"])

    def test_collector_errors_count_as_rows_a_run_retries(self):
        rows = ["repo_name,url,outcome"]
        rows += [f"o/e{i},https://x,collector_error" for i in range(self._list(67) + 1)]
        self.failed.write_text("\n".join(rows) + "\n")
        self.assertEqual(self._mark(), 1)
        self.assertIn("collector_error", self.err)
        self.assertEqual(_mod.TRANSIENT_OUTCOMES, ("clone_failed", "collector_error"))

    def test_permanent_outcomes_do_not_count(self):
        self._write_failures(0, permanent=self._list(67) + 10)
        self.assertEqual(self._mark(), 0)

    def test_no_failure_list_counts_as_none(self):
        self.assertEqual(_mod.transient_failures(self.stage / "absent.csv"), 0)
        self.assertEqual(self._mark(), 0)

    def _finished(self, repos: list, redo: bool, collection: "str | None" = None,
                  mode: str = "w") -> None:
        """Lines of `finished_repositories.jsonl` for `repos`, written by `collection`."""
        with open(self.stage / "finished_repositories.jsonl", mode, encoding="utf-8") as f:
            for repo in repos:
                f.write(json.dumps({"repo": repo, "row": f"{repo},x", "fail_row": None,
                                    **({"redo": True} if redo else {}),
                                    **({"collection": collection} if collection else {})})
                        + "\n")

    def test_repositories_marked_for_retry_count_toward_the_ceiling(self):
        # Before the step compares its clone failures with the ceiling, it adds the ok
        # lines that it has to collect again. The record counts these lines too. Otherwise
        # it would record a tree for which the step itself exits 4.
        ceiling = self._list(36_710)
        self._write_failures(ceiling)
        self._finished(["o/r0", "o/r1"], redo=True)
        self.assertEqual(self._mark(), 1)
        self.assertIn("finished_repositories.jsonl", self.err)
        self.assertNotIn('repository_cloning', read_state().get("completed", {}))
        self._finished(["o/r0", "o/r1"], redo=False)
        self.assertEqual(self._mark(), 0)

    def test_heads_without_a_git_response_count_toward_the_ceiling(self):
        """The step adds the heads without a git response to its failures before it compares
        them with the ceiling, and the record does too."""
        ceiling = self._list(36_710)
        self._write_failures(ceiling)
        with open(self.stage / "finished_repositories.jsonl", "w", encoding="utf-8") as f:
            for repo in ("o/r0", "o/r1"):
                f.write(json.dumps({"repo": repo, "row": f"{repo},x", "fail_row": None,
                                    "head_no_response": True}) + "\n")
        self.assertEqual(self._mark(), 1)
        self.assertNotIn('repository_cloning', read_state().get("completed", {}))

    def test_a_tree_without_a_line_for_a_repository_of_its_list_is_never_recorded(self):
        """A first collection that low disk space or a failed connectivity probe stopped
        leaves a collected list of only the repositories that it reached. A tree restored from
        its handover and recorded as complete would be published as the sample."""
        self.listed.write_text("repo_name\n" + "".join(f"o/r{i}\n" for i in range(200)))
        self._list(1)
        self._finished(["o/r0"], redo=False)
        self.assertEqual(self._mark(), 1)
        self.assertIn("199 of the 200 repositories", self.err)
        self.assertIn("o/r1", self.err)
        self.assertNotIn('repository_cloning', read_state().get("completed", {}))
        self._finished([f"o/r{i}" for i in range(200)], redo=False)
        self.assertEqual(self._mark(), 0)

    def test_a_collection_that_has_not_ended_is_never_recorded(self):
        """A collection that ends closes its record, and an open record therefore names a
        collection that stopped or was killed. The lines of the list of finished repositories
        cannot show what that collection still had to do. The list keeps the lines of every
        earlier collection, and a repository that the collection was retrying still has a line
        although its files are gone. Running the step finishes the collection."""
        from agentcontext import resume
        names = [f"o/r{i}" for i in range(200)]
        self.listed.write_text("repo_name\n" + "".join(f"{name}\n" for name in names))
        self._list(200)
        self._finished(names, redo=False, collection="2026-09-05T000000Z-earlier")
        finished = self.stage / resume.FINISHED_NAME
        record = resume.open_collection(finished, names)
        record["taken_over"] = set(names)
        resume.save_collection(finished, record)
        self.assertEqual(self._mark(), 1)
        self.assertIn(f"collection {record['collection']}", self.err)
        self.assertIn("has not ended", self.err)
        self.assertNotIn('repository_cloning', read_state().get("completed", {}))
        self.assertTrue(resume.collection_path(finished).exists())
        resume.close_collection(finished)
        self.assertEqual(self._mark(), 0)

    def test_a_collection_over_the_list_with_repositories_found_gone_has_not_ended_either(
            self):
        """The step continues a collection over a list of which its sample lacks only
        repositories that the labeling found gone (`resume.open_collection`), and that
        collection has not ended either. Recording the tree removes the record of the
        collection that ended too, which nothing ties to the recorded tree."""
        from agentcontext import resume
        names = [f"o/r{i}" for i in range(200)]
        self.listed.write_text("repo_name\n" + "".join(f"{name}\n" for name in names))
        (self.listed.parent / "repos_excluded.csv").write_text(
            "repo_name,repo_url,reason,detected_language\n"
            "o/gone,https://github.com/o/gone,not_found_or_no_access,\n")
        self._list(200)
        self._finished(names, redo=False, collection="2026-09-05T000000Z-earlier")
        finished = self.stage / resume.FINISHED_NAME
        with _captured_stderr():
            resume.open_collection(finished, [*names, "o/gone"])
        self.assertEqual(self._mark(), 1)
        self.assertIn("has not ended", self.err)
        resume.close_collection(finished)
        self.assertTrue(resume.ended_path(finished).exists())
        self.assertEqual(self._mark(), 0)
        self.assertFalse(resume.ended_path(finished).exists())

    def test_a_commit_history_without_its_retained_log_is_never_recorded(self):
        self._list(10)
        commits_dir = self.stage / "collect_commits"
        commits_dir.mkdir()
        (commits_dir / "o§r3.json").write_text(json.dumps(
            {"repo_name": "o/r3", "outcome": "history_read", "commit_evidence": {"head_sha": "a"},
             "unmatched": [], "no_signal_shas": [], "ai_rows": []}))
        self.assertEqual(self._mark(), 1)
        self.assertIn("o/r3", self.err)
        self.assertIn("commit log", self.err)
        (commits_dir / "o§r3.log.xz").write_bytes(b"")
        self.assertEqual(self._mark(), 0)


# ---------------------------------------------------------------------------
# CI coverage  (the workflow's module list against the modules on disk)
# ---------------------------------------------------------------------------

class TestContinuousIntegrationCoverage(unittest.TestCase):
    """CI lists its test modules by name rather than discovering them, so that
    discovery cannot recurse into an extracted collected tree. The list and the
    directory can then disagree in either direction, and the two disagreements
    fail differently. A module that the list leaves out never runs. A module that
    the list keeps after its file is gone fails the job with a
    `ModuleNotFoundError` for a suite that no longer exists."""

    ROOT = REPO_ROOT
    WORKFLOW = ROOT / ".github" / "workflows" / "pipeline-tests.yml"
    HERE = "tests"
    SAMPLING = "1_sampling"
    CLASSIFICATION = "2_classification"
    ANALYSIS = "4_data-analysis"
    REPOSITORY = "."
    CONTAINER = "deploy"

    def _listed(self) -> dict:
        """The modules that each `python3 -m unittest` block runs, keyed by the block's
        directory.

        A listed module is a line that contains the module's name and nothing else
        besides the shell continuation. A comment of the workflow can name a module
        without listing it.
        """
        if not self.WORKFLOW.is_file():
            self.skipTest("workflow file not present")
        listed: dict = {}
        directory = "."
        for line in self.WORKFLOW.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("working-directory:"):
                directory = stripped.split(":", 1)[1].strip()
            found = re.fullmatch(r"(?:([A-Za-z0-9_]+)\.)?(test_[A-Za-z0-9_]+)\s*\\?", stripped)
            if found:
                # `tests.test_x` in the root block is the module tests/test_x.py
                where = str(Path(directory) / found.group(1)) if found.group(1) else directory
                listed.setdefault(where, set()).add(found.group(2))
        return listed

    def test_the_sampling_runner_suite_runs_without_site_packages_before_the_install(self):
        """The sampling runner suite passes with the standard library and the agentcontext of the
        checkout alone. CI runs it before any install, with `python3 -S`, which imports no site
        packages, and the checkout on PYTHONPATH, and `python -m agentcontext verify` runs it the
        same way with the interpreter of an empty virtual environment. Every other suite runs
        after the one install of the development lock."""
        workflow = self.WORKFLOW.read_text()
        suites = [(match.start(), match.group(0)) for match in
                  re.finditer(r"python3(?:\s+-[A-Za-z]+)*\s+-m unittest", workflow)]
        installs = [match.start() for match in re.finditer(r"\bpip install\b", workflow)]
        self.assertTrue(installs)
        runner = workflow.index("            test_run_sampling")
        step = workflow[workflow.rindex("- name:", 0, runner):runner]
        self.assertIn("python3 -S -m unittest", step)
        self.assertIn("PYTHONPATH: ${{ github.workspace }}", step)
        self.assertEqual([command for position, command in suites if position < max(installs)],
                         ["python3 -S -m unittest"])
        self.assertLess(runner, min(installs))
        self.assertEqual(len(re.findall(r"-r locks/development\.txt", workflow)), 1)

    def test_the_checkout_reads_the_history_that_the_check_of_removed_ids_needs(self):
        """test_every_id_in_the_history_of_the_two_files_is_a_row_or_removed reads the history of
        the two commit heuristics files. In the default clone of depth 1 it reads no removal and
        passes on any removed row."""
        if not self.WORKFLOW.is_file():
            self.skipTest("workflow file not present")
        step = re.search(r"- uses: actions/checkout@\S+\n((?:[ ]{8,}.*\n)*)",
                         self.WORKFLOW.read_text())
        self.assertIsNotNone(step)
        self.assertRegex(step.group(1), r"(?m)^ +fetch-depth: 0$")

    def test_no_step_of_the_workflow_may_fail(self):
        """A block that is allowed to fail runs its suite without failing the job. A
        `continue-on-error` on a job or a step, or a module line that ends in `|| true`, lets
        every listing check above pass while the suite fails."""
        if not self.WORKFLOW.is_file():
            self.skipTest("workflow file not present")
        for number, line in enumerate(self.WORKFLOW.read_text().splitlines(), 1):
            with self.subTest(line=number):
                self.assertNotRegex(line, r"continue-on-error:\s*(?!false\b)\S")
                self.assertNotRegex(line, r"\|\|\s*(true|:)\s*$")

    def test_every_test_module_is_listed_in_the_workflow(self):
        listed = self._listed().get(self.HERE, set())
        for path in sorted((self.ROOT / self.HERE).glob("test_*.py")):
            with self.subTest(module=path.stem):
                self.assertIn(path.stem, listed)

    def test_every_sampling_test_module_is_listed(self):
        # A sampling suite that the workflow does not list is documented in CLAUDE.md
        # and run by nobody.
        listed = self._listed().get(self.SAMPLING, set())
        for path in sorted((self.ROOT / self.SAMPLING).glob("test_*.py")):
            with self.subTest(module=path.stem):
                self.assertIn(path.stem, listed)

    def test_every_step_with_a_suite_has_a_block_in_the_workflow(self):
        """The test finds every directory of this repository that contains a suite by
        listing files. Starting from the directories that the workflow already names would
        check only covered directories and miss a suite elsewhere, such as in `_website/`."""
        skip = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules",
                "_site", "release", "data"}
        listed = self._listed()
        found = set()
        # The files that git lists are the tracked files and the untracked files that no
        # ignore pattern covers. A suite that is added but not yet committed counts
        # too. A listing of the filesystem would also count every ignored copy, and a git
        # worktree under `.claude/worktrees/` contains this whole repository.
        listing = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=self.ROOT, capture_output=True, text=True, check=True).stdout
        for name in filter(None, listing.split("\0")):
            path = self.ROOT / name
            if not (path.name.startswith("test_") and path.suffix == ".py"):
                continue
            if set(path.relative_to(self.ROOT).parts) & skip:
                continue
            directory = str(path.parent.relative_to(self.ROOT)) or "."
            found.add(self.REPOSITORY if directory == "." else directory)
        for directory in sorted(found):
            with self.subTest(directory=directory):
                if directory in listed:
                    continue
                # A directory may be left out of the workflow on purpose, for example when
                # its suite needs packages that the workflow does not install. The
                # `0_README.md` beside the suite then has to state the reason, where the next
                # reader finds it.
                readme = self.ROOT / directory / "0_README.md"
                self.assertTrue(readme.is_file(),
                                f"{directory} contains a suite that no workflow block runs, "
                                f"and has no 0_README.md that gives the reason")
                self.assertIn("not run by the workflow", readme.read_text(encoding="utf-8"),
                              f"{directory} contains a suite that no workflow block runs, "
                              f"and its 0_README.md does not contain 'not run by the "
                              f"workflow'")

    def test_every_test_module_of_the_other_steps_is_listed(self):
        directories = self._listed()
        self.assertTrue({"tests", "1_sampling", "2_classification", "4_data-analysis", ".", "deploy", "_website"} <= directories.keys())
        for directory, listed in sorted(directories.items()):
            for path in sorted((self.ROOT / directory).glob("test_*.py")):
                with self.subTest(directory=directory, module=path.stem):
                    self.assertIn(path.stem, listed)

    def test_every_listed_module_is_a_module_that_exists(self):
        # The direction that the two tests above cannot check. Both start from the files
        # on disk, and neither reads a name that stays on the list after its file is
        # deleted.
        for directory, modules in sorted(self._listed().items()):
            for module in sorted(modules):
                with self.subTest(directory=directory, module=module):
                    self.assertTrue(
                        (self.ROOT / directory / f"{module}.py").is_file(),
                        f"{directory} has no {module}.py")

    def test_each_block_runs_in_a_directory_that_exists(self):
        # The tests check every module against the directory that its own block runs in.
        # A block with a misspelled working-directory would make every module in it fail
        # as missing rather than as misplaced.
        for directory in sorted(self._listed()):
            with self.subTest(directory=directory):
                self.assertTrue((self.ROOT / directory).is_dir())



# ---------------------------------------------------------------------------
# The fields that let one step list span the five stages
# ---------------------------------------------------------------------------

class TestTheStepRoot(_RunLogCase):
    """A step names the directory that it runs in under the data root, and the directory of
    its script in the repository. An empty root is the collection stage's directory."""

    def test_the_data_directory_is_the_stages_one_under_the_data_root_or_an_absolute_one(self):
        self.assertEqual(_mod.data_dir(Step("a", "python3", ["a.py"])), _mod.STAGE_DIR)
        self.assertEqual(_mod.data_dir(Step("a", "python3", ["a.py"], root="sampling")),
                         _mod.DATA_ROOT / "sampling")
        self.assertEqual(_mod.data_dir(Step("a", "python3", ["a.py"], root=str(self.tmp))),
                         self.tmp)

    def test_the_code_directory_is_under_the_repository_root_or_the_data_directory(self):
        self.assertEqual(_mod.code_dir(Step("a", "python3", ["a.py"], root="sampling",
                                            code="1_sampling")), _mod.REPO_ROOT / "1_sampling")
        self.assertEqual(_mod.code_dir(Step("a", "python3", ["a.py"], root=str(self.tmp))),
                         self.tmp)

    def test_a_script_runs_in_the_data_directory_from_its_code_directory(self):
        """The script stays in the repository, and what it writes goes to the data directory.
        A step's paths resolve against the data directory."""
        code, data = self.tmp / "code", self.tmp / "data"
        code.mkdir()
        data.mkdir()
        (code / "s.py").write_text("open('out.txt', 'w').write('x')\n")
        step = Step("s", sys.executable, ["s.py"], produces=("out.txt",), root=str(data),
                    code=str(code))
        with _quiet():
            self.assertTrue(_mod.run_step(step))
        self.assertEqual((data / "out.txt").read_text(), "x")
        self.assertFalse((code / "out.txt").exists())
        # The fingerprint follows the script in the code directory, not a file of the same name
        # in the data directory.
        before = _mod.script_fingerprint(step)
        self.assertTrue(before)
        (data / "s.py").write_text("print('elsewhere')\n")
        self.assertEqual(_mod.script_fingerprint(step), before)
        (code / "s.py").write_text("open('out.txt', 'w').write('y')\n")
        self.assertNotEqual(_mod.script_fingerprint(step), before)

    def test_a_step_creates_its_data_directory(self):
        """Over an empty data root, no stage has a directory yet. If the runner did not create
        a step's working directory, every first run would fail at its first step."""
        data = self.tmp / "data" / "sampling"
        step = Step("w", sys.executable, ["-c", "open('out.txt', 'w').write('x')"],
                    produces=("out.txt",), root=str(data))
        with _quiet():
            self.assertTrue(_mod.run_step(step))
        self.assertEqual((data / "out.txt").read_text(), "x")

    def test_a_code_input_is_fingerprinted_under_the_code_directory(self):
        code, data = self.tmp / "code", self.tmp / "data"
        code.mkdir()
        data.mkdir()
        (code / "s.py").write_text("print(1)\n")
        (code / "module.py").write_text("A = 1\n")
        (data / "table.csv").write_text("a\n")
        step = Step("s", sys.executable, ["s.py"], inputs=("table.csv",),
                    code_inputs=("module.py",), completion=COMPLETION_INPUTS, root=str(data),
                    code=str(code))
        before = _mod.inputs_fingerprint(step)
        self.assertTrue(before)
        (code / "module.py").write_text("A = 2\n")
        self.assertNotEqual(_mod.inputs_fingerprint(step), before)
        (code / "module.py").unlink()
        self.assertEqual(_mod.inputs_fingerprint(step), "")

    def test_a_step_runs_in_its_root_and_its_outputs_resolve_there(self):
        step = Step("w", sys.executable, ["-c", "open('out.txt', 'w').write('x')"],
                      produces=("out.txt",), root=str(self.tmp))
        with _quiet():
            self.assertTrue(_mod.run_step(step))
        self.assertEqual((self.tmp / "out.txt").read_text(), "x")

    def test_the_script_is_hashed_under_the_root(self):
        (self.tmp / "s.py").write_text("print(1)\n")
        step = Step("s", sys.executable, ["s.py"], root=str(self.tmp))
        before = _mod.script_fingerprint(step)
        self.assertTrue(before)
        self.assertEqual(_mod.script_fingerprint(
            Step("s", sys.executable, ["s.py"], root=str(self.tmp / "elsewhere"))), "")
        (self.tmp / "s.py").write_text("print(2)\n")
        self.assertNotEqual(_mod.script_fingerprint(step), before)

    def test_the_list_shows_the_steps_under_their_stage(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = _mod.run([Step("a", "python3", ["a.py"], stage="sampling"),
                             Step("b", "python3", ["b.py"], stage="sampling"),
                             Step("c", "python3", ["c.py"], stage="analysis")],
                            list_only=True)
        self.assertEqual(code, 0)
        lines = [line.split("runner: ", 1)[1] for line in out.getvalue().splitlines()]
        self.assertEqual(lines, ["The stage sampling runs these steps in order.",
                                 "  a runs python3 a.py.", "  b runs python3 b.py.",
                                 "The stage analysis runs these steps in order.",
                                 "  c runs python3 c.py."])

    def test_a_stage_is_known_to_from_and_refused_by_mark_complete(self):
        steps = [Step("a", "python3", ["a.py"], stage="sampling"),
                 Step("b", "python3", ["b.py"], stage="sampling")]
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(_mod.run(steps, list_only=True, from_name="sampling"), 0)
            self.assertEqual(_mod.run(steps, mark="sampling"), 1)
            with self.assertRaises(SystemExit):
                _mod.run(steps, list_only=True, from_name="nothing")
        self.assertIn("--mark-complete records one step", out.getvalue())
        self.assertIn("Unknown stage or step 'nothing'. The stage sampling has the steps a and "
                      "b.", out.getvalue())

    def test_the_list_names_the_data_directory_and_the_script_under_its_code_directory(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            code = _mod.run([Step("a", "python3", ["a.py"], root="sampling", code="1_sampling"),
                             Step("b", "python3", ["b.py"])], list_only=True)
        self.assertEqual(code, 0)
        self.assertIn("a runs python3 1_sampling/a.py (data directory sampling).\n",
                      out.getvalue())
        self.assertIn("b runs python3 b.py.\n", out.getvalue())


class TestTheInputsRule(_RunLogCase):
    """A step recorded by its inputs runs again when a CSV that it reads changes, as a figure
    does."""

    def setUp(self):
        super().setUp()
        (self.tmp / "figure.py").write_text("print('figure')\n")
        (self.tmp / "CSV.csv").write_text("a,b\n1,2\n")
        self.step = Step("figure", sys.executable, ["figure.py"], inputs=("CSV.csv",),
                           completion=COMPLETION_INPUTS, root=str(self.tmp))

    def test_the_rule_needs_an_input(self):
        with self.assertRaises(ValueError):
            Step("x", "python3", ["x.py"], completion=COMPLETION_INPUTS)

    def test_the_fingerprint_follows_the_input_and_the_script(self):
        first = _mod.step_fingerprint(self.step)
        self.assertTrue(first.startswith("inputs:"))
        (self.tmp / "CSV.csv").write_text("a,b\n1,3\n")
        second = _mod.step_fingerprint(self.step)
        self.assertNotEqual(first, second)
        (self.tmp / "figure.py").write_text("print('other')\n")
        self.assertNotEqual(second, _mod.step_fingerprint(self.step))

    def test_a_missing_input_fingerprints_to_nothing(self):
        (self.tmp / "CSV.csv").unlink()
        self.assertEqual(_mod.step_fingerprint(self.step), "")

    def test_what_inputs_of_names_is_part_of_the_fingerprint(self):
        """The content-quality analysis reads the files of the validated tree that its CSVs
        list, and no fixed list of paths can name these files. What `inputs_of` names is
        therefore part of the fingerprint of a step recorded by its inputs too. Otherwise a
        tree that changed under the same CSVs would leave the step done."""
        digest = {"value": "one"}
        named = dataclasses.replace(self.step,
                                    inputs_of=lambda step: [("the listed files", digest["value"])])
        first = _mod.step_fingerprint(named)
        self.assertTrue(first.startswith("inputs:"))
        digest["value"] = "two"
        self.assertNotEqual(_mod.step_fingerprint(named), first)
        digest["value"] = None
        self.assertEqual(_mod.step_fingerprint(named), "")
        self.assertNotEqual(first, _mod.step_fingerprint(self.step))

    def test_an_input_may_name_alternatives_and_the_first_on_disk_counts(self):
        (self.tmp / "CSV.csv.7z").write_text('collected_files_archive')
        either = Step("figure", sys.executable, ["figure.py"], root=str(self.tmp),
                       inputs=("CSV.csv|CSV.csv.7z",), completion=COMPLETION_INPUTS)
        with_csv = _mod.step_fingerprint(either)
        self.assertEqual(_mod.input_path(either, "CSV.csv|CSV.csv.7z"), self.tmp / "CSV.csv")
        (self.tmp / "CSV.csv").unlink()
        self.assertEqual(_mod.input_path(either, "CSV.csv|CSV.csv.7z"), self.tmp / "CSV.csv.7z")
        self.assertNotEqual(_mod.step_fingerprint(either), with_csv)
        self.assertTrue(_mod.step_fingerprint(either).startswith("inputs:"))

    def test_an_archive_after_the_extracted_file_counts_too(self):
        """A checkout keeps the extracted commit CSV beside the archive that git tracks, and a
        pull can replace the archive alone. The loader compares the two only when the step
        runs, so a changed archive or volume has to run the step again."""
        for volume in ("CSV.csv.7z.001", "CSV.csv.7z.002"):
            (self.tmp / volume).write_bytes(b"archive data")
        either = Step("figure", sys.executable, ["figure.py"], root=str(self.tmp),
                      inputs=("CSV.csv|CSV.csv.7z.*",), completion=COMPLETION_INPUTS)
        self.assertEqual(_mod.input_path(either, "CSV.csv|CSV.csv.7z.*"), self.tmp / "CSV.csv")
        for volume in ("CSV.csv.7z.001", "CSV.csv.7z.002"):
            with self.subTest(volume=volume):
                before = _mod.step_fingerprint(either)
                (self.tmp / volume).write_bytes(b"other archive data")
                self.assertNotEqual(_mod.step_fingerprint(either), before)

    def test_another_alternative_after_the_first_on_disk_does_not_count(self):
        """`output/repos.csv` makes the entry of `collection_summary` present when no commit
        reclassification wrote its record, and the step reads the record when there is one. A
        change to the stand-in changes nothing that the step reads and leaves its fingerprint,
        and with it a finished run on the NAS, as it was."""
        (self.tmp / "record.json").write_text("{}\n")
        either = Step("figure", sys.executable, ["figure.py"], root=str(self.tmp),
                      inputs=("record.json|CSV.csv",), completion=COMPLETION_INPUTS)
        before = _mod.step_fingerprint(either)
        (self.tmp / "CSV.csv").write_text("a,b\n9,9\n")
        self.assertEqual(_mod.step_fingerprint(either), before)
        summary = next(s for s in _mod.CORE_STEPS if s.key == "collection_summary")
        entry = next(rel for rel in summary.inputs if "|" in rel)
        output = self.tmp / "output"
        output.mkdir()
        for name in ("commit-reclassification.json", "repos.csv"):
            (output / name).write_text("x\n")
        self.assertEqual(_mod.input_paths(dataclasses.replace(summary, root=str(self.tmp)), entry),
                         [output / "commit-reclassification.json"])

    def test_a_changed_input_drops_the_step_and_what_reads_it(self):
        reader = Step("reader", sys.executable, ["figure.py"], consumes=("figure",),
                       root=str(self.tmp))
        steps = [self.step, reader]
        state = {"completed": {s.key: _mod.step_fingerprint(s) for s in steps}}
        with _quiet():
            self.assertEqual(_mod.completed_steps(steps, state), {"figure", "reader"})
        (self.tmp / "CSV.csv").write_text("a,b\n9,9\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_mod.completed_steps(steps, state), set())
        self.assertIn("An input of figure (CSV.csv) or its script has changed", err.getvalue())

    def test_an_input_an_earlier_step_of_the_run_rewrites_redoes_the_step(self):
        """The steps after the GHS download read the retrieval that the download's pointer
        names, and they do not consume the download, so that a fallback to the same retrieval
        redoes none of them. A run that decided what was done only before it began would skip
        these steps after a new retrieval until the next run."""
        (self.tmp / "writer.py").write_text(
            "import os; open('CSV.csv', 'w').write(os.environ.get('CONTENT', 'a,b\\n1,2\\n'))\n")
        (self.tmp / "figure.py").write_text("open('ran.log', 'a').write('figure\\n')\n")
        (self.tmp / "table.py").write_text("open('ran.log', 'a').write('table\\n')\n")
        writer = Step("writer", sys.executable, ["writer.py"], produces=("CSV.csv",),
                       completion=COMPLETION_OUTPUT, root=str(self.tmp))
        table = Step("table", sys.executable, ["table.py"], consumes=("figure",),
                      root=str(self.tmp))
        steps = [writer, self.step, table]

        def run(*args):
            with _quiet():
                return _mod.run(steps, argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0,
                                from_name=args[0] if args else None)

        self.assertEqual(run(), 0)
        os.environ["CONTENT"] = "a,b\n1,2\n"
        self.addCleanup(os.environ.pop, "CONTENT", None)
        self.assertEqual(run("writer"), 0)
        self.assertEqual((self.tmp / "ran.log").read_text().split(), ["figure", "table"])
        os.environ["CONTENT"] = "a,b\n7,7\n"
        self.assertEqual(run("writer"), 0)
        self.assertEqual((self.tmp / "ran.log").read_text().split(),
                         ["figure", "table", "figure", "table"])

    def test_a_reader_that_fails_after_its_input_changed_during_the_run_runs_again(self):
        """The figure is recorded by the CSV that the writer rewrites, and the table reads the
        figure. When the table fails after the figure is recorded again, the table must lose
        its old record. That record matches the table's unchanged script, and the next run
        would otherwise skip the table."""
        (self.tmp / "writer.py").write_text(
            "import os; open('CSV.csv', 'w').write(os.environ.get('CONTENT', 'a,b\\n1,2\\n'))\n")
        (self.tmp / "figure.py").write_text("open('ran.log', 'a').write('figure\\n')\n")
        (self.tmp / "table.py").write_text(
            "import os, sys; open('ran.log', 'a').write('table\\n')\n"
            "sys.exit(5 if os.environ.get('TABLE_FAILS') else 0)\n")
        writer = Step("writer", sys.executable, ["writer.py"], produces=("CSV.csv",),
                       completion=COMPLETION_OUTPUT, root=str(self.tmp))
        table = Step("table", sys.executable, ["table.py"], consumes=("figure",),
                      root=str(self.tmp))
        steps = [writer, self.step, table]

        def run(*args):
            with _quiet():
                return _mod.run(steps, argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0,
                                from_name=args[0] if args else None)

        self.addCleanup(os.environ.pop, "CONTENT", None)
        self.addCleanup(os.environ.pop, "TABLE_FAILS", None)
        self.assertEqual(run(), 0)
        os.environ["CONTENT"] = "a,b\n7,7\n"
        os.environ["TABLE_FAILS"] = "1"
        self.assertEqual(run("writer"), 1)
        del os.environ["TABLE_FAILS"]
        (self.tmp / "ran.log").write_text("")
        self.assertEqual(run(), 0)
        self.assertEqual((self.tmp / "ran.log").read_text().split(), ["table"])

    def test_an_output_that_cannot_be_read_stops_the_run_with_a_message(self):
        """`entry_kind` re-raises every errno outside the four that mean absence, such as the
        errors of a mount that is present but not responding. `step_fingerprint_or_stop` has
        to convert the error into a `SystemExit`. An `UnreadableOutput` is not an `OSError`.
        Nothing above `completed_steps` would catch it, and the run would end on a traceback
        without a message."""
        blocked = self.tmp / "mount"
        blocked.mkdir()
        (blocked / "out.csv").write_text("a\n", encoding="utf-8")
        step = Step("figure", sys.executable, ["figure.py"], produces=("mount/out.csv",),
                      completion=COMPLETION_SCRIPT, root=str(self.tmp))
        state = {"completed": {"figure": _mod.step_fingerprint(step)}}
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        try:
            os.listdir(blocked)
        except PermissionError:
            pass
        else:
            self.skipTest("this filesystem or user ignores the mode")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
            _mod.completed_steps([step], state)
        self.assertIn("Could not read an output of step 'figure'", str(caught.exception))

    def test_an_output_that_is_gone_drops_the_step_whatever_the_rule(self):
        """The fingerprint of a step recorded by its script is the same whether or not its
        output is there, and a step recorded by its inputs never reads `produces`. Without
        this check, deleting `output/repos.csv` after a finished run would leave every step
        after `collected_file_cleanup` counting as done, and the next run would exit 0 without
        the file."""
        for completion in (COMPLETION_SCRIPT, COMPLETION_INPUTS):
            with self.subTest(completion=completion):
                (self.tmp / "out.csv").write_text("a\n1\n", encoding="utf-8")
                step = Step("figure", sys.executable, ["figure.py"],
                              inputs=("CSV.csv",), produces=("out.csv",),
                              completion=completion, root=str(self.tmp))
                state = {"completed": {"figure": _mod.step_fingerprint(step)}}
                with _quiet():
                    self.assertEqual(_mod.completed_steps([step], state), {"figure"})
                (self.tmp / "out.csv").unlink()
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(_mod.completed_steps([step], state), set())
                self.assertIn("declares output that is not on disk (out.csv)", err.getvalue())

    def test_the_mark_and_list_guards_are_in_run_not_in_one_entry_point(self):
        """Only `python -m agentcontext run` reaches `main`, and the other runners call `run`
        directly. If the checks were only in `main`, the other runners would accept
        `--mark-complete` with `--list`, print the step list, exit 0 and record nothing.
        `--list --from nosuchstage` would also return 0 without a message."""
        step = Step("figure", sys.executable, ["figure.py"], root=str(self.tmp))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(_mod.run([step], list_only=True, mark="figure"), 1)
        self.assertIn("cannot be combined with --list", err.getvalue())

        err = io.StringIO()
        with (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err),
              self.assertRaises(SystemExit) as caught):
            _mod.run([step], list_only=True, from_name="nosuchstage")
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("Unknown stage or step 'nosuchstage'", err.getvalue())

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(_mod.run([step], list_only=True), 0)

    def test_a_missing_input_is_logged(self):
        state = {"completed": {"figure": _mod.step_fingerprint(self.step)}}
        (self.tmp / "CSV.csv").unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_mod.completed_steps([self.step], state), set())
        self.assertIn("declares input that is not on disk (CSV.csv)", err.getvalue())


class TestWhatAnEarlierStepCanMakePending(_RunLogCase):
    """A step recorded as done can run again when an earlier step of the run changes what
    decides its record. The pre-checks verify what such a step needs before the first step
    starts."""

    def setUp(self):
        super().setUp()
        self.data = self.tmp / "data"
        self.data.mkdir()

    def _run(self, steps, **options) -> int:
        with _quiet():
            return _mod.run(steps, argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0,
                            credentials_path=self.tmp / "no-credentials.csv", **options)

    def _ran(self) -> list:
        log = self.data / "ran.log"
        return log.read_text().split() if log.exists() else []

    def test_a_refused_openai_api_key_stops_a_monthly_run_before_the_download(self):
        """The labeling reads the retrieval that the download names, and the labeling becomes
        pending only once the download has named a new retrieval. Pre-checks that checked
        only the steps pending at the start would find the refused key only after the
        download and the sampling stage."""
        (self.data / "download.py").write_text(
            "import time\nopen('ran.log', 'a').write('download\\n')\n"
            "open('pointer.json', 'w').write(str(time.time_ns()))\n")
        (self.data / "classify.py").write_text(
            "DEFAULT_MODEL = 'model-of-the-script'\nopen('ran.log', 'a').write('labeling\\n')\n"
            "open('labels.csv', 'w').write(open('pointer.json').read())\n")
        steps = [Step("ghs_download", sys.executable, ["download.py"], produces=("pointer.json",),
                      completion=COMPLETION_OUTPUT, irreproducible=True, root=str(self.data)),
                 Step("repository_labeling", sys.executable, ["classify.py"],
                      produces=("labels.csv",), inputs=("pointer.json",),
                      reads=("pointer.json",), completion=COMPLETION_INPUTS,
                      env=("OPENAI_API_KEY",), root=str(self.data))]
        requests = []

        def accepted(key, model):
            requests.append(model)

        def revoked(key, model):
            requests.append(model)
            raise _mod.urllib.error.HTTPError(_mod.OPENAI_RESPONSES_URL, 401, "Unauthorized", {},
                                              io.BytesIO(b"{}"))

        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}):
            with mock.patch.object(_mod, "openai_response", accepted):
                self.assertEqual(self._run(steps), 0)
            self.assertEqual(self._ran(), ["download", "labeling"])
            with mock.patch.object(_mod, "openai_response", revoked):
                self.assertEqual(self._run(steps, from_name="ghs_download"), 1)
        self.assertEqual(self._ran(), ["download", "labeling"])
        self.assertEqual(requests, ["model-of-the-script", "model-of-the-script"])
        self.assertEqual(json.loads((self.tmp / "logs" / "state.json").read_text())["outcome"],
                         "pre_checks_failed")

    def test_new_labels_run_the_collection_in_the_same_run(self):
        """The cloning step is recorded by its tree and by its list, and the labeling and the
        selection rewrite the list. A run that computed the list's part of the record only
        before it began would skip the collection. It would then run the release over the new
        labels and the old collection and end in success."""
        (self.data / "labels.txt").write_text("o/a\n")
        (self.data / "label.py").write_text(
            "open('ran.log', 'a').write('labeling\\n')\n"
            "open('list.csv', 'w').write(open('labels.txt').read())\n")
        (self.data / "collect.py").write_text(
            "import os\nos.makedirs('collected', exist_ok=True)\n"
            "open('ran.log', 'a').write('collection\\n')\n"
            "open('collected/list.csv', 'w').write(open('list.csv').read())\n")
        (self.data / "release.py").write_text(
            "open('ran.log', 'a').write('release\\n')\n"
            "open('release.csv', 'w').write(open('collected/list.csv').read())\n")

        def listed(step):
            path = _mod.data_dir(step) / "list.csv"
            return [("the list", hashlib.sha256(path.read_bytes()).hexdigest()
                     if path.is_file() else None)]

        steps = [Step("labeling", sys.executable, ["label.py"], produces=("list.csv",),
                      inputs=("labels.txt",), completion=COMPLETION_INPUTS, root=str(self.data)),
                 Step("collection", sys.executable, ["collect.py"], produces=("collected",),
                      reads=("list.csv",), completion=COMPLETION_OUTPUT, inputs_of=listed,
                      root=str(self.data)),
                 Step("release", sys.executable, ["release.py"], produces=("release.csv",),
                      consumes=("collection",), inputs=("list.csv",),
                      completion=COMPLETION_INPUTS, root=str(self.data))]
        self.assertEqual(self._run(steps), 0)
        (self.data / "ran.log").unlink()
        (self.data / "labels.txt").write_text("o/a\no/b\n")
        self.assertEqual(self._run(steps), 0)
        self.assertEqual(self._ran(), ["labeling", "collection", "release"])
        self.assertEqual((self.data / "release.csv").read_text(), "o/a\no/b\n")

    def test_the_whole_chain_is_found_from_the_steps_that_are_pending(self):
        """A step may become pending when a step before it writes what it reads, and so may
        every step computed from it. A step recorded by its script decides nothing from what
        it reads, and a step before the writer reads the path before the writer runs. Neither
        may become pending."""
        steps = [Step("reader_before", "python3", ["b.py"], inputs=("a.csv",),
                      completion=COMPLETION_INPUTS),
                 Step("writer", "python3", ["w.py"], produces=("a.csv",)),
                 Step("by_inputs", "python3", ["i.py"], inputs=("other.csv|a.csv",),
                      produces=("b.csv",), completion=COMPLETION_INPUTS),
                 Step("by_script", "python3", ["s.py"], reads=("a.csv",)),
                 Step("computed", "python3", ["c.py"], consumes=("by_inputs",)),
                 Step("by_output", "python3", ["o.py"], produces=("tree",), reads=("b.csv",),
                      completion=COMPLETION_OUTPUT, inputs_of=lambda step: []),
                 Step("in_a_directory", "python3", ["d.py"], inputs=("tree/list.csv",),
                      completion=COMPLETION_INPUTS)]
        self.assertEqual(_mod.may_become_pending(steps, {"writer"}),
                         {"by_inputs", "computed", "by_output", "in_a_directory"})


class TestAdoption(_RunLogCase):
    """An irreproducible step with its output on disk is recorded rather than run."""

    def _snapshot(self, key="ghs_download", produces=("snapshot.json",)):
        return Step(key, sys.executable, ["-c", "pass"], produces=produces,
                     completion=COMPLETION_OUTPUT, irreproducible=True, root=str(self.tmp))

    def test_irreproducible_needs_the_output_rule(self):
        with self.assertRaises(ValueError):
            Step("x", "python3", ["x.py"], produces=("o",), irreproducible=True)

    def test_an_output_on_disk_is_adopted_and_logged(self):
        (self.tmp / "snapshot.json").write_text("{}")
        state = {"completed": {}}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertTrue(_mod.adopt_irreproducible([self._snapshot()], state, set()))
        self.assertTrue(state["completed"]["ghs_download"].startswith("output:"))
        self.assertIn("INFO runner: ghs_download is irreproducible, and its output is on disk",
                      err.getvalue())

    def test_nothing_is_adopted_over_a_missing_output_or_an_empty_one(self):
        """Adoption applies the check of `--mark-complete`, because it records a step as that
        flag does, without the flag and on every invocation of every runner. Adopting a
        zero-length file would record a download that was killed early, and every later run
        would skip the step and build from nothing."""
        state = {"completed": {}}
        with _quiet():
            self.assertFalse(_mod.adopt_irreproducible([self._snapshot()], state, set()))
        (self.tmp / "tree").mkdir()
        with _quiet():
            self.assertFalse(_mod.adopt_irreproducible(
                [self._snapshot(produces=("tree",))], state, set()))
        (self.tmp / "empty.json").write_text("", encoding="utf-8")
        with _quiet():
            self.assertFalse(_mod.adopt_irreproducible(
                [self._snapshot(produces=("empty.json",))], state, set()))
        self.assertEqual(state["completed"], {})

    def test_a_step_named_by_from_is_left_for_the_new_snapshot(self):
        (self.tmp / "snapshot.json").write_text("{}")
        state = {"completed": {}}
        with _quiet():
            self.assertFalse(_mod.adopt_irreproducible([self._snapshot()], state, {"ghs_download"}))
        self.assertEqual(state["completed"], {})

    def test_an_adopted_step_is_skipped_by_a_run(self):
        (self.tmp / "snapshot.json").write_text("{}")
        after = Step("after", sys.executable, ["-c", "open('after.txt', 'w').write('x')"],
                      produces=("after.txt",), consumes=("ghs_download",), root=str(self.tmp))
        _mod.MIN_FREE_DISK_GB = 0
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = _mod.run([self._snapshot(), after], argv=["x"], log_dir=self.tmp / "logs")
        self.assertEqual(code, 0)
        self.assertIn("INFO runner: ghs_download is irreproducible", err.getvalue())
        self.assertRegex(err.getvalue(),
                         r"(?m)^INFO runner: GHS download skipped, because it is already "
                         r"complete\. Recorded from its output on disk on \d{4}-\d\d-\d\d at "
                         r"\d\d:\d\d UTC( with the code of commit [0-9a-f]{7,8}"
                         r"( and uncommitted changes)?)?\. Run --from ghs_download to run it "
                         r"again\.$")
        self.assertTrue((self.tmp / "after.txt").is_file())
        recorded = json.loads((self.tmp / "logs" / "state.json").read_text())["completed"]
        self.assertEqual(set(recorded), {"ghs_download", "after"})

    # A snapshot step that writes part of its output and fails on its first run, and
    # writes the whole output and exits 0 on its second.
    _FAILS_ONCE = ("import pathlib, sys\n"
                   "p = pathlib.Path('snapshot.json')\n"
                   "first = not p.exists()\n"
                   "p.write_text('{\"part\": 1}' if first else '{}')\n"
                   "sys.exit(1 if first else 0)\n")

    def _fails_once(self):
        return Step("ghs_download", sys.executable, ["-c", self._FAILS_ONCE],
                     produces=("snapshot.json",), completion=COMPLETION_OUTPUT,
                     irreproducible=True, root=str(self.tmp))

    def _state(self) -> dict:
        return json.loads((self.tmp / "logs" / "state.json").read_text())

    def test_output_a_failed_run_left_is_not_adopted(self):
        """The classification's clone lists are written row by row, and a run that fails
        partway leaves output that is not empty. If the runner adopted that output as the
        whole snapshot, every later step would classify only the repositories that the
        failed run reached."""
        with _quiet():
            self.assertEqual(_mod.run([self._fails_once()], argv=["x"],
                                      log_dir=self.tmp / "logs", min_free_gb=0), 1)
        self.assertEqual(self._state()["unfinished"], ["ghs_download"])
        with _captured_stderr() as err:
            code = _mod.run([self._fails_once()], argv=["x"], log_dir=self.tmp / "logs",
                            min_free_gb=0)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("WARNING runner: ghs_download did not finish its last run",
                      err.getvalue())
        self.assertNotIn("its output is on disk", err.getvalue())
        self.assertEqual((self.tmp / "snapshot.json").read_text(), "{}")
        state = self._state()
        self.assertNotIn("unfinished", state)
        self.assertIn("ghs_download", state["completed"])

    def test_output_a_stopped_run_left_is_not_adopted(self):
        """A stop also leaves the step listed as unfinished, whatever the step returned."""
        original = _mod.run_step

        def stopping(step):
            ok = original(step)
            # A signal while the step runs, which `forward_stop` passes on to it.
            _mod._STOPPING = signal.SIGTERM
            _mod._FORWARDED = True
            return ok

        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        step = Step("ghs_download", sys.executable,
                      ["-c", "open('snapshot.json', 'w').write('{\"part\": 1}')"],
                      produces=("snapshot.json",), completion=COMPLETION_OUTPUT,
                      irreproducible=True, root=str(self.tmp))
        with _swap(_mod, "run_step", stopping), _quiet():
            code = _mod.run([step], argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0)
        self.assertEqual(code, 128 + signal.SIGTERM)
        _mod._STOPPING = 0
        state = self._state()
        self.assertEqual(state["unfinished"], ["ghs_download"])
        with _quiet():
            self.assertFalse(_mod.adopt_irreproducible([step], state, set()))
        self.assertEqual(state["completed"], {})

    def test_marking_the_step_complete_records_what_the_unfinished_run_left(self):
        (self.tmp / "snapshot.json").write_text('{"part": 1}')
        _mod.write_state({"completed": {}, "unfinished": ["ghs_download"]})
        with _quiet():
            self.assertEqual(_mod.mark_complete([self._snapshot()], "ghs_download",
                                                time.time(), ["x"]), 0)
        state = read_state()
        self.assertNotIn("unfinished", state)
        self.assertTrue(state["completed"]["ghs_download"].startswith("output:"))

    def test_a_record_whose_unfinished_list_is_not_step_names_is_refused(self):
        for value in ("ghs_download", [1], {"a": 1}):
            _mod.write_state({"completed": {}, "unfinished": value})
            with self.assertRaises(SystemExit):
                read_state()


class TestPerStepRequirements(_RunLogCase):
    """The pre-checks verify what a step declares that it needs before any step runs."""

    def _check(self, step) -> tuple:
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            ok = _mod.pre_checks([step], {step.key}, min_free_gb=0)
        return ok, err.getvalue()

    def test_a_reader_and_a_writer_in_different_roots_match(self):
        """`produces` and `reads` are each relative to their own step's root, and a comparison of
        the strings as written could only match inside one root. Over an empty data root, the
        pre-checks of `run_pipeline.py` would then refuse a run of the five stages because
        of a file that a pending earlier step writes."""
        writer = Step("w", sys.executable, ["w.py"], root="1_sampling",
                       produces=("no-such-frame-9f3.json",))
        reader = Step("r", sys.executable, ["r.py"], root="2_classification",
                       reads=("../1_sampling/no-such-frame-9f3.json",))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            ok = _mod.pre_checks([writer, reader], {"w", "r"}, min_free_gb=0)
        self.assertTrue(ok, err.getvalue())

        # The same reader is still refused when no pending step writes its input.
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            ok = _mod.pre_checks([writer, reader], {"r"}, min_free_gb=0)
        self.assertFalse(ok)
        self.assertIn("Input ../1_sampling/no-such-frame-9f3.json not found (required by r)",
                      err.getvalue())

    def test_a_zero_length_output_is_not_recorded_as_complete(self):
        """The check for empty output covers files as well as directories. Otherwise
        `: > ghs_complete.json` would be recorded, and every later run would skip the step and
        build from nothing, the failure that the refusal of an empty directory prevents. An
        extraction that fails partway leaves such a file."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        (tmp / "x.py").write_text("print('x')\n", encoding="utf-8")
        step = Step("x", sys.executable, ["x.py"], produces=("out.csv",),
                      completion=_mod.COMPLETION_OUTPUT, root=str(tmp))
        err = io.StringIO()
        (tmp / "out.csv").write_text("", encoding="utf-8")
        with (mock.patch.object(_mod, "CORE_STEPS", [step]),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err)):
            code = _mod.mark_complete([step], "x", 0.0)
        self.assertEqual(code, 1)
        self.assertIn("empty", err.getvalue())

        (tmp / "out.csv").write_text("a\n1\n", encoding="utf-8")
        with (mock.patch.object(_mod, "CORE_STEPS", [step]),
              contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            self.assertEqual(_mod.mark_complete([step], "x", 0.0), 0)

    def test_a_declared_binary_that_is_missing_fails_the_pre_checks(self):
        ok, err = self._check(Step("x", sys.executable, ["x.py"], binaries=("no-such-binary-9f3",)))
        self.assertFalse(ok)
        self.assertIn("'no-such-binary-9f3' not found on PATH (required by x)", err)
        self.assertTrue(self._check(Step("x", sys.executable, ["x.py"], binaries=("git",)))[0])

    def test_a_declared_import_that_fails_is_named(self):
        ok, err = self._check(Step("x", sys.executable, ["x.py"], imports=("no_such_module_9f3",)))
        self.assertFalse(ok)
        self.assertIn("cannot import no_such_module_9f3 (required by x). The import failed "
                      "(ModuleNotFoundError", err)
        self.assertTrue(self._check(Step("x", sys.executable, ["x.py"], imports=("json", "csv")))[0])

    def test_the_module_a_step_runs_is_imported_without_being_declared(self):
        """Every step of the package runs `-m agentcontext...`. The check exists for a virtual
        environment without the package installed, and it therefore imports the module that a
        step runs as well as what the step declares in `imports`."""
        ok, err = self._check(Step("x", sys.executable, ["-m", "agentcontext.no_such_stage_9f3"]))
        self.assertFalse(ok)
        self.assertIn("cannot import agentcontext.no_such_stage_9f3 (required by x)", err)
        self.assertTrue(self._check(Step("x", sys.executable, ["-m", "agentcontext.logs"]))[0])

    def test_a_declared_environment_variable_that_is_unset_is_named(self):
        saved = os.environ.pop("NO_SUCH_VAR_9F3", None)
        self.addCleanup(lambda: os.environ.update({"NO_SUCH_VAR_9F3": saved}) if saved else None)
        ok, err = self._check(Step("x", sys.executable, ["x.py"], env=("NO_SUCH_VAR_9F3",)))
        self.assertFalse(ok)
        self.assertIn("NO_SUCH_VAR_9F3 is unset (required by x)", err)
        os.environ["NO_SUCH_VAR_9F3"] = "set"
        self.addCleanup(os.environ.pop, "NO_SUCH_VAR_9F3", None)
        self.assertTrue(self._check(Step("x", sys.executable, ["x.py"], env=("NO_SUCH_VAR_9F3",)))[0])

    def test_an_environment_entry_with_alternatives_needs_one_of_them(self):
        """The GitHub query of the sampling stage reads GITHUB_TOKENS or GITHUB_TOKEN, and a
        machine may set either one. Requiring both would refuse a run that has only one."""
        for name in ("NO_SUCH_A_9F3", "NO_SUCH_B_9F3"):
            saved = os.environ.pop(name, None)
            self.addCleanup(lambda n=name, v=saved: os.environ.update({n: v}) if v else None)
        step = Step("x", sys.executable, ["x.py"], env=("NO_SUCH_A_9F3|NO_SUCH_B_9F3",))
        ok, err = self._check(step)
        self.assertFalse(ok)
        self.assertIn("NO_SUCH_A_9F3 and NO_SUCH_B_9F3 are unset (required by x)", err)
        os.environ["NO_SUCH_B_9F3"] = "set"
        self.addCleanup(os.environ.pop, "NO_SUCH_B_9F3", None)
        self.assertTrue(self._check(step)[0])

    def test_the_command_line_names_one_minimum_for_the_pre_checks_and_the_cloning_step(self):
        """`--min-free-gb` sets the minimum of free disk space for the pre-checks and the
        cloning step alike, so that a machine with less free space than the default can
        run."""
        asked = []

        def refuse(steps, pending, **kw):
            asked.append((kw.get("min_free_gb"),
                          next(s.args for s in steps if s.key == 'repository_cloning')))
            return False
        with mock.patch.object(_mod, "pre_checks", refuse), _quiet():
            code = _mod.main(["--min-free-gb", "20"])
        self.assertNotEqual(code, 0)
        floor, collect_args = asked[0]
        self.assertEqual(floor, 20)
        self.assertEqual(collect_args[collect_args.index("--min-free-gb") + 1], "20")

    def test_the_free_disk_minimum_is_a_parameter(self):
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            ok = _mod.pre_checks([Step("x", sys.executable, ["x.py"])], {"x"},
                                          min_free_gb=10 ** 9)
        self.assertFalse(ok)
        self.assertIn("under the minimum of 1000000000 GiB", err.getvalue())


class TestTheRunRecord(_RunLogCase):

    def test_the_record_names_the_step_interpreter_and_the_package_versions(self):
        """A stand-in for the step interpreter prints known versions, and the test compares
        the recorded versions with them. A comparison of the package names alone would pass a
        record of `null` for every package, which is the value for an interpreter that could
        not report its packages."""
        _mod.MIN_FREE_DISK_GB = 0
        stand_in = self.tmp / "python"
        stand_in.write_text("#!/bin/sh\n"
                            "echo '{\"pandas\": \"9.9.1\", \"numpy\": \"8.8.2\", \"scipy\": null}'\n")
        stand_in.chmod(0o755)
        step = Step("a", sys.executable, ["-c", "pass"], root=str(self.tmp))
        with _quiet(), _swap(_mod, "VENV_PYTHON", str(stand_in)), \
                _swap(_mod, "_STEP_PACKAGES", None):
            self.assertEqual(_mod.run([step], argv=["x"], log_dir=self.tmp / "logs"), 0)
        state = json.loads((self.tmp / "logs" / "state.json").read_text())
        self.assertEqual(state["step_interpreter"], str(stand_in))
        self.assertEqual(state["step_packages"],
                         {"pandas": "9.9.1", "numpy": "8.8.2", "scipy": None})


class TestTheStepReport(_RunLogCase):
    """A step's report is copied into the pipeline execution state. The GHS download records
    there whether it took a retrieval or fell back to an earlier one."""

    def _run(self, code: str):
        step = Step("s", sys.executable, ["-c", code], report="report.json",
                      root=str(self.tmp))
        with _quiet():
            result = _mod.run([step], argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0)
        state = json.loads((self.tmp / "logs" / "state.json").read_text())
        return result, state

    def test_the_report_a_step_writes_is_in_its_entry(self):
        code, state = self._run("import json; json.dump({'outcome': 'fallback'}, "
                                "open('report.json', 'w'))")
        self.assertEqual(code, 0)
        self.assertEqual(state["steps"][0]["report"], {"outcome": "fallback"})

    def test_a_report_an_earlier_run_left_is_not_recorded_for_this_one(self):
        (self.tmp / "report.json").write_text('{"outcome": "retrieved"}')
        code, state = self._run("pass")
        self.assertEqual(code, 1)
        self.assertEqual(state["steps"][0]["status"], "failed")
        self.assertNotIn("report", state["steps"][0])
        self.assertNotIn("s", state["completed"])

    def test_a_run_that_fails_keeps_the_report_of_the_last_run(self):
        """The release is recorded by its inputs, and the GHS download's report is one of
        them. A download that fails keeps the report of its last run beside the outputs of
        that run. A `--mark-complete` of the download, which the runner offers, then leaves
        the release a report to record. Without the report, the release would be recorded
        without that input and run again at every Start."""
        (self.tmp / "report.json").write_text('{"outcome": "retrieved"}')
        code, state = self._run("raise SystemExit(1)")
        self.assertEqual((code, state["steps"][0]["status"]), (1, "failed"))
        self.assertEqual(json.loads((self.tmp / "report.json").read_text()),
                         {"outcome": "retrieved"})
        (self.tmp / "r.py").write_text("pass\n")
        reader = Step("r", sys.executable, ["r.py"], inputs=("report.json",),
                      completion=COMPLETION_INPUTS, root=str(self.tmp))
        self.assertNotEqual(_mod.step_fingerprint(reader), "")

    def test_a_report_that_is_not_an_object_fails_the_step(self):
        code, state = self._run("open('report.json', 'w').write('[1]')")
        self.assertEqual(code, 1)
        self.assertEqual(state["steps"][0]["status"], "failed")


# ---------------------------------------------------------------------------
# invalidated_by / ancestors  (dependencies, not list positions)
# ---------------------------------------------------------------------------

class TestInvalidatedBy(unittest.TestCase):

    def test_a_chain_reaches_everything_downstream(self):
        self.assertEqual(invalidated_by(STEPS, "beta"), ["beta", "gamma"])

    def test_the_named_step_is_always_included(self):
        self.assertEqual(invalidated_by(STEPS, "gamma"), ["gamma"])

    def test_the_result_is_in_pipeline_order(self):
        self.assertEqual(invalidated_by(STEPS, "alpha"),
                         ["alpha", "beta", "gamma"])

    def test_a_sibling_that_reads_nothing_is_left_alone(self):
        # `right` runs after `left` but is not computed from it, and running `left` again
        # must not redo `right`.
        self.assertEqual(invalidated_by(BRANCHED, "left"), ["left", "leaf"])

    def test_a_shared_ancestor_reaches_both_branches(self):
        self.assertEqual(invalidated_by(BRANCHED, "root"),
                         ["root", "left", "right", "leaf"])

    def test_an_unknown_step_reaches_nothing(self):
        self.assertEqual(invalidated_by(STEPS, "delta"), [])

    def test_several_names_redo_each_and_what_is_computed_from_each(self):
        """`--from` takes several names, because a monthly run redoes the download and the
        cloning step, which no edge joins."""
        self.assertEqual(_mod.steps_to_redo(BRANCHED, ["left", "right"]), {"left", "right", "leaf"})
        self.assertEqual(_mod.steps_to_redo(BRANCHED, "right"), {"right"})
        self.assertEqual(_mod.steps_to_redo(BRANCHED, None), set())
        args = _mod.build_parser().parse_args(["--from", "left", "--from", "right"])
        self.assertEqual(args.from_name, ["left", "right"])

    def test_a_stage_of_the_pipeline_reaches_its_steps_and_what_is_computed_from_them(self):
        """The pipeline has five stages of several steps each. `--from collection` redoes
        every step of the collection, including a step that consumes nothing, and the later
        steps computed from them."""
        steps = [Step("download", "python3", ["d.py"], stage="sampling"),
                 Step("filter", "python3", ["f.py"], stage="sampling"),
                 Step("clone", "python3", ["c.py"], stage="collection"),
                 Step("check", "python3", ["k.py"], stage="collection"),
                 Step("table", "python3", ["t.py"], consumes=("check",), stage="analysis"),
                 Step("figure", "python3", ["g.py"], consumes=("filter",), stage="analysis")]
        self.assertEqual(invalidated_by(steps, "collection"), ["clone", "check", "table"])
        self.assertEqual(invalidated_by(steps, "sampling"), ["download", "filter", "figure"])
        self.assertEqual(invalidated_by(steps, "check"), ["check", "table"])


# ---------------------------------------------------------------------------
# modified_input_problems  (a later step modified an earlier step's output)
# ---------------------------------------------------------------------------

class TestMutatedInputProblems(_RunLogCase):

    def test_nothing_run_yet_is_no_problem(self):
        self.assertEqual(
            modified_input_problems(MUTATING, {"base", "mid", "late"}, self._completed().keys()), [])

    def test_the_mutator_having_run_makes_its_victims_dependents_a_problem(self):
        self._mark_done("base", "mid", "late")
        self.assertEqual(modified_input_problems(MUTATING, {"mid"}, self._completed().keys()),
                         [("mid", "base", "late")])

    def test_rebuilding_the_damaged_step_in_the_same_run_is_fine(self):
        # --from base runs base again, and mid then reads the tree that base produces. The
        # check has nothing to warn about.
        self._mark_done("base", "mid", "late")
        self.assertEqual(modified_input_problems(MUTATING, {"base", "mid"}, self._completed().keys()), [])

    def test_a_mutator_that_never_ran_damaged_nothing(self):
        self._mark_done("base", "mid")
        self.assertEqual(modified_input_problems(MUTATING, {"mid"}, self._completed().keys()), [])

    def test_a_step_that_does_not_read_the_damaged_output_is_unaffected(self):
        steps = MUTATING + [Step("aside", "python3", ["aside.py"])]
        self._mark_done("base", "mid", "late")
        self.assertEqual(modified_input_problems(steps, {"aside"}, self._completed().keys()), [])

    def test_only_direct_readers_are_reported(self):
        # `far` depends on base only through mid and never opens the output that `late`
        # modified. Running `far` alone again over the output that mid already wrote is
        # correct, and the check must not refuse that run.
        steps = MUTATING + [Step("far", "python3", ["far.py"], consumes=("mid",))]
        self._mark_done("base", "mid", "late", "far")
        self.assertEqual(modified_input_problems(steps, {"far"}, self._completed().keys()), [])

    def test_nothing_pending_is_no_problem(self):
        self._mark_done("base", "mid", "late")
        self.assertEqual(modified_input_problems(MUTATING, set(), self._completed().keys()), [])

    # `after` reads base's output after `late` has modified it, as the archive of collected
    # files reads the validated tree after the file history step has pruned it.
    AFTER = MUTATING + [Step("after", "python3", ["after.py"], consumes=("base",))]

    def test_a_reader_listed_after_the_mutator_reads_what_an_uninterrupted_run_gives_it(self):
        """A run that failed or was stopped between the mutator and the reader resumes at the
        reader. The reader then reads the output that the mutator left, as it does in a run
        that nothing interrupted."""
        self._mark_done("base", "mid", "late")
        self.assertEqual(modified_input_problems(self.AFTER, {"after"},
                                                 self._completed().keys()), [])

    def test_a_reader_after_a_mutator_that_runs_again_is_still_a_problem(self):
        """The mutator would modify an output that it has already modified, and the reader
        would then not read what an uninterrupted run gives it."""
        self._mark_done("base", "mid", "late")
        self.assertEqual(modified_input_problems(self.AFTER, {"late", "after"},
                                                 self._completed().keys()),
                         [("after", "base", "late")])

    def test_a_reader_listed_before_the_mutator_is_still_a_problem_beside_one_after_it(self):
        self._mark_done("base", "mid", "late", "after")
        self.assertEqual(modified_input_problems(self.AFTER, {"mid", "after"},
                                                 self._completed().keys()),
                         [("mid", "base", "late")])


# ---------------------------------------------------------------------------
# forward_stop  (a stop reaches the step, and ends the pipeline)
# ---------------------------------------------------------------------------

class _StopCase(unittest.TestCase):
    """Base class that restores the module's stop globals after every test."""

    def setUp(self):
        self._saved = (_mod._CHILD, _mod._STOPPING)
        _mod._CHILD, _mod._STOPPING = None, 0

    def tearDown(self):
        _mod._CHILD, _mod._STOPPING = self._saved

    def _sleeper(self) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(self._reap, proc)
        return proc

    @staticmethod
    def _reap(proc) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait()


class TestForwardStop(_StopCase):

    def test_the_first_signal_with_no_step_running_only_records_it(self):
        with _captured_stderr():
            forward_stop(signal.SIGTERM, None)
        self.assertEqual(_mod._STOPPING, signal.SIGTERM)

    def test_it_logs_that_the_run_ends_before_the_next_step(self):
        with _captured_stderr() as err:
            forward_stop(signal.SIGTERM, None)
        self.assertIn("Stopping before the next step", err.getvalue())

    def test_a_second_signal_with_no_step_running_exits(self):
        # A second signal means that the operator insists. If the handler returned again,
        # the operator's only way out would be kill -9.
        with _captured_stderr():
            forward_stop(signal.SIGINT, None)
            with self.assertRaises(SystemExit) as cm:
                forward_stop(signal.SIGINT, None)
        self.assertEqual(cm.exception.code, 128 + signal.SIGINT)

    def test_it_reaches_a_running_step(self):
        # Under `kill`, a systemd stop or a `docker stop`, only this process
        # is signaled, and a step that the stop does not reach is orphaned and
        # goes on cloning.
        proc = self._sleeper()
        _mod._CHILD = proc
        with _captured_stderr():
            forward_stop(signal.SIGTERM, None)
        self.assertEqual(proc.wait(timeout=10), -signal.SIGTERM)

    def test_signaling_a_running_step_does_not_exit_the_runner(self):
        # forward_stop has to return, so that the loop can end the run cleanly and record
        # what completed. Raising here would skip that record.
        proc = self._sleeper()
        _mod._CHILD = proc
        with _captured_stderr():
            forward_stop(signal.SIGTERM, None)
            forward_stop(signal.SIGTERM, None)   # must not raise
        self.assertEqual(_mod._STOPPING, signal.SIGTERM)

    def test_an_already_exited_step_is_not_signaled(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        _mod._CHILD = proc
        with _captured_stderr() as err:
            forward_stop(signal.SIGTERM, None)
        self.assertIn("while no step was running", err.getvalue())

    def test_the_recorded_signal_is_the_one_that_arrived(self):
        with _captured_stderr():
            forward_stop(signal.SIGINT, None)
        self.assertEqual(_mod._STOPPING, signal.SIGINT)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestAStopSentToARealRunnerAlone(unittest.TestCase):
    """A runner process in a session of its own, signaled while a real step sleeps, as
    `kill`, a systemd stop and `docker stop` signal it.

    The cases above set `_CHILD` by hand, and `TestMain` sets the stop flag from a patched
    function. Only this case checks that `run_step` publishes the process that it starts.
    Without its `global _CHILD`, a runner stopped this way would wait until the whole step
    ends."""

    def test_the_step_ends_and_the_run_exits_on_the_signal(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        pid_file = tmp / "stage.pid"
        step_code = ("import os, time\n"
                      f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
                      "time.sleep(60)\n")
        runner_code = (
            "import sys\n"
            "import agentcontext.runner as r\n"
            f"step = r.Step('sleepy', sys.executable, ['-c', {step_code!r}], root={str(tmp)!r})\n"
            f"sys.exit(r.run([step], argv=['test'], log_dir={str(tmp / 'logs')!r}, "
            "min_free_gb=0))\n")
        # The package that this suite imported, from whichever checkout it comes.
        package_root = str(Path(_mod.__file__).resolve().parents[1])
        env = {**os.environ,
               "PYTHONPATH": os.pathsep.join(filter(None, [package_root,
                                                           os.environ.get("PYTHONPATH")]))}
        runner = subprocess.Popen([sys.executable, "-c", runner_code], cwd=tmp, env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                  start_new_session=True)
        self.addCleanup(runner.wait)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        deadline = time.monotonic() + 20
        while not pid_file.exists() or not pid_file.read_text():
            self.assertIsNone(runner.poll(), "the runner exited before the step started")
            self.assertLess(time.monotonic(), deadline, "the step never started")
            time.sleep(0.05)
        step_pid = int(pid_file.read_text())
        self.addCleanup(lambda: _alive(step_pid) and os.kill(step_pid, signal.SIGKILL))

        os.kill(runner.pid, signal.SIGTERM)
        try:
            code = runner.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.fail("the runner was still waiting on its step 10 s after the signal")
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertFalse(_alive(step_pid), "the step outlived the runner")
        state = json.loads((tmp / "logs" / "state.json").read_text())
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual([(s["name"], s["status"]) for s in state["steps"]],
                         [("sleepy", "interrupted")])
        self.assertEqual(state["completed"], {})


class TestTheMutatorIsRememberedHoweverItIsForgotten(_RunLogCase):
    """The check for modified inputs tests whether a step ever ran, not whether its result
    counts.

    Two mechanisms drop a step from the set of completed steps that the runner uses, and
    the modification that a mutator made remains after both. `--from` drops a step because
    the operator requested a redo, and `completed_steps` drops it because a fingerprint
    changed. Given the set after the second drop, the check would miss everything that the
    second drop reaches, that is, every edit at or upstream of the mutator.
    """

    def setUp(self):
        super().setUp()
        self._orig_dir = _mod.STAGE_DIR
        _mod.STAGE_DIR = self.tmp
        for name in ("base.py", "mid.py", "late.py"):
            (self.tmp / name).write_text("original")
        # The chain models the file history step, as MUTATING does.
        self.chain = [
            Step("base", "python3", ["base.py"]),
            Step("mid", "python3", ["mid.py"], consumes=("base",)),
            Step("late", "python3", ["late.py"], consumes=("mid",), mutates=("base",)),
        ]
        self.recorded = {s.key for s in self.chain}

    def tearDown(self):
        _mod.STAGE_DIR = self._orig_dir
        super().tearDown()

    def _problems(self, pending):
        return modified_input_problems(self.chain, pending, self.recorded)

    def test_a_pending_reader_of_the_damaged_output_is_a_problem(self):
        self.assertEqual(self._problems({"mid", "late"}),
                         [("mid", "base", "late")])

    def test_it_survives_the_mutator_being_dropped_for_a_changed_fingerprint(self):
        # The drop that `--from` does not make. Editing mid's script drops mid and late,
        # because late is computed from mid. Given the set that the fingerprints filtered,
        # the check would conclude that late had never run.
        state = {"completed": {s.key: script_fingerprint(s) for s in self.chain}}
        (self.tmp / "mid.py").write_text("edited")
        with _quiet():
            done = completed_steps(self.chain, state)
        self.assertNotIn("late", done)
        pending = pending_step_names(self.chain, done)
        self.assertEqual(modified_input_problems(self.chain, pending, done), [])
        self.assertEqual(
            modified_input_problems(self.chain, pending, self.recorded),
            [("mid", "base", "late")])

    def test_rebuilding_the_damaged_step_in_the_same_run_is_fine(self):
        self.assertEqual(self._problems({"base", "mid", "late"}), [])

    def test_a_mutator_that_never_ran_damaged_nothing(self):
        self.assertEqual(
            modified_input_problems(self.chain, {"mid"}, {"base"}), [])


class TestAStepThatDidNotFinishAfterItsProducerRanAgain(_RunLogCase):
    """b reads a, and c reads b. After a changes, a run may record a again and then fail in b
    or be stopped in it. That run must not leave b's old record, which matches b's unchanged
    script. Otherwise the next invocation would skip b and c and exit 0 over the outputs of
    the old a. In the container, a Start would then skip the attribution check that stopped
    the previous run."""

    SCRIPTS = {
        "a.py": "import os; open('a.csv', 'w').write(os.environ.get('A', 'v1') + '\\n')\n",
        "b.py": ("import os, signal, sys, time\n"
                 "open('ran.log', 'a').write('b\\n')\n"
                 "if os.environ.get('B_FAILS'): sys.exit(5)\n"
                 "if os.environ.get('B_STOPPED'):\n"
                 "    os.kill(os.getppid(), signal.SIGTERM)\n"
                 "    time.sleep(30)\n"
                 "open('b.csv', 'w').write(open('a.csv').read())\n"),
        "c.py": ("open('ran.log', 'a').write('c\\n')\n"
                 "open('c.csv', 'w').write(open('b.csv').read())\n"),
    }

    def setUp(self):
        super().setUp()
        for name, text in self.SCRIPTS.items():
            (self.tmp / name).write_text(text)
        self.steps = [
            Step("a", sys.executable, ["a.py"], produces=("a.csv",), root=str(self.tmp)),
            Step("b", sys.executable, ["b.py"], produces=("b.csv",), consumes=("a",),
                 root=str(self.tmp)),
            Step("c", sys.executable, ["c.py"], produces=("c.csv",), consumes=("b",),
                 root=str(self.tmp))]
        self.addCleanup(os.environ.pop, "B_FAILS", None)

    def _run(self) -> int:
        with _quiet():
            return _mod.run(self.steps, argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0)

    def _outputs(self) -> list:
        return [(self.tmp / f"{name}.csv").read_text().strip() for name in "abc"]

    def test_a_failure_in_the_reader_leaves_it_and_what_reads_it_to_the_next_start(self):
        self.assertEqual(self._run(), 0)
        (self.tmp / "a.py").write_text(self.SCRIPTS["a.py"].replace("'v1'", "'v2'"))
        os.environ["B_FAILS"] = "1"
        self.assertEqual(self._run(), 1)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _mod.completed_steps(self.steps, read_state())
        self.assertIn("b did not finish its last run after one of its inputs changed", err.getvalue())
        completed = read_state()["completed"]
        self.assertTrue(completed["b"].startswith(_mod.STALE), completed)
        self.assertTrue(completed["c"].startswith(_mod.STALE), completed)
        del os.environ["B_FAILS"]
        (self.tmp / "ran.log").write_text("")
        self.assertEqual(self._run(), 0)
        self.assertEqual((self.tmp / "ran.log").read_text().split(), ["b", "c"])
        self.assertEqual(self._outputs(), ["v2", "v2", "v2"])
        self.assertFalse(any(v.startswith(_mod.STALE) for v in read_state()["completed"].values()))

    def test_a_stop_in_the_reader_leaves_it_to_the_next_start(self):
        """The runner runs in a process of its own, because the stop is a SIGTERM to the
        runner."""
        program = (
            "import os, sys\n"
            "from pathlib import Path\n"
            "from agentcontext import runner\n"
            "from agentcontext.runner import Step\n"
            "tmp = Path(sys.argv[1])\n"
            "steps = [Step('a', sys.executable, ['a.py'], produces=('a.csv',), root=str(tmp)),\n"
            "         Step('b', sys.executable, ['b.py'], produces=('b.csv',), consumes=('a',), root=str(tmp)),\n"
            "         Step('c', sys.executable, ['c.py'], produces=('c.csv',), consumes=('b',), root=str(tmp))]\n"
            "raise SystemExit(runner.run(steps, argv=['x'], log_dir=tmp / 'logs', min_free_gb=0))\n")
        (self.tmp / "start.py").write_text(program)

        def start(**env) -> int:
            return subprocess.run([sys.executable, str(self.tmp / "start.py"), str(self.tmp)],
                                  cwd=str(self.tmp), capture_output=True, timeout=120,
                                  env={**os.environ, "PYTHONPATH": str(REPO_ROOT), **env}).returncode

        self.assertEqual(start(), 0)
        (self.tmp / "a.py").write_text(self.SCRIPTS["a.py"].replace("'v1'", "'v2'"))
        self.assertEqual(start(B_STOPPED="1"), 128 + signal.SIGTERM)
        (self.tmp / "ran.log").write_text("")
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "ran.log").read_text().split(), ["b", "c"])
        self.assertEqual(self._outputs(), ["v2", "v2", "v2"])


class TestTheOutcomeOfTheLastRun(_RunLogCase):
    """The container's entrypoint decides from the last invocation that ran steps whether a
    Start begins the next monthly run. An invocation that ran no step leaves that outcome
    unchanged."""

    def test_a_start_that_ran_no_step_keeps_the_outcome_of_the_run_before_it(self):
        state = {"completed": {}}
        with _quiet():
            _mod.write_execution_state(state, [], "success", time.time(), ["x"])
            for outcome in _mod.NO_STEP_OUTCOMES:
                _mod.write_execution_state(state, [], outcome, time.time(), ["x"])
                self.assertEqual((read_state()["outcome"], read_state()[_mod.LAST_RUN_OUTCOME]),
                                 (outcome, "success"))
            _mod.write_execution_state(state, [], "running", time.time(), ["x"])
        self.assertEqual(read_state()[_mod.LAST_RUN_OUTCOME], "running")

    def test_a_wait_that_a_kill_left_in_the_state_ends_with_the_next_start(self):
        """A kill of the runner while the labeling waited for credit leaves the wait in the
        pipeline execution state. The next start writes the state before its first step, where
        no step waits, and the wait is gone from then on."""
        state = {"completed": {}}
        with _quiet():
            _mod.write_execution_state(state, [], "running", time.time(), ["x"],
                                       running_step="repository_labeling")
            _mod.record_step_status(state, {"waiting_for": "credit", "service": "OpenRouter"})
            self.assertIn(_mod.RUNNING_STEP_STATUS, read_state())
            # The next start reads the state as the kill left it.
            state = read_state()
            _mod.write_execution_state(state, [], "running", time.time(), ["x"],
                                       running_step="repository_labeling")
        self.assertNotIn(_mod.RUNNING_STEP_STATUS, read_state())

    def test_a_start_that_ran_no_step_keeps_the_record_of_a_start_that_a_kill_ended(self):
        """A kill inside the first step of an invocation leaves the step's name and the
        invocation's step results as the only record that the step after the release had not
        finished. A refused or stopped invocation after it must keep them. Otherwise the
        container's entrypoint would hand the data root over as complete while it holds part
        of a collection."""
        state = {"completed": {}}
        released = [{"name": "release", "status": "ok", "duration_seconds": 0}]
        with _quiet():
            _mod.write_execution_state(state, released, "success", time.time(), ["x"])
            _mod.write_execution_state(state, [], "running", time.time(), ["x"],
                                       running_step="repository_cloning")
            for outcome in _mod.NO_STEP_OUTCOMES:
                _mod.write_execution_state(state, [], outcome, time.time(), ["x"])
                self.assertEqual((read_state()[_mod.RUNNING_STEP], read_state()["steps"]),
                                 ("repository_cloning", []))
            # A check that stops an invocation before its first step writes `failed`, with
            # step results in which no step ran.
            skipped = [{"name": "ghs_download", "status": "skipped", "duration_seconds": 0}]
            _mod.write_execution_state(state, skipped, "running", time.time(), ["x"])
            _mod.write_execution_state(state, skipped, "failed", time.time(), ["x"])
            self.assertEqual(read_state()[_mod.RUNNING_STEP], "repository_cloning")
            self.assertEqual(read_state()[_mod.RUNNING_STEP_RESULTS], [],
                             "the killed start's own step results before its first step")
            _mod.write_execution_state(state, [{"name": "repository_cloning", "status": "ok"}],
                                       "running", time.time(), ["x"])
        self.assertNotIn(_mod.RUNNING_STEP, read_state())
        self.assertNotIn(_mod.RUNNING_STEP_STARTED_AT, read_state())
        self.assertNotIn(_mod.RUNNING_STEP_RESULTS, read_state())

    def test_only_a_start_that_ran_a_step_records_when_it_finished(self):
        """The entrypoint records this time with the handover of a completed run. An
        invocation that skipped every step or ran none writes the pipeline execution state
        too. It must not make the handover of the run that completed the steps look older than
        the data root."""
        state = {"completed": {}}
        with _quiet():
            _mod.write_execution_state(state, [{"name": "a", "status": "ok"},
                                               {"name": "b", "status": "skipped"}],
                                       "success", time.time(), ["x"])
            finished = read_state()[_mod.LAST_STEPS_FINISHED_AT]
            self.assertEqual(finished, read_state()["finished_at"])
            _mod.write_execution_state(state, [{"name": "a", "status": "skipped"}], "success",
                                       time.time(), ["x"])
            for outcome in _mod.NO_STEP_OUTCOMES:
                _mod.write_execution_state(state, [], outcome, time.time(), ["x"])
            self.assertEqual(read_state()[_mod.LAST_STEPS_FINISHED_AT], finished)
            _mod.write_execution_state(state, [{"name": "a", "status": "failed"}], "failed",
                                       time.time(), ["x"])
        self.assertNotEqual(read_state()[_mod.LAST_STEPS_FINISHED_AT], finished)

    def test_only_a_start_that_ran_a_step_records_its_step_results_as_the_last_runs(self):
        """The entrypoint reads from these step results which step failed the last run. A
        refusal, a `--mark-complete` and an invocation that skipped every step write step
        results in which no step ran. So does a check that stops the run before its first step
        and writes the outcome `failed`. None of them may hide that the run before them failed
        in the analysis stage."""
        state = {"completed": {}}
        failed = [{"name": "release", "status": "ok"}, {"name": "rq1", "status": "failed"}]
        skipped = [{"name": "release", "status": "skipped"}]
        with _quiet():
            _mod.write_execution_state(state, failed, "failed", time.time(), ["x"])
            for outcome in _mod.NO_STEP_OUTCOMES:
                _mod.write_execution_state(state, [], outcome, time.time(), ["x"])
            _mod.write_execution_state(state, skipped, "success", time.time(), ["x"])
            _mod.write_execution_state(state, skipped, "failed", time.time(), ["x"])
            self.assertEqual(read_state()["steps"], skipped)
            self.assertEqual(read_state()[_mod.LAST_RUN_STEPS], failed)
            ran = [{"name": "ghs_download", "status": "ok"}]
            _mod.write_execution_state(state, ran, "running", time.time(), ["x"])
        self.assertEqual(read_state()[_mod.LAST_RUN_STEPS], ran)

    def test_a_kill_inside_the_first_step_that_runs_leaves_the_outcome_running(self):
        """A reboot that kills the first step that a Start runs, such as the cloning step after
        an image that changed git, also kills the runner, and no handler writes the outcome. If
        the state kept the outcome of the invocation before, `success`, the entrypoint would not
        resume the run at the next Start. It would also not begin the next monthly run when the
        resumed run completes. The runner runs in a process of its own, because the kill is a
        SIGKILL to the runner."""
        (self.tmp / "a.py").write_text("open('a.csv', 'w').write('a\\n')\n")
        (self.tmp / "b.py").write_text("import os, signal\n"
                                       "os.kill(os.getppid(), signal.SIGKILL)\n")
        program = (
            "import sys\n"
            "from pathlib import Path\n"
            "from agentcontext import runner\n"
            "from agentcontext.runner import Step\n"
            "tmp = Path(sys.argv[1])\n"
            "steps = [Step('a', sys.executable, ['a.py'], produces=('a.csv',), root=str(tmp))]\n"
            "def check(step):\n"
            "    raise SystemExit('git could not start')\n"
            "if sys.argv[2:] == ['b']:\n"
            "    steps.append(Step('b', sys.executable, ['b.py'], consumes=('a',),\n"
            "                      root=str(tmp)))\n"
            "if sys.argv[2:] == ['b', 'check']:\n"
            "    steps.append(Step('b', sys.executable, ['b.py'], consumes=('a',),\n"
            "                      produces=('b.csv',), root=str(tmp), completion='output',\n"
            "                      inputs_of=check))\n"
            "raise SystemExit(runner.run(steps, argv=['x'], log_dir=tmp / 'run_logs',\n"
            "                            min_free_gb=0))\n")
        (self.tmp / "start.py").write_text(program)

        def start(*steps) -> int:
            return subprocess.run([sys.executable, str(self.tmp / "start.py"), str(self.tmp),
                                   *steps], cwd=str(self.tmp), capture_output=True, timeout=120,
                                  env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}).returncode

        self.assertEqual(start(), 0)
        finished = read_state()[_mod.LAST_STEPS_FINISHED_AT]
        # A step that ended is no longer named as the step that runs.
        self.assertNotIn(_mod.RUNNING_STEP, read_state())
        self.assertEqual(start("b"), -signal.SIGKILL)
        state = read_state()
        self.assertEqual((state["outcome"], state[_mod.LAST_RUN_OUTCOME]), ("running", "running"))
        self.assertEqual([(step["name"], step["status"]) for step in state["steps"]],
                         [("a", "skipped")])
        # The state names the step that the kill ended. The step results cannot show it,
        # because the runner records a step when the step ends. The entrypoint reads the name
        # to decide whether a `--fresh` of that step took effect.
        self.assertEqual(state[_mod.RUNNING_STEP], "b")
        # Because no step of the invocation finished, a handover of the run before still
        # contains the data root as it is.
        self.assertEqual(state[_mod.LAST_STEPS_FINISHED_AT], finished)
        # A check can stop the next invocation before its first step, for example when git
        # cannot start while the runner reads the inputs of the step. The invocation then
        # ends `failed`, and the state keeps the step's name with the start time of the
        # invocation that wrote it.
        self.assertEqual(start("b", "check"), 1)
        after = read_state()
        self.assertEqual((after["outcome"], after[_mod.RUNNING_STEP],
                          after[_mod.RUNNING_STEP_STARTED_AT]),
                         ("failed", "b", state["started_at"]))
        self.assertNotEqual(after["started_at"], state["started_at"])

    def _start_with_a_kill_after_the_step(self, completion: str = COMPLETION_OUTPUT,
                                          irreproducible: bool = True):
        """Return a function that starts a pipeline of one step in a process of its own,
        because the kill is a SIGKILL to the runner.

        The step is recorded by `completion`, by default by its output, and counts its runs in
        `counter`. The argument of the returned function selects what the invocation does:

        - `kill`: the runner ends itself with SIGKILL when it computes the step's fingerprint
          after the step exited 0.
        - `stop`: the runner sends itself two stop signals when it computes a fingerprint.
        - `refuse`: the pre-checks refuse the invocation.
        - `mark`: the invocation marks the step complete.

        The digest of the step's list is the text of the file `digest` when the file is
        there."""
        (self.tmp / "a.py").write_text("open('counter', 'a').write('x')\n"
                                       "open('a.csv', 'w').write('a\\n')\n")
        program = (
            "import os, signal, sys\n"
            "from pathlib import Path\n"
            "from agentcontext import runner\n"
            "from agentcontext.runner import Step\n"
            "tmp = Path(sys.argv[1])\n"
            "if sys.argv[2:] == ['kill']:\n"
            "    real = runner.step_fingerprint\n"
            "    def killed(step, *args, **kwargs):\n"
            "        if (tmp / 'counter').exists():\n"
            "            os.kill(os.getpid(), signal.SIGKILL)\n"
            "        return real(step, *args, **kwargs)\n"
            "    runner.step_fingerprint = killed\n"
            "if sys.argv[2:] == ['stop']:\n"
            "    real = runner.step_fingerprint\n"
            "    def stopped(step, *args, **kwargs):\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "        os.kill(os.getpid(), signal.SIGTERM)\n"
            "        return real(step, *args, **kwargs)\n"
            "    runner.step_fingerprint = stopped\n"
            "def digest(step):\n"
            "    path = tmp / 'digest'\n"
            "    return [('the list', path.read_text() if path.exists() else 'digest')]\n"
            "steps = [Step('a', sys.executable, ['a.py'], produces=('a.csv',), root=str(tmp),\n"
            f"              completion={completion!r}, irreproducible={irreproducible!r},\n"
            f"              inputs_of=digest if {completion!r} == 'output' else None)]\n"
            "free = 10 ** 9 if sys.argv[2:] == ['refuse'] else 0\n"
            "mark = 'a' if sys.argv[2:] == ['mark'] else None\n"
            "raise SystemExit(runner.run(steps, argv=['x'], log_dir=tmp / 'run_logs',\n"
            "                            min_free_gb=free, mark=mark))\n")
        (self.tmp / "start.py").write_text(program)

        def start(*args) -> int:
            return subprocess.run([sys.executable, str(self.tmp / "start.py"), str(self.tmp),
                                   *args], cwd=str(self.tmp), capture_output=True, timeout=120,
                                  env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}).returncode

        return start

    def test_a_step_that_exited_0_before_a_kill_is_recorded_and_not_run_again(self):
        """The runner records a step after it computes the fingerprint of the step's output,
        which reads the whole collected tree. The cloning step closes its collection before it
        exits 0. Without the record of the exit, a reboot in between would make the next Start
        run the step again, which continues the collection that ended, and every step after
        it."""
        start = self._start_with_a_kill_after_the_step()
        self.assertEqual(start("kill"), -signal.SIGKILL)
        self.assertNotIn("a", read_state().get("completed", {}))
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "counter").read_text(), "x")
        state = read_state()
        self.assertIn("a", state["completed"])
        self.assertNotIn(_mod.EXITED, state)
        self.assertEqual(state.get(_mod.UNFINISHED, []), [])

    def test_a_step_recorded_after_the_kill_names_the_invocation_in_which_it_exited(self):
        """The invocation that records the step from its exit runs later, possibly with the
        code of another image. The record names when the step exited and the code that ran
        it, which the exit record holds."""
        start = self._start_with_a_kill_after_the_step()
        self.assertEqual(start("kill"), -signal.SIGKILL)
        state = read_state()
        state[_mod.EXITED].update(finished_at="2026-10-27T18:41:07+00:00",
                                  code={"git_sha": "0123456789abcdef0123456789abcdef01234567",
                                        "git_dirty": False})
        write_state(state)
        self.assertEqual(start(), 0)
        self.assertEqual(_mod.completion_origin(read_state(), "a"),
                         "Finished on 2026-10-27 at 18:41 UTC with the code of commit 01234567. "
                         "Its log is run_logs/a.log.")

    def test_a_step_recorded_after_the_kill_counts_as_a_step_that_the_start_ran(self):
        """The container packs a handover when the pipeline execution state names a time of
        the last steps that is later than the last handover that it packed. It also reads the
        step that a kill left running. A step recorded from its exit 0, such as the release
        after a reboot, changed the data root. The invocation that records the step names its
        time and no running step. Otherwise the new release would never be handed over."""
        start = self._start_with_a_kill_after_the_step(COMPLETION_SCRIPT, irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        self.assertEqual(read_state()[_mod.RUNNING_STEP], "a")
        self.assertEqual(start(), 0)
        state = read_state()
        self.assertEqual((self.tmp / "counter").read_text(), "x")
        self.assertEqual([(step["name"], step["status"]) for step in state["steps"]],
                         [("a", "ok")])
        self.assertIn(_mod.LAST_STEPS_FINISHED_AT, state)
        self.assertNotIn(_mod.RUNNING_STEP, state)

    def test_a_start_that_ends_before_its_loop_leaves_the_result_to_the_next_start(self):
        """The invocation that records the step from its exit can end before its loop reaches the
        step, for example when the pre-checks refuse the invocation. The next invocation
        then gives the step its result. Otherwise the handover of the step's output would never be
        packed."""
        start = self._start_with_a_kill_after_the_step(COMPLETION_SCRIPT, irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        self.assertEqual(start("refuse"), 1)
        self.assertEqual(read_state()[_mod.RECORDED_FROM_EXIT], ["a"])
        self.assertEqual(start(), 0)
        state = read_state()
        self.assertEqual((self.tmp / "counter").read_text(), "x")
        self.assertEqual([(step["name"], step["status"]) for step in state["steps"]],
                         [("a", "ok")])
        self.assertIn(_mod.LAST_STEPS_FINISHED_AT, state)
        self.assertNotIn(_mod.RUNNING_STEP, state)
        self.assertNotIn(_mod.RECORDED_FROM_EXIT, state)

    def test_a_second_stop_while_a_start_records_the_step_keeps_the_record_of_its_exit(self):
        """The fingerprint of the output reads the whole collected tree, and at the first stop
        signal the runner logs that a second signal exits at once. If the runner removed the
        record of the exit before it computed the fingerprint, the state that the stop writes
        would lack that record. The next invocation would then run the cloning step again, which
        continues the collection that ended, and every step after it."""
        start = self._start_with_a_kill_after_the_step(irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        self.assertEqual(start("stop"), 128 + signal.SIGTERM)
        self.assertIn(_mod.EXITED, read_state())
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "counter").read_text(), "x")
        self.assertNotIn(_mod.EXITED, read_state())

    def test_a_step_marked_complete_after_the_kill_keeps_the_mark(self):
        """The record of the exit holds the inputs of the killed run. If that record stayed
        beside the mark, the next invocation would record the step from it over the mark, find
        that the inputs changed and run the step again."""
        start = self._start_with_a_kill_after_the_step(irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        (self.tmp / "digest").write_text("another list")
        self.assertEqual(start("mark"), 0)
        marked = read_state()["completed"]["a"]
        self.assertNotIn(_mod.EXITED, read_state())
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "counter").read_text(), "x")
        self.assertEqual(read_state()["completed"]["a"], marked)

    def test_a_step_recorded_by_its_output_is_recorded_after_the_kill_under_other_code(self):
        """The record of a step recorded by its output holds its output and its inputs, and no
        code. The runner keeps such a step complete under a new image unless its inputs
        changed. After a kill, the cloning step has already closed its collection. If it ran
        again under an image that changed no input of the collection, it would continue the
        collection that ended, and every step after it would run again. Like the cloning step,
        the step here is not irreproducible."""
        start = self._start_with_a_kill_after_the_step(irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        (self.tmp / "a.py").write_text("open('counter', 'a').write('y')\n"
                                       "open('a.csv', 'w').write('b\\n')\n")
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "counter").read_text(), "x")
        self.assertIn("a", read_state()["completed"])

    def test_a_step_recorded_by_its_output_runs_again_after_the_kill_under_other_inputs(self):
        start = self._start_with_a_kill_after_the_step(irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        (self.tmp / "digest").write_text("another list")
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "counter").read_text(), "xx")

    def test_a_step_whose_code_changed_after_the_kill_runs_again(self):
        """The record of a step recorded by its script would otherwise cover output that the
        new code never wrote, for example when an image that changed the step is imported
        before the next Start."""
        start = self._start_with_a_kill_after_the_step(COMPLETION_SCRIPT, irreproducible=False)
        self.assertEqual(start("kill"), -signal.SIGKILL)
        (self.tmp / "a.py").write_text("open('counter', 'a').write('y')\n"
                                       "open('a.csv', 'w').write('b\\n')\n")
        self.assertEqual(start(), 0)
        self.assertEqual((self.tmp / "counter").read_text(), "xy")
        self.assertEqual((self.tmp / "a.csv").read_text(), "b\n")
        self.assertIn("a", read_state()["completed"])

    def test_the_outputs_reach_the_disk_before_the_record(self):
        """Steps write their outputs by a rename without a sync. If the record reached the
        disk first, a power loss could leave a record of the step over an empty or an earlier
        file. The runner syncs the report and each declared output, including a declared
        directory with everything below it, even when a sync of the whole file system does not
        return."""
        script = ("import os\n"
                  "os.makedirs('tree/sub', exist_ok=True)\n"
                  "open('tree/sub/b.csv', 'w').write('b\\n')\n"
                  "open('a.csv', 'w').write('a\\n')\n"
                  "open('a.json', 'w').write('{}')\n")
        steps = [Step("a", sys.executable, ["-c", script], produces=("a.csv", "tree"),
                      report="a.json", root=str(self.tmp), completion=COMPLETION_OUTPUT)]
        synced, at_record, fsync, write_state = set(), [], os.fsync, _mod.write_state

        def recorded_sync(fd):
            synced.add((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return fsync(fd)

        def recorded(state):
            names = (state.get(_mod.EXITED) or {}).get("step") == "a" \
                or "a" in state.get("completed", {})
            if names and not at_record:
                at_record.append(set(synced))
            return write_state(state)

        with file_system_sync_never_returns(), \
                mock.patch.object(os, "fsync", recorded_sync), \
                mock.patch.object(_mod, "write_state", recorded), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_mod.run(steps, argv=["x"], min_free_gb=0), 0)
        self.assertTrue(at_record)
        for name in ("a.csv", "a.json", "tree", "tree/sub", "tree/sub/b.csv"):
            path = self.tmp / name
            with self.subTest(path=name):
                self.assertIn((path.stat().st_dev, path.stat().st_ino), at_record[0])

    def test_a_stop_after_the_step_exited_records_the_step(self):
        """A stop that arrives after the step has exited 0 and before `run_step` returns
        reaches no step, because the handler forwards a stop only to a running step. The step
        finished, and the runner records it and starts no further step."""
        steps = [Step("a", sys.executable, ["a.py"], produces=("a.csv",), root=str(self.tmp),
                      completion=COMPLETION_OUTPUT),
                 Step("b", sys.executable, ["b.py"], consumes=("a",), root=str(self.tmp))]

        def exited_then_stopped(step) -> bool:
            (self.tmp / "a.csv").write_text("a\n")
            _mod._STOPPING = signal.SIGTERM
            return True

        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        with mock.patch.object(_mod, "run_step", exited_then_stopped), \
                contextlib.redirect_stderr(io.StringIO()):
            code = _mod.run(steps, argv=["x"], min_free_gb=0)
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertEqual(set(read_state()["completed"]), {"a"})

    def test_a_snapshot_that_from_requests_again_is_unfinished_until_it_has_run(self):
        """A Stop during the pre-checks of a monthly Start must not leave the download
        recorded as done. The next Start would otherwise adopt the previous retrieval as the new
        snapshot."""
        steps = [Step("download", "python3", ["d.py"], produces=("d.json",),
                      completion=COMPLETION_OUTPUT, irreproducible=True),
                 Step("after", "python3", ["a.py"], consumes=("download",))]
        state = {"completed": {"download": "output:x", "after": "y"}}
        _mod.drop_completed_from(state, steps, "download")
        self.assertEqual(state["completed"], {})
        self.assertEqual(state[_mod.UNFINISHED], ["download"])


class TestDiscardedNamesMatchTheValidator(unittest.TestCase):
    """The manifest leaves out names that the validator always discards.

    The runner states these names itself rather than importing them from the
    validator, because no code of the runner encodes what a step does. A name added
    to the runner's list that the validator would keep would hide a real change in
    the collected tree.
    """

    def _validator(self):
        return importlib.import_module("agentcontext.steps.collected_file_cleanup")

    def test_there_is_at_least_one(self):
        self.assertTrue(_mod.DISCARDED_NAMES)

    def test_the_validator_rejects_every_one_of_them(self):
        validate = self._validator()
        for name in sorted(_mod.DISCARDED_NAMES):
            with self.subTest(name=name):
                self.assertFalse(
                    validate.is_valid_file(f"/repo/{name}", "/repo", set(), False),
                    f"{name} is left out of the manifest but the validator keeps it")
                self.assertFalse(
                    validate.is_valid_file(f"/repo/.claude/skills/{name}", "/repo",
                                           set(), True),
                    f"{name} is kept by the validator below the repository root")

    def test_the_validator_deletes_every_discarded_directory(self):
        """The test also covers a plugin's directory, which the cleanup keeps whole and in
        which the validity pass would keep every file."""
        validate = self._validator()
        self.assertTrue(_mod.DISCARDED_DIRECTORIES)
        for name in sorted(_mod.DISCARDED_DIRECTORIES):
            with self.subTest(name=name):
                repo = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
                (repo / "CLAUDE.md").write_text("# r\n")
                (repo / name).mkdir()
                (repo / name / "x").write_text("x")
                plugin = repo / "plug" / ".claude-plugin"
                plugin.mkdir(parents=True)
                (plugin / "plugin.json").write_text('{"name": "p"}')
                (plugin / name).mkdir()
                (plugin / name / "plugin.json@SynoEAStream").write_text("x")
                deletions = []
                validate.scan_and_clean_repo(str(repo), deletions)
                self.assertFalse((repo / name).exists())
                self.assertFalse((plugin / name).exists())
                self.assertTrue((repo / "CLAUDE.md").exists())
                self.assertEqual(sorted(reason for path, reason in deletions
                                        if path.endswith(name)),
                                 ["Synology DSM directory", "Synology DSM directory"])


# ---------------------------------------------------------------------------
# acquire_lock / release_lock
# ---------------------------------------------------------------------------

class TestLock(_RunLogCase):
    """An flock held for the life of the process, as in `checks/snapshot.py`.

    In a container, the runner is pid 1 on every start. After a hard stop, a pid written into
    the file and checked with `os.kill(pid, 0)` would name a process that is always alive, and
    every restart would refuse to run. The kernel releases the flock of a holder that died,
    and the runner has no stale lock to detect or take over. The file contains an identifier
    for the operator, and the liveness test never reads it.
    """

    def _hold_from_another_process(self) -> subprocess.Popen:
        return _hold_from_another_process(self, self.lock)

    def test_acquire_on_a_free_lock_writes_an_identifier_for_the_operator(self):
        self.assertTrue(acquire_lock())
        self.addCleanup(release_lock)
        text = self.lock.read_text()
        self.assertIn(f"pid {os.getpid()}", text)
        self.assertIn("since", text)

    def test_acquire_refuses_while_another_process_holds_the_flock(self):
        self._hold_from_another_process()
        with _quiet():
            self.assertFalse(acquire_lock())

    def test_a_second_run_is_refused_while_a_first_run_holds_the_lock(self):
        """Both runs take the lock through `acquire_lock`. The lock that the first run takes
        therefore has to exclude the lock that the second run takes. A shared lock would not,
        and both runs would hold it at once."""
        first = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\nfrom pathlib import Path\nfrom agentcontext import runner\n"
             "runner.use_log_dir(Path(sys.argv[1]))\n"
             "print('held' if runner.acquire_lock() else 'refused', flush=True)\n"
             "time.sleep(60)\n", str(self.lock.parent)],
            stdout=subprocess.PIPE, text=True)
        self.addCleanup(first.wait)
        self.addCleanup(first.kill)
        self.assertEqual(first.stdout.readline().strip(), "held")
        with _quiet():
            self.assertFalse(acquire_lock())

    def test_a_file_naming_a_live_process_that_does_not_hold_it_is_taken(self):
        """The container case: pid 1 is always alive, but no process holds the flock, and the
        lock is free."""
        self.lock.write_text("1")
        with _quiet():
            self.assertTrue(acquire_lock())
        self.addCleanup(release_lock)
        self.assertIn(f"pid {os.getpid()}", self.lock.read_text())

    def test_the_content_is_never_read_as_the_liveness_test(self):
        for content in ("not a pid", "", str(_dead_pid())):
            with self.subTest(content=content):
                self.lock.write_text(content)
                with _quiet():
                    self.assertTrue(acquire_lock())
                release_lock()

    def test_a_holder_that_dies_frees_the_lock_without_anyone_cleaning_up(self):
        holder = self._hold_from_another_process()
        holder.kill()
        holder.wait()
        with _quiet():
            self.assertTrue(acquire_lock())
        self.addCleanup(release_lock)

    def test_the_lock_is_held_until_release(self):
        """A second open file description of the same file conflicts even inside one
        process. The lock is therefore the open descriptor, not the existence of the file."""
        self.assertTrue(acquire_lock())
        probe = os.open(self.lock, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)
        release_lock()
        probe = os.open(self.lock, os.O_RDWR)
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(probe)

    def test_release_leaves_the_file_and_the_next_run_takes_it(self):
        """The runner never unlinks the file. A waiter that opened the old inode would lock a
        file without a name while the next run created a new file at the path, and both runs
        would proceed."""
        acquire_lock()
        release_lock()
        self.assertTrue(self.lock.exists())
        self.assertTrue(acquire_lock())
        release_lock()

    def test_release_without_a_lock_is_not_an_error(self):
        release_lock()


# ---------------------------------------------------------------------------
# step_names / log_path
# ---------------------------------------------------------------------------

class TestStepPaths(_RunLogCase):

    def test_step_names_in_order(self):
        self.assertEqual(step_names(STEPS), ["alpha", "beta", "gamma"])

    def test_step_names_of_an_empty_list(self):
        self.assertEqual(step_names([]), [])

    def test_log_path(self):
        self.assertEqual(log_path("alpha"), self.log_dir / "alpha.log")


# ---------------------------------------------------------------------------
# pending_step_names  (the steps that the pre-checks verify)
# ---------------------------------------------------------------------------

class TestPendingStepNames(_RunLogCase):

    def test_nothing_recorded_means_everything_is_pending(self):
        self.assertEqual(pending_step_names(STEPS, set()),
                         {"alpha", "beta", "gamma"})

    def test_recorded_steps_are_not_pending(self):
        self.assertEqual(pending_step_names(STEPS, {"alpha"}),
                         {"beta", "gamma"})

    def test_all_recorded_means_nothing_is_pending(self):
        self.assertEqual(
            pending_step_names(STEPS, {"alpha", "beta", "gamma"}), set())

    def test_a_recorded_step_outside_the_list_is_ignored(self):
        self.assertEqual(pending_step_names(STEPS, {"delta"}),
                         {"alpha", "beta", "gamma"})


# ---------------------------------------------------------------------------
# _step_argv / _flag_value
# ---------------------------------------------------------------------------

class TestStepArgvLookup(unittest.TestCase):

    def test_known_step_returns_its_argv(self):
        self.assertEqual(_step_argv(STEPS, "beta"), ["beta.py", "-w", "5"])

    def test_unknown_step_returns_empty(self):
        self.assertEqual(_step_argv(STEPS, "delta"), [])


class TestFlagValue(unittest.TestCase):

    def test_flag_with_a_value(self):
        self.assertEqual(_flag_value(["a.py", "-w", "5"], "-w", "4"), "5")

    def test_absent_flag_falls_back_to_the_default(self):
        self.assertEqual(_flag_value(["a.py"], "-w", "4"), "4")

    def test_trailing_flag_falls_back_to_the_default(self):
        # No value follows the flag.
        self.assertEqual(_flag_value(["a.py", "-w"], "-w", "4"), "4")

    def test_the_value_is_the_one_that_the_cloning_steps_parser_reads(self):
        """The runner records and checks the list that `--repo-csv` names, and it therefore
        reads the step's arguments as the step's parser does: the last occurrence, in either
        form. If the runner read the first occurrence of the bare flag, the step would collect
        a list given as `--repo-csv=other.csv`, or a second list given with `--step-arg`, while
        the runner recorded the step by another list."""
        from agentcontext.steps import repository_cloning
        for argv in (["--repo-csv=other.csv"], ["--repo-csv", "a.csv", "--repo-csv", "b.csv"],
                     ["-w", "5", "--repo-col", "name", "--repo-csv", "c.csv"]):
            with self.subTest(argv=argv):
                step, _ = repository_cloning._build_arg_parser().parse_known_args(argv)
                self.assertEqual(_flag_value(argv, "--repo-csv", "d.csv"), step.repo_csv)

    def test_a_value_that_the_step_refuses_falls_back_to_the_default(self):
        # The step's parser refuses `--repo-csv --weird.csv`, and the step does not run.
        self.assertEqual(_flag_value(["--repo-csv", "--weird.csv"], "--repo-csv", "d.csv"),
                         "d.csv")


# ---------------------------------------------------------------------------
# pre-checks  (checks of the steps that will run)
# ---------------------------------------------------------------------------

class TestPreChecks(_RunLogCase):

    def setUp(self):
        super().setUp()
        self._orig_pf = (_mod.BINARY_REQUIREMENTS, _mod.VENV_PYTHON,
                         _mod.MIN_FREE_DISK_GB, _mod.PYTHON_VERSION_FILE,
                         _mod.CLONING_STEPS, _mod.github_rate_limit, _mod.openai_response)
        # Defaults that check nothing, and each test enables the check that it tests. The
        # version file points at nothing, as in the replication package, where the version
        # check checks nothing. No step clones, and the GitHub check runs only when a test
        # names a cloning step. No request goes to OpenAI unless a test names a
        # labeling step and stands in for the request.
        _mod.BINARY_REQUIREMENTS = {}
        _mod.MIN_FREE_DISK_GB = 0
        _mod.PYTHON_VERSION_FILE = self.tmp / "no-mise-toml"
        _mod.CLONING_STEPS = frozenset()
        self.rate_limit_calls = []
        _mod.github_rate_limit = self._never_reached
        self.openai_calls = []
        _mod.openai_response = self._openai_never_reached

    def tearDown(self):
        (_mod.BINARY_REQUIREMENTS, _mod.VENV_PYTHON,
         _mod.MIN_FREE_DISK_GB, _mod.PYTHON_VERSION_FILE,
         _mod.CLONING_STEPS, _mod.github_rate_limit, _mod.openai_response) = self._orig_pf
        super().tearDown()

    def _never_reached(self, token):
        raise AssertionError("The pre-checks queried GitHub when no cloning step was "
                             "pending")

    def _openai_never_reached(self, key, model):
        raise AssertionError("The pre-checks sent a request to OpenAI that the test did "
                             "not expect")

    def _check_pending(self, steps: list) -> bool:
        """The pre-checks of the steps that the pipeline execution state does not record
        as complete."""
        return pre_checks(steps, pending_step_names(steps, set(self._completed())))

    def _answering(self, remaining=4999):
        def rate_limit(token):
            self.rate_limit_calls.append(token)
            return {"limit": 5000, "remaining": remaining, "reset": 0}
        return rate_limit

    @contextlib.contextmanager
    def _token(self, value):
        """Run with GITHUB_TOKEN set to `value`, or absent for None."""
        had = os.environ.get("GITHUB_TOKEN")
        if value is None:
            os.environ.pop("GITHUB_TOKEN", None)
        else:
            os.environ["GITHUB_TOKEN"] = value
        try:
            yield
        finally:
            if had is None:
                os.environ.pop("GITHUB_TOKEN", None)
            else:
                os.environ["GITHUB_TOKEN"] = had

    def _cloning_collect(self) -> list:
        csv_path = self.tmp / "repos.csv"
        csv_path.write_text("repo_name\n")
        _mod.CLONING_STEPS = frozenset({'repository_cloning'})
        return [Step('repository_cloning', "python3", ["c.py", "--repo-csv", str(csv_path)])]

    # -- GitHub: the GitHub access token and one request, before any clone --------------------

    def test_a_cloning_step_without_a_github_access_token_fails(self):
        """Without a GitHub access token, git clones anonymously and every clone fails.
        `finished_repositories.jsonl` then records each repository as finished, and the run
        exits 0 with an empty sample."""
        steps = self._cloning_collect()
        with self._token(None), _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'repository_cloning'}))
        self.assertIn("GITHUB_TOKEN", err.getvalue())
        self.assertIn("--no-github", err.getvalue())

    def test_a_cloning_step_with_a_github_access_token_queries_github_once(self):
        steps = self._cloning_collect()
        _mod.github_rate_limit = self._answering(remaining=4321)
        with self._token("t0ken"), _captured_stderr() as err:
            self.assertTrue(pre_checks(steps, {'repository_cloning'}))
        self.assertEqual(self.rate_limit_calls, ["t0ken"])
        self.assertIn("4,321", err.getvalue())

    def test_collection_log_identifies_the_one_github_access_token_checked_from_five(self):
        steps = self._cloning_collect()
        _mod.github_rate_limit = self._answering()
        tokens = [f"private_token_{n}" for n in range(5)]
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": tokens[0],
                                         "GITHUB_TOKENS": ",".join(tokens)}), _captured_stderr() as err:
            self.assertTrue(pre_checks(steps, {'repository_cloning'}))
        self.assertEqual(self.rate_limit_calls, tokens[:1])
        self.assertIn("GitHub access token 1 of 5 passed validation", err.getvalue())
        for token in tokens:
            self.assertNotIn(token, err.getvalue())

    def test_a_cloning_step_finds_its_github_access_token_in_the_credentials_file(self):
        """The GitHub access tokens are in credentials.csv at the repository root, which every
        step that calls GitHub reads. The environment may set none."""
        steps = self._cloning_collect()
        _mod.github_rate_limit = self._answering()
        path = self.tmp / "credentials.csv"
        path.write_text("service,account,credential\ngithub,someone,ghp_fromfile\n")
        with mock.patch.dict(os.environ, {}, clear=False), _captured_stderr() as err:
            os.environ.pop("GITHUB_TOKEN", None)
            os.environ.pop("GITHUB_TOKENS", None)
            self.assertTrue(pre_checks(steps, {"repository_cloning"},
                                                credentials_path=path))
            self.assertEqual(os.environ["GITHUB_TOKEN"], "ghp_fromfile")
        self.assertEqual(self.rate_limit_calls, ["ghp_fromfile"])
        self.assertNotIn("ghp_fromfile", err.getvalue())

    def test_a_declared_credential_may_be_in_the_credentials_file(self):
        step = Step("x", sys.executable, ["x.py"], env=("OPENAI_API_KEY",))
        path = self.tmp / "credentials.csv"
        with mock.patch.dict(os.environ, {}, clear=False), _captured_stderr() as err:
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertFalse(pre_checks([step], {"x"}, credentials_path=path))
            self.assertIn(f"No OpenAI API key is configured (required by x). OPENAI_API_KEY is "
                          f"unset, and {path} has none.", err.getvalue())
            path.write_text("service,account,credential\nopenai,,sk-fromfile\n")
            self.assertTrue(pre_checks([step], {"x"}, credentials_path=path))

    def test_a_problem_with_the_credentials_file_is_logged_without_its_text(self):
        path = self.tmp / "credentials.csv"
        path.write_text("service,account,credential\ngithub,someone,ghp_x # a comment\n")
        with _captured_stderr() as err:
            self.assertTrue(pre_checks([], set(), credentials_path=path))
        self.assertIn("WARNING pre_checks:", err.getvalue())
        self.assertIn("line 2 is not a GitHub access token", err.getvalue())
        self.assertNotIn("ghp_x", err.getvalue())

    def test_a_problem_with_a_credential_of_the_environment_is_logged_without_its_value(self):
        with mock.patch.dict(os.environ, {"GITHUB_TOKENS": "ghp_first\nghp_second"}), \
                _captured_stderr() as err:
            self.assertTrue(pre_checks([], set(), credentials_path=self.tmp / "none.csv"))
        self.assertIn("WARNING pre_checks: Value 1 of 1 in GITHUB_TOKENS does not have "
                      "the form of a GitHub access token", err.getvalue())
        self.assertNotIn("ghp_first", err.getvalue())
        self.assertNotIn("ghp_second", err.getvalue())

    def test_a_rejected_github_access_token_falls_back_to_the_next_configured_github_access_token(self):
        steps = self._cloning_collect()
        tokens = [f"secret_{n}" for n in range(5)]

        def rate_limit(token):
            self.rate_limit_calls.append(token)
            if token == tokens[0]:
                raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401, "Unauthorized", {}, None)
            return {"limit": 5000, "remaining": 4000, "reset": 0}

        _mod.github_rate_limit = rate_limit
        with mock.patch.dict(os.environ, {"GITHUB_TOKEN": tokens[0],
                                         "GITHUB_TOKENS": ",".join(tokens)}), _captured_stderr() as err:
            self.assertTrue(pre_checks(steps, {"repository_cloning"}))
            self.assertEqual(os.environ["GITHUB_TOKEN"], tokens[1])
        self.assertEqual(self.rate_limit_calls, tokens[:2])
        self.assertIn("GitHub access token 1 of 5 failed validation", err.getvalue())
        self.assertIn("GitHub access token 2 of 5 passed validation", err.getvalue())
        for token in tokens:
            self.assertNotIn(token, err.getvalue())

    def test_all_rejected_github_access_tokens_stop_collection(self):
        steps = self._cloning_collect()

        def rejected(token):
            self.rate_limit_calls.append(token)
            raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401, "Unauthorized", {}, None)

        _mod.github_rate_limit = rejected
        with mock.patch.object(_mod, "GITHUB_REJECTED_WAITS", (0, 0, 0)), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "bad1",
                                             "GITHUB_TOKENS": "bad1,bad2,bad2,bad3"}), \
                _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {"repository_cloning"}))
        self.assertEqual(self.rate_limit_calls, ["bad1", "bad2", "bad3"] * 4)
        self.assertIn("None of the 3 configured GitHub access tokens passed validation",
                      err.getvalue())

    def test_a_spell_in_which_github_rejects_every_github_access_token_is_waited_out(self):
        """GitHub can reject valid GitHub access tokens for a while. If one HTTP 401 for each GitHub
        access token refused the Start, nothing would start the unattended run again. The
        pre-checks therefore check the GitHub access tokens again, in their order, after
        each wait of GITHUB_REJECTED_WAITS."""
        steps = self._cloning_collect()

        def rejected_once(token):
            self.rate_limit_calls.append(token)
            if len(self.rate_limit_calls) <= 2:
                raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401,
                                                  "Unauthorized", {}, None)
            return {"limit": 5000, "remaining": 4000, "reset": 0}

        _mod.github_rate_limit = rejected_once
        with mock.patch.object(_mod, "GITHUB_REJECTED_WAITS", (0, 0, 0)), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_first",
                                             "GITHUB_TOKENS": "ghp_first,ghp_second"}), \
                _captured_stderr() as err:
            self.assertTrue(pre_checks(steps, {"repository_cloning"}))
            self.assertEqual(os.environ["GITHUB_TOKEN"], "ghp_first")
        self.assertEqual(self.rate_limit_calls, ["ghp_first", "ghp_second", "ghp_first"])
        self.assertIn("GitHub access token 1 of 2 passed validation", err.getvalue())
        self.assertNotIn("ghp_", err.getvalue())

    def test_configured_pool_supplies_a_github_access_token_when_github_token_is_unset(self):
        steps = self._cloning_collect()
        _mod.github_rate_limit = self._answering()
        with mock.patch.dict(os.environ, {"GITHUB_TOKENS": "first,second"}), self._token(None), _quiet():
            self.assertTrue(pre_checks(steps, {"repository_cloning"}))
            self.assertEqual(os.environ["GITHUB_TOKEN"], "first")
        self.assertEqual(self.rate_limit_calls, ["first"])

    def test_a_github_access_token_that_github_rejects_fails(self):
        steps = self._cloning_collect()

        def rejected(token):
            raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401, "Unauthorized", {}, None)
        _mod.github_rate_limit = rejected
        with mock.patch.object(_mod, "GITHUB_REJECTED_WAITS", (0, 0, 0)), self._token("bad"), \
                _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'repository_cloning'}))
        self.assertIn("401", err.getvalue())

    def test_the_wait_for_rejected_github_access_tokens_ends_at_a_stop(self):
        """The waits of GITHUB_REJECTED_WAITS last hours, and a stop handler returns. A plain
        sleep would hold a Stop of the container until the grace period ended in SIGKILL."""
        steps = self._cloning_collect()

        def rejected(token):
            self.rate_limit_calls.append(token)
            raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401,
                                              "Unauthorized", {}, None)
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            _mod._STOPPING = signal.SIGTERM

        _mod.github_rate_limit = rejected
        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        with mock.patch.object(_mod.time, "sleep", sleep), self._token("bad"), \
                _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'repository_cloning'}))
        self.assertEqual(self.rate_limit_calls, ["bad"])
        self.assertEqual(slept, [1.0])
        self.assertIn("A stop arrived during the wait before the next check", err.getvalue())

    def test_a_stop_during_the_pre_checks_sends_no_further_request(self):
        """After a stop, the run ends before its first step, whatever the pre-checks find.
        A stop during the check of one GitHub access token therefore keeps the pre-checks
        from checking the next GitHub access token and the OpenAI check from sending its request."""
        steps = self._cloning_collect()

        def rejected(token):
            self.rate_limit_calls.append(token)
            _mod._STOPPING = signal.SIGTERM
            raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401,
                                              "Unauthorized", {}, None)

        _mod.github_rate_limit = rejected
        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        with mock.patch.dict(os.environ, {"GITHUB_TOKENS": "bad1,bad2"}), self._token(None), \
                _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {"repository_cloning"}))
        self.assertEqual(self.rate_limit_calls, ["bad1"])
        # The log names no verdict that the stop cut short.
        self.assertIn("A stop arrived during the check of the GitHub access tokens",
                      err.getvalue())
        self.assertNotIn("rejected each", err.getvalue())
        self.assertNotIn("failed validation", err.getvalue())
        labeling = Step("repository_labeling", sys.executable, ["classify.py", "--model", "m"],
                        root=str(self.tmp))
        # The fixture's stand-in for the request fails the test when the check sends one.
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test"}), _quiet():
            self.assertTrue(_mod.labeling_keys_accepted([labeling], {"repository_labeling"}))

    def test_a_stop_during_the_last_check_of_rejected_github_access_tokens_refuses_nothing(self):
        """The runbook reads an `ERROR pre_checks:` line as a refusal of the GitHub access
        tokens. A stop during the last check, after the last wait, logs the stop rather than
        an error."""
        steps = self._cloning_collect()

        def rejected(token):
            self.rate_limit_calls.append(token)
            if len(self.rate_limit_calls) == len(_mod.GITHUB_REJECTED_WAITS) + 1:
                _mod._STOPPING = signal.SIGTERM
            raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 401,
                                              "Unauthorized", {}, None)

        _mod.github_rate_limit = rejected
        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        with mock.patch.object(_mod, "GITHUB_REJECTED_WAITS", (0, 0, 0)), self._token("bad"), \
                _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {"repository_cloning"}))
        self.assertEqual(self.rate_limit_calls, ["bad"] * 4)
        self.assertIn("A stop arrived during the check of the GitHub access tokens",
                      err.getvalue())
        self.assertNotIn("ERROR pre_checks", err.getvalue())

    def test_a_stop_during_a_check_without_a_verdict_announces_no_pause(self):
        def unreachable(token):
            self.rate_limit_calls.append(token)
            _mod._STOPPING = signal.SIGTERM
            raise _mod.urllib.error.URLError("no route")

        _mod.github_rate_limit = unreachable
        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        with _captured_stderr() as err:
            self.assertEqual(_mod.github_verdict("t0ken", 1, 1)[0], "undecided")
        self.assertEqual(self.rate_limit_calls, ["t0ken"])
        self.assertNotIn("checks it again", err.getvalue())

    def test_a_check_without_a_verdict_is_repeated_after_a_pause(self):
        """Only HTTP 401 shows that GitHub rejects a GitHub access token. A request that could not
        reach GitHub would refuse the Start of a monthly run if the pre-checks counted it as
        a rejection."""
        steps = self._cloning_collect()
        outcomes = [_mod.urllib.error.URLError("no route"), None]

        def rate_limit(token):
            self.rate_limit_calls.append(token)
            outcome = outcomes.pop(0)
            if outcome is not None:
                raise outcome
            return {"limit": 5000, "remaining": 4000, "reset": 0}

        _mod.github_rate_limit = rate_limit
        with mock.patch.object(_mod, "GITHUB_CHECK_WAITS", (0, 0, 0)), \
                self._token("t0ken"), _captured_stderr() as err:
            self.assertTrue(pre_checks(steps, {'repository_cloning'}))
        self.assertEqual(self.rate_limit_calls, ["t0ken", "t0ken"])
        self.assertIn("GitHub access token 1 of 1 passed validation", err.getvalue())
        self.assertNotIn("failed validation", err.getvalue())

    def test_the_pause_before_another_check_ends_at_a_stop(self):
        """A plain sleep would hold a stop through the pauses of GITHUB_CHECK_WAITS, and the
        pre-checks would then send their further requests."""
        def unreachable(token):
            self.rate_limit_calls.append(token)
            raise _mod.urllib.error.URLError("no route")
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            _mod._STOPPING = signal.SIGTERM

        _mod.github_rate_limit = unreachable
        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        with mock.patch.object(_mod.time, "sleep", sleep), _quiet():
            self.assertEqual(_mod.github_verdict("t0ken", 1, 1)[0], "undecided")
        self.assertEqual((self.rate_limit_calls, slept), (["t0ken"], [1.0]))

    def test_a_github_access_token_that_never_gets_a_verdict_is_kept(self):
        steps = self._cloning_collect()

        def unavailable(token):
            self.rate_limit_calls.append(token)
            raise _mod.urllib.error.HTTPError("https://api.github.com/rate_limit", 503,
                                              "Service Unavailable", {}, None)

        _mod.github_rate_limit = unavailable
        with mock.patch.object(_mod, "GITHUB_CHECK_WAITS", (0, 0, 0)), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_first",
                                             "GITHUB_TOKENS": "ghp_first,ghp_second"}), \
                _captured_stderr() as err:
            self.assertTrue(pre_checks(steps, {'repository_cloning'}))
            self.assertEqual(os.environ["GITHUB_TOKEN"], "ghp_first")
        self.assertEqual(self.rate_limit_calls, ["ghp_first"] * 4)
        self.assertIn("GitHub gave no verdict on GitHub access token 1 of 2 after 4 attempts "
                      "(HTTP 503)", err.getvalue())
        self.assertNotIn("ghp_first", err.getvalue())

    def test_a_rate_limit_body_in_another_format_is_no_verdict(self):
        """A 200 response with no object at `resources.core` in its body, such as the response
        of a proxy or of a failing server, shows nothing about the GitHub access token."""
        steps = self._cloning_collect()
        _mod.github_rate_limit = self._orig_pf[5]
        for body in (b"[]", b"null", b'{"resources": []}', b'{"resources": {"core": "x"}}'):
            with self.subTest(body=body):
                asked = []

                def urlopen(request, timeout=None):
                    asked.append(request.full_url)
                    return contextlib.nullcontext(io.BytesIO(body))

                with mock.patch.object(_mod.urllib.request, "urlopen", urlopen), \
                        mock.patch.object(_mod, "GITHUB_CHECK_WAITS", (0, 0, 0)), \
                        self._token("t0ken"), _captured_stderr() as err:
                    self.assertTrue(pre_checks(steps, {'repository_cloning'}))
                self.assertEqual(asked, [_mod.GITHUB_RATE_LIMIT_URL] * 4)
                self.assertIn("GitHub gave no verdict on GitHub access token 1 of 1 after 4 "
                              "attempts (ValueError)", err.getvalue())

    def test_no_github_skips_the_request_and_forbids_a_github_access_token(self):
        """A subsample run redirects github.com to file:// through GIT_CONFIG_*. With a GitHub
        access token, the clone URL names a user, and the redirect does not match it. The run
        must therefore have no GitHub access token."""
        steps = self._cloning_collect()
        with self._token(None), _quiet():
            self.assertTrue(pre_checks(steps, {'repository_cloning'}, no_github=True))
        with self._token("t0ken"), _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'repository_cloning'}, no_github=True))
        self.assertIn("--no-github", err.getvalue())
        self.assertIn("GITHUB_TOKEN", err.getvalue())

    def test_no_cloning_step_pending_never_queries_github(self):
        steps = self._cloning_collect() + [Step('detection', "python3", ["d.py"])]
        with self._token(None), _quiet():
            self.assertTrue(pre_checks(steps, {'detection'}))

    # -- OpenAI: one request with the key and the model of a pending labeling step ------------

    def _labeling(self, *extra) -> Step:
        """A labeling step with a script that assigns its default model, as the real script
        does."""
        (self.tmp / "classify.py").write_text('DEFAULT_MODEL = "model-of-the-script"\n')
        return Step('repository_labeling', sys.executable, ["classify.py", *extra],
                    env=("OPENAI_API_KEY",), root=str(self.tmp), code=str(self.tmp))

    def _openai(self, status=None, body=None, raised=None):
        """A stand-in for the request that records its key and model and fails as given."""
        def respond(key, model):
            self.openai_calls.append((key, model))
            if raised is not None:
                raise raised
            if status is not None:
                raise _mod.urllib.error.HTTPError(
                    _mod.OPENAI_RESPONSES_URL, status, "error", {},
                    io.BytesIO(json.dumps(body or {}).encode()))
        return respond

    def _pre_checks_with_key(self, steps, pending, key="sk-first"):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": key}), _captured_stderr() as err:
            ok = pre_checks(steps, pending,
                                     credentials_path=self.tmp / "no-credentials.csv")
        return ok, err.getvalue()

    def test_a_pending_labeling_step_sends_one_request_with_its_key_and_model(self):
        _mod.openai_response = self._openai()
        ok, err = self._pre_checks_with_key([self._labeling()], {'repository_labeling'})
        self.assertTrue(ok)
        self.assertEqual(self.openai_calls, [("sk-first", "model-of-the-script")])
        self.assertIn("OpenAI accepted a request", err)
        self.assertNotIn("sk-first", err)

    def test_the_model_is_the_last_model_of_the_arguments(self):
        """A `--step-arg repository_labeling:"--model ..."` is appended to the step's arguments,
        and the script's parser takes the last value."""
        _mod.openai_response = self._openai()
        steps = [self._labeling("--model", "listed", "--model=chosen")]
        self.assertTrue(self._pre_checks_with_key(steps, {'repository_labeling'})[0])
        self.assertEqual(self.openai_calls, [("sk-first", "chosen")])

    def test_spent_quota_is_a_warning_because_the_labeling_waits_for_credit(self):
        """A top-up ends a refusal for spent quota, and the labeling waits for the credit. A
        failed pre-check would stop a monthly run that nothing starts again."""
        cases = {"no quota": (429, {"error": {"type": "insufficient_quota",
                                              "code": "insufficient_quota"}}),
                 "billing limit": (400, {"error": {"type": "billing_hard_limit_reached"}})}
        for name, (status, body) in cases.items():
            with self.subTest(name):
                _mod.openai_response = self._openai(status, body)
                ok, err = self._pre_checks_with_key([self._labeling()],
                                                    {'repository_labeling'}, key="sk-secret")
                self.assertTrue(ok)
                self.assertIn("WARNING pre_checks:", err)
                self.assertIn("waits for credit when it reaches OpenAI. To continue then, raise "
                              "the quota or the billing limit of the OpenAI project.", err)
                self.assertNotIn("ERROR pre_checks:", err)
                self.assertNotIn("sk-secret", err)

    def test_what_refuses_every_request_of_the_run_fails_the_pre_checks(self):
        """A revoked key, a project without access and a model that the project cannot use fail
        every request of the labeling, and a top-up does not end the refusal. Without the check,
        the labeling would run after the sampling stage and fail there for each repository."""
        cases = {"rejected key": (401, {"error": {"code": "invalid_api_key"}}),
                 "no access": (403, {"error": {"code": "unsupported_country_region_territory"}}),
                 "no such model": (404, {"error": {"code": "model_not_found"}})}
        for name, (status, body) in cases.items():
            with self.subTest(name):
                _mod.openai_response = self._openai(status, body)
                ok, err = self._pre_checks_with_key([self._labeling()],
                                                             {'repository_labeling'},
                                                             key="sk-secret")
                self.assertFalse(ok)
                self.assertIn("ERROR pre_checks:", err)
                self.assertIn(f"HTTP {status}", err)
                self.assertIn("model-of-the-script", err)
                self.assertNotIn("sk-secret", err)

    def test_an_error_that_can_pass_is_a_warning(self):
        """A server error, a rate limit or a connection that does not respond in time shows
        nothing about the key, and the step retries its own requests."""
        class CutShort(io.BytesIO):
            """The body of an error response that the connection cuts off before its declared
            length."""
            def read(self, *args):
                raise _mod.http.client.IncompleteRead(b'{"error": {"type": "ser', 4975)

        cases = {"server error": dict(status=503, body={"error": {"type": "server_error"}}),
                 "rate limit": dict(status=429, body={"error": {"code": "rate_limit_exceeded"}}),
                 "timeout": dict(raised=TimeoutError("timed out")),
                 "unreachable": dict(raised=_mod.urllib.error.URLError("no route")),
                 "a server error whose body is cut short": dict(
                     raised=_mod.urllib.error.HTTPError(_mod.OPENAI_RESPONSES_URL, 503, "error",
                                                        {}, CutShort()))}
        for name, failure in cases.items():
            with self.subTest(name):
                _mod.openai_response = self._openai(**failure)
                ok, err = self._pre_checks_with_key([self._labeling()],
                                                             {'repository_labeling'})
                self.assertTrue(ok, err)
                self.assertIn("WARNING pre_checks:", err)
                self.assertNotIn("ERROR", err)

    def test_an_openrouter_model_checks_the_openrouter_api_key_and_sends_openai_nothing(self):
        """The default model of the labeling is DeepSeek V4.1 Flash through OpenRouter, and its
        model id, `deepseek/deepseek-v4.1-flash`, names its author. A request to OpenAI with that
        model would fail as a model that OpenAI does not offer and stop the run. Instead, the
        pre-checks read the remaining credit of the OpenRouter API key, and reading it spends
        nothing. A rejected key fails them. A key without credit is a warning, because the
        labeling waits for credit. An error that can pass, such as HTTP 503, is a warning too."""
        (self.tmp / "classify.py").write_text('DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"\n')
        step = Step('repository_labeling', sys.executable, ["classify.py"],
                    env=("OPENROUTER_API_KEY",), root=str(self.tmp), code=str(self.tmp))
        _mod.openai_response = self._openai()
        cases = {"credit left": ({"data": {"limit_remaining": 12.5}}, True, "INFO",
                                 "OpenRouter accepted the OpenRouter API key"),
                 "unlimited": ({"data": {"limit_remaining": None}}, True, "INFO",
                               "OpenRouter accepted the OpenRouter API key"),
                 "no credit": ({"data": {"limit_remaining": 0,
                                         "effective_budget": {"remaining_usd": 3}}}, True,
                               "WARNING", "waits for credit when it reaches OpenRouter"),
                 "rejected key": (401, False, "ERROR",
                                  "OpenRouter rejects the OpenRouter API key (HTTP 401, model "
                                  "deepseek/deepseek-v4.1-flash)"),
                 "server error": (503, True, "WARNING", "because the error can pass")}
        for name, (answer, accepted, level, message) in cases.items():
            with self.subTest(name):
                read = []

                def key_data(key, answer=answer):
                    read.append(key)
                    if isinstance(answer, int):
                        raise _mod.urllib.error.HTTPError(_mod.OPENROUTER_KEY_URL, answer,
                                                          "error", {}, io.BytesIO(b"{}"))
                    return answer["data"]
                with mock.patch.object(_mod, "openrouter_key", key_data), \
                        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-secret"}), \
                        _captured_stderr() as err:
                    ok = pre_checks([step], {'repository_labeling'},
                                    credentials_path=self.tmp / "no-credentials.csv")
                self.assertEqual((ok, read, self.openai_calls),
                                 (accepted, ["sk-or-secret"], []), err.getvalue())
                self.assertIn(f"{level} pre_checks:", err.getvalue())
                self.assertIn(message, err.getvalue())
                self.assertNotIn("sk-or-secret", err.getvalue())

    def test_one_openrouter_api_key_is_the_key_and_further_keys_are_named_as_unused(self):
        """The pre-check reads the credit of the first OpenRouter API key, and the labeling sends
        its requests with that key alone (`endpoints.api_key`). With one key, the line names it
        as the OpenRouter API key. With two or three, from the environment and the credentials
        file, a warning names the ones that stay unused, with the verb in their number, and the
        line names the checked key as the first."""
        (self.tmp / "classify.py").write_text('DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"\n')
        step = Step('repository_labeling', sys.executable, ["classify.py"],
                    env=("OPENROUTER_API_KEY",), root=str(self.tmp), code=str(self.tmp))
        several = self.tmp / "credentials.csv"
        several.write_text("service,account,credential\nopenrouter,,sk-or-second\n"
                           "openrouter,,sk-or-third\n")
        second = self.tmp / "second.csv"
        second.write_text("service,account,credential\nopenrouter,,sk-or-second\n")
        accepted = ("OpenRouter API key (required by repository_labeling with model "
                    "deepseek/deepseek-v4.1-flash).")
        cases = {"one key": (self.tmp / "no-credentials.csv", [],
                             f"INFO pre_checks: OpenRouter accepted the {accepted}"),
                 "two keys": (second,
                              ["WARNING pre_checks: 1 of the 2 configured OpenRouter API "
                               "keys is unused. The pre-check and repository_labeling use only "
                               "the first."],
                              f"INFO pre_checks: OpenRouter accepted the first {accepted}"),
                 "three keys": (several,
                                ["WARNING pre_checks: 2 of the 3 configured OpenRouter API "
                                 "keys are unused. The pre-check and repository_labeling use "
                                 "only the first."],
                                f"INFO pre_checks: OpenRouter accepted the first {accepted}")}
        for name, (path, warnings, line) in cases.items():
            with self.subTest(name):
                read = []

                def key_data(key):
                    read.append(key)
                    return {"limit_remaining": 12.5}
                with mock.patch.object(_mod, "openrouter_key", key_data), \
                        mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "sk-or-first"}), \
                        _captured_stderr() as err:
                    ok = pre_checks([step], {'repository_labeling'}, credentials_path=path)
                lines = err.getvalue().splitlines()
                self.assertTrue(ok, err.getvalue())
                self.assertEqual(read, ["sk-or-first"])
                self.assertEqual([found for found in lines if "OpenRouter API keys" in found],
                                 warnings)
                self.assertIn(line, lines)

    def test_no_request_goes_out_without_a_key_or_for_a_labeling_step_that_is_done(self):
        steps = [self._labeling(), Step('detection', "python3", ["d.py"])]
        with mock.patch.dict(os.environ, {}), _captured_stderr() as err:
            os.environ.pop("OPENAI_API_KEY", None)
            self.assertFalse(pre_checks(steps, {'repository_labeling'},
                                                 credentials_path=self.tmp / "no-credentials.csv"))
        self.assertIn("OPENAI_API_KEY is unset", err.getvalue())
        self.assertTrue(self._pre_checks_with_key(steps, {'detection'})[0])
        self.assertEqual(self.openai_calls, [])

    # -- free space: the whole minimum only when a step clones ---------------------------------

    def test_the_clones_minimum_applies_only_while_a_cloning_step_is_pending(self):
        """A resume of the steps after the collection clones nothing. The pre-checks must
        not refuse it at 188.7 GiB free against the minimum that five clones at the size bound
        need."""
        _mod.MIN_FREE_DISK_GB = _RUNNER_DEFAULTS["MIN_FREE_DISK_GB"]
        _mod.CLONING_STEPS = frozenset({'repository_cloning'})
        steps = self._cloning_collect() + [Step('detection', "python3", ["d.py"])]

        def free_space(path):
            return mock.Mock(free=188.7 * 1024 ** 3)
        with self._token(None), mock.patch.object(_mod.shutil, "disk_usage", free_space):
            with _captured_stderr() as err:
                self.assertTrue(pre_checks(steps, {'detection'}))
            self.assertNotIn("free on disk", err.getvalue())
            with _captured_stderr() as err:
                self.assertFalse(pre_checks(steps, {'repository_cloning', 'detection'},
                                                     no_github=True))
            self.assertIn("188.7 GiB free on disk", err.getvalue())
            self.assertIn(f"under the minimum of {_mod.MIN_FREE_DISK_GB} GiB", err.getvalue())
            # A minimum that the command line names applies whether or not a step clones.
            with _quiet():
                self.assertFalse(pre_checks(steps, {'detection'}, min_free_gb=189))

    def test_without_a_cloning_step_the_room_beside_the_clones_is_still_required(self):
        _mod.MIN_FREE_DISK_GB = _RUNNER_DEFAULTS["MIN_FREE_DISK_GB"]

        def free_space(path):
            return mock.Mock(free=1024 ** 3)
        with mock.patch.object(_mod.shutil, "disk_usage", free_space), \
                _captured_stderr() as err:
            self.assertFalse(pre_checks([Step('detection', "python3", ["d.py"])],
                                                 {'detection'}))
        self.assertIn(f"under the minimum of "
                      f"{_mod.MIN_FREE_DISK_GB - _mod.CLONES_IN_FLIGHT_GB} GiB", err.getvalue())

    def test_the_clones_in_flight_are_the_workers_times_the_clone_bound(self):
        from agentcontext.git import bounds
        from agentcontext.steps import repository_cloning
        workers = repository_cloning._build_arg_parser().get_default("workers")
        self.assertEqual(_mod.CLONES_IN_FLIGHT_GB * 1024 ** 3, workers * bounds.DEFAULT_MAX_BYTES)
        self.assertLess(_mod.CLONES_IN_FLIGHT_GB, _RUNNER_DEFAULTS["MIN_FREE_DISK_GB"])

    # -- what a step reads and no pending step writes -------------------------

    def test_a_pending_step_fails_when_an_input_that_it_reads_is_missing(self):
        steps = [Step('detection', "python3", ["d.py"],
                        reads=(str(self.tmp / "file_history.csv"),))]
        with _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'detection'}))
        self.assertIn("file_history.csv", err.getvalue())
        self.assertIn('detection', err.getvalue())

    def test_an_input_on_disk_that_the_step_reads_passes(self):
        cache = self.tmp / "file_history.csv"
        cache.write_text("repo_name\n")
        steps = [Step('detection', "python3", ["d.py"], reads=(str(cache),))]
        with _quiet():
            self.assertTrue(pre_checks(steps, {'detection'}))

    def test_an_input_that_the_step_reads_and_a_pending_step_produces_passes(self):
        cache = str(self.tmp / "ai_authorship.csv")
        steps = [Step("seed", "python3", ["s.py"], produces=(cache,)),
                  Step('detection', "python3", ["d.py"], reads=(cache,))]
        with _quiet():
            self.assertTrue(pre_checks(steps, {"seed", 'detection'}))

    def test_an_input_read_by_a_step_that_is_done_is_not_checked(self):
        steps = [Step('detection', "python3", ["d.py"],
                        reads=(str(self.tmp / "gone.csv"),))]
        with _quiet():
            self.assertTrue(pre_checks(steps, set()))

    # -- the scratch directory ---------------------------------------------------

    def test_the_help_names_the_directory_of_the_default_scratch_dir(self):
        """The clones go to `_scratch/` in the collection stage's data directory, which the
        runner sets as each step's working directory. The help names that directory, not the
        directory of the step's script."""
        text = " ".join(_mod.build_parser().format_help().split())
        self.assertIn("default: _scratch/ in the stage directory", text)
        self.assertNotIn("step script", text)

    def test_the_scratch_dir_is_created_and_measured_when_a_step_will_clone(self):
        """The scratch directory is on a volume of its own that is short of space, and the
        stage directory is not. The logged error is then the scratch directory's, and a
        pre-checks that stopped measuring the scratch directory would fail the test."""
        steps = self._cloning_collect()
        scratch = self.tmp / "volume" / "scratch"
        _mod.MIN_FREE_DISK_GB = 10
        real_stat = os.stat

        def on_its_own_device(path, *args, **kwargs):
            found = real_stat(path, *args, **kwargs)
            if Path(path) != scratch:
                return found
            fields = list(found)
            fields[2] = found.st_dev + 1          # st_dev
            return os.stat_result(fields)

        def free_space(path):
            return mock.Mock(free=(1 if Path(path) == scratch else 100) * 1024 ** 3)

        with self._token(None), _captured_stderr() as err, \
                _swap(_mod, "STAGE_DIR", self.tmp), \
                mock.patch.object(_mod.os, "stat", on_its_own_device), \
                mock.patch.object(_mod.shutil, "disk_usage", free_space):
            self.assertFalse(pre_checks(steps, {'repository_cloning'}, no_github=True,
                                                 scratch_dir=scratch))
        self.assertTrue(scratch.is_dir())
        self.assertIn(f"Only 1.0 GiB free on disk at {scratch}, the scratch directory",
                      err.getvalue())
        self.assertNotIn("the stage directory", err.getvalue())

    def test_free_space_is_measured_where_a_fresh_data_root_will_be(self):
        """Over a new data root, neither the data root nor the stage directory exists yet. The
        check therefore measures the nearest existing directory rather than skipping every
        volume without a message."""
        steps = [Step('detection', "python3", ["d.py"])]
        fresh = self.tmp / "fresh" / "data"

        def free_space(path):
            return mock.Mock(free=1024 ** 3)
        with _captured_stderr() as err, _swap(_mod, "DATA_ROOT", fresh), \
                _swap(_mod, "STAGE_DIR", fresh / "collection"), \
                mock.patch.object(_mod.shutil, "disk_usage", free_space):
            self.assertFalse(pre_checks(steps, {'detection'}, min_free_gb=10))
        self.assertIn(f"Only 1.0 GiB free on disk at {self.tmp}, the data root", err.getvalue())
        self.assertFalse(fresh.exists())

    def test_an_input_read_through_a_stage_directory_not_yet_created_is_found(self):
        """A restart after a first invocation that stopped before the analysis. The analysis
        has no directory yet, and for the kernel `analysis/../collection/data/validated` does
        not exist."""
        data = self.tmp / "data"
        (data / "collection" / "data" / "validated").mkdir(parents=True)
        step = Step("figure", "python3", ["f.py"], root=str(data / "analysis"),
                    reads=("../collection/data/validated",))
        self.assertEqual(_mod.missing_reads([step], {"figure"}), [])
        missing = Step("other", "python3", ["f.py"], root=str(data / "analysis"),
                       reads=("../collection/data/absent",))
        self.assertEqual(_mod.missing_reads([missing], {"other"}),
                         [("other", "../collection/data/absent")])

    def test_the_scratch_dir_is_left_alone_when_nothing_clones(self):
        steps = [Step('detection', "python3", ["d.py"])]
        scratch = self.tmp / "scratch"
        with _quiet():
            self.assertTrue(pre_checks(steps, {'detection'}, scratch_dir=scratch))
        self.assertFalse(scratch.exists())

    def test_a_scratch_dir_that_cannot_be_created_fails(self):
        steps = self._cloning_collect()
        blocker = self.tmp / "file"
        blocker.write_text("")
        with self._token(None), _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'repository_cloning'}, no_github=True,
                                                 scratch_dir=blocker / "scratch"))
        self.assertIn("scratch directory", err.getvalue())

    def test_the_cloning_steps_are_the_ones_that_need_git(self):
        _mod.CLONING_STEPS = self._orig_pf[4]
        self.assertEqual(_mod.CLONING_STEPS, self._orig_pf[0]["git"])

    def _pin(self, version: str) -> None:
        _mod.PYTHON_VERSION_FILE = self.tmp / "mise.toml"
        _mod.PYTHON_VERSION_FILE.write_text(
            f'[tools]\npython = "{version}"\n')

    @staticmethod
    def _running_version() -> str:
        return f"{sys.version_info[0]}.{sys.version_info[1]}"

    def _stub_interpreter(self, exit_code: int) -> str:
        """An executable that ignores its arguments and exits with exit_code."""
        p = self.tmp / f"interpreter{exit_code}"
        p.write_text(f"#!/bin/sh\nexit {exit_code}\n")
        p.chmod(0o755)
        return str(p)

    def test_nothing_to_check_passes(self):
        with _quiet():
            self.assertTrue(self._check_pending(STEPS))

    def test_missing_binary_for_a_pending_step_fails(self):
        _mod.BINARY_REQUIREMENTS = {"definitely-not-a-real-binary": {"alpha"}}
        with _captured_stderr() as err:
            self.assertFalse(self._check_pending(STEPS))
        self.assertIn("definitely-not-a-real-binary", err.getvalue())

    def test_present_binary_passes(self):
        _mod.BINARY_REQUIREMENTS = {"sh": {"alpha"}}
        with _quiet():
            self.assertTrue(self._check_pending(STEPS))

    def test_missing_binary_for_a_done_step_is_not_checked(self):
        # The case of 7zz, which only the archive and the release need. A step that is
        # already done cannot block a run that never reaches it.
        _mod.BINARY_REQUIREMENTS = {"definitely-not-a-real-binary": {"alpha"}}
        self._mark_done("alpha")
        with _quiet():
            self.assertTrue(self._check_pending(STEPS))

    def test_disk_below_the_minimum_fails(self):
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        with _captured_stderr() as err:
            self.assertFalse(self._check_pending(STEPS))
        self.assertIn("free on disk", err.getvalue())

    def test_disk_is_checked_even_when_every_step_is_done(self):
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        self._mark_done("alpha", "beta", "gamma")
        with _quiet():
            self.assertFalse(self._check_pending(STEPS))

    def test_collect_with_a_missing_input_csv_fails(self):
        steps = [Step('repository_cloning', "python3",
                        ["c.py", "--repo-csv", str(self.tmp / "nope.csv")])]
        with _captured_stderr() as err:
            self.assertFalse(self._check_pending(steps))
        self.assertIn(f"{self.tmp / 'nope.csv'}, the list of repositories to collect, is not on "
                      f"disk (required by repository_cloning)", err.getvalue())

    def test_collect_with_an_existing_input_csv_passes(self):
        csv_path = self.tmp / "repos.csv"
        csv_path.write_text("repo_name\n")
        steps = [Step('repository_cloning', "python3", ["c.py", "--repo-csv", str(csv_path)])]
        with _quiet():
            self.assertTrue(self._check_pending(steps))

    def test_a_pending_engineered_project_selection_lets_collect_run_before_the_list_exists(self):
        # Over an empty data root, the sample list is absent, and the step that writes it
        # runs earlier in the same invocation. An unconditional check would refuse the run
        # that the step exists to make possible.
        sample = str(self.tmp / "sample.csv")
        steps = [Step('engineered_project_selection', "python3", ["f.py"], produces=(sample,)),
                  Step('repository_cloning', "python3", ["c.py", "--repo-csv", sample])]
        with _quiet():
            self.assertTrue(pre_checks(steps, {'engineered_project_selection',
                                                        'repository_cloning'}))

    def test_collect_alone_still_needs_the_list_on_disk(self):
        # The other direction, with the same step list. No pending step writes the file, and the
        # pre-checks have to stop a cloning step that would open a missing list before it clones
        # anything. An assertion of the case above alone would also pass for pre-checks without
        # the check.
        sample = str(self.tmp / "sample.csv")
        steps = [Step('engineered_project_selection', "python3", ["f.py"], produces=(sample,)),
                  Step('repository_cloning', "python3", ["c.py", "--repo-csv", sample])]
        with _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'repository_cloning'}))
        self.assertIn(f"{sample}, the list of repositories to collect, is not on disk",
                      err.getvalue())

    def test_a_repo_csv_no_pending_step_produces_is_still_checked(self):
        # `engineered_project_selection` writes only the default path. A run with another
        # --repo-csv gets the check whether or not `engineered_project_selection` is pending.
        steps = [Step('engineered_project_selection', "python3", ["f.py"], produces=(str(self.tmp / "sample.csv"),)),
                  Step('repository_cloning', "python3", ["c.py", "--repo-csv", str(self.tmp / "other.csv")])]
        with _captured_stderr() as err:
            self.assertFalse(pre_checks(steps, {'engineered_project_selection',
                                                         'repository_cloning'}))
        self.assertIn("other.csv", err.getvalue())

    def test_collect_input_csv_is_not_checked_once_collect_is_done(self):
        steps = [Step('repository_cloning', "python3",
                        ["c.py", "--repo-csv", str(self.tmp / "nope.csv")])]
        self._mark_done('repository_cloning')
        with _quiet():
            self.assertTrue(self._check_pending(steps))

    def test_an_interpreter_that_is_not_the_pinned_version_fails(self):
        # The step interpreter is not always this process. It is DATA_COLLECTION_PYTHON
        # when that is set, and a step can declare its own interpreter. The pinned
        # version applies to every interpreter, because a run could otherwise mix three
        # Python versions in one pipeline.
        self._pin("2.7")
        with _captured_stderr() as err:
            self.assertFalse(self._check_pending(STEPS))
        self.assertIn("mise.toml", err.getvalue())
        self.assertIn("pins Python 2.7", err.getvalue())

    def test_the_pinned_version_being_the_running_one_passes(self):
        self._pin(self._running_version())
        steps = [Step("alpha", sys.executable, ["a.py"])]
        with _quiet():
            self.assertTrue(self._check_pending(steps))

    def test_a_step_interpreter_that_does_not_report_its_version_fails(self):
        self._pin(self._running_version())
        steps = [Step("alpha", self._stub_interpreter(1), ["a.py"])]
        with _captured_stderr() as err:
            self.assertFalse(self._check_pending(steps))
        self.assertIn("did not report its Python version", err.getvalue())

    def test_no_version_file_checks_no_interpreter(self):
        # The case of the replication package. A step archive unpacked without the
        # repository root has no mise.toml and runs under whatever interpreter is there.
        steps = [Step("alpha", self._stub_interpreter(1), ["a.py"])]
        with _quiet():
            self.assertTrue(self._check_pending(steps))

    def test_the_detect_interpreter_without_lingua_warns_and_does_not_fail(self):
        """`agentcontext/steps/detection.py` runs without lingua and records `n/a` for every
        language that it has to measure, and it still writes the rest of every CSV. A failed
        pre-checks here would block an offline regeneration over a package that the other
        steps never load.

        This test and its negative counterpart both have to name `detection`, the step that the
        pre-checks treat as needing lingua. For any other step the pre-checks
        verify nothing, and the counterpart's assertion that nothing is logged would then pass for
        pre-checks that never checked."""
        _mod.VENV_PYTHON = self._stub_interpreter(1)
        steps = [Step('detection', _mod.VENV_PYTHON, ["d.py"])]
        with _captured_stderr() as err:
            self.assertTrue(self._check_pending(steps))
        self.assertIn("cannot import lingua", err.getvalue())
        self.assertIn("WARNING", err.getvalue())

    def test_the_detect_interpreter_with_lingua_logs_nothing(self):
        _mod.VENV_PYTHON = self._stub_interpreter(0)
        steps = [Step('detection', _mod.VENV_PYTHON, ["d.py"])]
        with _captured_stderr() as err:
            self.assertTrue(self._check_pending(steps))
        self.assertNotIn("lingua", err.getvalue())

    def test_a_step_outside_the_lingua_set_is_not_probed(self):
        # The case that the two tests above would test if the step that they name left
        # LINGUA_STEPS. The check never runs, and the log does not name the interpreter
        # that cannot import lingua.
        _mod.VENV_PYTHON = self._stub_interpreter(1)
        self.assertNotIn("output_cross_check", _mod.LINGUA_STEPS)
        steps = [Step("output_cross_check", _mod.VENV_PYTHON, ["c.py"])]
        with _captured_stderr() as err:
            self.assertTrue(self._check_pending(steps))
        self.assertNotIn("lingua", err.getvalue())

    def test_every_failure_is_reported_not_just_the_first(self):
        _mod.BINARY_REQUIREMENTS = {"definitely-not-a-real-binary": {'repository_cloning'}}
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        steps = [Step('repository_cloning', "python3",
                        ["c.py", "--repo-csv", str(self.tmp / "nope.csv")])]
        with _captured_stderr() as err:
            self.assertFalse(self._check_pending(steps))
        self.assertEqual(err.getvalue().count("ERROR pre_checks:"), 3)


# ---------------------------------------------------------------------------
# parse_step_arg / apply_step_overrides
# ---------------------------------------------------------------------------

class TestParseStepArg(unittest.TestCase):

    def test_name_and_tokens(self):
        self.assertEqual(parse_step_arg("repository_cloning:-w 10"), ('repository_cloning', ["-w", "10"]))

    def test_quoted_value_stays_one_token(self):
        self.assertEqual(parse_step_arg('repository_cloning:--repo-csv "my file.csv"'),
                         ('repository_cloning', ["--repo-csv", "my file.csv"]))

    def test_empty_argument_list(self):
        self.assertEqual(parse_step_arg("repository_cloning:"), ('repository_cloning', []))

    def test_only_the_first_colon_separates(self):
        self.assertEqual(parse_step_arg("repository_cloning:--url http://x"),
                         ('repository_cloning', ["--url", "http://x"]))

    def test_missing_colon_exits(self):
        with _quiet(), self.assertRaises(SystemExit) as cm:
            parse_step_arg("collect -w 10")
        self.assertEqual(cm.exception.code, 1)


class TestResolveStepPython(unittest.TestCase):
    """The detection step needs lingua, a third-party package, for language detection, and the
    runner names the interpreter that runs it: the one that runs the runner, which `mise exec`
    makes the interpreter of `.venv` with the distributions of locks/development.txt, unless
    DATA_COLLECTION_PYTHON names another.
    """

    def test_the_running_interpreter_runs_the_step(self):
        self.assertEqual(resolve_step_python({}, "/somewhere/.venv/bin/python"),
                         "/somewhere/.venv/bin/python")

    def test_a_virtual_environment_in_the_checkout_is_not_the_step_interpreter(self):
        """A `venv/` beside the code, such as one left from an earlier setup, holds distributions
        that no dependency lock lists, and the verification compares only the environment that
        runs the runner with the dependency lock."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("venv", ".venv"):
                python = root / name / "bin" / "python"
                python.parent.mkdir(parents=True)
                python.touch()
            with _swap(_mod, "REPO_ROOT", root):
                got = resolve_step_python({}, "/usr/bin/python3")
        self.assertEqual(got, "/usr/bin/python3")

    def test_env_override_wins_over_the_running_interpreter(self):
        got = resolve_step_python({"DATA_COLLECTION_PYTHON": "/opt/py"}, "/usr/bin/python3")
        self.assertEqual(got, "/opt/py")

    def test_blank_env_override_is_ignored(self):
        got = resolve_step_python({"DATA_COLLECTION_PYTHON": "  "}, "/usr/bin/python3")
        self.assertEqual(got, "/usr/bin/python3")


class TestApplyRepoCsv(unittest.TestCase):
    """--repo-csv passes the cloning step the list that the classification stage
    writes, repos_annotated_included.csv in that stage's data directory. Without a
    flag of its own, the only way to point the cloning step at the list is
    --step-arg repository_cloning:"--repo-csv ...". That form is easy to get wrong
    and reads like an internal detail rather than the pipeline's main input.
    """

    def test_flag_reaches_the_cloning_step(self):
        out = apply_repo_csv(STEPS, "/runs/run_001/repos_annotated_included.csv", "alpha")
        self.assertEqual(_step_argv(out, "alpha"),
                         ["alpha.py", "--repo-csv",
                          "/runs/run_001/repos_annotated_included.csv"])

    def test_none_leaves_the_steps_untouched(self):
        self.assertEqual(apply_repo_csv(STEPS, None, "alpha"), STEPS)

    def test_other_steps_are_untouched(self):
        out = apply_repo_csv(STEPS, "x.csv", "alpha")
        self.assertEqual(_step_argv(out, "beta"), ["beta.py", "-w", "5"])

    def test_the_original_list_is_not_modified(self):
        apply_repo_csv(STEPS, "x.csv", "alpha")
        self.assertEqual(_step_argv(STEPS, "alpha"), ["alpha.py"])

    def test_a_path_with_spaces_stays_one_argument(self):
        out = apply_repo_csv(STEPS, "/my runs/included.csv", "alpha")
        self.assertEqual(_step_argv(out, "alpha")[-1], "/my runs/included.csv")

    def test_the_pre_checks_read_the_value(self):
        """The flag must be where the pre-checks read it, because the pre-checks
        read --repo-csv from the cloning step's argv to check that the file exists."""
        out = apply_repo_csv(STEPS, "somewhere/included.csv", "alpha")
        self.assertEqual(
            _flag_value(_step_argv(out, "alpha"), "--repo-csv", "default.csv"),
            "somewhere/included.csv")


class TestApplyStepOverrides(unittest.TestCase):

    def test_tokens_are_appended_to_the_named_step(self):
        out = apply_step_overrides(STEPS, ["alpha:-w 10"])
        self.assertEqual(_step_argv(out, "alpha"), ["alpha.py", "-w", "10"])

    def test_other_steps_are_untouched(self):
        out = apply_step_overrides(STEPS, ["alpha:-w 10"])
        self.assertEqual(_step_argv(out, "beta"), ["beta.py", "-w", "5"])

    def test_the_original_list_is_not_modified(self):
        apply_step_overrides(STEPS, ["alpha:-w 10"])
        self.assertEqual(_step_argv(STEPS, "alpha"), ["alpha.py"])

    def test_repeated_overrides_accumulate_in_order(self):
        out = apply_step_overrides(STEPS, ["alpha:-w 10", "alpha:--fresh"])
        self.assertEqual(_step_argv(out, "alpha"), ["alpha.py", "-w", "10", "--fresh"])

    def test_no_overrides_returns_an_equivalent_list(self):
        self.assertEqual(apply_step_overrides(STEPS, []), STEPS)

    def test_unknown_step_exits(self):
        with _quiet(), self.assertRaises(SystemExit) as cm:
            apply_step_overrides(STEPS, ["delta:-w 10"])
        self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# drop_completed_from  (what --from stops counting as complete)
# ---------------------------------------------------------------------------

class TestClearMarkersFrom(_RunLogCase):

    def _state_with(self, *names) -> dict:
        self._mark_done(*names)
        return read_state()

    def test_the_named_step_and_everything_computed_from_it_is_dropped(self):
        state = self._state_with("alpha", "beta", "gamma")
        dropped = drop_completed_from(state, STEPS, "beta")
        self.assertEqual(dropped, {"beta", "gamma"})
        self.assertEqual(set(state["completed"]), {"alpha"})

    def test_dropping_from_the_first_step_drops_all(self):
        state = self._state_with("alpha", "beta", "gamma")
        drop_completed_from(state, STEPS, "alpha")
        self.assertEqual(state["completed"], {})

    def test_dropping_from_the_last_step_keeps_the_others(self):
        state = self._state_with("alpha", "beta", "gamma")
        drop_completed_from(state, STEPS, "gamma")
        self.assertEqual(set(state["completed"]), {"alpha", "beta"})

    def test_a_step_that_was_never_recorded_is_not_an_error(self):
        state = self._state_with("gamma")
        drop_completed_from(state, STEPS, "beta")
        self.assertEqual(state["completed"], {})

    def test_a_sibling_that_reads_nothing_keeps_its_record(self):
        state = {"completed": {k: "" for k in ("root", "left", "right", "leaf")}}
        drop_completed_from(state, BRANCHED, "left")
        self.assertEqual(set(state["completed"]), {"root", "right"})

    def test_it_writes_nothing_itself(self):
        # The caller writes the state. Dropping is one of several edits that main makes to
        # the state before the next write, and a write per edit would leave the file
        # describing a run that is only partly decided.
        state = self._state_with("alpha", "beta", "gamma")
        drop_completed_from(state, STEPS, "alpha")
        self.assertEqual(set(read_state()["completed"]),
                         {"alpha", "beta", "gamma"})

    def test_unknown_step_exits(self):
        with _quiet(), self.assertRaises(SystemExit) as cm:
            drop_completed_from({}, STEPS, "delta")
        self.assertEqual(cm.exception.code, 1)


# ---------------------------------------------------------------------------
# run_step  (skip when done, run and log, mark only on exit zero)
# ---------------------------------------------------------------------------

class TestRunStep(_RunLogCase):

    def setUp(self):
        super().setUp()
        # run_step runs its command in the step's data directory, STAGE_DIR for these
        # steps. Point STAGE_DIR at the temporary directory, so that a step that writes a
        # file writes it where the test removes it.
        self._orig_dir = _mod.STAGE_DIR
        _mod.STAGE_DIR = self.tmp

    def tearDown(self):
        _mod.STAGE_DIR = self._orig_dir
        super().tearDown()

    def _print_step(self, code: int = 0, text: str = "hello") -> "Step":
        """A Step named alpha that prints text and exits with code."""
        return Step("alpha", sys.executable,
                     ["-c", f"import sys; print({text!r}); sys.exit({code})"])

    def test_a_successful_step_returns_true(self):
        with _quiet():
            self.assertTrue(run_step(self._print_step(0)))

    def test_what_a_step_waits_for_is_in_the_state_while_it_waits(self):
        """The labeling can wait for credit over a weekend. The step states what it waits for in
        the file that its environment names, and the pipeline execution state shows it while
        the step waits. The step of this test reads the state before it goes on and exits 3 when
        the state does not show the wait. The state no longer shows it once the wait has ended."""
        body = ("import json, os, sys, time\n"
                "path = os.environ['AGENTCONTEXT_STEP_STATUS']\n"
                "with open(path, 'w') as f:\n"
                "    json.dump({'waiting_for': 'credit', 'service': 'OpenRouter'}, f)\n"
                "print('waiting', flush=True)\n"
                "deadline = time.time() + 10\n"
                "while time.time() < deadline:\n"
                "    try:\n"
                f"        state = json.load(open({str(self.state)!r}))\n"
                "    except (OSError, ValueError):\n"
                "        state = {}\n"
                "    if state.get('running_step_status', {}).get('waiting_for') == 'credit':\n"
                "        break\n"
                "    time.sleep(0.05)\n"
                "else:\n"
                "    sys.exit(3)\n"
                "os.unlink(path)\n"
                "print('continuing', flush=True)\n")
        state = {}
        with _quiet():
            ok = run_step(Step("alpha", sys.executable, ["-c", body]),
                          on_status=lambda status: _mod.record_step_status(state, status))
        self.assertTrue(ok)
        self.assertNotIn(_mod.RUNNING_STEP_STATUS, json.loads(self.state.read_text()))
        self.assertFalse(_mod.status_path("alpha").exists())

    def test_a_step_that_ends_while_it_waits_leaves_no_wait_in_the_state(self):
        body = ("import json, os\n"
                "with open(os.environ['AGENTCONTEXT_STEP_STATUS'], 'w') as f:\n"
                "    json.dump({'waiting_for': 'credit'}, f)\n"
                "print('waiting', flush=True)\n"
                "raise SystemExit(1)\n")
        recorded = []
        with _quiet():
            self.assertFalse(run_step(Step("alpha", sys.executable, ["-c", body]),
                                      on_status=recorded.append))
        self.assertEqual(recorded, [{"waiting_for": "credit"}, None])
        self.assertFalse(_mod.status_path("alpha").exists())

    def test_cloning_progress_uses_a_noun_and_keeps_the_log_filename(self):
        step = Step('repository_cloning', sys.executable, ["-c", "pass"])
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(run_step(step))
        self.assertIn("Repository cloning starting (", err.getvalue())
        self.assertIn("Repository cloning finished in", err.getvalue())
        self.assertTrue(_mod.log_path('repository_cloning').is_file())

    def test_the_selection_progress_names_the_engineered_project_selection(self):
        step = Step('engineered_project_selection', sys.executable, ["-c", "print('sample selected')"])
        out = io.StringIO()
        with _captured_stderr() as err, contextlib.redirect_stdout(out):
            self.assertTrue(run_step(step))
        self.assertIn("Engineered project selection finished in", err.getvalue())
        self.assertEqual(out.getvalue(), "INFO engineered_project_selection: sample selected\n")
        self.assertTrue(_mod.log_path('engineered_project_selection').is_file())

    def test_a_failing_step_returns_false(self):
        with _quiet():
            self.assertFalse(run_step(self._print_step(1)))

    def test_pre_checks_that_cannot_start_a_process_are_a_refusal_not_the_state_file(self):
        # A fork() that fails at the interpreter check, the imports check or the lingua
        # check is a refusal of the pre-checks, not an error of the handler for the state
        # file.
        with mock.patch.object(_mod.subprocess, "run",
                               side_effect=BlockingIOError(11, "Resource temporarily unavailable")):
            self.assertEqual(_mod.interpreter_version(sys.executable), "")
        step = Step("needs", sys.executable, ["-c", "pass"], imports=("json",))
        real = _mod.subprocess.run

        def refusing(cmd, *args, **kwargs):
            if "-c" in cmd and any(str(a).startswith("import ") for a in cmd):
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real(cmd, *args, **kwargs)

        with mock.patch.object(_mod.subprocess, "run", side_effect=refusing), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            ok = _mod.pre_checks([step], {"needs"}, no_github=True, min_free_gb=0)
        self.assertFalse(ok)
        self.assertIn("ERROR pre_checks: Could not start", err.getvalue())
        self.assertNotIn("state.json", err.getvalue())

    def test_a_step_that_cannot_be_started_is_a_failed_step_naming_itself(self):
        # A failed fork() or a missing interpreter must not reach the handler for the
        # state file, which would name that file.
        with mock.patch.object(_mod.subprocess, "Popen",
                               side_effect=BlockingIOError(11, "Resource temporarily unavailable")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            ok = run_step(self._print_step(0))
        self.assertFalse(ok)
        self.assertIn("ERROR runner: alpha could not be started", err.getvalue())
        self.assertNotIn("state.json", err.getvalue())

    def test_a_log_that_cannot_be_written_ends_the_step_and_names_the_log(self):
        """ENOSPC on the step's log must not reach the handler for the state file, which
        would name that file and leave the step running. The last flush of the buffer must
        not raise the same error again when the block closes the log.

        The runner keeps reading the step's output once the log fails, and the test waits
        for the runner with a bound. The cloning and file history steps drain on SIGTERM and
        keep printing. If the runner stopped reading, a drain larger than the pipe's buffer
        would block the step on a print and the runner on its wait indefinitely. The
        stand-in drains more than a pipe's buffer in the same way. Its signal handler only
        sets a flag, because a print in the handler can interrupt the first print while that
        print's buffered writer is locked, and Python rejects that as reentrant."""
        marker = self.tmp / "stage.pid"
        step = Step("noisy", sys.executable,
                      ["-c", "import os, signal, sys, time\n"
                             "stopping = False\n"
                             "def stop(*_):\n"
                             "    global stopping\n"
                             "    stopping = True\n"
                             "signal.signal(signal.SIGTERM, stop)\n"
                             f"open({str(marker)!r}, 'w').write(str(os.getpid()))\n"
                             "print('a line', flush=True)\n"
                             "while not stopping: time.sleep(0.01)\n"
                             "for i in range(1500):\n"
                             "    print('draining', i, 'x' * 80, flush=True)\n"
                             "sys.exit(1)\n"])
        real_open = open

        class FailingLog:
            def __init__(self):
                self.writes = 0

            def write(self, data):
                self.writes += 1
                raise OSError(28, "No space left on device", str(_mod.log_path("noisy")))

            def flush(self):
                raise OSError(28, "No space left on device", str(_mod.log_path("noisy")))

            def close(self):
                raise OSError(28, "No space left on device", str(_mod.log_path("noisy")))

        log = FailingLog()

        def opening(path, *args, **kwargs):
            if str(path) == str(_mod.log_path("noisy")) and args[:1] == ("wb",):
                return log
            return real_open(path, *args, **kwargs)

        err, out = io.StringIO(), io.StringIO()
        with mock.patch("builtins.open", side_effect=opening), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            started = time.monotonic()
            ok = _within(self, 15, lambda: run_step(step),
                         lambda: os.kill(int(marker.read_text()), signal.SIGKILL))
        self.assertFalse(ok)
        self.assertLess(time.monotonic() - started, 15, "the step was ended, not waited out")
        self.assertIn("draining 1499", out.getvalue(), "the drain did not reach the console")
        self.assertIn("ERROR runner: Could not write ", err.getvalue())
        self.assertIn("noisy.log, the log of noisy (", err.getvalue())
        self.assertGreaterEqual(log.writes, 1)
        pid = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, signal.SIGKILL)
            self.fail("the step was still running after the runner had logged the failure")

    def test_a_step_that_exits_0_after_its_log_failed_is_a_failed_step(self):
        """The log is the record of the step. A step that the runner signaled to stop because
        its log could not be written finishes its drain and can exit 0 with every declared
        path written. Its exit code alone would then record the step."""
        step = Step("quiet", sys.executable,
                    ["-c", "import signal, sys, time\n"
                           "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
                           "open('quiet.txt', 'w').write('written')\n"
                           "print('a line', flush=True)\n"
                           "time.sleep(30)\n"],
                    root=str(self.tmp), produces=("quiet.txt",))
        real_open = open

        class FailingLog:
            def write(self, data):
                raise OSError(28, "No space left on device", str(_mod.log_path("quiet")))

            def flush(self):
                raise OSError(28, "No space left on device", str(_mod.log_path("quiet")))

            def close(self):
                pass

        def opening(path, *args, **kwargs):
            if str(path) == str(_mod.log_path("quiet")) and args[:1] == ("wb",):
                return FailingLog()
            return real_open(path, *args, **kwargs)

        err = io.StringIO()
        with mock.patch("builtins.open", side_effect=opening), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            ok = _within(self, 15, lambda: run_step(step), lambda: None)
        self.assertEqual((self.tmp / "quiet.txt").read_text(), "written")
        self.assertFalse(ok)
        self.assertIn("ERROR runner: Could not write ", err.getvalue())
        self.assertIn("quiet.log, the log of quiet (", err.getvalue())

    def test_a_stop_that_arrives_while_the_step_is_starting_reaches_it(self):
        """A signal delivered inside `Popen`, before `_CHILD` names the new process, finds no
        running step and only sets the flag. The check after `Popen` therefore forwards the
        stop. Otherwise the runner would wait until the whole step ends."""
        started = []
        real_popen = _mod.subprocess.Popen

        def signalled_while_starting(*args, **kwargs):
            started.append(real_popen(*args, **kwargs))
            _mod._STOPPING = signal.SIGTERM
            return started[0]

        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        step = Step("slow", sys.executable, ["-c", "import time; time.sleep(30)"])
        with mock.patch.object(_mod.subprocess, "Popen", side_effect=signalled_while_starting), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            ok = _within(self, 10, lambda: run_step(step), lambda: started[0].kill())
        self.assertFalse(ok)
        self.assertEqual(started[0].returncode, -signal.SIGTERM)
        self.assertIn("A stop arrived while slow was starting", err.getvalue())

    def test_it_records_nothing_itself(self):
        # main records a completed step, not run_step, because the record is a decision
        # about the run's state and run_step only runs a process. TestMain covers the
        # recording.
        with _quiet():
            run_step(self._print_step(0))
        self.assertEqual(self._completed(), {})

    def test_output_is_written_to_the_step_log(self):
        with _quiet():
            run_step(self._print_step(0, "STAGE OUTPUT"))
        self.assertIn("STAGE OUTPUT", log_path("alpha").read_text())

    def test_output_reaches_the_console_as_well(self):
        # The log is the record, and the console is what an operator watches. In a
        # container, the console is all that the operator sees. With the output in the log
        # alone, the console would show that a step started and then nothing until the step
        # ended.
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            run_step(Step("alpha", sys.executable, [
                "-c", "import sys; print('TO STDOUT'); print('TO STDERR', file=sys.stderr)"]))
        self.assertIn("TO STDOUT", out.getvalue())
        self.assertIn("TO STDERR", out.getvalue())
        self.assertIn("TO STDERR", log_path("alpha").read_text())

    def test_a_line_that_is_not_utf8_reaches_both_without_an_error(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(run_step(Step("alpha", sys.executable, [
                "-c", "import sys; sys.stdout.buffer.write(b'caf\\xe9\\n')"])))
        self.assertIn("caf", out.getvalue())
        self.assertEqual(log_path("alpha").read_bytes(), b"INFO alpha: caf\xe9\n")

    def test_every_line_names_the_step_and_none_is_empty(self):
        """In the container's log, a plain sentence between the formatted lines would not name
        the step that wrote it, and an empty line is a row without content."""
        body = ("import sys; print('Wrote 3 rows'); print(); "
                "print('INFO caches: kept as it is'); "
                "print('WARNING alpha: to stderr', file=sys.stderr)")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(run_step(Step("alpha", sys.executable, ["-c", body])))
        expected = ["INFO alpha: Wrote 3 rows", "INFO caches: kept as it is",
                    "WARNING alpha: to stderr"]
        self.assertEqual(sorted(log_path("alpha").read_text().splitlines()), sorted(expected))
        self.assertEqual(sorted(out.getvalue().splitlines()), sorted(expected))

    # -- produces: exit 0 without the output is not success ---------------

    def _writing_step(self, *written: str, code: int = 0,
                       produces: tuple = ()) -> "Step":
        """A Step that creates `written` under cwd, then exits with code."""
        body = ("import pathlib, sys; "
                f"[pathlib.Path(p).write_text('x') for p in {list(written)!r}]; "
                f"sys.exit({code})")
        return Step("alpha", sys.executable, ["-c", body], produces=produces)

    def test_a_step_that_writes_its_declared_output_succeeds(self):
        with _quiet():
            self.assertTrue(run_step(
                self._writing_step("out.csv", produces=("out.csv",))))

    def test_exit_zero_without_the_output_fails(self):
        # The step exits 0 without writing its output, and the next step would read
        # whatever the previous run left on disk.
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(run_step(
                self._writing_step(produces=("out.csv",))))
        self.assertIn("did not write", err.getvalue())
        self.assertIn("out.csv", err.getvalue())

    def test_only_the_missing_outputs_are_named(self):
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()):
            run_step(self._writing_step(
                "here.csv", produces=("here.csv", "gone.csv")))
        # The test reads only the failure line, because the line that logs the start of the
        # step quotes the whole command, which here names the file that the step writes.
        message = "\n".join(l for l in err.getvalue().splitlines() if l.startswith("ERROR "))
        self.assertIn("gone.csv", message)
        self.assertNotIn("here.csv", message)

    def test_an_output_an_earlier_run_left_does_not_count(self):
        # `--step-arg collected_files_archive:"--dry-run"` writes no archive, but an
        # archive from an earlier run is on disk. A check of existence alone would mark the
        # step done and skip it from then on.
        stale = _mod.STAGE_DIR / "out.csv"
        stale.write_text("left by an earlier run")
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(run_step(
                self._writing_step(produces=("out.csv",))))
        self.assertIn("did not write", err.getvalue())
        self.assertIn("out.csv (unchanged since before the step ran)", err.getvalue())
        self.assertEqual(stale.read_text(), "left by an earlier run")

    def test_overwriting_an_output_that_was_already_there_succeeds(self):
        # The test is "this run wrote it", not "it is new", because every step
        # after repository cloning writes over the file that the previous run left.
        (_mod.STAGE_DIR / "out.csv").write_text("left by an earlier run")
        with _quiet():
            self.assertTrue(run_step(
                self._writing_step("out.csv", produces=("out.csv",))))

    def test_rewriting_an_output_with_the_same_bytes_still_counts(self):
        # agentcontext/steps/file_history.py rewrites the mechanism CSVs in place. A run
        # over a sample that the file history cache covers in full writes the same bytes
        # back. open('w') truncates in place, and only the mtime changes, not the inode or
        # the size. The check has to catch even this case, which changes the least.
        (_mod.STAGE_DIR / "out.csv").write_text("x")
        with _quiet():
            self.assertTrue(run_step(
                self._writing_step("out.csv", produces=("out.csv",))))

    def test_a_directory_counts_as_written(self):
        # Repository cloning and the collected file cleanup declare data/collected/ and
        # data/validated/, which are directories. The check must not require a regular file.
        step = Step("alpha", sys.executable,
                      ["-c", "import pathlib; pathlib.Path('d').mkdir()"],
                      produces=("d",))
        with _quiet():
            self.assertTrue(run_step(step))

    def test_a_directory_that_was_already_there_counts_as_written(self):
        # Repository cloning resumes from `finished_repositories.jsonl`. A directory
        # satisfies the check by existing, because a run that collects nothing again leaves
        # data/collected/ correct without changing the directory entry.
        (_mod.STAGE_DIR / "d").mkdir()
        step = Step("alpha", sys.executable, ["-c", "pass"], produces=("d",))
        with _quiet():
            self.assertTrue(run_step(step))

    def test_declaring_nothing_keeps_the_exit_code_the_only_test(self):
        with _quiet():
            self.assertTrue(run_step(self._writing_step()))

    def test_a_nonzero_exit_is_reported_as_the_exit_it_was(self):
        # A step that failed and wrote nothing must be logged with its exit code, not as a
        # missing output, because the exit code is the cause.
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(run_step(
                self._writing_step(code=1, produces=("out.csv",))))
        self.assertIn("exited 1", err.getvalue())
        self.assertNotIn("did not write", err.getvalue())

    def test_outputs_are_resolved_against_script_dir(self):
        # setUp points STAGE_DIR at the temporary directory, and the step runs in it. A file
        # written there must satisfy the check.
        with _quiet():
            run_step(self._writing_step("out.csv", produces=("out.csv",)))
        self.assertTrue((_mod.STAGE_DIR / "out.csv").exists())

    def test_log_is_rewritten_on_each_attempt(self):
        with _quiet():
            run_step(self._print_step(1, "FIRST"))
            run_step(self._print_step(1, "SECOND"))
        text = log_path("alpha").read_text()
        self.assertIn("SECOND", text)
        self.assertNotIn("FIRST", text)


# ---------------------------------------------------------------------------
# _git_info / read_state / write_state / write_execution_state  (the state file)
# ---------------------------------------------------------------------------

class TestGitInfo(unittest.TestCase):

    def test_returns_the_expected_keys(self):
        info = _git_info()
        self.assertEqual(set(info), {"git_sha", "git_dirty"})

    def test_reports_a_sha_inside_a_repo_and_None_outside_one(self):
        # The test also runs outside a checkout, because the replication archive is
        # extracted without one, and _git_info is documented to return there rather than
        # raise. An unconditional assertion of a sha would fail the test there.
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, cwd=str(_mod.STAGE_DIR))
        info = _git_info()
        if inside.returncode == 0 and inside.stdout.strip() == "true":
            self.assertIsInstance(info["git_sha"], str)
            self.assertEqual(len(info["git_sha"]), 40)
        else:
            self.assertIsNone(info["git_sha"])
            self.assertIsNone(info["git_dirty"])


    def test_an_image_without_git_names_the_commit_it_was_built_from(self):
        """The image has no `.git`. Its BUILD_INFO records the commit, and without that file a
        run in the image would record a null commit in its handover."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / _mod.BUILD_INFO).write_text(
                "commit 893f51bd4c7627c8baaede5a4b1a96394dc64613\nbuilt 2026-09-27T20:05:51Z\n"
                "python 3.14\n", encoding="utf-8")
            self.assertEqual(_git_info(root), {
                "git_sha": "893f51bd4c7627c8baaede5a4b1a96394dc64613", "git_dirty": False})
            (root / _mod.BUILD_INFO).write_text(
                "commit 893f51bd4c7627c8baaede5a4b1a96394dc64613-dirty\n", encoding="utf-8")
            self.assertEqual(_git_info(root), {
                "git_sha": "893f51bd4c7627c8baaede5a4b1a96394dc64613", "git_dirty": True},
                "deploy/build_image.sh marks a build from a tree with uncommitted changes")

    def test_an_image_built_without_a_commit_records_none(self):
        """The Dockerfile's default is `unrecorded`, which is not a commit."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / _mod.BUILD_INFO).write_text("commit unrecorded\n", encoding="utf-8")
            self.assertEqual(_git_info(root), {"git_sha": None, "git_dirty": None})
            (root / _mod.BUILD_INFO).unlink()
            self.assertEqual(_git_info(root), {"git_sha": None, "git_dirty": None})


class TestReadWriteState(_RunLogCase):

    def test_an_absent_file_reads_as_empty(self):
        self.assertEqual(read_state(), {})

    def test_what_was_written_is_what_is_read(self):
        write_state({"completed": {"alpha": "abc"}, "outcome": "success"})
        self.assertEqual(read_state(),
                         {"completed": {"alpha": "abc"}, "outcome": "success"})

    def test_a_layout_the_runner_never_writes_is_a_message_not_a_traceback(self):
        # docs/runner.md allows editing this file by hand, and the functions that
        # write into state["completed"] expect an object.
        for text in ('{"completed": []}', '{"completed": "collect"}', '[]', 'null',
                     '{"completed": {}, "completed_by": []}'):
            with self.subTest(text=text):
                self.state.write_text(text)
                with self.assertRaises(SystemExit) as caught:
                    read_state()
                self.assertIn("is valid JSON but not a pipeline execution state",
                              str(caught.exception))

    def test_a_truncated_file_is_a_hard_stop_not_an_empty_state(self):
        # The read is not a try/except that returns {}. A run killed while it writes
        # leaves a partial file, and reading that file as "no run has happened" would
        # discard the record of a long collection and start the collection again.
        self.state.write_text('{"completed": {"alpha"')
        with self.assertRaises(SystemExit) as cm:
            read_state()
        self.assertIn("could not be read", str(cm.exception))

    def test_the_message_states_how_to_start_over(self):
        self.state.write_text("not json at all")
        with self.assertRaises(SystemExit) as cm:
            read_state()
        self.assertIn("delete it to run every step again", str(cm.exception))

    def test_writing_the_state_is_atomic(self):
        """write_state writes beside the real name and renames the file, so that an
        interruption leaves either the old state or the new one, never part of either. A
        check only that no temporary file is left would pass a plain write to `state.json`.
        Here the write fails after part of the new state reached the temporary file, as on a
        full volume."""
        write_state({"completed": {"alpha": "abc"}})
        before = self.state.read_bytes()
        written = []
        real_fsync = os.fsync

        def failing_partway(fd):
            written.append(os.fstat(fd).st_size)
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.ENOSPC, "No space left on device")
            real_fsync(fd)

        with mock.patch.object(os, "fsync", failing_partway), self.assertRaises(OSError):
            write_state({"completed": {"alpha": "abc", "beta": "def"}})
        self.assertTrue(written and written[0] > 0, written)
        self.assertEqual(self.state.read_bytes(), before)

    def test_the_state_reaches_the_disk_before_its_rename(self):
        """After a power loss, a rename that reached the disk before the data would leave an
        empty or truncated `state.json`, and every later invocation would stop at
        `read_state`. write_state syncs the temporary file before it renames the file to
        `state.json`, and syncs the directory after the rename."""
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append(("fsync", os.fstat(fd).st_ino))
            real_fsync(fd)

        def replace(source, target):
            events.append(("replace", os.stat(source).st_ino, Path(target)))
            real_replace(source, target)
        with mock.patch.object(os, "fsync", fsync), mock.patch.object(os, "replace", replace):
            write_state({"completed": {"alpha": "abc"}})
        renames = [i for i, event in enumerate(events)
                   if event[0] == "replace" and event[2] == self.state]
        self.assertEqual(len(renames), 1, events)
        index = renames[0]
        self.assertIn(("fsync", events[index][1]), events[:index])
        self.assertIn(("fsync", self.log_dir.stat().st_ino), events[index + 1:])
        self.assertEqual(read_state(), {"completed": {"alpha": "abc"}})

    def test_no_temporary_file_is_left_behind(self):
        write_state({"completed": {"alpha": ""}})
        self.assertEqual(sorted(p.name for p in self.log_dir.iterdir()), ["state.json"])

    def test_writing_replaces_rather_than_merges(self):
        write_state({"completed": {"alpha": ""}})
        write_state({"completed": {"beta": ""}})
        self.assertEqual(read_state()["completed"], {"beta": ""})

    def test_an_unwritable_path_raises(self):
        # A write that failed without a message after a long step would leave the step
        # unrecorded. Here a file in place of the directory cannot contain the state.
        (self.tmp / "a-file").write_text("", encoding="utf-8")
        _mod.STATE_PATH = self.tmp / "a-file" / "state.json"
        with self.assertRaises(OSError):
            write_state({"completed": {}})


class TestRecordRun(_RunLogCase):

    def _record(self, results, outcome, state=None):
        state = {} if state is None else state
        with _quiet():
            write_execution_state(state, results, outcome, 1000.0, ["agentcontext/runner.py"])
        return read_state()

    def test_it_records_outcome_and_steps(self):
        m = self._record([{"name": "alpha", "status": "ok", "duration_seconds": 1.0}],
                         "success")
        self.assertEqual(m["outcome"], "success")
        self.assertEqual(m["steps"][0]["name"], "alpha")

    def test_the_recorded_command_can_be_run_again(self):
        # The file records the exact command. Without quoting, an argument that contains a
        # space would come back as `--step-arg collection:--repo-csv repos_subset.csv`,
        # which parses as --step-arg="collection:--repo-csv" plus a stray positional
        # argument.
        argv = ["agentcontext/runner.py", "--step-arg",
                "collection:--repo-csv repos_subset.csv"]
        with _quiet():
            write_execution_state({}, [], "success", 1000.0, argv)
        recorded = shlex.split(read_state()["command"])
        self.assertEqual(recorded[1:], argv)

    def test_it_records_provenance_and_command(self):
        m = self._record([], "running")
        for key in ("git_sha", "git_dirty", "command", "started_at",
                    "finished_at", "duration_seconds", "hostname",
                    "platform", "python_version"):
            self.assertIn(key, m)

    def test_started_at_reflects_the_argument(self):
        m = self._record([], "success")
        self.assertTrue(m["started_at"].startswith("1970-01-01T00:16:40"))

    def test_it_keeps_the_completed_half_intact(self):
        # Provenance and resume state share one file, and writing the provenance must not
        # drop the resume state.
        m = self._record([], "success", state={"completed": {"alpha": "abc"}})
        self.assertEqual(m["completed"], {"alpha": "abc"})


# ---------------------------------------------------------------------------
# script_fingerprint / completed_steps  (a result outlives its code)
# ---------------------------------------------------------------------------

class TestScriptFingerprint(_RunLogCase):

    def setUp(self):
        super().setUp()
        self._orig_dir = _mod.STAGE_DIR
        _mod.STAGE_DIR = self.tmp

    def tearDown(self):
        _mod.STAGE_DIR = self._orig_dir
        super().tearDown()

    def _script(self, name: str, body: str) -> "Step":
        (self.tmp / name).write_text(body)
        return Step("alpha", "python3", [name])

    def test_the_same_contents_hash_the_same(self):
        a = script_fingerprint(self._script("s.py", "print(1)"))
        b = script_fingerprint(self._script("s.py", "print(1)"))
        self.assertEqual(a, b)

    def test_changed_contents_hash_differently(self):
        a = script_fingerprint(self._script("s.py", "print(1)"))
        b = script_fingerprint(self._script("s.py", "print(2)"))
        self.assertNotEqual(a, b)

    def test_a_missing_script_hashes_to_empty(self):
        self.assertEqual(
            script_fingerprint(Step("alpha", "python3", ["gone.py"])), "")

    def test_a_step_with_no_args_hashes_to_empty(self):
        self.assertEqual(script_fingerprint(Step("alpha", "python3", [])), "")

    def test_the_arguments_are_not_part_of_it(self):
        # --step-arg appends for one run, and a worker count does not change
        # what a step produced. Hashing argv would invalidate a finished
        # collection for `-w 10`.
        (self.tmp / "s.py").write_text("print(1)")
        bare = script_fingerprint(Step("alpha", "python3", ["s.py"]))
        flagged = script_fingerprint(Step("alpha", "python3", ["s.py", "-w", "10"]))
        self.assertEqual(bare, flagged)

    def test_a_module_that_an_analysis_script_imports_is_part_of_it(self):
        """The analysis table of the imports takes its columns from `imports.STATUSES`. A new
        image that changes only their order rewrites no input of the analysis step. Without the
        module in the fingerprint, the step would stay recorded and the handover would keep the
        old table. The test uses a package of its own, in which only the module changes."""
        package = self.tmp / "agentcontext"
        package.mkdir()
        (package / "__init__.py").write_text("")
        module = package / "imports.py"
        module.write_text('STATUSES = ("found", "missing")\n')
        (self.tmp / "table.py").write_text("from agentcontext.imports import STATUSES\n"
                                           "print(STATUSES)\n")
        analysis = Step("alpha", "python3", ["table.py"], package_code=True)
        plain = Step("beta", "python3", ["table.py"])
        with mock.patch.object(_mod, "PACKAGE_DIR", package):
            before = (script_fingerprint(analysis), script_fingerprint(plain))
            module.write_text('STATUSES = ("missing", "found")\n')
            after = (script_fingerprint(analysis), script_fingerprint(plain))
            module.write_text('# The statuses.\nSTATUSES = ("missing",\n            "found")\n')
            commented = script_fingerprint(analysis)
        self.assertNotEqual(after[0], before[0])
        self.assertEqual(after[1], before[1], "a step without package_code keeps its record")
        self.assertEqual(commented, after[0], "a comment or the layout changes nothing")

    def test_a_comment_a_docstring_a_log_message_or_the_layout_leaves_it_unchanged(self):
        before = script_fingerprint(self._script(
            "s.py", "from agentcontext import logs\nx = [1, 2]\nlogs.info('s', 'one')\n"))
        after = script_fingerprint(self._script(
            "s.py", '"""What s does."""\nfrom agentcontext import logs\n# why\n'
                    "x = [1,\n     2]\nlogs.info('s', 'another message')\n"))
        self.assertEqual(before, after)

    def test_a_script_that_does_not_parse_still_changes_with_every_edit(self):
        """A script that does not parse cannot run, and the step fails when it runs. A record
        that kept matching would skip the step instead."""
        a = script_fingerprint(self._script("s.py", "def broken(:\n"))
        b = script_fingerprint(self._script("s.py", "def broken(: # x\n"))
        self.assertTrue(a)
        self.assertNotEqual(a, b)

    def test_a_module_among_the_code_inputs_is_hashed_as_code_and_a_prompt_by_its_bytes(self):
        """The modules of the labeling and the analysis are code that the script runs. A
        prompt is what the model reads, and a changed sentence in the prompt has to run the
        step."""
        (self.tmp / "s.py").write_text("print(1)\n")
        (self.tmp / "module.py").write_text("A = 1\n")
        (self.tmp / "prompt.md").write_text("Label the repository.\n")
        step = Step("alpha", "python3", ["s.py"], code_inputs=("module.py", "prompt.md"),
                    completion=COMPLETION_INPUTS)
        before = _mod.inputs_fingerprint(step)
        (self.tmp / "module.py").write_text("# The constant that s reads.\nA = 1\n")
        self.assertEqual(_mod.inputs_fingerprint(step), before)
        (self.tmp / "prompt.md").write_text("Label the repository. # a note\n")
        self.assertNotEqual(_mod.inputs_fingerprint(step), before)


# ---------------------------------------------------------------------------
# manifest_lines / output_fingerprint  (a step recorded by what it produced)
# ---------------------------------------------------------------------------

class _OutputCase(_RunLogCase):
    """Base class that points STAGE_DIR at a temporary directory, inside which a
    step's declared outputs resolve."""

    def setUp(self):
        super().setUp()
        self._orig_dir = _mod.STAGE_DIR
        _mod.STAGE_DIR = self.tmp

    def tearDown(self):
        _mod.STAGE_DIR = self._orig_dir
        super().tearDown()

    def _tree(self, name: str = "out") -> Path:
        """A small tree with a file, a nested file, and an empty directory."""
        root = self.tmp / name
        (root / "nested").mkdir(parents=True)
        (root / "top.txt").write_text("top")
        (root / "nested" / "deep.txt").write_text("deep")
        (root / "empty").mkdir()
        return root

    def _step(self, *produces) -> "Step":
        return Step("snap", "python3", ["snap.py"], produces=produces,
                     completion=COMPLETION_OUTPUT)


class TestManifestLines(_OutputCase):

    def test_a_tree_is_described_as_the_records_describe_it(self):
        """The record of the cloning step is the fingerprint of these lines. A change of their
        content or order makes a finished collection count as unfinished, and the next Start
        collects the sample again. The test writes out the expected lines and the fingerprint,
        so that a listing in another order fails here before it invalidates a record."""
        root = self.tmp / "out"
        (root / "d1" / "sub").mkdir(parents=True)
        (root / "d1" / "empty").mkdir()
        (root / "d1" / "a.txt").write_text("alpha\n")
        (root / "d1" / "b.bin").write_bytes(b"\x00\x01")
        (root / "d1" / "sub" / "c.md").write_text("# c\n")
        os.symlink("d1/a.txt", root / "l1")
        os.symlink("nowhere", root / "l2")
        os.symlink("d1", root / "ld")
        (root / ".DS_Store").write_bytes(b"\x00")
        (root / "d1" / ".DS_Store").mkdir()
        (root / "d1" / ".DS_Store" / "x").write_text("x\n")
        (root / "@eaDir").mkdir()
        (root / "@eaDir" / "a.txt@SynoEAStream").write_text("ea\n")
        (root / "d1" / "@eaDir").write_text("file\n")
        os.mkfifo(root / "pipe")
        self.assertEqual(manifest_lines(root, "out"), [
            "d out",
            "d out/d1",
            "d out/d1/.DS_Store",
            "f out/d1/.DS_Store/x 73cb3858a687a8494ca3323053016282f3dad39d42cf62ca4e79dda2aac7d9ac",
            "f out/d1/@eaDir 8b911a8716b94442f9ca3dff20584048536e4c2f47b8b5bb9096cbd43c3432d5",
            "f out/d1/a.txt b6a98d9ce9a2d9149288fa3df42d377c3e42737afdcdaf714e33c0a100b51060",
            "f out/d1/b.bin b413f47d13ee2fe6c845b2ee141af81de858df4ec549a58b7970bb96645bc8d2",
            "d out/d1/empty",
            "d out/d1/sub",
            "f out/d1/sub/c.md f1caac39f93cd4a96202d4d2d0dc3d578116a73b654a1a754a0d62f52d2522f3",
            "l out/l1 d1/a.txt",
            "l out/l2 nowhere",
            "l out/ld d1",
            "? out/pipe"])
        self.assertEqual(
            output_fingerprint(self._step("out")),
            "output:d82b2d1b743794f9b85239abb7af15efb96d65b90eaa551e945a1f8be4e84318")

    def test_a_tree_deeper_than_the_recursion_limit_is_described(self):
        """The collector copies a configuration directory whatever its depth. A listing that
        called itself once per level would raise RecursionError, which no caller catches. The
        finished cloning step would then stay unrecorded, and every Start would collect the
        sample again. The listing runs in a subprocess with a lower recursion limit, so that
        the tree stays within the path length of macOS."""
        deep = self.tmp / "out" / ".claude" / Path(*["a"] * 150)
        deep.mkdir(parents=True)
        (deep / "notes.md").write_text("deep\n")
        code = ("import json, sys\n"
                "from pathlib import Path\n"
                "from agentcontext import runner\n"
                "sys.setrecursionlimit(120)\n"
                "print(json.dumps(runner.manifest_lines(Path(sys.argv[1]), 'out')))\n")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            filter(None, [str(REPO_ROOT), os.environ.get("PYTHONPATH")]))}
        proc = subprocess.run([sys.executable, "-c", code, str(self.tmp / "out")], env=env,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(json.loads(proc.stdout), manifest_lines(self.tmp / "out", "out"))
        self.assertEqual(len(json.loads(proc.stdout)), 153)

    def test_every_entry_is_described_once(self):
        self._tree()
        lines = manifest_lines(self.tmp / "out", "out")
        kinds = [line.split(" ", 2)[0] for line in lines]
        self.assertEqual(kinds.count("d"), 3)   # out, out/empty, out/nested
        self.assertEqual(kinds.count("f"), 2)   # top.txt, deep.txt

    def test_a_directory_is_listed_even_when_it_is_empty(self):
        self._tree()
        self.assertIn("d out/empty", manifest_lines(self.tmp / "out", "out"))

    def test_the_order_does_not_depend_on_the_filesystem(self):
        root = self.tmp / "out"
        root.mkdir()
        for name in ("z.txt", "a.txt", "m.txt"):
            (root / name).write_text(name)
        names = [line.split(" ")[1] for line in manifest_lines(root, "out")]
        self.assertEqual(names, sorted(names))

    def test_a_file_is_described_by_its_contents(self):
        root = self.tmp / "out"
        root.mkdir()
        (root / "a.txt").write_text("one")
        before = manifest_lines(root, "out")
        (root / "a.txt").write_text("two")
        self.assertNotEqual(before, manifest_lines(root, "out"))

    def test_a_symlink_is_described_by_its_target_and_not_followed(self):
        root = self.tmp / "out"
        (root / "real").mkdir(parents=True)
        (root / "real" / "inside.txt").write_text("inside")
        os.symlink("real", root / "link")
        lines = manifest_lines(root, "out")
        self.assertIn("l out/link real", lines)
        # Following it would list the target's contents a second time, under
        # the link's name.
        self.assertNotIn("f out/link/inside.txt", [" ".join(x.split(" ")[:2]) for x in lines])

    def test_a_dangling_symlink_is_recorded_rather_than_raising(self):
        root = self.tmp / "out"
        root.mkdir()
        os.symlink("nowhere", root / "link")
        self.assertIn("l out/link nowhere", manifest_lines(root, "out"))

    def test_a_retargeted_symlink_changes_the_description(self):
        root = self.tmp / "out"
        root.mkdir()
        os.symlink("here", root / "link")
        before = manifest_lines(root, "out")
        (root / "link").unlink()
        os.symlink("there", root / "link")
        self.assertNotEqual(before, manifest_lines(root, "out"))

    def test_a_file_larger_than_one_block_is_hashed_whole(self):
        # The read loop takes 1 MiB at a time, and the cloning step's own declared output,
        # data/collected/repos.csv, can be larger than that. A hash that stopped after the
        # first block would pass every other test here.
        root = self.tmp / "out"
        root.mkdir()
        big = root / "big.bin"
        big.write_bytes(b"a" * (1 << 20) + b"tail")
        before = manifest_lines(root, "out")
        big.write_bytes(b"a" * (1 << 20) + b"TAIL")
        self.assertNotEqual(before, manifest_lines(root, "out"))

    def test_a_name_that_is_not_valid_utf8_does_not_raise(self):
        # os.listdir returns such a name with surrogates in it, and plain
        # str.encode rejects them. Linux allows the name, and the sample
        # consists of arbitrary GitHub repositories.
        step = self._step("out")
        root = self._tree()
        try:
            (root / b"bad\xff.md".decode("utf-8", "surrogateescape")).write_text("x")
        except (UnicodeEncodeError, OSError):
            self.skipTest("this filesystem will not store a name that is not UTF-8")
        self.assertTrue(output_fingerprint(step).startswith("output:"))

    def test_a_name_the_pipeline_discards_is_left_out(self):
        # Opening the collected tree in the Finder writes .DS_Store, and the validator
        # deletes the file before any step reads the tree. If the manifest counted the
        # file, repository cloning would count as incomplete, and the collector would run
        # again over its whole input list.
        root = self._tree()
        before = manifest_lines(root, "out")
        (root / ".DS_Store").write_bytes(b"\x00\x01")
        (root / "nested" / ".DS_Store").write_bytes(b"\x00\x02")
        self.assertEqual(before, manifest_lines(root, "out"))

    def test_a_directory_with_a_discarded_name_is_not_skipped(self):
        # The validator rejects `.DS_Store` as a file but keeps
        # `.claude/.DS_Store/settings.json`, because it descends into a directory of that
        # name rather than removing it. The files in such a directory reach the validated
        # tree, are classified and reach a published row. A manifest that skipped entries
        # by name alone would hide these files from the fingerprint while they change
        # published values.
        root = self._tree()
        before = manifest_lines(root, "out")
        (root / ".DS_Store").mkdir()
        (root / ".DS_Store" / "settings.json").write_text("{}")
        self.assertNotEqual(before, manifest_lines(root, "out"))

    def test_a_link_with_a_discarded_name_is_kept_and_described_as_a_link(self):
        # Dropping an entry that the pipeline would have kept hides a change in the
        # sample. The link itself is still described by its target and not
        # followed.
        root = self.tmp / "out"
        (root / "real").mkdir(parents=True)
        (root / "real" / "a.md").write_text("x")
        os.symlink(root / "real", root / ".DS_Store")
        lines = manifest_lines(root, "out")
        self.assertTrue(any(line.startswith("l out/.DS_Store ") for line in lines))
        self.assertEqual(1, sum(1 for line in lines if line.startswith("f ")))

    def test_a_directory_with_a_discarded_name_counts_as_an_entry(self):
        root = self.tmp / "out"
        root.mkdir()
        (root / ".DS_Store").mkdir()
        self.assertEqual(counted_entries(root), [".DS_Store"])

    def test_a_file_beside_a_discarded_one_still_counts(self):
        root = self._tree()
        before = manifest_lines(root, "out")
        (root / ".DS_Store").write_bytes(b"\x00")
        (root / "real.md").write_text("real")
        self.assertNotEqual(before, manifest_lines(root, "out"))

    def test_a_single_file_describes_itself(self):
        (self.tmp / "one.csv").write_text("a,b\n")
        lines = manifest_lines(self.tmp / "one.csv", "one.csv")
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("f one.csv "))

    def test_a_synology_directory_leaves_the_cloning_step_counted_as_done(self):
        """Synology DSM writes `@eaDir` into the directories that File Station opens, and the
        validator deletes it. If the manifest counted the directory, the cloning step would
        count as unfinished, and the next run would run the collector again over its whole
        input list."""
        collected = self.tmp / "data" / "collected"
        (collected / "o§r").mkdir(parents=True)
        (collected / "o§r" / "CLAUDE.md").write_text("# r\n")
        step = Step("repository_cloning", "python3", ["c.py"], produces=("data/collected",),
                    completion=COMPLETION_OUTPUT)
        state = {"completed": {step.key: step_fingerprint(step)}}
        (collected / "o§r" / "@eaDir").mkdir()
        (collected / "o§r" / "@eaDir" / "x").write_bytes(b"\x00")
        (collected / "@eaDir").mkdir()
        with _quiet():
            self.assertEqual(completed_steps([step], state), {step.key})
        self.assertEqual(counted_entries(collected), ["o§r"])

    def test_a_file_or_a_link_with_a_discarded_directory_name_is_described(self):
        """The validator deletes a directory with the name, and it judges a link or a file with
        the name as any other entry. The manifest therefore describes such a link or file."""
        root = self.tmp / "out"
        (root / "real").mkdir(parents=True)
        (root / "nested").mkdir()
        os.symlink(root / "real", root / "@eaDir")
        (root / "nested" / "@eaDir").write_text("x")
        lines = manifest_lines(root, "out")
        self.assertTrue(any(line.startswith("l out/@eaDir ") for line in lines))
        self.assertTrue(any(line.startswith("f out/nested/@eaDir ") for line in lines))


class TestOutputFingerprint(_OutputCase):

    def test_the_same_tree_fingerprints_the_same(self):
        self._tree()
        step = self._step("out")
        self.assertEqual(output_fingerprint(step), output_fingerprint(step))

    def test_it_is_labeled_so_a_record_states_what_kind_it_is(self):
        self._tree()
        self.assertTrue(output_fingerprint(self._step("out")).startswith("output:"))

    def test_a_changed_byte_changes_it(self):
        root = self._tree()
        before = output_fingerprint(self._step("out"))
        (root / "top.txt").write_text("TOP")
        self.assertNotEqual(before, output_fingerprint(self._step("out")))

    def test_a_new_file_changes_it(self):
        root = self._tree()
        before = output_fingerprint(self._step("out"))
        (root / "extra.txt").write_text("extra")
        self.assertNotEqual(before, output_fingerprint(self._step("out")))

    def test_a_removed_file_changes_it(self):
        root = self._tree()
        before = output_fingerprint(self._step("out"))
        (root / "top.txt").unlink()
        self.assertNotEqual(before, output_fingerprint(self._step("out")))

    def test_a_new_mtime_over_the_same_bytes_does_not_change_it(self):
        # The cloning step's record depends on this property. The step's output can be
        # restored from an archive, and every file in it then has the extraction time, not
        # the collection time.
        root = self._tree()
        before = output_fingerprint(self._step("out"))
        for path in sorted(root.rglob("*")):
            os.utime(path, (0, 0))
        self.assertEqual(before, output_fingerprint(self._step("out")))

    def test_an_output_a_later_step_extends_counts_by_being_there(self):
        root = self._tree()
        (self.tmp / "cache.csv").write_text("a\n")
        step = Step("snap", "python3", ["snap.py"], produces=("out", "cache.csv"),
                      extended=("cache.csv",), completion=COMPLETION_OUTPUT)
        before = output_fingerprint(step)
        (self.tmp / "cache.csv").write_text("a\nb\n")
        self.assertEqual(before, output_fingerprint(step))
        (root / "top.txt").write_text("TOP")
        self.assertNotEqual(before, output_fingerprint(step))
        (self.tmp / "cache.csv").unlink()
        self.assertEqual(output_fingerprint(step), "")

    def test_collect_stays_done_when_the_git_step_adds_to_its_caches(self):
        """The file history step adds the rows of the files that it measures to
        `file_history.csv` and `ai_authorship.csv`, which the cloning step declares. If the
        record covered the contents of the two caches, they would invalidate the cloning step
        at the next Start. That Start would clone every repository in
        `finished_repositories.jsonl` again and run every step after it. The test uses the
        cloning step's declaration from `CORE_STEPS`."""
        collect = next(s for s in _mod.CORE_STEPS if s.key == 'repository_cloning')
        (self.tmp / "data" / "collected" / "o§r").mkdir(parents=True)
        (self.tmp / "data" / "collected" / "o§r" / "CLAUDE.md").write_text("# c\n")
        (self.tmp / "data" / "imported").mkdir(parents=True)
        for rel in ("data/collected/repos.csv", "file_history.csv", "submodules.csv",
                    "ai_authorship.csv"):
            (self.tmp / rel).write_text("header\n")
        before = output_fingerprint(collect)
        for rel in ("file_history.csv", "ai_authorship.csv"):
            (self.tmp / rel).write_text("header\nrow the file history step measured\n")
        self.assertEqual(before, output_fingerprint(collect))
        (self.tmp / "data" / "collected" / "repos.csv").write_text("header\nrow\n")
        self.assertNotEqual(before, output_fingerprint(collect))

    def test_the_imported_tree_is_part_of_the_record_of_the_cloning_step(self):
        """The import detection step reads the imported tree that the cloning step wrote. A
        tree that lost a file, or that is gone, makes the cloning step count as not done. The
        next Start then runs the cloning step, rather than stopping at the import detection
        step or publishing rows that are `missing` without a warning."""
        collect = next(s for s in _mod.CORE_STEPS if s.key == 'repository_cloning')
        (self.tmp / "data" / "collected" / "o§r").mkdir(parents=True)
        (self.tmp / "data" / "collected" / "o§r" / "CLAUDE.md").write_text("# c\n@docs/a.md\n")
        imported = self.tmp / "data" / "imported" / "o§r" / "docs"
        imported.mkdir(parents=True)
        (imported / "a.md").write_text("A\n")
        for rel in ("data/collected/repos.csv", "file_history.csv", "submodules.csv",
                    "ai_authorship.csv"):
            (self.tmp / rel).write_text("header\n")
        before = output_fingerprint(collect)
        self.assertNotEqual(before, "")
        (imported / "a.md").unlink()
        self.assertNotEqual(output_fingerprint(collect), before)
        shutil.rmtree(self.tmp / "data" / "imported")
        self.assertEqual(output_fingerprint(collect), "")

    def test_an_extended_output_has_to_be_a_declared_one(self):
        with self.assertRaises(ValueError):
            Step("snap", "python3", ["snap.py"], produces=("out",), extended=("cache.csv",),
                  completion=COMPLETION_OUTPUT)
        with self.assertRaises(ValueError):
            Step("snap", "python3", ["snap.py"], produces=("cache.csv",),
                  extended=("cache.csv",), completion=COMPLETION_OUTPUT)
        with self.assertRaises(ValueError):
            Step("snap", "python3", ["snap.py"], produces=("out",), may_be_empty=("tree",),
                  completion=COMPLETION_OUTPUT)

    def test_every_declared_output_is_covered(self):
        self._tree()
        (self.tmp / "list.csv").write_text("a\n")
        before = output_fingerprint(self._step("out", "list.csv"))
        (self.tmp / "list.csv").write_text("b\n")
        self.assertNotEqual(before, output_fingerprint(self._step("out", "list.csv")))

    def test_a_symlink_target_cannot_forge_another_entry(self):
        # The fingerprint hashes the manifest lines with a length prefix rather than joined
        # by newlines, because a symlink target is arbitrary text from an arbitrary GitHub
        # repository and can contain a newline. With lines joined by newlines, a tree that
        # contains one link would hash the same as a tree that contains a link and a file
        # that the first link's target names.
        empty = hashlib.sha256(b"").hexdigest()
        step = self._step("out")
        root = self.tmp / "out"

        root.mkdir()
        os.symlink(f"t\nf out/y {empty}", root / "link")
        forged = output_fingerprint(step)
        shutil.rmtree(root)

        root.mkdir()
        os.symlink("t", root / "link")
        (root / "y").write_text("")
        self.assertNotEqual(forged, output_fingerprint(step))

    def test_a_produces_entry_that_is_a_link_describes_what_it_points_at(self):
        # The usual layout puts the sample on another volume and links to it, as the
        # container does. If the manifest described the link as a symlink, it would be one
        # line that names the mount point, the same whether the mount contains the sample,
        # contains a different snapshot or has been unmounted.
        real = self.tmp / "mount" / "sample"
        (real / "owner§repo").mkdir(parents=True)
        (real / "owner§repo" / "CLAUDE.md").write_text("first")
        os.symlink(real, self.tmp / "linked")
        step = self._step("linked")
        before = output_fingerprint(step)
        (real / "owner§repo" / "CLAUDE.md").write_text("a different snapshot")
        self.assertNotEqual(before, output_fingerprint(step))

    def test_a_link_whose_target_is_gone_fingerprints_to_nothing(self):
        real = self.tmp / "mount"
        real.mkdir()
        os.symlink(real, self.tmp / "linked")
        step = self._step("linked")
        self.assertNotEqual(output_fingerprint(step), "")
        real.rmdir()
        self.assertEqual(output_fingerprint(step), "")

    def test_the_label_is_the_declared_name_not_the_link_target(self):
        # Moving the tree and pointing the link at an identical copy leaves the record
        # valid.
        for where in ("first", "second"):
            target = self.tmp / where
            (target / "a").mkdir(parents=True)
            (target / "a" / "f.md").write_text("same")
        os.symlink(self.tmp / "first", self.tmp / "linked")
        step = self._step("linked")
        before = output_fingerprint(step)
        (self.tmp / "linked").unlink()
        os.symlink(self.tmp / "second", self.tmp / "linked")
        self.assertEqual(before, output_fingerprint(step))

    def test_an_unreadable_mount_is_a_hard_stop_not_a_missing_one(self):
        # entry_kind re-raises every errno except ENOENT, ENOTDIR, EBADF and ELOOP. The
        # re-raised errors include the errors of a mount that is present but not working.
        # Outside the try, they would escape as an OSError that main logs as a failure to
        # write the state file. entry_kind uses os.lstat, because Path.exists behaves
        # differently across interpreters. Through 3.13, Path.exists re-raised such errors,
        # and 3.14 returns False for anything that it cannot stat, which reads a stalled
        # mount as absent.
        parent = self.tmp / "mount"
        (parent / "sample").mkdir(parents=True)
        (parent / "sample" / "a.txt").write_text("x")
        os.chmod(parent, 0o000)
        try:
            try:
                os.listdir(parent)
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            with self.assertRaises(UnreadableOutput) as caught:
                output_fingerprint(self._step("mount/sample"))
            self.assertEqual(caught.exception.rel, "mount/sample")
        finally:
            os.chmod(parent, 0o755)

    def test_an_absent_output_fingerprints_to_nothing(self):
        self.assertEqual(output_fingerprint(self._step("gone")), "")

    def test_one_absent_output_out_of_two_fingerprints_to_nothing(self):
        self._tree()
        self.assertEqual(output_fingerprint(self._step("out", "gone.csv")), "")

    def test_an_unreadable_tree_is_a_hard_stop_not_a_verdict(self):
        # A verdict of "not done" here would run the step again over output that is on
        # disk, which for repository cloning means cloning the sample again.
        root = self._tree()
        blocked = root / "nested"
        os.chmod(blocked, 0o000)
        try:
            try:
                os.listdir(blocked)
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            with self.assertRaises(UnreadableOutput) as caught:
                output_fingerprint(self._step("out"))
            self.assertEqual(caught.exception.rel, "out")
        finally:
            os.chmod(blocked, 0o755)


class TestUnreadableOutputIsPhrasedByEachCaller(_OutputCase):
    """What each caller logs about an output that is on disk but cannot be read.

    `output_fingerprint` raises a typed error, and each caller phrases the message, so
    that `--mark-complete` does not quote the wording of the resume inside its own
    message.
    """

    def _blocked(self):
        root = self._tree()
        os.chmod(root / "nested", 0o000)
        try:
            os.listdir(root / "nested")
        except PermissionError:
            return root / "nested"
        os.chmod(root / "nested", 0o755)
        self.skipTest("this filesystem or user ignores the mode")

    def test_the_fingerprint_raises_a_typed_error_with_the_cause(self):
        blocked = self._blocked()
        try:
            with self.assertRaises(UnreadableOutput) as caught:
                output_fingerprint(self._step("out"))
            self.assertEqual(caught.exception.rel, "out")
            self.assertEqual(caught.exception.step_key, "snap")
            self.assertIsInstance(caught.exception.error, OSError)
        finally:
            os.chmod(blocked, 0o755)

    def test_the_resume_side_turns_it_into_the_run_stopping(self):
        blocked = self._blocked()
        try:
            with self.assertRaises(SystemExit) as caught:
                step_fingerprint_or_stop(self._step("out"))
            # The message is a plain sentence, because the runner logs it with its component
            # where it catches it. The check therefore does not put the log format into the
            # message that it raises.
            self.assertIn("Could not read out of step",
                          str(caught.exception))
            self.assertIn("Whether the step is complete depends on it",
                          str(caught.exception))
        finally:
            os.chmod(blocked, 0o755)

    def test_a_script_step_passes_straight_through(self):
        (self.tmp / "snap.py").write_text("print(1)")
        step = Step("snap", "python3", ["snap.py"])
        self.assertEqual(step_fingerprint_or_stop(step), script_fingerprint(step))


class TestStepFingerprint(_OutputCase):
    """Which of the two fingerprints a step gets, from its own `completion`."""

    def test_a_script_step_gets_its_scripts_hash(self):
        (self.tmp / "snap.py").write_text("print(1)")
        step = Step("snap", "python3", ["snap.py"])
        self.assertEqual(step_fingerprint(step), script_fingerprint(step))

    def test_an_output_step_gets_its_output_manifest(self):
        self._tree()
        (self.tmp / "snap.py").write_text("print(1)")
        step = self._step("out")
        self.assertEqual(step_fingerprint(step), output_fingerprint(step))
        self.assertNotEqual(step_fingerprint(step), script_fingerprint(step))

    def test_the_subject_named_on_a_mismatch_is_the_thing_that_changed(self):
        self.assertEqual(fingerprint_subject(Step("snap", "python3", ["snap.py"])),
                         "snap.py")
        self.assertEqual(fingerprint_subject(self._step("out", "list.csv")),
                         "The output of snap (out and list.csv)")


class TestRealPipelineCompletion(unittest.TestCase):
    """Which of the real steps is recorded by its output. `repository_cloning` is the
    only one, because its result cannot be derived again from what is on disk."""

    def test_collect_is_recorded_by_what_it_produced(self):
        collect = next(s for s in _mod.CORE_STEPS if s.key == 'repository_cloning')
        self.assertEqual(collect.completion, COMPLETION_OUTPUT)
        self.assertEqual(collect.produces,
                         ("data/collected/repos.csv", "data/collected", "data/imported",
                          "file_history.csv", "submodules.csv", "ai_authorship.csv"))

    def test_every_other_step_is_recorded_by_its_script(self):
        # By its script alone, or by its script and the heuristics that it reads.
        # TestTheHeuristicsAreARecordedInput lists the steps that read a heuristics file.
        for step in _mod.steps_for(True):
            if step.key == 'repository_cloning':
                continue
            with self.subTest(step=step.key):
                self.assertIn(step.completion, (COMPLETION_SCRIPT, COMPLETION_INPUTS))
                if step.completion == COMPLETION_INPUTS:
                    self.assertTrue(step.inputs or step.heuristics,
                                    "an inputs step names what it reads")


# ---------------------------------------------------------------------------
# mark_complete  (recording a step from output that is already on disk)
# ---------------------------------------------------------------------------

class TestMarkComplete(_OutputCase):

    def setUp(self):
        super().setUp()
        self.steps = [self._step("out", "list.csv"),
                       Step("after", "python3", ["after.py"], consumes=("snap",))]
        (self.tmp / "after.py").write_text("print(1)")

    def _ready(self) -> None:
        self._tree()
        (self.tmp / "list.csv").write_text("a\n")

    def _mark(self, name: str) -> int:
        return mark_complete(self.steps, name, 0.0)

    def test_it_records_the_step_from_the_output_on_disk(self):
        self._ready()
        with _quiet():
            self.assertEqual(self._mark("snap"), 0)
        self.assertEqual(self._completed()["snap"],
                         output_fingerprint(self.steps[0]))

    def test_the_recorded_step_then_counts_as_done(self):
        self._ready()
        with _quiet():
            self._mark("snap")
        self.assertEqual(completed_steps(self.steps, read_state()), {"snap"})

    def test_it_refuses_when_the_output_is_absent(self):
        with _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertIn("out", err.getvalue())
        self.assertFalse(self.state.exists())

    def test_it_refuses_when_only_part_of_the_output_is_there(self):
        self._tree()                      # out/ but no list.csv
        with _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertIn("list.csv", err.getvalue())
        self.assertFalse(self.state.exists())

    def test_it_refuses_an_empty_declared_directory(self):
        # The README's recipe builds data/collected/ with mkdir followed by two mv
        # commands. If the second mv fails, the tree lacks a whole archive. Recording that
        # tree would make every later run skip the step and build the dataset from what is
        # left.
        (self.tmp / "out").mkdir()
        (self.tmp / "list.csv").write_text("a\n")
        with _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertIn("is empty", err.getvalue())
        self.assertFalse(self.state.exists())

    def test_it_reports_how_many_entries_a_declared_directory_contains(self):
        # The operator compares the count with the number that the README
        # documents.
        self._ready()
        with _captured_stderr() as err:
            self._mark("snap")
        self.assertIn("out (3 entries)", err.getvalue())

    def test_a_discarded_name_does_not_change_the_reported_count(self):
        # The fingerprint stays the same after the Finder writes .DS_Store, and the line
        # logged beside it must not show one more entry.
        self._ready()
        with _captured_stderr() as err:
            self._mark("snap")
        before = err.getvalue()
        (self.tmp / "out" / ".DS_Store").write_bytes(b"\x00")
        with _captured_stderr() as err:
            self._mark("snap")
        self.assertIn("out (3 entries)", before)
        self.assertIn("out (3 entries)", err.getvalue())

    def test_a_directory_containing_only_discarded_names_is_empty(self):
        # The refusal exists for a partly restored tree, here on a machine where the Finder
        # writes .DS_Store into it.
        (self.tmp / "out").mkdir()
        (self.tmp / "out" / ".DS_Store").write_bytes(b"\x00")
        (self.tmp / "list.csv").write_text("a\n")
        with _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertIn("is empty", err.getvalue())
        self.assertFalse(self.state.exists())

    def test_the_count_is_read_through_a_link(self):
        (self.tmp / "list.csv").write_text("a\n")
        elsewhere = self.tmp / "elsewhere"
        (elsewhere / "a").mkdir(parents=True)
        (elsewhere / "b").mkdir()
        os.symlink(elsewhere, self.tmp / "out")
        with _captured_stderr() as err:
            self._mark("snap")
        self.assertIn("out (2 entries)", err.getvalue())

    def test_it_refuses_a_step_recorded_by_its_script(self):
        # For such a step, the recorded value is a hash of a script, which does
        # not describe the files just checked. Marking it would accept whatever
        # output is there and disable the check for changed code.
        with _captured_stderr() as err:
            self.assertEqual(self._mark("after"), 1)
        self.assertIn("recorded by the hash of its script", err.getvalue())
        self.assertFalse(self.state.exists())

    def test_the_refusal_names_the_steps_that_can_be_marked(self):
        with _captured_stderr() as err:
            self._mark("after")
        self.assertIn("snap", err.getvalue())

    def test_it_refuses_output_that_is_a_link_pointing_at_nothing(self):
        # If the check counted a dangling link as present because is_symlink() is true, it
        # would record the link as a finished collection.
        (self.tmp / "list.csv").write_text("a\n")
        os.symlink(self.tmp / "gone", self.tmp / "out")
        with _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertIn("out", err.getvalue())
        self.assertFalse(self.state.exists())

    def test_it_refuses_an_empty_directory_reached_through_a_link(self):
        (self.tmp / "list.csv").write_text("a\n")
        (self.tmp / "elsewhere").mkdir()
        os.symlink(self.tmp / "elsewhere", self.tmp / "out")
        with _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertIn("is empty", err.getvalue())

    def test_an_unreadable_mount_is_reported_as_itself(self):
        # An OSError that escaped mark_complete would reach main's handler, which logs a
        # failure to write the state file and would name the sample as the file that it
        # could not write. --mark-complete is the documented command for a restored sample,
        # and resolve_produced exists for the layout with the sample on another volume.
        (self.tmp / "list.csv").write_text("a\n")
        mount = self.tmp / "mount"
        (mount / "sample").mkdir(parents=True)
        (mount / "sample" / "a.txt").write_text("x")
        os.symlink(mount / "sample", self.tmp / "out")
        os.chmod(mount, 0o000)
        try:
            try:
                os.listdir(mount)
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            with _captured_stderr() as err:
                self.assertEqual(self._mark("snap"), 1)
            self.assertIn("Could not read an output of step 'snap'", err.getvalue())
            self.assertNotIn("could not write", err.getvalue())
            self.assertFalse(self.state.exists())
        finally:
            os.chmod(mount, 0o755)

    def test_an_unreadable_directory_inside_the_output_is_reported_the_same_way(self):
        # Unlike the root, a directory one level down is caught by output_fingerprint, with
        # a message written for a resume. That message states that the step would run again
        # over output that is already there, which is the wrong advice for a command that
        # runs nothing.
        self._ready()
        inner = self.tmp / "out" / "nested"
        os.chmod(inner, 0o000)
        try:
            try:
                os.listdir(inner)
            except PermissionError:
                pass
            else:
                self.skipTest("this filesystem or user ignores the mode")
            with _captured_stderr() as err:
                self.assertEqual(self._mark("snap"), 1)
            self.assertIn("ERROR runner: Could not read an output of step 'snap'",
                          err.getvalue())
            self.assertNotIn("cannot determine whether the step is complete", err.getvalue())
            self.assertFalse(self.state.exists())
        finally:
            os.chmod(inner, 0o755)

    def test_nothing_is_written_when_the_description_cannot_be_read(self):
        # _describe_output runs before the state file is written. If it ran after the
        # write, an error in it would leave the step recorded as complete, and the runner
        # would then log a failure to write the record that it had just written.
        self._ready()
        calls = []

        def exploding(step, rel):
            calls.append(rel)
            raise OSError(5, "Input/output error")

        with _swap(_mod, "_describe_output", exploding), _captured_stderr() as err:
            self.assertEqual(self._mark("snap"), 1)
        self.assertTrue(calls)
        self.assertIn("ERROR runner: Could not read an output of step 'snap'",
                      err.getvalue())
        self.assertFalse(self.state.exists())

    def test_it_refuses_an_unknown_step(self):
        with _captured_stderr(), self.assertRaises(SystemExit) as caught:
            self._mark("nope")
        self.assertEqual(caught.exception.code, 1)

    def test_it_leaves_the_rest_of_the_record_alone(self):
        self._ready()
        write_state({"completed": {"after": "abc"}})
        with _quiet():
            self._mark("snap")
        self.assertEqual(read_state()["completed"]["after"], "abc")

    def test_it_records_who_asserted_it(self):
        self._ready()
        with _quiet(), _swap(sys, "argv", ["agentcontext/runner.py",
                                           "--mark-complete", "snap"]):
            self._mark("snap")
        state = read_state()
        self.assertEqual(state["outcome"], "marked_complete")
        self.assertIn("--mark-complete", state["command"])
        self.assertIn("finished_at", state)


class TestMainMarkComplete(_RunLogCase):
    """--mark-complete through main records the step and exits without running a step."""

    def setUp(self):
        super().setUp()
        self._saved = (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS,
                       _mod.STAGE_DIR, _mod.MIN_FREE_DISK_GB)
        _mod.STAGE_DIR = self.tmp
        # Without a minimum of 0, the second run in test_the_marked_step_is_skipped_by_the_next_run
        # would stop at the check of the free disk space, and the test would pass without reaching
        # the resume logic that it tests.
        _mod.MIN_FREE_DISK_GB = 0
        self.ran = self.tmp / "ran.txt"
        body = f"open({str(self.ran)!r}, 'w').write('x')"
        _mod.CORE_STEPS = [
            Step("snap", sys.executable, ["-c", body], produces=("out",),
                  completion=COMPLETION_OUTPUT),
        ]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        (self.tmp / "out").mkdir()
        (self.tmp / "out" / "a.txt").write_text("a")

    def tearDown(self):
        (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS,
         _mod.STAGE_DIR, _mod.MIN_FREE_DISK_GB) = self._saved
        super().tearDown()

    def _run(self, *argv) -> int:
        with _quiet(), _swap(sys, "argv", ["agentcontext/runner.py", *argv]):
            return main()

    def test_the_step_really_does_run_without_the_mark(self):
        # This test is the precondition of every "it ran nothing" assertion below. Without
        # it, those assertions would also pass for a run that stopped before the step list.
        self._run()
        self.assertTrue(self.ran.exists())

    def test_it_records_the_step_and_exits_zero(self):
        self.assertEqual(self._run("--mark-complete", "snap"), 0)
        self.assertIn("snap", read_state()["completed"])

    def test_it_runs_no_step(self):
        self._run("--mark-complete", "snap")
        self.assertFalse(self.ran.exists())

    def test_the_marked_step_is_skipped_by_the_next_run(self):
        """The step is skipped by a run that finished, not by a run that refused. A check
        only that the step did not run would pass a --mark-complete that kept the lock,
        after which the next run refuses to start."""
        self.assertEqual(self._run("--mark-complete", "snap"), 0)
        self.assertEqual(self._run(), 0)
        self.assertFalse(self.ran.exists())
        state = read_state()
        self.assertEqual(state["outcome"], "success")
        self.assertEqual([(s["name"], s["status"]) for s in state["steps"]],
                         [("snap", "skipped")])

    def test_the_skip_line_names_when_and_with_which_code_mark_complete_recorded_the_step(self):
        marked_with = {"git_sha": "0123456789abcdef0123456789abcdef01234567", "git_dirty": True}
        with mock.patch.object(_mod, "_git_info", return_value=marked_with):
            self.assertEqual(self._run("--mark-complete", "snap"), 0)
        with _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            self.assertEqual(main(), 0)
        self.assertRegex(err.getvalue(),
                         r"(?m)^INFO runner: snap skipped, because it is already complete\. "
                         r"Recorded by --mark-complete on \d{4}-\d\d-\d\d at \d\d:\d\d UTC with "
                         r"the code of commit 01234567 and uncommitted changes\. Run --from snap "
                         r"to run it again\.$")

    def test_it_takes_the_lock_and_releases_it(self):
        # An assertion only that the lock is free afterwards cannot tell "released" from
        # "never taken". --mark-complete writes the same file that a run writes.
        held = []
        real = _mod.acquire_lock

        def watched():
            got = real()
            held.append(not _lock_is_free(self.lock))
            return got

        with _swap(_mod, "acquire_lock", watched):
            self._run("--mark-complete", "snap")
        self.assertEqual(held, [True])
        self.assertTrue(_lock_is_free(self.lock))

    def test_a_live_lock_refuses_it(self):
        _hold_from_another_process(self, self.lock)
        self.assertEqual(self._run("--mark-complete", "snap"), 1)
        self.assertFalse(self.state.exists())


    def test_a_missing_output_exits_non_zero_and_records_nothing(self):
        shutil.rmtree(self.tmp / "out")
        self.assertEqual(self._run("--mark-complete", "snap"), 1)
        self.assertFalse(self.state.exists())

    def test_it_refuses_to_share_a_command_line_with_list(self):
        # main handles --list first. The pair would print the step list, exit 0 and record
        # nothing, which reads as success.
        self.assertEqual(self._run("--list", "--mark-complete", "snap"), 1)
        self.assertFalse(self.state.exists())

    def test_it_refuses_to_share_a_command_line_with_from(self):
        self.assertEqual(self._run("--from", "snap", "--mark-complete", "snap"), 1)
        self.assertFalse(self.state.exists())

    def test_it_refuses_to_share_a_command_line_with_repo_csv(self):
        self.assertEqual(
            self._run("--repo-csv", "nope.csv", "--mark-complete", "snap"), 1)
        self.assertFalse(self.state.exists())

    def test_it_refuses_to_share_a_command_line_with_step_arg(self):
        self.assertEqual(
            self._run("--step-arg", "snap:-w 2", "--mark-complete", "snap"), 1)
        self.assertFalse(self.state.exists())


class TestMainRemembersTheMutator(_RunLogCase):
    """`main` through the check for modified inputs, with real script files, so that a
    fingerprint can change.

    The fixture steps elsewhere in this file run `python -c`, and their fingerprint
    is `""` whatever happens, which leaves the drop by fingerprint unreachable. The
    steps here run real files, and an edit drops a step as an edit of
    agentcontext/steps/detection.py drops the file history step.
    """

    def setUp(self):
        super().setUp()
        self._saved = (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS,
                       _mod.MIN_FREE_DISK_GB, _mod.STAGE_DIR)
        _mod.MIN_FREE_DISK_GB = 0
        _mod.STAGE_DIR = self.tmp
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        for name in ("base.py", "mid.py", "late.py"):
            (self.tmp / name).write_text(
                f"open({str(self.tmp / (name + '.ran'))!r}, 'w').write('x')")
        _mod.CORE_STEPS = [
            Step("base", sys.executable, ["base.py"]),
            Step("mid", sys.executable, ["mid.py"], consumes=("base",)),
            Step("late", sys.executable, ["late.py"], consumes=("mid",),
                  mutates=("base",)),
        ]
        write_state({"completed": {st.key: script_fingerprint(st)
                                   for st in _mod.CORE_STEPS}})

    def tearDown(self):
        (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS,
         _mod.MIN_FREE_DISK_GB, _mod.STAGE_DIR) = self._saved
        super().tearDown()

    def _run(self, *argv) -> int:
        with _quiet(), _swap(sys, "argv", ["agentcontext/runner.py", *argv]):
            return main()

    def _ran(self, name) -> bool:
        return (self.tmp / f"{name}.py.ran").exists()

    def test_from_the_damaged_reader_runs_the_damaged_step_first(self):
        # The drop through --from, for comparison with the next test. mid reads what base
        # produces, and late has modified that output. The run produces it again before mid
        # reads it.
        self.assertEqual(self._run("--from", "mid"), 0)
        self.assertTrue(self._ran("base"))
        self.assertTrue(self._ran("mid"))

    def test_an_edited_script_reaches_the_same_rebuild(self):
        # Editing mid drops mid and late, because late is computed from mid. Given the set
        # that the fingerprints filtered, the check would conclude that late had never run,
        # and mid would run again over the input that late had modified.
        (self.tmp / "mid.py").write_text(
            f"open({str(self.tmp / 'mid.py.ran')!r}, 'w').write('edited')")
        self.assertEqual(self._run(), 0)
        self.assertTrue(self._ran("base"))
        self.assertEqual((self.tmp / "mid.py.ran").read_text(), "edited")

    def test_it_states_what_is_damaged_and_that_the_run_produces_it_again(self):
        (self.tmp / "mid.py").write_text("pass")
        with _captured_stderr() as err, _swap(
                sys, "argv", ["agentcontext/runner.py"]):
            main()
        self.assertIn("INFO runner: 'late' modified the output of 'base' in place since "
                      "'base' last ran", err.getvalue())
        self.assertIn("as --from base does", err.getvalue())

    def test_a_damaged_snapshot_is_refused_and_the_record_left_alone(self):
        """A step recorded by its output takes a snapshot, which can take long, and the run
        therefore does not take the snapshot again on its own. The test compares both parts
        of the record. A comparison of the key set alone would pass a check that does not
        refuse at all, because mid would run, its fingerprint would be rewritten, and the
        three keys would still be there."""
        (self.tmp / "snapshot.txt").write_text("taken once")
        _mod.CORE_STEPS = [dataclasses.replace(_mod.CORE_STEPS[0], produces=("snapshot.txt",),
                                               completion=COMPLETION_OUTPUT),
                           *_mod.CORE_STEPS[1:]]
        write_state({"completed": {st.key: step_fingerprint(st) for st in _mod.CORE_STEPS}})
        (self.tmp / "mid.py").write_text("pass")
        before = dict(self._completed())
        with _captured_stderr() as err, _swap(sys, "argv", ["agentcontext/runner.py"]):
            self.assertEqual(main(), 1)
        self.assertEqual(self._completed(), before)
        self.assertFalse(self._ran("base"))
        self.assertIn("ERROR runner: 'late' modified the output of 'base' in place on an "
                      "earlier run", err.getvalue())
        self.assertIn("--from base", err.getvalue())
        self.assertEqual(read_state()["outcome"], "modified_inputs")

    def test_an_edit_that_damages_nothing_still_runs(self):
        # `late` is the mutator itself, and no pending step reads what it modified. Editing
        # `late` only runs it again.
        (self.tmp / "late.py").write_text(
            f"open({str(self.tmp / 'late.py.ran')!r}, 'w').write('edited')")
        self.assertEqual(self._run(), 0)
        self.assertTrue(self._ran("late"))
        self.assertFalse(self._ran("mid"))


class TestAModifiedInputIsProducedAgain(_RunLogCase):
    """`late` prunes the tree that `base` writes and reads the tree itself, as the file history
    step prunes and reads the validated tree that the collected file cleanup writes. `mid`
    lists the tree, as the detection step reads it."""

    def setUp(self):
        super().setUp()
        self.data = self.tmp / "data"
        self.data.mkdir()
        (self.data / "base.py").write_text(
            "import os\nos.makedirs('tree', exist_ok=True)\n"
            "for name in ('a', 'b'):\n    open(os.path.join('tree', name), 'w').write(name)\n"
            "open('ran.log', 'a').write('base\\n')\n")
        (self.data / "mid.py").write_text(
            "import os\nopen('ran.log', 'a').write('mid\\n')\n"
            "open('mid.txt', 'w').write(' '.join(sorted(os.listdir('tree'))))\n")
        (self.data / "late.py").write_text(
            "import os, sys\nif os.path.exists(os.path.join('tree', 'b')):\n"
            "    os.remove(os.path.join('tree', 'b'))\n"
            "open('ran.log', 'a').write('late\\n')\n"
            "sys.exit(int(os.environ.get('LATE_EXIT', '0')))\n")
        self.steps = [
            Step("base", sys.executable, ["base.py"], produces=("tree",), root=str(self.data)),
            Step("mid", sys.executable, ["mid.py"], produces=("mid.txt",), consumes=("base",),
                 root=str(self.data)),
            Step("late", sys.executable, ["late.py"], consumes=("mid", "base"),
                 mutates=("base",), root=str(self.data)),
        ]
        self.addCleanup(os.environ.pop, "LATE_EXIT", None)

    def _run(self) -> int:
        with _quiet():
            return _mod.run(self.steps, argv=["x"], log_dir=self.tmp / "logs", min_free_gb=0)

    def _ran(self) -> list:
        return (self.data / "ran.log").read_text().split()

    def test_a_mutator_that_failed_after_it_pruned_is_remembered(self):
        """The mutator failed and is not recorded. Without a record of the modification, the
        next run would read the pruned tree, and nothing in the completion record would show
        that a step pruned it."""
        os.environ["LATE_EXIT"] = "1"
        self.assertEqual(self._run(), 1)
        self.assertEqual(sorted(os.listdir(self.data / "tree")), ["a"])
        del os.environ["LATE_EXIT"]
        (self.data / "ran.log").unlink()
        (self.data / "mid.py").write_text(
            "import os\nopen('ran.log', 'a').write('mid\\n')\nlisted = sorted(os.listdir('tree'))\n"
            "open('mid.txt', 'w').write(' '.join(listed))\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._ran(), ["base", "mid", "late"])
        self.assertEqual((self.data / "mid.txt").read_text(), "a b")

    def test_a_changed_mutator_produces_the_tree_again_before_it_runs(self):
        """A new image that changes only the file history step must not refuse the Start, and
        the step must not read a tree that it pruned in an earlier run."""
        self.assertEqual(self._run(), 0)
        (self.data / "ran.log").unlink()
        (self.data / "late.py").write_text(
            "import os\nif os.path.exists(os.path.join('tree', 'b')):\n"
            "    os.remove(os.path.join('tree', 'b'))\n"
            "open('ran.log', 'a').write('late\\n')\nchanged = True\n")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._ran(), ["base", "mid", "late"])
        state = json.loads((self.tmp / "logs" / "state.json").read_text())
        self.assertEqual(set(state["completed"]), {"base", "mid", "late"})
        self.assertEqual(state[_mod.MODIFIED], {"base": ["late"]})

    def test_the_record_of_the_modification_ends_when_the_damaged_step_runs_again(self):
        state = {"completed": {}}
        _mod.set_modified(state, "base", "late")
        _mod.set_modified(state, "base", "late")
        self.assertEqual(state[_mod.MODIFIED], {"base": ["late"]})
        _mod.clear_modified(state, "base")
        self.assertNotIn(_mod.MODIFIED, state)

    def test_a_record_with_another_modified_is_refused(self):
        write_state({"completed": {}, _mod.MODIFIED: ["late"]})
        with self.assertRaises(SystemExit) as caught:
            read_state()
        self.assertIn(_mod.MODIFIED, str(caught.exception))


class TestNewAndChangedDependencies(_RunLogCase):

    def test_an_added_step_recomputes_its_recorded_consumer(self):
        (self.tmp / "commits.txt").write_text("kept old\nexcluded old\n")
        producer = self.tmp / "reclassify.py"
        producer.write_text("from pathlib import Path\n"
                            "Path('commits.txt').write_text('kept new\\nexcluded new\\n')\n")
        consumer = self.tmp / "restrict.py"
        consumer.write_text("from pathlib import Path\n"
                            "Path('published.txt').write_text(''.join(line for line in "
                            "Path('commits.txt').read_text().splitlines(True) "
                            "if line.startswith('kept ')))\n")
        first = Step("restrict", sys.executable, [str(consumer)], root=self.tmp,
                      produces=("published.txt",))
        added = Step("reclassify", sys.executable, [str(producer)], root=self.tmp)
        second = _mod.replace(first, consumes=("reclassify",))
        with _quiet():
            self.assertEqual(_mod.run([first], no_github=True, min_free_gb=0), 0)
            self.assertEqual((self.tmp / "published.txt").read_text(), "kept old\n")
            self.assertEqual(_mod.run([added, second], no_github=True, min_free_gb=0), 0)
        self.assertEqual((self.tmp / "commits.txt").read_text(), "kept new\nexcluded new\n")
        self.assertEqual((self.tmp / "published.txt").read_text(), "kept new\n")

    def test_reclassification_rebuilds_the_tree_before_reading_pruned_inputs(self):
        steps = _mod.steps_for(True)
        recorded = {step.key: step.key for step in steps}
        def fingerprint(step):
            return "changed commit heuristics" if step.key == 'commit_reclassification' else step.key
        with mock.patch.object(_mod, "step_fingerprint_or_stop", side_effect=fingerprint), \
                mock.patch.object(_mod, "entry_kind", return_value=_mod.FILE), _quiet():
            done = completed_steps(steps, {"completed": recorded})
        pending = _mod.pending_step_names(steps, done)
        self.assertIn('collected_file_cleanup', pending)
        self.assertIn('detection', pending)
        self.assertIn('file_history', pending)
        self.assertNotIn('repository_cloning', pending)
        self.assertEqual(_mod.modified_input_problems(steps, pending, set(recorded)), [])


class TestCompletedSteps(_RunLogCase):

    def setUp(self):
        super().setUp()
        self._orig_dir = _mod.STAGE_DIR
        _mod.STAGE_DIR = self.tmp
        for name in ("a.py", "b.py", "c.py"):
            (self.tmp / name).write_text("original")
        self.chain = [
            Step("a", "python3", ["a.py"]),
            Step("b", "python3", ["b.py"], consumes=("a",)),
            Step("c", "python3", ["c.py"], consumes=("b",)),
        ]

    def tearDown(self):
        _mod.STAGE_DIR = self._orig_dir
        super().tearDown()

    def _all_recorded(self) -> dict:
        return {"completed": {s.key: script_fingerprint(s) for s in self.chain}}

    def test_nothing_recorded_is_nothing_done(self):
        self.assertEqual(completed_steps(self.chain, {}), set())

    def test_matching_fingerprints_all_count(self):
        self.assertEqual(completed_steps(self.chain, self._all_recorded()),
                         {"a", "b", "c"})

    def test_a_changed_script_uncompletes_its_own_step(self):
        state = self._all_recorded()
        (self.tmp / "c.py").write_text("edited")
        with _quiet():
            self.assertEqual(completed_steps(self.chain, state), {"a", "b"})

    def test_a_changed_script_uncompletes_everything_computed_from_it(self):
        # The later results were derived from output that this code no
        # longer produces.
        state = self._all_recorded()
        (self.tmp / "a.py").write_text("edited")
        with _quiet():
            self.assertEqual(completed_steps(self.chain, state), set())

    def test_it_logs_which_steps_it_dropped(self):
        state = self._all_recorded()
        (self.tmp / "b.py").write_text("edited")
        with _captured_stderr() as err:
            completed_steps(self.chain, state)
        self.assertIn("b.py has changed", err.getvalue())
        self.assertIn("No longer counting b and c as done.", err.getvalue())

    def test_a_recorded_step_outside_this_pipeline_is_ignored(self):
        # `--with-commit-reclassification` on one run and not the next must not make the
        # core steps count as incomplete.
        state = self._all_recorded()
        state["completed"]["not_in_this_run"] = "abc"
        self.assertEqual(completed_steps(self.chain, state), {"a", "b", "c"})


class TestCompletedStepsForASnapshot(_RunLogCase):
    """A step recorded by its output, with a step computed from it.

    `snap` stands in for the collection. Its result is a snapshot of a source that
    the pipeline does not control, and running its script again does not reproduce it.
    """

    def setUp(self):
        super().setUp()
        self._orig_dir = _mod.STAGE_DIR
        _mod.STAGE_DIR = self.tmp
        (self.tmp / "snap.py").write_text("original")
        (self.tmp / "after.py").write_text("original")
        (self.tmp / "out").mkdir()
        (self.tmp / "out" / "a.txt").write_text("collected")
        self.chain = [
            Step("snap", "python3", ["snap.py"], produces=("out",),
                  completion=COMPLETION_OUTPUT),
            Step("after", "python3", ["after.py"], consumes=("snap",)),
        ]

    def tearDown(self):
        _mod.STAGE_DIR = self._orig_dir
        super().tearDown()

    def _all_recorded(self) -> dict:
        return {"completed": {s.key: step_fingerprint(s) for s in self.chain}}

    def test_both_count_when_nothing_has_moved(self):
        self.assertEqual(completed_steps(self.chain, self._all_recorded()),
                         {"snap", "after"})

    def test_a_changed_script_leaves_the_snapshot_complete(self):
        # An edit to the collector does not make the tree that it wrote wrong, and
        # treating the tree as wrong would clone the sample again.
        state = self._all_recorded()
        (self.tmp / "snap.py").write_text("edited")
        self.assertEqual(completed_steps(self.chain, state), {"snap", "after"})

    def test_a_changed_script_does_not_drag_down_what_reads_the_snapshot(self):
        state = self._all_recorded()
        (self.tmp / "snap.py").write_text("edited")
        self.assertIn("after", completed_steps(self.chain, state))

    def test_a_changed_output_uncompletes_it(self):
        state = self._all_recorded()
        (self.tmp / "out" / "a.txt").write_text("something else")
        with _quiet():
            self.assertEqual(completed_steps(self.chain, state), set())

    def test_a_removed_output_uncompletes_it(self):
        state = self._all_recorded()
        shutil.rmtree(self.tmp / "out")
        with _quiet():
            self.assertEqual(completed_steps(self.chain, state), set())

    def test_a_changed_output_takes_what_is_computed_from_it_along(self):
        state = self._all_recorded()
        (self.tmp / "out" / "b.txt").write_text("a file that was not collected")
        with _quiet():
            self.assertNotIn("after", completed_steps(self.chain, state))

    def test_it_names_the_output_rather_than_the_script(self):
        state = self._all_recorded()
        (self.tmp / "out" / "a.txt").write_text("moved")
        with _captured_stderr() as err:
            completed_steps(self.chain, state)
        self.assertIn("The output of snap (out) has changed", err.getvalue())
        self.assertNotIn("snap.py", err.getvalue())

    def test_a_step_computed_from_it_is_still_recorded_by_its_own_script(self):
        state = self._all_recorded()
        (self.tmp / "after.py").write_text("edited")
        with _quiet():
            self.assertEqual(completed_steps(self.chain, state), {"snap"})

    def test_an_empty_record_does_not_count_when_the_output_is_gone(self):
        # The fingerprint of an absent output is "", and a recorded "" would match it. The
        # step would then always be skipped with no sample on disk.
        shutil.rmtree(self.tmp / "out")
        with _quiet():
            self.assertEqual(
                completed_steps(self.chain, {"completed": {"snap": ""}}), set())

    def test_a_gone_output_is_reported_as_gone_rather_than_as_changed(self):
        state = self._all_recorded()
        shutil.rmtree(self.tmp / "out")
        with _captured_stderr() as err:
            completed_steps(self.chain, state)
        self.assertIn("declares output that is not on disk", err.getvalue())

    def test_a_changed_output_points_at_the_way_back(self):
        # Without the command in the message, the operator's only option would be to clone
        # the whole input list again over a tree that is probably still correct.
        state = self._all_recorded()
        (self.tmp / "out" / "a.txt").write_text("moved")
        with _captured_stderr() as err:
            completed_steps(self.chain, state)
        self.assertIn("--mark-complete snap", err.getvalue())




# ---------------------------------------------------------------------------
# main  (end to end, with trivial steps standing in for the real scripts)
# ---------------------------------------------------------------------------

def _step(name: str, code: int = 0, text: str = "") -> "Step":
    """A Step with a command that prints text and exits with code."""
    body = f"import sys; print({text!r}); sys.exit({code})"
    return Step(name, sys.executable, ["-c", body])


class TestMain(_RunLogCase):

    def setUp(self):
        super().setUp()
        self._saved = (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS,
                       _mod.MIN_FREE_DISK_GB, _mod.STAGE_DIR)
        self._saved_stop = (_mod._CHILD, _mod._STOPPING)
        _mod.CORE_STEPS = [_step("alpha"), _step("beta")]
        _mod.COMMIT_RECLASSIFICATION_STEPS = [_step("gamma")]
        _mod.MIN_FREE_DISK_GB = 0         # the check of the free disk space always passes
        _mod.STAGE_DIR = self.tmp        # steps run in a disposable cwd

    def tearDown(self):
        (_mod.CORE_STEPS, _mod.COMMIT_RECLASSIFICATION_STEPS,
         _mod.MIN_FREE_DISK_GB, _mod.STAGE_DIR) = self._saved
        _mod._CHILD, _mod._STOPPING = self._saved_stop
        super().tearDown()

    def _run(self, *argv) -> int:
        with _quiet(), _swap(sys, "argv", ["agentcontext/runner.py", *argv]):
            return main()

    def _manifest(self) -> dict:
        return read_state()

    # -- --list ---------------------------------------------------------------

    def test_list_prints_and_returns_zero(self):
        with _swap(sys, "argv", ["agentcontext/runner.py", "--list"]), \
                _captured_stderr() as out:
            rc = main()
        self.assertEqual(rc, 0)
        self.assertIn("alpha", out.getvalue())

    def test_list_does_not_take_the_lock(self):
        self._run("--list")
        self.assertTrue(_lock_is_free(self.lock))

    def test_list_includes_ai_commits_when_requested(self):
        with _swap(sys, "argv",
                   ["agentcontext/runner.py", "--list", "--with-commit-reclassification"]), \
                _captured_stderr() as out:
            main()
        self.assertIn("gamma", out.getvalue())

    # -- happy path ------------------------------------------------------------

    def test_all_steps_succeed_returns_zero(self):
        self.assertEqual(self._run(), 0)

    def test_success_marks_every_step_done(self):
        self._run()
        self.assertTrue(self._is_done("alpha"))
        self.assertTrue(self._is_done("beta"))

    def test_success_releases_the_lock(self):
        self._run()
        self.assertTrue(_lock_is_free(self.lock))

    def test_success_writes_a_success_manifest(self):
        self._run()
        m = self._manifest()
        self.assertEqual(m["outcome"], "success")
        self.assertEqual([s["name"] for s in m["steps"]], ["alpha", "beta"])
        self.assertTrue(all(s["status"] == "ok" for s in m["steps"]))

    def test_a_lock_file_the_run_cannot_open_is_one_line_and_exit_one(self):
        """A restored handover can contain a read-only run.lock, which must not end the runner
        in a traceback before any step."""
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        self.lock.write_text("pid 1 on elsewhere\n")
        self.lock.chmod(0o444)
        with _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            self.assertEqual(main(), 1)
        self.assertIn("could not be opened", err.getvalue())
        self.assertFalse(self._is_done("alpha"))

    def test_with_commit_reclassification_executes_reclassification_after_collect(self):
        _mod.CORE_STEPS = [_step('repository_cloning'), _step('detection')]
        listing = self.tmp / "repos.csv"
        listing.write_text("repo_name\no/r\n")
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"}
        with mock.patch.object(_mod, "COMMIT_HEURISTICS_STEP", _step('commit_reclassification')), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._run("--with-commit-reclassification", "--no-github",
                                       "--repo-csv", str(listing)), 0)
        self.assertEqual([s["name"] for s in read_state()["steps"]],
                         ['repository_cloning', 'commit_reclassification', 'detection', "gamma"])

    def test_without_ai_commits_executes_only_the_core_steps(self):
        _mod.CORE_STEPS = [_step('repository_cloning'), _step('detection')]
        listing = self.tmp / "repos.csv"
        listing.write_text("repo_name\no/r\n")
        env = {key: value for key, value in os.environ.items() if key != "GITHUB_TOKEN"}
        with mock.patch.object(_mod, "COMMIT_HEURISTICS_STEP", _step('commit_reclassification')), \
                mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(self._run("--no-github", "--repo-csv", str(listing)), 0)
        self.assertEqual([s["name"] for s in read_state()["steps"]], ['repository_cloning', 'detection'])

    def test_list_places_reclassification_between_collect_and_detect(self):
        _mod.CORE_STEPS = [_step('repository_cloning'), _step('detection')]
        with mock.patch.object(_mod, "COMMIT_HEURISTICS_STEP", _step('commit_reclassification')), \
                _swap(sys, "argv", ["agentcontext/runner.py", "--list", "--with-commit-reclassification"]), \
                _captured_stderr() as out:
            self.assertEqual(main(), 0)
        self.assertLess(out.getvalue().index('repository_cloning'), out.getvalue().index('commit_reclassification'))
        self.assertLess(out.getvalue().index('commit_reclassification'), out.getvalue().index('detection'))

    def test_with_commit_reclassification_runs_the_extra_step(self):
        self.assertEqual(self._run("--with-commit-reclassification"), 0)
        self.assertTrue(self._is_done("gamma"))

    # -- resume ----------------------------------------------------------------

    def test_a_done_step_is_skipped_on_rerun(self):
        self._mark_done("alpha")
        self._run()
        m = self._manifest()
        by_name = {s["name"]: s["status"] for s in m["steps"]}
        self.assertEqual(by_name["alpha"], "skipped")
        self.assertEqual(by_name["beta"], "ok")

    def test_the_skip_line_names_when_and_with_which_code_the_step_finished(self):
        """A Build in Container Manager discards the container's log, and with it the lines of
        the invocation that ran a step. At a later invocation, the runner skips the step with a
        line that names when the step finished, the commit of the code that ran it and the log
        of the step, not the commit of the code that skips it."""
        ran_with = {"git_sha": "0123456789abcdef0123456789abcdef01234567", "git_dirty": False}
        skips_with = {"git_sha": "fedcba9876543210fedcba9876543210fedcba98", "git_dirty": False}
        before = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        with mock.patch.object(_mod, "_git_info", return_value=ran_with):
            self.assertEqual(self._run(), 0)
        after = datetime.now(timezone.utc)
        with mock.patch.object(_mod, "_git_info", return_value=skips_with), \
                _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            self.assertEqual(main(), 0)
        match = re.search(r"^INFO runner: alpha skipped, because it is already complete\. "
                          r"Finished on (\d{4}-\d\d-\d\d) at (\d\d:\d\d) UTC with the code of "
                          r"commit 01234567\. Its log is run_logs/alpha\.log\. Run --from alpha "
                          r"to run it again\.$", err.getvalue(), re.MULTILINE)
        self.assertIsNotNone(match, err.getvalue())
        finished = datetime.strptime(f"{match[1]} {match[2]}", "%Y-%m-%d %H:%M")
        self.assertTrue(before <= finished.replace(tzinfo=timezone.utc) <= after)

    def test_the_skip_line_of_a_record_edited_by_hand_names_no_origin(self):
        """The entry of `completed_by` describes the record that it names. After a hand edit
        of `completed`, it describes another record, and the line names no origin."""
        # The fixture step fingerprints to "", and the entry names the record before the edit.
        write_state({"completed": {"alpha": ""},
                     _mod.COMPLETED_BY: {"alpha": {"fingerprint": "before the edit",
                                                   "how": "ran",
                                                   "finished_at": "2026-10-27T18:41:07+00:00",
                                                   "git_sha": "0123456789abcdef"}}})
        with _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            self.assertEqual(main(), 0)
        self.assertIn("INFO runner: alpha skipped, because it is already complete. Run --from "
                      "alpha to run it again.\n", err.getvalue())

    # -- failure ---------------------------------------------------------------

    def test_a_failing_step_stops_the_pipeline(self):
        _mod.CORE_STEPS = [_step("alpha", code=1), _step("beta")]
        self.assertEqual(self._run(), 1)
        self.assertFalse(self._is_done("alpha"))
        self.assertFalse(self._is_done("beta"))

    def test_failure_writes_a_failed_manifest_and_releases_the_lock(self):
        _mod.CORE_STEPS = [_step("alpha", code=1), _step("beta")]
        self._run()
        self.assertEqual(self._manifest()["outcome"], "failed")
        self.assertTrue(_lock_is_free(self.lock))

    def test_a_refused_start_keeps_the_step_results_of_the_failed_run_before_it(self):
        """After a run that failed in the analysis stage, the container's entrypoint begins the
        next monthly run and reads the failed step from these results. A Start that the
        pre-checks refuse, for example because of an expired GitHub access token, must not
        replace them."""
        _mod.CORE_STEPS = [_step("alpha"), _step("beta", code=1)]
        self.assertEqual(self._run(), 1)
        failed = [(s["name"], s["status"]) for s in self._manifest()["steps"]]
        self.assertEqual(failed, [("alpha", "ok"), ("beta", "failed")])
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        self.assertEqual(self._run(), 1)
        state = self._manifest()
        self.assertEqual(state["outcome"], "pre_checks_failed")
        self.assertEqual([(s["name"], s["status"]) for s in state[_mod.LAST_RUN_STEPS]], failed)
        self.assertEqual([(s["name"], s["status"]) for s in state["steps"]], failed)

    def test_a_stop_before_any_step_keeps_the_outcome_of_the_failed_run_before_it(self):
        """The pre-checks wait up to hours for GitHub access tokens that GitHub rejects,
        and a Stop ends the wait. If the invocation recorded `interrupted`, it would replace the
        outcome of a run that failed in the analysis stage. The next Start would then run the
        failing step again and hand over the data root a second time. The same holds for three other
        stops that end an invocation that ran no step: a second signal before the record of
        completed steps changed, a stop during pre-checks that pass, and a stop while
        the run computes the record of the collected tree, after which the pre-checks send
        no request."""
        _mod.CORE_STEPS = [_step("alpha"), _step("beta", code=1)]
        self.assertEqual(self._run(), 1)
        failed = [(s["name"], s["status"]) for s in self._manifest()["steps"]]
        self.addCleanup(setattr, _mod, "_STOPPING", 0)

        def stopped(*args, **kwargs):
            _mod._STOPPING = signal.SIGTERM
            return False

        def stopped_twice(*args, **kwargs):
            _mod._STOPPING = signal.SIGTERM
            raise SystemExit(128 + signal.SIGTERM)

        def stopped_and_passed(*args, **kwargs):
            _mod._STOPPING = signal.SIGTERM
            return True

        def stopped_before(*args, **kwargs):
            raise AssertionError("no request goes out after a stop")

        completed = _mod.completed_steps

        def stopped_while_recorded(steps, state):
            _mod._STOPPING = signal.SIGTERM
            return completed(steps, state)

        for pre_checks, before in ((stopped, completed), (stopped_twice, completed),
                                  (stopped_and_passed, completed),
                                  (stopped_before, stopped_while_recorded)):
            with self.subTest(pre_checks=pre_checks.__name__), \
                    mock.patch.object(_mod, "completed_steps", before), \
                    mock.patch.object(_mod, "pre_checks", pre_checks):
                self.assertEqual(self._run(), 128 + signal.SIGTERM)
                state = self._manifest()
                self.assertEqual(state[_mod.LAST_RUN_OUTCOME], "failed")
                self.assertEqual([(s["name"], s["status"]) for s in state[_mod.LAST_RUN_STEPS]],
                                 failed)
                self.assertEqual(state["outcome"], "stopped_before_steps")

    def test_a_stop_while_the_run_skips_its_first_steps_ran_no_step(self):
        """The loop over the steps that the invocation skips computes their records again,
        which can take long. A stop there ends an invocation in which no step started."""
        _mod.CORE_STEPS = [_step("alpha"), _step("beta", code=1)]
        self.assertEqual(self._run(), 1)
        self.addCleanup(setattr, _mod, "_STOPPING", 0)
        changed = _mod.changed_during_run

        def stopped_while_skipping(step, recorded, state=None):
            _mod._STOPPING = signal.SIGTERM
            return changed(step, recorded, state)

        with mock.patch.object(_mod, "changed_during_run", stopped_while_skipping):
            self.assertEqual(self._run(), 128 + signal.SIGTERM)
        state = self._manifest()
        self.assertEqual(state[_mod.LAST_RUN_OUTCOME], "failed")
        self.assertEqual(state["outcome"], "stopped_before_steps")

    def test_a_step_killed_by_a_signal_the_runner_did_not_send_is_not_recorded(self):
        """The step is killed after writing everything that it declares, as the OOM killer or
        a `kill -9` aimed at the step leaves it. The exit code is negative, and a check for a
        positive exit code would record the step as complete."""
        _mod.CORE_STEPS = [
            Step("alpha", sys.executable,
                  ["-c", "import os, signal\n"
                         "open('out.csv', 'w').write('a\\n1\\n')\n"
                         "os.kill(os.getpid(), signal.SIGKILL)\n"],
                  produces=("out.csv",)),
            _step("beta")]
        with _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            self.assertEqual(main(), 1)
        self.assertIn(f"ERROR runner: alpha exited {-signal.SIGKILL}", err.getvalue())
        self.assertEqual(self._completed(), {})
        self.assertEqual([(s["name"], s["status"]) for s in self._manifest()["steps"]],
                         [("alpha", "failed")])

    def test_a_finished_step_whose_output_cannot_be_read_is_a_failed_step(self):
        """An output that the runner cannot open makes the step that has just exited 0 a failed
        step. The sync before the record cannot open it, and neither can `output_fingerprint`,
        which raises on output that it cannot read and ends a resume. The run catches the error
        where it records the step. Without the catch, the error would end the run with a
        traceback, the step that had just finished would stay unrecorded, and the run would
        leave no record."""
        _mod.CORE_STEPS = [
            Step("alpha", sys.executable,
                  ["-c", "import os\n"
                         "os.mkdir('out')\n"
                         "open('out/rows.csv', 'w').write('a\\n1\\n')\n"
                         "os.chmod('out/rows.csv', 0)\n"],
                  produces=("out",), completion=COMPLETION_OUTPUT),
            _step("beta")]
        blocked = self.tmp / "out" / "rows.csv"
        self.addCleanup(lambda: blocked.exists() and blocked.chmod(0o644))
        with _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            code = main()
        try:
            blocked.read_bytes()
        except PermissionError:
            pass
        else:
            self.skipTest("this filesystem or user ignores the mode")
        self.assertEqual(code, 1)
        self.assertIn("ERROR runner: alpha exited 0, but its output could not be synced to "
                      "disk (out", err.getvalue())
        self.assertIn("Did not record the step as complete.", err.getvalue())
        self.assertEqual(self._completed(), {})
        self.assertEqual(self._manifest()["outcome"], "failed")
        self.assertFalse(log_path("beta").exists())

    def test_a_state_file_that_cannot_be_written_stops_the_run(self):
        """The volume under `run_logs/` turns read-only while a step runs, and the record of
        the step's completion cannot be written. The run stops there. Without that stop, the
        error would end the run with a traceback."""
        _mod.CORE_STEPS = [
            Step("alpha", sys.executable,
                  ["-c", f"import os; os.chmod({str(self.log_dir)!r}, 0o555)"]),
            _step("beta")]
        with _swap(sys, "argv", ["agentcontext/runner.py"]), _captured_stderr() as err:
            try:
                code = main()
            finally:
                self.log_dir.chmod(0o755)
        if "could not write" not in err.getvalue() and code == 0:
            self.skipTest("this filesystem or user ignores the mode")
        self.assertEqual(code, 1)
        self.assertRegex(err.getvalue(), rf"ERROR runner: Could not write "
                                         rf"{re.escape(str(self.state))}\.[0-9a-f]+\.tmp")
        self.assertIn("Stopped, because the completion of further steps cannot be recorded",
                      err.getvalue())
        self.assertFalse(log_path("beta").exists())
        self.assertTrue(_lock_is_free(self.lock))

    def test_a_keyboard_interrupt_that_reaches_the_run_is_an_interruption(self):
        """The handler catches every real Ctrl+C. A KeyboardInterrupt therefore reaches the
        run only before the handler is installed or from code that raises it itself. Without
        the catch, the run would end with a traceback and record nothing."""
        def interrupted(step):
            raise KeyboardInterrupt

        with _swap(_mod, "run_step", interrupted):
            try:
                code = self._run()
            except KeyboardInterrupt:
                self.fail("the KeyboardInterrupt left the run")
        self.assertEqual(code, 128 + signal.SIGINT)
        state = self._manifest()
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual([(s["name"], s["status"]) for s in state["steps"]],
                         [("alpha", "interrupted")])
        self.assertEqual(self._completed(), {})
        self.assertTrue(_lock_is_free(self.lock))

    # -- lock conflict ----------------------------------------------------------

    def test_a_live_lock_refuses_the_run(self):
        _hold_from_another_process(self, self.lock)
        self.assertEqual(self._run(), 1)
        # The holder's identifier is left intact, not overwritten.
        self.assertEqual(self.lock.read_text().strip(), "held by the test")

    def test_a_refused_run_executes_no_step(self):
        _hold_from_another_process(self, self.lock)
        self._run()
        self.assertFalse(self._is_done("alpha"))

    # -- pre-checks ---------------------------------------------------------------

    def test_failed_pre_checks_run_no_step(self):
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        self.assertEqual(self._run(), 1)
        self.assertFalse(self._is_done("alpha"))

    def test_failed_pre_checks_write_the_manifest_and_release_the_lock(self):
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        self._run()
        self.assertEqual(self._manifest()["outcome"], "pre_checks_failed")
        self.assertTrue(_lock_is_free(self.lock))

    # -- --from ------------------------------------------------------------------

    def test_from_redoes_the_named_step(self):
        self._mark_done("alpha", "beta")
        self._run("--from", "beta")
        m = self._manifest()
        by_name = {s["name"]: s["status"] for s in m["steps"]}
        self.assertEqual(by_name["alpha"], "skipped")
        self.assertEqual(by_name["beta"], "ok")

    # -- --step-arg -----------------------------------------------------------

    # -- a stop ends the run, end to end ----------------------------------
    #
    # The tests set the module's stop flag from a patched function rather than signal
    # this process, because a real signal that the code failed to handle would stop the
    # test runner too.

    def _stop_during(self, target: str):
        """Patch `target` so that it sets the stop flag, as a signal would. For `run_step`,
        the patch also sets the flag that the signal reached the step, as `forward_stop` sets
        it for a running step."""
        original = getattr(_mod, target)

        def stopping(*a, **kw):
            result = original(*a, **kw)
            _mod._STOPPING = signal.SIGTERM
            if target == "run_step":
                _mod._FORWARDED = True
            return result

        return _swap(_mod, target, stopping)

    def test_a_stop_during_a_step_ends_the_run_with_the_signals_code(self):
        with self._stop_during("run_step"):
            rc = self._run()
        self.assertEqual(rc, 128 + signal.SIGTERM)

    def test_a_signaled_step_is_not_recorded_even_when_it_exits_zero(self):
        # A step that drains on a stop can exit 0 with every declared path present.
        # Recording that step would make the next run skip a partial result.
        with self._stop_during("run_step"):
            self._run()
        self.assertEqual(self._completed(), {})

    def test_a_stop_does_not_start_the_next_step(self):
        with self._stop_during("run_step"):
            self._run()
        self.assertFalse(log_path("beta").exists())

    def test_a_stop_records_the_step_as_interrupted(self):
        with self._stop_during("run_step"):
            self._run()
        state = self._manifest()
        self.assertEqual(state["outcome"], "interrupted")
        self.assertEqual(state["steps"][-1]["name"], "alpha")
        self.assertEqual(state["steps"][-1]["status"], "interrupted")

    def test_a_stop_between_two_steps_keeps_the_one_that_finished(self):
        # The handler returns instead of raising for a stop that arrives while the runner
        # writes the state after a step. The step that completed stays recorded, and the
        # next step never starts. The patch leaves the writes before the first step alone,
        # because they record no step.
        original = _mod.write_execution_state

        def stopping(state, step_results, *rest, **named):
            original(state, step_results, *rest, **named)
            if step_results:
                _mod._STOPPING = signal.SIGTERM
        with _swap(_mod, "write_execution_state", stopping):
            rc = self._run()
        self.assertEqual(rc, 128 + signal.SIGTERM)
        self.assertEqual(set(self._completed()), {"alpha"})
        self.assertFalse(log_path("beta").exists())

    def test_a_stop_releases_the_lock(self):
        with self._stop_during("run_step"):
            self._run()
        self.assertTrue(_lock_is_free(self.lock))

    def test_the_handlers_are_restored_when_main_returns(self):
        # This suite imports the runner, and a handler that outlived the call that
        # installed it would handle signals meant for whatever runs next.
        before = {sig: signal.getsignal(sig)
                  for sig in (signal.SIGTERM, signal.SIGINT)}
        self._run()
        for sig, handler in before.items():
            self.assertIs(signal.getsignal(sig), handler)

    def test_the_handlers_are_restored_when_the_lock_refuses(self):
        _hold_from_another_process(self, self.lock)
        before = {sig: signal.getsignal(sig)
                  for sig in (signal.SIGTERM, signal.SIGINT)}
        self.assertEqual(self._run(), 1)
        for sig, handler in before.items():
            self.assertIs(signal.getsignal(sig), handler)

    def test_a_stale_stop_flag_does_not_leak_into_the_next_run(self):
        _mod._STOPPING = signal.SIGTERM
        self.assertEqual(self._run(), 0)

    # -- the state file, end to end ---------------------------------------

    def _real_script_pipeline(self, body: str = "pass") -> None:
        """Set CORE_STEPS to one step that runs a real file on disk, so that its
        fingerprint is not the empty string."""
        (self.tmp / "one.py").write_text(body)
        _mod.CORE_STEPS = [Step("one", sys.executable, ["one.py"])]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []

    def test_a_completed_step_records_its_scripts_hash(self):
        self._real_script_pipeline()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._completed()["one"],
                         script_fingerprint(_mod.CORE_STEPS[0]))

    def test_editing_a_scripts_code_makes_its_step_run_again(self):
        self._real_script_pipeline("open('ran', 'a').close()")
        self.assertEqual(self._run(), 0)
        (self.tmp / "ran").unlink()
        (self.tmp / "one.py").write_text("open('ran', 'a').close()\nopen('ran', 'a').write('x')")
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.tmp / "ran").exists())

    def test_an_edit_to_a_comment_a_docstring_or_a_log_message_runs_nothing(self):
        """What the step runs is unchanged, and its output would be unchanged too. A hash of
        the script's bytes would run its step and every step computed from it again. For an
        edit to a comment in `agentcontext/logs.py`, that is every step from the collected
        file cleanup to the analysis."""
        body = ("from agentcontext import logs\n"
                "open('ran', 'a').close()\n"
                "logs.info('one', 'Done.')\n")
        self._real_script_pipeline(body)
        self.assertEqual(self._run(), 0)
        (self.tmp / "ran").unlink()
        (self.tmp / "one.py").write_text('"""A docstring added."""\n'
                                         "from agentcontext import logs\n"
                                         "# A comment added.\n"
                                         "open('ran', 'a').close()\n"
                                         "logs.info('one', 'Done, in other words.')\n")
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.tmp / "ran").exists(), "an edit that runs nothing new ran the step")

    def test_an_unchanged_script_is_not_run_again(self):
        self._real_script_pipeline("open('ran', 'a').close()")
        self.assertEqual(self._run(), 0)
        (self.tmp / "ran").unlink()
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.tmp / "ran").exists())

    def test_a_step_recorded_by_its_output_records_the_output_it_wrote(self):
        """A run records a manifest of the output of such a step. A run that recorded the
        script's hash instead would clone the whole sample again after an edit to the
        collector's script."""
        body = ("import os\n"
                "os.makedirs('out', exist_ok=True)\n"
                "open('out/a.txt', 'w').write('a')\n"
                "open('ran', 'a').close()\n")
        (self.tmp / "snap.py").write_text(body)
        _mod.CORE_STEPS = [Step("snap", sys.executable, ["snap.py"], produces=("out",),
                                  completion=COMPLETION_OUTPUT)]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._completed()["snap"], output_fingerprint(_mod.CORE_STEPS[0]))
        (self.tmp / "ran").unlink()
        (self.tmp / "snap.py").write_text(body + "edited = True\n")
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.tmp / "ran").exists(), "an edit to the script re-ran the snapshot")

    def test_an_edit_to_a_module_the_step_imports_makes_it_run_again(self):
        """A step that runs a package module is recorded by that module and every package
        module that it imports, because a module such as `paths.py` decides what the cloning
        step copies. A fingerprint of the step's own file alone would leave every step that
        imports `paths.py` counting as done after an edit to `paths.py`. The stand-in package
        is in the temporary directory, and its step imports the stand-in `paths`."""
        package = self.tmp / "agentcontext"
        (package / "steps").mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "steps" / "__init__.py").write_text("")
        (package / "paths.py").write_text("COPIED = ('CLAUDE.md',)\n")
        (package / "steps" / "copy.py").write_text(
            "from agentcontext import paths\n"
            "if __name__ == '__main__':\n"
            "    open('ran', 'w').write(repr(paths.COPIED))\n")
        _mod.CORE_STEPS = [Step("copy", sys.executable, ["-m", "agentcontext.steps.copy"])]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        ran = self.tmp / "ran"
        # The runner hashes the package of PACKAGE_DIR, and the pre-checks import the
        # step's module from the working directory. The test points both at the stand-in.
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            with _swap(_mod, "PACKAGE_DIR", package):
                self.assertEqual(self._run(), 0)
                self.assertEqual(ran.read_text(), "('CLAUDE.md',)")
                ran.unlink()
                self.assertEqual(self._run(), 0)
                self.assertFalse(ran.exists(), "the step ran again with nothing changed")
                (package / "paths.py").write_text("COPIED = ('CLAUDE.md', 'AGENTS.md')\n")
                self.assertEqual(self._run(), 0)
        finally:
            os.chdir(cwd)
        self.assertTrue(ran.exists(), "an edit to paths.py left the step counting as done")
        self.assertEqual(ran.read_text(), "('CLAUDE.md', 'AGENTS.md')")

    def test_a_comment_in_a_module_the_step_imports_runs_nothing(self):
        """Every step imports `agentcontext/logs.py`. A hash of the bytes of the module
        closure would run every step from the collected file cleanup to the analysis again
        after an edit to one of its comments. The stand-in step imports a stand-in `logs`."""
        package = self.tmp / "agentcontext"
        (package / "steps").mkdir(parents=True)
        (package / "__init__.py").write_text("")
        (package / "steps" / "__init__.py").write_text("")
        (package / "logs.py").write_text("def info(component, message):\n"
                                         "    print(component, message)\n")
        (package / "steps" / "copy.py").write_text(
            "from agentcontext import logs\n"
            "if __name__ == '__main__':\n"
            "    open('ran', 'w').write('x')\n"
            "    logs.info('copy', 'Done.')\n")
        _mod.CORE_STEPS = [Step("copy", sys.executable, ["-m", "agentcontext.steps.copy"])]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        ran = self.tmp / "ran"
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            with _swap(_mod, "PACKAGE_DIR", package):
                self.assertEqual(self._run(), 0)
                ran.unlink()
                (package / "logs.py").write_text('"""The log of a step."""\n\n\n'
                                                 "def info(component, message):\n"
                                                 "    # One line per message.\n"
                                                 "    print(component, message)\n")
                self.assertEqual(self._run(), 0)
                self.assertFalse(ran.exists(), "a comment in logs.py ran the step again")
                (package / "logs.py").write_text("def info(component, message):\n"
                                                 "    print(component, '-', message)\n")
                self.assertEqual(self._run(), 0)
        finally:
            os.chdir(cwd)
        self.assertTrue(ran.exists(), "a code change in logs.py left the step counting as done")

    def test_failed_pre_checks_leave_the_from_steps_recorded(self):
        # A run that logs that no step ran must not write a record without the --from
        # steps. The next run would otherwise redo work that this run refused to start. In
        # the real pipeline, a missing 7zz would discard the records of the collected file
        # cleanup, the detection and the file history.
        _mod.MIN_FREE_DISK_GB = 10 ** 9
        self._mark_done("alpha", "beta")
        before = dict(self._completed())
        self.assertEqual(self._run("--from", "alpha"), 1)
        self.assertEqual(self._manifest()["outcome"], "pre_checks_failed")
        self.assertEqual(self._completed(), before)

    def test_from_still_drops_them_once_the_run_starts(self):
        # The other half: after the pre-checks pass, the run removes the steps from the
        # record.
        self._mark_done("alpha", "beta")
        self._run("--from", "alpha")
        self.assertEqual(self._manifest()["outcome"], "success")
        by_name = {s["name"]: s["status"] for s in self._manifest()["steps"]}
        self.assertEqual(by_name["alpha"], "ok")

    def test_a_truncated_state_file_stops_the_run(self):
        """A failed run logs the refusal and leaves the damaged state file as it is.

        The message tells the operator to repair the file. A record of the run over it would
        write `completed: {}` in its place and leave nothing to repair. The next run would
        then clone the whole sample again, the outcome that `read_state` prevents by refusing
        the file."""
        self._real_script_pipeline()
        damaged = '{"completed": {"one"'
        self.state.write_text(damaged)
        with _quiet(), _swap(sys, "argv", ["agentcontext/runner.py"]):
            self.assertEqual(main(), 1)
        self.assertEqual(self.state.read_text(), damaged,
                         "the state file the operator was told to repair was overwritten")

    def test_a_second_signal_with_no_step_running_exits_on_the_signal(self):
        """`forward_stop` raises `SystemExit(128 + signum)` when a second signal arrives
        with no step running, and that exception reaches the same branch as a check's
        message. The type of the code tells the two apart, so that an interruption is never
        logged as an ordinary failure, as the KeyboardInterrupt branch requires too. The
        signals arrive before the record of completed steps changed. The invocation
        therefore ran nothing, and its state keeps the outcome of the run before it."""
        self._real_script_pipeline()

        def stop_twice(steps, state):
            _mod.forward_stop(signal.SIGINT, None)
            _mod.forward_stop(signal.SIGINT, None)
            return set()

        with (_quiet(), _swap(sys, "argv", ["agentcontext/runner.py"]),
              mock.patch.object(_mod, "completed_steps", stop_twice)):
            self.assertEqual(main(), 130)
        self.assertEqual(self._manifest()["outcome"], "stopped_before_steps")

    def test_legacy_markers_do_not_skip_steps(self):
        self._real_script_pipeline("open('ran', 'a').close()")
        (self.log_dir / "one.done").touch()
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.tmp / "ran").exists())
        self.assertIn("one", self._completed())

    def test_provenance_and_completions_share_one_file(self):
        self._real_script_pipeline()
        self._run()
        state = self._manifest()
        self.assertEqual(state["outcome"], "success")
        self.assertIn("one", state["completed"])

    # -- the check for modified inputs, end to end ------------------------------

    def _mutating_pipeline(self) -> None:
        """Set CORE_STEPS to steps built like the collected file cleanup, the detection and
        the file history."""
        _mod.CORE_STEPS = [
            _step("base"),
            dataclasses.replace(_step("mid"), consumes=("base",)),
            dataclasses.replace(_step("late"), consumes=("mid",),
                                mutates=("base",)),
        ]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []

    def _mutating_snapshot_pipeline(self) -> dict:
        """Set up the same list with `base` recorded by its output as a snapshot, and record
        the steps as a finished run leaves them. Return what the state file records."""
        self._mutating_pipeline()
        _mod.CORE_STEPS[0] = dataclasses.replace(_mod.CORE_STEPS[0], produces=("snap.txt",),
                                                 completion=COMPLETION_OUTPUT)
        (self.tmp / "snap.txt").write_text("taken once")
        recorded = {"base": step_fingerprint(_mod.CORE_STEPS[0]), "mid": "", "late": ""}
        write_state({"completed": recorded})
        return recorded

    def test_from_a_step_whose_input_was_modified_runs_the_damaged_step_first(self):
        """The run produces the modified input again, as a --from does that the operator
        would otherwise have to put into `command:` for one Start."""
        self._mutating_pipeline()
        self._mark_done("base", "mid", "late")
        log_path("base").write_text("SENTINEL")
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()), \
                _swap(sys, "argv", ["agentcontext/runner.py", "--from", "mid"]):
            rc = main()
        self.assertEqual(rc, 0)
        self.assertNotEqual(log_path("base").read_text(), "SENTINEL")
        self.assertEqual([s["status"] for s in self._manifest()["steps"]], ["ok", "ok", "ok"])
        self.assertIn("Running 'base' again", err.getvalue())

    def test_from_a_step_whose_input_was_modified_by_a_snapshot_is_refused(self):
        self._mutating_snapshot_pipeline()
        with _captured_stderr() as err, contextlib.redirect_stdout(io.StringIO()), \
                _swap(sys, "argv", ["agentcontext/runner.py", "--from", "mid"]):
            rc = main()
        self.assertEqual(rc, 1)
        self.assertIn("ERROR runner: 'late' modified the output of 'base' in place on an "
                      "earlier run", err.getvalue())
        self.assertIn("--from base", err.getvalue())

    def test_the_refusal_drops_no_recorded_step(self):
        # The refusal must not drop records, because the record is what the previous run
        # completed, and the operator has not yet chosen how to proceed.
        recorded = self._mutating_snapshot_pipeline()
        with _quiet(), _swap(sys, "argv",
                             ["agentcontext/runner.py", "--from", "mid"]):
            main()
        self.assertEqual(self._completed(), recorded)

    def test_the_refusal_releases_the_lock_and_records_the_outcome(self):
        self._mutating_snapshot_pipeline()
        with _quiet(), _swap(sys, "argv",
                             ["agentcontext/runner.py", "--from", "mid"]):
            main()
        self.assertTrue(_lock_is_free(self.lock))
        self.assertEqual(self._manifest()["outcome"], "modified_inputs")

    def test_from_the_step_that_rebuilds_it_is_allowed(self):
        # The remedy that the message names has to work.
        self._mutating_pipeline()
        self._mark_done("base", "mid", "late")
        self.assertEqual(self._run("--from", "base"), 0)

    def test_a_run_where_the_mutator_never_ran_is_allowed(self):
        self._mutating_pipeline()
        self._mark_done("base", "mid")
        self.assertEqual(self._run("--from", "mid"), 0)

    # -- --from follows dependencies, not list positions ------------------

    def test_from_leaves_a_later_step_that_reads_nothing_alone(self):
        _mod.CORE_STEPS = [
            _step("root"),
            dataclasses.replace(_step("left"), consumes=("root",)),
            dataclasses.replace(_step("right"), consumes=("root",)),
        ]
        _mod.COMMIT_RECLASSIFICATION_STEPS = []
        self._mark_done("root", "left", "right")
        # A sentinel in right's log. run_step rewrites the log on every attempt, and text
        # that survives shows that the step was skipped rather than run again.
        log_path("right").write_text("SENTINEL")
        with _quiet(), _swap(sys, "argv",
                             ["agentcontext/runner.py", "--from", "left"]):
            main()
        # `right` runs after `left` but is not computed from it. Its record survives, and
        # the step does not run again.
        self.assertTrue(self._is_done("right"))
        self.assertEqual(log_path("right").read_text(), "SENTINEL")

    def test_step_arg_reaches_the_step(self):
        # A step that fails unless it receives the injected flag.
        _mod.CORE_STEPS = [
            Step("alpha", sys.executable,
                  ["-c", "import sys; sys.exit(0 if '--go' in sys.argv else 1)"]),
        ]
        self.assertEqual(self._run("--step-arg", "alpha:--go"), 0)


class TestTheImportDetectionStep(unittest.TestCase):
    """`import_detection` reads the validated tree as the file history step leaves it and the
    imported tree that the cloning step writes. The release publishes what `import_detection`
    writes."""

    def _steps(self) -> dict:
        return {s.key: s for s in _RUNNER_DEFAULTS["CORE_STEPS"]}

    def test_it_runs_after_the_file_history_step_and_before_the_release(self):
        keys = [s.key for s in _RUNNER_DEFAULTS["CORE_STEPS"]]
        self.assertLess(keys.index("file_history"), keys.index("import_detection"))
        self.assertLess(keys.index("import_detection"), keys.index("release"))
        self.assertEqual(set(self._steps()["import_detection"].consumes),
                         {"repository_cloning", "collected_file_cleanup", "detection",
                          "file_history"})
        self.assertIn("import_detection", self._steps()["release"].consumes)

    def test_its_command_module_key_and_name_are_one_noun_phrase(self):
        step = self._steps()["import_detection"]
        self.assertEqual(step.args, ["-m", "agentcontext.steps.import_detection"])
        self.assertEqual(importlib.import_module("agentcontext.cli").COMMANDS[
            "import-detection"][0], "agentcontext.steps.import_detection")
        self.assertEqual(importlib.import_module("agentcontext.logs").step_name(step.key),
                         "Import detection")

    def test_the_content_of_the_imported_tree_decides_its_record(self):
        """When a file that the cloning step copied to the imported tree changes under the same
        CSVs, the step runs again, and the same holds for a link that the cloning step wrote
        there. Without the tree, the step counts as not done, and the step itself refuses to
        run."""
        step = self._steps()["import_detection"]
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            with mock.patch.object(_mod, "STAGE_DIR", stage):
                self.assertEqual(_mod.imported_tree_input(step), [("the imported tree", None)])
                tree = stage / "data" / "imported" / "o§a" / "docs"
                tree.mkdir(parents=True)
                (tree / "a.md").write_text("one\n")
                first = _mod.imported_tree_input(step)
                self.assertIsNotNone(step.inputs_of, "the step is recorded by the tree")
                self.assertEqual(step.inputs_of(step), first)
                (tree / "a.md").write_text("two\n")
                second = _mod.imported_tree_input(step)
                (tree / "b.md").symlink_to("a.md")
                third = _mod.imported_tree_input(step)
        self.assertEqual(len({first[0][1], second[0][1], third[0][1]}), 3)


if __name__ == "__main__":
    unittest.main()
