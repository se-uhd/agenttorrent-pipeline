"""The one command line: `python -m agentcontext <command> [arguments]`.

`run` is the runner, and every step, check and tool is a subcommand. The command line passes the
remaining arguments to the `main` of the command's module. The runner starts each step as a child
process through its module, `python -m agentcontext.steps.<name>`, and the step and its command
share one parser and one `--help`.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from agentcontext import logs

# The exit code for arguments that cannot be parsed. argparse exits 2 for an unknown flag, and 2
# is also the verdict that a command gives through its exit code when it finds a difference. The
# command line exits 1, "could not check", instead, so that a mistyped flag does not read as a
# difference.
USAGE_ERROR = 1


class ArgumentParser(argparse.ArgumentParser):
    """argparse's parser, with a usage error that exits `USAGE_ERROR` rather than 2, and with
    long options matched by their full names only. Every command of the package and every runner
    uses it, and its subparsers are of the same class.

    By default, argparse accepts an abbreviation of a long option: a runner would take
    `--fro release` for `--from release`, and a step `--fre` for `--fresh`. The container's
    entrypoint recognizes the arguments for one Start in the `command:` of compose.yaml, such as
    `--from`, `--mark-complete` and the `--fresh` of a `--step-arg` value, by their full names
    only. It passes every argument that it does not recognize at every Start. An abbreviation
    that a parser accepted would therefore act again at every Start, and the abbreviation
    `--step repository_cloning:--fresh` would collect the whole sample again in every monthly
    run. Because the parser refuses it, the abbreviation is a usage error at the first Start.
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str):
        try:
            super().error(message)
        except SystemExit as stop:
            if stop.code == 2:
                raise SystemExit(USAGE_ERROR) from None
            raise

COMMANDS = {
    "run": ("agentcontext.runner", "run the steps of the collection and publication stages in order, resuming from the pipeline execution state"),
    'engineered-project-selection': ("agentcontext.steps.engineered_project_selection", "derive the sample from the labels of the classification"),
    'repository-cloning': ("agentcontext.steps.repository_cloning", "clone every repository in the list and copy the files that the path patterns match"),
    "commit-reclassification": ("agentcontext.steps.commit_reclassification", "apply the current commit heuristics to the commit logs that the cloning step kept"),
    'collected-file-cleanup': ("agentcontext.steps.collected_file_cleanup", "derive the validated tree from the collected tree without the files that are not configuration"),
    'detection': ("agentcontext.steps.detection", "classify every collected file into a mechanism CSV"),
    'file-history': ("agentcontext.steps.file_history",
                     "fill the Git columns of the rows that file_history.csv and "
                     "ai_authorship.csv have no values for"),
    'import-detection': ("agentcontext.steps.import_detection", "write output/imports.csv, the imports of the configuration artifacts and the files that they reach"),
    "output-cross-check": ("agentcontext.steps.output_cross_check", "check the output CSVs against each other"),
    "unmatched": ("agentcontext.checks.unmatched", "paths ranks the path globs in other.csv that no heuristic explains. commits ranks the unmatched attributions. reduce writes the unmatched attributions of a collection as the tracked archive"),
    "deleted-files-report": ("agentcontext.checks.deletions", "count the rows of deletion.csv per reason, and check them against the two trees and the output CSVs"),
    'collected-files-archive': ("agentcontext.steps.collected_files_archive", "archive the validated tree, without the files that prune.is_prunable names, as release/repos_data.7z"),
    'release': ("agentcontext.steps.release", "build the release under release/"),
    "snapshot": ("agentcontext.checks.snapshot", "write regenerates output/snapshot.json, or compares it with --check. validation checks every repository against GitHub"),
    "regressions": ("agentcontext.checks.regressions",
                    "check compares output/ with the regression manifest, output/baseline.json. "
                    "compare lists the differences between two directories of the collection "
                    "stage"),
    "commit-attribution-validation": ("agentcontext.checks.commits", "match the commit patterns against the commit CSV again"),
    "measure": ("agentcontext.tools.measure", "query the sizes of the repositories from the GitHub API"),
    "subsample": ("agentcontext.tools.subsample", "select the subsample, or compare it with --check"),
    "docs": ("agentcontext.tools.docs", "fetch every vendor page that the heuristics cite and list the pages that changed"),
    "mirror-run": ("agentcontext.tools.mirror_run", "collect and publish the subsample from local Git mirrors, and compare the outputs with a reference run"),
    "evaluation": ("agentcontext.tools.evaluation", "sample draws the samples of the detection accuracy evaluation. report estimates precision and recall from their labels"),
    "tool-selection": ("agentcontext.tools.tool_selection", "count the repositories and the owners with a root-level path of each tool in a collection's file lists"),
    "statistics": ("agentcontext.tools.statistics", "write every stated statistic from docs/statistics.json, or compare them with --check"),
    "verify": ("agentcontext.tools.verify", "run every suite, every check and git status, and exit 0 only when all of them pass"),
}


def subcommand_usage(command: str, subcommands: dict, summary: str) -> str:
    """The usage line of a command that has subcommands of its own."""
    return (f"usage: python -m agentcontext {command} <subcommand> [arguments]\n\n"
            f"subcommands: " + ", ".join(subcommands) + "\n" + summary + "\n")


def subcommand_main(command: str, subcommands: dict, summary: str, argv=None) -> int:
    """Run one of a command's subcommands, or print its usage.

    Every module with subcommands calls this one dispatcher, so that the usage line, the exit
    codes and the message for an unknown subcommand are the same for all of them.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(subcommand_usage(command, subcommands, summary), end="")
        return 0 if argv else USAGE_ERROR
    if argv[0] not in subcommands:
        names = list(subcommands)
        listed = (f"{', '.join(names[:-1])} and {names[-1]}" if len(names) > 1
                  else ", ".join(names))
        logs.error("cli", f"Unknown subcommand {argv[0]!r} of {command}. The subcommands of "
                          f"{command} are {listed}.")
        return USAGE_ERROR
    return subcommands[argv[0]](argv[1:])


def usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [f"  {name:<{width}}  {text}" for name, (_, text) in COMMANDS.items()]
    return ("usage: python -m agentcontext <command> [arguments]\n\n"
            "commands:\n" + "\n".join(lines) + "\n\n"
            "Each command takes --help for its own arguments.\n")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(usage(), end="")
        return 0 if argv else USAGE_ERROR
    name, rest = argv[0], argv[1:]
    if name not in COMMANDS:
        logs.error("cli", f"Unknown command {name!r}. Run `python -m agentcontext --help` "
                          f"for the list of commands.")
        return USAGE_ERROR
    module = importlib.import_module(COMMANDS[name][0])
    code = module.main(rest)
    return 0 if code is None else int(code)
