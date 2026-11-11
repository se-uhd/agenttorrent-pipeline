#!/usr/bin/env python3
"""
Collect the configuration artifacts of the six tools from GitHub repositories.

Given a CSV of owner/repo names, the step clones each repository, copies its
configuration artifacts to an output directory, and writes a summary CSV with boolean flags.
The collector, `agentcontext/collector.py`, processes one repository. This module decides
which repositories a run collects, in which order and when it stops, and it writes the lists.

Inputs:
  - ../classification/repos_annotated_included.csv (or --repo-csv)
  - ../classification/repos_excluded.csv, beside the list, if it is there: the repositories that
    the labeling found gone, which a collection may lack without a new collection
  - heuristics/vendored_dirs.txt, through `agentcontext.heuristics`
  - heuristics/local_state_paths.csv, the places of the tools' local state that the step does
    not copy, through `agentcontext.heuristics`
  - heuristics/import_patterns.csv, through `agentcontext.imports`

Outputs:
  - data/collected/<owner§repo>/ (or --collected-dir): the configuration artifacts of each
    repository
  - data/collected/repos.csv (or --collected-csv): one row per repository, at the root of the
    tree that it lists
  - data/collected/clone_failed.csv (or --fail-csv): repositories that could not be cloned
  - clone_logs/<owner§repo>.log: the clone log of each repository
  - data/file_lists/<owner§repo>.ls-tree.xz (or --file-lists-dir): every path of the clone at
    the collected commit, outside the collected tree. No step reads the file lists.
  - data/workflows/<owner§repo>/ (or --workflows-dir): the clone's workflow files, outside the
    collected tree. No step reads these copies.
  - data/imported/<owner§repo>/ (or --imported-dir): the files that an import of a copied
    configuration artifact reaches, which the import detection step reads
  - file_history.csv (or --file-history), submodules.csv (or --submodules) and
    ai_authorship.csv (or --ai-authorship): the three caches, to which the run adds its
    measurements
  - collect_commits/ (or --commits-dir): for each repository with a commit history that the step
    read, the per-repository commit record, the shard of the matched commits, the shard of the
    commits of the tools outside the six under other/, and the commit log (a .log.xz) for the
    commit reclassification
  - output/all_ai_commits.csv (or --ai-commits-csv), written at the end of the run, with
    other_ai_commits.csv, all_ai_commits-unmatched.csv, all_ai_commits-mentions.csv and
    ai_commits-repos.csv beside it

Cloning the sample is a long run. The step therefore appends each repository to the
append-only list of finished repositories (finished_repositories.jsonl, or --finished) as soon
as the repository finishes. A later run takes over the repositories that the list records, as
`reuse.outdated` decides, and a crash costs only the repositories in flight rather than the
whole run. A run that resumes the collection in progress (`resume.open_collection`) takes over
what that collection already collected or confirmed without querying the heads again. So does a
run over the list of the last collection that ended, and a run over the list of either
collection without repositories that GitHub no longer serves. A run that starts a new
collection, over another list or after --from repository_cloning, queries them. --fresh removes
the list and what the collection left, and collects everything again. Each line of the list stores the repository's CSV rows,
and the final CSVs are assembled from the list rather than computed again.

No later step changes the collected tree, data/collected/.
agentcontext/steps/collected_file_cleanup.py reads it and writes data/validated/ with its own
list, data/validated/repos.csv, which every later step reads. After a failure of the cleanup or
of a later step, a run from the cleanup recovers without cloning anything again.
"""

import argparse
import collections
import csv
import hashlib
import os
import re
import shutil
import signal
import sys
import time
import traceback
import multiprocessing
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from agentcontext import STAGE_DIR
from agentcontext import cli


# A commit message is a field of the shards that this step writes. A long message exceeds the
# csv module's default limit of 131,072 characters, and the reader would raise and fail the
# step. Every reader of the commit CSV sets the limit to sys.maxsize, and so does this step.
csv.field_size_limit(sys.maxsize)
from agentcontext import caches
from agentcontext.git import auth, bounds, clone, history
from agentcontext import collector, commits, guards, logs, paths, heuristics, resume, reuse



_REPO_PATTERN = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
def parse_repo_csv(csv_path: Path, column: str) -> list[str]:
    """The values of `column` in the CSV that are a whole `owner/repo`, without duplicates and
    in their order.

    The step skips and logs a value that is more than a name. A search for the first
    `word/word` in such a value would read `github.com/owner` from a URL as the row's
    repository, and from a path inside a repository the repository that contains it.
    """
    with open(csv_path, encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or column not in reader.fieldnames:
            logs.error('repository_cloning', f"{csv_path} has no column '{column}'. Its columns "
                       f"are {reader.fieldnames}.")
            raise SystemExit(1)
        seen: set[str] = set()
        repos: list[str] = []
        skipped: list[str] = []
        for row in reader:
            raw = (row.get(column) or "").strip()
            m = _REPO_PATTERN.fullmatch(raw)
            if not m:
                if raw:
                    skipped.append(raw)
                continue
            repo = f"{m.group(1)}/{m.group(2)}"
            if repo in seen:
                continue
            seen.add(repo)
            repos.append(repo)
    if skipped:
        logs.warning('repository_cloning', f"Skipped {len(skipped):,} values of {csv_path} that "
                     f"are not an owner/repo name. The first is {skipped[0]!r}.")
    return repos


# The list of the repositories in the sampling frame that are not in repos_annotated.csv, the
# copies that the classification labeled `true` included, with the reason of each. The
# classification writes it beside the sample (`write_outputs` in
# 2_classification/1_classify_repos.py).
EXCLUDED_CSV_NAME = "repos_excluded.csv"


def labeling_gone(repo_csv: Path) -> set:
    """The repositories that the list of excluded repositories beside `repo_csv` names with the
    reason `not_found_or_no_access`, which GitHub no longer served when the labeling requested
    their README, or an empty set when the list is not there.

    The collection in progress, and the last collection that ended, continue over a sample that
    lacks only such repositories (`resume.open_collection`)."""
    path = repo_csv.with_name(EXCLUDED_CSV_NAME)
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return {row["repo_name"] for row in csv.DictReader(f)
                    if row.get("reason") == clone.NOT_FOUND_OR_NO_ACCESS and row.get("repo_name")}
    except FileNotFoundError:
        return set()
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        logs.warning('repository_cloning', f"Could not read {path} ({error}). The collection "
                     f"continues over no list that lacks repositories that the labeling found "
                     f"gone.")
        return set()


# GROUNDING: a policy. A worker that receives a stop signal kills its git processes and exits
# at once. Five seconds leave every worker enough time to exit before the parent kills the
# remaining workers and exits itself.
WORKER_STOP_SECONDS = 5.0


def stop_pool(signum, stack_frame) -> None:
    """The parent's stop handler. It ends every worker, and with it the worker's git processes,
    then exits.

    It exits with `os._exit`, because the pool's management thread would otherwise wait on
    workers that are gone. The finished repositories are in the list of finished repositories.
    A resumed run redoes the repositories that were in flight, which the list does not contain.
    """
    # Ends the parent's own git processes as well. `guards.remote_responds` runs `git ls-remote`
    # on this thread in a session of its own, which the terminal's signal does not reach, and
    # `os._exit` skips the `finally` that would otherwise reap the process.
    bounds.stop_running()
    for child in multiprocessing.active_children():
        child.terminate()
    deadline = time.time() + WORKER_STOP_SECONDS
    for child in multiprocessing.active_children():
        child.join(max(0.0, deadline - time.time()))
    for child in multiprocessing.active_children():
        child.kill()
    auth.discard()
    os._exit(128 + signum)


def processing_order(repos: list) -> list:
    """The repositories in the order that the step takes them, sorted by a hash of the name.

    The input is the sample in alphabetical order, which puts one owner's repositories next to
    each other. In that order, the large clones of one owner would run side by side, and the
    failures of an organization that no longer exists would come in a row. A stopped run would
    also cover the names from `0x` to wherever it stopped rather than a part of the whole
    sample. The hash spreads one owner's repositories over the run. Because it is the same on
    every Start, a resumed run takes the remaining repositories in the same order. The rows
    that this step writes stay in input order, because the step assembles them from the input
    list.
    """
    return sorted(repos, key=lambda repo: hashlib.sha256(repo.encode("utf-8")).hexdigest())


def heads_without_response(repos: list, done: dict) -> list:
    """The repositories of `repos`, in input order, that `done` records as taken over while git
    returned no head commit. The step queries their heads again and counts them with the
    repositories to redo."""
    return [r for r in repos if r in done and done[r][8]]


def history_to_read_again(config: collector.CollectionConfig, repos: list, done: dict) -> set:
    """The repositories of `repos` with an ok line in `done`, a list of finished repositories,
    and a per-repository commit record that requires reading the commit history again. Such a
    record is one that `resume.history_needs_reread` selects, such as a record without its
    commit log, or one with a commit log that failed the last commit reclassification
    (`resume.reclassification_failed`)."""
    failed = resume.reclassification_failed(config.reclassification_report, config.commits_dir)
    return {r for r in repos if r in done and done[r][1] is None
            and (r in failed
                 or resume.history_needs_reread(config.commits_dir, r, config.clone_timeout,
                                                config.max_clone_bytes))}


def to_retry(config: collector.CollectionConfig, repos: list, done: dict,
             collection: str | None = None) -> set:
    """The repositories of `repos` that `done`, a list of finished repositories, marks to redo.

    The step redoes a repository with:

    - a clone that failed on the network, the GitHub access token or the machine
    - an ok line marked to redo
    - a clone over a smaller size bound
    - a clone that the time bound stopped under a smaller bound, or in a collection other than
      `collection`, the collection in progress
    - a commit history or a history fetch that the per-repository commit record marks to redo
    - a commit log that failed the last commit reclassification
    - a collected tree that is missing
    """
    # The list of finished repositories, the per-repository commit record and the tree can each
    # require a retry. The step checks both the list and the record, because a tree written
    # before the list recorded `redo` has that value only in the record. A repository with a
    # record of copied files and no directory was restored from an archive that contains the
    # lists but not the collected trees. A resumed run that relied on the list alone would
    # record such a repository as collected with nothing on disk, and the validator would refuse
    # it on every later Start.
    #
    # A line marked to redo is not redone while the time bound that stopped its last clone
    # holds, because the clone would be stopped again. A record that requires reading the commit
    # history again is redone whatever the time bound. A retry that the time bound stops does
    # not keep such a collection (`run_collection`). A line that kept one therefore has a record
    # that came to require the reading after the line was written, such as a commit log that was
    # lost or failed the reclassification since. If the step kept that collection, its commit
    # log would stay missing or failed for the rest of the collection, and a missing commit log
    # makes the step exit 4 on every Start.
    def bound_holds(r: str) -> bool:
        return resume.time_bound_holds(done[r][9], config.clone_timeout, done[r][10], collection)

    reread = history_to_read_again(config, repos, done)
    return {r for r in repos if r in done
            and (resume.outcome_of(done[r][1]) in resume.TRANSIENT_OUTCOMES
                 or resume.clone_needs_redo(done[r][1], done[r][5], config.max_clone_bytes,
                                            done[r][9], config.clone_timeout, done[r][10],
                                            collection)
                 or (done[r][1] is None and resume.tree_missing(config, r, done[r][0]))
                 or r in reread
                 or (done[r][4] and not bound_holds(r)))}


def run_collection(
    config: collector.CollectionConfig, repos: list[str], workers: int = 5,
    finished_path: Path | None = None, check_heads: bool = True,
    collection: dict | None = None, head_waits: tuple = (),
) -> tuple[list[str], list[str], list[dict], list[dict]]:
    """Process all repositories (serially if workers<=1, else in a process pool).

    Returns (main_rows, fail_rows, file_history_rows, submodule_rows). `main_rows` and
    `fail_rows` are in input order, and `fail_rows` contains only the repositories with a
    failed clone.

    With a `finished_path`, the step takes over the repositories already recorded there, as
    `reuse.outdated` decides, and appends every repository as soon as it finishes, so that a
    crash costs only the repositories in flight. With `check_heads`, the step queries GitHub
    for the head of every repository that it takes over, except the repositories that
    `collection`, the collection in progress (`resume.open_collection`), collected or took over
    in an earlier run. While git returns no head commit for more of them than the ceiling
    allows, the step waits `head_waits` (`reuse.remote_heads`). Every line that this run writes
    names that collection. The rows are assembled in input order from the list of finished
    repositories and this run's results. The step ignores the lines of repositories no longer
    in `repos`, and a changed input CSV therefore needs no versioning. Without a
    `finished_path`, nothing is resumed and nothing is written.

    The step measures the free space on the volume of the clone directory before the first
    clone and after every `config.disk_check_every` finished repositories. Below
    `config.min_free_bytes`, it starts no further clone. The clones in flight finish and are
    recorded, and if a repository was left unstarted, the step then raises `guards.LowDiskStop`
    with the rows assembled so far. Low space measured after every repository has started is
    only a warning, because no clone is left to stop. The step lets the clones in flight finish
    rather than stop them, because a clone killed halfway is a repository to redo, while a line
    that the step appends after the worker returned is never torn.
    """
    done = resume.read_finished(finished_path) if finished_path else {}
    under = reuse.current(config) if finished_path else None
    current = collection["collection"] if collection else None
    # A repository with a clone that failed on the network, the GitHub access token or the
    # machine counts as done in the lists that a stopped run writes and as pending for the next
    # run. The next run redoes it and appends a newer line that replaces the old one.
    redone = to_retry(config, repos, done, current)
    retried = set(redone)
    queried = {}
    if done:
        retried |= reuse.outdated(config, repos, done, redone, under, check_heads, queried=queried,
                                  collection=collection, head_waits=head_waits)
        if collection is not None and finished_path:
            # Saves the repositories that the head query confirmed. A resumed run of this
            # collection takes them over without querying them again.
            resume.save_collection(finished_path, collection)
        # A line that kept the earlier collection keeps its shards as they are while the time
        # bound that stopped its retry holds. A clone to replace commits that cannot be
        # classified again would be stopped again, and the commit reclassification leaves the
        # shards of other heuristics out of the commit CSVs.
        taken = [r for r in repos if r in done and r not in retried and done[r][1] is None
                 and not resume.time_bound_holds(done[r][9], config.clone_timeout, done[r][10],
                                                 current)]
        retried |= reuse.reclassify(config, taken, done, workers)
    pending = processing_order([r for r in repos if r not in done or r in retried])
    if finished_path:
        # A line taken over while git returned no head commit is marked `head_no_response` for
        # the retry rounds, the ceiling and the next run. It loses the mark once git returns the
        # same head commit. A head that moved or a refusal makes the repository a retry, which
        # writes its own line. The line keeps the collection that collected the repository.
        for r, state in queried.items():
            if r not in retried and (state == reuse.NO_RESPONSE) != done[r][8]:
                resume.append_finished(finished_path, r, *done[r][:4], redo=done[r][4],
                                       size_bound=done[r][5], collected_under=done[r][6],
                                       head_no_response=state == reuse.NO_RESPONSE,
                                       time_bound=done[r][9], collection=done[r][10])
    config.clone_dir.mkdir(parents=True, exist_ok=True)
    # Read before any retry starts, because a retry removes the per-repository commit record
    # that it replaces.
    reread = history_to_read_again(config, pending, done)
    new = reuse.current_path_heuristics(config.vendored_dirs) if under is not None else None

    def preserved_on(repo: str) -> frozenset:
        """The outcomes of this run's clone of `repo` after which the step keeps the
        repository's earlier collection rather than remove it."""
        line = done.get(repo)
        # The step keeps only a complete collection of an ok line, and only if the path
        # heuristics of this run would collect the same files from the repository. A line still
        # marked `retrying` belongs to a retry that never recorded its end. That retry removed
        # the earlier shards and tree before it wrote its own, and keeping what is on disk would
        # publish the new tree under the earlier row. A collection kept under other path
        # heuristics would publish the repository without the files that this run's path
        # heuristics match. The import heuristics do not affect the decision. An imported tree
        # that predates them or that other import heuristics wrote leaves the collection of the
        # configuration artifacts unchanged, and the import detection step reads the imports
        # from what the imported tree contains.
        if (line is None or line[1] is not None or line[7]
                or resume.tree_missing(config, repo, line[0])
                or reuse.heuristics_reach(config, repo, line[6], under, new, imports_too=False)):
            return frozenset()
        if repo not in reread:
            return frozenset({"clone_failed", clone.TIME_BOUND_EXCEEDED})
        # A collection with a record that requires reading the commit history again is kept with
        # `redo` after a failed clone. It is not kept after a clone that the time bound stopped,
        # because the bound would then hold for the rest of the collection and nothing would
        # read the history. A collection without its commit log is never kept, because the step
        # exits 4 while such a collection remains, whatever the ceiling allows.
        if resume.commit_log_missing(config.commits_dir, repo):
            return frozenset()
        return frozenset({"clone_failed"})

    def start(repo: str) -> frozenset:
        """Write the line of a retry of an ok line, and return what `preserved_on` returns, right
        before the repository starts."""
        keep = preserved_on(repo)
        # Every retry of an ok line has its line in the list of finished repositories before it
        # starts. Otherwise a Stop after its clone and before its new line would leave the old
        # line in force for the new tree, with the per-repository commit record gone, and the
        # next Start would count the repository as done. The line is marked `retrying` and keeps
        # its `collected_under`. The step writes it for one repository at a time rather than for
        # every pending repository at the start of the run. A stop before a retry starts, such as
        # for low free space, then leaves no `retrying` line behind. Such a line would prevent
        # the next run from keeping the earlier collection.
        if marked(repo):
            resume.append_finished(finished_path, repo, *done[repo][:4], redo=True,
                                   collected_under=done[repo][6], retrying=True,
                                   collection=current)
        return keep

    def marked(repo: str) -> bool:
        """Whether `start` marks the line of `repo` `retrying`: a retry of an ok line that is
        not marked yet."""
        return bool(finished_path and repo in retried and done[repo][1] is None
                    and not done[repo][7])

    def not_started(repo: str) -> None:
        """Write again the line that `start` replaced with a `retrying` line, for a retry that
        no worker took, so that the next run keeps its earlier collection after a failed
        clone."""
        if marked(repo):
            line = done[repo]
            resume.append_finished(finished_path, repo, *line[:4], redo=line[4],
                                   size_bound=line[5], collected_under=line[6],
                                   head_no_response=line[8], time_bound=line[9],
                                   collection=line[10])

    def free_if_low():
        """The free bytes when they are under the minimum, else None."""
        if config.min_free_bytes <= 0:
            return None
        free = guards.free_bytes(config.clone_dir)
        return free if free < config.min_free_bytes else None

    low = free_if_low()
    started = finished = 0
    # Two counts: clones that failed in a row, and collector errors among the recently
    # finished repositories.
    streak, recent, stop_on_failures, broken = 0, collections.deque(maxlen=guards.ERROR_WINDOW), False, False
    stop_reason = None

    if done:
        logs.info('repository_cloning', f"{finished_path} records "
                  f"{len(repos) - len(pending):,} of {len(repos):,} repositories as finished, "
                  f"with {len(pending):,} pending. Collecting {len(redone):,} repositories "
                  f"again to retry them (outcome clone_failed or collector_error, a larger "
                  f"bound, a failed history command, missing copied files, or a commit log "
                  f"that is missing or failed validation), and {len(retried - redone):,} "
                  f"because their earlier collection is out of date.")

    results: dict = dict(done)
    progress = guards.Progress(len(repos), len(repos) - len(pending),
                        config.progress_every, config.progress_seconds)

    def record(repo: str, main_row: str, fail_row: str | None, facts: list,
               submodules: list, measures: dict | None = None) -> None:
        # A repository is appended only after the worker returned, when its tree of
        # configuration artifacts is fully written. A torn or missing line therefore costs at
        # most a redo of the repository, never a half-collected repository.
        nonlocal streak, stop_on_failures, stop_reason
        outcome = resume.outcome_of(fail_row)
        preserved = bool(measures and measures.get("preserved_previous"))
        collected_under = under
        if preserved:
            main_row, fail_row, facts, submodules = done[repo][:4]
            collected_under = done[repo][6]
        results[repo] = (main_row, fail_row, facts, submodules)
        # An earlier collection kept after a retry that failed on the network is marked to redo
        # for the rounds and the next run. After a retry that the time bound stopped, the line
        # records the bound instead. The bound holds for the rest of the collection
        # (`resume.time_bound_holds`), so that no round or resumed run clones the repository
        # again under it. A retry to read a commit history again that the time bound stopped
        # keeps nothing (`preserved_on`), and its `time_bound_exceeded` line records the bound
        # without `redo`, as every such line does. Only a run under a larger bound or the next
        # collection clones the repository. A mark to redo would count it toward the ceiling on
        # every Start of the collection although no run retried it.
        if preserved:
            redo = outcome != clone.TIME_BOUND_EXCEEDED
        else:
            redo = fail_row is None and resume.history_needs_reread(
                config.commits_dir, repo, config.clone_timeout, config.max_clone_bytes)
        if finished_path:
            resume.append_finished(finished_path, repo, main_row, fail_row, facts, submodules, redo,
                                   config.max_clone_bytes
                                   if outcome == clone.SIZE_BOUND_EXCEEDED else None, measures,
                                   collected_under,
                                   time_bound=(config.clone_timeout
                                               if outcome == clone.TIME_BOUND_EXCEEDED else None),
                                   collection=current)
        progress.note(outcome != "ok")
        # Clones that exceed the time bound count toward the connectivity probe as failed clones
        # do, because a network that stalls transfers ends clones at the time bound rather than
        # with an error.
        streak = streak + 1 if outcome in ("clone_failed", clone.TIME_BOUND_EXCEEDED) else 0
        recent.append(outcome)
        errors = recent.count(resume.COLLECTOR_ERROR)
        limit = config.max_consecutive_failures
        if limit and errors >= limit and not stop_on_failures:
            # Collector errors come from a defect, not from the network. The tracebacks are in
            # the log, and a Start after the fix redoes the repositories.
            stop_on_failures, stop_reason = True, (resume.COLLECTOR_ERROR, errors)
        if limit and streak >= limit and not stop_on_failures:
            # The step tests whether the network works rather than inferring it from the
            # failures. On a healthy network, the repositories of one owner that GitHub closed,
            # with a message that the permanent-failure patterns do not match, fail one after
            # the other in the same way. A stop there would need a Start by hand for nothing.
            # After a failed probe, the step waits and probes again (`guards.PROBE_WAITS`), so
            # that an outage that passes does not stop the run. No clone starts during a wait,
            # because this function runs before the next submission. A stop during a wait ends
            # the step through its handler (`stop_pool`, `guards.worker_stop`). Once the step is
            # stopping, it does not probe again, because every failure among the clones in
            # flight keeps the count over the limit and each probe can take the probe's timeout.
            waits = list(config.probe_waits)
            while (not (responds := guards.remote_responds(config.probe_url, config.token,
                                                            config.use_ssh))
                   and waits):
                wait = waits.pop(0)
                logs.warning('repository_cloning', f"{streak:,} clones failed in a row, and git "
                             f"could not list a ref of {config.probe_url}. Paused the clones. "
                             f"Probing again in {logs.hms(wait)} (wait "
                             f"{len(config.probe_waits) - len(waits):,} of "
                             f"{len(config.probe_waits):,}).")
                time.sleep(wait)
            if responds:
                logs.warning('repository_cloning', f"{streak:,} clones failed in a row for an "
                             f"unknown cause, but git could list a ref of {config.probe_url}. "
                             f"Continuing, with the failures in a row counted from zero. The "
                             f"clone logs in {config.log_dir} contain the errors.")
                streak = 0
            else:
                stop_on_failures, stop_reason = True, ("clone_failed", streak)

    if workers <= 1:
        # With one process doing the cloning, a stop has to end the git processes that this
        # process started, as a pool worker's handler does. Without the handler, the collector
        # would end under the default disposition, and the clone in flight would keep running.
        previous = {sig: signal.signal(sig, guards.worker_stop)
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        # The handlers are restored in a `finally`, as in the pool branch. Otherwise an exception
        # in the loop would leave the process with SIGINT bound to `guards.worker_stop`, which
        # calls os._exit. The step is designed to handle an exception there, such as an OSError
        # while it appends to the list of finished repositories on a full disk.
        try:
            for repo in pending:
                if low is not None or stop_on_failures:
                    break
                keep = start(repo)
                started += 1
                _, main_row, fail_row, facts, submodules, *measures = collector.process_repo(
                    config, repo, keep)
                record(repo, main_row, fail_row, facts, submodules, *measures)
                finished += 1
                if finished % config.disk_check_every == 0:
                    low = free_if_low()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    else:
        # The pool uses spawn, not the platform default (test_tech_stack checks it). Python 3.14
        # on Linux defaults to forkserver, which gives every worker the environment that its
        # server process started with. A worker would then clone with a stale GIT_CONFIG_* or
        # without a GitHub access token that the parent set later. Python on macOS defaults to
        # spawn.
        #
        # The step submits one repository per worker rather than all at once, so that low free
        # space stops the repositories that have not started and lets the started repositories
        # finish. Every submitted repository thus starts at once. `start` marks the line of a
        # retry `retrying` when the retry is submitted, and a repository queued behind the
        # workers would carry the mark although no worker had begun it. A stop or a lost worker
        # would then cost that repository its earlier collection.
        window = workers
        in_flight = {}
        # While the pool is up, a stop ends the workers and not only the parent. The handlers
        # are restored afterwards, because the tests also call this function in-process.
        previous = {sig: signal.signal(sig, stop_pool)
                    for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            with ProcessPoolExecutor(max_workers=workers,
                                     mp_context=multiprocessing.get_context("spawn")) as executor:
                def submit_more():
                    nonlocal started, broken
                    while (low is None and not stop_on_failures and not broken
                           and started < len(pending) and len(in_flight) < window):
                        repo = pending[started]
                        keep = start(repo)
                        try:
                            future = executor.submit(collector.process_repo, config, repo, keep)
                        except BrokenProcessPool:
                            # The pool detected the dead worker between one result
                            # and the next submission. The step ends as below, with this
                            # repository not started rather than recorded, and with the
                            # line that the repository had before `start` marked it
                            # `retrying`.
                            not_started(repo)
                            broken = True
                            return
                        started += 1
                        in_flight[future] = repo

                submit_more()
                while in_flight:
                    ready, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for future in ready:
                        repo = in_flight.pop(future)
                        try:
                            _, main_row, fail_row, facts, submodules, *measures = future.result()
                        except BrokenProcessPool:
                            # The kernel killed a worker, such as for lack of memory, and the
                            # pool is unusable from here. The repositories in flight, one per
                            # worker, are recorded as clone_failed, which the next run retries.
                            # The step ends as it does after failed clones in a row rather than
                            # with a traceback. A repository that was not submitted gets no new
                            # line and keeps its earlier one.
                            broken = True
                            main_row = paths.build_csv_row(repo, collector.utc_now(), "", "", "",
                                                           "", set())
                            fail_row = f"{repo},https://github.com/{repo},clone_failed"
                            facts, submodules, measures = [], [], []
                        except Exception:
                            main_row = paths.build_csv_row(repo, collector.utc_now(), "", "", "",
                                                           "", set())
                            fail_row = f"{repo},https://github.com/{repo},clone_failed"
                            facts, submodules, measures = [], [], []
                        record(repo, main_row, fail_row, facts, submodules, *measures)
                        finished += 1
                        if finished % config.disk_check_every == 0 and low is None:
                            low = free_if_low()
                    if not broken:
                        submit_more()
                    if broken:
                        for future, repo in list(in_flight.items()):
                            in_flight.pop(future)
                            record(repo, paths.build_csv_row(repo, collector.utc_now(), "", "", "",
                                                             "", set()),
                                   f"{repo},https://github.com/{repo},clone_failed", [], [])
                            finished += 1
                        break
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    main_rows = [results[r][0] for r in repos if r in results]
    fail_rows = [results[r][1] for r in repos
                 if r in results and results[r][1] is not None]
    facts_rows = [row for r in repos if r in results for row in results[r][2]]
    submodule_rows_out = [row for r in repos if r in results for row in results[r][3]]
    rows = (main_rows, fail_rows, facts_rows, submodule_rows_out)
    if low is not None and started < len(pending):
        raise guards.LowDiskStop(config.clone_dir, low, config.min_free_bytes, finished,
                          len(pending) - started, rows)
    if low is not None:
        # Measured after the last repository finished. The step is done because nothing is left
        # to start, and a stop would only keep the later steps, which clone nothing, from
        # running.
        logs.warning('repository_cloning', f"{config.clone_dir} has {low / 1024 ** 3:.1f} GiB "
                     f"free, under the {config.min_free_bytes / 1024 ** 3:.1f} GiB minimum, "
                     f"after the last repository finished. No clone was left to start.")
    if stop_on_failures:
        kind, count = stop_reason
        raise guards.ConsecutiveFailuresStop(count, finished, len(pending) - started, rows, kind)
    if broken:
        raise guards.WorkerLostStop(finished, len(pending) - started, rows)
    return rows


def collect_with_retries(config: collector.CollectionConfig, repos: list, workers: int = 5,
                         finished_path: Path | None = None, sleep=time.sleep,
                         collection: dict | None = None, head_waits: tuple = ()) -> tuple:
    """Run `run_collection`, then, after a pause, redo what a later run would redo, for
    `config.retry_rounds` rounds or until nothing is left to redo. The head check of every round
    waits `head_waits` while git returns no head commit for too many repositories
    (`reuse.remote_heads`).

    Each round runs `run_collection` again over the same list of finished repositories, as a
    Start does. A clone, a history fetch or a history query that failed during a short period of
    GitHub errors is thus collected again inside the run rather than left for an operator. What
    remains to redo after the last round counts toward the caller's ceiling. A stop that any
    round raises ends the step, as a stop in the first round does. Every round belongs to
    `collection`, the collection in progress.
    """
    rows = run_collection(config, repos, workers=workers, finished_path=finished_path,
                          collection=collection, head_waits=head_waits)
    if finished_path is None:
        return rows
    for round_ in range(1, config.retry_rounds + 1):
        finished = resume.read_finished(finished_path)
        again = (to_retry(config, repos, finished, collection["collection"] if collection else None)
                 | set(heads_without_response(repos, finished)))
        if not again:
            break
        logs.info('repository_cloning', f"Retry round {round_} of {config.retry_rounds} "
                  f"collects {len(again):,} repositories again after a pause of "
                  f"{config.retry_pause:g} s, starting with "
                  f"{processing_order(sorted(again))[0]}. Each of them had a failed clone, "
                  f"history fetch or file history query, a git process that could not start, "
                  f"or no head commit from git.")
        sleep(config.retry_pause)
        # The first pass queried the heads. The round queries again only the heads for which
        # git returned no head commit.
        rows = run_collection(config, repos, workers=workers, finished_path=finished_path,
                              check_heads=False, collection=collection, head_waits=head_waits)
    return rows


# The tree and its lists, which are at the root of the tree beside the repository
# directories. `repos.split` takes the names of the lists as the files to expect there.
COLLECTED_DIR_NAME = "data/collected"
COLLECTED_CSV_NAME = "data/collected/repos.csv"
FAIL_CSV_NAME = "data/collected/clone_failed.csv"
# The files that this step writes into the tree beside the repository directories, for every
# caller that has to tell them apart from a stray.
COLLECTED_TREE_FILES = ("repos.csv", "clone_failed.csv")
FAIL_CSV_HEADER = "repo_name,repo_url,reason"
# The list of the sample, in the classification stage's data directory. The path is therefore
# relative to the stage directory rather than a bare name. The `engineered_project_selection`
# step writes the file, which is not checked in.
DEFAULT_REPO_CSV = "../classification/repos_annotated_included.csv"
# The directory of the shards and records: <owner§repo>.csv contains the matched commits, and
# <owner§repo>.json is the per-repository commit record. The directory is in the stage
# directory, gitignored and kept across runs, so that the assembled CSV merges the way the
# caches do.
COMMITS_DIR_NAME = "collect_commits"
AI_COMMITS_CSV_NAME = "output/all_ai_commits.csv"
# The clones in flight by default.
WORKERS = 5


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    p = cli.ArgumentParser(
        description="Clone each repository in the sample, copy its configuration artifacts and "
                    "read its commit history.",
    )
    p.add_argument("--repo-csv", default=DEFAULT_REPO_CSV,
                   help="CSV of the repositories to collect, as owner/repo (default: %(default)s)")
    p.add_argument("--repo-col", default="repo_name",
                   help="Column of the repository names in --repo-csv (default: %(default)s)")
    # `--collected-dir` and `--collected-csv` rather than `--output-dir`, which means
    # `output/` in `agentcontext/steps/file_history.py`.
    p.add_argument("--collected-dir", default=COLLECTED_DIR_NAME,
                   help=f"Directory for the collected files, one directory per repository "
                        f"(default: {COLLECTED_DIR_NAME})")
    p.add_argument("--collected-csv", default=COLLECTED_CSV_NAME,
                   help=f"CSV of the collected repositories (default: {COLLECTED_CSV_NAME})")
    p.add_argument("--fail-csv", default=FAIL_CSV_NAME,
                   help=f"CSV of the clone failures (default: {FAIL_CSV_NAME})")
    p.add_argument("--file-history", default=None,
                   help=f"Cache to merge the Git columns of the copied files into "
                        f"(default: {caches.FILE_HISTORY_PATH.name} in the stage directory)")
    p.add_argument("--submodules", default=STAGE_DIR / caches.SUBMODULES_CSV_NAME, type=Path,
                   help=f"CSV to merge the gitlinks of the clones into "
                        f"(default: {caches.SUBMODULES_CSV_NAME} in the stage directory)")
    p.add_argument("--no-file-history", action="store_true",
                   help="Read no commit history, and leave the Git columns to the file "
                        "history step")
    p.add_argument("--ai-authorship", default=None,
                   help=f"Cache to merge the AI columns of the copied files into "
                        f"(default: {caches.AI_AUTHORSHIP_PATH.name} in the stage directory)")
    p.add_argument("--commits-dir", default=None,
                   help=f"Directory of the shards, per-repository commit records and commit "
                        f"logs (default: {COMMITS_DIR_NAME}/ in the stage directory)")
    p.add_argument("--ai-commits-csv", default=None,
                   help=f"Commit CSV to write, with the other commit CSVs beside it "
                        f"(default: {AI_COMMITS_CSV_NAME} in the stage directory)")
    p.add_argument("--no-ai-commits", action="store_true",
                   help="Read no commit history and write no commit CSV")
    p.add_argument("--finished", default=resume.FINISHED_NAME,
                   help=f"List of finished repositories, from which a run resumes "
                        f"(default: {resume.FINISHED_NAME})")
    p.add_argument("--fresh", action="store_true",
                   help="Collect every repository again, after deleting the list of finished "
                        "repositories, the shards, the clone logs, the file lists, the workflow "
                        "files, the imported files, the kept path heuristics and the collected "
                        "repositories")
    # Scratch space outside data/ on purpose. It contains a clone only while its repository is
    # processed, and no step reads it.
    p.add_argument("--clone-dir", default="repos_tmp",
                   help="Temporary directory for the clones, on the volume that --min-free-gb "
                        "checks (default: %(default)s)")
    p.add_argument("--min-free-gb", type=float, default=guards.MIN_FREE_GB,
                   help="Minimum free GiB on the volume of --clone-dir. Below it, no further "
                        "clone starts, and the step exits 3 after the running clones if "
                        "repositories remain (0 turns the check off, default: %(default)s)")
    p.add_argument("--disk-check-every", type=int, default=guards.DISK_CHECK_EVERY,
                   help="Finished repositories between two measurements of the free space "
                        "(default: %(default)s)")
    p.add_argument("--progress-every", type=int, default=guards.PROGRESS_EVERY,
                   help="Log a progress line after this many finished repositories "
                        "(default: %(default)s)")
    p.add_argument("--progress-seconds", type=float, default=guards.PROGRESS_SECONDS,
                   help="Also log a progress line when a repository finishes this many "
                        "seconds after the last one (default: %(default)s)")
    p.add_argument("--max-consecutive-failures", type=int, default=guards.MAX_CONSECUTIVE_FAILURES,
                   help="Clones in a row that fail or exceed the time bound before the "
                        "connectivity probe of --probe-url. Exit 4 if every probe fails "
                        "(0 turns the check off, default: %(default)s)")
    p.add_argument("--probe-url", default=guards.PROBE_URL,
                   help="Repository of which the connectivity probe lists a ref "
                        "(default: %(default)s)")
    p.add_argument("--max-clone-failures", type=int, default=None,
                   help="Exit 4 when more repositories than this remain to retry after the "
                        "retry rounds, such as after clone_failed or collector_error (0 turns "
                        "the check off, default: "
                        f"{guards.CLONE_FAILURE_SHARE * 100:.2f}%% of the input list, "
                        f"at least 1)")
    p.add_argument("--retry-rounds", type=int, default=guards.RETRY_ROUNDS,
                   help="Rounds in which the repositories left to retry are collected again "
                        "before the step ends (0 leaves them to a later run, "
                        "default: %(default)s)")
    p.add_argument("--retry-pause", type=float, default=guards.RETRY_PAUSE_SECONDS,
                   help="Seconds to wait before each retry round (default: %(default)s)")
    p.add_argument("-l", "--log-dir", default="clone_logs")
    p.add_argument("--workflows-dir", default="data/workflows",
                   help="Directory for a copy of each repository's .github/workflows/*.yml, "
                        "which no step reads (default: %(default)s)")
    p.add_argument("--no-workflows", action="store_true",
                   help="Copy no workflow file")
    p.add_argument("--file-lists-dir", default="data/file_lists",
                   help="Directory for each repository's file list at the collected commit "
                        "(default: %(default)s)")
    p.add_argument("--no-file-lists", action="store_true",
                   help="Write no file list")
    p.add_argument("--imported-dir", default="data/imported",
                   help="Directory of the imported tree, the files that the imports of the "
                        "copied configuration artifacts reach, one directory per repository "
                        "(default: %(default)s)")
    p.add_argument("-w", "--workers", type=int, default=WORKERS)
    p.add_argument("--use-ssh", action="store_true",
                   help="Clone over SSH instead of HTTPS")
    p.add_argument("--max-tries", type=int, default=6)
    p.add_argument("--max-clone-mb", type=int,
                   default=bounds.DEFAULT_MAX_BYTES // (1024 * 1024),
                   help="Size bound of a clone's directory in MiB. A clone over it is recorded "
                        "as size_bound_exceeded (0 turns the bound off, default: %(default)s)")
    p.add_argument("--clone-timeout", type=float, default=history.GIT_TIMEOUT_SECONDS,
                   help="Time bound of each clone attempt and history fetch in seconds. A "
                        f"repository with {clone.TIMEOUT_TRIES} attempts over it is recorded as "
                        f"{clone.TIME_BOUND_EXCEEDED} or keeps its earlier collection "
                        "(default: %(default)s)")
    p.add_argument("--print-matches", action="store_true",
                   help="Log every clone and every copied file")
    return p


def _write_csv(path: Path, header: str, rows: list[str]) -> None:
    """Write a CSV file of the header line and each preformatted row.

    The writer that the caches use replaces the file atomically. The step writes
    `data/collected/repos.csv` and `clone_failed.csv` with this function. Both files are
    tracked, and the steps after this one read them. A kill between the truncation and the
    flush would leave a `data/collected/repos.csv` that the collected file cleanup reads as a
    list with no columns, and a `clone_failed.csv` with a header that the cleanup's check does
    not recognize.
    """
    caches.replace_file(path, header + "\n" + "".join(row + "\n" for row in rows))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code."""
    args = _build_arg_parser().parse_args(argv)

    for binary in ("git", "rsync"):
        if shutil.which(binary) is None:
            logs.error('repository_cloning', f"Could not find the required program {binary} "
                       f"on PATH.")
            return 1
    # Checked before anything is removed, opened or decided. Every line of the list of finished
    # repositories records the version of git that collected the repository, and without this
    # run's version the step cannot tell which repositories it may take over (`reuse.current`).
    try:
        clone.git_version()
    except clone.GitVersionUnavailable as error:
        logs.error('repository_cloning', f"{error}. Stopped before collecting any repository, "
                   f"because the version of git decides which repositories are taken over. "
                   f"Start the step again once git runs.")
        return 1

    repo_csv = Path(args.repo_csv)
    if not repo_csv.is_file():
        logs.error('repository_cloning', f"Could not find the input CSV {repo_csv}.")
        return 1

    # The list by which both the collector and the validator prune. It needs no check here,
    # because `heuristics` loads it at import, and a missing file raises there before this
    # function runs.
    vendored_path = heuristics.VENDORED_DIRS_PATH

    try:
        repos = parse_repo_csv(repo_csv, args.repo_col)
    except SystemExit:
        return 1

    workers = max(1, args.workers)
    ai_commits_csv = (Path(args.ai_commits_csv).resolve() if args.ai_commits_csv
                      else STAGE_DIR / AI_COMMITS_CSV_NAME)
    commits_dir = (None if args.no_ai_commits or args.no_file_history
                   else Path(args.commits_dir).resolve() if args.commits_dir
                   else STAGE_DIR / COMMITS_DIR_NAME)
    config = collector.CollectionConfig(
        collected_dir=Path(args.collected_dir).resolve(),
        clone_dir=Path(args.clone_dir).resolve(),
        log_dir=Path(args.log_dir).resolve(),
        workflows_dir=None if args.no_workflows else Path(args.workflows_dir).resolve(),
        file_lists_dir=None if args.no_file_lists else Path(args.file_lists_dir).resolve(),
        imported_dir=Path(args.imported_dir).resolve(),
        vendored_dirs=heuristics.load_vendored_dirs(vendored_path),
        use_ssh=args.use_ssh,
        token=os.environ.get("GITHUB_TOKEN") or None,
        max_tries=args.max_tries,
        print_matches=args.print_matches,
        max_clone_bytes=args.max_clone_mb * 1024 * 1024,
        clone_timeout=args.clone_timeout,
        min_free_bytes=int(args.min_free_gb * 1024 ** 3),
        disk_check_every=max(1, args.disk_check_every),
        progress_every=max(1, args.progress_every),
        progress_seconds=args.progress_seconds,
        max_consecutive_failures=max(0, args.max_consecutive_failures),
        max_clone_failures=(None if args.max_clone_failures is None
                            else max(0, args.max_clone_failures)),
        retry_rounds=max(0, args.retry_rounds),
        retry_pause=max(0.0, args.retry_pause),
        probe_url=args.probe_url,
        probe_waits=guards.PROBE_WAITS,
        measure_git=not args.no_file_history,
        commits_dir=commits_dir,
        heuristics_dir=Path(args.finished).resolve().parent / reuse.HEURISTICS_DIR_NAME,
        # Where the commit reclassification writes it, beside the commit CSV.
        reclassification_report=(ai_commits_csv.parent / resume.RECLASSIFICATION_REPORT_NAME
                                 if commits_dir is not None else None),
    )
    ai_authorship_path = (Path(args.ai_authorship).resolve() if args.ai_authorship
                          else caches.AI_AUTHORSHIP_PATH)

    collected_csv = Path(args.collected_csv).resolve()
    fail_csv = Path(args.fail_csv).resolve()
    # The cache is at the fixed path that every reader uses, not beside the list. The list is in
    # the tree that it lists, and a cache path derived from the list's parent would be in that
    # tree too, where `agentcontext/steps/detection.py` and `caches.py` do not read it. The
    # detection step would then report every row as missing from the cache and publish blank
    # Git columns while exiting 0, and the file history step would clone the sample again to
    # measure them. A test run keeps its fixtures out of the tracked file with `--file-history`.
    file_history_path = (Path(args.file_history).resolve() if args.file_history
                         else caches.FILE_HISTORY_PATH)
    finished_path = Path(args.finished).resolve()

    if args.fresh:
        removed = resume.fresh_start(finished_path, config)
        logs.warning('repository_cloning', f"--fresh removed {', '.join(removed)}." if removed
                     else "--fresh found nothing to remove.")

    config.collected_dir.mkdir(parents=True, exist_ok=True)
    # Created even when no import reaches a file, so that the import detection step can tell a
    # collection without imported files from a collection that predates the imported tree.
    config.imported_dir.mkdir(parents=True, exist_ok=True)
    finished_path.parent.mkdir(parents=True, exist_ok=True)
    logs.info('repository_cloning', f"{repo_csv} lists {len(repos):,} repositories to collect.")
    logs.info('repository_cloning', f"Collecting with {workers:,} workers.")

    # The collection that this run belongs to. It is the collection in progress over the list,
    # the last collection that ended over the list, the collection that the list continues
    # because it lacks only repositories that GitHub no longer serves, or a new collection that
    # this run starts and that ends when the step exits 0.
    collection = resume.open_collection(finished_path, repos, labeling_gone(repo_csv))
    stopped = None
    try:
        main_rows, fail_rows, facts_rows, sub_rows = collect_with_retries(
            config, repos, workers=workers, finished_path=finished_path, collection=collection,
            head_waits=reuse.HEAD_WAITS)
    except (guards.LowDiskStop, guards.ConsecutiveFailuresStop, guards.WorkerLostStop) as stop:
        # The step writes the rows that this run reached, so that the lists on disk match the
        # list of finished repositories. The exit code reports that the step is not complete.
        stopped = stop
        main_rows, fail_rows, facts_rows, sub_rows = stop.rows
    except clone.GitVersionUnavailable as error:
        # Raised where a round begins (`reuse.current`), before the round decides anything. The
        # lines of the rounds before it stay, and the next run resumes the collection from them.
        logs.error('repository_cloning', f"{error}. Stopped the collection, because the version "
                   f"of git decides which repositories are taken over. Start the step again "
                   f"once git runs.")
        return 1
    else:
        # Removes the files of a repository that left the input list. The collected file cleanup
        # would otherwise report them on every run as a directory without a row.
        reuse.remove_left(config, repos, resume.read_finished(finished_path), finished_path)

    _write_csv(collected_csv, paths.COLLECTED_CSV_HEADER, main_rows)
    _write_csv(fail_csv, FAIL_CSV_HEADER, fail_rows)
    if config.measure_git:
        caches.write_file_history(file_history_path, facts_rows)
    wanted = set(repos)
    finished = {r: v for r, v in resume.read_finished(finished_path).items() if r in wanted}
    # The cache submodules.csv is in the stage directory, where
    # `collected_file_cleanup.DEFAULT_SUBMODULES` reads it. A path derived from the list's
    # parent would be inside the collected tree. The validator would then find no gitlinks, log
    # that it drops no file, and publish another repository's files as this repository's
    # configuration. The cache is written before the commit CSV, so that a failed assembly
    # leaves every list that a later step reads.
    #
    # The step passes the repositories that this run covered, not the whole input list.
    # `write_submodules` removes the rows of the repositories that it is given and writes back
    # only the rows that it is handed. With the input list, a stopped run would delete the rows
    # of every repository that it had not reached, including the repositories with clones that
    # are gone.
    caches.write_submodules(args.submodules, sub_rows, list(finished))
    unfetched, read_timed_out, missing_logs = [], [], []
    if config.commits_dir is not None:
        try:
            counts = commits.assemble_ai_commits(config.commits_dir, finished, ai_commits_csv,
                                         ai_authorship_path if config.measure_git else None,
                                         repos=repos)
        except Exception:
            logs.error('repository_cloning', f"Could not assemble the commit CSV from "
                       f"{config.commits_dir}. The lists and the caches were written before "
                       f"the failure.\n{traceback.format_exc()}")
            return 1
        unfetched, read_timed_out = counts["unfetched"], counts["read_timed_out"]
        missing_logs = counts["missing_logs"]

    transient = sum(1 for row in fail_rows if resume.outcome_of(row) in resume.TRANSIENT_OUTCOMES)
    logs.info('repository_cloning', f"{fail_csv} records {len(fail_rows):,} clone failures. A "
              f"later run retries the {transient:,} of them with outcome "
              f"{' or '.join(resume.TRANSIENT_OUTCOMES)}.")
    # A commit history that git could not start to read, a history fetch or a file's history
    # query that failed on the network, and a tree that is gone each leave an ok line with
    # `redo`, which a later run retries. The step counts these repositories with the fail rows.
    # Otherwise the run would exit 0, the runner would record the step as complete, and every
    # later Start would skip the step.
    redo = [r for r in repos if r in finished and finished[r][4]]
    if redo:
        logs.info('repository_cloning', f"After the retry rounds, {len(redo):,} repositories "
                  f"remain to collect again, such as {redo[0]}. Git could not start, a history "
                  f"command failed, or copied files or a commit log are missing (details in "
                  f"{config.log_dir} and {finished_path}). A resumed collection collects them "
                  f"again.")
        transient += len(redo)
    # A repository without a head commit is taken over with its earlier collection, and a
    # resumed collection queries it again. The step counts these repositories with the fail
    # rows, so that a run during which GitHub returned few heads is not recorded as a
    # collection of the day on which it ran.
    without_response = heads_without_response(repos, finished)
    if without_response:
        logs.info('repository_cloning', f"No head commit from git for "
                  f"{len(without_response):,} repositories, such as {without_response[0]}, after "
                  f"the retry rounds. Kept their earlier collection. A resumed collection "
                  f"queries their heads again.")
        transient += len(without_response)
    if unfetched:
        logs.info('repository_cloning', f"Could not fetch the commit histories of "
                  f"{len(unfetched):,} repositories, such as {unfetched[0]}, within "
                  f"--clone-timeout or --max-clone-mb. Their commit reclassification stays "
                  f"incomplete, and "
                  f"the file history step can measure their files again. Run repository cloning "
                  f"again with a larger --clone-timeout or --max-clone-mb to read the histories.")
    time_bound = [resume.outcome_of(row) == clone.TIME_BOUND_EXCEEDED for row in fail_rows]
    if any(time_bound):
        first = fail_rows[time_bound.index(True)].split(",", 1)[0]
        logs.info('repository_cloning', f"The clones of {sum(time_bound):,} repositories, such as "
                  f"{first}, exceeded --clone-timeout ({config.clone_timeout:g} s) "
                  f"{clone.TIMEOUT_TRIES:,} times. Recorded them as "
                  f"{clone.TIME_BOUND_EXCEEDED}. Only a run with a larger --clone-timeout or the "
                  f"next collection clones them again, not a resumed run under the same bound.")
    kept = [r for r in repos if r in finished and finished[r][1] is None
            and finished[r][9] is not None]
    if kept:
        logs.info('repository_cloning', f"The retries of {len(kept):,} repositories, such as "
                  f"{kept[0]}, exceeded --clone-timeout ({config.clone_timeout:g} s) "
                  f"{clone.TIMEOUT_TRIES:,} times. Kept their earlier collection. Only a run "
                  f"with a larger --clone-timeout or the next collection clones them again.")
    if read_timed_out:
        logs.info('repository_cloning', f"Reading the commit history timed out for "
                  f"{len(read_timed_out):,} repositories, such as {read_timed_out[0]}. Measured "
                  f"their file history and kept no complete commit log. Run the collection "
                  f"again with a larger --clone-timeout before you state that the commit "
                  f"attribution is complete.")
    logs.info('repository_cloning', f"Wrote the list of collected repositories to {collected_csv}.")
    if isinstance(stopped, guards.LowDiskStop):
        logs.warning('repository_cloning', f"{stopped}. {finished_path} records the finished "
                     f"repositories. Free disk space on the volume of {config.clone_dir}, then "
                     f"resume the collection.")
        return 3
    if isinstance(stopped, guards.WorkerLostStop):
        logs.warning('repository_cloning', f"{stopped}. The cause is unknown. Recorded the "
                     f"repositories in flight as clone_failed in {fail_csv}, which a resumed "
                     f"collection retries. Check the memory and process limits of the container, "
                     f"and lower the number of workers with -w if a limit caused the exit.")
        return 4
    if (isinstance(stopped, guards.ConsecutiveFailuresStop)
            and stopped.kind == resume.COLLECTOR_ERROR):
        logs.warning('repository_cloning', f"{stopped}. Stopped the collection. Fix the cause "
                     f"that the tracebacks above and in {config.log_dir} show. A resumed "
                     f"collection retries these repositories.")
        return 4
    if isinstance(stopped, guards.ConsecutiveFailuresStop):
        logs.warning('repository_cloning', f"{stopped}. Git could not list a ref of "
                     f"{config.probe_url} either. Check the network, the authentication and the "
                     f"process limits against the git errors in {config.log_dir}, then resume "
                     f"the collection.")
        return 4
    if missing_logs:
        first_log = commits.stored_log_path(config.commits_dir, missing_logs[0])
        logs.warning('repository_cloning', f"The commit logs of {len(missing_logs):,} "
                     f"repositories are missing, such as {first_log}. Exiting 4 with the "
                     f"collection incomplete, whatever --max-clone-failures allows. Fix the "
                     f"storage error that the logs in {config.log_dir} record, then resume the "
                     f"collection to collect these repositories again.")
        return 4
    ceiling = guards.failure_ceiling(config.max_clone_failures, len(repos))
    if ceiling and transient > ceiling:
        logs.warning('repository_cloning', f"{transient:,} repositories remain to retry, more "
                     f"than the {ceiling:,} that --max-clone-failures allows for "
                     f"{len(repos):,} repositories. Exiting 4 with the collection incomplete "
                     f"(details in {fail_csv}, {finished_path} and {config.log_dir}).")
        return 4
    # The commit CSVs reach the disk before the collection ends and the step exits 0.
    # `commits.assemble_ai_commits` writes each of them into the directory of `ai_commits_csv` by
    # a rename without a sync. After a power loss, the runner records the step from its record
    # of the exit (`runner.record_exited_step`), and the CSVs that the detection reads would then
    # be missing from a collection that ended. `caches.replace_file` has already
    # synced the lists and the caches. The step syncs the files of that directory and not the
    # file system that holds them, because a sync of the file system waits for every write that
    # is pending on the volume, the writes of other programs included.
    caches.sync_tree(ai_commits_csv.parent)
    # The collection is complete, and its record stays as the record of the last collection
    # that ended, which the next run over the same list continues. A new collection, which
    # queries the head of every repository that it takes over, begins over another list or
    # after --from repository_cloning.
    resume.close_collection(finished_path)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
