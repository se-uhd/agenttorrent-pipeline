"""The cache of GHS retrievals: every GHS search result that the sampling stage took, and
GitHub's response for each repository in it, as plain files under `ghs_retrievals/`.

A retrieval is the GHS search result for one query, taken at one time. The GHS search result is
the JSON file that GHS returns for the query, with the matching repositories under `items` and
the query under `parameters`. A retrieval is named by the UTC time at which it started, such as
`2026-03-16T180701Z`, and kept permanently. Because
GHS updates a row in place when it crawls a push to the repository and keeps no history of its
own, a change shows only when a later retrieval is compared with an earlier one.

    ghs_retrievals/
      index.csv                         one row per retrieval attempt, keyed by its id
      <id>.ghs.jsonl.xz                 the repositories of the GHS search result, one JSON
                                        object per line, in the order in which GHS sent them
      <id>.github.jsonl.xz              GitHub's response for each of those rows, in the same
                                        order

The rows are kept in GHS's order, by ascending GHS id, rather than sorted by name. GHS's order
is deterministic, and a reader that depends on the input order would otherwise read a different
order from the cache than from the download. A file that reaches VOLUME_BYTES is split into
numbered volumes (`<name>.001`, `<name>.002` and so on), which are read back as one stream,
because GitHub rejects a file over 100 MB. As with the files of the three caches under
`data/collection/` (`docs/caches.md`), every file is written whole under a temporary name and
synced to disk before the rename, and the directory is synced after the rename. On a power loss,
a rename that reached the disk before the data would leave an empty or truncated file under the
name, and an index or a pointer that cannot be read stops every later download.

Run as a script, the module imports a GHS search result that an earlier version of the download
wrote:

    python3 ghs_cache.py import ghs_complete.json --end-date 2026-03-16
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import lzma
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from agentcontext import SAMPLING_DIR, caches, logs

COMPONENT = "ghs_cache"

CACHE_DIR = SAMPLING_DIR / "ghs_retrievals"
INDEX_NAME = "index.csv"

# The file that a run of the download writes beside the cache. It names the retrieval that the
# rest of the stage reads, either the retrieval that the run took or the retrieval that it fell
# back to. Git tracks the index but not this file.
POINTER = SAMPLING_DIR / "ghs_retrieval.json"

# GitHub rejects a file over 100 MB.
VOLUME_BYTES = 95_000_000
XZ_PRESET = 6

KINDS = ("ghs", "github")
RETRIEVED, FAILED, IMPORTED = "retrieved", "failed", "imported"
OUTCOMES = (RETRIEVED, FAILED, IMPORTED)

# One row per retrieval attempt. The download writes the first group of columns, and the GitHub
# query writes the `github_` columns. A value that is a list or an object is written as JSON.
INDEX_COLUMNS = (
    "retrieval_id", "outcome", "started_at", "finished_at",
    "end_date_source", "end_date", "created_max", "committed_min",
    "query", "server_parameters", "ghs_version", "crawl_dates",
    "count_before", "count_after", "item_count", "bytes_received", "body_sha256",
    "ghs_file_sha256", "attempts", "error", "fallback", "imported_from",
    "github_started_at", "github_finished_at", "github_rows", "github_found",
    "github_not_found", "github_unresolved", "github_requests", "github_cost",
    "github_file_sha256",
)

_ID = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z")


class CacheError(Exception):
    """The cache cannot be read: a malformed index, a missing file or a checksum that differs.
    Its text never starts with a path, so that `upper_first` can start a sentence with it."""


def upper_first(text) -> str:
    """`text` with its first letter capitalized, for an error text that starts a sentence of a
    log message."""
    text = str(text)
    return text[:1].upper() + text[1:]


def retrieval_id(moment: datetime) -> str:
    """The id of a retrieval started at `moment`, its UTC time to the second."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def check_id(value: str) -> str:
    """`value` if it is a retrieval id, else CacheError."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CacheError(f"{value!r} is not a retrieval id (a UTC time such as "
                         f"2026-03-16T180701Z)")
    return value


def utc_now() -> datetime:
    """The current time in UTC, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    """`moment` in UTC as ISO 8601 with a `Z`, to the second."""
    return moment.astimezone(timezone.utc).replace(tzinfo=None, microsecond=0).isoformat() + "Z"


def name_key(name: str) -> str:
    """The key of a repository: its `owner/name`, lowercased, because GHS and GitHub find a
    repository by its name without regard to case."""
    return name.strip().lower()


def license_key(name: str | None) -> str:
    """A license name without its double quotes. GitHub's GraphQL API writes
    `BSD 3-Clause "New" or "Revised" License`, as SPDX does, and GHS the same name without the
    quotes. If names were compared with their quotes, an exclusion written in one spelling would
    not match the other spelling, and the comparison of two retrievals would count the
    repository as relicensed."""
    return (name or "").replace('"', "")


def normalize(item: dict) -> dict:
    """A GHS row with `metrics` sorted by language. GHS builds that list from a hash set
    (`GitRepoToDtoConverter.java:55-64`), and two GHS search results can list the languages of an
    unchanged repository in different orders."""
    metrics = item.get("metrics")
    if isinstance(metrics, list) and all(isinstance(m, dict) for m in metrics):
        item = dict(item, metrics=sorted(metrics, key=lambda m: str(m.get("language"))))
    return item


# ---------------------------------------------------------------------------
# The retrieval files
# ---------------------------------------------------------------------------

def rows_path(cache_dir: Path, rid: str, kind: str) -> Path:
    """The path of a retrieval's rows as one file. The volumes of a split file add `.001`,
    `.002` and so on."""
    if kind not in KINDS:
        raise CacheError(f"{kind!r} is not a kind of retrieval rows ({' or '.join(KINDS)})")
    return Path(cache_dir) / f"{check_id(rid)}.{kind}.jsonl.xz"


def volumes(path: Path) -> list[Path]:
    """The files of `path`: the file itself, or its numbered volumes in order, or none."""
    if path.is_file():
        return [path]
    found = sorted(path.parent.glob(path.name + ".[0-9][0-9][0-9]"))
    expected = [path.with_name(f"{path.name}.{n:03d}") for n in range(1, len(found) + 1)]
    if found != expected:
        raise CacheError(f"the volumes of {path.name} are not numbered 001 to "
                         f"{len(found):03d} without a gap ({', '.join(p.name for p in found)})")
    return found


def has_rows(cache_dir: Path, rid: str, kind: str) -> bool:
    """Whether the cache contains the rows of `kind` for retrieval `rid`."""
    return bool(volumes(rows_path(cache_dir, rid, kind)))


def file_sha256(cache_dir: Path, rid: str, kind: str) -> str:
    """The SHA-256 of a retrieval file's compressed bytes, over all its volumes."""
    parts = volumes(rows_path(cache_dir, rid, kind))
    if not parts:
        raise CacheError(f"no {kind} rows for retrieval {rid} in {cache_dir}")
    digest = hashlib.sha256()
    for part in parts:
        with open(part, "rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                digest.update(block)
    return digest.hexdigest()


def write_rows(cache_dir: Path, rid: str, kind: str, rows: Iterable[dict]) -> tuple[int, str]:
    """Write `rows` as the `kind` file of retrieval `rid`, and return the number of rows and the
    SHA-256 of the written bytes.

    The rows are compressed into a temporary file beside the target. Below VOLUME_BYTES, the
    temporary file is synced to disk and renamed. Otherwise it is split into volumes, each synced
    before its rename. The directory is synced after the renames. A file of the same name is
    replaced only once the new file is in place. A write that stops leaves the previous file,
    which still matches its recorded SHA-256. For volumes, a stop between two
    renames leaves a mix of volumes that matches no recorded SHA-256.
    """
    target = rows_path(cache_dir, rid, kind)
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale in target.parent.glob(target.name + ".*.tmp"):
        stale.unlink(missing_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(target.parent),
                                         prefix=target.name + ".", suffix=".tmp")
    count = 0
    digest = hashlib.sha256()
    try:
        compressor = lzma.LZMACompressor(preset=XZ_PRESET)
        with os.fdopen(handle, "wb") as f:
            for row in rows:
                line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
                block = compressor.compress(line.encode("utf-8"))
                digest.update(block)
                f.write(block)
                count += 1
            block = compressor.flush()
            digest.update(block)
            f.write(block)
            f.flush()
            single = f.tell() < VOLUME_BYTES
            if single:
                os.fsync(f.fileno())
        os.chmod(temporary, 0o644)
        # The previous file stays until the new file is in place, because if it were removed
        # first, a stop before the rename would leave neither file. The previous volumes are
        # removed after the rename, including the volumes beyond the new count, and a single file
        # is removed once the volumes that replace it are in place.
        previous = [target, *target.parent.glob(target.name + ".[0-9][0-9][0-9]")]
        if single:
            os.replace(temporary, target)
            written = [target]
        else:
            renames = []
            with open(temporary, "rb") as source:
                number = 0
                while chunk := source.read(VOLUME_BYTES):
                    number += 1
                    part = target.with_name(f"{target.name}.{number:03d}")
                    part_tmp = part.with_name(part.name + ".tmp")
                    with open(part_tmp, "wb") as f:
                        f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    os.chmod(part_tmp, 0o644)
                    renames.append((part_tmp, part))
            for part_tmp, part in renames:
                os.replace(part_tmp, part)
            Path(temporary).unlink()
            written = [part for _, part in renames]
        for path in previous:
            if path not in written:
                path.unlink(missing_ok=True)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        for stale in target.parent.glob(target.name + ".*.tmp"):
            stale.unlink(missing_ok=True)
        raise
    caches.sync_directory(target.parent)
    return count, digest.hexdigest()


class _Concatenated(io.RawIOBase):
    """The volumes of one file, read as one stream."""

    def __init__(self, parts: list[Path]):
        self.parts = list(parts)
        self.current = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while True:
            if self.current is None:
                if not self.parts:
                    return 0
                self.current = open(self.parts.pop(0), "rb")
            n = self.current.readinto(buffer)
            if n:
                return n
            self.current.close()
            self.current = None

    def close(self) -> None:
        if self.current is not None:
            self.current.close()
            self.current = None
        super().close()


def read_rows(cache_dir: Path, rid: str, kind: str, expected_sha256: str = "") -> Iterator[dict]:
    """The rows of the `kind` file of retrieval `rid`, one dict per line, streamed.

    With `expected_sha256`, the compressed bytes are checked against that digest before the
    first row is read, and a file that changed after the index recorded it is rejected rather
    than read.
    """
    parts = volumes(rows_path(cache_dir, rid, kind))
    if not parts:
        raise CacheError(f"no {kind} rows for retrieval {rid} in {cache_dir}")
    if expected_sha256 and file_sha256(cache_dir, rid, kind) != expected_sha256:
        raise CacheError(f"the SHA-256 of the {kind} rows of retrieval {rid} is not the "
                         f"{expected_sha256} that the index records (the file changed after it "
                         f"was written)")
    stream = io.BufferedReader(_Concatenated(parts), buffer_size=1 << 20)
    with lzma.open(stream, "rt", encoding="utf-8") as text:
        for number, line in enumerate(text, 1):
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise CacheError(f"line {number} of the {kind} rows of retrieval {rid} is not "
                                 f"JSON ({error})") from error


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------

def read_index(cache_dir: Path = CACHE_DIR) -> list[dict]:
    """Every row of the index, oldest first, or `[]` when there is no index yet."""
    path = Path(cache_dir) / INDEX_NAME
    if not path.is_file():
        return []
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != INDEX_COLUMNS:
            raise CacheError(f"the index {path} has the columns {reader.fieldnames} instead of "
                             f"{list(INDEX_COLUMNS)}")
        rows = list(reader)
    seen = set()
    for number, row in enumerate(rows, 2):
        rid = check_id(row["retrieval_id"])
        if rid in seen:
            raise CacheError(f"line {number} of the index {path} is a second row of retrieval "
                             f"{rid}")
        if row["outcome"] not in OUTCOMES:
            raise CacheError(f"line {number} of the index {path} has the outcome "
                             f"{row['outcome']!r}, which is not one of {', '.join(OUTCOMES)}")
        seen.add(rid)
    return sorted(rows, key=lambda row: row["retrieval_id"])


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def write_index(rows: list[dict], cache_dir: Path = CACHE_DIR) -> None:
    """Write the index whole, sorted by id, in one step (`caches.replace_file`)."""
    path = Path(cache_dir) / INDEX_NAME
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=INDEX_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in sorted(rows, key=lambda row: row["retrieval_id"]):
        unknown = set(row) - set(INDEX_COLUMNS)
        if unknown:
            raise CacheError(f"the index has no column {sorted(unknown)}")
        writer.writerow({column: _cell(row.get(column)) for column in INDEX_COLUMNS})
    caches.replace_file(path, buffer.getvalue())


def put_index_row(values: dict, cache_dir: Path = CACHE_DIR) -> dict:
    """Add the row that `values` names by `retrieval_id`, or update the columns that `values`
    contains, and return the row as written."""
    rid = check_id(values.get("retrieval_id", ""))
    rows = read_index(cache_dir)
    for row in rows:
        if row["retrieval_id"] == rid:
            row.update({key: _cell(value) for key, value in values.items()})
            break
    else:
        row = {column: "" for column in INDEX_COLUMNS}
        row.update({key: _cell(value) for key, value in values.items()})
        rows.append(row)
    write_index(rows, cache_dir)
    return row


def index_row(rid: str, cache_dir: Path = CACHE_DIR) -> dict:
    """The index row of retrieval `rid`, or CacheError."""
    for row in read_index(cache_dir):
        if row["retrieval_id"] == rid:
            return row
    raise CacheError(f"the index of {cache_dir} has no retrieval {rid}")


def usable(row: dict) -> bool:
    """Whether a row is a retrieval that the stage can read: taken or imported, not a failure."""
    return row["outcome"] in (RETRIEVED, IMPORTED)


def previous_retrieval(rid: str, cache_dir: Path = CACHE_DIR) -> dict | None:
    """The newest usable retrieval older than `rid`, or None."""
    older = [row for row in read_index(cache_dir) if usable(row) and row["retrieval_id"] < rid]
    return older[-1] if older else None


def latest_retrieval(cache_dir: Path = CACHE_DIR) -> dict | None:
    """The newest usable retrieval, or None."""
    rows = [row for row in read_index(cache_dir) if usable(row)]
    return rows[-1] if rows else None


# ---------------------------------------------------------------------------
# The pointer
# ---------------------------------------------------------------------------

def read_pointer(path: Path = POINTER) -> dict | None:
    """The retrieval that the last run of the download chose, or None when the download has not
    run."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise CacheError(f"the pointer {path} cannot be read as JSON ({error})") from error
    if not isinstance(value, dict):
        raise CacheError(f"the pointer {path} is not a JSON object")
    try:
        check_id(value.get("retrieval_id", ""))
    except CacheError as error:
        raise CacheError(f"the pointer {path} names no retrieval ({error})") from None
    return value


def write_pointer(rid: str, path: Path = POINTER) -> None:
    """Write the pointer to retrieval `rid`, the retrieval that the stage reads, in one step
    (`caches.replace_file`). The file contains only the id. A run that falls back to the
    retrieval that the pointer already names writes the same bytes, and the steps recorded by
    those bytes stay complete."""
    caches.replace_file(Path(path), json.dumps({"retrieval_id": check_id(rid)}) + "\n")


def pointed_retrieval(path: Path = POINTER, cache_dir: Path = CACHE_DIR) -> dict:
    """The index row of the retrieval that the pointer names, or CacheError."""
    pointer = read_pointer(path)
    if pointer is None:
        raise CacheError(f"the pointer {path} does not exist. Run the download, "
                         f"1_download_ghs.py, first")
    return index_row(pointer["retrieval_id"], cache_dir)


# ---------------------------------------------------------------------------
# Importing a GHS search result of the earlier download
# ---------------------------------------------------------------------------

# The earlier download's name for each filter, and GHS's name for the parameter.
_EARLIER_PARAMETERS = {
    "contributors_min": "contributorsMin", "pulls_min": "pullsMin",
    "created_max": "createdMax", "committed_min": "committedMin",
    "exclude_forks": "excludeForks", "has_license": "hasLicense",
}


def import_search_result(search_result: Path, end_date: str,
                         cache_dir: Path = CACHE_DIR) -> dict:
    """Add a GHS search result that the download wrote before this cache existed,
    `ghs_complete.json` with its `_metadata` block, as an `imported` retrieval, and return its
    index row."""
    with open(search_result, encoding="utf-8") as f:
        data = json.load(f)
    metadata = data.get("_metadata") or {}
    items = data.get("items")
    if not isinstance(items, list) or not metadata.get("downloaded_at"):
        raise CacheError(f"the file {search_result} is not a GHS search result of the earlier "
                         f"download, because it has no `items` list or no "
                         f"`_metadata.downloaded_at`")
    started = datetime.fromisoformat(metadata["downloaded_at"].replace("Z", "+00:00"))
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    started = started.replace(microsecond=0)
    rid = retrieval_id(started)
    if any(row["retrieval_id"] == rid for row in read_index(cache_dir)):
        raise CacheError(f"the index already has retrieval {rid}")
    resolved = metadata.get("resolved_filters") or {}
    query = {_EARLIER_PARAMETERS.get(key, key): value for key, value in resolved.items()}
    count, sha = write_rows(cache_dir, rid, "ghs", (normalize(item) for item in items))
    digest = hashlib.sha256(Path(search_result).read_bytes()).hexdigest()
    duration = float(metadata.get("download_duration_seconds") or 0)
    return put_index_row({
        "retrieval_id": rid, "outcome": IMPORTED,
        "started_at": iso(started),
        "finished_at": iso(started + timedelta(seconds=duration)) if duration else "",
        "end_date_source": "imported", "end_date": end_date,
        "created_max": resolved.get("created_max", ""),
        "committed_min": resolved.get("committed_min", ""),
        "query": query, "server_parameters": data.get("parameters") or {},
        "item_count": count, "ghs_file_sha256": sha,
        "imported_from": f"{Path(search_result).name} sha256:{digest}",
    }, cache_dir)


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain the cache of GHS retrievals.")
    commands = parser.add_subparsers(dest="command", required=True)
    importing = commands.add_parser("import", help="Add a GHS search result that the earlier "
                                                   "download wrote")
    importing.add_argument("search_result", type=Path,
                           help="The GHS search result, such as ghs_complete.json")
    importing.add_argument("--end-date", required=True,
                           help="The end date of the sampling window of the GHS search result, "
                                "YYYY-MM-DD")
    importing.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    args = parser.parse_args(argv)
    try:
        datetime.strptime(args.end_date, "%Y-%m-%d")
        row = import_search_result(args.search_result, args.end_date, args.cache_dir)
    except (CacheError, OSError, ValueError) as error:
        logs.error(COMPONENT, f"Could not import {args.search_result} ({error}).")
        return 1
    logs.info(COMPONENT, f"Imported {args.search_result} as retrieval {row['retrieval_id']} "
                         f"({int(row['item_count']):,} repositories).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
