"""The development subsample: `python -m agentcontext subsample`.

Select the development subsample: the repositories that a run on a laptop needs to exercise
every code path.

The pipeline is developed against a real run rather than against fixtures, and the full
sample is far too large to clone on a laptop. This command selects repositories that together
exercise every behavior of the cloning, cleanup and detection code that the March sample shows
and that are small enough to clone on a laptop. It selects them from tracked files rather than
keeping a list: A list that is not derived from the tracked outputs becomes outdated silently,
which is why `config_path_patterns.csv` exists.

The selection has four phases:

1. One coverage set per repository, from the tracked outputs. Its entries are each heuristic
   that one of the repository's published configuration artifacts matches, each mechanism CSV
   that it appears in, each tool flag set on its `output/repos.csv` row, and each code path
   below that the tracked files show it exercised: a recorded submodule, each reason for which
   the cleanup deleted something, a symlink marker at or inside a mechanism directory that
   resolves or does not, a real symlink inside or outside the tree, a configuration artifact
   that does not decode as UTF-8, a non-English configuration artifact, an AI commit
   per tool, a file spelled in another case than the heuristics, a mechanism directory
   below the root, a `collect_only` file, a branch that is neither `main` nor `master`,
   a recorded commit that GitHub no longer serves, a row with no Git metadata, a binary in a
   skills directory, a repository with many refs, and one with no AI commit.
2. Candidates are the repositories at or below `--max-size-mib` packed, 1,200 by default,
   by `output/repo-sizes.csv`.
3. A greedy set cover: repeatedly the candidate that covers the most entries not yet
   covered, ties broken by smaller `size_kb`, then by `repo_name`. Every choice is logged
   with the entries that it covers, and the end of the list, where each repository is the
   smallest repository that covers a remaining entry, can be read as such.
4. Four entries outside the sample, and therefore outside the matrix, each the smallest row
   of its own tracked list by the March GHS `size`: a clone that fails, from
   `data/collected/clone_failed.csv`; two repositories with an empty file as their only
   configuration artifact, the rows flagged in `data/collected/repos.csv` with no directory
   under `data/collected/`; and one with no configuration at all.


Inputs:
  - ``config_path_patterns.csv``, the mechanism CSVs, ``output/repos.csv``,
    ``output/repo-sizes.csv``, ``output/snapshot-validation.csv``, ``output/all_ai_commits.csv``
    (or the ``.7z`` beside it)
  - ``submodules.csv``, ``deletion.csv``
  - ``data/validated/`` for the markers, symlinks and configuration artifact bytes, and
    ``data/collected/`` with its two lists for the ``collect_only`` files and the four entries
    of `coverage` outside the sample
  - ``../sampling/ghs_filtered.json``, extracted from the archive beside it, for branches
    and the March sizes

Outputs:
  - ``dev_subsample.csv``: ``repo_name``, ``tier`` (``cover`` or ``supplement``),
    ``coverage`` (what it was selected for, ``;``-joined), ``size_kb``

Exit codes follow the other verification commands: 0 when the list was written or, with
``--check``, matches the file on disk, 2 when ``--check`` finds that it would change, and 1
when the selection could not run.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from agentcontext import SAMPLING_DIR, SEVEN_ZIP, STAGE_DIR, heuristics, paths, repos
from agentcontext import cli
from agentcontext.heuristics import heuristic_matches, match_heuristics
from agentcontext.checks import unmatched
from agentcontext.git import clone
from agentcontext.steps import detection
from agentcontext import logs


# -----------------------------------------------------------------------------
# The selection
# -----------------------------------------------------------------------------
csv.field_size_limit(sys.maxsize)

DEFAULT_OUTPUT_DIR = STAGE_DIR / "output"
DEFAULT_VALIDATED_DIR = STAGE_DIR / "data" / "validated"
DEFAULT_COLLECTED_DIR = STAGE_DIR / "data" / "collected"
DEFAULT_SUBMODULES = STAGE_DIR / "submodules.csv"
DEFAULT_DELETION = STAGE_DIR / "deletion.csv"
DEFAULT_GHS = SAMPLING_DIR / "ghs_filtered.json"
DEFAULT_SUBSAMPLE = STAGE_DIR / "dev_subsample.csv"

# GROUNDING: the coverage matrix over the March sample, in which every entry has a repository
# at or below this size, and a much smaller cap leaves entries that no repository covers.
DEFAULT_MAX_SIZE_MIB = 1200
# GROUNDING: the threshold is low enough that repositories under the size cap reach it in the
# March GHS data. A repository with many refs tests whether the cloning step's reading of the
# commit history and the scan read the same commits.
MANY_BRANCHES = 100
# The smallest repositories with an empty file as their only configuration that the supplement
# tier selects.
EMPTIED_REPOSITORIES = 2

COLUMNS = ("repo_name", "tier", "coverage", "size_kb")
TIER_COVER, TIER_SUPPLEMENT = "cover", "supplement"

# The cleanup's deletion reasons, each with the entry of `coverage` that it becomes. Some
# reasons have a detail in parentheses after them, which is dropped.
DELETION_FEATURES = {
    "inside submodule": "deleted:inside_submodule",
    "empty directory after cleanup": "deleted:empty_directory",
    "inside vendored directory": "deleted:vendored",
    "not a spec file": "deleted:not_a_spec_file",
    "file in .github outside the Copilot paths": "deleted:irrelevant_github_file",
    "no detected mechanism, repository directory removed": "deleted:no_mechanism",
    "empty file": "deleted:empty_file",
}

# Repositories with data that stopped a step of a NAS run, under the situation that each showed,
# where no output CSV marks that situation: a file that a merge added, for which `git log --follow`
# names no creation commit.
STEP_FAILURES = {
    "stage_failure:merge_added_path": ("che-incubator/che-code", "altinn/altinn-studio"),
}

# `output/repos.csv` flag columns that become an entry each.
TOOL_FLAGS = ("claude", "gemini", "codex", "copilot", "cursor", "opencode", "no_specific_tool")

# How every published boolean writes true.
TRUE = "TRUE"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

class Unusable(ValueError):
    """An input that the selection cannot use. The message is a sentence that names the input,
    and the command logs it as it is."""


def read_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_snapshot_validation(output_dir: Path) -> list:
    """Read the optional reachability check, which a new collection need not have."""
    path = output_dir / "snapshot-validation.csv"
    return read_csv(path) if path.is_file() else []


def commit_table_rows(output_dir: Path) -> list:
    """The rows of `all_ai_commits.csv`, extracted from the archive beside it if need be.

    The current archive stores the file at its root, and an earlier archive stored it as
    `3_data-collection/output/all_ai_commits.csv`, under the collection stage's directory before
    the data root. The function therefore searches wherever the extraction puts it, as the
    publication stage does. Requesting the bare name from 7zz would match no member, and 7zz
    would exit 0 with nothing on stdout and give every repository `zero_ai_commits`. An archive
    that yields no CSV with rows is refused for that reason.
    """
    CSV = output_dir / "all_ai_commits.csv"
    if CSV.is_file():
        return read_csv(CSV)
    from agentcontext import csv_archive
    archive = csv_archive(CSV)
    if not archive.is_file():
        raise Unusable(f"{CSV} and {archive} are both absent")
    with tempfile.TemporaryDirectory() as work:
        subprocess.run([SEVEN_ZIP, "x", "-bso0", "-bsp0", str(archive), f"-o{work}", "-y"],
                       capture_output=True, check=True)
        extracted = sorted(p for p in Path(work).rglob(CSV.name) if p.is_file())
        if len(extracted) != 1:
            raise Unusable(f"{archive} has {len(extracted)} files named {CSV.name} instead "
                           f"of one")
        with open(extracted[0], newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            header = reader.fieldnames or []
    missing = [c for c in ("repo_name", "ai_tool") if c not in header]
    if missing:
        raise Unusable(f"The {CSV.name} in {archive} has no column(s) {missing}")
    if not rows:
        raise Unusable(f"The {CSV.name} in {archive} has no rows")
    return rows


def ghs_items(path: Path) -> dict:
    """The March GHS rows, keyed by `owner/repo`."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return {item["name"]: item for item in data["items"]}


def heuristic_key(heuristic_row: dict) -> str:
    return f"{heuristic_row['tool']}|{heuristic_row['mechanism']}|{heuristic_row['path']}|{heuristic_row['match_mode']}"


# ---------------------------------------------------------------------------
# Markers and symlinks in the validated tree
# ---------------------------------------------------------------------------

def _looks_like_marker(path: Path) -> str | None:
    """The target that a collected symlink marker names, or None for an ordinary file."""
    try:
        if path.stat().st_size > detection._MARKER_MAX_BYTES:
            return None
        text = paths.marker_target(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if text is None or any(c.isspace() for c in text):
        return None
    if "/" not in text and not text.startswith("."):
        return None
    return text


def _resolves_inside(repo: Path, path: Path, target: str) -> bool:
    resolved = (path.parent / target).resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        return False
    return resolved.exists()


# A target that is absolute on Windows, `D:/code/...`, which is relative here and would
# resolve to a path inside the tree that does not exist.
_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def _points_inside(repo: Path, link: Path) -> bool:
    """Whether a symlink's own target names a path inside the repository.

    By the target as written, not by whether it exists: An absolute target, on this
    operating system or another, points out of the tree whatever it resolves to here.
    """
    target = os.readlink(link)
    if os.path.isabs(target) or _DRIVE_PATH.match(target):
        return False
    try:
        return (link.parent / target).resolve().is_relative_to(repo.resolve())
    except OSError:
        return False


def tree_coverage(repo: Path) -> set:
    """What the validated tree of one repository shows: markers and real symlinks."""
    found = set()
    mechanism_dirs = []
    for rel in detection._MARKER_DIRS:
        for anchored in detection._anchored(repo, rel):
            here = repo / anchored
            if here.is_symlink():
                continue
            if here.is_file():
                resolved = detection._resolve_dir(repo, anchored) is not None
                found.add("marker_at_dir:resolving" if resolved
                          else "marker_at_dir:unresolved")
            elif here.is_dir():
                mechanism_dirs.append(here)
    for directory in mechanism_dirs:
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames.sort()
            for name in sorted(filenames):
                path = Path(dirpath) / name
                if path.is_symlink():
                    continue
                target = _looks_like_marker(path)
                if target is None:
                    continue
                found.add("marker_inside_dir:resolving" if _resolves_inside(repo, path, target)
                          else "marker_inside_dir:unresolved")
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames.sort()
        for name in sorted(dirnames + filenames):
            path = Path(dirpath) / name
            if not path.is_symlink():
                continue
            found.add("symlink:inside_tree" if _points_inside(repo, path)
                      else "symlink:outside_tree")
    return found


# ---------------------------------------------------------------------------
# The coverage matrix
# ---------------------------------------------------------------------------

def coverage_matrix(output_dir: Path, validated_dir: Path, collected_dir: Path,
                   submodules_csv: Path, deletion_csv: Path, ghs: dict) -> tuple:
    """One coverage set per repository, and the heuristics that nothing in the output CSVs
    matched.

    Returns `(coverage_by_repo, unmatched_heuristics, unmatched_rows)`.
    """
    heuristic_rows = [r for r in heuristics.load_config_path_patterns() if r["mechanism"] != "collect_only"]
    collect_only = [r for r in heuristics.load_config_path_patterns() if r["mechanism"] == "collect_only"]
    coverage = defaultdict(set)
    artifacts_by_repo = defaultdict(set)
    matched_heuristics = set()
    unmatched_rows = []

    declared_by_repo = {}

    def declaration_heuristics(repo: str, artifact: str) -> list:
        """The declaring rows that name `artifact`, read from the repository's validated tree:
        a configuration that names a context file, an agent role that names a subagent file, and
        a rules.json that names a rule file."""
        if repo not in declared_by_repo:
            named = defaultdict(list)
            tree = validated_dir / repos.repo_name_to_dir(repo)
            if tree.is_dir():
                for rel, heuristic_row in (paths.declared_context_files(tree)
                                  + paths.declared_subagent_files(tree)
                                  + paths.registered_configuration_files(tree, include_registering_files=True)):
                    named[rel].append(heuristic_row)
            declared_by_repo[repo] = named
        return declared_by_repo[repo].get(artifact, [])

    parts_by_repo = {}

    def plugin_part_heuristics(repo: str, mechanism: str, artifact: str) -> list:
        """The rows that name a part of a plugin that `artifact` is or lies under, read from the
        repository's validated tree: A path does not show whether a manifest is beside it."""
        if repo not in parts_by_repo:
            tree = validated_dir / repos.repo_name_to_dir(repo)
            parts_by_repo[repo] = paths.plugin_components(tree) if tree.is_dir() else []
        matched = []
        for rel, heuristic_row, kind in parts_by_repo[repo]:
            if heuristic_row["mechanism"] != mechanism or not (artifact == rel or artifact.startswith(rel + "/")):
                continue
            if kind in ("declared", "inline"):
                matched.extend(field for field in heuristics.manifest_field_rows(heuristic_rows)
                               if heuristics.default_place_row(field, heuristic_rows) == heuristic_row)
            elif kind != "default" or heuristic_matches(
                    {**heuristic_row, "path": rel}, artifact) or artifact == rel:
                matched.append(heuristic_row)
        return matched

    for mechanism, (filename, artifact_column) in heuristics.MECHANISM_CSVS.items():
        for row in read_csv(output_dir / filename):
            repo, artifact = row["repo_name"], row[artifact_column]
            got = coverage[repo]
            got.add(f"mechanism:{mechanism}")
            artifacts_by_repo[repo].add(artifact)
            matched, depth = match_heuristics(heuristic_rows, mechanism, artifact)
            if mechanism in ("context_file", "rules", "skills", "subagents", "commands", "plugins"):
                # A path does not show what a configuration names. The row that names this file
                # is therefore looked up in the tree and credited beside any row that its name
                # matches: a context file that a configuration names, a rule file that a
                # rules.json names.
                named = [r for r in declaration_heuristics(repo, artifact) if r["mechanism"] == mechanism]
                if named and not matched:
                    matched, depth = named, 0
                elif named:
                    matched = matched + named
            # The same for a part of a plugin: Its row names the plugin's root by the manifest
            # in it rather than by a path.
            parts = plugin_part_heuristics(repo, mechanism, artifact)
            if parts and not matched:
                matched, depth = parts, 0
            elif parts:
                matched = matched + parts
            if not matched:
                unmatched_rows.append((mechanism, repo, artifact))
            else:
                for heuristic_row in matched:
                    got.add(f"heuristic:{heuristic_key(heuristic_row)}")
                    matched_heuristics.add(heuristic_key(heuristic_row))
                # Below the first segment: at a depth of the path, or at a place that a `nested:`
                # row reads below the root.
                if depth or any(heuristics.nested_inner(r["match_mode"]) is not None for r in matched):
                    got.add("nested_mechanism_dir")
                heuristic_row = matched[0]
                spelled = artifact.rsplit("/", 1)[-1]
                wanted = heuristic_row["path"].rsplit("/", 1)[-1]
                if (heuristic_row["match_mode"] in ("file", "basename")
                        and spelled != wanted and spelled.lower() == wanted.lower()):
                    got.add("case_variant")
            if not row.get("created_at"):
                got.add("row_without_git_metadata")
            language = row.get("language", "")
            if language and language not in ("English", "n/a"):
                got.add("non_english_artifact")
            if mechanism == "skills" and (row.get("scripts") == TRUE
                                          or row.get("assets") == TRUE):
                got.add("binary_in_skills_dir")
            on_disk = validated_dir / repos.repo_name_to_dir(repo) / artifact
            if on_disk.is_file() and not on_disk.is_symlink():
                try:
                    on_disk.read_bytes().decode("utf-8")
                except UnicodeDecodeError:
                    got.add("non_utf8_artifact")

    for row in read_csv(output_dir / "repos.csv"):
        got = coverage[row["repo_name"]]
        for flag in TOOL_FLAGS:
            if row.get(flag) == TRUE:
                got.add(f"tool:{flag}")
        if row.get("branch") not in ("main", "master"):
            got.add("branch_not_main_or_master")

    for row in read_csv(submodules_csv):
        coverage[row["repo_name"]].add("submodule_recorded")

    for row in read_csv(deletion_csv):
        # The repository segment wherever it is: An older log writes `repos/<owner§repo>/...`
        # and a current log `data/validated/<owner§repo>/...`, and a fixed index would silently
        # skip every row of one of them.
        name = next((part for part in row["path"].split("/")
                     if repos.is_repo_dir_name(part)), None)
        if name is None:
            continue
        repo = repos.dir_name_to_repo(name)
        reason = row["reason"].split(" (")[0]
        # Every reason, not only the reasons named above: A later run can delete files for
        # reasons that the March log never gave, such as each wrong spelling of a context file.
        coverage[repo].add(DELETION_FEATURES.get(reason)
                           or "deleted:" + re.sub(r"[^a-z0-9.]+", "_", reason.lower()).strip("_"))

    for row in read_snapshot_validation(output_dir):
        if row["outcome"] == "GONE":
            coverage[row["repo_name"]].add("head_sha_unreachable")

    with_commit = set()
    for row in commit_table_rows(output_dir):
        with_commit.add(row["repo_name"])
        for tool in row["ai_tool"].split(","):
            if tool:
                coverage[row["repo_name"]].add(f"ai_commit:{tool}")
        # Each commit heuristic that matched, by its id, `;`-joined in the column: A tool's
        # entry above comes from one of its heuristics and shows nothing about the others.
        for heuristic in (row.get("matched_heuristics") or "").split(";"):
            if heuristic.strip():
                coverage[row["repo_name"]].add(f"commit_heuristic:{heuristic.strip()}")

    # The outcome of reading the commit history, per repository, where the cloning step wrote
    # it: A later collection can record outcomes that the March collection never recorded.
    repos_csv = output_dir / "ai_commits-repos.csv"
    if repos_csv.is_file():
        for row in read_csv(repos_csv):
            if row["outcome"] not in ("history_read", "no_files"):
                coverage[row["repo_name"]].add(f"history:{row['outcome']}")

    # A workflow that runs a documented agent action, from the copies that the cloning step
    # keeps beside the collected tree, which contain such a workflow where the output CSVs of an
    # older run do not.
    workflows = collected_dir.parent / "workflows"
    if workflows.is_dir():
        actions = heuristics.workflow_actions(heuristics.load_config_path_patterns())
        commands = heuristics.workflow_commands(heuristics.load_config_path_patterns())
        for directory in sorted(workflows.iterdir()):
            if not repos.is_repo_dir_name(directory.name):
                continue
            for workflow in sorted(directory.iterdir()):
                try:
                    text = workflow.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for action, tool in actions.items():
                    if paths.uses_action(text, action):
                        coverage[repos.dir_name_to_repo(directory.name)].add(
                            f"agent_workflow:{tool}")
                if workflow.name.lower().endswith(paths.COMPILED_WORKFLOW_SUFFIX):
                    continue
                for tool, invocations in commands.items():
                    if any(paths.runs_command(text, i) for i in invocations):
                        coverage[repos.dir_name_to_repo(directory.name)].add(
                            f"workflow_command:{tool}")

    # A GitHub Agentic Workflow, Markdown that the copies above leave out, read in the validated
    # tree, which contains it.
    engines = heuristics.workflow_engines(heuristics.load_config_path_patterns())
    for workflow in sorted(validated_dir.glob(f"*/{paths.WORKFLOWS_SUBPATH}/*"
                                              f"{paths.AGENTIC_WORKFLOW_SUFFIX}")):
        tool = paths.workflow_tool(workflow, {}, engines)
        if tool:
            coverage[repos.dir_name_to_repo(workflow.parents[2].name)].add(
                f"agentic_workflow:{tool}")

    # A path that `unmatched paths` reports, so that the check has a finding over the
    # subsample: a path glob in other.csv that no heuristic explains.
    reported = output_dir / "unmatched-paths.csv"
    if reported.is_file():
        globs = {row["glob"] for row in read_csv(reported)
                 if row.get("found_in", "other.csv") == "other.csv"}
        filename, column = heuristics.MECHANISM_CSVS["other"]
        for row in read_csv(output_dir / filename):
            if unmatched.path_glob(row[column]) in globs:
                coverage[row["repo_name"]].add("unmatched_path_glob")

    # Only where the collection that was read contains the repository, so that the selection
    # does not require a situation of a sample that cannot contain it.
    for entry, carriers in STEP_FAILURES.items():
        for repo in carriers:
            if repo in coverage:
                coverage[repo].add(entry)
    for row in read_csv(output_dir / "repos.csv"):
        if row["repo_name"] not in with_commit:
            coverage[row["repo_name"]].add("zero_ai_commits")

    for repo, item in ghs.items():
        if repo in coverage and (item.get("branches") or 0) >= MANY_BRANCHES:
            coverage[repo].add("many_refs")

    repo_dir_names, _ = repos.split(validated_dir, expected=("repos.csv",))
    if not repo_dir_names:
        # Seven entries of `coverage` are read from these trees, and lists without the trees
        # silently change the cover. A tree with its directories removed and its lists kept is
        # in exactly this state, and the selection refuses it rather than skipping it silently.
        raise Unusable(
            f"{validated_dir} has its list but no repository directory. Restore the validated "
            f"tree from the complete handover, as docs/development.md describes.")
    for name in repo_dir_names:
        repo = repos.dir_name_to_repo(name)
        coverage[repo].update(tree_coverage(validated_dir / name))

    collected_names, _ = repos.split(collected_dir, expected=("repos.csv",
                                                                   "clone_failed.csv"))
    if not collected_names:
        raise Unusable(
            f"{collected_dir} has its lists but no repository directory. Restore the collected "
            f"tree from the complete handover, as docs/development.md describes.")
    for name in collected_names:
        repo = repos.dir_name_to_repo(name)
        root = collected_dir / name
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for filename in sorted(filenames):
                rel = str((Path(dirpath) / filename).relative_to(root))
                if rel in artifacts_by_repo.get(repo, ()):
                    continue
                if any(heuristic_matches(r, rel) for r in collect_only):
                    coverage[repo].add("collect_only_file")
                    break
            else:
                continue
            break

    unmatched_heuristics = sorted(heuristic_key(r) for r in heuristic_rows if heuristic_key(r) not in matched_heuristics)
    return dict(coverage), unmatched_heuristics, unmatched_rows


# ---------------------------------------------------------------------------
# The cover
# ---------------------------------------------------------------------------

def greedy_cover(coverage: dict, sizes: dict, max_size_kb: int) -> tuple:
    """The cover, as `[(repo_name, coverage, size_kb)]`, and the entries of `coverage` that no
    repository could cover.

    Candidates are the repositories with a size at or under the cap. Each pick is the
    candidate with the most entries not yet covered, ties broken by smaller size and
    then by name, so that two runs over the same inputs pick the same list.
    """
    candidates = {repo: entries for repo, entries in coverage.items()
                  if repo in sizes and sizes[repo] <= max_size_kb}
    uncovered = set().union(*coverage.values()) if coverage else set()
    chosen = []
    while uncovered:
        best = None
        for repo in sorted(candidates):
            gain = len(candidates[repo] & uncovered)
            if gain == 0:
                continue
            key = (-gain, sizes[repo], repo)
            if best is None or key < best[0]:
                best = (key, repo)
        if best is None:
            break
        repo = best[1]
        coverage = sorted(candidates[repo] & uncovered)
        chosen.append((repo, coverage, sizes[repo]))
        uncovered -= candidates[repo]
    return chosen, sorted(uncovered)


def supplements(collected_dir: Path, ghs: dict) -> list:
    """The four entries of `coverage` outside the sample, each the smallest row of its list by
    GHS size."""
    def size_of(repo):
        return (ghs.get(repo, {}).get("size") or 0, repo)

    failed = [r["repo_name"] for r in read_csv(collected_dir / "clone_failed.csv")]
    collected_names, _ = repos.split(collected_dir, expected=("repos.csv",
                                                                   "clone_failed.csv"))
    on_disk = {repos.dir_name_to_repo(n) for n in collected_names}
    emptied, unconfigured = [], []
    for row in read_csv(collected_dir / "repos.csv"):
        repo = row["repo_name"]
        flags = [v for k, v in row.items() if k not in heuristics.META_COLUMNS
                 and k not in ("first_commit_sha",)]
        if any(v == TRUE for v in flags):
            if repo not in on_disk and repo not in failed:
                emptied.append(repo)
        elif repo not in failed:
            unconfigured.append(repo)
    picks = []
    if failed:
        picks.append((min(failed, key=size_of), "clone_failure"))
    for repo in sorted(emptied, key=size_of)[:EMPTIED_REPOSITORIES]:
        picks.append((repo, "emptied_repository"))
    if unconfigured:
        picks.append((min(unconfigured, key=size_of), "no_configuration"))
    return [(repo, [entry], ghs.get(repo, {}).get("size") or 0) for repo, entry in picks]




def render(rows: list) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), lineterminator="\n")
    writer.writeheader()
    for repo, tier, coverage, size_kb in rows:
        writer.writerow({"repo_name": repo, "tier": tier, "coverage": ";".join(coverage),
                         "size_kb": size_kb})
    return buffer.getvalue()


def select(output_dir: Path, validated_dir: Path, collected_dir: Path, submodules_csv: Path,
           deletion_csv: Path, ghs_path: Path, max_size_mib: int,
           log=print) -> tuple:
    """Select the development subsample. Returns `(rows, report)`, and `rows` are
    `(repo, tier, coverage, size_kb)`."""
    ghs = ghs_items(ghs_path)
    # A row without a size is a repository that GitHub no longer serves. It cannot be cloned
    # and is no candidate.
    measured = read_csv(output_dir / "repo-sizes.csv")
    sizes = {r["repo_name"]: int(r["size_kb"]) for r in measured if r["size_kb"]}
    # A repository that GitHub did not serve: measured without a size, or refused to the
    # collection. Its run is a clone failure, which the supplement covers, and a cover entry
    # that it covered would go untested.
    unserved = {r["repo_name"] for r in measured if not r["size_kb"]}
    outcomes = output_dir / "ai_commits-repos.csv"
    if outcomes.is_file():
        unserved |= {r["repo_name"] for r in read_csv(outcomes)
                     if r["outcome"] in clone.PERMANENT_OUTCOMES}
    for repo in unserved:
        sizes.pop(repo, None)
    coverage, unmatched_heuristics, unmatched_rows = coverage_matrix(
        output_dir, validated_dir, collected_dir, submodules_csv, deletion_csv, ghs)
    # `repo-sizes.csv` contains the sizes of the repositories in the March sample. A repository
    # that a later collection added has GitHub's own size in the GHS row, its disk size in KiB,
    # which is close to the size of a mirror. Without it, such a repository would never be a
    # candidate. The GHS size applies only to a repository that was not measured and that GitHub
    # served: GHS's size of an unserved repository would make it a candidate after all.
    for repo in coverage:
        if repo not in sizes and repo not in unserved:
            size = (ghs.get(repo) or {}).get("size")
            if isinstance(size, (int, float)) and size > 0:
                sizes[repo] = int(size)
    # A repository that the snapshot validation found gone was measured while GitHub still served
    # it, and its size no longer applies: No clone reaches it, no mirror can contain it, and in a
    # run it is a clone failure rather than whatever the cover selected it for.
    gone = sorted(row["repo_name"] for row in read_snapshot_validation(output_dir)
                  if row["outcome"] == "REPO_GONE" and row["repo_name"] in coverage)
    for repo in gone:
        sizes.pop(repo, None)
    universe = set().union(*coverage.values()) if coverage else set()
    cover, uncoverable = greedy_cover(coverage, sizes, max_size_mib * 1024)
    rows = [(repo, TIER_COVER, list(entries), size) for repo, entries, size in cover]
    # A supplement that the cover selected already has its entry in the cover's row: the
    # smallest failed clone can be the repository that the cover selected for its clone
    # outcome, and the list would name it twice.
    for repo, entries, size in supplements(collected_dir, ghs):
        picked = next((row for row in rows if row[0] == repo), None)
        if picked is None:
            rows.append((repo, TIER_SUPPLEMENT, entries, size))
        else:
            picked[2].extend(entry for entry in entries if entry not in picked[2])
    report = {
        "repositories_with_coverage": len(coverage),
        "coverage": len(universe),
        "heuristics_matched": sum(1 for f in universe if f.startswith("heuristic:")),
        "unmatched_heuristics": unmatched_heuristics,
        "unmatched_rows": unmatched_rows,
        "uncoverable": uncoverable,
        "gone": gone,
        "cover_size_kb": sum(size for _, _, size in cover),
    }
    log(f"{report['repositories_with_coverage']:,} repositories cover {report['coverage']:,} "
        f"entries of `coverage`.")
    log(f"{report['heuristics_matched']:,} path patterns match a published configuration "
        f"artifact, and {len(unmatched_heuristics):,} match none.")
    for heuristic_row in unmatched_heuristics:
        log(f"  No configuration artifact matches {heuristic_row}.")
    if unmatched_rows:
        log(f"{len(unmatched_rows):,} published rows match no heuristic of their mechanism.")
        for mechanism, repo, artifact in unmatched_rows[:10]:
            log(f"  No heuristic of {mechanism} matches {artifact} of {repo}.")
    for number, (repo, coverage, size) in enumerate(cover, start=1):
        log(f"{number:3d}. {repo} ({size / 1024:,.1f} MiB) covers {len(coverage):,} entries "
            f"({', '.join(coverage)}).")
    for repo in gone:
        log(f"  {repo} is not a candidate, because GitHub no longer serves it.")
    for entry in uncoverable:
        log(f"  No repository of {max_size_mib:,} MiB or less covers {entry}.")
    log(f"The greedy set cover selected {len(cover):,} repositories with a packed size of "
        f"{report['cover_size_kb'] / 1024 ** 2:,.3f} GiB. {len(rows) - len(cover):,} repositories "
        f"from outside the sample supplement it.")
    return rows, report


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description="Select the subsample from the tracked outputs and "
                                            "write dev_subsample.csv, or compare the list on disk "
                                            "with the selection (--check).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--validated-dir", type=Path, default=DEFAULT_VALIDATED_DIR)
    parser.add_argument("--collected-dir", type=Path, default=DEFAULT_COLLECTED_DIR)
    parser.add_argument("--submodules", type=Path, default=DEFAULT_SUBMODULES)
    parser.add_argument("--deletion", type=Path, default=DEFAULT_DELETION)
    parser.add_argument("--ghs", type=Path, default=DEFAULT_GHS,
                        help="The GHS rows of the sampling frame (default: %(default)s). "
                             "Extract data/sampling/ghs_filtered.json.7z first.")
    parser.add_argument("--subsample", type=Path, default=DEFAULT_SUBSAMPLE,
                        help="The list to write, or the list to compare with under --check "
                             "(default: %(default)s)")
    parser.add_argument("--max-size-mib", type=int, default=DEFAULT_MAX_SIZE_MIB,
                        help="The largest packed size of a candidate, in MiB "
                             "(default: %(default)s)")
    parser.add_argument("--check", action="store_true",
                        help="Compare the list on disk with the selection, and write nothing")
    args = parser.parse_args(argv)

    if not args.ghs.is_file():
        logs.error("subsample", f"{args.ghs} is missing. Extract it with `7zz x "
                                f"{args.ghs.with_suffix('.json.7z').name}` in "
                                f"{args.ghs.parent}.")
        return 1
    try:
        rows, _ = select(args.output_dir, args.validated_dir, args.collected_dir,
                         args.submodules, args.deletion, args.ghs, args.max_size_mib, log=logs.logger("subsample").info)
    except Unusable as e:
        logs.error("subsample", f"{str(e).rstrip('.')}.")
        return 1
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as e:
        # Python's text after the exception's type, because the text of a KeyError is the
        # missing key alone.
        logs.error("subsample", f"Could not select the subsample ({type(e).__name__}, {e}).")
        return 1
    if not rows:
        logs.error("subsample", "The selection produced no repositories. Its inputs are missing "
                                "or empty.")
        return 1
    expected = render(rows)
    if args.check:
        if not args.subsample.is_file():
            logs.error("subsample", f"{args.subsample} does not exist. Write it by running "
                                    f"without --check.")
            return 1
        current = args.subsample.read_bytes().decode("utf-8")
        if current == expected:
            logs.info("subsample", f"{args.subsample.name} matches the selection.")
            return 0
        on_disk = {line.split(",")[0] for line in current.splitlines()[1:]}
        selected = {repo for repo, *_ in rows}
        added, removed = sorted(selected - on_disk), sorted(on_disk - selected)
        logs.info("subsample", f"{args.subsample.name} differs from the selection in its "
                               f"repositories or in the coverage or the size of a row. The "
                               f"selection adds {', '.join(added) or 'no repository'} and "
                               f"removes {', '.join(removed) or 'no repository'}.")
        return 2
    args.subsample.write_text(expected, encoding="utf-8")
    logs.info("subsample", f"Wrote {len(rows):,} rows to {args.subsample}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
