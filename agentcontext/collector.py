"""The collector: what the cloning step does with one repository, and the configuration of a run.

`process_repo` clones a repository and copies its configuration artifacts into the collected
tree. It records the repository's submodules, file list and workflow files, reads the commit
history of every copied file (`measure_collected_files`) and of the repository, and deletes the
clone. It returns the repository's rows, which the cloning step
(`agentcontext/steps/repository_cloning.py`) appends to the list of finished repositories.
`CollectionConfig` is the configuration that every worker shares.

This module and the modules that it imports, without the modules that only schedule a run
(`reuse.SCHEDULING_MODULES`) and the files of the packages (`reuse.PACKAGE_FILES`), are the
collector's code. `reuse` records the fingerprint of this code with every collected repository
(`reuse.collector_fingerprint`), and a later run takes a repository over only while the code is
unchanged or differs by changes that do not reach the repository (`code_versions.COLLECTOR`). A
change of this code therefore needs a new version with its reach in `agentcontext/code_versions.py`.
The cloning step decides which repositories a run collects, in
which order and when it stops. It is outside the collector's code, so that a fix to a stop
condition or to the resume of a run does not make the next run collect the sample again.
"""

import shutil
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agentcontext import caches, commits, guards, imports, logs, paths, resume
from agentcontext.git import auth, bounds, clone, history
from agentcontext.repos import repo_name_to_dir


def utc_now() -> str:
    """Current UTC time as an ISO-8601 'Z' string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class CollectionConfig:
    """Run configuration that every worker shares."""
    collected_dir: Path
    clone_dir: Path
    log_dir: Path
    # Where a repository's .github/workflows/*.yml are copied, beside the collection rather
    # than into it, for a person to read afterwards. None copies none, the default of every
    # test and of the subsample run.
    workflows_dir: Path | None = None
    # Where each repository's file list at the collected commit is written, beside the
    # collection as the workflow files are, so that a path heuristic can be counted over the
    # whole sample without cloning it again. None writes none.
    file_lists_dir: Path | None = None
    # The imported tree, `<owner§repo>/<path>` beside the collection, into which the files that
    # an import of a copied file reaches are copied (`imports.collect_imports`). The import
    # detection step reads it. None copies none.
    imported_dir: Path | None = None
    vendored_dirs: set[str] = field(default_factory=set)
    use_ssh: bool = False
    token: str | None = None
    max_tries: int = 6
    base_delay: float = 5.0
    print_matches: bool = False
    # The two bounds on one clone: bytes on disk, sampled while it runs and checked once
    # it finishes, and wall clock. See `clone.clone_with_retry`.
    max_clone_bytes: int = bounds.DEFAULT_MAX_BYTES
    clone_timeout: float = history.GIT_TIMEOUT_SECONDS
    # Free space on the volume that contains `clone_dir`, below which no further clone is
    # started. 0 turns the check off. The step measures it before the first clone and again
    # every `disk_check_every` finished repositories. The check is off here and on at the
    # command line, because the minimum is a setting for a run, `--min-free-gb`, and a
    # configuration built in code sets its own. The suites build many of these configurations
    # on CI runners with little free space, where a default minimum would stop every one of them.
    min_free_bytes: int = 0
    disk_check_every: int = guards.DISK_CHECK_EVERY
    # How often the progress line is logged (`guards.Progress`).
    progress_every: int = guards.PROGRESS_EVERY
    progress_seconds: float = guards.PROGRESS_SECONDS
    # The two failure ceilings (guards.MAX_CONSECUTIVE_FAILURES and
    # guards.clone_failure_ceiling, which `max_clone_failures` replaces when it is set). Zero
    # turns either off. `probe_url` is the repository that the step probes after failed clones
    # in a row, before it stops, and `probe_waits` are the waits after which it probes again
    # (guards.PROBE_WAITS). The waits are off here and on at the command line, as the retry
    # rounds are, and a configuration built in code sets its own.
    max_consecutive_failures: int = guards.MAX_CONSECUTIVE_FAILURES
    max_clone_failures: int | None = None
    probe_url: str = guards.PROBE_URL
    probe_waits: tuple = ()
    # Whether to read each collected file's commit history before the clone is deleted.
    # On by default, because the clone is here, the history fetch is the only extra network
    # traffic, and the alternative is a second clone of every repository that contributed a file.
    measure_git: bool = True
    # Where this step writes the shards of each repository. With None, the step reads no
    # commit history.
    commits_dir: Path | None = None
    # Where every version of the path heuristics that a repository was collected under is kept,
    # so that a later run can determine from a file list whether a change reaches the
    # repository (`reuse.affected`). None keeps none, and a change to the path heuristics then
    # makes the step collect every repository again.
    heuristics_dir: Path | None = None
    # How many times `collect_with_retries` collects again, before the step ends, what a later
    # run would redo, and how long it waits before each round (guards.RETRY_ROUNDS). Off here
    # and on at the command line, as the disk minimum is, and a configuration built in code sets
    # its own.
    retry_rounds: int = 0
    retry_pause: float = 0.0
    # The report of the last commit reclassification, beside the commit CSV, from which a run
    # collects again the repositories with a commit log that failed validation
    # (`resume.reclassification_failed`). None reads none.
    reclassification_report: Path | None = None


def measure_collected_files(repo: str, head_sha: str, clone_dir: Path,
                            out_repo_dir: Path, ai_rows: list | None = None,
                            clone_timeout: float = history.GIT_TIMEOUT_SECONDS,
                            max_clone_bytes: int | None = None,
                            failed: list | None = None,
                            measures: dict | None = None,
                            path_commits: list | None = None) -> tuple[str, list[dict], int]:
    """Read the commit history of every file that this repository contributed.

    `ai_rows`, when a list is given, receives one `ai_authorship.csv` row per copied file, with
    the four AI columns derived from the same history query as the git columns. `failed`, when
    a list is given, receives the path of every file with a failed history query. The history
    fetch runs under `clone_timeout` and, with `max_clone_bytes`, under the clone's size bound.
    `measures`, when a dict is given, receives how long the history fetch took and how large the
    clone is after it, `history_seconds` and `history_bytes`. `path_commits`, when a list is
    given, receives the commits of each file's history by hash (`commits.path_commits_entry`),
    from which the commit_reclassification step derives the AI columns again when the commit
    heuristics change.

    The history is read here because the clone is on disk and about to be deleted. The clone is
    `--depth 1` and has no history until `ensure_full_history` fetches it. That fetch is the
    only network traffic that the reading adds, and it runs once, for the repositories that
    contributed a file, on a clone that is already there.

    Returns (first_commit_sha, rows, copied). The rows are `file_history.csv` rows with the
    mechanism left blank, because the detection step decides which CSV a path is published
    under and the history does not depend on it. `copied` is how many files the repository
    contributed. When there are such files and their history could not be fetched, the function
    raises `history.HistoryUnavailable` with the cause, `time_bound`, `size_bound` or `exit`.
    The files then get no history row rather than a history read from the single commit of a
    shallow clone, and the file history step clones the repository for them. When git cannot be
    started, the function raises `MachineFailure`.
    """
    collected = paths.tree_files(out_repo_dir)
    if not collected:
        return "", [], 0

    extractor = history.GitFileMetadata(max_clone_bytes=max_clone_bytes)
    started = time.monotonic()
    fetched = extractor.ensure_full_history(str(clone_dir), timeout=clone_timeout,
                                            bounded_path=str(clone_dir))
    if measures is not None:
        measures["history_seconds"] = round(time.monotonic() - started, 1)
        measures["history_bytes"] = bounds.directory_size(clone_dir)
    if not fetched:
        cause = extractor.history_failure or "exit"
        reason = {"time_bound": f"The fetch exceeded --clone-timeout ({clone_timeout:g}s)",
                  "size_bound": "The clone exceeded --max-clone-mb during the fetch",
                  "exit": "A Git command failed"}[cause]
        logs.warning('repository_cloning', f"Could not fetch the commit history of {repo} into "
                     f"{clone_dir} (see the WARNING history line above). {reason}. Wrote no "
                     f"rows of file_history.csv or ai_authorship.csv for its "
                     f"{len(collected):,} copied files, which the file history step can "
                     f"measure again.")
        raise history.HistoryUnavailable(extractor.history_failure or "exit",
                                               f"no history for {len(collected)} files")

    machine = extractor.machine_failures
    first_commit_sha = extractor.get_root_commit_sha(str(clone_dir)) or ""
    if extractor.machine_failures != machine:
        raise history.MachineFailure(f"Could not start git to read the root commit of "
                                     f"{repo}")
    rows = []
    for artifact in collected:
        before = history._failures(extractor)
        path_history = history.read_artifact_history(extractor, str(clone_dir), artifact)
        facts = history.history_from(path_history)
        if path_history is None or history._failures(extractor) != before:
            # A history query raised or exited non-zero rather than returning a history. Every
            # such query returns a falsy default, which looks the same as a file without
            # history, and the row is therefore left out of both caches. This step collects the
            # repository again before it ends (`collect_with_retries`). When that collection
            # fails too, the file history step clones the repository for its Git and AI
            # columns, and a later run redoes the collection.
            logs.warning('repository_cloning', f"The commit history query of {repo} for "
                         f"{artifact} in {clone_dir} failed (see the WARNING history line "
                         f"above). Wrote no row of file_history.csv or ai_authorship.csv for the "
                         f"file. The retry rounds collect the repository again.")
            if failed is not None:
                failed.append(artifact)
            continue
        key = caches.FileHistoryKey(repo, head_sha, "", artifact)
        rows.append(dict(key.as_row(), **facts.as_row()))
        if ai_rows is not None and path_history is not None:
            ai_key = caches.AiAuthorshipKey(repo, head_sha, artifact)
            ai_rows.append(dict(ai_key.as_row(),
                                **commits.ai_authorship_from(path_history).as_row()))
        if path_commits is not None and path_history is not None:
            path_commits.append(commits.path_commits_entry(artifact, path_history))
    if extractor.machine_failures != machine:
        # Every query above that could not start git left its file out. The step raises a
        # machine failure, which the worker records as `collector_error` and the next run
        # redoes, rather than record a repository measured in part as done.
        raise history.MachineFailure(
            f"Could not start git for {extractor.machine_failures - machine} of the "
            f"{len(collected)} files of {repo}")
    return first_commit_sha, rows, len(collected)


def process_repo(
    config: CollectionConfig, repo: str, preserve_on: frozenset = frozenset(),
) -> tuple[str, str, str | None, list, list, dict]:
    """Clone, collect, measure, and clean up one repository.

    `preserve_on` holds the outcomes of the clone, among `clone_failed` and
    `time_bound_exceeded`, after which the step keeps the repository's earlier collection rather
    than removing it. The cloning step's `run_collection` sets it for a retry.

    Returns (repo, main_csv_row, fail_csv_row_or_None, file_history_rows, submodule_rows,
    measures). `measures` is what the repository cost. The list of finished repositories keeps
    it, so that the clone bounds can be set from a whole run rather than from a sample. Its keys
    are `seconds` for the whole repository, `clone_seconds` for the clone with its retries,
    `clone_bytes` for the clone on disk after it, `history_seconds` and `history_bytes` for the
    history fetch and the clone after it, `history_read_seconds` for reading the commit history,
    `entries` for the files, links and gitlinks at the collected commit, and `copied` for the
    files that this step copied. A part of the work that did not run has no key.

    Never raises. An exception after the clone is recorded as `collector_error`, with the
    traceback on the console and in the clone log, so that one failing repository cannot end
    the pool and a run after the fix redoes it.
    """
    guards.install_worker_stop()
    dir_name = repo_name_to_dir(repo)
    tmp_dir = config.clone_dir / dir_name
    out_repo_dir = config.collected_dir / dir_name
    log_path = config.log_dir / f"{dir_name}.log"
    canonical_url = f"https://github.com/{repo}"

    # Read once, before the clone, so that the three paths out of this function all
    # record the same scan time for this repository. It is the only value here
    # that is known after a failed clone.
    scanned_at = utc_now()
    started = time.monotonic()
    measures: dict = {}

    def measured() -> dict:
        return {"seconds": round(time.monotonic() - started, 1), **measures}

    try:
        config.clone_dir.mkdir(parents=True, exist_ok=True)
        config.collected_dir.mkdir(parents=True, exist_ok=True)
        config.log_dir.mkdir(parents=True, exist_ok=True)

        if config.print_matches:
            logs.info('repository_cloning', f"Cloning {repo}.")

        status = clone.clone_with_retry(
            repo, tmp_dir, log_path,
            use_ssh=config.use_ssh, token=config.token,
            max_tries=config.max_tries, base_delay=config.base_delay,
            max_clone_bytes=config.max_clone_bytes, clone_timeout=config.clone_timeout,
        )
        measures["clone_seconds"] = round(time.monotonic() - started, 1)
        # A retry that failed on the network or that the time bound stopped leaves the earlier
        # collection usable, a snapshot of the repository at the head that its row records.
        # Removing it would cost the repository its tree, shards, file list and workflow files
        # because the network or a transfer was too slow on one day. A repository from which
        # the earlier collection copied nothing keeps its row and file list the same way.
        preserved = status in preserve_on
        if not preserved:
            commits.remove_shards(config.commits_dir, repo)

        if status != "ok":
            shutil.rmtree(tmp_dir, ignore_errors=True)
            # Keep a previous successful collection when a retry cannot clone. Other failures
            # remove any incomplete collection.
            if preserved:
                measures["preserved_previous"] = True
                logs.warning('repository_cloning', f"The retry of {repo} ended in outcome "
                                                   f"{status}. Kept the earlier collection.")
            else:
                shutil.rmtree(out_repo_dir, ignore_errors=True)
                _remove_file_list(config, dir_name)
                _remove_workflows(config, dir_name)
                _remove_imported(config, dir_name)
                _sync_collection(config, dir_name)
            main_row = paths.build_csv_row(repo, scanned_at, "", "", "", "", set())
            fail_row = f"{repo},{canonical_url},{status}"
            if config.print_matches:
                logs.error('repository_cloning', f"The clone of {repo} failed with outcome "
                                                 f"{status} (see {log_path}).")
            return (repo, main_row, fail_row, [], [], measured())
        measures["clone_bytes"] = bounds.directory_size(tmp_dir)

        branch, commit_date, head_sha = clone.read_snapshot_metadata(tmp_dir)

        # Drop any tree left by an earlier interrupted attempt at this repository, so that
        # a redo starts clean instead of merging onto a half-written tree.
        shutil.rmtree(out_repo_dir, ignore_errors=True)
        flags = paths.collect_repo(tmp_dir, out_repo_dir, config.vendored_dirs,
                             print_matches=config.print_matches)
        if out_repo_dir.is_dir():
            dropped = paths.remove_undecodable_names(out_repo_dir)
            if dropped:
                note = (f"Removed {len(dropped):,} "
                        f"{'path' if len(dropped) == 1 else 'paths'} of {repo} from "
                        f"{out_repo_dir}, because their names are not valid UTF-8 and cannot be "
                        f"stored in the output CSVs (" + ", ".join(dropped) + ")")
                logs.warning('repository_cloning', f"{note}.")
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"UNDECODABLE_NAMES {note}\n")
                if not paths.tree_files(out_repo_dir):
                    # No file is left, whatever directories are. The step removes the
                    # directory and clears the flags, so that the row and the tree agree and
                    # the commit history is not read over a depth-1 clone with a history that
                    # was never fetched. Otherwise the row's flags would record files that are
                    # not there, and every Start would collect the repository again for a
                    # missing tree.
                    shutil.rmtree(out_repo_dir, ignore_errors=True)
                    flags = set()

        if config.workflows_dir is not None:
            # Beside the collection, never into it, because no step reads this tree and no CSV
            # records it. A failure here is a warning, because the purpose of the run is the
            # collection, and the workflow files are a supplementary record of it.
            try:
                _remove_workflows(config, dir_name)
                kept = paths.copy_workflow_files(
                    tmp_dir, config.workflows_dir / dir_name)
                if kept and config.print_matches:
                    logs.info('repository_cloning', f"Copied {kept:,} workflow file(s) of "
                                                    f"{repo}.")
            except OSError as why:
                logs.warning('repository_cloning', f"Could not copy the GitHub workflow files "
                             f"of {repo} from {tmp_dir / '.github/workflows'} to "
                             f"{config.workflows_dir / dir_name} ({why}). "
                             f"Continuing without them.")

        # The submodules are read while the clone is still here, rather than by deleting it
        # and cloning again to query the files just copied out of it. The clone records which
        # of its directories are gitlinks, where a later step would have to query GitHub.
        # Nothing under a gitlink is this repository's configuration. The submodules are never
        # checked out, and this listing is the record of what was there.
        #
        # The same listing is the repository's file list, kept whole beside the collection, so
        # that a path heuristic added later can be counted over every repository in the sample
        # without cloning again, including the repositories from which this step copied
        # nothing.
        try:
            # `read_snapshot_metadata` returns no commit only after `git rev-list --all
            # --count` returned 0, and such a repository contains no file and has an empty list.
            # `ls-tree HEAD` fails there, and a missing list would then mean both an empty
            # repository and a file list that could not be read.
            listing = clone.tree_listing(tmp_dir) if head_sha else b""
        except (subprocess.CalledProcessError, OSError) as error:
            listing = None
            command = f"git -C {tmp_dir} ls-tree -r -z HEAD"
            if isinstance(error, subprocess.CalledProcessError):
                stderr = error.stderr or b""
                if isinstance(stderr, bytes):
                    stderr = stderr.decode("utf-8", "replace")
                detail = auth.redact(stderr).strip() or "Git returned no error text"
                failure = f"Git exited {error.returncode} ({detail})"
            else:
                failure = f"Git could not start ({error})"
            inventory = (f"the file list {_file_list_path(config, dir_name)} and "
                         if config.file_lists_dir is not None else "")
            message = (f"Could not list the committed files of {repo} with {command}. "
                       f"Continuing without {inventory}the submodule rows. {failure}.")
            logs.warning('repository_cloning', message)
            with open(log_path, "a", encoding="utf-8") as output:
                output.write(f"FILE_LIST_ERROR {message}\n")
            if config.file_lists_dir is not None:
                _remove_file_list(config, dir_name)
            submodules = []
        else:
            submodules = clone.submodule_rows(repo, tmp_dir, listing)
            measures["entries"] = listing.count(b"\0")
        if config.file_lists_dir is not None and listing is not None:
            try:
                clone.write_file_list(listing, _file_list_path(config, dir_name))
            except OSError as why:
                _remove_file_list(config, dir_name)
                logs.warning('repository_cloning', f"Could not write the file list of {repo} "
                             f"to {_file_list_path(config, dir_name)} ({why}). "
                             f"Continuing without it.")

        # This pass runs after the passes that copy the configuration artifacts, in the clone,
        # which has every file that an import can reach. The imported files go to a tree of
        # their own, so that the collected tree and every row of the collection stay what they
        # are without imports.
        # A file that cannot be written ends the repository as a collector error, as a copy
        # of the collection does, because an imported tree written in part would be published
        # as the files that the imports reach.
        if config.imported_dir is not None:
            _remove_imported(config, dir_name)
            gitlinks = [row["path"] for row in submodules]
            measures["imported"] = imports.collect_imports(
                tmp_dir, out_repo_dir, config.imported_dir / dir_name, gitlinks=gitlinks,
                vendored_dirs=config.vendored_dirs)

        first_commit_sha, facts = ("", [])
        if config.measure_git:
            ai_rows, failed, path_commits = [], [], []
            try:
                first_commit_sha, facts, copied = measure_collected_files(
                    repo, head_sha, tmp_dir, out_repo_dir, ai_rows,
                    clone_timeout=config.clone_timeout, max_clone_bytes=config.max_clone_bytes,
                    failed=failed, measures=measures, path_commits=path_commits)
                measures["copied"] = copied
            except history.HistoryUnavailable as why:
                # The files get no history row, and `ai_commits-repos.csv` records why the
                # commit history was not read rather than `no_files`. A fetch that failed on the
                # network is `transient`, and the next run fetches it again. A fetch that the
                # timeout or the size bound stopped fails again under the same bounds. The
                # per-repository commit record therefore contains the bounds, and a run under
                # larger bounds fetches the history again.
                if config.commits_dir is not None:
                    record = commits.unavailable_record(repo, head_sha, branch, "history_unavailable")
                    record["cause"] = why.cause
                    record["transient"] = why.cause == "exit"
                    record["time_bound"] = config.clone_timeout
                    record["size_bound"] = config.max_clone_bytes
                    commits.write_shard_record(commits.shard_paths(config.commits_dir, repo)[1], record)
            else:
                # The history is fetched only for a repository that contributed a file, and
                # reading the commit history needs it. The step therefore reads no commit
                # history of a repository with nothing copied. `copied` is the only condition,
                # because a second `is_shallow_repo` here would return None under a refused
                # `fork()`, and the repository's history would be neither read nor recorded as
                # not read.
                if config.commits_dir is not None and copied:
                    history_read_started = time.monotonic()
                    record = commits.read_commit_history(repo, head_sha, branch, tmp_dir, config.commits_dir,
                                          timeout=config.clone_timeout)
                    measures["history_read_seconds"] = round(time.monotonic() - history_read_started, 1)
                    record["ai_rows"] = ai_rows
                    record["path_commits"] = path_commits
                    record["collected_artifacts"] = paths.tree_files(out_repo_dir)
                    # A file with a failed history query has no AI row, and a network failure
                    # is transient. `resume.history_needs_reread` reads this list, and the next
                    # run collects the repository again, as it fetches a history again after a
                    # network failure.
                    record["history_failed"] = failed
                    commits.write_shard_record(commits.shard_paths(config.commits_dir, repo)[1], record)
        _sync_collection(config, dir_name)
        main_row = paths.build_csv_row(repo, scanned_at, branch, head_sha, commit_date,
                                 first_commit_sha, flags)

        shutil.rmtree(tmp_dir, ignore_errors=True)
        if config.print_matches:
            logs.info('repository_cloning', f"Finished {repo} with the file history of "
                                            f"{len(facts):,} file(s).")
        return (repo, main_row, None, facts, submodules, measured())
    except Exception:
        detail = traceback.format_exc()
        logs.warning('repository_cloning', f"The collection of {repo} failed in {tmp_dir} with "
                     f"outcome {resume.COLLECTOR_ERROR}, which a resumed collection retries.\n"
                     f"{detail}")
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"COLLECTOR_ERROR\n{detail}")
        except OSError:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # The copied tree is removed too. Left in place, it would give the validator a
        # directory with a list row that records nothing copied, and the repository would be
        # published with a blank branch and head_sha.
        shutil.rmtree(out_repo_dir, ignore_errors=True)
        _remove_file_list(config, dir_name)
        _remove_workflows(config, dir_name)
        _remove_imported(config, dir_name)
        commits.remove_shards(config.commits_dir, repo)
        try:
            _sync_collection(config, dir_name)
        except OSError:
            # The line records a collector error, which a resumed collection redoes whatever
            # files of the repository a power loss brings back.
            pass
        main_row = paths.build_csv_row(repo, scanned_at, "", "", "", "", set())
        fail_row = f"{repo},{canonical_url},{resume.COLLECTOR_ERROR}"
        return (repo, main_row, fail_row, [], [], measured())


def _sync_collection(config: CollectionConfig, dir_name: str) -> None:
    """Write to disk every file that this collection of `repo` wrote, before the line of the list
    of finished repositories declares the collection complete (`caches.sync_tree`). The files
    are the collected tree, the workflow files, the file list, and the imported tree with its
    list of stubs, or the removal of each where the collection removed it. `commits` syncs the
    shards, the commit log and the per-repository commit record as it writes them. This
    function syncs the directories that hold them, which record their removal.

    After a power loss, a file with data that was not on disk before the line would be taken
    over as it is, empty or older than the record that describes it. A file with a removal that
    was not on disk would come back under a line that describes a collection without it."""
    targets = [config.collected_dir / dir_name]
    if config.workflows_dir is not None:
        targets.append(config.workflows_dir / dir_name)
    if config.file_lists_dir is not None:
        targets.append(_file_list_path(config, dir_name))
    if config.imported_dir is not None:
        targets += [config.imported_dir / dir_name,
                    imports.stub_list_path(config.imported_dir / dir_name)]
    for target in targets:
        caches.sync_tree(target)
    if config.commits_dir is not None:
        for directory in (config.commits_dir, config.commits_dir / commits.OTHER_SHARDS_DIR_NAME):
            caches.sync_directory(directory)


def _remove_workflows(config: CollectionConfig, dir_name: str) -> None:
    """Remove workflow copies from an earlier collection of this repository."""
    if config.workflows_dir is not None:
        shutil.rmtree(config.workflows_dir / dir_name, ignore_errors=True)


def _remove_imported(config: CollectionConfig, dir_name: str) -> None:
    """Remove the imported files of an earlier collection of this repository."""
    if config.imported_dir is not None:
        shutil.rmtree(config.imported_dir / dir_name, ignore_errors=True)


def _file_list_path(config: CollectionConfig, dir_name: str) -> Path:
    return config.file_lists_dir / f"{dir_name}{clone.FILE_LIST_SUFFIX}"


def _remove_file_list(config: CollectionConfig, dir_name: str) -> None:
    """Remove the file list that an earlier collection left, because a repository without a
    clone in this run has no file list from this run, as it has no tree."""
    if config.file_lists_dir is not None:
        _file_list_path(config, dir_name).unlink(missing_ok=True)
