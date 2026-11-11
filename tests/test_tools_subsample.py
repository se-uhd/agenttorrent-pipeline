"""
Tests for agentcontext.tools.subsample

Strategy:
  - The selection builds a coverage matrix from tracked files and covers it greedily. The
    tests of the selection build a small stage directory in a temporary directory: a handful
    of repositories, the output CSVs, the lists and the trees, each repository with one or
    two entries of `coverage`.
  - The tests assert the matcher per match mode and at depth. A wrong match credits a
    heuristic that no repository's files match, and the cover then leaves the heuristic out
    without a warning.
  - The tests assert the order and the tie-breaks of the cover, not only which repositories
    it contains, because the fixed order makes two runs write the same bytes.
  - The tests run `--check` for each of its three exit codes, because the exit code is its
    verdict.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_tools_subsample -v
"""

import csv
import importlib
import io
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from agentcontext import SEVEN_ZIP, STAGE_DIR
_sel = importlib.import_module("agentcontext.tools.subsample")
heuristics = importlib.import_module("agentcontext.heuristics")

HEURISTIC_ROWS = [r for r in heuristics.load_config_path_patterns() if r["mechanism"] != "collect_only"]
HEAD = "a" * 40
GIB = 1024 ** 3


def _heuristic_row(tool, mechanism, path):
    return next(r for r in HEURISTIC_ROWS if (r["tool"], r["mechanism"], r["path"]) == (tool, mechanism, path))


def _write_csv(path: Path, columns: list, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in columns})


class TestHeuristicMatching(unittest.TestCase):
    def test_each_loading_mode_has_a_distinct_key(self):
        heuristic_rows = heuristics.load_config_path_patterns()
        self.assertEqual(len({_sel.heuristic_key(row) for row in heuristic_rows}), len(heuristic_rows))


    def test_a_file_heuristic_matches_its_path_ignoring_case(self):
        heuristic_row = _heuristic_row("copilot", "context_file", ".github/copilot-instructions.md")
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".github/copilot-instructions.md"))
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".github/COPILOT-INSTRUCTIONS.md"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, "docs/.github/copilot-instructions.md"))

    def test_a_basename_heuristic_matches_at_any_depth(self):
        heuristic_row = _heuristic_row("claude", "context_file", "CLAUDE.md")
        self.assertTrue(_sel.heuristic_matches(heuristic_row, "packages/web/claude.md"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, "CLAUDE.txt"))

    def test_a_tree_heuristic_matches_below_its_directory_with_its_extension(self):
        heuristic_row = _heuristic_row("cursor", "rules", ".cursor/rules")
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".cursor/rules/frontend/react.mdc"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".cursor/rules/notes.txt"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".cursor/rules"))

    def test_extension_alternatives_retain_each_supported_suffix_and_depth(self):
        for mode in ("tree_ext", "tree_ext_described", "dir_ext", "dir_ext_described"):
            heuristic_row = {"path": ".cursor/agents", "match_mode": mode + ":.md|.mdc|.markdown"}
            for extension in ("md", "mdc", "markdown"):
                with self.subTest(mode=mode, extension=extension):
                    self.assertTrue(_sel.heuristic_matches(heuristic_row, f".cursor/agents/review.{extension}"))
                    self.assertEqual(_sel.heuristic_matches(heuristic_row, f".cursor/agents/nested/review.{extension}"),
                                     mode.startswith("tree_ext"))
            self.assertFalse(_sel.heuristic_matches(heuristic_row, ".cursor/agents/readme.txt"))

    def test_a_dir_ext_heuristic_matches_direct_children_only(self):
        heuristic_row = {"path": ".github/hooks", "match_mode": "dir_ext:.json"}
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".github/hooks/pre.json"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".github/hooks/nested/pre.json"))

    def test_a_package_heuristic_matches_the_entry_modules_of_the_folders_in_its_directory(self):
        heuristic_row = {"path": ".opencode/plugins", "match_mode": "package_entry:server|tui:.ts|.js"}
        for path in (".opencode/plugins/acme/server.ts", ".opencode/plugins/acme/tui.js"):
            with self.subTest(path=path):
                self.assertTrue(_sel.heuristic_matches(heuristic_row, path))
        for path in (".opencode/plugins/server.ts", ".opencode/plugins/acme/src/server.ts",
                     ".opencode/plugins/acme/rpc.ts", ".opencode/plugins/acme/server.json",
                     ".opencode/plugins/acme"):
            with self.subTest(path=path):
                self.assertFalse(_sel.heuristic_matches(heuristic_row, path))

    def test_an_opencode_package_credits_the_row_of_its_folder(self):
        """The entry module of a package, not a module directly in the folder, credits the
        `package_entry:` row, at the root and below it. A `tui` entry in `.opencode/plugin`
        credits no row, because OpenCode 2 does not load it there."""
        matched, depth = _sel.match_heuristics(HEURISTIC_ROWS, "plugins", ".opencode/plugins/acme/tui.tsx")
        self.assertEqual([r["match_mode"].split(":")[0] for r in matched], ["package_entry"])
        self.assertEqual(depth, 0)
        matched, depth = _sel.match_heuristics(HEURISTIC_ROWS, "plugins",
                                               "packages/web/.opencode/plugin/acme/index.mjs")
        self.assertEqual([(r["path"], r["match_mode"].split(":")[0]) for r in matched],
                         [(".opencode/plugin", "package_entry")])
        self.assertEqual(depth, 2)
        matched, _ = _sel.match_heuristics(HEURISTIC_ROWS, "plugins", ".opencode/plugins/notify.ts")
        self.assertEqual([r["match_mode"] for r in matched], ["dir_ext:.ts|.js"])
        matched, _ = _sel.match_heuristics(HEURISTIC_ROWS, "plugins", ".opencode/plugin/panel/tui.tsx")
        self.assertEqual(matched, [])

    def test_a_starred_segment_stands_for_one_segment(self):
        # No row with the match mode `file` has a star, because the plugin parts are under the
        # placeholders. The matcher still accepts a star in such a row, and this test covers it.
        heuristic_row = {**_heuristic_row("claude", "settings", ".claude/settings.json"),
                "path": ".claude/plugins/*/settings.json"}
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".claude/plugins/demo/settings.json"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".claude/plugins/a/b/settings.json"))

    def test_a_memory_directory_pattern_matches_every_file_below_it(self):
        heuristic_row = _heuristic_row("claude", "subagents", ".claude/agent-memory/*")
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".claude/agent-memory/reviewer/MEMORY.md"))
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".claude/agent-memory/reviewer/notes.md"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".claude/agent-memory/MEMORY.md"))

    def test_a_name_pattern_matches_the_direct_children_that_it_names(self):
        heuristic_row = _heuristic_row("claude", "hooks", ".claude")
        self.assertTrue(_sel.heuristic_matches(heuristic_row, ".claude/hookify.warn-rm.local.md"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".claude/rules/hookify.a.local.md"))
        self.assertFalse(_sel.heuristic_matches(heuristic_row, ".claude/hookify.md"))

    def test_a_skills_directory_and_the_marker_heuristic_both_credit_a_skill(self):
        matched, depth = _sel.match_heuristics(HEURISTIC_ROWS, "skills", ".claude/skills/deploy/SKILL.md")
        keys = {_sel.heuristic_key(r) for r in matched}
        self.assertIn("claude|skills|.claude/skills|skill_dir_flat", keys)
        self.assertIn("any|skills|SKILL.md|skill_marker", keys)
        self.assertEqual(depth, 0)
        self.assertEqual(_sel.heuristic_key(matched[0]), "claude|skills|.claude/skills|skill_dir_flat")

    def test_a_mechanism_directory_below_the_root_matches_at_depth(self):
        matched, depth = _sel.match_heuristics(HEURISTIC_ROWS, "rules", "packages/app/.cursor/rules/a.mdc")
        self.assertEqual([_sel.heuristic_key(r) for r in matched], ["cursor|rules|.cursor/rules|tree_ext:.mdc"])
        self.assertEqual(depth, 2)

    def test_a_path_that_no_heuristic_of_the_mechanism_explains_matches_nothing(self):
        matched, _ = _sel.match_heuristics(HEURISTIC_ROWS, "rules", ".github/instructions/x.instructions.md")
        self.assertEqual(matched, [])

    def test_other_csv_matches_spec_and_unclassified_directories(self):
        matched, _ = _sel.match_heuristics(HEURISTIC_ROWS, "other", ".claude/claude.md")
        self.assertEqual([_sel.heuristic_key(r) for r in matched], ["claude|other|.claude|spec_dir"])
        matched, _ = _sel.match_heuristics(HEURISTIC_ROWS, "other", ".github/x.yml")
        self.assertEqual([_sel.heuristic_key(r) for r in matched], ["copilot|other|.github|unclassified_dir"])


class _StageRoot(unittest.TestCase):
    """A small stage directory with configured repositories, a dropped repository, emptied
    repositories, a failed clone and repositories without configuration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.output = root / "output"
        self.validated = root / "data" / "validated"
        self.collected = root / "data" / "collected"
        self.submodules = root / "submodules.csv"
        self.deletion = root / "deletion.csv"
        self.ghs = root / "ghs_filtered.json"
        self.subset = root / "dev_subsample.csv"
        self.sizes = {"o/big": 900 * 1024, "o/small": 3 * 1024, "o/tiny": 1024,
                      "o/dropped": 2 * 1024, "o/huge": 5000 * 1024}
        self._tables()
        self._trees()
        self._lists()

    def _tables(self):
        meta = {"scanned_at": "2026-03-29", "branch": "main", "head_sha": HEAD,
                "head_commit_date": "2026-03-01T00:00:00Z"}
        flags = {c: "FALSE" for c in ("claude", "gemini", "codex", "copilot", "cursor", "opencode",
                                      "claude.md", "gemini.md", "agents.md",
                                      "copilot-instructions", "cursorrules", "context_file",
                                      "settings", "skills", "subagents", "commands", "hooks",
                                      "rules", "mcp", "other", "no_specific_tool")}
        repos = [
            {"repo_name": "o/big", **meta, **flags, "claude": "TRUE", "cursor": "TRUE",
             "branch": "develop"},
            {"repo_name": "o/small", **meta, **flags, "claude": "TRUE"},
            {"repo_name": "o/tiny", **meta, **flags, "no_specific_tool": "TRUE"},
            {"repo_name": "o/huge", **meta, **flags, "codex": "TRUE"},
        ]
        _write_csv(self.output / "repos.csv", list(repos[0]), repos)
        for mechanism, (filename, artifact) in heuristics.MECHANISM_CSVS.items():
            _write_csv(self.output / filename, heuristics.csv_header(mechanism), [])
        git = {"created_at": "2026-01-01T00:00:00Z", "#commits": "1",
               "first_commit_sha": "b" * 40, "last_commit_sha": "c" * 40}
        _write_csv(self.output / "context-files.csv",
                   heuristics.csv_header("context_file"), [
                       {"repo_name": "o/big", **meta, "context_file": "CLAUDE.md", **git,
                        "language": "German"},
                       {"repo_name": "o/big", **meta, "context_file": "packages/x/claude.md",
                        **git, "language": "English"},
                       {"repo_name": "o/small", **meta, "context_file": "CLAUDE.md", **git,
                        "language": "English"},
                       {"repo_name": "o/tiny", **meta, "context_file": "AGENTS.md",
                        "language": "English"},
                       {"repo_name": "o/huge", **meta, "context_file": "AGENTS.md", **git,
                        "language": "English"},
                   ])
        rules_column = heuristics.MECHANISM_CSVS["rules"][1]
        _write_csv(self.output / "rules.csv", heuristics.csv_header("rules"), [
            {"repo_name": "o/big", **meta, rules_column: ".cursor/rules/a.mdc", "name": "a",
             **git, "language": "English"},
            {"repo_name": "o/big", **meta, rules_column: "packages/x/.cursor/rules/b.mdc",
             "name": "b", **git, "language": "English"}])
        _write_csv(self.output / "skills.csv", heuristics.csv_header("skills"), [
            {"repo_name": "o/small", **meta, "skills.md": ".claude/skills/d/SKILL.md",
             "name": "d", "scripts": "TRUE", "references": "FALSE", "assets": "FALSE",
             **git, "language": "English"}])
        _write_csv(self.output / "settings.csv", heuristics.csv_header("settings"), [
            {"repo_name": "o/huge", **meta, "settings": ".codex/config.toml", **git}])
        _write_csv(self.output / "repo-sizes.csv", ["repo_name", "size_kb"],
                   [{"repo_name": r, "size_kb": s} for r, s in self.sizes.items()]
                   + [{"repo_name": "o/gone", "size_kb": ""}])
        _write_csv(self.output / "snapshot-validation.csv", ["repo_name", "outcome"],
                   [{"repo_name": "o/big", "outcome": "OK"},
                    {"repo_name": "o/small", "outcome": "GONE"}])
        _write_csv(self.output / "all_ai_commits.csv",
                   ["repo_name", "commit_sha", "ai_tool"],
                   [{"repo_name": "o/big", "commit_sha": "1" * 40, "ai_tool": "Claude,Cursor"},
                    {"repo_name": "o/small", "commit_sha": "2" * 40, "ai_tool": "Gemini"}])

    def _trees(self):
        for repo, files in {
            "o§big": {"CLAUDE.md": "# hi\n", "packages/x/claude.md": "# nested\n",
                      ".cursor/rules/a.mdc": "rule\n",
                      "packages/x/.cursor/rules/b.mdc": "rule\n"},
            "o§small": {"CLAUDE.md": b"caf\xe9\n", ".claude/skills/d/SKILL.md": "# d\n",
                        ".claude/skills/d/run.sh": "echo\n"},
            "o§tiny": {"AGENTS.md": "# agents\n"},
            "o§huge": {"AGENTS.md": "# a\n", ".codex/config.toml": "x = 1\n"},
        }.items():
            for rel, content in files.items():
                path = self.validated / repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    path.write_bytes(content)
                else:
                    path.write_text(content, encoding="utf-8")
        # A resolving and an unresolved marker at a mechanism directory, a resolving marker
        # inside a mechanism directory, and two real symlinks.
        big = self.validated / "o§big"
        (big / ".codex").mkdir()
        (big / ".codex" / "skills").write_text("../.claude/skills\n", encoding="utf-8")
        (big / ".claude" / "skills" / "x").mkdir(parents=True)
        (big / ".claude" / "skills" / "x" / "SKILL.md").write_text("# x\n", encoding="utf-8")
        (big / ".gemini").mkdir()
        (big / ".gemini" / "skills").write_text("../.agents/skills\n", encoding="utf-8")
        (big / ".cursor" / "skills").mkdir(parents=True)
        (big / ".cursor" / "skills" / "y").write_text("../../.claude/skills/x\n",
                                                        encoding="utf-8")
        (big / ".github" / "agents").mkdir(parents=True)
        os.symlink("../../CLAUDE.md", big / ".github" / "agents" / "inside.md")
        os.symlink("D:/code/elsewhere/skills", big / ".github" / "skills")
        _write_csv(self.validated / "repos.csv", ["repo_name"], [])
        # The collected tree: a collect_only file under a plugin directory of o/small.
        for repo, files in {"o§big": ["CLAUDE.md"], "o§small": ["CLAUDE.md",
                                                                  ".claude-plugin/README.md"],
                            "o§tiny": ["AGENTS.md"], "o§huge": ["AGENTS.md"]}.items():
            for rel in files:
                path = self.collected / repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x\n", encoding="utf-8")

    def _lists(self):
        _write_csv(self.submodules, ["repo_name", "path", "submodule_repo"],
                   [{"repo_name": "o/small", "path": "vendor/x", "submodule_repo": "x/x"}])
        # The paths as the cleanup writes them. Under a `repos/<owner§repo>/` layout, a fixed
        # index into the path would find the repository just as the search of the matrix does.
        # Under `data/validated/`, such an index fails, and these paths therefore tell the
        # index and the search apart.
        _write_csv(self.deletion, ["path", "reason"], [
            {"path": "data/validated/o§dropped/lib/AGENTS.md", "reason": "inside submodule (lib)"},
            {"path": "data/validated/o§dropped",
             "reason": "no detected mechanism, repository directory removed"},
            {"path": "data/validated/o§big/.github/x.yml",
             "reason": "file in .github outside the Copilot paths"},
        ])
        flags = ("claude", "gemini", "codex", "copilot", "cursor", "opencode", "claude.md",
                 "gemini.md", "agents.md", "plans.md", "copilot-instructions", "cursorrules")
        meta = ("repo_name", "scanned_at", "branch", "head_sha", "head_commit_date",
                "first_commit_sha")
        def row(repo, on):
            return {**{c: "" for c in meta}, "repo_name": repo,
                    **{f: ("TRUE" if f in on else "FALSE") for f in flags}}
        _write_csv(self.collected / "repos.csv", list(meta) + list(flags), [
            row("o/big", ("claude",)), row("o/small", ("claude",)), row("o/tiny", ("agents.md",)),
            row("o/huge", ("codex",)), row("o/dropped", ("gemini",)),
            row("o/emptied-a", ("claude",)), row("o/emptied-b", ("gemini",)),
            row("o/failed", ()), row("o/bare-a", ()), row("o/bare-b", ()),
        ])
        _write_csv(self.collected / "clone_failed.csv", ["repo_name", "repo_url", "reason"],
                   [{"repo_name": "o/failed", "repo_url": "u", "reason": "clone_failed"}])
        items = [{"name": r, "branches": b, "size": s} for r, b, s in (
            ("o/big", 300, 900 * 1024), ("o/small", 3, 3 * 1024), ("o/tiny", 1, 1024),
            ("o/huge", 5, 5000 * 1024), ("o/dropped", 2, 2 * 1024),
            ("o/emptied-a", 1, 700), ("o/emptied-b", 1, 300), ("o/failed", 1, 50),
            ("o/bare-a", 1, 20), ("o/bare-b", 1, 10))]
        self.ghs.write_text(json.dumps({"_metadata": {}, "items": items}), encoding="utf-8")

    def _matrix(self):
        return _sel.coverage_matrix(self.output, self.validated, self.collected,
                                   self.submodules, self.deletion, _sel.ghs_items(self.ghs))

    def _select(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return _sel.select(self.output, self.validated, self.collected, self.submodules,
                               self.deletion, self.ghs, kwargs.get("max_size_mib", 1200),
                               log=lambda *a: None)


class TestTheCoverageMatrix(_StageRoot):
    def test_each_coverage_entry_is_credited_to_the_repository_that_has_it(self):
        coverage, unmatched_heuristics, unmatched_rows = self._matrix()
        big, small, tiny, huge = (coverage[r] for r in ("o/big", "o/small", "o/tiny", "o/huge"))
        self.assertIn("heuristic:claude|context_file|CLAUDE.md|basename", big)
        self.assertIn("heuristic:cursor|rules|.cursor/rules|tree_ext:.mdc", big)
        self.assertIn("mechanism:rules", big)
        self.assertIn("nested_mechanism_dir", big)   # packages/x/.cursor/rules/b.mdc
        self.assertIn("case_variant", big)
        self.assertIn("non_english_artifact", big)
        self.assertIn("branch_not_main_or_master", big)
        self.assertIn("many_refs", big)
        self.assertIn("ai_commit:Claude", big)
        self.assertIn("ai_commit:Cursor", big)
        self.assertIn("marker_at_dir:resolving", big)
        self.assertIn("marker_at_dir:unresolved", big)
        self.assertIn("marker_inside_dir:resolving", big)
        self.assertIn("symlink:inside_tree", big)
        self.assertIn("symlink:outside_tree", big)
        self.assertIn("deleted:irrelevant_github_file", big)
        self.assertIn("tool:claude", big)
        self.assertIn("tool:cursor", big)
        self.assertIn("non_utf8_artifact", small)
        self.assertIn("binary_in_skills_dir", small)
        self.assertIn("heuristic:any|skills|SKILL.md|skill_marker", small)
        self.assertIn("submodule_recorded", small)
        self.assertIn("head_sha_unreachable", small)
        self.assertIn("collect_only_file", small)
        self.assertIn("ai_commit:Gemini", small)
        self.assertIn("tool:no_specific_tool", tiny)
        self.assertIn("row_without_git_metadata", tiny)
        self.assertIn("zero_ai_commits", tiny)
        self.assertIn("deleted:inside_submodule", coverage["o/dropped"])
        self.assertIn("deleted:no_mechanism", coverage["o/dropped"])
        self.assertIn("heuristic:codex|settings|.codex/config.toml|file", huge)
        self.assertIn("codex|hooks|.codex/config.toml|toml_hooks", unmatched_heuristics)
        self.assertNotIn("codex|settings|.codex/config.toml|file", unmatched_heuristics)
        self.assertEqual(unmatched_rows, [])

    def test_a_row_that_no_heuristic_explains_is_reported_not_credited(self):
        rules_column = heuristics.MECHANISM_CSVS["rules"][1]
        _write_csv(self.output / "rules.csv", heuristics.csv_header("rules"), [
            {"repo_name": "o/big", rules_column: ".github/instructions/x.instructions.md"}])
        coverage, _, unmatched_rows = self._matrix()
        self.assertEqual(unmatched_rows,
                         [("rules", "o/big", ".github/instructions/x.instructions.md")])
        self.assertNotIn("heuristic:cursor|rules|.cursor/rules|tree_ext:.mdc", coverage["o/big"])

    def test_a_part_of_a_plugin_credits_the_row_of_the_plugin_s_kind(self):
        """The row of a plugin's part names the plugin's root by the manifest in the root, and a
        path alone cannot show the manifest. The matrix therefore reads the credit from the
        tree, as it reads the credit of a declared context file."""
        small = self.validated / "o§small" / "plugins" / "review"
        (small / ".claude-plugin").mkdir(parents=True)
        (small / ".claude-plugin" / "plugin.json").write_text('{"name": "review"}\n',
                                                               encoding="utf-8")
        (small / "agents").mkdir()
        (small / "agents" / "reviewer.md").write_text("Review.\n", encoding="utf-8")
        column = heuristics.MECHANISM_CSVS["subagents"][1]
        _write_csv(self.output / "subagents.csv", heuristics.csv_header("subagents"), [
            {"repo_name": "o/small", column: "plugins/review/agents/reviewer.md"},
            # Without a manifest beside it, the same path in another repository belongs to no
            # plugin.
            {"repo_name": "o/big", column: "plugins/review/agents/reviewer.md"}])
        coverage, unmatched_heuristics, unmatched_rows = self._matrix()
        self.assertIn("heuristic:claude|subagents|<claude-plugin>/agents|tree_ext:.md", coverage["o/small"])
        self.assertNotIn("claude|subagents|<claude-plugin>/agents|tree_ext:.md", unmatched_heuristics)
        self.assertEqual(unmatched_rows,
                         [("subagents", "o/big", "plugins/review/agents/reviewer.md")])

    def test_manifest_declarations_and_default_components_have_distinct_coverage(self):
        for repo, manifest, artifact in (
            ("o§tiny", {"name": "p"}, "agents/review.md"),
            ("o§huge", {"name": "p", "agents": ["./custom/review.md"]}, "custom/review.md"),
        ):
            root = self.validated / repo / "p"
            (root / ".claude-plugin").mkdir(parents=True)
            (root / ".claude-plugin/plugin.json").write_text(json.dumps(manifest))
            (root / artifact).parent.mkdir(parents=True)
            (root / artifact).write_text("Review")
        _write_csv(self.output / "subagents.csv", heuristics.csv_header("subagents"), [
            {"repo_name": "o/tiny", "subagent": "p/agents/review.md"},
            {"repo_name": "o/huge", "subagent": "p/custom/review.md"},
        ])
        coverage, unmatched, _ = self._matrix()
        default = "heuristic:claude|subagents|<claude-plugin>/agents|tree_ext:.md"
        declared = "heuristic:claude|subagents|<claude-plugin>/agents|manifest_field:agents:files"
        self.assertIn(default, coverage["o/tiny"])
        self.assertNotIn(declared, coverage["o/tiny"])
        self.assertIn(declared, coverage["o/huge"])
        self.assertNotIn(default, coverage["o/huge"])
        self.assertNotIn(declared.removeprefix("heuristic:"), unmatched)

    def test_a_rule_file_that_a_rules_json_names_credits_its_row(self):
        """A rule file outside .agents/rules is a rule because .agents/rules.json registers it,
        and a path alone cannot show the registration. The matrix therefore reads the credit
        from the tree, as it reads the credit of a declared context file."""
        small = self.validated / "o§small"
        (small / ".agents").mkdir(parents=True, exist_ok=True)
        (small / ".agents" / "rules.json").write_text('{"entries": [{"path": "shared-rules"}]}\n',
                                                     encoding="utf-8")
        (small / "shared-rules").mkdir()
        (small / "shared-rules" / "api.md").write_text("---\ntrigger: manual\n---\n",
                                                      encoding="utf-8")
        column = heuristics.MECHANISM_CSVS["rules"][1]
        _write_csv(self.output / "rules.csv", heuristics.csv_header("rules"), [
            {"repo_name": "o/small", column: "shared-rules/api.md"}])
        coverage, unmatched_heuristics, unmatched_rows = self._matrix()
        self.assertIn("heuristic:gemini|rules|.agents/rules.json|declared_rules", coverage["o/small"])
        self.assertEqual(unmatched_rows, [])

    def test_the_role_file_that_the_codex_config_names_credits_its_row(self):
        """A TOML file outside `.codex/agents` is a Codex subagent file because the `config_file`
        of a role in `.codex/config.toml` names it, and a path alone cannot show the role. The
        matrix therefore reads the credit from the tree, as it reads the credit of a declared
        context file. The instructions file that the role names is a part of the role and no row
        of its own."""
        codex = self.validated / "o§small" / ".codex"
        codex.mkdir(parents=True)
        (codex / "config.toml").write_text(
            '[agents.reviewer]\nconfig_file = "../roles/reviewer.toml"\n', encoding="utf-8")
        roles = codex.parent / "roles"
        roles.mkdir()
        (roles / "reviewer.toml").write_text('model_instructions_file = "reviewer.md"\n',
                                             encoding="utf-8")
        (roles / "reviewer.md").write_text("# Reviewer\n", encoding="utf-8")
        column = heuristics.MECHANISM_CSVS["subagents"][1]
        _write_csv(self.output / "subagents.csv", heuristics.csv_header("subagents"), [
            {"repo_name": "o/small", column: "roles/reviewer.toml"}])
        coverage, unmatched_heuristics, unmatched_rows = self._matrix()
        self.assertIn("heuristic:codex|subagents|.codex/config.toml|declared_path:agents.*.config_file",
                      coverage["o/small"])
        self.assertNotIn("codex|subagents|.codex/config.toml|declared_path:agents.*.config_file",
                         unmatched_heuristics)
        self.assertEqual(unmatched_rows, [])

    def test_the_symlink_direction_is_the_targets_not_its_existence(self):
        big = self.validated / "o§big"
        os.symlink("../../missing.md", big / ".github" / "agents" / "dangling.md")
        found = _sel.tree_coverage(big)
        self.assertIn("symlink:inside_tree", found)
        self.assertIn("symlink:outside_tree", found)
        self.assertFalse(_sel._points_inside(big, big / ".github" / "skills"))
        self.assertTrue(_sel._points_inside(big, big / ".github" / "agents" / "dangling.md"))


@unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
class TestTheSituationsARunShowed(_StageRoot):
    """Coverage includes recorded collection failures, agent workflows, commit
    heuristics, cleanup reasons and unmatched paths."""

    def setUp(self):
        super().setUp()
        with open(self.deletion, "a", encoding="utf-8", newline="") as f:
            csv.writer(f, lineterminator="\n").writerow(
                ["data/validated/o§small/claude.md",
                 "wrong letter case of claude.md (instead of CLAUDE.md)"])
        _write_csv(self.output / "all_ai_commits.csv",
                   ["repo_name", "commit_sha", "ai_tool", "matched_heuristics"],
                   [{"repo_name": "o/big", "commit_sha": "1" * 40, "ai_tool": "Claude",
                     "matched_heuristics": "8;13"},
                    {"repo_name": "o/small", "commit_sha": "2" * 40, "ai_tool": "Gemini",
                     "matched_heuristics": "50"}])
        _write_csv(self.output / "ai_commits-repos.csv", ["repo_name", "outcome"],
                   [{"repo_name": "o/huge", "outcome": "history_unavailable"},
                    {"repo_name": "o/small", "outcome": "history_read"},
                    {"repo_name": "o/tiny", "outcome": "no_files"}])
        workflows = self.collected.parent / "workflows"
        (workflows / "o§tiny").mkdir(parents=True)
        (workflows / "o§tiny" / "nightly.yml").write_text(
            "jobs:\n  go:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n")
        (workflows / "o§small").mkdir(parents=True)
        (workflows / "o§small" / "ci.yml").write_text(
            "# see openai/codex-action\njobs:\n  t:\n    steps:\n      - uses: actions/checkout@v4\n")
        _write_csv(self.output / "other.csv", heuristics.csv_header("other"), [
            {"repo_name": "o/big", "other": ".claude/notes/x.md"},
            {"repo_name": "o/small", "other": ".claude/settings.local.json"},
            {"repo_name": "o/tiny", "other": "packages/web/.claude/notes/y.md"}])
        _write_csv(self.output / "unmatched-paths.csv", ["glob", "repos", "files"],
                   [{"glob": ".claude/notes/**", "repos": "2", "files": "2"}])

    def test_each_situation_is_credited_to_the_repository_that_showed_it(self):
        coverage, _, _ = self._matrix()
        self.assertIn("deleted:wrong_letter_case_of_claude.md", coverage["o/small"])
        self.assertTrue({"commit_heuristic:8", "commit_heuristic:13"} <= coverage["o/big"])
        self.assertIn("commit_heuristic:50", coverage["o/small"])
        self.assertIn("history:history_unavailable", coverage["o/huge"])
        self.assertFalse([e for e in coverage["o/small"] | coverage["o/tiny"]
                          if e.startswith("history:")], "a history that was read is no situation")
        self.assertIn("agent_workflow:claude", coverage["o/tiny"])
        self.assertFalse([e for e in coverage["o/small"] if e.startswith("agent_workflow:")],
                         "a workflow that names an action in a comment runs nothing")
        self.assertIn("unmatched_path_glob", coverage["o/big"])
        self.assertNotIn("unmatched_path_glob", coverage["o/small"])

    def test_a_path_below_the_root_shows_the_path_glob_of_its_place(self):
        """`unmatched paths` counts `packages/web/.claude/notes/y.md` under `.claude/notes/**`,
        the path glob of its path below `.claude`, and the selection reads it the same way."""
        coverage, _, _ = self._matrix()
        self.assertIn("unmatched_path_glob", coverage["o/tiny"])

    def test_a_known_failure_counts_only_where_the_collection_contains_the_repository(self):
        with mock.patch.dict(_sel.STEP_FAILURES,
                             {"stage_failure:merge_added_path": ("o/small", "o/elsewhere")},
                             clear=True):
            coverage, _, _ = self._matrix()
        self.assertIn("stage_failure:merge_added_path", coverage["o/small"])
        self.assertNotIn("o/elsewhere", coverage)

    def test_a_repository_that_the_size_list_lacks_is_a_candidate_by_its_ghs_size(self):
        """A repository that repo-sizes.csv does not list, such as a repository that a later
        collection added, is a candidate by its GHS size rather than no candidate at all."""
        meta = {"scanned_at": "2026-09-22", "branch": "main", "head_sha": HEAD,
                "head_commit_date": "2026-09-01T00:00:00Z"}
        with open(self.output / "settings.csv", "a", encoding="utf-8", newline="") as f:
            columns = heuristics.csv_header("settings")
            csv.DictWriter(f, columns, lineterminator="\n").writerow(
                {c: "" for c in columns} | {"repo_name": "o/new", **meta,
                                            "settings": ".gemini/settings.json"})
        items = json.loads(self.ghs.read_text(encoding="utf-8"))
        items["items"].append({"name": "o/new", "branches": 1, "size": 700})
        self.ghs.write_text(json.dumps(items), encoding="utf-8")
        rows, _ = self._select()
        picked = {repo: size for repo, tier, _entries, size in rows if tier == "cover"}
        self.assertEqual(picked.get("o/new"), 700)


class TestAFileWithANul(unittest.TestCase):
    """A symlink's target cannot contain a NUL, and the path functions raise ValueError for a
    path with a NUL. A file that contains a NUL, in a mechanism directory or at the path of a
    mechanism directory, must not stop the reading of the validated tree."""

    def test_it_is_no_marker(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "o§r"
            (repo / ".claude" / "rules").mkdir(parents=True)
            (repo / ".claude" / "rules" / "blank.md").write_bytes(b"../\0rules.md")
            (repo / ".claude" / "skills").write_bytes(b"../\0skills")
            self.assertEqual(_sel.tree_coverage(repo), {"marker_at_dir:unresolved"})


class TestTheCommitCsv(unittest.TestCase):
    """The commit CSV read from its archive when the CSV is absent, as in a clean checkout.
    An earlier archive stored it under `3_data-collection/output/`. Requested by the bare name
    `all_ai_commits.csv`, 7zz would match nothing and exit 0, and every repository would
    silently get `zero_ai_commits`."""

    HEADER = ["repo_name", "commit_sha", "ai_tool"]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output = self.tmp / "output"
        self.output.mkdir()

    def _pack(self, member: str, text: str) -> None:
        """An archive in `output/` that contains `text` at `member`."""
        stage = self.tmp / "stage"
        (stage / member).parent.mkdir(parents=True, exist_ok=True)
        (stage / member).write_text(text, encoding="utf-8")
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(self.output / "all_ai_commits.csv.7z"), member],
                       cwd=stage, check=True)

    def test_split_archives_yield_every_row_and_refuse_missing_volumes(self):
        self._pack("3_data-collection/output/all_ai_commits.csv",
                   "repo_name,commit_sha,ai_tool\no/a,aaa,Claude\no/b,bbb,Codex\n")
        archive = self.output / "all_ai_commits.csv.7z"
        raw = archive.read_bytes()
        size = len(raw) // 2
        for index, start in enumerate(range(0, len(raw), size), 1):
            Path(str(archive) + f".{index:03}").write_bytes(raw[start:start + size])
        archive.unlink()
        self.assertEqual(_sel.commit_table_rows(self.output), [
            {"repo_name": "o/a", "commit_sha": "aaa", "ai_tool": "Claude"},
            {"repo_name": "o/b", "commit_sha": "bbb", "ai_tool": "Codex"}])
        Path(str(archive) + ".002").unlink()
        with self.assertRaises(subprocess.CalledProcessError):
            _sel.commit_table_rows(self.output)

    def test_the_commit_csv_is_found_under_the_directory_that_the_archive_stores_it_in(self):
        self._pack("3_data-collection/output/all_ai_commits.csv",
                   "repo_name,commit_sha,ai_tool\no/big,1111,Claude\n")
        self.assertEqual(_sel.commit_table_rows(self.output),
                         [{"repo_name": "o/big", "commit_sha": "1111", "ai_tool": "Claude"}])

    def test_an_archive_without_the_commit_csv_is_refused(self):
        self._pack("3_data-collection/output/other.csv", "repo_name\no/big\n")
        with self.assertRaises(ValueError) as caught:
            _sel.commit_table_rows(self.output)
        self.assertIn("0 files named all_ai_commits.csv", str(caught.exception))

    def test_a_commit_csv_without_rows_is_refused(self):
        self._pack("all_ai_commits.csv", "repo_name,commit_sha,ai_tool\n")
        with self.assertRaises(ValueError) as caught:
            _sel.commit_table_rows(self.output)
        self.assertIn("no rows", str(caught.exception))

    def test_the_command_line_reports_the_refusal(self):
        """main catches the ValueError and exits 1 rather than writing a list."""
        root = _StageRoot("setUp")
        root.setUp()
        self.addCleanup(root.doCleanups)
        (root.output / "all_ai_commits.csv").unlink()
        stage = self.tmp / "empty"
        stage.mkdir()
        (stage / "all_ai_commits.csv").write_text("repo_name,commit_sha,ai_tool\n",
                                                  encoding="utf-8")
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(root.output / "all_ai_commits.csv.7z"), "all_ai_commits.csv"],
                       cwd=stage, check=True)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _sel.main(["--output-dir", str(root.output),
                              "--validated-dir", str(root.validated),
                              "--collected-dir", str(root.collected),
                              "--submodules", str(root.submodules),
                              "--deletion", str(root.deletion), "--ghs", str(root.ghs),
                              "--subsample", str(root.subset)])
        self.assertEqual(code, 1)
        self.assertIn("ERROR subsample: The all_ai_commits.csv in ", err.getvalue())
        self.assertIn("no rows", err.getvalue())
        self.assertFalse(root.subset.exists())


class TestTheCover(_StageRoot):
    def test_a_new_collection_needs_no_historical_snapshot_validation(self):
        (self.output / "snapshot-validation.csv").unlink()
        rows, report = self._select()
        self.assertTrue(rows)
        self.assertEqual(report["gone"], [])
        self.assertFalse(any("head_sha_unreachable" in entries for _, _, entries, _ in rows))

    def test_the_repository_with_most_entries_goes_first_and_each_row_has_only_new_entries(self):
        rows, report = self._select()
        cover = [(r, c) for r, tier, c, _ in rows if tier == "cover"]
        self.assertEqual(cover[0][0], "o/big")
        picked = [r for r, _ in cover]
        self.assertEqual(len(picked), len(set(picked)))
        seen = set()
        for _, coverage in cover:
            self.assertFalse(seen & set(coverage), "an entry was credited twice")
            seen.update(coverage)
        universe = set().union(*[set(c) for _, c in cover])
        self.assertEqual(len(universe), report["coverage"] - len(report["uncoverable"]))

    def test_a_tie_goes_to_the_smaller_repository_whatever_its_name(self):
        chosen, _ = _sel.greedy_cover({"o/a": {"f"}, "o/b": {"f"}}, {"o/a": 10, "o/b": 5}, 100)
        self.assertEqual([r for r, _, _ in chosen], ["o/b"])

    def test_a_repository_measured_without_a_size_is_no_candidate_of_the_selection(self):
        """`repo-sizes.csv` has a blank size for a repository that GitHub did not serve. GHS's
        size, the fallback for a repository that the file does not list, would make it a
        candidate."""
        # o/gone has a settings file that no other repository has, and GHS records its size.
        settings = _sel.read_csv(self.output / "settings.csv")
        gone = {**settings[0], "repo_name": "o/gone", "tool": "gemini",
                "settings": ".gemini/settings.json"}
        _write_csv(self.output / "settings.csv", list(settings[0]), [*settings, gone])
        frame = json.loads(self.ghs.read_text(encoding="utf-8"))
        frame["items"].append({"name": "o/gone", "branches": 1, "size": 10})
        self.ghs.write_text(json.dumps(frame), encoding="utf-8")
        rows, report = self._select()
        cover = [repo for repo, tier, *_ in rows if tier == "cover"]
        self.assertNotIn("o/gone", cover)
        self.assertTrue(any(".gemini/settings.json" in entry for entry in report["uncoverable"]),
                        report["uncoverable"])

    def test_a_repository_that_github_refused_to_the_collection_is_no_candidate(self):
        """A repository that GitHub did not serve to the collection is no candidate, even where
        `repo-sizes.csv` does not list it and GHS's size would make it a candidate."""
        frame = json.loads(self.ghs.read_text(encoding="utf-8"))
        frame["items"].append({"name": "o/refused", "branches": 1, "size": 1})
        self.ghs.write_text(json.dumps(frame), encoding="utf-8")
        _write_csv(self.output / "ai_commits-repos.csv", ["repo_name", "outcome"],
                   [{"repo_name": "o/refused", "outcome": "dmca_takedown"}])
        rows, report = self._select()
        self.assertNotIn("o/refused", [repo for repo, tier, *_ in rows if tier == "cover"])
        self.assertIn("history:dmca_takedown", report["uncoverable"])

    def test_an_empty_selection_writes_nothing_and_exits_1(self):
        err = io.StringIO()
        with mock.patch.object(_sel, "select", return_value=([], {})), \
                contextlib.redirect_stderr(err):
            code = _sel.main(["--ghs", str(self.ghs), "--subsample", str(self.subset)])
        self.assertEqual(code, 1)
        self.assertIn("produced no repositories", err.getvalue())
        self.assertFalse(self.subset.exists())

    def test_a_failure_in_pythons_words_follows_the_name_of_the_selection_and_its_type(self):
        """Python's text of a KeyError is the missing key alone, which names no input."""
        err = io.StringIO()
        with mock.patch.object(_sel, "select", side_effect=KeyError("items")), \
                contextlib.redirect_stderr(err):
            code = _sel.main(["--ghs", str(self.ghs), "--subsample", str(self.subset)])
        self.assertEqual(code, 1)
        self.assertIn("ERROR subsample: Could not select the subsample (KeyError, 'items')",
                      err.getvalue())
        self.assertFalse(self.subset.exists())

    def test_ties_break_by_size_then_name(self):
        coverage = {"o/x": {"f"}, "o/y": {"f"}, "o/w": {"f"}}
        sizes = {"o/x": 10, "o/y": 5, "o/w": 5}
        chosen, _ = _sel.greedy_cover(coverage, sizes, 100)
        self.assertEqual([r for r, _, _ in chosen], ["o/w"])

    def test_a_repository_over_the_cap_is_no_candidate_and_its_entries_are_named_uncoverable(self):
        rows, report = self._select()
        self.assertNotIn("o/huge", [r for r, *_ in rows])
        self.assertIn("heuristic:codex|settings|.codex/config.toml|file", report["uncoverable"])
        self.assertIn("tool:codex", report["uncoverable"])
        rows, report = self._select(max_size_mib=6000)
        self.assertIn("o/huge", [r for r, *_ in rows])
        self.assertEqual(report["uncoverable"], [])

    def test_a_repository_without_a_size_is_no_candidate(self):
        coverage = {"o/gone": {"f", "g"}, "o/x": {"f"}}
        chosen, uncoverable = _sel.greedy_cover(coverage, {"o/x": 1}, 100)
        self.assertEqual([r for r, _, _ in chosen], ["o/x"])
        self.assertEqual(uncoverable, ["g"])

    def test_a_repository_that_github_no_longer_serves_is_no_candidate(self):
        # The size of o/big was measured before it left GitHub. A run would record o/big as a
        # clone failure, because neither a clone nor a mirror reaches it.
        before, _ = self._select()
        _write_csv(self.output / "snapshot-validation.csv", ["repo_name", "outcome"],
                   [{"repo_name": "o/big", "outcome": "REPO_GONE"},
                    {"repo_name": "o/small", "outcome": "GONE"}])
        rows, report = self._select()
        self.assertIn("o/big", [r for r, *_ in before])
        self.assertNotIn("o/big", [r for r, *_ in rows])
        self.assertEqual(report["gone"], ["o/big"])
        def covered(picked):
            return {e for _, tier, entries, _ in picked if tier == "cover" for e in entries}

        lost = covered(before) - covered(rows)
        self.assertTrue(lost)
        self.assertLessEqual(lost, set(report["uncoverable"]))

    def test_the_supplements_are_the_smallest_of_each_list(self):
        rows, _ = self._select()
        supplements = [(r, c) for r, tier, c, _ in rows if tier == "supplement"]
        self.assertEqual(supplements, [
            ("o/failed", ["clone_failure"]),
            ("o/emptied-b", ["emptied_repository"]),
            ("o/emptied-a", ["emptied_repository"]),
            ("o/bare-b", ["no_configuration"]),
        ])


    def test_a_failed_clone_is_listed_once_as_the_supplement(self):
        # The smallest failed clone could also cover its clone outcome in the cover and would
        # then appear twice in the run list of the subsample run. Because GitHub did not serve
        # it, it is no candidate of the cover, and the supplement covers its clone failure.
        _write_csv(self.output / "ai_commits-repos.csv", ["repo_name", "outcome"],
                   [{"repo_name": "o/failed", "outcome": "not_found_or_no_access"}])
        rows, report = self._select()
        names = [r for r, *_ in rows]
        self.assertEqual(len(names), len(set(names)))
        failed = [(tier, entries) for r, tier, entries, _ in rows if r == "o/failed"]
        self.assertEqual(failed, [("supplement", ["clone_failure"])])
        self.assertIn("history:not_found_or_no_access", report["uncoverable"])

    def test_two_runs_write_the_same_bytes(self):
        first = _sel.render(self._select()[0])
        second = _sel.render(self._select()[0])
        self.assertEqual(first, second)
        self.assertEqual(first.splitlines()[0], ",".join(_sel.COLUMNS))
        self.assertNotIn("\r", first)


class TestTheCommandLine(_StageRoot):
    def _run(self, *args) -> tuple:
        argv = [sys.executable, "-m", "agentcontext.tools.subsample",
                "--output-dir", str(self.output), "--validated-dir", str(self.validated),
                "--collected-dir", str(self.collected), "--submodules", str(self.submodules),
                "--deletion", str(self.deletion), "--ghs", str(self.ghs),
                "--subsample", str(self.subset), *args]
        result = subprocess.run(argv, capture_output=True, text=True)
        self.stdout, self.stderr = result.stdout, result.stderr
        return result.returncode, result.stdout + result.stderr

    def test_a_run_writes_the_list_and_names_every_choice(self):
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn(" MiB) covers ", out)
        self.assertIn("No configuration artifact matches codex|hooks|.codex/config.toml", out)
        with self.subset.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["repo_name"], "o/big")
        self.assertEqual(rows[0]["tier"], "cover")
        self.assertIn("many_refs", rows[0]["coverage"].split(";"))

    def test_selection_summary_uses_component_logging_on_stderr(self):
        code, output = self._run()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.stdout, "")
        lines = self.stderr.splitlines()
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(line.startswith("INFO subsample: ") for line in lines), self.stderr)
        self.assertIn(" MiB) covers ", self.stderr)
        self.assertIn("with a packed size of", self.stderr)

    def test_check_exits_0_when_the_list_is_current(self):
        self._run()
        code, out = self._run("--check")
        self.assertEqual(code, 0, out)

    def test_check_exits_2_when_the_repository_sizes_changed(self):
        self._run()
        self.sizes["o/big"] = 5000 * 1024
        _write_csv(self.output / "repo-sizes.csv", ["repo_name", "size_kb"],
                   [{"repo_name": r, "size_kb": s} for r, s in self.sizes.items()])
        code, out = self._run("--check")
        self.assertEqual(code, 2, out)
        self.assertIn("or in the coverage or the size of a row", out)

    def test_check_exits_1_when_there_is_no_list(self):
        code, out = self._run("--check")
        self.assertEqual(code, 1, out)

    def test_a_missing_ghs_file_exits_1_naming_the_archive(self):
        self.ghs.unlink()
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("7zz x", out)

    def test_a_missing_repos_csv_exits_1(self):
        (self.output / "repos.csv").unlink()
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn("ERROR", out)


if __name__ == "__main__":
    unittest.main()
