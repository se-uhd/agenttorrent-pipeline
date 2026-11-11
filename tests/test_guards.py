"""Tests for agentcontext.guards: the progress line, the grounded constants and the process
counts. The tests of the stop conditions over a running collection are in
tests.test_steps_repository_cloning, where the collector runs.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_guards -v
"""

import contextlib
import io
import subprocess
import time
import unittest
from pathlib import Path

from agentcontext import guards as _guards
from agentcontext.steps import repository_cloning as _mod


class TestTheProgressLine(unittest.TestCase):
    """The progress line that the run logs, under a clock that the test sets."""

    def _progress(self, total=1000, already=0, every=100, seconds=600.0):
        """A `Progress` under a clock that the test sets. Its line goes through
        `agentcontext.logs`, which writes to the current stderr, and the
        `contextlib.redirect_stderr` of `setUp` therefore captures the line."""
        self.now = 1000.0
        return _guards.Progress(total, already, every, seconds, clock=lambda: self.now)

    def setUp(self):
        self.out = io.StringIO()
        redirect = contextlib.redirect_stderr(self.out)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def test_a_line_every_n_finished_repositories(self):
        progress = self._progress(every=3)
        self.assertIsNone(progress.note(False))
        self.assertIsNone(progress.note(True))
        line = progress.note(False)
        self.assertIsNotNone(line)
        self.assertIn("INFO repository_cloning: 3/1,000 (0.3%) done, 2 ok and 1 failed this run", line)
        self.assertEqual(self.out.getvalue().count("INFO repository_cloning:"), 1)

    def test_a_line_after_the_interval_even_when_the_count_is_not_due(self):
        progress = self._progress(every=100, seconds=600.0)
        self.assertIsNone(progress.note(False))
        self.now += 601
        self.assertIsNotNone(progress.note(False))
        self.now += 10
        self.assertIsNone(progress.note(False), "the interval restarts at the last line")

    def test_the_rate_and_the_remaining_time_are_this_run_s(self):
        # A resumed run. The 400 repositories already done count toward the share and not
        # toward the rate.
        progress = self._progress(total=1000, already=400, every=2)
        self.now += 60
        progress.note(False)
        line = progress.note(False)
        self.assertIn("402/1,000 (40.2%) done", line)
        self.assertIn("2.0 repositories/min", line)
        self.assertIn("elapsed 0:01:00", line)
        # 598 pending at 2 a minute is 299 minutes.
        self.assertIn("about 4:59:00 left at this rate", line)

    def test_no_division_by_zero_on_an_empty_total_or_an_instant_finish(self):
        # Neither a total of zero nor an instant finish can happen in a run, but neither may
        # raise an exception inside the loop that records a repository.
        progress = self._progress(total=0, every=1)
        self.assertIn("1/0", progress.note(False))
        instant = _guards.Progress(5, 0, 1, 600.0, clock=lambda: 1.0)
        self.assertIn("left at this rate", instant.note(False))

    def test_the_defaults_are_what_the_command_line_offers(self):
        args = _mod._build_arg_parser().parse_args(["--repo-csv", "x.csv"])
        self.assertEqual(args.progress_every, _guards.PROGRESS_EVERY)
        self.assertEqual(args.progress_seconds, _guards.PROGRESS_SECONDS)
        self.assertEqual(_guards.PROGRESS_EVERY, 100)
        self.assertEqual(_guards.PROGRESS_SECONDS, 600.0)


class TestTheGroundedConstants(unittest.TestCase):
    """The relationships that the GROUNDING comments of `agentcontext.guards` state. A changed
    number that breaks a relationship fails here."""
    def test_the_error_guard_reaches_a_defect_that_touches_the_repositories_with_files(self):
        # The right side is the share of the repositories of the March collection that
        # contributed a file. The failing share of a window that stops the step has to be below
        # that share. Otherwise a defect that affects every such repository never stops the step.
        self.assertLess(_guards.MAX_CONSECUTIVE_FAILURES / _guards.ERROR_WINDOW, 4799 / 36710)

    def test_the_ceiling_is_above_a_healthy_run_and_the_streak(self):
        # 36_710 is the number of repositories of the March collection, a healthy run that is
        # recorded as complete, and 17 is its number of clone_failed rows. At that size, the
        # ceiling is also above the number of failed clones in a row that stops the step.
        self.assertGreater(_guards.clone_failure_ceiling(36_710), 17)
        self.assertGreater(_guards.clone_failure_ceiling(36_710), _guards.MAX_CONSECUTIVE_FAILURES)

    def test_the_ceiling_grows_with_the_run_and_is_never_none(self):
        # A fixed ceiling of 50 would let most of a 67-repository run fail.
        self.assertEqual(_guards.clone_failure_ceiling(0), 1)
        self.assertEqual(_guards.clone_failure_ceiling(67), 1)
        self.assertLess(_guards.clone_failure_ceiling(10_000), _guards.clone_failure_ceiling(36_710))

    def test_a_retry_pause_outlasts_the_bursts_of_http_400_the_runs_met(self):
        # The bursts of HTTP 400 that docs/steps/repository_cloning.md describes lasted three
        # minutes each.
        self.assertGreater(_guards.RETRY_PAUSE_SECONDS, 3 * 60)
        self.assertGreaterEqual(_guards.RETRY_ROUNDS, 1)

    def test_the_probe_and_a_local_command_wait_at_least_the_github_request_timeout(self):
        from agentcontext.git import bounds
        from agentcontext import runner
        self.assertGreaterEqual(_guards.PROBE_TIMEOUT, runner.GITHUB_TIMEOUT_SECONDS)
        self.assertEqual(bounds.LOCAL_COMMAND_SECONDS,
                         runner.GITHUB_TIMEOUT_SECONDS)

    def test_the_progress_silence_is_under_one_git_clock(self):
        from agentcontext.git import history
        self.assertLess(_guards.PROGRESS_SECONDS, history.GIT_TIMEOUT_SECONDS)

    def test_the_stderr_tail_fits_the_longest_line_the_first_deployment_logged(self):
        from agentcontext.git import bounds
        self.assertGreaterEqual(bounds.STDERR_TAIL, 101)


class TestTheProcessCounts(unittest.TestCase):

    def test_counts_come_from_proc_or_are_none(self):
        counts = _guards.process_counts()
        if not Path("/proc").is_dir():
            self.assertIsNone(counts)
            return
        processes, zombies = counts
        self.assertGreater(processes, 0)
        self.assertLess(zombies, processes, "this process is not a zombie")

    @unittest.skipUnless(Path("/proc").is_dir(), "the counts are read from /proc")
    def test_a_zombie_is_counted_until_it_is_reaped(self):
        """A child that has exited and is not yet reaped is a zombie, and the count includes
        it."""
        child = subprocess.Popen(["true"])
        self.addCleanup(child.wait)
        status = Path(f"/proc/{child.pid}/status")
        deadline = time.monotonic() + 5
        # The state is read from /proc rather than through `child.poll()`, which would reap the
        # child.
        while "\tZ" not in next((line for line in status.read_text().splitlines()
                                 if line.startswith("State:")), ""):
            self.assertLess(time.monotonic(), deadline, "the child did not exit")
            time.sleep(0.01)
        _, with_it = _guards.process_counts()
        child.wait()
        _, without_it = _guards.process_counts()
        self.assertGreaterEqual(with_it, 1)
        self.assertGreater(with_it, without_it)

    def test_the_progress_line_contains_the_counts_where_proc_exists(self):
        with contextlib.redirect_stderr(io.StringIO()):
            line = _guards.Progress(10, 0, 1, 600.0, clock=lambda: 1.0).note(False)
        if Path("/proc").is_dir():
            self.assertIn("processes with", line)
        else:
            self.assertNotIn("processes", line)


if __name__ == "__main__":
    unittest.main()
