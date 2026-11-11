"""Read and write the collection's file history and AI authorship caches.

The file history cache is keyed by `(repo_name, head_sha, mechanism, artifact)`. The cloning
step measures the history before detection classifies a file, and leaves the mechanism blank.
`for_artifact` returns a measurement that is the only one for its path, or that is equal under
every mechanism that recorded it, and returns nothing for a path with different measurements. A
row with blank values records that the history was unavailable, which differs from a path
without a row.

The AI authorship cache is keyed by `(repo_name, head_sha, artifact)`. Its four values describe
the AI attribution in the history of the path. They are kept apart from the file history,
because a complete history query does not establish a complete AI attribution. The
reclassification requires the commit log and the per-repository commit record with the commits
of each copied path.

Both caches check their headers and save through a temporary file and an atomic rename.
`submodules.csv` records the gitlinks of the repositories separately and has no cache class.
"""

from __future__ import annotations

import csv
import glob
import io
import os
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
import sys

from agentcontext import STAGE_DIR, heuristics
from agentcontext import logs

FILE_HISTORY_PATH = STAGE_DIR / "file_history.csv"
AI_AUTHORSHIP_PATH = STAGE_DIR / "ai_authorship.csv"


# ---------------------------------------------------------------------------
# The file history
# ---------------------------------------------------------------------------
# The four columns that identify a row, in the order in which they are written.
FILE_HISTORY_KEY_COLUMNS = ("repo_name", "head_sha", "mechanism", "artifact")

# The values that `git log` returns for one path. They are defined in the heuristics, from which
# the published CSVs take the same four names, so that the cache and the output CSVs use the same
# columns. None of them describes the contents of a file, which the detection step reads from
# `data/validated/`.
HISTORY_COLUMNS = heuristics.HISTORY_COLUMNS

FILE_HISTORY_COLUMNS = FILE_HISTORY_KEY_COLUMNS + HISTORY_COLUMNS


@dataclass(frozen=True)
class FileHistoryKey:
    """The repository, commit, CSV and path that a set of facts belongs to."""

    repo_name: str
    head_sha: str
    mechanism: str
    artifact: str

    def as_row(self) -> dict:
        return {c: getattr(self, c) for c in FILE_HISTORY_KEY_COLUMNS}

    def sort_key(self) -> tuple:
        return tuple(getattr(self, c) for c in FILE_HISTORY_KEY_COLUMNS)


@dataclass(frozen=True)
class FileHistory:
    """The history that `git log` reported for one path in one checkout. A value is blank where
    `git log` returned none."""

    created_at: str = ""
    commits: str = ""
    first_commit_sha: str = ""
    last_commit_sha: str = ""

    # The column name of each field. Only `commits` differs, and only because `#commits` is
    # the published column name and is not a Python identifier.
    _COLUMN_BY_FIELD = {
        "created_at": "created_at",
        "commits": "#commits",
        "first_commit_sha": "first_commit_sha",
        "last_commit_sha": "last_commit_sha",
    }

    @classmethod
    def from_row(cls, row: dict) -> "FileHistory":
        """Read the four history columns of a CSV row, and read a missing column as blank."""
        return cls(**{field: (row.get(column) or "")
                      for field, column in cls._COLUMN_BY_FIELD.items()})

    def as_row(self) -> dict:
        return {column: getattr(self, field)
                for field, column in self._COLUMN_BY_FIELD.items()}

    def is_complete(self) -> bool:
        """Whether `git log` returned every value. The file history step queries an incomplete
        row again.

        The fields are named rather than derived from the field list, so that whoever adds a
        column decides what a blank in it means.
        """
        return all(getattr(self, field) for field in ("created_at", "commits",
                                                      "first_commit_sha",
                                                      "last_commit_sha"))


class FileHistoryCache:
    """The rows of `file_history.csv`, keyed by `FileHistoryKey`.

    Loading a file that does not exist gives an empty cache, because the first run has
    nothing to read. Loading a file with a header other than `FILE_HISTORY_COLUMNS` raises,
    because the columns that do not match would be read as blanks, and a blank is a recorded
    measurement here. A column added to `HISTORY_COLUMNS` therefore requires writing the file
    again in the same commit rather than migrating it in place.
    """

    def __init__(self, entries: dict | None = None, path: Path | None = None):
        self._entries = dict(entries or {})
        self.path = Path(path) if path is not None else None
        self._by_artifact = None
        # Whether anything has been put since the last save. A caller that saves on a
        # count change alone would miss a row replaced rather than added.
        self.dirty = False

    # -- reading ---------------------------------------------------------------
    @classmethod
    def load(cls, path=None) -> "FileHistoryCache":
        path = Path(path) if path is not None else FILE_HISTORY_PATH
        if not path.is_file():
            return cls(path=path)
        entries = {}
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = tuple(reader.fieldnames or ())
            if header != FILE_HISTORY_COLUMNS:
                raise ValueError(
                    f"The header of {path} names the columns {list(header)}, not the expected "
                    f"{list(FILE_HISTORY_COLUMNS)}")
            for row in reader:
                key = FileHistoryKey(*(row[c] for c in FILE_HISTORY_KEY_COLUMNS))
                if key in entries:
                    raise ValueError(f"{path} has the key {key} twice")
                entries[key] = FileHistory.from_row(row)
        return cls(entries, path=path)

    def get(self, key: FileHistoryKey):
        return self._entries.get(key)

    def for_artifact(self, repo_name: str, head_sha: str, artifact: str):
        """The facts for one path at one commit, whatever mechanism recorded them.

        A path that moves between mechanisms keeps its history, such as `.claude/commands`
        when a heuristic reclassifies it into skills. Looking up a reclassified row by its new
        mechanism alone would clone a repository to measure again what the cache already
        contains under the old mechanism.

        The lookup also finds a row that the cloning step measured. That step reads the
        history of every file that it copies before detection decides which CSV the file
        belongs to, and it writes the mechanism blank.

        Returns None where nothing was recorded, and also where two mechanisms recorded
        different values, which is why `mechanism` is part of the key.
        """
        if self._by_artifact is None:
            # Built once, on the first lookup. Scanning every entry per call compares against
            # every row of the cache, and a run in which every row misses would repeat that
            # scan for every row.
            index = {}
            for key, facts in self._entries.items():
                index.setdefault((key.repo_name, key.head_sha, key.artifact),
                                 set()).add(facts)
            self._by_artifact = index
        found = self._by_artifact.get((repo_name, head_sha, artifact))
        if not found or len(found) > 1:
            return None
        return next(iter(found))

    def __contains__(self, key) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def keys(self):
        return self._entries.keys()

    def items(self):
        return self._entries.items()

    def incomplete(self) -> list:
        """Every key with a row that lacks a value."""
        return [k for k, facts in self._entries.items() if not facts.is_complete()]

    # -- writing ---------------------------------------------------------------
    def put(self, key: FileHistoryKey, facts: FileHistory) -> None:
        self._entries[key] = facts
        self._by_artifact = None
        self.dirty = True

    def recount(self, counts: dict) -> int:
        """Set `#commits` from `counts`, keyed by `(repo_name, head_sha, artifact)`, on every row
        that matches one of its keys, whatever mechanism recorded the row. A row with a blank
        `#commits` keeps the blank, which records that the history query returned no value, and a
        count read from elsewhere does not change that record. Returns the number of rows with a
        changed count."""
        changed = 0
        for key, facts in list(self._entries.items()):
            count = counts.get((key.repo_name, key.head_sha, key.artifact))
            if count is None or not facts.commits or facts.commits == str(count):
                continue
            self.put(key, replace(facts, commits=str(count)))
            changed += 1
        return changed

    def rows(self) -> list:
        """Every entry as a CSV row, sorted by key.

        Sorted rather than insertion-ordered, so that two runs that measured the same
        configuration artifacts in a different order write the same file. The cache is tracked,
        and git would report a reordering as a change.
        """
        return [dict(key.as_row(), **facts.as_row())
                for key, facts in sorted(self._entries.items(),
                                         key=lambda item: item[0].sort_key())]

    def render(self) -> str:
        """The exact text that `save` writes."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(FILE_HISTORY_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows())
        return buffer.getvalue()

    def save(self, path=None) -> Path:
        """Write the cache through `replace_file`, and record its path."""
        # No fallback to FILE_HISTORY_PATH: a cache built in memory and saved without a path
        # would replace the tracked cache with whatever the caller had put in it.
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("save() needs a path for a cache that was not loaded from a file")
        replace_file(target, self.render())
        self.path = target
        self.dirty = False
        return target


# The length of the random part of a temporary file's name, `<target>.<random>.tmp`. A fixed
# length lets `temporary_files` tell the temporary files of one target from those of a target
# with a longer name. The temporary file of `o§a.json.json`, the per-repository commit record of
# `o/a.json`, is then never taken for one of `o§a.json`, the record of `o/a`, which another
# worker can write at the same time. `commits.remove_shards` matches the same names.
TEMPORARY_NAME_LENGTH = 8


def temporary_files(target: Path) -> list:
    """The temporary files of `replace_file` for `target`, including any that a killed process
    left."""
    target = Path(target)
    pattern = glob.escape(target.name) + "." + "?" * TEMPORARY_NAME_LENGTH + ".tmp"
    return sorted(target.parent.glob(pattern))


def sync_directory(directory: Path) -> None:
    """Write the entries of `directory` to disk, so that a file created or renamed in it is
    there after a power loss. A file system that cannot open or sync a directory is left as it
    is, because the file's own data is on disk either way."""
    try:
        handle = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(handle)
    except OSError:
        pass
    finally:
        os.close(handle)


def sync_file(path: Path) -> None:
    """Write the data of the file at `path` to disk. An error raises, because a later step may
    not count a file as written while its data may not be on disk."""
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def sync_tree(path: Path) -> None:
    """Write a file, or a directory with every file and directory below it, to disk, and the
    entry of `path` in its directory. A link is not followed, and its entry is written with
    its directory. For a path that does not exist, the function writes the directory, so that
    a removal of the path reaches the disk.

    The cloning step syncs what a repository's line in the list of finished repositories
    describes before it writes the line. A line that reached the disk before the files would,
    after a power loss, describe a collection with empty or missing files, and a resumed run
    would take that collection over as complete."""
    from agentcontext import paths
    path = Path(path)
    if not os.path.lexists(path):
        sync_directory(path.parent)
        return
    if path.is_dir() and not path.is_symlink():
        for root, dirs, files in os.walk(path):
            # Synology DSM writes and removes a directory of `DISCARDED_DIRECTORIES` on its own,
            # such as the thumbnails of an image, and no line describes what it holds.
            dirs[:] = [name for name in dirs if name not in paths.DISCARDED_DIRECTORIES]
            for name in files:
                file = os.path.join(root, name)
                if os.path.islink(file):
                    continue
                try:
                    sync_file(Path(file))
                except FileNotFoundError:
                    # Removed after the listing, by DSM or by the step itself: nothing to sync.
                    continue
            sync_directory(Path(root))
    elif not path.is_symlink():
        sync_file(path)
    sync_directory(path.parent)


def replace_file(target: Path, text: str) -> None:
    """Write `text` to `target` in one step: a temporary file beside it, synced to disk, then a
    rename.

    Both caches and `write_submodules` save through this function, and so do the
    per-repository commit records and the record of the collection in progress. A partial write
    would leave a file that loads with rows missing, and a missing row causes a network fetch on
    the next run rather than an error that anyone would see. The data is synced before the
    rename and the directory after it, because on a power loss a rename that reached the disk
    before the data would leave an empty or truncated file under the target's name.
    """
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A temporary file that a killed process left behind. The `except` below removes the
    # temporary file after an exception, but not after a SIGKILL, an OOM kill or the `os._exit`
    # that a stop handler ends with. These targets are beside tracked files, and a leftover file
    # would make the next `git status` report a file that nobody edited.
    for stale in temporary_files(target):
        stale.unlink(missing_ok=True)
    while True:
        temporary = target.with_name(
            f"{target.name}.{secrets.token_hex(TEMPORARY_NAME_LENGTH // 2)}.tmp")
        try:
            handle = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        break
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        # The file is created with mode 0600, and a tracked data file beside the other CSVs
        # needs 0644. The mode is set rather than left to the umask, so that a pipeline run and
        # a run by hand leave the same file.
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    sync_directory(target.parent)


# ---------------------------------------------------------------------------
# The AI authorship
# ---------------------------------------------------------------------------
# The three columns that identify a row, in the order in which they are written.
AI_AUTHORSHIP_KEY_COLUMNS = ("repo_name", "head_sha", "artifact")

# The values that the history query returns. They are defined in the heuristics, from which
# `context-files.csv` takes the same four names, so that the cache and the CSV use the same
# columns.
AI_COLUMNS = heuristics.AI_COLUMNS

AI_AUTHORSHIP_COLUMNS = AI_AUTHORSHIP_KEY_COLUMNS + AI_COLUMNS


@dataclass(frozen=True)
class AiAuthorshipKey:
    """The repository, commit and path that a set of values belongs to."""

    repo_name: str
    head_sha: str
    artifact: str

    def as_row(self) -> dict:
        return {c: getattr(self, c) for c in AI_AUTHORSHIP_KEY_COLUMNS}

    def sort_key(self) -> tuple:
        return tuple(getattr(self, c) for c in AI_AUTHORSHIP_KEY_COLUMNS)


@dataclass(frozen=True)
class AiAuthorship:
    """What the commit patterns found in the commits of one path. Blank where no history query
    read the path."""

    first_commit_ai_created: str = ""
    first_commit_ai_tool: str = ""
    ai_commits: str = ""
    ai_commit_tools: str = ""

    # The column name of each field. Only `ai_commits` differs, and only because `#ai_commits`
    # is the published column name and is not a Python identifier.
    _COLUMN_BY_FIELD = {
        "first_commit_ai_created": "first_commit_ai_created",
        "first_commit_ai_tool": "first_commit_ai_tool",
        "ai_commits": "#ai_commits",
        "ai_commit_tools": "ai_commit_tools",
    }

    @classmethod
    def from_row(cls, row: dict) -> "AiAuthorship":
        """Read the four columns out of a CSV row, treating a missing one as blank."""
        return cls(**{field: (row.get(column) or "")
                      for field, column in cls._COLUMN_BY_FIELD.items()})

    def as_row(self) -> dict:
        return {column: getattr(self, field)
                for field, column in self._COLUMN_BY_FIELD.items()}

    def is_complete(self) -> bool:
        """Whether the history query returned the values.

        The two tool columns are blank for a file that no tool changed. The test therefore
        reads the boolean and the count, which a completed history query always writes.
        """
        return bool(self.first_commit_ai_created and self.ai_commits)


class AiAuthorshipCache:
    """The rows of `ai_authorship.csv`, keyed by `AiAuthorshipKey`.

    The same contract as `FileHistoryCache`: a file that does not exist
    loads as an empty cache, a header that is not `AI_AUTHORSHIP_COLUMNS` raises, a duplicate key
    raises, and `save` replaces the file in one step.
    """

    def __init__(self, entries: dict | None = None, path: Path | None = None):
        self._entries = dict(entries or {})
        self.path = Path(path) if path is not None else None
        self.dirty = False

    @classmethod
    def load(cls, path=None) -> "AiAuthorshipCache":
        path = Path(path) if path is not None else AI_AUTHORSHIP_PATH
        if not path.is_file():
            return cls(path=path)
        entries = {}
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            header = tuple(reader.fieldnames or ())
            if header != AI_AUTHORSHIP_COLUMNS:
                raise ValueError(
                    f"The header of {path} names the columns {list(header)}, not the expected "
                    f"{list(AI_AUTHORSHIP_COLUMNS)}")
            for row in reader:
                key = AiAuthorshipKey(*(row[c] for c in AI_AUTHORSHIP_KEY_COLUMNS))
                if key in entries:
                    raise ValueError(f"{path} has the key {key} twice")
                entries[key] = AiAuthorship.from_row(row)
        return cls(entries, path=path)

    def get(self, key: AiAuthorshipKey):
        return self._entries.get(key)

    def __contains__(self, key) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def keys(self):
        return self._entries.keys()

    def items(self):
        return self._entries.items()

    def incomplete(self) -> list:
        """Every key with a row for which the history query returned no values."""
        return [k for k, values in self._entries.items() if not values.is_complete()]

    def put(self, key: AiAuthorshipKey, values: AiAuthorship) -> None:
        self._entries[key] = values
        self.dirty = True

    def rows(self) -> list:
        """Every entry as a CSV row, sorted by key, so that two runs write the same file."""
        return [dict(key.as_row(), **values.as_row())
                for key, values in sorted(self._entries.items(),
                                          key=lambda item: item[0].sort_key())]

    def render(self) -> str:
        """The exact text that `save` writes."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(AI_AUTHORSHIP_COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(self.rows())
        return buffer.getvalue()

    def save(self, path=None) -> Path:
        """Write the cache through `replace_file`, and record its path."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ValueError("save() needs a path for a cache that was not loaded from a file")
        replace_file(target, self.render())
        self.path = target
        self.dirty = False
        return target


# ---------------------------------------------------------------------------
# The submodules CSV, and the merge of a run's measurements into the caches
# ---------------------------------------------------------------------------
SUBMODULES_CSV_NAME = "submodules.csv"
SUBMODULES_HEADER = "repo_name,path,submodule_repo"
def write_file_history(path: Path, rows: list[dict]) -> int:
    """Merge this run's measurements into the file history cache and write it.

    Merged rather than replaced, because the cache also holds rows that no line of the list of
    finished repositories holds, such as the rows that the file history step measured. A row
    measured again replaces the earlier row with the same key. The key includes the commit, and
    a collection at a new `head_sha` adds rows beside the rows of the old commit. The
    file history step, the last step of a run that writes the cache, removes the rows of the old
    commit (`steps.file_history.retain_heads`).
    """
    cache = FileHistoryCache.load(path)
    for row in rows:
        cache.put(FileHistoryKey(*(row[c] for c in FILE_HISTORY_KEY_COLUMNS)),
                  FileHistory.from_row(row))
    cache.save(path)
    logs.info("caches", f"Merged {len(rows):,} measured row(s) of file history into "
                        f"{path.name}, which has {len(cache):,} rows.")
    return len(cache)


def write_submodules(path: Path, rows: list[dict], repos: list[str]) -> int:
    """Merge this run's gitlinks into submodules.csv and write it.

    Merged for the same reason as the file history, because a run covers a list of repositories
    and the file covers every repository ever collected. The rows of every repository that this
    run reached replace the rows that the repository had, and a submodule that has been removed
    since then leaves the file.
    """
    existing = {}
    if path.is_file():
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                existing.setdefault(row["repo_name"], []).append(row)
    for repo in repos:
        existing.pop(repo, None)
    for row in rows:
        existing.setdefault(row["repo_name"], []).append(row)
    flat = sorted((row for repo_rows in existing.values() for row in repo_rows),
                  key=lambda r: (r["repo_name"], r["path"]))
    # Written through the helper of the two caches. It removes the temporary file when the write
    # fails and sets the mode, so that no .tmp file stays behind and the file has mode 0644, as
    # the CSVs beside it have.
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=SUBMODULES_HEADER.split(","),
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(flat)
    replace_file(path, buffer.getvalue())
    logs.info("caches", f"Merged {len(rows):,} submodule path(s) read from the clones into "
                        f"{path.name}, which has {len(flat):,} rows.")
    return len(flat)
