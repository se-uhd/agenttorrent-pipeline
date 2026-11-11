"""Tests for agentcontext.heuristics and the files that it loads:

- the columns of `heuristics/config_path_patterns.csv`
- that the steps name no path that the heuristics do not list, and that the heuristics list no
  path that a step does not read
- the derived CSV headers, the provenance record and the vendored list
- `heuristics/ai_commit_patterns.csv`, compared with the heuristics compiled from it and with
  the published CSV

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_heuristics -v
"""

import ast
import contextlib
import csv
import hashlib
import importlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from agentcontext import REPO_ROOT, STAGE_DIR, caches, commits as _detect, paths as COLLECT, heuristics
from agentcontext.checks import commits as _validate, regressions as CHECK_REGRESSIONS
from agentcontext.heuristics import (
    ARTIFACT_COLUMNS,
    CONFIG_FIELDNAMES,
    MECHANISM_CSVS,
    META_COLUMNS,
    artifact_column,
    artifact_column_for_file,
    default_place_row,
    plugin_placeholder,
    extensions_for,
    is_directory_mode,
    load_config_path_patterns,
    load_vendored_dirs,
    paths_for,
    csv_filenames,
    csv_header,
)
from agentcontext.steps import (
    output_cross_check as CONSISTENCY,
    detection as DETECT,
    collected_file_cleanup as VALIDATE,
)

DATA_DIR = STAGE_DIR

ROWS = load_config_path_patterns()

SIMPLE_MODES = {
    "file", "root_file", "basename", "skill_dir", "skill_dir_flat", "skill_marker", "agent_memory",
    "json_copilot_marketplace", "json_plugin_mcp", "json_hooks", "json_hooks_or_modules", "json_named_hooks", "toml_hooks", "json_mcp_servers", "toml_mcp_servers",
    "basename_json_mcp_servers", "basename_json_plugin_manifest",
    "basename_json_agent_plugins_mcp", "spec_dir", "spec_dir_with_place", "unclassified_dir",
    "copy_dir", "copy_dir_gated", "plugin_sibling", "root_any_case", "declared_rules",
    "plugin_root_skill", "registered:skills", "registered:subagents",
    "registered:commands", "registered:plugins",
}

PARAMETRIC_MODES = ("dir_agent:", "dir_ext:", "dir_ext_described:", "dir_ext_triggered:", "tree_ext:", "tree_ext_described:", "tree_suffix:", "workflow_uses:", "name_glob:",
                    "workflow_engine:", "workflow_command:", "package_entry:")
# Parametric too, and about a file rather than a directory: the file that names context files.
DECLARING_MODES = ("declared_names:", "declared_fallback:", "declared_path:", "declared_local:",
                   "declared_globs:")
# Parametric and about a file too: the file that counts by the value under one of its keys.
JSON_KEY_MODES = ("json_servers:", "json_enabled:")
# A plugin placeholder row with a manifest key that names more places of the component. The row of
# the default place defines how such a place is read, and the mode itself names neither a
# file nor a directory.
MANIFEST_MODES = ("manifest_field:",)

NON_ARTIFACT_LITERALS = {
    ".git",         # excluded from every copy
    ".DS_Store",    # never valid anywhere
    ".log",         # log-file suffix
    ".gitmodules",  # git's own file, read for where a submodule points
    ".tmp",         # the suffix that an atomic write uses before the rename
    ".csv",         # the suffix of a CSV or of a commit shard
    # The workflow files that a run keeps beside the collection. The directory and the two
    # suffixes name no mechanism on purpose: some workflow files are configuration and have their
    # own rows, and the cloning step copies every workflow file into a tree that no step reads.
    # The heuristics decide nothing about that copy. See
    # docs/steps/repository_cloning.md.
    ".github/workflows",
    ".yml",
    ".yaml",
    # A compiled GitHub Agentic Workflow, which the workflow_command rows do not cover: the
    # Markdown that it was compiled from is the configuration.
    ".lock.yml",
    "provenance.json",  # the record that the detector writes beside its CSVs
    "commit-reclassification.json",  # the report that names the shards that detection leaves out
    "skills.md",        # the key under which the detector's skill records store the file
    # What a manifest key can name that is not a file of the repository: an MCP bundle.
    ".mcpb",
    ".dxt",
    ".agent.md",        # the suffix that a Copilot agent's name drops, which names no place
}

# A lower-case file name with a configuration suffix. The collectors compare a name with
# `f.name.lower()`, and `agents.md` in the code is AGENTS.md in the heuristics. A pattern
# that matched only names that start with a dot or a capital letter would never compare a name such
# as `agents.local.md` added to a collector's set, or `managed-settings.json` beside
# `managed-mcp.json`, with the heuristics.
LOWER_CASE_NAME = re.compile(r"^[a-z][a-z0-9_.-]*\.(md|mdc|json|jsonc|toml|ya?ml)$")

PATH_LITERAL = re.compile(
    r"^\.[A-Za-z][A-Za-z0-9_.-]*(/[A-Za-z0-9_.*-]+)*$"
    r"|^[A-Z][A-Za-z0-9_.]*\.(md|json|toml)$"
    rf"|{LOWER_CASE_NAME.pattern}"
)

def path_literals(filename):
    """Return every string literal in a step script that has the form of a path."""
    tree = ast.parse((REPO_ROOT / filename).read_text(encoding="utf-8"))
    return {n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and PATH_LITERAL.match(n.value)}


def write_sub_calls(source):
    """Read every `write_sub(...)` call, as ([(filename, mechanism)], [unreadable]).

    Each call is `write_sub("<file>.csv", csv_header("<mechanism>"), <rows>)`, and both halves of a
    `MECHANISM_CSVS` row are named at the call site: the file that the CSV is published
    as, and the mechanism of its header. A call that this function cannot read is returned rather
    than skipped, because a skipped call is a CSV that the comparison would stop covering without
    any message.
    """
    found, unreadable = [], []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "write_sub"):
            continue
        header = node.args[1] if len(node.args) > 1 else None
        if not (len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and isinstance(header, ast.Call)
                and isinstance(header.func, ast.Name)
                and header.func.id == "csv_header"
                and len(header.args) == 1
                and isinstance(header.args[0], ast.Constant)
                and isinstance(header.args[0].value, str)):
            unreadable.append(ast.unparse(node))
            continue
        found.append((node.args[0].value, header.args[0].value))
    return found, unreadable


def mechanism_constants():
    d = DETECT
    constants = {
        "context_file": [d.CONTEXT_FILENAMES, d.CONTEXT_FILES, d.ROOT_CONTEXT_FILES, d.CONTEXT_DIRS_SUFFIX,
                         [r["path"] for r in d.CONTEXT_DECLARATION_HEURISTICS], list(d.ROOT_ANY_CASE),
                         d.NESTED_CONTEXT_FILES, d.NESTED_CONTEXT_DIRS_SUFFIX],
        "settings": [d.SETTINGS_PATHS, d.SETTINGS_BASENAMES, d.SETTINGS_DIRS_TOML,
                     d.SETTINGS_DIRS_JSON, d.SETTINGS_WORKFLOW_DIRS],
        "skills": [d.SKILLS_DIRS, d.SKILL_COMMAND_DIRS, d.SKILL_WORKFLOW_DIRS,
                   d.SKILL_DIRS_MD_FLAT_DESCRIBED, [d.SKILL_MARKER], d.PLUGIN_ROOT_SKILLS,
                   d.NESTED_SKILLS_DIRS],
        "subagents": [[path for path, _ in d.JSON_KEY_ROWS["subagents"]], d.SUBAGENT_DIRS_DESCRIBED, d.SUBAGENT_DIRS_MD, d.SUBAGENT_DIRS_MARKDOWN, d.SUBAGENT_DIRS_MD_FLAT, d.SUBAGENT_DIRS_MD_FLAT_DESCRIBED, d.SUBAGENT_DIRS_NAMED, d.SUBAGENT_DIRS_TOML,
                      d.SUBAGENT_DIRS_SUFFIX, d.AGENT_MEMORY_PATTERNS, d.NESTED_SUBAGENT_DIRS_DESCRIBED,
                      [r["path"] for r in d.SUBAGENT_DECLARATION_HEURISTICS]],
        "commands": [d.COMMAND_DIRS_TOML, d.COMMAND_DIRS_TEXT, d.COMMAND_DIRS_MD, d.COMMAND_DIRS_MD_FLAT,
                     d.COMMAND_DIRS_PROMPT, [path for path, _ in d.JSON_KEY_ROWS["commands"]]],
        "hooks": [d.HOOKS_JSON_FILES, d.HOOKS_MODULE_FILES, d.HOOKS_NAMED_FILES, d.HOOKS_TOML_FILES,
                  d.HOOKS_DIRS_JSON,
                  [path for path, _ in d.HOOKS_NAME_GLOBS]],
        "rules": [d.RULES_DIRS_CODEX, d.RULES_DIRS_MARKDOWN, d.RULES_DIRS_CURSOR, d.RULES_DIRS_CLAUDE,
                  d.RULES_DIRS_MD_FLAT, d.RULES_DIRS_DESCRIBED, d.RULES_DIRS_TRIGGERED, d.RULES_FILES,
                  [r["path"] for r in d.RULES_JSON_ROWS]],
        "mcp": [[d.MCP_ROOT_FILE], d.MCP_FILES, d.MCP_PLUGIN_FILES, d.MCP_JSON_FILES,
                [d.MCP_CODEX_CONFIG], d.MCP_BASENAMES, d.MCP_AGENT_PLUGINS,
                [path for path, _ in d.JSON_KEY_ROWS["mcp"]], d.NESTED_MCP_FILES],
        "plugins": [d.COPILOT_MARKETPLACES, d.PLUGIN_FILES, d.PLUGIN_BASENAMES, d.PLUGIN_MANIFESTS,
                    d.PLUGIN_MODULE_DIRS, d.PLUGIN_PACKAGE_DIRS],
        "lsp": [d.LSP_FILES, [path for path, _ in d.JSON_KEY_ROWS["lsp"]], d.NESTED_LSP_FILES],
        "other": [d.SPEC_DIRS, d.ROOT_SPEC_FILES, d.SPEC_BASENAMES],
    }
    for mechanism in ("skills", "subagents", "commands", "plugins"):
        constants[mechanism].append([r["path"] for r in d.REGISTRATION_ROWS
                                    if r["mechanism"] == mechanism])
    return constants



COMMIT_ROWS = heuristics.load_ai_commit_patterns()
OTHER_COMMIT_ROWS = heuristics.load_other_ai_commit_patterns()


def _top_level_bar(pattern: str) -> bool:
    """Whether the pattern has a `|` outside every group and character class, which makes
    what follows it an alternative to the whole pattern."""
    depth, in_class, i = 0, False, 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            i += 2
            continue
        if in_class:
            in_class = c != "]"
        elif c == "[":
            in_class = True
            # A `]` right after `[` or `[^` is a literal, not the end of the class.
            if pattern[i + 1:i + 3].startswith("^]"):
                i += 2
            elif pattern[i + 1:i + 2] == "]":
                i += 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "|" and depth == 0:
            return True
        i += 1
    return False


PUBLISHED =STAGE_DIR / "output" / "all_ai_commits.csv"


class TestTheHeuristicsColumns(unittest.TestCase):
    """The heuristics file itself is well formed."""

    def test_has_rows(self):
        self.assertGreater(len(ROWS), 40)

    def test_every_column_present(self):
        for row in ROWS:
            self.assertEqual(set(row) & set(CONFIG_FIELDNAMES), set(CONFIG_FIELDNAMES), row)

    def test_evidence_that_is_not_a_link_is_refused(self):
        """A path that the page does not state in full links the files that it was read off,
        and a reader can follow only a link."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / "config_path_patterns.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CONFIG_FIELDNAMES)
            writer.writeheader()
            writer.writerow({"tool": "claude", "mechanism": "skills", "path": ".claude/skills",
                             "match_mode": "skill_dir", "content_format": "",
                             "doc_url": "https://x.test", "doc_verified": "2026-09-26",
                             "evidence": "o/r/.claude/skills", "notes": ""})
        with self.assertRaises(ValueError) as caught:
            load_config_path_patterns(path)
        self.assertRegex(str(caught.exception),
                         "The evidence 'o/r/.claude/skills' of .* is not an https link")

    def test_match_modes_are_known(self):
        for row in ROWS:
            # A `nested:` mode reads below the root with a mode of the vocabulary.
            mode = heuristics.nested_inner(row["match_mode"]) or row["match_mode"]
            ok = mode in SIMPLE_MODES or mode.startswith(PARAMETRIC_MODES + DECLARING_MODES
                                                         + MANIFEST_MODES + JSON_KEY_MODES)
            self.assertTrue(ok, f"unknown match mode {mode!r} for {row['path']}")

    def test_a_nested_row_names_a_place_outside_the_directories_that_tools_read_whole(self):
        """A `nested:` row reads its place in every directory below the root. The place is below
        a directory other than the directories that every step copies and reads whole at any
        depth, where the directory's own rows read the place already, or it is a file with
        the row's path as its name, such as `.mcp.json`, which `paths.nested_places` finds
        outside those directories."""
        nested = [r for r in ROWS if heuristics.nested_inner(r["match_mode"]) is not None]
        self.assertTrue(nested)
        whole = set(heuristics.spec_dir_paths(ROWS))
        for row in nested:
            with self.subTest(path=row["path"], mode=row["match_mode"]):
                head, _, rest = row["path"].partition("/")
                if rest:
                    self.assertNotIn(head, whole)
                else:
                    self.assertFalse(is_directory_mode(row["match_mode"]))
                self.assertIsNone(heuristics.nested_inner(heuristics.nested_inner(row["match_mode"])))

    def test_a_manifest_key_extends_a_place_a_row_reads(self):
        """Detection reads the places that a manifest_field key names with the match mode of
        the row for the key's default place, and a key with a default place that no row covers
        would lead to no file."""
        for row in ROWS:
            if row["match_mode"].startswith(MANIFEST_MODES):
                with self.subTest(row=row["path"]):
                    self.assertTrue(plugin_placeholder(row["path"]))
                    default = default_place_row(row, ROWS)
                    self.assertFalse(default["match_mode"].startswith(MANIFEST_MODES))

    def test_parametric_modes_contain_extensions(self):
        for row in ROWS:
            if row["match_mode"].startswith(("dir_ext:", "tree_ext:")):
                exts = extensions_for(row["match_mode"])
                self.assertTrue(exts, row["path"])
                for ext in exts:
                    self.assertTrue(ext.startswith("."), f"{row['path']}: {ext!r}")

    def test_a_package_mode_names_its_entry_modules_and_their_extensions(self):
        """`heuristics.package_entries` reads the names and the extensions in the order of the
        mode, and refuses a mode that lacks either, because such a row would find no folder."""
        self.assertEqual(heuristics.package_entries("package_entry:server|index:.ts|.js"),
                         (("server", "index"), (".ts", ".js")))
        for mode in ("package_entry:server", "package_entry::.ts", "dir_ext:.ts"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                heuristics.package_entries(mode)
        rows = [r for r in ROWS if r["match_mode"].startswith(heuristics.PACKAGE_ENTRY_MODE)]
        self.assertTrue(rows)
        for row in rows:
            _names, extensions = heuristics.package_entries(row["match_mode"])
            for ext in extensions:
                self.assertTrue(ext.startswith("."), f"{row['path']}: {ext!r}")

    def test_no_duplicate_rows(self):
        """A row per tool, place and match mode. Two tools can read one place in the same way,
        as Cursor and OpenCode read the category folders of `.claude/skills`, and each of them
        is a heuristic of its own."""
        seen = set()
        for row in ROWS:
            key = (row["tool"], row["mechanism"], row["match_mode"], row["path"])
            self.assertNotIn(key, seen, f"duplicate heuristic: {key}")
            seen.add(key)

    def test_tools_are_known(self):
        allowed = {"claude", "gemini", "codex", "copilot", "cursor", "opencode", "any"}
        for row in ROWS:
            self.assertIn(row["tool"], allowed, row["path"])

    def test_mechanisms_are_known(self):
        allowed = {"context_file", "settings", "skills", "subagents", "commands",
                   "hooks", "rules", "mcp", "plugins", "lsp", "other", "collect_only"}
        for row in ROWS:
            self.assertIn(row["mechanism"], allowed, row["path"])


class TestDocWatch(unittest.TestCase):
    """The watch hashes the document, not a page about it."""

    def test_github_blob_urls_resolve_to_raw(self):
        watch = importlib.import_module("agentcontext.tools.docs")
        self.assertEqual(
            watch.canonical_url(
                "https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/settings.md"),
            "https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/cli/settings.md")
        self.assertEqual(watch.canonical_url("https://cursor.com/docs/hooks"),
                         "https://cursor.com/docs/hooks")

    def test_every_github_doc_url_is_a_blob_url(self):
        """The watch would hash a tree or raw URL as it is, and the hash would change for other
        reasons than a change of the document."""
        for row in ROWS:
            url = row["doc_url"]
            if url.startswith("https://github.com/"):
                self.assertIn("/blob/", url, f"{row['path']}: {url}")


class TestProvenance(unittest.TestCase):
    """Every heuristic cites where it came from and when that source was last checked."""

    def test_every_row_cites_a_doc(self):
        for row in ROWS:
            self.assertTrue(row["doc_url"].startswith("https://"),
                            f"{row['path']} has no documentation URL")

    def test_every_row_records_a_verification_date(self):
        for row in ROWS:
            try:
                verified = date.fromisoformat(row["doc_verified"])
            except ValueError:
                self.fail(f"{row['path']}: doc_verified is not an ISO date: "
                          f"{row['doc_verified']!r}")
            self.assertLessEqual(verified, date.today(),
                                 f"{row['path']}: doc_verified is in the future")


class TestProvenanceRecordsThisHeuristics(unittest.TestCase):
    """`output/provenance.json` records which heuristics produced the CSVs beside it.

    An edit to `config_path_patterns.csv` after the last detection run leaves the
    recorded `heuristics_sha256` naming a file that is no longer in the tree, even
    when every heuristic is unchanged, and these tests then fail.
    """

    PROVENANCE_JSON = DATA_DIR / "output" / "provenance.json"
    HEURISTICS = importlib.import_module("agentcontext.heuristics").CONFIG_PATH_PATTERNS_PATH

    def _provenance(self) -> dict:
        if not self.PROVENANCE_JSON.is_file():
            self.skipTest(f"{self.PROVENANCE_JSON.name} is not present")
        return json.loads(self.PROVENANCE_JSON.read_text(encoding="utf-8"))

    def test_the_recorded_checksum_is_this_heuristics(self):
        recorded = self._provenance()["heuristics_sha256"]
        current = hashlib.sha256(self.HEURISTICS.read_bytes()).hexdigest()
        self.assertEqual(
            recorded, current,
            "output/provenance.json records a heuristics file that is not the one on "
            "disk. Re-run agentcontext/steps/detection.py so that the output records which "
            "heuristics produced it.")

    def test_the_recorded_path_count_is_this_heuristics(self):
        self.assertEqual(self._provenance()["pattern_count"], len(ROWS))

    def test_the_recorded_heuristics_is_the_file_the_loader_reads(self):
        self.assertEqual(self._provenance()["heuristics"], self.HEURISTICS.name)

    def test_the_recorded_version_is_the_one_the_detector_stamps(self):
        # The field that the rest of the pipeline uses as a key: DATASET_VERSION goes into
        # output/provenance.json and into the dataset_version column of repos.csv and every
        # mechanism CSV, and no other test checks it. The regression check tracks only CSVs,
        # and baseline.json does not cover provenance.json.
        self.assertEqual(self._provenance()["dataset_version"], DETECT.DATASET_VERSION)

    def test_the_recorded_version_is_the_one_that_the_published_csvs_contain(self):
        # This test fails on a provenance.json regenerated without the CSVs, or on CSVs
        # regenerated without it.
        recorded = self._provenance()["dataset_version"]
        seen = set()
        for filename in csv_filenames():
            path = DATA_DIR / "output" / filename
            if not path.is_file():
                continue
            with open(path, newline="", encoding="utf-8") as f:
                rows = csv.DictReader(f)
                if "dataset_version" not in (rows.fieldnames or []):
                    continue
                seen.update(r["dataset_version"] for r in rows)
        if not seen:
            self.skipTest("no published CSVs with dataset_version")
        self.assertEqual(seen, {recorded},
                         "output/provenance.json names a release that the published "
                         "CSVs beside it do not have")


class TestTheArtifactColumnComesFromTheHeuristics(unittest.TestCase):
    """Which column of a mechanism's CSV names the configuration artifact.

    A column position breaks when a column is added before it: with `head_sha` in the
    schema, `columns[3]` is the SHA, every row would be looked up as though a commit hash
    were a path, and every mechanism CSV would become empty without a single error. "The
    first column that is not leading metadata" breaks the same way when a column is added
    to the leading metadata.
    """

    OUTPUT = DATA_DIR / "output"
    DETECTOR = "agentcontext/steps/detection.py"

    def _detector_source(self):
        return (REPO_ROOT / self.DETECTOR).read_text(encoding="utf-8")

    def test_every_mechanism_that_classifies_has_a_mechanism_csv(self):
        mechanisms = {r["mechanism"] for r in ROWS} - {"collect_only"}
        self.assertEqual(mechanisms, set(MECHANISM_CSVS),
                         "a mechanism of the heuristics with no published CSV, or a "
                         "CSV that names a mechanism that the heuristics do not name")

    def test_a_call_this_check_cannot_read_is_reported_rather_than_skipped(self):
        """This test protects the two tests below. Skipping an unreadable call would
        drop its CSV from the comparison, and both tests would still pass."""
        calls, unreadable = write_sub_calls(
            'write_sub(name, csv_header("hooks"), rows)\n'
            'write_sub("mcp.csv", header, rows)\n'
            'write_sub("other.csv", csv_header(mechanism), rows)\n'
            'write_sub("rules.csv", csv_header("rules"), rows)\n')
        self.assertEqual(calls, [("rules.csv", "rules")])
        self.assertEqual(len(unreadable), 3)

    def test_the_filenames_are_the_ones_the_detector_writes(self):
        """The other half of MECHANISM_CSVS, which nothing else checks.

        Every published header exercises `artifact_column`, but only this test checks
        the filename beside it: after renaming `skills.csv` to `skillz.csv` in the
        heuristics, the rest of the suite would pass while
        `artifact_column_for_file("skills.csv")` raised. The test reads the calls out of
        the detector by AST rather than by running it, so that it works with or without
        `output/` on disk.
        """
        calls, unreadable = write_sub_calls(self._detector_source())
        self.assertEqual(unreadable, [],
                         "a write_sub call that this test cannot read: the filename "
                         "or the header is not a literal, so the CSV that it writes "
                         "is compared with nothing")
        self.assertEqual({filename for filename, _ in calls}, set(csv_filenames()),
                         "the heuristics and the detector name different files "
                         "for the mechanism CSVs")

    def test_each_mechanism_csv_is_written_with_its_own_header(self):
        """The file and the header are named at the same call, and a CSV written under one
        mechanism's name with another's header therefore fails here. The header has one declaration,
        and this test checks that each call uses it.
        """
        calls, unreadable = write_sub_calls(self._detector_source())
        self.assertEqual(unreadable, [])
        self.assertEqual(len(calls), len(MECHANISM_CSVS),
                         "one write_sub call per published mechanism CSV")
        for filename, mechanism in calls:
            with self.subTest(filename=filename):
                self.assertIn(mechanism, MECHANISM_CSVS,
                              f"{filename} is written with the header of a mechanism "
                              f"that the heuristics do not name")
                self.assertEqual(MECHANISM_CSVS[mechanism][0], filename,
                                 f"{filename} is written with the header of "
                                 f"{mechanism}, which is published as "
                                 f"{MECHANISM_CSVS[mechanism][0]}")

    def test_no_module_keeps_its_own_copy_of_the_mapping(self):
        """The other modules that contain the mapping contain the same pairs.

        `file_history.reconcile_repos_csv` reads `MECHANISM_CSVS` itself. The
        same list is in two more forms, a bare filename list and the check's
        mechanism-to-filename map, and this test compares both with `MECHANISM_CSVS`.
        """
        self.assertEqual(set(CONSISTENCY.MECHANISM_FILES), set(csv_filenames()))
        tracked = set(CHECK_REGRESSIONS.TRACKED_FILES.values())
        self.assertEqual(set(csv_filenames()) - tracked, set(),
                         "the regression check does not track every published "
                         "mechanism CSV, so it would not detect a change to one")

    def test_the_git_step_does_not_take_the_artifact_column_by_position(self):
        """A column position is right only while the output CSVs begin with exactly the
        columns that it assumes. With `head_sha` added, `columns[3]` is the commit hash, and
        every row would then be looked up as though a hash were a path, match nothing, and be
        removed."""
        source = (REPO_ROOT / "agentcontext/steps/file_history.py").read_text(encoding="utf-8")
        # By AST, so that a comment that mentions a positional derivation does not
        # count as one.
        indexed = [
            node for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id in ("header", "columns")
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, int)
        ]
        self.assertEqual(indexed, [],
                         "file_history.py takes the column of the configuration "
                         "artifact by position")

    def test_no_two_mechanisms_share_a_csv_or_an_artifact_column(self):
        files = [f for f, _ in MECHANISM_CSVS.values()]
        self.assertEqual(len(files), len(set(files)))
        self.assertEqual(len(ARTIFACT_COLUMNS), len(set(ARTIFACT_COLUMNS)))

    def test_the_published_headers_resolve_the_same_way_both_ways(self):
        seen = 0
        for filename in csv_filenames():
            path = self.OUTPUT / filename
            if not path.is_file():
                continue
            seen += 1
            with open(path, newline="", encoding="utf-8") as f:
                header = next(csv.reader(f))
            self.assertEqual(artifact_column(header),
                             artifact_column_for_file(filename), filename)
        if not seen:
            self.skipTest("no published CSVs on disk")

    def test_a_2_0_0_header_still_names_the_artifact(self):
        # A layout with `timestamp` renamed and `head_commit_date` added, where
        # neither position 3 nor "the first column that is not leading metadata"
        # is the column that names the configuration artifact.
        header = ["repo_name", "scanned_at", "branch", "head_sha",
                  "head_commit_date", "rule", "name", "github_link"]
        self.assertEqual(artifact_column(header), "rule")

    def test_the_detection_step_reads_the_columns_from_the_heuristics(self):
        """The detection step builds each row from the heuristics. When a mechanism's header gains a
        column, the step still puts the path in the column that names it and still looks
        up the Git columns by the same value. The test checks which column of this mechanism names
        the configuration artifact, not the position of that column.
        """
        import tempfile
        mechanism = "context_file"
        column = MECHANISM_CSVS[mechanism][1]
        cache = caches.FileHistoryCache()
        cache.put(caches.FileHistoryKey("o/r", "a" * 40, mechanism, "CLAUDE.md"),
                  caches.FileHistory("2026-01-01T00:00:00Z", "2", "b" * 40, "c" * 40))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "CLAUDE.md").write_text("# hi\n", encoding="utf-8")
            row, missing = DETECT.build_detail_row(
                mechanism, "CLAUDE.md",
                base={**{c: "" for c in META_COLUMNS}, "repo_name": "o/r",
                      "head_sha": "a" * 40},
                repo_dir=Path(tmp), repo_name="o/r", branch="main",
                head_sha="a" * 40, cache=cache, ai_authorship={},
                version_column={"dataset_version": DETECT.DATASET_VERSION})
        self.assertEqual(missing, [])
        self.assertEqual(row[column], "CLAUDE.md")
        self.assertEqual(row["#commits"], "2")
        self.assertEqual(row["github_link"],
                         "https://github.com/o/r/tree/main/CLAUDE.md")

    def test_the_row_the_detector_builds_fills_the_declared_header(self):
        """Every column of the header and nothing else.

        A header column that no row fills is published blank in every row, as a
        description of files that nobody has read. A key that no header has is the other
        direction, and the writer refuses it.
        """
        import tempfile
        cache = caches.FileHistoryCache()
        with tempfile.TemporaryDirectory() as tmp:
            for mechanism, (_, column) in MECHANISM_CSVS.items():
                item = {column: "CLAUDE.md"}
                item.update({c: "" for c in heuristics.detail_columns(mechanism)})
                with self.subTest(mechanism=mechanism):
                    row, _missing = DETECT.build_detail_row(
                        mechanism, item,
                        base={**{c: "" for c in META_COLUMNS}, "repo_name": "o/r"},
                        repo_dir=Path(tmp), repo_name="o/r", branch="main",
                        head_sha="a" * 40, cache=cache, ai_authorship={},
                        version_column={"dataset_version": DETECT.DATASET_VERSION})
                    # main fills `tool` once every row of the repository is built, because
                    # `artifact_tools` reads the rows together. `TestToolColumn` in the
                    # detection suite checks that every row of every mechanism CSV has one.
                    self.assertNotIn("tool", row)
                    self.assertEqual(sorted([*row, "tool"]), sorted(csv_header(mechanism)))

    def test_a_header_with_no_artifact_column_is_an_error(self):
        with self.assertRaises(ValueError):
            artifact_column(["repo_name", "branch", "github_link"])

    def test_a_header_with_two_artifact_columns_is_an_error(self):
        # Taking the first match would key the rows on whichever column came first in
        # the heuristics, a decision that artifact_column must not make without an error.
        with self.assertRaises(ValueError) as caught:
            artifact_column(["repo_name", "settings", "mcp"])
        self.assertIn("more than one configuration artifact column (settings, mcp)",
                      str(caught.exception))


class TestThePublishedHeaders(unittest.TestCase):
    """`csv_header` is the one declaration of what each mechanism CSV contains, and the widths
    below are the published columns of the output CSVs rather than a copy of
    anything.
    """

    # The published width of each CSV. Written out, because a width computed
    # from `csv_header` would agree with any change to `csv_header`.
    WIDTHS = {"settings": 13, "context_file": 21, "skills": 18, "subagents": 16,
              "commands": 15, "hooks": 13, "rules": 15, "mcp": 13, "plugins": 13,
              "lsp": 13, "other": 14}

    def test_every_mechanism_has_a_declared_width(self):
        self.assertEqual(set(self.WIDTHS), set(MECHANISM_CSVS))

    def test_every_declared_width(self):
        for mechanism, width in self.WIDTHS.items():
            with self.subTest(mechanism=mechanism):
                self.assertEqual(len(csv_header(mechanism)), width)

    def test_no_header_repeats_a_column(self):
        for mechanism in MECHANISM_CSVS:
            header = csv_header(mechanism)
            with self.subTest(mechanism=mechanism):
                self.assertEqual(len(header), len(set(header)))

    def test_artifact_column_resolves_on_every_header(self):
        """Both ways round, on the header that the detector writes. A header
        that gains a column named after another mechanism makes the row key
        ambiguous, and `artifact_column` raises rather than taking the first."""
        for mechanism, (filename, column) in MECHANISM_CSVS.items():
            with self.subTest(mechanism=mechanism):
                header = csv_header(mechanism)
                self.assertEqual(artifact_column(header), column)
                self.assertEqual(artifact_column_for_file(filename), column)

    def test_the_metadata_block_leads_every_header(self):
        for mechanism in MECHANISM_CSVS:
            with self.subTest(mechanism=mechanism):
                self.assertEqual(tuple(csv_header(mechanism)[:5]), META_COLUMNS)
        self.assertEqual(META_COLUMNS, ("repo_name", "scanned_at", "branch",
                                        "head_sha", "head_commit_date"))

    def test_every_header_contains_the_git_columns(self):
        """The git columns are in each mechanism CSV itself."""
        for mechanism in MECHANISM_CSVS:
            header = csv_header(mechanism)
            with self.subTest(mechanism=mechanism):
                for column in ("created_at", "#commits", "first_commit_sha",
                               "last_commit_sha"):
                    self.assertIn(column, header)

    def test_no_header_contains_is_empty(self):
        """`is_empty` could only be FALSE, because
        `agentcontext/steps/collected_file_cleanup.py` deletes every empty file before detection runs."""
        for mechanism in MECHANISM_CSVS:
            with self.subTest(mechanism=mechanism):
                self.assertNotIn("is_empty", csv_header(mechanism))

    def test_a_mechanism_with_no_csv_has_no_header(self):
        with self.assertRaises(KeyError):
            csv_header("collect_only")


class TestANestedMode(unittest.TestCase):
    """A `nested:` mode reads the place of its inner mode in a directory below the repository
    root and not at the root, which the row of the inner mode reads."""

    def _row(self, path, mode):
        return {"tool": "copilot", "mechanism": "context_file", "path": path, "match_mode": mode}

    def test_it_matches_below_the_root_only(self):
        file_row = self._row(".github/copilot-instructions.md", "nested:file")
        self.assertTrue(heuristics.heuristic_matches(file_row, "pkg/.github/copilot-instructions.md"))
        self.assertTrue(heuristics.heuristic_matches(file_row, "a/b/.github/Copilot-Instructions.md"))
        self.assertFalse(heuristics.heuristic_matches(file_row, ".github/copilot-instructions.md"))
        self.assertFalse(heuristics.heuristic_matches(file_row, "pkg/github/copilot-instructions.md"))
        tree_row = self._row(".github/instructions", "nested:tree_suffix:.instructions.md")
        self.assertTrue(heuristics.heuristic_matches(tree_row,
                                                     "pkg/.github/instructions/a/b.instructions.md"))
        self.assertFalse(heuristics.heuristic_matches(tree_row, "pkg/.github/instructions/b.md"))
        self.assertFalse(heuristics.heuristic_matches(tree_row,
                                                      ".github/instructions/b.instructions.md"))
        name_row = {**self._row(".mcp.json", "nested:file"), "mechanism": "mcp"}
        self.assertTrue(heuristics.heuristic_matches(name_row, "pkg/.mcp.json"))
        self.assertTrue(heuristics.heuristic_matches(name_row, "a/b/.mcp.json"))
        self.assertFalse(heuristics.heuristic_matches(name_row, ".mcp.json"))
        self.assertFalse(heuristics.heuristic_matches(name_row, "pkg/x.mcp.json"))

    def test_the_inner_mode_decides_whether_it_names_a_directory(self):
        self.assertTrue(is_directory_mode("nested:skill_dir"))
        self.assertTrue(is_directory_mode("nested:tree_ext_described:.md"))
        self.assertFalse(is_directory_mode("nested:file"))
        self.assertIsNone(heuristics.nested_inner("file"))
        self.assertEqual(heuristics.nested_inner("nested:tree_suffix:.x"), "tree_suffix:.x")

    def test_the_artifact_matches_the_nested_row_at_its_own_depth(self):
        rows = [r for r in ROWS if r["path"] == ".github/agents"]
        matched, depth = heuristics.match_heuristics(rows, "subagents", "pkg/.github/agents/a.md")
        self.assertEqual(([r["match_mode"] for r in matched], depth),
                         (["nested:tree_ext_described:.md"], 0))
        matched, depth = heuristics.match_heuristics(rows, "subagents", ".github/agents/a.md")
        self.assertNotIn("nested:tree_ext_described:.md", [r["match_mode"] for r in matched])


class TestTheDirectoryModesHaveOneDefinition(unittest.TestCase):
    """The validator's marker list, the detector's marker list and paths_under derive from one
    definition of which match modes name a directory. A mode added to one and not the others
    is the inconsistency that the heuristics exist to prevent, and no error shows it. A
    directory missing from the detector's copy stops being recognized as a marker. Its target
    is then never resolved, and the configuration artifacts behind it are recorded as `other`
    or not at all."""

    def test_every_match_mode_is_documented(self):
        """The docstring of heuristics.py states that the set of modes is closed and then
        lists it. A mode that the heuristics use but the list omits therefore makes the
        documentation wrong without an error."""
        from agentcontext import heuristics
        # A parametric mode is documented with its parameter attached ("dir_ext:.a|.b"), and
        # the check is that a line of the list starts with the mode name.
        documented = {line.split()[0].split(":")[0]
                      for line in heuristics.__doc__.splitlines()
                      if line and not line[0].isspace() and line.split()}
        undocumented = sorted({r["match_mode"].split(":")[0] for r in ROWS} - documented)
        self.assertEqual(undocumented, [],
                         f"match modes used by the heuristics but absent from the module "
                         f"docstring's list: {undocumented}")

    def test_the_two_steps_derive_the_same_list(self):
        self.assertEqual(DETECT._MARKER_DIRS, VALIDATE.MECHANISM_DIRS)

    def test_the_list_is_every_directory_row_and_nothing_else(self):
        """Deduplicated by path, which `directory_paths` documents: the `workflow_uses:` rows
        name one directory between them, because their tool comes from the action that a file
        runs rather than from where the file is."""
        expected = list(dict.fromkeys(r["path"] for r in ROWS
                                      if is_directory_mode(r["match_mode"])
                                      and not heuristics.plugin_placeholder(r["path"])))
        self.assertEqual(DETECT._MARKER_DIRS, expected)
        self.assertTrue(expected)
        self.assertEqual(len(DETECT._MARKER_DIRS), len(set(DETECT._MARKER_DIRS)))

    def test_every_match_mode_that_the_heuristics_use_is_classified(self):
        """A new parametric mode has to be classified deliberately, not by falling through to
        False because its prefix is missing from DIRECTORY_MODES."""
        for mode in sorted({heuristics.nested_inner(r["match_mode"]) or r["match_mode"]
                            for r in ROWS}):
            with self.subTest(mode=mode):
                self.assertEqual(is_directory_mode(heuristics.NESTED_PREFIX + mode),
                                 is_directory_mode(mode))
                known = mode in SIMPLE_MODES or mode.startswith(PARAMETRIC_MODES
                                                                 + DECLARING_MODES
                                                                 + MANIFEST_MODES
                                                                 + JSON_KEY_MODES)
                self.assertTrue(known, f"{mode} is outside the closed vocabulary")
                self.assertEqual(is_directory_mode(mode),
                                 mode in ("skill_dir", "skill_dir_flat") or mode.startswith(PARAMETRIC_MODES))

    def test_a_directory_mode_not_in_the_predicate_is_caught(self):
        """The failure itself: a mode that names a directory but for which is_directory_mode
        returns False would drop every path that uses it from the marker lists of both steps
        without an error."""
        self.assertTrue(is_directory_mode("skill_dir"))
        for prefix in PARAMETRIC_MODES:
            self.assertTrue(is_directory_mode(prefix + ".md"))
        for mode in ("file", "root_file", "basename", "json_hooks", "spec_dir"):
            self.assertFalse(is_directory_mode(mode))


class TestNoDriftFromCode(unittest.TestCase):
    """No step names a path pattern that the heuristics do not list."""

    def known_paths(self):
        paths = {r["path"] for r in ROWS}
        # A path with a wildcard is stored whole. Its parts appear in code.
        for r in ROWS:
            if "*" in r["path"]:
                paths.update(p for p in r["path"].split("/") if p and p != "*")
        # Extensions are part of the match modes rather than paths of rows.
        for r in ROWS:
            mode = r["match_mode"]
            if mode.startswith(("dir_ext:", "tree_ext:")):
                paths.update(extensions_for(mode))
            elif mode.startswith("tree_suffix:"):
                paths.add(mode.split(":", 1)[1])
        # The registered subagent directory format fixes this marker filename.
        # The end-to-end registration test checks its discovery.
        paths.add("agent.md")
        return paths | NON_ARTIFACT_LITERALS

    def assert_no_unknown_literals(self, filename):
        known = self.known_paths()
        # A lower-case name is known when it is a row's file name in any case, the last part
        # of its path included: copilot-instructions.md is .github/copilot-instructions.md.
        known_names = {p.rsplit("/", 1)[-1].lower() for p in known}
        unknown = sorted(l for l in path_literals(filename)
                         if l not in known
                         and not (LOWER_CASE_NAME.match(l) and l in known_names))
        self.assertEqual(unknown, [],
                         f"{filename} names paths that are not in config_path_patterns.csv: "
                         f"{unknown}. Add them to the heuristics, or to "
                         f"NON_ARTIFACT_LITERALS if they configure nothing.")

    def test_the_cloning_step_names_only_paths_of_the_heuristics(self):
        self.assert_no_unknown_literals("agentcontext/paths.py")
        self.assert_no_unknown_literals("agentcontext/collector.py")
        self.assert_no_unknown_literals("agentcontext/steps/repository_cloning.py")

    def test_a_lower_case_name_is_read(self):
        """The pattern matches lower-case literals too, because the collectors compare file names
        in lower case."""
        for name in ("agents.md", "agents.local.md", "managed-settings.json", "gemini.md",
                     "skill.md", "config.toml", "settings.yaml", "rules.mdc"):
            with self.subTest(name=name):
                self.assertRegex(name, PATH_LITERAL)
        for text in ("utf-8", "repos.csv", "agents", "Agents.local", "-x.md", "readme"):
            with self.subTest(text=text):
                self.assertNotRegex(text, PATH_LITERAL)

    def test_the_collected_file_cleanup_names_only_paths_of_the_heuristics(self):
        self.assert_no_unknown_literals("agentcontext/steps/collected_file_cleanup.py")

    def test_detection_names_only_paths_of_the_heuristics(self):
        self.assert_no_unknown_literals("agentcontext/steps/detection.py")


class TestEveryRowIsConsumed(unittest.TestCase):
    """Every heuristic is read by at least one step."""

    def consumed_paths(self):
        d, v, c = DETECT, VALIDATE, COLLECT
        consumed = set()
        for names, mod in (
            (("SETTINGS_PATHS", "SKILLS_DIRS", "SKILL_COMMAND_DIRS", "SKILL_WORKFLOW_DIRS",
              "SUBAGENT_DIRS_MD", "SUBAGENT_DIRS_MARKDOWN", "SUBAGENT_DIRS_NAMED", "SUBAGENT_DIRS_MD_FLAT", "SUBAGENT_DIRS_MD_FLAT_DESCRIBED", "PLUGIN_ROOT_SKILLS",
              "SUBAGENT_DIRS_TOML", "COMMAND_DIRS_TOML", "COMMAND_DIRS_MD",
              "COMMAND_DIRS_MD_FLAT", "COMMAND_DIRS_TEXT", "COMMAND_DIRS_PROMPT", "HOOKS_JSON_FILES",
              "HOOKS_MODULE_FILES",
              "HOOKS_NAMED_FILES", "HOOKS_DIRS_JSON", "RULES_DIRS_CODEX", "RULES_DIRS_CURSOR",
              "RULES_DIRS_CLAUDE", "RULES_DIRS_MARKDOWN", "RULES_DIRS_MD_FLAT", "RULES_DIRS_DESCRIBED", "RULES_DIRS_TRIGGERED", "RULES_FILES",
              "SPEC_DIRS", "ROOT_SPEC_FILES", "CONTEXT_FILENAMES", "CONTEXT_FILES", "ROOT_CONTEXT_FILES",
              "CONTEXT_DIRS_SUFFIX", "SETTINGS_BASENAMES", "SETTINGS_DIRS_TOML", "SETTINGS_DIRS_JSON",
              "PLUGIN_MODULE_DIRS", "PLUGIN_PACKAGE_DIRS",
              "HOOKS_TOML_FILES", "MCP_BASENAMES", "MCP_AGENT_PLUGINS",
              "COPILOT_MARKETPLACES", "PLUGIN_FILES", "PLUGIN_BASENAMES", "PLUGIN_MANIFESTS", "LSP_FILES"), d),
            (("SPEC_DIRS", "SPEC_FILES", "ROOT_ONLY_FILES", "SKILL_VALID_PARENTS",
              "CLAUDE_PLUGIN_PARENT_KEEP_EXTRA", "COPILOT_TRIGGERS"), v),
            (("_SKILL_MD_SPEC_DIRS", "_CLAUDE_PLUGIN_SIBLINGS", "_GEMINI_MISC_NAMES"), c),
        ):
            for name in names:
                consumed.update(getattr(mod, name))
        consumed.update(r["path"] for r in d.REGISTRATION_ROWS)
        consumed.update(r["path"] for r in d.RULES_JSON_ROWS)
        consumed.update(path for rows in d.JSON_KEY_ROWS.values() for path, _ in rows)
        consumed.update(d.MCP_FILES)
        consumed.update({d.MCP_ROOT_FILE, *d.MCP_JSON_FILES, *d.MCP_PLUGIN_FILES, d.MCP_CODEX_CONFIG,
                         d.SKILL_MARKER, *d.AGENT_MEMORY_PATTERNS,
                         *(path for path, _ in d.HOOKS_NAME_GLOBS)})
        consumed.update(".github/" + rel for rel, _ in COLLECT._GITHUB_KEEP)
        # Some rows take effect through a literal rather than through a constant, such as
        # .claude-plugin and .github, which the collector names directly. Read those
        # literals from the sources, not from the heuristics, or a row that no step reads
        # would look consumed merely because it is in the file being checked.
        for filename in ("agentcontext/paths.py", "agentcontext/collector.py",
                         "agentcontext/steps/repository_cloning.py",
                         "agentcontext/steps/collected_file_cleanup.py", "agentcontext/steps/detection.py"):
            consumed.update(path_literals(filename))
        return consumed

    def test_no_row_is_ignored_by_every_step(self):
        consumed = self.consumed_paths()
        ignored = []
        for row in ROWS:
            path = row["path"]
            if path in consumed:
                continue
            # A .github child is consumed under its bare name by the trigger list.
            if path.startswith(".github/") and path.split("/")[-1] in consumed:
                continue
            ignored.append(f"{path} ({row['mechanism']}/{row['match_mode']})")
        self.assertEqual(ignored, [],
                         f"heuristics that no step reads: {ignored}")


class TestEveryHeuristicIsApplied(unittest.TestCase):
    """Every detected row reaches a constant that the detector iterates.

    Comparing a constant with the expression that defines it proves nothing, because the
    assertion and the code are the same call. This class checks instead that no heuristic
    is unreachable. Changing a row's match_mode without changing the code that reads it
    empties its constant without an error, and this class detects that.
    """

    def test_every_row_reaches_a_constant(self):
        constants = mechanism_constants()
        unreachable = []
        for row in ROWS:
            mechanism = row["mechanism"]
            if mechanism == "collect_only":
                continue  # consumed by the collector, checked separately
            reachable = set()
            for group in constants[mechanism]:
                reachable.update(group)
            if row["path"] not in reachable:
                unreachable.append(f"{row['path']} ({mechanism}/{row['match_mode']})")
        self.assertEqual(unreachable, [],
                         f"heuristics that the detector can never apply: {unreachable}. "
                         f"A match_mode that the code does not read empties its constant.")

    def test_a_changed_match_mode_is_caught(self):
        """The check fails when a row's mode stops matching its constant."""
        constants = mechanism_constants()
        reachable = set()
        for group in constants["subagents"]:
            reachable.update(group)
        self.assertIn(".claude/agents", reachable)
        # The failure that this test detects: the row is still in subagents, but its
        # path no longer appears in any constant that the detector iterates.
        moved = [r for r in ROWS if r["path"] == ".claude/agents"][0]
        self.assertEqual(moved["match_mode"], "tree_ext:.md")


class TestConstantsMatchHeuristics(unittest.TestCase):
    """The detector's constants match the heuristics."""

    def test_order_bearing_constants_are_pinned(self):
        """The order of the heuristics is the order of the output, and the test asserts it
        rather than deriving it.

        The detector iterates these constants in file order, every mechanism CSV has that
        order, and `heuristics_sha256` in output/provenance.json changes with the file.
        Sorting the heuristics would change the published row order without an error, and
        the expected lists are therefore written out here.
        """
        self.assertEqual(DETECT.SETTINGS_PATHS, [
            ".claude/settings.json", ".claude/settings.local.json",
            ".gemini/settings.json", ".gemini/config.yaml", ".codex/config.toml",
            ".cursor/cli.json", ".cursor/environment.json", ".cursor/Dockerfile",
            ".github/copilot/settings.json",
            ".github/copilot/settings.local.json", ".github/allowed_models.txt",
            ".github/workflows/copilot-setup-steps.yml",
            ".github/workflows/copilot-code-review.yml", ".github/workflows/claude.yml",
            ".github/workflows/claude-code-review.yml", ".github/workflows/gemini-dispatch.yml",
            ".claude/launch.json", ".cursor/sandbox.json",
            ".cursor/permissions.json", "<claude-plugin>/settings.json",
            ".cursor/worktrees.json", ".cursor/settings.json",
            ".github/github-app.yml", ".github/copilot-desktop.yml", ".gemini/config.json",
            ".cursor/config/bugbot.yaml", "opencode.json", "opencode.jsonc",
            ".opencode/opencode.json", ".opencode/opencode.jsonc", "tui.json", "tui.jsonc",
            ".opencode/tui.json", ".opencode/tui.jsonc", ".github/workflows/opencode.yml",
        ])
        self.assertEqual(DETECT.SKILLS_DIRS, [
            ".claude/skills", ".gemini/skills", ".codex/skills", ".github/skills",
            ".cursor/skills", ".agents/skills", "<claude-plugin>/skills", "<cursor-plugin>/skills",
            "<codex-plugin>/skills", "<agent-plugin>/skills", "<copilot-plugin>/skills",
            "<gemini-extension>/skills", ".agent/skills", "<antigravity-plugin>/skills",
            "_agents/skills", "_agent/skills", ".opencode/skills", ".opencode/skill",
        ])
        self.assertEqual(DETECT.HOOKS_JSON_FILES, [
            ".claude/settings.json", ".gemini/settings.json",
            ".claude/settings.local.json", ".codex/hooks.json", ".cursor/hooks.json",
            "<codex-plugin>/hooks/hooks.json", "<agent-plugin>/hooks/hooks.json", "<copilot-plugin>/hooks.json|hooks/hooks.json",
            "<gemini-extension>/hooks/hooks.json",
            "<agent-plugin>/com.github.copilot/hooks/hooks.json",
            "<cursor-plugin>/hooks/hooks.json",
            ".github/copilot/settings.json", ".github/copilot/settings.local.json",
        ])
        self.assertEqual(DETECT.SPEC_DIRS, [
            ".claude", ".gemini", ".cursor", ".codex", ".agents", ".agent",
            "_agents", "_agent", ".opencode",
            ".github", ".cursor-plugin", ".codex-plugin", ".claude-plugin", ".plugin",
        ])
        self.assertEqual(DETECT.SPEC_BASENAMES,
                         [".geminiignore", ".cursorignore", ".cursorindexingignore",
                          ".aiexclude", ".antigravityignore"])

    def test_every_basename_row_survives_validation(self):
        """A basename row that the validator removes is collected and then deleted.

        A list of kept files that names mechanisms by hand misses a row that moves to
        another mechanism, which the collector still copies.
        """
        basenames = {r["path"] for r in ROWS if r["match_mode"] == "basename"}
        self.assertTrue(basenames <= VALIDATE.SPEC_FILES,
                        f"pruned by the validator: {sorted(basenames - VALIDATE.SPEC_FILES)}")

    def test_prompt_suffix_matches_its_match_mode(self):
        modes = {r["match_mode"] for r in ROWS if r["mechanism"] == "commands"
                 and r["path"] in DETECT.COMMAND_DIRS_PROMPT}
        self.assertEqual(modes, {"dir_ext:" + DETECT.PROMPT_SUFFIX})

    def test_cursor_rules_extensions_match_the_detector(self):
        """The suffixes that detect_rules applies are the ones that the heuristics declare."""
        modes = {r["match_mode"] for r in ROWS
                 if r["path"] in DETECT.RULES_DIRS_CURSOR and r["mechanism"] == "rules"}
        self.assertEqual(len(modes), 1)
        self.assertEqual(DETECT.RULES_CURSOR_EXTENSIONS, extensions_for(next(iter(modes))))

    def test_validator_keeps_only_the_always_valid_spec_dirs(self):
        """The validator's spec directories are the ones kept whole, not the directories in
        which the files that no row matches become `other` rows.

        .github contains workflows and issue templates as well as Copilot configuration. Its
        files reach the detector's `other` column, but the validator removes files from
        it rather than keeping it whole. The expected set is written out, because the
        `paths_for` call that defines the constant would change both sides at once when a
        .github row became a spec_dir.
        """
        self.assertNotIn(".github", VALIDATE.SPEC_DIRS)
        self.assertEqual(VALIDATE.SPEC_DIRS,
                         {".claude", ".gemini", ".cursor", ".codex", ".opencode", ".agents", ".agent",
                      "_agents", "_agent"})
        self.assertEqual(set(DETECT.SPEC_DIRS),
                         VALIDATE.SPEC_DIRS | {".github", ".cursor-plugin", ".codex-plugin",
                                               ".claude-plugin", ".plugin"})

    def test_collector_and_validator_agree_on_skill_parents(self):
        self.assertEqual(set(COLLECT._SKILL_MD_SPEC_DIRS), VALIDATE.SKILL_VALID_PARENTS)


class TestTheRealHeuristics(unittest.TestCase):

    def test_it_loads(self):
        self.assertTrue(COMMIT_ROWS)

    def test_no_pattern_is_written_twice_in_one_scope(self):
        keys = [(r["tool"], r["scope"], r["pattern"]) for r in COMMIT_ROWS]
        duplicates = sorted({k for k in keys if keys.count(k) > 1})
        self.assertEqual(duplicates, [])

    def test_every_tool_has_at_least_one_heuristic_in_some_scope(self):
        # The six tools are written out. Iterating over the tools that the rows name would
        # miss a tool with all of its rows deleted.
        for tool in ("Claude", "Copilot", "Cursor", "Codex", "Gemini", "OpenCode"):
            with self.subTest(tool=tool):
                self.assertTrue([r for r in COMMIT_ROWS if r["tool"] == tool])
        self.assertEqual(set(heuristics.ai_commit_tools(COMMIT_ROWS)),
                         {"Claude", "Copilot", "Cursor", "Codex", "Gemini", "OpenCode"})

    def test_a_trailer_pattern_starts_with_a_trailer_token(self):
        # A pattern in the trailer scope is matched against the trailer block alone, and a
        # pattern that is not anchored to a token would be a heuristic that can never
        # match.
        # A choice of tokens before `-by:`, as in `(?:co-authored|assisted|generated)-by:`,
        # is anchored the same way, and so is a choice of whole tokens, as in
        # `(?:authored-by|made-with):`, which can contain such a choice before `-by`, as in
        # `(?:(?:co-authored|assisted)-by|made-with):`. The patterns of the tools outside the six
        # are matched the same way and must have the same form.
        token = r"[a-z][a-z0-9-]*"
        stems = r"\(\?:[a-z-]+(?:\|[a-z-]+)+\)-by"
        choice = rf"\(\?:(?:{token}|{stems})(?:\|(?:{token}|{stems}))+\)"
        for row in COMMIT_ROWS + OTHER_COMMIT_ROWS:
            if row["scope"] != "trailer":
                continue
            with self.subTest(pattern=row["pattern"]):
                self.assertRegex(row["pattern"], rf"^(?:{token}|{stems}|{choice}):",
                                 "a trailer heuristic has to begin with its token")
                # An alternation at the top level would let its second branch begin
                # anywhere, token or not.
                self.assertFalse(_top_level_bar(row["pattern"]),
                                 "a top-level alternative would begin without its token")

    def test_the_other_tools_file_refuses_a_row_of_the_six(self):
        """A row that names one of the six would put that tool's commits in
        `output/other_ai_commits.csv` and not in the commit CSV, from which every count of the
        six is read."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other_ai_commit_patterns.csv"
            path.write_text("id,tool,scope,attribution,pattern,source,doc_url,doc_verified,evidence,notes\n"
                            "amp-trailer-01,Amp,trailer,standard,co-authored-by:\\s*amp,sample,,,https://example.test/c,read off the collection\n"
                            "claude-trailer-01,claude,trailer,standard,co-authored-by:\\s*claude,sample,,,https://example.test/c,read off the collection\n",
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "The rows of claude in .* belong in "
                                                    "ai_commit_patterns.csv"):
                heuristics.load_other_ai_commit_patterns(path)
            path.write_text("id,tool,scope,attribution,pattern,source,doc_url,doc_verified,evidence,notes\n"
                            "amp-trailer-01,Amp,trailer,standard,co-authored-by:\\s*amp,sample,,,https://example.test/c,read off the collection\n",
                            encoding="utf-8")
            self.assertEqual([r["tool"] for r in heuristics.load_other_ai_commit_patterns(path)],
                             ["Amp"])

    def test_the_scan_finds_a_bar_outside_every_group_and_class(self):
        # re's parser cannot make this check, because it factors a prefix that the branches
        # share out of the alternation. `co-authored-by:x|codex` parses as `c`, `o` and a
        # branch, and a check over that parse would pass a pattern with a second branch that
        # has no token.
        cases = {r"co-authored-by:\s*x|codex@openai\.com": True, r"ab|cd": True,
                 r"(?:a|b)|c": True, r"(?:co-authored|assisted)-by:x": False,
                 r"a-by:(?:x|y)": False, r"a-by:[|]": False, r"a-by:\|": False,
                 r"a-by:[\]|]": False}
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertIs(_top_level_bar(pattern), expected)

    def test_a_full_body_pattern_is_not_a_trailer_in_disguise(self):
        # The other direction: an attribution line in the full-body scope would match
        # prose that quotes it, which the scopes exist to prevent. The tokens are those that the
        # trailer heuristics read.
        for row in COMMIT_ROWS:
            if row["scope"] != "full_body":
                continue
            with self.subTest(pattern=row["pattern"]):
                self.assertNotRegex(
                    row["pattern"],
                    r"^(?:(?:co-authored|authored|assisted|created|generated|written)-by"
                    r"|made-with):")

    def test_every_doc_row_cites_a_page_and_a_date(self):
        for row in COMMIT_ROWS:
            if row["source"] != "doc":
                continue
            with self.subTest(pattern=row["pattern"]):
                self.assertTrue(row["doc_url"])
                self.assertRegex(row["doc_verified"], r"^\d{4}-\d{2}-\d{2}$")

    def test_every_sample_row_states_what_was_observed(self):
        # A sample heuristic states in its notes what was observed.
        for row in COMMIT_ROWS:
            if row["source"] != "sample":
                continue
            with self.subTest(pattern=row["pattern"]):
                self.assertTrue(row["notes"].strip())

    def test_a_cited_page_is_dated_for_every_source(self):
        # A sample heuristic may cite a page too. The documentation watch reads every cited
        # page again, and an undated citation is one that nobody has read.
        for row in COMMIT_ROWS:
            if not row["doc_url"]:
                continue
            with self.subTest(pattern=row["pattern"]):
                self.assertRegex(row["doc_verified"], r"^\d{4}-\d{2}-\d{2}$")

    def test_a_note_names_a_collection_of_october_2026_by_its_dates(self):
        """Two collections of the sample ran in October 2026, on October 2–3 and on October
        5–6. A note that counts commits of "the October 2026 collection" therefore does not say
        which commits it counted."""
        for row in COMMIT_ROWS + OTHER_COMMIT_ROWS:
            with self.subTest(id=row["id"]):
                self.assertNotRegex(row["notes"], r"\bOctober 2026 collections?\b")

    def test_a_note_gives_no_owner_to_the_apps_of_github_s_partner_agents(self):
        """GitHub installs the apps `anthropic-code-agent` and `openai-code-agent` for its partner
        agents for Claude and Codex, and its API returned no record of either app on 2026-10-21
        (`GET /apps/anthropic-code-agent` and `GET /apps/openai-code-agent` returned 404). A note
        of a row that reads one of the apps therefore states that no page names its owner, and
        names no vendor as the owner."""
        rows = [row for row in COMMIT_ROWS
                if re.search(r"(?:anthropic|openai)\[_\\-\]code\[_\\-\]agent", row["pattern"])]
        self.assertEqual(len(rows), 4)
        for row in rows:
            with self.subTest(id=row["id"]):
                self.assertIn("no page names its owner", row["notes"])
                self.assertNotRegex(row["notes"], r"\b(?:Anthropic|OpenAI)'s (?:second )?app\b")


class TestTheDetectorHasNoCopyOfThePatterns(unittest.TestCase):
    """agentcontext.commits reads the commit patterns from the heuristics, and this class
    checks that it still does rather than keeping a copy of them."""

    def test_the_compiled_set_is_the_heuristics(self):
        from_heuristics = {(r["tool"], r["scope"], r["pattern"]) for r in COMMIT_ROWS}
        from_detector = set()
        for tool, author, trailer, full_body in _detect._COMPILED_PATTERNS:
            for scope, compiled in (("author", author), ("trailer", trailer),
                                    ("full_body", full_body)):
                for rx in compiled:
                    from_detector.add((tool, scope, heuristics.heuristic_pattern(rx)))
        self.assertEqual(from_detector, from_heuristics)

    def test_every_pattern_is_compiled_case_insensitively(self):
        for tool, *scopes in _detect._COMPILED_PATTERNS:
            for compiled in scopes:
                for rx in compiled:
                    with self.subTest(tool=tool, pattern=rx.pattern):
                        self.assertTrue(rx.flags & re.IGNORECASE)

    def test_the_validation_script_reads_the_same_heuristics(self):
        # The check matches the message again through the detector and classifies a commit by
        # the heuristics that match it, and a second copy of them there would give a
        # classification that the cloning step never used.
        for check, rows in ((_validate.TOOLS, _detect.AI_COMMIT_PATTERNS),
                            (_validate.OTHER_TOOLS, _detect.OTHER_AI_COMMIT_PATTERNS)):
            self.assertEqual(list(check.by_id), [row["id"] for row in rows])
            for row in rows:
                with self.subTest(id=row["id"]):
                    self.assertIs(check.by_id[row["id"]], row)


class TestTheHeuristicsMatchThePublishedData(unittest.TestCase):
    """What the heuristics alone cannot check: a tool that ai_commit_patterns.csv names and
    the commit CSV does not contain, or a tool in the commit CSV that ai_commit_patterns.csv
    does not name.

    The tests read the commit CSV as the release reads it, from the tracked archive when the
    CSV is not extracted, because the CSV is gitignored and reading it alone would skip both
    tests in CI and in every clean checkout.
    """

    @classmethod
    def setUpClass(cls):
        publish = importlib.import_module("agentcontext.steps.release")
        cls.work = Path(tempfile.mkdtemp())
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                source = publish.commits_source(PUBLISHED.parent, cls.work)
        except (FileNotFoundError, RuntimeError) as e:
            shutil.rmtree(cls.work, ignore_errors=True)
            raise unittest.SkipTest(f"the commit CSV cannot be read here: {e}")
        csv.field_size_limit(sys.maxsize)
        cls.published = set()
        with source.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cls.published.update(t for t in row["ai_tool"].split(",") if t)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    def _published_tools(self) -> set:
        return self.published

    def test_every_published_tool_is_in_the_heuristics(self):
        self.assertEqual(self._published_tools() - set(heuristics.ai_commit_tools(COMMIT_ROWS)), set())

    def test_every_heuristics_tool_appears_in_the_published_data(self):
        # Not a necessity, but a tool with no commit in the commit CSV is a tool with
        # heuristics that have never matched, which this test reports.
        self.assertEqual(set(heuristics.ai_commit_tools(COMMIT_ROWS)) - self._published_tools(), set())


class TestWhatInvalidHeuristicsDo(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, *rows) -> Path:
        path = self.tmp / "heuristics.csv"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=heuristics.AI_COMMIT_FIELDNAMES,
                                    lineterminator="\n")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def _row(self, **overrides) -> dict:
        row = {"id": "claude-author-01", "tool": "Claude", "scope": "author", "attribution": "account",
               "pattern": r"claude", "source": "doc", "doc_url": "https://example.test/a",
               "doc_verified": "2026-09-12", "evidence": "", "notes": ""}
        row.update(overrides)
        return row

    def test_a_valid_row_loads(self):
        self.assertEqual(len(heuristics.load_ai_commit_patterns(self._write(self._row()))), 1)

    def test_a_pattern_longer_than_the_default_field_limit_loads_in_a_fresh_process(self):
        """A process that imports only `agentcontext.heuristics` reads a pattern of more than
        the 131,072 characters that the csv module accepts by default."""
        path = self._write(self._row(pattern="claude(?#" + "x" * 200_000 + ")"))
        script = ("import sys; from agentcontext import heuristics; "
                  "print(len(heuristics.load_ai_commit_patterns(sys.argv[1])))")
        result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True,
                                text=True, cwd=REPO_ROOT,
                                env={**os.environ, "PYTHONPATH": str(REPO_ROOT)})
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "1"), result.stderr)

    def test_an_id_is_the_tool_the_scope_and_a_number(self):
        """`matched_heuristics` names a heuristic by its id, and an id that does not name its
        tool and scope, or two rows with one id, would make a published value
        ambiguous."""
        refused = {
            "": self._row(id=""),
            "no number": self._row(id="claude-author"),
            "one digit": self._row(id="claude-author-1"),
            "another tool": self._row(id="copilot-author-01"),
            "another scope": self._row(id="claude-trailer-01"),
            "the scope's underscore": self._row(id="claude-full_body-01", scope="full_body",
                                                attribution="convention"),
        }
        for reason, row in refused.items():
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError) as caught:
                    heuristics.load_ai_commit_patterns(self._write(row))
                self.assertIn("The id of", str(caught.exception))
        loaded = heuristics.load_ai_commit_patterns(self._write(
            self._row(id="gemini-unspecified-full-body-12", tool="Gemini (unspecified)",
                      scope="full_body", attribution="convention")))
        self.assertEqual(loaded[0]["id"], "gemini-unspecified-full-body-12")

    def test_two_rows_with_one_id_are_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(
                self._row(), self._row(pattern=r"claude-code")))
        self.assertIn("is also the id of another row", str(caught.exception))

    def test_the_attribution_is_one_of_the_four_and_account_is_the_author_scope(self):
        cases = {
            "an unknown attribution": self._row(attribution="bot"),
            "a retired attribution name": self._row(id="claude-trailer-01", scope="trailer",
                                                    attribution="product"),
            "an author heuristic that is not account": self._row(attribution="standard"),
            "a trailer heuristic that is account": self._row(id="claude-trailer-01",
                                                             scope="trailer"),
        }
        for reason, row in cases.items():
            with self.subTest(reason=reason):
                with self.assertRaises(ValueError) as caught:
                    heuristics.load_ai_commit_patterns(self._write(row))
                self.assertRegex(str(caught.exception),
                                 "The attribution of .* is|has the attribution")

    def test_an_author_heuristic_can_have_the_attribution_convention(self):
        """A name or an address that names the tool and that users chose for the author or the
        committer is `convention`, as the paragraph "Attributions" of docs/heuristics.md defines
        it, and an author heuristic can have that attribution."""
        loaded = heuristics.load_ai_commit_patterns(self._write(self._row(attribution="convention")))
        self.assertEqual(loaded[0]["attribution"], "convention")

    def test_a_row_that_takes_the_id_of_a_removed_heuristic_is_refused(self):
        """An id in a commit CSV of an earlier collection names the removed heuristic, and a new
        row under that id would make it name two."""
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(
                id="claude-full-body-10", scope="full_body", attribution="convention")))
        self.assertIn("was the id of a removed heuristic", str(caught.exception))

    def test_every_id_in_the_history_of_the_two_files_is_a_row_or_removed(self):
        """A row removed from a commit heuristics file leaves its id in
        REMOVED_COMMIT_HEURISTIC_IDS, so that no later row takes it. A shallow clone reads only
        the history that it has."""
        current = {row["id"] for row in heuristics.load_ai_commit_patterns()
                   + heuristics.load_other_ai_commit_patterns()}
        listed = subprocess.run(
            ["git", "log", "-p", "--format=", "--", "heuristics/ai_commit_patterns.csv",
             "heuristics/other_ai_commit_patterns.csv"],
            capture_output=True, text=True, cwd=str(REPO_ROOT))
        if listed.returncode != 0:
            self.skipTest("not inside a git checkout")
        ever = set(re.findall(r"^[+-]([a-z0-9]+(?:-[a-z0-9]+)*-\d{2,}),", listed.stdout, re.M))
        self.assertEqual(ever - current - heuristics.REMOVED_COMMIT_HEURISTIC_IDS, set())
        self.assertEqual(heuristics.REMOVED_COMMIT_HEURISTIC_IDS & current, set())

    def test_every_id_of_the_two_files_is_its_own(self):
        """The two commit CSVs can be combined as they are, and an id of one heuristics file
        must not name a row of the other."""
        ids = [row["id"] for row in heuristics.load_ai_commit_patterns()
               + heuristics.load_other_ai_commit_patterns()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_a_missing_column_is_refused(self):
        path = self.tmp / "short.csv"
        path.write_text("id,tool,scope,attribution,pattern\nclaude-author-01,Claude,author,account,claude\n",
                        encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(path)
        self.assertIn("source", str(caught.exception))

    def test_an_empty_heuristics_is_refused(self):
        path = self.tmp / "empty.csv"
        path.write_text(",".join(heuristics.AI_COMMIT_FIELDNAMES) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            heuristics.load_ai_commit_patterns(path)

    def test_an_unknown_scope_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(scope="subject")))
        self.assertIn("subject", str(caught.exception))

    def test_an_unknown_source_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(source="guess")))
        self.assertIn("guess", str(caught.exception))

    def test_a_pattern_that_does_not_compile_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(pattern=r"claude[")))
        self.assertIn("compile", str(caught.exception))

    def test_a_doc_row_with_no_page_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(doc_url="", doc_verified="")))
        self.assertIn("doc_url", str(caught.exception))

    def test_a_cited_page_with_no_date_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(doc_verified="")))
        self.assertIn("doc_verified", str(caught.exception))

    def test_a_sample_row_with_no_evidence_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(
                self._row(source="sample", doc_url="", doc_verified="", notes="read off")))
        self.assertIn("has the source sample and no evidence", str(caught.exception))

    def test_a_sample_row_with_its_commits_linked_loads(self):
        row = self._row(source="sample", doc_url="", doc_verified="", notes="read off",
                        evidence="https://github.com/o/r/commit/1 https://github.com/o/s/commit/2")
        self.assertEqual(len(heuristics.load_ai_commit_patterns(self._write(row))), 1)

    def test_evidence_that_is_not_a_link_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(evidence="o/r@1234")))
        self.assertRegex(str(caught.exception),
                         "The evidence 'o/r@1234' of .* is not an https link")

    def test_a_sample_row_with_no_note_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(
                self._row(source="sample", doc_url="", doc_verified="", notes="")))
        self.assertIn("sample", str(caught.exception))

    def test_every_problem_is_reported_not_just_the_first(self):
        with self.assertRaises(ValueError) as caught:
            heuristics.load_ai_commit_patterns(self._write(self._row(scope="subject", source="guess")))
        message = str(caught.exception)
        self.assertIn("subject", message)
        self.assertIn("guess", message)

    def test_a_comment_row_is_skipped(self):
        path = self._write(self._row(pattern="# a note to a reader"), self._row())
        self.assertEqual(len(heuristics.load_ai_commit_patterns(path)), 1)


class TestLoadVendoredDirs(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_set(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("node_modules\nvendor\n")
        result = load_vendored_dirs(f)
        self.assertIsInstance(result, set)

    def test_entries_are_stripped(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("  node_modules  \nvendor\n")
        result = load_vendored_dirs(f)
        self.assertIn("node_modules", result)

    def test_blank_lines_skipped(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("node_modules\n\n\nvendor\n")
        result = load_vendored_dirs(f)
        self.assertEqual(result, {"node_modules", "vendor"})

    def test_comment_lines_skipped(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("# This is a comment\nnode_modules\n# Another\nvendor\n")
        result = load_vendored_dirs(f)
        self.assertEqual(result, {"node_modules", "vendor"})

    def test_inline_comment_not_stripped(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("node_modules # JS deps\n")
        result = load_vendored_dirs(f)
        self.assertNotIn("node_modules", result)
        self.assertIn("node_modules # JS deps", result)

    def test_empty_file_returns_empty_set(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("")
        result = load_vendored_dirs(f)
        self.assertEqual(result, set())

    def test_comments_only_returns_empty_set(self):
        f = self.tmp / "vendored_dirs.txt"
        f.write_text("# comment 1\n# comment 2\n")
        result = load_vendored_dirs(f)
        self.assertEqual(result, set())


class TestLoadLocalStatePaths(unittest.TestCase):
    """The places of the tools' local state carry their grounding, as the negatives do, and each
    names a place below a tool directory that the collector copies whole."""

    HEADER = "path,tool,source,doc_url,doc_verified,evidence,notes\n"
    PATTERNS = [{"tool": "claude", "mechanism": "other", "path": ".claude",
                 "match_mode": "spec_dir"},
                {"tool": "codex", "mechanism": "other", "path": ".codex",
                 "match_mode": "spec_dir"}]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, *lines):
        f = self.tmp / "local_state_paths.csv"
        f.write_text(self.HEADER + "".join(line + "\n" for line in lines), encoding="utf-8")
        return heuristics.load_local_state_paths(f, self.PATTERNS)

    def test_grounded_places_below_a_tool_directory_load_in_file_order(self):
        rows = self._load(
            ".codex/sessions/,codex,doc,https://example.com/codex,2026-10-21,,transcripts",
            ".claude/projects/?--*/,claude,sample,,,https://github.com/o/r/blob/abc/x,Windows")
        self.assertEqual([row["path"] for row in rows], [".codex/sessions/",
                                                         ".claude/projects/?--*/"])

    def test_a_place_outside_a_tool_directory_is_refused(self):
        for path in ("node_modules/x/", "projects/", ".cursor/chats/"):
            with self.subTest(path), self.assertRaisesRegex(ValueError, "tool directory"):
                self._load(f"{path},claude,doc,https://example.com/a,2026-10-21,,why")

    def test_the_tool_directory_itself_is_refused(self):
        for path in (".claude/", ".claude", ".claude//x"):
            with self.subTest(path), self.assertRaisesRegex(ValueError, "no place"):
                self._load(f"{path},claude,doc,https://example.com/a,2026-10-21,,why")

    def test_a_pattern_that_rsync_and_fnmatch_read_differently_is_refused(self):
        for path in (".claude/**/x.jsonl", ".claude/[ab]/", ".claude/{a,b}/", "<claude-plugin>/x"):
            with self.subTest(path), self.assertRaisesRegex(ValueError, "only"):
                self._load(f'"{path}",claude,doc,https://example.com/a,2026-10-21,,why')

    def test_a_row_without_its_grounding_is_refused(self):
        for line in (".claude/debug/,claude,doc,,,,no page",
                     ".claude/debug/,claude,sample,,,,no evidence",
                     ".claude/debug/,claude,doc,https://example.com/a,2026-10-21,,"):
            with self.subTest(line), self.assertRaises(ValueError):
                self._load(line)


class TestNoPlaceOfLocalStateHoldsConfiguration(unittest.TestCase):
    """A place of the tools' local state is left out of the collection, and no path that a path
    pattern or a heuristic of the planning documents reads may lie in one. Such a place
    would take a configuration artifact or a planning document out of the dataset."""

    def test_no_path_pattern_reads_a_place(self):
        for row in heuristics.load_config_path_patterns():
            path = row["path"]
            if heuristics.plugin_placeholder(path):
                continue
            concrete = path.replace("*", "name")
            examples = ([concrete, concrete + "/name/SKILL.md", concrete + "/rule.md"]
                        if heuristics.is_directory_mode(row["match_mode"]) else [concrete])
            for example in examples:
                with self.subTest(row=f"{row['tool']} {path} {row['match_mode']}",
                                  example=example):
                    self.assertIsNone(COLLECT.local_state(example))
                    self.assertIsNone(COLLECT.local_state(f"pkg/{example}"))

    def test_no_planning_document_lies_in_a_place(self):
        from agentcontext import planning
        for row in planning.load_planning_document_patterns():
            for example in row["example"].split():
                with self.subTest(pattern=row["pattern"], example=example):
                    self.assertIsNone(COLLECT.local_state(example))


class TestTheContentFormatIsHeldBothWays(unittest.TestCase):
    """The heuristics state which files their tool reads with comments stripped, and the
    detector reads that column rather than keeping a list. The tests check both directions,
    as for every other constant here: a path marked in the heuristics and not read that way
    would publish a configuration that nobody detects, and a path read that way without the
    mark would record a configuration that the tool never applies."""

    def test_the_column_contains_only_known_values(self):
        for row in load_config_path_patterns():
            with self.subTest(path=row["path"], tool=row["tool"]):
                self.assertIn(row["content_format"], heuristics.CONTENT_FORMATS)

    # What grounds each path that is marked `jsonc`: Gemini CLI's code, which reads its settings
    # with `JSON.parse(stripJsonComments(content))`, and the Desktop page of Claude Code, which
    # states of `.claude/launch.json` "The file supports JSON with comments."
    GROUNDS = {".gemini/settings.json": "stripJsonComments",
               ".claude/launch.json": "The file supports JSON with comments."}

    def test_the_detector_reads_exactly_what_the_heuristics_marks(self):
        # Written out, because the detector's constant is the same `jsonc_paths` call and a
        # comparison of the two cannot detect a row that gained or lost the marking.
        detect = importlib.import_module("agentcontext.steps.detection")
        self.assertEqual(detect.JSONC_PATHS, set(self.GROUNDS))
        self.assertEqual(heuristics.jsonc_paths(), set(self.GROUNDS))

    def test_a_marked_row_states_what_grounds_it(self):
        """A format is a claim about the tool, and the row has to state where the claim comes
        from: for Gemini CLI its code, and for the Desktop app of Claude Code its page."""
        marked = [r for r in load_config_path_patterns() if r["content_format"] == "jsonc"]
        self.assertTrue(marked, "nothing is marked jsonc, so this test would pass vacuously")
        for row in marked:
            with self.subTest(path=row["path"]):
                self.assertIn(self.GROUNDS[row["path"]], row["notes"])

    def test_every_marked_path_is_a_path_the_heuristics_has(self):
        paths = {r["path"] for r in load_config_path_patterns()}
        self.assertLessEqual(heuristics.jsonc_paths(), paths)


class TestTheTwoNegativeFiles(unittest.TestCase):
    """`not_configuration.csv` and `not_ai_commits.csv`: what each unmatched check skips.

    They have the same fields as the files that they negate, for the same reason: a heuristic
    with no source and no date is one that nobody can check.
    """

    def _write(self, rows, fieldnames, name="not_configuration.csv"):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        path = tmp / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)
            writer.writerows(rows)
        return path

    def _paths(self, rows):
        return self._write(rows, heuristics.NOT_CONFIGURATION_FIELDNAMES)

    def test_both_committed_files_load(self):
        self.assertTrue(heuristics.load_not_configuration())
        self.assertTrue(heuristics.load_not_ai_commits())

    def test_every_row_states_why(self):
        for rows in (heuristics.load_not_configuration(), heuristics.load_not_ai_commits()):
            for row in rows:
                with self.subTest(row=row):
                    self.assertTrue(row["notes"].strip())

    def test_a_doc_row_cites_a_dated_page(self):
        for rows in (heuristics.load_not_configuration(), heuristics.load_not_ai_commits()):
            for row in rows:
                if row["source"] != "doc":
                    continue
                with self.subTest(row=row):
                    self.assertTrue(row["doc_url"].startswith("https://"))
                    self.assertRegex(row["doc_verified"], r"^\d{4}-\d{2}-\d{2}$")

    def test_the_dicts_are_keyed_on_each_file_s_own_column(self):
        self.assertEqual(set(heuristics.not_configuration()),
                         {r["path"] for r in heuristics.load_not_configuration()})
        self.assertEqual(set(heuristics.not_ai_commits()),
                         {r["pattern"] for r in heuristics.load_not_ai_commits()})

    def test_a_row_with_no_reason_is_refused(self):
        path = self._paths([[".claude/x/**", "claude", "sample", "", "", "https://example.test/f", ""]])
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_configuration(path)
        self.assertIn("no note", str(caught.exception))

    def test_a_doc_row_without_a_page_is_refused(self):
        path = self._paths([[".claude/x/**", "claude", "doc", "", "", "", "why"]])
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_configuration(path)
        self.assertIn("no doc_url", str(caught.exception))

    def test_a_cited_page_without_a_date_is_refused(self):
        path = self._paths([[".claude/x/**", "claude", "doc", "https://x.test", "", "", "why"]])
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_configuration(path)
        self.assertIn("doc_verified", str(caught.exception))

    def test_the_same_path_twice_is_refused(self):
        path = self._paths([[".claude/x/**", "claude", "sample", "", "", "https://example.test/f", "one"],
                            [".claude/x/**", "claude", "sample", "", "", "https://example.test/f", "two"]])
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_configuration(path)
        self.assertIn("second row", str(caught.exception))

    def test_a_missing_column_is_refused(self):
        path = self._write([[".claude/x/**", "why"]], ["path", "notes"])
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_configuration(path)
        self.assertIn("lacks the columns", str(caught.exception))

    def test_an_empty_file_is_refused(self):
        path = self._paths([])
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_configuration(path)
        self.assertIn("no rows", str(caught.exception))

    def test_an_unknown_source_is_refused_in_either_file(self):
        """A source outside doc, sample and dropped is refused in either negative file, and
        deleting the check that refuses one would fail this test."""
        paths_file = self._paths([[".claude/x/**", "claude", "guessed", "", "", "", "why"]])
        commits_file = self._write([["dependabot", "guessed", "", "", "", "dependency bumps"]],
                                   heuristics.NOT_AI_COMMITS_FIELDNAMES,
                                   name="not_ai_commits.csv")
        for load, path in ((heuristics.load_not_configuration, paths_file),
                           (heuristics.load_not_ai_commits, commits_file)):
            with self.subTest(file=path.name):
                with self.assertRaises(ValueError) as caught:
                    load(path)
                self.assertIn("is 'guessed', not one of", str(caught.exception))

    def test_dropped_is_a_source_a_negative_accepts(self):
        path = self._paths([[".claude/x/**", "claude", "dropped", "", "", "https://example.test/f", "why"]])
        self.assertEqual([r["source"] for r in heuristics.load_not_configuration(path)],
                         ["dropped"])

    def test_a_row_read_off_repositories_with_no_evidence_is_refused(self):
        """A `sample` or `dropped` row was read off repositories, and the links are where a
        reader checks that it was."""
        for source in ("sample", "dropped"):
            with self.subTest(source=source):
                path = self._paths([[".claude/x/**", "claude", source, "", "", "", "why"]])
                with self.assertRaises(ValueError) as caught:
                    heuristics.load_not_configuration(path)
                self.assertIn(f"has the source {source} and no evidence",
                              str(caught.exception))

    def test_a_doc_row_needs_no_evidence(self):
        path = self._paths([[".claude/x/**", "claude", "doc", "https://x.test", "2026-09-26", "",
                             "why"]])
        self.assertEqual(len(heuristics.load_not_configuration(path)), 1)

    def test_evidence_that_is_not_a_link_is_refused(self):
        path = self._write([["dependabot", "sample", "", "", "notes.md", "dependency bumps"]],
                           heuristics.NOT_AI_COMMITS_FIELDNAMES, name="not_ai_commits.csv")
        with self.assertRaises(ValueError) as caught:
            heuristics.load_not_ai_commits(path)
        self.assertRegex(str(caught.exception),
                         "The evidence 'notes.md' of .* is not an https link")

    def test_the_commit_file_is_keyed_on_pattern(self):
        path = self._write([["dependabot", "sample", "", "", "https://example.test/f", "dependency bumps"]],
                           heuristics.NOT_AI_COMMITS_FIELDNAMES, name="not_ai_commits.csv")
        self.assertEqual(heuristics.not_ai_commits(heuristics.load_not_ai_commits(path)),
                         {"dependabot": "dependency bumps"})


class TestTheAgentPluginsContentTest(unittest.TestCase):
    """One content test for three steps: the collector copies, the validator keeps and the
    detector classifies a `plugin.json` or an `mcp.json` by the schema that it declares. Many
    programs use these names, and a heuristic over the path alone would match all of
    their files."""

    MANIFEST = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",'
                ' "name": "demo"}')

    def test_the_canonical_manifest(self):
        self.assertTrue(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest", self.MANIFEST))

    def test_the_canonical_mcp_document(self):
        self.assertTrue(heuristics.is_agent_plugins_document(
            "basename_json_agent_plugins_mcp",
            '{"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
            ' "mcpServers": {}}'))

    def test_a_later_version_of_the_standard_is_still_accepted(self):
        self.assertTrue(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest",
            '{"$schema": "https://agent-plugins.org/schemas/2.0.0/plugin.schema.json",'
            ' "name": "demo"}'))

    def test_another_schema_is_rejected(self):
        self.assertFalse(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest",
            '{"$schema": "https://grafana.com/plugin.schema.json", "name": "demo"}'))

    def test_the_wrong_schema_file_of_the_same_standard_is_rejected(self):
        self.assertFalse(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest",
            '{"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
            ' "name": "demo"}'))

    def test_a_document_missing_a_required_field_is_rejected(self):
        self.assertFalse(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest",
            '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"}'))

    def test_a_document_with_no_schema_is_rejected(self):
        self.assertFalse(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest", '{"name": "demo", "mcpServers": {}}'))

    def test_malformed_json_is_rejected(self):
        self.assertFalse(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest", "{not json"))

    def test_a_json_document_that_is_not_an_object_is_rejected(self):
        self.assertFalse(heuristics.is_agent_plugins_document(
            "basename_json_plugin_manifest", '["plugin.json"]'))

    def test_the_names_come_from_the_heuristics(self):
        self.assertEqual(heuristics.agent_plugins_names(ROWS),
                         {"plugin.json": "basename_json_plugin_manifest",
                          "mcp.json": "basename_json_agent_plugins_mcp"})

    def test_every_mode_of_the_test_is_a_mode_the_heuristics_uses(self):
        used = {r["match_mode"] for r in ROWS}
        self.assertLessEqual(set(heuristics.AGENT_PLUGINS_DOCUMENTS), used)

    def test_the_three_readers_call_the_same_function(self):
        """With a second copy of the test, one step could copy a file that another step then
        deletes. The readers call `heuristics.is_agent_plugins_document` itself or
        `paths.agent_plugins_document`, which calls it."""
        import agentcontext.paths as paths_module
        import agentcontext.steps.collected_file_cleanup as validate_module
        detect = importlib.import_module("agentcontext.steps.detection")
        for module in (paths_module, validate_module, detect):
            with self.subTest(module=module.__name__):
                source = Path(module.__file__).read_text(encoding="utf-8")
                self.assertIn("agent_plugins_document(", source)
                self.assertNotIn("agent-plugins.org/schemas/", source)


if __name__ == "__main__":
    unittest.main()
