"""The heuristics files that every step reads: the path patterns, the commit patterns, their
two negatives, the vendored directories and the places of the tools' local state.

The files are in `heuristics/` at the repository root, and the paths below are the only
definition of where they are.

# The path patterns

`config_path_patterns.csv` is the only place that records which path is which tool's
configuration of which mechanism, so that a heuristic cannot be added to the collector's
per-tool functions, the validator's keep-lists or the detector's module constants without the
other two.

The three steps read their path constants from here, and `test_heuristics.py` fails when a
path appears in code but not in the heuristics, or the other way round.

This module also defines `MECHANISM_CSVS`, the CSV that each mechanism is published as and the
column of that CSV that names the configuration artifact. The column is written down rather than
derived from the column layout, which a new column shifts (see `artifact_column` below).

Columns
-------
tool          claude | gemini | codex | copilot | cursor | opencode | any
mechanism     one of the named mechanisms in MECHANISM_CSVS, or `other` for the unclassified files,
              or `collect_only` for a path that is copied but classifies nothing
path          repo-relative path, bare directory, or bare filename (see match_mode)
match_mode    how `path` is matched (see the match modes below)
content_format empty for strict JSON, `jsonc` to permit comments, and `jsonc_trailing` to also
              permit trailing commas where the vendor format supports them
doc_url       the vendor documentation that defines the mechanism
doc_verified  ISO date on which the page was last read and confirmed to still state this
evidence      links to files in repositories that contain the path, separated by spaces, for
              a row with a path that the page does not state in full (see the commit columns)
notes         anything that a reader needs beyond the other columns

Match modes
-----------
file                  an exact repo-relative path to a regular file
root_file             an exact path. The detector classifies it at the repository root only,
                      while the collector and the validator keep it at any depth, so that the
                      archive can contain a nested copy that no CSV lists
basename              a filename, matched at any depth in the tree
root_any_case         a filename spelled in any case other than the one that a basename row
                      gives it, at the repository root only. The tools find `agents.md` for
                      `AGENTS.md` on a case-insensitive file system
dir_agent:.md         the direct Markdown files of a directory and its immediate <name>/agent.md
                      files, the name in any letter case, with YAML front matter that has a
                      non-empty `name` and `description`, such as Antigravity's agents
dir_ext:.a|.b         the direct children of a directory with one of these extensions
dir_ext_described:.a  the direct children of a directory with this extension and YAML front
                      matter with a non-empty `description` string, such as a Copilot CLI
                      plugin's rules
dir_ext_triggered:.a  the direct children of a directory with this extension and YAML front
                      matter that declares a `trigger` that Antigravity accepts, such as
                      Antigravity's rules
tree_ext:.a|.b        the descendants of a directory with one of these extensions, at any depth
tree_ext_described:.a the descendants of a directory with this extension and a `description` in
                      their front matter
json_copilot_marketplace a JSON object with name, owner.name and a plugins array
tree_suffix:.x.y      the descendants of a directory with a whole name that ends with this suffix
skill_dir             a directory searched recursively for skill folders that contain SKILL.md
skill_dir_flat        a directory with immediate child folders that contain SKILL.md
package_entry:n|m:.a|.b
                      the immediate child folders of a directory that contain an entry module:
                      a file named n or m with the extension .a or .b, such as OpenCode 2's
                      plugin packages. A folder is one configuration artifact, at its first entry
                      module in the order of the names and then of the extensions, and its other
                      files belong to it
workflow_uses:o/r     the direct children of a directory that run this GitHub Action in a
                      `uses:` line. The mode recognizes a workflow that runs an agent whatever
                      the repository called the file
workflow_engine:e     the direct children of a directory that are GitHub Agentic Workflows,
                      Markdown with `on:` in its front matter, run by the engine e. `engine:`
                      names the engine, which is `copilot` where `engine:` names none
workflow_command:c    the direct YAML children of a directory with a shell step that runs one of
                      the invocations that c lists, separated by `||`, such as `claude -p`, the
                      vendor's documented way to run its command-line tool without a person
json_hooks            a JSON file with a non-empty top-level "hooks" key, and where the key maps
                      events to their hooks, with a hook in at least one event
json_hooks_or_modules a JSON file with a non-empty "hooks" key or a non-empty "modules" list,
                      such as the hooks.json of a Claude Code plugin, where a mod names its hooks
                      module
json_named_hooks      a JSON object that maps hook names to their events, such as Antigravity's
                      hooks.json, which is read the way Antigravity decodes it
toml_hooks            a TOML file with a [hooks] table or [hooks.*] / [[hooks.<Event>]] tables
json_mcp_servers      a JSON file with at least one entry under an "mcpServers" key
json_servers:k        a JSON file with an object of at least one entry under the top-level key k,
                      such as the MCP servers under `mcp` in OpenCode's opencode.json
json_enabled:k        a JSON file with `true` or an object under the top-level key k, such as
                      `lsp` in OpenCode's opencode.json, which enables the built-in LSP servers
                      with either value and disables them with `false`
json_plugin_mcp       a plugin's MCP file with "mcpServers", or a flat command/URL server map, the
                      two forms that Claude Code and Codex read
toml_mcp_servers      a TOML file that defines a table under mcp_servers, by header, dotted keys
                      or inline
basename_json_mcp_servers
                      a filename matched at any depth, kept when it has at least one entry
                      under "mcpServers"
basename_json_plugin_manifest
                      a filename matched at any depth, kept when the JSON declares the
                      Agent Plugins plugin schema in "$schema" and has "name"
basename_json_agent_plugins_mcp
                      a filename matched at any depth, kept when the JSON declares the
                      Agent Plugins mcp schema in "$schema" and has "mcpServers"
spec_dir              a directory that is entirely configuration: every file in it is kept,
                      and every file that no detector classifies becomes `other`
spec_dir_with_place   a spec_dir only where it contains a place that another row names below
                      it, such as `_agents/skills` or `_agents/mcp_config.json`. A Python
                      package and a documentation partial can have the same name, and such a
                      directory without a place is no configuration
unclassified_dir      a directory with files that the detector reads for `other` but that
                      validation does not keep whole. The pruning differs per directory.
                      `.github` is pruned to the paths that the heuristics name and the context
                      files, and the plugin manifest directories are kept entire by their
                      copy_dir heuristic
copy_dir              a directory that the collector copies whole, and with contents that the
                      validator therefore keeps entire: the plugin manifest directories
copy_dir_gated        a directory that the collector copies only when a path that the heuristics
                      name exists, and that the validator prunes rather than keeps entire.
                      `.github` is the only one, because it contains a project's CI as well
plugin_sibling        a filename kept beside a plugin manifest as plugin context
skill_marker          the filename that makes a directory a skill
agent_memory          a directory with a `*` in place of the subagent's name. Every file in it
                      is the subagent's memory
name_glob:p           the direct children of a directory with a name that matches the pattern p,
                      such as `.claude` with `hookify.*.local.md`
declared_names:k      the file at `path` names context files under its key k, dotted into the
                      JSON or TOML: a filename or a list of them, each read at any depth, such as
                      Gemini CLI's `context.fileName`
declared_fallback:k   the same, but tried in order only in a directory that contains none of the
                      context files that every tool shares, and one file per directory at most,
                      such as Codex's `project_doc_fallback_filenames`
declared_path:k       the file at `path` names a file of the row's mechanism under each key that k
                      matches, a path relative to its own directory, such as Codex's
                      `model_instructions_file`. A `*` in k stands for every key at its level,
                      such as the name of each role in Codex's `agents.*.config_file`. Where
                      `path` is a directory, such as Codex's `.codex/agents`, each role file of
                      it names a part of its subagent that way, such as its instructions: the
                      TOML files below the directory at any depth, and the files that the
                      `declared_path:` rows of the same tool and mechanism with a file at their
                      `path` name
declared_local:k      one filename or a list under key k, strictly inside the declaring
                      directory, with no parent references, such as Gemini extension context
                      files
declared_globs:k      the file at `path`, relative to the repository root, lists paths and glob
                      patterns under its key k, each matched against the repository root,
                      dotfiles included, such as OpenCode's `instructions`. A URL, a path in the
                      home directory and an absolute path name no file of the repository
registered:m          an Antigravity registration file for skills, subagents, commands or plugins.
                      A relative path of its entries and inherits resolves against the directory
                      that contains its customization root, or the root of its plugin, and
                      against the workspace root where nothing exists there. Subagent directories
                      contain agent.md, and skill directories contain SKILL.md.
declared_rules        the file at `path` registers rule files. Each of its `entries` names a
                      directory, which resolves as the entries of a `registered:` row do,
                      scanned one level deep unless `include_only` selects nested files.
                      `exclude` filters matches, and each of its `inherits` names another such
                      file. Antigravity's `.agents/rules.json` is one
manifest_field:k:h    a placeholder row: the plugin manifest may name more places of the
                      component with the default place `path` under its key k, relative to the
                      plugin root and inside it, such as a Claude Code plugin's
                      `"skills": ["./extra-skills/"]`. What the key names is read the way the
                      row with the same tool, mechanism and path reads the default place, and h
                      defines how the two combine:
                        adds, merges  both are read
                        replaces      the named places alone are read once the key is set
                        files         as `replaces`, and the key names files only
                        paths         Copilot CLI's form: a string or a list replaces the
                                      default, and `{"paths": [...]}` adds to it unless
                                      `"exclusive": true`
                        precedes      a named file that exists is read in place of the default
                        follows       a named file is read only where no default file parses
                      An Antigravity plugin's `skills` names skills as the entries of its
                      skills.json do, against the plugin root and, where nothing exists there,
                      the workspace root. `paths.plugin_components` resolves these rows for
                      every step
plugin_root_skill     a placeholder row that names `SKILL.md`: the SKILL.md at the plugin's
                      root is a skill when the plugin has no `skills/` directory and its
                      manifest no `skills` key
nested:m              the place that the match mode m reads at `path`, read in every directory
                      below the repository root but not at the root, which another row reads.
                      Copilot CLI, for example, reads `.github/copilot-instructions.md` in its
                      working directory and in each directory between it and the root.
                      `paths.nested_places` finds the places for every step

A placeholder row's path can list alternatives after the placeholder, separated by `|`, as
`<copilot-plugin>/hooks.json|hooks/hooks.json` does. The tool reads the first of them that exists
and parses, and none after it.

A path can start with a plugin placeholder, `<claude-plugin>`, `<cursor-plugin>`,
`<codex-plugin>`, `<copilot-plugin>`, `<gemini-extension>`, `<antigravity-plugin>` or
`<agent-plugin>`, which stands for the root of every plugin of that kind in the tree, the
directory that contains the manifest that `PLUGIN_ROOTS` gives. `<claude-plugin>/skills` is the
skills directory of every Claude Code plugin in a repository, wherever the repository puts the
plugin, and the match mode reads it as it reads any skills directory. An Antigravity plugin is
the exception, because Antigravity loads plugins from a customization root's `plugins/<name>/`
directory or from a plugins.json registration. `PLUGIN_PARENTS` lists its root aliases, and
`paths.plugin_roots` finds the roots for every step.

A declaring row's `path` is the declaring file, found the way `file` finds a path with a `/` in
it and `basename` a bare filename. A `declared_globs:` row's file is the exception: it is at the
repository root, the directory that the tool resolves the patterns from.
`paths.declared_context_files` resolves those rows for every step, because only reading the file
shows what it declares.

# The AI-commit pattern heuristics


`ai_commit_patterns.csv` is the only place that records which text in a commit credits which
tool. Each row records its source, so that a heuristic taken from a vendor page can be told
apart from a heuristic inferred from the sample. `doc_url` and `doc_verified` record the same
for `config_path_patterns.csv`.

The path patterns and the commit patterns share these columns but are separate files, because
their provenance differs. The documentation watch, which detects a changed page, keeps the path
patterns current, because a vendor documents every configuration path. The commit patterns have
two sources, which the `source` column records:

`doc`     the tool's documentation states that the tool writes this, such as the bot account
          that it commits under, the email domain of those commits or the trailer that it
          adds. These rows cite a page in `doc_url` and `doc_verified`, and the documentation
          watch re-reads the page every week.
`sample`  a convention read off the commits themselves, such as a branch-naming prefix that
          appears in merge messages or an attribution line that people write by hand. No
          vendor page documents these rows, and only the comparison with the sample that
          `unmatched commits` makes shows when one is wrong. A `sample` row states in `notes`
          what was observed and links in `evidence` the commits that it was observed in,
          because those commits are its only provenance.

Columns
-------
id            `<tool>-<scope>-<nn>`, never reused: the name of the heuristic in the commit CSV's
              `matched_heuristics`, so that a row can be read against any release's heuristics
tool          Claude | Copilot | Cursor | Codex | Gemini | OpenCode, as the published `ai_tool`
              value
scope         author | trailer | full_body (see below)
attribution   account | reviewer | standard | convention: how the commit credits the tool,
              which the commit CSV's `attributions` records (see below)
pattern       a Python regular expression, matched case-insensitively
source        doc | sample
doc_url       the vendor page that documents the heuristic. Required when source is `doc`
doc_verified  ISO date on which the page was last read and confirmed to still state this
evidence      links to commits that the heuristic matches, and to the page of the account that
              a heuristic names, separated by spaces. Required for `sample`, because a
              heuristic that no commit shows is an invented one
notes         what a reader needs beyond the other columns. Required for `sample`

Scopes
------
author      the author and committer name and email, joined. A bare tool name is safe
            here, because a person's identity fields do not contain "copilot"
trailer     the subject plus every line of the body in the form of a git trailer. An
            attribution line belongs in this scope, so that prose such as "used claude.ai to
            draft this" cannot match a `co-authored-by` heuristic
full_body   the subject plus the whole body, for text that is not a trailer, such as a merge
            commit that names the branch that it merges or a sentence that claims authorship

The scope is a column rather than inferred from the pattern text, such as whether it starts
with a trailer token, because an inference would put a new attribution line in the wrong scope
without a warning.

Attributions
------------
account     the tool's own account authored or committed the commit, or the identity that the
            tool's code or environment writes, name and address together, or an agent account
            or a GitHub App that its own page or its project's own configuration grounds as
            running the tool. An address that GitHub links to a vendor's account is account
            only when the tool's code or environment writes it. Only an author heuristic has it
reviewer    the co-author trailer that GitHub writes when a person commits a review app's
            suggestion
standard    the line that the tool writes itself, with its name and its own address or
            account, or a marker that it writes into a commit or a pull request
convention  any other line that names the tool: a line that a person writes, or that an agent
            writes because a repository's instructions require it, and any other name or
            address that names the tool and that users chose for an author or a committer

Heuristics of different attributions can match one line. The name heuristic and the reviewer
heuristic, for example, both match `Co-authored-by: Copilot` with the code review app's address.
A line takes the first attribution in `ATTRIBUTIONS` order with a heuristic that matches the
line, so that the attributions of a commit record how the commit credits each tool rather than
which rows matched.

# The vendored directories

A vendored directory contains another project's code, such as node_modules, a Go module cache or a
checked-in third-party tree. Configuration inside one belongs to the vendored project rather than to
the repository that vendored it. All three steps therefore skip those subtrees, detection too,
because it reads mechanism directories below the repository root. The list has one name per line,
matched case-sensitively at any depth, and the section "The vendored list" of docs/heuristics.md
records the names that were considered and left out.

# The local state of the tools

`local_state_paths.csv` lists the places inside a tool directory where a tool keeps the files
that it writes for its own use on a machine: session logs, prompt histories, caches, locks and
logins. A repository contains them where its tool directory is a tool's home directory, such as
a `CLAUDE_CONFIG_DIR` or a `CODEX_HOME` in the project, or a committed copy of one. A tool can
also write them into the project, as Claude Code does with its worktrees. No tool reads them as
configuration, and they can contain a user's paths, prompts and credentials. The collector
therefore copies none of them, and the collected file cleanup deletes any that a tree contains.
The file has the columns of `not_configuration.csv` and is checked the same way. A `path` starts
with the path of a row of `SPEC_DIR_MODES`, the tool directory, and names the place below it.
With a trailing `/` it names a directory, and everything below the directory is left out.
Otherwise it names a file. `*` and `?` match within one component of the path.
`paths.local_state` matches a path against the places.
"""

import csv
import fnmatch
import json
import re
import sys
from pathlib import Path
from re import _constants as _sre_constants, _parser as _sre_parser

from agentcontext import REPO_ROOT

# A pattern of the disclosure rows is longer than the default limit of a CSV field, and the
# loaders read the files in any process, also in one that no other module has prepared.
csv.field_size_limit(sys.maxsize)

# The heuristics directory: the path patterns, the commit patterns, the vendored list and the
# recorded hashes of the documentation watch. It is beside the package rather than under the
# stage directory, because the heuristics are code. The container reads them from the image, as
# it runs the package from the image, and the volume contains the data that a run reads and
# writes.
HEURISTICS_DIR = REPO_ROOT / "heuristics"
CONFIG_PATH_PATTERNS_PATH = HEURISTICS_DIR / "config_path_patterns.csv"
AI_COMMIT_PATTERNS_PATH = HEURISTICS_DIR / "ai_commit_patterns.csv"
OTHER_AI_COMMIT_PATTERNS_PATH = HEURISTICS_DIR / "other_ai_commit_patterns.csv"
NOT_CONFIGURATION_PATH = HEURISTICS_DIR / "not_configuration.csv"
NOT_AI_COMMITS_PATH = HEURISTICS_DIR / "not_ai_commits.csv"
VENDORED_DIRS_PATH = HEURISTICS_DIR / "vendored_dirs.txt"
LOCAL_STATE_PATHS_PATH = HEURISTICS_DIR / "local_state_paths.csv"
DOC_HASHES_PATH = HEURISTICS_DIR / "doc_hashes.json"


# ---------------------------------------------------------------------------
# The path patterns
# ---------------------------------------------------------------------------
CONFIG_FIELDNAMES = ["tool", "mechanism", "path", "match_mode", "content_format",
                     "doc_url", "doc_verified", "evidence", "notes"]
# The values of `content_format`. Empty is strict JSON, which `json.loads` reads and Claude Code
# documents for its settings file: "a // comment or a trailing comma is a syntax error". `jsonc`
# is for a file that the tool itself reads with comments stripped. That reading has to be
# grounded in the tool, not assumed, because reading a file more permissively than its own tool
# does would record a configuration that the tool never applies.
CONTENT_FORMATS = ("", "jsonc", "jsonc_trailing")

def load_config_path_patterns(heuristics_path=None):
    """Return every heuristic as a dict, in file order."""
    path = Path(heuristics_path) if heuristics_path else CONFIG_PATH_PATTERNS_PATH
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(CONFIG_FIELDNAMES) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the columns {sorted(missing)}")
        rows = [r for r in reader if r["path"] and not r["path"].startswith("#")]
    if not rows:
        raise ValueError(f"{path} has no rows")
    problems = [p for r in rows for p in evidence_problems(r, f"{path.name} ({r['path']})")]
    if problems:
        raise problems_error(path, problems)
    return rows


def problems_error(path, problems: list) -> ValueError:
    """One ValueError for every problem of a heuristics file, each problem on a line of its
    own below the line that names the file."""
    return ValueError(f"{path} has {len(problems):,} problem(s)\n  " + "\n  ".join(problems))


# The match modes that name a directory rather than a file. The validator's marker list, the
# detector's marker list and paths_under below all depend on this list. A mode added to one of
# them and not to the others would be the inconsistency that these heuristics exist to prevent,
# and the list is therefore defined once, here.
DIRECTORY_MODES = ("skill_dir", "skill_dir_flat", "dir_ext:", "dir_ext_described:", "dir_ext_triggered:", "dir_agent:", "tree_ext:", "tree_ext_described:", "tree_suffix:",
                   "workflow_uses:", "workflow_engine:", "workflow_command:", "name_glob:",
                   "package_entry:")


def is_directory_mode(match_mode):
    """Whether a match mode names a directory with contents that the steps read."""
    match_mode = nested_inner(match_mode) or match_mode
    return match_mode in DIRECTORY_MODES[:2] or match_mode.startswith(DIRECTORY_MODES[2:])


# The prefix of a match mode that reads its place below the repository root rather than at it.
NESTED_PREFIX = "nested:"


def nested_inner(match_mode) -> str | None:
    """The match mode that a `nested:` mode reads its places with, or None for another mode."""
    return match_mode[len(NESTED_PREFIX):] if match_mode.startswith(NESTED_PREFIX) else None


# What makes a directory a plugin root, per plugin placeholder: a manifest at one of these paths
# under it. The Agent Plugins manifest counts only where its `$schema` declares the Agent Plugins
# schema, and no manifest counts inside another kind's manifest directory.
PLUGIN_ROOTS = {
    "<claude-plugin>": (".claude-plugin/plugin.json",),
    "<cursor-plugin>": (".cursor-plugin/plugin.json",),
    "<codex-plugin>": (".codex-plugin/plugin.json",),
    "<copilot-plugin>": (".plugin/plugin.json", ".github/plugin/plugin.json"),
    "<gemini-extension>": ("gemini-extension.json",),
    # Antigravity CLI 1.3.2: "keep the manifest in a .antigravity-plugin/ or .gemini-plugin/
    # folder". In which order the CLI tries the three places is not documented.
    "<antigravity-plugin>": ("plugin.json", ".antigravity-plugin/plugin.json",
                             ".gemini-plugin/plugin.json"),
    "<agent-plugin>": ("plugin.json",),
}

# The default parent directories of plugins. Antigravity can also register local plugin
# directories through plugins.json, and the discovery of registrations resolves those entries.
PLUGIN_PARENTS = {"<antigravity-plugin>": (".agents/plugins", ".agent/plugins",
                                             "_agents/plugins", "_agent/plugins")}

# The match modes of a directory that the steps copy and read whole wherever it is: the
# directory of a `spec_dir` row, and the directory of a `spec_dir_with_place` row where it
# contains one of its places (`spec_dir_places`).
SPEC_DIR_MODES = ("spec_dir", "spec_dir_with_place")


def spec_dir_paths(rows) -> list:
    """The paths of the `other` rows with a mode of `SPEC_DIR_MODES`, in file order."""
    return [r["path"] for r in rows
            if r["mechanism"] == "other" and r["match_mode"] in SPEC_DIR_MODES]


def spec_dir_places(rows) -> dict:
    """{directory name: its places} for each `spec_dir_with_place` row. Its places are the
    first segments below the directory in the paths of the other rows and of the parents of
    plugins (`PLUGIN_PARENTS`), such as `skills` of `_agents/skills` and `plugins` of
    `_agents/plugins`."""
    below = [r["path"] for r in rows] + [parent for parents in PLUGIN_PARENTS.values()
                                         for parent in parents]
    return {r["path"]: frozenset(path[len(r["path"]) + 1:].split("/")[0] for path in below
                                 if path.startswith(r["path"] + "/"))
            for r in rows if r["mechanism"] == "other" and r["match_mode"] == "spec_dir_with_place"}


# The file of an agent that is a folder in an Antigravity agents directory, `<name>/agent.md`.
FOLDER_AGENT_FILE = "agent.md"


# GROUNDING: Antigravity CLI 1.4.9 and 1.4.10 (linux_arm64,
# https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.9-4962164969177088/linux-arm/cli_linux_arm64.tar.gz
# and
# https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-arm/cli_linux_arm64.tar.gz),
# read 2026-11-05. The constructor of the agents customization stores the marker "agent.md",
# `(*subdirCustomization).findMarkerInDir` looks it up with `findFileCaseInsensitive`, which
# compares each name of the folder with `strings.EqualFold`, and `agentNameFromPath` compares
# the base name with "agent.md" the same way.
def is_folder_agent_file(name: str) -> bool:
    """Whether `name` makes a folder of an Antigravity agents directory an agent: `agent.md` in
    any letter case, as Antigravity finds it."""
    return name.lower() == FOLDER_AGENT_FILE


def plugin_placeholder(path: str) -> str | None:
    """The plugin placeholder that a heuristics path starts with, or None."""
    head = path.split("/", 1)[0]
    return head if head in PLUGIN_ROOTS else None


# The match modes of a row that names in `path` a configuration file that names context files.
DECLARING_MODES = ("declared_names:", "declared_fallback:", "declared_path:", "declared_local:",
                   "declared_globs:")


def context_declaration_heuristics(rows=None) -> list:
    """Every row with a file that declares context files, in file order."""
    return declaration_heuristics("context_file", rows)


def declaration_heuristics(mechanism: str, rows=None) -> list:
    """Every row of `mechanism` with a file that declares files of that mechanism, such as the
    context files that Codex's config.toml names or the instructions that a Codex agent role
    names, in file order."""
    rows = load_config_path_patterns() if rows is None else rows
    return [r for r in rows
            if r["mechanism"] == mechanism and r["match_mode"].startswith(DECLARING_MODES)]


# The match mode of a row that names in `path` a file that registers rule files.
RULES_JSON_MODE = "declared_rules"


def rules_json_rows(rows=None) -> list:
    """Every row with a file that registers rule files, in file order."""
    rows = load_config_path_patterns() if rows is None else rows
    return [r for r in rows if r["match_mode"] == RULES_JSON_MODE]


# The match mode of a placeholder row for a component to which a plugin manifest can add
# places, and the ways in which the added places combine with the default place.
MANIFEST_FIELD_MODE = "manifest_field:"
MANIFEST_HOWS = ("adds", "merges", "replaces", "files", "paths", "precedes", "follows")
# The match mode of the placeholder row that names a plugin's root SKILL.md.
PLUGIN_ROOT_SKILL_MODE = "plugin_root_skill"
# The optional directories of a skill that the Agent Skills specification names beside SKILL.md
# (https://agentskills.io/specification). They are the columns of skills.csv and the directories
# that a plugin's root skill includes, because the rest of a plugin root is the plugin's other
# parts and, at a repository's root, the whole repository.
SKILL_SUPPORT_DIRS = ("scripts", "references", "assets")


def manifest_field_rows(rows=None) -> list:
    """Every row for a component to which a plugin manifest can add places, in file order."""
    rows = load_config_path_patterns() if rows is None else rows
    return [r for r in rows if r["match_mode"].startswith(MANIFEST_FIELD_MODE)]


def manifest_key(row) -> tuple[str, str]:
    """(key, how) of a manifest_field row. Raises ValueError for an unknown how."""
    key, _, how = row["match_mode"][len(MANIFEST_FIELD_MODE):].partition(":")
    if not key or how not in MANIFEST_HOWS:
        raise ValueError(f"The match mode {row['match_mode']!r} of {row['path']} names no key or "
                         f"no known way of combining, one of {', '.join(MANIFEST_HOWS)}")
    return key, how


def path_alternatives(path: str) -> list[str]:
    """The places that a placeholder path names, in the order in which the tool tries them:
    `<p>/a|b` is `a` and then `b` under the plugin root. A path without `|` names itself."""
    head, sep, rest = path.partition("/")
    if not sep or head not in PLUGIN_ROOTS or "|" not in rest:
        return [rest if sep and head in PLUGIN_ROOTS else path]
    return rest.split("|")


def default_place_row(row, rows=None) -> dict:
    """The row that reads the default place of the component that a manifest_field row extends.

    The row has the same tool, mechanism and path under another match mode. Raises ValueError
    unless exactly one row has them, because otherwise the manifest_field row names a place
    that no row reads, or that several rows read.
    """
    rows = load_config_path_patterns() if rows is None else rows
    found = [r for r in rows if r["tool"] == row["tool"] and r["mechanism"] == row["mechanism"]
             and r["path"] == row["path"] and not r["match_mode"].startswith(MANIFEST_FIELD_MODE)]
    if len(found) != 1:
        raise ValueError(f"{len(found)} rows name the default place of {row['path']} "
                         f"({row['match_mode']}), but a manifest key extends exactly one")
    return found[0]


def directory_paths(rows):
    """Every path in the heuristics that names a directory, in file order.

    These are the paths at which a symlink marker can stand for a directory. The collector
    stores a symlink as a file that contains its target, and the directory of any mechanism can
    be a symlink. A directory missing from this list is not recognized as a marker. Its target
    is then never resolved, and the configuration artifacts behind it are recorded as `other`
    or not at all.
    """
    seen, found = set(), []
    for r in rows:
        # A plugin placeholder stands for directories found in the tree, not for a path at
        # which a marker can be.
        if plugin_placeholder(r["path"]):
            continue
        if is_directory_mode(r["match_mode"]) and r["path"] not in seen:
            seen.add(r["path"])
            found.append(r["path"])
    return found


def paths_for(rows, mechanism, match_mode):
    """Return the paths of every row with this exact mechanism and match mode.

    The order is the file order of the heuristics, which keeps the detector's constants in the
    same order across runs.
    """
    return [r["path"] for r in rows
            if r["mechanism"] == mechanism and r["match_mode"] == match_mode]


def one_path(rows, mechanism, match_mode):
    """Return the single path for this mechanism and match mode, or raise."""
    found = paths_for(rows, mechanism, match_mode)
    if len(found) != 1:
        raise ValueError(f"Expected exactly one {mechanism}/{match_mode} row and found "
                         f"{len(found)} ({found})")
    return found[0]


# The two documents that the Agent Plugins standard defines at a plugin root, keyed by the match
# mode that names each: the schema file that its `$schema` must point at, and the field that the
# standard requires beside it.
AGENT_PLUGINS_DOCUMENTS = {
    "basename_json_plugin_manifest": ("plugin.schema.json", "name"),
    "basename_json_agent_plugins_mcp": ("mcp.schema.json", "mcpServers"),
}

# The path that every canonical `$schema` of the standard contains. The version segment between
# this path and the schema file is not pinned, so that the dataset also records a plugin that
# targets a later version of the standard.
AGENT_PLUGINS_SCHEMA_PATH = "agent-plugins.org/schemas/"


def is_agent_plugins_document(match_mode, text) -> bool:
    """Whether `text` is the document that the mode's heuristic matches.

    Many programs use the names `plugin.json` and `mcp.json`, and the test is therefore the
    standard's own requirement rather than the path: "plugin.json MUST be a JSON object
    containing the required $schema and name fields" and "mcp.json MUST be a JSON object
    containing the required $schema and mcpServers fields", with `$schema` the canonical
    identifier `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`
    ([Agent Plugins 1.0.0](https://github.com/agentplugins/agent-plugins-spec/blob/main/spec/1.0.0.md)).
    A `plugin.json` that declares no such schema belongs to some other program. The collector,
    the validator and the detector all decide a file with this function.
    """
    schema_file, required = AGENT_PLUGINS_DOCUMENTS[match_mode]
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict) or required not in data:
        return False
    schema = data.get("$schema")
    return (isinstance(schema, str)
            and AGENT_PLUGINS_SCHEMA_PATH in schema
            and schema.endswith("/" + schema_file))


def agent_plugins_names(rows=None) -> dict:
    """{filename: match_mode} for every row with a file that the content test above decides."""
    rows = load_config_path_patterns() if rows is None else rows
    return {r["path"]: r["match_mode"] for r in rows
            if r["match_mode"] in AGENT_PLUGINS_DOCUMENTS}


def jsonc_paths(rows=None) -> set:
    """The paths in the heuristics with a file that its own tool reads with comments stripped.

    The format is a property of the file rather than of the heuristic. Every row that names
    such a file records it, including the rows that give a full path rather than a
    filename.
    """
    rows = load_config_path_patterns() if rows is None else rows
    return {r["path"] for r in rows if r.get("content_format") == "jsonc"}


def extensions_for(match_mode):
    """Return the extension tuple encoded in a dir_ext/tree_ext match mode."""
    if ":" not in match_mode:
        raise ValueError(f"The match mode {match_mode} names no extensions")
    return tuple(match_mode.split(":", 1)[1].split("|"))


# The prefix of the match mode of a directory with child folders that are packages with an
# entry module.
PACKAGE_ENTRY_MODE = "package_entry:"


def package_entries(match_mode) -> tuple[tuple, tuple]:
    """(names, extensions) of a `package_entry:` match mode: the names of the entry modules and
    their extensions, each in the order in which the tool tries them. Raises ValueError for
    another mode and for a mode without a name or an extension."""
    if not match_mode.startswith(PACKAGE_ENTRY_MODE):
        raise ValueError(f"{match_mode} is not a package entry mode")
    names, _, extensions = match_mode[len(PACKAGE_ENTRY_MODE):].partition(":")
    names = tuple(name for name in names.split("|") if name)
    extensions = tuple(extension for extension in extensions.split("|") if extension)
    if not names or not extensions:
        raise ValueError(f"{match_mode!r} names no entry module or no extension")
    return names, extensions


def action_for(match_mode):
    """Return the GitHub Action a `workflow_uses:` match mode names.

    A workflow that runs an agent names the vendor's action in a `uses:` line, and the three
    vendors that publish such an action publish the identifier verbatim. The mode contains the
    identifier because a rename changes the file name and not the identifier.
    """
    if not match_mode.startswith("workflow_uses:"):
        raise ValueError(f"The match mode {match_mode} names no action")
    return match_mode.split(":", 1)[1]


def workflow_actions(rows):
    """Return {action: tool} for every `workflow_uses:` row, in file order."""
    return {action_for(r["match_mode"]): r["tool"] for r in rows
            if r["match_mode"].startswith("workflow_uses:")}


def workflow_engines(rows):
    """Return {engine: tool} for every `workflow_engine:` row, in file order."""
    return {r["match_mode"].split(":", 1)[1]: r["tool"] for r in rows
            if r["match_mode"].startswith("workflow_engine:")}


def workflow_commands(rows):
    """Return {tool: [invocation, ...]} for every `workflow_command:` row, in file order.

    The mode contains the invocations that the vendor documents for running its command-line
    tool without a person, separated by `||`, such as `claude -p` and `codex exec`. A workflow
    that runs one of them in a shell step is that tool's configuration, as is a workflow that
    runs the tool's action.
    """
    found = {}
    for r in rows:
        if r["match_mode"].startswith("workflow_command:"):
            found.setdefault(r["tool"], []).extend(
                part.strip() for part in r["match_mode"].split(":", 1)[1].split("||")
                if part.strip())
    return found


# The match modes for which the tool is what a workflow file runs rather than where the file
# is. The path of such a row does not identify the tool of a file, because these rows share
# `.github/workflows`.
WORKFLOW_MODES = ("workflow_uses:", "workflow_engine:", "workflow_command:")
# The modes of a JSON file that counts by the value under one of its top-level keys.
JSON_KEY_MODES = ("json_servers:", "json_enabled:")


def json_key_counts(match_mode, data) -> bool:
    """Whether a parsed JSON document counts under a `JSON_KEY_MODES` row: for `json_servers:k`
    an object of at least one entry under the top-level key k, for `json_enabled:k` `true` or an
    object, empty or not, under it."""
    kind, _, key = match_mode.partition(":")
    value = data.get(key) if isinstance(data, dict) else None
    if kind == "json_servers":
        return isinstance(value, dict) and bool(value)
    if kind == "json_enabled":
        return value is True or isinstance(value, dict)
    raise ValueError(f"{match_mode} is not a JSON key mode")


def paths_under(rows, parent):
    """Return each path in the heuristics inside `parent`, as (relative path, is_dir).

    The collector and the validator take from this function the Copilot paths that they keep.
    A directory entry keeps everything below it, and a file entry keeps only itself. A workflow
    that the heuristics name, such as `.github/workflows/copilot-setup-steps.yml`, needs a file
    entry, because keeping the whole directory would publish every repository's CI.

    A row of `WORKFLOW_MODES` is left out for the same reason. Its path is `.github/workflows`,
    but the row matches a file in that directory that runs a named action or an agent, not the
    directory. Counting the row here would count an ordinary `ci.yml` as Copilot configuration
    and copy `.github` whole for every repository with a workflow.
    """
    prefix = parent.rstrip("/") + "/"
    found = []
    seen = set()
    for r in rows:
        path = r["path"]
        if not path.startswith(prefix) or r["match_mode"].startswith(WORKFLOW_MODES):
            continue
        is_dir = is_directory_mode(r["match_mode"])
        rel = path[len(prefix):]
        if is_dir:
            rel = rel.split("/")[0]
        if rel and rel not in seen:
            seen.add(rel)
            found.append((rel, is_dir))
    return found


# ---------------------------------------------------------------------------
# Which column of a published CSV names the configuration artifact
# ---------------------------------------------------------------------------
# Each mechanism has one CSV with rows keyed by `repo_name` plus one column that names the
# configuration artifact. That column is not derived by position, nor as "the first column
# that is not leading metadata", because a column added to the metadata prefix moves it. A step
# that read another column as the path would match nothing and prune every row as untracked
# without failing. The heuristics already list the mechanisms, and the mapping is written down
# once, here. `test_heuristics.py` fails when a mechanism in the heuristics has no CSV or a CSV
# names a mechanism that the heuristics do not name.

# Each mechanism of the heuristics with its published filename and the column that names the
# configuration artifact. `collect_only` has no CSV, because it names a path that is copied out
# and classifies nothing.
MECHANISM_CSVS = {
    "settings":     ("settings.csv", "settings"),
    "context_file": ("context-files.csv", "context_file"),
    "skills":       ("skills.csv", "skills.md"),
    "subagents":    ("subagents.csv", "subagent"),
    "commands":     ("commands.csv", "command"),
    "hooks":        ("hooks.csv", "hook"),
    "rules":        ("rules.csv", "rule"),
    "mcp":          ("mcp.csv", "mcp"),
    "plugins":      ("plugins.csv", "plugin"),
    "lsp":          ("lsp.csv", "lsp"),
    "other":        ("other.csv", "other"),
}

ARTIFACT_COLUMNS = tuple(artifact for _, artifact in MECHANISM_CSVS.values())

_ARTIFACT_BY_FILE = {filename: artifact
                     for filename, artifact in MECHANISM_CSVS.values()}


# ---------------------------------------------------------------------------
# The published header of each mechanism CSV
# ---------------------------------------------------------------------------
# One CSV per mechanism, written by the detection step.
#
# The header is defined here rather than in the detector because other scripts read it too.
# The file history step fills the columns that the cache could not fill. The snapshot writer
# counts the rows, and the regression manifest records the column list. `test_heuristics.py`
# compares this declaration with the detector's `write_sub` calls by AST, and a column added in
# one place and not the other fails the test rather than producing a CSV with an undeclared
# header.

# The metadata block that every published CSV has, in order. `scanned_at` is
# when the repository was collected, `head_sha` the commit that was collected, and
# `head_commit_date` the committer date of that commit.
META_COLUMNS = ("repo_name", "scanned_at", "branch", "head_sha", "head_commit_date")

# The columns that `git log` over one path fills, in published order. `caches.py` imports this
# list rather than restating it, because the cache's columns and the output CSVs' columns are one
# list. A column added to one and not the other would be the inconsistency that this file exists
# to prevent.
HISTORY_COLUMNS = ("created_at", "#commits", "first_commit_sha", "last_commit_sha")

# The mechanisms with a CSV that has the configuration artifact's natural language. The
# configuration artifacts of the other mechanisms contain JSON, TOML or whatever `detect_other`
# collected, and a language detector would report a language for text that has none.
LANGUAGE_MECHANISMS = frozenset({"context_file", "skills", "subagents",
                                 "commands", "rules"})

# What the detection step reads out of the file itself, for context files only.
REFERENCE_COLUMNS = ("line_count", "is_reference", "referenced_file")

# The AI-authorship columns of one context file.
AI_COLUMNS = ("first_commit_ai_created", "first_commit_ai_tool", "#ai_commits",
              "ai_commit_tools")

# Per mechanism, the columns that its detector writes between the configuration artifact column
# and `github_link`. A mechanism with rows that contain a path and nothing else has none.
_DETAIL_COLUMNS = {
    "skills":    ("name", "scripts", "references", "assets"),
    "subagents": ("name", "memory"),
    "commands":  ("name",),
    "rules":     ("name",),
    # The path at which a tool reads a file of `other` that a symlinked directory leads to,
    # blank for a file read where it is. Not published, as other.csv is not.
    "other":     ("link_path",),
}


def detail_columns(mechanism):
    """The per-mechanism columns that the detector writes beside the configuration artifact."""
    if mechanism not in MECHANISM_CSVS:
        raise ValueError(f"The mechanism {mechanism!r} has no published CSV")
    return _DETAIL_COLUMNS.get(mechanism, ())


def csv_header(mechanism):
    """The full published header of one mechanism's CSV, in order."""
    _, artifact = MECHANISM_CSVS[mechanism]
    header = [*META_COLUMNS, artifact, "tool", *detail_columns(mechanism),
              "github_link", "dataset_version", "created_at", "#commits"]
    if mechanism in LANGUAGE_MECHANISMS:
        header.append("language")
    header += ["first_commit_sha", "last_commit_sha"]
    if mechanism == "context_file":
        header += [*REFERENCE_COLUMNS, *AI_COLUMNS]
    return header


# The six tools in the order of the tool columns of `repos.csv`, and the value of a
# configuration artifact's `tool` column when no specific tool owns the configuration artifact:
# a file that several tools read, such as AGENTS.md, `.agents/skills` or the root `.mcp.json`,
# through a heuristics row with the tool `any`.
TOOLS = ("claude", "gemini", "codex", "copilot", "cursor", "opencode")
SHARED_TOOL = "any"


def tool_value(tools):
    """The `tool` column of a configuration artifact: its tools in column order, joined by
    `|`, or `any` when no specific tool owns it."""
    return "|".join(t for t in TOOLS if t in tools) or SHARED_TOOL


def value_tools(value):
    """The tools that a `tool` column names, the inverse of `tool_value`: empty for `any`. A
    value that `tool_value` cannot write, blank, unknown or out of column order, raises
    ValueError."""
    tools = set() if value == SHARED_TOOL else set(value.split("|"))
    if tool_value(tools) != value:
        raise ValueError(f"{value!r} is not a tool column value")
    return tools


def csv_filenames():
    """Every published mechanism filename, in file order."""
    return [filename for filename, _ in MECHANISM_CSVS.values()]


def artifact_column_for_file(filename):
    """The column that names the configuration artifact in `filename`.

    Prefer this function over `artifact_column` wherever the caller has the filename,
    because a filename cannot be ambiguous and a header can be.
    """
    try:
        return _ARTIFACT_BY_FILE[filename]
    except KeyError:
        raise ValueError(
            f"No published CSV is named {filename!r}. The published CSVs are "
            f"{', '.join(sorted(_ARTIFACT_BY_FILE))}") from None


def artifact_column(columns):
    """The column naming the configuration artifact in a published header.

    For a caller that has the header and not the filename. Ambiguity is an
    error rather than a first match, so that a CSV that gains a column named after
    another mechanism is caught here instead of keying its rows on the wrong
    column.
    """
    found = [c for c in ARTIFACT_COLUMNS if c in columns]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(
            f"The columns {list(columns)} include none of the configuration artifact columns "
            f"{', '.join(ARTIFACT_COLUMNS)}")
    raise ValueError(
        f"{list(columns)} has more than one configuration artifact column "
        f"({', '.join(found)}). Look the CSV up by its filename.")


# ---------------------------------------------------------------------------
# Matching a configuration artifact back to the heuristic that could have produced it
# ---------------------------------------------------------------------------

def _segments_match(pattern: str, path: str) -> bool:
    """Whether `path` is `pattern` with each `*` standing for one segment, ignoring case."""
    want, have = pattern.lower().split("/"), path.lower().split("/")
    return len(want) == len(have) and all(w == "*" or w == h for w, h in zip(want, have))


def _under(pattern: str, path: str) -> bool:
    """Whether `path` lies strictly below the directory `pattern` names."""
    want, have = pattern.lower().split("/"), path.lower().split("/")
    return (len(have) > len(want)
            and all(w == "*" or w == h for w, h in zip(want, have[:len(want)])))


def heuristic_matches(heuristic_row: dict, path: str) -> bool:
    """Whether one heuristic could have produced a configuration artifact at `path`."""
    mode, pattern = heuristic_row["match_mode"], heuristic_row["path"]
    inner = nested_inner(mode)
    if inner is not None:
        # The place below the root: the inner mode matches the path after one or more of its
        # leading directories.
        segments = path.split("/")
        inner_row = {**heuristic_row, "match_mode": inner}
        return any(heuristic_matches(inner_row, "/".join(segments[depth:]))
                   for depth in range(1, len(segments)))
    base = path.rsplit("/", 1)[-1]
    if mode in ("file", "root_file", "json_hooks", "json_hooks_or_modules", "json_named_hooks", "toml_hooks",
                "json_mcp_servers",
                "toml_mcp_servers", "json_plugin_mcp", "json_copilot_marketplace"):
        return _segments_match(pattern, path)
    if mode.startswith(("registered:",) + JSON_KEY_MODES):
        return _segments_match(pattern, path)
    if mode == "agent_memory":
        return _under(pattern, path)
    if mode.startswith("name_glob:"):
        return (_under(pattern, path) and path.count("/") == pattern.count("/") + 1
                and fnmatch.fnmatchcase(base, mode[len("name_glob:"):]))
    if mode in AGENT_PLUGINS_DOCUMENTS:
        # A path cannot decide these modes. `plugin.json` and `mcp.json` are the documented
        # names of the portable layout and of several unrelated programs, and only the schema
        # that the document declares separates them. Crediting them by name alone would also
        # take the credit away from the heuristic that did produce the row. A basename matches
        # at depth 0 and the search stops at the first depth that matches, and every nested
        # .claude-plugin/plugin.json would then be credited to the portable heuristic and to
        # nothing else.
        return False
    if mode in ("basename", "basename_json_mcp_servers", "skill_marker"):
        return base.lower() == pattern.lower()
    if mode == "root_any_case":
        return path.lower() == pattern.lower()
    if mode.startswith(("tree_ext:", "tree_ext_described:")):
        return _under(pattern, path) and base.lower().endswith(tuple(mode.split(":", 1)[1].split("|")))
    if mode.startswith("tree_suffix:"):
        return _under(pattern, path) and base.lower().endswith(mode[len("tree_suffix:"):])
    if mode.startswith(("dir_ext:", "dir_ext_described:", "dir_ext_triggered:")):
        # The front matter is not part of the path, and a published row has passed the
        # detector's test.
        return (_under(pattern, path) and base.lower().endswith(tuple(mode.split(":", 1)[1].split("|")))
                and path.count("/") == pattern.count("/") + 1)
    if mode.startswith(PACKAGE_ENTRY_MODE):
        # The entry module of a folder directly in the directory. Which of a folder's entry
        # modules is its row depends on the files beside it, which the path does not show.
        names, extensions = package_entries(mode)
        return (_under(pattern, path) and path.count("/") == pattern.count("/") + 2
                and base in {name + extension for name in names for extension in extensions})
    if mode == "dir_agent:.md":
        return (_under(pattern, path) and (
            path.count("/") == pattern.count("/") + 1 and base.endswith(".md")
            or path.count("/") == pattern.count("/") + 2 and is_folder_agent_file(base)))
    if mode == "skill_dir_flat":
        return _under(pattern, path) and path.count("/") == pattern.count("/") + 2
    if mode in ("skill_dir", *SPEC_DIR_MODES, "unclassified_dir", "copy_dir", "copy_dir_gated"):
        return _under(pattern, path)
    if mode == "plugin_sibling":
        return base == pattern
    return False


def match_heuristics(heuristic_rows: list, mechanism: str, artifact: str) -> tuple:
    """Every heuristic that a configuration artifact of `mechanism` matches, and the depth at
    which they matched.

    The path is tried as it is and then at every suffix below a directory, which is how a
    mechanism directory below the repository root is found. The first depth at which any
    heuristic matches is the result. Every heuristic that could have produced the row is
    credited. A `SKILL.md` under `.claude/skills`, for example, credits both the directory
    heuristic and the marker heuristic that counts a `SKILL.md` anywhere as a skill. Returns
    `(heuristic_rows, depth)`, with a heuristic that names a directory before a heuristic that
    names a basename, or `([], 0)` when no heuristic of that mechanism matches the path.
    """
    segments = artifact.split("/")
    candidates = [r for r in heuristic_rows if r["mechanism"] == mechanism]
    for depth in range(len(segments)):
        suffix = "/".join(segments[depth:])
        matched = [r for r in candidates if heuristic_matches(r, suffix)]
        if matched:
            matched.sort(key=lambda r: (r["match_mode"] in ("basename", "skill_marker",
                                                            "basename_json_mcp_servers"),
                                        -len(r["path"])))
            return matched, depth
    return [], 0


# ---------------------------------------------------------------------------
# The AI-commit pattern heuristics
# ---------------------------------------------------------------------------
AI_COMMIT_FIELDNAMES = ["id", "tool", "scope", "attribution", "pattern", "source", "doc_url",
                        "doc_verified", "evidence", "notes"]

SCOPES = ("author", "trailer", "full_body")
SOURCES = ("doc", "sample")
# The attributions, in the order in which a line of a commit message takes its attribution: a
# line that a `reviewer` heuristic matches is `reviewer`, whatever else matches it, and a line
# that a `standard` heuristic matches is not also `convention`. Only an `author` heuristic has
# `account`, and an author heuristic has `account` or `convention` (`AUTHOR_ATTRIBUTIONS`). The
# section Attributions of the module docstring and the paragraph Attributions of
# docs/heuristics.md define each value.
ATTRIBUTIONS = ("account", "reviewer", "standard", "convention")
AUTHOR_ATTRIBUTIONS = ("account", "convention")
# `<tool>-<scope>-<nn>`: the tool and the scope as lowercase words joined by hyphens, and a
# number counted per tool and scope. An id names one heuristic permanently. A removed row's id
# is not given to another row, and the published `matched_heuristics` stays readable
# against any release's heuristics.
_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{2,}$")
# The ids of the commit heuristics that were removed, of both commit heuristics files. No row
# may take one, and a removed row's id belongs here.
REMOVED_COMMIT_HEURISTIC_IDS = frozenset({
    "claude-full-body-10", "codex-full-body-09", "oh-my-opencode-trailer-01", "gitar-trailer-01",
    "hermes-agent-author-01", "openclaw-author-01", "claude-author-10", "claude-trailer-20",
    "claude-trailer-28", "codex-author-14", "codex-trailer-26", "gemini-author-07",
    "gemini-trailer-08", "kimi-code-trailer-03", "opencode-trailer-09", "opencode-trailer-13",
    "opencode-trailer-14", "grok-cli-trailer-01", "grok-cli-full-body-01",
})


def heuristic_id_prefix(tool: str, scope: str) -> str:
    """The part of a heuristic's id before its number: the tool and the scope as words."""
    return "-".join(re.sub(r"[^a-z0-9]+", "-", part.lower()).strip("-")
                    for part in (tool, scope))
# A negative can also come from a repository that the collected file cleanup dropped because it
# matched no mechanism. Such a repository is in no output CSV. `unmatched paths` finds the path
# glob in the collected tree, and a check of the row against `other.csv` cannot find the path
# glob there. The value `dropped` records this origin. Only the two negatives may use it,
# because a commit or a path heuristic describes what a run collects, and a dropped repository
# contributes nothing.
NEGATIVE_SOURCES = SOURCES + ("dropped",)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def evidence_problems(row: dict, where: str) -> list:
    """What is wrong with a row's `evidence`: a link that is not an https link, or no link where
    no page states the heuristic. A `sample` or `dropped` row was derived from repositories, and
    the links let a reader check the derivation. The note describes what was seen, and the
    links show it."""
    links = (row.get("evidence") or "").split()
    found = [f"The evidence {link!r} of {where} is not an https link"
             for link in links if not link.startswith("https://")]
    if row.get("source") in ("sample", "dropped") and not links:
        found.append(f"{where} has the source {row['source']} and no evidence. Link the commits "
                     f"or files that the heuristic was derived from.")
    return found


def _ai_commit_problems(rows: list, path: Path) -> list:
    """Every problem of the heuristics, rather than only the first."""
    found = []
    seen = set()
    for number, row in enumerate(rows, start=2):  # line 1 is the header
        where = f"{path.name}:{number} ({row.get('tool')}/{row.get('pattern')})"
        if not row["tool"]:
            found.append(f"{where} names no tool")
        if row["scope"] not in SCOPES:
            found.append(f"The scope of {where} is {row['scope']!r}, not one of {SCOPES}")
        prefix = heuristic_id_prefix(row["tool"], row["scope"]) + "-"
        if not _ID.match(row["id"] or "") or not row["id"].startswith(prefix) \
                or not row["id"][len(prefix):].isdigit():
            found.append(f"The id of {where} is {row['id']!r}, not {prefix}<number>")
        if row["id"] in seen:
            found.append(f"The id {row['id']!r} of {where} is also the id of another row")
        if row["id"] in REMOVED_COMMIT_HEURISTIC_IDS:
            found.append(f"The id {row['id']!r} of {where} was the id of a removed heuristic. "
                         f"Give the row a new id.")
        seen.add(row["id"])
        if row["attribution"] not in ATTRIBUTIONS:
            found.append(f"The attribution of {where} is {row['attribution']!r}, not one of "
                         f"{ATTRIBUTIONS}")
        elif (row["attribution"] == "account" and row["scope"] != "author") or (
                row["scope"] == "author" and row["attribution"] not in AUTHOR_ATTRIBUTIONS):
            found.append(f"{where} has the attribution {row['attribution']!r} with the scope "
                         f"{row['scope']!r}. Give an author heuristic one of "
                         f"{AUTHOR_ATTRIBUTIONS}, and 'account' to no other heuristic.")
        if row["source"] not in SOURCES:
            found.append(f"The source of {where} is {row['source']!r}, not one of {SOURCES}")
        try:
            compile_commit_pattern(row)
        except re.error as e:
            found.append(f"The pattern of {where} does not compile ({e})")
        if row["source"] == "doc" and not row["doc_url"]:
            found.append(f"{where} has the source doc and no doc_url")
        # A sample row may cite a page too. A Gemini account name, for example, is documented
        # as a pull request reviewer and not as a commit author. The documentation watch
        # should follow that page, which does not state that the tool writes the commit. The
        # date is required wherever a page is cited, because the documentation watch re-reads
        # every cited page, and an undated citation is one that nobody has read.
        if row["doc_url"] and not _DATE.match(row["doc_verified"] or ""):
            found.append(f"{where} cites a page, but its doc_verified is "
                         f"{row['doc_verified']!r}, not an ISO date")
        if row["source"] == "sample" and not (row["notes"] or "").strip():
            found.append(f"{where} has the source sample and no note. Describe in the note what "
                         f"was seen in the sample.")
        found += evidence_problems(row, where)
    return found


NOT_CONFIGURATION_FIELDNAMES = ["path", "tool", "source", "doc_url", "doc_verified", "evidence",
                                "notes"]

NOT_AI_COMMITS_FIELDNAMES = ["pattern", "source", "doc_url", "doc_verified", "evidence", "notes"]


def _negative_problems(rows, path, key) -> list:
    """Every problem of a negative heuristics file, rather than only the first."""
    found, seen = [], set()
    for i, row in enumerate(rows, start=2):
        where = f"row {i} ({row.get(key)})"
        if row[key] in seen:
            found.append(f"{where} is a second row for the same {key}")
        seen.add(row[key])
        if row["source"] not in NEGATIVE_SOURCES:
            found.append(f"The source of {where} is {row['source']!r}, not one of "
                         f"{NEGATIVE_SOURCES}")
        if row["source"] == "doc" and not row["doc_url"]:
            found.append(f"{where} has the source doc and no doc_url")
        if row["doc_url"] and not _DATE.match(row["doc_verified"] or ""):
            found.append(f"{where} cites a page, but its doc_verified is "
                         f"{row['doc_verified']!r}, not an ISO date")
        # The reason is the purpose of these files: a row without one records that a pattern
        # was passed over, and nobody can tell why.
        if not (row["notes"] or "").strip():
            found.append(f"{where} has no note that gives the reason")
        found += evidence_problems(row, where)
    return found


def _load_negative(path, fieldnames, key) -> list:
    """Every row as a dict, in file order, or raise ValueError with every problem."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(fieldnames) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the columns {sorted(missing)}")
        rows = [r for r in reader if r[key] and not r[key].startswith("#")]
    if not rows:
        raise ValueError(f"{path} has no rows")
    problems = _negative_problems(rows, path, key)
    if problems:
        raise problems_error(path, problems)
    return rows


def load_not_configuration(heuristics_path=None) -> list:
    """The path globs that are not a tool's configuration, each with the reason.

    `config_path_patterns.csv` defines what configuration is, and `not_configuration.csv` what
    it is not, for the path globs that `other.csv` collects, such as a helper script that a hook
    calls, a file with an extension that its own tool does not read, or a repository's own
    notes. `agentcontext/checks/unmatched.py` reads the file and lists every other path glob. A
    `doc` row cites the page that states that the tool does not read the file. A `sample` row
    was derived from the collected data. Its note describes what was seen, and its evidence
    links to where it was seen.
    """
    return _load_negative(Path(heuristics_path) if heuristics_path else NOT_CONFIGURATION_PATH,
                          NOT_CONFIGURATION_FIELDNAMES, "path")


def load_not_ai_commits(heuristics_path=None) -> list:
    """The commit attributions that credit no tool, each with the reason.

    The file has the layout of `not_configuration.csv`, and `unmatched commits` reads it. It
    names attributions such as dependency bots, translation platforms and release automation.
    Each pattern is matched inside the attribution token that the cloning step recorded.
    """
    return _load_negative(Path(heuristics_path) if heuristics_path else NOT_AI_COMMITS_PATH,
                          NOT_AI_COMMITS_FIELDNAMES, "pattern")


def not_configuration(rows=None) -> dict:
    """{path glob: why it is not configuration}, in file order."""
    rows = load_not_configuration() if rows is None else rows
    return {r["path"]: r["notes"] for r in rows}


def not_ai_commits(rows=None) -> dict:
    """{attribution: why it is not a tool}, in file order."""
    rows = load_not_ai_commits() if rows is None else rows
    return {r["pattern"]: r["notes"] for r in rows}


def load_ai_commit_patterns(heuristics_path=None) -> list:
    """Every heuristic as a dict, in file order, or raise ValueError with every problem."""
    path = Path(heuristics_path) if heuristics_path else AI_COMMIT_PATTERNS_PATH
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(AI_COMMIT_FIELDNAMES) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the columns {sorted(missing)}")
        rows = [r for r in reader if r["pattern"] and not r["pattern"].startswith("#")]
    if not rows:
        raise ValueError(f"{path} has no rows")
    problems = _ai_commit_problems(rows, path)
    if problems:
        raise problems_error(path, problems)
    return rows


def load_other_ai_commit_patterns(heuristics_path=None, tools=None) -> list:
    """The heuristics for the tools outside the six, in the columns and under the
    checks of `ai_commit_patterns.csv`.

    Their matches go to their own CSV, `other_ai_commits.csv`, so that they leave the six
    tools' columns and counts unchanged. A row that names one of `tools`, the six by default,
    would put a commit of one of them in that CSV and not in the commit CSV, and is refused.
    """
    path = Path(heuristics_path) if heuristics_path else OTHER_AI_COMMIT_PATTERNS_PATH
    rows = load_ai_commit_patterns(path)
    studied = {t.lower() for t in (ai_commit_tools() if tools is None else tools)}
    named = sorted({r["tool"] for r in rows if r["tool"].lower() in studied})
    if named:
        raise ValueError(f"The rows of {', '.join(named)} in {path} belong in "
                         f"{AI_COMMIT_PATTERNS_PATH.name}")
    return rows


def ai_commit_tools(rows=None) -> tuple:
    """The tool names, in the order in which the heuristics first name each one."""
    rows = load_ai_commit_patterns() if rows is None else rows
    seen = []
    for row in rows:
        if row["tool"] not in seen:
            seen.append(row["tool"])
    return tuple(seen)


# A trailer heuristic matches where its token begins a token: after a character that cannot continue
# a token, or after an escaped line break. Some tools write the two characters `\n` into a message
# in place of a line break, which puts the trailers on the subject line. A token that only ends in
# the heuristic's token does not match, such as `Commit-Message-Assisted-by:`, which credits the
# commit message rather than the change. The prefixes below name who or what assisted with the
# change, as in `AI-Assisted-By:`, `Co-Assisted-By:` and `Tests-Assisted-By:`, or how much of the
# change the credited author wrote, as in `Almost-Exclusively-Authored-By:`, and a token after one
# of them counts as the token itself (the section "Trailer extraction" of docs/commits.md).
# `Co-authored-by` is a token of its own, the co-author line, which the heuristics read under its
# full name. `Co-` before `authored-by` is therefore no prefix, and a heuristic of the token
# `Authored-by:` does not also read every co-author line.
TRAILER_TOKEN_PREFIXES = ("ai", "almost-exclusively", "co", "tests")
_TRAILER_TOKEN_START = (r"(?:(?<![A-Za-z0-9-])|(?<=\\n))(?:(?!co-authored-by)(?:"
                        + "|".join(TRAILER_TOKEN_PREFIXES) + r")-)?")


# The parser of `re` is the only reader of a pattern's structure, and `tests/test_commits.py` fails
# when a Python release changes what `required_literals` reads from it.
_REPEATS = (_sre_constants.MAX_REPEAT, _sre_constants.MIN_REPEAT, _sre_constants.POSSESSIVE_REPEAT)
_SHORTEST_REQUIRED_LITERAL = 2
# Dotted and dotless i, long s and the Kelvin sign.
_ASCII_CASE_VARIANTS = str.maketrans({"\u0130": "i", "\u0131": "i", "\u017f": "s", "\u212a": "k"})


def compile_commit_pattern(row: dict) -> re.Pattern:
    """A commit heuristic's pattern, case-insensitive, and for the trailer scope bound to where a
    trailer token begins."""
    if row["scope"] == "trailer":
        return re.compile(_TRAILER_TOKEN_START + "(?:" + row["pattern"] + ")", re.IGNORECASE)
    return re.compile(row["pattern"], re.IGNORECASE)


def heuristic_pattern(compiled: re.Pattern) -> str:
    """The pattern of the heuristic that `compile_commit_pattern` compiled, as its row gives it."""
    text = compiled.pattern
    if text.startswith(_TRAILER_TOKEN_START + "(?:") and text.endswith(")"):
        return text[len(_TRAILER_TOKEN_START) + 3:-1]
    return text


def _required_in(sequence) -> list:
    """The sets of literals that a match of a parsed sequence contains, one literal of each.

    Only what a match has to contain counts: a run of ASCII characters, a group, a part that
    repeats at least once, and an alternation in which every alternative contains a literal. A
    lookaround, a class and an optional part contribute nothing."""
    required = []
    run = []
    for operator, argument in [*sequence, (None, None)]:
        if operator is _sre_constants.LITERAL and argument < 128:
            run.append(chr(argument))
            continue
        if run:
            required.append(frozenset({"".join(run).lower()}))
            run = []
        if operator is _sre_constants.SUBPATTERN:
            required.extend(_required_in(argument[3]))
        elif operator is _sre_constants.ATOMIC_GROUP:
            required.extend(_required_in(argument))
        elif operator in _REPEATS and argument[0] >= 1:
            required.extend(_required_in(argument[2]))
        elif operator is _sre_constants.BRANCH:
            alternatives = [_required_in(alternative) for alternative in argument[1]]
            if all(alternatives):
                required.append(frozenset().union(*[
                    max(sets, key=lambda literals: (min(map(len, literals)), -len(literals)))
                    for sets in alternatives]))
    return required


def required_literals(pattern: str) -> tuple:
    """The sets of lowercase literals of a pattern: a text that the pattern matches without regard
    to case contains, after `lowered_for_literals`, at least one literal of each set.

    `commits._any_match` runs every heuristic on every commit of a history, and most commits
    contain no literal of most heuristics, so that checking the literals settles most heuristics
    without running their patterns. A literal of one character rejects almost nothing and is left
    out."""
    return tuple(literals for literals in _required_in(_sre_parser.parse(pattern, re.IGNORECASE))
                 if min(map(len, literals)) >= _SHORTEST_REQUIRED_LITERAL)


def lowered_for_literals(text: str) -> str:
    """`text` in the case of `required_literals`. A pattern that ignores case also matches four
    characters outside ASCII with an ASCII letter, and `str.lower` keeps these apart from it."""
    return (text if text.isascii() else text.translate(_ASCII_CASE_VARIANTS)).lower()


def compiled_ai_commit_patterns(rows=None) -> list:
    """One (tool, author, trailer, full_body) entry per tool, patterns compiled.

    The columns that the detector uses, built here so that the detector keeps no copy of the
    heuristics. Case-insensitive, because a bot account and a trailer token can be written in
    any case.
    """
    rows = load_ai_commit_patterns() if rows is None else rows
    by_tool = {tool: {scope: [] for scope in SCOPES} for tool in ai_commit_tools(rows)}
    for row in rows:
        by_tool[row["tool"]][row["scope"]].append(compile_commit_pattern(row))
    return [(tool, scoped["author"], scoped["trailer"], scoped["full_body"])
            for tool, scoped in by_tool.items()]


def ai_commit_patterns(scope=None, source=None, rows=None) -> list:
    """The pattern strings, filtered. For the tests and the counts of the check."""
    rows = load_ai_commit_patterns() if rows is None else rows
    return [r["pattern"] for r in rows
            if (scope is None or r["scope"] == scope)
            and (source is None or r["source"] == source)]


# ---------------------------------------------------------------------------
# The vendored directories
# ---------------------------------------------------------------------------
def load_vendored_dirs(path=None) -> set[str]:
    """Return the directory names in the list, ignoring comments and blank lines."""
    with open(path or VENDORED_DIRS_PATH, encoding="utf-8") as f:
        return {line.strip() for line in f
                if line.strip() and not line.lstrip().startswith("#")}


VENDORED_DIRS = load_vendored_dirs()


# ---------------------------------------------------------------------------
# The local state of the tools
# ---------------------------------------------------------------------------
# The characters that a place may not contain. `**` would match across components, which
# rsync's anchored patterns and `paths.local_state` read differently. A placeholder names no
# directory of the tree, and the character classes and alternatives of rsync and fnmatch differ.
_PLACE_FORBIDDEN = ("**", "<", ">", "[", "]", "{", "}", "\\")


def _local_state_problems(rows: list, path: Path, tool_dirs: set) -> list:
    """Every problem of the places of local state, beyond those of a negative heuristics file."""
    found = []
    for i, row in enumerate(rows, start=2):
        where = f"{path.name}:{i} ({row['path']})"
        place = row["path"]
        parts = place.rstrip("/").split("/")
        if parts[0] not in tool_dirs:
            found.append(f"{where} does not start with a tool directory that the collector "
                         f"copies whole, one of {sorted(tool_dirs)}")
        if len(parts) < 2 or "" in parts or place.endswith("//"):
            found.append(f"{where} names no place below the tool directory, or has an empty "
                         f"component")
        if any(token in place for token in _PLACE_FORBIDDEN):
            found.append(f"{where} contains one of {_PLACE_FORBIDDEN}. Use only * and ? for a "
                         f"part of a component.")
    return found


def load_local_state_paths(heuristics_path=None, patterns=None) -> list:
    """The places of the tools' local state, each with its grounding, in file order, or raise
    ValueError with every problem. `patterns` are the path patterns, and their rows of
    `SPEC_DIR_MODES` name the tool directories that a place can start with."""
    path = Path(heuristics_path) if heuristics_path else LOCAL_STATE_PATHS_PATH
    rows = _load_negative(path, NOT_CONFIGURATION_FIELDNAMES, "path")
    patterns = load_config_path_patterns() if patterns is None else patterns
    problems = _local_state_problems(rows, path, set(spec_dir_paths(patterns)))
    if problems:
        raise problems_error(path, problems)
    return rows


LOCAL_STATE_PATHS = tuple(row["path"] for row in load_local_state_paths())
