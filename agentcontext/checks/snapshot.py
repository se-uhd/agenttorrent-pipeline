"""Record current collection provenance and optionally verify recorded commits on GitHub.

`snapshot write` derives the collection counts and dates from the collected list and tree,
the published repositories from output/repos.csv, and the rows without commit metadata from
the mechanism CSVs. The runner runs it before the publication. `--outputs-only` checks only
the sections derived from output/ and does not read the collected tree.

Each section names its input counts in `derived_from`. A writer updates its sections
atomically under a permanent flock file, and the kernel releases the lock when the process
exits. A write keeps the sections of the other writers. No writer may substitute a partial
read for a complete provenance record.

`snapshot validation` checks the recorded head commits against GitHub's compare API. It tries
the collected branch, the current default branch and the commits of the files of the
configuration artifacts. A network error stays distinct from a missing repository or an
unreachable commit. A run resumes from its results and records them only when the coverage
checks pass.
"""

from __future__ import annotations

import contextlib
import csv
import datetime
import fcntl
import io
import json
import hashlib
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from agentcontext import (caches, cli, credentials, REPO_ROOT, STAGE_DIR, heuristics, paths,
                          planning_schema, repos)
from agentcontext import github_api, logs
from agentcontext.steps import commit_reclassification as reclassification_step
from agentcontext.steps import import_detection


# -----------------------------------------------------------------------------
# The record
# -----------------------------------------------------------------------------
SNAPSHOT_PATH = STAGE_DIR / "output" / "snapshot.json"


# GROUNDING: every writer serializes one document inside the lock, after its reads and requests
# have finished, and the critical section contains no network request. A wait of a minute
# therefore means that a live process holds the lock without making progress. A process that
# died is not waited for, because the kernel releases its lock when it exits.
LOCK_TIMEOUT_SECONDS = 60.0
# The poll interval starts at the minimum, so that writers that arrive together each wait little
# longer than the critical section, and doubles up to the maximum, so that a long wait does not
# poll at the minimum interval.
LOCK_POLL_MIN_SECONDS = 0.001
LOCK_POLL_MAX_SECONDS = 0.05


# The outcomes of the commit reclassification that keep evidence out of the release: the
# incomplete outcomes, `failed` and a recorded clone failure. The `commit_reclassification`
# section names the repositories of each, because the release README names them in the evidence
# limit of their outcome, and a user needs the names to leave these repositories out of a count.
NAMED_OUTCOMES = (*reclassification_step.INCOMPLETE_OUTCOMES, "failed", "unavailable")


class SnapshotError(RuntimeError):
    """The snapshot file exists but cannot be read as one."""


class InvalidReport(ValueError):
    """A commit reclassification report that contradicts itself or the collection list. Its
    text is a sentence that names the report."""


def load(path: Path | None = None) -> dict:
    """Return the snapshot, or an empty one when the file does not exist yet.

    A file that is not readable JSON raises rather than returning empty: An empty snapshot
    would let the next writer overwrite the record with a single section.
    """
    path = Path(path) if path is not None else SNAPSHOT_PATH
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            snapshot = json.load(f)
    except json.JSONDecodeError as e:
        raise SnapshotError(
            f"{path} is not readable JSON ({e}). Restore it from git, then run the command "
            f"again.") from e
    if not isinstance(snapshot, dict):
        raise SnapshotError(f"{path} does not contain a JSON object")
    return snapshot


def _open_lock_file(lock: Path) -> int:
    """Open the lock file, creating it when a writer locks this tree for the first time.
    Returns the fd.

    Opened read-only, never with ``O_TRUNC`` or ``O_WRONLY``: ``flock`` needs no write access,
    and nothing uses or writes the file's contents. The file is permanent, and so is a mode that
    denies a user write access. Opening the file for writing would then make the lock file that
    the first writer created raise an ``OSError`` for every later writer of another user on a
    shared checkout.

    Python opens descriptors as non-inheritable, and a subprocess started inside the critical
    section therefore does not keep the lock past this process's exit.
    """
    # A lock DIRECTORY that an earlier version of this module left has to be named rather
    # than removed or used. Removing it would mean deciding from the outside that no old run
    # is still going, which can lose sections. Using it would let the leftover directory keep
    # working unnoticed, because flock on a directory descriptor serializes as well.
    #
    # The two platforms detect the directory at different places. macOS opens a directory
    # with O_RDONLY|O_CREAT and returns a descriptor, which the S_ISDIR check below detects.
    # On Linux, O_CREAT on a directory is an error, and the open raises IsADirectoryError
    # (EISDIR). Both paths raise the same SnapshotError.
    directory = (f"{lock} is a directory, the lock of an earlier version of this module. Check "
                 f"that no run of that version is still going, then remove the directory and run "
                 f"the command again.")
    try:
        fd = os.open(lock, os.O_RDONLY | os.O_CREAT, 0o666)
    except IsADirectoryError:
        raise SnapshotError(directory) from None
    if stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise SnapshotError(directory)
    return fd


def _wait_for(fd: int, lock: Path, timeout: float, bound: float | None = None) -> None:
    """Take the exclusive lock on `fd`, or raise TimeoutError once `timeout` runs out.

    The function makes non-blocking attempts against a deadline rather than one blocking
    ``flock``. A blocking call cannot be given a bound without a signal, and ``signal.alarm``
    is process-global and can be set only from the main thread, while the validation runs a
    thread pool. The timeout prevents an indefinite wait when a live process holds the lock
    without making progress. The kernel releases the lock when its holder exits.
    """
    deadline = time.monotonic() + timeout
    delay = LOCK_POLL_MIN_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            # BlockingIOError is the only error that means that another process or thread
            # holds the lock. Every other OSError, such as from a filesystem that does not
            # implement flock, is a real failure and is raised rather than turned into a wait
            # that ends in writing the file without the lock.
            pass
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Could not lock {lock} within {timeout if bound is None else bound:g}s")
        time.sleep(delay)
        delay = min(delay * 2, LOCK_POLL_MAX_SECONDS)


def _still_at(fd: int, lock: Path) -> bool:
    """Whether the descriptor that this process locked is still the file at the lock path.

    It is not when the file was replaced between the open and the grant: The lock then covers
    an inode with no name, while the next writer creates a new file at the path and locks that
    file, and both run the critical section at the same time. Reopening and retrying puts this
    process back on the file that every other writer locks.

    The function detects a replacement up to the moment of the grant. A later replacement
    cannot be detected here, which is why the lock file is tracked in git rather than ignored:
    `git clean -xdf` removes an ignored file and keeps a tracked one.
    """
    try:
        return os.fstat(fd).st_ino == os.stat(lock).st_ino
    except OSError:
        return False  # Unlinked and not replaced: This process's lock excludes no writer.


@contextlib.contextmanager
def _locked(path: Path):
    """Hold an exclusive lock on a snapshot file for the length of a read-modify-write.

    ``flock`` and not ``fcntl.lockf``, because a POSIX record lock belongs to the process: A
    second thread of a process that already holds one is granted it, and a script that runs a
    thread pool imports this module. An ``flock`` belongs to the open file description, and two
    threads that each open the file exclude each other as two processes do.

    The lock file is created once and never removed. Unlinking it on release causes a race: A
    waiter blocked on the descriptor that it already opened then locks an inode that no longer
    has a name, while the next writer creates a new file at the same path and locks that file,
    and both run the critical section at the same time. Removing the file from outside the
    pipeline causes the same race, which is why the file is committed rather than ignored.
    `_still_at` detects the remaining case, a replacement up to the moment of the grant.

    Closing the descriptor releases the lock, and the `finally` closes it on every exit from
    the block that leaves this process running. A process that forks inside the critical
    section would leave the child with the lock, and none of the callers forks.
    """
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        fd = _open_lock_file(lock)
        try:
            _wait_for(fd, lock, max(deadline - time.monotonic(), 0.0),
                      bound=LOCK_TIMEOUT_SECONDS)
            if _still_at(fd, lock):
                break
        except BaseException:
            os.close(fd)
            raise
        # Another process replaced the file while this process waited. Start again on the file
        # that is at the path now, within the same overall deadline.
        os.close(fd)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Could not lock {lock}, because the file was replaced again "
                               f"and again during the wait")
    try:
        yield
    finally:
        os.close(fd)


def check_derived_from(derived_from) -> dict:
    """Validate a provenance mapping and return it. Raises ValueError on anything else.

    The layout is ``{input name: {unit: count}}``, as in
    ``{"data/collected/repos.csv": {"rows": 120}}``. Counts are integers so that a later run can
    compare them with the inputs that it finds rather than with a sentence. A key includes
    the input's directory, because three files in this step are named ``repos.csv``
    (``data/collected/``, ``data/validated/`` and ``output/``) and a bare basename would record
    the same key for any of them.
    """
    if not isinstance(derived_from, dict) or not derived_from:
        raise ValueError("derived_from must be a non-empty mapping of input names to mappings "
                         "of unit names to counts")
    for name, counts in derived_from.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"A key of derived_from is {name!r}, not an input name")
        if not isinstance(counts, dict) or not counts:
            raise ValueError(f"derived_from[{name!r}] must be a non-empty mapping of unit names "
                             f"to counts")
        for unit, count in counts.items():
            if not isinstance(unit, str) or not isinstance(count, int) or isinstance(count, bool):
                raise ValueError(
                    f"derived_from[{name!r}] maps {unit!r} to {count!r}, but it must map a unit "
                    f"name to an integer count")
    return derived_from


def _destination_mode(path: Path) -> int:
    """The permissions that the replacement should have: the file's own, or a new file's.

    ``tempfile.NamedTemporaryFile`` always creates at 0600, and ``os.replace`` keeps that mode
    on the destination. Without this function, every update would remove the group and other
    read permissions from a file that is committed at 0644 and shipped in the replication
    archive. git records only the exec bit, and neither a diff nor ``git status`` would show
    the change.
    """
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        # os.umask is process-global, and reading it means setting it, which makes this pair
        # non-atomic. It is safe only because no worker thread is alive at either call site:
        # The validation records after its ThreadPoolExecutor block has exited. Do not
        # call update() from inside a pool, or a file that another worker creates in this window
        # is created at 0666.
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


def update(section: str, payload: dict, path: Path | None = None, *,
           derived_from: dict) -> dict:
    """Replace one top-level section and write the file back. Returns the whole snapshot.

    The section is replaced rather than merged into, so that a key that a writer drops on a
    later run does not stay behind as if it were current.

    `derived_from` is required and is stored in the section. The module docstring gives the
    reason.
    """
    return update_sections({section: (payload, derived_from)}, path)


# The sections that no command writes, which `snapshot write` removes, so that a count does not
# stay under a name that means something else, such as a `sample` beside `configured`. The
# sections of the other commands, such as the `validation` of `snapshot validation`, stay.
RETIRED_SECTIONS = ("sample",)


def update_sections(sections: dict, path: Path | None = None, drop: tuple = ()) -> dict:
    """Replace several top-level sections in one write, `{section: (payload, derived_from)}`,
    as `update` replaces one. Returns the whole snapshot.

    All sections that a run derives are written at once, so that a run that fails on a later
    section leaves the file unchanged rather than containing the sections that it finished. The
    sections that `drop` names are removed from the file, and every other section that the file
    holds stays as it is.
    """
    sections = {name: {**payload, "derived_from": check_derived_from(derived_from)}
                for name, (payload, derived_from) in sections.items()}
    path = Path(path) if path is not None else SNAPSHOT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        snapshot = {name: value for name, value in load(path).items() if name not in drop}
        snapshot.update(sections)
        # Remove a temporary file that a killed process left beside the target. The `except`
        # branch below handles an exception, but not a SIGKILL, an OOM kill or the `os._exit`
        # that a stop handler ends with. The name is random, and the next run writes a different
        # file and never removes the old one. The targets lie beside tracked files, where a
        # leftover temporary file fails `release`'s inventory and shows in the next `git status`.
        for stale in path.parent.glob(path.name + ".*.tmp"):
            stale.unlink(missing_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=path.parent,
            prefix=path.name + ".", suffix=".tmp")
        try:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
            handle.write("\n")
            # The data is synced before the rename and the directory after it, as in
            # `caches.replace_file`: On a power loss, a rename that reached the disk before the
            # data would leave an empty file, which `load` refuses at every later run.
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.chmod(handle.name, _destination_mode(path))
            # The temporary file is in the same directory, which makes the rename atomic rather
            # than a copy across filesystems.
            os.replace(handle.name, path)
        except BaseException:
            handle.close()
            with contextlib.suppress(OSError):
                os.unlink(handle.name)
            raise
        caches.sync_directory(path.parent)
    return snapshot


# -----------------------------------------------------------------------------
# Writing the record
# -----------------------------------------------------------------------------
DEFAULT_COLLECTED_CSV = STAGE_DIR / "data" / "collected" / "repos.csv"
DEFAULT_COLLECTED_DIR = STAGE_DIR / "data" / "collected"
DEFAULT_VALIDATED_CSV = STAGE_DIR / "data" / "validated" / "repos.csv"
DEFAULT_OUTPUT_DIR = STAGE_DIR / "output"
# The fetched files of a working copy, which were copied into the collected tree after the
# collection (GLOSSARY.md, Fetched file). A collection without fetches has no such list.
DEFAULT_FETCHED_CSV = STAGE_DIR / "fetched_files.csv"

# The heuristics' CSV filenames, without their extension, read from the heuristics so that
# `artifact_column_for_file` accepts every name in this list.
MECHANISMS = tuple(sorted(Path(f).stem for f in heuristics.csv_filenames()))
# The named mechanisms, every mechanism but `other`, and the filenames of their CSVs without the
# extension. The release publishes these CSVs and withholds `other.csv`, the unclassified files.
NAMED_MECHANISMS = tuple(name for name in heuristics.MECHANISM_CSVS if name != "other")
PUBLISHED_MECHANISMS = frozenset(Path(heuristics.MECHANISM_CSVS[name][0]).stem
                                 for name in NAMED_MECHANISMS)


def _rows(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _header(path: Path) -> list[str]:
    """The column names, read without the rows.

    A CSV with a header and no rows is normal in a subsample run or a smaller replication
    sample, and reading the first row to find the columns would fail on it with a traceback.
    """
    with open(path, newline="", encoding="utf-8") as f:
        return next(csv.reader(f), [])


def provenance_key(path: Path) -> str:
    """How a section's ``derived_from`` names one of its inputs.

    The path relative to the stage directory, so that the key names the file that was read
    rather than any of the three ``repos.csv`` (see `check_derived_from`). A path outside the
    stage directory falls back to its name, because the test fixtures are stored in
    temporary directories with an absolute path that identifies nothing and changes with every
    run.
    """
    try:
        return path.resolve().relative_to(STAGE_DIR).as_posix()
    except ValueError:
        return path.name


# The collection counts that read the collected trees rather than the lists beside them.
TREE_COUNTS = ("archived", "flagged_without_a_directory", "rows_without_files")


def listed_trees(collected_csv: Path, collected_dir: Path) -> list[str]:
    """The repository directories of the collected tree that belong to a repository of the
    collected list, sorted.

    A directory of a repository that the list does not name belongs to no collection that the
    list describes: The tree of a repository that left the input list, which the cloning step
    could not remove (`reuse.remove_left`), stays until a later run removes it. The collected
    file cleanup does not copy such a directory, and the collection counts and the window of
    the files' times do not read it either.
    """
    if not collected_dir.is_dir():
        return []
    listed = {repos.repo_name_to_dir(row["repo_name"]) for row in _rows(collected_csv)
              if row.get("repo_name")}
    return [name for name in repos.split(collected_dir)[0] if name in listed]


def collection_counts(collected_csv: Path, collected_dir: Path, validated_csv: Path) -> dict:
    """Counts of the sampled, flagged and archived repositories, the clone failures, and the
    repositories that the collected file cleanup kept."""
    rows = _rows(collected_csv)
    failed = {r["repo_name"] for r in _rows(collected_csv.with_name("clone_failed.csv"))}
    flags = [c for c in _header(collected_csv)
             if c not in (*heuristics.META_COLUMNS, "first_commit_sha")]
    flagged = {r["repo_name"] for r in rows
               if any((r[c] or "").upper() == "TRUE" for c in flags)}
    no_sha = {r["repo_name"] for r in rows if not (r["head_sha"] or "").strip()}
    archived = set(listed_trees(collected_csv, collected_dir))
    missing = {repos.repo_name_to_dir(r) for r in flagged} - archived
    # Missing commit metadata does not establish that cloning failed or that files are absent.
    # A retry can also retain a previously collected tree despite a recorded clone failure.
    no_files = missing | ({repos.repo_name_to_dir(r) for r in failed | no_sha} - archived)
    return {
        "sampled": len(rows),
        "flagged": len(flagged),
        "archived": len(archived),
        "clone_failed": len(failed),
        "missing_head_sha": len(no_sha),
        "flagged_without_a_directory": len(missing),
        "rows_without_files": len(no_files),
        "validated": len(_rows(validated_csv)) if validated_csv.is_file() else None,
    }


def fetched_paths(fetched_csv: Path | None) -> dict[str, set[str]]:
    """{repository directory: {repository-relative path, ...}} of the fetched files and markers
    that `fetched_csv` lists, or an empty mapping where there is no such list."""
    if fetched_csv is None or not fetched_csv.is_file():
        return {}
    found = {}
    for row in _rows(fetched_csv):
        if row.get("repo_name") and row.get("path"):
            found.setdefault(repos.repo_name_to_dir(row["repo_name"]), set()).add(row["path"])
    return found


def _artifact_times(collected_dir: Path, trees: list[str], fetched: dict[str, set[str]]):
    """Yield the write time of every collected configuration artifact in the repository
    directories `trees`, skipping symlinks, the files that a file manager wrote
    (`paths.is_discarded_path`), which carry the time of a visit, and the fetched files of
    `fetched` (`fetched_paths`), which carry the time of the fetch."""
    for repo in trees:
        tree = os.path.join(collected_dir, repo)
        left_out = fetched.get(repo, set())
        for root, dirs, files in os.walk(tree):
            dirs[:] = [d for d in dirs if d not in paths.DISCARDED_DIRECTORIES]
            for name in files:
                path = os.path.join(root, name)
                if (os.path.islink(path) or paths.is_uncollected(name)
                        or Path(os.path.relpath(path, tree)).as_posix() in left_out):
                    continue
                try:
                    yield os.lstat(path).st_mtime
                except OSError:
                    continue


def artifact_window(collected_dir: Path, collected_csv: Path,
                    fetched_csv: Path | None = None) -> dict | None:
    """First and last write time among the collected configuration artifacts of the
    repositories of the collected list (`listed_trees`), symlinks and the fetched files that
    `fetched_csv` lists excluded.

    rsync -a preserves the clone's file times, and git sets them when it writes the working
    tree, which makes them scan times. Symlinks are skipped because 7zz does not restore their
    times. A fetched file was copied into the tree of a working copy after the collection, and
    its time is the time of the fetch rather than of a scan.
    """
    if not collected_dir.is_dir():
        return None
    lo = hi = None
    # Read only the directories of the listed repositories. A stray file with a recent mtime
    # would extend the window's end. A stray file or the tree of an earlier collection with an
    # older mtime would change its start, the bound against which the published count of
    # timestamps outside the window is measured.
    for stamp in _artifact_times(collected_dir, listed_trees(collected_csv, collected_dir),
                                 fetched_paths(fetched_csv)):
        lo = stamp if lo is None or stamp < lo else lo
        hi = stamp if hi is None or stamp > hi else hi
    if lo is None:
        return None
    fmt = lambda t: datetime.datetime.fromtimestamp(  # noqa: E731
        t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"first": fmt(lo), "last": fmt(hi)}










class UncheckableTable(Exception):
    """A published CSV that this check cannot compare, because it lacks the column that
    the heuristics name as its configuration artifact column.

    Raised rather than skipped, so that a renamed column is not silently compared on
    the wrong key. Looking up the column in the heuristics by filename returns the right
    column or nothing, and a section that cannot be compared is reported as unchecked,
    with the exit code that this script documents for that case.
    """


# The columns of a published row that hold commit metadata: `last_commit_sha` is blank where
# the history of the configuration artifact was not read, and `created_at` and
# `head_commit_date` are blank where git's date does not parse (`repos.to_utc_or_blank`).
COMMIT_METADATA_COLUMNS = ("last_commit_sha", "created_at", "head_commit_date")


def metadata_counts(output_dir: Path) -> dict:
    """Configuration artifact rows across the mechanism CSVs, and the rows of the published
    mechanism CSVs with a blank in one of `COMMIT_METADATA_COLUMNS`.

    The release states the rows with missing commit metadata among the evidence limits of its
    README. The function therefore counts them in the CSVs that the release publishes
    (`PUBLISHED_MECHANISMS`), not in `other.csv`, which it withholds. It reports a mechanism
    with an absent CSV rather than skipping it, because a skipped mechanism would record zeros
    without failing. It also reports a published CSV without one of the columns, which would
    otherwise count every row.
    """
    rows = 0
    without_metadata: dict[str, int] = {}
    for name in MECHANISMS:
        CSV = output_dir / f"{name}.csv"
        if not CSV.is_file():
            raise UncheckableTable(f"{CSV.name} is missing")
        # By filename, which is unambiguous, rather than by reading the header and
        # assuming that one of its columns is the right one.
        column = heuristics.artifact_column_for_file(f"{name}.csv")
        header = _header(CSV)
        if column not in header:
            raise UncheckableTable(
                f"{CSV.name} has no column {column!r}, its configuration artifact column in "
                f"the heuristics. Its header is {header}.")
        table_rows = _rows(CSV)
        rows += len(table_rows)
        if name not in PUBLISHED_MECHANISMS:
            continue
        absent = [c for c in COMMIT_METADATA_COLUMNS if c not in header]
        if absent:
            raise UncheckableTable(f"{CSV.name} has no column {', '.join(absent)} to count its "
                                   f"rows without commit metadata. Its header is {header}.")
        for r in table_rows:
            if any(not (r[c] or "").strip() for c in COMMIT_METADATA_COLUMNS):
                without_metadata[r["repo_name"]] = without_metadata.get(r["repo_name"], 0) + 1
    return {
        "artifact_rows": rows,
        "rows_without_commit_metadata": {"total": sum(without_metadata.values()),
                                         "repositories": len(without_metadata)},
    }


def planning_counts(output_dir: Path) -> dict:
    """The rows of `planning-documents.csv`, its repositories, its rows per framework and its rows
    with a blank in one of `COMMIT_METADATA_COLUMNS`, which the release publishes and states
    among its evidence limits as it does for the mechanism CSVs. An absent CSV, or one without a
    column that the counts read, is reported rather than counted as empty."""
    table = output_dir / planning_schema.CSV_NAME
    if not table.is_file():
        raise UncheckableTable(f"{table.name} is missing")
    header = _header(table)
    absent = [c for c in ("repo_name", "framework", *COMMIT_METADATA_COLUMNS) if c not in header]
    if absent:
        raise UncheckableTable(f"{table.name} has no column {', '.join(absent)}. Its header is "
                               f"{header}.")
    rows = _rows(table)
    without = Counter(r["repo_name"] for r in rows
                      if any(not (r[c] or "").strip() for c in COMMIT_METADATA_COLUMNS))
    return {
        "rows": len(rows),
        "repositories": len({r["repo_name"] for r in rows}),
        "frameworks": dict(sorted(Counter(r["framework"] for r in rows).items())),
        "rows_without_commit_metadata": {"total": sum(without.values()),
                                         "repositories": len(without)},
    }


def imports_counts(output_dir: Path) -> dict:
    """The rows of `imports.csv`, its repositories, and its rows per tool and per status of the
    target. An absent CSV, or one without a column that the counts read, is reported rather than
    counted as empty."""
    table = output_dir / import_detection.CSV_NAME
    if not table.is_file():
        raise UncheckableTable(f"{table.name} is missing")
    header = _header(table)
    absent = [c for c in ("repo_name", "tool", "target_status") if c not in header]
    if absent:
        raise UncheckableTable(f"{table.name} has no column {', '.join(absent)}. Its header is "
                               f"{header}.")
    rows = _rows(table)
    return {
        "rows": len(rows),
        "repositories": len({r["repo_name"] for r in rows}),
        "tools": dict(sorted(Counter(r["tool"] for r in rows).items())),
        "statuses": dict(sorted(Counter(r["target_status"] for r in rows).items())),
    }


def main_write(argv=None) -> int:
    parser = cli.ArgumentParser(description='Regenerate every derivable section of output/snapshot.json, or compare the sections with --check.')
    parser.add_argument("--collected-csv", type=Path, default=DEFAULT_COLLECTED_CSV)
    parser.add_argument("--collected-dir", type=Path, default=DEFAULT_COLLECTED_DIR)
    parser.add_argument("--validated-csv", type=Path, default=DEFAULT_VALIDATED_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--fetched-csv", type=Path, default=DEFAULT_FETCHED_CSV,
                        help="List of the files fetched into the collected tree after the "
                             "collection, which the window of the files' times leaves out "
                             "(default: %(default)s)")
    parser.add_argument("--without-fetched-files", action="store_true",
                        help="Read no list of fetched files, for a collection that fetched "
                             "nothing into its tree, such as the collection of a subsample run "
                             "in a checkout that tracks the list of the development dataset")
    parser.add_argument("--check", action="store_true",
                        help="Log what would change, write nothing, and exit 2 when "
                             "something would change")
    parser.add_argument("--outputs-only", action="store_true",
                        help="Check or regenerate only the sections derived from the "
                             "output CSVs, without reading the collected tree")
    args = parser.parse_args(argv)
    failed_csv = args.collected_csv.with_name("clone_failed.csv")
    # The list describes the fetches into the collected tree of the working copy of a handover.
    # A collection that wrote its own tree, such as a subsample run in a checkout, fetched
    # nothing, and the tracked list would leave its own files out of the window.
    fetched_csv = None if args.without_fetched_files else args.fetched_csv

    # The snapshot lies in the output directory that it describes. A run over a scratch
    # directory therefore leaves the tracked `output/snapshot.json` unchanged rather than
    # recording the provenance of a scratch directory in it.
    snapshot_path = args.output_dir / "snapshot.json"
    existing = load(snapshot_path)
    changes = []
    # The sections that this run derived, written together after every section has been
    # derived, so that a run that fails its checks, such as on a commit reclassification
    # report, leaves the snapshot unchanged.
    derived = {}

    def record(name: str, section: dict, provenance: dict) -> None:
        """Compare a section and, unless checking, keep it for writing. Provenance is compared
        like any other value, and a section rebuilt from a different or truncated input counts
        as a change even when its counts are equal."""
        check_derived_from(provenance)
        if existing.get(name, {}).get("derived_from") != provenance:
            changes.append(f"{name}.derived_from")
        section["derived_from"] = provenance
        if not args.check:
            derived[name] = (section, provenance)
    # The sections that this run could not check. A section with a gitignored input is absent on
    # a clean checkout and on CI, and without a message the run would appear to confirm it.
    unchecked = []

    if args.outputs_only:
        logs.info("collection_summary", "Skipping the sections derived from the collected tree "
                                        "because of --outputs-only.")
    elif args.collected_csv.is_file() and args.collected_dir.is_dir() and failed_csv.is_file():
        section = dict(existing.get("collection", {}))
        # A handover can contain a snapshot copied from an earlier dataset. Rebuild
        # the collection description from its current input, including its scan dates.
        # Notes about reconstruction and old file timestamps do not describe this input.
        for key in ("note", "reconstructed_from", "reconstructed_on"):
            if key in section:
                del section[key]
                changes.append(f"collection.{key}")
        scan_times = sorted({row.get("scanned_at", "") for row in _rows(args.collected_csv)
                             if row.get("scanned_at", "")})
        scan_window = ({"first": scan_times[0], "last": scan_times[-1]}
                       if scan_times else None)
        if section.get("scanned_at_window") != scan_window:
            changes.append("collection.scanned_at_window")
        section["scanned_at_window"] = scan_window
        counts = collection_counts(args.collected_csv, args.collected_dir, args.validated_csv)
        window = artifact_window(args.collected_dir, args.collected_csv, fetched_csv)
        if not listed_trees(args.collected_csv, args.collected_dir):
            # A checkout contains the lists of data/collected/ and no repository's tree. Against
            # such a checkout, the counts would find no archived repository and no directory for
            # any flagged repository. The counts that read the trees keep their recorded values,
            # as the validated count does, and are logged as not checked.
            recorded = section.get("counts", {})
            for key in TREE_COUNTS:
                if key in recorded:
                    counts[key] = recorded[key]
                else:
                    del counts[key]
            names = [f"collection.counts.{key}" for key in TREE_COUNTS]
            unchecked.append(f"{', '.join(names[:-1])} and {names[-1]}, with no repository's tree in "
                             f"{provenance_key(args.collected_dir)}/")
        # Two of this block's three keys are lists named repos.csv. Outside the stage directory,
        # `provenance_key` returns the name alone, and the validated entry of `derived_from`
        # would replace the collected entry. In that case, the directory that contains each
        # file qualifies its key.
        collected_key = provenance_key(args.collected_csv)
        validated_key = provenance_key(args.validated_csv)
        if validated_key == collected_key:
            collected_key = f"{args.collected_csv.resolve().parent.name}/{collected_key}"
            validated_key = f"{args.validated_csv.resolve().parent.name}/{validated_key}"
        validated_provenance = {}
        if counts["validated"] is None:
            # `data/validated/` is gitignored, and on a clean checkout the count is None.
            # Comparing None with the recorded number would report a difference and exit 2,
            # although the input was only not read, and writing it would replace the count with
            # `null`. Keep the recorded count and its `derived_from` entry together. The count
            # alone would leave `derived_from` without a key, and `record`, which compares
            # `derived_from` like any other value, would report the section as changed.
            del counts["validated"]
            recorded = section.get("counts", {})
            if "validated" in recorded:
                counts["validated"] = recorded["validated"]
                carried = section.get("derived_from", {}).get(validated_key)
                if carried is not None:
                    validated_provenance = {validated_key: carried}
            unchecked.append(f"collection.counts.validated, because {validated_key} is missing")
        else:
            validated_provenance = {validated_key: {"rows": counts["validated"]}}
        for key, value in (("counts", counts), ("artifact_mtime_window", window)):
            if value is None:
                # The window is None when the collected tree contains no file at all, while the
                # counts are still computed. Skipping the window silently would report a passing
                # check for a sample with empty repository directories.
                unchecked.append(f"collection.{key}, because no input for it is on disk")
                continue
            merged = value
            if section.get(key) != merged:
                changes.append(f"collection.{key}")
            section[key] = merged
        record("collection", section, {
            collected_key: {"rows": counts["sampled"]},
            provenance_key(failed_csv): {"rows": len(_rows(failed_csv))},
            f"{provenance_key(args.collected_dir)}/": {"repositories": counts["archived"]},
            **validated_provenance,
            # The list of the fetched files that the window leaves out, where the working copy
            # has one.
            **({provenance_key(fetched_csv): {"rows": len(_rows(fetched_csv))}}
               if fetched_csv is not None and fetched_csv.is_file() else {}),
        })
    else:
        absent = (provenance_key(args.collected_csv) if not args.collected_csv.is_file()
                  else provenance_key(failed_csv) if not failed_csv.is_file()
                  else f"{provenance_key(args.collected_dir)}/")
        # All collection inputs are needed. Counting against an absent directory would
        # record every flagged repository as missing its files.
        unchecked.append(f"collection, because {absent} is missing")

    # The repositories that the release publishes, which `output/repos.csv` names.
    published = ({row["repo_name"] for row in _rows(args.output_dir / "repos.csv")}
                 if (args.output_dir / "repos.csv").is_file() else None)
    if published is not None:
        # The section is named `configured` rather than `sample`, because the sample is the
        # repositories that the classification labeled as engineered software projects
        # (GLOSSARY.md), and the configured repositories are a subset of the sample.
        count = len(published)
        section = {"repositories": count}
        if existing.get("configured", {}).get("repositories") != count:
            changes.append("configured.repositories")
        record("configured", section, {
            f"{args.output_dir.name}/repos.csv": {"repositories": count},
        })

        section = dict(existing.get("metadata", {}))
        try:
            counts = metadata_counts(args.output_dir)
        except UncheckableTable as e:
            # An entry of `unchecked` leads to the documented exit 1.
            unchecked.append(f"metadata, because {e}")
        else:
            for key, value in counts.items():
                if key == "rows_without_commit_metadata":
                    merged = {**section.get(key, {}), **value}
                else:
                    merged = value
                if section.get(key) != merged:
                    changes.append(f"metadata.{key}")
                section[key] = merged
            record("metadata", section, {
                f"{args.output_dir.name}/<mechanism>.csv": {
                    "rows": counts["artifact_rows"],
                    "mechanisms": len(MECHANISMS),
                },
            })

        # The planning documents, which no mechanism CSV contains and which make no repository
        # configured, in a section of their own.
        try:
            counts = planning_counts(args.output_dir)
        except UncheckableTable as e:
            unchecked.append(f"planning_documents, because {e}")
        else:
            prior = existing.get("planning_documents", {})
            if any(prior.get(key) != value for key, value in counts.items()):
                changes.append("planning_documents")
            record("planning_documents", dict(counts), {
                f"{args.output_dir.name}/{planning_schema.CSV_NAME}": {"rows": counts["rows"]},
            })

        # The imports of the configuration artifacts, which no mechanism CSV contains either.
        try:
            counts = imports_counts(args.output_dir)
        except UncheckableTable as e:
            unchecked.append(f"imports, because {e}")
        else:
            prior = existing.get("imports", {})
            if any(prior.get(key) != value for key, value in counts.items()):
                changes.append("imports")
            record("imports", dict(counts), {
                f"{args.output_dir.name}/{import_detection.CSV_NAME}": {"rows": counts["rows"]},
            })
    else:
        unchecked.append(f"sample and metadata, because {args.output_dir.name}/repos.csv is "
                         f"missing")

    report_path = args.output_dir / "commit-reclassification.json"
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            with args.collected_csv.open("rb") as stream:
                collection_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if report["collection_sha256"] != collection_digest:
                raise InvalidReport(f"{report_path.name} describes a different collection list "
                                    f"than {args.collected_csv}")
            if report.get("assembly_complete") is False:
                raise InvalidReport(f"{report_path.name} records a failed assembly of the commit "
                                    f"CSV. Correct the error that the report names, then run "
                                    f"commit-reclassification again.")
            outcomes = report["repositories"]
            counts = reclassification_step.outcome_counts(
                item["outcome"] for item in outcomes.values())
            # The reclassification step's own rule, so that every outcome that the step reports
            # as incomplete is recorded as incomplete rather than rejected as a contradiction.
            complete, population_complete = reclassification_step.coverage(counts, assembled=True)
            if report["counts"] != counts or report["complete"] != complete:
                raise InvalidReport(f"The counts or the completeness of {report_path.name} "
                                    f"differ from its repository outcomes")
            # A report written before the field was added lacks it, and the recorded section
            # then keeps its value.
            if ("population_complete" in report
                    and report["population_complete"] != population_complete):
                raise InvalidReport(f"The population_complete of {report_path.name} differs "
                                    f"from what its repository outcomes give")
            if published is None:
                unchecked.append(f"commit_reclassification, because "
                                 f"{args.output_dir.name}/repos.csv is missing")
            else:
                # The section describes the published dataset, because the release states its
                # evidence limits from it. It counts the repositories that `output/repos.csv`
                # names and the repositories in the sample that have no row because of a gap in
                # the collection. These are the clone failures (`unavailable`) and the
                # repositories with a per-repository commit record and no directory in the
                # collected tree (`missing_tree`), from which the detection step reads no file.
                # A repository that the collected file cleanup dropped has no row and no commit
                # in the release. Its outcome limits nothing that the release publishes, and if
                # it were counted, a limit could name more repositories than the release has.
                counted = {repo: item for repo, item in outcomes.items()
                           if repo in published
                           or item["outcome"] in ("unavailable", "missing_tree")}
                counts = reclassification_step.outcome_counts(
                    item["outcome"] for item in counted.values())
                complete, population_complete = reclassification_step.coverage(
                    counts, assembled=True)
                # For each outcome, the number of published repositories with shards that the step
                # left out of the commit CSVs. The detection step leaves them out too, and the
                # release publishes none of their commits. The README states their number in the
                # evidence limit of their outcome rather than in a limit of their own, so that it
                # names each repository in one evidence limit. A repository without a row, such as
                # one with the outcome `missing_tree` and a record that other commit heuristics
                # classified, has no published commits whether or not the step left its shards out,
                # and the README names it only in the evidence limit of its outcome. `left_out`
                # therefore does not count it.
                left_out = reclassification_step.outcome_counts(
                    item["outcome"] for repo, item in counted.items()
                    if item.get("left_out") and repo in published)
                # The counted repositories of each outcome that keeps evidence out of the
                # release, sorted by name.
                named = {outcome: sorted(repo for repo, item in counted.items()
                                         if item["outcome"] == outcome)
                         for outcome in NAMED_OUTCOMES}
                section = {"classified_by": report["classified_by"],
                           "collection_sha256": collection_digest,
                           "complete": complete, "counts": counts, "left_out": left_out,
                           "repositories": named}
                if "population_complete" in report:
                    section["population_complete"] = population_complete
                prior = existing.get("commit_reclassification", {})
                if any(prior.get(key) != value for key, value in section.items()):
                    changes.append("commit_reclassification")
                section["derived_from"] = prior.get("derived_from")
                record("commit_reclassification", section,
                       {f"{args.output_dir.name}/commit-reclassification.json":
                        {"repositories": len(outcomes)},
                        f"{args.output_dir.name}/repos.csv": {"repositories": len(published)}})
        except InvalidReport as error:
            logs.error("collection_summary", f"Left {snapshot_path.name} unchanged. "
                                             f"{str(error).rstrip('.')}.")
            return 1
        except (OSError, ValueError, KeyError, TypeError) as error:
            logs.error("collection_summary", f"Left {snapshot_path.name} unchanged, because "
                                             f"{report_path.name} could not be read "
                                             f"({error.__class__.__name__} {error}).")
            return 1

    if derived:
        update_sections(derived, snapshot_path, drop=RETIRED_SECTIONS)

    for note in unchecked:
        logs.info("collection_summary", f"Did not check {note.rstrip('.')}.")
    if changes:
        logs.info("collection_summary", ("Would change " if args.check else "Recomputed ")
                                        + ", ".join(changes) + ".")
        if unchecked:
            # Repeat the count here. A run that recomputed one section and could not read
            # another logs the skipped sections before the line about the written sections, and
            # exits 0. Without this line, the skipped sections would be easy to miss.
            logs.info("collection_summary", f"The {'check' if args.check else 'write'} is "
                                            f"partial. {len(unchecked):,} section group(s) above "
                                            f"were not checked.")
        return 2 if args.check else 0
    if unchecked:
        # A partial result: The sections that this run could read match, and the log names the
        # sections that it could not read. Exit 1 rather than 0, so that a CI step does not
        # treat a partial check as a check that passed.
        logs.info("collection_summary", f"{len(unchecked):,} section group(s) above were not "
                                        f"checked. Every section that could be read matches its "
                                        f"inputs.")
        return 1
    logs.info("collection_summary", "Every derivable section matches its inputs.")
    return 0


# -----------------------------------------------------------------------------
# Validating the record
# -----------------------------------------------------------------------------
DEFAULT_INPUT = DEFAULT_COLLECTED_CSV
DEFAULT_OUTPUT = STAGE_DIR / "output" / "snapshot-validation.csv"
OUTPUT_DIR = DEFAULT_OUTPUT_DIR

API_ROOT = "https://api.github.com"
USER_AGENT = "agenttorrent-pipeline snapshot-validation (+https://github.com/se-uhd/agenttorrent-pipeline)"
# GROUNDING: the documentation watch uses the same 30 s for the same job, a single HTTPS GET
# against a vendor or GitHub endpoint. Matching it keeps one timeout for this job in the package
# rather than two.
TIMEOUT_SECONDS = 30

# GROUNDING: the number of attempts per request, a ceiling rather than a tuned value.
# docs/checks/snapshot.md gives the run that grounds it.
MAX_ATTEMPTS = 4

# A recorded commit is still reachable from a ref when GitHub's compare API reports the ref as
# identical to it or ahead of it. "behind" and "diverged" both mean that the ref no longer
# contains it.
REACHABLE_STATUSES = frozenset({"identical", "ahead"})

FIELDNAMES = [
    "repo_name", "head_sha", "recorded_branch", "outcome", "reached_via",
    "compare_status", "head_commit_date", "recorded_timestamp",
    "timestamp_is_head_commit_date", "detail",
]

# The per-file commit columns in the mechanism CSVs, in order of preference.
ARTIFACT_SHA_COLUMNS = ("last_commit_sha", "first_commit_sha")


# ---------------------------------------------------------------------------
# GitHub access tokens
# ---------------------------------------------------------------------------

def collect_tokens(path: Path | None, env: dict | None = None) -> list[str]:
    """The GitHub access tokens of the environment and of the credentials file at `path`
    (`agentcontext.credentials`), in that order, without repeats."""
    return [credential.value for credential in credentials.github_tokens(path, env)]


class TokenRotator:
    """Rotate through GitHub access tokens, holding each one back until its rate limit resets."""

    def __init__(self, tokens: list[str], sleep=time.sleep, now=time.time):
        self._tokens = list(tokens)
        self._index = 0
        self._ready_at = {t: 0.0 for t in self._tokens}
        self._lock = threading.Lock()
        self._sleep = sleep
        self._now = now

    def __len__(self) -> int:
        return len(self._tokens)

    def get(self) -> str | None:
        """Return a GitHub access token that is not rate limited, waiting while every one is."""
        if not self._tokens:
            return None
        while True:
            with self._lock:
                for _ in range(len(self._tokens)):
                    token = self._tokens[self._index]
                    self._index = (self._index + 1) % len(self._tokens)
                    if self._ready_at[token] <= self._now():
                        return token
                wait = min(self._ready_at.values()) - self._now()
            if wait > 0:
                logs.info("collection_summary", f"All {len(self._tokens):,} GitHub access "
                                                f"token(s) rate limited. Waiting {wait:.0f}s.")
                # GROUNDING: re-check at least once a minute. GitHub's reset header can name a
                # later time than the actual reset, and waiting the full reported time would
                # leave every GitHub access token unused without need.
                self._sleep(min(wait, 60))
            else:
                self._sleep(0.1)

    def park(self, token: str, until: float) -> None:
        """Hold a GitHub access token back until its rate limit resets."""
        with self._lock:
            if token in self._ready_at:
                self._ready_at[token] = max(self._ready_at[token], until)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def urllib_transport(url: str, token: str | None) -> tuple[int, dict, bytes]:
    """Perform one GET. Returns (status, headers, body). Raises only on transport failure."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        **({"Authorization": f"Bearer {token}"} if token else {}),
    })
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read()


class _Headers(dict):
    """Response headers, looked up without regard to the case of their names, as HTTP defines
    them."""

    def __init__(self, headers: dict):
        super().__init__({str(name).lower(): value for name, value in headers.items()})

    def get(self, name, default=None):
        return super().get(str(name).lower(), default)


class _Response:
    """A response of the transport as `github_api.rate_limit_wait` reads it, through `headers`
    and `json()`."""

    def __init__(self, headers: dict, body: bytes):
        self.headers, self._body = _Headers(headers or {}), body

    def json(self):
        return json.loads(self._body) if self._body else None


class GitHubClient:
    """GET requests that rotate GitHub access tokens, wait for rate limits, and retry transport
    failures."""

    def __init__(self, rotator: TokenRotator, transport=urllib_transport,
                 sleep=time.sleep, now=time.time, max_attempts: int = MAX_ATTEMPTS):
        self._rotator = rotator
        self._transport = transport
        self._sleep = sleep
        self._now = now
        self._max_attempts = max_attempts

    def get(self, path: str) -> tuple[int, object]:
        """GET one API path. Returns (status, decoded body) or (0, error string)."""
        url = f"{API_ROOT}{path}"
        last = "no attempt made"
        attempt = -1
        # Waiting for a reset is not an attempt: Otherwise a pool in which every GitHub access
        # token is rate limited could use up the retries without ever reaching the server and
        # report ERROR with the detail "no attempt made".
        rate_limited = 0
        while attempt + 1 < self._max_attempts + rate_limited:
            attempt += 1
            token = self._rotator.get()
            try:
                status, headers, body = self._transport(url, token)
            except Exception as e:  # transport failure, not an HTTP status
                last = f"{e.__class__.__name__} ({e})"
                # GROUNDING: 1 s, 2 s, 4 s and 8 s after the four attempts, and a request that
                # is going to fail costs at most 15 s of waiting plus its own timeouts. The
                # 30 s cap applies from the sixth pass of the loop on. A MAX_ATTEMPTS of 6 reaches
                # that pass, and so do rate-limited responses, each of which adds a pass.
                self._sleep(min(2 ** attempt, 30))
                continue

            wait = (github_api.rate_limit_wait(_Response(headers, body), clock=self._now)
                    if status in (403, 429) else None)
            if wait is not None:
                if token:
                    self._rotator.park(token, self._now() + wait)
                rate_limited += 1
                last = f"rate limited {rate_limited} time(s) without reaching the server"
                if rate_limited > 4 * max(1, len(self._rotator)):
                    # The rotator already waits for the soonest reset. Reaching this line
                    # therefore means that every GitHub access token is exhausted, not briefly
                    # limited.
                    return 0, last
                continue
            if 500 <= status < 600:
                last = f"HTTP {status}"
                self._sleep(min(2 ** attempt, 30))  # same backoff as the transport path
                continue

            try:
                return status, json.loads(body) if body else None
            except ValueError:
                return status, None
        return 0, last



# ---------------------------------------------------------------------------
# Checking one repository
# ---------------------------------------------------------------------------

def compare(client: GitHubClient, repo: str, base: str, head: str) -> tuple[int, object]:
    """Query GitHub's compare API for how `head` relates to `base`."""
    ref = f"{urllib.parse.quote(base, safe='')}...{urllib.parse.quote(head, safe='')}"
    return client.get(f"/repos/{repo}/compare/{ref}")


def _instant(value: str):
    """An ISO 8601 timestamp as an aware UTC datetime, or None where it does not parse.

    The collected list keeps the committer's offset, as git's `%cI` writes it, and GitHub
    returns UTC. The check compares the two as instants, because as strings the same commit in
    another offset would be a mismatch, and an offset timestamp would sort outside a UTC window.
    A value without an offset is read as UTC, as the steps write it. The function uses the
    steps' own conversion, `repos.utc_instant`, so that an offset of 24 hours or more, which git
    prints for a commit that records one, gives the commit's instant here too.
    """
    try:
        return repos.utc_instant(value or "")
    except ValueError:
        return None


def _same_instant(a: str, b: str) -> bool:
    """Whether two timestamps name the same instant. False where either does not parse."""
    first, second = _instant(a), _instant(b)
    return first is not None and first == second


def head_commit_date(payload: object) -> str:
    """The committer date of the base commit in a compare response."""
    if not isinstance(payload, dict):
        return ""
    base = payload.get("base_commit") or {}
    return ((base.get("commit") or {}).get("committer") or {}).get("date", "") or ""


def check_repository(client: GitHubClient, row: dict, artifact_shas: list[str]) -> dict:
    """Decide one repository's outcome.

    Zero requests for a repository with no recorded commit, one for the common case, and three
    when the branch was renamed. For a repository with an unreachable head, the check tries the
    SHAs of its configuration artifacts, one request each. The number of distinct commits at
    which its files were recorded, rather than a constant, bounds the worst case.
    """
    repo = row["repo_name"]
    head_sha = (row.get("head_sha") or "").strip()
    branch = (row.get("branch") or "").strip()
    recorded = (row.get("head_commit_date") or "").strip()

    result = {
        "repo_name": repo, "head_sha": head_sha, "recorded_branch": branch,
        "outcome": "", "reached_via": "", "compare_status": "",
        "head_commit_date": "", "recorded_timestamp": recorded,
        "timestamp_is_head_commit_date": "", "detail": "",
    }

    if not head_sha:
        result["outcome"] = "NO_SHA"
        result["detail"] = "the cloning step recorded no commit for this repository"
        return result

    def record(payload, via, outcome):
        result["compare_status"] = (payload or {}).get("status", "") if isinstance(payload, dict) else ""
        date = head_commit_date(payload)
        if date:
            result["head_commit_date"] = date
            result["timestamp_is_head_commit_date"] = str(_same_instant(date, recorded)).upper()
        result["reached_via"] = via
        result["outcome"] = outcome
        return result

    # 1. The branch recorded at collection time.
    if branch:
        status, payload = compare(client, repo, head_sha, branch)
        if status == 0:
            result["outcome"] = "ERROR"
            result["detail"] = str(payload)
            return result
        if status not in (200, 404, 422):
            # GitHub returns 404 and 422 when the ref or the commit does not exist, which is a
            # result. Any other status means that the request failed.
            result["outcome"] = "ERROR"
            result["detail"] = f"unexpected HTTP {status} comparing against {branch}"
            return result
        if status == 200 and not isinstance(payload, dict):
            result["outcome"] = "ERROR"
            result["detail"] = f"comparing against {branch} returned a non-object body"
            return result
        if status == 200 and isinstance(payload, dict):
            if payload.get("status") in REACHABLE_STATUSES:
                return record(payload, f"branch:{branch}", "OK")
            result["compare_status"] = payload.get("status", "")
            date = head_commit_date(payload)
            if date:
                result["head_commit_date"] = date
                result["timestamp_is_head_commit_date"] = str(_same_instant(date, recorded)).upper()

    # 2. The repository's current default branch.
    status, payload = client.get(f"/repos/{repo}")
    if status == 0:
        result["outcome"] = "ERROR"
        result["detail"] = str(payload)
        return result
    if status == 404:
        result["outcome"] = "REPO_GONE"
        result["detail"] = "the repository is no longer readable"
        return result
    if status != 200 or not isinstance(payload, dict):
        # 401 from an expired GitHub access token, 403 from a repository that GitHub blocks, 451
        # from a takedown, or a proxy returning HTML. None of these statuses shows anything about the
        # repository, and falling through here would publish every one of them as GONE.
        result["outcome"] = "ERROR"
        result["detail"] = f"unexpected HTTP {status} from /repos/{repo}"
        return result
    default_branch = payload.get("default_branch", "")
    if default_branch and default_branch != branch:
        status, payload = compare(client, repo, head_sha, default_branch)
        if status == 0:
            result["outcome"] = "ERROR"
            result["detail"] = str(payload)
            return result
        if status not in (200, 404, 422):
            result["outcome"] = "ERROR"
            result["detail"] = f"unexpected HTTP {status} comparing against {default_branch}"
            return result
        if status == 200 and not isinstance(payload, dict):
            result["outcome"] = "ERROR"
            result["detail"] = f"comparing against {default_branch} returned a non-object body"
            return result
        if status == 200 and isinstance(payload, dict) and payload.get("status") in REACHABLE_STATUSES:
            out = record(payload, f"default_branch:{default_branch}", "RENAMED")
            out["detail"] = f"recorded branch was {branch or '(none)'}"
            return out

    # 3. A configuration artifact's own commit, which can survive a rewrite of the history that
    # removed the recorded head commit.
    target = default_branch or branch
    for sha in artifact_shas:
        if not sha or sha == head_sha or not target:
            continue
        status, payload = compare(client, repo, sha, target)
        if status == 0:
            result["outcome"] = "ERROR"
            result["detail"] = str(payload)
            return result
        if status not in (200, 404, 422):
            result["outcome"] = "ERROR"
            result["detail"] = (f"unexpected HTTP {status} comparing a configuration artifact's "
                                f"commit")
            return result
        if status == 200 and not isinstance(payload, dict):
            result["outcome"] = "ERROR"
            result["detail"] = ("comparing a configuration artifact's commit returned a "
                                "non-object body")
            return result
        if status == 200 and isinstance(payload, dict) and payload.get("status") in REACHABLE_STATUSES:
            out = record(payload, f"artifact_sha:{sha[:12]}", "RESCUED_BY_FILE_SHA")
            out["detail"] = ("the recorded head commit is gone, and a configuration artifact's "
                             "commit is reachable")
            out["head_commit_date"] = ""
            out["timestamp_is_head_commit_date"] = ""
            return out

    result["outcome"] = "GONE"
    result["detail"] = ("neither the head commit nor any configuration artifact's commit is "
                        "reachable")
    return result


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

def load_rows(input_csv: Path) -> list[dict]:
    with open(input_csv, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_artifact_shas(output_dir: Path) -> dict[str, list[str]]:
    """Map each repository to the distinct per-file commit SHAs recorded for it.

    The output CSVs are named from the heuristics rather than globbed, because a glob that
    matches nothing returns an empty map without an error.
    """
    shas: dict[str, list[str]] = {}
    for filename in heuristics.csv_filenames():
        path = Path(output_dir) / filename
        if not path.is_file():
            continue
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                repo = row.get("repo_name")
                if not repo:
                    continue
                bucket = shas.setdefault(repo, [])
                for column in ARTIFACT_SHA_COLUMNS:
                    value = (row.get(column) or "").strip()
                    if value and value not in bucket:
                        bucket.append(value)
    return shas


def in_scope(rows: list[dict], collected_dir: Path) -> list[dict]:
    """Every repository that the cloning step wrote files for, and every row without files.

    The rows without files are repositories that the collected file cleanup emptied and removed
    or that the cloning step could not clone. They are in scope because their rows are still
    their only record, and a row that cannot be checked should be reported as such rather than
    skipped.
    """
    collected = set()
    if Path(collected_dir).is_dir():
        collected = set(repos.split(collected_dir)[0])
    flag_columns = [c for c in (rows[0] if rows else {})
                    if c not in (*heuristics.META_COLUMNS, "first_commit_sha")]
    scoped = []
    for row in rows:
        directory = repos.repo_name_to_dir(row["repo_name"])
        flagged = any((row.get(c) or "").upper() == "TRUE" for c in flag_columns)
        if directory in collected or flagged or not (row.get("head_sha") or "").strip():
            scoped.append(row)
    return scoped


def load_done(output_csv: Path) -> dict[str, dict]:
    """Read results from an earlier run so that an interrupted run resumes."""
    if not Path(output_csv).is_file():
        return {}
    with open(output_csv, newline="", encoding="utf-8") as f:
        return {row["repo_name"]: row for row in csv.DictReader(f)
                if row.get("outcome") and row["outcome"] != "ERROR"}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def outside_window(rows: list[dict], window: dict | None) -> int | None:
    """The number of rows with a recorded timestamp outside the collection window.

    A timestamp outside the span from the first configuration artifact file written to the last
    is not a scan time. The bound is the instant, not the calendar day: A row on the window's
    first day that precedes the first file written that day is outside the scan as well. The
    check needs no network access and covers the rows for which the compare endpoint returned
    no result. The recorded value keeps its committer's offset and the window is UTC, and the
    function compares them as instants. A recorded value that does not parse is not counted.
    """
    if not window or not window.get("first") or not window.get("last"):
        return None
    first, last = _instant(window["first"]), _instant(window["last"])
    if first is None or last is None:
        return None
    moments = [_instant(r.get("recorded_timestamp") or "") for r in rows
               if (r.get("recorded_timestamp") or "").strip()]
    return sum(1 for moment in moments if moment is not None and not first <= moment <= last)


def summarize(results: list[dict], collection_window: dict | None = None) -> dict:
    """Counts, the rescue rate, and the timestamp finding."""
    outcomes = Counter(r["outcome"] for r in results)
    # NO_SHA has no commit to check, and ERROR is a request that failed rather than a
    # repository that did. Neither belongs in the denominator of a survival rate.
    checkable = sum(v for k, v in outcomes.items() if k not in ("NO_SHA", "ERROR"))
    survived = outcomes["OK"] + outcomes["RENAMED"] + outcomes["RESCUED_BY_FILE_SHA"]
    dated = [r for r in results if r.get("head_commit_date")]
    matching = [r for r in dated if r.get("timestamp_is_head_commit_date") == "TRUE"]
    # The equality can be tested only where the commit still exists and has a date, which
    # limits it to the rows that survived. Naming the other rows keeps the finding from being
    # read as a statement about the whole sample, and the window check below covers them
    # without a commit.
    not_compared = [r for r in results if not r.get("head_commit_date")]
    finding = {
        "compared": len(dated),
        "matching": len(matching),
        "not_compared": {
            "count": len(not_compared),
            "outcomes": dict(sorted(Counter(r["outcome"] for r in not_compared).items())),
            "note": "No commit to read a date from. The equality is untested on these rows.",
        },
        "note": ("The pipeline documented this column as the time of the scan. It contains the "
                 "committer date of head_sha, and the compare response reports that date "
                 "independently."),
    }
    outside = outside_window(not_compared, collection_window)
    if outside is not None:
        finding["not_compared"]["outside_the_collection_window"] = outside
        finding["not_compared"]["window_note"] = (
            "Counted against the span from the first configuration artifact file written to the "
            "last, not against the calendar days of the span. A timestamp before the first file "
            "was written is not a scan time either.")
    return {
        "checked": len(results),
        "outcomes": dict(sorted(outcomes.items())),
        "reachable_or_rescued": survived,
        "reachable_or_rescued_rate": round(survived / checkable, 4) if checkable else None,
        "gone": sorted(r["repo_name"] for r in results
                       if r["outcome"] in ("GONE", "REPO_GONE")),
        "timestamp_is_head_commit_date": finding,
    }


def summarize_only(args) -> int:
    """Re-derive the summary from a completed result file, without a request.

    The validation writes the result file row by row as results arrive, and an interrupted run,
    a crash or an earlier ``--limit`` leaves a file that parses correctly and is short.
    Summarizing that file over a complete record would drop repositories from the ``gone``
    list, which is the only place that names them, and would record the shortened result with
    the complete run's date. The function therefore checks the rows against the scope that the
    input defines before it writes anything.
    """
    if not args.output.is_file():
        logs.error("collection_summary", f"Found no result file to summarize at {args.output}.")
        return 1
    with open(args.output, newline="", encoding="utf-8") as f:
        results = list(csv.DictReader(f))
    if not results:
        logs.error("collection_summary", f"{args.output} contains no results.")
        return 1

    if not args.input.is_file():
        logs.error("collection_summary", f"{args.input} does not exist. Name the input that "
                                         f"defines the scope with --input.")
        return 1
    with open(args.input, newline="", encoding="utf-8") as f:
        scope = {r["repo_name"] for r in in_scope(list(csv.DictReader(f)), args.collected_dir)}
    # Compare by name rather than by count. A count also matches when one repository left the
    # scope and another entered, or when rows are duplicated. A header-only input makes the
    # count zero, and then no comparison can fail.
    checked = {r["repo_name"] for r in results}
    missing = scope - checked
    if missing:
        shown = ", ".join(sorted(missing)[:5])
        logs.error("collection_summary", f"{args.output} has results for {len(checked):,} of the "
                                         f"{len(scope):,} repositories in scope and none for "
                                         f"{len(missing):,}, such as {shown}. Run the check again "
                                         f"to finish it.")
        return 1
    if not scope:
        logs.error("collection_summary", f"No repository of {args.input} is in scope to check "
                                         f"the results against.")
        return 1

    # The snapshot beside the result file, for the reason that main_write gives: A run over a
    # scratch directory would otherwise rewrite the tracked output/snapshot.json.
    snapshot_path = args.output.resolve().parent / "snapshot.json"
    window = (load(snapshot_path).get("collection", {}) or {}).get("artifact_mtime_window")
    summary = summarize(results, window)
    errors = summary["outcomes"].get("ERROR", 0)
    if errors and not args.record_with_errors:
        logs.error("collection_summary", f"{args.output} has {errors:,} repository/repositories "
                                         f"that could not be checked. Run the check again, or "
                                         f"pass --record-with-errors.")
        return 1
    if args.no_snapshot:
        logs.info("collection_summary", json.dumps(summary["timestamp_is_head_commit_date"], indent=2))
        return 0
    # Keep the date on which the run gathered the results, not the date of this summary. The
    # marker that states that the record is a lower bound comes from the result file, as every
    # other value here does. The file covers the scope, as checked above, and is therefore
    # incomplete exactly when it contains ERROR rows.
    previous = load(snapshot_path).get("validation", {})
    record = {**summary}
    if previous.get("checked_on"):
        record["checked_on"] = previous["checked_on"]
    if errors:
        record["incomplete"] = (
            f"{errors:,} repository/repositories could not be checked (recorded with "
            f"--record-with-errors). The counts below are a lower bound.")
    update("validation", record, snapshot_path, derived_from={
        provenance_key(args.output): {"rows": len(results), "in_scope": len(scope)}})
    logs.info("collection_summary", f"Re-derived the summary from {args.output.name} and "
                                    f"recorded it in {snapshot_path.name}.")
    return 0


def main_validate(argv=None) -> int:
    parser = cli.ArgumentParser(description='Check every repository in the collection against GitHub and record the validation section of output/snapshot.json.')
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--collected-dir", type=Path, default=DEFAULT_COLLECTED_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR,
                        help="Directory of the mechanism CSVs, read for the commit SHAs of "
                             "each configuration artifact (default: %(default)s)")
    parser.add_argument("--credentials", type=Path, default=credentials.default_path(),
                        help="Credentials file with GitHub access tokens, read after "
                             "GITHUB_TOKENS and GITHUB_TOKEN (default: %(default)s)")
    # GROUNDING: four concurrent requests per GitHub access token, capped at 16. The rate limit
    # does not bound the run: GitHub allows each GitHub access token 5,000 requests per hour,
    # and a run needs about one request per repository in scope. The cap limits the load on
    # GitHub's API rather than keeping the run within the rate limit.
    parser.add_argument("-w", "--workers", type=int, default=0,
                        help="Concurrent requests (default: 4 per GitHub access token, capped at 16)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Check only the first N in-scope repositories")
    parser.add_argument("--fresh", action="store_true",
                        help="Ignore an existing result file instead of resuming")
    parser.add_argument("--no-snapshot", action="store_true",
                        help="Do not update output/snapshot.json")
    parser.add_argument("--summarize-only", action="store_true",
                        help="Derive the summary again from an existing result file and record "
                             "it, without requests to GitHub (after a change to the summary)")
    parser.add_argument("--record-with-errors", action="store_true",
                        help="Record the run in output/snapshot.json even when some "
                             "repositories could not be checked (the section records their "
                             "count)")
    args = parser.parse_args(argv)

    if args.summarize_only:
        return summarize_only(args)

    if not args.input.is_file():
        logs.error("collection_summary", f"The list of the repositories to check, {args.input}, "
                                         f"does not exist.")
        return 1

    tokens = collect_tokens(args.credentials)
    if not tokens:
        logs.error("collection_summary", f"No GitHub access token found. Add one to "
                                         f"{args.credentials}, or set GITHUB_TOKENS.")
        return 1
    workers = args.workers or min(16, max(1, 4 * len(tokens)))
    logs.info("collection_summary", f"Checking with {len(tokens):,} GitHub access token(s) and "
                                    f"{workers:,} worker(s).")

    rows = load_rows(args.input)
    scoped = in_scope(rows, args.collected_dir)
    # The whole scope, kept before --limit narrows it. Recording the narrowed number as the
    # scope would let a trial run with --limit record itself as complete.
    # The run compares the scope by name, not by count. A resumed run seeds results from the
    # existing file, which can contain rows for repositories that are no longer in scope, and a
    # sufficient row count then does not show that the right repositories were checked.
    scope_names = {r["repo_name"] for r in scoped}
    if args.limit:
        scoped = scoped[:args.limit]
    artifact_shas = load_artifact_shas(args.output_dir)
    done = {} if args.fresh else load_done(args.output)
    pending = [r for r in scoped if r["repo_name"] not in done]
    logs.info("collection_summary", f"{len(scoped):,} repositories in scope, {len(done):,} "
                                    f"already done, {len(pending):,} to check.")

    rotator = TokenRotator(tokens)
    client = GitHubClient(rotator)
    results = list(done.values())

    # Rewrite the result file rather than append to it. Resuming checks every ERROR row again,
    # and appending would leave two rows for the same repository with the stale failure first,
    # where a reader that takes the first match would see the failure and not the result.
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # The rows that this run read go into a temporary file beside the result file, and the
    # temporary file replaces the result file before the first request. Every result is then
    # appended to the result file itself and flushed. Opening the result file for writing would
    # truncate the rows that this run has just read from it. Writing the whole session into the
    # temporary file would avoid the truncation but lose every result that an interrupted run
    # had already obtained, which is worse: A run can wait up to an hour for a rate limit to
    # reset, and that is when a person stops it.
    partial = args.output.with_suffix(args.output.suffix + ".partial")
    lock = threading.Lock()
    completed = 0

    with open(partial, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        for row in done.values():
            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})
        f.flush()
    os.replace(partial, args.output)

    with open(args.output, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, lineterminator="\n")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(check_repository, client, row,
                            artifact_shas.get(row["repo_name"], [])): row
                for row in pending
            }
            for future in as_completed(futures):
                result = future.result()
                with lock:
                    writer.writerow(result)
                    f.flush()
                    results.append(result)
                    completed += 1
                    if completed % 250 == 0:
                        logs.info("collection_summary", f"  Checked {completed:,}/"
                                                        f"{len(pending):,}...")

    # Read the window from the snapshot rather than computing it again, because it comes from
    # the files that the cloning step wrote. The snapshot is the one beside the result file, as
    # in `summarize_only`.
    snapshot_path = args.output.resolve().parent / "snapshot.json"
    window = (load(snapshot_path).get("collection", {}) or {}).get("artifact_mtime_window")
    summary = summarize(results, window)
    for outcome, count in summary["outcomes"].items():
        logs.info("collection_summary", f"  {outcome:20} {count:,}")
    rate = summary["reachable_or_rescued_rate"]
    # Log the share only when it exists, and the count in every case: A run with nothing
    # checkable, such as a --limit over clone failures or a run in which every request failed,
    # still logs the count.
    share = f" ({rate:.2%} of the checkable repositories)" if rate is not None else ""
    logs.info("collection_summary", f"{summary['reachable_or_rescued']:,} repositories are "
                                    f"reachable or rescued{share}.")
    ts = summary["timestamp_is_head_commit_date"]
    logs.info("collection_summary", f"The timestamp equals the head commit date for "
                                    f"{ts['matching']:,} of {ts['compared']:,} compared "
                                    f"repositories.")
    skipped = ts["not_compared"]
    outside = skipped.get("outside_the_collection_window")
    logs.info("collection_summary", f"  {skipped['count']:,} repositories have no commit to read "
                                    f"a date from and were not compared"
                                    + (f" ({outside:,} of them with a timestamp outside the "
                                       f"collection window)" if outside is not None else "")
                                    + ".")
    if summary["gone"]:
        logs.info("collection_summary", f"{len(summary['gone']):,} repositories are gone.")
        for repo in summary["gone"]:
            logs.info("collection_summary", f"  {repo} is gone.")

    errors = summary["outcomes"].get("ERROR", 0)
    # Taken after the run, so that it reflects what was checked. `results` starts with the
    # rows of the existing file and grows as results arrive.
    checked_names = {r["repo_name"] for r in results}

    if not args.no_snapshot:
        if errors and not args.record_with_errors:
            # The snapshot is provenance, and a run with failed requests has checked fewer
            # repositories than the run that it would overwrite. Writing it would replace a
            # complete record with a partial record, and the previous values are not
            # recoverable from anywhere else. A repository that fails the same way on every
            # attempt would block every later record, and --record-with-errors exists for that
            # case.
            logs.error("collection_summary", f"Not recorded in {SNAPSHOT_PATH.name}, because "
                                             f"{errors:,} repository/repositories could not be "
                                             f"checked. Run the command again to retry them, or "
                                             f"pass --record-with-errors to record the "
                                             f"incomplete run.")
        elif (checked_names != scope_names) and not args.record_with_errors:
            # --limit narrows the scope, and the record cannot state it: It would have today's
            # date, a rate computed over the limited repositories, and an empty gone list, which
            # is the only place that names a lost repository. summarize_only refuses a short
            # result file for the same reason.
            logs.error("collection_summary", f"Not recorded in {SNAPSHOT_PATH.name}, because the "
                                             f"results cover {len(checked_names & scope_names):,} "
                                             f"of the {len(scope_names):,} repositories in scope, "
                                             f"with {len(scope_names - checked_names):,} missing "
                                             f"and {len(checked_names - scope_names):,} no longer "
                                             f"in scope. Run the command again without --limit, "
                                             f"or pass --record-with-errors to record a partial "
                                             f"run.")
        else:
            record = {**summary, "checked_on": time.strftime("%Y-%m-%d")}
            if checked_names != scope_names:
                record["incomplete"] = (
                    f"Covers {len(checked_names & scope_names):,} of the {len(scope_names):,} "
                    f"repositories in scope (recorded with --record-with-errors). The counts "
                    f"below are a lower bound.")
            # Not `if errors:` alone, because both branches write the same key, and a partial
            # scope with one error would then lose the sentence that states how much of the
            # scope the run covers.
            if errors and not record.get("incomplete"):
                record["incomplete"] = (
                    f"{errors:,} repository/repositories could not be checked (recorded with "
                    f"--record-with-errors). The counts below are a lower bound.")
            update("validation", record, snapshot_path, derived_from={
                provenance_key(args.output): {"rows": len(results), "in_scope": len(scope_names)}})
            logs.info("collection_summary", f"Recorded the validation section in "
                                            f"{snapshot_path.name}"
                                            + (f" and marked it incomplete ({errors:,} "
                                               f"repositories unchecked)" if errors else "")
                                            + ".")

    if errors:
        logs.warning("collection_summary", f"{errors:,} repository/repositories could not be "
                                           f"checked because a request failed. A failed request "
                                           f"does not show that a repository left GitHub. Run "
                                           f"the command again to retry them.")
        return 1
    return 0


SUBCOMMANDS = {
    "write": main_write,
    'validation': main_validate,
}


SUBCOMMAND_SUMMARY = 'write regenerates every derivable section, or compares the sections with --check, and validation checks every repository against GitHub'


def main(argv=None) -> int:
    return cli.subcommand_main("snapshot", SUBCOMMANDS, SUBCOMMAND_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
