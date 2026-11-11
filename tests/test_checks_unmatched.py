"""Tests for agentcontext.checks.unmatched. `paths` ranks the path globs in other.csv that no
heuristic explains, and `commits` ranks the attributions that no commit pattern explains, each
with `--fail-above`. `reduce` tracks a collection's unmatched attributions in git, so that the
rows of not_ai_commits.csv can be checked against them.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_checks_unmatched -v
"""

import contextlib
import csv
import io
import json
import lzma
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from agentcontext import heuristics, paths, resume
from agentcontext import SEVEN_ZIP, STAGE_DIR
from agentcontext.checks import unmatched as _mod
from agentcontext.checks.unmatched import is_explained, glob_for
from tests._fixtures import dsm_removes_files_after_listing, dsm_writes_into_new_directories

# The header of output/other.csv. The check reads three of these columns, and the fixture writes
# every column of the CSV.
_HEADER = ["repo_name", "scanned_at", "branch", "head_sha", "head_commit_date", "other",
           "tool", "link_path", "github_link", "dataset_version", "created_at", "#commits",
           "first_commit_sha", "last_commit_sha"]


class TestPatternFor(unittest.TestCase):
    """The path glob determines which paths are counted together and which row of
    not_configuration.csv matches them. With a glob one level deeper, no recorded key would
    match, and every path that the keys cover would be listed as unmatched. A glob one level
    shallower would merge distinct mechanisms into one glob."""

    def test_a_file_two_levels_down_has_the_glob_of_its_directory(self):
        self.assertEqual(glob_for(".claude/rules/api.md"), ".claude/rules/**")

    def test_a_file_deeper_still_has_the_same_glob(self):
        self.assertEqual(glob_for(".claude/rules/frontend/forms.md"), ".claude/rules/**")

    def test_a_file_directly_inside_a_spec_directory_keeps_its_name(self):
        self.assertEqual(glob_for(".cursor/BUGBOT.md"), ".cursor/BUGBOT.md")

    def test_a_root_file_keeps_its_name(self):
        self.assertEqual(glob_for("AGENTS.md"), "AGENTS.md")

    def test_every_path_of_not_configuration_is_its_own_glob(self):
        """A row of not_configuration.csv that glob_for changes can never match. The paths that
        the row covers would be listed as unmatched in every run."""
        for key in _mod.NOT_CONFIGURATION:
            self.assertEqual(glob_for(key), key, key)


class TestThePathGlobsOfAPath(unittest.TestCase):
    """A reason of not_configuration.csv and a path of the heuristics name a place in a tool's
    directory, and the detection step reads that directory at any depth. A file of the directory
    below the root is therefore judged at its path below the directory, and the same place in two
    repositories is counted together, wherever each repository keeps the directory."""

    def test_a_spec_directory_below_the_root_gives_the_glob_below_it(self):
        self.assertEqual(_mod.path_globs("apps/readest-app/.claude/memory/notes.md"),
                         [".claude/memory/**"])
        self.assertEqual(_mod.path_globs("packages/oxygen-ui/.claude/patterns.md"),
                         [".claude/patterns.md"])

    def test_a_spec_directory_inside_another_gives_a_glob_below_each(self):
        self.assertEqual(_mod.path_globs(".claude/.claude-plugin/skills/bump.sh"),
                         [".claude/.claude-plugin/**", ".claude-plugin/skills/**"])
        self.assertEqual(_mod.path_globs(".github/scripts/ci/.agents/skills/lint/notes.md"),
                         [".github/scripts/**", ".agents/skills/**"])

    def test_an_ignore_file_gives_its_file_name_at_any_depth(self):
        """The rows of the ignore files match by file name at any depth, as the detection step
        reads them, and a test of the reduced path glob, such as
        `contrib/starrocks-python-client/**`, could not recover the name."""
        self.assertEqual(_mod.path_globs("contrib/starrocks-python-client/.cursorignore"),
                         [".cursorignore"])
        self.assertEqual(_mod.path_globs("tools/gemini-computer-use-eval/.geminiignore"),
                         [".geminiignore"])

    def test_a_path_in_no_spec_directory_keeps_the_glob_of_its_path(self):
        self.assertEqual(_mod.path_globs("src/plugins/core/mailchimp/plugin.json"),
                         ["src/plugins/**"])
        self.assertEqual(_mod.path_globs(".worktreeinclude"), [".worktreeinclude"])
        # A symlink marker is a file named after the directory, and it is in no spec directory
        # of its own.
        self.assertEqual(_mod.path_globs(".claude/.agents"), [".claude/.agents"])

    def test_the_outermost_glob_that_a_heuristic_explains_counts_the_path(self):
        """`.claude/.claude-plugin/**` names a plugin directory that Claude Code does not read
        inside its own directory, which covers whatever the directory contains."""
        self.assertIn(".claude/.claude-plugin/**", _mod.NOT_CONFIGURATION)
        self.assertEqual(_mod.path_glob(".claude/.claude-plugin/skills/bump.sh"),
                         ".claude/.claude-plugin/**")

    def test_without_an_explanation_the_innermost_glob_counts_the_path(self):
        self.assertEqual(_mod.path_glob("tt_metal/tt-llk/.claude/newplace/x.md"),
                         ".claude/newplace/**")

    def test_no_path_of_not_configuration_names_a_spec_directory_below_the_root(self):
        """A row such as `frontend/.claude/**` could never match, because the check judges a file
        of that directory at its path below `.claude`. The row would also suggest that the
        reasons of the root's `.claude` do not hold below the root."""
        for key in _mod.NOT_CONFIGURATION:
            directories = key.split("/")[:-1]
            with self.subTest(key=key):
                if any(name in _mod.SPEC_DIRECTORIES for name in directories[1:]):
                    self.assertIn(directories[0], _mod.SPEC_DIRECTORIES)
                    probe = key[:-len("**")] + "dir/file" if key.endswith("/**") else key
                    self.assertIn(key, _mod.path_globs(probe))


class TestSeptemberPlanningDocuments(unittest.TestCase):
    def test_optional_planning_instructions_have_a_specific_explanation(self):
        self.assertIn('.agent/PLANS.md', _mod.NOT_CONFIGURATION)
        self.assertNotIn('.agent/**', _mod.NOT_CONFIGURATION)
        self.assertIn('AGENTS.md', _mod.NOT_CONFIGURATION['.agent/PLANS.md'])


class TestTheListsComeFromTheHeuristics(unittest.TestCase):
    """The check reads both lists from the heuristics rather than from literals in its module.

    A list in code has no source, no date and no check that reports when an entry stops
    matching anything. `config_path_patterns.csv` and `ai_commit_patterns.csv` exist to prevent
    such lists.
    """

    def test_the_paths_come_from_not_configuration(self):
        self.assertEqual(_mod.NOT_CONFIGURATION, heuristics.not_configuration())

    def test_the_attributions_come_from_not_ai_commits(self):
        self.assertEqual(_mod.NOT_AI_COMMITS, heuristics.not_ai_commits())

    def test_the_module_contains_no_copy_of_the_list(self):
        """With a second copy, the behavior could differ from the file without a failing test."""
        source = Path(_mod.__file__).read_text(encoding="utf-8")
        for pattern in list(_mod.NOT_CONFIGURATION) + list(_mod.NOT_AI_COMMITS):
            with self.subTest(pattern=pattern):
                self.assertNotIn(f'"{pattern}"', source)




    def test_negative_path_explanations_keep_evidence_when_a_new_sample_lacks_the_path(self):
        for row in heuristics.load_not_configuration():
            with self.subTest(path=row["path"]):
                self.assertTrue(row["notes"].strip())
                self.assertTrue(row["doc_url"] if row["source"] == "doc" else row["evidence"])
                self.assertEqual(glob_for(row["path"]), row["path"])
                self.assertIn(row["path"], _mod.NOT_CONFIGURATION)

    def test_every_commit_row_still_matches_a_tracked_attribution(self):
        """Every row of not_ai_commits.csv matches an unmatched attribution of the last
        collection, which `unmatched reduce` tracks as a dated archive under output/.

        The test checks each row against the tracked archive rather than the cloning step's own
        file, because the cloning step rewrites that file on every run. It matches a row as
        `unmatched commits` does, through `explains`, and a change to how the check reads a
        pattern therefore changes the result of this test. The test reads the archive when its
        CSV is not extracted beside it, and it is skipped only where 7zz is missing.
        """
        archive = _mod.tracked_archive()
        if not archive.with_suffix("").is_file() and shutil.which(SEVEN_ZIP) is None:
            self.skipTest(f"7zz is not on PATH, and the unmatched attributions are only in "
                          f"{archive.name}")
        tokens = [entry[1] for entry in _mod.read_tracked(archive)]
        self.assertGreater(len(tokens), 1000, f"{archive.name} was read as almost empty")
        rows = heuristics.load_not_ai_commits()
        self.assertTrue(rows)
        dead = [row["pattern"] for row in rows
                if not any(_mod.explains(row["pattern"], token) for token in tokens)]
        self.assertEqual(dead, [], f"not_ai_commits.csv rows that match no attribution in "
                                   f"{archive.name}")


class TestIsExplained(unittest.TestCase):
    def test_an_exact_heuristics_path_is_explained(self):
        self.assertTrue(is_explained(".cursorignore", {".cursorignore"}))

    def test_an_unknown_path_is_not(self):
        self.assertFalse(is_explained(".newtool/config.json", {".cursorignore"}))

    def test_a_file_under_a_heuristics_directory_is_not_explained(self):
        """other.csv contains the files that no heuristic matched. The path glob of a file
        inside a mechanism's own directory is therefore unexplained, and the check has to list
        it."""
        self.assertFalse(is_explained(".claude/skills/**", {".claude/skills"}))

    def test_nothing_is_explained_by_an_empty_heuristics(self):
        self.assertFalse(is_explained(".claude/rules/**", set()))


class TestTheFailureThresholdAndTheListedGlobs(unittest.TestCase):
    """--min-repos decides what the check lists, and --fail-above decides what fails the
    check. `--fail-above 2` has to fail on a glob in exactly two repositories, because a
    threshold applied to the listed globs could never be reached below --min-repos."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.csv = self.tmp / "other.csv"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rows):
        with self.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_HEADER)
            w.writeheader()
            for repo, path, *link in rows:
                w.writerow({**{c: "" for c in _HEADER}, "repo_name": repo, "other": path,
                            "link_path": "".join(link)})

    def _run(self, *extra):
        """Every input points into the fixture, including the inputs that it does not write.

        A run here therefore reads neither the real `deletion.csv` nor the collected tree, and
        `--fail-above` applies to what the fixture wrote rather than to what the sample
        contains.
        """
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.checks.unmatched", "paths",
             "--other-csv", str(self.csv),
             "--deletion-csv", str(self.tmp / "deletion.csv"),
             "--collected-dir", str(self.tmp / "collected"),
             "--validated-dir", str(self.tmp / "validated"), *extra],
            capture_output=True, text=True, timeout=120)

    def _pattern(self, repos, path=".newtool/config/x.json"):
        self._write([(f"o/{i}", path) for i in range(repos)])

    def test_nothing_unexplained_is_zero(self):
        self._write([])
        self.assertEqual(self._run().returncode, 0)

    def test_a_glob_below_min_repos_is_not_listed(self):
        self._pattern(2)
        result = self._run()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(".newtool", result.stderr)

    def test_a_glob_at_min_repos_is_listed(self):
        self._pattern(3)
        result = self._run()
        self.assertIn(".newtool", result.stderr)

    def test_fail_above_below_min_repos_still_fires(self):
        self._pattern(2)
        self.assertEqual(self._run("--fail-above", "2").returncode, 2)

    def test_fail_above_over_the_count_does_not_fire(self):
        self._pattern(2)
        self.assertEqual(self._run("--fail-above", "5").returncode, 0)

    def test_a_file_behind_a_link_is_judged_at_the_path_the_tool_reads(self):
        """`skills/README.md` behind a `.claude/skills` link is the README that the row
        `.claude/skills/**` of not_configuration.csv explains. Under the path glob of its own
        path, the file would fail the check in every repository that links its skills this
        way."""
        self._write([(f"o/{i}", "skills/README.md", ".claude/skills/README.md")
                     for i in range(3)])
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self._write([(f"o/{i}", "skills/README.md") for i in range(3)])
        self.assertEqual(self._run("--fail-above", "2").returncode, 2)

    def test_fail_above_names_the_largest_glob(self):
        self._write([("o/1", ".alpha/config/x.json"), ("o/2", ".alpha/config/x.json"),
                     ("o/3", ".beta/config/y.json")])
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 2)
        self.assertIn(".alpha", result.stderr)

    def test_a_glob_that_not_configuration_names_is_not_a_failure(self):
        key = next(iter(_mod.NOT_CONFIGURATION))
        self._write([(f"o/{i}", key) for i in range(5)])
        self.assertEqual(self._run("--fail-above", "1").returncode, 0)

    def test_a_path_the_heuristics_name_is_not_a_failure(self):
        """A check that read no path from config_path_patterns.csv would fail only this test,
        because no heuristic names the other paths of the fixture."""
        self.assertNotIn(".cursorignore", _mod.NOT_CONFIGURATION)
        self._write([(f"o/{i}", ".cursorignore") for i in range(5)])
        self.assertEqual(self._run("--fail-above", "1").returncode, 0)

    def test_an_ignore_file_below_the_root_is_not_a_failure(self):
        """Cursor and Gemini CLI read their ignore files at any depth, and the rows of
        config_path_patterns.csv name them by file name. StarRocks/starrocks keeps a Cursor
        ignore list in `contrib/starrocks-python-client/.cursorignore`."""
        self._write([(f"o/{i}", path) for i in range(2)
                     for path in ("pkg/.cursorignore", "a/b/.cursorindexingignore",
                                  "tools/gemini-computer-use-eval/.geminiignore")])
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 in total", result.stderr)

    def test_the_same_place_at_the_root_and_below_it_is_counted_together(self):
        """olegbl/d2rmm keeps `.claude/patterns.md` and wso2/oxygen-ui
        `packages/oxygen-ui/.claude/patterns.md`. Counted under `packages/oxygen-ui/**`, the
        second file would never reach the threshold together with the first."""
        path = ".claude/conventions-of-this-project.md"
        self.assertNotIn(path, _mod.NOT_CONFIGURATION)
        self._write([("o/root", path), ("o/nested", f"packages/ui/{path}")])
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(f"{path} appears in 2 repositories", result.stderr)

    def test_a_reason_for_a_place_explains_it_below_the_root(self):
        """readest/readest keeps Claude Code's memory notes in
        `apps/readest-app/.claude/memory/`, which the row `.claude/memory/**` explains wherever
        the `.claude` is. tenstorrent/tt-metal keeps scripts in `tt_metal/tt-llk/.claude/scripts/`
        and `tt_metal/tt-llk/.cursor/scripts/`."""
        for key in (".claude/memory/**", ".claude/scripts/**", ".cursor/scripts/**"):
            self.assertIn(key, _mod.NOT_CONFIGURATION)
        self._write([(f"o/{i}", path) for i in range(3)
                     for path in ("apps/readest-app/.claude/memory/MEMORY.md",
                                  "tt_metal/tt-llk/.claude/scripts/run.sh",
                                  "tt_metal/tt-llk/.cursor/scripts/run.sh")])
        result = self._run("--fail-above", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 in total", result.stderr)

    def test_a_reason_for_a_spec_directory_explains_what_it_contains(self):
        """openshift/ci-tools keeps a plugin directory inside `.claude`, which the row
        `.claude/.claude-plugin/**` explains, although the files are below a second spec
        directory, `.claude-plugin`."""
        self._write([(f"o/{i}", ".claude/.claude-plugin/skills/bump.sh") for i in range(3)])
        result = self._run("--fail-above", "1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_dropped_ignore_file_below_the_root_is_not_a_failure(self):
        self._write([])
        self._dropped(2, "website/.cursorignore")
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 in total. Their files are in no output CSV", result.stderr)

    def test_a_dropped_place_below_the_root_is_ranked_at_the_place(self):
        self._write([])
        self._dropped(3, "packages/api/.codex/newtool/x.json")
        out = self.tmp / "unmatched-paths.csv"
        result = self._run("--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        with out.open(newline="", encoding="utf-8") as f:
            self.assertEqual([(r["glob"], r["repos"]) for r in csv.DictReader(f)],
                             [(".codex/newtool/**", "3")])

    def _dropped(self, repos, path):
        """Write the lines of the deletion log and the directories of the collected tree for
        `repos` repositories that the cleanup removed because they matched no mechanism, each
        containing `path`."""
        collected = self.tmp / "collected"
        with (self.tmp / "deletion.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["path", "reason"])
            for i in range(repos):
                name = f"o§dropped{i}"
                # The reason that the collected file cleanup writes, spelled out, because the
                # check reads it back.
                w.writerow([f"data/validated/{name}",
                            "no detected mechanism, repository directory removed"])
                file = collected / name / path
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("x\n", encoding="utf-8")

    def test_a_glob_only_dropped_repositories_hold_fails_at_the_threshold(self):
        """The other tests point the deletion log at a missing file. Only this test reads the
        dropped repositories, and a threshold that ignored them would pass the other tests."""
        self._write([])
        self._dropped(2, ".codex/newtool/x.json")
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(".codex/newtool/**", result.stderr)
        self.assertEqual(self._run("--fail-above", "3").returncode, 0)

    def test_the_file_contains_both_populations_and_the_console_neither(self):
        """The check writes the globs of the dropped repositories to the file beside the globs
        of other.csv. The log prints neither list, and the file keeps both."""
        self._write([(f"o/{i}", ".alpha/config/x.json") for i in range(3)])
        self._dropped(3, ".codex/newtool/x.json")
        out = self.tmp / "unmatched-paths.csv"
        result = self._run("--out", str(out))
        self.assertNotIn(".alpha", result.stderr)
        self.assertNotIn(".codex/newtool", result.stderr)
        with out.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(tuple(rows[0]), _mod.PATHS_OUT_COLUMNS)
        self.assertEqual({(r["glob"], r["found_in"]) for r in rows},
                         {(".alpha/config/**", "other.csv"),
                          (".codex/newtool/**", _mod.DROPPED_FOUND_IN)})

    def test_the_file_ends_each_record_with_lf(self):
        """The file of `--out` ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default."""
        self._write([(f"o/{i}", ".alpha/config/x.json") for i in range(3)])
        self._dropped(3, ".codex/newtool/x.json")
        out = self.tmp / "unmatched-paths.csv"
        result = self._run("--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"\r\n", out.read_bytes())
        with out.open(newline="", encoding="utf-8") as f:
            self.assertEqual({r["found_in"] for r in csv.DictReader(f)},
                             {"other.csv", _mod.DROPPED_FOUND_IN})

    def test_a_dropped_glob_not_configuration_names_is_not_a_failure(self):
        self.assertIn(".claude/docs/**", _mod.NOT_CONFIGURATION)
        self._write([])
        self._dropped(5, ".claude/docs/guide.md")
        result = self._run("--fail-above", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 in total. Their files are in no output CSV", result.stderr)

    def test_a_dropped_path_the_heuristics_name_is_not_a_failure(self):
        self._write([])
        self._dropped(5, ".cursorignore")
        result = self._run("--fail-above", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("0 in total. Their files are in no output CSV", result.stderr)

    def test_a_missing_input_is_one_rather_than_a_clean_report(self):
        self.assertEqual(self._run().returncode, 1)

    def test_an_input_that_is_not_other_csv_is_one_rather_than_a_clean_report(self):
        """A file with no header would be parsed as a CSV of no rows, and the check would exit
        0 with nothing unmatched over a file that it never read. A writer killed between the
        truncate and the flush leaves such a file, and the runner runs the check as a step."""
        for content in (b"", b"a,b\n1,2\n", b"\xff\xfe\x00\x01"):
            with self.subTest(content=content[:8]):
                self.csv.write_bytes(content)
                result = self._run("--fail-above", "3")
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("ERROR", result.stderr)


class TestTheFilesThatAnExcludeLeavesOut(unittest.TestCase):
    """An item of an Antigravity default place that the top-level `exclude` of a registration
    file leaves out is an unclassified file, and the detection step writes it into other.csv.
    The check counts such a file separately, because the exclude explains it, and does not rank
    its path glob as a path that lacks a heuristic.

    The repositories keep their customization root in `packages/app/`, as a monorepo does. The
    root is the legacy `.agent`, and the place is its `agents/`, because not_configuration.csv
    names no path glob of that place, `.agent/agents/**`, while it names `.agents/agents/**`
    for the files in the default place that are not subagents. The skills are in `_agents`,
    whose `skills/` it does not name either."""

    FRONT = "---\nname: {0}\ndescription: A subagent\n---\nReview the change.\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.csv = self.tmp / "other.csv"
        self.validated = self.tmp / "validated"

    def _repo(self, tree: Path, name: str, excluded=("defaultone",)) -> list:
        """A repository with a `packages/app/.agent/agents.json` that excludes `excluded`, the
        agents `defaultone` and `kept` in the default place and a file beside them that is no
        subagent. Returns the rows that the detection step writes into other.csv for the
        repository."""
        root = tree / name.replace("/", "§") / "packages" / "app" / ".agent"
        (root / "agents").mkdir(parents=True)
        (root / "agents.json").write_text(json.dumps({"exclude": list(excluded)}),
                                          encoding="utf-8")
        for agent in ("defaultone", "kept"):
            (root / "agents" / f"{agent}.md").write_text(self.FRONT.format(agent),
                                                         encoding="utf-8")
        (root / "agents" / "notes.txt").write_text("notes\n", encoding="utf-8")
        rows = [(name, "packages/app/.agent/agents/notes.txt")]
        if "defaultone" in excluded:
            rows.append((name, "packages/app/.agent/agents/defaultone.md"))
        return rows

    def _write(self, rows):
        with self.csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_HEADER)
            w.writeheader()
            for repo, path in rows:
                w.writerow({**{c: "" for c in _HEADER}, "repo_name": repo, "other": path})

    def _run(self, *extra, validated=None):
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.checks.unmatched", "paths",
             "--other-csv", str(self.csv),
             "--deletion-csv", str(self.tmp / "deletion.csv"),
             "--collected-dir", str(self.tmp / "collected"),
             "--validated-dir", str(validated or self.validated), *extra],
            capture_output=True, text=True, timeout=120)

    def test_an_agent_that_agents_json_excludes_is_not_ranked(self):
        # Only the excluded agent, because the file beside it is ranked whatever an exclude says.
        rows = [row for i in range(3) for row in self._repo(self.validated, f"o/r{i}")
                if row[1].endswith(".md")]
        self._write(rows)
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(".agent/agents/**", result.stderr)
        self.assertIn("Did not rank 3 files of other.csv in 3 repositories. The "
                      "top-level exclude of an Antigravity registration file leaves them out",
                      result.stderr)
        self.assertIn("0 in total", result.stderr)

    def test_without_the_validated_tree_the_excluded_agent_is_ranked(self):
        rows = [row for i in range(3) for row in self._repo(self.validated, f"o/r{i}")
                if row[1].endswith(".md")]
        self._write(rows)
        result = self._run("--fail-above", "2", validated=self.tmp / "absent")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn(".agent/agents/**", _mod.NOT_CONFIGURATION)
        self.assertIn(".agent/agents/** appears in 3 repositories", result.stderr)
        self.assertIn("absent does not exist", result.stderr)

    def test_a_file_that_no_exclude_names_is_still_ranked(self):
        """The exclude explains the agent that it names and not the file beside it. That file is
        no subagent, and other.csv contains it in each of the four repositories, whatever their
        registration file names."""
        rows = [row for i in range(3) for row in self._repo(self.validated, f"o/r{i}")]
        rows += self._repo(self.validated, "o/plain", excluded=("another",))
        self._write(rows)
        out = self.tmp / "unmatched-paths.csv"
        result = self._run("--min-repos", "1", "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Did not rank 3 files of other.csv in 3 repositories", result.stderr)
        with out.open(newline="", encoding="utf-8") as f:
            ranked = {(r["glob"], r["repos"], r["files"]) for r in csv.DictReader(f)}
        self.assertEqual(ranked, {(".agent/agents/**", "4", "4")})

    def test_the_support_files_of_a_skill_left_out_are_not_ranked(self):
        """A skill that `_agents/skills.json` excludes leaves its script in other.csv for the same
        reason as its SKILL.md, because a skill that the detection step counts covers every file
        of its folder."""
        rows = []
        for i in range(3):
            skill = self.validated / f"o§s{i}" / "packages" / "app" / "_agents"
            (skill / "skills" / "release").mkdir(parents=True)
            (skill / "skills.json").write_text(json.dumps({"exclude": ["release"]}),
                                               encoding="utf-8")
            (skill / "skills" / "release" / "SKILL.md").write_text(
                self.FRONT.format("release"), encoding="utf-8")
            (skill / "skills" / "release" / "publish.sh").write_text("echo\n", encoding="utf-8")
            rows += [(f"o/s{i}", "packages/app/_agents/skills/release/SKILL.md"),
                     (f"o/s{i}", "packages/app/_agents/skills/release/publish.sh")]
        self._write(rows)
        self.assertNotIn("_agents/skills/**", _mod.NOT_CONFIGURATION)
        result = self._run("--fail-above", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Did not rank 6 files of other.csv in 3 repositories", result.stderr)


class _Case(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.unmatched_csv = self.tmp / "unmatched.csv"

    def _write(self, rows) -> None:
        with self.unmatched_csv.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["kind", "token", "commits", "repos"])
            writer.writerows(rows)

    def _run(self, *extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.checks.unmatched", "commits", "--unmatched-csv", str(self.unmatched_csv), *extra],
            capture_output=True, text=True, timeout=120)


class TestTheVerdict(_Case):

    def test_an_absent_unmatched_attributions_csv_is_one(self):
        # Exit 1 rather than 0. The cloning step has not run, and having nothing to check
        # differs from a run that found nothing.
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Run repository-cloning to write it", result.stderr)

    def test_an_unmatched_attributions_csv_missing_a_column_is_one(self):
        self.unmatched_csv.write_text("kind,token\nauthor,x\n", encoding="utf-8")
        result = self._run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("repos", result.stderr)

    def test_nothing_unexplained_is_zero(self):
        self._write([["author", "dependabot[bot] <x@y.test>", 90, 40]])
        self.assertEqual(self._run().returncode, 0)

    def test_something_unexplained_is_still_zero_without_a_threshold(self):
        # The check lists the entries by default and fails only with --fail-above. A person
        # who reads its log sees every entry, and a pipeline gets a verdict.
        self._write([["trailer", "co-authored-by: devin ai", 80, 40]])
        self.assertEqual(self._run().returncode, 0)

    def test_something_at_the_threshold_is_two(self):
        self._write([["trailer", "co-authored-by: devin ai", 80, 40]])
        result = self._run("--fail-above", "3")
        self.assertEqual(result.returncode, 2)
        self.assertIn("devin", result.stderr)

    def test_something_below_the_threshold_is_zero(self):
        self._write([["trailer", "co-authored-by: devin ai", 2, 2]])
        self.assertEqual(self._run("--fail-above", "3").returncode, 0)


class TestKnownUnmatchedAttributions(_Case):

    def test_a_known_entry_is_not_reported(self):
        self._write([["author", "dependabot[bot] <49699333+dependabot[bot]@x.test>", 90, 40]])
        self.assertEqual(self._run("--fail-above", "3").returncode, 0)

    def test_a_known_entry_matches_as_a_substring(self):
        # An identity comes with whatever name and address the account uses. The key is
        # matched as a substring of the token, because the account is the part that stays the
        # same.
        self.assertTrue(_mod.explained_by("dependabot <49699333+dependabot[bot]@x.test>"))

    def test_an_unknown_entry_is_reported(self):
        self.assertEqual(_mod.explained_by("co-authored-by: devin ai"), "")

    def test_every_listed_attribution_has_a_reason(self):
        for key, reason in _mod.NOT_AI_COMMITS.items():
            with self.subTest(key=key):
                self.assertTrue(reason.strip(),
                                f"{key} has no reason in not_ai_commits.csv")


class TestTheReport(_Case):

    def test_entries_are_ranked_by_repositories(self):
        entries = [("trailer", "a", 100, 2), ("trailer", "b", 1, 9)]
        self.assertEqual([e[1] for e in _mod.unmatched_of(entries)], ["b", "a"])

    def test_commits_break_a_tie_on_repositories(self):
        entries = [("trailer", "a", 1, 5), ("trailer", "b", 9, 5)]
        self.assertEqual([e[1] for e in _mod.unmatched_of(entries)], ["b", "a"])

    def test_min_repos_limits_what_is_printed_but_not_what_fails(self):
        # --min-repos limits what a reader is shown. --fail-above decides the verdict, and an
        # entry below --min-repos still fails the check when it reaches that threshold.
        self._write([["trailer", "co-authored-by: devin ai", 2, 2]])
        result = self._run("--min-repos", "5", "--fail-above", "2")
        self.assertIn("No such attribution", result.stderr)
        self.assertEqual(result.returncode, 2)

    def test_the_out_file_contains_what_was_reported(self):
        self._write([["trailer", "co-authored-by: devin ai", 80, 40]])
        out = self.tmp / "report.csv"
        self._run("--out", str(out))
        with out.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows, [{"kind": "trailer", "token": "co-authored-by: devin ai",
                                 "repos": "40", "commits": "80"}])

    def test_the_out_file_ends_each_record_with_lf(self):
        """The file of `--out` ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default."""
        self._write([["trailer", "co-authored-by: devin ai", 80, 40],
                     ["trailer", "co-authored-by: person 01", 9, 5]])
        out = self.tmp / "report.csv"
        result = self._run("--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"\r\n", out.read_bytes())
        with out.open(newline="", encoding="utf-8") as f:
            self.assertEqual([r["token"] for r in csv.DictReader(f)],
                             ["co-authored-by: devin ai", "co-authored-by: person 01"])

    def test_a_list_that_goes_to_a_file_is_not_printed(self):
        """The runner copies a step's output to the container's log, and the whole ranked
        list would fill that log. When the file contains the list, the log gets only the
        counts."""
        self._write([["trailer", f"co-authored-by: person {n:02d}", 10, 100 - n]
                     for n in range(25)])
        out = self.tmp / "report.csv"
        result = self._run("--out", str(out))
        self.assertNotIn("person", result.stderr)
        self.assertIn("25 unmatched attributions in 3 repositories or more", result.stderr)
        self.assertIn(f"Wrote {out}", result.stderr)
        with out.open(newline="", encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), 25)
        whole = self._run()
        self.assertEqual(len([l for l in whole.stderr.splitlines() if "person" in l]), 25)


def _commit_log(commits_of_log: list) -> bytes:
    """A commit log in the format `nul-v1` in which the cloning step keeps it, from commits
    given as `(hash, author, body)`."""
    raw = b""
    for sha, author, body in commits_of_log:
        message = f"Subject\n\n{body}"
        fields = (sha, author, f"{author.lower()}@example.com", author,
                  f"{author.lower()}@example.com", "2026-11-01T00:00:00Z", "Subject", body,
                  message)
        raw += b"\x00" + b"\x00".join(field.encode("utf-8") for field in fields) + b"\x00\n"
    return raw


class TestThePrefixedCredits(_Case):
    """The heuristics read a token after `AI-`, `Co-`, `Tests-` or `Almost-Exclusively-` as the
    token itself, and the ranking of what no heuristic explains lists such a credit too, under
    its whole token. The cloning step lists none, and the check reads them from the commit logs
    that it keeps. caddyserver/caddy 401582cccfb3 credits Jcode as `AI-assisted-by`, and
    tektoncd/operator 19d139ab1a2b a bot as `Co-Assisted-By`."""

    def setUp(self):
        super().setUp()
        self.commits_dir = self.tmp / "collect_commits"
        self.commits_dir.mkdir()
        body = ("AI-assisted-by: Jcode (OpenAI)\n"
                "Co-Assisted-By: CVE Fixer Bot <cve-fixer@redhat.com>\n"
                "Co-written-by: Some Person\n"
                "Almost-Exclusively-Authored-By: Some Agent\n"
                "Co-developed-by: Some Developer <d@example.com>\n"
                "Commit-Message-Assisted-by: Some Tool\n")
        # The second commit credits Claude in a co-author line that a heuristic matches, and
        # its other credits are not unmatched.
        matched = "AI-assisted-by: Other Tool\nCo-authored-by: Claude <noreply@anthropic.com>"
        self._repository("o/a", [("a" * 40, "Dev", body), ("b" * 40, "Dev", matched)])
        self._repository("o/b", [("c" * 40, "Dev", "Fixes it.\n\nai-assisted-by: Jcode (OpenAI)"),
                                 ("d" * 40, "Dev", "ai-assisted-by: jcode (openai)")])
        self._repository("o/c", [("e" * 40, "Dev", "Nothing credited.")])
        self._write([["trailer", "co-developed-by: some developer", 1, 1]])

    def _repository(self, repo: str, commits_of_log: list) -> None:
        """A per-repository commit record of a commit history that was read, with its log."""
        name = repo.replace("/", "\u00a7")
        (self.commits_dir / f"{name}.json").write_text(json.dumps(
            {"repo_name": repo, "head_sha": commits_of_log[0][0], "branch": "main",
             "outcome": "history_read", "unmatched": [], "no_signal_shas": [], "ai_rows": [],
             "commit_evidence": {"log_format": "nul-v1"}}), encoding="utf-8")
        with lzma.open(self.commits_dir / f"{name}.log.xz", "wb") as f:
            f.write(_commit_log(commits_of_log))

    def _ranked(self, *extra) -> list:
        out = self.tmp / "report.csv"
        result = self._run("--min-repos", "1", "--out", str(out), *extra)
        self.assertEqual(result.returncode, 0, result.stderr)
        with out.open(newline="", encoding="utf-8") as f:
            return [(row["token"], row["repos"], row["commits"]) for row in csv.DictReader(f)]

    def test_a_credit_after_a_prefix_is_ranked_under_its_whole_token(self):
        self.assertEqual(self._ranked("--commits-dir", str(self.commits_dir)), [
            ("ai-assisted-by: jcode (openai)", "2", "3"),
            ("almost-exclusively-authored-by: some agent", "1", "1"),
            ("co-assisted-by: cve fixer bot", "1", "1"),
            ("co-developed-by: some developer", "1", "1"),
            ("co-written-by: some person", "1", "1")])

    def test_the_commit_logs_beside_the_output_directory_are_read_by_default(self):
        output = self.tmp / "output"
        output.mkdir()
        self.unmatched_csv = output / "all_ai_commits-unmatched.csv"
        self._write([])
        self.assertIn(("co-assisted-by: cve fixer bot", "1", "1"), self._ranked())

    def test_without_the_commit_logs_a_warning_names_what_is_missing(self):
        result = self._run("--commits-dir", str(self.tmp / "nowhere"), "--min-repos", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"WARNING unmatched_commits_report: {self.tmp / 'nowhere'} does not "
                      f"exist. The attributions lack the trailers that credit somebody after a "
                      f"prefix",
                      result.stderr)
        self.assertNotIn("ai-assisted-by", result.stderr)

    def test_a_commit_log_that_cannot_be_read_is_named_and_the_others_are_ranked(self):
        (self.commits_dir / "o\u00a7c.log.xz").write_bytes(b"not xz")
        out = self.tmp / "report.csv"
        result = self._run("--commits-dir", str(self.commits_dir), "--min-repos", "1",
                           "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"WARNING unmatched_commits_report: Could not read the commit log "
                      f"{self.commits_dir / 'o§c.log.xz'}", result.stderr)
        self.assertIn("co-assisted-by: cve fixer bot", out.read_text(encoding="utf-8"))

    def test_a_token_of_its_own_is_no_prefixed_credit(self):
        for token, credit in (("ai-assisted-by", True), ("tests-generated-by", True),
                              ("co-authored-by", False), ("co-authored-by-ai", False),
                              ("co-developed-by", False), ("commit-message-assisted-by", False),
                              ("assisted-by", False), ("co-signed-off-by", False)):
            with self.subTest(token=token):
                self.assertEqual(_mod.prefixed_credit(token), credit)


class TestTheDroppedRepositories(unittest.TestCase):
    """The check reads the files of a repository that the cleanup dropped from the collected
    tree, because such a repository is in no output CSV and no other check would list its
    unmatched globs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.collected = self.tmp / "collected"
        self.deletion = self.tmp / "deletion.csv"
        self.submodules = self.tmp / "submodules.csv"

    def _repo(self, name, files):
        for relative in files:
            path = self.collected / name / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n", encoding="utf-8")

    def _log(self, rows):
        with self.deletion.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["path", "reason"])
            w.writerows(rows)

    def test_only_the_repositories_dropped_for_no_mechanism_are_read(self):
        self._repo("o§dropped", [".codex/review-prompt.md"])
        self._repo("o§other", [".codex/kept.md"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON],
                   ["data/validated/o§other/x.md", "not a spec file"]])
        counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self.assertEqual(set(counts), {".codex/review-prompt.md"})
        self.assertEqual(counts[".codex/review-prompt.md"][1], {"o/dropped"})

    def test_the_planning_documents_are_left_out(self):
        """A planning document is explained by its heuristic, and other.csv leaves it out of a
        published repository. The planning documents of a dropped repository therefore give no
        unmatched path glob."""
        self._repo("o§dropped", [".codex/review-prompt.md", "openspec/specs/auth/spec.md",
                                 "openspec/changes/login/proposal.md"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON]])
        counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self.assertEqual(set(counts), {".codex/review-prompt.md"})

    def test_a_vendored_subtree_is_left_out(self):
        self._repo("o§dropped", [".claude/rules/a.md", "node_modules/pkg/.claude/rules/b.md"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON]])
        counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self.assertEqual(set(counts), {".claude/rules/**"})
        self.assertEqual(counts[".claude/rules/**"][0], 1, "the vendored copy is another project's")

    def test_a_submodules_files_are_left_out(self):
        self._repo("o§dropped", ["ext/skills/SKILL.md", ".codex/own.md"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON]])
        with self.submodules.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["repo_name", "path", "submodule_repo"])
            w.writerow(["o/dropped", "ext", "other/repo"])
        counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self.assertEqual(set(counts), {".codex/own.md"})

    def test_what_the_validation_removes_is_not_read(self):
        """The collector copies a `.github` directory whole when a Copilot path is in it, and
        copies a file with the name of a context file at any depth and in any letter case. The
        cleanup removes what is not configuration, such as CI files and library pages named
        agents.md. After validation, the remaining paths are what other.csv would have
        contained."""
        self._repo("o§dropped", [".github/hooks/pre-commit", ".github/ISSUE_TEMPLATE/bug.yml",
                                 ".github/workflows/ci.yml", "docs/src/agents.md", "lib/Agents.md",
                                 ".claude/notes.md"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON]])
        counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self.assertEqual(set(counts), {".claude/notes.md"})
        self.assertTrue((self.collected / "o§dropped" / "lib" / "Agents.md").is_file(),
                        "the collected tree is read, not changed")

    def _assert_read_without_eadir(self, counts):
        self.assertEqual(set(counts), {".claude/notes.md"})
        self.assertEqual(counts[".claude/notes.md"][0], 1)

    def test_dsm_removing_the_files_of_an_eadir_after_the_listing(self):
        """DSM rewrites what it keeps in an `@eaDir` at any time. A copy that included the
        directory would end in `shutil.Error` when a file of the directory was gone after the
        listing."""
        self._repo("o§dropped", [".claude/notes.md", ".claude/@eaDir/notes.md@SynoEAStream"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON]])
        with dsm_removes_files_after_listing(self.collected):
            counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self._assert_read_without_eadir(counts)

    def test_dsm_writing_an_eadir_into_a_directory_of_the_copy_first(self):
        """DSM indexes the copy too. A copy that included each `@eaDir` would end in
        `shutil.Error` when DSM wrote an `@eaDir` into a directory of the copy before the copy
        reached the `@eaDir` of the source directory."""
        self._repo("o§dropped", [".claude/notes.md", ".claude/@eaDir/notes.md@SynoEAStream"])
        self._log([["data/validated/o§dropped", _mod.DROPPED_REASON]])
        with dsm_writes_into_new_directories(".claude", Path(tempfile.gettempdir())) as written:
            counts = _mod.read_dropped(self.deletion, self.collected, self.submodules)
        self.assertEqual(len(written), 1, "DSM wrote into the copy")
        self._assert_read_without_eadir(counts)

    def test_a_directory_the_log_names_but_the_tree_lacks_is_skipped(self):
        self._log([["data/validated/o§gone", _mod.DROPPED_REASON]])
        self.collected.mkdir(parents=True, exist_ok=True)
        self.assertEqual(_mod.read_dropped(self.deletion, self.collected, self.submodules), {})

    def test_a_log_without_the_columns_is_refused(self):
        self.deletion.write_text("path\nx\n", encoding="utf-8")
        self.collected.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(ValueError):
            _mod.read_dropped(self.deletion, self.collected, self.submodules)


# What the lines of the reduce tests were collected under, and the collections that wrote them:
# the collection of the handover, started on 2026-11-02, and an earlier collection with
# repositories that the handover's collection took over.
UNDER = {"collector": "c", "heuristics": "h", "records": "r", "git": "g"}
CURRENT = "2026-11-02T225900Z-0a1b2c3d"
EARLIER = "2026-09-24T045800Z-4e5f6a7b"

# The columns that the cloning step writes, as `commits.assemble_ai_commits` writes them.
# `reduce` keeps the first four.
_UNMATCHED_HEADER = ["kind", "token", "commits", "repos", "shas", "lines"]
_UNMATCHED_ROWS = [
    ["author", "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>", "90", "40",
     "a1;b2", "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>"],
    # The token contains a comma, and `reduce` has to write and read it as CSV.
    ["trailer", "co-authored-by: example, release bot", "3", "2", "c3",
     "Co-authored-by: Example, Release Bot <bot@example.test>"],
    ["trailer", "co-authored-by: imgbot[bot]", "1", "1", "d4", "Co-authored-by: imgbot[bot]"],
]
# What `reduce` drops: a person named in a trailer, and a person's address under a bot's name.
_PEOPLE_ROWS = [
    ["trailer", "co-authored-by: jane doe", "3", "2", "e5", "Co-authored-by: Jane Doe <jd@x.test>"],
    ["author", "dependabot[bot] <jane.doe@example.test>", "1", "1", "f6",
     "dependabot[bot] <jane.doe@example.test>"],
]


class TestTheLinesThatTheArchiveKeeps(unittest.TestCase):
    """The rule of `unmatched reduce`, over lines with the forms of the real list. The names and
    the addresses of people are fictional."""

    # The lines of the same collection that show which names are people's.
    LIST = [("trailer", f"co-authored-by: {name}", 1, 1) for name in (
        "jane doe", "jane roe", "cody roe", "cody doe", "charlie roe", "jdoe", "max mustermann")]

    def keeps(self, kind: str, token: str) -> bool:
        return _mod.keeps(kind, token, _mod.personal_names(self.LIST))

    def test_a_github_app_is_kept(self):
        self.assertTrue(self.keeps("trailer", "co-authored-by: some-release-app[bot]"))
        self.assertTrue(self.keeps(
            "author", "some-release-app[bot] <123+some-release-app[bot]@users.noreply.github.com>"))
        self.assertTrue(self.keeps("author", "some-app <123+some-app[bot]@users.noreply.github.com>"))

    def test_an_address_that_the_limit_of_the_token_cuts_is_kept_for_an_app(self):
        self.assertTrue(self.keeps(
            "author", "some-long-named-app[bot] <123+some-long-named-app[b"))
        self.assertFalse(self.keeps("author", "jdoe-bot <jane.doe+long-mailbox-of-a-per"))

    def test_the_word_bot_is_kept(self):
        for name in ("release bot", "release-bot", "release_bot", "cve fixer bot"):
            with self.subTest(name=name):
                self.assertTrue(self.keeps("trailer", f"co-authored-by: {name}"))

    def test_a_surname_that_contains_bot_is_not_a_bot(self):
        for name in ("mary abbot", "tom talbot", "lebot", "abbot"):
            with self.subTest(name=name):
                self.assertFalse(self.keeps("trailer", f"co-authored-by: {name}"))
                self.assertFalse(self.keeps("author", f"{name} <person@example.org>"))

    def test_a_surname_that_is_the_word_bot_is_not_a_bot(self):
        """The first name makes the name a person's, as in a surname Bot or Le Bot."""
        self.assertFalse(self.keeps("trailer", "co-authored-by: jane bot"))
        self.assertFalse(self.keeps("trailer", "co-authored-by: jane le bot"))

    def test_a_bot_named_for_a_person_is_dropped(self):
        for name in ("jane-bot", "jane-doe-bot", "janedoe-bot", "jdoe-bot", "jdoe[bot]",
                     "jane-doe-s-bot[bot]"):
            with self.subTest(name=name):
                self.assertFalse(self.keeps("trailer", f"co-authored-by: {name}"))

    def test_a_bot_in_a_parenthesis_after_a_name_is_dropped(self):
        self.assertFalse(self.keeps("trailer", "co-authored-by: jane doe (bot)"))
        self.assertFalse(self.keeps("trailer", "co-authored-by: release bot (jdoe)"))

    def test_a_person_with_a_bot_in_the_address_is_dropped(self):
        for line in ("jane doe <jane.doe+bot@example.org>",
                     "release bot <jane.bot@example.org>",
                     "release bot <jane+release-bot@example.org>",
                     "release-bot <release-bot@gmail.com>",
                     "release bot <bot@example.org>",
                     "max mustermann <bot@example.org>",
                     "jane <456+jdoe-bot@users.noreply.github.com>",
                     "dependabot[bot] <jane.doe@example.org>"):
            with self.subTest(line=line):
                self.assertFalse(self.keeps("author", line))

    def test_the_mailbox_of_a_bot_at_the_domain_of_its_name_is_kept(self):
        for line in ("snyk-bot <snyk-bot@snyk.io>", "renovate bot <bot@renovateapp.com>",
                     "greenkeeperio-bot <support@greenkeeper.io>",
                     "openvix-bot <bot@openvix.co.uk>"):
            with self.subTest(line=line):
                self.assertTrue(self.keeps("author", line))

    def test_a_country_suffix_is_not_the_domain_that_a_name_has_to_fit(self):
        for line in ("release bot <noreply@example.co.uk>", "disco bot <noreply@example.co.uk>"):
            with self.subTest(line=line):
                self.assertFalse(self.keeps("author", line))

    def test_a_row_of_the_negatives_is_kept(self):
        """Every row of not_ai_commits.csv has to match a line of the tracked archive."""
        for pattern in _mod.NOT_AI_COMMITS:
            with self.subTest(pattern=pattern):
                self.assertTrue(self.keeps("trailer", f"co-authored-by: {pattern}"))
                self.assertTrue(self.keeps("author", f"{pattern} <{pattern}@example.org>"))

    def test_a_row_of_the_negatives_in_a_parenthesis_after_a_name_is_dropped(self):
        self.assertFalse(self.keeps("trailer", "co-authored-by: jane doe (weblate)"))

    def test_a_person_at_the_address_of_an_app_is_dropped(self):
        """A person's name on the address of the app of GitHub's workflows names the person,
        although the address matches a row of the negatives."""
        self.assertFalse(self.keeps(
            "author", "jdoe <41898282+github-actions[bot]@users.noreply.github.com>"))

    def test_a_tool_is_kept_where_its_name_is_no_first_name(self):
        for name in ("workbuddy", "kilo code", "swe-agent"):
            with self.subTest(name=name):
                self.assertTrue(self.keeps("trailer", f"co-authored-by: {name}"))

    def test_a_tool_with_a_model_a_version_or_a_kind_is_kept(self):
        for name in ("cody:claude 3.5 sonnet", "cline v3.27.0", "zcode (glm-5.3)", "jules agent",
                     "claude ai"):
            with self.subTest(name=name):
                self.assertTrue(self.keeps("trailer", f"assisted-by: {name}"))

    def test_a_person_with_the_first_name_of_a_tool_is_dropped(self):
        for name in ("cody roe", "charlie roe", "charlie-roe", "jules.roe", "zed(zijun) roe"):
            with self.subTest(name=name):
                self.assertFalse(self.keeps("trailer", f"co-authored-by: {name}"))
        self.assertFalse(self.keeps("author", "claude roe <claude@example.org>"))
        self.assertFalse(self.keeps("author", "claude <jane.doe@gmail.com>"))

    def test_a_tool_that_a_person_is_also_named_for_is_dropped_alone(self):
        """`co-authored-by: cody` can be a person when the list has `cody roe`, and a tool that
        no name begins with cannot."""
        self.assertFalse(self.keeps("trailer", "co-authored-by: cody"))
        self.assertFalse(self.keeps("trailer", "co-authored-by: charlie"))

    def test_a_tool_after_the_name_of_a_person_is_dropped(self):
        """A person's name before the tool, as in `Name (aider)`, names the person."""
        self.assertFalse(self.keeps("trailer", "co-authored-by: jane doe (aider)"))
        self.assertFalse(self.keeps("trailer", "co-authored-by: max mustermann (claude opus 4.6)"))

    def test_a_token_that_marks_an_agent_is_kept(self):
        self.assertTrue(self.keeps("trailer", "ai-assisted-by: some tool (some vendor)"))
        self.assertTrue(self.keeps("trailer", "co-authored-by-agent: some tool"))
        self.assertFalse(self.keeps("trailer", "co-developed-by: some tool"))

    def test_a_person_under_a_token_that_marks_an_agent_is_dropped(self):
        """A name under such a token passes no other check, and the same list can show it as a
        person's. The line's own name does not count as a person's, or no such line would stay."""
        entries = [("trailer", "co-authored-by: jane doe", 1, 1),
                   ("trailer", "co-authored-by: mary ann roe", 1, 1),
                   ("trailer", "ai-assisted-by: jcode (openai)", 1, 1)]
        people = _mod.personal_names(entries)
        self.assertFalse(_mod.keeps("trailer", "ai-assisted-by: jane doe", people))
        self.assertFalse(_mod.keeps("trailer", "ai-assisted-by: mary ann roe", people))
        self.assertTrue(_mod.keeps("trailer", "ai-assisted-by: jcode (openai)", people))

    def test_a_given_name_under_a_token_that_marks_an_agent_is_dropped(self):
        self.assertFalse(self.keeps("trailer", "ai-assisted-by: jane austen"))

    def test_an_app_at_githubs_address_is_kept_beside_a_plain_trailer_of_its_name(self):
        """Only an account at GitHub's own address shows that an app exists, so a plain trailer
        of the same name is not a person's. A mark on a trailer alone shows nothing, and the
        plain trailer itself fits no form of a bot."""
        people = _mod.personal_names([
            ("trailer", "co-authored-by: mergify", 1, 1),
            ("trailer", "co-authored-by: jdoe", 1, 1),
            ("author", "mergify[bot] <37929162+mergify[bot]@users.noreply.github.com>", 1, 1)])
        self.assertTrue(_mod.keeps(
            "author", "mergify[bot] <37929162+mergify[bot]@users.noreply.github.com>", people))
        self.assertTrue(_mod.keeps(
            "author", "mergify <37929162+mergify[bot]@users.noreply.github.com>", people))
        self.assertTrue(_mod.keeps("trailer", "co-authored-by: mergify[bot]", people))
        self.assertFalse(_mod.keeps("trailer", "co-authored-by: mergify", people))
        self.assertFalse(_mod.keeps("trailer", "co-authored-by: jdoe[bot]", people))

    def test_a_first_word_that_several_names_share_does_not_drop_a_marked_app(self):
        """`github actions` and `github copilot` make `github` a given name, and the mark of an
        app shows what the name is. Without the mark, the first word still drops the line."""
        people = _mod.personal_names([
            ("trailer", "co-authored-by: github actions", 1, 1),
            ("trailer", "co-authored-by: github staff", 1, 1)])
        self.assertTrue(_mod.keeps("trailer", "co-authored-by: github-merge[bot]", people))
        self.assertTrue(_mod.keeps(
            "author", "github-merge <12+github-merge[bot]@users.noreply.github.com>", people))
        self.assertFalse(_mod.keeps("trailer", "co-authored-by: github-merge-bot", people))

    def test_a_name_with_an_at_sign_or_an_apostrophe_is_dropped(self):
        self.assertFalse(self.keeps("trailer", "co-authored-by: bot@example.org"))
        self.assertFalse(self.keeps("trailer", "co-authored-by: jane's bot"))
        self.assertFalse(self.keeps("author", "a bot of @jdoe <123+jdoe-bot@users.noreply.github.com>"))

    def test_an_empty_name_is_dropped(self):
        self.assertFalse(self.keeps("trailer", "written-by: "))
        self.assertFalse(self.keeps("author", " <person@example.org>"))

    def test_a_line_that_fits_no_form_is_dropped(self):
        for name in ("root", "your name", "ubuntu", "anonymous"):
            with self.subTest(name=name):
                self.assertFalse(self.keeps("trailer", f"co-authored-by: {name}"))


@unittest.skipIf(shutil.which(SEVEN_ZIP) is None, "7zz is not on PATH")
class TestReduce(unittest.TestCase):
    """`unmatched reduce` over a handover laid out as a checkout contains it, with the real
    7zz."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.handover = self.tmp / "handover"
        self.handover.mkdir()
        self.unmatched = self.handover / "all_ai_commits-unmatched.csv"
        self.finished = self.handover / resume.FINISHED_NAME
        self.output = self.tmp / "output"
        self.output.mkdir()
        with self.unmatched.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(_UNMATCHED_HEADER)
            writer.writerows(_UNMATCHED_ROWS + _PEOPLE_ROWS)
        # A collection that started on 2026-11-02 and ran past midnight, so that its rows span
        # two days.
        self._line("o/b", "2026-11-03T01:00:00Z", CURRENT)
        self._line("o/a", "2026-11-02T23:00:00Z", CURRENT)

    def _line(self, repo: str, scanned_at: str, collection: str | None,
              collected_under: dict | None = UNDER) -> None:
        """A line of the list of finished repositories, as the cloning step writes it for a
        repository that `collection` collected."""
        resume.append_finished(self.finished, repo,
                               paths.build_csv_row(repo, scanned_at, "main", "a" * 40,
                                                   scanned_at, "b" * 40, set()),
                               None, collected_under=collected_under, collection=collection)

    def _reduce(self, *extra) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.checks.unmatched", "reduce",
             "--unmatched-csv", str(self.unmatched), "--finished", str(self.finished),
             "--output-dir", str(self.output), *extra],
            capture_output=True, text=True, timeout=120)

    def _archive_name(self) -> str:
        result = self._reduce()
        self.assertEqual(result.returncode, 0, result.stderr)
        return _mod.tracked_archive(self.output).name

    def test_the_archive_contains_the_credits_after_a_prefix_of_the_commit_logs(self):
        """The rows of not_ai_commits.csv are checked against the archive, and a row that
        explains a credit after a prefix, such as `AI-assisted-by:`, needs the credit there."""
        commits_dir = self.handover / "collect_commits"
        commits_dir.mkdir()
        (commits_dir / "o\u00a7a.json").write_text(json.dumps(
            {"repo_name": "o/a", "head_sha": "a" * 40, "branch": "main",
             "outcome": "history_read", "unmatched": [], "no_signal_shas": [], "ai_rows": [],
             "commit_evidence": {"log_format": "nul-v1"}}), encoding="utf-8")
        with lzma.open(commits_dir / "o\u00a7a.log.xz", "wb") as f:
            f.write(_commit_log([("a" * 40, "Dev", "AI-assisted-by: Jcode (OpenAI)")]))
        result = self._reduce("--commits-dir", str(commits_dir))
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = _mod.read_tracked(_mod.tracked_archive(self.output))
        self.assertIn(("trailer", "ai-assisted-by: jcode (openai)", 1, 1), rows)
        self.assertEqual(len(rows), len(_UNMATCHED_ROWS) + 1)

    def test_rows_taken_over_from_an_earlier_month_do_not_name_the_archive(self):
        """A repository that the collection took over keeps the line and the `scanned_at` of the
        September collection that collected it. The earliest `scanned_at` would name the
        archive for September."""
        self._line("o/c", "2026-09-24T05:00:00Z", EARLIER)
        self._line("o/d", "2026-09-25T07:00:00Z", EARLIER)
        self.assertEqual(self._archive_name(), "all_ai_commits-unmatched-2026-11-02.csv.7z")

    def test_the_collection_in_progress_names_the_archive(self):
        """A collection in progress that has taken over every repository so far wrote no line
        of its own. Its record beside the list names the collection."""
        self.finished.unlink()
        self._line("o/c", "2026-09-24T05:00:00Z", EARLIER)
        resume.save_collection(self.finished, {"collection": CURRENT, "input_sha256": "0" * 64,
                                               "taken_over": {"o/c"}})
        self.assertEqual(self._archive_name(), "all_ai_commits-unmatched-2026-11-02.csv.7z")

    def test_a_list_that_can_hold_no_repository_taken_over_is_named_for_its_first_scan(self):
        """The next collection collects the repository of a line again when the line does not
        record how the repository was collected. A list of such lines therefore holds the scans
        of one collection, as does the list of a handover from before collections were named."""
        self.finished.unlink()
        self._line("o/b", "2026-10-06T01:00:00Z", None, None)
        self._line("o/a", "2026-10-05T05:57:00Z", None, None)
        self.assertEqual(self._archive_name(), "all_ai_commits-unmatched-2026-10-05.csv.7z")

    def test_lines_that_could_be_taken_over_and_name_no_collection_are_refused(self):
        self.finished.unlink()
        self._line("o/b", "2026-11-03T01:00:00Z", None)
        self._line("o/c", "2026-09-24T05:00:00Z", None)
        result = self._reduce()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("names the collection that wrote it, and its repositories may have been "
                      "taken over", result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_the_archive_is_named_for_the_first_day_and_contains_the_four_columns(self):
        result = self._reduce()
        self.assertEqual(result.returncode, 0, result.stderr)
        archive = _mod.tracked_archive(self.output)
        self.assertEqual(archive.name, "all_ai_commits-unmatched-2026-11-02.csv.7z")
        work = self.tmp / "work"
        work.mkdir()
        subprocess.run([SEVEN_ZIP, "x", "-bso0", "-bsp0", "-y", f"-o{work}", str(archive)],
                       check=True, capture_output=True)
        with (work / "all_ai_commits-unmatched-2026-11-02.csv").open(
                newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows, [list(_mod.UNMATCHED_COLUMNS_READ)]
                         + [row[:4] for row in _UNMATCHED_ROWS])

    def test_the_archived_csv_ends_each_record_with_lf(self):
        """The CSV in the archive ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default."""
        result = self._reduce()
        self.assertEqual(result.returncode, 0, result.stderr)
        work = self.tmp / "work"
        work.mkdir()
        subprocess.run([SEVEN_ZIP, "x", "-bso0", "-bsp0", "-y", f"-o{work}",
                        str(_mod.tracked_archive(self.output))],
                       check=True, capture_output=True)
        written = work / "all_ai_commits-unmatched-2026-11-02.csv"
        self.assertNotIn(b"\r\n", written.read_bytes())
        with written.open(newline="", encoding="utf-8") as f:
            self.assertEqual(len(list(csv.DictReader(f))), len(_UNMATCHED_ROWS))

    def test_the_archive_holds_only_the_lines_that_name_no_person(self):
        """The check reads the tracked copy as it reads the cloning step's own file, and the
        tracked copy lacks the lines that name people."""
        self.assertEqual(self._reduce().returncode, 0)
        entries = _mod.read_tracked(_mod.tracked_archive(self.output))
        self.assertEqual(entries, [(row[0], row[1], int(row[2]), int(row[3]))
                                   for row in _UNMATCHED_ROWS])
        everything = _mod.load_unmatched(self.unmatched)
        self.assertEqual(len(everything), len(_UNMATCHED_ROWS) + len(_PEOPLE_ROWS))

    def test_it_replaces_the_tracked_copy_of_an_earlier_collection_and_its_extracted_csvs(self):
        older = self.output / "all_ai_commits-unmatched-2026-09-24.csv.7z"
        older.write_bytes(b"an earlier collection")
        stale = [self.output / "all_ai_commits-unmatched-2026-09-24.csv",
                 self.output / "all_ai_commits-unmatched-2026-11-02.csv"]
        for path in stale:
            path.write_text("kind,token,commits,repos\n", encoding="utf-8")
        kept = self.output / "all_ai_commits-unmatched.csv"
        kept.write_text("the live collection's file\n", encoding="utf-8")
        result = self._reduce()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         ["all_ai_commits-unmatched-2026-11-02.csv.7z",
                          "all_ai_commits-unmatched.csv"])
        self.assertIn(f"Removed {older}", result.stderr)
        self.assertIn("and the removal", result.stderr)

    def test_the_same_commit_history_gives_the_same_bytes(self):
        """The archive is written without timestamps. A second run over one handover writes
        the same bytes, and git reports no change."""
        self.assertEqual(self._reduce().returncode, 0)
        first = _mod.tracked_archive(self.output).read_bytes()
        time.sleep(1.1)
        self.assertEqual(self._reduce().returncode, 0)
        self.assertEqual(_mod.tracked_archive(self.output).read_bytes(), first)

    def test_a_missing_input_is_one_and_nothing_is_removed(self):
        older = self.output / "all_ai_commits-unmatched-2026-09-24.csv.7z"
        older.write_bytes(b"an earlier collection")
        for missing in (self.unmatched, self.finished):
            with self.subTest(missing=missing.name):
                moved = missing.with_suffix(".away")
                missing.rename(moved)
                try:
                    result = self._reduce()
                finally:
                    moved.rename(missing)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(str(missing), result.stderr)
                self.assertEqual([p.name for p in self.output.iterdir()], [older.name])

    def test_a_copy_that_fails_leaves_the_tracked_archive_whole(self):
        # A copy onto the tracked name that stops partway, such as on a full disk, would
        # leave a truncated tracked file and end in a traceback.
        self.assertEqual(self._reduce().returncode, 0)
        target = _mod.tracked_archive(self.output)
        whole = target.read_bytes()
        older = self.output / "all_ai_commits-unmatched-2026-09-24.csv.7z"
        older.write_bytes(b"an earlier collection")

        def interrupted(source, destination, *args, **kwargs):
            Path(destination).write_bytes(Path(source).read_bytes()[:10])
            raise OSError(28, "No space left on device")

        argv = ["--unmatched-csv", str(self.unmatched), "--finished", str(self.finished),
                "--output-dir", str(self.output)]
        with mock.patch.object(_mod.shutil, "copyfile", interrupted), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(_mod.main_reduce(argv), 1)
        self.assertEqual(target.read_bytes(), whole)
        self.assertEqual(sorted(p.name for p in self.output.iterdir()),
                         sorted([target.name, older.name]))
        self.assertIn("No space left on device", err.getvalue())
        self.assertIn(str(target), err.getvalue())

    def test_an_empty_list_of_finished_repositories_is_one(self):
        self.finished.write_text("", encoding="utf-8")
        result = self._reduce()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("lists no repository", result.stderr)
        self.assertEqual(list(self.output.iterdir()), [])


@unittest.skipIf(shutil.which(SEVEN_ZIP) is None, "7zz is not on PATH")
class TestTheTrackedArchive(unittest.TestCase):
    """How the tests find and read the tracked copy: exactly one archive, and its CSV first when
    the CSV is extracted."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output = self.tmp / "output"
        self.output.mkdir()

    def _archive(self, day: str, rows: list) -> Path:
        work = self.tmp / f"pack-{day}"
        work.mkdir()
        name = _mod.tracked_name(day)
        with (work / name).open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(_mod.UNMATCHED_COLUMNS_READ)
            writer.writerows(rows)
        archive = self.output / f"{name}.7z"
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), name], cwd=work,
                       check=True, capture_output=True)
        return archive

    def test_the_archive_is_read_when_the_csv_is_not_extracted(self):
        archive = self._archive("2026-09-24", [["trailer", "co-authored-by: x", 1, 1]])
        entries = _mod.read_tracked(archive)
        self.assertEqual(entries, [("trailer", "co-authored-by: x", 1, 1)])

    def test_the_extracted_csv_is_read_before_the_archive(self):
        archive = self._archive("2026-09-24", [["trailer", "co-authored-by: x", 1, 1]])
        archive.with_suffix("").write_text("kind,token,commits,repos\nauthor,y,2,2\n",
                                           encoding="utf-8")
        self.assertEqual(_mod.read_tracked(archive), [("author", "y", 2, 2)])

    def test_none_or_two_archives_are_refused(self):
        with self.assertRaises(ValueError):
            _mod.tracked_archive(self.output)
        self._archive("2026-09-24", [])
        self.assertEqual(_mod.tracked_archive(self.output).name,
                         "all_ai_commits-unmatched-2026-09-24.csv.7z")
        self._archive("2026-11-02", [])
        with self.assertRaises(ValueError):
            _mod.tracked_archive(self.output)

    def test_an_archive_without_the_csv_is_refused(self):
        archive = self._archive("2026-09-24", [])
        other = self.tmp / "other.csv"
        other.write_text("x\n", encoding="utf-8")
        archive.unlink()
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), str(other)],
                       check=True, capture_output=True)
        with self.assertRaises(RuntimeError):
            _mod.read_tracked(archive)


if __name__ == "__main__":
    unittest.main()
