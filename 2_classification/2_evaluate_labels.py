#!/usr/bin/env python3
"""Evaluate the classification's labels against a second model, so that people review only the
repositories on which the two models disagree.

    draw      draws the evaluation sample and writes it as
              evaluation/<id>/evaluation_sample.csv, with the record of the draw in
              evaluation/<id>/evaluation_sample.json. The evaluation sample is a simple random
              sample of the repositories in the sampling frame that the reference model labeled
              in its latest finished run over the sampling frame, without the calibration
              sample and without the repositories that the second model labeled in that run,
              as large as an agreement of ±3 % at 95 % confidence needs
    compare   reads the labels that both models gave the evaluation sample under the
              evaluation's classification parameters from the cache of labels, those of the
              second model on its pinned endpoint with the settings of its requests, and writes
              evaluation/<id>/disagreements.csv, one row per repository labeled differently,
              with empty columns for the person who reviews it, and
              evaluation/<id>/agreement.json, which also records the classification parameters
              of the compared labels of the second model. A reviewer's decision already in
              disagreements.csv is kept
    review    serves a page on 127.0.0.1 that shows each disagreement with the README that
              both models read, its GitHub link and each model's label, criteria,
              justification and tool calls, and writes the reviewer's label, the criteria that
              apply and a note into disagreements.csv as the reviewer decides each disagreement
    draw-check chooses the repositories that check new classification parameters against a
              reviewed evaluation: every disagreement, with the reviewer's label as the label
              expected, and a seeded random sample of the repositories that both models agreed
              on, with the agreed label as the label expected. A fork that the fork heuristic
              found not independent (forks.json) is expected to be false. It writes
              evaluation/<id>/check_sample.csv and check_sample.json
    check     reads the labels that each named model gave the repositories in
              check_sample.csv under the classification parameters of its latest finished run,
              and writes evaluation/<id>/check.csv, one row per repository with the expected
              label and the label of each model, and check.json, which counts the labels of
              each model that match the expected label. A label that the second model of a run
              made counts as no label of the run's model. With --runs, it labels the check samples
              of the named evaluations with one model, endpoint and reasoning effort in that
              many runs, each into a cache of labels of its own under checks/, and writes one
              row per run to evaluation/<id>/prompt_check_<date>.csv: the counts of the labels
              as expected, the repositories labeled differently between the runs, the failed
              responses, the higher temperature and the labels made at it, and the cost per
              repository. A check measures one model, and its runs label without the second
              model (`--no-second-model` of 1_classify_repos.py). With
              --endpoint-default-temperature, the runs send neither a temperature, a top_p nor
              a seed, and the endpoint applies its own defaults

Between `draw` and `compare`, `1_classify_repos.py --model <second model> --repository-list
evaluation/<id>/evaluation_sample.csv` labels the evaluation sample with the second model, by
default GLM-5.3 on its pinned endpoint (`endpoints.SECOND_MODEL`), under the same classification
parameters and with the same README as the first model. A disagreement
is then a difference between the models alone.

The calibration sample, drawn on 2026-10-05, contains the repositories on which the classification's
reading rules were measured and set (the section "The reading rules" of 0_README.md). A repository
in both samples would test the labels on repositories that the rules were fitted to. The evaluation
sample therefore leaves out the calibration sample and every other list that `--exclude` names.

The size of the evaluation sample is n0 = z^2 p (1 - p) / e^2 with p = 0.5, the proportion that
needs the largest sample, corrected for the finite population N as n = n0 / (1 + (n0 - 1) / N), at
e = 0.03 and z = 1.96.

Usage:
  python3 2_evaluate_labels.py draw --reference-model deepseek/deepseek-v4.1-flash
  python3 2_evaluate_labels.py compare ../data/classification/evaluation/<id>
  python3 2_evaluate_labels.py review ../data/classification/evaluation/<id>
  python3 2_evaluate_labels.py draw-check ../data/classification/evaluation/<id> --agreements 200
  python3 2_evaluate_labels.py check ../data/classification/evaluation/<id> \
      --models deepseek/deepseek-v4.1-flash z-ai/glm-5.3
  python3 2_evaluate_labels.py check ../data/classification/evaluation/<a> ../data/classification/evaluation/<b> \
      --runs 5 --model deepseek/deepseek-v4.1-flash --provider morph/fp8 --quantization fp8 \
      --hugging-face-id deepseek-ai/DeepSeek-V4.1-Flash
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import importlib
import json
import math
import os
import random
import re
import sys
import tempfile
import threading
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import NormalDist
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import endpoints  # noqa: E402
import label_cache  # noqa: E402
from label_cache import LABELED, UNDECIDED  # noqa: E402

CLASSIFY = importlib.import_module("1_classify_repos")
CRITERIA = CLASSIFY.CRITERIA

from agentcontext import CLASSIFICATION_DIR, SAMPLING_DIR, credentials, logs  # noqa: E402

COMPONENT = "evaluate_labels"
EVALUATION_DIR = CLASSIFICATION_DIR / "evaluation"
SAMPLE_CSV, SAMPLE_JSON = "evaluation_sample.csv", "evaluation_sample.json"
CHECK_SAMPLE_CSV, CHECK_SAMPLE_JSON = "check_sample.csv", "check_sample.json"
CHECK_CSV, CHECK_JSON = "check.csv", "check.json"
FORKS_JSON = "forks.json"
# Agreed repositories that a person reviewed. The reviewer's label replaces the agreed label in the
# check, because a label that both models agreed on under an earlier prompt can contradict the
# criteria that the reviewer set since, such as maintenance mode for inactive.
REVIEWED_AGREEMENTS_CSV = "reviewed_agreements.csv"
REVIEWED_AGREEMENT_COLUMNS = ["id", "repo_name", "human_label", "human_criteria", "human_note"]
CHECK_SAMPLE_COLUMNS = ["id", "repo_name", "group", "expected_label", "expected_criteria"]
DISAGREEMENT, AGREEMENT = "disagreement", "agreement"
SAMPLING_FRAME = SAMPLING_DIR / "ghs_filtered.json"
CALIBRATION_SAMPLE = CLASSIFICATION_DIR / "calibration" / "calibration_sample.csv"
REVIEW_PAGE = HERE / "review_disagreements.html"
REVIEW_PORT = 8765
MARGIN = 0.03
CONFIDENCE = 0.95
# The proportion that needs the largest sample, which the sample size assumes because the
# share of each label is not known before the labels are.
WORST_CASE_PROPORTION = 0.5
LABELS = ("true", "false", "unclear")
# The criteria columns contain the names of the criteria that applied to a repository, joined with
# "|": for each model as the classification derived them, and for the reviewer.
DISAGREEMENT_COLUMNS = ["id", "repo_name", "github_url", "readme_path", "readme_sha",
                        "reference_label", "reference_criteria", "reference_justification",
                        "reference_tools", "second_label", "second_criteria",
                        "second_justification", "second_tools",
                        "human_label", "human_criteria", "human_note"]


def sample_size(population: int, margin: float = MARGIN, confidence: float = CONFIDENCE) -> int:
    """The number of repositories that a simple random sample needs for `margin` at
    `confidence`, worst case, corrected for the finite population."""
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    n0 = z * z * WORST_CASE_PROPORTION * (1 - WORST_CASE_PROPORTION) / (margin * margin)
    return min(population, math.ceil(n0 / (1 + (n0 - 1) / population)))


def sampling_frame_repositories(path: Path) -> tuple[dict, str]:
    """The names of the repositories in the sampling frame by GitHub id, and the retrieval that
    it was filtered from."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    retrieval = ((data.get("_metadata") or {}).get("input_source") or {}).get("retrieval_id", "")
    return {int(item["id"]): item["name"] for item in data["items"]}, retrieval


def latest_labels(cache_dir: Path, model: str) -> tuple[dict, str]:
    """The latest label per GitHub id that `model` gave under the classification parameters of
    its latest finished run, and the SHA-256 of those parameters."""
    runs = [row for row in label_cache.read_index(cache_dir)
            if row["model"] == model and row["outcome"] == "finished"]
    if not runs:
        raise SystemExit(f"The cache of labels has no finished run of {model}")
    parameters = runs[-1]["parameters"]
    return labels_under(cache_dir, model, parameters), parameters


def sampling_frame_run(cache_dir: Path, model: str) -> dict:
    """The index row of the latest finished run of `model` over the whole sampling frame, with
    the outcome of each repository in its labels file. The latest finished run of any scope can
    be a check over part of the sampling frame, which the draw skips."""
    runs = [row for row in label_cache.read_index(cache_dir)
            if row["model"] == model and row["outcome"] == "finished"
            and row["scope"] == label_cache.SAMPLING_FRAME_SCOPE and row["labels_sha256"]]
    if not runs:
        raise SystemExit(f"The cache of labels has no finished run of {model} over a sampling "
                         f"frame")
    return runs[-1]


def labeled_in(cache_dir: Path, run: dict) -> set:
    """The GitHub ids of the repositories that `run` labeled, from its labels file. A label of
    an earlier run under the same classification parameters does not count for a repository
    that the run excluded, found gone or left without a decision, because the second model's run
    over the evaluation sample would give that repository no label either, and the comparison
    would then refuse the evaluation at every attempt."""
    return {int(row["id"]) for row in label_cache.read_labels(cache_dir, run["run_id"],
                                                              run["labels_sha256"])
            if row["outcome"] == LABELED}


def labeled_by_second_model(cache_dir: Path, run: dict) -> dict:
    """The names of the repositories that the second model labeled in `run`, by GitHub id, from
    the decisions that the labels of the run name (`1_classify_repos.Run.second_model_record`).
    The second model labels the evaluation sample, and it cannot check its own labels."""
    wanted: dict = {}
    for row in label_cache.read_labels(cache_dir, run["run_id"], run["labels_sha256"]):
        if row["outcome"] == LABELED:
            wanted.setdefault(row["decided_in"], set()).add(int(row["id"]))
    return {number: decision["name"]
            for (_, number), decision in label_cache.decisions_of(cache_dir, wanted).items()
            if decision.get("second_model")}


def own_label(decision: dict, model: str) -> bool:
    """Whether `decision` is a label that `model` made as the classifying model of its run. A
    label of the run's second model names the run's classifying model as its `model`
    (`1_classify_repos.Run.second_model_record`), and it counts as no label of that model, so
    that a check or a comparison never credits the second model's label to the model that it
    names. A label that the model made at its higher temperature counts as the model's
    label."""
    return (decision.get("outcome") == LABELED and decision["model"] == model
            and not decision.get("second_model"))


def labels_under(cache_dir: Path, model: str, parameters: str) -> dict:
    """The latest label per GitHub id that `model` gave under the classification parameters
    `parameters`, from any finished run (`own_label`), with the parameters matched by their keys
    (`label_cache.ParameterKeys`), which leave out the provider."""
    keys = label_cache.ParameterKeys(cache_dir)
    wanted = keys(parameters)
    return {int(decision["id"]): decision for _, decision in label_cache.all_decisions(cache_dir)
            if own_label(decision, model) and keys(decision["parameters"]) == wanted}


shared_parameters = label_cache.shared_parameters


def second_model_parameters(cache_dir: Path, parameters: str,
                            model: str) -> tuple[str, dict | None]:
    """The SHA-256 of the classification parameters under which `model`, the second model of a
    label evaluation under `parameters`, labels the evaluation sample, and the block
    `classifying_model` of those parameters, or None. The second model reads the same prompt,
    tools, summaries and README as the reference model and labels the weights of its pinned
    endpoint in its quantization with the settings of its requests (`endpoints.endpoint`,
    `endpoints.settings_of`), as `1_classify_repos.py --model <model>` labels without an endpoint
    or settings on its command line. Its parameters are therefore `parameters` with those
    weights, that quantization and those settings in the block `classifying_model`, without the
    provider of any model (`label_cache.without_provider`), and the evaluation compares none of
    its labels of other weights or with other settings, such as another reasoning effort. Its
    labels match the digest by their keys (`labels_under`), whichever provider served them.
    Parameters recorded without that block name no classifying model and are the parameters of
    both models. ValueError for an OpenRouter model without a pinned endpoint."""
    value = label_cache.recorded("parameters", parameters, cache_dir)
    if label_cache.CLASSIFYING_MODEL not in value:
        return parameters, None
    found = endpoints.endpoint(model)
    block = {**found.parameters(), **endpoints.settings_of(found).parameters()}
    return (label_cache.digest(label_cache.without_provider(
        {**value, label_cache.CLASSIFYING_MODEL: block})), block)


def compared_parameters(out: Path, agreement: dict) -> str:
    """The classification parameters of the labels of the second model that `compare`
    compared, as `agreement.json` of the evaluation in `out` records them, so that the review
    and `draw-check` read the compared labels also after the pinned endpoint or the settings of
    the second model changed. SystemExit for a record without them."""
    if not agreement.get("second_model_parameters"):
        raise SystemExit(f"{Path(out) / 'agreement.json'} names no classification parameters of "
                         f"the labels of the second model. Run compare again")
    return agreement["second_model_parameters"]


def recorded_path(path) -> str:
    """How a file is named in the tracked record of an evaluation: by its path in this
    repository, or by its name alone, so that no record contains the directories of one
    machine."""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(HERE.parent.resolve()).as_posix()
    except ValueError:
        return resolved.name


def excluded_ids(paths: list) -> tuple[set, list]:
    """The GitHub ids in the `id` column of each CSV, and what each file was: its path, its
    SHA-256 and how many ids it names."""
    ids, files = set(), []
    for path in paths:
        try:
            data = Path(path).read_bytes()
        except OSError as error:
            raise SystemExit(f"The list of repositories to leave out, {path}, cannot be read "
                             f"({error})") from error
        named = {int(row["id"]) for row in csv.DictReader(data.decode("utf-8").splitlines())}
        ids |= named
        files.append({"path": recorded_path(path), "sha256": hashlib.sha256(data).hexdigest(),
                      "repositories": len(named)})
    return ids, files


# The name of a repository as written in the prompt, owner/repo. The lookbehinds keep a match
# from starting inside a path or a longer name, such as o/r9 in src/o/r9, or ending with a period.
REPOSITORY_NAME = re.compile(r"(?<![\w./-])[\w.-]+/[\w.-]+(?<![.])")
# The passages in double quotes of the prompt that quote a text of one repository, such as its
# description or its README, each as the prompt writes it and with the repositories whose text it
# quotes. The prompt shortens some of these texts or words them in general terms, as it words the
# notice of rpm-software-management/dnf that its current version has its own repository. The
# draw leaves these repositories out while the prompt contains the passage, also when the prompt
# does not name them, because the prompt states how a criterion applies to each of them.
QUOTED_IN_PROMPT = {
    "I recommend using the Official Hex-Rays IDA MCP Server instead!": ("mrexodia/ida-pro-mcp",),
    "the MCP plugin is no longer recommended and will eventually be deprecated. Use "
    "`idalib-mcp` instead": ("mrexodia/ida-pro-mcp",),
    "The Ion maintainers recommend using `IonElement` instead of `IonValue` whenever "
    "possible": ("amazon-ion/ion-java",),
    "Do not use in production": ("mongodb/mongo-cxx-driver",),
    "The current version has its own repository": ("rpm-software-management/dnf",),
    "the better way": ("SiaFoundation/renterd",),
    "get started": ("SiaFoundation/renterd",),
    "no new features will be developed beyond its existing scope": ("omniscale/imposm3",),
    "mature, feature-complete": ("75lb/command-line-args",),
    "The project is maintained, but is not being actively developed": ("xerial/sqlite-jdbc",),
    "we aren't planning to add any additional features": ("NightOwl888/ICU4N",),
    "This repository hosts the code of the generated Algolia API client for PHP, if you'd like "
    "to contribute, head over to the main repository": ("algolia/algoliasearch-client-php",),
    "Please send code contributions by submitting a merge request at "
    "https://invent.kde.org/...": ("KDE/elisa",),
    "Read-only LibreOffice core repo - no pull request (use gerrit instead)": ("LibreOffice/core",),
    "We will accept pull requests based on this repository as well": ("bcgit/bc-java",
                                                                      "bcgit/bc-csharp"),
    "Pull requests and Issues accepted": ("coreutils/coreutils",),
    "Contributions via the GitHub mirror are welcome": ("krgamestudios/Toy",),
    "an extension of the DCAT Application Profile v3.0 in LinkML": ("nfdi-de/chem-dcat-ap",),
    "Experiments for testing-devex team for libtest ideas": ("assert-rs/libtest2",),
    "client samples": ("ibm-messaging/mq-metric-samples",),
    "though public, is only meant for demonstration purposes": ("IUPAC/IUPAC-FAIRSpec",),
    "Deploying Your Own Instance": ("elizaOS/elizaos.github.io",),
    "website sources": ("autowp/autowp",),
    "Web Experiments I do for fun": ("PierfrancescoSoffritti/doodles",),
    "Caution: This is an experimental project": ("syumai/workers-go",),
    "my own repository of scripts": ("alainbryden/bitburner-scripts",),
    "This is a monorepo for Warden Protocol and its services": ("warden-protocol/wardenprotocol",),
}
# The other passages in double quotes of the prompt, which quote no single repository, each with
# what it is. The draw leaves out no repository for such a passage. An owner's profile
# description or a README that a group of repositories shares describes the whole group, and a
# common wording belongs to every repository that writes it, so the prompt states a rule for all
# of them rather than how a criterion applies to one repository.
QUOTED_FROM_NO_REPOSITORY = {
    "...": "the placeholder for the path of the README in the tag of the input",
    "Open source projects and samples from Microsoft": "the profile description of Microsoft, "
                                                       "which describes all of its repositories",
    "Experimental projects from the Chrome team": "the profile description of GoogleChromeLabs, "
                                                  "which describes all of its repositories",
    "Official read-only mirror of the KDE project. Please don't send pull requests":
        "the profile description of KDE, which describes all of its repositories",
    "contributions are very welcome": "a common invitation to contribute, which the texts of "
                                      "many repositories write",
    "awesome": "the name of a kind of curated list of links",
    "A theme is a collection of components all styled the same way":
        "the definition of a theme of a UI framework, which the reviewer quoted for "
        "enactjs/agate, a theme of Enact, and which the prompt gives as what the framework's "
        "project calls a theme, so that it describes every theme of the framework",
    "Welcome to the JDK!": "the words of the JDK's README, which the repositories of the "
                           "OpenJDK projects, such as openjdk/loom and openjdk/tsan, carry as "
                           "downstreams of openjdk/jdk, which the prompt names",
    "Paths that show engineering practice": "a heading of the outline that the classification "
                                            "writes",
    "true": "a value of the field engineering",
    "unclear": "a value of the field engineering",
}


def named_in_prompt(repositories: dict, prompt: Path, quoted: dict) -> tuple[set, dict]:
    """The GitHub ids of the repositories in the sampling frame that the prompt names, such as
    mrexodia/ida-pro-mcp and elizaOS/elizaos.github.io, or quotes, as `quoted` gives the
    repositories of each passage that the prompt contains, and the record of the prompt: its
    path, its SHA-256, the number of these repositories and the names of those that it quotes. A
    repository that the prompt names or quotes as an example would test a label that the prompt
    states rather than one that the model derives."""
    text = Path(prompt).read_text(encoding="utf-8")
    names = {match.lower() for match in REPOSITORY_NAME.findall(text)}
    quotes = {name.lower() for passage, quoted_names in quoted.items() if passage in text
              for name in quoted_names}
    ids = {i for i, name in repositories.items() if name.lower() in names | quotes}
    return ids, {"path": recorded_path(prompt),
                 "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                 "repositories": len(ids),
                 "quoted": sorted(name for i, name in repositories.items()
                                  if i in ids and name.lower() in quotes)}


def default_exclusions(evaluation_dir: Path) -> list[Path]:
    """What `draw` leaves out unless `--exclude` names other lists: the calibration sample, on
    which the reading rules were set, and the evaluation sample of every evaluation in
    `evaluation_dir`, because the prompt was revised on the reviewed disagreements of each, as
    after the second pilot, evaluation 2026-10-30T222453Z, and the six-model test, evaluation
    2026-10-31T094615Z."""
    return [CALIBRATION_SAMPLE, *sorted(Path(evaluation_dir).glob(f"*/{SAMPLE_CSV}"))]


def draw(args) -> int:
    repositories, retrieval = sampling_frame_repositories(args.sampling_frame)
    run = sampling_frame_run(args.cache_dir, args.reference_model)
    if run["retrieval_id"] != retrieval:
        logs.error(COMPONENT, f"The latest run of {args.reference_model} over a sampling frame, "
                              f"{run['run_id']}, labeled the sampling frame of retrieval "
                              f"{run['retrieval_id']}, but {args.sampling_frame} is the sampling "
                              f"frame of retrieval {retrieval}. Label {args.sampling_frame} "
                              f"with {args.reference_model} first.")
        return 1
    parameters = run["parameters"]
    labels = labeled_in(args.cache_dir, run)
    left_out, exclude_files = excluded_ids(default_exclusions(args.evaluation_dir)
                                           if args.exclude is None else args.exclude)
    named, prompt_record = named_in_prompt(repositories, CLASSIFY.PROMPT, QUOTED_IN_PROMPT)
    left_out |= named
    second = labeled_by_second_model(args.cache_dir, run)
    left_out |= set(second)
    labeled = [i for i in repositories if i in labels]
    population = sorted(i for i in labeled if i not in left_out)
    if not population:
        logs.error(COMPONENT, f"No repository in the sampling frame has a label of "
                              f"{args.reference_model}.")
        return 1
    n = sample_size(len(population), args.margin, args.confidence)
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2 ** 32)
    chosen = sorted(random.Random(seed).sample(population, n))
    # The evaluation is named by the UTC second at which it was drawn, or by the first later second
    # that no other evaluation has.
    moment = label_cache.utc_now()
    while (Path(args.evaluation_dir) / label_cache.run_id(moment)).exists():
        moment += timedelta(seconds=1)
    eid = label_cache.run_id(moment)
    out = Path(args.evaluation_dir) / eid
    out.mkdir(parents=True)
    with open(out / SAMPLE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["id", "repo_name"])
        writer.writerows((i, repositories[i]) for i in chosen)
    (out / SAMPLE_JSON).write_text(json.dumps({
        "evaluation_id": eid, "retrieval_id": retrieval, "run_id": run["run_id"],
        "reference_model": args.reference_model, "parameters": parameters,
        "labeled": len(labeled), "excluded": exclude_files, "named_in_prompt": prompt_record,
        "labeled_by_second_model": {"repositories": len(second),
                                    "names": sorted(second.values())},
        "left_out": len(labeled) - len(population),
        "population": len(population), "size": n, "margin": args.margin,
        "confidence": args.confidence, "seed": seed}, indent=2) + "\n", encoding="utf-8")
    names = ", ".join(Path(f["path"]).name for f in exclude_files)
    if second:
        logs.info(COMPONENT, f"Left out the {len(second):,} repositories that the second model "
                             f"labeled in run {run['run_id']}, the first of them "
                             f"{min(second.values())}, because the second model labels the "
                             f"evaluation sample.")
    logs.info(COMPONENT, f"Drew an evaluation sample of {n:,} of the {len(population):,} "
                         f"repositories that {args.reference_model} labeled"
                         + (f", without the {len(labeled) - len(population):,} listed in "
                            f"{names}" if exclude_files else "")
                         + f" (±{args.margin:.0%} at {args.confidence:.0%} confidence), into "
                         f"{out / SAMPLE_CSV}.")
    return 0


def tools_named(decision: dict) -> str:
    return "|".join(call["tool"] for call in decision.get("tools", []))


def kappa(pairs: list) -> float | None:
    """Cohen's kappa of two raters over `pairs` of labels, or None when chance agreement is 1."""
    n = len(pairs)
    observed = sum(a == b for a, b in pairs) / n
    expected = sum((sum(a == label for a, _ in pairs) / n) * (sum(b == label for _, b in pairs) / n)
                   for label in LABELS)
    return None if expected == 1 else (observed - expected) / (1 - expected)


def wilson(successes: int, n: int, confidence: float, population: int) -> tuple[float, float]:
    """The Wilson interval of a proportion, its width corrected for the finite population."""
    z = NormalDist().inv_cdf(0.5 + confidence / 2)
    z *= math.sqrt((population - n) / (population - 1)) if population > n else 0.0
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def compare(args) -> int:
    out = Path(args.evaluation)
    meta = json.loads((out / SAMPLE_JSON).read_text(encoding="utf-8"))
    with open(out / SAMPLE_CSV, newline="", encoding="utf-8") as f:
        chosen = [(int(row["id"]), row["repo_name"]) for row in csv.DictReader(f)]
    # Both models' labels under the evaluation's classification parameters, the second model's
    # on its pinned endpoint and with the settings of its requests, so that a disagreement is a
    # difference between the models alone. After a check of new parameters with the reference
    # model, the labels of the reference model's latest finished run would come from other
    # parameters than the labels of the second model.
    parameters = meta["parameters"]
    try:
        second_parameters, block = second_model_parameters(args.cache_dir, parameters,
                                                           args.second_model)
    except ValueError as error:
        logs.error(COMPONENT, f"{args.second_model} has no pinned endpoint, on which the "
                              f"comparison takes the labels of the second model ({error}).")
        return 1
    reference = labels_under(args.cache_dir, meta["reference_model"], parameters)
    second = labels_under(args.cache_dir, args.second_model, second_parameters)
    pairs, rows, missing, changed = [], [], [], []
    for i, name in chosen:
        a, b = reference.get(i), second.get(i)
        if a is None or b is None:
            missing.append(name)
            continue
        if a["readme_sha"] != b["readme_sha"]:
            changed.append(name)
            continue
        pairs.append((a["label"], b["label"]))
        if a["label"] != b["label"]:
            rows.append({"id": i, "repo_name": name, "github_url": f"https://github.com/{name}",
                         "readme_path": a["readme_path"], "readme_sha": a["readme_sha"],
                         "reference_label": a["label"],
                         "reference_criteria": "|".join(a.get("applied_criteria", [])),
                         "reference_justification": a["justification"],
                         "reference_tools": tools_named(a), "second_label": b["label"],
                         "second_criteria": "|".join(b.get("applied_criteria", [])),
                         "second_justification": b["justification"],
                         "second_tools": tools_named(b), "human_label": "",
                         "human_criteria": "", "human_note": ""})
    if missing:
        logs.error(COMPONENT, f"{len(missing):,} of the {len(chosen):,} repositories in the "
                              f"evaluation sample, such as {missing[0]}, lack a label of one of "
                              f"the models under the evaluation's classification parameters "
                              f"{parameters[:12]}, which for {args.second_model} on its pinned "
                              f"endpoint with the settings of its requests are "
                              f"{second_parameters[:12]}. Label them under these parameters "
                              f"first.")
        return 1
    if not pairs:
        logs.error(COMPONENT, "The README of every repository in the evaluation sample changed "
                              "between the two labels. Label the evaluation sample with the "
                              "second model again.")
        return 1
    # A comparison that runs again keeps every decision that a reviewer already made.
    reviewed = {row["id"]: row for row in read_disagreements(out)}
    for row in rows:
        before = reviewed.get(str(row["id"]), {})
        for column in ("human_label", "human_criteria", "human_note"):
            row[column] = before.get(column, "")
    write_disagreements(out, rows)
    agreed = len(pairs) - len(rows)
    low, high = wilson(agreed, len(pairs), meta["confidence"], meta["population"])
    confusion = {a: {b: sum(1 for x, y in pairs if (x, y) == (a, b)) for b in LABELS}
                 for a in LABELS}
    (out / "agreement.json").write_text(json.dumps({
        "reference_model": meta["reference_model"], "second_model": args.second_model,
        "parameters": parameters, "second_model_parameters": second_parameters,
        "second_model_endpoint_and_settings": block, "compared": len(pairs), "agreed": agreed,
        "agreement": agreed / len(pairs), "agreement_interval": [low, high],
        "kappa": kappa(pairs), "confusion": confusion,
        "readme_changed_between_labels": changed}, indent=2) + "\n", encoding="utf-8")
    logs.info(COMPONENT, f"{meta['reference_model']} and {args.second_model} agree on "
                         f"{agreed:,} of {len(pairs):,} repositories ({agreed / len(pairs):.1%}, "
                         f"{low:.1%} to {high:.1%}).")
    if changed:
        logs.info(COMPONENT, f"Left out {len(changed):,} repositories with a README that changed "
                             f"between the two labels.")
    logs.info(COMPONENT, f"Review the {len(rows):,} disagreements in "
                         f"{out / 'disagreements.csv'}.")
    return 0


def read_disagreements(out: Path) -> list[dict]:
    """The rows of evaluation/<id>/disagreements.csv, or no rows before `compare` wrote it."""
    path = Path(out) / "disagreements.csv"
    if not path.is_file():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def replace_variant_rows(path: Path, variant: str, lines: list[dict]) -> str | None:
    """Replace the rows of `variant` in the record of a check at `path` with `lines`, keeping the
    rows of the other variants, or return the reason that the record cannot take them, a header
    with other columns than PROMPT_CHECK_COLUMNS. A record with a header of the first columns of
    PROMPT_CHECK_COLUMNS, written before the code added the others, gets the missing columns,
    empty in its earlier rows, whose code did not record them. Two checks of the same date and
    evaluation write the same record, each when its runs end. Each takes an exclusive lock of
    the lock file beside the record (`<record>.lock`) before it reads the record and holds it
    until the new record replaces the old one in one step, so that the second waits for the
    first and keeps its rows. The kernel releases the lock of a process that dies, and the lock
    file stays."""
    with open(path.with_name(path.name + ".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            kept = []
            if path.is_file():
                with open(path, newline="", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    columns = reader.fieldnames or []
                    if not columns or columns != PROMPT_CHECK_COLUMNS[:len(columns)]:
                        return (f"{path} has other columns than the record of a check, "
                                f"{', '.join(PROMPT_CHECK_COLUMNS)}")
                    kept = [line for line in reader if line["variant"] != variant]
            handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                                 suffix=".tmp")
            try:
                with os.fdopen(handle, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=PROMPT_CHECK_COLUMNS, restval="",
                                            lineterminator="\n")
                    writer.writeheader()
                    writer.writerows(kept + lines)
                os.chmod(temporary, 0o644)
                os.replace(temporary, path)
            except BaseException:
                Path(temporary).unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    return None


def write_disagreements(out: Path, rows: list[dict]) -> None:
    """Write disagreements.csv in one step, so that a stop never leaves half a review."""
    path = Path(out) / "disagreements.csv"
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=DISAGREEMENT_COLUMNS, lineterminator="\n")
            writer.writeheader()
            writer.writerows({column: row.get(column, "") for column in DISAGREEMENT_COLUMNS}
                             for row in rows)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


class Review:
    """The disagreements of one evaluation, with what each model read and its response, and the
    reviewer's decisions written into disagreements.csv as they are made."""

    def __init__(self, out: Path, cache_dir: Path, fetch_readme):
        self.out = Path(out)
        meta = json.loads((self.out / SAMPLE_JSON).read_text(encoding="utf-8"))
        if not (self.out / "agreement.json").is_file():
            # Without this check, a review before compare would end with a FileNotFoundError and
            # its traceback.
            raise SystemExit(f"{self.out / 'agreement.json'} does not exist. Run compare first")
        agreement = json.loads((self.out / "agreement.json").read_text(encoding="utf-8"))
        self.models = (meta["reference_model"], agreement["second_model"])
        compared = (meta["parameters"], compared_parameters(self.out, agreement))
        self.rows = read_disagreements(self.out)
        if not self.rows:
            raise SystemExit(f"{self.out / 'disagreements.csv'} has no disagreement. Run "
                             f"compare first")
        wanted = {int(row["id"]) for row in self.rows}
        # The decisions that were compared, under the evaluation's classification parameters. The
        # decisions of the latest finished run can be the decisions of a later check, with other
        # labels and criteria than the rows that the reviewer decides on.
        self.decisions = {}
        for side, model, parameters in zip(("reference", "second"), self.models, compared):
            labels = labels_under(cache_dir, model, parameters)
            self.decisions[side] = {i: labels[i] for i in wanted if i in labels}
        self.fetch_readme = fetch_readme
        self.readmes: dict[int, dict] = {}
        self.lock = threading.Lock()

    def item(self, row: dict) -> dict:
        """One disagreement as the page shows it."""
        rid = int(row["id"])
        sides = {}
        for side, model in zip(("reference", "second"), self.models):
            decision = self.decisions[side].get(rid, {})
            sides[side] = {"model": model, "label": row[f"{side}_label"],
                           "justification": row[f"{side}_justification"],
                           "model_label": decision.get("model_label", ""),
                           "applied_criteria": decision.get("applied_criteria", []),
                           "unclear_criteria": decision.get("unclear_criteria", []),
                           "exclusions": decision.get("exclusions", {}),
                           "maintenance_and_use": decision.get("maintenance_and_use"),
                           "purpose": decision.get("purpose", ""),
                           "purpose_quote": decision.get("purpose_quote", ""),
                           "purpose_source": decision.get("purpose_source", ""),
                           "description": decision.get("description", ""),
                           "fork": decision.get("fork"),
                           "mirror_merges": decision.get("mirror_merges"),
                           "readme_mainly_english": decision.get("readme_mainly_english"),
                           "readme_words_by_language": decision.get("readme_words_by_language"),
                           "limits_reached": decision.get("limits_reached", []),
                           "tools": [{"round": call.get("round"), "tool": call.get("tool"),
                                      "arguments": call.get("arguments", {}),
                                      "output": call.get("output", ""),
                                      "is_error": call.get("is_error", False)}
                                     for call in decision.get("tools", [])]}
        revision = self.decisions["reference"].get(rid, {}).get("revision", "")
        return {"id": rid, "repo_name": row["repo_name"], "github_url": row["github_url"],
                "revision": revision, "readme_path": row["readme_path"],
                "readme_sha": row["readme_sha"], "human_label": row["human_label"],
                "human_criteria": [c for c in row.get("human_criteria", "").split("|") if c],
                "human_note": row["human_note"], **sides}

    def items(self) -> list[dict]:
        with self.lock:
            return [self.item(row) for row in self.rows]

    def readme(self, rid: int) -> dict:
        """The README that the models read: its text at their revision, or the reason that it is
        missing."""
        with self.lock:
            cached = self.readmes.get(rid)
        if cached is not None:
            return cached
        row = next(r for r in self.rows if int(r["id"]) == rid)
        revision = self.decisions["reference"].get(rid, {}).get("revision", "")
        try:
            text, sha = self.fetch_readme(row["repo_name"], rid, revision)
        except Exception as error:  # noqa: BLE001 - shown on the page, the review goes on
            return {"text": "", "note": f"GitHub returned no response ({error})"}
        note = "" if sha == row["readme_sha"] else (
            f"GitHub returns blob {sha or 'none'}, not the blob {row['readme_sha']} that the "
            f"models read")
        readme = {"text": text or "", "note": note}
        with self.lock:
            self.readmes[rid] = readme
        return readme

    def decide(self, rid: int, label: str, note: str, criteria=()) -> dict:
        """Record the reviewer's label, the criteria that apply when the label is false, and the
        note for one disagreement in disagreements.csv."""
        if label not in ("", *LABELS):
            raise ValueError(f"the label {label!r} is not one of {', '.join(LABELS)}")
        unknown = [c for c in criteria if c not in CRITERIA]
        if unknown:
            raise ValueError(f"{', '.join(unknown)} is not a criterion of the classification")
        if criteria and label != "false":
            raise ValueError("criteria are allowed only with the label false")
        with self.lock:
            row = next((r for r in self.rows if int(r["id"]) == rid), None)
            if row is None:
                raise KeyError(f"no disagreement has the id {rid}")
            row["human_label"], row["human_note"] = label, note.strip()
            row["human_criteria"] = "|".join(c for c in CRITERIA if c in criteria)
            write_disagreements(self.out, self.rows)
            return self.item(row)


def draw_check(args) -> int:
    """The repositories that check new classification parameters against a reviewed
    evaluation, with the label that each is expected to get."""
    out = Path(args.evaluation)
    meta = json.loads((out / SAMPLE_JSON).read_text(encoding="utf-8"))
    agreement = json.loads((out / "agreement.json").read_text(encoding="utf-8"))
    rows = read_disagreements(out)
    undecided = [row["repo_name"] for row in rows if not row["human_label"]]
    if not rows or undecided:
        logs.error(COMPONENT, f"{out / 'disagreements.csv'} has "
                              + (f"{len(undecided):,} undecided disagreements, such as "
                                 f"{undecided[0]}. Review them first." if undecided
                                 else "no disagreement. Run compare and review first."))
        return 1
    forks = {}
    if (out / FORKS_JSON).is_file():
        record = json.loads((out / FORKS_JSON).read_text(encoding="utf-8"))
        forks = {fork["repo_name"]: fork for fork in record.get("forks", [])}
    chosen = []
    for row in rows:
        expected, criteria = row["human_label"], row.get("human_criteria", "")
        fork = forks.get(row["repo_name"])
        if fork is not None and not fork.get("independent") and expected == "unclear":
            # The reviewer leaves a fork open until its independence is decided: a fork that
            # the fork heuristic finds not independent is false.
            expected, criteria = "false", "fork"
        chosen.append({"id": int(row["id"]), "repo_name": row["repo_name"],
                       "group": DISAGREEMENT, "expected_label": expected,
                       "expected_criteria": criteria})
    with open(out / SAMPLE_CSV, newline="", encoding="utf-8") as f:
        names = {int(row["id"]): row["repo_name"] for row in csv.DictReader(f)}
    reference = labels_under(args.cache_dir, meta["reference_model"], meta["parameters"])
    second = labels_under(args.cache_dir, agreement["second_model"],
                          compared_parameters(out, agreement))
    disagreeing = {item["id"] for item in chosen}
    agreed = sorted(i for i in names if i not in disagreeing and i in reference and i in second
                    and reference[i]["label"] == second[i]["label"])
    if args.agreements > len(agreed):
        logs.error(COMPONENT, f"The evaluation has {len(agreed):,} repositories that both models "
                              f"agreed on, fewer than the {args.agreements:,} requested.")
        return 1
    reviewed = {}
    if (out / REVIEWED_AGREEMENTS_CSV).is_file():
        with open(out / REVIEWED_AGREEMENTS_CSV, newline="", encoding="utf-8") as f:
            reviewed = {int(row["id"]): row for row in csv.DictReader(f)}
    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2 ** 32)
    for i in sorted(random.Random(seed).sample(agreed, args.agreements)):
        review = reviewed.get(i)
        chosen.append({"id": i, "repo_name": names[i], "group": AGREEMENT,
                       "expected_label": review["human_label"] if review
                       else reference[i]["label"],
                       "expected_criteria": review["human_criteria"] if review else ""})
    with open(out / CHECK_SAMPLE_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CHECK_SAMPLE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(chosen)
    (out / CHECK_SAMPLE_JSON).write_text(json.dumps({
        "evaluation_id": out.name, "parameters": meta["parameters"],
        "reference_model": meta["reference_model"], "second_model": agreement["second_model"],
        "disagreements": len(rows), "agreed": len(agreed), "agreements": args.agreements,
        "seed": seed, "forks": recorded_path(out / FORKS_JSON) if forks else "",
        "reviewed_agreements": recorded_path(out / REVIEWED_AGREEMENTS_CSV) if reviewed else ""},
        indent=2) + "\n", encoding="utf-8")
    logs.info(COMPONENT, f"Chose {len(rows):,} disagreements and {args.agreements:,} of the "
                         f"{len(agreed):,} repositories that both models agreed on, with seed "
                         f"{seed}.")
    logs.info(COMPONENT, f"Label {out / CHECK_SAMPLE_CSV} with each model under the new "
                         f"classification parameters, then run check.")
    return 0


def check(args) -> int:
    """Compare each model's labels for the repositories in check_sample.csv with the expected
    labels."""
    out = Path(args.evaluation)
    with open(out / CHECK_SAMPLE_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    meta = json.loads((out / CHECK_SAMPLE_JSON).read_text(encoding="utf-8"))
    # A repository that left the sampling frame after the evaluation was drawn is not labeled
    # again. The sampling window moves with each retrieval, and some repositories in an
    # evaluation fall out of the window.
    in_sampling_frame, retrieval = sampling_frame_repositories(args.sampling_frame)
    absent = [row["repo_name"] for row in rows if int(row["id"]) not in in_sampling_frame]
    rows = [row for row in rows if int(row["id"]) in in_sampling_frame]
    if absent:
        logs.warning(COMPONENT, f"{len(absent):,} repositories in {out / CHECK_SAMPLE_CSV}, such "
                                f"as {absent[0]}, are not in the sampling frame of retrieval "
                                f"{retrieval}. Left them out of the check.")
    if not rows:
        # A check of no repository would report "0 of the 0" as a result.
        logs.error(COMPONENT, f"No repository in {out / CHECK_SAMPLE_CSV} is in the sampling "
                              f"frame {args.sampling_frame}.")
        return 1
    summary = {"evaluation_id": out.name, "previous_parameters": meta["parameters"],
               "sampling_frame_retrieval": retrieval, "not_in_sampling_frame": absent,
               "models": {}}
    for model in args.models:
        # The labels come from the latest finished run. Without this test, a later run that a stop
        # or a dropped connection left unfinished would be skipped, and an earlier run reported in
        # its place.
        runs = [row for row in label_cache.read_index(args.cache_dir) if row["model"] == model]
        if runs and runs[-1]["outcome"] != "finished":
            logs.error(COMPONENT, f"The latest run of {model}, {runs[-1]['run_id']}, is "
                                  f"{runs[-1]['outcome']}. Resume it before the check.")
            return 1
        labels, parameters = latest_labels(args.cache_dir, model)
        if (shared_parameters(args.cache_dir, parameters)
                == shared_parameters(args.cache_dir, meta["parameters"])):
            # Labels from a run under the evaluation's own parameters, such as the second model's
            # run over the evaluation sample, are the evaluation's labels and test no new
            # parameters.
            logs.error(COMPONENT, f"The latest finished run of {model} labeled under the "
                                  f"evaluation's classification parameters "
                                  f"{parameters[:12]}, not under new ones. Label "
                                  f"{out / CHECK_SAMPLE_CSV} under the new parameters first.")
            return 1
        missing = [row["repo_name"] for row in rows if int(row["id"]) not in labels]
        if missing:
            logs.error(COMPONENT, f"{model} has no label under the classification parameters "
                                  f"{parameters[:12]} for {len(missing):,} of the "
                                  f"{len(rows):,} repositories, such as {missing[0]}. Label "
                                  f"{out / CHECK_SAMPLE_CSV} with {model} first.")
            return 1
        counts = {group: {"repositories": 0, "expected": 0, "as_expected": 0, "changes": {}}
                  for group in (DISAGREEMENT, AGREEMENT)}
        for row in rows:
            decision = labels[int(row["id"])]
            row[f"{model}_label"] = decision["label"]
            row[f"{model}_criteria"] = "|".join(decision.get("applied_criteria", []))
            group = counts[row["group"]]
            group["repositories"] += 1
            # A reviewer's "unclear" expects no label in particular.
            if row["expected_label"] in ("true", "false"):
                group["expected"] += 1
                group["as_expected"] += decision["label"] == row["expected_label"]
            if decision["label"] != row["expected_label"]:
                change = f"{row['expected_label']} to {decision['label']}"
                group["changes"][change] = group["changes"].get(change, 0) + 1
        summary["models"][model] = {"parameters": parameters, **counts}
        logs.info(COMPONENT, f"{model} under {parameters[:12]} labeled "
                             f"{counts[DISAGREEMENT]['as_expected']:,} of the "
                             f"{counts[DISAGREEMENT]['expected']:,} reviewed disagreements with a "
                             f"definite label as the reviewer did, and "
                             f"{counts[AGREEMENT]['as_expected']:,} of the "
                             f"{counts[AGREEMENT]['expected']:,} agreed repositories with the "
                             f"agreed label.")
    columns = CHECK_SAMPLE_COLUMNS + [f"{model}_{field}" for model in args.models
                                      for field in ("label", "criteria")]
    with open(out / CHECK_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    (out / CHECK_JSON).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return 0


# Where the runs of `check --runs` keep their labels: one cache of labels per run, under the date
# of the check and its variant, so that a run labels every repository itself and reuses no label
# of another run under the same model and classification parameters.
CHECK_DIR = CLASSIFICATION_DIR / "checks"
# The record of the runs of one day's checks of an evaluation, in its directory, with the
# columns of the records that the earlier checks wrote (`prompt_check_2026-10-26.csv`) followed by
# the endpoint, the spread between the runs of the variant, the failed responses
# (`1_classify_repos.SECOND_MODEL_REASONS`), the repositories left without a decision and the
# cost, and then the higher temperature of the attempts after a failed response, empty for runs
# without one, and the labels of the run made at it. A label made at the higher temperature
# counts as the model's label in the check, and the two columns show how many labels of a row
# come from requests at another temperature than the variant names.
PROMPT_CHECK = "prompt_check_{date}.csv"
PROMPT_CHECK_COLUMNS = ["variant", "texts", "run_id", "labels", "parameters", "disagreements",
                        "disagreements_as_reviewed", "agreements", "agreements_as_reviewed",
                        "differences", "differing_labels", "model", "provider", "quantization",
                        "reasoning_effort", "varying_repositories", "responses",
                        "failed_responses", "failed_share", "undecided", "repositories_labeled",
                        "repositories_with_tool_rounds", "linked_pages", "cost_usd",
                        "cost_per_repository_usd", "higher_temperature",
                        "higher_temperature_labels"]


def label_check(arguments: list) -> int:
    """One run of the classification over the repositories of a check, `1_classify_repos.py`
    with `arguments`, and its exit code."""
    return importlib.import_module("1_classify_repos").main(arguments)


# The part of the name of a variant whose runs send neither a temperature, a top_p nor a seed
# (`--endpoint-default-temperature` of 1_classify_repos.py).
ENDPOINT_DEFAULT_TEMPERATURE = "endpoint-default-temperature"


def variant_name(args, found: endpoints.Endpoint, effort: str) -> str:
    """The variant of a check run: `--variant`, or the model, the provider slug of its endpoint
    `found`, the reasoning effort `effort` of the runs and a temperature, a top_p or a seed that
    the check names, or ENDPOINT_DEFAULT_TEMPERATURE for runs without any of them, in one name
    that can name a directory."""
    if args.variant:
        return args.variant
    parts = [args.model, found.provider, effort,
             *(f"{name}-{value}" for name, value in (("temperature", args.temperature),
                                                     ("top_p", args.top_p),
                                                     ("seed", args.seed)) if value is not None),
             ENDPOINT_DEFAULT_TEMPERATURE if args.endpoint_default_temperature else ""]
    return "-".join(part.replace("/", "_") for part in parts if part)


def finished_run(cache_dir: Path, model: str) -> dict | None:
    """The index row of the latest finished run of `model` in a cache of labels, or None."""
    if not (Path(cache_dir) / label_cache.INDEX_NAME).is_file():
        return None
    runs = [row for row in label_cache.read_index(cache_dir)
            if row["model"] == model and row["outcome"] == "finished"]
    return runs[-1] if runs else None


def run_record(cache_dir: Path, row: dict) -> dict:
    """What the comparison reads of one finished run of a check: its labels, the responses of the
    decisions that its labels name and their failed responses, the decisions made at the higher
    temperature, the decisions with a round of tool calls and the linked pages that they read, the
    repositories that the run left without a decision, and the cost of the runs that made those
    decisions (`costs.cost_record`). A check again under the same parameters starts a run that
    reuses every decision and sends no request to the model, and the decisions and the cost of the
    run that made them count, once each. The decisions of a run under other parameters, such as
    before a change of the summary model, are named by no label of the run."""
    costs = importlib.import_module("costs")
    labels = labels_under(cache_dir, row["model"], row["parameters"])
    outcomes = label_cache.read_labels(cache_dir, row["run_id"], row["labels_sha256"])
    # The decision that each label of the run names, once: a run that carries a label to changed
    # code writes it again with the usage of the decision that it carries.
    named = {(int(outcome["id"]), outcome["decided_in"]) for outcome in outcomes
             if outcome.get("decided_in")}
    made = [decision for run_id, decision in label_cache.all_decisions(cache_dir)
            if (int(decision["id"]), run_id) in named and decision.get("model") == row["model"]
            and "usage" in decision]
    # The runs that paid for those decisions: the run that made each one, and for a carried
    # decision the run that made the decision that it carries, through every carry between.
    every = {(int(decision["id"]), run_id): decision
             for run_id, decision in label_cache.all_decisions(cache_dir)}
    paid = set()
    for key in named & every.keys():
        seen = {key}
        while (carried := (every[key].get("carried_from") or {}).get("run")):
            origin = (key[0], carried)
            if origin not in every or origin in seen:
                break
            seen.add(origin)
            key = origin
        paid.add(key[1])
    cost = sum((costs.total(costs.cost_record(cache_dir, run_id, prices_path=costs.PRICES))
                for run_id in sorted(paid)), costs.Decimal(0))
    labeled = int(row["labeled"] or 0)
    return {"labels": labels,
            "responses": sum(int(d["usage"].get("responses") or 0) for d in made),
            "failed_responses": sum(int(d["usage"].get("failed_responses") or 0) for d in made),
            "higher_temperature_labels": sum(1 for d in made if d.get("higher_temperature")),
            "tool_rounds": sum(1 for d in made if int(d["usage"].get("tool_rounds") or 0)),
            "linked_pages": sum(int(d["usage"].get("linked_pages") or 0) for d in made),
            "undecided": sum(1 for outcome in outcomes if outcome["outcome"] == UNDECIDED),
            "labeled": labeled, "cost": cost}


def check_counts(rows: list, labels: dict) -> dict:
    """The counts of the earlier checks for one run over the check sample `rows`: the reviewed
    disagreements with a definite label and those labeled as the reviewer did, the agreed
    repositories with a definite label and those that kept it, the repositories labeled
    otherwise than expected and their labels."""
    counts = {"disagreements": 0, "disagreements_as_reviewed": 0, "agreements": 0,
              "agreements_as_reviewed": 0}
    differing = []
    for row in rows:
        label = labels.get(int(row["id"]), {}).get("label", UNDECIDED)
        if row["expected_label"] not in ("true", "false"):
            continue
        group = "disagreements" if row["group"] == DISAGREEMENT else "agreements"
        counts[group] += 1
        if label == row["expected_label"]:
            counts[f"{group}_as_reviewed"] += 1
        else:
            differing.append(f"{row['repo_name']}={label}")
    return {**counts, "differences": len(differing), "differing_labels": " ".join(sorted(differing))}


def check_runs(args, label=label_check) -> int:
    """Label the check samples of the evaluations of `args` with one model, endpoint and
    reasoning effort in `args.runs` runs, each over the repositories of every check sample in
    the sampling frame and in a cache of labels of its own, and write one row per run to the
    record of the day's checks of each evaluation. `label(arguments)` runs the classification.
    A run that a stop or a refusal ended is resumed by the next invocation with the same date.
    Each invocation hands every run to the classification, which reuses the decisions under the
    same model and classification parameters without a request to the model, and labels again
    otherwise, such as after a change of the summary model or of the code that the parameters
    record. Each row of the record describes the latest finished run of its cache. It counts,
    once each, the decisions that the labels of the run name and the cost of the runs that made
    those decisions (`run_record`)."""
    outs = [Path(out) for out in args.evaluation]
    in_sampling_frame, retrieval = sampling_frame_repositories(args.sampling_frame)
    samples = {}
    for out in outs:
        with open(out / CHECK_SAMPLE_CSV, newline="", encoding="utf-8") as f:
            samples[out] = [row for row in csv.DictReader(f)
                            if int(row["id"]) in in_sampling_frame]
    ids = sorted({int(row["id"]) for rows in samples.values() for row in rows})
    if not ids:
        logs.error(COMPONENT, f"No repository of the check samples is in the sampling frame "
                              f"{args.sampling_frame}.")
        return 1
    # The endpoint of the runs, resolved once: a model with a pinned endpoint that the check
    # names without --provider labels on that endpoint (`endpoints.PINNED`), and the options of
    # the runs, the variant and the record name it, as for a check that names it.
    try:
        found = endpoints.endpoint(args.model, args.provider, args.quantization,
                                   args.hugging_face_id)
    except ValueError as error:
        logs.error(COMPONENT, f"{error}.")
        return 1
    if args.endpoint_default_temperature and not found.openrouter:
        logs.error(COMPONENT, f"{args.model} is an OpenAI model, whose requests send no "
                              f"temperature, top_p or seed in any run, and "
                              f"--endpoint-default-temperature applies only to an OpenRouter "
                              f"model.")
        return 1
    # The reasoning effort of the runs, that of the model's settings unless the check names one,
    # which the variant and the record name.
    effort = args.reasoning_effort or endpoints.settings_of(found).reasoning_effort
    # The settings of the attempts after a failed response, as the runs take them from the same
    # options (`1_classify_repos.run_settings`), which the record names. The options of a check
    # are the text that the runs receive.
    try:
        higher = CLASSIFY.run_settings(argparse.Namespace(
            model=args.model, provider=found.provider, quantization=found.quantization,
            hugging_face_id=found.hugging_face_id, reasoning_effort=effort,
            temperature=None if args.temperature is None else float(args.temperature),
            top_p=None if args.top_p is None else float(args.top_p),
            seed=None if args.seed is None else int(args.seed),
            endpoint_default_temperature=args.endpoint_default_temperature)).higher()
    except ValueError as error:
        logs.error(COMPONENT, f"{error}.")
        return 1
    variant, date = variant_name(args, found, effort), args.check_date
    base = Path(args.check_dir) / date / variant
    base.mkdir(parents=True, exist_ok=True)
    listed = base / "repositories.csv"
    listed.write_text("id\n" + "".join(f"{i}\n" for i in ids), encoding="utf-8")
    endpoint = [option for name, value in (("--provider", found.provider),
                                           ("--quantization", found.quantization),
                                           ("--hugging-face-id", found.hugging_face_id),
                                           ("--temperature", args.temperature),
                                           ("--top-p", args.top_p),
                                           ("--seed", args.seed))
                if value is not None and value != "" for option in (name, str(value))]
    if args.endpoint_default_temperature:
        endpoint.append("--endpoint-default-temperature")
    summary = [option for name, value in (("--summary-model", args.summary_model),
                                          ("--summary-effort", args.summary_effort),
                                          ("--summary-provider", args.summary_provider),
                                          ("--summary-quantization", args.summary_quantization),
                                          ("--summary-hugging-face-id",
                                           args.summary_hugging_face_id))
               if value for option in (name, value)]
    records = []
    for number in range(1, args.runs + 1):
        cache = base / f"run-{number}"
        before = finished_run(cache, args.model)
        logs.info(COMPONENT, f"Run {number} of {args.runs} of {variant} labels {len(ids):,} "
                             f"repositories of {len(outs):,} check samples into {cache}"
                             + (f", where run {before['run_id']} finished" if before else "")
                             + ".")
        # A run measures the model of the check on the endpoint of the check: a repository that
        # the model fails stays without a label instead of getting the second model's, and a
        # request that the endpoint fails goes to no backup provider.
        code = label(["--sampling-frame", str(args.sampling_frame), "--cache-dir",
                      str(cache), "--repository-list", str(listed), "--model", args.model,
                      *endpoint, "--reasoning-effort", effort,
                      "--openai-service-tier", args.service_tier,
                      "--workers", str(args.workers), *summary,
                      "--credentials", str(args.credentials), "--no-second-model",
                      "--no-backup-provider"])
        if code:
            logs.error(COMPONENT, f"Run {number} of {variant} ended with exit {code}. Run "
                                  f"the check again with --check-date {date} to resume it.")
            return code
        row = finished_run(cache, args.model)
        if before and row and row["run_id"] != before["run_id"] and not int(row["decisions"] or 0):
            logs.info(COMPONENT, f"Run {number} of {variant} decided no repository, because run "
                                 f"{row['run_id']} reused the decisions of the earlier runs under "
                                 f"the same model and classification parameters.")
        if row is None:
            logs.error(COMPONENT, f"{cache} has no finished run of {args.model}.")
            return 1
        records.append((cache, row, run_record(cache, row)))
    for out in outs:
        rows = samples[out]
        varying = sum(1 for row in rows
                      if len({record["labels"].get(int(row["id"]), {}).get("label", UNDECIDED)
                              for _, _, record in records}) > 1)
        lines = []
        for cache, row, record in records:
            responses = record["responses"]
            lines.append({
                "variant": variant, "texts": args.texts or f"the code of the check with "
                                                           f"{args.model}",
                "run_id": row["run_id"],
                "labels": cache.relative_to(Path(args.check_dir).parent).as_posix(),
                "parameters": row["parameters"], **check_counts(rows, record["labels"]),
                "model": args.model, "provider": found.provider,
                "quantization": found.quantization, "reasoning_effort": effort,
                "varying_repositories": varying, "responses": responses,
                "failed_responses": record["failed_responses"],
                "failed_share": (f"{record['failed_responses'] / responses:.4f}" if responses
                                 else ""),
                "undecided": record["undecided"], "repositories_labeled": record["labeled"],
                "repositories_with_tool_rounds": record["tool_rounds"],
                "linked_pages": record["linked_pages"],
                "cost_usd": f"{record['cost']:.4f}",
                "cost_per_repository_usd": (f"{record['cost'] / record['labeled']:.6f}"
                                            if record["labeled"] else ""),
                "higher_temperature": higher.temperature if higher is not None else "",
                "higher_temperature_labels": record["higher_temperature_labels"]})
        path = out / PROMPT_CHECK.format(date=date)
        refused = replace_variant_rows(path, variant, lines)
        if refused:
            logs.error(COMPONENT, f"{refused}. Give the record these columns and run the check "
                                  f"again with --check-date {date}, whose runs reuse their "
                                  f"decisions without a request to the model.")
            return 1
        mean = sum(line["differences"] for line in lines) / len(lines)
        logs.info(COMPONENT, f"{variant} labeled the check sample of {out.name} in "
                             f"{len(lines):,} runs with {mean:.1f} repositories otherwise than "
                             f"expected on average and {varying:,} labeled differently between "
                             f"the runs. Wrote the runs to {path}.")
    return 0


def check_command(args, label=None) -> int:
    """`check`: with --runs, the runs of one model over the check samples (`check_runs`), and
    otherwise the comparison of the latest labels of each model of --models with each
    evaluation's expected labels (`check`)."""
    if args.runs:
        return check_runs(args, label or label_check)
    for out in args.evaluation:
        code = check(argparse.Namespace(**{**vars(args), "evaluation": out}))
        if code:
            return code
    return 0


def github_readme(credentials_path: Path):
    """A function that reads a README at a revision through GitHub's API, with the GitHub
    access tokens of the credentials file, and returns the README's text and blob SHA."""
    import readmes
    from agentcontext.github_api import TokenPool
    tokens = readmes.accepted_tokens(credentials.github_tokens(credentials_path, os.environ))
    if not tokens:
        raise SystemExit(f"Reading the READMEs requires a GitHub access token that GitHub "
                         f"accepts, from GITHUB_TOKENS, GITHUB_TOKEN or {credentials_path}")
    # Because a reviewer waits for the page, a README that no GitHub access token can read shows
    # the error at once, without the checks of the configured GitHub access tokens that an
    # unattended run waits through.
    client = readmes.Client(TokenPool(tokens), configured=[])

    def fetch(name: str, repository_id: int, revision: str) -> tuple[str, str]:
        readme = readmes.fetch(client, name, repository_id, revision)
        return readme.text or "", readme.sha
    return fetch


def review_server(review: Review, port: int) -> ThreadingHTTPServer:
    """The page and its four requests, on 127.0.0.1 only: the page, the disagreements, one
    README, and a decision."""
    page = REVIEW_PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def send(self, status: int, body: bytes, kind: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def json(self, status: int, value) -> None:
            self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

        def do_GET(self):  # noqa: N802 - the name http.server calls
            url = urlparse(self.path)
            if url.path == "/":
                self.send(200, page, "text/html")
            elif url.path == "/api/items":
                self.json(200, {"models": review.models, "criteria": list(CRITERIA),
                                "items": review.items()})
            elif url.path == "/api/readme":
                try:
                    rid = int(parse_qs(url.query)["id"][0])
                except (KeyError, ValueError):
                    self.json(400, {"error": "the request names no id"})
                    return
                self.json(200, review.readme(rid))
            else:
                self.json(404, {"error": f"nothing at {url.path}"})

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/api/decision":
                self.json(404, {"error": "nothing here"})
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                criteria = body.get("human_criteria") or []
                if not isinstance(criteria, list):
                    raise TypeError("human_criteria is a list of the criteria's names")
                item = review.decide(int(body["id"]), str(body.get("human_label", "")),
                                     str(body.get("human_note", "")), [str(c) for c in criteria])
            except (ValueError, KeyError, TypeError) as error:
                self.json(400, {"error": str(error)})
                return
            self.json(200, item)

        def log_message(self, format, *args):  # noqa: A002 - http.server's signature
            return

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def review(args, fetch_readme=None) -> int:
    """Serve the review of evaluation `args.evaluation` until it is stopped."""
    out = Path(args.evaluation)
    session = Review(out, args.cache_dir, fetch_readme or github_readme(args.credentials))
    server = review_server(session, args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    decided = sum(1 for row in session.rows if row["human_label"])
    logs.info(COMPONENT, f"Reviewing the {len(session.rows):,} disagreements of {out.name}, "
                         f"{decided:,} of them decided, at {url}. Each decision is written to "
                         f"{out / 'disagreements.csv'}. Stop with Ctrl+C.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    decided = sum(1 for row in session.rows if row["human_label"])
    logs.info(COMPONENT, f"{decided:,} of {len(session.rows):,} disagreements decided.")
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the labels of a model against the "
                                                 "labels of a second model.")
    parser.add_argument("--cache-dir", type=Path, default=label_cache.CACHE_DIR,
                        help="The cache of labels (default: %(default)s)")
    commands = parser.add_subparsers(dest="command", required=True)
    drawing = commands.add_parser("draw", help="Draw the evaluation sample")
    drawing.add_argument("--reference-model", required=True)
    drawing.add_argument("--sampling-frame", type=Path, default=SAMPLING_FRAME)
    drawing.add_argument("--evaluation-dir", type=Path, default=EVALUATION_DIR)
    drawing.add_argument("--margin", type=float, default=MARGIN)
    drawing.add_argument("--confidence", type=float, default=CONFIDENCE)
    drawing.add_argument("--seed", type=int, default=None,
                         help="The seed of the draw (default: a random one, recorded)")
    drawing.add_argument("--exclude", type=Path, nargs="*", default=None,
                         help="CSVs with an id column that names repositories to leave out of the "
                              "evaluation sample (default: the calibration sample, "
                              "../data/classification/calibration/calibration_sample.csv, and the "
                              "evaluation sample of every evaluation in --evaluation-dir, or "
                              "none when no path follows)")
    comparison = commands.add_parser("compare", help="Compare the two models' labels")
    comparison.add_argument("evaluation", type=Path, help="The evaluation's directory")
    comparison.add_argument("--second-model", default=endpoints.SECOND_MODEL.model,
                            help="The second model, whose labels are compared with the reference "
                                 "model's (default: %(default)s)")
    reviewing = commands.add_parser("review", help="Review the disagreements in the browser")
    reviewing.add_argument("evaluation", type=Path, help="The evaluation's directory")
    reviewing.add_argument("--port", type=int, default=REVIEW_PORT,
                           help="The port on 127.0.0.1 (default: %(default)s)")
    reviewing.add_argument("--no-browser", action="store_true",
                           help="Log the address without opening a browser")
    reviewing.add_argument("--credentials", type=Path, default=credentials.default_path(),
                           help="The credentials file, read after the environment (default: "
                                "%(default)s)")
    drawing_check = commands.add_parser("draw-check", help="Choose the repositories on which "
                                                          "to check new classification "
                                                          "parameters")
    drawing_check.add_argument("evaluation", type=Path, help="The reviewed evaluation's directory")
    drawing_check.add_argument("--agreements", type=int, required=True,
                               help="The number of repositories that both models agreed on "
                                    "to include")
    drawing_check.add_argument("--seed", type=int, default=None,
                               help="The seed of the draw (default: a random one, recorded)")
    checking = commands.add_parser("check", help="Compare each model's labels with the expected "
                                                 "labels, or label the check samples with one "
                                                 "model in several runs and compare those")
    checking.add_argument("evaluation", type=Path, nargs="+",
                          help="The directories of the reviewed evaluations")
    checking.add_argument("--models", nargs="+", default=None,
                          help="The models with the labels of their latest finished runs to "
                               "compare")
    checking.add_argument("--sampling-frame", type=Path, default=SAMPLING_FRAME,
                          help="The sampling frame, which limits the check to the repositories "
                               "of the check sample in it (default: %(default)s)")
    labeling = importlib.import_module("1_classify_repos").build_parser()
    checking.add_argument("--runs", type=int, default=0,
                          help="Label the check samples in this many runs with --model and "
                               "compare each run")
    for option in ("--model", "--provider", "--quantization", "--hugging-face-id",
                   "--reasoning-effort", "--temperature", "--top-p", "--seed",
                   "--openai-service-tier", "--summary-model", "--summary-effort",
                   "--summary-provider", "--summary-quantization", "--summary-hugging-face-id",
                   "--workers", "--credentials"):
        dest = next(action.dest for action in labeling._actions if option in action.option_strings)
        default = labeling.get_default(dest)
        checking.add_argument(option, dest=dest, default=default,
                              type=int if isinstance(default, int) else None,
                              help=f"As {option} of 1_classify_repos.py, for the runs of --runs "
                                   f"(default: %(default)s)")
    checking.add_argument("--endpoint-default-temperature", action="store_true",
                          help="As --endpoint-default-temperature of 1_classify_repos.py, for "
                               "the runs of --runs: their requests send neither a temperature, "
                               "a top_p nor a seed, so that the endpoint of an OpenRouter model "
                               "applies its own defaults. It takes neither --temperature, "
                               "--top-p nor --seed")
    checking.add_argument("--variant", default="",
                          help="The name of the runs in the record (default: the model, the "
                               "provider slug, the reasoning effort, and the temperature, the "
                               "top_p and the seed when the check names them, or "
                               f"{ENDPOINT_DEFAULT_TEMPERATURE} for runs without any of them)")
    checking.add_argument("--texts", default="",
                          help="What the runs check, for the record")
    checking.add_argument("--check-dir", type=Path, default=CHECK_DIR,
                          help="Where each run keeps its cache of labels (default: "
                               "%(default)s)")
    checking.add_argument("--check-date", default=label_cache.utc_now().date().isoformat(),
                          help="The date of the check, which names its runs and its record, "
                               "and with which a stopped check is resumed (default: today in "
                               "UTC)")
    args = parser.parse_args(argv)
    if args.command == "check" and not (args.runs or args.models):
        parser.error("check needs --models, or --runs with --model")
    if (args.command == "check" and args.endpoint_default_temperature
            and any(value is not None for value in (args.temperature, args.top_p, args.seed))):
        parser.error("--endpoint-default-temperature sends no temperature, top_p or seed, and "
                     "it takes neither --temperature, --top-p nor --seed")
    commands_by_name = {"draw": draw, "compare": compare, "review": review,
                        "draw-check": draw_check, "check": check_command}
    try:
        return commands_by_name[args.command](args)
    except label_cache.CacheError as error:
        logs.error(COMPONENT, f"The cache of labels cannot be used, because {error}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
