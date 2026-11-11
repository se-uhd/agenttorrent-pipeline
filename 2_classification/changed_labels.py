#!/usr/bin/env python3
"""The labels that changed since the previous run over a sampling frame, and why: the last step of
a run of 1_classify_repos.py over the whole sampling frame, which calls `compare`. Run on its own,
the script compares any finished run with any earlier run.

A label is reused while the README, the model and the classification parameters are unchanged.
A label therefore changes only where one of them changed, or where a README that the run read
again kept its blob SHA. Because the model's response varies from one request to the next, a
changed README alone does not establish that the README changed the label. For each repository
that the two runs labeled differently, the classifier of the current run labels the current
README once more: at the higher temperature when the classifying model made the current label at
its higher temperature, with the second model when the second model made it, each with the
settings that the label records, and with no other settings or model when a repeat fails. When
the README or the revision changed and the repeat
kept the current label, the classifier also labels the previous README at the previous revision
once. Each input then has two labels, from which the cause follows:

- model variation: one classifier gave one input two different labels;
- input change: each input kept its label under the current classifier, and the inputs differ;
- classifier change: the model or the classification parameters changed, and the current
  classifier gives the previous input the current label, or the input did not change;
- undetermined: a README can no longer be read, or the labels fit none of these.

Because a linked page is read when the model requests it, the comparison cannot distinguish a
page that changed between the runs from an unchanged page. A change between a label and an
exclusion, or between two exclusions, follows from the README or from the language check, which
are deterministic. Such a change is an input change when the README changed and a classifier
change when only the language check or the classification parameters changed. A label that
recorded no description counts as a label with a changed description, and its input cannot be
repeated.

Inputs:  labels/ (the run, the previous run over a sampling frame and their decisions), GitHub
         access tokens and the API key of the classifying model's service, an OpenRouter API key
         for the default model, as for 1_classify_repos.py
Outputs: label_changes.csv, labels/<id>.changes-NNN.jsonl.xz, the run's row in labels/index.csv,
         and the tokens of the repeats in labels/<id>.tokens.csv with the run's cost record,
         labels/<id>.cost.csv (costs.py)

A repeat that GitHub or the model fails is tried again in the retry rounds of the classification. A
repeat that still fails leaves the cause undetermined, with the error, as long as such repeats do
not exceed the classification's ceiling for as many repositories as the run covered. The cause is
also undetermined for a run with classification parameters that the current code would not send, or
with a model that the API no longer offers, because its labels cannot be repeated. In an invocation
of the classification in which the second model labels nothing, as while its endpoint is unserved,
the comparison does not repeat the labels that the second model made, and their causes are
undetermined too, with the reason. Once GitHub has failed the repeats of two repositories since its
last response, every worker pauses as in the labeling, and a repeat that GitHub failed is made again
after the pause (`GitHubPause` of 1_classify_repos.py). A repeat that GitHub failed earlier, in this
invocation or in an earlier one, fails again on its own and begins no pause, unless GitHub also
fails a request that depends on no repository, as in an outage. The repeats of the classifying
model go where its labels went: for an OpenRouter model to the provider that the run's
parameters name, or for parameters recorded since 2026-11-04, which name none, to the pinned
provider of the same weights, and to its backup provider where a failure of the pinned provider
would use an attempt, as in the labeling (`1_classify_repos.Classifier.again`). For an OpenAI
model they go to the run's service tier, flex by default as for the labeling.

A comparison stops when the API refuses every request, when its service or GitHub fails through
every pause, or when GitHub refuses every GitHub access token. The next invocation resumes a
comparison that stopped or that left more failed repeats than the ceiling, and it takes the
model's responses that wait for GitHub's facts from the partial file. A repository that the
previous run left without a decision is compared with the latest earlier run that decided it.

Exit codes: 0 when the comparison is complete or there is no earlier run to compare with, and 1
when it could not be made, stopped or left more failed repeats than the ceiling. The next run
over the sampling frame, or a second invocation, finishes such a comparison.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import costs  # noqa: E402
import label_cache  # noqa: E402
import readmes  # noqa: E402
from label_cache import EXCLUDED, GONE, LABELED, UNDECIDED, CacheError  # noqa: E402

from agentcontext import CLASSIFICATION_DIR, credentials, logs  # noqa: E402
from agentcontext.github_api import NoTokens, TokenPool  # noqa: E402

classify = importlib.import_module("1_classify_repos")

# The comparison is a step of the classification stage, so it logs under the step's key.
COMPONENT = classify.COMPONENT
MODEL_VARIATION, INPUT_CHANGE = "model_variation", "input_change"
CLASSIFIER_CHANGE, UNDETERMINED = "classifier_change", "undetermined"
CAUSES = (MODEL_VARIATION, INPUT_CHANGE, CLASSIFIER_CHANGE, UNDETERMINED)
COMPARED, FAILED, REPEATED = "compared", "failed", "repeated"
CHANGE_COLUMNS = ["repo_name", "cause", "previous", "now", "changed", "repeat_label",
                  "previous_input_label", "previous_run", "justification",
                  "previous_justification"]
DECISION_FIELDS = ("outcome", "label", "reason", "readme_path", "readme_sha", "revision",
                   "model", "higher_temperature", "second_model", "parameters", "justification",
                   "fork_thresholds", "activity", "description", "is_mirror")
# The field of a row of an earlier run's labels that names that run, for a repository that the
# previous run left without a decision (`pair`).
COMPARED_RUN = "compared_run"



def outcome(row: dict) -> str:
    """A repository's outcome in a run's labels: its label, its exclusion, undecided or gone."""
    if row["outcome"] == LABELED:
        return row["label"]
    if row["outcome"] == EXCLUDED:
        return f"excluded: {row['reason']}"
    if row["outcome"] == UNDECIDED:
        return UNDECIDED
    return GONE


def labeled_by(decision: dict) -> str:
    """The model that made a label: the second model of a decision that it made
    (`1_classify_repos.Run.second_model_record`), the run's classifying model at the temperature
    of a decision that it made at its higher temperature (`1_classify_repos.Classifier.label`),
    else the run's classifying model."""
    second = decision.get("second_model") or {}
    if second.get("model"):
        return second["model"]
    higher = decision.get("higher_temperature") or {}
    model = decision.get("model", "")
    return f"{model} at temperature {higher['temperature']}" if "temperature" in higher \
        else model


def fallback_key(decision: dict) -> str:
    """The settings that made a label of the higher temperature or of the second model, as one
    text, or "" for a label of the classifying model at its own settings
    (`1_classify_repos.fallback_settings`)."""
    for key in ("second_model", "higher_temperature"):
        if decision.get(key):
            return json.dumps({key: classify.fallback_settings(decision[key])}, sort_keys=True)
    return ""


def changed_inputs(previous: dict, current: dict, shared=None, keys=None) -> list[str]:
    """What differs between two decisions: the README, the revision, the description and the
    model that made the label (between two labels, `labeled_by`), the classification parameters
    and the language check.

    The classification parameters name the classifying model with its weights, its quantization
    and the settings of its requests. `keys(parameters)` gives the key of a digest
    (`label_cache.ParameterKeys`), and two digests with the same key, such as the digests of
    parameters that differ only in the provider of a model, are the same parameters.
    `shared(parameters)` gives the digest of parameters without the classifying model
    (`label_cache.shared_parameters`), and with it a change of the model, its weights, its
    quantization or its settings counts as a changed model, and only a change of the rest as
    changed parameters."""
    names = []
    if previous.get("readme_sha", "") != current.get("readme_sha", ""):
        names.append("readme")
    if previous.get("revision", "") != current.get("revision", ""):
        names.append("revision")
    both_labels = previous.get("outcome") == current.get("outcome") == LABELED
    # A label that recorded no description was made over an unknown input, which does not count
    # as unchanged.
    if both_labels and ("description" not in previous or "description" not in current
                        or not classify.same_text(previous["description"],
                                                  current["description"])):
        names.append("description")
    parameters = (previous.get("parameters"), current.get("parameters"))
    keyed = parameters
    if keys is not None and all(parameters):
        keyed = (keys(parameters[0]), keys(parameters[1]))
    rest_differs = keyed[0] != keyed[1]
    # `shared` reads the recorded parameters of a digest, which the cache holds under the digest
    # and not under its key, and it leaves out the provider itself.
    if rest_differs and shared is not None and all(parameters):
        rest_differs = shared(parameters[0]) != shared(parameters[1])
    if both_labels and (labeled_by(previous) != labeled_by(current)
                        or (keyed[0] != keyed[1] and not rest_differs)):
        names.append("model")
    if rest_differs:
        names.append("parameters")
    if previous.get("language_check") != current.get("language_check"):
        names.append("language_check")
    return names


def label_cause(previous_label: str, label: str, repeat: str, previous_input: str | None,
                classifier_changed: bool) -> str:
    """Why a label changed. `repeat` is the current input's second label. `previous_input` is
    the previous input's label under the current classifier, None when the input did not
    change, or "" when the previous README could not be read."""
    if repeat != label:
        return MODEL_VARIATION
    if previous_input is None:
        # The input is unchanged and got the same label twice now. Under an unchanged classifier
        # it had another label before, which gives one input two labels.
        return CLASSIFIER_CHANGE if classifier_changed else MODEL_VARIATION
    if previous_input == "":
        return UNDETERMINED
    if not classifier_changed:
        return INPUT_CHANGE if previous_input == previous_label else MODEL_VARIATION
    if previous_input == label:
        return CLASSIFIER_CHANGE
    if previous_input == previous_label:
        return INPUT_CHANGE
    return UNDETERMINED


def outcome_cause(changed: list[str]) -> str:
    """Why an outcome that involves an exclusion changed: the README, else the classification
    parameters or the language check."""
    if "readme" in changed:
        return INPUT_CHANGE
    if "parameters" in changed or "language_check" in changed:
        return CLASSIFIER_CHANGE
    return UNDETERMINED


def carried_cause(previous: dict, current: dict) -> str | None:
    """Why the label of a decision carried over from `previous` changed: GitHub's facts about a
    mirror or a fork, which are input, or else the rules and checks of the classification. No
    repeat can determine this cause, because the model's response is the same. None when
    `current` was not carried over from `previous`, directly or through decisions that were in
    turn carried over from it, such as by a run that a kill ended."""
    carried = current.get("carried_from") or {}
    if not carried or previous.get("decided_at") not in (
            carried.get("decided_at"), *carried.get("earlier_decided_at", [])):
        return None
    before, now = previous.get("fork") or {}, current.get("fork") or {}
    if before and now and before.get("heuristic") != now.get("heuristic"):
        # The fork heuristic changed, and the classification queried GitHub again. The facts and
        # the verdict changed with the code, and also with the thresholds of a new sampling frame.
        if previous.get("fork_thresholds") != current.get("fork_thresholds"):
            return UNDETERMINED
        return CLASSIFIER_CHANGE
    if any(previous.get(fact) != current.get(fact) for fact in ("fork", "mirror_merges")):
        return INPUT_CHANGE
    return CLASSIFIER_CHANGE


def pair(previous_rows: list[dict], current_rows: list[dict],
         decided_before=None) -> tuple[list, dict]:
    """The repositories with a changed outcome, as (previous row, current row), and the counts of
    the rest: unchanged, new to the sampling frame, left the sampling frame, without a decision in
    either run, gone and back from gone. A repository without a decision has no decision to
    compare.

    A repository that the previous run left without a decision is compared with its outcome in
    the latest earlier run that decided it, which `decided_before(id)` returns, or None. Compared
    with the previous run alone, a label that changed across a run that left the repository
    without a decision would never be shown."""
    before = {int(row["id"]): row for row in previous_rows}
    counts = {"unchanged": 0, "new": 0, "left": 0, "undecided": 0, "gone": 0, "back": 0}
    changes = []
    for row in current_rows:
        old = before.pop(int(row["id"]), None)
        if (old is not None and decided_before is not None and old["outcome"] == UNDECIDED
                and row["outcome"] != UNDECIDED):
            old = decided_before(int(row["id"])) or old
        if old is None:
            counts["new"] += 1
        elif UNDECIDED in (old["outcome"], row["outcome"]):
            counts["undecided"] += 1
        elif outcome(old) == outcome(row):
            counts["unchanged"] += 1
        elif row["outcome"] == GONE:
            counts["gone"] += 1
        elif old["outcome"] == GONE:
            counts["back"] += 1
        else:
            changes.append((old, row))
    counts["left"] = len(before)
    return changes, counts


def decided_before(cache_dir: Path, rows: dict, previous_id: str):
    """The function that `pair` calls for a repository that run `previous_id` left without a
    decision: its row in the labels of the latest finished run over a sampling frame before
    `previous_id` that decided it, naming that run as COMPARED_RUN, or None. `rows` are the
    index rows by run id, and each run's labels are read once, when first needed."""
    earlier = sorted((row for row in rows.values()
                      if row["run_id"] < previous_id and row["outcome"] == "finished"
                      and row["scope"] == label_cache.SAMPLING_FRAME_SCOPE
                      and row["labels_sha256"]),
                     key=lambda row: row["run_id"], reverse=True)
    read: dict = {}

    def find(repository_id: int) -> dict | None:
        for row in earlier:
            if row["run_id"] not in read:
                read[row["run_id"]] = {int(r["id"]): r for r in label_cache.read_labels(
                    cache_dir, row["run_id"], row["labels_sha256"])}
            found = read[row["run_id"]].get(repository_id)
            if found is not None and found["outcome"] != UNDECIDED:
                return {**found, COMPARED_RUN: row["run_id"]}
        return None
    return find


def summary_of(decision: dict) -> dict:
    """The fields of a decision that a change record keeps. The description is None where the
    decision recorded none, which differs from an empty description."""
    summary = {field: decision.get(field, "") for field in DECISION_FIELDS}
    summary["description"] = decision.get("description")
    return summary


def change_record(rid: str, previous_rid: str, old: dict, row: dict, decisions: dict,
                  shared=None, keys=None) -> dict:
    """The record of one changed outcome, before any repeated label. `old` names the earlier run
    that it comes from as COMPARED_RUN when that run is not the previous run. `shared` tells a
    changed model from changed parameters, and `keys` gives the key of a digest of parameters
    (`changed_inputs`)."""
    previous = decisions[(old["decided_in"], int(old["id"]))]
    current = decisions[(row["decided_in"], int(row["id"]))]
    changed = changed_inputs(previous, current, shared, keys)
    record = {"id": int(row["id"]), "name": row["repo_name"], "run": rid,
              "previous_run": old.get(COMPARED_RUN) or previous_rid,
              "decided_in": {"previous": old["decided_in"], "current": row["decided_in"]},
              "previous": summary_of(previous), "current": summary_of(current),
              "changed": changed}
    if not (old["outcome"] == row["outcome"] == LABELED):
        record["cause"] = outcome_cause(changed)
    elif carried_cause(previous, current):
        record["cause"] = carried_cause(previous, current)
    return record


async def readme_as_labeled(github, name: str, repository_id: int, decision: dict):
    """The README that a decision labeled, read again at its revision, or None with GitHub's
    response when the README cannot be read, because the repository is gone, or the revision is
    gone and GitHub returns the README of the default branch."""
    readme = await asyncio.to_thread(readmes.fetch, github, name, repository_id,
                                     decision["revision"])
    if readme.status == readmes.FOUND and readme.sha == decision["readme_sha"]:
        return readme, ""
    return None, (f"GitHub returns the README at {decision['revision']} as {readme.status} with "
                  f"blob {readme.sha or 'none'}, not the blob {decision['readme_sha']} that was "
                  f"labeled")


def shown_activity(decision: dict) -> dict:
    """The activity that a decision records, with only the fields that the current classifier
    shows (`ACTIVITY_FIELDS`). A decision made under other fields, such as the stars and the
    forks that an earlier classifier showed, would otherwise be repeated with an input that the
    current classifier never gives, and the cause of its change would depend on that input."""
    shown = {name for _field, name in classify.ACTIVITY_FIELDS}
    return {name: value for name, value in (decision["activity"] or {}).items() if name in shown}


async def repeat(record: dict, classifier, github, repeated: dict, keep=None,
                 keep_response=None, recorded_calls: dict | None = None) -> dict:
    """The record with the repeated labels and its cause: the current README labeled again, and
    the previous README, at the previous revision, when the input changed.

    `recorded_calls` holds, under "current" and "previous", the calls of the outline, the
    metadata and the issues and pull requests that each decision recorded for its first request
    (`recorded_results` of 1_classify_repos.py). Each repeat takes them as its decision recorded
    them and reads anew only those that the decision lacks, so that the input holds what the
    decision was made on rather than GitHub's current data.

    `repeated` keeps the current README's repeated label by GitHub id until the record is
    compared, and `keep(id, label)` is called with each such label as it is made. When the
    previous README's label fails, as when GitHub fails to return its facts, the retry round
    takes the current README's label from `repeated` instead of paying the model again, because a
    further request could give another label. `keep_response(id, key, response)` is called with the
    model's response for either README before GitHub's facts are requested for it, as
    `assess` of 1_classify_repos.py calls its `keep`."""
    response_keeper = None
    if keep_response is not None:
        def response_keeper(key, response):
            keep_response(record["id"], key, response)
    current, previous = record["current"], record["previous"]
    record = {**record, "repeat": None, "previous_input": None,
              "compared_at": label_cache.iso(label_cache.utc_now())}
    if not current.get("fork_thresholds"):
        return {**record, "repeat": {"unavailable": "the decision records no thresholds for "
                                                    "the fork heuristic"},
                "cause": UNDETERMINED}
    # The current decision's thresholds, so that the fork heuristic decides as it did in the run.
    thresholds = classify.forks.Thresholds(**current["fork_thresholds"])
    if current["description"] is None:
        return {**record, "repeat": {"unavailable": "the decision recorded no description"},
                "cause": UNDETERMINED}
    readme, missing = await readme_as_labeled(github, record["name"], record["id"], current)
    if readme is None:
        return {**record, "repeat": {"unavailable": missing}, "cause": UNDETERMINED}
    again = repeated.get(record["id"])
    if again is None:
        again = await classify.assess(classifier, github, readme, thresholds,
                                      shown_activity(current), current["description"],
                                      await asyncio.to_thread(classify.readmes.language,
                                                              readme.text),
                                      keep=response_keeper,
                                      recorded=(recorded_calls or {}).get("current"),
                                      is_mirror=bool(current.get("is_mirror")))
        repeated[record["id"]] = again
        if keep is not None:
            keep(record["id"], again)
    record["repeat"] = again
    if again["label"] != current["label"]:
        # One input has two labels. The previous input cannot change this cause and is not
        # labeled again.
        return {**record, "cause": MODEL_VARIATION}
    previous_input = None
    if {"readme", "revision", "description"} & set(record["changed"]):
        if previous["description"] is None:
            old, missing = None, "the previous decision recorded no description"
        else:
            old, missing = await readme_as_labeled(github, readme.name, record["id"], previous)
        if old is None:
            record["previous_input"] = {"unavailable": missing}
            previous_input = ""
        else:
            assessment = await classify.assess(classifier, github, old, thresholds,
                                               shown_activity(previous),
                                               previous["description"],
                                               await asyncio.to_thread(classify.readmes.language,
                                                                       old.text),
                                               keep=response_keeper,
                                               recorded=(recorded_calls or {}).get("previous"),
                                               is_mirror=bool(previous.get("is_mirror")))
            record["previous_input"] = assessment
            previous_input = assessment["label"]
    record["cause"] = label_cause(previous["label"], current["label"], again["label"],
                                  previous_input, bool({"model", "parameters"}
                                                       & set(record["changed"])))
    return record


def partial_lines(path: Path, statuses: tuple) -> Iterator[dict]:
    """The lines of a comparison's partial file with a status of `statuses` that name a
    repository, in order, read one line at a time without a last line that a stop cut short
    (`label_cache.partial_lines`)."""
    return (line for line in label_cache.partial_lines(path, statuses) if "id" in line)


def read_partial(path: Path) -> dict[int, dict]:
    """The comparisons that a stopped invocation finished, by GitHub id. A failed comparison is
    made again."""
    lines = {}
    for line in partial_lines(path, (COMPARED, FAILED)):
        if line["status"] == COMPARED:
            lines[int(line["id"])] = line["change"]
        else:
            lines.pop(int(line["id"]), None)
    return lines


def read_repeated(path: Path) -> dict[int, dict]:
    """The current README's repeated label of each repository that a stopped invocation labeled
    again and did not compare, by GitHub id (`repeat`). A failure of the previous README's
    label, or a stop before that label, leaves the repeated label for the next attempt. Without
    it, the next attempt would pay the model for the label again and could get another label."""
    repeated = {}
    for line in partial_lines(path, (REPEATED, COMPARED)):
        if line["status"] == REPEATED:
            repeated[int(line["id"])] = line["repeat"]
        else:
            repeated.pop(int(line["id"]), None)
    return repeated


class Progress(classify.Progress):
    """The progress line of the repeated labels, with the requests and tokens that `spent`
    returns."""

    def __init__(self, total: int, already_done: int, spent):
        logs.Progress.__init__(self, COMPONENT, total, already_done, classify.PROGRESS_EVERY)
        self.spent = spent


class Comparison:
    """The repeated labels of the changed repositories, written to the partial file as each is
    made, so that a stopped comparison resumes where it stopped."""

    def __init__(self, records: list, classifier, github, partial: Path, workers: int,
                 recorded_calls: dict | None = None, others: dict | None = None):
        """`recorded_calls` holds, by GitHub id, the calls of READ_FIRST that the two decisions
        of each record recorded (`repeat`). `others` holds, by `fallback_key`, the classifiers
        that repeat the labels of a record whose current label the classifying model made at its
        higher temperature or the second model made (`repeater`). The repeats measure one
        classifier each, and a repeat that fails is not made by another."""
        self.records, self.classifier, self.github = records, classifier, github
        self.others = others or {}
        self.partial, self.workers = partial, workers
        self.recorded_calls = recorded_calls or {}
        label_cache.end_partial(partial)
        self.done = read_partial(partial)
        self.failed: dict[int, str] = {}
        # The records with a repeat that the API refused as malformed. The API refuses such a
        # request the same way every time, and the set keeps every retry round of the invocation
        # from paying for the conversation's earlier requests again (`ModelFailure.final`).
        self.final: set[int] = set()
        # The current README's repeated label of each record not yet compared (`repeat`),
        # including the labels that an earlier invocation wrote to the partial file.
        self.repeated: dict[int, dict] = read_repeated(partial)
        # The model's responses for either README that wait for GitHub's facts and that an
        # earlier invocation wrote to the partial file. A resumed comparison then reads only the
        # facts. The classifiers share them, because the key of a response names its input,
        # which one classifier repeats.
        classifier.kept.update(classify.kept_responses(partial, (COMPARED,)))
        for other in self.others.values():
            other.kept = classifier.kept
        self.stopped = False
        # Why the run's classifier can repeat no further label, as when the API no longer offers
        # its model, or "".
        self.unrepeatable = ""
        self.progress = None
        self.spending = label_cache.Spending(lambda: classify.spent(github,
                                                                    *self.classifiers()))
        # The pause of every worker while GitHub fails, as in the labeling. The repeats that
        # GitHub failed in an earlier invocation can be all that a resumed comparison has left.
        # Their failures count toward the pause only when GitHub also fails the README request
        # that it served last.
        self.github_pause = classify.GitHubPause(lambda: github.responses, github.responds,
                                                 classifier.sleep, classifier.clock)
        names = {record["id"]: record["name"] for record in records}
        self.github_pause.failed_before |= {names[number] for number in failed_on_github(partial)
                                            if number in names}

    def classifiers(self) -> list:
        """The classifier of the run's model at its own settings, and those of `others`."""
        return [self.classifier, *self.others.values()]

    def repeater(self, record: dict):
        """The classifier that repeats the labels of `record`: the one of `others` with the
        settings of the higher temperature or of the second model that made the current label,
        else the run's model's at its own settings."""
        return self.others.get(fallback_key(record["current"]), self.classifier)

    def record_spent(self) -> None:
        """Record what the comparison spent since the last line, as the invocation ends."""
        self.write({})

    def write(self, line: dict) -> None:
        """Append `line` to the partial file with what the comparison spent since the last
        line."""
        spent = self.spending.since()
        line = {**line, "spent": spent} if spent else line
        if line:
            with open(self.partial, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")

    def keep(self, repository_id: int, label: dict) -> None:
        """Write the current README's repeated label to the partial file as soon as it is made,
        so that a stop before the previous README's label leaves it for the next invocation."""
        self.write({"id": repository_id, "status": REPEATED, "repeat": label})

    def keep_response(self, repository_id: int, key: str, response: dict) -> None:
        """Write the model's response for a README of the repository to the partial file while
        GitHub's facts are still to be read, so that a stop, a kill or a failure of GitHub that
        outlasts the retry rounds leaves the response for the next invocation."""
        self.write({"id": repository_id, "status": classify.KEPT, "key": key,
                    "response": response})

    async def work(self, queue: asyncio.Queue) -> None:
        while not self.stopped:
            try:
                record = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            if await self.take(record) and self.progress is not None:
                self.progress.note(record["id"] in self.failed)

    async def take(self, record: dict) -> bool:
        """Repeat the labels of `record` after the pause for GitHub, and again after each pause
        that follows a failure of GitHub in the repeat (`classify.GitHubPause`), unless another
        worker has stopped the comparison in the meantime. Return False when the comparison
        stopped."""
        waited = False
        while True:
            sent_after, failure = 0, None
            try:
                sent_after = await self.github_pause.ready()
                if waited and self.stopped:
                    return False
                compared = await repeat(record, self.repeater(record), self.github,
                                        self.repeated, self.keep, self.keep_response,
                                        self.recorded_calls.get(record["id"]))
                self.write({"id": record["id"], "status": COMPARED, "change": compared})
                self.done[record["id"]] = compared
                self.repeated.pop(record["id"], None)
            except classify.ModelNotOffered as error:
                # The API retired a model of the run's classifier after the run. No later
                # invocation can repeat the labels either, and a stop would make every later
                # invocation fail.
                self.unrepeatable = str(error)
                self.end_pauses(f"the comparison stopped, because {error}")
                self.stopped = True
                return False
            except (NoTokens, classify.ModelRefused, classify.ServiceDown,
                    classify.GitHubDown, classify.GitHubPauseEnded) as error:
                return self.stop(error)
            except classify.ModelFailure as error:
                self.fail(record, error, classify.MODEL_FAILED, retry=not error.final)
            except classify.GITHUB_ERRORS as error:
                failure = error
            except Exception:  # noqa: BLE001 - logged with its traceback, then stops
                logs.error(COMPONENT, f"An unexpected error stopped the comparison at "
                                      f"{record['name']}.\n"
                                      f"{traceback.format_exc().rstrip()}")
                self.failed[record["id"]] = "error"
                self.end_pauses(f"an unexpected error stopped the comparison at "
                                f"{record['name']}")
                self.stopped = True
            if failure is not None:
                try:
                    if not self.stopped and await self.github_pause.failed(
                            record["name"], sent_after, failure):
                        waited = True
                        continue
                except (NoTokens, classify.GitHubDown) as error:
                    return self.stop(error)
                self.fail(record, failure, classify.GITHUB_FAILED)
            return True

    def fail(self, record: dict, error: Exception, reason: str, retry: bool = True) -> None:
        """Record a repeat that GitHub or the model failed, as `reason` names. A retry round makes
        the repeat again when `retry` is true, and otherwise the next invocation does."""
        logs.warning(COMPONENT, f"Could not repeat the labels of {record['name']} ({error})"
                                f"{'. Retrying it later' if retry else ''}.")
        self.failed[record["id"]] = f"{type(error).__name__}: {error}"
        if not retry:
            self.final.add(record["id"])
        self.write({"id": record["id"], "status": FAILED, "reason": reason, "retry": retry,
                    "error": self.failed[record["id"]]})

    def end_pauses(self, reason: str) -> None:
        """End the pauses for GitHub, for the API's service and for spent credit of a comparison
        that stopped for `reason`, as `Run.end_pauses` does."""
        self.github_pause.end(reason)
        for classifier in self.classifiers():
            classifier.end_pauses(reason)

    def stop(self, error: Exception) -> bool:
        """Stop the comparison on `error`, after which no repeat can be made, and return False."""
        if not self.stopped:
            logs.error(COMPONENT, f"Stopped the comparison, because {error}. The comparisons so "
                                  f"far are saved in {self.partial.name}.")
            self.end_pauses(f"the comparison stopped, because {error}")
        self.stopped = True
        return False

    async def run(self) -> None:
        """Repeat the labels of every record without a line in the partial file, then make the
        failed repeats again in up to the classification's RETRY_ROUNDS, each after
        RETRY_ROUND_SECONDS, until none is left or the comparison stops. Without the retry
        rounds, a burst of failures of GitHub or the model would leave the causes of the affected
        repositories undetermined. A repeat that the API refused as malformed is not made again
        in the invocation."""
        pending = [r for r in self.records if r["id"] not in self.done]
        for round_number in range(classify.RETRY_ROUNDS + 1):
            if round_number:
                logs.info(COMPONENT, f"Retry round {round_number} of {classify.RETRY_ROUNDS} "
                                     f"starts in {classify.RETRY_ROUND_SECONDS} seconds for the "
                                     f"{len(pending):,} repositories without their repeated "
                                     f"labels.")
                await self.classifier.sleep(classify.RETRY_ROUND_SECONDS)
                for record in pending:
                    self.failed.pop(record["id"], None)
            queue: asyncio.Queue = asyncio.Queue()
            for record in pending:
                queue.put_nowait(record)
            self.progress = Progress(len(self.records), len(self.records) - len(pending),
                                     lambda: classify.spent(self.github, *self.classifiers()))
            await asyncio.gather(*(self.work(queue) for _ in range(self.workers)))
            pending = [r for r in self.records
                       if r["id"] in self.failed and r["id"] not in self.final]
            if self.stopped or not pending:
                return


def change_rows(changes: list[dict]) -> list[dict]:
    """label_changes.csv: one row per repository with a changed outcome."""
    rows = []
    for change in changes:
        previous, current = change["previous"], change["current"]
        rows.append({"repo_name": change["name"], "cause": change["cause"],
                     "previous": outcome(previous), "now": outcome(current),
                     "changed": " ".join(change["changed"]),
                     "repeat_label": (change.get("repeat") or {}).get("label", ""),
                     "previous_input_label": (change.get("previous_input") or {})
                     .get("label", ""),
                     "previous_run": change["previous_run"],
                     "justification": current.get("justification", ""),
                     "previous_justification": previous.get("justification", "")})
    return rows


def added(rid: str, cache_dir: Path, totals: dict) -> dict:
    """The comparison's requests and tokens, added to what the index records for run `rid`."""
    row = next((r for r in label_cache.read_index(cache_dir) if r["run_id"] == rid), {})
    return {key: int(row.get(key) or 0) + value for key, value in totals.items()}


def compared_in(partial: Path) -> str:
    """The previous run that the comparison of a partial file compares with, from the file's
    first line, or "" for a file without it."""
    try:
        with open(partial, encoding="utf-8") as f:
            first = json.loads(f.readline())
    except (OSError, ValueError):
        return ""
    return str(first.get("previous_run") or "") if isinstance(first, dict) else ""


def start_comparison(cache_dir: Path, rid: str, previous_id: str) -> None:
    """Begin the partial file of the comparison of run `rid` with `previous_id`, unless that file
    exists, before anything that can fail. A comparison that fails before its first repeated
    label then leaves a partial file, which the next run over the sampling frame takes up. The
    first line names the previous run. A partial file of a comparison with another previous run
    is replaced, and its requests and tokens are added to the run's row, because resuming that
    file would mix the repeated labels of two comparisons."""
    partial = label_cache.changes_partial_path(cache_dir, rid)
    if partial.is_file() and compared_in(partial) == previous_id:
        return
    if partial.is_file():
        logs.warning(COMPONENT, f"{partial.name} has the comparison of run {rid} with "
                                f"{compared_in(partial) or 'another run'}, not with "
                                f"{previous_id}. Starting the comparison again.")
        spent = label_cache.spent_in(partial, label_cache.SPENT_COLUMNS)
        if any(spent.values()):
            label_cache.put_index_row({"run_id": rid, **added(rid, cache_dir, spent)},
                                      cache_dir)
    partial.write_text(json.dumps({"previous_run": previous_id}) + "\n", encoding="utf-8")


def write_changes_csv(cache_dir: Path, output_dir: Path, run_ids: list,
                      unrecorded: dict | None = None) -> None:
    """label_changes.csv: the comparisons of the runs `run_ids`, in that order, from the cache
    of labels, each read with the checksums of its index row, or with the checksums in
    `unrecorded` for a run with a comparison that the index does not record yet. Each row names
    its previous run."""
    checksums = {row["run_id"]: row["changes_sha256"]
                 for row in label_cache.read_index(cache_dir)} | (unrecorded or {})
    changes = [change for rid in run_ids
               for change in label_cache.read_changes(cache_dir, rid, checksums[rid])]
    classify.write_csv(Path(output_dir) / "label_changes.csv", CHANGE_COLUMNS,
                       change_rows(changes))


def failed_on_github(partial: Path) -> set:
    """The GitHub ids of the repeats that GitHub failed in the invocations that wrote the
    comparison's partial file `partial`."""
    return {int(line["id"]) for line in partial_lines(partial, (FAILED,))
            if line.get("reason") == classify.GITHUB_FAILED}


class NoCredentials(Exception):
    """The repeated labels need GitHub and the model, and a credential for one of them is
    missing."""


class ParametersChanged(Exception):
    """The current code would send other classification parameters than the compared run
    recorded, as after a new image that changed a tool description or the summary prompt while
    the run's comparison was unfinished. If this case were refused, the comparison would fail in
    every invocation, and the stages after the classification would never run."""


def unrepeated(record: dict, error: str) -> dict:
    """A record with labels that could not be repeated, with its cause undetermined and the
    error."""
    return {**record, "repeat": {"unavailable": error}, "previous_input": None,
            "cause": UNDETERMINED, "compared_at": label_cache.iso(label_cache.utc_now())}


def called(found, served: str = ""):
    """The endpoint on which the comparison calls the model of `found`, a model that recorded
    classification parameters name (`endpoints.with_provider`): the provider that parameters
    recorded before 2026-11-04 name, else the pinned provider of the same weights and
    quantization, else `served`, the provider that served the run's own responses of the model
    (`served_providers`). An OpenRouter model without any of them keeps no provider, and
    `explain` cannot repeat its labels (`ParametersChanged`)."""
    try:
        return classify.endpoints.with_provider(found)
    except ValueError:
        return classify.dataclasses.replace(found, provider=served) if served else found


def served_providers(decisions) -> dict:
    """The provider that served most of the responses of the classifying model and most of the
    page summaries in `decisions`, by the role of the model (`1_classify_repos.CLASSIFYING_ROLE`,
    `SUMMARY_ROLE`), from the providers that each decision names under `served_by` and
    `summary_provider` (`1_classify_repos.Classifier.label_readme`), leaving out the backup
    providers and the labels of the second model. A role without such a provider is missing.
    Since 2026-11-04 the parameters of a run name no provider, and for an OpenRouter model
    without a pinned record, such as a candidate model of a check, the decisions name the
    endpoint that the run pinned."""
    counts = {classify.CLASSIFYING_ROLE: {}, classify.SUMMARY_ROLE: {}}

    def count(role: str, provider: str) -> None:
        counts[role][provider] = counts[role].get(provider, 0) + 1
    for decision in decisions:
        if decision.get("second_model"):
            continue
        for entry in decision.get("served_by") or []:
            if entry.get("provider") and not entry.get("backup"):
                count(classify.CLASSIFYING_ROLE, entry["provider"])
        for call in decision.get("tools") or []:
            if call.get("summary_provider") and not call.get("summary_backup"):
                count(classify.SUMMARY_ROLE, call["summary_provider"])
    return {role: max(sorted(found), key=found.get) for role, found in counts.items() if found}


def run_endpoints(recorded: dict, run: dict, served: dict | None = None) -> tuple:
    """The endpoints of the classifying model and the summary model of run `run`, with the
    settings of the classifying model's requests (`endpoints.Settings.of`), from `recorded`, its
    classification parameters, each with the provider on which the comparison calls it
    (`called`), where `served` gives by role the provider that served the run's own responses
    (`served_providers`). Parameters recorded before the endpoints were recorded name the
    classifying model only in the index and its reasoning effort at the top level of the
    parameters, and the current code cannot send them again (`ParametersChanged`). A setting
    that the parameters do not record is one that the requests did not send."""
    served = served or {}
    block = recorded.get("classifying_model") or {"model": run["model"],
                                                  "reasoning_effort":
                                                  recorded.get("reasoning_effort", "")}
    return (called(classify.endpoints.Endpoint.of(block),
                   served.get(classify.CLASSIFYING_ROLE, "")),
            classify.endpoints.Settings.of(block),
            called(classify.endpoints.Endpoint.of(recorded["summary"]),
                   served.get(classify.SUMMARY_ROLE, "")))


def fallback_classifier(decision: dict, classifying, settings, make):
    """The classifier that repeats a label of the higher temperature or of the second model,
    with the settings that `decision` records of it: the second model at the endpoint and with
    the settings of its record, or the run's classifying model at `classifying` with `settings`
    at the temperature, the top_p, the seed and the output limit of the record of the higher
    temperature. A record without an output limit was written while the attempts at the higher
    temperature sent the limit of the run's other requests, the one of `settings`.
    `make(endpoint, settings)` builds a classifier."""
    second = decision.get("second_model")
    if second:
        return make(called(classify.endpoints.Endpoint.of(second)),
                    classify.endpoints.Settings.of(second))
    higher = decision["higher_temperature"]
    return make(classifying, classify.dataclasses.replace(
        settings, **{name: higher.get(name) for name in ("temperature", "top_p", "seed")},
        max_output_tokens=higher.get("max_output_tokens", settings.max_output_tokens)))


def connect(args, endpoint=None) -> tuple:
    """The GitHub client and the client of the classifying model's service, OpenAI's or
    OpenRouter's, for the repeated labels, from the credentials. `endpoint` is the endpoint of the
    run's classifying model, the default model's endpoint by default. The GitHub access tokens
    that GitHub rejects at the start are checked again by the first request, as in the labeling
    (`readmes.Client`)."""
    endpoint = endpoint or classify.endpoints.endpoint(classify.DEFAULT_MODEL)
    configured = credentials.github_tokens(args.credentials, os.environ)
    if not configured:
        raise NoCredentials(f"repeating the labels requires a GitHub access token, from "
                            f"GITHUB_TOKENS, GITHUB_TOKEN or {args.credentials}")
    flex = getattr(args, "service_tier", None) == "flex" and not endpoint.openrouter
    try:
        client = classify.endpoints.client(endpoint, args.credentials,
                                           timeout=classify.FLEX_TIMEOUT if flex
                                           else classify.REQUEST_TIMEOUT,
                                           max_retries=classify.SDK_RETRIES)
    except classify.endpoints.NoKey as error:
        raise NoCredentials(f"repeating the labels requires the API key of "
                            f"{endpoint.model} ({error})") from error
    return (readmes.Client(TokenPool(readmes.accepted_tokens(configured)),
                           configured=configured), client)


async def explain(records: list, run: dict, cache_dir: Path, github, client, bridge_for,
                  workers: int, service_tier: str | None = None,
                  credentials_path: Path | None = None,
                  recorded_calls: dict | None = None,
                  second_client=None, backups=None, no_backup_provider: bool = False,
                  unserved_backups: dict | None = None,
                  served: dict | None = None) -> tuple[Comparison, dict]:
    """The repeated labels, made by the classifier of run `run`, that is, its model and the
    classification parameters that it recorded, which this code must reproduce. A label that the
    classifying model made at its higher temperature is repeated at that temperature, and a
    label that the second model made is repeated by the second model, through `second_client`,
    `client` by default, each with the settings that the label records (`fallback_classifier`).
    The requests go to `service_tier`, which sets the price and changes nothing that the model
    is given. The MCP server of the tools reads the credentials file at `credentials_path`.
    `recorded_calls` holds the calls of READ_FIRST that the decisions of each record recorded
    (`repeat`). The repeats go to the backup provider of a model as the labels do, the backup
    that `backups(endpoint, role)` gives (`1_classify_repos.backup_for`), and the summaries to
    the backup provider of the summary model unless `no_backup_provider` is set or
    `unserved_backups` names the summary model. The parameters of the run match those of the
    current code by their keys (`label_cache.ParameterKeys`), which leave out the provider.
    `served` gives by role the provider that served the run's own responses of a model
    (`served_providers`), on which a model without a pinned record is called (`run_endpoints`)."""
    recorded = label_cache.recorded("parameters", run["parameters"], cache_dir)
    classifying, settings, summary = run_endpoints(recorded, run, served)
    for found in (classifying, summary):
        if found.openrouter and not found.provider:
            raise ParametersChanged(f"No provider of the current code serves {found.model} "
                                    f"({found.hugging_face_id}) in {found.quantization}, which "
                                    f"run {run['run_id']} recorded ({run['parameters'][:12]}). "
                                    f"Only the code of that run can repeat its labels")
    options = SimpleNamespace(summary_model=summary.model,
                              summary_effort=recorded["summary"]["reasoning_effort"],
                              summary_provider=summary.provider,
                              summary_quantization=summary.quantization,
                              summary_hugging_face_id=summary.hugging_face_id,
                              credentials=credentials_path or credentials.default_path(),
                              no_backup_provider=no_backup_provider,
                              unserved_backups=dict(unserved_backups or {}))
    async with bridge_for(options) as bridge:
        parameters = classify.classification_parameters(
            recorded["prompt"], bridge.function_tools, classifying, settings, summary,
            options.summary_effort, bridge.output_schemas)
        if (label_cache.digest(label_cache.without_provider(parameters))
                != label_cache.ParameterKeys(cache_dir)(run["parameters"])):
            raise ParametersChanged(f"The classification parameters of the current code differ "
                                    f"from the parameters that run {run['run_id']} recorded "
                                    f"({run['parameters'][:12]}). Only the code of that run can "
                                    f"repeat its labels")

        def make(endpoint, own):
            """A classifier of the model at `endpoint` with the settings `own` and the backup
            provider of the model."""
            role = (classify.CLASSIFYING_ROLE if endpoint == classifying
                    else classify.SECOND_ROLE)
            return classify.Classifier(
                client if endpoint.service == classifying.service else (second_client or client),
                bridge, endpoint.model, recorded["prompt"], own, run["parameters"],
                service_tier=service_tier if endpoint == classifying else None,
                summary_model=options.summary_model, endpoint=endpoint, summary=summary,
                backup=backups(endpoint, role) if backups is not None else None,
                role=f"the {role}")
        classifier = make(classifying, settings)
        others = {}
        for record in records:
            key = fallback_key(record["current"])
            if key and key not in others:
                others[key] = fallback_classifier(record["current"], classifying, settings, make)
        comparison = Comparison(records, classifier, github,
                                label_cache.changes_partial_path(cache_dir, run["run_id"]),
                                workers, recorded_calls, others)
        try:
            await comparison.run()
        finally:
            comparison.record_spent()
            # The repeats are requests of the run, and their tokens go into the run's tokens
            # file by the tier that OpenAI billed or the provider that served an OpenRouter model,
            # the pinned provider or its backup provider, as the labeling's own tokens do.
            for repeating in comparison.classifiers():
                label_cache.add_tokens(cache_dir, run["run_id"], repeating.take_billed())
    return comparison, classify.spent(github, *comparison.classifiers())


def report(changes: list[dict], counts: dict, previous_rid: str) -> None:
    by_cause = {cause: sum(1 for c in changes if c["cause"] == cause) for cause in CAUSES}
    labels = [c for c in changes if c["previous"]["outcome"] == c["current"]["outcome"]
              == LABELED]
    logs.info(COMPONENT, f"Compared with run {previous_rid}, {counts['unchanged']:,} "
                         f"repositories kept their outcome and {len(changes):,} changed it, "
                         f"{len(labels):,} of them from one label to another. "
                         f"{counts['new']:,} repositories are new to the sampling frame, "
                         f"{counts['left']:,} left it, {counts['undecided']:,} have no decision "
                         f"in one of the two runs, {counts['gone']:,} are gone and "
                         f"{counts['back']:,} are back.")
    parts = [f"{by_cause[cause]:,} {cause.replace('_', ' ')}" for cause in CAUSES]
    logs.info(COMPONENT, f"By cause, the changes are {', '.join(parts[:-1])} and {parts[-1]}.")


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare a run of the classification with the "
                                                 "previous run over a sampling frame.")
    parser.add_argument("--run", default=None,
                        help="The run to compare (default: the last finished run over a "
                             "sampling frame)")
    parser.add_argument("--previous", default=None,
                        help="The run to compare it with (default: the run over a sampling "
                             "frame that finished before it)")
    parser.add_argument("--cache-dir", type=Path, default=label_cache.CACHE_DIR,
                        help="The cache of labels (default: %(default)s)")
    parser.add_argument("--output-dir", type=Path, default=CLASSIFICATION_DIR,
                        help="The directory of label_changes.csv (default: %(default)s)")
    parser.add_argument("--workers", type=int, default=classify.WORKERS,
                        help="Repositories compared at once (default: %(default)s)")
    parser.add_argument("--openai-service-tier", dest="service_tier",
                        choices=classify.SERVICE_TIERS, default="flex",
                        help="The OpenAI service tier of the requests that repeat the labels: "
                             "flex, default for the standard tier, or auto for the project's "
                             "setting (default: %(default)s)")
    parser.add_argument("--no-backup-provider", action="store_true",
                        help="Send every request that repeats a label to the pinned provider of "
                             "its model, also while the pinned provider fails, instead of "
                             "sending the requests that would use an attempt to the backup "
                             "provider of the model")
    parser.add_argument("--credentials", type=Path, default=credentials.default_path(),
                        help="The credentials file, read after the environment (default: "
                             "%(default)s)")
    args = parser.parse_args(argv)
    logs.capture_libraries()
    return compare(args)


def compare(args, connect=connect, bridge_for=classify.open_bridge) -> int:
    """The comparison of args.run, or of the last run over a sampling frame, with the previous
    run, and its exit code. `connect(args, endpoint)` returns the GitHub client and the client of
    the service of the run's classifying model, and `bridge_for(settings)` the bridge to the
    tools, when a label is repeated."""
    cache_dir = Path(args.cache_dir)
    tier = getattr(args, "service_tier", None)
    try:
        rows = {row["run_id"]: row for row in label_cache.read_index(cache_dir)}
        sampling_frame_runs = [rid for rid, row in rows.items() if row["outcome"] == "finished"
                      and row["scope"] == label_cache.SAMPLING_FRAME_SCOPE
                      and row["labels_sha256"]]
        rid = args.run or (sampling_frame_runs[-1] if sampling_frame_runs else None)
        if rid is None or rid not in rows or not rows[rid]["labels_sha256"]:
            logs.error(COMPONENT, f"The cache of labels {cache_dir} has no finished run "
                                  f"{rid or 'over a sampling frame'} with its labels.")
            return 1
        run = rows[rid]
        current_rows = label_cache.read_labels(cache_dir, rid, run["labels_sha256"])
        previous = rows.get(args.previous) if args.previous else label_cache.previous_run(
            cache_dir, rid)
        if previous is None or not previous["labels_sha256"]:
            if args.previous:
                logs.error(COMPONENT, f"The cache of labels {cache_dir} has no finished run "
                                      f"{args.previous} with its labels.")
                return 1
            classify.write_csv(Path(args.output_dir) / "label_changes.csv", CHANGE_COLUMNS, [])
            logs.info(COMPONENT, f"Run {rid} is the first run over a sampling frame in "
                                 f"{cache_dir}, with no earlier run to compare it with.")
            return 0
        if previous["run_id"] >= rid:
            # Compared with itself, a run would show no change. Compared with a later run, every
            # change would run backwards, and the later run's label would count as the earlier
            # label.
            logs.error(COMPONENT, f"Run {previous['run_id']} did not start before run {rid}. "
                                  f"Compare a run with an earlier run.")
            return 1
        previous_rows = label_cache.read_labels(cache_dir, previous["run_id"],
                                                previous["labels_sha256"])
        previous_id = previous["run_id"]
        start_comparison(cache_dir, rid, previous_id)
        changed, counts = pair(previous_rows, current_rows,
                               decided_before(cache_dir, rows, previous_id))
        wanted: dict = {}
        for old, row in changed:
            for line in (old, row):
                wanted.setdefault(line["decided_in"], set()).add(int(line["id"]))
        decisions = label_cache.decisions_of(cache_dir, wanted)
        known: dict = {}
        keys = label_cache.ParameterKeys(cache_dir)
        records = [change_record(rid, previous_id, old, row, decisions,
                                 lambda sha: label_cache.shared_parameters(cache_dir, sha, known),
                                 keys)
                   for old, row in changed]
        to_repeat = [r for r in records if "cause" not in r]
        # In an invocation of the classification in which the second model cannot label, the
        # comparison sends it no request either, because each would fail as the requests of the
        # labeling would, and the comparison would exit 1 in every invocation until the second
        # model labels again. The labels that it made keep an undetermined cause, with the
        # reason.
        unavailable = getattr(args, "second_model_unavailable", "")
        held = {r["id"]: f"the second model made the current label and labels nothing in this "
                         f"invocation. {unavailable}"
                for r in to_repeat if unavailable and r["current"].get("second_model")}
        repeatable = [r for r in to_repeat if r["id"] not in held]
        relabeled = sum(1 for r in records
                        if r["previous"]["outcome"] == r["current"]["outcome"] == LABELED)
        logs.info(COMPONENT, f"Compared with run {previous_id}, {len(records):,} repositories "
                             f"changed their outcome in run {rid}, {relabeled:,} of them from "
                             f"one label to another.")
        logs.info(COMPONENT, f"Repeating the labels of {len(repeatable):,} repositories with "
                             f"GitHub and the model.")
        totals = {}
        if to_repeat:
            served = served_providers(decisions[(record["decided_in"]["current"], record["id"])]
                                      for record in to_repeat)
            endpoint = run_endpoints(label_cache.recorded(
                "parameters", run["parameters"], cache_dir), run, served)[0]
            # OpenRouter has no service tier of OpenAI's, and the provider that served each
            # repeat prices it in the cost record.
            if endpoint.openrouter:
                tier = endpoint.provider
            partial = label_cache.changes_partial_path(cache_dir, rid)
            # The labels that an earlier invocation repeated are kept, also those of the second
            # model and those that this code cannot repeat (`ParametersChanged`).
            done, failed, unrepeatable = read_partial(partial), {}, ""
            totals = label_cache.spent_in(partial, label_cache.SPENT_COLUMNS)
            if repeatable:
                if classify.requests_unserved(args):
                    return 1
                github, client = connect(args, endpoint)
                # The second model of an OpenAI model's run is OpenRouter's, with a client of its
                # own, and it repeats the labels that it made.
                second_client = client
                for record in repeatable:
                    second = record["current"].get("second_model")
                    if second and (classify.endpoints.Endpoint.of(second).service
                                   != endpoint.service):
                        second_client = connect(args, classify.endpoints.Endpoint.of(second))[1]
                        break
                # Until the comparison has made a request of its own that shows whether GitHub
                # responds, the README of a compared decision stands in for that request, as in
                # the labeling (`readmes.Client.served_earlier`). The README of a repeat that
                # GitHub failed in an earlier invocation does not, because GitHub can fail that
                # repeat on its own.
                failed_before = failed_on_github(partial)
                for record in records:
                    current = decisions[(record["decided_in"]["current"], record["id"])]
                    if (record["id"] not in failed_before
                            and github.served_earlier(record["name"], current)):
                        break
                # The results that each decision recorded for its first request, which the
                # repeat of its input takes rather than reading GitHub's current data.
                recorded_calls = {record["id"]: {
                    side: classify.recorded_results(decisions[(record["decided_in"][side],
                                                               record["id"])])
                    for side in ("current", "previous")} for record in repeatable}
                try:
                    comparison, totals = asyncio.run(explain(
                        repeatable, run, cache_dir, github, client, bridge_for, args.workers,
                        tier, args.credentials,
                        recorded_calls, second_client,
                        lambda found, role: classify.backup_for(args, found, role),
                        getattr(args, "no_backup_provider", False),
                        getattr(args, "unserved_backups", None), served))
                except ParametersChanged as error:
                    # This code cannot repeat the labels that no earlier invocation repeated.
                    unrepeatable = str(error)
                else:
                    done, failed = comparison.done, comparison.failed
                    unrepeatable = comparison.unrepeatable
                    if not unrepeatable and (comparison.stopped or any(
                            r["id"] not in done and r["id"] not in failed for r in repeatable)):
                        costs.record_or_warn(cache_dir, rid, tier)
                        logs.error(COMPONENT, f"{len(to_repeat) - len(done):,} of "
                                              f"{len(to_repeat):,} repositories have no repeated "
                                              f"label. Saved the {len(done):,} repeated labels in "
                                              f"{partial.name}. The next invocation repeats the "
                                              f"rest.")
                        return 1
                    ceiling = classify.undecided_ceiling(len(current_rows))
                    if not unrepeatable and len(failed) > ceiling:
                        # This many failed repeats point to a failure of GitHub or the model that
                        # outlasted the retry rounds. If the comparison were written now, their
                        # causes would stay undetermined for good. The next invocation repeats
                        # them instead.
                        costs.record_or_warn(cache_dir, rid, tier)
                        logs.error(COMPONENT, f"{len(failed):,} of {len(to_repeat):,} "
                                              f"repositories have no repeated label after the "
                                              f"retry rounds, more than the ceiling of "
                                              f"{ceiling:,} for {len(current_rows):,} "
                                              f"repositories. Saved the {len(done):,} repeated "
                                              f"labels in {partial.name}. The next invocation "
                                              f"repeats the rest.")
                        return 1
                    # Every invocation of the comparison, including a killed one, recorded what
                    # it spent.
                    totals = label_cache.spent_in(partial, totals)
            if unrepeatable:
                logs.warning(COMPONENT, f"{unrepeatable[:1].upper()}{unrepeatable[1:]}. Recorded "
                                        f"the causes of the repositories without a repeated label "
                                        f"as undetermined.")
                failed = {**{r["id"]: unrepeatable for r in repeatable if r["id"] not in done},
                          **failed}
            if any(number not in done for number in held):
                logs.warning(COMPONENT, f"The second model labels nothing in this invocation. "
                                        f"{unavailable}. Recorded the causes of the repositories "
                                        f"with a current label of the second model and without a "
                                        f"repeated label as undetermined.")
                failed = {**{number: reason for number, reason in held.items()
                             if number not in done}, **failed}
            if failed:
                first = next(r for r in to_repeat if r["id"] in failed)
                logs.warning(COMPONENT, f"{len(failed):,} of {len(to_repeat):,} repositories "
                                        f"have no repeated label and an undetermined cause. The "
                                        f"first is {first['name']} ({failed[first['id']]}).")
            records = [done[r["id"]] if r["id"] in done
                       else unrepeated(r, failed[r["id"]]) if r["id"] in failed else r
                       for r in records]
        written, checksums = label_cache.write_changes(cache_dir, rid, records)
        # The CSV is written before the index row that records the comparison. A stop between
        # the two writes leaves a run that the index does not name as compared, and the next
        # invocation compares it again from the partial file. A comparison recorded before its
        # CSV would count as complete, and the next Start would begin a new run with a comparison
        # that replaces it.
        write_changes_csv(cache_dir, args.output_dir,
                          [*getattr(args, "earlier_runs", []), rid], {rid: checksums})
        label_cache.put_index_row({"run_id": rid, "compared_with": previous_id,
                                   "label_changes": written, "changes_sha256": checksums,
                                   **(added(rid, cache_dir, totals) if totals else {})},
                                  cache_dir)
        label_cache.changes_partial_path(cache_dir, rid).unlink(missing_ok=True)
        report(records, counts, previous_id)
        if to_repeat:
            # The run's cost record, written when the run ended, lacks the repeats.
            costs.record_or_warn(cache_dir, rid, tier)
        return 0
    except (CacheError, NoCredentials) as error:
        logs.error(COMPONENT, f"The comparison cannot be made, because {error}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
