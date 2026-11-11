#!/usr/bin/env python3
"""Check that the mechanism CSVs and ``repos.csv`` name the same repositories.

A repository in a mechanism CSV has at least one detected configuration artifact and must
therefore be a row of ``repos.csv``. A row of ``repos.csv`` exists because the detection found a
configuration artifact, and its repository must appear in at least one mechanism CSV. Different
code paths build the two sets of repositories, in the detection step and again in the file
history step. A difference between the sets means that one of the paths dropped or invented a
repository.

Every row also records the commit at which its repository was collected, `head_sha`. A
repository's rows in every CSV have to name the same non-empty commit. Rows of two collections
joined under one name agree by name but do not describe one snapshot, and neither does a
repository published without the commit that its files came from.

A repository's six tool flags in ``repos.csv`` have to be the tools that the `tool` column of
its rows names, joined over every mechanism CSV. A flag that no row sets credits a tool with
no configuration artifact, and a row with a tool that has no flag is left out of every count
by tool.

Planning documents are not configuration artifacts, and `planning-documents.csv` is not one of
the mechanism CSVs, which together define the configured repositories. Because the planning
documents are recorded for the configured repositories only, every repository in
`planning-documents.csv` has to be a row of ``repos.csv``. Its rows have to name the commit that
the repository's other rows name. A repository with planning documents alone would be counted
as configured nowhere and published anyway.

The step writes the report CSV on every run. It writes the mismatch CSV only when the CSVs
disagree and deletes it when they agree, and the presence of the file always describes the
latest run. A mismatch exits 2, which stops ``agentcontext/runner.py`` here, before the
collected_files_archive step can publish a sample with CSVs that disagree.

Inputs:
  - Every mechanism CSV named by ``heuristics.MECHANISM_CSVS``
  - ``output/planning-documents.csv``
  - ``output/repos.csv``

Outputs:
  - ``output/consistency-report.csv``, every run
  - ``output/consistency-mismatch.csv``, only when the sets disagree

Exit codes follow the other verification commands: 0 when the sets, the commits and the tools
agree, 2 when they do not, and 1 when a CSV could not be read or an argument is not one of this
command's.
"""

import contextlib
import csv
import io
import sys
from pathlib import Path

from agentcontext import STAGE_DIR
from agentcontext import cli

from agentcontext import caches
from agentcontext import heuristics
from agentcontext import logs
from agentcontext import planning_schema
from agentcontext.heuristics import TOOLS, value_tools

# The detection step joins every file that a context file references into `referenced_file`,
# and nothing limits the length of that field. A context file with enough references would
# exceed the default field limit of the csv module and stop the step at every Start.
csv.field_size_limit(sys.maxsize)

OUTPUT_DIR = STAGE_DIR / "output"

# The published mechanism CSVs, read from the heuristics rather than listed again here.
MECHANISM_FILES = heuristics.csv_filenames()
OTHER_FILE = heuristics.MECHANISM_CSVS["other"][0]

REFERENCE_FILE = "repos.csv"
REPORT_FILE = "consistency-report.csv"
MISMATCH_FILE = "consistency-mismatch.csv"


def read_repo_names(filepath):
    return set(read_repo_commits(filepath))


def read_repo_commits(filepath) -> dict:
    """Map each repo_name to the `head_sha` values of its rows, a blank one included."""
    commits = {}
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "head_sha" not in (reader.fieldnames or []):
            raise ValueError(f"{filepath} has no head_sha column (the collected commit of each "
                             f"repository). Re-run the detection step.")
        for row in reader:
            commits.setdefault(row["repo_name"], set()).add(row["head_sha"] or "")
    return commits


def read_repo_tools(filepath) -> dict:
    """Map each repo_name to the tools that the `tool` column of its rows names, joined."""
    tools = {}
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "tool" not in (reader.fieldnames or []):
            raise ValueError(f"{filepath} has no tool column (the tools of each row). Re-run the "
                             f"detection step.")
        for row in reader:
            try:
                tools.setdefault(row["repo_name"], set()).update(value_tools(row["tool"]))
            except ValueError as e:
                raise ValueError(f"The tool column of a row of {row['repo_name']} in {filepath} "
                                 f"is invalid ({e}). Re-run the detection step.") from e
    return tools


def read_tool_flags(filepath) -> dict:
    """Map each repo_name to the tools with a flag that its row of `repos.csv` sets."""
    with open(filepath, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        absent = [tool for tool in TOOLS if tool not in (reader.fieldnames or [])]
        if absent:
            raise ValueError(f"{filepath} lacks the tool flag column(s) {', '.join(absent)}. "
                             f"Re-run the detection step.")
        return {row["repo_name"]: {tool for tool in TOOLS if str(row[tool]).upper() == "TRUE"}
                for row in reader}


def read_outputs(output_dir: Path) -> tuple:
    """What the check compares, read from the CSVs of `output_dir`. The tuple holds the number
    of distinct repositories per mechanism CSV, the repositories of the mechanism CSVs, every
    head_sha that a repository's rows give it, the tools that a repository's rows of the ten
    mechanisms name, the repositories of `repos.csv`, the tools that a repository's row of
    `repos.csv` flags, and the repositories of `planning-documents.csv`."""
    per_file_counts = {}
    mechanism_repos = set()
    # Each repo_name with every head_sha that any CSV gives it, repos.csv included.
    heads = {}
    # Each repo_name with the tools of its rows, over every mechanism CSV.
    row_tools = {}

    for filename in MECHANISM_FILES:
        commits = read_repo_commits(output_dir / filename)
        per_file_counts[filename] = len(commits)
        mechanism_repos |= set(commits)
        for repo, shas in commits.items():
            heads.setdefault(repo, set()).update(shas)
        # The tool flags come from the rows of the ten mechanisms, and other.csv sets none.
        if filename == OTHER_FILE:
            continue
        for repo, tools in read_repo_tools(output_dir / filename).items():
            row_tools.setdefault(repo, set()).update(tools)

    reference = read_repo_commits(output_dir / REFERENCE_FILE)
    for repo, shas in reference.items():
        heads.setdefault(repo, set()).update(shas)
    flags = read_tool_flags(output_dir / REFERENCE_FILE)
    # Not part of the union above, because a planning document makes no repository configured.
    planned = read_repo_commits(output_dir / planning_schema.CSV_NAME)
    for repo, shas in planned.items():
        heads.setdefault(repo, set()).update(shas)
    return (per_file_counts, mechanism_repos, heads, row_tools, set(reference), flags,
            set(planned))


def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Check that the mechanism CSVs and output/repos.csv name the same "
                    "repositories, collected commits and tools, and exit 2 when they do not.")
    parser.add_argument("--output-dir", type=Path, default=None,
                        help=f"Directory of the mechanism CSVs and repos.csv (default: "
                             f"{OUTPUT_DIR})")
    args = parser.parse_args(argv)
    # Read OUTPUT_DIR at the call rather than binding it as the default, so that a caller that
    # reassigns the module's OUTPUT_DIR, as the suite does, gets the new value.
    output_dir = args.output_dir or OUTPUT_DIR
    try:
        (per_file_counts, mechanism_repos, heads, row_tools, output_repos,
         flags, planned_repos) = read_outputs(output_dir)
    except (OSError, KeyError, UnicodeDecodeError, csv.Error) as e:
        # A missing or unreadable CSV means that the check could not run (exit 1). The step
        # logs the error as one line rather than a traceback.
        logs.error("output_cross_check", f"Could not read the output CSVs in {output_dir} "
                                         f"({e.__class__.__name__} {e}).")
        return 1
    except ValueError as e:
        # A CSV without a column that the check compares, or with a tool value that the
        # detection step cannot write. The text names the file and the remedy.
        logs.error("output_cross_check", f"{str(e).rstrip('.')}.")
        return 1

    only_in_mechanisms = mechanism_repos - output_repos
    only_in_reference = output_repos - mechanism_repos
    head_missing = {repo for repo, shas in heads.items() if "" in shas}
    head_differs = {repo for repo, shas in heads.items() if len(shas - {""}) > 1}
    # Compare every repository of repos.csv, not only the repositories of the rows. A repository
    # with flags that no row of the ten mechanisms sets, such as a flag that only an `other` row
    # set, has flags and no tools and would otherwise be left out of the comparison.
    tools_differ = {repo for repo in flags if flags[repo] != row_tools.get(repo, set())}
    planning_only = planned_repos - output_repos

    # The report CSV has the counts per file and the mismatch CSV the repositories that differ.
    # The log records only the totals and the outcome.
    logs.info("output_cross_check", f"Found {len(mechanism_repos):,} repositories in the "
                                    f"{len(MECHANISM_FILES):,} mechanism CSVs and "
                                    f"{len(output_repos):,} in {REFERENCE_FILE}.")
    if not only_in_mechanisms and not only_in_reference:
        logs.info("output_cross_check", f"Every repository matches between the mechanism CSVs "
                                        f"and {REFERENCE_FILE}.")
    else:
        logs.info("output_cross_check", f"{len(only_in_mechanisms):,} repositories are only in the "
                                        f"mechanism CSVs and {len(only_in_reference):,} only in "
                                        f"{REFERENCE_FILE}. {MISMATCH_FILE} lists them.")
    if head_missing or head_differs:
        logs.info("output_cross_check", f"{len(head_missing):,} repositories have rows without "
                                        f"a head_sha, and {len(head_differs):,} have rows with "
                                        f"more than one head_sha. {MISMATCH_FILE} lists them.")
    if tools_differ:
        logs.info("output_cross_check", f"{len(tools_differ):,} repositories have tool flags that "
                                        f"differ from the tools of their rows. {MISMATCH_FILE} "
                                        f"lists them.")
    if planning_only:
        logs.info("output_cross_check", f"{len(planning_only):,} repositories have planning "
                                        f"documents and no row of {REFERENCE_FILE}. "
                                        f"{MISMATCH_FILE} lists them.")

    report_path = output_dir / REPORT_FILE
    # Write atomically, through the writer that the caches use. This CSV is tracked, and a kill
    # between truncating and flushing would leave a half-written tracked file. A report with the
    # same content is written again too, because the runner records the step only when it wrote
    # every output that it produces (`runner._wrote_output`).
    text = io.StringIO()
    with contextlib.closing(text) as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["file", "unique_repos"])
        for filename in MECHANISM_FILES:
            writer.writerow([filename, per_file_counts[filename]])
        writer.writerow(["TOTAL (mechanism CSVs)", len(mechanism_repos)])
        writer.writerow([REFERENCE_FILE, len(output_repos)])
        writer.writerow([planning_schema.CSV_NAME, len(planned_repos)])
        caches.replace_file(report_path, text.getvalue())

    logs.info("output_cross_check", f"Wrote {report_path}.")

    mismatch_path = output_dir / MISMATCH_FILE
    mismatches = [(r, "mechanism_only") for r in sorted(only_in_mechanisms)]
    mismatches += [(r, "reference_only") for r in sorted(only_in_reference)]
    mismatches += [(r, "head_sha_missing") for r in sorted(head_missing)]
    mismatches += [(r, "head_sha_differs") for r in sorted(head_differs)]
    mismatches += [(r, "tool_flags_differ") for r in sorted(tools_differ)]
    mismatches += [(r, "planning_only") for r in sorted(planning_only)]
    mismatch_count = len(mismatches)
    if mismatch_count:
        with open(mismatch_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["repo_name", "source"])
            writer.writerows(mismatches)
        logs.info("output_cross_check", f"Wrote {mismatch_path}.")
    else:
        # A stale copy from an earlier failing run would otherwise remain after the fix.
        if mismatch_path.exists():
            mismatch_path.unlink()

    if mismatch_count:
        logs.error("output_cross_check", f"{mismatch_count:,} mismatch(es) in repository, "
                                         f"collected commit or tool between the mechanism CSVs "
                                         f"and {REFERENCE_FILE}, listed in {mismatch_path}. "
                                         f"Re-run the collected file cleanup and the detection "
                                         f"step before publishing.")
        # Exit 2 reports a difference, as in every verification command. Exit 1 is for a
        # check that could not run.
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
