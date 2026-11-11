"""Computations over the cache of GHS retrievals of the sampling stage: its index, `index.csv`,
the rows of each GHS search result, `<retrieval>.ghs.jsonl.xz`, and GitHub's response for each
row, `<retrieval>.github.jsonl.xz`. They count what the index records, what the rows of a
retrieval show, and what the requests of the GHS download for crawl dates cost.

The cache keeps every retrieval as GHS and GitHub returned it, and a later retrieval adds files
beside it, so a statistic of a retrieval stays the same after later retrievals. `cache` names the
directory of the cache, `data/sampling/ghs_retrievals`.

The rows and the responses of a retrieval are read once for a `Files` (`Files.json_lines`), both
files of a retrieval in parallel when a computation needs both.
"""

import importlib.util
import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from types import ModuleType

import yaml

from agentcontext.statistics import Files, Unavailable, checkout

CACHE = "data/sampling/ghs_retrievals"
SAMPLING_CODE = Path(__file__).resolve().parents[2] / "1_sampling"
# The module of the cache with the size of a volume of a cached file.
CACHE_CODE = "1_sampling/ghs_cache.py"
# The timestamp fields of a GHS row, which GHS writes on a 12-hour clock without AM/PM.
GHS_TIMES = ("createdAt", "pushedAt", "updatedAt", "lastCommit")
# The filter with the license step that decides which rows count as licensed, and its config.
# The script reads the files that the config names relative to its own directory.
FILTER_SCRIPT = "1_sampling/4_filter_ghs.py"
FILTER_CONFIG = "1_sampling/config/ghs_filter.yaml"
# The download, whose settings decide what the requests for crawl dates cost.
DOWNLOAD_SCRIPT = "1_sampling/1_download_ghs.py"
DOWNLOAD_CONFIG = "1_sampling/config/ghs_download.yaml"
GROUP_KINDS = ("same_name", "case_only", "same_head", "unrelated")


def comparison() -> ModuleType:
    """The module of `3_compare_retrievals.py`, loaded once by path, because a name that starts
    with a digit cannot be imported, with `1_sampling/` on the path for its `ghs_cache`."""
    name = "_statistics_compare_retrievals"
    if name not in sys.modules:
        if str(SAMPLING_CODE) not in sys.path:
            sys.path.append(str(SAMPLING_CODE))
        spec = importlib.util.spec_from_file_location(
            name, SAMPLING_CODE / "3_compare_retrievals.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[name] = module
    return sys.modules[name]


def _index(files: Files, cache: str) -> list:
    """The rows of the index of the cache."""
    return files.memo(("retrieval index", cache), lambda: list(files.rows(f"{cache}/index.csv")))


def index_row(files: Files, retrieval: str, cache: str = CACHE) -> dict:
    """The row of a retrieval in the index of the cache."""
    found = [row for row in _index(files, cache) if row["retrieval_id"] == retrieval]
    if len(found) != 1:
        raise Unavailable(f"{cache}/index.csv has {len(found)} rows of the retrieval "
                          f"{retrieval}, not one")
    return found[0]


def ghs_rows(files: Files, retrieval: str, cache: str = CACHE) -> list:
    """The rows of the GHS search result of a retrieval."""
    return files.json_lines(f"{cache}/{retrieval}.ghs.jsonl.xz")


def github_responses(files: Files, retrieval: str, cache: str = CACHE) -> list:
    """GitHub's response for each row of a retrieval."""
    return files.json_lines(f"{cache}/{retrieval}.github.jsonl.xz")


def _pairs(files: Files, retrieval: str, cache: str) -> list:
    """GHS's rows of a retrieval, each with GitHub's response, or with None for a retrieval
    without GitHub's responses."""
    def read() -> list:
        if not files.exists(f"{cache}/{retrieval}.github.jsonl.xz"):
            return [(row, None) for row in ghs_rows(files, retrieval, cache)]
        # Decompressing releases the interpreter lock, so the responses are read beside the rows.
        with ThreadPoolExecutor(max_workers=1) as pool:
            responses = pool.submit(github_responses, files, retrieval, cache)
            rows = ghs_rows(files, retrieval, cache)
            responses = responses.result()
        if len(responses) != len(rows):
            raise Unavailable(f"Retrieval {retrieval} has {len(rows)} rows of GHS and "
                              f"{len(responses)} responses of GitHub")
        return list(zip(rows, responses))
    return files.memo(("retrieval pairs", cache, retrieval), read)


def keyed(files: Files, retrieval: str, cache: str = CACHE) -> dict:
    """The views of a retrieval by repository, as the comparison script keys them."""
    script = comparison()
    return files.memo(("retrieval keyed", cache, retrieval), lambda: script.by_repository(
        [script.view(row, response) for row, response in _pairs(files, retrieval, cache)]))


def _moment(text: str) -> datetime:
    """The moment of a timestamp of the index, such as `2026-10-05T10:14:30Z`."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _end_date(files: Files, retrieval: str, cache: str) -> date:
    """The end date that the index records for a retrieval."""
    return date.fromisoformat(index_row(files, retrieval, cache)["end_date"])


@checkout
def minutes(files: Files, retrieval: str, step: str = "download", cache: str = CACHE) -> float:
    """The minutes that the index records for a step of a retrieval: `download` from its
    `started_at` to its `finished_at`, `github` from its `github_started_at` to its
    `github_finished_at`."""
    prefix = {"download": "", "github": "github_"}[step]
    row = index_row(files, retrieval, cache)
    return (_moment(row[f"{prefix}finished_at"])
            - _moment(row[f"{prefix}started_at"])).total_seconds() / 60


@checkout
def end_date_days(files: Files, earlier: str, later: str, per: int = 1,
                  cache: str = CACHE) -> int | float:
    """The days from the end date of one retrieval to the end date of a later one, divided by
    `per`, such as 30 for the number of 30-day spans."""
    days = (_end_date(files, later, cache) - _end_date(files, earlier, cache)).days
    return days if per == 1 else days / per


@checkout
def languages(files: Files, retrieval: str | None = None, cache: str = CACHE) -> int:
    """The languages with a crawl date in the index row of a retrieval, by default of the latest
    retrieval that records crawl dates, GHS's languages then."""
    if retrieval is not None:
        row = index_row(files, retrieval, cache)
    else:
        recorded = [row for row in _index(files, cache) if row["crawl_dates"]]
        if not recorded:
            raise Unavailable(f"{cache}/index.csv records no crawl dates")
        row = max(recorded, key=lambda row: row["retrieval_id"])
    if not row["crawl_dates"]:
        raise Unavailable(f"{cache}/index.csv records no crawl dates of the retrieval {retrieval}")
    return len(json.loads(row["crawl_dates"]))


@checkout
def crawl_cost(files: Files, bound: bool, unit: str, retrieval: str | None = None) -> int | float:
    """The `requests` for crawl dates, or the `hours` that they take when every request times
    out, before the GHS download falls back when GHS's search fails for every language."""
    # An attempt requests the crawl date of each of GHS's languages of a retrieval, by default of
    # the latest, or with `bound` ends after `CRAWL_FAILURES_IN_A_ROW` failed requests. The
    # download makes `probe_attempts` attempts in each round, waits `probe_wait_seconds` between
    # two attempts of a round, and starts a round first and again after each wait of
    # `probe_round_wait_minutes`. A request that times out takes `READ_TIMEOUT` seconds.
    if unit not in ("requests", "hours"):
        raise ValueError(f"unit is requests or hours, not {unit!r}")
    retries = yaml.safe_load(files.text(DOWNLOAD_CONFIG))["retries"]
    rounds = 1 + len(retries["probe_round_wait_minutes"])
    per_attempt = (files.constant(DOWNLOAD_SCRIPT, "CRAWL_FAILURES_IN_A_ROW") if bound
                   else languages(files, retrieval))
    requests = per_attempt * retries["probe_attempts"] * rounds
    if unit == "requests":
        return requests
    waits = (60 * sum(retries["probe_round_wait_minutes"])
             + rounds * (retries["probe_attempts"] - 1) * retries["probe_wait_seconds"])
    return (requests * files.constant(DOWNLOAD_SCRIPT, "READ_TIMEOUT") + waits) / 3600


@checkout
def rows_per_file(files: Files, retrieval: str, cache: str = CACHE) -> int:
    """The rows of a GHS search result that one file of the cache holds before the cache splits
    it into volumes, at the compressed bytes per row of a retrieval."""
    volume_bytes = files.constant(CACHE_CODE, "VOLUME_BYTES")
    size = files.size(f"{cache}/{retrieval}.ghs.jsonl.xz")
    return int(volume_bytes * int(index_row(files, retrieval, cache)["item_count"]) // size)


def _group_kind(rows: list) -> str:
    """The kind of a group of GHS rows that share a creation time: the same repository name
    after the owner, names that differ only in case, other names with one HEAD, or neither."""
    names = {(row.get("name") or "").partition("/")[2] for row in rows}
    if len(names) == 1:
        return "same_name"
    if len({name.lower() for name in names}) == 1:
        return "case_only"
    if len({row.get("lastCommitSHA") or "" for row in rows}) == 1:
        return "same_head"
    return "unrelated"


@checkout
def created_at_groups(files: Files, retrieval: str, kind: str | None = None,
                      cache: str = CACHE) -> int:
    """The groups of two or more rows of a GHS search result that share a creation time, or
    with `kind` the groups of that kind: `same_name`, `case_only`, `same_head` or
    `unrelated`."""
    if kind is not None and kind not in GROUP_KINDS:
        raise Unavailable(f"kind is one of {GROUP_KINDS}, not {kind!r}")
    groups = defaultdict(list)
    for row in ghs_rows(files, retrieval, cache):
        groups[row.get("createdAt") or ""].append(row)
    return sum(1 for rows in groups.values()
               if len(rows) > 1 and (kind is None or _group_kind(rows) == kind))


@checkout
def created_at_head_pairs(files: Files, retrieval: str, cache: str = CACHE) -> int:
    """The pairs of rows of a GHS search result with the same creation time and the same
    HEAD."""
    counts = Counter((row.get("createdAt") or "", row.get("lastCommitSHA"))
                     for row in ghs_rows(files, retrieval, cache) if row.get("lastCommitSHA"))
    return sum(count * (count - 1) // 2 for count in counts.values())


@checkout
def timestamp_hour(files: Files, retrieval: str, how: str, cache: str = CACHE) -> int:
    """The smallest (`min`) or the largest (`max`) hour in the timestamp fields of the rows of
    a GHS search result."""
    if how not in ("min", "max"):
        raise Unavailable(f"how is min or max, not {how!r}")
    hours = [int(row[field][11:13]) for row in ghs_rows(files, retrieval, cache)
             for field in GHS_TIMES if row.get(field)]
    if not hours:
        raise Unavailable(f"The retrieval {retrieval} has no timestamp")
    return min(hours) if how == "min" else max(hours)


@checkout
def created_after_window(files: Files, retrieval: str, cache: str = CACHE) -> int:
    """The rows of a retrieval that GitHub found with an id and created on a day after the
    `created_max` of the retrieval's window."""
    created_max = index_row(files, retrieval, cache)["created_max"]
    if len(created_max) != 10:
        raise Unavailable(f"The retrieval {retrieval} has no date as created_max")
    return sum(1 for response in github_responses(files, retrieval, cache)
               if response.get("status") == "found"
               and (response.get("repository") or {}).get("id") is not None
               and (response["repository"].get("createdAt") or "")[:10] > created_max)


def _language_licenses(files: Files, retrieval: str, cache: str) -> Counter:
    """How many rows of a retrieval have each pair of `mainLanguage` and `license`."""
    return files.memo(("retrieval languages", cache, retrieval), lambda: Counter(
        (row.get("mainLanguage"), row.get("license")) for row in ghs_rows(files, retrieval, cache)))


def _licensed(files: Files, counts: Counter) -> set:
    """The licenses that the filter's license step keeps among the rows with these counts, by
    the filter's own code and config (`allowed_licenses` in `4_filter_ghs.py`), or every license
    when the config skips the step."""
    script_path = files.path(FILTER_SCRIPT)
    stage = script_path.parent
    files.path(CACHE_CODE)
    sys.path.insert(0, str(stage))
    try:
        spec = importlib.util.spec_from_file_location("_statistics_4_filter_ghs", script_path)
        script = importlib.util.module_from_spec(spec)
        # A dataclass of the script looks its module up by name.
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
    finally:
        sys.path.remove(str(stage))
    config = script.FilterConfig.from_yaml(files.path(FILTER_CONFIG))
    licenses = Counter()
    for (_language, license_name), number in counts.items():
        licenses[license_name] += number
    if not config.filter_licenses:
        return set(licenses)
    osi = set(script.read_spdx_list(files.path(f"1_sampling/{config.spdx_list_path}"))
              ["osi_approved"])
    reviewed = script.load_reviewed_licenses(files.path(f"1_sampling/{config.licenses_path}"))
    exclusions = [row["license"] for row in reviewed.values() if row["decision"] == "excluded"]
    items = [{"license": name} for name, number in licenses.items() for _ in range(number)]
    return script.allowed_licenses(items, osi, exclusions, config.license_min_count_percentile)


def _language_counts(files: Files, retrieval: str, licensed_only: bool, cache: str) -> Counter:
    """The rows of a retrieval with each `mainLanguage`, of every row or, with `licensed_only`,
    of the rows with a license that the filter's license step keeps."""
    counts = _language_licenses(files, retrieval, cache)
    kept = (files.memo(("retrieval licenses", cache, retrieval),
                       lambda: _licensed(files, counts)) if licensed_only else None)
    result = Counter()
    for (language, license_name), number in counts.items():
        if language and (kept is None or license_name in kept):
            result[language] += number
    return result


@checkout
def language_rows(files: Files, retrieval: str, language: str, cache: str = CACHE) -> int:
    """The rows of a retrieval with the `mainLanguage` `language`."""
    return _language_counts(files, retrieval, False, cache)[language]


@checkout
def language_loss(files: Files, earlier: str, later: str, language: str,
                  cache: str = CACHE) -> float:
    """The share, in percent, of the rows of a language in one retrieval that a later retrieval
    no longer has."""
    before = _language_counts(files, earlier, False, cache)[language]
    if before == 0:
        raise Unavailable(f"The retrieval {earlier} has no row of {language}")
    return 100 * (before - _language_counts(files, later, False, cache)[language]) / before


@checkout
def share_kept(files: Files, earlier: str, later: str, language: str, days: int,
               cache: str = CACHE) -> float:
    """The share, in percent, of its rows that a language keeps over `days` days if it goes on
    losing rows at the rate between two retrievals, which their end dates measure in days."""
    before = _language_counts(files, earlier, False, cache)[language]
    span = (_end_date(files, later, cache) - _end_date(files, earlier, cache)).days
    if before == 0 or span <= 0:
        raise Unavailable(f"The retrievals {earlier} and {later} give no rate for {language}")
    after = _language_counts(files, later, False, cache)[language]
    return 100 * math.pow(after / before, days / span)


@checkout
def language_lead(files: Files, retrievals: list, rank: int = 10, licensed_only: bool = False,
                  cache: str = CACHE) -> int:
    """The smallest lead, over the retrievals, of the language at `rank` among the most common
    values of `mainLanguage` over the next language, with `licensed_only` over the rows with a
    license that the filter's license step keeps."""
    # A tie in count goes to the name that sorts first, as the filter's step `languages` ranks.
    leads = []
    for retrieval in retrievals:
        counts = _language_counts(files, retrieval, licensed_only, cache)
        ranked = sorted(counts, key=lambda name: (-counts[name], name))
        if len(ranked) <= rank:
            raise Unavailable(f"The retrieval {retrieval} has no {rank + 1} languages")
        leads.append(counts[ranked[rank - 1]] - counts[ranked[rank]])
    return min(leads)
