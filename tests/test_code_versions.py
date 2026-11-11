"""Tests for agentcontext.code_versions: the recorded versions of the collector's code and of
the code of the imports, the changes between two versions, and the repositories that each
change reaches.

Requirements: Python 3.14, git and rsync on PATH (stdlib only)
Run with:  python3 -m unittest tests.test_code_versions -v
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentcontext import code_versions, heuristics, imports, paths, reuse
from agentcontext.code_versions import (EVERY_REPOSITORY, NO_REPOSITORY, Version, changes,
                                        linked_skill_file,
                                        plugin_json_in_an_unlisted_manifest_directory,
                                        plugin_json_outside_manifest_dirs, reached,
                                        review_md_at_root)
from agentcontext.git import clone
from tests._fixtures import _init_git_repo

RECORDS = {"COLLECTOR": code_versions.COLLECTOR, "IMPORTS": code_versions.IMPORTS}


class TestTheRecordedVersions(unittest.TestCase):
    """Every change of the code declares the repositories that it reaches."""

    def test_the_current_code_is_the_last_recorded_version(self):
        """A run takes over a repository that an earlier version collected only through the record.
        A change of the code without a version of its own would make the next run collect every
        repository again. Add a version with the reach of the change, as the section "Recording a
        change of the collector" of docs/steps/repository_cloning.md describes."""
        for name, current in (("COLLECTOR", reuse.collector_fingerprint()),
                              ("IMPORTS", reuse.import_code_fingerprint())):
            with self.subTest(record=name):
                self.assertEqual(RECORDS[name][-1].fingerprint, current,
                                 f"Add Version({current!r}, ...) to code_versions.{name}")

    def test_every_version_after_the_first_declares_a_reach(self):
        for name, record in RECORDS.items():
            with self.subTest(record=name):
                fingerprints = [version.fingerprint for version in record]
                self.assertEqual(len(set(fingerprints)), len(fingerprints))
                for fingerprint in fingerprints:
                    self.assertRegex(fingerprint, r"\A[0-9a-f]{64}\Z")
                self.assertIsNone(record[0].reach)
                for version in record[1:]:
                    test = (version.reach.test
                            if isinstance(version.reach, (code_versions.RecordReach,
                                                          code_versions.WorkflowReach))
                            else version.reach)
                    self.assertTrue(version.reach in (NO_REPOSITORY, EVERY_REPOSITORY)
                                    or (callable(test)
                                        and test.__module__ == code_versions.__name__),
                                    version)
                    self.assertTrue(version.commits and version.change, version)

    def test_a_new_version_changes_neither_fingerprint(self):
        """The record decides which repositories a run collects again, not what a collection
        contains. A version added to a copy of the package keeps both fingerprints, which in
        the copy are those of the package itself."""
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "agentcontext"
            shutil.copytree(reuse.PACKAGE_ROOT, package,
                            ignore=shutil.ignore_patterns("__pycache__"))
            before = (reuse.collector_fingerprint(package), reuse.import_code_fingerprint(package))
            self.assertEqual(before, (reuse.collector_fingerprint(),
                                      reuse.import_code_fingerprint()))
            record = package / "code_versions.py"
            record.write_text(record.read_text(encoding="utf-8")
                              + '\nCOLLECTOR += (Version("e" * 64, "c", "a change", '
                                'NO_REPOSITORY),)\n', encoding="utf-8")
            self.assertEqual((reuse.collector_fingerprint(package),
                              reuse.import_code_fingerprint(package)), before)


def _unread():
    raise AssertionError("the file list was read for a change that needs none")


class TestTheChangesBetweenTwoVersions(unittest.TestCase):
    """`changes` and `reached` over a record of four versions."""

    RECORD = (Version("a" * 64, "c1", "the first version", None),
              Version("b" * 64, "c2", "a reworded text", NO_REPOSITORY),
              Version("c" * 64, "c3", "a SKILL.md that is a link", linked_skill_file),
              Version("d" * 64, "c4", "another format of the file list", EVERY_REPOSITORY))
    LINKED = [("100644", "README.md"), ("120000", ".claude/skills/s/SKILL.md")]
    PLAIN = [("100644", "README.md"), ("100644", ".claude/skills/s/SKILL.md")]

    def test_a_change_that_reaches_no_repository_reads_no_file_list(self):
        self.assertFalse(reached(changes(self.RECORD, "a" * 64, "b" * 64), _unread))

    def test_a_predicate_decides_by_the_file_list(self):
        span = changes(self.RECORD, "a" * 64, "c" * 64)
        self.assertEqual([version.commits for version in span], ["c2", "c3"])
        self.assertTrue(reached(span, lambda: self.LINKED))
        self.assertFalse(reached(span, lambda: self.PLAIN))

    def test_a_predicate_without_a_file_list_reaches_the_repository(self):
        self.assertTrue(reached(changes(self.RECORD, "b" * 64, "c" * 64), lambda: None))

    def test_a_change_of_every_repository_reaches_it_without_its_file_list(self):
        self.assertTrue(reached(changes(self.RECORD, "c" * 64, "d" * 64), _unread))

    def test_an_earlier_version_over_a_later_collection_has_the_same_changes(self):
        """An operator can import an earlier image to resume a run that a later image began."""
        self.assertEqual(changes(self.RECORD, "c" * 64, "a" * 64),
                         changes(self.RECORD, "a" * 64, "c" * 64))

    def test_an_unrecorded_version_reaches_every_repository(self):
        for old, new in (("e" * 64, "a" * 64), ("a" * 64, "e" * 64)):
            with self.subTest(old=old[0], new=new[0]):
                self.assertIsNone(changes(self.RECORD, old, new))
        self.assertTrue(reached(None, _unread))


class TestAReachOverTheRecord(unittest.TestCase):
    """`reached` with a `RecordReach`, which decides by the commits of each copied path that
    the per-repository commit record keeps, and `path_added_again`, the predicate of the change
    that dates a path added again from the commit that first added it."""

    RECORD = (Version("a" * 64, "c1", "the first version", None),
              Version("b" * 64, "c2", "the creation commit of a path added again",
                       code_versions.RecordReach(code_versions.path_added_again)),
              Version("c" * 64, "c3", "a SKILL.md that is a link", linked_skill_file))
    # The commits of a path oldest first by author date, as `commits.path_commits_entry` lists
    # them, here AGENTS.md of angular-eslint/angular-eslint, deleted and added again in 0398240.
    ADDED_AGAIN = [{"artifact": "AGENTS.md", "first": "0398240", "commits": ["dbc01a9", "0398240"]}]
    PLAIN = [{"artifact": "CLAUDE.md", "first": "27dd758", "commits": ["27dd758", "0398240"]},
             {"artifact": "skills/a/SKILL.md", "first": "", "commits": []}]

    def test_the_record_decides(self):
        span = changes(self.RECORD, "a" * 64, "b" * 64)
        self.assertTrue(reached(span, _unread, lambda: self.ADDED_AGAIN))
        self.assertFalse(reached(span, _unread, lambda: self.PLAIN))
        self.assertFalse(reached(span, _unread, lambda: []))

    def test_a_record_that_cannot_show_the_paths_reaches_the_repository(self):
        span = changes(self.RECORD, "a" * 64, "b" * 64)
        self.assertTrue(reached(span, _unread, lambda: None))
        self.assertTrue(reached(span, _unread))

    def test_both_predicates_decide_together(self):
        span = changes(self.RECORD, "a" * 64, "c" * 64)
        linked = [("120000", ".claude/skills/s/SKILL.md")]
        self.assertFalse(reached(span, lambda: [], lambda: self.PLAIN))
        self.assertTrue(reached(span, lambda: linked, lambda: self.PLAIN))
        self.assertTrue(reached(span, lambda: [], lambda: self.ADDED_AGAIN))

    def test_a_change_over_the_file_list_reads_no_record(self):
        def unread():
            raise AssertionError("the record was read for a change that needs none")
        self.assertFalse(reached(changes(self.RECORD, "b" * 64, "c" * 64), lambda: [], unread))

    def test_the_version_that_dates_a_path_added_again_reaches_the_repositories_with_such_a_path(self):
        """The version that dates a path added again from the commit that first added it decides
        by the per-repository commit record and reaches no repository without such a path."""
        version = next(v for v in code_versions.COLLECTOR if v.commits.startswith(
            "Date a path added again"))
        self.assertIsInstance(version.reach, code_versions.RecordReach)
        self.assertTrue(version.reach.test(self.ADDED_AGAIN))
        self.assertFalse(version.reach.test(self.PLAIN))


class TestAReachOverTheWorkflows(unittest.TestCase):
    """`reached` with a `WorkflowReach`, which decides by the text of each workflow file that the
    earlier collection kept, and `prompt_file_input`, the predicate of the change that copies
    the prompt file of the Claude Code Base Action."""

    RECORD = (Version("a" * 64, "c1", "the first version", None),
              Version("b" * 64, "c2", "the prompt file of an agent action",
                       code_versions.WorkflowReach(code_versions.prompt_file_input)),
              Version("c" * 64, "c3", "a SKILL.md that is a link", linked_skill_file))
    # facebook/rocksdb's ai-review-analysis.yml at 935cdad34df0, shortened.
    BASE_ACTION = {"ai-review-analysis.yml":
                   "jobs:\n  recover:\n    steps:\n"
                   "      - uses: anthropics/claude-code-base-action@beta\n        with:\n"
                   "          prompt_file: claude_md/ci_recovery_prompt.md\n"}
    PLAIN = {"claude.yml": "jobs:\n  a:\n    steps:\n"
                           "      - uses: anthropics/claude-code-action@v1\n"
                           "        with:\n          prompt: Review the change.\n",
             "ci.yml": "jobs:\n  a:\n    steps:\n      - run: make\n"}

    def test_the_workflows_decide(self):
        span = changes(self.RECORD, "a" * 64, "b" * 64)
        self.assertTrue(reached(span, _unread, _unread, lambda: self.BASE_ACTION))
        self.assertFalse(reached(span, _unread, _unread, lambda: self.PLAIN))
        self.assertFalse(reached(span, _unread, _unread, lambda: {}))

    def test_workflows_that_were_not_all_kept_reach_the_repository(self):
        span = changes(self.RECORD, "a" * 64, "b" * 64)
        self.assertTrue(reached(span, _unread, _unread, lambda: None))
        self.assertTrue(reached(span, _unread))

    def test_a_change_over_the_file_list_reads_no_workflow(self):
        def unread():
            raise AssertionError("the workflows were read for a change that needs none")
        self.assertFalse(reached(changes(self.RECORD, "b" * 64, "c" * 64), lambda: [], _unread,
                                 unread))

    def test_the_version_that_copies_the_prompt_file_of_the_base_action_reads_the_workflows(self):
        version = next(v for v in code_versions.COLLECTOR if v.commits.startswith(
            "Copy the prompt file that a workflow gives the Claude Code Base Action"))
        self.assertIsInstance(version.reach, code_versions.WorkflowReach)
        self.assertTrue(version.reach.test(self.BASE_ACTION))
        self.assertFalse(version.reach.test(self.PLAIN))


def _commit_layout(root: Path, files: dict, links: dict) -> list:
    """A git repository at `root` with `files`, `{path: text}`, and `links`, `{path: target}`,
    committed, and its file list as the cloning step keeps it."""
    _init_git_repo(root)
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    for rel, target in links.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, root / rel)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "layout"], check=True,
                   capture_output=True)
    listed = root.parent / f"{root.name}{clone.FILE_LIST_SUFFIX}"
    clone.write_file_list(clone.tree_listing(root), listed)
    return clone.read_file_list(listed)


def _collected(repo: Path, out: Path, vendored=frozenset()) -> dict:
    """{path: bytes} of what the collector copies from `repo` into `out`, with the target of a
    link that rsync kept in place of its bytes."""
    paths.collect_repo(repo, out, set(vendored))
    if not out.is_dir():
        return {}
    return {rel: (os.readlink(out / rel).encode() if (out / rel).is_symlink()
                  else (out / rel).read_bytes()) for rel in paths.tree_files(out)}


class TestALinkedSkillFile(unittest.TestCase):
    """The reach of the change after which the cloning step copies the file that a SKILL.md links
    to in a folder of a skills directory (`paths.skill_files`). The collector before it is the
    same code with `paths.skill_files` finding no SKILL.md. The predicate has to be true for
    every repository that the two collect differently, here the two layouts with such a link."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LAYOUTS = {
        "a SKILL.md that is a link": (
            {"skills/s/SKILL.md": SKILL, ".claude/skills/s/notes.md": "notes\n"},
            {".claude/skills/s/SKILL.md": "../../../skills/s/SKILL.md"}, True),
        "such a link in a package": (
            {"shared/z/SKILL.md": SKILL, "pkg/.agents/skills/z/ref.md": "ref\n"},
            {"pkg/.agents/skills/z/SKILL.md": "../../../../shared/z/SKILL.md"}, True),
        "a skill without a link": ({".claude/skills/s/SKILL.md": SKILL}, {}, False),
        "a skill folder that is a link": (
            {"shared/y/SKILL.md": SKILL}, {".claude/skills/y": "../../shared/y"}, False),
        "a SKILL.md that is a link outside a skills directory": (
            {"shared/SKILL.md": SKILL, ".claude/agents/pdf/a.md": "a\n"},
            {".claude/agents/pdf/SKILL.md": "../../../shared/SKILL.md"}, False),
        "another link in a skill folder": (
            {"docs/guide.md": "guide\n", ".claude/skills/s/SKILL.md": SKILL},
            {".claude/skills/s/guide.md": "../../../docs/guide.md"}, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "skill_files", lambda base: []):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(linked_skill_file(entries), differs)

    def test_the_predicate_reads_the_skills_directories_of_the_collector(self):
        """Every skills directory of `paths._SKILL_PARENTS` counts, at any depth and in any
        case, so that a skills directory added to the heuristics reaches its repositories."""
        for parent in paths._SKILL_PARENTS:
            for path in (f"{parent}/s/SKILL.md", f"pkg/{parent.upper()}/s/skill.md"):
                with self.subTest(path=path):
                    self.assertTrue(linked_skill_file([("120000", path)]))
                    self.assertFalse(linked_skill_file([("100644", path)]))
        self.assertFalse(linked_skill_file([("120000", ".claude/skills/a/b/SKILL.md")]))
        self.assertFalse(linked_skill_file([("120000", "x.claude/skills/s/SKILL.md")]))


class TestARootPluginJson(unittest.TestCase):
    """The reach of the change after which a `plugin.json` with the content of a legacy Copilot
    CLI manifest makes its directory a Copilot plugin (`paths.copilot_legacy_manifest`). The
    collector before it is the same code with no plugin.json passing that test. The predicate
    has to be true for every repository that the two collect differently."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LEGACY = '{"name": "p", "skills": ["./skills/"]}'
    LAYOUTS = {
        "a plugin with a legacy manifest": (
            {"plugins/p/plugin.json": LEGACY, "plugins/p/skills/s/SKILL.md": SKILL}, True),
        # The collector copies this .github/plugin.json with Copilot's .github already, and the
        # change alters only the rows of the detection step there.
        "a .github directory with a legacy manifest": (
            {".github/plugin.json": LEGACY, ".github/skills/s/SKILL.md": SKILL}, False),
        "a legacy manifest beside the .github manifest": (
            {"p/plugin.json": LEGACY, "p/.github/plugin/plugin.json": '{"name": "p"}',
             "p/skills/s/SKILL.md": SKILL}, True),
        "another program's plugin.json": (
            {"src/plugin.json": '{"type": "app", "id": "x", "extensions": {}}',
             "src/skills/s/SKILL.md": SKILL}, False),
        "a Claude Code manifest": (
            {"p/.claude-plugin/plugin.json": LEGACY, "p/skills/s/SKILL.md": SKILL}, False),
        "no plugin.json": ({"p/skills/s/SKILL.md": SKILL, "AGENTS.md": "# a\n"}, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, {})
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "copilot_legacy_manifest",
                                           lambda root, base, text: False):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    if differs:
                        self.assertTrue(plugin_json_outside_manifest_dirs(entries))

    def test_the_predicate_reads_the_manifest_directories_of_the_collector(self):
        """A plugin.json in any of `paths._MANIFEST_DIRS` is the manifest of its vendor, and a
        plugin.json anywhere else can be a legacy manifest."""
        for directory in paths._MANIFEST_DIRS:
            for path in (f"{directory}/plugin.json", f"pkg/{directory}/plugin.json"):
                with self.subTest(path=path):
                    self.assertFalse(plugin_json_outside_manifest_dirs([("100644", path)]))
        for path in ("plugin.json", "plugins/p/plugin.json", ".github/plugin.json",
                     "x.claude-plugin/plugin.json"):
            with self.subTest(path=path):
                self.assertTrue(plugin_json_outside_manifest_dirs([("100644", path)]))
        self.assertFalse(plugin_json_outside_manifest_dirs([("100644", "plugins/plugin.jsonc")]))


class TestAPluginJsonInAnUnlistedManifestDirectory(unittest.TestCase):
    """The reach of the change after which `paths.copilot_legacy_manifest` refuses a plugin.json
    directly in a directory named `.<name>-plugin` (`paths._VENDOR_MANIFEST_DIRECTORY`). The
    collector before it is the same code with a pattern that matches no name. The predicate has
    to be true for every repository that the two collect differently."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LEGACY = '{"name": "p", "skills": "./skills/"}'
    LAYOUTS = {
        "a legacy manifest in a .grok-plugin directory": (
            {"p/.grok-plugin/plugin.json": LEGACY, "p/.grok-plugin/skills/s/SKILL.md": SKILL},
            True),
        # saadeghi/daisyui: the collector copies the skills directory, with the Grok manifest in
        # it, as part of the Claude Code plugin at the root, and the change alters only the rows
        # of the detection step.
        "a Grok manifest in the skills directory of a Claude Code plugin": (
            {".claude-plugin/plugin.json": '{"name": "daisyui"}',
             "skills/.grok-plugin/plugin.json": '{"name": "daisyui", "skills": "./"}',
             "skills/daisyui/SKILL.md": SKILL}, False),
        # The collector copies p/.claude-plugin/plugin.json as the manifest of the Claude Code
        # plugin at p, whatever the legacy test of the Copilot plugin at p/.claude-plugin
        # returns.
        "a Claude Code manifest directory that a Copilot manifest makes a plugin": (
            {"p/.claude-plugin/plugin.json": LEGACY, "p/.claude-plugin/.plugin/plugin.json":
             '{"name": "p"}', "p/.claude-plugin/skills/s/SKILL.md": SKILL}, False),
        "a legacy manifest at a plugin root": (
            {"plugins/p/plugin.json": LEGACY, "plugins/p/skills/s/SKILL.md": SKILL}, False),
        "a directory whose name only ends in -plugin": (
            {"my-plugin/plugin.json": LEGACY, "my-plugin/skills/s/SKILL.md": SKILL}, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, {})
                    after = _collected(root, Path(tmp) / f"after{number}")
                    no_name = re.compile("(?!)")
                    with mock.patch.object(paths, "_VENDOR_MANIFEST_DIRECTORY", no_name):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    if differs:
                        self.assertTrue(plugin_json_in_an_unlisted_manifest_directory(entries))

    def test_the_predicate_reads_the_manifest_directories_of_the_collector(self):
        """A plugin.json directly in a directory named `.<name>-plugin` outside
        `paths._MANIFEST_DIRS` counts at any depth. One of `paths._MANIFEST_DIRS` counts only
        where another Copilot CLI manifest makes its directory a plugin."""
        for path in (".grok-plugin/plugin.json", "skills/.grok-plugin/plugin.json",
                     "pkg/.my-tool-plugin/plugin.json"):
            with self.subTest(path=path):
                self.assertTrue(plugin_json_in_an_unlisted_manifest_directory([("100644", path)]))
        for directory in paths._MANIFEST_DIRS:
            for path in (f"{directory}/plugin.json", f"pkg/{directory}/plugin.json"):
                with self.subTest(path=path):
                    self.assertFalse(
                        plugin_json_in_an_unlisted_manifest_directory([("100644", path)]))
        for manifest in paths.PLUGIN_ROOTS["<copilot-plugin>"]:
            entries = [("100644", "p/.claude-plugin/plugin.json"),
                       ("100644", f"p/.claude-plugin/{manifest}")]
            with self.subTest(manifest=manifest):
                self.assertTrue(plugin_json_in_an_unlisted_manifest_directory(entries))
        for path in ("plugin.json", "my-plugin/plugin.json", ".grok-plugin/marketplace.json",
                     ".grok-plugin/sub/plugin.json", "-plugin/plugin.json"):
            with self.subTest(path=path):
                self.assertFalse(plugin_json_in_an_unlisted_manifest_directory([("100644", path)]))


class TestAnAntigravityPlugin(unittest.TestCase):
    """The reach of the change after which the cloning step copies the skills that the `skills`
    key of an Antigravity plugin's manifest lists (`paths._antigravity_manifest_skills`). The
    collector before it is the same code with that function finding no skill. The predicate
    reads no file, so it has to be true for every repository with an Antigravity plugin, and
    for these layouts it is false only where there is none."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LAYOUTS = {
        "a manifest that lists a skill outside the plugin": (
            {".agents/plugins/p/plugin.json": '{"name": "p", "skills": "shared"}',
             "shared/s/SKILL.md": SKILL}, {}, True, True),
        "a manifest in a folder of the plugin": (
            {"_agent/plugins/p/.gemini-plugin/plugin.json": '{"name": "p", "skills": ["x"]}',
             "_agent/plugins/p/x/SKILL.md": SKILL, "src/x/SKILL.md": SKILL}, {}, False, True),
        "a plugin that a plugins.json registers": (
            {".agents/plugins.json": '{"entries": [{"path": "tools/p"}]}',
             "tools/p/plugin.json": '{"name": "p", "skills": ["../../docs/s"]}',
             "docs/s/SKILL.md": SKILL}, {}, True, True),
        # The cloning step finds no plugin through a link to a plugin directory.
        "a plugin directory that is a link": (
            {"plugins/p/plugin.json": '{"name": "p", "skills": "../../more"}',
             "more/s/SKILL.md": SKILL}, {".agents/plugins/p": "../../plugins/p"}, False, False),
        "a plugins.json behind a linked customization root": (
            {"config/agents/plugins.json": '{"entries": [{"path": "tools/p"}]}',
             "tools/p/plugin.json": '{"name": "p", "skills": ["../../docs/s"]}',
             "docs/s/SKILL.md": SKILL}, {".agents": "config/agents"}, True, True),
        "a manifest without the key": (
            {".agents/plugins/p/plugin.json": '{"name": "p"}', "shared/s/SKILL.md": SKILL},
            {}, False, True),
        "skills and no plugin": (
            {".agents/skills/s/SKILL.md": SKILL, "plugin.json": '{"name": "p", "skills": "."}',
             "SKILL.md": SKILL}, {}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "_antigravity_manifest_skills",
                                           lambda *args: []):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.antigravity_plugin(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_plugin_places_of_the_collector(self):
        """Every default plugin directory of `paths.PLUGIN_PARENTS` and every manifest of
        `paths.PLUGIN_ROOTS` counts, in any case, so that a place added to the heuristics reaches
        its repositories."""
        token = "<antigravity-plugin>"
        for parent in paths.PLUGIN_PARENTS[token]:
            for manifest in paths.PLUGIN_ROOTS[token]:
                for path in (f"{parent}/p/{manifest}", f"{parent}/P/{manifest}".upper()):
                    with self.subTest(path=path):
                        self.assertTrue(code_versions.antigravity_plugin([("100644", path)]))
        for path in ("pkg/.agents/plugins.json", ".agents/plugins.json", "config/agents/plugins.json"):
            self.assertTrue(code_versions.antigravity_plugin([("100644", path)]))
        for path in ("plugins/p/plugin.json", ".agents/plugins/plugin.json",
                     ".agents/plugins/p/q/plugin.json", "pkg/.agents/plugins/p/plugin.json"):
            self.assertFalse(code_versions.antigravity_plugin([("100644", path)]))


class TestAnAntigravityRootAFolderAgentOrAClaudeManifest(unittest.TestCase):
    """The reach of the change after which the cloning step copies `_agents` and `_agent` only
    with a place of Antigravity, reads a folder agent's `agent.md` in any letter case, and flags
    claude for a Claude Code manifest that configures servers or hooks inline. It reads no file,
    and it reaches the repositories of the data in which the change copies or flags otherwise."""

    def test_the_predicate_holds_for_the_layouts_that_the_change_collects_differently(self):
        reach = code_versions.antigravity_root_folder_agent_or_claude_manifest
        for path in ("nemo_curator/stages/audio/_agent/_roles.py",
                     "py/packages/genkit/src/genkit/_ai/_agents/_base.py",
                     "_agents/skills/s/SKILL.md",
                     ".agent/agents/oshub-test-writer/AGENT.md",
                     ".claude-plugin/plugin.json",
                     "clients/claude-plugin/.claude-plugin/plugin.json",
                     "plugins/.claude-plugin/marketplace.json"):
            with self.subTest(path=path):
                self.assertTrue(reach([("100644", path)]))
        for path in (".agent/agents/a/agent.md", "src/agents/agent.py", "README.md",
                     ".claude/settings.json", ".codex-plugin/plugin.json", "my_agent/x.py"):
            with self.subTest(path=path):
                self.assertFalse(reach([("100644", path)]))


class TestACursorPluginManifest(unittest.TestCase):
    """The reach of the change after which the cloning step flags cursor for a Cursor manifest or
    marketplace entry that configures MCP servers inline. It reads no file."""

    def test_the_predicate_holds_for_a_manifest_or_a_marketplace_file(self):
        for path in (".cursor-plugin/plugin.json", ".cursor-plugin/marketplace.json",
                     "plugins/gitdiagram/.cursor-plugin/plugin.json",
                     "agent-plugins/anarlog/.cursor-plugin/plugin.json"):
            with self.subTest(path=path):
                self.assertTrue(code_versions.cursor_plugin_manifest([("100644", path)]))

    def test_the_predicate_does_not_hold_for_another_file(self):
        for path in (".cursor-plugin/README.md", ".claude-plugin/plugin.json", "plugin.json",
                     ".cursor/mcp.json", "cursor-plugin/plugin.json"):
            with self.subTest(path=path):
                self.assertFalse(code_versions.cursor_plugin_manifest([("100644", path)]))


class TestTheInstructionsThatACodexRoleNames(unittest.TestCase):
    """The reach of the change after which the cloning step copies the files that a Codex agent
    role names in `model_instructions_file` and the role files that `config_file` of
    `.codex/config.toml` names (`paths.declared_subagent_files`). The collector before it is the
    same code with `paths.declared_subagent_files` naming no file. The predicate has to be true
    for every repository that the two collect differently."""

    ROLE = 'name = "planner"\nmodel_instructions_file = "%s"\n'
    LAYOUTS = {
        "a role with its instructions outside .codex": (
            {".codex/agents/planner.toml": ROLE % "../../docs/planner.md",
             "docs/planner.md": "Plan.\n"}, {}, True, True),
        # areal-project/AReaL: the collector copies .codex whole before the change too.
        "a role with its instructions beside it": (
            {".codex/agents/planner.toml": ROLE % "./planner.md",
             ".codex/agents/planner.md": "Plan.\n"}, {}, False, True),
        "a role file that the config names outside .codex": (
            {".codex/config.toml": '[agents.planner]\nconfig_file = "../roles/planner.toml"\n',
             "roles/planner.toml": ROLE % "planner.md", "roles/planner.md": "Plan.\n"},
            {}, True, True),
        "a .codex that is a link": (
            {"config/codex/agents/planner.toml": ROLE % "../../../docs/planner.md",
             "docs/planner.md": "Plan.\n"}, {".codex": "config/codex"}, True, True),
        "no role": ({"AGENTS.md": "# a\n", "docs/planner.md": "Plan.\n"}, {}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "declared_subagent_files",
                                           lambda *args, **kwargs: []):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.agent_role_file(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_places_of_the_rows(self):
        """A TOML file below the agents directory of a row counts, and so do the config.toml of
        its folder and an entry at the place of either directory, in any letter case."""
        for path in (".codex/agents/a.toml", ".codex/agents/review/b.toml", ".CODEX/Agents/c.TOML",
                     ".codex/config.toml", ".codex", ".codex/agents"):
            with self.subTest(path=path):
                self.assertTrue(code_versions.agent_role_file([("100644", path)]))
        for path in (".codex/agents/a.md", ".codex/skills/s/SKILL.md", "pkg/.codex/agents/a.toml",
                     "agents/a.toml"):
            with self.subTest(path=path):
                self.assertFalse(code_versions.agent_role_file([("100644", path)]))


class TestThePromptFilesOfAnOpenCodeAgent(unittest.TestCase):
    """The reach of the change after which the cloning step copies the files that a `{file:...}`
    reference in the prompt of an OpenCode agent names (`paths.opencode_prompt_files`). The
    collector before it is the same code with `paths.opencode_prompt_files` naming no file. The
    predicate has to be true for every repository that the two collect differently."""

    AGENT = '{"agent": {"docs": {"prompt": "{file:%s}"}}}\n'
    LAYOUTS = {
        "an agent with its prompt outside .opencode": (
            {"opencode.json": AGENT % "./prompts/docs.md",
             "prompts/docs.md": "Write the docs.\n"}, {}, True, True),
        # cakmoel/Scriptlog: the collector copies .opencode whole before the change too.
        "an agent with its prompt in .opencode": (
            {"opencode.json": AGENT % ".opencode/prompts/docs.md",
             ".opencode/prompts/docs.md": "Write the docs.\n"}, {}, False, True),
        "a configuration of a package in .opencode": (
            {"pkg/.opencode/opencode.jsonc": AGENT % "../prompts/docs.md",
             "pkg/prompts/docs.md": "Write the docs.\n"}, {}, True, True),
        # The collector stored such a configuration with its content before the change too,
        # because it reads the file for the context files that `instructions` lists.
        "a configuration that is a link": (
            {"config/opencode.json": AGENT % ".opencode/prompts/docs.md",
             ".opencode/prompts/docs.md": "Write the docs.\n"},
            {"opencode.json": "config/opencode.json"}, False, True),
        "no configuration": ({"AGENTS.md": "# a\n", "prompts/docs.md": "Write.\n"}, {}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "opencode_prompt_files",
                                           lambda *args, **kwargs: []):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.opencode_agent_configuration(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_places_of_the_rows(self):
        for path in ("opencode.json", "OpenCode.jsonc", ".opencode/opencode.json",
                     "apps/web/.opencode/opencode.jsonc"):
            with self.subTest(path=path):
                self.assertTrue(code_versions.opencode_agent_configuration([("100644", path)]))
        for path in ("pkg/opencode.json", ".opencode/agents/a.md", "opencode.yaml"):
            with self.subTest(path=path):
                self.assertFalse(code_versions.opencode_agent_configuration([("100644", path)]))


class TestTheFilesThatAnotherConfigurationFileNames(unittest.TestCase):
    """The version of the merge 0e1c4c3e, which brought the copies of the files that a Codex role
    or the Codex config names and of the prompt files of an OpenCode agent onto `main`. The
    collector's code of the commits 00d6bfcf and ae4b9c0e of the merged branch has the
    fingerprints of `BRANCH` and lacks the readers of a plugin that a marketplace lists, which the
    version of 10c04402 records (`paths.plugin_roots`). A line under either fingerprint is
    therefore collected again, and a line under 10c04402 gets the reach of both copies."""

    PLUGIN_READERS = "10c044024500dc72bce5a45f171b4096b4db55bcfe9b280678a4b15c7b662605"
    MERGE = "1b683eabadde2c10f231c33000cf69a4db930a6749aa7cae2b893b231b612ca1"
    BRANCH = ("5248789808846c1f0e05187eb68bb4dc8c35b9836e0cd7e7cff5a52199ce2603",
              "4b1294345a38a16fcd56ec79caecf49759a0d325731fe801de76622352179d35")
    MARKETPLACE = [("100644", ".agents/plugins/marketplace.json"),
                   ("100644", "plugins/p/skills/s/SKILL.md")]
    ROLE = [("100644", ".codex/config.toml"), ("100644", ".codex/agents/planner.toml")]
    OPENCODE = [("100644", "opencode.json"), ("100644", "prompts/docs.md")]
    NEITHER = [("100644", "AGENTS.md"), ("100644", "prompts/docs.md")]

    def test_a_line_of_a_commit_of_the_branch_is_collected_again(self):
        for fingerprint in self.BRANCH:
            with self.subTest(fingerprint=fingerprint[:8]):
                self.assertTrue(reached(changes(code_versions.COLLECTOR, fingerprint, self.MERGE),
                                        lambda: self.MARKETPLACE))

    def test_the_merge_reaches_a_codex_role_and_an_opencode_agent(self):
        span = changes(code_versions.COLLECTOR, self.PLUGIN_READERS, self.MERGE)
        for name, entries, reaches in (("a Codex role", self.ROLE, True),
                                       ("an OpenCode configuration", self.OPENCODE, True),
                                       ("neither", self.NEITHER, False)):
            with self.subTest(layout=name):
                self.assertEqual(reached(span, lambda: entries), reaches)
                self.assertEqual(
                    code_versions.agent_role_file_or_opencode_agent_configuration(entries), reaches)


class TestTheReadersThatAMarketplaceOrAManifestIdentifies(unittest.TestCase):
    """The reach of the change after which a folder that a Codex or Copilot CLI marketplace lists
    is a plugin of that tool (`paths.plugin_roots`), and the portable `skills/` and `mcp.json` of
    a plugin with such a marketplace or with the tool's manifest beside the portable one are the
    tool's parts (`paths._PORTABLE_READERS`), and a Copilot CLI marketplace is copied as a
    manifest. The collector before it is the same code with no marketplace entry of either tool,
    no Copilot CLI marketplace among the manifests and no such reader. The predicate has to be
    true for every repository that the two collect or flag differently."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    PORTABLE = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",'
                ' "name": "p"}')
    CODEX_MARKETPLACE = ('{"name": "m", "plugins": [{"name": "p", "source": "./plugins/p",'
                         ' "category": "Coding"}]}')
    COPILOT_MARKETPLACE = ('{"name": "m", "owner": {"name": "o"},'
                           ' "plugins": [{"name": "p", "source": "plugins/p"}]}')
    LAYOUTS = {
        "a portable plugin that a Codex marketplace lists": (
            {"plugins/p/plugin.json": PORTABLE, "plugins/p/skills/s/SKILL.md": SKILL,
             ".agents/plugins/marketplace.json": CODEX_MARKETPLACE}, True),
        "a Claude Code plugin with an apps file that a Codex marketplace lists": (
            {"plugins/p/.claude-plugin/plugin.json": '{"name": "p"}',
             "plugins/p/.app.json": '{"apps": {"forms": {"id": "plugin_asdk_app_1"}}}',
             ".agents/plugins/marketplace.json": CODEX_MARKETPLACE}, True),
        "a Claude Code plugin with hooks.json that a Copilot marketplace lists": (
            {"plugins/p/.claude-plugin/plugin.json": '{"name": "p"}',
             "plugins/p/hooks.json":
                 '{"hooks": {"sessionStart": [{"type": "command", "bash": "x"}]}}',
             ".github/plugin/marketplace.json": COPILOT_MARKETPLACE}, True),
        "a Claude Code plugin that a Copilot marketplace below the root lists": (
            {"pkg/plugins/p/.claude-plugin/plugin.json": '{"name": "p"}',
             "pkg/plugins/p/skills/s/SKILL.md": SKILL,
             "pkg/.github/plugin/marketplace.json": COPILOT_MARKETPLACE}, True),
        "a portable manifest beside a Codex manifest": (
            {"plugins/p/plugin.json": PORTABLE, "plugins/p/.codex-plugin/plugin.json": '{"name": "p"}',
             "plugins/p/skills/s/SKILL.md": SKILL}, True),
        "a Claude Code plugin with hooks.json and no marketplace": (
            {"plugins/p/.claude-plugin/plugin.json": '{"name": "p"}',
             "plugins/p/hooks.json":
                 '{"hooks": {"sessionStart": [{"type": "command", "bash": "x"}]}}'}, False),
        "a portable plugin and no marketplace": (
            {"plugins/p/plugin.json": PORTABLE, "plugins/p/skills/s/SKILL.md": SKILL}, False),
    }

    @staticmethod
    def _collected(repo: Path, out: Path) -> tuple:
        """What the collector copies from `repo` into `out`, and the flags that it returns."""
        flags = paths.collect_repo(repo, out, set())
        files = ({rel: (out / rel).read_bytes() for rel in paths.tree_files(out)}
                 if out.is_dir() else {})
        return files, flags

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        no_entries = lambda root, vendored_dirs, listed=None: {}
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, {})
                    after = self._collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "_codex_marketplace_entries", no_entries), \
                            mock.patch.object(paths, "_copilot_marketplace_entries", no_entries), \
                            mock.patch.object(paths, "_copilot_marketplace_files", no_entries), \
                            mock.patch.dict(paths._PORTABLE_READERS, clear=True):
                        before = self._collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs,
                                     (sorted(before[0]), before[1], sorted(after[0]), after[1]))
                    if differs:
                        self.assertTrue(
                            code_versions.codex_or_copilot_marketplace_or_portable_overlay(entries))

    def test_the_predicate_reads_the_marketplaces_and_manifests_of_the_collector(self):
        reach = code_versions.codex_or_copilot_marketplace_or_portable_overlay
        for listed in ([".agents/plugins/marketplace.json"],
                       ["qamomile/agent_plugins/.agents/plugins/marketplace.json"],
                       ["marketplace.json"], [".plugin/marketplace.json"],
                       ["ts/.github/plugin/marketplace.json"],
                       ["plugin.json", ".codex-plugin/plugin.json"],
                       ["p/plugin.json", "p/.plugin/plugin.json"],
                       ["p/plugin.json", "p/.github/plugin/plugin.json"]):
            with self.subTest(listed=listed):
                self.assertTrue(reach([("100644", path) for path in listed]))
        for listed in (["docs/marketplace.json"], [".claude-plugin/marketplace.json"],
                       ["p/plugin.json", "p/.claude-plugin/plugin.json"],
                       [".codex-plugin/plugin.json"], ["plugins/p/plugin.json"]):
            with self.subTest(listed=listed):
                self.assertFalse(reach([("100644", path) for path in listed]))


class TestALinkedToolDirectory(unittest.TestCase):
    """The reach of the change after which the cloning step collects a tool directory that is a
    link as the link's marker and the directory that it leads to (`paths.TOOL_DIRECTORIES`,
    `paths.linked_tool_directories`). The collector before it is the same code with no tool
    directory, which finds no such link. The predicate has to be true for every repository that
    the two collect differently. It reads no file, and it is also true for a file with the name
    of a tool directory, which the change leaves alone where its text names no directory."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    AGENT = "---\nname: a\ndescription: Reviews.\n---\nReview.\n"
    LAYOUTS = {
        "a .claude that links to a directory of the repository": (
            {"config/claude/skills/s/SKILL.md": SKILL, "AGENTS.md": "# a\n"},
            {".claude": "config/claude"}, True, True),
        # comet-ml/opik: the collector copies `.agents` with its own pass, and the change adds
        # the markers.
        "a .cursor and a .codex that link to .agents": (
            {".agents/agents/a.md": AGENT}, {".cursor": ".agents", ".codex": ".agents"}, True,
            True),
        "a Codex config in a .codex that is a link": (
            {"config/codex/config.toml": 'model_instructions_file = "notes.md"\n',
             "config/codex/notes.md": "Use tabs.\n"}, {".codex": "config/codex"}, True, True),
        "a link at a tool directory below the root": (
            {"web/.agents/skills/s/SKILL.md": SKILL}, {"web/.claude": ".agents"}, True, True),
        "a tool directory that links out of the repository": (
            {"AGENTS.md": "# a\n"}, {"api/.claude": "/home/u/repo/.claude"}, True, True),
        # vitessio/vitess has an empty regular file `.codex`.
        "an empty file with the name of a tool directory": (
            {".codex": "", "AGENTS.md": "# a\n"}, {}, False, True),
        "a tool directory that is a directory": (
            {".claude/skills/s/SKILL.md": SKILL}, {}, False, False),
        "a mechanism directory that is a link": (
            {".agents/skills/s/SKILL.md": SKILL}, {".claude/skills": "../.agents/skills"}, False,
            False),
        "a .github that is a link": (
            {"docs/github/copilot-instructions.md": "Use tabs.\n"}, {".github": "docs/github"},
            False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "TOOL_DIRECTORIES", ()):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.linked_tool_directory(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_tool_directories_of_the_collector(self):
        """Every tool directory of `paths.TOOL_DIRECTORIES` counts, at any depth and in any
        case, so that a tool directory added to the heuristics reaches its repositories."""
        for name in paths.TOOL_DIRECTORIES:
            for path in (name, f"pkg/{name.upper()}"):
                with self.subTest(path=path):
                    self.assertTrue(code_versions.linked_tool_directory([("120000", path)]))
        for path in (".github", ".claude/skills", "x.claude", ".claude-plugin", ".agents.md"):
            with self.subTest(path=path):
                self.assertFalse(code_versions.linked_tool_directory([("120000", path)]))


class _NoLinkedToolDirectories:
    """`paths` as `planning.find` used it before it read the planning documents through a linked
    tool directory: every function of the module, and no link of a tool directory."""

    def __getattr__(self, name):
        return getattr(paths, name)

    @staticmethod
    def linked_tool_directory_pairs(root, vendored_dirs):
        return []


class TestTheLocalStateAndThePlansOfALinkedToolDirectory(unittest.TestCase):
    """The reach of the change after which the copy of the directory that a linked tool directory
    leads to leaves out the linking tool's local state (`paths.remove_local_state`), and the
    planning documents are read through the link (`planning.find`). The collector before it
    is the same code without the removal and with no link for the planning documents. The
    predicate of the earlier change, `linked_tool_directory`, has to hold for every repository
    that the two collect differently."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LAYOUTS = {
        "Claude's local state in the .agents that .claude links to": (
            {".agents/skills/s/SKILL.md": SKILL, ".agents/shell-snapshots/x.sh": "export A=1\n"},
            {".claude": ".agents"}, True, True),
        "a plans folder that the settings behind a linked .claude name": (
            {".agents/settings.json": '{"plansDirectory": "docs/plans"}',
             "docs/plans/auth.md": "# Auth\n", "AGENTS.md": "# a\n"},
            {".claude": ".agents"}, True, True),
        "a .claude that links to .agents without local state or plans": (
            {".agents/skills/s/SKILL.md": SKILL}, {".claude": ".agents"}, False, True),
        "Claude's local state in a .claude that is a directory": (
            {".claude/skills/s/SKILL.md": SKILL, ".claude/shell-snapshots/x.sh": "export A=1\n"},
            {}, False, False),
        "a shell-snapshots folder in an .agents that no link reaches": (
            {".agents/skills/s/SKILL.md": SKILL, ".agents/shell-snapshots/x.sh": "export A=1\n"},
            {}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        from agentcontext import planning
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "remove_local_state", lambda *args: None), \
                            mock.patch.object(planning, "paths", _NoLinkedToolDirectories()):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.linked_tool_directory(entries), reaches)
                    self.assertTrue(reaches or not differs)


class TestALinkedDirectoryInACopiedDirectory(unittest.TestCase):
    """The reach of the change after which `paths.copy_dir` writes the marker of a link to a
    directory that rsync leaves out of the directory that it copies (`paths.directory_links`).
    The collector before it is the same code with no such link. The predicate has to be true
    for every repository that the two collect differently."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    TOOLS = {"tools/run.sh": "exit 0\n"}
    LAYOUTS = {
        # meshery/meshery
        "a link in .claude to a directory of .agents": (
            {".claude/settings.json": "{}\n", ".agents/hooks/check.sh": "exit 0\n"},
            {".claude/hooks": "../.agents/hooks"}, True, True),
        # tursodatabase/turso
        "a link in a skill folder to a directory outside .claude": (
            {".claude/skills/s/SKILL.md": SKILL, **TOOLS},
            {".claude/skills/s/scripts": "../../../tools"}, True, True),
        "a link in the root's .github with a Copilot path": (
            {".github/copilot-instructions.md": "Use tabs.\n", **TOOLS},
            {".github/tools": "../tools"}, True, True),
        "a link in the root's .github without a Copilot path": (
            {".github/workflows/ci.yml": "jobs: {}\n", "AGENTS.md": "# a\n", **TOOLS},
            {".github/tools": "../tools"}, False, False),
        "a link in a folder of a .github below the root": (
            {"pkg/.github/skills/s/SKILL.md": SKILL, **TOOLS},
            {"pkg/.github/skills/s/scripts": "../../../../tools"}, True, True),
        "a link in the directory that a followed link leads to": (
            {"skills/s/SKILL.md": SKILL, **TOOLS},
            {".claude/skills": "../skills", "skills/s/scripts": "../../tools"}, True, True),
        "a link in a part of a plugin": (
            {"plugins/p/.claude-plugin/plugin.json": '{"name": "p"}\n',
             "plugins/p/skills/s/SKILL.md": SKILL, **TOOLS},
            {"plugins/p/skills/s/scripts": "../../../../tools"}, True, True),
        "a link to a directory inside the copied directory": (
            {".claude/skills/s/SKILL.md": SKILL}, {".claude/alias": "skills"}, False, True),
        "a link in a vendored directory": (
            {".claude/settings.json": "{}\n", ".claude/node_modules/x/a.js": "x\n", **TOOLS},
            {".claude/node_modules/tools": "../../tools"}, False, False),
        "a link in a place of a tool's local state": (
            {".claude/settings.json": "{}\n", ".claude/worktrees/w/a.md": "x\n", **TOOLS},
            {".claude/worktrees/w/tools": "../../../tools"}, False, False),
        "a link to a directory outside every copied directory": (
            {".claude/settings.json": "{}\n", "src/a.py": "x\n", **TOOLS},
            {"src/tools": "../tools", "docs": "tools"}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}",
                                       heuristics.VENDORED_DIRS)
                    with mock.patch.object(paths, "directory_links", lambda *args: iter(())):
                        before = _collected(root, Path(tmp) / f"before{number}",
                                            heuristics.VENDORED_DIRS)
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(
                        code_versions.linked_directory_in_a_copied_directory(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_directories_of_the_collector(self):
        """Every directory of `paths._SHARED_DIRS` counts, at any depth, and so does a link that
        the step follows, at a tool directory or in a directory of `paths._SYMLINK_PARENTS`, or
        a file of a plugin or of a registration, wherever the link is."""
        for name in paths._SHARED_DIRS:
            for path in (f"{name}/x", f"pkg/{name}/a/x"):
                with self.subTest(path=path):
                    self.assertTrue(code_versions.linked_directory_in_a_copied_directory(
                        [("120000", path)]))
        anywhere = ("120000", "src/lib")
        followed = [("120000", name) for name in paths.TOOL_DIRECTORIES]
        followed += [("120000", parent) for parent in paths._SYMLINK_PARENTS
                     if "*" not in parent and "<" not in parent]
        files = [("100644", f"x/{name}") for name in (*paths._REGISTRATION_NAMES, "plugin.json",
                                                      "gemini-extension.json", "marketplace.json")]
        for entry in followed + files:
            with self.subTest(entry=entry):
                self.assertTrue(code_versions.linked_directory_in_a_copied_directory(
                    [anywhere, entry]))
        for entries in ([anywhere], [("120000", ".claude")], [("100644", ".claude/x")],
                        [("120000", "x.claude/a/b")], [("120000", "pkg/.github/workflows/x")]):
            with self.subTest(entries=entries):
                self.assertFalse(code_versions.linked_directory_in_a_copied_directory(entries))


class TestALinkAtANestedPlace(unittest.TestCase):
    """The reach of the change after which a place of a `nested:` row below the root can be a
    link (`paths.nested_places`), which the collector copies as its marker, with the directory
    that it leads to for a place of a folder. The collector before it is the same code with no
    link and no marker at a place. The predicate has to be true for every repository that the
    two collect differently."""

    AGENT = "---\ndescription: Reviews.\n---\nReview.\n"
    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LAYOUTS = {
        # dotnet/dotnet's src/msbuild/.github/copilot-instructions.md
        "an instructions file that links to the AGENTS.md beside its .github": (
            {"pkg/AGENTS.md": "Use pnpm.\n"},
            {"pkg/.github/copilot-instructions.md": "../AGENTS.md"}, True, True),
        "a .mcp.json below the root that links to a file": (
            {"shared/mcp.json": '{"servers": {}}\n', "pkg/a.py": "x\n"},
            {"pkg/.mcp.json": "../shared/mcp.json"}, True, True),
        # The cloning step copied such a link and its folder before, as a mechanism directory.
        "an agents folder below the root that links to a folder": (
            {"pkg/agents/a.agent.md": AGENT, "pkg/.github/copilot-instructions.md": "x\n"},
            {"pkg/.github/agents": "../agents"}, False, True),
        "a file at the place of a skills folder that names a folder": (
            {"pkg/skills/s/SKILL.md": SKILL, "pkg/.github/skills": "../skills\n"}, {}, True,
            True),
        "an instructions file below the root that links out of the repository": (
            {"pkg/a.py": "x\n"}, {"pkg/.github/copilot-instructions.md": "/etc/hosts"}, False,
            True),
        "the root's instructions file that is a link": (
            {"AGENTS.md": "# a\n"}, {".github/copilot-instructions.md": "../AGENTS.md"}, False,
            False),
        "an instructions file below the root": (
            {"pkg/.github/copilot-instructions.md": "Use pnpm.\n"}, {}, False, False),
    }

    @staticmethod
    def _no_link_is_a_place(root: Path, rel: str, directory: bool) -> bool:
        """The test of a place before the change: a directory or a regular file, no link."""
        path = root / rel
        return not path.is_symlink() and (path.is_dir() if directory else path.is_file())

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, links, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, links)
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "_nested_place", self._no_link_is_a_place):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.link_at_a_nested_place(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_places_of_the_nested_rows(self):
        """Every place of `paths.NESTED_ROWS` counts below the root, a link at the place of a
        file, and a link or a file at the place of a folder, with the first segment after
        `.github` in any case."""
        for heuristic_row in paths.NESTED_ROWS:
            place = heuristic_row["path"]
            folder = heuristics.is_directory_mode(heuristic_row["match_mode"])
            head, _, rest = place.partition("/")
            upper = f"{head}/{rest.upper()}" if rest else place
            with self.subTest(place=place):
                self.assertTrue(code_versions.link_at_a_nested_place([("120000", f"pkg/{place}")]))
                self.assertTrue(code_versions.link_at_a_nested_place([("120000", f"pkg/{upper}")]))
                self.assertEqual(code_versions.link_at_a_nested_place(
                    [("100644", f"pkg/{place}")]), folder)
                self.assertFalse(code_versions.link_at_a_nested_place([("120000", place)]))
                self.assertFalse(code_versions.link_at_a_nested_place(
                    [("100644", f"pkg/{place}/x")]))


class TestTheRuntimeImportsOfAnAgenticWorkflow(unittest.TestCase):
    """The reach of the change after which the cloning step copies the files that the runtime
    imports of a GitHub Agentic Workflow name (`paths.agentic_workflow`). The collector before
    it is the same code with a parser that finds no runtime import in any file. The predicate
    has to be true for every repository that the two collect differently."""

    WORKFLOW = "---\non: issues\nengine:\n  id: claude\n---\n{{#runtime-import %s}}\n"
    CLAUDE_ACTION = "jobs:\n  go:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
    LAYOUTS = {
        # shopware/shopware's .github/workflows/sw-triage.md
        "a workflow that loads a policy from .github/aw": (
            {".github/workflows/triage.md": WORKFLOW % ".github/aw/policy.md",
             ".github/aw/policy.md": "The policy.\n"}, True, True),
        # opsmill/infrahub's .github/workflows/issue-triage-labeler.md
        "a workflow that loads a file from .github/workflows/shared": (
            {".github/workflows/triage.md": WORKFLOW % "shared/taxonomy.md",
             ".github/workflows/shared/taxonomy.md": "The labels.\n"}, True, True),
        # When .github holds a Copilot path, the cloning step copies the whole directory, so the
        # collector after the change copies no further file.
        "a workflow beside a Copilot path": (
            {".github/workflows/triage.md": WORKFLOW % ".github/aw/policy.md",
             ".github/aw/policy.md": "The policy.\n",
             ".github/copilot-instructions.md": "Use pnpm.\n"}, False, True),
        "a workflow without a runtime import": (
            {".github/workflows/triage.md": "---\non: issues\nengine: claude\n---\nTriage.\n",
             ".github/aw/policy.md": "The policy.\n"}, False, True),
        "a workflow that runs an action": (
            {".github/workflows/review.yml": CLAUDE_ACTION,
             ".github/aw/policy.md": "The policy.\n"}, False, False),
        "no workflow": ({"AGENTS.md": "# a\n"}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, {})
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(paths, "_runtime_import_specs", lambda text: []):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.agentic_workflow_file(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_place_of_the_workflows(self):
        """A Markdown file directly in `paths.WORKFLOWS_SUBPATH` counts, with the suffix in any
        case, and a Markdown file elsewhere does not."""
        place = paths.WORKFLOWS_SUBPATH
        for path in (f"{place}/triage.md", f"{place}/TRIAGE.MD"):
            with self.subTest(path=path):
                self.assertTrue(code_versions.agentic_workflow_file([("100644", path)]))
        for path in (f"{place}/shared/engine.md", f"{place}/ci.yml", "pkg/.github/workflows/a.md",
                     ".github/aw/policy.md"):
            with self.subTest(path=path):
                self.assertFalse(code_versions.agentic_workflow_file([("100644", path)]))


class TestThePromptFileOfTheClaudeCodeBaseAction(unittest.TestCase):
    """The reach of the change after which the cloning step copies the file that the input
    `prompt_file` of the Claude Code Base Action names (`paths.action_prompt_files`). The
    collector before it is the same code that reads only the `prompt-file` of the Codex action.
    The predicate over the workflows that the earlier collection kept (`reuse.kept_workflows`)
    has to be true for every repository that the two collect differently."""

    @staticmethod
    def _step(action, key, value):
        return f"jobs:\n  a:\n    steps:\n      - uses: {action}\n        with:\n          {key}: {value}\n"

    LAYOUTS = {
        # facebook/rocksdb's ai-review-analysis.yml
        "the Base Action with a prompt file of the repository": (
            {".github/workflows/ai-review-analysis.yml": _step.__func__(
                "anthropics/claude-code-base-action@beta", "prompt_file",
                "claude_md/ci_recovery_prompt.md"),
             "claude_md/ci_recovery_prompt.md": "Recover the build.\n"}, True, True),
        "the folder base-action of the Claude Code Action": (
            {".github/workflows/triage.yml": _step.__func__(
                "anthropics/claude-code-action/base-action@v1", "prompt_file", "prompts/triage.md"),
             "prompts/triage.md": "Triage.\n"}, True, True),
        # facebookincubator/velox's claude-review.yml, which writes the prompt at run time.
        "the Base Action with a prompt file in /tmp": (
            {".github/workflows/claude-review.yml": _step.__func__(
                "anthropics/claude-code-base-action@beta", "prompt_file", "/tmp/prompt.txt")},
            False, True),
        # When .github holds a Copilot path, the cloning step copies the whole directory, so the
        # collector after the change copies no further file.
        "the Base Action with a prompt file beside a Copilot path": (
            {".github/workflows/triage.yml": _step.__func__(
                "anthropics/claude-code-base-action@beta", "prompt_file", ".github/prompts/t.md"),
             ".github/prompts/t.md": "Triage.\n",
             ".github/copilot-instructions.md": "Use pnpm.\n"}, False, True),
        "the Codex action with its prompt file": (
            {".github/workflows/review.yml": _step.__func__(
                "openai/codex-action@v1", "prompt-file", ".github/codex/prompts/review.md"),
             ".github/codex/prompts/review.md": "Review.\n"}, False, True),
        "the Base Action with an inline prompt": (
            {".github/workflows/review.yml": _step.__func__(
                "anthropics/claude-code-base-action@beta", "prompt", "Review the change.")},
            False, False),
        "no workflow": ({"AGENTS.md": "# a\n", "prompts/triage.md": "Triage.\n"}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_collects_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    lists = Path(tmp) / f"lists{number}"
                    root = lists / "o§r"
                    _commit_layout(root, files, {})
                    after = _collected(root, Path(tmp) / f"after{number}")
                    with mock.patch.dict(paths._PROMPT_FILE_INPUTS,
                                         {"openai/codex-action": "prompt-file"}, clear=True):
                        before = _collected(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    workflows = Path(tmp) / f"workflows{number}"
                    paths.copy_workflow_files(root, workflows / "o§r")
                    config = SimpleNamespace(file_lists_dir=lists, workflows_dir=workflows)
                    kept = reuse.kept_workflows(config, "o/r")
                    self.assertIsNotNone(kept)
                    self.assertEqual(code_versions.prompt_file_input(kept), reaches)
                    self.assertTrue(reaches or not differs)


def _imported(repo: Path, out: Path) -> dict:
    """{path: bytes} of what the copy of the imports writes from `repo` into `out`, with the
    list of the stubs, after the collector copied the importing files into `out/collected`."""
    collected, imported = out / "collected", out / "imported" / "o§r"
    paths.collect_repo(repo, collected, set())
    imports.collect_imports(repo, collected, imported)
    written = {rel: (imported / rel).read_bytes() for rel in paths.tree_files(imported)}
    stubs = imports.stub_list_path(imported)
    return {**written, "stubs": stubs.read_bytes() if stubs.exists() else b""}


class TestASkillThatAReviewMdIncludes(unittest.TestCase):
    """The reach of the change after which the copy of the imports follows an include of a root
    REVIEW.md to the SKILL.md of a folder in .claude/skills (`imports.code_review_include`) and
    compares each name of an included path case-sensitively. The code of the imports before it
    is the same code with a parser that finds no such include. The predicate has to be true for
    every repository that the two copy differently."""

    SKILL = "---\nname: s\ndescription: A skill.\n---\n"
    LAYOUTS = {
        "a skill that the root REVIEW.md includes": (
            {"REVIEW.md": "@.claude/skills/s/SKILL.md\n", ".claude/skills/s/SKILL.md": SKILL},
            True),
        "a skill too large for Code Review": (
            {"REVIEW.md": "@.claude/skills/s/SKILL.md\n",
             ".claude/skills/s/SKILL.md": SKILL + "x" * 30_000}, True),
        "a file that the root REVIEW.md includes": (
            {"REVIEW.md": "@rules.md\n", "rules.md": "Rules\n"}, False),
        "a skill that a REVIEW.md below the root names": (
            {"docs/REVIEW.md": "@.claude/skills/s/SKILL.md\n", "AGENTS.md": "Agents\n",
             ".claude/skills/s/SKILL.md": SKILL}, False),
        "a skill that a CLAUDE.md imports": (
            {"CLAUDE.md": "@.claude/skills/s/SKILL.md\n", ".claude/skills/s/SKILL.md": SKILL},
            False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_copies_differently(self):
        current = imports.code_review_include

        def earlier(text: str) -> list:
            return [each for each in current(text) if "/" not in each.path]

        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, {})
                    after = _imported(root, Path(tmp) / f"after{number}")
                    with mock.patch.dict(imports.PARSERS, {"code_review_include": earlier}):
                        before = _imported(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    if differs:
                        self.assertTrue(review_md_at_root(entries))
                    self.assertEqual(review_md_at_root(entries), "REVIEW.md" in files)

    def test_the_predicate_reads_the_importing_file_of_the_rows(self):
        """The root REVIEW.md in its case, and no REVIEW.md below the root or in another case,
        which no row of the syntax reads."""
        self.assertTrue(review_md_at_root([("100644", "REVIEW.md")]))
        self.assertTrue(review_md_at_root([("120000", "REVIEW.md")]))
        for path in ("docs/REVIEW.md", "review.md", "REVIEW.md.bak"):
            with self.subTest(path=path):
                self.assertFalse(review_md_at_root([("100644", path)]))


class TestARootFileInAnotherCase(unittest.TestCase):
    """The reach of the change after which a chain of Claude Code or Gemini CLI from a root
    file in another case opens its imports in any case, and a root CLAUDE file in any case keeps
    Claude Code from reading a root agents.md (`imports.ignores_case`). The same code with no
    row that ignores case stands for the code before the change, because both find the same
    files on the volume that compares case, where the cloning step runs. The predicate has to be
    true for every repository that the two copy differently."""

    LAYOUTS = {
        "a root claude.md that imports AGENTS.md in another case": (
            {"claude.md": "@AGENTS.MD\n", "AGENTS.md": "A\n"}, True, True),
        "a root gemini.md that imports a file in another case": (
            {"gemini.md": "@Notes.md\n", "notes.md": "N\n"}, True, True),
        "a root agents.md beside a root claude.md": (
            {"agents.md": "@notes\n", "claude.md": "C\n", "notes": "N\n"}, True, True),
        "a root AGENTS.md beside a root claude.md": (
            {"AGENTS.md": "@notes\n", "claude.md": "C\n", "notes": "N\n"}, False, True),
        "a root CLAUDE.md that imports a file in another case": (
            {"CLAUDE.md": "@Notes.md\n", "notes.md": "N\n"}, False, False),
    }

    def test_the_predicate_holds_for_every_repository_that_the_change_copies_differently(self):
        with tempfile.TemporaryDirectory() as tmp:
            for number, (name, (files, differs, reaches)) in enumerate(self.LAYOUTS.items()):
                with self.subTest(layout=name):
                    root = Path(tmp) / f"repo{number}"
                    entries = _commit_layout(root, files, {})
                    after = _imported(root, Path(tmp) / f"after{number}")
                    with mock.patch.object(imports, "ignores_case", lambda row: False):
                        before = _imported(root, Path(tmp) / f"before{number}")
                    self.assertEqual(after != before, differs, (sorted(before), sorted(after)))
                    self.assertEqual(code_versions.root_file_in_another_case(entries), reaches)
                    self.assertTrue(reaches or not differs)

    def test_the_predicate_reads_the_names_of_the_rows(self):
        """A root file in another case than CLAUDE.md, AGENTS.md or GEMINI.md, also a link, and
        no such file in the tools' own case or below the root."""
        for path in ("claude.md", "Agents.md", "AGENTS.MD", "GEMINI.MD"):
            with self.subTest(path=path):
                self.assertTrue(code_versions.root_file_in_another_case([("100644", path)]))
                self.assertTrue(code_versions.root_file_in_another_case([("120000", path)]))
        for path in ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "docs/claude.md", "claude.md.bak",
                     "CLAUDE.local.md"):
            with self.subTest(path=path):
                self.assertFalse(code_versions.root_file_in_another_case([("100644", path)]))


if __name__ == "__main__":
    unittest.main()
