"""Tests for agentcontext.reuse: what a repository was collected under, the path heuristics kept
for a later run, whether a change to them reaches a repository's file list, the removal of the
files of a repository that left the input list, and the waits of the head check while git
returns no head commit for too many heads. The tests of the decision over a whole
collection, with its head checks against local remotes, are in
tests.test_steps_repository_cloning.TestRunCollectionFinished.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_reuse -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentcontext import code_versions, heuristics, imports, reuse
from tests._fixtures import _init_git_repo, dsm_writes_during_removal, github_on_loopback

ROWS = heuristics.load_config_path_patterns()


def _row(path, match_mode="basename", tool="claude", mechanism="context_file"):
    return {"tool": tool, "mechanism": mechanism, "path": path, "match_mode": match_mode,
            "content_format": "markdown", "doc_url": "", "doc_verified": "", "evidence": "",
            "notes": ""}


def _kept(rows, vendored=("node_modules",), local_state=()) -> dict:
    return reuse.path_heuristics(rows, vendored, local_state=local_state)


def _files(*paths) -> list:
    return [("100644", path) for path in paths]


class TestThePathHeuristicsKept(unittest.TestCase):
    """The columns that decide what is collected, kept under their SHA-256."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_documentation_columns_do_not_count(self):
        """The weekly documentation watch changes `doc_verified`, which does not change what is
        collected."""
        read_again = [dict(row, doc_verified="2026-10-12", notes="Read again.") for row in ROWS]
        self.assertEqual(reuse.heuristics_sha(_kept(read_again)), reuse.heuristics_sha(_kept(ROWS)))
        changed = [dict(ROWS[0], path="ELSEWHERE.md"), *ROWS[1:]]
        self.assertNotEqual(reuse.heuristics_sha(_kept(changed)), reuse.heuristics_sha(_kept(ROWS)))

    def test_a_place_of_local_state_counts(self):
        """The collector leaves the places of the tools' local state out, and a changed place
        changes what it copies."""
        self.assertNotEqual(reuse.heuristics_sha(_kept(ROWS, local_state=(".claude/debug/",))),
                            reuse.heuristics_sha(_kept(ROWS)))
        self.assertIn(".claude/projects/-*/",
                      reuse.current_path_heuristics(set())["local_state"])

    def test_kept_heuristics_are_read_back_by_their_sha(self):
        sha = reuse.keep_heuristics(self.tmp, _kept(ROWS))
        self.assertEqual(reuse.read_heuristics(self.tmp, sha), _kept(ROWS))
        self.assertEqual(reuse.keep_heuristics(self.tmp, _kept(ROWS)), sha)
        self.assertEqual(len(list(self.tmp.iterdir())), 1)

    def test_a_kept_file_that_does_not_hold_its_name_is_none(self):
        sha = reuse.keep_heuristics(self.tmp, _kept(ROWS))
        (self.tmp / f"{sha}.json").write_text(json.dumps(_kept(ROWS[1:])))
        self.assertIsNone(reuse.read_heuristics(self.tmp, sha))
        self.assertIsNone(reuse.read_heuristics(self.tmp, "0" * 64))
        self.assertIsNone(reuse.read_heuristics(None, sha))


class TestTheCollectorsCode(unittest.TestCase):
    """The code that decides what a repository's collection contains."""

    def test_the_modules_that_only_schedule_the_run_are_left_out(self):
        names = {path.relative_to(reuse.PACKAGE_ROOT).as_posix()
                 for path in reuse.collector_files()}
        self.assertTrue({"collector.py", "paths.py", "git/clone.py", "git/history.py",
                         "commits.py"} <= names, names)
        self.assertFalse({"guards.py", "logs.py", "resume.py", "reuse.py",
                          "steps/repository_cloning.py", "cli.py"} & names, names)

    def test_the_code_of_the_imports_is_recorded_with_the_import_heuristics(self):
        """`imports.py` is outside the collector's code and inside the record of the import
        heuristics, because a change to it collects again only the repositories with a file
        that can import (`TestTheImportHeuristicsHeldAgainstTheFileList`)."""
        collector = {path.relative_to(reuse.PACKAGE_ROOT).as_posix()
                     for path in reuse.collector_files()}
        code = {path.relative_to(reuse.PACKAGE_ROOT).as_posix()
                for path in reuse.import_files()}
        self.assertNotIn("imports.py", collector)
        self.assertIn("imports.py", code)
        self.assertFalse(collector & code)
        self.assertEqual(reuse.import_heuristics([])["code"], reuse.import_code_fingerprint())

    def test_a_scheduling_change_keeps_the_fingerprint_and_a_collector_change_moves_it(self):
        """Every line of the list of finished repositories records the fingerprint, and a run
        takes over only the lines with the current one. A new image that changes only how a run
        resumes or stops must therefore keep the fingerprint, or the next run collects the
        sample again. Each edit is to a copy of the package."""
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "agentcontext"
            shutil.copytree(reuse.PACKAGE_ROOT, package,
                            ignore=shutil.ignore_patterns("__pycache__"))

            def fingerprint() -> str:
                return reuse.code_fingerprint(reuse.collector_files(package))

            def edit(relative: str, old: str, new: str) -> None:
                path = package / relative
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count(old), 1, old)
                path.write_text(text.replace(old, new), encoding="utf-8")

            before = fingerprint()
            # A statement of `run_collection`: how many repositories the pool is handed at once.
            edit("steps/repository_cloning.py", "window = workers\n", "window = workers + 1\n")
            self.assertEqual(fingerprint(), before)
            # A statement of `process_repo`: what the measures of a repository record.
            edit("collector.py", 'measures["clone_bytes"] = bounds.directory_size(tmp_dir)',
                 'measures["clone_bytes"] = 0')
            self.assertNotEqual(fingerprint(), before)

    def _copy(self, tmp: str) -> Path:
        package = Path(tmp) / "agentcontext"
        shutil.copytree(reuse.PACKAGE_ROOT, package, ignore=shutil.ignore_patterns("__pycache__"))
        return package

    def _edit(self, package: Path, relative: str, old: str, new: str) -> None:
        path = package / relative
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, old)
        path.write_text(text.replace(old, new), encoding="utf-8")

    def test_a_reworded_exception_text_keeps_the_fingerprint(self):
        """An exception text of the collector's code reaches no collected file, and rewording it
        must not make the next run collect the sample again. A value that the exception carries
        to its caller, such as the cause of `HistoryUnavailable`, which the per-repository commit
        record stores, moves the fingerprint. Each edit is to a copy of the package."""
        with tempfile.TemporaryDirectory() as tmp:
            package = self._copy(tmp)
            before = reuse.collector_fingerprint(package)
            self._edit(package, "git/clone.py", '"Could not read the branch, the head commit',
                       '"Git read no branch, no head commit')
            self._edit(package, "collector.py", 'f"no history for {len(collected)} files"',
                       'f"the history of {len(collected)} files was not fetched"')
            self.assertEqual(reuse.collector_fingerprint(package), before)
            self._edit(package, "collector.py",
                       'HistoryUnavailable(extractor.history_failure or "exit",',
                       'HistoryUnavailable(extractor.history_failure or "failed",')
            self.assertNotEqual(reuse.collector_fingerprint(package), before)

    def test_the_init_files_of_the_packages_are_no_collector_code(self):
        """`agentcontext/__init__.py` names the data root and the directories of the stages,
        which the collector receives in its configuration, and `git/__init__.py` documents the
        git modules. An edit to either keeps the fingerprints of the collector's code and of the
        code of the imports."""
        with tempfile.TemporaryDirectory() as tmp:
            package = self._copy(tmp)
            before = (reuse.collector_fingerprint(package), reuse.import_code_fingerprint(package))
            self._edit(package, "__init__.py", 'SAMPLING_DIR = DATA_ROOT / "sampling"',
                       'SAMPLING_DIR = DATA_ROOT / "1_sampling"')
            (package / "git" / "__init__.py").write_text('"""The git modules."""\nimport os\n',
                                                         encoding="utf-8")
            self.assertEqual((reuse.collector_fingerprint(package),
                              reuse.import_code_fingerprint(package)), before)

    def test_the_archive_step_and_the_command_line_are_no_code_of_the_imports(self):
        """Every line records the fingerprint of the code of the imports with the import
        heuristics, and a run collects again every repository with a file that can import when
        it differs. The archive step and the command line that it imports decide nothing of
        what the imported tree holds, and an edit to either keeps the fingerprint. An
        edit to a parser or to the paths that the archive step prunes moves it. Each edit is to a
        copy of the package."""
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "agentcontext"
            shutil.copytree(reuse.PACKAGE_ROOT, package,
                            ignore=shutil.ignore_patterns("__pycache__"))

            def code() -> str:
                return reuse.import_heuristics([], package)["code"]

            def edit(relative: str, old: str, new: str) -> None:
                path = package / relative
                text = path.read_text(encoding="utf-8")
                self.assertEqual(text.count(old), 1, old)
                path.write_text(text.replace(old, new), encoding="utf-8")

            before = code()
            # The exit code of a usage error of the command line.
            edit("cli.py", "USAGE_ERROR = 1\n", "USAGE_ERROR = 3\n")
            self.assertEqual(code(), before)
            # Where the archive step copies the validated tree before it prunes the copy.
            edit("steps/collected_files_archive.py", 'STAGING_DIR = "repos_dataset_staging"',
                 'STAGING_DIR = "staging"')
            self.assertEqual(code(), before)
            # The characters that Copilot CLI drops from the end of an import.
            edit("imports.py", '_COPILOT_TRAILING = ".,;:)!?]}"', '_COPILOT_TRAILING = ".,;:)!?]"')
            self.assertNotEqual(code(), before)
            # A path that the archive step prunes, which the copy of the imports leaves out.
            parser_changed = code()
            edit("prune.py", '"**/*.log",', '"**/*.log", "**/*.bak",')
            self.assertNotEqual(code(), parser_changed)


    def test_the_published_side_of_the_planning_documents_is_no_collector_code(self):
        """The columns of `planning-documents.csv` and the layouts left out of the planning
        documents are read after the collection. Within the collector's code, a new column or a
        new reason would move the fingerprint, and the next run would collect every repository
        in the sample again although none would be copied differently."""
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "agentcontext"
            shutil.copytree(reuse.PACKAGE_ROOT, package,
                            ignore=shutil.ignore_patterns("__pycache__"))
            before = reuse.code_fingerprint(reuse.collector_files(package))
            schema = package / "planning_schema.py"
            text = schema.read_text(encoding="utf-8")
            for old, new in (('"line_count"]', '"line_count", "word_count"]'),
                             ('"not settled")', '"not settled", "superseded")')):
                self.assertEqual(text.count(old), 1, old)
                text = text.replace(old, new)
            schema.write_text(text, encoding="utf-8")
            self.assertEqual(reuse.code_fingerprint(reuse.collector_files(package)), before)
            self.assertIn(package / "planning.py", reuse.collector_files(package),
                          "the matching that decides what the cloning step copies is its code")


class TestALineThatTheTimeBoundKept(unittest.TestCase):
    """An ok line that kept its earlier collection after the time bound stopped its retry in
    this collection stays as it is while the bound holds, whatever import heuristics it records.
    The cloning step keeps such a collection after the time bound whatever they are
    (`preserved_on`). Testing them here would clone the repository again in every retry round
    and on every resumed Start, each time up to the bound."""

    def test_other_import_heuristics_do_not_clone_it_again(self):
        from agentcontext import collector, resume
        from agentcontext.git import clone
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            lists = tmp / "file_lists"
            lists.mkdir()
            clone.write_file_list(
                b"100644 blob 0000000000000000000000000000000000000000\tCLAUDE.md\0"
                b"100644 blob 0000000000000000000000000000000000000000\tsrc/main.c\0",
                lists / f"big§repo{clone.FILE_LIST_SUFFIX}")
            config = collector.CollectionConfig(
                collected_dir=tmp / "collected", clone_dir=tmp / "clones", log_dir=tmp / "logs",
                file_lists_dir=lists, imported_dir=tmp / "imported",
                vendored_dirs=heuristics.load_vendored_dirs(heuristics.VENDORED_DIRS_PATH),
                heuristics_dir=tmp / reuse.HEURISTICS_DIR_NAME, clone_timeout=3600)
            key = reuse.current(config)
            # The import heuristics of an earlier month: the same rows under other code.
            earlier = dict(reuse.import_heuristics(imports.load_import_patterns()), code="0" * 64)
            under = dict(key, imports=reuse.keep_heuristics(config.heuristics_dir, earlier))
            finished = tmp / "finished_repositories.jsonl"
            row = ("big/repo,2026-10-01T00:00:00Z,main," + "a" * 40 + ",2026-09-30T00:00:00Z,,"
                   + ",".join(["FALSE"] * 11))
            resume.append_finished(finished, "big/repo", row, None, [], [], False, None, {},
                                   under, time_bound=3600, collection="C1")
            done = resume.read_finished(finished)
            self.assertTrue(resume.time_bound_holds(done["big/repo"][9], 3600,
                                                    done["big/repo"][10], "C1"))
            again = reuse.outdated(config, ["big/repo"], done, set(), key, check_heads=False,
                                   collection={"collection": "C1", "taken_over": set()})
            self.assertEqual(again, set())


class TestALineOfAnEarlierVersionOfTheCollector(unittest.TestCase):
    """A line that an earlier recorded version of the collector's code collected is taken over
    where no change since that version reaches the repository's file list, and collected again
    where a change reaches it, where the repository has no file list for a change that needs
    one, and where the line's version is not recorded."""

    EARLIER, CURRENT, UNRECORDED = "a" * 64, "b" * 64, "c" * 64

    def setUp(self):
        from agentcontext.git import clone
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = SimpleNamespace(file_lists_dir=self.tmp / "lists",
                                      heuristics_dir=self.tmp / "kept",
                                      collected_dir=self.tmp / "collected", clone_timeout=0,
                                      max_clone_failures=0, token=None, use_ssh=False,
                                      vendored_dirs=heuristics.load_vendored_dirs())
        self.config.file_lists_dir.mkdir()
        listings = {"o§linked": b"120000 blob 0\t.claude/skills/s/SKILL.md\0",
                    "o§plain": b"100644 blob 0\t.claude/skills/s/SKILL.md\0",
                    "o§unrecorded": b"100644 blob 0\tCLAUDE.md\0"}
        for name, listing in listings.items():
            clone.write_file_list(listing,
                                  self.config.file_lists_dir / f"{name}{clone.FILE_LIST_SUFFIX}")
        self.key = {"collector": self.CURRENT, "heuristics": "path heuristics of both runs",
                    "imports": "import heuristics of both runs", "records": {},
                    "git": "git version 2"}
        row = "o/r,2026-10-01T00:00:00Z,main," + "a" * 40 + ",2026-09-30T00:00:00Z,"
        self.done = {repo: (row, None, [], [], False, None,
                            dict(self.key, collector=self.UNRECORDED if repo == "o/unrecorded"
                                 else self.EARLIER),
                            False, False, None, None)
                     for repo in ("o/linked", "o/plain", "o/unrecorded", "o/without_list")}

    def _again(self, reach) -> tuple:
        record = (code_versions.Version(self.EARLIER, "c1", "the earlier version", None),
                  code_versions.Version(self.CURRENT, "c2", "the change", reach))
        err = io.StringIO()
        with mock.patch.object(code_versions, "COLLECTOR", record), \
                contextlib.redirect_stderr(err):
            again = reuse.outdated(self.config, sorted(self.done), self.done, set(), self.key,
                                   check_heads=False)
        return again, err.getvalue()

    def test_a_change_with_a_predicate_collects_again_only_what_it_reaches(self):
        again, said = self._again(code_versions.linked_skill_file)
        self.assertEqual(again, {"o/linked", "o/unrecorded", "o/without_list"})
        self.assertIn("2 with changes of the collector's code that reach their files", said)
        self.assertIn("1 collected by code of an unrecorded version", said)

    def test_a_change_that_reaches_no_repository_collects_again_only_the_repository_of_an_unrecorded_version(self):
        self.assertEqual(self._again(code_versions.NO_REPOSITORY)[0], {"o/unrecorded"})

    def test_a_change_that_reaches_every_repository_collects_every_one_again(self):
        self.assertEqual(self._again(code_versions.EVERY_REPOSITORY)[0], set(self.done))


class TestALineOfTheCollectorBeforeTheDatingOfPathsAddedAgain(unittest.TestCase):
    """A change of `history.PathCommits.first` dates a path added again from the commit that
    first added it. The change reaches the repositories with a path that has a commit older
    than its recorded creation commit, which the per-repository commit record shows
    (`code_versions.path_added_again`). A line of the version before it is collected
    again where the record shows such a path, and where a collection that copied a file has no
    record, no commit log beside it, or a record of another commit. It is taken over where the
    record shows no such path, and where the collection copied no file and therefore keeps no
    record."""

    HEAD = "c" * 40

    def setUp(self):
        from agentcontext import commits
        from agentcontext.git import clone
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = SimpleNamespace(file_lists_dir=self.tmp / "lists",
                                      workflows_dir=self.tmp / "workflows",
                                      heuristics_dir=self.tmp / "kept",
                                      collected_dir=self.tmp / "collected",
                                      commits_dir=self.tmp / "collect_commits", clone_timeout=0,
                                      max_clone_failures=0, token=None, use_ssh=False,
                                      vendored_dirs=heuristics.load_vendored_dirs())
        for directory in (self.config.file_lists_dir, self.config.commits_dir):
            directory.mkdir()
        added_again = {"artifact": "AGENTS.md", "first": "b" * 40, "commits": ["a" * 40, "b" * 40]}
        plain = {"artifact": "AGENTS.md", "first": "a" * 40, "commits": ["a" * 40, "b" * 40]}
        # Each repository: the path histories and the head of its record, or None for both
        # without a record, whether the commit log is kept, and whether the collection copied a
        # file.
        layouts = {"o/added_again": ([added_again], self.HEAD, True, True),
                   "o/plain": ([plain], self.HEAD, True, True),
                   "o/no_record": (None, None, False, True),
                   "o/no_log": ([plain], self.HEAD, False, True),
                   "o/other_commit": ([plain], "d" * 40, True, True),
                   "o/nothing_copied": (None, None, False, False)}
        for repo, (path_commits, head, logged, copied) in layouts.items():
            name = repo.replace("/", "§")
            clone.write_file_list(b"100644 blob 0\tAGENTS.md\0",
                                  self.config.file_lists_dir / f"{name}{clone.FILE_LIST_SUFFIX}")
            if copied:
                (self.config.collected_dir / name).mkdir(parents=True)
                (self.config.collected_dir / name / "AGENTS.md").write_text("# a\n")
            if path_commits is not None:
                record = {"repo_name": repo, "head_sha": head, "outcome": "history_read",
                          "history_failed": False, "path_commits": path_commits}
                commits.shard_paths(self.config.commits_dir, repo)[1].write_text(
                    json.dumps(record), encoding="utf-8")
            if logged:
                commits.stored_log_path(self.config.commits_dir, repo).write_bytes(b"")
        versions = [version.fingerprint for version in code_versions.COLLECTOR]
        fix = next(i for i, version in enumerate(code_versions.COLLECTOR)
                   if version.commits.startswith("Date a path added again"))
        self.key = {"collector": versions[-1], "heuristics": "path heuristics of both runs",
                    "imports": "import heuristics of both runs", "records": {},
                    "git": "git version 2"}
        row = "o/r,2026-10-01T00:00:00Z,main," + self.HEAD + ",2026-09-30T00:00:00Z,"
        self.done = {repo: (row, None, [], [], False, None,
                            dict(self.key, collector=versions[fix - 1]), False, False, None, None)
                     for repo in layouts}

    def test_a_repository_is_collected_again_only_where_its_record_shows_a_path_added_again_or_cannot_show_it(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            again = reuse.outdated(self.config, sorted(self.done), self.done, set(), self.key,
                                   check_heads=False)
        self.assertEqual(again, {"o/added_again", "o/no_record", "o/no_log", "o/other_commit"})
        self.assertIn("4 with changes of the collector's code that reach their files",
                      err.getvalue())


class TestALineOfTheCollectorBeforeThePromptFileOfTheBaseAction(unittest.TestCase):
    """A change of `paths.action_prompt_files` copies the file that the input `prompt_file` of
    the Claude Code Base Action names. The change reaches the repositories with a kept workflow
    that runs such an action with that input (`code_versions.prompt_file_input`). A line of the
    version before it is collected again where a kept workflow does, and where a workflow file
    that the file list lists is not kept, such as a file after the first
    `paths.MAX_WORKFLOW_FILES`. It is taken over where no kept workflow does, where the file list
    lists no workflow file, and where the only workflow file is a link, which the collector reads
    no prompt file from."""

    HEAD = "c" * 40
    BASE_ACTION = ("jobs:\n  recover:\n    steps:\n"
                   "      - uses: anthropics/claude-code-base-action@beta\n        with:\n"
                   "          prompt_file: claude_md/ci_recovery_prompt.md\n")
    CLAUDE_ACTION = ("jobs:\n  a:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
                     "        with:\n          prompt: Review the change.\n")

    def setUp(self):
        from agentcontext.git import clone
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = SimpleNamespace(file_lists_dir=self.tmp / "lists",
                                      workflows_dir=self.tmp / "workflows",
                                      heuristics_dir=self.tmp / "kept",
                                      collected_dir=self.tmp / "collected", clone_timeout=0,
                                      max_clone_failures=0, token=None, use_ssh=False,
                                      vendored_dirs=heuristics.load_vendored_dirs())
        self.config.file_lists_dir.mkdir()
        # Each repository: its listed workflow files with their modes, and the kept ones.
        layouts = {"o/base_action": ({"ai-review.yml": "100644", "ci.yml": "100644"},
                                     {"ai-review.yml": self.BASE_ACTION, "ci.yml": "jobs: {}\n"}),
                   "o/claude_action": ({"claude.yml": "100644"}, {"claude.yml": self.CLAUDE_ACTION}),
                   "o/not_kept": ({"claude.yml": "100644", "late.yml": "100644"},
                                  {"claude.yml": self.CLAUDE_ACTION}),
                   "o/link": ({"claude.yml": "120000"}, {}),
                   "o/no_workflow": ({}, {})}
        for repo, (listed, kept) in layouts.items():
            name = repo.replace("/", "§")
            listing = b"100644 blob 0\tCLAUDE.md\0" + b"".join(
                f"{mode} blob 0\t.github/workflows/{file}\0".encode() for file, mode in listed.items())
            clone.write_file_list(listing,
                                  self.config.file_lists_dir / f"{name}{clone.FILE_LIST_SUFFIX}")
            for file, text in kept.items():
                (self.config.workflows_dir / name).mkdir(parents=True, exist_ok=True)
                (self.config.workflows_dir / name / file).write_text(text, encoding="utf-8")
        versions = [version.fingerprint for version in code_versions.COLLECTOR]
        change = next(i for i, version in enumerate(code_versions.COLLECTOR)
                      if version.commits.startswith("Copy the prompt file that a workflow gives"))
        self.key = {"collector": versions[-1], "heuristics": "path heuristics of both runs",
                    "imports": "import heuristics of both runs", "records": {},
                    "git": "git version 2"}
        row = "o/r,2026-10-01T00:00:00Z,main," + self.HEAD + ",2026-09-30T00:00:00Z,"
        self.done = {repo: (row, None, [], [], False, None,
                            dict(self.key, collector=versions[change - 1]), False, False, None,
                            None)
                     for repo in layouts}

    def test_a_repository_is_collected_again_only_where_a_kept_workflow_names_a_prompt_file_or_a_listed_workflow_is_not_kept(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            again = reuse.outdated(self.config, sorted(self.done), self.done, set(), self.key,
                                   check_heads=False)
        self.assertEqual(again, {"o/base_action", "o/not_kept"})
        self.assertIn("2 with changes of the collector's code that reach their files",
                      err.getvalue())

    def test_the_kept_workflows(self):
        """`{file name: text}` of each listed workflow file that is not a link, an empty dict where
        the file list lists no such file, and None where a listed one is not kept or the run keeps
        no workflow files."""
        self.assertEqual(reuse.kept_workflows(self.config, "o/base_action"),
                         {"ai-review.yml": self.BASE_ACTION, "ci.yml": "jobs: {}\n"})
        self.assertEqual(reuse.kept_workflows(self.config, "o/no_workflow"), {})
        self.assertEqual(reuse.kept_workflows(self.config, "o/link"), {})
        self.assertIsNone(reuse.kept_workflows(self.config, "o/not_kept"))
        self.config.workflows_dir = None
        self.assertIsNone(reuse.kept_workflows(self.config, "o/claude_action"))


class TestAFileListThatCannotBeRead(unittest.TestCase):
    """A file list is read before any clone whenever the path heuristics changed, and a list
    that is truncated, empty or not xz at all therefore has to count as no list rather than end
    every Start."""

    def test_a_damaged_list_is_no_list(self):
        import lzma
        from types import SimpleNamespace
        from agentcontext.git import clone
        with tempfile.TemporaryDirectory() as tmp:
            config = SimpleNamespace(file_lists_dir=Path(tmp))
            path = Path(tmp) / f"o§a{clone.FILE_LIST_SUFFIX}"
            whole = lzma.compress(b"100644 blob abc\tCLAUDE.md\0")
            for name, content in (("truncated", whole[:len(whole) // 2]), ("empty", b""),
                                  ("not xz", b"plain text")):
                with self.subTest(case=name):
                    path.write_bytes(content)
                    self.assertIsNone(reuse.file_list(config, "o/a"))


class TestTheFilesOfARepositoryThatLeft(unittest.TestCase):
    """`remove_left` runs after the whole collection, and a directory that cannot be removed
    has to stay with a warning rather than end the step. The step-level case, with the
    line marked to redo, is in tests.test_steps_repository_cloning.TestMainCli."""

    def _config(self, root: Path) -> SimpleNamespace:
        config = SimpleNamespace(collected_dir=root / "collected", commits_dir=None,
                                 file_lists_dir=None, log_dir=root / "clone_logs",
                                 workflows_dir=root / "workflows",
                                 imported_dir=root / "imported")
        for directory in self._trees(config):
            (directory / "docs").mkdir(parents=True)
            (directory / "docs" / "CLAUDE.md").write_text("x\n")
        return config

    def _trees(self, config) -> tuple:
        """The repository's directories of the collected tree, the workflow files and the
        imported tree."""
        return (config.collected_dir / "o§a", config.workflows_dir / "o§a",
                config.imported_dir / "o§a")

    def test_a_directory_that_cannot_be_removed_stays_with_a_warning(self):
        """Another entry written into it during the removal, which DSM did not write."""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            err = io.StringIO()
            with dsm_writes_during_removal(entry="notes.md", times=3), \
                    contextlib.redirect_stderr(err):
                self.assertEqual(reuse.remove_left(config, ["o/b"], {}), 1)
            for directory in self._trees(config):
                with self.subTest(directory=directory.parent.name):
                    self.assertTrue((directory / "docs" / "notes.md").is_file())
                    self.assertIn(f"Removing the directory {directory} of o/a raised",
                                  err.getvalue())
            self.assertNotIn("Removed the files of", err.getvalue())

    def test_what_dsm_writes_during_the_removal_is_removed_with_the_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            err = io.StringIO()
            with dsm_writes_during_removal(times=3) as written, contextlib.redirect_stderr(err):
                self.assertEqual(reuse.remove_left(config, ["o/b"], {}), 1)
            self.assertEqual(len(written), 3)
            for directory in self._trees(config):
                with self.subTest(directory=directory.parent.name):
                    self.assertFalse(directory.exists())
            self.assertIn("Removed the files of 1 repository that is not in the input list",
                          err.getvalue())

    def test_the_list_of_stubs_leaves_with_the_imported_files(self):
        """The list of a repository's stubs lies beside its directory of the imported tree,
        and stays behind when only the directory is removed."""
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(Path(tmp))
            stubs = imports.stub_list_path(config.imported_dir / "o§a")
            stubs.write_text('["docs/big.md"]', encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                reuse.remove_left(config, ["o/b"], {})
            self.assertFalse(stubs.exists())

    def test_the_log_counts_the_repositories_whose_files_this_run_removed(self):
        """The list of finished repositories keeps the line of a repository that left in an
        earlier run after its files are gone, here `o/gone`. Counted with the others, every
        later run would log that it removed the files of every repository that ever left the
        input list. `o/c` left with its per-repository commit record alone."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = self._config(root)
            config.commits_dir = root / "commits"
            config.commits_dir.mkdir()
            (config.commits_dir / "o§c.json").write_text("{}\n")
            done = {"o/gone": (), "o/c": ()}
            for run, left, expected in (
                    (1, 3, "Removed the files of 2 repositories that are not in the input list"),
                    (2, 2, None)):
                with self.subTest(run=run):
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        self.assertEqual(reuse.remove_left(config, ["o/b"], done), left)
                    if expected:
                        self.assertIn(expected, err.getvalue())
                    else:
                        self.assertNotIn("Removed the files of", err.getvalue())
            self.assertFalse((config.collected_dir / "o§a").exists())
            self.assertFalse((config.imported_dir / "o§a").exists())
            self.assertFalse((config.commits_dir / "o§c.json").exists())


class TestAffected(unittest.TestCase):
    """A change reaches a repository when a path of its file list contains a changed row's path."""

    def test_the_same_heuristics_reach_nothing(self):
        self.assertFalse(reuse.affected(_files("CLAUDE.md", ".claude/settings.json"),
                                        _kept(ROWS), _kept(ROWS)))

    def test_a_new_row_reaches_a_repository_with_its_path_anywhere_and_in_any_case(self):
        new = _kept([*ROWS, _row("STYLE.md")])
        self.assertTrue(reuse.affected(_files("docs/style.md"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files("docs/styles.md", "STYLE.md.bak"), _kept(ROWS), new))

    def test_a_removed_row_reaches_a_repository_with_its_path(self):
        removed = [row for row in ROWS if row["path"] != "GEMINI.md"]
        self.assertTrue(reuse.affected(_files("GEMINI.md"), _kept(ROWS), _kept(removed)))
        self.assertFalse(reuse.affected(_files("README.md"), _kept(ROWS), _kept(removed)))

    def test_a_row_of_a_tool_directory_reaches_a_repository_that_links_the_directory(self):
        """LedgerHQ/device-sdk-ts links `.claude` to `agent-files/claude` and `.cursor` to
        `agent-files/cursor`, and the collector copies the commands and the rules there through
        the links. The file list shows each link as an entry of the tool directory's name. A
        changed local-state place of a tool directory reaches such a repository too."""
        links = _files("agent-files/claude/commands/review.md", "agent-files/cursor/rules/a.mdc")
        links += [("120000", ".claude"), ("120000", ".cursor")]
        for changed in (_row(".claude/commands", "dir_ext:.md", "claude", "skills"),
                        _row(".cursor/rules", "dir_ext:.mdc", "cursor", "rules")):
            with self.subTest(path=changed["path"]):
                new = _kept([*ROWS, changed])
                self.assertTrue(reuse.affected(links, _kept(ROWS), new))
                self.assertFalse(reuse.affected(_files("agent-files/claude/commands/review.md"),
                                                _kept(ROWS), new))
        self.assertTrue(reuse.affected(links, _kept(ROWS),
                                       _kept(ROWS, local_state=(".claude/shell-snapshots/",))))

    def test_a_directory_row_reaches_every_file_under_it(self):
        new = _kept([*ROWS, _row(".newtool/commands", "dir_ext:.md", "cursor", "commands")])
        self.assertTrue(reuse.affected(_files("src/.newtool/commands/deploy.md"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files(".newtool/settings.json"), _kept(ROWS), new))

    def test_a_row_of_a_plugin_reaches_every_repository_with_a_configuration_path(self):
        """A manifest can name the plugin's parts anywhere, and the part's own path
        does not show where the part is. A plugin is found by a path that another row names."""
        new = _kept([*ROWS, _row("<claude-plugin>/styles", "dir_ext:.md", "claude", "rules")])
        self.assertTrue(reuse.affected(_files("tools/.claude-plugin/plugin.json",
                                              "tools/my-styles/a.md"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files("src/main.c", "Makefile"), _kept(ROWS), new))

    def test_a_row_of_a_plugin_s_manifest_reaches_only_a_repository_with_a_file_of_its_name(self):
        """A new manifest row decides only whether a file of its name is a plugin, so the collector
        copies the same files from a repository without such a file."""
        new = _kept([*ROWS, _row("<copilot-plugin>/plugin.json", "file", "copilot", "plugins")])
        self.assertTrue(reuse.affected(_files("plugins/mt-migration/plugin.json",
                                              ".github/copilot-instructions.md"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files(".github/copilot-instructions.md",
                                               "tools/.claude-plugin/marketplace.json"),
                                        _kept(ROWS), new))

    def test_a_row_of_an_antigravity_plugin_s_parts_reaches_a_repository_with_a_plugin_json(self):
        """Every Antigravity plugin has a plugin.json, so a repository without one has no part
        that a changed row of such a plugin can name."""
        new = _kept([*ROWS, _row("<antigravity-plugin>/skills", "manifest_field:skills:adds",
                                 "gemini", "skills")])
        self.assertTrue(reuse.affected(_files(".agents/plugins/p/plugin.json",
                                              "docs/skills/a/SKILL.md"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files("GEMINI.md", ".gemini/settings.json"),
                                        _kept(ROWS), new))

    def test_a_vendored_directory_reaches_a_repository_with_a_directory_of_its_name(self):
        new = _kept(ROWS, vendored=("node_modules", "third_party"))
        self.assertTrue(reuse.affected(_files("third_party/x/CLAUDE.md"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files("src/third_party.c"), _kept(ROWS), new))

    def test_a_place_of_local_state_reaches_a_repository_with_a_path_in_it(self):
        new = _kept(ROWS, local_state=(".claude/projects/-*/", ".codex/history.jsonl"))
        self.assertTrue(reuse.affected(_files(".claude/projects/-Users-a-r/x.jsonl"),
                                       _kept(ROWS), new))
        self.assertTrue(reuse.affected(_files("pkg/.codex/history.jsonl"), _kept(ROWS), new))
        self.assertFalse(reuse.affected(_files(".claude/projects/js-to-ts/AGENT.md",
                                               ".codex/history.json"), _kept(ROWS), new))

    def test_a_kept_version_from_before_the_places_counts_each_place_as_new(self):
        old = _kept(ROWS)
        del old["local_state"]
        new = _kept(ROWS, local_state=(".claude/scheduled_tasks.lock",))
        self.assertTrue(reuse.affected(_files(".claude/scheduled_tasks.lock"), old, new))
        self.assertFalse(reuse.affected(_files(".claude/settings.json"), old, new))

    def test_the_same_rows_in_another_order_reach_every_repository(self):
        self.assertTrue(reuse.affected(_files("src/main.c"), _kept(ROWS),
                                       _kept([*ROWS[1:], ROWS[0]])))


class TestTheReachIsKeptForTheRun(unittest.TestCase):
    """A run decides once whether changed path heuristics reach a repository. Its retry rounds
    and its test of whether a failed retry keeps the earlier collection take the decision for
    the same line and the same file list rather than matching the file list again. A line, a
    file list or path heuristics that change during the run are decided again."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = SimpleNamespace(file_lists_dir=self.tmp / "lists",
                                      heuristics_dir=self.tmp / "kept",
                                      collected_dir=self.tmp / "collected")
        self.list_files("CLAUDE.md", "src/a.c")
        self.new = _kept(ROWS)
        self.key = {"heuristics": reuse.keep_heuristics(self.config.heuristics_dir, self.new),
                    "imports": "import heuristics of both runs"}
        self.under = self.line(_kept([row for row in ROWS if row["path"] != "GEMINI.md"]))

    def list_files(self, *paths) -> None:
        """Write the file list of o/a as the cloning step writes it."""
        from agentcontext.git import clone
        clone.write_file_list(b"".join(b"100644 blob abc\t" + path.encode() + b"\0"
                                       for path in paths),
                              self.config.file_lists_dir / f"o§a{clone.FILE_LIST_SUFFIX}")

    def line(self, kept: dict) -> dict:
        """The `collected_under` of a line collected under the path heuristics `kept`."""
        return {"heuristics": reuse.keep_heuristics(self.config.heuristics_dir, kept),
                "imports": self.key["imports"]}

    def reach(self, under: dict = None, new: dict = None, imports_too: bool = True) -> bool:
        return reuse.heuristics_reach(self.config, "o/a", under or self.under, self.key,
                                      new or self.new, imports_too)

    def test_the_rounds_and_the_test_of_a_failed_retry_match_the_file_list_once(self):
        with mock.patch.object(reuse, "affected", wraps=reuse.affected) as matched:
            self.assertFalse(self.reach(), "the decision to take the repository over")
            self.assertFalse(self.reach(), "a retry round")
            self.assertFalse(self.reach(imports_too=False), "the test of a failed retry")
        self.assertEqual(matched.call_count, 1)

    def test_a_file_list_that_changes_during_the_run_is_matched_again(self):
        self.assertFalse(self.reach())
        self.list_files("CLAUDE.md", "GEMINI.md")
        self.assertTrue(self.reach())
        self.assertTrue(self.reach(imports_too=False))

    def test_a_line_that_changes_during_the_run_is_decided_again(self):
        self.assertFalse(self.reach())
        other = self.line(_kept([row for row in ROWS if row["path"] != "CLAUDE.md"]))
        self.assertTrue(self.reach(under=other))

    def test_path_heuristics_that_change_during_the_run_are_decided_again(self):
        self.assertFalse(self.reach())
        self.assertTrue(self.reach(new=_kept([row for row in ROWS if row["path"] != "GEMINI.md"]
                                             + [_row("a.c")])))


    def test_a_collected_tree_that_gains_a_file_during_the_run_is_decided_again(self):
        """Other import heuristics reach no repository from which the collection copied no
        file, and the decision changes when the collected tree gains one."""
        rows = imports.load_import_patterns()
        earlier = dict(reuse.import_heuristics(rows), code="0" * 64)
        key = dict(self.key, imports=reuse.keep_heuristics(self.config.heuristics_dir,
                                                           reuse.import_heuristics(rows)))
        under = dict(self.under, heuristics=key["heuristics"],
                     imports=reuse.keep_heuristics(self.config.heuristics_dir, earlier))
        decide = lambda: reuse.heuristics_reach(self.config, "o/a", under, key, self.new)
        self.assertFalse(decide(), "nothing copied, so nothing to import from")
        tree = self.config.collected_dir / "o§a"
        tree.mkdir(parents=True)
        (tree / "CLAUDE.md").write_text("# Claude\n", encoding="utf-8")
        self.assertTrue(decide())


class TestTheImportHeuristicsHeldAgainstTheFileList(unittest.TestCase):
    """A changed import row makes the step collect a repository again where a path of its file
    list contains the row's importing file, as a changed path pattern does, and takes the
    repository over otherwise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = SimpleNamespace(file_lists_dir=self.tmp / "lists",
                                      heuristics_dir=self.tmp / "kept",
                                      collected_dir=self.tmp / "collected")
        self.config.file_lists_dir.mkdir()
        self._list("o§claude", ("CLAUDE.md", "src/a.c"), collected=("CLAUDE.md",))
        self._list("o§other", ("src/a.c",))
        self.rows = imports.load_import_patterns()
        self.under = self._key(self.rows)

    def _list(self, repo: str, paths: tuple, collected: tuple = ()) -> None:
        """The file list of `repo` with `paths`, and its directory of the collected tree with
        `collected`, the files that the collection copied."""
        import lzma
        from agentcontext.git import clone
        (self.config.file_lists_dir / f"{repo}{clone.FILE_LIST_SUFFIX}").write_bytes(
            lzma.compress(b"".join(b"100644 blob abc\t" + path.encode() + b"\0"
                                   for path in paths)))
        for path in collected:
            (self.config.collected_dir / repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.config.collected_dir / repo / path).write_text("x\n")

    def _earlier_code(self) -> dict:
        """What a line records that was collected under the same import heuristics and other
        code of the imports."""
        earlier = dict(reuse.import_heuristics(self.rows), code="the code of an earlier image")
        return {"heuristics": "path heuristics of both runs",
                "imports": reuse.keep_heuristics(self.config.heuristics_dir, earlier)}

    def _key(self, rows: list) -> dict:
        return {"heuristics": "path heuristics of both runs",
                "imports": reuse.keep_heuristics(self.config.heuristics_dir,
                                                 reuse.import_heuristics(rows))}

    def _reach(self, rows: list, repo: str, under: dict = None) -> bool:
        return reuse.heuristics_reach(self.config, repo, under or self.under, self._key(rows),
                                      {})

    def test_the_same_import_heuristics_reach_nothing(self):
        self.assertFalse(self._reach(self.rows, "o/claude"))

    def test_a_changed_row_reaches_a_repository_with_its_importing_file(self):
        changed = [dict(row, max_depth="9") if row["id"] == "claude-import-01" else row
                   for row in self.rows]
        self.assertTrue(self._reach(changed, "o/claude"))
        self.assertFalse(self._reach(changed, "o/other"))

    def test_a_documentation_column_reaches_nothing(self):
        read_again = [dict(row, doc_verified="2026-12-31", notes="Read again.")
                      for row in self.rows]
        self.assertFalse(self._reach(read_again, "o/claude"))

    def test_other_code_of_the_imports_reaches_a_repository_with_a_file_that_can_import(self):
        """A line that differs only in the fingerprint of the code of the imports is taken over
        where its file list has no file that an import row names, and collected again where it
        has one."""
        for repo, path in (("o§gemini", "GEMINI.md"), ("o§agents", "pkg/AGENTS.md"),
                           ("o§copilot", ".github/copilot-instructions.md")):
            self._list(repo, (path,), collected=(path,))
        under = self._earlier_code()
        self.assertFalse(self._reach(self.rows, "o/other", under))
        for repo in ("o/claude", "o/gemini", "o/agents", "o/copilot"):
            with self.subTest(repo=repo):
                self.assertTrue(self._reach(self.rows, repo, under))

    def test_other_code_of_a_recorded_version_that_reaches_nothing_leaves_the_rows_to_decide(self):
        """Earlier code of the imports counts as the current code when the record of the versions
        of that code (`code_versions.IMPORTS`) connects the two only by changes that reach no
        repository. The rows decide whether the line is collected again, and a changed row still
        reaches the repository with its importing file."""
        record = (code_versions.Version("the code of an earlier image", "c1", "earlier", None),
                  code_versions.Version(reuse.import_code_fingerprint(), "c2",
                                        "a reworded text", code_versions.NO_REPOSITORY))
        changed = [dict(row, max_depth="9") if row["id"] == "claude-import-01" else row
                   for row in self.rows]
        with mock.patch.object(code_versions, "IMPORTS", record):
            under = self._earlier_code()
            self.assertFalse(self._reach(self.rows, "o/claude", under))
            self.assertTrue(self._reach(changed, "o/claude", under))
            self.assertFalse(self._reach(changed, "o/other", under))

    def test_a_repository_from_which_the_collection_copied_nothing_is_not_reached(self):
        """The importing files are among the files that the collection copied. Other code of the
        imports therefore copies nothing else from a repository from which it copied no file,
        such as a repository with a CLAUDE.md only in a vendored directory, which its file list
        names. Once the
        collection copied a file, the CLAUDE.md of its file list reaches it."""
        listed = ("node_modules/x/CLAUDE.md", "src/a.c")
        self._list("o§vendored", listed)
        self.assertFalse(self._reach(self.rows, "o/vendored", self._earlier_code()))
        self._list("o§vendored", listed, collected=(".claude/settings.json",))
        self.assertTrue(self._reach(self.rows, "o/vendored", self._earlier_code()))

    def test_a_row_of_a_plugin_s_rules_reaches_a_repository_with_such_a_plugin(self):
        """`<antigravity-plugin>/rules` names the rules of a plugin, a directory with a manifest,
        and not every folder named `rules`. For each layout, the row reaches the repository
        where the cloning step names an importing file of the row among the repository's files
        (`imports.importing_files`): a plugin in a default place, a plugin with its manifest in
        a folder of its own, and a directory that a plugins.json registers. A
        `rules` folder outside a plugin reaches nothing."""
        rule = "---\ntrigger: always_on\n---\nSee @[style](style.md)\n"
        layouts = {
            "default": {".agents/plugins/p/plugin.json": '{"name": "p"}',
                        ".agents/plugins/p/rules/a.md": rule},
            "folder": {".agents/plugins/q/.gemini-plugin/plugin.json": '{"name": "q"}',
                       ".agents/plugins/q/rules/b.md": rule},
            "registered": {".agents/plugins.json": '{"entries": [{"path": "tools/r"}]}',
                           "tools/r/plugin.json": '{"name": "r"}', "tools/r/rules/c.md": rule},
            "none": {"docs/rules/d.md": rule, "src/rules/e.md": rule},
        }
        row = next(each for each in self.rows if each["path"] == "<antigravity-plugin>/rules")
        changed = [dict(each, max_depth="2") if each is row else each for each in self.rows]
        for name, files in layouts.items():
            with self.subTest(layout=name):
                root = self.tmp / "clones" / f"o§{name}"
                for rel, text in files.items():
                    (root / rel).parent.mkdir(parents=True, exist_ok=True)
                    (root / rel).write_text(text)
                named = imports.importing_files([row], root, sorted(files),
                                                imports.CloneTree(root))
                self.assertEqual(bool(named), name != "none", named)
                self._list(f"o§{name}", tuple(sorted(files)), collected=tuple(files))
                self.assertEqual(self._reach(changed, f"o/{name}"), bool(named))

    def test_a_line_without_import_heuristics_is_collected_again(self):
        """A line written without an imported tree records no import heuristics."""
        under = {"heuristics": "path heuristics of both runs"}
        self.assertTrue(self._reach(self.rows, "o/claude", under))

    def test_the_log_names_the_import_heuristics_as_the_reason(self):
        """A repository that changed import heuristics reach is collected again for that reason,
        and not for path heuristics, which did not change."""
        key = {"collector": "the collector's code", "heuristics": "path heuristics of both runs",
               "imports": self.under["imports"], "records": {}, "git": "git version 2"}
        under = dict(key, imports=self._earlier_code()["imports"])
        row = "o/claude,https://github.com/o/claude,2026-10-01T00:00:00Z,main," + "a" * 40
        done = {"o/claude": (row, None, [], [], False, None, under, False, False, None, None)}
        config = SimpleNamespace(**vars(self.config), clone_timeout=0, max_clone_failures=0,
                                 vendored_dirs=heuristics.load_vendored_dirs(), token=None,
                                 use_ssh=False)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            again = reuse.outdated(config, ["o/claude"], done, set(), key, check_heads=False)
        self.assertEqual(again, {"o/claude"})
        self.assertIn("(1 with changed import heuristics that match their files)",
                      err.getvalue())
        self.assertNotIn("path heuristics", err.getvalue())


class TestTheHeadQueryFallsBackToAnAnonymousQuery(unittest.TestCase):
    """`_head` queries a head first with the GitHub access token and then without it, so that a
    repository that refuses an authenticated request is read as an anonymous query reads it. An
    organization with an IP allow list refuses an authenticated request from an address outside
    the list, also for a public repository, and GitHub refuses a GitHub access token that has
    expired."""

    TOKEN = "ghp_notarealtoken0000000000000000000000"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _init_git_repo(self.tmp / "source")
        subprocess.run(["git", "clone", "-q", "--bare", str(self.tmp / "source"),
                        str(self.tmp / "o" / "a.git")], capture_output=True, check=True)
        self.head = tuple(subprocess.run(
            ["git", "-C", str(self.tmp / "source"), *args], capture_output=True, text=True,
            check=True).stdout.strip() for args in (("branch", "--show-current"),
                                                    ("rev-parse", "HEAD")))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_refusal_of_the_authenticated_query_leaves_the_head_to_an_anonymous_one(self):
        for refuse in ("address", "token"):
            with self.subTest(refuse=refuse), github_on_loopback(self.tmp, refuse) as server:
                self.assertEqual(reuse._head(("o/a", self.TOKEN, False)), ("o/a", self.head))
                self.assertEqual(server.requests[0], ("x-access-token", self.TOKEN))


class _Clock:
    """A stand-in for the `time` module of `reuse`: `sleep` records the wait and moves `now` on
    rather than waiting."""

    def __init__(self):
        self.now, self.waits = 0.0, []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


class TestTheHeadCheckWaitsOutABurst(unittest.TestCase):
    """`remote_heads` queries again, after growing waits and with fewer threads, the heads for
    which git returned no head commit, while they are more than the step's ceiling. The
    step-level cases are in tests.test_steps_repository_cloning.TestTheFailureGuards."""

    REPOS = [f"o/r{number}" for number in range(40)]
    HEAD = ("main", "a" * 40)

    def setUp(self):
        self.clock, self.queried = _Clock(), []

    def _heads(self, responds, ceiling: int = 1, waits=(60.0, 120.0, 240.0)) -> dict:
        """`remote_heads` over `REPOS` with 32 threads, where git returns what
        `responds(repo, now)` returns."""
        def remote_head(repo, token=None, use_ssh=False, authenticate=False):
            self.queried.append(repo)
            return responds(repo, self.clock.now)

        config = SimpleNamespace(token=None, use_ssh=False)
        with mock.patch.object(reuse.clone, "remote_head", remote_head), \
                mock.patch.object(reuse, "time", self.clock), \
                contextlib.redirect_stderr(io.StringIO()):
            return reuse.remote_heads(config, self.REPOS, 32, ceiling, waits)

    def test_a_burst_that_ends_during_a_wait_leaves_every_head_with_a_response(self):
        responses = self._heads(lambda repo, now: self.HEAD if now >= 60 or repo == "o/r0"
                                else None)
        self.assertEqual(self.clock.waits, [60.0])
        self.assertEqual(set(responses.values()), {self.HEAD})

    def test_a_network_that_is_down_costs_a_few_queries_per_round(self):
        """Each round queries its first heads, as many as it has threads, half the threads of
        the round before and at least four, and stops there when none returns a head commit.
        Every query is made twice."""
        responses = self._heads(lambda repo, now: None)
        self.assertEqual(self.clock.waits, [60.0, 120.0, 240.0])
        self.assertEqual(set(responses.values()), {None})
        self.assertEqual(len(self.queried), 2 * len(self.REPOS) + 2 * (16 + 8 + 4))

    def test_heads_that_fail_every_time_and_sort_first_do_not_hide_the_others(self):
        """Half the heads, the first in sorted order, as one owner's repositories are, never
        return a head commit, and the others do once the burst ends. A round that tried only
        the first heads would never query the others again."""
        failing = set(self.REPOS[:20])
        responses = self._heads(lambda repo, now: None if repo in failing or now < 60
                                else self.HEAD)
        self.assertEqual({repo for repo, response in responses.items() if response is None},
                         failing)

    def test_heads_within_the_ceiling_or_without_one_cost_no_wait(self):
        for ceiling in (len(self.REPOS), 0):
            with self.subTest(ceiling=ceiling):
                self._heads(lambda repo, now: None, ceiling=ceiling)
                self.assertEqual(self.clock.waits, [])


if __name__ == "__main__":
    unittest.main()
