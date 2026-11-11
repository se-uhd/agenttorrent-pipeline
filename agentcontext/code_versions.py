"""The versions of the code that decides what a collection contains, in the order in which they
were made, and the repositories that each change from one version to the next reaches.

Each line of the list of finished repositories records the fingerprint of the collector's code
(`reuse.collector_fingerprint`), and the import heuristics that it records contain the
fingerprint of the code of the imports (`reuse.import_code_fingerprint`). `COLLECTOR` lists every
version of the collector's code that a collection was made under, and `IMPORTS` every version of
the code of the imports. Each version after the first declares the reach of the change from the
version before it. The reach is `NO_REPOSITORY` for a change that alters no collection, such as a
refactoring or a reworded text, and `EVERY_REPOSITORY` for a change that can alter the collection
of any repository. Otherwise it is a predicate over what the earlier collection kept: a
callable that takes the file list as `(mode, path)` pairs; a `RecordReach`, which tests the
per-repository commit record; or a `WorkflowReach`, which tests the workflow files. Each
predicate is true for every repository with a collection that the change can alter.
A predicate over the file list reads the definitions of the collector's own code, such as
`paths._SKILL_PARENTS`, so that the predicate and the collector name the same places.

A later run takes over a repository that an earlier version collected when no change from that
version to the current one reaches the repository (`changes`, `reached`), and when the other
conditions of `reuse` hold. When no version records the earlier fingerprint or none records the
current one, every repository counts as reached. `tests/test_code_versions.py` fails while the
current fingerprint of either code is not the last version, so that every change of the code is
recorded with its reach. The section "Recording a change of the collector" of
docs/steps/repository_cloning.md gives the steps.

The module is outside the collector's code and outside the code of the imports, because it
decides which repositories a run collects again and not what a collection contains. Adding a
version therefore changes neither fingerprint.

The commits that a version names are plain values, commits of the private repository
`se-uhd/agenttorrent-pipeline-internal`, which holds the history of this tree, and no check
resolves them.
"""

import posixpath
import re
from dataclasses import dataclass
from typing import Callable

from agentcontext import heuristics, imports, paths

# The reach of a change that alters no collection, and of a change that can alter the collection
# of any repository.
NO_REPOSITORY = "no repository"
EVERY_REPOSITORY = "every repository"

# The mode with which git lists a symbolic link in a tree (`git ls-tree`), which the file list
# keeps (`clone.write_file_list`).
LINK_MODE = "120000"


@dataclass(frozen=True)
class RecordReach:
    """A reach that a predicate over the per-repository commit record decides, rather than one
    over the file list. `test` takes the commits of each copied path that the record keeps
    (`path_commits`, `commits.path_commits_entry`) and is true for every repository with a
    collection that the change can alter. A change of how the history of a path is read can alter
    only the repositories with a path that has a particular history, such as a path added again
    after a deletion, and no file list shows that history."""
    test: Callable


@dataclass(frozen=True)
class WorkflowReach:
    """A reach decided by a predicate over the workflow files that the earlier collection kept,
    rather than by a predicate over the file list. The cloning step copies those files into a
    directory beside the collected tree (`paths.copy_workflow_files`). `test` takes the text of
    each kept workflow by its file name and is true for every repository with a collection that
    the change can alter. A change of how the collector reads one kind of workflow step, such as
    a step that runs an agent action with a prompt file, can alter only the repositories with a
    workflow that has a step of that kind, and no file list shows the steps."""
    test: Callable


@dataclass(frozen=True)
class Version:
    """One version of the code: its fingerprint, the commits of the private repository that made
    it or the subject of such a commit, what changed from
    the version before it, and the reach of that change, which is `NO_REPOSITORY`,
    `EVERY_REPOSITORY`, a predicate over a file list, a `RecordReach` or a `WorkflowReach`. The
    first version of `COLLECTOR` and of `IMPORTS` has no reach."""
    fingerprint: str
    commits: str
    change: str
    reach: str | Callable | RecordReach | WorkflowReach | None


def linked_skill_file(entries: list) -> bool:
    """Whether a SKILL.md with its name in any letter case is a link in a folder directly in a
    skills directory (`paths._SKILL_PARENTS`) at any depth: the links that the cloning step
    follows because it reads the SKILL.md of each skill folder as a candidate
    (`paths.skill_files`). The test leaves the vendored directories aside and matches the skills
    directory in any letter case. It therefore reaches more repositories than those from which
    the collector copies another file."""
    parents = "|".join(re.escape(parent.lower()) for parent in paths._SKILL_PARENTS)
    linked = re.compile(rf"(?:.*/)?(?:{parents})/[^/]+/{re.escape(paths._SKILL_MARKER.lower())}")
    return any(mode == LINK_MODE and linked.fullmatch(path.lower()) is not None
               for mode, path in entries)


def plugin_json_outside_manifest_dirs(entries: list) -> bool:
    """Whether a file list has a `plugin.json` at any depth outside the vendors' manifest
    directories (`paths._MANIFEST_DIRS`), the file that can make its directory a Copilot CLI
    plugin by its content (`paths.copilot_legacy_manifest`). The test leaves the content, the
    vendored directories and the other manifests aside, and it therefore reaches more
    repositories than those from which the collector copies another file."""
    for _mode, path in entries:
        directory, _, name = path.rpartition("/")
        if name == paths._COPILOT_LEGACY_MANIFEST and not any(
                directory == d or directory.endswith("/" + d) for d in paths._MANIFEST_DIRS):
            return True
    return False


def plugin_json_in_an_unlisted_manifest_directory(entries: list) -> bool:
    """Whether a file list has a `plugin.json` directly in a directory named `.<name>-plugin`
    (`paths._VENDOR_MANIFEST_DIRECTORY`), such as `skills/.grok-plugin/plugin.json`, at a place
    where the collector tests the file with `paths.copilot_legacy_manifest`, which refuses it by
    the name of its directory. `paths.plugin_roots` tests a plugin.json outside the vendors'
    manifest directories (`paths._MANIFEST_DIRS`) as the legacy manifest of its own directory,
    and `paths.plugin_components` tests the root plugin.json of a directory that another Copilot
    CLI manifest makes a plugin (`paths.PLUGIN_ROOTS`). The test leaves the content and the
    vendored directories aside, and it therefore reaches more repositories than those from which
    the collector copies another file."""
    listed = {path for _mode, path in entries}
    for path in listed:
        directory, _, name = path.rpartition("/")
        if (name != paths._COPILOT_LEGACY_MANIFEST
                or not paths._VENDOR_MANIFEST_DIRECTORY.fullmatch(posixpath.basename(directory))):
            continue
        if not any(directory == d or directory.endswith("/" + d) for d in paths._MANIFEST_DIRS):
            return True
        if any(f"{directory}/{manifest}" in listed
               for manifest in paths.PLUGIN_ROOTS["<copilot-plugin>"]):
            return True
    return False


def review_md_at_root(entries: list) -> bool:
    """Whether a file list has the importing file of a row with the syntax
    `code_review_include` (`imports.import_patterns`), the root REVIEW.md of Claude Code Review,
    in exactly that letter case. Only the chains of such a file read the skills that a REVIEW.md
    includes (`imports.code_review_include`) and compare each name of an included path
    case-sensitively (`imports._spelled`)."""
    importing = {row["path"] for row in imports.import_patterns()
                 if row["syntax"] == "code_review_include"}
    return any(path in importing for _mode, path in entries)


def root_file_in_another_case(entries: list) -> bool:
    """Whether a file list has a root file that a row of `imports.ignores_case` reads, such as
    a `claude.md` or an `AGENTS.MD`: a name of such a row in another case than the name that a
    `basename` row of the import heuristics reads, CLAUDE.md, AGENTS.md or GEMINI.md. Only the
    chains of such a file open their imports in any case (`imports.chain`) and count a root
    CLAUDE file in any case (`imports._no_claude_md`). The test reads no file, and it therefore
    reaches more repositories than those from which the copy of the imports copies another
    file."""
    rows = imports.import_patterns()
    names = {row["path"].lower() for row in rows if imports.ignores_case(row)}
    own = {row["path"] for row in rows
           if row["match_mode"] == "basename" and row["path"].lower() in names}
    return any("/" not in path and path.lower() in names and path not in own
               for _mode, path in entries)


def antigravity_plugin(entries: list) -> bool:
    """Whether a repository can have an Antigravity plugin, with a manifest that the cloning step
    reads for the skills that its `skills` key lists (`paths._antigravity_manifest_skills`): a
    manifest in a folder of a default plugin directory (`paths.PLUGIN_PARENTS`) or a
    `plugins.json`, which can register a plugin anywhere, even behind a linked customization
    root. `paths.plugin_roots` follows no link to a plugin directory. The test matches the paths
    in any letter case and reads no file. It therefore reaches every repository with such a
    plugin, even one with no `skills` key in its manifest."""
    token = "<antigravity-plugin>"
    parents = "|".join(re.escape(parent.lower()) for parent in paths.PLUGIN_PARENTS[token])
    manifests = "|".join(re.escape(manifest.lower()) for manifest in paths.PLUGIN_ROOTS[token])
    manifest = re.compile(rf"(?:{parents})/[^/]+/(?:{manifests})")
    registrations = {posixpath.basename(row["path"]).lower() for row in paths._REGISTRATION_ROWS
                     if row["mechanism"] == "plugins"}
    return any(manifest.fullmatch(path.lower()) is not None
               or posixpath.basename(path).lower() in registrations
               for _mode, path in entries)


def linked_tool_directory(entries: list) -> bool:
    """Whether a file list has an entry with the name of a tool directory
    (`paths.TOOL_DIRECTORIES`), such as `.claude` or `.agents`, at any depth: the entries that
    the cloning step follows because it collects a tool directory that is a link as the link's
    marker and the directory that it leads to (`paths.linked_tool_directories`). git lists no
    directory, and such an entry is therefore a link, or a file that the step reads as the
    marker of one, or a gitlink. The test ignores the vendored directories, the mode and the
    target, including a target outside the repository, for which the step keeps only the
    marker. It matches the name in any letter case. It therefore reaches more repositories than
    those from which the collector copies another file."""
    names = {name.lower() for name in paths.TOOL_DIRECTORIES}
    return any(posixpath.basename(path).lower() in names for _mode, path in entries)


def _github_has_a_copilot_path(paths_listed: set) -> bool:
    """Whether a file list has an entry at or below a path of `paths._GITHUB_KEEP` in the
    `.github` at the root, with the first segment after `.github` matched in any letter case.
    `paths.collect_copilot` copies the `.github` at the root whole when the same test holds. A
    link or a file at a place of a directory counts as well, because the file list does not say
    what it leads to."""
    for path in paths_listed:
        head, _, rest = path.partition("/")
        if head != ".github" or not rest:
            continue
        first, _, below = rest.partition("/")
        for rel, _is_dir in paths._GITHUB_KEEP:
            keep_first, _, keep_below = rel.partition("/")
            if first.lower() != keep_first.lower():
                continue
            if not keep_below or below == keep_below or below.startswith(keep_below + "/"):
                return True
    return False


def _vendored(path: str, skill_folders: set) -> bool:
    """Whether a directory above `path` has the name of a vendored directory and no SKILL.md of
    its own in the file list, which `paths.is_vendored` leaves out of every listing."""
    parts = path.split("/")[:-1]
    return any(part in heuristics.VENDORED_DIRS and "/".join(parts[:i + 1]) not in skill_folders
               for i, part in enumerate(parts))


def _followed_link(path: str) -> bool:
    """Whether the link at `path` is one that the cloning step follows to the directory that it
    leads to and copies that directory with `paths.copy_dir`: a tool directory
    (`paths.TOOL_DIRECTORIES`), a directory of `paths._SYMLINK_PARENTS` or a link directly in
    one, at any depth and in any letter case."""
    lowered = path.lower()
    if posixpath.basename(path) in paths.TOOL_DIRECTORIES:
        return True
    parents = [parent.lower() for parent in paths._SYMLINK_PARENTS
               if "*" not in parent and not heuristics.plugin_placeholder(parent)]
    return any(candidate == parent or candidate.endswith("/" + parent)
               for candidate in (lowered, posixpath.dirname(lowered)) for parent in parents)


def _in_a_nested_folder(path: str) -> bool:
    """Whether `path` is below a folder of a `nested:` row in a `.github` below the root, such as
    `pkg/.github/skills`, which `paths.collect_nested_places` copies whole, with the folder's
    name in any letter case."""
    folders = {heuristic_row["path"].partition("/")[2].lower()
               for heuristic_row in paths.NESTED_ROWS
               if heuristics.is_directory_mode(heuristic_row["match_mode"])}
    parts = path.split("/")
    return any(parts[i] == ".github" and parts[i + 1].lower() in folders
               for i in range(1, len(parts) - 2))


def linked_directory_in_a_copied_directory(entries: list) -> bool:
    """Whether a file list has a link below the root, outside the vendored directories and the
    places of the tools' local state, in a directory that the cloning step can copy whole with
    `paths.copy_dir`, which writes into that directory the marker of a link to a directory that
    rsync leaves out (`paths.directory_links`). The file list records no target, and the test
    takes every such link for one that can lead to a directory out of the copied directory.

    Such a directory is a directory of `paths._SHARED_DIRS`, such as `.claude` or
    `.claude-plugin`, at any depth, the root's `.github` where it has a Copilot path
    (`_github_has_a_copilot_path`), and a folder of a `nested:` row in a `.github` below the
    root (`_in_a_nested_folder`). It can be any directory where the file list has a link that
    the step follows to the directory that it leads to (`_followed_link`), or a file that can
    make a directory a part of a plugin, the support folder of a skill or a registered skill: a
    plugin manifest (`paths.PLUGIN_ROOTS`), a marketplace or an Antigravity registration file
    (`paths._REGISTRATION_NAMES`). The test therefore reaches more repositories than those from
    which the collector copies another file."""
    listed = {path for _mode, path in entries}
    skill_folders = {posixpath.dirname(path) for path in listed
                     if posixpath.basename(path).lower() == paths._SKILL_MARKER.lower()}
    links = [path for mode, path in entries if mode == LINK_MODE]
    inner = [path for path in links if "/" in path and not _vendored(path, skill_folders)
             and paths.local_state(path) is None and paths.local_state(path, is_dir=True) is None]
    if not inner:
        return False
    shared = set(paths._SHARED_DIRS)
    if any(set(path.split("/")[:-1]) & shared or _in_a_nested_folder(path) for path in inner):
        return True
    if any(path.startswith(".github/") for path in inner) and _github_has_a_copilot_path(listed):
        return True
    names = ({posixpath.basename(manifest) for manifests in paths.PLUGIN_ROOTS.values()
              for manifest in manifests} | {posixpath.basename(paths._CURSOR_MARKETPLACE)}
             | set(paths._REGISTRATION_NAMES))
    return (any(_followed_link(path) for path in links)
            or any(posixpath.basename(path) in names for path in listed))


def agentic_workflow_file(entries: list) -> bool:
    """Whether a file list has a Markdown file directly in `.github/workflows/`
    (`paths.WORKFLOWS_SUBPATH`, `paths.AGENTIC_WORKFLOW_SUFFIX`), the place of a GitHub Agentic
    Workflow. The cloning step copies the files that such a workflow names in its runtime imports
    (`paths.agentic_workflow`). The test reads neither the front matter nor the body, and it
    therefore reaches more repositories than those from which the collector copies another
    file."""
    prefix = paths.WORKFLOWS_SUBPATH + "/"
    return any(path.startswith(prefix) and "/" not in path[len(prefix):]
               and posixpath.splitext(path)[1].lower() == paths.AGENTIC_WORKFLOW_SUFFIX
               for _mode, path in entries)


def path_added_again(path_commits: list) -> bool:
    """Whether a per-repository commit record has a path with a commit older than its recorded
    creation commit. `history.PathCommits.first` can date only such a path from an earlier
    commit, as for a path that a commit deleted and a later commit added again as a copy.
    `commits.path_commits_entry` lists the commits of a path oldest first by author date
    (`history.PathCommits.union`), with the creation commit first among the commits of its
    author date, and the entry of such a path therefore lists another commit first. A path with
    no creation commit has no history that the change of `history.PathCommits.first` can
    alter."""
    return any(entry.get("first") and entry.get("commits")
               and entry["commits"][0] != entry["first"] for entry in path_commits)


def prompt_file_input(workflows: dict) -> bool:
    """Whether a kept workflow runs an action of `paths._PROMPT_FILE_INPUTS` (`paths.uses_action`)
    and has a line that starts with that action's input, such as `prompt_file:` beside `uses:
    anthropics/claude-code-base-action@beta`. The cloning step copies the file that such an input
    names (`paths.action_prompt_files`). The test does not tie the input to the step of the
    action, and it therefore reaches more repositories than those from which the collector
    copies another file."""
    return any(paths.uses_action(text, action) and re.search(rf"^\s*(?:-\s*)?{re.escape(key)}\s*:",
                                                             text, re.M)
               for text in workflows.values() for action, key in paths._PROMPT_FILE_INPUTS.items())


def antigravity_root_folder_agent_or_claude_manifest(entries: list) -> bool:
    """Whether a file list has a path below a directory with the name of a `spec_dir_with_place`
    row (`paths.SPEC_DIR_PLACES`), such as `_agent`, which the cloning step copies only where it
    contains a place of Antigravity (`paths.is_spec_directory`), a file named `agent.md` in
    another letter case, which an Antigravity registration can select as the file of a folder
    agent (`paths._registration_candidates`), or a Claude Code plugin manifest or marketplace
    file at any depth, for which `paths.plugin_components` returns the servers and hooks that the
    manifest configures inline and the cloning step flags claude. The test matches the directory
    names in their case, as `paths.find_dirs_by_name` does, and reads no file. It therefore
    reaches more repositories than those from which the collector copies another file or writes
    another flag."""
    names = set(paths.SPEC_DIR_PLACES)
    for _mode, path in entries:
        parts = path.split("/")
        if names & set(parts[:-1]):
            return True
        if heuristics.is_folder_agent_file(parts[-1]) and parts[-1] != heuristics.FOLDER_AGENT_FILE:
            return True
        if parts[-2:] in ([".claude-plugin", "plugin.json"], [".claude-plugin", "marketplace.json"]):
            return True
    return False


def cursor_plugin_manifest(entries: list) -> bool:
    """Whether a file list has a Cursor plugin manifest or marketplace file, for which
    `paths.plugin_components` returns the MCP servers that it configures inline and the cloning
    step flags cursor. The test reads no file, and it therefore reaches more repositories than those
    with such servers. The collector copies no other file, because it copies `.cursor-plugin/`
    whole."""
    return any(path.split("/")[-2:] in ([".cursor-plugin", "plugin.json"],
                                        [".cursor-plugin", "marketplace.json"])
               for _mode, path in entries)


def codex_or_copilot_marketplace_or_portable_overlay(entries: list) -> bool:
    """Whether a file list has a Codex marketplace at any depth (`paths._CODEX_MARKETPLACE`), a
    Copilot CLI marketplace (`paths._COPILOT_MARKETPLACES`), the bare `marketplace.json` at the
    root and the others at any depth, or a `plugin.json` with a manifest of Codex or Copilot CLI
    (`paths.PLUGIN_ROOTS`) in its directory. `paths.plugin_roots` makes the folders that such a
    marketplace lists plugins of Codex or Copilot CLI, the cloning step copies the parts that
    these tools read there and the Copilot CLI marketplace (`paths.plugin_manifests`), and
    `paths.plugin_components` returns the portable `skills/` and `mcp.json` of a plugin with the
    tool's manifest or marketplace as the tool's parts, which the cloning step flags. The test
    reads no file and leaves the vendored directories aside, and it
    therefore reaches more repositories than those from which the collector copies another file
    or writes another flag."""
    listed = {path for _mode, path in entries}
    marketplaces = (paths._CODEX_MARKETPLACE, *paths._COPILOT_MARKETPLACES[1:])
    manifests = (*paths.PLUGIN_ROOTS["<codex-plugin>"], *paths.PLUGIN_ROOTS["<copilot-plugin>"])
    for path in listed:
        if path == paths._COPILOT_MARKETPLACES[0] or any(
                path == name or path.endswith("/" + name) for name in marketplaces):
            return True
        directory, _, name = path.rpartition("/")
        prefix = f"{directory}/" if directory else ""
        if name == "plugin.json" and any(f"{prefix}{manifest}" in listed for manifest in manifests):
            return True
    return False


def codex_marketplace(entries: list) -> bool:
    """Whether a file list has a Codex marketplace at any depth (`paths._CODEX_MARKETPLACE`), from
    which `paths.plugin_roots` makes Codex plugins of the local folders that it lists
    (`paths._codex_marketplace_entries`). The test reads no file and leaves the vendored
    directories aside, and it therefore reaches more repositories than those from which the
    collector copies another file or writes another flag."""
    return any(path == paths._CODEX_MARKETPLACE or path.endswith("/" + paths._CODEX_MARKETPLACE)
               for _mode, path in entries)


def agent_role_file(entries: list) -> bool:
    """Whether a file list has a file that a `declared_path:` row of the subagents reads
    (`paths._SUBAGENT_DECLARATION_HEURISTICS`): a role file below the row's directory, such as
    a TOML file below `.codex/agents`, the row's file, such as `.codex/config.toml`, which can
    name a role file anywhere, or an entry at the place of the row's path or of its folder,
    which can be a link to a directory that holds them (`paths.at_its_own_path`). The cloning
    step copies the files that these name (`paths.declared_subagent_files`). The test matches
    the paths in any letter case and reads no file. It therefore reaches more repositories than
    those from which the collector copies another file."""
    places = [heuristic_row["path"].lower() for heuristic_row in paths._SUBAGENT_DECLARATION_HEURISTICS]
    for _mode, path in entries:
        lowered = path.lower()
        for place in places:
            if (lowered.startswith(place + "/") and lowered.endswith(".toml")
                    or lowered in (place, posixpath.dirname(place))):
                return True
    return False


def opencode_agent_configuration(entries: list) -> bool:
    """Whether a file list has a configuration with OpenCode's agents
    (`paths._OPENCODE_AGENT_KEYS`): a bare filename such as `opencode.json` at the root, or a
    file of `.opencode` such as `.opencode/opencode.jsonc` at any depth. The cloning step reads
    the prompts of its agents and copies the files that they name (`paths.opencode_prompt_files`).
    The test matches the paths in any letter case and reads no file. It therefore reaches more
    repositories than those from which the collector copies another file."""
    places = {place.lower() for place, _content_format in paths._OPENCODE_AGENT_KEYS}
    for _mode, path in entries:
        lowered = path.lower()
        for place in places:
            if lowered == place or "/" in place and lowered.endswith("/" + place):
                return True
    return False


def agent_role_file_or_opencode_agent_configuration(entries: list) -> bool:
    """Whether a file list has a file that a `declared_path:` row of the subagents reads
    (`agent_role_file`) or a configuration with OpenCode's agents
    (`opencode_agent_configuration`). The cloning step copies the files that a Codex role or the
    Codex config names and the files that the prompt of an OpenCode agent names. One version
    records both changes, because one merge brought both onto `main`."""
    return agent_role_file(entries) or opencode_agent_configuration(entries)


def link_at_a_nested_place(entries: list) -> bool:
    """Whether a file list has an entry that the cloning step reads as a place of a `nested:` row
    (`paths.nested_places`) because such a place can be a link: a link at a place of a file
    below the root, such as dotnet/dotnet's `src/msbuild/.github/copilot-instructions.md` or a
    `pkg/.mcp.json`, and a link or a file at a place of a folder, such as `pkg/.github/agents`,
    which a file list holds as an entry of its own only where the place is no directory. The
    first segment after `.github` is matched in any letter case, as `paths.nested_places`
    matches it. The test leaves the targets, the vendored directories and the directories that
    their own rows read whole aside, and it therefore reaches more repositories than those from
    which the collector copies another file."""
    names, places = set(), {}
    for heuristic_row in paths.NESTED_ROWS:
        head, _, rest = heuristic_row["path"].partition("/")
        if rest:
            places[(head, rest.lower())] = heuristics.is_directory_mode(heuristic_row["match_mode"])
        else:
            names.add(head)
    for mode, path in entries:
        parts = path.split("/")
        if len(parts) >= 2 and parts[-1] in names and mode == LINK_MODE:
            return True
        if len(parts) >= 3:
            directory = places.get((parts[-2], parts[-1].lower()))
            if directory is not None and (directory or mode == LINK_MODE):
                return True
    return False


# The versions of the collector's code. The first is the fingerprint that the image of e99d5614
# computed, with exception texts and the files of the packages, and that every line of the run
# of all five stages that began on 2026-10-23 records. The image of 953988cf resumed that run
# under the same fingerprint.
COLLECTOR = (
    Version("5883c7f5c7ab4f8eeb54842cc29286fcb8a6be26f57eb9b7ed20e073bc52e874", "e99d5614",
            "The collector of the run of all five stages that began on 2026-10-23", None),
    Version("cc84f5445a1fcf7420a64199b7933dda242eacec428ea83f300209897df6bb59",
            "8a25cea8 1e99141d ce26d346 c81c44d2",
            "The fingerprint leaves out exception texts and the files of the packages. 8a25cea8 "
            "and 1e99141d reworded messages, the lines of the clone log and the `detail` that "
            "a per-repository commit record keeps when the commit history could not be read, "
            "which no step publishes. ce26d346 removed `caches.sync_filesystem`, which nothing "
            "called. c81c44d2 made the collector copy the file that a SKILL.md in a skill folder "
            "links to",
            linked_skill_file),
    Version("116231e4f18391fd99dd43f0e3f73d8128c1fdb2a0e79f2113856a1463b4ad3e",
            "Count a Copilot CLI plugin that only its root plugin.json identifies",
            "A plugin.json with the content of a legacy Copilot CLI manifest makes its "
            "directory a Copilot plugin, and the collector copies the plugin.json with the parts "
            "of the plugin. A root plugin.json beside another Copilot manifest is copied where it "
            "has that content",
            plugin_json_outside_manifest_dirs),


    Version("70201882317b6cbf2529ee59b875cf760f979fd4a1d8252620f59a0292527b53",
            "Read the skills that an Antigravity plugin's manifest lists",
            "The cloning step copies the skills that the `skills` key of an Antigravity plugin's "
            "manifest lists, resolved as the entries of the plugin's skills.json, and reads the "
            "manifest with comments and trailing commas",
            antigravity_plugin),
    Version("f4223f114c54a5e5dbea64d2e68ebf408f1c2c7b25284314144f68e6b2f99663",
            "Read a plugin.json in a .<name>-plugin directory as that vendor's manifest",
            "A plugin.json directly in a directory named `.<name>-plugin`, such as "
            "`skills/.grok-plugin/plugin.json`, is no legacy Copilot CLI manifest, even where "
            "`paths._MANIFEST_DIRS` does not list the vendor, and the collector no longer copies "
            "its directory as a Copilot plugin. The change reaches the repositories with such a "
            "plugin.json where the collector tests it with `paths.copilot_legacy_manifest`: "
            "outside `paths._MANIFEST_DIRS`, as the legacy manifest of its own directory, and "
            "beside another Copilot CLI manifest of its directory, as that plugin's root "
            "plugin.json",
            plugin_json_in_an_unlisted_manifest_directory),
    Version("a18e7b94db503db9505f6526a75538028f81c400c7ff5c51de71adc435c30b99",
            "Collect a tool directory that is a link",
            "A tool directory that is a link, such as a `.claude` that leads to `.agents` or a "
            "`.cursor` that leads to `ai`, is collected as the link's marker and the directory "
            "that it leads to, without the places of the tool's local state named from the "
            "link, and the links in that directory are followed as in the tool directory. A "
            "file that a pass reads by its path in the tool directory, such as `.claude/loop.md` "
            "or a Codex config, is read in the directory that the link leads to. A link out of "
            "the repository is collected as its marker alone",
            linked_tool_directory),
    Version("831feaa4418446ca3c966e2d277e54eb97c1fc692a9ebccacdc8c1db349da741",
            "Fix the local state, the .github files and the plans of a linked tool directory",
            "When a tool directory is a link, the copy of the directory that it leads to leaves "
            "out the places of the linking tool's local state, even where another pass copied "
            "that directory under its own name before, such as `.agents/shell-snapshots/` with "
            "`.claude -> .agents`. The planning documents are matched through such a link as "
            "well, and a folder that a settings file behind the link names is copied",
            linked_tool_directory),
    Version("20c31507cbba3ee5e8f9af1622c6e03f2eb923b244e77d781ea738276e76b0b1",
            "Leave out the local state after a second copy into a linked directory",
            "The places of the tools' local state in a directory that a tool directory links to, "
            "named from every tool directory that links to it, are removed once every link is "
            "followed, so that a second link to the directory, such as `.claude -> .agents` "
            "beside `.cursor -> .agents`, or the link of a mechanism directory into it does not "
            "copy them back",
            linked_tool_directory),
    Version("263856b2a9d356f3cf2be21aa8590bd3b25382e119d9be72f70ef35a6d8bd600",
            "Give committed suggestions of the claude app and listed ones of Devin the reviewer",
            "When a squash merge lists another squash merge, the co-author lines that the listed "
            "squash merge repeats below its own separator credit nothing of their own "
            "(`commits._repeated_co_author_lines`). A run applies this change of the commit "
            "attributions to the kept commit logs",
            NO_REPOSITORY),
    Version("8245ba45f740538aedc180a0c3dbf85d1c6a2be9fdc69b5d9f6a7110ba519733",
            "Keep the marker of a link to a directory that rsync leaves out",
            "`paths.copy_dir` writes the marker of a link to a directory that rsync leaves out of "
            "the directory that it copies, because the link leads out of it, such as "
            "meshery/meshery's `.claude/hooks` that links to `../.agents/hooks`. It writes the "
            "marker once every pass has copied, and only where no pass has copied a file below "
            "the link's path",
            linked_directory_in_a_copied_directory),
    Version("e21864b17263d4c9eeb99eab5fa2d51fb878a46abbcaed740a9b338ee3c9b6db",
            "Collect a place below the root that is a link",
            "A place of a `nested:` row below the root can be a link to a file or a folder of the "
            "repository, such as dotnet/dotnet's `src/msbuild/.github/copilot-instructions.md` "
            "that links to `../AGENTS.md`, and the collector copies it as its marker, with the "
            "folder that a link at the place of a folder leads to",
            link_at_a_nested_place),
    Version("2c940899b8834d2dadfd86dffba6958bce773a524b2a4f113d27cb55af19a2ee",
            "Credit IBM Bob and Ona and rank the attributions of Co-authored-by-AI",
            "`Co-authored-by-AI` is a token of an attribution that `unmatched commits` ranks "
            "(`commits.ATTRIBUTION_TOKENS`). A run applies this change of the commit records to "
            "the kept commit logs",
            NO_REPOSITORY),
    Version("633f662e472190b969ed1b7983d2b9bd68230022c4c7c60bdaa77df262374214",
            "Read more forms of oxc's disclosure and of OpenClaw and leave out Gitar's flag cleanups",
            "`heuristics.REMOVED_COMMIT_HEURISTIC_IDS` names `gitar-trailer-01`, the row for "
            "Gitar's co-author line that `gitar-full-body-01` replaces. A run applies this change "
            "of the commit heuristics to the kept commit logs",
            NO_REPOSITORY),
    Version("1eb427a4263f62e163299bc4eebaf70181a207e4b24668fd242a9e7a787d0cd4",
            "Leave out the names of Hermes Agent and njzjz-bot and widen the model after a tool",
            "`heuristics.REMOVED_COMMIT_HEURISTIC_IDS` names `hermes-agent-author-01` and "
            "`openclaw-author-01`, rows that read a name that users gave an identity. A run "
            "applies this change of the commit heuristics to the kept commit logs",
            NO_REPOSITORY),
    Version("47742c1bf6d0778099c41f5c1a3f88d9331efa238dbc72074ac71bc8ef3f5eff",
            "Count a name of a tool that users gave an identity as a convention",
            "An author heuristic can have `convention` (`heuristics.AUTHOR_ATTRIBUTIONS`), and "
            "`commits._attributions` gives the author and the committer the attribution of the "
            "author heuristic that matches them. A run applies this change of the commit "
            "attributions to the kept commit logs",
            NO_REPOSITORY),
    Version("5cd70950036f23d5f2c2ea4d7b80b9547537e57ca526fd6cf7c92d365ea79152",
            "Date a path added again from the commit that first added it",
            "`history.PathCommits.first` takes the first commit of the exact query where the "
            "follow query did not list it and its author date is earlier than that of the first "
            "commit of the follow query, as for a path that a commit deleted and a later commit "
            "added again as a copy of another file. The change alters `created_at` and "
            "`first_commit_sha` in `file_history.csv` and the creation commit in the "
            "per-repository commit record. It reaches the repositories with a path that has a "
            "commit older than its recorded creation commit, which the record shows and the file "
            "list does not",
            RecordReach(path_added_again)),
    Version("f7f8db3872f736af5e473cca48b5311e754ccffd3158f3610525ad3bb3e59e42",
            "Keep the files that a GitHub Agentic Workflow loads when it runs",
            "`paths.agentic_workflow` adds to the imported files of a GitHub Agentic Workflow "
            "the files that its body and its imported files name in runtime imports, "
            "`{{#runtime-import path}}` and the deprecated `{{#import path}}`, and the cloning "
            "step copies them with the workflow",
            agentic_workflow_file),
    Version("59016e09cca23f9090b1c381ddd68dbca55905654eacb67e568b81b3ab350cdb",
            "Copy the prompt file that a workflow gives the Claude Code Base Action",
            "`paths.action_prompt_files` reads the input `prompt_file` of a step that runs "
            "anthropics/claude-code-base-action or the action in the folder `base-action` of "
            "anthropics/claude-code-action, as it reads the input `prompt-file` of the Codex "
            "action, and the cloning step copies the file that the input names with the workflow",
            WorkflowReach(prompt_file_input)),
    Version("0ba3356803f89a9c475db5de78eff2461b200af5c72fc882328fddde29f64e02",
            "af39df85",
            "`commits._named_identity` records an author or a committer that names one of the six "
            "tools in its name or its address among the unmatched attributions of the "
            "per-repository commit record. A run applies this change of the commit records to the "
            "kept commit logs",
            NO_REPOSITORY),
    Version("03edcfd03dbf5d4673a852e7131610a1cad3defd9ce13b9e02bf856e069b5191",
            "36ff3263",
            "`commits.CLASSIFIED_BY` hashes the columns of the two commit heuristics files that "
            "the classification reads (`commits.CLASSIFICATION_COLUMNS`) rather than their bytes. "
            "The change alters the digest in the per-repository commit record and no other "
            "file, no row and no commit log of a collection, and a run applies it to the kept "
            "commit logs",
            NO_REPOSITORY),
    Version("331c245c6e27512551bf658a5681c7861bbf606dd0c21193a3e6e7cd765cf09d",
            "Read the tools that Authored-by, Created-by, Written-by and Made-with lines name",
            "`Co-` before `authored-by` is no prefix of a trailer token "
            "(`heuristics._TRAILER_TOKEN_START`), so that a heuristic of the token `Authored-by:` "
            "does not also read every co-author line, and `Almost-Exclusively-` is one "
            "(`heuristics.TRAILER_TOKEN_PREFIXES`). A run applies this change of the commit "
            "heuristics to the kept commit logs",
            NO_REPOSITORY),
    Version("e00dad497c613eb8aee5ac2ebd9a9c7175f1e6909f8eceac8fef9fc9acd6aa14",
            "Leave the release notes that a dependency update of Renovate quotes out of its credits",
            "Both scopes of the commit heuristics and the unmatched attributions leave the "
            "`### Release Notes` section of a message that names Renovate empty "
            "(`commits._own_lines`), because a credit there belongs to a commit of the "
            "dependency. A run applies this change of the commit attributions to the kept commit "
            "logs",
            NO_REPOSITORY),
    Version("f457727de2f28df1a489443b31a2926739a96f28f2b9d0dcba6243fe23399612",
            "Collect _agents and _agent only with a place of Antigravity, and count what a Claude "
            "Code manifest configures inline",
            "`paths.collect_shared` copies a directory of a `spec_dir_with_place` row, `_agents` or "
            "`_agent`, only where it contains a place of Antigravity with a file or a link below it "
            "(`paths.is_spec_directory`), and `paths._registration_candidates` takes a folder with "
            "an `agent.md` in any letter case as a folder agent (`heuristics.is_folder_agent_file`). "
            "`paths.plugin_components` adds a skill that an Antigravity plugin's manifest names "
            "below the depth that its default `skills/` reads, and returns a Claude Code manifest, "
            "or the marketplace file with the entry that stands in for a missing manifest, as an "
            "inline part where it configures MCP servers, LSP servers or hooks, which the cloning "
            "step then flags claude. The collector copies no other file for a Claude Code plugin, "
            "because it copies `.claude-plugin/` whole",
            antigravity_root_folder_agent_or_claude_manifest),
    Version("b3d2b5d5b9bd5dd1daf651de0070e4c0f2d2908a11de8becd519940386b984e3",
            "Read trailers after escapes, emoji and terminal colors, and leave out credits that a "
            "commit quotes",
            "`commits._own_lines` turns an escaped line break of a body line into a line break, and "
            "leaves the blocks that Dependabot quotes and the previous message that a version bump "
            "of kube-HPC/hkube quotes empty. `commits._trailer_line` reads a trailer after an emoji "
            "or the color codes of a terminal. `heuristics.REMOVED_COMMIT_HEURISTIC_IDS` names the "
            "rows that a merge of the rows replaced. A run applies this change of the commit "
            "attributions to the kept commit logs",
            NO_REPOSITORY),
    Version("f3210b7515c8db56d525df300b82462d87c7df1ff45baac8baa894d9a567e2de",
            "Read a ticked box of a task list as its bullet",
            "`commits._full_body` turns the marker of a ticked box into its bullet, so that the "
            "rows that read the start of a line see the text after the box. A run applies this "
            "change of the commit attributions to the kept commit logs",
            NO_REPOSITORY),
    Version("d964b15c241ee5b0b577a51b96152e40f993ac5bbf2d6c543b7c2edd158bd057",
            "Run a commit heuristic only on a text with the literals that a match has to contain",
            "`commits._any_match` checks the literals of `heuristics.required_literals` before it "
            "runs a pattern, which changes no attribution, and the digest of the classification "
            "changes with the code of the two modules. A run applies this change of the commit "
            "attributions to the kept commit logs",
            NO_REPOSITORY),
    Version("5af523d346285f1b3dc49d7909dacf0dcba32cf0105e600f06c22cb5383e72b5",
            "Read an escaped line break in a list item and in a subject, and the trailers below the "
            "release notes that an update of Renovate quotes",
            "`commits._own_lines` keeps an escaped line break only in a list item that names another "
            "commit by its hash (`commits._LIST_ITEM`), `commits._full_body` reads an escaped line "
            "break in the subject as a line break, and the section that "
            "`commits._RENOVATE_RELEASE_NOTES` leaves empty ends at the separator of a squash merge "
            "with only trailers after it. A run applies this change of the commit attributions to "
            "the kept commit logs",
            NO_REPOSITORY),
    Version("3b330a692507d6fea0d94340978867669b257c067aa5135ef02aeadb28e67740",
            "Count the MCP servers that a Cursor plugin manifest or marketplace entry configures inline",
            "`paths.plugin_components` returns a Cursor manifest, or the marketplace file with the "
            "entry that holds the servers, as an inline part where it configures MCP servers, which "
            "the cloning step then flags cursor. The collector copies no other file, because it "
            "copies `.cursor-plugin/` whole",
            cursor_plugin_manifest),
    Version("6504e477653d6c32c99b6eed419254d1ccc2ed9973362d1fdaf930c62cec6c5d",
            "Leave the block Original prompt of Copilot's cloud agent out of the credits",
            "`commits._own_lines` leaves the block empty in which the agent quotes the issue and "
            "the comments on it (`commits._ORIGINAL_PROMPT`). The change alters how a commit "
            "message is read, classifies again and copies no other file, so its reach is no "
            "repository",
            NO_REPOSITORY),
    Version("d09858c273ce125cb25f4940e636ff21454d6ddd9568015819262a83fe52b31c",
            "Leave the commits of Chromium that brave/brave-core quotes out of the credits",
            "`commits._own_lines` leaves the `git log` blocks below a line `Chromium changes:` "
            "empty (`commits._without_quoted_chromium_commits`). The change alters how a commit "
            "message is read, classifies again and copies no other file, so its reach is no "
            "repository",
            NO_REPOSITORY),
    Version("baee0ab19e5547c3b637ba334f6775dda9b49087302f11f4f6ee91b28482489f",
            "Count an Antigravity rule and a Cursor skill only with a front matter that the tool "
            "parses",
            "`paths.has_trigger` passes a rule only where its front matter, once a `globs` value "
            "that starts with `*` and a `description` with a colon are quoted "
            "(`paths.antigravity_rule_front_matter`), parses with PyYAML and has no list or "
            "mapping under `trigger`, `description`, `globs` or `glob`, as Antigravity CLI 1.4.10 "
            "decodes a rule. The cloning step therefore copies no registered rule and starts no "
            "import chain at a rule that does not decode. Of the Markdown files of the last "
            "collection, only GoogleCloudPlatform/khi's `.agents/rules/description-rule.md` passed "
            "the test before and fails it now, and it lies in the `.agents` that the step copies "
            "whole and imports nothing, so that a run that takes over that collection copies the "
            "same files",
            NO_REPOSITORY),
    Version("10c044024500dc72bce5a45f171b4096b4db55bcfe9b280678a4b15c7b662605",
            "Name the readers of a plugin that a marketplace or a manifest of the repository "
            "identifies",
            "`paths.plugin_roots` makes a folder that a Codex marketplace, "
            "`.agents/plugins/marketplace.json` at any depth, lists as a local source a Codex "
            "plugin, read through the first of `paths._CODEX_MANIFEST_ORDER` or the manifest that "
            "Codex writes from the entry, and a folder with a manifest that a Copilot CLI "
            "marketplace lists a Copilot CLI plugin, read through `paths._COPILOT_MANIFEST_ORDER`, "
            "which ends with `.claude-plugin/plugin.json`. The cloning step copies the parts that "
            "the tool reads there, and `paths.plugin_manifests` holds the Copilot CLI marketplace, "
            "also in a `.github` below the root. `paths.plugin_components` returns the portable "
            "`skills/` and `mcp.json` of a plugin that is a root of `<codex-plugin>` or "
            "`<copilot-plugin>` as well as parts of Codex or Copilot (`paths._PORTABLE_READERS`), "
            "which the cloning step flags. The change reaches the repositories with such a "
            "marketplace or with a manifest of Codex or Copilot CLI beside a `plugin.json`",
            codex_or_copilot_marketplace_or_portable_overlay),
    Version("1b683eabadde2c10f231c33000cf69a4db930a6749aa7cae2b893b231b612ca1",
            "00d6bfcf ae4b9c0e 2efed62f 6b75e1f9 0e1c4c3e",
            "`paths.collect_declared_subagents` copies the file that the `model_instructions_file` "
            "of a Codex role names and the role file that `config_file` of `.codex/config.toml` "
            "names outside `.codex/agents` (`paths.declared_subagent_files`), which no other pass "
            "copies outside `.codex`. `paths.collect_opencode` copies the files that a "
            "`{file:...}` reference in the `prompt` or `system` of an agent of an OpenCode "
            "configuration names (`paths.opencode_prompt_files`), which no other pass copies "
            "outside `.opencode`. `paths.declared_subagent_files` reads the role file that "
            "`config_file` names under the row `declared_path:agents.*.config_file`, also below "
            "`.codex/agents`, which the collector copies with `.codex`, and "
            "`paths.declared_context_files` reads every value of a key with a `*`, which no row of "
            "the context files has. `paths.plugin_components` lists a Claude Code manifest or "
            "marketplace file with inline MCP servers only where Claude Code accepts every server "
            "(`paths.claude_loads_a_server`), and the collector copies that file with its "
            "`.claude-plugin` directory in any case. The change reaches the repositories with a "
            "role file or a config of Codex, or with a configuration that can define OpenCode's "
            "agents",
            agent_role_file_or_opencode_agent_configuration),
    Version("775b56bcb1854a168b6c99e3cb9e62bf18cd42e6362989811edb9cb7768ece57",
            "Raise the limit of a CSV field where the heuristics are loaded",
            "`agentcontext.heuristics` raises the limit of a CSV field on import, so that a "
            "process reads the longest rows of the commit heuristics without another module. The "
            "collector reads the same rows and copies the same files, so its reach is no "
            "repository",
            NO_REPOSITORY),
    Version("e1843c27ca962d23dfc84f1d45b506c7d679f92c5e62891b44ec1d08ccbf28cb",
            "Count a Codex marketplace only with an entry from which Codex installs a plugin",
            "`paths._codex_marketplace_entries` reads a Codex marketplace only where it parses as "
            "Codex's marketplace with the values of a policy that Codex knows "
            "(`paths.codex_marketplace_plugins`), and it lists no local folder of an entry that "
            "`paths.codex_installs` refuses: an entry set to `NOT_AVAILABLE`, as before, an entry "
            "with `products` that leave out Codex and an entry with a name that the plugin IDs of "
            "Codex refuse. The cloning step then copies the parts of no plugin of such an entry. "
            "The change reaches the repositories with a Codex marketplace",
            codex_marketplace),
    Version("b553d785657fe1ce294e6534c6e5cb2c720b9cf9b871e98a8871f0b5e5a90f86",
            "Read a trailer after the bullet of GitHub's squash list",
            "`commits._trailer_line` reads a line without the bullet `* ` at its start when the "
            "rest has the form of a trailer (`commits._SQUASH_BULLET`), and the co-author line "
            "with an address that ends a top-level `* ` item (`commits._JOINED_CO_AUTHOR`). The "
            "change alters how a commit message is read, classifies again and copies no other "
            "file, so its reach is no repository",
            NO_REPOSITORY),
    Version("fa716d59cd93b23c8dfcaa85dbdefb79190b1d093f7d2acd86cc94c85084949f",
            "Leave a commit of Chromium below `Chromium change:` unread",
            "`commits._CHROMIUM_CHANGES` also reads `Chromium change:` in the singular, so that "
            "the commit of Chromium that brave/brave-core quotes below it credits no tool, and "
            "`heuristics.REMOVED_COMMIT_HEURISTIC_IDS` names the ids of the two rows of Grok CLI, "
            "which the rows of Grok Build replace. The change alters how a commit message is "
            "read, classifies again and copies no other file, so its reach is no repository",
            NO_REPOSITORY),
)

# The versions of the code of the imports, recorded with the import heuristics. The first is the
# fingerprint that the image of e99d5614 computed, with exception texts.
IMPORTS = (
    Version("6d25c76234a434bc326ac1c284c734b01a8cc4921175f8ecac039d37e050d995", "e99d5614",
            "The code of the imports of the run of all five stages that began on 2026-10-23",
            None),
    Version("d6b2102c2c3c1e30bfc219193c7adc7a96a2ff0d7f361666ef02e844ab8b4415", "1e99141d",
            "The fingerprint leaves out exception texts. 1e99141d reworded the problems that "
            "the loaders of the import heuristics report", NO_REPOSITORY),
    Version("7ebc8aafbcf207753cf5cd442f9ec1a8c4f146923b6e116c6b47a91735c63dfe",
            "List the skills that a root REVIEW.md includes for Claude Code Review",
            "A root REVIEW.md includes the SKILL.md of a folder directly in .claude/skills on "
            "its first eight skill lines outside code fences and HTML blocks, up to 30,000 "
            "bytes and through no link, and each name of an included path is compared in its "
            "case", review_md_at_root),
    Version("839b1c7c33894c64f4061e00f581460c613dbd002c22c6aab8f5f95ad6925ede",
            "Read the fences of Copilot CLI's imports as release 1.0.107 reads them",
            "`imports.copilot_at` closes a fence only at a line that starts with at least as many "
            "of its character and has only whitespace after them, opens no fence at a run of "
            "backticks with another backtick on its line, and takes Unicode's White_Space as "
            "whitespace, also where a path ends. Every import that the change takes away in the "
            "last collection names a file that the repository lacks, so that a run that takes "
            "over that collection copies the same files", NO_REPOSITORY),
    Version("8f1a95708590ecb9552cd8581ba0d3354ed5f1df196fbd7d1b1bdc80e2d87b29",
            "Start no import at the @ of a mail link or a mail address",
            "`imports.claude_at`, `imports.copilot_at` and `imports.gemini_at` start no import at "
            "an `@` in the text of a Markdown link to a `mailto:` URI, inline or by reference, in "
            "the label of the definition of such a reference, or in the mail address of a "
            "`mailto:` URI or an autolink (`imports._mail_ats`). Every import that the change "
            "takes away in the last collection names a file that the repository lacks, and the "
            "change adds none, so that a run that takes over that collection copies the same "
            "files", NO_REPOSITORY),
    Version("163973bfbee645dea48bd6a018e5a4bfe0c4eae0928bc412a46ae1f3e6b0c927",
            "Open the imports of a chain from a root file in another case as a file system "
            "that ignores case opens them",
            "A chain of `imports.ignores_case`, of Claude Code or Gemini CLI from a root "
            "claude.md, agents.md or gemini.md in another case, looks up each name of a target "
            "as written and otherwise as the one entry of its directory that matches it "
            "ignoring case, and records the target as the directory spells it "
            "(`imports.chain`). For such a chain, a root CLAUDE file in any case keeps Claude "
            "Code from reading a root agents.md (`imports._no_claude_md`). Every other chain "
            "finds a target only in its case, also on a file system that ignores case "
            "(`imports.status_of`), and on the volume that compares case, on which the cloning "
            "step runs in the container, it finds the files that it found before. The change "
            "reaches the repositories with a root file in another case",
            root_file_in_another_case),
)


def changes(versions: tuple, old: str, new: str) -> tuple | None:
    """The versions of `versions` with the changes between the fingerprints `old` and `new`, that
    is, every version after the earlier of the two up to and including the later one, or None
    where either fingerprint is no version of `versions`. Swapping `old` and `new` gives the same
    versions, as in a run of an earlier image over a later collection."""
    order = [version.fingerprint for version in versions]
    if old not in order or new not in order:
        return None
    first, last = sorted((order.index(old), order.index(new)))
    return versions[first + 1:last + 1]


def reached(span: tuple | None, file_list: Callable,
            path_commits: Callable = lambda: None, workflows: Callable = lambda: None) -> bool:
    """Whether a change of `span` (`changes`) reaches a repository. `file_list()` returns the
    repository's file list, or None where none was kept, and it is called only for a change with
    a predicate over the file list. `path_commits()` returns the commits of each copied path
    that the per-repository commit record keeps, or None where the record cannot show them,
    and it is called only for a change with a `RecordReach`. `workflows()` returns the text of
    each kept workflow by its file name, or None where a workflow file of the repository may be
    missing from the kept workflows, and it is called only for a change with a `WorkflowReach`.
    True where `span` is None, where a predicate needs a file list that was not kept, where a
    `RecordReach` needs a record that cannot show the commits of the paths and where a
    `WorkflowReach` needs workflows that were not all kept."""
    if span is None:
        return True
    entries = recorded = kept = ...
    for version in span:
        if version.reach == NO_REPOSITORY:
            continue
        if isinstance(version.reach, RecordReach):
            if recorded is ...:
                recorded = path_commits()
            if recorded is None or version.reach.test(recorded):
                return True
            continue
        if isinstance(version.reach, WorkflowReach):
            if kept is ...:
                kept = workflows()
            if kept is None or version.reach.test(kept):
                return True
            continue
        if version.reach == EVERY_REPOSITORY or not callable(version.reach):
            return True
        if entries is ...:
            entries = file_list()
        if entries is None or version.reach(entries):
            return True
    return False
