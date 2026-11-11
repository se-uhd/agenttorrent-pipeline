"""Read a repository's README through GitHub's README API, and check that the classification
can use it.

`GET /repos/{owner}/{repo}/readme` returns the README that GitHub shows on the repository's
page. GitHub looks for it in the `.github` directory, the root and the `docs` directory, in that
order, at the commit that `ref` names. The classification names the commit that the
sampling frame recorded as the default branch's HEAD, and a label records this commit as the
revision that it was decided on. The response contains the file's path, its blob SHA and, up to
1 MB, its content in base64. Between 1 and 100 MB, only the raw media type returns the content. A request
that sends the ETag of an earlier response in `If-None-Match` gets 304 when nothing changed. A
304 to an authorized request does not count against the primary rate limit, and the monthly
request for an unchanged README therefore costs nothing against it.

GitHub returns 404 for a repository without a README, for a repository that GitHub does not have,
and for a commit that the repository no longer has, as after a force push. After a 404,
`GET /repos/{owner}/{repo}` distinguishes these cases and follows a rename. Another GitHub id
under the name is a different repository, created after the sampling frame's repository was
deleted, and counts as gone. For a repository that still exists, `fetch` requests the README of
its default branch and records no revision.

A README that the classification can use has text and is written in English. The check reads the
README that GitHub shows by default (rule 2 of the section "The reading rules" of 0_README.md). The
language check runs Lingua over the README's prose and excludes a README that it finds not to be in
English, without a request to the model. A README in another language written in ASCII can pass the
check, and the model then counts the words of the README's prose by language. From these counts,
the classification decides whether the README is mainly in English.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import re
import threading
import time
from dataclasses import dataclass

import requests

from agentcontext import logs
from agentcontext.github_api import (MAX_PAUSES, NoTokens, TokenPool, blocked, rate_limit_wait,
                                     rate_limited)

COMPONENT = "readmes"

README_URL = "https://api.github.com/repos/{name}/readme"
REPOSITORY_URL = "https://api.github.com/repos/{name}"
RATE_LIMIT_URL = "https://api.github.com/rate_limit"
GRAPHQL_URL = "https://api.github.com/graphql"
JSON = "application/vnd.github+json"
RAW = "application/vnd.github.raw+json"
API_VERSION = "2022-11-28"
TIMEOUT = (30, 120)
# Attempts at one request, and the waits between them, for a failure other than a rate limit.
ATTEMPTS = 4
WAITS = (10, 30, 60)
# The waits in seconds between checks of a GitHub access token while a check returns no verdict,
# as after a failed request, HTTP 5xx or a rate limit. Only HTTP 401 rejects a GitHub access
# token. GROUNDING: the policy of the sampling stage's query, `CHECK_WAITS` of
# ../1_sampling/2_query_github.py.
CHECK_WAITS = (10, 60, 5 * 60)
# The waits in seconds before each new check of the configured GitHub access tokens once GitHub
# has rejected every one of them. GitHub can reject valid GitHub access tokens for a while, and
# its errors and the outages of the network last for periods rather than single requests. The
# waits therefore grow from minutes to hours, and a run stops only when GitHub rejects every
# configured GitHub access token after the last wait. GROUNDING: the policy of the sampling
# stage's query, `ROUND_WAITS` of ../1_sampling/2_query_github.py.
RECHECK_WAITS = (5 * 60, 15 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60)

FOUND, UNCHANGED, NO_README, GONE = "found", "unchanged", "no_readme", "gone"

# The reasons why a repository is left out before any model reads its README.
NO_README_FOUND = "no_readme_found"
EMPTY_README = "empty_readme"
NOT_ENGLISH = "readme_non_english"
NOT_FOUND_OR_NO_ACCESS = "not_found_or_no_access"

# The thresholds of the language check. Prose with an ASCII share of at least ASCII_ENGLISH is
# English. Otherwise, prose of fewer than MIN_LETTERS letters is English when Lingua names English
# or gives it a confidence of at least SHORT_ENGLISH_CONFIDENCE, or when the ASCII share is at least
# SHORT_ASCII_ENGLISH. Longer prose is English when Lingua names English or gives it at least
# MIN_ENGLISH_CONFIDENCE, or when Lingua names no language and the ASCII share is at least
# SHORT_ASCII_ENGLISH. GROUNDING: the batch classification's thresholds, measured against the
# model's judgment over the READMEs of the calibration sample (rule 2 of the section "The reading
# rules" of 0_README.md, and readmes-spec.md for the counts). The model judged every README that the
# check excluded not mainly English. The ASCII rule keeps English READMEs that Lingua alone
# misreads, such as gchq/CyberChef as Yoruba. The model's response identifies the READMEs that pass
# the check and are not mainly English, and stricter thresholds that excluded them would be fitted
# to the READMEs of one sample.
MIN_LETTERS = 60
MIN_ENGLISH_CONFIDENCE = 0.70
SHORT_ENGLISH_CONFIDENCE = 0.50
ASCII_ENGLISH = 0.90
SHORT_ASCII_ENGLISH = 0.85
LANGUAGE_CHECK = {"min_letters": MIN_LETTERS, "min_english_confidence": MIN_ENGLISH_CONFIDENCE,
                  "short_english_confidence": SHORT_ENGLISH_CONFIDENCE,
                  "ascii_english": ASCII_ENGLISH, "short_ascii_english": SHORT_ASCII_ENGLISH}


class NoResponse(Exception):
    """GitHub returned no usable response to a request after every attempt."""


@dataclass
class Readme:
    """GitHub's response about one repository's README, and the commit that it was read at, or
    "" for the default branch at the time of the request."""

    status: str
    name: str
    revision: str = ""
    path: str = ""
    sha: str = ""
    etag: str = ""
    size: int = 0
    text: str | None = None


class Client:
    """Requests to GitHub's REST API, and queries of its GraphQL API, through the pool of GitHub
    access tokens.

    `configured` names the GitHub access tokens that `_refill` checks again once GitHub has
    rejected every GitHub access token of the pool. It defaults to the pool's GitHub access
    tokens as given."""

    def __init__(self, pool: TokenPool, session=None, sleep=None, configured=None):
        self.pool, self.session = pool, session or requests
        self.sleep = sleep or time.sleep
        self.lock = threading.Lock()
        self.requests = 0
        # The count of the responses that GitHub returned and the client passed on. The
        # classification's pause for GitHub reads it to tell that GitHub responds again
        # (`GitHubPause` of 1_classify_repos.py). A failed request, HTTP 5xx, a rate limit and
        # the response to the request of `responds` do not count.
        self.responses = 0
        # The time at which the GraphQL rate limit resets, keyed by the value of each GitHub
        # access token that GitHub reported at that limit. The pool tracks the REST API's rate
        # limit.
        self.graphql_reset: dict[str, float] = {}
        self.configured = list(pool.tokens if configured is None else configured)
        # One request at a time waits and checks the configured GitHub access tokens again, and
        # the requests of the other workers wait for its result.
        self.recheck = threading.Lock()
        # Whether GitHub rejected every configured GitHub access token after the last of
        # RECHECK_WAITS. Every later request then raises NoTokens at once, so that the requests
        # of the other workers do not wait through the checks again before the run stops.
        self.rejected = False
        # The URL of the README request that GitHub served last and the README's ETag, which
        # `served` records and `responds` sends again.
        self.last_readme: tuple[str, str] | None = None

    def _wait(self, attempt: int) -> int:
        """Wait before the next attempt, but not after the last, and return the attempts made."""
        if attempt + 1 < ATTEMPTS:
            self.sleep(WAITS[min(attempt, len(WAITS) - 1)])
        return attempt + 1

    def get(self, url: str, accept: str = JSON, etag: str = ""):
        """GitHub's response to one GET, waiting out a rate limit with another GitHub access
        token or a pause, or `NoResponse` after ATTEMPTS failures of another kind.

        Up to MAX_PAUSES rate limits do not count as failed attempts. A rate limit ends, and a
        response that keeps reporting one therefore has another cause.
        """
        return self._passed_on(self._request(url, accept, etag))

    def query(self, query: str, variables: dict):
        """GitHub's response to one query of its GraphQL API, with the retries of `get`. Its
        responses do not update the pool's count of the REST API's requests, because the GraphQL
        API has a rate limit of its own. GitHub reports that a GitHub access token reached the
        GraphQL rate limit with HTTP 200, an error of type RATE_LIMITED and no data. The query
        then continues with another GitHub access token or waits until the rate limit resets,
        because the error, taken for the result, would report that a mirror merged no pull
        request."""
        return self._passed_on(self._request(GRAPHQL_URL, JSON, "",
                                             {"query": query, "variables": variables}))

    def served(self, url: str, etag: str) -> None:
        """Record a README request that GitHub served, `url`, with the ETag of the README, for
        `responds`."""
        if etag:
            with self.lock:
                self.last_readme = (url, etag)

    def served_earlier(self, name: str, decision: dict) -> bool:
        """While this client has served no README, take the README of `decision`, an earlier
        decision on the repository `name`, as the request that `responds` sends, and return
        whether the decision names one. GitHub served that README at the revision and with the
        ETag that the decision records."""
        etag = decision.get("readme_etag") or ""
        with self.lock:
            if not etag or self.last_readme is not None:
                return False
            self.last_readme = (readme_url(name, decision.get("revision") or ""), etag)
        return True

    def responds(self) -> bool:
        """Whether GitHub returns a usable response to the README request that it served last,
        sent again with the README's ETag, with the attempts and waits of `get`. GitHub returns
        HTTP 304 while the README is unchanged, and a 304 costs nothing against the rate limit.
        Before GitHub has served a README, the request is `GET /rate_limit`, which costs nothing
        either. The classification's pause for GitHub sends the request when GitHub fails a
        repository on its own (`GitHubPause` of 1_classify_repos.py).

        A README request reads a repository's files, as do the requests that fail in an outage of
        GitHub's storage of repositories. `/rate_limit` reads none, and GitHub can respond to it
        while it fails every README request, in which case the pause would never begin. Raises
        NoTokens as `get` does. The response does not count among `responses`. While GitHub fails
        the requests for every other README, a counted response to this request would make each
        failure of another repository the first since GitHub's last response, and the pause for
        GitHub would never begin."""
        with self.lock:
            url, etag = self.last_readme or (RATE_LIMIT_URL, "")
        try:
            self._request(url, JSON, etag)
        except NoResponse:
            return False
        return True

    def _take(self) -> str:
        """The pool's next GitHub access token. When GitHub has rejected every GitHub access token
        of the pool, `_refill` checks the configured GitHub access tokens again after each of
        RECHECK_WAITS, and the pool continues with the GitHub access tokens that GitHub does not
        reject. Raises NoTokens when GitHub rejects all of them after the last wait."""
        while True:
            try:
                return self.pool.take()
            except NoTokens:
                self._refill()

    def _refill(self) -> None:
        """Fill the empty pool with the configured GitHub access tokens that GitHub does not
        reject, checked after each of RECHECK_WAITS, or raise NoTokens. Only HTTP 401 rejects a
        GitHub access token (`refused`). A check that fails keeps the GitHub access token, and the
        requests made with it handle the failure in their own attempts."""
        with self.recheck:
            if self.pool.tokens:
                return
            if self.rejected or not self.configured:
                raise NoTokens("GitHub accepts none of the configured GitHub access tokens")
            for wait in RECHECK_WAITS:
                logs.warning(COMPONENT, f"GitHub rejects every GitHub access token (HTTP 401). "
                                        f"Checking them again in {wait / 60:.0f} minutes.")
                self.sleep(wait)
                kept = [token for token in self.configured if not self.refused(token.value)]
                if kept:
                    self.pool = TokenPool(kept, clock=self.pool.clock, sleep=self.pool.sleep)
                    logs.info(COMPONENT, f"GitHub accepts {len(kept):,} of the "
                                         f"{len(self.configured):,} configured GitHub access "
                                         f"tokens again. Continuing the requests with them.")
                    return
            self.rejected = True
            raise NoTokens(f"GitHub accepts none of the configured GitHub access tokens, also "
                           f"after {len(RECHECK_WAITS)} further checks")

    def _graphql_token(self) -> str:
        """The pool's next GitHub access token below its GraphQL rate limit, waiting for the
        earliest reset when every GitHub access token has reached it."""
        while True:
            spent = []
            for _ in range(max(1, len(self.pool.tokens))):
                token = self._take()
                if self.graphql_reset.get(token, 0.0) <= self.pool.clock():
                    return token
                spent.append(token)
            self.sleep(max(1.0, min(self.graphql_reset[t] for t in spent) - self.pool.clock()))

    def _graphql_spent(self, token: str, response) -> bool:
        """Whether a GraphQL response reports that `token` reached its rate limit, and if so,
        record when the rate limit resets."""
        try:
            body = response.json()
        except ValueError:
            return False
        if not isinstance(body, dict) or not rate_limited(body):
            return False
        reset = str(response.headers.get("X-RateLimit-Reset") or "")
        with self.lock:
            first = self.graphql_reset.get(token, 0.0) <= self.pool.clock()
            self.graphql_reset[token] = (float(reset) if reset.isdigit()
                                         else self.pool.clock() + 60.0)
        if first:
            logs.warning(COMPONENT, f"A GitHub access token reached its GraphQL rate limit. Its "
                                    f"GraphQL queries wait "
                                    f"{self.graphql_reset[token] - self.pool.clock():.0f}s.")
        return True

    def refused(self, token: str) -> bool:
        """Whether GitHub also refuses `token` for a request of its rate limit, which costs
        nothing against the rate limit. GitHub can return HTTP 401 for a GitHub access token that
        it still accepts, and a GitHub access token dropped on such a response would stay out of
        the pool until the configured GitHub access tokens are checked again. A failed request is
        no evidence about the GitHub access token, which stays in the pool."""
        try:
            response = self.session.get(RATE_LIMIT_URL, timeout=TIMEOUT,
                                        headers={"Authorization": f"bearer {token}",
                                                 "Accept": JSON,
                                                 "X-GitHub-Api-Version": API_VERSION})
        except requests.RequestException:
            return False
        with self.lock:
            self.requests += 1
        return response.status_code == 401

    def _passed_on(self, response):
        """`response`, counted among the responses that the client passes on."""
        with self.lock:
            self.responses += 1
        return response

    def _request(self, url: str, accept: str, etag: str, payload: dict | None = None):
        """A GET, or a POST of `payload` when one is given, without counting the response among
        `responses`."""
        attempt = pauses = 0
        last = ""
        while attempt < ATTEMPTS:
            token = self._take() if payload is None else self._graphql_token()
            headers = {"Authorization": f"bearer {token}", "Accept": accept,
                       "X-GitHub-Api-Version": API_VERSION}
            if etag:
                headers["If-None-Match"] = etag
            try:
                if payload is None:
                    response = self.session.get(url, headers=headers, timeout=TIMEOUT)
                else:
                    response = self.session.post(url, headers=headers, json=payload,
                                                 timeout=TIMEOUT)
            except requests.RequestException as error:
                last = f"the request for {url} failed ({error})"
                attempt = self._wait(attempt)
                continue
            with self.lock:
                self.requests += 1
            if payload is None:
                self.pool.update(token, response.headers.get("X-RateLimit-Remaining"),
                                 response.headers.get("X-RateLimit-Reset"))
            status = response.status_code
            if status == 401:
                if self.refused(token):
                    logs.warning(COMPONENT, "GitHub rejected a GitHub access token (HTTP 401), "
                                            "also for its rate limit. Continuing without it.")
                    self.pool.drop(token)
                    continue
                last = (f"GitHub returned HTTP 401 for {url} with a GitHub access token that it "
                        f"accepts for its rate limit")
                attempt = self._wait(attempt)
                continue
            if status in (403, 429):
                wait = rate_limit_wait(response)
                if wait is not None:
                    if self.pool.pause(token, wait):
                        paused = self.pool.pausing(token)
                        logs.warning(COMPONENT, f"GitHub returned HTTP {status} for a rate "
                                                f"limit. {paused[:1].upper()}{paused[1:]} for "
                                                f"{wait:.0f}s.")
                    pauses += 1
                    if pauses > MAX_PAUSES:
                        attempt += 1
                    continue
                return response
            if status >= 500:
                last = f"GitHub returned HTTP {status} for {url}"
                attempt = self._wait(attempt)
                continue
            if payload is not None and status == 200 and self._graphql_spent(token, response):
                last = f"a GitHub access token reached its GraphQL rate limit at {url}"
                pauses += 1
                if pauses > MAX_PAUSES:
                    attempt += 1
                continue
            return response
        raise NoResponse(last)


def check_token(session, value: str) -> tuple:
    """Whether GitHub accepts a GitHub access token, from a request of `/rate_limit`, which
    costs nothing against the rate limit. The first item is True for HTTP 200, False for HTTP
    401 and None for no verdict, as after a failed request, HTTP 5xx or a rate limit. The second
    item names what GitHub returned."""
    try:
        response = session.get(RATE_LIMIT_URL, timeout=TIMEOUT,
                               headers={"Authorization": f"bearer {value}"})
    except requests.RequestException as error:
        return None, str(error)
    status = response.status_code
    return {200: True, 401: False}.get(status), f"HTTP {status}"


def accepted_tokens(tokens: list, session=None, sleep=None) -> list:
    """The GitHub access tokens that GitHub does not reject, as `Credential`s, in the order given.

    Only HTTP 401 rejects a GitHub access token. A check without a verdict is made again after
    each of CHECK_WAITS, and a GitHub access token that never gets a verdict is kept, because a
    failed request, HTTP 5xx or a rate limit is no evidence about the GitHub access token. If
    each such check dropped its GitHub access token, a minute without the network as the run
    starts would leave no GitHub access token, and the run would end before its first request.
    """
    session, sleep = session or requests, sleep or time.sleep
    verdicts = {}
    pending = list(range(len(tokens)))
    for wait in (*CHECK_WAITS, None):
        unchecked = []
        for index in pending:
            token = tokens[index]
            verdict, detail = check_token(session, token.value)
            label = f"GitHub access token {index + 1} of {len(tokens):,}"
            account = f"account {token.account}" if token.account else "no account named"
            if verdict is None:
                unchecked.append(index)
                logs.warning(COMPONENT, f"GitHub gave no verdict on {label} ({account}, "
                                        f"{detail}). " + (f"Checking again in {wait:.0f}s."
                                                          if wait is not None else "Kept it."))
                continue
            verdicts[index] = verdict
            if not verdict:
                logs.warning(COMPONENT, f"{label} ({account}) failed validation ({detail}).")
        if not unchecked or wait is None:
            break
        sleep(wait)
        pending = unchecked
    return [token for index, token in enumerate(tokens) if verdicts.get(index) is not False]


def _returned_name(body: dict, requested: str) -> str:
    """The repository's name in a response's `url`, which names the repository that a rename
    led to."""
    match = re.search(r"/repos/([^/]+/[^/?]+)/", str(body.get("url") or ""))
    return match.group(1) if match else requested


def readme_url(name: str, revision: str) -> str:
    return README_URL.format(name=name) + (f"?ref={revision}" if revision else "")


def fetch(client: Client, name: str, repository_id: int, revision: str = "",
          etag: str = "") -> Readme:
    """The README of repository `name`, with the GitHub id `repository_id`, at `revision`.

    With `etag`, a README unchanged since the response that contained it is returned as
    `UNCHANGED`, without its text.
    """
    url = readme_url(name, revision)
    response = client.get(url, etag=etag)
    status = response.status_code
    if status == 304:
        client.served(url, etag)
        return Readme(UNCHANGED, name, revision, etag=etag)
    if status == 200:
        client.served(url, response.headers.get("ETag", ""))
        body = response.json()
        size = int(body.get("size") or 0)
        content = body.get("content") or ""
        if body.get("encoding") == "base64" and (content or size == 0):
            data = base64.b64decode(content)
        else:
            raw = client.get(url, accept=RAW)
            if raw.status_code != 200:
                raise NoResponse(f"GitHub returned HTTP {raw.status_code} for the raw content of "
                                 f"the README of {name}")
            data = raw.content
        text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        return Readme(FOUND, _returned_name(body, name), revision,
                      path=str(body.get("path") or ""), sha=str(body.get("sha") or ""),
                      etag=response.headers.get("ETag", ""), size=size, text=text)
    if status == 451 or blocked(response):
        return Readme(GONE, name, revision)
    if status != 404:
        raise NoResponse(f"GitHub returned HTTP {status} for the README of {name}")
    repository = client.get(REPOSITORY_URL.format(name=name))
    if repository.status_code in (404, 451) or blocked(repository):
        return Readme(GONE, name, revision)
    if repository.status_code != 200:
        raise NoResponse(f"GitHub returned HTTP {repository.status_code} for the repository "
                         f"{name}")
    body = repository.json()
    if int(body.get("id") or 0) != int(repository_id):
        return Readme(GONE, name, revision)
    current = str(body.get("full_name") or name)
    if current.lower() != name.lower():
        return fetch(client, current, repository_id, revision, etag)
    if revision:
        return fetch(client, current, repository_id, "", "")
    return Readme(NO_README, current)


def repository(client: Client, name: str) -> dict:
    """GitHub's response for the repository, `GET /repos/{owner}/{repo}`, which contains its
    creation time and follows a rename."""
    response = client.get(REPOSITORY_URL.format(name=name))
    if response.status_code != 200:
        raise NoResponse(f"GitHub returned HTTP {response.status_code} for the repository "
                         f"{name}")
    body = response.json()
    if not isinstance(body, dict) or "full_name" not in body:
        raise NoResponse(f"GitHub's response for the repository {name} has no `full_name`")
    return body


def unusable(readme: Readme) -> str:
    """Why the classification cannot use a README before its language is checked, or "" for a
    README that it can use."""
    if readme.status == GONE:
        return NOT_FOUND_OR_NO_ACCESS
    if readme.status == NO_README:
        return NO_README_FOUND
    if not (readme.text or "").strip():
        return EMPTY_README
    return ""


# The Markdown removed before the language is detected: what a README contains in any language.
MD_FENCE_RE = re.compile(r"(^|\n)```[\s\S]*?```", re.MULTILINE)
MD_TILDE_FENCE_RE = re.compile(r"(^|\n)~~~[\s\S]*?~~~", re.MULTILINE)
MD_INLINE_CODE_RE = re.compile(r"`[^`]+`")
MD_TABLE_LINE_RE = re.compile(r"^\s*\|.*\|\s*$")
MD_TABLE_SEP_RE = re.compile(r"^\s*:?[-=]{2,}:?\s*(\|\s*:?[-=]{2,}:?\s*)+$")
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
HTML_TAG_RE = re.compile(r"<[^>]+>")
# An Actions badge links `github.com/<owner>/<repo>/actions`, two segments without a slash. With a
# segment of any text, the search tries every pair of later slashes, and on a line of many
# github.com links, such as a list with `<br>` tags turned into spaces, it holds a worker for a
# time that grows with the square of the line's links.
MD_BADGE_RE = re.compile(r"\b(shields\.io|badge|travis-ci|github\.com/[^\s/]+/[^\s/]+/actions)\b",
                         re.IGNORECASE)


def prose(text: str) -> str:
    """The README's prose: code, images, link targets, HTML tags, tables and badge lines removed,
    whitespace collapsed."""
    t = MD_FENCE_RE.sub("\n", text or "")
    t = MD_TILDE_FENCE_RE.sub("\n", t)
    t = MD_INLINE_CODE_RE.sub(" ", t)
    t = MD_IMAGE_RE.sub(" ", t)
    t = MD_LINK_RE.sub(r"\1", t)
    t = HTML_TAG_RE.sub(" ", t)
    lines = [line for line in t.splitlines()
             if not (MD_TABLE_LINE_RE.match(line) or MD_TABLE_SEP_RE.match(line)
                     or MD_BADGE_RE.search(line))]
    return re.sub(r"\s+", " ", "\n".join(lines)).strip()


# The parts of a README that its wording leaves out. GROUNDING: classify-repos-spec.md counts the
# READMEs that changed between two runs only in these parts or only in numbers. The other changed
# READMEs changed words. The wording keeps every word, because one added word such as
# "deprecated" can change a label.
WORDING_COMMENT = re.compile(r"<!--.*?-->", re.S)
WORDING_BADGE = re.compile(r"\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")
WORDING_IMAGE = re.compile(r"!\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])")
WORDING_HTML_IMAGE = re.compile(r"<img\b[^>]*>", re.I)
WORDING_REFERENCE = re.compile(r"^\s*\[[^\]]+\]:\s*\S+.*$", re.M)
WORDING_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# An HTML tag: its name, then its attributes up to its ">". The attributes may span a line break
# but not a blank line, because a tag in Markdown cannot. A pattern that took anything from "<"
# and a letter to the next ">" would read "a<b holds." or C++'s "i<n" as a tag and leave out every
# word up to the ">" of a block quote paragraphs later. A README that changed "maintained" to
# "deprecated" in those words would then keep its label.
WORDING_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?=[\s/>])(?:(?!\n[ \t]*\n)[^<>])*>")
WORDING_URL = re.compile(r"https?://\S+")


def wording(text: str) -> str:
    """The SHA-256 of a README's lowercase words, without the parts that WORDING_* matches and
    without numbers."""
    text = WORDING_COMMENT.sub(" ", text or "")
    for pattern in (WORDING_BADGE, WORDING_IMAGE, WORDING_HTML_IMAGE, WORDING_REFERENCE):
        text = pattern.sub(" ", text)
    text = WORDING_URL.sub(" ", WORDING_TAG.sub(" ", WORDING_LINK.sub(r"\1", text)))
    words = [word for word in re.findall(r"\w+", text.lower()) if not word.isdigit()]
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()


_detector = None
_detector_lock = threading.Lock()


def detector():
    """Lingua's detector over every language that Lingua supports, built once because building it
    is slow."""
    global _detector
    with _detector_lock:
        if _detector is None:
            from lingua import LanguageDetectorBuilder
            _detector = LanguageDetectorBuilder.from_all_languages().build()
        return _detector


@functools.cache
def lingua_version() -> str:
    """The installed Lingua's version, recorded with the language check, since Lingua's models
    determine the check's result. The version is read once, because it does not change while
    the process runs, and the check of every reused decision records it."""
    from importlib import metadata
    try:
        return metadata.version("lingua-language-detector")
    except metadata.PackageNotFoundError:
        return ""


def language_check() -> dict:
    """The language check as each decision records it: the thresholds and the version of Lingua
    that applies them. New thresholds apply to the measures that a decision recorded, and a new
    version of Lingua makes the classification check the README again
    (`1_classify_repos.Run.carry`)."""
    return {**LANGUAGE_CHECK, "lingua": lingua_version()}


def language(text: str) -> dict:
    """What the language check reads off a README's prose: its characters, the share of them that
    is ASCII, its letters, the language that Lingua detects as an ISO 639-1 code, or None, and the
    confidence that Lingua gives English. A decision records them so that the check can be
    computed again under other thresholds."""
    from lingua import Language
    cleaned = prose(text)
    if not cleaned:
        return {"prose_chars": 0}
    found = detector().detect_language_of(cleaned)
    return {"prose_chars": len(cleaned),
            "ascii_share": round(sum(1 for ch in cleaned if ord(ch) < 128) / len(cleaned), 4),
            "letters": sum(1 for ch in cleaned if ch.isalpha()),
            "detected": found.iso_code_639_1.name.lower() if found is not None else None,
            "english_confidence": round(
                detector().compute_language_confidence(cleaned, Language.ENGLISH), 4)}


# The names of natural languages that the model's counts use and that Lingua does not list: Lingua
# lists Norwegian as Bokmal and Nynorsk, Slovenian as Slovene and Bangla as Bengali, and it does not
# detect Sanskrit and Sinhala.
OTHER_LANGUAGE_NAMES = ("bangla", "norwegian", "sanskrit", "sinhala", "slovenian")
# A language name with a qualifier in parentheses, as "Chinese (Simplified)", and an ISO code with
# a script or a region, as "zh-Hans", "ja-JP" or "zh_CN". A script has four letters and a region
# has two letters or three digits, so that a word after a code, as in "so-called", is no region.
QUALIFIED_LANGUAGE = re.compile(r"(.+?)\s*\([^()]*\)")
LANGUAGE_SUBTAG = r"[-_](?:[a-z]{4}|[a-z]{2}|[0-9]{3})"
LANGUAGE_CODE = re.compile(rf"([a-z]{{2,3}})(?:{LANGUAGE_SUBTAG})*")


@functools.cache
def _language_names() -> frozenset:
    """The lowercase English names and ISO 639-1 and 639-3 codes of the languages that Lingua
    supports, with the names that it does not list."""
    from lingua import Language
    names = set(OTHER_LANGUAGE_NAMES)
    for language in Language.all():
        names.update((language.name, language.iso_code_639_1.name, language.iso_code_639_3.name))
    return frozenset(name.lower() for name in names)


def names_language(name: str) -> bool:
    """Whether the name that a model's count gives names a natural language, with or without a
    qualifier in parentheses or a script or a region, such as "zh-Hans". A name in another form,
    such as "Englisch" or "Chinese/English", does not, because the classification cannot tell
    which language it stands for."""
    name = name.strip().lower()
    qualified = QUALIFIED_LANGUAGE.fullmatch(name)
    code = LANGUAGE_CODE.fullmatch(name)
    return (name in _language_names() or bool(qualified and qualified[1] in _language_names())
            or bool(code and code[1] in _language_names()))


def english(measures: dict) -> bool:
    """Whether the language check finds a README in English, from what `language` read off it."""
    if not measures.get("prose_chars"):
        return True
    detected, confidence = measures["detected"], measures["english_confidence"]
    share = measures["ascii_share"]
    if share >= ASCII_ENGLISH:
        return True
    if measures["letters"] < MIN_LETTERS:
        return (detected == "en" or confidence >= SHORT_ENGLISH_CONFIDENCE
                or share >= SHORT_ASCII_ENGLISH)
    if detected == "en" or confidence >= MIN_ENGLISH_CONFIDENCE:
        return True
    return detected is None and share >= SHORT_ASCII_ENGLISH
