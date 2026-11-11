#!/usr/bin/env python3
"""
Run the whole pipeline: the five stages sampling, classification, collection, publication and
analysis in order, as one list of steps with one pipeline execution state.

Each stage's runner defines a list of steps that agentcontext/runner.py runs. This script joins
the four lists and adds the edges between them: the release reads the sampling frame and the
labels, and every analysis reads the collection's output CSVs. The labeling reads the
sampling frame too, but it has no edge to the filter. The runner records it by the repositories
in the sampling frame and the values of the sampling frame's `_metadata` that it reads
(`sampling_frame_contents`). A run of the filter that writes the same repositories therefore
leaves the labeling complete, whatever config or license review the filter writes into
`_metadata`.

The collection's list starts with the engineered project selection, the last step of the
classification, because a run of the collection's list alone needs it. This script takes the
step from the classification's list instead. The pipeline execution state, the run lock and the
logs of this script are in run_logs/ at the data root, apart from the files of each stage, and a
run of the whole pipeline never shares them with a run of one stage.

The GHS download takes a snapshot of an outside source, and when its output is on disk, the
runner records the step as complete rather than running it again. --from names a snapshot to
take again, and --mark-complete records a snapshot restored from an archive.
`repository_cloning` also takes a snapshot, but the runner does not record it from its output on
disk. --mark-complete records it on purpose after verified collection outputs are restored from
a handover.

Usage:
  python3 run_pipeline.py --list
  python3 run_pipeline.py
  python3 run_pipeline.py --from detection
  python3 run_pipeline.py --with-commit-reclassification --scratch-dir /mnt/scratch
  python3 run_pipeline.py --mark-complete repository_cloning
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# The three other runners are scripts in their stage directories. `agentcontext/runner.py` and
# the list of the collection and the publication are part of the package.
for stage_dir in ("1_sampling", "2_classification", "4_data-analysis"):
    sys.path.insert(0, str(ROOT / stage_dir))
from agentcontext import DATA_ROOT, credentials, runner
from agentcontext import cli
import run_analysis  # noqa: E402
import run_classification  # noqa: E402
import run_sampling  # noqa: E402

# For the edges between the four lists: the step that writes the paths under another stage's
# data directory that start with each prefix. The filter writes the sampling frame under
# sampling/, and the classification writes the labels under classification/. The collection's
# output CSVs belong to the file history step, which rewrites what the detection step wrote,
# except `all_ai_commits*`, which the detection step produces. The validated tree belongs to
# the file history step too, because that step removes the directory of a repository that git
# reports as gone, and a later step reads the tree as the file history step leaves it. With the
# cleanup as the tree's producer, the content-quality step would read an input that the file
# history step modifies, and the check for modified inputs would rebuild the tree in every run
# in which the file history step is pending and the cleanup is not.
PRODUCERS = {
    "../sampling/": "repository_filtering",
    "../classification/": "repository_labeling",
    "../collection/output/all_ai_commits": 'detection',
    "../collection/output/": 'file_history',
    "../collection/data/validated": 'file_history',
}


def producers_of(paths) -> tuple:
    """The keys of the steps that write the given paths, by their prefix, without repeats."""
    found = []
    for path in paths:
        for alternative in path.split("|"):
            for prefix, key in PRODUCERS.items():
                if alternative.startswith(prefix):
                    if key not in found:
                        found.append(key)
                    break
    return tuple(found)


# The step that labels the repositories in the sampling frame.
LABELING = "repository_labeling"
SAMPLING_FRAME_CONTENTS = ("the repositories in the sampling frame and the values of its "
                           "_metadata that the labeling reads")


def _object(value) -> dict:
    """`value` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def labeling_metadata(metadata: dict) -> dict:
    """The values of a sampling frame's `_metadata` that the labeling reads, through
    `read_sampling_frame` in `2_classification/1_classify_repos.py` and `forks.thresholds_of`:
    the retrieval's id, whether GitHub's responses are recorded, the `contributorsMin` of the GHS
    query and the first day of the sampling window. A value of `_metadata` that the labeling
    starts to read belongs here too. Otherwise a change to that value would leave the labeling
    complete."""
    source = _object(metadata.get("input_source"))
    return {"retrieval_id": source.get("retrieval_id"),
            "github": bool(source.get("github")),
            "contributorsMin": _object(source.get("query")).get("contributorsMin"),
            "committed_min": _object(source.get("window")).get("committed_min")}


def sampling_frame_contents(step) -> list:
    """What records the labeling besides its script, its prompts and its modules: the
    repositories in the sampling frame and the values of its `_metadata` that the labeling reads
    (`labeling_metadata`), as a label and a digest. The digest is None when the file is absent or
    is not a sampling frame, and the labeling then runs and names what is wrong.

    The filter runs again after every change to its code, its config, the reviewed licenses or
    the SPDX list, and each of its runs writes its time, its config and the license step's
    review into `_metadata`. If the runner recorded the labeling by the whole file, or by an
    edge to the filter, the labeling would request the README of every repository again after
    such a run, and every later step would run again, although the repositories and the values
    that the labeling reads did not change. `repository_cloning` is recorded the same way, by
    the contents of its list rather than by an edge to the engineered project selection."""
    path = Path(os.path.normpath(runner.data_dir(step) / run_classification.SAMPLING_FRAME))
    if not path.is_file():
        return [(SAMPLING_FRAME_CONTENTS, None)]
    try:
        with open(path, "rb") as f:
            data = json.load(f)
    except OSError as e:
        raise runner.UnreadableOutput(step.key, SAMPLING_FRAME_CONTENTS, e) from e
    except ValueError:
        return [(SAMPLING_FRAME_CONTENTS, None)]
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return [(SAMPLING_FRAME_CONTENTS, None)]
    digest = hashlib.sha256(json.dumps(labeling_metadata(_object(data.get("_metadata"))),
                                       sort_keys=True).encode("utf-8"))
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    for item in data["items"]:
        digest.update(b"\n" + encoder.encode(item).encode("utf-8"))
    return [(SAMPLING_FRAME_CONTENTS, digest.hexdigest())]


def compose(with_commit_reclassification: bool = False) -> list:
    """The four lists as one, with the edges between them added."""
    steps = []
    for step in run_sampling.STEPS:
        steps.append(step)
    for step in run_classification.STEPS:
        if step.key == LABELING:
            # The labeling gets no edge to the filter and is recorded by the sampling frame's
            # contents rather than by the file. `reads` keeps the file, so that the pre-checks require it.
            steps.append(replace(step, inputs=tuple(
                path for path in step.inputs if path != run_classification.SAMPLING_FRAME),
                inputs_of=sampling_frame_contents))
            continue
        extra = producers_of(step.reads)
        steps.append(replace(step, consumes=step.consumes + tuple(
            k for k in extra if k not in step.consumes)))
    for step in runner.steps_for(with_commit_reclassification):
        # The selection comes from the classification's list, where it is the last step. The
        # release reads the sampling frame and the labels, which steps of other lists write.
        if step.key != 'engineered_project_selection':
            extra = producers_of(step.inputs + step.reads)
            steps.append(replace(step, consumes=step.consumes + tuple(
                k for k in extra if k not in step.consumes)))
    for step in run_analysis.STEPS:
        extra = producers_of(step.inputs + step.reads)
        steps.append(replace(step, consumes=step.consumes + tuple(
            k for k in extra if k not in step.consumes)))
    keys = [s.key for s in steps]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ValueError(f"Step keys appear in more than one list of steps "
                         f"({runner.series(duplicates)})")
    return steps


def build_parser() -> argparse.ArgumentParser:
    parser = cli.ArgumentParser(description="Run the five stages of the pipeline in order.")
    parser.add_argument("--list", action="store_true", help="Print the stages with their steps and exit")
    parser.add_argument("--from", dest="from_name", action="append", default=None,
                        help="Run this stage or step again, with every step that reads its output "
                             "(repeatable)")
    parser.add_argument("--with-commit-reclassification", action="store_true",
                        help="Run commit_reclassification after repository cloning, and the "
                             "checks commit_attribution_validation and "
                             "unmatched_commits_report before the publication")
    parser.add_argument("--repo-csv", default=None, metavar="PATH",
                        help="The list of repositories that repository cloning collects "
                             "(default: the list that engineered_project_selection writes)")
    parser.add_argument("--step-arg", action="append", default=[], dest="step_args",
                        metavar="NAME:ARGS",
                        help="Append arguments to the command of one step, such as "
                             'repository_cloning:"-w 10" (repeatable)')
    parser.add_argument("--mark-complete", default=None, metavar="STEP", dest="mark_complete",
                        help="Record STEP as complete from its output on disk, without running it")
    parser.add_argument("--scratch-dir", type=Path, default=None, metavar="DIR",
                        help="The directory where the cloning steps put their clones")
    parser.add_argument("--min-free-gb", type=float, default=None, metavar="GB",
                        help="The free disk space in GiB that the pre-checks and "
                             "repository cloning require")
    parser.add_argument("--no-github", action="store_true",
                        help="Declare clones from local Git mirrors rather than from github.com "
                             "(the pre-checks then validate no GitHub access token)")
    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    if runner.refuses_mark_complete(args):
        return 1
    steps = compose(with_commit_reclassification=args.with_commit_reclassification)
    scratch_dir = args.scratch_dir if args.scratch_dir is not None else runner.DEFAULT_SCRATCH_DIR
    steps = runner.apply_scratch_dir(steps, scratch_dir)
    steps = runner.apply_repo_csv(steps, args.repo_csv)
    steps = runner.apply_min_free_gb(steps, args.min_free_gb)
    if args.step_args:
        # As in run_sampling.py, the filter's `--top-languages` is passed to the download too.
        steps = run_sampling.share_top_languages(
            runner.apply_step_overrides(steps, args.step_args))
    return runner.run(steps, list_only=args.list, from_name=args.from_name,
                      mark=args.mark_complete, no_github=args.no_github,
                      scratch_dir=scratch_dir, argv=sys.argv,
                      log_dir=DATA_ROOT / "run_logs", min_free_gb=args.min_free_gb,
                      credentials_path=credentials.default_path())


if __name__ == "__main__":
    sys.exit(main())
