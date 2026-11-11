"""The cache of labels: every decision that the classification made about a repository, as plain
files under `labels/`. A label is made once for a README and reused while nothing that decided
it has changed.

A decision is either a label, derived from the model's response for one README, or an exclusion,
a README that the classification could not use. A label is reused while the README's wording, the
description, the model and the classification parameters are unchanged, and the classification
derives it again from the stored response under its current rules. The classification parameters
are everything else that the model was given: the prompt, the tool descriptions, the response
format and the settings of the request, named by the SHA-256 of their canonical JSON. An
exclusion is a missing or empty README or a README that the language check finds not to be in
English. It is reused while the README and the classification parameters are unchanged. Each
decision records the language check that it was made under, which is not among the
classification parameters.

Every lookup matches a recorded digest by its key (`ParameterKeys`): the digest of the
parameters that the cache records under it, without the provider of a model. Parameters recorded
before 2026-11-04 name the provider of each model, and the maintainer decided that day that the
provider is no classification parameter. Their decisions therefore count as decisions under the
parameters that differ from them only in the provider, without a rewrite of a stored file.

    labels/
      index.csv                        one row per run, keyed by its id
      <id>.decisions-<NNN>.jsonl.xz    the decisions that the run made, one JSON object per
                                       line, in the order in which they were made, in as many
                                       numbered parts as keep each file under GitHub's limit.
                                       Decisions reused from an earlier run are not written
                                       again
      <id>.labels.csv.xz               the outcome of every repository that the run covered:
                                       its label or exclusion, gone, or undecided with what
                                       failed and the error as its reason, and the run that
                                       made its decision, so that a run's labels can be read
                                       back whole
      <id>.changes-<NNN>.jsonl.xz      the comparison with the previous run over a
                                       sampling frame: one line per repository with a changed
                                       outcome, with the repeated labels that determine the
                                       cause
      <id>.tokens.csv                  the tokens of the run's requests and of its comparison, by
                                       the role of the model, the model that the requests named,
                                       the service tier that each request named and the tier that
                                       its response reported
      <id>.cost.csv                    the cost record of the run: its tokens at the prices of
                                       config/model_prices.csv (costs.py)
      parameters/<sha256>.json         the classification parameters that a label names, in full
      <id>.partial.jsonl               a run in progress, one line per repository in the
                                       sampling frame that it finished or failed, untracked
      <id>.changes.partial.jsonl       a comparison in progress, untracked

A run is named by the UTC time at which it started, such as `2026-10-06T081500Z`. The cache keeps
labels permanently, as the sampling stage keeps its retrievals, because a later label shows what
changed only against an earlier label, and the label evaluation compares the labels of two models
for the same README. Every file but a partial file is written whole under a temporary name and
synced to disk before the rename. A stopped run or a power loss therefore leaves either the old
file or the new file. A partial file grows by a line at a time, and a line that a stop cut short
is dropped before the next line is appended (`end_partial`). Every reader of a partial file reads
it one line at a time and leaves out a last line that is cut short (`partial_lines`).
"""

from __future__ import annotations

import csv
import fcntl
import hashlib
import io
import json
import lzma
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from agentcontext import CLASSIFICATION_DIR, caches

CACHE_DIR = CLASSIFICATION_DIR / "labels"
INDEX_NAME = "index.csv"
XZ_PRESET = 6
# A part of a run's decisions stays under 95 MB, because GitHub refuses a file over 100 MB.
MAX_FILE_BYTES = 95_000_000

LABELED, EXCLUDED, GONE = "labeled", "excluded", "gone"
# The outcome, in a run's labels, of a repository that the run left without a decision after
# its retry rounds. Because it is no decision, no run reuses it, and the next run tries the
# repository again.
UNDECIDED = "undecided"
# The scope of a run that covered the whole sampling frame. A run over a list of repositories or
# the first N is a trial or a label evaluation, and no run is compared with such a run.
SAMPLING_FRAME_SCOPE = "sampling_frame"
# The outcomes of a run that has not finished. A later run can resume its partial file.
RESUMABLE = ("running", "incomplete")

INDEX_COLUMNS = [
    "run_id", "started_at", "finished_at", "outcome", "model", "parameters",
    "retrieval_id", "scope", "repositories", "labeled", "excluded", "reused", "gone",
    "github_requests", "model_requests", "input_tokens", "cached_tokens", "output_tokens",
    "reasoning_tokens", "summary_input_tokens", "summary_output_tokens", "decisions",
    "decisions_sha256", "labels_sha256", "compared_with", "label_changes", "changes_sha256",
]
LABEL_COLUMNS = ["id", "repo_name", "outcome", "label", "reason", "decided_in"]
# The tokens of a run, one row per role of the model, model that the requests named, service tier
# that they named and service tier that their responses reported, which is empty for a response
# that reported none. For an OpenRouter model, both tiers are the provider slug of the endpoint
# that served the responses, the pinned provider or the backup provider. The index sums the tokens without their tier and without telling apart the tokens that
# the API wrote to its prompt cache, which OpenAI bills at a rate of their own. `costs.py` prices
# the rows.
CLASSIFYING_MODEL, SUMMARY_MODEL = "classifying_model", "summary_model"
TOKEN_KEY = ["role", "model", "requested_tier", "service_tier"]
TOKEN_COUNTS = ["requests", "input_tokens", "cached_tokens", "cache_write_tokens",
                "output_tokens", "reasoning_tokens"]
TOKEN_COLUMNS = TOKEN_KEY + TOKEN_COUNTS
# The columns of the index that record what a run and its comparison spent.
SPENT_COLUMNS = INDEX_COLUMNS[INDEX_COLUMNS.index("github_requests"):
                              INDEX_COLUMNS.index("decisions")]

_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")


class CacheError(Exception):
    """The cache cannot be read as written."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment: datetime) -> str:
    """A UTC time to the second, as every file of the cache writes it."""
    return moment.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_id(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def new_run_id(cache_dir: Path = CACHE_DIR, now: datetime | None = None) -> str:
    """The id of a run starting `now`: its UTC second, or the first second after it that no
    run of the index has, so that two runs started in one second keep a row each."""
    taken = {row["run_id"] for row in read_index(cache_dir)}
    moment = now or utc_now()
    while run_id(moment) in taken:
        moment += timedelta(seconds=1)
    return run_id(moment)


def add_run(values: dict, cache_dir: Path = CACHE_DIR, now: datetime | None = None) -> str:
    """Add the row of a new run with `values` under the id of `new_run_id`, and return the id.
    The id is chosen and its row written under one lock of the index, so that two runs started
    in the same second get an id each."""
    with index_lock(cache_dir):
        rid = new_run_id(cache_dir, now)
        _put_index_row({**values, "run_id": rid}, cache_dir)
    return rid


def check_id(value: str) -> str:
    if not _ID_RE.match(value or ""):
        raise CacheError(f"{value!r} is not a run id such as 2026-10-06T081500Z")
    return value


def canonical(value) -> str:
    """The JSON that a record is hashed as: sorted keys, no whitespace, non-ASCII kept."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def record(kind: str, value: dict, cache_dir: Path = CACHE_DIR) -> str:
    """Write `value` under `<kind>/<sha256>.json` unless it is there, and return its SHA-256."""
    sha = digest(value)
    path = Path(cache_dir) / kind / f"{sha}.json"
    if not path.is_file():
        _write_whole(path, (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2)
                            + "\n").encode("utf-8"))
    return sha


def recorded(kind: str, sha: str, cache_dir: Path = CACHE_DIR) -> dict:
    """The value written under `<kind>/<sha>.json`, checked against its name."""
    path = Path(cache_dir) / kind / f"{sha}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CacheError(f"{path} cannot be read ({error})") from error
    if digest(value) != sha:
        raise CacheError(f"the SHA-256 of {path} does not match its name")
    return value


# The blocks of the classification parameters that name a model, and the field of such a block
# in which parameters recorded before 2026-11-04 name the provider of the model's endpoint. The
# maintainer decided on 2026-11-04 that "the model, quantization, configuration should be part
# of, but not the [...] provider", and `ParameterKeys` leaves the field out.
MODEL_BLOCKS = ("classifying_model", "summary", "second_model")
PROVIDER = "provider"


def without_provider(value: dict) -> dict:
    """Recorded classification parameters without the provider of any model (MODEL_BLOCKS):
    the parameters as the classification records them since 2026-11-04."""
    return {key: ({name: field for name, field in part.items() if name != PROVIDER}
                  if key in MODEL_BLOCKS and isinstance(part, dict) else part)
            for key, part in value.items()}


class ParameterKeys:
    """The key under which a run matches the classification parameters that the cache records
    under a digest: the digest of those parameters without the provider of any model
    (`without_provider`). Parameters recorded since 2026-11-04 name no provider, and their key is
    their digest. Parameters recorded before name the provider, and their key is the digest of
    the same parameters as the classification records them since, so that a run reuses their
    decisions, kept responses and partial file. Parameters that differ in the model, the Hugging
    Face id, the quantization or any setting keep keys of their own. A digest without a record
    in the cache, such as a digest that a test names, is its own key: a run writes the record of
    its own parameters as it starts, and an unrecorded digest names no parameters of a run. Raises
    `CacheError` for a record that cannot be read or does not match its name."""

    def __init__(self, cache_dir: Path = CACHE_DIR):
        self.cache_dir, self.known = Path(cache_dir), {}

    def __call__(self, sha: str) -> str:
        if sha not in self.known:
            if sha and (self.cache_dir / "parameters" / f"{sha}.json").is_file():
                self.known[sha] = digest(without_provider(recorded("parameters", sha,
                                                                   self.cache_dir)))
            else:
                self.known[sha] = sha
        return self.known[sha]


def shared_parameters(cache_dir: Path, parameters: str, known: dict | None = None) -> str:
    """The digest of the classification parameters `parameters` without the block that names the
    classifying model with its endpoint and the settings of its requests, `classifying_model`
    (`1_classify_repos.classification_parameters`), and without the provider of any other model
    (`without_provider`): what the labels of two models share when the models read the same
    prompt, tools, summaries and README. For parameters recorded without that block, the digest
    covers all of the parameters but the provider. `known` keeps the digests already
    computed."""
    known = known if known is not None else {}
    if parameters not in known:
        value = without_provider(recorded("parameters", parameters, cache_dir))
        known[parameters] = digest({key: part for key, part in value.items()
                                    if key != CLASSIFYING_MODEL})
    return known[parameters]


def _write_whole(path: Path, data: bytes) -> None:
    """Write `data` to `path` in one step: a temporary file beside it, synced to disk, then a
    rename, and the directory synced after it.

    On a power loss, a rename that reached the disk before the data would leave an empty or
    truncated file under the name, and an index that cannot be read stops every later run.
    These files are bytes, which `caches.replace_file` does not write, and the function syncs
    the directory itself through `caches.sync_directory`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    caches.sync_directory(path.parent)


def decisions_path(cache_dir: Path, rid: str, part: int = 1) -> Path:
    """The file of part `part` of run `rid`'s decisions, counted from 1."""
    return Path(cache_dir) / f"{check_id(rid)}.decisions-{part:03d}.jsonl.xz"


def changes_path(cache_dir: Path, rid: str, part: int = 1) -> Path:
    """The file of part `part` of the comparison of run `rid` with the previous run."""
    return Path(cache_dir) / f"{check_id(rid)}.changes-{part:03d}.jsonl.xz"


def labels_path(cache_dir: Path, rid: str) -> Path:
    return Path(cache_dir) / f"{check_id(rid)}.labels.csv.xz"


def partial_path(cache_dir: Path, rid: str) -> Path:
    return Path(cache_dir) / f"{check_id(rid)}.partial.jsonl"


def changes_partial_path(cache_dir: Path, rid: str) -> Path:
    return Path(cache_dir) / f"{check_id(rid)}.changes.partial.jsonl"


def tokens_path(cache_dir: Path, rid: str) -> Path:
    """The file of the tokens of run `rid` by the role, the model and the service tier."""
    return Path(cache_dir) / f"{check_id(rid)}.tokens.csv"


def cost_path(cache_dir: Path, rid: str) -> Path:
    """The cost record of run `rid`, which `costs.py` writes."""
    return Path(cache_dir) / f"{check_id(rid)}.cost.csv"


def read_tokens(cache_dir: Path, rid: str) -> list[dict]:
    """The rows of the tokens file of run `rid`, with their counts as integers, or no rows for a
    run without the file."""
    path = tokens_path(cache_dir, rid)
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise CacheError(f"{path} cannot be read ({error})") from error
    if reader.fieldnames != TOKEN_COLUMNS:
        raise CacheError(f"{path} has the columns {reader.fieldnames}, not {TOKEN_COLUMNS}")
    try:
        return [{**{key: row[key] for key in TOKEN_KEY},
                 **{key: int(row[key]) for key in TOKEN_COUNTS}} for row in rows]
    except ValueError as error:
        raise CacheError(f"{path} has a count that is not a whole number ({error})") from error


def add_tokens(cache_dir: Path, rid: str, billed: dict) -> None:
    """Add `billed`, the counts of TOKEN_COUNTS by the tuple of TOKEN_KEY, to the tokens file of
    run `rid`, under the lock of the index.

    Each invocation of a run and of its comparison adds the tokens that its own responses
    reported, once, as it ends. An invocation that a kill ends adds nothing, and the index, which
    sums the tokens that each line of a partial file records, then counts more tokens than this
    file. `costs.py` prices that difference at the tier that the requests named."""
    if not billed:
        return
    with index_lock(cache_dir):
        rows = {tuple(row[key] for key in TOKEN_KEY): row for row in read_tokens(cache_dir, rid)}
        for key, counts in billed.items():
            row = rows.setdefault(tuple(key), {**dict(zip(TOKEN_KEY, key)),
                                               **dict.fromkeys(TOKEN_COUNTS, 0)})
            for name in TOKEN_COUNTS:
                row[name] += int(counts.get(name) or 0)
        text = io.StringIO()
        writer = csv.DictWriter(text, fieldnames=TOKEN_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(row for _key, row in sorted(rows.items()))
        _write_whole(tokens_path(cache_dir, rid), text.getvalue().encode("utf-8"))


def kept_path(cache_dir: Path, rid: str) -> Path:
    """The file of the model's responses that run `rid` kept for the repositories that it
    finished without a decision, because GitHub failed the requests for their facts."""
    return Path(cache_dir) / f"{check_id(rid)}.kept.jsonl.xz"


def write_kept(cache_dir: Path, rid: str, kept: dict) -> None:
    """Write `kept`, the model's responses by the key under which the run keeps them, as run
    `rid`'s kept responses, or remove the file when `kept` is empty."""
    path = kept_path(cache_dir, rid)
    if not kept:
        path.unlink(missing_ok=True)
        return
    lines = [(json.dumps({"key": key, "response": response}, ensure_ascii=False,
                         separators=(",", ":")) + "\n").encode("utf-8")
             for key, response in sorted(kept.items())]
    _write_whole(path, lzma.compress(b"".join(lines), preset=XZ_PRESET))


def read_kept(path: Path) -> dict:
    """The responses of a file that `write_kept` wrote, by their key, or none when the file
    cannot be read or fails the check of its xz container. A response that a later run does not
    find only costs a new request, while a damaged response would decide a label."""
    try:
        data = lzma.decompress(Path(path).read_bytes())
    except (OSError, lzma.LZMAError, EOFError):
        return {}
    kept = {}
    for raw in data.split(b"\n"):
        try:
            line = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            return {}
        if isinstance(line, dict) and "key" in line and "response" in line:
            kept[line["key"]] = line["response"]
    return kept


def _parts(lines: list[bytes], rid: str) -> list[bytes]:
    """`lines` compressed into as few consecutive parts as keep each under MAX_FILE_BYTES: the
    whole when it fits, else each half, and so on. Compressing a candidate part gives its exact
    size, unlike an estimate, and the parts are written only once per run."""
    compressor = lzma.LZMACompressor(preset=XZ_PRESET)
    data = b"".join([*(compressor.compress(line) for line in lines), compressor.flush()])
    if len(data) < MAX_FILE_BYTES:
        return [data]
    if len(lines) == 1:
        raise CacheError(f"a decision of run {rid} is {len(data):,} bytes after compression, "
                         f"more than the {MAX_FILE_BYTES:,} bytes allowed for a file in the "
                         f"repository")
    middle = len(lines) // 2
    return _parts(lines[:middle], rid) + _parts(lines[middle:], rid)


def _write_parts(path_of, cache_dir: Path, rid: str, items: Iterable[dict]) -> tuple[int, str]:
    """Write `items` as JSON lines in parts under MAX_FILE_BYTES at `path_of(cache_dir, rid,
    part)`, and return the number written and the SHA-256 of each part, in order and separated
    by spaces.

    The function removes every part that a previous attempt at the same run left beyond the last
    part written, so that the files on disk are the parts that the index names.
    """
    lines = [(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
             .encode("utf-8") for item in items]
    parts = _parts(lines, rid)
    for number, data in enumerate(parts, 1):
        _write_whole(path_of(cache_dir, rid, number), data)
    stale = len(parts) + 1
    while path_of(cache_dir, rid, stale).exists():
        path_of(cache_dir, rid, stale).unlink()
        stale += 1
    return len(lines), " ".join(hashlib.sha256(data).hexdigest() for data in parts)


def write_decisions(cache_dir: Path, rid: str, decisions: Iterable[dict]) -> tuple[int, str]:
    """Write the decisions of run `rid` in parts under MAX_FILE_BYTES, and return the number
    written and the SHA-256 of each part, in order and separated by spaces, as the index records
    them."""
    return _write_parts(decisions_path, cache_dir, rid, decisions)


def write_changes(cache_dir: Path, rid: str, changes: Iterable[dict]) -> tuple[int, str]:
    """Write the comparison of run `rid` as `write_decisions` writes its decisions."""
    return _write_parts(changes_path, cache_dir, rid, changes)


def read_decisions(cache_dir: Path, rid: str, checksums: str) -> Iterator[dict]:
    """The decisions of run `rid`, from the parts that `checksums` lists by SHA-256 in order,
    each checked against its checksum."""
    return _read_parts(decisions_path, cache_dir, rid, checksums)


def read_changes(cache_dir: Path, rid: str, checksums: str) -> Iterator[dict]:
    """The comparison of run `rid`, read as `read_decisions` reads decisions."""
    return _read_parts(changes_path, cache_dir, rid, checksums)


# The head of a line of a partial file as the run and the comparison write it, with the
# separators of `json.dumps(..., separators=(",", ":"))`: the repository's GitHub id and the
# line's status, which are its first two keys.
_PARTIAL_HEAD = re.compile(rb'\{"id":\d+,"status":"([a-z]+)"')


def partial_lines(path: Path, statuses: Iterable[str] | None = None) -> Iterator[dict]:
    """The lines of a partial file as JSON objects, in the order of the file, or only the lines
    with a status in `statuses`.

    The file is read in binary, one line at a time, and only "\\n" ends a line, as in
    `_read_parts`. The partial file of a run over the whole sampling frame keeps every tool
    output, and its text, read whole, would take four bytes a character once one character is
    outside ASCII. A last line without its "\\n", cut short by a stop inside a character of UTF-8
    or still being written by a run in progress, is left out, as is a line that is not JSON. A
    line is skipped without being parsed when its head shows a status outside `statuses`."""
    wanted = None if statuses is None else set(statuses)
    path = Path(path)
    if not path.is_file():
        return
    with open(path, "rb") as f:
        for raw in f:
            if not raw.endswith(b"\n"):
                return
            if wanted is not None:
                head = _PARTIAL_HEAD.match(raw)
                if head is not None and head.group(1).decode("ascii") not in wanted:
                    continue
            try:
                line = json.loads(raw)
            except ValueError:
                continue
            if isinstance(line, dict) and (wanted is None or line.get("status") in wanted):
                yield line


def end_partial(path: Path) -> None:
    """Drop the last line of a partial file when a stop cut it short, so that the next appended
    line starts a line of its own. Appended to the cut line, the new line would be read as part
    of it and dropped with it, and its decision would be missing from the run's decisions file."""
    path = Path(path)
    if not path.is_file():
        return
    with open(path, "r+b") as f:
        end = f.seek(0, os.SEEK_END)
        position = end
        while position > 0:
            start = max(0, position - 65536)
            f.seek(start)
            chunk = f.read(position - start)
            if position == end and chunk.endswith(b"\n"):
                return
            found = chunk.rfind(b"\n")
            if found >= 0:
                f.truncate(start + found + 1)
                return
            position = start
        f.truncate(0)


class Spending:
    """What a run or a comparison spent since the last line of its partial file, from
    `counts()`, its requests and tokens so far. Each line records what was spent before it. The
    requests and tokens of an invocation that a kill ended then stay in the partial file, while
    the index, written only at an orderly end, would leave them out."""

    def __init__(self, counts):
        self.counts = counts
        self.recorded = counts()

    def since(self) -> dict:
        """The requests and tokens spent since the last call, for each count that changed."""
        now = self.counts()
        spent = {key: value - self.recorded.get(key, 0) for key, value in now.items()
                 if value != self.recorded.get(key, 0)}
        self.recorded = now
        return spent


def spent_in(path: Path, keys) -> dict:
    """The requests and tokens that the lines of a partial file record as spent, summed, for
    each of `keys`."""
    totals = dict.fromkeys(keys, 0)
    for line in partial_lines(path):
        spent = line.get("spent") or {}
        for key in totals:
            totals[key] += int(spent.get(key) or 0)
    return totals


def _read_parts(path_of, cache_dir: Path, rid: str, checksums: str) -> Iterator[dict]:
    """The items of run `rid` in the parts at `path_of(cache_dir, rid, part)`, each part checked
    against its SHA-256 in `checksums` before any of its lines is read.

    A part is read in two passes over one open file: the first computes the SHA-256 of its
    compressed bytes in blocks, and the second decompresses it one line at a time. The lines
    therefore come from the bytes that were checked. A part keeps every tool output of its
    decisions, and every run reads every part (`Decisions.load`). Decompressed whole, a part's
    text would take four bytes a character once one character is outside ASCII, and its lines as
    much again.

    Only "\\n" ends a line. A JSON string written with `ensure_ascii=False` keeps U+2028, U+2029
    and U+0085 as they are, and `str.splitlines` would end a line at each of them and cut a
    decision with one of them in its justification in two."""
    for part, expected in enumerate(checksums.split(), 1):
        path = path_of(cache_dir, rid, part)
        try:
            handle = open(path, "rb")
        except OSError as error:
            raise CacheError(f"{path} cannot be read ({error})") from error
        with handle:
            checksum = hashlib.sha256()
            try:
                for block in iter(lambda: handle.read(1 << 20), b""):
                    checksum.update(block)
            except OSError as error:
                raise CacheError(f"{path} cannot be read ({error})") from error
            if checksum.hexdigest() != expected:
                raise CacheError(f"{path} does not match the SHA-256 that the index records "
                                 f"for it")
            handle.seek(0)
            with lzma.open(handle) as lines:
                number = 0
                while True:
                    try:
                        raw = lines.readline()
                        if not raw:
                            break
                        line = raw.decode("utf-8")
                    except (lzma.LZMAError, EOFError, UnicodeDecodeError) as error:
                        raise CacheError(f"{path} is not an xz file of UTF-8 text "
                                         f"({error})") from error
                    except OSError as error:
                        raise CacheError(f"{path} cannot be read ({error})") from error
                    number += 1
                    try:
                        item = json.loads(line)
                    except ValueError as error:
                        raise CacheError(f"line {number} of {path} is not JSON "
                                         f"({error})") from error
                    yield item


def read_index(cache_dir: Path = CACHE_DIR) -> list[dict]:
    """The rows of the index in run order, or no rows for a cache without an index."""
    path = Path(cache_dir) / INDEX_NAME
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != INDEX_COLUMNS:
            raise CacheError(f"{path} has the columns {reader.fieldnames}, not {INDEX_COLUMNS}")
        rows = list(reader)
    for row in rows:
        check_id(row["run_id"])
    return sorted(rows, key=lambda row: row["run_id"])


@contextmanager
def index_lock(cache_dir: Path = CACHE_DIR):
    """An exclusive lock of the index, held from reading it to writing it. Without the lock, two
    runs at once, such as the two models over one check sample, would each read the index, change
    their own row and write the index, and the later write would undo the other run's change.
    The lock is an `flock` on the cache's directory, which is never replaced, and it ends with
    the process."""
    Path(cache_dir).mkdir(parents=True, exist_ok=True)
    handle = os.open(cache_dir, os.O_RDONLY)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        os.close(handle)


def put_index_row(values: dict, cache_dir: Path = CACHE_DIR) -> dict:
    """Add the row of run `values["run_id"]`, or update the cells that `values` names, under the
    lock of the index, and return the row."""
    with index_lock(cache_dir):
        return _put_index_row(values, cache_dir)


def _put_index_row(values: dict, cache_dir: Path) -> dict:
    rows = read_index(cache_dir)
    rid = check_id(values["run_id"])
    row = next((r for r in rows if r["run_id"] == rid), None)
    if row is None:
        row = {column: "" for column in INDEX_COLUMNS}
        rows.append(row)
    unknown = set(values) - set(INDEX_COLUMNS)
    if unknown:
        raise CacheError(f"the index has no column {sorted(unknown)}")
    row.update({k: "" if v is None else str(v) for k, v in values.items()})
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=INDEX_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(sorted(rows, key=lambda r: r["run_id"]))
    _write_whole(Path(cache_dir) / INDEX_NAME, text.getvalue().encode("utf-8"))
    return row


def write_labels(cache_dir: Path, rid: str, rows: Iterable[dict]) -> str:
    """Write the outcome of every repository that run `rid` covered, in LABEL_COLUMNS, and
    return the file's SHA-256, as the index records it."""
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=LABEL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in LABEL_COLUMNS} for row in rows)
    data = lzma.compress(text.getvalue().encode("utf-8"), preset=XZ_PRESET)
    _write_whole(labels_path(cache_dir, rid), data)
    return hashlib.sha256(data).hexdigest()


def read_labels(cache_dir: Path, rid: str, checksum: str) -> list[dict]:
    """The outcome of every repository that run `rid` covered, checked against `checksum`."""
    path = labels_path(cache_dir, rid)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise CacheError(f"{path} cannot be read ({error})") from error
    if hashlib.sha256(data).hexdigest() != checksum:
        raise CacheError(f"{path} does not match the SHA-256 that the index records for it")
    try:
        reader = csv.DictReader(io.StringIO(lzma.decompress(data).decode("utf-8")))
        rows = list(reader)
    except (lzma.LZMAError, UnicodeDecodeError, csv.Error) as error:
        raise CacheError(f"{path} is not an xz file of a CSV ({error})") from error
    if reader.fieldnames != LABEL_COLUMNS:
        raise CacheError(f"{path} has the columns {reader.fieldnames}, not {LABEL_COLUMNS}")
    return rows


def previous_run(cache_dir: Path, rid: str) -> dict | None:
    """The index row of the run over a sampling frame that finished last before run `rid`
    started, or None when there is none."""
    earlier = [row for row in read_index(cache_dir)
               if row["run_id"] < check_id(rid) and row["outcome"] == "finished"
               and row["scope"] == SAMPLING_FRAME_SCOPE and row["labels_sha256"]]
    return earlier[-1] if earlier else None


def decisions_of(cache_dir: Path, wanted: dict) -> dict:
    """The decisions that `wanted`, {run id: set of GitHub ids}, names, by (run id, GitHub id).
    Each run's files are read once."""
    rows = {row["run_id"]: row for row in read_index(cache_dir)}
    found = {}
    for rid, ids in wanted.items():
        if rid not in rows or not rows[rid]["decisions_sha256"]:
            raise CacheError(f"run {rid} has no decisions in the index")
        for decision in read_decisions(cache_dir, rid, rows[rid]["decisions_sha256"]):
            if int(decision["id"]) in ids:
                found[(rid, int(decision["id"]))] = decision
        missing = sorted(i for i in ids if (rid, i) not in found)
        if missing:
            raise CacheError(f"the labels name run {rid} as the run that decided repository "
                             f"{missing[0]}, but the decisions of that run have none for it")
    return found


def all_decisions(cache_dir: Path = CACHE_DIR) -> Iterator[tuple[str, dict]]:
    """Every decision of every run that wrote its decisions, oldest run first, with the run's
    id."""
    for row in read_index(cache_dir):
        if row["decisions_sha256"]:
            for decision in read_decisions(cache_dir, row["run_id"], row["decisions_sha256"]):
                yield row["run_id"], decision


def reusable(decision: dict, model: str, parameters: str, keys=None) -> bool:
    """Whether a run of `model` under the classification parameters `parameters` can reuse
    `decision`: a label under the same model and parameters, or an exclusion under the same
    parameters, which no model decided. `keys` gives the key of a digest (`ParameterKeys`), by
    which the parameters are compared, and the digest itself by default."""
    keys = keys or str
    same = keys(decision.get("parameters") or "") == keys(parameters)
    if decision.get("outcome") == LABELED:
        return decision.get("model") == model and same
    return decision.get("outcome") == EXCLUDED and same


class Decisions:
    """The latest decision per repository and per what decided it, for reuse.

    A label is kept under (GitHub id, model, key of the classification parameters), and an
    exclusion, which no model decided, under (GitHub id, key of the classification parameters).
    `keys` gives the key of a digest (`ParameterKeys`), the digest itself by default. The later
    of two decisions under one key replaces the earlier decision. `load` takes only the decisions
    that one model under one set of classification parameters can reuse. `add` also takes the
    decisions of the run in progress, so that a resumed run reuses what it decided before it
    stopped. It takes the decisions of a partial file that no decisions file contains as well,
    and `written` tells these decisions apart from the others.
    """

    def __init__(self, keys=None):
        self.labels, self.exclusions, self.origins = {}, {}, {}
        self.in_files: set = set()
        self.keys = keys or str

    def add(self, decision: dict, rid: str = "", written: bool = True) -> None:
        """Keep `decision` of run `rid`. Unless `written`, the decision is in a partial file, and
        a decisions file contains it only if one contained it before.

        A decision that a decisions file contains keeps that file's run as its origin, whichever
        partial file also contains it. A resumed run writes a decision that it carried from the
        partial file of a run that a kill ended. A later run that reuses the decision names its
        origin in its labels, and the comparison reads the decision from the origin's decisions
        file."""
        made = (int(decision["id"]), decision.get("decided_at", ""))
        if not written and made in self.in_files:
            return
        self.origins[made] = rid
        if written:
            self.in_files.add(made)
        if decision.get("outcome") == LABELED:
            key = (int(decision["id"]), decision["model"], self.keys(decision["parameters"]))
            self.labels[key] = (rid, decision)
        elif decision.get("outcome") == EXCLUDED:
            self.exclusions[(int(decision["id"]),
                             self.keys(decision["parameters"]))] = (rid, decision)

    @classmethod
    def load(cls, cache_dir: Path = CACHE_DIR, *, model: str, parameters: str, partial=None,
             skip: str = "") -> "Decisions":
        """The decisions that a run of `model` under the classification parameters `parameters`
        can reuse (`reusable`), from every run that wrote them, oldest run first, matched by the
        keys of their parameters (`ParameterKeys`). With `partial`, a function that returns the
        decisions of a partial file, also those in the partial file of every running or
        incomplete run but `skip`. A run that a kill ended wrote no decisions file, and a run
        over another sampling frame or scope does not resume it.

        A run looks up only the decisions under its own model and parameters, and each decision
        keeps every tool output. Holding the decisions of the other models and parameters too
        would hold every run of the cache in memory for the whole run."""
        keys = ParameterKeys(cache_dir)
        decisions = cls(keys)
        for row in read_index(cache_dir):
            rid = row["run_id"]
            if row["decisions_sha256"]:
                for decision in read_decisions(cache_dir, rid, row["decisions_sha256"]):
                    if reusable(decision, model, parameters, keys):
                        decisions.add(decision, rid)
            path = partial_path(cache_dir, rid)
            if (partial is not None and rid != skip and row["outcome"] in RESUMABLE
                    and path.is_file()):
                for decision in partial(path):
                    if reusable(decision, model, parameters, keys):
                        decisions.add(decision, rid, written=False)
        return decisions

    @staticmethod
    def kept(cache_dir: Path, model: str, parameters: str, partial=None,
             skip: str = "") -> dict:
        """The model's responses, by their key, that the runs under `model` and the
        classification parameters `parameters`, matched by their keys (`ParameterKeys`), kept
        for repositories that they left without a decision. The responses come from the kept
        file of every finished run and, with `partial`, a function that returns the kept
        responses of a partial file, from the partial file of every running or incomplete run
        but `skip`. A later run then requests only GitHub's facts for the same input instead of
        paying for a new response of the model."""
        kept = {}
        keys = ParameterKeys(cache_dir)
        for row in read_index(cache_dir):
            rid = row["run_id"]
            if (rid == skip or row["model"] != model
                    or keys(row["parameters"]) != keys(parameters)):
                continue
            if row["outcome"] in RESUMABLE:
                path = partial_path(cache_dir, rid)
                if partial is not None and path.is_file():
                    kept.update(partial(path))
            elif kept_path(cache_dir, rid).is_file():
                kept.update(read_kept(kept_path(cache_dir, rid)))
        return kept

    def written(self, decision: dict) -> bool:
        """Whether a decisions file contains `decision`, or the run in progress made it and
        writes it."""
        return (int(decision["id"]), decision.get("decided_at", "")) in self.in_files

    def latest(self, repository_id: int, model: str, parameters: str) -> dict | None:
        """The most recent label under this model and these classification parameters, or
        exclusion, for the repository, or None when there is neither."""
        key = self.keys(parameters)
        found = [entry for entry in (self.labels.get((repository_id, model, key)),
                                     self.exclusions.get((repository_id, key)))
                 if entry is not None]
        if not found:
            return None
        return max(found, key=lambda entry: (entry[0], entry[1].get("decided_at", "")))[1]

    def label(self, repository_id: int, model: str, parameters: str) -> dict | None:
        entry = self.labels.get((repository_id, model, self.keys(parameters)))
        return entry[1] if entry else None

    def exclusion(self, repository_id: int, parameters: str) -> dict | None:
        entry = self.exclusions.get((repository_id, self.keys(parameters)))
        return entry[1] if entry else None

    def origin(self, decision: dict) -> str:
        """The run with the decisions file that contains `decision`, or the run in progress that
        writes it, else the run of the partial file that contains it."""
        return self.origins.get((int(decision["id"]), decision.get("decided_at", "")), "")
