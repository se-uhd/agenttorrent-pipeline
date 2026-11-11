"""
Tests for 2_evaluate_labels.py

Strategy:
  - Each case builds a cache of labels and a sampling frame in a temporary directory, with the
    runs of two models written through label_cache as the classification writes them.
  - The size of the evaluation sample and Cohen's kappa are checked against values worked out
    by hand: 1,043 for the sampling frame of October 2026, the size decided for the evaluation,
    and kappa over four pairs.
  - Each case replaces the calibration sample with a list of its own, and the draw leaves out
    the repositories of that list.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_evaluate_labels -v
"""

import contextlib
import csv
import fcntl
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
evaluate = importlib.import_module("2_evaluate_labels")
import label_cache  # noqa: E402
from test_classify_repos import (DEEPSEEK, ENGLISH, _always_true, _GitHub, _Model,  # noqa: E402
                                 _repositories, _RunCase)

LUNA, SOL = "gpt-6-luna", "gpt-6.1-sol"
DISAGREE, AGREE = evaluate.DISAGREEMENT, evaluate.AGREEMENT
# Sets of classification parameters, each with its classifying model, by their digests as the
# cache keeps them. Each run writes its parameters to the cache before its first decision
# (`run_of`).
PARAMETERS = {name: {"prompt": name, "classifying_model": {"model": LUNA,
                                                           "reasoning_effort": "medium"}}
              for name in ("c1", "c2", "c3")}
RECORDS = {label_cache.digest(value): value for value in PARAMETERS.values()}
C1, C2, C3 = (label_cache.digest(PARAMETERS[name]) for name in ("c1", "c2", "c3"))


def _changed(parameters, **changes):
    """The digest of the classification parameters that the digest `parameters` names with
    `changes` in their block of the classifying model, such as another provider or another
    reasoning effort. Their record is added to RECORDS."""
    value = RECORDS[parameters]
    value = {**value, "classifying_model": {**value["classifying_model"], **changes}}
    RECORDS[label_cache.digest(value)] = value
    return label_cache.digest(value)


def _second(model, parameters=C1):
    """The digest of the classification parameters that the run of `model` over the evaluation
    sample of an evaluation under `parameters` records, `1_classify_repos.py --model <model>`
    without an endpoint or settings on its command line: those parameters with the endpoint and
    the settings of the requests that the classification resolves for `model` in place of their
    classifying model. Their record is added to RECORDS."""
    args = evaluate.CLASSIFY.build_parser().parse_args(["--model", model])
    block = {**evaluate.CLASSIFY.classifying_endpoint(args).parameters(),
             **evaluate.CLASSIFY.run_settings(args).parameters()}
    value = {**RECORDS[parameters], "classifying_model": block}
    RECORDS[label_cache.digest(value)] = value
    return label_cache.digest(value)


def _label(repository_id, label, model, sha="s", parameters=C1):
    return {"id": repository_id, "name": f"o/r{repository_id}", "outcome": label_cache.LABELED,
            "model": model, "parameters": parameters, "readme_sha": sha,
            "readme_path": "README.md", "label": label, "justification": f"{model} says {label}",
            "tools": [{"tool": "get_file_tree"}] if repository_id % 7 == 0 else [],
            "decided_at": "2026-10-06T08:00:00Z"}


class _EvaluationCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "labels"
        self.sampling_frame = self.tmp / "ghs_filtered.json"
        self.sampling_frame.write_text(json.dumps({
            "_metadata": {"input_source": {"retrieval_id": "2026-10-05T101430Z"}},
            "items": [{"id": i, "name": f"o/r{i}"} for i in range(1, 61)]}))

    def run_of(self, rid, model, decisions, parameters=C1, outcome="finished",
               scope="sampling_frame", labels=None):
        """A run that made `decisions`. A finished run writes the outcome of every repository
        that it covered, `labels`, or by default the outcome of each of its decisions."""
        for value in {d["parameters"] for d in decisions} | {parameters}:
            label_cache.record("parameters", RECORDS[value], self.cache)
        count, sha = label_cache.write_decisions(self.cache, rid, decisions)
        if labels is None:
            labels = [{"id": d["id"], "repo_name": d["name"], "outcome": d["outcome"],
                       "label": d.get("label", ""), "reason": d.get("reason", ""),
                       "decided_in": rid} for d in decisions]
        labels_sha = (label_cache.write_labels(self.cache, rid, labels) if outcome == "finished"
                      else "")
        label_cache.put_index_row({"run_id": rid, "outcome": outcome, "model": model,
                                   "parameters": parameters, "decisions": count,
                                   "decisions_sha256": sha, "scope": scope,
                                   "retrieval_id": "2026-10-05T101430Z",
                                   "labels_sha256": labels_sha}, self.cache)

    def main(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = evaluate.main(["--cache-dir", str(self.cache), *argv])
        return code, err.getvalue()

    def draw(self, seed="7", directory="evaluation"):
        code, err = self.main("draw", "--reference-model", LUNA,
                              "--sampling-frame", str(self.sampling_frame),
                              "--evaluation-dir", str(self.tmp / directory), "--seed", seed)
        self.assertEqual(code, 0, err)
        return sorted((self.tmp / directory).iterdir())[-1]


class TestSampleSize(unittest.TestCase):

    def test_the_size_decided_for_the_sampling_frame_of_october(self):
        self.assertEqual(evaluate.sample_size(45_849), 1_043)

    def test_a_small_population_needs_a_larger_share_and_never_more_than_itself(self):
        self.assertEqual(evaluate.sample_size(10), 10)
        self.assertGreater(evaluate.sample_size(500) / 500, evaluate.sample_size(45_849) / 45_849)
        self.assertGreater(evaluate.sample_size(45_849, margin=0.02), 1_043)


class TestDraw(_EvaluationCase):

    def test_the_evaluation_sample_contains_repositories_that_the_reference_model_labeled(self):
        self.run_of("2026-10-06T080000Z", LUNA,
                    [_label(i, "true", LUNA) for i in range(1, 41)])
        out = self.draw()
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        self.assertEqual(len(ids), evaluate.sample_size(40))
        self.assertTrue(set(ids) <= set(range(1, 41)))
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual({k: meta[k] for k in ("reference_model", "parameters", "population",
                                               "size", "seed", "retrieval_id")},
                         {"reference_model": LUNA, "parameters": C1, "population": 40,
                          "size": len(ids), "seed": 7,
                          "retrieval_id": "2026-10-05T101430Z"})

    def test_the_calibration_sample_is_left_out(self):
        """The classification's rules were set on the calibration sample. A repository from the
        calibration sample in the evaluation sample would test the labels on repositories that
        the rules were fitted to."""
        self.assertTrue(evaluate.CALIBRATION_SAMPLE.is_file())
        calibration = self.tmp / "calibration_sample.csv"
        calibration.write_text("id,name,revision\n" + "".join(f"{i},o/r{i},x\n"
                                                             for i in range(1, 31)))
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        code, err = self.main("draw", "--reference-model", LUNA,
                              "--sampling-frame", str(self.sampling_frame),
                              "--evaluation-dir", str(self.tmp / "evaluation"), "--seed", "7",
                              "--exclude", str(calibration))
        self.assertEqual(code, 0, err)
        out = sorted((self.tmp / "evaluation").iterdir())[-1]
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = [int(row["id"]) for row in csv.DictReader(f)]
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual(len(chosen), evaluate.sample_size(30))
        self.assertTrue(all(i > 30 for i in chosen))
        self.assertEqual((meta["labeled"], meta["left_out"], meta["population"]), (60, 30, 30))
        self.assertEqual(meta["excluded"][0]["repositories"], 30)

    def test_a_repository_that_the_prompt_names_is_left_out(self):
        """The prompt names mrexodia/ida-pro-mcp and elizaOS/elizaos.github.io as examples of how
        a criterion applies. Drawn into an evaluation, such a repository would test a label that
        the prompt states. The draw leaves out every repository in the sampling frame that the
        prompt names, also with --exclude, and records the prompt."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        prompt = self.tmp / "prompt.md"
        prompt.write_text("Such as O/R5, as in `o/r7`, but not o/r60x or the path src/o/r9.\n")
        with mock.patch.object(evaluate.CLASSIFY, "PROMPT", prompt):
            code, err = self.main("draw", "--reference-model", LUNA,
                                  "--sampling-frame", str(self.sampling_frame),
                                  "--evaluation-dir", str(self.tmp / "evaluation"),
                                  "--margin", "0.0001", "--exclude")
        self.assertEqual(code, 0, err)
        out = sorted((self.tmp / "evaluation").iterdir())[-1]
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = {int(row["id"]) for row in csv.DictReader(f)}
        self.assertEqual(chosen, set(range(1, 61)) - {5, 7})
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual(meta["named_in_prompt"]["repositories"], 2)

    def test_a_repository_that_the_prompt_quotes_is_left_out(self):
        """The prompt quotes, without their names, the descriptions of LibreOffice/core,
        coreutils/coreutils and PierfrancescoSoffritti/doodles and the READMEs of
        algolia/algoliasearch-client-php, KDE/elisa, bcgit/bc-java, bcgit/bc-csharp,
        krgamestudios/Toy, assert-rs/libtest2, warden-protocol/wardenprotocol and
        alainbryden/bitburner-scripts. The draw over the real prompt leaves them out as it leaves
        out a repository that the prompt names, and records their names. It keeps a repository
        of an owner whose profile description the prompt quotes, microsoft/quicreach, and a
        repository with the JDK's README, openjdk/loom. The test fails when the prompt no longer
        contains the words of one of the quoted repositories."""
        quoted = {11: "LibreOffice/core", 12: "algolia/algoliasearch-client-php", 13: "KDE/elisa",
                  14: "coreutils/coreutils", 15: "bcgit/bc-java", 16: "bcgit/bc-csharp",
                  17: "krgamestudios/Toy", 18: "assert-rs/libtest2",
                  19: "PierfrancescoSoffritti/doodles", 20: "warden-protocol/wardenprotocol",
                  21: "alainbryden/bitburner-scripts"}
        kept = {22: "microsoft/quicreach", 23: "openjdk/loom"}
        frame = json.loads(self.sampling_frame.read_text())
        for item in frame["items"]:
            item["name"] = (quoted | kept).get(item["id"], item["name"])
        self.sampling_frame.write_text(json.dumps(frame))
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        code, err = self.main("draw", "--reference-model", LUNA,
                              "--sampling-frame", str(self.sampling_frame),
                              "--evaluation-dir", str(self.tmp / "evaluation"),
                              "--margin", "0.0001", "--exclude")
        self.assertEqual(code, 0, err)
        out = sorted((self.tmp / "evaluation").iterdir())[-1]
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = {int(row["id"]) for row in csv.DictReader(f)}
        self.assertEqual(chosen, set(range(1, 61)) - set(quoted))
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual((meta["named_in_prompt"]["repositories"],
                          meta["named_in_prompt"]["quoted"]),
                         (len(quoted), sorted(quoted.values())))

    def test_a_repository_stays_in_the_draw_once_the_prompt_no_longer_quotes_it(self):
        """A repository is left out for a quote only while the prompt contains the quoted
        words, and a passage that the texts of two repositories share leaves out both."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        prompt = self.tmp / "prompt.md"
        prompt.write_text('A copy, as "Read-only mirror of the core repo" says.\n')
        quotes = {"Read-only mirror of the core repo": ("o/r3", "o/r6"),
                  "Pull requests go elsewhere": ("o/r4",)}
        with mock.patch.object(evaluate.CLASSIFY, "PROMPT", prompt), \
                mock.patch.object(evaluate, "QUOTED_IN_PROMPT", quotes):
            code, err = self.main("draw", "--reference-model", LUNA,
                                  "--sampling-frame", str(self.sampling_frame),
                                  "--evaluation-dir", str(self.tmp / "evaluation"),
                                  "--margin", "0.0001", "--exclude")
        self.assertEqual(code, 0, err)
        out = sorted((self.tmp / "evaluation").iterdir())[-1]
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = {int(row["id"]) for row in csv.DictReader(f)}
        self.assertEqual(chosen, set(range(1, 61)) - {3, 6})
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual(meta["named_in_prompt"]["quoted"], ["o/r3", "o/r6"])

    def test_every_passage_in_double_quotes_of_the_prompt_has_a_decision(self):
        """Each passage that the classification prompt puts in double quotes either quotes a text
        of the repositories that QUOTED_IN_PROMPT gives for it, which the draw leaves out, or is
        in QUOTED_FROM_NO_REPOSITORY, which records why the draw keeps the repositories that write
        it. A new quotation in the prompt fails this test until someone decides which it is, and
        so does an entry for a passage that the prompt no longer contains."""
        text = evaluate.CLASSIFY.PROMPT.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), 1):
            self.assertEqual(line.count('"') % 2, 0, f"line {number} has an unpaired quote")
        passages = set(re.findall(r'"([^"\n]+)"', text))
        attributed, unattributed = set(evaluate.QUOTED_IN_PROMPT), set(
            evaluate.QUOTED_FROM_NO_REPOSITORY)
        self.assertEqual(passages - attributed - unattributed, set())
        self.assertEqual((attributed | unattributed) - passages, set())
        self.assertEqual(attributed & unattributed, set())

    def test_the_record_names_a_list_by_its_path_in_the_repository(self):
        """Because git tracks the record, the record names the calibration sample by its path
        in this repository rather than by an absolute path on the machine that drew it."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        outside = self.tmp / "left_out.csv"
        outside.write_text("id\n1\n")
        code, err = self.main("draw", "--reference-model", LUNA,
                              "--sampling-frame", str(self.sampling_frame),
                              "--evaluation-dir", str(self.tmp / "evaluation"), "--seed", "7",
                              "--exclude", str(evaluate.CALIBRATION_SAMPLE), str(outside))
        self.assertEqual(code, 0, err)
        out = sorted((self.tmp / "evaluation").iterdir())[-1]
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual([f["path"] for f in meta["excluded"]],
                         ["data/classification/calibration/calibration_sample.csv",
                          "left_out.csv"])

    def test_a_list_to_leave_out_that_cannot_be_read_stops_the_draw(self):
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        with self.assertRaises(SystemExit):
            self.main("draw", "--reference-model", LUNA,
                      "--sampling-frame", str(self.sampling_frame),
                      "--evaluation-dir", str(self.tmp / "evaluation"),
                      "--exclude", str(self.tmp / "missing.csv"))

    def test_a_review_before_compare_names_the_file_it_lacks(self):
        """A review before compare ends with a message rather than a FileNotFoundError and its
        traceback."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw()
        with self.assertRaises(SystemExit) as stopped:
            evaluate.Review(out, self.cache, lambda *arguments: ("", ""))
        self.assertIn("agreement.json does not exist. Run compare first", str(stopped.exception))

    def test_the_draw_takes_the_latest_run_over_the_sampling_frame(self):
        """The draw skips a later finished run of the reference model over part of the
        sampling frame, such as a check under other classification parameters."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        self.run_of("2026-10-12T080000Z", LUNA,
                    [_label(i, "false", LUNA, parameters=C2) for i in range(1, 11)],
                    parameters=C2, scope="repository_list")
        meta = json.loads((self.draw() / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual({k: meta[k] for k in ("run_id", "parameters", "labeled", "population")},
                         {"run_id": "2026-10-06T080000Z", "parameters": C1, "labeled": 60,
                          "population": 60})
        frame = json.loads(self.sampling_frame.read_text())
        frame["_metadata"]["input_source"]["retrieval_id"] = "2026-11-05T101430Z"
        self.sampling_frame.write_text(json.dumps(frame))
        code, err = self.main("draw", "--reference-model", LUNA,
                              "--sampling-frame", str(self.sampling_frame),
                              "--evaluation-dir", str(self.tmp / "evaluation"), "--seed", "7")
        self.assertEqual(code, 1)
        self.assertIn("labeled the sampling frame of retrieval 2026-10-05T101430Z", err)

    def test_a_repository_that_the_latest_run_did_not_label_is_not_in_the_population(self):
        """A repository that the latest run over the sampling frame excluded, found gone or
        left without a decision keeps the label that an earlier run gave it under the same
        classification parameters. If drawn, such a repository would get no label from the
        second model's run over the evaluation sample, and compare would refuse the evaluation at
        every attempt."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        later = "2026-10-07T080000Z"
        excluded = [{"id": i, "name": f"o/r{i}", "outcome": label_cache.EXCLUDED,
                     "parameters": C1, "reason": "readme_non_english", "readme_sha": "t",
                     "decided_at": "2026-10-07T08:00:00Z"} for i in range(1, 11)]
        labels = ([{"id": d["id"], "repo_name": d["name"], "outcome": label_cache.EXCLUDED,
                    "reason": d["reason"], "decided_in": later} for d in excluded]
                  + [{"id": i, "repo_name": f"o/r{i}", "outcome": label_cache.UNDECIDED,
                      "reason": "model: ModelFailure: the model's response is not JSON"}
                     for i in range(11, 16)]
                  + [{"id": i, "repo_name": f"o/r{i}", "outcome": label_cache.GONE}
                     for i in range(16, 21)]
                  + [{"id": i, "repo_name": f"o/r{i}", "outcome": label_cache.LABELED,
                      "label": "true", "decided_in": "2026-10-06T080000Z"}
                     for i in range(21, 61)])
        self.run_of(later, LUNA, excluded, labels=labels)
        out = self.draw()
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = [int(row["id"]) for row in csv.DictReader(f)]
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual((meta["run_id"], meta["labeled"], meta["population"]), (later, 40, 40))
        self.assertEqual(len(chosen), evaluate.sample_size(40))
        self.assertTrue(all(i > 20 for i in chosen))

    def test_a_repository_that_the_second_model_labeled_is_left_out_and_recorded(self):
        """The second model labels the evaluation sample, and it cannot check the labels that it
        made in the reference model's run for the repositories that the reference model failed.
        The draw leaves them out, also a label that the run reused from an earlier run, and
        records them among what it left out."""
        def by_second(i):
            return {**_label(i, "true", LUNA),
                    "second_model": {"model": "z-ai/glm-5.3", "reason": "output_limit"}}
        earlier, later = "2026-10-06T080000Z", "2026-10-07T080000Z"
        self.run_of(earlier, LUNA, [by_second(i) if i == 3 else _label(i, "true", LUNA)
                                    for i in range(1, 61)])
        made = [by_second(i) for i in (5, 7)]
        labels = [{"id": i, "repo_name": f"o/r{i}", "outcome": label_cache.LABELED,
                   "label": "true", "decided_in": later if i in (5, 7) else earlier}
                  for i in range(1, 61)]
        self.run_of(later, LUNA, made, labels=labels)
        out = self.draw()
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = {int(row["id"]) for row in csv.DictReader(f)}
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertFalse(chosen & {3, 5, 7})
        self.assertEqual(meta["labeled_by_second_model"],
                         {"repositories": 3, "names": ["o/r3", "o/r5", "o/r7"]})
        self.assertEqual((meta["labeled"], meta["population"]), (60, 57))

    def test_the_same_seed_draws_the_same_evaluation_sample(self):
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        first = (self.draw() / evaluate.SAMPLE_CSV).read_text()
        self.assertEqual((self.draw(directory="again") / evaluate.SAMPLE_CSV).read_text(), first)

    def test_the_samples_of_the_earlier_evaluations_are_left_out(self):
        """A draw without `--exclude` leaves out the calibration sample and the evaluation
        sample of every evaluation in `--evaluation-dir`. It leaves out the evaluation samples
        because the prompt was revised on the reviewed disagreements of each evaluation, as it
        was after the pilots of evaluations 2026-10-30T222453Z and 2026-10-31T094615Z. With
        `--exclude`, a draw leaves out only the lists that the option names."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 61)])
        for name, ids in (("2026-10-30T222453Z", range(1, 11)),
                          ("2026-10-31T094615Z", range(11, 21))):
            earlier = self.tmp / "evaluation" / name
            earlier.mkdir(parents=True)
            (earlier / evaluate.SAMPLE_CSV).write_text(
                "id,repo_name\n" + "".join(f"{i},o/r{i}\n" for i in ids))
        out = self.draw()
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            chosen = [int(row["id"]) for row in csv.DictReader(f)]
        meta = json.loads((out / evaluate.SAMPLE_JSON).read_text())
        self.assertTrue(all(i > 20 for i in chosen))
        self.assertEqual((meta["left_out"], meta["population"]), (20, 40))
        self.assertEqual([(f["path"], f["repositories"]) for f in meta["excluded"]],
                         [("data/classification/calibration/calibration_sample.csv",
                           meta["excluded"][0]["repositories"]),
                          (evaluate.SAMPLE_CSV, 10), (evaluate.SAMPLE_CSV, 10)])
        outside = self.tmp / "left_out.csv"
        outside.write_text("id\n1\n")
        code, err = self.main("draw", "--reference-model", LUNA,
                              "--sampling-frame", str(self.sampling_frame),
                              "--evaluation-dir", str(self.tmp / "evaluation"), "--seed", "7",
                              "--exclude", str(outside))
        self.assertEqual(code, 0, err)
        meta = json.loads((sorted((self.tmp / "evaluation").iterdir())[-1]
                           / evaluate.SAMPLE_JSON).read_text())
        self.assertEqual((meta["left_out"], meta["population"]), (1, 59))


class TestDataRoot(unittest.TestCase):
    """The evaluation's default data paths follow the data root that `AGENTCONTEXT_DATA_ROOT`
    names, as the container sets it. The check is here rather than in
    `tests/test_data_layout.py`, which CI runs without the packages of this stage that the script
    imports through `1_classify_repos.py`."""

    def test_the_evaluations_and_the_sampling_frame_are_under_the_data_root(self):
        import os
        child = ("import importlib, sys; sys.path.insert(0, sys.argv[1]); "
                 "m = importlib.import_module('2_evaluate_labels'); "
                 "print(m.EVALUATION_DIR); print(m.SAMPLING_FRAME)")
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENTCONTEXT_DATA_ROOT=tmp)
            result = subprocess.run([sys.executable, "-c", child,
                                     str(Path(evaluate.__file__).parent)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            root = Path(tmp).resolve()
            self.assertEqual([Path(line) for line in result.stdout.splitlines()],
                             [root / "classification" / "evaluation",
                              root / "sampling" / "ghs_filtered.json"])


class TestCompare(_EvaluationCase):

    def test_the_disagreements_are_listed_for_review_and_the_agreement_reported(self):
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw(seed="1")
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        sol = _second(SOL)
        second = [_label(i, "false" if i == ids[0] else "true", SOL, parameters=sol)
                  for i in ids]
        second[1]["readme_sha"] = "changed"
        self.run_of("2026-10-07T080000Z", SOL, second, sol)
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 0, err)
        with open(out / "disagreements.csv", newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(int(r["id"]), r["reference_label"], r["second_label"],
                           r["human_label"]) for r in rows], [(ids[0], "true", "false", "")])
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["compared"], agreement["agreed"]), (len(ids) - 1, len(ids) - 2))
        self.assertEqual(agreement["readme_changed_between_labels"], [f"o/r{ids[1]}"])
        self.assertEqual(agreement["confusion"]["true"]["false"], 1)

    def test_the_second_model_is_glm_by_default(self):
        """The maintainer chose GLM-5.3 as the second model on 2026-10-31. A comparison that
        names no second model compares the labels of z-ai/glm-5.3."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw(seed="1")
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        glm = "z-ai/glm-5.3"
        self.run_of("2026-10-07T080000Z", glm,
                    [_label(i, "true", glm, parameters=_second(glm)) for i in ids], _second(glm))
        code, err = self.main("compare", str(out))
        self.assertEqual(code, 0, err)
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["second_model"], agreement["agreed"]), (glm, len(ids)))

    def test_the_second_model_is_compared_under_its_weights_and_settings_on_any_provider(self):
        """GLM-5.3 labeled the evaluation sample in fp8 at the reasoning effort high, and later
        runs of GLM-5.3 in another quantization and at another reasoning effort labeled the same
        repositories otherwise. The comparison takes the labels of the weights, the quantization
        and the settings of the pinned record alone, and agreement.json records them. The
        provider is no classification parameter (the maintainer's decision of 2026-11-04), and a
        later run of the same weights on another provider, recorded with that provider as the
        parameters before that day record it, gives the latest labels under the same
        parameters."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw(seed="1")
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        glm = "z-ai/glm-5.3"
        pinned = _second(glm)
        self.run_of("2026-10-07T080000Z", glm,
                    [_label(i, "true", glm, parameters=pinned) for i in ids], pinned,
                    scope="repository_list")
        for rid, other in (("2026-10-08T080000Z", _changed(pinned, quantization="mxfp4")),
                           ("2026-10-09T080000Z", _changed(pinned, reasoning_effort="max"))):
            self.run_of(rid, glm, [_label(i, "false", glm, parameters=other) for i in ids],
                        other, scope="repository_list")
        code, err = self.main("compare", str(out))
        self.assertEqual(code, 0, err)
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["compared"], agreement["agreed"]), (len(ids), len(ids)))
        self.assertEqual(agreement["second_model_parameters"], pinned)
        recorded = agreement["second_model_endpoint_and_settings"]
        self.assertEqual({key: recorded.get(key) for key in ("model", "provider",
                                                             "quantization",
                                                             "reasoning_effort")},
                         {"model": glm, "provider": None, "quantization": "fp8",
                          "reasoning_effort": "high"})
        other = _changed(pinned, provider="chutes/fp8")
        self.run_of("2026-10-10T080000Z", glm,
                    [_label(i, "false", glm, parameters=other) for i in ids], other,
                    scope="repository_list")
        code, err = self.main("compare", str(out))
        self.assertEqual(code, 0, err)
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["compared"], agreement["agreed"]), (len(ids), 0))

    def test_a_second_model_without_a_pinned_endpoint_is_refused(self):
        """Kimi K3 has no pinned endpoint, so no labels of it are the labels of a second model
        on its pinned endpoint."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw()
        code, err = self.main("compare", str(out), "--second-model", "moonshotai/kimi-k3")
        self.assertEqual(code, 1)
        self.assertIn("ERROR evaluate_labels: moonshotai/kimi-k3 has no pinned endpoint, on which "
                      "the comparison takes the labels of the second model", err)
        self.assertFalse((out / "agreement.json").exists())

    def test_parameters_that_name_no_classifying_model_are_those_of_both_models(self):
        """The classification parameters of the evaluations of 2026-10-07, 2026-10-11 and
        2026-10-28 name no classifying model, and both models labeled under them."""
        named_none = {"prompt": "c0", "reasoning_effort": "medium"}
        c0 = label_cache.digest(named_none)
        RECORDS[c0] = named_none
        self.run_of("2026-10-06T080000Z", LUNA,
                    [_label(i, "true", LUNA, parameters=c0) for i in range(1, 11)], c0)
        out = self.draw(seed="1")
        self.run_of("2026-10-07T080000Z", SOL,
                    [_label(i, "true", SOL, parameters=c0) for i in range(1, 11)], c0,
                    scope="repository_list")
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 0, err)
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["parameters"], agreement["second_model_parameters"],
                          agreement["second_model_endpoint_and_settings"]), (c0, c0, None))
        self.assertEqual(agreement["agreed"], agreement["compared"])

    def test_the_second_model_is_compared_under_the_parameters_with_its_own_model(self):
        """The classification parameters name the classifying model with its endpoint and its
        reasoning effort. The run of the second model therefore records other parameters than
        the reference model's run, although both read the same prompt, tools and README. The
        review page shows the second model's decisions under its own parameters too."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw(seed="1")
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        sol = _second(SOL)
        self.run_of("2026-10-07T080000Z", SOL,
                    [_label(i, "false" if i == ids[0] else "true", SOL, parameters=sol)
                     for i in ids], sol, scope="repository_list")
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 0, err)
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["compared"], agreement["agreed"]), (len(ids), len(ids) - 1))
        review = evaluate.Review(out, self.cache, lambda *a: ("", ""))
        self.assertEqual(review.decisions["second"][ids[0]]["parameters"], sol)

    def test_both_models_are_compared_under_the_evaluations_parameters(self):
        """After a check of new classification parameters with the reference model, the
        comparison takes the reference model's labels under the evaluation's parameters. The
        reference model's labels under the new parameters would disagree everywhere with the
        second model's labels."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw(seed="1")
        self.run_of("2026-10-07T080000Z", SOL,
                    [_label(i, "true", SOL, parameters=_second(SOL)) for i in range(1, 11)],
                    _second(SOL), scope="repository_list")
        self.run_of("2026-10-08T080000Z", LUNA,
                    [_label(i, "false", LUNA, parameters=C2) for i in range(1, 11)],
                    parameters=C2, scope="repository_list")
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 0, err)
        agreement = json.loads((out / "agreement.json").read_text())
        self.assertEqual((agreement["parameters"], agreement["agreed"]),
                         (C1, agreement["compared"]))
        self.assertEqual(evaluate.read_disagreements(out), [])

    def test_kappa_over_four_pairs_worked_out_by_hand(self):
        """Observed 2/4; expected .5*.5 + .25*.5 + .25*0 = .375; (0.5 - .375) / .625 = 0.2."""
        pairs = [("true", "true"), ("true", "false"), ("false", "false"), ("unclear", "true")]
        self.assertAlmostEqual(evaluate.kappa(pairs), 0.2)

    def test_other_classification_parameters_are_refused(self):
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw()
        self.run_of("2026-10-07T080000Z", SOL,
                    [_label(i, "true", SOL, parameters=C2) for i in range(1, 11)], C2)
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 1)
        self.assertIn("lack a label of one of the models under the evaluation's classification "
                      f"parameters {C1[:12]}, which for {SOL} on its pinned endpoint with the "
                      f"settings of its requests are {_second(SOL)[:12]}. Label them under these "
                      f"parameters first", err)

    def test_a_repository_without_the_second_models_label_is_refused(self):
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw()
        self.run_of("2026-10-07T080000Z", SOL, [_label(1, "true", SOL)])
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 1)
        self.assertIn("lack a label of one of the models", err)

    def test_a_label_that_the_second_model_of_the_compared_models_run_made_is_refused(self):
        """A candidate compared in place of GLM-5.3 labeled the evaluation sample in a run of
        its own, which labeled one repository through its second model. That label names the
        candidate as its `model` and is no label of the candidate."""
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        out = self.draw()
        with open(out / evaluate.SAMPLE_CSV, newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        decisions = [_label(i, "true", SOL, parameters=_second(SOL)) for i in ids]
        decisions[0]["second_model"] = {"model": "z-ai/glm-5.3", "reason": "output_limit"}
        self.run_of("2026-10-07T080000Z", SOL, decisions, parameters=_second(SOL))
        code, err = self.main("compare", str(out), "--second-model", SOL)
        self.assertEqual(code, 1)
        self.assertIn(f"1 of the {len(ids)} repositories in the evaluation sample, such as "
                      f"o/r{ids[0]}, lack a label of one of the models", err)


class TestReview(_EvaluationCase):
    """The review's page and its four requests, served by the real server on a free port."""

    def setUp(self):
        super().setUp()
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 11)])
        self.out = self.draw(seed="1")
        with open(self.out / evaluate.SAMPLE_CSV, newline="") as f:
            self.ids = [int(row["id"]) for row in csv.DictReader(f)]
        second = [_label(i, "false" if i in self.ids[:2] else "true", SOL,
                         parameters=_second(SOL)) for i in self.ids]
        for decision in second:
            decision["revision"] = "r" * 40
            if decision["label"] == "false":
                decision["applied_criteria"] = ["inactive", "website"]
            decision["tools"] = [{"round": 1, "tool": "get_file_tree", "arguments": {},
                                  "output": f"Files of o/r{decision['id']}", "is_error": False}]
            decision["mirror_merges"] = {"latest_merge": "2026-02-23T21:12:04Z",
                                         "window_start": "2026-03-22",
                                         "merged_since_window_start": False}
            decision["readme_words_by_language"] = [{"language": "English", "words": 125},
                                                    {"language": "Spanish", "words": 73},
                                                    {"language": "Portuguese", "words": 73}]
            decision["readme_mainly_english"] = "false"
            decision["purpose_quote"] = "explore techniques to downsize Java object headers"
            decision["purpose_source"] = "https://openjdk.org/projects/lilliput"
        self.run_of("2026-10-07T080000Z", SOL, second, _second(SOL))
        code, err = self.main("compare", str(self.out), "--second-model", SOL)
        self.assertEqual(code, 0, err)
        self.fetched = []

        def fetch(name, repository_id, revision):
            self.fetched.append((name, revision))
            return f"# {name}", "s" if repository_id == self.ids[0] else "other"
        review = evaluate.Review(self.out, self.cache, fetch)
        self.server = evaluate.review_server(review, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path):
        with urllib.request.urlopen(self.base + path) as response:
            return response.read().decode("utf-8")

    def post(self, value):
        request = urllib.request.Request(self.base + "/api/decision",
                                         data=json.dumps(value).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def rows(self):
        with open(self.out / "disagreements.csv", newline="") as f:
            return {int(row["id"]): row for row in csv.DictReader(f)}

    def test_the_page_lists_each_disagreement_with_both_models_and_their_tool_outputs(self):
        self.assertIn("<title>Label review</title>", self.get("/"))
        data = json.loads(self.get("/api/items"))
        self.assertEqual(data["models"], [LUNA, SOL])
        self.assertEqual([item["id"] for item in data["items"]], self.ids[:2])
        item = data["items"][0]
        self.assertEqual((item["reference"]["label"], item["second"]["label"]), ("true", "false"))
        self.assertEqual(item["second"]["tools"][0]["output"], f"Files of o/r{self.ids[0]}")
        # Each model's side of an item carries what the classification decided after that
        # model's response, such as a mirror's merges, and the evidence for the language and the
        # purpose: the model's count of the words of the README by language and the page of a
        # quoted purpose.
        self.assertEqual(item["second"]["mirror_merges"]["latest_merge"], "2026-02-23T21:12:04Z")
        self.assertEqual((item["second"]["readme_words_by_language"][1],
                          item["second"]["purpose_source"]),
                         ({"language": "Spanish", "words": 73},
                          "https://openjdk.org/projects/lilliput"))
        self.assertEqual(item["github_url"], f"https://github.com/o/r{self.ids[0]}")

    def test_the_page_shows_the_decisions_under_the_evaluations_parameters(self):
        """The page shows each model's decisions under the evaluation's parameters. The
        decisions of a model's latest finished run, after a check of new parameters, would show
        other labels and criteria than the decisions that the evaluation compares."""
        later = [{**_label(i, "false", LUNA, parameters=C2), "applied_criteria": ["inactive"]}
                 for i in self.ids]
        self.run_of("2026-10-08T080000Z", LUNA, later, parameters=C2,
                    scope="repository_list")
        review = evaluate.Review(self.out, self.cache, lambda *arguments: ("", ""))
        item = review.items()[0]
        self.assertEqual((item["reference"]["label"], item["reference"]["applied_criteria"]),
                         ("true", []))
        self.assertEqual(review.decisions["reference"][self.ids[0]]["parameters"], C1)

    def test_the_page_shows_the_compared_decisions_after_the_second_model_changes(self):
        """agreement.json records the classification parameters of the labels of the second
        model that were compared. The page shows these decisions also after the settings of
        the requests of the second model change, such as its reasoning effort, and so does
        draw-check choose from these labels."""
        with mock.patch.object(evaluate.endpoints, "OPENAI_REASONING_EFFORT", "high"):
            review = evaluate.Review(self.out, self.cache, lambda *arguments: ("", ""))
            rows = evaluate.read_disagreements(self.out)
            for row in rows:
                row["human_label"] = "false"
            evaluate.write_disagreements(self.out, rows)
            code, err = self.main("draw-check", str(self.out), "--agreements",
                                  str(len(self.ids) - 2), "--seed", "3")
        self.assertEqual(review.decisions["second"][self.ids[0]]["applied_criteria"],
                         ["inactive", "website"])
        self.assertEqual(code, 0, err)

    def test_a_comparison_without_the_parameters_of_the_second_model_is_compared_again(self):
        """A review or a draw of a check sample over an agreement.json that does not name the
        classification parameters of the labels of the second model would read labels that
        were not compared."""
        path = self.out / "agreement.json"
        agreement = json.loads(path.read_text())
        del agreement["second_model_parameters"]
        path.write_text(json.dumps(agreement))
        rows = evaluate.read_disagreements(self.out)
        for row in rows:
            row["human_label"] = "false"
        evaluate.write_disagreements(self.out, rows)
        starts = {"review": lambda: evaluate.Review(self.out, self.cache,
                                                    lambda *arguments: ("", "")),
                  "draw-check": lambda: self.main("draw-check", str(self.out), "--agreements",
                                                  "1")}
        for command, start in starts.items():
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as raised:
                    start()
                self.assertIn("names no classification parameters of the labels of the second "
                              "model. Run compare again", str(raised.exception))
        self.assertFalse((self.out / evaluate.CHECK_SAMPLE_CSV).exists())

    def test_a_readme_is_read_at_the_revision_and_another_blob_is_named(self):
        same = json.loads(self.get(f"/api/readme?id={self.ids[0]}"))
        other = json.loads(self.get(f"/api/readme?id={self.ids[1]}"))
        self.assertEqual((same["text"], same["note"]), (f"# o/r{self.ids[0]}", ""))
        self.assertIn("not the blob s that the models read", other["note"])
        # The page reads a README once and keeps it.
        self.get(f"/api/readme?id={self.ids[0]}")
        self.assertEqual(len(self.fetched), 2)

    def test_a_decision_is_written_into_the_csv_and_the_other_rows_are_kept(self):
        status, item = self.post({"id": self.ids[0], "human_label": "false",
                                   "human_note": " a list of links "})
        self.assertEqual((status, item["human_label"]), (200, "false"))
        rows = self.rows()
        self.assertEqual((rows[self.ids[0]]["human_label"], rows[self.ids[0]]["human_note"]),
                         ("false", "a list of links"))
        self.assertEqual(rows[self.ids[1]]["human_label"], "")
        self.assertEqual(rows[self.ids[1]]["second_justification"], f"{SOL} says false")

    def test_a_label_outside_the_three_or_an_unknown_id_is_refused(self):
        self.assertEqual(self.post({"id": self.ids[0], "human_label": "maybe"})[0], 400)
        self.assertEqual(self.post({"id": 999, "human_label": "true"})[0], 400)
        self.assertEqual(self.rows()[self.ids[0]]["human_label"], "")

    def test_comparing_again_keeps_the_decisions(self):
        self.post({"id": self.ids[0], "human_label": "false", "human_note": "reviewed",
                   "human_criteria": ["website"]})
        code, err = self.main("compare", str(self.out), "--second-model", SOL)
        self.assertEqual(code, 0, err)
        row = self.rows()[self.ids[0]]
        self.assertEqual((row["human_label"], row["human_criteria"], row["human_note"]),
                         ("false", "website", "reviewed"))

    def test_the_models_criteria_are_compared_and_offered_to_the_reviewer(self):
        row = self.rows()[self.ids[0]]
        self.assertEqual((row["reference_criteria"], row["second_criteria"]),
                         ("", "inactive|website"))
        data = json.loads(self.get("/api/items"))
        self.assertEqual(data["criteria"], list(evaluate.CRITERIA))
        self.assertEqual(data["items"][0]["second"]["applied_criteria"], ["inactive", "website"])

    def test_the_criteria_that_apply_are_recorded_with_false(self):
        status, item = self.post({"id": self.ids[0], "human_label": "false",
                                   "human_criteria": ["website", "inactive"]})
        self.assertEqual((status, item["human_criteria"]), (200, ["inactive", "website"]))
        self.assertEqual(self.rows()[self.ids[0]]["human_criteria"], "inactive|website")
        for refused in ({"human_label": "true", "human_criteria": ["website"]},
                        {"human_label": "false", "human_criteria": ["popular"]},
                        {"human_label": "false", "human_criteria": "website"}):
            with self.subTest(refused=refused):
                self.assertEqual(self.post({"id": self.ids[0], **refused})[0], 400)
        self.assertEqual(self.rows()[self.ids[0]]["human_criteria"], "inactive|website")
        status, item = self.post({"id": self.ids[0], "human_label": "true"})
        self.assertEqual((status, self.rows()[self.ids[0]]["human_criteria"]), (200, ""))


class TestCheckRuns(_EvaluationCase):
    """A candidate model labels the check samples of several evaluations in several runs, each
    in a cache of labels of its own, and the record of the day's checks gets one row per run."""

    DEEPSEEK = ("deepseek/deepseek-v4.1-flash", "morph/fp8", "fp8",
                "deepseek-ai/DeepSeek-V4.1-Flash")
    # The label that each run gives each repository, and None for a repository that the run left
    # without a decision.
    LABELS = {1: {1: "true", 2: "true", 3: "false", 4: "true", 5: "true", 6: "false", 7: "false"},
              2: {1: "true", 2: "false", 3: "true", 4: "false", 5: "true", 6: "false", 7: None}}

    def setUp(self):
        super().setUp()
        self.first, self.second = self.tmp / "evaluation" / "a", self.tmp / "evaluation" / "b"
        for out, rows in ((self.first, [(1, DISAGREE, "true"), (2, DISAGREE, "false"),
                                        (3, DISAGREE, "unclear"), (4, AGREE, "true"),
                                        (5, AGREE, "true"), (61, AGREE, "true")]),
                          (self.second, [(6, DISAGREE, "false"), (7, AGREE, "false")])):
            out.mkdir(parents=True)
            with open(out / evaluate.CHECK_SAMPLE_CSV, "w", newline="") as f:
                writer = csv.writer(f, lineterminator="\n")
                writer.writerow(evaluate.CHECK_SAMPLE_COLUMNS)
                writer.writerows([i, f"o/r{i}", group, expected, ""]
                                 for i, group, expected in rows)
        self.prices = self.tmp / "model_prices.csv"
        self.prices.write_text("model,service_tier,uncached_input_usd_per_million,"
                               "cached_input_usd_per_million,cache_write_usd_per_million,"
                               "output_usd_per_million,doc_url,doc_verified,notes\n"
                               "deepseek/deepseek-v4.1-flash,morph/fp8,1,0.5,1,10,"
                               "https://openrouter.ai/x,2026-10-30,\n")
        self.calls = []

    def label(self, arguments):
        """A fake run of the classification that gives each repository in `--repository-list`
        its label in LABELS. In run 1, one of the two responses for id 1 failed the response
        format, and one tool round read two linked pages for id 2. A run that makes decisions
        uses 1M input tokens and 0.1M output tokens at the pinned endpoint. When the cache holds
        a finished run under the same model and parameters, which name the summary model, the
        fake run reuses every decision of that run, as the classification does, and makes no
        decision and no request to the model."""
        options = dict(zip(arguments[::2], arguments[1::2]))
        self.calls.append(arguments)
        cache = Path(options["--cache-dir"])
        number = int(cache.name.split("-")[1])
        with open(options["--repository-list"], newline="") as f:
            ids = [int(row["id"]) for row in csv.DictReader(f)]
        parameters = label_cache.record("parameters", {"prompt": "p", "classifying_model": {
            "model": options["--model"], "provider": options["--provider"]},
            "summary": options.get("--summary-model", "")}, cache)
        rid = f"2026-10-30T1{len(self.calls):05d}Z"
        earlier = [row for row in (label_cache.read_index(cache)
                                   if (cache / label_cache.INDEX_NAME).is_file() else [])
                   if row["parameters"] == parameters and row["outcome"] == "finished"]
        if earlier:
            decided = earlier[-1]["run_id"]
            labels_sha = label_cache.write_labels(cache, rid, [
                {"id": i, "repo_name": f"o/r{i}",
                 "decided_in": decided if self.LABELS[number][i] else "",
                 "outcome": label_cache.LABELED if self.LABELS[number][i]
                 else label_cache.UNDECIDED,
                 "label": self.LABELS[number][i] or "", "reason": ""} for i in ids])
            label_cache.put_index_row({"run_id": rid, "outcome": "finished",
                                       "model": options["--model"], "parameters": parameters,
                                       "scope": "repository_list",
                                       "labeled": sum(1 for i in ids if self.LABELS[number][i]),
                                       "reused": len(ids), "decisions": 0,
                                       "decisions_sha256": "", "labels_sha256": labels_sha},
                                      cache)
            return 0
        decisions = [{**_label(i, self.LABELS[number][i], options["--model"],
                               parameters=parameters),
                      **({"higher_temperature": {"temperature": 1.0, "reason": "output_limit"}}
                         if (number, i) in self.HOTTER else {}),
                      "usage": {"responses": 2 if (number, i) == (1, 1) else 1,
                                "failed_responses": 1 if (number, i) == (1, 1) else 0,
                                "tool_rounds": 1 if (number, i) == (1, 2) else 0,
                                "linked_pages": 2 if (number, i) == (1, 2) else 0}}
                     for i in ids if self.LABELS[number][i]]
        count, sha = label_cache.write_decisions(cache, rid, decisions)
        labels_sha = label_cache.write_labels(cache, rid, [
            {"id": i, "repo_name": f"o/r{i}", "decided_in": rid if self.LABELS[number][i] else "",
             "outcome": label_cache.LABELED if self.LABELS[number][i] else label_cache.UNDECIDED,
             "label": self.LABELS[number][i] or "", "reason": ""} for i in ids])
        label_cache.put_index_row({"run_id": rid, "outcome": "finished",
                                   "model": options["--model"], "parameters": parameters,
                                   "scope": "repository_list", "labeled": len(decisions),
                                   "decisions": count, "decisions_sha256": sha,
                                   "labels_sha256": labels_sha}, cache)
        label_cache.add_tokens(cache, rid, {
            ("classifying_model", options["--model"], "morph/fp8", "morph/fp8"):
                {"requests": 8, "input_tokens": 1_000_000, "cached_tokens": 0,
                 "cache_write_tokens": 0, "output_tokens": 100_000, "reasoning_tokens": 0}})
        return 0

    def check(self, *options):
        model, provider, quantization, weights = self.DEEPSEEK
        costs = importlib.import_module("costs")
        with mock.patch.object(evaluate, "label_check", self.label), \
                mock.patch.object(costs, "PRICES", self.prices):
            return self.main("check", str(self.first), str(self.second), "--runs", "2",
                             "--model", model, "--provider", provider, "--quantization",
                             quantization, "--hugging-face-id", weights,
                             "--sampling-frame", str(self.sampling_frame),
                             "--check-dir", str(self.tmp / "checks"),
                             "--check-date", "2026-10-30", *options)

    # The decisions, by run and id, that the fake classification makes at the higher
    # temperature.
    HOTTER: set = set()

    def test_each_row_names_the_higher_temperature_and_the_labels_made_at_it(self):
        """A label made at the higher temperature after a failed response counts as the
        model's label in the check. Each row names the higher temperature of its runs and counts
        the labels made at it, so that two rows of one variant show when their labels come from
        requests at other temperatures. Runs at the endpoint's defaults, or at a temperature
        that is not below the higher one, have none."""
        self.HOTTER = {(1, 3)}
        code, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual([(row["higher_temperature"], row["higher_temperature_labels"])
                          for row in self.record(self.first)], [("1.0", "1"), ("1.0", "0")])
        for options, variant in ((("--endpoint-default-temperature",),
                                  "high-endpoint-default-temperature"),
                                 (("--temperature", "1.0"), "high-temperature-1.0")):
            with self.subTest(options=options):
                code, err = self.check(*options)
                self.assertEqual(code, 0, err)
                self.assertEqual([row["higher_temperature"] for row in self.record(self.first)
                                  if row["variant"].endswith(variant)], ["", ""])

    def test_the_runs_label_without_the_second_model(self):
        """A check measures one model. A repository that the model fails stays without a
        label in the check rather than getting the second model's label, so every run passes
        --no-second-model to the classification."""
        code, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 2)
        for call in self.calls:
            self.assertTrue(evaluate.CLASSIFY.build_parser().parse_args(call).no_second_model)

    def test_the_runs_label_without_a_backup_provider(self):
        """A check measures one model on one endpoint. A request that the endpoint fails goes to
        no backup provider, whose labels and summaries the record would count as those of the
        endpoint, so every run passes --no-backup-provider to the classification."""
        code, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 2)
        for call in self.calls:
            self.assertTrue(evaluate.CLASSIFY.build_parser().parse_args(call).no_backup_provider)

    def test_the_runs_pass_the_openai_service_tier_on_to_the_classification(self):
        """A check can label on OpenAI's default tier instead of flex, as the pilot of
        2026-10-30 does, and the runs pass the tier on to the classification."""
        code, err = self.check("--openai-service-tier", "default")
        self.assertEqual(code, 0, err)
        for call in self.calls:
            self.assertEqual(dict(zip(call[::2], call[1::2]))["--openai-service-tier"], "default")

    def test_the_runs_pass_a_temperature_and_a_seed_on_to_the_classification(self):
        """A check without `--temperature`, `--top-p` and `--seed` labels at the settings of the
        model, as the classification does, and a check that names them passes them on to every
        run, whose variant names them. The runs label at the reasoning effort of the model's
        settings unless the check names one."""
        code, err = self.check()
        self.assertEqual(code, 0, err)
        for call in self.calls:
            for option in ("--temperature", "--top-p", "--seed",
                           "--endpoint-default-temperature"):
                self.assertNotIn(option, call)
            self.assertEqual(dict(zip(call[::2], call[1::2]))["--reasoning-effort"], "high")
        self.calls.clear()
        code, err = self.check("--temperature", "0.5", "--top-p", "0.9", "--seed", "3",
                               "--reasoning-effort", "low")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 2)
        for call in self.calls:
            options = dict(zip(call[::2], call[1::2]))
            self.assertEqual((options["--temperature"], options["--top-p"], options["--seed"],
                              options["--reasoning-effort"]), ("0.5", "0.9", "3", "low"))
            self.assertIn("low-temperature-0.5-top_p-0.9-seed-3", options["--cache-dir"])

    def test_a_check_at_the_endpoints_default_temperature_runs_without_either(self):
        """With `--endpoint-default-temperature`, each run gets the option of the same name of
        the classification, whose requests and classification parameters then have neither a
        temperature, a top_p nor a seed, and the variant and the record name such runs."""
        classify, real, settings = evaluate.CLASSIFY, self.label, []

        def label(arguments):
            settings.append(classify.run_settings(classify.build_parser().parse_args(arguments)))
            return real(arguments)
        self.label = label
        code, err = self.check("--endpoint-default-temperature")
        self.assertEqual(code, 0, err)
        self.assertEqual([setting.sampled() for setting in settings], [{}, {}])
        self.assertEqual([setting.higher() for setting in settings], [None, None])
        variant = "deepseek_deepseek-v4.1-flash-morph_fp8-high-endpoint-default-temperature"
        for call in self.calls:
            self.assertIn("--endpoint-default-temperature", call)
            for option in ("--temperature", "--top-p", "--seed"):
                self.assertNotIn(option, call)
            self.assertEqual(Path(dict(zip(call[::2], call[1::2]))["--cache-dir"]).parent.name,
                             variant)
        self.assertEqual({row["variant"] for row in self.record(self.first)}, {variant})

    def test_the_endpoints_default_temperature_is_refused_with_a_temperature_or_openai(self):
        """Runs at the endpoint's defaults send no temperature, top_p or seed, so the check
        refuses each of these options with them. An OpenAI model's requests send none of them in
        any run, and the check refuses the option for it before a run starts."""
        for options in (("--temperature", "0.2"), ("--top-p", "0.95"), ("--seed", "7")):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                self.check("--endpoint-default-temperature", *options)
        with mock.patch.object(evaluate, "label_check", self.label):
            code, err = self.main("check", str(self.first), "--runs", "1", "--model", LUNA,
                                  "--endpoint-default-temperature",
                                  "--sampling-frame", str(self.sampling_frame),
                                  "--check-dir", str(self.tmp / "checks"),
                                  "--check-date", "2026-10-30")
        self.assertEqual(code, 1, err)
        self.assertIn("applies only to an OpenRouter model", err)
        self.assertEqual(self.calls, [])

    def test_a_pinned_model_without_its_endpoint_is_recorded_on_its_pinned_endpoint(self):
        """A check that names DeepSeek V4.1 Flash without --provider labels on its pinned
        endpoint (`endpoints.PINNED`), as the classification does. The runs get the options of
        that endpoint, and the variant and the record name it, as for a check that names the
        endpoint, so that the two checks are one variant."""
        model, provider, quantization, weights = self.DEEPSEEK
        costs = importlib.import_module("costs")
        with mock.patch.object(evaluate, "label_check", self.label), \
                mock.patch.object(costs, "PRICES", self.prices):
            code, err = self.main("check", str(self.first), "--runs", "1", "--model", model,
                                  "--sampling-frame", str(self.sampling_frame),
                                  "--check-dir", str(self.tmp / "checks"),
                                  "--check-date", "2026-10-30")
        self.assertEqual(code, 0, err)
        options = dict(zip(self.calls[0][::2], self.calls[0][1::2]))
        self.assertEqual((options["--provider"], options["--quantization"],
                          options["--hugging-face-id"]), (provider, quantization, weights))
        self.assertEqual([(row["variant"], row["provider"], row["quantization"])
                          for row in self.record(self.first)],
                         [("deepseek_deepseek-v4.1-flash-morph_fp8-high", provider,
                           quantization)])

    def record(self, out):
        with open(out / "prompt_check_2026-10-30.csv", newline="") as f:
            reader = csv.DictReader(f)
            self.assertEqual(reader.fieldnames, evaluate.PROMPT_CHECK_COLUMNS)
            return list(reader)

    def test_each_run_labels_in_a_cache_of_its_own_and_gets_a_row_per_evaluation(self):
        code, err = self.check()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 2)
        caches = [dict(zip(call[::2], call[1::2]))["--cache-dir"] for call in self.calls]
        self.assertEqual(len(set(caches)), 2)
        options = dict(zip(self.calls[0][::2], self.calls[0][1::2]))
        self.assertEqual((options["--provider"], options["--quantization"],
                          options["--hugging-face-id"], options["--reasoning-effort"]),
                         self.DEEPSEEK[1:] + ("high",))
        # The repository outside the sampling frame is labeled in no run.
        with open(options["--repository-list"], newline="") as f:
            self.assertEqual([int(row["id"]) for row in csv.DictReader(f)], list(range(1, 8)))
        first = self.record(self.first)
        self.assertEqual([{key: row[key] for key in (
            "disagreements", "disagreements_as_reviewed", "agreements", "agreements_as_reviewed",
            "differences", "differing_labels", "varying_repositories")} for row in first],
            [{"disagreements": "2", "disagreements_as_reviewed": "1", "agreements": "2",
              "agreements_as_reviewed": "2", "differences": "1", "differing_labels": "o/r2=true",
              "varying_repositories": "3"},
             {"disagreements": "2", "disagreements_as_reviewed": "2", "agreements": "2",
              "agreements_as_reviewed": "1", "differences": "1", "differing_labels": "o/r4=false",
              "varying_repositories": "3"}])
        second = self.record(self.second)
        self.assertEqual([(row["agreements_as_reviewed"], row["differing_labels"],
                           row["varying_repositories"], row["undecided"]) for row in second],
                         [("1", "", "1", "0"), ("0", "o/r7=undecided", "1", "1")])

    def test_each_row_has_the_failed_responses_the_tool_use_and_the_cost_per_repository(self):
        """Run 1 got 8 responses, one of which failed the response format. Each run paid $2 for
        1M input tokens at $1 and 0.1M output tokens at $10 per million. Run 1 labeled 7
        repositories and run 2 labeled 6."""
        code, err = self.check("--variant", "deepseek-medium", "--texts", "the candidate")
        self.assertEqual(code, 0, err)
        rows = self.record(self.first)
        self.assertEqual([(row["variant"], row["texts"], row["model"], row["provider"],
                           row["quantization"], row["reasoning_effort"]) for row in rows],
                         [("deepseek-medium", "the candidate", *self.DEEPSEEK[:3], "high")] * 2)
        self.assertEqual([(row["responses"], row["failed_responses"], row["failed_share"])
                          for row in rows], [("8", "1", "0.1250"), ("6", "0", "0.0000")])
        self.assertEqual([(row["repositories_labeled"], row["cost_usd"],
                           row["cost_per_repository_usd"]) for row in rows],
                         [("7", "2.0000", "0.285714"), ("6", "2.0000", "0.333333")])
        # Run 1 read two linked pages in one tool round for o/r2, and run 2 read none.
        self.assertEqual([(row["repositories_with_tool_rounds"], row["linked_pages"])
                          for row in rows], [("1", "2"), ("0", "0")])
        self.assertEqual(rows[0]["labels"], "checks/2026-10-30/deepseek-medium/run-1")

    def test_a_repeated_check_labels_nothing_again_and_replaces_its_rows(self):
        """The classification starts a run that reuses every decision under the same
        parameters, makes no decision and costs nothing. The rows keep the responses, the tool
        use and the cost of the runs that made the decisions."""
        self.assertEqual(self.check()[0], 0)
        columns = ("responses", "failed_responses", "repositories_with_tool_rounds",
                   "linked_pages", "cost_usd", "differences", "undecided")
        first = [{key: row[key] for key in columns} for row in self.record(self.first)]
        code, err = self.check()
        self.assertEqual(code, 0, err)
        rows = self.record(self.first)
        self.assertEqual([{key: row[key] for key in columns} for row in rows], first)
        self.assertIn(f"Run 1 of deepseek_deepseek-v4.1-flash-morph_fp8-high decided no "
                      f"repository, because run {rows[0]['run_id']} reused the decisions of the "
                      f"earlier runs under the same model and classification parameters", err)

    def test_a_repeated_check_under_other_parameters_labels_again(self):
        """The variant's name leaves out the summary model and the code. A check of the same
        date and variant under another summary model labels each run again, and the record
        names the new runs, not the runs that finished under the earlier parameters."""
        self.assertEqual(self.check()[0], 0)
        first = [row["run_id"] for row in self.record(self.first)]
        code, err = self.check("--summary-model", "gpt-6.1-sol")
        self.assertEqual(code, 0, err)
        rows = self.record(self.first)
        again = [row["run_id"] for row in rows]
        self.assertEqual(len(again), 2)
        self.assertFalse(set(first) & set(again))
        self.assertNotIn("reuses run", err)
        # The responses of the decisions of the new runs alone, not those of the earlier runs in
        # the same caches.
        self.assertEqual([row["responses"] for row in rows], ["8", "6"])

    def test_a_label_carried_to_changed_code_counts_its_decision_once(self):
        """A change of code that the parameters do not record, such as the purpose check, makes
        the next run carry every label: It writes each decision again with the usage of the
        decision that it carries and sends no request. The record counts each decision once and
        the cost of the run that paid for it, also when a decision was carried twice."""
        cache = self.tmp / "carried"
        model, provider = self.DEEPSEEK[0], self.DEEPSEEK[1]
        parameters = label_cache.record("parameters", {"prompt": "p"}, cache)
        usage = {"responses": 1, "failed_responses": 0, "tool_rounds": 1, "linked_pages": 1}
        first = [{**_label(i, "true", model, parameters=parameters), "usage": dict(usage)}
                 for i in (1, 2)]
        carried = [{**decision, "carried_from": {"run": "2026-10-30T100001Z"}}
                   for decision in first]
        # A decision carried a second time names the run that carried it first, which sent no
        # request.
        again = [{**decision, "carried_from": {"run": "2026-10-30T100002Z"}}
                 for decision in first]
        for rid, decisions in (("2026-10-30T100001Z", first), ("2026-10-30T100002Z", carried),
                               ("2026-10-30T100003Z", again)):
            count, sha = label_cache.write_decisions(cache, rid, decisions)
            labels_sha = label_cache.write_labels(cache, rid, [
                {"id": i, "repo_name": f"o/r{i}", "decided_in": rid,
                 "outcome": label_cache.LABELED, "label": "true", "reason": ""} for i in (1, 2)])
            label_cache.put_index_row({"run_id": rid, "outcome": "finished", "model": model,
                                       "parameters": parameters, "scope": "repository_list",
                                       "labeled": 2, "decisions": count, "decisions_sha256": sha,
                                       "labels_sha256": labels_sha}, cache)
        label_cache.add_tokens(cache, "2026-10-30T100001Z", {
            ("classifying_model", model, provider, provider):
                {"requests": 2, "input_tokens": 1_000_000, "cached_tokens": 0,
                 "cache_write_tokens": 0, "output_tokens": 100_000, "reasoning_tokens": 0}})
        costs = importlib.import_module("costs")
        with mock.patch.object(costs, "PRICES", self.prices):
            record = evaluate.run_record(cache, evaluate.finished_run(cache, model))
        self.assertEqual((record["responses"], record["tool_rounds"], record["linked_pages"]),
                         (2, 2, 2))
        self.assertEqual(record["cost"], costs.Decimal("2"))

    def test_a_run_that_does_not_finish_leaves_no_record_and_logs_how_to_resume_the_check(self):
        def stopped(arguments):
            self.calls.append(arguments)
            return 1
        costs = importlib.import_module("costs")
        with mock.patch.object(evaluate, "label_check", stopped), \
                mock.patch.object(costs, "PRICES", self.prices):
            code, err = self.main("check", str(self.first), "--runs", "1", "--model",
                                  self.DEEPSEEK[0], "--sampling-frame", str(self.sampling_frame),
                                  "--check-dir", str(self.tmp / "checks"), "--check-date",
                                  "2026-10-30", "--provider", self.DEEPSEEK[1],
                                  "--quantization", self.DEEPSEEK[2], "--hugging-face-id",
                                  self.DEEPSEEK[3])
        self.assertEqual(code, 1)
        self.assertIn("Run the check again with --check-date 2026-10-30 to resume it", err)
        self.assertFalse((self.first / "prompt_check_2026-10-30.csv").exists())

    def test_a_check_waits_for_the_lock_of_the_record_and_keeps_the_rows_of_another_check(self):
        """Two checks of the same date write the same record when their runs end. While one holds
        the lock of the record, the other waits, and then reads the record with the first one's
        row. Without the lock, the second would write the record that it read before the first
        one's row was in it."""
        path = self.first / "prompt_check_2026-10-30.csv"
        header = ",".join(evaluate.PROMPT_CHECK_COLUMNS) + "\n"
        path.write_text(header)
        other = "gpt-6-luna-medium" + "," * (len(evaluate.PROMPT_CHECK_COLUMNS) - 1) + "\n"
        outcome = {}
        with open(path.with_name(path.name + ".lock"), "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            checking = threading.Thread(target=lambda: outcome.update(result=self.check()))
            checking.start()
            deadline = time.monotonic() + 30
            while len(self.calls) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.5)
            self.assertEqual(path.read_text(), header)
            path.write_text(header + other)
            fcntl.flock(lock, fcntl.LOCK_UN)
        checking.join(30)
        code, err = outcome["result"]
        self.assertEqual(code, 0, err)
        self.assertEqual([row["variant"] for row in self.record(self.first)],
                         ["gpt-6-luna-medium", "deepseek_deepseek-v4.1-flash-morph_fp8-high",
                          "deepseek_deepseek-v4.1-flash-morph_fp8-high"])

    def test_a_record_that_cannot_be_replaced_stays_as_it_was(self):
        """The new record replaces the old one in one step. A failed replacement keeps the rows
        of the other variants and leaves no temporary file."""
        path = self.first / "prompt_check_2026-10-30.csv"
        record = (",".join(evaluate.PROMPT_CHECK_COLUMNS) + "\n" + "gpt-6-luna-medium"
                  + "," * (len(evaluate.PROMPT_CHECK_COLUMNS) - 1) + "\n")
        path.write_text(record)
        replace = os.replace

        def failing(source, destination):
            if Path(destination) == path:
                raise OSError("disk full")
            replace(source, destination)
        with mock.patch.object(os, "replace", failing), self.assertRaises(OSError):
            self.check()
        self.assertEqual(path.read_text(), record)
        self.assertEqual(sorted(p.name for p in self.first.iterdir()
                                if p.name.startswith("prompt_check")),
                         ["prompt_check_2026-10-30.csv", "prompt_check_2026-10-30.csv.lock"])

    def test_a_record_of_the_day_with_other_columns_is_left_alone(self):
        (self.first / "prompt_check_2026-10-30.csv").write_text("variant,run_id\nold,r\n")
        code, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("has other columns than the record of a check", err)
        self.assertIn("Give the record these columns and run the check again with --check-date "
                      "2026-10-30, whose runs reuse their decisions without a request to the "
                      "model.\n", err)
        self.assertEqual((self.first / "prompt_check_2026-10-30.csv").read_text(),
                         "variant,run_id\nold,r\n")

    def test_a_record_written_before_the_last_columns_takes_the_rows_of_the_runs(self):
        """A record of the day with the 25 columns that the code wrote before it recorded the
        higher temperature keeps its rows, gets the two columns of the higher temperature,
        empty in its earlier rows, and takes the rows of the runs."""
        earlier = ["variant", "texts", "run_id", "labels", "parameters", "disagreements",
                   "disagreements_as_reviewed", "agreements", "agreements_as_reviewed",
                   "differences", "differing_labels", "model", "provider", "quantization",
                   "reasoning_effort", "varying_repositories", "responses", "failed_responses",
                   "failed_share", "undecided", "repositories_labeled",
                   "repositories_with_tool_rounds", "linked_pages", "cost_usd",
                   "cost_per_repository_usd"]
        row = ["gpt-6-luna-medium", "the code", "2026-10-30T000000Z", "checks/x/run-1", "abc",
               "3", "3", "2", "2", "1", "1", "gpt-6-luna", "flex", "", "medium", "1", "6", "0",
               "0.0000", "0", "6", "1", "2", "0.3100", "0.051667"]
        path = self.first / "prompt_check_2026-10-30.csv"
        path.write_text(",".join(earlier) + "\n" + ",".join(row) + "\n")
        code, err = self.check()
        self.assertEqual(code, 0, err)
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            columns, rows = reader.fieldnames, list(reader)
        self.assertEqual(columns, earlier + ["higher_temperature", "higher_temperature_labels"])
        self.assertEqual(rows[0], {**dict(zip(earlier, row)), "higher_temperature": "",
                                   "higher_temperature_labels": ""})
        self.assertEqual([line["variant"] for line in rows[1:]],
                         ["deepseek_deepseek-v4.1-flash-morph_fp8-high"] * 2)


class TestEndpointDefaultTemperature(_RunCase):
    """A run of the classification with `--endpoint-default-temperature`, the option that each
    run of such a check gets, sends the requests of an OpenRouter model neither a temperature, a
    top_p nor a seed and records none of them among its classification parameters. A run
    without the option labels at the settings of the model's record."""

    def labeled(self, **options) -> tuple[dict, dict]:
        """The one request of a run over one repository with DeepSeek V4.1 Flash on its pinned
        endpoint, and the block `classifying_model` of the parameters that the run recorded."""
        model = _Model(_always_true)
        code, err = self.run_over(_repositories("o/a"), _GitHub({"o/a": (ENGLISH, "s1")}),
                                  model, **DEEPSEEK, **options)
        self.assertEqual(code, 0, err)
        [request] = model.requests
        parameters = label_cache.recorded("parameters", self.index()[-1]["parameters"],
                                          self.cache)
        return request, parameters["classifying_model"]

    def test_the_requests_and_the_parameters_have_neither_a_temperature_nor_a_seed(self):
        request, recorded = self.labeled(endpoint_default_temperature=True)
        for name in ("temperature", "top_p"):
            self.assertNotIn(name, request)
            self.assertNotIn(name, recorded)
        self.assertNotIn("seed", request["extra_body"])
        self.assertNotIn("seed", recorded)
        self.assertEqual(request["extra_body"]["provider"]["order"], ["morph/fp8"])
        self.setUp()
        request, recorded = self.labeled()
        self.assertEqual((request["temperature"], request["top_p"],
                          request["extra_body"]["seed"]), (0.6, 0.95, 7))
        self.assertEqual((recorded["temperature"], recorded["top_p"], recorded["seed"]),
                         (0.6, 0.95, 7))


class TestCheck(_EvaluationCase):
    """New classification parameters checked against a reviewed evaluation."""

    def setUp(self):
        super().setUp()
        self.run_of("2026-10-06T080000Z", LUNA, [_label(i, "true", LUNA) for i in range(1, 21)])
        self.out = self.draw(seed="1")
        with open(self.out / evaluate.SAMPLE_CSV, newline="") as f:
            self.ids = [int(row["id"]) for row in csv.DictReader(f)]
        self.disagreeing = self.ids[:3]
        self.run_of("2026-10-07T080000Z", SOL, [
            _label(i, "false" if i in self.disagreeing else "true", SOL,
                   parameters=_second(SOL)) for i in self.ids], _second(SOL))
        code, err = self.main("compare", str(self.out), "--second-model", SOL)
        self.assertEqual(code, 0, err)

    def review(self, labels):
        rows = evaluate.read_disagreements(self.out)
        for row in rows:
            row["human_label"] = labels.get(int(row["id"]), "")
        evaluate.write_disagreements(self.out, rows)

    def sample(self):
        with open(self.out / evaluate.CHECK_SAMPLE_CSV, newline="") as f:
            return list(csv.DictReader(f))

    def test_every_disagreement_and_a_seeded_sample_of_the_agreements_are_chosen(self):
        a, b, c = self.disagreeing
        self.review({a: "false", b: "true", c: "unclear"})
        (self.out / evaluate.FORKS_JSON).write_text(json.dumps({"forks": [
            {"repo_name": f"o/r{c}", "independent": False, "reasons": ["tracks its upstream"]}]}))
        code, err = self.main("draw-check", str(self.out), "--agreements", "4", "--seed", "3")
        self.assertEqual(code, 0, err)
        rows = self.sample()
        self.assertEqual([(int(r["id"]), r["group"], r["expected_label"], r["expected_criteria"])
                          for r in rows[:3]],
                         [(a, "disagreement", "false", ""), (b, "disagreement", "true", ""),
                          (c, "disagreement", "false", "fork")])
        agreements = [r for r in rows if r["group"] == "agreement"]
        self.assertEqual(len(agreements), 4)
        self.assertTrue(all(r["expected_label"] == "true" and int(r["id"]) not in self.disagreeing
                            for r in agreements))
        record = json.loads((self.out / evaluate.CHECK_SAMPLE_JSON).read_text())
        self.assertEqual((record["seed"], record["agreed"], record["agreements"]),
                         (3, len(self.ids) - 3, 4))
        self.main("draw-check", str(self.out), "--agreements", "4", "--seed", "3")
        self.assertEqual(self.sample(), rows)

    def test_a_reviewed_agreement_is_expected_with_the_reviewers_label(self):
        """A label that both models agreed on can contradict the criteria that the reviewer has
        set since, such as maintenance mode for inactive."""
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "4", "--seed", "3")
        reviewed = next(int(r["id"]) for r in self.sample() if r["group"] == "agreement")
        with open(self.out / evaluate.REVIEWED_AGREEMENTS_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=evaluate.REVIEWED_AGREEMENT_COLUMNS)
            writer.writeheader()
            writer.writerow({"id": reviewed, "repo_name": f"o/r{reviewed}", "human_label": "false",
                             "human_criteria": "inactive", "human_note": "maintenance mode"})
        code, err = self.main("draw-check", str(self.out), "--agreements", "4", "--seed", "3")
        self.assertEqual(code, 0, err)
        row = next(r for r in self.sample() if int(r["id"]) == reviewed)
        self.assertEqual((row["group"], row["expected_label"], row["expected_criteria"]),
                         ("agreement", "false", "inactive"))
        record = json.loads((self.out / evaluate.CHECK_SAMPLE_JSON).read_text())
        self.assertTrue(record["reviewed_agreements"].endswith(evaluate.REVIEWED_AGREEMENTS_CSV))

    def test_an_undecided_disagreement_or_too_few_agreements_are_refused(self):
        self.review({self.disagreeing[0]: "false"})
        code, err = self.main("draw-check", str(self.out), "--agreements", "2")
        self.assertEqual(code, 1)
        self.assertIn("undecided disagreements", err)
        self.review({i: "false" for i in self.disagreeing})
        code, err = self.main("draw-check", str(self.out), "--agreements", "999")
        self.assertEqual(code, 1)
        self.assertIn("fewer than the 999 requested", err)

    def test_each_models_new_labels_are_compared_with_the_labels_expected(self):
        a, b, c = self.disagreeing
        self.review({a: "false", b: "true", c: "unclear"})
        code, err = self.main("draw-check", str(self.out), "--agreements", "4", "--seed", "3")
        self.assertEqual(code, 0, err)
        rows = self.sample()
        agreed = [int(r["id"]) for r in rows if r["group"] == "agreement"]
        new = {a: "false", b: "false", c: "true", agreed[0]: "false"}
        for rid, model in (("2026-10-08T080000Z", LUNA), ("2026-10-08T090000Z", SOL)):
            decisions = [_label(int(r["id"]), new.get(int(r["id"]), "true"), model,
                                parameters=C2) for r in rows]
            decisions[0]["applied_criteria"] = ["website"]
            self.run_of(rid, model, decisions, parameters=C2)
        code, err = self.main("check", str(self.out), "--sampling-frame", str(self.sampling_frame), "--models", LUNA, SOL)
        self.assertEqual(code, 0, err)
        summary = json.loads((self.out / evaluate.CHECK_JSON).read_text())
        luna = summary["models"][LUNA]
        self.assertEqual(luna["parameters"], C2)
        self.assertEqual({k: luna["disagreement"][k] for k in ("repositories", "expected",
                                                               "as_expected")},
                         {"repositories": 3, "expected": 2, "as_expected": 1})
        self.assertEqual(luna["disagreement"]["changes"], {"true to false": 1,
                                                           "unclear to true": 1})
        self.assertEqual({k: luna["agreement"][k] for k in ("expected", "as_expected", "changes")},
                         {"expected": 4, "as_expected": 3, "changes": {"true to false": 1}})
        with open(self.out / evaluate.CHECK_CSV, newline="") as f:
            first = next(csv.DictReader(f))
        self.assertEqual((first[f"{LUNA}_label"], first[f"{LUNA}_criteria"],
                          first[f"{SOL}_label"]), ("false", "website", "false"))

    def test_a_repository_that_left_the_sampling_frame_is_left_out(self):
        """With a new retrieval, the sampling window moves, and some repositories of an
        evaluation fall out of the window. No run over the new sampling frame labels them
        again."""
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "2", "--seed", "3")
        rows = self.sample()
        gone = int(rows[0]["id"])
        frame = json.loads(self.sampling_frame.read_text())
        frame["items"] = [item for item in frame["items"] if item["id"] != gone]
        self.sampling_frame.write_text(json.dumps(frame))
        self.run_of("2026-10-08T080000Z", LUNA, [_label(int(r["id"]), "false", LUNA, parameters=C2)
                                                 for r in rows[1:]], parameters=C2)
        code, err = self.main("check", str(self.out), "--sampling-frame", str(self.sampling_frame),
                              "--models", LUNA)
        self.assertEqual(code, 0, err)
        self.assertIn(f"1 repositories in {self.out / evaluate.CHECK_SAMPLE_CSV}, such as "
                      f"{rows[0]['repo_name']}, are not in the sampling frame", err)
        summary = json.loads((self.out / evaluate.CHECK_JSON).read_text())
        self.assertEqual(summary["not_in_sampling_frame"], [rows[0]["repo_name"]])
        counted = sum(summary["models"][LUNA][group]["repositories"]
                      for group in ("disagreement", "agreement"))
        self.assertEqual(counted, len(rows) - 1)

    def test_a_model_whose_latest_run_is_the_evaluations_own_is_refused(self):
        """When the second model's latest run is its run over the evaluation sample, the check
        would show the evaluation's own labels as the labels of new classification
        parameters."""
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "2", "--seed", "3")
        code, err = self.main("check", str(self.out), "--sampling-frame",
                              str(self.sampling_frame), "--models", SOL)
        self.assertEqual(code, 1)
        self.assertIn(f"The latest finished run of {SOL} labeled under the evaluation's "
                      f"classification parameters {_second(SOL)[:12]}", err)
        self.assertFalse((self.out / evaluate.CHECK_JSON).exists())

    def test_a_check_of_no_repository_is_refused(self):
        """A sampling frame that contains none of the check sample, such as the sampling frame
        of the batch classification, which names GHS's ids, would otherwise make the check print
        0 of 0 and exit 0."""
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "2", "--seed", "3")
        ids = [int(r["id"]) for r in self.sample()]
        self.run_of("2026-10-08T080000Z", LUNA, [_label(i, "true", LUNA, parameters=C2)
                                                 for i in ids], parameters=C2)
        other = self.tmp / "other_frame.json"
        other.write_text(json.dumps({"_metadata": {}, "items": [{"id": 999, "name": "o/x"}]}))
        code, err = self.main("check", str(self.out), "--sampling-frame", str(other),
                              "--models", LUNA)
        self.assertEqual(code, 1)
        self.assertIn(f"No repository in {self.out / evaluate.CHECK_SAMPLE_CSV} is in the "
                      f"sampling frame {other}", err)
        self.assertFalse((self.out / evaluate.CHECK_JSON).exists())

    def test_a_model_without_a_label_for_every_repository_is_refused(self):
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "2", "--seed", "3")
        self.run_of("2026-10-08T080000Z", LUNA, [_label(self.disagreeing[0], "false", LUNA,
                                                        parameters=C2)], parameters=C2)
        code, err = self.main("check", str(self.out), "--sampling-frame", str(self.sampling_frame), "--models", LUNA)
        self.assertEqual(code, 1)
        self.assertIn(f"has no label under the classification parameters {C2[:12]}", err)
        self.assertFalse((self.out / evaluate.CHECK_JSON).exists())

    def test_a_label_of_the_second_model_counts_as_no_label_of_the_checked_model(self):
        """A run of gpt-6-luna labeled every repository of the check sample, one of them through
        its second model, whose label names gpt-6-luna as its `model`. The check counts that
        label as none of gpt-6-luna's and refuses the model, rather than crediting the second
        model's label to it. A label of the model at its higher temperature is its own."""
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "2", "--seed", "3")
        ids = [int(r["id"]) for r in self.sample()]
        decisions = [_label(i, "false", LUNA, parameters=C2) for i in ids]
        decisions[0]["second_model"] = {"model": "z-ai/glm-5.3", "reason": "output_limit"}
        decisions[1]["higher_temperature"] = {"temperature": 1.0, "reason": "output_limit"}
        self.run_of("2026-10-08T080000Z", LUNA, decisions, parameters=C2)
        code, err = self.main("check", str(self.out), "--sampling-frame",
                              str(self.sampling_frame), "--models", LUNA)
        self.assertEqual(code, 1)
        self.assertIn(f"has no label under the classification parameters {C2[:12]} for 1 of the "
                      f"{len(ids)} repositories, such as o/r{ids[0]}", err)
        del decisions[0]["second_model"]
        self.run_of("2026-10-09T080000Z", LUNA, decisions, parameters=C2)
        code, err = self.main("check", str(self.out), "--sampling-frame",
                              str(self.sampling_frame), "--models", LUNA)
        self.assertEqual(code, 0, err)


    def test_a_model_with_an_unfinished_latest_run_is_refused(self):
        """A check that skipped the latest run when dropped connections left it incomplete
        would compare the run before it instead."""
        self.review({i: "false" for i in self.disagreeing})
        self.main("draw-check", str(self.out), "--agreements", "2", "--seed", "3")
        ids = [int(r["id"]) for r in self.sample()]
        self.run_of("2026-10-08T080000Z", LUNA, [_label(i, "true", LUNA, parameters=C2)
                                                 for i in ids], parameters=C2)
        self.run_of("2026-10-09T080000Z", LUNA, [_label(ids[0], "false", LUNA, parameters=C3)],
                    parameters=C3, outcome="incomplete")
        code, err = self.main("check", str(self.out), "--sampling-frame", str(self.sampling_frame), "--models", LUNA)
        self.assertEqual(code, 1)
        self.assertIn("The latest run of gpt-6-luna, 2026-10-09T080000Z, is incomplete", err)
        self.assertFalse((self.out / evaluate.CHECK_JSON).exists())


@unittest.skipUnless(shutil.which("node"), "node is not on PATH")
class TestReviewPage(unittest.TestCase):
    """The page's `decide`, run in Node over a stand-in for the page."""

    def test_a_server_response_that_arrives_after_moving_on_stays_with_its_repository(self):
        """The reviewer can decide one repository and move to the next before the server's
        response arrives. Written into the repository shown at that point, the response would
        send the next decision under the first repository's id."""
        page = evaluate.REVIEW_PAGE.read_text(encoding="utf-8")
        decide = re.search(r"async function decide\(label, chosen\) \{.*?\n\}", page,
                           re.S).group()
        script = """
let items = [{id: 1, human_label: '', human_note: ''}, {id: 2, human_label: '', human_note: ''}];
let current = 0, release;
const sent = [];
const nodes = {note: {value: 'about the first'}, status: {textContent: ''}};
const document = {getElementById: id => nodes[id], querySelectorAll: () => []};
function renderList() {}
function fetch(url, options) {
  const body = JSON.parse(options.body);
  sent.push(body);
  return new Promise(resolve => { release = () => resolve({ok: true, json: async () => body}); });
}
""" + decide + """
(async () => {
  const first = decide('false');
  current = 1;
  release();
  await first;
  nodes.note.value = 'about the second';
  const second = decide('true');
  release();
  await second;
  console.log(JSON.stringify({sent, items}));
})();
"""
        run = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        result = json.loads(run.stdout)
        self.assertEqual([(s["id"], s["human_label"], s["human_note"]) for s in result["sent"]],
                         [(1, "false", "about the first"), (2, "true", "about the second")])
        self.assertEqual([(i["id"], i["human_label"]) for i in result["items"]],
                         [(1, "false"), (2, "true")])

    def test_the_panel_says_that_a_criterion_overrides_the_judgment_of_the_evidence(self):
        """A model can judge the evidence of software engineering practices `true` while a
        criterion makes the label `false`. The panel of the model on the review page says that
        the criterion decides the label, because the two statements side by side without a
        connection read as a contradiction. For a decision without the judgment, the panel shows
        only the criteria. The panel also shows the quoted purpose with its page and the model's
        count of the words of the README by language."""
        page = evaluate.REVIEW_PAGE.read_text(encoding="utf-8")
        script = page.split("<script>")[1].split("</script>")[0]
        functions = (script[script.index("function el("):script.index("function visible(")]
                     + script[script.index("function toolCalls("):
                              script.index("function rewriteLinks(")])
        script = """
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = ''; }
  append(child) { this.children.push(child); }
  setAttribute() {}
  addEventListener() {}
  get textContent() { return this.children.map(child => child.textContent).join(''); }
}
class Text extends Node {
  constructor(text) { super('#text'); this.text = text; }
  get textContent() { return this.text; }
}
const document = {createElement: tag => new Node(tag), createTextNode: text => new Text(text)};
""" + functions + """
const side = {model: 'gpt-6.1-sol', label: 'false', model_label: 'true',
              justification: 'It has tests and CI.', applied_criteria: ['no_source_code'],
              unclear_criteria: [], exclusions: {}, tools: []};
const notes = panel => panel.children.map(child => child.textContent)
  .filter(text => text.startsWith('Criteria that apply'));
const texts = panel => panel.children.map(child => child.textContent)
  .filter(text => text.startsWith('Quoted') || text.startsWith('Words of the README'));
console.log(JSON.stringify([notes(modelPanel(side)),
                            notes(modelPanel({...side, model_label: 'unclear'})),
                            notes(modelPanel({...side, model_label: ''})),
                            texts(modelPanel({...side, purpose_quote: 'A formatter',
                                              purpose_source: 'https://tool.example/',
                                              readme_mainly_english: 'false',
                                              readme_words_by_language: [
                                                {language: 'English', words: 73},
                                                {language: 'Chinese', words: 125}]}))]))
"""
        run = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(run.stdout), [
            ["Criteria that apply: no_source_code. The model judged the evidence of software "
             "engineering practices true, but a criterion that applies makes the label false."],
            ["Criteria that apply: no_source_code. The model judged the evidence of software "
             "engineering practices unclear, but a criterion that applies makes the label "
             "false."],
            ["Criteria that apply: no_source_code."],
            ['Quoted: "A formatter" from the purpose sentence of https://tool.example/',
             "Words of the README by language: English 73, Chinese 125, mainly English: "
             "false"]])


# The tracked cache of labels and the tracked records of the label evaluations.
TRACKED = Path(__file__).resolve().parents[1] / "data" / "classification"


class TestTheTrackedEvaluations(unittest.TestCase):
    """Since 2026-11-04, every reader matches a recorded digest of classification parameters by
    its key, the digest of the parameters without the provider of a model
    (`label_cache.ParameterKeys`). A record of the tracked cache that names no provider is its
    own key, so that the readers of the evaluations select the same labels as before. A record
    that names a provider is one that a run wrote before 2026-11-04, such as run
    `2026-11-03T002808Z` of the run that began on the NAS on 2026-11-03, and its key is the digest
    of the same parameters without the provider."""

    def test_every_digest_of_the_tracked_cache_and_evaluations_has_the_key_without_the_provider(self):
        cache = TRACKED / "labels"
        keys = label_cache.ParameterKeys(cache)
        index = label_cache.read_index(cache)
        named = {row["parameters"] for row in index if row["parameters"]}
        for path in sorted((TRACKED / "evaluation").glob("*/*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(record, dict):
                named |= {record[key] for key in ("parameters", "second_model_parameters",
                                                  "previous_parameters")
                          if isinstance(record.get(key), str) and record[key]}
        recorded = {path.stem for path in (cache / "parameters").glob("*.json")}
        self.assertTrue(named)
        self.assertLessEqual(named, recorded)
        for sha in sorted(recorded):
            with self.subTest(sha=sha):
                value = label_cache.recorded("parameters", sha, cache)
                without = label_cache.without_provider(value)
                if without == value:
                    self.assertEqual(keys(sha), sha)
                else:
                    self.assertEqual(keys(sha), label_cache.digest(without))
                    starts = [row["started_at"] for row in index if row["parameters"] == sha]
                    self.assertTrue(starts)
                    self.assertTrue(all(start < "2026-11-04" for start in starts), starts)

    def test_a_reviewed_label_of_the_removed_criterion_closed_to_contributions_is_true(self):
        """The reviewer labeled microsoft/Teams-AdaptiveCards-Mobile `false` in evaluation
        2026-10-07T021344Z with the note '"semi private repositories" should be excluded'. Its
        README says: "Adaptive Cards is now a semi private repo * Pull requests and issue
        creations will no longer be accepted and will be closed. * Source code will still be
        avilable to the public." That reading was the criterion closed_to_contributions, which
        the classification removed on 2026-10-31, because how a repository takes contributions
        excludes it from nothing (`classify-repos-spec.md`). No other criterion applies to the
        repository, and `check` expects the label that the current criteria give, `true`."""
        evaluation = TRACKED / "evaluation" / "2026-10-07T021344Z"
        for name, column in (("disagreements.csv", "human_label"),
                             ("check_sample.csv", "expected_label")):
            with self.subTest(name=name), open(evaluation / name, newline="",
                                               encoding="utf-8") as f:
                row = next(row for row in csv.DictReader(f)
                           if row["repo_name"] == "microsoft/Teams-AdaptiveCards-Mobile")
                self.assertEqual(row[column], "true")


if __name__ == "__main__":
    unittest.main()
