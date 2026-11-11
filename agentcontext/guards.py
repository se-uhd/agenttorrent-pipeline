"""The stop conditions and the progress line of the cloning step.

The module defines the progress line, the disk minimum, the count of clones that failed in a
row and the probe of the network that follows it, the window of collector errors, the stop
after a worker died, the ceiling on the repositories that a run may leave to retry and still
count as complete, and the stop handler of a worker.
"""

import math
import multiprocessing
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from agentcontext import logs, resume
from agentcontext.git import auth, bounds


# The free space in GiB below which this step starts no further clone. It is the minimum that the
# runner's pre-checks require, `runner.MIN_FREE_DISK_GB`, and a test of the runner checks
# that the two are equal. The pre-checks measure the space once, but the step runs for
# days, and the default five workers can have up to 160 GiB of clones on disk at once under the
# default size bound of 32 GiB. The step therefore measures the space again, in the directory of the
# clones, `--clone-dir`, which in a container is a mounted volume rather than the overlay.
MIN_FREE_GB = 190
# The number of finished repositories between two measurements of the free space. GROUNDING: a
# policy. The measurement is one statvfs call, which costs little next to the clones of fifty
# repositories.
DISK_CHECK_EVERY = 50
# How often the step logs a progress line: after this many finished repositories, or after this
# many seconds without a line, whichever comes first. GROUNDING: a policy. On a slow run, the
# second condition limits the time between two lines to ten minutes. The step logs a line only
# when a repository finishes. A longer time without a line under the default five workers
# therefore means that every worker is still cloning, fetching or reading the commit history
# of one repository, and `history.GIT_TIMEOUT_SECONDS` bounds each of these operations at one
# hour.
PROGRESS_EVERY = 100
PROGRESS_SECONDS = logs.PROGRESS_SECONDS


class Progress(logs.Progress):
    """The cloning step's progress line: `logs.Progress` under the component
    `repository_cloning`, with the counts of the container's processes at the end."""

    def __init__(self, total: int, already_done: int, every: int = PROGRESS_EVERY,
                 seconds: float = PROGRESS_SECONDS, clock=time.monotonic):
        super().__init__('repository_cloning', total, already_done, every, seconds, clock)

    def details(self) -> str:
        counts = process_counts()
        return (f", {counts[0]:,} processes with {counts[1]:,} zombies"
                if counts is not None else "")


class ConsecutiveFailuresStop(Exception):
    """The step stopped because clones failed one after the other, or because the collector
    failed repeatedly.

    Like `LowDiskStop`, the exception contains the rows assembled so far. It also contains the
    count that stopped the step, taken at the stop rather than after the clones in flight
    finished, and the outcome that it counted: `clone_failed` for a failure of the network, or
    `collector_error` for a defect.
    """

    def __init__(self, streak: int, finished: int, remaining: int, rows: tuple,
                 kind: str = "clone_failed"):
        what = ("clones failed in a row" if kind == "clone_failed"
                else f"repositories ended in {resume.COLLECTOR_ERROR} among the last "
                     f"{ERROR_WINDOW:,}")
        super().__init__(f"{streak:,} {what} after {finished:,} repositories finished, with "
                         f"{remaining:,} not started")
        self.streak, self.finished, self.remaining, self.rows = streak, finished, remaining, rows
        self.kind = kind


class WorkerLostStop(Exception):
    """The step stopped because a worker process died, for example because the kernel killed it
    for lack of memory.

    Like `LowDiskStop`, the exception contains the rows assembled so far. The step records the
    repositories that the workers had in flight as clone_failed, and a later run retries them.
    """

    def __init__(self, finished: int, remaining: int, rows: tuple):
        super().__init__(f"A worker process died after {finished:,} repositories finished, "
                         f"with {remaining:,} not started")
        self.finished, self.remaining, self.rows = finished, remaining, rows
def process_counts() -> "tuple | None":
    """How many processes are in the container, and how many of them are zombies.

    The function reads the counts from /proc and returns None where /proc does not exist. A
    count that grows by one per repository shows a leak, which would otherwise become visible
    only when `fork` fails.
    """
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return None
    zombies = 0
    for pid in pids:
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith("State:"):
                        zombies += line.split()[1] == "Z"
                        break
        except OSError:
            continue
    return len(pids), zombies


class LowDiskStop(Exception):
    """The collection stopped starting clones because the scratch volume ran low on free space.

    The step raises it only after it has recorded every clone in flight. The list of finished
    repositories then contains each repository that this run completed, and a resumed run
    starts where this run stopped. `rows` is what `run_collection` would have returned for the
    repositories that it reached.
    """

    def __init__(self, path, free_bytes: int, minimum_free_bytes: int, done: int, remaining: int,
                 rows: tuple):
        self.path = Path(path)
        self.free_bytes = free_bytes
        self.minimum_free_bytes = minimum_free_bytes
        self.done = done
        self.remaining = remaining
        self.rows = rows
        super().__init__(
            f"{self.path} has {free_bytes / 1024 ** 3:.1f} GiB free, under the "
            f"{minimum_free_bytes / 1024 ** 3:.1f} GiB minimum. Stopped after {done:,} "
            f"repositories, with {remaining:,} not started")


def free_bytes(path) -> int:
    """Free bytes on the volume that contains `path`."""
    return shutil.disk_usage(path).free
# How many failures stop the step, and over how many finished repositories. GROUNDING: a
# healthy collection has no two failed clones in a row in input order. Twenty in a row point to
# the network or the machine, and every further attempt would add a row to clone_failed.csv that
# records nothing about its repository. In an input sorted by owner, an owner that GitHub closed
# with a message that the permanent-failure patterns do not match would fail every clone of its
# repositories in a row. The step therefore takes the repositories in the order of a hash of
# their names, which spreads one owner's repositories over the run. It still probes `PROBE_URL`
# first, and it stops only when the probe fails too.
# A healthy run has no collector error, and twenty collector errors among the last
# `ERROR_WINDOW` finished repositories point to a defect of this step. No probe can rule out a
# defect, and the errors stop the step without a probe. A count over a window, unlike a count of
# errors in a row, detects a defect that affects only the repositories with copied files: such a
# defect stops the step within one window while those repositories are more than a tenth of the
# finished repositories.
MAX_CONSECUTIVE_FAILURES = 20
ERROR_WINDOW = 200
# What the probe after failed clones lists: one ref of a repository that is public, small and
# unlikely to be deleted, listed with `git ls-remote` under the environment that the clones run
# with. GROUNDING for the timeout: the request returns within seconds on a working network. Because
# the probe runs while the clones in flight share the uplink, it gets twice the thirty seconds that
# the runner's pre-checks give one API request (`runner.GITHUB_TIMEOUT_SECONDS`). A network
# on which the probe gets no response within a minute cannot serve the clones either.
PROBE_URL = "https://github.com/git/git.git"
PROBE_TIMEOUT = 60
# The waits in seconds after a failed probe. After each wait, the cloning step probes `PROBE_URL`
# again, and it starts no clone during the waits. The step stops only when every probe fails. A
# stop after the first failed probe would end an unattended run on an outage of the network or
# of GitHub that passes within minutes, and the run would then wait for a Start by hand, while
# the waits cost only their own time. GROUNDING: the policy of `runner.GITHUB_REJECTED_WAITS`,
# `RECHECK_WAITS` of ../2_classification/readmes.py and `ROUND_WAITS` of
# ../1_sampling/2_query_github.py, which wait between minutes and hours for a series of
# failures to end.
PROBE_WAITS = (5 * 60, 15 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60)


def remote_responds(url: str, token: str | None, use_ssh: bool,
                   timeout: float = PROBE_TIMEOUT) -> bool:
    """Whether one ref of `url` can be listed, under the environment that the clones run with."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    auth.apply(env, None if use_ssh else (token or None))
    if use_ssh and url.startswith("https://github.com/"):
        # The probe uses the transport of the clones. A probe over https while the clones go
        # over ssh would reset the count on every probe when the SSH key does not work, and the
        # step would record clone_failed for the whole input list, the outcome that the count of
        # failed clones in a row exists to prevent. The code rewrites every https GitHub URL, not
        # only the default, because `--probe-url` can name another URL together with
        # `--use-ssh`.
        url = "git@github.com:" + url[len("https://github.com/"):]
    try:
        proc = bounds.run_bounded(["git", "ls-remote", "--exit-code", url, "HEAD"],
                                       cwd=os.getcwd(), env=env, timeout=timeout,
                                       max_bytes=0)
    except Exception:
        return False
    return proc.returncode == 0

# How many times the cloning step collects again, before it ends, the repositories that a later
# run would redo, and how long it waits before each round. GROUNDING: GitHub returns HTTP 400 to
# fetches in bursts that end within minutes (docs/steps/repository_cloning.md gives the
# duration), and the later fetches of the run succeed. A pause of five minutes is longer than
# such a burst, and a second round retries what failed during a burst in the first round. A
# repository that still fails after the second round fails for a reason of its own rather than
# because of that day's network, and the ceiling below counts it.
RETRY_ROUNDS = 2
RETRY_PAUSE_SECONDS = 300.0

# How many repositories a run may leave for a later run to retry (clone_failed, collector_error
# and ok lines marked to redo), as a share of the repositories that it read. GROUNDING:
# CLONE_FAILURE_FACTOR times the share of clone_failed rows in the March collection (the two
# operands after it). A healthy run ends under it and is recorded as complete. Above it,
# someone has to check the failures before the sample is published: the step exits 4, the
# runner records the step as not complete, and a Start retries the repositories once the cause
# is fixed.
# `runner.CLONE_FAILURE_SHARE` is the same share, and a test checks that the two are equal.
# `--mark-complete` refuses to record the step as complete above it.
CLONE_FAILURE_FACTOR = 3
CLONE_FAILURE_SHARE = CLONE_FAILURE_FACTOR * 17 / 36710
# The fewest repositories that the ceiling allows, so that a run over a short list may leave
# one repository to retry.
CLONE_FAILURE_CEILING_MIN = 1


def clone_failure_ceiling(repositories: int) -> int:
    """How many repositories a run over `repositories` may leave to retry and still count as
    complete."""
    return max(CLONE_FAILURE_CEILING_MIN, math.ceil(CLONE_FAILURE_SHARE * repositories))


def failure_ceiling(max_clone_failures: int | None, repositories: int) -> int:
    """The ceiling of the cloning step for a run over `repositories`: `max_clone_failures`
    (`--max-clone-failures`) when it is set, else `clone_failure_ceiling`. 0 turns the ceiling
    off."""
    return clone_failure_ceiling(repositories) if max_clone_failures is None else max_clone_failures


def worker_stop(signum, stack_frame) -> None:
    """A worker that receives a stop signal kills the git processes that it started and exits
    with the signal's code.

    Workers are separate processes. Without this handler, a SIGTERM to the collector would end
    the parent alone, and its workers would go on cloning into the scratch directory. A resumed
    run shares that directory, and a worker of the killed run could delete the clone that a
    worker of the resumed run is reading. The handler records nothing, and a resumed run redoes
    the repository in flight, as the rule of `finished_repositories.jsonl` requires.
    """
    bounds.stop_running()
    auth.discard()
    os._exit(128 + signum)


def install_worker_stop() -> None:
    """In a pool worker, handle SIGTERM and SIGINT by stopping. In the parent, do nothing."""
    if multiprocessing.parent_process() is None:
        return
    signal.signal(signal.SIGTERM, worker_stop)
    signal.signal(signal.SIGINT, worker_stop)
