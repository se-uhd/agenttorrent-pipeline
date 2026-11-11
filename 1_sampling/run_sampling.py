#!/usr/bin/env python3
"""
Run the sampling stage end to end: take a retrieval of the GHS dataset, query GitHub for every
repository in it, report what changed since the previous retrieval, and filter the retrieval
into the sampling frame.

A list of steps for agentcontext/runner.py, which provides the lock, the pipeline execution state,
the pre-checks, the resume rules and the stop handling. This script defines only the four
steps and what each needs.

A download cannot be repeated, because GHS updates a repository when it crawls a push to it. A
retrieval that ghs_retrieval.json already names is adopted rather than taken again, and
--from ghs_download takes a new one. The three steps after the download are recorded by the
retrieval that they read, that is, by its rows and GitHub's responses as the index of the cache
records them. A run that falls back to the previous retrieval, which the download reports in the
pipeline execution state, redoes none of these steps, and a GitHub query that writes new
responses redoes the comparison and the filter. The comparison compares each new retrieval with
the collection before it. It is also recorded by the retrieval before it, but not by the
collected list, which the cloning step rewrites later in the same run.

Inputs: config/ghs_download.yaml, config/ghs_filter.yaml, config/licenses.csv,
        config/spdx_licenses.json,
        ghs_retrievals/ (the cache of retrievals, ghs_cache.py), the collected list
        ../collection/data/collected/repos.csv when there is one, GitHub access tokens
        in credentials.csv at the repository root, GITHUB_TOKENS or GITHUB_TOKEN
Outputs:
  - ghs_retrievals/, a new retrieval and GitHub's responses for it
  - ghs_retrieval.json, which names the retrieval that the stage reads, and
    ghs_retrieval_report.json
  - ghs_changes.csv, ghs_collection_changes.csv and ghs_changes.json, the change report
  - ghs_filtered.json, the sampling frame
  - run_logs/state.json, which steps are complete and the run's provenance

Usage:
  python3 run_sampling.py
  python3 run_sampling.py --list
  python3 run_sampling.py --from ghs_download
  python3 run_sampling.py --from repository_filtering
  python3 run_sampling.py --step-arg repository_filtering:"--top-languages 5"

The filter's step argument `--top-languages N` reaches the download as well
(`share_top_languages`). The download then checks against the same N languages whether the
filter would keep a language that the end date of the sampling window left out.
"""

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
from agentcontext import SAMPLING_DIR, credentials, runner
from agentcontext import cli
from agentcontext.runner import COMPLETION_INPUTS, COMPLETION_OUTPUT, Step
# The cache module beside this script, which each sampling script imports. A suite that loads
# this file by its path has no `1_sampling/` on `sys.path`.
if str(SCRIPT_DIR) not in sys.path:
    sys.path.append(str(SCRIPT_DIR))
import ghs_cache  # noqa: E402

# The stage's data directory under the data root, and the directory of its scripts.
ROOT = "sampling"
CODE = "1_sampling"
# The stage of the pipeline that these steps make up.
STAGE = "sampling"
# The download keeps the GHS search result in memory while it parses it, and writes it compressed
# into the cache. Two gigabytes of disk space cover the compressed stream, the cache file and the
# sampling frame, with a margin.
MIN_FREE_DISK_GB = 2

# The file that names the retrieval that the stage reads. The download writes it, and the three
# steps after the download are recorded by its contents and by ghs_cache.py, which each of them
# imports.
POINTER = "ghs_retrieval.json"
CACHE_MODULE = "ghs_cache.py"
# The cache of retrievals under the stage's data directory.
CACHE_DIR = "ghs_retrievals"
# The filter's flag for how many of the most common languages it keeps, which the download reads
# too (`share_top_languages`).
TOP_LANGUAGES = "--top-languages"
# The labels under which a step's fingerprint records what it reads beside the pointer.
POINTED_ROWS = "the rows of the retrieval that the pointer names and GitHub's responses for them"
PREVIOUS_ROWS = "the rows of the retrieval before it and GitHub's responses for them"


def _rows_digest(cache: Path, row: dict) -> str | None:
    """The digests that the index records for a retrieval's rows and GitHub's responses, as one
    string, or None when either file is not in the cache."""
    files = {}
    for kind in ghs_cache.KINDS:
        sha = row[f"{kind}_file_sha256"]
        if not sha or not ghs_cache.has_rows(cache, row["retrieval_id"], kind):
            return None
        files[kind] = sha
    return json.dumps(files, sort_keys=True)


def pointed_rows(step: Step) -> list:
    """What the GitHub query, the comparison and the filter read beside the pointer: the rows of
    the retrieval that the pointer names and GitHub's responses for them, as one label and a
    digest, with None while either file is not in the cache.

    The query writes its responses into the cache under a name that the retrieval gives them,
    and no `produces` entry can name that file. The query leaves the pointer unchanged. If the
    steps were recorded by the pointer alone, the comparison and the filter would be skipped
    after the query wrote new responses, and the query would be skipped when its responses were
    gone.
    """
    base = runner.data_dir(step)
    try:
        row = ghs_cache.pointed_retrieval(base / POINTER, base / CACHE_DIR)
        return [(POINTED_ROWS, _rows_digest(base / CACHE_DIR, row))]
    except ghs_cache.CacheError:
        return [(POINTED_ROWS, None)]
    except OSError as e:
        raise runner.UnreadableOutput(step.key, POINTED_ROWS, e) from e


def compared_rows(step: Step) -> list:
    """What the comparison is recorded by beside the pointer, each as a label and a digest:
    `pointed_rows`, and the rows and GitHub's responses of the usable retrieval before it. The
    comparison reports an absent earlier retrieval, which makes the absence a value of its own
    rather than None.

    The comparison also reads the collected list, which is left out on purpose. It compares each
    new retrieval with the collection before it. The sampling stage runs before the cloning
    step, and the list holds that collection when the comparison runs. The cloning step rewrites
    the list later in the same run. If the comparison were recorded by the list, it would run
    again at the next Start after every completed run and compare the retrieval with the run's
    own collection. Because that Start ran a step, the entrypoint would then pack another
    handover of the run. `--from retrieval_comparison` compares the retrieval with the collected
    list on disk.
    """
    entries = pointed_rows(step)
    base = runner.data_dir(step)
    try:
        previous = None
        if entries[0][1] is not None:
            rid = ghs_cache.read_pointer(base / POINTER)["retrieval_id"]
            previous = ghs_cache.previous_retrieval(rid, base / CACHE_DIR)
        entries.append((PREVIOUS_ROWS, "absent" if previous is None
                        else (_rows_digest(base / CACHE_DIR, previous)
                              or f"{previous['retrieval_id']} without its files")))
    except ghs_cache.CacheError:
        return entries[:1] + [(PREVIOUS_ROWS, None)]
    except OSError as e:
        raise runner.UnreadableOutput(step.key, PREVIOUS_ROWS, e) from e
    return entries

STEPS = [
    Step("ghs_download", runner.VENV_PYTHON, ["1_download_ghs.py"],
          produces=(POINTER,), completion=COMPLETION_OUTPUT, irreproducible=True,
          report="ghs_retrieval_report.json", root=ROOT, code=CODE, imports=("requests", "yaml"),
          stage=STAGE),
    # Also recorded by the responses that it writes, so that the step runs again when they are
    # gone.
    Step("github_query", runner.VENV_PYTHON, ["2_query_github.py"],
          inputs=(POINTER,), code_inputs=(CACHE_MODULE,), completion=COMPLETION_INPUTS,
          root=ROOT, code=CODE, imports=("requests",), env=("GITHUB_TOKENS|GITHUB_TOKEN",),
          reads=(POINTER,), stage=STAGE, inputs_of=pointed_rows),
    Step("retrieval_comparison", runner.VENV_PYTHON, ["3_compare_retrievals.py"],
          produces=("ghs_changes.csv", "ghs_collection_changes.csv", "ghs_changes.json"),
          inputs=(POINTER,), code_inputs=(CACHE_MODULE,), completion=COMPLETION_INPUTS,
          root=ROOT, code=CODE, reads=(POINTER,), stage=STAGE, inputs_of=compared_rows),
    Step("repository_filtering", runner.VENV_PYTHON, ["4_filter_ghs.py"],
          produces=("ghs_filtered.json", "ghs_licenses.csv"),
          inputs=(POINTER,),
          code_inputs=(CACHE_MODULE, "config/ghs_filter.yaml", "config/licenses.csv",
                       "config/spdx_licenses.json"),
          completion=COMPLETION_INPUTS, root=ROOT, code=CODE, imports=("requests", "yaml"),
          reads=(POINTER,), stage=STAGE, inputs_of=pointed_rows),
]


def share_top_languages(steps: list) -> list:
    """The steps, with the filter's `--top-languages` from a step argument passed to the
    download as well.

    The download checks whether a language that the end date of the sampling window left out
    is among the languages that the filter would keep (`check_left_out` in 1_download_ghs.py).
    Without a list of the languages that the previous sampling frame kept, such a language fails
    the retrieval, and with one, a warning names it. The download reads the number of languages
    from the filter's config. A step argument that sets another number for the filter would make
    the download decide over languages that the filter does not keep. The step's words are
    parsed as the filter's argparse parses them. The last value counts, and `--top-languages=N`
    counts as well. An abbreviation such as `--top N` does not, because the filter takes long
    options by their full names only. A value that the filter cannot parse is not passed on, and
    the filter reports it."""
    keys = [step.key for step in steps]
    if "ghs_download" not in keys or "repository_filtering" not in keys:
        return steps
    filtering = steps[keys.index("repository_filtering")]
    parser = argparse.ArgumentParser(add_help=False, exit_on_error=False, allow_abbrev=False)
    parser.add_argument(TOP_LANGUAGES, type=int, dest="top")
    try:
        top = parser.parse_known_args(filtering.args[1:])[0].top
    except argparse.ArgumentError:
        return steps
    if top is None:
        return steps
    return [replace(step, args=[*step.args, TOP_LANGUAGES, str(top)])
            if step.key == "ghs_download" else step for step in steps]


def build_parser() -> argparse.ArgumentParser:
    parser = cli.ArgumentParser(description="Run the sampling stage end to end.")
    parser.add_argument("--list", action="store_true", help="Print the stage with its steps and exit")
    parser.add_argument("--from", dest="from_name", action="append", default=None,
                        help="Run this step again, with every step that reads its output, or every "
                             "step of the stage with --from sampling (repeatable)")
    parser.add_argument("--step-arg", action="append", default=[], dest="step_args",
                        metavar="NAME:ARGS",
                        help="Append arguments to the command of one step, such as "
                             'repository_filtering:"--top-languages 5", which the GHS download '
                             'receives too (repeatable)')
    parser.add_argument("--mark-complete", default=None, metavar="STEP", dest="mark_complete",
                        help="Record STEP as complete from its output on disk, without running it")
    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    if runner.refuses_mark_complete(args):
        return 1
    steps = list(STEPS)
    if args.step_args:
        steps = share_top_languages(runner.apply_step_overrides(steps, args.step_args))
    return runner.run(steps, list_only=args.list, from_name=args.from_name,
                      mark=args.mark_complete, argv=sys.argv,
                      min_free_gb=MIN_FREE_DISK_GB, log_dir=SAMPLING_DIR / "run_logs",
                      credentials_path=credentials.default_path())


if __name__ == "__main__":
    sys.exit(main())
