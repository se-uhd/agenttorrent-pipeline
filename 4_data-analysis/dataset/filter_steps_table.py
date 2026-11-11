#!/usr/bin/env python3
"""Filter steps table: the steps of the repository filtering as a LaTeX table for the paper.

Renders, from the `_metadata` that the filter of the sampling stage records in the
sampling frame (its `stages` list), the retrieval's repository count, one line per filter step
with what it removed and what remained, and the size of the sampling frame. A step that
removed nothing is kept as a LaTeX comment, so that the printed lines are numbered without
gaps. Every label is built from the step's key and the values that the filter recorded, such
as the thresholds of the activity step or the languages that the language step left out because
the end date of the sampling window does not cover them, rather than parsed out of a
description.

Input:
  ../../data/sampling/ghs_filtered.json, which stops the script where it differs from the
      ghs_filtered.json.7z that git tracks beside it

Output:
  filter_steps_table.tex

Usage:
    python3 dataset/filter_steps_table.py [ghs_filtered.json] [filter_steps_table.tex]
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import SAMPLING_DIR, checked_copy, latex_text, output_dir
from agentcontext import logs

COMPONENT = "dataset_filter_steps_table"

OUTPUT_DIR = output_dir(__file__)
DEFAULT_INPUT = SAMPLING_DIR / "ghs_filtered.json"
DEFAULT_OUTPUT = OUTPUT_DIR / "filter_steps_table.tex"


def format_latex_number(n: int) -> str:
    """A number with LaTeX thousand separators: 187304 becomes $187{,}304$."""
    return "$" + f"{n:,}".replace(",", "{,}") + "$"


def _threshold(value: float) -> str:
    """A threshold as printed: A quantile of integers can fall between two of them."""
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


# The rows that the gone step removes beside the repositories that GitHub does not have, each
# with the label that names them: the rows that the GitHub query left unresolved, which no
# response shows to be gone from GitHub, the repositories that GitHub found without an id, and
# the repositories that GitHub created after the window under a name that GHS crawled for an
# earlier one.
GONE_PARTS = (("unresolved", "Unresolved"), ("without_id", "Without ID"),
              ("created_after_window", "Created After Window"))


def languages_label(values: dict) -> str:
    """The label of the language step: the number of languages kept, and the languages that the
    step left out because the end date of the sampling window does not cover them, which the
    filter records under `left_out`. Such a language would otherwise rank among the languages
    kept, and a label of the number alone would state that the step kept it. A step that keeps
    every language records no number and is labeled by the languages left out alone."""
    left_out = values.get("left_out")
    names = [latex_text(name) for name in sorted(left_out)] if isinstance(left_out, dict) else []
    parts = []
    if "top" in values or values.get("languages") or not names:
        parts.append(f"Non-Top-{values.get('top', len(values.get('languages', [])))} Languages")
    if names:
        joined = (" and ".join(names) if len(names) < 3
                  else f"{', '.join(names[:-1])}, and {names[-1]}")
        parts.append(f"{joined} Left Out by End Date")
    return " / ".join(parts)


def step_label(step: dict) -> str:
    """The LaTeX label of one filter step, from its key and recorded values. A step of an
    earlier sampling frame, which records a description and no key, is named by that
    description, because no run can write that sampling frame again."""
    if "key" not in step:
        return latex_text(step["description"])
    key, values = step["key"], step.get("values") or {}
    if key == "languages":
        return languages_label(values)
    if key == "activity":
        parts = [f"{name.title()} $<{_threshold(values[f'{name}_min'])}$"
                 for name in ("commits", "watchers") if f"{name}_min" in values]
        return " $\\lor$ ".join(parts) or "Low Activity"
    if key == "gone":
        return " / ".join(["Gone from GitHub"]
                          + [f"{format_latex_number(values[name])} {label}"
                             for name, label in GONE_PARTS if values.get(name)])
    return {"duplicates": "Duplicate Repos",
            "cleanup": "Archived / Disabled / Locked / Forked",
            "missing_data": "Missing Data",
            "licenses": "Non-OSI / Excluded Licenses",
            "educational": "Educational / Resource Repos",
            # The step kept, of the repositories at one head commit, the one that GitHub created
            # first. A mirror is a classification criterion and a flag of GitHub, and a
            # repository that GitHub marks as one stays in the sampling frame.
            "mirrors": "Later Copies at a Shared Head Commit"}.get(key, latex_text(key))


def render(meta: dict) -> list:
    """The table's lines."""
    lines = [
        r"\begin{table}[H]",
        r"\centering",
        r"\begin{tabular}{lrr}",
        r"\toprule",
        r"\textbf{Filtering Step} & $\Delta$ \textbf{Removed} & \textbf{Remaining} \\",
        r"\midrule",
        rf"\textit{{Initial Dataset}} & -- & {format_latex_number(meta['input_source']['item_count'])} \\",
    ]
    number = 0
    for step in meta["stages"]:
        cells = (f"{step_label(step)} & {format_latex_number(step['removed'])} & "
                 f"{format_latex_number(step['remaining'])} \\\\")
        if step["removed"] == 0:
            lines.append(rf"%\hphantom{{(1)\ }}{cells}")
            continue
        number += 1
        lines += [r"\midrule", f"({number}) {cells}"]
    final = format_latex_number(meta["final_count"])[1:-1]
    lines += [
        r"\midrule",
        r"\midrule",
        rf"\textbf{{Metadata-Filtered Dataset}} & -- & $\mathbf{{{final}}}$ \\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\caption{Metadata filtering steps applied to the baseline GHS dataset, with removed and remaining repositories per step.}",
        r"\label{tab:filtering-table}",
        r"\end{table}",
    ]
    return lines


def main(argv: list | None = None) -> int:
    """Render the table from the sampling frame and write it."""
    argv = sys.argv[1:] if argv is None else argv
    input_path = argv[0] if argv else DEFAULT_INPUT
    output_path = argv[1] if len(argv) > 1 else DEFAULT_OUTPUT
    try:
        with open(checked_copy(Path(input_path)), encoding="utf-8") as f:
            meta = json.load(f)["_metadata"]
        lines = render(meta)
    except (OSError, ValueError, KeyError, TypeError) as error:
        logs.error(COMPONENT, f"Could not read the filter steps of {input_path} ({error!r}).")
        return 1
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logs.info(COMPONENT, f"Wrote {output_path} ({len(lines):,} lines).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
