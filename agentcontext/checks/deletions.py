"""Summarize and check the deletion log: `python -m agentcontext deleted-files-report`.

The collected file cleanup writes `deletion.csv`, one row for every file, empty directory and
repository that it removes, each with a reason. The check counts the rows per reason with the
repositories of each reason, lists the files that a repository row covers, and compares the log
with the trees and the mechanism CSVs in three assertions:

- No (repository, path) pair that a mechanism CSV or the CSV of the planning documents
  publishes is a path that the cleanup removed or lies under a directory that the cleanup
  removed. A path that is both published and deleted means that two steps treat one file in
  opposite ways.
- No `SKILL.md` that the cleanup deleted lies in a directory that a symlink marker of the
  collected tree names, whatever reason the log records. The exceptions are the reasons of the
  two passes that run before any keep rule, for an empty file and for a link that leads out of
  the repository. Such a row means that two steps treat one file in opposite ways: The collector
  followed a link and copied the skill, and the cleanup deleted it. The cleanup deletes a
  `SKILL.md` anywhere else by design, and the check counts those rows rather than failing on them.
- Every repository directory of `data/collected/` that is missing from `data/validated/` has a
  repository row, and every repository row names a directory that is missing from
  `data/validated/`. A missing directory without a row is a deletion that nothing else records.

Inputs:  deletion.csv; data/collected/ and data/validated/ with their lists and
         data/collected/clone_failed.csv; the mechanism CSVs and planning-documents.csv
         under output/;
         heuristics/config_path_patterns.csv and heuristics/not_configuration.csv, read to find
         the path globs under a removed repository that a heuristic already names
Outputs: the counts and the three verdicts as log messages on stderr, and with --out a CSV with one
         row per reason and repository, per path glob and repository, and per finding

The exit code is the verdict, as for the other verification commands: 0 when all three
assertions held, 2 when one did not, and 1 when the log or a mechanism CSV could not be read or a
tree contained no repository directory. In the last case, the log messages name the assertions
that were not checked. The runner's step runs the check with `--report-only`, which exits 0
whatever the assertions found, and docs/checks/deletions.md gives the reason.
"""

import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

from agentcontext import STAGE_DIR, heuristics, logs, planning, planning_schema, repos
from agentcontext import cli
from agentcontext.checks.unmatched import (DROPPED_REASON, heuristics_paths, is_explained,
                                           path_glob)
from agentcontext.paths import is_discarded_path
from agentcontext.steps import collected_file_cleanup

DELETION_CSV = STAGE_DIR / "deletion.csv"
COLLECTED_DIR = STAGE_DIR / "data" / "collected"
VALIDATED_DIR = STAGE_DIR / "data" / "validated"
OUTPUT_DIR = STAGE_DIR / "output"

# The reason that `collected_file_cleanup.scan_and_clean_repo` records for a `SKILL.md` that no
# keep rule kept. The constant repeats the string because the step writes it inline, and
# tests/test_checks_deletions.py checks that the real step writes it.
SKILL_MD_REASON = "SKILL.md outside valid directory"
# The reasons of the two passes that run before any keep rule, wherever a file lies: a file that
# is empty, and a link that leads out of the repository. The second assertion skips a linked
# `SKILL.md` that either pass deleted, because such a deletion shows no conflict about where a
# skill may be. Every other reason shows such a conflict, including the submodule rule, which
# overrides the keep rule for a linked directory, and the vendored rule, which applies below a
# linked directory.
BEFORE_ANY_KEEP_RULE = ("empty file", "symlink leading outside the repository")

# The columns of the CSV that --out writes. `kind` names the type of a row, and
# the section "The CSV" of docs/checks/deletions.md defines the other four columns for each kind.
OUT_COLUMNS = ("kind", "repo_name", "subject", "count", "note")


def reason_of(reason: str) -> str:
    """The reason without the detail that a run puts in parentheses after it, such as the
    submodule's path, the vendored directory's name or the spelling that a context file should
    have had."""
    return reason.split(" (", 1)[0]


def split_path(path: str) -> tuple:
    """(repository directory name, path inside it) of a path that the log records.

    The log's paths are relative to the directory that the collected file cleanup ran in. The
    repository is the segment that is a repository directory name, wherever it lies, such as
    under `data/validated/`, or under `repos/` in an older log. A path with no such segment
    returns an empty name.
    """
    parts = path.split("/")
    for i, part in enumerate(parts):
        if repos.is_repo_dir_name(part):
            return part, "/".join(parts[i + 1:])
    return "", path


# A symlink marker is a one-line file that contains its target, and the collector writes one for
# a link to a target of any size. A file larger than this bound is content. The detection step
# reads markers under the same bound.
MARKER_MAX_BYTES = 4096


def linked_directories(repo_dir: Path) -> list:
    """The directories inside a collected repository that one of its symlink markers names."""
    found = set()
    for root, _dirs, files in os.walk(repo_dir):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path) or os.path.getsize(path) > MARKER_MAX_BYTES:
                continue
            target = collected_file_cleanup._marker_target_dir(str(repo_dir), path)
            if target:
                found.add(target.replace(os.sep, "/"))
    return sorted(found)


def read_log(path: Path) -> list:
    """The log as (repository directory name, path inside it, reason) triples."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = {"path", "reason"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the column(s) {sorted(missing)}")
        return [(*split_path(row["path"]), row["reason"]) for row in reader]


def read_published(output_dir: Path) -> dict:
    """{repository name: {path: row count}} over the mechanism CSVs and the planning
    documents, which the release publishes as well.

    Every CSV is required, because with one CSV missing, "nothing published was deleted" would
    hold only for the CSVs that were present.
    """
    published: dict = defaultdict(Counter)
    tables = [(filename, heuristics.artifact_column_for_file(filename))
              for filename in heuristics.csv_filenames()]
    tables.append((planning_schema.CSV_NAME, planning_schema.PATH_COLUMN))
    for filename, column in tables:
        path = output_dir / filename
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            missing = {"repo_name", column} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(f"{path} lacks the column(s) {sorted(missing)}")
            for row in reader:
                published[row["repo_name"]][row[column]] += 1
    return published


def read_other_paths(output_dir: Path) -> list:
    """The paths of `other.csv`, the unclassified files that the kept repositories have.

    `read_published` has already rejected the file if it lacks the column.
    """
    column = heuristics.artifact_column_for_file("other.csv")
    with open(output_dir / "other.csv", newline="", encoding="utf-8") as f:
        return [row[column] for row in csv.DictReader(f)]


def repository_dirs(tree: Path, expected: tuple) -> set:
    """The repository directory names directly under `tree`, or none when the tree is absent."""
    if not tree.is_dir():
        return set()
    found, _strays = repos.split(tree, expected=expected)
    return set(found)


def read_list(path: Path) -> set:
    """The repository directory names that a tree's `repos.csv` lists, or none when it is absent."""
    if not path.is_file():
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if "repo_name" not in (reader.fieldnames or []):
            raise ValueError(f"{path} lacks the column(s) ['repo_name']")
        return {repos.repo_name_to_dir(row["repo_name"]) for row in reader if row["repo_name"]}


def entries_of(root: Path) -> list:
    """Every path under `root` that is not a directory, relative to it, links included.

    A link to a directory is an entry and is not followed: The collector writes a symlinked
    mechanism directory as a marker, and the cleanup classifies the link by its own path.
    """
    found = []
    for directory, subdirs, files in os.walk(root):
        base = Path(directory)
        linked = [d for d in subdirs if (base / d).is_symlink()]
        for name in sorted(files + linked):
            found.append((base / name).relative_to(root).as_posix())
    return found


def unnamed_files(root: Path, rows: list) -> list:
    """The files of a removed repository that no row of the log names.

    A repository row covers every file that the repository still contained when the cleanup
    removed it, and the log names none of these files. The files that remain are the files that
    every validity heuristic kept, because the cleanup first removed the files and the empty
    directories that the earlier rows name. The function skips a file that a file manager wrote
    into the collected tree after the cleanup, such as below an `@eaDir` that DSM wrote on the
    NAS (`is_discarded_path`).
    """
    named = [p for p in rows if p]
    return [rel for rel in entries_of(root)
            if not is_discarded_path(rel)
            and not any(rel == p or rel.startswith(p + "/") for p in named)]


def published_and_deleted(log: list, published: dict) -> list:
    """(repository, removed path, published rows under it, reason) for every conflict.

    A removed path covers a published row when the row's path is that path or lies under it. A
    file row names one path, and the row of an empty directory or of a repository names a tree.
    """
    found = []
    for name, rel, reason in log:
        paths = published.get(repos.dir_name_to_repo(name)) if name else None
        if not paths:
            continue
        rows = sum(n for p, n in paths.items()
                   if not rel or p == rel or p.startswith(rel + "/"))
        if rows:
            found.append((repos.dir_name_to_repo(name), rel, rows, reason))
    return found


def compare_trees(removed: set, collected: set, validated: set, validated_list: set,
                  clone_failures: set, collected_list: "set | None" = None) -> dict:
    """The repository rows against the two trees, as named sets of directory names.

    The assertion fails on `unrecorded`, `row_still_validated` and `row_never_collected`. The
    other three sets explain a directory that is missing from `data/validated/` without a row.
    `not_copied` contains the repositories that `clone_failed.csv` names, which the cleanup does
    not copy. `not_listed` contains the repositories that `data/collected/repos.csv` does not
    name, which the cleanup does not copy either. `removed_later` contains the repositories that
    `data/validated/repos.csv` still lists, which a step after the cleanup removed, as the file
    history step does for a repository that git can no longer clone. `collected_list` is None
    when the collected list does not exist.
    """
    left = collected - validated
    not_logged = left - removed
    not_copied = {d for d in not_logged if repos.dir_name_to_repo(d) in clone_failures}
    not_listed = (set() if collected_list is None
                  else (not_logged - not_copied) - collected_list)
    removed_later = (not_logged - not_copied - not_listed) & validated_list
    return {"left": left,
            "not_copied": not_copied,
            "not_listed": not_listed,
            "removed_later": removed_later,
            "unrecorded": not_logged - not_copied - not_listed - removed_later,
            "row_still_validated": removed & validated,
            "row_never_collected": removed - collected - validated}


# With --out, the console shows the counts and the file contains the rows. The runner passes a
# file, and the container's log then records the counts and the file's path but none of the rows.
# Without --out, a person at a terminal requested the lists and gets every row.
def _shown(rows: list, out) -> list:
    """All of a list, or none of it when --out names a file that contains it."""
    return [] if out else rows


def _more(rows: list, out, what: str) -> None:
    """Log the file that contains the rows of a list, when --out names one. `what` names the
    rows."""
    if out and rows:
        logs.info("deleted_files_report", f"    {out} lists the {len(rows):,} {what}.")


def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Summarize the deletion log of the collected file cleanup (the rows per "
                    "reason with their repositories, the files of each removed repository) and "
                    "check it against the trees and the mechanism CSVs in three assertions. Exit "
                    "code 2 when an assertion fails, 1 when one could not be checked.")
    parser.add_argument("--deletion-csv", default=str(DELETION_CSV),
                        help=f"Deletion log of the collected file cleanup (default: {DELETION_CSV})")
    parser.add_argument("--collected-dir", default=str(COLLECTED_DIR),
                        help=f"The collected tree, with clone_failed.csv (default: {COLLECTED_DIR})")
    parser.add_argument("--validated-dir", default=str(VALIDATED_DIR),
                        help=f"The validated tree, with its repos.csv (default: {VALIDATED_DIR})")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR),
                        help=f"Directory of the mechanism CSVs (default: {OUTPUT_DIR})")
    parser.add_argument("--out", default=None,
                        help="CSV to write the counts, the path globs and the findings to")
    parser.add_argument("--report-only", action="store_true",
                        help="Exit 0 whatever the assertions find (the runner's step passes it)")
    args = parser.parse_args(argv)

    deletion_csv = Path(args.deletion_csv)
    collected_dir, validated_dir = Path(args.collected_dir), Path(args.validated_dir)
    output_dir = Path(args.output_dir)
    if not deletion_csv.is_file():
        logs.error("deleted_files_report", f"{deletion_csv} does not exist. Run "
                                           f"collected-file-cleanup to write it.")
        return 1
    # The input being read, which the error of a file that is not UTF-8 names. The text of
    # `UnicodeDecodeError` names no file.
    reading = str(deletion_csv)
    try:
        log = read_log(deletion_csv)
        reading = f"An output CSV in {output_dir}"
        published = read_published(output_dir)
        other_paths = read_other_paths(output_dir)
        collected = repository_dirs(collected_dir, collected_file_cleanup.COLLECTED_TREE_FILES)
        validated = repository_dirs(validated_dir, collected_file_cleanup.VALIDATED_TREE_FILES)
        reading = str(validated_dir / "repos.csv")
        validated_list = read_list(validated_dir / "repos.csv")
        reading = str(collected_dir / "repos.csv")
        collected_list = (read_list(collected_dir / "repos.csv")
                          if (collected_dir / "repos.csv").is_file() else None)
        # Inside the `try`, so that a list that is not UTF-8 ends in an error message rather
        # than a traceback.
        reading = str(collected_dir / "clone_failed.csv")
        clone_failures = collected_file_cleanup.load_clone_failures(reading)
    except OSError as e:
        # An error of the operating system names its file in `filename`.
        logs.error("deleted_files_report", f"{str(e).rstrip('.')}." if e.filename is None
                   else f"Could not read {e.filename} ({e.strerror}).")
        return 1
    except UnicodeDecodeError as e:
        logs.error("deleted_files_report", f"{reading} is not valid UTF-8 ({e}).")
        return 1
    except ValueError as e:
        logs.error("deleted_files_report", f"{str(e).rstrip('.')}.")
        return 1
    out_rows = []

    # The rows per reason, with the repositories of each reason.
    by_reason: dict = defaultdict(Counter)
    for name, _rel, reason in log:
        by_reason[reason_of(reason)][repos.dir_name_to_repo(name) if name else ""] += 1
    ranked = sorted(by_reason.items(), key=lambda item: (-sum(item[1].values()), item[0]))
    behind = {name for name, _rel, _reason in log if name}
    logs.info("deleted_files_report", f"{deletion_csv.name} has {len(log):,} rows with "
                                      f"{len(by_reason):,} reasons in {len(behind):,} "
                                      f"repositories.")
    if not args.out:
        logs.info("deleted_files_report", f"  {'rows':>7} {'repos':>6}  reason, and the "
                                          f"repository with the most rows")
    for reason, per_repo in ranked:
        if not args.out:
            top, top_rows = max(per_repo.items(), key=lambda item: (item[1], item[0]))
            logs.info("deleted_files_report", f"  {sum(per_repo.values()):>7,} "
                                              f"{len(per_repo):>6,}  {reason}, "
                                              f"{top or 'no repository'} {top_rows:,}")
        for repo, rows in sorted(per_repo.items(), key=lambda item: (-item[1], item[0])):
            out_rows.append(("reason", repo, reason, rows, ""))

    # The files that the repository rows cover: the files that each removed repository still
    # contained, which no row of the log names, and the path globs that they fall under.
    removed = {name for name, rel, reason in log if name and not rel and reason == DROPPED_REASON}
    rows_of: dict = defaultdict(list)
    for name, rel, _reason in log:
        if name in removed:
            rows_of[name].append(rel)
    quiet = sorted(d for d in removed if rows_of[d] == [""])
    logs.info("deleted_files_report", f"The cleanup removed {len(removed):,} repositories for "
                                      f"{DROPPED_REASON!r}, and {len(quiet):,} of them have no "
                                      f"other row in the deletion log.")
    if not collected:
        logs.info("deleted_files_report", f"  Read none of their files, because {collected_dir} has no "
                                          f"repository directory.")
    else:
        # {path glob: {repository: file count}}
        globs: dict = defaultdict(Counter)
        for name in sorted(removed & collected):
            # Leave out the planning documents and their anchors, which their heuristics
            # explain, as `other.csv` leaves them out of a published repository.
            documents, anchors = planning.find(collected_dir / name, heuristics.VENDORED_DIRS)
            planned = {rel for rel, _heuristic_row in documents} | anchors
            for rel in unnamed_files(collected_dir / name, rows_of[name]):
                if rel not in planned:
                    globs[path_glob(rel)][repos.dir_name_to_repo(name)] += 1
        registered = heuristics_paths(heuristics.load_config_path_patterns())
        not_configuration = heuristics.not_configuration()
        in_other = {path_glob(p) for p in other_paths}

        def named_in(glob: str) -> str:
            """The file that already names a glob, or an empty string."""
            if glob in not_configuration:
                return "not_configuration.csv"
            if is_explained(glob, registered):
                return "config_path_patterns.csv"
            return "other.csv" if glob in in_other else ""

        listed = sorted(((len(per_repo), sum(per_repo.values()), glob)
                         for glob, per_repo in globs.items()),
                        key=lambda s: (-s[0], -s[1], s[2]))
        files = sum(n for _count, n, _glob in listed)
        unnamed_globs = sum(1 for s in listed if not named_in(s[2]))
        logs.info("deleted_files_report", f"  No row of the deletion log names {files:,} files of "
                                          f"these repositories in {collected_dir.name}/. They lie "
                                          f"under {len(listed):,} path globs ({unnamed_globs:,} "
                                          f"named neither by a heuristic nor in other.csv).")
        if listed and not args.out:
            logs.info("deleted_files_report", f"  {'repos':>7} {'files':>6}  path glob, and the "
                                              f"file that names it")
            for count, n, glob in listed:
                logs.info("deleted_files_report", f"  {count:>7,} {n:>6,}  {glob}"
                                                  + (f"  ({named_in(glob)})" if named_in(glob)
                                                     else ""))
        for _count, _n, glob in listed:
            for repo, n in sorted(globs[glob].items()):
                out_rows.append(("dropped_glob", repo, glob, n, named_in(glob)))

    # The three assertions.
    failed, unchecked = [], []
    logs.info("deleted_files_report", "Checking the three assertions of the deletion log.")

    conflicts = published_and_deleted(log, published)
    pairs = sum(len(paths) for paths in published.values())
    if conflicts:
        failed.append("published and deleted")
        logs.info("deleted_files_report", f"  FAIL  {len(conflicts):,} paths that the deletion log "
                                          f"removed still have rows in the mechanism CSVs or "
                                          f"planning-documents.csv ({pairs:,} published paths "
                                          f"checked).")
        for repo, rel, rows, reason in _shown(conflicts, args.out):
            logs.info("deleted_files_report", f"          {repo} {rel or '(the repository)'} has "
                                              f"{rows:,} published row(s) and the reason "
                                              f"{reason!r}.")
        _more(conflicts, args.out, "published paths that the deletion log removed")
    else:
        logs.info("deleted_files_report", f"  PASS  None of the {pairs:,} paths in the mechanism "
                                          f"CSVs and planning-documents.csv is a path that the "
                                          f"deletion log removed.")
    for repo, rel, rows, reason in conflicts:
        out_rows.append(("published_and_deleted", repo, rel, rows, reason))

    # Every `SKILL.md` that the cleanup deleted, whatever its reason, except the reasons of the
    # two passes before any keep rule. The rule for a skill outside a skills directory is one way
    # to delete a linked skill, and the submodule rule, which overrides the keep rule for a
    # linked directory, is another.
    skill_rows = [(name, rel, reason) for name, rel, reason in log
                  if rel.rsplit("/", 1)[-1] == "SKILL.md" and reason not in BEFORE_ANY_KEEP_RULE]
    linked_rows = []
    if skill_rows and not collected:
        unchecked.append("the deleted SKILL.md rows against the collected links")
        logs.info("deleted_files_report", f"  NOT CHECKED  The {len(skill_rows):,} rows of a "
                                          f"deleted SKILL.md against the collected links, "
                                          f"because {collected_dir} has no repository directory.")
    else:
        links = {name: linked_directories(collected_dir / name)
                 for name in sorted({name for name, _rel, _reason in skill_rows} & collected)}
        linked_rows = [(repos.dir_name_to_repo(name), rel, reason) for name, rel, reason in skill_rows
                       if any(rel.startswith(d + "/") for d in links.get(name, ()))]
        if linked_rows:
            failed.append("a linked SKILL.md deleted")
            logs.info("deleted_files_report", f"  FAIL  The cleanup deleted {len(linked_rows):,} "
                                              f"SKILL.md files in "
                                              f"{len({r for r, _rel, _reason in linked_rows}):,} "
                                              f"repositories from directories that a collected "
                                              f"link names.")
            for repo, rel, reason in _shown(linked_rows, args.out):
                logs.info("deleted_files_report", f"          {repo} {rel} has the reason "
                                                  f"{reason!r}.")
            _more(linked_rows, args.out, "SKILL.md files deleted from a linked directory")
        else:
            logs.info("deleted_files_report", f"  PASS  The cleanup deleted no SKILL.md from a "
                                              f"directory that a collected link names.")
        if len(skill_rows) > len(linked_rows):
            logs.info("deleted_files_report", f"        The cleanup deleted "
                                              f"{len(skill_rows) - len(linked_rows):,} other "
                                              f"SKILL.md file(s) by design, outside the "
                                              f"directories that the collected links name. The "
                                              f"deletion log lists them.")
    linked = {(repo, rel) for repo, rel, _reason in linked_rows}
    for name, rel, reason in skill_rows:
        repo = repos.dir_name_to_repo(name)
        out_rows.append(("deleted_skill_md", repo, rel, 1,
                         f"linked, {reason}" if (repo, rel) in linked else reason))

    if not collected or not validated:
        absent = collected_dir if not collected else validated_dir
        unchecked.append("the repository rows against the trees")
        logs.info("deleted_files_report", f"  NOT CHECKED  The repository rows against the trees, "
                                          f"because {absent} has no repository directory.")
    else:
        trees = compare_trees(removed, collected, validated, validated_list, clone_failures,
                              collected_list)
        explained = []
        if trees["not_copied"]:
            explained.append(f"{len(trees['not_copied']):,} not copied because "
                             f"clone_failed.csv names them")
        if trees["not_listed"]:
            explained.append(f"{len(trees['not_listed']):,} not copied because "
                             f"{collected_dir.name}/repos.csv does not list them")
        if trees["removed_later"]:
            explained.append(f"{len(trees['removed_later']):,} still in data/validated/repos.csv "
                             f"and removed by a later step")
        wrong = {key: trees[key] for key in ("unrecorded", "row_still_validated",
                                             "row_never_collected") if trees[key]}
        summary = (f"The deletion log has {len(removed):,} repository rows, and "
                   f"{len(trees['left']):,} directories of {collected_dir.name}/ are missing from "
                   f"{validated_dir.name}/"
                   + (f" ({', '.join(explained)})" if explained else ""))
        if wrong:
            failed.append("the repository rows against the trees")
            logs.info("deleted_files_report", f"  FAIL  {summary}.")
            says = {"unrecorded": f"is missing from {validated_dir.name}/ and has no row",
                    "row_still_validated": f"has a row and is still in {validated_dir.name}/",
                    "row_never_collected": f"has a row and was never in {collected_dir.name}/"}
            listed = [(key, d) for key in wrong for d in sorted(wrong[key])]
            for key, d in _shown(listed, args.out):
                logs.info("deleted_files_report", f"          {repos.dir_name_to_repo(d)} "
                                                  f"{says[key]}.")
            _more(listed, args.out, "repositories that disagree with the trees")
        else:
            logs.info("deleted_files_report", f"  PASS  {summary}.")
        for key in ("unrecorded", "row_still_validated", "row_never_collected",
                    "not_copied", "not_listed", "removed_later"):
            for d in sorted(trees[key]):
                out_rows.append((f"repository_{key}", repos.dir_name_to_repo(d), "", 1, ""))

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(OUT_COLUMNS)
            writer.writerows(out_rows)
        logs.info("deleted_files_report", f"Wrote {args.out}.")

    if unchecked:
        logs.info("deleted_files_report", f"Did not check {' and '.join(unchecked)}.")
    if failed:
        message = (f"{len(failed)} of the three assertions failed ({', '.join(failed)}). "
                   f"docs/checks/deletions.md describes them.")
        if args.report_only:
            logs.warning("deleted_files_report", message + " Exit code 0 because of --report-only.")
            return 0
        logs.error("deleted_files_report", message)
        return 2
    if unchecked:
        # Exit 1 rather than 0, because the exit code is the verdict, and a run that checked
        # only the first assertion would otherwise exit 0 as though all three held.
        message = (f"{len(unchecked)} of the three assertions could not be checked "
                   f"({', '.join(unchecked)}). The others passed.")
        if args.report_only:
            logs.warning("deleted_files_report", message + " Exit code 0 because of --report-only.")
            return 0
        logs.error("deleted_files_report", message)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
