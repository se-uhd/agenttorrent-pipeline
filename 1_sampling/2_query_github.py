"""Query GitHub for every repository in a GHS retrieval, and write GitHub's responses into the
cache of retrievals beside the retrieval's rows.

Because GHS refreshes a repository only when it crawls a push to it, a repository's archived,
disabled and locked flags, its license and its name in GHS stay as they were at the repository's
last push. A renamed or transferred repository gets a second row under the new name
while the old row stays, and GHS writes every timestamp on a 12-hour clock without AM/PM
(`JacksonConfig.java:28`). GitHub returns each of these values as it is now. For every row of
the retrieval that `ghs_retrieval.json` names, this script queries GitHub's GraphQL API,
`BATCH_SIZE` repositories per request. It requests the repository's id, its current name, its
description, its timestamps in UTC, its archived, disabled, locked, fork, private and mirror
flags, its license, its primary language, its stars and its default branch with the HEAD commit
and the time that it was committed.

For a row that GitHub has no repository for under GHS's name, the script also queries the REST
API, which follows the redirect that GitHub keeps for a renamed or transferred repository. It
then queries the name that REST returns through GraphQL like any other name. A row is then
`found`, `not_found` (deleted, private or never there) or `blocked` (GitHub blocks access, as
after a DMCA takedown).

The rows that are still without a response or a REST check after the first pass are queried
again in further rounds, after waits that grow from minutes to hours (`ROUND_WAITS`), because
GitHub's errors and an outage of the network come in spells. A pass ends early when
`NO_RESPONSE_BATCHES` batches or REST checks in a row get no response, and it leaves its other
rows to the next round. The script sends no request and starts no round after `QUERY_SECONDS`.
A failure that every request meets therefore ends the run after the rounds. A row that no round
settles is `unresolved`. When the unresolved rows are at most `UNRESOLVED_MAX_SHARE` of the
retrieval, or a single row, the script records them and exits 0, and the filter's gone step
removes them. Above that, it exits 1 and leaves them to the next run.

Every batch of responses is appended to `<id>.github.partial.jsonl` in the cache, so that a run
that stops resumes where it stopped. When every row has a response, the script writes the
responses as `<id>.github.jsonl.xz` in GHS's order and removes the partial file. The index
records the requests, their cost and how many repositories GitHub found. A retrieval that
already has GitHub's responses is left alone.

The GitHub access tokens come from GITHUB_TOKENS, comma-separated, GITHUB_TOKEN and
`credentials.csv` (`agentcontext.credentials`). `agentcontext.github_api` defines the pool, the
rate limits and the pause of an account.

Usage:
  python3 2_query_github.py
  python3 2_query_github.py --workers 2 --batch-size 50
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

import requests
from agentcontext import credentials, logs
from agentcontext.github_api import (MAX_PAUSES, NoTokens, TokenPool, blocked, rate_limit_wait,
                                     rate_limited)

import ghs_cache
from ghs_cache import CacheError, upper_first

COMPONENT = "github_query"

GRAPHQL_URL = "https://api.github.com/graphql"
REST_URL = "https://api.github.com/repos/{name}"
RATE_LIMIT_URL = "https://api.github.com/rate_limit"
TIMEOUT = (30, 120)

# GitHub ends a GraphQL query that runs too long with HTTP 502. With the default branch's HEAD
# among the fields, a batch of this size finishes in time, and query-github-spec.md gives the
# measurements.
BATCH_SIZE = 50
# The batch sizes that --batch-size accepts.
BATCH_SIZE_MIN = 1
BATCH_SIZE_MAX = 100
WORKERS = 4
# Attempts at one batch, and at one REST check, before the script leaves its rows to the next
# round, and the waits in seconds between the attempts.
ATTEMPTS = 4
WAITS = (10, 30, 60)
# The waits in seconds before each further round over the rows that are still without a
# response or a REST check. GitHub's errors and outages of the network come in spells. The
# waits grow from minutes to hours, and a spell that ends within them leaves no row unresolved.
# The same waits come before each new check of the GitHub access tokens when GitHub rejects all
# of them, at the start of the query or later. They start again with each spell of such
# rejections that follows a request that GitHub served.
ROUND_WAITS = (5 * 60, 15 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60)
# The number of batches and REST checks in a row without a response after which a pass ends and
# leaves its other rows to the next round, three for each of the WORKERS. Without this limit, a
# failure that every request meets, such as a refused connection, HTTP 503 or a field that
# GitHub removed from the schema, would spend the attempts and waits of every batch of the
# retrieval in each pass. A spell that fails about three batches of every worker ends the pass,
# and a shorter spell ends none.
NO_RESPONSE_BATCHES = 12
# The seconds from the start of a run after which the query sends no request and starts no
# round. A failure that leaves some responses in every pass ends no pass early, and this limit
# ends such a query. Because the limit is well beyond a first pass and the waits of ROUND_WAITS
# together, it ends only a query that GitHub keeps failing. The next run resumes from the
# partial file.
QUERY_SECONDS = 24 * 60 * 60
# The share of the retrieval's rows that may stay unresolved after the last round, or a single
# row in a retrieval too small for the share to allow one. Up to this share, the query records
# the unresolved rows and exits 0, so that the stages after it run. Above it, the query exits 1,
# because that many rows without a response point to a failure that the next run should query
# again rather than to rows that the sampling frame can do without.
UNRESOLVED_MAX_SHARE = 0.001
# The waits in seconds between checks of a GitHub access token that returned no verdict, such as
# a failed request, HTTP 5xx or a rate limit. Only HTTP 401 rejects a GitHub access token.
CHECK_WAITS = (10, 60, 5 * 60)

FOUND, NOT_FOUND, BLOCKED, UNRESOLVED = "found", "not_found", "blocked", "unresolved"

FRAGMENT = """
fragment R on Repository {
  databaseId nameWithOwner description createdAt pushedAt updatedAt
  isArchived isDisabled isLocked isFork isPrivate isMirror
  licenseInfo { spdxId name }
  primaryLanguage { name }
  stargazerCount
  defaultBranchRef { name target { oid ... on Commit { committedDate } } }
}
"""


def paused(tokens: TokenPool, token: str) -> str:
    """What a pause of `token` sets aside (`TokenPool.pausing`), as the start of a sentence."""
    text = tokens.pausing(token)
    return text[:1].upper() + text[1:]


class Transient(Exception):
    """A failed request that another attempt may complete."""

    def __init__(self, message: str, wait: float | None = None, split: bool = False):
        super().__init__(message)
        self.wait, self.split = wait, split


def flatten(repository: dict) -> dict:
    """GitHub's response for one repository, as the flat record that the cache keeps."""
    branch = repository.get("defaultBranchRef") or {}
    target = branch.get("target") or {}
    license_info = repository.get("licenseInfo") or {}
    return {
        "id": repository.get("databaseId"),
        "name": repository.get("nameWithOwner"),
        "description": repository.get("description"),
        "createdAt": repository.get("createdAt"),
        "pushedAt": repository.get("pushedAt"),
        "updatedAt": repository.get("updatedAt"),
        "isArchived": repository.get("isArchived"),
        "isDisabled": repository.get("isDisabled"),
        "isLocked": repository.get("isLocked"),
        "isFork": repository.get("isFork"),
        "isPrivate": repository.get("isPrivate"),
        "isMirror": repository.get("isMirror"),
        "license": license_info.get("name"),
        "licenseSpdxId": license_info.get("spdxId"),
        "primaryLanguage": (repository.get("primaryLanguage") or {}).get("name"),
        "stargazers": repository.get("stargazerCount"),
        "defaultBranch": branch.get("name"),
        "headSha": target.get("oid"),
        "headCommittedAt": target.get("committedDate"),
    }


def complete(response: dict) -> bool:
    """Whether a response contains every field that the query requests. A response recorded
    before a field was added to the query lacks that field."""
    return response["status"] != FOUND or set(flatten({})) <= set(response["repository"])


def batch_query(names: list) -> tuple:
    """The GraphQL document and variables that request `names`, aliased r0, r1, ..."""
    declarations, fields, variables = [], [], {}
    for index, name in enumerate(names):
        owner, _, repo = name.partition("/")
        declarations.append(f"$o{index}: String!, $n{index}: String!")
        fields.append(f"  r{index}: repository(owner: $o{index}, name: $n{index}) {{ ...R }}")
        variables[f"o{index}"], variables[f"n{index}"] = owner, repo
    document = (f"query({', '.join(declarations)}) {{\n  rateLimit {{ cost remaining resetAt }}\n"
                + "\n".join(fields) + "\n}\n" + FRAGMENT)
    return document, variables


# The key of a response that lists the fields of a found repository that GitHub could not
# resolve. No row of the cache keeps the key. GitHub returns such a field as null, together
# with an error that names the field in its path, as on a timeout of the field or a failure of
# the storage that it reads. A null license or language would remove the repository from the
# sampling frame of the month.
UNRESOLVED_FIELDS = "unresolvedFields"
# The field without which a response is not taken after the last round (`take_incomplete`).
# The filter and the stages after it identify a repository by GitHub's name, and a response
# without the name gives them nothing that they can use. Such a row stays unresolved, and the
# filter's gone step removes and counts it.
NAME_FIELD = "nameWithOwner"


def classify(names: list, body: dict) -> tuple:
    """The response for each name, or None for an error worth another attempt, and the rate
    limit. A found repository lists the fields that GitHub could not resolve under
    UNRESOLVED_FIELDS."""
    data = body.get("data") or {}
    problems, fields = {}, {}
    for error in body.get("errors") or []:
        path = error.get("path") if isinstance(error, dict) else None
        if isinstance(path, list) and path and isinstance(path[0], str) \
                and path[0].startswith("r"):
            problems[path[0]] = error
            if len(path) > 1:
                fields.setdefault(path[0], set()).add(".".join(str(part) for part in path[1:]))
    responses = []
    for index, name in enumerate(names):
        alias = f"r{index}"
        repository = data.get(alias)
        if repository:
            response = {"status": FOUND, "repository": flatten(repository)}
            if alias in fields:
                response[UNRESOLVED_FIELDS] = sorted(fields[alias])
            responses.append(response)
            continue
        error = problems.get(alias, {})
        kind, message = error.get("type"), str(error.get("message", ""))
        if kind == "NOT_FOUND":
            responses.append({"status": NOT_FOUND, "error": message})
        elif kind == "FORBIDDEN" and "blocked" in message.lower():
            responses.append({"status": BLOCKED, "error": message})
        else:
            responses.append(None)
    return responses, data.get("rateLimit") or {}


def timed_out(body: dict) -> bool:
    """Whether GitHub ended a GraphQL query for its time limit in a response with HTTP 200:
    without data for any repository and with an error of the whole query, without a path, that
    names a timeout. GitHub ends such a query with HTTP 502 as well (`post`)."""
    data = body.get("data") or {}
    if any(data.get(key) for key in data if key != "rateLimit"):
        return False
    return any(not error.get("path") and any(
        word in str(error.get("message", "")).lower() for word in ("timeout", "timed out"))
        for error in body.get("errors") or [] if isinstance(error, dict))


def _reset_time(value) -> float:
    """GraphQL's `resetAt` as epoch seconds."""
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def post(token: str, document: str, variables: dict) -> tuple:
    """Send one GraphQL request, and return its body and headers. Its failures are sorted into
    Transient, a rejected GitHub access token (PermissionError) and the rest."""
    try:
        response = requests.post(GRAPHQL_URL, json={"query": document, "variables": variables},
                                 headers={"Authorization": f"bearer {token}"}, timeout=TIMEOUT)
    except requests.RequestException as error:
        raise Transient(f"the GraphQL request failed ({error})") from error
    status = response.status_code
    if status == 401:
        raise PermissionError("GitHub rejected the GitHub access token (HTTP 401)")
    if status in (403, 429):
        wait = rate_limit_wait(response)
        if wait is not None:
            raise Transient(f"GitHub returned HTTP {status} because of a rate limit", wait=wait)
        raise Transient(f"GitHub returned HTTP {status}")
    if status in (502, 504):
        raise Transient(f"GitHub returned HTTP {status} because the query took too long",
                        split=True)
    if status != 200:
        raise Transient(f"GitHub returned HTTP {status}")
    try:
        body = response.json()
    except ValueError as error:
        raise Transient(f"GitHub's response is not JSON ({error})") from error
    if not isinstance(body, dict):
        raise Transient("GitHub's response is not a JSON object")
    if body.get("data") is not None and not isinstance(body["data"], dict):
        raise Transient("`data` in GitHub's response is not a JSON object")
    # GraphQL's specification makes `errors` a list of objects. Another value, such as a proxy's
    # text, becomes the message of an error object, because every function that reads the
    # errors, such as `rate_limited`, `classify` and `timed_out`, expects objects. An
    # AttributeError in a worker would end every run that resumes the query.
    errors = body.get("errors")
    if errors is not None:
        listed = errors if isinstance(errors, list) else [errors]
        body = {**body, "errors": [error if isinstance(error, dict) else {"message": str(error)}
                                   for error in listed if error is not None]}
    return body, response.headers


def rest_name(token: str, name: str) -> tuple:
    """The name that GitHub's REST API returns for `name`, following a rename or transfer, or
    None for a repository that GitHub does not have, and the remaining requests of the core
    rate limit. Its failures are sorted as `post` sorts them."""
    try:
        response = requests.get(REST_URL.format(name=name), timeout=TIMEOUT,
                                headers={"Authorization": f"bearer {token}",
                                         "Accept": "application/vnd.github+json"})
    except requests.RequestException as error:
        raise Transient(f"the REST request failed ({error})") from error
    if response.status_code == 401:
        raise PermissionError("GitHub rejected the GitHub access token (HTTP 401)")
    if response.status_code in (404, 451):
        return None, response.headers.get("X-RateLimit-Remaining")
    if response.status_code in (403, 429):
        wait = rate_limit_wait(response)
        if wait is not None:
            raise Transient(f"GitHub returned HTTP {response.status_code} because of a rate "
                            f"limit", wait=wait)
        if blocked(response):
            return None, response.headers.get("X-RateLimit-Remaining")
        raise Transient(f"GitHub returned HTTP {response.status_code}")
    if response.status_code != 200:
        raise Transient(f"GitHub returned HTTP {response.status_code}")
    # A body that names no repository, such as the HTML page of a proxy, is no evidence about the
    # repository. It counts as a failed attempt rather than as a repository that GitHub does not
    # have.
    try:
        body = response.json()
    except ValueError as error:
        raise Transient(f"GitHub's REST response is not JSON ({error})") from error
    current = body.get("full_name") if isinstance(body, dict) else None
    if not isinstance(current, str) or not current:
        raise Transient("GitHub's REST response names no repository")
    return current, response.headers.get("X-RateLimit-Remaining")


def check_token(value: str) -> tuple:
    """Whether GitHub accepts a GitHub access token, from a request of `/rate_limit`, which
    does not count against the rate limit. The first item is True for HTTP 200, False for
    HTTP 401 and None for no verdict, such as after a failed request, HTTP 5xx or a rate limit.
    The second item names what GitHub returned."""
    try:
        response = requests.get(RATE_LIMIT_URL, timeout=TIMEOUT,
                                headers={"Authorization": f"bearer {value}"})
    except requests.RequestException as error:
        return None, str(error)
    status = response.status_code
    return {200: True, 401: False}.get(status), f"HTTP {status}"


class Query:
    """One run over a retrieval: the batches, their responses and what they cost."""

    def __init__(self, tokens: TokenPool, partial: Path, sleep=None, save=None, clock=None,
                 deadline: float = math.inf):
        self.tokens, self.partial, self.sleep = tokens, partial, sleep or time.sleep
        self.clock = clock or time.time
        # The time of `clock` after which the query sends no request (QUERY_SECONDS).
        self.deadline = deadline
        self.write_lock = threading.Lock()
        self.requests = 0
        self.cost = 0
        # The batches and REST checks in a row that got no response, and whether they have ended
        # the pass. Every request in flight reads `ended` before it is sent.
        self.failures = 0
        self.ended = threading.Event()
        # The further rounds that the run has made.
        self.rounds = 0
        # Called with the requests and the cost so far, so that a run that is stopped leaves
        # them in the index.
        self.save = save or (lambda requests, cost: None)
        # The latest response of each repository, by name, that GitHub returned with fields that
        # it could not resolve (UNRESOLVED_FIELDS). Such a repository is queried again in the
        # attempts and the rounds, as a repository without a response is, and this response is
        # taken after the last round (`take_incomplete`).
        self.incomplete: dict = {}
        # The name that GitHub's REST API returned for each row that GraphQL did not find under
        # its GHS name, keyed by the row's position. GraphQL is then queried for the row under
        # that name (`resolve_renames`).
        self.returned: dict = {}

    def begin_pass(self) -> None:
        """Start a pass over the rows that are not settled, with no failure counted."""
        with self.write_lock:
            self.failures = 0
        self.ended.clear()

    def past_deadline(self) -> bool:
        """Whether the run has reached its time limit."""
        return self.clock() >= self.deadline

    def over(self) -> bool:
        """Whether the pass has ended early or the run has reached its time limit. Either way,
        no further request is sent in this pass."""
        return self.ended.is_set() or self.past_deadline()

    def responded(self) -> None:
        """Count a request that returned a response for at least one repository."""
        with self.write_lock:
            self.failures = 0

    def failed(self, last) -> None:
        """Count a batch or a REST check that no attempt got a response for, and end the pass
        after NO_RESPONSE_BATCHES of them in a row."""
        with self.write_lock:
            self.failures += 1
            ending = self.failures >= NO_RESPONSE_BATCHES and not self.ended.is_set()
            if ending:
                self.ended.set()
        if ending:
            logs.warning(COMPONENT, f"GitHub returned no response to {NO_RESPONSE_BATCHES} batches "
                                    f"or name checks in a row. In the last one, {last}. Ended the "
                                    f"pass and left its other rows to the next round.")

    def wait_after(self, attempt: int) -> None:
        """Wait before the attempt after failed attempt number `attempt`, unless that attempt
        was the last or the pass is over."""
        if attempt < ATTEMPTS and not self.over():
            self.sleep(WAITS[min(attempt - 1, len(WAITS) - 1)])

    def take_incomplete(self, names: list, received: dict) -> list:
        """Take into `received` the latest response for each row without a response that
        GitHub, in every round, returned only with fields that it could not resolve. Those
        fields stay empty, and the taken responses are returned with the fields. A row that
        GraphQL did not find under its GHS name takes the response under the name that REST
        returned for it (`returned`) and counts as checked by REST.

        The responses stay out of the partial file. A run that then exits 1, because more rows
        are unresolved than the share allows, would otherwise leave them to the next run as
        GitHub's responses, and the next run would not query those rows again. Without a
        response, such a row would count as unresolved, and a field that GitHub fails for one
        repository every time would end every run once such repositories exceeded
        UNRESOLVED_MAX_SHARE. A response without the repository's name (NAME_FIELD) is not
        taken, and its row counts as unresolved."""
        taken = []
        for i, name in enumerate(names):
            earlier = received.get(i)
            if earlier is None:
                response, checked = self.incomplete.get(name), {}
            elif (earlier["status"] == NOT_FOUND and not earlier.get("restChecked")
                    and i in self.returned):
                # GraphQL did not find the row under its GHS name, and REST returned the name
                # under which GraphQL returned it with fields that it could not resolve.
                current = self.returned[i]
                response = self.incomplete.get(current)
                moved = ghs_cache.name_key(current) != ghs_cache.name_key(name)
                checked = {"restChecked": True, **({"redirectedTo": current} if moved else {})}
            else:
                continue
            if response is None or NAME_FIELD in response[UNRESOLVED_FIELDS]:
                continue
            row = {"row": i, "ghsName": name,
                   **{k: v for k, v in response.items() if k != UNRESOLVED_FIELDS}, **checked}
            taken.append((row, response[UNRESOLVED_FIELDS]))
        received.update({row["row"]: row for row, _fields in taken})
        return taken

    def record(self, rows: list) -> None:
        with self.write_lock:
            with open(self.partial, "a", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")

    def rejected(self, token: str) -> bool:
        """After a request returned HTTP 401, whether `/rate_limit` rejects `token` as well. A
        rejected GitHub access token is dropped from the pool. A GitHub access token that the
        check accepts or returns no verdict on stays, because one response is not enough to drop
        a GitHub access token for the rest of the run."""
        verdict, _ = check_token(token)
        if verdict is not False:
            return False
        self.tokens.drop(token)
        logs.warning(COMPONENT, f"GitHub rejected a GitHub access token for a request and for its "
                                f"check (HTTP 401). Dropped it ({self.tokens.accounts():,} "
                                f"account(s) left).")
        return True

    def fetch(self, names: list) -> list:
        """GitHub's responses for `names`, with None for a repository that no attempt got a
        response for.

        A batch that GitHub ends because the query took too long, with HTTP 502 or 504 or with
        HTTP 200 and a timeout error (`timed_out`), is split in half, down to single
        repositories, so that a repository that GitHub cannot return in time leaves only itself
        without a response. The repositories that came back from a batch with an error are
        queried again in a batch of their own. A batch that fails in any other way is left to the
        next round after its attempts, without a split, because a failure that every request
        meets would make each part of the batch spend the attempts and waits again, down to
        single repositories.
        """
        document, variables = batch_query(names)
        last = None
        # A rate limit, an exhausted rate limit and a rejected GitHub access token are not failed
        # attempts. The request waits for a GitHub access token that GitHub accepts and is sent
        # again, up to MAX_PAUSES times. An HTTP 401 for a GitHub access token that its check
        # accepts is a failed attempt.
        attempt = pauses = 0
        # Whether GitHub returned the repositories in the last attempt, with fields that it could
        # not resolve, rather than no response.
        returned_repositories = False
        while attempt < ATTEMPTS:
            if self.over():
                return [None] * len(names)
            token = self.tokens.take(self.ended)
            if token is None:
                return [None] * len(names)
            try:
                body, headers = post(token, document, variables)
            except PermissionError as error:
                if self.rejected(token):
                    continue
                last = Transient(str(error))
                attempt += 1
                self.wait_after(attempt)
                continue
            except Transient as error:
                last = error
                if error.wait is not None:
                    if self.tokens.pause(token, error.wait):
                        logs.warning(COMPONENT, f"{error}. {paused(self.tokens, token)} for "
                                                f"{error.wait:.0f}s.")
                    pauses += 1
                    if pauses > MAX_PAUSES:
                        attempt += 1
                    continue
                attempt += 1
                if error.split and len(names) > 1:
                    middle = len(names) // 2
                    return self.fetch(names[:middle]) + self.fetch(names[middle:])
                self.wait_after(attempt)
                continue
            with self.write_lock:
                self.requests += 1
            if rate_limited(body):
                self.tokens.update(token, 0, float(headers.get("X-RateLimit-Reset") or 0))
                last = Transient("a GitHub access token reached its GraphQL rate limit")
                logs.warning(COMPONENT, f"A GitHub access token reached its GraphQL rate limit. "
                                        f"{paused(self.tokens, token)} until the rate limit "
                                        f"resets.")
                pauses += 1
                if pauses > MAX_PAUSES:
                    attempt += 1
                continue
            responses, rate = classify(names, body)
            with self.write_lock:
                self.cost += int(rate.get("cost") or 0)
            self.tokens.update(token, rate.get("remaining"), _reset_time(rate.get("resetAt")))
            returned_repositories = any(response is not None for response in responses)
            if returned_repositories:
                self.responded()
            for name, response in zip(names, responses):
                if response is not None and UNRESOLVED_FIELDS in response:
                    with self.write_lock:
                        self.incomplete[name] = response
            responses = [None if response is not None and UNRESOLVED_FIELDS in response
                         else response for response in responses]
            missing = [i for i, response in enumerate(responses) if response is None]
            if not missing:
                return responses
            if len(missing) < len(names):
                again = iter(self.fetch([names[i] for i in missing]))
                return [response if response is not None else next(again) for response in responses]
            message = next((error.get("message") for error in body.get("errors") or []
                            if isinstance(error, dict) and error.get("message")), None)
            last = Transient(f"GitHub returned an error ({message})" if message
                             else "GitHub's response has none of the repositories")
            attempt += 1
            if timed_out(body) and len(names) > 1:
                middle = len(names) // 2
                return self.fetch(names[:middle]) + self.fetch(names[middle:])
            self.wait_after(attempt)
        if self.over():
            return [None] * len(names)
        which = (names[0] if len(names) == 1
                 else f"{len(names):,} repositories from {names[0]} to {names[-1]}")
        if returned_repositories:
            # Because GitHub responds, these attempts do not count toward ending the pass.
            logs.warning(COMPONENT, f"GitHub could not resolve all fields of {which} in any of "
                                    f"{ATTEMPTS} attempts. In the last attempt, {last}. Left to "
                                    f"the next round.")
            return [None] * len(names)
        logs.warning(COMPONENT, f"GitHub returned no response for {which} in {ATTEMPTS} "
                                f"attempts. In the last attempt, {last}.")
        self.failed(last)
        return [None] * len(names)

    def resolve(self, name: str):
        """The current name of `name` through REST, None for a repository that GitHub does not
        have, or False when no attempt got a response."""
        last = None
        attempt = pauses = 0
        while attempt < ATTEMPTS:
            if self.over():
                return False
            token = self.tokens.take(self.ended)
            if token is None:
                return False
            try:
                current, _ = rest_name(token, name)
            except PermissionError as error:
                if self.rejected(token):
                    continue
                last = error
                attempt += 1
                self.wait_after(attempt)
                continue
            except Transient as error:
                last = error
                if error.wait is not None:
                    if self.tokens.pause(token, error.wait):
                        logs.warning(COMPONENT, f"{error}. {paused(self.tokens, token)} for "
                                                f"{error.wait:.0f}s.")
                    pauses += 1
                    if pauses > MAX_PAUSES:
                        attempt += 1
                    continue
                attempt += 1
                self.wait_after(attempt)
                continue
            with self.write_lock:
                self.requests += 1
            self.responded()
            return current
        if not self.over():
            self.failed(last)
        return False


def accepted_tokens(tokens: list, sleep=None) -> list:
    """The GitHub access tokens that GitHub does not reject, as `Credential`s, in the order given.

    Only HTTP 401 rejects a GitHub access token. A check without a verdict is made again after
    each of `CHECK_WAITS`, and a GitHub access token that never gets a verdict is kept, because
    a failed request, HTTP 5xx or a rate limit is no evidence about the GitHub access token. A
    later request that GitHub rejects drops the GitHub access token (`Query.rejected`).
    """
    sleep = sleep or time.sleep
    verdicts = {}
    pending = list(range(len(tokens)))
    for wait in (*CHECK_WAITS, None):
        unchecked = []
        for index in pending:
            token = tokens[index]
            verdict, detail = check_token(token.value)
            account = f"account {token.account}" if token.account else "an unnamed account"
            label = f"GitHub access token {index + 1} of {len(tokens)} of {account}"
            if verdict is None:
                unchecked.append((index, f"GitHub gave no verdict on {label} ({detail})"))
                continue
            verdicts[index] = verdict
            if not verdict:
                logs.warning(COMPONENT, f"GitHub rejected {label} ({detail}).")
        if not unchecked:
            break
        for _, message in unchecked:
            logs.warning(COMPONENT, f"{message}. " + (
                f"Checking again in {wait:.0f}s." if wait is not None else
                "Kept it without a verdict."))
        if wait is None:
            break
        sleep(wait)
        pending = [index for index, _ in unchecked]
    return [token for index, token in enumerate(tokens) if verdicts.get(index) is not False]


def read_partial(path: Path) -> dict:
    """The responses that a stopped run left, keyed by row position."""
    responses = {}
    if not path.is_file():
        return responses
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                break  # a line that does not parse. Its row and the rows after it are queried again
            responses[row["row"]] = row
    return responses


def end_partial(path: Path) -> None:
    """Truncate the partial file to its last whole line. A stopped write leaves its last line
    without a newline. Appended to that line, the next run's first response would make one line
    that does not parse, and `read_partial` reads nothing after such a line. A run calls this
    function before `read_partial`, because a line cut inside a UTF-8 character cannot be
    decoded, and reading it would fail that run and every run after it."""
    if not path.is_file():
        return
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        with open(path, "r+b") as f:
            f.truncate(data.rfind(b"\n") + 1)


@contextmanager
def pass_pool(query: Query, workers: int):
    """A pool for the requests of a pass. When the code that reads the pool's results fails,
    such as on Ctrl+C or a full disk, the pool ends the pass: `ended` stops the requests in
    flight before their next request, and the requests not yet started are cancelled. Without
    both, the pool would send every queued batch of the pass before it shuts down, and nothing
    would record the responses."""
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        yield pool
    except BaseException:
        query.ended.set()
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)


def query_rows(names: list, query: Query, received: dict, batch_size: int, workers: int) -> dict:
    """Query GitHub for every row not yet in `received`, and return all responses by row
    position."""
    pending = [i for i in range(len(names)) if i not in received]
    batches = [pending[start:start + batch_size] for start in range(0, len(pending), batch_size)]
    pace = logs.Pace()
    done = len(received)
    with pass_pool(query, workers) as pool:
        futures = {pool.submit(query.fetch, [names[i] for i in batch]): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            rows = [{"row": i, "ghsName": names[i], **response}
                    for i, response in zip(batch, future.result()) if response is not None]
            query.record(rows)
            received.update({row["row"]: row for row in rows})
            done += len(rows)
            if pace.due():
                logs.info(COMPONENT, f"Queried {done:,} of {len(names):,} repositories with "
                                     f"{query.requests:,} requests.")
                query.save(query.requests, query.cost)
    return received


def resolve_renames(names: list, query: Query, received: dict, batch_size: int,
                    workers: int) -> None:
    """Query REST for the current name of each repository that GraphQL did not find, and
    GraphQL for the name that REST returns, whether new or unchanged. A row stays unchecked, and
    the next round queries it again, when the REST request failed or when GraphQL does not find
    the repository that REST returned, because the REST response shows that the repository
    exists."""
    missing = sorted(i for i, row in received.items()
                     if row["status"] == NOT_FOUND and not row.get("restChecked"))
    if not missing:
        return
    logs.info(COMPONENT, f"Querying GitHub's REST API for the current names of {len(missing):,} "
                         f"repositories that GitHub did not find under their GHS names.")
    returned = {}
    with pass_pool(query, workers) as pool:
        currents = dict(zip(missing, pool.map(lambda i: query.resolve(names[i]), missing)))
    checked = []
    for i, current in currents.items():
        if current is False:
            continue
        if current:
            returned[i] = query.returned[i] = current
        else:
            received[i] = {**received[i], "restChecked": True}
            checked.append(received[i])
    query.record(checked)
    queried = {}
    positions = sorted(returned)
    for start in range(0, len(positions), batch_size):
        chunk = positions[start:start + batch_size]
        for i, response in zip(chunk, query.fetch([returned[i] for i in chunk])):
            if response is None or response["status"] == NOT_FOUND:
                continue
            moved = ghs_cache.name_key(returned[i]) != ghs_cache.name_key(names[i])
            queried[i] = {"row": i, "ghsName": names[i], **response, "restChecked": True,
                          **({"redirectedTo": returned[i]} if moved else {})}
    query.record(list(queried.values()))
    received.update(queried)


def unsettled(count: int, received: dict) -> list:
    """The positions of the rows without a response, and of the `not_found` rows without a
    successful REST check, among the first `count` rows."""
    return [i for i in range(count)
            if i not in received or (received[i]["status"] == NOT_FOUND
                                     and not received[i].get("restChecked"))]


def unresolved_allowed(count: int) -> int:
    """The unresolved rows that a retrieval of `count` rows may keep: `UNRESOLVED_MAX_SHARE` of
    them, rounded up, and at least one."""
    return max(1, math.ceil(count * UNRESOLVED_MAX_SHARE))


def unresolved(position: int, name: str, received: dict) -> dict:
    """The response that the cache keeps for a row that no round settled."""
    error = ("GraphQL did not find the repository under this name, and no REST check settled "
             "whether GitHub has it" if position in received
             else "GitHub returned no response")
    return {"row": position, "ghsName": name, "status": UNRESOLVED, "error": error}


def settle(names: list, query: Query, received: dict, batch_size: int, workers: int,
           tokens: list) -> bool:
    """Query GitHub for every row not yet settled: in the first pass and, while rows are left,
    in a further round after each of `ROUND_WAITS`. No round starts after the run's time limit.
    When the pool is empty because GitHub rejected every GitHub access token, at the start or
    during the run, `tokens`, the configured GitHub access tokens, are checked again after each
    of the same waits, with no check after the run's time limit. A spell of such rejections
    that follows a request that GitHub served starts the waits again. Returns False when GitHub
    accepts none of the GitHub access tokens after the last wait of a spell."""
    rounds, checks = iter(ROUND_WAITS), iter(ROUND_WAITS)
    # The number of requests that GitHub had served when it last rejected every GitHub access
    # token. A request served since then shows that the spell ended. The waits of the next spell
    # then start again from the first wait, and only the time limit bounds all spells together.
    served = query.requests
    while True:
        query.begin_pass()
        try:
            query_rows(names, query, received, batch_size, workers)
            resolve_renames(names, query, received, batch_size, workers)
        except NoTokens as error:
            query.save(query.requests, query.cost)
            if query.requests > served:
                checks = iter(ROUND_WAITS)
            served = query.requests
            wait = next(checks, None)
            if wait is None:
                return False
            if query.clock() + wait >= query.deadline:
                logs.warning(COMPONENT, f"{error}. Stopped checking them, because the time limit "
                                        f"of {QUERY_SECONDS / 3600:.0f} hours from the start ends "
                                        f"before the next check.")
                return True
            logs.warning(COMPONENT, f"{error}. Checking them again in {wait / 60:.0f} minutes.")
            query.sleep(wait)
            query.tokens = TokenPool(accepted_tokens(tokens, query.sleep))
            continue
        pending = unsettled(len(names), received)
        wait = next(rounds, None) if pending else None
        if wait is None:
            return True
        if query.clock() + wait >= query.deadline:
            logs.warning(COMPONENT, f"Started no further round, because the time limit of "
                                    f"{QUERY_SECONDS / 3600:.0f} hours from the start ends before "
                                    f"the next round.")
            return True
        query.rounds += 1
        query.save(query.requests, query.cost)
        logs.warning(COMPONENT, f"Responses or name checks are missing for {len(pending):,} of "
                                f"{len(names):,} repositories. Requesting them again in "
                                f"{wait / 60:.0f} minutes.")
        query.sleep(wait)


def run(cache_dir: Path, pointer: Path, batch_size: int, workers: int, environ=os.environ,
        credentials_path: Path | None = None) -> int:
    """Query GitHub for the retrieval that the pointer names, and return the exit code."""
    row = ghs_cache.pointed_retrieval(pointer, cache_dir)
    rid = row["retrieval_id"]
    if row["github_file_sha256"] and ghs_cache.has_rows(cache_dir, rid, "github"):
        try:
            recorded = all(complete(response) for response in ghs_cache.read_rows(
                cache_dir, rid, "github", row["github_file_sha256"]))
        except ghs_cache.CacheError:
            # A run that queries GitHub writes the responses first and records their checksum in
            # the index after. A run stopped between the two writes leaves responses that the
            # index does not record, and the next run queries GitHub again rather than stopping
            # on the checksum. The partial file keeps the responses that the stopped run
            # received.
            logs.warning(COMPONENT, f"GitHub's responses for retrieval {rid} do not match the "
                                    f"checksum in the index.")
            recorded = False
        else:
            if recorded:
                logs.info(COMPONENT, f"Retrieval {rid} already has GitHub's responses "
                                     f"({row['github_found']} found, {row['github_not_found']} "
                                     f"not found or blocked, {row['github_unresolved']} "
                                     f"unresolved).")
                return 0
            logs.info(COMPONENT, f"GitHub's responses for retrieval {rid} lack fields that the "
                                 f"query requests.")
        row = {**row, "github_started_at": "", "github_requests": "", "github_cost": ""}
    names = [item["name"] for item in ghs_cache.read_rows(cache_dir, rid, "ghs",
                                                          row["ghs_file_sha256"])]
    tokens = credentials.github_tokens(credentials_path, environ)
    if not tokens:
        where = (f"{credentials_path} has none" if credentials_path
                 else "no credentials file was given")
        logs.error(COMPONENT, f"No GitHub access token to query GitHub about {len(names):,} "
                              f"repositories (GITHUB_TOKENS and GITHUB_TOKEN are unset, and "
                              f"{where}).")
        return 1
    configured = tokens
    # GitHub can reject valid GitHub access tokens for a while. When GitHub rejects all of them
    # at the start, the query begins with an empty pool, so that `settle` waits and checks them
    # again, as it does when GitHub rejects all of them later in the run.
    tokens = accepted_tokens(configured)
    partial = Path(cache_dir) / f"{rid}.github.partial.jsonl"
    end_partial(partial)
    received = {i: response for i, response in read_partial(partial).items() if complete(response)}
    started = row["github_started_at"] or ghs_cache.iso(ghs_cache.utc_now())
    ghs_cache.put_index_row({"retrieval_id": rid, "github_started_at": started}, cache_dir)
    pool = TokenPool(tokens)
    using = (f"with {len(tokens):,} GitHub access token(s) of {pool.accounts():,} account(s)"
             if tokens else "once GitHub accepts one of the configured GitHub access tokens")
    logs.info(COMPONENT, f"Querying GitHub about {len(names) - len(received):,} of the "
                         f"{len(names):,} repositories in retrieval {rid} {using}"
                         + (f". An earlier run queried the other {len(received):,}"
                            if received else "") + ".")
    base_requests = int(row["github_requests"] or 0)
    base_cost = int(row["github_cost"] or 0)

    def save(requests_so_far: int, cost_so_far: int) -> None:
        ghs_cache.put_index_row({"retrieval_id": rid,
                                 "github_requests": base_requests + requests_so_far,
                                 "github_cost": base_cost + cost_so_far}, cache_dir)

    query = Query(pool, partial, save=save, deadline=time.time() + QUERY_SECONDS)
    if not settle(names, query, received, batch_size, workers, configured):
        save(query.requests, query.cost)
        kept = (f"Saved the {len(received):,} of {len(names):,} responses so far in "
                f"{partial.name} for the next run" if received
                else "GitHub has returned no response for this retrieval")
        logs.error(COMPONENT, f"GitHub accepted none of the configured GitHub access tokens in "
                              f"{len(ROUND_WAITS)} further checks. {kept}.")
        return 1
    taken = query.take_incomplete(names, received)
    if taken:
        (row, fields), *_ = taken
        logs.warning(COMPONENT, f"GitHub returned {len(taken):,} repositories with fields that "
                                f"it could not resolve in every round, such as "
                                f"{row['ghsName']} ({', '.join(fields)}). Recorded their "
                                f"responses with those fields empty.")
    pending = unsettled(len(names), received)
    allowed = unresolved_allowed(len(names))
    if len(pending) > allowed:
        save(query.requests, query.cost)
        logs.error(COMPONENT, f"Responses or name checks are missing for {len(pending):,} of "
                              f"{len(names):,} repositories after {query.rounds:,} further "
                              f"round(s), more than the {allowed:,} allowed as unresolved "
                              f"({UNRESOLVED_MAX_SHARE:.1%} of the repositories, at least one). "
                              f"Saved the {len(received):,} responses in {partial.name}. The "
                              f"next run queries GitHub for the rest.")
        return 1
    if pending:
        received.update({i: unresolved(i, names[i], received) for i in pending})
        logs.warning(COMPONENT, f"Responses or name checks are missing for {len(pending):,} of "
                                f"{len(names):,} repositories after {query.rounds:,} further "
                                f"round(s). Recorded them as unresolved for the filter to "
                                f"remove.")
    ordered = [received[i] for i in range(len(names))]
    found = sum(1 for r in ordered if r["status"] == FOUND)
    left = sum(1 for r in ordered if r["status"] == UNRESOLVED)
    _, sha = ghs_cache.write_rows(cache_dir, rid, "github",
                                  ({k: v for k, v in r.items() if k != "row"} for r in ordered))
    # A repository that GitHub did not find or blocks is gone from GitHub, while an unresolved
    # row has no response that says whether its repository is gone. The index counts the two
    # cases separately.
    ghs_cache.put_index_row({
        "retrieval_id": rid, "github_finished_at": ghs_cache.iso(ghs_cache.utc_now()),
        "github_rows": len(ordered), "github_found": found,
        "github_not_found": len(ordered) - found - left, "github_unresolved": left,
        "github_requests": base_requests + query.requests,
        "github_cost": base_cost + query.cost,
        "github_file_sha256": sha}, cache_dir)
    partial.unlink(missing_ok=True)
    redirected = sum(1 for r in ordered if r.get("redirectedTo"))
    logs.info(COMPONENT, f"Queried GitHub about all {len(ordered):,} repositories in retrieval "
                         f"{rid} ({found:,} found, {redirected:,} of them under a new name, "
                         f"{len(ordered) - found - left:,} not found or blocked, {left:,} "
                         f"unresolved). The {query.requests:,} requests cost {query.cost:,} "
                         f"GraphQL points.")
    return 0


def main(argv: list | None = None) -> int:
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(description="Query GitHub about every repository in the "
                                                 "GHS retrieval that the stage reads.",
                                     allow_abbrev=False)
    parser.add_argument("--cache-dir", type=Path, default=ghs_cache.CACHE_DIR,
                        help="The cache of retrievals (default: %(default)s)")
    parser.add_argument("--pointer", type=Path, default=ghs_cache.POINTER,
                        help="The file that names the retrieval (default: %(default)s)")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE,
                        help="Repositories per GraphQL request (default: %(default)s)")
    parser.add_argument("--workers", type=int, default=WORKERS,
                        help="Concurrent requests (default: %(default)s)")
    parser.add_argument("--credentials", type=Path, default=credentials.default_path(),
                        help="The credentials file, read for GitHub access tokens after "
                             "GITHUB_TOKENS and GITHUB_TOKEN (default: %(default)s)")
    args = parser.parse_args(argv)
    if not BATCH_SIZE_MIN <= args.batch_size <= BATCH_SIZE_MAX or args.workers < 1:
        logs.error(COMPONENT, f"--batch-size must be from {BATCH_SIZE_MIN} to {BATCH_SIZE_MAX}, "
                              f"and --workers at least 1.")
        return 1
    try:
        return run(args.cache_dir, args.pointer, args.batch_size, args.workers,
                   credentials_path=args.credentials)
    except CacheError as error:
        logs.error(COMPONENT, f"The cache of retrievals cannot be used. {upper_first(error)}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
