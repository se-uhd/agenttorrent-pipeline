"""
Tests for agentcontext.git.bounds: the time bound, the size bound and the process group of a git
command.

Strategy:
  - The bounds are tested against real processes, because the failure that they
    prevent is a real process that outlives its parent. Every command is a shell
    loop or a file write, and nothing reaches the network or takes long.
  - wanted_submodules is a pure function over a .gitmodules file.
  - The tests of the two git modules clone a repository built with `git init`, through
    a redirect of its GitHub URL, and read the bounds that each clone was given.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_git_bounds -v
"""

import contextlib
import importlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

from agentcontext.git import clone as _clone, history as _history
from tests import _ambient_env
from tests._fixtures import _add_commit, _add_noise, ext_redirect, sampled_every, upload_pack

_mod = importlib.import_module("agentcontext.git.bounds")


def _env():
    return os.environ.copy()


# A command with a leader that exits at once while the child that it started in the background
# keeps its stdout open and sleeps. The child writes its pid to the file named by `{marker}`.
_LEADER_EXITS = "sh -c 'echo $$ > {marker}; exec sleep 30' & exit 0"


def _exited(pid: int) -> bool:
    """Whether a child of this process has exited, checked without reaping it."""
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None


def _is_alive(pid: int) -> bool:
    """Treat a Linux zombie as exited even if its parent has not reaped it."""
    if Path("/proc").is_dir():
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except (FileNotFoundError, ProcessLookupError):
            # Linux fails a read of the stat file of a process reaped after the open with
            # ESRCH rather than ENOENT.
            return False
        if state == "Z":
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_gone(case: unittest.TestCase, pid: int) -> None:
    """Wait for the exit, and kill a surviving process before failing the test."""
    deadline = time.monotonic() + 2
    while _is_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _is_alive(pid):
        os.kill(pid, signal.SIGKILL)
        case.fail(f"process {pid} outlived the kill")


def _kill_recorded(*markers: Path) -> None:
    """Kill the process with the pid in each marker file. A test that fails before its own check
    would otherwise leave the endless loops of its command running."""
    for marker in markers:
        try:
            os.kill(int(marker.read_text().strip()), signal.SIGKILL)
        except (FileNotFoundError, ValueError, ProcessLookupError):
            pass


# The bound on a wait for something that a working command does at once, such as writing its pid
# or exiting. It only turns a hang into a failure.
_HANG_SECONDS = 60


def _wait_until(condition, message: str):
    """The first true value that `condition()` returns. Fails with `message` when none has come
    within _HANG_SECONDS."""
    deadline = time.monotonic() + _HANG_SECONDS
    while not (value := condition()):
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.01)
    return value


def _pid_in(marker: Path) -> int | None:
    """The pid that `echo $$ > marker` wrote, or None while the file is missing or its line is
    incomplete. The redirection creates the file before echo writes to it."""
    try:
        text = marker.read_text()
    except FileNotFoundError:
        return None
    return int(text) if text.endswith("\n") and text.strip().isdigit() else None


def _recorded_command(marker: Path) -> tuple:
    """The pid that the child of a command, run through `run_bounded` on another thread, wrote
    to `marker`, and the process that `run_bounded` recorded for the command, once both are
    there. The shell writes the marker and the thread records the process in no fixed order."""
    child = _wait_until(lambda: _pid_in(marker), f"the command wrote no pid to {marker}")
    (leader,) = _wait_until(lambda: list(_mod._RUNNING), "run_bounded recorded no command")
    return child, leader


@contextlib.contextmanager
def _time_bound_from(ready):
    """Start the time bound of `run_bounded`'s first wait on its command once `ready()` returns
    a true value, so that a short bound expires on a command that has started its children,
    however late the machine starts them. Yields a list that receives the monotonic time at
    which the bound started.

    The patch replaces `subprocess` in the module under test alone. A command that the module
    runs through `subprocess.run`, such as the probe of the parent-death wrapper, keeps the real
    `Popen`."""
    started = []

    class Popen(subprocess.Popen):
        def communicate(self, input=None, timeout=None):
            if not started:
                _wait_until(ready, "the command never became ready")
                started.append(time.monotonic())
            return super().communicate(input, timeout)

    module = types.SimpleNamespace(**{**vars(subprocess), "Popen": Popen})
    with unittest.mock.patch.object(_mod, "subprocess", module):
        yield started


class TestTheWaitsOfThisSuite(unittest.TestCase):
    """`_pid_in`, `_recorded_command` and `_time_bound_from` let a test check a command only
    after the command has done what the check needs."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_marker_holds_a_pid_once_its_line_is_complete(self):
        marker = self.dir / "child.pid"
        self.assertIsNone(_pid_in(marker))
        marker.write_text("")
        self.assertIsNone(_pid_in(marker))
        marker.write_text("42")
        self.assertIsNone(_pid_in(marker))
        marker.write_text("4242\n")
        self.assertEqual(_pid_in(marker), 4242)

    def test_the_record_of_the_command_can_follow_its_pid(self):
        """The thread that runs `run_bounded` can record the process after the child of the
        command has written its pid, here at the first poll after the pid."""
        marker = self.dir / "child.pid"
        marker.write_text("4242\n")
        leader = object()
        running = set()
        sleep = time.sleep

        def record_late(seconds):
            running.add(leader)
            sleep(0)

        with unittest.mock.patch.object(_mod, "_RUNNING", running), \
                unittest.mock.patch.object(time, "sleep", record_late):
            self.assertEqual(_recorded_command(marker), (4242, leader))

    def test_the_time_bound_starts_once_the_command_is_ready(self):
        """The child writes its pid only after the wait for it has begun, as a child does that a
        loaded machine starts late. A bound counted from the start of the command would kill the
        child before it wrote the pid."""
        marker, go = self.dir / "child.pid", self.dir / "go"
        self.addCleanup(_kill_recorded, marker)
        script = (f"sh -c 'while [ ! -e {go} ]; do sleep 0.01; done; echo $$ > {marker}; "
                  f"exec sleep 30' & sleep 30")

        def ready():
            go.touch()
            return _pid_in(marker)

        with _time_bound_from(ready) as started, \
                self.assertRaises(subprocess.TimeoutExpired):
            _mod.run_bounded(["sh", "-c", script], cwd=str(self.dir), env=_env(), timeout=1)
        self.assertEqual(len(started), 1)
        child = _pid_in(marker)
        self.assertIsNotNone(child, "the child never wrote its pid")
        _assert_gone(self, child)


class TestProcessLiveness(unittest.TestCase):
    def test_a_running_child_is_alive(self):
        child = subprocess.Popen(["sleep", "30"])
        try:
            self.assertTrue(_is_alive(child.pid))
        finally:
            child.kill()
            child.wait()
        self.assertFalse(_is_alive(child.pid))

    def test_a_process_reaped_while_its_stat_file_is_read_has_exited(self):
        """Linux fails the read with ESRCH when the process is reaped between the open of its
        stat file and the read."""
        child = subprocess.Popen(["true"])
        child.wait()

        def reaped(path, *args, **kwargs):
            raise ProcessLookupError(3, "No such process")

        with unittest.mock.patch.object(Path, "is_dir", return_value=True), \
                unittest.mock.patch.object(Path, "read_text", reaped):
            self.assertFalse(_is_alive(child.pid))

    @unittest.skipUnless(sys.platform == "linux", "Linux exposes zombie state in /proc")
    def test_an_unreaped_child_has_exited(self):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        try:
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOWAIT)
            os.kill(pid, 0)
            self.assertFalse(_is_alive(pid))
            _assert_gone(self, pid)
        finally:
            os.waitpid(pid, 0)


class TestMeasuredCloneRequirements(unittest.TestCase):
    def test_defaults_accommodate_the_measured_large_repository(self):
        from agentcontext.steps import file_history
        measured_bytes = int(28.16 * 1024 ** 3)
        self.assertGreaterEqual(_mod.DEFAULT_MAX_BYTES, measured_bytes)
        self.assertGreater(_history.GIT_TIMEOUT_SECONDS, 1446)
        parsed = []
        parse_args = file_history.cli.ArgumentParser.parse_args

        class Parsed(Exception):
            pass

        def capture(parser, argv):
            parsed.append(parse_args(parser, argv))
            raise Parsed

        with unittest.mock.patch.object(file_history.cli.ArgumentParser, "parse_args", capture):
            with self.assertRaises(Parsed):
                file_history.main([])
        self.assertGreaterEqual(parsed[0].max_clone_mb * 1024 ** 2, measured_bytes)


class TestTheStallBound(unittest.TestCase):

    def test_the_stall_bound_ends_before_the_time_bound(self):
        """A connection that the network dropped ends at the stall bound as a failure of the
        network, which the next run fetches again, and not at the time bound, after which no
        later run of the collection fetches the repository again (docs/git.md)."""
        self.assertLess(_mod.LOW_SPEED_SECONDS, _history.GIT_TIMEOUT_SECONDS)


class TestRemainingInside(unittest.TestCase):

    def test_a_path_inside_the_submodule_and_not_beside_it(self):
        """`library/` begins with the name of the submodule `lib` and is not inside it."""
        self.assertEqual(_mod.remaining_inside("lib", ["lib/a/CLAUDE.md", "library/b.md",
                                                       "./lib/c.md"]),
                         ["a/CLAUDE.md", "c.md"])


class TestDirectorySize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_an_empty_directory_is_zero(self):
        self.assertEqual(_mod.directory_size(self.dir), 0)

    def test_it_adds_up_nested_files(self):
        (self.dir / "a").write_bytes(b"x" * 100)
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "b").write_bytes(b"y" * 50)
        self.assertEqual(_mod.directory_size(self.dir), 150)

    def test_a_missing_directory_is_zero_rather_than_an_error(self):
        self.assertEqual(_mod.directory_size(self.dir / "nope"), 0)

    def test_it_does_not_follow_a_symlink_out_of_the_tree(self):
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "big").write_bytes(b"z" * 10000)
        inner = self.dir / "inner"
        inner.mkdir()
        os.symlink(outside, inner / "link")
        self.assertLess(_mod.directory_size(inner), 10000)


class TestRunBounded(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_normal_command_returns_its_output(self):
        result = _mod.run_bounded(["echo", "hello"], cwd=str(self.dir), env=_env(),
                                  timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "hello")

    def test_a_failing_command_returns_its_code(self):
        result = _mod.run_bounded(["false"], cwd=str(self.dir), env=_env(), timeout=30)
        self.assertNotEqual(result.returncode, 0)

    def test_a_slow_command_raises_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            _mod.run_bounded(["sleep", "30"], cwd=str(self.dir), env=_env(), timeout=1)

    def test_a_timeout_kills_the_children_too(self):
        # The failure that this module prevents: a killed clone must not leave its
        # submodule clones running and downloading into a directory that the size bound
        # has just found too large.
        marker = self.dir / "child.pid"
        script = (f"sh -c 'echo $$ > {marker}; sleep 30' & sleep 30")
        with _time_bound_from(lambda: _pid_in(marker)), \
                self.assertRaises(subprocess.TimeoutExpired):
            _mod.run_bounded(["sh", "-c", script], cwd=str(self.dir), env=_env(),
                             timeout=2)
        _assert_gone(self, _pid_in(marker))

    def test_a_timeout_reaches_what_a_leader_that_exited_left_behind(self):
        """A leader that has exited keeps its pid until it is reaped. The group is therefore killed
        before `poll()` reaps it, and a child that the leader left behind cannot keep the call
        waiting on the pipes past one reaping period after its time bound."""
        marker = self.dir / "child.pid"
        with _time_bound_from(lambda: _pid_in(marker)) as started, \
                self.assertRaises(subprocess.TimeoutExpired):
            _mod.run_bounded(["sh", "-c", _LEADER_EXITS.format(marker=marker)],
                             cwd=str(self.dir), env=_env(), timeout=1)
        elapsed = time.monotonic() - started[0]
        _assert_gone(self, _pid_in(marker))
        self.assertLess(elapsed, 1 + _mod.REAP_SECONDS, "the call waited on the pipes")

    # dd's block size is spelled in bytes here, not as `1m`. The `1m` suffix is
    # BSD syntax and GNU dd rejects it. On Linux, where CI runs, every write would
    # then fail and the directory would never grow, and the call would wait for its
    # 60-second timeout instead of exceeding the size bound.
    def test_a_command_over_the_size_bound_is_stopped(self):
        target = self.dir / "grow"
        target.mkdir()
        leader = self.dir / "leader.pid"
        self.addCleanup(_kill_recorded, leader)
        script = (f"echo $$ > {leader}; "
                  f"i=0; while :; do dd if=/dev/zero of={target}/f$i bs=1048576 count=2 "
                  f"2>/dev/null; i=$((i+1)); done")
        with self.assertRaises(_mod.GitSizeBoundExceeded) as ctx:
            _mod.run_bounded(["sh", "-c", script], cwd=str(self.dir), env=_env(),
                             timeout=60, bounded_path=str(target),
                             max_bytes=4 * 1024 * 1024, sample_seconds=0.2)
        self.assertGreater(ctx.exception.size_bytes, 4 * 1024 * 1024)

    def test_a_size_bound_breach_stops_the_children_too(self):
        target = self.dir / "grow"
        target.mkdir()
        marker = self.dir / "child.pid"
        leader = self.dir / "leader.pid"
        self.addCleanup(_kill_recorded, leader, marker)
        # The leader writes into the tree only once the child has written its pid, so that the
        # breach stops a child that is running however late the machine starts it. The child
        # writes the pid under another name and renames the file, so that the leader never
        # finds part of the pid.
        script = (f"echo $$ > {leader}; "
                  f"sh -c 'echo $$ > {marker}.tmp && mv {marker}.tmp {marker}; "
                  f"while :; do sleep 1; done' & "
                  f"while [ ! -s {marker} ]; do sleep 0.01; done; "
                  f"i=0; while :; do dd if=/dev/zero of={target}/f$i bs=1048576 count=2 "
                  f"2>/dev/null; i=$((i+1)); done")
        with self.assertRaises(_mod.GitSizeBoundExceeded):
            _mod.run_bounded(["sh", "-c", script], cwd=str(self.dir), env=_env(),
                             timeout=60, bounded_path=str(target),
                             max_bytes=4 * 1024 * 1024, sample_seconds=0.2)
        _assert_gone(self, _pid_in(marker))

    def test_a_command_within_the_size_bound_is_left_alone(self):
        """The command outlasts several measurements, and the size is compared. A
        watcher that treated every measurement as a breach would fail here."""
        target = self.dir / "small"
        target.mkdir()
        with unittest.mock.patch.object(_mod, "directory_size",
                                        wraps=_mod.directory_size) as sampled:
            result = _mod.run_bounded(
                ["sh", "-c", f"printf x > {target}/f; sleep 1; echo done"], cwd=str(self.dir),
                env=_env(), timeout=30, bounded_path=str(target),
                max_bytes=1024 * 1024, sample_seconds=0.05)
        self.assertEqual(result.stdout.strip(), "done")
        self.assertGreater(sampled.call_count, 2, "the watcher never sampled")

    def test_output_that_is_not_utf8_is_replaced_rather_than_raised(self):
        # A commit body is arbitrary bytes, and the commit messages are read through
        # run_bounded. Strict decoding would raise UnicodeDecodeError inside communicate(),
        # which no caller counts as a failure, and the row would then be recorded as measured
        # with blanks.
        result = _mod.run_bounded(["printf", "caf\\351 \\377 done"], cwd=str(self.dir),
                                  env=_env(), timeout=30)
        self.assertEqual(result.returncode, 0)
        self.assertIsInstance(result.stdout, str)
        self.assertIn("caf\ufffd", result.stdout)
        self.assertTrue(result.stdout.endswith("done"))

    def test_stdout_can_go_to_a_file_instead_of_memory(self):
        target = self.dir / "out.bin"
        with open(target, "wb") as f:
            result = _mod.run_bounded(["printf", "caf\\351 read"], cwd=str(self.dir),
                                      env=_env(), timeout=30, stdout_file=f)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.stdout)
        self.assertEqual(target.read_bytes(), b"caf\xe9 read")

    def test_no_bounded_path_means_no_watcher(self):
        """No measurement happens during a command that outlasts several intervals, and a
        watcher started anyway would be detected."""
        with unittest.mock.patch.object(_mod, "directory_size",
                                        wraps=_mod.directory_size) as sampled:
            result = _mod.run_bounded(["sh", "-c", "sleep 0.5; echo ok"], cwd=str(self.dir),
                                      env=_env(), timeout=30, bounded_path=None,
                                      sample_seconds=0.05)
        self.assertEqual(result.stdout.strip(), "ok")
        sampled.assert_not_called()

    def test_a_size_bound_of_zero_means_no_watcher(self):
        """A `max_bytes` of 0 turns the bound off. A watcher started anyway would treat any
        tree as a breach, because every size is over zero. The command outlasts several
        intervals, so that such a watcher would measure the tree."""
        target = self.dir / "tree"
        target.mkdir()
        with unittest.mock.patch.object(_mod, "directory_size",
                                        wraps=_mod.directory_size) as sampled:
            result = _mod.run_bounded(
                ["sh", "-c", f"printf x > {target}/f; sleep 1; echo done"], cwd=str(self.dir),
                env=_env(), timeout=30, bounded_path=str(target), max_bytes=0,
                sample_seconds=0.05)
        self.assertEqual(result.stdout.strip(), "done")
        sampled.assert_not_called()


class TestNoGitMaintenance(unittest.TestCase):
    """No process that a git command starts outlives the command.

    From version 2.47, git ends every fetch with `maintenance run --auto --detach`, and the
    detached child outlives the command. Every git command through run_bounded runs with
    maintenance and auto-gc off.
    """

    # git's bound on a transfer that stalls, which every git command runs with.
    NETWORK = ["-c", "http.version=HTTP/1.1", "-c", "http.lowSpeedLimit=1",
               "-c", "http.lowSpeedTime=600"]

    def test_the_options_go_after_git_and_before_the_subcommand(self):
        self.assertEqual(_mod.git_command(["git", "fetch", "--unshallow"]),
                         ["git", *_mod.GIT_NO_MAINTENANCE, *self.NETWORK, "fetch", "--unshallow"])
        self.assertEqual(_mod.git_command(["/usr/bin/git", "log"]),
                         ["/usr/bin/git", *_mod.GIT_NO_MAINTENANCE, *self.NETWORK, "log"])
        self.assertEqual(_mod.git_command(["echo", "git"]), ["echo", "git"])
        self.assertEqual(_mod.git_command([]), [])

    def test_the_three_switches_are_named(self):
        self.assertEqual(_mod.GIT_NO_MAINTENANCE,
                         ("-c", "maintenance.auto=false", "-c", "gc.auto=0",
                          "-c", "gc.autoDetach=false"))

    def _fixture(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        source = tmp / "source"
        source.mkdir()
        run = lambda *a, cwd: subprocess.run(["git", *a], cwd=str(cwd), check=True,  # noqa: E731
                                              capture_output=True)
        run("init", "-q", "-b", "main", cwd=source)
        run("config", "user.email", "t@example.com", cwd=source)
        run("config", "user.name", "t", cwd=source)
        (source / "a.txt").write_text("one\n")
        run("add", ".", cwd=source); run("commit", "-q", "-m", "one", cwd=source)
        clone = tmp / "clone"
        run("clone", "-q", str(source), str(clone), cwd=tmp)
        (source / "b.txt").write_text("two\n")
        run("add", ".", cwd=source); run("commit", "-q", "-m", "two", cwd=source)
        return tmp, clone

    def _trace_of(self, tmp: Path, name: str) -> dict:
        trace = tmp / name
        env = os.environ.copy()
        env["GIT_TRACE"] = str(trace)
        return env, trace

    def test_a_fetch_through_run_bounded_starts_no_maintenance(self):
        tmp, clone = self._fixture()
        env, trace = self._trace_of(tmp, "bounded.log")
        result = _mod.run_bounded(["git", "fetch", "-q"], cwd=str(clone), env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("maintenance run", trace.read_text())

    def test_a_lazy_fetch_inside_git_log_starts_none_either(self):
        # A blobless clone fetches the blobs that `git log --follow` needs as it runs, one
        # fetch per path, and each of those fetches would also start the detached child.
        tmp, clone = self._fixture()
        source = tmp / "source"
        run = lambda *a, cwd: subprocess.run(["git", *a], cwd=str(cwd), check=True,  # noqa: E731
                                              capture_output=True)
        run("config", "uploadpack.allowFilter", "true", cwd=source)
        (source / "a.txt").write_text("one\ntwo\nthree\nfour\nfive\n")
        run("add", ".", cwd=source); run("commit", "-q", "-m", "three", cwd=source)
        run("mv", "a.txt", "renamed.txt", cwd=source)
        (source / "renamed.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n")
        run("add", ".", cwd=source); run("commit", "-q", "-m", "renamed", cwd=source)
        blobless = tmp / "blobless"
        run("clone", "-q", "--filter=blob:none", f"file://{source}", str(blobless), cwd=tmp)
        env, trace = self._trace_of(tmp, "lazy.log")
        # `--follow` compares contents across a rename that also changed the file, and a
        # blobless clone does not have the file's earlier contents. This is the fetch
        # inside the history query.
        result = _mod.run_bounded(["git", "log", "--reverse", "--follow", "--format=%H",
                                   "--", "renamed.txt"], cwd=str(blobless), env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("fetch origin", trace.read_text(), "the log did fetch lazily")
        self.assertNotIn("maintenance run", trace.read_text())
        # The control: the same query with plain git does start maintenance, where this git
        # version does.
        plain = tmp / "plain"
        run("clone", "-q", "--filter=blob:none", f"file://{source}", str(plain), cwd=tmp)
        env, trace = self._trace_of(tmp, "lazy-plain.log")
        subprocess.run(["git", "log", "--reverse", "--follow", "--format=%H", "--",
                        "renamed.txt"], cwd=str(plain), env=env, check=True, capture_output=True)
        if "maintenance run" not in trace.read_text():
            self.skipTest("this git does not run auto-maintenance after a lazy fetch")

    def test_the_same_fetch_without_the_options_does_start_it(self):
        # The control, which makes the test above meaningful: a plain fetch under this git
        # version starts the detached maintenance child. Skipped where this git version does
        # not.
        tmp, clone = self._fixture()
        env, trace = self._trace_of(tmp, "plain.log")
        subprocess.run(["git", "fetch", "-q"], cwd=str(clone), env=env, check=True,
                       capture_output=True)
        if "maintenance run" not in trace.read_text():
            self.skipTest("this git does not run auto-maintenance after a fetch")
        self.assertIn("--detach", trace.read_text())


class TestDieWithParent(unittest.TestCase):
    """A git process that the worker starts ends with the worker, whatever ends the worker.

    Otherwise a clone started by a worker that the kernel kills goes on writing into the
    scratch directory until the container ends. util-linux's `setpriv` sets the
    parent-death signal between fork and exec, where Python's own hook is not safe in a
    process with threads.
    """

    def setUp(self):
        self.saved = _mod._die_with_parent_available
        self.addCleanup(setattr, _mod, "_die_with_parent_available", self.saved)

    def test_the_wrapper_names_the_kill_signal(self):
        self.assertEqual(_mod.DIE_WITH_PARENT, ("setpriv", "--pdeathsig", "KILL", "--"))

    def test_the_command_is_wrapped_where_the_wrapper_works(self):
        _mod._die_with_parent_available = True
        self.assertEqual(_mod.bound_to_parent(["git", "fetch"]),
                         [*_mod.DIE_WITH_PARENT, "git", "fetch"])
        _mod._die_with_parent_available = False
        self.assertEqual(_mod.bound_to_parent(["git", "fetch"]), ["git", "fetch"])

    def test_availability_is_checked_once_and_is_false_off_linux(self):
        _mod._die_with_parent_available = None
        with unittest.mock.patch.object(_mod.subprocess, "run",
                                        wraps=_mod.subprocess.run) as run:
            first = _mod.die_with_parent_available()
            second = _mod.die_with_parent_available()
        self.assertEqual(first, second)
        self.assertLessEqual(run.call_count, 1)
        if _mod.sys.platform != "linux":
            self.assertFalse(first)
            self.assertEqual(run.call_count, 0)

    @unittest.skipUnless(_mod.sys.platform == "linux", "the parent-death signal is Linux's")
    def test_on_linux_a_child_through_run_bounded_dies_with_its_parent(self):
        _mod._die_with_parent_available = None
        if not _mod.die_with_parent_available():
            self.skipTest("no setpriv with --pdeathsig on this machine")
        # A parent that starts a sleeper through run_bounded from a thread, as a worker does,
        # and is then killed. The sleeper writes its pid first.
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        script = tmp / "parent.py"
        script.write_text(
            "import os, sys, threading, time\n"
            "from agentcontext.git import bounds\n"
            "def go():\n"
            "    bounds.run_bounded(['sh', '-c', 'echo $$ > pid; sleep 60'],\n"
            f"                             cwd={str(tmp)!r}, env=dict(os.environ), timeout=120,\n"
            "                             max_bytes=0)\n"
            "threading.Thread(target=go, daemon=True).start()\n"
            "time.sleep(60)\n")
        parent = subprocess.Popen([sys.executable, str(script)], cwd=str(tmp))
        deadline = time.monotonic() + 20
        while not (tmp / "pid").exists() or not (tmp / "pid").read_text().strip():
            self.assertLess(time.monotonic(), deadline, "the sleeper did not start")
            time.sleep(0.05)
        child = int((tmp / "pid").read_text())
        parent.kill()
        parent.wait()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{child}/stat").read_text().split(")")[-1].split()[0]
            except OSError:
                # Gone before the read, ENOENT, or during it, ESRCH.
                break
            if state == "Z":
                break
            time.sleep(0.05)
        else:
            self.fail("the sleeper outlived its parent")


class TestStopRunning(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_it_can_be_called_from_the_thread_that_acquired_the_lock(self):
        """A signal handler runs on a thread that may already be inside `run_bounded`'s two set
        operations, and `threading.Lock` is not reentrant. Taking the lock here would therefore
        deadlock the thread with itself, which in the serial branch of the collection is the only
        thread. The other tests call it from a thread that does not hold the lock, which never
        deadlocks."""
        import threading
        with _mod._RUNNING_LOCK:
            finished = threading.Event()

            def call():
                _mod.stop_running()
                finished.set()

            caller = threading.Thread(target=call)
            caller.start()
            caller.join(timeout=5)
        self.assertTrue(finished.is_set(),
                        "stop_running blocked on the lock of _RUNNING")

    def test_a_reaped_process_is_left_alone(self):
        """`os.killpg` on a reaped pid would signal whatever process now has that pid.
        `Popen.send_signal` returns early for the same reason, and so does `_terminate_group`.
        The calls are asserted rather than only run, because without the check nothing raises
        either: `killpg` on a group that is gone raises `ProcessLookupError`, and the `except`
        catches it."""
        import subprocess
        from unittest import mock
        done = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
        done.wait()
        with mock.patch.object(_mod.os, "getpgid") as getpgid, \
                mock.patch.object(_mod.os, "killpg") as killpg:
            _mod._terminate_group(done)
        getpgid.assert_not_called()
        killpg.assert_not_called()

    def test_it_reaches_what_a_leader_that_exited_left_behind(self):
        """A leader that has exited and is not yet reaped keeps its pid, and its group can
        be killed. Reaping it with `poll()` and returning early would leave its child with the pipes
        of a stopped command open."""
        import threading
        marker = self.dir / "child.pid"
        results = []
        worker = threading.Thread(daemon=True, target=lambda: results.append(
            _mod.run_bounded(["sh", "-c", _LEADER_EXITS.format(marker=marker)],
                             cwd=str(self.dir), env=_env(), timeout=30)))
        worker.start()
        child, leader = _recorded_command(marker)
        _wait_until(lambda: _exited(leader.pid), "the leader did not exit")
        self.assertEqual(_mod.stop_running(), 1)
        worker.join(timeout=10)
        _assert_gone(self, child)
        self.assertFalse(worker.is_alive(), "run_bounded did not return after the stop")

    def test_it_kills_every_command_in_flight_by_group(self):
        import threading
        marker = self.dir / "child.pid"
        script = f"sh -c 'echo $$ > {marker}; sleep 30' & sleep 30"
        results = []
        worker = threading.Thread(
            target=lambda: results.append(_mod.run_bounded(["sh", "-c", script],
                                                           cwd=str(self.dir), env=_env(),
                                                           timeout=60)))
        worker.start()
        child, _leader = _recorded_command(marker)
        self.assertEqual(_mod.stop_running(), 1)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "run_bounded did not return after the stop")
        self.assertNotEqual(results[0].returncode, 0)
        _assert_gone(self, child)

    def test_nothing_in_flight_stops_nothing(self):
        self.assertEqual(_mod.stop_running(), 0)

    def test_a_finished_command_is_forgotten(self):
        _mod.run_bounded(["true"], cwd=str(self.dir), env=_env(), timeout=30)
        self.assertEqual(_mod.stop_running(), 0)


class TestSubmodulePaths(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, text):
        (self.dir / ".gitmodules").write_text(text)

    def test_no_gitmodules_is_no_submodules(self):
        self.assertEqual(_mod.submodule_paths(self.dir), [])

    def test_it_reads_every_declared_path(self):
        self.write('[submodule "a"]\n\tpath = vendor/a\n\turl = https://x/a\n'
                   '[submodule "b"]\n\tpath = third_party/b\n\turl = https://x/b\n')
        self.assertEqual(_mod.submodule_paths(self.dir), ["vendor/a", "third_party/b"])

    def test_a_trailing_slash_is_dropped(self):
        self.write('[submodule "a"]\n\tpath = vendor/a/\n')
        self.assertEqual(_mod.submodule_paths(self.dir), ["vendor/a"])


class TestWantedSubmodules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        (self.dir / ".gitmodules").write_text(
            '[submodule "a"]\n\tpath = vendor/a\n'
            '[submodule "b"]\n\tpath = third_party/b\n')

    def test_a_path_inside_a_submodule_selects_it(self):
        self.assertEqual(_mod.wanted_submodules(self.dir, ["vendor/a/CLAUDE.md"]),
                         ["vendor/a"])

    def test_a_path_outside_every_submodule_selects_none(self):
        # The ordinary case: no submodule is fetched.
        self.assertEqual(_mod.wanted_submodules(self.dir, ["CLAUDE.md",
                                                           ".claude/settings.json"]), [])

    def test_the_submodule_itself_being_the_path_selects_it(self):
        self.assertEqual(_mod.wanted_submodules(self.dir, ["vendor/a"]), ["vendor/a"])

    def test_a_prefix_that_is_not_a_path_boundary_does_not_match(self):
        self.assertEqual(_mod.wanted_submodules(self.dir, ["vendor/abc/CLAUDE.md"]), [])

    def test_several_paths_select_several_submodules(self):
        self.assertEqual(
            sorted(_mod.wanted_submodules(self.dir, ["vendor/a/x", "third_party/b/y"])),
            ["third_party/b", "vendor/a"])

    def test_a_repository_with_no_submodules_selects_none(self):
        (self.dir / ".gitmodules").unlink()
        self.assertEqual(_mod.wanted_submodules(self.dir, ["vendor/a/CLAUDE.md"]), [])


class TestTheGitLayersUseTheBounds(unittest.TestCase):
    """The git client, through which both the cloning step and the file history step clone. Each
    test clones a repository built with `git init` through a redirect of its GitHub URL."""

    URL = "https://github.com/owner/repo.git"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        self.remote = self.tmp / "remote"
        self.remote.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", *args], cwd=self.remote, check=True, capture_output=True)
        # An ambient GITHUB_TOKEN puts a placeholder user into the URL, past the redirect.
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)

    def _redirect(self, config: dict) -> None:
        for key, value in config.items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)

    def test_every_clone_runs_under_a_size_bound_on_its_destination(self):
        """The test reads the calls rather than the source, which would show one
        `bounded_path=` per module even if two of the git client's three clone commands had no
        size bound. The first clone gets the response of a server that refuses the filter, and
        the recorded branch is gone. The git client's clone therefore runs all three commands."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n")
        head_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.remote,
                                  check=True, capture_output=True, text=True).stdout.strip()
        self._redirect({"GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": f"url.file://{self.remote}.insteadOf",
                        "GIT_CONFIG_VALUE_0": self.URL})
        real, refused = _mod.run_bounded, []

        def filter_refused_once(cmd, **kwargs):
            if "--filter=blob:none" in cmd and "--branch" in cmd and not refused:
                refused.append(cmd)
                return subprocess.CompletedProcess(
                    cmd, 128, "", "fatal: server does not support filter\n")
            return real(cmd, **kwargs)

        with unittest.mock.patch.object(_mod, "run_bounded",
                                        side_effect=filter_refused_once) as bounded, \
                contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            _, error = _history.measure_repo("owner/repo", head_sha, "", "gone", ["CLAUDE.md"], None,
                                             sleep=lambda s: None, scratch_dir=self.tmp / "s")
            dest = self.tmp / "collected" / "owner§repo"
            status = _clone.clone_with_retry("owner/repo", dest, self.tmp / "log", max_tries=1,
                                             base_delay=0, max_clone_bytes=5 * 1024 ** 3)
        self.assertEqual((error, status), ("", "ok"))
        clones = [c for c in bounded.call_args_list if "clone" in c.args[0]]
        self.assertEqual(len(clones), 4, "three by the git client, one by the cloning step")
        for call in clones:
            with self.subTest(cmd=" ".join(call.args[0])):
                self.assertEqual(call.kwargs.get("bounded_path"), call.args[0][-1])
        self.assertEqual({c.kwargs.get("max_bytes") for c in clones},
                         {_mod.DEFAULT_MAX_BYTES, 5 * 1024 ** 3})

    def test_a_clone_the_size_bound_stopped_is_counted_and_keeps_the_rows(self):
        """A breach raised through the git client's own command increments the counter, and
        `_clone_outcome` keeps the rows without measuring the repository again, which would meet
        the same bound (`bound_exceeded`)."""
        _add_noise(self.remote, "big.bin", 3 * 1024 * 1024)
        self._redirect(ext_redirect(upload_pack(self.tmp, pause=0.05), self.remote, self.URL))
        extractor = _history.GitFileMetadata(max_clone_bytes=1024 * 1024)
        temp_dir = self.tmp / "clone"
        temp_dir.mkdir()
        with sampled_every(0.05), contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            cloned = _history._clone_with_retries(extractor, "owner/repo", "main",
                                                  str(temp_dir), retries=1,
                                                  sleep=lambda s: None)
        self.assertFalse(cloned)
        self.assertEqual((extractor.size_bound_exceeded, extractor.transient_failures), (1, 0))
        self.assertEqual(_history._clone_outcome(extractor), "bound_exceeded")


if __name__ == "__main__":
    unittest.main()
