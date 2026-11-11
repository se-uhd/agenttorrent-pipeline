#!/usr/bin/env python3
"""
Run the collection and the publication in order: clone the repositories, validate the collected
files, detect the configuration artifacts, add the Git columns and build the release. With
--with-commit-reclassification, the runner also reclassifies the commit logs after the cloning
step and runs the two checks over the commit CSVs before the publication. A commit CSV that does
not match what the heuristics give then stops the run before the release is built.

Each step is one of the modules documented under docs/. This module decides the order, the
interpreter, and what to do when a step fails or has already succeeded. It uses nothing of a step
beyond its exit code and the files that the step declares that it writes. The run resumes from
run_logs/state.json and holds the run lock, run_logs/run.lock, so that one run at a time uses the
directory. The pre-checks run before any step starts. The runner forwards SIGTERM and
SIGINT to the running step and then ends with exit code 128 plus the signal number, without
recording the step. docs/runner.md describes the full behavior.

Inputs: none (the steps read the files already on disk).
Outputs:
  - run_logs/<step>.log
  - run_logs/state.json: which steps are complete and the fingerprint that recorded each of
    them (a hash of its script, a hash of its script and inputs, or a checksum manifest of its
    output), how, when and with which code each was recorded, and the run's provenance (Git
    commit, command, status and duration of each step, machine). Written atomically after every
    step

Usage:
  python3 -m agentcontext run
  python3 -m agentcontext run --with-commit-reclassification
  python3 -m agentcontext run --from detection
  python3 -m agentcontext run --list
  python3 -m agentcontext run --mark-complete repository_cloning
  python3 -m agentcontext run --step-arg repository_cloning:"-w 10"
  python3 -m agentcontext run --repo-csv /tmp/trial/repos_annotated_included.csv
"""

import argparse
import ast
import csv
import errno
import fcntl
import fnmatch
import hashlib
import glob
import http.client
import json
import math
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from agentcontext import DATA_ROOT, REPO_ROOT, SEVEN_ZIP, STAGE_DIR
from agentcontext import cli

from agentcontext import (caches, commits, credentials, imports, logs, heuristics, paths, planning,
                          planning_schema, resume)
from agentcontext import fingerprints
from agentcontext.steps import release

# The runners of the other stages, and run_pipeline.py at the root, are lists of steps that this
# module runs. Nothing in the module is specific to the collection stage, because a step names its
# own directories, `root` under the data root and `code` under the repository root.
LOG_DIR = STAGE_DIR / "run_logs"
LOCK_PATH = LOG_DIR / "run.lock"
STATE_PATH = LOG_DIR / "state.json"
# GROUNDING: the cloning step clones into the scratch directory, one clone per worker, and
# bounds.DEFAULT_MAX_BYTES bounds each clone at 32 GiB. With the step's default of five
# workers, 160 GiB of clones can be in flight. The trees under the data root are small beside
# that. The minimum is that worst case plus 30 GiB of room, and it changes with the clone bound.
# It is a minimum of free space, not a limit on the size of the scratch directory. The cloning
# step checks the same minimum again while it runs (`guards.MIN_FREE_GB`, and a test checks that
# the two are equal), because these pre-checks measure once and the step runs for days.
MIN_FREE_DISK_GB = 190
# The part of MIN_FREE_DISK_GB that the clones in flight take: the step's default number of
# workers times the clone bound, and a test checks that it equals that product. A start with no
# cloning step pending has no clone in flight and needs only the rest of the minimum. The whole
# minimum would refuse such a start, a resume of the steps after the collection, on a volume
# with room for everything that those steps write.
CLONES_IN_FLIGHT_GB = 160
# The share of the repositories in its list that a collection may record as clone_failed and
# still be recorded as complete by --mark-complete, at least one repository. Above it, the cloning
# step exits 4 (guards.CLONE_FAILURE_SHARE and guards.clone_failure_ceiling, and a test checks
# that both are equal to these). A collection above the share that is recorded as complete would
# make every later start skip the step and publish the repositories that failed to clone as
# absent.
CLONE_FAILURE_SHARE = 3 * 17 / 36710


def clone_failure_ceiling(repositories: int) -> int:
    """The ceiling that `guards.clone_failure_ceiling` computes, repeated here so that the
    runner does not import `guards`."""
    return max(1, math.ceil(CLONE_FAILURE_SHARE * repositories))
# The outcomes that those rows record, as in `resume.TRANSIENT_OUTCOMES`. A test of the runner
# checks that the two are equal.
TRANSIENT_OUTCOMES = ("clone_failed", "collector_error")
# The outcomes that `write_execution_state` writes for a start that ran no step: a refusal by the
# pre-checks, a refusal by the check for modified inputs, a `--mark-complete` that recorded
# a step, and a stop before the first step. The container's entrypoint reads them to decide that the
# start has nothing to hand over. A refusal by the run lock writes no outcome.
NO_STEP_OUTCOMES = ("pre_checks_failed", "modified_inputs", "marked_complete",
                    "stopped_before_steps")
# The outcome of a start that a stop ended before any of its steps started. The stop can come while
# the runner computes which steps are complete, during the pre-checks, in the loop over the
# steps that the start skips, or as a second signal. The pre-checks can wait for hours for
# GitHub access tokens that GitHub rejects (GITHUB_REJECTED_WAITS), whatever else it found. Recorded
# as `interrupted`, such a stop would replace the outcome of the run before it. The entrypoint would
# then no longer see a run that had failed in its analysis stage, and the next Start would run the
# failing step again and hand over the data root a second time.
STOPPED_BEFORE_STEPS = "stopped_before_steps"
# The outcome of the last start that ran steps. A start that ran none leaves it unchanged. The
# container's entrypoint decides from it whether a Start begins the next monthly run. A Start that
# the pre-checks refused must therefore not replace the "success" of the run before it, or
# the next Start would resume a finished run and the monthly run would begin one Start later.
LAST_RUN_OUTCOME = "last_run_outcome"
# The finish time of the last start that ran a step, written beside LAST_RUN_OUTCOME. A start
# that skipped every step, a refusal and a `--mark-complete` leave it unchanged, although each of
# them writes the pipeline execution state. The container's entrypoint records it with the
# handover of a completed run. A later start that skips every step can then tell that the
# handover contains the data root as it is, which the time of the pipeline execution state cannot
# show.
LAST_STEPS_FINISHED_AT = "last_steps_finished_at"
# The step results of the last start that ran a step, written with LAST_STEPS_FINISHED_AT. A
# refusal, a `--mark-complete` and a check that stops the run before its first step write step
# results in which no step ran. The container's entrypoint reads from this list the step that
# failed the last run. After a run that failed in the analysis stage, a refused Start then does
# not make the next Start resume that run, run the failed step again and pack the run's data root
# a second time before the next monthly run.
LAST_RUN_STEPS = "last_run_steps"
# The directory where every step that clones puts its clones, unless --scratch-dir names another.
# The cloning steps share it, and a container mounts one volume for it. The pre-checks
# measure the volume that the clones are written to rather than $TMPDIR, which in a container is
# the overlay layer. The directory is gitignored.
DEFAULT_SCRATCH_DIR = STAGE_DIR / "_scratch"
# The `engineered_project_selection` step writes this file into the data directory of the
# classification stage, and the path is therefore relative to the collection stage's directory
# rather than a bare name. The file is repos_annotated.csv filtered on engineered_project ==
# "true". It is not tracked, and `engineered_project_selection` writes it over an empty data root.
DEFAULT_REPO_CSV = "../classification/repos_annotated_included.csv"

STEP_PYTHON_ENV_VAR = "DATA_COLLECTION_PYTHON"

# How run_logs/state.json records a finished step, and therefore what makes a recorded
# completion stale.
#
# `script` is the default. It fits a step with an output that can be derived again from its
# input. The input is still on disk, and after a change to the script the result no longer
# matches the code that would produce it. Running the step again is then both possible and
# correct.
#
# `output` fits a step that takes a snapshot of something outside the pipeline.
# `repository_cloning` reads GitHub on a given day, and no edit to its script reproduces the day
# that it read. Running it again takes a new snapshot rather than reproducing the old one. With
# the script hash as its record, the first edit to the collector would mark the step as not done,
# and a resumed run would run the collector again over every repository in its input list, on top
# of a tree that is already on disk and is still exactly what the step wrote. The runner records
# the step's output instead, and through `inputs_of` what else decides the step: for
# `repository_cloning`, the collector's code, the version of git, the path heuristics, the
# import heuristics and its list (`collection_inputs`). The tree alone records none of them. A
# change of the version of git alone leaves a recorded step recorded (`kept_on`). A change of
# any other of them runs the step, which then takes over the repositories that the change does
# not reach.
COMPLETION_SCRIPT = "script"
COMPLETION_OUTPUT = "output"
# `inputs` fits a step with a result that depends only on its script and on named input files,
# such as an analysis script, which writes a figure or a CSV from the output CSVs of the
# collection. The record is a hash over the script and the contents of every declared input. A
# changed CSV removes exactly the steps that read it from the completed steps, and a changed
# script removes its own step.
COMPLETION_INPUTS = "inputs"

# The key of the pipeline execution state that lists the irreproducible steps that a run started
# and did not record as complete. The runner adds a step before it starts and removes it when it
# records the step. A failure, a stop and a kill all leave the step listed, and the next run takes
# the snapshot again rather than recording the part of it that is on disk.
UNFINISHED = "unfinished"
# The prefix of the record of a step that a run set out to run again, because its input changed
# or a step that it consumes ran again, and that has not finished. The record stays, because the
# check for modified inputs tests whether a step ever ran. With the prefix, the step no longer
# counts as done, because its output on disk is no longer what the step would write. Without it,
# a failure or a stop after the runner recorded the step's producer again would leave the old
# record matching the unchanged script, and the next start would skip the step and every step
# after it.
STALE = "stale:"
# The key of the pipeline execution state that names, for each step with an output that a later
# step modifies in place, the steps that started to modify that output since the step last ran.
# The runner names a modifying step before it starts. A failure, a stop or a kill after the
# modification leaves the name, and the runner removes the name only when it runs the producing
# step again and records that step. The completion records alone cannot show the modification:
# a modifying step that failed is not recorded, and `--from` removes its record at every monthly
# Start.
MODIFIED = "modified"
# The key of the step that the run has started and that has not ended. The runner writes it with
# the outcome `running` just before the step starts. It removes it at the next write of the
# provenance with step results that record a step that ran. A kill of the runner inside the step,
# which no handler catches, therefore leaves the step named, also through later starts that run
# no step. The container's entrypoint reads the key to tell whether a `--fresh` of a step took
# effect: a kill in an earlier step leaves the collection untouched, and a kill in the step
# itself can have deleted the collection.
RUNNING_STEP = "running_step"
# The key of the `started_at` of the start that wrote RUNNING_STEP. With it, the entrypoint
# distinguishes the step of the latest start from the step of an earlier start that a kill ended.
RUNNING_STEP_STARTED_AT = "running_step_started_at"
# The key of the step results of the start that wrote RUNNING_STEP, up to that step. With them,
# the entrypoint tells whether the release step came before the step that a kill ended. A later
# start that a check stops writes its own `steps`, which end where the check stopped the start.
RUNNING_STEP_RESULTS = "running_step_results"
# The key of what the running step states that it waits for, such as credit for the requests of
# a model (`logs.step_status`). The runner names the file of the status in the step's
# environment and copies the file under this key at each line of the step's log, and it removes
# the key when the step ends. A person who reads the state during a wait of days sees what to
# do, without a push notification service.
RUNNING_STEP_STATUS = "running_step_status"
# The key of the record of a step that exited 0 and that the runner has not recorded yet, with
# the inputs part that the step ran under (`inputs_before`) and the code that it ran. The runner
# writes it before it computes the fingerprint that records the step, which can take minutes over
# the collected tree, and removes it together with that record. After a kill or a power loss in
# between, the next run records the step from its output (`record_exited_step`) rather than
# running it again. The cloning step closes its collection before it exits 0. Run again, it
# would continue the collection that ended and take over every repository, and every step after
# it would run again.
EXITED = "exited"
# The steps that a start recorded from EXITED and to which no start has given a result yet. The
# start that records such a step can end by a refusal, a stop or a kill before its loop reaches
# the step, and the next start that reaches the step gives it the status `ok`
# (`record_exited_step`).
RECORDED_FROM_EXIT = "recorded_from_exit"
# The key of what each record of `completed` came from, by step key: the fingerprint that the
# entry describes, how the step was recorded (one of COMPLETED_HOW), when, and the commit of the
# code that recorded it. The line that skips a step names them. A Build in Container Manager
# creates the container again and discards its log, and the line is then the only record in
# the log of when and with which code a step that a later Start skips was completed. The entry
# names its fingerprint and counts only while `completed` holds that fingerprint. A record that
# `--from` removed, that `mark_stale` prefixed or that was edited by hand (docs/runner.md) then
# leaves its entry unread, and no code has to remove it.
COMPLETED_BY = "completed_by"
# `ran`: the step ran and exited 0, also when the next invocation recorded it after a kill
# (EXITED). `adopted`: the output of an irreproducible step was on disk
# (`adopt_irreproducible`). `marked_complete`: `--mark-complete` recorded it.
COMPLETED_HOW = ("ran", "adopted", "marked_complete")
# The labels that `collection_inputs` gives the inputs of the cloning step.
COLLECTOR_CODE = "the collector's code"
GIT_VERSION = "the version of git"
PATH_HEURISTICS = "the path heuristics"
IMPORT_HEURISTICS = "the import heuristics"
REPOSITORY_LIST = "the list of repositories to collect"

# File names left out of an output manifest, because the pipeline provably discards them and no
# published value can depend on one.
#
# `collected_file_cleanup.is_valid_file` rejects `.DS_Store` as a file at any depth, and the
# collected file cleanup deletes it from the validated tree with the reason `macOS Finder file`.
# A `.DS_Store` that Finder writes into the collected tree therefore changes no row. Counted in
# the manifest, it would mark `repository_cloning` as incomplete, and the next run would run the
# collector again over its whole input list. `test_runner` asserts that the cleanup rejects every
# name here, which keeps the two lists from differing.
DISCARDED_NAMES = frozenset({".DS_Store"})
# Directory names left out of an output manifest for the same reason. Synology DSM writes an
# `@eaDir` directory into the directories that its File Station and indexing services visit, and
# on the NAS these include the collected tree. `collected_file_cleanup.scan_and_clean_repo`
# deletes every directory of that name from the validated tree with the reason `Synology DSM
# directory`. Only a directory is left out, because the cleanup deletes only a directory, and a
# link with the name is described like any other link. `test_runner` asserts that the cleanup
# deletes every name here. Every comparison of a tree with a record uses this list
# (`paths.DISCARDED_DIRECTORIES`).
DISCARDED_DIRECTORIES = paths.DISCARDED_DIRECTORIES


def resolve_step_python(env: dict, executable: str) -> str:
    """Pick the interpreter for the one step with a third-party dependency.

    `detection` needs `lingua` to read the natural language of every configuration artifact
    in a CSV that has the column.

    The candidates, in order:

    1. ``DATA_COLLECTION_PYTHON``: the explicit override, for an interpreter other than the
       one that runs the runner.
    2. ``executable``, the interpreter that runs the runner: on a development machine the one
       of `.venv`, which `mise exec` activates and which holds the distributions of the
       dependency lock locks/development.txt, and in the image the interpreter of the base
       image, which holds those of locks/pipeline.txt. A virtual environment at another path,
       such as a `venv/` in the checkout, holds distributions that no dependency lock lists,
       so the runner looks for none.

    This function does not check the interpreter. The pre-checks verify whether it can import
    the package, and no subprocess runs at import time.
    """
    override = (env.get(STEP_PYTHON_ENV_VAR) or "").strip()
    return override or executable


VENV_PYTHON = resolve_step_python(os.environ, sys.executable)


@dataclass(frozen=True)
class Step:
    """One pipeline step.

    ``key``          the step's identifier in the pipeline execution state and in
                     the name of its ``.log`` file, and what ``--from`` and
                     ``--step-arg`` match
    ``interpreter``  the Python that runs the step: ``VENV_PYTHON`` for
                     detection, which needs a third-party package, and for the
                     steps that share its interpreter, and a bare ``python3``
                     for the rest
    ``args``         argv after the interpreter: the script, then its flags, or
                     ``-m``, a module of the ``agentcontext`` package, then its
                     flags. ``step_module`` reads the second form
    ``produces``     paths, relative to `root`, that this run must have
                     written by the time the step exits 0. A file that is
                     present but unchanged since before the step started was
                     left by an earlier run. A step that exits 0 without
                     writing its output did nothing, and the next step would
                     read the previous run's file without any later step being
                     able to detect it. Empty for a step that writes only log
                     messages
    ``consumes``     the steps from which this step's output is
                     computed. A step that runs again changes its outputs, and
                     nothing computed from them is still this run's result
                     (`invalidated_by`). These are dependencies, not positions
                     in the list: `collected_files_archive` runs after
                     `output_cross_check` but reads nothing that
                     `output_cross_check` writes, and running
                     `output_cross_check` again must not run
                     `collected_files_archive` again
    ``mutates``      the steps with outputs that this step modifies in place.
                     After it has run, those outputs are no longer what their
                     producer wrote, and only running the producer again
                     restores them (`modified_input_problems`)
    ``reads``        paths, relative to `root`, that the step opens and that no
                     step of a plain run necessarily writes, such as the
                     tracked caches. The pre-checks require each of them to be
                     on disk, or produced by a pending step, before any step
                     runs. The detection step joins two of them
    ``completion``   what a finished run records for this step:
                     COMPLETION_SCRIPT (the hash of its script),
                     COMPLETION_OUTPUT (a checksum manifest of `produces`) or
                     COMPLETION_INPUTS (a hash of the script and of `inputs`).
                     The comments of the three constants describe which steps
                     use which policy
    ``root``         the directory in which the step runs and against which its
                     data paths resolve, relative to the data root. Empty for
                     the collection stage's directory, `STAGE_DIR`. With this
                     field, one list of steps can cover the five stages
    ``code``         the directory that contains the step's script and its
                     `code_inputs`, relative to the repository root. Empty for
                     a step that runs a module of the package, or a script in
                     `root`, as a test's stand-in does
    ``irreproducible``
                     running the step again takes another snapshot rather than
                     redoing its work, as for the GHS download. When the
                     outputs of such a step are on disk, the runner records it
                     as complete at the start of a run rather than running it
                     again, unless the pipeline execution state names it as a
                     step that a run started and did not finish.
                     `repository_cloning` is recorded by its output but is not
                     irreproducible, because a reconstructed sample is recorded
                     on purpose with --mark-complete
    ``inputs``       paths, relative to `root`, with contents that the
                     COMPLETION_INPUTS policy fingerprints beside the script
    ``code_inputs``  paths, relative to `code`, fingerprinted the same way: the
                     modules and the configuration that the script reads
    ``package_code`` whether the fingerprint of a script's code also covers the
                     modules of the package that the script and its code inputs
                     import, with their module closures
                     (`fingerprints.imports_closure`). Set for the analysis
                     scripts, because their tables depend on such a module, for
                     example on the column order of `imports.STATUSES`. Not set
                     for the sampling and the classification, which would query
                     GitHub and label the sample again after an edit to any
                     module that they import
    ``heuristics``   the files under `heuristics/` that the step reads,
                     fingerprinted the same way under `heuristics/<name>`. The
                     heuristics directory is beside the package rather than
                     under `root`. The container therefore reads the image's
                     copy, and a heuristic that changed in the image runs the
                     steps that read it again
    ``binaries``     executables that the step needs on PATH, which the
                     pre-checks verify when the step is pending, in addition to
                     the map BINARY_REQUIREMENTS for the collection's steps
    ``imports``      packages that the step's interpreter has to import,
                     checked the same way, because a step that cannot import
                     them fails only after the steps before it have run
    ``env``          environment variables that the step needs, checked the
                     same way, so that a missing API key stops the run before an
                     upload starts rather than partway through it. An entry that
                     names alternatives with `|` needs one of them
    ``report``       the path, relative to `root`, of a JSON object that the
                     step writes and that a finished run copies into the step's
                     entry of the pipeline execution state. The object records
                     what the step did that its exit code and outputs cannot
                     express, such as the download's fallback to an earlier GHS
                     retrieval. The runner keeps the object of the step's last
                     run until the step writes a new one, and records an object
                     only when this run wrote it
    ``stage``        the stage of the pipeline to which the step belongs:
                     sampling, classification, collection, publication or
                     analysis. `--list` shows the steps under their stage, and
                     `--from` takes a stage for all of its steps. The pipeline
                     execution state records steps, not stages, and a stopped
                     run resumes at the step in which it stopped
    ``inputs_of``    a function of the step that names what else decides it, as
                     pairs of a label and a digest, with None as the digest of
                     an absent input. The record includes the pairs beside the
                     output of a step recorded by its output, and beside the
                     inputs of a step recorded by its inputs.
                     `repository_cloning` names its collector's code, the
                     version of git, its path heuristics, its import
                     heuristics and its list. The content-quality analysis
                     names the files of the validated tree that the mechanism
                     CSVs list and the files that the GitHub Agentic Workflows
                     of their repositories import, which no fixed list of
                     paths can name
    ``kept_on``      labels of `inputs_of` with a change that alone leaves a
                     recorded step recorded, because running the step after it
                     would take a new snapshot, with a WARNING that names them,
                     where the record names the digest of each label
                     (`changes_kept`). `repository_cloning` names the version
                     of git
    ``extended``     outputs of `produces` that a later step changes in place,
                     such as the two caches: the file history step adds the
                     rows of the files that it measures and removes the rows at
                     heads that no later step reads. COMPLETION_OUTPUT records
                     that each of these outputs exists, not what it contains,
                     and the changes do not make the step that wrote them look
                     unfinished
    ``may_be_empty`` outputs of `produces` that the step rightly leaves empty,
                     such as the imported tree of a collection in which no
                     import reaches a file. Any other empty output marks a
                     failed run, which neither `--mark-complete` nor the
                     recording of an irreproducible step from its output on
                     disk records

    A dataclass, because the fields that a runner checks, such as what a step
    must have written and what it is computed from, belong on the step itself.
    """

    key: str
    interpreter: str
    args: list
    produces: tuple = ()
    consumes: tuple = ()
    mutates: tuple = ()
    reads: tuple = ()
    completion: str = COMPLETION_SCRIPT
    root: str = ""
    code: str = ""
    irreproducible: bool = False
    inputs: tuple = ()
    code_inputs: tuple = ()
    package_code: bool = False
    heuristics: tuple = ()
    binaries: tuple = ()
    imports: tuple = ()
    env: tuple = ()
    extended: tuple = ()
    may_be_empty: tuple = ()
    report: str = ""
    stage: str = ""
    inputs_of: object = None
    kept_on: tuple = ()

    def __post_init__(self):
        """Reject a declaration with a completion that could never be recorded.

        A step recorded by its output needs an output to record. Declared
        without one, it would always fingerprint to the empty string, and no
        run could ever record it as complete.

        This check rules out the declaration, not the empty fingerprint. An
        output that is declared and then absent gives `""` at runtime too, and
        `completed_steps` rules that case out: a step recorded by its output
        does not count as done while its output is missing.
        """
        if self.completion not in (COMPLETION_SCRIPT, COMPLETION_OUTPUT, COMPLETION_INPUTS):
            raise ValueError(
                f"Step {self.key!r} declares the completion {self.completion!r}, which is not "
                f"{COMPLETION_SCRIPT!r}, {COMPLETION_OUTPUT!r} or {COMPLETION_INPUTS!r}")
        if self.completion == COMPLETION_OUTPUT and not self.produces:
            raise ValueError(
                f"Step {self.key!r} declares completion={COMPLETION_OUTPUT!r} with an empty "
                f"`produces`. That completion records a checksum of `produces`.")
        if self.completion == COMPLETION_INPUTS and not (self.inputs or self.heuristics
                                                         or self.code_inputs):
            raise ValueError(
                f"Step {self.key!r} declares completion={COMPLETION_INPUTS!r}, but none of "
                f"`inputs`, `code_inputs` and `heuristics` names a file. That completion "
                f"records a hash of these files.")
        if self.irreproducible and self.completion != COMPLETION_OUTPUT:
            raise ValueError(
                f"Step {self.key!r} is irreproducible and needs "
                f"completion={COMPLETION_OUTPUT!r}, because it is recorded as complete from its "
                f"output on disk")
        for field in ("extended", "may_be_empty"):
            if set(getattr(self, field)) - set(self.produces):
                raise ValueError(
                    f"Step {self.key!r} names "
                    f"{sorted(set(getattr(self, field)) - set(self.produces))} in `{field}`, "
                    f"which may name only outputs of `produces`")
        if self.completion == COMPLETION_OUTPUT and set(self.produces) <= set(self.extended):
            raise ValueError(
                f"Step {self.key!r} declares completion={COMPLETION_OUTPUT!r}, but `extended` "
                f"names every output. That completion records the contents of the outputs "
                f"that `extended` does not name.")


def data_dir(step: Step) -> Path:
    """The directory that a step runs in and that its data paths resolve against."""
    if not step.root:
        return STAGE_DIR
    root = Path(step.root)
    return root if root.is_absolute() else DATA_ROOT / root


# The package with the modules that a step's code covers: a module of the package that a step
# runs, and the modules that the script of a step with `package_code` imports.
PACKAGE_DIR = Path(__file__).resolve().parent


def code_dir(step: Step) -> Path:
    """The directory that contains a step's script and its `code_inputs`."""
    if not step.code:
        return data_dir(step)
    code = Path(step.code)
    return code if code.is_absolute() else REPO_ROOT / code


# The mechanism CSVs, under the names that the heuristics give them. Two steps write all of
# them, and the list is derived once rather than typed for each.
MECHANISM_OUTPUTS = tuple(f"output/{name}"
                          for name in heuristics.csv_filenames())
# The planning documents, which the same two steps write beside the mechanism CSVs.
PLANNING_OUTPUT = f"output/{planning_schema.CSV_NAME}"

# What the release step writes, taken from the CSV that also defines the checksum manifest and
# the README. The file of the collected_files_archive step is in that CSV but not here, because
# `produces` names what the step itself has to have written, and the release only checks that file.
RELEASE_OUTPUTS = tuple(f"release/{entry.name}" for entry in release.RELEASE_FILES
                        if entry.kind != 'collected_files_archive')

# The steps with an interpreter that has to import lingua. `detection` reads the
# natural language of every configuration artifact in a CSV that has the column.
LINGUA_STEPS = frozenset({'detection'})


# Every path below is a script default (see each script's --help or document),
# except where a step's flags name it.
#
# `produces` names what the next step reads, not everything that the step writes. The check
# of `produces` guards against a step that exits 0 without writing anything. The next step
# would otherwise read the file that the previous run left, and every later CSV would mix the
# outputs of two runs without any sign of it.
CORE_STEPS = [
    # Keeps the rows of the classification stage's annotated CSV that are labeled true, the
    # list that the collector reads. A run over an empty data root derives that list rather
    # than receiving it.
    Step('engineered_project_selection',             "python3",   ["-m", "agentcontext.steps.engineered_project_selection"],
          produces=(DEFAULT_REPO_CSV,), consumes=(),
          completion=COMPLETION_SCRIPT, stage="classification"),
    # The one step of this list that is recorded by what it produced, a snapshot of GitHub on a
    # given day, and by what decides whether it takes a repository over from that snapshot: the
    # collector's code, the version of git, the path heuristics and its list
    # (`collection_inputs`). See COMPLETION_OUTPUT.
    #
    # `consumes` stays empty, because `engineered_project_selection` is deliberately not a
    # producer of this step. An edge would put `repository_cloning` in
    # `invalidated_by("engineered_project_selection")`, and an edit to
    # agentcontext/steps/engineered_project_selection.py that writes the same list would remove
    # `repository_cloning` from the completed steps. The list's content is an input of the record
    # instead: a new list runs the step, and the same list does not.
    #
    # The collected list is declared on its own, beside the directory that contains it.
    # `_wrote_output` returns True for any directory that exists, and with only `data/collected`
    # declared, the runner would no longer check whether this run wrote the list. The manifest
    # then lists that one path twice, which does no harm, because the encoding is
    # length-prefixed and the fingerprint stays deterministic.
    #
    # The imported tree is declared like the collected tree. Undeclared, a tree removed after the
    # step would leave the import detection step without its input at every Start until a run
    # from this step, and a tree that lost files would give rows that are `missing` without a
    # warning.
    #
    # `ai_authorship.csv` is declared beside `file_history.csv`, because the history query over
    # each copied path fills both, and the detection step reads both next. The commit CSV that the
    # same step writes is not declared (see COMMIT_RECLASSIFICATION_STEPS).
    #
    # The two caches are also `extended`. The file history step adds the rows of the files that it
    # measures to both, and removes the rows at a head that neither the collected list nor the
    # validated list names. Recorded by their contents, the caches would make the cloning step
    # count as unfinished at every Start after the file history step has run. A restart after a
    # late failure would then run the collector again over every repository in
    # `finished_repositories.jsonl`, and every step after it.
    #
    # A change of the version of git alone leaves the recorded step recorded, with a WARNING that
    # names the change (`kept_on`). Every line of a collection records the version of git, and a
    # run under another version collects every repository again, each at the head that it has
    # now, which is a new snapshot of the sample. `--from repository_cloning` takes it when it is
    # wanted. A change of the collector's code, the path heuristics or the import heuristics
    # runs the step, also beside another version of git. The step continues the collection in
    # progress or the last collection that ended over its list and collects again the
    # repositories that the change reaches (`reuse.outdated`), or begins a new collection where
    # there is none to continue. Kept instead, the step would leave the collected tree as the
    # earlier code and heuristics collected it, and the steps after it would publish the rows of
    # the new heuristics over that tree, with no row for a file that a new path heuristic
    # matches. A change of the list runs the step too: kept, the step would publish the labels
    # of the new sample beside the tree of the old one.
    Step('repository_cloning',           VENV_PYTHON, ["-m", "agentcontext.steps.repository_cloning"],
          produces=("data/collected/repos.csv", "data/collected", "data/imported",
                    "file_history.csv", "submodules.csv", "ai_authorship.csv"),
          extended=("file_history.csv", "ai_authorship.csv"), may_be_empty=("data/imported",),
          completion=COMPLETION_OUTPUT, inputs_of=lambda step: collection_inputs(step),
          kept_on=(GIT_VERSION,), stage="collection"),
    # Recorded by its inputs, not by its script alone, because it reads the heuristics and the
    # list of vendored directories. An image that changes only a heuristic then runs it again.
    # Every step below that reads a heuristics file is recorded the same way.
    Step('collected_file_cleanup',          "python3",   ["-m", "agentcontext.steps.collected_file_cleanup"],
          produces=("data/validated/repos.csv", "data/validated"),
          consumes=('repository_cloning',), completion=COMPLETION_INPUTS,
          heuristics=("config_path_patterns.csv", "vendored_dirs.txt",
                      "planning_document_patterns.csv", "local_state_paths.csv"),
          stage="collection"),
    # VENV_PYTHON rather than a bare "python3", because the step uses lingua to read the natural
    # language of every configuration artifact in a CSV that has the column, and
    # pre_checks() checks lingua only in the interpreter that VENV_PYTHON names.
    #
    # Every mechanism CSV is declared, and a detection step that exits 0 after writing only
    # some of them fails.
    #
    # The two caches that the step joins are `reads`, not `consumes`. Both are outputs of the
    # cloning step, which does not run in a regeneration over an earlier collection, where both
    # caches are already on disk.
    Step('detection',            VENV_PYTHON, ["-m", "agentcontext.steps.detection"],
          produces=("output/repos.csv", *MECHANISM_OUTPUTS, PLANNING_OUTPUT),
          consumes=('collected_file_cleanup',),
          reads=("file_history.csv", "ai_authorship.csv"),
          completion=COMPLETION_INPUTS,
          # import_patterns.csv decides where Claude Code reads a `.claude/AGENTS.md`, as it
          # decides for the import detection.
          heuristics=("config_path_patterns.csv", "vendored_dirs.txt",
                      "planning_document_patterns.csv", "local_state_paths.csv",
                      "import_patterns.csv"),
          stage="collection"),
    # `mutates` declares a dependency that the order of the steps does not show.
    # agentcontext/steps/file_history.py deletes from data/validated/ the directory of a
    # repository that git reports as gone. It deletes no single file, because it keeps a
    # published row with no history at the recorded commit (`_prune_from_repos`).
    # data/validated/ is the output of the collected file cleanup, and file_history never writes
    # it back. Once this step has run, the detection step, run again over data/validated/, reads
    # a smaller tree than the pipeline specifies and produces a different output/repos.csv
    # without a warning. Only the collected file cleanup restores the tree, by deriving
    # data/validated/ again from the untouched data/collected/.
    #
    # The step rewrites in place the output CSVs that detection wrote. It fills the Git columns
    # of every row that the cache has no values for, and the AI columns of the context files
    # among them. A run over a sample that the cache covers writes the same bytes back, and the
    # output check still counts the file as written: it compares each file with its signature
    # from before the step started, and a rewrite in place changes the file's mtime.
    #
    # The collected list is `reads`, as the two caches are for the detection step. The step
    # keeps the rows of the caches at the heads that the list names, and the cloning step, which
    # writes the list, does not run in a regeneration over an earlier collection.
    Step('file_history',          VENV_PYTHON, ["-m", "agentcontext.steps.file_history"],
          produces=("output/repos.csv", *MECHANISM_OUTPUTS, PLANNING_OUTPUT),
          consumes=('detection', 'collected_file_cleanup'), mutates=('collected_file_cleanup',),
          reads=("data/collected/repos.csv",), stage="collection"),
    # The imports of the configuration artifacts, over the validated tree as the file history
    # step leaves it and over the imported tree that the cloning step wrote. Recorded by the
    # import heuristics and by the content of the imported tree (`imported_tree_input`), which no
    # list of paths can name. A tree that changed under the same CSVs runs the step again.
    Step('import_detection',      "python3",   ["-m", "agentcontext.steps.import_detection"],
          produces=("output/imports.csv",),
          consumes=('repository_cloning', 'collected_file_cleanup', 'detection', 'file_history'),
          completion=COMPLETION_INPUTS,
          inputs_of=lambda step: imported_tree_input(step),
          heuristics=("import_patterns.csv", "config_path_patterns.csv"), stage="collection"),
    # Reads the mechanism CSVs (detection) and repos.csv (rewritten by
    # file_history).
    Step("output_cross_check",       "python3",   ["-m", "agentcontext.steps.output_cross_check"],
          produces=("output/consistency-report.csv",),
          consumes=('detection', 'file_history'), completion=COMPLETION_INPUTS,
          heuristics=("config_path_patterns.csv",), stage="collection"),
    # A check rather than a documented manual step. Every path glob in other.csv that occurs in
    # two repositories is either a mechanism to add to the heuristics or a row to add to
    # heuristics/not_configuration.csv with a reason. `--min-repos 2` lists each such path glob
    # for a person to read.
    #
    # The check writes its findings and cannot stop the run. A sample collected months after the
    # last one has path globs that the heuristics do not explain yet, because the tools keep
    # changing. A threshold here would stop the run after the cloning and before the archive and
    # the release, which publish nothing on their own. The release is a local directory that a
    # person uploads to Zenodo, and refusing to build it protects nothing. The run has to deliver
    # the whole dataset and the list of the unmatched path globs, which
    # `output/unmatched-paths.csv` records in the handover. `python -m agentcontext verify` and
    # CI, where a person is present, apply the threshold. `docs/heuristics.md` gives the reason.
    Step('unmatched_paths_report',   "python3",   ["-m", "agentcontext.checks.unmatched", "paths",
                                             "--min-repos", "2",
                                             "--out", "output/unmatched-paths.csv"],
          produces=("output/unmatched-paths.csv",),
          consumes=('detection', 'file_history'), completion=COMPLETION_INPUTS,
          heuristics=("config_path_patterns.csv", "not_configuration.csv"), stage="collection"),
    # The check reads the cleanup's deletion log against the two trees and the mechanism CSVs.
    # Like the step above, it writes its findings and cannot stop the run. What it finds
    # concerns data that the run has already written, and the archive and the release that
    # follow publish nothing on their own. `output/deletions.csv` records its findings in the
    # handover, and the command run by hand exits 2 on them. docs/checks/deletions.md gives the
    # reason.
    #
    # It opens data/validated/ but does not consume `collected_file_cleanup`, the edge that the
    # check for modified inputs reads. The check distinguishes the directories that the file
    # history step removed from a deletion that the cleanup did not record by the validated
    # list, which the file history step leaves unchanged. Running this step alone again after
    # the file history step is therefore correct and must not rebuild the tree.
    Step("deleted_files_report",         "python3",   ["-m", "agentcontext.checks.deletions",
                                             "--report-only",
                                             "--out", "output/deletions.csv"],
          produces=("output/deletions.csv",),
          consumes=('detection', 'file_history'), completion=COMPLETION_INPUTS,
          heuristics=("config_path_patterns.csv", "not_configuration.csv"), stage="collection"),
    # The `imports` section of the snapshot counts output/imports.csv, and a new imports.csv
    # makes the step write the snapshot again.
    Step("collection_summary",          "python3", ["-m", "agentcontext.checks.snapshot", "write"],
          produces=("output/snapshot.json",),
          consumes=('repository_cloning', 'detection', 'file_history', 'import_detection'),
          completion=COMPLETION_INPUTS,
          inputs=("data/collected/repos.csv", "data/validated/repos.csv",
                  "output/repos.csv", *MECHANISM_OUTPUTS, PLANNING_OUTPUT, "output/imports.csv",
                  "output/commit-reclassification.json|output/repos.csv"), stage="collection"),
    # --dry-run writes no archive, and `--step-arg collected_files_archive:"--dry-run"` fails the
    # output check. This is intended: a dry run did not produce the dataset, and the step is not
    # done.
    #
    # The step reads data/validated/ and nothing under output/, and it does not consume
    # `detection`. Otherwise every `--from detection` and every edit to the detection step or the
    # path heuristics would copy the whole validated tree and compress the sample with 7zz again,
    # the case that the docstring of `invalidated_by` names. Nor does it consume file_history,
    # which only removes directories from data/validated/. That relation is `mutates`, which the
    # check for modified inputs reads separately.
    Step('collected_files_archive',           "python3",   ["-m", "agentcontext.steps.collected_files_archive"],
          produces=("release/repos_data.7z",), consumes=('collected_file_cleanup',), stage="publication"),
    # The publication. It reads output/ and the outputs of the two steps above it where those
    # steps write them, and it writes release/, so that nothing has to be copied by hand into the
    # directory that is also the published copy. `output_cross_check` is a consumed step rather
    # than merely an earlier one, because that check refuses output CSVs that disagree with each
    # other, and the release must not publish them.
    Step("release",           "python3",   ["-m", "agentcontext.steps.release"],
          produces=RELEASE_OUTPUTS,
          # The release also publishes the reports of the two checks that it consumes. The cloning
          # step and the commit reclassification, which write the other records that it publishes,
          # run the detection step again.
          consumes=('detection', 'file_history', 'import_detection', "output_cross_check",
                    "unmatched_paths_report", "deleted_files_report", "collection_summary",
                    'collected_files_archive'),
          completion=COMPLETION_INPUTS,
          # The release's repos.csv has a row per repository in the sampling frame with its
          # labels, and sampling.json records the sampling frame. A new sampling frame or new
          # labels therefore publish again even when the list of the sample, and with it every
          # collection step, stayed the same. The README states by reason from
          # repos_excluded.csv why the repositories without a label have none, and from the
          # download's report whether the GHS download fell back to an earlier retrieval. A
          # fallback rewrites that report without a new sampling frame. The release builds
          # unmatched-attributions.csv from the full list of the unmatched attributions, which the
          # cloning step and the commit reclassification rewrite.
          inputs=("../sampling/ghs_filtered.json", "../sampling/ghs_retrieval_report.json",
                  "../classification/repos_annotated.csv",
                  "../classification/repos_annotated.json",
                  "../classification/repos_excluded.csv",
                  "output/all_ai_commits-unmatched.csv"),
          # The release also contains the two commit heuristics files, the two import
          # heuristics files, the heuristics of the planning documents, the other negatives and
          # the record of the documentation watch, and an edit to any of them publishes again.
          heuristics=("config_path_patterns.csv", "ai_commit_patterns.csv",
                      "other_ai_commit_patterns.csv", "import_patterns.csv", "not_imports.csv",
                      "planning_document_patterns.csv", "not_configuration.csv",
                      "not_ai_commits.csv", "not_planning_documents.csv", "doc_hashes.json",
                      "doc_quotes.json"),
          stage="publication"),
]


def imported_tree_input(step: Step) -> list:
    """What records `import_detection` besides its code and its heuristics: a checksum manifest
    of the imported tree (`manifest_lines`), as a label and a digest. The digest is None where
    the tree is absent. The step reads the files of the tree, which no list of paths can name."""
    tree = data_dir(step) / "data" / "imported"
    try:
        if entry_kind(tree) == ABSENT:
            return [("the imported tree", None)]
        lines = manifest_lines(tree, "data/imported")
    except OSError as e:
        raise UnreadableOutput(step.key, "the imported tree", e) from e
    digest = hashlib.sha256()
    for line in lines:
        encoded = line.encode("utf-8", "surrogateescape")
        digest.update(b"%d:" % len(encoded))
        digest.update(encoded)
    return [("the imported tree", digest.hexdigest())]


def classified_by_input(step: Step) -> list:
    """What records `commit_reclassification` besides its code and the two commit heuristics
    files: `commits.CLASSIFIED_BY`, the digest that the step writes into every per-repository
    commit record and into `output/commit-reclassification.json`, as a label and the digest.

    The digest hashes the columns of the two commit heuristics files that the classification
    reads (`commits.CLASSIFICATION_COLUMNS`) and the code fingerprint of `commits.py` and
    `heuristics.py`, which, like the code fingerprint of the step, leaves out comments,
    docstrings and log calls. `snapshot write` copies the digest from the step's report, and
    the release refuses a snapshot with a digest other than its own. Recorded by its code and
    the heuristics files alone, the step would stay complete after an image that changes the
    digest in another way, while the release, which reads the image's digest, would refuse at
    every Start."""
    return [("the classified_by digest", commits.CLASSIFIED_BY)]


# The commit heuristics of this checkout, applied to the commit logs that the cloning step kept.
# The step is recorded by the two commit heuristics files and by the classified_by digest
# (`classified_by_input`). An edit to either file, or to the code of `commits.py` or
# `heuristics.py`, runs it again over the commit logs, with the steps after it, without cloning
# anything. An edit to a column that the classification does not read keeps the digest, and the
# step then keeps the shards of every record with validated evidence. Right after a collection,
# every shard is already classified under the current heuristics, and the step only assembles
# the commit CSVs. It runs before the detection step, which restricts the commit CSV to the
# published repositories, and `steps_for` puts it there when the two checks below run.
COMMIT_DEPENDENTS = ('collected_file_cleanup', 'detection', 'unmatched_commits_report')
COMMIT_HEURISTICS_STEP = Step(
    'commit_reclassification', VENV_PYTHON, ["-m", "agentcontext.steps.commit_reclassification"],
    produces=("output/all_ai_commits.csv", "output/other_ai_commits.csv"),
    consumes=('repository_cloning',), completion=COMPLETION_INPUTS,
    heuristics=("ai_commit_patterns.csv", "other_ai_commit_patterns.csv"),
    inputs_of=classified_by_input, stage="collection")


# The two checks over the commit CSVs of the cloning step. The cloning step writes the commit CSV,
# `other_ai_commits.csv`, the unmatched attributions, the mentions and `ai_commits-repos.csv` (the
# section "Output" of docs/steps/repository_cloning.md), and the `produces` of `repository_cloning`
# names none of the five. The step is recorded by a checksum manifest of what it declares, and the
# detection step rewrites the commit CSV in place. --mark-complete can also record the step from a
# restored tree that has the sample but not the CSV. The record therefore does not show whether the
# commit histories were read. `ai_commits-repos.csv` shows it, with one row per repository. The
# checks are opt-in, because a regeneration over an earlier collection has no output of the cloning
# step for either check to read. The repository tracks the commit CSV as `all_ai_commits.csv.7z`,
# and of the unmatched attributions only the lines that name a bot or a tool, as
# `all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z`, with the columns that `unmatched reduce` keeps, not
# the CSV that the check reads.
COMMIT_RECLASSIFICATION_STEPS = [
    # The validation script derives its output name from its input, `<stem>-validated.csv`, and
    # `produces` names the files that this naming gives for the inputs that the command line sets
    # here. The check reads the CSV after the detection step has restricted it to the published
    # repositories, and therefore consumes both steps.
    Step('commit_attribution_validation', "python3", [
        "-m", "agentcontext.checks.commits",
        "-i", "output/all_ai_commits.csv",
        "--other-input", "output/other_ai_commits.csv",
    ], produces=("output/all_ai_commits-validated.csv", "output/other_ai_commits-validated.csv"),
       consumes=('repository_cloning', 'detection'), completion=COMPLETION_INPUTS,
       heuristics=("ai_commit_patterns.csv", "other_ai_commit_patterns.csv"), stage="collection"),
    # The check of the recall of the commit heuristics. The cloning step records every
    # attribution in a commit that it did not match, and this check ranks the attributions that
    # no heuristic explains. Most commit heuristics are grounded in no vendor page, and for them
    # this check is the only way to find what needs updating.
    #
    # It has no threshold, for the reason given for the paths check, which applies even more
    # here. In a full collection, the attributions that no heuristic explains are mostly the
    # human co-authors of AI commits and the CI bots that no negative example names, such as
    # `co-authored-by: root`. No run could stay under any threshold, and a threshold here would
    # stop the run before the archive and the release, although the ranked list only needs a
    # person to read it. A person reads the ranked list and `output/all_ai_commits-unmatched.csv`
    # in the handover.
    #
    # The step writes the ranked list to `output/unmatched-commits.csv` and logs only the counts,
    # because the whole list is too long for the container's log.
    Step('unmatched_commits_report', "python3", [
        "-m", "agentcontext.checks.unmatched", "commits",
        "--unmatched-csv", "output/all_ai_commits-unmatched.csv",
        "--out", "output/unmatched-commits.csv",
    ], produces=("output/unmatched-commits.csv",),
       consumes=('repository_cloning',), completion=COMPLETION_INPUTS,
       heuristics=("ai_commit_patterns.csv", "not_ai_commits.csv"), stage="collection"),
]

# The steps that clone from github.com. They need git, a GitHub access token and a server that
# accepts it. The pre-checks verify these only when one of the steps is pending.
CLONING_STEPS = frozenset({'repository_cloning', 'file_history'})

# The external binaries that each step needs. pre_checks() checks them only for a step that
# is about to run, and a missing 7zz, which the archive and the release need, does not block a run
# that never reaches those steps.
BINARY_REQUIREMENTS = {
    "git":     CLONING_STEPS,
    "rsync":   {'repository_cloning'},
    SEVEN_ZIP: {'collected_files_archive', "release"},
}


GITHUB_RATE_LIMIT_URL = "https://api.github.com/rate_limit"
# GROUNDING: one small request. Thirty seconds leaves a wide margin for a slow connection, and
# over a connection on which this request takes longer, the run could not clone either.
GITHUB_TIMEOUT_SECONDS = 30
# The HTTP status with which GitHub rejects the credentials of a request, the response
# `Bad credentials` to a revoked, expired or mistyped GitHub access token. It is the one response
# that shows that the GitHub access token cannot be used. A 403 or a 429 is a rate limit or a
# refusal of one resource, and a server error says nothing about the GitHub access token.
GITHUB_REJECTED_STATUS = 401
# The pauses in seconds before the pre-checks repeat a check of a GitHub access token that
# returned no verdict: a request that could not reach GitHub, a server error, a rate limit or a
# response in another format. GROUNDING: a policy. A failure of the network or of GitHub's API
# that passes within minutes must not refuse an unattended Start, because the refusal would
# delay the month's run until the next Start. The cloning step's connectivity probe finds a
# failure that lasts longer.
GITHUB_CHECK_WAITS = (30, 60, 120)
# The waits in seconds before the pre-checks verify every GitHub access token again once
# GitHub has rejected each of them with GITHUB_REJECTED_STATUS. GitHub can reject valid GitHub
# access tokens for a while, and a refused Start loses an unattended month's run. The waits
# therefore grow from minutes to hours, like the waits of the steps that query GitHub. A stop ends
# the wait (`_wait_unless_stopped`). GROUNDING: the policy of `RECHECK_WAITS` of
# ../2_classification/readmes.py and of `ROUND_WAITS` of ../1_sampling/2_query_github.py.
GITHUB_REJECTED_WAITS = (5 * 60, 15 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60)


def github_rate_limit(token: str) -> dict:
    """One authenticated request: GitHub's core rate limit as `{limit, remaining, reset}`.

    With it, the pre-checks show that GitHub accepts the GitHub access token and can be
    reached before a step clones anything with it. Raises `OSError` when the server cannot be
    reached or rejects the GitHub access token, and `ValueError` when the response is not in the
    documented format, also when its body is JSON without an object at `resources.core`. """
    request = urllib.request.Request(
        GITHUB_RATE_LIMIT_URL,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "ai-config-pipeline"})
    with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT_SECONDS) as response:
        body = json.load(response)
    resources = body.get("resources") if isinstance(body, dict) else None
    core = resources.get("core") if isinstance(resources, dict) else None
    if not isinstance(core, dict):
        raise ValueError("The response of /rate_limit has no object at resources.core")
    return core


def github_verdict(token: str, index: int, total: int) -> tuple:
    """Whether GitHub accepts the GitHub access token `token`, number `index` of `total`:
    `("accepted", core)` with GitHub's core rate limit, `("rejected", detail)` for
    GITHUB_REJECTED_STATUS, or `("undecided", detail)` when no request returned a verdict.
    The function repeats the check after each pause of GITHUB_CHECK_WAITS, and a stop ends the
    pauses.

    `detail` names the HTTP status or the type of the error, never the GitHub access token or
    the body of the response. Only a rejection shows that the GitHub access token cannot be
    used. A failed request, a server error or a rate limit shows nothing about it, and counted
    as a rejection, a short failure of the network would refuse the Start of a monthly run.
    """
    detail = ""
    for wait in (None, *GITHUB_CHECK_WAITS):
        if wait is not None:
            if _STOPPING:
                # A stop came during the request, and the request's failure says nothing about
                # the GitHub access token. A pause announced now would end at once.
                break
            logs.info("pre_checks", f"GitHub gave no verdict on GitHub access token "
                                    f"{index} of {total:,} ({detail}). Checking again "
                                    f"in {wait:g} s.")
            if _wait_unless_stopped(wait):
                break
        try:
            return "accepted", github_rate_limit(token)
        except urllib.error.HTTPError as error:
            if error.code == GITHUB_REJECTED_STATUS:
                return "rejected", f"HTTP {error.code}"
            detail = f"HTTP {error.code}"
        except (OSError, ValueError, http.client.HTTPException) as error:
            detail = type(error).__name__
    return "undecided", detail


def _wait_unless_stopped(seconds: float) -> bool:
    """Wait `seconds`, or until a stop arrives, and return whether one arrived. The handler of a
    stop returns, and `time.sleep` continues after it. A single sleep of hours would hold a Stop
    until its grace period ended in SIGKILL."""
    end = time.monotonic() + seconds
    while not _STOPPING and (left := end - time.monotonic()) > 0:
        time.sleep(min(left, 1.0))
    return bool(_STOPPING)


def _choose_github_token(tokens: list) -> bool:
    """Check the GitHub access tokens `tokens` in order, set GITHUB_TOKEN to the first that GitHub
    does not reject (`github_verdict`), and return whether there was one. The messages name the
    HTTP status of an API error, never a credential or a response body.

    A stop ends the check before the next GitHub access token, because the run ends before its
    first step whatever the pre-checks find."""
    for index, candidate in enumerate(tokens, 1):
        if _STOPPING:
            return False
        verdict, detail = github_verdict(candidate, index, len(tokens))
        if _STOPPING:
            # A stop during the check ended its pauses. The verdict then says nothing about the
            # GitHub access token, and a message about it would mislead.
            return False
        if verdict == "rejected":
            logs.warning("pre_checks", f"GitHub access token {index} of "
                                       f"{len(tokens):,} failed validation ({detail}).")
            continue
        os.environ["GITHUB_TOKEN"] = candidate
        if verdict == "accepted":
            logs.info("pre_checks", f"GitHub access token {index} of {len(tokens):,} "
                                    f"passed validation, with "
                                    f"{_count(detail.get('remaining'))} of "
                                    f"{_count(detail.get('limit'))} core requests "
                                    f"remaining. Repository cloning and the file history "
                                    f"step use only this GitHub access token.")
        else:
            logs.warning("pre_checks", f"GitHub gave no verdict on GitHub access token "
                                       f"{index} of {len(tokens):,} after "
                                       f"{len(GITHUB_CHECK_WAITS) + 1:,} attempts "
                                       f"({detail}). Using it anyway as the only GitHub "
                                       f"access token of repository cloning and the file "
                                       f"history step.")
        return True
    return False


# The steps that send requests with a model to OpenAI's or OpenRouter's Responses API, with the
# API key of the model's service. When one of them is pending, the pre-checks send OpenAI one
# minimal request with the same key and model, or read the OpenRouter API key's credit for an
# OpenRouter model, whose id names its author, so that a key that the service refuses stops the
# run before the sampling stage rather than after it.
LABELING_STEPS = frozenset({'repository_labeling'})
# GET /api/v1/key, which spends nothing and returns the key's limits, "Remaining credits for
# the key, or null if unlimited" (openrouter.ai/docs/api-reference/limits).
OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"
# The statuses with which OpenRouter refuses every request with the key.
OPENROUTER_REFUSED_STATUSES = {401: "OpenRouter rejects the OpenRouter API key",
                               403: "OpenRouter denies the OpenRouter API key access"}
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
# GROUNDING: one small request, as for GitHub above.
OPENAI_TIMEOUT_SECONDS = 30
# The smallest output that the Responses API accepts in `max_output_tokens`, so that the request
# costs as little as a request can.
OPENAI_MAX_OUTPUT_TOKENS = 16
# The errors of the Responses API that refuse every request of the run, by their HTTP status or
# by their `code` or `type`, with what each means. Any other error, such as a server error, a
# rate limit or a timeout, can pass, and the pre-checks log it as a warning.
OPENAI_REFUSED_STATUSES = {401: "OpenAI rejects the OpenAI API key",
                           403: "OpenAI denies the OpenAI API key's project access"}
OPENAI_REFUSED_CODES = {
    "model_not_found": "OpenAI does not offer the model to the OpenAI API key's project",
}
# The errors of a project that has spent its quota or reached its billing limit. A top-up ends
# them, and the labeling waits for the credit rather than failing its repositories (`CreditPause`
# of `2_classification/1_classify_repos.py`). The pre-checks therefore warn rather than stop,
# because nothing starts a stopped monthly run again.
OPENAI_CREDIT_CODES = {
    "insufficient_quota": "The OpenAI API key's project has used up its quota or its spend limit",
    "billing_hard_limit_reached": "The OpenAI API key's organization has reached its billing "
                                  "limit",
}


def openai_response(key: str, model: str) -> None:
    """One minimal request to OpenAI's Responses API with `key` and `model`, stored nowhere.

    With it, the pre-checks show that OpenAI accepts the key, offers the model to its
    project and bills the request. Raises `urllib.error.HTTPError` for an error of the API, and
    `OSError` or `http.client.HTTPException` when the server cannot be reached or does not respond
    in time. """
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL, method="POST",
        data=json.dumps({"model": model, "input": "Reply with OK.", "store": False,
                         "max_output_tokens": OPENAI_MAX_OUTPUT_TOKENS}).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "ai-config-pipeline"})
    with urllib.request.urlopen(request, timeout=OPENAI_TIMEOUT_SECONDS) as response:
        response.read()


def openrouter_key(key: str) -> dict:
    """The data of the OpenRouter API key `key` from GET /api/v1/key, which spends nothing.
    Raises `urllib.error.HTTPError` for an error of the API, `OSError` or
    `http.client.HTTPException` when the server cannot be reached or does not respond in time, and
    ValueError for a body without an object at `data`."""
    request = urllib.request.Request(OPENROUTER_KEY_URL, headers={
        "Authorization": f"Bearer {key}", "User-Agent": "ai-config-pipeline"})
    with urllib.request.urlopen(request, timeout=OPENAI_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read() or b"{}")
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise ValueError("the response of GET /api/v1/key has no object at data")
    return data


def openrouter_remaining(data: dict) -> float | None:
    """The least of the key's `limit_remaining` and the `remaining_usd` of its
    `effective_budget`, or None when neither limits the key, as the labeling reads the credit
    (`KeyCredit` of `2_classification/endpoints.py`)."""
    budget = data.get("effective_budget")
    limits = [data.get("limit_remaining"),
              budget.get("remaining_usd") if isinstance(budget, dict) else None]
    found = [float(value) for value in limits
             if isinstance(value, (int, float)) and not isinstance(value, bool)]
    return min(found) if found else None


def openrouter_accepts(step_key: str, model: str, credentials_path=None) -> bool:
    """Read the credit of the first OpenRouter API key for the labeling step `step_key` with the
    OpenRouter model `model`, and return False when OpenRouter refuses the key. No credit left is
    a warning, because the labeling waits for credit, and an error that can pass is a warning,
    because it shows nothing about the key. Without a key, the check of the environment logs the
    key that is missing. Further keys are a warning that names how many of them are unused,
    because the labeling, like this check, uses only the first (`endpoints.api_key` of
    `2_classification/endpoints.py`)."""
    keys = credentials.openrouter_keys(credentials_path, os.environ)
    if not keys:
        return True
    if len(keys) > 1:
        unused = len(keys) - 1
        logs.warning("pre_checks", f"{unused:,} of the {len(keys):,} configured OpenRouter API "
                                   f"keys {'is' if unused == 1 else 'are'} unused. The pre-check "
                                   f"and {step_key} use only the first.")
    try:
        data = openrouter_key(keys[0].value)
    except urllib.error.HTTPError as error:
        refusal = OPENROUTER_REFUSED_STATUSES.get(error.code, "")
        # The message names the HTTP status and the model, never the key or the body.
        if refusal:
            logs.error("pre_checks", f"{refusal} (HTTP {error.code}, model {model}). Every "
                                     f"request of {step_key} fails the same way. Correct the "
                                     f"key before the next Start.")
            return False
        logs.warning("pre_checks", f"OpenRouter returned HTTP {error.code} to the request for "
                                   f"the OpenRouter API key's credit (required by {step_key} with "
                                   f"model {model}). Continuing, because the error can pass.")
        return True
    except (OSError, http.client.HTTPException, ValueError) as error:
        logs.warning("pre_checks", f"OpenRouter could not be reached or returned no data of the "
                                   f"key ({type(error).__name__}). Continuing without a check of "
                                   f"the OpenRouter API key (required by {step_key}).")
        return True
    remaining = openrouter_remaining(data)
    if remaining is not None and remaining <= 0:
        logs.warning("pre_checks", f"The OpenRouter API key has no credit left (model {model}). "
                                   f"{step_key} waits for credit when it reaches OpenRouter. To "
                                   f"continue then, add credits at "
                                   f"https://openrouter.ai/settings/credits, or raise the credit "
                                   f"limit of the OpenRouter API key.")
    else:
        logs.info("pre_checks", f"OpenRouter accepted the {'first ' if len(keys) > 1 else ''}"
                                f"OpenRouter API key (required by {step_key} with model "
                                f"{model}).")
    return True


def openai_refusal(error: urllib.error.HTTPError) -> tuple[str, str]:
    """Why OpenAI would refuse every request of the run, from the error of one, and why it
    refuses the requests until a top-up (OPENAI_CREDIT_CODES). Each is `""` when it does not
    apply, and both are `""` for an error that can pass, such as a server error or a rate limit."""
    try:
        body = json.loads(error.read() or b"{}")
    except (OSError, ValueError, http.client.HTTPException):
        # A connection that ends before the body's full length raises IncompleteRead, which says
        # nothing about the key either.
        body = {}
    details = body.get("error") if isinstance(body, dict) else None
    details = details if isinstance(details, dict) else {}
    for value in (details.get("code"), details.get("type")):
        if value in OPENAI_REFUSED_CODES:
            return OPENAI_REFUSED_CODES[value], ""
        if value in OPENAI_CREDIT_CODES:
            return "", OPENAI_CREDIT_CODES[value]
    return OPENAI_REFUSED_STATUSES.get(error.code, ""), ""


def labeling_model(step: Step) -> str:
    """The model that a labeling step requests: the `--model` of its arguments, or the
    `DEFAULT_MODEL` that its script assigns, or `""` when neither names one.

    Read from the script's source rather than imported, because the script imports packages
    that the runner's interpreter does not have. The last `--model` counts, as for the
    script's parser, and a `--step-arg` therefore overrides the list of steps.
    """
    model = ""
    for index, arg in enumerate(step.args):
        if arg == "--model" and index + 1 < len(step.args):
            model = step.args[index + 1]
        elif arg.startswith("--model="):
            model = arg.partition("=")[2]
    if model or step_module(step) or not step.args:
        return model
    script = code_dir(step) / step.args[0]
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return ""
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
                and any(isinstance(target, ast.Name) and target.id == "DEFAULT_MODEL"
                        for target in node.targets)):
            return node.value.value
    return ""


# Libraries with versions that can change what a step produces, recorded with every run. An
# unstable sort leaves the order of ties to numpy's sort implementation, and the analysis
# scripts' statistics change in their last digits with the versions of these libraries.
RECORDED_PACKAGES = ("pandas", "numpy", "scipy")
_STEP_PACKAGES: "dict | None" = None


def step_package_versions() -> dict:
    """Versions of `RECORDED_PACKAGES` as the step interpreter reports them, once per run.

    Read from distribution metadata rather than by importing the packages. A package that is
    not installed is recorded as `null` rather than left out, and the record always names
    every package that the function checked.
    """
    global _STEP_PACKAGES
    if _STEP_PACKAGES is not None:
        return _STEP_PACKAGES
    probe = ("import json, sys, importlib.metadata as md\n"
             "out = {}\n"
             "for name in sys.argv[1:]:\n"
             "    try:\n"
             "        out[name] = md.version(name)\n"
             "    except Exception:\n"
             "        out[name] = None\n"
             "print(json.dumps(out))")
    unknown = {name: None for name in RECORDED_PACKAGES}
    try:
        check = subprocess.run([VENV_PYTHON, "-c", probe, *RECORDED_PACKAGES],
                               capture_output=True, text=True, timeout=60)
        _STEP_PACKAGES = json.loads(check.stdout) if check.returncode == 0 else unknown
    except (OSError, ValueError, subprocess.TimeoutExpired):
        _STEP_PACKAGES = unknown
    return _STEP_PACKAGES


# The file at the image's root that records the commit that the image was built from, because
# the image has no `.git`: `commit <sha>`, `built <date>` and `python <version>`, one per line.
# `deploy/Dockerfile` writes it and `deploy/entrypoint.py` logs it.
BUILD_INFO = "BUILD_INFO"


def _git_info(root: Path = REPO_ROOT) -> dict:
    """Best-effort pipeline provenance: the commit that this code is at, and
    whether the working tree has uncommitted changes. Never raises.

    Git runs at REPO_ROOT, where the code is, and not at STAGE_DIR, because the container
    points STAGE_DIR at the host's bind mount and the subsample run points it at another
    checkout. At STAGE_DIR, the record would contain null or another repository's HEAD.

    Where git returns no commit, as in the image, which has no `.git`, the commit is read
    from the image's `BUILD_INFO`. `deploy/Dockerfile` writes that file from the commit that
    `deploy/build_image.sh` passes to it, as `<sha>`, or `<sha>-dirty` when the tree that it
    built from had uncommitted changes.
    """
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                             capture_output=True, text=True, timeout=5)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                capture_output=True, text=True, timeout=5)
        if sha.returncode == 0:
            return {
                "git_sha": sha.stdout.strip(),
                "git_dirty": bool(status.stdout.strip()) if status.returncode == 0 else None,
            }
    except Exception:
        pass
    try:
        for line in (root / BUILD_INFO).read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            built = re.fullmatch(r"([0-9a-f]{7,40})(-dirty)?", value.strip())
            if key == "commit" and built:
                return {"git_sha": built.group(1), "git_dirty": bool(built.group(2))}
    except (OSError, UnicodeDecodeError):
        pass
    return {"git_sha": None, "git_dirty": None}


def write_execution_state(state: dict, step_results: list, outcome: str,
               started_at: float, argv: list, running_step: str | None = None) -> None:
    """Update the provenance part of the pipeline execution state and write the file.

    The file serves two purposes. A resume reads `completed`, and everything that this
    function sets is the evidence of a run: the commit that the pipeline was at, the exact
    command, the status and duration of each step, and the machine. One file written
    atomically cannot contradict itself after a kill between two writes. `running_step` names
    the step that is about to start (RUNNING_STEP), with the start that names it
    (RUNNING_STEP_STARTED_AT) and that start's step results before the step
    (RUNNING_STEP_RESULTS). A write without `running_step` removes the three keys once
    `step_results` record a step that ran.

    The function removes RUNNING_STEP_STATUS, because it writes between steps, where no step can
    wait for anything. A runner killed during a wait leaves the key, and without the
    removal the next start would carry the wait into every later state and into the handover.

    A start with step results that record no step that ran leaves the name as the start
    before it wrote it. Such a start was refused or stopped, or a check stopped it before its
    first step with the outcome `failed`. A kill inside the first step of a start leaves the
    name as the only record of that step, and from it the container's entrypoint tells that
    the step after the release had not finished (`release_step_completed`). If a later start
    removed the name, the data root would be handed over as complete while it held part of a
    collection. A start that ran no step (NO_STEP_OUTCOMES) also leaves the step results
    (`steps`), as it leaves the outcome and the step results of the last run.
    """
    # Present even when nothing has completed, so that the file always has the same keys for a
    # program that reads it as evidence of a run.
    state.setdefault("completed", {})
    state.pop(RUNNING_STEP_STATUS, None)
    ran_nothing = outcome in NO_STEP_OUTCOMES
    started = datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
    ran_a_step = any(result.get("status") != "skipped" for result in step_results)
    if running_step is not None:
        state[RUNNING_STEP] = running_step
        state[RUNNING_STEP_STARTED_AT] = started
        state[RUNNING_STEP_RESULTS] = list(step_results)
    elif ran_a_step:
        for key in (RUNNING_STEP, RUNNING_STEP_STARTED_AT, RUNNING_STEP_RESULTS):
            state.pop(key, None)
    finished_at = datetime.now(timezone.utc).isoformat()
    state.update({
        **_git_info(),
        # shlex.join rather than " ".join, because the state file records the exact command.
        # " ".join leaves an argument that contains a space unquoted, and the command could not
        # be run again: `--step-arg repository_cloning:--repo-csv repos_subset.csv` would be
        # parsed again as two arguments.
        "command": shlex.join([sys.executable] + argv),
        "started_at": started,
        "finished_at": finished_at,
        "duration_seconds": round(time.time() - started_at, 1),
        "outcome": outcome,
        **({LAST_RUN_OUTCOME: outcome} if outcome not in NO_STEP_OUTCOMES else {}),
        **({LAST_STEPS_FINISHED_AT: finished_at, LAST_RUN_STEPS: list(step_results)}
           if ran_a_step else {}),
        "hostname": platform.node(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "step_interpreter": VENV_PYTHON,
        "step_packages": step_package_versions(),
        **({} if ran_nothing and "steps" in state else {"steps": step_results}),
    })
    write_state(state)


# The step subprocess while one is running, and the signal that stops this
# run (0 when none has arrived). Module-level, because a signal handler takes
# no arguments of its own but has to reach both.
_CHILD: "subprocess.Popen | None" = None
_STOPPING = 0
# Whether the stop reached the step that was running. A stop that arrives after the step has
# exited, and before `run_step` returns, did not stop the step.
_FORWARDED = False


def forward_stop(signum, _stack_frame) -> None:
    """Pass a stop on to the step that is running, and end the run after it.

    A plain `except KeyboardInterrupt` covers only Ctrl+C at a terminal, which
    the kernel delivers to the whole foreground process group, and the step
    gets it directly. `kill`, a systemd stop and a `docker stop` signal this
    process alone. Without the forward, the runner would exit and leave the
    step orphaned and still cloning.

    Forwarding alone is not enough. The handler returns, the signaled step
    exits, and the loop would start the next step. A stop therefore has to
    stop the pipeline and not just the step. A second signal that arrives
    while no step is running means that the operator insists, and the runner
    exits at once.

    For Ctrl+C, the forwarded signal duplicates what the terminal already
    delivered. It costs nothing, because the step is ending either way, and it
    makes `kill -INT` on the runner alone behave the same as Ctrl+C.
    """
    global _STOPPING, _FORWARDED
    first, _STOPPING = not _STOPPING, signum
    if _CHILD is not None and _CHILD.poll() is None:
        logs.info("runner", f"Received signal {signum}. Forwarded it to the running step. "
                            f"Starting no further step.")
        _CHILD.send_signal(signum)
        _FORWARDED = True
        return
    if first:
        logs.info("runner", f"Received signal {signum} while no step was running. Stopping "
                            f"before the next step. Send the signal again to exit at once.")
        return
    raise SystemExit(128 + signum)


def script_fingerprint(step: Step) -> str:
    """A hash of the code that the step's script runs, or `""` when it cannot be read.

    Recorded beside each completed step, so that a resume can check whether the code that
    produced the result is still the code on disk. The hash is per step rather than one hash
    over the whole pipeline: an edit to `agentcontext/steps/detection.py` runs the detection
    step and what is computed from it again, and clones no repository again.

    The hash is `code_digest`'s, and an edit to a comment, a docstring or a log message runs
    nothing again. A step writes none of these into its outputs. With a hash of the bytes, an
    edit to a comment of `agentcontext/logs.py` would run every step that imports the module
    again, from the collected file cleanup to the analysis.

    The step's *arguments* are deliberately not hashed. `--step-arg` appends to them for one
    run, and a worker count does not change what a step produced. Hashing argv would
    invalidate a completed collection for `-w 10`. The cost is that an edit to a flag that this
    file's list of steps sets, such as the `-i` of the commit attribution validation, does not
    change the fingerprint.
    """
    module = step_module(step)
    if module:
        return module_fingerprint(module)
    script = code_dir(step) / step.args[0] if step.args else None
    if script is None or not script.is_file():
        return ""
    if not step.package_code:
        return code_digest([script])
    # The modules of the package that the script and its code inputs import, which the code
    # inputs do not name. Such a module can change what the script writes while the step's own
    # inputs stay the same.
    sources = [script, *(code_dir(step) / rel for rel in step.code_inputs if rel.endswith(".py"))]
    return code_digest([script, *fingerprints.imports_closure(sources, PACKAGE_DIR)])


def step_module(step: Step) -> str:
    """The package module that a step runs, or `""` for a step that runs a script."""
    return step.args[1] if step.args[:1] == ["-m"] and len(step.args) > 1 else ""


def module_closure(name: str) -> list:
    """The files of a package module and of every package module that it imports
    (`fingerprints.module_closure`), in the package that contains this runner."""
    return fingerprints.module_closure(name, PACKAGE_DIR)


def module_fingerprint(name: str) -> str:
    """One hash over the code of the module closure's files, in path order, or `""` when it is
    empty."""
    files = module_closure(name)
    if not files:
        return ""
    return code_digest(files)


def code_digest(files: list) -> str:
    """The hash of the code that Python files run, `fingerprints.code_fingerprint`, which the
    cloning step records for the collector as well. A comment, a docstring, a log message or
    the layout of the code leaves it unchanged. A file that is not Python, or that does not
    parse, is hashed by its bytes, because it cannot be reduced to the code that it runs, and
    every edit to it then changes the hash."""
    if all(Path(path).suffix == ".py" for path in files):
        try:
            return fingerprints.code_fingerprint(files)
        except (SyntaxError, ValueError):
            pass
    digest = hashlib.sha256()
    for path in files:
        digest.update(Path(path).name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(Path(path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _sha256_of(path: Path) -> str:
    """The SHA-256 of a file's contents, read in blocks so that a large file need not fit in
    memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# Errnos that mean that nothing is at a path, as opposed to an entry that is there
# and cannot be read. Every other errno, such as EACCES, EIO, ESTALE, ENOTCONN and
# ETIMEDOUT, means a mount that is present and not working.
_ABSENT_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})

ABSENT, LINK, DIRECTORY, FILE, OTHER = "absent", "link", "dir", "file", "other"


def entry_kind(path: Path, follow: bool = False) -> str:
    """What is at `path`: ABSENT, LINK, DIRECTORY, FILE or OTHER.

    `os.lstat` rather than `Path.exists`, `is_dir` and `is_symlink`, because
    the behavior of those functions differs between Python versions. Up to 3.13
    they re-raised every errno except the four above, and an output on a
    stalled mount raised an error rather than reading as absent. Since 3.14
    they return False for any path that they cannot stat, which turns that
    mount into "nothing is there". Neither result is wrong for a predicate, but
    neither distinguishes an output that was never written from an output that
    cannot be read, and every caller below depends on that distinction. Taking
    an unreadable output for an absent output records `repository_cloning` as
    not done and runs the collector again over every repository in its input
    list.

    `follow` resolves a link first, for the callers that need what a link
    points at rather than the link itself.
    """
    try:
        st = os.stat(path) if follow else os.lstat(path)
    except OSError as e:
        if e.errno in _ABSENT_ERRNOS:
            return ABSENT
        raise
    if stat.S_ISLNK(st.st_mode):
        return LINK
    if stat.S_ISDIR(st.st_mode):
        return DIRECTORY
    if stat.S_ISREG(st.st_mode):
        return FILE
    return OTHER


def is_discarded(path: Path) -> bool:
    """Whether the manifest and the entry count leave out the entry at `path`, because the
    collected file cleanup deletes it: a file, or a link to one, with a name in
    DISCARDED_NAMES, or a directory with a name in DISCARDED_DIRECTORIES.

    Each name is left out only in the form in which the cleanup deletes it. `is_valid_file`
    rejects `.claude/.DS_Store` but accepts `.claude/.DS_Store/settings.json`, and the cleanup
    descends into such a directory rather than removing it. Those files reach
    `data/validated/`, are classified and reach a published row, and skipping by name alone
    would hide them. The test of a discarded file name follows links: an entry with that name
    that points at a directory is kept and described as a link. The test of a discarded
    directory name does not follow links, and a link with that name is kept too. Leaving out an
    entry that the pipeline would have kept hides a change in the sample.
    """
    if path.name in DISCARDED_NAMES:
        return entry_kind(path, follow=True) != DIRECTORY
    if path.name in DISCARDED_DIRECTORIES:
        return entry_kind(path) == DIRECTORY
    return False


def manifest_lines(path: Path, rel: str) -> list:
    """One line per entry at `path`, describing what is there.

    The entries are sorted at every level, and the result depends on the tree,
    not on the order in which the filesystem returns names. Each kind of entry
    has its own kind of line, so that a change to any single entry changes the
    manifest:

      ``d <path>``            a directory, which shows even when it has been emptied
      ``f <path> <sha256>``   a regular file, by content
      ``l <path> <target>``   a symlink, by what it points at
      ``? <path>``            anything else

    Symlinks are tested for first and never followed. A followed link to a
    directory would be read as though the tree that it points at belonged to
    this one, and a followed dangling link would read as absent. The collected
    sample has links of both kinds.

    Files are described by content rather than mtime, because a tree that has
    been copied, rsynced or restored by a tool that does not preserve
    timestamps is still the tree that the cloning step wrote. What
    `is_discarded` names is left out for the opposite reason: an entry that the
    pipeline deletes cannot be part of what identifies the tree.

    The tree is read with a stack of its own rather than by recursion, because
    the collector copies a configuration directory whatever its depth. Over a
    directory nested deeper than the recursion limit, a function that calls
    itself for each directory raises RecursionError, which no caller catches.
    The finished cloning step would then stay unrecorded, and every Start would
    collect the sample again. The stack takes the children of a directory in
    reverse order. The lines therefore come in the order that recursion gives,
    and a record written earlier stays valid.
    """
    lines = []
    pending = [(path, rel)]
    while pending:
        path, rel = pending.pop()
        kind = entry_kind(path)
        if kind == LINK:
            lines.append(f"l {rel} {os.readlink(path)}")
        elif kind == DIRECTORY:
            lines.append(f"d {rel}")
            children = [(path / name, f"{rel}/{name}") for name in sorted(os.listdir(path))
                        if not is_discarded(path / name)]
            pending.extend(reversed(children))
        elif kind == FILE:
            lines.append(f"f {rel} {_sha256_of(path)}")
        else:
            lines.append(f"? {rel}")
    return lines


def resolve_produced(step: Step, rel: str) -> Path:
    """Where a declared output actually is, following a link at this level only.

    `manifest_lines` never follows a symlink, which is correct for a link found
    while listing a tree: a link inside the sample is a configuration artifact
    that the collector copied, and its target string identifies it. A
    `produces` entry is not a configuration artifact. It is where a step was
    told to put its output, and putting a large tree on another volume and
    linking to it is the ordinary way to do that.

    Described as a symlink, `data/collected` would be a single manifest line
    that names the mount point, and that line would be the same whether the
    mount contains the sample, contains a different snapshot, or has been
    unmounted. The label stays `rel`, and moving the tree and pointing the link
    at an identical copy leaves the record valid.
    """
    path = data_dir(step) / rel
    if entry_kind(path) == LINK:
        path = Path(os.path.realpath(path))
    return path


class UnreadableOutput(Exception):
    """A declared output that is on disk and cannot be read.

    Raised rather than exited, so that each caller states the error in its own
    words. Treating the output as "not done" is wrong for every caller, in
    different ways: on a resume, it runs the step again over output that is
    already there, and under `--mark-complete` it would record an output that
    nobody checked.
    """

    def __init__(self, step_key: str, rel: str, error: OSError):
        super().__init__(f"{rel} ({error})")
        self.step_key, self.rel, self.error = step_key, rel, error


def output_fingerprint(step: Step) -> str:
    """A checksum manifest of everything that `step` declares as produced, with an
    output that `extended` names recorded by its presence alone.

    `""` when any declared output is absent, as `script_fingerprint` returns for
    a missing script. Against a recorded value, it means the same: whatever
    produced that record is not what is on disk now.

    An output that exists but cannot be read raises `UnreadableOutput`. The
    alternative is to treat a result that is on disk as "not done", which for
    `repository_cloning` costs another run of the collector over its whole
    input list.
    """
    lines = []
    for rel in step.produces:
        # The absence check is inside the try, not above it. `entry_kind` re-raises every errno
        # other than ENOENT, ENOTDIR, EBADF and ELOOP, among them the errors of a mount that is
        # present but not working, such as EACCES, EIO, ESTALE, ENOTCONN and ETIMEDOUT. Outside
        # the try, those errors would reach main's `except OSError`, which logs a failure to
        # write the state file and names the sample as the file that it could not write.
        try:
            path = resolve_produced(step, rel)
            if entry_kind(path) == ABSENT:
                return ""
            # An output that a later step adds to counts by its presence (see `extended`).
            lines += ([f"{rel} extended"] if rel in step.extended
                      else manifest_lines(path, rel))
        except OSError as e:
            raise UnreadableOutput(step.key, rel, e) from e
    digest = hashlib.sha256()
    for line in lines:
        # surrogateescape, because a filename is bytes. `os.listdir` returns a name that is
        # not valid UTF-8 with surrogates in it, and plain `encode` rejects them. Linux
        # allows such a name, and the sample consists of arbitrary GitHub repositories.
        #
        # Length-prefixed rather than newline-joined, which makes the encoding injective.
        # Otherwise a file with a newline in its name could not be distinguished from two
        # entries.
        encoded = line.encode("utf-8", "surrogateescape")
        digest.update(b"%d:" % len(encoded))
        digest.update(encoded)
    return f"{COMPLETION_OUTPUT}:{digest.hexdigest()}"


def inputs_fingerprint(step: Step) -> str:
    """A hash over the step's script, the contents of every declared input, and what its
    `inputs_of` names, each a label and a digest.

    `""` when the script or an input is absent. Against a recorded value, this means what it
    means for the other two policies: whatever produced the record is not what is on disk
    now. `inputs_of` is for what no list of paths can name, such as the files of the
    validated tree that the mechanism CSVs list. A step without it hashes its script and its
    declared inputs alone.
    """
    script = script_fingerprint(step)
    if not script:
        return ""
    # The modules among the code inputs are code like the script and are hashed like it, and a
    # comment in one of them runs nothing again.
    modules = {code_dir(step) / rel for rel in step.code_inputs if rel.endswith(".py")}
    digest = hashlib.sha256()
    digest.update(script.encode("ascii"))
    for label, path in input_entries(step):
        if path is None:
            return ""
        digest.update(b"\0" + label.encode("utf-8") + b"\0")
        try:
            # Wrapped like the read of `output_fingerprint`, and for the same reason.
            # `input_entries` checks `is_file()`, which passes a file that can be stat'ed but
            # not opened. An EACCES or an EIO here would escape as a bare OSError, and main
            # would log a failure to write the state file and name a heuristics file as the
            # file that it could not write.
            digest.update((code_digest([path]) if path in modules
                           else _sha256_of(path)).encode("ascii"))
        except OSError as e:
            raise UnreadableOutput(step.key, label, e) from e
    for label, value in (step.inputs_of(step) if step.inputs_of else ()):
        if value is None:
            return ""
        digest.update(b"\0" + label.encode("utf-8") + b"\0" + value.encode("utf-8"))
    return f"{COMPLETION_INPUTS}:{digest.hexdigest()}"


HEURISTICS_DIR = heuristics.HEURISTICS_DIR


def input_labels(step: Step) -> list:
    """What a step's fingerprint names: its `inputs` as written, its `code_inputs` under its
    `code` directory, its `heuristics` files under `heuristics/`, and what its `inputs_of`
    names."""
    return (list(step.inputs) + [f"{step.code}/{rel}" if step.code else rel
                                 for rel in step.code_inputs]
            + [f"heuristics/{name}" for name in step.heuristics]
            + ([label for label, _ in step.inputs_of(step)] if step.inputs_of else []))


def recorded_inputs_fingerprint(step: Step) -> str:
    """A hash over what `step.inputs_of` names, each a label and a digest, or `""` when one
    is absent."""
    return inputs_part(step.inputs_of(step))


def inputs_part(pairs) -> str:
    """The part of a record that `pairs`, what `inputs_of` names as pairs of a label and a
    digest, give: a hash over the pairs, or `""` when a digest is absent."""
    digest = hashlib.sha256()
    for label, value in pairs:
        if value is None:
            return ""
        digest.update(b"\0" + label.encode("utf-8") + b"\0" + value.encode("utf-8"))
    return f"{COMPLETION_INPUTS}:{digest.hexdigest()}"


def collection_list(step: Step) -> str:
    """The list of repositories that the cloning step collects, relative to its data directory:
    the `--repo-csv` of its arguments, which `--repo-csv` and `--step-arg` add, or the list that
    `engineered_project_selection` writes."""
    return _flag_value(step.args, "--repo-csv", DEFAULT_REPO_CSV)


def _repositories_digest(sample: Path, column: str) -> "str | None":
    """The digest of the repositories that the cloning step reads from `sample`, the digest that
    names its collection (`resume.open_collection`), or None when the list has no such column.
    The list carries the other columns of the labels, such as the justification, and a rewrite
    of these columns with the same repositories must not run the step again: run again, the
    step would continue the collection that ended, and every step after it would run again."""
    from agentcontext.steps import repository_cloning
    try:
        repos = repository_cloning.parse_repo_csv(sample, column)
    except SystemExit:
        return None
    return hashlib.sha256("\n".join(sorted(repos)).encode("utf-8")).hexdigest()


def collection_inputs(step: Step) -> list:
    """What records `repository_cloning` besides its tree, each as a label and a digest, with
    None as the digest of an absent file: the collector's code, the version of git that reads
    the histories, the path heuristics that decide what it copies, including the heuristics of
    the planning documents and the places of the tools' local state (`reuse.path_heuristics`),
    the import heuristics that decide what it copies to the imported tree, and the list of
    repositories that it collects.

    A git that prints no version (`clone.git_version`) stops the run. Read as an absent input,
    it would count the step as not done, and the run would start the collection and mark every
    step after it to run again.

    `reuse` checks these inputs before it takes a repository over from the tree. If the step
    were recorded by its tree alone, it would be skipped after a change to any of them. The run
    would then publish the CSVs of new heuristics over files that the old heuristics chose,
    keep the collection of a sample that the classification has replaced, or keep the Git
    columns of another version of git. A change to one input runs the step, which takes over
    the repositories that the change does not affect. The collector's code is its fingerprint
    without docstrings, log statements and exception texts and without the modules that only
    schedule the run (`reuse.collector_fingerprint`), and the path heuristics are the columns
    that decide what is copied. A new log message, a reworded error, or a page that the
    documentation watch read again, therefore runs nothing.
    """
    from agentcontext import reuse
    from agentcontext.git import clone
    patterns = HEURISTICS_DIR / "config_path_patterns.csv"
    vendored = HEURISTICS_DIR / "vendored_dirs.txt"
    planned = HEURISTICS_DIR / "planning_document_patterns.csv"
    local_state = HEURISTICS_DIR / "local_state_paths.csv"
    sample = data_dir(step) / collection_list(step)
    try:
        version = clone.git_version()
    except clone.GitVersionUnavailable as error:
        raise SystemExit(f"{error}. Stopped the run, because the version of git decides "
                         f"whether {step.key} is complete. Start the run again once git "
                         f"runs.") from error
    entries = [(COLLECTOR_CODE, reuse.collector_fingerprint()), (GIT_VERSION, version)]
    try:
        if (patterns.is_file() and vendored.is_file() and planned.is_file()
                and local_state.is_file()):
            rows = heuristics.load_config_path_patterns(patterns)
            places = heuristics.load_local_state_paths(local_state, rows)
            kept = reuse.path_heuristics(rows, heuristics.load_vendored_dirs(vendored),
                                         planning.load_planning_document_patterns(planned),
                                         [row["path"] for row in places])
            entries.append((PATH_HEURISTICS, reuse.heuristics_sha(kept)))
        else:
            entries.append((PATH_HEURISTICS, None))
        import_patterns = HEURISTICS_DIR / imports.IMPORT_PATTERNS_PATH.name
        entries.append((IMPORT_HEURISTICS,
                        reuse.heuristics_sha(reuse.import_heuristics(
                            imports.load_import_patterns(import_patterns)))
                        if import_patterns.is_file() else None))
        entries.append((REPOSITORY_LIST,
                        _repositories_digest(sample, _flag_value(step.args, "--repo-col",
                                                                 "repo_name"))
                        if sample.is_file() else None))
    except OSError as e:
        raise UnreadableOutput(step.key, "an input", e) from e
    return entries


def input_entries(step: Step) -> list:
    """`(label, path)` for everything that the COMPLETION_INPUTS policy hashes, with None as the
    path of an absent file. The digest includes the label, which is therefore the same on every
    machine: an `inputs` entry as written, a heuristics file as `heuristics/<name>`."""
    entries = []
    for rel in step.inputs:
        paths = input_paths(step, rel)
        if not paths:
            entries.append((rel, None))
        elif len(paths) == 1 and not any(char in rel for char in "*?["):
            entries.append((rel, paths[0]))
        else:
            entries.extend((f"{rel}:{path.name}", path) for path in paths)
    for rel in step.code_inputs:
        path = code_dir(step) / rel
        entries.append((f"{step.code}/{rel}" if step.code else rel,
                        path if path.is_file() else None))
    for name in step.heuristics:
        path = HEURISTICS_DIR / name
        entries.append((f"heuristics/{name}", path if path.is_file() else None))
    return entries


# The name of a 7-Zip archive, or of a volume of a split archive.
ARCHIVE_NAME = re.compile(r"\.7z(\.\d+)?$")


def input_paths(step: Step, rel: str) -> list:
    """The files that decide an `inputs` entry: those of its first alternative on disk, every
    volume of a split archive included, and the 7-Zip archives on disk among the alternatives
    after it.

    An archive among the alternatives holds the file that an earlier alternative names, as the
    archive of the commit CSV holds the CSV. A reader of the extracted file compares it with the
    archive that git tracks beside it (`archives.check_copy`), and the archive therefore decides
    what the step does too: A changed archive runs the step again, and its reader stops on the
    extracted file that no longer matches. Any other alternative after the first on disk is read
    only when the first is absent, as `output/repos.csv` makes the entry of `collection_summary`
    present when no commit reclassification wrote its record. Hashing such an alternative would
    add nothing that the step reads and would change the fingerprint that a finished run
    recorded, and a run on a new image would then run the step and every step that consumes it
    again."""
    found = []
    for alternative in rel.split("|"):
        pattern = str(data_dir(step) / alternative)
        paths = sorted(Path(name) for name in glob.glob(pattern) if Path(name).is_file())
        if not found:
            found = paths
        else:
            found += [path for path in paths if ARCHIVE_NAME.search(path.name)]
    return found


def input_path(step: Step, rel: str):
    """The first file of an available input alternative, or None if absent."""
    paths = input_paths(step, rel)
    return paths[0] if paths else None


def step_fingerprint(step: Step, inputs: str | None = None) -> str:
    """What a finished run records for `step`, per its `completion` field. A step recorded by
    its output that also names what else decides it (`inputs_of`) is recorded by both, joined
    by `+`, or by `""` when either is absent. `inputs` is that second part when the caller has
    already computed it (`inputs_before`)."""
    if step.completion == COMPLETION_OUTPUT:
        output = output_fingerprint(step)
        if step.inputs_of is None or not output:
            return output
        if inputs is None:
            inputs = recorded_inputs_fingerprint(step)
        return f"{output}+{inputs}" if inputs else ""
    if step.completion == COMPLETION_INPUTS:
        return inputs_fingerprint(step)
    return script_fingerprint(step)


def step_fingerprint_or_stop(step: Step) -> str:
    """`step_fingerprint`, with an unreadable output ending the run.

    For the callers that decide whether a recorded step still counts. For
    them, the only alternatives to stopping are to run a step again over output
    that is already there, or to skip it over output that nobody could read.
    `--mark-complete` and the recording of a step that has just finished each
    catch `UnreadableOutput` and state it in their own words.
    """
    try:
        return step_fingerprint(step)
    except UnreadableOutput as e:
        raise SystemExit(f"Could not read {e.rel} of step '{e.step_key}' ({e.error}). Whether "
                         f"the step is complete depends on it. Make the path readable and start "
                         f"the run again.") from e


def inputs_before(step: Step) -> list | None:
    """For a step recorded by its output and by `inputs_of`, what `inputs_of` names, as pairs
    of a label and a digest, computed right before the step runs, and None for every other step.
    The pairs give the part of the record that `inputs_of` gives (`inputs_part`), and the record
    names them (`record_completion`).

    The runner records the step under these inputs once it exits 0, so that the recording needs
    nothing that the step's work could have lost. Computed after the step, the inputs need git
    again (`collection_inputs`). A git that cannot start at that moment would leave the
    finished cloning step unrecorded after the step had closed its collection. The next Start
    would then run the step again, which continues the collection that ended, and every step
    after it. The step reads none of these inputs from its own output, and they are therefore
    what it ran under. A git that cannot start before the step stops the run before the step
    begins, which loses nothing.
    """
    if step.completion != COMPLETION_OUTPUT or step.inputs_of is None:
        return None
    try:
        return [[label, value] for label, value in step.inputs_of(step)]
    except UnreadableOutput as e:
        raise SystemExit(f"Could not read {e.rel} of step '{e.step_key}' ({e.error}). "
                         f"Recording the step after its run needs it. Make the path readable "
                         f"and start the run again.") from e


def changed_during_run(step: Step, recorded: str, state: dict | None = None) -> bool:
    """Whether what decides the record of a step no longer matches `recorded`, the record that
    counted as done when the run began, computed again when the run reaches the step.

    For a step recorded by its inputs, the function compares its fingerprint. For a step
    recorded by its output that names `inputs_of`, it compares the part of the record that
    `inputs_of` gives, not the manifest of the output: no other step writes the output, and its
    manifest can take long to compute, as for the collected tree. The labeling can give the
    cloning step a new list during the same run. Without this part computed again, the run
    would skip the collection and publish the new labels over the collection of the old
    sample. A step recorded by its script decides nothing from what an earlier step writes.

    A step that counted as done with a change of its `kept_on` inputs alone (`changes_kept`)
    stays done while no other input changes. `state` holds the record of the step, which names
    the digest of each input.
    """
    if step.completion == COMPLETION_INPUTS:
        return step_fingerprint_or_stop(step) != recorded
    if step.completion == COMPLETION_OUTPUT and step.inputs_of is not None:
        try:
            pairs = step.inputs_of(step)
        except UnreadableOutput as e:
            raise SystemExit(f"Could not read {e.rel} of step '{e.step_key}' ({e.error}). "
                             f"Whether the step is complete depends on it. Make the path "
                             f"readable and start the run again.") from e
        if inputs_part(pairs) == recorded.partition("+")[2]:
            return False
        if state is None:
            return True
        return not changes_kept(step, state, pairs)
    return False


def fingerprint_subject(step: Step) -> str:
    """What to name when a recorded fingerprint no longer matches."""
    if step.completion == COMPLETION_OUTPUT:
        return f"The output of {step.key} ({series(step.produces)})"
    if step.completion == COMPLETION_INPUTS:
        return f"An input of {step.key} ({series(input_labels(step))}) or its script"
    return step_module(step) or (step.args[0] if step.args else step.key)


def read_state() -> dict:
    """The pipeline execution state, or `{}` when there is none.

    An unreadable state file is not the same as an absent state file.
    `write_state` runs after every step, and a run killed during a write can
    leave a truncated file. Returning `{}` for that file would read as "no run
    has happened", discard the record of every completed step, and start the
    whole collection again. The function stops the run instead, and the
    operator deletes the file to start over.
    """
    if not STATE_PATH.is_file():
        return {}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise SystemExit(f"The pipeline execution state {STATE_PATH} exists but could not be "
                         f"read ({e}). Repair the file to resume the run, or delete it to run "
                         f"every step again.") from e
    # docs/runner.md allows editing this file by hand, and the functions that write into
    # `state["completed"]` expect an object. A value that the runner never writes would reach
    # them as a TypeError traceback. It gets a message instead, like every other bad input here.
    modified = state.get(MODIFIED, {}) if isinstance(state, dict) else None
    if (not isinstance(state, dict) or not isinstance(state.get("completed", {}), dict)
            or not isinstance(state.get(COMPLETED_BY, {}), dict)
            or not isinstance(state.get(UNFINISHED, []), list)
            or not all(isinstance(key, str) for key in state.get(UNFINISHED, []))
            or not isinstance(modified, dict)
            or not all(isinstance(mutators, list) and all(isinstance(key, str) for key in mutators)
                       for mutators in modified.values())):
        raise SystemExit(f"{STATE_PATH} is valid JSON but not a pipeline execution state. "
                         f"Expected an object with an object `completed` and, optionally, an "
                         f"object `{COMPLETED_BY}`, a list of step names `{UNFINISHED}` and an "
                         f"object `{MODIFIED}` that maps a step name to a list of step names. "
                         f"Repair the file, or delete it to run every step again.")
    return state


def set_modified(state: dict, damaged: str, mutator: str) -> None:
    """Name `mutator` in `state` as a step that started to modify the output of `damaged`. The
    caller writes the file."""
    mutators = state.setdefault(MODIFIED, {}).setdefault(damaged, [])
    if mutator not in mutators:
        mutators.append(mutator)


def clear_modified(state: dict, damaged: str) -> None:
    """Remove what `state` records as modifying the output of `damaged`, which `damaged` has
    just produced again. The caller writes the file."""
    modified = state.get(MODIFIED, {})
    modified.pop(damaged, None)
    if not modified:
        state.pop(MODIFIED, None)


def set_unfinished(state: dict, key: str, unfinished: bool) -> None:
    """List `key` under UNFINISHED in `state`, or remove it. The caller writes the file."""
    keys = [k for k in state.get(UNFINISHED, []) if k != key]
    if unfinished:
        keys.append(key)
    if keys:
        state[UNFINISHED] = keys
    else:
        state.pop(UNFINISHED, None)


def write_state(state: dict) -> None:
    """Write the state file atomically.

    Not best-effort, because a resume reads this file. A write that failed
    without an error after a long step would leave that step unrecorded, and
    the next run would redo it.

    `write_text` truncates first, and a kill at any point of the write
    corrupts the file. Writing a temporary file and replacing the state file
    with it is atomic on POSIX: an interruption leaves either the previous
    state or the new state, never part of either. The rename alone is not
    durable. After a power loss, a rename that reached the disk before the
    data leaves an empty or truncated state file, and `read_state` then stops
    every later start. `caches.replace_file` therefore syncs the temporary file
    before the rename and the directory after it.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    caches.replace_file(STATE_PATH, json.dumps(state, indent=2) + "\n")


def record_completion(state: dict, key: str, fingerprint: str, how: str,
                      finished_at: str | None = None, code: dict | None = None,
                      inputs: list | None = None) -> None:
    """Record `key` as complete in `state` with `fingerprint`, and what the record came from
    under COMPLETED_BY: `how`, the time `finished_at` (now by default), `code`, the commit
    that `_git_info` gives (this code's by default), and `inputs`, what `inputs_of` named for
    the record as pairs of a label and a digest, where the caller has them. The caller writes
    the file."""
    state.setdefault("completed", {})[key] = fingerprint
    state.setdefault(COMPLETED_BY, {})[key] = {
        "fingerprint": fingerprint,
        "how": how,
        "finished_at": finished_at or datetime.now(timezone.utc).isoformat(),
        **(code if code is not None else _git_info()),
        **({"inputs": [[label, value] for label, value in inputs]} if inputs is not None
           else {}),
    }


def recorded_inputs(state: dict, key: str) -> list | None:
    """What `inputs_of` named for the record of `key` that `completed` holds, as pairs of a
    label and a digest, or None where COMPLETED_BY names no pairs for that record or the pairs
    do not give its inputs part. A record of a runner that named no pairs, or one edited by
    hand, then names none."""
    entry = (state.get(COMPLETED_BY) or {}).get(key)
    record = (state.get("completed") or {}).get(key)
    if (not isinstance(entry, dict) or not isinstance(record, str)
            or entry.get("fingerprint") != record):
        return None
    pairs = entry.get("inputs")
    if not (isinstance(pairs, list) and all(
            isinstance(pair, list) and len(pair) == 2 and all(isinstance(v, str) for v in pair)
            for pair in pairs)):
        return None
    pairs = [(label, value) for label, value in pairs]
    return pairs if inputs_part(pairs) == record.partition("+")[2] else None


def changed_inputs(step: Step, state: dict, pairs: list) -> "list | None":
    """The labels of the inputs of `step` with a digest in `pairs`, what `inputs_of` names now,
    that differs from the digest that the record of the step names, or None where the record
    names no digests."""
    recorded = recorded_inputs(state, step.key)
    if recorded is None:
        return None
    before, now = dict(recorded), dict(pairs)
    return [label for label in dict.fromkeys([*before, *now])
            if before.get(label) != now.get(label)]


def changes_kept(step: Step, state: dict, pairs: list) -> list:
    """The labels that `changed_inputs` gives for `step` and `pairs` where each of them is in
    `step.kept_on`, and otherwise an empty list. The step stays recorded only with such labels.
    A record that names no digests keeps nothing."""
    if not step.kept_on:
        return []
    changed = changed_inputs(step, state, pairs) or []
    return changed if changed and set(changed) <= set(step.kept_on) else []


def name_recorded_inputs(steps: list, state: dict, done: set) -> bool:
    """Name the pairs of `inputs_of` in the record of each step in `done` with `kept_on` that
    names none, where its inputs are what the record was made under, and return whether a
    record changed. The caller writes the file.

    A runner that named no pairs, such as the runner of the image with the backup providers, recorded the step by the hash
    of its inputs alone, and no later run could tell which of them changed. A record that
    counts as done with the same inputs part names the pairs that it was made under, which
    are the pairs of now. Named here, they let a later image that changes only `kept_on`
    inputs keep the step (`changes_kept`). The container converts such a record once, with the
    record of the collection that the step ended, from the lines of the collection
    (deploy/entrypoint.py, `convert_cloning_records`)."""
    changed = False
    by_key = {step.key: step for step in steps}
    for key in sorted(done):
        step = by_key.get(key)
        if step is None or not step.kept_on or recorded_inputs(state, key) is not None:
            continue
        entry = (state.get(COMPLETED_BY) or {}).get(key)
        record = (state.get("completed") or {}).get(key)
        if not isinstance(entry, dict) or entry.get("fingerprint") != record:
            continue
        try:
            pairs = step.inputs_of(step)
        except UnreadableOutput:
            continue
        if inputs_part(pairs) == str(record).partition("+")[2]:
            entry["inputs"] = [[label, value] for label, value in pairs]
            changed = True
    return changed


def completion_origin(state: dict, key: str) -> str:
    """The sentences of the skip line that say when and with which code the record of `key`
    was made, or `""` when COMPLETED_BY has no entry for the record that `completed` holds."""
    entry = (state.get(COMPLETED_BY) or {}).get(key)
    if (not isinstance(entry, dict) or entry.get("how") not in COMPLETED_HOW
            or entry.get("fingerprint") != state.get("completed", {}).get(key)):
        return ""
    try:
        finished = datetime.fromisoformat(entry["finished_at"]).astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError):
        return ""
    when = finished.strftime("on %Y-%m-%d at %H:%M UTC")
    sha = entry.get("git_sha")
    if isinstance(sha, str) and sha:
        when += f" with the code of commit {sha[:8]}"
        if entry.get("git_dirty"):
            when += " and uncommitted changes"
    if entry["how"] == "adopted":
        return f"Recorded from its output on disk {when}."
    if entry["how"] == "marked_complete":
        return f"Recorded by --mark-complete {when}."
    return f"Finished {when}. Its log is {LOG_DIR.name}/{key}.log."


def mark_stale(state: dict, keys, steps: list) -> bool:
    """Prefix the records of `keys` with STALE. The caller writes the file. UNFINISHED covers a
    step that takes a snapshot, and this function leaves its record unchanged. Returns whether
    a record changed."""
    recorded = state.setdefault("completed", {})
    irreproducible = {s.key for s in steps if s.irreproducible}
    changed = False
    for key in sorted(keys):
        value = recorded.get(key)
        if value is None or key in irreproducible or value.startswith(STALE):
            continue
        recorded[key] = STALE + value
        changed = True
    return changed


def completed_steps(steps: list, state: dict) -> set:
    """The recorded steps that still count as done for this pipeline.

    A step stops counting when its fingerprint no longer matches its record,
    and so does every step computed from it, because those results were
    derived from the changed step's output. A newly enabled step also
    invalidates its recorded consumers before it runs.

    The step's completion policy decides what the fingerprint covers: the
    script for a step with output that can be derived again, the output itself
    for a step that took a snapshot of something that the pipeline does not
    control, and the script and the inputs for a step recorded by its inputs.
    See COMPLETION_SCRIPT, COMPLETION_OUTPUT and COMPLETION_INPUTS.
    """
    recorded = state.get("completed", {})
    by_key = {s.key: s for s in steps}
    done = {k for k in recorded if k in by_key}
    for key in sorted(done):
        if key not in done:
            continue
        step = by_key[key]
        if recorded[key].startswith(STALE):
            stale = set(invalidated_by(steps, key)) & done
            logs.info("runner", f"{key} did not finish its last run after one of its inputs "
                                f"changed. No longer counting {series(sorted(stale))} as done.")
            done -= stale
            continue
        current = step_fingerprint_or_stop(step)
        # An empty fingerprint cannot establish that an absent output is complete. The runner
        # checks the outputs of every step that declares any, not only for the two completion
        # policies with a fingerprint that is "" for an absent output. A step recorded by its
        # script has the same fingerprint whether or not its output is there, and a step
        # recorded by its inputs never reads `produces`. Without the check, a deleted
        # `output/repos.csv` would leave every step after `collected_file_cleanup` counting as
        # done.
        # The check is inside the try, because `entry_kind` re-raises every errno other than
        # the four that mean absence, among them the errors of a mount that is present and not
        # responding. Outside the try, those errors would reach `main`'s `except OSError`,
        # which logs a failure to write the state file and names a pipeline output as the file
        # that it could not write.
        try:
            absent = [rel for rel in step.produces
                      if entry_kind(resolve_produced(step, rel)) == ABSENT]
        except OSError as e:
            # The same `SystemExit` that `step_fingerprint_or_stop` raises. An `UnreadableOutput`
            # is not an `OSError`, and nothing above this point would catch it: the run would
            # end on a traceback without a message. In the container, the message is the only
            # sign of the error.
            raise SystemExit(f"Could not read an output of step '{step.key}' ({e}). Whether "
                             f"the step is complete depends on it. Make the path readable and "
                             f"start the run again.") from e
        gone = bool(absent) or (step.completion in (COMPLETION_OUTPUT, COMPLETION_INPUTS)
                                and not current)
        if not gone and recorded[key] == current:
            continue
        stale = set(invalidated_by(steps, key)) & done
        if gone:
            what, named = (("output", absent) if absent
                           else ("input", input_labels(step)))
            logs.info("runner", f"{key} declares {what} that is not on disk "
                                f"({series(named)}). No longer counting "
                                f"{series(sorted(stale))} as done.")
        elif (step.completion == COMPLETION_OUTPUT and step.inputs_of is not None
              and recorded[key].split("+")[0] == current.split("+")[0]):
            # The tree is what the step wrote, but an input that decides the step has changed.
            # A change of `kept_on` inputs alone keeps the step, which then runs only by --from.
            try:
                pairs = step.inputs_of(step)
            except UnreadableOutput as e:
                raise SystemExit(f"Could not read {e.rel} of step '{e.step_key}' ({e.error}). "
                                 f"Whether the step is complete depends on it. Make the path "
                                 f"readable and start the run again.") from e
            kept = changes_kept(step, state, pairs)
            if kept:
                named = series(kept)
                logs.warning("runner", f"{named[:1].upper()}{named[1:]} changed since {key} "
                                       f"ran. Counting {key} as done with the collection that "
                                       f"the earlier inputs collected, because running it under "
                                       f"the changed inputs would collect every repository "
                                       f"again. Run --from {key} to take a new snapshot under "
                                       f"the changed inputs.")
                continue
            # Otherwise the step runs and takes over the repositories that the change does not
            # reach. The message therefore does not offer --mark-complete, which would record
            # the tree under the changed inputs. The step's own log names the collection that
            # it continues or begins.
            changed = changed_inputs(step, state, pairs)
            if changed:
                named = series(changed)
                logs.info("runner", f"{named[:1].upper()}{named[1:]} changed since {key} ran. "
                                    f"Running {key} under the changed inputs. No longer "
                                    f"counting {series(sorted(stale))} as done.")
            else:
                logs.info("runner", f"An input of {key} ({series(input_labels(step))}) has "
                                    f"changed since the step ran. No longer counting "
                                    f"{series(sorted(stale))} as done.")
        else:
            logs.info("runner", f"{fingerprint_subject(step)} has changed "
                                f"since {key} ran. No longer counting "
                                f"{series(sorted(stale))} as done.")
            if step.completion == COMPLETION_OUTPUT:
                logs.info("runner", f"If that output is still {key}'s result, record "
                                    f"it again with --mark-complete {key} instead of running "
                                    f"the step.")
        done -= stale
    # A newly enabled step has no record to inspect above. Its recorded consumers
    # still have to run after it, even if their own fingerprints have not changed.
    for step in steps:
        if step.key not in done:
            stale = set(invalidated_by(steps, step.key)) & done
            if stale:
                logs.info("runner", f"{step.key} is pending. No longer counting the steps "
                                    f"computed from its output as done "
                                    f"({series(sorted(stale))}).")
                done -= stale
    return done




def record_exited_step(steps: list, state: dict) -> bool:
    """Record the step that EXITED names, a step that exited 0 in a run that ended before it
    recorded the step. The record combines the inputs part of that run with the output that is
    on disk, after the check that `adopt_irreproducible` applies to an output. The function then
    adds the step's key to RECORDED_FROM_EXIT and removes EXITED. Returns whether the pipeline
    execution state changed, which it does whenever EXITED was there.

    EXITED is removed only once the step is recorded or refused. The fingerprint of the output
    reads the whole collected tree, and a second stop signal during that read exits at once and
    writes the state that the start holds. Without EXITED, that state would leave the step to
    run again."""
    if EXITED not in state:
        return False
    exited = state[EXITED]
    if isinstance(exited, dict):
        _record_exited(steps, state, exited)
    del state[EXITED]
    return True


def _record_exited(steps: list, state: dict, exited: dict) -> None:
    """Record the step that `exited`, the value of EXITED, names, as `record_exited_step`
    describes."""
    step = next((s for s in steps if s.key == exited.get("step")), None)
    if step is None:
        return
    # The record of a step recorded by its script or by its inputs is computed now, under this
    # start's code and heuristics. It is valid only while these are what the step ran with: the
    # commit of the code, which also names the heuristics, and the step's own code, which an
    # uncommitted edit changes. The record of a step recorded by its output holds its output and
    # the inputs part of the run that wrote it, and no code, as on every other path:
    # `completed_steps` compares that part with the current inputs. If this record were checked
    # against the code, a new image would run the cloning step again after a kill, and every
    # step after it, although the step had closed its collection.
    if step.completion != COMPLETION_OUTPUT and (
            exited.get("code") != _git_info() or exited.get("script") != script_fingerprint(step)):
        logs.info("runner", f"{logs.step_name(step.key)} exited 0 under other code before the "
                            f"last run ended, without being recorded. Running it again.")
        return
    if any(_is_empty_output(resolve_produced(step, rel)) for rel in step.produces
           if rel not in step.may_be_empty):
        return
    pairs = exited.get("input_pairs")
    try:
        fingerprint = step_fingerprint(step, exited.get("inputs"))
    except UnreadableOutput as error:
        logs.warning("runner", f"{logs.step_name(step.key)} exited 0 before the last run "
                               f"ended, without being recorded. Could not read {error.rel} of "
                               f"the step ({error.error}). Running it again.")
        return
    if fingerprint:
        # The time and the code of the invocation in which the step exited, not of this one.
        code = exited.get("code")
        record_completion(state, step.key, fingerprint, "ran",
                          finished_at=exited.get("finished_at"),
                          code=code if isinstance(code, dict) else None,
                          inputs=pairs if isinstance(pairs, list) else None)
        set_unfinished(state, step.key, False)
        clear_modified(state, step.key)
        state[RECORDED_FROM_EXIT] = sorted(set(state.get(RECORDED_FROM_EXIT) or ())
                                           | {step.key})
        logs.info("runner", f"{logs.step_name(step.key)} exited 0 before the last run ended, "
                            f"without being recorded. Recorded it from its output.")


def adopt_irreproducible(steps: list, state: dict, redo: set) -> bool:
    """Record an irreproducible step with outputs that are already on disk.

    Running such a step again takes another snapshot rather than redoing its
    work. An output that is on disk is therefore the result, whoever produced
    it, and the runner records it with its current fingerprint. A step that
    `--from` names is left alone, because the operator requested a new
    snapshot. Nothing is recorded over a declared directory that is empty,
    which is not a snapshot, or for a step that the pipeline execution state
    lists under UNFINISHED. Returns whether the state changed.
    """
    recorded = state.setdefault("completed", {})
    unfinished = set(state.get(UNFINISHED, []))
    changed = False
    for step in steps:
        if not step.irreproducible or step.key in redo:
            continue
        current = step_fingerprint_or_stop(step)
        if not current or recorded.get(step.key) == current:
            continue
        # The output that a failed or stopped run of the step left is not recorded. The clone
        # lists of the classification are written row by row, and the download replaces its
        # snapshot in place. The output on disk is then not empty and would pass as the whole
        # snapshot.
        if step.key in unfinished:
            logs.warning("runner", f"{step.key} did not finish its last run. Running it again "
                                   f"rather than recording the output that the run left. To "
                                   f"record that output as complete instead, use "
                                   f"--mark-complete {step.key}.")
            continue
        # The same check that `--mark-complete` applies, because this is the same recording
        # without the flag. An empty directory or a zero-length file is no snapshot. This
        # recording runs without any flag on every invocation of every runner. Without the
        # check, it would record a download killed early, and every later run would skip the
        # step and build from nothing.
        if any(_is_empty_output(resolve_produced(step, rel)) for rel in step.produces
               if rel not in step.may_be_empty):
            continue
        record_completion(state, step.key, current, "adopted")
        changed = True
        logs.info("runner", f"{step.key} is irreproducible, and its output is on disk "
                            f"({', '.join(step.produces)}). Recorded the step as complete without "
                            f"running it. Run --from {step.key} to run it again.")
    return changed




# The descriptor that holds the run lock, for the life of this process.
_LOCK_FD = None


def acquire_lock() -> bool:
    """Take the run lock at LOCK_PATH: an flock held until `release_lock` or exit.

    Returns False when another live run holds it. A pid written into the file and checked
    with `os.kill(pid, 0)` does not work in a container, where the runner is pid 1 on every
    start. After a hard stop, the file would name a process that is always alive, and every
    restart would refuse to run. The kernel releases the flock of a dead holder, and there is
    no stale state to detect and nothing to take over. The file contains an identifier for
    the operator, with the process, host and start time of the holder, and is never read to
    test whether the holder is alive. `checks/snapshot.py` uses the same arrangement for the
    snapshot lock.
    """
    global _LOCK_FD
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return False
    except BaseException:
        os.close(fd)
        raise
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    os.ftruncate(fd, 0)
    os.write(fd, f"pid {os.getpid()} on {platform.node()} since {started}\n".encode())
    _LOCK_FD = fd
    return True


def release_lock() -> None:
    """Drop the lock by closing its descriptor. The file stays.

    Never unlinked, because a waiter that had opened the old inode would lock a file without
    a name while the next run created a new file at the path, and both runs would proceed.
    """
    global _LOCK_FD
    if _LOCK_FD is not None:
        os.close(_LOCK_FD)
        _LOCK_FD = None


def step_names(steps: list) -> list:
    return [s.key for s in steps]


def stage_names(steps: list) -> list:
    """The stages of the pipeline that the steps belong to, in the order of their first step."""
    return list(dict.fromkeys(s.stage for s in steps if s.stage))


def log_path(name: str) -> Path:
    return LOG_DIR / f"{name}.log"


def pending_step_names(steps: list, done: set) -> set:
    """The keys of the steps that this invocation runs. An earlier run recorded
    every other step as complete. --from removes steps from `done` before this
    function is called."""
    return {s.key for s in steps if s.key not in done}


def _step_argv(steps: list, name: str) -> list:
    for s in steps:
        if s.key == name:
            return s.args
    return []


def _flag_value(argv: list, flag: str, default: str) -> str:
    """The value of `flag` in a step's arguments as the step's own parser reads it: its last
    occurrence, as `flag VALUE` or `flag=VALUE`, else `default`. Read in another way, such as
    by its first occurrence alone, the value would make the runner record and check a list other
    than the list that the step collects."""
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument(flag, dest="value", default=default)
    try:
        known, _ = parser.parse_known_args(argv)
    except SystemExit:
        return default
    return known.value


def _require_known_name(steps: list, name: str) -> None:
    """Exit 1 when `name` is neither a stage of the pipeline nor a step in this run's list."""
    if name not in step_names(steps) and name not in stage_names(steps):
        known = [f"The stage {stage} has the steps "
                 f"{series(s.key for s in steps if s.stage == stage)}."
                 for stage in stage_names(steps)]
        unstaged = [s.key for s in steps if not s.stage]
        if unstaged:
            known.append(f"The steps outside a stage are {series(unstaged)}.")
        logs.error("runner", " ".join([f"Unknown stage or step '{name}'.", *known]))
        sys.exit(1)


def _grouped_problems(problems: list) -> dict:
    """The affected steps of each pair of a damaged step and the step that modified its output,
    so that one message names each damaged step once. Repeated once per affected step, the same
    remedy would read as several different problems."""
    grouped = {}
    for affected, damaged, mutator in problems:
        grouped.setdefault((damaged, mutator), set()).add(affected)
    return grouped


def report_mutated_inputs(problems: list) -> None:
    """Log one message per damaged step that the run does not produce again on its own, naming
    the `--from` that repairs it."""
    for (damaged, mutator), affected in _grouped_problems(problems).items():
        logs.error("runner", f"'{mutator}' modified the output of '{damaged}' in place on an "
                             f"earlier run. Pending steps read that output "
                             f"({series(sorted(affected))}). '{damaged}' takes a snapshot or is "
                             f"not a step of this run, and it does not run again on its own. Run "
                             f"python3 -m agentcontext run --from {damaged} to produce its "
                             f"output again.")


def report_rebuilds(problems: list) -> None:
    """Log one message per damaged step that the run produces again before a pending step reads
    its output."""
    for (damaged, mutator), affected in _grouped_problems(problems).items():
        logs.info("runner", f"'{mutator}' modified the output of '{damaged}' in place since "
                            f"'{damaged}' last ran. Pending steps read that output "
                            f"({series(sorted(affected))}). Running '{damaged}' again, with "
                            f"every step computed from it, as --from {damaged} does.")


def invalidated_by(steps: list, key: str) -> list:
    """`key` plus every step computed from it, transitively, in pipeline order.

    A step that runs again changes its outputs, and nothing computed from them
    is still this run's result. The function follows dependencies rather than
    positions in the list, because "runs after" is not "depends on": neither
    `output_cross_check` nor `collected_files_archive` reads anything that the
    other writes, and running one again must not run the other again.
    """
    # A stage of the pipeline stands for all of its steps.
    seeds = {s.key for s in steps if s.stage == key} or {key}
    reached, frontier = set(seeds), set(seeds)
    while frontier:
        frontier = {s.key for s in steps
                    if s.key not in reached and set(s.consumes) & frontier}
        reached |= frontier
    return [s.key for s in steps if s.key in reached]


def modified_input_problems(steps: list, pending: set, completed: set,
                            modified: "dict | None" = None) -> list:
    """Pending steps that read an output that an earlier run has already
    modified in place. One `(step, damaged_step, mutator)` per problem.

    A modifying step (`mutator`) has modified an output when it is in `completed`, or when
    `modified`, the MODIFIED entry of the pipeline execution state, names it for the step that
    produced the output. `completed` is the raw recorded set: every step that the state file
    names, before `--from` removes anything from it and before `completed_steps` removes any
    step with a changed fingerprint. An earlier run did the damage, and neither kind of removal
    undoes it. `modified` names a modifying step from the moment it started. It therefore also
    covers a modifying step that failed or was stopped after it modified the output, which the
    completion records do not name, and a modifying step with a record that `--from` removed
    before the step ran again.

    The two kinds of removal can remove the same step. `--from detection` on a finished run
    removes file_history, because it is computed from detection. An edit to
    `agentcontext/steps/detection.py` removes file_history for the same reason, through
    `completed_steps`. Given the set filtered by fingerprint, this check would miss, after the
    edit, exactly the run that it exists to find: detection running again over the
    `data/validated/` from which file_history removed directories, and writing a different
    `output/repos.csv`.

    A consumer listed after a modifying step that has run and does not run again is no
    problem. In an uninterrupted run it reads the output as the modifying step left it, and it
    reads the same output after a failure or a stop between the two. The archive of collected
    files reads the tree from which file_history removed directories, and a Start after a stop
    during its 7zz resumes it rather than rebuilding the tree.
    """
    position = {s.key: index for index, s in enumerate(steps)}
    modified = modified or {}
    problems = []
    for mutator in steps:
        for damaged in mutator.mutates:
            if mutator.key not in completed and mutator.key not in modified.get(damaged, ()):
                continue
            if damaged in pending:
                continue            # this run rebuilds it anyway
            for s in steps:
                # Direct consumers, not the transitive closure. Every later step depends on
                # `collected_file_cleanup` in the end, but the state of data/validated/ affects
                # only the steps that open it. `output_cross_check` reads output/ and nothing
                # else, and running it alone again against an output/repos.csv that is already
                # written is correct.
                if s.key not in pending or damaged not in s.consumes:
                    continue
                if mutator.key not in pending and position[s.key] > position[mutator.key]:
                    continue        # it reads what an uninterrupted run gives it
                problems.append((s.key, damaged, mutator.key))
    return problems


PYTHON_VERSION_FILE = REPO_ROOT / "mise.toml"


def declared_python_version() -> str:
    """The `major.minor` that this repository pins for python, or `""` when unstated.

    The file is absent in a replication package, where a step archive is
    unpacked on its own without the repository root, and a missing file is not
    a failure. The steps then run under the interpreter that the replicator
    has. A file that does not parse is treated the same way rather than
    stopping a run over the syntax of a configuration file.
    """
    try:
        tools = tomllib.loads(
            PYTHON_VERSION_FILE.read_text(encoding="utf-8")).get("tools", {})
    except (OSError, tomllib.TOMLDecodeError, AttributeError):
        return ""
    python = tools.get("python")
    if isinstance(python, dict):
        python = python.get("version")
    if not python:
        return ""
    # major.minor, because mise accepts a patch level and an interpreter
    # reports two parts.
    return ".".join(str(python).strip().split(".")[:2])


def interpreter_version(executable: str) -> str:
    """`major.minor` for `executable`, or `""` when the interpreter could not report it.

    `""` also covers an interpreter that could not be started, for example because `fork()` failed.
    The error would otherwise propagate out of the pre-checks into the handler written for
    the state file. """
    try:
        probe = subprocess.run(
            [executable, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True)
    except OSError:
        return ""
    return probe.stdout.strip() if probe.returncode == 0 else ""


def _count(value) -> str:
    """`value` with thousands separators when it is an integer, else as it is."""
    return f"{value:,}" if isinstance(value, int) else str(value)


def series(names) -> str:
    """The names as a sentence lists them: `a`, `a and b` or `a, b and c`."""
    names = [str(name) for name in names]
    return " and ".join(names) if len(names) < 3 else f"{', '.join(names[:-1])} and {names[-1]}"


def _existing(path: Path) -> Path:
    """`path`, or the nearest of its parents that exists."""
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def missing_reads(steps: list, pending: set) -> list:
    """`(step key, path)` for each path that a pending step opens and that is neither on disk nor
    written by a pending step before it is opened. A cache that is neither would be read as
    empty, and the detection step would publish blank Git columns while exiting 0. The paths are
    compared resolved, because `produces` and `reads` are each relative to their own step's data
    directory."""
    produced = {(data_dir(s) / p).resolve()
                for s in steps if s.key in pending for p in s.produces}
    missing = []
    for step in steps:
        if step.key not in pending:
            continue
        for rel in step.reads:
            # Normalized first, because the kernel cannot resolve `analysis/../collection`
            # while `analysis/` does not exist yet, before its stage has run.
            here = Path(os.path.normpath(data_dir(step) / rel))
            if here.resolve() not in produced and not here.exists():
                missing.append((step.key, rel))
    return missing


def read_paths(step: Step) -> list:
    """The paths, relative to a step's data directory, that decide the record of a step that is
    recorded by what it reads: each alternative of its `inputs`, its `reads`, and for
    `repository_cloning` the list of repositories that it collects (`collection_list`), which
    `collection_inputs` hashes."""
    paths = [alternative for rel in step.inputs for alternative in rel.split("|")]
    paths += list(step.reads)
    if step.key == 'repository_cloning':
        paths.append(collection_list(step))
    return paths


def _covers(written: Path, read: Path) -> bool:
    """Whether writing `written` can change what is read at `read`: the same path, one under the
    other, or a glob `read` that matches `written` or a path under it."""
    if any(char in str(read) for char in "*?["):
        return (fnmatch.fnmatchcase(str(written), str(read))
                or any(fnmatch.fnmatchcase(str(parent), str(read)) for parent in written.parents))
    return written == read or read in written.parents or written in read.parents


def may_become_pending(steps: list, pending: set) -> set:
    """The steps that are recorded as done and that this run can still run, because an earlier
    step of the run can change what decides their record.

    When the run reaches a step recorded by its inputs, or a step recorded by its output that names
    `inputs_of`, it computes the step's record again (`changed_during_run`). Such a step can
    therefore run when a pending step before it writes a path that it reads (`read_paths`). For
    example, the labeling runs after the GHS download of a monthly run has named a new retrieval and
    the filter has written a new sampling frame from it, and the cloning step runs after the
    labeling has given the sample new labels. Every step computed from such a step can run too, and
    the set grows until nothing is added. The pre-checks verify what these steps need in
    addition to what the pending steps need. A run that found a refused API key of the labeling
    or a missing GitHub access token only when it reached the step would have run the steps
    before it for nothing. """
    def resolved(step: Step, rel: str) -> Path:
        return Path(os.path.normpath(data_dir(step) / rel))

    wider = set(pending)
    added = True
    while added:
        added = False
        written = []
        for step in steps:
            if step.key in wider:
                written += [resolved(step, rel) for rel in step.produces]
                continue
            rechecked = step.completion == COMPLETION_INPUTS or (
                step.completion == COMPLETION_OUTPUT and step.inputs_of is not None)
            reads = [resolved(step, rel) for rel in read_paths(step)] if rechecked else []
            if set(step.consumes) & wider or any(_covers(path, read)
                                                for path in written for read in reads):
                wider.add(step.key)
                written += [resolved(step, rel) for rel in step.produces]
                added = True
    return wider - set(pending)


def free_space_minimum(pending: set, min_free_gb: "float | None") -> float:
    """The free space in GiB that the pre-checks require of each volume that they measure:
    the value of `--min-free-gb` when the command line gives one, MIN_FREE_DISK_GB when a cloning
    step is pending, and otherwise the room that this minimum leaves beside the clones in flight
    (CLONES_IN_FLIGHT_GB)."""
    if min_free_gb is not None:
        return min_free_gb
    if pending & CLONING_STEPS:
        return MIN_FREE_DISK_GB
    return max(0, MIN_FREE_DISK_GB - CLONES_IN_FLIGHT_GB)


def labeling_keys_accepted(steps: list, pending: set, credentials_path=None) -> bool:
    """Check the API key of the model's service for each labeling step in `pending`, the steps
    that the run runs or can run, and return False when the service refuses it in a way that
    would refuse every request of the step. For an OpenAI model, send one minimal request with
    the first OpenAI API key and the model. For an OpenRouter model, whose id names its author,
    read the credit of the first OpenRouter API key (`openrouter_accepts`).

    The check of the environment logs a key that is not configured, and this check sends
    nothing without one. An error that can pass, such as a server error, a rate limit or a
    timeout, is a warning, because the step retries its own requests and a check that cannot
    reach the service shows nothing about the key.
    """
    labeling = sorted(pending & LABELING_STEPS)
    by_key = {s.key: s for s in steps}
    ok = True
    for key in labeling:
        # A stop sends no request, because the run ends before its first step whatever the
        # pre-checks find.
        if _STOPPING:
            break
        if key not in by_key:
            continue
        model = labeling_model(by_key[key])
        if not model:
            logs.warning("pre_checks", f"Neither the arguments nor the script of {key} "
                                       f"name a model. Checked only that its API key is "
                                       f"configured.")
            continue
        if "/" in model:
            ok = openrouter_accepts(key, model, credentials_path) and ok
            continue
        keys = credentials.openai_keys(credentials_path, os.environ)
        if not keys:
            continue
        try:
            openai_response(keys[0].value, model)
        except urllib.error.HTTPError as error:
            refusal, spent = openai_refusal(error)
            # The message names the HTTP status and the model, never the key or the body.
            if refusal:
                logs.error("pre_checks", f"{refusal} (HTTP {error.code}, model {model}). "
                                         f"Every request of {key} fails the same way. "
                                         f"Correct the key or the model before the next "
                                         f"Start.")
                ok = False
            elif spent:
                logs.warning("pre_checks", f"{spent} (HTTP {error.code}, model {model}). "
                                           f"{key} waits for credit when it reaches OpenAI. "
                                           f"To continue then, raise the quota or the billing "
                                           f"limit of the OpenAI project.")
            else:
                logs.warning("pre_checks", f"OpenAI returned HTTP {error.code} to the "
                                           f"request with model {model} (required by "
                                           f"{key}). Continuing, because the error can "
                                           f"pass.")
        except (OSError, http.client.HTTPException) as error:
            logs.warning("pre_checks", f"OpenAI could not be reached "
                                       f"({type(error).__name__}). Continuing without a "
                                       f"check of the OpenAI API key (required by {key}).")
        else:
            logs.info("pre_checks", f"OpenAI accepted a request with the first OpenAI "
                                    f"API key and model {model} (required by {key}).")
    return ok


def pre_checks(steps: list, pending: set, no_github: bool = False,
               scratch_dir=None, min_free_gb: float = None,
               credentials_path=None) -> bool:
    """Check the prerequisites of the steps about to run, before any of them
    starts. Logs one ERROR line per problem and returns False if any check
    failed, so that main() can stop before any step runs. Otherwise a missing
    `7zz` would only be found after every earlier step had run.

    `no_github` is for a run with clones that do not reach github.com, such as a run
    against local mirrors through a `GIT_CONFIG_*` redirect. The pre-checks then skip the
    GitHub access token and the request. A GitHub access token that is set is an error: with
    a GitHub access token, the clone URL names the user `x-access-token`, and the redirect of
    `https://github.com/` does not match a URL with a user. `scratch_dir` is the directory
    into which the cloning steps clone. The pre-checks measure the free space there, and on
    the volumes that contain the data root and the stage directory when they differ, against
    `free_space_minimum`. A pending labeling step gets a check of the API key of its model's
    service (`labeling_keys_accepted`).

    What a step needs to run is checked for the pending steps and for the steps that an earlier
    step of the run can make pending (`may_become_pending`). The inputs on disk are checked for
    the pending steps alone, because a step that becomes pending reads what the steps before it
    write."""
    ok = True
    # The credentials file, read once, and the credentials of the environment. Their problems
    # are logged whether or not a pending step needs a credential, because the operator would not
    # expect the run to ignore a file or a variable that the operator wrote.
    _, file_problems = credentials.read(credentials_path)
    for problem in file_problems + credentials.environment_problems(os.environ):
        logs.warning("pre_checks", f"{problem.rstrip('.')}.")
    conditional = may_become_pending(steps, pending)
    if conditional:
        listed = series(s.key for s in steps if s.key in conditional)
        logs.info("pre_checks", f"Also checking the steps that an earlier step of the run "
                                f"can make pending ({listed}).")
    needed = set(pending) | conditional
    # Where the credentials that the environment does not set would be, for the messages about a
    # missing credential.
    where = (f"{credentials_path} has none" if credentials_path
             else "no credentials file was given")
    scratch = Path(scratch_dir) if scratch_dir is not None else DEFAULT_SCRATCH_DIR
    minimum_free_gb = free_space_minimum(needed, min_free_gb)
    by_key = {s.key: s for s in steps}

    # Select a validated GitHub access token before any clone. Without one, git clones
    # anonymously and every clone fails on the rate limit. `finished_repositories.jsonl` then
    # records each repository as finished, and the run exits 0 with an empty sample. Like the
    # check of the binaries, the check runs only for cloning steps that are pending or can
    # become pending, and an offline regeneration never reaches it.
    cloning = needed & CLONING_STEPS
    if cloning:
        token = os.environ.get("GITHUB_TOKEN")
        needed_by = series(sorted(cloning))
        if no_github:
            if token:
                logs.error("pre_checks", f"GITHUB_TOKEN is set under --no-github. The "
                                         f"redirect of https://github.com/ to the local "
                                         f"Git mirrors does not match the clone URL with "
                                         f"the user x-access-token that a GitHub access "
                                         f"token adds. Unset GITHUB_TOKEN.")
                ok = False
        else:
            # The environment first, then the credentials file.
            pool = credentials.github_tokens(credentials_path, os.environ)
            tokens = [credential.value for credential in pool]
            if not tokens:
                logs.error("pre_checks", f"No GitHub access token is configured (required "
                                         f"by {needed_by}). GITHUB_TOKENS and "
                                         f"GITHUB_TOKEN are unset, and {where}. Add a "
                                         f"GitHub access token, or pass --no-github for a "
                                         f"run against local Git mirrors.")
                ok = False
            # GitHub can reject valid GitHub access tokens for a while. A check in which GitHub
            # rejects every one of them is therefore repeated after each wait of
            # GITHUB_REJECTED_WAITS before the Start is refused, because nobody would start an
            # unattended run again.
            for wait in (None, *GITHUB_REJECTED_WAITS) if tokens else ():
                if _STOPPING:
                    logs.info("pre_checks", "A stop arrived during the check of the "
                                            "GitHub access tokens.")
                    ok = False
                    break
                if wait is not None:
                    logs.info("pre_checks", f"GitHub rejected each of the "
                                            f"{len(tokens):,} configured GitHub access "
                                            f"tokens with HTTP {GITHUB_REJECTED_STATUS}. "
                                            f"Checking them again in {logs.hms(wait)}.")
                    if _wait_unless_stopped(wait):
                        logs.info("pre_checks", "A stop arrived during the wait before "
                                                "the next check of the GitHub access "
                                                "tokens.")
                        ok = False
                        break
                if _choose_github_token(tokens):
                    break
                if _STOPPING:
                    # A stop during the last check also leaves the loop without a verdict, and
                    # the refusal below would read as a verdict.
                    logs.info("pre_checks", "A stop arrived during the check of the "
                                            "GitHub access tokens.")
                    ok = False
                    break
            else:
                if tokens:
                    logs.error("pre_checks", f"None of the {len(tokens):,} configured "
                                             f"GitHub access tokens passed validation. "
                                             f"GitHub rejected each with HTTP "
                                             f"{GITHUB_REJECTED_STATUS} in "
                                             f"{len(GITHUB_REJECTED_WAITS) + 1:,} checks "
                                             f"over {logs.hms(sum(GITHUB_REJECTED_WAITS))} "
                                             f"(required by {needed_by}).")
                    ok = False

    # The map BINARY_REQUIREMENTS for the collection's steps, and the binaries that each step
    # declares for itself.
    requirements = {binary: set(keys) for binary, keys in BINARY_REQUIREMENTS.items()}
    for step in steps:
        for binary in step.binaries:
            requirements.setdefault(binary, set()).add(step.key)
    for binary, needed_by in sorted(requirements.items()):
        blocking = needed_by & needed
        if blocking and shutil.which(binary) is None:
            logs.error("pre_checks", f"'{binary}' not found on PATH "
                                     f"(required by {series(sorted(blocking))}).")
            ok = False

    # What a step's interpreter has to import, and what its environment has to set. A missing
    # package or variable is found here rather than after the steps before it have run.
    for key in sorted(needed):
        step = by_key.get(key)
        if step is None:
            continue
        # The module of a step that runs a package module has to be importable by its
        # interpreter too, and a virtual environment without the package fails here rather
        # than at the step.
        imports = list(step.imports) + ([step_module(step)] if step_module(step) else [])
        if imports:
            try:
                check = subprocess.run([step.interpreter, "-c",
                                        "import " + ", ".join(imports)],
                                       capture_output=True, text=True)
            except OSError as error:
                logs.error("pre_checks", f"Could not start '{step.interpreter}' to check "
                                         f"the imports of {key} ({error}).")
                ok = False
                continue
            if check.returncode != 0:
                # The last line of Python's error, such as the ModuleNotFoundError that names
                # the missing module.
                detail = check.stderr.strip().splitlines()[-1] if check.stderr.strip() else ""
                logs.error("pre_checks", f"'{step.interpreter}' cannot import "
                                         f"{series(imports)} (required by {key}). The "
                                         f"import failed"
                                         f"{f' ({detail})' if detail else ''}.")
                ok = False
        for entry in step.env:
            names = entry.split("|")
            # A credential that the environment does not set may be in the credentials file.
            services = {credentials.SERVICE_OF[name] for name in names
                        if name in credentials.SERVICE_OF}
            if not any(os.environ.get(name) for name in names) and not any(
                    credentials.of_service(service, credentials_path, {}) for service in services):
                unset = f"{series(names)} {'is' if len(names) == 1 else 'are'} unset"
                # The name of the credential without its article, such as "OpenAI API key".
                kinds = series(credentials.NAMES[service].split(" ", 1)[1]
                               for service in sorted(services))
                logs.error("pre_checks", f"No {kinds} is configured (required by {key}). "
                                         f"{unset}, and {where}." if services
                                         else f"{unset} (required by {key}).")
                ok = False

    if not labeling_keys_accepted(steps, needed, credentials_path):
        ok = False

    # Every interpreter that is about to run a step is checked against the version that the
    # repository pins. The step interpreter is not always this process: VENV_PYTHON is
    # DATA_COLLECTION_PYTHON when that is set, and a step declares its own interpreter, such as
    # the `python3` on PATH. A run could otherwise mix three Python versions in one pipeline
    # without naming any of them.
    # The check is here rather than in the step, because a step that runs under the wrong
    # version writes its output before any error.
    wanted = declared_python_version()
    if wanted:
        for executable in sorted({sys.executable} | {s.interpreter for s in steps
                                                     if s.key in needed}):
            found = interpreter_version(executable)
            if found != wanted:
                version = (f"is Python {found}" if found
                           else "did not report its Python version")
                logs.error("pre_checks", f"'{executable}' {version}, but "
                                         f"{PYTHON_VERSION_FILE.name} pins Python "
                                         f"{wanted}. Run under Python {wanted}, or set "
                                         f"{STEP_PYTHON_ENV_VAR} to a Python {wanted} "
                                         f"interpreter.")
                ok = False

    if needed & LINGUA_STEPS:
        # A warning, not an error. The step runs without lingua and records `n/a` for every
        # language. Failing the run would block an offline regeneration of everything else
        # over a package that the other steps never load. The cost is the language of every
        # row of the run, because the detection step reads the language of every row on every
        # run.
        try:
            check = subprocess.run([VENV_PYTHON, "-c", "import lingua"],
                                   capture_output=True)
        except OSError as error:
            logs.error("pre_checks", f"Could not start '{VENV_PYTHON}' to check whether "
                                     f"it imports lingua ({error}).")
            check = None
            ok = False
        if check is not None and check.returncode != 0:
            wanted = series(sorted(needed & LINGUA_STEPS))
            logs.warning("pre_checks", f"'{VENV_PYTHON}' cannot import lingua (required "
                                       f"by {wanted}). The language of every "
                                       f"configuration artifact is recorded as 'n/a'. "
                                       f"Install lingua, or set {STEP_PYTHON_ENV_VAR} to "
                                       f"an interpreter that has it.")

    for step_key, rel in missing_reads(steps, pending):
        logs.error("pre_checks", f"Input {rel} not found (required by {step_key}).")
        ok = False

    if 'repository_cloning' in pending:
        repo_csv = collection_list(by_key['repository_cloning'])
        # The check tests whether the file will be there when the cloning step opens it, not
        # whether it is on disk now. A pending step that declares this path writes it before
        # the cloning step runs, and `engineered_project_selection` declares exactly this
        # path. Over an empty data root, an unconditional check would refuse the run that the
        # step exists to make possible. A --repo-csv that no pending step produces, which is
        # every value except the default, is still checked.
        produced = {(data_dir(s) / p).resolve()
                    for s in steps if s.key in pending for p in s.produces}
        wanted = data_dir(by_key['repository_cloning']) / repo_csv
        if wanted.resolve() not in produced and not wanted.is_file():
            logs.error("pre_checks", f"{repo_csv}, the list of repositories to collect, "
                                     f"is not on disk (required by repository_cloning).")
            ok = False

    # The volumes that the outputs are written to, and, when a step is about to clone, the
    # volume that the clones are written to. Each volume is measured once: in a container
    # these are different mounts, and on one disk they are the same volume. Each is measured
    # at its nearest existing directory, so that a new data root, which has none of the
    # directories yet, is measured too.
    volumes = [("the data root", _existing(DATA_ROOT)),
               ("the stage directory", _existing(STAGE_DIR))]
    if needed & CLONING_STEPS:
        try:
            scratch.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logs.error("pre_checks", f"Could not create the scratch directory "
                                     f"{scratch} ({e}).")
            ok = False
        else:
            volumes.append(("the scratch directory", scratch))
    devices = set()
    for label, path in volumes:
        try:
            device = os.stat(path).st_dev
        except OSError:
            continue
        if device in devices:
            continue
        devices.add(device)
        free_gb = shutil.disk_usage(path).free / (1024 ** 3)
        if free_gb < minimum_free_gb:
            logs.error("pre_checks", f"Only {free_gb:.1f} GiB free on disk at {path}, "
                                     f"{label}, under the minimum of {minimum_free_gb} "
                                     f"GiB.")
            ok = False

    return ok


def parse_step_arg(raw: str) -> tuple:
    """Parse one --step-arg value, 'name:raw tokens', into (name, [tokens])."""
    if ":" not in raw:
        logs.error("runner", f"--step-arg must be NAME:\"args\", not {raw!r}.")
        sys.exit(1)
    name, _, rest = raw.partition(":")
    return name, shlex.split(rest)


def apply_step_overrides(steps: list, overrides: list) -> list:
    """Return a new list of steps with the tokens of each --step-arg appended
    to the matching step's argv. Exits with an error on an unknown step name,
    because a typo ignored without a message would show only later, as a step
    failure with an unclear cause."""
    names = step_names(steps)
    extra_by_name = {}
    for raw in overrides:
        name, tokens = parse_step_arg(raw)
        if name not in names:
            logs.error("runner", f"Unknown step '{name}' in --step-arg. The steps of this run "
                                 f"are {series(names)}.")
            sys.exit(1)
        extra_by_name.setdefault(name, []).extend(tokens)

    return [replace(s, args=s.args + extra_by_name.get(s.key, []))
            for s in steps]


def apply_scratch_dir(steps: list, scratch_dir) -> list:
    """Point every step that clones at one scratch directory, through its --clone-dir."""
    return [replace(s, args=s.args + ["--clone-dir", str(scratch_dir)])
            if s.key in CLONING_STEPS else s
            for s in steps]


def apply_min_free_gb(steps: list, min_free_gb: "float | None") -> list:
    """Give the cloning step the minimum that the pre-checks require, so that both use one
    number."""
    if min_free_gb is None:
        return steps
    return [replace(s, args=s.args + ["--min-free-gb", f"{min_free_gb:g}"])
            if s.key == 'repository_cloning' else s
            for s in steps]


def apply_repo_csv(steps: list, repo_csv: "str | None",
                   step_key: str = 'repository_cloning') -> list:
    """Point the cloning step at a specific input CSV.

    A shorthand for the override that --step-arg applies, and the pre-checks,
    which reads --repo-csv from the cloning step's argv, works unchanged. It is
    a flag of its own because the list is the pipeline's input, not a setting
    of one step: the list of the sample that `engineered_project_selection`
    writes into the classification stage's directory as
    repos_annotated_included.csv.
    """
    if repo_csv is None:
        return steps
    return [replace(s, args=s.args + ["--repo-csv", repo_csv])
            if s.key == step_key else s
            for s in steps]


def from_names(from_name) -> list:
    """The names that `--from` gave, one or several. A monthly run gives two, the download and
    the cloning step, which no edge of the list of steps joins."""
    if not from_name:
        return []
    return [from_name] if isinstance(from_name, str) else list(from_name)


def steps_to_redo(steps: list, from_name) -> set:
    """The steps that `--from NAME` makes this run redo: NAME and everything
    computed from it, for each NAME given.

    Everything computed from it, not everything after it. The two are
    different sets, and the difference is work that the operator did not
    request.
    """
    redo = set()
    for name in from_names(from_name):
        _require_known_name(steps, name)
        redo |= set(invalidated_by(steps, name))
    return redo


def drop_completed_from(state: dict, steps: list, from_name: str) -> set:
    """Remove the steps of `steps_to_redo` from the completion records, so that a later run
    redoes them too.

    Separate from `steps_to_redo`, because the two happen at different moments. Deciding what
    this run will redo changes nothing. Removing the steps from the records discards the
    evidence that they ran, and the removal must wait until the run is about to run them. If
    they were removed earlier, pre-checks that refuse the run, for example over a missing
    `7zz`, would leave records without every step that `--from` names, and the next run would
    redo all of them.
    """
    dropped = steps_to_redo(steps, from_name)
    completed = state.setdefault("completed", {})
    for name in dropped:
        completed.pop(name, None)
    # A new snapshot of the cloning step is a new collection, whatever its list. The last
    # collection that ended is marked as replaced, because the step would otherwise continue that
    # collection over its list or over a list that lacks repositories that GitHub no longer
    # serves (`resume.open_collection`) and publish the heads of the earlier snapshot. The mark
    # also keeps the labeling from counting that collection as a collection over a sample of a
    # GHS retrieval that the download reused (`collection_over` in the labeling). The mark is
    # made here rather than by an argument of the step, so that a request survives a stop
    # before the step starts. A collection in progress that never ended is still resumed,
    # because it has not taken its snapshot yet. A continued collection that a stop, a kill, a
    # power loss or any exit other than 0 left in progress is the earlier snapshot, and it is
    # marked as replaced too (`resume.CONTINUED`).
    for step in steps:
        if step.key == 'repository_cloning' and step.key in dropped:
            finished_path = data_dir(step) / resume.FINISHED_NAME
            try:
                resume.forget_ended_collection(finished_path)
            except (OSError, UnicodeDecodeError) as e:
                raise SystemExit(f"Could not mark the last collection that ended as replaced in "
                                 f"{resume.ended_path(finished_path)}, which --from {step.key} "
                                 f"does to take a new snapshot ({e}). Make "
                                 f"{finished_path.name} readable and the directory writable, "
                                 f"and start the run again.") from e
    # A snapshot that --from requests again is not the snapshot on disk. The step is listed as
    # unfinished until it has run, so that a stop before it starts does not record the old
    # snapshot as complete.
    for step in steps:
        if step.irreproducible and step.key in dropped:
            set_unfinished(state, step.key, True)
    return dropped


def counted_entries(path: Path) -> list:
    """The entries of a directory, counted as the manifest counts them.

    This function and `manifest_lines` have to count the same entries of a
    tree, or the count logged for the operator and the fingerprint recorded
    beside it describe different directories. After a Finder visit, the
    fingerprint would be unchanged while the logged count showed one more
    entry, and a directory that contains only a `.DS_Store` or an `@eaDir` would
    pass the check that refuses an empty directory.
    """
    return [name for name in os.listdir(path) if not is_discarded(path / name)]


def _describe_output(step: Step, rel: str) -> str:
    """`rel`, with the entry count when it names a directory.

    The operator compares the count with the number that the README documents.
    A partly restored tree shows a smaller count.
    """
    path = resolve_produced(step, rel)
    if entry_kind(path, follow=True) == DIRECTORY:
        return f"{rel} ({len(counted_entries(path)):,} entries)"
    return rel


def transient_failures(clone_failed_csv: Path) -> int:
    """How many rows of a clone_failed.csv record an outcome that a run retries.

    An absent list means no failures, but a list that is on disk and cannot be read does not.
    Reading every OSError as zero would let a list that the process cannot open pass the
    ceiling check and record the step, the outcome that CLONE_FAILURE_SHARE exists to prevent.
    `mark_complete` already refuses on an unreadable output, and the function raises the error
    to it.
    """
    try:
        with open(clone_failed_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
    except FileNotFoundError:
        return 0
    return sum(1 for row in rows[1:] if row and row[-1] in TRANSIENT_OUTCOMES)


# The directory where the cloning step keeps its per-repository commit records, relative to
# the step's data directory, as `repository_cloning.COMMITS_DIR_NAME` names it. `mark_complete`
# reads it as the step does.
COMMITS_DIR_NAME = "collect_commits"


def collection_retries(root: Path, names: list) -> tuple:
    """What a collection run would still do for the repositories of its collected list.

    Returns the number of lines of `finished_repositories.jsonl` that are marked for retry or
    that were taken over while git returned no head commit, and the repositories with a
    commit history that was read but no commit log kept. Both are read the way the cloning
    step reads them before it exits. The step counts those lines among the clone failures that
    the ceiling limits, and exits 4 on a repository without its commit log.
    """
    wanted = set(names)
    finished = resume.read_finished(root / resume.FINISHED_NAME)
    redo = sum(1 for repo in wanted
               if repo in finished and (finished[repo][4] or finished[repo][8]))
    commits_dir = root / COMMITS_DIR_NAME
    missing = (sorted(repo for repo, _csv, record in commits.readable_shards(commits_dir, wanted)
                      if commits.required_log_missing(commits_dir, repo, record))
               if commits_dir.is_dir() else [])
    return redo, missing


def mark_complete(steps: list, name: str, started_at: float, argv: list = None) -> int:
    """Record a step as complete from the output already on disk.

    Meant for `repository_cloning` after its output has been restored from the
    archives. The tree is what the cloning step wrote, and no run will produce it
    again. Without a record, the next run clones every repository in the input
    list over it.

    Only a step recorded by its output can be recorded this way. For every
    other step, the recorded value is a hash of a script, or of a script and
    inputs, which does not describe the files on disk. Marking such a step
    would accept whatever output is there and permanently disable the check
    for changed code.

    Refuses when any declared output is missing, and when a declared directory
    is empty.
    """
    _require_known_name(steps, name)
    step = next(s for s in steps if s.key == name)
    if step.completion != COMPLETION_OUTPUT:
        markable = [s.key for s in steps if s.completion == COMPLETION_OUTPUT]
        inputs = " and inputs" if step.completion == COMPLETION_INPUTS else ""
        logs.error("runner", f"--mark-complete cannot record step '{name}', which is "
                             f"recorded by the hash of its script{inputs}, not by its output. "
                             + (f"{series(markable)} "
                                f"{'is the step' if len(markable) == 1 else 'are the steps'} of "
                                f"this run recorded by "
                                f"{'its' if len(markable) == 1 else 'their'} output."
                                if markable else "No step of this run is recorded by its "
                                                 "output."))
        return 1
    # `resolve_produced` makes a link that points at nothing a missing output rather than a
    # present output, and a dangling link is never recorded as a finished collection.
    #
    # Every read of the declared output is in this try, and all reads happen before anything
    # is written. `entry_kind` and `listdir` re-raise every errno except four. A sample on a
    # mount that is present and not responding would otherwise reach main's handler, which
    # logs a failure to write the state file and names the sample as the file that it could
    # not write.
    #
    # The fingerprint and the description are inside the try too. The fingerprint reads the
    # whole tree, and an unreadable directory inside an otherwise readable sample would make
    # `output_fingerprint` exit with the message written for a resume, which states that the
    # step would be run again over output that is already there. The description has to fail
    # before the state file is written. Otherwise an error there would leave
    # `repository_cloning` recorded as complete and log a failure to write the record that had
    # just been written.
    pairs = None
    try:
        missing = [rel for rel in step.produces
                   if entry_kind(resolve_produced(step, rel)) == ABSENT]
        # A zero-length file as well as an empty directory, such as the
        # `ghs_complete.json` that an extraction leaves when it fails partway. Recorded, either
        # would make every later run skip the step and build from nothing.
        empty = [] if missing else [
            rel for rel in step.produces
            if rel not in step.may_be_empty and _is_empty_output(resolve_produced(step, rel))]
        if not (missing or empty):
            if step.inputs_of is not None:
                pairs = [[label, value] for label, value in step.inputs_of(step)]
            fingerprint = step_fingerprint(step, inputs_part(pairs) if pairs is not None
                                           else None)
            described = ", ".join(_describe_output(step, rel) for rel in step.produces)
            if not fingerprint:
                logs.error("runner", f"Step '{name}' cannot be marked complete, because an "
                                     f"input that it is recorded by is not on disk "
                                     f"({', '.join(input_labels(step))}).")
                return 1
    except (OSError, UnreadableOutput) as e:
        logs.error("runner", f"Could not read an output of step '{name}' ({e}). Did not "
                             f"record the step as complete.")
        return 1
    if missing:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because its declared "
                             f"{'output' if len(missing) == 1 else 'outputs'} {series(missing)} "
                             f"{'is' if len(missing) == 1 else 'are'} not under "
                             f"{data_dir(step)}.")
        return 1
    if empty:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                             f"{series(empty)} {'is' if len(empty) == 1 else 'are'} empty. Run "
                             f"the step instead.")
        return 1
    try:
        transient = transient_failures(
            data_dir(step) / "data" / "collected" / "clone_failed.csv")
    except OSError as e:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                             f"data/collected/clone_failed.csv could not be read ({e}).")
        return 1
    try:
        with open(data_dir(step) / "data" / "collected" / "repos.csv", newline="",
                  encoding="utf-8") as f:
            names = [row["repo_name"] for row in csv.DictReader(f) if row.get("repo_name")]
    except FileNotFoundError:
        names = []
    except OSError as e:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                             f"data/collected/repos.csv could not be read ({e}).")
        return 1
    listed = len(names)
    try:
        redo, missing_logs = collection_retries(data_dir(step), names)
    except OSError as e:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                             f"{resume.FINISHED_NAME} or the per-repository commit records under "
                             f"{COMMITS_DIR_NAME}/ could not be read ({e}).")
        return 1
    if missing_logs:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                             f"{len(missing_logs):,} repositories have no commit log under "
                             f"{COMMITS_DIR_NAME}/ although their commit history was read (the "
                             f"first is {missing_logs[0]}). Restore the commit logs from the "
                             f"handover, or run the step to collect these repositories again.")
        return 1
    ceiling = clone_failure_ceiling(listed)
    if transient + redo > ceiling:
        logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                             f"{transient:,} rows of data/collected/clone_failed.csv are "
                             f"{' or '.join(TRANSIENT_OUTCOMES)} and {redo:,} lines of "
                             f"{resume.FINISHED_NAME} are marked for retry. That exceeds the "
                             f"ceiling of {ceiling:,} for {listed:,} repositories, above which "
                             f"the step itself exits 4. Run the step to retry them.")
        return 1
    if name == 'repository_cloning':
        # A collection that stopped before it reached every repository of its list, for
        # example at a low disk or after a failed connectivity probe, wrote its collected list
        # for the repositories that it reached. Recorded as complete, that partial collection
        # would be published as the sample.
        from agentcontext.steps import repository_cloning
        listed_path = data_dir(step) / collection_list(step)
        try:
            wanted = repository_cloning.parse_repo_csv(
                listed_path, _flag_value(step.args, "--repo-col", "repo_name"))
            reached = resume.read_finished(data_dir(step) / resume.FINISHED_NAME)
        except OSError as e:
            logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                                 f"{collection_list(step)}, the list of repositories that it "
                                 f"collects, or {resume.FINISHED_NAME} could not be read ({e}).")
            return 1
        except SystemExit:
            # `parse_repo_csv` logged the column that the list lacks.
            logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                                 f"{collection_list(step)}, the list of repositories that it "
                                 f"collects, names no repository column.")
            return 1
        # A collection that ends closes its record, and a record over this list therefore names
        # a collection that stopped or was killed before it ended. Its lines cannot show what it
        # still had to do: the list keeps the lines of every earlier collection, and the line of
        # a repository that it was retrying names the collection although the repository's
        # files are gone. When the step runs, it finishes the collection and takes over what the
        # collection reached. A collection over this list with repositories that GitHub no longer
        # serves is the same collection, which the step continues (`resume.open_collection`).
        progress = resume.collection_in_progress(data_dir(step) / resume.FINISHED_NAME, wanted,
                                                 repository_cloning.labeling_gone(listed_path))
        if progress is not None:
            logs.error("runner", f"Step '{name}' cannot be marked complete, because the "
                                 f"collection {progress['collection']} over "
                                 f"{collection_list(step)} has not ended "
                                 f"({resume.collection_path(data_dir(step) / resume.FINISHED_NAME).name}). "
                                 f"Run the step to finish the collection and take over the "
                                 f"repositories that it reached.")
            return 1
        unreached = [repo for repo in wanted if repo not in reached]
        if unreached:
            logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                                 f"{len(unreached):,} of the {len(wanted):,} repositories of "
                                 f"{collection_list(step)} have no line in "
                                 f"{resume.FINISHED_NAME} (the first is {unreached[0]}). The "
                                 f"collection stopped before it reached them. Run the step to "
                                 f"collect them.")
            return 1
        # The tree is recorded as the result of a finished collection, and the record of a
        # collection in progress, which can only be over another list, is removed here. Otherwise
        # it would make the next run over that list resume it and take over every repository
        # that it names without querying the head commit. The record of the last collection that
        # ended is removed too, because nothing shows that the restored tree is that
        # collection's, and the next run over its list without repositories that GitHub no
        # longer serves would take over its repositories without querying their heads. The
        # records are removed before the step is recorded, so that a failure leaves the step
        # unrecorded rather than recorded beside a collection still in progress.
        finished_path = data_dir(step) / resume.FINISHED_NAME
        try:
            resume.forget_collections(finished_path)
        except OSError as e:
            logs.error("runner", f"Step '{name}' cannot be marked complete, because "
                                 f"{resume.collection_path(finished_path).name} or "
                                 f"{resume.ended_path(finished_path).name}, the records of the "
                                 f"collection in progress and of the last collection that "
                                 f"ended, could not be removed ({e}).")
            return 1
    state = read_state()
    record_completion(state, name, fingerprint, "marked_complete", inputs=pairs)
    # The operator records what an unfinished run left, as the warning of
    # `adopt_irreproducible` offers.
    set_unfinished(state, name, False)
    # A kill after the step exited 0 can leave an EXITED record of the same step, with the
    # inputs of that run. The next start would record the step from it over this record, and
    # run the step again when those inputs differ from the current inputs.
    exited = state.get(EXITED)
    if isinstance(exited, dict) and exited.get("step") == name:
        del state[EXITED]
    # write_execution_state rather than write_state, because this is the one completion that no
    # run produced. The pipeline execution state then names the machine that recorded it, the
    # time, the checkout and the command.
    write_execution_state(state, [], "marked_complete", started_at, sys.argv if argv is None else argv)
    logs.info("runner", f"Step '{name}' recorded as complete in {STATE_PATH.name}, "
                        f"from {described} (fingerprint {fingerprint}).")
    return 0


def _is_empty_output(path: Path) -> bool:
    """Whether a declared output is there but empty: an empty directory, or a
    zero-length file. Both make every later run skip the step and build from nothing."""
    kind = entry_kind(path, follow=True)
    if kind == DIRECTORY:
        return not counted_entries(path)
    if kind == FILE:
        try:
            return path.stat().st_size == 0
        except OSError:
            return False
    return False


def _output_signature(path: Path) -> tuple:
    """The current signature of a declared output.

    ``()`` when there is nothing there. The signature of a directory records
    only that it is a directory. `repository_cloning` resumes from
    `finished_repositories.jsonl`, and a run that collects nothing again leaves
    `data/collected/` correct without changing the directory entry. Listing the
    tree to find out more costs more than the check gains. The signature of a
    regular file contains the fields that change when the file is rewritten.
    Every step here rewrites its files in place. When the bytes stay the same,
    only the mtime changes, and the check depends on it. Both platforms on
    which the pipeline runs record the mtime in nanoseconds.
    """
    try:
        if entry_kind(path, follow=True) == DIRECTORY:
            return ("dir",)
        st = path.stat()
    except OSError:
        return ()
    return ("file", st.st_mtime_ns, st.st_size, st.st_ino)


def sync_outputs(step: Step) -> None:
    """Write the declared outputs of `step` and its report to disk: each file, and each
    directory with every file and directory below it (`caches.sync_tree`).

    A record of the step stands for these paths. A sync of the file system that holds them
    would also wait for every write that is pending on the volume, the writes of other programs
    included, and the runner could not record the step while one of them had not reached the
    disk. Raises `UnreadableOutput` for an output that cannot be opened or synced."""
    base = data_dir(step)
    for rel in (*step.produces, *([step.report] if step.report else [])):
        try:
            caches.sync_tree(base / rel)
        except OSError as e:
            raise UnreadableOutput(step.key, rel, e) from e


def _wrote_output(path: Path, before: tuple) -> bool:
    """True when the step that just exited wrote `path` itself.

    `before` is the path's signature from before the step started. A file
    that an earlier run left does not count.
    `agentcontext/steps/collected_files_archive.py --dry-run` exits zero over
    the archive already on disk, and existence alone would record that run as
    having produced the dataset.
    """
    after = _output_signature(path)
    if not after:
        return False
    if after[0] == "dir":
        return True
    return after != before


class _closing_quietly:
    """A file with a close that does not raise. The caller flushes the file explicitly and
    logs the error of that last flush before the block ends."""

    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        return self.handle

    def __exit__(self, *exc):
        try:
            self.handle.close()
        except OSError:
            pass
        return False


def _labeled(line: bytes, step_key: str) -> "bytes | None":
    """One line of a step's output as the run's log records it, or None for an empty one.

    `logs.labeled` keeps a log message as it is, and puts `INFO <step key>:` in front of a
    line that the step printed for a person to read. Every line of the log then names the
    step that wrote it. The decoding uses surrogateescape, because a filename in a line is
    bytes, and the log file keeps those bytes.
    """
    text = logs.labeled(line.decode("utf-8", "surrogateescape"), step_key)
    return None if text is None else (text + "\n").encode("utf-8", "surrogateescape")


def read_report(step: Step) -> "dict | None":
    """The JSON object that a step declares as its report, or None, with an error logged,
    when the step exited 0 without writing a readable JSON object."""
    path = data_dir(step) / step.report
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logs.error("runner", f"{logs.step_name(step.key)} exited 0, but its declared report "
                             f"{step.report} could not be read ({error}).")
        return None
    if not isinstance(report, dict):
        logs.error("runner", f"{logs.step_name(step.key)} wrote {step.report}, which is not "
                             f"a JSON object.")
        return None
    return report


def status_path(key: str) -> Path:
    """The file in which step `key` states what it waits for (`logs.step_status`)."""
    return LOG_DIR / f"{key}.status.json"


# Where `run_step` sends a changed status of the running step when its caller names no place:
# the pipeline execution state of the run in progress, which `run` sets for each step.
_STATUS_TO: list = [None]


def record_step_status(state: dict, status: dict | None) -> None:
    """Record `status`, what the running step waits for, in the pipeline execution state under
    RUNNING_STEP_STATUS, or remove it for None, and write the file."""
    if status is None:
        state.pop(RUNNING_STEP_STATUS, None)
    else:
        state[RUNNING_STEP_STATUS] = status
    write_state(state)


def _step_status(path: Path, seen: tuple) -> tuple[tuple, dict | None, bool]:
    """The status that the step states in `path` and whether it changed since `seen`, the
    modification time and the size of the file when it was last read. A status that cannot be
    read as a JSON object counts as no status."""
    try:
        stat = path.stat()
    except OSError:
        return (), None, seen != ()
    mark = (stat.st_mtime_ns, stat.st_size)
    if mark == seen:
        return seen, None, False
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = None
    return mark, status if isinstance(status, dict) else None, True


def run_step(step: Step, on_status=None) -> bool:
    """Run one step and write its output to run_logs/<key>.log and to this process's stdout.

    Both receive each line as the step writes it. The log is the record, and the console
    is what an operator watches, in a container the only thing that the operator can watch.
    Each line passes through `_labeled`, and both outputs have the step's key on every line
    and no empty line.

    The step's environment names `status_path(key)` in `logs.STATUS_VARIABLE`, where the step
    states what it waits for. At each line of the log, a changed status goes to `on_status`, or
    else to the place that `run` set (`record_step_status`), and None goes there when the step
    ends with a status stated.

    Returns True when the step exits 0 after writing everything that it declares, and
    False otherwise, and the caller then stops the pipeline. The caller skips an already
    complete step and records a finished one, because both are decisions about the run's
    state, and this function only runs a process.
    """
    global _CHILD
    name = logs.step_name(step.key)
    cmd = [step.interpreter] + step.args
    if step.code and step.args and not step_module(step):
        # The step runs in its data directory, and its script is named by its full path.
        cmd[1] = str(code_dir(step) / step.args[0])
    log_file = log_path(step.key)
    logs.info("runner", f"{name} starting ({' '.join(cmd)}).")
    base = data_dir(step)
    # A first run over an empty data root has no directory for the stage yet.
    base.mkdir(parents=True, exist_ok=True)
    before = {p: _output_signature(base / p) for p in step.produces}
    start = time.monotonic()
    try:
        log_handle = open(log_file, "wb")
    except OSError as error:
        logs.error("runner", f"Could not open {log_file}, the log of {step.key} ({error}).")
        return False
    status_file = status_path(step.key)
    status_file.unlink(missing_ok=True)
    seen, stated = (), False
    on_status = on_status or _STATUS_TO[0]

    def note_status() -> None:
        nonlocal seen, stated
        seen, status, changed = _step_status(status_file, seen)
        if changed and on_status is not None and (status is not None or stated):
            on_status(status)
            stated = status is not None

    with _closing_quietly(log_handle) as log:
        try:
            _CHILD = subprocess.Popen(cmd, cwd=base, stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT,
                                      env={**os.environ, logs.STATUS_VARIABLE: str(status_file)})
        except OSError as error:
            # The step could not be started, because fork() failed or the interpreter is missing.
            # Caught here, because the handler written for the state file would name that file.
            logs.error("runner", f"{name} could not be started ({error}).")
            return False
        # A signal delivered inside Popen(), after main's check for a stop and before the
        # assignment above completes, would find _CHILD still None and take the branch for no
        # running step. The child would never be signaled, and wait() would block for the
        # step's full duration. The interval is short, and the cost is the whole step.
        if _STOPPING and _CHILD.poll() is None:
            logs.info("runner", f"A stop arrived while {name} was starting. Forwarded it to "
                                f"the step.")
            _CHILD.send_signal(_STOPPING)
        log_failed = None
        try:
            try:
                for line in iter(_CHILD.stdout.readline, b""):
                    text = _labeled(line, step.key)
                    if text is None:
                        continue
                    log.write(text)
                    log.flush()
                    sys.stdout.write(text.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                    note_status()
            except OSError as error:
                # A write to the log failed, for example with ENOSPC or EIO, or the console is
                # gone. The runner signals the step to stop and waits for it rather than
                # leaving it running under a runner that no longer reads its output. The error
                # does not reach the handler written for the state file, which would name that
                # file.
                log_failed = error
                logs.error("runner", f"Could not write {log_file}, the log of {step.key} "
                                     f"({error}).")
                logs.info("runner", f"Signaled {step.key} to stop.")
                _CHILD.send_signal(signal.SIGTERM)
                # The runner reads the output to its end rather than stopping. On SIGTERM,
                # repository_cloning and file_history finish the work in flight and keep
                # printing. Once the pipe's buffer is full, the step would block on its next
                # print, and the wait below would never return. The rest of the output goes to
                # the console alone, where a write that fails is dropped.
                for line in iter(_CHILD.stdout.readline, b""):
                    text = _labeled(line, step.key)
                    try:
                        if text is not None:
                            sys.stdout.write(text.decode("utf-8", errors="replace"))
                            sys.stdout.flush()
                    except OSError:
                        pass
            returncode = _CHILD.wait()
        finally:
            _CHILD = None
            # The status file and the recorded status are removed when the step ends, also on a
            # kill or a stop, because a step that has ended waits for nothing.
            status_file.unlink(missing_ok=True)
            if stated and on_status is not None:
                on_status(None)
        try:
            # Writing what the buffer still contains fails the same way on a full volume, and
            # the close of the `with` block would raise the error past the handler above.
            log.flush()
        except OSError as error:
            if log_failed is None:
                log_failed = error
                logs.error("runner", f"Could not write {log_file}, the log of {step.key} "
                                     f"({error}).")
    elapsed = time.monotonic() - start

    if log_failed is not None:
        return False
    if returncode != 0:
        logs.error("runner", f"{name} exited {returncode} after {elapsed:.0f}s. Its log is "
                             f"{log_file}.")
        return False

    unwritten = [p for p in step.produces
                 if not _wrote_output(base / p, before[p])]
    if unwritten:
        named = series(f"{p} (unchanged since before the step ran)"
                       if (base / p).exists() else p for p in unwritten)
        logs.error("runner", f"{name} exited 0 after {elapsed:.0f}s but did not write "
                             f"{named}. Its log is {log_file}.")
        return False

    logs.info("runner", f"{name} finished in {elapsed:.0f}s.")
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = cli.ArgumentParser(
        description="Run the steps of the collection and publication stages in order.")
    parser.add_argument("--with-commit-reclassification", action="store_true",
                         help="Run commit_reclassification after repository cloning, and the "
                              "checks commit_attribution_validation and unmatched_commits_report "
                              "before the publication (default: off)")
    parser.add_argument("--from", dest="from_name", action="append", default=None,
                         help="Run this stage or step again, with every step that reads its output, "
                              "even when it is recorded as complete (repeatable)")
    parser.add_argument("--list", action="store_true",
                         help="Print the stages with their steps and exit")
    parser.add_argument("--repo-csv", default=None, metavar="PATH",
                         help="The list of repositories that repository cloning collects "
                              f"(default: {DEFAULT_REPO_CSV} under the stage directory, written "
                              "by engineered_project_selection)")
    parser.add_argument("--mark-complete", default=None, metavar="STEP",
                         dest="mark_complete",
                         help="Record STEP as complete from its output on disk, without running it, "
                              "and exit. Only for a step recorded by its output, with no output "
                              "missing or empty, such as repository_cloning after a restore from a "
                              "handover")
    parser.add_argument("--step-arg", action="append", default=[], dest="step_args",
                         metavar="NAME:ARGS",
                         help='Append arguments to the command of one step, such as '
                              'repository_cloning:"-w 10" (repeatable)')
    parser.add_argument("--scratch-dir", type=Path, default=None, metavar="DIR",
                         help="The directory for the clones of every step that clones, where "
                              "the free space is measured (default: _scratch/ in the stage "
                              "directory). Use a mounted volume in a container")
    parser.add_argument("--min-free-gb", type=float, default=None, metavar="GB",
                         help=f"The free disk space in GiB that the pre-checks require "
                              f"on each volume that it measures and the cloning step requires "
                              f"while it clones (default: {MIN_FREE_DISK_GB} when a step that "
                              f"clones is pending, otherwise "
                              f"{MIN_FREE_DISK_GB - CLONES_IN_FLIGHT_GB})")
    parser.add_argument("--no-github", action="store_true",
                         help="Declare clones from local Git mirrors through a GIT_CONFIG_* "
                              "redirect rather than from github.com (the pre-checks then "
                              "validates no GitHub access token and fails when GITHUB_TOKEN is "
                              "set)")
    return parser


# The flags of a runner's command line that `--mark-complete` would ignore, each with the
# attribute that its parser gives it. They select the steps to run, print the steps instead, or
# change the arguments of a step.
MARK_COMPLETE_IGNORES = (("--list", "list"), ("--from", "from_name"), ("--repo-csv", "repo_csv"),
                         ("--step-arg", "step_args"))


def refuses_mark_complete(args: argparse.Namespace) -> bool:
    """Whether `--mark-complete` shares the command line with a flag that it would ignore,
    with an error logged when it does.

    `--mark-complete` records one step from the output on disk and exits. Every such flag
    would have no effect and no message: `--list --mark-complete repository_cloning` would
    print the list of steps, exit 0 and record nothing, which reads as success. Every runner's
    `main` calls this function before it applies `--repo-csv` and `--step-arg` to the steps.
    Once they are applied, `run` cannot see them, and the cloning step would be recorded under
    the inputs of the list that `--repo-csv` names.
    """
    if not getattr(args, "mark_complete", None):
        return False
    ignored = [flag for flag, name in MARK_COMPLETE_IGNORES if getattr(args, name, None)]
    if ignored:
        logs.error("runner", f"--mark-complete records one step and exits. It cannot be "
                             f"combined with {series(ignored)}.")
    return bool(ignored)


def main(argv: list = None) -> int:
    args = build_parser().parse_args(argv)
    if refuses_mark_complete(args):
        return 1

    steps = steps_for(args.with_commit_reclassification)
    scratch_dir = args.scratch_dir if args.scratch_dir is not None else DEFAULT_SCRATCH_DIR
    steps = apply_scratch_dir(steps, scratch_dir)
    steps = apply_repo_csv(steps, args.repo_csv)
    steps = apply_min_free_gb(steps, args.min_free_gb)
    if args.step_args:
        steps = apply_step_overrides(steps, args.step_args)
    return run(steps, list_only=args.list, from_name=args.from_name,
               mark=args.mark_complete, no_github=args.no_github,
               scratch_dir=scratch_dir, argv=sys.argv, min_free_gb=args.min_free_gb,
               credentials_path=credentials.default_path())


def steps_for(with_commit_reclassification: bool) -> list:
    """The list of steps: the core steps and, with the commit steps, the
    commit_reclassification step and the two checks. The commit_reclassification step comes
    right after repository cloning, before the detection step restricts its CSV, and the two
    checks come last among the steps of the collection, before the publication. A list with no
    cloning step gets the checks alone."""
    steps = list(CORE_STEPS)
    if with_commit_reclassification:
        keys = [step.key for step in steps]
        if 'repository_cloning' in keys:
            steps.insert(keys.index('repository_cloning') + 1, COMMIT_HEURISTICS_STEP)
        publication = [i for i, step in enumerate(steps) if step.stage == "publication"]
        at = publication[0] if publication else len(steps)
        steps[at:at] = COMMIT_RECLASSIFICATION_STEPS
        # The detection step restricts the reclassified CSV, and the unmatched check ranks its
        # attributions. The collected file cleanup must rebuild the tree first, because an
        # earlier run of file_history may have removed directories from it. The cleanup
        # rebuilds the tree from the collected files without collecting again.
        if COMMIT_HEURISTICS_STEP in steps:
            steps = [replace(step, consumes=(*step.consumes, COMMIT_HEURISTICS_STEP.key))
                      if step.key in COMMIT_DEPENDENTS else step for step in steps]
    return steps


def use_log_dir(log_dir) -> None:
    """Keep the pipeline execution state, the run lock and the logs under `log_dir` rather than
    under the stage directory.

    The runners of the other stages and `run_pipeline.py` each keep their own, and two lists of
    steps never share a state file or a run lock.
    """
    global LOG_DIR, LOCK_PATH, STATE_PATH
    LOG_DIR = Path(log_dir)
    LOCK_PATH = LOG_DIR / "run.lock"
    STATE_PATH = LOG_DIR / "state.json"


def run(steps: list, *, list_only: bool = False, from_name: str = None,
        mark: str = None, no_github: bool = False, scratch_dir=None,
        argv: list = None, min_free_gb: float = None,
        log_dir=None, credentials_path=None) -> int:
    """Run a list of steps: take the run lock, read the pipeline execution state, apply the
    checks and the pre-checks, and run the loop over the steps.

    `steps` is any list of `Step`s, each naming the directory in which it runs. `min_free_gb` is the
    minimum of free disk space that the pre-checks require, and `log_dir` the directory of
    the pipeline execution state. `argv` is what the state names as the command. """
    if log_dir is not None:
        use_log_dir(log_dir)
    argv = sys.argv if argv is None else argv

    # Checked here and not only in `main`, because only `python -m agentcontext run` reaches
    # `main`. The other runners call this function directly, and a flag that `--mark-complete`
    # ignores would otherwise have no effect. By this point, the two other flags that `main`
    # checks, `--repo-csv` and `--step-arg`, are already applied to `steps`.
    if mark:
        ignored = [flag for flag, given in (("--list", list_only), ("--from", from_name))
                   if given]
        if ignored:
            logs.error("runner", f"--mark-complete records one step and exits. It cannot be "
                                 f"combined with {series(ignored)}.")
            return 1
    # Before the listing, not after it, so that `--list --from nosuchstage` does not
    # return 0 without a message in any entry point.
    for name in from_names(from_name):
        _require_known_name(steps, name)
    if mark:
        _require_known_name(steps, mark)
        if mark in stage_names(steps) and mark not in step_names(steps):
            logs.error("runner", f"--mark-complete records one step, but {mark} is a stage of "
                                 f"{len([s for s in steps if s.stage == mark]):,} steps. Name the "
                                 f"step.")
            return 1

    if list_only:
        current = None
        for s in steps:
            if s.stage != current:
                current = s.stage
                if current:
                    logs.info("runner", f"The stage {current} runs these steps in order.")
            where = f" (data directory {s.root})" if s.root else ""
            indent = "  " if s.stage else ""
            args = ([f"{s.code}/{s.args[0]}", *s.args[1:]]
                    if s.code and s.args and not step_module(s) else s.args)
            logs.info("runner", f"{indent}{s.key} runs {s.interpreter} {' '.join(args)}{where}.")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Restored in the finally below rather than left installed, because this
    # module's test suite imports it, and a handler that outlived the call that
    # installed it would handle signals meant for whatever runs next.
    global _STOPPING, _FORWARDED
    _STOPPING = 0
    previous_handlers = {sig: signal.signal(sig, forward_stop)
                         for sig in (signal.SIGTERM, signal.SIGINT)}

    try:
        locked = acquire_lock()
    except OSError as error:
        # A lock file that the run cannot open, such as a read-only lock file restored from a
        # handover, ends the run with a message rather than a traceback.
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        logs.error("runner", f"The run lock {LOCK_PATH} could not be opened ({error}). Make "
                             f"the file writable, or remove it.")
        return 1
    if not locked:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        held_by = LOCK_PATH.read_text().strip() if LOCK_PATH.exists() else "?"
        logs.error("runner", f"Another run holds the run lock {LOCK_PATH} ({held_by}). No "
                             f"step ran.")
        return 1

    current_step = None
    step_results = []
    # Whether a step of this start has started. A stop before the first step, including a stop
    # in the loop over the steps that the start skips, ends a start that ran nothing
    # (STOPPED_BEFORE_STEPS).
    step_started = False
    run_started_wall = time.time()
    state = {}
    # Whether `read_state` has returned. Until it has, `state` is the empty dict that this line
    # sets, and writing that dict over a damaged `state.json` would destroy the file that the
    # operator has just been told to repair.
    state_is_readable = False
    try:
        # Marking completion writes pipeline execution state under the run lock.
        if mark:
            return mark_complete(steps, mark, run_started_wall, argv)


        state = read_state()
        state_is_readable = True
        # Before anything reads the completion records. A step that exited 0 in the run before
        # counts as that run would have recorded it, and as a step that this start ran. The
        # pipeline execution state then records what the step changed: the time of the last
        # steps, which the container compares with the handover that it packed, and the end of
        # the running step that the kill left named.
        if record_exited_step(steps, state):
            write_state(state)
        recorded_from_exit = set(state.get(RECORDED_FROM_EXIT) or ())
        # A snapshot already on disk is the result of its step. The step that --from names
        # is the one snapshot that the operator wants taken again.
        redo = steps_to_redo(steps, from_name)
        if adopt_irreproducible(steps, state, set(redo)):
            write_state(state)
        # Every step that the completion records name, whatever its fingerprint is now. The
        # check below tests whether a step ever ran, which differs from whether its result
        # still counts, and only the raw records show whether it ran.
        recorded = set(state.get("completed", {})) & {s.key for s in steps}
        done = completed_steps(steps, state)
        if name_recorded_inputs(steps, state, done):
            write_state(state)
        if done:
            logs.info("runner", f"Skipping the steps that are already complete "
                                f"({series(logs.step_name(key) for key in sorted(done))}).")

        # `recorded`, not `done`, because an earlier run did the damage, and removing a step
        # from the completion records does not undo what the step did. The check has to hold
        # for both ways of removing it: `--from detection` on a finished run removes
        # file_history, and so does an edit to agentcontext/steps/detection.py. A check against
        # either reduced set would conclude that file_history never ran and miss exactly the
        # run that it exists to find. MODIFIED adds a modifying step that started and was never
        # recorded, such as a file history step that removed directories from the tree and
        # then failed. The name that --from gives is checked once, above, where `redo` is
        # computed from it.
        prospective = pending_step_names(steps, done) | steps_to_redo(steps, from_name)
        problems = modified_input_problems(steps, prospective, recorded,
                                           state.get(MODIFIED, {}))
        # The runner does not run a damaged step that takes a snapshot again without the
        # operator, because a new snapshot can take days and is not the snapshot that the steps
        # read. Nor does it run a damaged step that is not in this run's list, which it cannot
        # run.
        by_key = {s.key: s for s in steps}
        refused = [p for p in problems if p[1] not in by_key
                   or by_key[p[1]].completion == COMPLETION_OUTPUT]
        if refused:
            report_mutated_inputs(refused)
            write_execution_state(state, step_results, "modified_inputs", run_started_wall, argv)
            return 1
        # This run produces every other damaged step again, as `--from` would, rather than
        # refusing. A refusal costs an unattended Start, and its remedy, a `--from` in
        # `command:`, keeps the entrypoint from beginning the monthly run.
        if problems:
            report_rebuilds(problems)
            given = from_names(from_name)
            from_name = given + [s.key for s in steps if s.key in {p[1] for p in problems}
                                 and s.key not in given]

        # What --from will redo is decided now, so that the pre-checks verify the right
        # pending steps, but it is not yet removed from the completion records. Pre-checks
        # that fails runs nothing, and a run that ran nothing must not be the reason that a
        # completed step stops counting.
        if from_name:
            done -= steps_to_redo(steps, from_name)

        pending = pending_step_names(steps, done)
        # A stop before this point, such as while the record of the collected tree is computed, and
        # a stop during the pre-checks, whatever the pre-checks found, end a start
        # that ran nothing, which leaves the outcome of the run before it in place.
        passed = not _STOPPING and pre_checks(steps, pending, no_github=no_github,
                                              scratch_dir=scratch_dir,
                                              min_free_gb=min_free_gb,
                                              credentials_path=credentials_path)
        if _STOPPING:
            logs.warning("runner", "Stopped before the first step. Start the run again to "
                                   "continue.")
            write_execution_state(state, step_results, STOPPED_BEFORE_STEPS, run_started_wall,
                                  argv)
            return 128 + _STOPPING
        if not passed:
            logs.info("pre_checks", "A check failed. No step ran.")
            write_execution_state(state, step_results, "pre_checks_failed",
                                  run_started_wall, argv)
            return 1

        # The last check that can refuse the run has passed, and the steps can now be removed
        # from the completion records.
        if from_name:
            drop_completed_from(state, steps, from_name)
        # What this run is going to run stops counting as done until it has run. A step
        # that fails or is stopped keeps a stale record, and the next start runs it again.
        if mark_stale(state, pending, steps):
            write_state(state)

        def stopped_at(where: str) -> int:
            """A stop signal arrived. End the run without starting another step."""
            logs.warning("runner", f"Stopped {where}. The steps that completed are recorded in "
                                   f"{STATE_PATH}. Start the run again to continue.")
            write_execution_state(state, step_results,
                                  "interrupted" if step_started else STOPPED_BEFORE_STEPS,
                                  run_started_wall, argv)
            return 128 + _STOPPING

        overall_start = time.monotonic()
        for step in steps:
            # A stop that arrived between two steps, for example while the state was written.
            # The handler returns rather than raising, and the loop notices the stop here and
            # ends the run without a traceback, with the step that just finished recorded.
            if _STOPPING:
                return stopped_at(f"before {logs.step_name(step.key)}")
            current_step = step.key
            step_start = time.monotonic()
            # A step recorded by its inputs is done only while its inputs are what it read,
            # and a step recorded by its output and by `inputs_of` only while those inputs are
            # what it was recorded under. An earlier step of this run can have rewritten them.
            # For example, the GHS download names a new retrieval, and the steps that read the
            # retrieval declare the file that names it as an input rather than consuming the
            # download, so that a fallback to the same retrieval redoes none of them. The
            # labeling can give the cloning step a new list. The set `done` was computed before
            # the run began, and what decides the record is therefore computed again here
            # (`changed_during_run`).
            if (step.key in done
                    and changed_during_run(step, state["completed"].get(step.key, ""),
                                           state)):
                logs.info("runner", f"{logs.step_name(step.key)} is pending, because an earlier "
                                    f"step of the run changed one of its inputs.")
                newly = set(invalidated_by(steps, step.key)) & done
                done -= newly
                if mark_stale(state, newly, steps):
                    write_state(state)
            if step.key in recorded_from_exit:
                # Whatever this start does with the step, it gives the step a result, and no
                # later start gives it a result again.
                state[RECORDED_FROM_EXIT] = [key for key in state[RECORDED_FROM_EXIT]
                                             if key != step.key]
                if not state[RECORDED_FROM_EXIT]:
                    state.pop(RECORDED_FROM_EXIT)
                write_state(state)
                if step.key in done:
                    step_results.append({"name": step.key, "status": "ok",
                                         "duration_seconds": 0.0})
                    continue
            if step.key in done:
                origin = completion_origin(state, step.key)
                logs.info("runner", f"{logs.step_name(step.key)} skipped, because it is already "
                                    f"complete.{' ' + origin if origin else ''} Run --from "
                                    f"{step.key} to run it again.")
                step_results.append({"name": step.key, "status": "skipped",
                                      "duration_seconds": 0.0})
                continue
            if not step_started:
                # Written before the first step that the start runs, so that a kill inside that
                # step, which no handler catches, leaves the outcome `running` rather than the
                # outcome of the start before. The container's entrypoint resumes a run with
                # that outcome, and when the run completes, it decides whether the next monthly
                # run begins in the same Start.
                write_execution_state(state, step_results, "running", run_started_wall, argv)
                step_started = True
            # After the outcome `running` is written and before the state lists the step as
            # unfinished or its outputs as modified, so that a git that cannot start ends the
            # run as `failed` with neither of these recorded (`inputs_before`).
            pairs = inputs_before(step)
            inputs = inputs_part(pairs) if pairs is not None else None
            if step.irreproducible:
                # Written before the step starts, so that a kill, which no handler
                # catches, leaves the step listed too.
                set_unfinished(state, step.key, True)
                write_state(state)
            if step.mutates:
                # Written before the step starts, for the same reason. A step that fails or is
                # stopped after it modified an output is not recorded, and the check for
                # modified inputs would otherwise not know that it ran.
                for damaged in step.mutates:
                    set_modified(state, damaged, step.key)
                write_state(state)
            # The report of the step's last run stays until this run writes its own. A run that
            # fails then keeps the report beside the outputs that it describes, which a
            # `--mark-complete` or the recording of an irreproducible step can record, and the
            # step's readers, such as the release, find it. Without the report, the record of
            # such a reader would lack an input, and the reader would run again at every Start.
            report_before = (_output_signature(data_dir(step) / step.report)
                             if step.report else None)
            # Written just before the step starts, so that a kill inside the step leaves it
            # named (RUNNING_STEP). The write after the step, which records its result, removes
            # the name.
            write_execution_state(state, step_results, "running", run_started_wall, argv,
                                  running_step=step.key)
            _FORWARDED = False
            _STATUS_TO[0] = lambda status: record_step_status(state, status)
            try:
                ok = run_step(step)
            finally:
                _STATUS_TO[0] = None
            if _STOPPING and (_FORWARDED or not ok):
                # The step was signaled and is not complete, whatever it returned. A step that
                # finishes its work in flight on a stop can exit 0 with every path that it
                # declares present, and recording it would make the next run skip a partial
                # result. Running it again costs little, because each long step resumes from
                # its own records.
                step_results.append({
                    "name": step.key, "status": "interrupted",
                    "duration_seconds": round(time.monotonic() - step_start, 1),
                })
                return stopped_at(f"during {logs.step_name(step.key)}")
            # Read before the step is recorded, because a step that declares a report and
            # exits 0 without one has not done what it declares, like a step that leaves an
            # output unwritten.
            report = None
            if ok and step.report:
                if _wrote_output(data_dir(step) / step.report, report_before):
                    report = read_report(step)
                else:
                    logs.error("runner", f"{logs.step_name(step.key)} exited 0 but did not "
                                         f"write its declared report {step.report}.")
            if step.report and report is None:
                ok = False
            if ok:
                # The step's outputs reach the disk before any record names the step. Steps
                # write them by a rename without a sync, and a power loss after the record would
                # leave the step recorded over an empty or an earlier file.
                try:
                    sync_outputs(step)
                except UnreadableOutput as e:
                    logs.error("runner", f"{logs.step_name(step.key)} exited 0, but its output "
                                         f"could not be synced to disk ({e}). Did not record "
                                         f"the step as complete.")
                    ok = False
            if ok:
                # Named before the fingerprint is computed, which reads the whole output. A kill
                # or a power loss during that computation then leaves the step for the next run
                # to record rather than to run again (EXITED).
                state[EXITED] = {"step": step.key, "inputs": inputs, "input_pairs": pairs,
                                 "code": _git_info(),
                                 "script": script_fingerprint(step),
                                 "finished_at": datetime.now(timezone.utc).isoformat()}
                write_state(state)
                # Recorded with the fingerprint that the step's completion policy gives, so
                # that a later run can check whether what produced this result is still what
                # is on disk: the script for a step that can be derived again, the output
                # itself for a snapshot.
                #
                # The error is caught here rather than propagated. `output_fingerprint` raises
                # when an output cannot be read, and outside this handler the error would leave
                # the step that just finished unrecorded and also skip writing the pipeline
                # execution state. The inputs part is the part computed before the step ran
                # (`inputs_before`).
                try:
                    record_completion(state, step.key, step_fingerprint(step, inputs), "ran",
                                      finished_at=state[EXITED]["finished_at"],
                                      code=state[EXITED]["code"], inputs=pairs)
                    set_unfinished(state, step.key, False)
                    # The step has produced its output again, and no step has modified that
                    # output since.
                    clear_modified(state, step.key)
                except UnreadableOutput as e:
                    logs.error("runner", f"{logs.step_name(step.key)} exited 0, but its output "
                                         f"could not be read ({e}). Did not record the step as "
                                         f"complete.")
                    ok = False
                state.pop(EXITED, None)
            step_results.append({
                "name": step.key,
                "status": "ok" if ok else "failed",
                "duration_seconds": round(time.monotonic() - step_start, 1),
                **({"report": report} if report is not None else {}),
            })
            # Written after every step, not only at the end, so that the state
            # file of a run in progress records the steps completed so far.
            write_execution_state(state, step_results, "running", run_started_wall, argv)
            if not ok:
                write_execution_state(state, step_results, "failed", run_started_wall, argv)
                return 1
        elapsed = time.monotonic() - overall_start
        logs.info("runner", f"All steps complete in {elapsed / 60:.1f} min.")
        write_execution_state(state, step_results, "success", run_started_wall, argv)
        return 0
    except KeyboardInterrupt:
        # Reachable only in the short interval before forward_stop is installed, or when
        # something raises KeyboardInterrupt by other means. An ordinary Ctrl+C goes through the
        # handler and ends through stopped_at. The branch stays because that interval exists
        # and the alternative there is a traceback. Exit 130 (128 + SIGINT) rather than 1, so
        # that an interruption is never mistaken for an ordinary failure.
        where = f" during step '{current_step}'" if current_step else ""
        logs.warning("runner", f"Interrupted{where} by Ctrl+C. A step that did not finish is "
                               f"not recorded as complete. Start the run again to continue.")
        if current_step:
            step_results.append({
                "name": current_step,
                "status": "interrupted",
                "duration_seconds": round(time.monotonic() - step_start, 1),
            })
        write_execution_state(state, step_results, "interrupted", run_started_wall, argv)
        return 130
    except SystemExit as e:
        # Two different sources raise `SystemExit` inside this block, and the type of the
        # code distinguishes them exactly. The checks raise it with a message, and
        # `forward_stop` raises `SystemExit(128 + signum)` with an integer when a second
        # signal arrives with no step running.
        if isinstance(e.code, int):
            # The operator insists. The exit code is 130 or 143 rather than 1, for the reason
            # given in the KeyboardInterrupt branch above. The run is recorded as interrupted
            # and nothing else is done, because the second signal requests an immediate exit.
            where = f" during step '{current_step}'" if current_step else ""
            logs.warning("runner", f"Stopped{where} by a second signal. Exiting now.")
            if state_is_readable:
                write_execution_state(state, step_results,
                                      "interrupted" if step_started else STOPPED_BEFORE_STEPS,
                                      run_started_wall, argv)
            return e.code
        # A check's message is logged here rather than formatted at the `raise`, because this is
        # where it reaches the console, and a check in a library module should not depend on
        # the component that the line names. Unhandled, the message would reach only the
        # terminal, and without a write `state.json` would keep the previous step's `running`
        # entry.
        logs.error("runner", f"{str(e).rstrip('.')}.")
        # Only over a state file that was read. `read_state` raises this way for a truncated or
        # malformed `state.json`, with a message that instructs the operator to repair it, and
        # `state` is still the empty dict at that point. Writing that dict would destroy the file
        # being repaired, and the next run would clone every repository again, the outcome that
        # `read_state` refuses the run to prevent. The `OSError` branch below never writes the
        # state, for the same reason.
        if state_is_readable:
            write_execution_state(state, step_results, "failed", run_started_wall, argv)
        return 1
    except OSError as e:
        # write_state is not best-effort. If the completion of a step cannot
        # be recorded, continuing would lose it without a message, and the next
        # run would redo the step as if it had never run.
        logs.error("runner", f"Could not write {getattr(e, 'filename', None) or STATE_PATH} "
                             f"({e}). Stopped, because the completion of further steps cannot "
                             f"be recorded.")
        return 1
    finally:
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
