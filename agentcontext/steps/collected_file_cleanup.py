#!/usr/bin/env python3
"""
Validate and clean up the collected configuration artifacts.

Reads the collected tree that agentcontext/steps/repository_cloning.py writes
(data/collected/, with its list at data/collected/repos.csv), copies the
repositories into the validated tree (data/validated/, with its list at
data/validated/repos.csv), and validates and cleans the copy in place. Running the
step again recovers from a failure at this step or a later one without cloning again,
because the step never modifies the collected tree.

Enforces the rules of docs/steps/collected_file_cleanup.md and docs/heuristics.md:
- Filenames are matched case-sensitively
- Vendored and build-output directories, such as node_modules, target and
  __pycache__, are excluded at any depth, even inside a directory that is kept
- The places where the tools keep sessions and other local state, such as
  .claude/projects/-<path>/ and .codex/sessions/, are excluded even where another rule
  would keep them
- Only spec directories and standalone spec files are kept
- A .github directory is kept only when it contains a Copilot trigger or a context file,
  and in a .github below the root, the places that Copilot CLI reads in the directory
  that it works in are kept
- Manifest directories, recognized plugin components, the allowed metadata siblings
  and standalone spec files are kept
- Planning documents and their anchors are kept, and they do not keep a repository

The step then computes the boolean columns from the remaining files and removes the
repositories in which the detectors find no named mechanism.
"""

from agentcontext import STAGE_DIR
from agentcontext import cli
import csv
import json
import os
import posixpath
import shutil
import sys
from pathlib import Path


from agentcontext.heuristics import (directory_paths, load_config_path_patterns, one_path,
                              paths_for, paths_under)
from agentcontext import repos
from agentcontext import heuristics
from agentcontext import paths
from agentcontext import imports, planning

# `main` keeps a repository when the detectors find a named mechanism in it, the test that the
# detection step applies when it builds the mechanism CSVs. The import keeps a single
# definition of a configured repository, where two copies could differ.
from agentcontext.steps.detection import (LINKED_RULES_DIRS, _nested_anchors,
                                         has_detected_mechanism)
from agentcontext import logs

_PATTERNS = load_config_path_patterns()

COLLECTED_DIR = "data/collected"
COLLECTED_CSV = "data/collected/repos.csv"
VALIDATED_DIR = "data/validated"
EXCLUDED_DIR = "data/excluded"
VALIDATED_CSV = "data/validated/repos.csv"
DELETION_CSV = "deletion.csv"

# Each list is at the root of the tree that it lists, and `repos.split` takes it as an
# expected entry. Otherwise its warning about stray entries, which reports accidents, would
# name the tree's own list.
COLLECTED_TREE_FILES = ("repos.csv", "clone_failed.csv")
VALIDATED_TREE_FILES = ("repos.csv",)
# The repository directories without a row of the list that the warning names, after it counts
# them all.
UNLISTED_NAMED = 10

# The directories that Synology DSM writes into the directories that its File Station and
# indexing services visit, as `paths.DISCARDED_DIRECTORIES` names them. On the NAS, these
# services visit the collected tree. No coding agent reads such a directory. The copy of the
# collected tree leaves each out (`paths.copy_tree`), and the step deletes each one that DSM
# writes into the validated tree, with the reason `Synology DSM directory`. The runner leaves
# them out of the manifest of the collected tree (`runner.DISCARDED_DIRECTORIES`), so that a
# visit does not make the cloning step count as unfinished.
SYNOLOGY_DIRECTORIES = paths.DISCARDED_DIRECTORIES

# The columns that this step copies unchanged from the collected list to the validated list.
# It can only copy them, because it clones nothing and reads no commit history.
METADATA_COLUMNS = heuristics.META_COLUMNS

# The name of the head commit date column in an older collected list, a name that suggests
# the scan time. The step refuses a list with this column rather than dropping the column
# without a warning (see the header check in `main`).
LEGACY_TIMESTAMP_COLUMN = "timestamp"

# The spec directories, kept with all their contents. The paths come from
# config_path_patterns.csv, which the cloning and detection steps also read. A mechanism added
# there is kept here without a second list to update.
SPEC_DIRS = set(heuristics.spec_dir_paths(_PATTERNS))

# Vendored and build-output directories, excluded even where another rule would keep them. They are
# loaded from heuristics/vendored_dirs.txt, the list that agentcontext/steps/repository_cloning.py
# reads too. The file lists only unambiguous names. Names such as `build`, `dist` and `packages` are
# left out because projects also use them for their own paths, such as the `packages/` of a
# JavaScript monorepo, a project's own `.claude/commands/build/` or a `dist/` of hand-written files.
# The section "The vendored list" of docs/heuristics.md names every name that is left out, with its
# reason.
VENDORED_DIRS = heuristics.VENDORED_DIRS

# The files directly beside a plugin manifest directory that are kept as plugin metadata, in
# addition to the standalone spec files (`is_claude_plugin_parent_kept_sibling`). Every other
# sibling is dropped.
CLAUDE_PLUGIN_PARENT_KEEP_EXTRA = set(paths_for(_PATTERNS, "collect_only", "plugin_sibling"))

# The same rules apply to all four manifest directories, because Cursor and Codex copied
# Claude's plugin layout and Copilot CLI keeps its manifest in .plugin.
PLUGIN_DIRS = set(paths_for(_PATTERNS, "collect_only", "copy_dir"))


# Each Copilot path inside .github, and whether it names a directory. A directory entry
# keeps everything below it. A file entry, such as .github/workflows/copilot-setup-steps.yml,
# keeps only that file and none of the rest of a project's CI.
GITHUB_KEEP = paths_under(_PATTERNS, ".github")
COPILOT_TRIGGERS = {rel for rel, _ in GITHUB_KEEP}

# The GitHub Actions that a `workflow_uses:` row names. `paths_under` leaves those rows out of
# GITHUB_KEEP, because their path, `.github/workflows`, would keep every workflow of a
# repository. This step keeps a workflow by the action that it runs instead.
WORKFLOW_ACTIONS = heuristics.workflow_actions(_PATTERNS)
# The engines that a `workflow_engine:` row names, for a GitHub Agentic Workflow, which is a
# Markdown file.
WORKFLOW_ENGINES = heuristics.workflow_engines(_PATTERNS)
WORKFLOW_COMMANDS = heuristics.workflow_commands(_PATTERNS)

# Standalone spec filenames, matched case-sensitively at any depth: the path of every basename
# row, ignore files included, whatever its mechanism. A file that a heuristic moves from one
# mechanism to another stays on this list and is not deleted here after the collector copied
# it. The root_file rows of the mcp, context_file and other mechanisms are in ROOT_ONLY_FILES
# instead, and a nested .mcp.json is not kept.
SPEC_FILES = {r["path"] for r in _PATTERNS if r["match_mode"] == "basename"}

# Spec files that are valid only at the repository root, among them the settings files that a
# root_file row names by a bare filename, such as OpenCode's opencode.json.
ROOT_ONLY_FILES = ({one_path(_PATTERNS, "mcp", "root_file")}
                   | set(paths_for(_PATTERNS, "context_file", "root_file"))
                   | set(paths_for(_PATTERNS, "other", "root_file"))
                   | {path for path in paths_for(_PATTERNS, "settings", "root_file")
                      if "/" not in path})

# OpenCode's files at the repository root, outside its .opencode directory.
OPENCODE_ROOT_FILES = paths.OPENCODE_ROOT_FILES

# Exact repository-relative paths, each kept only at that path: the Copilot files that the
# collector copies from outside .github. ROOT_ONLY_FILES matches a basename at depth 1 and
# cannot express them. .vscode/mcp.json is at depth 2 and shares its basename with
# .cursor/mcp.json, and Copilot CLI reads marketplace.json only at the repository root.
ROOT_ONLY_PATHS = set(paths.COPILOT_ROOT_FILES)

# A context file spelled in another case, kept at the repository root and nowhere else:
# {lower-case name: tool}. The tools find `agents.md` for `AGENTS.md` on a case-insensitive file
# system, and at the root such a file is an agent's instructions far more often than below it
# (docs/steps/collected_file_cleanup.md gives the counts).
ROOT_ANY_CASE = {r["path"].lower(): r["tool"] for r in _PATTERNS
                 if r["match_mode"] == "root_any_case"}

# Context files that keep .github, even without a Copilot trigger.
GITHUB_CONTEXT_FILES = set(paths_for(_PATTERNS, "context_file", "basename"))

# A SKILL.md is valid only inside spec directories or .github. The rows with a plugin
# placeholder are left out, because a placeholder names no directory of the tree and the step
# keeps a plugin's skills as parts of the plugin.
SKILL_VALID_PARENTS = {r["path"].split("/")[0] for r in _PATTERNS
                       if r["mechanism"] == "skills" and r["match_mode"] in ("skill_dir", "skill_dir_flat")
                       and not heuristics.plugin_placeholder(r["path"])}

# The two filenames that the Agent Plugins standard fixes at a plugin root, each with the
# content test that keeps it. `agentcontext/paths.py` copies only the documents that pass the
# test, and every document in a tree that the current cloning step produced passes it. This
# step applies the test again because it also runs over trees collected under older rules.
AGENT_PLUGINS_NAMES = heuristics.agent_plugins_names(_PATTERNS)


def read_text_or_empty(file_path):
    """The file's text, or `""` where it cannot be read, so that a content test fails."""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def repo_name_to_dir(repo_name):
    """Convert repo_name (owner/repo) to directory name (owner§repo)."""
    return repos.repo_name_to_dir(repo_name)


def dir_name_to_repo(dir_name):
    """Convert directory name (owner§repo) to repo_name (owner/repo)."""
    return repos.dir_name_to_repo(dir_name)


def has_copilot_content(github_dir):
    """Whether a .github directory contains a Copilot trigger."""
    # os.path.exists, not isdir, because the cloning step writes a symlinked .github/skills as
    # a one-line file that contains its target. Requiring a directory would drop that file
    # with the rest of .github. The names are compared case-insensitively, as in
    # is_relevant_github_file. A case-sensitive test would let COPILOT-INSTRUCTIONS.md keep
    # .github on macOS and not on Linux.
    try:
        entries = {e.lower(): e for e in os.listdir(github_dir)}
    except OSError:
        return False
    for rel, _is_dir in GITHUB_KEEP:
        head, _, tail = rel.partition("/")
        real = entries.get(head.lower())
        if real is None:
            continue
        if not tail and real.lower() == GITHUB_HOOKS and not holds_hook_file(
                os.path.join(github_dir, real)):
            continue
        if not tail or os.path.exists(os.path.join(github_dir, real, tail)):
            return True
    return False


# Copilot reads the JSON files of `.github/hooks` at any depth, as the `tree_ext:.json` row of
# `config_path_patterns.csv` reads them. `scan_and_clean_repo` deletes every other file
# directly in the folder. The git hook scripts that projects also keep there are no Copilot
# trigger.
GITHUB_HOOKS = "hooks"


def holds_hook_file(hooks_dir: str) -> bool:
    """Whether `.github/hooks` holds a JSON file at any depth, or is a link or a file that the
    cloning step wrote for a link. The rules for links decide about the target of such a
    link."""
    if os.path.islink(hooks_dir) or not os.path.isdir(hooks_dir):
        return True
    return any(name.endswith(".json") for _root, _dirs, files in os.walk(hooks_dir)
               for name in files)


def has_github_context_files(github_dir):
    """Whether a .github directory contains a context file at any depth."""
    for root, dirs, files in os.walk(github_dir):
        for f in files:
            if f in GITHUB_CONTEXT_FILES:
                return True
    return False


def should_keep_github(github_dir):
    """Whether to keep a .github directory, which needs a Copilot trigger or a context file."""
    return has_copilot_content(github_dir) or has_github_context_files(github_dir)


def find_claude_plugin_dirs(repo_path):
    """Return the parent directory of every plugin manifest directory in the repository."""
    plugin_parents = set()
    for root, dirs, files in os.walk(repo_path):
        if PLUGIN_DIRS & set(dirs):
            # The parent directory of a plugin manifest directory is valid
            plugin_parents.add(root)
    return plugin_parents


def get_path_parts(path, repo_path):
    """Get the path parts relative to repo_path."""
    rel = os.path.relpath(path, repo_path)
    return rel.split(os.sep)


def is_agent_workflow(file_path, repo_path) -> bool:
    """Whether the file is a `.github/workflows/<name>.yml` that runs a documented agent action
    or, in a shell step, a tool's documented headless invocation, or a
    `.github/workflows/<name>.md` that is a GitHub Agentic Workflow on one of the engines.

    The collector copies such a workflow whatever its name, and the detector reports it under
    the tool that it runs. If this step deleted a renamed agent workflow as an irrelevant file
    in `.github`, the workflow would reach no CSV, and a repository without other
    configuration would not count as configured.
    """
    parts = get_path_parts(file_path, repo_path)
    if len(parts) != 3 or parts[:2] != [".github", "workflows"]:
        return False
    return paths.workflow_tool(Path(file_path), WORKFLOW_ACTIONS, WORKFLOW_ENGINES,
                               WORKFLOW_COMMANDS) is not None


def is_inside_spec_dir(file_path, repo_path):
    """Whether a file is inside a spec directory at any depth, or is a file with the name of
    one, the marker of a tool directory that was a link. A directory with such a name that is
    no spec directory (`paths.is_spec_directory`), such as a Python package named `_agent`,
    keeps none of its files."""
    parts = get_path_parts(file_path, repo_path)
    return parts[-1] in SPEC_DIRS or any(
        part in SPEC_DIRS and paths.is_spec_directory(Path(repo_path, *parts[:i + 1]))
        for i, part in enumerate(parts[:-1]))


def is_inside_github_dir(file_path, repo_path):
    """Check if a file is inside a .github directory at any depth."""
    parts = get_path_parts(file_path, repo_path)
    return ".github" in parts


def is_inside_claude_plugin_dir(file_path, repo_path):
    """Check if a file is inside a plugin manifest directory at any depth."""
    parts = get_path_parts(file_path, repo_path)
    return bool(PLUGIN_DIRS & set(parts))


def is_claude_plugin_parent_kept_sibling(file_path, plugin_parents):
    """Whether the file is directly beside a plugin manifest directory and is an allowed
    sibling, such as a README, a LICENSE, package.json or a standalone spec file.

    The check keeps no deeper descendant of the parent, such as a file of the vendored
    submodules that utooland/utoo nests below a plugin's parent.
    """
    basename = os.path.basename(file_path)
    dirname = os.path.dirname(file_path)
    if dirname not in plugin_parents:
        return False
    return basename in CLAUDE_PLUGIN_PARENT_KEEP_EXTRA or basename in SPEC_FILES


def vendored_dir_of(file_path, repo_path, marker_targets=()):
    """The vendored or build-output directory that contains the path, relative to repo_path,
    or None. `paths.vendored_part` decides, and it does not count a skill folder with a
    vendored name (docs/heuristics.md).

    Inside an entry among `marker_targets`, only the part of the path below the deepest such
    entry counts. A link at a mechanism path makes the tool read what it leads to, wherever
    that is, and the collector copies it, such as the skills that
    `open-metadata/openmetadata` links one at a time from `.claude/skills/` into
    `skills/vendor/`. A vendored directory inside the target, such as a `node_modules/`, still
    counts."""
    rel = os.path.relpath(file_path, repo_path).replace(os.sep, "/")
    containing = [target.replace(os.sep, "/") for target in marker_targets
                  if rel == target.replace(os.sep, "/")
                  or rel.startswith(target.replace(os.sep, "/") + "/")]
    if containing:
        target = max(containing, key=len)
        return paths.vendored_part(Path(repo_path) / target, rel[len(target) + 1:],
                                   VENDORED_DIRS) if rel != target else None
    return paths.vendored_part(Path(repo_path), rel, VENDORED_DIRS)


def is_inside_vendored_dir(file_path, repo_path, marker_targets=()):
    """Whether the path is in a vendored or build-output directory (docs/heuristics.md), judged
    below the entry of `marker_targets` that contains it (`vendored_dir_of`)."""
    return vendored_dir_of(file_path, repo_path, marker_targets) is not None


def local_state_of(file_path, repo_path, linked=()):
    """The place of a tool's local state that the path is at or below, relative to repo_path,
    or None (`paths.local_state`, heuristics/local_state_paths.csv).

    `linked` holds (name, directory) for each tool directory that was a link
    (`linked_tool_directory_targets`). A path below such a directory is also named from the tool
    directory, because the tool writes its local state through the link: with `.claude ->
    .agents`, `.agents/shell-snapshots/` is Claude Code's `.claude/shell-snapshots/`."""
    rel = "/".join(get_path_parts(file_path, repo_path))
    place = paths.local_state(rel)
    for name, directory in linked:
        if place is None and rel.startswith(directory + "/"):
            place = paths.local_state(f"{name}/{rel[len(directory) + 1:]}")
    return place


def is_skill_md_in_valid_location(file_path, repo_path):
    """Check if SKILL.md is inside a valid spec directory."""
    parts = get_path_parts(file_path, repo_path)
    return any(p in SKILL_VALID_PARENTS for p in parts)


def is_relevant_github_file(file_path, repo_path):
    """Whether a file inside .github is one that the detection step inspects.

    Only the Copilot paths of GITHUB_KEEP, matched as its comment states, and the context
    files are relevant.

    The names are compared case-insensitively, as has_copilot_content compares them. If the
    two functions disagreed, a repository could be kept for containing .github/Instructions
    and then have every file inside it deleted.
    """
    parts = get_path_parts(file_path, repo_path)
    if len(parts) < 2:
        return False
    # GITHUB_KEEP applies only to a `.github` at the repository root, the only one that GitHub
    # reads. `is_inside_github_dir` matches the name at any depth, and a slice from the second
    # segment of a nested path would compare an unrelated part of it with the list, such as
    # `instructions/.github/z.md` for `a/instructions/.github/z.md`, which matches the
    # `instructions` entry. `is_valid_file` calls this function only in a repository with a
    # kept root `.github`, and only there does a context file below a nested `.github` reach
    # the basename check at the end.
    if parts[0] == ".github":
        rel = "/".join(parts[1:])
        lowered = rel.lower()
        for keep, is_dir in GITHUB_KEEP:
            keep_lower = keep.lower()
            if is_dir:
                if lowered == keep_lower or lowered.startswith(keep_lower + "/"):
                    return True
            # Repositories vary the casing of copilot-instructions.md.
            elif lowered == keep_lower:
                return True

    if os.path.basename(file_path) in GITHUB_CONTEXT_FILES:
        return True
    return False


# The mechanism directories that a symlink marker can replace. The detection step derives its
# list the same way, and the two lists cannot differ.
MECHANISM_DIRS = directory_paths(_PATTERNS)


# The skills directories, in which each entry is a skill, each once, also where the heuristics
# of several tools read it. A repository can link one skill at a time or the whole directory,
# and the collector copies the target of either.
SKILL_DIRS = tuple(dict.fromkeys(r["path"] for r in _PATTERNS
                                 if r["match_mode"] in ("skill_dir", "skill_dir_flat")
                                 and "*" not in r["path"]
                                 and not heuristics.plugin_placeholder(r["path"])))
# The Claude Code commands directories, in which every `.md` at any depth is a skill, and its
# rules directories, through which it reads the rules of a linked folder
# (`detection.LINKED_RULES_DIRS`). A repository can link a folder of commands or rules into one,
# and the collector copies its target.
LINKED_MARKDOWN_DIRS = tuple(r["path"] for r in _PATTERNS
                             if ((r["mechanism"] == "skills" and r["match_mode"] == "tree_ext:.md")
                                 or r["path"] in LINKED_RULES_DIRS)
                             and "*" not in r["path"]
                             and not heuristics.plugin_placeholder(r["path"]))


def _marker_target_dir(repo_path, marker):
    """The directory inside the repository that a collected symlink marker names, or None."""
    try:
        with open(marker, encoding="utf-8") as f:
            text = paths.marker_target(f.read(4096), os.path.dirname(marker))
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError, and has to be caught too. The
        # collector copies `.cursor` and `.claude` with all their contents, and a regular
        # binary file at a mechanism path would otherwise stop the whole step with an
        # exception.
        return None
    if text is None:
        return None
    target = os.path.normpath(os.path.join(os.path.dirname(marker), text))
    try:
        inside = os.path.commonpath([os.path.abspath(target),
                                     os.path.abspath(repo_path)]) == os.path.abspath(repo_path)
    except ValueError:
        return None
    return os.path.relpath(target, repo_path) if inside and os.path.isdir(target) else None


def _link_target_dir(repo_path, link):
    """The directory inside the repository, other than its root, that a symlink to a directory
    leads to, or None."""
    root = os.path.realpath(repo_path)
    target = os.path.realpath(link)
    if target == root or os.path.commonpath([target, root]) != root or not os.path.isdir(target):
        return None
    return os.path.relpath(target, root)


def _linked_tool_directories(repo_path, anchors) -> dict:
    """{name: [directory, ...]}: for each tool directory (`paths.TOOL_DIRECTORIES`) that was a
    link, the repository-relative directory that its marker names, at the root and below each
    directory of `anchors` (`detection._nested_anchors`), the places where the detection step
    reads the tool directory through the marker. A marker that names the repository root or a
    directory that contains the marker names no directory here, because the collector copies
    no such target."""
    found = {}
    for name in paths.TOOL_DIRECTORIES:
        for rel in [name, *(f"{parent}/{name}" for parent in anchors.get(name, ()))]:
            marker = os.path.join(repo_path, rel)
            if not os.path.isfile(marker) or os.path.islink(marker):
                continue
            target = _marker_target_dir(repo_path, marker)
            if target is None:
                continue
            target = target.replace(os.sep, "/")
            parent = posixpath.dirname(rel)
            if target == "." or parent == target or parent.startswith(target + "/"):
                continue
            found.setdefault(name, []).append(target)
    return found


def linked_tool_directory_targets(repo_path) -> list:
    """(name, directory) for each tool directory that was a link in the repository's tree, at
    the places where the detection step reads it (`_linked_tool_directories`). Computed without
    the cache of the detection step, as `find_marker_targets` computes the same places."""
    linked = _linked_tool_directories(repo_path, _nested_anchors.__wrapped__(repo_path))
    return [(name, target) for name, targets in linked.items() for target in targets]


def in_github(rel) -> bool:
    """Whether the repository-relative path is a `.github` directory or below one."""
    return ".github" in rel.split("/")


# What a tool reads in its tool directory, by the directory's name: the rows of its mechanisms
# with a path in the directory, without the rows that classify nothing and the workflow rows,
# which read `.github/workflows` by the action, engine or command that a file runs.
_TOOL_DIRECTORY_ROWS = {name: [r for r in _PATTERNS if r["path"].startswith(name + "/")
                               and r["mechanism"] not in ("other", "collect_only")
                               and not r["match_mode"].startswith(heuristics.WORKFLOW_MODES)]
                        for name in paths.TOOL_DIRECTORIES}


def read_through_link(name, rest) -> bool:
    """Whether the tool of the tool directory `name` reads the path `rest` below it as a
    configuration artifact, by a row of its mechanisms (`heuristics.heuristic_matches`)."""
    return any(heuristics.heuristic_matches(row, f"{name}/{rest}")
               for row in _TOOL_DIRECTORY_ROWS.get(name, ()))


# The bounds on reading a marker, a one-line file that contains its target, and on following a
# chain of markers. The detection step follows a chain under the same bounds.
MARKER_MAX_BYTES = 4096
MARKER_HOPS = 8


def _marker_target_files(repo_path, marker):
    """The repository-relative paths of the files inside the repository that the collected
    symlink marker at `marker` names. The function follows a chain of markers as the detection
    step does (`detection._followed_file`) and stops at a file in a `.git` directory
    (`paths.in_git_directory`). Empty where `marker` is no marker of a link to such a file."""
    root = os.path.realpath(repo_path)
    found, path = [], marker
    for _ in range(MARKER_HOPS):
        try:
            if os.path.getsize(path) > MARKER_MAX_BYTES:
                break
            with open(path, encoding="utf-8") as f:
                text = paths.marker_target(f.read(), os.path.dirname(path))
        except (OSError, UnicodeDecodeError):
            break
        if text is None:
            break
        target = os.path.realpath(os.path.join(os.path.dirname(path), text))
        if not Path(target).is_relative_to(root) or not os.path.isfile(target):
            break
        rel = os.path.relpath(target, root).replace(os.sep, "/")
        if rel in found or paths.in_git_directory(rel):
            break
        found.append(rel)
        path = target
    return found


def _link_chain(repo_path, link):
    """The repository-relative entries that the link at `link` leads through: each link of a
    chain and the entry at its end, at most `MARKER_HOPS`. The chain stops at an entry outside
    the repository, at the root itself or in a `.git` directory (`paths.in_git_directory`)."""
    root = os.path.realpath(repo_path)
    found, path = [], link
    for _ in range(MARKER_HOPS):
        try:
            target = os.path.join(os.path.dirname(path), os.readlink(path))
        except OSError:
            break
        head, name = os.path.split(target)
        # The last name stays unresolved, so that a link between two others is an entry of
        # the chain rather than the entry at its end.
        hop = (os.path.realpath(target) if name in ("", ".", "..")
               else os.path.join(os.path.realpath(head), name))
        if hop == root or not Path(hop).is_relative_to(root) or not os.path.lexists(hop):
            break
        rel = os.path.relpath(hop, root).replace(os.sep, "/")
        if rel in found or paths.in_git_directory(rel):
            break
        found.append(rel)
        if not os.path.islink(hop):
            break
        path = hop
    return found


def _links(repo_path):
    """Every symbolic link in the repository, without following one."""
    for root, dirs, files in os.walk(repo_path):
        for name in dirs + files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                yield path


# The mechanisms of the items that the detection step reads through a marker: the rules and
# agents that a front matter test selects (`detection._file_text`), every item with a
# `language` column, which describes the file that the marker names
# (`detection.measured_artifact`), and the hook files of `.github/hooks`, which the detection
# step counts only when their JSON configures hooks (`detection.detect_hooks`). A context file
# that is a link is published as a reference to its target, and the settings of a folder of
# items publish no column read from their text. The file behind such a link is part of no
# row.
TARGET_READING_MECHANISMS = (heuristics.LANGUAGE_MECHANISMS - {"context_file"}) | {"hooks"}


def reads_through_marker(heuristic_rows, name):
    """Whether one of `heuristic_rows`, the rows of a folder of file items, reads a file named
    `name` directly in the folder as an item of `TARGET_READING_MECHANISMS`, by its extension.
    For example, a link named `notes.txt` in `.cursor/rules` is no rule, and no row reads its
    text."""
    return any(row["mechanism"] in TARGET_READING_MECHANISMS
               and heuristics.heuristic_matches(row, f"{row['path']}/{name}")
               for row in heuristic_rows)


def find_marker_files(repo_path):
    """Return the files that each collected symlink marker of a file item names, keyed by the
    marker's path.

    A link to a file directly in a mechanism directory of file items, such as rules and agents,
    or in such a directory among a plugin's parts (`paths.link_parents`), is a marker. The
    detection step reads through it where a row of the directory matches the link as an item
    of a mechanism with text that the detection step reads (`reads_through_marker`). For
    example, angular/angular's `.agent/rules/agents.md` links to `../../AGENTS.md`, and the
    detection step reads the trigger of the rule in the file that the marker names. That file
    has to be in the tree. The files are kept only where a rule keeps the marker
    (`scan_and_clean_repo`). A marker in a `.github` below the root, which the detection step
    does not read, is deleted together with its file. A file that a link directly in a skills
    directory names is no skill and is not returned, unless a row of the directory reads files
    directly in it, as OpenCode 2 reads the Markdown files of `.opencode/skills`. The SKILL.md of
    a skill folder is a marker too where it was a link, and the detection step reads the skill in
    the file that it names (`paths.skill_files`). Where a tool directory was a link, the
    directories include those in
    the directory that it leads to, such as the `rules/` of the `config/claude` that the marker
    `.claude` names.
    """
    markers = {}
    for directory, heuristic_rows, skills in paths.link_parents(Path(repo_path), VENDORED_DIRS):
        if directory.is_symlink() or not directory.is_dir():
            continue
        items = ([entry for entry in sorted(directory.iterdir())
                  if reads_through_marker(heuristic_rows, entry.name)] if heuristic_rows else [])
        for entry in items + (paths.skill_files(directory) if skills else []):
            if entry.is_file() and not entry.is_symlink():
                targets = _marker_target_files(repo_path, str(entry))
                if targets:
                    markers[str(entry)] = targets
    return markers


def find_marker_targets(repo_path):
    """Return the directories that a collected symlink marker points at.

    The cloning step writes a symlinked mechanism directory as a one-line file that
    contains its target, and copies the target as well. Without this function, the step would
    keep the target only inside a spec directory, and it would delete the copied target of a
    `.claude/skills` link to `../skills`. A skill linked on its own, a `.claude/skills/<name>`
    that points at a directory elsewhere in the repository, is the same case one level down,
    wherever the collector found the skills directory. So is a folder linked on its own into a
    Claude Code commands directory, such as GovTechSG/sgds-web-component's
    `.claude/commands/sgds-components` that points at `../../skills/sgds-components`, because
    Claude Code reads every `.md` below the link as a command. A folder linked into its rules
    directory, such as opendatahub-io/opendatahub-operator's `.claude/rules/.rules` that points
    at `../../.rules`, is the same case. `find_marker_files` returns the
    file behind a marker of a file item.

    A mechanism directory below the root that is a link, such as the `.cursor/rules` of a
    package in a monorepo, is the same case at a place that the detection step reads
    (`detection._nested_anchors`). The collector copies its target as it copies the target of
    a link at the root.

    A tool directory that was a link (`paths.TOOL_DIRECTORIES`), such as a `.claude` that leads
    to `config/claude`, at the root or at such a place, is the same case one level up: the
    directory that its marker names is kept whole, as the tool directory itself is, and each
    mechanism directory there is a place where a marker can stand, such as a
    `config/claude/skills` that leads to `shared/skills`.
    """
    targets = set()
    # Computed without the cache of the detection step's function. Filled at this point,
    # before the step deletes any file, the cache would later give `has_detected_mechanism`
    # the places of the uncleaned tree.
    anchors = _nested_anchors.__wrapped__(repo_path)
    linked = _linked_tool_directories(repo_path, anchors)
    # A `.github` that a tool directory links to is not kept whole: its CI workflows and issue
    # templates are no configuration of the linking tool. `is_valid_file` keeps what the tool
    # reads there (`read_through_link`) and leaves the rest to the `.github` rule.
    targets.update(target for found in linked.values() for target in found
                   if not in_github(target))
    places = list(MECHANISM_DIRS)
    for rel in MECHANISM_DIRS:
        head, _, tail = rel.partition("/")
        places.extend(f"{parent}/{rel}" for parent in anchors.get(head, ()))
        places.extend(f"{target}/{tail}" for target in linked.get(head, ()) if tail)
    for rel in places:
        marker = os.path.join(repo_path, rel)
        # A tool directory can be a mechanism directory too, such as `.claude` with
        # `hookify.*.local.md`, and `_linked_tool_directories` has read its marker.
        if posixpath.basename(rel) in paths.TOOL_DIRECTORIES and os.path.isfile(marker):
            continue
        if os.path.islink(marker) and os.path.isdir(marker):
            # A link that rsync kept because it stays inside a directory that the collector
            # copies whole, such as `.github/agents -> copilot-agents`. The collector copied
            # its target too, and the detection step reads the items through the link.
            target = _link_target_dir(repo_path, marker)
        elif os.path.isfile(marker):
            target = _marker_target_dir(repo_path, marker)
        else:
            continue
        if target:
            targets.add(target)
    # A place below the root that a `nested:` row reads as a directory and that was a link, such
    # as a package's `.github/agents` that leads to `../shared/agents` (`paths.nested_places`). The
    # collector copies the directory that it leads to, as it copies the target of the root's
    # `.github/agents` that is a link, and the detection step reads the items there.
    for rel, heuristic_row in paths.nested_places(Path(repo_path), VENDORED_DIRS):
        if heuristics.is_directory_mode(heuristic_row["match_mode"]):
            target = paths.linked_nested_directory(Path(repo_path), rel)
            if target:
                targets.add(target)
    for dirs, holds_items in ((SKILL_DIRS, _contains_a_skill),
                              (LINKED_MARKDOWN_DIRS, _contains_markdown)):
        for rel in dirs:
            head, _, tail = rel.partition("/")
            for parent in [*paths.find_dirs_by_name(Path(repo_path), head, VENDORED_DIRS),
                           *(Path(repo_path) / target for target in linked.get(head, ()))]:
                base = parent / tail if tail else parent
                if not base.is_dir() or base.is_symlink():
                    continue
                for entry in sorted(base.iterdir()):
                    if entry.is_file() and not entry.is_symlink():
                        target = _marker_target_dir(repo_path, str(entry))
                        if target and holds_items(os.path.join(repo_path, target)):
                            targets.add(target)
    return targets


def _contains_a_skill(directory):
    """Whether a directory contains a `SKILL.md`, in any case and at any depth.

    The detection step reports a linked skill by its `SKILL.md`. A linked directory without
    one would be kept here and named in no row.
    """
    return any(name.lower() == "skill.md"
               for _root, _dirs, files in os.walk(directory) for name in files)


def _contains_markdown(directory):
    """Whether a directory contains a `.md` file at any depth, which Claude Code reads as a
    command or a rule where a commands or rules directory links to the directory. A linked
    directory without one would be kept here and named in no row."""
    return any(name.endswith(".md")
               for _root, _dirs, files in os.walk(directory) for name in files)


def is_inside_submodule(file_path, repo_path, submodule_paths):
    """Whether a path is inside one of the repository's submodules.

    A submodule is a reference to another repository. The repository's tree contains a
    gitlink, and the files below it belong to the repository that the gitlink names. The
    collector checks the submodule out and copies its files as any other file. It reads the
    gitlinks from GitHub, because a copy of a checked-out submodule is an ordinary directory.
    """
    rel = "/".join(get_path_parts(file_path, repo_path))
    return any(rel == p or rel.startswith(p + "/") for p in submodule_paths)


def is_valid_file(file_path, repo_path, plugin_parents, keep_github, marker_targets=(),
                  submodule_paths=(), declared=frozenset(), plugin_parts=(),
                  kept=frozenset(), nested=(), linked=()):
    """Whether to keep a file.

    `declared` contains the repository-relative paths that the repository's configuration
    names as context files (`paths.declared_context_files`) or as subagent files, such as a
    Codex role file and its instructions (`paths.declared_subagent_files`). `plugin_parts`
    contains the parts and the manifests of the plugins in the tree (`paths.plugin_parts`,
    `paths.plugin_manifests`), and a directory among them keeps everything below it. `kept`
    contains the files that the step keeps even when they are empty (`kept_when_empty`), such
    as the imports of the GitHub Agentic Workflows, the prompt files of the agent actions and
    the planning documents with their anchors. `nested` contains the places below the root
    that the `nested:` rows read (`paths.nested_places`), and a directory among them keeps
    everything below it. `linked` holds (name, directory) for each tool directory that was a
    link (`linked_tool_directory_targets`).
    """
    basename = os.path.basename(file_path)
    parts = get_path_parts(file_path, repo_path)

    # .DS_Store is never valid anywhere
    if basename == ".DS_Store":
        return False

    # Inside a submodule: another repository's file, whatever it configures.
    if is_inside_submodule(file_path, repo_path, submodule_paths):
        return False

    # Inside a vendored or build-output directory: always rejected, even when
    # an ancestor is a spec directory or a .claude-plugin parent. See
    # docs/heuristics.md. Inside the target of a link at a mechanism path, only a vendored
    # directory below the target counts (`vendored_dir_of`).
    if is_inside_vendored_dir(file_path, repo_path, marker_targets):
        return False

    # A session log, a history, a cache, a lock or a login that a tool keeps for itself, such as
    # a transcript under .claude/projects/, is rejected even where a rule below would keep it.
    # No tool reads such a file as configuration, and it can contain a user's paths, prompts
    # and credentials. The collector copies none, but a tree collected by an earlier collector
    # can contain such files.
    if local_state_of(file_path, repo_path, linked) is not None:
        return False

    # Inside a plugin manifest directory itself: always valid.
    if is_inside_claude_plugin_dir(file_path, repo_path):
        return True

    # A context file that the repository's configuration names is kept whatever its name and
    # wherever it is, such as a file in .github or a lowercase gemini.md that an extension
    # manifest names. The case rule below would delete that gemini.md as a misspelled
    # GEMINI.md.
    if "/".join(parts) in declared:
        return True

    # A file that a GitHub Agentic Workflow imports, or the prompt file of an agent action, is
    # part of its workflow wherever it is, such as in `.github/workflows/shared/`, which the
    # `.github` rule below would delete as CI, or in `.agents/`. The prompt file that an
    # OpenCode agent names is part of the agent wherever it is. A planning document or its
    # anchor is kept wherever its framework or tool puts it, such as in `specs/` beside a
    # `.specify/`, in `openspec/` or in `.github/upgrades/`. The rules below would delete these
    # files as files outside every spec directory or as irrelevant files in `.github`.
    rel = "/".join(parts)
    if rel in kept:
        return True

    # A part of a plugin in the repository, wherever the plugin is: its manifest, skills,
    # agents, commands, hooks and MCP servers. The rules below would delete these parts
    # outside `.claude/plugins`.
    if any(rel == part or rel.startswith(part + "/") for part in plugin_parts):
        return True

    # Directly beside a plugin manifest directory and an allowed sibling, such as a README,
    # package.json or a standalone spec file: kept as plugin metadata. Other siblings and
    # deeper descendants are dropped.
    if is_claude_plugin_parent_kept_sibling(file_path, plugin_parents):
        return True

    # Inside a spec directory, such as .claude or .cursor: all content kept
    if is_inside_spec_dir(file_path, repo_path):
        return True

    # Inside a directory that a symlinked mechanism directory points at, the file that a
    # link in a mechanism directory points at, or an entry that a kept link leads to. The
    # test comes before the .github rule and the rule for a standalone SKILL.md, because
    # neither rule would keep a target reached through a link. Placed after the .github rule,
    # the test would come too late for the target of a marker that points under .github. That
    # target would be dropped, and with it the mechanism and, when it has no other, the
    # repository.
    rel = "/".join(parts)
    for target in marker_targets:
        target = target.replace(os.sep, "/")
        if rel == target or rel.startswith(target + "/"):
            return True

    # A file of a `.github` that a tool directory links to, such as plesk/centos2alma's
    # `.claude -> .github/`, where the linking tool reads it as a configuration artifact. The
    # `.github` rule below decides on every other file there, as it would without the link, and
    # deletes a CI workflow or an issue template, which the tool reads as no configuration.
    if any(rel.startswith(directory + "/") and read_through_link(name, rel[len(directory) + 1:])
           for name, directory in linked if in_github(directory)):
        return True

    # A place in a `.github` below the root that Copilot CLI reads in the directory that it
    # works in, such as a package's `.github/copilot-instructions.md` or `.github/agents/`,
    # whatever the root's `.github` contains.
    if any(rel == place or rel.startswith(place + "/") for place in nested):
        return True

    # Inside .github, keep only the Copilot paths, the context files and an agent workflow,
    # which belongs to the tool that it runs rather than to Copilot.
    if is_inside_github_dir(file_path, repo_path):
        if is_agent_workflow(file_path, repo_path):
            return True
        if not keep_github:
            return False
        return is_relevant_github_file(file_path, repo_path)

    # SKILL.md outside spec dirs: not valid as a standalone file
    if basename == "SKILL.md":
        return False

    # Root-only spec files (e.g., .mcp.json only at repo root)
    if basename in ROOT_ONLY_FILES and len(parts) == 1:
        return True

    if "/".join(parts) in ROOT_ONLY_PATHS:
        return True

    if len(parts) == 1 and basename.lower() in ROOT_ANY_CASE:
        return True

    # Standalone spec file at any depth
    if basename in SPEC_FILES:
        return True

    # A portable Agent Plugins document, kept only when its content declares the standard.
    # Programs unrelated to agents also use `plugin.json` and `mcp.json`, and a test by name
    # alone would keep their files too.
    if basename in AGENT_PLUGINS_NAMES:
        return paths.agent_plugins_document(AGENT_PLUGINS_NAMES[basename],
                                            read_text_or_empty(file_path))

    return False


def clean_empty_dirs(repo_path):
    """Remove empty directories bottom-up.

    The removal goes through `paths.remove_tree`, because DSM can write an `@eaDir` into a
    directory between the listing that finds it empty and its removal."""
    removed = []
    for root, dirs, files in os.walk(repo_path, topdown=False):
        if root == repo_path:
            continue
        if not os.listdir(root):
            paths.remove_tree(root)
            removed.append(root)
    return removed


def is_empty_file(file_path):
    """Check if a file has no meaningful content.

    A file is empty if it contains only whitespace or parses as an empty JSON
    object or array. A TOML file with only comments is not empty.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError):
        return False

    if not content or not content.strip():
        return True

    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".json":
        # A parse error is a ValueError: a JSONDecodeError, or the ValueError of an integer
        # longer than Python converts, which is no JSONDecodeError.
        try:
            if json.loads(content) in ({}, []):
                return True
        except (ValueError, RecursionError):
            pass

    return False


def kept_when_empty(repo_path) -> set:
    """The repository-relative paths of the files that the step keeps even when they are
    empty, wherever they are: the files that the GitHub Agentic Workflows import
    (`paths.agentic_workflow_imports`), the prompt files that the agent actions of the
    workflows read (`paths.action_prompt_files`), the prompt files that the agents of an
    OpenCode configuration name (`paths.opencode_prompt_files`), the planning documents with
    their anchors (`planning.find`), and the CLAUDE files that decide whether Claude Code reads an
    AGENTS.md (`imports.deciding_claude_files`). The import chains follow these CLAUDE files, in
    the collected tree when the cloning step reads them and in the validated tree when the import
    detection step does.

    gh-aw does not compile a workflow without a required import, empty or not, and the agent of
    an action runs with the prompt that its prompt file contains. Each such file is therefore
    part of its workflow, as the prompt file of an OpenCode agent is part of the agent. An anchor
    can be an empty file, such as a `backlog/config.yml` of whitespace, and deleting it as an
    empty file would leave the documents that need it without their anchor. The anchors are
    kept with the documents, as the cloning step copied them, so that the detection step finds
    the same documents in the validated tree. The analysis of the content quality reads the
    same set in the validated tree and counts a listed empty file with 0 lines.
    """
    repo = Path(repo_path)
    kept = set(paths.agentic_workflow_imports(repo, WORKFLOW_ENGINES))
    kept.update(paths.action_prompt_files(repo))
    kept.update(paths.opencode_prompt_files(repo, VENDORED_DIRS))
    documents, anchors = planning.find(repo, VENDORED_DIRS)
    kept.update(rel for rel, _heuristic_row in documents)
    kept.update(anchors)
    kept.update(imports.deciding_claude_files(paths.tree_files(repo)))
    return kept


DEFAULT_SUBMODULES = os.path.join(str(STAGE_DIR),
                                  "submodules.csv")


def load_submodules(path=DEFAULT_SUBMODULES):
    """Map each repository to the submodule paths that the cloning step recorded for it."""
    if not os.path.isfile(path):
        return {}
    found = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            found.setdefault(row["repo_name"], []).append(row["path"])
    return {name: tuple(sorted(paths)) for name, paths in found.items()}


def leads_outside_repository(path, repo_path):
    """Whether `path` is a symbolic link that resolves outside the repository.

    The copy keeps a real symlink as a link, and `is_valid_file` decides on a link by its own
    path. A link inside a spec directory would therefore be kept whatever its target. The
    detection step reads through a link. Through a `.claude/skills` link to a directory outside
    the repository, it would report the SKILL.md files there as this repository's, under a path
    with no content in the repository's tree.
    """
    if not os.path.islink(path):
        return False
    root = os.path.realpath(repo_path)
    try:
        return os.path.commonpath([os.path.realpath(path), root]) != root
    except ValueError:
        return True


def remove_empty_files(repo_path, kept, deletions, removed) -> bool:
    """Delete the empty files of the repository at `repo_path` that are not in `kept`, the
    files that the step keeps even when they are empty. Where `kept` is None, the function
    computes `kept_when_empty` over the tree as it is, and only when the tree has an empty
    file. Returns whether a file was deleted.

    `removed` collects the real paths of the files that the calls delete. A link that one of
    those deletions left dangling counts as empty, such as a `.agents/CLAUDE.md` that links to
    the `AGENTS.md` beside it. Kept as a CLAUDE file that decides about that AGENTS.md, the
    link would lead nowhere once the AGENTS.md was deleted."""
    empty = []
    for root, dirs, files in os.walk(repo_path):
        for f in files:
            file_path = os.path.join(root, f)
            if is_empty_file(file_path) or (os.path.islink(file_path)
                                            and not os.path.exists(file_path)
                                            and os.path.realpath(file_path) in removed):
                empty.append(("/".join(get_path_parts(file_path, repo_path)), file_path))
    if empty and kept is None:
        kept = kept_when_empty(repo_path)
    deleted = False
    for rel, file_path in empty:
        if rel not in kept:
            deletions.append((os.path.relpath(file_path, "."), "empty file"))
            if not os.path.islink(file_path):
                removed.add(os.path.realpath(file_path))
            os.remove(file_path)
            deleted = True
    return deleted


def scan_and_clean_repo(repo_path, deletions, submodule_paths=()):
    """Scan a repository directory and delete the files that fail the validity rules."""
    # Remove each link that leads outside the repository before any pass below reads or
    # deletes through it. The `.github/hooks` pass lists the directory that it is given, and
    # through such a link it would remove files in the link's target, outside the validated
    # tree.
    for root, dirs, files in os.walk(repo_path):
        for name in dirs + files:
            path = os.path.join(root, name)
            if leads_outside_repository(path, repo_path):
                deletions.append((os.path.relpath(path, "."),
                                  "symlink leading outside the repository"))
                os.remove(path)
        dirs[:] = [d for d in dirs if os.path.lexists(os.path.join(root, d))]

    # Remove each Synology directory whole before any pass below reads the tree. The copy of
    # the collected tree leaves them out, but DSM can write one into the copy at any time
    # afterwards. Inside a directory that a rule keeps whole, such as a plugin's directory, the
    # Synology directory's files would otherwise be kept and published. A link with such a
    # name is not a directory that DSM wrote, and the validity pass judges it as any other
    # entry.
    for root, dirs, files in os.walk(repo_path):
        for name in [d for d in dirs if d in SYNOLOGY_DIRECTORIES
                     and not os.path.islink(os.path.join(root, d))]:
            path = os.path.join(root, name)
            deletions.append((os.path.relpath(path, "."), "Synology DSM directory"))
            paths.remove_tree(path)
            dirs.remove(name)

    # Computed before anything is deleted. The pass below deletes empty files, and once it had
    # deleted an empty anchor, the planning documents that need the anchor would no longer be
    # in the set.
    kept = kept_when_empty(repo_path)

    # Remove empty files first (before validity checks)
    removed_empty = set()
    remove_empty_files(repo_path, kept, deletions, removed_empty)

    plugin_parents = find_claude_plugin_dirs(repo_path)
    marker_targets = find_marker_targets(repo_path)
    marker_files = find_marker_files(repo_path)
    linked_tools = linked_tool_directory_targets(repo_path)
    # Computed before anything below is deleted. The declaring files are kept by their own
    # rules, and the files that they name are kept by this list.
    declared = ({rel for rel, _heuristic_row in paths.declared_context_files(Path(repo_path),
                                                                    VENDORED_DIRS)}
                | {rel for rel, _heuristic_row in paths.declared_subagent_files(Path(repo_path),
                                                                     VENDORED_DIRS)}
                | {rel for rel, _heuristic_row in paths.registered_configuration_files(Path(repo_path),
                                                                   VENDORED_DIRS, include_registering_files=True)})
    plugin_parts = sorted({rel for rel, _heuristic_row in paths.plugin_parts(Path(repo_path),
                                                                   VENDORED_DIRS)}
                          | set(paths.plugin_manifests(Path(repo_path), VENDORED_DIRS)))

    plugin_parts.extend(paths.registered_skill_folders(Path(repo_path), VENDORED_DIRS))
    nested = [rel for rel, _heuristic_row in paths.nested_places(Path(repo_path), VENDORED_DIRS)]

    # Check .github for copilot triggers or context files
    github_dir = os.path.join(repo_path, ".github")
    keep_github = False
    if os.path.isdir(github_dir):
        keep_github = should_keep_github(github_dir)

    # Add the entries that a link leads to where a rule keeps the link. rsync keeps a link as a
    # link when it stays inside a directory that the collector copies whole, such as
    # `.github/copilot-instructions.md -> docs/copilot.md`, and the detection step reads the
    # file through it. Judged by its own path, the file would be deleted and the link would
    # lead nowhere. The files that a marker of a file item names are kept the same way, where
    # a rule keeps the marker. The search repeats until it reaches no new entry, because a kept
    # directory can contain links that a rule then keeps.
    while True:
        reached = set()
        for link in _links(repo_path):
            if is_valid_file(link, repo_path, plugin_parents, keep_github, marker_targets,
                             submodule_paths, declared, plugin_parts, kept, nested,
                             linked_tools):
                reached.update(_link_chain(repo_path, link))
        for marker, targets in marker_files.items():
            if is_valid_file(marker, repo_path, plugin_parents, keep_github, marker_targets,
                             submodule_paths, declared, plugin_parts, kept, nested,
                             linked_tools):
                reached.update(targets)
        if reached <= marker_targets:
            break
        marker_targets |= reached

    # Delete non-JSON files from .github/hooks/. The files of a directory that a
    # `.github/hooks` link points at are left to the rules below, which judge them by their own
    # paths.
    hooks_dir = os.path.join(repo_path, ".github", "hooks")
    if os.path.isdir(hooks_dir) and not os.path.islink(hooks_dir):
        for f in os.listdir(hooks_dir):
            fpath = os.path.join(hooks_dir, f)
            if os.path.isfile(fpath) and not f.endswith(".json"):
                rel_path = os.path.relpath(fpath, ".")
                deletions.append((rel_path, "non-JSON file in .github/hooks"))
                os.remove(fpath)

    # Check every file for validity. os.walk sorts a symlink to a directory into `dirs`, where
    # nothing would test it, and an invalid link would remain in data/validated/ and the
    # published archive. Each such link is moved to the file list, to be checked as an entry,
    # and os.walk does not descend into it.
    for root, dirs, files in os.walk(repo_path, followlinks=False):
        linked = [d for d in dirs if os.path.islink(os.path.join(root, d))]
        if linked:
            dirs[:] = [d for d in dirs if d not in linked]
            files = files + linked
        for f in files:
            file_path = os.path.join(root, f)
            if not is_valid_file(file_path, repo_path, plugin_parents, keep_github,
                                 marker_targets, submodule_paths, declared, plugin_parts,
                                 kept, nested, linked_tools):
                rel_path = os.path.relpath(file_path, ".")
                # Determine reason
                if f == ".DS_Store":
                    reason = "macOS Finder file"
                elif is_inside_submodule(file_path, repo_path, submodule_paths):
                    relative = "/".join(get_path_parts(file_path, repo_path))
                    inside = next(p for p in submodule_paths
                                  if relative == p or relative.startswith(p + "/"))
                    reason = f"inside submodule ({inside})"
                elif is_inside_vendored_dir(file_path, repo_path, marker_targets):
                    reason = (f"inside vendored directory "
                              f"({vendored_dir_of(file_path, repo_path, marker_targets)})")
                elif local_state_of(file_path, repo_path, linked_tools) is not None:
                    reason = (f"local state of a tool "
                              f"({local_state_of(file_path, repo_path, linked_tools)})")
                elif f.lower() == "claude.md" and f != "CLAUDE.md":
                    reason = f"wrong letter case of {f} (instead of CLAUDE.md)"
                elif f.lower() == "agents.md" and f != "AGENTS.md":
                    reason = f"wrong letter case of {f} (instead of AGENTS.md)"
                elif f.lower() == "gemini.md" and f != "GEMINI.md":
                    reason = f"wrong letter case of {f} (instead of GEMINI.md)"
                elif f.lower() == "plans.md" and f != "PLANS.md":
                    reason = f"wrong letter case of {f} (instead of PLANS.md)"
                elif f == "SKILL.md":
                    reason = "SKILL.md outside valid directory"
                elif is_inside_github_dir(file_path, repo_path) and not keep_github:
                    reason = "file in .github without Copilot configuration or a context file"
                elif is_inside_github_dir(file_path, repo_path):
                    reason = "file in .github outside the Copilot paths"
                else:
                    reason = "not a spec file"
                deletions.append((rel_path, reason))
                os.remove(file_path)

    # The step chose the files that it keeps although they are empty before the passes above,
    # and those passes can delete what made one of them count, such as the AGENTS.md that an
    # empty CLAUDE file decides about. Such a file, if kept, would configure nothing. The
    # analysis of the content quality chooses the set in the validated tree and would stop at
    # an empty file outside it. The loop therefore chooses the set again over the cleaned tree
    # until the set holds every empty file that is left.
    while remove_empty_files(repo_path, None, deletions, removed_empty):
        pass

    # Remove empty directories, and log only the topmost directory of each removed subtree
    removed_dirs = set(clean_empty_dirs(repo_path))
    for d in removed_dirs:
        parent = os.path.dirname(d)
        if parent not in removed_dirs:
            rel_path = os.path.relpath(d, ".")
            deletions.append((rel_path, "empty directory after cleanup"))


def compute_columns(repo_path):
    """Compute the boolean columns from the remaining files."""
    columns = {
        "claude": False,
        "gemini": False,
        "codex": False,
        "copilot": False,
        "cursor": False,
        "opencode": False,
        "claude.md": False,
        "gemini.md": False,
        "agents.md": False,
        "plans.md": False,
        "copilot-instructions": False,
        "cursorrules": False,
    }

    if not os.path.isdir(repo_path):
        return columns

    # Check for spec directories at top level
    if os.path.isdir(os.path.join(repo_path, ".claude")):
        columns["claude"] = True
    if os.path.isdir(os.path.join(repo_path, ".gemini")):
        columns["gemini"] = True
    if os.path.isdir(os.path.join(repo_path, ".codex")):
        columns["codex"] = True
    if os.path.isdir(os.path.join(repo_path, ".cursor")):
        columns["cursor"] = True
    if os.path.isdir(os.path.join(repo_path, ".opencode")) or any(
            os.path.isfile(os.path.join(repo_path, name)) for name in OPENCODE_ROOT_FILES):
        columns["opencode"] = True

    # Check .github for copilot
    github_dir = os.path.join(repo_path, ".github")
    if os.path.isdir(github_dir):
        if has_copilot_content(github_dir):
            columns["copilot"] = True
            # Check copilot-instructions specifically
            if os.path.exists(os.path.join(github_dir, "copilot-instructions.md")):
                columns["copilot-instructions"] = True
            if os.path.isdir(os.path.join(github_dir, "instructions")):
                columns["copilot-instructions"] = True

    # Look for standalone files
    for root, dirs, files in os.walk(repo_path):
        for f in files:
            if f == "CLAUDE.md":
                columns["claude"] = True
                columns["claude.md"] = True
            elif f == "GEMINI.md":
                columns["gemini"] = True
                columns["gemini.md"] = True
            elif f == "AGENTS.md" or f == "AGENTS.override.md":
                columns["agents.md"] = True
            elif f == "PLANS.md":
                columns["plans.md"] = True
            elif f == ".cursorrules":
                columns["cursor"] = True
                columns["cursorrules"] = True

    # The flag is the lower-case name, and a row that names a tool sets that tool's flag too.
    for f in os.listdir(repo_path):
        tool = ROOT_ANY_CASE.get(f.lower())
        if tool and os.path.isfile(os.path.join(repo_path, f)):
            columns[f.lower()] = True
            if tool != "any":
                columns[tool] = True

    return columns


def all_false(columns):
    """Check if all boolean columns are FALSE."""
    return not any(columns.values())


def load_clone_failures(path: str) -> set:
    """The repository names in `clone_failed.csv`, or an empty set when the list is absent.

    The function checks the header rather than reading the rows with `.get`, which would
    return an empty set for a list with another header and for the zero-byte list that a
    killed cloning run leaves. The result keeps a repository that failed to clone out of the
    validated tree. An empty set without a warning would publish that repository with every
    Git column blank.
    """
    try:
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            if "repo_name" not in (reader.fieldnames or []):
                logs.warning('collected_file_cleanup', f"{path} has no repo_name column. No clone "
                                                       f"failure is excluded. Re-run the cloning "
                                                       f"step to rewrite the file.")
                return set()
            return {row["repo_name"] for row in reader if row.get("repo_name")}
    except OSError:
        return set()


def main(argv=None) -> int:
    parser = cli.ArgumentParser(description="Validate and clean up the collected configuration artifacts.")
    parser.add_argument("--collected-dir", default=COLLECTED_DIR,
                        help=f"The collected tree, left unchanged (default: {COLLECTED_DIR})")
    parser.add_argument("--collected-csv", default=COLLECTED_CSV,
                        help=f"The collected list, left unchanged (default: {COLLECTED_CSV})")
    parser.add_argument("--validated-dir", default=VALIDATED_DIR,
                        help=f"The validated tree to write and clean (default: {VALIDATED_DIR})")
    parser.add_argument("--excluded-dir", default=EXCLUDED_DIR,
                        help=f"Directory for excluded repositories, created and left empty "
                             f"(default: {EXCLUDED_DIR})")
    parser.add_argument("--validated-csv", default=VALIDATED_CSV,
                        help=f"The validated list to write (default: {VALIDATED_CSV})")
    parser.add_argument("--submodules", default=DEFAULT_SUBMODULES,
                        help="CSV of the submodule paths that the cloning step recorded, under "
                             f"which every file is dropped (default: {DEFAULT_SUBMODULES})")
    parser.add_argument("--deletion-csv", default=DELETION_CSV,
                        help=f"CSV of the deleted files and directories, with the reason for "
                             f"each (default: {DELETION_CSV})")
    args = parser.parse_args(argv)

    collected_dir = args.collected_dir
    collected_csv = args.collected_csv
    validated_dir = args.validated_dir
    excluded_dir = args.excluded_dir
    validated_csv = args.validated_csv
    deletion_csv = args.deletion_csv

    # Fail on a missing or empty collected tree. Otherwise the script would process no
    # repository, write an empty CSV and exit 0. The runner would read that exit code as
    # success and run the next steps over broken data.
    if not os.path.isfile(collected_csv):
        logs.error('collected_file_cleanup', f"Could not find the collected list {collected_csv}.")
        return 1
    if not os.path.isdir(collected_dir):
        logs.error('collected_file_cleanup', f"Could not find the collected tree {collected_dir}.")
        return 1
    # The test is whether the tree contains a repository directory, not whether the directory
    # is empty. The list is stored in the tree, and a cloning run that produced only its list
    # leaves a directory that is not empty.
    #
    # The stray entries are logged here, because only this listing contains them. The copy
    # below takes the repository directories one at a time. A `.DS_Store`, a reconstruction
    # extracted in place or a symlinked entry never reaches the validated tree, and the
    # counts further down do not include it. A stray entry is either an accident to remove or
    # a naming change that nobody recorded, and `repos.split` reports each one, so that none
    # is dropped without a warning.
    collected_repos, collected_strays = repos.split(collected_dir,
                                                         expected=COLLECTED_TREE_FILES)
    if collected_strays:
        logs.warning('collected_file_cleanup', f"{len(collected_strays):,} entry/entries in "
                                               f"{collected_dir} are not repository directories "
                                               f"and are not copied into the validated tree "
                                               f"({', '.join(collected_strays)}).")
    if not collected_repos:
        logs.error('collected_file_cleanup', f"No repository directory found in {collected_dir}.")
        return 1

    # An older collected list names the head commit date `timestamp`. The filter that
    # derives the validated list keeps only the columns that it recognizes and drops the rest
    # without a warning. Such a list would become a validated list without dates, and the
    # detection step would publish blank dates for every repository. The check runs before
    # the copy below, which replaces the validated tree.
    with open(collected_csv, "r", newline="", encoding="utf-8") as f:
        collected_header = csv.DictReader(f).fieldnames or []
    if LEGACY_TIMESTAMP_COLUMN in collected_header:
        logs.error('collected_file_cleanup', f"{collected_csv} has a {LEGACY_TIMESTAMP_COLUMN!r} "
                               f"column. The current list names that column 'head_commit_date' "
                               f"and adds 'scanned_at'. Run the cloning step again to rebuild "
                               f"the list.")
        return 1
    # The header has to name the repository and at least one more metadata column. The check
    # does not require the whole set, because a list from a cloning run that predates a column
    # still validates. A truncated or empty list names none of the metadata columns. It would
    # become a validated list of names with every other column blank, and the detection step
    # would publish its repositories without a branch, a head_sha or a github_link. A killed
    # cloning run can leave the list truncated, because `repository_cloning` writes it.
    if not collected_header or not set(collected_header) & (set(METADATA_COLUMNS) - {"repo_name"}):
        logs.error('collected_file_cleanup', f"{collected_csv} has none of the metadata columns "
                                             f"{list(METADATA_COLUMNS)}. Every column of the "
                                             f"validated list except the name would be blank. "
                                             f"Run the cloning step again to rewrite the list "
                                             f"from its list of finished repositories.")
        return 1
    if "repo_name" not in collected_header:
        logs.error('collected_file_cleanup', f"{collected_csv} has no repo_name column.")
        return 1
    with open(collected_csv, "r", newline="", encoding="utf-8") as f:
        listed = {row["repo_name"] for row in csv.DictReader(f) if row.get("repo_name")}

    # Derive a new validated tree from the collected tree. data/collected/ is never
    # modified, so that every run starts from the same input (see the module docstring).
    #
    # The repositories are copied one at a time, not the whole directory, because the
    # collected list and the list of clone failures are in the tree beside them. A `copytree`
    # of the directory would copy both lists into the validated tree. The collected list is
    # copied afterwards, as the validated list.
    if os.path.exists(validated_dir):
        paths.remove_tree(validated_dir)
    os.makedirs(validated_dir)
    # A directory for a repository in the list of clone failures holds a tree that a cloning
    # error left after the copy, although the row records the tree as never copied. The
    # cloning step removes such a directory, but an older collected tree can still contain
    # one. If copied, the repository would be published with a blank branch and head_sha.
    failed = load_clone_failures(os.path.join(collected_dir, "clone_failed.csv"))
    # A directory that the collected list does not name belongs to no collection that the
    # list describes. A repository that left the input list keeps the tree that an earlier
    # run wrote, while the cloning step rewrites the list from the input alone. If copied,
    # such a directory would be validated with every metadata column blank, and the detection
    # step would publish it without a `head_sha` to record which commit its files came from.
    unlisted = []
    for name in collected_repos:
        if repos.dir_name_to_repo(name) in failed:
            logs.warning('collected_file_cleanup', f"{name} is in clone_failed.csv. Did not "
                                                   f"copy the directory that the failed "
                                                   f"collection left.")
            continue
        if repos.dir_name_to_repo(name) not in listed:
            unlisted.append(name)
            continue
        # `paths.copy_tree` leaves out what DSM writes, which the Synology pass would delete
        # anyway, and copies without recursion, so that no depth of a collected directory ends
        # the copy.
        paths.copy_tree(os.path.join(collected_dir, name), os.path.join(validated_dir, name))
    if unlisted:
        shown = ", ".join(unlisted[:UNLISTED_NAMED]) + (
            "" if len(unlisted) <= UNLISTED_NAMED
            else f", and {len(unlisted) - UNLISTED_NAMED:,} more")
        logs.warning('collected_file_cleanup', f"Did not copy {len(unlisted):,} repository "
                                               f"directories in {collected_dir} without a row in "
                                               f"{collected_csv} ({shown}). Without a row, they "
                                               f"have no commit and no scan time. Collect them "
                                               f"again with an input list that names them, or "
                                               f"remove the directories.")
    shutil.copy2(collected_csv, validated_csv)

    # Step 0: Read CSV and list directories
    csv_repos = {}  # the row of each repo_name
    csv_fieldnames = None

    with open(validated_csv, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        csv_fieldnames = reader.fieldnames
        for row in reader:
            csv_repos[row["repo_name"]] = row

    # List the repository directories. Not every directory is one. An archive extracted in
    # place, or the documented reconstruction run twice, leaves a directory that would
    # otherwise be scanned, added to the validated list under its own name and published as a
    # repository that nobody collected.
    repo_dir_names, strays = repos.split(validated_dir,
                                              expected=VALIDATED_TREE_FILES)
    dir_repos = set(repo_dir_names)
    if strays:
        logs.warning('collected_file_cleanup', f"{len(strays):,} entry/entries in "
                                               f"{validated_dir} are not repository directories "
                                               f"and are not part of the sample "
                                               f"({', '.join(strays)}).")

    if not dir_repos:
        logs.error('collected_file_cleanup', f"No repository directory found in {validated_dir}.")
        return 1

    submodules = load_submodules(args.submodules)
    if submodules:
        logs.info('collected_file_cleanup', f"Dropping the files under the "
                                            f"{sum(len(v) for v in submodules.values()):,} "
                                            f"submodule path(s) in {len(submodules):,} "
                                            f"repositories that "
                                            f"{os.path.basename(args.submodules)} lists.")
    else:
        logs.info('collected_file_cleanup', f"No submodule path recorded by the cloning step "
                                            f"in {args.submodules}. No file is dropped as "
                                            f"another repository's.")

    # Step 1-2: Scan and clean each repo directory
    deletions = []
    logs.info('collected_file_cleanup', f"Cleaning {len(dir_repos):,} repository directories.")

    pace = logs.Pace()
    for i, dir_name in enumerate(sorted(dir_repos)):
        repo_path = os.path.join(validated_dir, dir_name)
        scan_and_clean_repo(repo_path, deletions,
                            submodules.get(dir_name_to_repo(dir_name), ()))
        if pace.due():
            logs.info('collected_file_cleanup', f"{i + 1:,}/{len(dir_repos):,} repository directories scanned.")

    empty_count = sum(1 for _, reason in deletions if reason == "empty file")
    logs.info('collected_file_cleanup', f"Deleted {len(deletions):,} files and directories, "
                                        f"{empty_count:,} of them empty files.")

    # Step 3: Compute columns for all repos with directories
    results = {}  # the columns of each repo_name
    repos_to_delete = []

    for dir_name in sorted(dir_repos):
        repo_name = dir_name_to_repo(dir_name)
        repo_path = os.path.join(validated_dir, dir_name)

        # Check if directory still has files
        has_files = False
        for root, dirs, files in os.walk(repo_path):
            if files:
                has_files = True
                break

        if not has_files:
            # Empty repo directory: delete
            repos_to_delete.append((dir_name, repo_name))
            continue

        columns = compute_columns(repo_path)

        # PLANS.md is valid only with Codex evidence: a .codex directory at the root or an
        # AGENTS.md.
        if columns["plans.md"] and not columns["codex"] and not columns["agents.md"]:
            columns["plans.md"] = False
            # Delete PLANS.md files from repo
            for root, dirs, files in os.walk(repo_path):
                for f in files:
                    if f == "PLANS.md":
                        fpath = os.path.join(root, f)
                        os.remove(fpath)
                        deletions.append((os.path.relpath(fpath, "."), "PLANS.md without Codex configuration or AGENTS.md"))
            clean_empty_dirs(repo_path)

        # Step 4: keep the repository when at least one named mechanism is detected, the
        # criterion that the dataset states. A repository is configured when it has a
        # configuration artifact, not when it has a directory that could contain one, such as
        # an empty .claude/ directory, which all_false(columns) would count.
        if not has_detected_mechanism(repo_path):
            repos_to_delete.append((dir_name, repo_name))
            continue

        results[repo_name] = columns

    # Delete the repositories with no detected mechanism
    for dir_name, repo_name in repos_to_delete:
        repo_path = os.path.join(validated_dir, dir_name)
        if os.path.isdir(repo_path):
            paths.remove_tree(repo_path)
            deletions.append((os.path.relpath(repo_path, "."), "no detected mechanism, repository directory removed"))

    logs.info('collected_file_cleanup', f"Removed {len(repos_to_delete):,} repository directories that "
                          f"were empty or had no detected mechanism.")

    # No repository is moved to the excluded directory, because no category lies between kept
    # and deleted. A repository with a root .mcp.json as its only configuration artifact has a
    # detected mechanism and is configured although it sets no tool flag, and a repository
    # with no detected mechanism is deleted above.
    #
    # The directory is still created and stays empty. Nothing reads it. Neither the runner nor
    # the archive step names it, and this step is its only writer.
    os.makedirs(excluded_dir, exist_ok=True)

    # Step 5: Write output CSV
    # Only include repos that have directories with valid files
    output_rows = []
    for repo_name in sorted(results):
        row = {"repo_name": repo_name}
        # Copy the metadata of the collection from the collected list. head_sha identifies
        # the commit that the configuration artifacts came from. Validation only deletes
        # files, and the SHA that identified the collected tree still identifies the source of
        # the remaining files.
        source = csv_repos.get(repo_name, {})
        for column in METADATA_COLUMNS[1:]:
            row[column] = source.get(column, "")
        output_rows.append(row)

    # Write only the metadata columns to the validated list. agentcontext/steps/detection.py
    # computes the boolean columns in the next step. The columns keep the order of the
    # collected CSV, and a list from a run that predates head_sha omits that column rather
    # than gaining a blank one.
    # There is no fallback to the full set of columns when the header names none of them. The
    # fallback would write the header that the next step checks for over a list without those
    # values, and a truncated list would validate and publish blanks. The column list cannot
    # be empty. The header check at the top of `main` requires `repo_name` and one more
    # metadata column of `collected_csv`, and `validated_csv` is a `shutil.copy2` of that
    # file. `repo_name` is therefore always among the columns, and a refusal here could never
    # run.
    base_fieldnames = [f for f in (csv_fieldnames or []) if f in METADATA_COLUMNS]

    with open(validated_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=base_fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in output_rows:
            writer.writerow({k: row.get(k, "") for k in base_fieldnames})

    logs.info('collected_file_cleanup', f"Wrote {len(output_rows):,} rows to {validated_csv}.")

    # Write the deletion log, sorted. The loops above follow the filesystem's order, which
    # differs between two runs over freshly copied trees, and an unsorted log would differ
    # between two runs that deleted the same files.
    with open(deletion_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, lineterminator="\n")
        writer.writerow(["path", "reason"])
        for path, reason in sorted(deletions):
            writer.writerow([path, reason])

    logs.info('collected_file_cleanup', f"Wrote {len(deletions):,} entries to {deletion_csv}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
