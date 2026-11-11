"""
Tests for agentcontext.steps.collected_file_cleanup: the validated tree and the files that
the step keeps.

Strategy:
  - Most validity helpers decide by the path alone. The tests call them with plain strings,
    without a directory tree.
  - is_empty_file reads from disk, and its tests write real files under a temporary
    directory.
  - The tests of main() point every path at a temporary directory, as TestMainCli in
    tests.test_steps_repository_cloning does.
  - The step copies only the five metadata columns from the collected list. The tests
    therefore assert both outcomes of the check on that list's header: the step refuses a
    list with the old header and validates a list with the current header.
  - The fixtures put each list at the root of the tree that it lists, as the pipeline does:
    data/collected/repos.csv and clone_failed.csv in the collected tree, and
    data/validated/repos.csv in the validated tree, which this step derives. A fixture with
    the lists outside the tree would not test what the copy does with them.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_steps_collected_file_cleanup -v
"""

import ast
import contextlib
import csv
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentcontext import STAGE_DIR
from agentcontext.heuristics import MECHANISM_CSVS
from tests import _logged
import subprocess
from tests._fixtures import (DEEP_JSON, LONG_INTEGER_JSON, deeply_nested_files,
                             dsm_removes_files_after_listing, dsm_writes_during_removal,
                             dsm_writes_into_new_directories, files_with_long_integers)
_mod = importlib.import_module("agentcontext.steps.collected_file_cleanup")
_detection = importlib.import_module("agentcontext.steps.detection")
VENDORED_DIRS = _mod.VENDORED_DIRS
all_false = _mod.all_false
clean_empty_dirs = _mod.clean_empty_dirs
compute_columns = _mod.compute_columns
find_claude_plugin_dirs = _mod.find_claude_plugin_dirs
find_marker_targets = _mod.find_marker_targets
find_marker_files = _mod.find_marker_files
get_path_parts = _mod.get_path_parts
has_copilot_content = _mod.has_copilot_content
has_github_context_files = _mod.has_github_context_files
is_claude_plugin_parent_kept_sibling = _mod.is_claude_plugin_parent_kept_sibling
is_empty_file = _mod.is_empty_file
is_inside_claude_plugin_dir = _mod.is_inside_claude_plugin_dir
is_inside_github_dir = _mod.is_inside_github_dir
is_inside_spec_dir = _mod.is_inside_spec_dir
is_inside_vendored_dir = _mod.is_inside_vendored_dir
is_relevant_github_file = _mod.is_relevant_github_file
is_valid_file = _mod.is_valid_file
main = _mod.main
METADATA_COLUMNS = _mod.METADATA_COLUMNS
LEGACY_TIMESTAMP_COLUMN = _mod.LEGACY_TIMESTAMP_COLUMN
scan_and_clean_repo = _mod.scan_and_clean_repo
should_keep_github = _mod.should_keep_github

# The header of the collected list, in the order that the cloning step writes it.
META = list(METADATA_COLUMNS)


# ---------------------------------------------------------------------------
# VENDORED_DIRS  (the vendored list, loaded from vendored_dirs.txt at import)
# ---------------------------------------------------------------------------

class TestVendoredDirs(unittest.TestCase):

    # Names left off the list because projects also use them for their own paths
    # (the section "The vendored list" of docs/heuristics.md).
    AMBIGUOUS = ("build", "dist", "out", "bin", "obj",
                 "env", "packages", "coverage", ".cache")

    def test_list_is_not_empty(self):
        self.assertTrue(VENDORED_DIRS)

    def test_known_names_are_loaded(self):
        for name in ("node_modules", "__pycache__", "vendor", "target", "third_party"):
            self.assertIn(name, VENDORED_DIRS)

    def test_comment_and_blank_lines_are_skipped(self):
        for entry in VENDORED_DIRS:
            self.assertTrue(entry)
            self.assertFalse(entry.startswith("#"), entry)

    def test_entries_are_bare_names(self):
        for entry in VENDORED_DIRS:
            self.assertEqual(entry, entry.strip(), entry)
            self.assertNotIn("/", entry)
            self.assertNotIn("*", entry)

    def test_ambiguous_names_are_not_blacklisted(self):
        for name in self.AMBIGUOUS:
            self.assertNotIn(name, VENDORED_DIRS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _RepoCase(unittest.TestCase):
    """A test case for the helpers that read a repository's tree, with a new repository for
    each test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "owner§repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, rel: str, content: str = "x\n") -> Path:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _dir(self, rel: str) -> Path:
        p = self.repo / rel
        p.mkdir(parents=True, exist_ok=True)
        return p


class _GithubDirCase(unittest.TestCase):
    """A test case for the .github check, with a new repository and its .github for each
    test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "owner§repo"
        self.github = self.repo / ".github"
        self.github.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, rel: str, content: str = "x\n") -> Path:
        p = self.github / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _dir(self, rel: str) -> Path:
        p = self.github / rel
        p.mkdir(parents=True, exist_ok=True)
        return p


# ---------------------------------------------------------------------------
# has_copilot_content  (a trigger directly in .github)
# ---------------------------------------------------------------------------

class TestHasCopilotContent(_GithubDirCase):

    def test_empty_github_has_no_trigger(self):
        self.assertFalse(has_copilot_content(str(self.github)))

    def test_copilot_instructions_file(self):
        self._file("copilot-instructions.md")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_instructions_dir(self):
        self._dir("instructions")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_agents_dir(self):
        self._dir("agents")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_hooks_dir(self):
        """Copilot reads the JSON files of `.github/hooks` at any depth. The cleanup deletes
        the git hook scripts that projects keep there, and the scripts alone are no
        trigger."""
        self._file("hooks/pre-commit")
        self.assertFalse(has_copilot_content(str(self.github)))
        self._file("hooks/team/hooks.json")
        self.assertTrue(has_copilot_content(str(self.github)))
        (self.github / "hooks" / "team" / "hooks.json").unlink()
        self._file("hooks/hooks.json")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_skills_dir(self):
        self._dir("skills")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_prompts_dir(self):
        self._dir("prompts")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_each_newer_heuristics_path_is_a_trigger_on_its_own(self):
        # The cleanup keeps a .github that contains only one of these paths.
        for rel in ("copilot/settings.json", "copilot/settings.local.json", "mcp.json",
                    "workflows/copilot-setup-steps.yml", "allowed_models.txt",
                    "workflows/copilot-code-review.yml", "lsp.json", "plugin/marketplace.json"):
            with self.subTest(rel=rel):
                shutil.rmtree(self.github)
                self.github.mkdir()
                self._file(rel)
                self.assertTrue(has_copilot_content(str(self.github)))
        shutil.rmtree(self.github)
        self.github.mkdir()
        self._dir("chatmodes")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_unrelated_content_is_not_a_trigger(self):
        self._file("workflows/ci.yml")
        self._file("PULL_REQUEST_TEMPLATE.md")
        self.assertFalse(has_copilot_content(str(self.github)))

    def test_trigger_nested_deeper_does_not_count(self):
        # The function reads only the direct children of .github.
        self._dir("foo/instructions")
        self.assertFalse(has_copilot_content(str(self.github)))

    def test_trigger_name_as_the_wrong_type_still_counts(self):
        # The function tests with os.path.exists rather than isdir or isfile, and a file
        # named "skills" counts too.
        self._file("skills")
        self.assertTrue(has_copilot_content(str(self.github)))

    def test_missing_github_dir_has_no_trigger(self):
        self.assertFalse(has_copilot_content(str(self.repo / "nope")))


# ---------------------------------------------------------------------------
# has_github_context_files  (a context file anywhere under .github)
# ---------------------------------------------------------------------------

class TestHasGithubContextFiles(_GithubDirCase):

    def test_empty_github_has_no_context_file(self):
        self.assertFalse(has_github_context_files(str(self.github)))

    def test_claude_md_at_github_root(self):
        self._file("CLAUDE.md")
        self.assertTrue(has_github_context_files(str(self.github)))

    def test_gemini_md_at_github_root(self):
        self._file("GEMINI.md")
        self.assertTrue(has_github_context_files(str(self.github)))

    def test_agents_md_nested_deep(self):
        self._file("a/b/AGENTS.md")
        self.assertTrue(has_github_context_files(str(self.github)))

    def test_wrong_case_is_not_a_context_file(self):
        # The comparison is case-sensitive even on a case-insensitive filesystem, because
        # the names come from os.walk.
        self._file("claude.md")
        self.assertFalse(has_github_context_files(str(self.github)))

    def test_every_heuristics_context_basename_keeps_github(self):
        # The list holds the basenames of the heuristics' context files. The cleanup keeps
        # an AGENTS.override.md or a CLAUDE.local.md inside .github, because a detector
        # searches the whole tree for exactly those names.
        for name in ("AGENTS.override.md", "CLAUDE.local.md", ".cursorrules"):
            with self.subTest(name):
                for stale in self.github.iterdir():
                    stale.unlink()
                self._file(name)
                self.assertTrue(has_github_context_files(str(self.github)))

    def test_skills_alone_is_not_a_context_file(self):
        # A skill keeps .github through has_copilot_content, not through this function.
        self._file("skills/SKILL.md")
        self.assertFalse(has_github_context_files(str(self.github)))

    def test_unrelated_files_are_not_context_files(self):
        self._file("workflows/ci.yml")
        self.assertFalse(has_github_context_files(str(self.github)))


# ---------------------------------------------------------------------------
# should_keep_github  (trigger or context file)
# ---------------------------------------------------------------------------

class TestShouldKeepGithub(_GithubDirCase):

    def test_neither_trigger_nor_context_file(self):
        self._file("workflows/ci.yml")
        self.assertFalse(should_keep_github(str(self.github)))

    def test_trigger_only(self):
        self._dir("instructions")
        self.assertTrue(should_keep_github(str(self.github)))

    def test_context_file_only(self):
        self._file("CLAUDE.md")
        self.assertTrue(should_keep_github(str(self.github)))

    def test_both(self):
        self._file("copilot-instructions.md")
        self._file("AGENTS.md")
        self.assertTrue(should_keep_github(str(self.github)))

    def test_empty_github_is_not_kept(self):
        self.assertFalse(should_keep_github(str(self.github)))


# ---------------------------------------------------------------------------
# find_claude_plugin_dirs  (parents of .claude-plugin directories)
# ---------------------------------------------------------------------------

class TestFindClaudePluginDirs(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "owner§repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_plugin_dir_returns_empty_set(self):
        (self.repo / ".claude").mkdir()
        self.assertEqual(find_claude_plugin_dirs(str(self.repo)), set())

    def test_plugin_dir_at_repo_root_returns_repo_root(self):
        (self.repo / ".claude-plugin").mkdir()
        self.assertEqual(find_claude_plugin_dirs(str(self.repo)), {str(self.repo)})

    def test_nested_plugin_dir_returns_its_parent(self):
        (self.repo / "plugins" / "mine" / ".claude-plugin").mkdir(parents=True)
        self.assertEqual(find_claude_plugin_dirs(str(self.repo)),
                         {str(self.repo / "plugins" / "mine")})

    def test_multiple_plugin_dirs_return_all_parents(self):
        (self.repo / "a" / ".claude-plugin").mkdir(parents=True)
        (self.repo / "b" / ".claude-plugin").mkdir(parents=True)
        self.assertEqual(find_claude_plugin_dirs(str(self.repo)),
                         {str(self.repo / "a"), str(self.repo / "b")})

    def test_file_named_like_a_plugin_dir_is_ignored(self):
        (self.repo / ".claude-plugin").write_text("not a dir\n")
        self.assertEqual(find_claude_plugin_dirs(str(self.repo)), set())

    def test_plugin_dir_inside_a_vendored_dir_is_still_returned(self):
        # The function does not skip vendored directories. is_valid_file rejects their files
        # earlier by the vendored list, and listing the parent changes nothing.
        (self.repo / "node_modules" / "pkg" / ".claude-plugin").mkdir(parents=True)
        self.assertEqual(find_claude_plugin_dirs(str(self.repo)),
                         {str(self.repo / "node_modules" / "pkg")})


# ---------------------------------------------------------------------------
# get_path_parts  (path components relative to the repo root)
# ---------------------------------------------------------------------------

class TestGetPathParts(unittest.TestCase):

    REPO = os.path.join(os.sep, "repo")

    def test_file_at_repo_root(self):
        self.assertEqual(
            get_path_parts(os.path.join(self.REPO, "CLAUDE.md"), self.REPO), ["CLAUDE.md"])

    def test_nested_file(self):
        self.assertEqual(
            get_path_parts(os.path.join(self.REPO, ".claude", "skills", "SKILL.md"), self.REPO),
            [".claude", "skills", "SKILL.md"])

    def test_repo_root_itself(self):
        # relpath returns ".", which matches no rule.
        self.assertEqual(get_path_parts(self.REPO, self.REPO), ["."])


# ---------------------------------------------------------------------------
# is_inside_spec_dir / is_inside_github_dir / is_inside_claude_plugin_dir
# ---------------------------------------------------------------------------

class TestIsInsideDirPredicates(unittest.TestCase):

    REPO = os.path.join(os.sep, "repo")

    def _p(self, *parts):
        return os.path.join(self.REPO, *parts)

    def test_spec_dir_at_root(self):
        self.assertTrue(is_inside_spec_dir(self._p(".claude", "settings.json"), self.REPO))

    def test_spec_dir_nested(self):
        self.assertTrue(is_inside_spec_dir(self._p("a", ".cursor", "rules", "x.mdc"), self.REPO))

    def test_all_four_spec_dirs(self):
        for name in (".claude", ".cursor", ".gemini", ".codex"):
            self.assertTrue(is_inside_spec_dir(self._p(name, "x"), self.REPO), name)

    def test_file_outside_spec_dirs(self):
        self.assertFalse(is_inside_spec_dir(self._p("docs", "guide.md"), self.REPO))

    def test_claude_plugin_is_not_a_spec_dir(self):
        # ".claude-plugin" has its own rule and does not match ".claude".
        self.assertFalse(is_inside_spec_dir(self._p(".claude-plugin", "plugin.json"), self.REPO))

    def test_spec_dir_match_is_case_sensitive(self):
        self.assertFalse(is_inside_spec_dir(self._p(".Claude", "settings.json"), self.REPO))

    def test_github_dir_at_root_and_nested(self):
        self.assertTrue(is_inside_github_dir(self._p(".github", "workflows", "ci.yml"), self.REPO))
        self.assertTrue(is_inside_github_dir(self._p("sub", ".github", "CLAUDE.md"), self.REPO))

    def test_file_outside_github_dir(self):
        self.assertFalse(is_inside_github_dir(self._p(".claude", "settings.json"), self.REPO))

    def test_plugin_dir_at_root_and_nested(self):
        self.assertTrue(
            is_inside_claude_plugin_dir(self._p(".claude-plugin", "plugin.json"), self.REPO))
        self.assertTrue(
            is_inside_claude_plugin_dir(self._p("a", ".claude-plugin", "plugin.json"), self.REPO))

    def test_file_outside_plugin_dir(self):
        self.assertFalse(is_inside_claude_plugin_dir(self._p(".claude", "x.json"), self.REPO))


# ---------------------------------------------------------------------------
# is_claude_plugin_parent_kept_sibling  (immediate siblings only)
# ---------------------------------------------------------------------------

class TestIsClaudePluginParentKeptSibling(unittest.TestCase):

    PARENT = os.path.join(os.sep, "repo", "plugins", "mine")

    def setUp(self):
        self.parents = {self.PARENT}

    def _sibling(self, *parts):
        return is_claude_plugin_parent_kept_sibling(
            os.path.join(self.PARENT, *parts), self.parents)

    def test_readme_is_kept(self):
        self.assertTrue(self._sibling("README.md"))

    def test_readme_case_variants_are_kept(self):
        for name in ("README.md", "readme.md"):
            self.assertTrue(self._sibling(name), name)

    def test_a_readme_case_that_no_collection_contains_is_not_kept(self):
        # The heuristics list the README spellings found beside a manifest in the
        # collections, and Readme.md is not one of them.
        self.assertFalse(self._sibling("Readme.md"))

    def test_unlisted_readme_case_is_not_kept(self):
        self.assertFalse(self._sibling("README.MD"))

    def test_license_variants_are_kept(self):
        for name in ("LICENSE", "LICENSE.md", "LICENSE.txt"):
            self.assertTrue(self._sibling(name), name)

    def test_package_json_is_kept(self):
        self.assertTrue(self._sibling("package.json"))

    def test_standalone_spec_file_is_kept(self):
        self.assertTrue(self._sibling("CLAUDE.md"))
        self.assertTrue(self._sibling(".cursorrules"))

    def test_unrelated_sibling_is_not_kept(self):
        self.assertFalse(self._sibling("index.js"))

    def test_deeper_descendant_is_not_kept(self):
        # This check must not keep a vendored subtree below the plugin's parent, such as the
        # subtree of utooland/utoo.
        self.assertFalse(self._sibling("src", "README.md"))

    def test_file_outside_any_plugin_parent_is_not_kept(self):
        self.assertFalse(is_claude_plugin_parent_kept_sibling(
            os.path.join(os.sep, "repo", "other", "README.md"), self.parents))

    def test_no_plugin_parents_keeps_nothing(self):
        self.assertFalse(is_claude_plugin_parent_kept_sibling(
            os.path.join(self.PARENT, "README.md"), set()))


# ---------------------------------------------------------------------------
# The portable Agent Plugins documents  (kept by content, not by name)
# ---------------------------------------------------------------------------

class TestOpenCodeRootFiles(unittest.TestCase):
    """The cleanup keeps OpenCode's configuration and the settings of its terminal interface
    at the repository root, where the heuristics' rows read them. It keeps OpenCode's
    directory at any depth."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)

    def _valid(self, rel):
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{}\n")
        return is_valid_file(path, self.repo, frozenset(), False)

    def test_the_root_files_are_kept(self):
        for rel in ("opencode.json", "opencode.jsonc", "tui.json", "tui.jsonc"):
            with self.subTest(rel=rel):
                self.assertTrue(self._valid(rel))

    def test_the_same_names_below_the_root_are_not(self):
        for rel in ("src/lang/en/opencode.json", "docs/opencode.jsonc", "metadata/tui.json"):
            with self.subTest(rel=rel):
                self.assertFalse(self._valid(rel))

    def test_the_directory_is_kept_below_the_root_too(self):
        self.assertTrue(self._valid("packages/web/.opencode/opencode.json"))

    def test_the_tool_column_is_set_by_the_directory_or_a_root_file(self):
        self.assertFalse(compute_columns(self.repo)["opencode"])
        self._valid("tui.json")
        self.assertTrue(compute_columns(self.repo)["opencode"])


class TestThePortableAgentPluginsDocuments(unittest.TestCase):
    """The cleanup keeps `plugin.json` and `mcp.json` only where the document declares the
    standard.

    The tests write the files to disk rather than pass their paths alone, because
    is_valid_file reads them.
    """

    MANIFEST = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",'
                ' "name": "demo"}')
    MCP_DOCUMENT = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
                    ' "mcpServers": {"demo": {"command": "x"}}}')

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)

    def _valid(self, rel, text):
        path = os.path.join(self.repo, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return is_valid_file(path, self.repo, frozenset(), False)

    def test_a_manifest_declaring_the_standard_is_kept(self):
        self.assertTrue(self._valid("plugins/demo/plugin.json", self.MANIFEST))

    def test_an_mcp_document_declaring_the_standard_is_kept(self):
        self.assertTrue(self._valid("plugins/demo/mcp.json", self.MCP_DOCUMENT))

    def test_another_program_s_plugin_json_is_deleted(self):
        self.assertFalse(self._valid("src/plugin.json", '{"id": "acme", "type": "panel"}'))

    def test_a_manifest_naming_another_schema_is_deleted(self):
        self.assertFalse(self._valid(
            "plugin.json", '{"$schema": "https://grafana.com/plugin.schema.json", "name": "x"}'))

    def test_a_document_missing_the_required_field_is_deleted(self):
        self.assertFalse(self._valid(
            "plugin.json",
            '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"}'))

    def test_malformed_json_is_deleted(self):
        self.assertFalse(self._valid("plugin.json", "{not json"))

    def test_a_manifest_inside_a_vendored_tree_is_deleted(self):
        self.assertFalse(self._valid("node_modules/pkg/plugin.json", self.MANIFEST))


# ---------------------------------------------------------------------------
# is_valid_file  (whether to keep or delete, with the rule order as the specification)
# ---------------------------------------------------------------------------

class TestIsValidFile(unittest.TestCase):

    REPO = os.path.join(os.sep, "repo")
    PLUGIN_PARENT = os.path.join(os.sep, "repo", "plugins", "mine")

    def _valid(self, parts, plugin_parents=frozenset(), keep_github=False):
        return is_valid_file(
            os.path.join(self.REPO, *parts), self.REPO, plugin_parents, keep_github)

    # ── .DS_Store: rejected everywhere, before every other rule ─────────────

    def test_ds_store_at_root(self):
        self.assertFalse(self._valid([".DS_Store"]))

    def test_ds_store_inside_a_spec_dir(self):
        # The .DS_Store rule comes before the spec directory rule, and the cleanup therefore
        # deletes a .DS_Store inside .claude too.
        self.assertFalse(self._valid([".claude", ".DS_Store"]))

    # ── vendored: rejected even under an otherwise-kept ancestor ─────────────

    def test_vendored_dir_at_root(self):
        self.assertFalse(self._valid(["node_modules", "pkg", "CLAUDE.md"]))

    def test_vendored_dir_inside_a_spec_dir(self):
        self.assertFalse(self._valid([".claude", "node_modules", "x.md"]))

    def test_vendored_dir_inside_a_claude_plugin_dir(self):
        # Because the vendored rule comes before the plugin directory rule, the cleanup
        # deletes a vendored subtree inside the plugin directory too.
        self.assertFalse(self._valid(
            [".claude-plugin", "node_modules", "x.js"]))

    # ── .claude-plugin contents: always valid ────────────────────────────────

    def test_inside_claude_plugin_dir_is_always_valid(self):
        self.assertTrue(self._valid([".claude-plugin", "plugin.json"]))

    def test_claude_plugin_content_need_not_be_a_spec_file(self):
        self.assertTrue(self._valid([".claude-plugin", "anything.txt"]))

    # ── .claude-plugin parent siblings: only the allowed list, only direct ──

    def test_plugin_parent_allowed_sibling_is_valid(self):
        parents = {self.PLUGIN_PARENT}
        self.assertTrue(self._valid(["plugins", "mine", "README.md"], plugin_parents=parents))

    def test_plugin_parent_unrelated_sibling_is_invalid(self):
        parents = {self.PLUGIN_PARENT}
        self.assertFalse(self._valid(["plugins", "mine", "index.js"], plugin_parents=parents))

    def test_plugin_parent_deeper_descendant_is_invalid(self):
        parents = {self.PLUGIN_PARENT}
        self.assertFalse(
            self._valid(["plugins", "mine", "src", "README.md"], plugin_parents=parents))

    # ── spec directories: everything inside is valid ─────────────────────────

    def test_spec_dir_content_need_not_be_a_spec_file(self):
        self.assertTrue(self._valid([".claude", "anything.txt"]))

    def test_spec_dir_content_at_depth(self):
        self.assertTrue(self._valid([".cursor", "rules", "a", "b.mdc"]))

    # ── .github: kept under keep_github, then by relevance ─────────────────────

    def test_github_not_kept_rejects_every_path(self):
        self.assertFalse(self._valid([".github", "CLAUDE.md"], keep_github=False))

    def test_github_kept_accepts_relevant_path(self):
        self.assertTrue(self._valid([".github", "copilot-instructions.md"], keep_github=True))

    def test_github_kept_rejects_irrelevant_path(self):
        self.assertFalse(self._valid([".github", "workflows", "ci.yml"], keep_github=True))

    # ── SKILL.md outside every valid parent: rejected ────────────────────────

    def test_skill_md_outside_spec_dirs_is_invalid(self):
        self.assertFalse(self._valid(["skills", "SKILL.md"]))

    def test_skill_md_inside_spec_dir_is_valid(self):
        # The spec directory rule comes before the rule that rejects a SKILL.md.
        self.assertTrue(self._valid([".claude", "skills", "pdf", "SKILL.md"]))

    def test_skill_md_inside_kept_github_is_valid(self):
        self.assertTrue(self._valid([".github", "skills", "pdf", "SKILL.md"], keep_github=True))

    # ── root-only files: valid at the root, invalid at any depth ─────────────

    def test_mcp_json_at_root_is_valid(self):
        self.assertTrue(self._valid([".mcp.json"]))

    def test_mcp_json_nested_is_invalid(self):
        self.assertFalse(self._valid(["sub", ".mcp.json"]))

    # ── standalone spec files: valid at any depth ─────────────────────────────

    def test_standalone_spec_file_at_root(self):
        self.assertTrue(self._valid(["CLAUDE.md"]))

    def test_standalone_spec_file_at_depth(self):
        self.assertTrue(self._valid(["a", "b", "AGENTS.md"]))

    def test_standalone_spec_file_inside_vendored_dir_is_invalid(self):
        self.assertFalse(self._valid(["vendor", "CLAUDE.md"]))

    # ── everything else: rejected ─────────────────────────────────────────────

    def test_unrelated_file_at_root_is_invalid(self):
        self.assertFalse(self._valid(["README.md"]))

    def test_wrong_case_context_file_is_invalid_below_the_root(self):
        self.assertFalse(self._valid(["docs", "claude.md"]))

    def test_wrong_case_context_file_is_valid_at_the_root(self):
        self.assertTrue(self._valid(["claude.md"]))
        self.assertTrue(self._valid(["Agents.md"]))
        self.assertFalse(self._valid(["plans.md"]))


# ---------------------------------------------------------------------------
# is_inside_vendored_dir  (by the path's components, except for a skill folder on disk)
# ---------------------------------------------------------------------------

class TestIsInsideVendoredDir(unittest.TestCase):

    REPO = os.path.join(os.sep, "repo")

    def _path(self, *parts):
        return os.path.join(self.REPO, *parts)

    def test_file_at_repo_root_is_not_vendored(self):
        self.assertFalse(is_inside_vendored_dir(self._path("CLAUDE.md"), self.REPO))

    def test_top_level_vendored_dir(self):
        self.assertTrue(
            is_inside_vendored_dir(self._path("node_modules", "pkg", "CLAUDE.md"), self.REPO))

    def test_vendored_dir_at_any_depth(self):
        self.assertTrue(
            is_inside_vendored_dir(self._path("a", "b", "vendor", "CLAUDE.md"), self.REPO))

    def test_vendored_dir_under_a_spec_dir(self):
        # The rules have their order for paths such as this one, because the cleanup has to
        # delete a vendored subtree even below a kept ancestor.
        self.assertTrue(
            is_inside_vendored_dir(self._path(".claude", "node_modules", "x.md"), self.REPO))

    def test_vendored_name_as_the_file_itself(self):
        # The function judges a component that is not a directory on disk by its name alone.
        self.assertTrue(is_inside_vendored_dir(self._path(".claude", "vendor"), self.REPO))

    def test_prefix_sharing_name_is_not_vendored(self):
        self.assertFalse(
            is_inside_vendored_dir(self._path("node_modules_backup", "CLAUDE.md"), self.REPO))

    def test_match_is_case_sensitive(self):
        self.assertFalse(
            is_inside_vendored_dir(self._path("NODE_MODULES", "CLAUDE.md"), self.REPO))

    def test_repo_dir_named_like_a_vendored_dir(self):
        # The function takes the components relative to the repository root. The cleanup
        # does not delete a repository that is itself checked out into vendor/ as a whole.
        repo = os.path.join(os.sep, "x", "vendor")
        self.assertFalse(is_inside_vendored_dir(os.path.join(repo, "CLAUDE.md"), repo))

    def test_multiple_vendored_components(self):
        self.assertTrue(
            is_inside_vendored_dir(self._path("vendor", "node_modules", "x.md"), self.REPO))

    def test_a_skill_folder_named_like_a_vendored_directory_is_not_one(self):
        """A folder that contains a SKILL.md is a skill whatever its name, such as unoplatform/uno's
        `.claude/skills/docs-build`. A folder of skills, such as open-metadata/openmetadata's
        `skills/vendor`, and a vendored tree inside a skill remain vendored."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        for rel in (".claude/skills/docs-build/SKILL.md", ".claude/skills/docs-build/node_modules/x.js",
                    "skills/vendor/react/SKILL.md"):
            (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
            (tmp / rel).write_text("x\n")
        self.assertFalse(is_inside_vendored_dir(str(tmp / ".claude/skills/docs-build/SKILL.md"), str(tmp)))
        self.assertTrue(is_inside_vendored_dir(
            str(tmp / ".claude/skills/docs-build/node_modules/x.js"), str(tmp)))
        self.assertTrue(is_inside_vendored_dir(str(tmp / "skills/vendor/react/SKILL.md"), str(tmp)))

    def test_every_blacklisted_name_is_matched(self):
        for name in VENDORED_DIRS:
            self.assertTrue(
                is_inside_vendored_dir(self._path(name, "CLAUDE.md"), self.REPO), name)

    def test_no_vendored_name_is_a_segment_of_a_configuration_path(self):
        """The cleanup deletes a directory with a vendored name at any depth, before any rule
        that keeps a file. The test above loops over the set that the predicate reads. With
        `commands` added to the list, that test would pass while the cleanup deleted every
        command file of Claude Code, Cursor and Gemini. This test reads both files, because
        it checks whether they agree."""
        heuristics_dir = Path(__file__).resolve().parents[1] / "heuristics"
        with open(heuristics_dir / "vendored_dirs.txt", encoding="utf-8") as f:
            vendored = {line.strip() for line in f
                        if line.strip() and not line.startswith("#")}
        self.assertIn("node_modules", vendored, "the list was not read")
        with open(heuristics_dir / "config_path_patterns.csv", newline="",
                  encoding="utf-8") as f:
            rows = [r for r in csv.DictReader(f) if not r["tool"].startswith("#")]
        self.assertIn(".claude/commands", {r["path"] for r in rows}, "the rows were not read")
        for row in rows:
            with self.subTest(path=row["path"]):
                self.assertFalse(set(row["path"].split("/")) & vendored)


# ---------------------------------------------------------------------------
# is_relevant_github_file  (which paths under a kept .github remain)
# ---------------------------------------------------------------------------

class TestIsRelevantGithubFile(unittest.TestCase):

    REPO = os.path.join(os.sep, "repo")

    def _relevant(self, *parts):
        return is_relevant_github_file(os.path.join(self.REPO, *parts), self.REPO)

    def test_copilot_instructions_at_github_root(self):
        self.assertTrue(self._relevant(".github", "copilot-instructions.md"))

    def test_copilot_instructions_match_is_case_insensitive(self):
        self.assertTrue(self._relevant(".github", "COPILOT-INSTRUCTIONS.md"))

    def test_instructions_subdir(self):
        self.assertTrue(self._relevant(".github", "instructions", "python.md"))

    def test_agents_subdir(self):
        self.assertTrue(self._relevant(".github", "agents", "reviewer.md"))

    def test_hooks_subdir(self):
        self.assertTrue(self._relevant(".github", "hooks", "hooks.json"))

    def test_skills_subdir(self):
        self.assertTrue(self._relevant(".github", "skills", "pdf", "SKILL.md"))

    def test_prompts_subdir(self):
        self.assertTrue(self._relevant(".github", "prompts", "lint-fix.prompt.md"))

    def test_context_file_at_github_root(self):
        self.assertTrue(self._relevant(".github", "CLAUDE.md"))

    def test_context_file_nested_anywhere(self):
        self.assertTrue(self._relevant(".github", "ISSUE_TEMPLATE", "AGENTS.md"))

    def test_workflows_are_irrelevant(self):
        self.assertFalse(self._relevant(".github", "workflows", "ci.yml"))

    def test_issue_template_without_context_file_is_irrelevant(self):
        self.assertFalse(self._relevant(".github", "ISSUE_TEMPLATE", "bug.md"))

    def test_wrong_case_context_file_is_irrelevant(self):
        self.assertFalse(self._relevant(".github", "claude.md"))

    def test_subdir_name_match_is_case_insensitive(self):
        # has_copilot_content decides whether .github is kept at all and compares these
        # names case-insensitively. This function compares them the same way. Otherwise a
        # repository kept for its .github/Instructions would lose every file inside it.
        self.assertTrue(self._relevant(".github", "Instructions", "python.md"))

    def test_an_unrelated_subdir_is_still_irrelevant(self):
        self.assertFalse(self._relevant(".github", "ISSUE_TEMPLATE", "bug.md"))

    def test_github_dir_itself_is_irrelevant(self):
        self.assertFalse(self._relevant(".github"))

    def test_nested_github_only_keeps_context_files(self):
        # The function inspects the component after the repository root, not the component
        # after the nested .github, and it does not recognize the Copilot paths of a nested
        # .github.
        self.assertFalse(self._relevant("sub", ".github", "copilot-instructions.md"))
        self.assertTrue(self._relevant("sub", ".github", "CLAUDE.md"))


# ---------------------------------------------------------------------------
# clean_empty_dirs  (bottom-up pruning, never the repo root)
# ---------------------------------------------------------------------------

class TestCleanEmptyDirs(_RepoCase):

    def test_no_empty_dirs_removes_nothing(self):
        self._file(".claude/settings.json")
        self.assertEqual(clean_empty_dirs(str(self.repo)), [])
        self.assertTrue((self.repo / ".claude").is_dir())

    def test_single_empty_dir_removed(self):
        self._dir(".claude")
        self.assertEqual(clean_empty_dirs(str(self.repo)), [str(self.repo / ".claude")])
        self.assertFalse((self.repo / ".claude").exists())

    def test_dir_containing_a_file_is_kept(self):
        self._file(".claude/commands/build.md")
        clean_empty_dirs(str(self.repo))
        self.assertTrue((self.repo / ".claude" / "commands").is_dir())

    def test_nested_empty_dirs_removed_bottom_up(self):
        self._dir("a/b/c")
        removed = clean_empty_dirs(str(self.repo))
        self.assertEqual(len(removed), 3)
        self.assertFalse((self.repo / "a").exists())

    def test_parent_emptied_by_the_pass_is_also_removed(self):
        # b is empty only after c is removed, and the bottom-up pass checks it again.
        self._dir("a/b/c")
        self._file("a/keep.md")
        clean_empty_dirs(str(self.repo))
        self.assertTrue((self.repo / "a").is_dir())
        self.assertFalse((self.repo / "a" / "b").exists())

    def test_repo_root_is_never_removed(self):
        self.assertEqual(clean_empty_dirs(str(self.repo)), [])
        self.assertTrue(self.repo.is_dir())

    def test_dir_containing_only_a_symlink_is_kept(self):
        target = self._file("CLAUDE.md")
        link_dir = self._dir("links")
        (link_dir / "CLAUDE.md").symlink_to(target)
        clean_empty_dirs(str(self.repo))
        self.assertTrue(link_dir.is_dir())

    def test_an_eadir_that_dsm_writes_after_the_listing_is_removed_with_the_directory(self):
        """DSM writes an `@eaDir` at any time. One can appear between the listing that finds
        a directory empty and the removal, and the removal must not then fail with
        ENOTEMPTY."""
        empty = self._dir(".claude/commands")
        real_listdir = os.listdir

        def dsm_writes_after_the_listing(path="."):
            names = real_listdir(path)
            if Path(path) == empty and not names:
                (empty / "@eaDir").mkdir()
                (empty / "@eaDir" / "x@SynoEAStream").write_text("x\n")
            return names

        with mock.patch("os.listdir", dsm_writes_after_the_listing):
            removed = clean_empty_dirs(str(self.repo))
        self.assertIn(str(empty), removed)
        self.assertFalse((self.repo / ".claude").exists())


# ---------------------------------------------------------------------------
# is_empty_file  (whitespace-only content, or JSON parsing to {} / [])
# ---------------------------------------------------------------------------

class TestIsEmptyFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str) -> str:
        p = self.tmp / name
        p.write_text(content, encoding="utf-8")
        return str(p)

    def test_zero_byte_file_is_empty(self):
        self.assertTrue(is_empty_file(self._write("CLAUDE.md", "")))

    def test_whitespace_only_file_is_empty(self):
        self.assertTrue(is_empty_file(self._write("CLAUDE.md", "  \n\t\n  ")))

    def test_file_with_content_is_not_empty(self):
        self.assertFalse(is_empty_file(self._write("CLAUDE.md", "# Claude\n")))

    def test_json_empty_object_is_empty(self):
        self.assertTrue(is_empty_file(self._write("settings.json", "{}")))

    def test_json_empty_array_is_empty(self):
        self.assertTrue(is_empty_file(self._write("settings.json", "[]")))

    def test_json_empty_object_with_surrounding_whitespace_is_empty(self):
        self.assertTrue(is_empty_file(self._write("settings.json", "\n  {}  \n")))

    def test_json_with_keys_is_not_empty(self):
        self.assertFalse(is_empty_file(self._write("settings.json", '{"a": 1}')))

    def test_malformed_json_is_not_empty(self):
        self.assertFalse(is_empty_file(self._write("settings.json", "{not json")))

    def test_json_extension_match_is_case_insensitive(self):
        self.assertTrue(is_empty_file(self._write("settings.JSON", "{}")))

    def test_empty_braces_outside_json_are_not_empty(self):
        # The {} / [] rule applies only to .json. A Markdown file that contains "{}"
        # still has content.
        self.assertFalse(is_empty_file(self._write("CLAUDE.md", "{}")))

    def test_undecodable_bytes_are_not_empty(self):
        p = self.tmp / "settings.json"
        p.write_bytes(b"\xff\xfe\x00\x01")
        self.assertFalse(is_empty_file(str(p)))

    def test_missing_path_is_not_empty(self):
        self.assertFalse(is_empty_file(str(self.tmp / "nope.md")))

    def test_directory_path_is_not_empty(self):
        (self.tmp / ".claude").mkdir()
        self.assertFalse(is_empty_file(str(self.tmp / ".claude")))


# ---------------------------------------------------------------------------
# scan_and_clean_repo  (one repo end to end: deletions and their reasons)
# ---------------------------------------------------------------------------

class TestScanAndCleanRepo(_RepoCase):

    def _clean(self) -> dict:
        """Clean the repository and return {basename: reason} for every deletion."""
        deletions = []
        scan_and_clean_repo(str(self.repo), deletions)
        return {os.path.basename(p): reason for p, reason in deletions}

    def test_valid_files_survive(self):
        self._file("CLAUDE.md", "# Claude\n")
        self._file(".claude/settings.json", '{"a": 1}')
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / "CLAUDE.md").exists())
        self.assertTrue((self.repo / ".claude" / "settings.json").exists())

    def test_an_agent_directory_is_kept_whole_only_with_a_place_of_antigravity(self):
        """Antigravity reads `rules/`, `skills/`, `agents/`, `plugins/`, `hooks.json` and
        `mcp_config.json` in a customization folder such as `_agent/`
        (https://antigravity.google/docs/customization-discovery, read 2026-11-05).
        bitbybit-dev/bitbybit's `templates/_agent/` holds the templates of a project generator
        and none of these places. Its files are deleted, except a context file, which counts
        at any depth."""
        templates = "packages/dev/create-app/templates/_agent"
        self._file("CLAUDE.md", "# Claude\n")
        self._file(f"{templates}/_dot-mcp.json", '{"mcpServers": {"bitbybit": {"url": "u"}}}\n')
        self._file(f"{templates}/_dot-gitignore", "node_modules\n")
        self._file(f"{templates}/AGENTS.md", "# Agents\n")
        self._file("pkg/_agent/mcp_config.json", '{"mcpServers": {"s": {"command": "x"}}}\n')
        self._file("pkg/_agent/notes.md", "Notes.\n")
        deleted = self._clean()
        self.assertEqual(set(deleted), {"_dot-mcp.json", "_dot-gitignore"})
        self.assertTrue((self.repo / templates / "AGENTS.md").is_file())
        self.assertTrue((self.repo / "pkg/_agent/notes.md").is_file())

    def test_an_empty_anchor_keeps_the_planning_documents_that_need_it(self):
        """Backlog.md's tasks count only beside its `backlog/config.yml`, and that file can
        hold nothing but whitespace. The step keeps the file although it is empty. Without
        the file, the tasks that the cloning step copied with it would no longer count."""
        self._file("CLAUDE.md", "# Claude\n")
        self._file("backlog/config.yml", "\n")
        self._file("backlog/tasks/task-1 - Login.md", "# Login\n")
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / "backlog" / "config.yml").exists())
        self.assertTrue((self.repo / "backlog" / "tasks" / "task-1 - Login.md").exists())

    def test_the_empty_files_that_kept_when_empty_names_are_the_ones_kept(self):
        """The analysis of the content quality counts a listed empty file with 0 lines when
        `kept_when_empty` names it in the validated tree, and it stops otherwise. The function
        therefore names the empty files that this step keeps: an import of a GitHub Agentic
        Workflow, the prompt file of the Codex action, and cursor-memory-bank's `/van` command,
        which anchors its tasks. The step deletes an empty context file that the function
        does not name."""
        self._file(".github/workflows/triage.md",
                   "---\non: issues\nimports:\n  - shared/engine.md\n  - shared/empty.md\n---\n"
                   "Triage.\n")
        self._file(".github/workflows/shared/engine.md", "---\nengine:\n  id: claude\n---\n")
        self._file(".github/workflows/shared/empty.md", "\n")
        self._file(".github/workflows/review.yml",
                   "jobs:\n  codex:\n    steps:\n      - uses: openai/codex-action@v1\n"
                   "        with:\n          prompt-file: .github/codex/prompts/review.md\n")
        self._file(".github/codex/prompts/review.md", "")
        self._file(".cursor/commands/van.md", "")
        self._file("memory-bank/tasks.md", "# Tasks\n")
        self._file("docs/CLAUDE.md", " \n")
        kept = _mod.kept_when_empty(self.repo)
        self.assertEqual(kept, {".github/workflows/shared/engine.md",
                                ".github/workflows/shared/empty.md",
                                ".github/codex/prompts/review.md",
                                ".cursor/commands/van.md", "memory-bank/tasks.md"})
        self.assertEqual(self._clean(), {"CLAUDE.md": "empty file",
                                         "docs": "empty directory after cleanup"})
        for rel in kept:
            with self.subTest(path=rel):
                self.assertTrue((self.repo / rel).is_file())

    def test_a_context_file_the_configuration_names_is_kept(self):
        """The collector copies what a Gemini settings file or an extension manifest names,
        and this step keeps it. That holds for a root CONTEXT.md, although it is no spec
        file, and for the lowercase gemini.md that google/clasp's extension manifest names.
        That gemini.md is no misspelled GEMINI.md. The step still deletes a file of the same name
        that the configuration does not name."""
        self._file(".gemini/settings.json", '{"context": {"fileName": ["CONTEXT.md"]}}\n')
        self._file("CONTEXT.md", "# context\n")
        self._file("gemini-extension.json", '{"name": "clasp", "contextFileName": "gemini.md"}\n')
        self._file("gemini.md", "# clasp\n")
        self._file("docs/gemini.md", "# not named\n")
        self.assertEqual(self._clean(),
                         {"gemini.md": "wrong letter case of gemini.md (instead of GEMINI.md)",
                          "docs": "empty directory after cleanup"})
        self.assertTrue((self.repo / "CONTEXT.md").exists())
        self.assertTrue((self.repo / "gemini.md").exists())
        self.assertFalse((self.repo / "docs" / "gemini.md").exists())

    def test_the_instructions_that_a_codex_role_names_are_kept(self):
        """The collector copies the file that the `model_instructions_file` of a Codex agent role
        names, the base instructions of the subagent, and this step keeps it outside `.codex`
        too. The step still deletes a file beside it that no role names."""
        self._file(".codex/agents/planner.toml", 'model_instructions_file = "../../docs/planner.md"\n')
        self._file("docs/planner.md", "Plan the change.\n")
        self._file("docs/other.md", "# Other\n")
        self._clean()
        self.assertTrue((self.repo / "docs" / "planner.md").is_file())
        self.assertFalse((self.repo / "docs" / "other.md").exists())

    def test_the_prompt_files_that_an_opencode_agent_names_are_kept_also_when_empty(self):
        """The collector copies the file that a `{file:...}` reference in the prompt of an
        OpenCode agent names, and this step keeps it outside `.opencode` too, as the prompt file
        of an agent action. OpenCode replaces the reference with the file's text, an empty file
        included. The step still deletes a file beside it that no agent names."""
        self._file("opencode.json", json.dumps({"agent": {
            "docs": {"prompt": "{file:./prompts/docs.md}"},
            "draft": {"prompt": "{file:.opencode/prompts/draft.md}"}}}))
        self._file("prompts/docs.md", "Write the docs.\n")
        self._file("prompts/other.md", "# Other\n")
        self._file(".opencode/prompts/draft.md", "")
        self._clean()
        self.assertTrue((self.repo / "prompts" / "docs.md").is_file())
        self.assertTrue((self.repo / ".opencode" / "prompts" / "draft.md").is_file())
        self.assertFalse((self.repo / "prompts" / "other.md").exists())

    def test_a_rule_file_a_rules_json_names_is_kept(self):
        """A file that .agents/rules.json registers outside .agents/rules is a rule file that
        Antigravity reads, although it is in no spec directory. The step still deletes a file
        that the rules.json excludes."""
        self._file(".agents/rules.json", '{"entries": [{"path": "shared-rules", '
                                         '"exclude": ["old.md"]}]}')
        self._file("shared-rules/api.md", "---\ntrigger: always_on\n---\n# api\n")
        self._file("shared-rules/old.md", "---\ntrigger: always_on\n---\n# old\n")
        self._clean()
        self.assertTrue((self.repo / "shared-rules" / "api.md").exists())
        self.assertFalse((self.repo / "shared-rules" / "old.md").exists())

    def test_a_registered_file_without_the_front_matter_of_its_mechanism_is_deleted(self):
        """Antigravity CLI 1.4.2 listed only the registered agents with a name and a
        description, and it counted only the registered rules with a trigger. A README.md that
        `.` or a registered directory reaches is therefore neither an agent nor a rule. The
        step does not keep it for being registered."""
        self._file(".agents/agents.json", '{"entries": [{"path": "."}]}')
        self._file(".agents/rules.json", '{"entries": [{"path": "docs"}]}')
        self._file("README.md", "# Project\n")
        self._file("agent.md", "---\nname: a\ndescription: d\n---\nbody\n")
        self._file("docs/README.md", "# Docs\n")
        self._file("docs/style.md", "---\ntrigger: glob\n---\nStyle.\n")
        self._clean()
        self.assertFalse((self.repo / "README.md").exists())
        self.assertFalse((self.repo / "docs" / "README.md").exists())
        self.assertTrue((self.repo / "agent.md").exists())
        self.assertTrue((self.repo / "docs" / "style.md").exists())

    def test_what_a_plugin_manifest_names_is_kept(self):
        """The step keeps a skill folder and a hooks file that a Claude Code manifest names
        outside the default locations, such as ecomfe/tempad-dev's clients/claude/hooks.json.
        It still deletes a file beside them that the manifest does not name."""
        self._file("plug/.claude-plugin/plugin.json",
                   '{"name": "p", "skills": ["./lib/skills/"], "hooks": "./cfg/hooks.json"}')
        self._file("plug/lib/skills/pool/SKILL.md", "# pool\n")
        self._file("plug/cfg/hooks.json", '{"hooks": {"Stop": []}}')
        self._file("plug/cfg/other.json", '{"x": 1}')
        self._clean()
        self.assertTrue((self.repo / "plug" / "lib" / "skills" / "pool" / "SKILL.md").exists())
        self.assertTrue((self.repo / "plug" / "cfg" / "hooks.json").exists())
        self.assertFalse((self.repo / "plug" / "cfg" / "other.json").exists())

    def test_copilot_s_root_marketplace_is_kept(self):
        """Copilot CLI looks for a marketplace.json at the root of the repository first."""
        self._file("marketplace.json", '{"name": "m", "plugins": []}')
        self._clean()
        self.assertTrue((self.repo / "marketplace.json").exists())

    def test_empty_file_is_removed_first(self):
        # Only the pass over empty files can remove an empty CLAUDE.md, because its location
        # is valid.
        self._file("CLAUDE.md", "   \n")
        self.assertEqual(self._clean()["CLAUDE.md"], "empty file")
        self.assertFalse((self.repo / "CLAUDE.md").exists())

    def test_empty_and_invalid_file_is_reported_as_empty(self):
        # The step records the reason of the pass over empty files, because that pass runs
        # before the validity pass.
        self._file("notes.txt", "")
        self.assertEqual(self._clean()["notes.txt"], "empty file")

    def test_ds_store_reason(self):
        self._file(".DS_Store", "junk")
        self.assertEqual(self._clean()[".DS_Store"], "macOS Finder file")

    def test_vendored_reason_names_the_directory(self):
        self._file("node_modules/pkg/CLAUDE.md", "# x\n")
        self.assertEqual(self._clean()["CLAUDE.md"],
                         "inside vendored directory (node_modules)")

    def test_wrong_case_reasons(self):
        # The files are below the root. At the root, the step keeps the first three (see the
        # next test).
        for rel, expected in (("docs/claude.md", "CLAUDE.md"), ("docs/agents.md", "AGENTS.md"),
                              ("docs/gemini.md", "GEMINI.md"), ("plans.md", "PLANS.md")):
            name = rel.rsplit("/", 1)[-1]
            with self.subTest(rel):
                self._file(rel, "# x\n")
                self.assertEqual(self._clean()[name],
                                 f"wrong letter case of {name} (instead of {expected})")

    def test_a_context_file_in_another_case_is_kept_at_the_root(self):
        """The tools find `agents.md` for `AGENTS.md` on a case-insensitive file system, and at
        the root such a file is an agent's instructions far more often than below it
        (docs/steps/collected_file_cleanup.md gives the counts)."""
        for name in ("claude.md", "Agents.md", "GEMINI.MD"):
            self._file(name, "# x\n")
        self._clean()
        self.assertEqual(sorted(p.name for p in self.repo.iterdir()),
                         ["Agents.md", "GEMINI.MD", "claude.md"])

    def test_an_empty_root_claude_md_in_another_case_keeps_a_root_agents_md_unread(self):
        """Claude Code reads a root agents.md only on a file system that ignores case, which
        also opens an empty root claude.md as CLAUDE.md, and it then reads no AGENTS.md. On the
        volume that compares case, where it reads a root AGENTS.md, the claude.md decides
        nothing, and the step deletes it as an empty file."""
        for agents, kept in (("agents.md", True), ("AGENTS.md", False)):
            with self.subTest(agents=agents):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self._file(agents, "Read @notes.md.\n")
                self._file("claude.md", "\n")
                self.assertEqual("claude.md" in self._clean(), not kept)
                self.assertEqual((self.repo / "claude.md").exists(), kept)

    def test_skill_md_reason(self):
        self._file("skills/SKILL.md", "# x\n")
        self.assertEqual(self._clean()["SKILL.md"], "SKILL.md outside valid directory")

    def test_github_without_trigger_reason(self):
        self._file(".github/workflows/ci.yml", "on: push\n")
        self.assertEqual(self._clean()["ci.yml"],
                         "file in .github without Copilot configuration or a context file")

    def test_github_kept_irrelevant_reason(self):
        self._file(".github/copilot-instructions.md", "# x\n")
        self._file(".github/workflows/ci.yml", "on: push\n")
        reasons = self._clean()
        self.assertEqual(reasons["ci.yml"],
                         "file in .github outside the Copilot paths")
        self.assertTrue((self.repo / ".github" / "copilot-instructions.md").exists())

    def test_unrelated_file_reason(self):
        self._file("README.md", "# readme\n")
        self.assertEqual(self._clean()["README.md"], "not a spec file")

    def test_non_json_in_github_hooks_reason(self):
        self._file(".github/hooks/hooks.json", '{"a": 1}')
        self._file(".github/hooks/run.sh", "echo hi\n")
        reasons = self._clean()
        self.assertEqual(reasons["run.sh"], "non-JSON file in .github/hooks")
        self.assertTrue((self.repo / ".github" / "hooks" / "hooks.json").exists())

    def test_git_hook_scripts_keep_no_github_directory(self):
        """`.github/hooks` is a Copilot trigger, and this step deletes the git hook scripts in
        it. The step decides about .github after that deletion. Otherwise the scripts would
        keep .github and the context files below a nested .github, and a second pass over the
        same tree would delete what the first kept."""
        self._file(".github/hooks/pre-commit", "#!/bin/sh\n")
        self._file("docs/.github/AGENTS.md", "# Agents\n")
        self._file("CLAUDE.md", "# Claude\n")
        first = self._clean()
        self.assertEqual(first["pre-commit"], "non-JSON file in .github/hooks")
        self.assertEqual(first["AGENTS.md"], "file in .github without Copilot configuration or a context file")
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / "CLAUDE.md").exists())

    def test_a_hooks_file_in_a_subfolder_of_github_hooks_is_kept(self):
        """Copilot CLI loads the hook JSON below `.github/hooks` too. A step that counted only
        the files directly in the folder would delete such a file, and with it a repository
        that has no other Copilot configuration."""
        self._file(".github/hooks/pre-commit", "#!/bin/sh\n")
        self._file(".github/hooks/team/hooks.json", '{"hooks": {"Stop": []}}')
        reasons = self._clean()
        self.assertEqual(reasons.get("pre-commit"), "non-JSON file in .github/hooks")
        self.assertNotIn("hooks.json", reasons)
        self.assertTrue((self.repo / ".github" / "hooks" / "team" / "hooks.json").exists())

    def test_an_empty_hooks_directory_beside_a_hooks_file_keeps_github(self):
        self._file(".github/hooks/hooks.json", '{"a": 1}')
        self._file(".github/hooks/run.sh", "echo hi\n")
        self._file("docs/.github/AGENTS.md", "# Agents\n")
        self._clean()
        self.assertTrue((self.repo / "docs" / ".github" / "AGENTS.md").exists())

    def test_directory_emptied_by_deletions_is_pruned(self):
        self._file("docs/notes.txt", "text\n")
        reasons = self._clean()
        self.assertEqual(reasons["notes.txt"], "not a spec file")
        self.assertEqual(reasons["docs"], "empty directory after cleanup")
        self.assertFalse((self.repo / "docs").exists())

    def test_only_the_topmost_removed_directory_is_logged(self):
        self._dir("docs/a/b")
        reasons = self._clean()
        self.assertEqual(reasons, {"docs": "empty directory after cleanup"})

    def test_plugin_parent_siblings_kept_and_others_deleted(self):
        self._file("plugins/mine/.claude-plugin/plugin.json", '{"a": 1}')
        self._file("plugins/mine/README.md", "# plugin\n")
        self._file("plugins/mine/index.js", "console.log(1)\n")
        reasons = self._clean()
        self.assertEqual(reasons["index.js"], "not a spec file")
        self.assertTrue((self.repo / "plugins" / "mine" / "README.md").exists())
        self.assertTrue(
            (self.repo / "plugins" / "mine" / ".claude-plugin" / "plugin.json").exists())

    def test_a_symlinked_directory_is_judged_by_its_own_path(self):
        """os.walk lists a link to a directory among the directories and does not descend into
        it. The step therefore moves the link to the files that it checks. Otherwise a `docs`
        link that no rule keeps would stay in the validated tree. Here the target is inside
        the repository, and only the validity check can remove the link."""
        self._file(".claude/skills/s/SKILL.md", "# s\n")
        (self.repo / "docs").symlink_to(".claude", target_is_directory=True)
        reasons = self._clean()
        self.assertFalse(os.path.lexists(self.repo / "docs"))
        self.assertTrue((self.repo / ".claude" / "skills" / "s" / "SKILL.md").is_file())
        self.assertEqual(reasons.get("docs"), "not a spec file")

    def test_a_link_inside_the_repository_is_kept_where_its_path_is_kept(self):
        self._file(".agents/skills/s/SKILL.md", "# s\n")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "skills").symlink_to("../.agents/skills",
                                                       target_is_directory=True)
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / ".claude" / "skills").is_symlink())

    def test_a_link_leading_outside_the_repository_is_removed(self):
        """The detection step would read through a kept link inside a spec directory and
        report the SKILL.md files that it reached as this repository's skills."""
        outside = self.tmp / "elsewhere"
        (outside / "s").mkdir(parents=True)
        (outside / "s" / "SKILL.md").write_text("# not this repository's\n", encoding="utf-8")
        self._file("CLAUDE.md", "# Claude\n")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "skills").symlink_to("../../elsewhere",
                                                       target_is_directory=True)
        (self.repo / ".claude" / "settings.json").symlink_to(outside / "s" / "SKILL.md")
        reasons = self._clean()
        self.assertFalse(os.path.lexists(self.repo / ".claude" / "skills"))
        self.assertFalse(os.path.lexists(self.repo / ".claude" / "settings.json"))
        self.assertTrue((outside / "s" / "SKILL.md").is_file(), "the target is left alone")
        self.assertTrue((self.repo / "CLAUDE.md").is_file())
        self.assertEqual(reasons.get("skills"), "symlink leading outside the repository")
        self.assertEqual(reasons.get("settings.json"), "symlink leading outside the repository")

    def test_the_target_of_a_link_that_rsync_kept_is_kept(self):
        """The collector copies `.github` whole, and rsync keeps a link that stays inside it,
        such as `.github/agents -> copilot-agents`, as a link. Judged by its own path, the
        target would be deleted as an irrelevant file in .github. A repository with no other
        configuration would then no longer be configured."""
        from agentcontext.steps import detection
        self._file(".github/copilot-agents/reviewer.agent.md",
                   "---\nname: reviewer\ndescription: Reviews pull requests\n---\nReview it.\n")
        (self.repo / ".github" / "agents").symlink_to("copilot-agents", target_is_directory=True)
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / ".github/copilot-agents/reviewer.agent.md").is_file())
        self.assertEqual([row["subagent"] for row in detection.detect_subagents(self.repo)],
                         [".github/agents/reviewer.agent.md"])

    def test_the_target_of_a_file_link_that_rsync_kept_is_kept(self):
        """rsync keeps a link to a file that stays inside `.github` as a link, such as
        `.github/copilot-instructions.md -> docs/copilot.md`. Judged by its own path, the
        target is an irrelevant file in .github. Deleting it would leave a link that leads
        nowhere, and the detection step does not count such a link."""
        from agentcontext.steps import detection
        self._file(".github/docs/copilot.md", "# Copilot\n")
        self._file(".github/agent-defs/reviewer.md",
                   "---\nname: reviewer\ndescription: Reviews pull requests\n---\nReview it.\n")
        (self.repo / ".github" / "copilot-instructions.md").symlink_to("docs/copilot.md")
        (self.repo / ".github" / "agents").mkdir()
        (self.repo / ".github" / "agents" / "reviewer.agent.md").symlink_to(
            "../agent-defs/reviewer.md")
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / ".github/docs/copilot.md").is_file())
        self.assertTrue((self.repo / ".github/agent-defs/reviewer.md").is_file())
        self.assertIn(os.path.join(".github", "copilot-instructions.md"),
                      detection.detect_context_files(self.repo))
        self.assertEqual([row["subagent"] for row in detection.detect_subagents(self.repo)],
                         [".github/agents/reviewer.agent.md"])

    def test_each_link_of_a_chain_that_rsync_kept_is_kept(self):
        """A kept link that leads through another link to its file reaches the file only while
        the link between them is there. Judged by its own path, that middle link would be
        deleted."""
        self._file(".github/docs/copilot.md", "# Copilot\n")
        (self.repo / ".github" / "shared").mkdir()
        (self.repo / ".github" / "shared" / "copilot.md").symlink_to("../docs/copilot.md")
        (self.repo / ".github" / "copilot-instructions.md").symlink_to("shared/copilot.md")
        self.assertEqual(self._clean(), {})
        self.assertTrue((self.repo / ".github/shared/copilot.md").is_symlink())
        self.assertEqual((self.repo / ".github/copilot-instructions.md").read_text(encoding="utf-8"),
                         "# Copilot\n")

    def test_the_target_of_a_link_that_no_rule_keeps_is_judged_by_its_own_path(self):
        """The step reads through only a link that a rule keeps. A link that the step deletes
        keeps nothing that it leads to."""
        self._file(".github/copilot-instructions.md", "# Copilot\n")
        self._file(".github/docs/notes.md", "# Notes\n")
        (self.repo / ".github" / "notes.md").symlink_to("docs/notes.md")
        reasons = self._clean()
        self.assertFalse((self.repo / ".github/docs/notes.md").exists())
        self.assertEqual(reasons.get("notes.md"),
                         "file in .github outside the Copilot paths")

    def test_nothing_is_deleted_through_a_hooks_link_inside_the_repository(self):
        """The `.github/hooks` pass deletes every file in the folder that is not a JSON file.
        Through a link, it would delete the files of the link's target, which the rules judge
        by their own paths."""
        self._file(".github/copilot-instructions.md", "# Copilot\n")
        self._file(".github/hook-scripts/check.sh", "echo hi\n")
        (self.repo / ".github" / "hooks").symlink_to("hook-scripts", target_is_directory=True)
        reasons = self._clean()
        self.assertNotEqual(reasons.get("check.sh"), "non-JSON file in .github/hooks")

    def test_nothing_is_deleted_through_a_link_leading_outside(self):
        """The `.github/hooks` pass lists the directory that it is given, and through a link it
        would remove the non-JSON files in the link's target, outside the validated tree."""
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "run.sh").write_text("echo hi\n", encoding="utf-8")
        self._file(".github/copilot-instructions.md", "# Copilot\n")
        (self.repo / ".github" / "hooks").symlink_to(outside, target_is_directory=True)
        reasons = self._clean()
        self.assertTrue((outside / "run.sh").is_file())
        self.assertEqual(reasons.get("hooks"), "symlink leading outside the repository")


# ---------------------------------------------------------------------------
# compute_columns  (the eleven flags, derived again from the remaining files)
# ---------------------------------------------------------------------------

class TestTheLocalStateOfAToolIsDeleted(_RepoCase):
    """A tree collected under an earlier collector holds the local state of the tools: the
    files that they keep for themselves inside their directories. The cleanup deletes these
    files whatever rule would otherwise keep them, names their place in the deletion log and
    keeps the configuration beside them."""

    CONFIGURATION = ("CLAUDE.md", ".claude/settings.json", ".claude/projects/js-to-ts/AGENT.md",
                     ".claude/tasks/build-app.md", ".codex/config.toml", ".cursor/rules/a.mdc")
    LOCAL_STATE = {
        ".claude/projects/-Users-a-repo/memory/MEMORY.md": ".claude/projects/-*/",
        ".claude/projects/C--Users-a-repo/0a1b.jsonl": ".claude/projects/?--*/",
        ".claude/scheduled_tasks.lock": ".claude/scheduled_tasks.lock",
        # Without the rule for local state, the step would keep these as a context file, a
        # planning document and a file of a plugin's manifest directory.
        ".claude/worktrees/w/CLAUDE.md": ".claude/worktrees/",
        ".claude/worktrees/w/.claude/plans/plan.md": ".claude/worktrees/",
        ".claude/plugins/cache/m/p/1.0.0/.claude-plugin/plugin.json": ".claude/plugins/cache/",
        ".agents/scheduled_tasks.lock": ".agents/scheduled_tasks.lock",
        "pkg/.codex/sessions/2026/10/01/rollout-x.jsonl": ".codex/sessions/",
        ".gemini/tmp/h/chats/session.json": ".gemini/tmp/",
    }

    def test_each_file_of_a_place_is_deleted_with_the_place_as_its_reason(self):
        for rel in self.CONFIGURATION:
            self._file(rel, "# x\n")
        for rel in self.LOCAL_STATE:
            self._file(rel, "# local\n")
        deletions = []
        scan_and_clean_repo(str(self.repo), deletions)
        reasons = {Path(path).relative_to(os.path.relpath(self.repo)).as_posix(): reason
                   for path, reason in deletions
                   if reason != "empty directory after cleanup"}
        self.assertEqual(reasons, {rel: f"local state of a tool ({place})"
                                   for rel, place in self.LOCAL_STATE.items()})
        for rel in self.CONFIGURATION:
            with self.subTest(rel):
                self.assertTrue((self.repo / rel).is_file())


class TestComputeColumns(_RepoCase):

    def test_missing_directory_is_all_false(self):
        columns = compute_columns(str(self.repo / "nope"))
        self.assertTrue(all_false(columns))

    def test_empty_repo_is_all_false(self):
        self.assertTrue(all_false(compute_columns(str(self.repo))))

    def test_spec_dirs_set_their_tool_flag(self):
        for name, flag in ((".claude", "claude"), (".gemini", "gemini"),
                           (".codex", "codex"), (".cursor", "cursor")):
            with self.subTest(name):
                self._dir(name)
                self.assertTrue(compute_columns(str(self.repo))[flag])

    def test_spec_dir_must_be_at_the_top_level(self):
        # The directory check reads the top level only: a nested .claude sets nothing.
        self._dir("sub/.claude")
        self.assertTrue(all_false(compute_columns(str(self.repo))))

    def test_github_with_trigger_sets_copilot(self):
        self._dir(".github/agents")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["copilot"])
        self.assertFalse(columns["copilot-instructions"])

    def test_copilot_instructions_file_sets_both_flags(self):
        self._file(".github/copilot-instructions.md")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["copilot"])
        self.assertTrue(columns["copilot-instructions"])

    def test_instructions_dir_sets_both_flags(self):
        self._dir(".github/instructions")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["copilot"])
        self.assertTrue(columns["copilot-instructions"])

    def test_github_without_trigger_sets_no_copilot(self):
        self._file(".github/workflows/ci.yml")
        self.assertFalse(compute_columns(str(self.repo))["copilot"])

    def test_claude_md_sets_claude_and_claude_md(self):
        self._file("CLAUDE.md")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["claude"])
        self.assertTrue(columns["claude.md"])

    def test_gemini_md_sets_gemini_and_gemini_md(self):
        self._file("GEMINI.md")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["gemini"])
        self.assertTrue(columns["gemini.md"])

    def test_agents_md_does_not_set_codex(self):
        self._file("AGENTS.md")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["agents.md"])
        self.assertFalse(columns["codex"])

    def test_agents_override_sets_agents_md(self):
        self._file("AGENTS.override.md")
        self.assertTrue(compute_columns(str(self.repo))["agents.md"])

    def test_plans_md_sets_plans_md(self):
        self._file("PLANS.md")
        self.assertTrue(compute_columns(str(self.repo))["plans.md"])

    def test_cursorrules_sets_cursor_and_cursorrules(self):
        self._file(".cursorrules")
        columns = compute_columns(str(self.repo))
        self.assertTrue(columns["cursor"])
        self.assertTrue(columns["cursorrules"])

    def test_standalone_files_are_found_at_any_depth(self):
        self._file("docs/nested/CLAUDE.md")
        self.assertTrue(compute_columns(str(self.repo))["claude.md"])

    def test_wrong_case_standalone_file_below_the_root_sets_nothing(self):
        self._file("docs/claude.md")
        self.assertTrue(all_false(compute_columns(str(self.repo))))

    def test_wrong_case_context_file_at_the_root_sets_its_flags(self):
        self._file("claude.md")
        self._file("Agents.md")
        columns = compute_columns(str(self.repo))
        self.assertEqual({k for k, v in columns.items() if v}, {"claude", "claude.md", "agents.md"})


# ---------------------------------------------------------------------------
# all_false  (whether every flag is FALSE)
# ---------------------------------------------------------------------------

class TestAllFalse(unittest.TestCase):

    def test_all_false_columns(self):
        self.assertTrue(all_false({"claude": False, "cursor": False}))

    def test_one_true_column(self):
        self.assertFalse(all_false({"claude": True, "cursor": False}))

    def test_every_column_true(self):
        self.assertFalse(all_false({"claude": True, "cursor": True}))


# ---------------------------------------------------------------------------
# main  (collected tree unchanged, validated tree derived, exit codes that report failures)
# ---------------------------------------------------------------------------

class _MainCase(unittest.TestCase):
    """A test case for main(), with every path in a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # Each list is at the root of the tree that it lists, as in a real run. A fixture with
        # the lists outside the tree would not test what a list inside it does to the copy.
        self.collected_dir = self.tmp / "data" / "collected"
        self.collected_csv = self.collected_dir / "repos.csv"
        self.clone_failed_csv = self.collected_dir / "clone_failed.csv"
        self.validated_dir = self.tmp / "data" / "validated"
        self.validated_csv = self.validated_dir / "repos.csv"
        self.excluded_dir = self.tmp / "data" / "excluded"
        self.deletion_csv = self.tmp / "deletion.csv"
        self.collected_dir.mkdir(parents=True)
        # The cloning step always writes clone_failed.csv, with a header and no rows when no
        # clone failed. Every run of this step has one to copy or leave behind.
        self.clone_failed_csv.write_text("repo_name,repo_url,reason\n", encoding="utf-8")
        self._csv_rows = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, repo_name: str, files: dict = None, dirs: list = None,
                 in_csv: bool = True) -> Path:
        """Add a repository to the collected tree and rewrite the collected CSV."""
        repo = self.collected_dir / repo_name.replace("/", "§")
        repo.mkdir(parents=True, exist_ok=True)
        for rel in dirs or []:
            (repo / rel).mkdir(parents=True, exist_ok=True)
        for rel, content in (files or {}).items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if in_csv:
            self._csv_rows.append([repo_name, "2026-03-29", "main", "a" * 40,
                                   "2026-01-01T00:00:00Z"])
        with open(self.collected_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(META)
            w.writerows(self._csv_rows)
        return repo

    def _args(self):
        return [
            "--collected-dir", str(self.collected_dir),
            "--collected-csv", str(self.collected_csv),
            "--validated-dir", str(self.validated_dir),
            "--excluded-dir", str(self.excluded_dir),
            "--validated-csv", str(self.validated_csv),
            "--deletion-csv", str(self.deletion_csv),
            # Without --submodules, the step would read the real checkout's
            # `data/collection/submodules.csv`, and a row there that names a fixture
            # repository would change the outcome of these tests.
            "--submodules", str(self.tmp / "submodules.csv"),
        ]

    def _run(self):
        return self._run_io()[0]

    def test_a_directory_the_clone_failure_list_names_is_not_copied(self):
        """A cloning error after the copy can leave a tree with a row in the collected list
        that records nothing as collected. If the step copied the tree, the release would
        publish the repository with a blank branch and head_sha. The cloning step removes such
        a tree, but an older collected tree can still contain one."""
        self._collect("owner/kept", {"CLAUDE.md": "# kept\n" + "x" * 80})
        self._collect("owner/failed", {"CLAUDE.md": "# failed\n" + "x" * 80})
        rows = self.collected_csv.read_text(encoding="utf-8").splitlines()
        rows = [r if not r.startswith("owner/failed,") else "owner/failed," + ",".join(
            ["" if i in (1, 2, 3) else v for i, v in enumerate(r.split(",")[1:], 1)])
                for r in rows]
        self.collected_csv.write_text("\n".join(rows) + "\n", encoding="utf-8")
        self.clone_failed_csv.write_text(
            "repo_name,repo_url,reason\nowner/failed,https://github.com/owner/failed,collector_error\n",
            encoding="utf-8")
        code, _out, err = self._run_io()
        self.assertEqual(code, 0)
        # The message is a warning and goes to stderr with every other log message
        # (agentcontext/logs.py).
        self.assertIn("owner§failed is in clone_failed.csv. Did not copy", err)
        self.assertTrue((self.validated_dir / "owner§kept").is_dir())
        self.assertFalse((self.validated_dir / "owner§failed").exists())
        self.assertEqual(_mod.load_clone_failures(str(self.clone_failed_csv)), {"owner/failed"})
        self.assertEqual(_mod.load_clone_failures(str(self.tmp / "absent.csv")), set())

        # The function logs a warning for a header that is not the current one and for the
        # zero-byte file that a killed cloning run leaves. Read with `.get`, both would return
        # an empty set, and the check above would do nothing.
        for content in ("", "repo,reason\nowner/failed,x\n"):
            with self.subTest(content=content):
                other = self.tmp / "other_shape.csv"
                other.write_text(content, encoding="utf-8")
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(_mod.load_clone_failures(str(other)), set())
                self.assertIn("no repo_name column", err.getvalue())

    def _run_io(self, args: list = None) -> tuple:
        """Run main() and return (exit code, stdout, stderr).

        Several checks differ only in their message. A test that reads only the exit code
        cannot tell which check failed.
        """
        orig_argv = sys.argv
        out, err = io.StringIO(), io.StringIO()
        try:
            sys.argv = ["prog"] + (self._args() if args is None else args)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main()
        finally:
            sys.argv = orig_argv
        return (rc, *_logged.split(out.getvalue() + err.getvalue()))

    def _validated_rows(self) -> list:
        with self.validated_csv.open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _deletion_reasons(self) -> dict:
        with self.deletion_csv.open(newline="", encoding="utf-8") as f:
            return {os.path.basename(row["path"]): row["reason"]
                    for row in csv.DictReader(f)}


class TestAStrayDirectoryIsNotARepository(_MainCase):
    """data/collected/ contains one directory per repository, the two lists that the cloning
    step writes beside them, and nothing else. The step would otherwise let any other
    directory pass the os.path.isdir test, scan it and write it into the validated list under
    its own name. The release would then publish a repository that nobody collected."""

    def _stray(self, name: str, rel: str = "CLAUDE.md") -> Path:
        p = self.collected_dir / name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# stray\n", encoding="utf-8")
        return p

    def test_a_stray_directory_stays_out_of_the_validated_list(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._stray("notes")
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a"])

    def test_a_nested_reconstruction_does_not_become_a_repository(self):
        """mv into an existing data/collected/ nests the sample at data/collected/repos/."""
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._stray("repos", "o§b/CLAUDE.md")
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a"])

    def test_the_repositories_beside_it_still_validate(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._stray("notes")
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "o§a" / "CLAUDE.md").is_file())

    def test_a_stray_never_reaches_the_validated_tree(self):
        """The copy takes the repository directories one at a time. A stray stays in the
        collected tree rather than reaching the validated tree, and the step names it.

        The warning has to read the collected tree. The step rebuilds the validated tree
        from that copy on every run, and when the second split runs, the entry is not
        there. No later step could name it. A stray is either an accident to remove or a
        naming change that nobody recorded. `repos.split` exists so that the step excludes
        no stray without a warning.
        """
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._stray("notes")
        rc, _out, err = self._run_io()
        self.assertEqual(rc, 0)
        self.assertTrue((self.collected_dir / "notes").is_dir(),
                        "the collected tree is never modified")
        self.assertFalse((self.validated_dir / "notes").exists())
        self.assertIn("notes", err)
        self.assertIn("not copied into the validated tree", err)

    def test_a_collected_tree_of_only_strays_is_an_error_rather_than_an_empty_run(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"}, in_csv=True)
        # Remove the one real repository and leave the stray behind.
        shutil.rmtree(self.collected_dir / "o§a")
        self._stray("notes")
        self.assertNotEqual(self._run(), 0)


class TestASymlinkedRepositoryDirectoryIsNotARepository(_MainCase):
    """A repository directory is a directory that the cloning step wrote, not a link to a
    tree elsewhere. ``os.path.isdir`` follows a link, and the step would scan a symlinked
    entry taken for a repository through its link. The cleanup would then remove the invalid
    files in the link's target, outside the validated tree that this step writes. Removing
    such a repository when it has no detected mechanism would reach ``rmtree``, which raises
    for a symbolic link and stops the run."""

    def _link(self, name: str = "o§linked") -> Path:
        """Link an entry with a repository directory's name to a tree outside the collected
        directory."""
        outside = self.tmp / "outside"
        outside.mkdir(exist_ok=True)
        (outside / "notes.md").write_text("# not a configuration\n", encoding="utf-8")
        (outside / "empty.txt").write_text("", encoding="utf-8")
        os.symlink(outside, self.collected_dir / name)
        return outside

    def test_the_run_finishes_instead_of_failing_in_rmtree(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._link()
        self.assertEqual(self._run(), 0)

    def test_the_link_target_is_left_alone(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        outside = self._link()
        self._run()
        self.assertTrue((outside / "notes.md").is_file())
        self.assertTrue((outside / "empty.txt").is_file())

    def test_a_link_target_containing_a_configuration_is_left_alone_too(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        outside = self._link()
        (outside / ".claude").mkdir()
        (outside / ".claude" / "settings.json").write_text('{"a": 1}\n', encoding="utf-8")
        self.assertEqual(self._run(), 0)
        self.assertTrue((outside / "notes.md").is_file())

    def test_a_link_into_the_raw_source_does_not_clean_the_raw_source(self):
        """Without the check, a link into data/collected/ would do damage without a warning.
        Nothing would raise, the run would exit 0, and the cleanup would delete through the
        link in the tree that the step never modifies. The release would also publish the
        link's own name as a repository that nobody collected."""
        self._collect("o/a", files={"CLAUDE.md": "# a\n",
                                    "notes.md": "# not a configuration\n"})
        self._collect("o/b", files={"CLAUDE.md": "# b\n"})
        os.symlink(self.collected_dir / "o§a", self.collected_dir / "o§link")
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.collected_dir / "o§a" / "notes.md").is_file())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a", "o/b"])

    def test_a_symlinked_entry_stays_out_of_the_validated_list(self):
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._link()
        self._run()
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a"])

    def test_a_symlinked_entry_never_reaches_the_validated_tree(self):
        """As with the stray above, the copy follows the list of repository directories, and
        a link is not a repository directory. The validated tree therefore contains no entry
        through which a later step could descend into a tree outside it. The step logs the
        link where the link is, in the collected tree."""
        self._collect("o/a", files={"CLAUDE.md": "# a\n"})
        self._link()
        rc, _out, err = self._run_io()
        self.assertEqual(rc, 0)
        self.assertFalse((self.validated_dir / "o§linked").exists())
        self.assertEqual(sorted(p.name for p in self.validated_dir.iterdir()),
                         ["o§a", "repos.csv"])
        self.assertIn("o§linked", err)


class TestRawSourceUntouched(_MainCase):
    """data/collected/, including both lists in it, never changes."""

    def setUp(self):
        super().setUp()
        self._collect("o/a", {"CLAUDE.md": "# hi\n", ".DS_Store": "junk"}, dirs=[".claude"])
        self.source_text = ("%s\no/a,2026-03-29,main,%s,2026-01-01T00:00:00Z\n"
                            % (",".join(META), "a" * 40))
        self.collected_csv.write_text(self.source_text)

    def test_source_files_survive_a_run(self):
        rc = self._run()
        self.assertEqual(rc, 0)
        # The invalid file is still in the collected tree, which the step never modifies...
        self.assertTrue((self.collected_dir / "o§a" / ".DS_Store").exists())
        self.assertEqual(self.collected_csv.read_text(), self.source_text)

    def test_the_invalid_file_is_removed_from_the_validated_tree(self):
        self._run()
        # ...but removed from the validated tree.
        self.assertFalse((self.validated_dir / "o§a" / ".DS_Store").exists())
        self.assertTrue((self.validated_dir / "o§a" / "CLAUDE.md").exists())

    def test_the_deletion_log_is_written_sorted(self):
        """The passes over the tree take the filesystem's order, and two runs over freshly
        copied trees do not share it. The step sorts the log so that a repeated run writes
        the same bytes."""
        self._collect("o/b", {"CLAUDE.md": "# hi\n", "zz.tmp": "junk", "aa.tmp": "junk",
                              ".claude/mm.tmp": "junk"}, dirs=[".claude"])
        self.collected_csv.write_text(
            self.source_text + "o/b,2026-03-29,main,%s,2026-01-01T00:00:00Z\n" % ("b" * 40))
        self.assertEqual(self._run(), 0)
        with open(self.deletion_csv, newline="", encoding="utf-8") as f:
            rows = [(r["path"], r["reason"]) for r in csv.DictReader(f)]
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual(rows, sorted(rows))

    def test_the_deletion_log_ends_each_record_with_lf(self):
        """The deletion log ends each record with LF, as every CSV of the pipeline does, rather
        than with the CRLF that the csv module writes by default."""
        self.assertEqual(self._run(), 0)
        self.assertNotIn(b"\r\n", self.deletion_csv.read_bytes())
        self.assertIn(".DS_Store", self._deletion_reasons())

    def test_working_input_csv_is_trimmed(self):
        self._run()
        with self.validated_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows, [{"repo_name": "o/a", "scanned_at": "2026-03-29",
                                 "branch": "main", "head_sha": "a" * 40,
                                 "head_commit_date": "2026-01-01T00:00:00Z"}])

    def test_rerun_recovers_from_a_validated_tree_that_is_gone(self):
        """An earlier partial run can leave this step's own output damaged. Because the list
        is in the tree, removing the tree removes the list too, and the next run has to
        rebuild both from the collected tree."""
        self._run()
        shutil.rmtree(self.validated_dir)
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "o§a" / "CLAUDE.md").exists())
        with self.validated_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["repo_name"], "o/a")

    def test_rerun_recovers_from_a_truncated_working_list(self):
        """This test covers the other damage: the tree is intact, and the list inside it is
        empty."""
        self._run()
        self.validated_csv.write_text("", encoding="utf-8")
        self.assertEqual(self._run(), 0)
        with self.validated_csv.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["repo_name"], "o/a")

    def test_fails_on_a_missing_collected_list(self):
        self.collected_csv.unlink()
        rc, _out, err = self._run_io()
        self.assertEqual(rc, 1)
        self.assertIn("Could not find the collected list", err)

    def test_fails_on_a_missing_collected_tree(self):
        """Removing the tree removes the list inside it, and the check for the list then
        fails first. The check for the tree covers the case that the options allow: a
        `--collected-csv` that exists and a `--collected-dir` that names a missing
        directory."""
        args = self._args()
        args[args.index("--collected-dir") + 1] = str(self.tmp / "gone")
        rc, _out, err = self._run_io(args)
        self.assertEqual(rc, 1)
        self.assertIn("Could not find the collected tree", err)

    def test_removing_the_whole_collected_tree_takes_the_list_with_it(self):
        """The counterpart of the test above: with both options at their defaults, removing
        the tree removes the list."""
        shutil.rmtree(self.collected_dir)
        rc, _out, err = self._run_io()
        self.assertEqual(rc, 1)
        self.assertIn("Could not find the collected list", err)

    def test_fails_on_a_collected_tree_containing_only_its_lists(self):
        """The check for an empty collection tests whether the tree contains a repository
        directory, not whether the directory is empty.

        Both lists are stored in the tree. A cloning run that cloned nothing still leaves
        two files, and a test of `any(entries)` would never find the tree empty. The run
        would then copy no repository, write a validated list with no rows and exit 0. The
        runner would read that exit code as success and continue with an empty sample.

        The message has to name the collected tree. The same test runs over the validated
        tree after the copy and returns 1 as well, and the exit code alone cannot tell a
        refusal before the copy from a refusal after it.
        """
        for name in os.listdir(self.collected_dir):
            entry = self.collected_dir / name
            if entry.is_dir():
                shutil.rmtree(entry)
        self.assertEqual(sorted(os.listdir(self.collected_dir)),
                         ["clone_failed.csv", "repos.csv"],
                         "the fixture has to leave both lists in place")
        rc, _out, err = self._run_io()
        self.assertEqual(rc, 1)
        self.assertIn(f"No repository directory found in {self.collected_dir}", err)
        self.assertFalse(self.validated_dir.exists(),
                         "the refusal has to come before the validated tree is replaced")


class TestTheValidatedTreeContainsRepositoriesAndOneList(_MainCase):
    """The step copies the repository directories one at a time, and then the list under
    the name that it publishes.

    Both lists are at the root of the collected tree, and a `copytree` of that directory
    would copy `repos.csv` and `clone_failed.csv` into the validated tree. The list that
    this step publishes would then overwrite the copied `repos.csv`. `clone_failed.csv`, a
    record of the cloning step, would remain in the validated tree. There,
    `agentcontext/steps/collected_files_archive.py` would copy it into the published archive
    of collected files, and every later `repos.split` would report it as a stray.
    """

    def setUp(self):
        super().setUp()
        self._collect("o/a", {"CLAUDE.md": "# a\n"})
        self._collect("o/b", {"CLAUDE.md": "# b\n"})

    def test_the_tree_contains_the_repository_directories_and_the_list(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(sorted(p.name for p in self.validated_dir.iterdir()),
                         ["o§a", "o§b", "repos.csv"])

    def test_the_clone_failure_list_is_not_copied(self):
        """This entry has a test of its own, because a `copytree` would leave it and nothing
        later overwrites it."""
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.clone_failed_csv.is_file(), "the collected tree keeps its own")
        self.assertFalse((self.validated_dir / "clone_failed.csv").exists())

    def test_the_published_list_contains_the_collected_rows(self):
        """The step derives the list from the copy. A copy that took no rows would leave
        every assertion above true and the sample empty."""
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a", "o/b"])

    def test_the_published_list_ends_each_record_with_lf(self):
        """The published list ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default. The collected list
        of the fixture ends its records with CRLF."""
        self.assertIn(b"\r\n", self.collected_csv.read_bytes())
        self.assertEqual(self._run(), 0)
        self.assertNotIn(b"\r\n", self.validated_csv.read_bytes())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a", "o/b"])

    def test_the_list_in_the_tree_is_not_reported_as_a_stray(self):
        """`repos.split` receives the list as an expected entry. Otherwise the step would
        name the file that it has just written in the warning about real accidents, on every
        run, and nobody reads a warning that is always there.

        The test checks both trees, because each has its lists at its root. It reads stderr,
        where the warning goes."""
        rc, _out, err = self._run_io()
        self.assertEqual(rc, 0)
        self.assertNotIn("are not repository directories", err)
        self.assertNotIn("repos.csv", err)
        self.assertNotIn("clone_failed.csv", err)

    def test_a_repository_directory_is_copied_whole(self):
        """The per-repository copy keeps `symlinks=True` and the subtree below the root,
        which a loop written over the top-level names alone would lose."""
        self._collect("o/c", {".cursor/rules/style.mdc": "be terse\n"})
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "o§c" / ".cursor" / "rules"
                         / "style.mdc").is_file())


# ---------------------------------------------------------------------------
# main  (a collected list with the old name of the head commit date column)
# ---------------------------------------------------------------------------

class TestALegacyCollectedListIsRefused(_MainCase):
    """An older collected list names the head commit date `timestamp`. The filter that
    derives data/validated/repos.csv keeps the columns that it recognizes and drops the rest
    without a warning. Such a list would validate into a list with no dates, and the
    detection step would publish a blank `head_commit_date` for every repository."""

    def _legacy_source(self) -> None:
        """Rewrite the collected list with the old header, which has `timestamp`."""
        with open(self.collected_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["repo_name", "timestamp", "branch", "head_sha"])
            w.writerow(["o/a", "2026-01-01T00:00:00Z", "main", "a" * 40])

    def test_the_run_stops_rather_than_dropping_the_column(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._legacy_source()
        self.assertEqual(self._run(), 1)

    def test_the_message_names_the_column_and_what_replaced_it(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._legacy_source()
        err = io.StringIO()
        orig_argv = sys.argv
        try:
            sys.argv = ["prog"] + self._args()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                main()
        finally:
            sys.argv = orig_argv
        self.assertIn(LEGACY_TIMESTAMP_COLUMN, err.getvalue())
        self.assertIn("head_commit_date", err.getvalue())
        self.assertIn("scanned_at", err.getvalue())

    def test_it_stops_before_the_validated_tree_is_replaced(self):
        """The refusal has to come before `data/validated/` and the list inside it are
        replaced, because the later steps read that copy."""
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)
        before = self.validated_csv.read_bytes()
        self._legacy_source()
        self.assertEqual(self._run(), 1)
        self.assertEqual(self.validated_csv.read_bytes(), before)
        self.assertTrue((self.validated_dir / "o§a" / "CLAUDE.md").is_file())

    def test_the_current_header_validates(self):
        """The check reads one column name. A list without that name has to pass rather than
        be refused for the columns that it has."""
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(list(self._validated_rows()[0]), META)

    def test_a_source_with_both_column_names_is_refused_too(self):
        """Such a list is in the middle of a migration. `head_commit_date` looks correct,
        while `timestamp` shows that a program that was not updated wrote the file."""
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        with open(self.collected_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([*META, "timestamp"])
            w.writerow(["o/a", "2026-03-29", "main", "a" * 40,
                        "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"])
        self.assertEqual(self._run(), 1)


# ---------------------------------------------------------------------------
# main  (which repositories a run keeps, and what the two output CSVs contain)
# ---------------------------------------------------------------------------

class TestARepositoryIsKeptOnlyForADetectedMechanism(_MainCase):
    """The step keeps a repository when it has a configuration, not when it has a directory
    that could contain one."""

    def test_a_tool_directory_with_nothing_in_it_is_dropped(self):
        # .claude/ exists, but an empty directory is not a configuration.
        self._collect("owner/empty-dir", files={".claude/notes.txt": "x"},
                      dirs=[".claude"])
        self._run()
        self.assertFalse((self.validated_dir / "owner§empty-dir").exists())

    def test_a_repository_with_one_detected_file_is_kept(self):
        self._collect("owner/one-file", files={"CLAUDE.md": "be terse"})
        self._run()
        self.assertTrue((self.validated_dir / "owner§one-file" / "CLAUDE.md").is_file())

    def test_the_kept_repository_reaches_the_output_csv(self):
        self._collect("owner/one-file", files={"CLAUDE.md": "be terse"})
        self._run()
        with open(self.validated_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([r["repo_name"] for r in rows], ["owner/one-file"])

    def test_the_dropped_repository_is_logged_with_its_reason(self):
        self._collect("owner/empty-dir", files={".claude/notes.txt": "x"},
                      dirs=[".claude"])
        self._run()
        with open(self.deletion_csv, newline="", encoding="utf-8") as f:
            reasons = {r["reason"] for r in csv.DictReader(f)}
        self.assertIn("no detected mechanism, repository directory removed", reasons)

    def test_a_repository_whose_only_artifacts_are_other_is_dropped(self):
        # The cleanup keeps the collected .claude/notes.txt as a file, but none of the ten
        # mechanisms matches it. It goes to "other", and the repository has no configuration
        # that this dataset describes.
        self._collect("owner/other-only", files={".claude/notes.txt": "scratch"})
        self._run()
        self.assertFalse((self.validated_dir / "owner§other-only").exists())

    def test_a_mechanism_below_the_top_level_still_counts(self):
        self._collect("owner/nested", files={".cursor/rules/style.mdc": "be terse"})
        self._run()
        self.assertTrue((self.validated_dir / "owner§nested").is_dir())

    def test_claude_rules_are_kept_but_unrelated_notes_are_dropped(self):
        self._collect("owner/rules", files={".claude/rules/style.md": "Use explicit types."})
        self._collect("owner/notes", files={".claude/notes/style.md": "A meeting note."})
        self._run()
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["owner/rules"])
        self.assertTrue((self.validated_dir / "owner§rules/.claude/rules/style.md").is_file())
        self.assertFalse((self.validated_dir / "owner§notes").exists())

    def test_the_check_keeps_the_repositories_at_the_relative_paths_that_a_real_run_passes(self):
        """The runner starts the step in the stage directory with the default
        `data/validated`, and the check receives a relative path. Every other test here
        passes absolute paths. A check that returned False for a relative path would
        drop every repository of a real run while every test passed."""
        self._collect("owner/one-file", files={"CLAUDE.md": "be terse"})
        self._collect("owner/linked", files={
            ".github/instructions": "../.claude/instructions",
            ".claude/instructions/a.instructions.md": "# a\n"})
        relative = [str(Path(a).relative_to(self.tmp)) if a.startswith(str(self.tmp)) else a
                    for a in self._args()]
        cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            code, _out, _err = self._run_io(relative)
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0)
        self.assertEqual([r["repo_name"] for r in self._validated_rows()],
                         ["owner/linked", "owner/one-file"])


class TestAnAgentWorkflowReachesTheDetector(_MainCase):
    """The tests run a workflow through this step and then the detection step, in the order
    of a real run. The workflow runs a documented agent action under a name that no path row
    gives it.

    Other tests check the heuristic that finds such a workflow over the collector and over
    the detector, each with a tree written for it. This step runs between them. If it
    deleted such a file as an irrelevant file in `.github`, it would drop a repository with
    no other configuration, and the heuristic would never reach a CSV in a real run."""

    WORKFLOW = "jobs:\n  review:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
    CI = "jobs:\n  test:\n    steps:\n      - uses: actions/checkout@v4\n"

    def _detect(self, names=("repos.csv", "settings.csv")) -> dict:
        """Run the detection step over the validated tree and return {csv name: rows}."""
        detect = importlib.import_module("agentcontext.steps.detection")
        saved = (detect.VALIDATED_DIR, detect.VALIDATED_CSV, detect.OUTPUT_DIR,
                 detect.COMMITS_DIR)
        cwd = os.getcwd()
        output = self.tmp / "output"
        try:
            detect.VALIDATED_DIR = self.validated_dir
            detect.VALIDATED_CSV = self.validated_csv
            detect.OUTPUT_DIR = output
            detect.COMMITS_DIR = self.tmp / "collect_commits"
            os.chdir(self.tmp)
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(detect.main([]), 0)
        finally:
            (detect.VALIDATED_DIR, detect.VALIDATED_CSV, detect.OUTPUT_DIR,
             detect.COMMITS_DIR) = saved
            os.chdir(cwd)
        tables = {}
        for name in names:
            with open(output / name, newline="", encoding="utf-8") as f:
                tables[name] = list(csv.DictReader(f))
        return tables

    def test_a_renamed_agent_workflow_is_kept_detected_and_attributed(self):
        self._collect("owner/only-workflow", {
            ".github/workflows/nightly-agent.yml": self.WORKFLOW,
            ".github/workflows/ci.yml": self.CI})
        self._collect("owner/with-copilot", {
            ".github/workflows/nightly-agent.yml": self.WORKFLOW,
            ".github/copilot-instructions.md": "# Copilot\n" + "x" * 80})
        code, _out, _err = self._run_io()
        self.assertEqual(code, 0)
        only = self.validated_dir / "owner§only-workflow" / ".github" / "workflows"
        self.assertTrue((only / "nightly-agent.yml").is_file(),
                        "the agent workflow was deleted, or its repository dropped")
        self.assertFalse((only / "ci.yml").exists(), "a workflow with no agent action is CI")
        self.assertTrue((self.validated_dir / "owner§with-copilot" / ".github" / "workflows"
                         / "nightly-agent.yml").is_file())

        tables = self._detect()
        flags = {r["repo_name"]: (r["claude"], r["copilot"], r["settings"])
                 for r in tables["repos.csv"]}
        self.assertEqual(flags, {"owner/only-workflow": ("TRUE", "FALSE", "TRUE"),
                                 "owner/with-copilot": ("TRUE", "TRUE", "TRUE")})
        self.assertEqual(sorted((r["repo_name"], r["settings"]) for r in tables["settings.csv"]),
                         [("owner/only-workflow", ".github/workflows/nightly-agent.yml"),
                          ("owner/with-copilot", ".github/workflows/nightly-agent.yml")])

    def test_an_agentic_workflow_is_kept_detected_and_attributed_to_its_engine(self):
        """This step keeps a GitHub Agentic Workflow, a Markdown file in `.github`. A
        repository with no other configuration stays in the validated list."""
        self._collect("owner/agentic", {
            ".github/workflows/review.md": "---\non: pull_request\nengine: claude\n---\nReview.\n",
            ".github/workflows/README.md": "# Workflows\n"})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§agentic" / ".github" / "workflows"
        self.assertTrue((kept / "review.md").is_file())
        self.assertFalse((kept / "README.md").exists())
        tables = self._detect()
        flags = {r["repo_name"]: (r["claude"], r["copilot"], r["settings"])
                 for r in tables["repos.csv"]}
        self.assertEqual(flags, {"owner/agentic": ("TRUE", "FALSE", "TRUE")})

    def test_an_agentic_workflow_keeps_its_imports_and_takes_their_engine(self):
        """pydantic/pydantic-ai's GitHub Agentic Workflows import their engine from
        `shared/engine-zai.md`. This step keeps the imported files, an empty one included,
        because gh-aw needs them to compile the workflow. It does not delete them as CI in
        `.github`, and the detection step reports none of them as a file of its own in
        other.csv."""
        self._collect("owner/imports", {
            ".github/workflows/review.md": "---\non: pull_request\nimports:\n  - shared/engine.md\n"
                                           "  - shared/empty.md\n---\nReview.\n",
            ".github/workflows/shared/engine.md": "---\nengine:\n  id: claude\n---\n",
            ".github/workflows/shared/empty.md": "",
            ".github/workflows/shared/unused.md": "---\ntools: {}\n---\n",
            ".github/copilot-instructions.md": "# Copilot\n" + "x" * 80})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§imports" / ".github" / "workflows"
        self.assertEqual(sorted(p.relative_to(kept).as_posix() for p in kept.rglob("*") if p.is_file()),
                         ["review.md", "shared/empty.md", "shared/engine.md"])
        tables = self._detect(("repos.csv", "settings.csv", "other.csv"))
        self.assertEqual([(r["settings"], r["tool"]) for r in tables["settings.csv"]],
                         [(".github/workflows/review.md", "claude")])
        self.assertEqual(tables["other.csv"], [])

    def test_an_agentic_workflow_keeps_the_files_that_its_body_loads_when_it_runs(self):
        """shopware/shopware's Claude workflows load a policy into the prompt with
        `{{#runtime-import .github/aw/sw-triage-policy.md}}`, and each policy loads a second
        file the same way. opsmill/infrahub's workflows name `shared/issue-taxonomy.md`, which
        gh-aw reads from `.github/workflows/`. This step keeps these files, which no Copilot
        row names, and the detection step reports none of them in other.csv. A file named in
        an HTML comment is not loaded, and this step deletes it."""
        self._collect("owner/runtime", {
            ".github/workflows/triage.md": "---\non: issues\nengine:\n  id: claude\n---\n# Triage\n\n"
                                           "{{#runtime-import .github/aw/triage-policy.md}}\n\n"
                                           "{{#runtime-import shared/taxonomy.md}}\n"
                                           "<!-- {{#runtime-import .github/aw/old.md}} -->\n",
            ".github/aw/triage-policy.md": "# Policy\n\n"
                                           "{{#runtime-import .github/aw/shared/triage-policy.md}}\n",
            ".github/aw/shared/triage-policy.md": "Label every issue.\n",
            ".github/workflows/shared/taxonomy.md": "The labels.\n",
            ".github/aw/old.md": "Not loaded.\n",
            ".github/copilot-instructions.md": "# Copilot\n" + "x" * 80})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§runtime" / ".github"
        self.assertEqual(sorted(p.relative_to(kept).as_posix() for p in kept.rglob("*") if p.is_file()),
                         ["aw/shared/triage-policy.md", "aw/triage-policy.md",
                          "copilot-instructions.md", "workflows/shared/taxonomy.md",
                          "workflows/triage.md"])
        tables = self._detect(("repos.csv", "settings.csv", "other.csv"))
        self.assertEqual([(r["settings"], r["tool"]) for r in tables["settings.csv"]],
                         [(".github/workflows/triage.md", "claude")])
        self.assertEqual(tables["other.csv"], [])

    def test_a_workflow_that_runs_an_agent_s_command_line_tool_is_kept_and_attributed(self):
        """The workflows run `claude -p` in a shell step, as collected workflow files do, and
        Cursor's documented install line."""
        self._collect("owner/cli", {
            ".github/workflows/fix.yml": "jobs:\n  go:\n    steps:\n      - run: |\n"
                                         "          claude -p \"$(cat prompt.md)\"\n",
            ".github/workflows/review.yml": "jobs:\n  go:\n    steps:\n      - run: |\n"
                                            "          curl https://cursor.com/install -fsS | bash\n",
            ".github/workflows/ci.yml": self.CI})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§cli" / ".github" / "workflows"
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["fix.yml", "review.yml"])
        tables = self._detect()
        flags = {r["repo_name"]: (r["claude"], r["cursor"], r["copilot"], r["settings"])
                 for r in tables["repos.csv"]}
        self.assertEqual(flags, {"owner/cli": ("TRUE", "TRUE", "FALSE", "TRUE")})

    def test_a_workflow_that_only_names_the_action_in_a_comment_is_not_kept(self):
        self._collect("owner/mentions", {
            ".github/workflows/notes.yml": "# see anthropics/claude-code-action\n" + self.CI,
            "CLAUDE.md": "# Claude\n" + "x" * 80})
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.validated_dir / "owner§mentions" / ".github").exists())

    def test_the_prompt_file_of_the_codex_action_is_kept_as_a_part_of_its_workflow(self):
        """The Codex action reads its prompt from the file that `prompt-file` names. The rule
        for `.github` would delete that file, because no Copilot row names it. The detection
        step counts it with the workflow and not as a file of its own."""
        self._collect("owner/codex", {
            ".github/workflows/review.yml": "jobs:\n  codex:\n    steps:\n"
                                            "      - uses: openai/codex-action@v1\n        with:\n"
                                            "          prompt-file: .github/codex/prompts/review.md\n",
            ".github/codex/prompts/review.md": "Review the change.\n",
            ".github/codex/prompts/unused.md": "Not named.\n",
            ".github/copilot-instructions.md": "# Copilot\n" + "x" * 80})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§codex" / ".github" / "codex" / "prompts"
        self.assertEqual(sorted(p.name for p in kept.iterdir()), ["review.md"])
        tables = self._detect(("repos.csv", "settings.csv", "other.csv"))
        self.assertEqual([(r["settings"], r["tool"]) for r in tables["settings.csv"]],
                         [(".github/workflows/review.yml", "codex")])
        self.assertEqual(tables["other.csv"], [])

    def test_the_prompt_file_of_the_claude_code_base_action_is_kept_outside_every_tool_directory(self):
        """The Claude Code Base Action reads its prompt from the file that `prompt_file` names,
        such as facebook/rocksdb's `claude_md/ci_recovery_prompt.md`. The file is in no directory
        of a tool, and the step keeps it because a workflow names it as the prompt file of an agent
        action (`paths.action_prompt_files`)."""
        self._collect("owner/base", {
            ".github/workflows/ai-review-analysis.yml":
                "jobs:\n  recover:\n    steps:\n"
                "      - uses: anthropics/claude-code-base-action@beta\n        with:\n"
                "          prompt_file: claude_md/ci_recovery_prompt.md\n",
            "claude_md/ci_recovery_prompt.md": "Recover the build.\n",
            "CLAUDE.md": "# Claude\n" + "x" * 80})
        self.assertEqual(self._run(), 0)
        self.assertEqual((self.validated_dir / "owner§base" / "claude_md" / "ci_recovery_prompt.md")
                         .read_text(encoding="utf-8"), "Recover the build.\n")
        tables = self._detect(("repos.csv", "settings.csv", "other.csv"))
        self.assertEqual([(r["settings"], r["tool"]) for r in tables["settings.csv"]],
                         [(".github/workflows/ai-review-analysis.yml", "claude")])
        self.assertEqual(tables["other.csv"], [])


class TestANestedGithubReachesTheDetector(_MainCase):
    """The test runs a package's `.github` through this step and then the detection step.
    The `.github` holds the places that Copilot CLI reads where it works, and the repository
    has no `.github` at its root. The rule for `.github` would delete every file of it, and
    the repository with it."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect

    def test_the_places_are_kept_detected_and_attributed_to_copilot(self):
        self._collect("owner/monorepo", {
            "packages/web/.github/copilot-instructions.md": "Use pnpm.\n" + "x" * 80,
            "packages/web/.github/instructions/react.instructions.md":
                "---\napplyTo: '**/*.tsx'\n---\nHooks.\n",
            "packages/web/.github/agents/review.agent.md": "---\ndescription: Review\n---\nx\n",
            "packages/web/.github/agents/notes.md": "No front matter.\n",
            "packages/web/.github/skills/release/SKILL.md":
                "---\nname: release\ndescription: d\n---\nx\n",
            "packages/web/.github/lsp.json":
                '{"lspServers": {"ts": {"command": "x", "fileExtensions": {".ts": "ts"}}}}\n',
            "packages/web/.github/mcp.json": '{"mcpServers": {"gh": {"command": "gh"}}}\n',
            "packages/web/.github/workflows/ci.yml": "jobs: {}\n",
            "packages/web/.github/prompts/fix.prompt.md": "Fix.\n"})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§monorepo" / "packages" / "web" / ".github"
        self.assertEqual(sorted(p.relative_to(kept).as_posix() for p in kept.rglob("*")
                                if p.is_file()),
                         ["agents/notes.md", "agents/review.agent.md", "copilot-instructions.md",
                          "instructions/react.instructions.md", "lsp.json", "mcp.json",
                          "skills/release/SKILL.md"])
        tables = self._detect(("repos.csv", "context-files.csv", "subagents.csv", "skills.csv",
                               "lsp.csv", "mcp.csv", "other.csv"))
        repo, = tables["repos.csv"]
        self.assertEqual((repo["copilot"], repo["copilot-instructions"]), ("TRUE", "TRUE"))
        self.assertEqual(sorted((r["context_file"], r["tool"]) for r in tables["context-files.csv"]),
                         [("packages/web/.github/copilot-instructions.md", "copilot"),
                          ("packages/web/.github/instructions/react.instructions.md", "copilot")])
        self.assertEqual([(r["subagent"], r["tool"]) for r in tables["subagents.csv"]],
                         [("packages/web/.github/agents/review.agent.md", "copilot")])
        self.assertEqual([(r["skills.md"], r["tool"]) for r in tables["skills.csv"]],
                         [("packages/web/.github/skills/release/SKILL.md", "copilot")])
        self.assertEqual([(r["lsp"], r["tool"]) for r in tables["lsp.csv"]],
                         [("packages/web/.github/lsp.json", "copilot")])
        self.assertEqual([(r["mcp"], r["tool"]) for r in tables["mcp.csv"]],
                         [("packages/web/.github/mcp.json", "copilot")])
        self.assertEqual([(r["other"], r["tool"]) for r in tables["other.csv"]],
                         [("packages/web/.github/agents/notes.md", "copilot")])

    def _collect_clone(self, repo_name: str, files: dict, links: dict) -> None:
        """Run the collector over a clone with `files`, `{path: text}`, and `links`,
        `{path: target}`, into the collected tree, and list the repository there."""
        from agentcontext import paths
        clone = self.tmp / "clones" / repo_name.replace("/", "§")
        for rel, text in files.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            (clone / rel).write_text(text, encoding="utf-8")
        for rel, target in links.items():
            (clone / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, clone / rel)
        paths.collect_repo(clone, self.collected_dir / clone.name, set(VENDORED_DIRS))
        self._collect(repo_name)

    def test_a_place_that_links_to_a_file_is_collected_kept_and_detected(self):
        """dotnet/dotnet links `src/msbuild/.github/copilot-instructions.md` to `../AGENTS.md`.
        Copilot CLI reads the instructions through the link, as it does at the root, and the
        collector keeps such a link as its marker below the root too."""
        self._collect_clone("owner/monorepo", {"pkg/AGENTS.md": "Use pnpm.\n" + "x" * 80},
                            {"pkg/.github/copilot-instructions.md": "../AGENTS.md"})
        self.assertEqual(self._run(), 0)
        marker = self.validated_dir / "owner§monorepo" / "pkg/.github/copilot-instructions.md"
        self.assertEqual(marker.read_text(encoding="utf-8"), "../AGENTS.md\n")
        tables = self._detect(("repos.csv", "context-files.csv"))
        repo, = tables["repos.csv"]
        self.assertEqual((repo["copilot"], repo["copilot-instructions"]), ("TRUE", "TRUE"))
        self.assertIn(("pkg/.github/copilot-instructions.md", "copilot"),
                      [(r["context_file"], r["tool"]) for r in tables["context-files.csv"]])

    def test_a_place_that_links_to_a_folder_is_read_as_at_the_root(self):
        """The folder that a place of a folder links to is collected, kept and read, and its
        items are attributed to Copilot, as for the root's `.github/agents` that is a link."""
        rows = {}
        for prefix in ("", "pkg/"):
            name = f"owner/{'root' if not prefix else 'package'}"
            self._collect_clone(name, {
                f"{prefix}shared/agents/review.agent.md": "---\ndescription: Review\n---\nx\n",
                f"{prefix}shared/instructions/react.instructions.md":
                    "---\napplyTo: '**/*.tsx'\n---\nHooks.\n",
                f"{prefix}shared/skills/release/SKILL.md":
                    "---\nname: release\ndescription: d\n---\nx\n",
                f"{prefix}.github/copilot-instructions.md": "Use pnpm.\n" + "x" * 80},
                {f"{prefix}.github/agents": "../shared/agents",
                 f"{prefix}.github/instructions": "../shared/instructions",
                 f"{prefix}.github/skills": "../shared/skills"})
        self.assertEqual(self._run(), 0)
        tables = self._detect(("context-files.csv", "subagents.csv", "skills.csv", "other.csv"))
        for prefix, repo in (("", "owner/root"), ("pkg/", "owner/package")):
            rows[prefix] = sorted(
                (table, row[column].removeprefix(prefix), row["tool"])
                for table, column in (("context-files.csv", "context_file"),
                                      ("subagents.csv", "subagent"), ("skills.csv", "skills.md"),
                                      ("other.csv", "other"))
                for row in tables[table] if row["repo_name"] == repo)
        self.assertEqual(rows["pkg/"], rows[""])
        self.assertIn(("subagents.csv", "shared/agents/review.agent.md", "copilot"), rows["pkg/"])
        self.assertIn(("skills.csv", "shared/skills/release/SKILL.md", "copilot"), rows["pkg/"])
        self.assertIn(("context-files.csv", "shared/instructions/react.instructions.md",
                       "copilot"), rows["pkg/"])

    def test_the_mcp_json_of_a_directory_below_the_root_is_kept_and_detected(self):
        """Copilot CLI reads the `.mcp.json` "in any directory from your working directory up
        to the repository root", in the bare format too. That includes the directory of a
        Claude Code plugin, where Claude Code reads the file as the plugin's MCP servers.
        Treated as a root-only file, a `.mcp.json` below the root would be deleted."""
        self._collect("owner/monorepo", {
            ".mcp.json": '{"mcpServers": {"root": {"command": "root"}}}\n',
            "packages/web/.mcp.json": '{"web": {"command": "web"}}\n',
            "plugins/claude/.claude-plugin/plugin.json": '{"name": "demo"}\n',
            "plugins/claude/.mcp.json": '{"mcpServers": {"demo": {"command": "demo"}}}\n'})
        self.assertEqual(self._run(), 0)
        kept = self.validated_dir / "owner§monorepo"
        self.assertEqual(sorted(p.relative_to(kept).as_posix() for p in kept.rglob(".mcp.json")),
                         [".mcp.json", "packages/web/.mcp.json", "plugins/claude/.mcp.json"])
        tables = self._detect(("repos.csv", "mcp.csv"))
        repo, = tables["repos.csv"]
        self.assertEqual((repo["claude"], repo["copilot"]), ("TRUE", "TRUE"))
        self.assertEqual(sorted((r["mcp"], r["tool"]) for r in tables["mcp.csv"]),
                         [(".mcp.json", "any"), ("packages/web/.mcp.json", "copilot"),
                          ("plugins/claude/.mcp.json", "claude|copilot")])


class TestAPluginReachesTheDetector(_MainCase):
    """The tests run a plugin that the repository ships under a directory of its choosing
    through this step and then the detection step.

    This step keeps a plugin's skills and agents wherever the plugin root is, such as under
    `plugins/<name>/`, and also the manifest directory and the files beside it, so that these
    files reach a mechanism CSV. A Copilot CLI plugin has its manifest under `.github` too.
    The step keeps that manifest below the root, although the rule for `.github` keeps a
    nested `.github` only for the places that Copilot CLI reads where it works."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect
    TABLES = ("repos.csv", "skills.csv", "subagents.csv", "plugins.csv", "hooks.csv")

    def test_the_parts_and_the_manifest_are_kept_detected_and_attributed(self):
        self._collect("owner/marketplace", {
            ".claude-plugin/marketplace.json": '{"plugins": [{"source": "./plugins/review"}]}\n',
            "plugins/review/.claude-plugin/plugin.json": '{"name": "review"}\n',
            "plugins/review/skills/review/SKILL.md": "---\nname: review\n---\nReview.\n",
            "plugins/review/agents/reviewer.md": "---\nname: reviewer\n---\nReview.\n",
            "plugins/review/hooks/hooks.json":
                '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}\n',
            "plugins/review/src/index.ts": "export {};\n"})
        self._collect("owner/copilot-plugin", {
            "plugins/lint/.github/plugin/plugin.json": '{"name": "lint"}\n',
            "plugins/lint/skills/lint/SKILL.md": "---\nname: lint\n---\nLint.\n",
            "plugins/lint/agents/linter.agent.md": "---\nname: linter\n---\nLint.\n"})
        self.assertEqual(self._run(), 0)
        review = self.validated_dir / "owner§marketplace" / "plugins" / "review"
        for rel in ("skills/review/SKILL.md", "agents/reviewer.md", "hooks/hooks.json"):
            self.assertTrue((review / rel).is_file(), f"{rel} was deleted")
        self.assertFalse((review / "src").exists(), "a plugin's source is not configuration")
        self.assertTrue((self.validated_dir / "owner§copilot-plugin" / "plugins" / "lint"
                         / ".github" / "plugin" / "plugin.json").is_file(),
                        "the Copilot CLI manifest was deleted")

        tables = self._detect(self.TABLES)
        flags = {r["repo_name"]: (r["claude"], r["copilot"], r["skills"], r["subagents"],
                                  r["plugins"]) for r in tables["repos.csv"]}
        self.assertEqual(flags, {
            "owner/marketplace": ("TRUE", "FALSE", "TRUE", "TRUE", "TRUE"),
            "owner/copilot-plugin": ("FALSE", "TRUE", "TRUE", "TRUE", "TRUE")})

        def rows(table):
            _, column = MECHANISM_CSVS[table.removesuffix(".csv")]
            return sorted((r["repo_name"], r[column]) for r in tables[table])
        self.assertEqual(rows("skills.csv"),
                         [("owner/copilot-plugin", "plugins/lint/skills/lint/SKILL.md"),
                          ("owner/marketplace", "plugins/review/skills/review/SKILL.md")])
        self.assertEqual(rows("subagents.csv"),
                         [("owner/copilot-plugin", "plugins/lint/agents/linter.agent.md"),
                          ("owner/marketplace", "plugins/review/agents/reviewer.md")])
        self.assertEqual(rows("hooks.csv"),
                         [("owner/marketplace", "plugins/review/hooks/hooks.json")])
        self.assertEqual(rows("plugins.csv"),
                         [("owner/copilot-plugin", "plugins/lint/.github/plugin/plugin.json"),
                          ("owner/marketplace", ".claude-plugin/marketplace.json"),
                          ("owner/marketplace", "plugins/review/.claude-plugin/plugin.json")])

    def test_a_directory_named_skills_beside_no_manifest_is_not_a_plugin_s(self):
        self._collect("owner/app", {
            "CLAUDE.md": "# Claude\n" + "x" * 80,
            "plugins/review/skills/review/SKILL.md": "---\nname: review\n---\nReview.\n",
            "plugins/review/plugin.json": '{"name": "review"}\n'})
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.validated_dir / "owner§app" / "plugins" / "review" / "skills")
                         .exists(), "a plugin.json declaring no schema made a plugin root")


class TestASkillLinkedIntoAVendoredDirectoryReachesTheDetector(_MainCase):
    """A skill that `.claude/skills/<name>` links into a vendored directory, as
    `open-metadata/openmetadata` links its skills into `skills/vendor/`, runs through this step
    and then the detection step. Claude Code reads the skill through the link, and the
    detection step reads its text there. Without the target of the link, the row would have a
    blank language."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect

    def test_the_skill_is_kept_and_its_text_is_read(self):
        self._collect("owner/app", {
            ".claude/skills/composition-patterns": "../../skills/vendor/composition-patterns\n",
            "skills/vendor/composition-patterns/SKILL.md":
                "---\nname: composition-patterns\n---\nPrefer composition over inheritance in "
                "every component that this repository ships.\n"})
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "owner§app" / "skills" / "vendor"
                         / "composition-patterns" / "SKILL.md").is_file())
        rows = self._detect(("skills.csv",))["skills.csv"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["language"], "English")


class TestASkillMdThatIsALinkReachesTheDetector(_MainCase):
    """A skill folder with a SKILL.md that is a link, as feast-dev/feast's
    `.claude/skills/feast-dev/SKILL.md` links to `../../../skills/feast-dev/SKILL.md`, runs
    through this step and then the detection step. Claude Code reads the skill through the
    link. Without the file that the marker names, the row would have a blank language."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect

    def test_the_file_is_kept_and_the_skill_is_read_through_the_marker(self):
        self._collect("owner/app", {
            ".claude/skills/feast-dev/SKILL.md": "../../../skills/feast-dev/SKILL.md\n",
            "skills/feast-dev/SKILL.md":
                "---\nname: feast-dev\n---\nRun the unit tests before every commit that changes "
                "the feature store.\n"})
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "owner§app" / "skills" / "feast-dev"
                         / "SKILL.md").is_file())
        rows = self._detect(("skills.csv",))["skills.csv"]
        self.assertEqual([(r["skills.md"], r["language"]) for r in rows],
                         [(".claude/skills/feast-dev/SKILL.md", "English")])


class TestAFolderLinkedIntoACommandsDirectoryReachesTheDetector(_MainCase):
    """A folder that `.claude/commands/<name>` links to, as GovTechSG/sgds-web-component links
    `.claude/commands/sgds-forms` to `../../skills/sgds-forms`, runs through this step and then
    the detection step. Claude Code reads the folder's SKILL.md as the command named for the
    folder. Without the folder, the link would be an `other` row and the skill would be in no
    CSV."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect

    def test_the_folder_is_kept_and_its_skill_is_claude_s(self):
        self._collect("owner/app", {
            ".claude/commands/sgds-forms": "../../skills/sgds-forms\n",
            "skills/sgds-forms/SKILL.md":
                "---\nname: sgds-forms\n---\nUse the form components of the design system for "
                "every input that this repository renders.\n"})
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "owner§app" / "skills" / "sgds-forms"
                         / "SKILL.md").is_file())
        tables = self._detect(("skills.csv", "other.csv"))
        self.assertEqual([(r["skills.md"], r["name"], r["tool"]) for r in tables["skills.csv"]],
                         [("skills/sgds-forms/SKILL.md", "sgds-forms", "claude")])
        self.assertEqual(tables["other.csv"], [])


class TestAFolderLinkedIntoAClaudeRulesDirectoryReachesTheDetector(_MainCase):
    """A folder that `.claude/rules/<name>` links to, as opendatahub-io/opendatahub-operator links
    `.claude/rules/.rules` to `../../.rules`, runs through this step and then the detection step.
    Claude Code reads the rules below a linked folder of its rules directory. Without the folder,
    the link would be an `other` row and the rules would be in no CSV."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect

    def test_the_folder_is_kept_and_its_rules_are_claude_s(self):
        self._collect("owner/app", {
            ".claude/rules/.rules": "../../.rules\n",
            ".rules/api-types.md": "Keep the API types in api/ and generate their deep copies.\n",
            ".rules/testing.md": "Run the unit tests with the envtest binaries.\n"})
        self.assertEqual(self._run(), 0)
        self.assertTrue((self.validated_dir / "owner§app" / ".rules" / "testing.md").is_file())
        tables = self._detect(("rules.csv", "other.csv"))
        self.assertEqual([(r["rule"], r["name"], r["tool"]) for r in tables["rules.csv"]],
                         [(".rules/api-types.md", "api-types", "claude"),
                          (".rules/testing.md", "testing", "claude")])
        self.assertEqual(tables["other.csv"], [])


class TestPlanningDocumentsReachTheirOwnCsv(_MainCase):
    """The tests run planning documents through this step and then the detection step. This
    step keeps them wherever their layout puts them. They do not keep a repository, because a
    planning document is no configuration artifact. The detection step writes them to their
    own CSV and takes them out of `other.csv`. It sets no flag for them and leaves a file that
    a mechanism counts to that mechanism."""

    _detect = TestAnAgentWorkflowReachesTheDetector._detect
    TABLES = ("repos.csv", "planning-documents.csv", "other.csv", "context-files.csv")
    PLANNING = {".specify/memory/constitution.md": "# Constitution\n",
                "specs/001-photo-albums/spec.md": "# Spec\n\nThe users group photos.\n",
                "specs/001-photo-albums/plan.md": "# Plan\n",
                "openspec/specs/auth/spec.md": "# Auth\n",
                "docs/prd.md": "# Product Requirements Document\n",
                ".claude/plans/fix-auth-race-snug-otter.md": "# Plan\n",
                ".cursor/plans/consolidation_8e9eb00f.plan.md":
                    "---\nname: Consolidation\noverview: Merge the services.\ntodos: []\n---\n"}
    # The folder that BMAD installs, which anchors docs/prd.md and is no planning document.
    ANCHOR = {".bmad-core/core-config.yaml": "prdFile: docs/prd.md\n"}

    def test_the_documents_are_kept_and_published_apart_from_the_configuration(self):
        self._collect("owner/app", {
            "AGENTS.md": "# Agents\n" + "x" * 80,
            "openspec/AGENTS.md": "# OpenSpec instructions\n" + "x" * 80,
            ".claude/plans/defect-register.html": "<html>A report</html>\n",
            ".cursor/plans/notes.md": "# A person's own notes\n",
            "docs/spec.md": "# Not a planning document\n",
            **self.ANCHOR, **self.PLANNING})
        self._collect("owner/planned", {**self.ANCHOR, **self.PLANNING})
        self.assertEqual(self._run(), 0)
        app = self.validated_dir / "owner§app"
        for rel in {**self.ANCHOR, **self.PLANNING}:
            with self.subTest(kept=rel):
                self.assertTrue((app / rel).is_file(), f"{rel} was deleted")
        self.assertFalse((app / "docs" / "spec.md").exists())
        self.assertFalse((self.validated_dir / "owner§planned").exists(),
                         "planning documents alone kept a repository")

        tables = self._detect(self.TABLES)
        self.assertEqual([r["repo_name"] for r in tables["repos.csv"]], ["owner/app"])
        flags = tables["repos.csv"][0]
        self.assertEqual({tool: flags[tool] for tool in ("claude", "cursor", "codex")},
                         {"claude": "FALSE", "cursor": "FALSE", "codex": "FALSE"},
                         "a planning document set a tool flag")
        self.assertEqual(flags["no_specific_tool"], "TRUE")
        rows = tables["planning-documents.csv"]
        self.assertEqual(sorted((r["planning_document"], r["framework"], r["document"])
                                for r in rows), [
            (".claude/plans/fix-auth-race-snug-otter.md", "claude", "plan"),
            (".cursor/plans/consolidation_8e9eb00f.plan.md", "cursor", "plan"),
            (".specify/memory/constitution.md", "spec-kit", "constitution"),
            ("docs/prd.md", "bmad-method", "prd"),
            ("openspec/specs/auth/spec.md", "openspec", "spec"),
            ("specs/001-photo-albums/plan.md", "spec-kit", "plan"),
            ("specs/001-photo-albums/spec.md", "spec-kit", "spec")])
        spec = next(r for r in rows if r["planning_document"].endswith("albums/spec.md"))
        self.assertEqual((spec["repo_name"], spec["head_sha"], spec["line_count"],
                          spec["dataset_version"]),
                         ("owner/app", "a" * 40, "3",
                          importlib.import_module("agentcontext.steps.detection").DATASET_VERSION))
        self.assertEqual(spec["github_link"],
                         "https://github.com/owner/app/tree/main/specs/001-photo-albums/spec.md")
        self.assertEqual(sorted(r["other"] for r in tables["other.csv"]),
                         [".claude/plans/defect-register.html", ".cursor/plans/notes.md"],
                         "a planning document stayed in other.csv, or another file left it")
        self.assertEqual(sorted(r["context_file"] for r in tables["context-files.csv"]),
                         ["AGENTS.md", "openspec/AGENTS.md"],
                         "a context file in a planning folder is a configuration artifact")

    def test_planning_documents_change_no_row_of_repos_csv(self):
        """Two repositories with the same configuration, one of them with planning documents
        outside every configuration directory, have the same flags."""
        context = {"AGENTS.md": "# Agents\n" + "x" * 80}
        self._collect("owner/plain", context)
        self._collect("owner/planner", {**context, **self.ANCHOR,
                                        **{rel: text for rel, text in self.PLANNING.items()
                                           if not rel.startswith(".c")}})
        self.assertEqual(self._run(), 0)
        tables = self._detect(self.TABLES)
        rows = {r["repo_name"]: r for r in tables["repos.csv"]}
        ignored = ("repo_name", "github_link")
        self.assertEqual({c: v for c, v in rows["owner/plain"].items() if c not in ignored},
                         {c: v for c, v in rows["owner/planner"].items() if c not in ignored})
        self.assertEqual({r["repo_name"] for r in tables["planning-documents.csv"]},
                         {"owner/planner"})


class TestALinkLeadingOutsideTheRepositoryIsNotKept(_MainCase):
    """The detection step would read through a real symlink inside a spec directory if this
    step kept it whatever its target. Through a `.claude/skills` link to a directory outside
    the repository, the detection step would report the SKILL.md files there as this
    repository's skills, and a repository with all its configuration elsewhere would stay
    in the validated list."""

    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "elsewhere"
        (self.outside / "s").mkdir(parents=True)
        (self.outside / "s" / "SKILL.md").write_text("# s\n", encoding="utf-8")
        # Beside the link, the second repository has a file that no heuristic explains. Files
        # remain, and only the keep check decides whether the repository stays.
        for name, files in (("owner/linked", {"CLAUDE.md": "# Claude\n"}),
                            ("owner/only-link", {".claude/notes.txt": "scratch\n"})):
            repo = self._collect(name, files)
            (repo / ".claude").mkdir(exist_ok=True)
            (repo / ".claude" / "skills").symlink_to(self.outside, target_is_directory=True)
        self.assertEqual(self._run(), 0)

    def test_the_link_is_removed_and_nothing_behind_it_is_detected(self):
        detect = importlib.import_module("agentcontext.steps.detection")
        repo = self.validated_dir / "owner§linked"
        self.assertFalse(os.path.lexists(repo / ".claude" / "skills"))
        self.assertEqual(detect.detect_skills(repo), [])
        self.assertTrue((self.outside / "s" / "SKILL.md").is_file(), "the target is left alone")
        self.assertTrue((self.collected_dir / "owner§linked" / ".claude" / "skills").is_symlink(),
                        "the collected tree is never modified")

    def test_a_repository_whose_only_configuration_is_elsewhere_is_dropped(self):
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["owner/linked"])


class TestASynologyDirectoryIsNotCopied(_MainCase):
    """Synology DSM writes `@eaDir` into the directories that File Station and its indexing
    service visit, and on the NAS these directories include the collected tree. A keep rule
    keeps a plugin's directory whole, and the `@eaDir` files below it would otherwise be
    published."""

    def setUp(self):
        super().setUp()
        repo = self._collect("owner/repo", {
            "CLAUDE.md": "# Claude\n",
            "@eaDir/CLAUDE.md@SynoEAStream": "attributes\n",
            "plug/.claude-plugin/plugin.json": '{"name": "p"}',
            "plug/.claude-plugin/@eaDir/plugin.json@SynoResource": "resource\n"})
        self.collected_repo = repo
        self.assertEqual(self._run(), 0)

    def test_the_cleanup_does_not_copy_it(self):
        validated = self.validated_dir / "owner§repo"
        self.assertTrue((validated / "CLAUDE.md").is_file())
        self.assertFalse((validated / "@eaDir").exists())
        self.assertFalse((validated / "plug" / ".claude-plugin" / "@eaDir").exists())
        self.assertTrue((self.collected_repo / "@eaDir").is_dir(),
                        "the collected tree is never modified")

    def test_the_deletion_log_has_no_row_for_one_of_the_collected_tree(self):
        """The copy leaves each `@eaDir` out, and the step deletes none of them. The log
        names no file that a file manager wrote into the collected tree."""
        with open(self.deletion_csv, newline="", encoding="utf-8") as f:
            logged = [(row["path"], row["reason"]) for row in csv.DictReader(f)]
        self.assertEqual([path for path, reason in logged if reason == "Synology DSM directory"], [])
        self.assertEqual([path for path, _ in logged if "@eaDir" in path], [])


class TestFilesNestedTooDeeplyToParse(_MainCase):
    """The parser raises RecursionError for a file that is nested too deeply. The step
    catches it where it catches a parse error, because one repository's file would otherwise
    stop the cleanup of the whole sample. Such a file reads as a file that does not parse."""

    def test_the_step_validates_the_rest_and_exits_0(self):
        self.assertRaises(RecursionError, json.loads, DEEP_JSON)
        self._collect("o/deep", deeply_nested_files())
        self._collect("o/b", {"CLAUDE.md": "# b\n"})
        code, _out, err = self._run_io()
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(r["repo_name"] for r in self._validated_rows()),
                         ["o/b", "o/deep"])
        deep = self.validated_dir / "o§deep"
        self.assertTrue((deep / "CLAUDE.md").is_file())
        self.assertTrue((deep / ".claude" / "settings.json").is_file(),
                        "a file that does not parse is not empty")
        self.assertFalse((deep / "plugin.json").exists(),
                         "a plugin.json that does not parse is no Agent Plugins document")


class TestFilesWithIntegersTooLongToParse(_MainCase):
    """For a JSON file with an integer literal longer than Python converts, the parser raises
    a ValueError that is no JSONDecodeError. The step catches it where it catches a parse
    error, because one repository's file would otherwise stop the cleanup of the whole sample
    at every Start. Such a file reads as a file that does not parse."""

    def test_the_step_validates_the_rest_and_exits_0(self):
        with self.assertRaises(ValueError) as caught:
            json.loads(LONG_INTEGER_JSON)
        self.assertNotIsInstance(caught.exception, json.JSONDecodeError)
        self._collect("o/long", files_with_long_integers())
        self._collect("o/b", {"CLAUDE.md": "# b\n"})
        code, _out, err = self._run_io()
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(r["repo_name"] for r in self._validated_rows()),
                         ["o/b", "o/long"])
        long = self.validated_dir / "o§long"
        self.assertTrue((long / "CLAUDE.md").is_file())
        self.assertTrue((long / ".claude" / "settings.json").is_file(),
                        "a file that does not parse is not empty")
        self.assertTrue((long / ".codex" / "config.toml").is_file())
        self.assertFalse((long / "plugin.json").exists(),
                         "a plugin.json that does not parse is no Agent Plugins document")


class TestDsmWritingDuringARemoval(_MainCase):
    """DSM writes an `@eaDir` into a directory of a shared folder at any time. One can
    appear in a directory that the step is removing, after `shutil.rmtree` listed it. The
    removal would then raise ENOTEMPTY, and the step would exit 1 after the collection."""

    def setUp(self):
        super().setUp()
        self._collect("owner/kept", {"CLAUDE.md": "# kept\n"})

    def test_the_validated_tree_of_an_earlier_run_is_removed(self):
        earlier = self.validated_dir / "owner§earlier"
        earlier.mkdir(parents=True)
        (earlier / "CLAUDE.md").write_text("# earlier\n", encoding="utf-8")
        with dsm_writes_during_removal("CLAUDE.md") as written:
            self.assertEqual(self._run(), 0)
        self.assertEqual(len(written), 1)
        self.assertFalse(earlier.exists())
        self.assertTrue((self.validated_dir / "owner§kept" / "CLAUDE.md").is_file())

    def test_a_repository_without_a_detected_mechanism_is_removed(self):
        self._collect("owner/other-only", {".claude/notes.txt": "scratch"})
        with dsm_writes_during_removal("notes.txt") as written:
            self.assertEqual(self._run(), 0)
        self.assertEqual(len(written), 1)
        self.assertFalse((self.validated_dir / "owner§other-only").exists())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["owner/kept"])

    def test_an_eadir_that_dsm_writes_into_while_the_step_removes_it_is_removed(self):
        """DSM writes an `@eaDir` into the validated repository right after the copy creates
        it, and writes into that `@eaDir` again while the Synology pass removes it."""
        with dsm_writes_into_new_directories("owner§kept", self.validated_dir,
                                             entry="@eaDir/CLAUDE.md@SynoEAStream") as indexed, \
                dsm_writes_during_removal("CLAUDE.md@SynoEAStream",
                                          entry="SKILL.md@SynoEAStream") as written:
            self.assertEqual(self._run(), 0)
        self.assertEqual(len(indexed), 1)
        self.assertEqual(len(written), 1)
        self.assertFalse((self.validated_dir / "owner§kept" / "@eaDir").exists())
        self.assertTrue((self.validated_dir / "owner§kept" / "CLAUDE.md").is_file())
        self.assertIn("Synology DSM directory", self._deletion_reasons().values())


class TestDsmWritingDuringTheCopy(_MainCase):
    """DSM writes into the collected tree and into the validated tree while the step copies
    one into the other. A copy that also copied each `@eaDir` would end in `shutil.Error`, and
    the step would exit 1 after the collection. That happens when DSM removes a file of an
    `@eaDir` between the listing and the copy. It also happens when DSM writes an `@eaDir`
    into a directory of the copy before the copy reaches the `@eaDir` of the collected
    directory."""

    def setUp(self):
        super().setUp()
        self.repo = self._collect("owner/repo", {
            "CLAUDE.md": "# Claude\n",
            ".claude/settings.json": '{"model": "opus"}\n',
            ".claude/@eaDir/settings.json@SynoEAStream": "attributes\n",
            ".claude/@eaDir/CLAUDE.md@SynoResource": "resource\n"})

    def _assert_validated_without_eadir(self):
        validated = self.validated_dir / "owner§repo"
        self.assertTrue((validated / ".claude" / "settings.json").is_file())
        self.assertEqual([p for p in validated.rglob("*") if "@eaDir" in p.parts], [])

    def test_dsm_removing_the_files_of_an_eadir_after_the_listing(self):
        with dsm_removes_files_after_listing(self.collected_dir):
            self.assertEqual(self._run(), 0)
        self._assert_validated_without_eadir()

    def test_dsm_writing_an_eadir_into_a_directory_of_the_copy_first(self):
        with dsm_writes_into_new_directories(".claude", self.validated_dir) as written:
            self.assertEqual(self._run(), 0)
        self.assertEqual(len(written), 1, "DSM wrote into the copy")
        self._assert_validated_without_eadir()


class TestADirectoryNestedDeeperThanTheRecursionLimit(_MainCase):
    """rsync copies a configuration directory of a clone whatever its depth, and `copytree`
    calls itself once per level. A directory nested deeper than the recursion limit would
    raise RecursionError, and the step would exit 1 at every Start. The test runs the step in
    a subprocess with a lower recursion limit, so that the tree stays within the path length
    of macOS."""

    DEPTH, LIMIT = 150, 200

    def test_the_step_copies_and_keeps_the_deep_file(self):
        deep = ".claude/" + "/".join(["a"] * self.DEPTH) + "/notes.md"
        self._collect("owner/deep", {"CLAUDE.md": "# Claude\n", deep: "deep\n"})
        code = ("import sys\n"
                "from agentcontext.steps import collected_file_cleanup\n"
                f"sys.setrecursionlimit({self.LIMIT})\n"
                "sys.exit(collected_file_cleanup.main(sys.argv[1:]))\n")
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(
            filter(None, [str(root), os.environ.get("PYTHONPATH")]))}
        proc = subprocess.run([sys.executable, "-c", code, *self._args()], cwd=self.tmp,
                              env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual((self.validated_dir / "owner§deep" / deep).read_text(), "deep\n")
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["owner/deep"])


class TestMainRepoSelection(_MainCase):

    def test_repo_with_no_valid_files_is_removed(self):
        self._collect("o/keep", {"CLAUDE.md": "# hi\n"})
        self._collect("o/drop", {"README.md": "# readme\n"})
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.validated_dir / "o§drop").exists())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/keep"])
        self.assertEqual(self._deletion_reasons()["o§drop"],
                         "no detected mechanism, repository directory removed")

    def test_a_root_mcp_json_keeps_the_repo(self):
        # A root .mcp.json sets no tool flag, but a declared MCP server belongs to one of
        # the ten mechanisms, and the repository therefore stays.
        self._collect("o/mcp", {".mcp.json": '{"mcpServers": {"a": {"command": "uvx"}}}'})
        self._collect("o/keep", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertTrue((self.validated_dir / "o§mcp" / ".mcp.json").exists())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()],
                         ["o/keep", "o/mcp"])

    def test_a_repo_whose_files_match_no_rule_is_removed(self):
        # is_valid_file keeps the file because it is inside a spec directory, but none of
        # the ten mechanisms matches it. The file is "other", and the repository has no
        # configuration that this dataset describes.
        self._collect("o/other", {".claude/notes.txt": "scratch"})
        self._collect("o/keep", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertFalse((self.validated_dir / "o§other").exists())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/keep"])

    def test_nothing_is_ever_moved_to_the_excluded_dir(self):
        # A repository is kept or deleted, and there is no category in between.
        self._collect("o/mcp", {".mcp.json": '{"mcpServers": {"a": {"command": "uvx"}}}'})
        self._collect("o/other", {".claude/notes.txt": "scratch"})
        self._run()
        self.assertEqual(list(self.excluded_dir.iterdir()), [])

    def test_spec_dir_alone_keeps_the_repo(self):
        self._collect("o/a", {".claude/settings.json": '{"a": 1}'})
        self._run()
        self.assertTrue((self.validated_dir / "o§a" / ".claude" / "settings.json").exists())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a"])

    def test_plans_md_without_codex_or_agents_is_deleted(self):
        self._collect("o/a", {"PLANS.md": "# plans\n", "CLAUDE.md": "# hi\n"})
        self._run()
        self.assertFalse((self.validated_dir / "o§a" / "PLANS.md").exists())
        self.assertTrue((self.validated_dir / "o§a" / "CLAUDE.md").exists())
        self.assertEqual(self._deletion_reasons()["PLANS.md"],
                         "PLANS.md without Codex configuration or AGENTS.md")

    def test_plans_md_with_agents_md_is_kept(self):
        self._collect("o/a", {"PLANS.md": "# plans\n", "AGENTS.md": "# agents\n"})
        self._run()
        self.assertTrue((self.validated_dir / "o§a" / "PLANS.md").exists())

    def test_plans_md_with_codex_dir_is_kept(self):
        self._collect("o/a", {".codex/config.toml": 'model = "x"\n',
                              "PLANS.md": "# plans\n"})
        self._run()
        self.assertTrue((self.validated_dir / "o§a" / "PLANS.md").exists())

    def test_plans_md_alone_drops_the_repo(self):
        self._collect("o/plans", {"PLANS.md": "# plans\n"})
        self._collect("o/keep", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertFalse((self.validated_dir / "o§plans").exists())
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/keep"])

    def test_output_rows_are_sorted_by_repo_name(self):
        for name in ("o/c", "o/a", "o/b"):
            self._collect(name, {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual([r["repo_name"] for r in self._validated_rows()],
                         ["o/a", "o/b", "o/c"])

    def test_metadata_is_copied_from_the_collected_csv(self):
        """The test checks all five columns. This step clones nothing and reads no commit
        history, and it can only copy them. The detection step would publish a column that
        this step drops as blank."""
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        row = self._validated_rows()[0]
        self.assertEqual(row["scanned_at"], "2026-03-29")
        self.assertEqual(row["head_commit_date"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["branch"], "main")
        self.assertEqual(row["head_sha"], "a" * 40)

    def test_output_csv_has_only_the_metadata_columns(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual(list(self._validated_rows()[0]), META)

    def test_a_directory_absent_from_the_list_is_not_validated(self):
        # A repository that left the input list keeps the tree that an earlier collection
        # wrote, and the list no longer names it. If this step validated the tree, every
        # metadata column would be blank, and the detection step would publish the repository
        # with no head_sha.
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._collect("o/extra", {"CLAUDE.md": "# hi\n"}, in_csv=False)
        code, _out, err = self._run_io()
        self.assertEqual(code, 0)
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a"])
        self.assertFalse((self.validated_dir / "o§extra").exists())
        self.assertTrue((self.collected_dir / "o§extra" / "CLAUDE.md").is_file())
        self.assertIn("Did not copy 1 repository directories in", err)
        self.assertIn("o§extra", err)

    def test_head_sha_survives_a_repo_losing_some_files(self):
        # Validation only deletes files. The SHA still names the commit that the remaining
        # files came from, and the step must not clear it with the dropped file.
        self._collect("o/a", {"CLAUDE.md": "# hi\n", ".DS_Store": "junk"})
        self._run()
        row = self._validated_rows()[0]
        self.assertEqual(row["head_sha"], "a" * 40)

    def test_distinct_head_shas_are_not_crossed_between_repos(self):
        self._collect("o/a", {"CLAUDE.md": "# a\n"})
        self._collect("o/b", {"CLAUDE.md": "# b\n"})
        # Rewrite the collected CSV with a SHA of its own for each repository.
        with open(self.collected_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(META)
            w.writerow(["o/a", "2026-03-29", "main", "a" * 40, "2026-01-01T00:00:00Z"])
            w.writerow(["o/b", "2026-03-29", "main", "b" * 40, "2026-01-01T00:00:00Z"])
        self._run()
        rows = {r["repo_name"]: r for r in self._validated_rows()}
        self.assertEqual(rows["o/a"]["head_sha"], "a" * 40)
        self.assertEqual(rows["o/b"]["head_sha"], "b" * 40)

    def test_a_collected_list_naming_no_metadata_column_is_refused(self):
        """The filter that derives the validated header keeps the columns that it
        recognizes. For a header that names none of them, a fallback to the full set would
        validate a truncated list into a list of names with every other column blank. The
        detection step would accept that list and publish it with no branch, head_sha or
        github_link. `repository_cloning` writes the collected list, and a killed cloning run
        leaves such a list."""
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        # The check exists for a header with `repo_name` alone. `repo_name` is in
        # METADATA_COLUMNS itself, and an intersection that counted it would accept a list
        # with no metadata.
        for content in ("", "a,b\n1,2\n", "repo_name\no/a\n"):
            with self.subTest(content=content):
                self.collected_csv.write_text(content, encoding="utf-8")
                code, out, err = self._run_io()
                self.assertEqual(code, 1, out)
                self.assertIn("metadata column", err)

    def test_a_source_predating_a_column_is_still_validated(self):
        """The check does not require the whole set. `detection` refuses a validated list
        that lacks a column, and this step accepts it on purpose."""
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self.collected_csv.write_text("repo_name,scanned_at,branch\no/a,2026-03-29,main\n",
                                      encoding="utf-8")
        self.assertEqual(self._run(), 0)

    def test_collected_csv_without_head_sha_does_not_grow_the_column(self):
        # A list from a cloning run that predates the column must still validate. The
        # output follows the columns of the collected list rather than adding a blank one.
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        with open(self.collected_csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["repo_name", "scanned_at", "branch"])
            w.writerow(["o/a", "2026-03-29", "main"])
        self.assertEqual(self._run(), 0)
        self.assertEqual(list(self._validated_rows()[0]),
                         ["repo_name", "scanned_at", "branch"])

    def test_csv_row_without_a_directory_is_dropped(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._csv_rows.append(["o/ghost", "2026-03-29", "main", "c" * 40,
                               "2026-01-01T00:00:00Z"])
        self._collect("o/b", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual([r["repo_name"] for r in self._validated_rows()], ["o/a", "o/b"])

    def test_deletion_csv_has_a_header(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n", ".DS_Store": "junk"})
        self._run()
        with self.deletion_csv.open(newline="", encoding="utf-8") as f:
            self.assertEqual(next(csv.reader(f)), ["path", "reason"])

    def test_the_excluded_dir_is_created(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertTrue(self.excluded_dir.is_dir())

    def test_rerun_is_idempotent(self):
        self._collect("o/a", {"CLAUDE.md": "# hi\n", ".DS_Store": "junk"})
        self._collect("o/drop", {"README.md": "# readme\n"})
        self._run()
        first_input = self.validated_csv.read_text()
        first_deletions = self.deletion_csv.read_text()
        self._run()
        self.assertEqual(self.validated_csv.read_text(), first_input)
        self.assertEqual(self.deletion_csv.read_text(), first_deletions)


class TestASubmoduleIsAnotherRepositorysFiles(unittest.TestCase):
    """A submodule is a reference to another repository. The tree contains a gitlink, and
    the files belong to the repository that the gitlink names. The cloning step checks these
    files out and copies them as any other file. They can therefore duplicate a repository
    that the sample already contains under its own name."""

    REPO = os.path.join(os.sep, "repo")

    def _valid(self, rel, submodules=()):
        return is_valid_file(os.path.join(self.REPO, *rel.split("/")), self.REPO,
                             frozenset(), keep_github=True, marker_targets=(),
                             submodule_paths=submodules)

    def test_a_file_inside_a_recorded_submodule_is_dropped(self):
        self.assertFalse(self._valid("lib/vscode/.claude/CLAUDE.md", ("lib/vscode",)))
        self.assertFalse(self._valid("lib/vscode/.claude/skills/x/SKILL.md", ("lib/vscode",)))

    def test_the_same_file_is_kept_when_no_submodule_is_recorded(self):
        self.assertTrue(self._valid("lib/vscode/.claude/CLAUDE.md"))

    def test_a_sibling_directory_with_the_same_prefix_is_not_inside_it(self):
        self.assertTrue(self._valid("lib/vscode-extra/.claude/CLAUDE.md", ("lib/vscode",)))

    def test_the_repositorys_own_configuration_is_untouched(self):
        self.assertTrue(self._valid(".claude/settings.json", ("lib/vscode",)))


class TestSubmoduleDeletionReason(unittest.TestCase):
    def test_reason_names_the_directory_containing_the_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            file = root / "lib/foo-extra/CLAUDE.md"
            file.parent.mkdir(parents=True)
            file.write_text("# Instructions\n")
            deletions = []
            _mod.scan_and_clean_repo(str(root), deletions, ("lib/foo", "lib/foo-extra"))
            self.assertFalse(file.exists())
            self.assertTrue(any(row[1] == "inside submodule (lib/foo-extra)"
                                for row in deletions))


class TestARecordedSubmoduleIsDroppedByTheRun(_MainCase):
    """The tests above check the predicate alone. `main` reads `submodules.csv` and passes
    each repository its own paths. A run that read nothing, or passed every repository an
    empty tuple, would pass every test above while the files of submodules stayed in the
    validated tree."""

    NESTED = "lib/vscode/.claude/CLAUDE.md"

    def setUp(self):
        super().setUp()
        (self.tmp / "submodules.csv").write_text(
            "repo_name,path,submodule_repo\nowner/mono,lib/vscode,microsoft/vscode\n",
            encoding="utf-8")
        self._collect("owner/mono", {"CLAUDE.md": "# mono\n", self.NESTED: "# vscode\n"})
        # The same path in a repository that records no submodule is its own file.
        self._collect("owner/plain", {self.NESTED: "# plain\n"})
        self.code, self.out, _err = self._run_io()

    def test_the_file_inside_the_submodule_is_deleted_with_its_reason(self):
        self.assertEqual(self.code, 0)
        self.assertTrue((self.validated_dir / "owner§mono" / "CLAUDE.md").is_file())
        self.assertFalse((self.validated_dir / "owner§mono" / "lib").exists())
        with self.deletion_csv.open(newline="", encoding="utf-8") as f:
            reasons = {row["path"].replace(os.sep, "/").split("owner§mono/", 1)[-1]: row["reason"]
                       for row in csv.DictReader(f) if "owner§mono" in row["path"]}
        self.assertEqual(reasons.get(self.NESTED), "inside submodule (lib/vscode)")

    def test_a_repository_that_records_no_submodule_keeps_the_same_path(self):
        self.assertTrue((self.validated_dir / "owner§plain" / self.NESTED).is_file())

    def test_the_run_logs_how_many_paths_it_read(self):
        self.assertIn("1 submodule path(s) in 1 repositories", self.out)


class TestTheHeuristicPathsSurviveValidation(unittest.TestCase):
    """Every configuration path that the collector copies, such as .github/mcp.json and
    .vscode/mcp.json, has to remain after validation. Otherwise this step deletes the
    collector's copies before the detection step reads them. The test lists the paths rather
    than reading them from the heuristics. A row that the validation stops loading then
    fails this test instead of making it shorter."""

    REPO = os.path.join(os.sep, "repo")
    KEPT = [
        ".github/copilot/settings.json", ".github/copilot/settings.local.json",
        ".github/mcp.json", ".github/workflows/copilot-setup-steps.yml",
        ".github/chatmodes/review.chatmode.md", ".github/prompts/fix.prompt.md",
        ".github/allowed_models.txt", ".github/workflows/copilot-code-review.yml",
        ".vscode/mcp.json", ".cursorindexingignore", "CLAUDE.local.md",
        ".agents/skills/demo/SKILL.md", ".agents/notes.md",
        ".cursor-plugin/plugin.json", ".codex-plugin/plugin.json",
        ".github/lsp.json", ".github/plugin/plugin.json", ".github/plugin/marketplace.json",
        ".plugin/plugin.json", ".plugin/marketplace.json", "marketplace.json",
        ".agent/workflows/deploy.md", ".agent/rules/style.md", ".agent/skills/s/SKILL.md",
        ".agents/rules.json", ".agents/mcp_config.json", ".agents/hooks.json", ".claude/AGENTS.md",
    ]

    def _valid(self, rel):
        return is_valid_file(os.path.join(self.REPO, *rel.split("/")), self.REPO,
                             frozenset(), keep_github=True)

    def test_each_heuristics_path_is_kept(self):
        for rel in self.KEPT:
            with self.subTest(rel=rel):
                self.assertTrue(self._valid(rel))

    def test_each_new_configuration_path_survives_cleaning_on_its_own(self):
        for relative in (".github/lsp.json", ".github/plugin/marketplace.json",
                         ".plugin/marketplace.json", ".agent/workflows/deploy.md",
                         ".agent/rules/style.md", ".agent/skills/s/SKILL.md"):
            with self.subTest(path=relative), tempfile.TemporaryDirectory() as temporary:
                tree = Path(temporary)
                path = tree / relative
                path.parent.mkdir(parents=True)
                path.write_text('{"name":"example"}\n' if relative.endswith('.json')
                                else '# Instructions\n')
                deletions = []
                scan_and_clean_repo(str(tree), deletions)
                self.assertTrue(path.is_file(), deletions)

    def test_the_setup_steps_entry_keeps_one_workflow_not_the_directory(self):
        self.assertFalse(self._valid(".github/workflows/ci.yml"))


class TestAMarkerTargetUnderGithubIsKept(unittest.TestCase):
    """The step keeps the target of a symlinked mechanism directory under .github even where
    the .github rule would delete it. The step therefore has to check the marker target rule
    first. Checked after the .github branch, the target would be dropped with the mechanism
    that it contains."""

    REPO = os.path.join(os.sep, "repo")

    def test_a_file_the_github_rule_rejects_is_kept_as_a_marker_target(self):
        path = os.path.join(self.REPO, ".github", "shared", "style.md")
        for keep_github in (True, False):
            with self.subTest(keep_github=keep_github):
                self.assertTrue(is_valid_file(path, self.REPO, frozenset(), keep_github,
                                              marker_targets={".github/shared"}))

    def test_without_the_marker_the_same_file_is_dropped(self):
        path = os.path.join(self.REPO, ".github", "shared", "style.md")
        self.assertFalse(is_valid_file(path, self.REPO, frozenset(), True))


class TestTheGithubKeepListMatchesWholeSegments(unittest.TestCase):
    """is_relevant_github_file decides which files remain after validation, and with them
    which files the published archive contains. A prefix match rather than a segment match
    would keep every .github directory with a name that begins with a keep name. Such
    directories hold unrelated repository content, such as CI definitions, archived material
    or a backup."""

    KEPT = [".github/instructions/x.md", ".github/prompts/y.prompt.md",
            ".github/agents/z.md", ".github/copilot-instructions.md"]
    NOT_KEPT = [".github/instructions-archive/x.md", ".github/agents-backup/z.md",
                ".github/promptsmith/y.md", ".github/instructionsold/x.md"]

    def _check(self, rel):
        # The helper takes an absolute path and the repository root that contains it.
        return is_relevant_github_file(os.path.join("/repo", rel), "/repo")

    def test_the_keep_directories_are_kept(self):
        for rel in self.KEPT:
            self.assertTrue(self._check(rel), rel)

    def test_a_directory_that_merely_starts_with_a_keep_name_is_not(self):
        for rel in self.NOT_KEPT:
            self.assertFalse(self._check(rel), rel)

    def test_the_keep_directory_itself_without_a_child_is_not_a_file(self):
        self.assertFalse(self._check(".github/instructionsx"))

    def test_case_is_ignored_for_the_keep_directories(self):
        self.assertTrue(self._check(".github/Instructions/x.md"))


# ---------------------------------------------------------------------------
# find_marker_targets  (what a symlinked mechanism directory points at)
# ---------------------------------------------------------------------------

class TestMarkerTargets(unittest.TestCase):
    """A copied symlink target remains after validation wherever it is."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "o§a"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, rel: str, content: str = "x\n") -> Path:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _keep(self, rel: str) -> bool:
        return is_valid_file(str(self.repo / rel), str(self.repo), set(), False,
                             find_marker_targets(str(self.repo)))

    def _marker_files(self) -> dict:
        """`find_marker_files`, with each marker repository-relative."""
        return {Path(marker).relative_to(self.repo).as_posix(): targets
                for marker, targets in find_marker_files(str(self.repo)).items()}

    def _left_after_cleanup(self) -> set:
        """The files of the repository after `scan_and_clean_repo`, repository-relative."""
        _mod.scan_and_clean_repo(str(self.repo), [])
        return {p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*") if p.is_file()}

    def test_target_outside_a_spec_dir_is_kept(self):
        # The cloning step copies the target, and deleting it here would undo the copy.
        self._file(".claude/skills", "../skills\n")
        self._file("skills/pdf/SKILL.md")
        self.assertEqual(find_marker_targets(str(self.repo)), {"skills"})
        self.assertTrue(self._keep("skills/pdf/SKILL.md"))

    def test_an_unrelated_directory_is_still_dropped(self):
        self._file(".claude/skills", "../skills\n")
        self._file("skills/pdf/SKILL.md")
        self._file("docs/notes.md")
        self.assertFalse(self._keep("docs/notes.md"))

    def test_a_target_outside_the_repository_is_not_a_target(self):
        self._file(".claude/skills", "../../elsewhere\n")
        self.assertEqual(find_marker_targets(str(self.repo)), set())

    def test_a_real_directory_is_not_a_marker(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self.assertEqual(find_marker_targets(str(self.repo)), set())

    def test_a_marker_whose_target_is_missing_is_ignored(self):
        self._file(".claude/skills", "../gone\n")
        self.assertEqual(find_marker_targets(str(self.repo)), set())

    def test_a_skill_linked_on_its_own_keeps_the_directory_it_names(self):
        # `isaac-sim/IsaacLab` links `.claude/skills/<name>` into a directory of its own, one
        # skill at a time. The collector copies the target, and this step keeps its
        # SKILL.md rather than deleting it as a skill outside a skills directory.
        self._file(".claude/skills/isaaclab", "../../tools/skills/isaaclab\n")
        self._file("tools/skills/isaaclab/SKILL.md")
        self._file("tools/skills/isaaclab/scripts/run.sh")
        self._file("tools/notes.md")
        self.assertEqual(find_marker_targets(str(self.repo)), {"tools/skills/isaaclab"})
        self.assertTrue(self._keep("tools/skills/isaaclab/SKILL.md"))
        self.assertTrue(self._keep("tools/skills/isaaclab/scripts/run.sh"))
        self.assertFalse(self._keep("tools/notes.md"))

    def test_a_folder_linked_into_a_commands_directory_keeps_the_directory_it_names(self):
        # GovTechSG/sgds-web-component links `.claude/commands/sgds-forms` to
        # `../../skills/sgds-forms`. Claude Code reads every `.md` below the link as a
        # command, and the collector copies the target.
        self._file(".claude/commands/sgds-forms", "../../skills/sgds-forms\n")
        self._file("skills/sgds-forms/SKILL.md")
        self._file("skills/sgds-forms/reference/input.md")
        self._file("skills/other/SKILL.md")
        self.assertEqual(find_marker_targets(str(self.repo)), {"skills/sgds-forms"})
        self.assertTrue(self._keep("skills/sgds-forms/SKILL.md"))
        self.assertTrue(self._keep("skills/sgds-forms/reference/input.md"))
        self.assertFalse(self._keep("skills/other/SKILL.md"))

    def test_a_folder_linked_into_a_claude_rules_directory_keeps_the_directory_it_names(self):
        # opendatahub-io/opendatahub-operator links `.claude/rules/.rules` to `../../.rules`.
        # Claude Code reads the rules below the link, and the collector copies the target.
        self._file(".claude/rules/.rules", "../../.rules\n")
        self._file(".rules/testing.md")
        self._file("docs/notes.md")
        self.assertEqual(find_marker_targets(str(self.repo)), {".rules"})
        self.assertTrue(self._keep(".rules/testing.md"))
        self.assertFalse(self._keep("docs/notes.md"))

    def test_a_folder_without_markdown_linked_into_a_commands_directory_is_not_kept(self):
        self._file(".claude/commands/tools", "../../tools\n")
        self._file("tools/run.sh")
        self.assertEqual(find_marker_targets(str(self.repo)), set())

    def test_a_skill_md_that_is_a_marker_keeps_the_file_it_names(self):
        # feast-dev/feast links `.claude/skills/feast-dev/SKILL.md` to
        # `../../../skills/feast-dev/SKILL.md`. The collector copies that file, and this step
        # keeps it rather than deleting it as a SKILL.md outside a skills directory.
        self._file(".claude/skills/feast-dev/SKILL.md", "../../../skills/feast-dev/SKILL.md\n")
        self._file("skills/feast-dev/SKILL.md", "---\nname: feast-dev\n---\n")
        self._file("skills/feast-dev/notes.md")
        self.assertEqual(self._marker_files(),
                         {".claude/skills/feast-dev/SKILL.md": ["skills/feast-dev/SKILL.md"]})
        left = self._left_after_cleanup()
        self.assertIn("skills/feast-dev/SKILL.md", left)
        self.assertIn(".claude/skills/feast-dev/SKILL.md", left)
        self.assertNotIn("skills/feast-dev/notes.md", left)

    def test_a_flat_skill_of_opencode_that_is_a_marker_keeps_the_file_it_names(self):
        # OpenCode 2 reads a Markdown file directly in `.opencode/skills` as a skill, also through
        # a link, and the detection step reads the description in the file that the marker names.
        self._file(".opencode/skills/release.md", "../../docs/release.md\n")
        self._file("docs/release.md", "---\ndescription: Publish a release.\n---\n")
        self._file(".claude/skills/release.md", "../../docs/claude.md\n")
        self._file("docs/claude.md", "---\ndescription: Publish a release.\n---\n")
        self.assertEqual(self._marker_files(),
                         {".opencode/skills/release.md": ["docs/release.md"]})
        left = self._left_after_cleanup()
        self.assertIn("docs/release.md", left)
        self.assertNotIn("docs/claude.md", left)

    def test_a_skill_linked_into_a_vendored_directory_is_kept(self):
        # `open-metadata/openmetadata` links `.claude/skills/<name>` into `skills/vendor/`, one
        # skill at a time. Claude Code reads each skill through its link, and the collector
        # copies the target. Below the target, a vendored directory is still deleted, and so
        # is a skill under `skills/vendor/` that no link names.
        self._file(".claude/skills/composition-patterns",
                   "../../skills/vendor/composition-patterns\n")
        self._file("skills/vendor/composition-patterns/SKILL.md")
        self._file("skills/vendor/composition-patterns/rules/props.md")
        self._file("skills/vendor/composition-patterns/node_modules/pkg/index.js")
        self._file("skills/vendor/unlinked/SKILL.md")
        deletions = []
        _mod.scan_and_clean_repo(str(self.repo), deletions)
        left = {p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*") if p.is_file()}
        self.assertEqual(left, {".claude/skills/composition-patterns",
                                "skills/vendor/composition-patterns/SKILL.md",
                                "skills/vendor/composition-patterns/rules/props.md"})
        # The log's paths are relative to the working directory and end with the path inside
        # the repository.
        reasons = {path.split("o§a/", 1)[1]: reason for path, reason in deletions}
        self.assertEqual(reasons, {
            "skills/vendor/composition-patterns/node_modules/pkg/index.js":
                "inside vendored directory (node_modules)",
            "skills/vendor/composition-patterns/node_modules": "empty directory after cleanup",
            "skills/vendor/unlinked/SKILL.md": "inside vendored directory (vendor)",
            "skills/vendor/unlinked": "empty directory after cleanup"})

    def test_a_linked_skill_in_a_nested_skills_directory_is_kept(self):
        self._file("packages/web/.agents/skills/lint", "../../../../shared/lint\n")
        self._file("shared/lint/SKILL.md")
        self.assertEqual(find_marker_targets(str(self.repo)), {"shared/lint"})

    def test_a_linked_directory_without_a_skill_keeps_nothing(self):
        # The March collected tree contains `open-metadata/openmetadata`'s linked skills
        # without their SKILL.md. If this step kept such a directory, the detection step would
        # report nothing of it, and the archive would contain files that no row names.
        self._file(".claude/skills/connector-building", "../../skills/connector-building\n")
        self._file("skills/connector-building/GUIDE.md")
        self.assertEqual(find_marker_targets(str(self.repo)), set())
        self.assertFalse(self._keep("skills/connector-building/GUIDE.md"))

    def test_a_file_in_a_skills_directory_that_names_no_directory_keeps_nothing(self):
        self._file(".claude/skills/README.md", "# Skills\n\nOne per directory.\n")
        self._file(".claude/skills/pointer", "../../docs/guide.md\n")
        self._file("docs/guide.md")
        self.assertEqual(find_marker_targets(str(self.repo)), set())

    def test_the_file_that_a_linked_rule_or_agent_names_is_kept(self):
        """The collector copies the file behind a rule or an agent that is a link, and the
        detection step reads the item's front matter in it. If this step deleted that file as
        a file outside every spec directory, the item would fail its test."""
        self._file(".agents/rules/team.md", "../../docs/rules/team.md\n")
        self._file("docs/rules/team.md", "---\ntrigger: always_on\n---\nx\n")
        self._file(".claude/agents/reviewer.md", "../../docs/agents/reviewer.md\n")
        self._file("docs/agents/reviewer.md", "---\nname: r\ndescription: d\n---\nx\n")
        self._file(".agents/plugins/p/plugin.json", '{"name": "p"}\n')
        self._file(".agents/plugins/p/agents/x.md", "../../../../shared/x.md\n")
        self._file("shared/x.md", "---\nname: x\ndescription: d\n---\nx\n")
        self._file("docs/notes.md")
        self.assertEqual(self._marker_files(),
                         {".agents/rules/team.md": ["docs/rules/team.md"],
                          ".claude/agents/reviewer.md": ["docs/agents/reviewer.md"],
                          ".agents/plugins/p/agents/x.md": ["shared/x.md"]})
        left = self._left_after_cleanup()
        self.assertLessEqual({".agents/rules/team.md", "docs/rules/team.md",
                              ".claude/agents/reviewer.md", "docs/agents/reviewer.md",
                              ".agents/plugins/p/agents/x.md", "shared/x.md"}, left)
        self.assertNotIn("docs/notes.md", left)

    def test_a_chain_of_markers_keeps_each_file(self):
        self._file(".codex/agents/x.md", "../../.claude/agents/x.md\n")
        self._file(".claude/agents/x.md", "../../docs/x.md\n")
        self._file("docs/x.md", "---\nname: x\ndescription: d\n---\nx\n")
        self.assertEqual(self._marker_files()[".codex/agents/x.md"],
                         [".claude/agents/x.md", "docs/x.md"])
        self.assertLessEqual({".codex/agents/x.md", ".claude/agents/x.md", "docs/x.md"},
                             self._left_after_cleanup())

    def test_a_file_in_the_git_directory_is_not_kept(self):
        self._file(".claude/agents/log.md", "../../.git/logs/HEAD\n")
        self._file(".git/logs/HEAD", "0000 1111 clone\n")
        self.assertEqual(self._marker_files(), {})

    def test_only_a_link_that_a_row_reads_through_keeps_its_file(self):
        """The detection step reads a rule, an agent, a command or a skill through its marker,
        for a front matter test and for the language. It reads a hook file of `.github/hooks`
        through its marker to test whether its JSON configures hooks. A link that a row of its
        folder reads as such an item therefore keeps its file. A link that no row reads as an
        item keeps nothing, such as a `.txt` in `.cursor/rules`, a link without `.json` in
        `.github/hooks`, or a linked context file, which the detection step publishes as a
        reference."""
        self._file(".cursor/rules/style.mdc", "../../docs/style.mdc\n")
        self._file("docs/style.mdc", "Use tabs.\n")
        self._file(".claude/commands/release.md", "../../docs/release.md\n")
        self._file("docs/release.md", "Cut the release.\n")
        self._file(".cursor/rules/notes.txt", "../../docs/notes.txt\n")
        self._file("docs/notes.txt", "Notes.\n")
        self._file(".github/hooks/build", "../../ci/build.json\n")
        self._file(".github/hooks/lint.json", "../../ci/lint.json\n")
        self._file("ci/build.json", '{"hooks": {}}\n')
        self._file("ci/lint.json", '{"hooks": {}}\n')
        self._file(".github/instructions/api.instructions.md", "../../docs/api.md\n")
        self._file("docs/api.md", "Call the API.\n")
        self.assertEqual(self._marker_files(),
                         {".cursor/rules/style.mdc": ["docs/style.mdc"],
                          ".claude/commands/release.md": ["docs/release.md"],
                          ".github/hooks/lint.json": ["ci/lint.json"]})
        left = self._left_after_cleanup()
        self.assertLessEqual({"docs/style.mdc", "docs/release.md", "ci/lint.json"}, left)
        self.assertFalse({"docs/notes.txt", "ci/build.json", "docs/api.md"} & left)

    def test_a_marker_that_the_step_deletes_keeps_no_file(self):
        """The detection step does not read a link in a `.github` below the root, such as in
        its `hooks` or `prompts`, and this step deletes the marker. It deletes the file that
        the marker names too, rather than leave that file in the archive with no row."""
        self._file("AGENTS.md", "# Agents\nRun the tests.\n")
        self._file("pkg/.github/hooks/n.json", "../../config/nested-hooks.json\n")
        self._file("pkg/config/nested-hooks.json", '{"version": 1, "hooks": {}}\n')
        self._file("pkg/.github/prompts/p.prompt.md", "../../docs/p.md\n")
        self._file("pkg/docs/p.md", "Do the thing.\n")
        self.assertEqual(self._marker_files(),
                         {"pkg/.github/hooks/n.json": ["pkg/config/nested-hooks.json"],
                          "pkg/.github/prompts/p.prompt.md": ["pkg/docs/p.md"]})
        self.assertEqual(self._left_after_cleanup(), {"AGENTS.md"})

    def test_a_linked_mechanism_directory_of_a_package_keeps_what_it_links_to(self):
        """blockscout/blockscout-rs links the `.cursor/rules`, `.claude/rules` and
        `.codex/rules` of its packages to `../.memory-bank/rules`. The detection step reads the
        rules of a package through the marker, and the files that the marker names stay. A
        package with linked skills that are the repository's only configuration keeps the
        repository."""
        self._file("indexer/.cursor/rules", "../.memory-bank/rules\n")
        self._file("indexer/.memory-bank/rules/graphify.mdc",
                   "---\ndescription: Graphs.\nalwaysApply: true\n---\nUse graphify.\n")
        self._file("indexer/.memory-bank/notes.md")
        self._file("web/.claude/skills", "../shared/skills\n")
        self._file("web/shared/skills/lint/SKILL.md", "---\nname: lint\ndescription: d\n---\nx\n")
        left = self._left_after_cleanup()
        self.assertLessEqual({"indexer/.cursor/rules", "indexer/.memory-bank/rules/graphify.mdc",
                              "web/.claude/skills", "web/shared/skills/lint/SKILL.md"}, left)
        self.assertNotIn("indexer/.memory-bank/notes.md", left)
        self.assertTrue(_mod.has_detected_mechanism(str(self.repo)))
        rules = _detection.detect_rules(self.repo)
        self.assertTrue(any("graphify.mdc" in str(row) for row in rules), rules)

    def test_every_empty_file_left_is_one_that_the_cleaned_tree_keeps(self):
        """The step keeps an empty CLAUDE file as long as that file decides whether Claude
        Code reads an AGENTS.md. The cleanup deletes that AGENTS.md when it is empty too or
        inside a vendored directory, and the CLAUDE file then decides nothing. If the step
        kept it, the release would publish a repository with an empty file as its
        configuration. The analysis of the content quality, which chooses the set in the
        validated tree, would also stop at it."""
        layouts = {"empty": {"AGENTS.md": "\n", "CLAUDE.md": " \n", ".cursorrules": "Tabs.\n"},
                   "vendored": {"node_modules/x/AGENTS.md": "Read @x.md.\n",
                                "CLAUDE.local.md": "\n", ".cursorrules": "Tabs.\n"},
                   "deciding": {"AGENTS.md": "Read @x.md.\n", "CLAUDE.md": "\n"}}
        for name, files in layouts.items():
            with self.subTest(layout=name):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                for rel, text in files.items():
                    self._file(rel, text)
                left = self._left_after_cleanup()
                empty = {rel for rel in left if _mod.is_empty_file(str(self.repo / rel))}
                self.assertLessEqual(empty, _mod.kept_when_empty(str(self.repo)))
                self.assertEqual("CLAUDE.md" in left, name == "deciding", left)
                self.assertNotIn("CLAUDE.local.md", left)

    def test_a_claude_file_that_links_to_an_empty_agents_md_goes_with_it(self):
        """A `.agents/CLAUDE.md` that links to the AGENTS.md beside it decides whether Claude
        Code reads that AGENTS.md, and it reads as empty through an empty AGENTS.md. Once the
        cleanup deletes the AGENTS.md, the link leads nowhere. If the step kept the link, the
        release would publish it as a context file that no step can read."""
        self._file(".agents/AGENTS.md", "\n  \n")
        os.symlink("AGENTS.md", self.repo / ".agents" / "CLAUDE.md")
        self._file(".cursorrules", "Use tabs.\n")
        _mod.scan_and_clean_repo(str(self.repo), [])
        self.assertFalse(os.path.lexists(self.repo / ".agents" / "CLAUDE.md"))
        self.assertTrue((self.repo / ".cursorrules").is_file())

    def test_a_file_of_nul_bytes_is_no_marker(self):
        """A symlink's target cannot contain a NUL, and the path functions raise ValueError for
        a path with one. Read as a marker, a file of NUL bytes in a commands folder would stop
        the step at every Start. The step keeps the file as the command that its path makes
        it."""
        (self.repo / ".claude/commands").mkdir(parents=True)
        (self.repo / ".claude/commands/blank.md").write_bytes(b"\0" * 16)
        (self.repo / ".claude/skills").write_bytes(b"../\0skills")
        self.assertEqual(find_marker_targets(str(self.repo)), set())
        self.assertEqual(self._marker_files(), {})
        deletions = []
        _mod.scan_and_clean_repo(str(self.repo), deletions)
        self.assertTrue((self.repo / ".claude/commands/blank.md").is_file(), deletions)


class TestALinkedToolDirectory(unittest.TestCase):
    """The cloning step collects a tool directory that is a link as the marker of the link and as
    the directory that the link leads to, at the path of that directory. It follows the links in
    that directory as in the tool directory. The collected file cleanup keeps that directory
    whole, as it keeps the tool directory, and it also keeps the target of a marker in that
    directory, here `shared/lint` behind `config/claude/skills/lint`."""

    setUp, tearDown = TestMarkerTargets.setUp, TestMarkerTargets.tearDown
    _file, _left_after_cleanup = TestMarkerTargets._file, TestMarkerTargets._left_after_cleanup

    def test_the_directory_that_a_linked_claude_leads_to_is_kept(self):
        self._file(".claude", "config/claude\n")
        self._file("config/claude/settings.json", '{"permissions": {"allow": ["Bash(ls)"]}}\n')
        self._file("config/claude/notes.txt")
        self._file("config/claude/skills/lint", "../../../shared/lint\n")
        self._file("shared/lint/SKILL.md", "---\nname: lint\ndescription: Lints.\n---\nLint.\n")
        self._file("shared/notes.md")
        self._file("web/.cursor", ".agents\n")
        self._file("web/.agents/rules/style.mdc", "---\nalwaysApply: true\n---\nTabs.\n")
        self.assertEqual(find_marker_targets(str(self.repo)),
                         {"config/claude", "shared/lint", "web/.agents"})
        self.assertEqual(self._left_after_cleanup(),
                         {".claude", "config/claude/settings.json", "config/claude/notes.txt",
                          "config/claude/skills/lint", "shared/lint/SKILL.md", "web/.cursor",
                          "web/.agents/rules/style.mdc"})

    def test_a_marker_that_names_its_own_parent_keeps_nothing(self):
        """The collector copies no target that contains the link, and a marker of such a link
        keeps no directory around it."""
        self._file("pkg/src/.claude", "..\n")
        self._file("pkg/src/app.py")
        self._file("AGENTS.md", "# Agents\n")
        self.assertEqual(find_marker_targets(str(self.repo)), set())
        self.assertEqual(self._left_after_cleanup(), {"pkg/src/.claude", "AGENTS.md"})

    def test_the_local_state_behind_a_marker_is_deleted(self):
        """With `.claude -> .agents`, Claude Code writes its local state through the link into
        `.agents`, and a tree collected under an earlier collector can contain that state there.
        The step reads each path below the target under the name of the marker, such as
        `.agents/shell-snapshots/x.sh` as `.claude/shell-snapshots/x.sh`, and deletes the local
        state there as well."""
        self._file(".claude", ".agents\n")
        self._file(".agents/skills/docs/SKILL.md", "---\nname: docs\ndescription: D.\n---\nB.\n")
        self._file(".agents/shell-snapshots/x.sh", "export TOKEN=1\n")
        self._file(".agents/scheduled_tasks.lock", "1\n")
        self._file("web/.claude", ".agents\n")
        self._file("web/.agents/todos/t.json", '["x"]\n')
        self._file("web/.agents/rules/r.md", "---\ntrigger: always_on\n---\nTabs.\n")
        deletions = []
        _mod.scan_and_clean_repo(str(self.repo), deletions)
        self.assertEqual({p.relative_to(self.repo).as_posix() for p in self.repo.rglob("*")
                          if p.is_file()},
                         {".claude", ".agents/skills/docs/SKILL.md", "web/.claude",
                          "web/.agents/rules/r.md"})
        self.assertIn(("o§a/.agents/shell-snapshots/x.sh",
                       "local state of a tool (.claude/shell-snapshots/)"),
                      [(os.path.relpath(p, self.tmp), reason) for p, reason in deletions])

    def test_a_linked_github_is_left_to_the_github_rule(self):
        """plesk/centos2alma links `.claude` to `.github/`. If the step kept `.github/` whole as
        the target of the marker, its CI workflow and its issue template would be published as
        files of Claude Code's directory, although the step deletes them from a `.github` without
        the link."""
        self._file(".claude", ".github/\n")
        self._file(".github/workflows/ci.yml", "on: push\njobs: {}\n")
        self._file(".github/ISSUE_TEMPLATE/bug.md", "Describe the bug.\n")
        self._file(".github/settings.json", '{"permissions": {"allow": ["Bash(ls)"]}}\n')
        self._file(".github/commands/review.md", "Review the change.\n")
        self._file("AGENTS.md", "# Agents\n")
        self.assertEqual(find_marker_targets(str(self.repo)), set())
        self.assertEqual(self._left_after_cleanup(),
                         {".claude", ".github/settings.json", ".github/commands/review.md",
                          "AGENTS.md"})


class TestTheReasonVocabularyIsTheDocumentedOne(unittest.TestCase):
    """`docs/steps/collected_file_cleanup.md` calls the deletion reasons a closed vocabulary,
    and this test keeps it closed.

    The test fails for a reason that the step records and the table lacks. It also fails for
    a row of the table that no run can write, such as a reason string renamed only in the
    documents.

    The comparison uses the part of a reason that a run always writes, the text before the
    first substitution. The four `wrong letter case of` reasons are therefore one entry on both
    sides.
    """

    ROOT = Path(__file__).resolve().parents[1]
    DOC = ROOT / "docs" / "steps" / "collected_file_cleanup.md"
    MODULE = ROOT / "agentcontext" / "steps" / "collected_file_cleanup.py"
    TABLE_HEADING = "The reason vocabulary is closed:"

    @staticmethod
    def _literal_prefix(node):
        """The text that a reason starts with, or None when the node is not a string."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr) and node.values:
            first = node.values[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                return first.value
        return None

    def _reasons_the_module_writes(self):
        """Every reason that `collected_file_cleanup.py` records: assigned to `reason`, or
        appended inline."""
        tree = ast.parse(self.MODULE.read_text(encoding="utf-8"))
        found = set()
        for node in ast.walk(tree):
            written = None
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "reason" for t in node.targets):
                written = node.value
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == "append"
                  and isinstance(node.func.value, ast.Name)
                  and node.func.value.id == "deletions"
                  and len(node.args) == 1 and isinstance(node.args[0], ast.Tuple)
                  and len(node.args[0].elts) == 2):
                written = node.args[0].elts[1]
            prefix = self._literal_prefix(written) if written is not None else None
            if prefix:
                found.add(prefix)
        return found

    def _reasons_the_document_lists(self):
        """The first column of the table, minus the rows that the table marks as removed."""
        lines = self.DOC.read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(lines) if self.TABLE_HEADING in line)
        listed = set()
        for line in lines[start:]:
            if not line.startswith("|"):
                if listed:
                    break
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2 or not cells[0].startswith("`"):
                continue
            if cells[1].startswith("Removed."):
                continue      # documented history: the step that wrote it is gone
            listed.add(cells[0].strip("`"))
        return listed

    @staticmethod
    def _before_substitution(reason):
        """`inside submodule (<path>)` and `f"inside submodule ({inside})"` meet here."""
        return reason.split("<", 1)[0] if "<" in reason else reason

    def test_every_reason_the_module_writes_is_in_the_table(self):
        written = {self._before_substitution(r) for r in self._reasons_the_module_writes()}
        listed = {self._before_substitution(r) for r in self._reasons_the_document_lists()}
        self.assertEqual(sorted(written - listed), [],
                         f"{self.DOC.name} calls the vocabulary closed and these are not in it")

    def test_every_reason_the_table_lists_can_be_written(self):
        written = {self._before_substitution(r) for r in self._reasons_the_module_writes()}
        listed = {self._before_substitution(r) for r in self._reasons_the_document_lists()}
        self.assertEqual(sorted(listed - written), [],
                         f"{self.DOC.name} names reasons no run can produce")

    def test_the_reasons_are_found_at_all(self):
        # Both directions above pass trivially if the extraction finds nothing.
        self.assertGreaterEqual(len(self._reasons_the_module_writes()), 8)
        self.assertGreaterEqual(len(self._reasons_the_document_lists()), 8)


if __name__ == "__main__":
    unittest.main()
