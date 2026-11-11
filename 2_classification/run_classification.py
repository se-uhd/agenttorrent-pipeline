#!/usr/bin/env python3
"""
Run the classification stage. A model labels every repository in the sampling frame as an
engineered software project or not: it reads the README with an outline of the files, what GitHub
records about the repository and the activity of its issues and pull requests, and it reads a
linked page that could change a field that these do not state outright or that rests on an
inference. The stage then compares the labels with the
previous run's and selects the repositories labeled as engineered software projects as the
sample.

The stage is a list of two steps that agentcontext/runner.py runs. The runner records the labeling
with its inputs: the sampling frame, the two prompts and the modules that the script imports. A
new sampling frame or a changed prompt therefore runs the labeling again. A run over an unchanged
sampling frame reuses the label of every repository with an unchanged README, from the cache of
labels under labels/, and sends only the other repositories to the model. A changed module
runs the labeling again too, and when the current rules derive each decision of the finished run
over the same sampling frame unchanged, the labeling completes that run without a new run and
without a README request, so that the sample that the collection clones stays. The run ends by
comparing its labels with the labels of the previous run and by labeling each changed repository
again to determine why its label changed (changed_labels.py). The engineered project selection, a
module of the package, writes the sample that the collection clones.

Inputs: ../data/sampling/ghs_filtered.json, prompts/classification-prompt.md,
        prompts/page-summary-prompt.md, labels/, GitHub access
        tokens and an OpenRouter API key, for the default model, in credentials.csv at the
        repository root or in GITHUB_TOKENS, GITHUB_TOKEN and OPENROUTER_API_KEY
Outputs: repos_annotated.csv, repos_annotated.json, repos_excluded.csv, label_changes.csv, labels/,
         repos_annotated_included.csv,
         run_logs/state.json

Usage:
  python3 run_classification.py
  python3 run_classification.py --list
  python3 run_classification.py --step-arg repository_labeling:"--workers 4"
"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
from agentcontext import CLASSIFICATION_DIR, credentials, runner
from agentcontext import cli
from agentcontext.runner import COMPLETION_INPUTS, Step

# The stage's data directory under the data root, and the directory of its scripts.
ROOT = "classification"
CODE = "2_classification"
# The step clones nothing, because GitHub's API returns the README and the outline of the files.
# It writes only the run's partial file and the cache of labels (run-classification-spec.md).
MIN_FREE_DISK_GB = 2
SAMPLING_FRAME = "../sampling/ghs_filtered.json"
PROMPT = "prompts/classification-prompt.md"
SUMMARY_PROMPT = "prompts/page-summary-prompt.md"
MODULES = ("label_cache.py", "readmes.py", "forks.py", "classification_tools.py", "endpoints.py",
           "mcp_bridge.py", "changed_labels.py", "costs.py")

STEPS = [
    Step("repository_labeling", runner.VENV_PYTHON, ["1_classify_repos.py"],
          produces=("repos_annotated.csv", "repos_annotated.json", "repos_excluded.csv",
                    "label_changes.csv"),
          inputs=(SAMPLING_FRAME,), code_inputs=(PROMPT, SUMMARY_PROMPT, *MODULES),
          completion=COMPLETION_INPUTS, root=ROOT, code=CODE, reads=(SAMPLING_FRAME,),
          imports=("openai", "mcp", "pydantic", "lingua", "html2text", "requests"),
          env=("OPENROUTER_API_KEY", "GITHUB_TOKENS|GITHUB_TOKEN"), stage="classification"),
    # The last step: the sample from the labels. Its module is part of the package, and the
    # collection's own list has the same step, because the container runs that list alone.
    replace(next(s for s in runner.CORE_STEPS if s.key == "engineered_project_selection"),
            consumes=("repository_labeling",)),
]


def build_parser() -> argparse.ArgumentParser:
    parser = cli.ArgumentParser(description="Run the classification stage end to end.")
    parser.add_argument("--list", action="store_true", help="Print the stage with its steps and exit")
    parser.add_argument("--from", dest="from_name", action="append", default=None,
                        help="Run this step again, with every step that reads its output, or every "
                             "step of the stage with --from classification (repeatable)")
    parser.add_argument("--step-arg", action="append", default=[], dest="step_args",
                        metavar="NAME:ARGS",
                        help="Append arguments to the command of one step, such as "
                             'repository_labeling:"--workers 4" (repeatable)')
    parser.add_argument("--mark-complete", default=None, metavar="STEP", dest="mark_complete",
                        help="Record STEP as complete from its output on disk, without running it")
    return parser


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    if runner.refuses_mark_complete(args):
        return 1
    steps = list(STEPS)
    if args.step_args:
        steps = runner.apply_step_overrides(steps, args.step_args)
    return runner.run(steps, list_only=args.list, from_name=args.from_name,
                      mark=args.mark_complete, argv=sys.argv,
                      min_free_gb=MIN_FREE_DISK_GB, log_dir=CLASSIFICATION_DIR / "run_logs",
                      credentials_path=credentials.default_path())


if __name__ == "__main__":
    sys.exit(main())
