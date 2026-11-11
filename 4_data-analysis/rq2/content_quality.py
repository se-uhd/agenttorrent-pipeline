#!/usr/bin/env python3
"""Line counts of the configuration artifacts of every mechanism, and a check that none is empty.

For each mechanism (Context Files, Rules, Skills, Commands, Subagents, Settings, MCP, Hooks,
Plugins, LSP), reads the files that its CSV lists from the validated tree and counts them and
their length in lines: the mean, the median, the quartiles, nine bins and the number of files
over 500 lines. A listed file that is the collection's marker of a symlink is read as the file
that the marker names in the repository, as the detection step reads it for the `language`
column (`detection.measured_artifact`). A context file is the exception: The detection step
publishes it as a reference to the file that it links to, and it counts as its marker, also
where the tree holds the link as a real symlink (`detection.artifact_text`). The marker of a
link with a target that the tree lacks counts in the total without a line count, because the
detection step publishes its `language` blank. A file that is no text, such as a file that
git-crypt encrypted (`detection.no_text`), counts the same way, and the detection step publishes
its `line_count` blank.

The table contains no count of empty files. The collected file cleanup deletes every empty file
before detection, by the test that `is_empty_file` applies here: whitespace, or JSON that
parses to an empty object or array. It keeps the empty files of
`collected_file_cleanup.kept_when_empty`: a file that a GitHub Agentic Workflow imports, because
gh-aw does not compile a workflow without a required import, the prompt file of an agent action,
a planning document or its anchor, and a CLAUDE file that determines whether Claude Code reads an
AGENTS.md (`imports.deciding_claude_files`). The detection step lists such a file when its path
is a configuration artifact, such as an agent in `.github/agents/`, the
`.cursor/commands/van.md` that anchors the tasks of cursor-memory-bank or an empty CLAUDE.md
beside an AGENTS.md. Such a file counts with 0 lines and in no bin, and
the script finds it with the cleanup's function. A count of the other empty files would be zero
by construction, and the cleanup's deletion log records them. A listed file that is empty and
that the cleanup does not keep when empty means that the tree is not the tree that the CSVs
describe. The script then writes nothing, as it writes nothing when it cannot read a listed
file.

Inputs:
  ../../data/collection/output/<mechanism>.csv  (for each mechanism)
  ../../data/collection/data/validated/  (the validated tree, which
      docs/development.md#handover-working-copy shows how to rebuild)

Outputs (in ../../data/analysis/rq2/):
  content_quality.csv
  content_quality.tex
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Paths
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, VALIDATED_DIR, latex_text, output_dir

from agentcontext import heuristics, logs
from agentcontext.repos import repo_name_to_dir
from agentcontext.steps import collected_file_cleanup, detection

# The component of this script's log messages in the pipeline's one log format,
# `LEVEL component: message`. agentcontext/logs.py and docs/logs.md define the format.
COMPONENT = "rq2_content_quality"

OUTPUT_DIR = output_dir(__file__)

# The mechanism CSVs: (csv_filename, file_column, label)
ARTIFACT_TYPES = [
    ("context-files.csv", "context_file", "Context files"),
    ("rules.csv",         "rule",         "Rules"),
    ("skills.csv",        "skills.md",    "Skills"),
    ("commands.csv",      "command",      "Commands"),
    ("subagents.csv",     "subagent",     "Subagents"),
    ("settings.csv",      "settings",     "Settings"),
    ("mcp.csv",           "mcp",          "MCP"),
    ("hooks.csv",         "hook",         "Hooks"),
    ("plugins.csv",        "plugin",       "Plugins"),
    ("lsp.csv",           "lsp",          "LSP"),
]

# The mechanism of each mechanism CSV, which decides the file that a row's line count describes
# (`detection.measured_artifact`).
MECHANISM_OF_CSV = {filename: mechanism
                    for mechanism, (filename, _column) in heuristics.MECHANISM_CSVS.items()}

# What a mechanism's statistics contain beyond the columns of the CSV. The files that could not
# be read and the empty files that the collected file cleanup deletes each stop the run before a
# table is written. The empty files that the cleanup keeps, the markers of links with a target
# that the tree lacks and the files that are no text are logged.
CHECKED_ONLY = ("unread", "empty", "empty_kept", "target_absent", "no_text")

# The bins of the line count of a nonempty file: from each low to the high beside it, and from
# the last low on without a bound. Each bin is a label, its low and its high.
LINE_BIN_LOWS = (1, 6, 11, 21, 51, 101, 201, 501, 1001)
LINE_BIN_HIGHS = (5, 10, 20, 50, 100, 200, 500, 1000)
LINE_BINS = ([(f"{low}--{high}", low, high) for low, high in zip(LINE_BIN_LOWS, LINE_BIN_HIGHS)]
             + [(f"{LINE_BIN_LOWS[-1]}+", LINE_BIN_LOWS[-1], float("inf"))])
# The line count above which a nonempty file counts as long, in the column gt_500 and in the
# LaTeX table.
LONG_FILE_LINES = 500


def is_empty_file(content: str | None, filepath: str) -> bool:
    """Check whether a file is empty or contains no meaningful configuration."""
    if not content or not content.strip():
        return True

    ext = os.path.splitext(filepath)[1].lower()

    if ext == ".json":
        # A file nested deeper than the parser reads raises RecursionError. The collected file
        # cleanup and the detection step keep such a file, and it counts as content here too, as
        # in the cleanup's test.
        try:
            data = json.loads(content)
            if data in ({}, []):
                return True
        except (ValueError, RecursionError):
            pass
    elif ext == ".toml":
        if not content.strip():
            return True

    return False


def kept_when_empty(repo_name: str) -> set:
    """The repository-relative paths of the files that the collected file cleanup keeps even when
    they are empty, read from the validated tree with the cleanup's function."""
    return collected_file_cleanup.kept_when_empty(VALIDATED_DIR / repo_name_to_dir(repo_name))


def measured_file(repo_name: str, filepath: str, mechanism: str) -> str | None:
    """The path, relative to the repository, of the file that gives a listed
    configuration artifact its line count: the file that a marker at `filepath` names, except
    for a context file, and None for the marker of a link with a target that the tree lacks
    (`detection.measured_artifact`)."""
    return detection.measured_artifact(VALIDATED_DIR / repo_name_to_dir(repo_name), mechanism,
                                       filepath)


def read_file(repo_name: str, filepath: str) -> str | None:
    """Read a file from the validated tree. Returns content or None."""
    full_path = VALIDATED_DIR / repo_name_to_dir(repo_name) / filepath
    if not full_path.is_file():
        return None
    try:
        return full_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def analyze_artifact_type(csv_name: str, file_col: str, label: str) -> dict:
    """Analyze one mechanism. Returns a dict of summary stats."""
    csv_path = DATA_DIR / csv_name

    df = pd.read_csv(csv_path)
    total = len(df)
    mechanism = MECHANISM_OF_CSV[csv_name]

    # Always read files from VALIDATED_DIR to compute line counts and emptiness
    line_counts = []
    is_empty_list = []
    missing = 0
    empty_kept = 0
    target_absent = 0
    no_text = 0

    for _, row in df.iterrows():
        repo_name = row["repo_name"]
        filepath = measured_file(repo_name, row[file_col], mechanism)
        if filepath is None:
            # The marker's one line is a path rather than the text of the skill, agent, command
            # or rule, and the detection step publishes the row's `language` blank. The row
            # counts in `total` with no line count, as an unread file does, but the run goes on:
            # The tree is the tree that the CSVs describe, and the collector never copies a
            # target outside the repository.
            target_absent += 1
            line_counts.append(np.nan)
            is_empty_list.append(False)
            continue
        repository = VALIDATED_DIR / repo_name_to_dir(repo_name)
        # The text that the detection step measures (`detection.artifact_text`), which decodes
        # UTF-16 too and reads a context file that is a real symlink as its marker. Read as
        # UTF-8, UTF-16 text gives each half of a CRLF a line break of its own and counts every
        # line twice.
        text = detection.artifact_text(repository, mechanism, filepath)
        if text is None and detection.no_text(repository / filepath):
            # Lines read from such bytes would count noise, such as git-crypt's ciphertext of a
            # CLAUDE.md read as text.
            no_text += 1
            line_counts.append(np.nan)
            is_empty_list.append(False)
            continue
        if text is None:
            missing += 1
            line_counts.append(np.nan)
            is_empty_list.append(False)
            continue

        # The emptiness test reads the file as the cleanup's `is_empty_file` does: in UTF-8,
        # through a link, and a link without a target as a file that is not empty.
        content = read_file(repo_name, filepath)
        empty = content is not None and is_empty_file(content, filepath)
        # The set is read only for an empty file, the one file that it decides.
        kept = empty and filepath in kept_when_empty(repo_name)
        empty_kept += kept
        # An empty file that the cleanup keeps has no line of configuration, whatever
        # whitespace it contains. The lines of any other file are counted as the detection step
        # counts them (`detection.split_lines`).
        line_counts.append(0 if kept else len(detection.split_lines(text)))
        is_empty_list.append(empty)

    if missing > 0:
        logs.warning(COMPONENT, f"Could not read {missing:,} of the {total:,} files that "
                                f"{csv_name} lists from {VALIDATED_DIR}.")

    df["_line_count"] = line_counts
    df["_is_empty"] = is_empty_list

    # A file that could not be read has no line count, and `main` writes no table while any
    # file is unread, or empty and outside the set that the cleanup keeps when empty.
    lc = df["_line_count"].dropna()

    # A mechanism with no files has no line count, so its statistics are blank rather than 0.0.
    stats = {
        "type": label,
        "total": total,
        "lc_mean": round(lc.mean(), 1) if len(lc) else np.nan,
        "lc_median": round(lc.median(), 1) if len(lc) else np.nan,
        "lc_q1": round(lc.quantile(0.25), 1) if len(lc) else np.nan,
        "lc_q3": round(lc.quantile(0.75), 1) if len(lc) else np.nan,
    }

    # Line count bins (only for non-empty files)
    lc_nonempty = lc[~df["_is_empty"]]
    for bin_label, lo, hi in LINE_BINS:
        count = int(((lc_nonempty >= lo) & (lc_nonempty <= hi)).sum())
        stats[f"bin_{bin_label}"] = count

    # Number of nonempty files with more than LONG_FILE_LINES lines.
    stats["gt_500"] = int((lc_nonempty > LONG_FILE_LINES).sum())
    stats["unread"] = missing
    stats["empty"] = int(df["_is_empty"].sum()) - empty_kept
    stats["empty_kept"] = empty_kept
    stats["target_absent"] = target_absent
    stats["no_text"] = no_text

    return stats


def print_summary_table(results: list[dict]) -> None:
    """Log the two summary tables."""
    logs.info(COMPONENT, "Configuration artifacts and their length in lines, per mechanism.")

    header = (f"{'Mechanism':<16} {'Total':>6} {'Mean':>8} {'Median':>8} {'Q1':>8} {'Q3':>8}")
    logs.info(COMPONENT, header)
    logs.info(COMPONENT, "-" * len(header))
    for r in results:
        logs.info(COMPONENT, f"{r['type']:<16} {r['total']:>6,} {r['lc_mean']:>8.1f} "
                             f"{r['lc_median']:>8.1f} {r['lc_q1']:>8.1f} {r['lc_q3']:>8.1f}")
    logs.info(COMPONENT, "-" * len(header))
    logs.info(COMPONENT, f"{'TOTAL':<16} {sum(r['total'] for r in results):>6,}")

    # Line count bins
    bin_labels = [label for label, _low, _high in LINE_BINS]
    header = f"{'Mechanism':<16}" + "".join(f" {b:>8}" for b in bin_labels) + f" {'gt_500':>8}"
    logs.info(COMPONENT, "Non-empty configuration artifacts by length in lines.")
    logs.info(COMPONENT, header)
    logs.info(COMPONENT, "-" * len(header))
    for r in results:
        logs.info(COMPONENT, f"{r['type']:<16}" + "".join(f" {r[f'bin_{b}']:>8,}" for b in bin_labels)
                  + f" {r['gt_500']:>8,}")


def write_csv(results: list[dict]) -> None:
    """Write results to content_quality.csv."""
    out_path = OUTPUT_DIR / "content_quality.csv"
    # `unread` and `empty` are zero in every table written, and `main` logs `empty_kept`,
    # `target_absent` and `no_text`. The table's columns are the counts and the line counts.
    df = pd.DataFrame(results).drop(columns=list(CHECKED_ONLY), errors="ignore")
    df.to_csv(out_path, index=False)
    logs.info(COMPONENT, f"Wrote {out_path.name}.")


def _lines(value: float) -> str:
    """A line count as the table writes it: a whole number without decimals, else one."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def write_latex(results: list[dict]) -> None:
    """Write the dataset release content-quality table to content_quality.tex."""
    tex_lines = [
        r"\begin{table}[t]",
        r"\centering",
        r"\small",
        r"\caption{Configuration artifacts per mechanism and their length in lines.}",
        r"\label{tab:content-quality}",
        r"\begin{tabular}{lrrr}",
        r"\toprule",
        (r"\textbf{Mechanism} & \textbf{Configuration artifacts} & "
         rf"\textbf{{Lines, median (Q1--Q3)}} & \textbf{{$>${LONG_FILE_LINES} lines}} \\"),
        r"\midrule",
    ]

    for r in results:
        lines = ("--" if pd.isna(r["lc_median"]) else
                 f"{_lines(r['lc_median'])} ({_lines(r['lc_q1'])}--{_lines(r['lc_q3'])})")
        tex_lines.append(f"\\textbf{{{latex_text(r['type'])}}} & {r['total']:,} & {lines} & "
                         f"{r['gt_500']:,} \\\\")

    total = sum(r["total"] for r in results)
    gt_500 = sum(r["gt_500"] for r in results)
    tex_lines += [
        r"\midrule",
        f"\\textbf{{Total}} & \\textbf{{{total:,}}} & & \\textbf{{{gt_500:,}}} \\\\",
        r"\bottomrule",
        r"\end{tabular}",
        r"\end{table}",
    ]

    tex = "\n".join(tex_lines) + "\n"
    out_path = OUTPUT_DIR / "content_quality.tex"
    out_path.write_text(tex)
    logs.info(COMPONENT, f"Wrote {out_path.name}.")


def per_mechanism(counts: dict) -> str:
    """The counts by mechanism label as the log states them, such as `2 in Skills and 1 in
    Rules`."""
    parts = [f"{count:,} in {label}" for label, count in counts.items()]
    return parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"


def main() -> None:
    """Analyze every mechanism and write the CSV and the LaTeX table."""
    if not VALIDATED_DIR.is_dir():
        logs.error(COMPONENT, f"The validated tree {VALIDATED_DIR} is missing. Extract a working "
                              "copy of a handover and run python3 -m agentcontext "
                              "collected-file-cleanup (docs/development.md#handover-working-copy).")
        sys.exit(1)

    results = []
    for csv_name, file_col, label in ARTIFACT_TYPES:
        logs.info(COMPONENT, f"Analyzing {label} ({csv_name})...")
        stats = analyze_artifact_type(csv_name, file_col, label)
        results.append(stats)

    unread = {r["type"]: r["unread"] for r in results if r["unread"]}
    if unread:
        logs.error(COMPONENT, f"Could not read {sum(unread.values()):,} listed file(s) from "
                              f"{VALIDATED_DIR} ({per_mechanism(unread)}). The validated tree "
                              f"does not match the mechanism CSVs, and no table was written.")
        sys.exit(1)
    empty = {r["type"]: r["empty"] for r in results if r["empty"]}
    if empty:
        logs.error(COMPONENT, f"{sum(empty.values()):,} listed file(s) are empty "
                              f"({per_mechanism(empty)}). The collected file cleanup deletes such "
                              f"files. The validated tree does not match the mechanism CSVs, and "
                              f"no table was written.")
        sys.exit(1)
    kept = {r["type"]: r["empty_kept"] for r in results if r["empty_kept"]}
    if kept:
        logs.info(COMPONENT, f"{sum(kept.values()):,} listed file(s) are empty "
                             f"({per_mechanism(kept)}), and the collected file cleanup keeps "
                             f"them. Counted them with 0 lines.")
    absent = {r["type"]: r["target_absent"] for r in results if r["target_absent"]}
    if absent:
        logs.info(COMPONENT, f"{sum(absent.values()):,} listed file(s) are markers of links to a "
                             f"target that the validated tree lacks ({per_mechanism(absent)}). "
                             f"Counted them without a line count.")
    binary = {r["type"]: r["no_text"] for r in results if r["no_text"]}
    if binary:
        logs.info(COMPONENT, f"{sum(binary.values()):,} listed file(s) are not text "
                             f"({per_mechanism(binary)}), such as files that git-crypt encrypted. "
                             f"Counted them without a line count.")

    print_summary_table(results)
    write_csv(results)
    write_latex(results)


if __name__ == "__main__":
    main()
