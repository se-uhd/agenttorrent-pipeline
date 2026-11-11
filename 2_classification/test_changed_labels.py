"""
Tests for changed_labels.py

Strategy:
  - Two functions decide the causes from the labels alone, and the tests check them against
    every case that the specification names.
  - A comparison runs between real runs of 1_classify_repos.py into a temporary cache of labels,
    with the fakes of test_classify_repos. The fake model responds from the README in its input.
    The fake GitHub returns each README by repository and revision, which lets the comparison
    read a previous README at the revision that the previous run recorded.
  - The comparison gets a fresh fake model, and each case counts the requests of the repeated
    labels alone.

Requirements: Python 3.14, openai, requests (requirements.txt)
Run with:  python3 -m unittest test_changed_labels -v
"""

import argparse
import asyncio
import contextlib
import csv
import functools
import importlib
import io
import itertools
import json
import shutil
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import openai

sys.path.insert(0, str(Path(__file__).resolve().parent))
changed_labels = importlib.import_module("changed_labels")
import forks  # noqa: E402
import label_cache  # noqa: E402
import readmes  # noqa: E402
from test_classify_repos import (DEEPSEEK, ENGLISH, LATER, REVISION, _answer,  # noqa: E402
                                 _Billed,
                                 _Bridge, _call, _Clock, _GitHub, _Glm, _Model, _repositories,
                                 _parameters_with_provider, _repository_of, _Response,
                                 _RunCase, _Runaway, traced_peak, write_partial)

from agentcontext.credentials import Credential  # noqa: E402
from agentcontext.github_api import NoTokens, TokenPool  # noqa: E402

V, I, C, U = (changed_labels.MODEL_VARIATION, changed_labels.INPUT_CHANGE,
              changed_labels.CLASSIFIER_CHANGE, changed_labels.UNDETERMINED)
CHANGED = ENGLISH + " Now with tests."


def _by_readme(request, number):
    """`false` for the changed README, `true` for every other README."""
    text = request["input"] if isinstance(request["input"], str) else ""
    return _answer(_repository_of(request), "false" if "Now with tests" in text else "true")


def _always(label):
    return lambda request, number: _answer(_repository_of(request), label)


def _refusing(*names):
    """The model of `_by_readme`, except that the API refuses the requests for the repositories
    in `names` as under a rejected key. The refusal stops the comparison."""
    response = SimpleNamespace(request=SimpleNamespace(), status_code=401, headers={})

    def script(request, number):
        if _repository_of(request) in names:
            raise openai.AuthenticationError("Incorrect API key provided", response=response,
                                             body=None)
        return _by_readme(request, number)
    return script


class _Revisions(_GitHub):
    """READMEs by repository and revision. `by_revision[(name, revision)]` is the README returned
    for that revision. GitHub returns 404 for the revisions in `missing`, as for a commit that is
    gone, and the README of `readmes` for any other revision."""

    def __init__(self, readmes_by_name):
        super().__init__(readmes_by_name)
        self.by_revision, self.missing = {}, set()

    def get(self, url, headers=None, timeout=None):
        if "/readme" in url and "?ref=" in url:
            name = url.split("/repos/")[1].split("/readme")[0]
            revision = url.split("?ref=")[1]
            if (name, revision) in self.missing:
                self.sent.append(url)
                return _Response(404, {})
            if (name, revision) in self.by_revision:
                saved = self.readmes.get(name)
                self.readmes[name] = self.by_revision[(name, revision)]
                try:
                    return super().get(url, headers, timeout)
                finally:
                    self.readmes[name] = saved
        return super().get(url, headers, timeout)


class _Metadata(_Bridge):
    """The fake tools, with metadata that gives each repository `description`, which a case
    changes as GitHub's description of the repository changes."""

    def __init__(self, description):
        super().__init__()
        self.description = description

    async def call(self, name, arguments):
        if name != "get_repository_metadata":
            return await super().call(name, arguments)
        self.calls.append((name, arguments))
        return json.dumps({"repo_name": arguments["repo_name"], "available": True,
                           "description": self.description}), False


class TestCauses(unittest.TestCase):
    """label_cause(previous label, label, repeat, previous input's label, classifier changed)."""

    def test_an_input_the_repeat_labels_otherwise_has_two_labels(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "true", None, False), V)
        self.assertEqual(changed_labels.label_cause("true", "false", "unclear", "true", True), V)

    def test_labels_that_follow_their_input_are_an_input_change(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "true", False), I)

    def test_the_previous_input_labeled_as_now_by_the_same_classifier_has_two_labels(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "false", False), V)
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "unclear", False), V)

    def test_an_unchanged_input_labeled_otherwise_by_the_same_classifier_has_two_labels(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "false", None, False), V)

    def test_a_new_classifier_that_labels_the_previous_input_as_now_changed_the_label(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "false", True), C)
        self.assertEqual(changed_labels.label_cause("true", "false", "false", None, True), C)

    def test_a_new_classifier_that_keeps_the_previous_inputs_label_leaves_the_input(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "true", True), I)

    def test_an_unreadable_previous_input_or_a_third_label_leaves_the_cause_open(self):
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "", False), U)
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "", True), U)
        self.assertEqual(changed_labels.label_cause("true", "false", "false", "unclear", True), U)

    def test_an_exclusion_changes_with_its_readme_else_with_the_language_check(self):
        self.assertEqual(changed_labels.outcome_cause(["readme", "parameters"]), I)
        self.assertEqual(changed_labels.outcome_cause(["revision", "parameters"]), C)
        self.assertEqual(changed_labels.outcome_cause(["revision"]), U)

    def test_the_model_counts_as_changed_only_between_two_labels(self):
        label = {"outcome": "labeled", "model": "gpt-6-luna", "parameters": "p", "readme_sha": "s",
                 "revision": "r", "description": ""}
        other = {**label, "model": "gpt-6.1-sol"}
        exclusion = {"outcome": "excluded", "parameters": "p", "readme_sha": "", "revision": "r"}
        self.assertEqual(changed_labels.changed_inputs(label, other), ["model"])
        self.assertEqual(changed_labels.changed_inputs(label, exclusion), ["readme"])

    def test_a_label_of_the_second_model_counts_as_a_changed_model(self):
        """Under one model and one set of classification parameters, the second model can make
        one of two labels, and the model that made the label changed."""
        label = {"outcome": "labeled", "model": "deepseek/deepseek-v4.1-flash", "parameters": "p",
                 "readme_sha": "s", "revision": "r", "description": ""}
        second = {**label, "second_model": {"model": "z-ai/glm-5.3",
                                            "reason": "output_limit"}}
        self.assertEqual(changed_labels.changed_inputs(label, second), ["model"])
        self.assertEqual(changed_labels.changed_inputs(second, second), [])

    def test_parameters_that_differ_in_the_classifying_model_alone_are_a_changed_model(self):
        """The classification parameters name the classifying model with its endpoint and its
        reasoning effort, so that they differ whenever the model does."""
        shared = {"luna": "rest", "sol": "rest", "sol-new-prompt": "other"}.get
        label = {"outcome": "labeled", "model": "gpt-6-luna", "parameters": "luna",
                 "readme_sha": "s", "revision": "r", "description": ""}
        sol = {**label, "model": "gpt-6.1-sol", "parameters": "sol"}
        high = {**label, "parameters": "sol"}
        self.assertEqual(changed_labels.changed_inputs(label, sol, shared), ["model"])
        self.assertEqual(changed_labels.changed_inputs(label, high, shared), ["model"])
        self.assertEqual(changed_labels.changed_inputs(
            label, {**sol, "parameters": "sol-new-prompt"}, shared), ["model", "parameters"])

    def test_the_description_and_the_language_check_count_as_changed_inputs(self):
        label = {"outcome": "labeled", "model": "m", "parameters": "p", "readme_sha": "s",
                 "revision": "r", "description": "A formatter.", "language_check": {"lingua": "1"}}
        self.assertEqual(changed_labels.changed_inputs(label, {**label, "description": "A formatter. "}), [])
        self.assertEqual(changed_labels.changed_inputs(label, {**label, "description": "A linter."}),
                         ["description"])
        self.assertEqual(changed_labels.changed_inputs(label, {**label, "language_check": {"lingua": "2"}}),
                         ["language_check"])
        self.assertEqual(changed_labels.outcome_cause(["language_check"]), C)
        # The input of a label that recorded no description is not known.
        self.assertEqual(changed_labels.changed_inputs(
            {k: v for k, v in label.items() if k != "description"}, label), ["description"])
        self.assertIsNone(changed_labels.summary_of({"outcome": "labeled"})["description"])

    def test_a_fork_verdict_of_another_heuristic_is_a_classifier_change(self):
        """When a new version of the fork heuristic decides the fork verdict of a carried label
        again, the verdict's facts differ too. The differing facts must not count as an input
        change."""
        thresholds = {"author_minimum": 2, "window_start": "2026-03-22"}
        previous = {"decided_at": "t1", "fork_thresholds": thresholds,
                    "fork": {"independent": True, "shares_history": None, "heuristic": "a"}}
        current = {"carried_from": {"decided_at": "t1"}, "fork_thresholds": thresholds,
                   "fork": {"independent": False, "shares_history": False, "heuristic": "b"}}
        self.assertEqual(changed_labels.carried_cause(previous, current), C)
        self.assertEqual(changed_labels.carried_cause(
            previous, {**current, "fork_thresholds": {**thresholds, "window_start": "2026-04-22"}}), U)
        self.assertEqual(changed_labels.carried_cause(
            previous, {**current, "fork": {**current["fork"], "heuristic": "a"}}), I)

    def test_repositories_are_paired_by_their_github_id(self):
        def row(number, outcome, label="", reason=""):
            return {"id": str(number), "repo_name": f"o/{number}", "outcome": outcome,
                    "label": label, "reason": reason, "decided_in": "r"}
        before = [row(1, "labeled", "true"), row(2, "labeled", "true"), row(3, "gone"),
                  row(4, "labeled", "true"), row(5, "labeled", "false"),
                  row(6, "excluded", reason="empty_readme"), row(8, "labeled", "true"),
                  row(9, "undecided", reason="model: ModelFailure: x")]
        after = [row(1, "labeled", "true"), row(2, "labeled", "false"), row(3, "labeled", "true"),
                 row(4, "gone"), row(6, "labeled", "true"), row(7, "labeled", "true"),
                 row(8, "undecided", reason="github: NoResponse: x"), row(9, "labeled", "true")]
        changes, counts = changed_labels.pair(before, after)
        self.assertEqual([old["id"] for old, _ in changes], ["2", "6"])
        self.assertEqual(counts, {"unchanged": 1, "new": 1, "left": 1, "undecided": 2, "gone": 1,
                                  "back": 1})


class _CompareCase(_RunCase):

    def setUp(self):
        super().setUp()
        self.github = _Revisions({"o/a": (ENGLISH, "s1"), "o/b": (ENGLISH, "s2")})
        self.github.by_revision[("o/a", REVISION)] = (ENGLISH, "s1")
        self.github.by_revision[("o/a", LATER)] = (CHANGED, "s3")
        self.connected = 0

    def run_over(self, repositories, github, client, bridge=None, **options):
        """A new run, as each run over a later sampling frame is. The cases give the runs one
        retrieval, and without `new_run` a run over the repositories of the finished run before
        it would complete that run (`finished_run_to_complete`)."""
        return super().run_over(repositories, github, client, bridge,
                                **{"new_run": True, **options})

    def first_and_second(self, second_model, first_model=None, **second):
        """A run at REVISION, then a run at LATER, where o/a's README changed."""
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                  _Model(first_model or _by_readme))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(second_model), **second)
        self.assertEqual(code, 0, err)

    def compare(self, model, bridge=None, bridge_for=None, **options):
        args = argparse.Namespace(run=None, previous=None, cache_dir=self.cache,
                                  output_dir=self.out, workers=2, credentials=None)
        for key, value in options.items():
            setattr(args, key, value)
        pool = TokenPool([Credential("github", "a", "t")])
        client = readmes.Client(pool, session=self.github, sleep=lambda s: None)

        def connect(arguments, endpoint=None):
            self.connected += 1
            return client, model
        bridge = bridge or _Bridge()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = changed_labels.compare(args, connect=connect,
                                          bridge_for=bridge_for or (lambda s: bridge))
        return code, err.getvalue()

    def changes(self):
        with open(self.out / "label_changes.csv", newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def recorded_changes(self):
        row = self.index()[-1]
        return list(label_cache.read_changes(self.cache, row["run_id"], row["changes_sha256"]))


class TestTheStopOfAComparison(unittest.TestCase):
    """Every stop of a comparison ends a pause for spent credit, so that a worker that waits for
    credit does not hold the comparison for the longest wait."""

    def comparison(self, clock):
        async def no_credit(service):
            return False
        pause = changed_labels.classify.CreditPause(clock.sleep, clock, check=no_credit)
        pause.refused(changed_labels.classify.endpoints.CreditRefusal(
            changed_labels.classify.endpoints.OPENROUTER, "OpenRouter refuses the requests"),
            probe=False)
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        classifier = SimpleNamespace(credit=pause, kept={}, requests=0, usage={},
                                     sleep=clock.sleep, clock=clock,
                                     end_pauses=lambda reason: pause.end(reason))
        github = SimpleNamespace(requests=0, responses=0, responds=lambda: None)
        return pause, changed_labels.Comparison([], classifier, github,
                                                tmp / "comparison.partial.jsonl", workers=1)

    def stops_and_ends_the_pause(self, stop):
        clock = _Clock()
        pause, comparison = self.comparison(clock)

        async def scenario():
            waiting = asyncio.ensure_future(
                pause.ready(changed_labels.classify.endpoints.OPENROUTER))
            await asyncio.sleep(0)
            await stop(comparison)
            with self.assertRaises(changed_labels.classify.CreditDown):
                await waiting
        with contextlib.redirect_stderr(io.StringIO()), \
                mock.patch.object(changed_labels.classify.logs, "step_status"):
            asyncio.run(asyncio.wait_for(scenario(), 10))
        self.assertLess(clock.now, changed_labels.classify.CREDIT_WAIT_SECONDS)
        self.assertTrue(comparison.stopped)

    def test_a_comparison_that_stops_ends_a_credit_pause(self):
        async def stop(comparison):
            comparison.stop(RuntimeError("GitHub rejects every GitHub access token"))
        self.stops_and_ends_the_pause(stop)

    def test_an_unexpected_error_or_a_retired_model_ends_a_credit_pause(self):
        """A defect that raises in one repeat, and a model that the API no longer offers, stop
        the comparison as a refusal does."""
        for raised in (RuntimeError("a defect"),
                       changed_labels.classify.ModelNotOffered("the model is retired")):
            with self.subTest(raised=type(raised).__name__):
                async def failing(*args, raised=raised, **kwargs):
                    raise raised

                async def stop(comparison):
                    with mock.patch.object(changed_labels, "repeat", failing):
                        await comparison.take({"id": 1, "name": "o/a", "current": {}})
                self.stops_and_ends_the_pause(stop)


class TestComparison(_CompareCase):

    def test_a_label_of_the_second_model_is_repeated_by_the_second_model(self):
        """In the second run, DeepSeek V4.1 Flash reaches its output limit in every attempt at
        o/a's changed README, and the second model labels it false. The repeats of both READMEs
        go to the second model, which made the current label, and both keep false: the model
        that made the label changed."""
        glm = _always("false")
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                  _Model(_always("true")), **DEEPSEEK, no_second_model=False,
                                  second_client=_Glm(glm))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Runaway({"o/a"}), **DEEPSEEK, no_second_model=False,
                                  second_client=_Glm(glm))
        self.assertEqual(code, 0, err)
        model = _Glm(glm)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual([request["model"] for request in model.requests],
                         ["z-ai/glm-5.3", "z-ai/glm-5.3"])
        [change] = self.recorded_changes()
        self.assertEqual((change["name"], change["cause"], change["changed"]),
                         ("o/a", changed_labels.CLASSIFIER_CHANGE, ["readme", "revision",
                                                                    "model"]))
        self.assertEqual(change["current"]["second_model"]["model"], "z-ai/glm-5.3")
        self.assertEqual(change["previous"]["second_model"], "")

    def test_a_label_of_the_second_model_is_not_repeated_while_the_second_model_cannot_label(
            self):
        """In the second run, the second model labels o/a, and DeepSeek V4.1 Flash labels o/b's
        changed README otherwise than the first run. In an invocation in which the second model
        labels nothing, as while its endpoint is unserved, a repeat of o/a would fail as the
        labeling's requests would, and the comparison would exit 1 in every invocation. The
        comparison repeats o/b alone and leaves o/a's cause undetermined, with the reason."""
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        glm = _always("false")
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                  _Model(_always("true")), **DEEPSEEK, no_second_model=False,
                                  second_client=_Glm(glm))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        self.github.readmes["o/b"] = (CHANGED, "s5")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Runaway({"o/a"}, script=_always("false")), **DEEPSEEK,
                                  no_second_model=False, second_client=_Glm(glm))
        self.assertEqual(code, 0, err)
        reason = ("The endpoint morph/fp8 of z-ai/glm-5.3 lacks max_tokens among its supported "
                  "parameters")
        model = _Glm(_always("false"))
        code, err = self.compare(model, second_model_unavailable=reason)
        self.assertEqual(code, 0, err)
        self.assertEqual({request["model"] for request in model.requests},
                         {"deepseek/deepseek-v4.1-flash"})
        self.assertEqual({_repository_of(request) for request in model.requests}, {"o/b"})
        changes = {change["name"]: change for change in self.recorded_changes()}
        self.assertEqual(changes["o/a"]["cause"], U)
        self.assertNotEqual(changes["o/b"]["cause"], U)
        self.assertIn(reason, changes["o/a"]["repeat"]["unavailable"])
        self.assertIn("WARNING repository_labeling: The second model labels nothing in this "
                      "invocation. " + reason, err)

    def test_a_label_of_the_second_model_that_an_earlier_invocation_repeated_is_kept(self):
        """In the second run, the second model labels o/a and o/b, each otherwise than the first
        run. A first comparison repeats o/a's labels with the second model, and the API refuses
        the requests for o/b, which stops it. The next invocation, in which the second model
        labels nothing, keeps o/a's repeated labels and their cause from the partial file and
        sends no request. Only o/b, which no invocation repeated, keeps an undetermined cause,
        with the reason."""
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        glm = _always("false")
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                  _Model(_always("true")), **DEEPSEEK, no_second_model=False,
                                  second_client=_Glm(glm))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        self.github.readmes["o/b"] = (CHANGED, "s5")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Runaway({"o/a", "o/b"}), **DEEPSEEK, no_second_model=False,
                                  second_client=_Glm(glm))
        self.assertEqual(code, 0, err)
        refused = SimpleNamespace(request=SimpleNamespace(), status_code=401, headers={})

        def refusing_b(request, number):
            if _repository_of(request) == "o/b":
                raise openai.AuthenticationError("Incorrect API key provided", response=refused,
                                                 body=None)
            return glm(request, number)
        stopped = _Glm(refusing_b)
        code, err = self.compare(stopped)
        self.assertEqual(code, 1, err)
        self.assertEqual({(request["model"], _repository_of(request))
                          for request in stopped.requests if _repository_of(request) == "o/a"},
                         {("z-ai/glm-5.3", "o/a")})
        reason = ("The endpoint morph/fp8 of z-ai/glm-5.3 lacks max_tokens among its supported "
                  "parameters")
        model = _Glm(glm)
        code, err = self.compare(model, second_model_unavailable=reason)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])
        changes = {change["name"]: change for change in self.recorded_changes()}
        self.assertEqual((changes["o/a"]["cause"], changes["o/a"]["changed"]),
                         (C, ["readme", "revision", "model"]))
        self.assertNotIn("unavailable", changes["o/a"]["repeat"])
        self.assertEqual(changes["o/b"]["cause"], U)
        self.assertIn(reason, changes["o/b"]["repeat"]["unavailable"])
        self.assertIn("WARNING repository_labeling: 1 of 2 repositories have no repeated label "
                      "and an undetermined cause. The first is o/b (the second model made the "
                      "current label and labels nothing in this invocation. " + reason, err)

    def test_a_previous_input_is_repeated_with_the_activity_that_the_classifier_shows(self):
        """The previous run's classifier showed the model the stars and the forks, and the
        current classifier leaves them out. A repeat of the previous README with them would give
        the model an input that the current classifier never gives, and the cause of the change
        would depend on that input."""
        classify = changed_labels.classify
        old_fields = (*classify.ACTIVITY_FIELDS, ("stargazers", "stars"), ("forks", "forks"))
        first = _repositories("o/a", "o/b")
        for repository in first:
            repository["activity"] = {"commits": 100, "stars": 500, "forks": 30}
        with mock.patch.object(classify, "ACTIVITY_FIELDS", old_fields):
            code, err = self.run_over(first, self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        second = _repositories("o/a", "o/b", revision=LATER)
        for repository in second:
            repository["activity"] = {"commits": 120}
        code, err = self.run_over(second, self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        previous_input = model.requests[1]["input"]
        self.assertIn("commits: 100", previous_input)
        self.assertNotIn("stars", previous_input)

    def test_a_repeat_takes_the_results_that_its_decision_recorded(self):
        """Both runs read the metadata with the description that GitHub gave at the time of each
        run, and GitHub has changed the description since. Repeated with the metadata read anew,
        an input would differ from the input that its decision was made on, and the cause of the
        change would depend on GitHub's current data."""
        bridge = _Metadata("A formatter, as GitHub described it in October.")
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(_by_readme),
                                  bridge=bridge)
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(_by_readme), bridge=bridge)
        self.assertEqual(code, 0, err)
        bridge.description = "A linter, as GitHub describes it in November."
        bridge.calls.clear()
        model = _Model(_by_readme)
        code, err = self.compare(model, bridge=bridge)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.changes()[0]["cause"], I)
        # The current input, then the previous input.
        self.assertEqual(len(model.requests), 2)
        for request in model.requests:
            self.assertIn("as GitHub described it in October", request["input"])
            self.assertNotIn("in November", request["input"])
        self.assertEqual(bridge.calls, [])

    def test_a_repeat_reads_anew_the_results_that_its_decision_lacks(self):
        """The classification reads the outline, the metadata and the issues and pull requests
        itself before the first request. When a decision recorded none of them for its first
        request, the repeat of its input reads them anew, and the repeat of the other input takes
        those that its decision recorded."""
        classify = changed_labels.classify
        read_first = classify.Classifier.read_first

        async def unrecorded(classifier, *arguments):
            """The input of a first request, with the tool calls made for it left out of the
            decision. The eighth argument is the state of the decision."""
            text = await read_first(classifier, *arguments)
            arguments[7]["tools"][:] = [call for call in arguments[7]["tools"]
                                        if call["round"] != 0]
            return text
        bridge = _Metadata("A formatter, as GitHub described it in October.")
        with mock.patch.object(classify.Classifier, "read_first", unrecorded):
            code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                      _Model(_by_readme), bridge=bridge)
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(_by_readme), bridge=bridge)
        self.assertEqual(code, 0, err)
        bridge.description = "A linter, as GitHub describes it in November."
        bridge.calls.clear()
        model = _Model(_by_readme)
        code, err = self.compare(model, bridge=bridge)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        current_input, previous_input = (request["input"] for request in model.requests)
        self.assertIn("in October", current_input)
        self.assertNotIn("in November", current_input)
        self.assertIn("as GitHub describes it in November", previous_input)
        self.assertEqual(sorted(name for name, _ in bridge.calls),
                         ["get_file_tree", "get_issues_and_pull_requests",
                          "get_repository_metadata"])

    def test_the_repeat_gives_the_tools_the_credentials_file_of_the_comparison(self):
        """The MCP server of the tools reads the credentials file that the comparison reads, as
        it does in the classification."""
        self.first_and_second(_by_readme)
        opened = []

        def bridge_for(settings):
            opened.append(changed_labels.classify.open_bridge(settings).arguments)
            return _Bridge()
        code, err = self.compare(_Model(_by_readme), bridge_for=bridge_for,
                                 credentials=Path("/elsewhere/credentials.csv"))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0][opened[0].index("--credentials") + 1],
                         "/elsewhere/credentials.csv")

    def test_the_classification_compares_the_latest_run_and_finishes_a_started_comparison(self):
        """The comparison is the last step of a run over the sampling frame. A run with a
        comparison that an earlier invocation started and left unfinished is compared first. A run
        that was never compared is left alone, because the comparison labels again with this
        code's classifier, which may differ from the run's classifier."""
        self.first_and_second(_by_readme)
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(_by_readme), retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        runs = [row["run_id"] for row in self.index()]
        compared, unavailable = [], []

        def compare(args):
            compared.append(args.run)
            unavailable.append(args.second_model_unavailable)
            label_cache.put_index_row({"run_id": args.run, "compared_with": "earlier"},
                                      self.cache)
            return 0
        classify = changed_labels.classify
        label_cache.changes_partial_path(self.cache, runs[0]).write_text("")
        self.assertEqual(classify.compare_with_previous_runs(
            self.cache, self.out, 2, None, compare=compare,
            second_model_unavailable="OPENROUTER_API_KEY is unset"), 0)
        self.assertEqual(compared, [runs[2]])
        # The comparison learns why the second model labels nothing in the invocation.
        self.assertEqual(unavailable, ["OPENROUTER_API_KEY is unset"])
        compared.clear()
        label_cache.put_index_row({"run_id": runs[2], "compared_with": ""}, self.cache)
        label_cache.changes_partial_path(self.cache, runs[1]).write_text("")
        self.assertEqual(classify.compare_with_previous_runs(self.cache, self.out, 2, None,
                                                             compare=compare), 0)
        self.assertEqual(compared, runs[1:])
        compared.clear()
        self.assertEqual(classify.compare_with_previous_runs(self.cache, self.out, 2, None,
                                                             compare=compare), 0)
        self.assertEqual(compared, [])

    def test_the_first_run_has_nothing_to_compare_with(self):
        code, err = self.run_over(_repositories("o/a"), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.changes(), [])
        self.assertEqual(model.requests, [])
        self.assertIn("no earlier run to compare it with", err)

    def test_the_repeat_applies_the_fork_heuristic_as_the_run_did(self):
        """The changed README calls o/a a fork that tracks its upstream, and the run labeled o/a
        false by the fork heuristic. A repeat of the model's response alone would give true and
        attribute the change to model variation."""
        decided = []

        def independence(client, found, revision, upstream, thresholds):
            decided.append(thresholds)
            return {"upstream": upstream, "independent": False,
                    "reasons": ["tracks its upstream"]}
        patcher = mock.patch.object(forks, "independence", independence)
        patcher.start()
        self.addCleanup(patcher.stop)

        def by_readme(request, number):
            text = request["input"] if isinstance(request["input"], str) else ""
            return _answer(_repository_of(request), "true",
                           fork_of="https://github.com/up/a" if "Now with tests" in text else "")
        self.first_and_second(by_readme, by_readme)
        code, err = self.compare(_Model(by_readme))
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["cause"], row["previous"], row["now"], row["repeat_label"],
                          row["previous_input_label"]), (I, "true", "false", "false", "true"))
        self.assertEqual(len(decided), 2)
        self.assertEqual(decided[0], decided[1])

    def test_a_label_that_follows_its_readme_is_an_input_change(self):
        self.first_and_second(_by_readme)
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["previous"], row["now"]),
                         ("o/a", I, "true", "false"))
        self.assertEqual((row["repeat_label"], row["previous_input_label"]), ("false", "true"))
        self.assertEqual(row["changed"], "readme revision")
        # The repeat reads the current README, and the second request the previous README.
        self.assertIn("Now with tests", model.requests[0]["input"])
        self.assertNotIn("Now with tests", model.requests[1]["input"])
        self.assertEqual(len(model.requests), 2)
        [record] = self.recorded_changes()
        self.assertEqual(record["previous"]["readme_sha"], "s1")
        self.assertEqual(record["previous_input"]["label"], "true")
        runs = self.index()
        self.assertEqual(runs[1]["compared_with"], runs[0]["run_id"])
        self.assertEqual(runs[1]["label_changes"], "1")
        # The repeated labels count among the run's requests.
        self.assertEqual(int(runs[1]["model_requests"]), 1 + 2)

    def test_a_refused_endpoint_stops_the_comparison_before_its_first_repeat(self):
        """While OpenRouter's lists show that the run's models cannot serve the requests as they
        are (`classify.endpoint_check`), the labeling passes the reason, and a comparison with a
        label to repeat exits 1 with the ERROR before its first request. The next invocation
        compares the run, with the repeat."""
        self.first_and_second(_by_readme)
        model = _Model(_by_readme)
        reason = "OpenRouter lists no endpoint morph/fp8 of deepseek/deepseek-v4.1-flash"
        code, err = self.compare(model, unserved=reason)
        self.assertEqual(code, 1, err)
        self.assertEqual((model.requests, self.connected), ([], 0))
        self.assertIn(f"ERROR repository_labeling: {reason}.", err)
        self.assertEqual(self.index()[1]["compared_with"], "")
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)

    def test_a_label_the_same_readme_does_not_keep_is_model_variation(self):
        self.first_and_second(_always("false"), first_model=_always("true"))
        model = _Model(_always("true"))
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        rows = {row["repo_name"]: row for row in self.changes()}
        # The run reused o/b's label because its README did not change, and o/a's repeat gave
        # true.
        self.assertEqual(list(rows), ["o/a"])
        self.assertEqual((rows["o/a"]["cause"], rows["o/a"]["repeat_label"]), (V, "true"))
        self.assertEqual(rows["o/a"]["previous_input_label"], "")
        self.assertEqual(len(model.requests), 1)

    def test_the_repeats_count_in_the_tokens_file_and_the_cost_record_of_the_run(self):
        """The repeats are requests of the compared run, and the index adds their tokens to those
        of the run. The tokens file of the run counts them by the tier that their responses
        report, and the comparison writes the cost record of the run again with them. Here the
        run's request was served on flex and the repeat on the standard tier."""
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(_always("true")))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Billed(_always("false"), lambda number: "flex"))
        self.assertEqual(code, 0, err)
        rid = self.index()[-1]["run_id"]
        model = _Billed(_always("true"), lambda number: "default")
        code, err = self.compare(model, service_tier="flex")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        self.assertEqual([(row["requested_tier"], row["service_tier"], row["requests"])
                          for row in label_cache.read_tokens(self.cache, rid)],
                         [("flex", "default", 1), ("flex", "flex", 1)])
        with open(label_cache.cost_path(self.cache, rid), newline="", encoding="utf-8") as f:
            record = list(csv.DictReader(f))
        row = self.index()[-1]
        self.assertEqual(sum(int(r["tokens"]) for r in record if r["billed_as"] != "output"),
                         int(row["input_tokens"]))
        self.assertEqual(sum(int(r["tokens"]) for r in record if r["billed_as"] == "output"),
                         int(row["output_tokens"]))
        self.assertEqual({r["service_tier"] for r in record}, {"flex", "default"})
        self.assertEqual({r["note"] for r in record}, {""})
        self.assertIn(f"INFO repository_labeling: Run {rid} cost $", err)

    def test_the_previous_readme_labeled_as_now_by_the_same_classifier_is_model_variation(self):
        self.first_and_second(_always("false"), first_model=_always("true"))
        model = _Model(_always("false"))
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["cause"], row["repeat_label"], row["previous_input_label"]),
                         (V, "false", "false"))

    def test_a_new_model_that_gives_the_same_readme_another_label_is_a_classifier_change(self):
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(_always("true")))
        self.assertEqual(code, 0, err)
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                  _Model(_always("false")), model="gpt-6.1-sol")
        self.assertEqual(code, 0, err)
        model = _Model(_always("false"))
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        rows = self.changes()
        self.assertEqual({row["cause"] for row in rows}, {C})
        self.assertEqual({row["changed"] for row in rows}, {"model"})
        # With an unchanged input, only the repeat is requested, from the run's own model.
        self.assertEqual(len(model.requests), 2)
        self.assertEqual({request["model"] for request in model.requests}, {"gpt-6.1-sol"})

    def test_the_repeat_of_a_repository_that_github_marks_as_a_mirror_reads_the_mark(self):
        """GitHub marks o/a as a mirror, as it marks luci/recipes-py, and a new model labels it
        false through the mark while its response does not call o/a a mirror. The repeat reads
        the mark that the decision recorded, gives the same label, and the change is a
        classifier change, not model variation."""
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(_always("true")))
        self.assertEqual(code, 0, err)
        marked = _repositories("o/a", "o/b")
        marked[0]["is_mirror"] = True
        code, err = self.run_over(marked, self.github, _Model(_always("true")),
                                  model="gpt-6.1-sol")
        self.assertEqual(code, 0, err)
        code, err = self.compare(_Model(_always("true")))
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["previous"], row["now"], row["repeat_label"],
                          row["cause"]), ("o/a", "true", "false", "false", C))

    def test_a_label_carried_over_under_a_fixed_rule_is_a_classifier_change_without_the_model(
            self):
        """A repeat could only attribute the change to model variation, because the model's
        response is the same in both runs."""
        def script(request, number):
            return _answer(_repository_of(request),
                           facts={"work_in_progress": {"unfinished": "true", "in_use": "true"}})
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(script))
        self.assertEqual(code, 0, err)
        rule = {"work_in_progress": lambda facts: facts["unfinished"]}
        with mock.patch.dict(changed_labels.classify.CRITERION_APPLIES, rule):
            code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(script))
        self.assertEqual(code, 0, err)
        model = _Model(script)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual({(row["repo_name"], row["cause"]) for row in self.changes()},
                         {("o/a", C), ("o/b", C)})
        self.assertEqual(model.requests, [])

    def test_a_label_carried_twice_across_a_killed_run_is_attributed_without_the_model(self):
        """A run over a new sampling frame carries a fork's label over under the run's
        thresholds, and a kill ends the run. The next run, over another sampling frame, carries
        the label again. The previous run's decision stays among the decisions that the label
        was carried from, and the change is attributed to GitHub's facts without a repeat. A
        repeat could only find that the model's stored response gives the label that it gave."""
        # A clock of the cache of labels that moves a second per reading, so that each run's
        # decision has a decided_at of its own.
        start, seconds = label_cache.utc_now(), itertools.count(1)
        clock = mock.patch.object(label_cache, "utc_now",
                                  lambda: start + timedelta(seconds=next(seconds)))
        clock.start()
        self.addCleanup(clock.stop)
        verdict = {"independent": True}

        def independence(client, found, revision, upstream, thresholds):
            return {"upstream": upstream, "thresholds": thresholds.as_dict(), "reasons": [],
                    **verdict}
        patcher = mock.patch.object(forks, "independence", independence)
        patcher.start()
        self.addCleanup(patcher.stop)

        class Killed(KeyboardInterrupt):
            pass

        def killed_at_the_end(*arguments):
            raise Killed()

        def fork(request, number):
            return _answer(_repository_of(request), fork_of="https://github.com/up/a")
        code, err = self.run_over(_repositories("o/a"), self.github, _Model(fork))
        self.assertEqual(code, 0, err)
        verdict["independent"] = False
        with mock.patch.object(changed_labels.classify, "report", killed_at_the_end), \
                self.assertRaises(Killed):
            self.run_over(_repositories("o/a"), self.github, _Model(fork),
                          retrieval="2026-11-05T101430Z",
                          thresholds=forks.Thresholds(author_minimum=2,
                                                      window_start="2026-04-22"))
        code, err = self.run_over(_repositories("o/a"), self.github, _Model(fork),
                                  retrieval="2026-12-05T101430Z",
                                  thresholds=forks.Thresholds(author_minimum=2,
                                                              window_start="2026-05-22"))
        self.assertEqual(code, 0, err)
        self.assertEqual([row["outcome"] for row in self.index()],
                         ["finished", "running", "finished"])
        model = _Model(fork)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["previous"], row["now"]),
                         ("o/a", I, "true", "false"))
        self.assertEqual((model.requests, self.connected), ([], 0))

    def test_the_repeat_reads_the_descriptions_that_the_decisions_recorded(self):
        frame = [{**r, "description": "A formatter."} for r in _repositories("o/a", "o/b")]

        def deprecated(request, number):
            text = request["input"] if isinstance(request["input"], str) else ""
            return _answer(_repository_of(request), "false" if "deprecated" in text else "true")
        code, err = self.run_over(frame, self.github, _Model(deprecated))
        self.assertEqual(code, 0, err)
        frame[0]["description"] = "A deprecated formatter."
        code, err = self.run_over(frame, self.github, _Model(deprecated))
        self.assertEqual(code, 0, err)
        model = _Model(deprecated)
        sent = len(self.github.sent)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"]), ("o/a", I))
        inputs = [request["input"] for request in model.requests]
        self.assertEqual(sorted("deprecated" in text for text in inputs), [False, True])
        self.assertNotIn("https://api.github.com/repos/o/a", self.github.sent[sent:])

    def test_an_exclusion_that_ends_with_a_new_readme_needs_no_model(self):
        del self.github.readmes["o/b"]
        self.github.repositories["o/b"] = {"id": 2, "full_name": "o/b"}
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        # o/b gains a README at a later commit, and o/a's README stays as it was.
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/a", LATER)] = (ENGLISH, "s1")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(_by_readme))
        self.assertEqual(code, 0, err)
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["previous"], row["now"]),
                         ("o/b", I, "excluded: no_readme_found", "true"))
        self.assertEqual(model.requests, [])
        self.assertEqual(self.connected, 0)

    def test_a_previous_readme_that_cannot_be_read_leaves_the_cause_undetermined(self):
        self.first_and_second(_by_readme)
        # The previous revision is gone: GitHub returns the README of the default branch.
        del self.github.by_revision[("o/a", REVISION)]
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["cause"], row["repeat_label"], row["previous_input_label"]),
                         (U, "false", ""))
        [record] = self.recorded_changes()
        self.assertIn("not the blob s1", record["previous_input"]["unavailable"])
        self.assertEqual(len(model.requests), 1)

    def test_a_current_readme_that_cannot_be_read_is_not_labeled_again(self):
        self.first_and_second(_by_readme)
        self.github.by_revision[("o/a", LATER)] = (CHANGED + " Edited.", "s4")
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["cause"], row["repeat_label"]), (U, ""))
        self.assertEqual(model.requests, [])

    def test_a_repeat_that_fails_is_tried_again_in_a_retry_round(self):
        """A dropped connection fails each attempt of a repeat. A retry round makes the repeat
        again, and the cause is determined."""
        self.first_and_second(_by_readme)
        dropped = {"left": changed_labels.classify.ATTEMPTS}

        def script(request, number):
            if dropped["left"]:
                dropped["left"] -= 1
                raise openai.APIConnectionError(request=SimpleNamespace())
            return _by_readme(request, number)
        code, err = self.compare(_Model(script))
        self.assertEqual(code, 0, err)
        self.assertIn("Retry round 1 of", err)
        self.assertIn("for the 1 repositories without their repeated labels", err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"]), ("o/a", I))

    def test_a_retry_round_keeps_the_repeat_made_before_github_failed(self):
        """GitHub fails the request for the fork's facts of the previous README after the model
        has labeled both READMEs again. The retry round takes the current README's repeated label
        from the round before and requests GitHub's facts of the previous README again. A second
        label of the current README would cost a further request and could differ from the
        first."""
        failing = {"left": 0}

        def independence(client, found, revision, upstream, thresholds):
            if failing["left"] and revision == REVISION:
                failing["left"] -= 1
                raise readmes.NoResponse("o/a: GitHub returned HTTP 502 for the upstream's "
                                         "commits")
            return {"upstream": upstream, "thresholds": thresholds.as_dict(),
                    "independent": True, "reasons": []}
        patcher = mock.patch.object(forks, "independence", independence)
        patcher.start()
        self.addCleanup(patcher.stop)

        def fork(request, number):
            text = request["input"] if isinstance(request["input"], str) else ""
            return _answer(_repository_of(request), "false" if "Now with tests" in text else "true",
                           fork_of="https://github.com/up/a")
        self.first_and_second(fork, fork)
        current = []

        def labels_the_current_readme_once(request, number):
            text = request["input"] if isinstance(request["input"], str) else ""
            if "Now with tests" in text:
                current.append(number)
                # A second label of the current README differs from the first.
                return _answer(_repository_of(request), "false" if len(current) == 1 else "true",
                               fork_of="https://github.com/up/a")
            return fork(request, number)
        failing["left"] = 1
        model = _Model(labels_the_current_readme_once)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertIn("Retry round 1 of", err)
        self.assertIn("for the 1 repositories without their repeated labels", err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["repeat_label"],
                          row["previous_input_label"]), ("o/a", I, "false", "true"))
        self.assertEqual((len(model.requests), len(current)), (2, 1))

    def test_a_response_kept_for_githubs_facts_is_not_paid_again_after_a_stop(self):
        """After the model has labeled the previous README again, GitHub rejects every GitHub
        access token in the request for the fork's facts of that README, and the rejection stops
        the comparison. The next invocation takes the current README's repeated label and the
        previous README's response from the partial file and reads only GitHub's facts. A
        response kept in memory alone would be paid for again."""
        rejecting = {"on": False}

        def independence(client, found, revision, upstream, thresholds):
            if rejecting["on"] and revision == REVISION:
                raise NoTokens("GitHub accepts none of the configured GitHub access tokens")
            return {"upstream": upstream, "thresholds": thresholds.as_dict(),
                    "independent": True, "reasons": []}
        patcher = mock.patch.object(forks, "independence", independence)
        patcher.start()
        self.addCleanup(patcher.stop)

        def fork(request, number):
            text = request["input"] if isinstance(request["input"], str) else ""
            return _answer(_repository_of(request), "false" if "Now with tests" in text else "true",
                           fork_of="https://github.com/up/a")
        self.first_and_second(fork, fork)
        rejecting["on"] = True
        stopped = _Model(fork)
        code, err = self.compare(stopped)
        self.assertEqual((code, len(stopped.requests)), (1, 2), err)
        self.assertIn("GitHub accepts none of the configured GitHub access tokens", err)
        rejecting["on"] = False
        model = _Model(fork)
        code, err = self.compare(model)
        self.assertEqual((code, model.requests), (0, []), err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["repeat_label"],
                          row["previous_input_label"]), ("o/a", I, "false", "true"))

    def test_an_outage_of_github_during_the_comparison_is_waited_out(self):
        """GitHub returns HTTP 502 to every request for an hour while the comparison repeats
        labels, longer than its retry rounds take. Every worker pauses as in the labeling, and
        the causes are determined once GitHub responds again, instead of the comparison ending
        with more failed repeats than the ceiling."""
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        self.first_and_second(_by_readme)
        clock, outage = _Clock(), 3600
        self.assertGreater(outage, changed_labels.classify.RETRY_ROUNDS * 60)
        responding = self.github.get

        def failing(url, headers=None, timeout=None):
            if clock() < outage:
                return _Response(502, {})
            return responding(url, headers, timeout)
        self.github.get = failing
        on_the_clock = functools.partial(changed_labels.classify.Classifier, sleep=clock.sleep,
                                         clock=clock)
        with mock.patch.object(changed_labels.classify, "Classifier", on_the_clock), \
                mock.patch.object(changed_labels.classify, "RETRY_ROUND_SECONDS", 60):
            code, err = self.compare(_Model(_by_readme))
        self.assertEqual(code, 0, err)
        self.assertEqual({row["repo_name"]: row["cause"] for row in self.changes()},
                         {"o/a": I, "o/b": I})
        self.assertGreaterEqual(clock.now, outage)
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)

    def test_a_resumed_comparison_does_not_pause_for_the_repeats_that_github_failed_before(self):
        """GitHub fails every request for o/b and o/c, each between the repeats of o/a. The
        failed repeats exceed the ceiling, and the comparison exits 1 with its partial file kept.
        The next invocation holds only these two repeats. If their failures counted toward the
        pause, every worker would pause through every wait of GITHUB_PAUSES. The two repeats
        fail on their own, and the comparison is written with their causes undetermined."""
        names = ("o/b", "o/a", "o/c")
        for name, sha in (("o/b", "s2"), ("o/c", "s4")):
            self.github.readmes[name] = (ENGLISH, sha)
            self.github.by_revision[(name, REVISION)] = (ENGLISH, sha)
            self.github.by_revision[(name, LATER)] = (CHANGED, sha + "-later")
        code, err = self.run_over(_repositories(*names), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        for name in names:
            self.github.readmes[name] = self.github.by_revision[(name, LATER)]
        code, err = self.run_over(_repositories(*names, revision=LATER), self.github,
                                  _Model(_by_readme))
        self.assertEqual(code, 0, err)
        responding = self.github.get

        def failing(url, headers=None, timeout=None):
            if "/repos/o/b/" in url or "/repos/o/c/" in url:
                return _Response(502, {})
            return responding(url, headers, timeout)
        self.github.get = failing
        clock = _Clock()
        on_the_clock = functools.partial(changed_labels.classify.Classifier, sleep=clock.sleep,
                                         clock=clock)
        with mock.patch.object(changed_labels.classify, "Classifier", on_the_clock):
            code, err = self.compare(_Model(_by_readme), workers=1)
            self.assertEqual(code, 1, err)
            self.with_ceiling(lambda repositories: 2)
            code, err = self.compare(_Model(_by_readme))
        self.assertEqual(code, 0, err)
        self.assertEqual({row["repo_name"]: row["cause"] for row in self.changes()},
                         {"o/a": I, "o/b": U, "o/c": U})
        self.assertLess(clock.now, changed_labels.classify.GITHUB_PAUSES[0])
        self.assertNotIn("Pausing every worker", err)

    def test_an_outage_of_github_that_begins_in_the_retry_rounds_is_waited_out(self):
        """In the first round, GitHub fails the repeats of o/b and o/c on their own, each while
        it responds for o/a. From the start of the first retry round, GitHub then fails every
        request for twenty minutes, longer than the retry rounds take. The retry round holds only
        the two repeats that GitHub failed before. GitHub also returns no response to the request
        for the README of o/a, which it served last. The failures of the two repeats therefore count
        toward the pause, and both causes are determined after the pause. Left on their own, the
        two repeats would fail in every round of the outage, and the comparison would end with
        more failed repeats than the ceiling."""
        names = ("o/b", "o/a", "o/c")
        for name, sha in (("o/b", "s2"), ("o/c", "s4")):
            self.github.readmes[name] = (ENGLISH, sha)
            self.github.by_revision[(name, REVISION)] = (ENGLISH, sha)
            self.github.by_revision[(name, LATER)] = (CHANGED, sha + "-later")
        code, err = self.run_over(_repositories(*names), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        for name in names:
            self.github.readmes[name] = self.github.by_revision[(name, LATER)]
        code, err = self.run_over(_repositories(*names, revision=LATER), self.github,
                                  _Model(_by_readme))
        self.assertEqual(code, 0, err)
        clock, outage = _Clock(), {"until": None}
        self.assertGreater(20 * 60, changed_labels.classify.RETRY_ROUNDS * 90)
        on_the_clock = clock.sleep

        async def sleep(seconds):
            if seconds == 90 and outage["until"] is None:
                outage["until"] = clock.now + 20 * 60
            await on_the_clock(seconds)
        clock.sleep = sleep
        responding = self.github.get

        def get(url, headers=None, timeout=None):
            if outage["until"] is None:
                if "/repos/o/b/" in url or "/repos/o/c/" in url:
                    return _Response(502, {})
            elif clock() < outage["until"]:
                self.github.sent.append(url)
                return _Response(502, {})
            return responding(url, headers, timeout)
        self.github.get = get
        classifier = functools.partial(changed_labels.classify.Classifier, sleep=clock.sleep,
                                       clock=clock)
        with mock.patch.object(changed_labels.classify, "Classifier", classifier), \
                mock.patch.object(changed_labels.classify, "RETRY_ROUND_SECONDS", 90):
            code, err = self.compare(_Model(_by_readme), workers=1)
        self.assertEqual(code, 0, err)
        self.assertEqual({row["repo_name"]: row["cause"] for row in self.changes()},
                         {"o/a": I, "o/b": I, "o/c": I})
        self.assertIn("Retry round 1 of", err)
        self.assertIn("for the 2 repositories without their repeated labels", err)
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)
        self.assertGreaterEqual(clock.now, outage["until"])
        # The README of o/a, which no retry round holds, was requested again in the outage.
        self.assertTrue(any("/repos/o/a/readme" in url for url in self.github.sent))

    def test_an_outage_of_the_readme_requests_under_way_at_the_start_is_waited_out(self):
        """GitHub fails every README request for twenty minutes from the start of the comparison
        and responds to `/rate_limit`. The request for the README of a compared decision is the
        request that tells whether GitHub responds. Every worker therefore pauses, and the cause
        is determined after the pause rather than written as undetermined."""
        names = ("o/b", "o/a", "o/c")
        for name, sha in (("o/b", "s2"), ("o/c", "s4")):
            self.github.readmes[name] = (ENGLISH, sha)
            self.github.by_revision[(name, REVISION)] = (ENGLISH, sha)
            self.github.by_revision[(name, LATER)] = (CHANGED, sha + "-later")
        code, err = self.run_over(_repositories(*names), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        for name in names:
            self.github.readmes[name] = self.github.by_revision[(name, LATER)]
        code, err = self.run_over(_repositories(*names, revision=LATER), self.github,
                                  _Model(_by_readme))
        self.assertEqual(code, 0, err)
        clock = _Clock()
        responding = self.github.get

        def get(url, headers=None, timeout=None):
            if "/readme" in url and clock() < 20 * 60:
                self.github.sent.append(url)
                return _Response(502, {})
            return responding(url, headers, timeout)
        self.github.get = get
        classifier = functools.partial(changed_labels.classify.Classifier, sleep=clock.sleep,
                                       clock=clock)
        with mock.patch.object(changed_labels.classify, "Classifier", classifier), \
                mock.patch.object(changed_labels.classify, "RETRY_ROUND_SECONDS", 60):
            code, err = self.compare(_Model(_by_readme), workers=2)
        self.assertEqual(code, 0, err)
        self.assertEqual({row["repo_name"]: row["cause"] for row in self.changes()},
                         {"o/a": I, "o/b": I, "o/c": I})
        self.assertIn("Pausing every worker for 0:05:00, then trying the failed repositories "
                      "again", err)
        self.assertGreaterEqual(clock.now, 20 * 60)

    def test_the_repeated_labels_go_to_the_service_tier_of_the_run(self):
        """The labeling sends its requests to the flex tier, which costs half the standard
        price. The comparison at the end of a run passes the run's tier on, and the comparison's
        repeats go to that tier. On the project's default tier, the repeats would cost twice as
        much."""
        self.first_and_second(_by_readme)
        passed = []

        def compare(args):
            passed.append(args.service_tier)
            return 0
        self.assertEqual(changed_labels.classify.compare_with_previous_runs(
            self.cache, self.out, 2, None, compare=compare, service_tier="flex"), 0)
        self.assertEqual(passed, ["flex"])
        model = _Model(_by_readme)
        code, err = self.compare(model, service_tier="flex")
        self.assertEqual(code, 0, err)
        self.assertEqual([r.get("service_tier") for r in model.requests], ["flex", "flex"])

    def test_the_repeats_send_the_temperature_and_the_seed_that_the_run_recorded(self):
        """A run of an OpenRouter model records the temperature, the top_p, the seed and the
        output limit of its requests among its classification parameters. The repeats of the
        comparison send the recorded values, so that they reproduce the run's requests.
        Parameters without them record requests that sent none of them."""
        def by_readme(request, number):
            # OpenRouter's input is the whole conversation, a list of items.
            changed = "Now with tests" in json.dumps(request["input"])
            return _answer(_repository_of(request), "false" if changed else "true")
        options = {**DEEPSEEK, "temperature": 0.5, "top_p": 0.9, "seed": 3}
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(by_readme),
                                  **options)
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(by_readme), **options)
        self.assertEqual(code, 0, err)
        model = _Model(by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual({(r["temperature"], r["top_p"], r["extra_body"]["seed"],
                           r["max_output_tokens"]) for r in model.requests},
                         {(0.5, 0.9, 3, 65_536)})
        recorded = {"classifying_model": {**DEEPSEEK, "reasoning_effort": "medium"},
                    "summary": {"model": "gpt-6-luna"}}
        self.assertEqual(changed_labels.run_endpoints(recorded, {"model": DEEPSEEK["model"]})[1],
                         changed_labels.classify.endpoints.Settings("medium"))

    def test_the_labels_of_a_run_under_the_parameters_with_the_provider_are_repeated(self):
        """The maintainer decided on 2026-11-04 that the provider is no classification
        parameter. A run whose parameters the earlier code recorded with the provider of each
        model has the parameters of the current code by their keys, and the comparison repeats
        its labels on that provider. A run since records no provider, and the repeats go to the
        pinned provider of its weights, with the backup provider of the model where a failure of
        the pinned provider would use an attempt."""
        def by_readme(request, number):
            changed = "Now with tests" in json.dumps(request["input"])
            return _answer(_repository_of(request), "false" if changed else "true")
        for earlier in (True, False):
            with self.subTest(earlier=earlier):
                self.setUp()
                recording = (mock.patch.object(changed_labels.classify.endpoints.Endpoint,
                                               "parameters", _parameters_with_provider)
                             if earlier else contextlib.nullcontext())
                with recording:
                    code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                              _Model(by_readme), **DEEPSEEK)
                    self.assertEqual(code, 0, err)
                    self.github.readmes["o/a"] = (CHANGED, "s3")
                    code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER),
                                              self.github, _Model(by_readme), **DEEPSEEK)
                    self.assertEqual(code, 0, err)
                recorded = label_cache.recorded("parameters", self.index()[-1]["parameters"],
                                                self.cache)
                self.assertEqual("provider" in recorded["classifying_model"], earlier)
                model = _Model(by_readme)
                code, err = self.compare(model)
                self.assertEqual(code, 0, err)
                self.assertNotIn("differ from the parameters", err)
                self.assertEqual(len(model.requests), 2)
                self.assertEqual({r["extra_body"]["provider"]["order"][0]
                                  for r in model.requests}, {"morph/fp8"})

    def test_the_labels_of_a_model_without_a_pinned_record_are_repeated_on_its_endpoint(self):
        """Since 2026-11-04 the parameters of a run name no provider, and Kimi K3 has no pinned
        record in endpoints.py. The decisions of the run name the endpoint that served each
        response, wafer/us, and the comparison repeats the labels there, rather than leaving
        their causes undetermined because no provider of the current code serves the model."""
        kimi = {"model": "moonshotai/kimi-k3", "provider": "wafer/us", "quantization": "mxfp4",
                "hugging_face_id": "moonshotai/Kimi-K3"}

        def by_readme(request, number):
            changed = "Now with tests" in json.dumps(request["input"])
            return _answer(_repository_of(request), "false" if changed else "true")
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(by_readme),
                                  **kimi)
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(by_readme), **kimi)
        self.assertEqual(code, 0, err)
        recorded = label_cache.recorded("parameters", self.index()[-1]["parameters"], self.cache)
        self.assertNotIn("provider", recorded["classifying_model"])
        model = _Model(by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertNotIn("No provider of the current code serves", err)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual({r["extra_body"]["provider"]["order"][0] for r in model.requests},
                         {"wafer/us"})
        self.assertEqual([row["cause"] for row in self.changes()], ["input_change"])

    def test_digests_that_differ_only_in_the_provider_are_no_changed_input(self):
        """Two decisions under digests with one key, such as the digests of parameters recorded
        with the provider and without it, changed neither the model nor the parameters."""
        keys = {"with-provider": "current", "current": "current"}.get
        shared = {"current": "rest"}.get
        label = {"outcome": "labeled", "model": "deepseek/deepseek-v4.1-flash",
                 "parameters": "with-provider", "readme_sha": "s", "revision": "r",
                 "description": ""}
        current = {**label, "parameters": "current"}
        self.assertEqual(changed_labels.changed_inputs(label, current, shared, keys), [])
        self.assertEqual(changed_labels.changed_inputs(label, current), ["parameters"])

    def test_parameters_recorded_with_the_provider_are_compared_by_their_records(self):
        """Two decisions under parameters recorded before 2026-11-04, each with the provider, that
        differ in the prompt changed the parameters, and two that differ in a setting of the
        classifying model changed the model. The cache holds the records under these digests,
        and no run has recorded the parameters under their keys."""
        cache = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, cache, ignore_errors=True)
        block = {"model": "deepseek/deepseek-v4.1-flash",
                 "hugging_face_id": "deepseek-ai/DeepSeek-V4.1-Flash", "quantization": "fp8",
                 "provider": "morph/fp8", "reasoning_effort": "high"}
        summary = {**block, "reasoning_effort": "low"}
        first, prompt, effort = (
            label_cache.record("parameters", {"prompt": prompt, "classifying_model": model,
                                              "summary": summary}, cache)
            for prompt, model in (("A", block), ("B", block),
                                  ("A", {**block, "reasoning_effort": "max"})))
        keys = label_cache.ParameterKeys(cache)
        self.assertFalse((cache / "parameters" / f"{keys(first)}.json").is_file())
        known: dict = {}

        def shared(sha):
            return label_cache.shared_parameters(cache, sha, known)
        label = {"outcome": "labeled", "model": "deepseek/deepseek-v4.1-flash",
                 "parameters": first, "readme_sha": "s", "revision": "r", "description": ""}
        self.assertEqual(changed_labels.changed_inputs(label, {**label, "parameters": prompt},
                                                       shared, keys), ["parameters"])
        self.assertEqual(changed_labels.changed_inputs(label, {**label, "parameters": effort},
                                                       shared, keys), ["model"])

    def test_a_label_of_the_higher_temperature_is_repeated_at_that_temperature(self):
        """In the second run, the first response of DeepSeek V4.1 Flash at o/a's changed README
        stops at its output limit, and the model labels the README false at its higher
        temperature. The repeats of both READMEs are made at that temperature and with the
        output limit of its attempts, as the label records them, and not with the lower limit
        of the run's other requests. Both keep false: the classifier that made the label
        changed."""
        def by_readme(request, number):
            changed = "Now with tests" in json.dumps(request["input"])
            return _answer(_repository_of(request), "false" if changed else "true")
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(by_readme),
                                  **DEEPSEEK)
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Runaway({"o/a"}, runaways=1, script=by_readme), **DEEPSEEK)
        self.assertEqual(code, 0, err)
        model = _Model(lambda request, number: _answer(_repository_of(request), "false"))
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual([(request["temperature"], request["max_output_tokens"])
                          for request in model.requests], [(1.0, 262_144), (1.0, 262_144)])
        [change] = self.recorded_changes()
        self.assertEqual((change["name"], change["cause"], change["changed"]),
                         ("o/a", changed_labels.CLASSIFIER_CHANGE, ["readme", "revision",
                                                                    "model"]))
        self.assertEqual({name: change["current"]["higher_temperature"][name]
                          for name in ("temperature", "max_output_tokens")},
                         {"temperature": 1.0, "max_output_tokens": 262_144})
        self.assertEqual(changed_labels.labeled_by(change["current"]),
                         "deepseek/deepseek-v4.1-flash at temperature 1.0")

    def test_a_label_of_the_higher_temperature_without_an_output_limit_keeps_the_runs_limit(
            self):
        """A label of the higher temperature that records the temperature, the top_p and the
        seed but no output limit was made while the attempts at the higher temperature sent the
        limit of the run's other requests. Its repeat sends that limit and does not drop the
        limit from the request."""
        settings = changed_labels.classify.endpoints.Settings("high", 262_144, 0.6, 0.95, 7)
        decision = {"higher_temperature": {"temperature": 1.0, "top_p": 0.95, "seed": 7}}
        repeated = changed_labels.fallback_classifier(decision, None, settings,
                                                      lambda endpoint, made: made)
        self.assertEqual(repeated,
                         changed_labels.classify.endpoints.Settings("high", 262_144, 1.0, 0.95, 7))

    def test_the_readers_of_a_comparisons_partial_file_hold_one_line_at_a_time(self):
        """A comparison's partial file keeps the repeated labels with their whole tool outputs.
        Each reader of the file holds one line at a time, and its peak memory stays a small
        multiple of the longest line however long the file is. Each reader reads what the file
        holds except the last line, which a stop cut inside a UTF-8 character."""
        lines = [{"previous_run": "2026-10-06T140917Z"}]
        for number in range(1, 151):
            lines.append({"id": number, "status": changed_labels.REPEATED,
                          "repeat": {"label": "true", "tools": [{"output": "x" * 200_000}]},
                          "spent": {"model_requests": 1}})
            lines.append({"id": number, "status": changed_labels.COMPARED,
                          "change": {"id": number, "justification": "Ein Werkzeug 🛠 für "
                                                                    "Dateien."},
                          "spent": {"github_requests": 2}})
        lines.append({"id": 150, "status": changed_labels.FAILED, "reason": "github",
                      "error": "NoResponse: GitHub returned HTTP 502"})
        lines.append({"id": 151, "status": changed_labels.REPEATED, "repeat": {"label": "false"}})
        path = self.tmp / "2026-11-05T080000Z.changes.partial.jsonl"
        longest = write_partial(path, lines)
        self.assertGreater(path.stat().st_size, 100 * longest)
        for reader, expected in (
                (lambda: sorted(changed_labels.read_partial(path)), list(range(1, 150))),
                (lambda: changed_labels.read_repeated(path), {151: {"label": "false"}}),
                (lambda: label_cache.spent_in(path, ("model_requests", "github_requests")),
                 {"model_requests": 150, "github_requests": 300}),
                (lambda: changed_labels.compared_in(path), "2026-10-06T140917Z")):
            result, peak = traced_peak(reader)
            self.assertEqual(result, expected)
            self.assertLess(peak, 10 * longest)

    def test_a_repeat_that_fails_through_every_round_leaves_the_cause_undetermined(self):
        """The comparison is the last part of the labeling. A comparison that exited 1 over one
        repository for which GitHub or the model fails every time would keep every stage after
        the classification from running. While the failures stay within the classification's
        ceiling for as many repositories, the cause stays undetermined with the error, and the
        comparison is written."""
        self.real_ceiling()
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        self.first_and_second(_by_readme)

        def fails_for_b(request, number):
            # A response without a message fails the attempt.
            return [] if _repository_of(request) == "o/b" else _by_readme(request, number)
        model = _Model(fails_for_b)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual({row["repo_name"]: row["cause"] for row in self.changes()},
                         {"o/a": I, "o/b": U})
        self.assertEqual([_repository_of(r) for r in model.requests].count("o/b"),
                         (changed_labels.classify.RETRY_ROUNDS + 1)
                         * changed_labels.classify.ATTEMPTS)
        record = next(r for r in self.recorded_changes() if r["name"] == "o/b")
        self.assertEqual(record["repeat"], {"unavailable": "ModelFailure: the model's response "
                                                           "does not call submit_classification "
                                                           "(the status none, no output items "
                                                           "and 10 output tokens)"})
        self.assertIn("1 of 2 repositories have no repeated label and an undetermined cause. "
                      "The first is o/b", err)
        self.assertEqual(self.index()[1]["compared_with"], self.index()[0]["run_id"])
        self.assertFalse(label_cache.changes_partial_path(self.cache,
                                                          self.index()[1]["run_id"]).exists())

    def test_a_repeat_the_api_refuses_as_malformed_is_not_made_again_in_the_retry_rounds(self):
        """The API refuses o/a's repeat with HTTP 400, as for `invalid_prompt` under OpenAI's
        usage policy, and it refuses the same request the same way every time. The comparison
        sends the request once, as the labeling does. Sent again in every retry round, the
        request would pay each time for the conversation's earlier requests. The cause stays
        undetermined with the error."""
        self.real_ceiling()
        self.first_and_second(_by_readme)
        response = SimpleNamespace(request=SimpleNamespace(), status_code=400, headers={})
        refused = openai.BadRequestError("Invalid prompt: flagged", response=response,
                                         body={"code": "invalid_prompt", "message": "flagged"})

        def script(request, number):
            raise refused
        model = _Model(script)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"]), ("o/a", U))
        record = next(r for r in self.recorded_changes() if r["name"] == "o/a")
        self.assertEqual(record["repeat"], {"unavailable": "ModelFailure: the API refused the "
                                                           "request (Invalid prompt: flagged)"})
        self.assertNotIn("Retrying it later", err)
        self.assertNotIn("Retry round", err)

    def test_more_failed_repeats_than_the_ceiling_keep_the_comparison_for_the_next_start(self):
        """An outage of GitHub or of the model longer than the retry rounds fails every repeat.
        A comparison written after such an outage would leave every cause undetermined for good.
        When the failed repeats exceed the classification's ceiling for as many repositories, the
        comparison exits 1 with its partial file kept, and the next invocation repeats the
        labels."""
        self.first_and_second(_by_readme)
        self.with_ceiling(lambda repositories: 0)
        model = _Model(lambda request, number: [])
        code, err = self.compare(model)
        self.assertEqual(code, 1)
        self.assertIn("1 of 1 repositories have no repeated label after the retry rounds, more "
                      "than the ceiling of 0 for 2 repositories", err)
        run = self.index()[1]["run_id"]
        self.assertEqual(self.index()[1]["compared_with"], "")
        self.assertTrue(label_cache.changes_partial_path(self.cache, run).is_file())
        code, err = self.compare(_Model(_by_readme))
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"]), ("o/a", I))
        self.assertEqual(self.index()[1]["compared_with"], self.index()[0]["run_id"])

    def test_a_label_that_changed_across_a_run_without_a_decision_is_shown(self):
        """A repository labeled true, then left without a decision, then labeled false changed
        its label, although no two consecutive runs both decided it. Each run is compared with
        the latest earlier run that decided the repository."""
        self.real_ceiling()
        third = "d" * 40
        code, err = self.run_over(_repositories("o/a", "o/b"), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")

        def fails_for_a(request, number):
            return [] if _repository_of(request) == "o/a" else _by_readme(request, number)
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(fails_for_a), retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        code, err = self.run_over(_repositories("o/a", "o/b", revision=third), self.github,
                                  _Model(_by_readme), retrieval="2026-12-05T101430Z")
        self.assertEqual(code, 0, err)
        first = self.index()[0]["run_id"]
        code, err = self.compare(_Model(_by_readme))
        self.assertEqual(code, 0, err)
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["previous"], row["now"],
                          row["previous_run"]), ("o/a", I, "true", "false", first))
        self.assertIn("0 have no decision in one of the two runs", err)

    def test_a_retired_model_of_an_earlier_run_leaves_its_causes_undetermined(self):
        """A comparison that an invocation left unfinished repeats labels with the compared
        run's models. Once the API no longer offers the run's classifying model or summary model,
        a comparison that stopped as on a spent quota would fail every Start. The labels that the
        comparison repeated stand. The other causes are undetermined with the reason, and the
        next comparison is made."""
        retired = SimpleNamespace(request=SimpleNamespace(), status_code=404, headers={})

        def classifying(request, number):
            if request["model"] == "gpt-6-old":
                raise openai.NotFoundError("The model `gpt-6-old` does not exist",
                                           response=retired,
                                           body={"code": "model_not_found", "message": "gone"})
            return _by_readme(request, number)

        def reading(request, number):
            if request["model"] == "gpt-6-old" and not request.get("previous_response_id"):
                return [_call("read_linked_page", {"url": "https://tool.example/docs"})]
            return _by_readme(request, number)

        class SummaryRetired(_Bridge):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                return (f"Error executing tool {name}: [summary_refused] the API does not offer "
                        f"the model: gone", True)
        for retired_model, script, bridge in (("classifying", classifying, _Bridge()),
                                              ("summary", reading, SummaryRetired())):
            with self.subTest(retired=retired_model):
                self.setUp()
                self.github.readmes["o/b"] = (ENGLISH, "s2")
                self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
                self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
                self.first_and_second(_by_readme, model="gpt-6-old")
                code, err = self.compare(_Model(_refusing("o/b")), workers=1)
                self.assertEqual(code, 1, err)
                code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER),
                                          self.github, _Model(_by_readme))
                self.assertEqual(code, 0, err)
                model = _Model(script)
                pool = TokenPool([Credential("github", "a", "t")])
                client = readmes.Client(pool, session=self.github, sleep=lambda s: None)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    code = changed_labels.classify.compare_with_previous_runs(
                        self.cache, self.out, 1, None,
                        compare=lambda arguments: changed_labels.compare(
                            arguments, connect=lambda a, endpoint=None: (client, model),
                            bridge_for=lambda s: bridge))
                self.assertEqual(code, 0, err.getvalue())
                first, second, _ = [row["run_id"] for row in self.index()]
                self.assertEqual([row["compared_with"] for row in self.index()],
                                 ["", first, second])
                causes = {(row["previous_run"], row["repo_name"]): row["cause"]
                          for row in self.changes()}
                self.assertEqual((causes[(first, "o/a")], causes[(first, "o/b")]), (I, U))
                changed = list(label_cache.read_changes(self.cache, second,
                                                        self.index()[1]["changes_sha256"]))
                record = next(r for r in changed if r["name"] == "o/b")
                self.assertIn("the API does not offer the model", record["repeat"]["unavailable"])

    def test_a_stopped_comparison_resumes_and_repeats_only_the_rest(self):
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        self.first_and_second(_by_readme)
        model = _Model(_refusing("o/b"))
        code, err = self.compare(model)
        self.assertEqual(code, 1, err)
        self.assertIn("repositories have no repeated label", err)
        self.assertTrue(label_cache.changes_partial_path(self.cache,
                                                         self.index()[1]["run_id"]).is_file())
        self.assertEqual(self.index()[1]["compared_with"], "")
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual({row["repo_name"]: row["cause"] for row in self.changes()},
                         {"o/a": I, "o/b": I})
        self.assertEqual({_repository_of(r) for r in model.requests}, {"o/b"})
        self.assertFalse(label_cache.changes_partial_path(self.cache,
                                                          self.index()[1]["run_id"]).exists())

    def test_a_comparison_stopped_before_its_index_row_has_written_label_changes_csv(self):
        """The index records a comparison only once label_changes.csv contains it. A stop
        between the two writes leaves a run that the index does not name as compared. The next
        invocation compares that run again from the labels that the stopped invocation saved,
        without a request. A comparison recorded in the index first would count as complete, and
        the next Start would begin a new run with a comparison that replaces the CSV."""
        self.first_and_second(_by_readme)
        put = label_cache.put_index_row

        def stopped_at_the_record(row, cache_dir):
            if row.get("compared_with"):
                raise KeyboardInterrupt
            return put(row, cache_dir)

        with mock.patch.object(label_cache, "put_index_row", stopped_at_the_record), \
                self.assertRaises(KeyboardInterrupt):
            self.compare(_Model(_by_readme))
        self.assertEqual(self.index()[1]["compared_with"], "")
        self.assertEqual([row["repo_name"] for row in self.changes()], ["o/a"])
        written = (self.out / "label_changes.csv").read_bytes()
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(model.requests, [])
        self.assertEqual(self.index()[1]["compared_with"], self.index()[0]["run_id"])
        self.assertEqual((self.out / "label_changes.csv").read_bytes(), written)

    def test_a_comparison_stopped_between_the_two_labels_keeps_the_first(self):
        """After the current README's label was repeated, the API refuses the request for the
        previous README's label, and the refusal stops the comparison. The next invocation takes
        the current README's repeated label from the partial file and sends only the previous
        README. A second label of the current README would cost a further request and could
        differ from the first."""
        self.first_and_second(_by_readme)
        refused = SimpleNamespace(request=SimpleNamespace(), status_code=401, headers={})

        def refusing_the_previous_readme(request, number):
            text = request["input"] if isinstance(request["input"], str) else ""
            if "Now with tests" not in text:
                raise openai.AuthenticationError("Incorrect API key provided", response=refused,
                                                 body=None)
            return _by_readme(request, number)
        stopped = _Model(refusing_the_previous_readme)
        code, err = self.compare(stopped)
        self.assertEqual((code, len(stopped.requests)), (1, 2), err)
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 1)
        self.assertNotIn("Now with tests", model.requests[0]["input"])
        [row] = self.changes()
        self.assertEqual((row["repo_name"], row["cause"], row["repeat_label"],
                          row["previous_input_label"]), ("o/a", I, "false", "true"))
        # The request of the stopped invocation counts among the run's requests.
        self.assertEqual(int(self.index()[1]["model_requests"]), 1 + 1 + 1)

    def test_a_stopped_comparison_keeps_what_it_wrote_whole(self):
        """A reader splits the partial file at "\\n" alone. A repeated label with U+2028 in its
        justification therefore stays whole, and a line written after a line that a stop cut
        short starts a line of its own. The next invocation labels neither repository again."""
        names = ("o/a", "o/b", "o/c")
        for name, sha in (("o/b", "s2"), ("o/c", "s4")):
            self.github.readmes[name] = (ENGLISH, sha)
            self.github.by_revision[(name, REVISION)] = (ENGLISH, sha)
            self.github.by_revision[(name, LATER)] = (CHANGED, sha + "-later")
        code, err = self.run_over(_repositories(*names), self.github, _Model(_by_readme))
        self.assertEqual(code, 0, err)
        for name in names:
            self.github.readmes[name] = self.github.by_revision[(name, LATER)]
        code, err = self.run_over(_repositories(*names, revision=LATER), self.github,
                                  _Model(_by_readme))
        self.assertEqual(code, 0, err)

        refused = SimpleNamespace(request=SimpleNamespace(), status_code=401, headers={})

        def answering(*names):
            # The API refuses the other repositories' requests, which stops the comparison.
            def script(request, number):
                if _repository_of(request) not in names:
                    raise openai.AuthenticationError("Incorrect API key provided",
                                                     response=refused, body=None)
                text = request["input"] if isinstance(request["input"], str) else ""
                return _answer(_repository_of(request),
                               "false" if "Now with tests" in text else "true",
                               justification="It is software. It has tests.")
            return script
        code, err = self.compare(_Model(answering("o/a")))
        self.assertEqual(code, 1, err)
        partial = label_cache.changes_partial_path(self.cache, self.index()[1]["run_id"])
        with open(partial, "a", encoding="utf-8") as f:
            f.write('{"id":3,"status":"compared","change":{"id":3,"na')
        code, err = self.compare(_Model(answering("o/b")))
        self.assertEqual(code, 1, err)
        model = _Model(answering(*names))
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertEqual({_repository_of(r) for r in model.requests}, {"o/c"})
        self.assertEqual(len(self.changes()), 3)

    def test_a_killed_comparisons_requests_count_when_it_is_finished(self):
        """Each line of the partial file records the requests and tokens spent. The invocation
        that finishes the comparison adds the spending of every line to the run's index row,
        including the requests and tokens of an invocation that a kill ended."""
        class Killed(KeyboardInterrupt):
            pass

        def killed_at_b(request, number):
            if _repository_of(request) == "o/b":
                raise Killed()
            return _by_readme(request, number)
        for interrupted in (True, False):
            with self.subTest(interrupted=interrupted):
                self.setUp()
                self.github.readmes["o/b"] = (ENGLISH, "s2")
                self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
                self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
                self.first_and_second(_by_readme)
                before = int(self.index()[1]["model_requests"])
                killed = _Model(killed_at_b)
                with contextlib.ExitStack() as stack:
                    if not interrupted:
                        stack.enter_context(mock.patch.object(
                            changed_labels.Comparison, "record_spent", lambda comparison: None))
                    with self.assertRaises(Killed):
                        self.compare(killed, workers=1)
                self.assertEqual(int(self.index()[1]["model_requests"]), before)
                model = _Model(_by_readme)
                code, err = self.compare(model, workers=1)
                self.assertEqual(code, 0, err)
                # The request that the kill interrupted got no response and was not counted.
                self.assertEqual(int(self.index()[1]["model_requests"]),
                                 before + len(killed.requests) - 1 + len(model.requests))

    def test_a_rejected_key_stops_the_comparison(self):
        """Every repeated label fails the same way under a key that the API rejects."""
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        self.first_and_second(_by_readme)
        response = SimpleNamespace(request=SimpleNamespace(), status_code=401, headers={})

        def rejected(request, number):
            raise openai.AuthenticationError("Incorrect API key provided", response=response,
                                             body=None)
        model = _Model(rejected)
        code, err = self.compare(model, workers=1)
        self.assertEqual(code, 1)
        self.assertEqual(len(model.requests), 1)
        self.assertIn("ERROR repository_labeling: Stopped the comparison, because the API rejects "
                      "the OpenAI API key", err)

    def test_classification_parameters_this_code_would_not_send_leave_the_causes_undetermined(
            self):
        """A new image that changed a tool description or the summary prompt while a
        comparison was unfinished cannot repeat the compared run's labels. A comparison that
        refused to continue would fail every invocation, and the stages after the classification
        would never run. The labels that an earlier invocation repeated under the run's
        parameters stand."""
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        self.first_and_second(_by_readme)
        code, err = self.compare(_Model(_refusing("o/b")), workers=1)
        self.assertEqual(code, 1, err)
        model = _Model(_by_readme)
        with mock.patch.object(changed_labels.classify, "MAX_TOOL_ROUNDS", 7):
            code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertIn("The classification parameters of the current code differ from the "
                      "parameters that run", err)
        self.assertEqual(model.requests, [])
        self.assertEqual({row["repo_name"]: (row["cause"], row["repeat_label"])
                          for row in self.changes()},
                         {"o/a": (I, "false"), "o/b": (U, "")})
        record = next(r for r in self.recorded_changes() if r["name"] == "o/b")
        self.assertIn("The classification parameters of the current code differ",
                      record["repeat"]["unavailable"])
        self.assertEqual(self.index()[1]["compared_with"], self.index()[0]["run_id"])
        self.assertFalse(label_cache.changes_partial_path(self.cache,
                                                          self.index()[1]["run_id"]).exists())

    def test_a_comparison_that_failed_before_its_first_repeat_is_made_and_shown(self):
        """A comparison that fails before its first repeated label, here because no OpenAI API
        key is set, leaves a partial file. The next run over the sampling frame therefore also
        makes the comparison of the run before it, and label_changes.csv shows that comparison's
        changes."""
        self.first_and_second(_by_readme)
        first, second = [row["run_id"] for row in self.index()]

        def no_key(arguments, endpoint=None):
            raise changed_labels.NoCredentials("no OpenAI API key")
        args = argparse.Namespace(run=None, previous=None, cache_dir=self.cache,
                                  output_dir=self.out, workers=2, credentials=None)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(changed_labels.compare(args, connect=no_key,
                                                    bridge_for=lambda s: _Bridge()), 1)
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(_by_readme), retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        third = self.index()[2]["run_id"]
        model = _Model(_by_readme)
        pool = TokenPool([Credential("github", "a", "t")])
        client = readmes.Client(pool, session=self.github, sleep=lambda s: None)
        with contextlib.redirect_stderr(io.StringIO()):
            code = changed_labels.classify.compare_with_previous_runs(
                self.cache, self.out, 2, None, compare=lambda arguments: changed_labels.compare(
                    arguments, connect=lambda a, endpoint=None: (client, model),
                    bridge_for=lambda s: _Bridge()))
        self.assertEqual(code, 0)
        self.assertEqual([row["compared_with"] for row in self.index()], ["", first, second])
        self.assertEqual([(row["repo_name"], row["previous_run"]) for row in self.changes()],
                         [("o/a", first)])
        self.assertEqual(self.index()[2]["run_id"], third)

    def test_a_resumed_comparison_compares_with_one_previous_run(self):
        """The comparison with the default previous run begins a stopped comparison with an
        earlier previous run again rather than resuming it, and label_changes.csv contains one
        comparison."""
        self.github.readmes["o/b"] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", REVISION)] = (ENGLISH, "s2")
        self.github.by_revision[("o/b", LATER)] = (CHANGED, "s5")
        for _ in range(2):
            code, err = self.run_over(_repositories("o/a", "o/b"), self.github,
                                      _Model(_by_readme))
            self.assertEqual(code, 0, err)
        self.github.readmes["o/a"] = (CHANGED, "s3")
        self.github.readmes["o/b"] = (CHANGED, "s5")
        code, err = self.run_over(_repositories("o/a", "o/b", revision=LATER), self.github,
                                  _Model(_by_readme), retrieval="2026-11-05T101430Z")
        self.assertEqual(code, 0, err)
        earliest, previous, run = [row["run_id"] for row in self.index()]
        code, err = self.compare(_Model(_refusing("o/b")), run=run, previous=earliest)
        self.assertEqual(code, 1)
        model = _Model(_by_readme)
        code, err = self.compare(model)
        self.assertEqual(code, 0, err)
        self.assertIn(f"not with {previous}. Starting the comparison again", err)
        self.assertEqual({row["previous_run"] for row in self.changes()}, {previous})
        self.assertEqual({_repository_of(r) for r in model.requests}, {"o/a", "o/b"})
        self.assertEqual(self.index()[2]["compared_with"], previous)

    def test_a_previous_run_that_did_not_start_before_the_run_is_refused(self):
        """A run compared with itself would show no change, and a run compared with a later run
        would show every change backwards. Neither comparison is written as the run's
        comparison."""
        self.first_and_second(_by_readme)
        first, second = [row["run_id"] for row in self.index()]
        for run, previous in ((second, second), (first, second)):
            with self.subTest(run=run, previous=previous):
                model = _Model(_by_readme)
                code, err = self.compare(model, run=run, previous=previous)
                self.assertEqual(code, 1)
                self.assertIn(f"Run {previous} did not start before run {run}", err)
                self.assertEqual(model.requests, [])
                self.assertEqual([row["compared_with"] for row in self.index()], ["", ""])

    def test_a_trial_run_is_never_the_previous_run(self):
        self.first_and_second(_by_readme)
        first = self.index()[0]["run_id"]
        code, err = self.run_over(_repositories("o/a"), self.github, _Model(_always("unclear")),
                                  limit=1, output_dir=self.tmp / "trial", model="gpt-6.1-sol")
        self.assertEqual(code, 0, err)
        code, err = self.compare(_Model(_by_readme))
        self.assertEqual(code, 0, err)
        self.assertEqual({row["previous_run"] for row in self.changes()}, {first})
        self.assertEqual(self.index()[2]["scope"], "limit")
        self.assertEqual(self.index()[2]["compared_with"], "")

    def test_every_line_is_in_the_log_format_under_the_step_key(self):
        self.first_and_second(_by_readme)
        code, err = self.compare(_Model(_by_readme))
        self.assertEqual(code, 0, err)
        lines = err.strip().splitlines()
        self.assertTrue(lines)
        for line in lines:
            self.assertRegex(line, r"^(INFO|WARNING|ERROR) repository_labeling: ")
        self.assertIn("1 input change", err)

