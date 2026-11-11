"""The unmatched checks, `python -m agentcontext unmatched paths` and `python -m agentcontext
unmatched commits`, and `python -m agentcontext unmatched reduce`.

The paths that no heuristic matches

Rank the paths in output/other.csv that no heuristic covers.

`other.csv` contains the unclassified files: every file inside a spec directory that no detector
recognized. A mechanism that the tools add appears there first, and stays there until a check
reads the column for path globs that repeat. The items of an Antigravity default place that the
top-level `exclude` of a registration file leaves out are there too, and the exclude explains
them.

`unmatched paths` is that check. It reduces each path to a path glob, the path glob of its path
below the spec directory that contains it, counts the distinct repositories per path glob, and
lists the path globs that the heuristics do not already cover.
It counts the files that an exclude leaves out separately and reads the validated tree to find
them. Run it after every regeneration. A path glob above the threshold is either a mechanism to
add to `config_path_patterns.csv` or a row to add to heuristics/not_configuration.csv with a
reason.

Inputs:  output/other.csv, config_path_patterns.csv, not_configuration.csv, the validated
         tree, and the repositories that the collected file cleanup dropped because they
         matched no mechanism, read from deletion.csv and the collected tree
Outputs: the ranked path globs as log messages, and a CSV with --out

The exit codes follow the other comparison tools, where the code is the verdict: 0 means that
the check ran and no path glob reached the threshold, 2 means that --fail-above was given and a
path glob reached it, and 1 means that the check could not run. Returning 1 both when other.csv
could not be read and when a path glob was found would present a check that never ran as a
check that found a problem.

The attributions that no commit pattern matches

Rank the attributions in the commits that the cloning step read and that no commit pattern
matches.

`ai_commit_patterns.csv` defines which commits credit one of the six tools, and the cloning step
applies it as it reads the commit histories. Only this check shows what the heuristics miss. The
`doc` rows of the heuristics cite a vendor page, which the documentation watch re-reads every
week. The `sample` rows are conventions read off the commits themselves, and no vendor page
shows when one of them is wrong or incomplete. This check keeps the `sample` rows current, as
`unmatched paths` does for the path patterns.

The cloning step writes the input of this check. For every commit that no pattern matched, it
records the git trailers that credit somebody with writing the commit and any author or
committer identity that looks like a bot. The check adds the trailers that credit somebody under
a token after a prefix that the heuristics read as the token itself, such as `AI-assisted-by:`,
which it reads from the commit logs that the cloning step keeps (`prefixed_credits`). It ranks
the attributions by the number of repositories that have them, leaves out the attributions that
`not_ai_commits.csv` names, and lists the rest.

A listed attribution credits a tool that no heuristic covers, an automation that is not a coding
agent, or a human co-author. Add a tool to `ai_commit_patterns.csv` as a `sample` row with
what was observed, and an automation to `not_ai_commits.csv` with a reason. A human co-author
gets no row in either file, and the check lists it again on every run. A person has to read
every listed attribution.

Inputs:  output/all_ai_commits-unmatched.csv, written by the cloning step, the per-repository
         commit records and the commit logs in collect_commits/ beside output/, and
         not_ai_commits.csv
Outputs: the ranked attributions as log messages, and a CSV with --out

The exit codes are those of `unmatched paths`, with attributions in place of path globs.

The tracked unmatched attributions

`reduce` writes the attributions of a collection that name a bot or a tool and no person, with
the four columns that `commits` reads, as output/all_ai_commits-unmatched-<day the collection
started>.csv.7z, and removes the file that it replaces. The cloning step's own file is large,
every run rewrites it, and most of its attributions name people, so it stays in the handover of
the run, where `commits` ranks it. Without a tracked copy, no row of not_ai_commits.csv could be
checked against a collection in a clean checkout. tests/test_checks_unmatched.py checks every row
against the tracked copy. The release publishes the same archive.

Inputs:  output/all_ai_commits-unmatched.csv, the commit logs in collect_commits/ and
         finished_repositories.jsonl of one collection, with the record of the collection in
         progress beside the list when there is one, which an extracted handover contains
Outputs: output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z. Exit 0 when it is written, 1 when
         an input or 7zz is missing or the list does not show the day on which the collection
         started
"""

import csv
import json
import lzma
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from agentcontext import cli, commits, resume, SEVEN_ZIP, STAGE_DIR
from agentcontext.heuristics import (TRAILER_TOKEN_PREFIXES, load_config_path_patterns,
                                    not_ai_commits, not_configuration, one_path, paths_for)
from agentcontext.repos import dir_name_to_repo, repo_name_to_dir
from agentcontext import logs, paths, planning


# -----------------------------------------------------------------------------
# The paths that no heuristic matches
# -----------------------------------------------------------------------------
DATA_DIR = STAGE_DIR
OTHER_CSV = DATA_DIR / "output" / "other.csv"
DELETION_CSV = DATA_DIR / "deletion.csv"
COLLECTED_DIR = DATA_DIR / "data" / "collected"
VALIDATED_DIR = DATA_DIR / "data" / "validated"
SUBMODULES_CSV = DATA_DIR / "submodules.csv"

# The path globs that are not configuration, each with the reason, from
# heuristics/not_configuration.csv.
NOT_CONFIGURATION = not_configuration()

_PATTERNS = load_config_path_patterns()
# The file that makes a folder a skill. A skill that the detection step counts covers every file
# of its folder.
SKILL_MARKER = one_path(_PATTERNS, "skills", "skill_marker")
# The directories that the detection step reads at any depth and whose files that no detector
# claims `other.csv` lists: the tool directories, `.github` and the plugin directories.
SPEC_DIRECTORIES = frozenset(paths_for(_PATTERNS, "other", "spec_dir")
                             + paths_for(_PATTERNS, "other", "unclassified_dir"))
# The ignore files, which their rows name by file name and the detection step lists at any
# depth.
IGNORE_FILES = frozenset(paths_for(_PATTERNS, "other", "basename"))

# The leading segments of a path below a spec directory that its path glob keeps, the spec
# directory and the directory of the mechanism in it.
GLOB_SEGMENTS = 2
# The repositories in which a path glob has to occur before `unmatched paths` lists it.
PATHS_MIN_REPOS = 3


def glob_for(path: str) -> str:
    """Reduce a path to the path glob that it shares with its neighbors.

    `.claude/rules/frontend/forms.md` and `.claude/rules/api.md` are the same mechanism and
    both become `.claude/rules/**`. A file directly inside a spec directory keeps its own name,
    which distinguishes `.cursor/BUGBOT.md` from `.cursor/worktrees.json`.
    """
    parts = path.split("/")
    if len(parts) <= GLOB_SEGMENTS:
        return path
    return "/".join(parts[:GLOB_SEGMENTS]) + "/**"


def heuristics_paths(rows: list) -> set:
    """Return every path that the heuristics name."""
    return {r["path"] for r in rows}


# Every path that the heuristics name.
REGISTERED = heuristics_paths(_PATTERNS)


def is_explained(glob: str, registered: set) -> bool:
    """Whether the heuristics name this exact path.

    Only an exact match counts. `other.csv` contains files that no detector classified. A file
    under a directory that the heuristics name is therefore not explained: It is a file inside a
    mechanism's directory that the mechanism did not recognize, the most important path glob
    that the check can find. The exact match covers the ignore files, which are heuristics with
    no mechanism, because the path glob of an ignore file is its file name (`path_globs`).
    """
    return glob in registered


def path_globs(path: str) -> list:
    """The path globs that can explain a file of `other.csv`, the outermost first.

    A reason of not_configuration.csv and a path of the heuristics name a place in a spec
    directory, and the detection step reads a spec directory at any depth. A file in a spec
    directory below the root is therefore judged at its path below that directory:
    `apps/readest-app/.claude/memory/notes.md` has the path glob `.claude/memory/**`, which the
    reason for the memory notes in `.claude` explains. A file in a spec directory inside another
    one has a path glob below each: `.claude/.claude-plugin/skills/bump.sh` has
    `.claude/.claude-plugin/**` and `.claude-plugin/skills/**`. An ignore file has its file name
    as its path glob, because its row names it by that name at any depth. A path in no spec
    directory keeps the path glob of its own path.
    """
    parts = path.split("/")
    if parts[-1] in IGNORE_FILES:
        return [parts[-1]]
    starts = [i for i, part in enumerate(parts[:-1]) if part in SPEC_DIRECTORIES]
    return [glob_for("/".join(parts[i:])) for i in starts] or [glob_for(path)]


def path_glob(path: str) -> str:
    """The path glob under which `unmatched paths` judges and counts a file.

    It is the outermost of `path_globs` that not_configuration.csv or the heuristics name,
    because a reason for a spec directory covers what the directory contains, and otherwise the
    innermost, the place in the tool's directory that a missing heuristic would name. The same
    place in two repositories is thus counted together, at the root or below it.
    """
    globs = path_globs(path)
    return next((glob for glob in globs
                 if glob in NOT_CONFIGURATION or is_explained(glob, REGISTERED)), globs[-1])


def excluded_by_registration(root: Path):
    """A test of whether a file of the repository at `root`, given by its path in the
    repository, is a file that the top-level `exclude` of an Antigravity registration file
    leaves out of a default place.

    Such a file is an unclassified file although a detection heuristic names its place. It is
    either a file that `paths.antigravity_left_out` names, which the detection step does not
    read as an item of that place, or a file in the folder of a skill left out that way, because
    a skill that the detection step counts covers every file of its folder. The exclude explains
    the file, and no heuristic is missing for it.
    """
    files = set(paths.antigravity_left_out(root))
    folders = tuple(posixpath.dirname(rel) + "/" for rel in files
                    if posixpath.basename(rel).lower() == SKILL_MARKER.lower())
    return lambda rel: rel in files or rel.startswith(folders)


@dataclass
class LeftOut:
    """The rows of `other.csv` that `read_other` leaves out of the path globs: the files that
    the top-level `exclude` of an Antigravity registration file leaves out, the repositories
    that have them, and the repositories with no directory in the validated tree, in which such
    files could not be told apart."""
    files: int = 0
    repos: set = field(default_factory=set)
    without_tree: set = field(default_factory=set)


def read_other(path: Path, validated_dir: Path | None = None) -> tuple[dict, LeftOut]:
    """Return {path glob: [file count, set of repositories]} and the rows left out of it. Each
    row is counted under its `path_glob`.

    With `validated_dir`, the validated tree that the detection step read, a row of a file that
    the top-level `exclude` of an Antigravity registration file leaves out
    (`excluded_by_registration`) is counted in the `LeftOut` rather than under its path glob.
    The detection step writes such a file into `other.csv`. Under its path glob, such as
    `.agent/agents/**`, the file would be listed as a path that a heuristic is missing for.

    The header is checked, as `load_unmatched` checks its own. Without the check, a truncated
    or empty `other.csv` would read as a CSV of no rows, and the check would list nothing
    unmatched and exit 0 for a file that it could not read at all. A file with the wrong
    columns would end the check with a `KeyError`.
    """
    counts: dict = defaultdict(lambda: [0, set()])
    left_out = LeftOut()
    excluded: dict = {}
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = {"other", "link_path", "repo_name"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the column(s) {sorted(missing)}")
        for row in reader:
            repo = row["repo_name"]
            if validated_dir is not None:
                if repo not in excluded:
                    root = validated_dir / repo_name_to_dir(repo)
                    excluded[repo] = excluded_by_registration(root) if root.is_dir() else None
                    if excluded[repo] is None:
                        left_out.without_tree.add(repo)
                test = excluded[repo]
                if test is not None and (test(row["other"])
                                         or (row["link_path"] and test(row["link_path"]))):
                    left_out.files += 1
                    left_out.repos.add(repo)
                    continue
            # A file behind a symlinked directory is judged at the path at which the tool
            # reads it.
            entry = counts[path_glob(row["link_path"] or row["other"])]
            entry[0] += 1
            entry[1].add(repo)
    return counts, left_out


# The reason that the collected file cleanup writes when it removes a repository that no
# mechanism matched. Such a repository is in no output CSV, and `other.csv` never contains its
# files. `read_dropped` reads them instead.
DROPPED_REASON = "no detected mechanism, repository directory removed"


def dropped_repositories(deletion_csv: Path) -> list:
    """The repository directory names that the collected file cleanup removed because they
    matched no mechanism."""
    with open(deletion_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = {"path", "reason"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{deletion_csv} lacks the column(s) {sorted(missing)}")
        return [Path(row["path"]).name for row in reader
                if row["reason"].startswith(DROPPED_REASON)]


def read_dropped(deletion_csv: Path, collected_dir: Path, submodules_csv: Path) -> dict:
    """Return {path glob: [file count, set of repositories]} for the dropped repositories, with
    each file under its `path_glob`.

    `other.csv` contains the files that a published repository had and that no detector
    classified. A repository that the collected file cleanup dropped because it matched no
    mechanism is in no output CSV at all, and a path that occurs only in such repositories is
    outside every input of `read_other`. The function leaves out the planning documents and
    their anchors (`planning.find`), which a heuristic explains, as `other.csv` leaves them out
    of a published repository.

    The collected tree is read rather than the validated tree, because the cleanup deleted
    these directories. Of each directory, the function reads what the cleanup keeps of a
    repository that it does not drop: The cleanup's own `scan_and_clean_repo` runs over a copy
    and, before anything is counted, removes submodule contents and vendored subtrees, prunes
    `.github` to the paths that the heuristics name, and removes a context file with its name in
    the wrong case, as it does before `other.csv` is written. Over whole directories, the ranked
    list would consist mostly of a project's CI files and library pages named `agents.md`, which
    the collector copies so that the validation can test them, and which the validation
    removes.
    """
    from agentcontext.steps import collected_file_cleanup  # the step module, imported where it is used
    submodules = collected_file_cleanup.load_submodules(submodules_csv)
    counts: dict = defaultdict(lambda: [0, set()])
    for name in dropped_repositories(deletion_csv):
        root = collected_dir / name
        if not root.is_dir():
            continue
        repo = dir_name_to_repo(name)
        with tempfile.TemporaryDirectory() as work:
            copy = Path(work) / name
            paths.copy_tree(root, copy)
            collected_file_cleanup.scan_and_clean_repo(str(copy), [], submodules.get(repo, ()))
            documents, anchors = planning.find(copy, collected_file_cleanup.VENDORED_DIRS)
            planned = {rel for rel, _heuristic_row in documents} | anchors
            for directory, _subdirs, files in os.walk(copy):
                for file_name in files:
                    relative = (Path(directory) / file_name).relative_to(copy).as_posix()
                    if relative in planned:
                        continue
                    entry = counts[path_glob(relative)]
                    entry[0] += 1
                    entry[1].add(repo)
    return counts


def _not_read(error: OSError) -> str:
    """The log message of an input that could not be read. An error of the operating system
    names its file in `filename`, and an error that a check raised itself states the file."""
    if error.filename is None:
        return str(error)
    return f"Could not read {error.filename} ({error.strerror})"


# The columns of `--out` for `unmatched paths`. `found_in` names the population that a path glob
# came from: `other.csv`, the unclassified files of the published repositories, or the
# repositories that the collected file cleanup dropped, which have files in no output CSV.
PATHS_OUT_COLUMNS = ("glob", "repos", "files", "found_in")
DROPPED_FOUND_IN = "dropped repositories"


def _table(rows: list) -> None:
    """Log every ranked path glob, or a line that states that there is none."""
    if not rows:
        logs.info('unmatched_paths_report', "  No such path glob.")
        return
    logs.info('unmatched_paths_report', f"  {'repos':>6} {'files':>6}  glob")
    for repos, files, glob in rows:
        logs.info('unmatched_paths_report', f"  {repos:>6} {files:>6}  {glob}")


def main_paths(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Rank the path globs in other.csv that no heuristic explains. With "
                    "--fail-above N, exit 2 when a path glob occurs in N or more repositories.")
    parser.add_argument("--other-csv", default=str(OTHER_CSV),
                        help=f"Path to other.csv (default: {OTHER_CSV})")
    parser.add_argument("--deletion-csv", default=str(DELETION_CSV),
                        help=f"Deletion log of the collected file cleanup, read for the "
                             f"repositories dropped for no detected mechanism "
                             f"(default: {DELETION_CSV})")
    parser.add_argument("--collected-dir", default=str(COLLECTED_DIR),
                        help=f"Collected tree, read for the files of the dropped repositories "
                             f"(default: {COLLECTED_DIR})")
    parser.add_argument("--validated-dir", default=str(VALIDATED_DIR),
                        help=f"Validated tree, read to leave out of the ranking the files that "
                             f"the top-level exclude of an Antigravity registration file leaves "
                             f"out (default: {VALIDATED_DIR})")
    parser.add_argument("--min-repos", type=int, default=PATHS_MIN_REPOS,
                        help="List only the path globs in at least this many repositories "
                             "(default: %(default)s)")
    parser.add_argument("--fail-above", type=int, default=None,
                        help="Exit 2 when an unmatched path glob occurs in this many "
                             "repositories or more")
    parser.add_argument("--out", default=None, help="CSV to write the ranked path globs to")
    args = parser.parse_args(argv)

    other_csv = Path(args.other_csv)
    if not other_csv.is_file():
        logs.error('unmatched_paths_report', f"{other_csv} does not exist. Run "
                                             f"`python -m agentcontext detection` first.")
        return 1

    # The validated tree tells apart the files that an exclude leaves out. The files of a
    # repository without a directory there, as in a checkout, are ranked with the rest.
    validated_dir = Path(args.validated_dir)
    try:
        counts, left_out = read_other(other_csv,
                                      validated_dir if validated_dir.is_dir() else None)
    except OSError as e:
        logs.error('unmatched_paths_report', f"{_not_read(e).rstrip('.')}.")
        return 1
    except UnicodeDecodeError as e:
        logs.error('unmatched_paths_report', f"{other_csv} is not valid UTF-8 ({e}).")
        return 1
    except ValueError as e:
        logs.error('unmatched_paths_report', f"{str(e).rstrip('.')}.")
        return 1

    # Every unmatched path glob, and the path globs in enough repositories to list. --min-repos
    # decides what is worth a reader's attention, and --fail-above decides what fails the check.
    # Applying --fail-above to the listed path globs alone would mean that a threshold below
    # --min-repos could never fail the check.
    all_unmatched, unmatched = [], []
    for glob, (files, repos) in counts.items():
        if glob in NOT_CONFIGURATION or is_explained(glob, REGISTERED):
            continue
        all_unmatched.append((len(repos), files, glob))
        if len(repos) >= args.min_repos:
            unmatched.append((len(repos), files, glob))
    unmatched.sort(reverse=True)

    # The repositories that the collected file cleanup dropped, with files in no output CSV.
    # The check reads them when both inputs exist and lists them separately, because a path glob
    # from `other.csv` comes from a published repository and a path glob from these repositories
    # does not, and a reader acts differently on the two. Missing inputs narrow the ranked list
    # rather than causing an error: The collected tree can be removed to save space, and the
    # deletion log belongs to the run that wrote it.
    deletion_csv, collected_dir = Path(args.deletion_csv), Path(args.collected_dir)
    dropped, dropped_note = [], ""
    if deletion_csv.is_file() and collected_dir.is_dir():
        try:
            dropped_counts = read_dropped(deletion_csv, collected_dir, SUBMODULES_CSV)
        except OSError as e:
            logs.error('unmatched_paths_report', f"{_not_read(e).rstrip('.')}.")
            return 1
        except UnicodeDecodeError as e:
            logs.error('unmatched_paths_report', f"{deletion_csv} or {SUBMODULES_CSV} is not "
                                                 f"valid UTF-8 ({e}).")
            return 1
        except ValueError as e:
            logs.error('unmatched_paths_report', f"{str(e).rstrip('.')}.")
            return 1
        for glob, (files, repos) in dropped_counts.items():
            if glob in NOT_CONFIGURATION or is_explained(glob, REGISTERED):
                continue
            dropped.append((len(repos), files, glob))
        dropped.sort(reverse=True)
    else:
        absent = deletion_csv if not deletion_csv.is_file() else collected_dir
        dropped_note = (f"Read none of the repositories that the cleanup dropped, because "
                        f"{absent} does not exist. A path glob found only in them is missing "
                        f"from the ranking.")

    # With --out, the file contains the path globs and the log the counts. The runner names a
    # file, so that its log contains what a person needs to decide whether to open the file, and
    # no list. Without --out, a person at a terminal requested the list and gets every row of it.
    if not args.out:
        logs.info('unmatched_paths_report', f"Unclassified path globs in {other_csv.name} in "
                                            f"{args.min_repos} or more repositories.")
        _table(unmatched)
    # Log both counts. Logging only the count at --min-repos would state "0 unmatched" for a path
    # glob in one or two repositories, which is the population that the check exists to find
    # before it exceeds the threshold. Logging only the total would read as a failure when
    # nothing reached the threshold.
    logs.info('unmatched_paths_report', f"{other_csv.name} has {len(unmatched):,} unmatched "
                                        f"path globs in {args.min_repos:,} repositories or more "
                                        f"and {len(all_unmatched):,} in total. "
                                        f"not_configuration.csv names "
                                        f"{len(NOT_CONFIGURATION):,} path globs.")
    if unmatched and not args.out:
        logs.info('unmatched_paths_report', "Add a mechanism to config_path_patterns.csv, or a "
                                            "row to heuristics/not_configuration.csv with a "
                                            "reason.")
    # Count the files that an exclude leaves out separately, because the exclude explains them
    # and no heuristic is missing for them.
    if left_out.files:
        logs.info('unmatched_paths_report', f"Did not rank {left_out.files:,} files of "
                                            f"{other_csv.name} in {len(left_out.repos):,} "
                                            f"repositories. The top-level exclude of an "
                                            f"Antigravity registration file leaves them out of a "
                                            f"default place.")
    if not validated_dir.is_dir():
        logs.info('unmatched_paths_report', f"{validated_dir} does not exist. Without it, the "
                                            f"files of {other_csv.name} that the top-level "
                                            f"exclude of an Antigravity registration file leaves "
                                            f"out are ranked with the unmatched files.")
    elif left_out.without_tree:
        logs.info('unmatched_paths_report', f"{validated_dir} has no directory for "
                                            f"{len(left_out.without_tree):,} repositories in "
                                            f"{other_csv.name}, such as "
                                            f"{min(left_out.without_tree)}. Their files that the "
                                            f"top-level exclude of an Antigravity registration "
                                            f"file leaves out are ranked with the unmatched "
                                            f"files.")

    over_min = [s for s in dropped if s[0] >= args.min_repos]
    if dropped_note:
        logs.info('unmatched_paths_report', dropped_note)
    else:
        if not args.out:
            logs.info('unmatched_paths_report', f"Unclassified path globs in {args.min_repos} or "
                                                f"more of the repositories dropped for no "
                                                f"detected mechanism.")
            _table(over_min)
        logs.info('unmatched_paths_report', f"The repositories that the cleanup dropped have "
                                            f"{len(over_min):,} unmatched path globs in "
                                            f"{args.min_repos:,} repositories or more and "
                                            f"{len(dropped):,} in total. Their files are in no "
                                            f"output CSV. A heuristic for them changes only the "
                                            f"next collection.")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(PATHS_OUT_COLUMNS)
            for found_in, rows in ((other_csv.name, unmatched), (DROPPED_FOUND_IN, over_min)):
                for repos, files, glob in rows:
                    writer.writerow([glob, repos, files, found_in])
        logs.info('unmatched_paths_report', f"Wrote {args.out}.")

    if args.fail_above is not None:
        over = [s for s in all_unmatched + dropped if s[0] >= args.fail_above]
        if over:
            worst = max(over)
            logs.error('unmatched_paths_report', f"{worst[2]} appears in {worst[0]:,} "
                                                 f"repositories, at or above the --fail-above "
                                                 f"threshold of {args.fail_above:,}.")
            return 2
    return 0


# -----------------------------------------------------------------------------
# The attributions that no commit pattern matches
# -----------------------------------------------------------------------------
UNMATCHED_CSV = STAGE_DIR / "output" / "all_ai_commits-unmatched.csv"

# Attributions that no commit pattern matches on purpose and that `unmatched commits` leaves out
# of its ranking. A key matches an entry when it is a substring of the token, because an
# identity contains whatever name and address the account uses, and only the account stays the
# same. Each key needs a reason, because the check exists so that a person reviews every
# unmatched attribution rather than ignoring it.
NOT_AI_COMMITS = not_ai_commits()

# The columns of the unmatched attributions that this check reads. The cloning step writes two
# more, a sample of the shas and of the lines that each attribution came from, which a person
# reads and no code does.
UNMATCHED_COLUMNS_READ = ("kind", "token", "commits", "repos")


def load_unmatched(path: Path) -> list:
    """The unmatched attributions as (kind, token, commits, repos) tuples. A file that is not
    UTF-8 or has a count that is not an integer raises ValueError with its path, because the
    texts of `UnicodeDecodeError` and of `int` name no file."""
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = set(UNMATCHED_COLUMNS_READ) - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{path} lacks the column(s) {sorted(missing)}")
            rows = list(reader)
    except UnicodeDecodeError as error:
        raise ValueError(f"{path} is not valid UTF-8 ({error})") from error
    try:
        return [(row["kind"], row["token"], int(row["commits"]), int(row["repos"]))
                for row in rows]
    except ValueError as error:
        raise ValueError(f"{path} has a count that is not an integer ({error})") from error


def explains(pattern: str, token: str) -> bool:
    """Whether a row of not_ai_commits.csv covers this attribution: its pattern is inside it."""
    return pattern in token


def explained_by(token: str) -> str:
    """The reason why this attribution credits no tool, or an empty string."""
    for key, reason in NOT_AI_COMMITS.items():
        if explains(key, token):
            return reason
    return ""


def unmatched_of(entries: list) -> list:
    """Every entry that no row of not_ai_commits.csv covers, most repositories first."""
    return sorted((e for e in entries if not explained_by(e[1])),
                  key=lambda e: (-e[3], -e[2], e[1]))


# The tokens of a trailer that the cloning step lists among the unmatched attributions
# (`commits.ATTRIBUTION_TOKENS`) can also follow a prefix with which the heuristics read a token
# as the token itself (`heuristics.TRAILER_TOKEN_PREFIXES`), such as `AI-assisted-by: Jcode
# (OpenAI)` of caddyserver/caddy 401582cccfb3, which no heuristic reads. The cloning step lists
# no such trailer, and the check reads them from the commit logs. Recorded by the cloning step
# instead, they would change the collector's code and the digest of the commit classification.
# A run would then count the collection as collected under other code, and the commit
# reclassification would classify every kept commit log again.
#
# A commit log with a line that begins with such a token is read commit by commit. The others,
# nearly all, are only searched, which needs no parse of their commits. A line of a trailer is
# stripped (`commits._extract_trailers`), and in the log of the cloning step a body begins after
# a NUL.
_PREFIXED_LINE = re.compile(
    rb"(?im)(?:^|\x00)[ \t]*(?!(?:" + b"|".join(re.escape(token.encode())
                                                for token in commits.ATTRIBUTION_TOKENS)
    + rb")[ \t]*:)(?:" + b"|".join(re.escape(prefix.encode()) for prefix in TRAILER_TOKEN_PREFIXES)
    + rb")-(?:" + b"|".join(re.escape(token.encode()) for token in commits.ATTRIBUTION_TOKENS)
    + rb")[ \t]*:")
# How much of a commit log is searched at a time, and how much of its end is searched again with
# the next part, so that a line across the border of two parts is found.
_SEARCH_CHUNK, _SEARCH_OVERLAP = 1 << 22, 256


def prefixed_credit(token: str) -> bool:
    """Whether a lowercase trailer token is a token of `commits.ATTRIBUTION_TOKENS` after one of
    the prefixes of `heuristics.TRAILER_TOKEN_PREFIXES`, such as `ai-assisted-by` or
    `co-written-by`, and is not such a token itself. `co-authored-by`, `co-authored-by-ai` and
    `co-developed-by` are tokens of their own, as the heuristics read them, and the cloning step
    lists them."""
    if token in commits.ATTRIBUTION_TOKENS:
        return False
    return any(token.startswith(prefix + "-")
               and token[len(prefix) + 1:] in commits.ATTRIBUTION_TOKENS
               for prefix in TRAILER_TOKEN_PREFIXES)


def _has_prefixed_line(log: Path) -> bool:
    """Whether the commit log at `log` has a line that begins with a token of `prefixed_credit`,
    in any case."""
    tail = b""
    with lzma.open(log, "rb") as stream:
        while chunk := stream.read(_SEARCH_CHUNK):
            if _PREFIXED_LINE.search(tail + chunk):
                return True
            tail = chunk[-_SEARCH_OVERLAP:]
    return False


def credits_of(commit: dict) -> set:
    """The `(kind, token)` pairs of the trailers of `commit`, a commit of a commit log, that
    credit somebody under a token of `prefixed_credit`, when no heuristic of either file of the
    commit heuristics matches the commit, and otherwise an empty set. The token keeps its prefix
    and is followed by the name that the trailer gives, without its address, as the cloning step
    writes the unmatched attributions (`commits.unmatched_attributions`)."""
    subject, body = commits.message_parts(commits.stored_message(commit))
    found = set()
    for line in commits._extract_trailers(body).splitlines():
        token, _, value = line.partition(":")
        token = token.strip().lower()
        if prefixed_credit(token):
            found.add(("trailer", f"{token}: {value.split('<')[0].strip().lower()}"[:80]))
    if found:
        fields = (commit["author_name"], commit["author_email"], commit["committer_name"],
                  commit["committer_email"], subject, body)
        if (commits.detect_ai_tools_in_fields(*fields)
                or commits.detect_other_ai_tools_in_fields(*fields)):
            return set()
    return found


def prefixed_credits(commits_dir: Path) -> tuple:
    """The credits under a token of `prefixed_credit` in the commits that no heuristic matches,
    as `(kind, token, commits, repos)` tuples, from the commit log of every per-repository
    commit record in `commits_dir` that records a commit history that was read, and the
    warnings for the logs that could not be read. A commit counts once per tuple, however many
    of its lines give it.

    A record is read only for a log with a line of such a token, and one record at a time. A
    record that does not parse adds nothing, as it adds no row to the commit CSVs
    (`commits.readable_shards`). A commit log that cannot be read adds nothing either, with a
    warning that names it, because the commit reclassification reports such a log and the
    ranking of the other logs still holds. The caller logs the warnings under its step key."""
    found, warnings = {}, []
    for json_path in sorted(commits_dir.glob("*.json")):
        repo = dir_name_to_repo(json_path.stem)
        log = commits.stored_log_path(commits_dir, repo)
        counted = {}
        try:
            if not log.is_file() or not _has_prefixed_line(log):
                continue
            try:
                record = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(record, dict) or record.get("outcome") != "history_read":
                continue
            log_format = (record.get("commit_evidence") or {}).get("log_format", "nul-v1")
            with lzma.open(log, "rb") as stream:
                for commit in commits._records_from(stream, log_format):
                    for pair in credits_of(commit):
                        counted[pair] = counted.get(pair, 0) + 1
        except (OSError, EOFError, lzma.LZMAError, ValueError) as error:
            warnings.append(f"Could not read the commit log {log} ({error}). Its trailers that "
                            f"credit somebody after a prefix, such as AI-assisted-by, are not "
                            f"ranked.")
            continue
        for pair, number in counted.items():
            entry = found.setdefault(pair, [0, 0])
            entry[0] += number
            entry[1] += 1
    return [(kind, token, number, repos) for (kind, token), (number, repos)
            in sorted(found.items())], warnings


# The directory of the per-repository commit records and the commit logs in the stage directory
# (`repository_cloning.COMMITS_DIR_NAME`).
COMMITS_DIR_NAME = "collect_commits"


def default_commits_dir(unmatched_csv: Path) -> Path | None:
    """The directory of the per-repository commit records and the commit logs of the collection
    that wrote `unmatched_csv`: `collect_commits/` beside the `output/` directory that holds it,
    as the cloning step writes both, or None when no `output/` directory holds it."""
    unmatched_csv = Path(unmatched_csv)
    if unmatched_csv.parent.name != "output":
        return None
    return unmatched_csv.parent.parent / COMMITS_DIR_NAME


def attributions(unmatched_csv: Path, commits_dir: Path | None) -> tuple:
    """The unmatched attributions of `unmatched_csv` (`load_unmatched`) with the credits under a
    token after a prefix in the commit logs of `commits_dir` (`prefixed_credits`), and the
    warnings for the caller to log under its step key. Without the directory, a warning says
    that these credits are missing."""
    entries = load_unmatched(unmatched_csv)
    if commits_dir is None or not commits_dir.is_dir():
        where = (f"{commits_dir} does not exist" if commits_dir is not None
                 else f"{unmatched_csv} is not in an output/ directory beside "
                      f"{COMMITS_DIR_NAME}/")
        return entries, [f"{where}. The attributions lack the trailers that credit somebody "
                         f"after a prefix that the heuristics read, such as AI-assisted-by, "
                         f"which only the commit logs contain. Name the directory of the commit "
                         f"logs with --commits-dir."]
    credits, warnings = prefixed_credits(commits_dir)
    return entries + credits, warnings


def main_commits(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Rank the unmatched attributions of the cloning step that "
                    "not_ai_commits.csv does not list. With --fail-above N, exit 2 when an "
                    "attribution occurs in N or more repositories.")
    parser.add_argument("--unmatched-csv", default=str(UNMATCHED_CSV),
                        help=f"The unmatched attributions of the cloning step (default: {UNMATCHED_CSV})")
    parser.add_argument("--commits-dir", default=None,
                        help=f"The per-repository commit records and the commit logs of the "
                             f"same collection, from which the check reads the trailers that "
                             f"credit somebody after a prefix that the heuristics read (default: "
                             f"{COMMITS_DIR_NAME}/ beside the output/ directory that holds "
                             f"--unmatched-csv)")
    parser.add_argument("--min-repos", type=int, default=3,
                        help="Only list the attributions in at least this many repositories "
                             "(default: 3)")
    parser.add_argument("--fail-above", type=int, default=None,
                        help="Exit 2 when an unmatched attribution occurs in this many "
                             "repositories or more")
    parser.add_argument("--out", default=None, help="CSV to write the ranked attributions to")
    args = parser.parse_args(argv)

    path = Path(args.unmatched_csv)
    if not path.is_file():
        logs.error('unmatched_commits_report', f"{path} does not exist. Run repository-cloning to "
                                               f"write it.")
        return 1
    commits_dir = (Path(args.commits_dir) if args.commits_dir is not None
                   else default_commits_dir(path))
    try:
        entries, warnings = attributions(path, commits_dir)
    except OSError as e:
        logs.error('unmatched_commits_report', f"{_not_read(e).rstrip('.')}.")
        return 1
    except ValueError as e:
        logs.error('unmatched_commits_report', f"{str(e).rstrip('.')}.")
        return 1
    for warning in warnings:
        logs.warning('unmatched_commits_report', warning)

    all_unmatched = unmatched_of(entries)
    reported = [e for e in all_unmatched if e[3] >= args.min_repos]
    explained = len(entries) - len(all_unmatched)

    # With --out, the log contains the counts and the file the attributions, as for `unmatched
    # paths`. The ranked list consists mostly of the names of the human co-authors of AI
    # commits, which are too many for the container's log.
    if not args.out:
        logs.info('unmatched_commits_report', f"Unmatched attributions in {path.name} in "
                                              f"{args.min_repos} or more repositories.")
        if not reported:
            logs.info('unmatched_commits_report', "  No such attribution.")
        else:
            width = max(len(e[1]) for e in reported)
            logs.info('unmatched_commits_report', f"  {'kind':<8} {'token':<{width}} "
                                                  f"{'repos':>6} {'commits':>8}")
            for kind, token, commits, repos in reported:
                logs.info('unmatched_commits_report', f"  {kind:<8} {token:<{width}} "
                                                      f"{repos:>6} {commits:>8}")

    logs.info('unmatched_commits_report', f"{path.name} has {len(reported):,} unmatched "
                                          f"attributions in {args.min_repos:,} repositories or "
                                          f"more and {len(all_unmatched):,} in total. "
                                          f"not_ai_commits.csv covers {explained:,} further "
                                          f"attributions.")
    if reported and not args.out:
        logs.info('unmatched_commits_report', "Add a row to ai_commit_patterns.csv, or a row to "
                                              "heuristics/not_ai_commits.csv with a reason.")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(["kind", "token", "repos", "commits"])
            for kind, token, commits, repos in reported:
                writer.writerow([kind, token, repos, commits])
        logs.info('unmatched_commits_report', f"Wrote {args.out}.")

    if args.fail_above is not None:
        over = [e for e in all_unmatched if e[3] >= args.fail_above]
        if over:
            worst = max(over, key=lambda e: e[3])
            logs.error('unmatched_commits_report', f"{worst[1]} appears in {worst[3]:,} "
                                                   f"repositories, at or above the --fail-above "
                                                   f"threshold of {args.fail_above:,}.")
            return 2
    return 0


# -----------------------------------------------------------------------------
# The tracked unmatched attributions
# -----------------------------------------------------------------------------
OUTPUT_DIR = STAGE_DIR / "output"
FINISHED = STAGE_DIR / resume.FINISHED_NAME

# The tracked copy is named for the day on which its collection started (`collection_started`),
# so that a reader can tell which collection the rows of not_ai_commits.csv were checked
# against. The repository tracks one copy at a time: `reduce` removes the copy that it replaces.
TRACKED_PREFIX = "all_ai_commits-unmatched-"
TRACKED_GLOB = TRACKED_PREFIX + "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].csv"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
# The name of a collection, which begins with the time at which it started
# (`resume.open_collection`). Names of this format sort in the order in which the collections
# started.
_COLLECTION_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})T\d{6}Z-[0-9a-f]+")


def tracked_name(started: str) -> str:
    """The name of the CSV of the collection that started on `started`, a YYYY-MM-DD day."""
    return f"{TRACKED_PREFIX}{started}.csv"


def tracked_archive(output_dir: Path = OUTPUT_DIR) -> Path:
    """The one tracked archive of a collection's unmatched attributions in `output_dir`.

    Raises ValueError when there is none or more than one, because then it is unclear which
    collection the rows are checked against.
    """
    found = sorted(output_dir.glob(TRACKED_GLOB + ".7z"))
    if len(found) != 1:
        raise ValueError(f"{output_dir} has {len(found)} archives named "
                         f"{TRACKED_GLOB}.7z instead of one ({[p.name for p in found]})")
    return found[0]


def read_tracked(archive: Path) -> list:
    """`load_unmatched` over the CSV beside `archive` when it is extracted, and otherwise over
    the archive extracted into a temporary directory.

    The CSV is gitignored and the archive is tracked, as for all_ai_commits.csv, and a clean
    checkout has only the archive. Raises FileNotFoundError when the CSV is not extracted
    and 7zz is not on PATH, and RuntimeError when 7zz fails or the archive does not contain the
    CSV.
    """
    extracted = archive.with_suffix("")
    if extracted.is_file():
        return load_unmatched(extracted)
    if shutil.which(SEVEN_ZIP) is None:
        raise FileNotFoundError(f"7zz is not on PATH, and {extracted.name} is not extracted "
                                f"beside {archive}")
    with tempfile.TemporaryDirectory() as work:
        result = subprocess.run([SEVEN_ZIP, "x", "-bso0", "-bsp0", "-y", f"-o{work}", str(archive)],
                                capture_output=True, text=True)
        found = sorted(p for p in Path(work).rglob(extracted.name) if p.is_file())
        if result.returncode != 0 or len(found) != 1:
            raise RuntimeError(f"7zz could not extract {extracted.name} from {archive} (exit "
                               f"{result.returncode}, {len(found)} file(s) of that name, "
                               f"{result.stderr.strip()[-300:]})")
        return load_unmatched(found[0])


def collection_started(finished_path: Path) -> str:
    """The day on which the current collection over the list of finished repositories at
    `finished_path` started, as YYYY-MM-DD.

    The earliest `scanned_at` is not that day. A repository that a collection takes over keeps
    the line of the earlier collection that collected it, with its `scanned_at`, and the
    earliest `scanned_at` can be a day of an earlier month. The current collection is the
    collection in progress that the record beside the list names (`resume.collection_path`).
    Once that collection has ended, its record becomes the record of the last collection that
    ended (`resume.ended_path`), and the current collection is the latest collection that a line
    names, because every line names the collection that wrote it. A list in which no line records how its repository was collected (`collected_under`)
    contains no repository taken over, because a collection collects such a repository again
    (`reuse.outdated`). One collection then scanned every repository, and it started on the day
    of the earliest `scanned_at`.

    Raises ValueError when the list does not show the day: when it is missing or empty, or
    when its lines record how their repositories were collected and name no collection.
    """
    names = []
    try:
        record = json.loads(resume.collection_path(finished_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        record = None
    if isinstance(record, dict) and isinstance(record.get("collection"), str):
        names = [record["collection"]]
    lines = resume.read_finished(finished_path)
    if not names:
        names = [line[10] for line in lines.values() if isinstance(line[10], str)]
    started = [match.group(1) for match in map(_COLLECTION_NAME.fullmatch, names) if match]
    if started:
        return max(started)
    if not lines:
        raise ValueError(f"{finished_path} does not exist or lists no repository. The day on "
                         f"which the collection started is unknown.")
    if any(line[6] for line in lines.values()):
        raise ValueError(f"No line of {finished_path} names the collection that wrote it, and "
                         f"its repositories may have been taken over from an earlier collection "
                         f"(collected_under). The day on which the collection started is "
                         f"unknown.")
    days = {paths.row_column(line[0], "scanned_at")[:10] for line in lines.values()}
    days = {day for day in days if _DAY.fullmatch(day)}
    if not days:
        raise ValueError(f"No line of {finished_path} has a scanned_at. The day on which the "
                         f"collection started is unknown.")
    return min(days)


# -----------------------------------------------------------------------------
# The attributions that name a bot or a tool and no person
# -----------------------------------------------------------------------------
# `reduce` keeps a line of the unmatched attributions only when it names a bot or a tool and no
# person, because the archive that it writes is tracked and published. The full list stays in the
# handover of each run, and `unmatched commits` ranks it there. The rule is an inclusion rule: A
# line stays only when its form shows that it names a bot or a tool, and a line that fits no form
# is dropped. The rule can therefore lose a bot, which the full list still shows, and a name that
# a person also has takes the line out of the archive rather than into it.

# A word `bot` of a name, delimited, so that names such as Abbot and Talbot are no bots.
_BOT_WORD = re.compile(r"(?<![a-z0-9])bot(?![a-z0-9])")
_APP_MARK = "[bot]"
# What separates the words of a name, as in `snyk-bot`, `snyk bot` and `snyk_bot`.
_WORD_SEPARATORS = re.compile(r"[ \-_.]+")
# The tools of the commit heuristics by name. The other two values of the `tool` column say that
# a heuristic names no particular tool.
_TOOLS = frozenset(row["tool"].lower()
                   for row in (*commits.AI_COMMIT_PATTERNS, *commits.OTHER_AI_COMMIT_PATTERNS)
                   if row["tool"] not in ("tool not named", "Gemini (unspecified)"))
# What follows the name of a tool in a line that credits the tool: a model after a colon or a
# slash, a parenthesis that ends the name, a version, or a word that names the tool's kind.
# Anything else, as in a tool's name followed by a surname, makes the name of the tool the first
# name of a person.
_TOOL_WORDS = ("agent", "ai", "assistant", "code", "cli")
_AFTER_TOOL = re.compile(r"\s*[:/]\s*\S|\s*\([^()]*\)\s*$|[ \-_]v?\d|[ \-_](?:"
                         + "|".join(_TOOL_WORDS) + r")(?![a-z0-9])")
# A trailer token that itself marks what it credits as a coding agent, as in `Co-authored-by-agent:
# Cursor` and `AI-assisted-by: Jcode (OpenAI)`.
_AGENT_TOKEN = re.compile(r"ai-.*|.*-(?:ai|agent)")
# The mailboxes of a bot or of a service that reads no mail. A person's address can end in
# `.bot`, as in `first.bot@example.org`, which the separators of this form rule out.
_BOT_MAILBOX = re.compile(r"noreply|no-reply|support|(?:.*[-_])?(?:ro)?bot")
# The domains where an address is a person's account, whatever its mailbox says.
_PERSONAL_MAIL = frozenset({"126.com", "163.com", "gmail.com", "googlemail.com", "hotmail.com",
                            "icloud.com", "live.com", "me.com", "outlook.com", "proton.me",
                            "protonmail.com", "qq.com", "yahoo.com"})
# A first word counts as a given name when plain names begin with it and differ in the second
# word, as first names do across surnames.
_GIVEN_NAME_SURNAMES = 2
_GITHUB_NOREPLY = "users.noreply.github.com"
# The second-level labels of a country's suffix, as in `example.co.uk`, which name no organization.
_COUNTRY_SECOND_LEVEL = frozenset({"ac", "co", "com", "edu", "gov", "net", "org"})


@dataclass(frozen=True)
class PersonalNames:
    """What the lines of one collection that name no bot or tool show about people's names:
    `whole`, their names with the words joined by a space, `given`, the first words that count
    as given names, and `first`, every first word of a name of two words."""
    whole: frozenset
    given: frozenset
    first: frozenset


def _name_and_address(kind: str, token: str) -> tuple:
    """The name and the address of a line. A trailer gives only a name, and the limit of the
    cloning step on the length of a token can cut an address before its end."""
    if kind == "trailer":
        return token.partition(":")[2].strip(), ""
    name, _, address = token.rpartition(" <")
    return name, address.rstrip(">")


def _words(name: str) -> list:
    """The words of a name, without the mark of an app and the word `bot`."""
    text = _BOT_WORD.sub(" ", name.replace(_APP_MARK, " "))
    return [word for word in _WORD_SEPARATORS.split(text) if word]


def _github_account(address: str) -> str | None:
    """The account of an address at GitHub's own domain, after the number that begins it, or
    None for any other address. The limit on the length of a token can cut the domain."""
    local, at, domain = address.partition("@")
    if not at or not domain or not (_GITHUB_NOREPLY.startswith(domain)
                                    or domain == "noreply.github.com"):
        return None
    return local.partition("+")[2] or local


def _names_a_bot(name: str) -> bool:
    """Whether a name has the mark of a GitHub App or the word `bot`."""
    return _APP_MARK in name or _BOT_WORD.search(name) is not None


def _negative_in(name: str) -> bool:
    """Whether a row of not_ai_commits.csv is in a name, before any parenthesis, because a
    parenthesis can add the name of the automation to the name of a person."""
    head = name.partition("(")[0]
    return any(explains(pattern, head) for pattern in NOT_AI_COMMITS)


def _tool_of(name: str) -> str | None:
    """The tool at the start of a name, the longest when several fit, or None."""
    tools = [tool for tool in _TOOLS if name.startswith(tool)]
    return max(tools, key=len) if tools else None


def personal_names(entries: list) -> PersonalNames:
    """The names of people that the trailers of `entries`, `(kind, token, ...)` tuples, show:
    the names without a form of a bot or a tool, and without a token that marks an agent. A name
    that the entries show as the account of a GitHub App at GitHub's own address is an app's,
    because only such an account shows that the app exists. `keeps` drops the name of a bot that
    is built on a person's name."""
    apps = set()
    for kind, token, *_counts in entries:
        if kind != "trailer":
            account = _github_account(_name_and_address(kind, token)[1])
            if account is not None and _APP_MARK in account:
                apps.add(" ".join(_words(account)))
    surnames = defaultdict(set)
    whole = set()
    for kind, token, *_counts in entries:
        if kind != "trailer" or _AGENT_TOKEN.fullmatch(token.partition(":")[0].strip()):
            continue
        name = _name_and_address(kind, token)[0]
        if (not name or _names_a_bot(name) or _negative_in(name)
                or " ".join(_words(name)) in apps):
            continue
        words = _WORD_SEPARATORS.split(name)
        if len(words) == 2 and words[1] in _TOOL_WORDS:
            continue
        whole.add(" ".join(words))
        if len(words) == 2 and all(word.isalpha() for word in words):
            surnames[words[0]].add(words[1])
            # A person's account often joins the words, as in `firstlast-bot`.
            whole.add("".join(words))
    return PersonalNames(frozenset(whole),
                         frozenset(first for first, seconds in surnames.items()
                                   if len(seconds) >= _GIVEN_NAME_SURNAMES),
                         frozenset(surnames))


def _names_a_person(name: str, people: PersonalNames, marked: bool) -> bool:
    """Whether the name of a bot begins with a given name, as in a surname that ends in the word
    `bot`, or with the first two words of a person's name, or is one word that a trailer gives
    alone. A given name does not count when the line has the mark of a GitHub App (`marked`),
    which a person's surname does not bring, as in `github-actions[bot]`."""
    words = _words(name)
    if not words:
        return False
    return ((not marked and words[0] in people.given) or " ".join(words[:2]) in people.whole
            or (len(words) == 1 and words[0] in people.whole))


def _address_names_no_person(name: str, address: str) -> bool:
    """Whether the address of an author line is an account of a bot and of no person."""
    if not address:
        return True
    local, at, domain = address.partition("@")
    if not at:
        # The limit on the length of a token cut the address before its at sign. The address of
        # an account begins with its number and then repeats the name.
        number, plus, account = address.partition("+")
        return bool(plus) and number.isdigit() and name.startswith(account)
    account = _github_account(address)
    if account is not None:
        return _APP_MARK in account or re.search(r"[-_]bot$", account) is not None
    if domain in _PERSONAL_MAIL or "+" in local:
        return False
    # A row of not_ai_commits.csv names the automation, and its mailbox can be at any domain, as
    # in the identity under which a vendor's release workflows commit.
    if local == name and _negative_in(name):
        return True
    if not _BOT_MAILBOX.fullmatch(local):
        return False
    # The domain has to fit the name, as in `snyk-bot <snyk-bot@snyk.io>`, because a mailbox such
    # as `bot@` says nothing about whose domain it is.
    labels = domain.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _COUNTRY_SECOND_LEVEL:
        labels = labels[:-1]
    label = labels[-2] if len(labels) > 1 else labels[0]
    return (label in name.replace(" ", "")
            or any(len(word) >= 4 and word in label for word in _words(name)))


def _app_of_the_address(name: str, address: str) -> bool:
    """Whether a name without a mark of a bot repeats the account of a GitHub App in its address,
    as in `mergify <37929162+mergify[bot]@users.noreply.github.com>`."""
    account = address.partition("@")[0]
    account = account.partition("+")[2] or account
    return (_APP_MARK[:-1] in account
            and _words(name) == _words(account.partition("[")[0]))


def keeps(kind: str, token: str, people: PersonalNames) -> bool:
    """Whether the archive keeps a line: its form shows that it names a bot or a tool, and its
    name and address name no person."""
    name, address = _name_and_address(kind, token)
    if not name or "@" in name or "'" in name:
        return False
    if kind != "trailer" and not _address_names_no_person(name, address):
        return False
    if _negative_in(name):
        return True
    if _names_a_bot(name) or (kind != "trailer" and _app_of_the_address(name, address)):
        marked = _APP_MARK in name or _APP_MARK in address
        return "(" not in name and not _names_a_person(name, people, marked)
    tool = _tool_of(name)
    if tool is not None:
        rest = name[len(tool):]
        return (_AFTER_TOOL.match(rest) is not None if rest else tool not in people.first)
    if kind != "trailer" or _AGENT_TOKEN.fullmatch(token.partition(":")[0].strip()) is None:
        return False
    # A name under such a token can be a person's that the same list shows as a plain trailer.
    words = _WORD_SEPARATORS.split(name)
    return " ".join(words) not in people.whole and words[0] not in people.given


def bots_and_tools(entries: list) -> list:
    """The entries, `(kind, token, commits, repos)` tuples, that the archive keeps."""
    people = personal_names(entries)
    return [entry for entry in entries if keeps(entry[0], entry[1], people)]


def main_reduce(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Write the unmatched attributions of a collection that name a bot or a tool "
                    f"and no person as the tracked {TRACKED_PREFIX}<day the collection "
                    "started>.csv.7z, with the four columns that `unmatched commits` reads, and "
                    "remove the archive that it replaces. Run it where the collection's handover "
                    "is extracted, in a "
                    "checkout or in a working copy with AGENTCONTEXT_DATA_ROOT set to its data "
                    "root. Copy the archive from a working copy into the checkout "
                    "(docs/development.md).")
    parser.add_argument("--unmatched-csv", default=str(UNMATCHED_CSV),
                        help=f"The unmatched attributions of the cloning step (default: {UNMATCHED_CSV})")
    parser.add_argument("--commits-dir", default=None,
                        help=f"The per-repository commit records and the commit logs of the "
                             f"same collection, from which the archive gets the trailers that "
                             f"credit somebody after a prefix that the heuristics read (default: "
                             f"{COMMITS_DIR_NAME}/ beside the output/ directory that holds "
                             f"--unmatched-csv)")
    parser.add_argument("--finished", default=str(FINISHED),
                        help=f"List of finished repositories of the same collection, read with "
                             f"the record of the collection in progress beside it for the day on "
                             f"which the collection started (default: {FINISHED})")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR),
                        help=f"Directory to write the archive to (default: {OUTPUT_DIR})")
    args = parser.parse_args(argv)

    unmatched_csv, finished = Path(args.unmatched_csv), Path(args.finished)
    output_dir = Path(args.output_dir)
    for path in (unmatched_csv, finished):
        if not path.is_file():
            logs.error("unmatched", f"{path} does not exist. Extract the collection's handover "
                                    f"into the checkout first, or name the file.")
            return 1
    if not output_dir.is_dir():
        logs.error("unmatched", f"{output_dir} is not a directory.")
        return 1
    if shutil.which(SEVEN_ZIP) is None:
        logs.error("unmatched", "7zz is not on PATH, and the tracked file is a 7z archive.")
        return 1
    commits_dir = (Path(args.commits_dir) if args.commits_dir is not None
                   else default_commits_dir(unmatched_csv))
    try:
        entries, warnings = attributions(unmatched_csv, commits_dir)
        started = collection_started(finished)
    except OSError as e:
        logs.error("unmatched", f"{_not_read(e).rstrip('.')}.")
        return 1
    except UnicodeDecodeError as e:
        logs.error("unmatched", f"{finished} is not valid UTF-8 ({e}).")
        return 1
    except ValueError as e:
        logs.error("unmatched", f"{str(e).rstrip('.')}.")
        return 1
    for warning in warnings:
        logs.warning("unmatched", warning)
    kept = bots_and_tools(entries)

    name = tracked_name(started)
    target = output_dir / f"{name}.7z"
    with tempfile.TemporaryDirectory() as work:
        written = Path(work) / name
        with open(written, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(UNMATCHED_COLUMNS_READ)
            writer.writerows(kept)
        # The timestamps are off, as in the archives of the publication stage, so that one
        # handover gives the same bytes on every run and a second run changes nothing that git
        # tracks.
        packed = Path(work) / target.name
        result = subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", "-mx=9", "-mtc=off", "-mtm=off",
                                 "-mta=off", str(packed), name],
                                cwd=work, capture_output=True, text=True)
        if result.returncode != 0:
            logs.error("unmatched", f"7zz exited {result.returncode} "
                                    f"({result.stderr.strip()[-500:]}).")
            return 1
        # The copy is written beside the tracked name and renamed onto it, so that a copy that
        # stops part way, such as on a full disk, leaves the tracked archive unchanged rather
        # than truncated.
        partial = target.with_name(target.name + ".partial")
        try:
            shutil.copyfile(packed, partial)
            os.replace(partial, target)
        except OSError as e:
            partial.unlink(missing_ok=True)
            logs.error("unmatched", f"Could not write {target} ({e}). The tracked archive and "
                                    f"the copies that it would replace are unchanged.")
            return 1

    # Remove every other tracked copy and every extracted CSV. The readers prefer an extracted
    # CSV to the archive and would otherwise still read an extracted copy of the file that this
    # archive replaces.
    replaced = sorted(p for pattern in (TRACKED_GLOB, TRACKED_GLOB + ".7z")
                      for p in output_dir.glob(pattern) if p != target)
    for path in replaced:
        path.unlink()

    logs.info("unmatched", f"Wrote {target} with the {len(kept):,} attributions that name a bot "
                           f"or a tool, of the {len(entries):,} unmatched attributions of the "
                           f"collection that started on {started}, read from {unmatched_csv}.")
    for path in replaced:
        logs.info("unmatched", f"Removed {path}.")
    logs.info("unmatched", f"Commit the archive{' and the removal' if replaced else ''}. "
                           f"tests/test_checks_unmatched.py checks every row of "
                           f"heuristics/not_ai_commits.csv against it.")
    return 0


SUBCOMMANDS = {
    "paths": main_paths,
    "commits": main_commits,
    "reduce": main_reduce,
}


SUBCOMMAND_SUMMARY = ('paths ranks the path globs in other.csv that no heuristic explains, commits '
                      'ranks the attributions that no commit pattern explains, and reduce writes '
                      'the unmatched attributions of a collection that name a bot or a tool as the '
                      'tracked archive that the tests check the rows of not_ai_commits.csv against')


def main(argv=None) -> int:
    return cli.subcommand_main("unmatched", SUBCOMMANDS, SUBCOMMAND_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
