"""
Tests for agentcontext.steps.detection: every collected file classified into a mechanism CSV.

Strategy:
  - The detectors take a repository path. The tests call them on small trees under a
    temporary directory, without git and without network access.
  - The step takes no options, and its paths are module-level constants. The tests that need
    them reassign VALIDATED_DIR, VALIDATED_CSV and OUTPUT_DIR on the imported module and
    restore them afterwards. The collected file cleanup writes the validated list at the root
    of the tree that it lists, and the fixture nests `repos.csv` inside `data/validated/`
    rather than beside it. The step reads `file_history.csv` and `ai_authorship.csv` by
    relative path, because the runner starts it from the collection stage's directory. The
    step cases run with the working directory inside their own temporary tree and write the
    two files there.
  - This suite installs nothing, and `lingua` is a large package. The language cases put a
    recording stub in place of the detector and check the logic around it: the sample cap,
    the minimum and the `n/a` results.

The suite imports the step's module by name with `importlib`.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_steps_detection -v
"""

import contextlib
import csv
import hashlib
import json
import importlib
import os
import io
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from agentcontext import heuristics
from agentcontext import caches
from agentcontext import paths
from agentcontext import STAGE_DIR
from tests._fixtures import (DEEP_JSON, DEEP_TOML, LONG_INTEGER_JSON, LONG_INTEGER_TOML,
                             deeply_nested_files, files_with_long_integers)

_mod = importlib.import_module("agentcontext.steps.detection")
_collect_all_spec_files = _mod._collect_all_spec_files
_collect_known_files = _mod._collect_known_files
_has_hooks = _mod._has_hooks
detect_commands = _mod.detect_commands
detect_context_files = _mod.detect_context_files
detect_hooks = _mod.detect_hooks
detect_lsp = _mod.detect_lsp
detect_mcp = _mod.detect_mcp
detect_other = _mod.detect_other
detect_plugins = _mod.detect_plugins
detect_rules = _mod.detect_rules
detect_settings = _mod.detect_settings
detect_skills = _mod.detect_skills
detect_subagents = _mod.detect_subagents
github_link = _mod.github_link
main = _mod.main
read_validated_csv = _mod.read_validated_csv
classify = _mod.classify
detect_language = _mod.detect_language
extract_references = _mod.extract_references
meaningful_lines = _mod.meaningful_lines
split_lines = _mod.split_lines

META_COLUMNS = list(heuristics.META_COLUMNS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _in_dir(path: Path):
    """Run the body with `path` as the working directory, as the runner runs the step."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _case_sensitive_filesystem() -> bool:
    """Whether two names that differ only in case can coexist where the tests build trees."""
    probe = Path(tempfile.mkdtemp())
    try:
        (probe / "aFile").write_text("x", encoding="utf-8")
        return not (probe / "afile").exists()
    finally:
        shutil.rmtree(probe, ignore_errors=True)


_CASE_SENSITIVE = _case_sensitive_filesystem()


def _spelled_on_disk(repo: Path, rel: str) -> bool:
    """Whether `rel` names a file below `repo` with every segment spelled as the directory
    listing spells it. `Path.is_file` returns True for any spelling on a case-insensitive
    volume."""
    current = repo
    for segment in rel.split("/"):
        if not current.is_dir() or segment not in os.listdir(current):
            return False
        current = current / segment
    return current.is_file()


# A repository that contains no file, for the tests of the attribution by path alone. No front
# matter test passes in it.
_NO_FILES = Path(tempfile.gettempdir()) / "agentcontext-tests-no-such-repository"


def derive_flags(cf, s, sk, sa, cmd, hk, rl, mc, ot, markers=(), plugins=(), workflows=(),
                 plugin_parts=(), lsp=(), declared=(), repo=_NO_FILES):
    """Return (file flags, tool flags) as main writes them for these detector results. The
    file flags come from the context files, and the tool flags are the union of
    `artifact_tools` over the paths of the ten mechanisms. An unclassified file keeps its tool
    and sets no flag. `markers` holds the [(marker, directory)] pairs of
    `marker_directories`."""
    rows = [*(("settings", x) for x in s), *(("context_file", x) for x in cf),
            *(("skills", x["skills.md"]) for x in sk), *(("subagents", x["subagent"]) for x in sa),
            *(("commands", x["command"]) for x in cmd), *(("hooks", x) for x in hk),
            *(("rules", x["rule"]) for x in rl), *(("mcp", x) for x in mc),
            *(("plugins", x) for x in plugins), *(("lsp", x) for x in lsp),
            *(("other", x) for x in ot)]
    tools = _mod.artifact_tools(repo, rows, workflows, plugin_parts, declared, markers)
    return _mod.file_flags_of(cf), _mod.tool_flags_of(
        found for (mechanism, _path), found in tools.items() if mechanism != "other")


def _other_as_main_does(repo: Path) -> list:
    """Run every detector with the arguments that main passes, and return the unclassified
    files.

    The helper passes the agent workflows, the resolved markers and the plugin manifests too.
    Without them, it would report a manifest as other while main writes the manifest to
    plugins.csv, and a detect_other that ignored the manifests would pass every case.
    """
    s = detect_settings(repo)
    for path, _tool in _mod.detect_agent_workflows(repo):
        if path not in s:
            s.append(path)
    return detect_other(repo, s, detect_context_files(repo), detect_skills(repo),
                        detect_subagents(repo), detect_commands(repo), detect_hooks(repo),
                        detect_rules(repo), detect_mcp(repo), _mod.resolved_markers(repo),
                        detect_plugins(repo), detect_lsp(repo))



# A Claude Code subagent as Claude Code loads it: a front matter with a name and a description.
AGENT = "---\nname: reviewer\ndescription: Reviews a change\n---\nReview the diff.\n"

class _RepoCase(unittest.TestCase):
    """A test case for the detectors, with one repository tree per test."""

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


class _StepCase(unittest.TestCase):
    """A test case for the step, with its module-level paths pointed at a temporary
    directory."""

    # The March sample records a bare date here, the first day of the collection window. It is
    # the one metadata value that the step must not convert.
    SCANNED_AT = "2026-03-29"
    HEAD_COMMIT_DATE = "2026-01-01T00:00:00Z"
    HEAD_SHA = "a" * 40

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # As in a real run, the validated list is at the root of the tree that it lists, beside
        # the repository directories, and every step case here reads a tree that contains it.
        # A fixture with the list outside the tree would never exercise VALIDATED_TREE_FILES.
        # A defect there would make the warning about stray entries name the step's own input
        # in every real run.
        self.validated = self.tmp / "data" / "validated"
        self.validated.mkdir(parents=True)
        self.validated_csv = self.validated / "repos.csv"
        self.output = self.tmp / "output"
        # The two caches that the step joins. The step reads both by relative path, and the
        # tests write them to the stage directory rather than to the validated tree.
        # _run() sets the working directory.
        self.file_history_csv = self.tmp / "file_history.csv"
        self.ai_authorship_csv = self.tmp / "ai_authorship.csv"
        self._rows = []
        self._git_rows = []
        self._ai_rows = []
        self.stdout = ""
        self.stderr = ""
        # The tests redirect COMMITS_DIR with the other three paths. Otherwise it would be the
        # checkout's gitignored `collect_commits/`, which every real collection fills, and the
        # cases here would pass or fail depending on whether this machine had run one.
        self._orig = (_mod.VALIDATED_DIR, _mod.VALIDATED_CSV, _mod.OUTPUT_DIR,
                      _mod.COMMITS_DIR)
        _mod.VALIDATED_DIR = self.validated
        _mod.VALIDATED_CSV = self.validated_csv
        _mod.OUTPUT_DIR = self.output
        _mod.COMMITS_DIR = self.tmp / "collect_commits"

    def tearDown(self):
        (_mod.VALIDATED_DIR, _mod.VALIDATED_CSV, _mod.OUTPUT_DIR,
         _mod.COMMITS_DIR) = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_input(self, rows: list, fieldnames: list = None) -> None:
        if fieldnames is None:
            fieldnames = list(META_COLUMNS)
        with self.validated_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for row in rows:
                w.writerow(row)

    def _repo(self, repo_name: str, files: dict = None, in_csv: bool = True,
              scanned_at: str = None, branch: str = "main",
              head_sha: str = None, head_commit_date: str = None) -> Path:
        """Add a repository directory and, unless `in_csv` is False, its row of the validated
        list."""
        repo = self.validated / repo_name.replace("/", "§")
        repo.mkdir(parents=True, exist_ok=True)
        for rel, content in (files or {}).items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if in_csv:
            self._rows.append({
                "repo_name": repo_name,
                "scanned_at": self.SCANNED_AT if scanned_at is None else scanned_at,
                "branch": branch,
                "head_sha": self.HEAD_SHA if head_sha is None else head_sha,
                "head_commit_date": (self.HEAD_COMMIT_DATE if head_commit_date is None
                                     else head_commit_date),
            })
            self._write_input(self._rows)
        return repo

    def _git_fact(self, repo_name: str, artifact: str, mechanism: str = "", *,
                  head_sha: str = None, created_at: str = "2026-02-01T00:00:00Z",
                  commits: str = "7", first_commit_sha: str = "b" * 40,
                  last_commit_sha: str = "c" * 40) -> None:
        """Add one row to `file_history.csv`, the cache that the step joins.

        `mechanism` is blank by default, as the cloning step writes it. The cloning step reads
        a file's history before any step decides which CSV the file belongs to.
        """
        self._git_rows.append({
            "repo_name": repo_name,
            "head_sha": self.HEAD_SHA if head_sha is None else head_sha,
            "mechanism": mechanism,
            "artifact": artifact,
            "created_at": created_at,
            "#commits": commits,
            "first_commit_sha": first_commit_sha,
            "last_commit_sha": last_commit_sha,
        })
        with self.file_history_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(caches.FILE_HISTORY_COLUMNS))
            w.writeheader()
            w.writerows(self._git_rows)

    def _ai_fact(self, repo_name: str, artifact: str, *, created: str = "TRUE",
                 tool: str = "Claude", ai_commits: str = "3",
                 tools: str = "Claude", head_sha: str = None) -> None:
        """Add one row to `ai_authorship.csv`, the cache that the step joins."""
        self._ai_rows.append({
            "repo_name": repo_name,
            "head_sha": self.HEAD_SHA if head_sha is None else head_sha,
            "artifact": artifact,
            "first_commit_ai_created": created, "first_commit_ai_tool": tool,
            "#ai_commits": ai_commits, "ai_commit_tools": tools,
        })
        columns = ["repo_name", "head_sha", "artifact", *heuristics.AI_COLUMNS]
        with self.ai_authorship_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=columns)
            w.writeheader()
            w.writerows(self._ai_rows)

    def _run(self) -> int:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                _in_dir(self.tmp):
            rc = main([])
        # The step writes its counts and its warnings as log messages, which the tests read as
        # one log.
        self.stdout = self.stderr = out.getvalue() + err.getvalue()
        return rc

    def _out_rows(self, name: str) -> list:
        with (self.output / name).open(newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def _out_header(self, name: str) -> list:
        with (self.output / name).open(newline="", encoding="utf-8") as f:
            return next(csv.reader(f))


# ---------------------------------------------------------------------------
# github_link
# ---------------------------------------------------------------------------

class TestGithubLink(unittest.TestCase):

    def test_repo_link_without_path(self):
        self.assertEqual(github_link("owner/repo", "main"),
                         "https://github.com/owner/repo/tree/main")

    def test_artifact_link_with_path(self):
        self.assertEqual(github_link("owner/repo", "main", "CLAUDE.md"),
                         "https://github.com/owner/repo/tree/main/CLAUDE.md")

    def test_nested_path_is_appended_as_given(self):
        self.assertEqual(github_link("owner/repo", "main", ".claude/skills/pdf/SKILL.md"),
                         "https://github.com/owner/repo/tree/main/.claude/skills/pdf/SKILL.md")

    def test_non_default_branch(self):
        self.assertEqual(github_link("owner/repo", "develop", "CLAUDE.md"),
                         "https://github.com/owner/repo/tree/develop/CLAUDE.md")

    def test_blank_branch_returns_empty(self):
        self.assertEqual(github_link("owner/repo", ""), "")

    def test_blank_branch_with_path_returns_empty(self):
        self.assertEqual(github_link("owner/repo", "", "CLAUDE.md"), "")

    def test_a_path_or_branch_that_is_not_a_url_is_percent_encoded(self):
        # The link encodes a space or a character outside ASCII in the path, including a name
        # that starts with a space, because the column would otherwise contain no valid URL.
        self.assertEqual(github_link("o/r", "main", ".github/chatmodes/ 🎭 planner.chatmode.md"),
                         "https://github.com/o/r/tree/main/.github/chatmodes/"
                         "%20%F0%9F%8E%AD%20planner.chatmode.md")
        self.assertEqual(github_link("o/r", "fix#1", "a?b%c.md"),
                         "https://github.com/o/r/tree/fix%231/a%3Fb%25c.md")
        self.assertEqual(github_link("o/r", "feature/x"),
                         "https://github.com/o/r/tree/feature/x")

    def test_the_characters_a_path_segment_allows_are_kept(self):
        """`@`, `(`, `)`, `*` and `+` are valid in a link's path, and encoding them would
        change links that work, such as every `packages/@scope/...` path."""
        self.assertEqual(github_link("o/r", "main", "packages/@expo/(tools)/c++/*.md"),
                         "https://github.com/o/r/tree/main/packages/@expo/(tools)/c++/*.md")


# ---------------------------------------------------------------------------
# read_validated_csv
# ---------------------------------------------------------------------------

class TestReadValidatedCsv(_StepCase):

    def test_rows_are_keyed_by_repo_name(self):
        self._write_input([
            {"repo_name": "o/a", "scanned_at": "2026-03-29", "branch": "main",
             "head_sha": "a" * 40, "head_commit_date": "2026-01-01T00:00:00Z"},
            {"repo_name": "o/b", "scanned_at": "2026-03-29", "branch": "dev",
             "head_sha": "b" * 40, "head_commit_date": "2026-01-02T00:00:00Z"},
        ])
        rows, _ = read_validated_csv()
        self.assertEqual(set(rows), {"o/a", "o/b"})
        self.assertEqual(rows["o/b"]["branch"], "dev")

    def test_fieldnames_are_returned_in_order(self):
        self._write_input([])
        _, fieldnames = read_validated_csv()
        self.assertEqual(fieldnames, ["repo_name", "scanned_at", "branch",
                                      "head_sha", "head_commit_date"])

    def test_extra_columns_are_preserved(self):
        self._write_input(
            [{"repo_name": "o/a", "scanned_at": "2026-03-29", "branch": "main",
              "head_sha": "a" * 40, "head_commit_date": "t", "note": "kept"}],
            fieldnames=META_COLUMNS + ["note"])
        rows, fieldnames = read_validated_csv()
        self.assertIn("note", fieldnames)
        self.assertEqual(rows["o/a"]["note"], "kept")

    def test_header_only_csv_returns_no_rows(self):
        self._write_input([])
        rows, _ = read_validated_csv()
        self.assertEqual(rows, {})

    def test_duplicate_repo_name_keeps_the_last_row(self):
        self._write_input([
            {"repo_name": "o/a", "scanned_at": "2026-03-29", "branch": "main",
             "head_sha": "a" * 40, "head_commit_date": "first"},
            {"repo_name": "o/a", "scanned_at": "2026-03-29", "branch": "main",
             "head_sha": "a" * 40, "head_commit_date": "second"},
        ])
        rows, _ = read_validated_csv()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows["o/a"]["head_commit_date"], "second")


# ---------------------------------------------------------------------------
# detect_settings  (the settings paths of the heuristics, a `file` row at every depth)
# ---------------------------------------------------------------------------

# A dispatcher of the Gemini CLI action and the reusable workflow that it calls, which runs the
# action and declares `workflow_call`, as the action's example workflows do.
GEMINI_DISPATCHER = ("on: issue_comment\njobs:\n  review:\n"
                     "    uses: ./.github/workflows/gemini-review.yml\n")
GEMINI_REVIEW = ("on:\n  workflow_call:\njobs:\n  review:\n    runs-on: ubuntu-latest\n"
                 "    steps:\n      - uses: google-github-actions/run-gemini-cli@v0\n")


class TestDetectSettings(_RepoCase):

    # Every settings file that the heuristics name, with its tool, in the order of their rows.
    # The list is written out rather than read from SETTINGS_PATHS, because a row that moves to
    # another mechanism or is dropped must fail its case rather than take the case with it.
    FILES = (
        (".claude/settings.json", "claude"),
        (".claude/settings.local.json", "claude"),
        (".gemini/settings.json", "gemini"),
        (".gemini/config.yaml", "gemini"),
        (".codex/config.toml", "codex"),
        (".cursor/cli.json", "cursor"),
        (".cursor/environment.json", "cursor"),
        (".cursor/Dockerfile", "cursor"),
        (".github/copilot/settings.json", "copilot"),
        (".github/copilot/settings.local.json", "copilot"),
        (".github/allowed_models.txt", "copilot"),
        (".github/workflows/copilot-setup-steps.yml", "copilot"),
        (".github/workflows/copilot-code-review.yml", "copilot"),
        (".github/workflows/claude.yml", "claude"),
        (".github/workflows/claude-code-review.yml", "claude"),
        (".github/workflows/gemini-dispatch.yml", "gemini"),
        (".claude/launch.json", "claude"),
        (".cursor/sandbox.json", "cursor"),
        (".cursor/permissions.json", "cursor"),
        (".cursor/worktrees.json", "cursor"),
    )

    def test_cursor_worktree_setup_is_a_named_configuration(self):
        path = self.repo / ".cursor" / "worktrees.json"
        path.parent.mkdir()
        path.write_text('{"setup-worktree": ["npm install"]}\n')
        self.assertEqual(detect_settings(self.repo), [".cursor/worktrees.json"])
        self.assertTrue(_mod.has_detected_mechanism(self.repo))
        _files, tools = derive_flags([], [".cursor/worktrees.json"], [], [], [], [], [], [], [])
        self.assertEqual({tool for tool, present in tools.items() if present}, {"cursor"})

    def test_repo_without_settings(self):
        self.assertEqual(detect_settings(self.repo), [])

    def test_the_gemini_dispatcher_counts_where_it_calls_a_reusable_gemini_workflow(self):
        """The Gemini CLI action's README describes `gemini-dispatch.yml` as "a central
        dispatcher for Gemini CLI, routing requests to the appropriate workflow", and its example
        workflows declare `workflow_call`. pantavisor/pantavisor's dispatcher calls a
        `gemini-review.yml` that declares only `workflow_dispatch`, so no Gemini runs through
        it, and the file is an `other` row (pp-0688 of the evaluation of 2026-11-04).
        NationalSecurityAgency/datawave's calls a `gemini-review.yml` that runs `gemini --prompt`
        and declares `workflow_call`."""
        dispatcher = ".github/workflows/gemini-dispatch.yml"
        review = ".github/workflows/gemini-review.yml"
        command = ("on: [workflow_call]\njobs:\n  review:\n    runs-on: ubuntu-latest\n"
                   "    steps:\n      - run: gemini --prompt \"Review\"\n")
        claude = GEMINI_REVIEW.replace("google-github-actions/run-gemini-cli@v0",
                                       "anthropics/claude-code-action@v1")
        cases = (("a reusable Gemini workflow", GEMINI_DISPATCHER, GEMINI_REVIEW, True),
                 ("a reusable workflow that runs gemini --prompt", GEMINI_DISPATCHER, command,
                  True),
                 ("the same repository with $/",
                  GEMINI_DISPATCHER.replace("./.github", "$/.github"), GEMINI_REVIEW, True),
                 ("on: workflow_call as a name", GEMINI_DISPATCHER,
                  GEMINI_REVIEW.replace("on:\n  workflow_call:\n", "on: workflow_call\n"), True),
                 ("a Gemini workflow without workflow_call", GEMINI_DISPATCHER,
                  GEMINI_REVIEW.replace("workflow_call", "workflow_dispatch"), False),
                 ("a reusable workflow that runs Claude Code", GEMINI_DISPATCHER, claude, False),
                 ("a workflow of another repository", GEMINI_DISPATCHER.replace(
                     "./.github/workflows/gemini-review.yml",
                     "owner/tools/.github/workflows/gemini-review.yml@v1"),
                  GEMINI_REVIEW, False),
                 ("no job that calls a workflow", "on: issue_comment\njobs:\n  debug:\n"
                  "    runs-on: ubuntu-latest\n    steps:\n      - run: env\n", GEMINI_REVIEW,
                  False),
                 ("no workflow at the path that it calls", GEMINI_DISPATCHER, None, False))
        for i, (name, dispatching, called, counts) in enumerate(cases):
            with self.subTest(case=name):
                repo = self.tmp / f"d{i}"
                (repo / dispatcher).parent.mkdir(parents=True)
                (repo / dispatcher).write_text(dispatching, encoding="utf-8")
                if called is not None:
                    (repo / review).write_text(called, encoding="utf-8")
                self.assertEqual(dispatcher in detect_settings(repo), counts)
                self.assertEqual(dispatcher in _other_as_main_does(repo), not counts)

    def test_a_workflow_of_comments_alone_is_no_settings(self):
        """A YAML file of comment lines and blank lines is an empty document, with no `on:` and
        no `jobs:`, and GitHub runs no workflow from it. Every line of auth0/laravel-auth0's
        claude-code-review.yml is a comment, and so is every line of utopia-php/database's
        claude.yml. A workflow with a line of YAML is still read by its name."""
        commented = ("# name: Claude Code PR Review\n\n# on:\n#   issue_comment:\n"
                     "#     types: [ created ]\n\n# jobs:\n#   claude-review:\n"
                     "#     uses: auth0/auth0-ai-pr-analyzer-gh-action/.github/workflows/"
                     "claude-code-review.yml@main\n")
        names = (".github/workflows/copilot-setup-steps.yml",
                 ".github/workflows/copilot-code-review.yml", ".github/workflows/claude.yml",
                 ".github/workflows/claude-code-review.yml",
                 ".github/workflows/gemini-dispatch.yml", ".github/workflows/opencode.yml")
        for i, name in enumerate(names):
            with self.subTest(name):
                repo = self.tmp / f"c{i}"
                (repo / name).parent.mkdir(parents=True)
                (repo / name).write_text(commented, encoding="utf-8")
                self.assertEqual(detect_settings(repo), [])
                self.assertFalse(_mod.has_detected_mechanism(repo))
                self.assertEqual(_other_as_main_does(repo), [name])
                if name == ".github/workflows/gemini-dispatch.yml":
                    (repo / name).write_text("---\n" + commented + GEMINI_DISPATCHER,
                                             encoding="utf-8")
                    (repo / ".github/workflows/gemini-review.yml").write_text(
                        GEMINI_REVIEW, encoding="utf-8")
                else:
                    (repo / name).write_text("---\n" + commented + "on: issue_comment\n",
                                             encoding="utf-8")
                self.assertEqual(detect_settings(repo), [name])

    def test_each_settings_path_is_detected_and_attributed(self):
        # Each file is in a repository of its own, and the result must be that file and that
        # tool alone. The last two cases are the tree row and the basename row, which name no
        # fixed file.
        cases = self.FILES + ((".codex/environments/dev.toml", "codex"),
                              ("tools/ext/gemini-extension.json", "gemini"))
        for i, (rel, tool) in enumerate(cases):
            with self.subTest(rel):
                repo = self.tmp / f"r{i}"
                (repo / rel).parent.mkdir(parents=True)
                (repo / rel).write_text('{"name": "x"}', encoding="utf-8")
                if rel == ".github/workflows/gemini-dispatch.yml":
                    (repo / rel).write_text(GEMINI_DISPATCHER, encoding="utf-8")
                    (repo / ".github/workflows/gemini-review.yml").write_text(
                        GEMINI_REVIEW, encoding="utf-8")
                self.assertEqual(detect_settings(repo), [rel])
                _files, tools = derive_flags([], [rel], [], [], [], [], [], [], [])
                self.assertEqual({t for t, on in tools.items() if on}, {tool})

    def test_a_plugin_settings_file_is_a_setting(self):
        self._file(".claude/plugins/review/.claude-plugin/plugin.json", '{"name": "review"}')
        self._file(".claude/plugins/review/settings.json", '{"agent": "reviewer"}')
        self.assertEqual(detect_settings(self.repo), [".claude/plugins/review/settings.json"])

    def test_a_settings_file_beside_no_manifest_is_not_a_plugin_s(self):
        # `.claude/plugins/` is no location that Claude Code documents. A plugin is the
        # directory that contains its manifest, wherever that is.
        self._file(".claude/plugins/review/settings.json", '{"agent": "reviewer"}')
        self.assertEqual(detect_settings(self.repo), [])

    def test_gemini_code_assist_reads_its_files_at_the_root_only(self):
        """Google's page: "adding a config.yaml file to a .gemini/ folder located in the root of
        each of your repositories". A detector that read the folder at any depth would give
        googleapis/google-cloud-java its gemini flag from java-storage/.gemini/config.yaml
        alone."""
        self._file("java-storage/.gemini/config.yaml", "have_fun: false\n")
        self.assertNotIn("java-storage/.gemini/config.yaml", detect_settings(self.repo))
        self._file(".gemini/config.yaml", "have_fun: false\n")
        self.assertIn(".gemini/config.yaml", detect_settings(self.repo))
        self._file("java-storage/.gemini/styleguide.md", "# Style\n")
        self.assertNotIn("java-storage/.gemini/styleguide.md", detect_context_files(self.repo))
        self._file(".gemini/styleguide.md", "# Style\n")
        self.assertIn(".gemini/styleguide.md", detect_context_files(self.repo))

    def test_result_follows_the_declared_order(self):
        # The files are written back to front. The output follows the order of the rows in the
        # heuristics, not the order in which the files were created. Reordering the rows
        # reorders settings.csv, and this case then fails.
        for rel, _tool in reversed(self.FILES):
            self._file(rel, GEMINI_DISPATCHER if rel.endswith("/gemini-dispatch.yml") else "{}")
        self._file(".github/workflows/gemini-review.yml", GEMINI_REVIEW)
        self.assertEqual(detect_settings(self.repo), [rel for rel, _tool in self.FILES])

    def test_directory_at_a_settings_path_is_not_a_setting(self):
        self._dir(".claude/settings.json")
        self.assertEqual(detect_settings(self.repo), [])

    def test_unlisted_file_in_a_spec_dir_is_not_a_setting(self):
        self._file(".claude/config.json", "{}")
        self.assertEqual(detect_settings(self.repo), [])

    def test_settings_are_found_at_depth(self):
        # A monorepo configures its packages one by one, and the tools read the
        # directory that they are working in.
        self._file("packages/web/.claude/settings.json", "{}")
        self.assertEqual(detect_settings(self.repo), ["packages/web/.claude/settings.json"])

    def test_a_settings_file_in_a_vendored_tree_is_not_a_setting(self):
        self._file("node_modules/pkg/.claude/settings.json", "{}")
        self.assertEqual(detect_settings(self.repo), [])

    def test_a_nested_github_settings_file_is_not_a_setting(self):
        # GitHub reads a repository's .github at the root and nowhere else.
        self._file("packages/web/.github/copilot/settings.json", "{}")
        self.assertEqual(detect_settings(self.repo), [])


# ---------------------------------------------------------------------------
# detect_context_files  (by basename, by path, by instruction directory and by declaration)
# ---------------------------------------------------------------------------

class TestDetectContextFiles(_RepoCase):

    def test_repo_without_context_files(self):
        self._file("README.md")
        self.assertEqual(detect_context_files(self.repo), [])

    def test_each_context_file_is_detected_and_attributed(self):
        # Each file is written out with its tool, alone in a repository of its own, rather than
        # read from CONTEXT_FILENAMES. That set, which the detector reads, names none of the
        # `file` rows, such as .cursor/BUGBOT.md and .gemini/styleguide.md.
        expected = (
            ("CLAUDE.md", {"claude"}),
            ("GEMINI.md", {"gemini"}),
            ("AGENTS.md", set()),
            ("AGENTS.override.md", {"codex"}),
            ("CLAUDE.local.md", {"claude"}),
            # The three in another case, at the root
            ("claude.md", {"claude"}),
            ("Gemini.md", {"gemini"}),
            ("agents.md", set()),
            (".cursorrules", {"cursor"}),
            (".cursor/BUGBOT.md", {"cursor"}),
            (".gemini/styleguide.md", {"gemini"}),
            (".github/copilot-instructions.md", {"copilot"}),
            (".github/instructions/python.instructions.md", {"copilot"}),
        )
        for i, (rel, tools) in enumerate(expected):
            with self.subTest(rel):
                repo = self.tmp / f"r{i}"
                (repo / rel).parent.mkdir(parents=True)
                (repo / rel).write_text("# guidance\n", encoding="utf-8")
                self.assertEqual(detect_context_files(repo), [rel])
                self.assertEqual(_other_as_main_does(repo), [])
                _files, flags = derive_flags([rel], [], [], [], [], [], [], [], [])
                self.assertEqual({t for t, on in flags.items() if on}, tools)

    def test_context_file_at_any_depth(self):
        self._file("docs/nested/CLAUDE.md")
        self.assertEqual(detect_context_files(self.repo), ["docs/nested/CLAUDE.md"])

    def test_context_file_inside_a_spec_dir(self):
        self._file(".claude/CLAUDE.md")
        self.assertEqual(detect_context_files(self.repo), [".claude/CLAUDE.md"])

    def test_wrong_case_below_the_root_is_not_a_context_file(self):
        self._file("docs/claude.md")
        self._file("pkg/Agents.md")
        self.assertEqual(detect_context_files(self.repo), [])

    def test_wrong_case_at_the_root_sets_the_file_flag(self):
        files, _tools = derive_flags(["claude.md", "Agents.md", "GEMINI.MD"],
                                     [], [], [], [], [], [], [], [])
        self.assertTrue(files["claude.md"] and files["agents.md"] and files["gemini.md"])
        files, _tools = derive_flags(["docs/claude.md"], [], [], [], [], [], [], [], [])
        self.assertFalse(files["claude.md"])

    def test_wrong_case_at_the_root_is_a_context_file_under_its_name_on_disk(self):
        self._file("claude.md")
        self._file("Agents.md")
        self._file("plans.md")
        self.assertEqual(detect_context_files(self.repo), ["Agents.md", "claude.md"])

    def test_copilot_instructions_is_detected(self):
        self._file(".github/copilot-instructions.md")
        self.assertEqual(detect_context_files(self.repo),
                         [".github/copilot-instructions.md"])

    def test_copilot_instructions_only_at_the_github_root(self):
        self._file(".github/sub/copilot-instructions.md")
        self.assertEqual(detect_context_files(self.repo), [])

    def test_github_instructions_markdown_is_detected(self):
        self._file(".github/instructions/python.instructions.md")
        self.assertEqual(detect_context_files(self.repo),
                         [".github/instructions/python.instructions.md"])

    def test_github_instructions_recurses(self):
        self._file(".github/instructions/lang/python.instructions.md")
        self.assertEqual(detect_context_files(self.repo),
                         [".github/instructions/lang/python.instructions.md"])

    def test_a_case_variant_directory_is_reported_under_its_own_name(self):
        # A case-insensitive volume opens `.github/instructions` for `.github/Instructions`.
        # Without _real_dir_name, the detector would report the file under the heuristics'
        # spelling, a path in no tree, and again as other under its own name. A case-sensitive
        # volume would find nothing.
        self._file(".github/Instructions/python.instructions.md")
        self.assertEqual(detect_context_files(self.repo),
                         [".github/Instructions/python.instructions.md"])
        self.assertEqual(_other_as_main_does(self.repo), [])

    def test_github_instructions_requires_the_double_extension(self):
        # "Instructions files are Markdown files with the .instructions.md
        # extension". A plain .md file there is not one.
        self._file(".github/instructions/python.md")
        self.assertEqual(detect_context_files(self.repo), [])

    def test_github_instructions_ignores_non_markdown(self):
        self._file(".github/instructions/notes.txt")
        self.assertEqual(detect_context_files(self.repo), [])

    def test_other_github_markdown_is_not_a_context_file(self):
        self._file(".github/PULL_REQUEST_TEMPLATE.md")
        self.assertEqual(detect_context_files(self.repo), [])

    def test_context_file_under_github_instructions_is_recorded_once(self):
        # The repository-wide search claims CLAUDE.md. Because CLAUDE.md is not an
        # instructions file, the search of .github/instructions does not claim it too, and
        # the row appears once.
        self._file(".github/instructions/CLAUDE.md")
        found = detect_context_files(self.repo)
        self.assertEqual(found.count(".github/instructions/CLAUDE.md"), 1)

    def test_a_file_the_configuration_names_is_a_context_file_once(self):
        """A file that a Codex configuration names is a context file whatever its name. A named
        file that a path pattern already found is listed once. `.codex/CODEX.md` is therefore
        not reported under other, unlike a file in `.codex` that no detector claims."""
        self._file(".codex/config.toml", 'model_instructions_file = "CODEX.md"\n'
                                         'project_doc_fallback_filenames = ["AGENTS.md", '
                                         '"TEAM.md"]\n')
        self._file(".codex/CODEX.md")
        self._file("AGENTS.md")
        self._file("web/TEAM.md")
        found = detect_context_files(self.repo)
        self.assertEqual(sorted(found), [".codex/CODEX.md", "AGENTS.md", "web/TEAM.md"])
        self.assertNotIn(".codex/CODEX.md", _other_as_main_does(self.repo))

    def test_multiple_context_files_all_returned(self):
        self._file("CLAUDE.md")
        self._file("docs/AGENTS.md")
        self._file(".cursorrules")
        self.assertEqual(set(detect_context_files(self.repo)),
                         {"CLAUDE.md", "docs/AGENTS.md", ".cursorrules"})


# ---------------------------------------------------------------------------
# detect_skills  (one level under each skills directory, only with a SKILL.md)
# ---------------------------------------------------------------------------

class TestClaudeSkillDepth(_RepoCase):
    def test_a_gemini_cli_skill_needs_front_matter_with_a_name_and_a_description(self):
        # Gemini CLI's loadSkillFromFile drops a SKILL.md without a name and a description in its
        # front matter, such as GoogleCloudPlatform/k8s-config-connector's
        # mockgcp/.gemini/skills/fix-diffs-mockgcp/SKILL.md.
        # A skills folder that Claude Code also reads keeps such a skill.
        self._file("mockgcp/.gemini/skills/fix-diffs/SKILL.md", "# Skill: Fix MockGCP Diffs\n")
        self._file(".gemini/skills/review/SKILL.md", "---\nname: review\ndescription: Reviews.\n---\nReview.\n")
        self._file("ext/gemini-extension.json", '{"name": "ext", "version": "1.0.0"}')
        self._file("ext/.claude-plugin/plugin.json", '{"name": "ext"}')
        self._file("ext/skills/plain/SKILL.md", "# Plain\n")
        self.assertEqual(sorted(r["skills.md"] for r in detect_skills(self.repo)),
                         [".gemini/skills/review/SKILL.md", "ext/skills/plain/SKILL.md"])

    def test_an_antigravity_skill_needs_front_matter(self):
        """Antigravity CLI 1.4.10's validator reports "no frontmatter: the file must start with a
        --- block holding name and description" for a SKILL.md that the loader skips, and only
        warns about a missing `name` or `description`. GoogleCloudPlatform/cloud-solutions
        registers projects/legacy-detox-demo/skills/zero-copy-ingestion, whose SKILL.md opens with
        a heading, in projects/legacy-detox-demo/.agents/skills.json (path_precision pp-0658 of
        the evaluation of 2026-10-26). `.agents/skills`, which other tools read too, and a folder
        that a Claude Code plugin shares keep such a skill."""
        heading = "# Skill: Zero-Copy Data Exploration & Ingestion\n"
        front = "---\nname: {}\n---\nBody.\n"
        self._file("demo/.agents/skills.json",
                   '{"entries": [{"path": "skills/zero-copy"}, {"path": "skills/named"}]}')
        self._file("demo/skills/zero-copy/SKILL.md", heading)
        self._file("demo/skills/named/SKILL.md", "\ufeff" + front.format("named"))
        self._file(".agent/skills/core/SKILL.md", heading)
        self._file("_agents/skills/lint/SKILL.md", front.format("lint"))
        self._file("_agent/skills/open/SKILL.md", "---\nname: open\n")
        self._file(".agents/skills/shared/SKILL.md", heading)
        self._file(".agents/plugins/p/plugin.json",
                   json.dumps({"name": "p", "skills": ["./extra", "."]}))
        self._file(".agents/plugins/p/SKILL.md", heading)
        self._file(".agents/plugins/p/skills/plain/SKILL.md", heading)
        self._file(".agents/plugins/p/extra/listed/SKILL.md", heading)
        self._file(".agents/plugins/q/plugin.json", '{"name": "q"}')
        self._file(".agents/plugins/q/.claude-plugin/plugin.json", '{"name": "q"}')
        self._file(".agents/plugins/q/skills/both/SKILL.md", heading)
        self.assertEqual(sorted(r["skills.md"] for r in detect_skills(self.repo)),
                         [".agents/plugins/q/skills/both/SKILL.md",
                          ".agents/skills/shared/SKILL.md",
                          "_agents/skills/lint/SKILL.md",
                          "demo/.agents/skills.json",
                          "demo/skills/named/SKILL.md"])

    def test_antigravity_reads_the_skills_folders_that_only_it_reads_one_level_deep(self):
        """The customization discovery page of Antigravity states that "Antigravity scans each
        customization folder one level deep. A skill folder must sit directly inside skills/"
        and "To load a nested item, move it up one level or name it in a discovery manifest"
        (https://antigravity.google/docs/customization-discovery, read 2026-11-05).
        joemccann/dillinger keeps `.agent/skills/game-development/2d-games/SKILL.md` in the
        folder of the skill `game-development`, and the nested file is a file of that skill.
        A discovery manifest and the `skills` of a plugin's manifest can name a nested skill.
        Codex reads `.agents/skills` recursively, and a nested skill there stays a skill."""
        front = "---\nname: {}\ndescription: {} principles.\n---\nBody.\n"
        for root in (".agent", "_agents", "_agent", ".agents/plugins/p", ".agents/plugins/q",
                     ".agents"):
            self._file(f"{root}/skills/game-development/SKILL.md",
                       front.format("game-development", "Game development"))
            self._file(f"{root}/skills/game-development/2d-games/SKILL.md",
                       front.format("2d-games", "2D game development"))
        self._file(".agents/plugins/p/plugin.json", '{"name": "p"}')
        self._file(".agents/plugins/q/plugin.json",
                   json.dumps({"name": "q", "skills": ["skills/game-development/2d-games"]}))
        self._file("_agent/skills.json", json.dumps({"entries": [
            {"path": "_agent/skills", "include_only": ["game-development/2d-games"]}]}))
        self.assertEqual(sorted(r["skills.md"] for r in detect_skills(self.repo)),
                         [".agent/skills/game-development/SKILL.md",
                          ".agents/plugins/p/skills/game-development/SKILL.md",
                          ".agents/plugins/q/skills/game-development/2d-games/SKILL.md",
                          ".agents/plugins/q/skills/game-development/SKILL.md",
                          ".agents/skills/game-development/2d-games/SKILL.md",
                          ".agents/skills/game-development/SKILL.md",
                          "_agent/skills.json",
                          "_agent/skills/game-development/2d-games/SKILL.md",
                          "_agent/skills/game-development/SKILL.md",
                          "_agents/skills/game-development/SKILL.md"])
        self.assertEqual(_other_as_main_does(self.repo), [])

    def test_project_skills_are_immediate_children(self):
        # Gemini CLI globs "['SKILL.md', '*/SKILL.md']" in a skills folder, so it does not read a
        # deeper SKILL.md such as GoogleCloudPlatform/k8s-config-connector's
        # .gemini/skills/kcc-bulk-controller-generator/generate/SKILL.md. Claude Code reads the
        # folders directly in .claude/skills too, and Cursor and OpenCode read the skills of its
        # category folders (TestTheCategoryFoldersOfClaudeSkills).
        for path in (".claude/skills/top/SKILL.md", ".claude/skills/category/deep/SKILL.md",
                     ".gemini/skills/top/SKILL.md", ".gemini/skills/category/deep/SKILL.md"):
            self._file(path, "---\nname: test\ndescription: Test\n---\nInstructions\n")
        self.assertEqual({r["skills.md"] for r in detect_skills(self.repo)},
                         {".claude/skills/top/SKILL.md", ".claude/skills/category/deep/SKILL.md",
                          ".gemini/skills/top/SKILL.md"})

    def test_plugin_defaults_are_shallow_but_explicit_skills_are_supported(self):
        self._file("plugins/p/.claude-plugin/plugin.json",
                   json.dumps({"name": "p", "skills": ["./skills/category/deep", "./extra"]}))
        for path in ("skills/top/SKILL.md", "skills/category/deep/SKILL.md",
                     "skills/category/hidden/SKILL.md", "extra/one/SKILL.md",
                     "extra/category/hidden/SKILL.md"):
            self._file("plugins/p/" + path, "---\nname: test\ndescription: Test\n---\nInstructions\n")
        self.assertEqual({r["skills.md"] for r in detect_skills(self.repo)},
                         {"plugins/p/skills/top/SKILL.md", "plugins/p/skills/category/deep/SKILL.md",
                          "plugins/p/extra/one/SKILL.md"})


class TestDetectSkills(_RepoCase):

    def test_repo_without_skills(self):
        self.assertEqual(detect_skills(self.repo), [])

    def test_a_skill_inside_a_codex_plugin_is_a_skill(self):
        """A Codex plugin keeps its skills in skills/ beside .codex-plugin/plugin.json, the
        folder that Codex's plugin loader reads by default."""
        self._file(".agents/plugins/demo/.codex-plugin/plugin.json", '{"name": "demo"}')
        self._file(".agents/plugins/demo/skills/pdf/SKILL.md",
                   "---\nname: pdf\ndescription: Reads PDFs\n---\n")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         [".agents/plugins/demo/skills/pdf/SKILL.md"])

    def test_skills_under_agents_plugins_beside_no_manifest_are_no_plugin_s(self):
        """opendatahub-io/notebooks keeps skills under `.agents/plugins/<name>/` with no
        marketplace and no Codex manifest. Codex does not load such skills."""
        self._file(".agents/plugins/demo/skills/pdf/SKILL.md", "---\nname: pdf\n---\n")
        self.assertEqual(detect_skills(self.repo), [])

    def test_the_skills_of_an_antigravity_plugin_are_skills(self):
        """With a plugin.json in the folder, the same layout is an Antigravity plugin, which
        loads the skills beside its manifest."""
        self._file(".agents/plugins/demo/plugin.json", '{"name": "demo"}')
        self._file(".agents/plugins/demo/skills/pdf/SKILL.md", "---\nname: pdf\n---\n")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         [".agents/plugins/demo/skills/pdf/SKILL.md"])

    def test_antigravity_reads_the_skills_that_a_manifest_lists_in_addition_to_those_in_skills(self):
        """Antigravity CLI 1.4.9 reads the skills that the `skills` key of a plugin manifest lists
        in addition to those in `skills/`, and a listed skill in the plugin root is the root skill
        of the plugin, named for its folder, with its support directories."""
        self._file(".agents/plugins/demo/plugin.json", '{"name": "demo", "skills": ["./extra"]}')
        self._file(".agents/plugins/demo/skills/pdf/SKILL.md", "---\nname: pdf\n---\n")
        self._file(".agents/plugins/demo/extra/csv/SKILL.md", "---\nname: csv\n---\n")
        self._file(".agents/plugins/tool/plugin.json", '{"name": "tool", "skills": "."}')
        self._file(".agents/plugins/tool/SKILL.md", "---\nname: tool\n---\n")
        self._file(".agents/plugins/tool/references/api.md", "x\n")
        self.assertEqual(sorted((r["skills.md"], r["name"], r["references"])
                                for r in detect_skills(self.repo)),
                         [(".agents/plugins/demo/extra/csv/SKILL.md", "csv", "FALSE"),
                          (".agents/plugins/demo/skills/pdf/SKILL.md", "pdf", "FALSE"),
                          (".agents/plugins/tool/SKILL.md", "tool", "TRUE")])

    def test_skill_row_fields(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self.assertEqual(detect_skills(self.repo), [{
            "skills.md": ".claude/skills/pdf/SKILL.md",
            "name": "pdf",
            "scripts": "FALSE",
            "references": "FALSE",
            "assets": "FALSE",
        }])

    def test_each_skills_dir_is_scanned(self):
        # Each directory is written out with its tool rather than read from SKILLS_DIRS,
        # because deleting a row such as .gemini/skills must fail its case. Several tools read
        # `.agents/skills`, and it credits none of them.
        expected = (
            (".claude/skills", {"claude"}),
            (".gemini/skills", {"gemini"}),
            # Codex's parser reads the file, and the attribution reads it in the repository.
            (".codex/skills", {"codex"}),
            (".github/skills", {"copilot"}),
            (".cursor/skills", {"cursor"}),
            (".agents/skills", set()),
        )
        for i, (skills_dir, tools) in enumerate(expected):
            with self.subTest(skills_dir):
                repo = self.tmp / f"r{i}"
                (repo / skills_dir / "pdf").mkdir(parents=True)
                (repo / skills_dir / "pdf" / "SKILL.md").write_text(
                    "---\nname: pdf\ndescription: Reads PDFs.\n---\nx\n", encoding="utf-8")
                rows = detect_skills(repo)
                self.assertEqual([r["skills.md"] for r in rows], [f"{skills_dir}/pdf/SKILL.md"])
                _files, flags = derive_flags([], [], rows, [], [], [], [], [], [], repo=repo)
                self.assertEqual({t for t, on in flags.items() if on}, tools)

    def test_subdirectory_flags(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self._dir(".claude/skills/pdf/scripts")
        self._dir(".claude/skills/pdf/references")
        self._dir(".claude/skills/pdf/assets")
        row = detect_skills(self.repo)[0]
        self.assertEqual(
            (row["scripts"], row["references"], row["assets"]), ("TRUE", "TRUE", "TRUE"))

    def test_subdirectory_flag_needs_a_directory(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self._file(".claude/skills/pdf/scripts")
        self.assertEqual(detect_skills(self.repo)[0]["scripts"], "FALSE")

    def test_subdirectory_without_skill_md_is_not_a_skill(self):
        self._file(".claude/skills/pdf/README.md")
        self.assertEqual(detect_skills(self.repo), [])

    def test_skill_md_directly_in_the_skills_dir_is_not_a_skill(self):
        self._file(".claude/skills/SKILL.md")
        self.assertEqual(detect_skills(self.repo), [])

    def test_a_category_folder_of_claude_skills_holds_no_lowercase_skill(self):
        # Cursor and OpenCode, which read the category folders that Claude Code does not, keep
        # only a file named SKILL.md in capitals, and GitHamza0206/simba's
        # .claude/skills/simba/deploy/skill.md is no skill.
        self._file(".claude/skills/group/pdf/skill.md")
        self.assertEqual(detect_skills(self.repo), [])

    def test_a_marker_in_another_case_below_another_skill_is_part_of_that_skill(self):
        """gohypergiant/standard-toolkit keeps a reference document named skill.md in the
        references/ folder of the skill .agents/skills/accelint-skill-manager/. Codex, Cursor CLI
        and OpenCode compare the name with SKILL.md exactly (`== Some(SKILLS_FILENAME)` in
        Codex's discovery.rs, `"SKILL.md"!==e.name` in Cursor CLI 2026.09.28-64d2043,
        `child.name === "SKILL.md"` in OpenCode's skill-scan.ts), and VS Code and Antigravity,
        which compare it in any case, read only the folders directly in the directory. The file
        belongs to the skill around it, as its other references do. A skill.md directly in a
        folder of the directory, as cchangelabs/openepd has one, stays a skill."""
        self._file(".agents/skills/accelint-skill-manager/SKILL.md",
                   "---\nname: accelint-skill-manager\ndescription: Manage skills\n---\nSteps.\n")
        self._file(".agents/skills/accelint-skill-manager/references/skill.md",
                   "# 1.2 SKILL.md\n\n## Overview\n\nGeneral rule of thumb is to follow guidance "
                   "from [Agent Skills].\n")
        self._file(".agents/skills/openepd/skill.md", "---\nname: openepd\ndescription: x\n---\n")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         [".agents/skills/accelint-skill-manager/SKILL.md",
                          ".agents/skills/openepd/skill.md"])
        self.assertEqual(_other_as_main_does(self.repo), [])

    def test_lowercase_skill_marker_is_a_skill(self):
        # Otherwise the same file would be a skill on a case-insensitive filesystem and no
        # skill on a case-sensitive one.
        self._file(".claude/skills/pdf/skill.md")
        self.assertEqual(detect_skills(self.repo), [{
            "skills.md": ".claude/skills/pdf/skill.md",
            "name": "pdf",
            "scripts": "FALSE",
            "references": "FALSE",
            "assets": "FALSE",
        }])

    def test_claude_command_file_is_a_skill(self):
        # In Claude Code, .claude/commands/deploy.md and .claude/skills/deploy/SKILL.md
        # both define /deploy and work the same way.
        self._file(".claude/commands/deploy.md")
        self.assertEqual(detect_skills(self.repo), [{
            "skills.md": ".claude/commands/deploy.md",
            "name": "deploy",
            "scripts": "FALSE",
            "references": "FALSE",
            "assets": "FALSE",
        }])

    def test_claude_command_files_are_found_recursively(self):
        self._file(".claude/commands/group/deploy.md")
        self.assertEqual(detect_skills(self.repo)[0]["skills.md"],
                         ".claude/commands/group/deploy.md")

    def test_a_command_folder_with_a_skill_md_is_one_skill_named_for_the_folder(self):
        # enricoros/big-AGI keeps `.claude/commands/tape-opens/SKILL.md`. Claude Code reads
        # only the SKILL.md of such a folder, as the command named for the folder, and the
        # folder holds its supporting files. A command in a folder below it stays a command.
        self._file(".claude/commands/tape-opens/SKILL.md", "---\nname: tape-opens\n---\n")
        self._file(".claude/commands/tape-opens/notes.md")
        self._file(".claude/commands/tape-opens/references/format.md")
        self.assertEqual(detect_skills(self.repo), [{
            "skills.md": ".claude/commands/tape-opens/SKILL.md",
            "name": "tape-opens",
            "scripts": "FALSE",
            "references": "TRUE",
            "assets": "FALSE",
        }, {
            "skills.md": ".claude/commands/tape-opens/references/format.md",
            "name": "format",
            "scripts": "FALSE",
            "references": "FALSE",
            "assets": "FALSE",
        }])

    def test_a_folder_linked_into_claude_commands_is_read_as_commands(self):
        # GovTechSG/sgds-web-component links `.claude/commands/sgds-forms` to
        # `../../skills/sgds-forms`. Claude Code lists the commands with ripgrep's `--follow`
        # and reads the folder's SKILL.md as the command `sgds-forms`.
        self._file(".claude/commands/sgds-forms", "../../skills/sgds-forms\n")
        self._file("skills/sgds-forms/SKILL.md", "---\nname: sgds-forms\n---\n")
        self._file("skills/sgds-forms/reference/input.md")
        self.assertEqual([(r["skills.md"], r["name"]) for r in detect_skills(self.repo)],
                         [("skills/sgds-forms/SKILL.md", "sgds-forms"),
                          ("skills/sgds-forms/reference/input.md", "input")])
        self.assertIn(".claude/commands/sgds-forms", _mod.resolved_markers(self.repo))

    def test_non_markdown_in_claude_commands_is_not_a_skill(self):
        self._file(".claude/commands/deploy.toml")
        self.assertEqual(detect_skills(self.repo), [])

    def test_cursor_commands_are_not_skills(self):
        # Only Claude Code merged the two mechanisms.
        self._file(".cursor/commands/deploy.md")
        self.assertEqual(detect_skills(self.repo), [])

    def test_a_command_and_a_skill_of_the_same_name_are_both_recorded(self):
        # In Claude Code the skill takes precedence, but both files exist and the dataset
        # records configuration artifacts, not which one would run.
        self._file(".claude/commands/deploy.md")
        self._file(".claude/skills/deploy/SKILL.md")
        self.assertEqual(sorted(r["skills.md"] for r in detect_skills(self.repo)),
                         [".claude/commands/deploy.md", ".claude/skills/deploy/SKILL.md"])

    def test_a_saved_workflow_is_a_skill(self):
        # A workflow script is a skill without a directory, as a command file is, in the
        # project and in a plugin that the project contains. The case names both paths, because
        # dropping the loop over them must fail it.
        self._file(".claude/workflows/release.js", "export default async () => {}\n")
        self._file(".claude/plugins/ship/.claude-plugin/plugin.json", '{"name": "ship"}')
        self._file(".claude/plugins/ship/workflows/build.js", "export default async () => {}\n")
        self._file(".claude/workflows/notes.txt", "scratch\n")
        blank = {"scripts": "FALSE", "references": "FALSE", "assets": "FALSE"}
        self.assertEqual(detect_skills(self.repo), [
            {"skills.md": ".claude/workflows/release.js", "name": "release", **blank},
            {"skills.md": ".claude/plugins/ship/workflows/build.js", "name": "build", **blank},
        ])
        self.assertEqual(_other_as_main_does(self.repo), [".claude/workflows/notes.txt"])

    def test_multiple_skills_all_returned(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self._file(".claude/skills/xlsx/SKILL.md")
        self.assertEqual({r["name"] for r in detect_skills(self.repo)}, {"pdf", "xlsx"})


# ---------------------------------------------------------------------------
# detect_subagents  (recursive *.md, and memory only for .claude/agents)
# ---------------------------------------------------------------------------

class TestDetectSubagents(_RepoCase):

    def test_repo_without_subagents(self):
        self.assertEqual(detect_subagents(self.repo), [])

    def test_claude_subagent_row_fields(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self.assertEqual(detect_subagents(self.repo), [{
            "subagent": ".claude/agents/reviewer.md",
            "name": "reviewer",
            "memory": "FALSE",
        }])

    def test_memory_is_true_when_the_memory_file_exists(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory/reviewer/MEMORY.md")
        self.assertEqual(detect_subagents(self.repo)[0]["memory"], "TRUE")

    def test_local_memory_also_counts(self):
        # .claude/agent-memory-local is the scope meant to stay out of version control. A
        # repository that commits it still gives the subagent memory.
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory-local/reviewer/MEMORY.md")
        self.assertEqual(detect_subagents(self.repo)[0]["memory"], "TRUE")

    def test_memory_is_matched_by_agent_name(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory/other/MEMORY.md")
        self.assertEqual(detect_subagents(self.repo)[0]["memory"], "FALSE")

    def test_a_memory_directory_of_topic_files_is_memory(self):
        """The memory page: the directory "contains a MEMORY.md index and one topic file per
        memory", and Claude reads the topic files on demand. A subagent with topic files and no
        index in its memory still has memory. A test for MEMORY.md alone would report none."""
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory/reviewer/cloud_client_patterns.md")
        self.assertEqual(detect_subagents(self.repo)[0]["memory"], "TRUE")

    def test_only_the_direct_markdown_of_the_copilot_directory_is_an_agent(self):
        """GitHub names .github/agents/NAME.md, and VS Code reads only the files directly in
        the folder, except README.md. The playbooks that an agent links to are not agents."""
        self._file(".github/agents/release-manager.agent.md")
        self._file(".github/agents/README.md")
        self._file(".github/agents/release-manager/validation-release/README.md")
        self._file(".github/agents/release-manager/references/checklist.md")
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".github/agents/release-manager.agent.md"])

    def test_memory_is_not_applicable_outside_claude(self):
        for agents_dir in (".github/agents", ".cursor/agents", ".gemini/agents"):
            with self.subTest(agents_dir):
                self._file(f"{agents_dir}/reviewer.md",
                           "---\nname: reviewer\ndescription: Reviews code.\n---\nReview.\n")
                rows = [r for r in detect_subagents(self.repo)
                        if r["subagent"].startswith(agents_dir)]
                self.assertEqual(rows[0]["memory"], "n/a")

    def test_a_file_claude_code_skips_is_no_subagent(self):
        """According to Claude Code's subagents page, a file without a name in a project's
        agents directory is documentation kept beside the agents. Claude Code skips a file with
        a name that starts with -, contains : or is longer than 256 characters, and a file with
        a name and no description. Checkmk/checkmk's .claude/agents/README.md is therefore no
        subagent."""
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agents/README.md", "# Do not add agents here\n")
        self._file(".claude/agents/named.md", "---\nname: named\n---\n")
        self._file(".claude/agents/described.md", "---\ndescription: Does it\n---\n")
        self._file(".claude/agents/colon.md", "---\nname: team:reviewer\ndescription: d\n---\n")
        self._file(".claude/agents/dash.md", "---\nname: '-reviewer'\ndescription: d\n---\n")
        self._file(".claude/agents/long.md", f"---\nname: {'r' * 257}\ndescription: d\n---\n")
        self._file(".claude/agents/role.md", "---\nrole: reviewer\ndescription: d\n---\n")
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".claude/agents/reviewer.md"])

    def test_a_linked_subagent_is_judged_by_the_file_it_links(self):
        """The collector writes a link as a file that contains its target, and it copies the
        target. The front matter test therefore reads the target rather than the link's text.
        The link's text has no front matter, and judging it would drop a subagent such as
        Ramilito/kubectl.nvim's `.claude/agents/lua.md -> ../../.agents/roles/lua.md`. A file
        with a line that names nothing in the tree, such as streamlit/streamlit's
        `@./AGENTS.md`, is judged as it is."""
        self._file(".agents/roles/lua.md", AGENT)
        self._file(".agents/roles/notes.md", "# Notes\n")
        self._file(".claude/agents/lua.md", "../../.agents/roles/lua.md\n")
        self._file(".claude/agents/notes.md", "../../.agents/roles/notes.md\n")
        self._file(".claude/agents/CLAUDE.md", "@./AGENTS.md\n")
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".claude/agents/lua.md"])

    def test_subagents_are_found_recursively(self):
        self._file(".claude/agents/team/reviewer.md", AGENT)
        row = detect_subagents(self.repo)[0]
        self.assertEqual(row["subagent"], ".claude/agents/team/reviewer.md")
        self.assertEqual(row["name"], "reviewer")

    def test_non_markdown_is_not_a_subagent(self):
        self._file(".claude/agents/reviewer.txt")
        self.assertEqual(detect_subagents(self.repo), [])

    def test_gemini_subagent_is_markdown(self):
        self._file(".gemini/agents/security-auditor.md",
                   "---\nname: security-auditor\ndescription: Audits.\n---\nAudit.\n")
        self.assertEqual(detect_subagents(self.repo), [{
            "subagent": ".gemini/agents/security-auditor.md",
            "name": "security-auditor",
            "memory": "n/a",
        }])

    def test_codex_subagent_is_toml(self):
        self._file(".codex/agents/ci-monitor.toml")
        self.assertEqual(detect_subagents(self.repo), [{
            "subagent": ".codex/agents/ci-monitor.toml",
            "name": "ci-monitor",
            "memory": "n/a",
        }])

    def test_a_file_of_claude_s_agents_that_only_cursor_loads_is_cursor_s_subagent(self):
        """Cursor reads `.claude/agents` "(Claude compatibility)". benbjohnson/litestream's
        `sqlite-expert.md` has a front matter without a `name`, which Claude Code treats as
        documentation, and Cursor's loader accepts it. Claude Code has no memory for such a file."""
        self._file(".claude/agents/sqlite-expert.md", "---\nrole: SQLite WAL and Page Expert\n---\nExplain.\n")
        self.assertEqual(detect_subagents(self.repo), [{
            "subagent": ".claude/agents/sqlite-expert.md", "name": "sqlite-expert", "memory": "n/a"}])
        self.assertEqual(_mod.artifact_tools(self.repo, [("subagents", ".claude/agents/sqlite-expert.md")]),
                         {("subagents", ".claude/agents/sqlite-expert.md"): {"cursor"}})

    def test_a_file_of_claude_s_agents_that_claude_code_loads_is_claude_s(self):
        """Both tools read the file, and it is the agent of the directory's own tool."""
        self._file(".claude/agents/reviewer.md", "---\nname: reviewer\ndescription: Reviews\n---\nReview.\n")
        self.assertEqual(detect_subagents(self.repo), [{
            "subagent": ".claude/agents/reviewer.md", "name": "reviewer", "memory": "FALSE"}])
        self.assertEqual(_mod.artifact_tools(self.repo, [("subagents", ".claude/agents/reviewer.md")]),
                         {("subagents", ".claude/agents/reviewer.md"): {"claude"}})

    def test_a_file_of_claude_s_agents_that_neither_tool_loads_is_no_subagent(self):
        """A file without front matter, which Claude Code and Cursor both refuse."""
        self._file(".claude/agents/README.md", "# Agents\n\nNotes.\n")
        self.assertEqual(detect_subagents(self.repo), [])

    def test_a_document_with_two_horizontal_rules_in_claude_s_agents_is_no_subagent(self):
        """openshift/addon-operator's `.claude/agents/README.md` opens with a heading and has two
        `---` lines further down, and streamlit/streamlit's `AGENTS.md` has a pair inside a fenced
        template. Cursor's parser accepts both, but they are documents with horizontal rules and no
        YAML front matter, which Cursor's page requires of a subagent."""
        self._file(".claude/agents/README.md",
                   "# Claude Agents\n\nThe agents.\n\n---\n\n### [test-agent](./test-agent.md)\n\n"
                   "---\n\nMore notes.\n")
        self._file(".claude/agents/AGENTS.md",
                   "# Agent Definitions\n\n```markdown\n---\nname: agent-name\n---\nBody.\n```\n")
        self.assertEqual(detect_subagents(self.repo), [])
        self.assertEqual(_mod.tools_reading("subagents", ".claude/agents/README.md",
                                            self.repo / ".claude/agents/README.md", self.repo)[1],
                         set())

    def test_html_comments_before_a_front_matter_keep_cursor_s_subagent(self):
        """lablup/backend.ai-webui's agents open with two comment lines above the front matter,
        which Cursor's parser reads as the front matter that the page requires."""
        self._file(".claude/agents/docs-update-writer.md",
                   "<!-- Generated by docs-toolkit. You may customize this file. -->\n"
                   "<!-- Re-run the toolkit to regenerate from the template. -->\n"
                   "---\nrole: Docs update writer\n---\nWrite.\n")
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".claude/agents/docs-update-writer.md"])
        self.assertEqual(_mod.tools_reading("subagents", ".claude/agents/docs-update-writer.md",
                                            self.repo / ".claude/agents/docs-update-writer.md",
                                            self.repo)[1],
                         {"cursor"})

    def test_a_byte_order_mark_stands_above_the_front_matter_of_a_cursor_agent(self):
        """Cursor's reader `Ct.yR` removes a byte order mark before its parser `Hs` reads the
        file, so that a block below the mark is a front matter at the top of the file. A heading
        below the mark stays a document, and a file that Claude Code loads stays Claude Code's."""
        self._file(".cursor/agents/plain.md", "\ufeff---\nmodel: fast\n---\nBody.\n")
        self._file(".cursor/agents/later-rule.md", "\ufeff---\nmodel: fast\n---\nBody.\n\n---\n\nMore.\n")
        self._file(".cursor/agents/heading.md", "\ufeff# Notes\n\n---\nx: y\n---\nBody.\n")
        self._file(".claude/agents/no-name.md", "\ufeff---\nmodel: fast\n---\nBody.\n")
        self._file(".claude/agents/named.md", "\ufeff---\nname: named\ndescription: d\n---\nBody.\n")
        self._file(".codex/agents/linter.md", "\ufeff---\nmodel: fast\n---\nBody.\n")
        self.assertEqual({r["subagent"] for r in detect_subagents(self.repo)},
                         {".cursor/agents/plain.md", ".cursor/agents/later-rule.md",
                          ".claude/agents/no-name.md", ".claude/agents/named.md",
                          ".codex/agents/linter.md"})
        rows = [("subagents", path) for path in (".cursor/agents/plain.md", ".claude/agents/no-name.md",
                                                 ".claude/agents/named.md", ".codex/agents/linter.md")]
        self.assertEqual(_mod.artifact_tools(self.repo, rows),
                         {("subagents", ".cursor/agents/plain.md"): {"cursor"},
                          ("subagents", ".claude/agents/no-name.md"): {"cursor"},
                          ("subagents", ".claude/agents/named.md"): {"claude"},
                          ("subagents", ".codex/agents/linter.md"): {"cursor"}})

    def test_cursor_s_other_extensions_in_claude_s_agents_are_cursor_s(self):
        """Cursor's loader reads `.md`, `.mdc` and `.markdown` there, and Claude Code only `.md`."""
        self._file(".claude/agents/reviewer.mdc", "---\nname: reviewer\n---\nReview.\n")
        self._file(".claude/agents/notes.mdc", "# Notes\n")
        self.assertEqual({r["subagent"]: r["memory"] for r in detect_subagents(self.repo)},
                         {".claude/agents/reviewer.mdc": "n/a"})
        self.assertEqual(_mod.tool_for_path(".claude/agents/reviewer.md"), "claude")
        self.assertEqual(_mod.artifact_tools(self.repo, [("subagents", ".claude/agents/reviewer.mdc")]),
                         {("subagents", ".claude/agents/reviewer.mdc"): {"cursor"}})

    def test_a_link_to_the_agents_of_claude_gives_each_file_the_tool_that_loads_it(self):
        """yamadashy/repomix's `.claude/agents` is a link to `../.agents/agents`. A file that Claude
        Code loads through the link is Claude Code's, and one that it does not load is Cursor's."""
        self._file(".agents/agents/reviewer.md", "---\nname: reviewer\ndescription: Reviews\n---\nReview.\n")
        self._file(".agents/agents/expert.md", "---\nrole: Expert\n---\nExplain.\n")
        rows = [("subagents", ".agents/agents/reviewer.md"), ("subagents", ".agents/agents/expert.md")]
        found = _mod.artifact_tools(self.repo, rows, marker_dirs=[(".claude/agents", ".agents/agents")])
        self.assertIn("claude", found[rows[0]])
        self.assertNotIn("claude", found[rows[1]])
        self.assertIn("cursor", found[rows[1]])

    def test_an_other_row_of_an_agents_folder_stays_the_tool_of_its_directory(self):
        """A Markdown file of `.codex/agents` that opens with a heading and that no role names is
        no subagent of Cursor, and an `other` row takes the owner of its directory, also where
        Cursor reads the directory for compatibility. `tool_for_path` gives the Markdown there to
        Cursor."""
        self._file(".codex/agents/planner.md", "# Implementation Planner\n")
        self._file(".codex/agents/planner.toml", 'developer_instructions = "Plan."\n')
        self._file("pkg/.codex/agents/notes.md", "# Notes\n")
        self._file(".claude/agents/README.md", "# Agents\n")
        rows = [("other", ".codex/agents/planner.md"), ("other", "pkg/.codex/agents/notes.md"),
                ("other", ".claude/agents/README.md")]
        self.assertEqual(_mod.artifact_tools(self.repo, rows),
                         {rows[0]: {"codex"}, rows[1]: {"codex"}, rows[2]: {"claude"}})

    def test_a_link_in_claude_s_skills_adds_no_reader_of_the_shared_skills(self):
        """amelioro/ameliorate's `.claude/skills/skills` is a link to `.agents/skills`. Several tools
        read that directory, and the skill keeps `any`, as the directory's own skills do. A link in
        a reader's own directory still credits that reader."""
        self._file(".agents/skills/commit-message/SKILL.md", "---\nname: m\ndescription: d\n---\n")
        row = ("skills", ".agents/skills/commit-message/SKILL.md")
        self.assertEqual(_mod.artifact_tools(self.repo, [row], marker_dirs=[
            (".claude/skills/skills", ".agents/skills")]), {row: set()})
        self.assertEqual(_mod.artifact_tools(self.repo, [row], marker_dirs=[
            (".cursor/skills", ".agents/skills")]), {row: {"cursor"}})

    def test_a_link_in_antigravity_s_own_skills_directories_credits_gemini(self):
        """sanity-io/pkg-utils's `.agent/skills/tsdown` and yuanjian-org/app's
        `.agent/skills/web-design-guidelines` link to `.agents/skills`. Only Antigravity, which
        counts as Gemini, reads `.agent/skills`, `_agents/skills` and `_agent/skills`, so each is
        its own directory, as `.gemini/skills` is. A link in `.claude/skills`, which Cursor and
        OpenCode read as well, stays a link in another tool's directory for them."""
        self._file(".agents/skills/s/SKILL.md", "---\nname: s\ndescription: d\n---\n")
        row = ("skills", ".agents/skills/s/SKILL.md")
        for marker, target in ((".agent/skills/s", ".agents/skills/s"),
                               ("_agents/skills/s", ".agents/skills/s"),
                               ("_agent/skills/s", ".agents/skills/s"),
                               (".agent/skills", ".agents/skills"), (".agent", ".agents"),
                               (".gemini/skills/s", ".agents/skills/s")):
            with self.subTest(marker=marker):
                self.assertEqual(_mod.artifact_tools(self.repo, [row], marker_dirs=[(marker, target)]),
                                 {row: {"gemini"}})
        self.assertEqual(_mod.artifact_tools(self.repo, [row], marker_dirs=[
            (".claude/skills/skills", ".agents/skills")]), {row: set()})

    def test_opencode_s_configuration_that_defines_an_agent_is_a_subagent(self):
        """OpenCode's agents page: "Define agents under agents in any OpenCode configuration
        file", and its migration page: "The singular agent and deprecated mode maps become
        agents." One row per file, named after the file, also where two keys define an agent."""
        cases = {"opencode.json": '{"agent": {"playwright-test-generator": {"mode": "subagent"}}}',
                 "opencode.jsonc": '{\n  // agents\n  "agents": {"reviewer": {"mode": "subagent"},},\n}',
                 ".opencode/opencode.json": '{"agent": {"a": {}}, "agents": {"b": {}}}',
                 ".opencode/opencode.jsonc": '{"agents": {"reviewer": {}}}',
                 "packages/web/.opencode/opencode.json": '{"mode": {"reviewer": {}}}'}
        for path, text in cases.items():
            self._file(path, text)
        for path in cases:
            with self.subTest(path=path):
                self.assertIn({"subagent": path, "name": Path(path).stem, "memory": "n/a"},
                              detect_subagents(self.repo))
                self.assertEqual(_mod.artifact_tools(self.repo, [("subagents", path)]),
                                 {("subagents", path): {"opencode"}})

    def test_opencode_s_configuration_of_its_built_in_agents_defines_none(self):
        """OpenCode's agent.ts loads build, plan, general, explore, compaction, title and summary
        with `native: true`, and an entry with such a name adjusts that agent."""
        self._file("opencode.json", '{"agent": {"build": {"model": "x"}, "plan": {}, "general": {}, '
                                    '"explore": {}, "compaction": {}, "title": {}, "summary": {}}}')
        self._file("opencode.jsonc", '{"agents": {}, "mode": {"build": {}, "plan": {}}}')
        self._file(".opencode/opencode.json", '{"agent": "reviewer"}')
        self._file(".opencode/opencode.jsonc", '{"mode": {}}')
        self.assertEqual(detect_subagents(self.repo), [])

    def test_markdown_in_the_codex_directory_is_cursor_s_subagent(self):
        """Codex reads only the TOML files of .codex/agents. Cursor reads the Markdown there
        "(Codex compatibility)". Each file is the subagent of the tool that reads it."""
        self._file(".codex/agents/reviewer.md", "---\nname: reviewer\n---\nReview.\n")
        self._file(".codex/agents/planner.toml")
        self.assertEqual({r["subagent"] for r in detect_subagents(self.repo)},
                         {".codex/agents/reviewer.md", ".codex/agents/planner.toml"})
        self.assertEqual(_mod.tool_for_path(".codex/agents/reviewer.md"), "cursor")
        self.assertEqual(_mod.tool_for_path(".codex/agents/planner.toml"), "codex")

    def test_a_cursor_agent_without_front_matter_is_no_subagent(self):
        """Cursor's subagents page: "Each subagent is a markdown file with YAML frontmatter".
        The project loader of Cursor CLI 2026.09.28-64d2043 skips a file that its parser `Hs`
        refuses, and `Hs` needs a line `---`, a later line that starts with `---` and a body
        after it that is not blank. The parser's pattern is multiline, so that a pair of such
        lines further down the file also passes it, although such a file has no YAML front matter
        at its top. Only blank lines or HTML comments may therefore stand before the first `---`
        line. Every field of the front matter is optional. The files without front matter
        are tegojs/tego's, the instructions file of a Codex agent of areal-project/AReaL, which
        is a part of its Codex role, and sveltejs/cli's test snapshot."""
        self._file(".cursor/agents/debugging.md",
                   "# Debugging Agent / 错误调试 Agent\n\n## Purpose / 目的\n\nAnalyze errors.\n")
        self._file(".codex/agents/planner.md",
                   "# Implementation Planner\n\nYou are an expert software architect.\n")
        self._file(".codex/agents/planner.toml",
                   'name = "planner"\nmodel_instructions_file = "./planner.md"\n')
        self._file("packages/sv/snapshots/.cursor/agents/sv-files-snapshots.md",
                   "svelte-file-editor.md\n")
        self._file(".cursor/agents/single-separator.md", "# Notes\n\n---\n\nMore notes.\n")
        self._file(".cursor/agents/no-body.md", "---\nname: no-body\n---\n\n")
        self._file(".cursor/agents/unclosed.md", "---\nname: unclosed\nReview.\n")
        self._file(".cursor/agents/reviewer.md", "---\nname: reviewer\n---\nReview.\n")
        self._file(".cursor/agents/empty-front-matter.md", "---\n\n---\nReview.\n")
        self._file(".cursor/agents/later-pair.md",
                   "# Notes\n\n---\ntitle: notes\n---\nReview.\n")
        self._file(".codex/agents/linter.md", "---\ndescription: Lints\n---\nLint.\n")
        self.assertEqual({r["subagent"] for r in detect_subagents(self.repo)},
                         {".cursor/agents/reviewer.md", ".cursor/agents/empty-front-matter.md",
                          ".codex/agents/linter.md", ".codex/agents/planner.toml"})

    def test_a_cursor_agent_that_a_link_reaches_needs_front_matter_too(self):
        """comet-ml/opik links `.cursor` to `.agents`, and Cursor reads the Markdown of
        `.agents/agents` through `.cursor/agents` with the same loader."""
        tools = _mod.tools_reading
        self._file(".agents/agents/reviewer.md",
                   "---\nname: reviewer\ndescription: Reviews.\n---\nReview.\n")
        self._file(".agents/agents/notes.md", "# Notes on the agents\n")
        self.assertEqual(tools("subagents", ".cursor/agents/reviewer.md",
                               self.repo / ".agents/agents/reviewer.md", self.repo)[1],
                         {"cursor"})
        self.assertEqual(tools("subagents", ".cursor/agents/notes.md",
                               self.repo / ".agents/agents/notes.md", self.repo),
                         (False, set()))

    def test_codex_subagents_are_found_recursively(self):
        self._file(".codex/agents/team/reviewer.toml")
        self.assertEqual(detect_subagents(self.repo)[0]["subagent"],
                         ".codex/agents/team/reviewer.toml")

    def test_multiple_subagents_all_returned(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agents/planner.md", AGENT)
        self.assertEqual({r["name"] for r in detect_subagents(self.repo)},
                         {"reviewer", "planner"})

    def test_an_antigravity_folder_agent_is_its_agent_md_in_any_case(self):
        """Antigravity CLI 1.4.9 finds the file of a folder agent with
        `customizations.findFileCaseInsensitive`, which compares the names with
        `strings.EqualFold`. opensupplyhub/open-supply-hub keeps its agents at
        `.agent/agents/<name>/AGENT.md`, and each one is a subagent of Gemini, reported at the
        path that the listing spells, on macOS as on Linux."""
        self._file(".agent/agents/oshub-codebase-expert/AGENT.md",
                   "---\nname: oshub-codebase-expert\ndescription: Investigates the OS Hub "
                   "codebase to map architecture.\n---\nbody\n")
        self._file(".agent/agents/lower/agent.md", "---\nname: lower\ndescription: d\n---\nbody\n")
        rows = detect_subagents(self.repo)
        self.assertEqual(rows, [
            {"subagent": ".agent/agents/lower/agent.md", "name": "lower", "memory": "n/a"},
            {"subagent": ".agent/agents/oshub-codebase-expert/AGENT.md",
             "name": "oshub-codebase-expert", "memory": "n/a"}])
        self.assertTrue(all(_spelled_on_disk(self.repo, row["subagent"]) for row in rows))
        self.assertEqual(_other_as_main_does(self.repo), [])
        self.assertEqual(_mod.tool_for_path(".agent/agents/oshub-codebase-expert/AGENT.md"),
                         "gemini")
        rel = ".agent/agents/oshub-codebase-expert/AGENT.md"
        self.assertEqual(_mod.tools_reading("subagents", rel, self.repo / rel, self.repo),
                         (True, {"gemini"}))

    def test_an_excluded_folder_agent_is_left_out_in_any_case(self):
        """A top-level `exclude` of `.agents/agents.json` leaves the folder agent out of its
        default place, whatever the letter case of its `AGENT.md`, and the file is then an
        `other` row of the customization root."""
        self._file(".agents/agents.json", '{"exclude": ["upper"]}')
        self._file(".agents/agents/upper/AGENT.md", "---\nname: upper\ndescription: d\n---\nx\n")
        self.assertEqual(paths.antigravity_left_out(self.repo),
                         {".agents/agents/upper/AGENT.md": ".agents/agents"})
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".agents/agents.json"])
        self.assertIn(".agents/agents/upper/AGENT.md", _other_as_main_does(self.repo))


# ---------------------------------------------------------------------------
# detect_commands  (Gemini *.toml and Cursor *.md recursively, Copilot *.prompt.md directly)
# ---------------------------------------------------------------------------

class TestDetectCommands(_RepoCase):

    def test_repo_without_commands(self):
        self.assertEqual(detect_commands(self.repo), [])

    def test_gemini_toml_command(self):
        self._file(".gemini/commands/review.toml")
        self.assertEqual(detect_commands(self.repo),
                         [{"command": ".gemini/commands/review.toml", "name": "review"}])

    def test_gemini_ignores_non_toml(self):
        self._file(".gemini/commands/review.md")
        self.assertEqual(detect_commands(self.repo), [])

    def test_gemini_recurses_into_namespaces(self):
        # .gemini/commands/git/fix.toml is the command /git:fix.
        self._file(".gemini/commands/group/review.toml")
        self.assertEqual(detect_commands(self.repo),
                         [{"command": ".gemini/commands/group/review.toml",
                           "name": "review"}])

    def test_cursor_markdown_command(self):
        self._file(".cursor/commands/review.md")
        self.assertEqual(detect_commands(self.repo),
                         [{"command": ".cursor/commands/review.md", "name": "review"}])

    def test_antigravity_workflows_in_either_directory(self):
        """Antigravity runs its workflows as slash commands. They are in `.agents/workflows`,
        or in `.agent/workflows`, where Google's codelab put them before the rename and where
        Antigravity still reads them. Only the Markdown files directly in either directory
        count."""
        self._file(".agents/workflows/deploy.md", "---\ndescription: deploy\n---\n")
        self._file(".agent/workflows/release.md", "---\ndescription: release\n---\n")
        self._file(".agent/workflows/notes/draft.md", "x\n")
        self.assertEqual(sorted(c["command"] for c in detect_commands(self.repo)),
                         [".agent/workflows/release.md", ".agents/workflows/deploy.md"])
        _files, tools = derive_flags([], [], [], [], detect_commands(self.repo), [], [], [], [])
        self.assertTrue(tools["gemini"])

    def test_claude_commands_are_not_commands(self):
        # Claude Code merged custom commands into skills. The detector reports
        # .claude/commands as a skill instead (see TestDetectSkills).
        self._file(".claude/commands/review.md")
        self.assertEqual(detect_commands(self.repo), [])

    def test_cursor_commands_recurse_as_the_ide_reads_them(self):
        """A Cursor staff member, 2026-03-16: "the IDE recursively scans subdirectories within
        .cursor/commands/", while the CLI reads the top level alone
        (https://forum.cursor.com/t/154719)."""
        self._file(".cursor/commands/group/review.md")
        self._file(".cursor/commands/group/notes.txt")
        self.assertEqual(detect_commands(self.repo),
                         [{"command": ".cursor/commands/group/review.md", "name": "review"}])

    def test_markdown_dirs_ignore_non_markdown(self):
        self._file(".cursor/commands/review.toml")
        self.assertEqual(detect_commands(self.repo), [])

    def test_copilot_prompt_file_is_a_command(self):
        # "Prompt files are Markdown files with the .prompt.md extension", and a user
        # invokes one by typing / followed by its name.
        self._file(".github/prompts/lint-fix.prompt.md")
        self.assertEqual(detect_commands(self.repo),
                         [{"command": ".github/prompts/lint-fix.prompt.md",
                           "name": "lint-fix"}])

    def test_plain_markdown_in_prompts_dir_is_not_a_command(self):
        # The double extension is required. A plain .md there does not work.
        self._file(".github/prompts/notes.md")
        self.assertEqual(detect_commands(self.repo), [])

    def test_nested_prompt_files_are_not_commands(self):
        self._file(".github/prompts/group/deploy.prompt.md")
        self.assertEqual(detect_commands(self.repo), [])

    def test_toml_commands_come_before_markdown_commands(self):
        self._file(".cursor/commands/a.md")
        self._file(".gemini/commands/b.toml")
        self.assertEqual([r["name"] for r in detect_commands(self.repo)], ["b", "a"])


# ---------------------------------------------------------------------------
# _has_hooks  (a hook that one of the tools loads, in that tool's format)
# ---------------------------------------------------------------------------

# The tools that nest each handler of a hooks file in a matcher group of its event, and the
# tools that list the handlers under the event.
_GROUPED = ("claude", "codex", "gemini")
_FLAT = ("cursor", "copilot")


class TestHasHooks(_RepoCase):

    def _loads(self, content):
        """{tool: whether the tool loads a hook from a hooks file with this content}."""
        p = self._file("hooks.json", content)
        return {tool: _has_hooks(p, tools={tool}) for tool in _GROUPED + _FLAT}

    def test_non_empty_hooks_object(self):
        handler = '[{"matcher": "Bash", "hooks": [{"type": "command", "command": "./check.sh"}]}]'
        p = self._file("settings.json", f'{{"hooks": {{"PreToolUse": {handler}, "BeforeTool": {handler}}}}}')
        for tool in _GROUPED + _FLAT:
            with self.subTest(tool=tool):
                self.assertTrue(_has_hooks(p, tools={tool}))

    def test_gemini_cli_reads_only_its_own_events(self):
        """GoogleCloudPlatform/k8s-config-connector's `.gemini/settings.json` has one event,
        `PreToolUse`, which is Claude Code's name. Gemini CLI skips a name that is not a value of
        its `HookEventName`: "Invalid hook event name: ... Skipping." """
        handler = '[{"matcher": "Write", "hooks": [{"type": "command", "command": "./check.sh"}]}]'
        foreign = self._file("settings.json", f'{{"hooks": {{"PreToolUse": {handler}}}}}')
        self.assertFalse(_has_hooks(foreign, tools={"gemini"}))
        self.assertTrue(_has_hooks(foreign, tools={"claude"}))
        for event in ("BeforeTool", "AfterTool", "BeforeAgent", "Notification", "AfterAgent",
                      "SessionStart", "SessionEnd", "PreCompress", "BeforeModel", "AfterModel",
                      "BeforeToolSelection"):
            with self.subTest(event=event):
                own = self._file("own.json", f'{{"hooks": {{"{event}": {handler}}}}}')
                self.assertTrue(_has_hooks(own, tools={"gemini"}))

    def test_an_event_without_a_hook_configures_none(self):
        """wp-media/wp-rocket's `.claude/settings.json` is `{"hooks": {"PostToolUse": []}}`, and
        microsoft/FluidFramework's has `"hooks": {"PreToolUse": []}`: the event holds no matcher
        group and no handler, so Claude Code runs no hook. An event with a hook beside it
        counts."""
        p = self._file("settings.json", '{"hooks": {"PostToolUse": []}}')
        self.assertFalse(_has_hooks(p, tools={"claude"}))
        q = self._file("other.json", '{"hooks": {"PreToolUse": [], "Stop": [{"hooks": '
                                     '[{"type": "command", "command": "true"}]}]}}')
        self.assertTrue(_has_hooks(q, tools={"claude"}))

    def test_an_event_that_holds_an_object_configures_no_hook(self):
        """itbench-hub/ITBench's `.claude/settings.json` maps UserPromptSubmit to one object.
        Claude Code 2.1.318 drops the event, "hooks.UserPromptSubmit: must be an array of
        matchers; received an object; entry ignored", and so does Gemini CLI ("is not an array.
        Skipping."). Codex reads each event as a list of matcher groups."""
        loads = self._loads('{"hooks": {"UserPromptSubmit": '
                            '{"command": "$CLAUDE_PROJECT_DIR/.claude/hooks/skill-activation-prompt.sh"}}}')
        for tool in _GROUPED:
            with self.subTest(tool=tool):
                self.assertFalse(loads[tool])

    def test_a_handler_in_place_of_a_matcher_group_configures_no_hook(self):
        """nevalang/neva's `.codex/hooks.json` lists the handler of Stop where Codex reads a
        `MatcherGroup` with `#[serde(default)] pub hooks: Vec<HookHandlerConfig>`
        (codex-rs/protocol/src/hooks.rs at be765f1a): a group with no handler, which runs
        nothing. Claude Code and Gemini CLI discard an entry without a `hooks` list. Cursor and
        Copilot list their handlers under the event, and Copilot CLI also reads Claude Code's
        matcher groups."""
        loads = self._loads('{"hooks": {"Stop": [{"type": "command", "command": '
                            '"$(git rev-parse --show-toplevel)/.codex/hooks/run-pre-commit-on-stop.sh", '
                            '"timeout": 120, "statusMessage": "Running pre-commit checks"}]}}')
        self.assertEqual(loads, {"claude": False, "codex": False, "gemini": False,
                                 "cursor": True, "copilot": True})
        group = '[{"matcher": "", "hooks": [{"type": "command", "command": "./stop.sh"}]}]'
        grouped = self._loads(f'{{"hooks": {{"Stop": {group}, "AfterAgent": {group}}}}}')
        self.assertEqual(set(grouped.values()), {True})
        empty_group = self._loads('{"hooks": {"SessionStart": [{"matcher": "startup", "hooks": []}]}}')
        for tool in _GROUPED:
            with self.subTest(tool=tool):
                self.assertFalse(empty_group[tool])

    def test_codex_refuses_a_hooks_file_with_another_top_level_key(self):
        """Codex parses a hooks file as `#[serde(deny_unknown_fields)] pub struct HooksFile {
        description, hooks }` (codex-rs/config/src/hook_config.rs at be765f1a), and
        razorpay/blade's plugin hooks file opens with `_comment`. Claude Code strips the key and
        loads the hook."""
        hook = ('"hooks": {"SessionStart": [{"hooks": [{"type": "command", '
                '"command": "node \\"${CLAUDE_PLUGIN_ROOT}/hooks/session-start.mjs\\"", "timeout": 5}]}]}')
        loads = self._loads('{"_comment": "Plain Node scripts with no dependencies.", ' + hook + '}')
        self.assertFalse(loads["codex"])
        self.assertTrue(loads["claude"])
        self.assertTrue(self._loads('{"description": "d", ' + hook + '}')["codex"])

    def test_non_empty_hooks_array(self):
        p = self._file("settings.json", '{"hooks": [{"a": 1}]}')
        self.assertTrue(_has_hooks(p, tools={"cursor"}))

    def test_empty_hooks_object(self):
        p = self._file("settings.json", '{"hooks": {}}')
        self.assertFalse(_has_hooks(p, tools=_GROUPED + _FLAT))

    def test_empty_hooks_array(self):
        p = self._file("settings.json", '{"hooks": []}')
        self.assertFalse(_has_hooks(p, tools=_GROUPED + _FLAT))

    def test_hooks_key_absent(self):
        p = self._file("settings.json", '{"model": "opus"}')
        self.assertFalse(_has_hooks(p, tools=_GROUPED + _FLAT))

    def test_hooks_null(self):
        p = self._file("settings.json", '{"hooks": null}')
        self.assertFalse(_has_hooks(p, tools=_GROUPED + _FLAT))

    def test_truthiness_not_type(self):
        # In a flat format, any truthy value counts, including a bare string.
        p = self._file("settings.json", '{"hooks": "always"}')
        self.assertTrue(_has_hooks(p, tools={"copilot"}))

    def test_malformed_json(self):
        p = self._file("settings.json", "{not json")
        self.assertFalse(_has_hooks(p, tools=_GROUPED + _FLAT))

    def test_json_that_is_not_an_object(self):
        p = self._file("settings.json", "[1, 2]")
        self.assertFalse(_has_hooks(p, tools=_GROUPED + _FLAT))

    def test_missing_file(self):
        self.assertFalse(_has_hooks(self.repo / "nope.json", tools=_GROUPED + _FLAT))


# ---------------------------------------------------------------------------
# detect_hooks  (settings and hook files that declare hooks, plus .github/hooks/*.json)
# ---------------------------------------------------------------------------

class TestDetectHooks(_RepoCase):

    # A hook in the three levels of event, matcher group and handler, which every tool reads.
    HOOKS = '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}}'
    GEMINI_HOOKS = '{"hooks": {"BeforeTool": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}]}}'

    def test_repo_without_hooks(self):
        self.assertEqual(detect_hooks(self.repo), [])

    def test_a_hookify_rule_file_is_a_hook(self):
        """hookify's loader reads `.claude/hookify.*.local.md` directly in .claude. A file in
        a folder below .claude, or a name outside the pattern, is no hook."""
        self._file(".claude/hookify.warn-rm.local.md", "---\nname: warn-rm\nevent: bash\n---\n")
        self._file(".claude/hookify.md", "notes")
        self._file(".claude/rules/hookify.x.local.md", "a rule")
        self.assertEqual(detect_hooks(self.repo), [".claude/hookify.warn-rm.local.md"])

    def test_a_claude_code_mod_s_hooks_json_is_hooks(self):
        """A mod's hooks/hooks.json names its hooks module, "modules: an array with one path,
        relative to this file, to the hooks module, as in "modules": ["./register.js"]. Can
        also hold settings hooks under hooks." (code.claude.com/docs/en/plugins/mods/reference).
        The same file without a plugin manifest, or with an empty list, is no mod."""
        self._file("plugins/chart/.claude-plugin/plugin.json", '{"name": "chart"}')
        self._file("plugins/chart/hooks/hooks.json", '{"modules": ["./register.js"]}')
        self._file("plugins/chart/hooks/register.js", "export function register(on) {}\n")
        self._file("plugins/empty/.claude-plugin/plugin.json", '{"name": "empty"}')
        self._file("plugins/empty/hooks/hooks.json", '{"modules": []}')
        self._file("scripts/hooks/hooks.json", '{"modules": ["./register.js"]}')
        self.assertEqual(detect_hooks(self.repo), ["plugins/chart/hooks/hooks.json"])

    def test_the_hooks_of_a_codex_plugin_are_hooks(self):
        self._file(".agents/plugins/demo/.codex-plugin/plugin.json", '{"name": "demo"}')
        self._file(".agents/plugins/demo/hooks/hooks.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo), [".agents/plugins/demo/hooks/hooks.json"])

    def test_claude_settings_with_hooks(self):
        self._file(".claude/settings.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo), [".claude/settings.json"])

    def test_gemini_settings_with_hooks(self):
        self._file(".gemini/settings.json", self.GEMINI_HOOKS)
        self.assertEqual(detect_hooks(self.repo), [".gemini/settings.json"])

    def test_gemini_settings_with_an_event_of_claude_code_are_no_hooks(self):
        self._file(".gemini/settings.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo), [])

    def test_codex_hooks_json(self):
        self._file(".codex/hooks.json",
                   '{"description": "d", "hooks": {"SessionStart": [{"matcher": "startup", '
                   '"hooks": [{"type": "command", "command": "./start.sh"}]}]}}')
        self.assertEqual(detect_hooks(self.repo), [".codex/hooks.json"])

    def test_codex_hooks_json_without_hooks_key_is_not_hooks(self):
        self._file(".codex/hooks.json", '{"description": "d"}')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_a_codex_hooks_json_with_a_handler_in_place_of_a_matcher_group_is_no_hook(self):
        """nevalang/neva's `.codex/hooks.json`: Codex reads the handler as a matcher group
        without handlers, `handler_count` sums `group.hooks.len()` to 0, and Stop runs
        nothing."""
        self._file(".codex/hooks.json",
                   '{\n  "hooks": {\n    "Stop": [\n      {\n        "type": "command",\n'
                   '        "command": "$(git rev-parse --show-toplevel)/.codex/hooks/run-pre-commit-on-stop.sh",\n'
                   '        "timeout": 120,\n        "statusMessage": "Running pre-commit checks"\n'
                   '      }\n    ]\n  }\n}\n')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_claude_settings_with_an_event_that_holds_an_object_have_no_hooks(self):
        """itbench-hub/ITBench's `.claude/settings.json`, shortened. Claude Code 2.1.318 ignores
        an event that is no list of matchers: "hooks.UserPromptSubmit: must be an array of
        matchers; received an object; entry ignored"."""
        self._file(".claude/settings.json",
                   '{\n  "permissions": {"allow": ["Bash(find:*)"], "deny": []},\n'
                   '  "hooks": {\n    "UserPromptSubmit": {\n'
                   '      "command": "$CLAUDE_PROJECT_DIR/.claude/hooks/skill-activation-prompt.sh"\n'
                   '    }\n  },\n  "mcpServers": {"enableAllProjectMcpServers": true}\n}\n')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_cursor_hooks_json(self):
        self._file(".cursor/hooks.json",
                   '{"version": 1, "hooks": {"beforeShellExecution": [{"command": "x"}]}}')
        self.assertEqual(detect_hooks(self.repo), [".cursor/hooks.json"])

    def test_cursor_hooks_json_without_hooks_key_is_not_hooks(self):
        self._file(".cursor/hooks.json", '{"version": 1}')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_settings_without_hooks_are_not_hooks(self):
        self._file(".claude/settings.json", '{"model": "opus"}')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_local_settings_hooks_are_detected(self):
        # .claude/settings.local.json declares hooks the same way
        # .claude/settings.json does, and repositories do commit it.
        self._file(".claude/settings.local.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo), [".claude/settings.local.json"])

    def test_a_codex_config_toml_with_only_the_trust_state_of_hooks_is_no_hook(self):
        """vinta/hal-9000's `dotfiles/.codex/config.toml`, shortened: Codex writes the trust state
        of hooks that other files define under `[hooks.state."<hook>"]`, and `HooksToml` reads
        `state` beside the events."""
        self._file("dotfiles/.codex/config.toml",
                   '[projects."/usr/local/hal-9000"]\ntrust_level = "trusted"\n\n[hooks.state]\n\n'
                   '[hooks.state."ponytail@ponytail:hooks/claude-codex-hooks.json:session_start:0:0"]\n'
                   'trusted_hash = "sha256:defea43402fc8edba23234c694a1117cf80efc2da38968a40a1004cd9d63f725"\n')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_a_codex_config_toml_table_that_is_no_array_of_matcher_groups_is_no_hook(self):
        self._file(".codex/config.toml", '[hooks.pre_commit]\ncommand = "fmt"\n')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_codex_config_toml_hook_arrays_are_detected(self):
        # The form that the Codex hooks page documents: arrays of tables per event.
        self._file(".codex/config.toml",
                   '[[hooks.PreToolUse]]\nmatcher = "^Bash$"\n\n'
                   '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "x"\n')
        self.assertEqual(detect_hooks(self.repo), [".codex/config.toml"])

    def test_a_hooks_switch_is_not_a_hook(self):
        for text in ('[cases]\nhooks = true\n', 'codex_hooks = true\n'):
            with self.subTest(text=text):
                self._file(".codex/config.toml", text)
                self.assertEqual(detect_hooks(self.repo), [])

    def test_codex_config_toml_without_hooks_is_not_a_hook(self):
        self._file(".codex/config.toml", 'model = "o3"\n')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_github_hooks_json(self):
        self._file(".github/hooks/hooks.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo), [".github/hooks/hooks.json"])

    def test_github_hooks_content_is_inspected(self):
        # The detector holds the file to the same content test as every other hook file. A
        # JSON file in the directory is not a hook configuration by itself.
        self._file(".github/hooks/hooks.json", '{"unrelated": 1}')
        self.assertEqual(detect_hooks(self.repo), [])

    def test_github_hooks_ignores_non_json(self):
        self._file(".github/hooks/run.sh", "echo hi\n")
        self.assertEqual(detect_hooks(self.repo), [])

    def test_github_hooks_include_nested_files(self):
        self._file(".github/hooks/group/hooks.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo), [".github/hooks/group/hooks.json"])

    def test_hooks_are_recorded_under_the_name_on_disk(self):
        # The heuristics' spelling would name a path that exists in no tree. Its github_link
        # would return 404, and the archive would contain nothing there. detect_other, which
        # reads the names on disk, would also report the same file under its own name.
        self._file(".codex/Hooks.json", self.HOOKS)
        self._file(".gemini/Settings.json", self.GEMINI_HOOKS)
        self.assertEqual(detect_hooks(self.repo),
                         [".gemini/Settings.json", ".codex/Hooks.json"])

    @unittest.skipUnless(_CASE_SENSITIVE, "two spellings of one name cannot coexist here")
    def test_a_case_variant_does_not_shadow_the_file_the_heuristics_names(self):
        # The content test reads the file that it is given. Picking the variant would miss the
        # hooks, not only misspell the path.
        self._file(".claude/settings.json", self.HOOKS)
        self._file(".claude/Settings.json", '{"not": "hooks"}')
        self.assertEqual(detect_hooks(self.repo), [".claude/settings.json"])

    def test_settings_come_before_github_hooks(self):
        self._file(".github/hooks/hooks.json", self.HOOKS)
        self._file(".claude/settings.json", self.HOOKS)
        self.assertEqual(detect_hooks(self.repo),
                         [".claude/settings.json", ".github/hooks/hooks.json"])


# ---------------------------------------------------------------------------
# detect_rules  (Codex *.rules directly, Cursor *.mdc recursively)
# ---------------------------------------------------------------------------

class TestDetectRules(_RepoCase):

    def test_repo_without_rules(self):
        self.assertEqual(detect_rules(self.repo), [])

    def test_codex_rules_file(self):
        self._file(".codex/rules/style.rules")
        self.assertEqual(detect_rules(self.repo),
                         [{"rule": ".codex/rules/style.rules", "name": "style"}])

    def test_antigravity_s_rules_file_and_a_plugin_s_rules(self):
        """`.agents/rules.json` registers the rule files that the directory scan leaves out, and a
        plugin's rules are the Markdown files in its rules folder."""
        self._file(".agents/rules.json", '{"entries": [{"path": "rules"}]}')
        self._file(".agents/plugins/demo/plugin.json", '{"name": "demo"}')
        self._file(".agents/plugins/demo/rules/style.md", "---\ntrigger: always_on\n---\n")
        self.assertEqual(detect_rules(self.repo), [
            {"rule": ".agents/plugins/demo/rules/style.md", "name": "style"},
            {"rule": ".agents/rules.json", "name": "rules"}])

    def test_the_rule_files_antigravity_s_rules_json_names(self):
        """A rules.json names rule files outside .agents/rules and nested in it. The directory
        scan reads only the Markdown directly in .agents/rules."""
        self._file(".agents/rules.json", '{"entries": [{"path": "shared-rules"}, '
                                         '{"path": ".agents/rules", "include_only": ["web/react.md"]}]}')
        self._file(".agents/rules/style.md", "---\ntrigger: always_on\n---\n")
        self._file(".agents/rules/web/react.md", "---\ntrigger: glob\n---\n")
        self._file(".agents/rules/web/vue.md", "---\ntrigger: glob\n---\n")
        self._file("shared-rules/api.md", "---\ntrigger: manual\n---\n")
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         [".agents/rules/style.md", ".agents/rules.json",
                          ".agents/rules/web/react.md", "shared-rules/api.md"])

    def test_codex_ignores_other_extensions(self):
        self._file(".codex/rules/style.md")
        self.assertEqual(detect_rules(self.repo), [])

    def test_codex_does_not_recurse(self):
        self._file(".codex/rules/group/style.rules")
        self.assertEqual(detect_rules(self.repo), [])

    def test_cursor_rule_extension(self):
        self._file(".cursor/rules/style.mdc")
        found = [r["rule"] for r in detect_rules(self.repo)]
        self.assertIn(".cursor/rules/style.mdc", found)

    def test_cursor_ignores_markdown_without_frontmatter(self):
        # "Project rules must use the .mdc extension. A plain .md file in
        # .cursor/rules is ignored by the rules system because it has no
        # frontmatter to specify description, globs, and alwaysApply."
        for ext in (".md", ".mdx"):
            with self.subTest(ext):
                self._file(f".cursor/rules/style{ext}")
                found = [r["rule"] for r in detect_rules(self.repo)]
                self.assertNotIn(f".cursor/rules/style{ext}", found)

    def test_cursor_ignores_other_extensions(self):
        self._file(".cursor/rules/style.txt")
        self.assertEqual(detect_rules(self.repo), [])

    def test_cursor_recurses(self):
        self._file(".cursor/rules/group/style.mdc")
        self.assertEqual(detect_rules(self.repo)[0]["rule"],
                         ".cursor/rules/group/style.mdc")

    def test_claude_rule_file(self):
        self._file(".claude/rules/testing.md")
        self.assertEqual(detect_rules(self.repo),
                         [{"rule": ".claude/rules/testing.md", "name": "testing"}])

    def test_claude_recurses(self):
        # "All .md files are discovered recursively, so you can organize rules
        # into subdirectories like frontend/ or backend/"
        self._file(".claude/rules/frontend/react.md")
        self.assertEqual(detect_rules(self.repo)[0]["rule"],
                         ".claude/rules/frontend/react.md")

    def test_claude_ignores_other_extensions(self):
        self._file(".claude/rules/style.mdc")
        self._file(".claude/rules/notes.txt")
        self.assertEqual(detect_rules(self.repo), [])

    def test_an_output_style_is_a_rule(self):
        """Claude Code's page puts a project output style in `.claude/output-styles`, and an
        output style replaces the instructions that the tool gives itself for every response. No
        repository in the March sample has one, and this case is the only check of the
        heuristic."""
        self._file(".claude/output-styles/diagrams-first.md")
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         [".claude/output-styles/diagrams-first.md"])

    def test_an_output_style_below_the_repository_root(self):
        # The page loads them "from every .claude/output-styles/ between the working
        # directory and the repository root".
        self._file("packages/web/.claude/output-styles/concise.md")
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         ["packages/web/.claude/output-styles/concise.md"])

    def test_an_output_style_of_a_plugin_the_project_ships(self):
        self._file(".claude/plugins/mine/.claude-plugin/plugin.json", '{"name": "mine"}')
        self._file(".claude/plugins/mine/output-styles/tutor.md")
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         [".claude/plugins/mine/output-styles/tutor.md"])

    def test_a_non_markdown_file_in_an_output_styles_directory_is_not_a_rule(self):
        self._file(".claude/output-styles/notes.txt")
        self.assertEqual(detect_rules(self.repo), [])

    def test_name_is_the_file_stem(self):
        self._file(".cursor/rules/style.mdc")
        self.assertEqual(detect_rules(self.repo)[0]["name"], "style")

    def test_codex_rules_come_before_cursor_rules(self):
        self._file(".cursor/rules/a.mdc")
        self._file(".codex/rules/b.rules")
        self.assertEqual([r["name"] for r in detect_rules(self.repo)], ["b", "a"])


# ---------------------------------------------------------------------------
# detect_mcp  (by existence or by content)
# ---------------------------------------------------------------------------

class TestDetectMcp(_RepoCase):

    def test_repo_without_mcp(self):
        self.assertEqual(detect_mcp(self.repo), [])

    def test_root_mcp_json(self):
        self._file(".mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self.assertEqual(detect_mcp(self.repo), [".mcp.json"])

    def test_the_mcp_file_of_a_codex_plugin_is_mcp_when_it_declares_servers(self):
        """Codex reads the servers wrapped in mcpServers or as a plain map of servers."""
        for name in ("demo", "flat", "other"):
            self._file(f".agents/plugins/{name}/.codex-plugin/plugin.json", '{"name": "x"}')
        self._file(".agents/plugins/demo/.mcp.json", '{"mcpServers": {"x": {"command": "y"}}}')
        self._file(".agents/plugins/flat/.mcp.json", '{"x": {"command": "uvx", "args": ["y"]}}')
        self._file(".agents/plugins/other/.mcp.json", '{"name": "not mcp"}')
        self.assertEqual(detect_mcp(self.repo), [".agents/plugins/demo/.mcp.json",
                                                 ".agents/plugins/flat/.mcp.json"])

    def test_a_root_mcp_json_counts_only_with_a_server_that_a_tool_loads(self):
        # mondaycom/mcp's only server has a url and no type, which Claude Code skips: "A JSON
        # entry that has a url but no type is a configuration error ... Claude Code skips that
        # server". Visual Studio and VS Code keep the servers under "servers", and Copilot CLI
        # also reads the bare top-level form.
        cases = {"{}": False, '{"mcpServers": {}}': False,
                 '{"mcpServers": {"monday": {"url": "https://mcp.monday.com/mcp"}}}': True,
                 '{"mcpServers": {"x": {"command": "uvx"},}, // VS Code\n}': True,
                 '{"mcpServers": {"monday": {"type": "http", "url": "https://mcp.monday.com/mcp"}}}': True,
                 '{"servers": {"aspire": {"type": "stdio", "command": "dotnet"}}}': True,
                 '{"playwright": {"type": "local", "command": "npx"}}': True}
        for content, counts in cases.items():
            with self.subTest(content):
                self._file(".mcp.json", content)
                self.assertEqual(detect_mcp(self.repo), [".mcp.json"] if counts else [])

    def test_a_nested_mcp_json_counts_with_a_server(self):
        # Copilot CLI reads the .mcp.json of the directory that it works in (the nested: row).
        self._file("sub/.mcp.json", "{}")
        self.assertEqual(detect_mcp(self.repo), [])
        self._file("sub/.mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self.assertEqual(detect_mcp(self.repo), ["sub/.mcp.json"])

    def test_cursor_mcp_json_reads_a_server_with_a_url_alone(self):
        self._file(".cursor/mcp.json", '{"mcpServers": {"x": {"url": "https://example.invalid/mcp"}}}')
        self.assertEqual(detect_mcp(self.repo), [".cursor/mcp.json"])

    def test_the_copilot_mcp_files(self):
        for rel, content in ((".github/mcp.json", '{"mcpServers": {"x": {"type": "local", "command": "npx"}}}'),
                             (".vscode/mcp.json", '{"servers": {"x": {"type": "stdio", "command": "npx"}}}')):
            with self.subTest(rel):
                self._file(rel, content)
                self.assertIn(rel, detect_mcp(self.repo))

    def test_an_mcp_file_without_a_server_configures_nothing(self):
        # byteball/ocore's .vscode/mcp.json is {"servers": {}}.
        for content in ('{"servers": {}}', '{"servers": {"x": {"description": "no command or url"}}}'):
            with self.subTest(content):
                self._file(".vscode/mcp.json", content)
                self.assertEqual(detect_mcp(self.repo), [])

    def test_vs_code_and_visual_studio_read_a_server_without_a_type(self):
        # VS Code's sanitizeServer infers the type from the command or the url, and Visual
        # Studio's own example is {"servers": {"github": {"url": ...}}}, as microsoft/vs-mef keeps
        # it. VS Code reads the file with comments and trailing commas, and
        # microsoft/service-fabric-explorer's .vscode/mcp.json has a trailing comma.
        for content in ('{"servers": {"github": {"url": "https://api.githubcopilot.com/mcp/"}}, "inputs": []}',
                        '{"servers": {"sfx": {"command": "node",},},}',
                        '// local servers\n{"servers": {"x": {"command": "npx"}}}'):
            with self.subTest(content):
                self._file(".vscode/mcp.json", content)
                self.assertEqual(detect_mcp(self.repo), [".vscode/mcp.json"])

    def test_copilot_cli_reads_no_servers_key_in_github_mcp_json(self):
        # Copilot CLI's page on adding MCP servers lists "It uses the unsupported top-level key
        # `servers`" among the reasons that a server is not loaded, and OrchardCMS/OrchardCore's
        # .github/mcp.json keeps its server under that key.
        self._file(".github/mcp.json", '{"servers": {"orchardcore": {"type": "http", "url": "https://ai.orchardcore.net"}}}')
        self.assertEqual(detect_mcp(self.repo), [])
        self._file(".github/mcp.json", '{"mcpServers": {"orchardcore": {"type": "http", "url": "https://ai.orchardcore.net"}}}')
        self.assertEqual(detect_mcp(self.repo), [".github/mcp.json"])

    def test_vs_code_reads_only_the_servers_key_in_vscode_mcp_json(self):
        # lightspeedwp/tour-operator keeps its servers under "mcpServers", which VS Code does not
        # read in this file, and neither does it read the bare form there.
        self._file(".vscode/mcp.json", '{"mcpServers": {"github": {"command": "npx"}}}')
        self.assertEqual(detect_mcp(self.repo), [])
        self._file(".vscode/mcp.json", '{"github": {"command": "npx"}}')
        self.assertEqual(detect_mcp(self.repo), [])
        self._file(".vscode/mcp.json", '{"servers": {"github": {"command": "npx"}}}')
        self.assertEqual(detect_mcp(self.repo), [".vscode/mcp.json"])

    def test_only_the_root_mcp_json_is_read_with_trailing_commas(self):
        # VS Code reads the root's file with trailing commas, and Codex reads a plugin's file
        # with serde_json, which allows none.
        self._file(".agents/plugins/marketplace.json",
                   '{"name": "m", "plugins": [{"name": "sb", "source": {"source": "local", "path": "./plugins/sb"}}]}')
        self._file("plugins/sb/.codex-plugin/plugin.json", '{"name": "sb"}')
        self._file("plugins/sb/.mcp.json", '{"mcpServers": {"sb": {"command": "npx"},},}')
        self._file(".mcp.json", '{"mcpServers": {"a": {"command": "npx"},},}')
        found = detect_mcp(self.repo)
        self.assertIn(".mcp.json", found)
        self.assertNotIn("plugins/sb/.mcp.json", found)

    def test_a_plugin_mcp_file_without_a_server_configures_nothing(self):
        # storybookjs/storybook's Codex plugin keeps {"mcpServers": {}}.
        self._file(".agents/plugins/marketplace.json",
                   '{"name": "m", "plugins": [{"name": "sb", "source": {"source": "local", "path": "./plugins/sb"}}]}')
        self._file("plugins/sb/.codex-plugin/plugin.json", '{"name": "sb"}')
        self._file("plugins/sb/.mcp.json", '{"mcpServers": {}}')
        self.assertNotIn("plugins/sb/.mcp.json", detect_mcp(self.repo))
        self._file("plugins/sb/.mcp.json", '{"mcpServers": {"sb": {"command": "npx"}}}')
        self.assertIn("plugins/sb/.mcp.json", detect_mcp(self.repo))

    def test_gemini_settings_with_mcp_servers(self):
        self._file(".gemini/settings.json", '{"mcpServers": {"a": {}}}')
        self.assertEqual(detect_mcp(self.repo), [".gemini/settings.json"])

    def test_gemini_settings_with_an_empty_mcp_servers_configure_no_server(self):
        self._file(".gemini/settings.json", '{"mcpServers": {}}')
        self.assertEqual(detect_mcp(self.repo), [])

    def test_gemini_settings_without_mcp_servers(self):
        self._file(".gemini/settings.json", '{"theme": "dark"}')
        self.assertEqual(detect_mcp(self.repo), [])

    def test_gemini_settings_malformed_json(self):
        self._file(".gemini/settings.json", "{not json")
        self.assertEqual(detect_mcp(self.repo), [])

    def test_codex_config_with_named_server_table(self):
        self._file(".codex/config.toml", '[mcp_servers.github]\ncommand = "x"\n')
        self.assertEqual(detect_mcp(self.repo), [".codex/config.toml"])

    def test_codex_config_unnamed_table_is_not_enough(self):
        # The pattern requires [mcp_servers.<name>], not a bare table header.
        self._file(".codex/config.toml", '[mcp_servers]\n')
        self.assertEqual(detect_mcp(self.repo), [])

    def test_codex_config_without_mcp(self):
        self._file(".codex/config.toml", 'model = "gpt"\n')
        self.assertEqual(detect_mcp(self.repo), [])

    def test_codex_config_defines_a_server_with_dotted_keys_or_an_inline_table(self):
        # TOML 1.0.0: "Dotted keys create and define a table for each key part before the last
        # one". ever-co/ever-teams declares its server this way, and a regular expression
        # over [mcp_servers.<name>] headers would miss it.
        for text in ("mcp_servers.nx-mcp.command = 'npx'\nmcp_servers.nx-mcp.args = ['nx', 'mcp']\n",
                     'mcp_servers = { github = { command = "x" } }\n'):
            with self.subTest(text=text):
                self._file(".codex/config.toml", text)
                self.assertEqual(detect_mcp(self.repo), [".codex/config.toml"])

    def test_codex_config_with_a_commented_header_or_broken_toml_defines_no_server(self):
        for text in ('# [mcp_servers.github]\nmodel = "gpt"\n',
                     '[mcp_servers.github]\ncommand = \n'):
            with self.subTest(text=text):
                self._file(".codex/config.toml", text)
                self.assertEqual(detect_mcp(self.repo), [])

    def test_the_mcp_document_of_a_portable_plugin(self):
        self._file("plugins/demo/mcp.json", PORTABLE_MCP)
        self.assertEqual(detect_mcp(self.repo), ["plugins/demo/mcp.json"])

    def test_an_mcp_json_that_declares_no_such_schema_is_not_one(self):
        # `.cursor/mcp.json` and `.vscode/mcp.json` are rows of their own, and a bare
        # mcp.json is a configuration artifact only where the standard puts it.
        self._file("build/mcp.json", '{"mcpServers": {"demo": {"command": "x"}}}')
        self.assertEqual(detect_mcp(self.repo), [])

    def test_mcp_sources_are_recorded_under_the_name_on_disk(self):
        # The reason is the same as for detect_hooks. The heuristics' spelling names a path with
        # a github_link that returns 404, and the published archive does not contain the path.
        self._file(".MCP.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self._file(".gemini/Settings.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self._file(".codex/Config.toml", "[mcp_servers.github]\n")
        self.assertEqual(detect_mcp(self.repo),
                         [".MCP.json", ".gemini/Settings.json", ".codex/Config.toml"])

    def test_sources_are_returned_in_declared_order(self):
        self._file(".codex/config.toml", "[mcp_servers.github]\n")
        self._file(".gemini/settings.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self._file(".cursor/mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self._file(".mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self.assertEqual(detect_mcp(self.repo),
                         [".mcp.json", ".cursor/mcp.json",
                          ".gemini/settings.json", ".codex/config.toml"])


# ---------------------------------------------------------------------------
# detect_plugins  (a manifest at any depth, and the Gemini extension manifest)
# ---------------------------------------------------------------------------

PORTABLE_MANIFEST = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",'
                     ' "name": "demo"}')
PORTABLE_MCP = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
                ' "mcpServers": {"demo": {"command": "x"}}}')


class TestNamesFoundAtAnyDepth(_RepoCase):

    def test_a_name_in_another_case_is_no_row_of_its_mechanism(self):
        """The rows that name a file by its name alone, at any depth, such as
        `gemini-extension.json` and the portable `plugin.json` and `mcp.json`, compare the name
        in its case, as on a case-sensitive volume. A stat on the name would accept
        `Gemini-Extension.json` on a case-insensitive volume and report it at
        `gemini-extension.json`, a path that exists in no tree."""
        self._file("pkg/Gemini-Extension.json",
                   '{"name": "ext", "mcpServers": {"s": {"command": "x"}}}')
        self._file("portable/Plugin.json", PORTABLE_MANIFEST)
        self._file("portable/MCP.json", PORTABLE_MCP)
        self.assertEqual(detect_settings(self.repo), [])
        self.assertEqual(detect_mcp(self.repo), [])
        self.assertEqual(detect_plugins(self.repo), [])
        self.assertFalse(_mod.has_detected_mechanism(self.repo))
        # In other folders, because a case-insensitive volume opens the files above for the
        # names in this case.
        self._file("ext/gemini-extension.json",
                   '{"name": "ext", "mcpServers": {"s": {"command": "x"}}}')
        self._file("demo/plugin.json", PORTABLE_MANIFEST)
        self._file("demo/mcp.json", PORTABLE_MCP)
        found = [*detect_settings(self.repo), *detect_mcp(self.repo), *detect_plugins(self.repo)]
        self.assertEqual(sorted(set(found)), ["demo/mcp.json", "demo/plugin.json",
                                              "ext/gemini-extension.json"])
        self.assertTrue(all(_spelled_on_disk(self.repo, rel) for rel in found))


class TestDetectPlugins(_RepoCase):

    def test_repo_without_a_plugin(self):
        self.assertEqual(detect_plugins(self.repo), [])

    def test_the_root_manifest(self):
        self._file(".claude-plugin/plugin.json", '{"name": "demo"}')
        self.assertEqual(detect_plugins(self.repo), [".claude-plugin/plugin.json"])

    def test_manifest_content_is_not_inspected(self):
        # The path alone decides these rows. The portable Agent Plugins manifest, a bare
        # plugin.json, is the only manifest that the detector has to read.
        self._file(".claude-plugin/plugin.json", "{}")
        self.assertEqual(detect_plugins(self.repo), [".claude-plugin/plugin.json"])

    def test_a_manifest_beside_a_package(self):
        self._file("packages/web/.claude-plugin/plugin.json", '{"name": "web"}')
        self.assertEqual(detect_plugins(self.repo),
                         ["packages/web/.claude-plugin/plugin.json"])

    def test_a_manifest_below_a_mechanism_directory(self):
        # The sample uses this layout most: a plugin bundle under .claude/plugins/.
        # `_anchored` stops at a mechanism directory, and reading the heuristics' path through
        # `_anchored` would miss this case.
        self._file(".claude/plugins/seed/.claude-plugin/plugin.json", '{"name": "seed"}')
        self.assertEqual(detect_plugins(self.repo),
                         [".claude/plugins/seed/.claude-plugin/plugin.json"])

    def test_a_manifest_below_the_plugin_directory_itself(self):
        self._file(".claude-plugin/plugins/cache/.claude-plugin/plugin.json", '{"name": "c"}')
        self.assertEqual(detect_plugins(self.repo),
                         [".claude-plugin/plugins/cache/.claude-plugin/plugin.json"])

    def test_an_antigravity_plugin_s_manifest(self):
        """Antigravity reads a workspace's plugins from `.agents/plugins/<name>/`, and the
        plugin.json there is the manifest, whatever it contains beside its name."""
        self._file(".agents/plugins/pr-review/plugin.json", '{"name": "pr-review"}')
        self._file("plugins/other/plugin.json", '{"name": "other"}')
        self.assertEqual(detect_plugins(self.repo), [".agents/plugins/pr-review/plugin.json"])

    def test_an_antigravity_manifest_kept_in_a_folder(self):
        """Antigravity CLI 1.3.2: "keep the manifest in a .antigravity-plugin/ or .gemini-plugin/
        folder". The plugin is the directory above the folder. Its rules count, and a
        plugins.json entry finds it."""
        self._file(".agents/plugins/a/.antigravity-plugin/plugin.json", '{"name": "a"}')
        self._file(".agents/plugins/a/rules/style.md", "---\ntrigger: always_on\n---\n# Style\n")
        self._file(".agents/plugins/b/.gemini-plugin/plugin.json", '{"name": "b"}')
        self._file(".agents/plugins.json", '{"entries": [{"path": "shared"}]}')
        self._file("shared/c/.gemini-plugin/plugin.json", '{"name": "c"}')
        self._file("shared/c/rules/naming.md", "---\ntrigger: manual\n---\n# Naming\n")
        self.assertEqual(_mod.paths.plugin_roots(self.repo)["<antigravity-plugin>"],
                         [".agents/plugins/a", ".agents/plugins/b", "shared/c"])
        self.assertEqual(detect_plugins(self.repo), [
            ".agents/plugins.json",
            ".agents/plugins/a/.antigravity-plugin/plugin.json",
            ".agents/plugins/b/.gemini-plugin/plugin.json",
            "shared/c/.gemini-plugin/plugin.json"])
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         [".agents/plugins/a/rules/style.md", "shared/c/rules/naming.md"])

    def test_the_manifest_folder_of_an_antigravity_plugin_holds_no_component(self):
        """Antigravity CLI 1.4.0's `agy plugin validate` warns about "components placed inside
        .antigravity-plugin/ instead of the plugin root" (changelog, read 2026-10-19). CLI
        1.4.2, run offline on 2026-10-19 over such a plugin, reported "never loaded: components
        are read from the plugin root, not .antigravity-plugin" for the folder's `agents`,
        `rules` and `skills`, and the same for `.gemini-plugin`. It counted one component of
        each and listed only the agent at the root."""
        agent = "---\nname: {}\ndescription: d\n---\nbody\n"
        for plugin, folder in (("a", ".antigravity-plugin"), ("b", ".gemini-plugin")):
            base = f".agents/plugins/{plugin}"
            self._file(f"{base}/{folder}/plugin.json", f'{{"name": "{plugin}"}}')
            self._file(f"{base}/{folder}/rules/inside.md", "---\ntrigger: always_on\n---\n# In\n")
            self._file(f"{base}/{folder}/agents/inside.md", agent.format(f"{plugin}-inside"))
            self._file(f"{base}/{folder}/skills/inside/SKILL.md", agent.format("inside"))
            self._file(f"{base}/rules/root.md", "---\ntrigger: always_on\n---\n# Root\n")
            self._file(f"{base}/agents/root.md", agent.format(f"{plugin}-root"))
            self._file(f"{base}/skills/root/SKILL.md", agent.format("root"))
        self.assertEqual(sorted(r["rule"] for r in detect_rules(self.repo)),
                         [".agents/plugins/a/rules/root.md", ".agents/plugins/b/rules/root.md"])
        self.assertEqual(sorted(r["subagent"] for r in detect_subagents(self.repo)),
                         [".agents/plugins/a/agents/root.md", ".agents/plugins/b/agents/root.md"])
        self.assertEqual(sorted(r["skills.md"] for r in detect_skills(self.repo)),
                         [".agents/plugins/a/skills/root/SKILL.md",
                          ".agents/plugins/b/skills/root/SKILL.md"])

    def test_an_antigravity_plugin_s_agents_and_rules_need_their_front_matter(self):
        """`antigravity plugin validate` of Antigravity CLI 1.4.2, run offline on 2026-10-19,
        counted the agents of a plugin's `agents/` as the CLI lists the agents of
        `.agents/agents`: the Markdown files directly in the folder and each `<name>/agent.md`,
        with a non-empty name and description. It counted the rules of the plugin's `rules/` as
        the rules of `.agents/rules`: the Markdown files directly in the folder with a trigger
        that the rules page lists. It reported "the frontmatter has no name or description. The
        loader skips this agent" and "no valid trigger in the frontmatter, so the rule never
        applies". The detection step follows the CLI: It does not count every Markdown file
        directly in either folder, and it counts an agent with a name that starts with `_`."""
        agent = "---\nname: {}\ndescription: d\n---\nbody\n"
        rule = "---\ntrigger: {}\n---\nbody\n"
        self._file(".agents/plugins/p/plugin.json", '{"name": "p"}')
        for rel, text in {"README.md": agent.format("readme"), "x.md": agent.format("x"),
                          "_u.md": agent.format("u"), "named.md": "---\nname: named\n---\nbody\n",
                          "notes.md": "notes\n", "f/agent.md": agent.format("f"),
                          "f/notes.md": agent.format("fnotes"), "g/other.md": agent.format("g")}.items():
            self._file(f".agents/plugins/p/agents/{rel}", text)
        for rel, text in {"README.md": rule.format("always_on"), "t.md": rule.format("glob"),
                          "r.md": "Always do x.\n", "camel.md": rule.format("alwaysOn"),
                          "sub/n.md": rule.format("manual")}.items():
            self._file(f".agents/plugins/p/rules/{rel}", text)
        self.assertEqual(sorted(r["subagent"] for r in detect_subagents(self.repo)),
                         [".agents/plugins/p/agents/README.md", ".agents/plugins/p/agents/_u.md",
                          ".agents/plugins/p/agents/f/agent.md", ".agents/plugins/p/agents/x.md"])
        self.assertEqual(sorted(r["rule"] for r in detect_rules(self.repo)),
                         [".agents/plugins/p/rules/README.md", ".agents/plugins/p/rules/t.md"])

    def test_an_antigravity_plugin_s_agents_and_skills_files(self):
        """Antigravity CLI 1.3.3: "a root-level agents.json in plugins, so a plugin can declare
        its subagents the same way it declares skills and rules with skills.json and
        rules.json"."""
        self._file(".agents/plugins/a/plugin.json", '{"name": "a"}')
        self._file(".agents/plugins/a/agents.json", '{"entries": [{"path": ".agents/plugins/a/team"}]}')
        self._file(".agents/plugins/a/team/reviewer/agent.md",
                   "---\nname: reviewer\ndescription: Reviews.\n---\n")
        self._file(".agents/plugins/a/skills.json", '{"entries": [{"path": ".agents/plugins/a/extra"}]}')
        self._file(".agents/plugins/a/extra/lint/SKILL.md", "---\nname: lint\n---\n")
        self.assertIn(".agents/plugins/a/team/reviewer/agent.md",
                      [r["subagent"] for r in detect_subagents(self.repo)])
        # Any SKILL.md is a skill, and skills.json adds that Antigravity registers its folder.
        self.assertEqual(_mod.paths.registered_skill_folders(self.repo),
                         [".agents/plugins/a/extra/lint"])

    def test_a_cursor_marketplace_entry_is_a_plugin_without_its_own_manifest(self):
        """"For a marketplace entry with "source": "my-plugin": The parser looks for
        my-plugin/.cursor-plugin/plugin.json If found, the per-plugin manifest is merged with the
        marketplace entry (manifest values take precedence) Component discovery runs within the
        my-plugin/ directory, using manifest paths if specified or folder-based discovery as
        fallback" (cursor.com/docs/reference/plugins). `metadata.pluginRoot` prefixes every
        source, and an entry's component fields count where the plugin's manifest sets none."""
        self._file(".cursor-plugin/marketplace.json", json.dumps({
            "name": "m", "owner": {"name": "o"}, "metadata": {"pluginRoot": "plugins"},
            "plugins": [{"name": "lint", "source": "lint"},
                        {"name": "docs", "source": {"path": "./docs"}, "rules": "./guides"},
                        {"name": "ops", "source": "ops", "rules": "./ignored"},
                        {"name": "away", "source": "../../away"}]}))
        self._file("plugins/lint/rules/no-any.mdc", "x\n")
        self._file("plugins/docs/guides/style.mdc", "x\n")
        self._file("plugins/docs/rules/default.mdc", "x\n")
        self._file("plugins/ops/.cursor-plugin/plugin.json", '{"name": "ops", "rules": "./own"}')
        self._file("plugins/ops/own/deploy.mdc", "x\n")
        self._file("plugins/ops/ignored/skip.mdc", "x\n")
        self.assertEqual(_mod.paths.plugin_roots(self.repo)["<cursor-plugin>"],
                         ["plugins/docs", "plugins/lint", "plugins/ops"])
        self.assertEqual(sorted(r["rule"] for r in detect_rules(self.repo)),
                         ["plugins/docs/guides/style.mdc", "plugins/lint/rules/no-any.mdc",
                          "plugins/ops/own/deploy.mdc"])

    def test_copilot_s_marketplaces_outside_github(self):
        """The CLI checks "marketplace.json, .plugin/marketplace.json, .github/plugin/
        marketplace.json, or .claude-plugin/marketplace.json". It reads the bare name at the
        root only."""
        self._file("marketplace.json", '{"name": "m", "owner": {"name": "Owner"}, "plugins": []}')
        self._file(".plugin/marketplace.json", '{"name": "m", "owner": {"name": "Owner"}, "plugins": []}')
        self._file("docs/marketplace.json", '{"name": "m", "owner": {"name": "Owner"}, "plugins": []}')
        self.assertEqual(detect_plugins(self.repo),
                         [".plugin/marketplace.json", "marketplace.json"])

    def test_a_manifest_outside_a_plugin_directory_is_not_one(self):
        # A bare plugin.json is a manifest of some other program until its content
        # shows otherwise. These rows match on the directory name.
        self._file("plugin.json", '{"name": "something-else"}')
        self._file("src/plugin.json", "{}")
        self.assertEqual(detect_plugins(self.repo), [])

    def test_the_marketplace_catalog(self):
        self._file(".claude-plugin/marketplace.json", '{"plugins": []}')
        self.assertEqual(detect_plugins(self.repo), [".claude-plugin/marketplace.json"])

    def test_the_cursor_and_codex_manifests(self):
        for rel in (".cursor-plugin/plugin.json", ".codex-plugin/plugin.json"):
            with self.subTest(rel):
                self._file(rel, "{}")
                self.assertIn(rel, detect_plugins(self.repo))

    def test_the_codex_repo_scoped_marketplace(self):
        self._file(".agents/plugins/marketplace.json",
                   '{"name": "local", "plugins": [{"name": "p", "source": "./plugins/p"}]}')
        self.assertEqual(detect_plugins(self.repo), [".agents/plugins/marketplace.json"])

    def test_a_codex_marketplace_from_which_codex_installs_no_plugin_is_other(self):
        """UMEP-dev/SUEWS lists its only plugin with `"installation": "NOT_AVAILABLE"`, and
        `find_installable_marketplace_plugin` of openai/codex c9b4267c370a refuses to install it.
        The file gives Codex no plugin to load and is an `other` row of `.agents` (pp-0374 of the
        evaluation of 2026-11-04), as a file that does not parse as Codex's marketplace is."""
        suews = json.dumps({"name": "suews", "plugins": [
            {"name": "suews", "source": {"source": "local", "path": "./plugins/suews"},
             "policy": {"installation": "NOT_AVAILABLE", "authentication": "ON_INSTALL"},
             "category": "Productivity"}]})
        for name, text in (("an entry that is not available", suews),
                           ("no entry", '{"name": "local", "plugins": []}'),
                           ("no marketplace name",
                            '{"plugins": [{"name": "p", "source": "./p"}]}')):
            with self.subTest(marketplace=name):
                shutil.rmtree(self.repo)
                self.repo.mkdir()
                self._file(".agents/plugins/marketplace.json", text)
                self.assertEqual(detect_plugins(self.repo), [])
                self.assertIn(".agents/plugins/marketplace.json", _other_as_main_does(self.repo))

    def test_the_gemini_extension_manifest(self):
        self._file("gemini-extension.json", '{"name": "ext", "version": "1.0.0"}')
        self.assertEqual(detect_plugins(self.repo), ["gemini-extension.json"])

    def test_the_gemini_extension_manifest_at_any_depth(self):
        self._file("templates/init/gemini-extension.json", '{"name": "ext"}')
        self.assertEqual(detect_plugins(self.repo),
                         ["templates/init/gemini-extension.json"])

    def test_the_root_manifest_is_matched_ignoring_case(self):
        # The reason is the same as for detect_mcp. A path in the heuristics' spelling rather
        # than in the repository's has a github_link that returns 404.
        self._file(".claude-plugin/Plugin.json", "{}")
        self.assertEqual(detect_plugins(self.repo), [".claude-plugin/Plugin.json"])

    def test_every_manifest_of_a_repository_is_a_row(self):
        self._file(".claude-plugin/marketplace.json", '{"plugins": []}')
        self._file(".claude/plugins/a/.claude-plugin/plugin.json", '{"name": "a"}')
        self._file(".claude/plugins/b/.claude-plugin/plugin.json", '{"name": "b"}')
        self.assertEqual(detect_plugins(self.repo),
                         [".claude-plugin/marketplace.json",
                          ".claude/plugins/a/.claude-plugin/plugin.json",
                          ".claude/plugins/b/.claude-plugin/plugin.json"])

    def test_a_manifest_is_reported_once(self):
        # The pass through `_anchored` and the depth-first pass both find a root manifest.
        self._file(".claude-plugin/plugin.json", '{"name": "demo"}')
        self.assertEqual(len(detect_plugins(self.repo)), 1)

    def test_the_portable_manifest_declares_the_standard_it_targets(self):
        # The path cannot decide this one file. `plugin.json` in the plugin root is the Agent
        # Plugins layout, but programs that have nothing to do with agents use the same name.
        # The schema that the document declares is therefore the test.
        self._file("plugins/demo/plugin.json", PORTABLE_MANIFEST)
        self.assertEqual(detect_plugins(self.repo), ["plugins/demo/plugin.json"])

    def test_the_portable_manifest_at_the_repository_root(self):
        self._file("plugin.json", PORTABLE_MANIFEST)
        self.assertEqual(detect_plugins(self.repo), ["plugin.json"])

    def test_another_program_s_plugin_json_is_not_a_plugin(self):
        self._file("src/plugin.json", '{"id": "acme-panel", "type": "panel"}')
        self._file("web/plugin.json",
                   '{"$schema": "https://grafana.com/plugin.schema.json", "name": "x"}')
        self.assertEqual(detect_plugins(self.repo), [])

    def test_a_portable_manifest_without_a_name_is_not_one(self):
        self._file("plugin.json",
                   '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"}')
        self.assertEqual(detect_plugins(self.repo), [])

    def test_a_later_version_of_the_standard_still_matches(self):
        # The canonical identifier contains the version that the plugin targets, and the
        # heuristic tests the form rather than the version.
        self._file("plugin.json",
                   '{"$schema": "https://agent-plugins.org/schemas/2.1.0/plugin.schema.json",'
                   ' "name": "demo"}')
        self.assertEqual(detect_plugins(self.repo), ["plugin.json"])

    def test_a_vendor_manifest_and_a_portable_one_are_two_rows(self):
        # A plugin may contain both: Codex documents `.codex-plugin/plugin.json` as the
        # compatibility fallback beside the portable manifest.
        self._file(".codex-plugin/plugin.json", "{}")
        self._file("plugin.json", PORTABLE_MANIFEST)
        self.assertEqual(detect_plugins(self.repo),
                         [".codex-plugin/plugin.json", "plugin.json"])


class TestDetectLsp(_RepoCase):
    """The files from which a tool reads its language servers, one row per path: Copilot
    CLI's `.github/lsp.json` and the files that a Copilot CLI or Claude Code plugin
    contains."""

    LSP = '{"lspServers": {"rust-analyzer": {"command": "rust-analyzer", "fileExtensions": {".rs": "rust"}}}}'

    def test_repo_without_a_language_server(self):
        self.assertEqual(detect_lsp(self.repo), [])

    def test_copilot_s_repository_file(self):
        self._file(".github/lsp.json", self.LSP)
        self.assertEqual(detect_lsp(self.repo), [".github/lsp.json"])

    def test_the_file_of_each_plugin_kind(self):
        self._file("plugins/a/.github/plugin/plugin.json", '{"name": "a"}')
        self._file("plugins/a/lsp.json", self.LSP)
        self._file("plugins/b/.plugin/plugin.json", '{"name": "b"}')
        self._file("plugins/b/.github/lsp.json", self.LSP)
        self._file("plugins/c/.plugin/plugin.json", '{"name": "c"}')
        self._file("plugins/c/.lsp.json", self.LSP)
        self._file("plugins/d/plugin.json", PORTABLE_MANIFEST)
        self._file("plugins/d/com.github.copilot/lsp.json", self.LSP)
        self._file("plugins/e/.claude-plugin/plugin.json", '{"name": "e"}')
        self._file("plugins/e/.lsp.json", self.LSP)
        self.assertEqual(detect_lsp(self.repo),
                         ["plugins/a/lsp.json", "plugins/b/.github/lsp.json", "plugins/c/.lsp.json",
                          "plugins/d/com.github.copilot/lsp.json", "plugins/e/.lsp.json"])

    def test_the_file_the_plugin_reference_names_and_no_version_reads(self):
        """GitHub's plugin reference says to "create a lsp-config/servers.json file in the
        plugin directory". No version of Copilot CLI with code that could be read on
        2026-09-30, from 1.0.6 to 1.0.90-5, looks for that name. Each version reads .lsp.json,
        lsp.json and .github/lsp.json."""
        self._file("plugins/c/.github/plugin/plugin.json", '{"name": "c"}')
        self._file("plugins/c/lsp-config/servers.json", self.LSP)
        self.assertEqual(detect_lsp(self.repo), [])

    def test_a_file_beside_no_manifest_is_none(self):
        """Claude Code documents `.lsp.json` at a plugin's root only. The collection that
        started on 2026-09-27 found one at the root of a repository with no plugin."""
        self._file(".lsp.json", self.LSP)
        self._file("tools/lsp.json", self.LSP)
        self.assertEqual(detect_lsp(self.repo), [])

    def test_a_claude_plugin_s_file_is_not_a_copilot_one(self):
        """No row matches a `lsp.json` beside a Claude Code manifest. Claude Code reads
        `.lsp.json`, and the rows give a Claude Code plugin to Claude Code alone."""
        self._file(".claude-plugin/plugin.json", '{"name": "demo"}')
        self._file("lsp.json", self.LSP)
        self.assertEqual(detect_lsp(self.repo), [])

    def test_it_is_not_reported_as_other(self):
        self._file(".github/copilot-instructions.md", "# x\n")
        self._file(".github/lsp.json", self.LSP)
        self.assertNotIn(".github/lsp.json", _other_as_main_does(self.repo))


class TestWhatAManifestNames(_RepoCase):
    """The detectors report what a Claude Code plugin's manifest names beyond the default
    places under its mechanism, for Claude Code. The manifest of jasonxudeveloper/jengine
    names one folder for each skill under .claude-plugin/skills, and the files there are
    therefore skills rather than `other` rows."""

    def _plugin(self, **keys):
        self._file(".claude-plugin/plugin.json", json.dumps({"name": "demo", **keys}))

    def test_skill_folders_are_skills_not_other(self):
        self._plugin(skills=["./.claude-plugin/skills/jaction", "./lib/skills/"])
        self._file(".claude-plugin/skills/jaction/SKILL.md", "---\nname: jaction\n---\n")
        self._file(".claude-plugin/skills/jaction/references/api.md", "x\n")
        self._file("lib/skills/pool/SKILL.md", "x\n")
        rows = {r["skills.md"]: r for r in detect_skills(self.repo)}
        self.assertEqual(sorted(rows), [".claude-plugin/skills/jaction/SKILL.md",
                                        "lib/skills/pool/SKILL.md"])
        self.assertEqual((rows[".claude-plugin/skills/jaction/SKILL.md"]["name"],
                          rows[".claude-plugin/skills/jaction/SKILL.md"]["references"]),
                         ("jaction", "TRUE"))
        other = _other_as_main_does(self.repo)
        self.assertFalse([p for p in other if "jaction" in p], other)

    def test_the_other_components(self):
        self._plugin(hooks="./cfg/hooks.json", mcpServers="./cfg/mcp.json",
                     lspServers="./cfg/lsp.json", agents=["./team/reviewer.md"],
                     outputStyles="./styles/", commands=["./cmds/status.md"],
                     workflows=["./flows/audit.js"])
        self._file("cfg/hooks.json",
                   '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}')
        self._file("cfg/mcp.json", '{"mcpServers": {"x": {"command": "y"}}}')
        self._file("cfg/lsp.json", '{"lspServers": {}}')
        self._file("team/reviewer.md")
        self._file("styles/terse.md")
        self._file("cmds/status.md")
        self._file("flows/audit.js")
        self.assertIn("cfg/hooks.json", detect_hooks(self.repo))
        self.assertIn("cfg/mcp.json", detect_mcp(self.repo))
        self.assertEqual(detect_lsp(self.repo), ["cfg/lsp.json"])
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)], ["team/reviewer.md"])
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)], ["styles/terse.md"])
        self.assertEqual(sorted(r["skills.md"] for r in detect_skills(self.repo)),
                         ["cmds/status.md", "flows/audit.js"])
        # The repository with the files, because a hooks file gives the plugin's tool only where
        # that tool loads a hook from it.
        _files, tools = derive_flags([], [], [], [], [], ["cfg/hooks.json"], [], [], [],
                                     plugin_parts=_mod.plugin_part_tools(self.repo), repo=self.repo)
        self.assertTrue(tools["claude"])

    def test_content_is_held_to_the_default_row_s_test(self):
        """A hooks file with no hooks, or an MCP file with no servers, counts for neither
        mechanism, as at the default place."""
        self._plugin(hooks="./cfg/hooks.json", mcpServers="./cfg/mcp.json")
        self._file("cfg/hooks.json", '{"hooks": {}}')
        self._file("cfg/mcp.json", '{"servers": {}}')
        self.assertEqual(detect_hooks(self.repo), [])
        self.assertNotIn("cfg/mcp.json", detect_mcp(self.repo))

    def test_a_copilot_manifest_s_commands_and_agents(self):
        """A string under Copilot CLI's `commands` or `agents` key replaces the default folder (the
        section "The files of a plugin that Copilot CLI reads" of docs/heuristics.md), and the CLI
        reads the Markdown files directly in the folder that it names. A README without agent front
        matter stays documentation."""
        self._file("plugins/p/.plugin/plugin.json",
                   json.dumps({"name": "p", "commands": "./cmds", "agents": "./team"}))
        self._file("plugins/p/cmds/deploy.md", "Deploy.\n")
        self._file("plugins/p/commands/old.md", "Old.\n")
        self._file("plugins/p/team/reviewer.md", "Review.\n")
        self._file("plugins/p/team/README.md", "About the team.\n")
        self.assertEqual(detect_commands(self.repo),
                         [{"command": "plugins/p/cmds/deploy.md", "name": "deploy"}])
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         ["plugins/p/team/reviewer.md"])

    def test_a_path_outside_the_plugin_is_not_reported(self):
        self._file("plugins/p/.claude-plugin/plugin.json",
                   json.dumps({"name": "p", "hooks": "../hooks/hooks.json"}))
        self._file("plugins/hooks/hooks.json", '{"hooks": {"Stop": [{"command": "true"}]}}')
        self.assertEqual(detect_hooks(self.repo), [])


class TestEveryManifestKeyIsRead(_RepoCase):
    """One case per `manifest_field` row of the heuristics: a manifest that sets the key, and the
    configuration artifact that the detector reports for it. `test_heuristics` finds a row by its
    path. A `manifest_field` row shares its path with its default row by definition, and every such
    row therefore passes there whether or not a detector reads what the key names. The expectations
    follow the vendors' readings in the sections "The places that a plugin's manifest names" and
    "The files of a plugin that Copilot CLI reads" of docs/heuristics.md."""

    HOOKS = '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}'
    MCP = '{"mcpServers": {"x": {"command": "y"}}}'
    RULE = "---\ndescription: Use tabs\n---\nTabs.\n"
    CLAUDE = ".claude-plugin/plugin.json"
    COPILOT = ".plugin/plugin.json"

    # Maps (tool, mechanism, the row's path) to (the manifest, its keys, the files, the path
    # that the detector reports).
    CASES = {
        ("claude", "skills", "<claude-plugin>/skills"):
            (CLAUDE, {"skills": ["./extra"]}, {"extra/b/SKILL.md": "x\n"}, "extra/b/SKILL.md"),
        ("claude", "skills", "<claude-plugin>/commands"):
            (CLAUDE, {"commands": ["./cmds/status.md"]}, {"cmds/status.md": "x\n"}, "cmds/status.md"),
        ("claude", "subagents", "<claude-plugin>/agents"):
            (CLAUDE, {"agents": ["./team/reviewer.md"]}, {"team/reviewer.md": "x\n"},
             "team/reviewer.md"),
        ("claude", "hooks", "<claude-plugin>/hooks/hooks.json"):
            (CLAUDE, {"hooks": "./cfg/hooks.json"}, {"cfg/hooks.json": HOOKS}, "cfg/hooks.json"),
        ("claude", "mcp", "<claude-plugin>/.mcp.json"):
            (CLAUDE, {"mcpServers": "./cfg/mcp.json"}, {"cfg/mcp.json": MCP}, "cfg/mcp.json"),
        ("claude", "lsp", "<claude-plugin>/.lsp.json"):
            (CLAUDE, {"lspServers": "./cfg/lsp.json"}, {"cfg/lsp.json": "{}"}, "cfg/lsp.json"),
        ("claude", "rules", "<claude-plugin>/output-styles"):
            (CLAUDE, {"outputStyles": "./styles/"}, {"styles/terse.md": "x\n"}, "styles/terse.md"),
        ("claude", "skills", "<claude-plugin>/workflows"):
            (CLAUDE, {"workflows": ["./flows/audit.js"]}, {"flows/audit.js": "x\n"}, "flows/audit.js"),
        ("copilot", "skills", "<copilot-plugin>/skills"):
            (COPILOT, {"skills": "./extra"}, {"extra/s/SKILL.md": "x\n"}, "extra/s/SKILL.md"),
        ("copilot", "subagents", "<copilot-plugin>/agents"):
            (COPILOT, {"agents": "./team"}, {"team/reviewer.md": "x\n"}, "team/reviewer.md"),
        ("copilot", "commands", "<copilot-plugin>/commands"):
            (COPILOT, {"commands": "./cmds"}, {"cmds/deploy.md": "x\n"}, "cmds/deploy.md"),
        ("copilot", "rules", "<copilot-plugin>/rules"):
            (COPILOT, {"rules": "./my-rules"}, {"my-rules/tabs.mdc": RULE}, "my-rules/tabs.mdc"),
        ("copilot", "hooks", "<copilot-plugin>/hooks.json|hooks/hooks.json"):
            (COPILOT, {"hooks": "./cfg/h.json"}, {"cfg/h.json": HOOKS}, "cfg/h.json"),
        ("copilot", "mcp", "<copilot-plugin>/.mcp.json|.github/mcp.json"):
            (COPILOT, {"mcpServers": "./cfg/m.json"}, {"cfg/m.json": MCP}, "cfg/m.json"),
        ("copilot", "lsp", "<copilot-plugin>/.lsp.json|lsp.json|.github/lsp.json"):
            (COPILOT, {"lspServers": "./cfg/l.json"}, {"cfg/l.json": '{"lspServers": {}}'}, "cfg/l.json"),
    }

    CASES.update({
        ('cursor', 'skills', '<cursor-plugin>/skills'):
            ('.cursor-plugin/plugin.json', {'skills':'./custom'}, {'custom/s/SKILL.md':'Skill.'}, 'custom/s/SKILL.md'),
        ('cursor', 'rules', '<cursor-plugin>/rules'):
            ('.cursor-plugin/plugin.json', {'rules':'./custom'}, {'custom/rule.markdown':'Rule.'}, 'custom/rule.markdown'),
        ('cursor', 'subagents', '<cursor-plugin>/agents'):
            ('.cursor-plugin/plugin.json', {'agents':'./custom'}, {'custom/agent.mdc':'Agent.'}, 'custom/agent.mdc'),
        ('cursor', 'commands', '<cursor-plugin>/commands'):
            ('.cursor-plugin/plugin.json', {'commands':'./custom'}, {'custom/command.txt':'Command.'}, 'custom/command.txt'),
        ('cursor', 'hooks', '<cursor-plugin>/hooks/hooks.json'):
            ('.cursor-plugin/plugin.json', {'hooks':'./custom.json'}, {'custom.json':HOOKS}, 'custom.json'),
        ('cursor', 'mcp', '<cursor-plugin>/mcp.json'):
            ('.cursor-plugin/plugin.json', {'mcpServers':'./custom.json'}, {'custom.json':MCP}, 'custom.json'),
        ('cursor', 'mcp', '<cursor-plugin>/.mcp.json'):
            ('.cursor-plugin/plugin.json', {'mcpServers':'./custom.json'}, {'custom.json':MCP}, 'custom.json'),
    })

    CASES.update({
        ("codex", "skills", "<codex-plugin>/skills"):
            (".codex-plugin/plugin.json", {"skills": "./selected"},
             {"selected/build/SKILL.md": "---\ndescription: Build\n---\nBuild\n"},
             "selected/build/SKILL.md"),
        ("codex", "mcp", "<codex-plugin>/.mcp.json"):
            (".codex-plugin/plugin.json", {"mcpServers": "./selected.json"},
             {"selected.json": MCP}, "selected.json"),
        ("codex", "hooks", "<codex-plugin>/hooks/hooks.json"):
            (".codex-plugin/plugin.json", {"hooks": "./selected.json"},
             {"selected.json": HOOKS}, "selected.json"),
        ("codex", "hooks", "<agent-plugin>/hooks/hooks.json"):
            ("plugin.json", {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                             "extensions": {"com.openai": {"hooks": "./selected.json"}}},
             {"selected.json": HOOKS}, "selected.json"),
        # Antigravity resolves the entries as those of the plugin's skills.json, from the plugin
        # root.
        ("gemini", "skills", "<antigravity-plugin>/skills"):
            (".agents/plugins/p/plugin.json", {"skills": "./extra"},
             {".agents/plugins/p/extra/s/SKILL.md": "---\ndescription: S.\n---\n"},
             ".agents/plugins/p/extra/s/SKILL.md"),
        # An apps file counts by the apps that it maps, the same test as for the default file.
        ("codex", "mcp", "<codex-plugin>/.app.json"):
            (".codex-plugin/plugin.json", {"apps": "./config/apps.json"},
             {"config/apps.json": '{"apps": {"forms": {"id": "plugin_asdk_app_1"}}}'},
             "config/apps.json"),
    })

    def test_every_manifest_field_row_has_a_case(self):
        rows = {(r["tool"], r["mechanism"], r["path"])
                for r in heuristics.load_config_path_patterns()
                if r["match_mode"].startswith("manifest_field:")}
        self.assertEqual(sorted(rows), sorted(self.CASES))

    def _reported(self, mechanism: str) -> list:
        if mechanism == "skills":
            return [r["skills.md"] for r in detect_skills(self.repo)]
        if mechanism == "subagents":
            return [r["subagent"] for r in detect_subagents(self.repo)]
        if mechanism == "commands":
            return [r["command"] for r in detect_commands(self.repo)]
        if mechanism == "rules":
            return [r["rule"] for r in detect_rules(self.repo)]
        return {"hooks": detect_hooks, "mcp": detect_mcp, "lsp": detect_lsp}[mechanism](self.repo)

    def test_what_each_key_names_is_reported_under_its_mechanism(self):
        for (tool, mechanism, path), (manifest, keys, files, artifact) in self.CASES.items():
            with self.subTest(tool=tool, path=path):
                shutil.rmtree(self.repo, ignore_errors=True)
                self.repo.mkdir(parents=True)
                self._file(manifest, json.dumps({"name": "p", **keys}))
                for rel, text in files.items():
                    self._file(rel, text)
                _mod._plugin_components.cache_clear()
                _mod._nested_anchors.cache_clear()
                self.assertIn(artifact, self._reported(mechanism))


class TestACopilotPluginRuleNeedsADescription(_RepoCase):
    """Copilot CLI loads a plugin's `.mdc` rule only where its YAML front matter has a
    non-empty `description` string. Each case is a file that Copilot CLI 1.0.63's rule loader
    read on 2026-09-30, with what the loader returned: the rule, or the quoted warning."""

    LOADED = {
        "plain": "---\ndescription: Style\n---\nx\n",
        "with-globs": '---\ndescription: Python\nglobs: "*.py"\n---\nx\n',
        "quoted-key": '---\n"description": quoted key\n---\nx\n',
        "single-quoted": "---\ndescription: 'single'\n---\nx\n",
        "quoted-number": '---\ndescription: "3"\n---\nx\n',
        "yes": "---\ndescription: yes\n---\nx\n",
        "date": "---\ndescription: 2026-09-30\n---\nx\n",
        "version": "---\ndescription: 1.2.3\n---\nx\n",
        "comment": "---\ndescription: text # comment\n---\nx\n",
        "folded": "---\ndescription: >\n  folded\n  text\n---\nx\n",
        "continued": "---\ndescription:\n  continued plain\n---\nx\n",
        "indented": "---\n  description: indented\n---\nx\n",
        "crlf": "---\r\ndescription: crlf\r\n---\r\nx\r\n",
        "bom": "﻿---\ndescription: bom\n---\nx\n",
        "no-body": "---\ndescription: ok\n---\n",
    }
    REFUSED = {
        "no-front-matter": "# heading\n\nbody text\n",           # missing or malformed
        "leading-blank": "\n---\ndescription: x\n---\nx\n",      # missing or malformed
        "unterminated": "---\ndescription: x\nx\n",              # missing or malformed
        "closed-by-dots": "---\ndescription: x\n...\nx\n",       # missing or malformed
        "always-only": "---\nalwaysApply: true\n---\nx\n",       # description: Required
        "empty-front-matter": "---\n---\nx\n",                   # description: Required
        "nested": "---\nmeta:\n  description: x\n---\nx\n",      # description: Required
        "duplicate": "---\ndescription: a\ndescription: b\n---\nx\n",  # duplicate entry
        "null-value": "---\ndescription:\n---\nx\n",             # Expected string
        "tilde": "---\ndescription: ~\n---\nx\n",                # Expected string
        "comment-only": "---\ndescription: # only comment\n---\nx\n",  # Expected string
        "boolean": "---\ndescription: False\n---\nx\n",          # Expected string
        "integer": "---\ndescription: -1\n---\nx\n",             # Expected string
        "float": "---\ndescription: .5\n---\nx\n",              # Expected string
        "exponent": "---\ndescription: 1e3\n---\nx\n",           # Expected string
        "hex": "---\ndescription: 0x1F\n---\nx\n",               # Expected string
        "list": "---\ndescription: [a, b]\n---\nx\n",            # Expected string
        "dash-list": "---\ndescription:\n  - item\n---\nx\n",    # Expected string
        "mapping": "---\ndescription: {a: 1}\n---\nx\n",         # Expected string
        "empty-string": '---\ndescription: ""\n---\nx\n',        # at least 1 character
        "empty-block": "---\ndescription: |\n---\nx\n",          # at least 1 character
    }

    def test_quoted_descriptions_accept_comments_and_continuations(self):
        self._file("plugins/p/.plugin/plugin.json", '{"name": "p"}')
        values = {
            "comment": '\"Style\" # comment',
            "continued": '\"Style\n  and formatting\"',
            "escaped": '\"Say \\\"hello\\\"\" # comment',
            "single": "'It''s style' # comment",
        }
        for name, value in values.items():
            self._file(f"plugins/p/rules/{name}.mdc", f"---\ndescription: {value}\n---\nText\n")
        self.assertEqual({r["name"] for r in detect_rules(self.repo)}, set(values))

    def test_invalid_description_values_are_rejected(self):
        self._file("plugins/p/.plugin/plugin.json", '{"name": "p"}')
        values = ['\"\" # empty', '\"Style\" extra', '\"not closed',
                  '\n  name: Style', '\n  - Style']
        for index, value in enumerate(values):
            self._file(f"plugins/p/rules/{index}.mdc", f"---\ndescription: {value}\n---\nText\n")
        self.assertEqual(detect_rules(self.repo), [])

    def test_what_copilot_cli_loaded(self):
        self._file("plugins/p/.plugin/plugin.json", '{"name": "p"}')
        for name, text in {**self.LOADED, **self.REFUSED}.items():
            (self.repo / "plugins/p/rules").mkdir(parents=True, exist_ok=True)
            (self.repo / f"plugins/p/rules/{name}.mdc").write_bytes(text.encode("utf-8"))
        self.assertEqual(sorted(r["name"] for r in detect_rules(self.repo)), sorted(self.LOADED))

    def test_a_rule_the_manifest_names_needs_one_too(self):
        self._file("plugins/p/.plugin/plugin.json", '{"name": "p", "rules": "./shared"}')
        self._file("plugins/p/shared/kept.mdc", self.LOADED["plain"])
        self._file("plugins/p/shared/bare.mdc", self.REFUSED["always-only"])
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)], ["plugins/p/shared/kept.mdc"])


# ---------------------------------------------------------------------------
# The flags from detector output  (ownership by path prefix or basename)
# ---------------------------------------------------------------------------

class TestDeriveFlags(unittest.TestCase):

    def _tools_of_unclassified(self, path) -> set:
        """Return the tools that `artifact_tools` gives an unclassified file at `path`: the
        owner of the directory that contains the file, as its `other.csv` row records it."""
        return _mod.artifact_tools(_NO_FILES, [("other", path)])[("other", path)]

    def _flags(self, cf=(), s=(), sk=(), sa=(), cmd=(), hk=(), rl=(), mc=(), ot=(),
               markers=(), workflows=(), pl=()):
        """Call derive_flags with plain paths, wrapped in dicts where a detector returns
        dicts."""
        return derive_flags(
            list(cf), list(s),
            [{"skills.md": p} for p in sk],
            [{"subagent": p} for p in sa],
            [{"command": p} for p in cmd],
            list(hk),
            [{"rule": p} for p in rl],
            list(mc), list(ot), list(markers), list(pl), list(workflows),
        )

    def test_a_workflow_is_owned_by_the_action_it_runs_not_by_github(self):
        """Every agent workflow is in `.github/workflows`, which `tool_for_path` reads as
        Copilot's. Without the action as the evidence, a Claude workflow would set the Copilot
        flag and not the Claude flag, which is wrong for both tools."""
        path = ".github/workflows/nightly.yml"
        self.assertEqual(_mod.tool_for_path(path), "copilot")
        _files, tools = self._flags(s=[path], workflows=[(path, "claude")])
        self.assertTrue(tools["claude"])
        self.assertFalse(tools["copilot"])

    def test_the_path_of_a_workflow_names_none_of_the_actions_tools(self):
        """The `workflow_uses:` rows share `.github/workflows`. A lookup by path would return
        the tool of the last of these rows for the whole directory and flag a workflow that
        runs the Claude or the Codex action as that tool's."""
        for name in ("claude-issue-triage.yml", "review.yml", "codex.yml"):
            with self.subTest(name=name):
                self.assertNotIn(_mod.tool_for_path(f".github/workflows/{name}"),
                                 {"claude", "codex", "gemini"})

    def test_a_workflow_matched_by_name_still_belongs_to_its_tool(self):
        """`copilot-setup-steps.yml` is a path row, and no action runs in it. It is attributed
        by its path, as every other path is."""
        _files, tools = self._flags(s=[".github/workflows/copilot-setup-steps.yml"])
        self.assertTrue(tools["copilot"])
        self.assertFalse(tools["claude"])

    # ── file flags ───────────────────────────────────────────────────────────

    def test_no_artifacts_sets_nothing(self):
        file_flags, tool_flags = self._flags()
        self.assertFalse(any(file_flags.values()))
        self.assertFalse(any(tool_flags.values()))

    def test_claude_md_file_flag(self):
        self.assertTrue(self._flags(cf=["CLAUDE.md"])[0]["claude.md"])

    def test_file_flags_match_by_basename_at_any_depth(self):
        self.assertTrue(self._flags(cf=["docs/nested/CLAUDE.md"])[0]["claude.md"])

    def test_gemini_md_file_flag(self):
        self.assertTrue(self._flags(cf=["GEMINI.md"])[0]["gemini.md"])

    def test_agents_md_file_flag(self):
        self.assertTrue(self._flags(cf=["AGENTS.md"])[0]["agents.md"])

    def test_agents_override_sets_agents_md(self):
        self.assertTrue(self._flags(cf=["AGENTS.override.md"])[0]["agents.md"])

    def test_cursorrules_file_flag(self):
        self.assertTrue(self._flags(cf=[".cursorrules"])[0]["cursorrules"])

    def test_copilot_instructions_file_flag(self):
        self.assertTrue(
            self._flags(cf=[".github/copilot-instructions.md"])[0]["copilot-instructions"])

    def test_github_instructions_sets_copilot_instructions(self):
        self.assertTrue(
            self._flags(cf=[".github/instructions/python.md"])[0]["copilot-instructions"])

    def test_other_github_markdown_does_not_set_copilot_instructions(self):
        self.assertFalse(
            self._flags(cf=[".github/CLAUDE.md"])[0]["copilot-instructions"])

    # ── tool flags from context files ────────────────────────────────────────

    def test_claude_md_sets_claude(self):
        self.assertTrue(self._flags(cf=["CLAUDE.md"])[1]["claude"])

    def test_gemini_md_sets_gemini(self):
        self.assertTrue(self._flags(cf=["GEMINI.md"])[1]["gemini"])

    def test_cursorrules_sets_cursor(self):
        self.assertTrue(self._flags(cf=[".cursorrules"])[1]["cursor"])

    def test_agents_md_sets_no_tool(self):
        # AGENTS.md belongs to no specific tool, and the tool paths leave it out on purpose.
        # The exclusion makes no_specific_tool possible.
        file_flags, tool_flags = self._flags(cf=["AGENTS.md"])
        self.assertTrue(file_flags["agents.md"])
        self.assertFalse(any(tool_flags.values()))

    def test_agents_override_sets_codex(self):
        flags = self._flags(cf=["AGENTS.override.md"])[1]
        self.assertEqual({tool for tool, present in flags.items() if present}, {"codex"})

    def test_copilot_instructions_sets_copilot(self):
        self.assertTrue(self._flags(cf=[".github/copilot-instructions.md"])[1]["copilot"])

    # ── tool flags from every other mechanism ────────────────────────────────

    def test_settings_set_their_tool(self):
        self.assertTrue(self._flags(s=[".cursor/cli.json"])[1]["cursor"])

    def test_skills_set_their_tool(self):
        self.assertTrue(self._flags(sk=[".github/skills/pdf/SKILL.md"])[1]["copilot"])

    def test_subagents_set_their_tool(self):
        self.assertTrue(self._flags(sa=[".cursor/agents/reviewer.md"])[1]["cursor"])

    def test_commands_set_their_tool(self):
        self.assertTrue(self._flags(cmd=[".claude/commands/review.md"])[1]["claude"])

    def test_hooks_set_their_tool(self):
        self.assertTrue(self._flags(hk=[".gemini/settings.json"])[1]["gemini"])

    def test_rules_set_their_tool(self):
        self.assertTrue(self._flags(rl=[".codex/rules/style.rules"])[1]["codex"])

    def test_an_unclassified_file_sets_no_tool_flag(self):
        # A file in .codex/ that no detector reads is an unclassified file. It keeps its tool in
        # other.csv, and because the release does not publish other.csv, it sets no flag.
        self.assertFalse(self._flags(ot=[".codex/notes.txt"])[1]["codex"])

    # ── ownership by path ────────────────────────────────────────────────────

    def test_root_mcp_json_counts_as_no_tool(self):
        # Several tools read .mcp.json. As with AGENTS.md, the file is evidence of MCP use but
        # not of a particular tool.
        flags = self._flags(mc=[".mcp.json"])[1]
        self.assertFalse(any(flags.values()))

    def test_cursor_mcp_counts_as_cursor(self):
        self.assertTrue(self._flags(mc=[".cursor/mcp.json"])[1]["cursor"])

    def test_copilot_instructions_flag_ignores_case(self):
        # Detection matches the name case-insensitively and records the name on disk. The flag
        # has to compare the same way, or a repository gets a context-files.csv row and a FALSE
        # flag in the same run.
        for path in (".github/COPILOT-INSTRUCTIONS.md",
                     ".github/Copilot-Instructions.md",
                     ".github/Instructions/python.instructions.md"):
            with self.subTest(path):
                self.assertTrue(self._flags(cf=[path])[0]["copilot-instructions"])

    def test_copilot_instructions_flag_is_only_copilot_files(self):
        # The detector matches .cursor/BUGBOT.md and .gemini/styleguide.md the same way, and
        # they belong to other tools.
        for path in (".cursor/BUGBOT.md", ".gemini/styleguide.md"):
            with self.subTest(path):
                self.assertFalse(self._flags(cf=[path])[0]["copilot-instructions"])

    def test_a_resolved_marker_attributes_its_tool(self):
        # The detectors report the configuration artifacts behind a link at their real path.
        # Without the marker, the linking tool would get no credit for them.
        flags = self._flags(sk=[".claude/skills/pdf/SKILL.md"],
                            markers=[(".cursor/skills", ".claude/skills")])[1]
        self.assertEqual({tool for tool, present in flags.items() if present},
                         {"claude", "cursor"})

    def test_a_marker_with_nothing_behind_it_attributes_no_tool(self):
        # A link to a directory without a configuration artifact gives its tool nothing to
        # read. No row could name the tool, and a flag would disagree with the rows.
        flags = self._flags(cf=["AGENTS.md"], markers=[(".cursor/skills", "docs/skills")])[1]
        self.assertFalse(any(flags.values()))

    def test_claude_context_file_under_github_is_not_copilot(self):
        # .github contains far more than Copilot configuration. A CLAUDE.md
        # there is a Claude context file, and a bare .github/
        # prefix match for Copilot would set both flags.
        flags = self._flags(cf=[".github/CLAUDE.md"])[1]
        self.assertTrue(flags["claude"])
        self.assertFalse(flags["copilot"])

    def test_anything_under_github_counts_as_copilot(self):
        for path in (".github/skills/pdf/SKILL.md", ".github/agents/a.md",
                     ".github/hooks/hooks.json"):
            with self.subTest(path):
                self.assertIn("copilot", self._tools_of_unclassified(path))

    def test_prefix_match_requires_the_separator(self):
        # ".claudex/" must not be read as ".claude/".
        self.assertNotIn("claude", self._tools_of_unclassified(".claudex/notes.txt"))

    def test_a_mechanism_directory_below_the_root_sets_its_tool(self):
        # The manifest arrives through `plugins`, as main passes it, not through `ot`. main
        # never puts a manifest in `ot`, and through `ot` the case would pass even if the
        # flags left the plugins out.
        self.assertTrue(self._flags(s=["packages/web/.claude/settings.json"])[1]["claude"])
        self.assertTrue(self._flags(pl=["plugins/core/.cursor-plugin/plugin.json"])[1]["cursor"])

    def test_plugin_manifests_set_their_tool(self):
        # Each manifest sets its own tool and no other. The portable manifest belongs to
        # the standard, not to one tool.
        expected = {
            ".claude-plugin/plugin.json": {"claude"},
            ".claude-plugin/marketplace.json": {"claude"},
            ".cursor-plugin/plugin.json": {"cursor"},
            ".cursor-plugin/marketplace.json": {"cursor"},
            ".codex-plugin/plugin.json": {"codex"},
            ".agents/plugins/marketplace.json": {"codex"},
            "gemini-extension.json": {"gemini"},
            "plugins/demo/plugin.json": set(),
            ".github/plugin/plugin.json": {"copilot"},
            ".github/plugin/marketplace.json": {"copilot"},
            ".plugin/plugin.json": {"copilot"},
            ".plugin/marketplace.json": {"copilot"},

        }
        for path, tools in expected.items():
            with self.subTest(path):
                flags = self._flags(pl=[path])[1]
                self.assertEqual({t for t, on in flags.items() if on}, tools)

    def test_a_plugin_sibling_name_does_not_outrank_the_directory_that_contains_it(self):
        # README.md, LICENSE and package.json are plugin_sibling rows owned by Claude, and
        # they are longer strings than `.cursor`. Matched anywhere in a path, they would
        # attribute a .cursor/README.md to Claude instead of Cursor.
        tools = self._tools_of_unclassified(".cursor/README.md")
        self.assertIn("cursor", tools)
        self.assertNotIn("claude", tools)

    def test_nested_ignore_files_keep_their_tool(self):
        self.assertIn("cursor", self._tools_of_unclassified("packages/web/.cursorignore"))
        self.assertEqual(self._tools_of_unclassified("packages/x/.github/workflows/ci.yml"), set())

    def test_multiple_tools_can_be_set_at_once(self):
        _, tool_flags = self._flags(cf=["CLAUDE.md"], s=[".gemini/settings.json"],
                                    rl=[".codex/rules/x.rules"])
        self.assertEqual({k for k, v in tool_flags.items() if v},
                         {"claude", "gemini", "codex"})

    def test_each_row_credits_its_tool(self):
        """One path per row of the heuristics that the cases above do not name, with the tool
        written out. Retagging a row then fails this case and not only the checksum. The
        settings, skills directories, context files and plugin manifests have cases of their
        own. `None` marks a row that several tools read and that credits none of them."""
        expected = {
            # subagents
            ".claude/agent-memory/reviewer/MEMORY.md": "claude",
            ".claude/agent-memory-local/reviewer/MEMORY.md": "claude",
            ".claude/agents/reviewer.md": "claude",
            ".github/agents/reviewer.md": "copilot",
            ".github/chatmodes/plan.chatmode.md": "copilot",
            ".cursor/agents/reviewer.md": "cursor",
            ".gemini/agents/reviewer.md": "gemini",
            ".codex/agents/reviewer.toml": "codex",
            # skills without a directory of their own
            ".claude/commands/deploy.md": "claude",
            ".claude/workflows/release.js": "claude",
            # commands
            ".gemini/commands/deploy.toml": "gemini",
            ".cursor/commands/deploy.md": "cursor",
            ".github/prompts/deploy.prompt.md": "copilot",
            # hooks
            ".codex/hooks.json": "codex",
            ".cursor/hooks.json": "cursor",
            ".github/hooks/guard.json": "copilot",
            ".codex/config.toml": "codex",
            # rules
            ".codex/rules/default.rules": "codex",
            ".cursor/rules/style.mdc": "cursor",
            ".claude/rules/style.md": "claude",
            ".claude/output-styles/tutor.md": "claude",
            # mcp
            ".mcp.json": None,
            ".cursor/mcp.json": "cursor",
            ".github/mcp.json": "copilot",
            ".vscode/mcp.json": "copilot",
            "plugins/ship/mcp.json": None,
            # other
            ".claude/notes.txt": "claude",
            ".gemini/notes.txt": "gemini",
            ".cursor/notes.txt": "cursor",
            ".codex/notes.txt": "codex",
            ".github/notes.txt": "copilot",
            ".cursor-plugin/README.md": "cursor",
            ".codex-plugin/README.md": "codex",
            ".claude-plugin/README.md": "claude",
            ".agents/notes.txt": None,
            ".geminiignore": "gemini",
            ".cursorignore": "cursor",
            ".cursorindexingignore": "cursor",
            ".aiexclude": "gemini",
            ".agents/rules/style.md": "gemini",
            ".agent/rules/style.md": "gemini",
            ".agents/agents/reviewer.md": "gemini",
            ".agents/hooks.json": "gemini",
            ".agents/mcp_config.json": "gemini",
            ".agent/skills/pdf/SKILL.md": "gemini",
            ".agents/workflows/deploy.md": "gemini",
            ".agent/workflows/release.md": "gemini",
            ".agents/rules.json": "gemini",

        }
        for path, tool in expected.items():
            with self.subTest(path):
                self.assertEqual(_mod.tool_for_path(path), tool)


class TestTheGithubOfADirectoryBelowTheRoot(_RepoCase):
    """Copilot CLI reads `.github/copilot-instructions.md`, `.github/instructions`,
    `.github/agents`, `.github/skills`, `.github/lsp.json` and `.github/mcp.json` in its working
    directory and in each directory between it and the root, as Copilot CLI 1.0.101 did on test
    workspaces on 2026-10-21. It reads the rest of a `.github` at the root alone, as GitHub
    does."""

    LSP = '{"lspServers": {"ts": {"command": "x", "fileExtensions": {".ts": "ts"}}}}\n'

    def test_the_places_that_copilot_cli_reads_are_detected_as_copilot_s(self):
        self._file("pkg/.github/copilot-instructions.md")
        self._file("pkg/.github/instructions/deep/a.instructions.md")
        self._file("pkg/.github/instructions/notes.md")
        self._file("pkg/.github/agents/review.agent.md", "---\ndescription: Review\n---\nx\n")
        self._file("pkg/.github/agents/team/plain.md", "---\ndescription: Plain\n---\nx\n")
        self._file("pkg/.github/agents/notes.md", "No front matter.\n")
        self._file("pkg/.github/skills/group/release/SKILL.md")
        self._file("pkg/.github/lsp.json", self.LSP)
        self._file("pkg/.github/mcp.json", '{"mcpServers": {"gh": {"command": "gh"}}}\n')
        cf = detect_context_files(self.repo)
        sa = detect_subagents(self.repo)
        sk = detect_skills(self.repo)
        lsp = detect_lsp(self.repo)
        mcp = detect_mcp(self.repo)
        self.assertEqual(mcp, ["pkg/.github/mcp.json"])
        self.assertEqual(sorted(cf), ["pkg/.github/copilot-instructions.md",
                                      "pkg/.github/instructions/deep/a.instructions.md"])
        self.assertEqual(sorted(r["subagent"] for r in sa),
                         ["pkg/.github/agents/review.agent.md", "pkg/.github/agents/team/plain.md"])
        self.assertEqual([r["skills.md"] for r in sk], ["pkg/.github/skills/group/release/SKILL.md"])
        self.assertEqual(lsp, ["pkg/.github/lsp.json"])
        file_flags, tool_flags = derive_flags(cf, [], sk, sa, [], [], [], mcp, [], lsp=lsp,
                                              repo=self.repo)
        self.assertTrue(file_flags["copilot-instructions"])
        self.assertEqual({tool for tool, on in tool_flags.items() if on}, {"copilot"})
        self.assertEqual(_other_as_main_does(self.repo),
                         ["pkg/.github/agents/notes.md", "pkg/.github/instructions/notes.md"])

    def test_the_rest_of_a_github_below_the_root_is_not_read(self):
        self._file("pkg/.github/prompts/fix.prompt.md")
        self._file("pkg/.github/chatmodes/plan.chatmode.md")
        self._file("pkg/.github/copilot/settings.json", '{"hooks": {"sessionStart": [{"command": "true"}]}}\n')
        self._file("pkg/.github/hooks/h.json", '{"hooks": {"sessionStart": [{"command": "true"}]}}\n')
        self._file("pkg/.github/workflows/copilot-setup-steps.yml", "jobs: {}\n")
        self.assertEqual(detect_commands(self.repo), [])
        self.assertEqual(detect_subagents(self.repo), [])
        self.assertEqual(detect_settings(self.repo), [])
        self.assertEqual(detect_hooks(self.repo), [])
        self.assertEqual(_other_as_main_does(self.repo), [])

    def test_no_place_is_read_in_a_tool_directory_or_a_vendored_tree(self):
        """A `.github` inside `.claude` is a part of `.claude`, which the rows of `.claude` read.
        A `.github` in `node_modules` belongs to another project."""
        self._file(".claude/templates/.github/copilot-instructions.md")
        self._file("node_modules/pkg/.github/copilot-instructions.md")
        self._file(".github/copilot-instructions.md")
        self.assertEqual(detect_context_files(self.repo), [".github/copilot-instructions.md"])
        self.assertEqual(_mod.tool_for_path(".claude/templates/.github/agents/x.md"), "claude")

    def test_the_owner_of_a_file_in_a_place_below_the_root(self):
        self.assertEqual(_mod.tool_for_path("pkg/.github/agents/notes.md"), "copilot")
        self.assertEqual(_mod.tool_for_path("pkg/.github/instructions/notes.md"), "copilot")
        self.assertIsNone(_mod.tool_for_path("pkg/.github/workflows/ci.yml"))
        self.assertIsNone(_mod.tool_for_path("pkg/.github/prompts/fix.prompt.md"))


def _mcp_tools(repo):
    """Return {path: tools} for the MCP rows of a repository, as the detection step attributes
    them."""
    tools = _mod.artifact_tools(repo, [("mcp", rel) for rel in detect_mcp(repo)],
                                plugin_parts=_mod.plugin_part_tools(repo))
    return {rel: found for (_mechanism, rel), found in tools.items()}


class TestTheMcpJsonOfADirectoryBelowTheRoot(_RepoCase):
    """Copilot CLI reads ".mcp.json (in any directory from your working directory up to the
    repository root)" (add-mcp-servers), in the format with `mcpServers` and in the bare format,
    as Copilot CLI 1.0.101 did on test workspaces on 2026-10-21. Several tools read the root's
    file."""

    def test_a_package_s_file_is_copilot_s_and_the_root_s_no_tool_s(self):
        self._file(".mcp.json", '{"mcpServers": {"root": {"command": "root"}}}\n')
        self._file("pkg/.mcp.json", '{"web": {"command": "web"}}\n')
        self._file("node_modules/dep/.mcp.json", '{"mcpServers": {}}\n')
        self.assertEqual(detect_mcp(self.repo), [".mcp.json", "pkg/.mcp.json"])
        self.assertEqual(_mcp_tools(self.repo), {".mcp.json": set(), "pkg/.mcp.json": {"copilot"}})

    def test_the_file_of_a_tool_directory_below_the_root_is_that_tool_s(self):
        """A `.mcp.json` inside `.claude` is a part of `.claude`, as a `.github` there is."""
        self._file("pkg/.claude/.mcp.json", '{"mcpServers": {"x": {"command": "x"}}}\n')
        self.assertEqual(detect_mcp(self.repo), [])
        self.assertEqual(_mod.tool_for_path("pkg/.claude/.mcp.json"), "claude")

    def test_a_plugin_s_file_is_the_plugin_tool_s_and_copilot_s(self):
        """Claude Code reads a plugin's `.mcp.json` as the plugin's MCP servers. Copilot CLI
        1.0.101 read the same file with the plugin's directory, or a directory below it, as its
        working directory. OpenAgentsInc/openagents's plugins/claude/.mcp.json is such a
        file."""
        self._file("plugins/claude/.claude-plugin/plugin.json", '{"name": "demo"}')
        self._file("plugins/claude/.mcp.json", '{"mcpServers": {"demo": {"command": "demo"}}}\n')
        self.assertEqual(_mcp_tools(self.repo), {"plugins/claude/.mcp.json": {"claude", "copilot"}})


class TestTheAppsOfACodexPlugin(_RepoCase):
    """Codex reads the `.app.json` of a plugin with a legacy manifest, an app for each entry of
    its apps object (codex-rs/core-plugins/src/loader.rs and codex-rs/connectors/src/plugin_config.rs
    of openai/codex 3f31c0a040ca), and keeps the apps of a portable plugin inactive."""

    APPS = '{"apps": {"forms": {"id": "plugin_asdk_app_1"}}}'

    def test_an_apps_file_with_an_app_is_codex_s_mcp(self):
        self._file("plugins/a/.codex-plugin/plugin.json", '{"name": "a"}')
        self._file("plugins/a/.app.json", self.APPS)
        mcp = detect_mcp(self.repo)
        self.assertEqual(mcp, ["plugins/a/.app.json"])
        tools = _mod.artifact_tools(self.repo, [("mcp", "plugins/a/.app.json")],
                                    plugin_parts=_mod.plugin_part_tools(self.repo))
        self.assertEqual(tools[("mcp", "plugins/a/.app.json")], {"codex"})

    def test_the_empty_apps_object_of_the_scaffold_declares_no_app(self):
        self._file("plugins/a/.codex-plugin/plugin.json", '{"name": "a"}')
        self._file("plugins/a/.app.json", '{"apps": {}}')
        self.assertEqual(detect_mcp(self.repo), [])

    def test_a_named_apps_file_replaces_the_default_one(self):
        self._file("plugins/a/.codex-plugin/plugin.json", '{"name": "a", "apps": "./cfg/apps.json"}')
        self._file("plugins/a/.app.json", self.APPS)
        self._file("plugins/a/cfg/apps.json", self.APPS)
        self.assertEqual(detect_mcp(self.repo), ["plugins/a/cfg/apps.json"])

    def test_no_apps_file_is_read_beside_an_agent_plugins_manifest(self):
        self._file("plugins/opnform/plugin.json",
                   '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", '
                   '"name": "opnform"}')
        self._file("plugins/opnform/.codex-plugin/plugin.json",
                   '{"name": "opnform", "apps": "./.app.json"}')
        self._file("plugins/opnform/.app.json", self.APPS)
        self.assertEqual(detect_mcp(self.repo), [])


class TestTheMetadataOfACodexSkill(_RepoCase):
    """Codex reads `agents/openai.yaml` as the metadata of the skill with its SKILL.md beside the
    `agents/` folder (`discover_skill_metadata` in codex-rs/ext/skills of openai/codex
    3f31c0a040ca). The file needs no row of its own, because the skill's row claims its
    folder."""

    def test_the_metadata_is_a_part_of_its_skill_and_of_no_skill_without_one(self):
        self._file(".agents/skills/deps/SKILL.md", "---\nname: deps\ndescription: d\n---\nx\n")
        self._file(".agents/skills/deps/agents/openai.yaml", "interface:\n  display_name: Deps\n")
        self._file(".agents/skills/orphan/agents/openai.yaml", "interface:\n  display_name: X\n")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         [".agents/skills/deps/SKILL.md"])
        self.assertEqual(_other_as_main_does(self.repo),
                         [".agents/skills/orphan/agents/openai.yaml"])


class TestTheFrontMatterThatCursorParses(_RepoCase):
    """Cursor CLI 2026.09.28-64d2043 parses a SKILL.md with gray-matter 4.0.3, which removes a
    byte order mark, takes a first line of `---` with no fourth `-`, an optional language
    after it, and ends the front matter at the first later line that starts with `---` or at
    the end of the file, and parses YAML with js-yaml 3.14.1. The expected values of the
    first test are the results of gray-matter 4.0.3 with js-yaml 3.14.1 on each text, with the
    engines js and javascript replaced as Cursor CLI replaces them, run on 2026-11-07."""

    def test_the_front_matter_is_split_off_and_parsed_as_gray_matter_does(self):
        for text, expected in (("\ufeff---\nname: a\ndescription: x: y\n---\nbody\n", False),
                               ("----\nname: a: b\n", True),
                               ("---yaml\nname: a: b\n---\nbody\n", False),
                               ("---yaml\nname: a\n---\nbody\n", True),
                               ("---\nname: a\n--- end\nx: y: z\n", True),
                               ('---\r\nname: a\r\ndescription: "x: y"\r\n---\r\nbody\r\n', True),
                               ("---\r\ndescription: x: y\r\n---\r\nbody\r\n", False),
                               ("---\nname: a\n", True),
                               ("---\nname: a\nx: y: z", False),
                               ("# Title\n\nx: y: z\n", True),
                               ("---\n# a: b: c\n---\nbody\n", True),
                               ("---\nname: a\nname: b\n---\n", False),
                               ("---YAML\nname: a: b\n---\nbody\n", False),
                               ("---Yml\nname: a\n---\nbody\n", True),
                               ('---json\n{"name": "a", "name": "b"}\n---\nbody\n', True),
                               ("---JS\nname: a: b\n---\nbody\n", True),
                               ("---- x\nname: a: b\n---\n", True)):
            with self.subTest(text=text):
                path = self._file(".cursor/skills/s/SKILL.md", text)
                self.assertIs(_mod._cursor_skill_loads(self.repo, path), expected)

    def test_a_front_matter_in_another_language_is_kept(self):
        """gray-matter 4.0.3 refuses a language without an engine, 'gray-matter engine "toml"
        is not registered', and Cursor CLI then drops the skill. Cursor's staff states the
        failure of the IDE only for a front matter that is not valid YAML, and the test keeps
        such a file even where its front matter would not parse as YAML."""
        path = self._file(".cursor/skills/s/SKILL.md", '---toml\n[skill]\nname = "a"\n---\nbody\n')
        self.assertTrue(_mod._cursor_skill_loads(self.repo, path))


# ---------------------------------------------------------------------------
# _collect_all_spec_files  (the candidate set "other" is drawn from)
# ---------------------------------------------------------------------------

class TestCollectAllSpecFiles(_RepoCase):

    def test_empty_repo(self):
        self.assertEqual(_collect_all_spec_files(self.repo), set())

    def test_each_spec_dir_is_scanned_recursively(self):
        for sd in (".claude", ".gemini", ".cursor", ".codex"):
            with self.subTest(sd):
                self._file(f"{sd}/deep/notes.txt")
                self.assertIn(f"{sd}/deep/notes.txt", _collect_all_spec_files(self.repo))

    def test_a_spec_directory_below_the_root_is_scanned(self):
        self._file("packages/web/.claude/notes.txt")
        self._file("plugins/core/.claude-plugin/plugin.json", "{}")
        self.assertEqual(_collect_all_spec_files(self.repo),
                         {"packages/web/.claude/notes.txt",
                          "plugins/core/.claude-plugin/plugin.json"})

    def test_a_spec_directory_in_a_vendored_tree_is_not_scanned(self):
        self._file("node_modules/pkg/.claude/notes.txt")
        self.assertEqual(_collect_all_spec_files(self.repo), set())

    def test_a_github_directory_below_the_root_is_scanned_at_the_places_copilot_cli_reads(self):
        """GitHub reads the root's `.github` alone, and Copilot CLI reads a few places of the
        `.github` of each directory where it works. Only these places of a package's `.github`
        add files to `other`."""
        self._file("packages/web/.github/copilot-instructions.md")
        self._file("packages/web/.github/agents/notes.txt")
        self._file("packages/web/.github/workflows/ci.yml")
        self._file("packages/web/.github/prompts/fix.prompt.md")
        self.assertEqual(_collect_all_spec_files(self.repo),
                         {"packages/web/.github/copilot-instructions.md",
                          "packages/web/.github/agents/notes.txt"})

    def test_root_ignore_files_are_included(self):
        for name in (".geminiignore", ".cursorignore", ".aiexclude"):
            with self.subTest(name):
                self._file(name)
                self.assertIn(name, _collect_all_spec_files(self.repo))

    def test_nested_ignore_files_are_inventoried(self):
        names = (".cursorignore", ".cursorindexingignore", ".geminiignore", ".aiexclude")
        for name in names:
            self._file(f"sub/{name}")
        self.assertEqual(_collect_all_spec_files(self.repo), {f"sub/{name}" for name in names})

    def test_github_is_a_spec_dir(self):
        # The collected file cleanup prunes .github to the Copilot paths. What remains is
        # configuration and belongs in `other`, as in any other spec directory.
        self._file(".github/copilot-instructions.md")
        self.assertEqual(_collect_all_spec_files(self.repo),
                         {".github/copilot-instructions.md"})

    def test_claude_plugin_is_a_spec_dir(self):
        self._file(".claude-plugin/plugin.json")
        self.assertEqual(_collect_all_spec_files(self.repo),
                         {".claude-plugin/plugin.json"})

    def test_root_context_file_is_not_a_candidate(self):
        self._file("CLAUDE.md")
        self.assertEqual(_collect_all_spec_files(self.repo), set())

    def test_directories_are_not_included(self):
        self._dir(".claude/skills")
        self.assertEqual(_collect_all_spec_files(self.repo), set())

    def test_an_agent_directory_is_a_customization_root_only_with_a_place_of_antigravity(self):
        """Antigravity recognizes `_agents/` and `_agent/` beside `.agents/`, and the places that
        it reads in such a folder are `rules/`, `skills/`, `agents/`, `plugins/`, `hooks.json`,
        `mcp_config.json`, the legacy `workflows/` and the discovery manifests
        (https://antigravity.google/docs/customization-discovery, read 2026-11-05). A private
        Python package has the same name, such as NVIDIA-NeMo/Curator's
        `nemo_curator/stages/audio/_agent/`, and so does a documentation partial, such as
        reactive/data-client's `docs/core/_agents/`. Without such a place, the directory is no
        customization root, and its files are no `other` rows."""
        self._file("nemo_curator/stages/audio/_agent/_roles.py",
                   '# Copyright (c) 2026, NVIDIA CORPORATION.\n'
                   '"""Semantic-role mapping for agent-ready audio stages."""\n')
        self._file("tests/stages/audio/_agent/test_roles.py", "def test_roles():\n    pass\n")
        self._file("docs/core/_agents/browser-cursor.md", "---\ntitle: Cursor Browser Setup\n---\n")
        self._file("packages/web/_agent/rules/style.md", "---\ntrigger: always_on\n---\nUse tabs.\n")
        self._file("packages/web/_agent/notes.md", "Notes.\n")
        self._file("_agents/skills/create-microbenchmark/SKILL.md",
                   "---\nname: create-microbenchmark\ndescription: Writes a benchmark.\n---\n")
        self._file("_agents/README.md", "Skills of the project.\n")
        self.assertEqual(_other_as_main_does(self.repo),
                         ["_agents/README.md", "packages/web/_agent/notes.md"])
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         ["packages/web/_agent/rules/style.md"])
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         ["_agents/skills/create-microbenchmark/SKILL.md"])


# ---------------------------------------------------------------------------
# _collect_known_files  (context files count only inside a spec directory)
# ---------------------------------------------------------------------------

class TestCollectKnownFiles(_RepoCase):

    def _known(self, context_files):
        return _collect_known_files(self.repo, [], context_files, [], [], [], [], [], [])

    def test_context_file_inside_a_spec_dir_is_known(self):
        self.assertIn(".claude/CLAUDE.md", self._known([".claude/CLAUDE.md"]))

    def test_root_context_file_is_not_known(self):
        self.assertEqual(self._known(["CLAUDE.md"]), set())

    def test_github_context_file_is_known(self):
        # Unclaimed files in .github become `other` rows, and a detected context file there
        # belongs in the known set. Otherwise, it would be reported as `other` although it
        # already has a row in context-files.csv.
        self.assertEqual(self._known([".github/copilot-instructions.md"]),
                         {".github/copilot-instructions.md"})

    def test_root_context_file_is_still_not_known(self):
        # A CLAUDE.md at the repository root is outside every spec directory. It is never a
        # candidate for `other`, and the known set can leave it out.
        self.assertEqual(self._known(["CLAUDE.md"]), set())


# ---------------------------------------------------------------------------
# detect_other  (spec-directory files no other detector claimed)
# ---------------------------------------------------------------------------

class TestMechanismsBelowTheRoot(_RepoCase):
    """The tools read configuration from the directory that they work in, and a package of a
    monorepo gets the same detection as the root. The exceptions are `.github`, which GitHub
    reads only at the root, the `root_file` rows, and anything in a vendored tree, which
    belongs to the vendored project."""

    def test_a_nested_skill(self):
        self._file("packages/web/.claude/skills/deploy/SKILL.md")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         ["packages/web/.claude/skills/deploy/SKILL.md"])

    def test_a_tool_directory_below_the_root_s_github_is_read(self):
        """ydb-platform/ydb keeps a skill beside an AGENTS.md and a CLAUDE.md in
        .github/scripts/utils/analytics/datalens/, a directory that a session can start in, and
        "Codex scans .agents/skills in every directory from your current working directory up to
        the repository root" (https://learn.chatgpt.com/docs/build-skills, read 2026-11-05). The
        heuristics of the root's .github name the places of Copilot and of the workflows, and
        none of them covers that directory, so the file is a skill and no unclassified file. A
        .github below the root stops the search for tool directories, as it did."""
        place = ".github/scripts/utils/analytics/datalens"
        skill = f"{place}/.agents/skills/ydb-ci-metrics-datalens/SKILL.md"
        self._file(f"{place}/AGENTS.md", "# DataLens dashboards\n")
        self._file(skill, "---\nname: ydb-ci-metrics-datalens\ndescription: Build the DataLens "
                          "dashboards of the CI metrics\n---\nSteps.\n")
        self._file("ydb/apps/dstool/.agents/skills/ydb-nbs-dbg-like-load/SKILL.md",
                   "---\nname: ydb-nbs-dbg-like-load\ndescription: Load\n---\nSteps.\n")
        self._file("pkg/.github/scripts/.agents/skills/copied/SKILL.md",
                   "---\nname: copied\ndescription: A copy\n---\nSteps.\n")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         [skill, "ydb/apps/dstool/.agents/skills/ydb-nbs-dbg-like-load/SKILL.md"])
        self.assertNotIn(skill, _other_as_main_does(self.repo))

    def test_a_nested_subagent_and_its_memory(self):
        self._file("apps/api/.claude/agents/reviewer.md", AGENT)
        self._file("apps/api/.claude/agent-memory/reviewer/MEMORY.md")
        rows = detect_subagents(self.repo)
        self.assertEqual([r["subagent"] for r in rows], ["apps/api/.claude/agents/reviewer.md"])
        self.assertEqual(rows[0]["memory"], "TRUE")

    def test_memory_beside_the_root_does_not_count_for_a_nested_subagent(self):
        self._file("apps/api/.claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory/reviewer/MEMORY.md")
        self.assertEqual(detect_subagents(self.repo)[0]["memory"], "FALSE")

    def test_a_nested_rules_directory(self):
        self._file("packages/web/.cursor/rules/style.mdc")
        self.assertEqual([r["rule"] for r in detect_rules(self.repo)],
                         ["packages/web/.cursor/rules/style.mdc"])

    def test_a_nested_hooks_file(self):
        self._file("packages/web/.cursor/hooks.json", '{"hooks": {"a": 1}}')
        self.assertEqual(detect_hooks(self.repo), ["packages/web/.cursor/hooks.json"])

    def test_a_nested_mcp_file(self):
        self._file("packages/web/.cursor/mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self.assertEqual(detect_mcp(self.repo), ["packages/web/.cursor/mcp.json"])

    def test_a_nested_root_only_file_is_not_detected(self):
        # OpenCode reads opencode.json at the repository root. A .mcp.json below the root is
        # Copilot CLI's, which reads it in the directory that it works in (the nested: row).
        self._file("packages/web/opencode.json", '{"mcp": {"x": {"type": "local", "command": ["x"]}}}')
        self._file("packages/web/.mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self.assertEqual(detect_mcp(self.repo), ["packages/web/.mcp.json"])

    def test_a_nested_command(self):
        self._file("packages/web/.cursor/commands/review.md")
        self.assertEqual([r["command"] for r in detect_commands(self.repo)],
                         ["packages/web/.cursor/commands/review.md"])

    def test_a_nested_plugin_manifest_is_a_plugin_and_not_other(self):
        # detect_other returns the manifest as other when it does not get the plugins. main
        # passes them and writes the manifest to plugins.csv.
        self._file("plugins/core/.claude-plugin/plugin.json", "{}")
        self.assertEqual(detect_plugins(self.repo), ["plugins/core/.claude-plugin/plugin.json"])
        self.assertEqual(self._other_for(), [])

    def test_a_nested_context_file_is_not_reported_twice(self):
        # The file is a context file, matched by basename at any depth. `detect_other` reads
        # the directory that contains it and must not report it a second time.
        self._file("packages/web/.claude/CLAUDE.md")
        self.assertIn("packages/web/.claude/CLAUDE.md", detect_context_files(self.repo))
        self.assertEqual(self._other_for(), [])

    def test_a_vendored_tree_is_not_read(self):
        self._file("node_modules/pkg/.claude/skills/deploy/SKILL.md")
        self._file("vendor/dep/.cursor/rules/style.mdc")
        self.assertEqual(detect_skills(self.repo), [])
        self.assertEqual(detect_rules(self.repo), [])

    def _other_for(self):
        return _other_as_main_does(self.repo)


class TestDetectOther(_RepoCase):

    def _other(self):
        return _other_as_main_does(self.repo)

    def test_empty_repo(self):
        self.assertEqual(self._other(), [])

    def test_unclaimed_spec_file_is_other(self):
        self._file(".claude/notes.txt")
        self.assertEqual(self._other(), [".claude/notes.txt"])

    def test_settings_are_not_other(self):
        self._file(".claude/settings.json", "{}")
        self.assertEqual(self._other(), [])

    def test_context_file_in_a_spec_dir_is_not_other(self):
        self._file(".claude/CLAUDE.md")
        self.assertEqual(self._other(), [])

    def test_root_context_file_is_not_other(self):
        self._file("CLAUDE.md")
        self.assertEqual(self._other(), [])

    def test_whole_skill_directory_is_known(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self._file(".claude/skills/pdf/scripts/run.py")
        self._file(".claude/skills/pdf/references/spec.md")
        self._file(".claude/skills/pdf/notes.txt")
        self.assertEqual(self._other(), [])

    def test_a_registered_root_skill_claims_its_own_directories_alone(self):
        """A package that registers itself as an Antigravity skill, with `pkg/.agents/skills.json`
        naming `.`, is the skill's folder. The skill consists of its SKILL.md and its support
        directories, as a plugin's root skill does. A file of the package that no detector
        claims stays `other`, and the skill is named after its folder."""
        self._file("pkg/.agents/skills.json", '{"entries": [{"path": "."}]}')
        self._file("pkg/SKILL.md", "---\nname: pkg\ndescription: d\n---\n")
        self._file("pkg/references/spec.md")
        self._file("pkg/.claude/notes.txt")
        self.assertEqual(self._other(), ["pkg/.claude/notes.txt"])
        self.assertEqual([(r["skills.md"], r["name"], r["references"])
                          for r in detect_skills(self.repo)],
                         [("pkg/.agents/skills.json", "skills", "FALSE"),
                          ("pkg/SKILL.md", "pkg", "TRUE")])

    def test_a_file_of_a_category_folder_is_unclassified_and_its_skill_is_not(self):
        self._file(".claude/skills/group/README.md")
        self._file(".claude/skills/group/pdf/SKILL.md")
        self._file(".claude/skills/group/pdf/scripts/run.sh")
        self.assertEqual(self._other(), [".claude/skills/group/README.md"])

    def test_subagent_is_not_other_but_its_siblings_are(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agents/notes.txt")
        self.assertEqual(self._other(), [".claude/agents/notes.txt"])

    def test_agent_memory_is_known_when_matched(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory/reviewer/MEMORY.md")
        self.assertEqual(self._other(), [])

    def test_local_agent_memory_is_known_when_matched(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory-local/reviewer/MEMORY.md")
        self.assertEqual(self._other(), [])

    def test_unmatched_agent_memory_is_other(self):
        self._file(".claude/agent-memory/ghost/MEMORY.md")
        self.assertEqual(self._other(), [".claude/agent-memory/ghost/MEMORY.md"])

    def test_every_file_of_a_subagent_s_memory_is_known(self):
        self._file(".claude/agents/reviewer.md", AGENT)
        self._file(".claude/agent-memory/reviewer/MEMORY.md")
        self._file(".claude/agent-memory/reviewer/build_discipline.md")
        self._file(".claude/agent-memory/reviewer/notes/dialects.md")
        self.assertEqual(self._other(), [])

    def test_commands_and_rules_are_not_other(self):
        self._file(".cursor/commands/review.md")
        self._file(".codex/rules/style.rules")
        self.assertEqual(self._other(), [])

    def test_claude_command_is_known_but_its_siblings_are_not(self):
        # A command file is a skill without a directory. Claiming it must not claim the whole
        # .claude/commands tree, as claiming a SKILL.md claims its directory.
        self._file(".claude/commands/review.md")
        self._file(".claude/commands/notes.txt")
        self.assertEqual(self._other(), [".claude/commands/notes.txt"])

    def test_the_files_of_a_command_folder_with_a_skill_md_belong_to_its_skill(self):
        # enricoros/big-AGI keeps `census.sh` beside `.claude/commands/tape-opens/SKILL.md`. The
        # folder is the skill's, as a folder of .claude/skills is, while a file beside a command
        # file stays other.
        self._file(".claude/commands/tape-opens/SKILL.md", "---\nname: tape-opens\n---\n")
        self._file(".claude/commands/tape-opens/notes.md")
        self._file(".claude/commands/tape-opens/census.sh")
        self._file(".claude/commands/notes.txt")
        self.assertEqual(self._other(), [".claude/commands/notes.txt"])

    def test_wrong_extension_in_a_rules_dir_is_other(self):
        self._file(".cursor/rules/style.txt")
        self.assertEqual(self._other(), [".cursor/rules/style.txt"])

    def test_mcp_files_are_not_other(self):
        self._file(".cursor/mcp.json", '{"mcpServers": {"x": {"command": "uvx"}}}')
        self.assertEqual(self._other(), [])

    def test_root_cursorindexingignore_is_other(self):
        self._file(".cursorindexingignore")
        self.assertEqual(self._other(), [".cursorindexingignore"])

    def test_root_ignore_files_are_always_other(self):
        # No detector claims them. They are the one kind of file that reaches other.csv by
        # design rather than by omission.
        self._file(".cursorignore")
        self.assertEqual(self._other(), [".cursorignore"])

    def test_unclaimed_github_files_are_other(self):
        # .github is a spec directory, and a Copilot configuration artifact there that no
        # detector claims is reported rather than dropped without trace. The skill detector
        # claims the skill.
        self._file(".github/workflows/ci.yml")
        self._file(".github/skills/pdf/SKILL.md")
        self.assertEqual(self._other(), [".github/workflows/ci.yml"])

    def test_a_hook_under_another_case_is_not_also_other(self):
        # One configuration artifact gets one row. The hook detector claims the file under the
        # name on disk, and `detect_other` does not report it a second time.
        self._file(".codex/Hooks.json",
                   '{"hooks": {"SessionStart": [{"matcher": "startup", "hooks": '
                   '[{"type": "command", "command": "./start.sh"}]}]}}')
        self.assertEqual(self._other(), [])

    def test_plugin_manifests_are_not_other(self):
        # A manifest is a plugins row. No detector claims the README beside it, and the README
        # stays other, which is what the unclassified_dir rows are for.
        for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json",
                    ".cursor-plugin/plugin.json", ".codex-plugin/plugin.json"):
            self._file(rel, '{"name": "demo"}')
        self._file(".cursor-plugin/README.md", "# demo\n")
        self.assertEqual(self._other(), [".cursor-plugin/README.md"])

    def test_result_is_sorted(self):
        for name in ("c.txt", "a.txt", "b.txt"):
            self._file(f".claude/{name}")
        self.assertEqual(self._other(),
                         [".claude/a.txt", ".claude/b.txt", ".claude/c.txt"])


# ---------------------------------------------------------------------------
# main  (the CSVs, the derived columns, and the failure exits)
# ---------------------------------------------------------------------------

class TestDeclaredFileAttribution(_StepCase):
    def test_detection_and_metadata_rebuild_use_the_declaring_tool(self):
        fixtures = {
            'o/codex': ({'.codex/config.toml': 'project_doc_fallback_filenames = ["README.md"]',
                         'README.md': '# Instructions\n'}, {'codex'}),
            'o/gemini': ({'.gemini/settings.json': '{"context":{"fileName":["README.md"]}}',
                          'README.md': '# Instructions\n'}, {'gemini'}),
            'o/github': ({'.gemini/settings.json': '{"context":{"fileName":["CONTRIBUTING.md"]}}',
                          '.github/CONTRIBUTING.md': '# Instructions\n'}, {'gemini'}),
            'o/mcp': ({'.gemini/settings.json': '{"context":{"fileName":["mcp.json"]}}',
                       '.github/mcp.json': '{"mcpServers":{"x":{"command":"true"}}}'},
                      {'gemini', 'copilot'}),
            'o/shared': ({'.gemini/settings.json': '{"context":{"fileName":["CLAUDE.md"]}}',
                          'CLAUDE.md': '# Instructions\n'}, {'gemini', 'claude'}),
        }
        for name, (files, _) in fixtures.items():
            self._repo(name, files)
        self.assertEqual(self._run(), 0)
        from agentcontext.steps.file_history import reconcile_repos_csv
        for phase in ('detection', 'file_history'):
            if phase == 'file_history':
                reconcile_repos_csv(self.output)
            with (self.output / 'repos.csv').open() as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 5)
            for row in rows:
                with self.subTest(phase=phase, repo=row['repo_name']):
                    tools = {key for key in ('claude', 'gemini', 'codex', 'copilot', 'cursor')
                             if row[key] == 'TRUE'}
                    self.assertEqual(tools, fixtures[row['repo_name']][1])

    def test_collection_support_files_do_not_identify_a_tool(self):
        for path in ('README.md', 'readme.md', 'LICENSE', 'LICENSE.md', 'LICENSE.txt', 'package.json'):
            with self.subTest(path=path):
                self.assertIsNone(_mod.tool_for_path(path))


class TestToolColumn(_StepCase):
    """Each row's `tool` is an attribution, and a repository's tool flags are the union of the
    attributions of its rows.

    The expected values are written out from the documents that each case names rather than
    read from `artifact_tools`."""

    TOOL_FLAGS = ("claude", "gemini", "codex", "copilot", "cursor", "opencode")

    def _tools(self) -> dict:
        """Return {(repo_name, path): tool} over every mechanism CSV."""
        found = {}
        for mechanism, (filename, column) in heuristics.MECHANISM_CSVS.items():
            for row in self._out_rows(filename):
                found[(row["repo_name"], row[column])] = row["tool"]
        return found

    def _flags(self, repo_name) -> set:
        row = {r["repo_name"]: r for r in self._out_rows("repos.csv")}[repo_name]
        return {t for t in (*self.TOOL_FLAGS, "no_specific_tool") if row[t] == "TRUE"}

    def test_the_instructions_that_a_codex_role_names_are_a_part_of_the_role(self):
        """areal-project/AReaL declares each role in `.codex/config.toml` with a role file in
        `.codex/agents`, such as `agents/planner.toml`, which names `./planner.md` in
        `model_instructions_file`, the base instructions of the subagent up to Codex
        rust-v0.148.0. Codex delegates to the role, and the instructions file is its prompt, as
        the prompt file of an OpenCode agent is a part of the agent: each role is one subagent row
        of Codex, the TOML file, and the instructions file is neither a row of its own nor an
        `other` row. The Markdown files open with a heading, which Cursor's loader of
        `.codex/agents` refuses, and a Markdown file that no role names stays an `other` row."""
        names = ("algorithm-expert", "archon-expert", "code-verifier", "fsdp-expert",
                 "launcher-expert", "megatron-expert", "planner", "simple-code-reviewer")
        files = {".codex/config.toml": "".join(
            f'[agents.{name}]\ndescription = "{name}"\nconfig_file = "./agents/{name}.toml"\n\n'
            for name in names), ".codex/agents/notes.md": "# Notes\n"}
        for name in names:
            files[f".codex/agents/{name}.toml"] = (f'name = "{name}"\n'
                                                   f'model_instructions_file = "./{name}.md"\n')
            files[f".codex/agents/{name}.md"] = f"# {name}\n\nYou are an expert.\n"
        self._repo("o/a", files)
        self.assertEqual(self._run(), 0)
        self.assertEqual(sorted((row["subagent"], row["name"], row["tool"])
                                for row in self._out_rows("subagents.csv")),
                         [(f".codex/agents/{name}.toml", name, "codex") for name in names])
        self.assertEqual({row["other"] for row in self._out_rows("other.csv")},
                         {".codex/agents/notes.md"})

    def test_a_role_file_that_the_codex_config_names_is_a_subagent_file_of_codex(self):
        """The `config_file` of a role in `.codex/config.toml` names a role file outside
        `.codex/agents`, relative to `.codex`, and the role names its instructions relative to
        its own folder. The instructions file is a part of the role."""
        self._repo("o/a", {".codex/config.toml": '[agents.reviewer]\ndescription = "Reviews."\n'
                                                 'config_file = "../roles/reviewer.toml"\n',
                           "roles/reviewer.toml": 'name = "reviewer"\n'
                                                  'model_instructions_file = "reviewer.md"\n',
                           "roles/reviewer.md": "# Reviewer\n\nReview.\n"})
        self.assertEqual(self._run(), 0)
        tools = self._tools()
        self.assertEqual(tools[("o/a", "roles/reviewer.toml")], "codex")
        self.assertNotIn(("o/a", "roles/reviewer.md"), tools)
        self.assertEqual({row["subagent"] for row in self._out_rows("subagents.csv")},
                         {"roles/reviewer.toml"})

    def test_the_prompt_file_of_an_opencode_agent_is_a_part_of_the_agent(self):
        """cakmoel/Scriptlog's `opencode.json` gives its subagents the prompts that
        `{file:.opencode/prompts/<name>.md}` names, and OpenCode copies the text of each file into
        the configuration. The prompt files are parts of the subagent row of `opencode.json`,
        and a file of `.opencode` that no agent names stays an `other` row."""
        agents = {name: {"description": name, "mode": "subagent",
                         "prompt": "{file:.opencode/prompts/%s.md}" % name}
                  for name in ("playwright-test-generator", "playwright-test-healer")}
        self._repo("o/a", {"opencode.json": json.dumps({"$schema": "https://opencode.ai/config.json",
                                                        "agent": agents}),
                           ".opencode/prompts/playwright-test-generator.md": "Generate.\n",
                           ".opencode/prompts/playwright-test-healer.md": "Heal.\n",
                           ".opencode/package.json": '{"name": "tools"}\n'})
        self.assertEqual(self._run(), 0)
        self.assertEqual([(row["subagent"], row["tool"]) for row in self._out_rows("subagents.csv")],
                         [("opencode.json", "opencode")])
        self.assertEqual({row["other"] for row in self._out_rows("other.csv")},
                         {".opencode/package.json"})

    def test_a_workflow_is_the_tool_of_the_action_it_runs(self):
        """By path, `.github/workflows` is Copilot's, but the workflow runs the Codex action."""
        self._repo("o/a", {".github/workflows/review.yml":
                           "jobs:\n  review:\n    steps:\n      - uses: openai/codex-action@v1\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(), {("o/a", ".github/workflows/review.yml"): "codex"})
        self.assertEqual(self._flags("o/a"), {"codex"})

    def test_an_unclassified_file_keeps_its_tool_and_sets_no_flag(self):
        """`.codex/notes.txt` is in Codex's directory, and no Codex detector reads it. Its
        `other.csv` row names Codex, and the repository's only flag comes from `AGENTS.md`."""
        self._repo("o/a", {"AGENTS.md": "# Instructions\n", ".codex/notes.txt": "notes\n"})
        self.assertEqual(self._run(), 0)
        other = {r["other"]: r["tool"] for r in self._out_rows("other.csv")}
        self.assertEqual(other.get(".codex/notes.txt"), "codex")
        self.assertEqual(self._flags("o/a"), {"no_specific_tool"})

    def test_a_declared_file_is_the_declaring_tool_s(self):
        """Codex reads README.md because `project_doc_fallback_filenames` names it."""
        self._repo("o/a", {".codex/config.toml": 'project_doc_fallback_filenames = ["README.md"]',
                           "README.md": "# Instructions\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(), {("o/a", ".codex/config.toml"): "codex",
                                         ("o/a", "README.md"): "codex"})

    def test_a_plugin_part_is_its_plugin_s(self):
        """`plugins/lint/skills` names no tool, but the Copilot manifest beside it does."""
        self._repo("o/a", {"plugins/lint/.github/plugin/plugin.json": '{"name": "lint"}',
                           "plugins/lint/skills/lint/SKILL.md":
                               "---\nname: lint\ndescription: Run lint\n---\nLint files.\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(),
                         {("o/a", "plugins/lint/.github/plugin/plugin.json"): "copilot",
                          ("o/a", "plugins/lint/skills/lint/SKILL.md"): "copilot"})
        self.assertEqual(self._flags("o/a"), {"copilot"})

    def test_a_plugin_that_its_root_plugin_json_identifies_is_copilot_s(self):
        """dotnet/msbuild's plugins/mt-migration, a legacy Copilot CLI plugin with its manifest
        at the plugin root: "Legacy plugins: .plugin/plugin.json, plugin.json,
        .github/plugin/plugin.json, or .claude-plugin/plugin.json (checked in this order)". The
        manifest names its skills and its agent."""
        self._repo("o/a", {
            "plugins/mt-migration/plugin.json": json.dumps({
                "name": "mt-migration", "skills": ["./skills/"],
                "agents": ["./agents/mt-migration-reviewer.agent.md"]}),
            "plugins/mt-migration/skills/mt-migration/SKILL.md":
                "---\nname: mt-migration\ndescription: Migrate tasks\n---\nMigrate.\n",
            "plugins/mt-migration/agents/mt-migration-reviewer.agent.md":
                "---\ndescription: Review the migration\n---\nReview.\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(),
                         {("o/a", "plugins/mt-migration/plugin.json"): "copilot",
                          ("o/a", "plugins/mt-migration/skills/mt-migration/SKILL.md"): "copilot",
                          ("o/a", "plugins/mt-migration/agents/mt-migration-reviewer.agent.md"):
                              "copilot"})
        self.assertEqual(self._flags("o/a"), {"copilot"})
        self.assertEqual(self._out_rows("other.csv"), [])

    def test_a_github_directory_with_a_legacy_manifest_adds_only_the_manifest(self):
        """dotnet/maui's .github/plugin.json names ./skills/. The skills and agents of the plugin
        are the repository's own .github/skills and .github/agents, and each of them is one row."""
        self._repo("o/a", {
            ".github/plugin.json": json.dumps({"name": "dotnet-maui-repo",
                                               "skills": ["./skills/"]}),
            ".github/skills/triage/SKILL.md":
                "---\nname: triage\ndescription: Triage issues\n---\nTriage.\n",
            ".github/agents/review.agent.md": "---\ndescription: Review\n---\nReview.\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(),
                         {("o/a", ".github/plugin.json"): "copilot",
                          ("o/a", ".github/skills/triage/SKILL.md"): "copilot",
                          ("o/a", ".github/agents/review.agent.md"): "copilot"})
        self.assertEqual([row["plugin"] for row in self._out_rows("plugins.csv")],
                         [".github/plugin.json"])
        self.assertEqual(self._out_rows("other.csv"), [])
        rows = [(name, row[column]) for name, (filename, column)
                in heuristics.MECHANISM_CSVS.items() for row in self._out_rows(filename)]
        self.assertEqual(len(rows), len(set(rows)), rows)

    def _mechanism_tools(self) -> dict:
        """Return {(mechanism, path): tool} over every mechanism CSV and other.csv."""
        found = {(mechanism, row[column]): row["tool"]
                 for mechanism, (filename, column) in heuristics.MECHANISM_CSVS.items()
                 for row in self._out_rows(filename)}
        found.update({("other", row["other"]): row["tool"] for row in self._out_rows("other.csv")})
        return found

    def test_a_plugin_json_in_another_vendor_s_manifest_directory_makes_no_copilot_plugin(self):
        """saadeghi/daisyui: the root is a Claude Code and Cursor plugin, and its skills
        directory holds the Grok plugin with its manifest in skills/.grok-plugin/. Grok's
        marketplace names ./skills as the plugin's source, and the manifest's "skills": "./"
        names that directory. A legacy Copilot CLI manifest is directly in the plugin directory,
        as the default layout of the CLI plugin reference has plugin.json in my-plugin/, so the
        Grok manifest makes no Copilot plugin and is no row of plugins.csv."""
        self._repo("o/a", {
            ".claude-plugin/plugin.json": json.dumps({
                "name": "daisyui", "version": "5.7.0", "author": {"name": "daisyUI"},
                "defaultEnabled": True, "license": "MIT"}),
            ".cursor-plugin/plugin.json": json.dumps({
                "name": "daisyui", "version": "5.7.0", "author": {"name": "daisyUI"},
                "license": "MIT"}),
            ".grok-plugin/marketplace.json": json.dumps({
                "name": "daisyui", "owner": {"name": "daisyUI"},
                "plugins": [{"name": "daisyui", "category": "development",
                             "source": {"type": "local", "path": "./skills"}}]}),
            "skills/.grok-plugin/plugin.json": json.dumps({
                "name": "daisyui", "displayName": "daisyUI", "version": "5.7.0",
                "author": {"name": "daisyUI", "url": "https://daisyui.com"}, "license": "MIT",
                "skills": "./"}),
            "skills/daisyui/SKILL.md": "---\nname: daisyui\ndescription: Official daisyUI "
                                       "component-library skill for Tailwind CSS.\n---\nUse it.\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._mechanism_tools(),
                         {("plugins", ".claude-plugin/plugin.json"): "claude",
                          ("plugins", ".cursor-plugin/plugin.json"): "cursor",
                          ("skills", "skills/daisyui/SKILL.md"): "claude|cursor"})
        self.assertEqual(self._flags("o/a"), {"claude", "cursor"})

    def test_a_legacy_manifest_in_another_plugin_s_part_is_copilot_s_alone(self):
        """A legacy Copilot CLI plugin in a skill folder of a Claude Code and Cursor plugin.
        The skill's SKILL.md is a skill of all three plugins, and the plugin.json is the
        manifest of the Copilot plugin alone, because the plugins around it read the folder as
        a skill."""
        self._repo("o/a", {
            ".claude-plugin/plugin.json": json.dumps({"name": "kit", "version": "1.0.0"}),
            ".cursor-plugin/plugin.json": json.dumps({"name": "kit", "version": "1.0.0"}),
            "skills/deploy/plugin.json": json.dumps({"name": "deploy", "version": "1.0.0",
                                                     "skills": "./"}),
            "skills/deploy/SKILL.md": "---\nname: deploy\ndescription: Deploy the site.\n---\n"
                                      "Deploy.\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._mechanism_tools(),
                         {("plugins", ".claude-plugin/plugin.json"): "claude",
                          ("plugins", ".cursor-plugin/plugin.json"): "cursor",
                          ("plugins", "skills/deploy/plugin.json"): "copilot",
                          ("skills", "skills/deploy/SKILL.md"): "claude|copilot|cursor"})

    def test_the_root_mcp_json_of_a_legacy_or_claude_code_plugin_at_the_root_keeps_any(self):
        """wonderwhy-er/DesktopCommanderMCP: the root is a Gemini extension and, by its root
        plugin.json, a legacy Copilot CLI plugin, which names "./skills/" and ".mcp.json". A
        part of a plugin at the root gets the plugin's tool beside the tools of the
        repository's own rows. The root `.mcp.json` keeps the `any` of its `root_file` row,
        because every tool that reads the repository's MCP configuration reads it, and so does
        the root `.mcp.json` of a Claude Code plugin at the root (CopilotKit/CopilotKit). The
        skills are read by both plugins, the Gemini extension and the Copilot plugin."""
        mcp = json.dumps({"mcpServers": {"desktop-commander": {
            "command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander@latest"]}}})
        self._repo("o/a", {
            "plugin.json": json.dumps({
                "name": "desktop-commander", "version": "1.5.0",
                "author": {"name": "Desktop Commander"}, "license": "MIT",
                "keywords": ["shell", "mcp"], "skills": "./skills/", "mcpServers": ".mcp.json"}),
            ".mcp.json": mcp,
            "gemini-extension.json": json.dumps({
                "name": "desktop-commander", "version": "1.5.0",
                "mcpServers": {"desktop-commander": {
                    "command": "npx", "args": ["-y", "@wonderwhy-er/desktop-commander"]}},
                "excludeTools": []}),
            "skills/terminal/SKILL.md": "---\nname: terminal\nversion: 0.1.0\ndescription: Use "
                                        "Desktop Commander for terminal work.\n---\nRun it.\n",
            "rules/desktop-commander-default.mdc": "---\ndescription: Prefer Desktop Commander "
                                                   "MCP.\nalwaysApply: true\n---\nPrefer it.\n"})
        self._repo("o/b", {".claude-plugin/plugin.json": json.dumps({"name": "copilotkit"}),
                           ".mcp.json": mcp})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(),
                         {("o/a", "plugin.json"): "copilot",
                          ("o/a", ".mcp.json"): "any",
                          ("o/a", "gemini-extension.json"): "gemini",
                          ("o/a", "skills/terminal/SKILL.md"): "gemini|copilot",
                          ("o/a", "rules/desktop-commander-default.mdc"): "copilot",
                          ("o/b", ".claude-plugin/plugin.json"): "claude",
                          ("o/b", ".mcp.json"): "any"})

    def test_the_root_mcp_json_keeps_any_in_a_plugin_of_every_format_at_the_root(self):
        """Each manifest makes the root a plugin that reads the root `.mcp.json` as its MCP
        servers: Claude Code's `.claude-plugin/`, Codex's `.codex-plugin/`, Cursor's
        `.cursor-plugin/`, and Copilot CLI's `.github/plugin/`, `.plugin/` and a root
        `plugin.json` that names its MCP file. The file keeps `any`. The plugin's skill, which the
        `SKILL.md` row with the tool `any` explains, still gets the plugin's tool, because that row
        is no `root_file` row. A plugin below the root gives its own `.mcp.json` its tool beside
        Copilot's."""
        mcp = json.dumps({"mcpServers": {"server": {"command": "npx", "args": ["server"]}}})
        skill = "---\nname: lint\ndescription: Run lint\n---\nLint files.\n"
        manifests = {"claude": ".claude-plugin/plugin.json", "codex": ".codex-plugin/plugin.json",
                     "cursor": ".cursor-plugin/plugin.json",
                     "copilot-github": ".github/plugin/plugin.json",
                     "copilot-dot": ".plugin/plugin.json", "copilot-legacy": "plugin.json"}
        for name, manifest in manifests.items():
            data = {"name": "lint", **({"mcpServers": ".mcp.json"} if name == "copilot-legacy"
                                       else {})}
            self._repo(f"o/{name}", {manifest: json.dumps(data), ".mcp.json": mcp,
                                     "skills/lint/SKILL.md": skill})
        self._repo("o/below", {"plugins/lint/.claude-plugin/plugin.json": '{"name": "lint"}',
                               "plugins/lint/.mcp.json": mcp})
        self.assertEqual(self._run(), 0)
        tools = self._tools()
        for name, manifest in manifests.items():
            tool = name.split("-")[0]
            with self.subTest(manifest=manifest):
                self.assertEqual({path: value for (repo, path), value in tools.items()
                                  if repo == f"o/{name}"},
                                 {manifest: tool, ".mcp.json": "any",
                                  "skills/lint/SKILL.md": tool})
                self.assertEqual(self._flags(f"o/{name}"), {tool})
        self.assertEqual({path: value for (repo, path), value in tools.items() if repo == "o/below"},
                         {"plugins/lint/.claude-plugin/plugin.json": "claude",
                          "plugins/lint/.mcp.json": "claude|copilot"})

    def test_a_copilot_plugin_manifest_below_the_root_is_copilot_s(self):
        """`tool_for_path` reads `.github` at the root only. By path alone, a Copilot CLI plugin
        under `plugins/lint/` without parts would give its repository no tool at all."""
        self._repo("o/a", {"plugins/lint/.github/plugin/plugin.json": '{"name": "lint"}'})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(),
                         {("o/a", "plugins/lint/.github/plugin/plugin.json"): "copilot"})
        self.assertEqual(self._flags("o/a"), {"copilot"})

    def test_a_linked_directory_is_also_the_linking_tool_s(self):
        """Cursor reads `.claude/skills` through its `.cursor/skills` link, and the skill
        belongs to both tools, joined in column order. A shared directory that a tool links to
        belongs to that tool, and the repository counts for the tool rather than for no
        specific tool."""
        self._repo("o/a", {".claude/skills/pdf/SKILL.md": "Read PDFs.\n",
                           ".cursor/skills": "../.claude/skills\n"})
        self._repo("o/b", {".agents/skills/pdf/SKILL.md": "Read PDFs.\n",
                           ".claude/skills": "../.agents/skills\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(), {("o/a", ".claude/skills/pdf/SKILL.md"): "claude|cursor",
                                         ("o/b", ".agents/skills/pdf/SKILL.md"): "claude"})
        self.assertEqual(self._flags("o/a"), {"claude", "cursor"})
        self.assertEqual(self._flags("o/b"), {"claude"})

    def test_a_linked_skill_md_is_no_skill_where_each_tool_of_its_directory_skips_it(self):
        """A SKILL.md that links to a file, which the collector stores as a marker beside the
        file that it names. Cursor CLI keeps a SKILL.md only when its real path is within the real
        path of the skills directory, so the links of `.cursor/skills` to `agents/skills/`, as
        NotePlan/plugins has them, and to a file that the tree lacks are `other` rows of Cursor,
        and the link to a skill in `.cursor/skills` is a skill. Codex skips every such link, and
        Cursor reads `.codex/skills` too, so the link out of `.codex/skills` is an `other` row of
        Codex and the link to another skill of `.codex/skills` is Cursor's skill. Claude Code,
        OpenCode and Copilot CLI read a linked SKILL.md. In `.agents/skills`, Copilot CLI,
        Antigravity CLI and OpenCode read it, and the skill therefore keeps `any` there."""
        skill = "---\nname: nplog\ndescription: Read the plugin log\n---\nRead the log.\n"
        shared = "../../../agents/skills/nplog/SKILL.md\n"
        self._repo("o/a", {"AGENTS.md": "# Instructions\n", "agents/skills/nplog/SKILL.md": skill,
                           ".cursor/skills/nplog/SKILL.md": shared,
                           ".cursor/skills/gone/SKILL.md": "../../../agents/skills/gone/SKILL.md\n",
                           ".codex/skills/nplog/SKILL.md": shared})
        self._repo("o/b", {".cursor/skills/real/SKILL.md": skill,
                           ".cursor/skills/alias/SKILL.md": "../real/SKILL.md\n",
                           ".codex/skills/real/SKILL.md": skill,
                           ".codex/skills/alias/SKILL.md": "../real/SKILL.md\n"})
        self._repo("o/c", {"agents/skills/nplog/SKILL.md": skill,
                           **{f"{directory}/nplog/SKILL.md": shared
                              for directory in (".claude/skills", ".agents/skills",
                                                ".github/skills", ".opencode/skills")}})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._mechanism_tools(),
                         {("context_file", "AGENTS.md"): "any",
                          ("other", ".cursor/skills/nplog/SKILL.md"): "cursor",
                          ("other", ".cursor/skills/gone/SKILL.md"): "cursor",
                          ("other", ".codex/skills/nplog/SKILL.md"): "codex",
                          ("skills", ".cursor/skills/real/SKILL.md"): "cursor",
                          ("skills", ".cursor/skills/alias/SKILL.md"): "cursor",
                          ("skills", ".codex/skills/real/SKILL.md"): "codex",
                          ("skills", ".codex/skills/alias/SKILL.md"): "cursor",
                          ("skills", ".claude/skills/nplog/SKILL.md"): "claude",
                          ("skills", ".agents/skills/nplog/SKILL.md"): "any",
                          ("skills", ".github/skills/nplog/SKILL.md"): "copilot",
                          ("skills", ".opencode/skills/nplog/SKILL.md"): "opencode"})
        self.assertEqual(self._flags("o/a"), {"no_specific_tool"})
        self.assertEqual(self._flags("o/b"), {"codex", "cursor"})
        self.assertEqual(self._flags("o/c"), {"claude", "copilot", "opencode"})

    def test_the_skills_of_a_category_folder_of_claude_skills_are_cursor_s_and_opencode_s(self):
        """Claude Code reads the skill folders directly in .claude/skills, and no folder that
        groups them, such as the gitnexus/ that GitNexus writes into
        RocketChat/Rocket.Chat.Electron's .claude/skills. Cursor reads the directory at any depth:
        "For compatibility, Cursor also loads skills from Claude and Codex directories:
        `.claude/skills/`" and "Skill directories can be organized into subdirectories"
        (https://cursor.com/docs/skills, read 2026-11-05). OpenCode 1.19.3 scans every .claude
        from the working directory up to the worktree with "skills/**/SKILL.md", which reaches
        growilabs/growi's apps/app/.claude/skills/learned/. Both keep a file named SKILL.md
        exactly, which GitHamza0206/simba's lowercase skill.md is not. A SKILL.md in the folder
        of a skill that Claude Code reads, as humanlayer/humanlayer keeps one in references/,
        is part of that skill. So is one that a link leads to: DataDog/datadog-agent links
        .claude/skills to ../.agents/skills, which has a skill allium/ with skills in
        allium/skills/, and those keep the tool `any` of .agents/skills."""
        self._repo("o/a", {
            ".claude/skills/review/SKILL.md": "---\nname: review\ndescription: Review\n---\nx\n",
            ".claude/skills/gitnexus/gitnexus-cli/SKILL.md":
                "---\nname: gitnexus-cli\ndescription: \"Use when the user needs to run GitNexus CLI "
                "commands\"\n---\n\n# GitNexus CLI Commands\n",
            ".claude/skills/gitnexus/README.md": "# GitNexus\n",
            ".claude/skills/test-it-locally/SKILL.md":
                "---\nname: test-it-locally\ndescription: Test\n---\nx\n",
            ".claude/skills/test-it-locally/references/local-cli-testing/SKILL.md":
                "---\nname: local-cli-testing\ndescription: Guide local CLI login\n---\n"})
        self._repo("o/b", {
            "apps/app/.claude/skills/learned/fix-broken-next-symlinks/SKILL.md":
                "---\nname: fix-broken-next-symlinks\ndescription: Fix broken symlinks in "
                ".next/node_modules/\n---\n\n## IMPORTANT\n"})
        self._repo("o/c", {
            ".claude/skills/simba/deploy/skill.md":
                "---\nname: deploy\ndescription: Deploy the Simba chat widget\n---\n"})
        self._repo("o/d", {
            ".claude/skills": "../.agents/skills\n",
            ".agents/skills/allium/SKILL.md": "---\nname: allium\ndescription: Allium\n---\nx\n",
            ".agents/skills/allium/skills/distill/SKILL.md":
                "---\nname: distill\ndescription: Distill\n---\nx\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._mechanism_tools(), {
            ("skills", ".claude/skills/review/SKILL.md"): "claude",
            ("skills", ".claude/skills/gitnexus/gitnexus-cli/SKILL.md"): "cursor|opencode",
            ("other", ".claude/skills/gitnexus/README.md"): "claude",
            ("skills", ".claude/skills/test-it-locally/SKILL.md"): "claude",
            ("skills", "apps/app/.claude/skills/learned/fix-broken-next-symlinks/SKILL.md"):
                "cursor|opencode",
            ("other", ".claude/skills/simba/deploy/skill.md"): "claude",
            ("skills", ".agents/skills/allium/SKILL.md"): "claude",
            ("skills", ".agents/skills/allium/skills/distill/SKILL.md"): "any"})
        self.assertEqual(self._flags("o/a"), {"claude", "cursor", "opencode"})
        self.assertEqual(self._flags("o/b"), {"cursor", "opencode"})
        self.assertEqual(self._flags("o/d"), {"claude"})

    def test_a_codex_skill_that_codex_does_not_load_is_cursor_s(self):
        """Codex's parser refuses a SKILL.md without a description, "if description.is_empty() {
        return Err(SkillParseError::MissingField("description")); }", and one whose first line
        is not ---, which a byte order mark makes it, because the file is read with
        String::from_utf8 and trim keeps U+FEFF (codex-rs/skills/src/parser.rs and
        codex-rs/file-system/src/lib.rs at f9c10053, read 2026-11-05). Cursor reads .codex/skills
        at the root and below it and needs no description (Cursor CLI 2026.09.28-64d2043). The
        fixtures are probelabs/probe's test_data/skills/.codex/skills/no-desc/SKILL.md and
        FlameskyDexive/Legends-Of-Heroes's .codex/skills/aibridge/SKILL.md, shortened. A Codex
        plugin's skill that Codex refuses is no skill, because no other tool reads it."""
        loads = "---\nname: {}\ndescription: Does {}\n---\nSteps.\n"
        self._repo("o/a", {
            "test_data/skills/.codex/skills/no-desc/SKILL.md":
                "---\nname: no-desc\n---\n\nFirst paragraph description.\n",
            "test_data/skills/.codex/skills/valid/SKILL.md": loads.format("valid", "it"),
            "p/.codex-plugin/plugin.json": '{"name": "p"}',
            "p/skills/plain/SKILL.md": "# Plain\n",
            "p/skills/build/SKILL.md": loads.format("build", "builds")})
        self._repo("o/b", {
            ".codex/skills/aibridge/SKILL.md":
                "\ufeff---\nname: aibridge\ndescription: Unity Editor and Player Runtime CLI "
                "integration for AIBridge.\n---\n",
            ".codex/skills/unity-yaml-editing/SKILL.md": loads.format("unity-yaml-editing", "it")})
        self.assertEqual(self._run(), 0)
        tools = self._mechanism_tools()
        self.assertEqual({key: tool for key, tool in tools.items() if key[0] == "skills"}, {
            ("skills", "test_data/skills/.codex/skills/no-desc/SKILL.md"): "cursor",
            ("skills", "test_data/skills/.codex/skills/valid/SKILL.md"): "codex",
            ("skills", "p/skills/build/SKILL.md"): "codex",
            ("skills", ".codex/skills/aibridge/SKILL.md"): "cursor",
            ("skills", ".codex/skills/unity-yaml-editing/SKILL.md"): "codex"})
        self.assertEqual(self._flags("o/b"), {"codex", "cursor"})

    def test_a_skill_with_a_front_matter_that_does_not_parse_is_no_skill_of_cursor(self):
        """Cursor CLI 2026.09.28-64d2043 drops a skill when gray-matter 4.0.3 cannot parse its
        front matter, "catch(e){return(0,m.cY)(`Failed to parse skill frontmatter for ${s}:`,e),
        null}" in parseSkillMarkdown, for a skills directory and a plugin alike, and Cursor's
        staff states for the IDE that "If that frontmatter isn't valid YAML, the request fails
        with a generic server error" (forum thread 168440, read 2026-11-07). The fixtures are
        shortened from cabloy/cabloy's .cursor/skills, with an unquoted ": " in the descriptions,
        from Shopify/shopify-app-js's .claude/skills/dependabot/fix-broken-major-upgrade/SKILL.md,
        in a category folder that Claude Code does not read, from probelabs/probe's
        test_data/skills/.codex/skills/invalid-yaml/SKILL.md, "name: [invalid", which Codex
        refuses as well, and from Codecademy/gamut's packages/gamut/agent-tools, a Claude Code and
        Cursor plugin. A description in quotes parses, a front matter without a closing line runs
        to the end of the file, and a file without a front matter loads."""
        colon = "---\nname: {}\ndescription: Route the work: backend or frontend\n---\nSteps.\n"
        quoted = '---\nname: {}\ndescription: "Route the work: backend or frontend"\n---\nSteps.\n'
        self._repo("o/a", {
            ".cursor/skills/workflow/SKILL.md": colon.format("workflow"),
            ".cursor/skills/quoted/SKILL.md": quoted.format("quoted"),
            ".cursor/skills/open/SKILL.md": "---\nname: open\n\n# Open\n\nSteps: one: two\n",
            ".cursor/skills/plain/SKILL.md": "# Plain\n\nNo front matter.\n"})
        self._repo("o/b", {
            ".claude/skills/dependabot/fix-broken-major-upgrade/SKILL.md":
                "---\ndescription: Process a major Dependabot PR: research breaking changes, apply "
                "fixes, validate CI\n---\nSteps.\n",
            ".claude/skills/dependabot/triage/SKILL.md": quoted.format("triage")})
        self._repo("o/c", {
            "test_data/skills/.codex/skills/invalid-yaml/SKILL.md":
                "---\nname: [invalid\n---\n\nInvalid YAML should be skipped.\n"})
        self._repo("o/d", {
            "agent-tools/.claude-plugin/plugin.json": '{"name": "gamut"}',
            "agent-tools/.cursor-plugin/plugin.json": '{"name": "gamut"}',
            "agent-tools/skills/component-first/SKILL.md": colon.format("component-first"),
            "plugins/p/.cursor-plugin/plugin.json": '{"name": "p"}',
            "plugins/p/skills/broken/SKILL.md": colon.format("broken"),
            "plugins/p/skills/fine/SKILL.md": quoted.format("fine"),
            "plugins/named/.cursor-plugin/plugin.json": '{"name": "named", "skills": "./tools/"}',
            "plugins/named/tools/broken/SKILL.md": colon.format("broken"),
            "plugins/named/tools/fine/SKILL.md": quoted.format("fine"),
            "plugins/one/.cursor-plugin/plugin.json": '{"name": "one"}',
            "plugins/one/SKILL.md": colon.format("one"),
            "plugins/two/.cursor-plugin/plugin.json": '{"name": "two"}',
            "plugins/two/SKILL.md": quoted.format("two")})
        self.assertEqual(self._run(), 0)
        tools = self._mechanism_tools()
        self.assertEqual({key: tool for key, tool in tools.items()
                          if key[0] in ("skills", "other")}, {
            ("other", ".cursor/skills/workflow/SKILL.md"): "cursor",
            ("skills", ".cursor/skills/quoted/SKILL.md"): "cursor",
            ("other", ".cursor/skills/open/SKILL.md"): "cursor",
            ("skills", ".cursor/skills/plain/SKILL.md"): "cursor",
            ("skills", ".claude/skills/dependabot/fix-broken-major-upgrade/SKILL.md"): "opencode",
            ("skills", ".claude/skills/dependabot/triage/SKILL.md"): "cursor|opencode",
            ("other", "test_data/skills/.codex/skills/invalid-yaml/SKILL.md"): "codex",
            ("skills", "agent-tools/skills/component-first/SKILL.md"): "claude",
            ("skills", "plugins/p/skills/fine/SKILL.md"): "cursor",
            ("skills", "plugins/named/tools/fine/SKILL.md"): "cursor",
            ("skills", "plugins/two/SKILL.md"): "cursor"})
        self.assertEqual(self._flags("o/c"), set())

    def test_codex_reads_no_skill_md_in_another_case_through_a_link(self):
        """shapeshift/web links .codex/skills to ../.claude/skills, and its
        .claude/skills/chain-integration-crew/skill.md opens with a heading. Codex keeps a file
        only when "entry.path.basename().as_deref() == Some(SKILLS_FILENAME)", with
        SKILLS_FILENAME = "SKILL.md" (codex-rs/ext/skills/src/loader/discovery.rs at f9c10053,
        read 2026-11-05), and compares the name that the listing returns on every host. The file
        is Claude Code's alone, and the skill beside it is Claude Code's and Codex's. A skill.md
        with a front matter that Codex's parser accepts is Claude Code's alone too."""
        self._repo("o/a", {
            ".codex/skills": "../.claude/skills\n",
            ".claude/skills/chain-integration/SKILL.md":
                "---\nname: chain-integration\ndescription: Integrate a chain\n---\nSteps.\n",
            ".claude/skills/chain-integration-crew/skill.md":
                "# Chain Integration Full YOLO E2E Crew\n",
            ".claude/skills/review/skill.md":
                "---\nname: review\ndescription: Review a change\n---\nSteps.\n"})
        self.assertEqual(self._run(), 0)
        tools = self._mechanism_tools()
        self.assertEqual(tools[("skills", ".claude/skills/chain-integration/SKILL.md")],
                         "claude|codex")
        self.assertEqual(tools[("skills", ".claude/skills/chain-integration-crew/skill.md")],
                         "claude")
        self.assertEqual(tools[("skills", ".claude/skills/review/skill.md")], "claude")

    def test_a_skill_s_reference_file_is_no_skill_of_the_skills_directory(self):
        """GovTechSG/sgds-web-component links .claude/commands/storybook-stories to
        ../../.github/skills/storybook-stories, and Claude Code reads every .md below the link
        as a command. Copilot reads the SKILL.md of each folder of .github/skills, VS Code by
        `joinPath(child.resource, SKILL_FILENAME)` (promptFilesLocator.ts), and no other file of
        the folder as a skill. reference/examples.md is therefore Claude Code's alone."""
        self._repo("o/a", {
            ".claude/commands/storybook-stories": "../../.github/skills/storybook-stories\n",
            ".github/skills/storybook-stories/SKILL.md":
                "---\nname: storybook-stories\ndescription: Write stories\n---\nSteps.\n",
            ".github/skills/storybook-stories/reference/examples.md": "# Examples\n"})
        self.assertEqual(self._run(), 0)
        tools = self._mechanism_tools()
        self.assertEqual(tools[("skills", ".github/skills/storybook-stories/SKILL.md")],
                         "claude|copilot")
        self.assertEqual(tools[("skills", ".github/skills/storybook-stories/reference/examples.md")],
                         "claude")

    def test_a_linked_skill_md_in_agents_skills_keeps_any_while_several_tools_read_it(self):
        """`.agents/skills` names no tool, because Codex, Copilot, Cursor, Antigravity and
        OpenCode read it. Of a SKILL.md there that links to a file, Codex reads nothing, Cursor
        reads the file only where it lies inside the directory, and Copilot CLI, Antigravity CLI
        and OpenCode read it wherever it lies, as microsoft/beachball links
        `.agents/skills/beachball-change-file/SKILL.md` to
        `skills/beachball-change-file/SKILL.md`. Several tools still read the link, so the skill
        keeps `any` and sets no tool flag, as the directory's own skills do. A `.codex` and a
        `.cursor` that link to `.agents` add Codex and Cursor to the skill that is no link, and
        neither tool to the link out of the directory, which both skip."""
        skill = "---\nname: change\ndescription: Write a change file\n---\nWrite it.\n"
        outside = "../../../skills/change/SKILL.md\n"
        self._repo("o/a", {"skills/change/SKILL.md": skill,
                           ".agents/skills/change/SKILL.md": outside,
                           ".agents/skills/plain/SKILL.md": skill})
        self._repo("o/b", {".agents/skills/real/SKILL.md": skill,
                           ".agents/skills/alias/SKILL.md": "../real/SKILL.md\n"})
        self._repo("o/c", {".codex": ".agents\n", ".cursor": ".agents\n",
                           "skills/change/SKILL.md": skill,
                           ".agents/skills/change/SKILL.md": outside,
                           ".agents/skills/plain/SKILL.md": skill})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(),
                         {("o/a", ".agents/skills/change/SKILL.md"): "any",
                          ("o/a", ".agents/skills/plain/SKILL.md"): "any",
                          ("o/b", ".agents/skills/real/SKILL.md"): "any",
                          ("o/b", ".agents/skills/alias/SKILL.md"): "any",
                          ("o/c", ".agents/skills/change/SKILL.md"): "any",
                          ("o/c", ".agents/skills/plain/SKILL.md"): "codex|cursor"})
        self.assertEqual(self._flags("o/a"), {"no_specific_tool"})
        self.assertEqual(self._flags("o/b"), {"no_specific_tool"})
        self.assertEqual(self._flags("o/c"), {"codex", "cursor"})

    def test_a_shared_file_is_any(self):
        self._repo("o/a", {"AGENTS.md": "# agents\n", ".mcp.json": '{"mcpServers": {"x": {"command": "uvx"}}}',
                           "CLAUDE.md": "# claude\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(), {("o/a", "AGENTS.md"): "any", ("o/a", ".mcp.json"): "any",
                                         ("o/a", "CLAUDE.md"): "claude"})
        self.assertEqual(self._flags("o/a"), {"claude"})

    def test_portable_skills_alone_are_no_specific_tool(self):
        """A repository with `.agents/skills` as its only configuration counts for no specific
        tool, as one with AGENTS.md alone does."""
        self._repo("o/a", {".agents/skills/pdf/SKILL.md": "Read PDFs.\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools(), {("o/a", ".agents/skills/pdf/SKILL.md"): "any"})
        self.assertEqual(self._flags("o/a"), {"no_specific_tool"})

    def test_the_tool_flags_are_the_union_of_the_named_mechanisms_rows_tools(self):
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        self.assertEqual(self._run(), 0)
        other = {(row["repo_name"], row["other"]) for row in self._out_rows("other.csv")}
        union = set().union(*(heuristics.value_tools(t) for key, t in self._tools().items()
                              if key not in other))
        self.assertEqual(self._flags("o/a") - {"no_specific_tool"}, union)

    def test_an_other_row_sets_no_tool_flag(self):
        """VS Code reads a prompt file only as `*.prompt.md`. `.github/prompts/notes.md` is
        therefore an `other` row in Copilot's directory, and not_configuration.csv names it.
        The row does not set the `copilot` flag of a repository such as
        carbon-design-system/carbon, with AGENTS.md as its only other file."""
        self._repo("o/a", {"AGENTS.md": "# agents\n", ".github/prompts/notes.md": "# notes\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._tools().get(("o/a", ".github/prompts/notes.md")), "copilot")
        self.assertEqual(self._flags("o/a"), {"no_specific_tool"})


class TestCodexOverrideAttribution(_StepCase):
    def test_an_override_only_repository_is_codex_configuration(self):
        self._repo("o/override", {"pkg/AGENTS.override.md": "# Package instructions\n"})
        self.assertEqual(self._run(), 0)
        with (self.output / "repos.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual({key for key in ("claude", "codex", "copilot", "cursor", "gemini")
                          if row[key] == "TRUE"}, {"codex"})
        self.assertEqual(row["no_specific_tool"], "FALSE")
        self.assertEqual(row["agents.md"], "TRUE")


class TestMainCli(_StepCase):

    # Written out rather than derived, because this is the published column list of
    # repos.csv, 30 columns.
    REPOS_HEADER = [
        "repo_name", "scanned_at", "branch", "head_sha", "head_commit_date",
        "claude", "gemini", "codex", "copilot", "cursor", "opencode",
        "claude.md", "gemini.md", "agents.md", "copilot-instructions", "cursorrules",
        "context_file", "settings", "skills", "subagents", "commands", "hooks",
        "rules", "mcp", "plugins", "lsp", "other", "no_specific_tool", "github_link",
        "dataset_version",
    ]

    DETAIL_CSVS = ["settings.csv", "context-files.csv", "skills.csv", "subagents.csv",
                   "commands.csv", "hooks.csv", "rules.csv", "mcp.csv", "plugins.csv",
                   "lsp.csv", "other.csv"]

    def test_every_output_csv_is_written(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)
        for name in ["repos.csv", "excluded.csv"] + self.DETAIL_CSVS:
            self.assertTrue((self.output / name).is_file(), name)

    def test_no_extended_twin_is_written(self):
        """One CSV per mechanism, with the Git columns in it, and no
        `output/<mechanism>-extended.csv` beside it with a key set that could diverge."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        twins = sorted(p.name for p in self.output.glob("*-extended.csv"))
        self.assertEqual(twins, [])
        self.assertFalse((self.output / "context-files-ai.csv").exists())

    def test_every_csv_records_the_dataset_version(self):
        # The release publishes the mechanism CSVs to Zenodo as separate files. Each row
        # records the release that produced it rather than relying on a manifest beside the
        # files. The fixture fills every CSV, and a version stamped on only some of them
        # fails.
        self.assertRegex(_mod.DATASET_VERSION, r"^\d+\.\d+\.\d+$")
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        self.assertEqual(self._run(), 0)
        for name in ["repos.csv"] + self.DETAIL_CSVS:
            rows = self._out_rows(name)
            with self.subTest(name):
                self.assertTrue(rows, f"{name} has no rows, so the fixture does not cover it")
                self.assertEqual({r["dataset_version"] for r in rows}, {_mod.DATASET_VERSION})

    def test_every_csv_ends_each_record_with_lf(self):
        """repos.csv and the mechanism CSVs end each record with LF, as every CSV of the
        pipeline does, rather than with the CRLF that the csv module writes by default. The
        fixture fills every CSV."""
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        self.assertEqual(self._run(), 0)
        for name in ["repos.csv"] + self.DETAIL_CSVS:
            with self.subTest(name):
                self.assertTrue(self._out_rows(name),
                                f"{name} has no rows, so the fixture does not cover it")
                self.assertNotIn(b"\r\n", (self.output / name).read_bytes())

    def test_provenance_json_records_the_heuristics(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        import json
        manifest = json.loads((self.output / "provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["dataset_version"], _mod.DATASET_VERSION)
        # The checksum of the file itself, computed here, because a check of its length
        # alone would pass the checksum of any other bytes.
        heuristics_csv = Path(__file__).resolve().parents[1] / "heuristics" / "config_path_patterns.csv"
        self.assertEqual(manifest["heuristics_sha256"],
                         hashlib.sha256(heuristics_csv.read_bytes()).hexdigest())
        self.assertGreater(manifest["pattern_count"], 40)
        # The vendored directories and the places of the local state decide what the collection
        # and this step read, so the record names their checksums too.
        for key, name in (("vendored_dirs_sha256", "vendored_dirs.txt"),
                          ("local_state_sha256", "local_state_paths.csv")):
            with self.subTest(key=key):
                self.assertEqual(manifest[key], hashlib.sha256(
                    (heuristics_csv.parent / name).read_bytes()).hexdigest())

    def test_repos_csv_header(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual(self._out_header("repos.csv"), self.REPOS_HEADER)
        self.assertEqual(len(self.REPOS_HEADER), 30)

    def test_excluded_csv_header(self):
        """The head commit date is `head_commit_date`, and the scan time is a column
        of its own."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual(self._out_header("excluded.csv"),
                         ["repo_name", "scanned_at", "head_commit_date", "reason"])

    def test_flags_for_a_claude_repo(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        row = self._out_rows("repos.csv")[0]
        self.assertEqual(
            {k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
            {"claude", "claude.md", "context_file"})

    def test_flags_for_a_repository_whose_only_configuration_is_a_plugin_manifest(self):
        """The manifest is a plugins row and sets its tool. A detect_other that ignored the
        manifests would write it to other.csv as well, and tool flags that ignored them would
        leave the repository with no tool at all."""
        self._repo("o/a", {".cursor-plugin/plugin.json": '{"name": "demo"}'})
        self.assertEqual(self._run(), 0)
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"cursor", "plugins"})
        self.assertEqual([r["plugin"] for r in self._out_rows("plugins.csv")],
                         [".cursor-plugin/plugin.json"])
        self.assertEqual(self._out_rows("other.csv"), [])

    def test_lsp_rows_set_their_tool_and_are_not_unclassified(self):
        manifest = '{"$schema":"https://agent-plugins.org/schemas/1.0.0/plugin.schema.json","name":"demo"}'
        self._repo("o/a", {".github/lsp.json": '{"lspServers":{}}'})
        self._repo("o/b", {"plugin.json": manifest,
                           "com.github.copilot/lsp.json": '{"lspServers":{}}'})
        self.assertEqual(self._run(), 0)
        rows = {r["repo_name"]: r for r in self._out_rows("repos.csv")}
        for name, expected in (("o/a", {"copilot", "lsp"}),
                               ("o/b", {"copilot", "lsp", "plugins"})):
            self.assertEqual({k for k in self.REPOS_HEADER if rows[name].get(k) == "TRUE"}, expected)
        self.assertEqual(sorted((r["repo_name"], r["lsp"]) for r in self._out_rows("lsp.csv")),
                         [("o/a", ".github/lsp.json"), ("o/b", "com.github.copilot/lsp.json")])
        self.assertEqual(self._out_rows("other.csv"), [])

    def test_followed_markers_set_their_tools_and_are_not_unclassified(self):
        self._repo("o/a", {".claude/skills/pdf/SKILL.md": "Read PDFs.\n",
                           ".cursor/skills": "../.claude/skills\n"})
        self._repo("o/b", {"tools/skills/review/SKILL.md": "Review.\n",
                           ".claude/skills/review": "../../tools/skills/review\n"})
        self.assertEqual(self._run(), 0)
        rows = {r["repo_name"]: r for r in self._out_rows("repos.csv")}
        for name, expected in (("o/a", {"claude", "cursor", "skills"}),
                               ("o/b", {"claude", "skills"})):
            self.assertEqual({k for k in self.REPOS_HEADER if rows[name].get(k) == "TRUE"}, expected)
        self.assertEqual(sorted((r["repo_name"], r["skills.md"]) for r in self._out_rows("skills.csv")),
                         [("o/a", ".claude/skills/pdf/SKILL.md"),
                          ("o/b", "tools/skills/review/SKILL.md")])
        self.assertEqual(self._out_rows("other.csv"), [])

    def test_a_followed_plugin_marker_is_not_unclassified(self):
        self._repo("o/a", {
            ".claude/linked/pdf/SKILL.md": "Read PDFs.\n",
            ".claude/plugins/myplugin/.claude-plugin/plugin.json": '{"name":"myplugin"}',
            ".claude/plugins/myplugin/skills": "../../linked\n",
        })
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["skills.md"] for r in self._out_rows("skills.csv")],
                         [".claude/linked/pdf/SKILL.md"])
        self.assertEqual(self._out_rows("other.csv"), [])
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"claude", "plugins", "skills"})

    def test_an_antigravity_plugin_is_gemini_s_and_not_other(self):
        """The plugins of opendatahub-io/notebooks are under `.agents`, which names no tool.
        A plugin there is Antigravity's and counts as Gemini, as does every part of it that
        the plugins page names."""
        self._repo("o/a", {".agents/plugins/pr-review/plugin.json": '{"name": "pr-review"}',
                           ".agents/plugins/pr-review/mcp_config.json":
                               '{"mcpServers": {"gh": {"command": "gh-mcp"}}}',
                           ".agents/plugins/pr-review/hooks.json": '{"lint": {"Stop": []}}',
                           ".agents/plugins/pr-review/rules/style.md":
                               "---\ntrigger: always_on\n---\nUse types.\n",
                           ".agents/plugins/pr-review/skills/pdf/SKILL.md":
                               "---\ndescription: Reads PDFs.\n---\nRead PDFs.\n",
                           ".agents/plugins/pr-review/agents/reviewer.md":
                               "---\nname: reviewer\ndescription: Reviews\n---\nReview.\n"})
        self.assertEqual(self._run(), 0)
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"gemini", "plugins", "mcp", "hooks", "subagents", "rules", "skills"})
        self.assertEqual([r["plugin"] for r in self._out_rows("plugins.csv")],
                         [".agents/plugins/pr-review/plugin.json"])
        self.assertEqual(self._out_rows("other.csv"), [])

    def test_the_copilot_parts_of_a_portable_plugin(self):
        """An Agent Plugins plugin keeps Copilot's agents, commands, rules and hooks under
        com.github.copilot/. Copilot CLI 1.0.89 loads every Markdown file directly in agents/
        except a README.md, the Markdown files in commands/, and only the .mdc files in rules/
        with a description in their front matter."""
        manifest = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",'
                    ' "name": "demo"}')
        self._repo("o/a", {"plugin.json": manifest,
                           "com.github.copilot/agents/reviewer.agent.md": "Review.\n",
                           "com.github.copilot/agents/README.md": "# Agents\n",
                           "com.github.copilot/agents/notes.md": "Notes.\n",
                           "com.github.copilot/commands/review.prompt.md": "Review.\n",
                           "com.github.copilot/rules/git.mdc": "---\ndescription: Git\n---\nCommit small.\n",
                           "com.github.copilot/rules/always.mdc": "---\nalwaysApply: true\n---\nx\n",
                           "com.github.copilot/rules/git.instructions.md": "Commit small.\n",
                           "com.github.copilot/hooks/hooks.json": '{"version": 1, "hooks": {"sessionStart": [{"command": "true"}]}}'})
        self.assertEqual(self._run(), 0)
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"copilot", "plugins", "subagents", "commands", "rules", "hooks"})
        self.assertEqual([(r["subagent"], r["name"]) for r in self._out_rows("subagents.csv")],
                         [("com.github.copilot/agents/notes.md", "notes"),
                          ("com.github.copilot/agents/reviewer.agent.md", "reviewer")])
        self.assertEqual([r["command"] for r in self._out_rows("commands.csv")],
                         ["com.github.copilot/commands/review.prompt.md"])
        self.assertEqual([r["rule"] for r in self._out_rows("rules.csv")],
                         ["com.github.copilot/rules/git.mdc"])

    def test_a_legacy_copilot_plugin(self):
        """A Copilot CLI plugin with a .plugin manifest has its default commands/ and rules/, a
        root SKILL.md where there is no skills/, and hooks.json, the first of hooks.json and
        hooks/hooks.json. Copilot CLI 1.0.89 listed a README.md in commands/ as the command
        README."""
        self._repo("o/a", {".plugin/plugin.json": '{"name": "demo"}',
                           "SKILL.md": "---\nname: demo\n---\nx\n",
                           "commands/ship.md": "Ship.\n", "commands/sub/deep.md": "x\n",
                           "commands/README.md": "# Commands\n",
                           "rules/style.mdc": "---\ndescription: Style\n---\nx\n",
                           "rules/bare.mdc": "x\n", "rules/style.md": "x\n",
                           "hooks.json": '{"hooks": {"Stop": [{"command": "true"}]}}',
                           "hooks/hooks.json": '{"hooks": {"Stop": [{"command": "true"}]}}'})
        self.assertEqual(self._run(), 0)
        self.assertEqual([(r["skills.md"], r["name"]) for r in self._out_rows("skills.csv")],
                         [("SKILL.md", "a")])
        self.assertEqual([r["command"] for r in self._out_rows("commands.csv")],
                         ["commands/README.md", "commands/ship.md"])
        self.assertEqual([r["rule"] for r in self._out_rows("rules.csv")], ["rules/style.mdc"])
        self.assertEqual([r["hook"] for r in self._out_rows("hooks.csv")], ["hooks.json"])
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"copilot", "plugins", "skills", "commands", "rules", "hooks"})

    def test_a_copilot_root_skill_sets_no_other_tool(self):
        self._repo("o/a", {".plugin/plugin.json": '{"name": "demo"}',
                           "SKILL.md": "---\nname: demo\n---\nx\n"})
        self.assertEqual(self._run(), 0)
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"copilot", "plugins", "skills"})

    def test_a_root_skill_and_what_it_brings(self):
        """tconbeer/harlequin's plugin under src/harlequin/hsql/skill is one skill with
        references/, and a plugin at the repository root claims no more of the repository
        than its skill's own directories. A plugin inside a skills directory is that
        directory's skill as well, and its folder is the skill's."""
        self._repo("o/a", {"src/harlequin/hsql/skill/.claude-plugin/plugin.json": '{"name": "hsql"}',
                           "src/harlequin/hsql/skill/SKILL.md": "---\nname: hsql\n---\nx\n",
                           "src/harlequin/hsql/skill/references/config.md": "x\n"})
        self._repo("o/b", {".claude-plugin/plugin.json": '{"name": "b"}',
                           "SKILL.md": "---\nname: b\n---\nx\n", "scripts/run.sh": "x\n",
                           ".claude/notes.txt": "x\n"})
        # kvcache-ai/Mooncake: a plugin inside a skills directory, with a skill that owns its
        # folder.
        self._repo("o/c", {".claude/skills/ci/.claude-plugin/plugin.json": '{"name": "ci"}',
                           ".claude/skills/ci/SKILL.md": "---\nname: ci\n---\nx\n",
                           ".claude/skills/ci/examples/minimal.md": "x\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(sorted((r["repo_name"], r["skills.md"], r["name"], r["scripts"], r["references"])
                                for r in self._out_rows("skills.csv")),
                         [("o/a", "src/harlequin/hsql/skill/SKILL.md", "skill", "FALSE", "TRUE"),
                          ("o/b", "SKILL.md", "b", "TRUE", "FALSE"),
                          ("o/c", ".claude/skills/ci/SKILL.md", "ci", "FALSE", "FALSE")])
        self.assertEqual([(r["repo_name"], r["other"]) for r in self._out_rows("other.csv")],
                         [("o/b", ".claude/notes.txt")])

    def test_an_agents_md_under_claude_is_claude_s(self):
        """Claude Code reads "every AGENTS.md and .claude/AGENTS.md". The file under .claude is
        therefore Claude's, as .claude/CLAUDE.md is. An AGENTS.md anywhere else names no
        tool."""
        self._repo("o/a", {".claude/AGENTS.md": "# agents\n"})
        self._repo("o/b", {".github/AGENTS.md": "# agents\n", "AGENTS.md": "# agents\n"})
        # Only the documented spelling counts: jdereg/json-io's .claude/agents.md belongs to no
        # tool.
        self._repo("o/c", {".claude/agents.md": "# agents\n", "CLAUDE.md": "# claude\n"})
        self.assertEqual(self._run(), 0)
        rows = {r["repo_name"]: r for r in self._out_rows("repos.csv")}
        self.assertEqual(sorted((r["repo_name"], r["context_file"])
                                for r in self._out_rows("context-files.csv")),
                         [("o/a", ".claude/AGENTS.md"), ("o/b", ".github/AGENTS.md"),
                          ("o/b", "AGENTS.md"), ("o/c", "CLAUDE.md")])
        self.assertEqual({k for k in self.REPOS_HEADER if rows["o/a"].get(k) == "TRUE"},
                         {"claude", "agents.md", "context_file"})
        self.assertEqual({k for k in self.REPOS_HEADER if rows["o/b"].get(k) == "TRUE"},
                         {"agents.md", "context_file", "no_specific_tool"})

    def test_an_agents_md_under_claude_is_claude_s_only_where_claude_code_reads_it(self):
        """Claude Code's memory page: "By default, Claude reads `AGENTS.md` only when you have no
        `CLAUDE.md` in your working directory or above it", and a `CLAUDE.md`, a
        `.claude/CLAUDE.md` or a `CLAUDE.local.md` count. ng-alain/delon has a
        `.claude/CLAUDE.md`, and tsedio/tsed a root `CLAUDE.md`, a link to `AGENTS.md`, and
        neither imports the `.claude/AGENTS.md`, which therefore names no tool, as an AGENTS.md
        elsewhere does. ValidatorsDAO/slv has no CLAUDE file, and the `.claude/CLAUDE.md` of
        keycloakify/keycloakify-angular imports the file with `@AGENTS.md`, which resolves in
        `.claude`."""
        self._repo("o/delon", {".claude/AGENTS.md": "# agents\n",
                               ".claude/CLAUDE.md": "# Delon\n\nRead the docs.\n"})
        self._repo("o/tsed", {".claude/AGENTS.md": "# agents\n", "AGENTS.md": "# Ts.ED\n",
                              "CLAUDE.md": "AGENTS.md"})
        self._repo("o/local", {".claude/AGENTS.md": "# agents\n", "CLAUDE.local.md": "# mine\n"})
        self._repo("o/slv", {".claude/AGENTS.md": "# agents\n"})
        self._repo("o/keycloakify", {".claude/AGENTS.md": "# agents\n",
                                     ".claude/CLAUDE.md": "# Keycloakify\n\n@AGENTS.md\n"})
        self.assertEqual(self._run(), 0)
        tools = {(r["repo_name"], r["context_file"]): r["tool"]
                 for r in self._out_rows("context-files.csv")}
        self.assertEqual({repo: tool for (repo, path), tool in tools.items()
                          if path == ".claude/AGENTS.md"},
                         {"o/delon": "any", "o/tsed": "any", "o/local": "any",
                          "o/slv": "claude", "o/keycloakify": "claude"})

    def test_a_workflow_on_gh_aw_s_cursor_engine_is_cursor_s(self):
        """gh-aw's sample Cursor engine declares id: cursor, and a workflow names it in the
        nested form that the engines page requires for an engine that is not built in."""
        self._repo("o/a", {".github/workflows/triage.md":
                           "---\non: issues\nengine:\n  id: cursor\n---\nTriage.\n"})
        self.assertEqual(self._run(), 0)
        row = self._out_rows("repos.csv")[0]
        self.assertEqual({k for k in self.REPOS_HEADER if row.get(k) == "TRUE"},
                         {"cursor", "settings"})

    def test_documented_on_2026_10_16_agy_windows_review_md(self):
        """gh-aw's agy engine runs Antigravity CLI, which counts as Gemini. Cursor's GitHub
        Actions page installs the CLI on Windows with irm. REVIEW.md, which Claude Code Review
        and Copilot code review both read, names no tool, as AGENTS.md does."""
        self._repo("o/a", {".github/workflows/docs.md": "---\non: push\nengine: agy\n---\nDocs.\n"})
        self._repo("o/b", {".github/workflows/win.yml":
                           "on: push\njobs:\n  go:\n    steps:\n"
                           "      - run: irm 'https://cursor.com/install?win32=true' | iex\n"})
        self._repo("o/c", {"REVIEW.md": "# Review\n"})
        self.assertEqual(self._run(), 0)
        rows = {r["repo_name"]: r for r in self._out_rows("repos.csv")}
        self.assertEqual({k for k in self.REPOS_HEADER if rows["o/a"].get(k) == "TRUE"},
                         {"gemini", "settings"})
        self.assertEqual({k for k in self.REPOS_HEADER if rows["o/b"].get(k) == "TRUE"},
                         {"cursor", "settings"})
        self.assertEqual({k for k in self.REPOS_HEADER if rows["o/c"].get(k) == "TRUE"},
                         {"context_file", "no_specific_tool"})

    def test_no_specific_tool_for_agents_md_alone(self):
        self._repo("o/a", {"AGENTS.md": "# agents\n"})
        self._run()
        row = self._out_rows("repos.csv")[0]
        self.assertEqual(row["no_specific_tool"], "TRUE")
        self.assertEqual(row["agents.md"], "TRUE")
        for tool in ("claude", "gemini", "codex", "copilot", "cursor", "opencode"):
            self.assertEqual(row[tool], "FALSE", tool)

    def test_no_specific_tool_is_false_when_a_tool_is_set(self):
        self._repo("o/a", {"AGENTS.md": "# agents\n", "CLAUDE.md": "# hi\n"})
        self._run()
        row = self._out_rows("repos.csv")[0]
        self.assertEqual(row["no_specific_tool"], "FALSE")
        self.assertEqual(row["agents.md"], "TRUE")

    def test_detail_row_contains_metadata_and_link(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual(row["repo_name"], "o/a")
        self.assertEqual(row["scanned_at"], self.SCANNED_AT)
        self.assertEqual(row["branch"], "main")
        self.assertEqual(row["head_sha"], self.HEAD_SHA)
        self.assertEqual(row["head_commit_date"], self.HEAD_COMMIT_DATE)
        self.assertEqual(row["context_file"], "CLAUDE.md")
        self.assertEqual(row["github_link"],
                         "https://github.com/o/a/tree/main/CLAUDE.md")

    def test_one_detail_row_per_artifact(self):
        self._repo("o/a", {".claude/skills/pdf/SKILL.md": "x\n",
                           ".claude/skills/xlsx/SKILL.md": "x\n"})
        self._run()
        self.assertEqual({r["name"] for r in self._out_rows("skills.csv")}, {"pdf", "xlsx"})
        self.assertEqual(self._out_rows("repos.csv")[0]["skills"], "TRUE")

    def test_input_row_without_a_directory_is_excluded(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._rows.append({"repo_name": "o/ghost", "scanned_at": self.SCANNED_AT,
                           "branch": "main", "head_sha": self.HEAD_SHA,
                           "head_commit_date": self.HEAD_COMMIT_DATE})
        self._write_input(self._rows)
        self._run()
        self.assertEqual([r["repo_name"] for r in self._out_rows("repos.csv")], ["o/a"])
        excluded = self._out_rows("excluded.csv")
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0]["repo_name"], "o/ghost")
        self.assertEqual(excluded[0]["scanned_at"], self.SCANNED_AT)
        self.assertEqual(excluded[0]["head_commit_date"], self.HEAD_COMMIT_DATE)
        self.assertEqual(excluded[0]["reason"], "no_artifacts")

    def test_directory_without_an_input_row_is_detected_but_unlisted(self):
        # The directory contributes rows with blank metadata to the mechanism CSVs but never
        # reaches repos.csv, because the step writes repos.csv from the rows of the validated
        # list.
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._repo("o/extra", {"CLAUDE.md": "# hi\n"}, in_csv=False)
        self._run()
        self.assertEqual([r["repo_name"] for r in self._out_rows("repos.csv")], ["o/a"])
        extra = [r for r in self._out_rows("context-files.csv")
                 if r["repo_name"] == "o/extra"]
        self.assertEqual(len(extra), 1)
        self.assertEqual(extra[0]["scanned_at"], "")
        self.assertEqual(extra[0]["head_commit_date"], "")
        self.assertEqual(extra[0]["branch"], "")
        self.assertEqual(extra[0]["github_link"], "")

    def test_repos_csv_follows_input_order(self):
        for name in ("o/c", "o/a", "o/b"):
            self._repo(name, {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual([r["repo_name"] for r in self._out_rows("repos.csv")],
                         ["o/c", "o/a", "o/b"])

    def test_blank_branch_yields_empty_links(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"}, branch="")
        self._run()
        self.assertEqual(self._out_rows("repos.csv")[0]["github_link"], "")
        self.assertEqual(self._out_rows("context-files.csv")[0]["github_link"], "")

    def test_timestamps_are_converted_to_utc(self):
        """The step converts `head_commit_date`, which git writes with the committer's own
        offset. It does not convert `scanned_at`. The March sample records the first day of the
        collection window there, and converting a date would make an instant at midnight, a
        time that nobody measured."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"},
                   head_commit_date="2026-01-01T02:00:00+02:00",
                   scanned_at="2026-03-29")
        self._run()
        row = self._out_rows("repos.csv")[0]
        self.assertEqual(row["head_commit_date"], "2026-01-01T00:00:00Z")
        self.assertEqual(row["scanned_at"], "2026-03-29")
        detail = self._out_rows("context-files.csv")[0]
        self.assertEqual(detail["head_commit_date"], "2026-01-01T00:00:00Z")
        self.assertEqual(detail["scanned_at"], "2026-03-29")

    def test_a_head_commit_in_a_zone_past_24_hours_is_its_epoch_in_utc(self):
        """git accepts an offset such as +2500 in a commit and prints it as `+25:00` in `%cI`,
        and `datetime` refuses that as a time zone. The step converts the collected value, read
        from a real commit, to the instant of the commit's epoch seconds and exits 0."""
        import subprocess
        from datetime import datetime, timezone
        from agentcontext.git import clone
        from tests._fixtures import _add_commit_in_zone, _init_git_repo
        remote = self.tmp / "remote"
        _init_git_repo(remote)
        _add_commit_in_zone(remote, "CLAUDE.md", "# hi\n", 1700000000, "+2500")
        branch, head_commit_date, head_sha = clone.read_snapshot_metadata(remote)
        self.assertTrue(head_commit_date.endswith("+25:00"), head_commit_date)
        epoch = int(subprocess.run(["git", "-C", str(remote), "show", "-s", "--format=%ct",
                                    "HEAD"], capture_output=True, text=True,
                                   check=True).stdout)
        expected = datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self._repo("o/a", {"CLAUDE.md": "# hi\n"}, branch=branch, head_sha=head_sha,
                   head_commit_date=head_commit_date)
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(self._out_rows("repos.csv")[0]["head_commit_date"], expected)
        self.assertEqual(self._out_rows("context-files.csv")[0]["head_commit_date"], expected)

    # A commit of the year 10000 in UTC, which git accepts, and an instant 30 minutes before
    # the year 10000 in UTC. In the offset -0100 git prints the first as
    # `9999-12-31T23:30:00-01:00`, and in the offset +0100 the second as
    # `10000-01-01T00:30:00+01:00`. Neither parses.
    AFTER_9999 = {"o/minus": ("@253402302600 -0100", "9999-12-31T23:30:00-01:00"),
                  "o/plus": ("@253402299000 +0100", "10000-01-01T00:30:00+01:00")}

    def _repository_dated(self, name: str, date: str) -> Path:
        """Create a git repository with a last commit that adds `CLAUDE.md` at `date`, as git
        takes the date."""
        from tests._fixtures import _add_commit, _init_git_repo
        remote = self.tmp / "remotes" / name.replace("/", "§")
        _init_git_repo(remote)
        _add_commit(remote, "CLAUDE.md", "# hi\n", date=date)
        return remote

    def test_a_head_commit_after_the_year_9999_is_blank_and_the_step_exits_0(self):
        """git prints such a head commit date in a form that does not parse. If the step
        refused the value, one such repository would stop the detection step at every Start,
        and the run would never reach the release."""
        from agentcontext.git import clone
        for name, (date, printed) in self.AFTER_9999.items():
            branch, head_commit_date, head_sha = clone.read_snapshot_metadata(
                self._repository_dated(name, date))
            self.assertEqual(head_commit_date, printed)
            self._repo(name, {"CLAUDE.md": "# hi\n"}, branch=branch, head_sha=head_sha,
                       head_commit_date=head_commit_date)
        self._repo("o/dated", {"CLAUDE.md": "# hi\n"},
                   head_commit_date="2026-01-01T02:00:00+02:00")
        self.assertEqual(self._run(), 0, self.stderr)
        expected = {"o/minus": "", "o/plus": "", "o/dated": "2026-01-01T00:00:00Z"}
        for name in ("repos.csv", "context-files.csv"):
            with self.subTest(csv=name):
                self.assertEqual({row["repo_name"]: row["head_commit_date"]
                                  for row in self._out_rows(name)}, expected)
        warning = next(line for line in self.stderr.splitlines()
                       if "head_commit_date value(s) that are not" in line)
        self.assertTrue(warning.startswith("WARNING detection: "), warning)
        self.assertIn("had 2 head_commit_date value(s)", warning)
        self.assertTrue(warning.endswith("in 2 repositories (o/minus, o/plus)."), warning)

    def test_a_creation_commit_after_the_year_9999_leaves_created_at_blank(self):
        """`file_history.csv` keeps git's text for the date of such a creation commit. If the
        step published the date unconverted, the file history step would stop at it on every
        Start. The other Git columns of the row and the rows of other repositories stay."""
        from agentcontext.git import history
        for name, (date, printed) in self.AFTER_9999.items():
            remote = self._repository_dated(name, date)
            with contextlib.redirect_stderr(io.StringIO()):
                facts = history.history_from(
                    history.GitFileMetadata().get_path_commits(str(remote), "CLAUDE.md"))
            self.assertEqual(facts.created_at, printed)
            self._repo(name, {"CLAUDE.md": "# hi\n"})
            self._git_fact(name, "CLAUDE.md", created_at=facts.created_at,
                           commits=facts.commits, first_commit_sha=facts.first_commit_sha,
                           last_commit_sha=facts.last_commit_sha)
        self._repo("o/dated", {"CLAUDE.md": "# hi\n"})
        self._git_fact("o/dated", "CLAUDE.md")
        self.assertEqual(self._run(), 0, self.stderr)
        rows = {row["repo_name"]: row for row in self._out_rows("context-files.csv")}
        self.assertEqual({name: row["created_at"] for name, row in rows.items()},
                         {"o/minus": "", "o/plus": "", "o/dated": "2026-02-01T00:00:00Z"})
        for name in self.AFTER_9999:
            with self.subTest(repo=name):
                self.assertEqual(rows[name]["#commits"], "1")
                self.assertRegex(rows[name]["first_commit_sha"], "^[0-9a-f]{40}$")
                self.assertEqual(rows[name]["last_commit_sha"], rows[name]["first_commit_sha"])
        warning = next(line for line in self.stderr.splitlines()
                       if "created_at value(s) that are not" in line)
        self.assertTrue(warning.startswith("WARNING detection: "), warning)
        self.assertTrue(warning.endswith("in 2 repositories (o/minus, o/plus)."), warning)

    def test_a_commit_timestamp_after_the_year_9999_is_blank_in_the_commit_csv(self):
        """The cloning step keeps the author date as git prints it in UTC, and for a commit of
        the year 10000 the year has five digits. A reader of the published CSV that parses
        dates would stop at that row. A commit 30 minutes before the year 10000 in UTC keeps
        its date, as any other commit does."""
        import subprocess
        printed = {}
        for name, (date, _) in self.AFTER_9999.items():
            remote = self._repository_dated(name, date)
            printed[name] = subprocess.run(
                ["git", "-C", str(remote), "log", "-1",
                 "--date=format-local:%Y-%m-%dT%H:%M:%SZ", "--format=%ad"],
                capture_output=True, text=True, check=True,
                env={**os.environ, "TZ": "UTC"}).stdout.strip()
        self.assertEqual(printed, {"o/minus": "10000-01-01T00:30:00Z",
                                   "o/plus": "9999-12-31T23:30:00Z"})
        printed["o/dated"] = "2026-01-01T00:00:00Z"
        commits_dir = self.tmp / "collect_commits"
        commits_dir.mkdir()
        for name, timestamp in printed.items():
            shard = commits_dir / (name.replace("/", "§") + ".csv")
            with shard.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(_mod.commits.COMMIT_COLUMNS),
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerow({c: "" for c in _mod.commits.COMMIT_COLUMNS}
                                | {"repo_name": name, "commit_timestamp": timestamp,
                                   "commit_sha": "2" * 40, "ai_tool": "Claude"})
            shard.with_suffix(".json").write_text(json.dumps(
                {"repo_name": name, "outcome": "history_read", "commits_read": 1,
                 "commits_matched": 1, "unmatched": [], "no_signal_shas": [],
                 "ai_rows": []}) + "\n")
        CSV = self.output / "all_ai_commits.csv"
        CSV.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _mod.restrict_ai_commits(CSV, set(printed), commits_dir)
        with CSV.open(newline="", encoding="utf-8") as f:
            rows = {row["repo_name"]: row["commit_timestamp"] for row in csv.DictReader(f)}
        self.assertEqual(rows, {"o/minus": "", "o/plus": "9999-12-31T23:30:00Z",
                                "o/dated": "2026-01-01T00:00:00Z"})
        warning = next(line for line in err.getvalue().splitlines()
                       if "commit_timestamp value(s) that are not" in line)
        self.assertTrue(warning.startswith("WARNING detection: all_ai_commits.csv had 1 "),
                        warning)
        self.assertTrue(warning.endswith("in 1 repositories (o/minus)."), warning)

    def test_extra_input_column_is_preserved(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._write_input(
            [{"repo_name": "o/a", "scanned_at": self.SCANNED_AT, "branch": "main",
              "head_sha": self.HEAD_SHA, "head_commit_date": self.HEAD_COMMIT_DATE,
              "note": "kept"}],
            fieldnames=META_COLUMNS + ["note"])
        self._run()
        self.assertEqual(self._out_rows("repos.csv")[0]["note"], "kept")

    def test_output_dir_is_created(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertFalse(self.output.exists())
        self._run()
        self.assertTrue(self.output.is_dir())

    def test_a_missing_validated_list_returns_nonzero(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.validated_csv.unlink()
        self.assertNotEqual(self._run(), 0)
        self.assertIn("Validated list not found", self.stderr)

    def test_a_missing_validated_tree_returns_nonzero(self):
        # The list is inside the tree. Removing the tree removes the list with it, and the
        # first guard stops the step. The next case covers the second guard.
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        shutil.rmtree(self.validated)
        self.assertNotEqual(self._run(), 0)

    def test_a_readable_list_over_an_absent_tree_returns_nonzero(self):
        # The second guard on its own: a list that the step can read while the tree that it
        # names is gone. Without the guard, the step would read nothing, write empty CSVs and
        # exit 0, and the runner would record a step that succeeded.
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        _mod.VALIDATED_DIR = self.tmp / "data" / "absent"
        self.assertNotEqual(self._run(), 0)
        self.assertIn("Validated tree not found", self.stderr)

    def test_a_tree_of_nothing_but_the_list_returns_nonzero(self):
        # A tree with the list and no repository is empty, and `expected=VALIDATED_TREE_FILES`
        # must not change that. Reading the list as one entry would let the step run over
        # nothing and exit 0.
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        shutil.rmtree(self.validated / "o§a")
        self.assertNotEqual(self._run(), 0)
        self.assertIn("No repository directory found", self.stderr)

    def test_rerun_is_byte_identical(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n", ".claude/skills/pdf/SKILL.md": "x\n",
                           ".cursorignore": "x\n"})
        # The step joins the Git and AI columns, rather than a later step appending them. A
        # rerun is byte-identical only if the joins are.
        self._git_fact("o/a", "CLAUDE.md", "context_file")
        self._git_fact("o/a", ".claude/skills/pdf/SKILL.md", "skills")
        self._ai_fact("o/a", "CLAUDE.md")
        self._run()
        first = {name: (self.output / name).read_text()
                 for name in ["repos.csv"] + self.DETAIL_CSVS}
        self._run()
        for name, text in first.items():
            self.assertEqual((self.output / name).read_text(), text, name)


# A fixture that produces at least one row in each of the output CSVs.
ONE_ARTIFACT_PER_TABLE = {
    "CLAUDE.md": "# hi\n",
    ".claude/settings.json": '{"a": 1}',
    ".claude/skills/s/SKILL.md": "# s\n",
    ".claude/agents/x.md": "---\nname: x\ndescription: Does x\n---\n# x\n",
    ".cursor/commands/c.md": "# c\n",
    ".github/hooks/h.json": '{"hooks": {"PreToolUse": [{"command": "true"}]}}',
    ".cursor/rules/r.mdc": "# r\n",
    ".mcp.json": '{"mcpServers": {"x": {"command": "uvx"}}}',
    ".claude-plugin/plugin.json": '{"name": "demo"}',
    ".github/lsp.json": '{"lspServers": {}}',
    ".claude/notes.txt": "scratch",
}


class TestLinkedDirectoryOther(_StepCase):
    """other.csv names the path at which a tool reads a file behind a symlinked directory."""

    def test_a_file_behind_a_link_records_the_link_path(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n",
                           ".claude/skills": "../skills\n",
                           "skills/pdf/SKILL.md": "---\nname: pdf\n---\n",
                           "skills/README.md": "# Skills\n",
                           ".claude/notes.txt": "scratch"})
        self.assertEqual(self._run(), 0)
        self.assertEqual({row["other"]: row["link_path"] for row in self._out_rows("other.csv")},
                         {"skills/README.md": ".claude/skills/README.md",
                          ".claude/notes.txt": ""})

    def test_a_file_in_a_spec_directory_keeps_its_own_path(self):
        """A link from .claude/skills to .agents/skills reaches files that `other` reads in
        .agents already, and the check judges them at that path, as it would without the
        link."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n",
                           ".claude/skills": "../.agents/skills\n",
                           ".agents/skills/pdf/SKILL.md": "---\nname: pdf\n---\n",
                           ".agents/skills/README.md": "# Skills\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual({row["other"]: row["link_path"] for row in self._out_rows("other.csv")},
                         {".agents/skills/README.md": ""})


class TestALinkedToolDirectory(_StepCase):
    """A tool directory that was a link is a marker in the validated tree, and the directory
    that it leads to is collected at its own path. comet-ml/opik links `.cursor` and `.codex` to
    `.agents`, and Cursor reads `.agents/agents/*.md` through `.cursor/agents`. A step that read
    no mechanism directory below the marker would give those agents to Antigravity alone and
    report nothing of a `.claude` that links to `config/claude`."""

    AGENT = "---\nname: {0}\ndescription: Does {0}.\n---\nBody.\n"
    FILES = {".cursor": ".agents\n", ".codex": ".agents\n", ".claude": "config/claude\n",
             ".agents/agents/planner.md": AGENT.format("planner"),
             ".agents/skills/docs/SKILL.md": AGENT.format("docs"),
             "config/claude/agents/reviewer.md": AGENT.format("reviewer"),
             "config/claude/settings.json": '{"permissions": {"allow": ["Bash(make:*)"]}}\n',
             "config/claude/README.md": "# Claude\n"}

    def test_each_tool_is_credited_with_what_it_reads_through_its_link(self):
        self._repo("comet-ml/opik", self.FILES)
        self.assertEqual(self._run(), 0)
        self.assertEqual({row["subagent"]: row["tool"] for row in self._out_rows("subagents.csv")},
                         {".agents/agents/planner.md": "gemini|cursor",
                          "config/claude/agents/reviewer.md": "claude"})
        self.assertEqual({row["skills.md"]: row["tool"] for row in self._out_rows("skills.csv")},
                         {".agents/skills/docs/SKILL.md": "codex|cursor"})
        self.assertEqual({row["settings"]: row["tool"] for row in self._out_rows("settings.csv")},
                         {"config/claude/settings.json": "claude"})
        self.assertEqual({(row["other"], row["link_path"]): row["tool"]
                          for row in self._out_rows("other.csv")},
                         {("config/claude/README.md", ".claude/README.md"): "claude"})
        repo = self._out_rows("repos.csv")[0]
        self.assertEqual({tool: repo[tool] for tool in ("claude", "gemini", "codex", "copilot",
                                                        "cursor", "opencode")},
                         {"claude": "TRUE", "gemini": "TRUE", "codex": "TRUE",
                          "copilot": "FALSE", "cursor": "TRUE", "opencode": "FALSE"})

    def test_a_plan_behind_the_link_is_a_planning_document(self):
        """A plan in the directory that `.claude` links to is a plan of Claude Code, as
        samanhappy/mcphub's `.agents/plans/cleartext-logging-fix.md` is with `.claude ->
        .agents`. Matched at its own path alone, `config/claude/plans/fix.md` would be an
        `other` row of Claude."""
        self._repo("comet-ml/opik", {**self.FILES, "config/claude/plans/fix.md": "# Fix\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual([(row["planning_document"], row["framework"], row["document"])
                          for row in self._out_rows("planning-documents.csv")],
                         [("config/claude/plans/fix.md", "claude", "plan")])
        self.assertEqual([row["other"] for row in self._out_rows("other.csv")],
                         ["config/claude/README.md"])


class TestHeadShaPropagation(_StepCase):
    """head_sha reaches repos.csv and every mechanism CSV."""

    DETAIL_CSVS = ("settings.csv", "context-files.csv", "skills.csv",
                   "subagents.csv", "commands.csv", "hooks.csv", "rules.csv",
                   "mcp.csv", "plugins.csv", "lsp.csv", "other.csv")

    def test_repos_csv_contains_head_sha(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("repos.csv")[0]["head_sha"], "a" * 40)

    def test_context_files_csv_contains_head_sha(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertEqual(self._out_rows("context-files.csv")[0]["head_sha"], "a" * 40)

    def test_every_detail_csv_has_the_column(self):
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        self._run()
        for name in self.DETAIL_CSVS:
            with self.subTest(csv=name):
                rows = self._out_rows(name)
                self.assertTrue(rows, f"{name} has no rows, so the fixture does not cover it")
                self.assertEqual(rows[0]["head_sha"], "a" * 40)

    def test_the_metadata_block_leads_every_detail_csv(self):
        """Every CSV starts with the five metadata columns in this order. They are written out
        rather than read from the heuristics, because this is the published layout."""
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        self._run()
        for name in self.DETAIL_CSVS:
            with self.subTest(csv=name):
                self.assertEqual(
                    self._out_header(name)[:5],
                    ["repo_name", "scanned_at", "branch", "head_sha",
                     "head_commit_date"])

    def test_each_repo_keeps_its_own_sha(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"}, head_sha="a" * 40)
        self._repo("o/b", {"CLAUDE.md": "# b\n"}, head_sha="b" * 40)
        self._run()
        rows = {r["repo_name"]: r for r in self._out_rows("repos.csv")}
        self.assertEqual(rows["o/a"]["head_sha"], "a" * 40)
        self.assertEqual(rows["o/b"]["head_sha"], "b" * 40)

    def test_repo_dir_absent_from_input_csv_gets_blank_sha(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self._repo("o/orphan", {"CLAUDE.md": "# o\n"}, in_csv=False)
        self._run()
        rows = {r["repo_name"]: r for r in self._out_rows("context-files.csv")}
        self.assertEqual(rows["o/a"]["head_sha"], "a" * 40)
        self.assertEqual(rows["o/orphan"]["head_sha"], "")


class TestTheInputListMustContainTheMetadata(_StepCase):
    """The step refuses a validated list without a metadata column before it writes anything.

    A DictWriter fills a name that its row does not have with a blank. A validated list that
    stopped naming a metadata column would therefore publish an empty column in every output
    CSV and in repos.csv without failing. `head_sha` is also the key on which the step joins
    the file history. A blank `head_sha` matches no row and leaves every Git column blank.
    """

    def _input_without(self, column: str) -> None:
        """Write a validated list with every metadata column except `column`."""
        fieldnames = [c for c in META_COLUMNS if c != column]
        values = {"repo_name": "o/a", "scanned_at": self.SCANNED_AT, "branch": "main",
                  "head_sha": self.HEAD_SHA,
                  "head_commit_date": self.HEAD_COMMIT_DATE}
        # read_validated_csv keys its rows on repo_name, and a data row without that column
        # cannot be written at all. The check reads the header.
        rows = [] if column == "repo_name" else [{c: values[c] for c in fieldnames}]
        self._write_input(rows, fieldnames)

    def test_every_metadata_column_is_required(self):
        for column in META_COLUMNS:
            with self.subTest(column=column):
                self._repo("o/a", {"CLAUDE.md": "# hi\n"}, in_csv=False)
                self._input_without(column)
                self.assertNotEqual(self._run(), 0)
                self.assertIn(column, self.stderr)

    def test_nothing_is_written_when_a_column_is_missing(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"}, in_csv=False)
        self._input_without("head_commit_date")
        self._run()
        self.assertFalse(self.output.exists(),
                         "a refused input list still produced an output directory")

    def test_the_refusal_names_the_header_it_read(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"}, in_csv=False)
        self._input_without("scanned_at")
        self._run()
        self.assertIn("head_sha", self.stderr)

    def test_the_five_columns_are_accepted(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)


class TestMarkersUnderARelativeRepoPath(unittest.TestCase):
    """Markers at every directory mechanism, reached as a real run reaches them.

    main() and the keep check of the collected file cleanup both build a relative repository
    path. _resolve_dir returns an absolute path when it follows a marker, and relative_to()
    against a relative root would raise. The cases build markers at .github/instructions,
    .codex/environments, .github/hooks and other places. They run with the working directory
    inside a temporary tree and a relative path, as the pipeline does.
    """

    MARKERS = {
        "ctx": (".github/instructions", "../.claude/instructions",
                ".claude/instructions/a.instructions.md", "# a\n"),
        "settings": (".codex/environments", "../.claude/envs",
                     ".claude/envs/e.toml", "x = 1\n"),
        "hooks": (".github/hooks", "../.claude/hooksdir",
                  ".claude/hooksdir/h.json", '{"hooks": {"a": [{"command": "true"}]}}'),
        "skills": (".claude/skills", "../.cursor/skills",
                   ".cursor/skills/s/SKILL.md", "---\nname: s\n---\n"),
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="markers_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cwd = Path.cwd()
        (self.tmp / "data" / "validated").mkdir(parents=True)
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, self.cwd)

    def _build(self, name, marker, target, artifact, body):
        repo = Path("data/validated") / name   # relative, as main() builds it
        (repo / artifact).parent.mkdir(parents=True, exist_ok=True)
        (repo / artifact).write_text(body)
        (repo / marker).parent.mkdir(parents=True, exist_ok=True)
        (repo / marker).write_text(target)
        return repo

    def test_each_mechanism_is_found_through_its_marker(self):
        # The case asserts the whole result, not only that there is one. A detector that
        # reported the file under the link, as .github/hooks/h.json, would pass a check that
        # something was found.
        detectors = {"ctx": detect_context_files, "settings": detect_settings,
                     "hooks": detect_hooks,
                     "skills": lambda repo: [r["skills.md"] for r in _mod.detect_skills(repo)]}
        for key, (marker, target, artifact, body) in self.MARKERS.items():
            with self.subTest(mechanism=key):
                repo = self._build(f"o§{key}", marker, target, artifact, body)
                self.assertEqual(detectors[key](repo), [artifact])

    def test_the_artifact_is_reported_at_its_real_path(self):
        # Never under the link: a link path exists in no tree. Its github_link returns 404,
        # and the archive contains nothing at that path.
        marker, target, artifact, body = self.MARKERS["ctx"]
        repo = self._build("o§ctx", marker, target, artifact, body)
        self.assertEqual(detect_context_files(repo), [artifact])

    def test_a_relative_repo_path_does_not_raise(self):
        # The case asserts the result as well. A check that returns False for every relative
        # path raises nothing and would empty the sample.
        for key, (marker, target, artifact, body) in self.MARKERS.items():
            with self.subTest(mechanism=key):
                repo = self._build(f"o§rel-{key}", marker, target, artifact, body)
                self.assertTrue(_mod.has_detected_mechanism(repo))   # the cleanup keep check

    def test_the_same_artifact_is_not_reported_twice(self):
        marker, target, artifact, body = self.MARKERS["skills"]
        repo = self._build("o§dup", marker, target, artifact, body)
        paths = [r["skills.md"] for r in _mod.detect_skills(repo)]
        self.assertEqual(len(paths), len(set(paths)))

    def test_a_marker_that_resolves_nowhere_finds_nothing(self):
        for name, target in (("dangling", "../nowhere"), ("outside", "../../../etc")):
            with self.subTest(form=name):
                repo = Path("data/validated") / f"o§{name}"
                (repo / ".github").mkdir(parents=True)
                (repo / ".github" / "instructions").write_text(target)
                self.assertEqual(detect_context_files(repo), [])

    def test_a_marker_pointing_at_a_file_finds_nothing(self):
        repo = Path("data/validated") / "o§tofile"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "target.md").write_text("x")
        (repo / ".github").mkdir(parents=True)
        (repo / ".github" / "instructions").write_text("../.claude/target.md")
        self.assertEqual(detect_context_files(repo), [])

    def test_a_cycle_of_markers_terminates_and_finds_nothing(self):
        repo = Path("data/validated") / "o§cycle"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".cursor").mkdir(parents=True)
        (repo / ".claude" / "skills").write_text("../.cursor/skills")
        (repo / ".cursor" / "skills").write_text("../.claude/skills")
        self.assertEqual(_mod.detect_skills(repo), [])

    def test_a_real_directory_is_unaffected(self):
        repo = Path("data/validated") / "o§plain"
        (repo / ".github" / "instructions").mkdir(parents=True)
        (repo / ".github" / "instructions" / "p.instructions.md").write_text("# p")
        self.assertEqual(detect_context_files(repo),
                         [".github/instructions/p.instructions.md"])


class TestAStrayDirectoryIsNotARepository(_StepCase):
    """The step does not read a directory of the validated tree that holds no repository as a
    repository. Otherwise, the directory would get rows in every mechanism CSV under its own
    name, and a repository that nobody collected would reach the published dataset."""

    def _stray(self, name: str, rel: str = "CLAUDE.md") -> Path:
        p = self.validated / name / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# stray\n", encoding="utf-8")
        return p

    def test_a_stray_gets_no_row_in_the_main_csv(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self._stray("notes")
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._out_rows("repos.csv")], ["o/a"])

    def test_a_stray_gets_no_row_in_a_detail_csv(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self._stray("notes")
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._out_rows("context-files.csv")], ["o/a"])

    def test_a_nested_reconstruction_does_not_become_a_repository(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self._stray("repos", "o§b/CLAUDE.md")
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._out_rows("repos.csv")], ["o/a"])

    def test_a_stray_is_reported_rather_than_skipped_in_silence(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self._stray("notes")
        self._run()
        # The step logs a warning, which agentcontext/logs.py writes to stderr.
        self.assertIn("notes", self.stderr)

    def test_a_directory_of_only_strays_is_an_error(self):
        self._write_input([])
        self._stray("notes")
        self.assertNotEqual(self._run(), 0)


class TestFilesNestedTooDeeplyToParse(_StepCase):
    """A file of one repository that is nested too deeply for the parser raises
    RecursionError. The step catches it where it catches a parse error, because an uncaught
    RecursionError would stop the detection over the whole sample. Such a file reads as one
    that does not parse."""

    def test_the_step_detects_the_rest_and_exits_0(self):
        self.assertRaises(RecursionError, json.loads, DEEP_JSON)
        self.assertRaises(RecursionError, tomllib.loads, DEEP_TOML)
        self._repo("o/deep", deeply_nested_files())
        self._repo("o/b", {"CLAUDE.md": "# b\n"})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(sorted(r["repo_name"] for r in self._out_rows("repos.csv")),
                         ["o/b", "o/deep"])
        context = {(r["repo_name"], r["context_file"])
                   for r in self._out_rows("context-files.csv")}
        self.assertIn(("o/deep", "CLAUDE.md"), context)
        mcp = {(r["repo_name"], r["mcp"]) for r in self._out_rows("mcp.csv")}
        self.assertNotIn(("o/deep", ".codex/config.toml"), mcp)
        self.assertNotIn(("o/deep", "mcp.json"), mcp)


class TestFilesWithIntegersTooLongToParse(_StepCase):
    """A TOML or JSON file of one repository with an integer literal longer than Python
    converts raises a ValueError that is no TOMLDecodeError. Every place that parses a file,
    the Codex MCP test included, catches it where it catches a parse error, because an
    uncaught ValueError would stop the detection over the whole sample at every Start. Such a
    file reads as one that does not parse."""

    def test_the_step_detects_the_rest_and_exits_0(self):
        for parse, text, decode_error in ((json.loads, LONG_INTEGER_JSON, json.JSONDecodeError),
                                          (tomllib.loads, LONG_INTEGER_TOML,
                                           tomllib.TOMLDecodeError)):
            with self.assertRaises(ValueError) as caught:
                parse(text)
            self.assertNotIsInstance(caught.exception, decode_error)
        self._repo("o/long", files_with_long_integers())
        self._repo("o/b", {"CLAUDE.md": "# b\n"})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(sorted(r["repo_name"] for r in self._out_rows("repos.csv")),
                         ["o/b", "o/long"])
        context = {(r["repo_name"], r["context_file"])
                   for r in self._out_rows("context-files.csv")}
        self.assertIn(("o/long", "CLAUDE.md"), context)
        mcp = {(r["repo_name"], r["mcp"]) for r in self._out_rows("mcp.csv")}
        self.assertNotIn(("o/long", ".codex/config.toml"), mcp)
        self.assertNotIn(("o/long", "mcp.json"), mcp)


class TestTheValidatedListIsNotAStray(_StepCase):
    """The list is at the root of the tree that it lists, and the listing has to expect it.

    `repos.split` is the one function that reports an entry that nobody collected. It tests
    whether an entry is a repository, not whether it is a directory. Without
    `VALIDATED_TREE_FILES`, the step's own input would come back as a stray on every run, and
    the operator would learn to ignore the warning that catches a real accident.
    """

    def test_the_list_in_the_tree_is_not_reported_as_a_stray(self):
        # The step writes the warning to stderr, never to stdout.
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self.assertEqual(self._run(), 0)
        self.assertNotIn("are not repository directories", self.stderr)

    def test_the_list_in_the_tree_gets_no_row_of_its_own(self):
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        self.assertEqual(self._run(), 0)
        self.assertEqual([r["repo_name"] for r in self._out_rows("repos.csv")], ["o/a"])

    def test_a_real_stray_is_still_reported_beside_it(self):
        # The other direction: naming the list must not turn the warning off for
        # everything else. Asserting only the first case would pass for a listing that
        # reports no stray at all.
        self._repo("o/a", {"CLAUDE.md": "# a\n"})
        (self.validated / "notes").mkdir()
        self._run()
        self.assertIn("not repository directories", self.stderr)
        self.assertIn("notes", self.stderr)


class TestTheValidatedPathsAgree(unittest.TestCase):
    """Two constants name one file, and a rename that moves only one of them puts the step's
    own input back in the stray warning."""

    def test_the_name_the_cloning_step_expects_is_the_list_the_step_reads(self):
        self.assertIn(Path(_mod.VALIDATED_CSV).name, _mod.VALIDATED_TREE_FILES)

    def test_the_list_is_at_the_root_of_the_tree_it_lists(self):
        self.assertEqual(Path(_mod.VALIDATED_CSV).parent, Path(_mod.VALIDATED_DIR))


class TestAMarkerCannotEscapeTheRepository(_StepCase):
    """_resolve_dir keeps a followed symlink marker inside the collected tree. Without that
    bound, a marker would resolve outside the repository, _repo_relative would raise, and the
    step would stop in the middle of the run. The same escape would also attribute one
    repository's configuration artifacts to another."""

    def _marker(self, repo_name, rel, target):
        repo = self._repo(repo_name, {})
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(target, encoding="utf-8")
        return repo

    def test_a_marker_pointing_outside_the_tree_is_ignored(self):
        # The SKILL.md is one directory down. A SKILL.md directly in the skills root names no
        # skill. A fixture that put it there would report nothing whether the bound holds or
        # not, and the test would pass for the wrong reason.
        outside = self.tmp / "outside" / "skills" / "pdf"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("# x\n", encoding="utf-8")
        repo = self._marker("o/a", ".claude/skills", "../../../outside/skills")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("skills.csv"), [])
        self.assertIsNone(_mod._resolve_dir(repo, ".claude/skills"))

    def test_a_chain_of_markers_cannot_leave_the_repository(self):
        # The resolver follows a chain, such as from .codex/skills to ../.claude/skills and on
        # to ../.agents/skills. The bound therefore has to hold at every hop, not only at the
        # first.
        outside = self.tmp / "outside" / "skills" / "pdf"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("# x\n", encoding="utf-8")
        repo = self._marker("o/a", ".claude/skills", "../shared/link")
        (repo / "shared").mkdir(parents=True, exist_ok=True)
        (repo / "shared" / "link").write_text("../../../outside/skills", encoding="utf-8")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("skills.csv"), [])
        self.assertIsNone(_mod._resolve_dir(repo, ".claude/skills"))

    def test_the_step_still_finishes(self):
        self._marker("o/a", ".claude/skills", "../../../outside/skills")
        self._repo("o/b", {"CLAUDE.md": "# b\n"})
        self.assertEqual(self._run(), 0)
        self.assertIn("o/b", [r["repo_name"] for r in self._out_rows("repos.csv")])

    def test_a_marker_pointing_at_a_sibling_repository_is_ignored(self):
        self._repo("o/victim", {".claude/skills/pdf/SKILL.md": "# v\n"})
        self._marker("o/thief", ".claude/skills", "../../o§victim/.claude/skills")
        self.assertEqual(self._run(), 0)
        owners = {r["repo_name"] for r in self._out_rows("skills.csv")}
        self.assertNotIn("o/thief", owners)

    def test_a_marker_inside_the_repository_is_still_followed(self):
        repo = self._repo("o/a", {"shared/skills/web/SKILL.md": "# s\n"})
        p = repo / ".claude" / "skills"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("../shared/skills", encoding="utf-8")
        self.assertEqual(self._run(), 0)
        self.assertTrue(self._out_rows("skills.csv"))


class TestTheContentFormatComesFromTheHeuristics(_StepCase):
    """The step reads a file as the tool that owns it reads it, and in no other way.

    Gemini CLI reads `.gemini/settings.json` with `JSON.parse(stripJsonComments(content))`.
    Claude Code documents its own settings file as strict JSON, where "a `//` comment or a
    trailing comma is a syntax error". A repository can declare `mcpServers` in a
    `.gemini/settings.json` that departs from strict JSON only by a comment line. If the step
    read the file as strict JSON, the repository would be published as having no MCP."""

    WITH_COMMENT = ('// ~/.gemini/settings.json\n'
                    '{\n  "mcpServers": {\n'
                    '    "github": {"url": "https://api.example.com/mcp"}\n  }\n}\n')

    def test_a_gemini_settings_file_with_a_comment_is_read(self):
        self._repo("o/a", {".gemini/settings.json": self.WITH_COMMENT})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([r["mcp"] for r in self._out_rows("mcp.csv")],
                         [".gemini/settings.json"])

    def test_a_nested_gemini_settings_file_is_read_the_same_way(self):
        """`.gemini` is a nestable root, and the same file also appears below the root. The
        format belongs to the file rather than to the one path at which the heuristics
        declare it."""
        self._repo("o/a", {"sub/.gemini/settings.json": self.WITH_COMMENT})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([r["mcp"] for r in self._out_rows("mcp.csv")],
                         ["sub/.gemini/settings.json"])

    def test_a_claude_settings_file_with_a_comment_is_not_read(self):
        """Claude Code reports that file as a Settings Error, and it never applies the hooks
        declared in the file."""
        self._repo("o/a", {".claude/settings.json":
                           '{\n  // why\n  "hooks": {"PostToolUse": [{"matcher": "Edit"}]}\n}\n'})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(self._out_rows("hooks.csv"), [])

    def test_a_commented_out_block_is_not_a_detection(self):
        """The sample contains this form: a block commented out under a TODO. As JSONC, the
        file parses, and the step finds the hooks outside the comment but not the servers
        inside it, as the tool does. As strict JSON, the file yields neither."""
        self._repo("o/a", {".gemini/settings.json":
                           '{\n  "hooks": {"BeforeTool": [{"matcher": "x", "hooks": '
                           '[{"type": "command", "command": "./check.sh"}]}]}\n'
                           '  // TODO: instate later\n'
                           '  // , "mcpServers": {"a": {}}\n}\n'})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([r["hook"] for r in self._out_rows("hooks.csv")],
                         [".gemini/settings.json"])
        self.assertEqual(self._out_rows("mcp.csv"), [])

    def test_an_extension_manifest_with_a_comment_is_not_read(self):
        """Gemini CLI reads its settings file with comments stripped and its extension
        manifest without: `loadExtensionConfig` calls `JSON.parse(configContent)`. An
        extension with a comment in its manifest does not load, and the tool never starts
        its servers."""
        self._repo("o/a", {"gemini-extension.json": self.WITH_COMMENT})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(self._out_rows("mcp.csv"), [])

    def test_a_file_that_is_broken_for_its_own_tool_is_still_no_detection(self):
        self._repo("o/a", {".gemini/settings.json": '{"mcpServers": {"a": }}'})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(self._out_rows("mcp.csv"), [])

    def test_a_url_survives_reading_the_settings_file(self):
        """A regular expression that deletes from `//` to the end of the line would leave
        `"url": "https:`, and the file would stop parsing for a new reason."""
        self._repo("o/a", {".gemini/settings.json":
                           '{"mcpServers": {"github": {"url": "https://api.example.com/mcp"}}}'})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([r["mcp"] for r in self._out_rows("mcp.csv")],
                         [".gemini/settings.json"])


# ---------------------------------------------------------------------------
# Symlinked mechanism directories, chat modes, and the unclassified files that they leave
# ---------------------------------------------------------------------------

class TestEachRowIsAttributedByItsMechanism(_RepoCase):
    """A row credits a tool when a heuristic of that tool and of the row's mechanism reads the
    path, where it is or through a symlink marker. The path alone, or a marker that credited
    its tool with everything below the linked directory, would credit the rows below
    wrongly."""

    def _marker(self, link_rel: str, target_rel: str) -> None:
        self._file(link_rel, target_rel + "\n")

    def _tools(self) -> dict:
        """Return {(mechanism, path): tool value} as main writes the tool column."""
        rows = ([("skills", r["skills.md"]) for r in detect_skills(self.repo)]
                + [("subagents", r["subagent"]) for r in _mod.detect_subagents(self.repo)]
                + [("commands", r["command"]) for r in detect_commands(self.repo)]
                + [("rules", r["rule"]) for r in detect_rules(self.repo)]
                + [("context_file", p) for p in detect_context_files(self.repo)])
        markers = _mod.resolved_markers(self.repo)
        found = _mod.artifact_tools(self.repo, rows, (), _mod.plugin_part_tools(self.repo),
                                    _mod.declared_file_tools(self.repo),
                                    _mod.marker_directories(self.repo, markers))
        return {key: heuristics.tool_value(tools) for key, tools in found.items()}

    def test_the_root_mcp_json_is_copilots_when_only_servers_names_a_server(self):
        # OrchardCMS/OrchardCore's root .mcp.json keeps its server under "servers", which only
        # Visual Studio reads there, and the file is therefore Copilot's. VS Code, Copilot CLI and
        # Claude Code read "mcpServers" or the bare form, so a file with a server under
        # "mcpServers" or in the bare form stays `any`.
        cases = [('{"servers": {"orchardcore": {"type": "http", "url": "https://ai.orchardcore.net"}}}',
                  "copilot"),
                 ('{"mcpServers": {"github": {"url": "https://api.githubcopilot.com/mcp/"}}}', "any"),
                 ('{"github": {"command": "npx", "args": ["github-mcp"]}}', "any"),
                 ('{"servers": {"a": {"url": "https://a.example"}},'
                  ' "mcpServers": {"b": {"command": "b"}}}', "any")]
        for text, expected in cases:
            with self.subTest(text=text):
                self._file(".mcp.json", text)
                found = _mod.artifact_tools(self.repo, [("mcp", p) for p in _mod.detect_mcp(self.repo)])
                self.assertEqual(heuristics.tool_value(found[("mcp", ".mcp.json")]), expected)

    def test_an_mcp_json_below_the_root_is_claudes_too_when_mcp_servers_names_a_server(self):
        # Claude Code reads the .mcp.json of each directory from its working directory up to the
        # root under "mcpServers", as GitTools/GitVersion's src/.mcp.json has it. Copilot CLI also
        # reads the bare form, and Visual Studio "servers", which Claude Code does not read.
        cases = [('{"mcpServers": {"rider": {"url": "http://127.0.0.1:64482/stream", "type": "http"}}}',
                  "claude|copilot"),
                 ('{"rider": {"command": "rider-mcp"}}', "copilot"),
                 ('{"servers": {"rider": {"url": "http://127.0.0.1:64482/stream"}}}', "copilot"),
                 # boardsesh/boardsesh's packages/web/.mcp.json: Claude Code skips a server with a
                 # url and no type.
                 ('{"mcpServers": {"Sentry": {"url": "https://mcp.sentry.dev/mcp/boardsesh/boardsesh"}}}',
                  "copilot"),
                 # Claude Code reads a type that is not a string as a stdio server's and rejects it.
                 ('{"mcpServers": {"x": {"type": [], "command": "npx"}}}', "copilot")]
        for text, expected in cases:
            with self.subTest(text=text):
                self._file("src/.mcp.json", text)
                found = _mod.artifact_tools(self.repo, [("mcp", p) for p in _mod.detect_mcp(self.repo)])
                self.assertEqual(heuristics.tool_value(found[("mcp", "src/.mcp.json")]), expected)

    def test_a_plugin_part_credits_its_tool_only_for_the_files_that_it_reads(self):
        # mondaycom/mcp: the root is a Gemini CLI extension and a Cursor plugin, and Cursor reads
        # the .md, .mdc, .markdown and .txt files of the plugin's commands, not the extension's
        # .toml commands in the same folder.
        self._file("gemini-extension.json", '{"name": "monday", "version": "1.0.0"}')
        self._file(".cursor-plugin/plugin.json", '{"name": "monday"}')
        self._file("commands/monday/create.toml", 'description = "Create"\nprompt = "Create an item"\n')
        self._file("commands/review.md", "# Review\n")
        tools = self._tools()
        self.assertEqual(tools[("commands", "commands/monday/create.toml")], "gemini")
        self.assertEqual(tools[("commands", "commands/review.md")], "cursor")

    def test_a_plugin_part_credits_its_tool_only_to_rows_of_its_mechanism(self):
        # xberg-io/xberg's plugin/ is a Claude Code and a Cursor plugin: Claude Code reads
        # commands/*.md as skills, and Cursor as commands.
        self._file("plugin/.claude-plugin/plugin.json", '{"name": "xberg"}')
        self._file("plugin/.cursor-plugin/plugin.json", '{"name": "xberg"}')
        self._file("plugin/commands/xberg-extract.md", "# Extract\n")
        tools = self._tools()
        self.assertEqual(tools[("skills", "plugin/commands/xberg-extract.md")], "claude")
        self.assertEqual(tools[("commands", "plugin/commands/xberg-extract.md")], "cursor")

    def test_a_link_credits_its_tool_only_for_what_the_tool_reads_through_it(self):
        # martin-georgiev/postgresql-for-doctrine: .claude/rules and .cursor/rules both link
        # .ai-tools/rules, and Cursor reads only .mdc there.
        self._file(".ai-tools/rules/testing.md", "# Testing\n")
        self._marker(".claude/rules", "../.ai-tools/rules")
        self._marker(".cursor/rules", "../.ai-tools/rules")
        self.assertEqual(self._tools(), {("rules", ".ai-tools/rules/testing.md"): "claude"})

    def test_a_rule_that_the_instructions_name_is_a_context_file_of_opencode(self):
        # AnswerOverflow/AnswerOverflow: OpenCode's instructions name the Cursor rules, which
        # OpenCode reads as context files and Cursor as rules.
        self._file("opencode.jsonc", '{"instructions": [".cursor/rules/*.mdc"]}\n')
        self._file(".cursor/rules/always-use-bun.mdc", "---\nalwaysApply: true\n---\nUse bun.\n")
        self.assertEqual(self._tools(),
                         {("rules", ".cursor/rules/always-use-bun.mdc"): "cursor",
                          ("context_file", ".cursor/rules/always-use-bun.mdc"): "opencode"})

    def test_a_plugin_skill_that_an_extension_names_is_a_context_file_of_gemini(self):
        # obsei/obsei: the Gemini extension names the SKILL.md of the Claude Code and Cursor
        # plugin as its context file. The plugin's tools read the file as a skill.
        plugin = "integrations/claude/obsei"
        self._file(".claude-plugin/marketplace.json",
                   '{"name": "obsei", "plugins": [{"name": "obsei", "source": "./' + plugin + '"}]}\n')
        self._file(".cursor-plugin/marketplace.json",
                   '{"name": "obsei", "plugins": [{"name": "obsei", "source": "' + plugin + '"}]}\n')
        self._file(f"{plugin}/.claude-plugin/plugin.json", '{"name": "obsei"}\n')
        self._file(f"{plugin}/.cursor-plugin/plugin.json", '{"name": "obsei"}\n')
        skill = f"{plugin}/skills/voice-of-customer/SKILL.md"
        self._file("gemini-extension.json", '{"name": "obsei", "contextFileName": "' + skill + '"}\n')
        self._file(skill, "---\nname: voice-of-customer\ndescription: Answer questions.\n---\n")
        self.assertEqual(self._tools(), {("skills", skill): "claude|cursor",
                                         ("context_file", skill): "gemini"})

    def test_a_file_two_tools_read_as_two_mechanisms_is_each_tools_in_its_row(self):
        # openshift/node-feature-discovery: .claude/commands links .cursor/commands. Cursor reads
        # the file as a command and Claude Code as a skill.
        self._file(".cursor/commands/cherry-pick.md", "# Cherry pick\n")
        self._marker(".claude/commands", "../.cursor/commands")
        tools = self._tools()
        self.assertEqual(tools[("commands", ".cursor/commands/cherry-pick.md")], "cursor")
        self.assertEqual(tools[("skills", ".cursor/commands/cherry-pick.md")], "claude")

    def test_a_subagent_below_a_group_folder_is_not_antigravitys(self):
        # razorpay/blade: Antigravity reads .agents/agents/<name>.md and <name>/agent.md, and
        # Claude Code reads .claude/agents at any depth.
        self._file(".agents/agents/critiques/code-quality.md", "---\nname: q\ndescription: d\n---\n")
        self._file(".agents/agents/planner.md", "---\nname: p\ndescription: d\n---\n")
        self._marker(".claude/agents", "../.agents/agents")
        tools = self._tools()
        self.assertEqual(tools[("subagents", ".agents/agents/critiques/code-quality.md")], "claude")
        self.assertEqual(tools[("subagents", ".agents/agents/planner.md")], "claude|gemini")

    def test_an_mdc_rule_a_cursor_plugin_names_in_agents_rules_is_cursors_alone(self):
        # NVIDIA/earth2studio: the root .cursor-plugin/plugin.json names .agents/rules, where
        # Antigravity reads .md files only.
        self._file(".cursor-plugin/plugin.json",
                   json.dumps({"name": "earth2studio", "rules": ".agents/rules"}))
        self._file(".agents/rules/style.mdc", "---\ndescription: style\n---\n")
        self.assertEqual(self._tools(), {("rules", ".agents/rules/style.mdc"): "cursor"})

    def test_agents_md_inside_a_linked_skill_is_every_tools_agents_md(self):
        self._file(".agents/skills/react/SKILL.md", "---\nname: react\ndescription: d\n---\n")
        self._file(".agents/skills/react/AGENTS.md", "# Rules\n")
        self._marker(".claude/skills/react", "../../.agents/skills/react")
        tools = self._tools()
        self.assertEqual(tools[("skills", ".agents/skills/react/SKILL.md")], "claude")
        self.assertEqual(tools[("context_file", ".agents/skills/react/AGENTS.md")], "any")


class TestACursorrulesInsideCursorIsNoContextFile(_RepoCase):
    """Cursor reads "the .cursorrules file in the root of your project", and a project is never
    its `.cursor` directory."""

    def test_only_a_cursorrules_outside_cursor_is_a_context_file(self):
        for rel in (".cursorrules", "packages/web/.cursorrules", ".cursor/.cursorrules",
                    ".cursor/rules/.cursorrules"):
            self._file(rel, "Use tabs.\n")
        self.assertEqual(sorted(p for p in detect_context_files(self.repo) if p.endswith(".cursorrules")),
                         [".cursorrules", "packages/web/.cursorrules"])

    def test_one_inside_cursor_is_cursors_other(self):
        self._file(".cursor/.cursorrules", "Use tabs.\n")
        other = detect_other(self.repo, [], detect_context_files(self.repo), [], [], [], [], [], [], [])
        self.assertEqual(other, [".cursor/.cursorrules"])


class TestAntigravityRulesNeedATrigger(_RepoCase):
    """Antigravity "silently discards" a rule in .agents/rules without a front matter declaring
    a valid trigger (https://antigravity.google/docs/rules, read 2026-10-13)."""

    def test_only_a_rule_with_a_valid_trigger_is_a_rule(self):
        self._file(".agents/rules/on.md", "---\ntrigger: always_on\n---\nUse tabs.\n")
        self._file(".agents/rules/quoted.md", "---\ntrigger: 'glob'  # ts only\nglobs: '*.ts'\n---\n")
        self._file(".agents/rules/none.md", "---\ndescription: no trigger\n---\n")
        self._file(".agents/rules/camel.md", "---\ntrigger: alwaysOn\n---\n")
        self._file(".agents/rules/bare.md", "Use tabs.\n")
        self.assertEqual(sorted(r["rule"] for r in detect_rules(self.repo)),
                         [".agents/rules/on.md", ".agents/rules/quoted.md"])

    def test_a_rule_without_one_is_other_in_antigravitys_directory(self):
        self._file(".agents/rules/bare.md", "Use tabs.\n")
        other = detect_other(self.repo, [], [], [], [], [], [], detect_rules(self.repo), [], [])
        self.assertEqual(other, [".agents/rules/bare.md"])
        found = _mod.artifact_tools(self.repo, [("other", ".agents/rules/bare.md")])
        self.assertEqual(found[("other", ".agents/rules/bare.md")], {"gemini"})

    def test_a_rule_without_one_that_claude_reads_through_a_link_is_claudes(self):
        """podman-desktop/extension-bootc links .claude/rules to ../.agents/rules, and the rules
        there have `paths` and no trigger: Claude Code reads them and Antigravity discards them."""
        self._file(".agents/rules/backend.md", "---\npaths: ['src/**']\n---\nUse tabs.\n")
        self._file(".agents/rules/on.md", "---\ntrigger: always_on\n---\nUse tabs.\n")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/rules").write_text("../.agents/rules\n", encoding="utf-8")
        rules = detect_rules(self.repo)
        markers = _mod.resolved_markers(self.repo)
        found = _mod.artifact_tools(self.repo, [("rules", r["rule"]) for r in rules],
                                    marker_dirs=_mod.marker_directories(self.repo, markers))
        self.assertEqual(found, {("rules", ".agents/rules/backend.md"): {"claude"},
                                 ("rules", ".agents/rules/on.md"): {"claude", "gemini"}})

    def test_a_linked_item_is_tested_through_its_marker(self):
        """angular/angular links `.agent/rules/agents.md` to `../../AGENTS.md`, which sets
        `trigger: always_on`, and the collector stores the link as a marker. The front matter
        tests read the file that the marker names. A test that read the marker's one line
        would make the rule an `other` row, and a linked agent of Antigravity or of Copilot
        CLI as well."""
        self._file("AGENTS.md", "---\ntrigger: always_on\n---\nUse pnpm.\n")
        self._file("docs/plain.md", "No front matter.\n")
        self._file("shared/a.md", "---\nname: a\ndescription: d\n---\nbody\n")
        self._file(".agent/rules/agents.md", "../../AGENTS.md\n")
        self._file(".agent/rules/plain.md", "../../docs/plain.md\n")
        self._file(".agents/agents/a.md", "../../shared/a.md\n")
        self._file(".github/agents/team/a.md", "../../../shared/a.md\n")
        rules, subagents = detect_rules(self.repo), detect_subagents(self.repo)
        self.assertEqual([r["rule"] for r in rules], [".agent/rules/agents.md"])
        self.assertEqual(sorted(r["subagent"] for r in subagents),
                         [".agents/agents/a.md", ".github/agents/team/a.md"])
        self.assertEqual(detect_other(self.repo, [], [], [], subagents, [], [], rules, [], []),
                         [".agent/rules/plain.md"])
        found = _mod.artifact_tools(self.repo, [("rules", ".agent/rules/agents.md"),
                                                ("subagents", ".agents/agents/a.md"),
                                                ("subagents", ".github/agents/team/a.md")])
        self.assertEqual(found, {("rules", ".agent/rules/agents.md"): {"gemini"},
                                 ("subagents", ".agents/agents/a.md"): {"gemini"},
                                 ("subagents", ".github/agents/team/a.md"): {"copilot"}})

    def test_a_file_with_a_nul_is_no_marker(self):
        """A symlink's target cannot contain a NUL, and the path functions raise ValueError for
        a path with one. If the step read it as a marker, a file of NUL bytes in a rules
        folder, or at the path of a skills directory, would stop the step."""
        (self.repo / ".agent/rules").mkdir(parents=True)
        (self.repo / ".agent/rules/blank.md").write_bytes(b"\0" * 16)
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/skills").write_bytes(b"../\0skills")
        self.assertEqual(detect_rules(self.repo), [])
        self.assertEqual(detect_skills(self.repo), [])
        self.assertEqual(_mod.resolved_markers(self.repo), [])
        self.assertEqual(_mod.measured_artifact(self.repo, "rules", ".agent/rules/blank.md"),
                         ".agent/rules/blank.md")

    def test_a_rule_counts_only_with_a_front_matter_that_antigravity_parses(self):
        """Antigravity CLI 1.4.10 quotes a `globs` value that starts with `*` and a description
        with an unquoted ": " before it decodes the front matter of a rule with go-yaml v3, and
        it drops a rule with a front matter that still does not decode, "Failed to parse rule
        file %s: %v" (parseRuleInfo, read 2026-11-07). GoogleCloudPlatform/khi's
        .agents/rules/html-coding-rule.md has `globs: **/*.html` and mum4k/termdash's
        .agents/rules/012-q-prompt.md has `globs: *`, which YAML reads as aliases, and both are
        rules, as is the same value in quotes. A flow list that is not closed, a key twice and an
        alias in the description do not parse. khi's .agents/rules/description-rule.md lists its
        globs, and Antigravity decodes `globs` into a string."""
        self._file(".agents/rules/html.md", "---\ntrigger: glob\nglobs: **/*.html\n---\n# HTML\n")
        self._file(".agents/rules/listed.md",
                   "---\ntrigger: glob\nglobs:\n  - pkg/task/**/*.go\n  - pkg/model/**/*.go\n---\n")
        self._file(".agents/rules/q-prompt.md",
                   "---\ntrigger: always_on\ndescription: Read only question mode\nglobs: *\n---\n")
        self._file(".agents/rules/quoted.md", '---\ntrigger: glob\nglobs: "**/*.html"\n---\n')
        self._file(".agents/rules/colon.md",
                   "---\ntrigger: model_decision\ndescription: Migrations: expand, then contract\n---\n")
        self._file(".agents/rules/list.md", "---\ntrigger: always_on\ntags: [a, b\n---\n")
        self._file(".agents/rules/twice.md",
                   "---\ntrigger: always_on\ndescription: a\ndescription: b\n---\n")
        self._file(".agents/rules/alias.md", "---\ntrigger: always_on\ndescription: *important\n---\n")
        rules = detect_rules(self.repo)
        self.assertEqual(sorted(r["rule"] for r in rules),
                         [".agents/rules/colon.md", ".agents/rules/html.md",
                          ".agents/rules/q-prompt.md", ".agents/rules/quoted.md"])
        self.assertEqual(detect_other(self.repo, [], [], [], [], [], [], rules, [], []),
                         [".agents/rules/alias.md", ".agents/rules/list.md",
                          ".agents/rules/listed.md", ".agents/rules/twice.md"])

    def test_the_trigger_must_be_set_once_at_the_top_level(self):
        for text in ("---\ntrigger: manual\ntrigger: glob\n---\n",
                     "---\nmeta:\n  trigger: manual\n---\n", "---\ntrigger:\n---\n"):
            with self.subTest(text=text):
                path = self._file(".agents/rules/r.md", text)
                self.assertFalse(_mod._has_trigger(self.repo, path))


class TestSymlinkedMechanismDirs(_RepoCase):
    """A directory reached through a marker yields real paths, once each."""

    def _marker(self, link_rel: str, target_rel: str) -> None:
        p = self.repo / link_rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(target_rel + "\n", encoding="utf-8")

    def test_artifacts_are_reported_at_their_real_path(self):
        # A link path exists in no tree. Its github_link returns 404, and the published
        # archive contains nothing at that path.
        self._file(".claude/skills/pdf/SKILL.md")
        self._marker(".cursor/skills", "../.claude/skills")
        paths = {r["skills.md"] for r in detect_skills(self.repo)}
        self.assertEqual(paths, {".claude/skills/pdf/SKILL.md"})

    def test_a_file_reached_twice_is_one_row(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self._marker(".cursor/skills", "../.claude/skills")
        self._marker(".codex/skills", "../.claude/skills")
        self.assertEqual(len(detect_skills(self.repo)), 1)

    def test_a_resolved_marker_is_not_other(self):
        self._file(".claude/skills/pdf/SKILL.md")
        self._marker(".cursor/skills", "../.claude/skills")
        markers = _mod.resolved_markers(self.repo)
        self.assertEqual(markers, [".cursor/skills"])
        other = detect_other(self.repo, [], [], detect_skills(self.repo), [], [], [], [], [],
                             markers)
        self.assertNotIn(".cursor/skills", other)

    def test_the_unclaimed_files_of_a_linked_directory_are_other(self):
        # utooland/utoo links .claude/agents to agents/, which contains performance reports with
        # a heading above two `---` horizontal rules and no front matter. Neither Claude Code nor
        # the test of Cursor's front matter takes them as subagents. `other` reads the linked
        # directory, because the reports would otherwise be in no CSV at all.
        self._file("agents/reviewer.md", "---\nname: reviewer\ndescription: Reviews.\n---\nBody\n")
        self._file("agents/reports/2026-09.md",
                   "# Performance report\n\nThe run.\n\n---\n\n## Results\n\n---\n\nThe summary.\n")
        self._marker(".claude/agents", "../agents")
        markers = _mod.resolved_markers(self.repo)
        self.assertEqual(markers, [".claude/agents"])
        subagents = detect_subagents(self.repo)
        self.assertEqual([r["subagent"] for r in subagents], ["agents/reviewer.md"])
        other = detect_other(self.repo, [], [], [], subagents, [], [], [], [], markers)
        self.assertEqual(other, ["agents/reports/2026-09.md"])
        # `unmatched paths` judges each file by the path that Claude Code reads.
        self.assertEqual(_mod.linked_spec_files(self.repo, markers),
                         {"agents/reviewer.md": ".claude/agents/reviewer.md",
                          "agents/reports/2026-09.md": ".claude/agents/reports/2026-09.md"})

    def test_a_link_to_a_directory_that_contains_it_adds_nothing_to_other(self):
        # The collector never copies a target that contains the link, because that would
        # put the surrounding tree into the collected files. A marker naming such a target
        # still resolves here, and reading it would list the whole repository.
        self._file("src/main.py")
        self._marker(".claude/agents", "..")
        other = detect_other(self.repo, [], [], [], [], [], [], [], [],
                             _mod.resolved_markers(self.repo))
        self.assertNotIn("src/main.py", other)

    def test_a_skill_linked_on_its_own_is_reported_where_it_is(self):
        # A repository can link one skill at a time rather than the whole directory. The skill
        # is the directory that the link names, and the marker is no `other` row under the link.
        self._file("tools/skills/isaaclab/SKILL.md")
        self._file("tools/skills/isaaclab/scripts/run.sh")
        self._marker(".claude/skills/isaaclab", "../../tools/skills/isaaclab")
        rows = detect_skills(self.repo)
        self.assertEqual([(r["skills.md"], r["name"], r["scripts"]) for r in rows],
                         [("tools/skills/isaaclab/SKILL.md", "isaaclab", "TRUE")])
        markers = _mod.resolved_markers(self.repo)
        self.assertEqual(markers, [".claude/skills/isaaclab"])
        other = detect_other(self.repo, [], [], rows, [], [], [], [], [], markers)
        self.assertEqual(other, [])
        self.assertTrue(derive_flags([], [], rows, [], [], [], [], [], other,
                                     _mod.marker_directories(self.repo, markers))[1]["claude"])

    def test_cursor_reads_no_skill_folder_that_links_outside_its_skills_directory(self):
        """Cursor CLI scans `.cursor/skills` by its real path and follows no folder link that
        resolves outside it. A skill that such a link leads to is Cursor's only where Cursor
        reads it at its own path, in a skills directory that Cursor reads, such as
        `.agents/skills`, with a front matter that parses: bestofjs/bestofjs links
        `.cursor/skills/find-skills` to `.agents/skills/find-skills`. A folder linked within
        `.cursor/skills`, and `.cursor/skills` linked as a whole, are Cursor's."""
        skill_md = "---\nname: find-skills\ndescription: Finds skills\n---\nFind skills.\n"
        # An unquoted description with a colon is no YAML, and Cursor drops the skill.
        unparsed = "---\nname: find-skills\ndescription: Finds: skills\n---\nFind skills.\n"
        cases = (("outside, where Cursor does not read the skill",
                  "tools/skills/find-skills/SKILL.md", skill_md, ".cursor/skills/find-skills",
                  "../../tools/skills/find-skills", False),
                 ("outside, in a skills directory that Cursor reads",
                  ".agents/skills/find-skills/SKILL.md", skill_md, ".cursor/skills/find-skills",
                  "../../.agents/skills/find-skills", True),
                 ("outside, with a front matter that Cursor does not parse",
                  ".agents/skills/find-skills/SKILL.md", unparsed, ".cursor/skills/find-skills",
                  "../../.agents/skills/find-skills", False),
                 # Cursor CLI keeps a skill file of `.claude/skills` only when it is named
                 # `SKILL.md`, and Claude Code's skill is no skill of Cursor.
                 ("outside, with a skill file that Cursor does not read",
                  ".claude/skills/find-skills/skill.md", skill_md, ".cursor/skills/find-skills",
                  "../../.claude/skills/find-skills", False),
                 ("outside, in Claude Code's skills directory, which Cursor reads",
                  ".claude/skills/find-skills/SKILL.md", skill_md, ".cursor/skills/find-skills",
                  "../../.claude/skills/find-skills", True),
                 ("within", ".cursor/skills/nplog/SKILL.md", skill_md, ".cursor/skills/alias",
                  "nplog", True),
                 ("whole directory", ".claude/skills/pdf/SKILL.md", skill_md, ".cursor/skills",
                  "../.claude/skills", True))
        for i, (name, skill, content, link, target, cursor) in enumerate(cases):
            with self.subTest(case=name):
                self.repo = self.tmp / f"s{i}"
                self.repo.mkdir()
                self._file(skill, content)
                self._marker(link, target)
                rows = detect_skills(self.repo)
                markers = _mod.resolved_markers(self.repo)
                flags = derive_flags([], [], rows, [], [], [], [], [], [],
                                     _mod.marker_directories(self.repo, markers),
                                     repo=self.repo)[1]
                self.assertEqual(flags["cursor"], cursor, rows)

    def test_a_skill_that_each_tool_directory_links_is_each_tools(self):
        """yoopta-editor/Yoopta-Editor keeps its skills in `.agents/skills` and links each skill
        folder from `.claude/skills`, `.codex/skills`, `.cursor/skills` and `.opencode/skills`.
        Cursor does not follow its link, but it reads the skill in `.agents/skills`, and the
        skill is the skill of the four tools (pp-0198 of the evaluation of 2026-11-04)."""
        self._file(".agents/skills/pricing-strategy/SKILL.md",
                   "---\nname: pricing-strategy\ndescription: Helps with pricing\n---\nPrice.\n")
        for directory in (".claude", ".codex", ".cursor", ".opencode"):
            self._marker(f"{directory}/skills/pricing-strategy",
                         "../../.agents/skills/pricing-strategy")
        rows = detect_skills(self.repo)
        markers = _mod.marker_directories(self.repo, _mod.resolved_markers(self.repo))
        found = _mod.artifact_tools(self.repo, [("skills", r["skills.md"]) for r in rows],
                                    marker_dirs=markers)
        self.assertEqual(found, {("skills", ".agents/skills/pricing-strategy/SKILL.md"):
                                 {"claude", "codex", "cursor", "opencode"}})

    def test_a_linked_skill_already_in_a_skills_directory_is_one_row(self):
        self._file(".agents/skills/pdf/SKILL.md")
        self._marker(".claude/skills/pdf", "../../.agents/skills/pdf")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         [".agents/skills/pdf/SKILL.md"])

    def test_a_linked_directory_without_a_skill_md_stays_other(self):
        self._file(".claude/linked/GUIDE.md")
        self._marker(".claude/skills/guide", "../linked")
        self.assertEqual(detect_skills(self.repo), [])
        self.assertEqual(_mod.resolved_markers(self.repo), [])
        self.assertIn(".claude/skills/guide",
                      detect_other(self.repo, [], [], [], [], [], [], [], [], []))

    def test_a_link_to_a_file_in_a_skills_directory_is_not_a_skill(self):
        self._file("docs/guide.md")
        self._marker(".claude/skills/guide.md", "../../docs/guide.md")
        self.assertEqual(detect_skills(self.repo), [])
        self.assertEqual(_mod.resolved_markers(self.repo), [])

    def test_every_fixed_mechanism_directory_the_heuristics_names_is_read_as_a_marker(self):
        # The step does not recognize a marker at a directory missing from _MARKER_DIRS. It
        # never resolves the marker's target and reports the marker file itself as an
        # unclassified file, a spurious row in other.csv.
        #
        # The match modes below are the modes for a directory that the detector reads. The
        # spec_dir and unclassified_dir rows are outside this invariant, because they name the
        # tool directories themselves, which _MARKER_DIRS does not contain.
        target = self._dir(".claude/linked")
        heuristics_dirs = [r["path"] for r in _mod._PATTERNS
                         if r["match_mode"] in ("skill_dir", "skill_dir_flat")
                         or r["match_mode"].startswith(("tree_ext:", "tree_suffix:", "dir_ext:"))]
        self.assertTrue(heuristics_dirs)
        # A `*` stands for a plugin name, and resolved_markers compares paths as they are
        # rather than as globs. The case below covers those rows.
        fixed = [r for r in heuristics_dirs if "*" not in r and not r.startswith("<")]
        self.assertTrue(fixed)
        for rel in fixed:
            with self.subTest(directory=rel):
                marker = self.repo / rel
                marker.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self._marker(rel, os.path.relpath(target, marker.parent))
                    markers = _mod.resolved_markers(self.repo)
                    self.assertIn(rel, markers)
                    self.assertNotIn(rel, detect_other(self.repo, [], [], [], [], [], [], [],
                                                       [], markers))
                finally:
                    marker.unlink(missing_ok=True)

    def test_a_marker_under_a_directory_without_a_manifest_is_not_a_plugin(self):
        self._dir(".claude/linked")
        self._marker(".claude/plugins/myplugin/skills", "../../linked")
        self.assertEqual(_mod.resolved_markers(self.repo), [])
        self.assertIn(".claude/plugins/myplugin/skills",
                      detect_other(self.repo, [], [], [], [], [], [], [], [], []))

    def test_a_marker_pointing_outside_the_tree_stays_other(self):
        # A link of this form is still evidence that the directory that it names is used. The
        # case builds the target, and the bound, not a missing target, leaves the marker
        # unresolved.
        elsewhere = self.tmp / "elsewhere" / "skills" / "pdf"
        elsewhere.mkdir(parents=True)
        (elsewhere / "SKILL.md").write_text("# x\n", encoding="utf-8")
        self._marker(".claude/skills", "../../elsewhere/skills")
        self.assertEqual(_mod.resolved_markers(self.repo), [])
        other = detect_other(self.repo, [], [], [], [], [], [], [], [], [])
        self.assertIn(".claude/skills", other)


class TestDetectChatModes(_RepoCase):

    def test_chatmode_is_a_subagent(self):
        self._file(".github/chatmodes/planner.chatmode.md")
        self.assertEqual(detect_subagents(self.repo), [{
            "subagent": ".github/chatmodes/planner.chatmode.md",
            "name": "planner",
            "memory": "n/a",
        }])

    def test_plain_markdown_in_chatmodes_is_not_a_subagent(self):
        self._file(".github/chatmodes/README.md")
        self.assertEqual(detect_subagents(self.repo), [])


# ---------------------------------------------------------------------------
# The merged mechanism CSVs
# ---------------------------------------------------------------------------

class TestTheMergedMechanismCsvHeaders(_StepCase):
    """One CSV per mechanism, with the whole published header.

    `heuristics.csv_header` declares one header per mechanism, and this class checks that the
    file on disk has that header.
    """

    # The published width of each CSV, written out, because a width read from the same call
    # that the detector makes would agree with any change to that call.
    WIDTHS = {"settings": 13, "context_file": 21, "skills": 18, "subagents": 16,
              "commands": 15, "hooks": 13, "rules": 15, "mcp": 13, "plugins": 13,
              "lsp": 13, "other": 14}

    # The published headers, written out for the same reason. A comparison with
    # `heuristics.csv_header`, the function that main writes them with, would pass a swap of
    # two of its columns.
    _META = ["repo_name", "scanned_at", "branch", "head_sha", "head_commit_date"]
    _LINK = ["github_link", "dataset_version", "created_at", "#commits"]
    _SHAS = ["first_commit_sha", "last_commit_sha"]
    HEADERS = {
        "settings": _META + ["settings", "tool"] + _LINK + _SHAS,
        "context_file": _META + ["context_file", "tool"] + _LINK + ["language"] + _SHAS
        + ["line_count", "is_reference", "referenced_file", "first_commit_ai_created",
           "first_commit_ai_tool", "#ai_commits", "ai_commit_tools"],
        "skills": _META + ["skills.md", "tool", "name", "scripts", "references", "assets"]
        + _LINK + ["language"] + _SHAS,
        "subagents": _META + ["subagent", "tool", "name", "memory"] + _LINK + ["language"]
        + _SHAS,
        "commands": _META + ["command", "tool", "name"] + _LINK + ["language"] + _SHAS,
        "hooks": _META + ["hook", "tool"] + _LINK + _SHAS,
        "rules": _META + ["rule", "tool", "name"] + _LINK + ["language"] + _SHAS,
        "mcp": _META + ["mcp", "tool"] + _LINK + _SHAS,
        "plugins": _META + ["plugin", "tool"] + _LINK + _SHAS,
        "lsp": _META + ["lsp", "tool"] + _LINK + _SHAS,
        "other": _META + ["other", "tool", "link_path"] + _LINK + _SHAS,
    }

    def setUp(self):
        super().setUp()
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        self._run()

    def test_every_mechanism_has_a_width(self):
        self.assertEqual(set(self.WIDTHS), set(heuristics.MECHANISM_CSVS))
        self.assertEqual(set(self.HEADERS), set(heuristics.MECHANISM_CSVS))

    def test_each_mechanism_csv_is_written_with_the_declared_header(self):
        for mechanism, width in self.WIDTHS.items():
            filename, _ = heuristics.MECHANISM_CSVS[mechanism]
            with self.subTest(mechanism=mechanism):
                header = self._out_header(filename)
                self.assertEqual(header, self.HEADERS[mechanism])
                self.assertEqual(heuristics.csv_header(mechanism), self.HEADERS[mechanism])
                self.assertEqual(len(header), width)

    def test_every_mechanism_csv_contains_the_git_columns(self):
        """The Git columns are in the mechanism CSVs themselves."""
        for mechanism in self.WIDTHS:
            filename, _ = heuristics.MECHANISM_CSVS[mechanism]
            with self.subTest(mechanism=mechanism):
                header = self._out_header(filename)
                for column in ("created_at", "#commits", "first_commit_sha",
                               "last_commit_sha"):
                    self.assertIn(column, header)

    def test_no_mechanism_csv_contains_is_empty(self):
        """`is_empty` could only be FALSE, because the collected file cleanup deletes every
        empty file before this step runs."""
        for mechanism in self.WIDTHS:
            filename, _ = heuristics.MECHANISM_CSVS[mechanism]
            with self.subTest(mechanism=mechanism):
                self.assertNotIn("is_empty", self._out_header(filename))

    def test_only_the_five_prose_mechanisms_have_a_language_column(self):
        expected = {"context_file", "skills", "subagents", "commands", "rules"}
        carried = {m for m in self.WIDTHS
                   if "language" in self._out_header(
                       heuristics.MECHANISM_CSVS[m][0])}
        self.assertEqual(carried, expected)

    def test_only_context_files_have_the_reference_and_ai_columns(self):
        extra = ("line_count", "is_reference", "referenced_file",
                 "first_commit_ai_created", "first_commit_ai_tool", "#ai_commits",
                 "ai_commit_tools")
        for mechanism in self.WIDTHS:
            filename, _ = heuristics.MECHANISM_CSVS[mechanism]
            header = self._out_header(filename)
            with self.subTest(mechanism=mechanism):
                for column in extra:
                    self.assertEqual(column in header, mechanism == "context_file",
                                     f"{filename}: {column}")


# ---------------------------------------------------------------------------
# extract_references
# ---------------------------------------------------------------------------

class TestExtractReferences(unittest.TestCase):

    def test_no_references(self):
        self.assertEqual(extract_references("Just some prose.\n"), [])

    def test_at_reference(self):
        self.assertEqual(extract_references("@AGENTS.md"), ["AGENTS.md"])

    def test_at_reference_with_dot_slash(self):
        self.assertEqual(extract_references("@./AGENTS.md"), ["AGENTS.md"])

    def test_at_reference_keeps_a_parent_directory_and_a_dot_folder(self):
        # ClusterLabs/pcs's .claude/CLAUDE.md reads "Read @../AGENTS.md", the root's file, and
        # Alfresco/alfresco-transform-core's CLAUDE.md is "@.github/copilot-instructions.md".
        self.assertEqual(extract_references("Read @../AGENTS.md"), ["../AGENTS.md"])
        self.assertEqual(extract_references("@.github/copilot-instructions.md"),
                         [".github/copilot-instructions.md"])
        self.assertEqual(extract_references("@../../.claude/CLAUDE.md"), ["../../.claude/CLAUDE.md"])

    def test_bare_filename_on_its_own_line(self):
        self.assertEqual(extract_references("AGENTS.md\n"), ["AGENTS.md"])

    def test_markdown_link(self):
        self.assertEqual(extract_references("See [the guide](docs/GUIDE.md)."),
                         ["docs/GUIDE.md"])

    def test_backtick_filename(self):
        self.assertEqual(extract_references("Read `CLAUDE.md` first."), ["CLAUDE.md"])

    def test_github_url(self):
        self.assertEqual(
            extract_references("https://github.com/o/r/blob/main/AGENTS.md"),
            ["https://github.com/o/r/blob/main/AGENTS.md"])

    def test_non_markdown_is_not_captured(self):
        self.assertEqual(extract_references("See config.yaml and setup.py"), [])

    def test_case_insensitive(self):
        self.assertEqual(extract_references("@Agents.MD"), ["Agents.MD"])

    def test_deduplicated_in_first_seen_order(self):
        content = "@AGENTS.md\nSee `AGENTS.md` and [x](GUIDE.md)"
        self.assertEqual(extract_references(content), ["AGENTS.md", "GUIDE.md"])


# ---------------------------------------------------------------------------
# meaningful_lines
# ---------------------------------------------------------------------------

class TestMeaningfulLines(unittest.TestCase):

    def test_blank_lines_are_dropped(self):
        self.assertEqual(meaningful_lines("a\n\n\nb\n"), ["a", "b"])

    def test_bare_headers_are_dropped(self):
        self.assertEqual(meaningful_lines("#\n##\ncontent\n"), ["content"])

    def test_header_with_text_is_kept(self):
        self.assertEqual(meaningful_lines("# Title\ncontent\n"), ["# Title", "content"])

    def test_lines_are_stripped(self):
        self.assertEqual(meaningful_lines("   a   \n\tb\t\n"), ["a", "b"])

    def test_empty_content(self):
        self.assertEqual(meaningful_lines(""), [])

    def test_a_form_feed_or_a_line_separator_inside_a_line_starts_no_line(self):
        self.assertEqual(meaningful_lines("a\u2028b\nc\x0cd\n"), ["a\u2028b", "c\x0cd"])


# ---------------------------------------------------------------------------
# split_lines
# ---------------------------------------------------------------------------

class TestSplitLines(unittest.TestCase):
    """`split_lines` ends a line at LF, CRLF or CR and at no other character that
    `str.splitlines` takes for a line end."""

    def test_lf_crlf_and_cr_end_a_line(self):
        self.assertEqual(split_lines("a\nb\r\nc\rd"), ["a", "b", "c", "d"])

    def test_a_final_line_break_ends_the_last_line(self):
        for text, lines in (("", []), ("\n", [""]), ("a", ["a"]), ("a\n", ["a"]),
                            ("a\r\n", ["a"]), ("a\r", ["a"]), ("a\n\n", ["a", ""])):
            with self.subTest(text=text):
                self.assertEqual(split_lines(text), lines)

    def test_no_other_character_ends_a_line(self):
        for separator in "\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029":
            with self.subTest(separator=hex(ord(separator))):
                self.assertEqual(split_lines(f"a{separator}b\n"), [f"a{separator}b"])


# ---------------------------------------------------------------------------
# classify  (in order: empty, too long, no reference, tier 1, tier 2)
# ---------------------------------------------------------------------------

class TestClassify(unittest.TestCase):
    """The three values that a context file's row reads out of the file itself.

    There is no `is_empty`, because the collected file cleanup deletes every empty file before
    this step runs.
    """

    def test_classify_returns_three_values(self):
        self.assertEqual(len(classify("@AGENTS.md\n", 10)), 3)

    def test_empty_content(self):
        self.assertEqual(classify("", 10), (0, False, ""))

    def test_whitespace_only(self):
        self.assertEqual(classify("   \n\t\n", 10), (2, False, ""))

    def test_inline_bare_filename_in_prose_is_not_extracted(self):
        # "See AGENTS.md" without backticks or @ matches no pattern, because a bare filename
        # must be alone on its line. A pointer phrase does nothing without an extracted
        # reference.
        self.assertEqual(classify("See AGENTS.md\n", 10), (1, False, ""))

    def test_too_long_is_not_a_reference(self):
        content = "\n".join(f"line {i}" for i in range(20)) + "\nSee AGENTS.md\n"
        _, is_ref, ref = classify(content, 10)
        self.assertFalse(is_ref)
        self.assertEqual(ref, "")

    def test_short_without_a_reference(self):
        self.assertEqual(classify("Some brief note.\n", 10), (1, False, ""))

    def test_tier1_short_file_with_a_reference(self):
        # At most 3 meaningful lines and a reference, with no pointer phrase needed.
        _, is_ref, ref = classify("@AGENTS.md\n", 10)
        self.assertTrue(is_ref)
        self.assertEqual(ref, "AGENTS.md")

    def test_tier1_boundary_three_meaningful_lines(self):
        content = "Title\nSubtitle\n@AGENTS.md\n"  # exactly 3 meaningful lines
        self.assertTrue(classify(content, 10)[1])

    def test_tier2_needs_a_pointer_phrase(self):
        # At least 4 meaningful lines with a reference but no pointer phrase.
        content = "Alpha\nBravo\nCharlie\nDelta `GUIDE.md`\n"
        self.assertFalse(classify(content, 10)[1])

    def test_tier2_with_a_pointer_phrase(self):
        content = "Alpha\nBravo\nCharlie\nPlease see `GUIDE.md`\n"
        self.assertTrue(classify(content, 10)[1])

    def test_reference_string_joins_multiple(self):
        content = "@A.md\n@B.md\n"
        self.assertEqual(classify(content, 10)[2], "A.md;B.md")

    def test_line_count_counts_all_lines(self):
        self.assertEqual(classify("a\nb\nc\n", 10)[0], 3)

    def test_a_form_feed_or_a_line_separator_inside_a_line_starts_no_line(self):
        self.assertEqual(classify("a\u2028b\nc\x0cd\n", 10)[0], 2)

    def test_a_line_separator_inside_a_line_adds_no_meaningful_line(self):
        """Three meaningful lines with a reference are a reference without a pointer phrase. A
        U+2028 inside the first line leaves it one line, and the file has three."""
        content = "Title\u2028of the project\nSubtitle\n@AGENTS.md\n"
        self.assertEqual(classify(content, 10), (3, True, "AGENTS.md"))

    def test_the_default_window_is_ten_lines(self):
        """The step calls classify with no window, and the published `is_reference` therefore
        comes from this constant."""
        self.assertEqual(_mod.MAX_REFERENCE_LINES, 10)
        ten = "\n".join(["Title", "Subtitle", "@AGENTS.md"] + [""] * 7)
        self.assertTrue(classify(ten)[1])
        self.assertFalse(classify(ten + "\nx" * 2)[1])


# ---------------------------------------------------------------------------
# detect_language
# ---------------------------------------------------------------------------

class _StubDetector:
    """A recording stand-in for lingua, which this suite does not install.

    The tests check the logic around the detector: how much of a file it is shown, which
    content is too short to show it at all, and what an undecided result becomes.
    """

    def __init__(self, answer="Language.ENGLISH"):
        self.answer = answer
        self.samples = []

    def detect_language_of(self, sample):
        self.samples.append(sample)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@contextlib.contextmanager
def _stub_lingua(answer="Language.ENGLISH"):
    detector = _StubDetector(answer)
    saved = (_mod.LINGUA_AVAILABLE, _mod._lingua_detector)
    _mod.LINGUA_AVAILABLE, _mod._lingua_detector = True, detector
    try:
        yield detector
    finally:
        _mod.LINGUA_AVAILABLE, _mod._lingua_detector = saved


class TestDetectLanguage(unittest.TestCase):

    PROSE = ("This file explains how the project is configured and what the "
             "assistant should do with it.\n")

    def test_prose_is_given_the_detector_s_response(self):
        with _stub_lingua() as detector:
            self.assertEqual(detect_language(self.PROSE), "English")
        self.assertEqual(detector.samples, [self.PROSE])

    def test_an_undecided_response_is_n_a(self):
        with _stub_lingua(answer=None):
            self.assertEqual(detect_language(self.PROSE), "n/a")

    def test_a_detector_error_is_n_a_rather_than_the_end_of_the_run(self):
        with _stub_lingua(answer=RuntimeError("model unavailable")):
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(detect_language(self.PROSE), "n/a")
        self.assertIn("RuntimeError", err.getvalue())

    def test_a_file_under_the_minimum_length_is_not_shown_to_the_detector(self):
        """The collector stores a symlink as a one-line file that contains its target. A
        language read from a file that short would describe a file that the row does not
        name."""
        with _stub_lingua() as detector:
            self.assertEqual(detect_language("x" * 49), "n/a")
            self.assertEqual(detector.samples, [])

    def test_the_minimum_length_is_fifty_characters(self):
        with _stub_lingua() as detector:
            self.assertEqual(detect_language("x" * 50), "English")
            self.assertEqual(detector.samples, ["x" * 50])

    def test_a_symlink_marker_is_under_the_minimum_length(self):
        with _stub_lingua():
            self.assertEqual(detect_language("../../shared/skills"), "n/a")

    def test_surrounding_blank_space_does_not_count_toward_the_minimum_length(self):
        with _stub_lingua() as detector:
            self.assertEqual(detect_language("   \n" + "x" * 20 + "\n" * 200), "n/a")
            self.assertEqual(detector.samples, [])

    def test_empty_content_is_n_a(self):
        with _stub_lingua() as detector:
            self.assertEqual(detect_language(""), "n/a")
            self.assertEqual(detector.samples, [])

    def test_only_the_first_10240_characters_are_shown(self):
        content = "abcde" * 4096  # 20,480 characters
        with _stub_lingua() as detector:
            self.assertEqual(detect_language(content), "English")
        self.assertEqual(len(detector.samples[0]), 10240)
        self.assertEqual(detector.samples[0], content[:10240])

    def test_without_lingua_every_artifact_is_n_a(self):
        saved = _mod.LINGUA_AVAILABLE
        _mod.LINGUA_AVAILABLE = False
        try:
            self.assertEqual(detect_language(self.PROSE), "n/a")
        finally:
            _mod.LINGUA_AVAILABLE = saved


class TestTheLanguageColumn(_StepCase):
    """The `language` column, which the detection step computes from the validated tree with
    no extension filter.

    TOML commands, `.cursorrules` and `.rules` files get a language by the same method as
    Markdown, and the published column follows one method.
    """

    PROSE = ("This file explains how the project is configured and what the "
             "assistant should do with it.\n")

    def test_a_toml_command_is_given_a_language(self):
        self._repo("o/a", {".gemini/commands/deploy.toml":
                           f'prompt = """{self.PROSE}"""\n'})
        with _stub_lingua():
            self._run()
        self.assertEqual(self._out_rows("commands.csv")[0]["language"], "English")

    def test_a_cursorrules_file_is_given_a_language(self):
        self._repo("o/a", {".cursorrules": self.PROSE})
        with _stub_lingua():
            self._run()
        self.assertEqual(self._out_rows("context-files.csv")[0]["language"], "English")

    def test_a_symlink_marker_row_is_n_a(self):
        self._repo("o/a", {"CLAUDE.md": "../shared/CLAUDE.md"})
        with _stub_lingua():
            self._run()
        self.assertEqual(self._out_rows("context-files.csv")[0]["language"], "n/a")

    # NVIDIA/Model-Optimizer links its Claude agents to the agents of its plugin, and the
    # collector stores each link as a marker that contains the target.
    LINKED_AGENT = ("---\nname: modelopt-model-downloader\ndescription: Use this agent when a "
                    "model must be staged.\n---\nDownload the model from Hugging Face into the "
                    "workspace, check the checksum of every file, and report the path of the "
                    "checkpoint to the caller.\n")
    LINKED_RULE = ("---\ntrigger: always_on\n---\nAlways run the formatter before you commit, "
                   "and keep every public function documented with a short docstring.\n")
    LINKED_FILES = {
        ".claude/agents/modelopt-model-downloader.md":
            "../../plugins/modelopt/agents/modelopt-model-downloader.md\n",
        "plugins/modelopt/agents/modelopt-model-downloader.md": LINKED_AGENT,
        ".agent/rules/x.md": "../../docs/rules/x.md\n",
        "docs/rules/x.md": LINKED_RULE,
    }

    def test_a_linked_agent_and_rule_are_measured_in_the_file_that_they_link_to(self):
        """A row of an agent or a rule that is a link exists because of the front matter of the
        file that its marker names, and its language describes that file too. At the link's
        path, the detector would see the agent's marker, a path over the minimum length, and the
        rule's marker would be too short for a language."""
        self._repo("NVIDIA/Model-Optimizer", self.LINKED_FILES)
        with _stub_lingua() as detector:
            self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(sorted(detector.samples), sorted([self.LINKED_AGENT, self.LINKED_RULE]))
        self.assertEqual([(row["subagent"], row["language"])
                          for row in self._out_rows("subagents.csv")],
                         [(".claude/agents/modelopt-model-downloader.md", "English")])
        self.assertEqual([(row["rule"], row["language"]) for row in self._out_rows("rules.csv")],
                         [(".agent/rules/x.md", "English")])

    @unittest.skipUnless(_mod.LINGUA_AVAILABLE, "lingua is not installed")
    def test_lingua_reads_the_linked_agent_and_rule_as_english(self):
        """lingua reads the agent's marker as German, and it reads the linked files as
        English."""
        self._repo("NVIDIA/Model-Optimizer", self.LINKED_FILES)
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([row["language"] for row in self._out_rows("subagents.csv")],
                         ["English"])
        self.assertEqual([row["language"] for row in self._out_rows("rules.csv")], ["English"])

    def test_a_linked_context_file_stays_a_reference_to_the_file_that_it_links_to(self):
        """A context file that is a link points to another context file by design. Its columns
        therefore describe the marker at its path, a reference of one line."""
        self._repo("o/a", {"CLAUDE.md": "AGENTS.md\n", "AGENTS.md": self.PROSE * 3})
        with _stub_lingua():
            self.assertEqual(self._run(), 0, self.stderr)
        row = next(row for row in self._out_rows("context-files.csv")
                   if row["context_file"] == "CLAUDE.md")
        self.assertEqual({column: row[column] for column in
                          ("language", "line_count", "is_reference", "referenced_file")},
                         {"language": "n/a", "line_count": "1", "is_reference": "TRUE",
                          "referenced_file": "AGENTS.md"})

    def test_a_context_file_that_is_a_real_symlink_is_a_reference_as_its_marker_is(self):
        """rsync keeps a link inside a directory that the collector copies whole, and
        `paths.write_marker` leaves a link to a file of the tree in place, such as gomlx/gomlx's
        `.agents/CLAUDE.md`, which links to the `AGENTS.md` beside it. Read through the link,
        the row would describe that AGENTS.md, with its lines and its language."""
        repo = self._repo("o/a", {".agents/AGENTS.md": self.PROSE * 3})
        os.symlink("AGENTS.md", repo / ".agents" / "CLAUDE.md")
        with _stub_lingua() as detector:
            self.assertEqual(self._run(), 0, self.stderr)
        rows = {row["context_file"]: {column: row[column] for column in
                                      ("language", "line_count", "is_reference",
                                       "referenced_file")}
                for row in self._out_rows("context-files.csv")}
        self.assertEqual(rows, {
            ".agents/AGENTS.md": {"language": "English", "line_count": "3",
                                  "is_reference": "FALSE", "referenced_file": ""},
            ".agents/CLAUDE.md": {"language": "n/a", "line_count": "1", "is_reference": "TRUE",
                                  "referenced_file": "AGENTS.md"}})
        self.assertEqual(detector.samples, [self.PROSE * 3])

    def test_a_context_file_that_is_a_real_symlink_without_a_target_stays_a_reference(self):
        """rsync's `--safe-links` judges a link by its text and keeps a link that leads nowhere
        inside a directory that the collector copies whole. A context file that is such a link
        is a reference, as a marker with a target that the tree lacks is, rather than a file
        that was not read."""
        repo = self._repo("o/a", {".agents/README.md": self.PROSE})
        os.symlink("notes/AGENTS.md", repo / ".agents" / "CLAUDE.md")
        with _stub_lingua():
            self.assertEqual(self._run(), 0, self.stderr)
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual({column: row[column] for column in
                          ("context_file", "language", "line_count", "is_reference",
                           "referenced_file")},
                         {"context_file": ".agents/CLAUDE.md", "language": "n/a",
                          "line_count": "1", "is_reference": "TRUE",
                          "referenced_file": "notes/AGENTS.md"})
        self.assertNotIn("Could not read", self.stderr)

    def test_the_provenance_record_stays_whole_when_writing_it_is_interrupted(self):
        # Opening the record for writing truncates it. The step has to produce the content
        # before it opens the file.
        self._repo("o/a", {"CLAUDE.md": self.PROSE})
        self.output.mkdir(exist_ok=True)
        record = self.output / "provenance.json"
        record.write_text('{"dataset_version": "earlier"}\n')
        with mock.patch.object(_mod.json, "dumps", side_effect=OSError(28, "No space")), \
                mock.patch.object(_mod.json, "dump", side_effect=OSError(28, "No space")), \
                self.assertRaises(OSError):
            self._run()
        self.assertEqual(record.read_text(), '{"dataset_version": "earlier"}\n')

    def test_a_head_commit_date_that_is_no_timestamp_is_blank_and_the_step_goes_on(self):
        """A damaged repository can carry a date that does not parse. If the step refused the
        date, it would stop at every Start, and the run would never reach the release."""
        self._repo("o/a", {"CLAUDE.md": self.PROSE}, head_commit_date="the day before")
        self._repo("o/b", {"CLAUDE.md": self.PROSE})
        self.assertEqual(self._run(), 0, self.stderr)
        dates = {row["repo_name"]: row["head_commit_date"]
                 for row in self._out_rows("repos.csv")}
        self.assertEqual(dates, {"o/a": "", "o/b": self.HEAD_COMMIT_DATE})
        self.assertIn("had 1 head_commit_date value(s) that are not ISO 8601", self.stderr)
        self.assertIn("in 1 repositories (o/a).\n", self.stderr)

    def test_a_file_that_cannot_be_read_is_blank_rather_than_empty(self):
        # A read that failed and returned the empty string would make the row record that the
        # file has no lines, is no pointer and has no language, although nothing read the
        # file.
        repo = self._repo("o/a", {"CLAUDE.md": self.PROSE})
        os.chmod(repo / "CLAUDE.md", 0o000)
        if os.access(repo / "CLAUDE.md", os.R_OK):
            self.skipTest("this user can read a file with a mode that denies it")
        with _stub_lingua():
            self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual({column: row[column] for column in
                          ("language", "line_count", "is_reference", "referenced_file")},
                         {"language": "", "line_count": "", "is_reference": "",
                          "referenced_file": ""})
        self.assertIn("Could not read", self.stderr)
        self.assertIn("CLAUDE.md (", self.stderr)

    def test_a_file_that_git_crypt_encrypted_is_no_text(self):
        """vuejs/router publishes a CLAUDE.md that git-crypt encrypted, which opens with
        `\\0GITCRYPT\\0`. Latin-1 decodes any bytes. If the step read the file as text, the
        row would record the noise as Vietnamese, with the newline bytes of the ciphertext as
        its line count."""
        repo = self._repo("o/a", {})
        (repo / "CLAUDE.md").write_bytes(b"\0GITCRYPT\0" + bytes(range(256)) * 4)
        with _stub_lingua() as detector:
            self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual({column: row[column] for column in
                          ("language", "line_count", "is_reference", "referenced_file")},
                         {"language": "n/a", "line_count": "", "is_reference": "",
                          "referenced_file": ""})
        self.assertEqual(detector.samples, [])

    def test_a_utf_16_file_is_decoded_before_it_is_read(self):
        """`read_artifact` tries UTF-8, UTF-16 and latin-1 in turn. Reading with UTF-8 and
        replacement characters instead would show the detector mojibake and count a line that
        the file does not have."""
        repo = self._repo("o/a", {})
        (repo / "CLAUDE.md").write_bytes(self.PROSE.encode("utf-16"))
        with _stub_lingua() as detector:
            self._run()
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual(row["language"], "English")
        self.assertEqual(row["line_count"], "1")
        self.assertEqual(detector.samples, [self.PROSE])

    # mathlab/pina links its skills to `.opencode/skills`, cline/cline its commands to
    # `.clinerules/workflows`, transloadit/node-sdk its Cursor rules to `.ai/rules`, and the
    # collector copies none of these targets. The agent's marker, from
    # pingidentity/terraform-provider-pingone, is long enough for the detector, which would read
    # it as English.
    UNFOLLOWED_MARKERS = {
        ".claude/skills/create-skill/SKILL.md": "../../../.opencode/skills/create-skill/SKILL.md\n",
        ".claude/commands/release.md": "../../.clinerules/workflows/release.md\n",
        ".cursor/rules/general.mdc": "../../.ai/rules/general.mdc\n",
        ".github/agents/LocalCodeReview.agent.md":
            "../.././shared-configs/common/ai/code-review/LocalCodeReview.agent.md\n",
        ".cursor/commands/deploy.md": "../../docs\n",
    }

    def test_a_marker_with_a_target_that_the_tree_lacks_has_a_blank_language(self):
        """The marker's one line is a path, and a language measured on it describes no file. A
        blank value records that the step did not read the configuration artifact. The last
        marker names a directory of the tree, which is no file either."""
        self._repo("o/a", {**self.UNFOLLOWED_MARKERS, "docs/guide.md": self.PROSE})
        with _stub_lingua() as detector:
            self.assertEqual(self._run(), 0, self.stderr)
        languages = {row[column]: row["language"]
                     for name, column in (("skills.csv", "skills.md"), ("rules.csv", "rule"),
                                          ("subagents.csv", "subagent"),
                                          ("commands.csv", "command"))
                     for row in self._out_rows(name)}
        self.assertEqual(languages, dict.fromkeys(self.UNFOLLOWED_MARKERS, ""))
        self.assertEqual(detector.samples, [])
        self.assertIn("INFO detection: .cursor/rules/general.mdc in o/a is a symlink marker, "
                      "and the validated tree lacks its target. Left its language blank.",
                      self.stderr)

    def test_a_file_reference_of_one_line_is_the_file_s_own_text(self):
        """lge-ros2/cloisim's commands are one line each, a reference such as
        `@../../.github/skills/add-actor-animation/SKILL.md`, and no marker of a link. The line
        is long enough for the detector."""
        reference = "@../../.github/skills/add-actor-animation/SKILL.md\n"
        self._repo("o/a", {".claude/commands/add-actor-animation.md": reference})
        with _stub_lingua() as detector:
            self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([row["language"] for row in self._out_rows("skills.csv")], ["English"])
        self.assertEqual(detector.samples, [reference])

    def test_a_context_file_with_a_target_that_the_tree_lacks_stays_a_reference(self):
        self._repo("o/a", {"CLAUDE.md": "../shared/AGENTS.md\n"})
        with _stub_lingua():
            self.assertEqual(self._run(), 0, self.stderr)
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual({column: row[column] for column in
                          ("language", "line_count", "is_reference", "referenced_file")},
                         {"language": "n/a", "line_count": "1", "is_reference": "TRUE",
                          "referenced_file": "../shared/AGENTS.md"})


class TestTheLineCountColumn(_StepCase):
    """`line_count` of `context-files.csv` and of `planning-documents.csv` counts the lines that
    LF, CRLF or CR end. A spec of apache/struts has a U+2028 in a code span, and a task of
    rvdbreemen/OTGW-firmware has a form feed inside a path. Each of these characters is inside
    a line of its file."""

    TEXT = "# Plan\nKeep `a\u2028b` as written.\nLinking .pio\x08uild\x1bsp32\x0cirmware.elf\n"

    def test_a_context_file(self):
        self._repo("o/a", {"CLAUDE.md": self.TEXT})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(self._out_rows("context-files.csv")[0]["line_count"], "3")

    def test_a_planning_document(self):
        self._repo("o/a", {"CLAUDE.md": "# Claude\n", ".claude/plans/fix.md": self.TEXT})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([(row["planning_document"], row["line_count"])
                          for row in self._out_rows("planning-documents.csv")],
                         [(".claude/plans/fix.md", "3")])

    def test_a_byte_that_latin_1_decodes_to_nel_starts_no_line(self):
        """Windows-1252 writes an ellipsis as the byte 0x85. The file is no UTF-8, and its odd
        number of bytes is no UTF-16, so latin-1, the last encoding that `read_artifact` tries,
        decodes the byte to NEL (U+0085)."""
        repo = self._repo("o/a", {})
        (repo / "CLAUDE.md").write_bytes(b"Wait\x85 then go.\n")
        self.assertEqual(_mod.read_artifact(repo / "CLAUDE.md"), "Wait\x85 then go.\n")
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual(self._out_rows("context-files.csv")[0]["line_count"], "1")


class TestTheMeasuredArtifact(_RepoCase):
    """`measured_artifact` returns the file of the tree with the text that a row's `language`
    describes, or None where no file of the tree has that text."""

    def test_none_for_a_marker_that_names_no_file_of_the_tree(self):
        self._file("docs/dir/guide.md", "Guide.\n")
        self._file("docs/agent.md", AGENT)
        cases = {"absent.md": "../../docs/gone.md", "dir.md": "../../docs/dir",
                 "outside.md": "../../../outside.md", "self.md": "../agents/self.md",
                 "loop1.md": "./loop2.md", "loop2.md": "./loop1.md",
                 "chain.md": "absent.md"}
        for name, text in cases.items():
            self._file(f".claude/agents/{name}", text + "\n")
        (self.tmp / "outside.md").write_text(AGENT, encoding="utf-8")
        for name in cases:
            with self.subTest(marker=name):
                self.assertIsNone(_mod.measured_artifact(self.repo, "subagents",
                                                         f".claude/agents/{name}"))

    def test_the_named_file_for_a_marker_that_names_one(self):
        self._file("docs/agent.md", AGENT)
        self._file(".claude/agents/linked.md", "../../docs/agent.md\r\n")
        self.assertEqual(_mod.measured_artifact(self.repo, "subagents", ".claude/agents/linked.md"),
                         "docs/agent.md")

    def test_the_file_itself_for_its_own_text(self):
        """The cases are a file reference, a line with a space, a word that names no file of
        its folder, and prose."""
        for name, text in (("reference.md", "@../../docs/gone.md\n"),
                           ("spaced.md", "../../docs/a gent.md\n"), ("word.md", "hello\n"),
                           ("prose.md", AGENT)):
            self._file(f".claude/agents/{name}", text)
            with self.subTest(file=name):
                self.assertEqual(_mod.measured_artifact(self.repo, "subagents",
                                                        f".claude/agents/{name}"),
                                 f".claude/agents/{name}")

    def test_a_context_file_is_measured_at_its_own_path(self):
        self._file("CLAUDE.md", "../shared/AGENTS.md\n")
        self.assertEqual(_mod.measured_artifact(self.repo, "context_file", "CLAUDE.md"),
                         "CLAUDE.md")


class TestTheArtifactText(_RepoCase):
    """`artifact_text` returns the text that the columns read from a configuration artifact
    describe."""

    def test_a_context_file_that_is_a_link_reads_as_the_marker_that_the_collector_writes(self):
        """The marker of a target that is not UTF-8 escapes each byte that UTF-8 cannot
        decode."""
        self._file(".agents/AGENTS.md", AGENT)
        for name, target in (("CLAUDE.md", "AGENTS.md"),
                             ("GEMINI.md", os.fsdecode(b"n\xffotes/AGENTS.md"))):
            link = self.repo / ".agents" / name
            try:
                os.symlink(target, link)
            except OSError as error:
                self.skipTest(f"this file system refuses the link target {target!r} ({error})")
            marker = self.tmp / "markers" / name
            paths.write_marker(marker, os.readlink(link))
            with self.subTest(target=target):
                self.assertEqual(_mod.artifact_text(self.repo, "context_file", f".agents/{name}"),
                                 marker.read_text(encoding="utf-8"))

    def test_a_link_of_another_mechanism_reads_as_the_file_that_it_leads_to(self):
        """The tool reads an agent through its link."""
        self._file("docs/agent.md", AGENT)
        os.symlink("../../docs/agent.md", self._dir(".claude/agents") / "linked.md")
        self.assertEqual(_mod.artifact_text(self.repo, "subagents", ".claude/agents/linked.md"),
                         AGENT)


class TestAStringThatUtf8CannotEncode(_StepCase):
    """A lone surrogate, which JSON can escape and `json.loads` returns, is the only kind of code
    point that UTF-8 cannot encode. `caches.replace_file` writes UTF-8 strictly."""

    def test_a_run_over_a_command_name_with_a_lone_surrogate_exits_zero(self):
        """Writing skills.csv would raise after the step had replaced repos.csv and the CSVs
        before it. The tree would not change, and every later Start would fail the same way."""
        self._repo("o/r", {
            "plugins/p/.claude-plugin/plugin.json": TestDeclaredComponentNames.SURROGATE_MANIFEST,
            "plugins/p/commands/deploy.md": "# Deploy\n\nDeploy the service to staging.\n"})
        self.assertEqual(self._run(), 0, self.stderr)
        self.assertEqual([(row["skills.md"], row["name"]) for row in self._out_rows("skills.csv")],
                         [("plugins/p/commands/deploy.md", "deploy")])
        self.assertIn("Ignored the command name 'de\\ud800ploy'", self.stderr)

    def test_the_writer_replaces_a_lone_surrogate_and_names_the_row(self):
        row = {"repo_name": "o/a", "context_file": "CLAUDE.md", "referenced_file": "a\ud800.md",
               "language": "English"}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _mod.write_output_table("context-files.csv", list(row), [row], output_dir=self.output)
        self.assertEqual(self._out_rows("context-files.csv"),
                         [{**row, "referenced_file": "a\ufffd.md"}])
        self.assertEqual(row["referenced_file"], "a\ud800.md")
        self.assertIn("WARNING detection: The column(s) referenced_file of the row of o/a for "
                      "CLAUDE.md in context-files.csv contain a lone surrogate, which UTF-8 "
                      "cannot encode. Wrote each lone surrogate as U+FFFD.",
                      err.getvalue())

    def test_a_row_of_repos_csv_names_its_repository(self):
        row = {"repo_name": "o/a", "branch": "ma\udcffin"}
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _mod.write_output_table("repos.csv", list(row), [row], output_dir=self.output)
        self.assertEqual(self._out_rows("repos.csv"),
                         [{"repo_name": "o/a", "branch": "ma\ufffdin"}])
        self.assertIn("The column(s) branch of the row of o/a in repos.csv contain a lone "
                      "surrogate", err.getvalue())


# ---------------------------------------------------------------------------
# The git-facts join
# ---------------------------------------------------------------------------

class TestTheFileHistoryJoin(_StepCase):
    """The Git columns come from `file_history.csv` rather than from a clone.

    The step uses two lookups, because neither alone is complete. The exact key finds a row
    that the file history step measured for this mechanism. `for_artifact` finds a row that
    the cloning step wrote. Such a row records no mechanism, because the cloning step reads a
    file's history before any step decides which CSV the file belongs to.
    """

    def test_a_row_keyed_on_the_exact_mechanism_resolves(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._git_fact("o/a", "CLAUDE.md", "context_file",
                       created_at="2026-02-01T00:00:00Z", commits="7")
        self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual(row["created_at"], "2026-02-01T00:00:00Z")
        self.assertEqual(row["#commits"], "7")
        self.assertEqual(row["first_commit_sha"], "b" * 40)
        self.assertEqual(row["last_commit_sha"], "c" * 40)

    def test_a_row_the_cloning_step_wrote_resolves_through_for_artifact(self):
        """The cloning step writes the mechanism blank, and every one of its rows would miss
        a lookup by the mechanism alone."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._git_fact("o/a", "CLAUDE.md", "", commits="4")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("context-files.csv")[0]["#commits"], "4")
        self.assertNotIn("no file history", self.stderr)

    def test_a_reclassified_path_keeps_its_history(self):
        """AgentTorrent publishes `.claude/commands` under skills, where AgentConfigs 1.0.0
        published it under commands. The row that the file history step measured names the old
        mechanism, and the history is the same."""
        self._repo("o/a", {".claude/commands/deploy.md": "# deploy\n"})
        self._git_fact("o/a", ".claude/commands/deploy.md", "commands", commits="11")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("skills.csv")[0]["#commits"], "11")

    def test_the_exact_key_wins_where_two_mechanisms_disagree(self):
        """`.claude/settings.local.json` is a settings row and a hooks row. `git log --all`
        counts `#commits`, and the two CSVs therefore read it at different moments. The exact
        key finds each."""
        self._repo("o/a", {".claude/settings.local.json":
                           '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": '
                           '[{"type": "command", "command": "./check.sh"}]}]}}'})
        self._git_fact("o/a", ".claude/settings.local.json", "settings", commits="7")
        self._git_fact("o/a", ".claude/settings.local.json", "hooks", commits="9")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("settings.csv")[0]["#commits"], "7")
        self.assertEqual(self._out_rows("hooks.csv")[0]["#commits"], "9")

    def test_two_disagreeing_mechanisms_leave_a_third_blank(self):
        """`for_artifact` refuses where the recorded mechanisms disagree, rather
        than letting whichever was seeded last decide the published value."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._git_fact("o/a", "CLAUDE.md", "other", commits="7")
        self._git_fact("o/a", "CLAUDE.md", "settings", commits="9")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("context-files.csv")[0]["#commits"], "")

    def test_a_row_recorded_at_another_commit_is_not_used(self):
        """`head_sha` ties a row to a commit: `repo_name` alone is out of date as soon as
        the repository gains a commit."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"}, head_sha="a" * 40)
        self._git_fact("o/a", "CLAUDE.md", "context_file", head_sha="d" * 40,
                       commits="7")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("context-files.csv")[0]["#commits"], "")

    def test_a_row_with_no_cache_entry_is_blank_and_reported(self):
        """The step leaves the columns blank rather than refusing, because
        `agentcontext/steps/file_history.py` measures those rows and reads the output CSVs
        that this step writes. It reports the blanks, because a column that nobody reports as
        blank could stop being filled without notice."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        for column in heuristics.HISTORY_COLUMNS:
            self.assertEqual(row[column], "", column)
        self.assertIn("no file history", self.stderr)
        self.assertIn("CLAUDE.md", self.stderr)
        self.assertIn("agentcontext file-history", self.stderr)

    def test_an_absent_cache_is_not_an_error(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertFalse(self.file_history_csv.exists())
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._out_rows("context-files.csv")[0]["created_at"], "")

    def test_every_mechanism_reads_the_cache(self):
        """Every mechanism reads the cache, not a subset of them."""
        self._repo("o/a", ONE_ARTIFACT_PER_TABLE)
        for mechanism, artifact in (
                ("settings", ".claude/settings.json"),
                ("context_file", "CLAUDE.md"),
                ("skills", ".claude/skills/s/SKILL.md"),
                ("subagents", ".claude/agents/x.md"),
                ("commands", ".cursor/commands/c.md"),
                ("hooks", ".github/hooks/h.json"),
                ("rules", ".cursor/rules/r.mdc"),
                ("mcp", ".mcp.json"),
                ("plugins", ".claude-plugin/plugin.json"),
                ("lsp", ".github/lsp.json"),
                ("other", ".claude/notes.txt")):
            self._git_fact("o/a", artifact, mechanism, commits="5")
        self.assertEqual(self._run(), 0)
        for mechanism in heuristics.MECHANISM_CSVS:
            filename, _ = heuristics.MECHANISM_CSVS[mechanism]
            with self.subTest(mechanism=mechanism):
                self.assertEqual(self._out_rows(filename)[0]["#commits"], "5")


# ---------------------------------------------------------------------------
# The commit CSV is restricted to what this step publishes
# ---------------------------------------------------------------------------

class TestTheCommitCsvRestriction(_StepCase):
    """The cloning step reads the commit history of every repository that contributed a file,
    and this step publishes fewer repositories. The commit CSV beside the mechanism CSVs
    describes the same repositories as they do."""

    def _commits(self, *repos):
        path = self.output / "all_ai_commits.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["repo_name", "commit_sha", "ai_tool"],
                               lineterminator="\n")
            w.writeheader()
            for repo in repos:
                w.writerow({"repo_name": repo, "commit_sha": "1" * 40, "ai_tool": "Claude"})
        return path

    def test_rows_of_unpublished_repositories_are_left_out_and_counted(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        path = self._commits("o/a", "o/unpublished", "o/a")
        self.assertEqual(self._run(), 0)
        rows = list(csv.DictReader(path.open(encoding="utf-8")))
        self.assertEqual([r["repo_name"] for r in rows], ["o/a", "o/a"])
        self.assertIn("left out 1 row(s)", self.stdout)

    def test_an_absent_commit_csv_is_logged_and_not_an_error(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertEqual(self._run(), 0)
        self.assertIn("nor its shards exist", self.stderr)
        self.assertFalse((self.output / "all_ai_commits.csv").exists())

    def _shards(self, commits_dir: Path, *repos, record=True, columns=None):
        # A shard is a CSV beside a per-repository commit record. The collector's reader decides
        # from the record whether the CSV counts.
        commits_dir.mkdir(parents=True, exist_ok=True)
        for repo in repos:
            path = commits_dir / (repo.replace("/", "§") + ".csv")
            fields = list(columns or ["repo_name", "commit_sha", "ai_tool"])
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
                w.writeheader()
                row = {c: "" for c in fields}
                row.update({"repo_name": repo, "commit_sha": "2" * 40, "ai_tool": "Claude"})
                w.writerow({c: row.get(c, "x") for c in fields})
            if record:
                path.with_suffix(".json").write_text(
                    json.dumps({"repo_name": repo, "outcome": "history_read", "commits_read": 1,
                                "commits_matched": 1, "unmatched": [], "no_signal_shas": [],
                                "ai_rows": []}) + "\n")

    def test_the_restriction_reads_the_shards_the_collector_reads(self):
        """The step reads the shards through the collector's reader alone. The rows of a
        repository with a per-repository commit record that cannot be parsed stay out, and so
        does a CSV without a record, which a killed run leaves behind. The published CSV
        therefore agrees with `ai_commits-repos.csv`."""
        commits_dir = self.tmp / "collect_commits"
        self._shards(commits_dir, "o/a")
        self._shards(commits_dir, "o/torn")
        (commits_dir / "o§torn.json").write_text('{"repo_name": "o/torn", "outc')
        self._shards(commits_dir, "o/orphan", record=False)
        CSV = self.output / "all_ai_commits.csv"
        CSV.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _mod.restrict_ai_commits(CSV, {"o/a", "o/torn", "o/orphan"}, commits_dir)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual([r["repo_name"] for r in rows], ["o/a"])
        self.assertIn("o§torn.json is not a readable per-repository commit record",
                      err.getvalue())
        self.assertEqual(list(rows[0]), list(_mod.commits.COMMIT_COLUMNS),
                         "every row is written in the current column set")

    def test_a_shard_from_another_column_set_is_read_into_the_current_one(self):
        # The step reads a shard with a header that differs from the first shard's into the
        # current column set rather than raising ValueError after the whole collection. The
        # shards of the repositories that succeeded are never read again, and a column added
        # later meets the old shards.
        commits_dir = self.tmp / "collect_commits"
        self._shards(commits_dir, "o/old", columns=["repo_name", "commit_sha", "ai_tool", "extra"])
        self._shards(commits_dir, "o/new")
        CSV = self.output / "all_ai_commits.csv"
        CSV.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.restrict_ai_commits(CSV, {"o/old", "o/new"}, commits_dir)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual({r["repo_name"] for r in rows}, {"o/old", "o/new"})
        self.assertTrue(all(list(r) == list(_mod.commits.COMMIT_COLUMNS) for r in rows))

    def test_with_shards_the_commit_csv_is_rebuilt_so_a_repository_the_set_gains_is_in_it(self):
        # Restricting the CSV in place cannot give a row back. A second detection run over a
        # larger published set, after a fix to the heuristics, would then publish the new
        # repository with no commit rows. The shards that the cloning step keeps are therefore
        # the source.
        commits_dir = self.tmp / "collect_commits"
        self._shards(commits_dir, "o/a", "o/b", "o/c")
        CSV = self.output / "all_ai_commits.csv"
        CSV.parent.mkdir(parents=True, exist_ok=True)
        dropped = _mod.restrict_ai_commits(CSV, {"o/a", "o/b"}, commits_dir)
        self.assertEqual(dropped, 1)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual(sorted(r["repo_name"] for r in rows), ["o/a", "o/b"])
        dropped = _mod.restrict_ai_commits(CSV, {"o/a", "o/b", "o/c"}, commits_dir)
        self.assertEqual(dropped, 0)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual(sorted(r["repo_name"] for r in rows), ["o/a", "o/b", "o/c"])

    def test_the_commit_csv_rebuilt_from_the_shards_ends_each_record_with_lf(self):
        """The commit CSV ends each record with LF, as every CSV of the pipeline does, rather
        than with the CRLF that the csv module writes by default."""
        commits_dir = self.tmp / "collect_commits"
        self._shards(commits_dir, "o/a", "o/b")
        CSV = self.output / "all_ai_commits.csv"
        CSV.parent.mkdir(parents=True, exist_ok=True)
        _mod.restrict_ai_commits(CSV, {"o/a", "o/b"}, commits_dir)
        self.assertNotIn(b"\r\n", CSV.read_bytes())
        rows = list(csv.DictReader(CSV.open(newline="", encoding="utf-8")))
        self.assertEqual(sorted(r["repo_name"] for r in rows), ["o/a", "o/b"])

    def test_the_other_tools_commit_csv_is_rebuilt_from_their_shards(self):
        """The same restriction applies to the second shard of each repository. A repository
        with a per-repository commit record that parses keeps its rows of the other tools, and a
        repository with a damaged record keeps none. The step does not read
        `other_ai_commits.csv` from the main shards."""
        commits_dir = self.tmp / "collect_commits"
        self._shards(commits_dir, "o/a", "o/b", "o/torn")
        (commits_dir / "o§torn.json").write_text('{"repo_name": "o/torn", "outc')
        for repo in ("o/a", "o/b", "o/torn"):
            other = _mod.commits.other_shard_path(commits_dir, repo)
            other.parent.mkdir(parents=True, exist_ok=True)
            with other.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(_mod.commits.COMMIT_COLUMNS),
                                   lineterminator="\n")
                w.writeheader()
                w.writerow({c: "" for c in _mod.commits.COMMIT_COLUMNS}
                           | {"repo_name": repo, "commit_sha": "3" * 40, "ai_tool": "Devin"})
        CSV = self.output / "other_ai_commits.csv"
        CSV.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.redirect_stderr(io.StringIO()):
            dropped = _mod.restrict_ai_commits(CSV, {"o/a", "o/torn"}, commits_dir,
                                               other_tools=True)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual([(r["repo_name"], r["ai_tool"]) for r in rows], [("o/a", "Devin")])
        self.assertEqual(dropped, 1)

    def test_main_rebuilds_both_commit_csvs_from_their_own_shards(self):
        self._repo("o/published", {"CLAUDE.md": "Instructions\n"})
        names = ("o/published", "o/unpublished")
        self._shards(self.tmp / "collect_commits", *names)
        self._commits(*names)
        other_rows = []
        for name in names:
            row = {c: "" for c in _mod.commits.COMMIT_COLUMNS}
            row.update(repo_name=name, commit_sha="3" * 40, ai_tool="Devin")
            other_rows.append(row)
            path = _mod.commits.other_shard_path(self.tmp / "collect_commits", name)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_mod.commits.COMMIT_COLUMNS)
                writer.writeheader()
                writer.writerow(row)
        with (self.output / "other_ai_commits.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_mod.commits.COMMIT_COLUMNS)
            writer.writeheader()
            writer.writerows(other_rows)
        self.assertEqual(self._run(), 0)
        self.assertEqual([(r["repo_name"], r["ai_tool"], r["commit_sha"])
                          for r in self._out_rows("all_ai_commits.csv")],
                         [("o/published", "Claude", "2" * 40)])
        self.assertEqual([(r["repo_name"], r["ai_tool"], r["commit_sha"])
                          for r in self._out_rows("other_ai_commits.csv")],
                         [("o/published", "Devin", "3" * 40)])

    def test_the_shards_that_the_reclassification_left_out_stay_out(self):
        """The commit reclassification leaves out the shards of a repository with evidence that
        failed validation, or of a repository that other commit heuristics classified. It names
        these repositories in `commit-reclassification.json` beside the CSV, with the SHA-256
        of each record. A CSV rebuilt from every shard would contain them again. A record that
        changed since, as after a new collection, is not left out, and an unreadable
        `commit-reclassification.json` leaves out nothing."""
        commits_dir = self.tmp / "collect_commits"
        self._shards(commits_dir, "o/a", "o/stale", "o/collected-again")
        digest = {repo: hashlib.sha256((commits_dir / f"{repo.replace('/', '§')}.json")
                                       .read_bytes()).hexdigest()
                  for repo in ("o/stale", "o/collected-again")}
        report = self.output / "commit-reclassification.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"repositories": {
            "o/a": {"outcome": "current"},
            **{repo: {"outcome": "history_incomplete", "left_out": True,
                      "record_sha256": digest[repo]} for repo in digest}}}))
        (commits_dir / "o§collected-again.json").write_text(json.dumps({
            "repo_name": "o/collected-again", "outcome": "history_read", "commits_read": 2,
            "commits_matched": 1, "unmatched": [], "no_signal_shas": [], "ai_rows": []}) + "\n")
        CSV = self.output / "all_ai_commits.csv"
        repos = {"o/a", "o/stale", "o/collected-again"}
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.restrict_ai_commits(CSV, repos, commits_dir)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual(sorted(r["repo_name"] for r in rows), ["o/a", "o/collected-again"])
        report.write_text("{not json")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            _mod.restrict_ai_commits(CSV, repos, commits_dir)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual(len(rows), 3)
        self.assertIn("Left no shard out of the commit CSVs", err.getvalue())

    def test_without_shards_the_commit_csv_is_restricted_in_place_as_before(self):
        CSV = self._commits("o/a", "o/gone")
        dropped = _mod.restrict_ai_commits(CSV, {"o/a"}, self.tmp / "no_shards_here")
        self.assertEqual(dropped, 1)
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertEqual([r["repo_name"] for r in rows], ["o/a"])

    def test_the_commit_csv_restricted_in_place_ends_each_record_with_lf(self):
        """Without shards, the restricted commit CSV ends each record with LF too."""
        CSV = self._commits("o/a", "o/b", "o/gone")
        _mod.restrict_ai_commits(CSV, {"o/a", "o/b"}, self.tmp / "no_shards_here")
        self.assertNotIn(b"\r\n", CSV.read_bytes())
        rows = list(csv.DictReader(CSV.open(newline="", encoding="utf-8")))
        self.assertEqual([r["repo_name"] for r in rows], ["o/a", "o/b"])

    def test_the_shard_directory_is_the_cloning_step_s(self):
        from agentcontext.steps import repository_cloning
        self.assertEqual(_mod.COMMITS_DIR.name, repository_cloning.COMMITS_DIR_NAME)


# ---------------------------------------------------------------------------
# The AI-authorship join
# ---------------------------------------------------------------------------

class TestTheAiFactsJoin(_StepCase):
    """The AI columns of `context-files.csv`, joined from `ai_authorship.csv`.

    The join's key is `(repo_name, head_sha, configuration artifact)`, the key of
    `file_history.csv` without the mechanism. A row applies to the commit that it names and to
    no other.
    """

    def test_a_context_file_with_a_row_gets_its_values(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._ai_fact("o/a", "CLAUDE.md", created="TRUE", tool="Claude",
                      ai_commits="3", tools="Claude;Copilot")
        self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        self.assertEqual(row["first_commit_ai_created"], "TRUE")
        self.assertEqual(row["first_commit_ai_tool"], "Claude")
        self.assertEqual(row["#ai_commits"], "3")
        self.assertEqual(row["ai_commit_tools"], "Claude;Copilot")

    def test_a_context_file_with_no_row_gets_blank_columns(self):
        """The columns are blank, not FALSE. A blank records that nobody looked, and FALSE would
        record that the file has no AI commit."""
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._ai_fact("o/b", "CLAUDE.md")  # another repository's row
        self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        for column in heuristics.AI_COLUMNS:
            self.assertEqual(row[column], "", column)

    def test_a_blank_row_is_reported(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._run()
        self.assertIn("ai_authorship.csv", self.stderr)

    def test_an_absent_facts_file_is_not_an_error(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self.assertFalse(self.ai_authorship_csv.exists())
        self.assertEqual(self._run(), 0)
        self.assertEqual(
            self._out_rows("context-files.csv")[0]["first_commit_ai_created"], "")

    def test_the_join_is_keyed_on_the_artifact_as_well(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n", "AGENTS.md": "# agents\n"})
        self._ai_fact("o/a", "CLAUDE.md", created="TRUE")
        self.assertEqual(self._run(), 0)
        rows = {r["context_file"]: r for r in self._out_rows("context-files.csv")}
        self.assertEqual(rows["CLAUDE.md"]["first_commit_ai_created"], "TRUE")
        self.assertEqual(rows["AGENTS.md"]["first_commit_ai_created"], "")

    def test_a_row_for_another_commit_of_the_same_repository_is_not_used(self):
        self._repo("o/a", {"CLAUDE.md": "# hi\n"})
        self._ai_fact("o/a", "CLAUDE.md", created="TRUE", head_sha="e" * 40)
        self.assertEqual(self._run(), 0)
        row = self._out_rows("context-files.csv")[0]
        for column in heuristics.AI_COLUMNS:
            self.assertEqual(row[column], "", column)

    def test_a_facts_file_without_the_key_columns_is_refused(self):
        with self.ai_authorship_csv.open("w", newline="", encoding="utf-8") as f:
            f.write("repo_name,#ai_commits\no/a,3\n")
        with self.assertRaises(ValueError):
            _mod.load_ai_authorship(self.ai_authorship_csv)

    def test_a_facts_file_keyed_without_the_commit_is_refused(self):
        """A cache keyed `(repo_name, configuration artifact)` would apply to every commit
        of a repository."""
        with self.ai_authorship_csv.open("w", newline="", encoding="utf-8") as f:
            f.write("repo_name,artifact," + ",".join(heuristics.AI_COLUMNS)
                    + "\no/a,CLAUDE.md,TRUE,Claude,1,Claude\n")
        with self.assertRaises(ValueError):
            _mod.load_ai_authorship(self.ai_authorship_csv)


class TestDetectAgentWorkflows(_RepoCase):
    """A workflow that runs a documented agent action is that tool's configuration wherever
    the repository put it. The file name cannot show this, because the Gemini action's example
    workflows each use the action under a name that a repository may change."""

    CLAUDE = "jobs:\n  go:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
    GEMINI = "jobs:\n  go:\n    steps:\n      - uses: 'google-github-actions/run-gemini-cli@v0'\n"

    def test_a_renamed_workflow_is_found_and_attributed_to_its_action(self):
        self._file(".github/workflows/nightly-agent.yml", self.CLAUDE)
        self.assertEqual(_mod.detect_agent_workflows(self.repo),
                         [(".github/workflows/nightly-agent.yml", "claude")])

    def test_the_quoted_form_the_gemini_examples_use_is_found(self):
        self._file(".github/workflows/gemini-review.yml", self.GEMINI)
        self.assertEqual(_mod.detect_agent_workflows(self.repo),
                         [(".github/workflows/gemini-review.yml", "gemini")])

    def test_naming_the_action_without_running_it_is_not_a_match(self):
        self._file(".github/workflows/ci.yml",
                   "# anthropics/claude-code-action is what we use elsewhere\n"
                   "jobs:\n  go:\n    steps:\n      - run: make\n")
        self._file(".github/workflows/other.yml",
                   "jobs:\n  go:\n    steps:\n      - uses: actions/checkout@v5\n"
                   "        with:\n          repository: anthropics/claude-code-action\n")
        self.assertEqual(_mod.detect_agent_workflows(self.repo), [])

    def test_a_repository_without_workflows_finds_none(self):
        self.assertEqual(_mod.detect_agent_workflows(self.repo), [])

    def test_an_agentic_workflow_is_its_engine_s_and_a_readme_is_nobody_s(self):
        self._file(".github/workflows/fix.md", "---\non: issues\nengine:\n  id: codex\n---\nFix.\n")
        self._file(".github/workflows/triage.md", "---\non: issues\n---\nLabel.\n")
        self._file(".github/workflows/README.md", "# Workflows\n")
        self.assertEqual(_mod.detect_agent_workflows(self.repo),
                         [(".github/workflows/fix.md", "codex"),
                          (".github/workflows/triage.md", "copilot")])

    def test_every_documented_action_is_read_from_the_heuristics(self):
        """The actions come from the rows of the heuristics, and another vendor's action needs
        a row rather than an edit here."""
        self.assertEqual(set(_mod.WORKFLOW_ACTIONS.values()), {"claude", "codex", "gemini", "opencode"})
        self.assertIn("anthropics/claude-code-action", _mod.WORKFLOW_ACTIONS)


class TestEachMechanismRetainsARepository(_RepoCase):

    def test_each_named_mechanism_alone_retains_a_repository(self):
        examples = {
            'CLAUDE.md': '# Project instructions\n',
            '.claude/settings.json': '{}',
            '.claude/skills/pdf/SKILL.md': 'Read PDFs.\n',
            '.gemini/agents/reviewer.md': '---\nname: reviewer\ndescription: Reviews code.\n---\nReview.\n',
            '.cursor/commands/review.md': 'Review.\n',
            '.cursor/hooks.json': '{"hooks": {"stop": [{"command": "true"}]}}',
            '.cursor/rules/style.mdc': 'Use explicit names.\n',
            '.cursor/mcp.json': '{"mcpServers": {"x": {"command": "uvx"}}}',
            '.cursor-plugin/plugin.json': '{"name": "example"}',
            '.github/lsp.json': '{"lspServers": {}}',
        }
        for index, (relative, content) in enumerate(examples.items()):
            with self.subTest(path=relative):
                repo = self.tmp / f'example-{index}'
                path = repo / relative
                path.parent.mkdir(parents=True)
                path.write_text(content)
                self.assertTrue(_mod.has_detected_mechanism(repo))

    def test_unclassified_content_alone_does_not_retain_a_repository(self):
        self._file('.claude/notes.txt', 'Personal notes.\n')
        self.assertFalse(_mod.has_detected_mechanism(self.repo))

class TestCursorPluginDiscovery(_RepoCase):
    MANIFEST = '{"name":"review"}'

    def test_default_components_are_collected_and_detected(self):
        files = {
            'p/.cursor-plugin/plugin.json': self.MANIFEST,
            'p/agents/reviewer.mdc': 'Review.',
            'p/commands/check.txt': 'Check.',
            'p/hooks/hooks.json': '{"hooks":{"stop":[{"command":"true"}]}}',
            'p/rules/style.markdown': 'Use types.',
            'p/.mcp.json': '{"mcpServers":{"one":{"command":"true"}}}',
            'p/mcp.json': '{"mcpServers":{"two":{"command":"true"}}}',
            'p/SKILL.md': '# Review\n',
        }
        for path, content in files.items():
            self._file(path, content)
        from agentcontext import paths
        copied = self.tmp / 'collected'
        self.assertEqual(paths.collect_plugin_parts(self.repo, copied, set()), {'cursor'})
        self.assertEqual({p.relative_to(copied).as_posix() for p in copied.rglob('*') if p.is_file()}, set(files))
        self.assertEqual([row['subagent'] for row in detect_subagents(copied)], ['p/agents/reviewer.mdc'])
        self.assertEqual([row['command'] for row in detect_commands(copied)], ['p/commands/check.txt'])
        self.assertEqual(detect_hooks(copied), ['p/hooks/hooks.json'])
        self.assertEqual([row['rule'] for row in detect_rules(copied)], ['p/rules/style.markdown'])
        self.assertEqual(set(detect_mcp(copied)), {'p/.mcp.json', 'p/mcp.json'})
        self.assertEqual([row['skills.md'] for row in detect_skills(copied)], ['p/SKILL.md'])

    def test_manifest_component_paths_replace_defaults(self):
        self._file('p/.cursor-plugin/plugin.json', json.dumps({
            'name':'review', 'skills':'./custom-skills', 'rules':'./custom-rules',
            'agents':'./custom-agents', 'commands':'./custom-commands',
            'hooks':'./custom-hooks.json', 'mcpServers':'./custom-mcp.json'}))
        for prefix in ('', 'custom-'):
            for path, content in {
                f'p/{prefix}skills/review/SKILL.md':'Review.',
                f'p/{prefix}rules/style.md':'Use types.',
                f'p/{prefix}agents/review.md':'Review.',
                f'p/{prefix}commands/check.md':'Check.',
                ('p/hooks/hooks.json' if not prefix else 'p/custom-hooks.json'):
                    '{"hooks":{"stop":[{"command":"true"}]}}',
                ('p/mcp.json' if not prefix else 'p/custom-mcp.json'):
                    '{"mcpServers":{"x":{"command":"true"}}}',
            }.items():
                self._file(path, content)
        self._file('p/.mcp.json', '{"mcpServers":{"old":{"command":"true"}}}')
        self.assertEqual([r['skills.md'] for r in detect_skills(self.repo)], ['p/custom-skills/review/SKILL.md'])
        self.assertEqual([r['subagent'] for r in detect_subagents(self.repo)], ['p/custom-agents/review.md'])
        self.assertEqual([r['command'] for r in detect_commands(self.repo)], ['p/custom-commands/check.md'])
        self.assertEqual([r['rule'] for r in detect_rules(self.repo)], ['p/custom-rules/style.md'])
        self.assertEqual(detect_hooks(self.repo), ['p/custom-hooks.json'])
        # With the manifest's mcpServers, the default .mcp.json is not Cursor's. It stays
        # Copilot CLI's and Claude Code's, because both read the .mcp.json of the directory that
        # they work in, and Claude Code reads its servers under "mcpServers".
        self.assertEqual(_mcp_tools(self.repo), {'p/.mcp.json': {'claude', 'copilot'},
                                                 'p/custom-mcp.json': {'cursor'}})

    def test_project_settings_subagent_extensions_and_nested_commands(self):
        self._file('.cursor/settings.json', '{"plugins":{}}')
        for extension in ('md', 'mdc', 'markdown'):
            self._file(f'.cursor/agents/review.{extension}', '---\nname: review\n---\nReview.\n')
        self._file('.cursor/commands/review.md', 'Review.')
        self._file('.cursor/commands/nested/grouped.md', 'Read by the IDE.')
        self.assertIn('.cursor/settings.json', detect_settings(self.repo))
        self.assertEqual({r['subagent'] for r in detect_subagents(self.repo)},
                         {'.cursor/agents/review.md', '.cursor/agents/review.mdc',
                          '.cursor/agents/review.markdown'})
        self.assertEqual(sorted(r['command'] for r in detect_commands(self.repo)),
                         ['.cursor/commands/nested/grouped.md', '.cursor/commands/review.md'])


class TestCopilotPluginAgentReadmes(unittest.TestCase):
    def test_a_portable_plugin_readme_with_agent_front_matter_is_an_agent(self):
        with tempfile.TemporaryDirectory() as temporary:
            tree = Path(temporary)
            (tree / "plugin.json").write_text(PORTABLE_MANIFEST)
            agent = tree / "com.github.copilot/agents/README.md"
            agent.parent.mkdir(parents=True)
            agent.write_text("---\nname: reader\ndescription: Read files\n---\nRead files.\n")
            self.assertEqual([row["subagent"] for row in detect_subagents(tree)],
                             ["com.github.copilot/agents/README.md"])

    def test_default_and_declared_plugin_readmes_are_agents(self):
        for declared in (False, True):
            with self.subTest(declared=declared), tempfile.TemporaryDirectory() as temporary:
                tree = Path(temporary)
                manifest = tree / "plugins/p/.plugin/plugin.json"
                manifest.parent.mkdir(parents=True)
                manifest.write_text(json.dumps({"name": "p", **({"agents": "custom"} if declared else {})}))
                folder = tree / "plugins/p" / ("custom" if declared else "agents")
                folder.mkdir()
                (folder / "README.md").write_text("---\nname: reader\ndescription: Read files\n---\nRead files.\n")
                project = tree / ".github/agents"
                project.mkdir(parents=True)
                (project / "README.md").write_text("Documentation.\n")
                self.assertEqual([row["subagent"] for row in detect_subagents(tree)],
                                 [str((folder / "README.md").relative_to(tree))])

class TestClaudePluginMcpMaps(_RepoCase):
    def test_flat_and_wrapped_plugin_servers_are_detected(self):
        for name, data in (("flat", {"server": {"command": "echo"}}),
                           ("http", {"server": {"type": "http", "url": "https://example.invalid/mcp"}}),
                           ("wrapped", {"mcpServers": {"server": {"command": "echo"}}})):
            self._file(f"plugins/{name}/.claude-plugin/plugin.json", '{"name":"test"}')
            self._file(f"plugins/{name}/.mcp.json", json.dumps(data))
        self._file("plugins/invalid/.claude-plugin/plugin.json", '{"name":"test"}')
        self._file("plugins/invalid/.mcp.json", '{"unrelated": 42}')
        self.assertEqual([name for name in ("flat", "http", "wrapped", "invalid")
                          if _mod._plugin_mcp_servers(
                              json.loads((self.repo / f"plugins/{name}/.mcp.json").read_text()),
                              "claude")],
                         ["flat", "http", "wrapped"])
        # Copilot CLI reads each of them that configures a server as the .mcp.json of the plugin's
        # directory, as the root_file row reads the .mcp.json of a plugin at the root.
        self.assertEqual(set(detect_mcp(self.repo)),
                         {"plugins/flat/.mcp.json", "plugins/http/.mcp.json",
                          "plugins/wrapped/.mcp.json"})

    def test_declared_flat_servers_are_detected(self):
        self._file("plugins/test/.claude-plugin/plugin.json",
                   '{"name":"test","mcpServers":"./servers.json"}')
        self._file("plugins/test/servers.json", '{"flatserver":{"command":"echo","args":["x"]}}')
        self.assertEqual(detect_mcp(self.repo), ["plugins/test/servers.json"])

    def test_a_server_with_a_url_and_no_type_is_a_server_of_codex_alone(self):
        """Claude Code reads a server without a `type` as a stdio server and skips one with a
        `url` alone, and Codex infers the transport from the `url`. A folder with both manifests
        shares its `.mcp.json`."""
        url_alone = '{"mcpServers": {"board": {"url": "http://127.0.0.1:7777/mcp"}}}'
        self._file("plugins/both/.claude-plugin/plugin.json", '{"name": "both"}')
        self._file("plugins/both/.codex-plugin/plugin.json", '{"name": "both"}')
        self._file("plugins/both/.mcp.json", url_alone)
        self._file("plugins/claude/.claude-plugin/plugin.json",
                   '{"name": "claude", "mcpServers": "./servers.json"}')
        self._file("plugins/claude/servers.json", url_alone)
        self.assertEqual(detect_mcp(self.repo), ["plugins/both/.mcp.json"])
        self.assertEqual(_mcp_tools(self.repo), {"plugins/both/.mcp.json": {"codex", "copilot"}})


class TestInvalidClaudePluginPaths(_RepoCase):
    def test_invalid_component_paths_disable_default_components(self):
        for key, value in (("hooks", "../hooks/hooks.json"), ("hooks", "./../outside.json"),
                           ("skills", "missing-prefix"), ("skills", "./absent"),
                           ("commands", {"check": {"source": "commands/check.md"}})):
            with self.subTest(key=key, value=value):
                self._file("plugins/test/.claude-plugin/plugin.json", json.dumps({"name":"test", key:value}))
                self._file("plugins/test/skills/default/SKILL.md", "Instructions")
                self._file("plugins/test/hooks/hooks.json", '{"hooks":{"Stop":[{"command": "true"}]}}')
                self._file("plugins/test/commands/check.md", "Check")
                self.assertEqual(detect_skills(self.repo), [])
                self.assertEqual(detect_hooks(self.repo), [])

    def test_valid_inline_components_and_explicit_skills_keep_defaults(self):
        self._file("plugins/test/.claude-plugin/plugin.json", json.dumps({
            "name":"test", "skills":"./extra", "hooks":{"hooks":{"Stop":[{"command": "true"}]}},
            "mcpServers":{"server":{"command":"echo"}}}))
        self._file("plugins/test/skills/default/SKILL.md", "Instructions")
        self._file("plugins/test/extra/additional/SKILL.md", "Instructions")
        self.assertEqual({row["skills.md"] for row in detect_skills(self.repo)},
                         {"plugins/test/skills/default/SKILL.md", "plugins/test/extra/additional/SKILL.md"})


class TestCopilotProjectDiscovery(_RepoCase):
    def test_nested_described_agents_and_described_readmes_are_loaded(self):
        self._file(".github/agents/security/review.agent.md", "---\ndescription: Review code\n---\nInstructions\n")
        self._file(".github/agents/README.md", "---\ndescription: Read files\n---\nInstructions\n")
        self._file(".github/agents/security/notes.md", "Documentation without agent front matter.\n")
        self._file(".github/agents/direct.md", "VS Code agent.\n")
        self.assertEqual({r["subagent"] for r in detect_subagents(self.repo)},
                         {".github/agents/security/review.agent.md", ".github/agents/README.md",
                          ".github/agents/direct.md"})
        self.assertTrue(_mod.has_detected_mechanism(self.repo))

    def test_copilot_marketplaces_require_name_owner_and_plugins(self):
        valid = {"name": "example", "owner": {"name": "Owner"}, "plugins": []}
        invalid = [{"name": "mem0-plugins", "interface": {"displayName": "Mem0"}, "plugins": []},
                   {**valid, "owner": {}}, {**valid, "plugins": {}}, {**valid, "name": 3}, []]
        for filename in ("marketplace.json", ".plugin/marketplace.json", ".github/plugin/marketplace.json"):
            for value in invalid + [valid]:
                with self.subTest(path=filename, value=value):
                    path = self._file(filename, json.dumps(value))
                    self.assertEqual(detect_plugins(self.repo), [filename] if value == valid else [])
                    path.unlink()


class TestAntigravityMcpJsonc(_RepoCase):
    def test_workspace_and_plugin_accept_comments_and_trailing_commas(self):
        content = '{ // server\n"mcpServers":{"test":{"command":"node",},},}'
        self._file(".agents/mcp_config.json", content)
        self._file(".agents/plugins/test/plugin.json", '{"name":"test"}')
        self._file(".agents/plugins/test/mcp_config.json", content)
        self.assertEqual(set(detect_mcp(self.repo)),
                         {".agents/mcp_config.json", ".agents/plugins/test/mcp_config.json"})


class TestClaudeMarketplaceComponents(_RepoCase):
    def _marketplace(self, entry, prefix=""):
        self._file(prefix + ".claude-plugin/marketplace.json",
                   json.dumps({"name": "test", "owner": {"name": "test"}, "plugins": [entry]}))

    def test_local_plugin_without_manifest_loads_standard_components(self):
        self._marketplace({"name": "test", "source": "./plugins/test"}, "nested/")
        self._file("nested/plugins/test/skills/build/SKILL.md", "Build")
        self._file("nested/plugins/test/commands/check.md", "Check")
        self.assertEqual({r["skills.md"] for r in detect_skills(self.repo)},
                         {"nested/plugins/test/skills/build/SKILL.md",
                          "nested/plugins/test/commands/check.md"})

    def test_marketplace_root_skill_selection_excludes_unlisted_skills(self):
        self._marketplace({"name": "test", "source": "./", "strict": False,
                           "skills": ["./skills/selected"]})
        self._file("skills/selected/SKILL.md", "Selected")
        self._file("skills/unlisted/SKILL.md", "Unlisted")
        self.assertEqual([r["skills.md"] for r in detect_skills(self.repo)],
                         ["skills/selected/SKILL.md"])

    def test_entry_appends_agents_to_manifest_and_keeps_implicit_defaults(self):
        self._marketplace({"name": "test", "source": "./plugins/test",
                           "agents": ["./extra/review.md"], "commands": ["./extra/check.md"]})
        self._file("plugins/test/.claude-plugin/plugin.json",
                   '{"name":"test","agents":["./selected/build.md"]}')
        self._file("plugins/test/selected/build.md", "Build")
        self._file("plugins/test/extra/review.md", "Review")
        self._file("plugins/test/extra/check.md", "Check")
        self._file("plugins/test/commands/default.md", "Default")
        self.assertEqual({r["subagent"] for r in detect_subagents(self.repo)},
                         {"plugins/test/selected/build.md", "plugins/test/extra/review.md"})
        self.assertEqual({r["skills.md"] for r in detect_skills(self.repo)},
                         {"plugins/test/extra/check.md", "plugins/test/commands/default.md"})

    def test_conflicting_entry_disables_components(self):
        self._marketplace({"name": "test", "source": "./plugins/test", "strict": False,
                           "skills": ["./skills/build"]})
        self._file("plugins/test/.claude-plugin/plugin.json", '{"name":"test"}')
        self._file("plugins/test/skills/build/SKILL.md", "Build")
        self.assertEqual(detect_skills(self.repo), [])

    def test_catalog_cannot_claim_sibling_or_remote_plugins(self):
        for source in ("../outside", "/outside", "https://example.invalid/plugin"):
            with self.subTest(source=source):
                self._marketplace({"name": "test", "source": source}, "catalog/")
                self._file("outside/skills/build/SKILL.md", "Build")
                self.assertEqual(detect_skills(self.repo), [])


class TestClaudeReviewContext(_RepoCase):
    def test_review_instructions_are_root_only_and_no_tool_s_own(self):
        """Claude Code Review and Copilot code review both read REVIEW.md. The file is
        therefore shared, as AGENTS.md is, and only the root file counts."""
        self._file("REVIEW.md", "Review instructions")
        self._file("nested/REVIEW.md", "Unrelated review")
        self._file(".claude/REVIEW.md", "Wrong location")
        self.assertEqual(detect_context_files(self.repo), ["REVIEW.md"])
        self.assertIsNone(_mod.tool_for_path("REVIEW.md"))
        self.assertIsNone(_mod.tool_for_path("nested/REVIEW.md"))

    def test_a_root_file_is_compared_in_its_case(self):
        """A `root_file` row names its path in its case, as on a case-sensitive volume.
        `Path.is_file` on a case-insensitive volume would accept a root `review.md` and
        `.claude/LOOP.md` and report them at `REVIEW.md` and `.claude/loop.md`, paths that exist
        in no tree."""
        self._file("review.md", "Review instructions")
        self._file(".claude/LOOP.md", "Loop")
        self.assertEqual(detect_context_files(self.repo), [])
        self.assertTrue(all(_spelled_on_disk(self.repo, rel)
                            for rel in _collect_all_spec_files(self.repo)))





class TestDocumentedDirectoryLayouts(_RepoCase):
    def test_copilot_prompts_and_chatmodes_are_direct_children(self):
        for directory, suffix in (("prompts", ".prompt.md"), ("chatmodes", ".chatmode.md")):
            self._file(f".github/{directory}/direct{suffix}")
            self._file(f".github/{directory}/nested/deep{suffix}")
        self.assertEqual([r["command"] for r in detect_commands(self.repo)],
                         [".github/prompts/direct.prompt.md"])
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".github/chatmodes/direct.chatmode.md"])

    def test_gemini_agents_exclude_nested_and_underscore_files(self):
        # Gemini CLI refuses a file without front matter, such as sveltejs/cli's snapshot
        # .gemini/agents/sv-files-snapshots.md, with "Missing mandatory YAML frontmatter", and it
        # requires a description for a local agent.
        agent = "---\nname: {}\ndescription: Reviews code.\n---\nReview.\n"
        self._file(".gemini/agents/direct.md", agent.format("direct"))
        self._file(".gemini/agents/README.md", "# The agents of this repository\n")
        self._file(".gemini/agents/_draft.md", agent.format("draft"))
        self._file(".gemini/agents/nested/deep.md", agent.format("deep"))
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".gemini/agents/direct.md"])

    def test_a_gemini_cli_agent_names_tools_that_gemini_cli_knows(self):
        """Gemini CLI refuses an agent with a `tools` entry that `isValidToolName` rejects, such as
        `client_view_file` in RirikoAI/RirikoBot's `.gemini/agents/devops.md` (path_precision
        pp-0667 of the evaluation of 2026-10-26), and a `tools` that is not a list. Such a file is an
        `other` row of Gemini's configuration directory."""
        agent = "---\nname: {}\ndescription: Works.\n{}---\nWork.\n"
        cases = {
            "antigravity": "tools:\n  - client_view_file\n  - grep_search\n  - run_command\n",
            "builtin": "tools:\n  - read_file\n  - search_file_content  # the legacy name\n",
            "flow": 'tools: [grep_search, "mcp_github_list_issues", \'*\',\n  mcp_*]\n',
            "same_indent": "tools:\n- glob\n- discovered_tool_lint\n",
            "empty_list": "tools: []\n",
            "none": "",
            "string": "tools: Read, Grep\n",
            "null": "tools:\n",
            "claude_names": "tools: [Read, Grep]\n",
            "open_flow": "tools: [read_file\n",
            "twice": "tools: [glob]\ntools: [glob]\n",
        }
        for name, tools in cases.items():
            self._file(f".gemini/agents/{name}.md", agent.format(name, tools))
        loaded = ["builtin", "empty_list", "flow", "none", "same_indent"]
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [f".gemini/agents/{name}.md" for name in loaded])
        self.assertEqual(_mod.tools_reading("subagents", ".gemini/agents/antigravity.md",
                                            self.repo / ".gemini/agents/antigravity.md",
                                            self.repo), (False, set()))
        self.assertEqual(_other_as_main_does(self.repo),
                         sorted(f".gemini/agents/{name}.md" for name in cases if name not in loaded))

    def test_an_agents_folder_that_a_claude_code_plugin_shares_keeps_the_agents_of_the_plugin(self):
        """A Gemini CLI extension and a Claude Code plugin at the root share `agents/`, as in
        TheQtCompanyRnD/agent-skills. Claude Code loads an agent with its own `tools: Read, Grep`
        and an agent with a name that starts with an underscore, although Gemini CLI refuses both.
        Both agents are subagent rows of Claude Code alone, because the extension's `agents/`
        gives Gemini only to an agent that Gemini CLI loads. In the `agents/` of an extension that
        no plugin shares, Gemini CLI's test still applies."""
        agent = "---\nname: {}\ndescription: Reviews.\ntools: Read, Grep\n---\nReview.\n"
        self._file("gemini-extension.json", '{"name": "qt", "version": "1.0.0"}')
        self._file(".claude-plugin/plugin.json", '{"name": "qt"}')
        self._file("agents/reviewer.md", agent.format("reviewer"))
        self._file("agents/_draft.md", agent.format("draft"))
        self._file("ext/gemini-extension.json", '{"name": "ext", "version": "1.0.0"}')
        self._file("ext/agents/reviewer.md", agent.format("reviewer"))
        rows = [("subagents", r["subagent"]) for r in detect_subagents(self.repo)]
        self.assertEqual(sorted(path for _mechanism, path in rows),
                         ["agents/_draft.md", "agents/reviewer.md"])
        found = _mod.artifact_tools(self.repo, rows, (), _mod.plugin_part_tools(self.repo))
        for path in ("agents/_draft.md", "agents/reviewer.md"):
            with self.subTest(path=path):
                self.assertEqual(found[("subagents", path)], {"claude"})

    def test_gemini_cli_accepts_the_tool_names_of_is_valid_tool_name(self):
        """The cases of `isValidToolName` with wildcards in tool-names.ts of gemini-cli at
        718e97c2448e0384cb582409cf4e479437bf50fe."""
        accepted = ["read_file", "invoke_agent", "list_mcp_resources", "search_file_content",
                    "discovered_tool_x", "*", "mcp_*", "mcp_github_*", "mcp_github_list_issues",
                    "mcp_my.server_tool:v2", "mcp_s_t_u"]
        rejected = ["Read", "client_view_file", "save_memory", "mcp_", "mcp__tool",
                    "mcp_server", "mcp_server_", "mcp_server___", "mcp_ser ver_tool",
                    "mcp_server_to ol", "", "read_file "]
        self.assertEqual([n for n in accepted if not _mod._gemini_cli_accepts_tool(n)], [])
        self.assertEqual([n for n in rejected if _mod._gemini_cli_accepts_tool(n)], [])

    def test_antigravity_agents_use_named_files_or_agent_directories(self):
        agent = "---\nname: {}\ndescription: Reviews code.\n---\nYou review code.\n"
        for root in (".agents", ".agent", "_agents", "_agent"):
            self._file(f"{root}/agents/direct.md", agent.format("direct"))
            self._file(f"{root}/agents/reviewer/agent.md", agent.format("reviewer"))
            self._file(f"{root}/agents/reviewer/notes.md", agent.format("notes"))
            self._file(f"{root}/agents/reviewer/deep/agent.md", agent.format("deep"))
        expected = {f"{root}/agents/{rel}": name
                    for root in (".agents", ".agent", "_agents", "_agent")
                    for rel, name in (("direct.md", "direct"), ("reviewer/agent.md", "reviewer"))}
        self.assertEqual({r["subagent"]: r["name"] for r in detect_subagents(self.repo)}, expected)

    def test_an_antigravity_agent_without_a_name_and_a_description_is_not_one(self):
        """Antigravity CLI 1.3.5 lists an agent only when its front matter has a non-empty
        `name` and `description`. scaffold-eth/scaffold-eth-2 keeps a prompt without front
        matter in `.agents/agents/`, which is therefore no agent."""
        self._file(".agents/agents/good.md", "---\nname: good\ndescription: Reviews.\n---\nBody.\n")
        self._file(".agents/agents/no-front-matter.md", "You are Carlos, a grumpy reviewer.\n")
        self._file(".agents/agents/name-only.md", "---\nname: name-only\n---\nBody.\n")
        self._file(".agents/agents/description-only.md", "---\ndescription: Reviews.\n---\nBody.\n")
        self._file(".agents/agents/empty.md", '---\nname: empty\ndescription: ""\n---\nBody.\n')
        self._file(".agents/agents/folder/agent.md", "---\nname: folder\n---\nBody.\n")
        self._file(".agents/agents/no-body.md", "---\nname: no-body\ndescription: Lists.\n---\n")
        self.assertEqual([r["subagent"] for r in detect_subagents(self.repo)],
                         [".agents/agents/good.md", ".agents/agents/no-body.md"])

    def test_antigravity_hooks_count_when_antigravity_loads_them(self):
        """Antigravity CLI 1.3.5 drops a whole hooks.json that it cannot decode. A repository can
        keep Cursor's format there, `"version": 1` beside `"hooks"`, and Antigravity rejects that
        format as a hook named `version` that is not an object."""
        loads = {
            "documented": '{"lint": {"PostToolUse": [{"matcher": "run_command", "hooks": '
                          '[{"type": "command", "command": "./lint.sh", "timeout": 10}]}]}}',
            "cursor_without_version": '{"hooks": {"preToolUse": [{"command": "x", "matcher": "Shell"}]}}',
            "any_case_and_null": '{"a": {"ENABLED": false, "stop": null}, "b": null}',
            "grouped_stop": '{"a": {"Stop": [{"matcher": "", "hooks": [{"command": "x"}]}]}}',
            "timeout_bounds": '{"a": {"Stop": [{"command": "x", "timeout": 2147483647}, '
                              '{"command": "y", "timeout": -5}]}}',
        }
        drops = {
            "cursor": '{"version": 1, "hooks": {"preToolUse": [{"command": "x"}]}}',
            "comment": '// lint\n{"lint": {"Stop": [{"command": "x"}]}}',
            "trailing_comma": '{"lint": {"Stop": [{"command": "x"}],}}',
            "byte_order_mark": '\ufeff{"lint": {"Stop": [{"command": "x"}]}}',
            "enabled_string": '{"lint": {"enabled": "yes"}}',
            "event_string": '{"lint": {"PreToolUse": "x"}}',
            "matcher_number": '{"lint": {"PreToolUse": [{"matcher": 5}]}}',
            "hooks_object": '{"lint": {"PreToolUse": [{"matcher": "x", "hooks": {"a": 1}}]}}',
            "command_number": '{"lint": {"Stop": [{"command": 5}]}}',
            "timeout_fraction": '{"lint": {"Stop": [{"command": "x", "timeout": 1.5}]}}',
            "timeout_string": '{"lint": {"Stop": [{"command": "x", "timeout": "10"}]}}',
            "timeout_over_32_bits": '{"lint": {"Stop": [{"command": "x", "timeout": 2147483648}]}}',
            "http_handler": '{"lint": {"Stop": [{"type": "http", "url": "x"}]}}',
            "list": '[{"PreToolUse": []}]',
            "empty": "{}",
        }
        for name, text in {**loads, **drops}.items():
            with self.subTest(name=name):
                self._file(".agents/hooks.json", text)
                self.assertEqual(detect_hooks(self.repo), [".agents/hooks.json"] if name in loads else [])

    def test_a_linked_antigravity_hooks_file_is_tested_through_its_marker(self):
        """An `.agents/hooks.json` that is a link arrives as a marker, and the test of its
        named hooks reads the file that the marker names, as the front matter tests of a linked
        rule or agent do. The marker's one line is no JSON and contains no hooks."""
        self._file("config/hooks.json", '{"lint": {"Stop": [{"command": "./lint.sh"}]}}')
        self._file("config/notes.json", '{"version": 1, "hooks": {}}')
        self._file(".agents/hooks.json", "../config/hooks.json\n")
        self._file(".agent/hooks.json", "../config/notes.json\n")
        self.assertEqual(detect_hooks(self.repo), [".agents/hooks.json"])
        found = _mod.artifact_tools(self.repo, [("hooks", ".agents/hooks.json")])
        self.assertEqual(found, {("hooks", ".agents/hooks.json"): {"gemini"}})

    def test_a_linked_copilot_hook_file_is_tested_through_its_marker(self):
        """A hook file of `.github/hooks` that is a link arrives as a marker, and its test reads
        the file that the marker names. The marker's one line is no JSON. A test at the marker
        would find no hook, the collected file cleanup would delete the target, and a
        repository with the hook file as its only configuration would no longer count as
        configured."""
        self._file("config/a.json", '{"hooks": {"sessionStart": [{"command": "./start.sh"}]}}')
        self._file("config/b.json", '{"name": "data"}')
        self._file(".github/hooks/a.json", "../../config/a.json\n")
        self._file(".github/hooks/b.json", "../../config/b.json\n")
        self.assertEqual(detect_hooks(self.repo), [".github/hooks/a.json"])

    def test_copilot_hooks_include_nested_json_only_with_hooks(self):
        self._file(".github/hooks/team/build.json", '{"hooks":{"sessionStart":[{"command": "true"}]}}')
        self._file(".github/hooks/team/data.json", '{"name":"data"}')
        self.assertEqual(detect_hooks(self.repo), [".github/hooks/team/build.json"])

    def test_alias_directories_survive_collection_and_validation(self):
        from agentcontext import paths
        from agentcontext.steps import collected_file_cleanup
        for root in (".agent", "_agents", "_agent"):
            self._file(f"{root}/hooks.json", '{"lint":{"Stop":[]}}')
            self._file(f"{root}/mcp_config.json", '{"mcpServers":{"x":{"command":"x"}}}')
            self._file(f"{root}/plugins/demo/plugin.json", '{"name":"demo"}')
            self._file(f"{root}/workflows/build.md", "Build")
        with tempfile.TemporaryDirectory() as temporary:
            collected = Path(temporary)
            paths.collect_repo(self.repo, collected, set())
            collected_file_cleanup.scan_and_clean_repo(str(collected), [])
            for root in (".agent", "_agents", "_agent"):
                self.assertIn(f"{root}/hooks.json", detect_hooks(collected))
                self.assertIn(f"{root}/mcp_config.json", detect_mcp(collected))
                self.assertIn(f"{root}/plugins/demo/plugin.json", _mod.detect_plugins(collected))
                self.assertIn(f"{root}/workflows/build.md", [r["command"] for r in detect_commands(collected)])


class TestDeclaredComponentNames(_RepoCase):
    def test_declared_copilot_agent_drops_the_double_suffix(self):
        self._file('p/.plugin/plugin.json', '{"name":"p","agents":"./custom"}')
        self._file('p/custom/review.agent.md', '---\ndescription: Review\n---\nRead code')
        self.assertEqual(detect_subagents(self.repo), [
            {'subagent': 'p/custom/review.agent.md', 'name': 'review', 'memory': 'n/a'}])

    def test_claude_command_map_uses_the_declared_name(self):
        self._file('p/.claude-plugin/plugin.json',
                   '{"name":"p","commands":{"status":{"source":"./commands/deploy-status.md"}}}')
        self._file('p/commands/deploy-status.md', 'Show status')
        self.assertEqual([(r['skills.md'], r['name']) for r in detect_skills(self.repo)],
                         [('p/commands/deploy-status.md', 'status')])

    def test_marketplace_command_names_survive_mixed_component_forms(self):
        self._file('.claude-plugin/marketplace.json',
                   '{"name":"local","plugins":[{"name":"p","source":"./p",'
                   '"commands":{"status":{"source":"./custom/status-file.md"}}}]}')
        self._file('p/.claude-plugin/plugin.json', '{"name":"p","commands":["./custom/build.md"]}')
        self._file('p/custom/status-file.md', 'Status')
        self._file('p/custom/build.md', 'Build')
        self.assertEqual({r['name'] for r in detect_skills(self.repo)}, {'build', 'status'})

    def test_several_names_for_one_file_use_alphabetical_first(self):
        self._file('p/.claude-plugin/plugin.json',
                   '{"name":"p","commands":{"z":{"source":"./c.md"},"a":{"source":"./c.md"}}}')
        self._file('p/c.md', 'Command')
        self.assertEqual([r['name'] for r in detect_skills(self.repo)], ['a'])

    # The manifest as a repository can commit it: JSON text, ASCII only, with the escape of a
    # lone surrogate in a key, which `json.loads` returns in the string.
    SURROGATE_MANIFEST = ('{"name": "p", "commands": '
                          '{"de\\ud800ploy": {"source": "./commands/deploy.md"}}}\n')

    def test_a_command_name_that_does_not_encode_keeps_the_file_stem(self):
        """If the name reached skills.csv, the strict UTF-8 write of the CSV would raise after
        the step had replaced repos.csv and the CSVs before it, at every Start."""
        self._file('plugins/p/.claude-plugin/plugin.json', self.SURROGATE_MANIFEST)
        self._file('plugins/p/commands/deploy.md', '# Deploy\n\nDeploy the service to staging.\n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rows = detect_skills(self.repo)
        self.assertEqual([(r['skills.md'], r['name']) for r in rows],
                         [('plugins/p/commands/deploy.md', 'deploy')])
        self.assertIn("WARNING detection: Ignored the command name 'de\\ud800ploy' that the "
                      "plugin of owner/repo at plugins/p declares for "
                      "plugins/p/commands/deploy.md, because it does not encode as UTF-8.",
                      err.getvalue())
        output = self.tmp / 'output'
        _mod.write_output_table('skills.csv', list(rows[0]), rows, output_dir=output)
        with (output / 'skills.csv').open(newline='', encoding='utf-8') as f:
            self.assertEqual([row['name'] for row in csv.DictReader(f)], ['deploy'])

    def test_a_name_that_does_not_encode_leaves_the_other_names_of_its_file(self):
        self._file('p/.claude-plugin/plugin.json', '{"name":"p","commands":'
                   '{"a\\udc80":{"source":"./c.md"},"b":{"source":"./c.md"}}}')
        self._file('p/c.md', 'Command')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual([r['name'] for r in detect_skills(self.repo)], ['b'])


class TestCodexPluginOverrides(_RepoCase):
    def _plugin(self, **fields):
        self._file('p/.codex-plugin/plugin.json', json.dumps({'name': 'p', **fields}))
        self._file('p/skills/default/SKILL.md', '---\ndescription: Default skill\n---\nDefault skill\n')
        self._file('p/.mcp.json', '{"mcpServers":{"default":{"command":"x"}}}')
        self._file('p/hooks/hooks.json', '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}')

    def test_paths_replace_compatibility_defaults(self):
        self._plugin(skills='./custom', mcpServers='./custom-mcp.json', hooks='./custom-hooks.json')
        self._file('p/custom/selected/SKILL.md', '---\ndescription: Selected skill\n---\nSelected skill\n')
        self._file('p/custom-mcp.json', '{"mcpServers":{"selected":{"command":"x"}}}')
        self._file('p/custom-hooks.json', '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}')
        self.assertEqual([r['skills.md'] for r in detect_skills(self.repo)], ['p/custom/selected/SKILL.md'])
        # With the manifest's mcpServers, the default .mcp.json is not Codex's. It stays
        # Copilot CLI's and Claude Code's, because both read the .mcp.json of the directory that
        # they work in, and Claude Code reads its servers under "mcpServers".
        self.assertEqual(_mcp_tools(self.repo), {'p/.mcp.json': {'claude', 'copilot'},
                                                 'p/custom-mcp.json': {'codex'}})
        self.assertEqual(detect_hooks(self.repo), ['p/custom-hooks.json'])

    def test_empty_inline_hooks_suppress_the_default_file(self):
        self._plugin(hooks={})
        self.assertEqual(detect_hooks(self.repo), [])

    def test_invalid_or_empty_paths_keep_defaults(self):
        for value in ([], False, 3, '', './', './..', 'outside'):
            with self.subTest(value=value):
                self._plugin(skills=value, hooks=value)
                self.assertIn('p/skills/default/SKILL.md', [r['skills.md'] for r in detect_skills(self.repo)])
                self.assertIn('p/hooks/hooks.json', detect_hooks(self.repo))

    def test_inline_components_are_attributed_to_the_manifest(self):
        self._plugin(hooks={'hooks': {'Stop': []}}, mcpServers={'x': {'command': 'x'}})
        self.assertEqual(detect_hooks(self.repo), ['p/.codex-plugin/plugin.json'])
        self.assertEqual(_mcp_tools(self.repo), {'p/.mcp.json': {'claude', 'copilot'},
                                                 'p/.codex-plugin/plugin.json': {'codex'}})

    def test_portable_openai_extension_overrides_hooks_only(self):
        self._file('p/plugin.json', json.dumps({
            '$schema': 'https://agent-plugins.org/schemas/1.0.0/plugin.schema.json', 'name': 'p',
            'extensions': {'com.openai': {'hooks': './custom-hooks.json', 'skills': './ignored'}}}))
        self._file('p/hooks/hooks.json', '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}')
        self._file('p/custom-hooks.json', '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"true"}]}]}}')
        self._file('p/skills/default/SKILL.md', 'Skill')
        self._file('p/ignored/other/SKILL.md', 'Unselected')
        self.assertEqual(detect_hooks(self.repo), ['p/custom-hooks.json'])
        self.assertIn(('p/custom-hooks.json', 'codex'),
                      [(path, tool) for path, tool, _row in _mod.plugin_part_tools(self.repo)])
        self.assertIn('p/skills/default/SKILL.md', [r['skills.md'] for r in detect_skills(self.repo)])


class TestTheReadersThatAMarketplaceOrAManifestIdentifies(_RepoCase):
    """A row names every tool that a file of the repository identifies as a reader: a vendor's
    manifest, or a vendor's marketplace that lists the folder. The portable manifest alone names
    no tool. Codex reads the `skills/` and `mcp.json` of a portable plugin that its marketplace
    lists or that has `.codex-plugin/plugin.json` beside the portable manifest, and Copilot CLI
    reads the `.claude-plugin/plugin.json` of a folder that a Copilot marketplace lists as its
    legacy manifest."""

    SKILL = "---\nname: {}\ndescription: Does it.\n---\n"
    PORTABLE = json.dumps({"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                           "name": "p"})
    SERVERS = '{"mcpServers": {"x": {"command": "x"}}}'

    def _tools(self) -> dict:
        """{(mechanism, path): tool value} of the skills, MCP and plugins rows, as main writes the
        tool column."""
        rows = ([("skills", r["skills.md"]) for r in detect_skills(self.repo)]
                + [("mcp", p) for p in detect_mcp(self.repo)]
                + [("plugins", p) for p in _mod.detect_plugins(self.repo)])
        found = _mod.artifact_tools(self.repo, rows, (), _mod.plugin_part_tools(self.repo),
                                    _mod.declared_file_tools(self.repo))
        return {key: heuristics.tool_value(tools) for key, tools in found.items()}

    def _marionette(self, codex_marketplace: bool) -> None:
        """marionettejs/marionette's `plugins/marionette`, which its Claude Code and Cursor
        marketplaces list, and its Codex marketplace too where `codex_marketplace` holds."""
        self._file("plugins/marionette/plugin.json", self.PORTABLE)
        self._file("plugins/marionette/.claude-plugin/plugin.json", '{"name": "marionette"}')
        self._file("plugins/marionette/mcp.json", self.SERVERS)
        self._file("plugins/marionette/skills/marionette/SKILL.md", self.SKILL.format("marionette"))
        self._file(".claude-plugin/marketplace.json", json.dumps({
            "name": "m", "owner": {"name": "o"},
            "plugins": [{"name": "marionette", "source": "./plugins/marionette"}]}))
        self._file(".cursor-plugin/marketplace.json", json.dumps({
            "name": "m", "plugins": [{"name": "marionette", "source": "plugins/marionette"}]}))
        if codex_marketplace:
            self._file(".agents/plugins/marketplace.json", json.dumps({
                "name": "m", "plugins": [{"name": "marionette", "source": {
                    "source": "local", "path": "./plugins/marionette"}, "category": "Coding"}]}))

    def test_a_codex_marketplace_makes_codex_a_reader_of_a_portable_plugin(self):
        """pp-0515 and pp-0256 of the detection accuracy evaluations."""
        self._marionette(codex_marketplace=True)
        tools = self._tools()
        self.assertEqual(tools[("skills", "plugins/marionette/skills/marionette/SKILL.md")],
                         "claude|codex|cursor")
        self.assertEqual(tools[("mcp", "plugins/marionette/mcp.json")], "codex|cursor")
        self.assertEqual(tools[("plugins", "plugins/marionette/plugin.json")], "any")
        self.assertEqual(tools[("plugins", "plugins/marionette/.claude-plugin/plugin.json")],
                         "claude")

    def test_without_a_codex_marketplace_codex_reads_no_portable_part(self):
        self._marionette(codex_marketplace=False)
        tools = self._tools()
        self.assertEqual(tools[("skills", "plugins/marionette/skills/marionette/SKILL.md")],
                         "claude|cursor")
        self.assertEqual(tools[("mcp", "plugins/marionette/mcp.json")], "cursor")

    def test_a_codex_manifest_beside_the_portable_one_makes_codex_a_reader(self):
        """saadeghi/daisyui keeps a portable manifest, `.codex-plugin/plugin.json` and
        `.claude-plugin/plugin.json` at its root (pp-0259). Codex reads only a SKILL.md that its
        parser accepts, one with a `description` (`_codex_skill_loads`)."""
        self._file("plugin.json", self.PORTABLE)
        self._file(".codex-plugin/plugin.json", '{"name": "daisyui"}')
        self._file(".claude-plugin/plugin.json", '{"name": "daisyui"}')
        self._file("skills/daisyui/SKILL.md", self.SKILL.format("daisyui"))
        self._file("skills/draft/SKILL.md", "---\nname: draft\n---\n")
        tools = self._tools()
        self.assertEqual(tools[("skills", "skills/daisyui/SKILL.md")], "claude|codex")
        self.assertEqual(tools[("skills", "skills/draft/SKILL.md")], "claude")

    def _azure(self, copilot_marketplace: bool) -> None:
        """Azure/azure-sdk-tools's `plugins/azure-sdk-tools`, a Claude Code plugin that only its
        Copilot marketplace lists, through `metadata.pluginRoot`."""
        self._file("plugins/azure-sdk-tools/.claude-plugin/plugin.json", json.dumps({
            "name": "azure-sdk-tools", "mcpServers": "./.mcp.json", "skills": "./skills/"}))
        self._file("plugins/azure-sdk-tools/.mcp.json", self.SERVERS)
        self._file("plugins/azure-sdk-tools/skills/sdk-release/SKILL.md",
                   self.SKILL.format("sdk-release"))
        if copilot_marketplace:
            self._file(".github/plugin/marketplace.json", json.dumps({
                "name": "azure-sdk-tools", "owner": {"name": "Azure SDK"},
                "metadata": {"pluginRoot": "./plugins"},
                "plugins": [{"name": "azure-sdk-tools", "source": "./azure-sdk-tools",
                             "strict": False}]}))

    def test_a_copilot_marketplace_makes_copilot_a_reader_of_a_claude_code_plugin(self):
        """pp-0096: Copilot CLI reads the manifest as the last of its legacy manifests, "Legacy
        plugins: .plugin/plugin.json, plugin.json, .github/plugin/plugin.json, or
        .claude-plugin/plugin.json (checked in this order)"."""
        self._azure(copilot_marketplace=True)
        tools = self._tools()
        self.assertEqual(tools[("plugins", "plugins/azure-sdk-tools/.claude-plugin/plugin.json")],
                         "claude|copilot")
        self.assertEqual(tools[("skills", "plugins/azure-sdk-tools/skills/sdk-release/SKILL.md")],
                         "claude|copilot")

    def test_without_a_copilot_marketplace_the_claude_code_plugin_is_claude_s(self):
        self._azure(copilot_marketplace=False)
        tools = self._tools()
        self.assertEqual(tools[("plugins", "plugins/azure-sdk-tools/.claude-plugin/plugin.json")],
                         "claude")
        self.assertEqual(tools[("skills", "plugins/azure-sdk-tools/skills/sdk-release/SKILL.md")],
                         "claude")

    def test_a_copilot_marketplace_below_the_root_is_copilot_s(self):
        """dotnet/dotnet keeps a Copilot CLI marketplace in `src/msbuild/.github/plugin/`, where
        the directory names no tool, because Copilot reads `.github` at the root."""
        self._file("src/msbuild/.github/plugin/marketplace.json", json.dumps({
            "name": "msbuild-plugins", "owner": {"name": "dotnet/msbuild"},
            "plugins": [{"name": "mt-migration", "source": "./plugins/mt-migration"}]}))
        self._file("src/msbuild/plugins/mt-migration/.claude-plugin/plugin.json",
                   '{"name": "mt-migration"}')
        self._file("src/msbuild/plugins/mt-migration/skills/mt/SKILL.md", self.SKILL.format("mt"))
        tools = self._tools()
        self.assertEqual(tools[("plugins", "src/msbuild/.github/plugin/marketplace.json")],
                         "copilot")
        self.assertEqual(tools[("skills", "src/msbuild/plugins/mt-migration/skills/mt/SKILL.md")],
                         "claude|copilot")

    def test_codex_reads_the_claude_code_manifest_of_a_folder_that_its_marketplace_lists(self):
        """obsei/obsei's Codex marketplace lists `integrations/claude/obsei`, with the manifests
        of Claude Code and Cursor. Codex takes `.claude-plugin/plugin.json`, the first of them in
        its order."""
        self._file(".agents/plugins/marketplace.json", json.dumps({
            "name": "obsei", "plugins": [{"name": "obsei", "source": {
                "source": "local", "path": "./integrations/claude/obsei"}}]}))
        self._file("integrations/claude/obsei/.claude-plugin/plugin.json", '{"name": "obsei"}')
        self._file("integrations/claude/obsei/.cursor-plugin/plugin.json", '{"name": "obsei"}')
        self._file("integrations/claude/obsei/skills/voice/SKILL.md", self.SKILL.format("voice"))
        tools = self._tools()
        self.assertEqual(tools[("plugins", "integrations/claude/obsei/.claude-plugin/plugin.json")],
                         "claude|codex")
        self.assertEqual(tools[("plugins", "integrations/claude/obsei/.cursor-plugin/plugin.json")],
                         "cursor")
        self.assertEqual(tools[("skills", "integrations/claude/obsei/skills/voice/SKILL.md")],
                         "claude|codex|cursor")


def _row_tools(repo, mechanism, detected):
    """{path: tools} for the rows of `mechanism` at the `detected` paths, as the detection step
    attributes them."""
    tools = _mod.artifact_tools(repo, [(mechanism, rel) for rel in detected],
                                plugin_parts=_mod.plugin_part_tools(repo))
    return {rel: found for (_mechanism, rel), found in tools.items()}


class TestAHooksFileThatOneToolOfAPluginFolderRefuses(_RepoCase):
    """razorpay/blade keeps a Claude Code and a Codex plugin in `packages/blade-plugin`, and
    both manifests name `./hooks/hooks.json`. The file opens with `_comment`, which Claude Code
    strips and Codex refuses: `HooksFile` has `#[serde(deny_unknown_fields)]`, and the plugin
    loader reports "failed to parse plugin hooks config" (codex-rs at be765f1a)."""

    MANIFEST = ('{"name": "blade", "version": "1.33.1", "license": "MIT", '
                '"hooks": "./hooks/hooks.json"}')
    HOOK = ('"hooks": {"SessionStart": [{"hooks": [{"type": "command", '
            '"command": "node \\"${CLAUDE_PLUGIN_ROOT}/hooks/session-start.mjs\\"", "timeout": 5}]}]}')

    def _plugin(self, hooks):
        self._file("packages/blade-plugin/.claude-plugin/plugin.json", self.MANIFEST)
        self._file("packages/blade-plugin/.codex-plugin/plugin.json", self.MANIFEST)
        self._file("packages/blade-plugin/skills/blade/SKILL.md",
                   "---\nname: blade\ndescription: Blade components.\n---\nUse Blade.\n")
        self._file("packages/blade-plugin/hooks/hooks.json", hooks)

    def test_the_hooks_file_is_claude_code_s_alone(self):
        self._plugin('{\n  "_comment": "Plain Node scripts with no dependencies.",\n  '
                     + self.HOOK + '\n}\n')
        hooks = detect_hooks(self.repo)
        self.assertEqual(hooks, ["packages/blade-plugin/hooks/hooks.json"])
        self.assertEqual(_row_tools(self.repo, "hooks", hooks),
                         {"packages/blade-plugin/hooks/hooks.json": {"claude"}})

    def test_a_hooks_file_that_both_tools_load_is_both_tools(self):
        self._plugin('{' + self.HOOK + '}')
        self.assertEqual(_row_tools(self.repo, "hooks", detect_hooks(self.repo)),
                         {"packages/blade-plugin/hooks/hooks.json": {"claude", "codex"}})


class TestTheInlineServersOfACursorPlugin(_RepoCase):
    """Cursor's manifest reference: "Path to MCP config file, inline MCP server config, or an array
    of either." (cursor.com/docs/reference/plugins, read 2026-11-06). The manifest, or the
    marketplace file with the entry, is then a row of the mechanism, as a Claude Code manifest with
    inline servers is. shadcn-ui/ui's `.cursor-plugin/plugin.json` has the first form."""

    SHADCN = {"shadcn": {"command": "npx", "args": ["shadcn@latest", "mcp"]}}

    def test_inline_servers_of_a_manifest_are_an_mcp_row_of_cursor(self):
        self._file(".cursor-plugin/plugin.json", json.dumps({"name": "shadcn", "mcpServers": self.SHADCN}))
        mcp = detect_mcp(self.repo)
        self.assertEqual(mcp, [".cursor-plugin/plugin.json"])
        self.assertEqual(_row_tools(self.repo, "mcp", mcp), {".cursor-plugin/plugin.json": {"cursor"}})

    def test_a_list_of_a_path_and_inline_servers_is_read_as_the_cli_reads_it(self):
        """Cursor CLI 2026.09.28-64d2043 reads an array entry by entry: a string is a file, and an
        object holds servers."""
        self._file("plugins/p/.cursor-plugin/plugin.json",
                   json.dumps({"name": "p", "mcpServers": ["./mcp.json", self.SHADCN]}))
        self.assertIn("plugins/p/.cursor-plugin/plugin.json", detect_mcp(self.repo))

    def test_inline_servers_of_a_marketplace_entry_are_an_mcp_row_of_the_marketplace_file(self):
        self._file(".cursor-plugin/marketplace.json", json.dumps({
            "name": "m", "plugins": [{"name": "p", "source": "p", "mcpServers": self.SHADCN}]}))
        self._file("p/rules/style.mdc", "x\n")
        mcp = detect_mcp(self.repo)
        self.assertEqual(mcp, [".cursor-plugin/marketplace.json"])
        self.assertEqual(_row_tools(self.repo, "mcp", mcp),
                         {".cursor-plugin/marketplace.json": {"cursor"}})

    def test_the_manifest_of_a_listed_plugin_goes_first(self):
        """"the per-plugin manifest is merged with the marketplace entry (manifest values take
        precedence)". The servers of the entry do not apply beside servers of the manifest."""
        self._file(".cursor-plugin/marketplace.json", json.dumps({
            "name": "m", "plugins": [{"name": "p", "source": "p", "mcpServers": self.SHADCN}]}))
        self._file("p/.cursor-plugin/plugin.json", json.dumps({"name": "p", "mcpServers": self.SHADCN}))
        self.assertEqual(detect_mcp(self.repo), ["p/.cursor-plugin/plugin.json"])

    def test_a_manifest_without_a_server_is_no_mcp_row(self):
        for value in ({}, {"x": {"args": ["a"]}}, "./mcp.json", []):
            with self.subTest(value=value):
                self._file(".cursor-plugin/plugin.json", json.dumps({"name": "p", "mcpServers": value}))
                self.assertEqual(detect_mcp(self.repo), [])

    def test_a_cursor_manifest_with_hooks_inline_is_no_hooks_row(self):
        """Cursor's hooks are flat entries, which `has_a_grouped_hook` does not read."""
        self._file(".cursor-plugin/plugin.json", json.dumps({"name": "p", "hooks": {
            "PreToolUse": [{"matcher": "x", "hooks": [{"type": "command", "command": "y"}]}]}}))
        self.assertEqual(detect_hooks(self.repo), [])


class TestTheInlineServersAndHooksOfAClaudeCodePlugin(_RepoCase):
    """A Claude Code manifest's `mcpServers`, `lspServers` and `hooks` take "Path, object, or
    array of either", and an inline map is used "as server configs keyed by name"
    (code.claude.com/docs/en/plugins/manifest-reference, read 2026-11-05). The manifest is then
    a row of the mechanism, as a Codex manifest with inline servers is."""

    TERRAVISION = {"terravision": {"command": "uvx", "args": [
        "--from", "terravision[mcp]==0.56.0", "terravision", "mcp", "--output-dir", "./diagrams"]}}

    def _manifest(self, rel, **fields):
        self._file(rel, json.dumps({"name": "p", "version": "1.0.0", **fields}))

    def test_identical_inline_servers_of_a_claude_code_and_a_codex_manifest_are_two_rows(self):
        """patrickchugh/terravision gives both manifests at its root the same server."""
        self._manifest(".claude-plugin/plugin.json", skills=["./skills/terravision-cloud-diagrams"],
                       mcpServers=self.TERRAVISION)
        self._manifest(".codex-plugin/plugin.json", skills=["./skills/terravision-cloud-diagrams"],
                       interface={"websiteURL": "https://github.com/patrickchugh/terravision"},
                       mcpServers=self.TERRAVISION)
        self._file("skills/terravision-cloud-diagrams/SKILL.md",
                   "---\nname: terravision-cloud-diagrams\ndescription: Draw diagrams.\n---\nDraw.\n")
        mcp = detect_mcp(self.repo)
        self.assertEqual(sorted(mcp), [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"])
        self.assertEqual(_row_tools(self.repo, "mcp", mcp),
                         {".claude-plugin/plugin.json": {"claude"},
                          ".codex-plugin/plugin.json": {"codex"}})

    def test_inline_language_servers_are_an_lsp_row(self):
        """DataDog/datadog-agent's gopls plugin configures its server in the manifest alone, with
        the two fields that the reference requires, `command` and `extensionToLanguage`."""
        rel = ".claude-plugins/datadog-agent-gopls/.claude-plugin/plugin.json"
        self._manifest(rel, lspServers={"gopls": {
            "command": "gopls", "extensionToLanguage": {".go": "go"},
            "initializationOptions": {"formatting.local": "github.com/DataDog/datadog-agent"}}})
        self._file(".claude-plugins/datadog-agent-gopls/README.md", "# gopls\n")
        lsp = detect_lsp(self.repo)
        self.assertEqual(lsp, [rel])
        self.assertEqual(_row_tools(self.repo, "lsp", lsp), {rel: {"claude"}})

    def test_inline_hooks_are_a_hooks_row_beside_a_hooks_file_that_the_manifest_names(self):
        """The reference's example: "This array loads one hooks file and declares one inline
        `PostToolUse` hook". "An inline object is the event map itself, with no wrapper"."""
        self._manifest("p/.claude-plugin/plugin.json", hooks=[
            "./config/extra-hooks.json",
            {"PostToolUse": [{"matcher": "Write|Edit", "hooks": [
                {"type": "command", "command": "\"${CLAUDE_PLUGIN_ROOT}\"/scripts/format.sh"}]}]}])
        self._file("p/config/extra-hooks.json",
                   '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", '
                   '"command": "\\"${CLAUDE_PLUGIN_ROOT}\\"/scripts/check-command.sh"}]}]}}')
        hooks = detect_hooks(self.repo)
        self.assertEqual(sorted(hooks), ["p/.claude-plugin/plugin.json", "p/config/extra-hooks.json"])
        self.assertEqual(_row_tools(self.repo, "hooks", hooks),
                         {"p/.claude-plugin/plugin.json": {"claude"},
                          "p/config/extra-hooks.json": {"claude"}})

    def test_a_value_that_configures_no_server_or_hook_is_no_row(self):
        """An empty map, a server without a `command` or a `url`, a language server without its
        `extensionToLanguage`, an event without a matcher group and a hooks object wrapped in a
        `hooks` key, which Claude Code reads as an event named "hooks", configure nothing."""
        cases = {"mcpServers": [{}, {"x": {"args": ["serve"]}}, "./servers.json"],
                 "lspServers": [{"go": {"command": "gopls"}},
                                {"go": {"command": "gopls", "extensionToLanguage": {}}}],
                 "hooks": [{"Stop": []},
                           {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "true"}]}]}}]}
        detectors = {"mcpServers": detect_mcp, "lspServers": detect_lsp, "hooks": detect_hooks}
        for key, values in cases.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    shutil.rmtree(self.repo)
                    self._manifest("p/.claude-plugin/plugin.json", **{key: value})
                    self._file("p/servers.json", "{}")
                    _mod._plugin_components.cache_clear()
                    _mod._nested_anchors.cache_clear()
                    self.assertEqual(detectors[key](self.repo), [])

    def test_inline_servers_count_only_where_claude_code_accepts_every_server(self):
        """Claude Code parses an inline map of MCP servers as a whole and refuses the manifest
        when one server fails its schema. `claude plugin validate` of Claude Code 2.1.319 reports
        "mcpServers: Invalid input" for a server with a `url` and no `type`, alone, beside a
        server with a `command` or in a second map of a list, and for the boolean in the `env`
        of e2e-test-quest/uuv's server. It accepts an `sdk` server with its `name` beside a
        stdio server, and drops the `sdk` server when it loads the plugin."""
        uuv = {"uuv-mcp-server": {"type": "stdio", "command": "npx",
                                  "args": ["-y", "@uuv/mcp-server@latest"],
                                  "env": {"UUV_LLM_MODEL": "anthropic/claude-haiku-4-5",
                                          "UUV_JSON_FLAT_MODEL_ENABLED": True}}}
        local = {"local": {"command": "npx", "args": ["-y", "server"]}}
        remote = {"remote": {"url": "https://example.invalid/mcp"}}
        http = {"remote": {"type": "http", "url": "https://example.invalid/mcp"}}
        cases = [(remote, False), ({**local, **remote}, False), ([local, remote], False),
                 (uuv, False), (http, True),
                 ({**local, "host": {"type": "sdk", "name": "host"}}, True),
                 ({**local, "host": {"type": "sdk"}}, False),
                 ({"host": {"type": "sdk", "name": "host"}}, False), (local, True)]
        for value, counts in cases:
            with self.subTest(value=value):
                shutil.rmtree(self.repo)
                self._manifest("p/.claude-plugin/plugin.json", mcpServers=value)
                _mod._plugin_components.cache_clear()
                _mod._nested_anchors.cache_clear()
                self.assertEqual(detect_mcp(self.repo),
                                 ["p/.claude-plugin/plugin.json"] if counts else [])

    def test_an_entry_without_a_manifest_with_a_server_that_claude_code_refuses_is_no_row(self):
        """Claude Code 2.1.319 parses a marketplace entry with the schema of the manifest and
        stubs an entry that fails it as an unsupported source, "Stubbing unparseable marketplace
        plugin entry", so that the plugin cannot be installed."""
        self._file(".claude-plugin/marketplace.json", json.dumps({
            "name": "remote", "owner": {"name": "Remote"},
            "plugins": [{"name": "remote", "source": "./", "mcpServers": {
                "remote": {"url": "https://example.invalid/mcp"}}}]}))
        self._file("skills/remote/SKILL.md",
                   "---\nname: remote\ndescription: Remote.\n---\nUse it.\n")
        self.assertEqual(detect_mcp(self.repo), [])

    def test_the_inline_servers_of_an_entry_without_a_manifest_are_the_marketplace_s_row(self):
        """firebase/firebase-tools has no plugin.json, and its marketplace entry is the plugin's
        manifest: "No `plugin.json`: the entry is the manifest regardless of `strict`. Every
        manifest field that the entry accepts applies, including `mcpServers`" (marketplace
        reference, read 2026-11-05). The row is the marketplace file, which holds the
        servers."""
        self._file(".claude-plugin/marketplace.json", json.dumps({
            "name": "firebase", "owner": {"name": "Firebase"},
            "plugins": [{"name": "firebase", "source": "./", "mcpServers": {"firebase": {
                "command": "npx", "args": ["-y", "firebase-tools", "mcp", "--dir", "."]}}}]}))
        self._file("skills/firebase-basics/SKILL.md",
                   "---\nname: firebase-basics\ndescription: Firebase.\n---\nUse Firebase.\n")
        mcp = detect_mcp(self.repo)
        self.assertEqual(mcp, [".claude-plugin/marketplace.json"])
        self.assertEqual(_row_tools(self.repo, "mcp", mcp),
                         {".claude-plugin/marketplace.json": {"claude"}})

    def test_the_inline_servers_of_an_entry_beside_a_manifest_do_not_apply(self):
        """genomoncology/biomcp's entry configures a server and its plugin.json none: "Entry
        `mcpServers`, `lspServers`, `userConfig`, and `channels` don't apply. Declare them in
        `plugin.json`" (marketplace reference, read 2026-11-05)."""
        self._file(".claude-plugin/marketplace.json", json.dumps({
            "name": "biomcp", "owner": {"name": "GenomOncology"},
            "plugins": [{"name": "biomcp", "source": "./",
                         "mcpServers": {"biomcp": {"command": "biomcp", "args": ["serve"]}}}]}))
        self._manifest(".claude-plugin/plugin.json")
        self._file("skills/biomcp/SKILL.md", "---\nname: biomcp\ndescription: BioMCP.\n---\nUse it.\n")
        self.assertEqual(detect_mcp(self.repo), [])


class TestAdditionalProjectConfiguration(_RepoCase):
    def test_project_settings_survive_collection_and_validation(self):
        from agentcontext import paths
        from agentcontext.steps import collected_file_cleanup
        expected = {'.github/github-app.yml', '.github/copilot-desktop.yml', '.gemini/config.json'}
        for path in expected:
            self._file(path, '{"enabled":true}')
        self._file('CLAUDE.md', 'Instructions')
        self._file('.worktreeinclude', 'local.env')
        with tempfile.TemporaryDirectory() as temporary:
            copied = Path(temporary)
            paths.collect_repo(self.repo, copied, set())
            collected_file_cleanup.scan_and_clean_repo(str(copied), [])
            self.assertEqual(set(detect_settings(copied)), expected)
            self.assertIn('.worktreeinclude', _collect_all_spec_files(copied))


class TestFilesThatAFileManagerWroteAreNoConfiguration(_RepoCase):
    """Synology DSM writes `@eaDir/<name>@SynoEAStream` beside the files of a directory that
    File Station visits on the NAS, and a thumbnail directory beside an image. Finder writes
    `.DS_Store`. The collected file cleanup removes these files from the validated tree, but a
    visit after the cleanup can put them back before a resumed detection. The detectors skip
    them, because other.csv and unmatched-paths.csv would otherwise list them."""

    def test_no_detector_lists_them(self):
        self._file(".claude/settings.json", '{"model": "opus"}\n')
        self._file(".claude/@eaDir/settings.json@SynoEAStream", "\0")
        self._file(".claude/skills/x/SKILL.md", "---\nname: x\ndescription: Does x\n---\nDo x.\n")
        self._file(".claude/skills/@eaDir/x@SynoEAStream", "\0")
        self._file(".claude/skills/x/@eaDir/SKILL.md@SynoEAStream", "\0")
        self._file(".cursor/rules/style.mdc", "---\ndescription: Tabs\n---\nUse tabs.\n")
        self._file(".cursor/rules/@eaDir/logo.png/SYNOFILE_THUMB_M.jpg", "\0")
        self._file(".claude/.DS_Store", "\0")
        self.assertEqual(_other_as_main_does(self.repo), [])
        found = [*detect_settings(self.repo), *detect_context_files(self.repo),
                 *(row["skills.md"] for row in detect_skills(self.repo)),
                 *(row["subagent"] for row in detect_subagents(self.repo)),
                 *(row["command"] for row in detect_commands(self.repo)),
                 *detect_hooks(self.repo), *(row["rule"] for row in detect_rules(self.repo)),
                 *detect_mcp(self.repo), *detect_plugins(self.repo), *detect_lsp(self.repo)]
        self.assertEqual(sorted(found), [".claude/settings.json", ".claude/skills/x/SKILL.md",
                                         ".cursor/rules/style.mdc"])


if __name__ == "__main__":
    unittest.main()


class TestOpenCode(_RepoCase):
    """OpenCode's configuration as OpenCode 1.19.3 reads it: its configuration files at the root,
    its `.opencode` directory at any depth, the MCP and LSP servers inside the configuration, and
    the plugin and tool modules directly in their folders."""

    SERVERS = '{\n  // the docs server\n  "mcp": {"docs": {"type": "remote", "url": "https://x.test"},},\n}\n'

    def test_the_configuration_counts_at_the_repository_root_only(self):
        """Every opencode.json below the root of the October 5-6 file lists is a translation, an
        example, a fixture or a template, such as FlyEnv's src/lang/en/opencode.json."""
        for rel in ("opencode.json", "tui.json", "packages/web/opencode.json",
                    "src/lang/en/opencode.json", "metadata/tui.json"):
            self._file(rel, "{}\n")
        settings = detect_settings(self.repo)
        self.assertIn("opencode.json", settings)
        self.assertIn("tui.json", settings)
        self.assertFalse([p for p in settings if "/" in p and p.endswith(("opencode.json", "tui.json"))])

    def test_the_mcp_servers_count_when_mcp_names_one(self):
        self._file("opencode.jsonc", self.SERVERS)
        self._file(".opencode/opencode.json", '{"mcp": {}}\n')
        self._file("packages/web/.opencode/opencode.jsonc", self.SERVERS)
        self.assertEqual(sorted(p for p in detect_mcp(self.repo) if "opencode" in p),
                         ["opencode.jsonc", "packages/web/.opencode/opencode.jsonc"])

    def test_the_lsp_servers_count_when_lsp_is_true_or_an_object(self):
        """`true` and an object enable the built-in servers, `false` disables them, and a
        configuration without `lsp` has none."""
        for value, counts in (("true", True), ("{}", True),
                              ('{"rust": {"command": ["rust-analyzer"]}}', True),
                              ("false", False), (None, False)):
            with self.subTest(value=value):
                self._file("opencode.json", "{}\n" if value is None else f'{{"lsp": {value}}}\n')
                self.assertEqual(detect_lsp(self.repo), ["opencode.json"] if counts else [])

    def test_the_plugins_and_tools_are_the_modules_directly_in_their_folders(self):
        """OpenCode's patterns, "{plugin,plugins}/*.{ts,js}" and "{tool,tools}/*.{js,ts}", read
        neither a subfolder, nor another extension, nor another case."""
        plugin = "export const Notify = async () => ({})\n"
        tool = ('export default tool({ description: "Query", args: {}, '
                'async execute() { return "" } })\n')
        for rel, text in ((".opencode/plugins/notify.ts", plugin), (".opencode/plugins/lib/helper.ts", plugin),
                          (".opencode/plugins/README.md", plugin), (".opencode/plugins/Upper.TS", plugin),
                          (".opencode/tool/database.js", tool), (".opencode/tools/add.py", tool)):
            self._file(rel, text)
        self.assertEqual([p for p in detect_plugins(self.repo) if p.startswith(".opencode/")],
                         [".opencode/plugins/notify.ts", ".opencode/tool/database.js"])

    def test_a_module_that_defines_no_tool_or_plugin_is_none(self):
        """OpenCode takes each export of a plugin module as a plugin, and of a tool module only an
        export with args, description and execute. event-driven-io/Pongo keeps a vitest file
        without an export in .opencode/plugins, and cloudflare/workerd a helper that exports
        functions in .opencode/tools."""
        self._file(".opencode/plugins/validate-change.unit.spec.ts",
                   'import { describe, it } from "vitest"\ndescribe("validate", () => { it("runs", () => {}) })\n')
        self._file(".opencode/tools/capnp.ts",
                   "export function resolveCapnpPath(root: string) { return root }\n")
        self._file(".opencode/tools/schema.ts",
                   'export default tool({ description: "Compile", args: {}, async execute() { return "" } })\n')
        self.assertEqual([p for p in detect_plugins(self.repo) if p.startswith(".opencode/")],
                         [".opencode/tools/schema.ts"])

    def test_a_theme_is_a_settings_file(self):
        self._file(".opencode/themes/night.json", "{}\n")
        self._file(".opencode/themes/drafts/day.json", "{}\n")
        settings = detect_settings(self.repo)
        self.assertIn(".opencode/themes/night.json", settings)
        self.assertNotIn(".opencode/themes/drafts/day.json", settings)

    def test_a_file_that_the_instructions_list_is_an_opencode_context_file(self):
        """mandarons/icloud-docker lists `docs/index.md` and `docs/standards/*.md`. A file that
        only the instructions name is OpenCode's, and an AGENTS.md that they name as well stays
        the shared context file that every tool reads."""
        self._file("opencode.json",
                   '{"instructions": ["docs/index.md", "docs/standards/*.md", "AGENTS.md"]}\n')
        for rel in ("docs/index.md", "docs/standards/naming.md", "docs/glossary.md", "AGENTS.md"):
            self._file(rel, "# Guide\n")
        files = detect_context_files(self.repo)
        self.assertEqual(sorted(p for p in files if p.startswith("docs/")),
                         ["docs/index.md", "docs/standards/naming.md"])
        rows = [("context_file", p) for p in files]
        tools = _mod.artifact_tools(self.repo, rows, (), (), _mod.declared_file_tools(self.repo))
        self.assertEqual({key[1]: heuristics.tool_value(value) for key, value in tools.items()},
                         {"docs/index.md": "opencode", "docs/standards/naming.md": "opencode",
                          "AGENTS.md": "any"})

    def test_every_markdown_file_of_the_modes_folder_is_a_mode(self):
        """OpenCode reads "{mode,modes}/*.md", a README.md among them, and "{agent,agents}/**/*.md"
        at any depth."""
        for rel in (".opencode/modes/README.md", ".opencode/modes/docs.md",
                    ".opencode/agents/review/security.md"):
            self._file(rel)
        self.assertEqual(sorted(r["subagent"] for r in detect_subagents(self.repo)),
                         [".opencode/agents/review/security.md", ".opencode/modes/README.md",
                          ".opencode/modes/docs.md"])

    def test_a_markdown_agent_named_after_a_built_in_agent_defines_none(self):
        """OpenCode loads `.opencode/agents/plan.md` into its agents as `plan` and merges it into
        the built-in agent `plan` (`load` in packages/opencode/src/config/agent.ts and the
        agents in packages/opencode/src/agent/agent.ts of anomalyco/opencode at e3c4eb242585), as
        it merges an entry `plan` of its configuration. debanjandhar12/logseq-astro-box's
        `.opencode/agents/plan.md` and equinor/design-system's `.opencode/agent/build.md` adjust a
        built-in agent and are `other` rows of `.opencode` (pp-0797 of the evaluation of
        2026-11-04). The name is the path below the folder, and a `name` in the front matter
        takes its place."""
        plan = "---\ndescription: Produces plans\nmode: primary\n---\nPlan.\n"
        cases = ((".opencode/agents/plan.md", plan, False),
                 (".opencode/agent/build.md", plan, False),
                 (".opencode/modes/plan.md", plan, False),
                 (".opencode/mode/general.md", plan, False),
                 ("packages/web/.opencode/agents/explore.md", plan, False),
                 (".opencode/agents/review.md", "---\nname: plan\n---\nPlan.\n", False),
                 (".opencode/agents/review/plan.md", plan, True),
                 (".opencode/agents/plan.md", "---\nname: planner\n---\nPlan.\n", True),
                 (".opencode/agents/Plan.md", plan, True))
        for i, (rel, text, defines) in enumerate(cases):
            with self.subTest(path=rel, text=text):
                repo = self.tmp / f"o{i}"
                (repo / rel).parent.mkdir(parents=True)
                (repo / rel).write_text(text, encoding="utf-8")
                self.assertEqual([r["subagent"] for r in detect_subagents(repo)],
                                 [rel] if defines else [])
                if rel.startswith(".opencode/"):
                    self.assertEqual(rel in _other_as_main_does(repo), not defines)

    def test_every_path_is_opencode_s(self):
        for path in ("opencode.json", "tui.jsonc", ".opencode/agents/review.md",
                     ".opencode/tools/database.ts", "packages/web/.opencode/commands/test.md",
                     ".opencode/themes/night.json"):
            with self.subTest(path=path):
                self.assertEqual(_mod.tool_for_path(path), "opencode")


class TestOpenCodeConfigurationThatDefinesCommands(_RepoCase):
    """OpenCode's page: "Define commands under commands in any OpenCode JSON or JSONC
    configuration file.", and its migration page: "Rename the singular command map to commands"
    and "The V1 config format remains supported."."""

    def test_a_configuration_with_a_command_is_a_command(self):
        """One row per file, named after the file, also where both keys define a command."""
        cases = {"opencode.json": '{"commands": {"test": {"template": "Run the tests."}}}',
                 "opencode.jsonc": '{\n  // commands\n  "command": {"test": {"template": "T"},},\n}',
                 ".opencode/opencode.json": '{"command": {"a": {}}, "commands": {"b": {}}}',
                 "packages/web/.opencode/opencode.jsonc": '{"commands": {"test": {}}}'}
        for path, text in cases.items():
            self._file(path, text)
        self.assertEqual(sorted(r["command"] for r in detect_commands(self.repo)), sorted(cases))
        for path in cases:
            with self.subTest(path=path):
                self.assertIn({"command": path, "name": Path(path).stem},
                              detect_commands(self.repo))
                self.assertEqual(_mod.artifact_tools(self.repo, [("commands", path)]),
                                 {("commands", path): {"opencode"}})

    def test_a_configuration_without_a_command_defines_none(self):
        self._file("opencode.json", '{"commands": {}}')
        self._file("opencode.jsonc", '{"command": "test"}')
        self._file(".opencode/opencode.json", '{"mcp": {"docs": {}}}')
        self.assertEqual(detect_commands(self.repo), [])

    def test_a_file_of_that_name_below_the_root_is_no_command(self):
        """The configuration counts at the repository root, as its other rows read it."""
        self._file("src/lang/en/opencode.json", '{"commands": {"test": {}}}')
        self.assertEqual(detect_commands(self.repo), [])


class TestOpenCodeFlatSkills(_RepoCase):
    """OpenCode 2's other form of a skill: "Markdown files must be at the source root, such as
    skills/review.md." and "Each source can contain either form", which its `scan` of
    skill-scan.ts reads with "if (item.depth === 0)" and "if (skill) {"."""

    SKILL = "---\nname: release\ndescription: Publish a release.\n---\nPublish.\n"

    def _skills(self) -> list:
        return [r["skills.md"] for r in detect_skills(self.repo)]

    def test_a_described_file_directly_in_the_folder_is_a_skill_named_after_the_file(self):
        for rel in (".opencode/skills/git-release.md", ".opencode/skill/review.md",
                    "packages/web/.opencode/skills/deploy.md"):
            self._file(rel, self.SKILL)
        rows = detect_skills(self.repo)
        self.assertEqual(sorted(r["skills.md"] for r in rows),
                         [".opencode/skill/review.md", ".opencode/skills/git-release.md",
                          "packages/web/.opencode/skills/deploy.md"])
        self.assertIn({"skills.md": ".opencode/skills/git-release.md", "name": "git-release",
                       "scripts": "FALSE", "references": "FALSE", "assets": "FALSE"}, rows)
        artifact = ("skills", ".opencode/skills/git-release.md")
        self.assertEqual(_mod.artifact_tools(self.repo, [artifact]), {artifact: {"opencode"}})

    def test_a_folder_can_hold_both_forms(self):
        self._file(".opencode/skills/git-release.md", self.SKILL)
        self._file(".opencode/skills/review/SKILL.md", self.SKILL)
        self.assertEqual(sorted(self._skills()), [".opencode/skills/git-release.md",
                                                  ".opencode/skills/review/SKILL.md"])

    def test_a_file_without_a_description_is_no_skill_and_stays_an_other_row(self):
        """"Add a clear description when the model should discover the skill; skills without one
        are not advertised." A README.md or a list of credits in the folder stays out."""
        self._file(".opencode/skills/README.md", "# Our skills\n")
        self._file(".opencode/skills/credits.md", "---\nname: credits\n---\nThanks.\n")
        self.assertEqual(self._skills(), [])
        self.assertEqual(sorted(p for p in _other_as_main_does(self.repo) if "/skills/" in p),
                         [".opencode/skills/README.md", ".opencode/skills/credits.md"])

    def test_a_counted_file_is_no_other_row_and_owns_no_other_file(self):
        self._file(".opencode/skills/git-release.md", self.SKILL)
        self._file(".opencode/skills/README.md", "# Our skills\n")
        self.assertEqual([p for p in _other_as_main_does(self.repo) if "/skills/" in p],
                         [".opencode/skills/README.md"])

    def test_a_file_below_the_folder_is_no_flat_skill(self):
        """The scan keeps the Markdown files of the top level only, and a folder below it counts
        by its SKILL.md."""
        self._file(".opencode/skills/team/notes.md", self.SKILL)
        self.assertEqual(self._skills(), [])

    def test_a_folder_with_a_skill_marker_of_its_own_keeps_no_flat_file(self):
        """The scan takes a SKILL.md directly in the folder and stops there."""
        self._file(".opencode/skills/SKILL.md", self.SKILL)
        self._file(".opencode/skills/git-release.md", self.SKILL)
        self.assertEqual(self._skills(), [])

    def test_a_hidden_file_is_no_skill(self):
        """The scan leaves out each entry with a name that starts with a dot."""
        self._file(".opencode/skills/.draft.md", self.SKILL)
        self.assertEqual(self._skills(), [])

    def test_the_skills_folder_of_another_tool_keeps_its_form(self):
        """docs/heuristics.md credits a tool that reads another tool's skills directory only with
        a marker named SKILL.md, so that OpenCode 2's flat file counts in its own folders alone."""
        self._file(".claude/skills/git-release.md", self.SKILL)
        self._file(".agents/skills/git-release.md", self.SKILL)
        self.assertEqual(self._skills(), [])

    def test_a_linked_file_is_read_where_the_link_leads(self):
        """The scan follows a link to a file, and the collector writes the link as a marker with
        its target. The row keeps the path of the link, as a rule behind a link does."""
        self._file("docs/git-release.md", self.SKILL)
        self._file(".opencode/skills/git-release.md", "../../docs/git-release.md\n")
        self.assertEqual(self._skills(), [".opencode/skills/git-release.md"])


class TestOpenCodePluginPackages(_RepoCase):
    """OpenCode 2's plugin packages as 2.1.1 loads them: each folder directly in
    `.opencode/plugins` with a `server`, `index` or `tui` entry module, and each folder directly
    in `.opencode/plugin` with a `server` or `index` entry module, because the terminal
    interface lists the folders of `plugins` alone (`Host.resolve`, `ConfigPluginSource.scan` and
    `localPluginDirectories`)."""

    def _plugins(self) -> list:
        _mod._nested_anchors.cache_clear()
        _mod._plugin_components.cache_clear()
        return [p for p in detect_plugins(self.repo) if ".opencode/" in p]

    def test_a_folder_with_an_entry_module_is_one_plugin(self):
        for entry in (".opencode/plugins/acme/server.ts", ".opencode/plugins/acme/index.js",
                      ".opencode/plugins/acme/tui.tsx", ".opencode/plugin/acme/server.mts",
                      ".opencode/plugin/acme/index.cjs",
                      "packages/web/.opencode/plugins/acme/index.ts"):
            with self.subTest(entry=entry):
                shutil.rmtree(self.repo)
                folder = entry.rsplit("/", 1)[0]
                self._file(entry)
                for rel in ("package.json", "README.md", "src/hooks.ts", "rpc.ts"):
                    self._file(f"{folder}/{rel}")
                self.assertEqual(self._plugins(), [entry])

    def test_the_row_is_the_first_entry_module_that_opencode_resolves(self):
        """OpenCode tries `server` before `index` before `tui`, and the extensions in Bun's
        order, `.ts` before `.js`."""
        for rel in ("server.js", "index.ts", "tui.tsx"):
            self._file(f".opencode/plugins/both/{rel}")
        for rel in ("index.js", "index.ts"):
            self._file(f".opencode/plugins/scripts/{rel}")
        self.assertEqual(self._plugins(), [".opencode/plugins/both/server.js",
                                           ".opencode/plugins/scripts/index.ts"])

    def test_a_folder_without_an_entry_module_is_no_plugin(self):
        """These folders have no entry module directly in the folder, in the case and with an
        extension that OpenCode resolves. The folder in `.opencode/plugin` has a terminal
        interface entry alone, which OpenCode 2 does not load there."""
        for rel in (".opencode/plugins/contract/rpc.ts", ".opencode/plugins/nested/src/index.ts",
                    ".opencode/plugins/upper/Index.ts", ".opencode/plugins/data/index.json",
                    ".opencode/plugins/notes/README.md", ".opencode/plugin/panel/tui.tsx"):
            self._file(rel)
        self.assertEqual(self._plugins(), [])

    def test_the_files_of_a_package_belong_to_its_plugin(self):
        """The files of a package are parts of its plugin rather than `other` rows, and the
        files of a folder that OpenCode does not load stay `other` rows."""
        for rel in (".opencode/plugins/acme/index.ts", ".opencode/plugins/acme/package.json",
                    ".opencode/plugins/acme/src/hooks.ts", ".opencode/plugins/notes/README.md",
                    ".opencode/plugin/panel/tui.tsx"):
            self._file(rel)
        other = _other_as_main_does(self.repo)
        self.assertEqual(sorted(p for p in other if "/plugin" in p),
                         [".opencode/plugin/panel/tui.tsx", ".opencode/plugins/notes/README.md"])

    def test_a_linked_folder_is_read_where_the_link_leads(self):
        """The collector keeps a link that stays inside `.opencode` as a link. The row and the
        package's files are at the folder that the link leads to."""
        self._file(".opencode/packages/acme/index.ts")
        self._file(".opencode/packages/acme/src/hooks.ts")
        self._dir(".opencode/plugins")
        os.symlink("../packages/acme", self.repo / ".opencode/plugins/acme")
        self.assertEqual(self._plugins(), [".opencode/packages/acme/index.ts"])
        self.assertFalse([p for p in _other_as_main_does(self.repo) if "acme" in p])

    def test_a_package_is_opencode_s_by_its_heuristic(self):
        """The row's tool is the tool of the heuristic that reads the entry module, rather than
        the owner of the directory, which only a row that no heuristic explains takes."""
        entry = ".opencode/plugins/acme/tui.tsx"
        self._file(entry)
        self.assertEqual(_mod.tools_reading("plugins", entry, self.repo / entry, self.repo),
                         (True, {"opencode"}))


class TestAPluginPackageChangesOnlyItsOwnRows(_StepCase):
    """A plugin package adds its row to plugins.csv and takes its files out of other.csv, and
    every other output of the step is the output for the same tree without the entry module."""

    FILES = {
        "CLAUDE.md": "# Rules\n",
        ".opencode/opencode.json": '{"mcp": {"docs": {"type": "remote", "url": "https://x.test"}}}\n',
        ".opencode/plugins/notify.ts": "export default {}\n",
        ".opencode/plugins/acme/index.ts": "export default {}\n",
        ".opencode/plugins/acme/package.json": "{}\n",
        ".opencode/plugins/acme/src/hooks.ts": "export {}\n",
        ".opencode/plugins/notes/README.md": "# Notes\n",
    }

    def _outputs(self) -> dict:
        shutil.rmtree(self.output, ignore_errors=True)
        self.assertEqual(self._run(), 0, self.stdout)
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(self.output.iterdir())}

    @staticmethod
    def _artifacts(text: str, column: str) -> list:
        return [(row[column], row["tool"]) for row in csv.DictReader(io.StringIO(text))]

    def test_only_the_plugin_row_and_the_package_files_change(self):
        repo = self._repo("o/r", self.FILES)
        with_package = self._outputs()
        (repo / ".opencode/plugins/acme/index.ts").rename(repo / ".opencode/plugins/acme/rpc.ts")
        without = self._outputs()
        self.assertEqual(sorted(name for name in with_package
                                if with_package[name] != without.get(name)),
                         ["other.csv", "plugins.csv"])
        self.assertEqual(self._artifacts(with_package["plugins.csv"], "plugin"),
                         [(".opencode/plugins/acme/index.ts", "opencode"),
                          (".opencode/plugins/notify.ts", "opencode")])
        self.assertEqual(self._artifacts(without["plugins.csv"], "plugin"),
                         [(".opencode/plugins/notify.ts", "opencode")])
        self.assertEqual([path for path, _tool in self._artifacts(with_package["other.csv"], "other")],
                         [".opencode/plugins/notes/README.md"])
        self.assertEqual([path for path, _tool in self._artifacts(without["other.csv"], "other")],
                         [".opencode/plugins/acme/package.json", ".opencode/plugins/acme/rpc.ts",
                          ".opencode/plugins/acme/src/hooks.ts",
                          ".opencode/plugins/notes/README.md"])
