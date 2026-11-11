"""The list of finished repositories, and what a resumed run redoes.

`finished_repositories.jsonl` contains one line per finished repository, with the repository's
rows and the outcome of its clone, and one more line per retry of a finished repository,
written before the retry starts. A run reads the list and skips the repositories in it, except
the repositories that the functions here select to collect again, for one of these reasons:

- a transient outcome
- a clone stopped by a size bound that this run exceeds
- a clone stopped by a time bound that this run exceeds or that an earlier collection recorded
- a commit history that a machine failure kept the cloning step from reading
- a history that did not arrive under bounds that this run exceeds
- a commit history read in the current format without its retained commit log
- a per-repository commit record that does not parse
- a commit log that the commit reclassification found failed (`reclassification_failed`)
- a file with a failed history query
- a collected directory that is gone while the line records that files were copied

Each line names the collection that wrote it. The record of the collection in progress
(`open_collection`) lets a resumed run of that collection take over its own lines and the lines
that it took over without querying their heads again. A run over the collection's list without
repositories that GitHub no longer serves continues the collection. After the collection ended,
a run over its list or over such a list continues it from the record that the end keeps, until
`--from repository_cloning` marks the collection as replaced for a new snapshot. `--fresh`
removes what a collection leaves.

A collection appends a line for every repository in the sample, and a list that only grew would
make every later collection read the lines of all earlier ones. When a collection begins,
`reduce_finished` therefore keeps only the lines that the readers of the list use. A resumed
collection leaves the list as it is.
"""

import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from agentcontext import caches, commits, paths
from agentcontext.git import clone
from agentcontext.repos import repo_name_to_dir, split
from agentcontext import logs

if TYPE_CHECKING:  # circular otherwise, because the collector imports this module
    from agentcontext.collector import CollectionConfig


def outcome_of(fail_row: "str | None") -> str:
    """The outcome that a line of the list of finished repositories records: `ok`, or the last
    field of its fail row."""
    return "ok" if fail_row is None else fail_row.rsplit(",", 1)[-1]
# The step's own failure on a repository with a successful clone: an exception after the
# clone. It is recorded under its own name, not as `clone_failed`, with the traceback in the
# clone log and on the console.
COLLECTOR_ERROR = "collector_error"
# The outcomes that a later run redoes. `clone_failed` comes from the network, the GitHub access
# token or the machine on that day, not from the repository, and a resumed run that skipped
# these repositories would publish a sample without them. A collector error comes from the
# machine or a defect, and a run after the fix redoes it. The other outcomes concern the
# repository. A refusal by GitHub, `not_found_or_no_access`, `dmca_takedown` or
# `disabled_by_github` (`clone.PERMANENT_OUTCOMES`), and `file_name_too_long`, a name that the
# file system cannot write, stay recorded for the collection. Only a run under a larger size
# bound redoes `size_bound_exceeded`, and `time_bound_exceeded` is redone by a run under a larger
# time bound and by the next collection (`clone_needs_redo`).
TRANSIENT_OUTCOMES = ("clone_failed", COLLECTOR_ERROR)


def time_bound_holds(recorded_time_bound: "float | None", clone_timeout: float,
                     recorded_collection: "str | None", collection: "str | None") -> bool:
    """Whether the time bound that stopped a repository's last clone, which its line records,
    still holds for this run.

    It holds while this run's bound, `clone_timeout`, is not larger than the recorded one,
    where 0 stands for no bound, and while the run belongs to the collection that wrote the
    line. Within that collection a clone under the same bound would be stopped again, and each
    attempt would hold a worker for the whole bound in every retry round and on every Start. A
    new collection clones the repository again under the same bound, because a transfer that
    the bound stopped on one day can finish on another. A line without a recorded bound holds
    none.
    """
    if recorded_time_bound is None:
        return False
    return ((clone_timeout or float("inf")) <= (recorded_time_bound or float("inf"))
            and recorded_collection == collection)


def clone_needs_redo(fail_row: "str | None", recorded_size_bound: "int | None",
                     max_clone_bytes: int = 0, recorded_time_bound: "float | None" = None,
                     clone_timeout: float = 0, recorded_collection: "str | None" = None,
                     collection: "str | None" = None) -> bool:
    """Whether this run should clone again a repository that the size bound or the time bound
    stopped.

    A clone over the size bound is cloned again when this run's size bound is larger than the
    one that the line records, where 0 stands for no bound. Under the same bound, the clone
    exceeds it again. A clone that the time bound stopped is cloned again when the bound that
    its line records no longer holds (`time_bound_holds`). Such a line is a `time_bound_exceeded`
    line, or an ok line that kept the earlier collection after the time bound stopped its retry.
    `recorded_collection` is the collection that wrote the line, and `collection` the collection
    in progress. A `size_bound_exceeded` or `time_bound_exceeded` line without the recorded
    bound is redone once, which records the bound.
    """
    outcome = outcome_of(fail_row)
    if outcome == clone.SIZE_BOUND_EXCEEDED:
        if recorded_size_bound is None:
            return True
        return (max_clone_bytes or float("inf")) > (recorded_size_bound or float("inf"))
    if outcome == clone.TIME_BOUND_EXCEEDED and recorded_time_bound is None:
        return True
    if outcome in (clone.TIME_BOUND_EXCEEDED, "ok") and recorded_time_bound is not None:
        return not time_bound_holds(recorded_time_bound, clone_timeout, recorded_collection,
                                    collection)
    return False


def history_needs_reread(commits_dir: Path | None, repo: str, clone_timeout: float = 0,
                         max_clone_bytes: int = 0) -> bool:
    """Whether this run should read the repository's commit history again, as its
    per-repository commit record decides.

    When reading the commit history raised an `OSError`, such as from a refused `fork()`, the
    repository has an `ok` line in the list of finished repositories and a per-repository
    commit record of the failure. The repository is redone, because a Start would otherwise
    leave it out of the commit CSV permanently. A history that did not arrive within the timeout
    or the size bound is redone when this run's bounds, `clone_timeout` and `max_clone_bytes`,
    are larger than the bounds in the record, as the run's last log message instructs. The
    history is not fetched again under the same bounds, because the failure would recur. A
    repository with a file with a
    failed history query is redone, so that the file is measured in a clone made on the day of
    its collection. When no run of this step redoes it, the file history step measures the
    file later. A record that does not parse, such as one that a power loss left empty, is
    redone, because none of its commits reach a CSV and no later step can read it.
    """
    if commits_dir is None:
        return False
    json_path = commits.shard_paths(commits_dir, repo)[1]
    try:
        with open(json_path, encoding="utf-8") as f:
            record = json.load(f)
    except OSError:
        return False
    except ValueError:
        return True
    if not isinstance(record, dict):
        return True
    if commits.required_log_missing(commits_dir, repo, record):
        return True
    if record.get("history_failed"):
        # Files with a history query that failed after the fetch brought the history, such as
        # when GitHub refused the file contents that `--follow` requests, in a burst of HTTP 400
        # responses. The commit history was read, but the files have no AI row until a redo or
        # the file history step writes one.
        return True
    if record.get("outcome") not in ("history_unavailable", "history_read_failed"):
        return False
    if record.get("transient"):
        return True
    if record.get("outcome") == "history_unavailable" and "time_bound" not in record:
        # A record without the bounds and without the `transient` flag has an unknown cause. It
        # is redone once, which rewrites the record.
        return True
    if "time_bound" in record:
        # A history fetch that the time or size bound stopped, or a reading of the commit history
        # that the timeout ended, is redone under larger bounds and not under the same bounds.
        recorded_time_bound = record.get("time_bound") or float("inf")
        recorded_size_bound = record.get("size_bound") or float("inf")
        return ((clone_timeout or float("inf")) > recorded_time_bound
                or (max_clone_bytes or float("inf")) > recorded_size_bound)
    if "transient" in record:
        return bool(record["transient"])
    # A record without the `transient` flag. An unavailable history without a recorded reason
    # is redone once, which rewrites the record with the flag. A record with a
    # `detail` that names a failure to start git is redone, and a record of a reading that the
    # timeout ended or that git refused is not.
    detail = str(record.get("detail", ""))
    return (record["outcome"] == "history_unavailable"
            or detail.split(":")[0] in MACHINE_ERROR_NAMES)


def commit_log_missing(commits_dir: Path | None, repo: str) -> bool:
    """Whether the repository's per-repository commit record records a commit history that was
    read and its commit log is missing (`commits.required_log_missing`). The cloning step exits 4
    while such a record remains."""
    if commits_dir is None:
        return False
    try:
        with open(commits.shard_paths(commits_dir, repo)[1], encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(record, dict) and commits.required_log_missing(commits_dir, repo, record)


# The report in which the commit reclassification records the outcome of each repository, beside
# the commit CSV.
RECLASSIFICATION_REPORT_NAME = "commit-reclassification.json"


def reclassification_failed(report: Path | None, commits_dir: Path | None) -> set:
    """The repositories that the commit reclassification's report `report` names with the
    outcome `failed`, while each one's per-repository commit record under `commits_dir` is the
    record that the reclassification read, as the SHA-256 that `report` records for it shows.

    The reclassification records `failed` for evidence that fails validation, such as a truncated
    commit log, and leaves the repository's shards out of the commit CSVs. Such a record can be
    classified under the current commit heuristics, and a record of the current heuristics is not
    classified again when its repository is taken over (`reuse.reclassify`). Without this test,
    no later collection would read the commit history of the repository again. A record that
    changed since then, as after a new collection of the repository, is not named. Without
    `report`, or with a `report` that cannot be read, no repository is named.
    """
    if report is None or commits_dir is None:
        return set()
    try:
        repositories = json.loads(report.read_text(encoding="utf-8"))["repositories"]
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, KeyError, TypeError) as error:
        logs.warning('repository_cloning', f"Could not read {report} ({error}). Collecting no "
                     f"repository again for a commit log that failed validation.")
        return set()
    failed = set()
    for repo, result in (repositories.items() if isinstance(repositories, dict) else ()):
        if not isinstance(result, dict) or result.get("outcome") != "failed":
            continue
        try:
            record = commits.shard_paths(commits_dir, repo)[1].read_bytes()
        except OSError:
            continue
        if hashlib.sha256(record).hexdigest() == result.get("record_sha256"):
            failed.add(repo)
    return failed


# The exception names, `OSError` and its subclasses, that a per-repository commit record without
# the `transient` flag may have in its `detail` when git could not start to read the history.
MACHINE_ERROR_NAMES = frozenset(
    cls.__name__ for cls in (OSError, BlockingIOError, ChildProcessError, FileNotFoundError,
                             InterruptedError, PermissionError, ProcessLookupError))



def row_has_files(main_row: str) -> bool:
    """Whether the flags of the collected row record that a file was copied, that is, whether
    any of them is TRUE.

    `.agents/` alone sets no flag, and this function therefore returns False for a repository
    that contributed nothing else. `tree_missing` finds such a repository through its
    per-repository commit record where the shards still exist.
    """
    return "TRUE" in paths.row_fields(main_row)[paths.FLAG_COLUMNS_START:]


def tree_missing(config: CollectionConfig, repo: str, main_row: str = "") -> bool:
    """Whether the repository's row or its per-repository commit record indicates that files
    were copied, and the repository's directory is gone.

    The function reads the row because an archive can restore the list of finished repositories
    and the two lists without the per-repository commit records. A check that read only the
    record would find nothing pending among the repositories that such an archive restores.
    """
    if (config.collected_dir / repo_name_to_dir(repo)).is_dir():
        return False
    if row_has_files(main_row):
        return True
    return (config.commits_dir is not None
            and commits.shard_paths(config.commits_dir, repo)[1].is_file())


def append_finished(path: Path, repo: str, main_row: str, fail_row: str | None,
                  facts: list | None = None, submodules: list | None = None,
                  redo: bool = False, size_bound: int | None = None,
                  measures: dict | None = None, collected_under: dict | None = None,
                  retrying: bool = False, head_no_response: bool = False,
                  time_bound: float | None = None, collection: str | None = None) -> None:
    """Append one finished repository to the list of finished repositories, synced to disk
    immediately.

    The list of finished repositories stores each repository's finished CSV rows, not just its
    name, because the clone is deleted once a repository is done and a resumed run could not
    compute the rows again. Storing the rows makes the list of finished repositories the source
    of truth, and writing the final CSVs a pure function of it.

    `size_bound` and `time_bound` are the size bound and the time bound that a clone went over,
    which `clone_needs_redo` compares with a later run's. An ok line records `time_bound` when
    the time bound stopped a retry and the line keeps the earlier collection. `collection` names
    the collection that wrote the line (`open_collection`), and a resumed run of that collection
    takes the line over without querying the head. `measures` is what the repository cost, with
    the keys that the docstring of `collector.process_repo` lists, and nothing here reads it
    back. `collected_under` is what the repository was collected under, which a later run
    compares with its own (`reuse.outdated`).

    `retrying` marks the line written before a retry starts. A retry removes the earlier commit
    shards and tree before it writes its own, and while that line is the last one of its
    repository, the tree on disk may be neither the earlier tree nor the new one.
    `head_no_response` marks a line taken over while git returned no head commit for the
    repository. The retry rounds and a later run query such a repository again rather than
    clone it.

    The file history rows, `facts`, are in the line for the same reason as the CSV rows, because
    they cost a history fetch and the clone that they were read from is gone by the time the
    line is written.
    """
    record = {"repo": repo, "row": main_row, "fail_row": fail_row}
    if facts:
        record["facts"] = facts
    if submodules:
        record["submodules"] = submodules
    if redo:
        record["redo"] = True
    if size_bound is not None:
        record["size_bound"] = size_bound
    if measures:
        record["measures"] = measures
    if collected_under:
        record["collected_under"] = collected_under
    if retrying:
        record["retrying"] = True
    if head_no_response:
        record["head_no_response"] = True
    if time_bound is not None:
        record["time_bound"] = time_bound
    if collection:
        record["collection"] = collection
    # A kill in the middle of a write leaves a line without its newline, and the next run's
    # first record would be written on the same line, where the reader skips both.
    with open(path, "a+b") as f:
        f.seek(0, os.SEEK_END)
        created = f.tell() == 0
        if not created:
            f.seek(-1, os.SEEK_END)
            if f.read(1) != b"\n":
                f.write(b"\n")
        f.write((json.dumps(record) + "\n").encode("utf-8"))
        f.flush()
        # On disk before the step goes on, because the step removes or replaces a repository's
        # files around its lines. After a power loss, a line that never reached the disk would
        # leave the repository's earlier line in force over files that it does not describe.
        os.fsync(f.fileno())
    if created:
        caches.sync_directory(path.parent)


def read_finished(path: Path) -> dict[str, tuple[str, str | None, list, list, bool, int | None,
                                                 dict | None, bool, bool, float | None,
                                                 str | None]]:
    """Read the list of finished repositories into
    {repo: (main_row, fail_row, facts, submodules, redo, size_bound, collected_under, retrying,
    head_no_response, time_bound, collection)}.

    Unparseable lines are skipped rather than fatal. The file is append-only, and a crash
    mid-write can therefore leave only the last line incomplete. The repository of that line is
    treated as not done and collected again.
    """
    done: dict[str, tuple[str, str | None, list, list, bool, int | None, dict | None, bool,
                          bool, float | None, str | None]] = {}
    if not path.is_file():
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                done[record["repo"]] = (record["row"], record.get("fail_row"),
                                        record.get("facts") or [],
                                        record.get("submodules") or [],
                                        bool(record.get("redo")), record.get("size_bound"),
                                        record.get("collected_under"),
                                        bool(record.get("retrying")),
                                        bool(record.get("head_no_response")),
                                        record.get("time_bound"), record.get("collection"))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    return done


def reduce_finished(path: Path) -> "tuple[int, int] | None":
    """Rewrite the list of finished repositories with the lines that its readers use, in one
    step, and return the number of lines before and after, or None when there is no list.

    Of the lines of a repository, `read_finished` uses the last line that parses. The commit
    reclassification (`commit_reclassification.finished_copied`) uses the count of copied files
    of the last line with a count, as long as every later line of the repository repeats its
    row. The reduced list keeps these lines in their order. It leaves out every other line,
    every line that does not parse and every line without a row, which neither reader uses.
    """
    if not path.is_file():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    entries: dict = {}
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
            repo, row = record["repo"], record["row"]
            measures = record.get("measures")
            counted = isinstance(measures, dict) and isinstance(measures.get("copied"), int)
            entries.setdefault(repo, []).append((index, row, counted))
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            continue
    keep = set()
    for repo_entries in entries.values():
        last, _, last_counted = repo_entries[-1]
        keep.add(last)
        counted = [entry for entry in repo_entries if entry[2]]
        if counted and not last_counted:
            index, row, _ = counted[-1]
            if all(entry[1] == row for entry in repo_entries if entry[0] > index):
                keep.add(index)
    if len(keep) < len(lines):
        caches.replace_file(path, "".join(lines[index] + "\n" for index in sorted(keep)))
    return len(lines), len(keep)


FINISHED_NAME = "finished_repositories.jsonl"


def collection_path(finished_path: Path) -> Path:
    """Where the record of the collection in progress over a list of finished repositories is:
    beside the list, named after it, such as `finished_repositories.collection.json`."""
    return finished_path.with_name(finished_path.stem + ".collection.json")


def ended_path(finished_path: Path) -> Path:
    """Where the record of the last collection that ended over a list of finished repositories
    is: beside the list, named after it, such as
    `finished_repositories.ended-collection.json`."""
    return finished_path.with_name(finished_path.stem + ".ended-collection.json")


def list_digest(repos) -> str:
    """The SHA-256 of an input list by its repositories in sorted order, which ties a collection
    to its input list."""
    return hashlib.sha256("\n".join(sorted(repos)).encode("utf-8")).hexdigest()


def open_collection(finished_path: Path, repos: list, gone=frozenset()) -> dict:
    """The collection in progress over the list of finished repositories and the input list
    `repos`: the collection that its record names, the last collection that ended, or a new
    collection, whose record this function writes.

    A collection starts when the cloning step runs over an input list with no collection in
    progress, and ends when the step exits 0 (`close_collection`). Every invocation in between,
    after a stop, a crash or an exit that leaves repositories to retry, resumes it. A record of
    another input list belongs to a collection of another sample, and a new collection starts.
    Before a new collection starts, the function reduces the list (`reduce_finished`).

    The collection in progress also continues over an input list that lacks only repositories
    of its own list that GitHub no longer serves (`dropped_from`). When no collection is in
    progress, the last collection that ended continues over its own list and over such a list.
    `gone` names the repositories that the labeling found gone. Such a list comes from a
    labeling that ran again over the same sampling frame and found repositories gone, for
    example after a new image changed a module of the classification. The step runs over the
    list of the collection that ended when its tree changed, after a change of the collector's
    code or of the heuristics, or when the runner could not tell which of its inputs changed. A
    new collection would query the head of every repository again and collect again every
    repository with a head that moved, although the list adds no repository. The continued
    collection takes over what it collected and confirmed, and what a change of the collector,
    git or the heuristics reaches is collected again (`reuse.outdated`). A new snapshot of the
    same sample is a new collection, and `--from repository_cloning` requests it by marking the
    collection that ended as replaced (`forget_ended_collection`). The record of a continued
    collection has the key `CONTINUED`, so that `--from repository_cloning` marks that collection
    as replaced too: It is the earlier snapshot, not a collection that has yet to take its
    snapshot.

    The returned dict names the collection under `collection`, by the time at which it started
    and a random suffix, and records the SHA-256 of its input list and the list under
    `repositories`. `taken_over` lists the repositories of earlier collections that it took
    over after querying their heads (`reuse.outdated`). An invocation that resumes or continues
    the collection takes over these repositories and the repositories with lines that the
    collection wrote without querying their heads again, so that a crash costs only the
    repositories that were in flight.
    """
    digest = list_digest(repos)
    path = collection_path(finished_path)
    earlier, ended, dropped = _continued(finished_path, repos, gone)
    if earlier is not None and not ended and not dropped:
        logs.info('repository_cloning', f"Resuming the collection {earlier['collection']}, "
                  f"recorded in {path}.")
        # A record written before records listed their repositories gets the list with the next
        # save, so that a later list without repositories that GitHub no longer serves can
        # continue the collection also for a repository that the collection has not reached.
        return {**earlier, "repositories": sorted(repos)}
    if earlier is not None:
        collection = {**earlier, "input_sha256": digest, "repositories": sorted(repos),
                      **({CONTINUED: True} if ended else {})}
        save_collection(finished_path, collection)
        ended_path(finished_path).unlink(missing_ok=True)
        over = ("over its list" if not dropped else
                f"over its list without the {len(dropped):,} repositories that GitHub no longer "
                f"serves, such as {dropped[0]}")
        logs.info('repository_cloning', f"Continuing the collection {collection['collection']}"
                  f"{', which ended,' if ended else ''} {over}, recorded in {path} until the "
                  f"step exits 0.")
        return collection
    reduced = reduce_finished(finished_path)
    if reduced is not None and reduced[1] < reduced[0]:
        logs.info('repository_cloning', f"Kept {reduced[1]:,} of the {reduced[0]:,} lines of "
                  f"{finished_path.name}, the lines that a collection and the commit "
                  f"reclassification read.")
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    collection = {"collection": f"{started}-{secrets.token_hex(4)}", "input_sha256": digest,
                  "taken_over": set(), "repositories": sorted(repos)}
    save_collection(finished_path, collection)
    ended_path(finished_path).unlink(missing_ok=True)
    logs.info('repository_cloning', f"Starting the collection {collection['collection']}, "
              f"recorded in {path} until the step exits 0.")
    return collection


def _continued(finished_path: Path, repos: list, gone) -> tuple:
    """The record of the collection that a run over the input list `repos` resumes or
    continues, whether that collection ended, and the repositories of its list that `repos`
    lacks because GitHub no longer serves them (`dropped_from`), or `(None, False, [])` where
    the run begins a new collection.

    The collection is the collection in progress, or the last collection that ended where none
    is in progress. It is resumed or continued over its own list and over its list without
    repositories that GitHub no longer serves. A collection in progress over another list
    belongs to another sample, and the last collection that ended is then not read."""
    record = read_collection(collection_path(finished_path))
    ended = record is None
    if ended:
        record = read_collection(ended_path(finished_path))
    if record is None:
        return None, False, []
    if record["input_sha256"] == list_digest(repos):
        return record, ended, []
    dropped = dropped_from(record, repos, gone, finished_path)
    return (record, ended, dropped) if dropped else (None, False, [])


def dropped_from(record: dict, repos: list, gone, finished_path: Path) -> list:
    """The repositories of the input list of the collection that `record` names that `repos`
    lacks, sorted, if GitHub no longer serves any of them and `repos` adds no repository, and
    otherwise an empty list.

    GitHub no longer serves a repository in `gone`, which the labeling found gone, or a
    repository with a line in the list of finished repositories that records
    `not_found_or_no_access`. The repositories of the collection's list are those that the
    record lists under `repositories`. A record written before records listed their
    repositories gives only the repositories that the collection wrote lines for and took over.
    The SHA-256 of the collection's list decides: `repos` with the dropped repositories has to
    have it.

    A record that lists its repositories is read alone, because the lines name more than its
    list after a shrink: a repository dropped by an earlier shrink keeps a line that names the
    collection, the line that `reuse.remove_left` appends again. Added to the list, that
    repository would give the list of the collection before the earlier shrink, and a second
    shrink would begin a new collection."""
    wanted = set(repos)
    done = read_finished(finished_path)
    listed = record.get("repositories")
    known = (set(listed) if listed is not None else
             record["taken_over"] | {repo for repo, line in done.items()
                                     if line[10] == record["collection"]})
    dropped = sorted(repo for repo in known - wanted
                     if repo in gone or (repo in done and outcome_of(done[repo][1])
                                         == clone.NOT_FOUND_OR_NO_ACCESS))
    if dropped and list_digest(wanted | set(dropped)) == record["input_sha256"]:
        return dropped
    return []


def read_collection(path: Path) -> "dict | None":
    """The record of a collection at `path`, as `open_collection` returns it, or None where there
    is none, where it does not parse and where it is the mark of a replaced collection
    (`forget_ended_collection`), which names no list."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not (isinstance(record, dict) and isinstance(record.get("collection"), str)
            and isinstance(record.get("input_sha256"), str)
            and isinstance(record.get("taken_over"), list)):
        return None
    listed = record.get("repositories")
    return {"collection": record["collection"], "input_sha256": record["input_sha256"],
            "taken_over": set(record["taken_over"]),
            **({"repositories": listed} if isinstance(listed, list) else {}),
            **({CONTINUED: True} if record.get(CONTINUED) is True else {})}


def collection_in_progress(finished_path: Path, repos: list, gone=frozenset()) -> "dict | None":
    """The collection in progress over the list of finished repositories and the input list
    `repos`, or over a list of which `repos` lacks only repositories that GitHub no longer
    serves (`dropped_from`), or None where no record names one, without writing a record."""
    record = read_collection(collection_path(finished_path))
    if record is None:
        return None
    if record["input_sha256"] == list_digest(repos) or dropped_from(record, repos, gone,
                                                                     finished_path):
        return record
    return None


def save_collection(finished_path: Path, collection: dict) -> None:
    """Write the record of the collection in progress, in one step."""
    caches.replace_file(collection_path(finished_path), json.dumps(
        {"collection": collection["collection"], "input_sha256": collection["input_sha256"],
         "taken_over": sorted(collection["taken_over"]),
         **({"repositories": collection["repositories"]} if "repositories" in collection
            else {}),
         **({CONTINUED: True} if collection.get(CONTINUED) is True else {})}) + "\n")


def close_collection(finished_path: Path) -> None:
    """End the collection in progress. The record of the collection stays as the record of the
    last collection that ended (`ended_path`), which a run over its list or over the list without
    repositories that GitHub no longer serves continues (`open_collection`). A new collection,
    which queries the head of every repository that it would take over, starts over another
    list, or after `forget_ended_collection`."""
    path = collection_path(finished_path)
    if path.is_file():
        os.replace(path, ended_path(finished_path))
        caches.sync_directory(path.parent)


# The key of the record of the last collection that ended that `forget_ended_collection` writes
# in its place, with the name of the collection that a new snapshot replaces.
REPLACED = "replaced"
# The key of the record of a continued collection (`open_collection`), which
# `--from repository_cloning` marks as replaced while it is in progress
# (`forget_ended_collection`). The key stays in the record when the collection ends again, and
# continuing the collection once more sets it anyway.
CONTINUED = "continued_after_end"


def forget_ended_collection(finished_path: Path) -> None:
    """Mark the last collection that ended as replaced by a new snapshot, so that the next run
    over any input list begins a new collection unless a collection that never ended is in
    progress (`open_collection`), and so that the labeling counts neither that collection nor an
    earlier one as a collection over its sample (`replaced_collection`).
    `--from repository_cloning` requests a new snapshot this way (`runner.drop_completed_from`).

    The mark replaces the record of that collection, and `read_collection` reads none from it.
    A continued collection in progress (`CONTINUED`) is that collection: The function removes
    its record and marks it as replaced. A stop, a kill, a power loss or any exit other than 0 of
    the continued collection leaves its record, and the next run would otherwise resume it and
    publish the heads of the earlier snapshot. Where no record of the collection that ended is
    kept, because `--mark-complete` removes the record and leaves the lines, the mark names the
    latest collection that a line of the list of finished repositories names besides the
    collection in progress. Where no collection is named, the function removes what is at the
    place of the record. A new collection removes the mark when it starts."""
    path = ended_path(finished_path)
    in_progress = read_collection(collection_path(finished_path))
    if in_progress is not None and in_progress.get(CONTINUED):
        # The record of the collection in progress is removed before the mark is written. A
        # kill between the two then leaves no record that the next run resumes, and that run
        # begins a new collection.
        collection_path(finished_path).unlink()
        caches.replace_file(path, json.dumps({"collection": in_progress["collection"],
                                              REPLACED: True}) + "\n")
        return
    ended = read_collection(path)
    if ended is not None:
        name = ended["collection"]
    else:
        names = {line[10] for line in read_finished(finished_path).values()
                 if isinstance(line[10], str)}
        if in_progress is not None:
            names.discard(in_progress["collection"])
        # The name of a collection begins with the time at which it started, and the latest
        # name sorts last. The order of the lines is no such order, because the step appends
        # a line of an earlier collection again when it removes the files of its repository
        # (`reuse.remove_left`).
        name = max(names) if names else None
    if name is None:
        path.unlink(missing_ok=True)
        return
    caches.replace_file(path, json.dumps({"collection": name, REPLACED: True}) + "\n")


def replaced_collection(finished_path: Path) -> "str | None":
    """The name of the collection that `forget_ended_collection` marked as replaced by a new
    snapshot, or None where no mark is kept."""
    try:
        record = json.loads(ended_path(finished_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(record, dict) and record.get(REPLACED) is True
            and isinstance(record.get("collection"), str)):
        return record["collection"]
    return None


def forget_collections(finished_path: Path) -> None:
    """Remove the record of the collection in progress and of the last collection that ended,
    so that the next run starts a new collection whatever its input list."""
    for path in (collection_path(finished_path), ended_path(finished_path)):
        path.unlink(missing_ok=True)


def fresh_start(finished_path: Path, config: CollectionConfig) -> list[str]:
    """Remove what a collection leaves, so that `--fresh` starts from nothing. Returns what it
    removed.

    The shards, the clone logs and the collected repositories of the previous collection are
    removed with the list of finished repositories and the records of the collection in progress
    and of the last collection that ended over it. Left in place, they would put rows of a commit history that the new run did not
    read again into the commit CSV, and a repository dropped from the input would keep its tree
    for the validator to read. The file lists, the workflow files and the imported files are
    removed too, as records of clones that the new run makes again, and so are the path
    heuristics that the repositories were collected under. The two lists at the root of the
    collected tree, one of them the tracked `clone_failed.csv`, stay, because the run rewrites
    them.
    """
    removed = []
    for path in (finished_path, collection_path(finished_path), ended_path(finished_path)):
        if path.exists():
            path.unlink()
            removed.append(f"{path}")
    for label, directory in (("the shards", config.commits_dir),
                             ("the clone logs", config.log_dir),
                             ("the file lists", config.file_lists_dir),
                             ("the workflow files", config.workflows_dir),
                             ("the imported files", config.imported_dir),
                             ("the path heuristics of the collection", config.heuristics_dir)):
        if directory is not None and directory.is_dir():
            paths.remove_tree(directory)
            removed.append(f"{label} {directory}")
    if config.collected_dir.is_dir():
        # Through `split`, which makes a symlink named like a repository a stray. `remove_tree`
        # refuses to remove such a symlink, and this function leaves it in place and names it,
        # as the validator does.
        repos, strays = split(config.collected_dir,
                                         expected=("repos.csv", "clone_failed.csv"))
        for name in repos:
            paths.remove_tree(config.collected_dir / name)
        if repos:
            removed.append(f"{len(repos):,} collected repositories under {config.collected_dir}")
        for name in strays:
            logs.warning('repository_cloning', f"--fresh left {config.collected_dir / name} in "
                                               f"place, because it is not a repository "
                                               f"directory.")
    return removed
