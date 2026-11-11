#!/usr/bin/env python3
"""
Tests for agentcontext/tools/mirror_run.py

Strategy:
  - The guards refuse this repository as the checkout, a checkout with an origin other than
    this repository, a list without a repo_name column and a list over the row ceiling.
  - The clones' environment has the mirror rewrite and no GitHub access token.
  - A step past its time limit is sent SIGTERM, and SIGKILL when it runs past the grace
    period. The exit code is the signal's, and the result records that the run killed the
    step.
  - The history fetch of one repository fails through git itself. A clone rewritten to a copy
    of its mirror that contains only the tip stays shallow after `git fetch --unshallow`,
    which exits 0. The tests clone a mirror that they make of a repository that they make.
  - The run checks that the failure happened, that the step tried the repository again and
    that the file history step measured its rows, and fails when any of the three did not.
  - The whole subsample run over real mirrors is started by hand, not by these tests.

Run with:  python3 -m unittest tests.test_tools_mirror_run -v
Requirements: Python 3.14, stdlib only, git on PATH.
"""

import importlib
import shutil
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agentcontext import SEVEN_ZIP
from unittest import mock

from tests._fixtures import report_counts

_mod = importlib.import_module("agentcontext.tools.mirror_run")


class TestTheGuards(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _repo(self, name, origin=None):
        repo = self.tmp / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        if origin is not None:
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", origin], check=True)
        return repo

    def test_this_repository_is_refused_as_the_checkout(self):
        with self.assertRaises(_mod.Refused) as caught:
            _mod.checkout_of_this_repository(_mod.REPO_ROOT)
        self.assertIn("is this repository", str(caught.exception))

    def test_a_checkout_with_a_remote_origin_is_refused(self):
        repo = self._repo("elsewhere", "https://github.com/owner/repo.git")
        with self.assertRaises(_mod.Refused) as caught:
            _mod.checkout_of_this_repository(repo)
        self.assertIn("not a local directory", str(caught.exception))

    def test_a_checkout_of_another_local_repository_is_refused(self):
        other = self._repo("other")
        clone = self._repo("clone", str(other))
        with self.assertRaises(_mod.Refused) as caught:
            _mod.checkout_of_this_repository(clone)
        self.assertIn("not this repository", str(caught.exception))

    def test_a_checkout_of_this_repository_passes(self):
        clone = self._repo("clone", str(_mod.REPO_ROOT))
        _mod.checkout_of_this_repository(clone)

    def test_a_list_without_the_column_is_refused(self):
        listing = self.tmp / "list.csv"
        listing.write_text("name\nowner/repo\n")
        with self.assertRaises(_mod.Refused):
            _mod.count_rows(listing)

    def test_the_rows_are_counted_without_the_header(self):
        listing = self.tmp / "list.csv"
        listing.write_text("repo_name\n" + "".join(f"o/r{i}\n" for i in range(3)))
        self.assertEqual(_mod.count_rows(listing), 3)

    def test_the_development_subsample_with_the_history_repository_is_under_the_ceiling(self):
        """The run list is `data/collection/dev_subsample.csv` and the repository with the
        history fetch that the run makes fail (docs/validation/README.md). The subsample tool
        draws as many repositories as the heuristics need."""
        rows = _mod.count_rows(_mod.REPO_ROOT / "data" / "collection" / "dev_subsample.csv")
        self.assertLessEqual(rows + 1, _mod.MAX_ROWS)

    def test_a_list_that_cannot_be_read_is_refused(self):
        """The run list fails in the same ways as the file of expected findings: a list that is
        not text, a list that cannot be read, and a field over the csv module's default limit.
        Each failure is refused in one line, as every other guard refuses, rather than with a
        traceback.

        The test runs the command line in a subprocess, because the field limit is a
        process-wide setting that other modules of the package raise when they are imported.
        In a process that also runs `tests.test_commits`, the large field would be read, while
        the `python -m agentcontext mirror-run` that a person types has the default limit."""
        clone = self._repo("clone", str(_mod.REPO_ROOT))
        cases = {"binary.7z": b"\xff\xfe\x00\x01",
                 "huge.csv": ("repo_name\n" + "x" * 200_000 + "\n").encode("utf-8")}
        for name, content in cases.items():
            with self.subTest(name=name):
                listing = self.tmp / name
                listing.write_bytes(content)
                result = subprocess.run(
                    [sys.executable, "-m", "agentcontext", "mirror-run",
                     "--checkout", str(clone), "--repo-csv", str(listing),
                     "--mirrors", str(self.tmp), "--reference", str(self.tmp)],
                    capture_output=True, text=True, cwd=str(_mod.REPO_ROOT), timeout=120)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"Could not read the run list {listing.resolve()}", result.stderr)

    def test_a_list_over_the_ceiling_is_refused_by_main(self):
        """Every guard after the ceiling passes here, and the refusal can therefore come only
        from the ceiling. A reference without output/ would fail its own guard with exit 1, and
        the test would pass with the ceiling removed."""
        listing = self.tmp / "list.csv"
        listing.write_text("repo_name\n" + "".join(f"o/r{i}\n" for i in range(_mod.MAX_ROWS + 1)))
        clone = self._repo("clone", str(_mod.REPO_ROOT))
        reference = self.tmp / "reference"
        (reference / "output").mkdir(parents=True)
        err = io.StringIO()
        with (mock.patch.object(_mod, "reset_checkout",
                                side_effect=AssertionError("the ceiling did not refuse")),
              mock.patch.object(sys, "stderr", err)):
            code = _mod.main(["--checkout", str(clone), "--repo-csv", str(listing),
                              "--mirrors", str(self.tmp), "--reference", str(reference)])
        self.assertEqual(code, 1)
        self.assertIn(f"has {_mod.MAX_ROWS + 1} rows, but a subsample run reads at most "
                      f"{_mod.MAX_ROWS}", err.getvalue())


class TestTheClonesEnvironment(unittest.TestCase):

    def test_the_rewrite_is_set_and_the_github_access_tokens_are_dropped(self):
        os.environ["GITHUB_TOKEN"] = "secret"
        self.addCleanup(os.environ.pop, "GITHUB_TOKEN", None)
        mirrors = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(mirrors, ignore_errors=True))
        checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(checkout, ignore_errors=True))
        env = _mod.child_environment(mirrors, checkout)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("GH_TOKEN", env)
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], f"url.{mirrors.resolve().as_uri()}/.insteadOf")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "https://github.com/")
        # The run uses the package and the data root of the checkout and therefore writes
        # nothing into the caches of this repository.
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(checkout.resolve()))
        self.assertEqual(env["AGENTCONTEXT_DATA_ROOT"], str(checkout.resolve() / "data"))

    def test_a_checkout_without_the_package_is_refused_before_anything_runs(self):
        checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(checkout, ignore_errors=True))
        (checkout / "data" / "collection").mkdir(parents=True)
        env = _mod.child_environment(checkout, checkout)
        with self.assertRaises(_mod.Refused) as caught:
            _mod.package_in(checkout, env)
        self.assertIn("not the checkout's own", str(caught.exception))

    def test_a_checkout_with_the_package_passes(self):
        checkout = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(checkout, ignore_errors=True))
        (checkout / "data" / "collection").mkdir(parents=True)
        (checkout / "agentcontext").mkdir()
        (checkout / "agentcontext" / "__init__.py").write_text(
            (_mod.REPO_ROOT / "agentcontext" / "__init__.py").read_text())
        _mod.package_in(checkout, _mod.child_environment(checkout, checkout))


class TestTheFindings(unittest.TestCase):

    def test_the_roots_paths_are_replaced_and_the_header_dropped(self):
        report = ("2 difference(s) between /a/ref and /b/run, ignoring scanned_at\n"
                  "  output/repos.csv.branch changed on 1 row(s) (o/a)\n"
                  "  ai_authorship.csv gained 3 row(s) (o/r /b/run/x)\n")
        self.assertEqual(_mod.normalized_findings(report, (Path("/a/ref"), Path("/b/run"))),
                         ["output/repos.csv.branch changed on 1 row(s) (o/a)",
                          "ai_authorship.csv gained 3 row(s) (o/r <root>/x)"])

    def test_the_verdict_is_both_directions(self):
        extra, missing = _mod.unexpected(["a", "b"], ["b", "c"])
        self.assertEqual((extra, missing), (["a"], ["c"]))
        self.assertEqual(_mod.unexpected(["a"], ["a"]), ([], []))


class TestTheFirstCollect(unittest.TestCase):
    """What `main` does with the first collection's exit code and whether the run killed it.

    The run kills a collection and resumes it. A first collection that ends on its own is
    therefore one of two cases, and the run reports them separately: a window longer than the
    work, which tests neither the stop nor the resume, and a collection that failed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.checkout = self.tmp / "clone"
        self.checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(self.checkout)], check=True)
        subprocess.run(["git", "-C", str(self.checkout), "remote", "add", "origin",
                        str(_mod.REPO_ROOT)], check=True)
        (self.checkout / "data" / "collection").mkdir(parents=True)
        self.listing = self.tmp / "list.csv"
        self.listing.write_text("repo_name\no/r\n", encoding="utf-8")
        self.reference = self.tmp / "reference"
        (self.reference / "output").mkdir(parents=True)
        self.mirrors = self.tmp / "mirrors"
        # The listed repository has a mirror, as every repository of a run list has except a
        # clone failure. A stub below replaces the copy of the mirror.
        (self.mirrors / "o" / "r.git").mkdir(parents=True)
        # The stubs return what a run reads back after the injected history failure: the copy
        # of the mirror, the failure that the cloning step recorded, the step's second attempt
        # at the repository and the rows that the file history step measured. Each test of a
        # check overrides one stub.
        self.shallow = self.tmp / "shallow"
        # The kill left two repositories finished: the repository with the failing history,
        # which the step tries again, and a repository that the resume takes over with its one
        # line.
        for name, value in (("shallow_copy", {"return_value": self.shallow}),
                            ("history_outcome", {"return_value": "history_unavailable"}),
                            ("finished_repositories",
                             {"return_value": frozenset({"o/r", "o/done"})}),
                            ("finished_records",
                             {"side_effect": lambda stage, repo: 3 if repo == "o/r" else 1}),
                            ("rows_without_history", {"return_value": (3, 0)})):
            patcher = mock.patch.object(_mod, name, **value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def _main(self, first, extra=()):
        argv = ["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                "--mirrors", str(self.mirrors), "--reference", str(self.reference), *extra]
        with (mock.patch.object(_mod, "reset_checkout", return_value="0" * 40),
              mock.patch.object(_mod, "package_in"),
              mock.patch.object(_mod, "run_step_killed", return_value=first),
              mock.patch.object(_mod, "extract_inputs"),
              mock.patch.object(_mod, "run_step", side_effect=self._step(0, [])) as after):
            return _mod.main(argv), after

    def _step(self, compare_code, finding_lines):
        """A `run_step` stub. Its comparison writes the findings that the run reads to the file
        that `--report` names, as `regressions compare` does."""
        def step(name, argv, *rest, **kw):
            if name == 'commit_reclassification':
                # The injected history failure stays within the cloning step's ceiling. The
                # step records an incomplete reclassification and exits 0.
                root = rest[0]
                repo = self.shallow_copy.call_args.args[1]
                report = root / "output" / "commit-reclassification.json"
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(json.dumps({
                    "assembly_complete": True, "complete": False,
                    "population_complete": False,
                    "requested_repositories": 1, "counts": report_counts(history_not_read=1),
                    "repositories": {repo: {"outcome": "history_not_read"}},
                }), encoding="utf-8")
                return 0
            if name == "compare":
                report = Path(argv[argv.index("--report") + 1])
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text("header\n" + "".join(l + "\n" for l in finding_lines),
                                  encoding="utf-8")
                return compare_code
            return 0
        return step

    def test_an_incomplete_reclassification_continues_to_publication_as_the_runner_does(self):
        """After a reclassification that exits 0 with the injected history failure, the runner
        collects nothing again and continues through publication. The run does the same."""
        code, after = self._main((143, True))
        names = [call.args[0] for call in after.call_args_list]
        self.assertEqual(code, 0)
        self.assertEqual(names.count('repository_cloning'), 1, "only the resume collects")
        self.assertLess(names.index('commit_reclassification'), names.index('file_history'))
        self.assertLess(names.index('file_history'), names.index('release'))
        logs = self.checkout.parent / f"{self.checkout.name}-logs-00000000"
        self.assertFalse(json.loads((logs / "reclassification-incomplete.json").read_text())["complete"])

    def test_missing_reclassification_report_prevents_publication(self):
        names = []
        original = self._step(0, [])
        def step(name, *args, **kwargs):
            names.append(name)
            return 0 if name == 'commit_reclassification' else original(name, *args, **kwargs)
        with mock.patch.object(self, "_step", return_value=step):
            code, _ = self._main((143, True))
        self.assertEqual(code, 2)
        self.assertNotIn('release', names)

    def test_a_first_collect_that_failed_before_the_window_is_a_failed_step(self):
        code, after = self._main((4, False))
        self.assertEqual(code, 2)
        self.assertEqual(after.call_count, 0, "the steps after it ran over a failed collection")

    def test_a_first_collect_that_finished_before_the_window_refuses(self):
        code, after = self._main((0, False))
        self.assertEqual(code, 1)
        self.assertEqual(after.call_count, 0)

    def test_a_killed_first_collect_runs_the_steps_after_it(self):
        code, after = self._main((143, True))
        self.assertEqual(after.call_count, len(_mod.steps_after_collect()) + 2,
                         "the resume, the steps after the collection and the comparison")
        self.assertEqual(code, 0)

    def test_an_expected_findings_file_that_is_not_there_is_refused_before_anything_runs(self):
        # With `package_in` stubbed too, the only refusal left is the refusal under test. The
        # test asserts the message, because the refusal of the package also returns 1.
        err = io.StringIO()
        with (mock.patch.object(_mod, "reset_checkout", return_value="0" * 40),
              mock.patch.object(_mod, "package_in"),
              mock.patch.object(sys, "stderr", err),
              mock.patch.object(_mod, "run_step_killed", return_value=(143, True)) as killed):
            code = _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                              "--mirrors", str(self.mirrors), "--reference", str(self.reference),
                              "--expected-findings", str(self.tmp / "nowhere.txt")])
        self.assertEqual(code, 1)
        self.assertIn("Could not read the file of expected findings", err.getvalue())
        killed.assert_not_called()

    def test_an_expected_findings_file_that_is_not_text_is_refused(self):
        """A decode failure is a `ValueError`, not an `OSError`. Given the archive beside the
        list, a guard that caught only `OSError` would end the run with a traceback rather than
        one line."""
        binary = self.tmp / "expected.7z"
        binary.write_bytes(b"\xff\xfe\x00\x01not utf-8")
        err = io.StringIO()
        with (mock.patch.object(_mod, "reset_checkout", return_value="0" * 40),
              mock.patch.object(_mod, "package_in"),
              mock.patch.object(sys, "stderr", err),
              mock.patch.object(_mod, "run_step_killed", return_value=(143, True)) as killed):
            code = _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                              "--mirrors", str(self.mirrors), "--reference", str(self.reference),
                              "--expected-findings", str(binary)])
        self.assertEqual(code, 1)
        self.assertIn("Could not read the file of expected findings", err.getvalue())
        killed.assert_not_called()

    def test_a_comparison_that_could_not_check_cannot_match_expected_findings(self):
        for compare_code, lines, expected_text in ((1, [], ""), (1, ["a finding"], "a finding\n"),
                                                    (-9, [], "")):
            with self.subTest(compare_code=compare_code, lines=lines):
                expected = self.tmp / "expected.txt"
                expected.write_text(expected_text, encoding="utf-8")
                with (mock.patch.object(_mod, "reset_checkout", return_value="0" * 40),
                      mock.patch.object(_mod, "package_in"),
                      mock.patch.object(_mod, "run_step_killed", return_value=(143, True)),
                      mock.patch.object(_mod, "extract_inputs"),
                      mock.patch.object(_mod, "run_step", side_effect=self._step(compare_code, lines))):
                    code = _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                                      "--mirrors", str(self.mirrors), "--reference", str(self.reference),
                                      "--expected-findings", str(expected)])
                self.assertEqual(code, 2)

    def test_the_expected_findings_are_read_before_the_checkout_is_cleaned(self):
        """`reset_checkout` runs `git clean -xdfq`, which removes a list inside the checkout
        before the run compares. The guard therefore reads the list first."""
        expected = self.checkout / "expected.txt"
        expected.write_text("a finding\n", encoding="utf-8")
        with (mock.patch.object(_mod, "reset_checkout",
                                side_effect=lambda *a, **k: (expected.unlink(), "0" * 40)[1]),
              mock.patch.object(_mod, "package_in"),
              mock.patch.object(_mod, "run_step_killed", return_value=(143, True)),
              mock.patch.object(_mod, "extract_inputs"),
              mock.patch.object(_mod, "run_step", side_effect=self._step(2, ["a finding"]))):
            code = _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                              "--mirrors", str(self.mirrors), "--reference", str(self.reference),
                              "--expected-findings", str(expected)])
        self.assertEqual(code, 0, "the run did not compare with the list that it was given")

    def test_the_runs_own_findings_are_not_the_list_that_it_is_compared_with(self):
        """The run writes its findings to `<logs>/findings.txt` before it reads the expected
        list. Given that path as the expected list, the run would compare its findings with
        themselves, expect every finding and exit 0 whatever the comparison found."""
        logs = self.checkout.parent / f"{self.checkout.name}-logs-00000000"
        logs.mkdir()
        (logs / "findings.txt").write_text("what the previous run found\n", encoding="utf-8")
        with (mock.patch.object(_mod, "reset_checkout", return_value="0" * 40),
              mock.patch.object(_mod, "package_in"),
              mock.patch.object(_mod, "run_step_killed", return_value=(143, True)),
              mock.patch.object(_mod, "extract_inputs"),
              mock.patch.object(_mod, "run_step",
                                side_effect=self._step(2, ["something else entirely"]))):
            code = _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                              "--mirrors", str(self.mirrors), "--reference", str(self.reference),
                              "--expected-findings", str(logs / "findings.txt")])
        self.assertEqual(code, 2, "a real difference was reported as agreement")


    def test_the_history_of_the_first_repository_of_the_list_fails(self):
        self._main((143, True))
        self.assertEqual(self.shallow_copy.call_args.args[1], "o/r")
        self._main((143, True), ["--history-fails", "o/other"])
        self.assertEqual(self.shallow_copy.call_args.args[1], "o/other")

    def test_both_collects_keep_the_minimum_the_run_names(self):
        for extra, expected in (((), None), (("--min-free-gb", "20"), "20")):
            with self.subTest(extra=extra), \
                    mock.patch.object(_mod, "reset_checkout", return_value="0" * 40), \
                    mock.patch.object(_mod, "package_in"), \
                    mock.patch.object(_mod, "run_step_killed", return_value=(143, True)) as first, \
                    mock.patch.object(_mod, "extract_inputs"), \
                    mock.patch.object(_mod, "run_step", side_effect=self._step(0, [])) as after:
                _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                           "--mirrors", str(self.mirrors), "--reference", str(self.reference),
                           *extra])
                resumed = next(call.args[1] for call in after.call_args_list
                               if call.args[0] == 'repository_cloning')
                for argv in (first.call_args.args[1], resumed):
                    given = argv[argv.index("--min-free-gb") + 1] if "--min-free-gb" in argv else None
                    self.assertEqual(given, expected)

    def test_both_collects_clone_from_the_copy_and_no_later_step_does(self):
        with (mock.patch.object(_mod, "reset_checkout", return_value="0" * 40),
              mock.patch.object(_mod, "package_in"),
              mock.patch.object(_mod, "run_step_killed", return_value=(143, True)) as first,
              mock.patch.object(_mod, "extract_inputs"),
              mock.patch.object(_mod, "run_step", side_effect=self._step(0, [])) as after):
            _mod.main(["--checkout", str(self.checkout), "--repo-csv", str(self.listing),
                       "--mirrors", str(self.mirrors), "--reference", str(self.reference)])
        self.assertEqual(first.call_args.args[1][-2:],
                         ["--retry-pause", str(_mod.RETRY_PAUSE_SECONDS)])
        rewrite = f"url.{self.shallow.as_uri()}.insteadOf"
        envs = {call.args[0]: call.args[3] for call in after.call_args_list}
        self.assertIn(rewrite, first.call_args.args[3].values())
        self.assertIn(rewrite, envs['repository_cloning'].values())
        for name, env in envs.items():
            if name != 'repository_cloning':
                self.assertNotIn(rewrite, env.values(), name)
        git_data = next(call.args[1] for call in after.call_args_list
                        if call.args[0] == 'file_history')
        self.assertEqual(git_data[-3:], ["--offline", "--offline-except", "o/r"])

    def test_a_history_failure_that_did_not_happen_fails_the_run(self):
        self.history_outcome.return_value = "history_read"
        code, _ = self._main((143, True))
        self.assertEqual(code, 2)

    def test_a_repository_the_step_did_not_try_again_fails_the_run(self):
        # With one line in the list of finished repositories, the repository was collected
        # once, by the first collection or by the resume, and neither the resume nor a retry
        # round of the step tried it again.
        for lines in (0, 1):
            with self.subTest(lines=lines):
                self.finished_records.side_effect = (
                    lambda stage, repo, lines=lines: lines if repo == "o/r" else 1)
                self.assertEqual(self._main((143, True))[0], 2)

    def test_a_collection_that_did_not_stop_on_sigterm_fails_the_run(self):
        """The run tests the stop as well as the resume. The step's signal handlers end it with
        143. A traceback, `LowDiskStop`'s 4, SIGKILL after the grace period or a step with no
        handler at all means that the step did not stop as it should, and the run must not
        continue with the resume."""
        for first in ((1, True), (4, True), (-9, True), (-15, True)):
            with self.subTest(first=first):
                code, after = self._main(first)
                self.assertEqual(code, 2)
                self.assertEqual(after.call_count, 0)

    def test_a_kill_before_any_repository_finished_refuses(self):
        """With nothing finished, the resume is a first collection and takes nothing over."""
        self.finished_repositories.return_value = frozenset({"o/r"})
        code, after = self._main((143, True))
        self.assertEqual(code, 1)
        self.assertEqual(after.call_count, 0)

    def test_a_resume_that_collects_a_finished_repository_again_fails_the_run(self):
        self.finished_records.side_effect = lambda stage, repo: 3 if repo == "o/r" else 2
        self.assertEqual(self._main((143, True))[0], 2)

    def test_a_reclassification_that_exits_otherwise_than_expected_fails_the_run(self):
        """The reclassification has to exit 0 with the injected history failure, whatever its
        JSON record states, because the runner stops on any other exit code."""
        original = self._step(0, [])
        names = []
        for code in (1, 2):
            def step(step_name, *args, code=code, **kwargs):
                names.append(step_name)
                result = original(step_name, *args, **kwargs)
                return code if step_name == 'commit_reclassification' else result
            with self.subTest(code=code), mock.patch.object(self, "_step", return_value=step):
                self.assertEqual(self._main((143, True))[0], 2)
                self.assertNotIn('release', names)

    def test_the_logs_are_kept_beside_the_checkout(self):
        """Logs inside the checkout, including the coverage reports, would last only until the
        next run, because its `git clean -xdfq` removes everything in the checkout."""
        code, _ = self._main((143, True))
        self.assertEqual(code, 0)
        logs = self.checkout.parent / f"{self.checkout.name}-logs-00000000"
        self.assertTrue((logs / "reclassification-incomplete.json").is_file())
        self.assertFalse((self.checkout / "_mirror_run_logs").exists())
        # The log directory also records the git version, on which the history columns depend.
        self.assertTrue((logs / "git-version.txt").read_text().startswith("git version "))

    def test_rows_that_the_file_history_step_left_without_history_fail_the_run(self):
        for rows in ((3, 1), (0, 0)):
            with self.subTest(rows=rows):
                self.rows_without_history.return_value = rows
                self.assertEqual(self._main((143, True))[0], 2)


def _git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


class TestTheFailedHistory(unittest.TestCase):
    """Git itself produces the failure, over a mirror that the test makes. A clone that the
    rewrite sends to the copy of the mirror is shallow, and `fetch --unshallow` exits 0 and
    leaves it shallow. The cloning step reads the shallow clone as a history that did not
    arrive."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        work = self.tmp / "work"
        _git("init", "-q", "-b", "main", str(work))
        for n in range(3):
            (work / "CLAUDE.md").write_text(f"version {n}\n", encoding="utf-8")
            _git("add", "CLAUDE.md", cwd=work)
            _git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q",
                 "-m", f"commit {n}", cwd=work)
        self.mirrors = self.tmp / "mirrors"
        _git("clone", "-q", "--mirror", work.as_uri(), str(self.mirrors / "o" / "r.git"))
        self.head = _git("rev-parse", "HEAD", cwd=work)

    def _clone(self, env, name):
        target = self.tmp / name
        subprocess.run(["git", "clone", "-q", "--depth", "1", "https://github.com/o/r.git",
                        str(target)], env=env, check=True, capture_output=True)
        return target

    def test_the_copy_is_shallow_and_at_the_mirrors_head(self):
        copy = _mod.shallow_copy(self.mirrors, "o/r", self.tmp / "shallow")
        self.assertEqual(_git("rev-parse", "--is-shallow-repository", cwd=copy), "true")
        self.assertEqual(_git("rev-parse", "HEAD", cwd=copy), self.head)

    def test_the_repositories_of_the_list_without_a_mirror_are_named(self):
        listing = self.tmp / "list.csv"
        listing.write_text("repo_name\no/r\nso/gone\n", encoding="utf-8")
        mirrors = self.tmp / "mirrors-of-one"
        (mirrors / "o" / "r.git").mkdir(parents=True)
        self.assertEqual(_mod.repositories_without_a_mirror(mirrors, listing),
                         frozenset({"so/gone"}))

    def test_a_copy_that_is_not_shallow_is_refused(self):
        """The failure that the run causes needs a shallow copy. From a full copy,
        `--unshallow` fetches the history, and the run would test nothing."""
        real = _mod.git
        def answering(*args, **kwargs):
            if args[:2] == ("rev-parse", "--is-shallow-repository"):
                return "false"
            return real(*args, **kwargs)
        with mock.patch.object(_mod, "git", side_effect=answering), \
                self.assertRaisesRegex(_mod.Refused, "is not shallow"):
            _mod.shallow_copy(self.mirrors, "o/r", self.tmp / "copies")

    def test_a_repository_without_a_mirror_is_refused(self):
        with self.assertRaises(_mod.Refused):
            _mod.shallow_copy(self.mirrors, "o/absent", self.tmp / "shallow")

    def test_the_history_does_not_arrive_from_the_copy_and_does_from_the_mirror(self):
        copy = _mod.shallow_copy(self.mirrors, "o/r", self.tmp / "shallow")
        env = _mod.child_environment(self.mirrors, self.tmp)
        injected = self._clone(_mod.collect_environment(env, "o/r", copy), "injected")
        fetch = subprocess.run(["git", "fetch", "--unshallow"], cwd=injected, capture_output=True,
                               env=_mod.collect_environment(env, "o/r", copy))
        self.assertEqual(fetch.returncode, 0)
        self.assertEqual(_git("rev-parse", "--is-shallow-repository", cwd=injected), "true")
        plain = self._clone(env, "plain")
        subprocess.run(["git", "fetch", "-q", "--unshallow"], cwd=plain, env=env, check=True)
        self.assertEqual(_git("rev-parse", "--is-shallow-repository", cwd=plain), "false")
        self.assertEqual(_git("rev-list", "--count", "HEAD", cwd=plain), "3")

    def test_the_repositorys_own_rewrite_is_added_after_the_mirrors(self):
        env = _mod.collect_environment(_mod.child_environment(self.mirrors, self.tmp), "o/r",
                                       self.tmp / "shallow" / "o" / "r.git")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "https://github.com/")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "https://github.com/o/r.git")


class TestWhatTheRunReadsBack(unittest.TestCase):

    def setUp(self):
        self.step = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.step, True)

    def test_the_finished_lines_of_one_repository_are_counted(self):
        (self.step / "finished_repositories.jsonl").write_text(
            '{"repo": "o/r"}\n{"repo": "o/s"}\n{"repo": "o/r", "redo": true}\nnot json\n',
            encoding="utf-8")
        self.assertEqual(_mod.finished_records(self.step, "o/r"), 2)
        self.assertEqual(_mod.finished_records(self.step / "absent", "o/r"), 0)

    def test_the_outcome_is_the_per_repository_commit_record(self):
        shards = self.step / "collect_commits"
        shards.mkdir()
        (shards / "o§r.json").write_text('{"outcome": "history_unavailable"}', encoding="utf-8")
        self.assertEqual(_mod.history_outcome(self.step, "o/r"), "history_unavailable")
        self.assertIsNone(_mod.history_outcome(self.step, "o/absent"))

    def test_rows_without_git_columns_are_counted_over_the_mechanism_csvs(self):
        output = self.step / "output"
        output.mkdir()
        (output / "context-files.csv").write_text(
            "repo_name,created_at\no/r,2021-01-01T00:00:00Z\no/r,\no/s,\n", encoding="utf-8")
        (output / "skills.csv").write_text("repo_name,created_at\no/r,2022-01-01T00:00:00Z\n",
                                          encoding="utf-8")
        self.assertEqual(_mod.rows_without_history(self.step, "o/r"), (3, 1))


class TestReclassificationCoverage(unittest.TestCase):
    def _report(self, incomplete=False):
        outcomes = {"o/r": {"outcome": "history_not_read" if incomplete else "current"},
                    "o/s": {"outcome": "not_applicable"}}
        return {"repositories": outcomes, "requested_repositories": 2,
                "counts": report_counts(**{"history_not_read" if incomplete else "current": 1},
                                        not_applicable=1),
                "assembly_complete": True, "complete": not incomplete,
                "population_complete": not incomplete}

    def test_exact_injected_failure_is_accepted(self):
        _mod.check_reclassification_report(self._report(True), "o/r")

    def test_complete_recovery_is_accepted(self):
        _mod.check_reclassification_report(self._report())

    def test_an_unrelated_incomplete_repository_is_refused(self):
        report = self._report(True)
        report["repositories"]["o/s"]["outcome"] = "no_log"
        report["counts"] = report_counts(history_not_read=1, no_log=1)
        with self.assertRaises(_mod.Refused):
            _mod.check_reclassification_report(report, "o/r")

    def test_failure_of_another_repository_does_not_exercise_injection(self):
        with self.assertRaises(_mod.Refused):
            _mod.check_reclassification_report(self._report(True), "o/other")

    def test_zero_exit_flags_cannot_hide_missing_evidence(self):
        report = self._report(True)
        report["complete"] = report["population_complete"] = True
        with self.assertRaises(_mod.Refused):
            _mod.check_reclassification_report(report)

    def test_incomplete_assembly_and_population_are_refused(self):
        for field in ("assembly_complete", "complete", "population_complete"):
            with self.subTest(field=field):
                report = self._report()
                report[field] = False
                with self.assertRaises(_mod.Refused):
                    _mod.check_reclassification_report(report)

    def _without_a_mirror(self, report: dict) -> dict:
        # solidjs/signals has left GitHub. The run list includes it without a mirror, and the
        # reclassification records the clone failure as `unavailable`.
        report["repositories"]["solidjs/signals"] = {"outcome": "unavailable",
                                                     "reason": "not_found_or_no_access"}
        report["requested_repositories"] += 1
        report["counts"]["unavailable"] = 1
        report["population_complete"] = False
        return report

    def test_a_repository_without_a_mirror_is_an_expected_clone_failure(self):
        # A run over a subsample that includes a repository without a mirror must not stop
        # here.
        unmirrored = frozenset({"solidjs/signals"})
        _mod.check_reclassification_report(self._without_a_mirror(self._report(True)), "o/r",
                                           unmirrored)
        _mod.check_reclassification_report(self._without_a_mirror(self._report()), None,
                                           unmirrored)

    def test_unavailable_is_refused_for_a_repository_with_a_mirror(self):
        with self.assertRaises(_mod.Refused):
            _mod.check_reclassification_report(self._without_a_mirror(self._report()))

    def test_a_repository_without_a_mirror_has_to_be_unavailable(self):
        report = self._report()
        report["population_complete"] = False
        with self.assertRaises(_mod.Refused):
            _mod.check_reclassification_report(report, None, frozenset({"o/s"}))

    def test_a_report_over_other_repositories_than_the_run_list_is_refused(self):
        """A complete report over other repositories than the run list names is refused."""
        _mod.check_reclassification_report(self._report(), None, frozenset(),
                                           frozenset({"o/r", "o/s"}))
        for run_list in (frozenset({"o/r", "o/s", "o/t"}), frozenset({"o/r"})):
            with self.subTest(run_list=sorted(run_list)), self.assertRaises(_mod.Refused):
                _mod.check_reclassification_report(self._report(), None, frozenset(), run_list)

    def test_missing_population_and_inconsistent_counts_are_refused(self):
        for field, value in (("requested_repositories", 3), ("counts", report_counts(current=2)),
                             ("repositories", [])):
            with self.subTest(field=field):
                report = self._report()
                report[field] = value
                with self.assertRaises(_mod.Refused):
                    _mod.check_reclassification_report(report)


class TestTheKill(unittest.TestCase):
    """`run_step_killed` on real processes. `main` reads its second value to distinguish a
    stop and a resume from two ordinary collections. The tests above stub `run_step_killed`,
    and the tests here call it directly, because a kill reported as a finish would pass
    through `run_step`, which drops the second value."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _script(self, name: str, text: str) -> list:
        (self.tmp / name).write_text(text, encoding="utf-8")
        return [str(self.tmp / name)]

    def test_a_step_past_its_clock_is_sent_sigterm(self):
        argv = self._script("sleeper.py", "import time\ntime.sleep(60)\n")
        result = _mod.run_step_killed("sleeper", argv, self.tmp, dict(os.environ), self.tmp,
                                       kill_after=0.5)
        self.assertEqual(result, (-signal.SIGTERM, True))
        self.assertTrue((self.tmp / "sleeper.log").exists())

    def test_a_step_that_ignores_sigterm_is_killed_after_the_grace(self):
        """The test shortens the grace period. The child ignores SIGTERM from its first line and
        then prints `ignoring` on stdout. The test's `Popen` returns the step only once its log
        holds that line, and `run_step_killed` starts its clock when `Popen` returns, so SIGTERM
        finds the handler in place however long the interpreter takes to start."""
        argv = self._script("stubborn.py",
                            "import signal, sys, time\n"
                            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                            "print('ignoring', flush=True)\n"
                            "time.sleep(60)\n")
        log, real_popen, ready = self.tmp / "stubborn.log", subprocess.Popen, []

        def popen(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            # The bound only turns a step that hangs before its first line into a failure.
            deadline = time.monotonic() + 600
            while log.read_text(encoding="utf-8") != "ignoring\n":
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    proc.wait()
                    self.fail("the step did not start ignoring SIGTERM")
                time.sleep(0.01)
            ready.append(time.monotonic())
            return proc

        with mock.patch.object(_mod.subprocess, "Popen", popen):
            result = _mod.run_step_killed("stubborn", argv, self.tmp, dict(os.environ),
                                           self.tmp, kill_after=1.5, grace=0.5)
        self.assertEqual(log.read_text(encoding="utf-8"), "ignoring\n")
        self.assertEqual(result, (-signal.SIGKILL, True))
        # A run that waited the default grace period after SIGTERM rather than `grace` would
        # take longer than that period.
        self.assertLess(time.monotonic() - ready[0], _mod.kill_grace_seconds())

    def test_the_grace_outlasts_the_cloning_steps_own_stop(self):
        """The step stops its workers within `WORKER_STOP_SECONDS` and reaps its git processes
        within `REAP_SECONDS`. A grace period shorter than the sum would kill the step during a
        stop that took its full time, and the run would count the kill as the stop."""
        from agentcontext.git.bounds import REAP_SECONDS
        from agentcontext.steps.repository_cloning import WORKER_STOP_SECONDS
        self.assertGreater(_mod.kill_grace_seconds(), WORKER_STOP_SECONDS + REAP_SECONDS)

    def test_a_step_that_ends_inside_its_clock_is_not_reported_killed(self):
        argv = self._script("quick.py", "raise SystemExit(3)\n")
        self.assertEqual(_mod.run_step_killed("quick", argv, self.tmp, dict(os.environ),
                                               self.tmp, kill_after=30), (3, False))

    def test_a_step_that_ends_returns_its_code(self):
        argv = self._script("quick.py", "raise SystemExit(3)\n")
        self.assertEqual(_mod.run_step("quick", argv, self.tmp, dict(os.environ), self.tmp), 3)



class TestTheStepsAreTheContainers(unittest.TestCase):
    """The run takes the steps after the cloning step from the runner, in the runner's order,
    so that the check made before a NAS run reaches the publication. A list written here would
    miss a step that the runner gains."""

    def test_the_steps_after_collect_are_the_runners_in_its_order(self):
        runner = importlib.import_module("agentcontext.runner")
        container = [(s.key, list(s.args)) for s in runner.steps_for(True)
                     if s.key not in ('engineered_project_selection', 'repository_cloning')]
        here = [(name, [a for a in argv if a not in ("--offline", "--without-fetched-files")])
                for name, argv in _mod.steps_after_collect()]
        self.assertEqual(here, container)
        self.assertIn('release', [name for name, _ in here])
        self.assertEqual(here[0][0], 'commit_reclassification', "the commit heuristics apply before the detection")
        self.assertEqual([name for name, argv in _mod.steps_after_collect() if "--offline" in argv],
                         ['file_history'])

    def _archives(self, checkout: Path) -> None:
        """Create each archive of `INPUT_ARCHIVES` with its files, each file containing its own
        name."""
        for archive, names in _mod.INPUT_ARCHIVES:
            folder = (checkout / archive).parent
            folder.mkdir(parents=True, exist_ok=True)
            for name in names:
                (folder / name).write_text(f"{name}\n", encoding="utf-8")
            subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(checkout / archive), *names],
                           cwd=folder, check=True, capture_output=True)
            for name in names:
                (folder / name).unlink()

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_an_archived_input_is_extracted_beside_its_archive_and_kept_when_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._archives(checkout)
            _mod.extract_inputs(checkout)
            for archive, names in _mod.INPUT_ARCHIVES:
                for name in names:
                    self.assertEqual(((checkout / archive).parent / name)
                                     .read_text(encoding="utf-8"), f"{name}\n")
            kept = (checkout / _mod.INPUT_ARCHIVES[0][0]).parent / _mod.INPUT_ARCHIVES[0][1][0]
            kept.write_text("edited\n", encoding="utf-8")
            _mod.extract_inputs(checkout)
            self.assertEqual(kept.read_text(encoding="utf-8"), "edited\n")

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_the_record_of_the_labels_is_extracted_beside_labels_extracted_before_it(self):
        """A checkout can have the CSV of the labels alone, extracted from an archive without the
        record of the model that made the labels. An archive is still extracted when the CSV is
        beside it, because the release would otherwise refuse the publication of the subsample
        run."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._archives(checkout)
            archive, (labels, record, *_) = _mod.INPUT_ARCHIVES[0]
            folder = (checkout / archive).parent
            (folder / labels).write_text("edited\n", encoding="utf-8")
            _mod.extract_inputs(checkout)
            self.assertEqual((folder / record).read_text(encoding="utf-8"), f"{record}\n")
            self.assertEqual((folder / labels).read_text(encoding="utf-8"), "edited\n")

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_an_archive_that_7z_cannot_read_is_refused_in_one_line(self):
        """A failed extraction is a refusal in one line, not a CalledProcessError traceback at
        the end of the run."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            archive, names = _mod.INPUT_ARCHIVES[0]
            (checkout / archive).parent.mkdir(parents=True, exist_ok=True)
            (checkout / archive).write_bytes(b"not an archive")
            with self.assertRaises(_mod.Refused) as refused:
                _mod.extract_inputs(checkout)
            self.assertIn("7zz could not extract", str(refused.exception))
            for name in names:
                self.assertFalse(((checkout / archive).parent / name).exists())



class TestPublishedCommitComparisons(unittest.TestCase):

    def setUp(self):
        import csv
        from agentcontext.checks import regressions
        self.compare = regressions.compare_roots
        self.csv = csv
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.before = Path(self.tmp.name) / 'before'
        self.after = Path(self.tmp.name) / 'after'
        for directory in (self.before, self.after):
            (directory / 'output').mkdir(parents=True)

    def write(self, directory, filename, rows):
        with (directory / 'output' / filename).open('w', newline='') as out:
            writer = self.csv.writer(out)
            writer.writerow(['repo_name', 'commit_sha', 'ai_tool', 'matched_heuristics'])
            writer.writerows(rows)

    def test_heuristic_changes_remain_visible_when_a_commit_is_added(self):
        self.write(self.before, 'all_ai_commits.csv', [['a/b', 'a' * 40, 'Claude', 'claude-trailer-01']])
        self.write(self.after, 'all_ai_commits.csv', [
            ['a/b', 'a' * 40, 'Claude', 'claude-trailer-02'], ['a/b', 'b' * 40, 'Claude', 'claude-trailer-02']])
        findings = self.compare(self.before, self.after, set(_mod.IGNORED_COLUMNS))
        self.assertTrue(any('all_ai_commits.csv.matched_heuristics changed on 1 row' in f
                            for f in findings), findings)
        self.assertTrue(any('all_ai_commits.csv gained 1 row(s)' in f for f in findings), findings)

    def test_other_tool_commits_are_compared(self):
        self.write(self.before, 'other_ai_commits.csv', [])
        self.write(self.after, 'other_ai_commits.csv', [['a/b', 'c' * 40, 'Devin', '1']])
        findings = self.compare(self.before, self.after, set(_mod.IGNORED_COLUMNS))
        self.assertTrue(any('other_ai_commits.csv gained 1 row(s)' in f for f in findings),
                        findings)
        self.write(self.before, 'other_ai_commits.csv', [['a/b', 'c' * 40, 'Devin', '1']])
        self.assertEqual(self.compare(self.before, self.after, set(_mod.IGNORED_COLUMNS)), [])

    def test_the_json_records_of_a_repeat_run_differ_only_in_what_is_ignored(self):
        """The comparison reads the JSON records. Three of their keys contain the scan time of
        the run, and a repeat run therefore differs only in these keys. The comparison compares
        every other value."""
        def records(root, scanned, digest, sampled):
            (root / 'output' / 'snapshot.json').write_text(json.dumps({
                'collection': {'scanned_at_window': {'first': scanned, 'last': scanned},
                               'artifact_mtime_window': {'first': scanned, 'last': scanned},
                               'counts': {'sampled': sampled}},
                'commit_reclassification': {'collection_sha256': digest}}))
            (root / 'output' / 'commit-reclassification.json').write_text(json.dumps({
                'collection_sha256': digest, 'counts': {'current': sampled}}))

        records(self.before, '2026-10-14T20:10:46Z', 'a' * 64, 94)
        records(self.after, '2026-10-15T07:39:19Z', 'b' * 64, 94)
        self.assertEqual(self.compare(self.before, self.after, set(_mod.IGNORED_COLUMNS)), [])
        records(self.after, '2026-10-15T07:39:19Z', 'b' * 64, 93)
        findings = self.compare(self.before, self.after, set(_mod.IGNORED_COLUMNS))
        self.assertIn('1 value(s) of output/snapshot.json changed (/collection/counts/sampled '
                      'from 94 to 93)', findings)


if __name__ == "__main__":
    unittest.main()
