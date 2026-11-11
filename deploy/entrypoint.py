#!/usr/bin/env python3
"""
Start the pipeline runner inside the container.

The image contains the code of the five stages at /opt/pipeline: the package, the scripts of the
other stages, the heuristics and this script, and no data. The host's project folder contains
everything that a run reads and writes, mounted under /app as the data root `data/` and as
`handover/`. At every start, this script checks that the two directories are mounts that a run
can write, and removes what killed processes left in the data root and the handover directory
(`remove_leftovers`). It then runs `run_pipeline.py` as a child over the data root and packs
the handover when the runner ends. The first start begins from an empty data root: the sampling
stage downloads the GHS retrieval, the classification labels its sampling frame, and the
collection, the publication and the analysis write their outputs. A later start continues from
what the previous run left.

A start after a completed run, such as the Start of a monthly schedule, begins the next run with
a new retrieval (`monthly_argv`) when the current calendar month is later than the month in
which the run's GHS download started. Both months are taken in the NAS's time zone, which
compose.yaml mounts as a file beside the container's own local time, UTC (`month_zone`,
`next_month_due`). A start after a run that failed in a step of the analysis stage after its
release step completed does the same (`failed_analysis_step`). A start decides again after every
run that it ran, monthly runs included, when that run completes or fails in such a step, and
begins the next run in the same start when the next run is due by then
(`begin_next_month_after`). It begins no next run when a Stop arrived, or when the monthly run
that just ended left the record of its GHS download unchanged. A warning states the second case,
also for a monthly run that a check of the runner stopped before its first step. A start in
which the GHS download fell back to the previous sampling frame ends with a warning that names
the reason and how many downloads in a row fell back (`warn_after_fallback`).

A start begins the next run only when a handover contains the data root of the previous run,
which the next run rewrites. When no handover contains it, the start packs that handover first
(`hand_over_before_next_month`). It does so before a monthly run, and before a run with a
`--from` in the `command:` of compose.yaml over a data root in which the release step is done,
also after a Stop or a kill in the analysis stage (`from_arguments`, `release_step_completed`).
When every `--from` names the analysis stage or one of its steps, the start packs nothing before
the run (`redoes_only_the_analysis`). Under the image that ran the previous run, such a run
rewrites only `analysis/` and `run_logs/`. A new image also runs again every step that its code
or heuristics change, without packing first, as at every start with a new image.

An argument for one Start in `command:`, such as `--from`, is passed until a run has used it.
Later starts leave it out with a warning while `command:` names it
(`settle_one_start_arguments`). Such an argument is recognized by its full name, and the runner
and its steps refuse an abbreviation, such as `--fro`, with a usage error
(`agentcontext.cli.ArgumentParser`). A run has used `--accept-smaller` or `--end-date` only when
its GHS download took a retrieval (`took_effect`).

A start runs nothing when the container was created from a compose.yaml of a revision other than
the revision that this script requires, or when the container's own local time is not UTC, as
under a TZ variable with an offset other than zero (`container_problems`). Container Manager
keeps the compose.yaml of a project when a new image is imported, and a compose.yaml of an
earlier bundle can lack what this script reads. A start over a data root of an earlier data root
revision converts its stored files to this image's revision before any step runs, where
`CONVERSIONS` has a conversion from each revision in between (`convert_data_root`). A start runs
nothing over a data root of any other revision, because this image does not read the stored formats
of that revision (`data_root_problem`). The error names two remedies: the import of the image that
began or last converted the data root, which resumes the run, and a clean run with this image. The
first start over a data root without a pipeline execution state records the revision and the image
(`record_data_root`). The first log line of every start names the image's data root revision, as
BUILD_INFO.txt beside the image's tarball does.

The data root is mounted rather than built in, as the authors' mining project
jaws26-loop-engineering mounts its pinned `input/`, and replacing an input needs no rebuild. It
is mounted as one volume rather than path by path, because the package derives its default paths
from the data root, which this script names in `AGENTCONTEXT_DATA_ROOT`, and because
`os.replace` rewrites the caches. A rename over a file bind mount fails with EBUSY, and
a rename over a symlink replaces the symlink with a regular file in the container's own layer,
where the next start does not read it. Only with the whole data root mounted, the run's files
and the caches included, is every file that a run writes stored on the host. The code and the
heuristics are read from the image, and the volume contains no code.

After the runner exits, the script packs everything that a checkout needs from the run into one
archive in the second mount, `handover/`, as the mining project's run writes its handover zip.
The script packs the archive after every run that ends on its own: when every step finished,
when a step failed, and when a Stop ended the run. Only a kill of the container leaves no
archive, because no process remains to pack it. The script does not pack the collected and
validated trees again while an archive in `handover/` contains them as they are
(`recorded_trees`). After a Stop, the archive of a completed run leaves out the data root,
because 7zz can take longer to pack it than the stop grace period lasts, and the next start packs
the data root (`hand_over`). The operator downloads the archive and the container's log.

Inputs and outputs: see docs/deploy.md.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
"""

from __future__ import annotations

import csv
import fcntl
import fnmatch
import hashlib
import io
import json
import logging
import lzma
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

IMAGE_ROOT = Path("/opt/pipeline")
APP_ROOT = Path("/app")
# The data root, the one directory that a run reads and writes, mounted from the project folder
# at the path that it has in a checkout, so that every path in a log names the same file on the
# host.
DATA = "data"
# The collection stage's directory under the data root: its scratch directory and its collected
# and validated trees.
STAGE = f"{DATA}/collection"
# Where the archive of the run goes when the runner ends, mounted like the data root.
HANDOVER = "handover"
MOUNTS = (DATA, HANDOVER)
# The suffix under which 7zz writes an archive until the archive is done, and the pattern of such
# a file, which a packing that a kill ended leaves in the handover directory.
PARTIAL_SUFFIX = ".partial"
PARTIAL_ARCHIVES = f"*.7z{PARTIAL_SUFFIX}"
# The suffix of the file beside an archive that records the archive's SHA-256, the pattern of
# such a file that a kill left under its temporary name, and the number of bytes that
# `record_checksum` reads at a time.
CHECKSUM_SUFFIX = ".sha256"
PARTIAL_CHECKSUMS = f"*.7z{CHECKSUM_SUFFIX}{PARTIAL_SUFFIX}"
CHECKSUM_BLOCK = 1024 * 1024
# The collection stage's scratch directory, which holds the clones in progress and, because the
# entrypoint names it in TMPDIR, every temporary file of the run.
SCRATCH = f"{STAGE}/_scratch"
# The cache of GHS retrievals, and the pattern of a download that a stop or a kill interrupted:
# `1_download_ghs.py` writes the download as `<retrieval>.json.gz.part`. Each download takes a
# retrieval of its own, and no later download reads such a file.
RETRIEVALS = f"{DATA}/sampling/ghs_retrievals"
INTERRUPTED_DOWNLOAD = "*.json.gz.part"
# Left out of the handover, as paths under /app: the clones in progress, which the steps delete
# anyway, and a GHS download that a stop interrupted, which the next Start downloads again. The
# download is left out by its path rather than by its suffix, because a file of the sample can
# have any name, a `.part` name included.
HANDOVER_EXCLUDED_PATHS = (SCRATCH, f"{RETRIEVALS}/{INTERRUPTED_DOWNLOAD}")
# Left out wherever they are: the files that Python and Finder write beside the data.
HANDOVER_EXCLUDED_NAMES = ("__pycache__", ".DS_Store")
# Left out as directories wherever they are: the directories that Synology DSM writes into the
# shared folders that its File Station and indexing services visit, and that it changes at any
# time. A change while 7zz reads such a directory makes 7zz warn, which fails the packing. The
# names are those of `agentcontext.paths.DISCARDED_DIRECTORIES`, repeated rather than imported
# because this script has to run even when the package cannot be imported. A test checks that
# the two lists are equal. Like the pipeline, the packing leaves out such a name only as a
# directory.
DISCARDED_DIRECTORIES = ("@eaDir",)
# What a run that did not complete packs instead of the whole data root, relative to the data
# root, for the operator to read before the next Start: the pipeline execution state and logs,
# the data of the sampling, classification and analysis stages, and from the collection
# `finished_repositories.jsonl` with the records of the collection in progress and of the last
# collection that ended, the collected and validated lists, the caches, the output CSVs, the
# workflow files and the imported tree. The collected and validated trees follow in a second
# archive (`SAMPLE_TREES`) when the handover volume has room for them. The clone logs and the
# shards are packed only with the data root of a completed run. Between runs, the sample is in
# the project folder.
PARTIAL_HANDOVER_MEMBERS = ("run_logs", "sampling", "classification", "analysis",
                            "collection/run_logs", "collection/finished_repositories.jsonl",
                            "collection/finished_repositories.collection.json",
                            "collection/finished_repositories.ended-collection.json",
                            "collection/deletion.csv", "collection/file_history.csv",
                            "collection/submodules.csv", "collection/ai_authorship.csv",
                            "collection/output", "collection/data/collected/repos.csv",
                            "collection/data/collected/clone_failed.csv",
                            "collection/data/validated/repos.csv", "collection/data/workflows",
                            "collection/data/imported")
# The collected and validated trees, relative to the data root, which a run that did not
# complete packs into a second archive. After a failed step, the trees show the cause, because
# only they contain the symlinks, the markers and the files that the cleanup deleted. The second
# archive is written after the first, so that a Stop with a grace period that ends during their
# packing, or a volume without room for them, still leaves the first archive complete.
SAMPLE_TREES = ("collection/data/collected", "collection/data/validated")
# What `PARTIAL_HANDOVER_MEMBERS` contains, as the log messages name it.
WITHOUT_THE_SAMPLE = ("the pipeline execution state and logs, the data of the sampling, "
                      "classification and analysis stages, and the lists, caches and output CSVs "
                      "of the collection")

# The run lock under the data root.
RUN_LOCK = "run_logs/run.lock"
# The pipeline execution state, and the outcomes that the runner records for a start that ran no
# step: a refusal by the pre-checks or by the check for modified inputs, a `--mark-complete`
# that recorded a step and exited 0, and a stop before the first step. A test checks that the
# outcomes equal `runner.NO_STEP_OUTCOMES`.
STATE_FILE = "run_logs/state.json"
NO_STEP_OUTCOMES = ("pre_checks_failed", "modified_inputs", "marked_complete",
                    "stopped_before_steps")
# The key of the outcome of the last start that ran steps, which a start that ran no step leaves
# unchanged. A test checks that it equals `runner.LAST_RUN_OUTCOME`.
LAST_RUN_OUTCOME = "last_run_outcome"
# The key of the time at which the last start that ran a step finished, which a start that
# skipped every step, a refusal and a `--mark-complete` leave unchanged. A test checks that it
# equals `runner.LAST_STEPS_FINISHED_AT`.
LAST_STEPS_FINISHED_AT = "last_steps_finished_at"
# The key of the step results of the last start that ran a step, which a refusal, a
# `--mark-complete` and a check that stops the run before its first step leave unchanged,
# although each of them writes step results in which no step ran. A test checks that it equals
# `runner.LAST_RUN_STEPS`.
LAST_RUN_STEPS = "last_run_steps"
# The key of the step that the runner has started and that has not ended, which a kill of the
# runner inside that step leaves in the state. It is `runner.RUNNING_STEP`, repeated rather than
# imported, because this script has to run even when the package cannot be imported.
RUNNING_STEP = "running_step"
# The key of the `started_at` of the start that wrote RUNNING_STEP. A start that runs no step
# leaves the name of the step that a kill ended in an earlier start, and the key distinguishes
# that step from a step of the latest start (`step_of_the_latest_start`). It is
# `runner.RUNNING_STEP_STARTED_AT`, repeated for the same reason.
RUNNING_STEP_STARTED_AT = "running_step_started_at"
# The key of the step results of the start that wrote RUNNING_STEP, up to that step, which show
# whether the release step came before the step that a kill ended (`release_step_completed`).
# It is `runner.RUNNING_STEP_RESULTS`, repeated for the same reason.
RUNNING_STEP_RESULTS = "running_step_results"
# The record of the last handover of a completed run that this script packed: the archive's name
# and the `last_steps_finished_at` of the pipeline execution state that the archive contains,
# null when that state has none. A start that skipped every step packs no handover while the
# record names the time that the state names, or null while the state still names none. This
# holds also after you downloaded and deleted the archive, so that the Start at every boot-up
# does not fill the handover volume with copies of one data root. A packing that failed writes no
# record, and the next start packs the handover.
HANDOVER_RECORD = "run_logs/complete_handover.json"
# The record of the last archive of the collected and validated trees that this script packed:
# the archive's name and the fingerprint of the entries that it contains (`trees_listing`). A
# start that would pack the trees as the record names them packs nothing while that archive is
# in the handover directory, so that Starts that fail one after another, such as in a step of
# the analysis stage, do not fill the volume with copies of one sample.
TREES_RECORD = "run_logs/trees_handover.json"
# The record of the data root's revision, and the revision of the formats in which this image
# stores its files in the data root: the pipeline execution state, the caches, such as the cache
# of GHS retrievals, and the records of the steps and of this script. The first start over a
# data root without a pipeline execution state writes the record (`record_data_root`). A start
# over a data root of an earlier revision converts its stored files to this revision before any
# step runs, where `CONVERSIONS` has a conversion from each revision in between
# (`convert_data_root`). A start over a data root of any other revision runs no step and names a
# clean run as the remedy, and so does a start over a data root with a pipeline execution state
# but no record, which an image that writes no record began (`data_root_problem`). The revision
# rises by one with every change of a stored format, as deploy/0_README.md states.
DATA_ROOT_RECORD = "run_logs/data_root.json"
DATA_ROOT_REVISION = 3
# The record that a conversion of the data root writes after it has written each converted file
# beside its original. The record names the files still to replace (`convert_data_root`). A
# start after a kill finishes the replacement before anything else reads the data root.
CONVERSION_RECORD = "run_logs/data_root_conversion.json"
# The suffix of a converted file beside the file that it replaces.
CONVERTED_SUFFIX = ".converted"
# How many converted files the log line of a conversion names. Above it, the line names the
# directories of the files, because a cache of labels of many runs has a part per run.
NAMED_CONVERTED_FILES = 10
# The cache of labels of the classification under the data root, its index of runs, and the
# columns of the index that hold the SHA-256 of each part of a run's decisions and of its
# comparison with the previous run, each mapped to the word that names such a part in its file
# name, such as `decisions` in `<run>.decisions-001.jsonl.xz` (2_classification/label_cache.py).
LABEL_CACHE = "classification/labels"
LABEL_INDEX = "index.csv"
LABEL_PARTS = {"decisions_sha256": "decisions", "changes_sha256": "changes"}
# The preset with which the cache of labels compresses a part, `label_cache.XZ_PRESET`,
# repeated rather than imported, because this script has to run even when the package cannot
# be imported. `deploy/test_container.py` checks that the two are equal.
LABEL_XZ_PRESET = 6
# The step that ends the publication stage, and the stage of every step after it in the pipeline,
# which `--from` takes by its name for all of its steps. A test checks both against
# `run_pipeline.compose`.
RELEASE_STEP = "release"
ANALYSIS_STAGE = "analysis"
# The credentials file beside compose.yaml, mounted read-only, with the GitHub access tokens and
# the OpenRouter API key that the operator wrote. The classification needs the OpenRouter API
# key for its default model. The runner and each step read the file through
# `agentcontext.credentials`. This script does not read the file, because it has to run even
# when the package cannot be imported. It names the file to the run through CREDENTIALS_VARIABLE
# and logs whether the file exists.
CREDENTIALS_FILE = APP_ROOT / "credentials.csv"
CREDENTIALS_VARIABLE = "AGENTCONTEXT_CREDENTIALS"
# The build record that `deploy/Dockerfile` writes into the image, and what `build_info` returns
# in its place in an image without one.
BUILD_INFO = "BUILD_INFO"
NO_BUILD_RECORD = "no build record"
# What the entrypoint records about the machine at every start, in the run's log directory, so
# that the handover records the machine that the run used. The operator enters nothing.
MACHINE_RECORD = Path("run_logs") / "machine.json"
# The heuristics files of the image, copied beside the machine record at every start.
# `output/provenance.json` records the checksums of `config_path_patterns.csv`,
# `planning_document_patterns.csv`, `vendored_dirs.txt` and `local_state_paths.csv` alone.
# Without these copies, a handover cannot show which commit patterns or which negatives a
# collection used, and an image built before an edit to any
# of them produces data that the checkout cannot explain. The files are small, and the image's
# copies are read-only.
HEURISTICS_RECORD = Path("run_logs") / "heuristics"
# The runner arguments that print and exit, and therefore need no mount.
LISTING_FLAGS = ("--list", "-h", "--help")


# The format of every log message of this pipeline, and the component name of this script's
# lines. The format is `agentcontext.logs.FORMAT`, repeated rather than imported, because this
# script has to log problems even when the package cannot be imported.
# `deploy/test_container.py` checks that the two copies are equal.
LOG_FORMAT = "%(levelname)s %(name)s: %(message)s"
COMPONENT = "entrypoint"


def log(message: str, level: int = logging.INFO) -> None:
    """Write one log message of this script in the pipeline's format to stderr.

    The component name is this script's rather than the container's, because the container's
    log contains every line: one Log tab shows this script's lines, the runner's and every
    step's. The function formats the line itself rather than through `basicConfig`, so that the
    line goes to the current `sys.stderr`, which the suites redirect with `redirect_stderr`.
    """
    record = logging.LogRecord(COMPONENT, level, __file__, 0, message, None, None)
    print(logging.Formatter(LOG_FORMAT).format(record), file=sys.stderr, flush=True)


def log_warning(message: str) -> None:
    """Log what the operator has to know about a start that continues anyway."""
    log(message, logging.WARNING)


def log_error(message: str) -> None:
    """Log why this start cannot continue."""
    log(message, logging.ERROR)


def build_info(image_root: "Path | None" = None) -> str:
    """The build's identity, as the build script wrote it into `image_root`, the image's
    `IMAGE_ROOT` by default, or a note that it is absent."""
    try:
        return ((image_root or IMAGE_ROOT) / BUILD_INFO).read_text(
            encoding="utf-8").strip().replace("\n", ", ")
    except OSError:
        return NO_BUILD_RECORD


def first_line() -> str:
    """The first log line of every start: the build record and the data root revision of the
    image, which decides whether a Start resumes the run in a data root (`data_root_problem`)."""
    return (f"Running the image agentcontext-pipeline ({build_info()}). Its data root revision "
            f"is {DATA_ROOT_REVISION}.")


def series(names) -> str:
    """The names as a sentence lists them: `a`, `a and b` or `a, b and c`. The function is
    `agentcontext.runner.series`, repeated rather than imported, because this script has to run
    even when the package cannot be imported."""
    names = [str(name) for name in names]
    return " and ".join(names) if len(names) < 3 else f"{', '.join(names[:-1])} and {names[-1]}"


def unmounted(app_root: Path, names: tuple = MOUNTS,
              dev_of=lambda p: os.stat(p).st_dev) -> list:
    """The mount points among `names` that are not mounts, compared by device number.

    Inside the container, /app is the image's own layer, and every bind mount has the host's
    device. A directory on the same device as /app is in the container's layer: the directory
    exists and writes into it succeed, but everything written is lost when the container is
    removed. A name that does not exist is returned too.
    """
    try:
        root_device = dev_of(app_root)
    except OSError:
        return list(names)
    loose = []
    for name in names:
        try:
            if dev_of(app_root / name) == root_device:
                loose.append(name)
        except OSError:
            loose.append(name)
    return loose


def unwritable(app_root: Path, names: tuple = MOUNTS) -> list:
    """The mount points among `names` that cannot be written.

    `unmounted` checks only whether the host's filesystem is mounted. A mount can also be
    read-only, which Docker sets on request and a share can set itself. A read-only `handover/`
    matters most: the script writes to it only once, at the end, and a read-only mount there
    would pass every other check of the start and lose the archive of a finished run.
    """
    blocked = []
    for name in names:
        probe = app_root / name / f".writable-{os.getpid()}"
        try:
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError:
            blocked.append(name)
    return blocked


def flock_works(directory: Path) -> bool:
    """Whether the filesystem under `directory` implements flock.

    The run lock and the snapshot lock are both flocks, and `checks/snapshot.py` raises an error
    rather than continuing without a lock on a filesystem that refuses one. A remote share
    mounted into the project folder can refuse flock. A bind mount of the NAS's own volume does
    not.
    """
    probe = directory / ".flock-probe"
    try:
        fd = os.open(probe, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)
        try:
            probe.unlink()
        except OSError:
            pass


def run_logs_problem(data: Path) -> "str | None":
    """Why `run_logs/` cannot contain the pipeline execution state, or None.

    The cause is a file or a link in place of the directory.
    """
    path = data / "run_logs"
    if os.path.lexists(path) and not (path.is_dir() and not path.is_symlink()):
        return (f"/app/{DATA}/run_logs is not a directory. Remove it. The next Start creates "
                f"it as the directory of the pipeline execution state.")
    return None


def lock_holder(state: Path) -> "str | None":
    """The line in the file of the run lock when another process holds the lock, else None."""
    path = state / RUN_LOCK
    if not path.is_file():
        return None
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return path.read_text(encoding="utf-8", errors="replace").strip() or "no identifier"
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def handover_wanted(code: int, before: tuple, state: Path) -> bool:
    """Whether this start gets an archive: a step ran, or the run completed.

    The runner refused before any step ran when the pipeline execution state is unchanged, which is
    the refusal by the run lock, or when the state records a refusal by the pre-checks or by
    the check for modified inputs. A `--mark-complete` records a step and runs none. A refused Start
    gets no archive. A signal death is never a refusal. The runner writes the outcome `running` just
    before the first step that it runs, and a runner killed earlier, for example during the
    pre-checks, can leave the state unchanged, as a refusal by the run lock does. Treated as
    a refusal, the kill would leave no record on the host beyond one line of the container log. """
    if code < 0:
        return True
    if code == 0:
        return run_outcome(state) not in NO_STEP_OUTCOMES
    return state_signature(state) != before and run_outcome(state) not in NO_STEP_OUTCOMES


def pack_everything(outcome: str) -> bool:
    """Whether the archive contains the sample, which only the archive of a completed run does."""
    return outcome == "complete"


def read_state(state: Path) -> "dict | None":
    """The pipeline execution state, or None where it is missing or is not a JSON object.

    `startup_problems` refuses a start over a state that exists and cannot be read
    (`state_problem`), and after the checks, None means that no start has written the state.
    """
    try:
        recorded = json.loads((state / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return recorded if isinstance(recorded, dict) else None


def state_problem(data: Path) -> "str | None":
    """Why the pipeline execution state exists and cannot be read as a JSON object, or None.

    A state that cannot be read is not a missing state. Read as missing, it would make the
    start decide as if no run had happened, and the runner refuses it anyway, because writing a
    new state over it would discard the record of every completed step. The start stops before
    it changes anything, with a message that names the file and the recovery.
    """
    path = data / STATE_FILE
    if not path.is_file():
        return None
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        cause = f"cannot be read ({error})"
    else:
        if isinstance(recorded, dict):
            return None
        cause = "is not a JSON object"
    return (f"The pipeline execution state /app/{DATA}/{STATE_FILE} {cause}. No step ran. Keep "
            f"a copy of the file, and replace it with the {DATA}/{STATE_FILE} of the latest "
            f"archive in the handover directory. Deleting the file instead discards the record "
            f"of every completed step, and the next Start runs those steps again.")


def data_root_problem(data: Path) -> "str | None":
    """Why the data root stores its files in the formats of a revision other than
    `DATA_ROOT_REVISION` that this image does not convert (`convertible`), or None.

    The record names the revision and the build record of the image that began or last
    converted the data root. An image that writes no record began a data root with a pipeline
    execution state and no record, and a data root with neither is a first start. The steps read
    the caches and the records of the data root as this image writes them. Over the files of
    another revision, a step would stop at every Start with an error that names a column or a
    key rather than the remedy, and every Start would pack another archive of the run. A data
    root of an earlier revision with a conversion is converted before any step runs
    (`convert_data_root`). Over any other revision, the start stops before it removes, packs
    or writes anything, with a message that names the record, the revision and the two
    remedies: an image of the data root's revision, such as the image that the record names,
    which resumes the run, and a clean run with this image. An image's `BUILD_INFO.txt` names
    its revision, so that you can tell before an import whether the image resumes the run.
    """
    path = data / DATA_ROOT_RECORD
    resume = ""
    if not os.path.lexists(path):
        if not os.path.lexists(data / STATE_FILE):
            return None
        found = (f"has a pipeline execution state ({STATE_FILE}) and no record of its revision "
                 f"({DATA_ROOT_RECORD}). An image that writes no such record began it")
    else:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            revision = record["revision"]
        except (OSError, ValueError, KeyError, TypeError) as error:
            found = (f"has a record of its revision ({DATA_ROOT_RECORD}) that cannot be read "
                     f"({type(error).__name__}, {error})")
        else:
            if type(revision) is int and (revision == DATA_ROOT_REVISION
                                          or convertible(revision)):
                return None
            found = f"is of data root revision {revision!r} ({DATA_ROOT_RECORD})"
            image = record.get("image")
            if type(revision) is int:
                named = ("converted" if isinstance(record, dict) and "converted_from" in record
                         else "began")
                began = (f"the image that {named} the data root ({image}) or another image"
                         if isinstance(image, str) and image and image != NO_BUILD_RECORD
                         else "an image")
                resume = (f"To resume the run, import {began} with data_root_revision "
                          f"{revision} in its BUILD_INFO.txt, copy compose.yaml and deploy.md "
                          f"from the bundle of its build into the project folder, and build the "
                          f"project again. To run this image instead, ")
    clean = ("download the archives in the handover directory and the container's log, and begin "
             "a clean run in an empty project folder, as the section \"A clean run\" of deploy.md "
             "describes.")
    earlier = [str(revision) for revision in sorted(CONVERSIONS) if convertible(revision)]
    either = (f"{', '.join(earlier[:-1])} or {earlier[-1]}" if len(earlier) > 1
              else "".join(earlier))
    converts = (f"converts only a data root of revision {either} to them" if earlier
                else "converts no stored file")
    return (f"The data root /app/{DATA} {found}. This image reads only the stored formats of data "
            f"root revision {DATA_ROOT_REVISION} and {converts}. No step ran, and nothing "
            f"changed. " + (resume + clean if resume else clean[:1].upper() + clean[1:]))


def record_data_root(data: Path) -> bool:
    """Write the record of the data root's revision when the data root has none, and return
    whether it wrote one.

    `data_root_problem` has already refused a data root with a pipeline execution state and no
    record. A data root without the record is therefore a first start here, and the record names
    the revision and the image of that start.
    """
    path = data / DATA_ROOT_RECORD
    if os.path.lexists(path):
        return False
    write_record(path, {"revision": DATA_ROOT_REVISION, "image": build_info()})
    return True


class ConversionError(Exception):
    """A stored file that a conversion of the data root cannot convert."""


# The old name of the count and the limit of the linked pages as a JSON string. Every line that
# `renamed_linked_pages` changes contains it, as a key under `usage` or as a value of
# `limits_reached`.
PAGES_KEY = b'"pages"'


def renamed_linked_pages(value) -> bool:
    """Rename `pages` to `linked_pages` in each decision of the classification within `value`,
    where `pages` is the key under `usage` that counts the linked pages that the model read and
    where it is the value in `limits_reached` that names their limit. Data root revision 2 stores
    the key and the value as `linked_pages`. A decision is an object with a `usage` object. A
    run's decisions keep one decision per line, and a comparison and the partial files keep
    decisions inside their lines, such as under `repeat`. Returns whether anything changed."""
    changed = False
    if isinstance(value, list):
        for item in value:
            changed = renamed_linked_pages(item) or changed
        return changed
    if not isinstance(value, dict):
        return False
    usage = value.get("usage")
    if isinstance(usage, dict):
        if "pages" in usage:
            value["usage"] = {"linked_pages" if key == "pages" else key: count
                              for key, count in usage.items()}
            changed = True
        limits = value.get("limits_reached")
        if isinstance(limits, list) and "pages" in limits:
            value["limits_reached"] = sorted(("linked_pages" if limit == "pages" else limit
                                              for limit in limits), key=str)
            changed = True
    for item in value.values():
        changed = renamed_linked_pages(item) or changed
    return changed


def _converted_line(raw: bytes, convert, marker: bytes) -> "bytes | None":
    """`raw`, one JSON line of the cache of labels with its line break, after `convert`, which
    changes a parsed line in place and returns whether it changed it, or None when the line stays
    as it is. The line is written as the cache of labels writes it. A line without `marker` is
    not parsed, because every line that `convert` changes contains `marker`. A line that is not
    JSON, or that a stop cut short before its line break, stays."""
    if not raw.endswith(b"\n") or marker not in raw:
        return None
    try:
        item = json.loads(raw)
    except ValueError:
        return None
    if not convert(item):
        return None
    return (json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _convert_lines(path: Path, convert, marker: bytes, compressed: bool) -> "str | None":
    """Write the lines of `path` after `convert` (`_converted_line`) to `path` with
    CONVERTED_SUFFIX, xz-compressed with the preset of the cache of labels when `compressed`,
    and sync the file. Returns the SHA-256 of the written file, or None when no line changes,
    in which case the converted file is removed. The lines are read and written one at a time,
    because a part of a run's decisions keeps every tool output, and its text is many times the
    size of the part."""
    target = path.with_name(path.name + CONVERTED_SUFFIX)
    compressor = lzma.LZMACompressor(preset=LABEL_XZ_PRESET) if compressed else None
    digest, changed = hashlib.sha256(), False
    try:
        with (lzma.open(path, "rb") if compressed else open(path, "rb")) as lines, \
                open(target, "wb") as out:
            for raw in lines:
                converted = _converted_line(raw, convert, marker)
                changed = changed or converted is not None
                data = converted if converted is not None else raw
                data = compressor.compress(data) if compressor else data
                out.write(data)
                digest.update(data)
            if compressor:
                data = compressor.flush()
                out.write(data)
                digest.update(data)
            out.flush()
            os.fsync(out.fileno())
    except (lzma.LZMAError, EOFError) as error:
        target.unlink(missing_ok=True)
        raise ConversionError(f"{path} is not an xz file ({error})") from error
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if not changed:
        target.unlink()
        return None
    return digest.hexdigest()


def convert_linked_pages(data: Path) -> list:
    """For each file of the cache of labels under the data root `data` that stores a decision of
    data root revision 1, write the file as revision 2 stores it (`renamed_linked_pages`) under
    its name with CONVERTED_SUFFIX appended, beside the original, and return the files that the
    converted files replace.

    The parts of each run's decisions and comparison are the parts that the index of the cache
    names, and each is checked against the SHA-256 that the index records for it before it is
    converted. The converted part has one line for each line of the original, in the same order,
    and the conversion copies byte for byte every line that it does not change. When a part
    changes, the converted index records the SHA-256 of the converted part, and the index is the
    last of the returned files. The partial files of a run or a comparison in progress are
    converted as well. The labels of a run, its kept responses and the classification parameters
    store no decision and stay as they are."""
    cache = data / LABEL_CACHE
    for left in sorted(cache.glob(f"*{CONVERTED_SUFFIX}")) if cache.is_dir() else ():
        left.unlink()
    index = cache / LABEL_INDEX
    replaced = []
    if index.is_file():
        try:
            with open(index, newline="", encoding="utf-8") as f:
                table = list(csv.reader(f))
        except (csv.Error, UnicodeDecodeError) as error:
            raise ConversionError(f"{index} is not a CSV file ({error})") from error
        header, rows = (table[0], table[1:]) if table else ([], [])
        missing = [column for column in ("run_id", *LABEL_PARTS) if column not in header]
        if missing:
            raise ConversionError(f"{index} lacks the columns {missing}")
        for row in rows:
            if len(row) != len(header):
                raise ConversionError(f"{index} has a row with {len(row):,} of its "
                                      f"{len(header):,} columns")
            run = row[header.index("run_id")]
            for column, kind in LABEL_PARTS.items():
                position = header.index(column)
                checksums = row[position].split()
                for number, expected in enumerate(checksums, 1):
                    part = cache / f"{run}.{kind}-{number:03d}.jsonl.xz"
                    try:
                        found = _sha256(part)
                    except OSError as error:
                        raise ConversionError(f"{part} cannot be read ({error})") from error
                    if found != expected:
                        raise ConversionError(f"{part} does not match the SHA-256 that {index} "
                                              f"records for it")
                    converted = _convert_lines(part, renamed_linked_pages, PAGES_KEY,
                                               compressed=True)
                    if converted is not None:
                        checksums[number - 1] = converted
                        replaced.append(part)
                row[position] = " ".join(checksums)
    for partial in sorted(cache.glob("*.partial.jsonl")) if cache.is_dir() else ():
        if _convert_lines(partial, renamed_linked_pages, PAGES_KEY,
                          compressed=False) is not None:
            replaced.append(partial)
    if any(path.name.endswith(".jsonl.xz") for path in replaced):
        text = io.StringIO()
        csv.writer(text, lineterminator="\n").writerows([header, *rows])
        target = index.with_name(index.name + CONVERTED_SUFFIX)
        with open(target, "w", encoding="utf-8", newline="") as out:
            out.write(text.getvalue())
            out.flush()
            os.fsync(out.fileno())
        replaced.append(index)
    return replaced


# The outcome with which the runner of revision 1 recorded a start that its pre-checks refused,
# and the outcome of revision 2. Read under its earlier name, such a start would not count as
# refused (`REFUSED_OUTCOMES`).
REFUSED_BY_THE_PRE_CHECKS = {"preflight_failed": "pre_checks_failed"}


def convert_refused_outcome(data: Path) -> list:
    """When the pipeline execution state under the data root `data` records a start that the
    pre-checks refused, write a copy of the state, as the runner writes it, with the outcome
    renamed to its name of revision 2 (`REFUSED_BY_THE_PRE_CHECKS`), to the path of the state
    with CONVERTED_SUFFIX appended. Return a list with the path of the state, which the converted
    file replaces, or an empty list when the state has another outcome."""
    path = data / STATE_FILE
    target = path.with_name(path.name + CONVERTED_SUFFIX)
    target.unlink(missing_ok=True)
    state = read_state(data)
    if state is None or state.get("outcome") not in REFUSED_BY_THE_PRE_CHECKS:
        return []
    state["outcome"] = REFUSED_BY_THE_PRE_CHECKS[state["outcome"]]
    with open(target, "w", encoding="utf-8") as out:
        out.write(json.dumps(state, indent=2) + "\n")
        out.flush()
        os.fsync(out.fileno())
    return [path]


def convert_to_revision_2(data: Path) -> list:
    """The conversion from data root revision 1 to revision 2: the count and the limit of the
    linked pages in the decisions of the cache of labels (`convert_linked_pages`), and the
    outcome of a start that the pre-checks refused (`convert_refused_outcome`)."""
    return convert_linked_pages(data) + convert_refused_outcome(data)


# The key of the cloning step in the pipeline execution state, the key of the record of a step
# that exited 0 before the runner recorded it (`runner.EXITED`) and the key under which that
# record names the digest of each input, the prefix of the part of the record of the step that
# its inputs give, and the labels of those inputs in the order in which
# `agentcontext.runner.collection_inputs` names them, which that part hashes
# (`runner.inputs_part`). The keys of `collected_under` in a line of the list of finished
# repositories that record the first four of them (`agentcontext.reuse.current`). They are
# repeated rather than imported, because this script has to run even when the package cannot be
# imported, and a test checks them against the package.
CLONING_STEP = "repository_cloning"
EXITED = "exited"
EXITED_PAIRS = "input_pairs"
INPUTS_PART = "inputs:"
CLONING_INPUTS = ("the collector's code", "the version of git", "the path heuristics",
                  "the import heuristics", "the list of repositories to collect")
COLLECTED_UNDER = ("collector", "git", "heuristics", "imports")
# The list of finished repositories and the records of the collection in progress and of the
# last collection that ended beside it (`agentcontext.resume`), and the list that the cloning
# step collects by default (`runner.DEFAULT_REPO_CSV`), relative to the data root. The column of
# that list that names the repositories, and the form of a name that the cloning step reads from
# it (`repository_cloning.parse_repo_csv`).
FINISHED_LIST = "collection/finished_repositories.jsonl"
COLLECTION_RECORD = "collection/finished_repositories.collection.json"
ENDED_RECORD = "collection/finished_repositories.ended-collection.json"
SAMPLE_LIST = "classification/repos_annotated_included.csv"
SAMPLE_COLUMN = "repo_name"
REPOSITORY_NAME = re.compile(r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
# The time at which a collection began, at the start of its name
# (`agentcontext.resume.open_collection`), and the time at which the cloning step collected a
# repository, the field `scanned_at` of the row of its line (`agentcontext.collector.utc_now`,
# `agentcontext.paths.COLLECTED_CSV_HEADER`), both in UTC.
COLLECTION_BEGAN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{6}Z")
COLLECTION_BEGAN_FORMAT = "%Y-%m-%dT%H%M%SZ"
SCANNED_AT = "%Y-%m-%dT%H:%M:%SZ"
SCANNED_AT_FIELD = 1


def _sample_repositories(path: Path) -> "list | None":
    """The repositories of the list at `path` as the cloning step reads them, sorted and without
    duplicates: each value of SAMPLE_COLUMN that is a whole `owner/repo`. None where the list or
    the column is missing or the file is no CSV."""
    try:
        with open(path, encoding="utf-8", errors="ignore", newline="") as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames or SAMPLE_COLUMN not in reader.fieldnames:
                return None
            repos = {(row.get(SAMPLE_COLUMN) or "").strip() for row in reader}
    except (OSError, csv.Error):
        return None
    return sorted(repo for repo in repos if REPOSITORY_NAME.fullmatch(repo))


def _inputs_part(pairs) -> str:
    """The part of the record of a step that `pairs`, each a label and a digest, give, as
    `agentcontext.runner.inputs_part` computes it."""
    digest = hashlib.sha256()
    for label, value in pairs:
        digest.update(b"\0" + label.encode("utf-8") + b"\0" + value.encode("utf-8"))
    return INPUTS_PART + digest.hexdigest()


def _finished_lines(path: Path) -> list:
    """The lines of the list of finished repositories at `path` that parse as JSON objects, in
    their order, or an empty list where there is no list. A line that a kill cut short does not
    parse, as for `agentcontext.resume.read_finished`."""
    lines = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                try:
                    line = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(line, dict):
                    lines.append(line)
    except FileNotFoundError:
        return []
    return lines


def _latest_lines(lines: list) -> dict:
    """The latest line of each repository among `lines`, by repository, as
    `agentcontext.resume.read_finished` reads them: the last line with a repository and a row."""
    latest = {}
    for line in lines:
        if isinstance(line.get("repo"), str) and "row" in line:
            latest[line["repo"]] = line
    return latest


def _taken_over(latest: dict, repos: list, collection: str) -> list:
    """The repositories of `repos` that the collection `collection` took over from an earlier
    collection after a head query, from the latest line of each repository in `latest`, sorted.

    A collection that takes over a repository leaves its line, which names the earlier
    collection, and lists the repository in its record (`agentcontext.reuse.outdated`). Such a
    line names a collection other than `collection`. Only an ok line counts. The line of a
    repository for which git returned no head commit names the earlier collection as well, with
    `head_no_response`, and a continued collection has to query its head again. A failed line
    of an earlier collection carries no such mark when git returned no head commit for a
    refused repository, and the lines do not tell it from a confirmed refusal, so a continued
    collection queries its head again, at the cost of one head query. A line that the size
    bound or the time bound stopped is left to the bounds without a head query (`size_bound`,
    `time_bound`)."""
    taken = []
    for repo in repos:
        line = latest.get(repo)
        if line is None:
            continue
        if (isinstance(line.get("collection"), str) and line["collection"] != collection
                and line.get("fail_row") is None and not line.get("head_no_response")
                and line.get("size_bound") is None and line.get("time_bound") is None):
            taken.append(repo)
    return taken


def _parsed_time(text, form: str) -> "datetime | None":
    """The UTC time that `text` gives in the format `form`, or None where it gives none."""
    try:
        return datetime.strptime(text, form).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _written_during(lines: list, collection: str, started_at) -> bool:
    """Whether the collection `collection` wrote a line of `lines` during the start of the
    runner that began at `started_at`, as RUNNING_STEP_STARTED_AT names it: the collection began
    at or after that time, or one of its lines records that the cloning step collected its
    repository at or after it. The times have whole seconds, and the start counts from its
    second."""
    try:
        started = datetime.fromisoformat(started_at).astimezone(timezone.utc).replace(
            microsecond=0)
    except (TypeError, ValueError):
        return False
    named = COLLECTION_BEGAN.match(collection)
    began = _parsed_time(named.group(), COLLECTION_BEGAN_FORMAT) if named else None
    if began is not None and began >= started:
        return True
    for line in lines:
        row = line.get("row")
        if line.get("collection") != collection or not isinstance(row, str):
            continue
        fields = next(csv.reader([row]), [])
        scanned = (_parsed_time(fields[SCANNED_AT_FIELD], SCANNED_AT)
                   if len(fields) > SCANNED_AT_FIELD else None)
        if scanned is not None and scanned >= started:
            return True
    return False


def _write_converted(path: Path, text: str) -> None:
    """Write `text` to `path` with CONVERTED_SUFFIX appended, and sync the file."""
    target = path.with_name(path.name + CONVERTED_SUFFIX)
    with open(target, "w", encoding="utf-8") as out:
        out.write(text)
        out.flush()
        os.fsync(out.fileno())


def convert_cloning_records(data: Path) -> list:
    """Complete the records of a cloning step that the runner of an image of data root revision
    2 wrote under the data root `data`, such as the image with the backup providers, and return the files
    that the converted files replace or add, each written beside it with CONVERTED_SUFFIX
    appended.

    That runner recorded the cloning step by the hash of its inputs alone, and the end of a
    collection removed the record of the collection. An image of revision 3 names the digest of
    each input beside the record (`completed_by`), and the end of a collection keeps its record
    as the record of the last collection that ended (`finished_repositories.ended-collection.json`).
    With the record of the collection that ended, its runner runs the cloning step after a
    change of the collector's code or of the heuristics, and the step continues that collection
    and collects again only the repositories that the change reaches. Without it, such a change
    would make the step begin a new collection, which queries the head of every repository
    again and collects again every repository with a head that moved. The digests let a later
    change of the version of git alone keep the step.

    The step is the one that the record names, or the one that the record of an exit names
    (EXITED): a step that exited 0 in a run that a kill or a power loss ended before the runner
    recorded it, while it computed the fingerprint of the collected tree. The next start records
    that step from its output with the inputs part that the record of the exit names, and the
    conversion names the digest of each input beside it, as the runner of revision 3 does
    (EXITED_PAIRS). That run of the step came after the run that the record names, if any, and
    it decides which collection ended.

    The digests are rebuilt from the lines of the list of finished repositories: the collector's
    code, the version of git, the path heuristics and the import heuristics that a line records
    under `collected_under`, with the SHA-256 of the repositories of the list that the step
    collects by default (SAMPLE_LIST). They are accepted only where their hash is the part of the
    record that the inputs give, which shows that they are the inputs that the step was recorded
    under and that the list is the list of its collection. A record that a change of the list
    followed, or a step recorded over another list with `--repo-csv`, therefore stays as it is.

    The record of the last collection that ended is written only for a step that ran and exited
    0, with no collection in progress and no record of an ended collection. It names the latest
    collection that a line names, the SHA-256 and the repositories of the list, and the
    repositories that the collection took over from an earlier collection after a head query,
    which the latest line of each repository gives (`_taken_over`). A continued collection then
    queries none of their heads again. A step that `--mark-complete` recorded gets no such
    record, because `--mark-complete` removes it. During a collection, the record of the
    collection in progress names it, and the function writes no record of an ended collection.

    A kill or a power loss can also end the runner after the step closed its collection, which
    removed the record of the collection, and exited 0: while the runner synced the outputs of
    the step, before it wrote the record of the exit. The state then still names the step as the
    running step (RUNNING_STEP), and no record shows its inputs. The function writes the record
    of the collection that ended for such a step too, where all of these hold:

    - no record of an exit names the step, and the record of the step, if any, does not give the
      digests;
    - neither the record of a collection in progress nor that of an ended collection exists;
    - the lines cover the sample: every repository in the sample has a line, and the latest
      collection wrote its lines only for repositories in the sample;
    - the latest collection wrote a line during the start that RUNNING_STEP_STARTED_AT names
      (`_written_during`).

    The last condition keeps a monthly run, or a `--from repository_cloning`, that a kill ended
    before the step began its new collection from continuing the collection of the earlier
    snapshot, because every line of the latest collection then predates the start. A step whose
    collection wrote no line during the start, such as a collection that took over every
    repository, gets no record, and the step run again begins a new collection."""
    state_path = data / STATE_FILE
    ended_record = data / ENDED_RECORD
    for path in (state_path, ended_record):
        path.with_name(path.name + CONVERTED_SUFFIX).unlink(missing_ok=True)
    state = read_state(data)
    repos = _sample_repositories(data / SAMPLE_LIST)
    if state is None or repos is None:
        return []
    listed = hashlib.sha256("\n".join(repos).encode("utf-8")).hexdigest()
    lines = _finished_lines(data / FINISHED_LIST)

    def pairs_of(part) -> "list | None":
        """The labels and digests of the inputs that give the inputs part `part`, from the
        lines, the latest first, or None where no line gives it."""
        if not isinstance(part, str) or not part.startswith(INPUTS_PART):
            return None
        tried = set()
        for line in reversed(lines):
            under = line.get("collected_under")
            values = (tuple(under.get(key) for key in COLLECTED_UNDER)
                      if isinstance(under, dict) else ())
            if (not values or not all(isinstance(value, str) for value in values)
                    or values in tried):
                continue
            tried.add(values)
            candidate = list(zip(CLONING_INPUTS, (*values, listed)))
            if _inputs_part(candidate) == part:
                return [[label, value] for label, value in candidate]
        return None

    rewrite, ended, matched = False, False, False
    record = (state.get("completed") or {}).get(CLONING_STEP)
    entry = (state.get("completed_by") or {}).get(CLONING_STEP)
    if (isinstance(record, str) and isinstance(entry, dict)
            and entry.get("fingerprint") == record):
        pairs = pairs_of(record.partition("+")[2])
        if pairs is not None:
            matched = True
            if entry.get("inputs") is None:
                entry["inputs"] = pairs
                rewrite = True
            ended = entry.get("how") == "ran"
    exited = state.get(EXITED)
    named = isinstance(exited, dict) and exited.get("step") == CLONING_STEP
    if named:
        pairs = pairs_of(exited.get("inputs"))
        if pairs is not None and exited.get(EXITED_PAIRS) is None:
            exited[EXITED_PAIRS] = pairs
            rewrite = True
        ended = pairs is not None
    replaced = []
    if rewrite:
        _write_converted(state_path, json.dumps(state, indent=2) + "\n")
        replaced.append(state_path)
    names = [line["collection"] for line in lines if isinstance(line.get("collection"), str)]
    if not names or os.path.lexists(data / COLLECTION_RECORD) or os.path.lexists(ended_record):
        return replaced
    # The name of a collection begins with the time at which it started, and the collection
    # that ended last sorts last. The order of the lines is no such order, because the step
    # appends a line of an earlier collection again when it removes the files of its repository
    # (`agentcontext.reuse.remove_left`).
    collection = max(names)
    latest = _latest_lines(lines)
    if (not ended and not matched and not named and state.get(RUNNING_STEP) == CLONING_STEP
            and all(repo in latest for repo in repos)
            and set(repos) >= {repo for repo, line in latest.items()
                               if line.get("collection") == collection}
            and _written_during(lines, collection, state.get(RUNNING_STEP_STARTED_AT))):
        # The step closed its collection and exited 0, and a kill or a power loss ended the
        # runner while it synced the outputs, before it wrote EXITED.
        ended = True
    if ended:
        _write_converted(ended_record, json.dumps(
            {"collection": collection, "input_sha256": listed,
             "taken_over": _taken_over(latest, repos, collection),
             "repositories": repos}) + "\n")
        replaced.append(ended_record)
    return replaced


def convert_to_revision_3(data: Path) -> list:
    """The conversion from data root revision 2 to revision 3: the digest of each input of a
    recorded cloning step and the record of the collection that it ended
    (`convert_cloning_records`)."""
    return convert_cloning_records(data)


# The conversion of a data root from each revision to the next, by the revision that it converts
# from. A conversion writes every file that it changes or adds beside the file with
# CONVERTED_SUFFIX and returns the files that the converted files replace or add, and
# `convert_data_root` renames them into place.
CONVERSIONS = {1: convert_to_revision_2, 2: convert_to_revision_3}


def _record_revision(data: Path) -> "dict | None":
    """The record of the data root's revision, or None where it is missing or cannot be read
    as an object with a revision."""
    try:
        record = json.loads((data / DATA_ROOT_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and "revision" in record else None


def convertible(revision) -> bool:
    """Whether `CONVERSIONS` converts a data root of `revision` to `DATA_ROOT_REVISION`, one
    revision at a time."""
    return (type(revision) is int and revision < DATA_ROOT_REVISION
            and all(step in CONVERSIONS for step in range(revision, DATA_ROOT_REVISION)))


def _replace_converted(data: Path, names: list) -> None:
    """Rename the converted file of each path of `names`, relative to the data root `data`, over
    the file that it replaces, where the converted file still exists, and sync the directories."""
    directories = set()
    for name in names:
        path = data / name
        converted = path.with_name(path.name + CONVERTED_SUFFIX)
        if converted.exists():
            os.replace(converted, path)
        directories.add(path.parent)
    for directory in sorted(directories):
        sync_directory(directory)


def convert_data_root(data: Path) -> "str | None":
    """Convert the stored files of the data root `data` from the revision that its record names
    to `DATA_ROOT_REVISION`, one revision at a time, and return a log message of what was
    converted, or None when the data root needs no conversion. `data_root_problem` has refused
    every revision that `convertible` does not accept.

    Each conversion first writes every converted file beside the file that it replaces. It then
    writes the record of the conversion, which names the files. It renames the converted files
    over the originals, removes the record of the conversion and records the next revision. A
    kill before the record of the conversion leaves the originals and the record of the earlier
    revision, and the next start converts again. A kill after the record of the conversion
    leaves it in place, and the next start finishes renaming the converted files over the
    originals before it reads the data root. The revision is recorded only after every file of
    its conversion has replaced its original, so that no start reads a data root in two formats.
    Raises ConversionError or OSError when a file cannot be converted or written."""
    pending = data / CONVERSION_RECORD
    if pending.is_file():
        try:
            names = json.loads(pending.read_text(encoding="utf-8"))["files"]
        except (ValueError, KeyError, TypeError) as error:
            raise ConversionError(f"{pending} cannot be read ({error})") from error
        _replace_converted(data, names)
        pending.unlink()
        sync_directory(pending.parent)
    recorded = _record_revision(data)
    if recorded is None or not convertible(recorded["revision"]):
        return None
    started, image, converted = recorded["revision"], recorded.get("image"), []
    log(f"Converting the data root /app/{DATA} from revision {started} to revision "
        f"{DATA_ROOT_REVISION}.")
    for revision in range(started, DATA_ROOT_REVISION):
        replaced = CONVERSIONS[revision](data)
        names = [path.relative_to(data).as_posix() for path in replaced]
        if names:
            write_record(pending, {"from": revision, "to": revision + 1, "files": names})
            _replace_converted(data, names)
            pending.unlink()
            sync_directory(pending.parent)
        write_record(data / DATA_ROOT_RECORD,
                     {"revision": revision + 1, "image": build_info(),
                      "converted_from": {"revision": started, "image": image}})
        converted += names
    directories = sorted({name.rpartition("/")[0] or "." for name in converted})
    files = (f"Rewrote {len(converted):,} stored {'file' if len(converted) == 1 else 'files'} "
             f"({series(converted)})."
             if 0 < len(converted) <= NAMED_CONVERTED_FILES
             else f"Rewrote {len(converted):,} stored files in {series(directories)}."
             if converted else "No stored file needed a change.")
    return (f"Converted the data root /app/{DATA} from revision {started} to revision "
            f"{DATA_ROOT_REVISION}. {files}")


def every_step_skipped(state: Path) -> bool:
    """Whether the pipeline execution state records every step of the start as skipped. The
    runner records this at a Start after a completed run when nothing that a step depends on
    changed."""
    steps = (read_state(state) or {}).get("steps")
    return (isinstance(steps, list) and bool(steps)
            and all(isinstance(step, dict) and step.get("status") == "skipped" for step in steps))


def step_of_the_latest_start(recorded: dict) -> "str | None":
    """The step that the latest start in the pipeline execution state `recorded` began and did
    not end (RUNNING_STEP), or None. The runner keeps the name of a step that a kill ended
    through later starts that run no step. That name then belongs to the start that
    RUNNING_STEP_STARTED_AT names, not to the latest start."""
    name = recorded.get(RUNNING_STEP)
    if not isinstance(name, str) or recorded.get(RUNNING_STEP_STARTED_AT) != recorded.get(
            "started_at"):
        return None
    return name


def stopped_by_a_check(state: Path) -> bool:
    """Whether the latest start ended with the outcome `failed` and step results that record no
    step that ran, and began no step (`step_of_the_latest_start`). A check of the runner that
    stops the run before its first step writes this state, with the `last_run_steps` of an
    earlier start."""
    recorded = read_state(state) or {}
    steps = recorded.get("steps")
    return (recorded.get("outcome") == "failed" and isinstance(steps, list)
            and step_of_the_latest_start(recorded) is None
            and all(isinstance(step, dict) and step.get("status") == "skipped" for step in steps))


def last_steps_finished_at(state: Path) -> "str | None":
    """When the last start that ran a step finished, as the pipeline execution state records
    it, or None where it records no such start."""
    value = (read_state(state) or {}).get(LAST_STEPS_FINISHED_AT)
    return value if isinstance(value, str) and value else None


def sync_file(path: Path) -> None:
    """Write the data of a file through to the disk, or raise.

    A rename is atomic but not durable on its own. After a power loss, a renamed archive or
    record can name blocks that never reached the disk. A file is synced before its rename and
    its directory after the rename (`sync_directory`), so that the record never names an archive
    with bytes that can be lost, and the next monthly run never replaces a data root that only
    such an archive contains.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_directory(path: Path) -> None:
    """Write the entries of a directory through to the disk, where the filesystem can.

    Some filesystems refuse to open or sync a directory. The file's own data is on the disk
    either way. An error here would fail every packing on such a filesystem, and no monthly run
    could ever begin. `agentcontext.caches.sync_directory` does the same. It is repeated
    rather than imported, because this script has to run even when the package cannot be
    imported.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_record(path: Path, record: dict) -> None:
    """Write a record of this script under a temporary name, sync it and rename it, or raise.

    The caller writes it after `_pack` synced the archive that it names. A power loss therefore
    leaves either the previous record or the new record, and each names an archive with its
    bytes on the disk.
    """
    partial = path.with_name(path.name + ".partial")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        sync_file(partial)
        os.replace(partial, path)
    except OSError:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    sync_directory(path.parent)


def record_handover(state: Path, archive: Path) -> None:
    """Write the record of the handover of a completed run that was just packed: the archive's
    name and the `last_steps_finished_at` of the pipeline execution state that it contains."""
    write_record(state / HANDOVER_RECORD,
                 {"archive": archive.name, LAST_STEPS_FINISHED_AT: last_steps_finished_at(state)})


def recorded_handover(state: Path, label: str) -> "str | None":
    """The archive `agentcontext-handover-<time>-<label>.7z` that the record of the last handover
    of a completed run names, when it contains the data root as it is, or None.

    The last start that ran a step writes the pipeline execution state at its end, and the
    archive is packed after that write. An archive recorded with that start's
    `last_steps_finished_at` therefore contains the data root as that start left it. A start
    that skipped every step changed nothing in the data root, and the archive still contains the
    data root as it is now, wherever you keep it. The time of the pipeline execution state
    cannot show this, because every start writes the state, also a start that skipped every step
    and a refused start. Nor can the archives in the handover directory, because you delete the
    archives that you have downloaded. For a record that is missing, damaged or of an earlier
    start, the function returns None, and the start packs.

    A state written by a runner that does not record `last_steps_finished_at` has no such time,
    and the record of its handover has null in its place. The two match: a start that runs a
    step writes the time, and while the state has none, no step has run since the packing.
    Without the match, every start over such a state would pack the data root again, and the
    start that begins the next monthly run would pack it and then refuse to begin the run.
    """
    if read_state(state) is None:
        return None
    finished = last_steps_finished_at(state)
    try:
        record = json.loads((state / HANDOVER_RECORD).read_text(encoding="utf-8"))
        archive, packed_after = record["archive"], record[LAST_STEPS_FINISHED_AT]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (packed_after != finished or not isinstance(archive, str)
            or not archive.endswith(f"-{label}.7z")):
        return None
    return archive


def archives_in(handover_dir: Path) -> tuple:
    """The number and the bytes of the archives in the handover directory, without the partial
    files of a packing that was killed."""
    count = size = 0
    for path in handover_dir.glob("*.7z"):
        try:
            size += path.stat().st_size
        except OSError:
            continue
        count += 1
    return count, size


def last_run_outcome(state: Path) -> "str | None":
    """The outcome of the last start that ran steps, or None where there is none."""
    return (read_state(state) or {}).get(LAST_RUN_OUTCOME)


def run_outcome(state: Path) -> "str | None":
    """The outcome that the pipeline execution state records, or None where there is none."""
    return (read_state(state) or {}).get("outcome")


def failed_analysis_step(state: Path) -> "str | None":
    """The step that failed the last start that ran steps, when it is a step of the analysis
    stage, or None.

    The runner runs the steps in order and stops at the first step that fails, and every step after
    the release step is a step of the analysis stage. A failed step after a release step that the
    start recorded as done or skipped is therefore a step of the analysis stage, and the release
    step completed. The step results are those of the last start that ran a step (`LAST_RUN_STEPS`),
    not those of the latest start, which name no step after a refusal by the pre-checks.
    Read from the latest start, the results would make a Start after the refused Start that began
    the next monthly run resume the failed run, run the failed step again and pack the data root of
    that run a second time, because running the failed step changes the time that the record of the
    first packing names. """
    recorded = read_state(state)
    if recorded is None or recorded.get(LAST_RUN_OUTCOME) != "failed":
        return None
    steps = recorded.get(LAST_RUN_STEPS)
    if not isinstance(steps, list):
        return None
    released = False
    for step in steps:
        if not isinstance(step, dict):
            return None
        if step.get("name") == RELEASE_STEP:
            released = step.get("status") in ("ok", "skipped")
        elif step.get("status") == "failed":
            name = step.get("name")
            return name if released and isinstance(name, str) else None
    return None


def name_credentials(env: dict, path: Path = CREDENTIALS_FILE) -> str:
    """Name the credentials file to the run, unless the environment names another, and return
    the line to log: where the file is, that there is none, or a warning for a directory that
    Docker created in place of a missing file."""
    named = Path(env.setdefault(CREDENTIALS_VARIABLE, str(path)))
    if named.is_dir():
        return (f"WARNING {named} is a directory, not a file. Docker created it because the "
                f"file did not exist when the container started. Remove the directory and write "
                f"the credentials file in its place.")
    if named.is_file():
        return (f"Found the credentials file {named}. The pre-checks verify its GitHub "
                f"access tokens and its OpenRouter API key.")
    return (f"No credentials file at {named}. A pending step that needs a GitHub access token or "
            f"the OpenRouter API key fails the pre-checks unless the environment sets one.")


# How long a step left by a killed runner gets to end after SIGTERM before its group is killed.
# GROUNDING: the cloning step's own stop is bounded by the time for its workers to end
# (`repository_cloning.WORKER_STOP_SECONDS`) and the time for a killed group to release its
# pipes (`bounds.REAP_SECONDS`). The other steps have no worker pool and end on the signal. This
# time is well above that bound, which a test checks, and a small part of the grace period in
# compose.yaml (`stop_grace_period`), which has to cover this time and the packing after it.
KILLED_RUNNER_DRAIN_SECONDS = 120.0
# The program of 7-Zip that packs the handover, `agentcontext.SEVEN_ZIP`, repeated rather than
# imported, because this script has to pack even when the package cannot be imported.
# `deploy/test_container.py` checks that the two copies are equal.
SEVEN_ZIP = "7zz"
# The number of characters at the end of 7zz's output that the exception text keeps. The complete
# output of a failed packing is logged separately. GROUNDING: a policy, which keeps the exception
# text readable in the container log.
SEVEN_ZIP_TAIL_CHARS = 300
# The signals that a Stop and Ctrl+C send.
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
# The first stop signal that arrived during this start, or 0: a signal that this start passed on
# to the runner or to 7zz, or a signal that arrived while neither ran. After a Stop, `main` starts
# no runner, begins no next monthly run and starts no packing other than the handover of the run
# that just ended. Docker kills the container when the grace period ends, and a run or a packing
# that Docker kills leaves no handover. A runner that receives the signal while it finishes its
# last step still exits 0.
_stop_signal = 0


def note_stop(signum: int) -> None:
    """Record that a stop signal arrived during this start."""
    global _stop_signal
    _stop_signal = _stop_signal or signum


def note_stop_signal(signum: int, _stack_frame) -> None:
    """The handler of a stop signal while neither the runner nor 7zz runs: record the Stop, and
    let the start pack its handover."""
    note_stop(signum)


def run_runner(command: list, cwd: Path,
               drain_seconds: float = KILLED_RUNNER_DRAIN_SECONDS) -> int:
    """Run the runner as a child and return its exit code, forwarding SIGTERM and SIGINT.

    A child rather than `os.execv`, so that the handover can be packed after the runner ends.
    This process receives the signal that a Stop sends, and the runner forwards the signal to
    the running step. This process must therefore not end before the runner. The runner leads
    its own process group, so that a runner killed by the kernel, for example by the OOM killer,
    does not leave its step running under the init process while the handover is packed. The
    group then receives the SIGTERM that the runner would have forwarded, gets `drain_seconds`
    to end, and is killed after that. The function records every signal that it passes on
    (`note_stop`).
    """
    child = subprocess.Popen(command, cwd=str(cwd), start_new_session=True)

    def forward(signum, _stack_frame):
        note_stop(signum)
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    previous = {sig: signal.signal(sig, forward) for sig in STOP_SIGNALS}
    try:
        # The handler of the whole start, which passes nothing on, recorded a Stop that arrived
        # after the caller's last check and before `forward` was installed. The runner gets
        # that Stop now, because Docker sends no second signal and kills the container when
        # the grace period ends.
        if _stop_signal:
            forward(_stop_signal, None)
        code = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if code != 0:
        # After any exit other than 0: a signal death, or an exception in the runner, such as
        # an OSError on the step's log, after which the runner returns 1 while the step would
        # go on running during the packing and the container's teardown. A runner that ended
        # its step leaves an empty group, and ending that group costs one signal.
        ended = end_process_group(child.pid, drain_seconds)
        if ended != "empty":
            ending = (f"was killed by signal {-code}" if code < 0
                      else f"exited with {code}")
            log(f"The runner {ending} while its step was running.")
            log("The step ended after SIGTERM." if ended == "stopped"
                else f"Killed the step, still running {drain_seconds:g} s after SIGTERM.")
    return code


def end_process_group(pgid: int, drain_seconds: float) -> str:
    """SIGTERM a process group, wait up to `drain_seconds` for it to empty, SIGKILL the rest.

    Returns `empty` when the group had no member, `stopped` when it emptied on its own, and
    `killed` when the rest was killed. `os.killpg` raises `ProcessLookupError` for a group with
    no member left, which shows that the group is empty. On macOS it raises `PermissionError`
    for a group with only members that have exited and wait to be reaped. Such members hold
    nothing open, and no signal reaches them. That group counts as empty too.
    """
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return "empty"
    deadline = time.monotonic() + drain_seconds
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return "stopped"
        except PermissionError:
            # This process may not signal what is left in the group: on macOS, a member
            # that has exited and waits for the init process to reap it.
            return "stopped"
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return "stopped"
    return "killed"


def outcome_name(code: int) -> str:
    """The outcome in the archive's name: complete, stopped, killed or failed.

    `Popen.wait` returns a negative code for a child killed by a signal that the runner did not
    handle: a kill by the kernel, or a signal before the runner installed its handler. The
    outcome is `killed` rather than `stopped`, because the runner did not stop its step.
    """
    if code == 0:
        return "complete"
    if code < 0:
        return "killed"
    if code >= 128:
        return "stopped"
    return "failed"


def container_exit_code(code: int) -> int:
    """The runner's code as the container's: a signal death as 128 plus the signal."""
    return 128 - code if code < 0 else code


def state_signature(state: Path) -> tuple:
    """The modification time and size of the pipeline execution state, which show whether the
    runner wrote it."""
    try:
        st = (state / "run_logs" / "state.json").stat()
    except OSError:
        return ()
    return (st.st_mtime_ns, st.st_size)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class PackingStopped(RuntimeError):
    """A Stop ended 7zz during a packing."""


def _run_7z(args: list, cwd: Path, starts_after_stop: bool = True) -> tuple:
    """Run 7zz and return (code, output). A stop signal kills 7zz, and the function then returns
    128 plus the signal, a code with which 7zz itself never exits.

    With `starts_after_stop` False, as for 7zz over the whole data root, which can take longer
    than the stop grace period, 7zz does not start after a Stop that the handler of the whole
    start recorded before this function installed its own handler, and the function returns the
    same code. The caller checks for a Stop before it decides what to pack, and a Stop that
    arrives after that check would otherwise go unnoticed until Docker kills 7zz.
    """
    stopped = []
    process = None

    def stop(signum, _stack_frame):
        note_stop(signum)
        stopped.append(signum)
        if process is not None:
            process.kill()

    previous = {sig: signal.signal(sig, stop) for sig in STOP_SIGNALS}
    try:
        if not starts_after_stop and _stop_signal:
            return 128 + _stop_signal, ""
        process = subprocess.Popen([SEVEN_ZIP, *args], cwd=str(cwd), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        if stopped:
            # A Stop that arrived while Popen started 7zz.
            process.kill()
        output, _ = process.communicate()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if stopped:
        return 128 + stopped[0], output or ""
    return process.returncode, output or ""


def pack_handover(app_root: Path, handover_dir: Path, outcome: str, when: str,
                  everything: bool = True) -> Path:
    """Pack what a checkout needs from the run into one archive in the handover directory.

    When the run completed, the archive contains the data root without the collection stage's
    scratch directory, under the paths that it has in the repository, and `7zz x -snld` over a
    checkout puts every file where the repository keeps it. Otherwise it contains the files in
    `PARTIAL_HANDOVER_MEMBERS`. Symlinks are stored as links, because the collector writes
    symlink markers on purpose, and without `-snl` 7zz stores the target's bytes and fails on a
    dangling marker. The archive is written under a `.partial` name and renamed when 7zz is done,
    so that a kill during the packing leaves no file that looks complete. `remove_leftovers`
    removes such a file at the next start, before anything measures the free space of the
    handover volume. Every nonzero 7zz exit fails the packing, because a warning can mean that
    the archive omits files. 7zz over the whole data root does not start after a Stop
    (`_run_7z`).
    """
    archive = handover_dir / f"agentcontext-handover-{when}-{outcome}.7z"
    if everything:
        members = [DATA]
    else:
        members = [f"{DATA}/{name}" for name in PARTIAL_HANDOVER_MEMBERS
                   if (app_root / DATA / name).exists()]
    _pack(app_root, archive, members, starts_after_stop=not everything)
    return archive


def seven_zip_excludes() -> list:
    """The switches that leave out of every archive what `HANDOVER_EXCLUDED_PATHS`,
    `HANDOVER_EXCLUDED_NAMES` and `DISCARDED_DIRECTORIES` name. A wildcard that ends with a
    slash matches a directory and no file."""
    return ([f"-x!{path}" for path in HANDOVER_EXCLUDED_PATHS]
            + [f"-xr!{name}" for name in HANDOVER_EXCLUDED_NAMES]
            + [f"-xr!{name}/" for name in DISCARDED_DIRECTORIES])


def _pack(app_root: Path, archive: Path, members: list, starts_after_stop: bool = True) -> None:
    """Pack `members` under a temporary name, and rename the archive only after 7-Zip exits 0.

    Raises `PackingStopped` when a Stop ended 7zz, or kept it from starting where
    `starts_after_stop` is False (`_run_7z`), and RuntimeError for any other exit.
    """
    partial = archive.with_name(archive.name + PARTIAL_SUFFIX)
    code, output = _run_7z(["a", "-y", "-mx=3", "-snl", str(partial), *members,
                            *seven_zip_excludes()], app_root, starts_after_stop=starts_after_stop)
    if code != 0:
        partial.unlink(missing_ok=True)
        if _stop_signal and code in {128 + signum for signum in STOP_SIGNALS}:
            raise PackingStopped(f"a Stop (signal {_stop_signal}) ended 7zz")
        for line in output.splitlines():
            if line.strip():
                log_error(f"7zz printed a line while packing {archive.name} ({line}).")
        raise RuntimeError(f"7zz exited {code}, with the output ending in "
                           f"{output.strip()[-SEVEN_ZIP_TAIL_CHARS:]}")
    sync_file(partial)
    os.replace(partial, archive)
    sync_directory(archive.parent)


def record_checksum(archive: Path) -> None:
    """Write the SHA-256 of a packed archive to `<archive>.sha256` beside it, and log it.

    The file has the format that `sha256sum -c` and `shasum -a 256 -c` read, so that a download
    can be checked, before it is extracted, against the archive that the entrypoint packed. When
    `HANDOVER_RECORD` or `TREES_RECORD` names the archive, the record is written before this
    function is called, so that a kill during the hashing leaves the record in place. The file is
    written under a temporary name and renamed, and a kill leaves no file with a wrong checksum.
    A failure fails no handover, because the archive is complete without the file.
    """
    path = archive.with_name(archive.name + CHECKSUM_SUFFIX)
    partial = path.with_name(path.name + PARTIAL_SUFFIX)
    try:
        digest = hashlib.sha256()
        with open(archive, "rb") as handle:
            for block in iter(lambda: handle.read(CHECKSUM_BLOCK), b""):
                digest.update(block)
        partial.write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="utf-8")
        sync_file(partial)
        os.replace(partial, path)
        sync_directory(path.parent)
    except OSError as error:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        log_warning(f"Could not record the SHA-256 of {archive.name} ({error}). The archive is "
                    f"complete without {path.name}.")
        return
    log(f"The SHA-256 of {archive.name} is {digest.hexdigest()}, recorded in {path.name} beside "
        f"it.")


def tree_bytes(path: Path, skip: Path = None) -> int:
    """The bytes of every file under `path`, links counted as themselves, without the directory
    `skip`."""
    total = 0
    for dirpath, dirs, files in os.walk(path):
        if skip is not None:
            dirs[:] = [d for d in dirs if Path(dirpath, d) != skip]
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def has_room(handover_dir: Path, size: int) -> tuple:
    """Whether the handover volume has room to pack `size` bytes, and its free bytes.

    The function requires twice the size, because the cloning step stops when the volume runs
    low, and packing the sample onto that volume would fill it. 7zz's archive is smaller than
    what it packs, and twice the size leaves at least as much free space on the volume as the
    archive takes.
    """
    free = shutil.disk_usage(handover_dir).free
    return free >= 2 * size, free


def room_for_everything(app_root: Path, handover_dir: Path) -> bool:
    """Whether the handover volume has room for the archive of the whole data root, with a log
    message when it has not."""
    size = tree_bytes(app_root / DATA, skip=app_root / SCRATCH)
    room, free = has_room(handover_dir, size)
    if not room:
        log(f"Did not pack the whole data root ({size / 1024 ** 3:.1f} GiB), because the "
            f"handover volume has {free / 1024 ** 3:.1f} GiB free, less than twice that. Packing "
            f"{WITHOUT_THE_SAMPLE} instead. The data root remains in the project folder.")
    return room


def left_out(relative: str, name: str, directory: bool) -> bool:
    """Whether every packing leaves out the entry at `relative` under the app root, named
    `name`, as `seven_zip_excludes` tells 7zz to. `directory` is True for a directory that is not
    a link."""
    return (name in HANDOVER_EXCLUDED_NAMES or (directory and name in DISCARDED_DIRECTORIES)
            or any(fnmatch.fnmatchcase(relative, path) for path in HANDOVER_EXCLUDED_PATHS))


def trees_listing(app_root: Path, members: list) -> tuple:
    """The bytes of what a packing of `members` takes, links counted as themselves, and a
    fingerprint of it: the path and the kind of every entry, the size and the modification time
    of every file, and the target of every link.

    The time of a directory is left out, because DSM changes it when it writes an `@eaDir` into
    the directory, which no packing takes, and 7zz reads the files below it rather than the time.
    """
    digest = hashlib.sha256()
    total = 0
    for member in sorted(members):
        digest.update(json.dumps(["member", member]).encode("ascii") + b"\n")
        for dirpath, dirs, files in os.walk(app_root / member):
            here = Path(dirpath)
            kept = set()
            for name in sorted(dirs + files):
                path = here / name
                try:
                    st = os.lstat(path)
                    target = os.readlink(path) if stat.S_ISLNK(st.st_mode) else None
                except OSError:
                    continue
                directory = stat.S_ISDIR(st.st_mode)
                relative = path.relative_to(app_root).as_posix()
                if left_out(relative, name, directory):
                    continue
                kept.add(name)
                if directory:
                    entry = ["directory", relative]
                elif target is not None:
                    total += st.st_size
                    entry = ["link", relative, target]
                else:
                    total += st.st_size
                    entry = ["file", relative, st.st_size, st.st_mtime_ns]
                digest.update(json.dumps(entry).encode("ascii") + b"\n")
            dirs[:] = [name for name in dirs if name in kept]
    return total, digest.hexdigest()


def recorded_trees(data: Path, handover_dir: Path, fingerprint: str) -> "str | None":
    """The archive of the collected and validated trees that `TREES_RECORD` names, when it is in
    the handover directory and its fingerprint is `fingerprint`, or None.

    For a record that is missing or damaged, or that names an archive that you deleted, the
    function returns None, and the start packs the trees.
    """
    try:
        record = json.loads((data / TREES_RECORD).read_text(encoding="utf-8"))
        archive, packed = record["archive"], record["fingerprint"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if (packed != fingerprint or not isinstance(archive, str) or Path(archive).name != archive
            or not archive.endswith("-trees.7z") or not (handover_dir / archive).is_file()):
        return None
    return archive


def pack_trees(app_root: Path, handover_dir: Path, outcome: str, when: str) -> "Path | None":
    """Pack the collected and validated trees of a run that did not complete into a second archive.

    Returns the archive, or None when there is no tree, when an archive in the handover
    directory contains the trees as they are (`recorded_trees`), or when the handover volume has
    less than twice the trees' size free. A log message states the last two cases. The trees
    stay in the project folder either way. After a packing, the function writes `TREES_RECORD`,
    and a record that cannot be written makes the next start pack the trees again.
    """
    trees = [f"{DATA}/{tree}" for tree in SAMPLE_TREES if (app_root / DATA / tree).is_dir()]
    if not trees:
        return None
    size, fingerprint = trees_listing(app_root, trees)
    held = recorded_trees(app_root / DATA, handover_dir, fingerprint)
    if held is not None:
        log(f"Did not pack the collected and validated trees again, because {held} in the "
            f"handover directory contains them as they are.")
        return None
    room, free = has_room(handover_dir, size)
    if not room:
        log(f"Did not pack the collected and validated trees ({size / 1024 ** 3:.1f} GiB), "
            f"because the handover volume has {free / 1024 ** 3:.1f} GiB free, less than twice "
            f"that. The trees remain in the project folder.")
        return None
    archive = handover_dir / f"agentcontext-handover-{when}-{outcome}-trees.7z"
    _pack(app_root, archive, trees)
    try:
        write_record(app_root / DATA / TREES_RECORD,
                     {"archive": archive.name, "fingerprint": fingerprint})
    except OSError as error:
        log_warning(f"Could not write {TREES_RECORD} ({error}). The next Start that packs the "
                    f"collected and validated trees packs them again.")
    return archive


def _proc_value(path: str, key: str) -> "str | None":
    """The value of `key` in a Linux /proc text file of `key: value` lines, or None."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                name, _, value = line.partition(":")
                if name.strip() == key:
                    return value.strip()
    except OSError:
        return None
    return None


def _filesystem_of(path: Path) -> "str | None":
    """The filesystem type of the mount that contains `path`, from /proc/mounts, or None."""
    try:
        device = os.stat(path).st_dev
        with open("/proc/mounts", encoding="utf-8", errors="replace") as handle:
            mounts = [line.split() for line in handle]
    except OSError:
        return None
    for fields in mounts:
        if len(fields) < 3:
            continue
        try:
            if os.stat(fields[1]).st_dev == device:
                return fields[2]
        except OSError:
            continue
    return None


def _cgroup_memory_limit() -> "int | None":
    """The container's memory limit in bytes when one is set, else None."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            text = Path(path).read_text().strip()
        except OSError:
            continue
        if text.isdigit() and int(text) < 1 << 60:
            return int(text)
        return None
    return None


# The container's cgroup, which a `cpus:` limit in compose.yaml or `docker run --cpus` sets a CPU
# quota on. `os.cpu_count()` counts the processors of the host, also under such a quota.
CGROUP = Path("/sys/fs/cgroup")


def _cgroup_cpu_quota(cgroup: Path) -> "float | None":
    """The container's CPU quota in processors when the cgroup sets one, else None: the quota
    divided by the period, from `cpu.max` of cgroup v2, or from `cpu.cfs_quota_us` and
    `cpu.cfs_period_us` of cgroup v1, in which a quota of -1 sets none."""
    try:
        quota, period = (cgroup / "cpu.max").read_text().split()
    except (OSError, ValueError):
        try:
            quota = (cgroup / "cpu" / "cpu.cfs_quota_us").read_text().strip()
            period = (cgroup / "cpu" / "cpu.cfs_period_us").read_text().strip()
        except OSError:
            return None
    if not (quota.isdigit() and period.isdigit()) or int(period) == 0:
        return None
    return int(quota) / int(period)


def _process_limit() -> "int | None":
    """The container's process ceiling when the cgroup sets one, else None."""
    for path in ("/sys/fs/cgroup/pids.max", "/sys/fs/cgroup/pids/pids.max"):
        try:
            text = Path(path).read_text().strip()
        except OSError:
            continue
        return int(text) if text.isdigit() else None
    return None


def _proc_int(path: str) -> "int | None":
    """One integer from a /proc file, or None where there is none."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def _nproc_limit() -> "int | None":
    """RLIMIT_NPROC, the processes and threads that one user may run, when it is finite."""
    try:
        import resource
        soft, _hard = resource.getrlimit(resource.RLIMIT_NPROC)
    except (ImportError, ValueError, OSError):
        return None
    return None if soft == resource.RLIM_INFINITY else soft


def record_heuristics(state: Path) -> int:
    """Make the run's log directory hold copies of the heuristics files of the image and no
    other file, and return how many it holds.

    The image determines the heuristics, because it contains the code and the heuristics files
    and the mounts contain the data. Without these copies, a handover read months later cannot
    show which heuristics produced the output CSVs beside it. The copies of an earlier image are
    removed first, because a file that the image lacks would be in the handover as if this run
    had read it. A file or a link in place of the directory is left as it is, and creating the
    directory then raises. A missing directory in the image is not an error: the copies cannot
    record the heuristics of an image built without them, and that is no reason to stop the run.
    """
    source = IMAGE_ROOT / "heuristics"
    target = state / HEURISTICS_RECORD
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    if not source.is_dir():
        return 0
    target.mkdir(parents=True)
    copied = 0
    for path in sorted(source.iterdir()):
        if path.is_file():
            shutil.copy2(path, target / path.name)
            copied += 1
    return copied


# The programs that the steps run, and the command that prints the version of each. git
# determines values in published columns through its own rename and copy detection:
# `git log --follow --reverse` lists only a renamed file's rename commit, and `--follow` crosses
# a copy, the reason for `history.renames_only` (docs/git.md). `deploy/install_tools.sh` links
# the versions that mise.toml pins into the image, and the record shows the versions that a run
# used. 7-Zip prints its version when it runs without arguments.
TOOL_VERSION_COMMANDS = {"git": ["git", "--version"], SEVEN_ZIP: [SEVEN_ZIP],
                         "rsync": ["rsync", "--version"]}


def tool_versions() -> dict:
    """The first line that each program prints about its version, or None where it could not run."""
    found = {}
    for name, command in TOOL_VERSION_COMMANDS.items():
        try:
            output = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                    timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            found[name] = None
            continue
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        found[name] = lines[0] if lines else None
    return found


def machine_record(state: Path, when: str, image: str) -> dict:
    """The machine that the run uses: the processor, the container's CPU quota, the memory, the
    volume, the kernel, the image, and the versions of git, 7zz and rsync.

    The values are read from what is visible inside a container, which includes the processor's
    model but not the machine's. The function does not measure the uplink: the rows of
    `data/collected/repos.csv` with a `scanned_at` after the start of the collection show it as
    repositories finished per hour. The start is in the name of the latest collection that a line
    of `finished_repositories.jsonl` names. A row taken over from an earlier collection keeps
    that collection's `scanned_at`.
    """
    memory_kb = _proc_value("/proc/meminfo", "MemTotal")
    usage = shutil.disk_usage(state)
    return {
        "recorded_at": when,
        "hostname": platform.node(),
        "kernel": platform.release(),
        "architecture": platform.machine(),
        "cpu_model": _proc_value("/proc/cpuinfo", "model name"),
        "cpus": os.cpu_count(),
        "cpu_quota": _cgroup_cpu_quota(CGROUP),
        "memory_total_bytes": int(memory_kb.split()[0]) * 1024 if memory_kb else None,
        "memory_limit_bytes": _cgroup_memory_limit(),
        # The process limits, so that a fork() that returns EAGAIN can be traced to the limit
        # that it hit.
        "process_limit": _process_limit(),
        "nproc_limit": _nproc_limit(),
        "pid_max": _proc_int("/proc/sys/kernel/pid_max"),
        "threads_max": _proc_int("/proc/sys/kernel/threads-max"),
        # Whether an init process is process 1. This script reaps only the runner that it
        # starts. The compose file sets `init: true` so that an init process reaps any process
        # left without a parent, and a plain `docker run` needs `--init` for the same purpose.
        "init_process": os.getpid() != 1,
        "state_volume": {"path": str(state), "filesystem": _filesystem_of(state),
                         "total_bytes": usage.total, "free_bytes": usage.free},
        "image": image,
        "tools": tool_versions(),
    }


def machine_line(record: dict) -> str:
    """The machine record as one log message. The versions that the programs print are quoted in
    parentheses at the end."""
    gib = lambda value: f"{value / 1024 ** 3:.1f} GiB" if value else "unknown"  # noqa: E731
    volume = record["state_volume"]
    quota = record.get("cpu_quota")
    memory = (f"{gib(record['memory_total_bytes'])} of memory" if record["memory_total_bytes"]
              else "an unknown amount of memory")
    limit = record["memory_limit_bytes"]
    versions = series(version or f"{name} not found"
                      for name, version in record.get("tools", {}).items())
    return (f"The machine {record['hostname']} has {record['cpus']} CPUs "
            f"({record['cpu_model'] or 'model unknown'}), "
            f"{'no CPU quota' if quota is None else f'a CPU quota of {quota:g}'} and {memory}"
            f"{f' with a limit of {gib(limit)}' if limit else ''}. Its process limits are "
            f"{record['process_limit'] or 'none'} in the cgroup, "
            f"{record['nproc_limit'] or 'none'} for nproc, {record['pid_max'] or 'unknown'} for "
            f"pid_max and {record['threads_max'] or 'unknown'} for threads-max. "
            f"{'An' if record['init_process'] else 'No'} init process is present. "
            f"The data root is on {volume['filesystem'] or 'an unknown filesystem'} with "
            f"{gib(volume['free_bytes'])} free of {gib(volume['total_bytes'])}. The kernel is "
            f"{record['kernel']} on {record['architecture']}. The programs that the steps run "
            f"print these versions ({versions}).")


def wants_listing(argv: list) -> bool:
    return any(arg in LISTING_FLAGS for arg in argv)


# The record of the last GHS download: the retrieval that it took, or the retrieval that it
# attempted and the previous retrieval that it fell back to. A monthly run begins with the
# attempt, and its age is the age of the attempt. `ghs_retrieval.json` names the retrieval that the
# stage reads, which after a fallback is the previous retrieval. An age taken from that file would
# make every Start after a fallback begin a new monthly run.
RETRIEVAL_REPORT = "sampling/ghs_retrieval_report.json"
# The NAS's time zone file, which compose.yaml mounts read-only at a path of its own. A Start after
# a completed run begins the next monthly run when the current calendar month is later than the
# month in which the run's GHS download started, both in the zone of this file (`next_month_due`).
# DSM's Task Scheduler starts the container at a time of the NAS's zone, and in a zone east of UTC,
# midnight on the first of a month is still the last day of the month before in UTC. Because only
# the calendar month counts, the scheduled Start on the first of a month begins that month's run
# however late in the previous month the previous run began, for example after a Start that the
# pre-checks refused. A Start within the month, such as the Start that loads a new image,
# continues the completed run, in which the runner runs again the steps whose code, code inputs
# or heuristics the new image changes. A labeling that runs again then completes its finished
# run without labeling again when the current rules derive each label unchanged, and the
# collection stays (docs/runner.md, "A new image during or after the collection").
#
# The file is mounted beside the image's /etc/localtime, not over it, and the container's own
# local time stays UTC. The image's /etc/localtime is a link to /usr/share/zoneinfo/Etc/UTC, and
# Docker mounts a file over the target of such a link. A mount over /etc/localtime would therefore
# put the NAS's zone into the zone file of UTC. Every program that reads the time in UTC, such as
# the `git log` with TZ=UTC that writes the commit times of the cloning step, would then write
# the NAS's local time with a Z.
HOST_LOCALTIME = Path("/etc/host-localtime")
# The container's own zone files: /etc/localtime, which the image links to the zone file of UTC,
# and the zone file that `TZ=UTC` reads, under which the cloning step runs `git log` to write the
# commit times. A compose.yaml that mounts the NAS's zone file at /etc/localtime puts that zone
# into the zone file of UTC, and every commit time would then be the NAS's local time with a Z
# (`container_problems`).
LOCALTIME = Path("/etc/localtime")
UTC_ZONE_FILE = Path("/usr/share/zoneinfo/UTC")
# The variable that compose.yaml sets to its revision, and the revision that this script
# requires. Container Manager keeps the compose.yaml from which you created the project when you
# import a new image and build the project again. A new image can therefore run under the
# compose.yaml of an earlier bundle, which can lack a mount or a variable that this script reads,
# such as the NAS's zone file at `HOST_LOCALTIME`. A start under another revision runs nothing.
# The value in compose.yaml and this constant change together whenever compose.yaml changes in a
# way that this script depends on, as deploy/0_README.md states. A test checks that the two are
# equal.
COMPOSE_REVISION_VARIABLE = "AGENTCONTEXT_COMPOSE_REVISION"
COMPOSE_REVISION = 3
# The steps that the next monthly run redoes: the download, which takes a new retrieval, and the
# cloning step, which does not depend on the download in the list of steps. A download that falls
# back to the previous sampling frame leaves the list of the sample unchanged, but the collection
# still has to collect the repositories with a changed head commit.
NEW_RUN_FROM = ("ghs_download", "repository_cloning")


def utc_now() -> datetime:
    """The current time in UTC, which the decision on the next monthly run reads."""
    return datetime.now(timezone.utc)


def read_zone(path: Path) -> tzinfo:
    """The zone of a time zone file in the TZif format, or raise OSError or ValueError.

    `zoneinfo` reads the last line of a file of version 2 or later up to its newline and does
    not return when the file ends before the newline. The function therefore refuses such a
    file first.
    """
    data = path.read_bytes()
    if data[:4] == b"TZif" and data[4:5] != b"\0" and not data.endswith(b"\n"):
        raise ValueError("the file ends inside its last line")
    try:
        return ZoneInfo.from_file(io.BytesIO(data))
    except (struct.error, AssertionError) as error:
        raise ValueError(f"the file is cut short ({error})") from error


def host_zone() -> tuple:
    """The zone of `HOST_LOCALTIME` and None, or UTC and why that file gives no zone.

    A directory at the path, which Docker creates for a bind mount of a file that the host does
    not have, counts as no file.
    """
    if not HOST_LOCALTIME.is_file():
        return timezone.utc, f"there is no time zone file at {HOST_LOCALTIME}"
    try:
        return read_zone(HOST_LOCALTIME), None
    except (OSError, ValueError) as error:
        return timezone.utc, f"{HOST_LOCALTIME} cannot be read as a time zone file ({error})"


def month_zone() -> tzinfo:
    """The zone in which this start compares calendar months: the NAS's, which compose.yaml
    mounts at `HOST_LOCALTIME`, or UTC when that file is missing or cannot be read.

    The zone is read from the file rather than set as the container's local time, so that every
    other program in the container keeps reading the time in UTC.
    """
    return host_zone()[0]


def log_month_zone(now: datetime) -> None:
    """Log the zone in which this start compares calendar months, its offset at `now` and where
    it comes from, with a warning when `HOST_LOCALTIME` gives no zone."""
    zone, problem = host_zone()
    local = now.astimezone(zone)
    offset = local.strftime("%z")
    if problem is None:
        log(f"Calendar months are counted in {local.tzname()} (UTC{offset[:3]}:{offset[3:]}), "
            f"the time zone of {HOST_LOCALTIME}.")
    else:
        log_warning(f"Calendar months are counted in UTC, because {problem}. Mount the NAS's "
                    f"/etc/localtime read-only at {HOST_LOCALTIME}, as compose.yaml does.")


def next_month_due(taken: datetime, now: datetime) -> bool:
    """Whether a Start at `now` begins the next monthly run after a GHS download that started
    at `taken`: the calendar month of `now` is later than that of `taken`, both in the zone of
    `month_zone`, the NAS's time zone in the container that compose.yaml defines.

    The months are compared rather than an age, because the Starts of a monthly schedule are
    28 to 31 days apart, and a run that began some days into a month, or late in the month after
    a refused Start, would otherwise be too young at the next scheduled Start, which would then
    skip its month. They are compared in local time, because DSM's Task Scheduler starts the
    container at a time of the NAS's zone.
    """
    zone = month_zone()
    taken, now = taken.astimezone(zone), now.astimezone(zone)
    return (now.year, now.month) > (taken.year, taken.month)


def compose_problem(env: dict) -> "str | None":
    """Why the compose.yaml that the container was created from is not of the revision that this
    script requires (`COMPOSE_REVISION`), or None."""
    given = env.get(COMPOSE_REVISION_VARIABLE)
    if given == str(COMPOSE_REVISION):
        return None
    found = (f"sets no {COMPOSE_REVISION_VARIABLE}" if given is None
             else f"sets {COMPOSE_REVISION_VARIABLE} to {given!r}")
    return (f"The compose.yaml that created the container {found}, but this image requires "
            f"revision {COMPOSE_REVISION}. No step ran. Copy compose.yaml and deploy.md from the "
            f"bundle of the same build as the image into the project folder, add again what you "
            f"had added to its `command:`, and build the project again (Container Manager, "
            f"Project, the project, Action, Build).")


def tz_offsets(value: str, instants: tuple) -> list:
    """The offsets from UTC in seconds that the C library gives at each of `instants` under the
    TZ variable `value`.

    The function uses the C library rather than `zoneinfo`, because the steps read their local
    time through the C library. The C library takes `value` as the name of a zone file, relative
    to the zone database or absolute, and as a POSIX rule when no such file can be read. The
    function sets the variable in this process for the measurement and restores it afterwards.
    """
    saved = os.environ.get("TZ")
    os.environ["TZ"] = value
    time.tzset()
    try:
        return [time.localtime(instant.timestamp()).tm_gmtoff for instant in instants]
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def utc_offset(seconds: int) -> str:
    """An offset from UTC in seconds as `UTC+01:00`."""
    sign, minutes = ("-" if seconds < 0 else "+"), abs(seconds) // 60
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def container_zone_problem(env: dict, now: datetime) -> "str | None":
    """Why the container's own local time is not UTC, or None.

    The cloning step runs `git log` under `TZ=UTC` and writes the commit times with a Z, and the
    other steps write their times in the container's local time, which the image sets to UTC. A
    TZ variable with another zone changes the local time of every step, and a mount over
    /etc/localtime puts another zone into the zone file of UTC, which `TZ=UTC` reads as well. The
    function reads the zone of the TZ variable (`tz_offsets`) and of each zone file at the first
    of January and the first of July of the year of `now` and at `now`, so that a zone with
    daylight saving time shows a nonzero offset at one of them. A TZ variable with offset zero at
    all three, such as `UTC` or
    `Etc/UTC`, writes the same times as no variable. A file that is missing or is not a zone file
    is no problem, because the C library then takes UTC.
    """
    instants = (datetime(now.year, 1, 1, tzinfo=timezone.utc),
                datetime(now.year, 7, 1, tzinfo=timezone.utc), now)
    if "TZ" in env:
        for instant, seconds in zip(instants, tz_offsets(env["TZ"], instants)):
            if seconds:
                return (f"The environment of the container sets TZ={env['TZ']}, which gives the "
                        f"offset {utc_offset(seconds)} on {instant:%Y-%m-%d}. No step ran, "
                        f"because the container's own local time has to be UTC. Copy "
                        f"compose.yaml from the bundle of the same build as the image, which sets "
                        f"no TZ and mounts the NAS's time zone at {HOST_LOCALTIME}, and build the "
                        f"project again.")
    for path in (LOCALTIME, UTC_ZONE_FILE):
        if not path.is_file():
            continue
        try:
            zone = read_zone(path)
        except (OSError, ValueError):
            continue
        for instant in instants:
            local = instant.astimezone(zone)
            if local.utcoffset():
                offset = local.strftime("%z")
                return (f"{path} gives the offset UTC{offset[:3]}:{offset[3:]} on "
                        f"{instant:%Y-%m-%d}, because compose.yaml mounts a file over the zone "
                        f"files of the image. No step ran, because that offset shifts every "
                        f"commit time that the cloning step writes as UTC. Mount the NAS's "
                        f"/etc/localtime only at {HOST_LOCALTIME}, as the compose.yaml of the "
                        f"bundle does, and build the project again.")
    return None


def container_problems(env: dict, now: datetime) -> list:
    """Why the container that compose.yaml defines cannot run this image, as messages: a
    compose.yaml of another revision (`compose_problem`) and a local time that is not UTC
    (`container_zone_problem`)."""
    return [problem for problem in (compose_problem(env), container_zone_problem(env, now))
            if problem]


def names_from_or_mark(argv: list) -> bool:
    """Whether the arguments name `--from` or `--mark-complete`, which the entrypoint passes as
    the operator wrote them until a run has used them (`settle_one_start_arguments`)."""
    return any(arg == flag or arg.startswith(flag + "=")
               for arg in argv for flag in ONE_START_FLAGS)


# The record of the arguments for one Start that the `command:` of compose.yaml names and that a
# run has used. The runbook gives `--from` and `--mark-complete` for one Start, and likewise the
# `--fresh` of a `--step-arg` value, which deletes the collection and collects the whole sample
# again, its `--accept-smaller`, with which the GHS download takes a retrieval that is far
# smaller than the retrieval before it, and its `--end-date YYYY-MM-DD`, with which the GHS
# download takes that end date of the sampling window. Left in `command:`, each would act again
# at every later Start. A `--from` would run its steps again and keep every later monthly run from
# beginning, a `--fresh` would collect the whole sample again in every monthly run, an
# `--accept-smaller` would take every later retrieval however many rows GHS lost, and an
# `--end-date` would keep the sampling window of every later monthly run where it was. A start
# therefore passes such an argument to the runner until a run has used it, and leaves it out with
# a warning at every later start while `command:` still names it. A start with a `command:` that does not name the argument
# removes it from the record, so that removing it and adding it again passes it again. Every
# other argument, such as `--with-commit-reclassification` or a `--step-arg` value without such a
# word, is a setting that every start passes. The flags and the words are recognized by their
# full names. The runner and its steps take long options by their full names only
# (`agentcontext.cli.ArgumentParser`), and an abbreviation such as `--fro`, which would otherwise
# act at every start, is a usage error of the runner at the first start.
ONE_START_RECORD = "run_logs/one_start_arguments.json"
# The list of finished repositories of the collection, relative to the data root, which a
# `--fresh` of the cloning step removes before the step collects anything
# (`resume.fresh_start`). A change of the list therefore shows that the `--fresh` took effect.
# The path is `collection/` and `resume.FINISHED_NAME`, repeated rather than imported, because
# this script has to run even when the package cannot be imported. A test checks that the two
# are equal.
FINISHED_LIST = "collection/finished_repositories.jsonl"
ONE_START_FLAGS = ("--from", "--mark-complete")
STEP_ARG = "--step-arg"
ONE_START_STEP_WORDS = ("--fresh", "--accept-smaller")
# The option of a `--step-arg` value that is an argument for one Start with any value. Its words
# are joined into one, `--end-date=VALUE`, also when the value follows as the next word.
END_DATE_OPTION = "--end-date"
END_DATE_WORD = END_DATE_OPTION + "="


def one_start_word(word: str) -> bool:
    """Whether a word of a `--step-arg` value is an argument for one Start: a word of
    `ONE_START_STEP_WORDS` or an `--end-date=` with its value."""
    return word in ONE_START_STEP_WORDS or word.startswith(END_DATE_WORD)


# The outcomes of a runner that ended its run before any step started, after which a start passes
# its arguments for one Start again: a refusal by the pre-checks, a refusal by the check for
# modified inputs, and a stop before the first step. A `--from` that the stopped runner applied acts
# again in the next run, which removes the same steps from the completion records. A test checks
# that they are outcomes of `runner.NO_STEP_OUTCOMES`.
REFUSED_OUTCOMES = ("pre_checks_failed", "modified_inputs", "stopped_before_steps")


def _options(argv: list) -> list:
    """Each `--from`, `--mark-complete` and `--step-arg` of `argv` as (flag, value, position,
    width), the width 1 for `--flag=value` and 2 for `--flag value`."""
    found, index = [], 0
    while index < len(argv):
        width = 1
        for flag in (*ONE_START_FLAGS, STEP_ARG):
            if argv[index].startswith(flag + "="):
                found.append((flag, argv[index][len(flag) + 1:], index, 1))
                break
            if argv[index] == flag and index + 1 < len(argv):
                found.append((flag, argv[index + 1], index, 2))
                width = 2
                break
        index += width
    return found


def _step_words(value: str) -> tuple:
    """The step and the words of a `--step-arg` value, as the runner splits them, with an
    `--end-date` and its value joined into one word, or no words when the value cannot be split,
    which the runner then refuses."""
    name, _, rest = value.partition(":")
    try:
        words = shlex.split(rest)
    except ValueError:
        return name, []
    joined, index = [], 0
    while index < len(words):
        if words[index] == END_DATE_OPTION and index + 1 < len(words):
            joined.append(END_DATE_WORD + words[index + 1])
            index += 2
        else:
            joined.append(words[index])
            index += 1
    return name, joined


def one_start_arguments(argv: list) -> list:
    """The arguments for one Start that `argv` names, each as [flag, value]: every `--from` and
    `--mark-complete` with its value, and [`--step-arg`, NAME:WORD] for every word of a
    `--step-arg` value of the step NAME that `one_start_word` accepts, such as `--fresh`."""
    found = []
    for flag, value, _index, _width in _options(argv):
        if flag in ONE_START_FLAGS:
            argument = [flag, value]
            found += [] if argument in found else [argument]
        else:
            name, words = _step_words(value)
            found += [[STEP_ARG, f"{name}:{word}"] for word in words
                      if one_start_word(word) and [STEP_ARG, f"{name}:{word}"] not in found]
    return found


def without_arguments(argv: list, arguments: list) -> list:
    """`argv` without the arguments for one Start in `arguments`. A `--step-arg` value keeps its
    other words, and a value without other words is left out with its flag."""
    options = {index: (flag, value, width) for flag, value, index, width in _options(argv)}
    kept, index = [], 0
    while index < len(argv):
        flag, value, width = options.get(index, (None, None, 1))
        if flag in ONE_START_FLAGS and [flag, value] in arguments:
            index += width
            continue
        if flag == STEP_ARG:
            name, words = _step_words(value)
            rest = [word for word in words if [STEP_ARG, f"{name}:{word}"] not in arguments]
            if rest != words:
                if rest:
                    left = f"{name}:{shlex.join(rest)}"
                    kept += [f"{flag}={left}"] if width == 1 else [flag, left]
                index += width
                continue
        kept += argv[index:index + width]
        index += width
    return kept


def named(arguments: list) -> str:
    """Arguments for one Start as the `command:` of compose.yaml names them."""
    return series(f"{flag} {value}" for flag, value in arguments)


def _argument_pairs(value) -> list:
    """The [flag, value] pairs of a list read from the record, without what is not such a pair."""
    if not isinstance(value, list):
        return []
    return [list(item) for item in value if isinstance(item, list) and len(item) == 2
            and all(isinstance(part, str) for part in item)]


def read_one_start_record(data: Path) -> dict:
    """The record of the arguments for one Start, or an empty dict where it is missing or
    damaged: `used`, the arguments that a run used, and `passed` with `state_before` and
    `completed_before`, the arguments that a start passed to the runner, the signature of the
    pipeline execution state before that runner started (`state_signature`) and the steps that
    the state recorded as completed then (`completed_names`)."""
    try:
        record = json.loads((data / ONE_START_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def completed_names(data: Path) -> list:
    """The steps that the pipeline execution state records as completed, sorted."""
    completed = (read_state(data) or {}).get("completed")
    if not isinstance(completed, dict):
        return []
    return sorted(name for name in completed if isinstance(name, str))


def took_effect(argument: list, data: Path, state_before: list,
                completed_before: list = (), retrieval_before: "str | None" = None,
                finished_before: "list | None" = None) -> bool:
    """Whether the runner to which a start passed `argument` recorded its effect in the
    pipeline execution state. Before that runner started, `state_before` was the signature of
    the state, the state recorded the steps of `completed_before` as completed, the record of
    the GHS download named `retrieval_before` as the retrieval that a download took
    (`taken_retrieval`), and the list of finished repositories had the identity
    `finished_before` (`finished_list_identity`).

    The runner takes the steps that `--from` names out of its record of completed steps after the
    last check that can refuse the run, and writes that record before the first step that it runs,
    or with the outcome `interrupted` when a Stop arrives before that step. A `--from` has therefore
    taken effect when a step ran or when a step of `completed_before` is no longer recorded as
    completed. A check that stops the run before that point, such as a git that cannot start while
    the runner fingerprints the cloning step, writes the outcome `failed` with the record unchanged
    and no step run. The refusal by the run lock, the refusals by the pre-checks and by the
    check for modified inputs, and a second signal during the pre-checks leave the record
    unchanged too, and the next start passes the argument again. `--mark-complete` writes the
    outcome `marked_complete`.

    A `--fresh` takes effect when its step removes the list of finished repositories, the first
    thing that the step deletes before it collects again. The `--fresh` therefore counts as used
    when its step ran and the list is gone or is another file. The step results record the step
    when it ends, and the runner names the step that it has started and that has not ended as
    `running_step`, which a kill of the runner inside that step leaves in the state. A start
    after the kill that runs no step leaves the name too, and the step counts only for the start
    that named it (`step_of_the_latest_start`). A step that failed to start, exited before it
    removed the list or was stopped while its interpreter started has a result too, and the
    unchanged list shows that its `--fresh` deleted nothing.

    An `--accept-smaller` and an `--end-date=` take effect when the record of the GHS download
    names a retrieval that a download took and that the record did not name before the runner
    started. A download
    that failed, was stopped or was killed before it wrote its record took no retrieval. Neither
    did a download that fell back to the previous sampling frame, which exits 0 after a fallback
    for any reason, such as a GHS that does not respond, and the download that a later start
    runs would refuse the smaller GHS search result without the argument. The step results do
    not decide this: a kill after the download exited 0 and before the runner recorded the step
    leaves no result, and the next start records the step from the runner's `exited` record
    without running it. The argument would then stay in `command:` and let every later download
    take a smaller GHS search result.
    """
    if list(state_signature(data)) == state_before:
        return False
    state = read_state(data) or {}
    outcome = state.get("outcome")
    if not isinstance(outcome, str) or outcome in REFUSED_OUTCOMES:
        return False
    flag, value = argument
    if flag == "--mark-complete":
        return outcome == "marked_complete"
    steps = state.get("steps")
    results = ([result for result in steps if isinstance(result, dict)]
               if isinstance(steps, list) else [])
    ran = {result.get("name") for result in results
            if result.get("status") not in (None, "skipped")}
    if (running := step_of_the_latest_start(state)) is not None:
        ran.add(running)
    if flag == STEP_ARG:
        name, _, word = value.partition(":")
        if word == "--accept-smaller" or word.startswith(END_DATE_WORD):
            taken = taken_retrieval(data)
            return taken is not None and taken != retrieval_before
        if word == "--fresh":
            return name in ran and finished_list_identity(data) != finished_before
        return name in ran
    completed = state.get("completed") if isinstance(state.get("completed"), dict) else {}
    return bool(ran) or any(name not in completed for name in completed_before)


def used_arguments(data: Path) -> list:
    """The arguments for one Start that the record names as used, and the arguments that it
    names as passed and with an effect that the runner recorded (`took_effect`)."""
    record = read_one_start_record(data)
    used = _argument_pairs(record.get("used"))
    before = record.get("state_before")
    listed = record.get("completed_before")
    completed = ([name for name in listed if isinstance(name, str)]
                 if isinstance(listed, list) else [])
    retrieval = record.get("retrieval_before")
    retrieval = retrieval if isinstance(retrieval, str) else None
    finished = record.get("finished_before")
    finished = finished if isinstance(finished, list) else None
    if isinstance(before, list):
        used += [argument for argument in _argument_pairs(record.get("passed"))
                 if argument not in used
                 and took_effect(argument, data, before, completed, retrieval, finished)]
    return used


def write_one_start_record(data: Path, used: list, passed: list = (),
                           state_before: tuple = (), completed_before: list = (),
                           retrieval_before: "str | None" = None,
                           finished_before: "list | None" = None) -> None:
    """Write the record of the arguments for one Start, or remove it when it names none, and log
    a warning when it cannot be written."""
    path = data / ONE_START_RECORD
    record = ({"used": used, "passed": list(passed), "state_before": list(state_before),
               "completed_before": list(completed_before), "retrieval_before": retrieval_before,
               "finished_before": finished_before}
              if passed else {"used": used})
    try:
        if not used and not passed:
            path.unlink(missing_ok=True)
        elif read_one_start_record(data) != record:
            write_record(path, record)
    except OSError as error:
        log_warning(f"Could not write {ONE_START_RECORD} ({error}). A later Start may pass an "
                    f"argument for one Start again, although a run has used it.")


def settle_one_start_arguments(argv: list, data: Path) -> list:
    """`argv` without the arguments for one Start that a run has used, with a warning that names
    them.

    A start that a kill ended before it could record whether its runner used the arguments that
    it passed, for example at a reboot, left them in the record as passed, and the pipeline
    execution state shows whether that runner used them. The record keeps only the used
    arguments that `argv` still names, and a start without them forgets them.
    """
    recorded = used_arguments(data)
    used = [argument for argument in one_start_arguments(argv) if argument in recorded]
    write_one_start_record(data, used)
    if used:
        it, what = (("it", "an argument") if len(used) == 1 else ("them", "arguments"))
        log_warning(f"The `command:` of compose.yaml still names {named(used)}, {what} for "
                    f"one Start that the run of an earlier Start used. Left {it} out. Remove "
                    f"{it} from `command:`. Once a Start has run without {it}, you can add {it} "
                    f"again for another Start.")
    return without_arguments(argv, used)


def run_released(data: Path) -> bool:
    """Whether the last start that ran steps completed, or failed in a step of the analysis stage
    after its release step (`failed_analysis_step`): a run with a complete collection and
    release, which the next monthly run replaces (`monthly_argv`)."""
    return last_run_outcome(data) == "success" or failed_analysis_step(data) is not None


def _releases(steps) -> bool:
    """Whether step results record the release step as done or skipped."""
    return isinstance(steps, list) and any(
        isinstance(step, dict) and step.get("name") == RELEASE_STEP
        and step.get("status") in ("ok", "skipped") for step in steps)


def release_step_completed(data: Path) -> bool:
    """Whether the release step is done in the data root: the step results of the last start
    that ran a step (`LAST_RUN_STEPS`) record it as done or skipped, whatever the outcome of that
    start, and a step that a kill ended (`RUNNING_STEP`) came after it.

    A run with a `--from` of a step before the analysis stage rewrites the outputs of the steps
    that it redoes, such as the release after `--from release`, and `start` therefore packs the
    data root first when no `-complete` archive contains it (`from_arguments`). The release step
    is done after a run that completed or failed in the analysis stage (`run_released`). It is
    also done after a run that a Stop or a kill ended in the analysis stage, with the outcome
    `interrupted` or `running`, which a Start resumes rather than replacing it with the next
    monthly run. A kill before any step of its start ended leaves the step results of the start
    before it in `LAST_RUN_STEPS`. The step results of the killed start up to the step that the
    kill ended (`RUNNING_STEP_RESULTS`) then show whether the release step came before that
    step. After a kill in a cloning step that a new image runs again, it did not, and the data
    root packed after such a kill would be named `-complete` while it holds part of a
    collection. A start after the kill that runs no step, such as a start that a check stops
    before its first step, leaves the name of the step and these step results. Its own step
    results (`steps`) end where the check stopped it, before the release step even after a kill
    in the analysis stage.
    """
    recorded = read_state(data) or {}
    return _releases(recorded.get(LAST_RUN_STEPS)) and (
        recorded.get(RUNNING_STEP) is None or _releases(recorded.get(RUNNING_STEP_RESULTS)))


def from_arguments(argv: list) -> list:
    """The `--from` arguments of `argv` as `--from NAME` pairs, in their order.

    A run with a `--from` redoes the steps that it names and every step computed from them, and
    writes their outputs over the outputs of the run before it. After `--from
    repository_cloning`, these are the collected and validated trees, the output CSVs and the
    release. After the `--from ghs_download --from repository_cloning` that the warnings of
    `monthly_argv` and `warn_after_fallback` name for one Start, they are the data of every
    stage. Before a run with a `--from` in `command:`, such as `--from release`, `start`
    therefore packs the data root first when the release step is done in it
    (`release_step_completed`) and no archive contains it. It packs nothing first when every
    `--from` names the analysis stage or one of its steps (`redoes_only_the_analysis`). Under
    the image that ran the previous run, such a run rewrites only `analysis/` and `run_logs/`,
    which the handover of the previous run contains unless its packing failed. A packing before
    the run would take the room of a second archive of the data root beside the archive that
    the run's own handover packs. A new image, such as an image that repairs a step of the
    analysis stage, also runs again every step that its code or heuristics change, with the
    steps computed from it, without packing first, as at every start with a new image.
    """
    return [arg for flag, value, _index, _width in _options(argv) if flag == "--from"
            for arg in (flag, value)]


def redoes_only_the_analysis(froms: list, data: Path) -> bool:
    """Whether every `--from` of `froms`, the pairs of `from_arguments`, names the analysis stage
    or a step that the step results of the last start that ran a step (`LAST_RUN_STEPS`) list
    after the release step.

    The runner runs the steps in the pipeline's order and stops at the first step that fails.
    After a completed run, these step results therefore list every step of the analysis stage,
    and after a run that failed in that stage, its steps up to the step that failed. A `--from`
    of a step that
    they do not list, such as a later step of the analysis stage or a step that a new image added
    to it, counts as a step before the analysis stage, and `start` packs the data root before its
    run as before a `--from release`. The entrypoint reads the steps from the pipeline execution
    state rather than from `run_pipeline.compose`, because it has to run when the package cannot
    be imported.
    """
    steps = (read_state(data) or {}).get(LAST_RUN_STEPS)
    names = ([step.get("name") for step in steps if isinstance(step, dict)]
             if isinstance(steps, list) else [])
    after = names[names.index(RELEASE_STEP) + 1:] if RELEASE_STEP in names else []
    named = froms[1::2]
    return bool(named) and all(name == ANALYSIS_STAGE or name in after for name in named)


def monthly_argv(argv: list, data: Path, now: datetime) -> tuple:
    """The arguments for this Start, and the log message that explains a change to them, or None.

    After a completed run with a GHS download that makes the next month due (`next_month_due`),
    the function adds `--from` for each step of `NEW_RUN_FROM`. The runner then redoes these
    steps and everything computed from them, and the Start of a monthly schedule begins the next
    monthly run. A run that failed in a step of the analysis stage after its release step
    completed counts as completed here (`failed_analysis_step`): its collection and its release
    are done, and a step of the analysis stage that fails at every Start would otherwise keep
    every later month from being collected. A Start that names a `--from` or `--mark-complete`
    that no run has used yet keeps the operator's arguments (`settle_one_start_arguments` has
    left out the arguments that a run used). When these arguments name a `--from`, `start`
    packs the data root of the last run first, unless every `--from` names the analysis stage or
    one of its steps (`from_arguments`). Every Start after any other run that did not complete
    also keeps its arguments and resumes that run. When the record of the last GHS download is
    missing or cannot be read, the function logs a warning, because no monthly run can begin
    until the record is repaired, and a Start that logged nothing would leave the operator to
    notice a missing month.
    """
    if names_from_or_mark(argv) or not run_released(data):
        return argv, None
    failed = failed_analysis_step(data)
    previous = ("The previous run completed" if failed is None
                else f"The previous run completed its release step and failed in {failed}")
    path = data / RETRIEVAL_REPORT
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        rid = (report["failed_retrieval_id"] if report.get("outcome") == "fallback"
               else report["retrieval_id"])
        taken = datetime.strptime(rid, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        cause = ("does not exist" if not path.exists()
                 else f"cannot be read ({type(error).__name__}, {error})")
        extra = " ".join(arg for name in NEW_RUN_FROM for arg in ("--from", name))
        log_warning(f"{previous}, but the record of its GHS download, /app/{DATA}/"
                    f"{RETRIEVAL_REPORT}, {cause}. Without the start time of the download, no "
                    f"next monthly run begins. To begin it, put {extra} in the `command:` of "
                    f"compose.yaml for one Start.")
        return argv, None
    if not next_month_due(taken, now):
        return argv, None
    age = (now - taken).days
    extra = [arg for name in NEW_RUN_FROM for arg in ("--from", name)]
    return [*argv, *extra], (f"{previous}, and its GHS download started {age:,} days ago, in "
                             f"{taken.astimezone(month_zone()):%B %Y} (retrieval {rid}). "
                             f"Beginning the next monthly run with {' '.join(extra)}.")


def runner_argv(argv: list) -> list:
    """The pipeline's runner, `run_pipeline.py` of the image, plus the arguments."""
    return [sys.executable, str(IMAGE_ROOT / "run_pipeline.py"), *argv]


def run_once(argv: list, data: Path, one_start: list = ()) -> tuple:
    """Run the runner once with `argv`, and return its exit code, whether the run gets a
    handover (`handover_wanted`), and whether the runner skipped every step.

    `one_start` are the operator's arguments for one Start in `argv` that no run has used. The
    record names them as passed before the runner starts and as used after it when the runner
    recorded their effect, so that a start that a kill ends during the run leaves what the next
    start needs to decide (`settle_one_start_arguments`).
    """
    command = runner_argv(argv)
    log(f"Running {' '.join(command)}.")
    before = state_signature(data)
    if one_start:
        write_one_start_record(data, used_arguments(data), one_start, before,
                               completed_names(data), taken_retrieval(data),
                               finished_list_identity(data))
    code = run_runner(command, data)
    log(f"The runner exited {code} ({outcome_name(code)}).")
    if one_start:
        write_one_start_record(data, used_arguments(data))
    return code, handover_wanted(code, before, data), code == 0 and every_step_skipped(data)


def startup_problems(app_root: Path) -> list:
    """Every problem with the mounts and their contents, as messages, before a run starts.

    A function rather than a sequence of `if`s inside `main`, so that the tests call every check
    through it. With each check tested only on its own, deleting the call of a check would break
    no test. This function runs every check, and `main` logs the messages that it returns.
    """
    data = app_root / DATA
    problems = []
    loose = unmounted(app_root)
    if loose:
        problems.append(
            f"{series(f'/app/{name}' for name in loose)} {'is' if len(loose) == 1 else 'are'} "
            f"not mounted from the host. Files in the container's own layer are lost when the "
            f"container is removed. Mount /app/data and /app/handover from the project folder, "
            f"as compose.yaml does.")
        return problems
    blocked = unwritable(app_root)
    if blocked:
        problems.append(
            f"{series(f'/app/{name}' for name in blocked)} "
            f"{'is' if len(blocked) == 1 else 'are'} mounted read-only. Mount /app/data and "
            f"/app/handover read-write.")
        if DATA in blocked:
            # The three checks below write into the data mount, and over a read-only mount they
            # would fail because of the mount. The message of `flock_works` would then name a
            # filesystem that does not implement flock, a second problem with a remedy that does
            # not help.
            return problems
    if not flock_works(data):
        problems.append(
            f"The filesystem under /app/{DATA} does not implement flock (required by the run "
            f"lock and the snapshot lock). Put the project folder on a volume of the machine "
            f"itself, not on a share mounted from elsewhere.")
    holder = lock_holder(data)
    if holder is not None:
        problems.append(
            f"Another run holds /app/{DATA}/{RUN_LOCK} ({holder}). Nothing was written. Stop "
            f"the other container of this project first.")
    # A data root of another revision is named without the problem of the state, because
    # recovering the state from an archive leaves the data root in the formats of that revision.
    for problem in (run_logs_problem(data), data_root_problem(data) or state_problem(data)):
        if problem:
            problems.append(problem)
    return problems


def remove_leftovers(app_root: Path) -> None:
    """Remove what killed processes left in the data root and the handover directory, and log
    what was removed: the GHS downloads that a stop or a kill interrupted, every entry of the
    collection stage's scratch directory, the partial archives of packings that a kill ended,
    and the checksum files that a kill left under their temporary names.

    `startup_problems` has checked that no runner holds the run lock, and no process of a run writes
    to these places now. No step reads such a download, because each download takes a retrieval of
    its own. No step reads an entry of the scratch directory that an earlier process left: the
    cloning step removes a clone's directory before each attempt, and every other entry is a
    temporary file or directory of a step, such as the release, that a kill ended before it could
    remove it. Left in place, the entries would take free space that the pre-checks
    require, at every later Start. A partial archive is no handover. Left in place until a packing,
    it would count as used space when the start measures whether the handover volume has room for
    the data root, and a monthly Start would then pack the data root without the sample and begin no
    month. """
    removed, kept = [], []
    leftovers = sorted((app_root / RETRIEVALS).glob(INTERRUPTED_DOWNLOAD))
    leftovers += sorted((app_root / HANDOVER).glob(PARTIAL_ARCHIVES))
    leftovers += sorted((app_root / HANDOVER).glob(PARTIAL_CHECKSUMS))
    scratch = app_root / SCRATCH
    if scratch.is_dir() and not scratch.is_symlink():
        try:
            leftovers += sorted(scratch.iterdir())
        except OSError as error:
            kept.append(f"Could not list {SCRATCH} for what killed processes left ({error}).")
    for path in leftovers:
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as error:
            kept.append(f"Could not remove {path.relative_to(app_root)}, which a killed process "
                        f"left ({error}).")
        else:
            removed.append(str(path.relative_to(app_root)))
    if removed:
        log(f"Removed what killed processes left ({series(removed)}).")
    for problem in kept:
        log_warning(problem)


def main(argv: list) -> int:
    global _stop_signal
    _stop_signal = 0
    log(first_line())
    data = APP_ROOT / DATA

    if wants_listing(argv):
        # A listing reads and writes nothing and needs no mount. It names the paths that a run
        # would use under the mounted data root.
        os.environ["AGENTCONTEXT_DATA_ROOT"] = str(data)
        os.execv(sys.executable, runner_argv(argv))

    # A Stop is recorded for the whole start, not only while the runner or 7zz runs. Between
    # them, this script measures the data root and the handover directory, and with the default
    # handler a Stop there would end the script before it packs the handover. `run_runner` and
    # `_run_7z` install their own handlers and restore this one.
    previous = {sig: signal.signal(sig, note_stop_signal) for sig in STOP_SIGNALS}
    try:
        return start(argv, data)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stopped_before_runner() -> bool:
    """Whether a Stop arrived before the runner started, with a log message when it did. A
    runner started after a Stop would run until Docker kills the container, and leave no
    handover."""
    if _stop_signal:
        log(f"A Stop (signal {_stop_signal}) arrived before the runner started. No step ran.")
    return bool(_stop_signal)


def log_failed_analysis(step: str) -> None:
    """Log that the previous month's analysis failed, before the next monthly run begins."""
    log_warning(f"The analysis of the previous month failed in {step}. Its figures and tables "
                f"are incomplete. The handover of the previous run contains "
                f"run_logs/{step}.log.")


def start(argv: list, data: Path) -> int:
    """Check the container and the mounts, run the runner and pack the handover, and return the
    container's exit code. `main` calls it with the stop signals recorded.

    Before a monthly run, and before a run with a `--from` in `command:` over a data root in
    which the release step is done (`release_step_completed`), the start packs the data root of
    the previous run when no `-complete` archive contains it (`hand_over_before_next_month`).
    `from_arguments` describes when the start packs nothing first."""
    problems = container_problems(os.environ, utc_now()) + startup_problems(APP_ROOT)
    if problems:
        for problem in problems:
            log_error(problem)
        return 1

    said = name_credentials(os.environ)
    if said.startswith("WARNING "):
        log_warning(said[len("WARNING "):])
    else:
        log(said)

    remove_leftovers(APP_ROOT)
    # Before anything reads the stored files, also before the handover of the previous run,
    # which then contains the converted data root.
    try:
        converted = convert_data_root(data)
    except (ConversionError, OSError) as error:
        log_error(f"Could not convert the data root /app/{DATA} to revision "
                  f"{DATA_ROOT_REVISION} ({error}). No step ran. Correct the cause, such as a "
                  f"full volume, or replace a file that cannot be read with its copy in the "
                  f"latest archive in the handover directory, and Start again.")
        return 1
    if converted:
        log(converted)
    # Put every temporary file of the run on the volume. agentcontext/steps/release.py builds
    # the release under tempfile's default directory, which in a container is in the image's
    # layer. The clones already go to the collection stage's _scratch/.
    scratch = APP_ROOT / SCRATCH
    try:
        scratch.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        log_error(f"Could not create /app/{SCRATCH} ({error}). No step ran. Correct the cause, "
                  f"such as a full volume or a file of that name, and Start again.")
        return 1
    os.environ["TMPDIR"] = str(scratch)
    # The package is installed from the image's copy of the repository under /opt/pipeline,
    # and would derive every default path from there. The run's files are on the volume.
    os.environ["AGENTCONTEXT_DATA_ROOT"] = str(data)

    argv = settle_one_start_arguments(argv, data)
    now = utc_now()
    log_month_zone(now)
    run_argv, why = monthly_argv(argv, data, now)
    failed = failed_analysis_step(data) if why else None
    # A `--from` in `command:` rewrites the outputs of the steps that it redoes in the data root
    # of the last run, as a monthly run rewrites all of them. A `--from` of the analysis stage
    # rewrites only `analysis/` and `run_logs/` under the image that ran the last run.
    froms = from_arguments(argv)
    before_the_analysis = bool(froms) and not redoes_only_the_analysis(froms, data)
    if why or before_the_analysis and release_step_completed(data):
        # Packed before this start writes its machine record and its copies of the heuristics
        # files, so that the handover of the previous run names the image and the heuristics
        # that the previous run used.
        blocked = hand_over_before_next_month(
            data, begins="the next monthly run" if why else f"the run with {' '.join(froms)}")
        if blocked:
            return blocked

    # These writes come before the runner's pre-checks, which names a full volume, and the
    # handler below names the cause of an error in one line rather than in a traceback.
    try:
        if record_data_root(data):
            log(f"Recorded revision {DATA_ROOT_REVISION} of the new data root in "
                f"{DATA_ROOT_RECORD}.")
        record = machine_record(data, timestamp(), build_info())
        (data / MACHINE_RECORD).parent.mkdir(parents=True, exist_ok=True)
        (data / MACHINE_RECORD).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        log(machine_line(record))
        kept = record_heuristics(data)
    except OSError as error:
        log_error(f"Could not write the record of the data root's revision ({DATA_ROOT_RECORD}), "
                  f"the machine record ({MACHINE_RECORD}) or the copies of the heuristics files "
                  f"({HEURISTICS_RECORD}) under /app/{DATA} ({error}). No step ran. Correct the "
                  f"cause, such as a full volume, and Start again.")
        return 1
    log(f"Copied the {kept:,} heuristics files of the image to {HEURISTICS_RECORD}."
        if kept else f"The image has no {IMAGE_ROOT / 'heuristics'}. The only records of the "
                     f"run's heuristics are the checksums of config_path_patterns.csv, "
                     f"planning_document_patterns.csv, vendored_dirs.txt and "
                     f"local_state_paths.csv in output/provenance.json.")
    if not record["init_process"]:
        log_warning("The container has no init process. The entrypoint is process 1 and reaps "
                    "only the runner. A process left without a parent stays a zombie until the "
                    "container ends. Set `init: true` in compose.yaml, or pass --init to docker "
                    "run.")

    if failed:
        log_failed_analysis(failed)
    if why:
        log(why)
    if stopped_before_runner():
        return 128 + _stop_signal
    at_start = retrieval_record(data)
    began_with = at_start if why else None
    code, wanted, skipped = run_once(run_argv, data, one_start_arguments(argv))
    if not wanted:
        log("No step ran, and no handover was packed.")
        return container_exit_code(code)
    handed = hand_over(code, skipped, data)
    returned = begin_next_month_after(argv, data, code, handed, began_with)
    warn_after_fallback(data, at_start)
    return returned


def warn_after_fallback(data: Path, before: "bytes | None") -> None:
    """Log a warning at the end of a start in which the GHS download fell back to the previous
    sampling frame, that is, when the record of the last GHS download differs from `before`,
    its bytes when the start began, and records the outcome `fallback`. The warning names the
    retrieval, the reason and how many downloads in a row fell back, and after a retrieval that
    the size check refused, how to take it.

    The sampling stage exits 0 after a fallback, and the run completes over the previous
    sampling frame. Without this warning, only `run_logs/ghs_download.log` would show that the
    sampling window did not move, also when GHS stays smaller and every later month falls
    back."""
    after = retrieval_record(data)
    if after is None or after == before:
        return
    try:
        report = json.loads(after)
    except ValueError:
        return
    if not isinstance(report, dict) or report.get("outcome") != "fallback":
        return
    count = report.get("fallbacks_in_a_row")
    times = ("" if not isinstance(count, int) or isinstance(count, bool) or count < 1
             else " No earlier download fell back to that retrieval." if count == 1
             else f" {count:,} downloads in a row have fallen back to that retrieval.")
    reason = str(report.get("reason") or "").strip().rstrip(".")
    # The reason is a sentence that the download wrote. Its first letter is lowercased after
    # "because", unless the first word is an acronym, such as GHS.
    because = (", for a reason that the record does not state" if not reason
               else f", because {reason[:1].lower()}{reason[1:]}" if reason[1:2].islower()
               else f", because {reason}")
    remedy = ""
    if report.get("failed_item_count") is not None:
        remedy = (f" When GHS has lost those rows for good, put {STEP_ARG} "
                  f"ghs_download:--accept-smaller in the `command:` of compose.yaml for one "
                  f"Start. Alone, it takes the smaller retrieval at the next monthly run, and "
                  f"with {' '.join(arg for name in NEW_RUN_FROM for arg in ('--from', name))}, "
                  f"at that Start.")
    log_warning(f"The GHS download fell back to the previous sampling frame, retrieval "
                f"{report.get('retrieval_id')}{because}. The sampling window did not "
                f"move.{times} Read run_logs/ghs_download.log." + remedy)


def stopped_during_start() -> bool:
    """Whether a Stop arrived during this start, with a log message that gives the Stop as the
    reason why the start begins no next monthly run."""
    if _stop_signal:
        log(f"A Stop (signal {_stop_signal}) arrived during this Start. The next monthly run "
            f"does not begin.")
    return bool(_stop_signal)


def retrieval_record(data: Path) -> "bytes | None":
    """The record of the last GHS download as its bytes on disk, or None where it cannot be
    read."""
    try:
        return (data / RETRIEVAL_REPORT).read_bytes()
    except OSError:
        return None


def finished_list_identity(data: Path) -> "list | None":
    """The inode, the modification time in nanoseconds and the size of the list of finished
    repositories (FINISHED_LIST), or None when it is absent or cannot be read."""
    try:
        info = (data / FINISHED_LIST).stat()
    except OSError:
        return None
    return [info.st_ino, info.st_mtime_ns, info.st_size]


def taken_retrieval(data: Path) -> "str | None":
    """The retrieval that the record of the last GHS download names as the retrieval that the
    download took, or None when the download took none or fell back to the previous
    sampling frame, or when the record cannot be read."""
    try:
        report = json.loads((data / RETRIEVAL_REPORT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict) or report.get("outcome") != "retrieved":
        return None
    taken = report.get("retrieval_id")
    return taken if isinstance(taken, str) and taken else None


def begin_next_month_after(argv: list, data: Path, code: int, handed: "Handover",
                           began_with: "bytes | None" = None) -> int:
    """Decide again after the run that this start ran, and after each monthly run that this
    start begins, and return the container's exit code. The function begins the next monthly
    run when the run that just ended completed, or failed in a step of the analysis stage after
    its release step, and the next month is due now (`monthly_argv`).

    Docker ignores a Start while the container runs, and the scheduled Start of a month that
    begins during a long run is lost. Such a run can resume a run that a reboot killed, collect
    again under a new image, or be a monthly run that began late in a month after you repaired a
    Start that the pre-checks refused. Without this decision, the new retrieval would wait a
    month for the Start after it. The next run replaces the sampling frame, the labels, the
    collected and validated trees and the release. It therefore begins only when a handover
    contains the data root of the run that just ended, and not after a Stop. When the handover
    of a run that failed in the analysis stage could not be packed, the next run still begins
    once `hand_over_before_next_month` has packed the data root of that run, which contains the
    pipeline execution state and the logs that the handover would have contained. The next run
    goes without the arguments for one Start that the runs of this start used
    (`used_arguments`).

    `began_with` is the record of the last GHS download (`retrieval_record`) from before the run
    that just ended when that run was a monthly run, and None otherwise. A monthly run takes a
    new retrieval and rewrites the record, and the next month is due after it only when the run
    passed the first of a later month. After a monthly run that left the record as it was, the
    record still names the download before it. The start then begins no other monthly run, which
    would pack the data root and run again on the same record until a Stop. A monthly run that a
    check of the runner stopped before its first step also leaves the record as it was, with the
    outcome `failed` and the step results of the run before it in `last_run_steps`
    (`stopped_by_a_check`). The start then begins no other monthly run either. Whether the run
    before it completed or failed in the analysis stage, a warning then states that the monthly
    run ran no step, rather than pointing to the log of a download that did not run, and the log
    shows why no month began.
    """
    while True:
        if began_with is not None and stopped_by_a_check(data):
            log_warning("The monthly run that just ended ran no step, because a check of the "
                        "runner stopped it before its first step. The ERROR line of the runner "
                        "above names the check. No other monthly run begins before the next "
                        "Start.")
            return handed.code
        failed = failed_analysis_step(data) if code != 0 else None
        if code != 0 and failed is None:
            return handed.code
        # The arguments for one Start that a run of this start used apply to that run alone.
        recorded = used_arguments(data)
        used = [argument for argument in one_start_arguments(argv) if argument in recorded]
        argv = without_arguments(argv, used)
        next_argv, why = monthly_argv(argv, data, utc_now())
        if why is None:
            return handed.code
        if began_with is not None and retrieval_record(data) == began_with:
            log_warning(f"The next month is due, but the monthly run that just ended left the "
                        f"record of its GHS download, /app/{DATA}/{RETRIEVAL_REPORT}, "
                        f"unchanged. No other monthly run begins before the next Start. Read "
                        f"run_logs/ghs_download.log.")
            return handed.code
        if stopped_during_start():
            return handed.code
        if handed.archive is None and failed is None:
            log_error("The handover of the run that just ended could not be packed. The next "
                      "monthly run does not begin, because it would replace the data root of "
                      "that run. The data root remains in the project folder.")
            return handed.code
        if handed.archive is None:
            log(f"The handover of the run that just ended could not be packed. That run failed "
                f"in {failed} after its release step. Packing its data root, with the pipeline "
                f"execution state and the logs, before the next monthly run begins.")
        blocked = hand_over_before_next_month(data, handed.archive)
        if blocked:
            return handed.code if _stop_signal else blocked
        if stopped_during_start():
            return handed.code
        if failed:
            log_failed_analysis(failed)
        if used:
            log(f"The next monthly run goes without {named(used)}, "
                f"{'an argument' if len(used) == 1 else 'arguments'} for one Start that the run "
                f"that just ended used.")
        log(why)
        began_with = retrieval_record(data)
        next_code, next_wanted, next_skipped = run_once(next_argv, data,
                                                        one_start_arguments(argv))
        if not next_wanted:
            # Refused before its first step, such as by the pre-checks for an expired GitHub
            # access token. The handover of the run before it is packed, and the container's
            # exit code shows the refusal.
            log("The next monthly run ran no step, and no handover was packed for it.")
            return container_exit_code(next_code)
        code, handed = next_code, hand_over(next_code, next_skipped, data)


def hand_over_before_next_month(data: Path, packed: "str | None" = None,
                                begins: str = "the next monthly run") -> int:
    """Pack the handover of the previous run, which contains the data root that the next run
    rewrites, and return 0 when the next run can begin, else the container's exit code. The
    function packs nothing when a `-complete` archive of the data root was packed earlier in
    this start (`packed`) or the record of the last handover of a completed run names one.
    `begins` names the next run in the log: the next monthly run, or the run with the `--from`
    arguments of `command:`. `start` does not call the function when every `--from` names the
    analysis stage or one of its steps (`redoes_only_the_analysis`, `from_arguments`).

    A monthly run replaces the sampling frame, the labels, the collected and validated trees and
    the release, and a run with any other `--from` rewrites the outputs of the steps that it
    redoes. Without this packing, a previous run would be lost if its packing failed, its data
    root did not fit on the handover volume, or its analysis failed after its release step. The
    previous run changed nothing since it ended, and its handover is packed as at the end of a
    start that skipped every step. The archive that this packing returns counts, as does the
    record, so that a record that could not be written does not refuse the month after a
    packing that succeeded, and a start packs the data root at most once. When no `-complete`
    archive contains the data root afterwards, because the packing failed or the data root
    still does not fit, the start begins no new month, logs which of the two happened and
    exits 1. The data root remains in the project folder. No packing starts after a Stop,
    because Docker kills a packing of the whole data root when the grace period ends. The
    function then returns `128 + signal`, and the next Start packs the data root.
    """
    if packed is not None and packed.endswith("-complete.7z"):
        return 0
    if recorded_handover(data, "complete") is not None:
        return 0
    if not _stop_signal:
        log(f"The record {HANDOVER_RECORD} names no handover that contains the data root of "
            f"the previous run. Packing the data root before {begins}.")
        handed = hand_over(0, True, data, previous_run=True)
        if handed.archive is not None and handed.archive.endswith("-complete.7z"):
            return 0
    if _stop_signal:
        log(f"A Stop (signal {_stop_signal}) arrived before the data root of the previous run "
            f"was packed. Nothing more was packed, and {begins} does not begin. The data root "
            f"remains in the project folder, and the next Start packs it.")
        return 128 + _stop_signal
    if handed.problem is not None:
        cause = f"its packing failed ({handed.problem})"
        remedy = "Correct the cause of that error and Start again"
    else:
        cause = "the data root does not fit on the handover volume"
        remedy = "Make room on the handover volume and Start again"
    log_error(f"No handover contains the data root of the previous run, because {cause}. "
              f"{begins[:1].upper()}{begins[1:]} does not begin, because it would rewrite that "
              f"data root. The data root remains in the project folder. {remedy}.")
    return handed.code or 1


class Handover(NamedTuple):
    """What `hand_over` did: the container's exit code, the name of the archive that is the
    handover of the run, packed now or recorded earlier, or None when the packing failed, and
    the error of a packing that failed."""
    code: int
    archive: "str | None"
    problem: "str | None" = None


def hand_over(code: int, skipped: bool, data: Path, previous_run: bool = False) -> Handover:
    """Pack the handover of a run that ran a step or completed, record the handover of a
    completed run (`record_handover`), and return the container's exit code with the archive.
    The code is the runner's. After a run that exited 0 without a packed handover, the code is
    1 when the packing failed, or 128 plus the signal when a Stop ended the packing, as after a
    Stop that ended the run.

    `previous_run` is True for the handover of a run that ended before this start, which
    `hand_over_before_next_month` packs. A Stop that arrived before that packing starts cancels
    it, and the archive is then None, because the data root remains in the project folder for
    the next Start. The handover of the run that just ended is packed after a Stop too, because
    it records the pipeline execution state and the logs at the end of that run, which the next
    Start replaces. After a Stop, the handover of a completed run contains
    `PARTIAL_HANDOVER_MEMBERS` rather than the data root, as `-complete-without-sample` without
    its trees, because 7zz over the whole data root can take longer than the stop grace period.
    This handover is not recorded, so that the next Start packs the data root.
    """
    outcome = outcome_name(code)
    handovers = APP_ROOT / HANDOVER
    # The entrypoint deletes no archive, because only you know which archives you have
    # downloaded. A complete archive contains the sample, and the count and the size show when
    # the volume fills.
    count, size = archives_in(handovers)
    log(f"{handovers} holds {count:,} archive(s) of earlier runs, {size / 1024 ** 3:.1f} GiB "
        f"in all. Download them and delete those that you have kept elsewhere.")
    if skipped:
        current = recorded_handover(data, "complete")
        if current is not None:
            log(f"Every step was already complete, and no handover was packed. {current}, "
                f"packed after the last Start that ran a step, contains the data root as it is.")
            return Handover(container_exit_code(code), current)
    complete = pack_everything(outcome)
    # 7zz over the whole data root can take longer than the stop grace period and does not start
    # after a Stop. Docker would kill it when the grace period ends, and no handover would remain.
    everything = complete and not _stop_signal and room_for_everything(APP_ROOT, handovers)
    if previous_run and _stop_signal:
        return Handover(128 + _stop_signal, None)
    # A Stop that the runner received after its last step, or that arrived after the runner
    # ended, such as while the data root was measured.
    stopped = complete and bool(_stop_signal)
    if stopped:
        everything = False
        log(f"A Stop (signal {_stop_signal}) arrived before the data root of the completed run "
            f"was packed. Packing {WITHOUT_THE_SAMPLE} instead, because 7zz over the whole data "
            f"root can outlast the stop grace period. The data root remains in the project "
            f"folder, and the next Start packs it.")
    elif skipped and not everything:
        # Without room for the data root, the `-complete-without-sample` archive that the
        # completed run packed still contains the pipeline execution state, the logs and the
        # lists as they are, and another such archive with its trees would take the room that
        # the volume lacks.
        current = recorded_handover(data, f"{outcome}-without-sample")
        if current is not None:
            log(f"Every step was already complete, and the data root does not fit on the "
                f"handover volume. No handover was packed. {current}, packed after the last "
                f"Start that ran a step, contains {WITHOUT_THE_SAMPLE} as they are.")
            return Handover(container_exit_code(code), current)
    # The archive of a completed run without the data root has a name of its own, so that
    # neither you nor `recorded_handover` takes it for the archive that contains the data root.
    label = f"{outcome}-without-sample" if complete and not everything else outcome
    when = timestamp()
    try:
        archive = pack_handover(APP_ROOT, handovers, label, when, everything=everything)
    except PackingStopped as error:
        # Exits as every other Stop does: with the runner's code, which after a run that exited
        # 0 is the code of the signal.
        log(f"The handover was not packed, because {error}. The files that it would have "
            f"contained remain in the project folder.")
        return Handover(container_exit_code(code) or 128 + _stop_signal, None, str(error))
    except (RuntimeError, OSError) as error:
        log_error(f"The handover could not be packed ({error}).")
        return Handover(container_exit_code(code) or 1, None, str(error))
    log(f"Packed the handover {archive} ({archive.stat().st_size / 1024 ** 2:,.0f} MiB"
        + (")." if everything else ", without the collected and validated trees)."))
    # The archive that a Stop made smaller is not recorded, so that the next Start packs the
    # data root, or without room for it, the trees beside the lists.
    if complete and not stopped:
        try:
            record_handover(data, archive)
        except OSError as error:
            log_warning(f"Could not write {HANDOVER_RECORD} ({error}). A later Start that skips "
                        f"every step packs the data root again.")
    record_checksum(archive)
    # The trees of a completed run are not packed after a Stop, because the next Start packs
    # them, within the data root when it fits, and their packing can outlast the grace period.
    if not everything and not (complete and _stop_signal):
        try:
            trees = pack_trees(APP_ROOT, APP_ROOT / HANDOVER, outcome, when)
        except PackingStopped as error:
            log(f"Did not pack the collected and validated trees, because {error}. The handover "
                f"without them is complete. The trees remain in the project folder.")
            trees = None
        except (RuntimeError, OSError) as error:
            log_error(f"Could not pack the collected and validated trees ({error}). The handover without "
                      f"them is complete.")
            trees = None
        if trees is not None:
            log(f"Packed the collected and validated trees {trees} "
                f"({trees.stat().st_size / 1024 ** 2:,.0f} MiB).")
            record_checksum(trees)
    return Handover(container_exit_code(code), archive.name)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
