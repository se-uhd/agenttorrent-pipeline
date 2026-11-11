#!/usr/bin/env python3
"""
Run every analysis script twice and log every output that changed or did not reproduce.

The convention in the section "Analysis scripts" of CLAUDE.md: A CSV or TeX output is byte-identical
between two runs over the same inputs, and so is a PDF once SOURCE_DATE_EPOCH is fixed,
which this script sets for every run. An output changed when the first run wrote other bytes
than the bytes on disk before it. After a change of the inputs, the changed outputs are
therefore the figures that the change affects. An output did not reproduce when the second
run wrote other bytes than the first. A script failed when it exited non-zero.

The exit code is a verdict. It is 0 when every script ran, every output reproduced and
nothing changed, and 2 when an output changed or did not reproduce, or when no script wrote a
figure or table in an output directory. It is 1 when a script failed, an input that it needs
is missing, or every selected script was skipped, because then nothing was checked.

CI runs the check with --skip-missing, which turns a missing input into a skip, and with
--reproduce-only, which skips the test for changed outputs. The scripts with inputs in
extracted archives or in the validated tree run only locally. Only the machine that wrote the
tracked outputs can check whether an output changed, because the CI runner's PDF bytes and the
last digits of one CSV differ from that machine's while every script still reproduces there.

Two environment variables reach the scripts through `_shared`: DATA_COLLECTION_OUTPUT
names another directory of output CSVs, and ANALYSIS_OUTPUT_DIR makes the scripts write under
another root. `--data-dir testdata/output --output-dir DIR` runs the scripts over the
fixture and leaves the tracked figures unchanged. Nothing is on disk under DIR before the
first run, and the check then tests only reproduction.

Inputs: the scripts below and what each of them reads
Outputs: what each script writes, twice, and the log messages that name every changed,
         differing and unwritten output

Usage:
  python3 check_reproduction.py                    # the local check, every script
  python3 check_reproduction.py --skip-missing --reproduce-only   # CI
  python3 check_reproduction.py --only rq3         # a subset, by substring of the path
  python3 check_reproduction.py --data-dir testdata/output --output-dir /tmp/figures
  python3 check_reproduction.py --list             # the scripts, their inputs, and which are here
"""

from __future__ import annotations

import glob
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

from agentcontext import ANALYSIS_DIR, DATA_ROOT, STAGE_DIR, logs
from agentcontext import cli

# The component of this script's log messages in the pipeline's one log format,
# `LEVEL component: message`. agentcontext/logs.py and docs/logs.md define the format.
COMPONENT = "reproduction_check"

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = STAGE_DIR / "output"

# Every script, relative to the repository root, with the inputs that it reads beyond the output
# CSVs under the data directory. An input is a glob relative to the data root, in which `{data}`
# stands for the directory of the output CSVs and one match among the alternatives of a `|` is
# enough. A script with no entry beyond the output CSVs runs wherever the output CSVs are, which
# includes a clean checkout.
SCRIPTS = (
    ("4_data-analysis/rq2/config_cooccurrence.py", ()),
    ("4_data-analysis/rq2/config_mechanism_upset.py", ()),
    ("4_data-analysis/rq2/config_tool_heatmap.py", ()),
    # The validated tree has to contain its repositories. The tracked list alone is an empty
    # sample, over which the script would run and write other numbers.
    ("4_data-analysis/rq2/content_quality.py", ("collection/data/validated/*§*",)),
    ("4_data-analysis/rq2/language_ranking_all.py",
     ("classification/repos_annotated.csv", "sampling/ghs_filtered.json")),
    ("4_data-analysis/rq2/language_tool_association.py", ("sampling/ghs_filtered.json",)),
    ("4_data-analysis/rq2/repo_metadata_comparison.py",
     ("classification/repos_annotated.csv", "sampling/ghs_filtered.json")),
    ("4_data-analysis/rq2/tool_cooccurrence.py", ()),
    ("4_data-analysis/rq2/tool_count_distribution.py", ()),
    ("4_data-analysis/rq2/upset_plot.py", ()),
    ("4_data-analysis/rq2/venn_diagram.py", ()),
    ("4_data-analysis/rq3/adoption_timeline.py", ()),
    ("4_data-analysis/rq3/cooccurrence_analysis.py", ()),
    ("4_data-analysis/rq3/count_distribution.py", ()),
    ("4_data-analysis/dataset/artifact_distribution.py",
     ("{data}/context-files.csv", "{data}/skills.csv", "{data}/commands.csv",
      "{data}/subagents.csv", "{data}/rules.csv", "{data}/settings.csv",
      "{data}/hooks.csv", "{data}/mcp.csv", "{data}/plugins.csv", "{data}/lsp.csv")),
    ("4_data-analysis/dataset/commit_message_first_words.py",
     ("{data}/all_ai_commits.csv|{data}/all_ai_commits.csv.7z.*|{data}/all_ai_commits.csv.7z",)),
    ("4_data-analysis/dataset/monthly_ai_commit_volume.py",
     ("{data}/all_ai_commits.csv|{data}/all_ai_commits.csv.7z.*|{data}/all_ai_commits.csv.7z",)),
    ("4_data-analysis/dataset/tool_adoption.py", ()),
    ("4_data-analysis/dataset/filter_steps_table.py", ("sampling/ghs_filtered.json",)),
    ("4_data-analysis/dataset/planning_documents.py", ("{data}/planning-documents.csv",)),
    ("4_data-analysis/dataset/imports.py", ("{data}/imports.csv",)),
)

# The scripts of the analysis directories and of the diagnostics that the check does not run,
# each with the reason. test_check_reproduction fails on a script that is in neither NOT_RUN
# nor SCRIPTS.
NOT_RUN = {
    "2_classification/diagnostics/calibration_measure.py":
        "it reads the READMEs, the linked pages and the file trees of the calibration sample "
        "from GitHub and the web",
    "2_classification/diagnostics/calibration_summaries.py":
        "it sends linked pages to an OpenAI model, which costs money, and reads page texts that "
        "no checkout has",
    "2_classification/diagnostics/calibration_language_variants.py":
        "it reads the READMEs of the calibration sample from GitHub before it tests the checks",
    "2_classification/diagnostics/calibration_analyze.py":
        "it writes no file, only the summary in its log",
}

# The lines at the end of a failing script's stderr that the check logs.
STDERR_TAIL_LINES = 3
# Written by the scripts and not an output: the byte-compiled cache.
IGNORED_SUFFIXES = (".py", ".pyc")
# What an output is, by suffix: a figure, a table, or the LaTeX of a table. The check logs a
# file with one of these suffixes in an output directory that no script wrote. It compares only
# what the scripts write, and a figure that a script stopped writing would otherwise stay on
# disk unchecked.
OUTPUT_SUFFIXES = (".pdf", ".csv", ".tex")


def joined(items: list) -> str:
    """The items as a list in a sentence, such as `a, b and c`."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def missing_inputs(needs: tuple, data_dir: Path) -> list:
    """The inputs of a script that are not on disk, each alternative group as one string."""
    missing = []
    for need in needs:
        alternatives = [alt.replace("{data}", str(data_dir)) for alt in need.split("|")]
        if not any(glob.glob(str(DATA_ROOT / alt), recursive=True) for alt in alternatives):
            missing.append(need)
    return missing


def output_dir_of(script: Path, output_root: Path | None) -> Path:
    """Where the script writes, as `_shared.output_dir` decides it, under `output_root` when
    that is given."""
    return (output_root if output_root is not None else ANALYSIS_DIR) / script.parent.name


def shown(path: Path) -> Path:
    """An output as the log messages name it: relative to the data root when it is under it."""
    return path.relative_to(DATA_ROOT) if path.is_relative_to(DATA_ROOT) else path


def digests(directory: Path) -> dict:
    """sha256 of every file in the directory, by path, and nothing for an absent directory."""
    found = {}
    if not directory.is_dir():
        return found
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix not in IGNORED_SUFFIXES and not path.name.startswith("."):
            found[path] = hashlib.sha256(path.read_bytes()).hexdigest()
    return found


def run_script(script: Path, env: dict, out_dir: Path) -> tuple:
    """Run the script once, and return the exit code, the stderr tail and the files that it
    wrote with their digests."""
    started = time.time_ns()
    result = subprocess.run([sys.executable, str(script)], cwd=str(script.parent), env=env,
                            capture_output=True, text=True)
    written = {path: digest for path, digest in digests(out_dir).items()
               if path.stat().st_mtime_ns >= started}
    tail = "\n".join(result.stderr.strip().splitlines()[-STDERR_TAIL_LINES:])
    return result.returncode, tail, written


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--skip-missing", action="store_true",
                        help="Skip a script with a missing input instead of failing")
    parser.add_argument("--reproduce-only", action="store_true",
                        help="Check only that the second run reproduces the first (for a machine "
                             "that did not write the tracked outputs)")
    parser.add_argument("--only", default=None, metavar="TEXT",
                        help="Run only the scripts with a path that contains TEXT")
    parser.add_argument("--data-dir", default=None, metavar="DIR",
                        help="Read the output CSVs from DIR instead of the collection stage's "
                             "output/ (sets DATA_COLLECTION_OUTPUT)")
    parser.add_argument("--output-dir", default=None, metavar="DIR",
                        help="Write under DIR/<script directory> instead of the data root, and "
                             "compare only the two runs (sets ANALYSIS_OUTPUT_DIR)")
    parser.add_argument("--list", action="store_true",
                        help="List the scripts and the missing inputs of each")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).resolve() if args.data_dir else DEFAULT_DATA_DIR
    output_root = Path(args.output_dir).resolve() if args.output_dir else None
    env = dict(os.environ)
    env.update({"SOURCE_DATE_EPOCH": "0", "PYTHONDONTWRITEBYTECODE": "1", "MPLBACKEND": "Agg",
                "DATA_COLLECTION_OUTPUT": str(data_dir)})
    if output_root is not None:
        env["ANALYSIS_OUTPUT_DIR"] = str(output_root)
    else:
        env.pop("ANALYSIS_OUTPUT_DIR", None)

    selected = [(ROOT / rel, needs) for rel, needs in SCRIPTS
                if not args.only or args.only in rel]
    if args.only and not selected:
        # Exit 0 means that every script ran and every output reproduced. A selector that
        # matches nothing checks nothing, and exit 0 would report a stale or mistyped selector
        # as agreement.
        logs.error(COMPONENT,
                   f"--only {args.only} matches none of the {len(SCRIPTS):,} scripts.")
        return 1
    if args.list:
        for script, needs in selected:
            rel = script.relative_to(ROOT)
            missing = missing_inputs(needs, data_dir)
            logs.info(COMPONENT, f"{rel} cannot run here (missing {joined(missing)})." if missing
                                 else f"{rel} can run here.")
        return 0

    failed, skipped, changed, differs = [], [], [], []
    check_changed = output_root is None and not args.reproduce_only
    before = {}
    for script, _ in selected:
        before.update(digests(output_dir_of(script, output_root)))

    # Every output that the first runs wrote.
    written = set()
    for script, needs in selected:
        rel = script.relative_to(ROOT)
        missing = missing_inputs(needs, data_dir)
        if missing:
            skipped.append(rel)
            logs.info(COMPONENT, f"SKIP  {rel} (missing {joined(missing)}).")
            continue
        out_dir = output_dir_of(script, output_root)
        code, tail, first = run_script(script, env, out_dir)
        written.update(first)
        if code != 0:
            failed.append(rel)
            logs.info(COMPONENT, f"FAIL  {rel} exited {code}. The last lines of its stderr follow.")
            for line in tail.splitlines():
                logs.info(COMPONENT, f"      {line}")
            continue
        code, tail, second = run_script(script, env, out_dir)
        if code != 0:
            failed.append(rel)
            logs.info(COMPONENT, f"FAIL  {rel} exited {code} on the second run. The last lines of "
                                 f"its stderr follow.")
            for line in tail.splitlines():
                logs.info(COMPONENT, f"      {line}")
            continue
        for path in sorted(set(first) | set(second)):
            name = shown(path)
            if first.get(path) != second.get(path):
                differs.append(name)
            if check_changed and before.get(path) != first.get(path):
                changed.append(name)
        logs.info(COMPONENT, f"ok    {rel} wrote {len(first):,} file(s).")

    # Checked only in a whole run on the machine that wrote the outputs on disk, because a
    # selection, a skip or a failure leaves outputs unwritten for a reason of its own.
    unwritten = []
    if check_changed and not args.only and not skipped and not failed:
        unwritten = sorted(shown(path) for path in before
                           if path.suffix in OUTPUT_SUFFIXES and path not in written)

    for name in changed:
        logs.info(COMPONENT, f"CHANGED  {name}.")
    for name in differs:
        logs.info(COMPONENT, f"DIFFERS  {name}.")
    for name in unwritten:
        logs.info(COMPONENT, f"UNWRITTEN {name} (no script wrote it).")
    ran = len(selected) - len(skipped) - len(failed)
    logs.info(COMPONENT, f"{ran:,} script(s) ran twice, {len(skipped):,} skipped, "
                         f"{len(failed):,} failed. {len(changed):,} output(s) changed, "
                         f"{len(differs):,} did not reproduce"
                         + (f", {len(unwritten):,} written by no script" if unwritten else "")
                         + ".")
    if failed or (skipped and not args.skip_missing):
        return 1
    # A run in which every selected script was skipped compared nothing. Exit 0 means that every
    # script ran and reproduced, and exit 0 would report such a run as agreement.
    if skipped and len(skipped) == len(selected):
        logs.error(COMPONENT, f"All {len(selected):,} selected script(s) were skipped. Nothing "
                              f"was checked.")
        return 1
    if changed or differs or unwritten:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
