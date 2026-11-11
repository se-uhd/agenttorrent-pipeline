"""
Tests for 1_download_ghs.py

Strategy:
  - `_script_loader` loads the script by path and stubs `requests` and `yaml` where they are
    not installed. Without `yaml`, the tests that read the YAML config are skipped.
  - The window, the parameters and the check of GHS's echo are pure functions that the tests
    call directly. The echo is checked against the `parameters` block that GHS wrote into the
    GHS search result of March.
  - A run is tested against a fake GHS that replaces `requests.get`. The fake responds to
    `/version`, `/l`, `/r/search` and `/r/download/json` as GHS does. On demand, it fails as a
    server can fail: with an HTTP status, a stream cut short, a GHS search result with fewer
    items than the count, a certificate that does not verify, a TLS connection dropped in the
    handshake or a parameter that GHS does not apply. A TLS failure carries the chain of
    exceptions that `requests` and urllib3 build. A stand-in for `open` raises the error of a
    full disk, a quota, a read-only file system or a denied permission, as on a host that cannot
    write the file of the GHS search result. A replaced `time.sleep` lets the retries run in
    milliseconds, and every write goes to a temporary directory.
  - `_script_loader.synced_writes` records each call of the real `os.fsync` and `os.replace`,
    and the tests read from these calls that a file is on disk before its rename.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_download_ghs -v
"""

import contextlib
import errno
import gzip
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _script_loader import is_stub, load_script, synced_writes  # noqa: E402

_mod = load_script("1_download_ghs.py")
import ghs_cache  # noqa: E402

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config" / "ghs_download.yaml"

# The `parameters` block of the GHS search result of March, as GHS wrote it (ghs_complete.json,
# 2026-03-16).
MARCH_SERVER_PARAMETERS = {"nameEquals": False, "contributorsMin": 2, "pullsMin": 1,
                           "createdMax": 1726444800000, "committedMin": 1757980800000,
                           "excludeForks": True, "onlyForks": False, "hasIssues": False,
                           "hasPulls": False, "hasWiki": False, "hasLicense": True}


def _settings(**overrides):
    values = dict(query={"contributorsMin": 2, "pullsMin": 1, "excludeForks": True,
                         "hasLicense": True},
                  end_date="crawl_date", created_before_months=18, committed_within_months=6,
                  max_crawl_lag_days=30, attempts=3, wait_minutes=(5, 30), probe_attempts=2,
                  probe_wait_seconds=1, slack=0.001, on_failure="previous_sampling_frame")
    values.update(overrides)
    return _mod.Settings(**values)


class _Response:

    def __init__(self, status=200, body=b"", text="", payload=None, headers=None, chunks=None,
                 fail_after=None):
        self.status_code, self.headers, self.text = status, headers or {}, text
        self.payload, self.body, self.fail_after = payload, body, fail_after
        self.chunks = chunks

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload

    def iter_content(self, chunk_size):
        data = self.body
        for start in range(0, len(data), 7):
            if self.fail_after is not None and start >= self.fail_after:
                raise _mod.requests.RequestException("connection reset")
            yield data[start:start + 7]

    def close(self):
        pass


def _ms(day):
    return _mod._midnight_ms(day)


def _tls_failure(cause):
    """The exception that `requests` raises for a TLS failure, chained as urllib3 2 and
    `requests` 2 chain it. urllib3 wraps the error of `ssl` in its own `SSLError` and raises
    `MaxRetryError` from it. `requests` raises its `SSLError` while it handles that
    `MaxRetryError`."""
    import urllib3
    try:
        try:
            try:
                raise cause
            except ssl.SSLError as error:
                wrapped = urllib3.exceptions.SSLError(error)
                raise urllib3.exceptions.MaxRetryError(None, _mod.BASE_URL + "/version",
                                                       wrapped) from wrapped
        except urllib3.exceptions.MaxRetryError as error:
            raise _mod.requests.exceptions.SSLError(error)
    except _mod.requests.exceptions.SSLError as error:
        return error


class FakeGhs:
    """GHS as the script uses it: a version page, a language list, a search and the download of
    the GHS search result."""

    # The number of requests for pages of the language list after which the fake stops the
    # test. A download that never stops paging then fails rather than hangs.
    LANGUAGE_PAGE_REQUESTS_MAX = 1000

    def __init__(self, items=3, crawl_dates=None, counts=None, search_results=None, echo=None,
                 versions=None, languages=None, crawl_failures=None, extra_echo=None,
                 language_pages=None):
        # `languages` maps a language to the number of items that have it as their main
        # language. The items take the languages in order, and the items after them have no
        # main language. Every item has a license that the filter keeps.
        main = [language for language, number in (languages or {}).items()
                for _ in range(number)]
        self.items = [{"id": i, "name": f"o{i}/r{i}", "metrics": [{"language": "Z"},
                                                                 {"language": "A"}],
                       "license": "MIT License",
                       **({"mainLanguage": main[i]} if i < len(main) else {})}
                      for i in range(items)]
        self.crawl_dates = crawl_dates or {"Python": "2026-09-22", "Shell": "2026-10-02"}
        # The responses to the requests for a language's crawl date, in order, before the fake
        # returns the crawl date. Each is a `_Response` that fails, an exception that the
        # request raises, or None for a request that gets the crawl date.
        self.crawl_failures = {language: list(steps)
                               for language, steps in (crawl_failures or {}).items()}
        self.counts = list(counts) if counts else []
        self.search_results = list(search_results) if search_results else []
        # The responses to the requests for the version page, in order, before the fake returns
        # the version.
        self.versions = list(versions) if versions else []
        self.echo = echo
        # Parameters that a GHS upgrade can add to the echo, beside the parameters that GHS was
        # sent.
        self.extra_echo = extra_echo or {}
        # A function that returns the response to a request for a page of the language list,
        # given the request's parameters. Without it, the fake lists every language on one page.
        self.language_pages = language_pages
        self.calls = []

    def get(self, url, params=None, stream=False, timeout=None, verify=None):
        path = url[len(_mod.BASE_URL):]
        self.calls.append((path, dict(params or {})))
        assert timeout == (_mod.CONNECT_TIMEOUT, _mod.READ_TIMEOUT)
        assert verify
        if path == "/version":
            step = self.versions.pop(0) if self.versions else None
            if isinstance(step, Exception):
                raise step
            if isinstance(step, _Response):
                return step
            return _Response(text="<pre>MMMM version: 1.17.1, built on: 2024-08-06T15:37:33Z</pre>")
        if path == "/l":
            if self.language_pages is None:
                return _Response(payload={"items": list(self.crawl_dates), "last": True})
            if [called for called, _ in self.calls].count("/l") > self.LANGUAGE_PAGE_REQUESTS_MAX:
                raise AssertionError(f"the download requested more than "
                                     f"{self.LANGUAGE_PAGE_REQUESTS_MAX} pages of the language list")
            return self.language_pages(params)
        if path == "/r/search" and "language" in params:
            steps = self.crawl_failures.get(params["language"])
            step = steps.pop(0) if steps else None
            if isinstance(step, Exception):
                raise step
            if step is not None:
                return step
            day = self.crawl_dates[params["language"]]
            assert params["sort"] == "lastCommit,desc" and params["committedMax"]
            return _Response(payload={"items": [{"lastCommit": f"{day}T04:23:40"}]})
        if path == "/r/search":
            step = self.counts.pop(0) if self.counts else len(self.items)
            if isinstance(step, _Response):
                return step
            return _Response(payload={"totalItems": step})
        if path == "/r/download/json":
            step = self.search_results.pop(0) if self.search_results else None
            if isinstance(step, _Response):
                return step
            if isinstance(step, Exception):
                raise step
            items = self.items if step is None else self.items[:step]
            echo = self.echo or {name: (_ms(value) if name in _mod.DATE_PARAMETERS
                                        else value == "true" if value in ("true", "false")
                                        else int(value) if value.isdigit() else value)
                                 for name, value in params.items()}
            echo = {**echo, **self.extra_echo}
            body = gzip.compress(json.dumps({"parameters": echo, "items": items}).encode())
            return _Response(body=body)
        raise AssertionError(f"unexpected request {path}")


class _RunCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.paths = _mod.Paths(self.tmp / "ghs_retrievals", self.tmp / "ghs_retrieval.json",
                                self.tmp / "report.json", self.tmp / "ghs_filtered.json")
        for target, value in ((_mod.time, "sleep"), (_mod, "write_ca_bundle")):
            patcher = mock.patch.object(target, value, (lambda seconds: None) if value == "sleep"
                                        else (lambda directory: Path(directory) / "ca.pem"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.sleeps = []
        _mod.time.sleep = self.sleeps.append
        clock = mock.patch.object(ghs_cache, "utc_now", side_effect=self._now)
        clock.start()
        self.addCleanup(clock.stop)
        self.now = iter(_now_times())

    def _now(self):
        return next(self.now)

    def run_with(self, ghs, settings=None, dry_run=False, accept_smaller=False):
        err = io.StringIO()
        with mock.patch.object(_mod.requests, "get", ghs.get), contextlib.redirect_stderr(err):
            code = _mod.retrieve(settings or _settings(), self.paths, dry_run, accept_smaller)
        return code, err.getvalue()

    def frame_kept(self, languages):
        """Write the previous sampling frame as the filter writes it, with the languages that
        the filter's step `languages` kept."""
        self.paths.sampling_frame.write_text(json.dumps({"_metadata": {"stages": [
            {"stage": 6, "key": "languages", "values": {"top": 10, "languages": languages}}]},
            "items": []}), encoding="utf-8")

    def index(self):
        return ghs_cache.read_index(self.paths.cache_dir)

    def report(self):
        return json.loads(self.paths.report.read_text())


def _now_times():
    from datetime import datetime, timedelta, timezone
    start = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)
    return (start + timedelta(seconds=n) for n in range(10_000))


class TestLoadSettings(unittest.TestCase):

    def setUp(self):
        if is_stub("yaml"):
            self.skipTest("yaml is not installed")
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _load(self, text, end_date=None):
        path = self.tmp / "c.yaml"
        path.write_text(text)
        return _mod.load_settings(path, end_date)

    BASE = ("query: {contributorsMin: 2, hasLicense: true}\n"
            "window: {end_date: crawl_date, created_before_months: 18, "
            "committed_within_months: 6}\n")

    def test_the_shipped_config_sets_the_march_query_and_ends_the_window_on_the_crawl_date(self):
        settings = _mod.load_settings(CONFIG)
        self.assertEqual(settings.query, {"contributorsMin": 2, "pullsMin": 1,
                                          "excludeForks": True, "hasLicense": True})
        self.assertEqual(settings.end_date, "crawl_date")
        self.assertEqual((settings.created_before_months, settings.committed_within_months),
                         (18, 6))
        self.assertEqual(settings.on_failure, "previous_sampling_frame")

    def test_the_shipped_config_waits_hours_for_the_short_requests_before_falling_back(self):
        """An outage of GHS or of the network can last hours, and a fallback keeps the previous
        sampling window until the next run."""
        waits = _mod.load_settings(CONFIG).probe_round_wait_minutes
        self.assertEqual(list(waits), sorted(waits))
        self.assertGreaterEqual(sum(waits), 2 * 60)

    def test_a_parameter_that_ghs_does_not_have_is_refused(self):
        with self.assertRaisesRegex(ValueError, "GHS has no parameter contributors_min"):
            self._load(self.BASE.replace("contributorsMin", "contributors_min"))

    def test_the_window_parameters_are_not_set_by_the_query(self):
        with self.assertRaisesRegex(ValueError, "createdMax is set by the window"):
            self._load(self.BASE.replace("hasLicense: true", "createdMax: '2024-09-16'"))

    def test_values_of_the_wrong_type_are_refused(self):
        with self.assertRaisesRegex(ValueError, "hasLicense is true or false"):
            self._load(self.BASE.replace("hasLicense: true", "hasLicense: 'yes'"))
        with self.assertRaisesRegex(ValueError, "contributorsMin is an integer"):
            self._load(self.BASE.replace("contributorsMin: 2", "contributorsMin: '2'"))

    def test_the_end_date_is_a_date_or_the_crawl_date_and_the_command_line_wins(self):
        self.assertEqual(self._load(self.BASE, "2026-03-16").end_date, "2026-03-16")
        with self.assertRaisesRegex(ValueError, "the end date is"):
            self._load(self.BASE, "18 months ago")

    def test_too_few_waits_and_an_unknown_failure_mode_are_refused(self):
        with self.assertRaisesRegex(ValueError, "must list 2 waits"):
            self._load(self.BASE + "retries: {attempts: 3, wait_minutes: [5]}\n")
        with self.assertRaisesRegex(ValueError, "on_failure is one of"):
            self._load(self.BASE + "on_failure: retry_forever\n")
        with self.assertRaisesRegex(ValueError, "probe_round_wait_minutes must list waits"):
            self._load(self.BASE + "retries: {probe_round_wait_minutes: [10, -1]}\n")


class TestWindow(unittest.TestCase):

    def test_months_are_counted_back_with_the_day_clamped(self):
        self.assertEqual(_mod.months_before(date(2026, 3, 16), 18), date(2024, 9, 16))
        self.assertEqual(_mod.months_before(date(2026, 3, 16), 6), date(2025, 9, 16))
        self.assertEqual(_mod.months_before(date(2026, 3, 31), 1), date(2026, 2, 28))
        self.assertEqual(_mod.months_before(date(2024, 2, 29), 12), date(2023, 2, 28))
        self.assertEqual(_mod.months_before(date(2026, 1, 15), 13), date(2024, 12, 15))

    def test_a_date_as_the_end_date_gives_the_march_window(self):
        window = _mod.resolve_window(_settings(end_date="2026-03-16"), {}, date(2026, 10, 5))
        self.assertEqual(window, {"end_date": "2026-03-16", "end_date_source": "date",
                                  "created_max": "2024-09-16", "committed_min": "2025-09-16"})

    def test_the_end_date_is_the_oldest_crawl_date(self):
        window = _mod.resolve_window(_settings(), {"Shell": "2026-10-02", "Python": "2026-09-22",
                                                   "Racket": None}, date(2026, 10, 5))
        self.assertEqual(window["end_date"], "2026-09-22")
        self.assertEqual(window["end_date_source"], "crawl_date:Python")
        self.assertEqual((window["created_max"], window["committed_min"]),
                         ("2025-03-22", "2026-03-22"))

    def test_a_crawler_that_stopped_gives_no_end_date(self):
        with self.assertRaisesRegex(_mod.NoResponse, "is 41 days old"):
            _mod.resolve_window(_settings(), {"Python": "2026-08-25"}, date(2026, 10, 5))
        with self.assertRaisesRegex(_mod.NoResponse, "no crawl date"):
            _mod.resolve_window(_settings(), {"Python": None}, date(2026, 10, 5))

    def test_a_crawl_date_as_old_as_the_config_allows_is_the_end_date(self):
        window = _mod.resolve_window(_settings(max_crawl_lag_days=30), {"Python": "2026-09-05"},
                                     date(2026, 10, 5))
        self.assertEqual(window["end_date"], "2026-09-05")
        with self.assertRaisesRegex(_mod.NoResponse, "is 31 days old"):
            _mod.resolve_window(_settings(max_crawl_lag_days=30), {"Python": "2026-09-04"},
                                date(2026, 10, 5))

    def test_a_stalled_language_that_the_previous_sampling_frame_did_not_keep_is_left_out(self):
        """Only the kept languages are held to the crawl lag, because the filter removes every
        row of a language that it does not keep. Another language still decides the end date
        while its crawl date is within the lag. Beyond the lag, it is left out of the end date."""
        dates = {"Python": "2026-09-22", "Shell": "2026-08-01", "Elm": "2026-09-20"}
        today = date(2026, 10, 5)
        window = _mod.resolve_window(_settings(), dates, today, frozenset({"Python"}))
        self.assertEqual((window["end_date"], window["end_date_source"]),
                         ("2026-09-20", "crawl_date:Elm"))
        self.assertEqual(_mod.stalled_outside(_settings(), dates, today, frozenset({"Python"})),
                         {"Shell": "2026-08-01"})
        with self.assertRaisesRegex(_mod.NoResponse, "Shell, is 65 days old"):
            _mod.resolve_window(_settings(), dates, today)
        with self.assertRaisesRegex(_mod.NoResponse, r"The crawl date of Shell \(kept in the "
                                                     r"previous sampling frame\), 2026-08-01, is "
                                                     r"65 days old"):
            _mod.resolve_window(_settings(), dates, today, frozenset({"Shell"}))

    def test_the_parameters_are_the_query_and_the_window_without_a_sort(self):
        window = _mod.resolve_window(_settings(end_date="2026-03-16"), {}, date(2026, 3, 16))
        self.assertEqual(_mod.query_parameters(_settings(), window), {
            "committedMin": "2025-09-16", "contributorsMin": "2", "createdMax": "2024-09-16",
            "excludeForks": "true", "hasLicense": "true", "pullsMin": "1"})


class TestTheEcho(unittest.TestCase):

    def setUp(self):
        window = _mod.resolve_window(_settings(end_date="2026-03-16"), {}, date(2026, 3, 16))
        self.params = _mod.query_parameters(_settings(), window)

    def test_the_march_search_result_echoes_every_parameter_of_the_march_query(self):
        self.assertEqual(_mod.unapplied(self.params, MARCH_SERVER_PARAMETERS), [])

    def test_a_parameter_ghs_dropped_or_changed_is_named(self):
        echoed = dict(MARCH_SERVER_PARAMETERS, committedMin=_ms("2025-09-17"))
        del echoed["pullsMin"]
        self.assertEqual(_mod.unapplied(self.params, echoed),
                         [f"committedMin=2025-09-16 echoed as {_ms('2025-09-17')}",
                          "pullsMin=1 echoed as None"])


class TestStatusAndRetries(unittest.TestCase):

    def test_busy_and_failing_servers_are_retried_and_other_client_errors_refused(self):
        """Only a request that carries the config's query can be rejected for that query. A
        client error on the version page, the language list or a crawl date counts as no
        response."""
        for carries_query in (True, False):
            _mod.check_status(_Response(200), "x", carries_query)
            for status in (408, 429, 500, 502, 503, 504):
                with self.subTest(status=status, carries_query=carries_query), \
                        self.assertRaises(_mod.Retryable):
                    _mod.check_status(_Response(status), "x", carries_query)
        for status in (400, 403, 404):
            with self.subTest(status=status), self.assertRaises(_mod.Refused):
                _mod.check_status(_Response(status), "x", True)
            with self.subTest(status=status), self.assertRaises(_mod.Retryable):
                _mod.check_status(_Response(status), "x", False)
        with self.assertRaises(_mod.Retryable) as caught:
            _mod.check_status(_Response(429, headers={"Retry-After": "90"}), "x", True)
        self.assertEqual(caught.exception.retry_after, 90)

    def test_attempts_are_bounded_waited_and_recorded(self):
        sleeps, record, results = [], [], iter([_mod.Retryable("a"), _mod.Retryable("b", 900), 7])

        def action():
            value = next(results)
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(_mod.time, "sleep", sleeps.append), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_mod.with_retries(action, 3, [5, 10], "x", record), 7)
        self.assertEqual(sleeps, [5, 900])
        self.assertEqual([r["error"] for r in record], ["a", "b", ""])

    def test_every_attempt_failing_raises_no_response_with_the_last_error(self):
        def action():
            raise _mod.Retryable("still down")

        with mock.patch.object(_mod.time, "sleep", lambda s: None), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaisesRegex(_mod.NoResponse,
                                       r"^all 2 attempts at x failed\. In the last one, still "
                                       r"down$"):
            _mod.with_retries(action, 2, [1], "x", [])


class TestDownloadOnce(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.part = self.tmp / "x.json.gz.part"

    def _once(self, response):
        with mock.patch.object(_mod.requests, "get", lambda *a, **k: response), \
                contextlib.redirect_stderr(io.StringIO()):
            return _mod.download_once({}, self.tmp / "ca.pem", self.part)

    def test_a_whole_search_result_is_parsed_and_its_body_hashed(self):
        body = json.dumps({"parameters": {}, "items": [{"id": 1}]}).encode()
        data, received, sha = self._once(_Response(body=gzip.compress(body)))
        self.assertEqual(data["items"], [{"id": 1}])
        self.assertEqual(sha, __import__("hashlib").sha256(body).hexdigest())
        self.assertFalse(self.part.exists())

    def test_the_log_names_the_start_the_size_and_the_decompression_and_no_progress(self):
        """A download is one transfer. Its log names the start, the size at the end and the
        decompression as the next action. The log has no line for the bytes received so far,
        even when a progress line would be due."""
        body = gzip.compress(json.dumps({"parameters": {}, "items": []}).encode() * 1)
        err = io.StringIO()
        with mock.patch.object(_mod.requests, "get", lambda *a, **k: _Response(body=body)), \
                mock.patch.object(_mod.logs.Pace, "due", lambda self: True), \
                contextlib.redirect_stderr(err):
            _mod.download_once({}, self.tmp / "ca.pem", self.part)
        lines = [line.split(": ", 1)[1] for line in err.getvalue().splitlines()]
        self.assertEqual(lines, ["Downloading the GHS search result.",
                                 "Downloaded the GHS search result (0 MiB).",
                                 "Decompressing the GHS search result."])

    def test_a_body_that_is_not_gzip_is_retryable(self):
        """gzip raises an OSError for a body that does not start as a gzip stream, such as the
        HTML page of a proxy. That OSError comes from the server's response, not from this
        host's file system."""
        with self.assertRaisesRegex(_mod.Retryable, "not a complete gzip stream"):
            self._once(_Response(body=b"<html><body>502 Bad Gateway</body></html>"))
        self.assertFalse(self.part.exists())

    def test_a_body_cut_short_is_retryable_and_leaves_nothing(self):
        whole = gzip.compress(b'{"parameters": {}, "items": []}' * 50)
        with self.assertRaisesRegex(_mod.Retryable, "not a complete gzip stream"):
            self._once(_Response(body=whole[: len(whole) // 2]))
        self.assertFalse(self.part.exists())

    def test_a_stream_broken_part_way_is_retryable(self):
        if is_stub("requests"):
            self.skipTest("requests is not installed")
        with self.assertRaisesRegex(_mod.Retryable, "the download of the GHS search result failed"):
            self._once(_Response(body=gzip.compress(b"x" * 100), fail_after=14))
        self.assertFalse(self.part.exists())

    def test_a_response_without_items_or_parameters_is_retryable(self):
        for payload in ([], {"items": []}, {"parameters": {}, "items": {}}):
            with self.subTest(payload=payload), self.assertRaises(_mod.Retryable):
                self._once(_Response(body=gzip.compress(json.dumps(payload).encode())))


class TestARun(_RunCase):

    def test_a_retrieval_is_written_into_the_cache_and_named(self):
        code, err = self.run_with(FakeGhs(items=3))
        self.assertEqual(code, 0, err)
        rows = self.index()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        rid = row["retrieval_id"]
        self.assertEqual(rid, "2026-10-05T120000Z")
        self.assertEqual(row["outcome"], "retrieved")
        self.assertEqual((row["end_date"], row["end_date_source"]),
                         ("2026-09-22", "crawl_date:Python"))
        self.assertEqual((row["created_max"], row["committed_min"]), ("2025-03-22", "2026-03-22"))
        self.assertEqual(json.loads(row["crawl_dates"]),
                         {"Python": "2026-09-22", "Shell": "2026-10-02"})
        self.assertNotIn("sort", json.loads(row["query"]))
        self.assertEqual((row["count_before"], row["count_after"], row["item_count"]),
                         ("3", "3", "3"))
        self.assertEqual(row["ghs_version"], "1.17.1 built 2024-08-06T15:37:33Z")
        cached = list(ghs_cache.read_rows(self.paths.cache_dir, rid, "ghs", row["ghs_file_sha256"]))
        self.assertEqual([item["id"] for item in cached], [0, 1, 2])
        self.assertEqual([m["language"] for m in cached[0]["metrics"]], ["A", "Z"])
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": rid})
        self.assertEqual(self.report()["outcome"], "retrieved")
        self.assertTrue(self.report()["window_moved"])
        self.assertEqual(list(self.paths.cache_dir.glob("*.part")), [])
        self.assertIn(f"INFO ghs_download: Wrote retrieval {rid} (3 repositories). GHS counted 3 "
                      f"repositories before the download and 3 after it", err)
        self.assertNotIn("received", err)
        for line in ("The version of GHS is 1.17.1 built 2024-08-06T15:37:33Z",
                     "The sampling window ends on 2026-09-22, the crawl date of Python, the "
                     "oldest of GHS's languages. The query selects the repositories created on or "
                     "before 2025-03-22 (createdMax) with a last commit on or after 2026-03-22 "
                     "(committedMin)",
                     "3 repositories match the query"):
            with self.subTest(line=line):
                self.assertIn(f"INFO ghs_download: {line}.\n", err)

    def test_a_search_result_short_of_the_count_is_taken_again(self):
        ghs = FakeGhs(items=1000, search_results=[500])
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        self.assertIn("Attempt 1 of 3 failed. The GHS search result has 500 items", err)
        self.assertEqual(self.index()[0]["item_count"], "1000")
        self.assertEqual(self.sleeps[-1], 300)

    def test_the_counts_before_and_after_bound_the_search_result_with_the_slack(self):
        ghs = FakeGhs(items=1000, counts=[1001, 1002])
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        ghs = FakeGhs(items=1000, counts=[1010, 1010, 1010, 1010, 1010, 1010])
        code, err = self.run_with(ghs, _settings(on_failure="fail"))
        self.assertEqual(code, 1)
        self.assertIn("the count was 1,010 before the download and 1,010 after it", err)

    def test_a_search_result_with_more_items_than_the_query_matched_is_refused(self):
        ghs = FakeGhs(items=1010, counts=[1000] * 6)
        code, err = self.run_with(ghs, _settings(on_failure="fail"))
        self.assertEqual(code, 1)
        self.assertIn("the GHS search result has 1,010 items, but the count was 1,000 before "
                      "the download", err)

    def test_a_probe_that_fails_is_retried(self):
        ghs = FakeGhs(counts=[_Response(503), 3, 3])
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        self.assertIn("Attempt 1 of 2 failed. GHS returned HTTP 503 for the count", err)

    def test_a_client_error_on_a_request_without_the_query_is_retried(self):
        """A 404 for the version page cannot reject the config's query. It counts as no
        response, which another attempt may get past, and not as a refusal that ends the run."""
        ghs = FakeGhs(versions=[_Response(404)])
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        self.assertIn("Attempt 1 of 2 failed. GHS returned HTTP 404 for the version", err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")

    def test_a_short_request_is_made_again_after_an_outage_of_hours(self):
        """GHS returns no response through the attempts of a short request and the first round
        wait. The second round gets a response, and the retrieval is taken rather than the stage
        falling back."""
        down = [_Response(503)] * 4
        code, err = self.run_with(FakeGhs(versions=down),
                                  _settings(probe_round_wait_minutes=(30, 120)))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")
        self.assertEqual([s for s in self.sleeps if s >= 30 * 60], [30 * 60, 120 * 60])
        self.assertIn("Requesting the version again in 30 minutes", err)

    def test_a_count_after_the_download_that_fails_does_not_download_again(self):
        """The count after the download has the retries of a short request. A count inside the
        retries of the download would download the GHS search result again when it failed."""
        ghs = FakeGhs(counts=[3, _Response(503), 3])
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        self.assertEqual([path for path, _ in ghs.calls].count("/r/download/json"), 1)
        self.assertIn("Attempt 1 of 2 failed. GHS returned HTTP 503 for the count", err)

    def test_a_second_retrieval_with_the_same_end_date_records_that_the_window_did_not_move(self):
        self.run_with(FakeGhs())
        code, _ = self.run_with(FakeGhs())
        self.assertEqual(code, 0)
        self.assertFalse(self.report()["window_moved"])
        self.assertEqual(self.report()["previous_retrieval_id"], "2026-10-05T120000Z")

    def test_each_attempt_requests_only_the_crawl_dates_that_it_lacks(self):
        """A request for a crawl date can fail on its own. GHS fails the request for Shell's
        crawl date in the first attempt and the second request for Python's. An attempt that
        requested every language again would fail in both attempts, and the retrieval would
        fail. The second attempt requests Shell alone."""
        ghs = FakeGhs(crawl_failures={"Python": [None, _Response(503)],
                                      "Shell": [_Response(503)]})
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")
        requested = [params["language"] for path, params in ghs.calls
                     if path == "/r/search" and "language" in params]
        self.assertEqual(requested, ["Python", "Shell", "Shell"])
        self.assertIn("Attempt 1 of 2 failed. GHS returned no crawl date for 1 of 2 languages "
                      "(Shell). In the last failed request, GHS returned HTTP 503 for the crawl "
                      "date of Shell. Trying again in", err)
        self.assertEqual(json.loads(self.index()[-1]["crawl_dates"]),
                         {"Python": "2026-09-22", "Shell": "2026-10-02"})

    def test_a_search_that_fails_for_every_language_ends_each_attempt_after_a_few_requests(self):
        """GHS's search times out, or a proxy returns 504 for it, while the version page and the
        language list respond. An attempt that requested every language without a date would
        send one request per language in each attempt of each round and wait for each request
        to fail before the stage falls back. With two attempts in each of three rounds, each
        attempt may send no more than three requests."""
        failures = [("HTTP 504", lambda: _Response(504))]
        if not is_stub("requests"):
            failures.append(("a timeout", lambda: _mod.requests.exceptions.ReadTimeout(
                "read timed out (read timeout=300)")))
        languages = {f"Language{number:02d}": "2026-09-30" for number in range(44)}
        for name, failure in failures:
            with self.subTest(failure=name):
                ghs = FakeGhs(crawl_dates=languages,
                              crawl_failures={language: [failure() for _ in range(20)]
                                              for language in languages})
                code, err = self.run_with(ghs, _settings(probe_attempts=2,
                                                         probe_round_wait_minutes=(10, 30),
                                                         on_failure="fail"))
                self.assertEqual(code, 1, err)
                searches = [params["language"] for path, params in ghs.calls
                            if path == "/r/search" and "language" in params]
                self.assertGreaterEqual(len(searches), 3 * 2)
                self.assertLessEqual(len(searches), 3 * 2 * 3)
                self.assertIn("Ended the attempt after 3 failed requests in a row, without "
                              "requesting 41 of the languages", self.index()[-1]["error"])

    def test_languages_that_fail_once_each_still_get_their_crawl_dates(self):
        """Requests for single languages can fail on their own. Two failures that are not in a
        row end no attempt, and the second attempt requests the two failed languages alone."""
        dates = {"A": "2026-09-30", "B": "2026-09-29", "C": "2026-10-01", "D": "2026-09-28",
                 "E": "2026-10-02"}
        ghs = FakeGhs(crawl_dates=dates, crawl_failures={"B": [_Response(503)],
                                                         "D": [_Response(503)]})
        code, err = self.run_with(ghs, _settings(probe_attempts=2))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")
        requested = [params["language"] for path, params in ghs.calls
                     if path == "/r/search" and "language" in params]
        self.assertEqual(requested, ["A", "B", "C", "D", "E", "B", "D"])
        self.assertEqual(json.loads(self.index()[-1]["crawl_dates"]), dates)

    def test_a_last_commit_that_is_no_date_is_a_failed_request(self):
        """GHS's search for the crawl date of Python returns a repository with a `lastCommit`
        that does not start with a date. The search counts as a failed request, because the
        window and the filter read a crawl date as a date. The next attempt gets Python's crawl
        date."""
        for stamp in ("yesterday", 1759276800000):
            with self.subTest(stamp=stamp):
                ghs = FakeGhs(crawl_failures={
                    "Python": [_Response(payload={"items": [{"lastCommit": stamp}]})]})
                code, err = self.run_with(ghs)
                self.assertEqual(code, 0, err)
                row = self.index()[-1]
                self.assertEqual((row["outcome"], json.loads(row["crawl_dates"])),
                                 ("retrieved", {"Python": "2026-09-22", "Shell": "2026-10-02"}))
                self.assertIn(f"Attempt 1 of 2 failed. GHS returned no crawl date for 1 of 2 "
                              f"languages (Python). In the last failed request, the `lastCommit` "
                              f"of the response for the crawl date of Python does not start with "
                              f"a date ({stamp!r}). Trying again in", err)

    def test_a_date_as_the_end_date_requests_no_crawl_date(self):
        """With a date as the end date, no crawl date bounds the window. GHS's search fails for
        the crawl date of Python every time. Python has the most repositories of the GHS search
        result, and without a sampling frame, a run that requested crawl dates would fail the
        retrieval with Python as a language that the end date left out. The run requests neither
        the languages nor their crawl dates, and the retrieval is written."""
        ghs = FakeGhs(items=6, languages={"Python": 3, "Go": 2, "Smalltalk": 1},
                      crawl_dates={"Go": "2026-09-25", "Python": "2026-09-22",
                                   "Smalltalk": "2026-09-30"},
                      crawl_failures={"Python": [_Response(500) for _ in range(100)]})
        code, err = self.run_with(ghs, _settings(end_date="2026-10-01"))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"],
                          row["crawl_dates"]), ("retrieved", "2026-10-01", "date", ""))
        self.assertEqual([path for path, params in ghs.calls
                          if path == "/l" or "language" in params], [])
        self.assertEqual(self.sleeps, [])

    def test_the_next_download_removes_a_partial_search_result_that_a_stop_left(self):
        """A Stop or a kill can end the download before it removes its `<id>.json.gz.part`. The
        next download removes that file, because no later download reads it."""
        self.paths.cache_dir.mkdir(parents=True)
        left = self.paths.cache_dir / "2026-10-01T000000Z.json.gz.part"
        left.write_bytes(b"\x1f\x8b part of a GHS search result")
        code, err = self.run_with(FakeGhs())
        self.assertEqual(code, 0, err)
        self.assertFalse(left.exists())
        self.assertEqual(list(self.paths.cache_dir.glob("*.part")), [])
        self.assertIn("INFO ghs_download: Removed the partial GHS search results of stopped "
                      "downloads (2026-10-01T000000Z.json.gz.part).\n", err)

    def test_the_report_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """An empty report after a power loss would keep the runner from reading the outcome of
        the download."""
        code, err = self.run_with(FakeGhs())
        self.assertEqual(code, 0, err)
        path = self.paths.report
        before = path.stat().st_ino
        calls = synced_writes(lambda: _mod.write_report(path, {"outcome": "retrieved"}),
                              path.parent)
        after = path.stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(calls, [("file", after), ("rename", after, path.name),
                                 ("directory", True)])
        self.assertEqual(self.report(), {"outcome": "retrieved"})

    def test_a_dry_run_writes_nothing(self):
        code, err = self.run_with(FakeGhs(), dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn("Dry run", err)
        self.assertFalse(self.paths.cache_dir.exists())
        self.assertFalse(self.paths.pointer.exists())


class TestWhenGhsReturnsNoResponse(_RunCase):

    def _earlier(self):
        code, _ = self.run_with(FakeGhs(items=2))
        self.assertEqual(code, 0)
        return self.paths.pointer.read_bytes()

    def test_the_stage_continues_with_the_previous_sampling_frame(self):
        pointer = self._earlier()
        down = [_mod.requests.RequestException("refused")] * 3 if not is_stub("requests") \
            else [_Response(503)] * 3
        code, err = self.run_with(FakeGhs(search_results=down))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.paths.pointer.read_bytes(), pointer)
        failed = self.index()[-1]
        self.assertEqual(failed["outcome"], "failed")
        self.assertEqual(failed["fallback"], "2026-10-05T120000Z")
        self.assertIn("all 3 attempts at the download of the GHS search result failed",
                      failed["error"])
        self.assertEqual(len(json.loads(failed["attempts"])), 3 + 4)
        report = self.report()
        self.assertEqual((report["outcome"], report["window_moved"]), ("fallback", False))
        self.assertIn("WARNING ghs_download: Retrieval ", err)
        self.assertIn("Fell back to the previous retrieval, 2026-10-05T120000Z, and its sampling "
                      "window.\n", err)
        self.assertEqual(self.sleeps, [300, 1800])

    def test_without_an_earlier_retrieval_the_run_fails(self):
        code, err = self.run_with(FakeGhs(search_results=[_Response(503)] * 3))
        self.assertEqual(code, 1)
        self.assertIn("the cache of retrievals has no earlier retrieval", err)
        self.assertEqual(self.index()[0]["outcome"], "failed")
        self.assertFalse(self.paths.pointer.exists())

    def test_a_previous_retrieval_whose_rows_are_gone_is_no_fallback(self):
        self._earlier()
        for path in self.paths.cache_dir.glob("*.ghs.jsonl.xz*"):
            path.unlink()
        code, err = self.run_with(FakeGhs(search_results=[_Response(503)] * 3))
        self.assertEqual(code, 1, err)
        self.assertEqual(self.index()[-1]["fallback"], "")

    def test_on_failure_fail_does_not_fall_back(self):
        self._earlier()
        code, err = self.run_with(FakeGhs(search_results=[_Response(503)] * 3),
                                  _settings(on_failure="fail"))
        self.assertEqual(code, 1)
        self.assertIn("on_failure to fail", err)

    def test_a_crawler_that_stopped_falls_back(self):
        self._earlier()
        code, err = self.run_with(FakeGhs(crawl_dates={"Python": "2026-08-01"}))
        self.assertEqual(code, 0)
        self.assertIn("is 65 days old", self.report()["reason"])

    def _falls_back(self, ghs, pointer: bytes, reason: str) -> None:
        """Run the download against `ghs` and check that it fell back to the earlier retrieval
        with the pointer `pointer`. The new index row is `failed`, and its error contains
        `reason`."""
        code, err = self.run_with(ghs)
        self.assertEqual((code, self.report()["outcome"]), (0, "fallback"), err)
        self.assertEqual(self.paths.pointer.read_bytes(), pointer)
        failed = self.index()[-1]
        self.assertEqual((failed["outcome"], failed["fallback"]),
                         ("failed", "2026-10-05T120000Z"))
        self.assertIn(reason, failed["error"])

    def test_a_language_list_that_never_ends_falls_back(self):
        """GHS returns every page of its language list with more pages to come, as a GHS that
        ignores `page` would. Each request succeeds, and a download that read pages until GHS
        returned `"last": true` would never end its first step. An attempt reads a bounded
        number of pages and fails, and the next attempt starts again on the first page. After
        the attempts, the stage continues over the previous sampling frame."""
        pointer = self._earlier()
        ghs = FakeGhs(language_pages=lambda params: _Response(
            payload={"items": ["Python"], "last": False}))
        self._falls_back(ghs, pointer, "all 2 attempts at the language list failed. In the last "
                                       "one, the language list did not end within")
        pages = [params["page"] for path, params in ghs.calls if path == "/l"]
        self.assertEqual(pages, list(range(len(pages) // 2)) * 2)

    def test_a_response_that_the_download_cannot_read_falls_back(self):
        """GHS responds with HTTP 200 and JSON that the download cannot read. The language list
        has null `items`, holds no names or is not in an object, or the count is a list, a
        string or a number rather than an object with `totalItems`. Each such response counts as
        no response, because another attempt may get a whole response. After the attempts, the
        stage continues over the previous sampling frame, as when GHS does not respond."""
        pointer = self._earlier()
        language_list = ("page 0 of the language list is not an object with an `items` list of "
                         "names")
        count = "the response for the count is not an object with a `totalItems` number"
        for name, ghs, reason in (
                ("null items", FakeGhs(language_pages=lambda params: _Response(
                    payload={"items": None, "last": True})), language_list),
                ("items without names", FakeGhs(language_pages=lambda params: _Response(
                    payload={"items": [{"name": "Python"}], "last": True})), language_list),
                ("a list of languages", FakeGhs(language_pages=lambda params: _Response(
                    payload=["Python"])), language_list),
                ("a list as the count", FakeGhs(counts=[_Response(payload=[])] * 10), count),
                ("a string as the count", FakeGhs(counts=[_Response(payload="busy")] * 10),
                 count),
                ("a number as the count", FakeGhs(counts=[_Response(payload=3)] * 10), count)):
            with self.subTest(response=name):
                self._falls_back(ghs, pointer, reason)

    def test_a_stalled_language_that_the_previous_sampling_frame_did_not_keep_is_no_failure(self):
        """The previous sampling frame kept Python, and the crawler stopped for Shell, a
        language that the filter does not keep. The window ends on Python's crawl date, and the
        warning names Shell. The download falls back when the previous sampling frame kept
        Shell. It also falls back without a sampling frame when Shell has the most repositories
        of the GHS search result, because the filter would then keep Shell."""
        self._earlier()
        self.frame_kept(["Python"])
        stalled = {"Python": "2026-09-22", "Shell": "2026-08-01"}
        code, err = self.run_with(FakeGhs(crawl_dates=stalled))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-09-22", "crawl_date:Python"))
        self.assertIn("WARNING ghs_download: The crawl dates of Shell 2026-08-01 are more than "
                      "30 days old. Left these languages out of the end date of the sampling "
                      "window, because the previous sampling frame kept none of them.\n", err)
        self.assertIn("INFO ghs_download: The sampling window ends on 2026-09-22, the crawl date "
                      "of Python, the oldest of GHS's languages apart from those left out above.",
                      err)
        for frame in (["Python", "Shell"], None):
            with self.subTest(frame=frame):
                if frame is None:
                    self.paths.sampling_frame.unlink()
                else:
                    self.frame_kept(frame)
                code, err = self.run_with(FakeGhs(crawl_dates=stalled, languages={"Shell": 3}))
                self.assertEqual((code, self.report()["outcome"]), (0, "fallback"), err)
                self.assertIn("is 65 days old", self.report()["reason"])

    def test_a_stalled_language_that_the_filter_would_now_keep_is_named_in_a_warning(self):
        """The previous sampling frame kept Python and not Shell, and the crawler stopped for
        Shell. The filter keeps the most common languages of each retrieval, and in the new
        GHS search result Shell has the most repositories under a license that the filter keeps. The
        filter leaves Shell out, because the end date does not cover it. The previous
        sampling frame lacks Shell as well. A fallback would therefore add no row and only keep
        the window from moving. A warning names Shell, and the retrieval is taken. When the
        filter's license step removes Shell's repositories, the filter would not keep Shell,
        and no warning names it."""
        if is_stub("yaml"):
            self.skipTest("yaml is not installed")
        top = _mod.yaml.safe_load(_mod.FILTER_CONFIG.read_text(encoding="utf-8"))[
            "top_n_languages"]
        self._earlier()
        self.frame_kept(["Python"])
        stalled = {"Python": "2026-09-22", "Shell": "2026-08-01"}
        code, err = self.run_with(FakeGhs(items=3, crawl_dates=stalled,
                                          languages={"Shell": 2, "Python": 1}))
        self.assertEqual((code, self.report()["outcome"]), (0, "retrieved"), err)
        self.assertIn(f"WARNING ghs_download: The crawl date of Shell, 2026-08-01, is 65 days "
                      f"old, more than the 30 days that the config allows. Shell is among the "
                      f"{top} languages that the filter would keep (the most common in the GHS "
                      f"search result under a license that it keeps). The filter will leave Shell "
                      f"out of the sampling frame, as it did from the previous one. Took the "
                      f"retrieval.\n",
                      err)
        ghs = FakeGhs(items=3, crawl_dates=stalled, languages={"Shell": 2, "Python": 1})
        for item in ghs.items:
            if item["mainLanguage"] == "Shell":
                item["license"] = "Other"
        code, err = self.run_with(ghs)
        self.assertEqual((code, self.report()["outcome"]), (0, "retrieved"), err)
        self.assertIn("INFO ghs_download: Left Shell out of the end date of the sampling window. "
                      "Shell is not among the", err)
        self.assertNotIn("will leave Shell out", err)

    def test_a_language_without_a_crawl_date_that_the_sampling_frame_did_not_keep_is_left_out(
            self):
        """GHS's search fails for Smalltalk in every attempt and round. The previous
        sampling frame kept Python, and the filter removes every row of Smalltalk. The
        retrieval is taken with Python's crawl date as the end date, and a warning names
        Smalltalk. With a previous sampling frame that kept Smalltalk, the download falls
        back."""
        dates = {"Python": "2026-10-02", "Smalltalk": "2026-10-01"}
        settings = _settings(probe_round_wait_minutes=(1, 2))
        self.frame_kept(["Python"])
        ghs = FakeGhs(crawl_dates=dates,
                      crawl_failures={"Smalltalk": [_Response(500) for _ in range(100)]})
        code, err = self.run_with(ghs, settings)
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-10-02", "crawl_date:Python"))
        self.assertEqual(json.loads(row["crawl_dates"]), {"Python": "2026-10-02",
                                                          "Smalltalk": None})
        self.assertIn("WARNING ghs_download: GHS returned no crawl date for Smalltalk after every "
                      "attempt and round. All 2 attempts at the crawl dates failed", err)
        self.assertIn("Left them out of the end date of the sampling window, because the "
                      "previous sampling frame kept none of them.\n", err)
        searched = [params["language"] for path, params in ghs.calls
                    if path == "/r/search" and "language" in params]
        self.assertEqual(searched.count("Smalltalk"), 2 * 3)
        self.frame_kept(["Python", "Smalltalk"])
        code, err = self.run_with(FakeGhs(crawl_dates=dates, crawl_failures={
            "Smalltalk": [_Response(500) for _ in range(100)]}), settings)
        self.assertEqual((code, self.report()["outcome"]), (0, "fallback"), err)
        self.assertIn("GHS returned no crawl date for 1 of 2 languages (Smalltalk)",
                      self.report()["reason"])

    def test_languages_whose_search_fails_every_time_leave_the_kept_ones_their_crawl_dates(self):
        """The previous sampling frame kept C and Python. GHS lists Ada, Agda and Alloy before
        them, and its search returns HTTP 500 for each of the three every time. The first
        attempt ends after the three failures. Each later attempt first requests the languages
        that the earlier attempts requested least, and C and Python get their crawl dates. After
        the last round, the end date leaves Ada, Agda and Alloy out."""
        self.frame_kept(["C", "Python"])
        dates = {"Ada": "2026-10-01", "Agda": "2026-10-01", "Alloy": "2026-10-01",
                 "C": "2026-10-01", "Python": "2026-10-02"}
        ghs = FakeGhs(items=30, languages={"Python": 20, "C": 10}, crawl_dates=dates,
                      crawl_failures={name: [_Response(500) for _ in range(100)]
                                      for name in ("Ada", "Agda", "Alloy")})
        code, err = self.run_with(ghs, _settings(probe_round_wait_minutes=(1, 2)))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-10-01", "crawl_date:C"))
        searched = [params["language"] for path, params in ghs.calls
                    if path == "/r/search" and "language" in params]
        self.assertEqual((searched.count("C"), searched.count("Python")), (1, 1))
        self.assertIn("GHS returned no crawl date for Ada, Agda, Alloy after every attempt and "
                      "round", err)

    def test_a_kept_language_whose_search_fails_once_gets_its_crawl_date(self):
        """As in the test above, and the one request for C that the second attempt sends fails.
        C has then been requested before, as Ada, Agda and Alloy have. An order that sorted C
        behind them would end every later attempt after their failures without a request for
        C, and the download would fall back to the earlier retrieval. C is requested again,
        because the languages requested least come first."""
        self._earlier()
        self.frame_kept(["C", "Python"])
        dates = {"Ada": "2026-10-01", "Agda": "2026-10-01", "Alloy": "2026-10-01",
                 "C": "2026-10-01", "Python": "2026-10-02"}
        failures = {name: [_Response(500) for _ in range(100)] for name in ("Ada", "Agda", "Alloy")}
        failures["C"] = [_Response(500)]
        ghs = FakeGhs(items=30, languages={"Python": 20, "C": 10}, crawl_dates=dates,
                      crawl_failures=failures)
        code, err = self.run_with(ghs, _settings(probe_round_wait_minutes=(1, 2)))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date_source"]), ("retrieved", "crawl_date:C"))
        searched = [params["language"] for path, params in ghs.calls
                    if path == "/r/search" and "language" in params]
        self.assertEqual(searched.count("C"), 2)

    def test_a_search_without_a_repository_of_a_kept_language_is_a_failed_request(self):
        """GHS's search for the crawl date of Python, a language that the previous
        sampling frame kept, returns HTTP 200 without a repository. Without a crawl date,
        Python would not hold the end date back, although the sampling frame keeps its rows.
        The search therefore counts as a failed request. The next attempt that finds a
        repository gives the crawl date, and a search that never finds a repository fails the
        retrieval."""
        self._earlier()
        self.frame_kept(["Python"])
        code, err = self.run_with(FakeGhs(crawl_failures={
            "Python": [_Response(payload={"items": []})]}))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-09-22", "crawl_date:Python"))
        self.assertIn("Attempt 1 of 2 failed. GHS returned no crawl date for 1 of 2 languages "
                      "(Python). In the last failed request, GHS found no repository for the "
                      "crawl date of Python. Trying again in", err)
        code, err = self.run_with(FakeGhs(crawl_failures={
            "Python": [_Response(payload={"items": []}) for _ in range(20)]}),
            _settings(probe_round_wait_minutes=(1,)))
        self.assertEqual((code, self.report()["outcome"]), (0, "fallback"), err)
        self.assertIn("GHS found no repository for the crawl date of Python",
                      self.report()["reason"])

    def test_a_search_without_a_repository_of_another_language_leaves_it_out_with_a_warning(self):
        """GHS's search for the crawl date of Shell, which the previous sampling frame did not
        keep, returns HTTP 200 without a repository. The filter removes every row of Shell. The
        end date leaves Shell out with a warning, and Shell is not requested again."""
        self.frame_kept(["Python"])
        ghs = FakeGhs(crawl_dates={"Python": "2026-09-22", "Shell": "2026-09-20"},
                      crawl_failures={"Shell": [_Response(payload={"items": []})]})
        code, err = self.run_with(ghs)
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-09-22", "crawl_date:Python"))
        self.assertIn("WARNING ghs_download: GHS found no repository for the crawl date of Shell. "
                      "Left Shell out of the end date of the sampling window, because the "
                      "previous sampling frame did not keep it.\n", err)
        self.assertEqual([params["language"] for path, params in ghs.calls
                          if path == "/r/search" and "language" in params], ["Python", "Shell"])

    def test_a_retrieval_before_without_its_rows_gives_way_to_the_next_older_one(self):
        """The pointer names a retrieval without its rows. The run falls back to the older
        retrieval and compares the next GHS search result with it, rather than finding no
        earlier retrieval."""
        self._earlier()
        older = self.index()[-1]["retrieval_id"]
        code, _ = self.run_with(FakeGhs(items=1000))
        self.assertEqual(code, 0)
        newer = self.index()[-1]["retrieval_id"]
        for path in self.paths.cache_dir.glob(f"{newer}.ghs.jsonl.xz*"):
            path.unlink()
        code, err = self.run_with(FakeGhs(search_results=[_Response(503)] * 3))
        self.assertEqual(code, 0, err)
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": older})
        self.assertEqual((self.index()[-1]["outcome"], self.index()[-1]["fallback"]),
                         ("failed", older))
        self.assertIn(f"The cache of retrievals lacks the rows of {newer}. Took retrieval {older} "
                      f"as the previous retrieval.\n", err)
        code, err = self.run_with(FakeGhs(items=1))
        self.assertEqual((code, self.report()["outcome"], self.report()["retrieval_id"]),
                         (0, "fallback", older), err)
        self.assertIn(f"fewer than 90% of the 2 of the previous retrieval, {older}",
                      self.report()["reason"])

    def test_a_certificate_that_does_not_verify_is_not_retried(self):
        if is_stub("requests"):
            self.skipTest("requests is not installed")
        self._earlier()
        failure = _tls_failure(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
               "issuer certificate"))
        code, err = self.run_with(FakeGhs(versions=[failure]))
        self.assertEqual(code, 0)
        self.assertIn("lets-encrypt-intermediates.pem", self.report()["reason"])
        self.assertEqual(self.sleeps, [])

    def test_a_tls_connection_dropped_in_the_handshake_is_retried(self):
        """`requests` raises the same `SSLError` for a connection that the server or the network
        dropped in the handshake as for a certificate that does not verify. The dropped
        connection gets another attempt. With a cache that has no earlier retrieval, the run
        takes the retrieval rather than ending."""
        if is_stub("requests"):
            self.skipTest("requests is not installed")
        failure = _tls_failure(ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))
        code, err = self.run_with(FakeGhs(versions=[failure]))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")
        self.assertIn("Attempt 1 of 2 failed. The request for the version failed", err)


class TestAFirstDownload(_RunCase):
    """As on the first Start on an empty data root, no sampling frame names the languages that
    the filter keeps, and the cache has no retrieval to fall back to. With a filter config that
    keeps the two most common primary languages, the GHS search result shows which languages
    the filter would keep."""

    LANGUAGES = {"Python": 3, "Go": 2, "Smalltalk": 1}

    def setUp(self):
        super().setUp()
        if is_stub("yaml"):
            self.skipTest("yaml is not installed")
        self.paths.filter_config = self.tmp / "ghs_filter.yaml"
        self.paths.filter_config.write_text("filter_languages: true\ntop_n_languages: 2\n")

    def test_a_stalled_language_that_the_filter_would_not_keep_is_left_out(self):
        """The crawler stopped for Smalltalk, which has the fewest repositories of the GHS
        search result. The window ends on Python's crawl date, and the warning names Smalltalk.
        A filter config that keeps every language keeps Smalltalk too, and the download fails."""
        dates = {"Python": "2026-09-22", "Go": "2026-09-25", "Smalltalk": "2026-08-01"}
        code, err = self.run_with(FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-09-22", "crawl_date:Python"))
        self.assertIn("WARNING ghs_download: The crawl date of Smalltalk, 2026-08-01, is 65 days "
                      "old, more than the 30 days that the config allows. Left Smalltalk out of "
                      "the end date of the sampling window (no list of the languages that the "
                      "previous sampling frame kept). The retrieval fails after the download of "
                      "the GHS search result if the filter would keep it.\n", err)
        self.assertIn("INFO ghs_download: Left Smalltalk out of the end date of the sampling "
                      "window. Smalltalk is not among the 2 languages that the filter would keep "
                      "(the most common in the GHS search result under a license that it "
                      "keeps).\n", err)
        self.setUp()
        self.paths.filter_config.write_text("filter_languages: false\n")
        code, err = self.run_with(FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates))
        self.assertEqual(code, 1, err)
        self.assertIn("Every language counts as one that the filter would keep (no number of "
                      "languages in the filter's config)", self.index()[-1]["error"])

    def test_a_stalled_language_that_the_filter_would_keep_fails_the_download(self):
        """The crawler stopped for Python, which has the most repositories of the GHS search
        result. GHS therefore lacks the latest pushes of rows that the filter would keep. With no
        retrieval to fall back to, the download exits 1 without a pointer, and the next Start
        tries again."""
        dates = {"Python": "2026-08-01", "Go": "2026-09-25", "Smalltalk": "2026-09-22"}
        code, err = self.run_with(FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates))
        self.assertEqual(code, 1, err)
        failed = self.index()[-1]
        self.assertEqual((failed["outcome"], failed["end_date"]), ("failed", "2026-09-22"))
        self.assertEqual(failed["error"], "the crawl date of Python, 2026-08-01, is 65 days old, "
                                          "more than the 30 days that the config allows. Python "
                                          "is among the 2 languages that the filter would keep "
                                          "(the most common in the GHS search result under a "
                                          "license that it keeps)")
        self.assertIn("Stopped without a fallback, because the cache of retrievals has no earlier "
                      "retrieval.\n", err)
        self.assertFalse(self.paths.pointer.exists())

    def test_a_language_without_a_crawl_date_fails_the_download_only_if_the_filter_keeps_it(self):
        """GHS's search fails in every attempt and round for one language. For Smalltalk, which
        has the fewest repositories of the GHS search result, the retrieval is taken without its
        crawl date. For Python, which has the most, the download fails."""
        dates = {"Python": "2026-09-22", "Go": "2026-09-25", "Smalltalk": "2026-09-20"}
        settings = _settings(probe_round_wait_minutes=(1,))
        for language, outcome in (("Smalltalk", "retrieved"), ("Python", "failed")):
            with self.subTest(language=language):
                self.setUp()
                ghs = FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates,
                              crawl_failures={language: [_Response(500) for _ in range(100)]})
                code, err = self.run_with(ghs, settings)
                self.assertEqual((code, self.index()[-1]["outcome"]),
                                 (0 if outcome == "retrieved" else 1, outcome), err)
                self.assertIn(f"GHS returned no crawl date for {language}. Left {language} out "
                              f"of the end date of the sampling window (no list of the languages "
                              f"that the previous sampling frame kept)", err)
                if outcome == "retrieved":
                    self.assertEqual(self.index()[-1]["end_date"], "2026-09-22")
                else:
                    self.assertIn("GHS returned no crawl date for Python. Python is among the 2 "
                                  "languages", self.index()[-1]["error"])

    def test_languages_without_a_repository_ahead_of_the_others_are_left_to_the_rows(self):
        """GHS lists Ada, Agda and Alloy before the other languages, and its search returns
        HTTP 200 without a repository for each of the three every time. The first attempt
        requests every language, because a response with HTTP 200 shows that the search works.
        Without a sampling frame, the three languages get no crawl date after the attempts of
        the first round, without the waits of the later rounds. The rows of the GHS search
        result show that the filter keeps none of them. The run waits less than the first round
        wait of the shipped config, and the retrieval is written."""
        settings = _mod.load_settings(CONFIG)
        empty = ("Ada", "Agda", "Alloy")
        dates = {**dict.fromkeys(empty, "2026-10-01"), "Go": "2026-09-25",
                 "Python": "2026-09-22", "Smalltalk": "2026-09-30"}
        ghs = FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates,
                      crawl_failures={name: [_Response(payload={"items": []}) for _ in range(100)]
                                      for name in empty})
        code, err = self.run_with(ghs, settings)
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-09-22", "crawl_date:Python"))
        self.assertEqual(json.loads(row["crawl_dates"]),
                         {**dict.fromkeys(empty), "Go": "2026-09-25", "Python": "2026-09-22",
                          "Smalltalk": "2026-09-30"})
        searched = [params["language"] for path, params in ghs.calls
                    if path == "/r/search" and "language" in params]
        self.assertEqual([searched.count(name) for name in ("Go", "Python", "Smalltalk")],
                         [1, 1, 1])
        # Each attempt requested every language that it lacked, the first attempt included.
        failed = [attempt["error"] for attempt in json.loads(row["attempts"])
                  if attempt["what"] == "the crawl dates" and attempt["error"]]
        self.assertEqual(failed, ["GHS returned no crawl date for 3 of 6 languages (Ada, Agda, "
                                  "Alloy). In the last failed request, GHS found no repository "
                                  "for the crawl date of Alloy"] * settings.probe_attempts)
        self.assertLess(sum(self.sleeps), 60 * settings.probe_round_wait_minutes[0])
        self.assertIn("INFO ghs_download: Left Ada, Agda, Alloy out of the end date of the "
                      "sampling window. None of them is among the 2 languages that the filter "
                      "would keep", err)

    def test_the_filters_top_languages_decides_which_languages_it_would_keep(self):
        """The filter's step argument `--top-languages 2` keeps two languages where the shipped
        filter's config keeps more. The sampling stage passes the argument to the download
        (`run_sampling.share_top_languages`). GHS returns no crawl date for Smalltalk, the third
        most common language of the GHS search result. The missing crawl date fails the download
        under the config's number of languages and not under the argument's."""
        dates = {"Python": "2026-09-22", "Go": "2026-09-25", "Smalltalk": "2026-09-20"}
        shipped = _mod.yaml.safe_load(_mod.FILTER_CONFIG.read_text(encoding="utf-8"))
        self.assertGreaterEqual(shipped["top_n_languages"], 3)
        for argv, code in (([], 1), (["--top-languages", "2"], 0)):
            with self.subTest(argv=argv):
                self.setUp()
                ghs = FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates,
                              crawl_failures={"Smalltalk": [_Response(500) for _ in range(100)]})
                err = io.StringIO()
                with mock.patch.object(_mod.requests, "get", ghs.get), \
                        contextlib.redirect_stderr(err):
                    exit_code = _mod.main(["--cache-dir", str(self.paths.cache_dir),
                                           "--pointer", str(self.paths.pointer),
                                           "--report", str(self.paths.report),
                                           "--sampling-frame", str(self.paths.sampling_frame),
                                           *argv])
                self.assertEqual(exit_code, code, err.getvalue())
                if code:
                    self.assertIn(f"GHS returned no crawl date for Smalltalk. Smalltalk is among "
                                  f"the {shipped['top_n_languages']} languages",
                                  self.index()[-1]["error"])
                else:
                    self.assertEqual(self.index()[-1]["outcome"], "retrieved")

    def test_the_shipped_filter_config_limits_the_languages_that_the_filter_keeps(self):
        """The download reads the filter's config beside the scripts, and that config keeps a
        fixed number of languages rather than every language."""
        shipped = _mod.Paths().filter_config
        self.assertEqual(shipped, CONFIG.parent / "ghs_filter.yaml")
        self.assertIsInstance(_mod.filter_top_languages(shipped), int)


class TestAMonthlyDownload(_RunCase):
    """As at the Start of a monthly run, the previous sampling frame names the languages that
    the filter kept, and the cache has an earlier retrieval to fall back to. The filter's config
    keeps the three most common primary languages. It switches off the missing data step and
    the activity step, which read counts that the rows of the fake GHS lack."""

    LANGUAGES = {"Python": 3, "Go": 2, "Smalltalk": 1}

    def setUp(self):
        super().setUp()
        if is_stub("yaml"):
            self.skipTest("yaml is not installed")
        self.paths.filter_config = self.tmp / "ghs_filter.yaml"
        self.paths.filter_config.write_text("top_n_languages: 3\nfilter_missing_data: false\n"
                                            "filter_activity: false\n")

    def query_github(self, rid: str) -> None:
        """Write GitHub's responses to the rows of retrieval `rid` into the cache as
        2_query_github.py writes them. Each repository is found, with GHS's `mainLanguage` as
        its primary language and with GHS's license."""
        row = next(row for row in self.index() if row["retrieval_id"] == rid)
        responses = [{"ghsName": item["name"], "status": "found", "repository": {
            "id": 1000 + item["id"], "name": item["name"], "createdAt": "2020-01-01T00:00:00Z",
            "pushedAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z",
            "isArchived": False, "isDisabled": False, "isLocked": False, "isFork": False,
            "isPrivate": False, "isMirror": False, "license": item["license"],
            "primaryLanguage": item.get("mainLanguage"), "stargazers": 5,
            "defaultBranch": "main", "headSha": f"{item['id']:040x}",
            "headCommittedAt": "2026-09-30T00:00:00Z", "description": "A tool."}}
            for item in ghs_cache.read_rows(self.paths.cache_dir, rid, "ghs",
                                            row["ghs_file_sha256"])]
        _, sha = ghs_cache.write_rows(self.paths.cache_dir, rid, "github", responses)
        ghs_cache.put_index_row({"retrieval_id": rid, "github_file_sha256": sha,
                                 "github_found": len(responses), "github_not_found": 0,
                                 "github_unresolved": 0,
                                 "github_started_at": "2026-10-05T13:00:00Z",
                                 "github_finished_at": "2026-10-05T13:20:00Z"},
                                self.paths.cache_dir)

    def test_a_stalled_language_that_the_filter_would_keep_is_left_to_the_filter(self):
        """The previous sampling frame kept Go and Python, and the crawler stopped for
        Smalltalk. The GHS search result ranks Smalltalk third, among the three languages that
        the filter keeps. The filter leaves Smalltalk out, because the end date does not cover
        it, and the previous sampling frame lacks Smalltalk as well. A fallback would therefore
        add no row and would keep the window from moving every month while the crawler stays
        stopped. The download takes the retrieval and names Smalltalk in a warning. The filter,
        run over the retrieval, keeps Go and Python and records Smalltalk under `left_out`."""
        code, err = self.run_with(FakeGhs(items=2))
        self.assertEqual(code, 0, err)
        earlier = self.index()[-1]["retrieval_id"]
        self.frame_kept(["Go", "Python"])
        dates = {"Python": "2026-09-22", "Go": "2026-09-25", "Smalltalk": "2026-08-01"}
        code, err = self.run_with(FakeGhs(items=6, languages=self.LANGUAGES, crawl_dates=dates))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        rid = row["retrieval_id"]
        self.assertNotEqual(rid, earlier)
        self.assertEqual((row["outcome"], row["end_date"], row["end_date_source"]),
                         ("retrieved", "2026-09-22", "crawl_date:Python"))
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": rid})
        self.assertEqual((self.report()["outcome"], self.report()["retrieval_id"]),
                         ("retrieved", rid))
        self.assertIn("WARNING ghs_download: The crawl date of Smalltalk, 2026-08-01, is 65 days "
                      "old, more than the 30 days that the config allows. Smalltalk is among the "
                      "3 languages that the filter would keep (the most common in the GHS search "
                      "result under a license that it keeps). The filter will leave Smalltalk out "
                      "of the sampling frame, as it did from the previous one. Took the "
                      "retrieval.\n", err)
        self.query_github(rid)
        script = _mod.filter_script()
        data = self.tmp / "data"
        data.mkdir()
        err = io.StringIO()
        with mock.patch.object(script, "SAMPLING_DIR", data), contextlib.redirect_stderr(err):
            code = script.main(["--config", str(self.paths.filter_config),
                                "--cache-dir", str(self.paths.cache_dir),
                                "--pointer", str(self.paths.pointer),
                                "--output", str(self.paths.sampling_frame)])
        self.assertEqual(code, 0, err.getvalue())
        sampling_frame = json.loads(self.paths.sampling_frame.read_text(encoding="utf-8"))
        step = next(stage for stage in sampling_frame["_metadata"]["stages"]
                    if stage["key"] == "languages")
        self.assertEqual(step["values"], {"top": 3, "languages": ["Go", "Python"],
                                          "left_out": {"Smalltalk": "2026-08-01"}})
        self.assertEqual(sorted(item["mainLanguage"] for item in sampling_frame["items"]),
                         ["Go", "Go", "Python", "Python", "Python"])


class TestAnUnreadablePointer(_RunCase):
    """A pointer that a power loss left empty or cut, or that names a retrieval missing from the
    index, is no reason to stop, because the download writes the pointer again."""

    def _earlier(self):
        code, _ = self.run_with(FakeGhs(items=2))
        self.assertEqual(code, 0)
        return self.index()[-1]["retrieval_id"]

    def test_a_retrieval_after_an_empty_pointer_names_the_new_retrieval(self):
        self._earlier()
        self.paths.pointer.write_text("")
        code, err = self.run_with(FakeGhs())
        self.assertEqual(code, 0, err)
        rid = self.index()[-1]["retrieval_id"]
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": rid})
        self.assertIn(f"WARNING ghs_download: The pointer {self.paths.pointer} cannot be read as "
                      f"JSON", err)
        self.assertIn(". Ignored the pointer until it is written again.\n", err)

    def test_a_failed_retrieval_after_a_cut_pointer_falls_back_to_the_latest_retrieval(self):
        earlier = self._earlier()
        self.paths.pointer.write_text('{"retrieval_id": "2026-')
        code, err = self.run_with(FakeGhs(search_results=[_Response(503)] * 3))
        self.assertEqual(code, 0, err)
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": earlier})
        self.assertEqual((self.index()[-1]["outcome"], self.index()[-1]["fallback"]),
                         ("failed", earlier))
        self.assertEqual(self.report()["outcome"], "fallback")

    def test_a_pointer_to_a_retrieval_that_the_index_lacks_counts_as_no_pointer(self):
        earlier = self._earlier()
        ghs_cache.write_pointer("2026-01-01T000000Z", self.paths.pointer)
        code, err = self.run_with(FakeGhs(search_results=[_Response(503)] * 3))
        self.assertEqual(code, 0, err)
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": earlier})
        self.assertIn(f"Retrieval 2026-01-01T000000Z in {self.paths.pointer} is not in the index "
                      f"of {self.paths.cache_dir}. Ignored the pointer until it is written "
                      f"again.\n", err)

    def test_the_main_command_after_an_empty_pointer_exits_0(self):
        """The command that the runner starts exits 0 after a power loss left the pointer
        empty."""
        if is_stub("yaml"):
            self.skipTest("yaml is not installed")
        self._earlier()
        self.paths.pointer.write_text("")
        err = io.StringIO()
        with mock.patch.object(_mod.requests, "get", FakeGhs().get), \
                contextlib.redirect_stderr(err):
            code = _mod.main(["--cache-dir", str(self.paths.cache_dir),
                              "--pointer", str(self.paths.pointer),
                              "--report", str(self.paths.report),
                              "--sampling-frame", str(self.paths.sampling_frame)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer),
                         {"retrieval_id": self.index()[-1]["retrieval_id"]})


class TestASmallerRetrieval(_RunCase):
    """A retrieval of the same query with far fewer rows than the previous retrieval, overall or
    in a language, counts as a failed download. GHS has then lost rows, and the collection would
    delete the trees of every repository that left the sample."""

    def _earlier(self, items, languages=None):
        code, err = self.run_with(FakeGhs(items=items, languages=languages))
        self.assertEqual(code, 0, err)
        return self.index()[-1]["retrieval_id"]

    def test_a_retrieval_with_far_fewer_rows_falls_back_to_the_previous_retrieval(self):
        earlier = self._earlier(1000)
        pointer = self.paths.pointer.read_bytes()
        code, err = self.run_with(FakeGhs(items=600))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.paths.pointer.read_bytes(), pointer)
        failed = self.index()[-1]
        self.assertEqual((failed["outcome"], failed["fallback"], failed["count_after"]),
                         ("failed", earlier, "600"))
        reason = (f"the GHS search result has 600 repositories, fewer than 90% of the 1,000 of "
                  f"the previous retrieval, {earlier}, for the same query")
        self.assertEqual(failed["error"], reason)
        self.assertEqual((self.report()["outcome"], self.report()["retrieval_id"],
                          self.report()["reason"]), ("fallback", earlier, reason))
        self.assertIn(f"WARNING ghs_download: Retrieval {failed['retrieval_id']} failed. "
                      f"{reason[:1].upper()}{reason[1:]}. Fell back to the previous retrieval, "
                      f"{earlier}, and its sampling window.\n", err)
        self.assertFalse(ghs_cache.has_rows(self.paths.cache_dir, failed["retrieval_id"], "ghs"))

    def test_a_retrieval_slightly_smaller_than_the_previous_one_is_used(self):
        self._earlier(1000)
        code, err = self.run_with(FakeGhs(items=950))
        self.assertEqual(code, 0, err)
        row = self.index()[-1]
        self.assertEqual((row["outcome"], row["item_count"]), ("retrieved", "950"))
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer),
                         {"retrieval_id": row["retrieval_id"]})

    def test_a_language_with_far_fewer_rows_falls_back(self):
        """The total stays above its share. Python has at least the rows from which a language
        is compared, and it falls below the share of a language."""
        earlier = self._earlier(10_000, {"Python": 2000, "Go": 8000})
        code, err = self.run_with(FakeGhs(items=9400, languages={"Python": 1400, "Go": 8000}))
        self.assertEqual(code, 0, err)
        failed = self.index()[-1]
        self.assertEqual((failed["outcome"], failed["fallback"]), ("failed", earlier))
        self.assertEqual(failed["error"],
                         f"the GHS search result has fewer than 75% of the repositories of the "
                         f"previous retrieval, {earlier}, for the same query, in these languages "
                         f"with at least 1,000 there (Python 1,400 of 2,000)")
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer), {"retrieval_id": earlier})

    def test_a_language_slightly_smaller_or_with_few_rows_is_used(self):
        """Python stays above the share of a language. Elm falls to no rows, but it had fewer
        rows than a language needs to be compared."""
        self._earlier(10_000, {"Python": 2000, "Elm": 900, "Go": 7100})
        code, err = self.run_with(FakeGhs(items=9900, languages={"Python": 1600, "Go": 7100}))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")

    def test_a_language_that_the_previous_sampling_frame_did_not_keep_is_not_compared(self):
        """The filter removes every row of a language that it does not keep. A loss of such rows
        leaves the sampling frame as it is, as when the crawler stopped for Shell and Shell's
        rows leave the moving window. The previous sampling frame kept Python, and Python kept
        its rows. Shell fell from 1,000 rows to 200, and the total stayed above its share.
        Without a sampling frame, or with a sampling frame that kept Shell, Shell is compared,
        and the retrieval falls back."""
        before, after = {"Python": 12_000, "Shell": 1000}, {"Python": 12_000, "Shell": 200}
        self._earlier(13_000, before)
        self.frame_kept(["Python"])
        code, err = self.run_with(FakeGhs(items=12_200, languages=after))
        self.assertEqual(code, 0, err)
        self.assertEqual((self.index()[-1]["outcome"], self.index()[-1]["item_count"]),
                         ("retrieved", "12200"))
        for frame, kept in ((None, ""), (["Python", "Shell"], ", each kept in the previous "
                                                              "sampling frame")):
            with self.subTest(frame=frame):
                if frame is None:
                    self.paths.sampling_frame.unlink()
                else:
                    self.frame_kept(frame)
                earlier = self._earlier(13_000, before)
                code, err = self.run_with(FakeGhs(items=12_200, languages=after))
                self.assertEqual(code, 0, err)
                failed = self.index()[-1]
                self.assertEqual((failed["outcome"], failed["fallback"]), ("failed", earlier))
                self.assertIn(f"in these languages with at least 1,000 there{kept} (Shell 200 of "
                              f"1,000)", failed["error"])

    def test_with_on_failure_fail_a_far_smaller_retrieval_ends_the_run(self):
        self._earlier(1000)
        code, err = self.run_with(FakeGhs(items=600), _settings(on_failure="fail"))
        self.assertEqual(code, 1)
        self.assertIn("fewer than 90% of the 1,000", err)
        self.assertIn("on_failure to fail", err)

    def test_a_retrieval_of_another_query_is_not_compared(self):
        """A query that the config narrowed matches fewer repositories by design."""
        self._earlier(1000)
        narrower = _settings(query={"contributorsMin": 5, "pullsMin": 1, "excludeForks": True,
                                    "hasLicense": True})
        code, err = self.run_with(FakeGhs(items=600), narrower)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")

    def test_a_change_of_the_window_lengths_is_not_compared(self):
        """A window of fewer months covers fewer repositories by design, as a narrower query
        does, though the parameters outside the window stay the same."""
        for lengths in ({"committed_within_months": 3}, {"created_before_months": 24}):
            with self.subTest(lengths=lengths):
                self._earlier(1000)
                code, err = self.run_with(FakeGhs(items=600), _settings(**lengths))
                self.assertEqual(code, 0, err)
                self.assertEqual((self.index()[-1]["outcome"], self.index()[-1]["item_count"]),
                                 ("retrieved", "600"))

    def test_an_echo_with_another_parameter_is_still_the_same_query(self):
        """A GHS upgrade can add a parameter to the `parameters` block that the GHS search
        result echoes. The GHS search result is compared, because the query sent is the same."""
        earlier = self._earlier(1000)
        code, err = self.run_with(FakeGhs(items=600, extra_echo={"hasTopics": False}))
        self.assertEqual(code, 0, err)
        failed = self.index()[-1]
        self.assertEqual((failed["outcome"], failed["fallback"]), ("failed", earlier))

    def test_fallbacks_in_a_row_are_counted_with_the_rows_of_each_refused_search_result(self):
        """While GHS stays smaller, every later download falls back to the same retrieval. The
        report counts the downloads in a row that fell back, and the index and the report
        record the rows of the refused GHS search result."""
        earlier = self._earlier(1000)
        for number in (1, 2, 3):
            code, err = self.run_with(FakeGhs(items=600))
            self.assertEqual(code, 0, err)
            report = self.report()
            self.assertEqual((report["outcome"], report["retrieval_id"],
                              report["fallbacks_in_a_row"], report["failed_item_count"]),
                             ("fallback", earlier, number, 600))
            failed = self.index()[-1]
            self.assertEqual((failed["outcome"], failed["item_count"], failed["ghs_file_sha256"]),
                             ("failed", "600", ""))
        self.assertIn("previous retrieval, " + earlier + ", and its sampling window. The last 3 "
                      "downloads fell back.\n", err)

    def test_accept_smaller_takes_a_smaller_retrieval_once(self):
        """After GHS lost rows for good, `--accept-smaller` takes the smaller retrieval, and the
        next download is compared with that retrieval."""
        self._earlier(1000)
        code, _ = self.run_with(FakeGhs(items=600))
        self.assertEqual((code, self.report()["outcome"]), (0, "fallback"))
        code, err = self.run_with(FakeGhs(items=600), accept_smaller=True)
        self.assertEqual(code, 0, err)
        taken = self.index()[-1]
        self.assertEqual((taken["outcome"], taken["item_count"]), ("retrieved", "600"))
        self.assertEqual(ghs_cache.read_pointer(self.paths.pointer),
                         {"retrieval_id": taken["retrieval_id"]})
        self.assertEqual(self.report()["outcome"], "retrieved")
        self.assertIn("fewer than 90% of the 1,000", self.report()["accepted_smaller"])
        self.assertIn(". Took it under --accept-smaller.\n", err)
        code, err = self.run_with(FakeGhs(items=590))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.index()[-1]["outcome"], "retrieved")
        self.assertNotIn("accepted_smaller", self.report())
        code, err = self.run_with(FakeGhs(items=300))
        self.assertEqual(self.report()["fallbacks_in_a_row"], 1)


class TestWhenGhsRefuses(_RunCase):

    def test_a_refused_query_ends_the_run_without_falling_back(self):
        code, _ = self.run_with(FakeGhs(items=2))
        self.assertEqual(code, 0)
        code, err = self.run_with(FakeGhs(counts=[_Response(400)]))
        self.assertEqual(code, 1)
        self.assertIn("failed. GHS rejected the request for the count with HTTP 400. Correct the "
                      "query in the config.\n", err)
        self.assertEqual(self.index()[-1]["fallback"], "")

    def test_a_search_result_that_does_not_echo_the_query_is_refused(self):
        code, err = self.run_with(FakeGhs(echo={"contributorsMin": 2}))
        self.assertEqual(code, 1)
        self.assertIn("failed. The `parameters` of the GHS search result differ from the query",
                      err)
        self.assertIn("pullsMin=1 echoed as None", err)
        self.assertFalse(self.paths.pointer.exists())


class TestADryRunThatFails(_RunCase):
    """A dry run that a failed request or check stops exits 1 and writes nothing. A download in
    its place would record a failed retrieval in the index, and the fallback would rewrite the
    pointer and the report and exit 0. A dry run takes no retrieval, and the cache of retrievals
    of the checkout is tracked."""

    def setUp(self):
        super().setUp()
        code, err = self.run_with(FakeGhs(items=2))
        self.assertEqual(code, 0, err)

    def files(self) -> dict:
        """Every file below the temporary directory, by its path, with its bytes."""
        return {path: path.read_bytes() for path in sorted(self.tmp.rglob("*")) if path.is_file()}

    def _fails_without_writing(self, ghs, reason: str) -> None:
        """Run a dry run against `ghs` and check that it exits 1, logs `reason` as the cause and
        leaves every file as it was."""
        before = self.files()
        code, err = self.run_with(ghs, dry_run=True)
        self.assertEqual(code, 1, err)
        self.assertIn(f"ERROR ghs_download: Dry run failed, nothing written. {reason}", err)
        self.assertNotIn("Fell back", err)
        self.assertEqual(self.files(), before)

    def test_a_ghs_that_returns_no_response(self):
        self._fails_without_writing(FakeGhs(versions=[_Response(503)] * 2),
                                    "All 2 attempts at the version failed")

    def test_a_crawl_date_of_a_kept_language_beyond_the_limit(self):
        """The crawl date of Python, which the previous sampling frame kept, is 65 days old on
        the day of the run, 2026-10-05, and the config allows 30."""
        self.frame_kept(["Python"])
        self._fails_without_writing(
            FakeGhs(crawl_dates={"Python": "2026-08-01", "Shell": "2026-10-02"}),
            "The crawl date of Python (kept in the previous sampling frame), 2026-08-01, is 65 "
            "days old, more than the 30 days that the config allows.\n")

    def test_a_certificate_that_does_not_verify(self):
        if is_stub("requests"):
            self.skipTest("requests is not installed")
        failure = _tls_failure(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local "
               "issuer certificate"))
        self._fails_without_writing(FakeGhs(versions=[failure]), "The certificate of ")

    def test_a_refused_query(self):
        self._fails_without_writing(FakeGhs(counts=[_Response(400)]),
                                    "GHS rejected the request for the count with HTTP 400. "
                                    "Correct the query in the config.\n")


class _SearchResultFile:
    """The file of the GHS search result in the cache, opened on a host that fails to write it.
    Opening creates the file, and the first write raises the error of `code`."""

    def __init__(self, path, code):
        self.code = code
        Path(path).touch()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def write(self, data):
        raise OSError(self.code, os.strerror(self.code))


def _host_fails(code, on_open):
    """Return an `open` for the script that raises the error of `code` for the file of the GHS
    search result, when the file is opened or when it is written. Every other file opens as
    usual."""
    real = open

    def fake(path, mode="r", *args, **kwargs):
        if not str(path).endswith(".json.gz.part"):
            return real(path, mode, *args, **kwargs)
        if on_open:
            raise OSError(code, os.strerror(code), str(path))
        return _SearchResultFile(path, code)

    return fake


class TestWhenThisHostFails(_RunCase):

    def setUp(self):
        super().setUp()
        usage = mock.patch.object(_mod.shutil, "disk_usage",
                                  return_value=mock.Mock(free=5 * 1024 ** 2))
        usage.start()
        self.addCleanup(usage.stop)

    def test_a_file_system_error_while_writing_the_search_result_ends_the_run_and_no_fallback(
            self):
        """A full disk, a quota, a read-only file system or a cache that the run may not write
        is a fault of this host, not a GHS that does not respond. The run exits 1 after one
        attempt and names the file and the free space. It leaves the pointer and the report of
        the earlier retrieval as they are, rather than continuing over the previous
        sampling frame."""
        code, _ = self.run_with(FakeGhs(items=2))
        self.assertEqual(code, 0)
        pointer, report = self.paths.pointer.read_bytes(), self.paths.report.read_bytes()
        for name, on_open in (("ENOSPC", False), ("EDQUOT", False), ("EROFS", True),
                              ("EACCES", True)):
            with self.subTest(error=name):
                ghs = FakeGhs(items=2)
                with mock.patch.object(_mod, "open", _host_fails(getattr(errno, name), on_open),
                                       create=True):
                    code, err = self.run_with(ghs)
                self.assertEqual(code, 1, err)
                self.assertEqual([path for path, _ in ghs.calls].count("/r/download/json"), 1)
                failed = self.index()[-1]
                self.assertEqual((failed["outcome"], failed["fallback"]), ("failed", ""))
                self.assertIn(os.strerror(getattr(errno, name)), failed["error"])
                self.assertEqual(self.paths.pointer.read_bytes(), pointer)
                self.assertEqual(self.paths.report.read_bytes(), report)
                self.assertIn(f"failed. Could not write {self.paths.cache_dir}/", err)
                self.assertIn(f".json.gz.part ([Errno {getattr(errno, name)}] "
                              f"{os.strerror(getattr(errno, name))}", err)
                self.assertIn(f"5 MiB are free in {self.paths.cache_dir}", err)
                self.assertIn("Stopped without a fallback, because the fault is on this host, not "
                              "at GHS.\n", err)
                self.assertEqual(list(self.paths.cache_dir.glob("*.part")), [])

    def test_a_full_disk_while_writing_the_rows_names_the_free_space(self):
        """An error while writing the rows into the cache has no file name. The message names
        the cache of retrievals and its free space instead."""
        if is_stub("yaml"):
            self.skipTest("yaml is not installed")
        err = io.StringIO()
        full = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(_mod.requests, "get", FakeGhs().get), \
                mock.patch.object(ghs_cache, "write_rows", side_effect=full), \
                contextlib.redirect_stderr(err):
            code = _mod.main(["--cache-dir", str(self.paths.cache_dir),
                              "--pointer", str(self.paths.pointer),
                              "--report", str(self.paths.report),
                              "--sampling-frame", str(self.paths.sampling_frame)])
        self.assertEqual(code, 1)
        self.assertIn(f"ERROR ghs_download: Could not write the retrieval ([Errno {errno.ENOSPC}] "
                      f"{os.strerror(errno.ENOSPC)}). 5 MiB are free in {self.paths.cache_dir}",
                      err.getvalue())
        self.assertFalse(self.paths.pointer.exists())


class TestMain(unittest.TestCase):

    def test_a_missing_config_is_reported(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _mod.main(["--config", "/nonexistent/ghs_download.yaml"])
        self.assertEqual(code, 1)
        self.assertIn("ERROR ghs_download: Could not read the config", err.getvalue())

    def test_the_config_defaults_to_the_one_beside_the_script(self):
        """The default is not relative to the working directory, because the step runs in the
        stage's data directory."""
        self.assertEqual(_mod.build_parser().parse_args([]).config,
                         HERE / "config" / "ghs_download.yaml")

    def test_an_abbreviated_option_is_a_usage_error(self):
        """The container's entrypoint recognizes `--accept-smaller` in a `--step-arg` value by
        its full name and passes it until a download has taken a retrieval. If argparse took
        `--accept-sm` for the option, the abbreviation would act at every later Start, and every
        later download would take a retrieval however many rows GHS lost."""
        for words in (["--accept-sm"], ["--end-d", "2026-10-01"]):
            with self.subTest(words=words):
                with contextlib.redirect_stderr(io.StringIO()), \
                        self.assertRaises(SystemExit) as stop:
                    _mod.build_parser().parse_args(words)
                self.assertEqual(stop.exception.code, 2)


class TestTheShippedIntermediates(unittest.TestCase):
    """`certs/lets-encrypt-intermediates.pem`, which the download trusts in addition to the
    trust store."""

    def test_every_certificate_chains_to_a_root_of_the_trust_store(self):
        """A certificate in the file that no trusted root issued would make the download
        trust whatever it signs. An expired intermediate fails the download rather than this
        suite, because the check ignores times."""
        if is_stub("requests") or shutil.which("openssl") is None:
            self.skipTest("requests or openssl is not available")
        text = _mod.CA_INTERMEDIATES.read_text(encoding="utf-8")
        blocks = re.findall(r"-----BEGIN CERTIFICATE-----.+?-----END CERTIFICATE-----", text, re.S)
        self.assertGreaterEqual(len(blocks), 1)
        with tempfile.TemporaryDirectory() as tmp:
            for index, block in enumerate(blocks):
                with self.subTest(certificate=index):
                    path = Path(tmp) / f"{index}.pem"
                    path.write_text(block + "\n", encoding="utf-8")
                    result = subprocess.run(
                        ["openssl", "verify", "-no_check_time",
                         "-CAfile", _mod.requests.certs.where(),
                         "-untrusted", str(_mod.CA_INTERMEDIATES), str(path)],
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
