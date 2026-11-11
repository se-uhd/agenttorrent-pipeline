"""
Tests for 2_query_github.py

Strategy:
  - `_script_loader` loads the script by path and stubs `requests` where it is not
    installed.
  - A run is tested against a fake GitHub. `requests.post` responds to GraphQL batches from a
    map of repositories, with NOT_FOUND and blocked errors as GitHub writes them.
    `requests.get` responds to the check of the GitHub access tokens and to the REST lookup,
    which follows a rename. On demand, the fake fails as GitHub does:
      - HTTP 502 for a batch that took too long
      - HTTP 200 with a timeout error for every batch that contains a slow repository
      - a secondary rate limit, or an exhausted GraphQL rate limit
      - an error for one repository of a batch
      - HTTP 401, and spells in which GitHub rejects every GitHub access token after a
        request that it served
      - an outage of the network until a time of the fake clock, or a refused connection for
        every request
      - a REST response with an HTML page
  - Every write goes to a temporary cache. A fake clock replaces `time.sleep`, and the rounds
    of hours run in milliseconds.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_query_github -v
"""

import contextlib
import io
import json
import math
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _script_loader import load_script  # noqa: E402

_mod = load_script("2_query_github.py")
import ghs_cache  # noqa: E402

RID = "2026-10-05T120000Z"


class _Response:

    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self.payload, self.headers = status, payload, headers or {}

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


def _repository(name, number):
    return {"databaseId": 1000 + number, "nameWithOwner": name, "description": f"Tool {number}.",
            "createdAt": "2023-06-04T16:23:40Z", "pushedAt": "2026-10-01T10:00:00Z",
            "updatedAt": "2026-10-04T10:00:00Z", "isArchived": False, "isDisabled": False,
            "isLocked": False, "isFork": False, "isPrivate": False,
            "isMirror": False, "licenseInfo": {"spdxId": "MIT", "name": "MIT License"},
            "primaryLanguage": {"name": "Rust"}, "stargazerCount": 42,
            "defaultBranchRef": {"name": "main", "target": {
                "oid": f"{number:040x}", "committedDate": "2026-10-01T09:59:00Z"}}}


class FakeGitHub:

    def __init__(self, names, missing=(), renamed=None, blocked=(), posts=None, tos=(),
                 checks=None, down=False):
        self.repos = {n.lower(): _repository(n, i) for i, n in enumerate(names)
                      if n not in missing and n not in blocked and n not in (renamed or {})}
        self.renamed = renamed or {}
        for new in self.renamed.values():
            self.repos[new.lower()] = _repository(new, 9000 + len(self.repos))
        self.blocked = {n.lower() for n in blocked}
        self.posts = list(posts or [])
        # Every GraphQL request after the scripted ones fails with HTTP 500.
        self.down = down
        self.batches = []
        self.rest = []
        # Repositories that get an error in one batch (`flaky`) or in every batch (`broken`),
        # and repositories that GraphQL does not find once (`not_found_once`).
        self.flaky, self.broken, self.not_found_once = set(), set(), set()
        # Maps a repository's name to the number of responses in which GitHub could not
        # resolve its license. Such a response has a null license and an error with a path
        # that names the field.
        self.unresolved_license: dict = {}
        # The same for the repository's name.
        self.unresolved_name: dict = {}
        # Repositories that GitHub took down for a terms-of-service violation, for which REST
        # returns 403.
        self.tos = {n.lower() for n in tos}
        # The responses to the checks of the GitHub access tokens, in order, before the fake
        # returns its default response.
        self.checks = list(checks or [])
        # Until this time of `clock`, every request fails as it does without a network.
        self.clock, self.offline_until = None, 0.0

    def offline(self):
        if self.clock is not None and self.clock() < self.offline_until:
            raise _mod.requests.RequestException("Failed to resolve 'api.github.com'")

    def post(self, url, json=None, headers=None, timeout=None):
        assert url == _mod.GRAPHQL_URL and headers["Authorization"].startswith("bearer ")
        variables = json["variables"]
        names = [f"{variables[f'o{i}']}/{variables[f'n{i}']}" for i in range(len(variables) // 2)]
        self.batches.append(names)
        self.offline()
        if self.posts:
            step = self.posts.pop(0)
            if isinstance(step, _Response):
                return step
        elif self.down:
            return _Response(500)
        data, errors = {"rateLimit": {"cost": 1, "remaining": 4000,
                                      "resetAt": "2026-10-05T13:00:00Z"}}, []
        for i, name in enumerate(names):
            key = name.lower()
            if key in self.flaky or key in self.broken:
                self.flaky.discard(key)
                data[f"r{i}"] = None
                errors.append({"type": "SERVICE_UNAVAILABLE", "path": [f"r{i}"], "message": "x"})
            elif key in self.not_found_once:
                self.not_found_once.discard(key)
                data[f"r{i}"] = None
                errors.append({"type": "NOT_FOUND", "path": [f"r{i}"],
                               "message": f"Could not resolve to a Repository with the name '{name}'."})
            elif self.unresolved_license.get(key, 0) > 0:
                self.unresolved_license[key] -= 1
                data[f"r{i}"] = {**self.repos[key], "licenseInfo": None}
                errors.append({"type": "SERVICE_UNAVAILABLE", "path": [f"r{i}", "licenseInfo"],
                               "message": "Something went wrong while executing your query."})
            elif self.unresolved_name.get(key, 0) > 0:
                self.unresolved_name[key] -= 1
                data[f"r{i}"] = {**self.repos[key], "nameWithOwner": None}
                errors.append({"type": "SERVICE_UNAVAILABLE", "path": [f"r{i}", "nameWithOwner"],
                               "message": "Something went wrong while executing your query."})
            elif key in self.repos:
                data[f"r{i}"] = self.repos[key]
            elif key in self.blocked:
                data[f"r{i}"] = None
                errors.append({"type": "FORBIDDEN", "path": [f"r{i}"],
                               "message": "Repository access blocked"})
            else:
                data[f"r{i}"] = None
                errors.append({"type": "NOT_FOUND", "path": [f"r{i}"],
                               "message": f"Could not resolve to a Repository with the name '{name}'."})
        return _Response(payload={"data": data, "errors": errors})

    def get(self, url, headers=None, timeout=None):
        self.offline()
        if url == _mod.RATE_LIMIT_URL:
            if self.checks:
                step = self.checks.pop(0)
                if isinstance(step, Exception):
                    raise step
                return step
            return _Response(401 if "bad" in headers["Authorization"] else 200, {})
        name = url[len("https://api.github.com/repos/"):]
        self.rest.append(name)
        if name.lower() in self.tos:
            return _Response(403, {"message": "Repository access blocked",
                                   "block": {"reason": "tos"}})
        if name in self.renamed:
            return _Response(payload={"full_name": self.renamed[name]})
        if name.lower() in self.repos:
            return _Response(payload={"full_name": name})
        return _Response(404, {})


class _Spells(FakeGitHub):
    """A GitHub that rejects every GitHub access token in the minute after each request that it
    serves, for requests and for their checks alike. The fake has `count` such spells."""

    def __init__(self, names, count, clock):
        super().__init__(names)
        self.left, self.now, self.until = count, clock, -math.inf

    def rejecting(self):
        return self.now() < self.until

    def post(self, url, json=None, headers=None, timeout=None):
        if self.rejecting():
            return _Response(401, {"message": "Bad credentials"})
        response = super().post(url, json=json, headers=headers, timeout=timeout)
        if self.left:
            self.left -= 1
            self.until = self.now() + 60
        return response

    def get(self, url, headers=None, timeout=None):
        if self.rejecting():
            return _Response(401, {"message": "Bad credentials"})
        return super().get(url, headers=headers, timeout=timeout)


class _QueryCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "ghs_retrievals"
        self.pointer = self.tmp / "ghs_retrieval.json"
        # A fake clock that each sleep moves forward. A pause then ends without real waiting.
        self.sleeps, self.now = [], [1_000_000.0]

        def sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds

        for name, value in (("sleep", sleep), ("time", lambda: self.now[0])):
            patcher = mock.patch.object(_mod.time, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def retrieval(self, names):
        _, sha = ghs_cache.write_rows(self.cache, RID, "ghs",
                                      ({"id": i, "name": n} for i, n in enumerate(names)))
        ghs_cache.put_index_row({"retrieval_id": RID, "outcome": "retrieved",
                                 "ghs_file_sha256": sha}, self.cache)
        ghs_cache.write_pointer(RID, self.pointer)

    def run_with(self, github, tokens="t1,t2", batch_size=100, workers=2):
        err = io.StringIO()
        with mock.patch.object(_mod.requests, "post", github.post), \
                mock.patch.object(_mod.requests, "get", github.get), \
                contextlib.redirect_stderr(err):
            code = _mod.run(self.cache, self.pointer, batch_size, workers,
                            environ={"GITHUB_TOKENS": tokens})
        return code, err.getvalue()

    def responses(self):
        row = ghs_cache.index_row(RID, self.cache)
        return list(ghs_cache.read_rows(self.cache, RID, "github", row["github_file_sha256"]))


class TestTheCommandLine(unittest.TestCase):

    def test_an_abbreviated_option_is_a_usage_error(self):
        """The step takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with mock.patch.object(_mod, "run", return_value=0), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as stop:
            _mod.main(["--batch", "10"])
        self.assertEqual(stop.exception.code, 2)


class TestPieces(unittest.TestCase):

    def test_a_batch_queries_each_repository_under_its_own_variables(self):
        document, variables = _mod.batch_query(["a/b", "c/d.e"])
        self.assertIn("r1: repository(owner: $o1, name: $n1) { ...R }", document)
        self.assertIn("fragment R on Repository", document)
        self.assertEqual(variables, {"o0": "a", "n0": "b", "o1": "c", "n1": "d.e"})

    def test_a_response_is_flattened(self):
        flat = _mod.flatten(_repository("o/r", 1))
        self.assertEqual((flat["id"], flat["name"], flat["license"], flat["licenseSpdxId"],
                          flat["primaryLanguage"], flat["defaultBranch"], flat["headSha"],
                          flat["description"]),
                         (1001, "o/r", "MIT License", "MIT", "Rust", "main", f"{1:040x}",
                          "Tool 1."))
        empty = _mod.flatten(dict(_repository("o/r", 1), defaultBranchRef=None, licenseInfo=None,
                                  primaryLanguage=None))
        self.assertEqual((empty["defaultBranch"], empty["headSha"], empty["license"]),
                         (None, None, None))

    def test_errors_are_sorted_into_not_found_blocked_and_worth_another_attempt(self):
        body = {"data": {"r0": None, "r1": None, "r2": None, "r3": _repository("x/y", 3)},
                "errors": [{"type": "NOT_FOUND", "path": ["r0"], "message": "gone"},
                           {"type": "FORBIDDEN", "path": ["r1"],
                            "message": "Repository access blocked"},
                           {"type": "SERVICE_UNAVAILABLE", "path": ["r2"], "message": "later"}]}
        responses, _ = _mod.classify(["a/a", "b/b", "c/c", "x/y"], body)
        self.assertEqual([a and a["status"] for a in responses],
                         ["not_found", "blocked", None, "found"])

    def test_only_an_error_of_the_whole_query_that_names_a_timeout_is_a_timeout(self):
        """A query that GitHub rejects, such as a query with a field removed from the schema,
        fails every batch. If the batch were split, each part would spend the attempts again."""
        timeout = {"message": "Something went wrong while executing your query. This may be the "
                              "result of a timeout, or it could be a GitHub bug."}
        self.assertTrue(_mod.timed_out({"data": None, "errors": [timeout]}))
        self.assertTrue(_mod.timed_out({"data": {"rateLimit": {"cost": 1}, "r0": None},
                                        "errors": [timeout]}))
        self.assertFalse(_mod.timed_out({"data": None, "errors": [
            {"message": "Field 'isMirrored' doesn't exist on type 'Repository'"}]}))
        self.assertFalse(_mod.timed_out({"data": {"r0": None}, "errors": [
            {"type": "SERVICE_UNAVAILABLE", "path": ["r0"], "message": "timeout"}]}))
        self.assertFalse(_mod.timed_out({"data": {"r0": _repository("a/b", 1), "r1": None},
                                         "errors": [timeout]}))


class TestARun(_QueryCase):

    def test_a_response_is_recorded_for_every_repository_in_ghs_order(self):
        names = [f"o{i}/r{i}" for i in range(250)]
        missing, blocked = {"o5/r5"}, {"o6/r6"}
        github = FakeGitHub(names, missing=missing, blocked=blocked,
                            renamed={"o7/r7": "new-owner/R7"})
        self.retrieval(names)
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        responses = self.responses()
        self.assertEqual([a["ghsName"] for a in responses], names)
        self.assertEqual(responses[5]["status"], "not_found")
        self.assertTrue(responses[5]["restChecked"])
        self.assertEqual(responses[6]["status"], "blocked")
        self.assertEqual(responses[7]["status"], "found")
        self.assertEqual((responses[7]["repository"]["name"], responses[7]["redirectedTo"]),
                         ("new-owner/R7", "new-owner/R7"))
        self.assertEqual(responses[0]["repository"]["createdAt"], "2023-06-04T16:23:40Z")
        row = ghs_cache.index_row(RID, self.cache)
        self.assertEqual((row["github_rows"], row["github_found"], row["github_not_found"]),
                         ("250", "248", "2"))
        self.assertEqual(row["github_cost"], "4")
        self.assertEqual(sorted(github.rest), ["o5/r5", "o7/r7"])
        self.assertFalse((self.cache / f"{RID}.github.partial.jsonl").exists())

    def test_a_failure_while_the_responses_are_recorded_sends_no_further_batch(self):
        """A full disk or Ctrl+C while the run records a batch ends the pass. The queued batches
        are not sent, because nobody would record their responses. The next Start sends them
        again.

        The fake GitHub responds to the first two batches at once and holds each later batch
        until the pass has ended, and the second record fails once each of the two workers holds
        a batch. The held batches are the requests in flight when the pass ends, so the run sends
        four batches whatever the order in which the threads run. Without the hold, the workers
        could send every queued batch while the run records the first."""
        names = [f"o{i}/r{i}" for i in range(100)]
        self.retrieval(names)
        github = FakeGitHub(names)
        send, sent, lock = github.post, [], threading.Lock()
        holding, failed, gave_up = threading.Event(), threading.Event(), threading.Event()
        record, calls, queries, held = _mod.Query.record, [], [], []

        def post(url, json=None, headers=None, timeout=None):
            with lock:
                sent.append(url)
                number = len(sent)
            if number > 2:
                if number == 4:
                    holding.set()
                # The bound only turns a pass that never ends into a failure.
                if not gave_up.is_set() and not (failed.wait(600) and queries[0].ended.wait(600)):
                    gave_up.set()
            return send(url, json=json, headers=headers, timeout=timeout)

        def failing(query, rows):
            calls.append(rows)
            if len(calls) < 2:
                return record(query, rows)
            queries.append(query)
            held.append(holding.wait(600))
            failed.set()
            raise OSError(28, "No space left on device")

        github.post = post
        with mock.patch.object(_mod.Query, "record", failing), \
                self.assertRaises(OSError):
            self.run_with(github, batch_size=1, workers=2)
        self.assertEqual(held, [True], "the workers did not both hold a batch")
        self.assertFalse(gave_up.is_set(), "the failed record did not end the pass")
        self.assertEqual(len(github.batches), 4)

    def test_a_batch_that_waits_for_a_paused_account_sends_nothing_once_the_pass_ended(self):
        """A full disk while another worker's response is recorded ends the pass while this
        batch waits for its account. If the batch were sent after the pause, nobody would record
        its response, and the run would exit only after the pause."""
        github = FakeGitHub(["a/b"])
        pool = _mod.TokenPool(["t1"], clock=_mod.time.time, sleep=_mod.time.sleep)
        pool.pause("t1", 600)
        query = _mod.Query(pool, self.tmp / "partial.jsonl")
        sleep = _mod.time.sleep

        def ending(seconds):
            sleep(seconds)
            query.ended.set()

        with mock.patch.object(_mod.requests, "post", github.post), \
                mock.patch.object(pool, "sleep", ending):
            self.assertEqual(query.fetch(["a/b"]), [None])
        self.assertEqual((github.batches, self.sleeps), ([], [1.0]))

    def test_a_retrieval_that_already_has_githubs_responses_is_left_alone(self):
        names = ["a/b"]
        self.retrieval(names)
        self.run_with(FakeGitHub(names))
        github = FakeGitHub(names)
        code, err = self.run_with(github)
        self.assertEqual(code, 0)
        self.assertIn("already has GitHub's responses", err)
        self.assertEqual(github.batches, [])

    def test_responses_without_a_field_that_the_query_requests_are_requested_again(self):
        """Responses that lack the description, which the query requests, are requested again,
        because a sampling frame filtered from them would give the model no description."""
        names = ["a/b", "c/d"]
        self.retrieval(names)
        self.run_with(FakeGitHub(names))
        responses = self.responses()
        for response in responses:
            del response["repository"]["description"]
        _, sha = ghs_cache.write_rows(self.cache, RID, "github", responses)
        ghs_cache.put_index_row({"retrieval_id": RID, "github_file_sha256": sha}, self.cache)
        github = FakeGitHub(names)
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertIn("lack fields that the query requests", err)
        self.assertEqual(github.batches, [names])
        self.assertEqual([a["repository"]["description"] for a in self.responses()],
                         ["Tool 0.", "Tool 1."])
        self.assertEqual(ghs_cache.index_row(RID, self.cache)["github_requests"], "1")

    def test_responses_that_the_index_does_not_record_are_requested_again(self):
        """A run that queries GitHub again writes the new responses, then records their SHA-256
        in the index. A run stopped between the write and the record leaves a file that fails
        the check against the recorded SHA-256. The next run queries GitHub again rather than
        stopping on the failed check."""
        names = ["a/b", "c/d"]
        self.retrieval(names)
        self.run_with(FakeGitHub(names))
        ghs_cache.write_rows(self.cache, RID, "github", reversed(self.responses()))
        github = FakeGitHub(names)
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertIn("do not match the checksum in the index", err)
        self.assertEqual(github.batches, [names])
        self.assertEqual([a["ghsName"] for a in self.responses()], names)

    def test_errors_that_are_not_objects_are_a_failed_attempt(self):
        """A proxy can send a response with HTTP 200 that has text in `errors` rather than
        error objects, or `errors` or `data` of another type than GraphQL's. Such a response is
        a failed attempt, and the warning names its text as the error. Read as error objects,
        the text would raise AttributeError in the worker, and every run that resumes the query
        would exit 1."""
        names = [f"o{i}/r{i}" for i in range(3)]
        for body, named in (
                ({"data": None, "errors": ["upstream connect error"]}, "upstream connect error"),
                ({"errors": "upstream connect error"}, "upstream connect error"),
                ({"data": {"r0": None}, "errors": [None, "upstream connect error"]},
                 "upstream connect error"),
                ({"data": ["upstream connect error"]},
                 "`data` in GitHub's response is not a JSON object")):
            with self.subTest(body=body):
                shutil.rmtree(self.cache, ignore_errors=True)
                self.retrieval(names)
                github = FakeGitHub(names, posts=[_Response(payload=json.loads(json.dumps(body)))
                                                  for _ in range(_mod.ATTEMPTS)])
                code, err = self.run_with(github, workers=1)
                self.assertEqual(code, 0, err)
                self.assertEqual([r["status"] for r in self.responses()], ["found"] * 3)
                self.assertEqual(len(github.batches), _mod.ATTEMPTS + 1)
                self.assertIn(f"GitHub returned no response for 3 repositories from o0/r0 to "
                              f"o2/r2 in {_mod.ATTEMPTS} attempts. In the last attempt, ", err)
                self.assertIn(named, err)

    def test_a_batch_that_takes_too_long_is_split(self):
        names = [f"o{i}/r{i}" for i in range(10)]
        self.retrieval(names)
        github = FakeGitHub(names, posts=[_mod_response(502)])
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual([len(b) for b in github.batches], [10, 5, 5])

    def test_a_batch_that_times_out_with_http_200_is_split_down_to_the_slow_repository(self):
        """GitHub ends a query that runs past its time limit with HTTP 200, `data: null` and an
        error of the whole query that names a timeout. Every batch that contains a repository
        that GitHub cannot return in time gets this response. The batch is split in halves down
        to that repository. Only that repository stays unresolved after the rounds, and the run
        exits 0."""
        names = [f"o{i}/r{i}" for i in range(10)]
        self.retrieval(names)
        github = FakeGitHub(names)
        respond = github.post

        def post(url, json=None, headers=None, timeout=None):
            variables = json["variables"]
            batch = [f"{variables[f'o{i}']}/{variables[f'n{i}']}"
                     for i in range(len(variables) // 2)]
            if "o6/r6" in batch:
                github.batches.append(batch)
                return _Response(payload={"data": None, "errors": [{"message": (
                    "Something went wrong while executing your query. This may be the result "
                    "of a timeout, or it could be a GitHub bug. Please include `0000:0000` when "
                    "reporting this issue.")}]})
            return respond(url, json=json, headers=headers, timeout=timeout)

        github.post = post
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        responses = self.responses()
        self.assertEqual([(r["ghsName"], r["status"]) for r in responses if r["status"] != "found"],
                         [("o6/r6", "unresolved")])
        self.assertEqual([b for b in github.batches if b != ["o6/r6"]],
                         [names, names[:5], names[5:], names[5:7], ["o5/r5"], names[7:]])
        self.assertEqual(github.batches.count(["o6/r6"]),
                         _mod.ATTEMPTS * (1 + len(_mod.ROUND_WAITS)))
        self.assertIn("GitHub returned no response for o6/r6 in", err)

    def test_a_rate_limit_pauses_that_github_access_token_and_the_request_goes_to_another(self):
        names = ["a/b"]
        self.retrieval(names)
        github = FakeGitHub(names, posts=[_mod_response(403, headers={"Retry-After": "120"})])
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sleeps, [])
        self.assertIn("WARNING github_query: GitHub returned HTTP 403 because of a rate limit. "
                      "That GitHub access token is paused for 120s", err)

    def test_with_every_github_access_token_paused_the_request_waits_for_the_pause_to_end(self):
        names = ["a/b"]
        self.retrieval(names)
        github = FakeGitHub(names, posts=[_mod_response(403, headers={"Retry-After": "120"})])
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertAlmostEqual(sum(self.sleeps), 120.0)

    def test_rate_limits_do_not_use_up_a_batchs_attempts(self):
        """Counted as failed attempts, rate limits would split a batch and leave it without a
        response."""
        names = ["a/b", "c/d"]
        self.retrieval(names)
        limited = _mod_response(429, headers={"Retry-After": "60"})
        github = FakeGitHub(names, posts=[limited] * (_mod.ATTEMPTS + 2))
        code, err = self.run_with(github, tokens="t1", workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual([len(b) for b in github.batches], [2] * (_mod.ATTEMPTS + 3))

    def test_a_repository_blocked_for_its_terms_is_gone_rather_than_a_rate_limit(self):
        """GitHub returns 403 "Repository access blocked" for a repository that it took down for
        a terms-of-service violation. Read as a rate limit, the 403 would pause the GitHub access
        token permanently."""
        names = ["a/b", "c/d"]
        self.retrieval(names)
        github = FakeGitHub(names, missing={"a/b"}, tos={"a/b"})
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertEqual((self.responses()[0]["status"], self.responses()[0]["restChecked"]),
                         ("not_found", True))
        self.assertEqual(self.sleeps, [])
        self.assertNotIn("rate limit", err)

    def test_a_403_without_the_signs_of_a_rate_limit_is_a_failed_attempt(self):
        names = ["a/b"]
        self.retrieval(names)
        github = FakeGitHub(names, posts=[_mod_response(403, {"message": "Forbidden"})])
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.sleeps, [_mod.WAITS[0]])
        self.assertNotIn("rate limit", err)

    def test_a_rate_limit_that_does_not_end_ends_the_request(self):
        """Each pause beyond MAX_PAUSES counts as a failed attempt. The request ends after its
        attempts, and the next round queries the repository again."""
        names = ["a/b"]
        self.retrieval(names)
        limited = _mod_response(429, headers={"Retry-After": "60"})
        github = FakeGitHub(names, posts=[limited] * (_mod.MAX_PAUSES + _mod.ATTEMPTS))
        code, err = self.run_with(github, tokens="t1", workers=1)
        self.assertEqual(code, 0, err)
        self.assertIn(f"GitHub returned no response for a/b in {_mod.ATTEMPTS} attempts", err)
        self.assertEqual(len(github.batches), _mod.MAX_PAUSES + _mod.ATTEMPTS + 1)
        self.assertIn(_mod.ROUND_WAITS[0], self.sleeps)

    def test_an_exhausted_rate_limit_moves_to_another_github_access_token(self):
        names = ["a/b"]
        self.retrieval(names)
        spent = _mod_response(200, {"data": None, "errors": [{"type": "RATE_LIMITED",
                                                               "message": "API rate limit"}]},
                              headers={"X-RateLimit-Reset": "9999999999"})
        github = FakeGitHub(names, posts=[spent])
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(github.batches), 2)
        self.assertEqual(self.sleeps, [])

    def test_a_repository_that_errs_in_a_batch_is_queried_again_alone(self):
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.flaky = {"o2/r2"}
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [names, ["o2/r2"]])

    def test_a_field_that_github_could_not_resolve_is_queried_again(self):
        """GitHub returns the repository with its license null and an error that names the
        field. Taken as GitHub's value, the null license would remove the repository from the
        month's sampling frame."""
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.unresolved_license = {"o2/r2": 1}
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [names, ["o2/r2"]])
        self.assertEqual(self.responses()[2]["repository"]["licenseSpdxId"], "MIT")
        self.assertNotIn("unresolvedFields", json.dumps(self.responses()))

    def test_a_field_that_github_never_resolves_keeps_the_latest_response(self):
        """If a field that GitHub fails for a repository every time counted as unresolved,
        every run would end once such repositories exceeded the share that may stay unresolved,
        here one of four."""
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.unresolved_license = {"o2/r2": math.inf, "o3/r3": math.inf}
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        responses = self.responses()
        self.assertEqual([(r["status"], r["repository"]["licenseSpdxId"]) for r in responses],
                         [("found", "MIT"), ("found", "MIT"), ("found", None), ("found", None)])
        self.assertEqual([s for s in self.sleeps if s >= _mod.ROUND_WAITS[0]],
                         list(_mod.ROUND_WAITS))
        self.assertIn("GitHub returned 2 repositories with fields that it could not resolve "
                      "in every round, such as o2/r2 (licenseInfo). Recorded their responses "
                      "with those fields empty", err)

    def test_a_name_that_github_never_resolves_leaves_the_row_unresolved(self):
        """The filter and the stages after it know a repository by GitHub's name. Taken without
        the name, the response would stop the filter at every Start, because the filter reads
        the name of every row."""
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.unresolved_name = {"o2/r2": math.inf}
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["status"] for r in self.responses()],
                         ["found", "found", "unresolved", "found"])

    def test_a_renamed_repository_with_a_field_that_github_never_resolves_is_found(self):
        """GraphQL does not find the repository under its GHS name, and REST returns its new
        name. GraphQL then returns the repository under that name with a field that GitHub never
        resolves. The row keeps that response, checked by REST, rather than ending unresolved.
        A repository that GraphQL did not find once under its own name keeps its response in
        the same way."""
        for renamed, unresolved, not_found_once, redirected in (
                ({"o2/r2": "new/R2"}, "new/r2", set(), "new/R2"),
                (None, "o2/r2", {"o2/r2"}, None)):
            with self.subTest(renamed=renamed):
                names = [f"o{i}/r{i}" for i in range(4)]
                self.retrieval(names)
                github = FakeGitHub(names, renamed=renamed)
                github.not_found_once = set(not_found_once)
                github.unresolved_license = {unresolved: math.inf}
                code, err = self.run_with(github, workers=1)
                self.assertEqual(code, 0, err)
                row = self.responses()[2]
                self.assertEqual(row["status"], "found")
                self.assertEqual((row["repository"]["licenseSpdxId"], row.get("restChecked"),
                                  row.get("redirectedTo")), (None, True, redirected))
                shutil.rmtree(self.cache)

    def test_a_field_that_github_never_resolved_is_queried_again_after_a_failed_run(self):
        """The run takes the latest response of a repository with a field that GitHub never
        resolved. It then exits 1 for more unresolved rows than the share allows. Written to the
        partial file, that response would count as GitHub's. The next run, against a GitHub
        that resolves the field, would then keep the field empty."""
        names = [f"o{i}/r{i}" for i in range(8)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.unresolved_license = {"o2/r2": math.inf}
        github.broken = {"o5/r5", "o6/r6"}
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 1, err)
        github = FakeGitHub(names)
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertIn("o2/r2", [name for batch in github.batches for name in batch])
        self.assertEqual(self.responses()[2]["repository"]["licenseSpdxId"], "MIT")

    def test_a_stopped_run_resumes_from_its_partial_responses(self):
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        partial = self.cache / f"{RID}.github.partial.jsonl"
        lacking = {k: v for k, v in _mod.flatten(_repository(names[1], 1)).items()
                   if k != "description"}
        partial.write_text(
            json.dumps({"row": 0, "ghsName": names[0], "status": "found",
                        "repository": _mod.flatten(_repository(names[0], 0))}) + "\n"
            + json.dumps({"row": 1, "ghsName": names[1], "status": "found",
                          "repository": lacking}) + "\n" + '{"row": 2, "ghs')
        github = FakeGitHub(names)
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [["o1/r1", "o2/r2", "o3/r3"]])
        self.assertIn("An earlier run queried the other 1", err)
        self.assertEqual(len(self.responses()), 4)

    def test_responses_appended_after_a_truncated_line_are_read_by_the_next_run(self):
        """A stopped write leaves its last line without a newline. Appended to that line, a
        resumed run's first response would make a line that does not parse, and `read_partial`
        would stop at that line."""
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        partial = self.cache / f"{RID}.github.partial.jsonl"
        partial.write_text(json.dumps({"row": 0, "ghsName": names[0], "status": "found",
                                       "repository": _mod.flatten(_repository(names[0], 0))})
                           + "\n" + '{"row": 1, "ghs')
        github = FakeGitHub(names, posts=[None], down=True)
        code, _ = self.run_with(github, batch_size=1, workers=1)
        self.assertEqual((code, github.batches[0]), (1, ["o1/r1"]))
        github = FakeGitHub(names)
        code, err = self.run_with(github, batch_size=1, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [["o2/r2"], ["o3/r3"]])

    def test_a_last_line_cut_inside_a_character_is_queried_again(self):
        """A stop can cut the last line of the partial file inside a UTF-8 character, such as a
        character of a description. The run removes that line before it reads the file and
        queries that row again. A run that failed on the bytes that cannot be decoded would fail
        every later run as well."""
        names = [f"o{i}/r{i}" for i in range(3)]
        self.retrieval(names)

        def line(i):
            repository = dict(_mod.flatten(_repository(names[i], i)),
                              description="Werkzeug für Äpfel")
            return json.dumps({"row": i, "ghsName": names[i], "status": "found",
                               "repository": repository},
                              ensure_ascii=False, separators=(",", ":")).encode()

        cut = line(1)
        (self.cache / f"{RID}.github.partial.jsonl").write_bytes(
            line(0) + b"\n" + cut[:cut.index("ü".encode()) + 1])
        github = FakeGitHub(names)
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [names[1:]])
        self.assertEqual(self.responses()[0]["repository"]["description"], "Werkzeug für Äpfel")

    def test_a_repository_that_rest_returns_under_its_own_name_is_queried_again(self):
        """GraphQL does not find a name, and REST returns the repository under the same name.
        The REST response proves that GitHub has the repository. GraphQL is queried for that
        name again, as for a renamed repository, and the filter does not remove an existing
        repository as gone."""
        names = ["a/b", "o/case"]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.not_found_once = {"o/case"}
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [names, ["o/case"]])
        response = self.responses()[1]
        self.assertEqual((response["status"], response["restChecked"]), ("found", True))
        self.assertEqual(response["repository"]["name"], "o/case")
        self.assertNotIn("redirectedTo", response)

    def test_http_451_leaves_a_missing_repository_not_found(self):
        """GitHub returns HTTP 451 for a repository that it withholds for legal reasons. The
        response names no repository under any name, and GraphQL is not queried again."""
        class Withheld(FakeGitHub):
            def get(self, url, headers=None, timeout=None):
                if url == _mod.RATE_LIMIT_URL:
                    return super().get(url, headers, timeout)
                self.rest.append(url[len("https://api.github.com/repos/"):])
                return _Response(451, {"message": "Repository access blocked"})

        names = ["a/b", "o/legal"]
        self.retrieval(names)
        github = Withheld(names, missing=("o/legal",))
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [names])
        response = self.responses()[1]
        self.assertEqual((response["status"], response["restChecked"]), ("not_found", True))
        self.assertNotIn("redirectedTo", response)

    def test_rows_without_a_response_are_queried_again_in_later_rounds(self):
        """An outage of the network outlasts the attempts of every request but ends within the
        rounds. The rows without a response from the first pass get a response in a later
        round, and the run does not end."""
        names = [f"o{i}/r{i}" for i in range(4)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.clock, github.offline_until = (lambda: self.now[0]), self.now[0] + 2 * 3600
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual([r["status"] for r in self.responses()], ["found"] * 4)
        self.assertIn(_mod.ROUND_WAITS[0], self.sleeps)
        self.assertIn("Requesting them again in", err)

    def test_a_few_rows_that_no_round_settles_are_recorded_as_unresolved(self):
        """After the last round, two repositories are unresolved. GraphQL returns an error for
        the first in every batch. GraphQL does not find the second, and no REST check reaches
        it. Within the share that the query allows, the query records both and exits 0, so that
        the stages after it run."""
        names = [f"o{i}/r{i}" for i in range(math.ceil(2 / _mod.UNRESOLVED_MAX_SHARE))]
        self.retrieval(names)
        github = FakeGitHub(names, missing={"o8/r8"})
        github.broken = {"o7/r7"}
        lookup = github.get

        def get(url, headers=None, timeout=None):
            if url.endswith("/repos/o8/r8"):
                return _Response(500, {})
            return lookup(url, headers=headers, timeout=timeout)

        github.get = get
        code, err = self.run_with(github, workers=2)
        self.assertEqual(code, 0, err)
        responses = self.responses()
        self.assertEqual([(r["ghsName"], r["status"]) for r in responses
                          if r["status"] != "found"],
                         [("o7/r7", "unresolved"), ("o8/r8", "unresolved")])
        self.assertEqual(responses[7]["error"], "GitHub returned no response")
        self.assertEqual([s for s in self.sleeps if s >= _mod.ROUND_WAITS[0]],
                         list(_mod.ROUND_WAITS))
        self.assertIn("Recorded them as unresolved for the filter to remove", err)
        self.assertIn("2 unresolved", err)
        self.assertFalse((self.cache / f"{RID}.github.partial.jsonl").exists())

    def test_rows_without_a_response_fail_the_run_and_write_no_github_rows(self):
        """More rows without a response than the query records as unresolved end the run after
        the last round, and the next run queries them again."""
        names = ["a/b", "c/d"]
        self.retrieval(names)
        github = FakeGitHub(names, down=True)
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 1)
        self.assertIn("Responses or name checks are missing for 2 of 2 repositories after", err)
        self.assertEqual([s for s in self.sleeps if s >= _mod.ROUND_WAITS[0]],
                         list(_mod.ROUND_WAITS))
        self.assertFalse(ghs_cache.has_rows(self.cache, RID, "github"))

    def test_a_run_that_stops_keeps_its_requests_and_the_next_adds_to_them(self):
        """The index counts the requests of every run of the query, stopped runs included. Two
        of the three rows get no response, which is more than the query records as unresolved,
        and the first run fails."""
        names = ["a/b", "c/d", "e/f"]
        self.retrieval(names)
        github = FakeGitHub(names, posts=[None], down=True)
        code, _ = self.run_with(github, batch_size=1, workers=1)
        self.assertEqual(code, 1)
        self.assertEqual(ghs_cache.index_row(RID, self.cache)["github_requests"], "1")
        code, err = self.run_with(FakeGitHub(names), batch_size=1, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(ghs_cache.index_row(RID, self.cache)["github_requests"], "3")

    def test_a_rest_check_that_fails_leaves_the_row_for_the_next_run(self):
        names = ["a/b", "c/d"]
        self.retrieval(names)
        github = FakeGitHub(names, missing=set(names))
        broken = github.get

        def get(url, headers=None, timeout=None):
            if "/repos/" in url:
                return _Response(500, {})
            return broken(url, headers=headers, timeout=timeout)

        github.get = get
        code, err = self.run_with(github)
        self.assertEqual(code, 1)
        github = FakeGitHub(names, missing=set(names))
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.batches, [])
        self.assertEqual([r["status"] for r in self.responses()], ["not_found", "not_found"])

    def test_a_rest_response_that_is_not_json_leaves_the_row_for_the_next_round(self):
        """A REST check that returns HTTP 200 with an HTML page, such as a proxy's, says nothing
        about the repository. The check counts as a failed attempt. Once the attempts are
        spent, the next round checks the row again, rather than the run ending on an
        exception."""
        class Proxied(FakeGitHub):
            pages = 0

            def get(self, url, headers=None, timeout=None):
                if url.endswith("/repos/o/gone") and self.pages < _mod.ATTEMPTS:
                    self.pages += 1
                    self.rest.append("o/gone")
                    return _Response(200, None, {"Content-Type": "text/html"})
                return super().get(url, headers, timeout)

        names = ["a/b", "o/gone"]
        self.retrieval(names)
        github = Proxied(names, missing={"o/gone"})
        code, err = self.run_with(github, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual(github.rest, ["o/gone"] * (_mod.ATTEMPTS + 1))
        self.assertIn(_mod.ROUND_WAITS[0], self.sleeps)
        response = self.responses()[1]
        self.assertEqual((response["status"], response["restChecked"]), ("not_found", True))

    def test_one_row_that_no_round_settles_in_a_small_retrieval_is_unresolved(self):
        """In a retrieval too small for `UNRESOLVED_MAX_SHARE` to allow one unresolved row, the
        query still records a single unresolved row and exits 0."""
        code, err = self.one_row_never_settles()
        self.assertEqual(code, 0, err)
        self.assertEqual([(r["ghsName"], r["status"]) for r in self.responses()
                          if r["status"] != "found"], [("o7/r7", "unresolved")])

    def test_the_index_counts_an_unresolved_row_apart_from_the_repositories_not_found(self):
        """The index does not count an unresolved row among the repositories that GitHub did
        not find, because no response says whether GitHub has the repository."""
        code, err = self.one_row_never_settles()
        self.assertEqual(code, 0, err)
        row = ghs_cache.index_row(RID, self.cache)
        self.assertEqual((row["github_rows"], row["github_found"], row["github_not_found"],
                          row["github_unresolved"]), ("300", "299", "0", "1"))

    def one_row_never_settles(self) -> tuple:
        """Run the query over 300 rows, with one row that gets an error from GraphQL in every
        batch."""
        names = [f"o{i}/r{i}" for i in range(300)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.broken = {"o7/r7"}
        return self.run_with(github, workers=2)

    def test_a_failure_of_every_request_ends_the_query_after_the_rounds(self):
        """A refused connection fails every request. After its attempts, each batch is left
        unsplit to the next round. A pass ends after `NO_RESPONSE_BATCHES` batches in a row
        without a response. The run exits 1 after the rounds and spends only the waits of the
        rounds and the attempts of a few batches per pass."""
        names = [f"o{i}/r{i}" for i in range(2000)]
        self.retrieval(names)
        github = FakeGitHub(names)

        def refused(url, json=None, headers=None, timeout=None):
            github.batches.append(len(json["variables"]) // 2)
            raise _mod.requests.RequestException("[Errno 111] Connection refused")

        github.post = refused
        workers = 2
        code, err = self.run_with(github, batch_size=50, workers=workers)
        self.assertEqual(code, 1)
        passes = len(_mod.ROUND_WAITS) + 1
        batches = passes * (_mod.NO_RESPONSE_BATCHES + workers)
        self.assertLessEqual(sum(self.sleeps),
                             sum(_mod.ROUND_WAITS) + batches * sum(_mod.WAITS))
        self.assertLessEqual(len(github.batches), batches * _mod.ATTEMPTS)
        self.assertEqual(set(github.batches), {50})
        self.assertIn("in a row. In the last one, the GraphQL request failed ([Errno 111] "
                      "Connection refused). Ended the pass and left its other rows to the next "
                      "round", err)
        self.assertIn("Responses or name checks are missing for 2,000 of 2,000 repositories after "
                      f"{len(_mod.ROUND_WAITS)} further round(s)", err)

    def test_a_query_that_github_keeps_failing_in_part_stops_at_its_time_limit(self):
        """No pass ends early, because only every other repository gets an error in each
        request and no response. The query sends no request after `QUERY_SECONDS` and starts no
        further round. It exits 1 and keeps the responses so far for the next run."""
        names = [f"o{i}/r{i}" for i in range(100)]
        self.retrieval(names)
        github = FakeGitHub(names)
        github.broken = set(names[1::2])
        sent, post = [], github.post

        def timed(url, json=None, headers=None, timeout=None):
            sent.append(self.now[0])
            return post(url, json=json, headers=headers, timeout=timeout)

        github.post = timed
        start, limit = self.now[0], 1000
        with mock.patch.object(_mod, "QUERY_SECONDS", limit):
            code, err = self.run_with(github, batch_size=1, workers=1)
        self.assertEqual(code, 1)
        self.assertLess(max(sent), start + limit)
        self.assertLess(len(sent), len(names))
        self.assertIn("Started no further round, because the time limit of 0 hours from the start "
                      "ends before the next round", err)
        self.assertNotIn(_mod.ROUND_WAITS[0], self.sleeps)
        self.assertTrue((self.cache / f"{RID}.github.partial.jsonl").read_text())


class TestGitHubAccessTokens(_QueryCase):

    def run_with_file(self, github, text):
        """Run with the GitHub access tokens of a credentials file alone, and return the exit
        code, the log and the GitHub access tokens that the GraphQL requests sent, in order."""
        path = self.tmp / "credentials.csv"
        path.write_text(text)
        seen, original = [], github.post

        def post(url, json=None, headers=None, timeout=None):
            seen.append(headers["Authorization"].removeprefix("bearer "))
            return original(url, json=json, headers=headers, timeout=timeout)

        err = io.StringIO()
        with mock.patch.object(_mod.requests, "post", post), \
                mock.patch.object(_mod.requests, "get", github.get), \
                contextlib.redirect_stderr(err):
            code = _mod.run(self.cache, self.pointer, 100, 1, environ={}, credentials_path=path)
        return code, err.getvalue(), seen

    def test_the_github_access_tokens_of_the_credentials_file_are_used(self):
        self.retrieval(["a/b"])
        code, err, seen = self.run_with_file(
            FakeGitHub(["a/b"]), "service,account,credential\ngithub,someone,t_from_file\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, ["t_from_file"])
        self.assertIn("1 GitHub access token(s) of 1 account(s)", err)

    def test_a_rate_limit_sets_aside_every_github_access_token_of_the_account(self):
        """A secondary rate limit applies to the account. The second GitHub access token of the
        same account would get the same rate limit."""
        self.retrieval(["a/b"])
        code, err, seen = self.run_with_file(
            FakeGitHub(["a/b"], posts=[_mod_response(403, headers={"Retry-After": "120"})]),
            "service,account,credential\ngithub,first,t1\ngithub,first,t2\n"
            "github,second,t3\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, ["t1", "t3"])
        self.assertEqual(self.sleeps, [])
        self.assertIn("because of a rate limit. The GitHub access tokens of account first are "
                      "paused for 120s.\n", err)
        self.assertIn("3 GitHub access token(s) of 2 account(s)", err)

    def test_no_github_access_token_is_an_error(self):
        self.retrieval(["a/b"])
        code, err = self.run_with(FakeGitHub(["a/b"]), tokens="")
        self.assertEqual(code, 1)
        self.assertIn("No GitHub access token to query GitHub about 1 repositories "
                      "(GITHUB_TOKENS and GITHUB_TOKEN are unset", err)

    def test_a_refused_github_access_token_is_skipped(self):
        self.retrieval(["a/b"])
        code, err = self.run_with(FakeGitHub(["a/b"]), tokens="bad1,t2")
        self.assertEqual(code, 0, err)
        self.assertIn("GitHub rejected GitHub access token 1 of 2 of an unnamed account "
                      "(HTTP 401)", err)

    def test_every_github_access_token_rejected_at_the_start_is_checked_again(self):
        """GitHub rejects both GitHub access tokens when the query starts and accepts them
        after the first wait. The query waits as it does for a rejection later in the run,
        rather than ending before its first request."""
        names = ["a/b", "c/d", "e/f"]
        self.retrieval(names)
        github = FakeGitHub(names, checks=[_mod_response(401), _mod_response(401)])
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertIn("GitHub accepts none of the configured GitHub access tokens. Checking them "
                      "again in", err)
        self.assertEqual([s for s in self.sleeps if s >= _mod.ROUND_WAITS[0]],
                         [_mod.ROUND_WAITS[0]])
        self.assertEqual([(r["ghsName"], r["status"]) for r in self.responses()],
                         [(name, "found") for name in names])

    def test_a_github_access_token_that_github_keeps_rejecting_ends_the_query(self):
        """GitHub rejects the only GitHub access token at the start and at every check after
        the waits of the rounds. The query exits 1 without a request."""
        self.retrieval(["a/b"])
        github = FakeGitHub(["a/b"])
        code, err = self.run_with(github, tokens="bad1")
        self.assertEqual(code, 1)
        self.assertEqual(self.sleeps, list(_mod.ROUND_WAITS))
        self.assertEqual(github.batches, [])
        self.assertIn("GitHub accepted none of the configured GitHub access tokens in "
                      f"{len(_mod.ROUND_WAITS)} further checks", err)
        self.assertEqual(ghs_cache.index_row(RID, self.cache)["github_file_sha256"], "")

    def test_each_short_spell_of_rejections_starts_the_waits_again(self):
        """GitHub rejects every GitHub access token in the minute after each request that it
        serves, in one spell more than `ROUND_WAITS` has waits. Each spell ends within the first
        wait, and every spell therefore gets the first wait again. The query settles every row
        and exits 0 rather than ending once one sequence of waits is spent across the spells."""
        names = [f"o{i}/r{i}" for i in range(10)]
        self.retrieval(names)
        spells = len(_mod.ROUND_WAITS) + 1
        github = _Spells(names, spells, lambda: self.now[0])
        code, err = self.run_with(github, tokens="t1", batch_size=1, workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual([s for s in self.sleeps if s >= _mod.ROUND_WAITS[0]],
                         [_mod.ROUND_WAITS[0]] * spells)
        self.assertEqual([r["status"] for r in self.responses()], ["found"] * len(names))

    def test_spells_of_rejections_end_the_query_at_its_time_limit(self):
        """GitHub keeps rejecting every GitHub access token after each request that it serves.
        Each spell starts the waits again, and the time limit of the run ends the query. No
        wait for a check ends after the time limit, and the remaining rows wait for the next
        run."""
        names = [f"o{i}/r{i}" for i in range(10)]
        self.retrieval(names)
        github = _Spells(names, math.inf, lambda: self.now[0])
        start, limit = self.now[0], 2.5 * _mod.ROUND_WAITS[0]
        with mock.patch.object(_mod, "QUERY_SECONDS", limit):
            code, err = self.run_with(github, tokens="t1", batch_size=1, workers=1)
        self.assertEqual(code, 1)
        self.assertLess(self.now[0], start + limit)
        self.assertIn("Stopped checking them, because the time limit of 0 hours from the start "
                      "ends before the next check", err)
        self.assertIn("Responses or name checks are missing for", err)

    def test_a_github_access_token_that_could_not_be_checked_is_checked_again(self):
        """A check that fails on the network or with HTTP 5xx says nothing about the GitHub
        access token. The query checks the GitHub access token again rather than rejecting
        it."""
        self.retrieval(["a/b"])
        github = FakeGitHub(["a/b"], checks=[_mod_response(502),
                                             _mod.requests.RequestException("timed out")])
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertNotIn("GitHub rejected GitHub access token", err)
        self.assertIn("GitHub gave no verdict on GitHub access token 1 of 1 of an unnamed account "
                      "(HTTP 502). Checking again in", err)
        self.assertEqual(self.sleeps[:2], list(_mod.CHECK_WAITS[:2]))

    def test_a_github_access_token_that_no_check_reaches_is_kept(self):
        self.retrieval(["a/b"])
        github = FakeGitHub(["a/b"], checks=[_mod_response(503)] * (len(_mod.CHECK_WAITS) + 1))
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertIn("GitHub gave no verdict on GitHub access token 1 of 1 of an unnamed account "
                      "(HTTP 503). Kept it without a verdict", err)

    def test_a_401_drops_a_github_access_token_only_when_its_check_rejects_it(self):
        """GitHub returns HTTP 401 for one request, and `/rate_limit` accepts the GitHub access
        token. The request is sent again with that GitHub access token rather than the run
        ending without a GitHub access token."""
        self.retrieval(["a/b"])
        github = FakeGitHub(["a/b"], posts=[_mod_response(401)])
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(github.batches), 2)
        self.assertEqual(self.sleeps, [_mod.WAITS[0]])
        self.assertNotIn("Dropped it", err)

    def test_with_every_github_access_token_rejected_the_query_checks_them_again(self):
        """GitHub rejects the only GitHub access token for a request and for its check, and the
        pool is then empty. The query waits, checks the GitHub access tokens again, and
        continues with the GitHub access token that GitHub accepts again."""
        self.retrieval(["a/b"])
        github = FakeGitHub(["a/b"], posts=[_mod_response(401)],
                            checks=[_mod_response(200), _mod_response(401)])
        code, err = self.run_with(github, tokens="t1")
        self.assertEqual(code, 0, err)
        self.assertIn("(HTTP 401). Dropped it (0 account(s) left)", err)
        self.assertIn("GitHub accepts none of the configured GitHub access tokens. Checking them "
                      "again in", err)
        self.assertIn(_mod.ROUND_WAITS[0], self.sleeps)
        self.assertEqual(self.responses()[0]["status"], "found")

    def test_a_401_from_rest_is_handled_as_one_from_graphql(self):
        """The REST check of a missing repository and the GraphQL query treat HTTP 401 alike. A
        GitHub access token that its check rejects as well is dropped at once, and the request
        is not tried again with it after a wait."""
        class RestRejects(FakeGitHub):
            rejected = None

            def get(self, url, headers=None, timeout=None):
                token = headers["Authorization"]
                if url == _mod.RATE_LIMIT_URL and token == self.rejected:
                    return _Response(401, {"message": "Bad credentials"})
                if url != _mod.RATE_LIMIT_URL and self.rejected is None:
                    self.rejected = token
                    return _Response(401, {"message": "Bad credentials"})
                return super().get(url, headers, timeout)

        self.retrieval(["a/b"])
        github = RestRejects(["a/b"], missing={"a/b"})
        code, err = self.run_with(github)
        self.assertEqual(code, 0, err)
        self.assertIn("(HTTP 401). Dropped it (1 account(s) left)", err)
        self.assertEqual(self.sleeps, [])
        self.assertEqual((self.responses()[0]["status"], self.responses()[0]["restChecked"]),
                         ("not_found", True))


def _mod_response(status, payload=None, headers=None):
    return _Response(status, payload, headers)


if __name__ == "__main__":
    unittest.main()
