"""Take a retrieval of the GHS dataset: the GHS search result, the JSON file in which SEART GitHub
Search returns the repositories that match the configured query, written into the cache of
retrievals and named for the rest of the sampling stage.

The query is the config's `query` block, in GHS's own parameter names, plus the sampling window:
repositories created at least `created_before_months` before the window's end date, with a last
commit in the `committed_within_months` before it. The end date is a date, or `crawl_date`, the
oldest crawl date of GHS's languages. A language's crawl date is the latest `lastCommit` date
that GHS has in that language. GHS has no update cycle to align with. It crawls GitHub one
language at a time, each from its own checkpoint, for repositories pushed since then, and
updates a repository only when it crawls a push to it. GHS has recorded every push before the
oldest crawl date, and a window that ends on that date covers only data that GHS has.

A crawl date more than `max_crawl_lag_days` old means that the crawler stopped. Only the
languages that the filter keeps need a crawl date within that limit, because the filter removes
every row of another language. The languages that the previous sampling frame kept are held to
the limit before the download of the GHS search result. Any other language with a crawl date
beyond the limit, or without a crawl date, is left out of the end date, and the filter leaves it
out as well. The rows of the GHS search result show whether the filter would otherwise keep such
a language, because the filter keeps the most common languages of each retrieval. Without a list
of kept languages, as on the first Start, such a language fails the retrieval, and with a list, a
warning names it.

A run:
  1. Reads the languages that the previous sampling frame kept, requests GHS's version, its
     languages and the crawl date of each language, resolves the sampling window, and counts the
     repositories that the query matches. With a date as the end date, no crawl date bounds the
     window, and the run requests neither the languages nor their crawl dates. When GHS returns
     no response to these requests, the attempt ends early rather than after the long stream of
     the GHS search result. A short request that fails `retries.probe_attempts` times is made
     again after each of `retries.probe_round_wait_minutes`, because an outage of GHS or of the
     network can last hours. Another attempt at the crawl dates requests only the languages
     without a crawl date, and it requests first the languages that earlier attempts requested
     least. An attempt ends after CRAWL_FAILURES_IN_A_ROW failed requests in a row, and a
     response with HTTP 200 resets the count. A search that finds no repository of a language
     counts as a failed request when the language is held to the limit. Without a list of kept
     languages, such a language gets no crawl date after the attempts of a round, and the rows
     of the GHS search result decide whether the filter keeps it.
  2. Removes the partial GHS search results that stopped downloads left and downloads the GHS
     search result. It retries a failed stream, a body cut short, a GHS search result with fewer
     items than the count, and HTTP 408, 429 and 5xx, up to `retries.attempts` times.
  3. Checks that GHS applied every parameter that it was sent, as its `parameters` block echoes
     them. Counts the repositories again, with the retries of a short request, and checks that
     the item count lies between the counts taken before and after the stream. Checks whether
     the filter would keep a language that the end date left out, counted over the rows of the
     GHS search result with a license that the filter keeps. Without a list of kept languages,
     such a language fails the retrieval, and with a list, a warning names it
     (`check_left_out`).
  4. Compares the rows, overall and per language that the previous sampling frame kept, with
     the retrieval that the stage read before, when both were taken with the same query and
     window lengths (`smaller_than_previous`).
  5. Writes the rows into `ghs_retrievals/` (ghs_cache.py), a row of its index, the pointer
     `ghs_retrieval.json` and the report `ghs_retrieval_report.json`, each synced to disk before
     its rename. The runner copies the report into the pipeline execution state.

When GHS returns no response within the attempts, the run records the failure in the index. With
`on_failure: previous_sampling_frame`, it then writes the pointer to the retrieval that the
pointer already named, and the stage continues over the previous sampling frame. The report
records that the window did not move and how many downloads in a row fell back. A GHS search
result for the same query with far fewer rows than the retrieval that the stage read before
counts as such a failure, because GHS has then lost rows, unless `--accept-smaller` takes the GHS
search result. A pointer
that cannot be read counts as none, because the run writes it again. A client error counts as
no response unless the request carries the config's query. A query that GHS rejects or does not
apply is a defect of the config and ends the run with exit 1. A file that the run cannot write
or read on this host, such as on a full disk, ends the run with exit 1 as well, with a message
that names the file and the free space, because GHS is not at fault.

Usage:
  python3 1_download_ghs.py --config config/ghs_download.yaml
  python3 1_download_ghs.py --config config/ghs_download.yaml --dry-run
  python3 1_download_ghs.py --config config/ghs_download.yaml --end-date 2026-10-01
  python3 1_download_ghs.py --config config/ghs_download.yaml --accept-smaller
  python3 1_download_ghs.py --config config/ghs_download.yaml --top-languages 5
"""

from __future__ import annotations

import argparse
import functools
import gzip
import hashlib
import importlib.util
import json
import re
import shutil
import ssl
import sys
import tempfile
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml
from agentcontext import caches, logs

import ghs_cache
from ghs_cache import CacheError, upper_first

COMPONENT = "ghs_download"

HERE = Path(__file__).resolve().parent
BASE_URL = "https://seart-ghs.si.usi.ch/api"
REPORT = ghs_cache.SAMPLING_DIR / "ghs_retrieval_report.json"
# The sampling frame that the filter (4_filter_ghs.py) wrote over the previous retrieval.
SAMPLING_FRAME = ghs_cache.SAMPLING_DIR / "ghs_filtered.json"
# The filter's config, which states how many of the most common languages the filter keeps.
FILTER_CONFIG = HERE / "config" / "ghs_filter.yaml"
# The filter's script. Its config and license step decide over the rows of the GHS search result
# which languages the filter would keep (`licensed_rows`).
FILTER_SCRIPT = HERE / "4_filter_ghs.py"

# The server's TLS certificate is verified against the trust store of `requests` and the
# intermediates of Let's Encrypt that are shipped beside this script. The server sends its own
# certificate without the intermediate that issued it (download-ghs-spec.md). Python verifies
# only a chain that the server presents, while a browser fetches the missing intermediate from
# the URL that the certificate names. Every intermediate that Let's Encrypt publishes is
# shipped, because Let's Encrypt issues from several, and the server's next certificate may come
# from another one.
CA_INTERMEDIATES = HERE / "certs" / "lets-encrypt-intermediates.pem"

CONNECT_TIMEOUT = 30
# Between two chunks of the GHS search result, and for the whole response to a probe.
READ_TIMEOUT = 300
CHUNK_SIZE = 1024 * 1024
# The longest `Retry-After` in seconds that a retry waits for, so that a server's long wait
# cannot hold the download for longer than an hour.
RETRY_AFTER_MAX_SECONDS = 3600

# Every parameter that GHS's search and download accept (`dto/SearchParameterDto.java:35-82` of
# GHS v1.17.1). GHS ignores a name that it does not define and returns the results of the rest of
# the query. A misspelled filter would widen the retrieval without an error, and
# `load_settings` rejects a name outside this set.
STRING_PARAMETERS = frozenset({"name", "language", "license", "label", "topic"})
BOOLEAN_PARAMETERS = frozenset({"nameEquals", "excludeForks", "onlyForks", "hasIssues",
                                "hasPulls", "hasWiki", "hasLicense"})
RANGE_PARAMETERS = frozenset(f"{field}{bound}" for bound in ("Min", "Max") for field in (
    "commits", "contributors", "issues", "pulls", "branches", "releases", "stars", "watchers",
    "forks", "codeLines", "commentLines", "nonBlankLines"))
DATE_PARAMETERS = frozenset({"createdMin", "createdMax", "committedMin", "committedMax"})
API_PARAMETERS = STRING_PARAMETERS | BOOLEAN_PARAMETERS | RANGE_PARAMETERS | DATE_PARAMETERS
# The two parameters that the sampling window sets.
WINDOW_PARAMETERS = ("createdMax", "committedMin")

CRAWL_DATE = "crawl_date"
ON_FAILURE = ("previous_sampling_frame", "fail")

# A GHS search result for the same query with fewer rows than this share of the rows of the
# retrieval that the stage read before counts as a failed download (`smaller_than_previous`).
# GHS has then lost rows, such as after a restore of part of its database, and the cloning step
# would delete the trees of every repository that left the sample, which the next collection
# would clone again.
# The second share applies to each language with at least LANGUAGE_ROWS_MIN rows in the previous
# retrieval, because the rows of one language vary more from one retrieval to the next than the
# total does. A language with fewer rows is not compared. When the previous sampling frame names
# the languages that it kept, only those languages are compared, because the filter removes the
# rows of every other language and a loss of those rows leaves the sampling frame unchanged.
# docs/refresh.md gives the measurements.
MIN_SHARE_OF_PREVIOUS = 0.9
MIN_LANGUAGE_SHARE_OF_PREVIOUS = 0.75
LANGUAGE_ROWS_MIN = 1000

# The number of failed requests in a row after which an attempt at the crawl dates ends and
# counts the languages that it did not request as failed. GROUNDING: a policy. One or two
# failures in a row end no attempt, because a request for one language can fail on its own and
# the next attempt requests only the languages without a date. A failure of GHS's search, such
# as a search that times out while the version page and the language list respond, fails the
# request of every language, and each further request of an attempt then waits up to
# READ_TIMEOUT. Without the bound, every attempt and round would request every language without
# a date. A response with HTTP 200, with or without a repository, shows that the search responds
# and resets the count. Each attempt requests first the languages that the attempts requested
# least, so that languages with a search that fails every time cannot end each attempt before
# the other languages are requested. download-ghs-spec.md gives the cost.
CRAWL_FAILURES_IN_A_ROW = 3

# The number of languages on a page of GHS's language list, and the number of pages that an
# attempt at the list reads before it fails. GROUNDING: a policy. The list ends on the page that
# returns `"last": true`. If GHS ignored `page` or never returned `"last": true`, the run would
# request page after page, each request successful, and neither the attempts of a short request
# nor the fallback would end the run. GHS listed every language that it crawls on the first page
# in each retrieval of October 2026 (`retrieval_2026_10_05.ghs_languages` in
# docs/statistics.json), and the bound leaves room for a list ten times as long as one page.
LANGUAGE_PAGE_SIZE = 100
LANGUAGE_PAGES_MAX = 10

# The HTTP statuses worth another attempt: a busy server, a timeout on the server's side, a
# gateway error.
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
# A body that is not a whole gzip stream: BadGzipFile when it does not start as one, EOFError
# when it was cut short, zlib.error when damaged. Any other OSError is a failure of this host
# (FileSystemFailure).
STREAM_ERRORS = (gzip.BadGzipFile, EOFError, zlib.error)


class NoResponse(Exception):
    """GHS returned no response within the attempts. Another run may succeed."""


class Refused(Exception):
    """GHS rejected the query or did not apply it. The config has to change."""


class Retryable(Exception):
    """One attempt failed in a way that another attempt may not."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class CertificateFailure(Exception):
    """The server's certificate did not verify. Another attempt would get the same certificate."""


class FileSystemFailure(Exception):
    """A file of the run could not be written or read on this host, such as on a full disk. GHS
    is not at fault. Another attempt would fail the same way, and continuing over the previous
    sampling frame would hide a fault that has to be fixed on the host."""


def free_space(path: Path) -> str:
    """A sentence that states the free space of the file system that holds `path`, measured at
    its nearest existing directory."""
    directory = Path(path)
    while not directory.is_dir() and directory != directory.parent:
        directory = directory.parent
    try:
        free = shutil.disk_usage(directory).free
    except OSError as error:
        return f"The free space in {directory} could not be measured ({error})"
    return f"{free / 1024 ** 2:,.0f} MiB are free in {directory}"


def caused_by(error: BaseException, kind: type) -> bool:
    """Whether an exception of `kind` is in the chain of `error`: its causes and contexts, and
    the exceptions that urllib3 and `requests` keep in `reason` and in their arguments.

    `requests` raises its `SSLError` for every TLS failure, for a certificate that does not
    verify as for a connection that the server or the network dropped in the handshake, and it
    keeps the error of `ssl` several links down the chain."""
    seen, chain = set(), [error]
    while chain:
        current = chain.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, kind):
            return True
        chain += [current.__cause__, current.__context__, getattr(current, "reason", None)]
        chain += [arg for arg in current.args if isinstance(arg, BaseException)]
    return False


# ---------------------------------------------------------------------------
# The config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    """The query that a run sends to GHS, and how often the run retries."""
    query: dict
    end_date: str
    created_before_months: int
    committed_within_months: int
    max_crawl_lag_days: int
    attempts: int
    wait_minutes: tuple
    probe_attempts: int
    probe_wait_seconds: float
    slack: float
    on_failure: str
    probe_round_wait_minutes: tuple = ()


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} has to be a positive integer, not {value!r}")
    return value


def check_end_date(value: str) -> str:
    """`value` if it is `crawl_date` or a YYYY-MM-DD date, else ValueError."""
    if value == CRAWL_DATE:
        return value
    try:
        datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"the end date is {CRAWL_DATE!r} or a date YYYY-MM-DD, "
                         f"not {value!r}") from None
    return str(value)


def load_settings(path: Path, end_date: str | None = None) -> Settings:
    """The config at `path`, checked, with `end_date` in place of the config's when given."""
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a mapping")
    query = raw.get("query") or {}
    if not isinstance(query, dict):
        raise ValueError("`query` has to be a mapping of GHS parameter names to values")
    unknown = sorted(set(query) - API_PARAMETERS)
    if unknown:
        raise ValueError(f"GHS has no parameter {', '.join(unknown)}. Correct or remove "
                         f"{'it' if len(unknown) == 1 else 'them'} in `query`")
    set_by_window = sorted(set(query) & set(WINDOW_PARAMETERS))
    if set_by_window:
        raise ValueError(f"{', '.join(set_by_window)} {'is' if len(set_by_window) == 1 else 'are'} "
                         f"set by the window, not by `query`")
    for name, value in query.items():
        if name in BOOLEAN_PARAMETERS and not isinstance(value, bool):
            raise ValueError(f"{name} is true or false, not {value!r}")
        if name in RANGE_PARAMETERS and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(f"{name} is an integer, not {value!r}")
        if name in DATE_PARAMETERS:
            try:
                datetime.strptime(str(value), "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{name} is a date YYYY-MM-DD, not {value!r}") from None
    window = raw.get("window") or {}
    retries = raw.get("retries") or {}
    waits = tuple(retries.get("wait_minutes", (5, 30)))
    settings = Settings(
        query=dict(query),
        end_date=check_end_date(end_date or window.get("end_date", CRAWL_DATE)),
        created_before_months=_positive_int(window.get("created_before_months"),
                                            "window.created_before_months"),
        committed_within_months=_positive_int(window.get("committed_within_months"),
                                              "window.committed_within_months"),
        max_crawl_lag_days=_positive_int(window.get("max_crawl_lag_days", 30),
                                         "window.max_crawl_lag_days"),
        attempts=_positive_int(retries.get("attempts", 3), "retries.attempts"),
        wait_minutes=waits,
        probe_attempts=_positive_int(retries.get("probe_attempts", 5), "retries.probe_attempts"),
        probe_wait_seconds=float(retries.get("probe_wait_seconds", 60)),
        slack=float(raw.get("completeness_slack", 0.001)),
        on_failure=raw.get("on_failure", "previous_sampling_frame"),
        probe_round_wait_minutes=tuple(retries.get("probe_round_wait_minutes", ())),
    )
    if len(settings.wait_minutes) < settings.attempts - 1 or any(
            not isinstance(w, (int, float)) or w < 0 for w in settings.wait_minutes):
        raise ValueError(f"retries.wait_minutes must list {settings.attempts - 1} waits of zero "
                         f"or more minutes for {settings.attempts} attempts")
    if any(isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0
           for w in settings.probe_round_wait_minutes):
        raise ValueError("retries.probe_round_wait_minutes must list waits of zero or more "
                         "minutes")
    if settings.on_failure not in ON_FAILURE:
        raise ValueError(f"on_failure is one of {', '.join(ON_FAILURE)}, "
                         f"not {settings.on_failure!r}")
    if not 0 <= settings.slack < 1:
        raise ValueError(f"completeness_slack is a share below 1, not {settings.slack!r}")
    return settings


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------

def months_before(day: date, months: int) -> date:
    """`day` minus `months` months, with the day clamped to the last day of the target
    month."""
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(day.day, last))


def lagging(settings: Settings, crawl_dates: dict, today: date) -> dict:
    """The languages with a crawl date more than `max_crawl_lag_days` old, each with its crawl
    date, when the end date is `crawl_date`. With a date as the end date, no crawl date limits
    the window, and no language is returned."""
    if settings.end_date != CRAWL_DATE:
        return {}
    return {language: day for language, day in crawl_dates.items()
            if day and (today - date.fromisoformat(day)).days > settings.max_crawl_lag_days}


def stalled_outside(settings: Settings, crawl_dates: dict, today: date, kept) -> dict:
    """The languages outside `kept`, the languages that the previous sampling frame kept, with a
    crawl date more than `max_crawl_lag_days` old, each with its crawl date. Without `kept`, no
    language is returned, because `retrieve` then leaves every language with such a crawl date,
    or without one, out of the end date and checks it against the GHS search result
    (`check_left_out`).

    A crawler that stopped for a language outside `kept` leaves the sampling frame complete,
    because the filter removes every row of such a language. If the language were held to the
    limit, it would make every later download fall back to the previous sampling frame."""
    if not kept:
        return {}
    return {language: day for language, day in lagging(settings, crawl_dates, today).items()
            if language not in kept}


def resolve_window(settings: Settings, crawl_dates: dict, today: date, kept=(),
                   left_out=()) -> dict:
    """The sampling window: its end date, where the end date came from, and the two dates that
    it sets.

    With the end date `crawl_date`, the end date is the oldest crawl date of the languages,
    leaving out the languages that `stalled_outside` returns for `kept` and the languages in
    `left_out`. `left_out` holds the languages that the download decides from the rows of the GHS
    search result when no list of kept languages is on disk (`retrieve`). Raises `NoResponse`
    when that date is more than `max_crawl_lag_days` old, which means that GHS's crawler stopped
    rather than that it lags.
    """
    if settings.end_date == CRAWL_DATE:
        ignored = {**stalled_outside(settings, crawl_dates, today, kept),
                   **{language: crawl_dates.get(language) for language in left_out}}
        known = {language: day for language, day in crawl_dates.items()
                 if day and language not in ignored}
        dated = {language: day for language, day in crawl_dates.items() if day}
        if not known and kept and ignored:
            raise NoResponse(f"GHS returned no crawl date for any language that the previous "
                             f"sampling frame kept, and every other crawl date is more than "
                             f"{settings.max_crawl_lag_days} days old")
        if not known and dated and ignored:
            newest = max(dated, key=lambda name: (dated[name], name))
            raise NoResponse(f"The newest crawl date of GHS's languages, {dated[newest]} for "
                             f"{newest}, is {(today - date.fromisoformat(dated[newest])).days} "
                             f"days old, more than the {settings.max_crawl_lag_days} days that "
                             f"the config allows")
        if not known:
            raise NoResponse("GHS returned no crawl date for any language")
        language = min(known, key=lambda name: (known[name], name))
        end = date.fromisoformat(known[language])
        lag = (today - end).days
        if lag > settings.max_crawl_lag_days:
            # A language outside `kept` with such a lag is in `ignored`. This language is in
            # `kept`.
            raise NoResponse((f"The oldest crawl date of GHS's languages, {end} for {language}, "
                              if not kept else
                              f"The crawl date of {language} (kept in the previous sampling "
                              f"frame), {end}, ")
                             + f"is {lag} days old, more than the {settings.max_crawl_lag_days} "
                               f"days that the config allows")
        source = f"{CRAWL_DATE}:{language}"
    else:
        end = date.fromisoformat(settings.end_date)
        source = "date"
    return {"end_date": end.isoformat(), "end_date_source": source,
            "created_max": months_before(end, settings.created_before_months).isoformat(),
            "committed_min": months_before(end, settings.committed_within_months).isoformat()}


def describe_window(window: dict, left_out: bool = False) -> str:
    """The log message of the sampling window that `resolve_window` returned: the end date with
    where it came from, and the two dates of the query. `left_out` says whether the end date
    left languages out."""
    kind, _, language = window["end_date_source"].partition(":")
    if kind == CRAWL_DATE:
        origin = (f"the crawl date of {language}, the oldest of GHS's languages"
                  + (" apart from those left out above" if left_out else ""))
    else:
        origin = "set by --end-date or the config"
    return (f"The sampling window ends on {window['end_date']}, {origin}. The query selects the "
            f"repositories created on or before {window['created_max']} (createdMax) with a last "
            f"commit on or after {window['committed_min']} (committedMin)")


def parameter_text(value) -> str:
    """A value of the query as it is sent to GHS: a boolean as `true` or `false`, else its
    text."""
    return ("true" if value else "false") if isinstance(value, bool) else str(value)


def query_parameters(settings: Settings, window: dict) -> dict:
    """The parameters sent to GHS, as strings: the config's query and the window's two dates."""
    params = {name: parameter_text(value) for name, value in settings.query.items()}
    params["createdMax"] = window["created_max"]
    params["committedMin"] = window["committed_min"]
    return dict(sorted(params.items()))


def _midnight_ms(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000)


def unapplied(params: dict, server: dict) -> list:
    """The parameters that the `parameters` block of the GHS search result does not echo with the
    value sent. GHS echoes a date as midnight UTC in epoch milliseconds and a boolean as a JSON
    boolean."""
    missing = []
    for name, sent in params.items():
        echoed = server.get(name)
        if name in DATE_PARAMETERS:
            expected = _midnight_ms(sent)
        elif name in BOOLEAN_PARAMETERS:
            expected = sent == "true"
        elif name in RANGE_PARAMETERS:
            expected = int(sent)
        else:
            expected = sent
        if echoed != expected:
            missing.append(f"{name}={sent} echoed as {echoed!r}")
    return missing


# ---------------------------------------------------------------------------
# Requests to GHS
# ---------------------------------------------------------------------------

def write_ca_bundle(directory: Path) -> Path:
    """A CA file of the trust store of `requests` and the shipped intermediates."""
    path = Path(directory) / "ca-bundle.pem"
    path.write_text(Path(requests.certs.where()).read_text(encoding="utf-8") + "\n"
                    + CA_INTERMEDIATES.read_text(encoding="utf-8"), encoding="utf-8")
    return path


def _retry_after(response) -> float | None:
    value = (getattr(response, "headers", None) or {}).get("Retry-After")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def check_status(response, what: str, carries_query: bool) -> None:
    """Raise for a status that is not 200. A client error other than 408 and 429 on a request
    that carries the config's query raises Refused, because another attempt would get the same
    error. Every other status raises Retryable. A request without the query, such as the version
    page or the language list, cannot be rejected for the query. Its client error counts as no
    response, and the run falls back rather than ending."""
    status = response.status_code
    if status == 200:
        return
    if carries_query and 400 <= status < 500 and status not in RETRY_STATUSES:
        raise Refused(f"GHS rejected the request for {what} with HTTP {status}")
    raise Retryable(f"GHS returned HTTP {status} for {what}", _retry_after(response))


def request(path: str, params: dict, ca_bundle: Path, what: str, stream: bool = False,
            carries_query: bool = False):
    """One GET to GHS, its failures sorted into the three kinds that a caller handles
    differently. A TLS failure is a `CertificateFailure` only when `ssl` failed to verify the
    certificate. A connection that the server or the network dropped in the handshake gets
    another attempt like any other failed connection."""
    try:
        response = requests.get(BASE_URL + path, params=params, stream=stream,
                                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), verify=str(ca_bundle))
    except requests.RequestException as error:
        if caused_by(error, ssl.SSLCertVerificationError):
            raise CertificateFailure(str(error)) from error
        raise Retryable(f"the request for {what} failed ({error})") from error
    check_status(response, what, carries_query)
    return response


def with_retries(action, attempts: int, waits_seconds: list, what: str, record: list):
    """`action()`, attempted up to `attempts` times with the given waits in seconds between
    them. Each attempt is appended to `record` with its start and error. Raises `NoResponse`
    with the last error when every attempt failed."""
    last = None
    for attempt in range(1, attempts + 1):
        started = ghs_cache.iso(ghs_cache.utc_now())
        try:
            result = action()
        except Retryable as error:
            last = error
            record.append({"what": what, "started_at": started, "error": str(error)})
            if attempt == attempts:
                break
            wait = waits_seconds[attempt - 1]
            if error.retry_after is not None:
                wait = max(wait, min(error.retry_after, RETRY_AFTER_MAX_SECONDS))
            logs.warning(COMPONENT, f"Attempt {attempt} of {attempts} failed. "
                                    f"{upper_first(error)}. Trying again in {wait:.0f}s.")
            time.sleep(wait)
            continue
        record.append({"what": what, "started_at": started, "error": ""})
        return result
    raise NoResponse(f"all {attempts} attempts at {what} failed. In the last one, {last}")


def ghs_version(ca_bundle: Path) -> str:
    """GHS's version and build time, from the text of its `/version` page."""
    text = re.sub(r"<[^>]+>", " ", request("/version", {}, ca_bundle, "the version").text)
    version = re.search(r"version:\s*([\w.\-]+)", text)
    built = re.search(r"built on:\s*(\S+)", text)
    if not version:
        return "unknown"
    return version.group(1) + (f" built {built.group(1)}" if built else "")


def _json(response, what: str):
    try:
        return response.json()
    except ValueError as error:
        raise Retryable(f"the response for {what} is not JSON ({error})") from error


def is_day(text: str) -> bool:
    """Whether `text` is a date in the form YYYY-MM-DD."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def languages(ca_bundle: Path) -> list:
    """Every language that GHS crawls, read page by page. Raises `Retryable` for a page that is
    not an object with an `items` list of names, and for a list that does not end within
    LANGUAGE_PAGES_MAX pages, so that the attempts of a short request and the fallback apply."""
    names = []
    for page in range(LANGUAGE_PAGES_MAX):
        body = _json(request("/l", {"page": page, "size": LANGUAGE_PAGE_SIZE}, ca_bundle,
                             "the language list"), "the language list")
        items = body.get("items") if isinstance(body, dict) else None
        if not isinstance(items, list) or not all(isinstance(name, str) for name in items):
            raise Retryable(f"page {page} of the language list is not an object with an "
                            f"`items` list of names")
        names += items
        if body.get("last", True):
            return names
    raise Retryable(f"the language list did not end within {LANGUAGE_PAGES_MAX} pages of "
                    f"{LANGUAGE_PAGE_SIZE} languages")


@dataclass
class CrawlSearches:
    """What the attempts at the crawl dates have found so far. Each attempt reads and adds to
    it.

    - `found`: the crawl date of each language that has one, or None for a language that gets
      none
    - `requested`: how often the attempts requested each language
    - `empty`: the languages that need a crawl date and for which the latest search returned
      HTTP 200 without a repository
    """
    found: dict = field(default_factory=dict)
    requested: Counter = field(default_factory=Counter)
    empty: set = field(default_factory=set)


def crawl_dates(language_names: list, ca_bundle: Path, today: date,
                searches: CrawlSearches | None = None, kept=frozenset()) -> dict:
    """Per language, the latest `lastCommit` date that GHS has, not later than today.

    GHS's search cannot sort by `pushedAt` (`GitRepoController.java:95-109`), and `lastCommit`
    is the field that GHS compares with the window's lower bound. `committedMax` excludes a
    commit dated in the future, which GHS records for some repositories. Only the day is read,
    because the hour of every GHS timestamp is on a 12-hour clock without AM/PM
    (`JacksonConfig.java:28`).

    `searches` keeps what the earlier attempts found. An attempt requests each language that
    `searches.found` lacks and adds every date that GHS returns. At the end, it raises
    `Retryable` and names the languages without a crawl date, so that the next attempt requests
    only these languages. If each attempt requested every language again, an attempt would need
    a response to every one of its requests, and failures of single requests could use up the
    attempts. After CRAWL_FAILURES_IN_A_ROW failed requests in a row, the attempt requests no
    further language and counts the languages that it did not request as failed. That many
    failures show that GHS's search fails for every language, and an attempt over all languages
    would wait for each request to fail. A response with HTTP 200, with or without a repository,
    shows that the search responds and resets the count. The languages that the attempts
    requested least come first, in the order of the list, so that languages with a search that
    fails every time cannot end each attempt before the other languages are requested, also
    after each of the other languages failed once.

    A search that finds no repository of a language counts as a failed request when the
    language is in `kept`, the languages that the previous sampling frame kept, or when there
    is no list of them, because such a language needs a crawl date. The language stays in
    `searches.empty` until a search finds a repository. A language outside `kept` then gets no
    crawl date, with a warning, because the filter removes its rows. A search that returns a
    repository with a `lastCommit` that does not start with a date counts as a failed request.
    """
    searches = CrawlSearches() if searches is None else searches
    found = searches.found
    pending = [language for language in language_names if language not in found]
    failed, last, wait, in_a_row, skipped = set(), None, None, 0, 0
    for language in sorted(pending, key=lambda name: searches.requested[name]):
        if in_a_row >= CRAWL_FAILURES_IN_A_ROW:
            failed.add(language)
            skipped += 1
            continue
        what = f"the crawl date of {language}"
        searches.requested[language] += 1
        try:
            body = _json(request("/r/search", {"language": language,
                                               "committedMax": today.isoformat(),
                                               "sort": "lastCommit,desc", "size": 1},
                                 ca_bundle, what), what)
        except Retryable as error:
            failed.add(language)
            last, in_a_row = error, in_a_row + 1
            if error.retry_after is not None:
                wait = max(wait or 0, error.retry_after)
            continue
        in_a_row = 0
        items = body.get("items") if isinstance(body, dict) else None
        stamp = items[0].get("lastCommit") \
            if isinstance(items, list) and items and isinstance(items[0], dict) else None
        day = stamp[:10] if isinstance(stamp, str) else ""
        if stamp and not is_day(day):
            # The search counts as a failed request, because the window and the filter read the
            # crawl date as a date and could not read such a value.
            failed.add(language)
            last = Retryable(f"the `lastCommit` of the response for {what} does not start with a "
                             f"date ({stamp!r})")
            continue
        if day:
            found[language] = day
            searches.empty.discard(language)
        elif not kept or language in kept:
            searches.empty.add(language)
            failed.add(language)
            last = Retryable(f"GHS found no repository for {what}")
        else:
            found[language] = None
            logs.warning(COMPONENT, f"GHS found no repository for {what}. Left {language} out of "
                                    f"the end date of the sampling window, because the previous "
                                    f"sampling frame did not keep it.")
    if failed:
        unrequested = (f". Ended the attempt after {CRAWL_FAILURES_IN_A_ROW} failed requests in a "
                       f"row, without requesting {skipped} of the languages" if skipped else "")
        names = [language for language in language_names if language in failed]
        raise Retryable(f"GHS returned no crawl date for {len(failed)} of "
                        f"{len(language_names)} languages ({', '.join(names)}). In the last "
                        f"failed request, {last}{unrequested}", wait)
    return {language: found[language] for language in language_names}


def count(params: dict, ca_bundle: Path) -> int:
    """How many repositories GHS's search finds for `params`. Raises `Retryable` for a response
    that is not an object with a `totalItems` number."""
    body = _json(request("/r/search", {**params, "size": 1}, ca_bundle, "the count",
                         carries_query=True), "the count")
    total = body.get("totalItems") if isinstance(body, dict) else None
    if not isinstance(total, int):
        raise Retryable("the response for the count is not an object with a `totalItems` "
                        "number")
    return total


def download_once(params: dict, ca_bundle: Path, temp_path: Path) -> tuple:
    """Stream the GHS search result into `temp_path` once, and return it parsed, the number of
    bytes received and the SHA-256 of the decompressed body. The compressed file is removed
    however the attempt ends."""
    received, written = 0, False
    try:
        logs.info(COMPONENT, "Downloading the GHS search result.")
        response = request("/r/download/json", params, ca_bundle, "the GHS search result",
                           stream=True, carries_query=True)
        try:
            with open(temp_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    f.write(chunk)
                    received += len(chunk)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        written = True
        logs.info(COMPONENT, f"Downloaded the GHS search result "
                             f"({received / 1024 ** 2:,.0f} MiB).")
        logs.info(COMPONENT, "Decompressing the GHS search result.")
        with gzip.open(temp_path, "rb") as f:
            body = f.read()
    except requests.RequestException as error:
        raise Retryable(f"the download of the GHS search result failed after {received:,} "
                        f"bytes ({error})") from error
    except STREAM_ERRORS as error:
        raise Retryable(f"the {received:,} bytes of the GHS search result are not a complete "
                        f"gzip stream ({error})") from error
    except OSError as error:
        # `requests` raises its own exceptions for a failure of the network. Any other OSError
        # comes from this host's file system, such as from a full disk, a quota or a cache that
        # is read-only or that the run may not write.
        raise FileSystemFailure(f"could not {'read' if written else 'write'} {temp_path} "
                                f"({error}). {free_space(temp_path)}") from error
    finally:
        temp_path.unlink(missing_ok=True)
    sha = hashlib.sha256(body).hexdigest()
    try:
        data = json.loads(body)
    except ValueError as error:
        raise Retryable(f"the GHS search result is not JSON ({error})") from error
    del body
    if not isinstance(data, dict) or not isinstance(data.get("items"), list) \
            or not isinstance(data.get("parameters"), dict):
        raise Retryable("the GHS search result is not an object with an `items` list and a "
                        "`parameters` object")
    return data, received, sha


# ---------------------------------------------------------------------------
# A run
# ---------------------------------------------------------------------------

@dataclass
class Paths:
    """Where a run reads and writes. The defaults are in the sampling stage's data directory.
    The sampling frame is the filter's output, and the run reads from it the languages that the
    filter kept. The filter's config states how many languages the filter keeps."""
    cache_dir: Path = ghs_cache.CACHE_DIR
    pointer: Path = ghs_cache.POINTER
    report: Path = REPORT
    sampling_frame: Path = SAMPLING_FRAME
    filter_config: Path = FILTER_CONFIG


def write_report(path: Path, report: dict) -> None:
    """Write the JSON object that the runner copies into the pipeline execution state, in one
    step (`caches.replace_file`), so that a power loss cannot leave an empty report."""
    caches.replace_file(Path(path), json.dumps(report, indent=1, sort_keys=True) + "\n")


def _window_of(row: dict | None) -> dict | None:
    if not row:
        return None
    return {key: row.get(key, "") for key in ("end_date", "end_date_source", "created_max",
                                              "committed_min")}


def retrieval_before(paths: Paths) -> dict | None:
    """The retrieval that the stage read before this run, which a failed run falls back to and
    a new retrieval is compared with. It is the retrieval that the pointer names, or the newest
    usable retrieval when there is no pointer, provided that the cache has its rows. When the
    cache lacks the rows of that retrieval, the next older usable retrieval with its rows takes
    its place, with a warning, because without a previous retrieval a run could neither compare
    the GHS search result nor fall back.

    A pointer that cannot be read, such as an empty file after a power loss, or that names a
    retrieval that the index does not have, counts as no pointer, with a warning, because the
    run writes the pointer again."""
    rows = ghs_cache.read_index(paths.cache_dir)
    try:
        pointer = ghs_cache.read_pointer(paths.pointer)
    except CacheError as error:
        logs.warning(COMPONENT, f"{upper_first(error)}. Ignored the pointer until it is written "
                                f"again.")
        pointer = None
    candidates = []
    if pointer is not None:
        pointed = next((row for row in rows if row["retrieval_id"] == pointer["retrieval_id"]),
                       None)
        if pointed is None:
            logs.warning(COMPONENT, f"Retrieval {pointer['retrieval_id']} in {paths.pointer} is "
                                    f"not in the index of {paths.cache_dir}. Ignored the pointer "
                                    f"until it is written again.")
        else:
            candidates.append(pointed)
            rows = [row for row in rows if row["retrieval_id"] < pointed["retrieval_id"]]
    candidates += [row for row in reversed(rows) if ghs_cache.usable(row)]
    without = []
    for row in candidates:
        if ghs_cache.has_rows(paths.cache_dir, row["retrieval_id"], "ghs"):
            if without:
                logs.warning(COMPONENT, f"The cache of retrievals lacks the rows of "
                                        f"{', '.join(without)}. Took retrieval "
                                        f"{row['retrieval_id']} as the previous retrieval.")
            return row
        without.append(row["retrieval_id"])
    return None


def same_query(params: dict, settings: Settings, previous: dict) -> bool:
    """Whether the GHS search result was taken with the query of the retrieval `previous`: the
    same parameters sent, apart from the window's two dates, and the same window lengths, the
    months from the end date back to `createdMax` and to `committedMin`.

    The compared parameters are the parameters sent (`query`), not the parameters that GHS
    echoed, because a GHS that adds a parameter to its echo runs the same query, and `unapplied`
    has checked that GHS applied every parameter sent. A change of a window length changes which
    repositories the window covers, as a change of the query does. A retrieval without a
    recorded query is compared by its window lengths alone."""
    try:
        earlier = json.loads(previous.get("query") or "{}")
    except ValueError:
        earlier = {}
    if isinstance(earlier, dict) and earlier:
        def outside_window(query: dict) -> dict:
            return {name: parameter_text(value) for name, value in query.items()
                    if name not in WINDOW_PARAMETERS}
        if outside_window(earlier) != outside_window(params):
            return False
    try:
        end = date.fromisoformat(previous["end_date"])
    except (KeyError, TypeError, ValueError):
        return True
    return (months_before(end, settings.created_before_months).isoformat(),
            months_before(end, settings.committed_within_months).isoformat()) == (
        previous.get("created_max"), previous.get("committed_min"))


def smaller_than_previous(data: dict, params: dict, settings: Settings, previous: dict | None,
                          cache_dir: Path, kept=frozenset()) -> str:
    """Why the GHS search result has too few rows to replace `previous`, the retrieval that the
    stage read before, or "" when it has enough.

    A GHS search result for the same query (`same_query`) has too few rows when it has fewer than
    MIN_SHARE_OF_PREVIOUS of the previous retrieval's rows, or, in a language with at least
    LANGUAGE_ROWS_MIN rows in the previous retrieval, fewer than MIN_LANGUAGE_SHARE_OF_PREVIOUS
    of that language's rows. Only the languages in `kept`, the languages that the previous
    sampling frame kept, are compared, or every language when there is no list of them, because
    the filter removes the rows of every other language. The counts before and after the
    download read the same database as the GHS search result and cannot show that GHS lost rows,
    such as after a restore of part of its database."""
    if previous is None or not same_query(params, settings, previous):
        return ""
    rid, now = previous["retrieval_id"], len(data["items"])
    before = int(previous["item_count"] or 0)
    if now < MIN_SHARE_OF_PREVIOUS * before:
        return (f"the GHS search result has {now:,} repositories, fewer than "
                f"{MIN_SHARE_OF_PREVIOUS:.0%} of the {before:,} of the previous retrieval, {rid}, "
                f"for the same query")
    try:
        earlier = Counter(row.get("mainLanguage") for row in ghs_cache.read_rows(
            cache_dir, rid, "ghs", previous.get("ghs_file_sha256", "")))
    except CacheError as error:
        logs.warning(COMPONENT, f"The rows of retrieval {rid} cannot be read. "
                                f"{upper_first(error)}. Skipped the comparison of the GHS search "
                                f"result with them per language.")
        return ""
    current = Counter(item.get("mainLanguage") for item in data["items"])
    short = [f"{language} {current[language]:,} of {count:,}"
             for language, count in sorted(earlier.items(), key=lambda item: str(item[0]))
             if count >= LANGUAGE_ROWS_MIN and (not kept or language in kept)
             and current[language] < MIN_LANGUAGE_SHARE_OF_PREVIOUS * count]
    if short:
        return (f"the GHS search result has fewer than {MIN_LANGUAGE_SHARE_OF_PREVIOUS:.0%} of the "
                f"repositories of the previous retrieval, {rid}, for the same query, in these "
                f"languages with at least {LANGUAGE_ROWS_MIN:,} there"
                + (", each kept in the previous sampling frame" if kept else "")
                + f" ({', '.join(short)})")
    return ""


def kept_languages(path: Path) -> frozenset:
    """The primary languages that the sampling frame at `path` kept, as the values of the
    filter's step `languages` in its `_metadata` record them. The set is empty, with a warning
    in the last case, when the sampling frame is absent, records no list (the sampling frame of
    March 2026 records none) or cannot be read."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return frozenset()
    except (OSError, ValueError) as error:
        logs.warning(COMPONENT, f"The sampling frame {path} cannot be read ({error}). Every "
                                f"language with a crawl date more days old than the config "
                                f"allows, or without one, is left out of the end date of the "
                                f"sampling window, and the retrieval fails if the filter would "
                                f"keep one of these languages.")
        return frozenset()
    metadata = data.get("_metadata") if isinstance(data, dict) else None
    stages = metadata.get("stages") if isinstance(metadata, dict) else None
    for stage in stages if isinstance(stages, list) else ():
        if isinstance(stage, dict) and stage.get("key") == "languages":
            values = stage.get("values")
            languages = values.get("languages") if isinstance(values, dict) else None
            if isinstance(languages, list):
                return frozenset(name for name in languages if isinstance(name, str))
    return frozenset()


def filter_top_languages(path: Path, top: int | None = None) -> int | None:
    """How many of the most common primary languages the filter keeps. This is `top`, the
    number that the filter's step argument `--top-languages` sets and the sampling stage passes
    to the download (`run_sampling.share_top_languages`), or else the config's
    `top_n_languages`. The result is None when the filter keeps every language
    (`filter_languages: false`), and None with a warning when the config at `path` cannot be
    read or, without `top`, names no number. None holds every language to the crawl lag, which
    leaves out no language that the filter could keep."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise ValueError("it is not a mapping")
        if raw.get("filter_languages", True) is False:
            return None
        if top is not None:
            return top
        return _positive_int(raw.get("top_n_languages"), "top_n_languages")
    except (OSError, TypeError, ValueError, getattr(yaml, "YAMLError", ValueError)) as error:
        logs.warning(COMPONENT, f"The filter's config {path} does not state how many languages "
                                f"the filter keeps ({error}). Every language counts as one that "
                                f"the filter would keep.")
        return None


@functools.cache
def filter_script():
    """The filter's script, 4_filter_ghs.py, loaded by its path, because a module name cannot
    start with a numeral. The download reads the filter's config and applies its license step
    with the filter's own code, so that both decide the same way."""
    spec = importlib.util.spec_from_file_location("_download_4_filter_ghs", FILTER_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def licensed_rows(items: list, path: Path) -> list:
    """The rows of the GHS search result with a license that the filter's license step keeps, as
    the filter's config at `path` sets the step (`allowed_licenses` in 4_filter_ghs.py), or every
    row when the config skips the step. The license is read from GHS's `license`. The license
    step removes more rows than any other step before the step `languages`, and of these steps it
    alone needs no response from GitHub. When the config, the SPDX list or the review of
    licenses cannot be read, the filter cannot run either, and every row is returned, with a
    warning."""
    try:
        script = filter_script()
        config = script.FilterConfig.from_yaml(path)
        if not config.filter_licenses:
            return list(items)
        osi = set(script.read_spdx_list(HERE / config.spdx_list_path)["osi_approved"])
        reviewed = script.load_reviewed_licenses(HERE / config.licenses_path)
    except (OSError, TypeError, ValueError, getattr(yaml, "YAMLError", ValueError)) as error:
        logs.warning(COMPONENT, f"The filter's license step cannot be applied ({error}). Counted "
                                f"the languages that the filter would keep over every row of the "
                                f"GHS search result.")
        return list(items)
    exclusions = [row["license"] for row in reviewed.values() if row["decision"] == "excluded"]
    allowed = script.allowed_licenses(items, osi, exclusions, config.license_min_count_percentile)
    return [item for item in items if item.get("license") in allowed]


def would_keep(items: list, top: int) -> frozenset:
    """The languages that the filter's step `languages` would keep over the rows of the GHS search
    result, `items`: the `top` most common values of `mainLanguage`, the name deciding a tie in
    count, as the step decides (`step_languages` in 4_filter_ghs.py). The caller passes the rows
    that the license step keeps (`licensed_rows`). The filter's step counts GitHub's primary
    language after the merge with GitHub's responses and after the other filter steps before it.
    This function returns the step's choice before those changes."""
    counts = Counter(item.get("mainLanguage") for item in items if item.get("mainLanguage"))
    return frozenset(sorted(counts, key=lambda name: (-counts[name], name))[:top])


def without_crawl_date(undecided: dict, settings: Settings, today: date) -> str:
    """What each language in `undecided` lacks, joined into one clause: a crawl date at most
    `max_crawl_lag_days` old, with the crawl date and its age, or any crawl date."""
    parts = [(f"the crawl date of {name}, {day}, is {(today - date.fromisoformat(day)).days} days "
              f"old, more than the {settings.max_crawl_lag_days} days that the config allows")
             if day else f"GHS returned no crawl date for {name}"
             for name, day in sorted(undecided.items())]
    return ", ".join(parts[:-1]) + (", and " if len(parts) > 1 else "") + parts[-1]


def check_left_out(items: list, undecided: dict, top: int | None, settings: Settings,
                   today: date, kept: frozenset = frozenset()) -> None:
    """Check whether the filter, which keeps the `top` most common languages, or every language
    when `top` is None, would keep a language of `undecided` over the rows of the GHS search
    result, `items`, with a license that it keeps (`licensed_rows`).

    `undecided` holds the languages that the end date left out, each with its crawl date or
    None. `kept` lists the languages that the previous sampling frame kept. Without `kept`,
    `undecided` can hold any language. With `kept`, it holds only languages outside that list,
    because a language of the list needs a recent crawl date before the download of the GHS
    search result. The filter keeps
    the most common languages of each retrieval, and a language outside the list can be among
    them. GHS lacks the latest pushes of such a language, and the filter's step
    `languages` leaves it out (`step_languages` in 4_filter_ghs.py).

    Without the list, as on the first Start, such a language raises `NoResponse`, because a
    crawler that stopped for the language, or a failed search for its crawl date, would drop a
    language that the filter keeps from the first sampling frame. With the list, a warning names
    the language and the retrieval is taken. The previous sampling frame lacks the language as
    well, and a fallback would only keep the window from moving every month while the crawler
    stays stopped. When the filter would keep none of the languages, they are only logged."""
    if top is None:
        held = dict(undecided)
        reason = (f"{without_crawl_date(held, settings, today)}. Every language counts as one "
                  f"that the filter would keep (no number of languages in the filter's config)")
    else:
        among = (f"among the {top:,} languages that the filter would keep (the most common in "
                 f"the GHS search result under a license that it keeps)")
        held = {name: day for name, day in undecided.items() if name in would_keep(items, top)}
        if not held:
            names = ", ".join(sorted(undecided))
            subject = f"{names} is not" if len(undecided) == 1 else "None of them is"
            logs.info(COMPONENT, f"Left {names} out of the end date of the sampling window. "
                                 f"{subject} {among}.")
            return
        reason = (f"{without_crawl_date(held, settings, today)}. {', '.join(sorted(held))} "
                  f"{'is' if len(held) == 1 else 'are'} {among}")
    if not kept:
        raise NoResponse(reason)
    logs.warning(COMPONENT, f"{upper_first(reason)}. The filter will leave "
                            f"{', '.join(sorted(held))} out of the sampling frame, as it did from "
                            f"the previous one. Took the retrieval.")


def fallbacks_in_a_row(cache_dir: Path) -> int:
    """How many downloads in a row fell back to the previous sampling frame: the `failed` rows
    of the index that name a fallback, since the newest usable retrieval."""
    count = 0
    for row in reversed(ghs_cache.read_index(cache_dir)):
        if ghs_cache.usable(row):
            break
        if row["outcome"] == ghs_cache.FAILED and row["fallback"]:
            count += 1
    return count


def remove_interrupted_downloads(cache_dir: Path) -> None:
    """Remove the partial GHS search results that stopped downloads left, `<id>.json.gz.part`. A
    download removes its file however an attempt ends, but a Stop or a kill can end the process
    before the download removes the file. No later download reads such a file, because each
    download takes a retrieval of its own."""
    removed = []
    for path in sorted(Path(cache_dir).glob("*.json.gz.part")):
        try:
            path.unlink()
        except OSError as error:
            logs.warning(COMPONENT, f"Could not remove {path.name}, the partial GHS search result "
                                    f"of a stopped download ({error}).")
        else:
            removed.append(path.name)
    if removed:
        logs.info(COMPONENT, f"Removed the partial GHS search results of stopped downloads "
                             f"({', '.join(removed)}).")


def fall_back(settings: Settings, paths: Paths, rid: str, record: dict, reason: str,
              previous: dict | None = None) -> int:
    """Record the failed retrieval and, when the config allows it, keep the previous
    sampling frame. That is the retrieval `previous` when the caller has looked it up, else the
    retrieval that `retrieval_before` finds.

    The report names how many downloads in a row fell back (`fallbacks_in_a_row`), this one
    included. For a GHS search result that the size check refused, it also names the rows of the
    GHS search result (`failed_item_count`), which the record's `item_count` holds. A download
    that keeps falling back keeps the sampling window where it is, and the entrypoint and the
    release state the count."""
    if settings.on_failure != "previous_sampling_frame":
        previous = None
    elif previous is None:
        previous = retrieval_before(paths)
    ghs_cache.put_index_row({**record, "outcome": ghs_cache.FAILED, "error": reason,
                             "finished_at": ghs_cache.iso(ghs_cache.utc_now()),
                             "fallback": previous["retrieval_id"] if previous else ""},
                            paths.cache_dir)
    if previous is None:
        why = ("the config sets on_failure to fail" if settings.on_failure == "fail"
               else "the cache of retrievals has no earlier retrieval")
        logs.error(COMPONENT, f"Retrieval {rid} failed. {upper_first(reason)}. Stopped without a "
                              f"fallback, because {why}.")
        return 1
    in_a_row = fallbacks_in_a_row(paths.cache_dir)
    ghs_cache.write_pointer(previous["retrieval_id"], paths.pointer)
    report = {"outcome": "fallback", "retrieval_id": previous["retrieval_id"],
              "failed_retrieval_id": rid, "window_moved": False, "reason": reason,
              "window": _window_of(previous), "fallbacks_in_a_row": in_a_row}
    if record.get("item_count") is not None:
        report["failed_item_count"] = record["item_count"]
    write_report(paths.report, report)
    logs.warning(COMPONENT, f"Retrieval {rid} failed. {upper_first(reason)}. Fell back to the "
                            f"previous retrieval, {previous['retrieval_id']}, and its sampling "
                            f"window." + (f" The last {in_a_row:,} downloads fell back."
                                          if in_a_row > 1 else ""))
    return 0


def failed_dry_run(reason: str) -> int:
    """End a dry run that a failed request or check stopped, and return 1. A dry run takes no
    retrieval, so it writes no index row, no pointer and no report, and it does not fall back to
    the previous sampling frame. The exit code shows that the check failed."""
    logs.error(COMPONENT, f"Dry run failed, nothing written. {upper_first(reason)}.")
    return 1


def retrieve(settings: Settings, paths: Paths, dry_run: bool = False,
             accept_smaller: bool = False, top_languages: int | None = None) -> int:
    """A whole run: the probes, the window, the download, the checks and the writes. With
    `accept_smaller`, a GHS search result that `smaller_than_previous` refuses is taken, with a
    warning. `top_languages`, the filter's `--top-languages`, replaces the number of languages in
    the filter's config when the run checks the languages that the end date left out. With
    `dry_run`, the run stops after the count, and a failed request or check ends it through
    `failed_dry_run`."""
    started = ghs_cache.utc_now()
    rid = ghs_cache.retrieval_id(started)
    today = started.date()
    attempts: list = []
    record = {"retrieval_id": rid, "started_at": ghs_cache.iso(started), "attempts": attempts}
    probe_waits = [settings.probe_wait_seconds] * settings.probe_attempts
    round_waits = [60 * w for w in settings.probe_round_wait_minutes]
    # Read before the probes, because the crawl dates need the languages that the filter keeps.
    kept = kept_languages(paths.sampling_frame)
    with tempfile.TemporaryDirectory() as directory:
        ca_bundle = write_ca_bundle(Path(directory))
        try:
            def probe(action, what, after_round=None):
                """A short request, with its attempts made again after each round wait.
                `after_round`, called when the attempts of a round fail, returns a result that
                ends the rounds without a further round, or None."""
                for wait in (*round_waits, None):
                    try:
                        return with_retries(action, settings.probe_attempts, probe_waits, what,
                                            attempts)
                    except NoResponse as error:
                        result = after_round() if after_round else None
                        if result is not None:
                            return result
                        if wait is None:
                            raise
                        logs.warning(COMPONENT, f"{upper_first(error)}. Requesting {what} again in "
                                                f"{wait / 60:.0f} minutes.")
                        time.sleep(wait)

            record["ghs_version"] = probe(lambda: ghs_version(ca_bundle), "the version")
            logs.info(COMPONENT, f"The version of GHS is {record['ghs_version']}.")
            # With a date as the end date, no crawl date bounds the window. The run then requests
            # neither the languages nor their crawl dates and leaves no language out of the end
            # date. Otherwise a crawl date that GHS cannot return would hold the run for the
            # attempts and rounds, or fail a retrieval with a window that does not depend on
            # that crawl date.
            dates, undecided, ignored = {}, {}, {}
            if settings.end_date == CRAWL_DATE:
                names = probe(lambda: languages(ca_bundle), "the language list")
                searches = CrawlSearches()

                def without_repository():
                    """Without a list of the languages that the previous sampling frame kept,
                    a language for which the search found no repository in the attempts of a
                    round gets no crawl date, and the rows of the GHS search result decide
                    whether the filter would keep the language (`check_left_out`). When no other
                    language lacks a crawl date, the crawl dates end the rounds, because more
                    rounds would not change what the GHS search result decides."""
                    if kept or not searches.empty:
                        return None
                    logs.warning(COMPONENT, f"GHS found no repository for the crawl date of "
                                            f"{', '.join(sorted(searches.empty))} in the attempts "
                                            f"of a round. The rows of the GHS search result "
                                            f"decide whether the filter would keep "
                                            f"{'it' if len(searches.empty) == 1 else 'them'} (no "
                                            f"list of the languages that the previous sampling "
                                            f"frame kept).")
                    searches.found.update(dict.fromkeys(searches.empty))
                    searches.empty.clear()
                    if any(name not in searches.found for name in names):
                        return None
                    return {name: searches.found[name] for name in names}

                try:
                    dates = probe(lambda: crawl_dates(names, ca_bundle, today, searches, kept),
                                  "the crawl dates", without_repository)
                except NoResponse as error:
                    # After the last round, a language outside the list of the languages that the
                    # previous sampling frame kept, or any language without such a list, needs no
                    # crawl date before the download of the GHS search result, and its rows
                    # decide whether the filter would keep the language.
                    missing = [name for name in names if name not in searches.found]
                    if not searches.found or any(name in kept for name in missing):
                        raise
                    dates = {name: searches.found.get(name) for name in names}
                    if kept:
                        logs.warning(COMPONENT, f"GHS returned no crawl date for "
                                                f"{', '.join(missing)} after every attempt and "
                                                f"round. {upper_first(error)}. Left them out of "
                                                f"the end date of the sampling window, because "
                                                f"the previous sampling frame kept none of them.")
                record["crawl_dates"] = dates
                known = sorted((day, name) for name, day in dates.items() if day)
                logs.info(COMPONENT, "The crawl dates of GHS's languages, oldest first, are "
                                     + ", ".join(f"{name} {day}" for day, name in known) + ".")
                ignored = stalled_outside(settings, dates, today, kept)
                if ignored:
                    stalled = ", ".join(f"{name} {day}" for name, day in sorted(ignored.items()))
                    logs.warning(COMPONENT, f"The crawl dates of {stalled} are more than "
                                            f"{settings.max_crawl_lag_days} days old. Left these "
                                            f"languages out of the end date of the sampling "
                                            f"window, because the previous sampling frame kept "
                                            f"none of them.")
                # A language with a crawl date older than the limit, or without a crawl date, is
                # left out of the end date when it is outside the list of the languages that the
                # previous sampling frame kept, or when there is no such list, as on the first
                # download into an empty data root. The rows of the GHS search result show
                # whether the filter would keep it, because the filter keeps the most common
                # languages of each retrieval.
                undecided = {name: day for name, day in {
                    **lagging(settings, dates, today),
                    **{name: None for name, day in dates.items() if not day}}.items()
                    if name not in kept}
            # With a list of kept languages, an earlier warning has named each language left out.
            if undecided and not kept:
                lacking = without_crawl_date(undecided, settings, today)
                logs.warning(COMPONENT, f"{upper_first(lacking)}. Left "
                                        f"{', '.join(sorted(undecided))} out of the end date of "
                                        f"the sampling window (no list of the languages that the "
                                        f"previous sampling frame kept). The retrieval fails "
                                        f"after the download of the GHS search result if the "
                                        f"filter would keep "
                                        f"{'it' if len(undecided) == 1 else 'one of them'}.")
            window = resolve_window(settings, dates, today, kept, frozenset(undecided))
            record.update(window)
            logs.info(COMPONENT, f"{describe_window(window, bool(ignored or undecided))}.")
            params = query_parameters(settings, window)
            record["query"] = params
            before = probe(lambda: count(params, ca_bundle), "the count")
            record["count_before"] = before
            logs.info(COMPONENT, f"{before:,} repositories match the query.")
            if dry_run:
                logs.info(COMPONENT, f"Dry run, nothing downloaded. A download requests the GHS "
                                     f"search result from {BASE_URL}/r/download/json with the "
                                     f"parameters {json.dumps(params, sort_keys=True)}.")
                return 0

            def attempt():
                data, received, sha = download_once(params, ca_bundle,
                                                    paths.cache_dir / f"{rid}.json.gz.part")
                missing = unapplied(params, data["parameters"])
                if missing:
                    raise Refused(f"the `parameters` of the GHS search result differ from the "
                                  f"query that was sent ({', '.join(missing)})")
                # A short request with its own attempts. A count without a response then raises
                # NoResponse, which `with_retries` passes on, rather than a Retryable, after
                # which `with_retries` would download the GHS search result again.
                after = probe(lambda: count(params, ca_bundle), "the count after the download")
                items = len(data["items"])
                low, high = min(before, after), max(before, after)
                if not low * (1 - settings.slack) <= items <= high * (1 + settings.slack):
                    raise Retryable(f"the GHS search result has {items:,} items, but the count "
                                    f"was {before:,} before the download and {after:,} after it")
                return data, received, sha, after

            paths.cache_dir.mkdir(parents=True, exist_ok=True)
            remove_interrupted_downloads(paths.cache_dir)
            data, received, sha, after = with_retries(
                attempt, settings.attempts, [60 * w for w in settings.wait_minutes],
                "the download of the GHS search result", attempts)
            if undecided:
                top = filter_top_languages(paths.filter_config, top_languages)
                rows = (data["items"] if top is None
                        else licensed_rows(data["items"], paths.filter_config))
                check_left_out(rows, undecided, top, settings, today, kept)
        except CertificateFailure as error:
            reason = (f"the certificate of {BASE_URL.split('/')[2]} did not verify against the "
                      f"trust store and {CA_INTERMEDIATES.name} ({error}). If the error names an "
                      f"issuer missing from {CA_INTERMEDIATES.name}, add that issuer's "
                      f"certificate from the CA Issuers URL of the server's certificate")
            if dry_run:
                return failed_dry_run(reason)
            return fall_back(settings, paths, rid, record, reason)
        except NoResponse as error:
            if dry_run:
                return failed_dry_run(str(error))
            return fall_back(settings, paths, rid, record, str(error))
        except Refused as error:
            if dry_run:
                return failed_dry_run(f"{error}. Correct the query in the config")
            ghs_cache.put_index_row({**record, "outcome": ghs_cache.FAILED, "error": str(error),
                                     "finished_at": ghs_cache.iso(ghs_cache.utc_now())},
                                    paths.cache_dir)
            logs.error(COMPONENT, f"Retrieval {rid} failed. {upper_first(error)}. Correct the "
                                  f"query in the config.")
            return 1
        except FileSystemFailure as error:
            # Logged before the index row is written, because a full disk can fail that write
            # as well, and the log then still names the cause.
            logs.error(COMPONENT, f"Retrieval {rid} failed. {upper_first(error)}. Stopped without "
                                  f"a fallback, because the fault is on this host, not at GHS.")
            ghs_cache.put_index_row({**record, "outcome": ghs_cache.FAILED, "error": str(error),
                                     "finished_at": ghs_cache.iso(ghs_cache.utc_now())},
                                    paths.cache_dir)
            return 1

    previous_row = retrieval_before(paths)
    smaller = smaller_than_previous(data, params, settings, previous_row, paths.cache_dir, kept)
    if smaller and not accept_smaller:
        # `item_count` holds the rows of the refused GHS search result, which are not written.
        return fall_back(settings, paths, rid, {**record, "count_after": after,
                                                "server_parameters": data["parameters"],
                                                "item_count": len(data["items"])},
                         smaller, previous_row)
    if smaller:
        logs.warning(COMPONENT, f"Retrieval {rid} would fall back, because {smaller}. Took it "
                                f"under --accept-smaller.")
    logs.info(COMPONENT, f"Writing {len(data['items']):,} repositories into the cache.")
    count_written, file_sha = ghs_cache.write_rows(
        paths.cache_dir, rid, "ghs", (ghs_cache.normalize(item) for item in data["items"]))
    row = ghs_cache.put_index_row({
        **record, "outcome": ghs_cache.RETRIEVED,
        "finished_at": ghs_cache.iso(ghs_cache.utc_now()),
        "server_parameters": data["parameters"], "count_after": after,
        "item_count": count_written, "bytes_received": received, "body_sha256": sha,
        "ghs_file_sha256": file_sha}, paths.cache_dir)
    window = _window_of(row)
    moved = previous_row is None or (window["created_max"], window["committed_min"]) != (
        previous_row["created_max"], previous_row["committed_min"])
    ghs_cache.write_pointer(rid, paths.pointer)
    write_report(paths.report, {
        "outcome": "retrieved", "retrieval_id": rid, "item_count": count_written,
        "window": window, "window_moved": moved,
        "previous_retrieval_id": previous_row["retrieval_id"] if previous_row else None,
        **({"accepted_smaller": smaller} if smaller else {})})
    logs.info(COMPONENT, f"Wrote retrieval {rid} ({count_written:,} repositories). GHS counted "
                         f"{before:,} repositories before the download and {after:,} after it.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`, which
    # gives the reason: the container's entrypoint recognizes `--accept-smaller` by its full name.
    parser = argparse.ArgumentParser(
        description="Download a retrieval of the GHS dataset into the cache of retrievals.",
        allow_abbrev=False)
    parser.add_argument("--config", "-c", type=Path, default=HERE / "config" / "ghs_download.yaml",
                        help="The YAML config (default: config/ghs_download.yaml beside this script)")
    parser.add_argument("--end-date", help=f"The end date of the sampling window, a date "
                                           f"YYYY-MM-DD or {CRAWL_DATE} for the oldest crawl date "
                                           f"of GHS's languages (default: the config's)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Compute the sampling window and count the matching repositories, "
                             "without downloading or writing anything")
    parser.add_argument("--cache-dir", type=Path, default=ghs_cache.CACHE_DIR,
                        help="The cache of retrievals (default: %(default)s)")
    parser.add_argument("--pointer", type=Path, default=ghs_cache.POINTER,
                        help="The file that names the retrieval that the stage reads "
                             "(default: %(default)s)")
    parser.add_argument("--report", type=Path, default=REPORT,
                        help="The JSON file of the outcome for the pipeline execution state "
                             "(default: %(default)s)")
    parser.add_argument("--sampling-frame", type=Path, default=SAMPLING_FRAME,
                        help="The previous sampling frame, read for the languages that it kept "
                             "(default: %(default)s)")
    parser.add_argument("--accept-smaller", action="store_true",
                        help="Take the retrieval even when it has far fewer rows than the "
                             "previous retrieval, such as after GHS lost rows for good (for one "
                             "run)")
    parser.add_argument("--top-languages", type=int, default=None,
                        help="The number of languages that the filter keeps, for the check of "
                             "the languages that the end date leaves out (default: "
                             "top_n_languages of the filter's config)")
    return parser


def main(argv: list | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.config, args.end_date)
    except (OSError, ValueError, getattr(yaml, "YAMLError", ValueError)) as error:
        logs.error(COMPONENT, f"Could not read the config {args.config} ({error}).")
        return 1
    try:
        return retrieve(settings, Paths(args.cache_dir, args.pointer, args.report,
                                        args.sampling_frame), args.dry_run, args.accept_smaller,
                        args.top_languages)
    except CacheError as error:
        logs.error(COMPONENT, f"The cache of retrievals cannot be used. {upper_first(error)}.")
        return 1
    except OSError as error:
        # A write error such as a full disk names no file, and the free space is then measured
        # in the cache of retrievals.
        where = Path(error.filename) if error.filename else args.cache_dir
        logs.error(COMPONENT, f"Could not write the retrieval ({error}). {free_space(where)}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
