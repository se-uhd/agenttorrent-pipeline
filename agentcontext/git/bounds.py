#!/usr/bin/env python3
"""Bound the time and size of one git command, and stop every process that it started.

The cloning step and the file history step clone a repository to read the history of the
configuration artifacts in it, and without bounds that clone can take unlimited time and disk
space, for reasons that are not specific to one repository:

The size of a clone. `--filter=blob:none` leaves the historical file contents on the server,
which saves most of the transfer, but a repository with a very large commit graph still
transfers gigabytes. A time bound does not limit that transfer either, because the timeout has
to be long enough for an ordinary repository on a slow uplink.

The submodules. `git submodule update --init --recursive` follows every submodule of every
submodule, from servers unrelated to the repository being measured. `wanted_submodules` below
limits that part: a submodule is initialized only when it could contain one of the paths being
looked up.

Stopping the command has to stop every process that it started. `subprocess` kills the process
that it started, but git delegates to child processes that do not receive the signal. Killing a
clone alone would therefore leave its submodule clones running and downloading into the
directory that the size bound has just found too large. Every command here runs in its own
process group and is stopped by group.

A repository that exceeds the size bound is reported, not deleted. The callers treat a network
failure the same way, because the pipeline could not measure the repository, which differs from
finding nothing there.

Inputs and outputs: none. This module is a library for the steps that clone repositories.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

# The size bound and the time bound stop different failures. The time bound stops a clone that
# is large and grows at an ordinary rate, and the size bound stops a clone that grows faster than
# any single repository does, as recursive submodule cloning does.
#
# GROUNDING, from output/repo-sizes.csv (every repository, from GitHub) and
# docs/history/clone-probe.csv (blobless clones across that range of sizes): the size of a clone
# varies too widely against the size that GitHub reports to be predicted from it. The largest
# repositories in the sample, cloned with the collector's commands (the depth-1 blobless clone
# and the history fetch) and measured with `du` after both, fit under this bound with room to
# grow, and the bound still stops a clone that grows without limit. `GIT_TIMEOUT_SECONDS`
# remains the bound that stops a clone that does not finish.
DEFAULT_MAX_BYTES = 32 * 1024 ** 3

# How often the watcher measures the directory against the size bound. Each measurement lists
# every file of the tree, and the interval has to be long compared with the listing
# and short compared with how fast the tree can grow.
#
# GROUNDING: a clone that grows without limit does so while it downloads, and during the
# download git keeps everything in pack files. The listing is cheap exactly when the
# size bound matters, and even a large checked-out tree lists in a fraction of the interval. What a
# clone downloads in one interval is small against a size bound in gigabytes.
SAMPLE_SECONDS = 15.0

# How long to wait for a killed process group to exit and close the pipes that it inherited.
# GROUNDING: a policy. A group sent SIGKILL exits at once, and this is the margin that
# `repository_cloning.WORKER_STOP_SECONDS` gives the workers for the same reason. After it the
# wait ends.
REAP_SECONDS = 5.0
# The time bound for a git or setpriv command that reads a local file or nothing at all and returns
# at once: the parent-death probe and the `.gitmodules` reader. GROUNDING: a policy, the time that
# the runner's pre-checks give one API request (`runner.GITHUB_TIMEOUT_SECONDS`, which a
# test keeps equal to this value). A local command that has not returned by then indicates a machine
# that cannot run the step, not a slow command.
LOCAL_COMMAND_SECONDS = 30
# How many characters of git's stderr a caller keeps when it turns a failure into an exception
# or a record. GROUNDING: git's own line, `fatal:` or `error:`, states the reason. The tail has
# room for two such lines and leaves out a hook's or a server's output above them.
# `tests/test_guards.py` checks the tail against the longest such line that a deployment logged.
STDERR_TAIL = 200


# Every command that this process has started through run_bounded and not yet reaped, so that
# a process that receives a stop signal can kill what it started rather than leave a clone
# downloading.
_RUNNING = set()
_RUNNING_LOCK = threading.Lock()


def stop_running() -> int:
    """Kill the process group of every running command of this process. Returns the number.

    Meant for a signal handler. A pool worker that its parent stops kills the git that it
    started before it exits, so that no clone goes on writing into a directory that the next run
    will use.

    The function takes no lock. A signal handler runs on a thread that may already hold the
    lock, inside one of `run_bounded`'s two set operations, and `threading.Lock` is not
    reentrant. Taking the lock here would therefore deadlock the thread with itself, which in
    the serial branch of the collection is the only thread. The set can also change while it is
    copied, which raises rather than blocks, and the function retries the copy. `run_bounded`
    itself reaps a command started between the copy and the kill.
    """
    running = []
    for _attempt in range(3):
        try:
            running = list(_RUNNING)
            break
        except RuntimeError:  # the set changed size during iteration
            continue
    for process in running:
        _terminate_group(process)
    return len(running)


class GitSizeBoundExceeded(Exception):
    """A git command was stopped because its working tree grew past the size bound."""

    def __init__(self, path, size_bytes: int, limit_bytes: int):
        self.path = str(path)
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"{self.path} reached {size_bytes / 1024 ** 3:.1f} GiB, over the "
            f"{limit_bytes / 1024 ** 3:.1f} GiB size bound")


def directory_size(path) -> int:
    """The bytes used under `path`, without following symlinks and without raising."""
    total = 0
    stack = [str(path)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _terminate_group(process) -> None:
    """Stop the command and every process that it started.

    The kill goes to the group by its id, which under `start_new_session` is the leader's pid,
    whether the leader is running or has exited. A leader that has exited and has not been
    reaped keeps its pid. The number cannot belong to another process yet, and the kill is
    meant for the processes that the leader left in the group. The group is therefore killed
    before `poll()` could reap the leader, so that a child that the leader left behind, such as
    the sleep of `sh -c "sleep 30 & exit 0"`, cannot keep the pipes open.

    A leader that this process has reaped is left alone. Its pid may belong to another process
    by then, and once the last member of the group has exited, the group's id may become the id
    of a new session. `killpg` could then signal a group that this process never started.
    `Popen.send_signal` returns early for the same reason. `run_bounded` reaps only inside
    `communicate()`, after the pipes have closed, and this check never skips a member that still
    has the pipes open. A reaped process stays in `_RUNNING` only between that reap and the
    `finally` that removes it.
    """
    if process.returncode is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            process.kill()
        except OSError:
            pass


# From version 2.47, git runs `maintenance run --auto --detach` at the end of every fetch, and
# the detached child outlives the fetch. It finds nothing to do on a clone that this pipeline
# deletes soon after and exits. With its parent gone, it is reparented to process 1, where it
# stays a zombie unless process 1 reaps it, until a long run reaches the process limit. Every
# git command that runs through this module runs with maintenance off, so that no process that
# git starts outlives the command. `gc.auto=0` is the same setting for versions before
# `maintenance.auto`, and `gc.autoDetach=false` keeps a gc that does run in the foreground.
GIT_NO_MAINTENANCE = ("-c", "maintenance.auto=false", "-c", "gc.auto=0",
                      "-c", "gc.autoDetach=false")

# How long a transfer may move less than one byte per second before git ends it. A connection
# that stays open after the network dropped, such as behind a NAT that sends no reset, moves
# nothing, and without this bound a command waits for its time bound. A history fetch that
# waited twice would then be recorded as too large for the time bound, which no later run of the
# collection fetches again, rather than as a failure of the network, which the next run fetches.
# GitHub itself sends nothing while it prepares a large pack, as for the blobs of a checkout and
# for a long history. A bound that ended such a fetch would end it in every attempt and every
# run, so the bound gives the preparation ten minutes without data. The value is well below the
# time bound (`history.GIT_TIMEOUT_SECONDS`).
LOW_SPEED_SECONDS = 600


def git_network_options() -> tuple:
    """The options that every git command runs with for its transfers over HTTP: HTTP/1.1, and
    the end of a transfer that stalls (`LOW_SPEED_SECONDS`)."""
    return ("-c", "http.version=HTTP/1.1", "-c", "http.lowSpeedLimit=1",
            "-c", f"http.lowSpeedTime={LOW_SPEED_SECONDS}")


# `setpriv --pdeathsig KILL -- git ...`: the kernel ends the git process when the process that
# started it ends, whatever ended that process. Without the wrapper, a worker that the kernel
# kills, such as through the OOM killer, leaves its git running. The clone that it started, in
# its own session, goes on writing into the scratch directory until the container ends.
# `PR_SET_PDEATHSIG` is the only mechanism that reaches a child after its parent has exited and
# no process is left to signal it. The util-linux wrapper sets it between fork and exec, as a
# Python `preexec_fn` would. Python documents that hook as unsafe in a process with threads,
# and the size watcher in `run_bounded` starts a thread. The wrapper is used on Linux
# only, and only where it is on the path and accepts the option, which is checked once per
# process.
DIE_WITH_PARENT = ("setpriv", "--pdeathsig", "KILL", "--")
_die_with_parent_available: bool | None = None


def die_with_parent_available() -> bool:
    """Whether this machine can end a child process when its parent ends. Checked once."""
    global _die_with_parent_available
    if _die_with_parent_available is None:
        available = False
        if sys.platform == "linux":
            try:
                available = subprocess.run([*DIE_WITH_PARENT, "true"], capture_output=True,
                                           timeout=LOCAL_COMMAND_SECONDS).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                available = False
        _die_with_parent_available = available
    return _die_with_parent_available


def bound_to_parent(cmd: list) -> list:
    """The command under the parent-death wrapper where the wrapper works, unchanged elsewhere."""
    return [*DIE_WITH_PARENT, *cmd] if die_with_parent_available() else list(cmd)


def git_command(cmd: list) -> list:
    """The command with maintenance turned off and the network options after `git`
    (`git_network_options`), and any other command unchanged."""
    if cmd and os.path.basename(str(cmd[0])) == "git":
        return [cmd[0], *GIT_NO_MAINTENANCE, *git_network_options(), *cmd[1:]]
    return list(cmd)


def run_bounded(cmd: list, cwd: str, env: dict, timeout: float,
                bounded_path=None, max_bytes: int = DEFAULT_MAX_BYTES,
                capture_output: bool = True,
                sample_seconds: float = SAMPLE_SECONDS,
                stdout_file=None, stdin_file=None) -> subprocess.CompletedProcess:
    """Run one git command under a time bound and an optional size bound.

    Raises `subprocess.TimeoutExpired` on the timeout and `GitSizeBoundExceeded` when
    `bounded_path` grows past `max_bytes`. In both cases the whole process group is stopped
    first, so that no child keeps downloading after the call returns. Captured output is
    returned as text, decoded with a replacement character for anything that is not UTF-8, and
    a commit message in another encoding becomes text and never raises an exception.

    `stdout_file` is an open binary file to write the command's stdout into instead of
    capturing it, for reading every commit of a repository, which produces more output than a
    worker should keep in memory. stderr is still captured, and `stdout` in the result is
    None. `stdin_file` is an open file that the command reads as its standard input, such as
    the revisions of `git log --stdin`, which on the command line could exceed the system's
    limit on the size of the arguments.
    """
    cmd = git_command(cmd)
    pipe = subprocess.PIPE if capture_output else None
    out = stdout_file if stdout_file is not None else pipe
    # Bytes, decoded below with errors="replace". A commit body is arbitrary bytes, and the
    # commit messages are read through this function. With text=True the decoding would happen
    # inside communicate() and raise UnicodeDecodeError on the first Latin-1 message, which no
    # caller counts as a failure, and the row would then be recorded as measured with blanks. A
    # replacement character in a message changes no value that the pipeline derives from it.
    process = subprocess.Popen(bound_to_parent(cmd), cwd=cwd, env=env, stdout=out,
                               stderr=pipe, stdin=stdin_file, start_new_session=True)
    with _RUNNING_LOCK:
        _RUNNING.add(process)

    breach = {}
    stop = threading.Event()
    # Held across each measurement, so that the watcher cannot kill the group while the main
    # thread is reaping the process. Without the lock, the watcher could signal a pid that the
    # OS had already reused, and could record a breach for a command that had exited
    # successfully.
    alive = threading.Lock()
    finished = []

    def watch():
        while not stop.wait(sample_seconds):
            size = directory_size(bounded_path)
            if size <= max_bytes:
                continue
            with alive:
                if finished:
                    return
                breach["size"] = size
                _terminate_group(process)
            return

    watcher = None
    if bounded_path is not None and max_bytes:
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()

    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with alive:
            finished.append(True)
        _terminate_group(process)
        # A descendant that called setsid() is outside the group that the kill reached and still has
        # the stdout and stderr that it inherited open, and this call would then never read EOF.
        # run_bounded exists so that a command cannot take unlimited time, and waiting here without
        # a bound would defeat that purpose. GROUNDING: the processes involved are git and its
        # helpers, which exit at once on SIGKILL, and REAP_SECONDS is a wide margin for
        # the group to exit and the pipes to close. Pipes still open after it belong to a process
        # that the kill did not reach.
        try:
            process.communicate(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.communicate(timeout=REAP_SECONDS)
            except subprocess.TimeoutExpired:
                # A descendant outside the group keeps the pipes open. Close this end and stop
                # waiting, so that the caller gets its timeout rather than waiting forever on a
                # process that this function cannot reap.
                for pipe in (process.stdout, process.stderr, process.stdin):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
        raise
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(process)
        with alive:
            finished.append(True)
        stop.set()
        if watcher is not None:
            # GROUNDING: the watcher is either waiting on the stop event, which it observes at
            # once, or inside one listing of the tree (see SAMPLE_SECONDS). The watcher is a
            # daemon thread, and the result does not depend on the join, which can
            # have a short timeout.
            watcher.join(timeout=1.0)

    if "size" in breach:
        # Raised only when the kill took effect. A command that had already finished
        # successfully when the watcher measured the directory is a success, not a breach.
        raise GitSizeBoundExceeded(bounded_path, breach["size"], max_bytes)

    return subprocess.CompletedProcess(cmd, process.returncode, _decoded(stdout),
                                       _decoded(stderr))


def _decoded(data):
    """The text of one captured stream, or None when nothing was captured."""
    return None if data is None else data.decode("utf-8", errors="replace")


def submodule_paths(repo_dir) -> list[str]:
    """Every submodule path declared in .gitmodules, in file order.

    The paths are read from the file rather than from `git submodule status`, because they are
    needed before any submodule exists on disk, and that command would start a process without
    initializing anything.
    """
    gitmodules = Path(repo_dir) / ".gitmodules"
    if not gitmodules.is_file():
        return []

    # git itself parses the file. .gitmodules is a git config file, and hand-written files use
    # forms that a line scan gets wrong: `path=x` with no spaces, a quoted value, a
    # continuation, a `[submodule "x"] path = y` on one line, and a key spelled `Path`. A line
    # scan would skip those submodules silently and lose the configuration artifacts inside
    # them.
    try:
        # -z separates records with NUL and the key from its value with a newline, so that a name or
        # a path that contains a space or a newline is read intact. Splitting a line at its first
        # space would split inside the key whenever the submodule name contains a space, and
        # `git submodule add <url> "my module"` names the submodule after the path.
        result = subprocess.run(
            ["git", "config", "-z", "-f", str(gitmodules), "--get-regexp",
             r"^submodule\..*\.path$"],
            capture_output=True, text=True, timeout=LOCAL_COMMAND_SECONDS)
        # Exit 0 means that keys matched, and exit 1 that no key matched. Both mean that git
        # read the file. Any other exit means that git rejected it.
        if result.returncode in (0, 1):
            paths = []
            for record in (result.stdout or "").split("\0"):
                if not record:
                    continue
                _, _, value = record.partition("\n")
                value = value.strip().rstrip("/")
                if value:
                    paths.append(value)
            return paths
    except Exception:
        pass

    # Every step that calls this function requires git, and the fallback is only for a malformed
    # file that git rejects. It reads the common form and nothing else.
    paths = []
    try:
        text = gitmodules.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("path") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().strip('"').rstrip("/")
            if value:
                paths.append(value)
    return paths


def wanted_submodules(repo_dir, wanted_paths) -> list[str]:
    """The submodules that could contain one of `wanted_paths`, at this level only.

    A configuration artifact inside a submodule has no history in the parent repository, which
    is the only reason that the steps initialize submodules at all. A submodule that contains
    none of the paths being looked up cannot change any measurement and is not
    fetched.

    Only the submodules that the .gitmodules of `repo_dir` declares are returned.
    `GitFileMetadata._init_submodules_recursively` in `git/history.py` descends further for a
    path that lies inside a submodule of a submodule.
    """
    declared = submodule_paths(repo_dir)
    if not declared:
        return []
    def normalize(path) -> str:
        # lstrip() takes a character set, not a prefix: lstrip("./") turns ".claude/x" into
        # "claude/x", and no submodule with a name that begins with a dot would match.
        # Configuration directories such as `.claude` and `.github` begin with a dot.
        text = str(path).replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        return text.lstrip("/")

    normalized = [normalize(p) for p in wanted_paths]
    return [sub for sub in declared
            if any(path == sub or path.startswith(sub + "/") for path in normalized)]


def remaining_inside(submodule: str, wanted_paths) -> list:
    """The wanted paths that lie inside `submodule`, with its prefix removed.

    The function computes one level of the descent. The caller initializes `submodule`, and
    only then can the submodule's own .gitmodules be read. The levels therefore cannot be
    computed in advance: against directories that do not exist yet, they would contain nothing
    below level 0, and no nested submodule would be fetched.
    """
    sub = str(submodule).replace("\\", "/").rstrip("/")
    out = []
    for path in wanted_paths:
        text = str(path).replace("\\", "/")
        while text.startswith("./"):
            text = text[2:]
        if text.startswith(sub + "/"):
            out.append(text[len(sub) + 1:])
    return out
