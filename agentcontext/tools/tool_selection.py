"""The measurement behind the choice of the tools: `python -m agentcontext tool-selection`.

Counts, in the file lists of a collection, the repositories and the owners with a root-level path
of each tool that `signatures.csv` lists, and writes the counts and the two cuts of their
distribution to `counts.json`. The dataset covers the five tools of the 2025 Stack Overflow
Developer Survey and the tools above the cut, and docs/heuristics.md states the result.

An owner is the account in a repository's name, so that an organization with many repositories
of one layout counts once for a tool. A signature path ending in `/` names a directory at the
repository root and matches every file below it, and any other path names a file at the root.
The counts are of root-level paths alone, so that the tools are counted alike: A nested
configuration of one tool and a vendored copy of another would otherwise count differently.

The cuts are over the tools of the categories `survey` and `agent` with at least one owner. The
largest step is the largest ratio between the owners of two tools that are neighbors in the
order of their owners, from the last tool of the survey down, and the tools above it are the
tools that the dataset adds. Head/tail breaks (Jiang 2013, "Head/tail breaks: A new
classification scheme for data with a heavy-tailed distribution", The Professional Geographer
65(3)) split the tools at the mean of their owners, again over the head, while the head is less
than `HEAD_SHARE` of the tools that the split divides.

Inputs:
  - the file lists of a collection, `<owner>§<repo>.ls-tree.xz`, the `git ls-tree -r -z`
    output that the cloning step keeps beside its tree
  - `data/collection/evaluation/tool-selection/signatures.csv`: `tool`, `category` (`survey`,
    `agent` or `review`), `paths` joined by `|`, `doc_url`, `doc_verified`, `notes`

Outputs:
  - `data/collection/evaluation/tool-selection/counts.json`

Exit codes: 0 when the counts were written, 1 when an input could not be read.
"""

from __future__ import annotations

import csv
import hashlib
import json
import lzma
import os
from pathlib import Path

from agentcontext import STAGE_DIR, cli, logs, repos

COMPONENT = "tool_selection"
DEFAULT_DIR = STAGE_DIR / "evaluation" / "tool-selection"
LIST_SUFFIX = ".ls-tree.xz"
CATEGORIES = ("survey", "agent", "review")
# The categories of the tools, over which the cuts are taken. The review products are
# left out, because they review a change that another tool or a person made.
CODING_CATEGORIES = ("survey", "agent")
# Jiang's criterion for another head/tail break: the head is a minority of the tools that it is
# split from.
HEAD_SHARE = 0.4


def load_signatures(path: Path) -> list:
    """The rows of the signatures file, each with its paths split, or raise ValueError."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"{path} has no rows")
    seen = set()
    for row in rows:
        if row["category"] not in CATEGORIES:
            raise ValueError(f"{path} gives the tool {row['tool']} the category "
                             f"{row['category']!r}, not one of {CATEGORIES}")
        if row["tool"] in seen:
            raise ValueError(f"{path} has a second row for the tool {row['tool']}")
        seen.add(row["tool"])
        row["paths"] = [p for p in row["paths"].split("|") if p]
        if not row["paths"]:
            raise ValueError(f"{path} gives the tool {row['tool']} no path")
    return rows


def listed_paths(path: Path) -> list:
    """The paths of one file list, the `git ls-tree -r -z` output that the cloning step keeps."""
    with lzma.open(path, "rt", encoding="utf-8", errors="surrogateescape") as f:
        return [entry.split("\t", 1)[-1] for entry in f.read().split("\0") if entry]


def tools_of(paths: list, signatures: list) -> set:
    """The tools with a root-level path among `paths`: a directory of a signature that a path is
    in, at the depth that the signature gives, or a file that it names."""
    directories, files = set(), set()
    for path in paths:
        parts = path.split("/")
        for depth in range(1, min(len(parts), 3)):
            directories.add("/".join(parts[:depth]) + "/")
        if len(parts) <= 3:
            files.add(path)
    return {row["tool"] for row in signatures
            if any(signature in (directories if signature.endswith("/") else files)
                   for signature in row["paths"])}


def head_tail_breaks(owners: dict) -> list:
    """The levels of the head/tail breaks of {tool: owners}: each with the mean that it splits
    at and the tools above the mean, while the head is a minority of the tools that it splits."""
    levels, current = [], dict(owners)
    while len(current) > 1:
        mean = sum(current.values()) / len(current)
        head = {t: n for t, n in current.items() if n > mean}
        if not head:
            break
        levels.append({"mean": round(mean, 1), "tools": len(current),
                       "head": sorted(head, key=lambda t: (-head[t], t))})
        if len(head) >= HEAD_SHARE * len(current) or len(head) < 2:
            break
        current = head
    return levels


def steps(ordered: list) -> list:
    """The ratio between the owners of each tool and of the next one, in the order given."""
    return [{"above": a, "below": b, "ratio": round(na / nb, 2)}
            for (a, na), (b, nb) in zip(ordered, ordered[1:]) if nb]


def count(file_lists: Path, signatures: list) -> dict:
    """The repositories and the owners of each tool over every file list of the directory."""
    by_tool = {row["tool"]: set() for row in signatures}
    lists = sorted(name for name in os.listdir(file_lists) if name.endswith(LIST_SUFFIX))
    for name in lists:
        repo = repos.dir_name_to_repo(name[: -len(LIST_SUFFIX)])
        for tool in tools_of(listed_paths(file_lists / name), signatures):
            by_tool[tool].add(repo)
    return {"file_lists": len(lists), "repositories": {t: len(r) for t, r in by_tool.items()},
            "owners": {t: len({r.split("/", 1)[0].lower() for r in rs})
                       for t, rs in by_tool.items()}}


def cuts(counts: dict, signatures: list) -> dict:
    """The largest step below the tools of the survey and the head/tail breaks, over the tools
    of the coding categories with an owner."""
    category = {row["tool"]: row["category"] for row in signatures}
    coding = {t: n for t, n in counts["owners"].items()
              if category[t] in CODING_CATEGORIES and n > 0}
    ordered = sorted(coding.items(), key=lambda item: (-item[1], item[0]))
    order = [t for t, _ in ordered]
    survey = [i for i, t in enumerate(order) if category[t] == "survey"]
    below = steps(ordered[survey[-1]:]) if survey else []
    # The first of two equal ratios, the higher step, is the cut. Without a tool of the survey
    # or a tool below the last of them there is no cut, and no tool is added.
    largest = max(below, key=lambda step: step["ratio"]) if below else None
    added = order[survey[-1] + 1:order.index(largest["below"])] if largest else []
    return {"order": order,
            "steps": steps(ordered),
            "largest_step_below_the_survey": largest,
            "added": added,
            "head_tail_breaks": head_tail_breaks(coding),
            "head_tail_breaks_outside_the_survey": head_tail_breaks(
                {t: n for t, n in coding.items() if category[t] != "survey"})}


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description="Count the repositories and the owners with a "
                                            "root-level path of each tool in a collection's file "
                                            "lists, and write the counts and their cuts.")
    parser.add_argument("--file-lists", type=Path, required=True,
                        help="The directory of a collection's file lists, data/file_lists in "
                             "its data root")
    parser.add_argument("--signatures", type=Path, default=DEFAULT_DIR / "signatures.csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_DIR / "counts.json")
    args = parser.parse_args(argv)
    try:
        signatures = load_signatures(args.signatures)
        if not args.file_lists.is_dir():
            raise ValueError(f"{args.file_lists} is not a directory")
        counts = count(args.file_lists, signatures)
    except (OSError, ValueError, EOFError, lzma.LZMAError) as exc:
        logs.error(COMPONENT, f"Could not count the tools ({exc}).")
        return 1
    result = {"signatures": args.signatures.name,
              "signatures_sha256": hashlib.sha256(args.signatures.read_bytes()).hexdigest(),
              **counts, "cuts": cuts(counts, signatures)}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    largest = result["cuts"]["largest_step_below_the_survey"]
    logs.info(COMPONENT, f"Counted {len(signatures)} tools over {counts['file_lists']:,} file "
                         f"lists, and wrote {args.output}.")
    if largest:
        added = result["cuts"]["added"]
        logs.info(COMPONENT, f"The largest step below the tools of the survey is from "
                             f"{largest['above']} to {largest['below']}, with a ratio of "
                             f"{largest['ratio']}. "
                             + (f"The dataset adds the tools between the last tool of the survey "
                                f"and the step ({', '.join(added)})." if added
                                else "No tool is between the last tool of the survey and the "
                                     "step, and the dataset adds none."))
    else:
        logs.info(COMPONENT, "No tool with an owner is below the tools of the survey.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
