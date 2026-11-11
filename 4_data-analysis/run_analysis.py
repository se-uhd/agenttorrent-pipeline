#!/usr/bin/env python3
"""
Run the analysis stage end to end: every figure and table script over the output CSVs that
the collection stage wrote, the filter steps table for the paper, and the reproduction check.

agentcontext/runner.py runs the steps. It records each script's step by a hash of the script
and of the contents of every file that the script reads, so that it reruns exactly the figures
that read a changed output CSV, and the figures of a changed script. For the content-quality
step the hash covers the files of the validated tree that the mechanism CSVs list, the files that
the markers among them name, and the files of their repositories that the collected file cleanup
keeps even when they are empty. The reproduction check runs last and requires that every script
write the same bytes twice.

Inputs: data/collection/output/*.csv, and for five steps the extracted
        data/classification/repos_annotated.csv, data/sampling/ghs_filtered.json and the
        validated tree. The pre-checks name the validated tree when it is missing, and a
        missing CSV or sampling frame stops the run at the first step that reads it. The
        archive beside the commit CSV, the labels or the sampling frame counts too, and an
        extracted file that differs from an archive that git tracks stops its step
Outputs: the figures and tables under data/analysis/, and data/analysis/run_logs/state.json

Usage:
  python3 run_analysis.py
  python3 run_analysis.py --list
  python3 run_analysis.py --from rq3_adoption_timeline
"""

import argparse
import csv
import hashlib
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
from agentcontext import ANALYSIS_DIR, heuristics, runner
from agentcontext import cli
from agentcontext.repos import repo_name_to_dir
from agentcontext.runner import COMPLETION_INPUTS, Step
from agentcontext.steps import collected_file_cleanup, detection

# The stage's data directory under the data root, and the directory of its scripts.
ROOT = "analysis"
CODE = "4_data-analysis"
# The stage of the pipeline that these steps make up.
STAGE = "analysis"
# Allow space for generated figures and tables.
MIN_FREE_DISK_GB = 2

OUT = "../collection/output"
REPOS = f"{OUT}/repos.csv"
MECHANISM_CSVS = tuple(f"{OUT}/{name}.csv" for name in
               ("context-files", "skills", "commands", "subagents", "rules", "settings",
                "hooks", "mcp", "plugins", "lsp"))
# The commit CSV, the labels and the sampling frame, which the repository tracks as archives, are
# each declared with the archive as an alternative. The fingerprint covers the extracted file and
# the archive when both are on disk (`runner.input_paths`), so that a changed archive runs the
# steps again, and `_shared.checked_copy` then stops on an extracted file that differs from an
# archive that git tracks. The scripts of the labels and the sampling frame read the extracted
# file alone.
COMMITS = f"{OUT}/all_ai_commits.csv|{OUT}/all_ai_commits.csv.7z.*|{OUT}/all_ai_commits.csv.7z"
ANNOTATED = "../classification/repos_annotated.csv|../classification/repos_annotated.7z"
SAMPLING_FRAME = "../sampling/ghs_filtered.json|../sampling/ghs_filtered.json.7z"
VALIDATED = "../collection/data/validated"
REPO_METADATA = "rq2/_repo_metadata.py"
PLOTTING = ("pandas", "matplotlib", "seaborn")


# The label under which the content-quality step's fingerprint records the files that it reads
# out of the validated tree.
LISTED_FILES = ("the files of the validated tree that the mechanism CSVs list, the files that "
                "the markers among them name, and the files of their repositories that the "
                "collected file cleanup keeps even when they are empty")


def listed_validated_files(step: Step) -> list:
    """What the content-quality step reads beside its CSVs, as one label and one digest over
    the paths and contents: every file of the validated tree that a mechanism CSV lists, the
    file that such a file names when it is the collection's marker of a symlink and the step
    counts the lines of the named file (`detection.measured_artifact`), and every file of a
    listed repository that the collected file cleanup keeps even when it is empty
    (`collected_file_cleanup.kept_when_empty`). The digest is None when a CSV or one of the
    listed and named files is not there.

    A listed path that is a symbolic link is digested by the link's text, and by the bytes of the
    file that it leads to where it leads to one. The script reads a context file that is a link
    as the link's text, also where the link has no target (`detection.artifact_text`), so such a
    link is there, and it reads the file of any other link through the link.

    Declaring the tree in `reads` alone is not enough. The pre-checks require a `reads`
    path on disk, but no fingerprint covers it, and a tree that changed under the same CSVs would
    leave the step done. The set that the cleanup keeps decides whether a listed empty file counts
    with 0 lines or stops the script. Its paths are therefore in the digest, and a tree in which the
    set changed runs the step again. A path of the set that is no file, such as a link without a
    target, is no file that the script counts, and it is left out. """
    # Normalized: The kernel cannot follow `analysis/..` before the stage has an `analysis/`.
    base = runner.data_dir(step)
    tree = Path(os.path.normpath(base / VALIDATED))
    mechanisms = {filename: mechanism
                  for mechanism, (filename, _column) in heuristics.MECHANISM_CSVS.items()}
    listed = set()
    for rel in MECHANISM_CSVS:
        path = Path(os.path.normpath(base / rel))
        if not path.is_file():
            return [(LISTED_FILES, None)]
        column = heuristics.artifact_column_for_file(path.name)
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                listed.add((row["repo_name"], row[column]))
                # None for the marker of a link with a target that the tree lacks, which the
                # script counts with no line count, and which is a listed file itself.
                measured = detection.measured_artifact(
                    tree / repo_name_to_dir(row["repo_name"]), mechanisms[path.name], row[column])
                if measured is not None:
                    listed.add((row["repo_name"], measured))
    for repo_name in {name for name, _artifact in listed}:
        repository = tree / repo_name_to_dir(repo_name)
        listed.update((repo_name, rel) for rel in collected_file_cleanup.kept_when_empty(repository)
                      if (repository / rel).is_file())
    digest = hashlib.sha256()
    for repo_name, artifact in sorted(listed):
        file = tree / repo_name_to_dir(repo_name) / artifact
        link = os.readlink(file) if file.is_symlink() else None
        if link is None and not file.is_file():
            return [(LISTED_FILES, None)]
        # A name contains no NUL, so the entry is unambiguous. The text of a link follows as
        # `->` and the text, which no hexadecimal digest of a file's bytes starts with.
        digest.update(f"{repo_name}\0{artifact}\0".encode("utf-8", "surrogateescape"))
        if link is not None:
            digest.update(f"->{link}\0".encode("utf-8", "surrogateescape"))
        if not file.is_file():
            continue
        try:
            content = file.read_bytes()
        except OSError as e:
            # The runner raises the same error for an input that it can stat but not open.
            raise runner.UnreadableOutput(step.key, LISTED_FILES, e) from e
        digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
    return [(LISTED_FILES, digest.hexdigest())]


def _figure(key: str, script: str, inputs: tuple, produces: tuple, imports: tuple = PLOTTING,
            reads: tuple = (), code_inputs: tuple = (), inputs_of=None) -> Step:
    return Step(key, runner.VENV_PYTHON, [script], produces=produces, inputs=inputs,
                 code_inputs=(*code_inputs, "_shared.py"), package_code=True,
                 heuristics=("config_path_patterns.csv",),
                 completion=COMPLETION_INPUTS, root=ROOT, code=CODE, imports=imports, reads=reads,
                 stage=STAGE, inputs_of=inputs_of)


# Each step's `inputs` are what the script reads and `produces` what it writes, both relative
# to the stage's data directory. check_reproduction.py lists the same scripts.
STEPS = [
    _figure("rq2_config_cooccurrence", "rq2/config_cooccurrence.py", (REPOS,),
            ("rq2/config_cooccurrence.csv", "rq2/config_cooccurrence.tex"),
            imports=("pandas", "scipy", "statsmodels")),
    _figure("rq2_config_mechanism_upset", "rq2/config_mechanism_upset.py", (REPOS,),
            ("rq2/config_mechanism_upset.pdf",)),
    _figure("rq2_config_tool_heatmap", "rq2/config_tool_heatmap.py", (REPOS, *MECHANISM_CSVS),
            ("rq2/config_tool_heatmap.pdf",)),
    # The step reads the validated tree, and the fingerprint covers the files of the tree that
    # the CSVs list.
    _figure("rq2_content_quality", "rq2/content_quality.py", MECHANISM_CSVS,
            ("rq2/content_quality.csv", "rq2/content_quality.tex"),
            imports=("pandas",), reads=(VALIDATED,), inputs_of=listed_validated_files),
    _figure("rq2_language_ranking_all", "rq2/language_ranking_all.py", (ANNOTATED, SAMPLING_FRAME),
            ("rq2/language_ranking_all.csv", "rq2/top5_languages_all.tex", "rq2/repo_metadata.csv"),
            imports=("pandas",), code_inputs=(REPO_METADATA,)),
    _figure("rq2_language_tool_association", "rq2/language_tool_association.py", (REPOS, SAMPLING_FRAME),
            ("rq2/language_ranking.csv", "rq2/top5_languages.tex", "rq2/language_tool_heatmap.pdf",
             "rq2/context_file_language_adoption.csv")),
    _figure("rq2_repo_metadata_comparison", "rq2/repo_metadata_comparison.py",
            (REPOS, ANNOTATED, SAMPLING_FRAME),
            ("rq2/repo_metadata_comparison.csv", "rq2/repo_metadata_comparison.tex",
             "rq2/repo_metadata.csv"),
            imports=("pandas", "scipy", "statsmodels"), code_inputs=(REPO_METADATA,)),
    _figure("rq2_tool_cooccurrence", "rq2/tool_cooccurrence.py", (REPOS,),
            ("rq2/tool_cooccurrence.csv", "rq2/tool_cooccurrence.pdf", "rq2/tool_cooccurrence.tex")),
    _figure("rq2_tool_count_distribution", "rq2/tool_count_distribution.py", (REPOS,),
            ("rq2/tool_count_distribution.csv", "rq2/tool_count_distribution.pdf",
             "rq2/tool_count_distribution.tex")),
    _figure("rq2_upset_plot", "rq2/upset_plot.py", (REPOS,), ("rq2/upset-tool-usage.pdf",),
            imports=("pandas", "matplotlib")),
    _figure("rq2_venn_diagram", "rq2/venn_diagram.py", (REPOS,), ("rq2/venn-tool-usage.pdf",),
            imports=("pandas", "matplotlib", "venn")),
    _figure("rq3_adoption_timeline", "rq3/adoption_timeline.py",
            (f"{OUT}/context-files.csv", f"{OUT}/skills.csv", f"{OUT}/subagents.csv",
             f"{OUT}/planning-documents.csv"),
            ("rq3/cumulative_repo_adoption.pdf",), imports=("pandas", "matplotlib")),
    _figure("rq3_cooccurrence_analysis", "rq3/cooccurrence_analysis.py",
            (f"{OUT}/context-files.csv",),
            ("rq3/creation_order_sequences.pdf", "rq3/reference_network.pdf",
             "rq3/cooccurrence_matrix.pdf")),
    _figure("rq3_count_distribution", "rq3/count_distribution.py",
            (f"{OUT}/context-files.csv", f"{OUT}/skills.csv", f"{OUT}/subagents.csv"),
            ("rq3/count_distribution.pdf",), imports=("pandas", "matplotlib")),
    _figure("dataset_artifact_distribution", "dataset/artifact_distribution.py", MECHANISM_CSVS,
            ("dataset/artifact_distribution.pdf",)),
    _figure("dataset_tool_adoption", "dataset/tool_adoption.py", (REPOS,),
            ("dataset/tool_adoption.pdf",)),
    _figure("dataset_monthly_ai_commit_volume", "dataset/monthly_ai_commit_volume.py",
            (REPOS, COMMITS),
            ("dataset/monthly_ai_commit_volume.pdf",)),
    _figure("dataset_commit_message_first_words", "dataset/commit_message_first_words.py",
            (COMMITS,), ("dataset/commit_message_first_words.pdf",)),
    # The paper's table of the filter steps, from the record that the filter leaves in the
    # sampling frame.
    _figure("dataset_filter_steps_table", "dataset/filter_steps_table.py", (SAMPLING_FRAME,),
            ("dataset/filter_steps_table.tex",), imports=()),
    # The two extensions beside the configuration artifacts, as tables.
    _figure("dataset_planning_documents", "dataset/planning_documents.py",
            (f"{OUT}/planning-documents.csv",),
            ("dataset/planning_documents.csv", "dataset/planning_documents.tex"),
            imports=("pandas",)),
    _figure("dataset_imports", "dataset/imports.py", (f"{OUT}/imports.csv",),
            ("dataset/imports.csv", "dataset/imports.tex"), imports=("pandas",)),
    # The reproduction check runs last and requires the same bytes from both runs of every
    # script. The runner records it by its script, so it runs in every run that reaches it.
    Step("reproduction_check", runner.VENV_PYTHON,
          ["check_reproduction.py", "--skip-missing", "--reproduce-only"], root=ROOT, code=CODE,
          consumes=tuple(key for key in (
              "rq2_config_cooccurrence", "rq2_config_mechanism_upset", "rq2_config_tool_heatmap",
              "rq2_content_quality", "rq2_language_ranking_all", "rq2_language_tool_association",
              "rq2_repo_metadata_comparison", "rq2_tool_cooccurrence", "rq2_tool_count_distribution",
              "rq2_upset_plot", "rq2_venn_diagram", "rq3_adoption_timeline",
              "rq3_cooccurrence_analysis", "rq3_count_distribution", "dataset_artifact_distribution",
              "dataset_tool_adoption", "dataset_monthly_ai_commit_volume",
              "dataset_commit_message_first_words", "dataset_filter_steps_table",
              "dataset_planning_documents", "dataset_imports")), stage=STAGE),
]


def build_parser() -> argparse.ArgumentParser:
    parser = cli.ArgumentParser(description="Run the analysis stage end to end.")
    parser.add_argument("--list", action="store_true", help="Print the stage with its steps and exit")
    parser.add_argument("--from", dest="from_name", action="append", default=None,
                        help="Run this step again, with every step that reads its output, or every "
                             "step of the stage with --from analysis (repeatable)")
    parser.add_argument("--step-arg", action="append", default=[], dest="step_args",
                        metavar="NAME:ARGS",
                        help="Append arguments to the command of one step, such as "
                             'reproduction_check:"--only rq3" (repeatable)')
    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    steps = list(STEPS)
    if args.step_args:
        steps = runner.apply_step_overrides(steps, args.step_args)
    return runner.run(steps, list_only=args.list, from_name=args.from_name,
                      argv=sys.argv, min_free_gb=MIN_FREE_DISK_GB,
                      log_dir=ANALYSIS_DIR / "run_logs")


if __name__ == "__main__":
    sys.exit(main())
