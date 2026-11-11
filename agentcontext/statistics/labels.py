"""Computations over the runs of the classification in `data/classification/labels/` and over
the label evaluations.

The decisions of a run, `<run>.decisions-NNN.jsonl.xz`, hold one JSON object per repository with
the model's fields, its tool calls under `tools`, its requests and rounds under `usage`, the
limits that it reached under `limits_reached` and the result of the fork heuristic under `fork`.
The labels of a run are in `<run>.labels.csv.xz`, the expected labels in the check samples of
the label evaluations, and the label of each model in the labels CSV of an evaluation.

The files of a run are tracked once the run has finished, and the directory is never
rewritten, so a statistic of a run stays reproducible from its files.

The decisions of a run are read once for a `Files`, their parts side by side, and each decision
is kept with what the computations count: its fields, its fork result, the size of its line and
each tool call with its round, its output and its arguments.
"""

import json
import re
from typing import NamedTuple

from agentcontext.statistics import Files, Unavailable, checkout
from agentcontext.statistics.retrievals import CACHE, github_responses
from agentcontext.statistics.tables import holds

LABELS = "data/classification/labels"
DEFINITE = ("true", "false")
# A link of a repository's own file or directory on GitHub at a ref: the owner, the repository
# and the ref, in the forms that `resolve_link` of the classification tools reads.
OWN_LINK = re.compile(r"^https?://(?:github\.com/([^/]+)/([^/]+)/(?:blob|tree|raw)|"
                      r"raw\.githubusercontent\.com/([^/]+)/([^/]+))/([^/?#]+)")


class Call(NamedTuple):
    """A tool call of a decision."""
    tool: str | None
    round: int | None
    output: str
    arguments: object


class Decision(NamedTuple):
    """What the computations read of a decision."""
    name: str
    revision: str | None
    outcome: str
    described_as_mirror: bool
    fork_of: bool
    upstream_on_github: bool
    limits_reached: tuple
    readme_path: str
    usage: dict
    fork: dict | None
    size: int
    calls: tuple


def _decision(decision: dict, size: int) -> Decision:
    """What the computations read of a decision, with the size of its line in bytes."""
    mirror = ((decision.get("exclusions") or {}).get("mirror") or {}).get("described_as_mirror")
    fork = decision.get("fork")
    calls = []
    for call in decision.get("tools") or ():
        output = call.get("output", "")
        calls.append(Call(call.get("tool"), call.get("round"),
                          output if isinstance(output, str) else str(output),
                          call.get("arguments")))
    return Decision(
        decision.get("name"), decision.get("revision"), decision.get("outcome") or "",
        str(mirror).lower() == "true", bool(decision.get("fork_of")),
        bool(fork.get("upstream_repository")) if isinstance(fork, dict) else False,
        tuple(decision.get("limits_reached") or ()), decision.get("readme_path") or "",
        decision.get("usage") or {}, fork if isinstance(fork, dict) else None, size,
        tuple(calls))


def decisions_of(files: Files, run: str, directory: str = LABELS) -> list:
    """Every decision of a run, from all of its numbered files."""
    def read() -> list:
        parts = files.glob(f"{directory}/{run}.decisions-*.jsonl.xz")
        if not parts:
            raise Unavailable(f"{directory} has no decisions of the run {run}")
        # Every part starts to decompress at once, and the size of a line counts its line feed.
        found = []
        for lines in [files.lines(part) for part in parts]:
            for line in lines:
                if line.strip():
                    found.append(_decision(json.loads(line), len(line)))
        return found
    return files.memo(("decisions", directory, run), read)


def _at(value: object, pointer: str) -> object:
    """The value at a JSON pointer, or None where there is none."""
    for part in pointer.split("/")[1:]:
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def matching(fork: dict, conditions: dict) -> bool:
    """Whether a fork result meets every condition: {pointer: [operator, value]}, where the value
    is a number, a string, a boolean, null or {"pointer": POINTER} for another value of the same
    result. An ordering comparison with a missing value fails."""
    for pointer, (name, wanted) in conditions.items():
        if isinstance(wanted, dict):
            wanted = _at(fork, wanted["pointer"])
        if not holds(_at(fork, pointer), name, wanted):
            return False
    return True


@checkout
def decisions(files: Files, run: str, outcome: str | None = None,
              described_as_mirror: bool | None = None, fork_of: bool | None = None,
              upstream_on_github: bool | None = None, limit_reached: str | None = None,
              fork: dict | None = None, directory: str = LABELS) -> int:
    """The decisions of a run that every given condition selects."""
    # The conditions: the outcome, such as `labeled`; with or without a mirror description, a
    # named upstream in `fork_of` or an upstream that GitHub resolved; a limit, such as
    # `linked_pages`, among the limits reached; and a fork result that meets every condition of
    # `matching`, such as more own commits than a number and fewer own authors than another.
    wanted = {"outcome": outcome, "described_as_mirror": described_as_mirror,
              "fork_of": fork_of, "upstream_on_github": upstream_on_github}
    return sum(1 for decision in decisions_of(files, run, directory)
               if all(value is None or getattr(decision, field) == value
                      for field, value in wanted.items())
               and (limit_reached is None or limit_reached in decision.limits_reached)
               and (fork is None or (decision.fork is not None
                                     and matching(decision.fork, fork))))


def _default_branches(files: Files, retrieval: str, cache: str) -> dict:
    """{lower-cased name: default branch} of GitHub's responses of a retrieval."""
    def read() -> dict:
        branches = {}
        for response in github_responses(files, retrieval, cache):
            repository = response.get("repository") or {}
            if repository.get("name"):
                branches[repository["name"].lower()] = repository.get("defaultBranch")
        return branches
    return files.memo(("default branches", cache, retrieval), read)


def _at_other_ref(decision: Decision, call: Call, branches: dict) -> bool:
    """Whether a call read the repository's own file at a ref that is neither the revision of
    the decision nor the default branch."""
    arguments = call.arguments or {}
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    link = OWN_LINK.match(str(arguments.get("url", "")))
    if link is None:
        return False
    owner, name = link.group(1) or link.group(3), link.group(2) or link.group(4)
    if f"{owner}/{name}".lower() != decision.name.lower():
        return False
    return link.group(5) not in (decision.revision, branches.get(decision.name.lower()))


@checkout
def calls(files: Files, run: str, tool: str | None = None, output: str | None = None,
          readme: str | None = None, other_ref: str | None = None, distinct: str = "call",
          directory: str = LABELS, cache: str = CACHE) -> int:
    """The tool calls of a run, or with `distinct` `repository` their repositories, that every
    given condition selects."""
    # The conditions: the tool; an output that the regular expression `output` matches; a
    # decision with a README path that the regular expression `readme` matches; and with
    # `other_ref`, a link of the repository's own file on GitHub at a ref that is neither the
    # revision of the decision nor the default branch that GitHub's responses of the retrieval
    # `other_ref` give.
    if distinct not in ("call", "repository"):
        raise ValueError(f"distinct is call or repository, not {distinct!r}")
    outputs = re.compile(output) if output is not None else None
    readmes = re.compile(readme) if readme is not None else None
    branches = _default_branches(files, other_ref, cache) if other_ref is not None else None
    names = []
    for decision in decisions_of(files, run, directory):
        if readmes is not None and not readmes.search(decision.readme_path):
            continue
        for call in decision.calls:
            if tool is not None and call.tool != tool:
                continue
            if outputs is not None and not outputs.search(call.output):
                continue
            if branches is not None and not _at_other_ref(decision, call, branches):
                continue
            names.append(decision.name)
    return len(names) if distinct == "call" else len(set(names))


@checkout
def calls_per_decision(files: Files, run: str, how: str, tool: str | None = None,
                       tool_round: int | None = None, limit_reached: str | None = None,
                       directory: str = LABELS) -> int:
    """The fewest (`min`) or most (`max`) calls of a tool, in a round of tool calls, in one
    decision of a run, among the decisions that reached the limit `limit_reached`."""
    if how not in ("min", "max"):
        raise ValueError(f"how is min or max, not {how!r}")
    counts = [sum(1 for call in decision.calls
                  if (tool is None or call.tool == tool)
                  and (tool_round is None or call.round == tool_round))
              for decision in decisions_of(files, run, directory)
              if limit_reached is None or limit_reached in decision.limits_reached]
    if not counts:
        raise Unavailable(f"No decision of run {run} reached the limit {limit_reached}")
    return min(counts) if how == "min" else max(counts)


@checkout
def usage(files: Files, run: str, field: str, directory: str = LABELS) -> int:
    """The sum of a field of `usage`, such as `tool_rounds`, over the decisions of a run."""
    return sum(decision.usage.get(field, 0) for decision in decisions_of(files, run, directory))


@checkout
def bytes_per_decision(files: Files, run: str, directory: str = LABELS) -> float:
    """The mean size in bytes of the line of a decision of a run, uncompressed."""
    sizes = [decision.size for decision in decisions_of(files, run, directory)]
    if not sizes:
        raise Unavailable(f"Run {run} has no decision")
    return sum(sizes) / len(sizes)


@checkout
def fork_value(files: Files, run: str, name: str, pointer: str,
               directory: str = LABELS) -> int | float:
    """The number at a JSON pointer of the fork result of one repository in a run, such as its
    own commits."""
    found = [decision.fork for decision in decisions_of(files, run, directory)
             if decision.name == name and decision.fork is not None]
    if len(found) != 1:
        raise Unavailable(f"The run {run} has {len(found)} fork results of {name}, not one")
    value = _at(found[0], pointer)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Unavailable(f"The fork result of {name} in the run {run} has {value!r} at {pointer}")
    return value


def labels_of(files: Files, run: str, directory: str = LABELS) -> dict:
    """{id: label} of the labels of a run."""
    path = f"{directory}/{run}.labels.csv.xz"
    return files.memo(("labels", path), lambda: {row["id"]: row["label"]
                                                 for row in files.rows(path)})


@checkout
def agreement(files: Files, alike: bool, runs: list | None = None, path: str | None = None,
              models: list | None = None, where: dict | None = None,
              directory: str = LABELS) -> int:
    """The repositories that every run of `runs`, or every model of `models` in the labels CSV
    of an evaluation at `path`, labels, and labels alike, or with `alike` false differently."""
    if (runs is None) == (path is None):
        raise ValueError("agreement needs either runs or the labels CSV of an evaluation")
    if runs is not None:
        labels = [labels_of(files, run, directory) for run in runs]
        shared = set.intersection(*(set(run) for run in labels)) if labels else set()
        given = [{run[repository] for run in labels} for repository in shared]
    else:
        by_id: dict = {}
        for row in files.rows(path):
            if row["model"] in models and all(row[column] == value
                                              for column, value in (where or {}).items()):
                by_id.setdefault(row["id"], {})[row["model"]] = row["label"]
        given = [set(by_model.values()) for by_model in by_id.values()
                 if len(by_model) == len(models)]
    return sum(1 for labels in given if (len(labels) == 1) == alike)
