"""
Tests for agentcontext/github_api.py

Strategy:
  - The pool runs on a fake clock that a fake sleep moves forward, so that a pause or an
    exhausted rate limit ends without real waiting.
  - A response is a small object with `status_code`, `headers` and `json()`, the parts of a
    `requests` response that the module reads.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_github_api -v
"""

import unittest

from agentcontext import github_api
from agentcontext.credentials import Credential
from agentcontext.github_api import TokenPool


class _Answer:

    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def json(self):
        if self.body is None:
            raise ValueError("not JSON")
        return self.body


class _Clock:

    def __init__(self):
        self.now, self.waits = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def _pool(*tokens):
    clock = _Clock()
    return TokenPool(list(tokens), clock=clock, sleep=clock.sleep), clock


class TestTokenPool(unittest.TestCase):

    def test_github_access_tokens_are_taken_in_turn(self):
        pool, _ = _pool(Credential("github", "a", "t1"), Credential("github", "b", "t2"))
        self.assertEqual([pool.take() for _ in range(4)], ["t1", "t2", "t1", "t2"])

    def test_a_pause_returns_true_once_and_is_only_extended(self):
        pool, clock = _pool(Credential("github", "a", "t"))
        self.assertTrue(pool.pause("t", 60))
        self.assertFalse(pool.pause("t", 30))
        self.assertFalse(pool.pause("t", 90))
        self.assertEqual(pool.paused["a"], clock.now + 90)

    def test_a_pause_sets_aside_every_github_access_token_of_the_account(self):
        """GitHub applies its limits per account, and another GitHub access token of the same
        account therefore reaches the same limit."""
        pool, _ = _pool(Credential("github", "a", "t1"), Credential("github", "a", "t2"),
                        Credential("github", "b", "t3"))
        pool.pause("t1", 60)
        self.assertEqual([pool.take() for _ in range(3)], ["t3", "t3", "t3"])

    def test_the_pause_message_names_the_account_that_it_sets_aside(self):
        pool, _ = _pool(Credential("github", "a", "t1"), "t2")
        self.assertEqual(pool.pausing("t1"), "the GitHub access tokens of account a are paused")
        self.assertEqual(pool.pausing("t2"), "that GitHub access token is paused")

    def test_github_access_tokens_without_an_account_are_accounts_of_their_own(self):
        pool, _ = _pool("t1", "t2")
        pool.pause("t1", 60)
        self.assertEqual(pool.take(), "t2")
        self.assertEqual(pool.accounts(), 2)

    def test_with_every_account_paused_take_waits_for_the_earliest(self):
        pool, clock = _pool(Credential("github", "a", "t1"), Credential("github", "b", "t2"))
        pool.pause("t1", 120)
        pool.pause("t2", 30)
        self.assertEqual(pool.take(), "t2")
        self.assertEqual(clock.waits, [30])

    def test_a_wait_ends_without_a_github_access_token_once_its_event_is_set(self):
        """The GitHub query sets the event when its pass ends, such as on a full disk while it
        records a response. A GitHub access token taken after the pause would send a request
        with a response that nobody records, and the run would exit only after the pause."""
        import threading
        pool, clock = _pool(Credential("github", "a", "t"))
        pool.pause("t", 600)
        stop = threading.Event()
        sleep = clock.sleep

        def sleeping(seconds):
            sleep(seconds)
            if len(clock.waits) == 3:
                stop.set()

        pool.sleep = sleeping
        self.assertIsNone(pool.take(stop))
        self.assertEqual(sum(clock.waits), 3)
        self.assertIsNone(pool.take(stop))
        self.assertEqual(len(clock.waits), 3, "a set event sends no wait either")

    def test_an_exhausted_rate_limit_waits_for_the_reset(self):
        pool, clock = _pool(Credential("github", "a", "t"))
        pool.update("t", github_api.RESERVE, clock.now + 600)
        self.assertEqual(pool.take(), "t")
        self.assertEqual(clock.waits, [600])

    def test_a_dropped_github_access_token_is_not_taken_and_none_left_is_an_error(self):
        pool, _ = _pool("t1", "t2")
        pool.drop("t1")
        self.assertEqual(pool.take(), "t2")
        pool.drop("t2")
        with self.assertRaises(github_api.NoTokens):
            pool.take()


class TestReadingAResponse(unittest.TestCase):

    def test_a_rate_limit_is_read_from_githubs_signals(self):
        self.assertEqual(github_api.rate_limit_wait(_Answer(403, {}, {"Retry-After": "90"})), 90)
        self.assertEqual(github_api.rate_limit_wait(
            _Answer(403, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1300"}),
            clock=lambda: 1000), 300)
        self.assertEqual(github_api.rate_limit_wait(
            _Answer(403, {"message": "You have exceeded a secondary rate limit."})), 60)

    def test_a_403_without_those_signals_is_not_a_rate_limit(self):
        """GitHub returns 403 "Repository access blocked" for a repository that it took down
        for a terms-of-service violation. Read as a rate limit, the response would pause a GitHub
        access token permanently."""
        answer = _Answer(403, {"message": "Repository access blocked", "block": {"reason": "tos"}})
        self.assertIsNone(github_api.rate_limit_wait(answer))
        self.assertTrue(github_api.blocked(answer))
        self.assertFalse(github_api.blocked(_Answer(403, {"message": "Forbidden"})))
        self.assertFalse(github_api.blocked(_Answer(500)))

    def test_an_exhausted_graphql_rate_limit_comes_as_an_error_of_its_own_type(self):
        self.assertTrue(github_api.rate_limited({"errors": [{"type": "RATE_LIMITED"}]}))
        self.assertFalse(github_api.rate_limited({"data": {}, "errors": [{"type": "NOT_FOUND"}]}))

    def test_errors_that_are_not_objects_are_read_as_messages(self):
        """GraphQL's specification makes `errors` a list of objects, and a proxy or a failing
        server can send anything else. Reading such a body as a list of objects would raise
        AttributeError and end the labeling at every resume that gets the same body."""
        for body in ({"data": None, "errors": ["502 Bad Gateway"]},
                     {"data": None, "errors": "502 Bad Gateway"},
                     {"data": None, "errors": [None, 502]},
                     {"data": None, "errors": 502},
                     ["502 Bad Gateway"], "502 Bad Gateway", None):
            with self.subTest(body=body):
                self.assertFalse(github_api.rate_limited(body))
        self.assertTrue(github_api.rate_limited(
            {"data": None, "errors": ["502 Bad Gateway", {"type": "RATE_LIMITED"}]}))
        self.assertTrue(github_api.rate_limited({"errors": {"type": "RATE_LIMITED"}}))
        self.assertEqual(github_api.graphql_errors({"errors": ["502 Bad Gateway", None, 502]}),
                         [{"message": "502 Bad Gateway"}, {"message": "502"}])
        self.assertEqual(github_api.graphql_errors({"errors": "proxy text"}),
                         [{"message": "proxy text"}])
        self.assertEqual(github_api.graphql_errors({"data": {}}), [])


if __name__ == "__main__":
    unittest.main()
