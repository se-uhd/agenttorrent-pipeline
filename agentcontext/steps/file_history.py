#!/usr/bin/env python3
"""Measure the Git columns that the detection step could not fill from the cache.

`file_history.csv` keeps the measurements, keyed on
`(repo_name, head_sha, mechanism, artifact)`. The cloning step fills it while each clone is
still on disk, and the detection step joins it. After a sample collected in one pass, this step
has nothing left to measure. It measures the rest: the files of a tree that came without a
cache, of a repository with a history that could not be read the first time, and of a sample
after its clones were deleted.

The step writes each row that it measures to the cache and, in place, back into the CSV that
the detection step wrote. It does the same with the row's four AI columns, in
`ai_authorship.csv` and `context-files.csv`, which it derives from the commits that the git
columns come from. A file with a history that the cloning step could not read reaches this step
with both sets of columns blank, and no other step writes the AI columns. Each file has one
writer, one line terminator and one set of keys, because the step writes no second CSV beside
an `output/<mech>.csv`.

`--offline` fetches nothing and fails with the rows that the cache cannot fill. A run with it
shows, in a checkout without credentials, whether the CSVs are complete.
`--offline-except OWNER/REPO` fetches that repository's rows and fails on every other cache
miss in the same way. The subsample run makes the history fetch of one repository fail in the
cloning step and passes that repository with this flag, so that this step measures it, as a
container run does after a network failure stopped a history fetch. Every other row still has
to come from the cloning step's own measurement.

The output CSVs are planned before any of them is written, so that a repository is cloned once
for all of its rows in them. A repository can have configuration artifacts under more than one
mechanism, and building one CSV at a time would clone it once per CSV.

On a cache miss, the step checks out the clone at `head_sha`, the commit at which the repository
was collected. It caches the file history and the AI authorship only after it has fetched the
complete commit history and verified the checkout. When the commit is unavailable, the row
keeps its existing measurements and gets no new one.

The step is the last of a run that writes the two caches, and it removes from both the rows at
a head that neither the collected list nor the validated list names (`current_heads`). No later
step reads such a row, and the next collection takes a repository over only at the head that
the collected list names. The tracked caches therefore grow with the sample rather than with
every head that a collection measured.

Inputs:
  - ``output/<mech>.csv``, the mechanism CSVs that the detection step wrote, and
    ``output/planning-documents.csv``, which has the same Git columns
  - ``file_history.csv``, the cache of the Git columns
  - ``ai_authorship.csv``, the cache of the AI columns
  - ``data/validated/``, for the pruning below, and its list, ``data/validated/repos.csv``
  - ``data/collected/repos.csv``, the collected list

Outputs:
  - ``output/<mech>.csv`` and ``output/planning-documents.csv``, with the Git columns of every
    row that it measured filled, and without the planning documents of a repository that the
    reconciled ``repos.csv`` no longer lists
  - ``file_history.csv``, with the rows that this run measured, and without the rows at a head
    that neither the collected list nor the validated list names
  - ``ai_authorship.csv``, with the AI columns of the same files, and without the rows at such
    a head
  - ``output/repos.csv``, reconciled against the output CSVs after pruning
  - ``output/all_ai_commits.csv`` and ``output/other_ai_commits.csv``, without the rows of a
    repository that the reconciled ``repos.csv`` no longer lists

``--cache``, ``--ai-authorship`` and ``--collected-csv`` default to the directory above
``--output-dir``, as ``--validated-dir`` does, so that a run over another output directory does
not change the stage's caches.

Exit codes: 0 when every CSV was written and verified, 1 when the step could not run or the
verification failed, 4 when the network did not respond after every wait of the probe, and 130
when the step was stopped.
"""

import csv
import io
import os
import shutil
import signal
import sys
import tempfile
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from agentcontext import STAGE_DIR
from agentcontext import cli
from typing import Optional

from agentcontext import logs, heuristics
from agentcontext import caches, commits, guards
from agentcontext.git import auth, bounds
from agentcontext import paths, planning_schema, repos
from agentcontext.git import history
from agentcontext.git.history import measure_repo


# -------------------------------
# What each CSV contains
# -------------------------------
# The columns that this step fills. The detection step writes every other column of a
# mechanism CSV, and this step writes their values back as it read them.
FILLED_COLUMNS = heuristics.HISTORY_COLUMNS

# The CSVs with Git columns that this step fills: the mechanism CSVs and
# `planning-documents.csv`. Each entry maps the key under which `file_history.csv` records the
# CSV's rows to its filename and the column that names the file. Planning documents are no
# configuration artifacts and are therefore in no mechanism CSV.
HISTORY_TABLES = {**heuristics.MECHANISM_CSVS,
                  planning_schema.HISTORY_KEY: (planning_schema.CSV_NAME,
                                                planning_schema.PATH_COLUMN)}


def table_header(key: str) -> list:
    """The header that the detection step writes for the CSV of `HISTORY_TABLES[key]`."""
    if key == planning_schema.HISTORY_KEY:
        return planning_schema.csv_header()
    return heuristics.csv_header(key)

# The columns with a date value, which are normalized to UTC before writing.
TIMESTAMP_COLUMNS = ("head_commit_date", "created_at")


def read_table(path: Path) -> tuple:
    """One CSV as (header, rows), read through the standard library, which keeps every value a
    string.

    The function does not use pandas, because its default NA handling turns `n/a` into NaN,
    and a subagents row with the `memory` value `n/a` would then be published blank.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_table(path: Path, columns: list, rows: list) -> None:
    """Write one CSV, replacing it in a single step.

    Raises ValueError, and writes nothing, for a row with a field that the header does not
    name. `DictReader` stores the fields past the end of the header under the key None, and
    the writer below writes only the header's columns. Without the check, such a row would
    lose its extra fields and the run would exit 0.
    """
    duplicates = [c for c in columns if list(columns).count(c) > 1]
    if duplicates:
        raise ValueError(f"The header of {path} names {sorted(set(duplicates))} twice. One "
                         f"column would overwrite the other.")
    named = set(columns)
    for number, row in enumerate(rows, start=1):
        if None in row:
            raise ValueError(f"Row {number} of {path} has more fields than the header. Re-run "
                             f"the detection step.")
        unnamed = sorted(set(row) - named)
        if unnamed:
            raise ValueError(f"The header of {path} lacks the column(s) {', '.join(unnamed)} of "
                             f"row {number}. Writing the row would drop their values.")
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=list(columns), lineterminator="\n",
                            extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    # Remove the temporary files that a killed process left beside the target. The `except`
    # branch below handles a raised exception, but not a SIGKILL, an OOM kill or the `os._exit`
    # with which a stop handler ends the process. Each temporary file has a random name, and
    # the next run would write another one and never remove the old one. The targets are
    # beside tracked files, where a leftover file fails `release`'s inventory and shows in the
    # next `git status`.
    for stale in path.parent.glob(path.name + ".*.tmp"):
        stale.unlink(missing_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text.getvalue())
            # Sync the data before the rename and the directory after it, as
            # `caches.replace_file` does. If the power fails before the runner records this
            # step, a rename that reached the disk before the data would leave an empty CSV of
            # the detection step. This step could not read that CSV at any later Start, while
            # the detection step stays recorded.
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    caches.sync_directory(path.parent)


def to_utc(rows: list, where="") -> list:
    """Normalize every date column in place to the format of the published CSVs, and blank a
    value that does not parse, with a warning that names the repositories.

    Returns the blanked values, as `repos.to_utc_or_blank` does. Git prints the date of a
    commit in the year 10000 as a value that does not parse, and `file_history.csv` keeps git's
    text for it. Refusing such a value would stop every run at the same repository.
    """
    blanked = repos.to_utc_or_blank(rows, TIMESTAMP_COLUMNS)
    if blanked:
        logs.warning('file_history', repos.blanked_dates_message(str(where), blanked))
    return blanked


# -------------------------------
# Measuring one repository
def _result_of(future, repo_name: str) -> tuple:
    """One repository's result or, when its measurement raised, the exception logged as an
    error and the outcome `git_unavailable`, which records a repository that could not be
    measured.

    `future.result()` re-raises the exception, and no caller catches it. Without this
    function, one `OSError` would end the CSV being built and every CSV after it.
    """
    try:
        return future.result()
    except Exception as e:  # noqa: BLE001 - one repository must not end the run
        logs.error('file_history', f"Measuring the git columns of {repo_name} raised "
                                   f"{e.__class__.__name__} ({e}).")
        return {}, "git_unavailable"


def _prune_from_repos(validated_dir: Path, repo_name: str) -> None:
    """Remove from the validated tree a repository that git reports as gone.

    The runner's step list declares this deletion with `mutates=("collected_file_cleanup",)`.
    Only `collected_file_cleanup` restores the repository, by deriving `data/validated/` again
    from the unchanged collected tree.

    The step deletes no single file as untracked, that is, without commit history and not on
    disk. It keeps a published row with a file that has no history at the recorded commit,
    because deleting such files would delete rows of the dataset.
    """
    repo_dir = Path(validated_dir) / repos.repo_name_to_dir(repo_name)
    if repo_dir.exists():
        # DSM can write an `@eaDir` into the tree during the removal (`paths.remove_tree`).
        paths.remove_tree(repo_dir)
        # Log the full path rather than a fixed prefix, so that the message names the directory
        # that the run changed wherever the tree is.
        logs.info('file_history', f"    Deleted {repo_dir}.")


# -------------------------------
# repos.csv reconciliation
# -------------------------------
# fill_table() prunes the rows of every repository that git reports as gone when this step
# clones it again. That pruning leaves output/repos.csv, which agentcontext/steps/detection.py
# wrote earlier, unchanged. Without the reconciliation below, a repository that loses every
# detected configuration artifact would remain in repos.csv without rows, and a repository that
# loses some of its configuration artifacts would keep stale TRUE flags for what was pruned.

# The tool flags are the union of the `tool` column of a repository's rows, which the
# detection step wrote from the tree. For a workflow, the column names the tool of the action
# that the workflow runs. It names the declaring tool for a declared file and the plugin's tool
# for a plugin part, and it also counts a file behind a symlink marker for the linking tool.
# Flags recomputed from the paths would name the wrong tool for a workflow that runs another
# tool's action, because by its path every workflow belongs to `.github/workflows`. Read from
# the column, the flags equal the tools of the rows, which `output_cross_check` checks.
from agentcontext.heuristics import value_tools
from agentcontext.steps.detection import (file_flags_of, no_specific_tool, preferred_facts,
                                          tool_flags_of)


def _artifacts_by_repo(csv_path: Path, path_col: str) -> dict:
    """Map each repo_name to its [(path, tools), ...] in one mechanism CSV after the pruning,
    with the tools read from its `tool` column."""
    result: dict = {}
    if not csv_path.is_file():
        return result
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "tool" not in (reader.fieldnames or ()):
            raise ValueError(
                f"The header of {csv_path} lacks the tool column. The output CSVs are from an "
                f"earlier dataset version. Re-run the detection step.")
        for row in reader:
            try:
                tools = value_tools(row["tool"])
            except ValueError as e:
                raise ValueError(f"Could not read the tool of {row[path_col]} of "
                                 f"{row['repo_name']} in {csv_path} ({e})") from e
            result.setdefault(row["repo_name"], []).append((row[path_col], tools))
    return result


def reconcile_repos_csv(output_dir: Path) -> tuple[int, list]:
    """Derive repos.csv again from the pruned mechanism CSVs.

    A repository keeps its row, with recomputed flags, if any configuration artifact remains,
    and is dropped if none does. The mechanism flags record which mechanism CSVs still contain
    a row of the repository. The file flags are `file_flags_of` its context files, and the
    tool flags are the union of the `tool` column of its rows. The header and the other
    columns, such as scanned_at, branch and github_link, are kept from the existing repos.csv.
    The commit CSVs and `planning-documents.csv` then lose the rows of every repository that
    the rewritten list does not contain (`restrict_commit_csvs`,
    `restrict_planning_documents`). Run on mechanism CSVs that were not pruned, the function
    reproduces repos.csv and the commit CSVs unchanged, which shows that the reconciliation
    itself changes nothing.

    Returns (kept_count, dropped_repo_names).
    """
    output_dir = Path(output_dir)
    repos_csv = output_dir / "repos.csv"
    with open(repos_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        existing = {row["repo_name"]: row for row in reader}

    by_mechanism = {mechanism: _artifacts_by_repo(output_dir / filename, column)
                    for mechanism, (filename, column) in heuristics.MECHANISM_CSVS.items()}

    kept_rows = []
    dropped = []
    for repo_name, row in existing.items():
        found = {mechanism: rows.get(repo_name, []) for mechanism, rows in by_mechanism.items()}
        if not any(found.values()):
            dropped.append(repo_name)
            continue
        # Only the rows of the mechanisms set the tool flags, as in the detection step, and an
        # `other` row sets none.
        tool_flags = tool_flags_of(tools for mechanism, artifacts in found.items()
                                   if mechanism != "other" for _path, tools in artifacts)
        file_flags = file_flags_of([path for path, _tools in found["context_file"]])
        # `plugins` and `lsp` are rebuilt like the other mechanism flags. Copied from the
        # detection step, they would stay TRUE for a repository after the rebuild dropped its
        # plugin rows.
        mechanism_flags = {mechanism: bool(artifacts) for mechanism, artifacts in found.items()}
        new_flags = {
            **{c: str(v).upper() for c, v in tool_flags.items()},
            **{c: str(v).upper() for c, v in file_flags.items()},
            **{c: str(v).upper() for c, v in mechanism_flags.items()},
            "no_specific_tool": str(no_specific_tool(
                [v for c, v in mechanism_flags.items() if c != "other"],
                tool_flags)).upper(),
        }
        kept_rows.append({**row, **new_flags})

    # The header has to name every flag that the rebuild computes. An `output/` written under
    # older heuristics can lack a newer flag, and `verify_tables` does not check the header of
    # repos.csv. This function checks it, with a message that names the cause.
    seen = {column for row in kept_rows for column in row}
    if None in seen:
        # `DictReader`'s restkey: a row with more fields than the header has.
        raise ValueError(
            f"A row of {repos_csv} has more fields than the header. Re-run the detection "
            f"step.")
    absent = sorted(seen - set(fieldnames))
    if absent:
        raise ValueError(
            f"The header of {repos_csv} lacks the computed column(s) {', '.join(absent)}. The "
            f"output CSVs and repos.csv are from different dataset versions. Re-run the "
            f"detection step.")
    # Write through `write_table`, which replaces the file in one step. Writing in place would
    # truncate the published repos.csv first, and a failure inside the rewrite, such as a full
    # disk, would leave it empty or short of rows while the mechanism CSVs stay complete.
    write_table(repos_csv, fieldnames, kept_rows)
    restrict_commit_csvs(output_dir, {row["repo_name"] for row in kept_rows})
    restrict_planning_documents(output_dir, {row["repo_name"] for row in kept_rows})

    return len(kept_rows), dropped


# The commit CSVs that the detection step restricts to the repositories that it publishes.
COMMIT_CSVS = ("all_ai_commits.csv", commits.OTHER_AI_COMMITS_NAME)


def restrict_commit_csvs(output_dir: Path, kept: set) -> None:
    """Drop the rows of the commit CSVs for the repositories that `repos.csv` no longer lists.

    The commit CSVs describe the repositories of `repos.csv`, as the detection step restricts
    them. The rows to drop therefore come from the mechanism CSVs, in which a pruned repository
    has no row left, and not from the repositories that this run found gone. After a run that
    stops between writing the mechanism CSVs and writing the commit CSVs, a rerun finds no
    repository gone, and the commit rows would stay. An absent commit CSV stays absent, and a
    commit CSV with nothing to drop is not rewritten.

    The function reads and writes the CSVs a row at a time, because the commit CSVs grow with
    every collection and, held in memory with their messages, take many times their size.

    It first removes the temporary files that a killed `write_retained` left, also where
    nothing is dropped. The release refuses an output directory with a file that it does not
    publish, and no other step writes these names.
    """
    for filename in COMMIT_CSVS:
        path = Path(output_dir) / filename
        for stale in caches.temporary_files(path):
            stale.unlink(missing_ok=True)
        if not path.is_file():
            continue
        dropped = rows_to_drop(path, kept)
        if dropped:
            write_retained(path, kept)
            logs.info('file_history', f"Dropped {dropped:,} row(s) of {filename} for "
                                      f"repositories that repos.csv no longer lists.")


def _repo_column(path: Path, columns: list) -> int:
    """The position of `repo_name` in the header of a commit CSV."""
    if "repo_name" not in columns:
        raise ValueError(f"The header of {path} lacks repo_name (the header is {columns}). "
                         f"Could not restrict the rows to the repositories that repos.csv "
                         f"lists.")
    return columns.index("repo_name")


def rows_to_drop(path: Path, kept: set) -> int:
    """How many rows of the commit CSV at `path` name a repository outside `kept`."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        columns = next(reader, [])
        column = _repo_column(path, columns)
        return sum(1 for row in reader if row and (row[column] if column < len(row) else "")
                   not in kept)


def write_retained(path: Path, kept: set) -> None:
    """Rewrite the commit CSV at `path` with the rows of the repositories in `kept`, a row at a
    time. As `write_table` does, it replaces the file in a single step.

    Raises ValueError for a row with more fields than the header and leaves the file as it was,
    as `write_table` does."""
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                         suffix=".tmp")
    try:
        with open(path, newline="", encoding="utf-8") as source, \
                os.fdopen(handle, "w", encoding="utf-8", newline="") as target:
            reader = csv.reader(source)
            columns = next(reader, [])
            column = _repo_column(path, columns)
            writer = csv.writer(target, lineterminator="\n")
            writer.writerow(columns)
            for number, row in enumerate(reader, start=1):
                if not row:
                    continue
                if len(row) > len(columns):
                    raise ValueError(f"Row {number} of {path} has more fields than the "
                                     f"header. Re-run the detection step.")
                if (row[column] if column < len(row) else "") in kept:
                    writer.writerow(row + [""] * (len(columns) - len(row)))
            # Sync the data before the rename and the directory after it, as `write_table` does.
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    caches.sync_directory(path.parent)


def restrict_planning_documents(output_dir: Path, kept: set) -> None:
    """Drop the rows of `planning-documents.csv` for the repositories that `repos.csv` no longer
    lists.

    The dataset records planning documents only for the configured repositories, and a planning
    document does not make a repository configured. A repository that lost all of its
    configuration artifacts to the pruning therefore loses its planning documents too, as it
    loses its commits. The CSV is rewritten only when a row is dropped.
    """
    path = Path(output_dir) / planning_schema.CSV_NAME
    if not path.is_file():
        return
    columns, rows = read_table(path)
    retained = [row for row in rows if row["repo_name"] in kept]
    if len(retained) != len(rows):
        write_table(path, columns, retained)
        logs.info('file_history', f"Dropped {len(rows) - len(retained):,} row(s) of "
                                  f"{planning_schema.CSV_NAME} for repositories that repos.csv "
                                  f"no longer lists.")


# -------------------------------
# The heads that the caches keep
# -------------------------------
def current_heads(collected_csv: Path, validated_csv: Path) -> set:
    """The `(repo_name, head_sha)` of every row of the collected list and of the validated list,
    the heads at which a later step or the next collection reads the two caches.

    The next collection takes a repository over only at the head that the collected list names
    for it (`reuse.outdated`), also where a failed clone kept the earlier collection. The
    detection step joins the caches at the heads of the validated list, and the output CSVs
    that this step fills have the same heads. After a run, every head of the validated list is
    also in the collected list. The function reads the validated list too, so that the rows
    that the detection step reads stay when the validated tree comes from another collection
    than the collected list beside it. A clone failure has a blank head, which names no row.

    Raises ValueError for a list without a `repo_name` or a `head_sha` column, and OSError for a
    list that cannot be read."""
    heads = set()
    for path in (Path(collected_csv), Path(validated_csv)):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            absent = [c for c in ("repo_name", "head_sha") if c not in (reader.fieldnames or ())]
            if absent:
                raise ValueError(f"The header of {path} lacks {', '.join(absent)} (the header "
                                 f"is {reader.fieldnames}). Could not tell which rows of the "
                                 f"caches a later step reads.")
            heads.update((row["repo_name"], row["head_sha"]) for row in reader)
    return heads


def retain_heads(cache, heads: set) -> tuple:
    """`cache`, a `caches.FileHistoryCache` or a `caches.AiAuthorshipCache`, with only the rows
    at a `(repo_name, head_sha)` of `heads`, and the number of rows removed. A cache that lost
    rows is returned as a new cache with the same path, marked `dirty` so that the next save
    writes it. A cache that lost none is returned unchanged.

    The function is here rather than a method in `caches.py`, because `caches.py` belongs to
    the collector's code (`reuse.COLLECTOR_MODULE`), and a change to that code needs a version
    with its reach in `code_versions.COLLECTOR`."""
    kept = {key: values for key, values in cache.items()
            if (key.repo_name, key.head_sha) in heads}
    removed = len(cache) - len(kept)
    if not removed:
        return cache, 0
    retained = type(cache)(kept, path=cache.path)
    retained.dirty = True
    return retained, removed


def without_superseded(cache) -> tuple:
    """`cache`, a `caches.FileHistoryCache`, without its superseded rows, and the number of rows
    removed, returned as `retain_heads` returns its cache.

    A row that the file history step measured for a mechanism is superseded where the cloning
    step has a complete row of the same path at the same commit (`preferred_facts`)."""
    collected = {(key.repo_name, key.head_sha, key.artifact) for key, facts in cache.items()
                 if not key.mechanism and facts.is_complete()}
    kept = {key: facts for key, facts in cache.items()
            if not key.mechanism or (key.repo_name, key.head_sha, key.artifact) not in collected}
    removed = len(cache) - len(kept)
    if not removed:
        return cache, 0
    retained = type(cache)(kept, path=cache.path)
    retained.dirty = True
    return retained, removed


# -------------------------------
# Per-CSV build
# -------------------------------
def _plan_misses(mechanism: str, rows: list, artifact_column: str,
                 cache: caches.FileHistoryCache, retry_missing_history: bool) -> tuple:
    """Split one CSV's rows into the rows that the cache fills and the rows to fetch.

    Returns `(known_facts, missing_by_repo, retried)`. `known_facts` maps the row key to its
    facts. A row with a path that the cache has under another mechanism is filled from that
    entry, because the history of a path does not change when a heuristic reclassifies it.
    `retried` contains the keys of the misses that the cache has with blank values, which
    `retry_missing_history` fetches again.
    """
    known_facts, missing, retried = {}, defaultdict(list), set()
    for row in rows:
        artifact = row[artifact_column]
        facts = preferred_facts(cache, row["repo_name"], row["head_sha"], mechanism, artifact)
        if facts is None:
            facts = cache.for_artifact(row["repo_name"], row["head_sha"], artifact)
        if facts is not None and not (retry_missing_history and not facts.is_complete()):
            known_facts[(row["repo_name"], artifact)] = facts
            continue
        if facts is not None:
            retried.add((row["repo_name"], artifact))
        missing[(row["repo_name"], row["head_sha"], row["head_commit_date"],
                 row["branch"])].append(artifact)
    return known_facts, missing, retried


def record_ai_authorship(ai_cache: "caches.AiAuthorshipCache | None", repo_name: str,
                         head_sha: str, measured: dict, path_histories: dict) -> None:
    """Record the four AI columns of every file that one repository's measurement filled.

    The AI columns come from the commits that the Git columns came from, through the same
    three queries that the cloning step runs, in a clone checked out at `head_sha`. The cloning
    step writes these rows from its own history queries. When it could not read a file's
    history, because the time bound or the size bound stopped the fetch or one query failed,
    the file reaches this step with the Git columns to measure and the AI columns blank, and
    this step measures both.
    """
    if ai_cache is None:
        return
    for artifact in measured:
        if artifact in path_histories:
            ai_cache.put(caches.AiAuthorshipKey(repo_name, head_sha, artifact),
                         commits.ai_authorship_from(path_histories[artifact]))


def measure_missing(plans: dict, cache: caches.FileHistoryCache, workers: int,
                    github_token: Optional[str], sleep=time.sleep,
                    scratch_dir=None, ai_cache=None, only=None) -> dict:
    """Clone each repository with a missing row once, for the missing rows of every CSV.

    Every mechanism CSV is planned before the measurement. A repository can have rows in
    several mechanism CSVs, and the pass measures all of its missing rows in one clone and
    records them together.

    Returns the error per repository, so that the CSVs written afterwards do not clone again a
    repository on which this pass already failed. `ai_cache`, when given, receives the AI
    columns of every measured file (`record_ai_authorship`). `only`, a set of
    `(repo_name, head_sha)`, limits the pass to the repositories that it names.
    """
    wanted = defaultdict(set)
    for plan in plans.values():
        for repo_key, artifacts in plan["missing"].items():
            if only is None or repo_key[:2] in only:
                wanted[repo_key].update(artifacts)
    if not wanted:
        return {}

    logs.info('file_history', f"Measuring {sum(len(a) for a in wanted.values()):,} row(s) "
                              f"across {len(wanted):,} repositories.")
    outcomes = {}
    # The pool is shut down by hand rather than by a `with` block, because the block's exit
    # waits for every submitted clone and on Ctrl+C would delay the interrupt by up to
    # GIT_TIMEOUT_SECONDS per worker.
    pool = ThreadPoolExecutor(max_workers=workers)
    path_histories_by_repo = defaultdict(dict)
    try:
        futures = {
            pool.submit(measure_repo, repo_name, head_sha, head_commit_date, branch,
                        sorted(artifacts), github_token, sleep, scratch_dir,
                        path_histories_by_repo[(repo_name, head_sha)]):
            (repo_name, head_sha)
            for (repo_name, head_sha, head_commit_date, branch), artifacts in wanted.items()
        }
        for future in as_completed(futures):
            repo_name, head_sha = futures[future]
            measured, error = _result_of(future, repo_name)
            outcomes[(repo_name, head_sha)] = error
            record_ai_authorship(ai_cache, repo_name, head_sha, measured,
                                 path_histories_by_repo.pop((repo_name, head_sha), {}))
            for artifact, facts in measured.items():
                for mechanism, plan in plans.items():
                    if artifact in plan["artifacts_by_repo"].get((repo_name, head_sha),
                                                                 ()):
                        cache.put(caches.FileHistoryKey(repo_name, head_sha, mechanism,
                                                        artifact), facts)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        pool.shutdown(wait=True)
    return outcomes


def retry_unavailable(plans: dict, attempted: dict, cache: caches.FileHistoryCache,
                      workers: int, github_token: Optional[str], sleep=time.sleep,
                      scratch_dir=None, ai_cache=None, save=lambda: None) -> dict:
    """Measure again the repositories that `attempted`, the outcomes of a pass, leaves
    `git_unavailable`, in up to `guards.RETRY_ROUNDS` rounds with a pause of
    `guards.RETRY_PAUSE_SECONDS` before each, and return their latest outcomes. The cloning
    step collects again in the same way what a period of GitHub failures left. `save` writes
    the caches after each round.

    A period of failures that ends before the pass does, such as the bursts of HTTP 400
    responses that the cloning step receives to its fetches (docs/steps/repository_cloning.md),
    leaves the probe of `wait_out_an_outage` responding. Without a second attempt, the rows
    would stay blank and the step would exit 0. A clone or a history fetch that the time bound
    or the size bound stopped (`bound_exceeded`) is not measured again, because it would exceed
    the same bound."""
    outcomes = dict(attempted)
    for round_number in range(1, guards.RETRY_ROUNDS + 1):
        again = {key for key, error in outcomes.items() if error == "git_unavailable"}
        if not again:
            break
        logs.info('file_history', f"Measuring {len(again):,} repositories again in "
                                  f"{logs.hms(guards.RETRY_PAUSE_SECONDS)}, round {round_number} "
                                  f"of {guards.RETRY_ROUNDS}.")
        sleep(guards.RETRY_PAUSE_SECONDS)
        outcomes.update(measure_missing(plans, cache, workers, github_token, sleep, scratch_dir,
                                        ai_cache, only=again))
        save()
    return {key: outcomes[key] for key in outcomes if attempted.get(key) != outcomes[key]}


class NetworkDown(Exception):
    """git reached no server for the repositories that a pass could not clone, and the probe
    of `guards.PROBE_URL` failed after every wait of `guards.PROBE_WAITS`."""


def wait_out_an_outage(plans: dict, attempted: dict, cache: caches.FileHistoryCache,
                       workers: int, github_token: Optional[str], sleep=time.sleep,
                       scratch_dir=None, ai_cache=None) -> dict:
    """When the probe of `guards.PROBE_URL` fails, measure again, once the network responds,
    the repositories that `attempted`, the outcomes of a pass, leaves `git_unavailable`, and
    return their outcomes. The probe is repeated after each wait of `guards.PROBE_WAITS`, and
    `NetworkDown` is raised when every probe fails.

    Without the wait, an outage of the network or of GitHub's git service during the pass would
    leave those rows blank and the step would exit 0. The runner would then record the step as
    complete, and the release would publish the blank rows. When only the repositories fail,
    the probe responds and the rows stay as they are."""
    unavailable = {key for key, error in attempted.items() if error == "git_unavailable"}
    if not unavailable or guards.remote_responds(guards.PROBE_URL, github_token, False):
        return {}
    for wait in guards.PROBE_WAITS:
        logs.warning('file_history', f"Git could not clone {len(unavailable):,} repositories, "
                                     f"and {guards.PROBE_URL} does not respond. Probing again in "
                                     f"{logs.hms(wait)}.")
        sleep(wait)
        if guards.remote_responds(guards.PROBE_URL, github_token, False):
            logs.info('file_history', f"{guards.PROBE_URL} responds. Measuring the "
                                      f"{len(unavailable):,} repositories again.")
            return measure_missing(plans, cache, workers, github_token, sleep, scratch_dir,
                                   ai_cache, only=unavailable)
    raise NetworkDown(f"Git could not clone {len(unavailable):,} repositories, and "
                      f"{guards.PROBE_URL} did not respond after the waits of "
                      f"{', '.join(logs.hms(wait) for wait in guards.PROBE_WAITS)}")


def plan_table(mechanism: str, output_dir: Path, cache: caches.FileHistoryCache,
               retry_missing_history: bool) -> dict:
    """Read one CSV of `HISTORY_TABLES` and determine which of its rows the cache cannot fill."""
    filename, artifact_column = HISTORY_TABLES[mechanism]
    table_path = Path(output_dir) / filename
    columns, rows = read_table(table_path)
    # Every column that the lookup and the key of a cache miss read. Without this check, a CSV
    # that lacks one of them would raise a KeyError inside the planner that names only the
    # column.
    required = ["repo_name", artifact_column, "head_sha", "head_commit_date", "branch"]
    absent = [c for c in required if c not in columns]
    if absent:
        raise ValueError(f"{table_path} lacks the column(s) {absent}. The header is {columns}.")
    # Reject a row with more fields than the header here, before any CSV is written.
    # `write_table` would reject it inside the write loop, after the CSVs before this one had
    # been rewritten.
    longer = next((number for number, row in enumerate(rows, start=1) if None in row), None)
    if longer is not None:
        raise ValueError(f"Row {longer} of {table_path} has more fields than the header. "
                         f"Re-run the detection step.")
    known_facts, missing, retried = _plan_misses(mechanism, rows, artifact_column, cache,
                                              retry_missing_history)
    artifacts_by_repo = defaultdict(set)
    for (repo_name, head_sha, _, _), artifacts in missing.items():
        artifacts_by_repo[(repo_name, head_sha)].update(artifacts)

    # The values that the CSV already has, kept for a row that this run cannot measure. A clone
    # that the time bound or the size bound stopped is a failure of the run, and blanking the
    # row for it would replace a measurement with nothing.
    previous = {(row["repo_name"], row[artifact_column]): caches.FileHistory.from_row(row)
                for row in rows}

    return {"mechanism": mechanism, "filename": filename,
            "artifact_column": artifact_column, "table_path": table_path,
            "columns": columns, "rows": rows,
            "known_facts": known_facts, "missing": missing, "retried": retried,
            "previous": previous, "artifacts_by_repo": artifacts_by_repo}


def retry_only(plans: dict) -> set:
    """The repositories for which every miss, over all the plans, is a row that the cache has
    with blank values.

    When a clone finds such a repository gone, the step keeps its rows rather than pruning
    them. The rows were published before, and a retry tests whether the history can be read
    now, not whether the repository still exists. A repository that GitHub no longer serves,
    such as `chainapsis/keplr-wallet`, can have blank rows in the cache that an earlier
    collection filled.
    """
    missed, retried = set(), set()
    for plan in plans.values():
        for (repo_name, _, _, _), artifacts in plan["missing"].items():
            for artifact in artifacts:
                if (repo_name, artifact) in plan["retried"]:
                    retried.add(repo_name)
                else:
                    missed.add(repo_name)
    return retried - missed


def fill_table(mechanism: str, output_dir: Path, cache: caches.FileHistoryCache,
               validated_dir: Path, workers: int, github_token: Optional[str],
               offline: bool, retry_missing_history: bool,
               sleep=time.sleep, plan: dict = None,
               attempted: dict = None, gone: set = None, scratch_dir=None,
               ai_cache=None) -> dict:
    """Fill the Git columns of one CSV of `HISTORY_TABLES` in place, and its AI columns where
    it has them.

    `build_all` passes `plan`, the plan of the rows, and `attempted`, the repositories that its
    one measuring pass has already tried. Called without them, as the tests call it for one
    CSV, the function plans and measures by itself. `ai_cache`, when given, receives the AI
    columns of what this call measures, and the function fills the AI columns of every row
    that the cache contains, as the detection step joins them. A row that the cache does not
    contain keeps what the detection step wrote.
    """
    plan = plan if plan is not None else plan_table(mechanism, output_dir, cache,
                                                    retry_missing_history)
    attempted = attempted or {}
    gone = gone or set()
    filename = plan["filename"]
    artifact_column = plan["artifact_column"]
    table_path = plan["table_path"]
    columns, rows = plan["columns"], plan["rows"]
    known_facts = dict(plan["known_facts"])
    missing = {k: v for k, v in plan["missing"].items()
               if (k[0], k[1]) not in attempted}
    no_history = 0
    # Prune every row of a repository that git reports as gone, not only the rows that missed
    # the cache. Pruning only the misses would delete `data/validated/<repo>` while the output
    # CSVs kept the rows that the cache could fill and `output/repos.csv` kept the repository
    # with its flags set.
    pruned = {(row["repo_name"], row[artifact_column]) for row in rows
              if row["repo_name"] in gone}

    # Resolve the rows that the measuring pass already tried here rather than fetch them again.
    for repo_key, artifacts in plan["missing"].items():
        repo_name, head_sha = repo_key[0], repo_key[1]
        if (repo_name, head_sha) not in attempted or repo_name in gone:
            continue
        for artifact in artifacts:
            facts = cache.get(caches.FileHistoryKey(repo_name, head_sha, mechanism,
                                                    artifact))
            if facts is not None and facts.is_complete():
                known_facts[(repo_name, artifact)] = facts
                continue
            no_history += 1
            known_facts[(repo_name, artifact)] = plan["previous"].get(
                (repo_name, artifact), caches.FileHistory())

    if missing and offline:
        rows_missing = sum(len(v) for v in missing.values())
        raise ValueError(
            f"{rows_missing:,} row(s) of {filename} in {len(missing):,} repositories are not "
            f"in the cache, and --offline does not clone their repositories. Fill the cache, "
            f"or run without --offline.")

    if missing:
        logs.info('file_history', f"  Measuring {sum(len(v) for v in missing.values()):,} "
                                  f"row(s) of {filename} across {len(missing):,} repositories.")
        path_histories_by_repo = defaultdict(dict)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(measure_repo, repo_name, head_sha, head_commit_date, branch,
                            artifacts, github_token, sleep, scratch_dir,
                            path_histories_by_repo[(repo_name, head_sha)]):
                (repo_name, head_sha, artifacts)
                for (repo_name, head_sha, head_commit_date, branch), artifacts
                in missing.items()
            }
            for future in as_completed(futures):
                repo_name, head_sha, artifacts = futures[future]
                measured, error = _result_of(future, repo_name)
                record_ai_authorship(ai_cache, repo_name, head_sha, measured,
                                     path_histories_by_repo.pop((repo_name, head_sha), {}))
                if error == "clone_failed" and repo_name not in retry_only({"": plan}):
                    for row in rows:
                        if row["repo_name"] == repo_name:
                            pruned.add((repo_name, row[artifact_column]))
                    _prune_from_repos(validated_dir, repo_name)
                    continue
                if error:
                    for artifact in artifacts:
                        no_history += 1
                        known_facts[(repo_name, artifact)] = plan["previous"].get(
                            (repo_name, artifact), caches.FileHistory())
                    continue
                for artifact, facts in measured.items():
                    known_facts[(repo_name, artifact)] = facts
                    cache.put(caches.FileHistoryKey(repo_name, head_sha, mechanism,
                                                    artifact), facts)

    has_ai = ai_cache is not None and all(c in columns for c in heuristics.AI_COLUMNS)
    kept = []
    for row in rows:
        key = (row["repo_name"], row[artifact_column])
        if key in pruned:
            continue
        out = dict(row)
        facts = known_facts.get(key)
        if facts is not None:
            # A Git column that the header lacks is not added. `verify_tables` names such a
            # header after the writes, and `write_table` rejects a field that no column names.
            out.update({column: value for column, value in facts.as_row().items()
                        if column in FILLED_COLUMNS and column in columns})
        if has_ai:
            values = ai_cache.get(caches.AiAuthorshipKey(row["repo_name"], row["head_sha"],
                                                         row[artifact_column]))
            if values is not None:
                out.update(values.as_row())
        kept.append(out)

    blanked = to_utc(kept, table_path)
    # Keep the detection step's row order, because this step rewrites a CSV that it does not
    # own.
    write_table(table_path, columns, kept)

    return {"mechanism": mechanism, "rows": len(kept), "pruned": len(pruned),
            "fetched": sum(len(v) for v in plan["missing"].values()),
            "no_history": no_history,
            "undated": {(row["repo_name"], row[artifact_column])
                        for row, column, _ in blanked if column == "created_at"}}


def build_all(output_dir: Path, cache_path: Path, validated_dir: Path, workers: int,
              github_token: Optional[str], offline: bool,
              retry_missing_history: bool, sleep=time.sleep, scratch_dir=None,
              ai_cache_path: Path | None = None,
              offline_except: frozenset = frozenset(), *, heads: set) -> list:
    """Write every output CSV, saving the caches after each CSV and when a CSV raises.

    `ai_cache_path` is `ai_authorship.csv`. It receives the AI columns of every file that this
    run measures, and the step fills the AI columns of `context-files.csv` from it. None leaves
    both unchanged. `offline_except` names the repositories that an offline run may still
    fetch. `heads` are the `(repo_name, head_sha)` at which the caches keep their rows
    (`current_heads`). Both caches lose every other row before the first CSV is planned, and
    the first save writes them without those rows.

    The function makes three passes, because a repository with configuration artifacts under
    several mechanisms would otherwise be cloned once per CSV. It plans every CSV first, clones
    each repository once for all of its missing rows, and then writes the output CSVs from the
    filled cache.
    """
    cache = caches.FileHistoryCache.load(cache_path)
    logs.info('file_history', f"{cache_path.name} has {len(cache):,} rows, "
                              f"{len(cache.incomplete()):,} of them without a history.")
    ai_cache = (caches.AiAuthorshipCache.load(ai_cache_path) if ai_cache_path is not None
                else None)
    # Remove the rows before the plans, so that every lookup reads only the kept rows. The
    # `dirty` mark of a cache that lost rows makes the first save write it without them, also in
    # a run that measures nothing.
    removed = {}
    cache, removed[cache_path.name] = retain_heads(cache, heads)
    cache, superseded = without_superseded(cache)
    if superseded:
        logs.info('file_history', f"Removed {superseded:,} rows of {cache_path.name} that a "
                                  f"later measurement of the cloning step superseded (same path, "
                                  f"same commit).")
    if ai_cache is not None:
        ai_cache, removed[ai_cache_path.name] = retain_heads(ai_cache, heads)
    if any(removed.values()):
        logs.info('file_history', "Removed "
                                  + " and ".join(f"{count:,} rows of {name}"
                                                 for name, count in removed.items())
                                  + " at heads that neither the collected list nor the "
                                    "validated list names.")
    before = len(cache)

    def save_caches() -> None:
        """Save each cache that this run changed, the AI cache first.

        Where `file_history.csv` has no row for a path, the next run measures the path again and
        rewrites its AI row. A row of `file_history.csv` without its AI row would instead leave
        the AI columns of the path blank until the repository is collected again, because no
        lookup of a later run reads the AI cache."""
        if ai_cache is not None and ai_cache.dirty:
            ai_cache.save(ai_cache_path)
        if cache.dirty:
            cache.save(cache_path)

    # Plan every CSV, and check the header of every mechanism CSV, before any repository is
    # cloned. The check below has to fail before any CSV is written. Raised from inside the
    # loop, it would leave the output CSVs that the loop had reached rewritten and the rest as
    # they were, a state that is neither the old output nor the new one.
    plans = {mechanism: plan_table(mechanism, output_dir, cache, retry_missing_history)
             for mechanism in HISTORY_TABLES}
    short = {m: {key: artifacts for key, artifacts in plan["missing"].items()
                 if key[0] not in offline_except}
             for m, plan in plans.items()}
    short = {m: missing for m, missing in short.items() if missing}
    if short and offline:
        rows = sum(len(v) for plan in short.values() for v in plan.values())
        raise ValueError(
            f"{rows:,} row(s) in {len(short)} output CSV(s) "
            f"({', '.join(sorted(HISTORY_TABLES[m][0] for m in short))}) are not in the cache, "
            f"and --offline does not clone their repositories. Fill the cache, or run without "
            f"--offline.")
    try:
        # Offline, every remaining cache miss belongs to a repository of `offline_except`.
        # Measuring every miss therefore measures only those repositories.
        attempted = ({} if offline and not offline_except else
                     measure_missing(plans, cache, workers, github_token, sleep,
                                     scratch_dir, ai_cache))
        # Save before the retry rounds and the waits for the network below, which can be long,
        # and before the removal of the gone repositories, which a stop or a failed removal can
        # end. The measurements of the pass are then on disk whatever happens in either. The
        # save is inside this `try`, so that a stop during the save leads to a second save
        # below. That save completes, because the stop handler ignores a later signal.
        save_caches()
        if not offline:
            attempted.update(retry_unavailable(plans, attempted, cache, workers, github_token,
                                               sleep, scratch_dir, ai_cache, save_caches))
            attempted.update(wait_out_an_outage(plans, attempted, cache, workers, github_token,
                                                sleep, scratch_dir, ai_cache))
            save_caches()
    except BaseException:
        # Ctrl+C included. The runner forwards SIGINT and SIGTERM to the step that it is
        # running, and a signal is the ordinary way in which an operator stops a long pass. The
        # caches keep what the pass measured.
        if cache.dirty:
            logs.info('file_history', f"Saving {cache_path.name} with {len(cache):,} rows "
                                      f"({len(cache) - before:+,d}) before stopping.")
        save_caches()
        raise
    gone = {repo_name for (repo_name, _), error in attempted.items()
            if error == "clone_failed"} - retry_only(plans)
    # Remove the gone repositories once per run, not once per CSV. A repository that git
    # reports as gone is gone for every CSV, including an output CSV with rows that the cache
    # fills, for which no clone runs.
    for repo_name in sorted(gone):
        _prune_from_repos(validated_dir, repo_name)
    summaries = []
    try:
        for mechanism in HISTORY_TABLES:
            summaries.append(fill_table(mechanism, output_dir, cache, validated_dir,
                                        workers, github_token, offline,
                                        retry_missing_history, sleep=sleep,
                                        plan=plans[mechanism],
                                        attempted=attempted, gone=gone,
                                        scratch_dir=scratch_dir, ai_cache=ai_cache))
            # Save after each CSV rather than once at the end, so that a failure in one CSV
            # keeps the measurements of the CSVs before it.
            save_caches()
        # `reconcile_repos_csv` prunes the commit CSVs later, from what the mechanism CSVs
        # contain, and not here from `gone` (`restrict_commit_csvs` gives the reason).
    except BaseException:
        save_caches()
        raise
    if len(cache) != before:
        logs.info('file_history', f"{cache_path.name} has {len(cache):,} rows after this run "
                                  f"({len(cache) - before:+,d}).")
    return summaries


# -------------------------------
# Verification
# -------------------------------
def verify_tables(output_dir: Path, undated: frozenset = frozenset()) -> bool:
    """Check every CSV of `HISTORY_TABLES`, and log what the check finds rather than fix it.

    The check tests the header and whether every measured row has the columns that this step
    fills. It counts the rows with blank Git columns, the rows on which a measurement failed,
    and logs them without failing, because a row with a history that the pipeline could not
    read is not a configuration artifact that is absent. `undated` holds
    `(mechanism, repo_name, artifact)` for each row with a `created_at` that the run blanked
    because git's date does not parse (`to_utc`), and such a blank passes the check.
    """
    ok = True
    for mechanism, (filename, artifact_column) in HISTORY_TABLES.items():
        table_path = Path(output_dir) / filename
        if not table_path.is_file():
            continue
        columns, rows = read_table(table_path)
        expected = table_header(mechanism)
        if columns != expected:
            logs.info('file_history', f"  FAIL {filename} has the header {columns}, not the "
                                      f"header {expected} that the heuristics define.")
            ok = False
            continue

        without_history = [r for r in rows if not r.get("last_commit_sha")]
        for column in FILLED_COLUMNS:
            blank = [r for r in rows if not r.get(column) and r.get("last_commit_sha")
                     and not (column == "created_at"
                              and (mechanism, r["repo_name"], r[artifact_column]) in undated)]
            if blank:
                logs.info('file_history', f"  FAIL {len(blank):,} measured row(s) of {filename} "
                                          f"have a blank {column}, such as "
                                          f"{blank[0][artifact_column]} of "
                                          f"{blank[0]['repo_name']}.")
                ok = False
        if without_history:
            logs.info('file_history', f"  Kept {len(without_history):,} row(s) of {filename} "
                                      f"with blank git columns in "
                                      f"{len({r['repo_name'] for r in without_history}):,} "
                                      f"repositories.")
    return ok


# -------------------------------
# CLI
# -------------------------------
def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Measure the git columns that the detection step could not fill from "
                    "file_history.csv, and write them into the mechanism CSVs.")
    parser.add_argument("-w", "--workers", type=int, default=4,
                        help="Number of repositories to clone in parallel on a cache miss "
                             "(default: 4)")
    parser.add_argument("--max-clone-mb", type=int,
                        default=bounds.DEFAULT_MAX_BYTES // (1024 * 1024),
                        help=f"Stop a clone when its working tree exceeds this many MiB "
                             f"(default: {bounds.DEFAULT_MAX_BYTES // (1024 * 1024)}, 0 for no "
                             f"limit)")
    parser.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN"),
                        help="GitHub access token, passed to git through a credential helper "
                             "(default: GITHUB_TOKEN)")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help="Directory that contains the mechanism CSVs (default: output in "
                             "the stage directory)")
    parser.add_argument("--validated-dir", type=Path, default=None,
                        help="The validated tree with its list, repos.csv, from which each "
                             "repository that git can no longer clone is removed (default: "
                             "data/validated in the directory above --output-dir)")
    parser.add_argument("--collected-csv", type=Path, default=None,
                        help="The collected list (default: data/collected/repos.csv in the "
                             "directory above --output-dir). The caches keep only the rows at the "
                             "heads of this list and of the validated list")
    parser.add_argument("--cache", type=Path, default=None,
                        help=f"The cache of the git columns (default: "
                             f"{caches.FILE_HISTORY_PATH.name} in the directory above "
                             f"--output-dir)")
    parser.add_argument("--ai-authorship", type=Path, default=None,
                        help=f"The cache of the AI columns, which receives the AI columns of "
                             f"every measured file (default: {caches.AI_AUTHORSHIP_PATH.name} "
                             f"in the directory above --output-dir)")
    parser.add_argument("--offline", action="store_true",
                        help="Clone nothing, and fail with the rows that are not in the cache")
    parser.add_argument("--offline-except", action="append", default=[], metavar="OWNER/REPO",
                        help="Act as --offline, but clone this repository to measure the rows "
                             "that the cloning step could not measure (repeatable)")
    parser.add_argument("--keep-blank", action="store_true",
                        help="Leave a cached row with blank values unchanged, rather than "
                             "cloning its repository again to measure it")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip the check of the header and the git columns of each CSV")
    parser.add_argument("--clone-dir", type=Path, default=None,
                        help="Directory for the clones, cleared of an earlier run's clones at "
                             "the start (default: Python's temporary directory, never cleared, "
                             "on the overlay file system in a container)")
    args = parser.parse_args(argv)

    # The handler makes SIGTERM raise, so that it reaches the `except BaseException` that saves
    # the cache and the `except KeyboardInterrupt` that exits 130, as this step's document
    # describes. The runner forwards the signal to the step that it is running. A `docker stop`
    # or a systemd stop can arrive during the pass in which every clone happens, before the
    # first save, and the default disposition would then discard everything that the run had
    # measured.
    stopped = False

    def _stop(_signum, _stack_frame):
        # Stop the running git commands first. The workers of the pool are threads, which
        # cannot be terminated, and `cancel_futures` drops only the work that has not started.
        # Raising alone would leave the process waiting for a clone until its time bound and
        # retries end.
        nonlocal stopped
        if stopped:
            # Already stopping. Ctrl+C reaches this step twice, once from the process group and
            # once forwarded by the runner, and the second signal can arrive during the cache
            # save that the first began. `replace_file` would then unlink its temporary file and
            # re-raise, and the measurements that the save is meant to keep would be lost.
            # Ignoring the second signal is safe because the stop path is short.
            return
        stopped = True
        bounds.stop_running()
        raise KeyboardInterrupt

    # Handle both signals, as the cloning step does. The runner starts this step without a
    # session of its own. Ctrl+C therefore reaches the step from the process group, and the
    # runner forwards the same signal too. With a handler for SIGTERM alone, Ctrl+C would take
    # the default disposition and lead to the hang that this handler prevents.
    previous = {sig: signal.signal(sig, _stop)
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        code = _run(args)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if stopped:
        # The cache is saved by now. The step ends with `os._exit`, as the cloning step does,
        # because the interpreter joins the pool's threads at exit. A thread that started
        # another git command after the stop would keep the process running.
        sys.stdout.flush()
        sys.stderr.flush()
        # Remove the credential helper here, because `os._exit` runs no `atexit` handler.
        auth.cleanup()
        os._exit(code)
    return code


def remove_stale_clones(clone_dir: Path) -> int:
    """Remove every clone that an earlier run of this step left in `clone_dir`, and return how
    many it removed.

    A stop ends the step with `os._exit` while pool threads are inside `measure_repo`, and the
    `finally` that removes their clones never runs. Each clone of a large repository would then
    stay in the scratch directory and reduce the free space of every later run. A clone is a
    directory named with `history.CLONE_PREFIX`. The runner's run lock lets only one run at a
    time use the scratch directory, and every such directory at the start of a run is
    therefore a clone that no process reads. A symbolic link with that name is not a clone and
    stays.
    """
    if not clone_dir.is_dir():
        return 0
    removed, kept = 0, []
    for path in sorted(clone_dir.glob(f"{history.CLONE_PREFIX}*")):
        if path.is_symlink() or not path.is_dir():
            continue
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            kept.append(path)
        else:
            removed += 1
    if removed:
        logs.info('file_history', f"Removed {removed:,} clones that an earlier run left in "
                                  f"{clone_dir}.")
    if kept:
        logs.warning('file_history', f"Could not remove {len(kept):,} clones that an earlier "
                                     f"run left in {clone_dir}, such as {kept[0]}. Remove them "
                                     f"to free their disk space.")
    return removed


def _run(args) -> int:
    """The step, under the stop handler that `main` installs."""
    bounds.DEFAULT_MAX_BYTES = args.max_clone_mb * 1024 * 1024

    output_dir = (args.output_dir or STAGE_DIR / "output").resolve()
    validated_dir = (args.validated_dir
                     or output_dir.parent / "data" / "validated").resolve()
    # The caches default to the directory above the output directory, as the validated tree
    # does, so that a run over another output directory does not write its measurements into
    # the tracked `file_history.csv` and `ai_authorship.csv`. Without `--output-dir`, the two
    # paths are the stage's caches.
    cache_path = (args.cache or output_dir.parent / caches.FILE_HISTORY_PATH.name).resolve()
    ai_authorship_path = (args.ai_authorship
                          or output_dir.parent / caches.AI_AUTHORSHIP_PATH.name).resolve()
    collected_csv = (args.collected_csv
                     or output_dir.parent / "data" / "collected" / "repos.csv").resolve()
    validated_csv = validated_dir / "repos.csv"
    if not output_dir.is_dir():
        logs.error('file_history', f"The output directory {output_dir} does not exist.")
        return 1
    if not validated_dir.is_dir():
        logs.error('file_history', f"The validated tree {validated_dir} does not exist.")
        return 1
    # Both lists name the heads at which the caches keep their rows (`current_heads`), and
    # without either the step could not tell which rows a later step reads.
    for name, path, step in (("collected list", collected_csv, "the cloning step"),
                             ("validated list", validated_csv, "the collected file cleanup")):
        if not path.is_file():
            logs.error('file_history', f"The {name} {path} does not exist. Run {step} first.")
            return 1
    for filename, _ in HISTORY_TABLES.values():
        if not (output_dir / filename).is_file():
            logs.error('file_history', f"{output_dir / filename} does not exist. Run the detection "
                                       f"step first.")
            return 1
    if not (output_dir / "repos.csv").is_file():
        logs.error('file_history', f"{output_dir / 'repos.csv'} does not exist. Run the detection "
                                   f"step first.")
        return 1

    # Remove stale clones only under `--clone-dir`, which the runner always passes. Every other
    # process on the machine shares the interpreter's temporary directory, and a directory of
    # that name there can belong to another process.
    if args.clone_dir is not None:
        remove_stale_clones(args.clone_dir.resolve())

    offline = args.offline or bool(args.offline_except)
    try:
        summaries = build_all(output_dir, cache_path, validated_dir, args.workers,
                              args.github_token, offline,
                              not (offline or args.keep_blank),
                              scratch_dir=args.clone_dir, ai_cache_path=ai_authorship_path,
                              offline_except=frozenset(args.offline_except),
                              heads=current_heads(collected_csv, validated_csv))
        # The first column is as wide as its longest name, such as `planning_document`, so that
        # the counts of every row stand under their headers.
        width = max([len("mechanism"), *(len(summary["mechanism"]) for summary in summaries)])
        logs.info('file_history', f"{'mechanism':<{width}} {'rows':>6} {'fetched':>8} "
                                  f"{'pruned':>7} {'no history':>10}")
        for summary in summaries:
            logs.info('file_history', f"{summary['mechanism']:<{width}} {summary['rows']:>6,} "
                                      f"{summary['fetched']:>8,} {summary['pruned']:>7,} "
                                      f"{summary['no_history']:>10,}")

        # Inside the same `try`, because the reconciliation reads the output CSVs that this run
        # has just written and raises the same exception classes from the same causes. They
        # reach the operator as an error line rather than as a traceback.
        kept, dropped = reconcile_repos_csv(output_dir)
    except (OSError, ValueError, KeyError) as e:
        # OSError and KeyError too, not only FileNotFoundError, for a cache that cannot be
        # opened, a header that the loader rejects or a mechanism CSV that lost a column. Each
        # reaches the operator as an error line with exit 1 rather than as a traceback. The
        # text of a ValueError is a sentence of this step. An OSError and a KeyError name only
        # the file or the column, and the line says what failed.
        if isinstance(e, KeyError):
            logs.error('file_history', f"An input CSV of the step lacks the column {e}.")
        elif isinstance(e, OSError):
            logs.error('file_history', f"Could not read or write a file of the step ({e}).")
        else:
            logs.error('file_history', f"{str(e).rstrip('.')}.")
        return 1
    except KeyboardInterrupt:
        logs.warning('file_history', "Interrupted. The cache holds the measurements so far.")
        return 130
    except NetworkDown as e:
        # Exit 4, as the cloning step exits when its probe fails after every wait, so that the
        # runner records the step as not complete and a later Start measures the rows.
        logs.error('file_history', f"{e}. The cache holds the measurements so far, and the next "
                                   f"run measures the rest.")
        return 4

    try:
        logs.info('file_history', f"Kept {kept:,} repositories in repos.csv and dropped "
                                  f"{len(dropped):,}.")
        for repo_name in dropped[:10]:
            logs.info('file_history', f"  Dropped {repo_name}.")

        if not args.no_verify:
            undated = frozenset((summary["mechanism"], *key) for summary in summaries
                                for key in summary["undated"])
            if not verify_tables(output_dir, undated):
                return 1
    except KeyboardInterrupt:
        # The output CSVs and the cache are written by this point, and a stop here still exits
        # 130.
        logs.warning('file_history', "Interrupted after the output CSVs were written.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
