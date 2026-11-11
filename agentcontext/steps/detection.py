#!/usr/bin/env python3
"""Detect the configuration artifacts in the validated tree and write the mechanism CSVs.
Planning documents are no configuration artifacts, and the step writes them into a CSV of
their own."""

import csv
import fnmatch
import functools
import hashlib
import json
import os
import io
import posixpath
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path
from agentcontext import STAGE_DIR
from agentcontext import cli

from agentcontext import heuristics
from agentcontext import imports
from agentcontext import paths
from agentcontext import planning, planning_schema
from agentcontext.heuristics import (CONFIG_PATH_PATTERNS_PATH, directory_paths, extensions_for, 
                              is_directory_mode, load_config_path_patterns, one_path, paths_for,
                              csv_header, TOOLS, tool_value)
from agentcontext.repos import url_path
from agentcontext import caches
from agentcontext import jsonc

csv.field_size_limit(sys.maxsize)
from agentcontext import repos
from agentcontext import commits
from agentcontext import logs

# Language detection for every configuration artifact in a CSV with a `language` column. The
# package is optional, and a run without it logs its absence rather than failing. The package
# includes a large model, which an offline regeneration of everything else should not need.
try:
    from lingua import LanguageDetectorBuilder
    LINGUA_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by the pre-checks, not here
    LINGUA_AVAILABLE = False

# The dataset release that these heuristics produce. The version changes with a detection
# heuristic, not with a tag, because a CSV read on its own then records which heuristics built
# it. CHANGELOG.md lists what each version changed.
DATASET_VERSION = "2.0.0"

VALIDATED_DIR = Path("data/validated")
VALIDATED_CSV = Path("data/validated/repos.csv")
OUTPUT_DIR = Path("output")

# The list that the collected file cleanup writes at the root of the validated tree. The
# listing below expects it, and would otherwise report it as a stray entry on every run.
VALIDATED_TREE_FILES = ("repos.csv",)
FILE_HISTORY_CSV = Path("file_history.csv")
AI_AUTHORSHIP_CSV = Path("ai_authorship.csv")

# The columns of excluded.csv, with one row per repository in the validated list that has no
# directory in the validated tree. A row records when and at which commit the repository was
# collected, and no column of the detectors, because the repository has nothing to detect.
EXCLUDED_COLUMNS = ("repo_name", "scanned_at", "head_commit_date", "reason")

# Every path below comes from config_path_patterns.csv, the heuristics that the cloning step,
# the collected file cleanup and the detection step share, under the names that the detectors
# and their tests read. heuristics.py defines the columns and the match modes, and
# test_heuristics.py checks that the code and the heuristics agree.
_PATTERNS = load_config_path_patterns()

# The settings files in the heuristics' order, which is the order of the published rows. A
# `root_file` row among them is read at the repository root alone.
SETTINGS_PATHS = [r["path"] for r in _PATTERNS
                  if r["mechanism"] == "settings" and r["match_mode"] in ("file", "root_file")]
SETTINGS_ROOT_FILES = frozenset(paths_for(_PATTERNS, "settings", "root_file"))
SETTINGS_BASENAMES = paths_for(_PATTERNS, "settings", "basename")
SETTINGS_DIRS_TOML = paths_for(_PATTERNS, "settings", "tree_ext:.toml")
# The folders of JSON files that a tool reads directly in the folder, such as OpenCode's themes.
SETTINGS_DIRS_JSON = paths_for(_PATTERNS, "settings", "dir_ext:.json")

CONTEXT_FILENAMES = set(paths_for(_PATTERNS, "context_file", "basename"))
# {lower-case name: tool} of the context files read in any case at the repository root. The
# tools find `agents.md` for `AGENTS.md` on a case-insensitive file system. Below the root,
# only the exact name matches, because the step takes a file there with the name in another
# case for a document about agents rather than a file for them.
ROOT_ANY_CASE = {r["path"].lower(): r["tool"] for r in _PATTERNS
                 if r["match_mode"] == "root_any_case"}
# Context files at a fixed path. They are matched apart from CONTEXT_FILENAMES, which match a
# filename at any depth.
CONTEXT_FILES = paths_for(_PATTERNS, "context_file", "file")
ROOT_CONTEXT_FILES = paths_for(_PATTERNS, "context_file", "root_file")
# The copilot-instructions column names Copilot's own instruction files, not every context
# file at a fixed path. Files of other tools, such as .cursor/BUGBOT.md, are matched the same
# way.
COPILOT_CONTEXT_FILES = [r["path"] for r in _PATTERNS
                         if r["tool"] == "copilot" and r["mechanism"] == "context_file"
                         and r["match_mode"] == "file"]
# Copilot instruction files have a double extension, as the prompt files below do:
# "Instructions files are Markdown files with the .instructions.md extension". A plain .md
# there is not an instruction file.
CONTEXT_DIRS_SUFFIX = paths_for(_PATTERNS, "context_file", "tree_suffix:.instructions.md")
# The rows of the files that declare context files: Gemini CLI's settings and extension
# manifests and Codex's config. `paths.declared_context_files` reads what they name.
CONTEXT_DECLARATION_HEURISTICS = heuristics.context_declaration_heuristics(_PATTERNS)
# The rows of the configuration that names subagent files, such as the `config_file` of a role in
# Codex's `.codex/config.toml` and the `model_instructions_file` of a role file.
# `paths.declared_subagent_files` reads what they name.
SUBAGENT_DECLARATION_HEURISTICS = heuristics.declaration_heuristics("subagents", _PATTERNS)
# The places of the declaring rows that read the role files of a directory, such as
# `.codex/agents`, which a row of the heuristics reads as a directory. Such a row names a part
# of a role, such as its instructions, and not a subagent (`_declared_subagents`).
ROLE_DIRECTORIES = frozenset(r["path"] for r in SUBAGENT_DECLARATION_HEURISTICS
                             if r["path"] in directory_paths(_PATTERNS))
INSTRUCTIONS_SUFFIX = ".instructions.md"

# Each skills directory once, also where the heuristics of several tools read it.
SKILLS_DIRS = list(dict.fromkeys(r["path"] for r in _PATTERNS
                                 if r["mechanism"] == "skills"
                                 and r["match_mode"] in ("skill_dir", "skill_dir_flat")))
RECURSIVE_SKILLS_DIRS = paths_for(_PATTERNS, "skills", "skill_dir")
FLAT_SKILLS_DIRS = paths_for(_PATTERNS, "skills", "skill_dir_flat")
# A plugin with the SKILL.md at its root as its skill (`paths.plugin_components`).
PLUGIN_ROOT_SKILLS = paths_for(_PATTERNS, "skills", heuristics.PLUGIN_ROOT_SKILL_MODE)
SKILL_MARKER = one_path(_PATTERNS, "skills", "skill_marker")
# Reported as columns of skills.csv rather than as configuration artifacts of their own.
SKILL_SUPPORT_DIRS = heuristics.SKILL_SUPPORT_DIRS
# The path of the subagent memory directory has a `*` in place of the subagent's name. The
# directory is the memory, a MEMORY.md index and one topic file per memory, and every file in
# it belongs to the subagent.
AGENT_MEMORY_PATTERNS = paths_for(_PATTERNS, "subagents", "agent_memory")

# Claude Code merged custom commands into skills. A file at .claude/commands/deploy.md and a
# skill at .claude/skills/deploy/SKILL.md both define /deploy and behave the same way, and the
# step reports command files as skills. Only Claude Code merged the two, and detect_commands
# keeps the commands of Cursor and Gemini. See https://code.claude.com/docs/en/skills
SKILL_COMMAND_DIRS = paths_for(_PATTERNS, "skills", "tree_ext:.md")
# Beside a command file and a skill, a saved dynamic workflow is the third form of a `/`
# command: a JavaScript file in `.claude/workflows/` or in a plugin's `workflows/`. Claude Code
# saves it as a command of its own. It is a skills row, as a command file is, and it has no
# supporting directories.
SKILL_WORKFLOW_DIRS = paths_for(_PATTERNS, "skills", "tree_ext:.js")
SKILL_WORKFLOW_EXTENSIONS = extensions_for("tree_ext:.js")
# The skills folders that also read a Markdown file directly in the folder as a skill, with a
# description in its front matter, such as OpenCode 2's.
SKILL_DIRS_MD_FLAT_DESCRIBED = paths_for(_PATTERNS, "skills", "dir_ext_described:.md")

SUBAGENT_DIRS_DESCRIBED = paths_for(_PATTERNS, "subagents", "tree_ext_described:.md")
SUBAGENT_DIRS_MD = paths_for(_PATTERNS, "subagents", "tree_ext:.md")
SUBAGENT_MARKDOWN_MODE = "tree_ext:.md|.mdc|.markdown"
SUBAGENT_DIRS_MARKDOWN = paths_for(_PATTERNS, "subagents", SUBAGENT_MARKDOWN_MODE)
SUBAGENT_DIRS_NAMED = paths_for(_PATTERNS, "subagents", "dir_agent:.md")
SUBAGENT_DIRS_TOML = paths_for(_PATTERNS, "subagents", "tree_ext:.toml")
# Copilot's agent directories, with the Markdown files directly in them as agents. VS Code skips
# a README.md there as documentation (promptFileLocations.ts in microsoft/vscode). In a
# Copilot plugin, a README.md with agent front matter is an agent.
SUBAGENT_DIRS_MD_FLAT = paths_for(_PATTERNS, "subagents", "dir_ext:.md")
# The agents folders whose files need YAML front matter with a description, as Gemini CLI's
# local agents do.
SUBAGENT_DIRS_MD_FLAT_DESCRIBED = paths_for(_PATTERNS, "subagents", "dir_ext_described:.md")
# The agents folders of a project that Cursor reads with its loader of subagents, in which a file
# counts with a front matter at its top (`_cursor_agent_has_front_matter`), with the extensions of
# the files that it reads there. A
# Cursor plugin's agents folder is not among them, because the plugin's discovery reads a file
# without front matter and names the agent after the file.
CURSOR_PROJECT_AGENT_ROWS = [r for r in _PATTERNS
                             if r["tool"] == "cursor" and r["mechanism"] == "subagents"
                             and r["match_mode"].startswith("tree_ext:")
                             and not heuristics.plugin_placeholder(r["path"])]
# The heuristic of a project's agents folder of Claude Code, which reads a file there only with the
# front matter that `_claude_agent_loads` tests.
CLAUDE_PROJECT_AGENT_ROWS = [r for r in _PATTERNS
                             if r["tool"] == "claude" and r["mechanism"] == "subagents"
                             and r["match_mode"] == "tree_ext:.md"
                             and not heuristics.plugin_placeholder(r["path"])]
# A Copilot chat mode defines an agent persona and its tool set. The double extension
# identifies the file, as it does a prompt file.
SUBAGENT_DIRS_SUFFIX = paths_for(_PATTERNS, "subagents", "dir_ext:.chatmode.md")
CHATMODE_SUFFIX = ".chatmode.md"
# The suffix that the name of a Copilot agent drops: GitHub's pages name agents "NAME.agent.md".
AGENT_MD_SUFFIX = ".agent.md"

COMMAND_DIRS_TOML = paths_for(_PATTERNS, "commands", "tree_ext:.toml")
COMMAND_TEXT_MODE = "tree_ext:.md|.mdc|.markdown|.txt"
COMMAND_DIRS_TEXT = paths_for(_PATTERNS, "commands", COMMAND_TEXT_MODE)
COMMAND_DIRS_MD = paths_for(_PATTERNS, "commands", "tree_ext:.md")
# Antigravity workflows and Cursor project commands, the Markdown files directly in the folder.
COMMAND_DIRS_MD_FLAT = paths_for(_PATTERNS, "commands", "dir_ext:.md")
# Copilot prompt files are invoked as slash commands, as the files of the directories above
# are, but they need the double extension: "Prompt files are Markdown files with the
# .prompt.md extension". A plain .md there is not a prompt file. See
# https://code.visualstudio.com/docs/copilot/customization/prompt-files
COMMAND_DIRS_PROMPT = paths_for(_PATTERNS, "commands", "dir_ext:.prompt.md")
PROMPT_SUFFIX = ".prompt.md"

# Claude Code and Gemini declare hooks in their settings files under .claude and .gemini, and
# Codex and Cursor in a hooks file of their own. Each of these files wraps the events in a
# "hooks" key, and the content test reads the events in the format of the tool of each row
# that names the file (`_loads_hooks`).
HOOKS_JSON_FILES = paths_for(_PATTERNS, "hooks", "json_hooks")
# A Claude Code plugin's hooks.json, where a mod can name its JavaScript hooks module instead.
HOOKS_MODULE_FILES = paths_for(_PATTERNS, "hooks", "json_hooks_or_modules")
# Antigravity's hooks.json, which has no "hooks" key to test: it names each hook at the top level.
HOOKS_NAMED_FILES = paths_for(_PATTERNS, "hooks", "json_named_hooks")
# Copilot instead keeps one JSON file per hook in a directory.
HOOKS_DIRS_JSON = paths_for(_PATTERNS, "hooks", "tree_ext:.json")
# (directory, pattern) for the files that a plugin finds in a directory by a pattern of their
# name, as hookify reads `.claude/hookify.*.local.md`.
HOOKS_NAME_GLOBS = [(r["path"], r["match_mode"][len("name_glob:"):]) for r in _PATTERNS
                    if r["mechanism"] == "hooks" and r["match_mode"].startswith("name_glob:")]

RULES_DIRS_CODEX = paths_for(_PATTERNS, "rules", "dir_ext:.rules")
# Cursor reads only .mdc files here. A plain .md in .cursor/rules has no front matter to set
# description, globs or alwaysApply, and Cursor's rules system ignores it.
RULES_DIRS_CURSOR = paths_for(_PATTERNS, "rules", "tree_ext:.mdc")
RULES_MARKDOWN_MODE = "tree_ext:.md|.mdc|.markdown"
RULES_DIRS_MARKDOWN = paths_for(_PATTERNS, "rules", RULES_MARKDOWN_MODE)
RULES_CURSOR_EXTENSIONS = extensions_for("tree_ext:.mdc")
RULES_DIRS_CLAUDE = paths_for(_PATTERNS, "rules", "tree_ext:.md")
# Claude Code's rules folders, through which it follows a link to a folder: its memory page says
# that "The `.claude/rules/` directory supports symlinks" and names a rules file "you reach through
# a symlink, whether the file or its directory is the link". No page that the heuristics cite says
# the same of its output styles, the other folders of RULES_DIRS_CLAUDE.
LINKED_RULES_DIRS = [path for path in RULES_DIRS_CLAUDE
                     if posixpath.basename(path) == "rules"]
# Antigravity scans only the Markdown files directly in its rules directory.
RULES_DIRS_MD_FLAT = paths_for(_PATTERNS, "rules", "dir_ext:.md")
# A Copilot CLI plugin's rules: the .mdc files directly in the directory with a description in
# their front matter, which Copilot CLI's loader requires.
RULES_DIRS_DESCRIBED = paths_for(_PATTERNS, "rules", "dir_ext_described:.mdc")
# Antigravity's rules: a `.md` file directly in `.agents/rules` with front matter that sets a
# trigger that Antigravity accepts (`_has_trigger`). Antigravity discards a file without such a
# trigger.
RULES_DIRS_TRIGGERED = paths_for(_PATTERNS, "rules", "dir_ext_triggered:.md")
# A rules file by its name: Antigravity's .agents/rules.json, which registers rule files that
# the directory scan leaves out.
RULES_FILES = paths_for(_PATTERNS, "rules", "file")
# The same file, read for the rule files that it registers (`paths.declared_rule_files`).
RULES_JSON_ROWS = heuristics.rules_json_rows(_PATTERNS)

MCP_ROOT_FILE = one_path(_PATTERNS, "mcp", "root_file")
MCP_FILES = paths_for(_PATTERNS, "mcp", "file")
# JSON files that are MCP configuration when they have "mcpServers": Gemini's settings file,
# and the .mcp.json of a plugin that a repository's Codex marketplace lists.
MCP_PLUGIN_FILES = paths_for(_PATTERNS, "mcp", "json_plugin_mcp")
# The same rows with their tools, because Claude Code loads fewer of the servers of such a file
# than Codex does (`_plugin_mcp_servers`).
MCP_PLUGIN_ROWS = [r for r in _PATTERNS if r["mechanism"] == "mcp" and r["match_mode"] == "json_plugin_mcp"]
MCP_JSON_FILES = paths_for(_PATTERNS, "mcp", "json_mcp_servers")
MCP_CODEX_CONFIG = one_path(_PATTERNS, "mcp", "toml_mcp_servers")
MCP_BASENAMES = paths_for(_PATTERNS, "mcp", "basename_json_mcp_servers")
# The plugin manifests that each vendor documents. A plugin is a bundle of mechanisms. The
# rows that name its skills, commands, subagents, hooks and settings detect them, and the
# plugins mechanism records that the repository contains the bundle. In Claude Code, a plugin
# is a directory of skills, agents or hooks "optionally alongside a .claude-plugin/plugin.json
# manifest". Cursor and Codex document their own manifests and the portable Agent Plugins
# layout. Gemini calls the same bundle an extension and packages prompts, MCP servers,
# commands, themes, hooks, subagents and skills in it.
COPILOT_MARKETPLACES = paths_for(_PATTERNS, "plugins", "json_copilot_marketplace")
# Codex's marketplace, a `file` row of `PLUGIN_FILES` that counts only where Codex can install a
# plugin from it (`paths.codex_marketplace_installs`).
CODEX_MARKETPLACE = paths._CODEX_MARKETPLACE
PLUGIN_FILES = paths_for(_PATTERNS, "plugins", "file")
PLUGIN_BASENAMES = paths_for(_PATTERNS, "plugins", "basename")
# The folders of plugin modules that a tool loads directly from the folder, such as OpenCode's
# plugins and custom tools, where each file is the plugin itself rather than its manifest.
PLUGIN_MODULE_MODE = "dir_ext:.ts|.js"
PLUGIN_MODULE_DIRS = paths_for(_PATTERNS, "plugins", PLUGIN_MODULE_MODE)
# The folders of plugin packages, such as OpenCode 2's, where each child folder with an entry
# module is one plugin. Each directory has its own mode, because OpenCode's terminal interface
# reads a `tui` entry in `plugins` and not in `plugin`.
PLUGIN_PACKAGE_ROWS = [(r["path"], r["match_mode"]) for r in _PATTERNS
                       if r["mechanism"] == "plugins"
                       and r["match_mode"].startswith(heuristics.PACKAGE_ENTRY_MODE)]
PLUGIN_PACKAGE_DIRS = [path for path, _mode in PLUGIN_PACKAGE_ROWS]
# The portable manifest. The standard puts it in the plugin root, where no directory name
# identifies it, and the step tests its content instead. `heuristics.is_agent_plugins_document`
# is the one implementation of that test. The collector, the collected file cleanup and this
# step call it through `paths.agent_plugins_document`.
PLUGIN_MANIFESTS = paths_for(_PATTERNS, "plugins", "basename_json_plugin_manifest")
MCP_AGENT_PLUGINS = paths_for(_PATTERNS, "mcp", "basename_json_agent_plugins_mcp")
HOOKS_TOML_FILES = paths_for(_PATTERNS, "hooks", "toml_hooks")
# The files that a tool reads its language servers from: Copilot CLI's .github/lsp.json, and
# the files that a Copilot or Claude Code plugin contains. A file at one of these paths counts
# whatever its content.
LSP_FILES = paths_for(_PATTERNS, "lsp", "file")
# The places that a tool also reads below the repository root, the rows with `nested:` in their
# match mode. The step reads them where `paths.nested_places` finds them, with the inner mode.
# They are Copilot CLI's `.github/copilot-instructions.md`, `.github/instructions`,
# `.github/agents`, `.github/skills`, `.github/lsp.json` and `.github/mcp.json` of its working
# directory and of each directory between that directory and the root.
NESTED_CONTEXT_FILES = paths_for(_PATTERNS, "context_file", heuristics.NESTED_PREFIX + "file")
NESTED_CONTEXT_DIRS_SUFFIX = paths_for(_PATTERNS, "context_file",
                                       heuristics.NESTED_PREFIX + "tree_suffix:.instructions.md")
NESTED_SUBAGENT_DIRS_DESCRIBED = paths_for(_PATTERNS, "subagents",
                                           heuristics.NESTED_PREFIX + "tree_ext_described:.md")
NESTED_SKILLS_DIRS = paths_for(_PATTERNS, "skills", heuristics.NESTED_PREFIX + "skill_dir")
NESTED_LSP_FILES = paths_for(_PATTERNS, "lsp", heuristics.NESTED_PREFIX + "file")
NESTED_MCP_FILES = paths_for(_PATTERNS, "mcp", heuristics.NESTED_PREFIX + "file")

GITHUB_BASE = "https://github.com"


def github_link(repo_name, branch, path=None):
    """The configuration artifact's page on GitHub, or the repository's at `branch`.

    `repos.url_path` percent-encodes the branch and the path, because a file name can contain
    spaces, `#`, `?` or characters outside ASCII, and a link with them unencoded is not a URL.
    """
    if not branch:
        return ""
    tree = f"{GITHUB_BASE}/{repo_name}/tree/{url_path(branch)}"
    return f"{tree}/{url_path(path)}" if path else tree


def read_validated_csv():
    rows = {}
    fieldnames = None
    with open(VALIDATED_CSV, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        # An empty list file has no header, and its `fieldnames` is None.
        fieldnames = list(reader.fieldnames or [])
        for row in reader:
            rows[row["repo_name"]] = row
    return rows, fieldnames


# The cloning step collects a symlink as a one-line file that contains its target, and a
# symlinked mechanism directory arrives as a small text file rather than a directory. The
# detectors follow these markers, because an unresolved .claude/skills that points at a shared
# directory would read as no skills at all. docs/steps/repository_cloning.md gives the reason
# for the marker.
_MARKER_MAX_BYTES = 4096

# Every mechanism directory that a marker can stand in for, read from the heuristics. A
# hand-written list would hide each directory that it leaves out, such as a symlinked
# `.codex/environments`, `.github/instructions` or `.github/hooks`.
# collected_file_cleanup.MECHANISM_DIRS derives the same list, and the two steps agree on it.
_MARKER_DIRS = directory_paths(_PATTERNS)


# The directory names that a mechanism path can start with, at any depth of the
# tree. `.github` is not among them, because GitHub reads a repository's `.github` at the root
# only. A nested `.github` is a copy rather than configuration of this repository.
_NESTABLE_ROOTS = {r["path"].split("/")[0] for r in _PATTERNS
                   if ("/" in r["path"] or is_directory_mode(r["match_mode"])
                       or r["match_mode"] in (*heuristics.SPEC_DIR_MODES, "unclassified_dir",
                                              "copy_dir"))
                   and not r["path"].startswith(".github")
                   and not heuristics.plugin_placeholder(r["path"])}
# The root's `.github`, below which `_nested_anchors` searches on.
_ROOT_GITHUB = ".github"


@functools.lru_cache(maxsize=4)
def _nested_anchors(repo_path):
    """Return the directories below the root that contain a mechanism directory.

    The tools read configuration from their working directory, not only from the repository
    root. Claude Code loads a `.claude/skills` below the root for sessions started in or under
    its directory, and Codex scans `.agents/skills` from the working directory up to the root.
    A monorepo that configures its packages one by one has real configuration there, and
    detection reads it too.

    The search skips vendored subtrees, because configuration inside one belongs to the
    vendored project. It also skips everything below a mechanism directory, which that
    directory's own heuristics cover. A `.claude` inside a plugin bundle belongs to the
    bundle, and reporting it again under a nestable root would count the same file twice. A
    `_agent` without a place of Antigravity, such as a Python package, is no such directory
    (`paths.is_spec_directory`), and the search continues below it. The root's `.github` is the
    exception too. Its heuristics name the places that Copilot and the workflows read, and the
    collected file cleanup prunes the rest rather than keeping it whole, so they cover no tool
    directory below it. A tool that works in such a directory reads it, as "Codex scans
    .agents/skills in every directory from your current working directory up to the repository
    root" (https://learn.chatgpt.com/docs/build-skills, read 2026-11-05). A `.github` below the
    root stops the search, as every other mechanism directory does, and `paths.nested_places`
    finds the places in it that Copilot CLI reads.

    A tool directory that is a link is a file in the tree, the marker that the collector wrote
    for the link (`paths.TOOL_DIRECTORIES`), and the directory that contains the marker is a
    parent of that tool directory as well. `_resolve_dir` reads the mechanism directories below
    the marker in the directory that it names.

    Returns {directory name: sorted list of repository-relative parents}. With the lists
    sorted, a rerun reports its rows in the order that the heuristics and the sorted paths give.
    """
    found = {}
    for dirpath, dirnames, filenames in os.walk(repo_path):
        rel = os.path.relpath(dirpath, repo_path)
        parts = [] if rel == "." else rel.split(os.sep)
        dirnames[:] = sorted(d for d in dirnames
                             if d != ".git" and d not in paths.DISCARDED_DIRECTORIES
                             and not paths.is_vendored(Path(dirpath) / d,
                                                       heuristics.VENDORED_DIRS))
        # The search lists nothing below a spec directory, and only the last part can be one.
        # The root's `.github` is searched below.
        if (parts and parts != [_ROOT_GITHUB] and parts[-1] in SPEC_DIRS
                and paths.is_spec_directory(Path(dirpath))):
            dirnames[:] = []
            continue
        if not parts:
            continue
        for d in dirnames:
            if d in _NESTABLE_ROOTS:
                found.setdefault(d, []).append("/".join(parts))
        for name in filenames:
            if name in paths.TOOL_DIRECTORIES:
                found.setdefault(name, []).append("/".join(parts))
    return {name: tuple(sorted(set(where))) for name, where in found.items()}


def _directories(repo_path):
    """Every directory of the tree, repository-relative, as the filesystem spells it.

    `_nested_anchors` stops at a mechanism directory and returns fewer directories. The
    plugins detector needs the whole list, because a plugin bundle lies inside a mechanism
    directory, `.claude/plugins/<name>/`. The bundle's manifest belongs to the bundle and is
    not a second reading of a file that another row already claims.
    """
    out = []
    for dirpath, dirnames, _ in os.walk(repo_path):
        dirnames[:] = sorted(d for d in dirnames
                             if d != ".git" and d not in paths.DISCARDED_DIRECTORIES
                             and not paths.is_vendored(Path(dirpath) / d,
                                                       heuristics.VENDORED_DIRS))
        rel = os.path.relpath(dirpath, repo_path)
        if rel != ".":
            out.append(rel.replace(os.sep, "/"))
    return tuple(out)


@functools.lru_cache(maxsize=4)
def _plugin_components(repo_path):
    """Every place of every plugin in the tree that its tool reads (`paths.plugin_components`),
    without the files that a file manager wrote into a plugin's directories (`_listed`)."""
    return tuple(component for component in paths.plugin_components(Path(repo_path))
                 if not paths.is_discarded_path(component[0]))


@functools.lru_cache(maxsize=4)
def _nested_places(repo_path):
    """{row path: [place below the root, ...]} for the `nested:` rows (`paths.nested_places`),
    each list in path order, without the places that a file manager wrote."""
    found = defaultdict(list)
    for rel, heuristic_row in paths.nested_places(Path(repo_path), heuristics.VENDORED_DIRS):
        if not paths.is_discarded_path(rel):
            found[heuristic_row["path"]].append(rel)
    return dict(found)


def _nested(repo_path, patterns):
    """The places below the root of the rows of `patterns`, in the order of the rows and then of
    the paths."""
    places = _nested_places(repo_path)
    return [rel for pattern in patterns for rel in places.get(pattern, ())]


def _declared(repo_path, mechanism):
    """The places that a plugin manifest names for this mechanism, with the match mode that reads
    each, repository-relative."""
    return [(rel, rule["match_mode"]) for rel, rule, kind in _plugin_components(repo_path)
            if kind == "declared" and rule["mechanism"] == mechanism]


def _anchored(repo_path, rel):
    """Yield `rel` at the repository root and under every directory that can contain it.

    A path that starts with a plugin placeholder yields the rest of the path under every root
    of a plugin with that placeholder instead, and nowhere else.
    """
    placeholder = heuristics.plugin_placeholder(rel)
    if placeholder:
        # The default places that the plugin's tool reads: one alternative of a list, and none
        # that a manifest key replaced (`paths.plugin_components`).
        for found, rule, kind in _plugin_components(repo_path):
            if kind == "default" and rule["path"] == rel:
                yield found
        return
    yield rel
    for parent in _nested_anchors(repo_path).get(rel.split("/")[0], ()):
        yield f"{parent}/{rel}"


def _expanded_files(repo_path, patterns):
    """Yield the concrete files that a list of the heuristics' paths names.

    The file form of `_expanded`, for a path with a segment that varies.
    """
    seen = set()
    for pattern in patterns:
        for rel in _anchored(repo_path, pattern):
            for found in ([rel] if "*" not in rel else
                          [str(m.relative_to(repo_path))
                           for m in sorted(_listed(repo_path, repo_path.glob(rel)))
                           if m.is_file()]):
                # Each file once, also where two rows reach it, as in a directory with the
                # manifests of two vendors, which is a root of each placeholder.
                if found not in seen:
                    seen.add(found)
                    yield found


def _expanded(repo_path, patterns):
    """Yield the concrete directories that a list of the heuristics' paths names.

    A path can contain a `*` for a segment with a varying name, and it then yields every
    directory that the segment matches. No row of the current heuristics contains a `*`,
    because the parts of a plugin are under the plugin placeholders. A path without a `*`
    yields itself, and every caller treats both kinds of path the same way.
    """
    seen = set()
    for pattern in patterns:
        for rel in _anchored(repo_path, pattern):
            for found in ([rel] if "*" not in rel else
                          [str(m.relative_to(repo_path))
                           for m in sorted(_listed(repo_path, repo_path.glob(rel)))
                           if m.is_dir()]):
                if found not in seen:
                    seen.add(found)
                    yield found


def _agent_plugins_documents(repo_path, name, match_mode):
    """Every file named `name` with the content of the Agent Plugins document that `match_mode`
    names."""
    found = []
    for p in sorted(_listed(repo_path, _named(repo_path.rglob("*"), name))):
        if not p.is_file():
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if paths.agent_plugins_document(match_mode, text):
            found.append(str(p.relative_to(repo_path)))
    return found


def _real_dir_name(repo_path, rel):
    """Return `rel` with each segment spelled the way the filesystem spells it, or None.

    A case-insensitive volume opens `.github/instructions` for a directory named
    `.github/Instructions`. Read through the heuristics' spelling, everything below it would be
    reported under a path that exists in no tree, with a github_link that returns 404 and
    nothing in the archive at that path. `detect_other` reads the real names and would report
    the same file again under the real path, which gives one configuration artifact two rows.

    Matching each segment explicitly gives the same result on both kinds of volume, as
    `_find_case_insensitive` does for the names of files.
    """
    current = repo_path
    parts = []
    for segment in str(rel).replace("\\", "/").split("/"):
        if not segment:
            continue
        if not current.is_dir():
            return None
        candidates = [e for e in sorted(current.iterdir())
                      if e.name.lower() == segment.lower() and e.is_dir()]
        # The heuristics' own spelling first, as in `_find_case_insensitive`. On a
        # case-sensitive volume, such as the container's, a repository with both
        # `.github/Instructions` and `.github/instructions` would otherwise resolve to whichever
        # sorts first rather than to the directory that the heuristics name.
        match = next((e for e in candidates if e.name == segment),
                     candidates[0] if candidates else None)
        if match is None:
            # Not a directory here. It may still be a marker file, which _resolve_dir handles.
            return None
        parts.append(match.name)
        current = match
    return "/".join(parts) if parts else None


def _resolve_dir(repo_path, rel, max_hops=8):
    """Return the directory at `rel`, following collected symlink markers.

    A marker can point at another marker. .codex/skills, for example, points at
    ../.claude/skills, which points at ../.agents/skills. The function follows the whole chain
    for up to `max_hops` links, stops at a cycle, and checks at every link that the target is
    inside the tree.

    A directory above `rel` can be a marker too, that of a tool directory that is a link, such
    as a `.claude` that leads to `config/claude`, and `.claude/skills` is then the
    `config/claude/skills` below the directory that the marker names (`_below_a_marker`).

    Returns None when `rel` is neither a directory nor a marker with a directory inside the
    collected tree as its target. A target outside the tree, such as `../.agents/skills`,
    cannot be resolved, because the collector never copied it, and its marker remains an
    `other` row.
    """
    root = repo_path.resolve()
    # The directory as the filesystem spells it, because a path reported below it must exist.
    real = _real_dir_name(repo_path, rel)
    if real is None:
        below = _below_a_marker(repo_path, rel)
        if below is not None:
            return _resolve_dir(repo_path, below, max_hops)
    path = repo_path / (real if real is not None else rel)
    seen = set()
    for _ in range(max_hops):
        if path.is_dir():
            return path
        if not path.is_file():
            return None
        try:
            if path.stat().st_size > _MARKER_MAX_BYTES:
                return None
            target_text = paths.marker_target(path.read_text(encoding="utf-8"), path.parent)
        except Exception:
            return None
        if target_text is None:
            return None
        target = (path.parent / target_text).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None
        if target in seen:
            return None
        seen.add(target)
        path = target
    return None


def _below_a_marker(repo_path, rel):
    """`rel` with the part of it that is a marker of a linked directory replaced by the
    directory that the marker names, repository-relative, or None where no directory above the
    end of `rel` is a marker of a directory of the tree.

    The collector stores a tool directory that is a link as a marker and copies the directory
    that it leads to at that directory's path (`paths.collect_symlink_targets`), and the tool
    reads `.claude/settings.json` of a `.claude` that links to `config/claude` as
    `config/claude/settings.json`. A marker that names a directory that contains it, such as
    the repository root, stands for no directory, because the collector copies no such
    target."""
    parts = [part for part in str(rel).replace("\\", "/").split("/") if part]
    for end in range(1, len(parts)):
        head = "/".join(parts[:end])
        if not (repo_path / head).is_file():
            continue
        directory = _resolve_dir(repo_path, head)
        if (directory is None
                or (repo_path / head).parent.resolve().is_relative_to(directory.resolve())):
            return None
        inner = _repo_relative(repo_path, directory).replace(os.sep, "/")
        rest = "/".join(parts[end:])
        return rest if inner == "." else f"{inner}/{rest}"
    return None


def _marker_text(path):
    """The target that the file at `path` names when its text can be a collected symlink
    marker (`paths.marker_target`), or None."""
    try:
        if path.stat().st_size > _MARKER_MAX_BYTES:
            return None
        return paths.marker_target(path.read_text(encoding="utf-8"), path.parent)
    except (OSError, UnicodeDecodeError):
        return None


# The prefix of a file reference. Each of lge-ros2/cloisim's `.claude/commands/*.md` is one line
# such as `@../../.github/skills/<name>/SKILL.md`. Such a line is the file's own text and no
# marker of a link, and the first of the `REF_PATTERNS` reads it as a reference.
_FILE_REFERENCE_PREFIX = "@"


def _unfollowed_marker(path):
    """Whether the file at `path`, which `_followed_file` did not follow, is the marker of a link
    to a target that the tree lacks. Such a marker has a text that `paths.marker_target`
    accepts, contains a `/` and is no file reference (`_FILE_REFERENCE_PREFIX`).

    A text of one word that names no file, such as `hello`, is the file's own text. A link
    target without a `/` would be in the marker's own folder, which the collector copies with
    the marker."""
    text = _marker_text(path)
    return text is not None and paths.link_marker(text)


def _followed_file(repo_path, path, max_hops=8):
    """The file that a collected symlink marker at `path` names, following a chain of markers
    inside the tree, or `path` itself when it contains no such link. A line such as
    `@./AGENTS.md` names nothing in the tree and is the file's own text."""
    root = repo_path.resolve()
    seen = set()
    for _ in range(max_hops):
        text = _marker_text(path)
        if text is None:
            return path
        target = (path.parent / text).resolve()
        if not target.is_file() or not target.is_relative_to(root) or target in seen:
            return path
        seen.add(target)
        path = target
    return path


def measured_artifact(repo_path, mechanism, artifact):
    """The file, relative to the repository, that the columns read from the text of the
    configuration artifact at `artifact` describe: the `language` of this step and the line
    count of the content quality analysis. None where no file of the tree has that text.

    For every mechanism but context files, this is the file that a collected symlink marker at
    `artifact` names inside the repository (`_followed_file`). The tool reads the item through
    its link, and the front matter tests read it the same way (`_file_text`).
    NVIDIA/Model-Optimizer links `.claude/agents/modelopt-model-downloader.md` to
    `../../plugins/modelopt/agents/modelopt-model-downloader.md`, an English agent, and the
    language detector takes the marker's one line, a path, for German. Where the link's target
    is not a file of the tree (`_unfollowed_marker`), the function returns None, because a
    language measured on the marker's one line describes no file. mathlab/pina's skills, for
    example, link to `.opencode/skills`, a directory that the collector does not copy. A
    context file that is a link points at another context file by design. `classify` publishes
    it as a reference with the target in `referenced_file`, and its columns describe the file
    at its own path, the marker, which `artifact_text` also reads for a link that the tree holds
    as a real symlink. The row keeps the link's path in every case."""
    if mechanism == "context_file":
        return artifact
    repo_path = Path(repo_path)
    measured = _followed_file(repo_path, repo_path / artifact)
    if _unfollowed_marker(measured):
        return None
    return _repo_relative(repo_path, measured)


def _named(found, name):
    """The paths of `found`, which a listing returned, with the name `name` in its case.

    A glob with a fixed last segment, such as `Path.rglob(name)` or `glob("*/agent.md")`, tests
    that segment with a stat. On a case-insensitive volume, such as macOS's, the stat succeeds for
    the name in any case, and the glob returns the pattern's spelling, a path that exists in no
    tree. A listing returns the names on disk, and comparing them gives the same paths on such a
    volume as on a case-sensitive one, such as the container's.
    """
    return [p for p in found if p.name == name]


def _file_in_its_case(repo_path, rel):
    """Whether `rel` is a file below `repo_path` with every segment spelled as in `rel`.

    `Path.is_file` on a case-insensitive volume accepts `review.md` for `REVIEW.md`, and a row
    would name a path that exists in no tree. The names that a listing returns give the same
    result on such a volume as on a case-sensitive one, as in `_named`.
    """
    current = Path(repo_path)
    for segment in rel.split("/"):
        if not current.is_dir() or segment not in os.listdir(current):
            return False
        current = current / segment
    return current.is_file()


def _listed(base, found):
    """The paths of `found`, which a glob or a listing below `base` returned, in the order
    given, without the files and directories that a file manager wrote there
    (`paths.is_discarded_path`).

    Every listing of a repository's directory goes through this function or prunes the same
    directories. The collected file cleanup removes these entries from the validated tree.
    Synology DSM, however, writes an `@eaDir` into every directory that File Station visits,
    and Finder writes a `.DS_Store`. A detection that resumed after such a visit would publish
    them as configuration artifacts or unclassified files. A name of
    `paths.DISCARDED_DIRECTORIES` is left out only for a directory, because DSM writes a
    directory.
    """
    return [p for p in found
            if not paths.is_discarded_path(p.relative_to(base).as_posix())
            and not (p.name in paths.DISCARDED_DIRECTORIES and p.is_dir()
                     and not p.is_symlink())]


def _repo_relative(repo_path, path):
    """Return a configuration artifact's path relative to the repository root.

    A followed marker resolves to an absolute path, and the function tries the root both as
    given and resolved. A configuration artifact is reported where the file is, never under
    the link that reached it. A link path exists in no tree, its `github_link` returns 404,
    and the published archive contains nothing at it. The marker credits the linking tool
    instead, in `artifact_tools`.
    """
    try:
        return str(path.relative_to(repo_path))
    except ValueError:
        return str(path.relative_to(repo_path.resolve()))


def resolved_markers(repo_path):
    """Return the symlink markers that resolve to a directory in this repository.

    The markers stand for directories, not configuration artifacts. They produce no row of
    their own but record that a tool reached the configuration artifacts behind them, and the
    step accounts for them rather than report them as `other`.
    """
    found = []
    # Under every nestable root, as `_collect_all_spec_files` reads the same list. A marker
    # below a nestable root resolves for `detect_skills`. If it did not resolve here too,
    # `detect_other` would emit the marker directory itself as a row.
    for pattern in _MARKER_DIRS:
        if pattern in paths.TOOL_DIRECTORIES:
            continue  # `.claude` with `hookify.*.local.md`, read with the tool directories below
        for rel in _anchored(repo_path, pattern):
            if (repo_path / rel).is_file() and _resolve_dir(repo_path, rel) is not None:
                found.append(rel)
    # A tool directory that is a link, such as a `.claude` that leads to `.agents`, through
    # which the detectors read the mechanism directories below it (`_resolve_dir`). A marker
    # that names a directory that contains it stands for no directory, because the collector
    # copies no such target.
    for name in paths.TOOL_DIRECTORIES:
        for rel in _anchored(repo_path, name):
            directory = _resolve_dir(repo_path, rel) if (repo_path / rel).is_file() else None
            if (directory is not None and rel not in found
                    and not (repo_path / rel).parent.resolve().is_relative_to(directory.resolve())):
                found.append(rel)
    # A place below the root that a `nested:` row reads as a folder and that was a link, such as
    # a package's `.github/agents`, which the detectors read through its marker, as the root's.
    for places in _nested_places(repo_path).values():
        for rel in places:
            if (repo_path / rel).is_file() and _resolve_dir(repo_path, rel) is not None \
                    and rel not in found:
                found.append(rel)
    # Plugin placeholders expand to concrete component paths. A followed directory marker
    # belongs to the component's mechanism even when its target is outside the plugin.
    for rel, rule, _kind in _plugin_components(repo_path):
        if (heuristics.is_directory_mode(rule["match_mode"])
                and (repo_path / rel).is_file()
                and _resolve_dir(repo_path, rel) is not None and rel not in found):
            found.append(rel)
    # A skill linked on its own, which `detect_skills` reports at the directory that it names.
    # Left out here, the marker would be an `other` row under the link's path.
    for skills_dir in _expanded(repo_path, SKILLS_DIRS):
        sd = _resolve_dir(repo_path, skills_dir)
        if sd is not None:
            found.extend(marker for marker, _target in _linked_skills(repo_path, sd)
                         if marker not in found)
    # A folder linked on its own into a commands directory or a Claude Code rules directory,
    # which `detect_skills` and `detect_rules` read below the directory that it names.
    for folder in _expanded(repo_path, SKILL_COMMAND_DIRS + LINKED_RULES_DIRS):
        cd = _resolve_dir(repo_path, folder)
        if cd is not None:
            found.extend(marker for marker, _target in _linked_markdown_folders(repo_path, cd)
                         if marker not in found)
    return found


def _find_case_insensitive(repo_path, rel):
    """Return the real relative path of `rel`, ignoring case, or None.

    Path.is_file() follows the host filesystem. On macOS it accepts COPILOT-INSTRUCTIONS.md for
    copilot-instructions.md, and on Linux it does not. Comparing names explicitly gives the
    same result on both and keeps the name that the repository uses.

    Where a case-sensitive volume holds both spellings, the spelling that the heuristics name
    takes precedence. Otherwise a `.claude/Settings.json` without hooks would hide the
    `.claude/settings.json` beside it that contains them, and the detectors that read the file
    that they are given would read the wrong file.

    Where a directory of `rel` is the marker of a linked directory, such as a `.claude` that
    leads to `config/claude`, the file is looked for in the directory that the marker names and
    returned at its own path there, `config/claude/settings.json`, as a linked directory's
    files are reported (`_below_a_marker`).
    """
    parent = repo_path / os.path.dirname(rel)
    if not parent.is_dir():
        below = _below_a_marker(repo_path, rel)
        return None if below is None else _find_case_insensitive(repo_path, below)
    target = os.path.basename(rel)
    matches = [e for e in sorted(os.listdir(parent))
               if e.lower() == target.lower() and (parent / e).is_file()]
    if not matches:
        return None
    entry = target if target in matches else matches[0]
    return os.path.join(os.path.dirname(rel), entry) if os.path.dirname(rel) else entry


def _workflow_has_content(repo_path, rel):
    """Whether the workflow at `rel` has a line that is neither blank nor a comment. A YAML file
    of comments and blank lines alone is an empty document without jobs, and GitHub's workflow
    syntax states that "A workflow is a configurable automated process made up of one or more
    jobs". A workflow name of the heuristics, such as `claude.yml`, therefore names no
    configuration in such a file."""
    return any(line.strip() and not line.lstrip().startswith("#")
               for line in _file_text(repo_path, Path(repo_path) / rel).splitlines())


# The dispatcher of the example workflows of the Gemini CLI action, the one workflow that a
# settings row matches by a Gemini name: "This workflow acts as a central dispatcher for Gemini
# CLI, routing requests to the appropriate workflow based on the triggering event and the command
# provided in the comment" (README.md of google-github-actions/run-gemini-cli at 387c8ddb7a72,
# read 2026-11-08). It counts only where it calls such a workflow (`_dispatches_a_gemini_workflow`).
GEMINI_DISPATCHER = ".github/workflows/gemini-dispatch.yml"
# The prefixes of a reusable workflow of the same repository in `jobs.<job_id>.uses`: "When you
# reference a reusable workflow in the same repository using `$/` or `./` (without
# `{owner}/{repo}` and `@{ref}`), the called workflow is from the same commit as the caller
# workflow" (https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax,
# read 2026-11-08).
_SAME_REPOSITORY_WORKFLOW = ("./", "$/")


def _declares_workflow_call(repo_path, rel):
    """Whether the workflow at `rel` declares the trigger `workflow_call`, which makes it a
    reusable workflow: "Use on.workflow_call to define the inputs and outputs for a reusable
    workflow" (workflow syntax, read 2026-11-08). `on` is a name, a list of names or a mapping of
    names, and PyYAML reads the key `on` as true."""
    parsed, doc = paths.load_yaml(_file_text(repo_path, Path(repo_path) / rel))
    if not parsed or not isinstance(doc, dict):
        return False
    trigger = doc.get("on", doc.get(True))
    if isinstance(trigger, str):
        return trigger == "workflow_call"
    return isinstance(trigger, (list, dict)) and "workflow_call" in trigger


def _dispatches_a_gemini_workflow(repo_path, rel):
    """Whether the dispatcher at `rel` (`GEMINI_DISPATCHER`) has a job that calls a reusable
    workflow of the repository, `jobs.<job_id>.uses: ./.github/workflows/<file>`, that runs Gemini
    (`detect_agent_workflows`) and that declares `workflow_call` (`_declares_workflow_call`), as
    the example workflows of the action that the dispatcher calls declare it. A dispatcher that
    calls no such workflow runs no Gemini, as pantavisor/pantavisor's, which calls a
    `gemini-review.yml` that declares only `workflow_dispatch`, and the file is an `other`
    row."""
    parsed, doc = paths.load_yaml(_file_text(repo_path, Path(repo_path) / rel))
    if not parsed or not isinstance(doc, dict) or not isinstance(doc.get("jobs"), dict):
        return False
    gemini = {path for path, tool in detect_agent_workflows(Path(repo_path)) if tool == "gemini"}
    for job in doc["jobs"].values():
        uses = job.get("uses") if isinstance(job, dict) else None
        if not isinstance(uses, str) or not uses.startswith(_SAME_REPOSITORY_WORKFLOW):
            continue
        called = posixpath.normpath(uses[2:])
        if called in gemini and _declares_workflow_call(repo_path, called):
            return True
    return False


def detect_settings(repo_path):
    found = []
    # A `root_file` row is read at the repository root alone, as Gemini Code Assist reads
    # .gemini/config.yaml, and a `file` row is expanded to every depth.
    for rel in (found_rel for path in SETTINGS_PATHS
                for found_rel in ([path] if path in SETTINGS_ROOT_FILES
                                  else _expanded_files(repo_path, [path]))):
        # The name as spelled on disk. The heuristics' spelling would name a path that exists in
        # no tree, and the detector of unclassified files, which reads real names, would report
        # the same file again under its real name.
        real = _find_case_insensitive(repo_path, rel)
        if real is not None and (not real.startswith(paths.WORKFLOWS_SUBPATH + "/")
                                 or _workflow_has_content(repo_path, real)) and (
                real.lower() != GEMINI_DISPATCHER
                or _dispatches_a_gemini_workflow(repo_path, real)):
            found.append(real)

    for rel in _expanded(repo_path, SETTINGS_DIRS_TOML):
        d = _resolve_dir(repo_path, rel)
        if d is None:
            continue
        for p in sorted(_listed(d, d.rglob("*.toml"))):
            if p.is_file():
                found.append(_repo_relative(repo_path, p))

    for rel in _expanded(repo_path, SETTINGS_DIRS_JSON):
        d = _resolve_dir(repo_path, rel)
        if d is None:
            continue
        for p in sorted(_listed(d, d.glob("*.json"))):
            if p.is_file():
                found.append(_repo_relative(repo_path, p))

    # An extension manifest can be at any depth and is matched by name.
    for name in SETTINGS_BASENAMES:
        for p in sorted(_listed(repo_path, _named(repo_path.rglob("*"), name))):
            if p.is_file():
                found.append(str(p.relative_to(repo_path)))
    # Each file once, because a plugin at the repository root names the same file as a root row.
    return list(dict.fromkeys(found))


# GROUNDING: Cursor's rules page, captured 2025-06-03: "The .cursorrules file in the root of your
# project is still supported" (https://web.archive.org/web/20250603080456/https://docs.cursor.com/context/rules).
# A project is a folder opened in Cursor. It can be a package of a monorepo but is never
# Cursor's own configuration directory, and no tool reads a .cursorrules inside `.cursor`, such
# as .cursor/.cursorrules or .cursor/rules/.cursorrules.
_CURSORRULES = ".cursorrules"


def detect_context_files(repo_path, *, include_declared=True):
    found = []
    # Sorted, as in every other detector. The filesystem's directory order changes when
    # `collected_file_cleanup` rebuilds data/validated/ from scratch, and two runs over the same
    # sample would otherwise write the same rows in different orders.
    for dirpath, dirnames, filenames in os.walk(repo_path):
        dirnames[:] = sorted(d for d in dirnames if d not in paths.DISCARDED_DIRECTORIES)
        for fn in sorted(filenames):
            if fn in CONTEXT_FILENAMES:
                rel = os.path.relpath(os.path.join(dirpath, fn), repo_path)
                if fn == _CURSORRULES and ".cursor" in rel.split(os.sep)[:-1]:
                    continue
                found.append(rel)
            elif fn.lower() in ROOT_ANY_CASE and os.path.relpath(dirpath, repo_path) == ".":
                found.append(fn)

    # A `root_file` row at the root alone and in its case, on every volume.
    for rel in ROOT_CONTEXT_FILES:
        if _file_in_its_case(repo_path, rel):
            found.append(rel)

    # Matched case-insensitively and recorded under the name on disk. A case-sensitive lookup
    # would make detection depend on the host filesystem, with a different row for the same
    # repository on macOS and on Linux. The `file` rows are read under every nestable root, as
    # the settings, hooks and MCP paths are. `.cursor` and `.gemini` are nestable roots, and
    # `.github` is not. Read at the root alone, a nested BUGBOT.md would stay in other.csv
    # without a language, a line count or the AI columns, and with context_file FALSE for the
    # repository. A `root_file` row, such as .gemini/styleguide.md, is read above at the root
    # alone.
    for rel in _expanded_files(repo_path, CONTEXT_FILES):
        # A name that a basename row reads, such as .claude/AGENTS.md, was found above in its
        # exact case, and its `file` row attributes it to the tool. Read in any case here, the
        # row would also match a lowercase .claude/agents.md, such as jdereg/json-io's, a name
        # that no tool reads below the root.
        if os.path.basename(rel) in CONTEXT_FILENAMES:
            continue
        real = _find_case_insensitive(repo_path, rel)
        if real:
            found.append(real)

    for rel in CONTEXT_DIRS_SUFFIX:
        instr_dir = _resolve_dir(repo_path, rel)
        if instr_dir is None:
            continue
        for p in sorted(_listed(instr_dir, instr_dir.rglob("*" + INSTRUCTIONS_SUFFIX))):
            if p.is_file():
                found.append(_repo_relative(repo_path, p))

    # The instructions of a directory below the root that Copilot CLI reads where it works.
    found.extend(_nested(repo_path, NESTED_CONTEXT_FILES))
    for rel in _nested(repo_path, NESTED_CONTEXT_DIRS_SUFFIX):
        # The place can be the marker of a link to a folder, which the collector stores as it
        # stores a root `.github/instructions` that is a link.
        instr_dir = _resolve_dir(repo_path, rel)
        if instr_dir is None:
            continue
        found.extend(_repo_relative(repo_path, p)
                     for p in sorted(_listed(instr_dir, instr_dir.rglob("*" + INSTRUCTIONS_SUFFIX)))
                     if p.is_file())

    # The files that the repository's configuration names, after the files that a name or a
    # path finds. Each file appears once, because an AGENTS.md that a Gemini settings file lists
    # is already in the list.
    seen = set(found)
    for rel, _heuristic_row in (paths.declared_context_files(Path(repo_path), heuristic_rows=CONTEXT_DECLARATION_HEURISTICS)
                       if include_declared else ()):
        if rel not in seen:
            seen.add(rel)
            found.append(rel)

    return found


def _find_skill_files(skills_dir, *, recursive=True):
    """Yield the skill markers at the depth that the directory's heuristic selects.

    The marker is matched case-insensitively, as the context files are. A case-sensitive match
    would give a repository with a skill.md a skill on a case-insensitive filesystem and none
    on a case-sensitive one. That reason holds for the tools that read the marker of each
    folder directly in a skills directory, VS Code by a stat of the joined path and Antigravity
    with `strings.EqualFold`. Below the folder of another skill, only a tool that searches the
    directory at any depth reads a marker, and Codex, Cursor CLI and OpenCode keep only a file
    named `SKILL.md` exactly (`_inside_a_skill`). Such a marker in another case, such as
    gohypergiant/standard-toolkit's `.agents/skills/accelint-skill-manager/references/skill.md`,
    is a reference document of the skill around it.
    """
    target = SKILL_MARKER.lower()
    candidates = _listed(skills_dir,
                         skills_dir.rglob("*") if recursive else skills_dir.glob("*/*"))
    for path in candidates:
        if not path.is_file() or path.name.lower() != target:
            continue
        # A skill is a directory with a SKILL.md. A SKILL.md directly in the skills root belongs
        # to no skill and names nothing.
        if path.parent == skills_dir:
            continue
        if (path.name != SKILL_MARKER
                and _inside_a_skill(path, len(path.relative_to(skills_dir).parts))):
            continue
        yield path


def _inside_a_skill(skill_md, depth):
    """Whether a folder between a skills directory and the folder of `skill_md` contains a skill
    marker in any case, so that `skill_md` lies in the folder of another skill. `depth` is the
    number of segments of the path of `skill_md` below the skills directory, also where a link
    to a directory leads to the file, with the folders below the link. The folder of a skill
    belongs to that skill, with its scripts, references and assets."""
    return any(any(p.is_file() and p.name.lower() == SKILL_MARKER.lower()
                   for p in _listed(folder, folder.iterdir()))
               for folder in list(Path(skill_md).parents)[1:depth - 1] if folder.is_dir())


def _linked_skills(repo_path, skills_dir):
    """Yield (marker, directory) for every skill linked on its own into a skills directory.

    A repository can link single skills as well as the whole directory, such as a
    `.claude/skills/<name>` that points at `../../skills/<name>`. The collector writes the link
    as a one-line marker and copies the directory that it names. The skill is that directory,
    reported at its own path, and the marker records that the tool reaches it. A directory
    without a `SKILL.md` is no skill, and its marker remains an `other` row, as
    `collected_file_cleanup.find_marker_targets` keeps no such directory.
    """
    target_name = SKILL_MARKER.lower()
    for entry in sorted(_listed(skills_dir, skills_dir.iterdir())):
        if not entry.is_file():
            continue
        rel = _repo_relative(repo_path, entry)
        target = _resolve_dir(repo_path, rel)
        if target is not None and any(p.is_file() and p.name.lower() == target_name
                                      for p in _listed(target, target.rglob("*"))):
            yield rel, target


# The tools that skip a SKILL.md that is a link to a file (docs/heuristics.md). Codex skips every
# such link. Cursor CLI keeps a SKILL.md only when its real path is within the real path of the
# skills directory that it lists, and it skips a link with a target outside that directory. Claude
# Code, OpenCode, Gemini CLI, Copilot CLI and Antigravity CLI read a linked SKILL.md.
_SKIPS_EVERY_LINKED_SKILL = frozenset({"codex"})
_SKIPS_A_LINKED_SKILL_OUTSIDE = frozenset({"cursor"})
# The tools that read each skills directory that the heuristics give the tool `any`, because
# several tools read it (docs/heuristics.md). Codex, Copilot in VS Code and Copilot CLI, Cursor,
# Antigravity, which counts as Gemini, and OpenCode read `.agents/skills`, and Claude Code reads
# no `.agents/skills`. The heuristics name these tools only in the notes of the directory's row,
# which the code does not parse.
_SHARED_SKILLS_READERS = {".agents/skills": frozenset({"codex", "copilot", "cursor", "gemini",
                                                       "opencode"})}
# The tool of each tool directory, such as Claude Code of `.claude`, from the heuristics of the
# directories that contain only configuration. A skills directory in it is that tool's, and the
# heuristics of another tool for the same directory, such as Cursor's for `.claude/skills` and
# `.codex/skills`, credit the skills that the directory's own tool does not read
# (`_tools_reading_a_skill`).
_DIRECTORY_TOOLS = {r["path"]: r["tool"] for r in _PATTERNS
                    if r["mechanism"] == "other"
                    and r["match_mode"] in ("spec_dir", "unclassified_dir")
                    and r["tool"] != heuristics.SHARED_TOOL}
# The longest name of a skill that Codex accepts (`MAX_NAME_LEN` in
# codex-rs/skills/src/parser.rs).
_CODEX_MAX_NAME_LENGTH = 64


# GROUNDING: openai/codex at f9c10053fb3d9b960e78cfeae1f3ffa21e0dc00a, read 2026-11-05.
# codex-rs/ext/skills/src/loader/discovery.rs keeps a file of a skills directory only when
# `entry.path.basename().as_deref() == Some(SKILLS_FILENAME)`, with `SKILLS_FILENAME: &str =
# "SKILL.md"` in loader/mod.rs, and compares the name that the directory listing returns.
# codex-rs/skills/src/parser.rs, parse_skill_frontmatter_metadata:
# `extract_frontmatter(contents).ok_or(SkillParseError::MissingFrontmatter)?`, where
# extract_frontmatter requires `line.trim() == "---"` of the first line and of a later line, with
# a line between them, then `if description.is_empty() { return
# Err(SkillParseError::MissingField("description")); }`, and validate_len refuses a name of more
# than MAX_NAME_LEN characters. The file system reads the file with `String::from_utf8(bytes)`
# (codex-rs/file-system/src/lib.rs), which keeps a byte order mark, and `trim` does not remove
# U+FEFF. A skill that does not parse is a SkillError, and Codex loads no skill from it
# (loader/host.rs).
def _codex_skill_loads(repo_path, skill_md):
    """Whether Codex loads the SKILL.md at `skill_md`: a file named `SKILL.md` in capitals and
    encoded in UTF-8, which opens with a front matter block with a first line of `---` and no
    byte order mark, with a non-empty `description` string and a `name` of at most
    `_CODEX_MAX_NAME_LENGTH` characters where it sets one. A file that is a link is read as the
    file that its marker names, and `_linked_skill_skippers` records that Codex skips a link. The
    test reads lines rather than parse YAML, as `paths.has_string` does, and a front matter that
    YAML rejects elsewhere still passes it."""
    if Path(skill_md).name != SKILL_MARKER:
        return False
    try:
        text = _followed_file(Path(repo_path), Path(skill_md)).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    lines = [line.removesuffix("\r") for line in text.split("\n")]
    if lines[0].strip() != "---":
        return False
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None or end == 1:
        return False
    block = "\n".join(["---", *lines[1:end], "---"])
    if not paths.has_string(block, "description"):
        return False
    found = paths.front_matter(block)
    names = paths.top_level_keys(*found, "name") if found else []
    if len(names) == 1:
        name = re.sub(r"(^|\s)#.*$", "", names[0][1]).strip()
        if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
            name = name[1:-1]
        if len(" ".join(name.split())) > _CODEX_MAX_NAME_LENGTH:
            return False
    return True


# GROUNDING: Cursor CLI 2026.09.28-64d2043
# (https://downloads.cursor.com/lab/2026.09.28-64d2043/linux/x64/agent-cli-package.tar.gz), read
# 2026-11-07. parseSkillMarkdown in dist-package/1623.index.js, which reads a skill of a skills
# directory and, through loadPluginSkills, a skill of a plugin, parses the file with
# `(0,u.be)(t)` and drops the skill on an error:
# "catch(e){return(0,m.cY)(`Failed to parse skill frontmatter for ${s}:`,e),null}". The module
# ../utils/dist/gray-matter.js in dist-package/index.js defines `be` as gray-matter 4.0.3 with the
# engines js and javascript replaced by `()=>({})`, and gray-matter's yaml engine is the safeLoad
# of js-yaml 3.14.1, which the package bundles. For the Cursor IDE, Cursor's staff states "If
# that frontmatter isn't valid YAML, the request fails with a generic server error" and names "An
# unquoted description that contains a colon" among the causes
# (https://forum.cursor.com/t/error-connecting-to-server-sending-prompts-with-skills-attached/168440,
# 2026-08-14, Cursor 3.14.27, read 2026-11-07).
def _cursor_skill_loads(repo_path, skill_md):
    """Whether Cursor parses the front matter of the SKILL.md at `skill_md` as gray-matter 4.0.3
    splits it off: after a byte order mark, a first line that starts with `---`, up to the first
    later line that starts with `---` or to the end of the file. A file without such a first
    line has no front matter and loads. gray-matter reads a word after the first `---` as the
    language of the front matter, and the test parses YAML only, for `yaml` and `yml` in any
    case or no word. It keeps a file with another word. gray-matter refuses such a word unless
    it is `json`, or `js` or `javascript` in any case, and Cursor CLI then drops the file as
    well, but Cursor's staff states the failure of the IDE only for a front matter that is not
    valid YAML. A first line that starts with `----` opens no front matter in gray-matter, and
    the test reads the rest of that line as a word and keeps the file too. A file that is a
    link is read as the file that its marker names (`_file_text`)."""
    text = _file_text(repo_path, skill_md).removeprefix("\ufeff")
    if not text.startswith("---"):
        return True
    rest = text[3:]
    newline = re.search(r"\r?\n", rest)
    # gray-matter takes `str.slice(0, str.search(/\r?\n/))`, all but the last character where
    # no line break follows.
    first = rest[:newline.start()] if newline else rest[:-1]
    language = first.strip()
    if language.lower() not in ("", "yaml", "yml"):
        return True
    if language:
        rest = rest[len(first):]
    end = rest.find("\n---")
    return paths.yaml_parses(rest if end == -1 else rest[:end])


def _linked_skill_skippers(repo_path, skill_md, sd):
    """The tools that skip `skill_md`, a SKILL.md below `sd`, the directory that a skills
    directory resolves to, because the file is a link to a file: Codex, and Cursor where the
    link leads outside `sd` or to a file that the tree lacks. Empty for a SKILL.md that is no
    link."""
    followed = _followed_file(repo_path, skill_md)
    if followed == skill_md and not _unfollowed_marker(skill_md):
        return frozenset()
    within = followed != skill_md and followed.resolve().is_relative_to(sd.resolve())
    return _SKIPS_EVERY_LINKED_SKILL | (frozenset() if within else _SKIPS_A_LINKED_SKILL_OUTSIDE)


def _links_outside_its_directory(marker, directory):
    """Whether the link at `marker`, a path of the repository, is a skill folder linked on its
    own into a `skills` directory and leads to `directory` outside that directory, as a folder of
    `.cursor/skills` that links to `.agents/skills` does. Cursor CLI scans a project skills
    directory by its real path and does not follow a folder link with a real path outside it
    (`_SKIPS_A_LINKED_SKILL_OUTSIDE`). A link that is the skills directory itself, such as
    `.cursor/skills` to `.claude/skills`, names the directory that Cursor scans, and the folders
    below it lie inside it."""
    parent = os.path.dirname(marker)
    return (os.path.basename(parent) == "skills"
            and not os.path.normpath(directory).startswith(parent + "/"))


def _cursor_reads_at_its_own_path(repo_path, path):
    """Whether Cursor reads the SKILL.md at `path`, a path of the repository, where it is: a
    skills heuristic of Cursor reads `path`, or the heuristic of a shared skills directory that
    Cursor reads, such as `.agents/skills` (`_SHARED_SKILLS_READERS`), and Cursor does not skip
    the file (`_skill_skippers`), as it skips a front matter that does not parse. A skill folder
    in `.cursor/skills` that links to such a skill outside `.cursor/skills` therefore keeps
    Cursor, which reads the skill without following the link."""
    repo_path = Path(repo_path)
    directory, row = _skills_directory(path)
    if directory is None:
        return False
    tools = tools_reading("skills", path, repo_path / path, repo_path)[1]
    readers = tools if tools else _SHARED_SKILLS_READERS.get(row["path"], frozenset())
    if "cursor" not in readers:
        return False
    return "cursor" not in _skill_skippers(repo_path, repo_path / path,
                                           _resolve_dir(repo_path, directory), readers,
                                           _owner_of(row))


def _skill_skippers(repo_path, skill_md, sd, tools, owner):
    """The tools of `tools`, the tools with a skills heuristic that reads `skill_md`, that skip
    it: the tools that skip a link to a file where `skill_md` is one below `sd`, the directory
    that its skills directory resolves to (`_linked_skill_skippers`), Codex where it does not
    load the file (`_codex_skill_loads`), Cursor where its front matter does not parse
    (`_cursor_skill_loads`), and for a marker in another case than `SKILL.md`, each
    tool other than `owner`, the tool of the tool directory that contains the skills directory.
    Such a tool reads the directory for compatibility, and a credit for it needs the code that
    shows that it reads the file: Cursor CLI keeps a file only when `"SKILL.md"===e.name`
    (findSkillMarkdownFiles in dist-package/1623.index.js of Cursor CLI 2026.09.28-64d2043), and
    OpenCode compares `child.name === "SKILL.md"` (packages/core/src/config/plugin/skill-scan.ts
    at 510aa79d). Cursor in its own `.cursor/skills` keeps the marker in any case, because the
    loader of the Cursor IDE was not read, and so does each tool of a directory without an
    owner, such as Antigravity, which compares the name with `strings.EqualFold`."""
    skipping = set(_linked_skill_skippers(repo_path, skill_md, sd)) if sd is not None else set()
    if "codex" in tools and not _codex_skill_loads(repo_path, skill_md):
        skipping.add("codex")
    if "cursor" in tools and not _cursor_skill_loads(repo_path, skill_md):
        skipping.add("cursor")
    if owner is not None and Path(skill_md).name != SKILL_MARKER:
        skipping |= set(tools) - {owner}
    return skipping


def _owner_of(row):
    """The tool of the tool directory that contains the directory of the skills heuristic `row`,
    or None for a directory that several tools share, such as `.agents/skills`."""
    return None if row is None else _DIRECTORY_TOOLS.get(row["path"].split("/")[0])


def _skipped_skill(repo_path, skills_dir, sd, skill_md):
    """Whether each tool that reads the skills directory `skills_dir` skips `skill_md`, a
    SKILL.md in `sd`, the directory that `skills_dir` resolves to (`_skill_skippers`).

    The collector writes a link as a marker and copies the file that it names. A tool that
    skips the link reads no skill there, and the marker is a file of the tool's configuration
    directory that no mechanism covers, an `other` row. NotePlan/plugins links
    `.cursor/skills/nplog/SKILL.md` to `agents/skills/nplog/SKILL.md`, outside `.cursor/skills`.
    A skills directory that a tool with no such limit reads, such as `.claude/skills`, keeps the
    skill, and so does `.agents/skills`, which Copilot CLI, Antigravity CLI and OpenCode read
    with the link (`_tools_reading_a_skill`). So does a SKILL.md of a skill folder that is a
    link, which lies outside `sd`. A SKILL.md of `.codex/skills` that Codex does not load is no
    `other` row, because Cursor reads `.codex/skills` and loads it, as it loads probelabs/probe's
    `test_data/skills/.codex/skills/no-desc/SKILL.md`, which has no description.
    """
    try:
        inner = skill_md.relative_to(sd).as_posix()
    except ValueError:
        return False
    read_path = f"{skills_dir}/{inner}"
    _explained, tools = tools_reading("skills", read_path, skill_md, repo_path)
    if not tools:
        return False
    return tools <= _skill_skippers(repo_path, skill_md, sd, tools,
                                    _owner_of(_skills_directory(read_path)[1]))


def _linked_markdown_folders(repo_path, folder):
    """Yield (marker, directory) for every folder linked on its own into a Claude Code commands
    or rules directory, such as GovTechSG/sgds-web-component's
    `.claude/commands/sgds-components`, a link to `../../skills/sgds-components`, or
    opendatahub-io/opendatahub-operator's `.claude/rules/.rules`, a link to `../../.rules`.

    Claude Code lists the Markdown files of a commands directory with ripgrep's `--follow` and
    reads every `.md` below such a link as a command, and it reads the rules below a linked
    folder of its rules directory (`LINKED_RULES_DIRS`). The collector writes the link as a
    one-line marker and copies the directory that it names. The commands and the rules are
    reported at their own paths, and the marker records that the tool reaches them. A directory
    without a `.md` file holds neither, and its marker remains an `other` row, as
    `collected_file_cleanup.find_marker_targets` keeps no such directory.
    """
    for entry in sorted(_listed(folder, folder.iterdir())):
        if not entry.is_file():
            continue
        rel = _repo_relative(repo_path, entry)
        target = _resolve_dir(repo_path, rel)
        if target is not None and any(p.is_file() for p in _listed(target, target.rglob("*.md"))):
            yield rel, target


REGISTRATION_ROWS = [r for r in _PATTERNS if r["match_mode"].startswith("registered:")]


def _registered(repo_path, mechanism):
    rows = [r for r in REGISTRATION_ROWS if r["mechanism"] == mechanism]
    return [rel for rel, _ in paths.registered_configuration_files(
        Path(repo_path), heuristic_rows=rows, include_registering_files=True)]


def _left_out(repo_path):
    """{file: default place} for each item of an Antigravity default place that a top-level
    `exclude` of a registration file leaves out (`paths.antigravity_left_out`). A detector
    skips such a file in that place and reads it in any other place, such as a Claude Code
    directory that links to it. In a customization root, the file is then an `other` row unless
    another tool reads it."""
    return paths.antigravity_left_out(Path(repo_path))


def _folder_name(repo_path, folder):
    """The name of a skill's folder, and for the repository root the repository's name rather
    than the `owner§repo` directory that holds it."""
    return folder.name if folder != repo_path else repos.dir_name_to_repo(repo_path.name).rsplit("/", 1)[-1]


# The skills folders that Gemini CLI reads, its own and those of its extensions.
_GEMINI_CLI_SKILL_DIRS = [r["path"] for r in _PATTERNS
                          if r["tool"] == "gemini" and r["mechanism"] == "skills"
                          and r["path"] in (".gemini/skills", "<gemini-extension>/skills")]
# The skills folders that Antigravity reads and no other tool's heuristic names: those of the
# customization roots `.agent`, `_agents` and `_agent` and of its plugins. `.agents/skills` is
# shared with the other tools.
_ANTIGRAVITY_SKILL_DIRS = [path for path in SKILLS_DIRS
                           if path not in _GEMINI_CLI_SKILL_DIRS
                           and all(r["tool"] == "gemini" for r in _PATTERNS
                                   if r["mechanism"] == "skills" and r["path"] == path)]
# The skills folders that Codex reads and no other tool's heuristic names: those of its plugins.
# Cursor reads `.codex/skills` too (the heuristics of that directory).
_CODEX_SKILL_DIRS = [path for path in SKILLS_DIRS
                     if all(r["tool"] == "codex" for r in _PATTERNS
                            if r["mechanism"] == "skills" and r["path"] == path)]
# The skills folders that Cursor reads and no other tool's heuristic names: `.cursor/skills` and
# those of its plugins.
_CURSOR_SKILL_DIRS = [path for path in SKILLS_DIRS
                      if all(r["tool"] == "cursor" for r in _PATTERNS
                             if r["mechanism"] == "skills" and r["path"] == path)]


# GROUNDING: Antigravity CLI 1.4.10 (linux_arm64,
# https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-arm/cli_linux_arm64.tar.gz),
# read 2026-11-05. The changelog that the binary ships says for 1.4.1 that `agy plugin validate`
# "errors on a `SKILL.md` with missing or invalid frontmatter and warns when `name` or
# `description` is missing", and for 1.4.6 that it "reports every `SKILL.md` the plugin loader
# skips, including skills listed through `plugin.json` or `skills.json`". The binary's messages
# include "no frontmatter: the file must start with a --- block holding name and description" and
# "%s. The loader skips this skill". The skills page states "Every `SKILL.md` file begins with YAML
# frontmatter" (https://antigravity.google/docs/skills, read 2026-11-05).
def _antigravity_skill_loads(repo_path, skill_md):
    """Whether Antigravity loads the SKILL.md at `skill_md`: whether the file, or the file that
    a collected symlink marker names, opens with a front matter block, a `---` line after an
    optional byte order mark and a later `---` line. A front matter without `name` or
    `description` loads with a warning, and the test does not read the keys."""
    lines = _file_text(repo_path, skill_md).removeprefix("\ufeff").splitlines()
    return bool(lines) and lines[0].rstrip() == "---" and any(line.rstrip() == "---"
                                                              for line in lines[1:])


def _skills_named_by(repo_path, placeholder):
    """The skill folders and the root SKILL.md files that the manifest of a plugin of the kind
    `placeholder`, such as `<antigravity-plugin>`, names and the manifest of no other kind of
    plugin names (`paths.plugin_components`)."""
    named = defaultdict(set)
    for rel, rule, kind in _plugin_components(repo_path):
        if rule["mechanism"] == "skills" and kind in ("declared", "root_skill"):
            named[rel].add(heuristics.plugin_placeholder(rule["path"]))
    return {rel for rel, placeholders in named.items() if placeholders == {placeholder}}


def _beyond_the_flat_depth(sd, skill_md):
    """Whether `skill_md`, found below `sd`, the directory that a skills directory resolves to,
    lies deeper than the folders directly in it, which is the depth of a `skill_dir_flat`
    heuristic."""
    return skill_md.is_relative_to(sd) and len(skill_md.relative_to(sd).parts) > 2


def detect_skills(repo_path, *, include_registered=True):
    # The skills folders of the directories below the root that Copilot CLI reads where it
    # works, searched as the root's `.github/skills` is.
    nested_dirs = _nested(repo_path, NESTED_SKILLS_DIRS)
    recursive_dirs = set(_expanded(repo_path, RECURSIVE_SKILLS_DIRS)) | set(nested_dirs)
    left_out = _left_out(repo_path)
    rows = []
    # A directory reachable through more than one link yields the same file more than once.
    # The file is one configuration artifact and gets one row.
    seen = set()
    # Gemini CLI loads a skill only from a SKILL.md with YAML front matter that has a `name` and a
    # `description` (loadSkillFromFile in packages/core/src/skills/skillLoader.ts of
    # google-gemini/gemini-cli at 718e97c2448e0384cb582409cf4e479437bf50fe). In a skills folder
    # that only Gemini CLI reads, a SKILL.md without them, such as GoogleCloudPlatform/
    # k8s-config-connector's mockgcp/.gemini/skills/fix-diffs-mockgcp/SKILL.md, is no skill.
    gemini_cli_dirs = (set(_expanded(repo_path, _GEMINI_CLI_SKILL_DIRS))
                       - set(_expanded(repo_path, [d for d in SKILLS_DIRS if d not in _GEMINI_CLI_SKILL_DIRS])))
    # Antigravity skips a SKILL.md without front matter (`_antigravity_skill_loads`). In a
    # skills folder that only Antigravity reads, such as themeum/tutor's `.agent/skills`, such a
    # file is no skill.
    antigravity_dirs = (set(_expanded(repo_path, _ANTIGRAVITY_SKILL_DIRS))
                        - set(_expanded(repo_path, [d for d in SKILLS_DIRS
                                                    if d not in _ANTIGRAVITY_SKILL_DIRS])))
    # Codex skips a SKILL.md that its parser rejects (`_codex_skill_loads`). In a skills folder
    # that only Codex reads, a Codex plugin's `skills/`, such a file is no skill.
    codex_dirs = (set(_expanded(repo_path, _CODEX_SKILL_DIRS))
                  - set(_expanded(repo_path, [d for d in SKILLS_DIRS
                                              if d not in _CODEX_SKILL_DIRS])))
    # Cursor skips a SKILL.md with a front matter that does not parse (`_cursor_skill_loads`). In
    # a skills folder that only Cursor reads, such as cabloy/cabloy's `.cursor/skills`, such a
    # file is no skill.
    cursor_dirs = (set(_expanded(repo_path, _CURSOR_SKILL_DIRS))
                   - set(_expanded(repo_path, [d for d in SKILLS_DIRS
                                               if d not in _CURSOR_SKILL_DIRS])))
    # A directory that a `skill_dir_flat` heuristic and a `skill_dir` heuristic both read, such as
    # `.claude/skills`, which Claude Code reads one skill folder deep and Cursor and OpenCode at
    # any depth. A SKILL.md deeper than the flat heuristic reads is a skill of the tools that
    # search the directory, which keep only a file named `SKILL.md` exactly, and only outside the
    # folder of another skill, which holds that skill's files.
    flat_dirs = set(_expanded(repo_path, FLAT_SKILLS_DIRS))
    for skills_dir in [*_expanded(repo_path, SKILLS_DIRS), *nested_dirs]:
        sd = _resolve_dir(repo_path, skills_dir)
        if sd is None:
            continue
        recursive = skills_dir in recursive_dirs
        # A linked skill of a Claude Code skills directory has its SKILL.md directly in the
        # linked folder. Other vendors keep the search depth that their own heuristic selects.
        linked = [skill_md for _marker, target in _linked_skills(repo_path, sd)
                  for skill_md in [*_listed(target, target.glob("*")),
                                   *(_find_skill_files(target)
                                     if recursive and skills_dir not in flat_dirs else [])]
                  if skill_md.is_file() and skill_md.name.lower() == SKILL_MARKER.lower()]
        for skill_md in [*sorted(_find_skill_files(sd, recursive=recursive)), *linked]:
            child = skill_md.parent
            name = child.name
            rel = _repo_relative(repo_path, skill_md)
            if (rel in seen or left_out.get(rel) == skills_dir
                    or skills_dir in flat_dirs and _beyond_the_flat_depth(sd, skill_md)
                    and (skill_md.name != SKILL_MARKER
                         or _inside_a_skill(skill_md, len(skill_md.relative_to(sd).parts)))
                    or _skipped_skill(repo_path, skills_dir, sd, skill_md)
                    or skills_dir in gemini_cli_dirs and not _has_name_and_description(repo_path, skill_md)
                    or skills_dir in antigravity_dirs and not _antigravity_skill_loads(repo_path, skill_md)
                    or skills_dir in codex_dirs and not _codex_skill_loads(repo_path, skill_md)
                    or skills_dir in cursor_dirs and not _cursor_skill_loads(repo_path, skill_md)):
                continue
            seen.add(rel)
            scripts = (child / SKILL_SUPPORT_DIRS[0]).is_dir()
            references = (child / SKILL_SUPPORT_DIRS[1]).is_dir()
            assets = (child / SKILL_SUPPORT_DIRS[2]).is_dir()
            rows.append({
                "skills.md": rel,
                "name": name,
                "scripts": str(scripts).upper(),
                "references": str(references).upper(),
                "assets": str(assets).upper(),
            })

    # A command file is a skill without a directory. It has no supporting files, because skills
    # added the directory that holds them. A saved workflow is the same form with another
    # extension. Claude Code reads the commands below a folder that a commands directory links
    # to as well (`_linked_markdown_folders`). Of a folder below the commands directory with a
    # SKILL.md, it reads that file, as the command named for the folder, and no other `.md` file
    # directly in the folder, and the folder holds the command's supporting files.
    for dirs, extensions in ((SKILL_COMMAND_DIRS, (".md",)),
                             (SKILL_WORKFLOW_DIRS, SKILL_WORKFLOW_EXTENSIONS)):
        for cmd_dir in _expanded(repo_path, dirs):
            cd = _resolve_dir(repo_path, cmd_dir)
            if cd is None:
                continue
            bases = [cd]
            if dirs is SKILL_COMMAND_DIRS:
                bases.extend(target for _marker, target in _linked_markdown_folders(repo_path, cd))
            files = [p for base in bases
                     for p in sorted(_listed(base, (q for ext in extensions
                                                    for q in base.rglob(f"*{ext}"))))
                     if p.is_file()]
            skill_folders = {p.parent for p in files
                             if p.name.lower() == SKILL_MARKER.lower() and p.parent != cd}
            for p in files:
                folder = p.parent if p.parent in skill_folders else None
                if folder is not None and p.name.lower() != SKILL_MARKER.lower():
                    continue
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "skills.md": rel,
                    "name": p.stem if folder is None else folder.name,
                    "scripts": str(folder is not None
                                   and (folder / SKILL_SUPPORT_DIRS[0]).is_dir()).upper(),
                    "references": str(folder is not None
                                      and (folder / SKILL_SUPPORT_DIRS[1]).is_dir()).upper(),
                    "assets": str(folder is not None
                                  and (folder / SKILL_SUPPORT_DIRS[2]).is_dir()).upper(),
                })

    # OpenCode 2's other form of a skill, named after the file: "Markdown files must be at the
    # source root, such as skills/review.md." and "Each source can contain either form"
    # (https://opencode.ai/docs/skills/). Its scan leaves out a hidden entry and keeps no such
    # file in a folder with a file named `SKILL.md` exactly (`scan` of skill-scan.ts). It reads a
    # file without a description too, and "skills without one are not advertised", so that a
    # README.md or a list of credits in the folder is no skill.
    for skills_dir in _expanded(repo_path, SKILL_DIRS_MD_FLAT_DESCRIBED):
        sd = _resolve_dir(repo_path, skills_dir)
        files = [] if sd is None else sorted(p for p in _listed(sd, sd.glob("*.md")) if p.is_file())
        if any(p.name == SKILL_MARKER for p in files):
            continue
        for p in files:
            rel = _repo_relative(repo_path, p)
            if not p.name.startswith(".") and rel not in seen and _has_description(repo_path, p):
                seen.add(rel)
                rows.append({"skills.md": rel, "name": p.stem, "scripts": "FALSE",
                             "references": "FALSE", "assets": "FALSE"})

    # What a plugin manifest names beyond the default places: a skill folder, or a command
    # or workflow file. A skill that only an Antigravity plugin's manifest names needs front
    # matter, one that only a Codex plugin's manifest names has to pass Codex's parser, and one
    # that only a Cursor plugin's manifest names needs a front matter that Cursor parses, as a
    # skill of the default place does.
    antigravity_named = _skills_named_by(repo_path, "<antigravity-plugin>")
    codex_named = _skills_named_by(repo_path, "<codex-plugin>")
    cursor_named = _skills_named_by(repo_path, "<cursor-plugin>")
    for rel, mode in _declared(repo_path, "skills"):
        path = repo_path / rel
        if mode in ("skill_dir", "skill_dir_flat"):
            skill_md = next((p for p in sorted(_listed(path, path.iterdir())) if p.is_file()
                             and p.name.lower() == SKILL_MARKER.lower()), None) \
                if path.is_dir() else None
            if (skill_md is None or _repo_relative(repo_path, skill_md) in seen
                    or rel in antigravity_named and not _antigravity_skill_loads(repo_path, skill_md)
                    or rel in codex_named and not _codex_skill_loads(repo_path, skill_md)
                    or rel in cursor_named and not _cursor_skill_loads(repo_path, skill_md)):
                continue
            seen.add(_repo_relative(repo_path, skill_md))
            rows.append({
                "skills.md": _repo_relative(repo_path, skill_md),
                "name": path.name,
                "scripts": str((path / SKILL_SUPPORT_DIRS[0]).is_dir()).upper(),
                "references": str((path / SKILL_SUPPORT_DIRS[1]).is_dir()).upper(),
                "assets": str((path / SKILL_SUPPORT_DIRS[2]).is_dir()).upper(),
            })
        elif path.is_file() and rel not in seen:
            seen.add(rel)
            rows.append({
                "skills.md": rel,
                "name": path.stem,
                "scripts": "FALSE",
                "references": "FALSE",
                "assets": "FALSE",
            })

    # A plugin's root skill, named for its folder (`paths.plugin_components`): the SKILL.md at
    # the root of a plugin that has no `skills/` directory and no `skills` key, the root SKILL.md
    # that a manifest names with `.`, or a skill in the plugin root or a folder above it that an
    # Antigravity plugin's manifest lists. A root skill of a Cursor plugin alone needs a front
    # matter that Cursor parses.
    for rel, _rule, kind in _plugin_components(repo_path):
        if (kind != "root_skill" or rel in seen
                or rel in antigravity_named and not _antigravity_skill_loads(repo_path, repo_path / rel)
                or rel in cursor_named and not _cursor_skill_loads(repo_path, repo_path / rel)):
            continue
        seen.add(rel)
        folder = (repo_path / rel).parent
        rows.append({
            "skills.md": rel,
            "name": _folder_name(repo_path, folder),
            "scripts": str((folder / SKILL_SUPPORT_DIRS[0]).is_dir()).upper(),
            "references": str((folder / SKILL_SUPPORT_DIRS[1]).is_dir()).upper(),
            "assets": str((folder / SKILL_SUPPORT_DIRS[2]).is_dir()).upper(),
        })
    # A skill that an Antigravity registration selects, named for its folder as well, and the
    # registration files. Antigravity skips a registered SKILL.md without front matter, such as
    # those of GoogleCloudPlatform/cloud-solutions' projects/legacy-detox-demo/skills.
    for rel in _registered(repo_path, "skills") if include_registered else ():
        is_skill = Path(rel).name.lower() == SKILL_MARKER.lower()
        if rel not in seen and not (is_skill and not _antigravity_skill_loads(repo_path, repo_path / rel)):
            seen.add(rel)
            folder = (repo_path / rel).parent
            rows.append({"skills.md": rel,
                         "name": _folder_name(repo_path, folder) if is_skill else Path(rel).stem,
                         **{name: str(is_skill and (folder / name).is_dir()).upper()
                            for name in SKILL_SUPPORT_DIRS}})

    command_names = paths.plugin_command_names(Path(repo_path), heuristics.VENDORED_DIRS)
    for row in rows:
        if row["skills.md"] in command_names:
            row["name"] = command_names[row["skills.md"]]
    return rows


def _memory_paths(agents_dir, name):
    """Return the memory directories of a subagent, beside the subagent's own directory.

    `agents_dir` is where the subagent was found. A `.claude/agents` below the root takes the
    `.claude/agent-memory` beside it rather than the one at the repository root.
    """
    agents_dir = agents_dir.replace(os.sep, "/")
    prefix = ""
    for pattern in AGENT_MEMORY_PATTERNS:
        head = pattern.split("/")[0]
        marker = f"/{head}/"
        if marker in f"/{agents_dir}/":
            prefix = f"/{agents_dir}/".split(marker)[0].lstrip("/")
            break
    return [f"{prefix}/{p}".lstrip("/") for p in
            (pattern.replace("*", name) for pattern in AGENT_MEMORY_PATTERNS)]


def _memory_files(repo_path, memory_dir):
    """The files of one subagent memory directory, relative to the repository, sorted."""
    d = repo_path / memory_dir
    if not d.is_dir():
        return []
    return sorted(_repo_relative(repo_path, p) for p in _listed(d, d.rglob("*"))
                  if p.is_file())


def _declared_subagents(repo_path) -> tuple[list, set]:
    """The files that configuration names as subagents, as `[(path, heuristics row)]`, and the
    files that it names as parts of a subagent (`paths.declared_subagent_files`).

    A declaring row with a file at its `path`, such as `declared_path:agents.*.config_file` of
    Codex's `.codex/config.toml`, names a role file, which is a subagent. A declaring row with a
    directory at its `path` (`ROLE_DIRECTORIES`), such as `declared_path:model_instructions_file`
    of `.codex/agents`, names the instructions of a role, which are a part of the role and no row
    of their own, as the prompt file of an OpenCode agent is a part of the agent
    (`paths.opencode_prompt_files`). The cloning step copies both kinds of file, and the
    collected file cleanup keeps both. A file that both kinds of row name is a role file."""
    declared = paths.declared_subagent_files(Path(repo_path),
                                             heuristic_rows=SUBAGENT_DECLARATION_HEURISTICS)
    roles = [(rel, heuristic_row) for rel, heuristic_row in declared
             if heuristic_row["path"] not in ROLE_DIRECTORIES]
    named = {rel for rel, _heuristic_row in roles}
    return roles, {rel for rel, _heuristic_row in declared} - named


def detect_subagents(repo_path, *, include_registered=True):
    copilot_parts = [rel for rel, rule, _ in _plugin_components(repo_path)
                     if rule["tool"] == "copilot" and rule["mechanism"] == "subagents"]

    def plugin_readme(path):
        rel = _repo_relative(repo_path, path)
        return (_has_description(repo_path, path)
                and any(rel == part or rel.startswith(part + "/") for part in copilot_parts))

    left_out = _left_out(repo_path)
    rows = []
    seen = set()
    # The agents folders that Copilot CLI reads at any depth: the root's, and the agents folders
    # of the directories below the root that it reads where it works.
    described_dirs = (set(_expanded(repo_path, SUBAGENT_DIRS_DESCRIBED))
                      | set(_nested(repo_path, NESTED_SUBAGENT_DIRS_DESCRIBED)))
    gemini_flat_dirs = {directory for r in _PATTERNS
                        if r["tool"] == "gemini" and r["mechanism"] == "subagents"
                        and r["match_mode"] in ("dir_ext:.md", "dir_ext_described:.md")
                        for directory in _expanded(repo_path, [r["path"]])}
    # Gemini CLI skips a name that starts with an underscore and an agent with `tools` that it
    # refuses. In an agents folder that a Claude Code, Copilot or Cursor plugin shares with a
    # Gemini CLI extension, the plugin's tool loads the file regardless.
    gemini_only_dirs = gemini_flat_dirs - {directory for r in _PATTERNS
                                           if r["tool"] != "gemini" and r["mechanism"] == "subagents"
                                           for directory in _expanded(repo_path, [r["path"]])}
    flat_described_dirs = set(_expanded(repo_path, SUBAGENT_DIRS_MD_FLAT_DESCRIBED))
    cursor_dirs = {directory: extensions_for(r["match_mode"]) for r in CURSOR_PROJECT_AGENT_ROWS
                   for directory in _expanded(repo_path, [r["path"]])}
    # OpenCode reads every Markdown file of its modes folder as a mode, a README.md among them
    # ("{mode,modes}/*.md").
    readme_dirs = gemini_flat_dirs | {directory for r in _PATTERNS
                                      if r["tool"] == "opencode" and r["mechanism"] == "subagents"
                                      and r["match_mode"] == "dir_ext:.md"
                                      for directory in _expanded(repo_path, [r["path"]])}
    # OpenCode's agents and modes folders. A Markdown agent there named after a built-in agent
    # adjusts that agent and defines none, as an entry of the configuration with such a name does
    # (`_OPENCODE_BUILT_IN_AGENTS`).
    opencode_dirs = {directory for r in _PATTERNS
                     if r["tool"] == "opencode" and r["mechanism"] == "subagents"
                     and r["match_mode"] in ("tree_ext:.md", "dir_ext:.md")
                     for directory in _expanded(repo_path, [r["path"]])}
    for agents_dir, pattern, recursive in (
            [(d, "*.md", True) for d in _expanded(repo_path, SUBAGENT_DIRS_MD)]
            + [(d, "*" + ext, True) for d in _expanded(repo_path, SUBAGENT_DIRS_MARKDOWN)
               for ext in extensions_for(SUBAGENT_MARKDOWN_MODE)]
            + [(d, "*.md", False) for d in _expanded(repo_path, SUBAGENT_DIRS_MD_FLAT)]
            + [(d, "*.md", False) for d in sorted(flat_described_dirs)]
            + [(d, "*.toml", True) for d in _expanded(repo_path, SUBAGENT_DIRS_TOML)]
            + [(d, "*" + CHATMODE_SUFFIX, False)
               for d in _expanded(repo_path, SUBAGENT_DIRS_SUFFIX)]
            + [(d, "*.md", True) for d in sorted(described_dirs)]):
        ad = _resolve_dir(repo_path, agents_dir)
        if ad is None:
            continue
        for p in sorted(_listed(ad, ad.rglob(pattern) if recursive else ad.glob(pattern))):
            if not p.is_file() or (not recursive and agents_dir not in readme_dirs
                                   and p.name.lower() == "readme.md"
                                   and not plugin_readme(p)):
                continue
            if agents_dir in gemini_only_dirs and (p.name.startswith("_")
                                                   or not _gemini_agent_tools_load(repo_path, p)):
                continue
            if recursive and agents_dir in described_dirs and not _has_description(repo_path, p):
                continue
            if not recursive and agents_dir in flat_described_dirs and not _has_description(repo_path, p):
                continue
            if (agents_dir in opencode_dirs and _opencode_markdown_agent_name(repo_path, p, ad)
                    in _OPENCODE_BUILT_IN_AGENTS):
                continue
            # Cursor reads `.claude/agents` for compatibility with Claude Code, so a file that Claude
            # Code does not load counts where it has the front matter of a subagent of Cursor.
            claude_dir = agents_dir.replace(os.sep, "/").split("/")[-2:] == [".claude", "agents"]
            claude_loads = claude_dir and p.suffix == ".md" and _claude_agent_loads(repo_path, p)
            if (not claude_loads and agents_dir in cursor_dirs
                    and p.name.lower().endswith(cursor_dirs[agents_dir])
                    and not _cursor_agent_has_front_matter(repo_path, p)):
                continue
            # A chat mode's name is the file name without the whole double extension:
            # planner.chatmode.md is the agent "planner". An agent file is named the same way.
            name = next((p.name[: -len(suffix)] for suffix in (CHATMODE_SUFFIX, AGENT_MD_SUFFIX)
                         if p.name.endswith(suffix)), p.stem)
            rel = _repo_relative(repo_path, p)
            if rel in seen or left_out.get(rel) == agents_dir:
                continue
            seen.add(rel)
            memory = "n/a"
            # Claude Code is the tool with a memory per subagent. Its agents directory can be at
            # any depth, and the test compares the last two segments rather than the whole
            # path, because apps/api/.claude/agents has its own memory beside it. A file there that
            # only Cursor loads has no such memory.
            if claude_loads:
                memory = str(any(_memory_files(repo_path, mem)
                                 for mem in _memory_paths(agents_dir, name))).upper()
            rows.append({
                "subagent": rel,
                "name": name,
                "memory": memory,
            })
    for directory in _expanded(repo_path, SUBAGENT_DIRS_NAMED):
        folder = _resolve_dir(repo_path, directory)
        if folder is None:
            continue
        for file in sorted(_listed(folder, [*folder.glob("*.md"),
                                            *(p for p in folder.glob("*/*")
                                              if heuristics.is_folder_agent_file(p.name))])):
            rel = _repo_relative(repo_path, file)
            if (file.is_file() and rel not in seen and left_out.get(rel) != directory
                    and _has_name_and_description(repo_path, file)):
                seen.add(rel)
                name = file.parent.name if file.parent != folder else file.stem
                rows.append({"subagent": rel, "name": name, "memory": "n/a"})
    # An agent file that a plugin manifest names beyond its agents/ directory. A folder that the
    # manifest names is read at the default depth. A Copilot plugin's agents can include a
    # README.md.
    for rel, mode in _declared(repo_path, "subagents"):
        if rel in seen or not (repo_path / rel).is_file():
            continue
        if (mode.startswith("dir_ext:") and Path(rel).name.lower() == "readme.md"
                and not plugin_readme(repo_path / rel)):
            continue
        seen.add(rel)
        name = next((Path(rel).name[:-len(suffix)] for suffix in (CHATMODE_SUFFIX, AGENT_MD_SUFFIX)
                     if rel.endswith(suffix)), Path(rel).stem)
        rows.append({"subagent": rel, "name": name, "memory": "n/a"})
    for rel in _registered(repo_path, "subagents") if include_registered else ():
        if rel not in seen:
            seen.add(rel)
            rows.append({'subagent': rel, "name": Path(rel).stem, "memory": "n/a"})
    # The files that configuration names as subagents, such as a role file that Codex's
    # `.codex/config.toml` names, which the declaring row's tool reads (`declared_file_tools`).
    # The instructions that a role names are a part of the role and no row of their own.
    for rel, _heuristic_row in (_declared_subagents(repo_path)[0] if include_registered else ()):
        if rel not in seen:
            seen.add(rel)
            rows.append({"subagent": rel, "name": Path(rel).stem, "memory": "n/a"})
    # OpenCode's configuration, which defines agents under a key: one row per file, because no
    # mechanism CSV repeats a path of a repository, named after the file.
    for rel in dict.fromkeys(_json_key_files(repo_path, "subagents")):
        if rel not in seen and _opencode_defines_an_agent(repo_path, rel):
            seen.add(rel)
            rows.append({"subagent": rel, "name": Path(rel).stem, "memory": "n/a"})

    return rows


def detect_commands(repo_path, *, include_registered=True):
    rows = []
    seen = set()
    for cmd_dir in _expanded(repo_path, COMMAND_DIRS_TOML):
        cd = _resolve_dir(repo_path, cmd_dir)
        if cd is None:
            continue
        # Gemini namespaces commands by subdirectory, and .gemini/commands/git/fix.toml is the
        # command /git:fix. The detector searches the subdirectories too.
        for p in sorted(_listed(cd, cd.rglob("*.toml"))):
            if p.is_file():
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "command": rel,
                    "name": p.stem,
                })
    for cmd_dir, extension in ([(d, ".md") for d in _expanded(repo_path, COMMAND_DIRS_MD)]
                               + [(d, ext) for d in _expanded(repo_path, COMMAND_DIRS_TEXT)
                                  for ext in extensions_for(COMMAND_TEXT_MODE)]):
        cd = _resolve_dir(repo_path, cmd_dir)
        if cd is None:
            continue
        for p in sorted(_listed(cd, cd.rglob("*" + extension))):
            if p.is_file():
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "command": rel,
                    "name": p.stem,
                })
    left_out = _left_out(repo_path)
    for cmd_dir in _expanded(repo_path, COMMAND_DIRS_MD_FLAT):
        cd = _resolve_dir(repo_path, cmd_dir)
        if cd is None:
            continue
        for p in sorted(_listed(cd, cd.glob("*.md"))):
            if p.is_file():
                rel = _repo_relative(repo_path, p)
                if rel in seen or left_out.get(rel) == cmd_dir:
                    continue
                seen.add(rel)
                rows.append({
                    "command": rel,
                    "name": p.stem,
                })
    for cmd_dir in _expanded(repo_path, COMMAND_DIRS_PROMPT):
        cd = _resolve_dir(repo_path, cmd_dir)
        if cd is None:
            continue
        for p in sorted(_listed(cd, cd.glob("*" + PROMPT_SUFFIX))):
            if p.is_file():
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "command": rel,
                    # Without the whole double extension: lint-fix.prompt.md is the command
                    # "lint-fix" that the user types.
                    "name": p.name[: -len(PROMPT_SUFFIX)],
                })
    # A command file that a Copilot CLI plugin's manifest names instead of its commands/ folder.
    for rel, _mode in _declared(repo_path, "commands"):
        if rel in seen or not (repo_path / rel).is_file():
            continue
        seen.add(rel)
        rows.append({"command": rel, "name": Path(rel).stem})
    for rel in _registered(repo_path, "commands") if include_registered else ():
        if rel not in seen:
            seen.add(rel)
            rows.append({'command': rel, "name": Path(rel).stem})
    # OpenCode's configuration, which defines commands under a key: one row per file, because no
    # mechanism CSV repeats a path of a repository, named after the file.
    for rel in dict.fromkeys(_json_key_files(repo_path, "commands")):
        if rel not in seen:
            seen.add(rel)
            rows.append({"command": rel, "name": Path(rel).stem})

    return rows


# The paths that their tool reads with comments stripped, from the heuristics'
# `content_format` column. A file read more permissively than its tool reads it would record a
# configuration that the tool never applies, and the format is therefore set per file rather
# than for every JSON file. Claude Code documents its settings file as strict JSON, and Gemini
# CLI reads its own with `JSON.parse(stripJsonComments(content))`.
JSONC_PATHS = heuristics.jsonc_paths(load_config_path_patterns())
# Each path of a `jsonc_trailing` row, with whether only the file at the root is read with
# comments and trailing commas: the root `.mcp.json` is VS Code's, and the `.mcp.json` of a
# plugin or of a directory below the root is read as strict JSON.
JSON_TRAILING_PATHS = {}
for _row in _PATTERNS:
    if _row["content_format"] == "jsonc_trailing":
        JSON_TRAILING_PATHS[_row["path"]] = (JSON_TRAILING_PATHS.get(_row["path"], True)
                                             and _row["match_mode"] == "root_file")


def _format_for(rel, repo_path=None) -> str:
    """`jsonc` when the heuristics record that this path is read with comments stripped.

    The path is matched at a segment boundary, not by equality. `.gemini` is a nestable root,
    and `_expanded_files` passes this function `sub/dir/.gemini/settings.json` as well as
    `.gemini/settings.json`, which are the same file to the same tool. A path of the heuristics
    without a separator is a basename row and matches at any depth.
    """
    text = str(rel).replace("\\", "/")
    for declared, root_only in JSON_TRAILING_PATHS.items():
        if declared.startswith("<"):
            if repo_path is not None and text in _expanded_files(repo_path, [declared]):
                return "jsonc_trailing"
        elif text == declared or (not root_only and text.endswith("/" + declared)):
            return "jsonc_trailing"
    for declared in JSONC_PATHS:
        if "/" in declared:
            if text == declared or text.endswith("/" + declared):
                return "jsonc"
        elif text.rsplit("/", 1)[-1] == declared:
            return "jsonc"
    return ""


def _load_json(file_path, rel=None):
    """The file's content, read the way the tool that owns it reads it."""
    with open(file_path, encoding="utf-8") as f:
        root = Path(file_path).parents[len(Path(rel).parts) - 1] if rel is not None else None
        return jsonc.loads(f.read(), _format_for(rel if rel is not None else file_path, root))


def _file_text(repo_path, file_path):
    """The text of a file with front matter that a detector tests, or "" where the file cannot
    be read. The tests themselves are in `paths`. The cloning step and the collected file
    cleanup read registered items with the same tests, and the three steps select the same
    items.

    A collected symlink marker is read as the file that it names inside the repository
    (`_followed_file`), as the tool reads a rule or an agent through its link.
    angular/angular's `.agent/rules/agents.md`, for example, links to `AGENTS.md`, which sets
    `trigger: always_on`, while the marker's one line sets no trigger. The row keeps the link's
    path, the file in the tool's folder."""
    try:
        return _followed_file(Path(repo_path), Path(file_path)).read_text(encoding="utf-8",
                                                                          errors="replace")
    except OSError:
        return ""


def _has_trigger(repo_path, file_path):
    """Whether a rule file's YAML front matter sets a trigger that Antigravity accepts, which it
    requires of every rule that it reads (`paths.has_trigger`)."""
    return paths.has_trigger(_file_text(repo_path, file_path))


def _has_description(repo_path, file_path):
    """Whether a rule file opens with a YAML front matter with a non-empty string as `description`.

    Copilot CLI loads a plugin's rule only then. Its loader reports "description: Required" for
    a front matter without the key, "Expected string" for a number, a boolean, null, a list or a
    mapping, and "missing or malformed YAML frontmatter" for a file that does not open with
    `---` or does not close its front matter. The test reads lines rather than parse YAML, and
    a front matter that is malformed elsewhere still counts.
    """
    return paths.has_string(_file_text(repo_path, file_path), "description")


def _has_name_and_description(repo_path, file_path):
    """Whether an Antigravity agent file's YAML front matter has a non-empty `name` and a
    non-empty `description`, without which Antigravity does not load the agent
    (`paths.has_name_and_description`)."""
    return paths.has_name_and_description(_file_text(repo_path, file_path))


# GROUNDING: Claude Code's subagents page, read 2026-10-15
# (https://code.claude.com/docs/en/sub-agents, "Subagent files Claude Code skips"): "Claude Code
# skips a file in a project, user, or managed `agents` directory ... when the frontmatter has any
# of these problems": "No `name`: Claude Code treats the file as documentation kept beside your
# agents", "A `name` that starts with `-`, contains `:`, or is longer than 256 characters", "A
# `name` but no `description`" and "YAML that doesn't parse". A plugin's agents are not among
# those directories. Without this test every .md of .claude/agents would count as a subagent,
# Checkmk/checkmk's README.md among them.
def _claude_agent_loads(repo_path, file_path):
    """Whether Claude Code loads a file of a project's .claude/agents as a subagent: a front
    matter with a `name` that it accepts and a `description`."""
    text = _file_text(repo_path, file_path)
    if not paths.has_name_and_description(text):
        return False
    front, indent = paths.front_matter(text)
    (_, value), = paths.top_level_keys(front, indent, "name")
    name = re.sub(r"(^|\s)#.*$", "", value).strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
        name = name[1:-1]
    return not name.startswith("-") and ":" not in name and len(name) <= 256


# GROUNDING: Cursor CLI 2026.09.28-64d2043, index.js of
# https://downloads.cursor.com/lab/2026.09.28-64d2043/linux/x64/agent-cli-package.tar.gz, read
# 2026-11-05. The loader of the project's subagents in .cursor/agents, .claude/agents and
# .grok/agents runs "const n=Hs(await(0,Ct.yR)(r));if(!n)continue;", and the parser begins
# "function Hs(e){const t=e.match(/^---\s*\n([\s\S]*?)\n---\s*\n?([\s\S]*)$/m);if(!t)return
# null;const r=t[1]??"",n=(t[2]??"").trim();if(0===n.length)return null;". Cursor's subagents
# page: "Each subagent is a markdown file with YAML frontmatter". The pattern is a JavaScript
# one, so `\s` and `trim` take JavaScript's whitespace, and `^` and `$` under the flag `m` take
# its line terminators. The reader `Ct.yR`, the export `yR:()=>v` of the module
# ../utils/dist/encoding.js, removes a byte order mark before the parser:
# "g=function(e){return e.startsWith("\ufeff")?e.slice(1):e}(c)". Cursor's staff on the parser's
# reading, in a thread on nested Markdown files in .cursor/agents: "any markdown file with YAML
# frontmatter or a pair of --- separators gets detected as a separate sub-agent." and "You
# specifically need a pair of `---` (two horizontal rules) or a real frontmatter block."
# (https://forum.cursor.com/t/nested-markdown-files-inside-cursor-agents-are-incorrectly-detected-as-subagents/166296)
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_JS_SPACE_CHARACTERS = ("\t\n\v\f\r \u00a0\u1680" + "".join(map(chr, range(0x2000, 0x200b)))
                        + "\u2028\u2029\u202f\u205f\u3000\ufeff")
_JS_LINE_END = "\n\r\u2028\u2029"
_CURSOR_AGENT_FRONT_MATTER = re.compile(
    rf"(?:\A|(?<=[{_JS_LINE_END}]))---[{_JS_SPACE}]*\n([\s\S]*?)\n---[{_JS_SPACE}]*\n?"
    rf"([\s\S]*)(?:\Z|(?=[{_JS_LINE_END}]))")
_HTML_COMMENT = re.compile(r"<!--[\s\S]*?-->")


def _cursor_agent_has_front_matter(repo_path, file_path):
    """Whether the file has the front matter of a subagent of Cursor: a line `---`, a later line
    that starts with `---` and text after it that is not blank, with only blank lines or HTML
    comments before the first `---` line. The test reads the text as the reader of Cursor's
    loader returns it, without a byte order mark.

    The first three conditions are those of the parser of Cursor's loader. The last one is
    narrower than the parser, whose pattern is multiline and also accepts two `---` horizontal
    rules below the top of a Markdown document. Cursor's page requires YAML front matter, so that
    such a document is no subagent although Cursor lists it."""
    text = _file_text(repo_path, file_path).removeprefix("\ufeff")
    found = _CURSOR_AGENT_FRONT_MATTER.search(text)
    return (found is not None and found.group(2).strip(_JS_SPACE_CHARACTERS) != ""
            and _HTML_COMMENT.sub("", text[:found.start()]).strip(_JS_SPACE_CHARACTERS) == "")


# GROUNDING: google-gemini/gemini-cli at 718e97c2448e0384cb582409cf4e479437bf50fe,
# packages/core/src/agents/agentLoader.ts: the `tools` of a local agent are
# "z.array(z.string().refine((val: string) => isValidToolName(val, { allowWildcards: true })"
# and `.optional()`, and `loadAgentsFromDirectory` records a file that fails the schema as an
# error and loads no agent from it. packages/core/src/tools/tool-names.ts: `isValidToolName`
# accepts a name of `ALL_BUILTIN_TOOL_NAMES`, a key of `TOOL_LEGACY_ALIASES`, a name that starts
# with `discovered_tool_`, `*`, and an MCP name `mcp_<server>_<tool>` (`parseMcpToolName` in
# mcp-tool.ts) with both parts matching `/^[a-z0-9_.:-]+$/i`, a tool that is not underscores
# alone, or `*` for the tool or for the whole name after `mcp_`. The built-in names are those of
# definitions/base-declarations.ts and the `tracker_*` and `invoke_agent` names of
# tool-names.ts, unchanged on the main branch on 2026-11-04.
_GEMINI_CLI_TOOL_NAMES = frozenset({
    "glob", "write_todos", "write_file", "google_web_search", "web_fetch", "replace",
    "run_shell_command", "grep_search", "read_many_files", "read_file", "list_directory",
    "activate_skill", "ask_user", "tracker_create_task", "tracker_update_task",
    "tracker_get_task", "tracker_list_tasks", "tracker_add_dependency", "tracker_visualize",
    "get_internal_docs", "enter_plan_mode", "exit_plan_mode", "update_topic", "complete_task",
    "invoke_agent", "read_mcp_resource", "list_mcp_resources",
    # TOOL_LEGACY_ALIASES
    "search_file_content",
})
_GEMINI_CLI_TOOL_SLUG = re.compile(r"[a-z0-9_.:-]+", re.IGNORECASE)


def _gemini_cli_accepts_tool(name):
    """Whether Gemini CLI's `isValidToolName` with wildcards accepts `name` in the `tools` of an
    agent."""
    if name in _GEMINI_CLI_TOOL_NAMES or name.startswith("discovered_tool_") or name == "*":
        return True
    if not name.startswith("mcp_"):
        return False
    rest = name[len("mcp_"):]
    if rest == "*":
        return True
    parts = re.fullmatch(r"([^_]+)_(.+)", rest)
    if parts is None or not _GEMINI_CLI_TOOL_SLUG.fullmatch(parts.group(1)):
        return False
    tool = parts.group(2)
    return tool == "*" or (not re.fullmatch(r"_*", tool)
                           and bool(_GEMINI_CLI_TOOL_SLUG.fullmatch(tool)))


def _yaml_scalar(text):
    """A YAML scalar of a list item without a trailing comment and without its quotes."""
    text = re.sub(r"(^|\s)#.*$", "", text).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    return text


def _gemini_agent_tools_load(repo_path, file_path):
    """Whether the `tools` of the YAML front matter of a Gemini CLI agent pass the agent schema:
    absent, or a list in which Gemini CLI accepts every name (`_gemini_cli_accepts_tool`). A
    `tools` that is a string, such as Claude Code's `tools: Read, Grep`, or that sets nothing
    fails, because the schema takes a list or no key. The test reads lines, a block list of
    `- name` items or a flow list `[a, b]`, and does not parse YAML."""
    found = paths.front_matter(_file_text(repo_path, file_path))
    if found is None:
        return True  # `_has_description` decides a file without front matter
    front, indent = found
    keys = paths.top_level_keys(front, indent, "tools")
    if not keys:
        return True
    if len(keys) > 1:
        return False  # a duplicate key, which js-yaml's `load` refuses
    i, value = keys[0]
    value = re.sub(r"(^|\s)#.*$", "", value).strip()
    below = []  # the value's own lines: deeper than the key, or `- ` items at its indent
    for line in front[i + 1:]:
        depth = len(line) - len(line.lstrip())
        if depth < indent or depth == indent and not line.lstrip().startswith("-"):
            break
        below.append(line.strip())
    if value.startswith("["):
        flow = " ".join([value, *below]).strip()
        if not flow.endswith("]"):
            return False
        inner = flow[1:-1].strip()
        items = [_yaml_scalar(item) for item in inner.split(",")] if inner else []
    elif value:
        return False
    elif below and all(line.startswith("-") for line in below):
        items = [_yaml_scalar(line[1:]) for line in below]
    else:
        return False
    return all(_gemini_cli_accepts_tool(item) for item in items)


# The tools that read a hook in three levels of event, matcher group and handler
# (`paths.has_a_grouped_hook`). Cursor and Copilot list the handlers under the event, and
# Copilot CLI also reads Claude Code's matcher groups there.
_GROUPED_HOOK_TOOLS = frozenset({"claude", "codex", "gemini"})
# The events of Gemini CLI, the values of its `HookEventName`. It skips any other name:
# `if (!this.isValidEventName(eventName)) { ... Invalid hook event name: ... Skipping.`
# (packages/core/src/hooks/hookRegistry.ts of google-gemini/gemini-cli at f4df88a70d2f, read
# 2026-11-06).
_GEMINI_HOOK_EVENTS = frozenset({"BeforeTool", "AfterTool", "BeforeAgent", "Notification",
                                 "AfterAgent", "SessionStart", "SessionEnd", "PreCompress",
                                 "BeforeModel", "AfterModel", "BeforeToolSelection"})
# The top-level keys of a Codex hooks file. Codex parses `.codex/hooks.json` and a plugin's
# hooks file as `#[serde(deny_unknown_fields)] pub struct HooksFile { description, hooks }`
# (codex-rs/config/src/hook_config.rs at be765f1a), and a plugin's file with another key fails
# with "failed to parse plugin hooks config" (core-plugins/src/loader.rs).
_CODEX_HOOKS_FILE_KEYS = frozenset({"description", "hooks"})


def _loads_hooks(document, tool):
    """Whether `tool` loads a hook from the JSON document of a hooks or settings file: a
    non-empty "hooks" key, and, where the key maps events to their hooks, an event with a hook.

    Claude Code, Codex and Gemini CLI need a matcher group with a handler
    (`paths.has_a_grouped_hook`). `{"PostToolUse": []}` in wp-media/wp-rocket's
    `.claude/settings.json` names an event and no hook, itbench-hub/ITBench's
    `"UserPromptSubmit": {"command": ...}` puts an object where Claude Code expects a list, and
    nevalang/neva's `.codex/hooks.json` lists a handler where Codex expects a matcher group.
    Codex also refuses a file with a top-level key other than `description` and `hooks`, as
    razorpay/blade's plugin hooks file has `_comment`. Gemini CLI reads only its own events
    (`_GEMINI_HOOK_EVENTS`), and `PreToolUse` in GoogleCloudPlatform/k8s-config-connector's
    `.gemini/settings.json` configures no hook. For Cursor and Copilot, an event with any entry
    counts."""
    if not isinstance(document, dict):
        return False
    if tool == "codex" and not document.keys() <= _CODEX_HOOKS_FILE_KEYS:
        return False
    hooks = document.get("hooks")
    if tool == "gemini" and isinstance(hooks, dict):
        hooks = {event: groups for event, groups in hooks.items() if event in _GEMINI_HOOK_EVENTS}
    if tool in _GROUPED_HOOK_TOOLS:
        return paths.has_a_grouped_hook(hooks)
    if isinstance(hooks, dict):
        return any(hooks.values())
    return bool(hooks)


def _has_hooks(file_path, rel=None, *, tools):
    """Whether one of `tools` loads a hook from the JSON file (`_loads_hooks`), read the way the
    tool that owns `rel` reads it."""
    try:
        document = _load_json(file_path, rel)
    except Exception:
        return False
    return any(_loads_hooks(document, tool) for tool in tools)


# The tools of the hooks heuristics, by their path.
_HOOKS_TOOLS = defaultdict(set)
for _row in _PATTERNS:
    if _row["mechanism"] == "hooks":
        _HOOKS_TOOLS[_row["path"]].add(_row["tool"])


def _hooks_places(repo_path, patterns, expand):
    """{place: tools} for the places that the hooks heuristics' `patterns` name, files or
    directories as `expand` yields them, in the order of the patterns, with the tools of every
    pattern that names each place. A folder with the manifests of a Claude Code and a Codex
    plugin is a root of both placeholders, and its `hooks/hooks.json` is a place of both
    tools."""
    readers = {}
    for pattern in patterns:
        for rel in expand(repo_path, [pattern]):
            readers.setdefault(rel, set()).update(_HOOKS_TOOLS[pattern])
    return readers


def _has_hook_modules(file_path):
    """Whether a mod's hooks.json names its hooks module: a non-empty "modules" list."""
    try:
        modules = _load_json(file_path).get("modules")
    except Exception:
        return False
    return isinstance(modules, list) and bool(modules)


# GROUNDING: Antigravity CLI 1.3.5 (linux_amd64, from the installer's manifest on 2026-10-14),
# run offline over trusted workspaces, each with one .agents/hooks.json, and its log
# (hooks_manager.go). The CLI drops a file that it cannot decode whole and logs "failed to parse
# hooks.json at %s" and then "loaded 0 named hooks from 1 hooks.json file(s)". It decodes Go's
# way into a map[string]jsonhook.JSONHookSpec, which requires the following: strict JSON with no
# comment, trailing comma or byte order mark; each value an object or null; `enabled` a boolean
# ("cannot unmarshal string into Go struct field .lint.enabled of type bool"); PreToolUse and
# PostToolUse lists of groups with a string `matcher` and a list `hooks` of handlers;
# SessionStart, PreInvocation, PostInvocation and Stop lists of handlers; a handler's `type`
# "command" or absent ("unsupported hook type: \"http\""), `command` a string and `timeout` a
# whole number that fits 32 bits. Keys match fields in any case, `ENABLED` too, and unknown keys
# are ignored. The Cursor format without its "version" key therefore loads as one hook named
# "hooks", while "version": 1 drops the file. The hooks page gives the format:
# https://antigravity.google/docs/hooks
_NAMED_HOOK_GROUPED_EVENTS = ("pretooluse", "posttooluse")
_NAMED_HOOK_EVENTS = ("sessionstart", "preinvocation", "postinvocation", "stop")


def _loads_as_named_hooks(repo_path, file_path):
    """Whether Antigravity loads this hooks.json, which maps each hook's name to its events.

    A collected symlink marker is read as the file that it names inside the repository
    (`_followed_file`), as the front matter tests read a rule or an agent (`_file_text`)."""
    def handler(item):
        if item is None:
            return True
        if not isinstance(item, dict):
            return False
        fields = {key.lower(): value for key, value in item.items()}
        timeout = fields.get("timeout")
        return (fields.get("type") in (None, "", "command")
                and isinstance(fields.get("command", ""), (str, type(None)))
                and (timeout is None or (isinstance(timeout, int) and not isinstance(timeout, bool)
                                         and -2 ** 31 <= timeout < 2 ** 31)))

    def handlers(value):
        return value is None or isinstance(value, list) and all(handler(item) for item in value)

    def group(item):
        if item is None:
            return True
        if not isinstance(item, dict):
            return False
        fields = {key.lower(): value for key, value in item.items()}
        return (isinstance(fields.get("matcher", ""), (str, type(None)))
                and handlers(fields.get("hooks")))

    def reject(constant):
        raise ValueError(constant)

    try:
        text = _followed_file(Path(repo_path), Path(file_path)).read_text(encoding="utf-8")
        data = json.loads(text, parse_constant=reject)
    except (OSError, ValueError, RecursionError):
        return False
    if not isinstance(data, dict) or not data:
        return False  # an empty object loads, with no hook in it
    for spec in data.values():
        if spec is None:
            continue
        if not isinstance(spec, dict):
            return False
        for key, value in spec.items():
            field = key.lower()
            if field == "enabled" and value is not None and not isinstance(value, bool):
                return False
            if field in _NAMED_HOOK_GROUPED_EVENTS and not (
                    value is None or isinstance(value, list) and all(group(item) for item in value)):
                return False
            if field in _NAMED_HOOK_EVENTS and not handlers(value):
                return False
    return True


def _has_toml_hooks(file_path):
    """Whether a Codex config.toml declares a hook inline: an event of its `hooks` table with a
    matcher group with a handler (`paths.has_a_grouped_hook`), as `[[hooks.PreToolUse]]` with
    `[[hooks.PreToolUse.hooks]]`.

    Codex reads the table as `HooksToml`, the events flattened beside `state`, and `state` maps
    each hook defined elsewhere to its trust state, `enabled` and `trusted_hash`
    (codex-rs/config/src/hook_config.rs at be765f1a). vinta/hal-9000's
    `[hooks.state."<hook>"]` tables therefore declare no hook. A `hooks = true` under [cases] is
    a switch, not a hook, and so is `codex_hooks = true`. A file that is not TOML declares
    nothing."""
    try:
        data = tomllib.loads(Path(file_path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # UnicodeDecodeError and tomllib.TOMLDecodeError are ValueErrors (`_codex_mcp_servers`).
        return False
    return paths.has_a_grouped_hook(data.get("hooks"))


def detect_hooks(repo_path):
    found = []
    for rel, tools in _hooks_places(repo_path, HOOKS_JSON_FILES, _expanded_files).items():
        # The name as it is spelled on disk, for the reason that detect_settings gives.
        real = _find_case_insensitive(repo_path, rel)
        # `rel`, not the name on disk, because the heuristics declare the format under the
        # declared path, and a nestable root puts the same file at several depths.
        if real is not None and _has_hooks(repo_path / real, rel, tools=tools):
            found.append(real)

    # Under every nestable root, as the JSON files above and the directories below. `.codex` is
    # nestable. Read at the root alone, a nested `.codex/config.toml` would get a settings row
    # and an mcp row but no hooks row, and the file would then appear in other.csv.
    for rel in _expanded_files(repo_path, HOOKS_TOML_FILES):
        real = _find_case_insensitive(repo_path, rel)
        if real is not None and _has_toml_hooks(repo_path / real):
            found.append(real)

    for rel in _expanded_files(repo_path, HOOKS_NAMED_FILES):
        real = _find_case_insensitive(repo_path, rel)
        if real is not None and _loads_as_named_hooks(repo_path, repo_path / real):
            found.append(real)

    for rel, tools in _hooks_places(repo_path, HOOKS_MODULE_FILES, _expanded_files).items():
        real = _find_case_insensitive(repo_path, rel)
        if real is not None and (_has_hooks(repo_path / real, rel, tools=tools)
                                 or _has_hook_modules(repo_path / real)):
            found.append(real)

    for rel, tools in _hooks_places(repo_path, HOOKS_DIRS_JSON, _expanded).items():
        hooks_dir = _resolve_dir(repo_path, rel)
        if hooks_dir is None:
            continue
        # The same content test as for every other hook file, because a JSON file in the
        # directory is not a hook configuration by itself. A collected symlink marker is read as
        # the file that it names inside the repository (`_followed_file`), as the tool reads the
        # hook file through its link. The collected file cleanup keeps that file
        # (`TARGET_READING_MECHANISMS`).
        for p in sorted(_listed(hooks_dir, hooks_dir.rglob("*.json"))):
            if (p.is_file() and p.suffix == ".json"
                    and _has_hooks(_followed_file(repo_path, p), tools=tools)):
                found.append(_repo_relative(repo_path, p))

    for rel, name_pattern in HOOKS_NAME_GLOBS:
        for directory in _expanded(repo_path, [rel]):
            d = _resolve_dir(repo_path, directory)
            if d is None:
                continue
            for p in sorted(_listed(d, d.iterdir())):
                if p.is_file() and fnmatch.fnmatchcase(p.name, name_pattern):
                    found.append(_repo_relative(repo_path, p))

    # A hooks file that a plugin manifest names beyond hooks/hooks.json, held to the same test
    # in the format of the manifest's tool.
    for rel, rule, kind in _plugin_components(repo_path):
        if (kind == "declared" and rule["mechanism"] == "hooks" and (repo_path / rel).is_file()
                and _has_hooks(repo_path / rel, rel, tools={rule["tool"]})):
            found.append(rel)

    # Each file once, because a plugin at the repository root names the same file as a root row.
    found.extend(rel for rel, rule, kind in _plugin_components(repo_path)
                 if kind == "inline" and rule["mechanism"] == "hooks")
    return list(dict.fromkeys(found))


def detect_rules(repo_path, *, include_declared=True):
    rows = []
    seen = set()
    for rd in _expanded(repo_path, RULES_DIRS_CODEX):
        d = _resolve_dir(repo_path, rd)
        if d is None:
            continue
        for p in sorted(_listed(d, d.iterdir())):
            if p.is_file() and p.suffix == ".rules":
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "rule": rel,
                    "name": p.stem,
                })
    for rd, extensions in ([(d, RULES_CURSOR_EXTENSIONS)
                            for d in _expanded(repo_path, RULES_DIRS_CURSOR)]
                           + [(d, extensions_for(RULES_MARKDOWN_MODE))
                              for d in _expanded(repo_path, RULES_DIRS_MARKDOWN)]):
        d = _resolve_dir(repo_path, rd)
        if d is None:
            continue
        for p in sorted(_listed(d, d.rglob("*"))):
            if p.is_file() and p.suffix in extensions:
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "rule": rel,
                    "name": p.stem,
                })
    for rd in _expanded(repo_path, RULES_DIRS_CLAUDE):
        d = _resolve_dir(repo_path, rd)
        if d is None:
            continue
        bases = [d]
        if posixpath.basename(rd) == "rules":
            bases.extend(target for _marker, target in _linked_markdown_folders(repo_path, d))
        for p in [p for base in bases for p in sorted(_listed(base, base.rglob("*.md")))]:
            if p.is_file():
                rel = _repo_relative(repo_path, p)
                if rel in seen:
                    continue
                seen.add(rel)
                rows.append({
                    "rule": rel,
                    "name": p.stem,
                })
    left_out = _left_out(repo_path)
    for rd, pattern, required in (
            [(d, "*.md", None) for d in _expanded(repo_path, RULES_DIRS_MD_FLAT)]
            + [(d, "*.mdc", _has_description) for d in _expanded(repo_path, RULES_DIRS_DESCRIBED)]
            + [(d, "*.md", _has_trigger) for d in _expanded(repo_path, RULES_DIRS_TRIGGERED)]):
        d = _resolve_dir(repo_path, rd)
        if d is None:
            continue
        for p in sorted(_listed(d, d.glob(pattern))):
            if p.is_file() and (required is None or required(repo_path, p)):
                rel = _repo_relative(repo_path, p)
                if rel in seen or left_out.get(rel) == rd:
                    continue
                seen.add(rel)
                rows.append({
                    "rule": rel,
                    "name": p.stem,
                })
    for rel in _expanded_files(repo_path, RULES_FILES):
        real = _find_case_insensitive(repo_path, rel)
        if real is None or not (repo_path / real).is_file() or real in seen:
            continue
        seen.add(real)
        rows.append({
            "rule": real,
            "name": Path(real).stem,
        })
    for rel, _heuristic_row in (paths.declared_rule_files(Path(repo_path), heuristic_rows=RULES_JSON_ROWS,
                                                include_registering_files=True)
                       if include_declared else ()):
        if rel in seen:
            continue
        seen.add(rel)
        rows.append({
            "rule": rel,
            "name": Path(rel).stem,
        })
    # An output style that a Claude Code manifest names, and a rule that a Copilot CLI manifest
    # names.
    for rel, mode in _declared(repo_path, "rules"):
        if rel in seen or not (repo_path / rel).is_file():
            continue
        if mode.startswith("dir_ext_described:") and not _has_description(repo_path, repo_path / rel):
            continue
        if mode.startswith("dir_ext_triggered:") and not _has_trigger(repo_path, repo_path / rel):
            continue
        seen.add(rel)
        rows.append({"rule": rel, "name": Path(rel).stem})
    return rows


# {mechanism: [(path, match mode)]} for the files that count by the value under one of their
# top-level keys, OpenCode's configuration files for its agents, commands, MCP and LSP servers.
JSON_KEY_ROWS = defaultdict(list)
for _row in _PATTERNS:
    if _row["match_mode"].startswith(heuristics.JSON_KEY_MODES):
        JSON_KEY_ROWS[_row["mechanism"]].append((_row["path"], _row["match_mode"]))


def _json_key_files(repo_path, mechanism):
    """The files of the `JSON_KEY_ROWS` of `mechanism` that count, each where its row reads it:
    a bare filename at the repository root, a path under a nestable root also below it."""
    found = []
    for path, mode in JSON_KEY_ROWS.get(mechanism, ()):
        for rel in _expanded_files(repo_path, [path]):
            real = _find_case_insensitive(repo_path, rel)
            if real is None or not (repo_path / real).is_file():
                continue
            try:
                data = _load_json(repo_path / real, real)
            except (OSError, ValueError, RecursionError):
                continue
            if heuristics.json_key_counts(mode, data):
                found.append(real)
    return found


def detect_lsp(repo_path):
    """The LSP server configurations of the repository, a path each, in path order."""
    found = list(dict.fromkeys(_json_key_files(repo_path, "lsp")))
    for rel in _expanded_files(repo_path, LSP_FILES):
        real = _find_case_insensitive(repo_path, rel)
        if real is not None and (repo_path / real).is_file() and real not in found:
            found.append(real)
    found.extend(rel for rel in _nested(repo_path, NESTED_LSP_FILES) if rel not in found)
    found.extend(rel for rel, _mode in _declared(repo_path, "lsp")
                 if (repo_path / rel).is_file() and rel not in found)
    # The manifest that configures a plugin's language servers inline (`paths.plugin_components`).
    found.extend(rel for rel, rule, kind in _plugin_components(repo_path)
                 if kind == "inline" and rule["mechanism"] == "lsp" and rel not in found)
    return sorted(found)


def _codex_mcp_servers(path):
    """Whether a Codex config.toml defines an MCP server: a table under `mcp_servers`, as a
    `[mcp_servers.<name>]` header or as dotted keys. The TOML spec 1.0.0 states that "Dotted
    keys create and define a table for each key part before the last one", and
    `mcp_servers.nx-mcp.command = 'npx'` in ever-co/ever-teams defines the same table as a
    header. A regular expression over headers would miss that table and would read a header
    that a `#` comments out. A file that is not TOML defines no table."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        # UnicodeDecodeError and tomllib.TOMLDecodeError are ValueErrors, and so is the error
        # of an integer longer than Python converts, which is no TOMLDecodeError.
        return False
    servers = data.get("mcp_servers")
    return isinstance(servers, dict) and any(isinstance(v, dict) for v in servers.values())


def _names_a_server(servers):
    """Whether a map of MCP servers has a server with a `command` or a `url`, from which the
    tools that read it infer the server's type, as VS Code's `sanitizeServer` does."""
    return isinstance(servers, dict) and any(
        isinstance(server, dict) and any(isinstance(server.get(key), str) for key in ("command", "url"))
        for server in servers.values())


# The agents that OpenCode defines itself, which its `agent.ts` loads with `native: true`
# (packages/opencode/src/agent/agent.ts of anomalyco/opencode at e3c4eb242585, read 2026-11-06). An
# entry with one of these names adjusts a built-in agent and defines none. GLOSSARY.md defines
# subagents as "An agent definition to which the tool can delegate part of a task".
_OPENCODE_BUILT_IN_AGENTS = frozenset({"build", "plan", "general", "explore", "compaction", "title",
                                       "summary"})


def _opencode_markdown_agent_name(repo_path, path, folder):
    """The name under which OpenCode puts the Markdown agent at `path`, a file below `folder`,
    one of its agents or modes folders, into its agents. `load` and `loadMode` in
    packages/opencode/src/config/agent.ts of anomalyco/opencode at e3c4eb242585 (read
    2026-11-08) build an agent of `name`, `...md.data` and `prompt: md.content.trim()` and store
    it under `result[config.name]`: the `name` of the front matter where it sets one, and
    otherwise the path below the folder without its extension, such as `review/plan` for
    `.opencode/agents/review/plan.md`. `load` forms that name with
    `configEntryNameFromPath(path.relative(dir, item), ["agent/", "agents/"])` and `loadMode`
    with `configEntryNameFromPath(path.relative(dir, item), ["mode/", "modes/"])`. A front
    matter that does not parse leaves the name of the path, and a `name` that is no string gives
    None, which names no built-in agent."""
    lines = paths._item_front_matter_lines(_file_text(repo_path, path))
    if lines is not None:
        parsed, data = paths.load_yaml("\n".join(lines))
        if parsed and isinstance(data, dict) and "name" in data:
            return data["name"] if isinstance(data["name"], str) else None
    return Path(path).relative_to(folder).with_suffix("").as_posix()


def _opencode_defines_an_agent(repo_path, rel):
    """Whether the OpenCode configuration at `rel` names an agent other than a built-in one under a
    key of `JSON_KEY_ROWS`: the `agents` of OpenCode's agents page, or the `agent` or the `mode`
    of its migration page."""
    try:
        data = _load_json(repo_path / rel, rel)
    except (OSError, ValueError, RecursionError):
        return False
    return any(isinstance(data.get(key), dict) and set(data[key]) - _OPENCODE_BUILT_IN_AGENTS
               for key in {mode.partition(":")[2] for _path, mode in JSON_KEY_ROWS["subagents"]})


def _plugin_mcp_servers(data, tool):
    """Accept a Claude Code or Codex plugin's flat or wrapped map of MCP server objects with a
    server in it that the plugin's `tool` loads. `{"mcpServers": {}}`, as storybookjs/storybook's
    Codex plugin has it, and `{}` configure no server. Codex drops the `type` of a server and
    infers the transport from the `command` or the `url` (`normalize_plugin_mcp_server_value`
    removes "type" in codex-rs/codex-mcp/src/plugin_config.rs of openai/codex rust-v0.149.0), and
    Claude Code loads a server with a `url` only where its type names the transport
    (`paths.claude_loads_a_server`)."""
    if not isinstance(data, dict):
        return False
    servers = data["mcpServers"] if isinstance(data.get("mcpServers"), dict) else data
    return paths.claude_loads_a_server(servers) if tool == "claude" else _names_a_server(servers)


def _has_mcp_servers(data):
    """Whether a document of a `json_mcp_servers` row names a server under "mcpServers"."""
    return isinstance(data, dict) and isinstance(data.get("mcpServers"), dict) and bool(data["mcpServers"])


# The top-level keys of the MCP files that a tool reads by their place, and whether the tool also
# reads the bare form, in which each top-level key names a server. Copilot CLI reads
# `.github/mcp.json` with "mcpServers" or in the bare form and calls "servers" "the unsupported
# top-level key". VS Code reads `.vscode/mcp.json` under "servers" alone
# (`fromWorkspaceFolderMcpServers` of mcpResourceScannerService.ts at 419dc878), and Visual
# Studio's page shows that file and the `.mcp.json` of a solution directory with "servers". The
# root `.mcp.json` and a `.mcp.json` below it are read with either key or in the bare form.
_MCP_KEYS = {".github/mcp.json": (("mcpServers",), True),
             ".vscode/mcp.json": (("servers",), False)}
_DEFAULT_MCP_KEYS = (("mcpServers", "servers"), True)


def _configures_a_server(repo_path, rel):
    """Whether the MCP file `rel` configures a server: an entry with a `command` or a `url`
    under one of the file's keys (`_MCP_KEYS`, "mcpServers" and "servers" otherwise), or at the
    top level in the bare form where the file's tool reads it. byteball/ocore's
    `.vscode/mcp.json`, `{"servers": {}}`, configures no server. The file is parsed in the
    content format of its row, as VS Code reads `.vscode/mcp.json` and the root `.mcp.json`
    with comments and trailing commas. A collected symlink marker is read as the file that it
    names, and a file that does not parse configures nothing."""
    try:
        data = _load_json(_followed_file(Path(repo_path), Path(repo_path) / rel), rel)
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    place = rel.replace(os.sep, "/").lower()
    keys, bare = next((v for suffix, v in _MCP_KEYS.items()
                       if place == suffix or place.endswith("/" + suffix)), _DEFAULT_MCP_KEYS)
    servers = next((data[key] for key in keys if isinstance(data.get(key), dict)),
                   data if bare else {})
    return _names_a_server(servers)


def _servers_key_alone(repo_path, rel):
    """Whether the MCP file `rel` names its servers under "servers" and nowhere else. Of the tools
    that read the root `.mcp.json`, VS Code reads it wrapped in "mcpServers" or in the bare form
    (`parseWorkspaceRootMcpConfiguration` of mcpWorkspaceConfiguration.ts at 419dc878), Copilot
    CLI reads "mcpServers" or the bare form and Claude Code "mcpServers", and only Visual Studio,
    whose Copilot reads the `.mcp.json` of a solution directory, reads "servers"."""
    try:
        data = _load_json(_followed_file(Path(repo_path), Path(repo_path) / rel), rel)
    except Exception:
        return False
    if not isinstance(data, dict) or not _names_a_server(data.get("servers")):
        return False
    return not _names_a_server(data.get("mcpServers")) and not _names_a_server(data)


def _claude_reads_nested_mcp(repo_path, rel):
    """Whether Claude Code reads the `.mcp.json` `rel` below the root, besides Copilot CLI, which
    the `nested:` row names. Claude Code's project scope reads the `.mcp.json` of each directory
    from its working directory up to the file system root and merges their "mcpServers"
    (`while(w!==SYr(w).root)y.push(w),w=gYr(w);for(let L of y.reverse()){let G=goe(L,".mcp.json")`
    in Claude Code 2.1.314), so it reads such a file in a session started in its directory or
    below it, under "mcpServers" alone. It reads the file only where it loads a server of it
    (`paths.claude_loads_a_server`), and boardsesh/boardsesh's `packages/web/.mcp.json`, whose one
    server has a `url` and no `type`, is Copilot's alone."""
    if posixpath.basename(rel) != MCP_ROOT_FILE or "/" not in rel:
        return False
    try:
        data = _load_json(_followed_file(Path(repo_path), Path(repo_path) / rel), rel)
    except Exception:
        return False
    return isinstance(data, dict) and paths.claude_loads_a_server(data.get("mcpServers"))


def detect_mcp(repo_path):
    found = []

    # The files that a tool reads by their place count when they configure a server.
    mcp_root = _find_case_insensitive(repo_path, MCP_ROOT_FILE)
    if mcp_root is not None and _configures_a_server(repo_path, mcp_root):
        found.append(mcp_root)

    for rel in _expanded_files(repo_path, MCP_FILES):
        real = _find_case_insensitive(repo_path, rel)
        if real is not None and _configures_a_server(repo_path, real):
            found.append(real)
    found.extend(rel for rel in _nested(repo_path, NESTED_MCP_FILES)
                 if _configures_a_server(repo_path, rel))

    # The MCP file of a plugin counts when the tool of a plugin that contains it loads a server
    # of it, and a folder with the manifests of a Claude Code and a Codex plugin shares one file.
    plugin_mcp_tools = defaultdict(set)
    for row in MCP_PLUGIN_ROWS:
        for rel in _expanded_files(repo_path, [row["path"]]):
            plugin_mcp_tools[rel].add(row["tool"])
    for rel in list(_expanded_files(repo_path, MCP_JSON_FILES)) + sorted(plugin_mcp_tools):
        declared = _find_case_insensitive(repo_path, rel)
        if declared is None:
            continue
        try:
            data = _load_json(repo_path / declared, declared)
            if (any(_plugin_mcp_servers(data, tool) for tool in plugin_mcp_tools[rel])
                    if rel in plugin_mcp_tools else _has_mcp_servers(data)):
                found.append(declared)
        except Exception:
            pass

    for name in MCP_AGENT_PLUGINS:
        found.extend(_agent_plugins_documents(repo_path, name,
                                              "basename_json_agent_plugins_mcp"))

    for name in MCP_BASENAMES:
        for p in sorted(_listed(repo_path, _named(repo_path.rglob("*"), name))):
            if not p.is_file():
                continue
            try:
                if _has_mcp_servers(_load_json(p, name)):
                    found.append(str(p.relative_to(repo_path)))
            except Exception:
                pass

    found.extend(_json_key_files(repo_path, "mcp"))

    for rel in _expanded_files(repo_path, [MCP_CODEX_CONFIG]):
        codex_config = _find_case_insensitive(repo_path, rel)
        if codex_config is None:
            continue
        if _codex_mcp_servers(repo_path / codex_config):
            found.append(codex_config)

    # An MCP file that a plugin manifest names beyond the default one, under the same test. A
    # file such as Codex's .app.json is tested by the value under a key and counts by its apps.
    for rel, rule, kind in _plugin_components(repo_path):
        if kind != "declared" or rule["mechanism"] != "mcp":
            continue
        mode = rule["match_mode"]
        try:
            data = _load_json(repo_path / rel, rel)
            matches = (_plugin_mcp_servers(data, rule["tool"]) if mode == "json_plugin_mcp"
                       else heuristics.json_key_counts(mode, data)
                       if mode.startswith(heuristics.JSON_KEY_MODES)
                       else _has_mcp_servers(data))
            if matches:
                found.append(rel)
        except Exception:
            pass

    # Each file once, because a plugin at the repository root names the same file as a root row.
    found.extend(rel for rel, rule, kind in _plugin_components(repo_path)
                 if kind == "inline" and rule["mechanism"] == "mcp")
    return list(dict.fromkeys(found))


# The heuristics' `tool` column says which tool owns a path, and a new mechanism is attributed
# by adding its row rather than by editing a prefix test here. Rows marked `any` own nothing,
# because several tools read AGENTS.md and .mcp.json, and neither file is evidence for a
# particular tool. The workflow rows are left out. Their tool is the action, the command or the
# engine that a file runs, which a path cannot identify. They also share `.github/workflows`,
# and a lookup by path would give every workflow the tool of whichever workflow row was read
# last. Here a workflow path belongs to `.github`, and `artifact_tools` takes the tool of each
# agent workflow from `detect_agent_workflows`.
_TOOLS_BY_PATH = defaultdict(list)
for _row in _PATTERNS:
    # A plugin part belongs to its plugin, and `plugin_part_tools` reads the plugin's tool off
    # the tree.
    if _row["mechanism"] == "collect_only" or heuristics.plugin_placeholder(_row["path"]):
        continue
    if not _row["match_mode"].startswith(heuristics.WORKFLOW_MODES):
        _TOOLS_BY_PATH[_row["path"]].append((_row["tool"], _row["match_mode"]))


def _tool_of_rows(rows, basename):
    """The tool of the row that a file in this path's directory belongs to.

    Two tools can read one directory by extension: Codex reads the TOML files of
    `.codex/agents` and Cursor the Markdown. The row with the file's extension takes
    precedence. A path with one row, or with no row that names the file's extension, takes
    the first row.
    """
    for tool, mode in rows:
        for prefix in ("tree_ext:", "dir_ext:", "tree_suffix:"):
            if mode.startswith(prefix) and basename.lower().endswith(
                    tuple(mode[len(prefix):].split("|"))):
                return tool
    return rows[0][0]
# Only basename rows are matched by filename. A root_file row is exact by definition, and
# matching it by name would attribute .claude/.mcp.json to no tool instead of to Claude, which
# owns the directory that contains it.
_TOOL_BY_BASENAME = {r["path"]: r["tool"] for r in _PATTERNS
                     if r["match_mode"] == "basename" and r["mechanism"] != "collect_only"}


def tool_for_path(path):
    """Return the tool that owns a configuration artifact's path, or None if no tool does.

    The longest path of the heuristics that contains the configuration artifact takes
    precedence. `.github/skills/x/SKILL.md` is attributed through `.github/skills` rather than
    through the bare `.github` row, and a `.github/CLAUDE.md` is a Claude context file rather
    than evidence of Copilot.

    A path of the heuristics is matched against whole segments anywhere in the path of the
    configuration artifact, because a mechanism directory below the root owns its contents as
    the root directory does. `packages/web/.claude/settings.json`, for example, is Claude's.
    Matching whole segments keeps `.claudex/x` and `my.claude/x` out.
    """
    basename = os.path.basename(path)
    if basename in _TOOL_BY_BASENAME:
        tool = _TOOL_BY_BASENAME[basename]
        if tool != "any":
            return tool
        # A name that every tool reads belongs to one tool inside that tool's own directory when
        # a row names it there, as Claude Code reads .claude/AGENTS.md.
        owned = {p: rows for p, rows in _TOOLS_BY_PATH.items()
                 if "/" in p and os.path.basename(p) == basename}
        best = _longest_match(path.split("/"), owned, basename)
        return None if best is None or best[1] == "any" else best[1]
    if "/" not in path and basename.lower() in ROOT_ANY_CASE:
        tool = ROOT_ANY_CASE[basename.lower()]
        return None if tool == "any" else tool

    best = _longest_match(path.split("/"), _TOOLS_BY_PATH, basename)
    if best is None or best[1] == "any":
        return None
    return best[1]


def _longest_match(parts, tools_by_path, basename):
    """(heuristics path, tool) for the longest path of `tools_by_path` that occurs as whole
    segments in `parts`."""
    best = None
    for heuristics_path, rows in tools_by_path.items():
        tool = _tool_of_rows(rows, basename)
        wanted = heuristics_path.split("/")
        # A row is matched below the root only when its first segment is a directory that can
        # be there. Every other row is matched at the root alone: `.github` stays at the root,
        # a `root_file` row owns nothing deeper, and so do the `plugin_sibling` filenames.
        # Otherwise a `.cursor/README.md` would read as Claude's, because "README.md" is the
        # longer match.
        last = len(parts) - len(wanted) if wanted[0] in _NESTABLE_ROOTS else 0
        starts = range(last + 1)
        if (wanted[0] not in _NESTABLE_ROOTS
                and any(heuristics.nested_inner(mode) is not None for _tool, mode in rows)):
            # A `nested:` row also reads its path below the root, outside the directories that
            # their own rows read whole, where `paths.nested_places` finds its places.
            starts = [start for start in range(len(parts) - len(wanted) + 1)
                      if not any(part in SPEC_DIRS for part in parts[:start])]
        for start in starts:
            if parts[start:start + len(wanted)] == wanted:
                if best is None or len(heuristics_path) > len(best[0]):
                    best = (heuristics_path, tool)
                break
    return best


WORKFLOW_ACTIONS = heuristics.workflow_actions(_PATTERNS)
WORKFLOW_ENGINES = heuristics.workflow_engines(_PATTERNS)
WORKFLOW_COMMANDS = heuristics.workflow_commands(_PATTERNS)
# The directory that the workflow rows name, the same for all of them, because the tool comes
# from the action, the command or the engine that a file runs, not from where the file is.
SETTINGS_WORKFLOW_DIRS = [r["path"] for r in _PATTERNS
                          if r["match_mode"].startswith(heuristics.WORKFLOW_MODES)]
WORKFLOWS_DIR = SETTINGS_WORKFLOW_DIRS[0] if SETTINGS_WORKFLOW_DIRS else ".github/workflows"


def detect_agent_workflows(repo_path):
    """Return [(path, tool)] for each workflow that runs one of the documented agent actions
    or has a shell step that runs a tool's documented headless invocation, and each GitHub
    Agentic Workflow on one of the engines.

    The tool comes from the action, the command or the engine rather than from the path,
    because every one of these files is in `.github/workflows`, and `tool_for_path` would read
    that directory as Copilot's. A file name does not identify the tool either, because a
    repository may rename the Gemini action's example workflows.
    """
    found = []
    directory = repo_path / WORKFLOWS_DIR
    if not directory.is_dir():
        return found
    for entry in sorted(_listed(directory, directory.iterdir())):
        tool = paths.workflow_tool(entry, WORKFLOW_ACTIONS, WORKFLOW_ENGINES, WORKFLOW_COMMANDS)
        if tool:
            found.append((f"{WORKFLOWS_DIR}/{entry.name}", tool))
    return found


# The tool of each plugin placeholder, read off its rows. The rows of the portable Agent
# Plugins format name `any` and the tools of its vendor parts, such as `com.github.copilot`,
# and its manifest belongs to no tool.
_PLUGIN_KIND_TOOLS = defaultdict(set)
for _row in _PATTERNS:
    if heuristics.plugin_placeholder(_row["path"]):
        _PLUGIN_KIND_TOOLS[heuristics.plugin_placeholder(_row["path"])].add(_row["tool"])


def plugin_part_tools(repo_path):
    """Return [(path, tool, row)] for each part and each manifest of a plugin in the tree, with
    the plugin's tool and the heuristic row that reads the part, or None for a manifest.

    A plugin's skills belong to the plugin's tool wherever the repository placed the plugin,
    but `tool_for_path` finds no tool in a path such as `plugins/modelopt/skills`. Nor does it
    find one in the manifest of a Copilot CLI plugin below the root,
    `plugins/lint/.github/plugin/plugin.json`, because `tool_for_path` reads `.github` at the
    root only. A plugin root with the manifests of two vendors, `.claude-plugin` and
    `.codex-plugin`, gives its parts both tools. The portable Agent Plugins manifest attributes
    the shared parts to no tool, and its vendor-specific parts under `com.github.copilot/` to
    Copilot. Its `skills/` and `mcp.json` are also parts of Codex and of Copilot CLI where the
    plugin is that tool's as well, through the tool's manifest beside the portable one or the
    tool's marketplace (`paths.plugin_components`). Codex and Copilot CLI each read another
    vendor's manifest as their own in a folder that the tool's marketplace lists and that has no
    manifest of the tool's own and no portable manifest, as Copilot CLI reads the
    `.claude-plugin/plugin.json` of such a folder, and that manifest is the tool's too
    (`paths.borrowed_manifest`). A Copilot CLI marketplace is Copilot's, also in a `.github`
    below the root, where `tool_for_path` finds no tool (`paths.copilot_marketplaces`).
    """
    root = Path(repo_path)
    found = [(rel, rule["tool"], rule) for rel, rule in paths.plugin_parts(root)
             if rule["tool"] != "any"]
    roots = paths.plugin_roots(root)
    for kind, bases in roots.items():
        tools = _PLUGIN_KIND_TOOLS.get(kind, set()) - {"any"}
        if len(tools) != 1:
            continue
        tool, = tools
        for base in bases:
            for manifest in heuristics.PLUGIN_ROOTS[kind]:
                rel = f"{base}/{manifest}" if base else manifest
                if (root / rel).is_file():
                    found.append((rel, tool, None))
            if kind in ("<codex-plugin>", "<copilot-plugin>"):
                borrowed = paths.borrowed_manifest(root, base, kind, roots["<agent-plugin>"])
                if borrowed is not None:
                    found.append((borrowed, tool, None))
    found.extend((rel, "copilot", None) for rel in paths.copilot_marketplaces(root))
    return found


def _part_loads_hooks(repo_path, rel, rule):
    """Whether the tool of the plugin part `rel`, a hooks file that the heuristic `rule` reads,
    loads a hook from it, by the test that `detect_hooks` applies to the file in the format of
    that tool (`_loads_hooks`, and a mod's `modules` for a `json_hooks_or_modules` row). A
    folder with the manifests of a Claude Code and a Codex plugin shares one hooks file, and
    Codex refuses razorpay/blade's, which Claude Code loads. A manifest, a part of another
    mechanism or match mode and the file that configures hooks inline, which
    `paths.plugin_components` tested, pass."""
    if (rule is None or rule["mechanism"] != "hooks"
            or rule["match_mode"] not in ("json_hooks", "json_hooks_or_modules")):
        return True
    if any(found == rel and row is rule and kind == "inline"
           for found, row, kind in _plugin_components(Path(repo_path))):
        return True
    path = Path(repo_path) / rel
    return (_has_hooks(path, rel, tools={rule["tool"]})
            or rule["match_mode"] == "json_hooks_or_modules" and _has_hook_modules(path))


def _part_loads_servers(repo_path, rel, rule):
    """Whether the tool of the plugin part `rel`, an MCP file that the heuristic `rule` reads,
    loads a server of it (`_plugin_mcp_servers` for that tool). A folder with the manifests of a
    Claude Code and a Codex plugin shares one `.mcp.json`, and a server with a `url` and no
    `type` there is Codex's and not Claude Code's. A manifest, a part of another mechanism or
    match mode and the file that configures servers inline, which `paths.plugin_components`
    tested, pass."""
    if rule is None or rule["mechanism"] != "mcp" or rule["match_mode"] != "json_plugin_mcp":
        return True
    if any(found == rel and row is rule and kind == "inline"
           for found, row, kind in _plugin_components(Path(repo_path))):
        return True
    try:
        data = _load_json(Path(repo_path) / rel, rel)
    except Exception:
        return False
    return _plugin_mcp_servers(data, rule["tool"])


def _part_loads(repo_path, rel, rule, mechanism):
    """Whether the plugin part `rel` gives its tool to the row of `mechanism` at its own path:
    a hooks row only where the tool loads a hook from it (`_part_loads_hooks`), an MCP row only
    where the tool loads a server of it (`_part_loads_servers`), and a row of another
    mechanism always."""
    if mechanism == "hooks":
        return _part_loads_hooks(repo_path, rel, rule)
    if mechanism == "mcp":
        return _part_loads_servers(repo_path, rel, rule)
    return True


# The match modes of a plugin part that reads only the files with an extension in it.
_EXTENSION_PART_MODES = ("dir_ext:", "dir_ext_described:", "dir_ext_triggered:", "tree_ext:",
                         "tree_ext_described:", "tree_suffix:")


def _part_reads(row, part, mechanism, path, repo_path):
    """Whether the plugin part at `part`, which the heuristic `row` reads, reads the file `path`
    below it as the `mechanism` of its row. A part gives its tool only to rows of its own
    mechanism: xberg-io/xberg's `plugin/commands/*.md` are skills of its Claude Code plugin and
    commands of its Cursor plugin, and each tool reads them as one of the two. A part of files
    with an extension reads only these files: Cursor reads the `.md`, `.mdc`, `.markdown` and
    `.txt` files of a plugin's `commands`, and not the `.toml` commands that a Gemini CLI
    extension in the same folder keeps there, as mondaycom/mcp does. The `agents/` of a Gemini
    CLI extension reads only an agent that Gemini CLI loads, by the tests of `detect_subagents`,
    because a Claude Code plugin that shares the folder can keep agents there that Gemini CLI
    refuses. A skill of a Codex plugin is only a SKILL.md that Codex's parser accepts
    (`_codex_skill_loads`), and one of a Cursor plugin only a SKILL.md with a front matter that
    Cursor parses (`_cursor_skill_loads`)."""
    if row is None:
        return True
    if row["mechanism"] != mechanism:
        return False
    if (row["tool"] in ("codex", "cursor") and mechanism == "skills"
            and posixpath.basename(path).lower() == SKILL_MARKER.lower()):
        loads = _codex_skill_loads if row["tool"] == "codex" else _cursor_skill_loads
        return loads(repo_path, Path(repo_path) / path)
    if not row["match_mode"].startswith(_EXTENSION_PART_MODES):
        return True
    if not heuristics.heuristic_matches({**row, "path": part}, path):
        return False
    if row["tool"] == "gemini" and mechanism == "subagents":
        return (not posixpath.basename(path).startswith("_")
                and _passes_content_test(row, repo_path, Path(repo_path) / path, path))
    return True


def declared_file_tools(repo_path):
    """[(mechanism, path, tool)] for each file that only a declaration of that mechanism finds,
    the declaring tool.

    A file that the detector of the declaration's mechanism also finds keeps its ordinary
    attribution. A file that another mechanism reads keeps that mechanism's attribution in the
    other mechanism's row, and takes the declaring tool in the row of the declaration: a
    `.cursor/rules/*.mdc` that OpenCode's `instructions` name is Cursor's rule and OpenCode's
    context file. The declaring configuration is attributed to its own tool separately. A part of
    a subagent that configuration names, such as the instructions of a Codex role, is no row of
    the subagents and needs no tool (`_declared_subagents`).
    """
    declarations = (paths.declared_context_files(Path(repo_path), heuristic_rows=CONTEXT_DECLARATION_HEURISTICS)
                    + _declared_subagents(repo_path)[0]
                    + paths.registered_configuration_files(Path(repo_path), include_registering_files=True))
    if not declarations:
        return []
    ordinary = {"context_file": set(detect_context_files(repo_path, include_declared=False)),
                "rules": {row["rule"] for row in detect_rules(repo_path, include_declared=False)},
                "plugins": set(detect_plugins(repo_path, include_registered=False))}
    for mechanism, detector in (("settings", detect_settings), ("hooks", detect_hooks),
                                ("mcp", detect_mcp), ("lsp", detect_lsp)):
        ordinary[mechanism] = set(detector(repo_path))
    for mechanism, detector, column in (("skills", detect_skills, "skills.md"),
                                        ("subagents", detect_subagents, "subagent"),
                                        ("commands", detect_commands, "command")):
        ordinary[mechanism] = {row[column] for row in detector(repo_path, include_registered=False)}
    return [(rule["mechanism"], rel, rule["tool"]) for rel, rule in declarations
            if rel not in ordinary[rule["mechanism"]]]


def marker_directories(repo_path, markers):
    """[(marker, directory)] for each symlink marker, the directory relative to the repository.

    The configuration artifacts behind a marker are reported at their real path, the
    directory here, and the marker gives the tool that reads them through the link. Where a tool
    directory is a link, a marker below the directory that the link leads to is also given at the
    path at which the tool reads it through that link: a skill linked into the
    `config/claude/skills` of a `.claude` that leads to `config/claude` is read at
    `.claude/skills/<name>`.
    """
    root = Path(repo_path).resolve()
    found = []
    for marker in markers:
        target = _resolve_dir(Path(repo_path), marker)
        if target is not None:
            found.append((marker, target.resolve().relative_to(root).as_posix()))
    for _ in range(len(found)):
        through = [(outer + marker[len(directory):], target)
                   for marker, target in found for outer, directory in found
                   if posixpath.basename(outer) in paths.TOOL_DIRECTORIES
                   and marker.startswith(directory + "/")]
        added = [pair for pair in dict.fromkeys(through) if pair not in found]
        if not added:
            break
        found.extend(added)
    return found


# The path heuristics that a row's tool is read from, by mechanism: every row except the workflow
# rows, the plugin placeholder rows and the `collect_only` rows. A workflow row takes the tool of
# the action, engine or command that a file runs, a plugin placeholder row takes the plugin's
# tool, and a `collect_only` row classifies nothing.
_ATTRIBUTION_ROWS = defaultdict(list)
for _row in _PATTERNS:
    if (_row["mechanism"] != "collect_only" and not heuristics.plugin_placeholder(_row["path"])
            and not _row["match_mode"].startswith(heuristics.WORKFLOW_MODES)):
        _ATTRIBUTION_ROWS[_row["mechanism"]].append(_row)


# The rows that name an AGENTS.md in Claude Code's own directory, `.claude/AGENTS.md`.
CLAUDE_AGENTS_MD_ROWS = [r for r in _PATTERNS
                         if r["tool"] == "claude" and r["mechanism"] == "context_file"
                         and posixpath.basename(r["path"]) == "AGENTS.md"]
# Claude Code's imports of `@path`, the rows of import_patterns.csv that read its memory files.
_CLAUDE_IMPORT_ROWS = [r for r in imports.import_patterns()
                       if r["tool"] == "claude" and r["syntax"] == "claude_at"]


class _LinkedTree(imports.CloneTree):
    """The repository of the validated tree as a tool reads it: a collected symlink marker is
    read as the file that it names in the tree (`_followed_file`)."""

    def locate(self, rel):
        path = super().locate(rel)
        return None if path is None else _followed_file(self.root.resolve(), path)


# GROUNDING: Claude Code's memory page, read 2026-11-05 (https://code.claude.com/docs/en/memory):
# "By default, Claude reads `AGENTS.md` only when you have no `CLAUDE.md` in your working
# directory or above it", where "a `CLAUDE.md`, `.claude/CLAUDE.md`, or `CLAUDE.local.md`" count,
# and "When none count", Claude reads "every `AGENTS.md` and `.claude/AGENTS.md` in your working
# directory and the directories above it". A CLAUDE.md that imports `AGENTS.md` includes it
# "through the import". The value that loads both kinds of file, `claude-md-and-agents-md`, is
# ignored "in project and local settings files", so that a repository cannot select it.
def _claude_reads_agents_md(repo_path, path):
    """Whether Claude Code reads the AGENTS.md at `path` in a `.claude` directory, by the rules
    of the import detection: no CLAUDE file is at the repository root or in the directory above
    the `.claude`, which the row of `AGENTS.md` with the condition `no_claude_md` tests
    (`imports.importing_files`), or a chain of Claude Code's imports from one of its memory files
    reaches the file (`imports.chain`). ng-alain/delon's `.claude/CLAUDE.md` imports nothing, and
    Claude Code does not read its `.claude/AGENTS.md`."""
    root = Path(repo_path)
    files = sorted({file.relative_to(root).as_posix()
                    for file in paths.list_repo_files(root, heuristics.VENDORED_DIRS)} | {path})
    tree = _LinkedTree(root)
    for row, start in imports.importing_files(_CLAUDE_IMPORT_ROWS, root, files, tree,
                                              heuristics.VENDORED_DIRS):
        if start == path or any(record.target == path and record.status == imports.FOUND
                                for record in imports.chain(row, start, tree)):
            return True
    return False


# (match mode prefix, test) for the match modes with files that the detectors read before they
# report them. A test takes the repository and the file.
_CONTENT_TESTS = (("dir_ext_described:", _has_description), ("tree_ext_described:", _has_description),
                  ("dir_ext_triggered:", _has_trigger), ("dir_agent:", _has_name_and_description),
                  ("json_named_hooks", _loads_as_named_hooks))


def _passes_content_test(row, repo_path, file_path, path):
    """Whether the file passes the front matter test of the heuristic's match mode, or of the
    inner mode for a `nested:` row. A mode without a test passes every file. For the heuristic of
    `.gemini/agents`, the agent's `tools` also have to pass `_gemini_agent_tools_load`, for the
    heuristics of a project's agents folders that Cursor reads, the file has to have a front matter
    at its top (`_cursor_agent_has_front_matter`), and for the heuristic of `.claude/agents`, the
    file has to pass Claude Code's loader (`_claude_agent_loads`). The heuristic of `.claude/AGENTS.md` reads the file at
    `path`, the path at which the tool reads it, only where Claude Code reads it
    (`_claude_reads_agents_md`)."""
    if row in CLAUDE_AGENTS_MD_ROWS and not _claude_reads_agents_md(repo_path, path):
        return False
    mode = heuristics.nested_inner(row["match_mode"]) or row["match_mode"]
    test = next((test for prefix, test in _CONTENT_TESTS if mode.startswith(prefix)), None)
    if (row["tool"] == "gemini" and row["mechanism"] == "subagents"
            and mode.startswith("dir_ext_described:")
            and not _gemini_agent_tools_load(repo_path, file_path)):
        return False
    if row in CURSOR_PROJECT_AGENT_ROWS and not _cursor_agent_has_front_matter(repo_path, file_path):
        return False
    if row in CLAUDE_PROJECT_AGENT_ROWS and not _claude_agent_loads(repo_path, file_path):
        return False
    return test is None or test(repo_path, file_path)


def tools_reading(mechanism, path, file_path, repo_path):
    """(explained, tools): whether a heuristic of `mechanism` reads `path`, and the tools of the
    heuristics that do, without `any`.

    A heuristic reads a path by its match mode (`heuristics.heuristic_matches`): by the path's
    extension, its depth below the heuristic's directory or its `<name>/agent.md`. For the
    modes of `_CONTENT_TESTS`, it also reads the front matter of `file_path` (`_file_text`),
    through a marker of a link to a file. `file_path` is the file on disk in the repository at
    `repo_path`: the path itself, or the file that a symlinked directory leads to. Of the
    heuristics that read the path, the heuristics with the longest path take precedence, as in
    `tool_for_path`, and `.claude/AGENTS.md` is Claude's rather than the `AGENTS.md` that every
    tool reads, where Claude Code reads it (`_claude_reads_agents_md`). A heuristic with a path
    that starts with a directory of `_NESTABLE_ROOTS` reads it at any depth.

    A heuristic of a skills directory reads a skill's marker and no other file of the skill.
    VS Code reads `joinPath(child.resource, SKILL_FILENAME)` of each folder of a skills directory
    (promptFilesLocator.ts), and the folder's other files are the skill's scripts, references and
    assets. GovTechSG/sgds-web-component's `.claude/commands/storybook-stories` links to
    `.github/skills/storybook-stories`, and Claude Code reads the `.md` files below the link as
    commands. The file `reference/examples.md` there is Claude Code's command and no skill of
    Copilot.
    """
    parts = path.split("/")
    marker = posixpath.basename(path).lower() == SKILL_MARKER.lower()
    best, tools = -1, set()
    for row in _ATTRIBUTION_ROWS.get(mechanism, ()):
        if (not marker and (heuristics.nested_inner(row["match_mode"]) or row["match_mode"])
                in ("skill_dir", "skill_dir_flat")):
            continue
        nestable = row["path"].split("/")[0] in _NESTABLE_ROOTS
        for depth in range(len(parts) if nestable else 1):
            if heuristics.heuristic_matches(row, "/".join(parts[depth:])):
                if not _passes_content_test(row, repo_path, file_path, path):
                    break
                if len(row["path"]) > best:
                    best, tools = len(row["path"]), {row["tool"]}
                elif len(row["path"]) == best:
                    tools.add(row["tool"])
                break
    return best >= 0, tools - {"any"}


# The skills heuristics that name a directory, such as `.agents/skills` and `.cursor/skills`.
_SKILLS_DIRECTORY_ROWS = [r for r in _ATTRIBUTION_ROWS["skills"] if is_directory_mode(r["match_mode"])]
# The tool of each skills directory that the heuristics of one tool alone read, such as Antigravity's
# `.agent/skills`, which `_DIRECTORY_TOOLS` does not name, because the heuristic of `.agent` has the
# tool `any`. `.claude/skills` is not among them, because Cursor and OpenCode read it too.
_ONE_TOOL_SKILLS_DIRECTORIES = {
    row["path"]: row["tool"] for row in _SKILLS_DIRECTORY_ROWS
    if row["tool"] != heuristics.SHARED_TOOL
    and {other["tool"] for other in _SKILLS_DIRECTORY_ROWS if other["path"] == row["path"]}
    == {row["tool"]}}


def _skills_directory(path):
    """(directory, row): the part of `path`, a path at which a tool reads a SKILL.md, that the
    skills heuristic with the longest directory names, such as `pkg/.agents/skills` of
    `pkg/.agents/skills/lint/SKILL.md`, and that heuristic, or (None, None) where no skills
    heuristic with a directory reads `path`. Each heuristic is read at the depths at which
    `tools_reading` reads it, and a `nested:` heuristic below the root alone."""
    parts = path.split("/")
    best = (None, None)
    for row in _SKILLS_DIRECTORY_ROWS:
        inner = heuristics.nested_inner(row["match_mode"])
        if inner is not None:
            depths, place = range(1, len(parts)), {**row, "match_mode": inner}
        else:
            nestable = row["path"].split("/")[0] in _NESTABLE_ROOTS
            depths, place = range(len(parts) if nestable else 1), row
        for depth in depths:
            if heuristics.heuristic_matches(place, "/".join(parts[depth:])):
                if best[1] is None or len(row["path"]) > len(best[1]["path"]):
                    best = ("/".join(parts[:depth + row["path"].count("/") + 1]), row)
                break
    return best


def _tools_reading_a_skill(repo_path, read_path, path, tools):
    """The tools of `tools` that read the SKILL.md at `path`, a path of the repository, at
    `read_path`, which is `path` itself or the path through a link. `tools` holds the tools with
    a skills heuristic that reads `read_path`, and the function leaves out each tool that skips
    the file (`_skill_skippers`): a tool that skips a link to a file, Codex where it does not
    load the file, and a tool that reads another tool's directory for compatibility where the
    marker is not named `SKILL.md`. Of the tools left, the tool of the tool directory that
    contains the skills directory takes precedence. `.claude/skills/<name>/SKILL.md` is
    therefore Claude Code's, which reads it, although Cursor and OpenCode read it too, and
    `.claude/skills/<category>/<name>/SKILL.md`, which Claude Code does not read, is Cursor's and
    OpenCode's. The function returns `tools` unchanged for a SKILL.md that no skills heuristic
    with a directory reads, and for a command file or a workflow, which is no SKILL.md.

    A skills directory that the heuristics give the tool `any`, such as `.agents/skills`, gives
    an empty set of tools, which stands for every tool that reads the directory
    (`_SHARED_SKILLS_READERS`).
    Where some of them skip the file and several still read it, the skill keeps `any`, which
    names a file that several tools read and none owns, as the directory's own skills do.
    microsoft/beachball links `.agents/skills/beachball-change-file/SKILL.md` to
    `skills/beachball-change-file/SKILL.md`, outside the directory: Copilot CLI, Antigravity CLI
    and OpenCode read the skill, Codex and Cursor do not, and the skill keeps `any`. Where one
    reader is left, the skill is that tool's.
    """
    if posixpath.basename(path).lower() != SKILL_MARKER.lower():
        return tools
    repo_path = Path(repo_path)
    directory, row = _skills_directory(read_path)
    if directory is None:
        return tools
    owner = _owner_of(row)
    skipping = _skill_skippers(repo_path, repo_path / path, _resolve_dir(repo_path, directory),
                               tools if tools else _SHARED_SKILLS_READERS.get(row["path"], ()),
                               owner)
    if not tools and row["tool"] == heuristics.SHARED_TOOL:
        readers = _SHARED_SKILLS_READERS[row["path"]] - skipping
        return tools if len(readers) > 1 else set(readers)
    if (owner is not None and owner not in tools
            and _inside_a_skill(repo_path / path, len(read_path[len(directory) + 1:].split("/")))):
        # The folder of a skill that the directory's own tool reads holds that skill's files,
        # also where a link leads into it, such as a `.claude/skills` that links to an
        # `.agents/skills` with a skill in a subfolder of another skill's folder.
        return set()
    left = set(tools) - skipping
    return {owner} if owner in left else left


def _readers_without_their_own_link(path, marker, read_path):
    """The tools that read the shared skills directory of the SKILL.md at `path`, such as
    `.agents/skills`, with no directory of their own that contains `marker`, the link through which
    `read_path` reaches the file. Such a tool reads the file in the shared directory, and a link in
    another tool's directory adds no reader, as the `.claude/skills/skills` of amelioro/ameliorate,
    a link to `.agents/skills`, adds OpenCode to no skill. A skills directory that the heuristics of
    one tool alone read is that tool's own, so that the `.agent/skills/tsdown` of
    sanity-io/pkg-utils, a link to `.agents/skills/tsdown`, keeps Gemini, which counts Antigravity.
    Empty for a skill in a directory that one tool owns."""
    row = _skills_directory(path)[1]
    if row is None or row["tool"] != heuristics.SHARED_TOOL:
        return frozenset()
    read_row = _skills_directory(read_path)[1]
    own = {_DIRECTORY_TOOLS.get(part) for part in marker.split("/")}
    if read_row is not None:
        own.add(_ONE_TOOL_SKILLS_DIRECTORIES.get(read_row["path"]))
    return _SHARED_SKILLS_READERS.get(row["path"], frozenset()) - own


def _tool_of_an_other_row(path):
    """The tool of an `other` row: the owner of its directory. A file in the agents folder of
    another tool's directory that Cursor also reads stays that tool's, as a Markdown file of
    `.codex/agents` that no role names and that has no front matter at its top, such as
    `.codex/agents/notes.md`, is Codex's. `tool_for_path` gives the Markdown of such a folder to
    Cursor, because Cursor reads a subagent there."""
    parts = path.split("/")
    for row in CURSOR_PROJECT_AGENT_ROWS:
        wanted = row["path"].split("/")
        owner = _DIRECTORY_TOOLS.get(wanted[0])
        if owner not in (None, row["tool"]) and any(
                parts[start:start + len(wanted)] == wanted
                for start in range(len(parts) - len(wanted))):
            return owner
    return tool_for_path(path)


def _without_the_compatibility_reader(mechanism, tools):
    """`tools` without Cursor for a subagent that Claude Code reads too. Cursor reads
    `.claude/agents` for compatibility, and the file is the agent of the directory's own tool, as a
    skill is (`_tools_reading_a_skill`)."""
    return tools - {"cursor"} if mechanism == "subagents" and "claude" in tools else tools


# The `root_file` heuristics with the tool `any`, such as `.mcp.json`: a file at the root that every
# tool with the mechanism reads as the repository's own. The other heuristics with the tool `any`
# are left out. The `SKILL.md` heuristic, for example, reads the file at any depth, and a skill in a
# plugin's `skills/` is read by the plugin's tool.
_SHARED_ROOT_FILE_ROWS = [r for rows in _ATTRIBUTION_ROWS.values() for r in rows
                          if r["match_mode"] == "root_file" and r["tool"] == "any"]


def _shared_root_file(mechanism, path):
    """Whether a `root_file` heuristic of `mechanism` with the tool `any` reads `path`, as the
    heuristic of `.mcp.json` reads the root's `.mcp.json`."""
    return any(row["mechanism"] == mechanism and heuristics.heuristic_matches(row, path)
               for row in _SHARED_ROOT_FILE_ROWS)


def artifact_tools(repo_path, rows, workflows=(), plugin_parts=(), declared=(), marker_dirs=()):
    """{(mechanism, path): set of tools} for each detected row, the attribution from which the
    tool flags are derived.

    A workflow's tool is the action, engine or command that it runs. A path that a
    configuration declares or registers belongs to the declaring tool in the rows of the
    declaration's mechanism (`declared_file_tools`), and the plugin that contains it adds no
    tool, as the Claude Code plugin around a SKILL.md that a Gemini extension names as its
    context file reads the file as a skill and not as a context file. Any other row takes the
    tools with heuristics of its mechanism that read its path (`tools_reading`). A path at or
    below a plugin part also gets the plugin's tool, except a file at the root that a `root_file`
    heuristic with the tool `any` reads (`_shared_root_file`). The root `.mcp.json` of a
    repository that is a Claude Code plugin at its root therefore stays `any`, because every tool
    that reads the repository's MCP configuration reads it under "mcpServers" or in the bare
    form. A root `.mcp.json` with its servers under "servers" alone is Copilot's, because only
    Visual Studio reads that key there (`_servers_key_alone`). A `.mcp.json` below the root with a
    server under "mcpServers" is Claude Code's beside Copilot's (`_claude_reads_nested_mcp`). A plugins row that is the manifest of a
    plugin of `plugin_parts` gets that plugin's tool alone, as the plugin.json of a legacy
    Copilot CLI plugin in the skills directory of a Claude Code plugin is Copilot's: the tool of
    the plugin that contains the manifest reads it as no plugin. A path at or below the
    directory that a symlink marker names also gets the tools with heuristics of the row's
    mechanism that read the path through the link, at the marker. A SKILL.md gets no tool that
    skips it (`_skill_skippers`), at its path or through a link, such as a tool that skips a link
    to a file and Codex where it does not load the file, and one in `.agents/skills` keeps `any`
    while several readers of that directory read it. Of the tools that read it, the tool of the
    tool directory takes precedence over a tool that reads the directory for compatibility
    (`_tools_reading_a_skill`). A row that no heuristic of its
    mechanism explains and that no link or plugin reaches keeps the owner of its directory
    (`tool_for_path`). An `other` row is a file in a configuration directory that no mechanism
    covers. It takes the owner of its directory (`_tool_of_an_other_row`) and of the directory of
    each marker that reaches it. An empty set stands for a file that no specific tool owns. A
    subagent that Claude Code reads in `.claude/agents` is Claude Code's, at its path and through a
    link, although Cursor reads that directory too (`_without_the_compatibility_reader`), and a link
    adds no reader of a shared skills directory outside the reader's own directory
    (`_readers_without_their_own_link`). A skill folder in `.cursor/skills` that links to a skill
    outside that directory adds Cursor only where Cursor reads the skill at its own path
    (`_cursor_reads_at_its_own_path`).

    Reading the path alone, and crediting a marker's tool to every path below the linked
    directory, would make a `.md` rule that `.claude/rules` and `.cursor/rules` both link
    Cursor's, although Cursor reads only `.mdc` there. It would also make a nested
    `.agents/agents/<group>/<name>.md` Antigravity's, and a `.cursor/commands` file that
    `.claude/commands` links Claude's command and Cursor's skill. `repo_path` is the repository
    with the files that `tools_reading` reads for the front matter tests. A rule without a
    trigger in `.agents/rules` that `.claude/rules` links is therefore Claude's and not
    Antigravity's.
    """
    by_action = dict(workflows)
    declared_tools = defaultdict(set)
    for mechanism, path, tool in declared:
        declared_tools[(mechanism, path)].add(tool)

    def plugin(p, mechanism=None):
        """The tools of the plugin parts and manifests at or above `p`, or of the manifest at
        `p` alone for a plugins row. For a row of a mechanism, a part gives its tool only to a
        file that it reads (`_part_reads`), a hooks file only to a hooks row of a tool that
        loads a hook from it and an MCP file only to an MCP row of a tool that loads a server
        of it (`_part_loads`), and an `other` row keeps the tool of the plugin that contains
        it."""
        manifest = {tool for part, tool, _row in plugin_parts if tool and p == part}
        if mechanism == "plugins" and manifest:
            return manifest
        return {tool for part, tool, row in plugin_parts
                if tool and (p == part and _part_loads(repo_path, part, row, mechanism)
                             or p.startswith(part + "/")
                             and (mechanism in (None, "other")
                                  or _part_reads(row, part, mechanism, p, repo_path)))}

    linked = [(marker, directory, plugin(marker)) for marker, directory in marker_dirs]
    found = {}
    for mechanism, p in rows:
        below = [(marker, directory, tools) for marker, directory, tools in linked
                 if p == directory or p.startswith(directory + "/")]
        declaring = declared_tools.get((mechanism, p))
        if p in by_action:
            tools = {by_action[p]}
        elif declaring:
            tools = set(declaring)
        elif mechanism == "other":
            tools = {_tool_of_an_other_row(p)} - {None}
        else:
            explained, tools = tools_reading(mechanism, p, Path(repo_path) / p, repo_path)
            tools = _without_the_compatibility_reader(mechanism, tools)
            if not explained and not below and not plugin(p):
                tools = {tool_for_path(p)} - {None}
            if mechanism == "mcp" and tools and _claude_reads_nested_mcp(repo_path, p):
                tools = set(tools) | {"claude"}
            elif mechanism == "skills":
                tools = _tools_reading_a_skill(repo_path, p, p, tools)
        if not declaring and not _shared_root_file(mechanism, p):
            tools |= plugin(p, mechanism)
        elif (not declaring and mechanism == "mcp"
              and _servers_key_alone(repo_path, p)):
            tools = {"copilot"}
        for marker, directory, marker_tools in below:
            read_path = marker + p[len(directory):]
            through = ({tool_for_path(marker)} - {None} if mechanism == "other"
                       else _without_the_compatibility_reader(mechanism, tools_reading(
                           mechanism, read_path, Path(repo_path) / p, repo_path)[1]))
            if mechanism == "skills":
                if (_links_outside_its_directory(marker, directory)
                        and not _cursor_reads_at_its_own_path(repo_path, p)):
                    through -= _SKIPS_A_LINKED_SKILL_OUTSIDE
                through = _tools_reading_a_skill(repo_path, read_path, p, through)
                through -= _readers_without_their_own_link(p, marker, read_path)
            tools |= through | marker_tools
        found[(mechanism, p)] = tools
    return found


# The mechanism of each mechanism CSV, by the column that names its configuration artifact.
_MECHANISM_OF = {artifact: mechanism for mechanism, (_filename, artifact)
                 in heuristics.MECHANISM_CSVS.items()}


def tool_flags_of(tool_sets):
    """The six tool flags of a repository: TRUE for each tool that one of its
    configuration artifacts of a named mechanism is attributed to. The caller passes the tool
    sets of those configuration artifacts."""
    owned = set().union(*tool_sets)
    return {tool: tool in owned for tool in TOOLS}


def no_specific_tool(mechanism_flags, tool_flags):
    """Whether a repository has a detected mechanism and no tool flag: every
    configuration artifact that it has is one that no specific tool owns."""
    return any(mechanism_flags) and not any(tool_flags.values())


def file_flags_of(cf):
    """The five context-file flags of `repos.csv` from the detected context files.

    The names are compared case-insensitively, as detection compares them. A case-sensitive
    comparison would give a repository with COPILOT-INSTRUCTIONS.md a context-files.csv row and
    a FALSE flag in the same run.
    """
    # At the root and below it, where Copilot CLI reads them too (the `nested:` rows), as the
    # other flags count their file at any depth.
    copilot_files = {p.lower() for p in COPILOT_CONTEXT_FILES + NESTED_CONTEXT_FILES}
    copilot_dirs = tuple(p.lower() + "/" for p in CONTEXT_DIRS_SUFFIX + NESTED_CONTEXT_DIRS_SUFFIX)

    def copilot_file(f):
        lowered = "/" + f.lower()
        return (any(lowered.endswith("/" + p) for p in copilot_files)
                or any("/" + d in lowered for d in copilot_dirs))

    # A root file named in another case sets the flag of its lower-case name.
    root_any_case = {f.lower() for f in cf if "/" not in f and f.lower() in ROOT_ANY_CASE}
    return {
        "claude.md": any(os.path.basename(f) == "CLAUDE.md" for f in cf)
                     or "claude.md" in root_any_case,
        "gemini.md": any(os.path.basename(f) == "GEMINI.md" for f in cf)
                     or "gemini.md" in root_any_case,
        "agents.md": any(
            os.path.basename(f) in ("AGENTS.md", "AGENTS.override.md") for f in cf
        ) or "agents.md" in root_any_case,
        "copilot-instructions": any(copilot_file(f) for f in cf),
        "cursorrules": any(os.path.basename(f) == ".cursorrules" for f in cf),
    }


SPEC_DIRS = (heuristics.spec_dir_paths(_PATTERNS)
             + paths_for(_PATTERNS, "other", "unclassified_dir"))
ROOT_SPEC_FILES = paths_for(_PATTERNS, "other", "root_file")
SPEC_BASENAMES = paths_for(_PATTERNS, "other", "basename")


def _spec_directories(repo_path):
    """Yield the paths of `SPEC_DIRS` in the tree, at the root and below it, without a directory
    that is no spec directory (`paths.is_spec_directory`), such as a Python package named
    `_agent`. The marker of a tool directory that was a link stays among them."""
    for sd in _expanded(repo_path, SPEC_DIRS):
        d = repo_path / sd
        if not d.is_dir() or paths.is_spec_directory(d):
            yield sd


def _collect_all_spec_files(repo_path, markers=()):
    """Collect every file inside the spec directories, and the spec files at the root.

    Spec directories are read at any depth, as the detectors read them, and the unclaimed files
    of a mechanism directory below the root stay visible. The ignore basenames
    (`SPEC_BASENAMES`) are listed at any depth, as the collected file cleanup lists them.

    A marker in a spec directory that resolves to a directory is read as the directory that it
    stands for, at the paths where the files are. utooland/utoo links `.claude/agents` to
    `agents/`. If the marker were not read as its directory, the reports in `agents/` that
    Claude Code skips as subagents would be in no CSV. The marker of a tool directory that is a
    link, such as a `.claude` that leads to `config/claude`, is read as its directory the same
    way (`linked_spec_files`). The marker itself is a file with the tool directory's name. One
    that resolves to no directory, such as the marker of a link out of the repository, is a
    candidate, as a marker in a spec directory that resolves to none is.
    """
    found = set()
    for sd in _spec_directories(repo_path):
        d = repo_path / sd
        if d.is_file() and d.name in paths.TOOL_DIRECTORIES:
            found.add(sd)
        if not d.is_dir():
            continue
        for p in _listed(d, d.rglob("*")):
            if p.is_file():
                found.add(str(p.relative_to(repo_path)))
    # The places below the root that a `nested:` row reads, which the collected file cleanup
    # keeps whole. Their files that no row claims become `other` rows, as the files of the
    # root's `.github` do.
    for places in _nested_places(repo_path).values():
        for rel in places:
            d = repo_path / rel
            found.update(str(p.relative_to(repo_path))
                         for p in ([d] if d.is_file() else _listed(d, d.rglob("*")))
                         if p.is_file())
    found.update(linked_spec_files(repo_path, markers))
    for file in paths.list_repo_files(Path(repo_path), heuristics.VENDORED_DIRS):
        if (file.name in SPEC_BASENAMES
                and not paths.is_discarded_path(file.relative_to(repo_path).as_posix())):
            found.add(file.relative_to(repo_path).as_posix())
    for rf in ROOT_SPEC_FILES:
        if _file_in_its_case(repo_path, rf):
            found.add(rf)
    return found


def linked_spec_files(repo_path, markers) -> dict:
    """{path of a file: the path at which a tool reads it} for the files of the directories that
    the markers in spec directories link to, where the file is in no spec directory itself.

    `unmatched paths` explains a file of `other` by the path that the tool reads. A
    `skills/README.md` behind a `.claude/skills` link is explained as `.claude/skills/README.md`
    would be. A file that `other` reads in a spec directory anyway, such as
    `.agents/skills/README.md` behind the same link, keeps its own path. The marker of a tool
    directory that is a link stands in place of a spec directory, and the files of `config/claude`
    behind a `.claude` that links to it are explained as `.claude/<path>`.
    """
    spec_paths = list(_spec_directories(repo_path))
    spec_dirs = [sd for sd in spec_paths if (repo_path / sd).is_dir()]
    root = Path(repo_path).resolve()
    found = {}
    for marker in markers:
        if marker not in spec_paths and not any(marker.startswith(sd.rstrip("/") + "/")
                                                for sd in spec_dirs):
            continue
        target = _resolve_dir(repo_path, marker)
        # The collector copies no target that contains the link, because such a target would be
        # the tree around the link.
        if target is None or (root / marker).is_relative_to(target):
            continue
        for p in sorted(_listed(target, target.rglob("*"))):
            rel = _repo_relative(repo_path, p)
            if p.is_file() and not any(rel.startswith(sd.rstrip("/") + "/") for sd in spec_dirs):
                found.setdefault(rel, f"{marker}/{p.relative_to(target).as_posix()}")
    return found


def _collect_known_files(repo_path, settings, context_files, skills, subagents, commands, hooks, rules, mcp,
                         markers=(), plugins=(), lsp=()):
    """Collect the set of file paths already captured by other detectors."""
    known = set()
    # plugin manifests and marketplace catalogs: a path each
    known.update(plugins)
    # A plugin package owns its folder, as a skill owns its folder.
    packages = _plugin_packages(repo_path)
    for entry in plugins:
        if entry in packages:
            folder = repo_path / packages[entry]
            known.update(str(p.relative_to(repo_path)) for p in _listed(folder, folder.rglob("*"))
                         if p.is_file())
    # LSP server configurations: a path each
    known.update(lsp)
    # settings: list of relative paths
    known.update(settings)
    # context_files: list of relative paths. Only the paths inside spec directories count,
    # because a CLAUDE.md at the repository root is never a candidate for `other`. A spec
    # directory can be at any depth, and a packages/web/.claude/CLAUDE.md is the context file
    # that it was detected as, not also an unclaimed file. So is a context file in a directory
    # that a marker in a spec directory or of a tool directory leads to (`linked_spec_files`),
    # such as the instructions file that the Codex config of a linked `.codex` names.
    linked = linked_spec_files(repo_path, markers)
    for cf in context_files:
        if any(part in SPEC_DIRS for part in cf.split("/")[:-1]) or cf in linked:
            known.add(cf)
    # skills: list of dicts with a "skills.md" key. Each skill adds its entire subdirectory. A
    # plugin's root skill outside every skills directory has the plugin root as its directory.
    # kvcache-ai/Mooncake keeps plugins inside .claude/skills/<name>/, where the SKILL.md is
    # also a skill of the skills directory and owns its whole folder, as every such skill does.
    # A root skill that an Antigravity registration selects counts as a root skill too
    # (`paths.registered_root_skills`).
    skills_dirs = tuple(_expanded(repo_path, SKILLS_DIRS))
    root_skills = ([rel for rel, _heuristic_row, kind in _plugin_components(Path(repo_path))
                    if kind == "root_skill"]
                   + paths.registered_root_skills(Path(repo_path)))
    root_skills = {rel for rel in root_skills
                   if not any(rel.startswith(d.rstrip("/") + "/") for d in skills_dirs)}
    for sk in skills:
        skill_md = sk["skills.md"]
        # Only a real SKILL.md owns its directory. A merged command file is directly in
        # .claude/commands, and claiming that parent would claim every unrelated file beside it.
        if Path(skill_md).name.lower() != SKILL_MARKER.lower():
            known.add(skill_md)
            continue
        skill_dir = str(Path(skill_md).parent)
        # Every file of the skill directory. For a plugin's root skill, which has the plugin root
        # as its directory, the SKILL.md and the directories beside it that belong to the skill.
        if skill_md in root_skills:
            known.add(skill_md)
            folders = [repo_path / skill_dir / d for d in SKILL_SUPPORT_DIRS]
        else:
            folders = [repo_path / skill_dir]
        for sd in folders:
            if sd.is_dir():
                for p in _listed(sd, sd.rglob("*")):
                    if p.is_file():
                        known.add(str(p.relative_to(repo_path)))
    # subagents: list of dicts with a "subagent" key, and the files of each subagent's memory.
    for sa in subagents:
        known.add(sa["subagent"])
        if sa["memory"] == "TRUE":
            agents_dir = os.path.dirname(sa["subagent"])
            for mem in _memory_paths(agents_dir, sa["name"]):
                known.update(_memory_files(repo_path, mem))
    # commands: list of dicts
    for cmd in commands:
        known.add(cmd["command"])
    # hooks: list of relative paths
    known.update(hooks)
    # rules: list of dicts
    for rl in rules:
        known.add(rl["rule"])
    # mcp: list of relative paths
    known.update(mcp)
    # A followed marker stands for the directory that the detector read, not a stray file. A
    # marker that does not resolve is not among `markers`, and a link to a target that the
    # collector did not copy stays visible in `other`.
    known.update(markers)
    return known


def _copilot_marketplace(path):
    """Check the fields that distinguish a Copilot marketplace from other catalogs
    (`paths.copilot_marketplace_document`)."""
    try:
        data = _load_json(path)
    except (OSError, ValueError, RecursionError):
        return False
    return paths.copilot_marketplace_document(data)


def _codex_marketplace(repo_path, rel, catalog_root):
    """Whether the Codex marketplace at `rel`, with the marketplace root `catalog_root`, is a
    marketplace from which Codex can install a plugin (`paths.codex_marketplace_installs`). A
    file that is a link is read as the file that its marker names, as Codex reads it through the
    link."""
    try:
        data = _load_json(_followed_file(Path(repo_path), Path(repo_path) / rel))
    except (OSError, ValueError, RecursionError):
        return False
    return paths.codex_marketplace_installs(data, catalog_root)


def _plugin_packages(repo_path):
    """{entry module: package folder}, repository-relative, for each plugin package of the tree:
    a folder directly in a directory of `PLUGIN_PACKAGE_ROWS` with an entry module that the row's
    mode names (`heuristics.package_entries`).

    The entry module is the first match in the order of the names and then of the extensions.
    The names are compared with the names on disk, which gives the same result on a
    case-insensitive volume, where OpenCode would open `Index.ts` for `index.ts`, as on the
    Linux file system of a run. A folder that is a link is read where the link leads inside the
    repository, and the row names a file that the repository has. A link that the collector
    stored as a marker is a file, not a folder.
    """
    root = Path(repo_path).resolve()
    found = {}
    for pattern, mode in PLUGIN_PACKAGE_ROWS:
        names, extensions = heuristics.package_entries(mode)
        for rel in _expanded(repo_path, [pattern]):
            directory = _resolve_dir(repo_path, rel)
            if directory is None:
                continue
            for folder in sorted(_listed(directory, directory.iterdir())):
                if not folder.is_dir() or paths.is_vendored(folder, heuristics.VENDORED_DIRS):
                    continue
                real = folder.resolve()
                if not real.is_relative_to(root):
                    continue
                files = {p.name for p in real.iterdir() if p.is_file()}
                entry = next((name + extension for name in names for extension in extensions
                              if name + extension in files), None)
                if entry is not None:
                    found.setdefault(_repo_relative(repo_path, real / entry),
                                     _repo_relative(repo_path, real))
    return found


_EXPORT = re.compile(r"^\s*export\b|\bmodule\.exports\b", re.MULTILINE)


def _opencode_module_loads(repo_path, path, folder):
    """Whether OpenCode loads a tool or a plugin from the module at `path` in `folder`, one of
    its tool or plugin folders. OpenCode takes each export of a plugin module as a plugin
    (`getLegacyPlugins` in `packages/opencode/src/plugin/index.ts` of anomalyco/opencode at
    e3c4eb242585dcd77be1a867391b7c25fd47ce52), and a module without an export, such as the vitest
    file .opencode/plugins/validate-change.unit.spec.ts of event-driven-io/Pongo, yields none. Of a
    tool module it keeps an export only with "args", "description" and "execute" (`isPluginTool`
    in `packages/opencode/src/tool/registry.ts`), which cloudflare/workerd's helper
    .opencode/tools/capnp.ts does not have. The text test reads the words, not the module's
    values. It counts a plugin module with an export that OpenCode 1 refuses, which is right for
    the `{id, setup}` default export that OpenCode 2 loads (`PluginModule.load` in
    `packages/core/src/plugin/module.ts` at ba546ec1598a70db2570e9c6258028d955ba8f58) and wrong
    for any other export that is no plugin, and it misses a CommonJS `exports.X =` and a tool
    that a module re-exports. docs/steps/detection.md states these limits."""
    text = _file_text(repo_path, path)
    if not _EXPORT.search(text):
        return False
    if os.path.basename(folder.replace(os.sep, "/").rstrip("/")) in ("tool", "tools"):
        return all(re.search(rf"\b{word}\b", text) for word in ("args", "description", "execute"))
    return True


def detect_plugins(repo_path, *, include_registered=True):
    """Detect the plugin bundles that a repository contains, one row per manifest.

    A marketplace catalog counts beside a manifest. The vendor pages describe a catalog as the
    list of plugins and where to fetch them. docs/steps/detection.md gives the share of
    catalogs that name sources inside the repository that contains the catalog, and a catalog
    is evidence that the repository contains plugins rather than only a pointer elsewhere.

    A manifest counts wherever its own directory is. Claude Code's page describes a plugin as
    its own directory, and the repositories put such directories under
    `.claude/plugins/<name>/`, under `.claude-plugin/plugins/<name>/` and beside a package.
    `_anchored`, which every other file row goes through, stops at a mechanism directory. Read
    under the nestable roots alone, the heuristics' path would leave such manifests in
    `other`. Rows come out in path order, because one path of the heuristics can match more
    than once in a repository.
    """
    found = []
    holders = ("",) + _directories(repo_path)
    for pattern in PLUGIN_FILES + COPILOT_MARKETPLACES:
        # The folder that contains an Antigravity manifest identifies it, and the placeholder's
        # roots are such folders.
        if heuristics.plugin_placeholder(pattern):
            found.extend(rel for rel in _expanded_files(repo_path, [pattern])
                         if (repo_path / rel).is_file())
            continue
        parent, _, name = pattern.rpartition("/")
        low = parent.lower()
        for holder in holders:
            here = holder.lower()
            if here != low and not (low and here.endswith("/" + low)):
                continue
            real = _find_case_insensitive(repo_path, f"{holder}/{name}" if holder else name)
            if real is not None and (pattern not in COPILOT_MARKETPLACES
                                     or _copilot_marketplace(repo_path / real)) and (
                    pattern != CODEX_MARKETPLACE or _codex_marketplace(
                        repo_path, real, real[:len(real) - len(CODEX_MARKETPLACE)].rstrip("/"))):
                found.append(real)
    for name in PLUGIN_BASENAMES:
        for p in sorted(_listed(repo_path, _named(repo_path.rglob("*"), name))):
            if p.is_file():
                found.append(str(p.relative_to(repo_path)))
    for name in PLUGIN_MANIFESTS:
        found.extend(_agent_plugins_documents(repo_path, name,
                                              "basename_json_plugin_manifest"))
    for rel in _expanded(repo_path, PLUGIN_MODULE_DIRS):
        d = _resolve_dir(repo_path, rel)
        if d is None:
            continue
        found.extend(_repo_relative(repo_path, p)
                     for p in sorted(_listed(d, d.iterdir()))
                     # OpenCode's pattern, "{plugin,plugins}/*.{ts,js}", is case-sensitive.
                     if p.is_file() and p.name.endswith(extensions_for(PLUGIN_MODULE_MODE))
                     and _opencode_module_loads(repo_path, p, rel))
    found.extend(_plugin_packages(repo_path))
    if include_registered:
        found.extend(_registered(repo_path, "plugins"))

    return sorted(dict.fromkeys(found))


def detect_other(repo_path, settings, context_files, skills, subagents, commands, hooks, rules, mcp,
                 markers=(), plugins=(), lsp=()):
    """Detect the files of spec directories that no other detector captured.

    A file that a GitHub Agentic Workflow imports is part of that workflow and not a file of
    its own, as a skill's support files are part of the skill. The prompt file that an agent
    action of a workflow reads is part of the workflow too (`paths.action_prompt_files`), the
    prompt file that an agent of an OpenCode configuration names is part of the agent
    (`paths.opencode_prompt_files`), and the instructions file that a Codex role names in
    `model_instructions_file` is part of the role (`_declared_subagents`).
    """
    all_files = _collect_all_spec_files(repo_path, markers)
    known = _collect_known_files(repo_path, settings, context_files, skills, subagents,
                                 commands, hooks, rules, mcp, markers, plugins, lsp)
    known.update(paths.agentic_workflow_imports(Path(repo_path), WORKFLOW_ENGINES))
    known.update(paths.action_prompt_files(Path(repo_path)))
    known.update(paths.opencode_prompt_files(Path(repo_path)))
    known.update(_declared_subagents(repo_path)[1])
    return sorted(all_files - known)


# The named mechanisms, in the order in which the output schema lists them. "other" is absent,
# because it contains the files that no heuristic explains, and a repository with files only
# there has no configuration that this dataset can describe.
NAMED_MECHANISM_DETECTORS = (
    detect_context_files,
    detect_settings,
    # A settings row too, which `detect_settings` cannot find, because the action inside the
    # file identifies it and the path does not. Without this detector, a repository with a
    # renamed agent workflow as its only configuration would fail the keep check and leave the
    # configured repositories.
    detect_agent_workflows,
    detect_skills,
    detect_subagents,
    detect_commands,
    detect_hooks,
    detect_rules,
    detect_mcp,
    detect_plugins,
    detect_lsp,
)


def has_detected_mechanism(repo_path) -> bool:
    """Whether any named mechanism is detected in this repository.

    This function decides which repositories are configured repositories, and
    agentcontext/steps/collected_file_cleanup.py applies it as its keep check. A tool directory
    alone does not count, because an empty `.claude/` is a directory, not a configuration. The
    function is defined here, next to the detectors that it calls, and the check and the
    detection therefore agree.

    Returns at the first detector that finds something, which in the common case costs one pass
    over the files rather than one per mechanism.
    """
    path = Path(repo_path)
    return any(detector(path) for detector in NAMED_MECHANISM_DETECTORS)


# ---------------------------------------------------------------------------
# What the file of the configuration artifact contains
# ---------------------------------------------------------------------------
# This step reads the language, the line count and the pointer classification from the
# validated tree, which every other column describes. Read from a fresh clone, the language
# would describe another version of the configuration artifact than the rest of the row.
#
# The reads are in `main` rather than in the detectors. As the keep check of the collected file
# cleanup, `has_detected_mechanism` calls the detectors once per repository of the whole input
# list and keeps nothing but a boolean.

# Encodings tried in order, UTF-8 first. latin-1, the last, decodes any bytes.
_ENCODINGS = ("utf-8", "utf-16", "latin-1")

# How much of a file the language detector reads. `line_count` counts the whole file, because a
# count over part of the file would not be its line count.
_LANGUAGE_SAMPLE = 10240

# Below this many non-blank characters, a detected language is noise rather than a result.
# Such a file can be a symlink, which the collector stores as a one-line file that contains its
# target, and a language detected there would describe a file that the row does not name.
_LANGUAGE_MIN_CHARS = 50

# The value for a configuration artifact with a language that could not be decided.
_NO_LANGUAGE = "n/a"

# A file with more lines than this is not a pointer to another file, and the reference patterns
# do not apply to it.
MAX_REFERENCE_LINES = 10
# A file with a reference and at most this many meaningful lines is a reference without a
# pointer phrase, because so short a file says nothing of its own.
MAX_MEANINGFUL_LINES = 3

# File reference extraction patterns (ordered by priority).
REF_PATTERNS = [
    # @ reference: @AGENTS.md, @./AGENTS.md, @../AGENTS.md, @.github/copilot-instructions.md.
    # Only a leading "./" or "/" is left out, because Claude Code resolves the path from the
    # importing file, so a leading ".." or the dot of a dot folder is part of the path.
    re.compile(r'@(?:\.?/)?([A-Za-z0-9._/\-]+\.md)\b', re.IGNORECASE),
    # Bare filename on its own line: AGENTS.md
    re.compile(r'^\s*([A-Za-z0-9._/\-\\]+\.md)\s*$', re.IGNORECASE | re.MULTILINE),
    # Markdown link: [text](path.md)
    re.compile(r'\[.*?\]\(([^)]*\.md)\)', re.IGNORECASE),
    # Backtick reference: `AGENTS.md`
    re.compile(r'`([A-Za-z0-9._/\-\\]+\.md)`', re.IGNORECASE),
    # URL reference
    re.compile(r'(https?://github\.com/[^\s)]+\.md)', re.IGNORECASE),
]

# Phrases that confirm that a short file is a pointer
POINTER_PHRASES = re.compile(
    r'(?:see|read|refer\s+to|follow(?:\s+the)?\s+instructions?\s+(?:from|in)|'
    r'use\s+instructions?\s+from|always\s+read|check|consult|'
    r'instructions?\s+(?:are|is)\s+(?:in|at|located))',
    re.IGNORECASE,
)

_lingua_detector = None


def _get_lingua_detector():
    """The one detector, built on first use, because building it is slow."""
    global _lingua_detector
    if _lingua_detector is None:
        _lingua_detector = LanguageDetectorBuilder.from_all_languages().build()
    return _lingua_detector


def no_text(path) -> bool:
    """Whether the file at `path` is no text, such as an image or a file that git-crypt
    encrypted, which opens with `\\0GITCRYPT\\0`. Such a file has a NUL byte and does not open
    with a UTF-16 byte order mark. A text in UTF-16, which `read_artifact` reads, has NUL bytes
    too. A file that cannot be read is left to `read_artifact`, which logs it."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    return b"\0" in data and not data.startswith((b"\xff\xfe", b"\xfe\xff"))


def read_artifact(path):
    """The configuration artifact's whole text, or None when the file cannot be read.

    latin-1, the last encoding tried, decodes any bytes. None therefore means that the file
    could not be opened, such as a link to a target that was not collected or a file that the
    step may not read. The empty string would make the row record a file of no lines and no
    language.
    """
    for encoding in _ENCODINGS:
        try:
            with open(path, encoding=encoding) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError as error:
            logs.warning('detection', f"Could not read {path} ({error}). Its language, line "
                                      f"count and reference columns stay blank.")
            return None
    return None


def artifact_text(repo_path, mechanism, measured):
    """The text that the columns read from a configuration artifact describe, for `measured`,
    the path that `measured_artifact` returned, or None where the step reads no text.

    A context file that the validated tree holds as a real symlink reads as the marker that
    `paths.write_marker` writes for a link, its target and a newline, and is published as a
    reference to its target, as a marker is. rsync keeps a link inside a directory that the
    collector copies whole, and `paths.write_marker` leaves a link to a file of the tree in
    place, such as gomlx/gomlx's `.agents/CLAUDE.md`, which links to the `AGENTS.md` beside it.
    Read through the link, the row would describe that AGENTS.md. A link of another mechanism is
    read through, as `_followed_file` reads the file that a marker names, because the tool reads
    the file that the link leads to.

    Any other file reads as `read_artifact` reads it. The function returns None for a file that
    is no text (`no_text`), such as a CLAUDE.md that git-crypt encrypted, because latin-1, the
    last encoding that `read_artifact` tries, decodes any bytes into noise, and for a file that
    cannot be read, which `read_artifact` logs. `no_text` tells the two apart."""
    path = Path(repo_path) / measured
    if mechanism == "context_file" and path.is_symlink():
        target = os.readlink(path)
        # A target that is not UTF-8 is escaped as `paths.write_marker` escapes it.
        return target.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace") + "\n"
    if no_text(path):
        return None
    return read_artifact(path)


def detect_language(content):
    """The configuration artifact's natural language, or `n/a`.

    There is no extension filter. A TOML command or a `.rules` file gets a language as a
    Markdown file does, and every row of the column follows one method.
    """
    if not LINGUA_AVAILABLE:
        return _NO_LANGUAGE
    sample = content[:_LANGUAGE_SAMPLE]
    if not sample or len(sample.strip()) < _LANGUAGE_MIN_CHARS:
        return _NO_LANGUAGE
    try:
        language = _get_lingua_detector().detect_language_of(sample)
    except Exception as e:  # noqa: BLE001 - one configuration artifact must not end the run
        logs.warning('detection', f"Language detection raised {e.__class__.__name__} ({e}).")
        return _NO_LANGUAGE
    if not language:
        return _NO_LANGUAGE
    return str(language).replace("Language.", "").title()


def extract_references(content):
    """Extract all referenced .md files from content."""
    refs = []
    seen = set()
    for pattern in REF_PATTERNS:
        for match in pattern.finditer(content):
            ref = match.group(1)
            if ref not in seen:
                seen.add(ref)
                refs.append(ref)
    return refs


# A line break: LF, CRLF or CR. `str.splitlines` also ends a line at a vertical tab, a form feed,
# the separators U+001C to U+001E, NEL (U+0085), U+2028 and U+2029. A file can contain such a
# character inside a line, such as a U+2028 in a code span of a Markdown spec, and a count that
# ended a line there would count a line that the file does not have.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def split_lines(content):
    """The lines of a text without their line breaks. A line ends at LF, CRLF or CR alone. A
    line break at the end of the text ends the last line and starts no empty one, as with
    `str.splitlines`. Every line count that the dataset and its analysis publish is the length of
    this list."""
    lines = _LINE_BREAK.split(content)
    if lines[-1] == "":
        lines.pop()
    return lines


def meaningful_lines(content):
    """Return non-empty, non-header-only lines (for classification purposes)."""
    lines = []
    for line in split_lines(content):
        stripped = line.strip()
        if not stripped:
            continue
        # Skip pure markdown headers with no other content
        if re.match(r'^#+\s*$', stripped):
            continue
        lines.append(stripped)
    return lines


def classify(content, max_lines=MAX_REFERENCE_LINES):
    """Classify a context file and return (line_count, is_reference, referenced_file).

    There is no `is_empty`, because the collected file cleanup deletes every empty file before
    this step runs.
    """
    line_count = len(split_lines(content))

    if not content or not content.strip():
        return line_count, False, ''

    # Only consider short files
    if line_count > max_lines:
        return line_count, False, ''

    refs = extract_references(content)
    if not refs:
        return line_count, False, ''

    ref_str = ';'.join(refs)
    meaningful_count = len(meaningful_lines(content))

    # Tier 1: very short files with a reference
    if meaningful_count <= MAX_MEANINGFUL_LINES:
        return line_count, True, ref_str

    # Tier 2/3: longer files with a pointer phrase and a reference
    if POINTER_PHRASES.search(content):
        return line_count, True, ref_str

    return line_count, False, ''


def load_ai_authorship(path=AI_AUTHORSHIP_CSV):
    """The AI authorship cache, keyed `(repo_name, head_sha, artifact)`.

    It is a second cache beside `file_history.csv` rather than more columns of that file, and
    `caches.py` gives the reason. A missing file gives an empty cache, and a tree without it
    writes blank AI columns rather than failing. A file with a header that is not the cache's
    raises an error rather than joining on a key that is not there.
    """
    return caches.AiAuthorshipCache.load(path)


def build_detail_row(mechanism, item, *, base, repo_dir, repo_name, branch, head_sha,
                     cache, ai_authorship, version_column):
    """One published row, and the fact CSVs that had no row for it.

    `item` is what the detector produced: a path for a mechanism with a path as its whole
    row, and a dict for a mechanism with rows that also describe the configuration artifact.
    Returns `(row, missing)`, where `missing` lists the fact CSVs that had no row.
    """
    _, artifact_column = heuristics.MECHANISM_CSVS[mechanism]
    row = dict(base)
    row.update(item if isinstance(item, dict) else {artifact_column: item})
    artifact = row[artifact_column]
    row["github_link"] = github_link(repo_name, branch, artifact)
    row.update(version_column)
    missing = []

    git = git_columns_for(cache, repo_name, head_sha, mechanism, artifact)
    if git is None:
        missing.append(("file_history.csv", mechanism, repo_name, artifact))
        git = {c: "" for c in heuristics.HISTORY_COLUMNS}
    row.update(git)

    wants_language = mechanism in heuristics.LANGUAGE_MECHANISMS
    wants_references = mechanism == "context_file"
    if wants_language or wants_references:
        measured = measured_artifact(repo_dir, mechanism, artifact)
        # Blank where the file could not be read, because `n/a` records that a text was read and
        # no language was found in it, or that the file is no text.
        language = ""
        if measured is None:
            # The marker of a link to a target that the tree lacks. Its one line is a path, and
            # the row is published as a file that was not read.
            logs.info('detection', f"{artifact} in {repo_name} is a symlink marker, and the "
                                   f"validated tree lacks its target. Left its language blank.")
            content = None
        else:
            content = artifact_text(repo_dir, mechanism, measured)
            if content is None and no_text(Path(repo_dir) / measured):
                # A file that is no text has no language, which `n/a` records. A language read
                # from its bytes would be noise, such as Vietnamese for a CLAUDE.md that
                # git-crypt encrypted.
                language = _NO_LANGUAGE
        if wants_language:
            row["language"] = language if content is None else detect_language(content)
        if wants_references:
            if content is None:
                row.update({"line_count": "", "is_reference": "", "referenced_file": ""})
            else:
                line_count, is_reference, referenced = classify(content)
                row["line_count"] = str(line_count)
                row["is_reference"] = str(is_reference).upper()
                row["referenced_file"] = referenced

    if wants_references:
        # Blank rather than FALSE where the AI cache has no row for this configuration artifact
        # at this commit. A blank records that the cache has no value, and FALSE would record
        # that the file has no AI commit.
        values = ai_authorship.get(caches.AiAuthorshipKey(repo_name, head_sha,
                                                                  artifact))
        row.update(values.as_row() if values is not None
                   else {c: "" for c in heuristics.AI_COLUMNS})
    return row, missing


def preferred_facts(cache, repo_name, head_sha, mechanism, artifact):
    """The facts of one path at one commit for `mechanism`, or None. The cloning step's row
    counts where it is complete, and otherwise the row that the file history step measured for
    the mechanism. The cloning step writes only complete rows, and the file history step
    measures a row only where the cache had none for the commit. Where both rows exist, the
    cloning step's row is therefore the later measurement, such as after a collection that
    measured the path again at the same commit under a newer collector or git."""
    collected = cache.get(caches.FileHistoryKey(repo_name, head_sha, "", artifact))
    own = cache.get(caches.FileHistoryKey(repo_name, head_sha, mechanism, artifact))
    if collected is not None and (own is None or collected.is_complete()):
        return collected
    return own


def git_columns_for(cache, repo_name, head_sha, mechanism, artifact):
    """The Git columns for one configuration artifact, or None when the cache has no row.

    The cloning step writes its row without a mechanism, because it reads a file's history
    before the detection decides which CSV the file belongs to. The file history step writes a
    row for the mechanism. The cloning step's complete row counts first, then the row for this
    mechanism (`preferred_facts`), then a row of the path under another mechanism
    (`for_artifact`).
    """
    facts = preferred_facts(cache, repo_name, head_sha, mechanism, artifact)
    if facts is None:
        facts = cache.for_artifact(repo_name, head_sha, artifact)
    if facts is None:
        return None
    return {column: value for column, value in facts.as_row().items()
            if column in heuristics.HISTORY_COLUMNS}


def build_planning_row(path, heuristic_row, *, base, repo_dir, repo_name, branch, head_sha,
                       cache, version_column):
    """One row of `planning-documents.csv`, and the fact CSVs that had no row for it.

    The Git columns come from `file_history.csv`, as for a configuration artifact, under the
    key `planning_schema.HISTORY_KEY`. `language` is read from a Markdown file alone, because
    the other files of a planning folder are scripts, JSON and YAML, for which a language
    detector returns noise. `line_count` is read from every text file. A file with a NUL byte
    and no UTF-16 byte order mark, such as an image, is no text and has `n/a` and a blank line
    count. Both columns are blank where the file could not be read.
    """
    row = dict(base)
    row.update({planning_schema.PATH_COLUMN: path, "framework": heuristic_row["framework"],
                "document": heuristic_row["document"],
                "github_link": github_link(repo_name, branch, path)})
    row.update(version_column)
    missing = []
    git = git_columns_for(cache, repo_name, head_sha, planning_schema.HISTORY_KEY, path)
    if git is None:
        missing.append(("file_history.csv", planning_schema.HISTORY_KEY, repo_name, path))
        git = {c: "" for c in heuristics.HISTORY_COLUMNS}
    row.update(git)
    target = Path(repo_dir) / path
    binary = no_text(target)
    content = None if binary else read_artifact(target)
    if binary:
        row.update({"language": _NO_LANGUAGE, "line_count": ""})
    elif content is None:
        row.update({"language": "", "line_count": ""})
    else:
        markdown = path.lower().endswith(planning_schema.LANGUAGE_SUFFIXES)
        row["language"] = detect_language(content) if markdown else _NO_LANGUAGE
        row["line_count"] = str(len(split_lines(content)))
    return row, missing


# The directory in which the cloning step keeps one shard for each repository with a commit
# history that it read, with the commit rows that it found there. The name repeats
# `repository_cloning.COMMITS_DIR_NAME`, and a test checks that the two agree. When the shards
# are on disk, the restriction reads them. The commit CSV beside the output CSVs then depends
# only on the shards and the published set, and a second detection run over a larger published
# set gets every row back. Restricting the CSV in place would not, because a change to the
# heuristics that brings a repository into the published set would then publish it with no
# commit rows.
COMMITS_DIR = STAGE_DIR / "collect_commits"
# The report that the commit reclassification writes beside the commit CSV. It names the
# repositories with shards that the reclassification left out of the CSVs.
RECLASSIFICATION_REPORT_NAME = "commit-reclassification.json"
# The rows without a file history that a run names in its log, after it counts them all.
WITHOUT_HISTORY_NAMED = 10

# The column that names the configuration artifact, by the filename of its mechanism CSV, for the
# warning of `_encodable_row`.
_ARTIFACT_COLUMN_OF_CSV = dict(heuristics.MECHANISM_CSVS.values())

# A lone surrogate, the one kind of code point that UTF-8 cannot encode. JSON can escape one, as
# in `"\ud800"`, and `json.loads` returns it in the string.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _encodable_row(filename, row):
    """`row` with each lone surrogate of its values replaced with U+FFFD, and a warning that
    names the repository, the configuration artifact and the columns, or `row` itself where every
    value encodes as UTF-8.

    `caches.replace_file` writes strict UTF-8. A value that it cannot encode would stop the step
    after the CSVs written before it had been replaced, and again at every later Start, because
    the tree does not change. A value with U+FFFD keeps the row and its other columns. A blank
    value would state that the file was not read, and a dropped row would remove a
    configuration artifact and change the flags of its repository.
    """
    replaced = {column: _SURROGATE.sub("\ufffd", value) for column, value in row.items()
                if isinstance(value, str) and _SURROGATE.search(value)}
    if not replaced:
        return row
    row = {**row, **replaced}
    artifact_column = _ARTIFACT_COLUMN_OF_CSV.get(filename)
    artifact = f" for {row[artifact_column]}" if artifact_column in row else ""
    logs.warning('detection', f"The column(s) {', '.join(sorted(replaced))} of the row of "
                              f"{row.get('repo_name', '')}{artifact} in {filename} contain a lone "
                              f"surrogate, which UTF-8 cannot encode. Wrote each lone surrogate "
                              f"as U+FFFD.")
    return row


def write_output_table(filename, columns, rows, output_dir=None) -> None:
    """Write one CSV of this step, replacing it in a single operation.

    Every CSV that this step writes is tracked, and every later step reads one. A kill between
    the truncate and the flush would leave a file that the next step reads, without a warning,
    as a CSV with no columns rather than refusing it. A value that UTF-8 cannot encode is
    written with U+FFFD (`_encodable_row`).

    The mechanism CSVs go through the wrapper `write_sub` below, and `test_heuristics` reads its
    calls by AST to compare each filename with the header beside it. The main CSV and the
    excluded list take their headers from a variable. They call this function directly and
    stay out of that comparison.
    """
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(_encodable_row(filename, row))
    caches.replace_file((output_dir or OUTPUT_DIR) / filename, text.getvalue())


def write_sub(filename, columns, rows) -> None:
    """One of the mechanism CSVs, by the name and the header the heuristics give it."""
    write_output_table(filename, columns, rows)


def reclassification_left_out(report: Path, commits_dir: Path) -> set:
    """The repositories with shards that the commit reclassification left out of the commit
    CSVs, as the report at `report` records them, as long as each repository's per-repository
    commit record is still the record that the reclassification read.

    The reclassification leaves out the shards of a repository if its evidence failed
    validation or if other commit heuristics classified it
    (`commit_reclassification.left_out_shards`). A repository with a record that changed since,
    such as after a new collection of the repository, is not left out. Without a report, no
    shard is left out.
    """
    try:
        repositories = json.loads(report.read_text(encoding="utf-8"))["repositories"]
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, KeyError, TypeError) as error:
        logs.warning('detection', f"Could not read {report} ({error}). Left no shard out of the "
                                  f"commit CSVs for the commit reclassification.")
        return set()
    left_out = set()
    for repo, result in (repositories.items() if isinstance(repositories, dict) else ()):
        if not isinstance(result, dict) or not result.get("left_out"):
            continue
        try:
            record = commits.shard_paths(Path(commits_dir), repo)[1].read_bytes()
        except OSError:
            continue
        if hashlib.sha256(record).hexdigest() == result.get("record_sha256"):
            left_out.add(repo)
    return left_out


def restrict_ai_commits(path, published, commits_dir=None, other_tools=False) -> int:
    """Write the commit CSV's rows for the repositories that this step publishes.

    The cloning step reads the commit history of every repository that contributed a file,
    including repositories that detection does not publish. The restriction runs on every run
    rather than as a one-time correction, and the CSV beside the mechanism CSVs describes the
    same repositories as they do. The rows come from the shards under `commits_dir` when there
    are any, which puts a repository that the published set gains into the CSV. The shards that
    the commit reclassification left out, as its report beside the CSV records them, stay out
    (`reclassification_left_out`). Without shards, the rows come from the CSV itself, as in a
    regeneration over an earlier collection, where the tracked CSV is already restricted and
    the rewrite is idempotent. With neither the CSV nor a shard, the function writes nothing
    and logs that the CSV is missing. `other_tools` reads the shards of the tools outside the
    six, from which `other_ai_commits.csv` is written.

    Returns the number of rows left out, 0 when nothing was written.
    """
    # Read at the call rather than bound as a default, because a default holds the value of
    # `COMMITS_DIR` at import time. The suite points the step at its own tree by reassigning the
    # attribute, and a bound default would still read the real checkout's shards.
    commits_dir = COMMITS_DIR if commits_dir is None else commits_dir
    path = Path(path)
    left_out = (reclassification_left_out(path.with_name(RECLASSIFICATION_REPORT_NAME),
                                          commits_dir)
                if Path(commits_dir).is_dir() else set())
    shards = [commits.other_shard_path(commits_dir, repo) if other_tools else csv_path
              for repo, csv_path, _ in commits.readable_shards(commits_dir)
              if csv_path is not None and repo not in left_out
              ] if Path(commits_dir).is_dir() else []
    shards = [shard for shard in shards if shard.is_file()]
    if left_out:
        logs.info('detection', f"Left the shards of {len(left_out):,} repositories, such as "
                               f"{min(left_out)}, out of {path.name}, as the commit "
                               f"reclassification did.")
    if not shards and not path.is_file():
        logs.info('detection', f"Neither {path} nor its shards exist. Restricted no commit rows "
                               f"to the published repositories.")
        return 0
    kept, dropped = 0, 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        kept, dropped, blanked = _write_restricted(path, tmp, shards, published)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    logs.info('detection', f"Kept {kept:,} row(s) of {path.name} and left out {dropped:,} row(s) "
                           f"of repositories that repos.csv does not list.")
    if blanked:
        logs.warning('detection', repos.blanked_dates_message(path.name, blanked))
    return dropped


def _write_restricted(path: Path, tmp: Path, shards: list, published: set) -> tuple:
    """Write the restricted CSV to `tmp`. Returns (kept, dropped, blanked), where `blanked`
    lists the `commit_timestamp` values that the rows kept leave blank (`repos.to_utc_or_blank`).

    The shards keep the author date as git printed it in UTC. For a commit of the year 10000,
    the year has five digits, and a reader of the published CSV that parses dates would stop at
    that row.
    """
    kept, dropped, blanked = 0, 0, []
    with open(tmp, "w", newline="", encoding="utf-8") as dst:
        src = None
        if shards:
            # The collector's own reader. A repository with a per-repository commit record that
            # does not parse stays out here, as it does in the assembled CSV, and every row is
            # written with the current columns.
            writer = csv.DictWriter(dst, fieldnames=list(commits.COMMIT_COLUMNS),
                                    lineterminator="\n")
            writer.writeheader()
            rows = (row for csv_path in shards for row in commits.shard_rows(csv_path))
        else:
            src = open(path, newline="", encoding="utf-8")
            reader = csv.DictReader(src)
            writer = csv.DictWriter(dst, fieldnames=list(reader.fieldnames or []),
                                    lineterminator="\n")
            writer.writeheader()
            rows = reader
        try:
            for row in rows:
                if row.get("repo_name") in published:
                    blanked += repos.to_utc_or_blank((row,), ("commit_timestamp",))
                    writer.writerow(row)
                    kept += 1
                else:
                    dropped += 1
        finally:
            if src is not None:
                src.close()
    return kept, dropped, blanked


def main(argv=None) -> int:
    cli.ArgumentParser(
        description="Classify every file of the validated tree into a mechanism CSV and write "
                    "output/repos.csv. Run it from data/collection/.").parse_args(argv)
    # Exit 1 on a missing input. Without this check, the step would process no repository,
    # write empty CSVs and exit 0, which a runner reads as success before it continues with
    # broken data.
    if not VALIDATED_CSV.is_file():
        logs.error('detection', f"Validated list not found ({VALIDATED_CSV}).")
        return 1
    if not VALIDATED_DIR.is_dir():
        logs.error('detection', f"Validated tree not found ({VALIDATED_DIR}).")
        return 1

    input_rows, fieldnames = read_validated_csv()

    # Every metadata column has to be there. The mechanism CSVs name them all, and a DictWriter
    # fills a column that the row lacks with a blank. An input list that lost a column would
    # publish it empty in every output CSV without failing. `head_sha` is also the key on which
    # this step joins the file history.
    absent = [c for c in heuristics.META_COLUMNS if c not in fieldnames]
    if absent:
        logs.error('detection', f"{VALIDATED_CSV} lacks the column(s) {absent}. Its header is "
                                f"{list(fieldnames)}.")
        return 1

    OUTPUT_DIR.mkdir(exist_ok=True)

    # The collector writes the head commit date as git reports it, with the committer's own
    # offset, and this step converts it to UTC. `scanned_at` is not converted, because the March
    # rows have a date rather than an instant, and the converter would turn it into midnight. A
    # value that does not parse is left blank (`repos.to_utc_or_blank`). For a commit of the
    # year 10000, git prints such a value, and a refusal would stop every run at that repository.
    blanked = repos.to_utc_or_blank(input_rows.values(), ("head_commit_date",))
    if blanked:
        logs.warning('detection', repos.blanked_dates_message(str(VALIDATED_CSV), blanked))

    # Output schema: the metadata of the input, the derived tool and file flags, and the
    # mechanism flags
    tool_columns = list(TOOLS)
    file_columns = ["claude.md", "gemini.md", "agents.md", "copilot-instructions", "cursorrules"]
    config_columns = ["context_file", "settings", "skills", "subagents", "commands", "hooks", "rules", "mcp", "plugins", "lsp", "other", "no_specific_tool"]
    out_fieldnames = (fieldnames + tool_columns + file_columns + config_columns
                      + ["github_link", "dataset_version"])

    # The metadata columns that repos.csv and every mechanism CSV share. repos.csv takes them
    # from `fieldnames`, in the order of the validated list, and the mechanism CSVs in the
    # heuristics' order. The check above makes both name the same columns.
    meta_columns = list(heuristics.META_COLUMNS)

    # In every row of every CSV, because the mechanism CSVs are published to Zenodo as separate
    # files, and a manifest beside them would be separated from them.
    version_column = {"dataset_version": DATASET_VERSION}

    settings_rows = []
    context_rows = []
    skills_rows = []
    subagents_rows = []
    commands_rows = []
    hooks_rows = []
    rules_rows = []
    mcp_rows = []
    plugins_rows = []
    lsp_rows = []
    other_rows = []
    planning_rows = []

    repos_in_dir = set()
    processed = {}

    # The fact CSVs that this step joins. `file_history.csv` is the cloning step's record of
    # what `git log` returned for each copied file, and `ai_authorship.csv` contains the AI
    # authorship columns for context files. This step cannot compute either, and it logs a row
    # without a match in them rather than publishing it blank without notice.
    cache = caches.FileHistoryCache.load(FILE_HISTORY_CSV)
    ai_authorship = load_ai_authorship()
    logs.info('detection', f"Read {len(cache):,} rows of {FILE_HISTORY_CSV} and "
                           f"{len(ai_authorship):,} rows of {AI_AUTHORSHIP_CSV}.")
    without_history = []

    # A stray directory in the validated tree would be read as a repository and get rows in
    # every mechanism CSV under its own name, and a repository that no collection copied would
    # reach the published dataset.
    repo_dir_names, strays = repos.split(VALIDATED_DIR, expected=VALIDATED_TREE_FILES)
    repo_dirs = [VALIDATED_DIR / name for name in repo_dir_names]
    if strays:
        logs.warning('detection', f"Skipped {len(strays):,} entry/entries in {VALIDATED_DIR} that "
                                  f"are not repository directories ({', '.join(strays)}).")
    if not repo_dirs:
        logs.error('detection', f"No repository directory found in {VALIDATED_DIR}.")
        return 1

    for entry in repo_dirs:
        repo_name = repos.dir_name_to_repo(entry.name)
        repos_in_dir.add(repo_name)

        input_row = input_rows.get(repo_name) or {}
        branch = input_row.get("branch", "")
        head_sha = input_row.get("head_sha", "")
        base = {c: input_row.get(c, "") for c in meta_columns}
        base["repo_name"] = repo_name

        repo_rows = []

        def detail_row(mechanism, item):
            """One published row: the detection, the file, and the joined facts. Its `tool`
            is filled in once every detector has run."""
            row, missing = build_detail_row(
                mechanism, item, base=base, repo_dir=entry, repo_name=repo_name,
                branch=branch, head_sha=head_sha, cache=cache, ai_authorship=ai_authorship,
                version_column=version_column)
            without_history.extend(missing)
            repo_rows.append((heuristics.MECHANISM_CSVS[mechanism][1], row))
            return row

        # settings
        s = detect_settings(entry)
        agent_workflows = detect_agent_workflows(entry)
        # Reported as settings, beside the workflow files that are matched by name, and added
        # to `s` once when a file is both.
        for path, _tool in agent_workflows:
            if path not in s:
                s.append(path)
        has_settings = bool(s)
        for item in s:
            settings_rows.append(detail_row("settings", item))

        # context_files
        cf = detect_context_files(entry)
        for item in cf:
            context_rows.append(detail_row("context_file", item))

        # context_file boolean: TRUE if any context file was detected
        has_context = bool(cf)

        # skills
        sk = detect_skills(entry)
        has_skills = bool(sk)
        for item in sk:
            skills_rows.append(detail_row("skills", item))

        # subagents
        sa = detect_subagents(entry)
        has_subagents = bool(sa)
        for item in sa:
            subagents_rows.append(detail_row("subagents", item))

        # commands
        cmd = detect_commands(entry)
        has_commands = bool(cmd)
        for item in cmd:
            commands_rows.append(detail_row("commands", item))

        # hooks
        hk = detect_hooks(entry)
        has_hooks = bool(hk)
        for item in hk:
            hooks_rows.append(detail_row("hooks", item))

        # rules
        rl = detect_rules(entry)
        has_rules = bool(rl)
        for item in rl:
            rules_rows.append(detail_row("rules", item))

        # mcp
        mc = detect_mcp(entry)
        has_mcp = bool(mc)
        for item in mc:
            mcp_rows.append(detail_row("mcp", item))

        # plugins
        pl = detect_plugins(entry)
        has_plugins = bool(pl)
        for item in pl:
            plugins_rows.append(detail_row("plugins", item))

        # LSP servers
        lsp = detect_lsp(entry)
        has_lsp = bool(lsp)
        for item in lsp:
            lsp_rows.append(detail_row("lsp", item))

        # The planning documents. A file that a mechanism claims is a configuration artifact,
        # which a tool reads as configuration, and no planning document. The openspec/AGENTS.md
        # that earlier OpenSpec versions wrote, for example, is a context file.
        markers = resolved_markers(entry)
        configured = _collect_known_files(entry, s, cf, sk, sa, cmd, hk, rl, mc, markers, pl, lsp)
        configured.update(row[column] for column, row in repo_rows)
        planned = [(rel, heuristic_row) for rel, heuristic_row
                   in planning.planning_documents(entry, heuristics.VENDORED_DIRS)
                   if rel not in configured]
        for rel, heuristic_row in planned:
            row, missing = build_planning_row(
                rel, heuristic_row, base=base, repo_dir=entry, repo_name=repo_name,
                branch=branch, head_sha=head_sha, cache=cache, version_column=version_column)
            without_history.extend(missing)
            planning_rows.append(row)

        # other (unclassified files), without the planning documents that the heuristics explain
        planned_paths = {rel for rel, _heuristic_row in planned}
        ot = [item for item in detect_other(entry, s, cf, sk, sa, cmd, hk, rl, mc, markers, pl, lsp)
              if item not in planned_paths]
        has_other = bool(ot)
        linked = linked_spec_files(entry, markers)
        for item in ot:
            other_rows.append(detail_row("other", {"other": item,
                                                   "link_path": linked.get(item, "")}))

        # Each row's tool, and the repository's tool flags as the union over the rows of the
        # named mechanisms, which `output_cross_check` checks on the published CSVs. An `other`
        # row keeps its tool, the owner of the directory that contains it, but sets no flag. It
        # is a file that no detector of the tool reads, such as a file that
        # not_configuration.csv records as unread by the tool. other.csv is also not published,
        # and no published row would explain a flag that an `other` row set.
        tools = artifact_tools(entry, [(_MECHANISM_OF[column], row[column]) for column, row in repo_rows],
                               agent_workflows, plugin_part_tools(entry), declared_file_tools(entry),
                               marker_directories(entry, markers))
        for column, row in repo_rows:
            row["tool"] = tool_value(tools[(_MECHANISM_OF[column], row[column])])
        file_flags = file_flags_of(cf)
        tool_flags = tool_flags_of(found for (mechanism, _path), found in tools.items()
                                   if mechanism != "other")

        mechanism_flags = (has_context, has_settings, has_skills, has_subagents, has_commands,
                           has_hooks, has_rules, has_mcp, has_plugins, has_lsp)

        processed[repo_name] = {
            # Tool flags
            **{col: str(val).upper() for col, val in tool_flags.items()},
            # File-level flags
            **{col: str(val).upper() for col, val in file_flags.items()},
            # Config mechanism flags
            "context_file": str(has_context).upper(),
            "settings": str(has_settings).upper(),
            "skills": str(has_skills).upper(),
            "subagents": str(has_subagents).upper(),
            "commands": str(has_commands).upper(),
            "hooks": str(has_hooks).upper(),
            "rules": str(has_rules).upper(),
            "mcp": str(has_mcp).upper(),
            "plugins": str(has_plugins).upper(),
            "lsp": str(has_lsp).upper(),
            "other": str(has_other).upper(),
            "no_specific_tool": str(no_specific_tool(mechanism_flags, tool_flags)).upper(),
        }

    # The Git columns come from a clone that this step does not make. A row without a match in
    # the cache is published blank and logged rather than refused.
    # `agentcontext/steps/file_history.py` measures such rows from the output CSVs that this
    # step writes, and a refusal here would leave it nothing to repair. Its `--offline` mode
    # turns a blank into an error.
    if without_history:
        logs.warning('detection', f"{len(without_history):,} row(s) have no file history and "
                                  f"blank Git columns. Run `python -m agentcontext file-history` "
                                  f"to measure them.")
        for source, mechanism, repo_name, artifact in without_history[:WITHOUT_HISTORY_NAMED]:
            logs.info('detection', f"{source} has no row for {artifact} ({mechanism}) of "
                                   f"{repo_name}.")
    # `file_history.csv` keeps git's text for a creation date that does not parse, and the
    # mechanism CSVs leave it blank, as `head_commit_date` above.
    mechanism_rows = (settings_rows, context_rows, skills_rows, subagents_rows, commands_rows,
                      hooks_rows, rules_rows, mcp_rows, plugins_rows, lsp_rows, other_rows)
    blanked = repos.to_utc_or_blank((row for rows in mechanism_rows for row in rows),
                                    ("created_at",))
    if blanked:
        logs.warning('detection', repos.blanked_dates_message("The mechanism CSVs", blanked))
    blanked = repos.to_utc_or_blank(planning_rows, ("created_at",))
    if blanked:
        logs.warning('detection', repos.blanked_dates_message(planning_schema.CSV_NAME, blanked))

    blank_ai = sum(1 for r in context_rows if r["first_commit_ai_created"] == "")
    if blank_ai:
        logs.info('detection', f"{blank_ai:,} context file(s) have no row in {AI_AUTHORSHIP_CSV} "
                               f"and blank AI columns.")

    # The main CSV, with the repositories that have a directory in the validated tree.
    excluded_rows = []
    main_rows = []
    for repo_name, row in input_rows.items():
        if repo_name not in processed:
            excluded_rows.append({**{c: row.get(c, "") for c in EXCLUDED_COLUMNS},
                                  "repo_name": repo_name,
                                  "reason": "no_artifacts"})
            continue
        flags = processed[repo_name]
        link = github_link(repo_name, row.get("branch", ""))
        main_rows.append({**row, **flags, "github_link": link, **version_column})
    write_output_table("repos.csv", out_fieldnames, main_rows)

    # excluded.csv, with the repositories of the validated list that have no directory in the
    # validated tree.
    write_output_table("excluded.csv", list(EXCLUDED_COLUMNS), excluded_rows)

    write_sub("settings.csv", csv_header("settings"), settings_rows)
    write_sub("context-files.csv", csv_header("context_file"), context_rows)
    write_sub("skills.csv", csv_header("skills"), skills_rows)
    write_sub("subagents.csv", csv_header("subagents"), subagents_rows)
    write_sub("commands.csv", csv_header("commands"), commands_rows)
    write_sub("hooks.csv", csv_header("hooks"), hooks_rows)
    write_sub("rules.csv", csv_header("rules"), rules_rows)
    write_sub("mcp.csv", csv_header("mcp"), mcp_rows)
    write_sub("plugins.csv", csv_header("plugins"), plugins_rows)
    write_sub("lsp.csv", csv_header("lsp"), lsp_rows)
    write_sub("other.csv", csv_header("other"), other_rows)
    # Not through `write_sub`, which writes the mechanism CSVs alone, because a planning document
    # is no configuration artifact.
    write_output_table(planning_schema.CSV_NAME, planning_schema.csv_header(), planning_rows)

    # After the mechanism CSVs, because the restriction rebuilds a tracked CSV from the shards.
    # A read that failed there before the mechanism CSVs were written would stop the step with
    # `repos.csv` already rewritten from the new heuristics and the output CSVs beside it still
    # from the old ones.
    restrict_ai_commits(OUTPUT_DIR / "all_ai_commits.csv", set(processed))
    restrict_ai_commits(OUTPUT_DIR / commits.OTHER_AI_COMMITS_NAME, set(processed),
                        other_tools=True)

    # Log the mismatches
    input_names = set(input_rows.keys())
    only_in_csv = input_names - repos_in_dir
    only_in_dir = repos_in_dir - input_names

    if only_in_csv:
        logs.info('detection', f"{len(only_in_csv):,} repositories of the validated list "
                               f"({VALIDATED_CSV}) have no directory in the validated tree "
                               f"({VALIDATED_DIR}).")
        for name in sorted(only_in_csv):
            logs.info('detection', f"{name} is in {VALIDATED_CSV} and has no directory in "
                                   f"{VALIDATED_DIR}.")
    if only_in_dir:
        logs.info('detection', f"{len(only_in_dir):,} repositories of the validated tree "
                               f"({VALIDATED_DIR}) have no row in the validated list "
                               f"({VALIDATED_CSV}).")
        for name in sorted(only_in_dir):
            logs.info('detection', f"{name} has a directory in {VALIDATED_DIR} and no row in "
                                   f"{VALIDATED_CSV}.")

    # A machine-readable record beside the CSVs, for anyone who reconstructs a run. The file is
    # replaced in one operation, as the CSVs are, because the publication copies it, and opening
    # it for writing would truncate it before its content exists.
    caches.replace_file(OUTPUT_DIR / "provenance.json", json.dumps({
        "dataset_version": DATASET_VERSION,
        "heuristics": str(CONFIG_PATH_PATTERNS_PATH.name),
        "heuristics_sha256": hashlib.sha256(CONFIG_PATH_PATTERNS_PATH.read_bytes()).hexdigest(),
        "pattern_count": len(_PATTERNS),
        "planning_heuristics": planning.PLANNING_DOCUMENT_PATTERNS_PATH.name,
        "planning_heuristics_sha256": hashlib.sha256(
            planning.PLANNING_DOCUMENT_PATTERNS_PATH.read_bytes()).hexdigest(),
        "planning_pattern_count": len(planning.load_planning_document_patterns()),
        # The two heuristics files that decide which directories the step skips and which places
        # of the tools' local state the collection and the archives leave out.
        "vendored_dirs": heuristics.VENDORED_DIRS_PATH.name,
        "vendored_dirs_sha256": hashlib.sha256(
            heuristics.VENDORED_DIRS_PATH.read_bytes()).hexdigest(),
        "local_state": heuristics.LOCAL_STATE_PATHS_PATH.name,
        "local_state_sha256": hashlib.sha256(
            heuristics.LOCAL_STATE_PATHS_PATH.read_bytes()).hexdigest(),
    }, indent=2) + "\n")

    logs.info('detection', f"Wrote {OUTPUT_DIR / 'repos.csv'} with {len(out_fieldnames)} columns "
                           f"and {len(processed):,} rows.")
    written = (("settings.csv", settings_rows), ("context-files.csv", context_rows),
               ("skills.csv", skills_rows), ("subagents.csv", subagents_rows),
               ("commands.csv", commands_rows), ("hooks.csv", hooks_rows),
               ("rules.csv", rules_rows), ("mcp.csv", mcp_rows), ("plugins.csv", plugins_rows),
               ("lsp.csv", lsp_rows), ("other.csv", other_rows))
    for filename, mechanism_csv_rows in written:
        logs.info('detection', f"Wrote {OUTPUT_DIR / filename} with {len(mechanism_csv_rows):,} "
                               f"rows.")
    logs.info('detection', f"Wrote {OUTPUT_DIR / planning_schema.CSV_NAME} with "
                           f"{len(planning_rows):,} rows in "
                           f"{len({row['repo_name'] for row in planning_rows}):,} repositories.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
