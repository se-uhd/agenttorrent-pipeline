"""
Tests for readmes.py

Strategy:
  - GitHub is a fake session that returns a response for each URL from a map and records the
    headers that it was sent. The pool of GitHub access tokens runs on a fake clock, and a pause
    ends without real waiting.
  - The language check runs Lingua itself, over texts with an unambiguous language.

Requirements: Python 3.14, requests, lingua-language-detector (requirements.txt)
Run with:  python3 -m unittest test_readmes -v
"""

import base64
import contextlib
import io
import sys
import time
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import readmes  # noqa: E402

from agentcontext.credentials import Credential  # noqa: E402
from agentcontext.github_api import NoTokens, TokenPool  # noqa: E402

REVISION = "a" * 40
README = f"https://api.github.com/repos/o/r/readme?ref={REVISION}"


class _Response:

    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code, self.body = status, body
        self.headers, self.content = headers or {}, content

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


def _readme(text: str, name="o/r", path="README.md", sha="b" * 40):
    data = text.encode("utf-8")
    return _Response(200, {"path": path, "sha": sha, "size": len(data), "encoding": "base64",
                           "content": base64.b64encode(data).decode(),
                           "url": f"https://api.github.com/repos/{name}/contents/{path}"},
                     headers={"ETag": 'W/"etag-1"'})


class _GitHub:
    """Returns the responses for each URL from `routes`, a list per URL taken in order, the last
    response repeated."""

    def __init__(self, routes):
        self.routes = {url: list(answers) for url, answers in routes.items()}
        self.sent = []

    def get(self, url, headers=None, timeout=None):
        self.sent.append((url, dict(headers or {})))
        answers = self.routes.get(url)
        if not answers:
            return _Response(404, {"message": "Not Found"})
        return answers.pop(0) if len(answers) > 1 else answers[0]

    def post(self, url, headers=None, json=None, timeout=None):
        self.posted = getattr(self, "posted", []) + [json]
        return self.get(url, headers, timeout)


class _Clock:

    def __init__(self):
        self.now, self.waits = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def _client(routes, *tokens):
    clock = _Clock()
    pool = TokenPool(list(tokens or [Credential("github", "a", "t1")]), clock=clock,
                     sleep=clock.sleep)
    github = _GitHub(routes)
    return readmes.Client(pool, session=github, sleep=clock.sleep), github, clock


class TestFetch(unittest.TestCase):

    def test_a_readme_is_read_at_the_revision_with_its_path_sha_and_etag(self):
        client, github, _ = _client({README: [_readme("# Tool\n\nDoes things.")]})
        readme = readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual((readme.status, readme.name, readme.revision, readme.path, readme.sha,
                          readme.etag, readme.text),
                         (readmes.FOUND, "o/r", REVISION, "README.md", "b" * 40, 'W/"etag-1"',
                          "# Tool\n\nDoes things."))
        self.assertEqual(github.sent[0][1]["Authorization"], "bearer t1")

    def test_an_etag_is_sent_and_a_304_is_unchanged(self):
        client, github, _ = _client({README: [_Response(304)]})
        readme = readmes.fetch(client, "o/r", 1, REVISION, 'W/"etag-1"')
        self.assertEqual(readme.status, readmes.UNCHANGED)
        self.assertIsNone(readme.text)
        self.assertEqual(github.sent[0][1]["If-None-Match"], 'W/"etag-1"')

    def test_a_readme_over_a_megabyte_is_read_through_the_raw_media_type(self):
        """The contents API returns no content in JSON for a file between 1 and 100 MB."""
        big = _Response(200, {"path": "README.md", "sha": "c" * 40, "size": 2_000_000,
                              "encoding": "none", "content": "",
                              "url": "https://api.github.com/repos/o/r/contents/README.md"})
        raw = _Response(200, content="é long".encode("utf-8"))
        client, github, _ = _client({README: [big, raw]})
        readme = readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual(readme.text, "é long")
        self.assertEqual(github.sent[1][1]["Accept"], readmes.RAW)

    def test_a_404_with_the_repository_there_is_no_readme(self):
        client, _, _ = _client({README: [_Response(404, {})],
                                "https://api.github.com/repos/o/r/readme": [_Response(404, {})],
                                "https://api.github.com/repos/o/r": [
                                    _Response(200, {"id": 1, "full_name": "o/r"})]})
        self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.NO_README)

    def test_a_404_without_the_repository_is_gone(self):
        client, _, _ = _client({})
        self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.GONE)

    def test_another_repository_under_the_name_is_gone(self):
        """A repository created under the name after the sampling frame's repository was deleted
        is a different repository, as its GitHub id shows."""
        client, _, _ = _client({"https://api.github.com/repos/o/r": [
            _Response(200, {"id": 2, "full_name": "o/r"})]})
        self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.GONE)

    def test_a_renamed_repository_is_read_under_its_new_name(self):
        client, _, _ = _client({
            "https://api.github.com/repos/o/r": [_Response(200, {"id": 1, "full_name": "n/r"})],
            f"https://api.github.com/repos/n/r/readme?ref={REVISION}": [_readme("x", name="n/r")]})
        readme = readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual((readme.status, readme.name), (readmes.FOUND, "n/r"))

    def test_a_revision_the_repository_lost_reads_the_default_branch(self):
        """A force push removes the commit that the sampling frame recorded. The README API then
        returns 404 for the commit, and the README of the default branch is read without a
        revision."""
        client, _, _ = _client({
            "https://api.github.com/repos/o/r": [_Response(200, {"id": 1, "full_name": "o/r"})],
            "https://api.github.com/repos/o/r/readme": [_readme("now")]})
        readme = readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual((readme.status, readme.revision, readme.text), (readmes.FOUND, "", "now"))

    def test_a_blocked_repository_is_gone(self):
        client, _, _ = _client({README: [_Response(451, {"message": "Repository access blocked",
                                                         "block": {"reason": "dmca"}})]})
        self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.GONE)


class TestRepository(unittest.TestCase):

    def test_the_repository_response_contains_its_description_and_creation_time(self):
        client, _, _ = _client({"https://api.github.com/repos/o/r": [_Response(200, {
            "id": 1, "full_name": "o/r", "description": "A tool.",
            "created_at": "2020-01-01T00:00:00Z"})]})
        found = readmes.repository(client, "o/r")
        self.assertEqual((found["description"], found["created_at"]),
                         ("A tool.", "2020-01-01T00:00:00Z"))

    def test_a_404_or_a_response_without_a_full_name_is_no_usable_response(self):
        for answer in (_Response(404, {"message": "Not Found"}), _Response(200, {"message": "x"})):
            client, _, _ = _client({"https://api.github.com/repos/o/r": [answer]})
            with self.subTest(status=answer.status_code), self.assertRaises(readmes.NoResponse):
                readmes.repository(client, "o/r")


class TestClient(unittest.TestCase):

    def test_a_rate_limit_pauses_the_account_and_the_request_goes_to_another(self):
        limited = _Response(403, {"message": "You have exceeded a secondary rate limit"},
                            headers={"Retry-After": "120"})
        client, github, clock = _client({README: [limited, _readme("x")]},
                                        Credential("github", "a", "t1"),
                                        Credential("github", "a", "t2"),
                                        Credential("github", "b", "t3"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            readme = readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual(readme.status, readmes.FOUND)
        self.assertEqual([h["Authorization"] for _, h in github.sent], ["bearer t1", "bearer t3"])
        self.assertEqual(clock.waits, [])
        self.assertIn("GitHub returned HTTP 403 for a rate limit. The GitHub access tokens of "
                      "account a are paused for 120s", err.getvalue())

    def test_a_refused_github_access_token_is_dropped(self):
        """GitHub refuses the GitHub access token for its rate limit too."""
        client, github, _ = _client({README: [_Response(401, {}), _readme("x")],
                                     readmes.RATE_LIMIT_URL: [_Response(401, {})]},
                                    Credential("github", "a", "bad"),
                                    Credential("github", "b", "good"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.FOUND)
        self.assertEqual([(url, h["Authorization"]) for url, h in github.sent],
                         [(README, "bearer bad"), (readmes.RATE_LIMIT_URL, "bearer bad"),
                          (README, "bearer good")])
        self.assertEqual([t.value for t in client.pool.tokens], ["good"])

    def test_a_github_access_token_that_github_accepts_for_its_rate_limit_is_kept(self):
        """A 401 for a GitHub access token that GitHub still accepts for its rate limit is a
        failed attempt. A pool that dropped a GitHub access token at each such 401 would be
        empty after as many 401s as there are GitHub access tokens, and every request after them
        would fail."""
        client, github, clock = _client({README: [_Response(401, {}), _Response(401, {}),
                                                  _readme("x")],
                                         readmes.RATE_LIMIT_URL: [_Response(200, {})]},
                                        Credential("github", "a", "t1"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.FOUND)
        self.assertEqual([t.value for t in client.pool.tokens], ["t1"])
        self.assertEqual([url for url, _ in github.sent],
                         [README, readmes.RATE_LIMIT_URL] * 2 + [README])
        self.assertEqual(clock.waits, [10, 30])
        self.assertEqual(err.getvalue(), "")

    def test_a_request_that_depends_on_no_repository_tells_whether_github_responds(self):
        """`responds` requests GitHub's rate limit, which costs nothing against that limit, with
        the attempts of any request. A response after a server error means that GitHub responds,
        and `responses` does not count it. While GitHub fails the requests for every README, a
        counted response would make each failure of another repository the first since GitHub's
        last response, and the pause for GitHub would never begin. Server errors through every
        attempt mean that GitHub does not respond."""
        client, github, clock = _client({readmes.RATE_LIMIT_URL: [
            _Response(502), _Response(200, {"resources": {"core": {"remaining": 4999}}})]})
        self.assertTrue(client.responds())
        self.assertEqual([(url, h["Authorization"]) for url, h in github.sent],
                         [(readmes.RATE_LIMIT_URL, "bearer t1")] * 2)
        self.assertEqual((client.responses, client.requests, clock.waits), (0, 2, [10]))
        client, _, clock = _client({readmes.RATE_LIMIT_URL: [_Response(502)]})
        self.assertFalse(client.responds())
        self.assertEqual((client.responses, clock.waits), (0, [10, 30, 60]))

    def test_server_errors_are_retried_and_then_count_as_no_usable_response(self):
        client, _, clock = _client({README: [_Response(502)]})
        with self.assertRaises(readmes.NoResponse):
            readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual(clock.waits, [10, 30, 60])

    def test_a_graphql_query_is_posted_and_leaves_the_rest_apis_count_alone(self):
        """The GraphQL API has a rate limit of its own. Its remaining points, at zero here, do
        not concern the REST API, and the next README is requested without a pause."""
        spent = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "99999"}
        client, github, clock = _client({readmes.GRAPHQL_URL: [_Response(502),
                                                               _Response(200, {"data": {}},
                                                                         headers=spent)],
                                         README: [_readme("x")]})
        answer = client.query("query { viewer { login } }", {"owner": "o"})
        self.assertEqual(answer.json(), {"data": {}})
        self.assertEqual(github.posted, [{"query": "query { viewer { login } }",
                                          "variables": {"owner": "o"}}] * 2)
        self.assertEqual(clock.waits, [10])
        self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.FOUND)
        self.assertEqual(clock.waits, [10])

    def test_an_exhausted_graphql_rate_limit_is_waited_out_and_leaves_the_rest_api_alone(self):
        """GitHub reports an exhausted GraphQL rate limit with HTTP 200, an error of type
        RATE_LIMITED and no data, which the classification would read as a repository without
        merged pull requests. With another account's GitHub access token, the query gets a
        response at once. With one account, the query waits for the reset that the response
        names, and a README request does not wait."""
        spent = _Response(200, {"data": None, "errors": [
            {"type": "RATE_LIMITED", "message": "API rate limit exceeded for user ID 1."}]},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"})
        answered = _Response(200, {"data": {"repository": {}}})
        client, github, clock = _client({readmes.GRAPHQL_URL: [spent, answered]},
                                        Credential("github", "a", "t1"),
                                        Credential("github", "b", "t2"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(client.query("q", {}).json(), {"data": {"repository": {}}})
        self.assertEqual([h["Authorization"] for _, h in github.sent], ["bearer t1", "bearer t2"])
        self.assertEqual(clock.waits, [])
        client, github, clock = _client({readmes.GRAPHQL_URL: [spent, answered],
                                         README: [_readme("x")]})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(client.query("q", {}).json(), {"data": {"repository": {}}})
        self.assertEqual(clock.waits, [100])
        self.assertIn("A GitHub access token reached its GraphQL rate limit. Its GraphQL "
                      "queries wait 100s", err.getvalue())
        github.routes[readmes.GRAPHQL_URL] = [spent]
        clock.now = 1050.0
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(readmes.NoResponse):
                client.query("q", {})
            waits = len(clock.waits)
            self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.FOUND)
        self.assertEqual(len(clock.waits), waits)

    def test_a_graphql_response_with_errors_that_are_not_objects_is_passed_on(self):
        """A proxy's text in `errors` is no rate limit, and the client passes the response on to
        the reader of the query, which fails the repository. A check of that text for a rate
        limit would raise AttributeError and end the labeling at every resume that got the same
        body."""
        for errors in (["502 Bad Gateway"], "502 Bad Gateway", [None, 502]):
            with self.subTest(errors=errors):
                broken = _Response(200, {"data": None, "errors": errors})
                client, github, clock = _client({readmes.GRAPHQL_URL: [broken]})
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(client.query("q", {}).json(),
                                     {"data": None, "errors": errors})
                self.assertEqual(len(github.posted), 1)
                self.assertEqual(clock.waits, [])
                self.assertEqual(err.getvalue(), "")

    def test_a_403_without_the_signs_of_a_rate_limit_is_not_handled_as_a_rate_limit(self):
        client, _, _ = _client({README: [_Response(403, {"message": "Forbidden"})]})
        with self.assertRaises(readmes.NoResponse):
            readmes.fetch(client, "o/r", 1, REVISION)

    def test_accepted_tokens_leave_out_the_refused(self):
        github = _GitHub({readmes.RATE_LIMIT_URL: [_Response(401), _Response(200)]})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            accepted = readmes.accepted_tokens([Credential("github", "a", "bad"),
                                                Credential("github", "b", "good")], github)
        self.assertEqual([t.value for t in accepted], ["good"])
        self.assertIn("GitHub access token 1 of 2 (account a) failed validation (HTTP 401)",
                      err.getvalue())

    def test_a_check_without_a_verdict_is_made_again_and_keeps_the_github_access_token(self):
        """Only HTTP 401 rejects a GitHub access token. HTTP 5xx or a connection that fails as
        the run starts says nothing about the GitHub access token. A pool that dropped each
        GitHub access token for such a failure would end the run before its first request."""
        github = _GitHub({readmes.RATE_LIMIT_URL: [_Response(503), _Response(200)]})
        waits = []
        with contextlib.redirect_stderr(io.StringIO()) as err:
            accepted = readmes.accepted_tokens([Credential("github", "a", "t1")], github,
                                               waits.append)
        self.assertEqual([t.value for t in accepted], ["t1"])
        self.assertEqual(waits, [10])
        self.assertIn("GitHub gave no verdict on GitHub access token 1 of 1 (account a, HTTP 503). "
                      "Checking again in 10s", err.getvalue())

        class Unreachable:
            def get(self, url, headers=None, timeout=None):
                raise requests.ConnectionError("Network is unreachable")
        waits.clear()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            accepted = readmes.accepted_tokens([Credential("github", "a", "t1")], Unreachable(),
                                               waits.append)
        self.assertEqual([t.value for t in accepted], ["t1"])
        self.assertEqual(waits, [10, 60, 300])
        self.assertIn("GitHub gave no verdict on GitHub access token 1 of 1 (account a, Network "
                      "is unreachable). Kept it", err.getvalue())

    def test_a_spell_in_which_github_rejects_every_github_access_token_is_waited_out(self):
        """GitHub can reject valid GitHub access tokens for a while, also for their rate limit.
        Once GitHub has rejected every GitHub access token, the pool checks the configured GitHub
        access tokens again after each wait, and the requests continue with the GitHub access
        tokens that GitHub accepts again. An empty pool would stop the run."""
        class Spell(_GitHub):
            """HTTP 401 for the first `rejected` requests, then the responses of `routes`."""

            def __init__(self, routes, rejected):
                super().__init__(routes)
                self.rejected = rejected

            def get(self, url, headers=None, timeout=None):
                if self.rejected:
                    self.rejected -= 1
                    self.sent.append((url, dict(headers or {})))
                    return _Response(401, {"message": "Bad credentials"})
                return super().get(url, headers, timeout)
        clock = _Clock()
        pool = TokenPool([Credential("github", "a", "t1"), Credential("github", "b", "t2")],
                         clock=clock, sleep=clock.sleep)
        # GitHub rejects the README request, the check of each GitHub access token and the
        # checks after the first wait.
        github = Spell({README: [_readme("x")], readmes.RATE_LIMIT_URL: [_Response(200, {})]},
                       rejected=6)
        client = readmes.Client(pool, session=github, sleep=clock.sleep)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(readmes.fetch(client, "o/r", 1, REVISION).status, readmes.FOUND)
        self.assertEqual(clock.waits, [300, 900])
        self.assertEqual([t.value for t in client.pool.tokens], ["t1", "t2"])
        self.assertIn("GitHub rejects every GitHub access token (HTTP 401). Checking them "
                      "again in 5 minutes", err.getvalue())

    def test_github_rejecting_every_github_access_token_after_the_last_wait_stops(self):
        """A GitHub access token that GitHub rejects through every wait ends the requests, and
        a later request ends at once rather than waiting through the checks again."""
        client, github, clock = _client({README: [_Response(401, {})],
                                         readmes.RATE_LIMIT_URL: [_Response(401, {})]})
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(NoTokens) as stopped:
                readmes.fetch(client, "o/r", 1, REVISION)
            self.assertEqual(clock.waits, list(readmes.RECHECK_WAITS))
            self.assertIn("also after 5 further checks", str(stopped.exception))
            sent = len(github.sent)
            with self.assertRaises(NoTokens):
                readmes.fetch(client, "o/r", 1, REVISION)
        self.assertEqual((clock.waits, len(github.sent)), (list(readmes.RECHECK_WAITS), sent))


class TestUsable(unittest.TestCase):

    def test_a_readme_with_text_is_usable_until_its_language_is_checked(self):
        for text in ("# Parser\n\nA fast parser for configuration files.",
                     "# Анализатор\n\nБыстрый анализатор конфигурационных файлов."):
            with self.subTest(text=text[:12]):
                self.assertEqual(readmes.unusable(readmes.Readme(readmes.FOUND, "o/r",
                                                                 text=text)), "")

    def test_empty_missing_and_gone(self):
        self.assertEqual(readmes.unusable(readmes.Readme(readmes.FOUND, "o/r", text=" \n")),
                         readmes.EMPTY_README)
        self.assertEqual(readmes.unusable(readmes.Readme(readmes.NO_README, "o/r")),
                         readmes.NO_README_FOUND)
        self.assertEqual(readmes.unusable(readmes.Readme(readmes.GONE, "o/r")),
                         readmes.NOT_FOUND_OR_NO_ACCESS)


class TestTheLanguageCheck(unittest.TestCase):
    """Rule 2 of the section "The reading rules" of 0_README.md."""

    def test_an_english_readme_is_english(self):
        measures = readmes.language(
            "# Parser\n\nA fast parser for configuration files. Install it with pip, then call "
            "parse() on a file. See the documentation for the options it takes.")
        self.assertEqual(measures["detected"], "en")
        self.assertTrue(readmes.english(measures))

    def test_a_readme_in_another_script_is_not(self):
        measures = readmes.language(
            "# Анализатор\n\nБыстрый анализатор конфигурационных файлов. Установите его через "
            "pip и вызовите функцию для файла. Подробности смотрите в документации проекта.")
        self.assertEqual(measures["detected"], "ru")
        self.assertFalse(readmes.english(measures))

    def test_code_and_tables_do_not_decide_the_language(self):
        """A Chinese README with English code blocks and tables is not English."""
        measures = readmes.language(
            "# 工具\n\n这是一个用于解析配置文件的快速工具。请先安装它，然后在文件上调用解析函数。"
            "更多信息请参阅项目文档和示例。\n\n```python\nimport parser\nparser.parse('a.toml')\n"
            "print('hello world from the parser example code')\n```\n\n| option | default |\n"
            "|---|---|\n| strict | false |\n")
        self.assertEqual(measures["detected"], "zh")
        self.assertFalse(readmes.english(measures))

    def test_a_language_written_in_ascii_passes_and_records_which_it_is(self):
        """The check passes the README to the model, which states whether the README is mainly
        English. The decision keeps the language that Lingua detected."""
        measures = readmes.language(
            "# Werkzeug\n\nEin Werkzeug, das Konfigurationsdateien formatiert. Installiere es "
            "mit pip und rufe es auf einem Verzeichnis auf. Die Dokumentation beschreibt alle "
            "Optionen.")
        self.assertEqual(measures["detected"], "de")
        self.assertTrue(readmes.english(measures))

    def test_which_names_in_a_count_name_a_language(self):
        """The names that the classification reads as a natural language: the names of Lingua in
        any case, with a qualifier in parentheses, their ISO codes with a script or a region, and
        the names for which the model's counts use another word than Lingua. A word that is no
        language, a misspelling and a name that joins languages are not read."""
        for name in ("Japanese", "japanese", " FRENCH ", "Chinese (Simplified)", "Portuguese (BR)",
                     "ja", "ja-JP", "zh_CN", "zh-Hans", "es-419", "jpn", "fra", "Slovene",
                     "Slovenian", "Norwegian", "Bokmal", "Bangla", "Bengali", "Sanskrit", "Sinhala",
                     "English"):
            with self.subTest(name=name):
                self.assertTrue(readmes.names_language(name))
        for name in ("Englisch", "Japanes", "Chinese/English", "Non-English", "Not English",
                     "code", "xx", "xx-YY", "", "(Simplified)", "words", "so-called", "it-terms",
                     "pl-sql"):
            with self.subTest(name=name):
                self.assertFalse(readmes.names_language(name))

    def test_a_readme_without_prose_is_english(self):
        """A README without prose gives the language check nothing to judge, and the model
        reads it."""
        self.assertEqual(readmes.language("```\ncode\n```\n"), {"prose_chars": 0})
        self.assertTrue(readmes.english({"prose_chars": 0}))

    def test_a_line_of_many_github_links_is_read_in_time(self):
        """A list of links separated by `<br>` becomes one line once its tags are spaces. A
        badge pattern that let a segment span the line would try every pair of later slashes in
        it and hold a worker for minutes to hours."""
        links = "<br>".join(f"https://github.com/user{n}/project{n}" for n in range(3200))
        started = time.monotonic()
        readmes.prose(f"# People\n\nThe people who took part.\n\n{links}\n")
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_name_with_a_run_of_underscores_is_read_in_time(self):
        """A pattern for what follows a language code that could read a run of underscores in
        many ways would try each of them, and the classification reads the names of a count on
        the event loop, where a slow match holds every worker."""
        started = time.monotonic()
        self.assertFalse(readmes.names_language("zz" + "_" * 42 + "."))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_a_line_with_a_badge_is_left_out_of_the_prose(self):
        for line in ("[![CI](https://github.com/o/r/actions/workflows/ci.yml/badge.svg)](x)",
                     "Builds run on https://github.com/o/r/actions every night",
                     "[![Coverage](https://img.shields.io/codecov/c/github/o/r)](y)"):
            with self.subTest(line=line):
                self.assertEqual(readmes.prose(f"A tool that formats files.\n{line}\n"),
                                 "A tool that formats files.")



class TestTheWording(unittest.TestCase):
    BASE = ("# Tool\n\nA tool that formats configuration files. Release 1.2.3.\n"
            "[![CI](https://ci.example/a.svg)](https://ci.example/a) See [the guide](docs/a.md).")

    def test_badges_images_link_targets_comments_whitespace_and_numbers_leave_it(self):
        changed = ("# Tool\n\n\nA tool that formats configuration files.   Release 1.2.4.\n"
                   "[![CI](https://ci.example/b.svg)](https://ci.example/b) See [the guide](docs/b.md)"
                   ". <!-- a note --> ![logo](logo.png) <img src=\"x.png\"> https://tool.example")
        self.assertEqual(readmes.wording(self.BASE), readmes.wording(changed))

    def test_a_word_more_is_another_wording(self):
        self.assertNotEqual(readmes.wording(self.BASE),
                            readmes.wording(self.BASE.replace("A tool", "A deprecated tool")))
        self.assertNotEqual(readmes.wording(self.BASE),
                            readmes.wording(self.BASE.replace("the guide", "the old guide")))

    def test_a_tag_ends_within_its_paragraph(self):
        """A "<" before a letter, as in "a<b" or C++'s "i<n", does not start a tag that runs up
        to the next ">". Read as a tag, the span would take the words in between out of the
        wording. A README that states that the project is deprecated would then keep the label
        made when the README stated that the project is maintained."""
        for before in ("Sorts so that a<b holds.\n\nThis project is actively maintained.\n\n"
                       "> Note: see the docs\n",
                       "```cpp\nfor (int i=0;i<n;i++) x();\n```\n\nThis library is actively "
                       "maintained.\n\n```cpp\nv->push();\n```\n"):
            with self.subTest(before=before[:20]):
                after = before.replace("actively maintained", "deprecated and unmaintained")
                self.assertNotEqual(readmes.wording(before), readmes.wording(after))
        self.assertEqual(readmes.wording('<p\n  align="center">A tool</p>\n<br/>'),
                         readmes.wording('<p align="left">A tool</p>\n<br />'))


class TestTheCalibrationMeasurementOfAReadme(unittest.TestCase):
    """The calibration diagnostic applies the classification's checks to each README that it
    measures: `unusable` first, and the language check on a README that passes it."""

    def measure(self, text: str) -> dict:
        import importlib.util
        import tempfile
        from unittest import mock
        path = Path(__file__).resolve().parent / "diagnostics" / "calibration_measure.py"
        spec = importlib.util.spec_from_file_location("calibration_measure", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        readme = readmes.Readme(readmes.FOUND, "o/r", revision="abc", path="README.md",
                                text=text)
        with tempfile.TemporaryDirectory() as raw, \
                mock.patch.object(module, "RAW", Path(raw)), \
                mock.patch.object(module.readmes, "fetch", return_value=readme):
            return module.measure_readme(None, {"id": 1, "name": "o/r", "revision": "abc"})

    def test_an_english_readme_is_measured_as_usable(self):
        record = self.measure("A command line tool that converts Markdown tables into CSV "
                              "files, with options for the delimiter and the quoting.")
        self.assertEqual(record["unusable"], "")
        self.assertEqual(record["language"]["detected"], "en")
        self.assertGreater(record["chars"], 0)

    def test_a_readme_that_is_not_english_is_measured_as_such(self):
        record = self.measure("Инструмент командной строки, который преобразует таблицы в "
                              "файлы с разделителями и позволяет выбрать разделитель.")
        self.assertEqual(record["unusable"], readmes.NOT_ENGLISH)


if __name__ == "__main__":
    unittest.main()
