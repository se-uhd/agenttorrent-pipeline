"""The code that every step that queries GitHub's API shares: the pool of GitHub access tokens
with each account's rate limit and pause, and the test of a response for a rate limit or a block.

The GitHub access tokens come from `agentcontext.credentials`. GitHub applies its rate limits,
the primary hourly rate limit and the secondary limits alike, per account, and every GitHub
access token of one account shares them. A pause or a rate limit reached with one GitHub access
token therefore sets aside every GitHub access token of that account. A GitHub access token
without a named account counts as an account of its own.

The module imports no HTTP client, so that the package has no third-party dependency for it. It
reads a response through `status_code`, `headers` and `json()`, which a `requests` response has.
"""

from __future__ import annotations

import threading
import time

from agentcontext.credentials import Credential

# The requests kept in reserve on each account, so that a run never uses up a rate limit.
RESERVE = 50
# The number of pauses that one request waits through before each further pause counts as a
# failed attempt. A response that still reads as a rate limit after that many pauses has another
# cause, because a rate limit ends.
MAX_PAUSES = 30
# The seconds that an account pauses for a rate limit when GitHub states no time to wait.
RATE_LIMIT_DEFAULT_WAIT = 60.0


class NoTokens(Exception):
    """No GitHub access token is left that GitHub accepts."""


class TokenPool:
    """The GitHub access tokens, taken in turn, with the remaining rate limit of each account and
    the time until which GitHub paused each account.

    A secondary rate limit, HTTP 403 or 429, applies to the user of a GitHub access token. Every
    request with that GitHub access token fails until the pause that GitHub names has passed.
    The pause applies to the account, not to the request, because the other requests in flight
    would reach the limit again if each request waited out the pause on its own. The pool skips
    a paused or spent account, and when every account is paused or spent, `take` waits for the
    earliest account to become usable again.
    """

    def __init__(self, tokens: list, clock=None, sleep=None):
        self.lock = threading.Lock()
        self.tokens = [t if isinstance(t, Credential) else Credential("github", "", t)
                       for t in tokens]
        self.rate_limits, self.paused = {}, {}
        for token in self.tokens:
            self.rate_limits.setdefault(self.account(token), (5000, 0.0))
            self.paused.setdefault(self.account(token), 0.0)
        self.turn = 0
        self.clock, self.sleep = clock or time.time, sleep or time.sleep

    @staticmethod
    def account(token: Credential) -> str:
        """The key under which the limits of a GitHub access token are kept: its account, or
        the GitHub access token itself."""
        return token.account or f"token:{token.value}"

    def _of(self, value: str) -> Credential | None:
        return next((t for t in self.tokens if t.value == value), None)

    def _usable_at(self, account: str) -> float:
        remaining, reset = self.rate_limits[account]
        spent_until = reset if remaining <= RESERVE else 0.0
        return max(spent_until, self.paused[account])

    def take(self, stop=None) -> "str | None":
        """The value of a GitHub access token with an account that is below its rate limit and
        not paused. When no account qualifies, `take` waits for the earliest one. It returns
        None once `stop`, an event, is set, during the wait too. After the code that records
        the responses has failed, a request would be sent for nobody, and waiting out the rest
        of the pause for it would delay the exit of the run."""
        while True:
            if stop is not None and stop.is_set():
                return None
            with self.lock:
                if not self.tokens:
                    raise NoTokens("GitHub accepts none of the configured GitHub access tokens")
                now = self.clock()
                for _ in range(len(self.tokens)):
                    token = self.tokens[self.turn % len(self.tokens)]
                    self.turn += 1
                    if self._usable_at(self.account(token)) <= now:
                        return token.value
                wait = max(1.0, min(self._usable_at(self.account(t)) for t in self.tokens) - now)
            if stop is None:
                self.sleep(wait)
                continue
            # In steps of a second, so that the event ends the wait.
            while wait > 0 and not stop.is_set():
                self.sleep(min(wait, 1.0))
                wait -= 1.0

    def pause(self, value: str, seconds: float) -> bool:
        """Set aside every GitHub access token of `value`'s account for `seconds`. Returns
        whether this call started a pause, so that only the first request that reaches a limit
        logs it."""
        with self.lock:
            token = self._of(value)
            if token is None:
                return False
            account = self.account(token)
            now = self.clock()
            until = now + seconds
            if until <= self.paused[account]:
                return False
            started = self.paused[account] <= now
            self.paused[account] = until
            return started

    def update(self, value: str, remaining, reset) -> None:
        """Record the remaining rate limit that GitHub reported for `value`'s account."""
        with self.lock:
            token = self._of(value)
            if token is not None and remaining is not None:
                self.rate_limits[self.account(token)] = (int(remaining), float(reset or 0))

    def drop(self, value: str) -> None:
        """Leave out a GitHub access token that GitHub refused."""
        with self.lock:
            self.tokens = [t for t in self.tokens if t.value != value]

    def pausing(self, value: str) -> str:
        """What a pause of `value` sets aside, as the subject and verb of a log message: "the
        GitHub access tokens of account X are paused", or "that GitHub access token is paused"
        for a GitHub access token without a named account."""
        token = self._of(value)
        if token is not None and token.account:
            return f"the GitHub access tokens of account {token.account} are paused"
        return "that GitHub access token is paused"

    def accounts(self) -> int:
        """The number of accounts that the GitHub access tokens of the pool belong to."""
        return len({self.account(t) for t in self.tokens})


def rate_limit_wait(response, clock=time.time) -> float | None:
    """The seconds that a rate limit requires a request to wait, or None for a response that is
    not a rate limit.

    GitHub marks a rate limit with `Retry-After`, with `X-RateLimit-Remaining: 0`, or with
    "rate limit" in the message. A 403 without these marks has another cause. GitHub returns 403
    "Repository access blocked" for a repository that it took down for a terms-of-service
    violation, and reading that response as a rate limit would pause the GitHub access token
    permanently.
    """
    headers = response.headers or {}
    retry_after = str(headers.get("Retry-After") or "")
    if retry_after.isdigit():
        return float(retry_after)
    if str(headers.get("X-RateLimit-Remaining")) == "0":
        reset = str(headers.get("X-RateLimit-Reset") or "")
        return max(1.0, float(reset) - clock()) if reset.isdigit() else RATE_LIMIT_DEFAULT_WAIT
    try:
        message = str((response.json() or {}).get("message", ""))
    except (ValueError, AttributeError):
        message = ""
    return RATE_LIMIT_DEFAULT_WAIT if "rate limit" in message.lower() else None


def blocked(response) -> bool:
    """Whether a REST response reports that GitHub blocks access to the repository: 451 after a
    DMCA takedown, or 403 "Repository access blocked" for a terms-of-service violation."""
    try:
        body = response.json() or {}
    except ValueError:
        return False
    return isinstance(body, dict) and ("block" in body
                                       or "access blocked" in str(body.get("message", "")).lower())


def graphql_errors(body) -> list[dict]:
    """The `errors` of a GraphQL response body, each an object.

    GraphQL's specification makes `errors` a list of objects. Another value, such as a proxy's
    text, becomes the message of an error object, and a body that is not an object has no
    errors. Every reader can then read the type and the message of each error. A reader that
    called `.get` on such a value would raise AttributeError and end the labeling at every
    resume that gets the same body.
    """
    errors = body.get("errors") if isinstance(body, dict) else None
    if errors is None:
        return []
    listed = errors if isinstance(errors, list) else [errors]
    return [error if isinstance(error, dict) else {"message": str(error)}
            for error in listed if error is not None]


def rate_limited(body) -> bool:
    """Whether a GraphQL response reports that the hourly rate limit is reached. GraphQL reports
    it with HTTP 200 and no data."""
    return any(error.get("type") == "RATE_LIMITED" for error in graphql_errors(body))
