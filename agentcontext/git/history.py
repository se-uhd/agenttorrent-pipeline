#!/usr/bin/env python3
"""Read one repository's commit history, for the steps that already have a clone.

The cloning step and the file history step run the same git queries over the same repositories.
The cloning step clones every repository in the sample to copy the files that the path patterns
match, and the file history step clones the repositories with such files to read each path's
history. The queries are the same and the clone is the expensive part. This module therefore
contains the queries, and whichever step has the clone runs them.

`GitFileMetadata` is the client: it runs one git command at a time through
`bounds.run_bounded`, under the bounds and with the credential helper. `measure_artifact`
reads the git columns of one configuration artifact from a checkout. `measure_repo` runs the
whole sequence for a step that has no clone yet, starting with the clone.

Every query method catches the exception of its command and returns a falsy default. A caller
that has to distinguish "no history" from "the command failed" gets no exception, and
`_failures` distinguishes the two instead: its two counters increase when a command raises, and
a caller compares them before and after whatever it measured.

Inputs and outputs: none. This module is a library for the steps of the collection stage.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agentcontext import caches
from agentcontext.git import auth, bounds
from agentcontext.repos import parse_and_convert_to_utc
from agentcontext import logs


# -------------------------------
# The git client
# -------------------------------
# GROUNDING: the time bound for one git command, and the bound that stops a very large
# repository. The largest repositories that the size bound admits clone with the collector's
# commands well within this bound (docs/history/clone-probe.csv has blobless clone times). It is
# a per-repository bound rather than a claim about repositories, because no single repository
# may delay the step for longer. A repository stopped here is reported, not pruned.
GIT_TIMEOUT_SECONDS = 3600
# How many times the history fetch is attempted under the clone's time bound. GROUNDING: a
# policy. A fetch that times out is either a transfer that stalled or a repository too large
# for the time bound, and one retry distinguishes the two at the cost of one more time bound.
# A repository that times out twice is recorded with its bounds and fetched again only by a
# run under larger bounds, and a third attempt here would repeat what the next run
# does by design. The clone keeps its attempts with a backoff (CLONE_RETRIES), which are meant for a
# network interruption, and this fetch runs right after a clone that succeeded.
HISTORY_FETCH_TRIES = 2
# GitHub's SSH and git URLs, which a `.gitmodules` may name, and the https address that the
# submodule update fetches them from instead, so that it needs no SSH key
# (`submodule_url_options`).
GITHUB_HTTPS = "https://github.com/"
GITHUB_OTHER_URL_PREFIXES = ("git@github.com:", "ssh://git@github.com/", "git://github.com/")


def submodule_url_options() -> list:
    """The `-c` options with which git reads GitHub's SSH and git URLs as `GITHUB_HTTPS`, one
    `insteadOf` value per prefix. The options go on the command line rather than into the
    clone's configuration, because git hands its `-c` options to the `git clone` of each
    submodule, which does not read the configuration of the clone that contains it."""
    return [option for prefix in GITHUB_OTHER_URL_PREFIXES
            for option in ("-c", f"url.{GITHUB_HTTPS}.insteadOf={prefix}")]


# A failure of this kind indicates nothing about the repository. git reports every other
# failure through an exit code.
TRANSIENT_GIT_EXCEPTIONS = (subprocess.TimeoutExpired, OSError)

# The counter pair before anything has failed.
_ZERO_FAILURES = (0, 0)


def _is_transient(exc: Exception) -> bool:
    """Whether this failure means "try again later" rather than "there is nothing there"."""
    return isinstance(exc, TRANSIENT_GIT_EXCEPTIONS)


class HistoryUnavailable(Exception):
    """The history fetch did not bring the history, with the cause, `time_bound`, `size_bound`
    or `exit`.

    `time_bound` and `size_bound` recur under the same bounds and belong to the repository.
    `exit`, a fetch that exited non-zero twice, belongs to the network on that day, such as a
    5xx during `git fetch --unshallow`, and a later run fetches the history again. Recorded as
    one outcome, every cause would read as "did not arrive under the time or size bound", and a
    network failure would never be fetched again under the same bounds.
    """

    def __init__(self, cause: str, detail: str = ""):
        super().__init__(f"the history did not arrive ({cause}"
                         + (f", {detail}" if detail else "") + ")")
        self.cause = cause


class MachineFailure(Exception):
    """git could not be started, such as because `fork()` failed, memory ran out or the
    binary is missing.

    The failure is the machine's and not the repository's. The cloning step therefore records
    the repository as `collector_error`, which the next run collects again, rather than as
    measured with blanks or as a repository without history. A timeout and a clone over the
    size bound remain the repository's failures, because they recur under the same bounds.
    """


# git reports a missing repository in its message, and every other failure with the same exit
# code. Classifying every non-zero exit as a deleted repository would let a DNS failure, a 503,
# an expired GitHub access token or a secondary rate limit delete data/validated/<repo> and
# prune every row of the repository.
_REPOSITORY_IS_GONE = (
    "repository not found",
    "does not appear to be a git repository",
    # GitHub's two closure messages, which the cloning step records as `dmca_takedown` and
    # `disabled_by_github` (`clone.permanent_failure_reason`): "Repository unavailable due to
    # DMCA takedown." and "Access to this repository has been disabled by GitHub staff."
    "repository unavailable due to",
    "access to this repository has been disabled",
)

# git's message for a recorded branch that the repository no longer has, after which
# `clone_repo` clones the default branch.
_BRANCH_NOT_FOUND_RE = re.compile(r"Remote branch .* not found in upstream", re.IGNORECASE)

# Left out on purpose: "remote branch", from git's "Remote branch <name> not found in upstream
# origin". The repository responded and therefore exists, and a recorded branch that is gone is
# not a finding about the repository. `clone_repo` falls back to the default branch, and a
# collected commit that the branch does not reach is `revision_unavailable`.
#
# Left out on purpose: "could not read from remote repository". die_initial_contact() in git's
# connect.c prints the phrase whenever the ref advertisement cannot be read over SSH, for any
# reason, such as a refused connection, a restarted agent, a failed host-key check or GitHub's
# concurrent-connection limit. For a repository that is gone, GitHub's SSH response prints
# "ERROR: Repository not found." on the line above, which the first entry matches.


def _stderr_says_repository_is_gone(text: str) -> bool:
    """Whether git's own message reports that the repository is gone or closed, not
    unreachable."""
    lowered = (text or "").lower()
    return any(phrase in lowered for phrase in _REPOSITORY_IS_GONE)


def remove_stale_locks(repo_dir: str) -> list[str]:
    """Delete the lock files that a git process killed at the timeout left in this clone.
    Returns their paths.

    `bounds.run_bounded` stops a command that exceeded its time bound with SIGKILL to its whole
    process group, and git deletes its lock files on exit and on the signals that it can catch,
    which SIGKILL is not. A `fetch --unshallow` holds `.git/shallow.lock` for its whole
    transfer. The second attempt of a fetch that the time bound stopped would therefore fail at
    once with "Unable to create '.git/shallow.lock': File exists", and the timeout would be
    recorded as a network failure, `exit`, which every later run fetches again. No other process
    runs git in a clone that a worker made, and every lock in it belongs to the killed command.
    """
    git_dir = Path(repo_dir) / ".git"
    stale = [p for p in git_dir.rglob("*.lock") if p.is_file() and not p.is_symlink()]
    for path in stale:
        path.unlink(missing_ok=True)
    return [str(p.relative_to(repo_dir)) for p in stale]


def _git_failed(what: str, exc: Exception) -> None:
    """Log a git call that raised, instead of letting it pass silently.

    Every caller of this function returns a falsy default, and downstream a falsy default is
    indistinguishable from a real result. A `subprocess.TimeoutExpired` in a history query
    reads exactly like "this file has no history", and the row that it belongs to can then be
    pruned as untracked. These failures stay non-fatal, because one unreachable repository
    must not end a long run, but they are logged, so that a run's log shows whether one
    occurred.
    """
    logs.warning("history", f"Git {what} raised {exc.__class__.__name__} ({exc}).")


class GitFileMetadata:
    """Run the git commands that read a repository's commit history, one at a time."""

    def __init__(self, use_ssh: bool = False, github_token: Optional[str] = None,
                 max_clone_bytes: int = None):
        self.use_ssh = use_ssh
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN")
        # Every caller of a git query returns a falsy default when it fails, and downstream a
        # falsy default reads as "this file is not in git", which prunes the row and deletes
        # the file. Counting the failures that indicate nothing about the repository lets the
        # file history step distinguish an absent file from an unreachable server.
        self.transient_failures = 0
        # The transient failures in which git could not be started at all.
        self.machine_failures = 0
        # The cause for which the last history fetch did not bring the history: `time_bound`,
        # `size_bound` or `exit`.
        self.history_failure = None
        # git's stderr from the last clone attempt, so that a caller can distinguish "this
        # repository is not there" from "the server could not be reached".
        self.last_clone_stderr = ""
        # The bound that stopped the last clone attempt, `time_bound` or `size_bound`, or None.
        # An attempt under the same bounds would be stopped again, and the clone is
        # not retried.
        self.last_clone_bound = None
        # The number of commands stopped at the size bound. A repository with a working tree
        # that grows past `max_clone_bytes` is stopped and reported rather than left to run
        # (see bounds).
        self.size_bound_exceeded = 0
        self.max_clone_bytes = (bounds.DEFAULT_MAX_BYTES
                                if max_clone_bytes is None else max_clone_bytes)

    def _build_clone_url(self, repo_name: str) -> str:
        """The URL that git is given. The GitHub access token is passed in the environment.

        See `auth` for the reason: a GitHub access token in the URL is a GitHub access token in
        argv, which `ps` shows and which any message that echoes the command line reproduces.
        """
        return auth.clone_url(repo_name, self.github_token, self.use_ssh)

    def _run_git_command(self, cmd: list, cwd: str, capture_output: bool = True,
                         bounded_path=None,
                         timeout: float = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_LFS_SKIP_SMUDGE"] = "1"
        # BatchMode prevents SSH passphrase prompts, because a git configuration may rewrite
        # HTTPS URLs to SSH.
        env.setdefault("GIT_SSH_COMMAND",
                        "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new")
        auth.apply(env, None if self.use_ssh else self.github_token)

        try:
            return bounds.run_bounded(
                cmd, cwd=cwd, env=env, timeout=timeout,
                bounded_path=bounded_path, max_bytes=self.max_clone_bytes,
                capture_output=capture_output)
        except Exception as e:
            if _is_transient(e):
                self.transient_failures += 1
            if isinstance(e, OSError):
                self.machine_failures += 1
            elif isinstance(e, bounds.GitSizeBoundExceeded):
                self.size_bound_exceeded += 1
            raise

    def clone_repo(self, repo_name: str, branch: str, temp_dir: str) -> bool:
        """Clone the full commit history without the historical file contents.

        Every metadata query that this class makes is a `git log` over the commit graph, and
        the only file contents that it reads are the checked-out contents at HEAD.
        `--filter=blob:none` fetches exactly that and leaves every past revision's blobs on the
        server until a query needs one, as `git log --follow` does for its rename detection.

        `--depth 1` followed by `git fetch --unshallow` would download every version of every
        file in a repository's history to read the history of the configuration artifacts,
        and the history queries would return the same results.

        Falls back to a plain full clone only when the server refuses a partial one, because
        `--filter` needs git 2.19+ at both ends (`clone.is_filter_unsupported`). A full clone
        after another failure, such as an early EOF, would fetch every version of every file
        and could reach the size bound or the time bound, which end the retries, where the next
        attempt of the partial clone would have succeeded. Each further command starts from an
        emptied destination, because a failed clone can leave files behind.
        """
        from agentcontext.git import clone

        def emptied() -> None:
            shutil.rmtree(temp_dir, ignore_errors=True)
            os.makedirs(temp_dir, exist_ok=True)

        url = self._build_clone_url(repo_name)
        self.last_clone_stderr = ""
        self.last_clone_bound = None
        try:
            result = self._run_git_command(
                ["git", "clone", "--filter=blob:none", "--single-branch",
                 "--branch", branch, url, temp_dir],
                cwd=".", bounded_path=temp_dir,
            )
            if result.returncode != 0 and clone.is_filter_unsupported(result.stderr or ""):
                emptied()
                result = self._run_git_command(
                    ["git", "clone", "--single-branch", "--branch", branch, url, temp_dir],
                    cwd=".", bounded_path=temp_dir,
                )
            if result.returncode != 0 and _BRANCH_NOT_FOUND_RE.search(result.stderr or ""):
                # The recorded branch may not exist any more. Cloning without --branch takes
                # the repository's current default branch, which for a renamed branch still
                # contains the recorded commit. output/snapshot-validation.csv records
                # repositories in exactly that state, with the commit reachable from the new
                # default. Without this fallback the clone fails, and the rows stay blank.
                emptied()
                result = self._run_git_command(
                    ["git", "clone", "--filter=blob:none", "--single-branch", url, temp_dir],
                    cwd=".", bounded_path=temp_dir,
                )
            self.last_clone_stderr = result.stderr or ""
            return result.returncode == 0
        except Exception as e:
            _git_failed(f"clone {repo_name}", e)
            if isinstance(e, subprocess.TimeoutExpired):
                self.last_clone_bound = "time_bound"
            elif isinstance(e, bounds.GitSizeBoundExceeded):
                self.last_clone_bound = "size_bound"
            return False

    def is_shallow_repo(self, repo_dir: str) -> "bool | None":
        """Whether this clone's history is truncated, or None when the query failed.

        None rather than False when the query's own git command raises, because a `fork()` refused
        at this one call would otherwise read as "the history is here" for a `--depth 1` clone, and
        every file would be measured against one commit and cached as complete.
        """
        try:
            result = self._run_git_command(
                ["git", "rev-parse", "--is-shallow-repository"], cwd=repo_dir)
        except Exception as e:
            _git_failed(f"rev-parse --is-shallow-repository in {repo_dir}", e)
            return None
        if result.returncode != 0:
            _git_failed(f"rev-parse --is-shallow-repository in {repo_dir}",
                        RuntimeError((result.stderr or "").strip()[-bounds.STDERR_TAIL:]))
            return None
        status = (result.stdout or "").strip()
        if status in ("true", "false"):
            return status == "true"
        logs.warning("history", f"Could not tell whether the commit history in {repo_dir} is "
                                f"complete. `git rev-parse --is-shallow-repository` returned "
                                f"{status!r}.")
        return None

    def ensure_full_history(self, repo_dir: str, timeout: float = GIT_TIMEOUT_SECONDS,
                            bounded_path=None, tries: int = HISTORY_FETCH_TRIES) -> bool:
        """Make the full commit history available for the `git log` queries.

        A blobless clone already has it. This method therefore does nothing in the ordinary case
        and fetches only for a shallow clone: the cloning step's `--depth 1` clones, submodules,
        which `git submodule update` may fetch shallow, and any clone left over from an older
        run.

        Returns whether the history is there afterwards, which is read from the clone, not
        from the fetch. A fetch killed at the timeout during its transfer leaves the clone at
        one commit, and trusting the fetch would measure every file against that one commit,
        cache the result as complete and never measure it again. A fetch killed after git
        rewrote `.git/shallow` does leave the history, which is why the method checks the clone
        rather than the exit code. A timeout is retried once, under the same time bound and
        after the lock files that the killed fetch left are deleted. A fetch over the size
        bound, when `bounded_path` names the clone, is not retried, because the second transfer
        would be the same bytes.
        """
        machine = self.machine_failures
        shallow = self.is_shallow_repo(repo_dir)
        if shallow is False:
            return True
        if shallow is None:
            if self.machine_failures != machine:
                raise MachineFailure(f"Could not start git to check whether {repo_dir} "
                                     f"is shallow")
            return False
        self.history_failure = None
        for attempt in range(1, tries + 1):
            # A fetch that failed after git rewrote `.git/shallow` left the history, and a
            # second `fetch --unshallow` on a complete clone fails with "--unshallow on a
            # complete repository does not make sense", which would log a needless warning.
            if attempt > 1 and self.is_shallow_repo(repo_dir) is False:
                self.history_failure = None
                break
            try:
                # Without submodules, because a fetch that recurses into them, git's default,
                # parses the `.gitmodules` of the working tree before it fetches. A `.gitmodules`
                # that git cannot parse, such as one with committed merge-conflict markers, would
                # fail the fetch on every attempt. The history of the repository needs no
                # submodule.
                result = self._run_git_command(
                    ["git", "fetch", "--unshallow", "--no-recurse-submodules"], cwd=repo_dir,
                    bounded_path=bounded_path, timeout=timeout)
            except bounds.GitSizeBoundExceeded as e:
                _git_failed(f"fetch --unshallow in {repo_dir}", e)
                self.history_failure = "size_bound"
                break
            except OSError as e:
                # A failure of the machine, not of the repository: a second attempt would run
                # on the same kernel.
                _git_failed(f"fetch --unshallow in {repo_dir}", e)
                raise MachineFailure(f"Could not start git to fetch the history of "
                                     f"{repo_dir}") from e
            except Exception as e:
                _git_failed(f"fetch --unshallow in {repo_dir}, attempt {attempt}/{tries}", e)
                # `time_bound` only for a timeout, and `exit` for anything else that raised. Once
                # recorded, `exit` stays: a fetch that the network refused on either attempt is
                # a network failure, whichever attempt the time bound then stopped.
                if self.history_failure != "exit":
                    self.history_failure = ("time_bound" if isinstance(e, subprocess.TimeoutExpired)
                                            else "exit")
                if isinstance(e, subprocess.TimeoutExpired):
                    remove_stale_locks(repo_dir)
                continue
            if result.returncode == 0:
                self.history_failure = None
                break
            _git_failed(f"fetch --unshallow in {repo_dir}, attempt {attempt}/{tries}",
                        RuntimeError((result.stderr or "").strip()[-bounds.STDERR_TAIL:]))
            self.history_failure = "exit"
        machine = self.machine_failures
        shallow = self.is_shallow_repo(repo_dir)
        if shallow is None and self.machine_failures != machine:
            raise MachineFailure(f"Could not start git to check whether {repo_dir} "
                                 f"is shallow after the fetch")
        return shallow is False

    def checkout_ref(self, repo_dir: str, ref: str) -> bool:
        """Resolve `ref`, check it out and verify that HEAD is that commit."""
        try:
            outputs = []
            for cmd in (["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                        ["git", "checkout", ref, "--"],
                        ["git", "rev-parse", "HEAD"]):
                result = self._run_git_command(cmd, cwd=repo_dir)
                if result.returncode != 0:
                    detail = auth.redact((result.stderr or "").strip()[-bounds.STDERR_TAIL:])
                    logs.warning("history", f"`{' '.join(cmd)}` in {repo_dir} exited "
                                            f"{result.returncode} ({detail or 'no error text'}).")
                    return False
                outputs.append((result.stdout or "").strip())
            wanted, _, actual = outputs
            full_hash = (len(ref) in (40, 64)
                         and all(char in "0123456789abcdefABCDEF" for char in ref))
            if (not wanted or actual != wanted
                    or (full_hash and actual.lower() != ref.lower())):
                logs.warning("history", f"Could not verify the checkout of {ref} in {repo_dir} "
                                        f"(resolved commit {wanted!r}, HEAD {actual!r}).")
                return False
            return True
        except Exception as e:
            _git_failed(f"checkout {ref} in {repo_dir}", e)
            return False

    def _init_submodules_recursively(self, repo_dir: str, wanted_paths, depth: int = 0) -> None:
        """Initialize the submodules on the chain that leads to each wanted path, one level at a
        time.

        A submodule's own .gitmodules exists only once that submodule has been cloned, and the
        levels have to be discovered as they are created rather than computed in
        advance. Depth is bounded because a submodule graph can be cyclic.
        """
        if depth >= 4:
            return
        wanted = bounds.wanted_submodules(repo_dir, wanted_paths)
        if not wanted:
            return
        self._init_submodule_level(repo_dir, wanted)
        for sub in wanted:
            inner = bounds.remaining_inside(sub, wanted_paths)
            sub_dir = os.path.join(repo_dir, sub)
            if inner and os.path.isdir(os.path.join(sub_dir, ".git")) or \
                    (inner and os.path.isfile(os.path.join(sub_dir, ".git"))):
                self._init_submodules_recursively(sub_dir, inner, depth + 1)

    def _init_submodule_level(self, repo_dir: str, wanted: list) -> None:
        """Initialize one level of submodules, and count a non-zero exit as a failure."""
        self._run_git_command(["git", "submodule", "sync", "--"] + wanted,
                              cwd=repo_dir, capture_output=True, bounded_path=repo_dir)
        result = self._run_git_command(
            ["git", *submodule_url_options(), "-c", "submodule.fetchJobs=8", "submodule",
             "update", "--init", "--depth", "1", "--force", "--"] + wanted,
            cwd=repo_dir, capture_output=True, bounded_path=repo_dir,
        )
        if result.returncode != 0:
            # A submodule with an unreachable or private URL fails with an exit code and no
            # exception, which the counters do not count. Every configuration artifact inside it
            # would then look like a file with no history that is not on disk, the case that
            # gets pruned and deleted.
            self.transient_failures += 1
            logs.warning("history", f"Submodule update failed in {repo_dir} for "
                                    f"{', '.join(wanted)}. The rows inside these submodules keep "
                                    f"blank Git columns.")

    def update_submodules(self, repo_dir: str, wanted_paths=()) -> None:
        """Initialize only the submodules that could contain one of `wanted_paths`.

        `--init --recursive` over everything would follow every submodule of every submodule
        from servers unrelated to the repository being measured. A submodule that contains none
        of the paths being looked up cannot change any measurement and is not
        initialized.
        """
        if not wanted_paths:
            # Omitting the paths would mean "initialize nothing", and the method
            # raises. A caller that omitted them would fetch no submodule and report every
            # configuration artifact inside one as untracked, which prunes its row from both
            # CSVs and deletes the file. An exception is easier to notice than rows missing from
            # the output CSVs.
            raise TypeError("update_submodules got no paths to look up. Without them, it "
                            "initializes no submodule.")
        if not bounds.wanted_submodules(repo_dir, wanted_paths):
            return
        try:
            self._init_submodules_recursively(repo_dir, wanted_paths)
        except Exception as e:
            _git_failed(f"submodule update in {repo_dir}", e)

    def file_exists_in_repo(self, repo_dir: str, filepath: str) -> bool:
        """Whether the path exists, through `os.path.lexists`, so that a symlink, even a broken
        one, counts as an existing configuration artifact."""
        return os.path.lexists(os.path.join(repo_dir, filepath))

    def is_symlink_in_repo(self, repo_dir: str, filepath: str) -> bool:
        return os.path.islink(os.path.join(repo_dir, filepath))

    def read_symlink_target(self, repo_dir: str, filepath: str) -> str:
        try:
            return os.readlink(os.path.join(repo_dir, filepath))
        except Exception as e:
            _git_failed(f"readlink {filepath}", e)
            return ""

    def get_path_commits(self, repo_dir: str, filepath: str) -> "PathCommits | None":
        """Every commit that touched one path, or None when a query raised.

        One call fills eight columns: `created_at`, `#commits`, `first_commit_sha` and
        `last_commit_sha` from the three `git log` queries here, and the four AI columns, which
        `commits.ai_authorship_from` derives from the same commits. The queries are three
        because each column has its own bound. The checkout bounds the creation and the last
        commit, and the count covers every ref of the clone, which is single-branch.
        """
        return path_commits(lambda cmd, cwd: self._run_git_command(cmd, cwd=cwd),
                            repo_dir, filepath)

    def get_root_commit_sha(self, repo_dir: str) -> Optional[str]:
        """The repository's own first commit, the commit with no parents.

        The query is per repository rather than per path, and the only query here that is not
        about a configuration artifact. The cloning step records it beside `head_sha`, so that a
        repository's row records where its history starts as well as the commit at which it
        was read.
        """
        try:
            result = self._run_git_command(
                ["git", "rev-list", "--max-parents=0", "HEAD", "--"], cwd=repo_dir)
            if result.returncode != 0:
                return None
            # A repository with more than one root commit lists them newest first, and
            # the oldest is where its history starts.
            roots = result.stdout.strip().splitlines()
            return roots[-1].strip() if roots else None
        except Exception as e:
            _git_failed(f"rev-list --max-parents=0 in {repo_dir}", e)
            return None

# -------------------------------
# One path's commits
# -------------------------------
# One record per commit, every field closed by a NUL byte, which no subject, body or name can
# contain. A subject or a body that contains a newline cannot split a record. `%at` is the
# author date as a Unix timestamp, by which `PathCommits.union` sorts. `%cI` is the committer
# date, which `created_at` records.
PATH_COMMIT_FIELDS = ("hash", "author_name", "author_email", "committer_name",
                      "committer_email", "author_ts", "committer_date", "subject", "body")
PATH_COMMIT_FORMAT = "%x00%H%x00%an%x00%ae%x00%cn%x00%ce%x00%at%x00%cI%x00%s%x00%b%x00"


def parse_records(raw: str, fields: tuple) -> list:
    """Read NUL-delimited commit fields, and the RS/US-delimited fields that tests give."""
    return [record for record, _status in _record_parts(raw, fields)]


def _record_parts(raw: str, fields: tuple):
    """Yield each commit's fields with the name-status output that follows them."""
    if "\0" in raw:
        parts = raw.split("\0")
        if parts[0].strip():
            raise ValueError("invalid path-history record boundary")
        index = 1
        while index < len(parts):
            if index + len(fields) >= len(parts):
                raise ValueError("incomplete path-history record")
            yield ({name: value if name in ("subject", "body") else value.strip()
                    for name, value in zip(fields, parts[index:index + len(fields)])},
                   parts[index + len(fields)].strip())
            index += len(fields) + 1
        return
    for chunk in raw.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split("\x1f")
        while len(parts) < len(fields):
            parts.append("")
        yield ({name: parts[index].strip() for index, name in enumerate(fields)},
               parts[len(fields)].strip() if len(parts) > len(fields) else "")


@dataclass
class PathCommits:
    """Every commit that touched one path, from the three queries that the columns come from.

    `followed` is `git log --follow` up to the checkout, reversed here to oldest first. Its
    first record is the commit that created the file, under whatever name it had then. A copy
    of another file was created by the commit that copied it, and `renames_only` gives the
    reason. `created_at` and `first_commit_sha` come from `first()`, which takes the first
    commit of the exact query instead where that commit is older than the first commit of the
    follow query and the follow query did not list it, such as the commit that first added a
    path that a later commit deleted and another commit added again as a copy.

    `exact` is the query without `--follow`, every commit that touched the path under this
    name up to the checkout, and its last record is `last_commit_sha`. `everywhere` is the
    exact query over every ref of the clone, `--all`. Every clone is `--single-branch`. Those
    refs are one branch and the tags that point into its history, and no other
    branch reaches the query. The branch's tip bounds `everywhere`, not the checkout. Where the
    branch had moved past the recorded commit when the clone fetched it, the later commits
    reach `everywhere` and no other list.

    `#commits` and the AI columns count over the union of the three lists, every commit of
    that branch that touched the file under any of its names. Counting `everywhere` alone, the
    current name only, would give a renamed file a `created_at` from before the rename and a
    count from after it.
    """

    followed: list
    exact: list
    everywhere: list
    source_path: str = ""

    def first(self):
        """The creation commit, or None for a path with no history.

        The first record of `followed`, or the first record of `exact` where the follow query
        did not list that commit and its author date is earlier. `renames_only` ends the
        follow query at a copy, and git often detects the file that a commit adds again at a
        deleted path as a copy of another file of that commit, such as a CLAUDE.md from which
        the commit moves text into the added file. The exact query lists the commits of the
        path before the deletion, and `union()` includes them. Without the comparison,
        `#commits` and the AI columns would count commits from before the commit that
        `created_at` names. Because the exact query lists only the commits of this path, it
        brings in no history of another file, just as `renames_only` keeps that history out of
        the follow query. Where the follow query listed the first record of `exact`, as for a
        renamed path, the first record of `followed` is the older one, and `first()` returns it
        whatever the author dates of the two records.

        Where `followed` is empty and `exact` is not, the first record of `exact` is returned.
        `git log --follow` lists nothing, at exit 0, for a path that a merge added and that
        neither parent contained. Without the fallback, the row would come back with
        `#commits` and `last_commit_sha` filled and the other two blank, which `file_history`
        rejects. A path with no history at all leaves both lists empty and returns None.
        """
        if not self.followed:
            return self.exact[0] if self.exact else None
        created = self.followed[0]
        if self.exact and self.exact[0]["hash"] not in {r["hash"] for r in self.followed}:
            if _author_ts(self.exact[0]) < _author_ts(created):
                return self.exact[0]
        return created

    def last(self):
        """The newest commit up to the checkout that touched the path under this name."""
        return self.exact[-1] if self.exact else None

    def union(self) -> list:
        """Every commit once, oldest first by author date."""
        seen = {}
        for record in (*self.followed, *self.exact, *self.everywhere):
            seen.setdefault(record["hash"], record)
        return sorted(seen.values(), key=_author_ts)


def _author_ts(record: dict) -> int:
    """The author date of a commit record as a Unix timestamp, 0 where it is blank."""
    return int(record.get("author_ts") or "0")


# The three queries, as the flags that distinguish them, in the order of the fields of
# PathCommits. The first lists the newest commit first, and `path_commits` reverses the list,
# because `git log --follow --reverse` over a renamed file lists the rename commit and nothing
# else, neither the commits under the old name nor the commits after the rename (git 2.55).
# Over a file that was never renamed, the two orders list the same commits. The first query
# includes `--name-status` because `--follow` crosses a copy as well as a rename, and
# `renames_only` ends the list at the copy.
PATH_QUERIES = (["--follow", "--name-status"], ["--reverse"], ["--reverse", "--all"])


def renames_only(raw: str) -> list:
    """The records of `git log --follow --name-status`, newest first, up to the file's creation.

    `--follow` runs git's copy detection over every file of the commit that added the path,
    and crosses to the source of a copy as it does to the source of a rename. A file with
    content that resembles the content of another file, which the commit kept, is followed into
    that file's history. An `AGENTS.md` written as a copy of a `CLAUDE.md` beside it would get
    the `CLAUDE.md`'s creation commit, and git's similarity score can rate even two unrelated
    short files as a copy. A copy is where the file was created, and the list therefore stops at
    the commit with the status `C`. git offers no flag that follows renames alone.
    """
    kept = []
    for record, status in _record_parts(raw, PATH_COMMIT_FIELDS):
        kept.append(record)
        if status.startswith("C"):
            break
    return kept


def path_commits(run, repo_dir: str, filepath: str) -> "PathCommits | None":
    """Run the three queries through `run(cmd, cwd)`. Returns None when any raised or failed.

    A non-zero exit is a failure like a raise. `git log` lists nothing and exits 0 for a path
    that is not in the history, and exits non-zero when it could not read the history. On a
    blobless clone, that happens when the blob fetch that `--follow` needs fails on the network.
    Counted as an empty list, the row that it produced, with `#commits` and `last_commit_sha`
    filled and the other two blank, would be cached and published, and no later run would
    query the path again.
    """
    # Over an empty working tree, because the queries compare trees, and at a gitlink git reads
    # the `.gitmodules` of the working tree. A `.gitmodules` there that git cannot parse, such as
    # one with committed merge-conflict markers, would end the query with exit 128 and leave the
    # history of the file unread in every run. Over an empty working tree, git reads the file
    # from the index, where a parse error is a message. The queries read no other file of the
    # working tree, because their format names the identities as recorded, without the mailmap.
    with tempfile.TemporaryDirectory(prefix="no-worktree-") as empty:
        return _path_commits(run, repo_dir, filepath, empty)


def _path_commits(run, repo_dir: str, filepath: str, worktree: str) -> "PathCommits | None":
    """`path_commits` with the working tree `worktree`."""
    lists = []
    for flags in PATH_QUERIES:
        # `:(literal)`, because `--` stops option parsing and does not make the argument
        # literal. git matches a pathspec with wildmatch, and a filename that contains `*`, `?`
        # or a bracket would select another file's history. A published row can contain GitHub's
        # `.github/instructions/*.instructions.md` placeholder verbatim, and every column that
        # this function fills, the four git columns and the four AI columns, would describe
        # another file once its repository adds a file that the pattern matches.
        cmd = ["git", f"--work-tree={worktree}", "log", *flags,
               f"--format={PATH_COMMIT_FORMAT}", "--", f":(literal){filepath}"]
        try:
            result = run(cmd, repo_dir)
        except Exception as e:
            _git_failed(f"log {' '.join(flags)} -- {filepath} in {repo_dir}", e)
            return None
        if result.returncode != 0:
            _git_failed(f"log {' '.join(flags)} -- {filepath} in {repo_dir}",
                        RuntimeError(f"exit {result.returncode}: "
                                     f"{(result.stderr or '').strip()[-bounds.STDERR_TAIL:]}"))
            return None
        if "--follow" in flags:
            records = renames_only(result.stdout)
            records.reverse()
        else:
            records = parse_records(result.stdout, PATH_COMMIT_FIELDS)
        lists.append(records)
    return PathCommits(*lists)


def history_from(commits: "PathCommits | None") -> "caches.FileHistory":
    """The four git columns of one path, from its commits. Blank where the history query failed.

    `created_at` keeps git's text where the date does not parse, such as for a commit of the
    year 10000, so that the cache records a complete measurement, and the steps that publish
    the column leave it blank (`repos.to_utc_or_blank`).
    """
    if commits is None:
        return caches.FileHistory()
    first, last = commits.first(), commits.last()
    return caches.FileHistory(
        created_at=parse_and_convert_to_utc(first["committer_date"]) if first else "",
        commits=str(len(commits.union())),
        first_commit_sha=first["hash"] if first else "",
        last_commit_sha=last["hash"] if last else "",
    )


# -------------------------------
# Clone with retries
# -------------------------------
# GROUNDING: a policy, not a measurement. Three attempts, 10 s and 20 s apart, outlast a
# network interruption of about 30 s and stop after that. One unreachable host therefore costs at
# most 30 s of waiting per repository rather than stalling the run. A failure on all three
# attempts is reported as git_unavailable and the rows are kept, and stopping early costs a
# re-run, not lost data. An attempt that the time bound or the size bound stopped is not
# repeated, because the next attempt runs under the same bounds.
CLONE_RETRIES = 3
RETRY_BACKOFF = 10  # seconds, doubled on each retry


def _clone_outcome(extractor: GitFileMetadata) -> str:
    """The cause of a failed clone: "clone_failed" only when git reported that the repository
    is not there, and "bound_exceeded" when the time bound or the size bound stopped it, which
    another attempt under the same bounds would meet again.

    Every other failure is "git_unavailable", which keeps the rows, as "bound_exceeded" does. The
    asymmetry is deliberate: a row kept after an unreachable server costs a re-run, while a row
    pruned after one deletes a real configuration artifact from data/validated/ and from both
    CSVs.
    """
    if extractor.last_clone_bound is not None:
        return "bound_exceeded"
    if extractor.transient_failures or extractor.size_bound_exceeded:
        return "git_unavailable"
    if _stderr_says_repository_is_gone(extractor.last_clone_stderr):
        return "clone_failed"
    return "git_unavailable"


def _clone_with_retries(extractor: GitFileMetadata, repo_name: str, branch: str,
                        temp_dir: str, retries: int = CLONE_RETRIES,
                        backoff: int = RETRY_BACKOFF, sleep=time.sleep) -> bool:
    """Clone with exponential backoff between attempts.

    Returns True on the first successful clone, False if all `retries` attempts
    fail. `sleep` is injectable so that tests do not have to wait. The wait is between
    attempts, not after the last one. An attempt that the time bound or the size bound stopped
    ends the loop, because the bounds are the same for every attempt. Each further attempt would
    hold the worker for the whole time bound or fetch the same bytes again, on every run of the
    step. The backoff is meant for a network interruption.
    """
    for attempt in range(retries):
        if extractor.clone_repo(repo_name, branch, temp_dir):
            return True
        if extractor.last_clone_bound is not None:
            logs.info("history", f"    The {extractor.last_clone_bound.replace('_', ' ')} "
                                 f"stopped the clone of {repo_name}. Skipped the remaining "
                                 f"attempts.")
            break
        if attempt == retries - 1:
            # git's own message, such as `Failed to connect to github.com port 443`, which the
            # operator reads for the cause of the outcome `git_unavailable`.
            said = (extractor.last_clone_stderr or "").strip().splitlines()
            logs.warning("history", f"    Git could not clone {repo_name} in {retries} attempts "
                                    f"({said[-1] if said else 'no message'}).")
            break
        wait = backoff * (2 ** attempt)
        logs.info("history", f"    Cloning {repo_name} again in {wait}s (attempt {attempt + 2} "
                             f"of {retries}).")
        sleep(wait)
        shutil.rmtree(temp_dir, ignore_errors=True)
        os.makedirs(temp_dir, exist_ok=True)
    return False


# -------------------------------
# Filesystem helpers
# -------------------------------
def _find_submodule_root(repo_dir: str, filepath: str):
    """If filepath is inside a git submodule, return (submodule_dir, relative_path).

    A submodule has a `.git` file, not a directory, that points to the parent's .git/modules/
    tree. The function searches the file's directory and each directory above it.
    """
    parts = Path(filepath).parts
    for i in range(len(parts) - 1, 0, -1):
        candidate = Path(repo_dir) / Path(*parts[:i])
        git_file = candidate / ".git"
        if git_file.exists() and git_file.is_file():
            relative = str(Path(*parts[i:]))
            return str(candidate), relative
    return None


# -------------------------------
def _failures(extractor: "GitFileMetadata") -> tuple:
    """The two counters that record that a git command raised rather than returned a result.

    `GitFileMetadata` keeps them because every one of its query methods returns a falsy
    default on an exception, and downstream a falsy default is what "this file has no
    history" looks like. `_ZERO_FAILURES` is the value they start at.
    """
    return (extractor.transient_failures, extractor.size_bound_exceeded)


def _checkout(extractor: "GitFileMetadata", repo_dir: str, repo_name: str,
              head_sha: str) -> bool:
    """Require the complete collected commit hash before measuring metadata."""
    if (len(head_sha) not in (40, 64)
            or any(char not in "0123456789abcdefABCDEF" for char in head_sha)):
        logs.warning("history", f"The collected head_sha {head_sha!r} of {repo_name} is not a "
                                f"complete commit hash. Measured no file history and no AI "
                                f"authorship.")
        return False
    if extractor.checkout_ref(repo_dir, head_sha):
        return True
    logs.warning("history", f"Git could not check out the collected commit {head_sha} of "
                            f"{repo_name} in {repo_dir} (see the git error above). Measured no "
                            f"file history and no AI authorship.")
    return False


# The start of the name of every clone that `measure_repo` makes, by which the file history step
# finds the clones that a stopped run left in its scratch directory.
CLONE_PREFIX = "file_history_"


def measure_repo(repo_name: str, head_sha: str, head_commit_date: str, branch: str,
                 artifacts: list, github_token: Optional[str],
                 sleep=time.sleep, scratch_dir=None, path_histories: dict | None = None) -> tuple:
    """Clone one repository once and read the history of every configuration artifact that this
    run is missing for it.

    Returns (facts_by_artifact, error). `error` is "" on success, "clone_failed" when the
    repository is permanently unavailable, "bound_exceeded" when the time bound or the size
    bound stopped the clone or the fetch of the complete history, "git_unavailable" when
    complete history or setup is unavailable otherwise or the history query of a
    configuration artifact failed, with the facts of every other configuration artifact, and
    "revision_unavailable" when the collected commit cannot be verified. Only
    "clone_failed" can prune a row. No date or branch-tip substitution supplies measurements
    for a collected commit.

    `sleep` is injectable so that a test with a repository that does not clone does not wait
    for the backoff. `scratch_dir` is the directory for the clone, and None means the
    interpreter's temporary directory, which in a container is the overlay rather than a
    mounted volume. `path_histories`, when a dict is given, receives the commits of every
    configuration artifact measured, from which the file history step also fills the four AI
    columns.
    """
    extractor = GitFileMetadata(github_token=github_token)
    if scratch_dir is not None:
        Path(scratch_dir).mkdir(parents=True, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=CLONE_PREFIX, dir=scratch_dir)
    try:
        if not _clone_with_retries(extractor, repo_name, branch, temp_dir, sleep=sleep):
            return {}, _clone_outcome(extractor)
        if not extractor.ensure_full_history(temp_dir, bounded_path=temp_dir):
            logs.warning("history", f"Could not fetch the complete commit history of "
                                    f"{repo_name} into {temp_dir} (see the git error above). "
                                    f"Measured no file history and no AI authorship.")
            return {}, ("bound_exceeded" if extractor.history_failure in ("time_bound", "size_bound")
                        else "git_unavailable")
        if not _checkout(extractor, temp_dir, repo_name, head_sha):
            return {}, "revision_unavailable"
        # Full history and checkout have explicit verified results. A timeout during a
        # recovered history fetch must not invalidate those results through an old counter.
        before_submodules = _failures(extractor)
        extractor.update_submodules(temp_dir, artifacts)
        if _failures(extractor) != before_submodules:
            # The clone succeeded, but a later command failed, such as a submodule fetch that
            # timed out or a working tree that reached the size bound. Every query below would
            # read an incomplete repository.
            return {}, "git_unavailable"

        measured, failed = {}, False
        for artifact in artifacts:
            before = _failures(extractor)
            commits = read_artifact_history(extractor, temp_dir, artifact)
            if commits is None or _failures(extractor) != before:
                # A history query raised or exited non-zero rather than returning a result.
                # Its callers return a falsy default, and a falsy default is what "this file
                # has no history" looks like. A row measured through one would be
                # cached as a fact.
                logs.warning("history", f"Could not read the history of {artifact} of "
                                        f"{repo_name}. A git query failed.")
                failed = True
                continue
            measured[artifact] = history_from(commits)
            if path_histories is not None:
                path_histories[artifact] = commits
        # A failed query is often a blob fetch that `git log --follow` makes on a blobless
        # clone and that a burst of GitHub errors ends. The step measures the
        # repository again (`git_unavailable`).
        return measured, ("git_unavailable" if failed else "")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def read_artifact_history(extractor: "GitFileMetadata", repo_dir: str,
                          artifact: str) -> "PathCommits | None":
    """Every commit that touched one path, out of a clone that is already checked out.

    A path inside a submodule is read from the submodule. The parent repository tracks a
    submodule as a gitlink, and `git log` in the parent lists nothing for a path below it. The
    row would then come back with no history for a file that is on disk.
    """
    source = artifact
    root = Path(repo_dir).resolve()
    try:
        # Resolve directory links, retaining a final file link's own commit history.
        parent = (root / artifact).parent.resolve(strict=True)
        source = (parent / Path(artifact).name).relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        # Missing parents and links outside the checkout cannot supply target history.
        pass
    commits = extractor.get_path_commits(repo_dir, source)
    if ((commits is None or commits.first() is None)
            and extractor.file_exists_in_repo(repo_dir, source)):
        found = _find_submodule_root(repo_dir, source)
        if found:
            submodule_dir, _ = found
            if not extractor.ensure_full_history(submodule_dir, bounded_path=submodule_dir):
                logs.warning("history", f"Could not fetch the complete commit history of "
                                        f"submodule {submodule_dir}. Measured no file history "
                                        f"and no AI authorship of {artifact}.")
                return None
            commits = extractor.get_path_commits(*found)
    if commits is not None:
        commits.source_path = source

    return commits


def measure_artifact(extractor: "GitFileMetadata", repo_dir: str,
                     artifact: str) -> "caches.FileHistory":
    """Read one path's history out of a clone that is already checked out."""
    return history_from(read_artifact_history(extractor, repo_dir, artifact))
