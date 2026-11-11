"""Tests for agentcontext.steps.repository_cloning and agentcontext.collector: the worker over one
repository, the pool, the conditions that stop a running collection, the resume, reading the
commit history and the command line. The tests build every repository with `git init` and clone it
through a `file://` redirect, which keeps every test off the network. The module takes an ambient
GITHUB_TOKEN away for the run.

Every path that the collector writes defaults to the stage directory. A test that calls `main`
therefore passes `--file-history`, `--submodules`, `--ai-authorship`, `--commits-dir` and
`--ai-commits-csv`, and the module fails when a test wrote into the stage directory.

Requirements: Python 3.14, git and rsync on PATH (stdlib only)
Run with:  python3 -m unittest tests.test_steps_repository_cloning -v
"""

import contextlib
import csv
import dataclasses
import errno
import functools
import hashlib
import importlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from unittest import mock

from tests._fixtures import (_add_commit, _add_noise, _init_git_repo, _row_field,
                             dsm_writes_during_removal, ext_redirect,
                             file_system_sync_never_returns, git_version_fails, sampled_every,
                             upload_pack)
from agentcontext import STAGE_DIR, caches, collector, commits, guards as _guards, paths
from agentcontext import resume as _resume
from agentcontext.caches import write_file_history
from agentcontext.collector import CollectionConfig, measure_collected_files, process_repo
from agentcontext.git import clone as _clone
from agentcontext.paths import COLLECTED_CSV_HEADER, METADATA_COLUMNS as _METADATA_COLUMNS
from agentcontext.repos import repo_name_to_dir
from agentcontext.resume import append_finished, read_finished
from agentcontext.steps import repository_cloning as _mod
from agentcontext.steps.repository_cloning import (
    COLLECTED_CSV_NAME,
    FAIL_CSV_HEADER,
    FAIL_CSV_NAME,
    main,
    parse_repo_csv,
    run_collection,
)
from tests import _ambient_env


def _another_collector(real=_mod.reuse.code_fingerprint):
    """A stand-in for `reuse.code_fingerprint` as in an image that changed only the collector.
    The collector's code gets another fingerprint, and the code of the imports that
    `reuse.import_files` names keeps its own."""
    return lambda files, **options: (real(files, **options)
                                     if any(Path(path).name == "imports.py" for path in files)
                                     else "another collector")


def _ai_commit_flags(root: Path) -> list:
    """The flags that put what the cloning step writes about the commits, the records beside
    the collection and the imported tree under a test's directory. Their defaults are relative
    to the working directory: the stage directory under the runner, and the repository root
    under the suite."""
    return ["--ai-authorship", str(root / "ai_authorship.csv"),
            "--commits-dir", str(root / "collect_commits"),
            "--ai-commits-csv", str(root / "output" / "all_ai_commits.csv"),
            "--file-lists-dir", str(root / "file_lists"),
            "--workflows-dir", str(root / "workflows"),
            "--imported-dir", str(root / "imported")]


class _HeadCheckClock:
    """A stand-in for the `time` module that the head check of `agentcontext.reuse` waits with.
    `sleep` records the wait and advances `now` instead of waiting."""

    def __init__(self):
        self.now, self.waits = 0.0, []

    def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


def _without_scan_time(row: str) -> list[str]:
    """One row of the collected list without `scanned_at`.

    `scanned_at` is the wall-clock time at which the cloning step collected the repository. Two
    runs over the same remote differ in it by design and in nothing else.
    """
    parts = row.split(",")
    del parts[COLLECTED_CSV_HEADER.split(",").index("scanned_at")]
    return parts


def _commit_message(repo: Path, rel: str, content: str, message: str,
                    author: str = None, date: str = None) -> str:
    """Commit one file with a given message, and optionally an identity and a date."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    env = dict(os.environ)
    if author:
        env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = author
        env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = f"{author}@example.com"
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", "-C", str(repo), "add", "."], capture_output=True, check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-F", "-"], input=message,
                   text=True, capture_output=True, check=True, env=env)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.strip()


_ROW = paths.build_csv_row("owner/repo", "2026-01-01T00:00:00Z", "main", "a" * 40,
                          "2026-01-01T00:00:00Z", "", set())

def _history_not_fetched(cause: str, calls: list):
    """A stand-in for `ensure_full_history` after a fetch that did not retrieve the history: It
    returns False and sets the extractor's `history_failure` to `cause`. Each call appends to
    `calls` the clone that it was called for and the bounds that it ran under, the extractor's
    size bound included."""
    def not_fetched(self_, repo_dir, timeout=None, bounded_path=None, **kwargs):
        calls.append({"repo_dir": repo_dir, "timeout": timeout, "bounded_path": bounded_path,
                      "max_clone_bytes": self_.max_clone_bytes})
        self_.history_failure = cause
        return False
    return not_fetched


_ROOT_WRITES = ("file_history.csv", "ai_authorship.csv", "submodules.csv", "output",
                "collect_commits", "data/collected/repos.csv",
                "data/collected/clone_failed.csv")

def _root_state() -> dict:
    state = {}
    for name in _ROOT_WRITES:
        path = STAGE_ROOT / name
        if path.is_dir():
            state[name] = sorted((q.name, q.stat().st_size, q.stat().st_mtime_ns)
                                 for q in path.iterdir())
        elif path.exists():
            state[name] = (path.stat().st_size, path.stat().st_mtime_ns)
        else:
            state[name] = None
    return state


STAGE_ROOT = STAGE_DIR


_saved_credentials = {}
_root_before = None


def setUpModule():
    # An ambient GITHUB_TOKEN changes the clone URL that these tests redirect, and whether the
    # suite passes would then depend on the machine rather than on the code. _ambient_env lists
    # the variables and the reasons. The state of the stage directory is recorded so that
    # tearDownModule catches a test that wrote into it.
    global _root_before
    _ambient_env.take(_saved_credentials)
    _root_before = _root_state()


def tearDownModule():
    _ambient_env.restore(_saved_credentials)
    after = _root_state()
    changed = [name for name in _ROOT_WRITES if after[name] != _root_before[name]]
    if changed:
        raise AssertionError(f"a test wrote into the stage directory: {', '.join(changed)}. "
                             f"Every call of main needs --file-history, --submodules, "
                             f"--ai-authorship, --commits-dir and --ai-commits-csv.")


class TestParseRepoCsv(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_csv(self, text: str) -> Path:
        p = self.tmp / "input.csv"
        p.write_text(text)
        return p

    def test_simple_owner_repo_extracted(self):
        csv = self._write_csv("repo_name\nowner/repo\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["owner/repo"])

    def test_preserves_input_order(self):
        csv = self._write_csv("repo_name\nalpha/one\nzeta/two\nbeta/three\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["alpha/one", "zeta/two", "beta/three"])

    def test_deduplicates_on_first_occurrence(self):
        csv = self._write_csv("repo_name\nowner/repo\nowner/repo\nowner/repo\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["owner/repo"])

    def test_dedup_preserves_first_position(self):
        csv = self._write_csv("repo_name\na/first\nb/second\na/first\nc/third\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["a/first", "b/second", "c/third"])

    def test_a_url_is_skipped_and_named_rather_than_read_as_another_repository(self):
        """The first `word/word` of a URL is `github.com/owner`, and the step would clone it as
        the repository of the row. A path below a repository would name the repository above
        it."""
        csv = self._write_csv("repo_name\nhttps://github.com/owner/repo\nowner/repo/tree/main\n"
                              "owner/other\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["owner/other"])
        self.assertIn("Skipped 2 values", err.getvalue())
        self.assertIn("https://github.com/owner/repo", err.getvalue())

    def test_skips_rows_without_valid_match(self):
        csv = self._write_csv("repo_name\nnot-a-repo\nowner/repo\n\n")
        with contextlib.redirect_stderr(io.StringIO()):
            result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["owner/repo"])

    def test_dotted_owner_and_repo(self):
        csv = self._write_csv("repo_name\nmy.org/my.repo\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["my.org/my.repo"])

    def test_hyphenated_and_underscored_names(self):
        csv = self._write_csv("repo_name\nsome-org/some_repo\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["some-org/some_repo"])

    def test_missing_column_raises_system_exit(self):
        csv = self._write_csv("wrong_col\nowner/repo\n")
        with self.assertRaises(SystemExit):
            parse_repo_csv(csv, "repo_name")

    def test_empty_csv_body_returns_empty_list(self):
        csv = self._write_csv("repo_name\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, [])

    def test_whitespace_around_value_handled(self):
        csv = self._write_csv("repo_name\n  owner/repo  \n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertEqual(result, ["owner/repo"])

    def test_custom_column_name(self):
        csv = self._write_csv("url,other\nowner/repo,x\n")
        result = parse_repo_csv(csv, "url")
        self.assertEqual(result, ["owner/repo"])

    def test_returns_list_not_set(self):
        csv = self._write_csv("repo_name\nowner/repo\n")
        result = parse_repo_csv(csv, "repo_name")
        self.assertIsInstance(result, list)


class TestTheRunRecordsSubmodules(unittest.TestCase):
    """End to end, the gitlinks reach submodules.csv from the clone that the run already has,
    without a request to GitHub."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
        )

    def tearDown(self):
        for k in ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect(self):
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"

    def _add_gitlink(self, path: str) -> None:
        subprocess.run(["git", "-C", str(self.remote), "update-index", "--add",
                        "--cacheinfo", "160000", "1" * 40, path],
                       capture_output=True, check=True)
        (self.remote / ".gitmodules").write_text(
            f'[submodule "d"]\n\tpath = {path}\n'
            f'\turl = https://github.com/owner/dep.git\n')
        subprocess.run(["git", "-C", str(self.remote), "add", ".gitmodules"],
                       capture_output=True, check=True)
        subprocess.run(["git", "-C", str(self.remote), "commit", "-m", "submodule"],
                       capture_output=True, check=True)

    def test_the_gitlink_reaches_the_row(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._add_gitlink("lib/dep")
        self._redirect()
        _, _, _, _, submodules, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(submodules,
                         [{"repo_name": "owner/repo", "path": "lib/dep",
                           "submodule_repo": "owner/dep"}])

    def test_a_repository_without_one_records_nothing(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        _, _, _, _, submodules, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(submodules, [])

    def test_the_list_of_finished_repositories_keeps_the_submodules_for_a_resume(self):
        finished = self.tmp / "list of finished repositories.jsonl"
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._add_gitlink("lib/dep")
        self._redirect()
        first = run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        again = run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertEqual(first[3], again[3], "the resume lost them")
        self.assertEqual([r["path"] for r in again[3]], ["lib/dep"])


class TestWhatALineDescribesIsOnDisk(unittest.TestCase):
    """The repository's line in the list of finished repositories is synced after
    `process_repo` returns, and every file of the collection has to be synced before that line.
    After a power loss, a resumed run would take over a file that reached the disk after the
    line as it is, empty or missing. The collected file cleanup would then delete an empty file
    and drop the repository."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n\n@docs/style.md\n")
        _add_commit(self.remote, "docs/style.md", "Use tabs.\n")
        _add_commit(self.remote, ".github/workflows/ci.yml", "on: push\n")
        self.cfg = CollectionConfig(
            collected_dir=self.tmp / "collected", clone_dir=self.tmp / "clones",
            log_dir=self.tmp / "logs", workflows_dir=self.tmp / "workflows",
            file_lists_dir=self.tmp / "file_lists", imported_dir=self.tmp / "imported",
            commits_dir=self.tmp / "collect_commits", max_tries=1, base_delay=0)
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{self.remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)

    def test_every_file_of_the_collection_is_synced_before_the_line(self):
        synced, real = set(), os.fsync

        def recorded(fd):
            synced.add((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return real(fd)

        with mock.patch.object(os, "fsync", recorded):
            _repo, _row, failure, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(failure)
        files = [path for top in ("collected", "workflows", "file_lists", "imported",
                                  "collect_commits")
                 for path in (self.tmp / top).rglob("*")
                 if path.is_file() and not path.is_symlink()]
        self.assertTrue(any(path.match("imported/*/docs/style.md") for path in files))
        for path in files:
            with self.subTest(path=path.relative_to(self.tmp).as_posix()):
                self.assertIn((path.stat().st_dev, path.stat().st_ino), synced)

    DIR_NAME = repo_name_to_dir("owner/repo")

    def _collect_again(self, between) -> tuple:
        """Collect the repository, call `between`, collect it again, and return what the
        second collection synced, as (device, inode), with its failure row."""
        process_repo(self.cfg, "owner/repo")
        self.assertTrue((self.cfg.collected_dir / self.DIR_NAME).is_dir())
        self.assertTrue((self.cfg.commits_dir / f"{self.DIR_NAME}.json").is_file())
        between()
        synced, real = set(), os.fsync

        def recorded(fd):
            synced.add((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return real(fd)

        with mock.patch.object(os, "fsync", recorded), \
                contextlib.redirect_stderr(io.StringIO()):
            _repo, _row, failure, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        return synced, failure

    def _assert_the_removal_is_synced(self, synced: set) -> None:
        """The collected tree and the per-repository commit record of the first collection are
        gone, and the directories that held them were synced."""
        self.assertFalse((self.cfg.collected_dir / self.DIR_NAME).exists())
        self.assertFalse((self.cfg.commits_dir / f"{self.DIR_NAME}.json").exists())
        for directory in (self.cfg.collected_dir, self.cfg.commits_dir):
            with self.subTest(directory=directory.name):
                self.assertIn((directory.stat().st_dev, directory.stat().st_ino), synced)

    def test_the_removal_of_an_earlier_collection_is_synced_before_the_line(self):
        """A repository that dropped its configuration since the earlier collection loses its
        tree and its shards. If the removal reached the disk after the line, a power loss would
        bring the files back under a line that describes a collection without them."""
        def drop_the_configuration():
            for command in (["rm", "-q", "CLAUDE.md", "docs/style.md"], ["commit", "-q", "-m", "drop"]):
                subprocess.run(["git", "-C", str(self.remote), *command], check=True,
                               capture_output=True)

        synced, failure = self._collect_again(drop_the_configuration)
        self.assertIsNone(failure)
        self._assert_the_removal_is_synced(synced)

    def test_the_removal_after_a_failed_clone_is_synced_before_the_line(self):
        synced, failure = self._collect_again(lambda: shutil.rmtree(self.remote))
        self.assertIsNotNone(failure)
        self._assert_the_removal_is_synced(synced)

    def test_the_removal_after_a_collector_error_is_synced_before_the_line(self):
        patcher = mock.patch.object(paths, "collect_repo", side_effect=RuntimeError("defect"))
        synced, failure = self._collect_again(patcher.start)
        patcher.stop()
        self.assertTrue(failure.endswith(_resume.COLLECTOR_ERROR))
        self._assert_the_removal_is_synced(synced)


class TestFileHistoryInTheCollectPass(unittest.TestCase):
    """The collector reads the history from the clone that it already has.

    The clone is `--depth 1` and has no history to read until the collector fetches it. That
    fetch is the only network traffic that reading the history adds.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
        )
        self.facts_path = self.tmp / "file_history.csv"

    def tearDown(self):
        for k in ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect(self):
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"

    ENGLISH = ("# Claude\n\nThis file tells the assistant how to work in this "
               "repository, which commands to run and which files to leave alone.\n")

    def test_every_collected_file_is_measured(self):
        _add_commit(self.remote, "CLAUDE.md", self.ENGLISH)
        _add_commit(self.remote, ".mcp.json", '{"mcpServers": {}}\n')
        self._redirect()
        _, _, _, facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(sorted(r["artifact"] for r in facts),
                         [".mcp.json", "CLAUDE.md"])
        row = next(r for r in facts if r["artifact"] == "CLAUDE.md")
        self.assertEqual(row["#commits"], "1")
        self.assertRegex(row["first_commit_sha"], r"\A[0-9a-f]{40}\Z")
        # The row has the columns of `file_history.csv` and no others. The collector does not
        # read `is_empty` and `language`, which here would come from the clone rather than from
        # the tree that every other column of the row describes.
        self.assertEqual(list(row), list(caches.FILE_HISTORY_COLUMNS))

    def test_the_measurement_is_keyed_on_the_commit_that_was_read(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        _, main_row, _, facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(facts[0]["head_sha"], _row_field(main_row, "head_sha"))
        self.assertEqual(facts[0]["repo_name"], "owner/repo")

    def test_the_mechanism_is_left_blank(self):
        """The detection step decides which CSV publishes a path, and the history does not
        depend on that decision."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        _, _, _, facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(facts[0]["mechanism"], "")

    def test_the_root_commit_reaches_the_repository_row(self):
        """The root commit comes from the history that the collector fetches, because a
        depth-1 clone lacks it."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        root = subprocess.run(["git", "-C", str(self.remote), "rev-list",
                               "--max-parents=0", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "y" * 95)
        self._redirect()
        _, main_row, _, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(_row_field(main_row, "first_commit_sha"), root)

    def test_a_repository_that_tracks_a_file_named_head_is_collected(self):
        """protoman/rockbot tracks a file named `HEAD` at its root, which makes `git show HEAD`
        and `git rev-list HEAD` ambiguous between the commit and the file."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        root = subprocess.run(["git", "-C", str(self.remote), "rev-list",
                               "--max-parents=0", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
        _add_commit(self.remote, "HEAD", "a file, not a ref\n")
        self._redirect()
        _, main_row, fail_row, facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertRegex(_row_field(main_row, "head_commit_date"), r"\A\d{4}-\d{2}-\d{2}T")
        self.assertEqual(_row_field(main_row, "first_commit_sha"), root)
        self.assertEqual([r["artifact"] for r in facts], ["CLAUDE.md"])

    def test_a_broken_symlink_in_the_output_tree_is_still_a_collected_file(self):
        """`is_file()` follows a link and returns False for a broken link. A collected tree can
        contain a broken link on purpose, such as the symlink marker that the collector records
        for `.github/skills` of `dotnet/sdk`.

        The test builds the broken link in the output tree directly. `copy_dir` decides whether
        a copy reproduces a link or writes its target into a regular file, and this test checks
        what a later listing of the tree finds."""
        _add_commit(self.remote, "CLAUDE.md", self.ENGLISH)
        self._redirect()
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", f"file://{self.remote}", str(clone)],
                       check=True, capture_output=True)
        out = self.tmp / "collected"
        (out / ".github").mkdir(parents=True)
        (out / "CLAUDE.md").write_text(self.ENGLISH, encoding="utf-8")
        (out / ".github" / "skills").symlink_to("../../elsewhere/skills")
        self.assertFalse((out / ".github" / "skills").exists(), "the link should dangle")
        _first, facts, copied = measure_collected_files("owner/repo", "a" * 40, clone, out)
        self.assertEqual(copied, 2)
        self.assertIn(".github/skills", [r["artifact"] for r in facts])

    def test_a_history_that_cannot_be_fetched_leaves_the_files_without_a_history_row(self):
        """A failed fetch does not return True. Otherwise, after the timeout killed the
        `git fetch --unshallow` of a `--depth 1` clone, the collector would measure every file
        against the clone's one commit, cache the result as complete and never examine the file
        again. The bounds of the clone apply to the fetch."""
        _add_commit(self.remote, "CLAUDE.md", self.ENGLISH)
        self._redirect()
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.remote}",
                        str(clone)], check=True, capture_output=True)
        out = self.tmp / "collected"
        out.mkdir()
        (out / "CLAUDE.md").write_text(self.ENGLISH, encoding="utf-8")
        fetch = []
        with mock.patch.object(_mod.history.GitFileMetadata, "ensure_full_history",
                               _history_not_fetched("time_bound", fetch)), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(_mod.history.HistoryUnavailable) as caught:
            measure_collected_files("owner/repo", "a" * 40, clone, out, clone_timeout=123.0,
                                    max_clone_bytes=456)
        self.assertEqual(caught.exception.cause, "time_bound")
        self.assertIn("Wrote no rows of file_history.csv or ai_authorship.csv", err.getvalue())
        # The size bound reaches the fetch through the extractor. An extractor built without it
        # fails this assertion.
        self.assertEqual(fetch, [{"repo_dir": str(clone), "timeout": 123.0,
                                  "bounded_path": str(clone), "max_clone_bytes": 456}])

    def test_a_repository_that_contributed_nothing_is_not_measured(self):
        """A repository without a copied file has no history to read and no reason to fetch
        one."""
        _add_commit(self.remote, "README.md", "# nothing to collection\n")
        self._redirect()
        _, main_row, _, facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(facts, [])
        self.assertEqual(_row_field(main_row, "first_commit_sha"), "")

    def test_no_file_history_skips_the_whole_thing(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        cfg = CollectionConfig(
            collected_dir=self.cfg.collected_dir, clone_dir=self.cfg.clone_dir,
            log_dir=self.cfg.log_dir, max_tries=1, base_delay=0, measure_git=False,
        )
        _, main_row, _, facts, _subs, _measures = process_repo(cfg, "owner/repo")
        self.assertEqual(facts, [])
        self.assertEqual(_row_field(main_row, "first_commit_sha"), "")
        self.assertTrue((cfg.collected_dir / "owner§repo" / "CLAUDE.md").exists())

    def test_the_list_of_finished_repositories_keeps_the_file_history_rows_for_a_resume(self):
        """The rows cost a history fetch, and the clone that they were read from is gone."""
        finished = self.tmp / "list of finished repositories.jsonl"
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        first = run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertEqual(len(first[2]), 1)
        again = run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertEqual(again[2], first[2], "the resume re-measured or lost them")

    def test_writing_the_cache_merges_rather_than_replaces(self):
        """A run covers a list of repositories, and the cache covers every repository ever
        collected."""
        existing = caches.FileHistoryCache()
        existing.put(caches.FileHistoryKey("other/repo", "a" * 40, "", "CLAUDE.md"),
                     caches.FileHistory("t", "1", "b" * 40, "c" * 40))
        existing.save(self.facts_path)
        rows = [dict(caches.FileHistoryKey("owner/repo", "d" * 40, "", "x.md").as_row(),
                     **caches.FileHistory("t", "2", "e" * 40, "f" * 40).as_row())]
        total = write_file_history(self.facts_path, rows)
        self.assertEqual(total, 2)
        reloaded = caches.FileHistoryCache.load(self.facts_path)
        self.assertIn(caches.FileHistoryKey("other/repo", "a" * 40, "", "CLAUDE.md"),
                      reloaded)

    def test_the_cache_is_written_to_the_path_every_reader_uses(self):
        """The cache goes to `caches.FILE_HISTORY_PATH`, not beside the list.

        The list is in the tree that it lists. A cache path derived from the list's parent would
        be `data/collected/file_history.csv`, where nothing reads it, because
        `agentcontext/steps/detection.py` and `caches.py` both read the stage directory. The
        detection step would then report each of its rows as missing from the cache, log a
        WARNING, exit 0 and publish blank Git columns. `agentcontext/steps/file_history.py`
        would clone every repository again to measure it.

        The test redirects `DEFAULT_PATH` rather than writing to it, because it asserts that the
        collector reads that name, not that it writes that file. A real test run keeps its
        fixtures out of the tracked cache with `--file-history`.
        """
        _add_commit(self.remote, "CLAUDE.md", self.ENGLISH)
        self._redirect()
        out = self.tmp / "run"
        collected = out / "data" / "collected"
        collected.mkdir(parents=True)
        shared = out / "stage_root" / "file_history.csv"
        shared.parent.mkdir(parents=True)
        argv = ["--repo-csv", str(self._repo_csv()),
                "--collected-dir", str(collected),
                "--collected-csv", str(collected / "repos.csv"),
                "--fail-csv", str(collected / "clone_failed.csv"),
                "--finished", str(out / "list of finished repositories.jsonl"),
                "--submodules", str(out / "submodules.csv"),
                *_ai_commit_flags(out),
                "--min-free-gb", "0",
                "--clone-dir", str(out / "tmp"), "-l", str(out / "logs"),
                "-w", "1", "--max-tries", "1"]
        saved = caches.FILE_HISTORY_PATH
        caches.FILE_HISTORY_PATH = shared
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(_mod.main(argv), 0)
        finally:
            caches.FILE_HISTORY_PATH = saved
        self.assertTrue(shared.is_file(), "the cache was not written to DEFAULT_PATH")
        self.assertFalse((collected / "file_history.csv").exists(),
                         "the cache was written inside the collected tree, where nothing reads it")

    def test_the_submodules_path_is_the_one_validation_reads(self):
        """The test compares the defaults of the two modules, because each module defines its
        own default and the step works only when the two agree.

        `collected_file_cleanup.DEFAULT_SUBMODULES` is a fixed path in the stage directory. A
        collector default derived from the list's parent would put the file at
        `data/collected/submodules.csv`. The collected file cleanup would then read an empty
        map, log that it drops no file and exit 0. It would publish the files of the repository
        that a gitlink names as the referencing repository's own configuration.
        """
        validate = importlib.import_module("agentcontext.steps.collected_file_cleanup")
        default = _mod._build_arg_parser().get_default("submodules")
        self.assertEqual(Path(default), Path(validate.DEFAULT_SUBMODULES))

    def _repo_csv(self) -> Path:
        path = self.tmp / "repos.csv"
        path.write_text("repo_name\nowner/repo\n", encoding="utf-8")
        return path

    def test_a_repository_measured_again_at_a_new_head_keeps_both_rows(self):
        """The old row stays for the old commit, because the key contains the commit."""
        rows = []
        for head in ("a" * 40, "b" * 40):
            rows.append(dict(
                caches.FileHistoryKey("owner/repo", head, "", "CLAUDE.md").as_row(),
                **caches.FileHistory("t", "1", "c" * 40, "d" * 40).as_row()))
        self.assertEqual(write_file_history(self.facts_path, rows), 2)


class TestProcessRepoPrintMatches(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)

    def tearDown(self):
        for k in ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, print_matches: bool) -> "CollectionConfig":
        return CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
            print_matches=print_matches,
        )

    def _redirect(self, base_url: str) -> None:
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.{base_url}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"

    def test_clone_start_and_done_logged(self):
        _add_commit(self.remote, "CLAUDE.md", "# c")
        self._redirect(f"file://{self.remote}")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            process_repo(self._cfg(True), "owner/repo")
        out = buf.getvalue()
        self.assertIn("INFO repository_cloning: Cloning owner/repo", out)
        self.assertIn("INFO repository_cloning: Finished owner/repo with the file history of", out)

    def test_clone_failure_logged(self):
        self._redirect("http://127.0.0.1:9/nonexistent/x")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            process_repo(self._cfg(True), "owner/repo")
        self.assertIn("ERROR repository_cloning: The clone of owner/repo failed with outcome "
                      "clone_failed", buf.getvalue())

    def test_disabled_produces_no_stderr(self):
        _add_commit(self.remote, "CLAUDE.md", "# c")
        self._redirect(f"file://{self.remote}")
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            process_repo(self._cfg(False), "owner/repo")
        self.assertEqual(buf.getvalue(), "")


class TestProcessRepo(unittest.TestCase):
    """The collection of one repository from end to end, from a local file:// remote without
    network access."""

    def test_a_failed_snapshot_query_is_a_collector_error_for_a_repository_with_commits(self):
        _add_commit(self.remote, "CLAUDE.md", "# instructions\n")
        self._redirect(self.remote.as_uri())
        real = subprocess.run
        for query in (("rev-parse", "HEAD"), ("symbolic-ref", "HEAD"),
                      ("log", "-1", "--format=%cI", "HEAD", "--")):
            def rejected(cmd, *args, **kwargs):
                if tuple(cmd[-len(query):]) == query:
                    return subprocess.CompletedProcess(cmd, 128, "", "fatal: injected snapshot failure")
                return real(cmd, *args, **kwargs)
            with self.subTest(query=query), mock.patch.object(subprocess, "run", rejected), \
                    contextlib.redirect_stderr(io.StringIO()) as output:
                _, _, failure, facts, _, _ = process_repo(self.cfg, "owner/repo")
                self.assertIsNotNone(failure)
                self.assertTrue(failure.endswith(",collector_error"))
                self.assertEqual(facts, [])
                self.assertIn("injected snapshot failure", output.getvalue())

    def test_a_genuinely_empty_repository_has_no_snapshot_and_is_not_a_collector_error(self):
        empty = self.tmp / "empty"
        subprocess.run(["git", "init", "--bare", str(empty)], capture_output=True, check=True)
        self._redirect(empty.as_uri())
        with contextlib.redirect_stderr(io.StringIO()):
            _, row, failure, facts, _, _ = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(failure)
        self.assertEqual(_row_field(row, "head_sha"), "")
        self.assertEqual(facts, [])

    def test_an_empty_repository_count_failure_retains_the_failed_command_and_error(self):
        empty = self.tmp / "empty"
        subprocess.run(["git", "init", "--bare", str(empty)], capture_output=True, check=True)
        self._redirect(empty.as_uri())
        real = subprocess.run
        def rejected(cmd, *args, **kwargs):
            if cmd[-3:] == ["rev-list", "--all", "--count"]:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: injected commit-count failure")
            return real(cmd, *args, **kwargs)
        with mock.patch.object(subprocess, "run", rejected), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            _, _, failure, facts, _, _ = process_repo(self.cfg, "owner/repo")
        self.assertTrue(failure.endswith(",collector_error"))
        self.assertEqual(facts, [])
        self.assertIn("rev-list --all --count", output.getvalue())
        self.assertIn("injected commit-count failure", output.getvalue())

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.cfg = CollectionConfig(
            collected_dir=self.tmp / "out",
            clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs",
            max_tries=1,
            base_delay=0,
        )

    def tearDown(self):
        for k in ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"]:
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect(self, base_url: str, original: str = "https://github.com/owner/repo.git") -> None:
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.{base_url}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = original

    def test_success_sets_flag(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        _, main_row, fail_row, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(_row_field(main_row, "claude"), "TRUE")
        self.assertEqual(_row_field(main_row, "claude.md"), "TRUE")

    def test_success_copies_artifact_to_output(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        process_repo(self.cfg, "owner/repo")
        self.assertTrue((self.cfg.collected_dir / "owner§repo" / "CLAUDE.md").exists())

    def test_success_deletes_clone(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        process_repo(self.cfg, "owner/repo")
        self.assertFalse((self.cfg.clone_dir / "owner§repo").exists())

    def test_success_records_branch_and_both_dates(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        _, main_row, _, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertNotEqual(_row_field(main_row, "branch"), "")
        # committer date is ISO-8601 with a 'T'
        self.assertIn("T", _row_field(main_row, "head_commit_date"))
        self.assertIn("T", _row_field(main_row, "scanned_at"))

    def test_the_commit_date_is_the_clones_head_and_not_the_scan_time(self):
        """A commit made years before the run has to reach `head_commit_date` unchanged,
        or the column is the scan time under a second name."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude", date="2019-04-04T09:00:00+02:00")
        self._redirect(f"file://{self.remote}")
        _, main_row, _, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        expected = subprocess.run(
            ["git", "-C", str(self.remote), "show", "-s", "--format=%cI", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(_row_field(main_row, "head_commit_date"), expected)
        self.assertTrue(expected.startswith("2019-04-04T09:00:00"), expected)
        self.assertNotEqual(_row_field(main_row, "scanned_at"),
                            _row_field(main_row, "head_commit_date"))

    def test_success_records_head_sha_of_the_clone(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        _, main_row, _, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        expected = subprocess.run(
            ["git", "-C", str(self.remote), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(_row_field(main_row, "head_sha"), expected)

    def test_no_config_does_not_create_a_collected_dir(self):
        # The remote has only a README, which is not a configuration artifact.
        self._redirect(f"file://{self.remote}")
        _, main_row, fail_row, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertFalse((self.cfg.collected_dir / "owner§repo").exists())

    def test_no_config_all_flags_false(self):
        self._redirect(f"file://{self.remote}")
        _, main_row, _, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        for col in COLLECTED_CSV_HEADER.split(",")[len(_METADATA_COLUMNS):]:
            self.assertEqual(_row_field(main_row, col), "FALSE")

    def test_clone_failure_returns_fail_row(self):
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        _, main_row, fail_row, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(fail_row, "owner/repo,https://github.com/owner/repo,clone_failed")

    def test_clone_failure_main_row_all_false_empty_branch(self):
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        _, main_row, _, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(_row_field(main_row, "branch"), "")
        self.assertEqual(_row_field(main_row, "claude"), "FALSE")

    def test_a_failed_clone_records_the_scan_and_no_commit_date(self):
        """A clone failure has no commit and therefore no committer date. The collection was
        still attempted, at a time that the row can state. One column for both would publish
        the time of the clone failure as the date of a commit that does not exist."""
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        _, main_row, fail_row, _facts, _subs, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNotNone(fail_row)
        self.assertEqual(_row_field(main_row, "head_commit_date"), "")
        self.assertEqual(_row_field(main_row, "head_sha"), "")
        self.assertRegex(_row_field(main_row, "scanned_at"),
                         r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

    def test_a_clone_past_the_size_bound_records_budget_exceeded(self):
        """A repository that the size bound stopped leaves a row of the collected list with the
        scan time and every flag FALSE, a clone_failed.csv row under its own reason, no
        collected directory and no clone. The list of finished repositories records it as it
        records any finished repository, and a resumed run under the same bound does not clone
        it again.

        The watcher stops the clone while the pack arrives. The breach that the watcher raises
        must not leave the clone as an exception, because `process_repo` would record that
        exception as `collector_error`."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        _add_noise(self.remote, "big.bin", 3 * 1024 * 1024)
        for key, value in ext_redirect(upload_pack(self.tmp, pause=0.05), self.remote,
                                       "https://github.com/owner/repo.git").items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=3, base_delay=0,
            max_clone_bytes=1024 * 1024)
        with sampled_every(0.05):
            _, main_row, fail_row, facts, subs, _measures = process_repo(cfg, "owner/repo")
        self.assertEqual(fail_row, "owner/repo,https://github.com/owner/repo,size_bound_exceeded")
        self.assertEqual(_row_field(main_row, "head_sha"), "")
        self.assertEqual(_row_field(main_row, "claude"), "FALSE")
        self.assertEqual((facts, subs), ([], []))
        self.assertFalse((cfg.collected_dir / "owner§repo").exists())
        self.assertFalse((cfg.clone_dir / "owner§repo").exists())
        log = (cfg.log_dir / "owner§repo.log").read_text()
        self.assertNotIn("after the clone", log, "the clone was not stopped in flight")
        self.assertNotIn("attempt 2/3", log)

    def test_a_budget_exceeded_repository_is_cloned_again_under_a_larger_size_bound_alone(self):
        """The clone exceeds the same size bound on every run, and a resumed run under that
        bound skips the repository. A resumed run under a larger bound clones it, which is the
        purpose of raising the size bound. The list of finished repositories records the size
        bound for this decision. Without it, the outcome would stay recorded under any
        bound."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        _add_commit(self.remote, "big.bin", "x" * (3 * 1024 * 1024))
        self._redirect(f"file://{self.remote}")
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
            max_clone_bytes=1024 * 1024)
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("size_bound_exceeded", finished.read_text())
        # Each clone starts its log afresh. Only a run that skips the repository keeps the
        # line below.
        log = cfg.log_dir / "owner§repo.log"
        log.write_text("not cloned since\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                finished_path=finished)
        self.assertEqual(log.read_text(), "not cloned since\n")
        self.assertEqual(fail_rows, ["owner/repo,https://github.com/owner/repo,size_bound_exceeded"])
        larger = dataclasses.replace(cfg, max_clone_bytes=64 * 1024 * 1024)
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, fail_rows, _, _ = run_collection(larger, ["owner/repo"], workers=1,
                                                        finished_path=finished)
        self.assertNotIn("not cloned since", log.read_text())
        self.assertEqual(fail_rows, [])
        self.assertEqual(_row_field(main_rows[0], "claude.md"), "TRUE")

    def test_a_clone_past_the_time_bound_twice_is_cloned_again_under_a_larger_one_alone(self):
        """The cloning step records a clone that the time bound ends twice as
        `time_bound_exceeded`, with the bound. Neither the retry rounds nor a resumed run under
        the same `--clone-timeout` clone the repository again, because each attempt would hold
        a worker for the whole bound. A run under a larger bound clones it."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=6, base_delay=0, clone_timeout=0.001)
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                finished_path=finished)
        stopped = "owner/repo,https://github.com/owner/repo,time_bound_exceeded"
        self.assertEqual(fail_rows, [stopped])
        self.assertEqual(read_finished(finished)["owner/repo"][9], 0.001)
        self.assertEqual(_mod.to_retry(cfg, ["owner/repo"], read_finished(finished)), set())
        # Each clone starts its log afresh. Only a run that skips the repository keeps the
        # line below.
        log = cfg.log_dir / "owner§repo.log"
        log.write_text("not cloned since\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                finished_path=finished)
        self.assertEqual(log.read_text(), "not cloned since\n")
        self.assertEqual(fail_rows, [stopped])
        larger = dataclasses.replace(cfg, clone_timeout=600)
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, fail_rows, _, _ = run_collection(larger, ["owner/repo"], workers=1,
                                                        finished_path=finished)
        self.assertNotIn("not cloned since", log.read_text())
        self.assertEqual(fail_rows, [])
        self.assertEqual(_row_field(main_rows[0], "claude.md"), "TRUE")

    def test_a_retry_past_the_time_bound_keeps_the_earlier_collection(self):
        """A repository with an ok line is collected again, here for a changed collector, and
        the time bound ends its clone twice. The tree, the shards, the file list, the workflow
        files and the row of the earlier collection stay. The line records the bound. Under that
        bound, neither the retry rounds nor a resumed run of the collection clone the repository
        again, and a run under a larger bound clones it."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        _add_commit(self.remote, ".github/workflows/ci.yml", "on: push\n")
        self._redirect(f"file://{self.remote}")
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=6, base_delay=0,
            commits_dir=self.tmp / "commits", file_lists_dir=self.tmp / "lists",
            workflows_dir=self.tmp / "workflows")
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            first, _, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                            finished_path=finished)
        kept = [cfg.collected_dir / "owner§repo" / "CLAUDE.md",
                commits.shard_paths(cfg.commits_dir, "owner/repo")[0],
                commits.shard_paths(cfg.commits_dir, "owner/repo")[1],
                cfg.file_lists_dir / f"owner§repo{_clone.FILE_LIST_SUFFIX}",
                cfg.workflows_dir / "owner§repo" / "ci.yml"]
        self.assertTrue(all(path.is_file() for path in kept), kept)
        short = dataclasses.replace(cfg, clone_timeout=0.001)
        with mock.patch.object(_mod.reuse, "code_fingerprint", _another_collector()):
            with contextlib.redirect_stderr(io.StringIO()):
                main_rows, fail_rows, _, _ = run_collection(short, ["owner/repo"], workers=1,
                                                            finished_path=finished)
            self.assertEqual(fail_rows, [])
            self.assertEqual(main_rows, first)
            self.assertEqual([path for path in kept if not path.is_file()], [])
            line = read_finished(finished)["owner/repo"]
            self.assertIsNone(line[1])
            self.assertFalse(line[4], "not marked to redo under the same bound")
            self.assertEqual(line[9], 0.001)
            self.assertEqual(_mod.to_retry(short, ["owner/repo"], read_finished(finished)), set())
            # Each clone starts its log afresh. Only a run that skips the repository keeps the
            # line below.
            log = cfg.log_dir / "owner§repo.log"
            log.write_text("not cloned since\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                run_collection(short, ["owner/repo"], workers=1, finished_path=finished)
            self.assertEqual(log.read_text(), "not cloned since\n")
            with contextlib.redirect_stderr(io.StringIO()):
                main_rows, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                            finished_path=finished)
        self.assertNotIn("not cloned since", log.read_text())
        self.assertEqual(fail_rows, [])
        self.assertIsNone(read_finished(finished)["owner/repo"][9])

    def test_a_time_bound_of_an_earlier_collection_is_tried_again_under_the_same_bound(self):
        """For the rest of the collection that recorded it, a repository with a clone that the
        time bound stopped is not cloned again under the same bound. The next collection clones
        it again, because a transfer that the bound stopped on one day can finish on
        another."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}")
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=6, base_delay=0, clone_timeout=600)
        finished = self.tmp / "list of finished repositories.jsonl"
        stopped = "owner/repo,https://github.com/owner/repo,time_bound_exceeded"
        append_finished(finished, "owner/repo", "owner/repo,,,FALSE", stopped,
                        time_bound=600, collection="OLD")
        log = cfg.log_dir / "owner§repo.log"
        log.parent.mkdir(parents=True)
        log.write_text("not cloned since\n", encoding="utf-8")

        def collection(name: str) -> dict:
            return {"collection": name, "input_sha256": "0" * 64, "taken_over": set()}

        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                finished_path=finished,
                                                collection=collection("OLD"))
        self.assertEqual(fail_rows, [stopped])
        self.assertEqual(log.read_text(), "not cloned since\n")
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                        finished_path=finished,
                                                        collection=collection("NEW"))
        self.assertNotIn("not cloned since", log.read_text())
        self.assertEqual(fail_rows, [])
        self.assertEqual(_row_field(main_rows[0], "claude.md"), "TRUE")

    def test_a_budget_exceeded_line_without_its_size_bound_is_cloned_again_once(self):
        """A line without a size bound does not record which bound the clone went over, and
        the next run clones the repository. The line that this run writes records its size
        bound and stays in force under the same bound afterwards."""
        _add_commit(self.remote, "big.bin", "x" * (3 * 1024 * 1024))
        self._redirect(f"file://{self.remote}")
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
            max_clone_bytes=1024 * 1024)
        finished = self.tmp / "list of finished repositories.jsonl"
        fail_row = "owner/repo,https://github.com/owner/repo,size_bound_exceeded"
        append_finished(finished, "owner/repo", "owner/repo,,,FALSE", fail_row)
        # Each clone starts its log afresh. Only a run that skips the repository keeps the
        # line below.
        log = cfg.log_dir / "owner§repo.log"
        log.parent.mkdir(parents=True)
        cloned = []
        for _ in range(2):
            log.write_text("not cloned since\n", encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()):
                _, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                    finished_path=finished)
            cloned.append("not cloned since" not in log.read_text())
            self.assertEqual(fail_rows, [fail_row])
        self.assertEqual(cloned, [True, False])
        self.assertEqual(read_finished(finished)["owner/repo"][5], 1024 * 1024)

    def test_clone_failure_leaves_no_collected_dir(self):
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path")
        process_repo(self.cfg, "owner/repo")
        self.assertFalse((self.cfg.collected_dir / "owner§repo").exists())

    def test_fail_row_never_contains_github_access_token(self):
        cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", token="SECRETTOKEN", max_tries=1, base_delay=0,
        )
        self._redirect("http://127.0.0.1:9/nonexistent/repo/path",
                       original="https://x-access-token@github.com/owner/repo.git")
        _, _, fail_row, _facts, _subs, _measures = process_repo(cfg, "owner/repo")
        self.assertNotIn("SECRETTOKEN", fail_row)
        self.assertEqual(fail_row, "owner/repo,https://github.com/owner/repo,clone_failed")

    def test_dotted_repo_name_stays_under_the_collected_dir(self):
        # owner/repo with dots must become a single child directory of collected_dir,
        # never escaping via path traversal.
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        self._redirect(f"file://{self.remote}", original="https://github.com/my.org/my.repo.git")
        process_repo(self.cfg, "my.org/my.repo")
        out = self.cfg.collected_dir / "my.org§my.repo"
        self.assertTrue(out.exists())
        self.assertEqual(out.resolve().parent, self.cfg.collected_dir.resolve())


class TestReadingTheCommitHistory(unittest.TestCase):
    """Reading the commit history in the cloning step, from a shard per repository to the
    assembled CSVs.

    The cloning step has the clone and has fetched its history for the rows of
    `file_history.csv`. It reads the commits there rather than in a second clone of every
    repository, as a stream with one commit in memory at a time. It writes the matched rows as
    it finds them and keeps the unmatched attributions with the shas and lines that they came
    from. It also keeps a sample of the commits with no attribution at all, which turns the
    ranking of the unmatched attributions into a bound.
    """

    def test_a_log_that_cannot_be_kept_is_recollected_on_resume(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\n")
        with mock.patch.object(commits, "_store_log", side_effect=OSError(28, "No space left on device")):
            self.assertEqual(self._run_main("--retry-rounds", "0", "--max-clone-failures", "0"), 4)
        rows, record = self._shard()
        self.assertEqual((record["outcome"], record["commits_matched"], len(rows)), ("history_read", 1, 1))
        self.assertFalse(commits.stored_log_path(self.commits, "owner/repo").exists())
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo"))
        self.assertEqual(commits.reclassify(self.commits, "owner/repo"), "no_log")
        self.assertIn("Could not keep the commit log", self.err)
        self.assertIn(str(commits.stored_log_path(self.commits, "owner/repo")), self.err)
        self.assertEqual(self._run_main("--retry-rounds", "0", "--max-clone-failures", "0"), 0)
        self.assertIn("Collecting 1 repositories again to retry them", self.err)
        self.assertTrue(commits.stored_log_path(self.commits, "owner/repo").is_file())
        self.assertFalse(_resume.history_needs_reread(self.commits, "owner/repo"))
        self.assertEqual(commits.reclassify(self.commits, "owner/repo"), "current")


    CLAUDE = "feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\n"
    # Bounds that differ from the defaults of the step and of `measure_collected_files`, so
    # that a test can tell a history fetch under a default from a fetch under these bounds.
    BOUNDS = {"clone_timeout": 321.0, "max_clone_bytes": 654 * 1024 ** 2}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.commits = self.tmp / "collect_commits"
        self.cfg = CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "scratch",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0, commits_dir=self.commits)
        self.saved = {}
        _ambient_env.take(self.saved)
        self.addCleanup(_ambient_env.restore, self.saved)

    def tearDown(self):
        for k in ["GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
                  "GIT_CONFIG_KEY_1", "GIT_CONFIG_VALUE_1"]:
            os.environ.pop(k, None)

    def _redirect(self, repo="owner/repo"):
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = f"https://github.com/{repo}.git"

    def _assert_fetched_under(self, fetched: list, config: CollectionConfig) -> None:
        """Assert that every history fetch ran on the clone under the run's time and size
        bounds. The fakes record the bounds, and a worker that dropped them from the fetch fails
        this check."""
        self.assertTrue(fetched, "no history was fetched")
        clone = str(config.clone_dir / "owner§repo")
        for call in fetched:
            self.assertEqual(call, {"repo_dir": clone, "timeout": config.clone_timeout,
                                    "bounded_path": clone,
                                    "max_clone_bytes": config.max_clone_bytes})

    def _shard(self, repo="owner/repo") -> tuple:
        csv_path, json_path = commits.shard_paths(self.commits, repo)
        rows = list(csv.DictReader(csv_path.open(encoding="utf-8"))) if csv_path.exists() else None
        record = json.loads(json_path.read_text()) if json_path.exists() else None
        return rows, record

    def test_a_line_naming_a_tool_in_an_unmatched_commit_is_kept_by_its_phrasing(self):
        """An attribution in prose has no trailer and no identity, and the unmatched
        attributions therefore do not contain it. Without the mentions, a new phrasing would
        stay invisible until someone read the commit histories of the sample again. Two commits
        of one phrasing are one entry, and a commit that a heuristic matched adds no entry."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        first = _commit_message(self.remote, "a.txt", "a\n", "Tune the build\n\n"
                                "Written alongside Cursor 1.2, see https://cursor.com/x\n")
        second = _commit_message(self.remote, "b.txt", "b\n",
                                 "written alongside cursor 1.7, see https://cursor.com/y")
        _commit_message(self.remote, "CLAUDE.md", "# v2\n" + "y" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        _, record = self._shard()
        self.assertEqual([(m["tool"], m["pattern"], m["commits"]) for m in record["mentions"]],
                         [("cursor", "written alongside cursor 0.0, see <url>", 2)])
        self.assertEqual(sorted(record["mentions"][0]["shas"]), sorted([first, second]))
        out = self.tmp / "output" / "all_ai_commits.csv"
        counts = commits.assemble_ai_commits(self.commits, {"owner/repo": (_ROW, None, [], [])},
                                             out, None)
        self.assertEqual(counts["mentions"], 1)
        with open(out.with_name(commits.MENTIONS_NAME), newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["tool"], r["commits"], r["repos"]) for r in rows],
                         [("cursor", "2", "1")])
        # The line is verbatim from whichever of the two commits the cloning step read first.
        self.assertIn(rows[0]["line"], ("Written alongside Cursor 1.2, see https://cursor.com/x",
                                        "written alongside cursor 1.7, see https://cursor.com/y"))

    def test_the_phrasings_per_tool_and_repository_are_bounded_and_the_rest_counted(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        with mock.patch.object(commits, "MENTION_PATTERNS", 2):
            for word in ("alpha", "beta", "gamma"):
                _commit_message(self.remote, f"{word}.txt", "x\n", f"Move the cursor to {word}")
            process_repo(self.cfg, "owner/repo")
        _, record = self._shard()
        self.assertEqual(len(record["mentions"]), 2)
        self.assertEqual(record["mentions_dropped"], {"cursor": 1})

    def test_a_matched_commit_becomes_a_row_with_its_rules(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        sha = _commit_message(self.remote, "CLAUDE.md", "# v2\n" + "y" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertEqual([r["commit_sha"] for r in rows], [sha])
        self.assertEqual(rows[0]["ai_tool"], "Claude")
        self.assertEqual(list(rows[0]), list(commits.COMMIT_COLUMNS))
        ids = {row["id"] for row in commits.AI_COMMIT_PATTERNS}
        self.assertTrue(rows[0]["matched_heuristics"])
        self.assertLessEqual(set(rows[0]["matched_heuristics"].split(";")), ids)
        self.assertEqual(rows[0]["attributions"], "Claude:standard")
        self.assertEqual(rows[0]["github_link"],
                         f"https://github.com/owner/repo/commit/{sha}")
        self.assertIn("Co-authored-by: Claude", rows[0]["commit_message"])
        self.assertEqual((record["outcome"], record["commits_read"],
                          record["commits_matched"]), ("history_read", 3, 1))

    AMP = "feat: y\n\nCo-authored-by: Amp <amp@ampcode.com>\n"

    def _other_shard(self, repo="owner/repo"):
        path = commits.other_shard_path(self.commits, repo)
        return list(csv.DictReader(path.open(encoding="utf-8"))) if path.exists() else None

    def test_a_commit_by_a_tool_outside_the_six_goes_to_its_own_shard(self):
        """A heuristic explains the commit's attribution, and the commit is not in
        the unmatched attributions either. People review that list for attributions that no
        heuristic explains."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        sha = _commit_message(self.remote, "CLAUDE.md", "# v2\n" + "y" * 90, self.AMP)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertEqual(rows, [])
        other = self._other_shard()
        self.assertEqual([(r["commit_sha"], r["ai_tool"]) for r in other], [(sha, "Amp")])
        self.assertEqual(other[0]["matched_heuristics"], "amp-trailer-01")
        self.assertEqual(other[0]["attributions"], "Amp:standard")
        self.assertEqual((record["commits_matched"], record["commits_other_tools"]), (0, 1))
        self.assertEqual([e["token"] for e in record["unmatched"]], [])

    def test_a_commit_crediting_a_tool_of_each_kind_is_in_both_shards(self):
        sha = _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90,
                              self.CLAUDE + "Co-authored-by: Amp <amp@ampcode.com>\n")
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertEqual([(r["commit_sha"], r["ai_tool"]) for r in rows], [(sha, "Claude")])
        self.assertEqual([(r["commit_sha"], r["ai_tool"]) for r in self._other_shard()],
                         [(sha, "Amp")])
        self.assertEqual((record["commits_matched"], record["commits_other_tools"]), (1, 1))

    def test_the_assembled_csv_and_the_count_contain_the_other_tools(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.AMP)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        out = self.tmp / "output" / "all_ai_commits.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            counts = commits.assemble_ai_commits(self.commits, {"owner/repo": (_ROW, None, [], [])},
                                                 out, None)
        other = list(csv.DictReader(out.with_name("other_ai_commits.csv").open(encoding="utf-8")))
        self.assertEqual([(r["repo_name"], r["ai_tool"]) for r in other], [("owner/repo", "Amp")])
        self.assertEqual(list(other[0]), list(commits.COMMIT_COLUMNS))
        self.assertEqual((counts["rows"], counts["other_rows"]), (0, 1))
        repos = {r["repo_name"]: r for r in
                  csv.DictReader(out.with_name("ai_commits-repos.csv").open(encoding="utf-8"))}
        self.assertEqual((repos["owner/repo"]["commits_matched"],
                          repos["owner/repo"]["commits_other_tools"]), ("0", "1"))

    def test_collecting_a_repository_again_removes_its_other_shard(self):
        """A shard left from an earlier run would keep rows that the clone of this run did not
        produce. `remove_shards` prevents that for the shard of the commit CSV, and it has to
        remove the shard of the other tools too."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.AMP)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        self.assertTrue(commits.other_shard_path(self.commits, "owner/repo").exists())
        commits.remove_shards(self.commits, "owner/repo")
        self.assertFalse(commits.other_shard_path(self.commits, "owner/repo").exists())

    def test_a_repository_with_no_matching_commit_is_read_and_sampled(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertEqual(rows, [])
        self.assertEqual(record["commits_matched"], 0)
        self.assertEqual(record["commits_read"], 2)
        self.assertEqual(len(record["no_signal_shas"]), 2)

    def test_the_unmatched_attributions_contain_shas_and_the_whole_line(self):
        """The history has one matched commit and three unmatched commits, one of each kind: a
        trailer that no heuristic explains, an identity that looks like a bot, and a commit with
        no attribution at all, which only the no-signal sample records."""
        plain = subprocess.run(["git", "-C", str(self.remote), "rev-parse", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        person = _commit_message(self.remote, "CLAUDE.md", "# v3\n" + "z" * 90,
                                 "chore: y\n\nCo-authored-by: Some Person <p@example.com>\n")
        bot = _commit_message(self.remote, "CLAUDE.md", "# v4\n" + "w" * 90, "chore: bump\n",
                              author="renovate[bot]")
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertEqual((record["commits_read"], record["commits_matched"], len(rows)),
                         (4, 1, 1))
        by_kind = {e["kind"]: e for e in record["unmatched"]}
        self.assertEqual(sorted(by_kind), ["author", "trailer"])
        self.assertEqual(by_kind["trailer"]["token"], "co-authored-by: some person")
        self.assertEqual(by_kind["trailer"]["shas"], [person])
        self.assertEqual(by_kind["trailer"]["lines"],
                         ["Co-authored-by: Some Person <p@example.com>"])
        self.assertEqual(by_kind["trailer"]["commits"], 1)
        self.assertEqual(by_kind["author"]["shas"], [bot])
        self.assertEqual(by_kind["author"]["lines"], ["renovate[bot] <renovate[bot]@example.com>"])
        self.assertEqual(record["no_signal_shas"], [plain])

    def test_a_commit_crediting_one_name_under_two_addresses_counts_once(self):
        """The CSV of unmatched attributions counts commits. A squash merge with the same
        co-author under two addresses is one commit with two lines, not two commits."""
        sha = _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90,
                              "chore: squash\n\nCo-authored-by: Some Person <p@example.com>\n"
                              "Co-authored-by: Some Person <person@work.example>\n")
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        _, record = self._shard()
        entry = next(e for e in record["unmatched"] if e["kind"] == "trailer")
        self.assertEqual((entry["commits"], entry["shas"]), (1, [sha]))
        self.assertEqual(entry["lines"], ["Co-authored-by: Some Person <p@example.com>",
                                          "Co-authored-by: Some Person <person@work.example>"])

    def test_a_non_utf8_commit_body_is_read(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        subprocess.run(["git", "-C", str(self.remote), "commit", "-q", "--allow-empty", "-F", "-"],
                       input="caf\xe9\n\nCo-authored-by: Claude <noreply@anthropic.com>\n"
                             .encode("latin-1"), capture_output=True, check=True)
        self._redirect()
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_1"] = "i18n.logOutputEncoding"
        os.environ["GIT_CONFIG_VALUE_1"] = "latin1"
        process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertEqual(record["outcome"], "history_read")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["commit_message"].startswith("caf"))

    def test_reading_stopped_by_the_bound_is_recorded_and_the_history_rows_kept(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        original = _mod.bounds.run_bounded

        def stopped_read(command, **kwargs):
            if "i18n.logOutputEncoding=UTF-8" in command:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return original(command, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=stopped_read):
            _, _, fail_row, facts, _, _measures = process_repo(self.cfg, "owner/repo")
        rows, record = self._shard()
        self.assertIsNone(fail_row)
        self.assertEqual(record["outcome"], "history_read_failed")
        self.assertIn("TimeoutExpired", record["detail"])
        self.assertIsNone(rows)
        self.assertEqual([r["artifact"] for r in facts], ["CLAUDE.md"])

    def test_the_per_repository_commit_record_contains_the_ai_columns_of_each_copied_file(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        _, record = self._shard()
        self.assertEqual(len(record["ai_rows"]), 1)
        self.assertEqual(record["ai_rows"][0]["artifact"], "CLAUDE.md")
        self.assertEqual(record["ai_rows"][0]["first_commit_ai_created"], "TRUE")
        self.assertEqual(record["ai_rows"][0]["#ai_commits"], "1")

    def test_reclassification_uses_the_file_commits_written_by_collection(self):
        created = _commit_message(self.remote, "CLAUDE.md", "Instructions\n" + "x" * 90,
                                  "Add instructions\n")
        developed = _commit_message(self.remote, "CLAUDE.md", "Updated\n" + "y" * 90,
                                    "Update\n\nCo-developed-by: Claude <noreply@anthropic.com>\n")
        _commit_message(self.remote, "unrelated.txt", "Unrelated\n", "Add another file\n")
        self._redirect()
        with mock.patch.object(commits, "detect_ai_tools_in_fields", return_value=[]), \
                contextlib.redirect_stderr(io.StringIO()):
            process_repo(self.cfg, "owner/repo")
        record = self._shard()[1]
        self.assertEqual([(e["artifact"], e["first"], sorted(e["commits"]))
                          for e in record["path_commits"]],
                         [("CLAUDE.md", created, sorted([created, developed]))])
        self.assertEqual(record["ai_rows"][0]["#ai_commits"], "0")
        record["classified_by"] = "an older image"
        commits.write_shard_record(commits.shard_paths(self.commits, "owner/repo")[1], record)
        self.assertEqual(commits.reclassify(self.commits, "owner/repo"), "reclassified")
        row = self._shard()[1]["ai_rows"][0]
        self.assertEqual((row["artifact"], row["#ai_commits"], row["first_commit_ai_created"]),
                         ("CLAUDE.md", "1", "FALSE"))

    def test_a_repository_with_nothing_copied_has_no_history_read(self):
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        self.assertEqual(self._shard(), (None, None))

    def _run_main(self, *extra) -> int:
        out = self.tmp / "collected"
        argv = ["--repo-csv", str(self.tmp / "list.csv"), "--collected-dir", str(out),
                "--collected-csv", str(out / "repos.csv"),
                "--fail-csv", str(out / "clone_failed.csv"),
                "--finished", str(self.tmp / "list of finished repositories.jsonl"),
                "--file-history", str(self.tmp / "fh.csv"),
                "--submodules", str(self.tmp / "sub.csv"),
                *_ai_commit_flags(self.tmp),
                "--clone-dir", str(self.tmp / "scratch"), "-l", str(self.tmp / "logs"),
                "-w", "1", "--max-tries", "1", "--min-free-gb", "0", "--retry-pause", "0", *extra]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(argv)
        self.err = err.getvalue()
        return code

    def test_main_prunes_vendored_plugins_and_context_files(self):
        for path, content in {
            "node_modules/pkg/.claude-plugin/plugin.json": '{"name":"pkg"}',
            "node_modules/pkg/skills/review/SKILL.md": "Review.\n",
            "node_modules/pkg/CLAUDE.md": "Instructions.\n",
            "src/app.py": "print('hello')\n",
        }.items():
            _add_commit(self.remote, path, content)
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\n")
        self._redirect()
        self.assertEqual(self._run_main(), 0)
        with (self.tmp / "collected/repos.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repo_name"], "owner/repo")
        self.assertEqual(rows[0]["claude"], "FALSE")
        self.assertEqual(rows[0]["claude.md"], "FALSE")
        self.assertFalse((self.tmp / "collected/owner§repo").exists())
        self.assertTrue((self.tmp / "fh.csv").is_file())
        cache = caches.FileHistoryCache.load(self.tmp / "fh.csv")
        self.assertEqual(len(cache), 0)

    def test_main_assembles_the_commit_csvs_and_merges_the_cache(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        (self.tmp / "list.csv").write_text(
            "repo_name\nowner/repo\nowner/gone\nowner/unavailable\n")
        self._redirect()
        os.environ["GIT_CONFIG_COUNT"] = "3"
        os.environ["GIT_CONFIG_KEY_1"] = "url.http://127.0.0.1:9/nonexistent.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/owner/gone.git"
        missing = (self.tmp / "missing.git").as_uri()
        os.environ["GIT_CONFIG_KEY_2"] = f"url.{missing}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_2"] = "https://github.com/owner/unavailable.git"
        self.assertEqual(self._run_main(), 0)
        output = self.tmp / "output"
        rows = list(csv.DictReader((output / "all_ai_commits.csv").open(encoding="utf-8")))
        self.assertEqual([(r["repo_name"], r["ai_tool"]) for r in rows], [("owner/repo", "Claude")])
        unmatched = list(csv.DictReader((output / "all_ai_commits-unmatched.csv").open()))
        self.assertEqual(unmatched, [])
        repos = {r["repo_name"]: r for r in
                  csv.DictReader((output / "ai_commits-repos.csv").open(encoding="utf-8"))}
        self.assertEqual(repos["owner/repo"]["outcome"], "history_read")
        self.assertEqual(repos["owner/repo"]["commits_read"], "2")
        # Both failure outcomes come from local remotes with fixed failure conditions.
        failed = {r["repo_name"]: r["reason"] for r in
                  csv.DictReader((self.tmp / "collected" / "clone_failed.csv").open())}
        self.assertEqual(failed, {"owner/gone": "clone_failed",
                                  "owner/unavailable": "not_found_or_no_access"})
        for repo, outcome in failed.items():
            self.assertEqual(repos[repo]["outcome"], outcome)
            self.assertEqual(repos[repo]["commits_read"], "")
        cache = caches.AiAuthorshipCache.load(self.tmp / "ai_authorship.csv")
        self.assertEqual(len(cache), 1)
        self.assertEqual(next(iter(cache.keys())).artifact, "CLAUDE.md")

    def test_a_history_that_cannot_be_fetched_is_in_ai_commits_repos_csv(self):
        """For a repository with copied files and a history that the fetch did not retrieve,
        `ai_commits-repos.csv` records why the commit history was not read rather than
        `no_files`."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        with mock.patch.object(_mod.history.GitFileMetadata, "ensure_full_history",
                               return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            _, _, fail_row, facts, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(facts, [])
        rows, record = self._shard()
        self.assertIsNone(rows)
        self.assertEqual(record["outcome"], "history_unavailable")
        self.assertEqual(record["repo_name"], "owner/repo")
        out = self.tmp / "output" / "all_ai_commits.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            commits.assemble_ai_commits(self.commits, {"owner/repo": (_ROW, None, [], [])},
                                     out, None)
        repos = {r["repo_name"]: r for r in
                  csv.DictReader(out.with_name("ai_commits-repos.csv").open(encoding="utf-8"))}
        self.assertEqual(repos["owner/repo"]["outcome"], "history_unavailable")

    def test_reading_that_the_machine_ended_is_redone_by_the_next_run(self):
        """A resumed run reads a commit history again when reading it raised an error of the
        machine, such as a refused `fork()`, or when the fetch did not retrieve the history.
        Keeping an `ok` line in the list of finished repositories with a per-repository commit
        record of the failure would leave the repository out of the commit CSV permanently."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        real = _mod.bounds.run_bounded

        def refusing(cmd, **kwargs):
            if "i18n.logOutputEncoding=UTF-8" in cmd:  # the history query, not the measurement's
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing), \
                contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                finished_path=finished)
        self.assertEqual(fail_rows, [])
        self.assertEqual(self._shard()[1]["outcome"], "history_read_failed")
        self.assertTrue(self._shard()[1]["transient"])
        self.assertTrue(read_finished(finished)["owner/repo"][4],
                        "the list of finished repositories records redo")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them (outcome clone_failed or "
                      "collector_error",
                      err.getvalue())
        self.assertEqual(self._shard()[1]["outcome"], "history_read")
        self.assertFalse(read_finished(finished)["owner/repo"][4])

    def test_reading_that_git_refused_or_the_clock_ended_is_not_redone(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        cfg = self.cfg
        real = _mod.bounds.run_bounded

        def slow_read(cmd, **kwargs):
            if "i18n.logOutputEncoding=UTF-8" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            return real(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=slow_read), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertEqual(self._shard()[1]["outcome"], "history_read_failed")
        self.assertFalse(self._shard()[1]["transient"])
        self.assertFalse(read_finished(finished)["owner/repo"][4])

    def test_a_history_that_did_not_arrive_under_the_clock_is_recorded_and_not_redone(self):
        """Under the same time or size bound, the fetch fails the same way on every Start.
        Fetching again would cost a clone and two attempts under the timeout for each such
        repository on every Start, with exit 0 and no log message. At its end, the run logs the
        number of these repositories."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\n")
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        bounded = dataclasses.replace(self.cfg, **self.BOUNDS)
        fetched = []
        with mock.patch.object(_mod.history.GitFileMetadata, "ensure_full_history",
                               _history_not_fetched("time_bound", fetched)), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(bounded, ["owner/repo"], workers=1, finished_path=finished)
        self._assert_fetched_under(fetched, bounded)
        self.assertEqual(self._shard()[1]["outcome"], "history_unavailable")
        self.assertEqual(self._shard()[1]["cause"], "time_bound")
        self.assertFalse(self._shard()[1]["transient"])
        self.assertFalse(read_finished(finished)["owner/repo"][4])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(bounded, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 0 repositories again to retry them", err.getvalue())
        with mock.patch.object(_mod.history.GitFileMetadata, "ensure_full_history",
                               _history_not_fetched("time_bound", [])):
            code = self._run_main("--clone-timeout", str(bounded.clone_timeout),
                                  "--max-clone-mb", str(bounded.max_clone_bytes // 1024 ** 2))
        self.assertEqual(code, 0)
        self.assertIn("Could not fetch the commit histories of 1 repositories", self.err)
        self.assertIn("owner/repo", self.err)

    def test_a_history_that_did_not_arrive_is_fetched_again_under_a_larger_clock(self):
        # The run's last log message recommends a larger timeout. A run under a larger timeout
        # fetches the histories again, and a resumed run under the same timeout skips them.
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        short = dataclasses.replace(self.cfg, clone_timeout=2.0,
                                    max_clone_bytes=self.BOUNDS["max_clone_bytes"])
        fetched = []
        with mock.patch.object(_mod.history.GitFileMetadata, "ensure_full_history",
                               _history_not_fetched("time_bound", fetched)), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(short, ["owner/repo"], workers=1, finished_path=finished)
        self._assert_fetched_under(fetched, short)
        record = self._shard()[1]
        self.assertEqual((record["outcome"], record["time_bound"], record["size_bound"]),
                         ("history_unavailable", 2.0, short.max_clone_bytes))
        self.assertFalse(_resume.history_needs_reread(self.commits, "owner/repo", 2.0, short.max_clone_bytes))
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo", 600.0, short.max_clone_bytes))
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo", 2.0, short.max_clone_bytes * 2))
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo", 0, 0), "no bound is a larger one")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(short, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 0 repositories again to retry them", err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, _, facts, _ = run_collection(dataclasses.replace(self.cfg, clone_timeout=600.0),
                                            ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertEqual(self._shard()[1]["outcome"], "history_read")
        self.assertTrue(facts)

    def test_a_history_fetch_the_network_failed_is_fetched_again_by_the_next_run(self):
        """A fetch that exited non-zero twice, such as after a 5xx during
        `git fetch --unshallow`, is a transient failure, and the next run fetches the history
        again. Recording it as a history that did not arrive under the bounds would mean that no
        run under these bounds fetches it again."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        bounded = dataclasses.replace(self.cfg, **self.BOUNDS)
        fetched = []
        with mock.patch.object(_mod.history.GitFileMetadata, "ensure_full_history",
                               _history_not_fetched("exit", fetched)), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(bounded, ["owner/repo"], workers=1, finished_path=finished)
        self._assert_fetched_under(fetched, bounded)
        record = self._shard()[1]
        self.assertEqual((record["outcome"], record["cause"], record["transient"]),
                         ("history_unavailable", "exit", True))
        self.assertTrue(read_finished(finished)["owner/repo"][4])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, _, facts, _ = run_collection(bounded, ["owner/repo"], workers=1,
                                            finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertEqual(self._shard()[1]["outcome"], "history_read")
        self.assertTrue(facts)

    def test_the_fetch_reports_its_cause(self):
        # The extractor records why the last fetch did not retrieve the history.
        # `tests.test_git_history` tests the timeout with two fetches that a real timeout ends,
        # because a mocked timeout leaves none of the locks that a killed fetch leaves.
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.remote}", str(clone)],
                       check=True, capture_output=True)
        meta = _mod.history.GitFileMetadata()
        real = _mod.bounds.run_bounded

        def over_the_size_bound(cmd, **kwargs):
            if "fetch" in cmd:
                raise _mod.bounds.GitSizeBoundExceeded(clone, 10, 5)
            return real(cmd, **kwargs)
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=over_the_size_bound), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(meta.ensure_full_history(str(clone)))
        self.assertEqual(meta.history_failure, "size_bound")

        def non_zero(cmd, **kwargs):
            if "fetch" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: the remote end hung up")
            return real(cmd, **kwargs)
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=non_zero), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(meta.ensure_full_history(str(clone)))
        self.assertEqual(meta.history_failure, "exit")

    def test_a_retry_of_an_ok_record_is_in_the_finished_before_it_starts(self):
        """A Stop after the clone of a retried repository and before its new line in the list of
        finished repositories must not leave the old line in force for the new tree. The
        per-repository commit record would then be gone, and the next Start would count the
        repository as done."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        shutil.rmtree(self.cfg.collected_dir / "owner§repo")
        real = collector.process_repo

        def stops_after_the_clone(config, repo, preserve_on=frozenset()):
            # The Stop arrives after the clone and the copy, once the shards are removed.
            raise KeyboardInterrupt

        with mock.patch.object(collector, "process_repo", stops_after_the_clone), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(KeyboardInterrupt):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        records = [json.loads(l) for l in finished.read_text().splitlines() if l.strip()]
        self.assertEqual(len(records), 2, "the retry was written before it started")
        self.assertTrue(records[-1].get("redo"))
        self.assertTrue(read_finished(finished)["owner/repo"][4])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertTrue((self.cfg.collected_dir / "owner§repo" / "CLAUDE.md").is_file())
        self.assertFalse(read_finished(finished)["owner/repo"][4])

    def test_a_tree_left_with_empty_directories_is_no_tree_and_sets_no_flag(self):
        """`remove_undecodable_names` removes the entries with names that are not UTF-8 and
        leaves their decodable parents. An empty `.claude/` must not pass the check before the
        commit history is read. Otherwise, the collector would read the history from the
        depth-1 clone, and the row's flags would record files that are not there. Every Start
        would then collect the repository again for a missing tree."""
        _commit_message(self.remote, ".claude/settings.json", "{}", self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"

        def files_only(root):
            names = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
            for p in list(root.rglob("*")):
                if p.is_file():
                    p.unlink()
            return names

        with mock.patch.object(paths, "remove_undecodable_names", side_effect=files_only), \
                contextlib.redirect_stderr(io.StringIO()):
            main_rows, fail_rows, _, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                        finished_path=finished)
        self.assertEqual(fail_rows, [])
        self.assertFalse((self.cfg.collected_dir / "owner§repo").exists())
        self.assertEqual(self._shard(), (None, None), "the commit history was not read")
        self.assertNotIn("TRUE", main_rows[0].split(",")[6:], "no flag for no file")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 0 repositories again to retry them", err.getvalue())

    def test_a_retry_of_the_history_keeps_the_earlier_tree_when_the_clone_fails(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        json_path = commits.shard_paths(self.commits, "owner/repo")[1]
        json_path.write_text(json.dumps({"repo_name": "owner/repo", "outcome": "history_read_failed",
                                         "transient": True}) + "\n")
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/nonexistent/nowhere.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                finished_path=finished)
        self.assertEqual(fail_rows, [])
        self.assertTrue((self.cfg.collected_dir / "owner§repo" / "CLAUDE.md").is_file())
        record = json.loads(finished.read_text().splitlines()[-1])
        self.assertIsNone(record.get("fail_row"))
        self.assertTrue(record["redo"])

    def test_a_failed_history_query_is_retried_in_the_next_round(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        finished = self.tmp / "finished.jsonl"
        real = _mod.history.GitFileMetadata._run_git_command
        failing = [True]

        def follow(reader, command, cwd, **kwargs):
            if failing[0] and "--follow" in command:
                return subprocess.CompletedProcess(command, 128, "", "fatal: HTTP 400")
            return real(reader, command, cwd, **kwargs)

        paused = []

        def pause(seconds):
            paused.append(seconds)
            failing[0] = False

        cfg = dataclasses.replace(self.cfg, retry_rounds=2, retry_pause=300.0)
        with mock.patch.object(_mod.history.GitFileMetadata, "_run_git_command", follow), \
                contextlib.redirect_stderr(io.StringIO()):
            _, failures, facts, _ = _mod.collect_with_retries(
                cfg, ["owner/repo"], workers=1, finished_path=finished, sleep=pause)
        self.assertEqual(paused, [300.0])
        self.assertEqual(failures, [])
        self.assertEqual([row["artifact"] for row in facts], ["CLAUDE.md"])
        self.assertEqual(self._shard()[1]["history_failed"], [])
        self.assertFalse(read_finished(finished)["owner/repo"][4])

    def test_a_per_repository_commit_record_without_the_bounds_is_redone_once(self):
        # A record without the bounds contains `transient: False` and no `time_bound`. The test
        # of the bounds alone would never redo it, although the run's last log message
        # recommended a larger timeout.
        json_path = commits.shard_paths(self.commits, "owner/repo")[1]
        self.commits.mkdir()
        json_path.write_text(json.dumps({"repo_name": "owner/repo",
                                         "outcome": "history_unavailable",
                                         "transient": False}) + "\n")
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo", 900, 0))
        json_path.write_text(json.dumps({"repo_name": "owner/repo",
                                         "outcome": "history_unavailable",
                                         "transient": True, "time_bound": 900, "size_bound": 0}) + "\n")
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo", 900, 0),
                        "a fetch that failed on the network, whatever the bounds")

    def test_reading_that_the_clock_ended_is_repeated_under_a_larger_clock(self):
        # When the timeout ended the reading of the commit history, a run under a larger timeout
        # reads the history again, as it fetches again a history that the same timeout stopped.
        # Otherwise, the record would be `history_read_failed` without the timeout, the run's
        # end would log nothing, and no run under any bound would read the history again.
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\n")
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        real = _mod.bounds.run_bounded

        def slow_read(cmd, **kwargs):
            if "i18n.logOutputEncoding=UTF-8" in cmd:
                raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))
            return real(cmd, **kwargs)

        short = dataclasses.replace(self.cfg, clone_timeout=2.0)
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=slow_read), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(short, ["owner/repo"], workers=1, finished_path=finished)
        record = self._shard()[1]
        self.assertEqual((record["outcome"], record["cause"], record["time_bound"]),
                         ("history_read_failed", "time_bound", 2.0))
        self.assertFalse(_resume.history_needs_reread(self.commits, "owner/repo", 2.0, 0))
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo", 600.0, 0))
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=slow_read):
            self._run_main("--clone-timeout", "2")
        self.assertIn("Reading the commit history timed out for 1 repositories, such as", self.err)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(dataclasses.replace(self.cfg, clone_timeout=600.0), ["owner/repo"],
                           workers=1, finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertEqual(self._shard()[1]["outcome"], "history_read")

    def test_the_cause_is_the_network_once_seen(self):
        # Only two timeouts give the cause `time_bound`, and `tests.test_git_history` tests that
        # cause with two fetches that a real timeout ends.
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        clone = self.tmp / "clone"
        subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{self.remote}", str(clone)],
                       check=True, capture_output=True)
        real = _mod.bounds.run_bounded
        for attempts, cause in (((subprocess.CompletedProcess(["git"], 128, "", "hung up"),
                                  subprocess.TimeoutExpired(["git"], 1)), "exit"),
                                ((subprocess.TimeoutExpired(["git"], 1),
                                  subprocess.CompletedProcess(["git"], 128, "", "hung up")), "exit"),
                                ((MemoryError(), MemoryError()), "exit")):
            answers = list(attempts)

            def fetching(cmd, **kwargs):
                if "fetch" in cmd:
                    answer = answers.pop(0)
                    if isinstance(answer, Exception):
                        raise answer
                    return answer
                return real(cmd, **kwargs)

            meta = _mod.history.GitFileMetadata()
            with mock.patch.object(_mod.bounds, "run_bounded", side_effect=fetching), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertFalse(meta.ensure_full_history(str(clone)))
            self.assertEqual(meta.history_failure, cause, attempts)

    def test_the_first_flag_column_follows_the_metadata_columns(self):
        self.assertEqual(paths.FLAG_COLUMNS_START, len(paths.METADATA_COLUMNS))
        self.assertEqual(paths.COLLECTED_CSV_HEADER.split(",")[paths.FLAG_COLUMNS_START],
                         paths.FLAG_COLUMNS[0])

    def test_a_branch_named_true_is_not_a_file(self):
        row = paths.build_csv_row("owner/repo", "t", "TRUE", "a" * 40, "d", "", set())
        self.assertFalse(_resume.row_has_files(row))
        self.assertTrue(_resume.row_has_files(paths.build_csv_row("owner/repo", "t", "main", "a" * 40,
                                                              "d", "", {"cursor"})))

    def test_a_collector_error_leaves_no_collected_tree(self):
        # If the tree stayed in place after a run that ended under the ceiling, the collected
        # file cleanup would receive it with a row that records nothing copied. The repository
        # would then be published with a blank branch and head_sha.
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        bounded = _mod.bounds.run_bounded

        def refusing_root(cmd, **kwargs):
            if "rev-list" in cmd and "--max-parents=0" in cmd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return bounded(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing_root), \
                contextlib.redirect_stderr(io.StringIO()):
            _, _, fail_row, _, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(_resume.outcome_of(fail_row), "collector_error")
        self.assertFalse((self.cfg.collected_dir / "owner§repo").exists())

    def test_a_tree_emptied_of_names_that_are_not_utf8_is_no_tree(self):
        # The emptied directory would pass the check before the commit history is read. The
        # collector would then read the history from the depth-1 clone without fetching it and
        # record `history_read` with one commit.
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()

        def remove_everything(root):
            names = [str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()]
            for p in list(root.rglob("*")):
                if p.is_file():
                    p.unlink()
            return names

        with mock.patch.object(paths, "remove_undecodable_names", side_effect=remove_everything), \
                contextlib.redirect_stderr(io.StringIO()):
            _, _, fail_row, facts, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(facts, [])
        self.assertFalse((self.cfg.collected_dir / "owner§repo").exists())
        self.assertEqual(self._shard(), (None, None),
                         "the commit history was not read, since nothing was copied")

    def test_a_collected_tree_that_is_gone_is_found_by_the_row_too(self):
        # An archive can restore the list of finished repositories and the two lists without the
        # per-repository commit records. A check that read only the record would find nothing
        # pending among the repositories that the archive restores.
        cfg = dataclasses.replace(self.cfg, commits_dir=None)
        row_with_files = paths.build_csv_row("owner/repo", "t", "main", "a" * 40, "d", "", {"claude"})
        row_without = paths.build_csv_row("owner/repo", "t", "main", "a" * 40, "d", "", set())
        self.assertTrue(_resume.row_has_files(row_with_files))
        self.assertFalse(_resume.row_has_files(row_without))
        self.assertTrue(_resume.tree_missing(cfg, "owner/repo", row_with_files))
        self.assertFalse(_resume.tree_missing(cfg, "owner/repo", row_without))
        (cfg.collected_dir / "owner§repo").mkdir(parents=True)
        self.assertFalse(_resume.tree_missing(cfg, "owner/repo", row_with_files))

    def test_a_shard_whose_header_cannot_be_read_is_unreadable_not_fatal(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        for name, content in (("owner/latin", b"repo_name,commit_sha\xe9\nx,y\n"),
                              ("owner/empty", b"")):
            csv_path, json_path = commits.shard_paths(self.commits, name)
            csv_path.write_bytes(content)
            json_path.write_text(json.dumps({"repo_name": name, "outcome": "history_read",
                                             "unmatched": [], "no_signal_shas": [],
                                             "ai_rows": []}) + "\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            shards = {repo: (csv_path, record["outcome"]) for repo, csv_path, record
                      in commits.readable_shards(self.commits)}
        self.assertEqual(shards["owner/latin"], (None, "shard_unreadable"))
        self.assertEqual(shards["owner/empty"], (None, "shard_unreadable"))
        self.assertEqual(shards["owner/repo"][1], "history_read")
        self.assertEqual(err.getvalue().count("is not a readable shard"), 2)
        # The two repositories are recorded as done and taken over, with their trees in place.
        # The run reads their shards rather than collecting them again.
        for name in ("outdated", "reclassify"):
            taken_over = mock.patch.object(_mod.reuse, name, lambda *args, **kwargs: set())
            taken_over.start()
            self.addCleanup(taken_over.stop)
        finished = self.tmp / "list of finished repositories.jsonl"
        for name in ("owner/latin", "owner/empty"):
            append_finished(finished, name, paths.build_csv_row(name, "t", "main", "a" * 40, "d", "", set()), None)
            tree = self.tmp / "collected" / name.replace("/", "§")
            tree.mkdir(parents=True)
            (tree / "CLAUDE.md").write_text("# x\n")
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\nowner/latin\nowner/empty\n")
        self.assertEqual(self._run_main(), 0)
        self.assertEqual(self.err.count("owner§latin.csv is not a readable shard"), 1,
                         "the shards are read once per run")

    def test_git_that_could_not_be_started_for_the_history_is_a_collector_error(self):
        # A refused `fork()` at the fetch is not a history that did not arrive. It is a failure
        # of the machine, recorded as collector_error and redone by the next run.
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        real = _mod.bounds.run_bounded

        def refusing_fetch(cmd, **kwargs):
            if "fetch" in cmd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing_fetch), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _, fail_rows, _, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                finished_path=finished)
        self.assertEqual([_resume.outcome_of(r) for r in fail_rows], ["collector_error"])
        self.assertIn("MachineFailure", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, facts, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                    finished_path=finished)
        self.assertEqual(fail_rows, [])
        self.assertTrue(facts)
        self.assertEqual(self._shard()[1]["outcome"], "history_read")

    def test_an_old_record_without_the_flag_is_read_by_its_detail(self):
        """In a tree from a collector that did not record `redo` in the list of finished
        repositories, ok lines stand for commit histories that git could not start reading and
        for histories that did not arrive. Only the per-repository commit record shows the
        failure, by its `transient` flag, or by its `detail` in a record without that flag. The
        resume therefore reads the record beside `finished_repositories.jsonl`."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        json_path = commits.shard_paths(self.commits, "owner/repo")[1]
        old = {"repo_name": "owner/repo", "outcome": "history_read_failed",
               "detail": "BlockingIOError: [Errno 11] Resource temporarily unavailable"}
        json_path.write_text(json.dumps(old) + "\n")
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo"))
        old["detail"] = "TimeoutExpired: Command '['git', 'log']' timed out after 900 seconds"
        json_path.write_text(json.dumps(old) + "\n")
        self.assertFalse(_resume.history_needs_reread(self.commits, "owner/repo"))
        json_path.write_text(json.dumps({"repo_name": "owner/repo",
                                         "outcome": "history_unavailable"}) + "\n")
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo"),
                        "once, to record the cause")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertEqual(self._shard()[1]["outcome"], "history_read")

    def test_a_record_that_does_not_parse_is_collected_again(self):
        """`caches.replace_file` does not sync, and a power loss can therefore leave a
        per-repository commit record empty. None of its commits would reach a CSV, and a
        repository taken over would keep the empty record for every later collection."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        json_path = commits.shard_paths(self.commits, "owner/repo")[1]
        json_path.write_bytes(b"")
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertEqual(self._shard()[1]["outcome"], "history_read")

    def test_a_collected_tree_that_is_gone_is_collected_again(self):
        # An archive can restore the lists and the list of finished repositories without the
        # collected trees. A resumed run that relied on the list of finished repositories alone
        # would record every repository as collected with nothing on disk, and the collected
        # file cleanup would refuse the tree on every later Start.
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        shutil.rmtree(self.cfg.collected_dir / "owner§repo")
        self.assertTrue(_resume.tree_missing(self.cfg, "owner/repo"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            run_collection(self.cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertIn("missing copied files", err.getvalue())
        self.assertIn("Collecting 1 repositories again to retry them", err.getvalue())
        self.assertTrue((self.cfg.collected_dir / "owner§repo" / "CLAUDE.md").is_file())
        self.assertFalse(_resume.tree_missing(self.cfg, "owner/repo"))

    def test_reading_follows_the_fetch_without_a_second_shallow_check(self):
        """A second `is_shallow_repo` at the check would return None under a refused `fork()`.
        The repository's commit history would then be neither read nor recorded as not read.
        `ai_commits-repos.csv` would show `no_files` for a repository with measured files, and
        no later run would read the history."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        real = _mod.history.GitFileMetadata.is_shallow_repo
        answers = []

        def counting(self_, repo_dir):
            answers.append(real(self_, repo_dir))
            return answers[-1]

        with mock.patch.object(_mod.history.GitFileMetadata, "is_shallow_repo", counting), \
                contextlib.redirect_stderr(io.StringIO()):
            process_repo(self.cfg, "owner/repo")
        self.assertEqual(answers, [True, False], "before and after the fetch, and no more")
        self.assertEqual(self._shard()[1]["outcome"], "history_read")

    def test_the_commit_history_is_read_even_when_no_file_history_was(self):
        # A check that required measured files would leave unread the complete history of a
        # repository in which every file's history query failed.
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        real = _mod.history.path_commits
        with mock.patch.object(_mod.history, "path_commits", return_value=None), \
                contextlib.redirect_stderr(io.StringIO()):
            _, _, fail_row, facts, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(facts, [])
        self.assertEqual(self._shard()[1]["outcome"], "history_read")

    def test_a_history_query_that_exits_non_zero_leaves_the_row_out_and_is_redone(self):
        """A query that exits non-zero is not an empty history, because an empty history would
        cache and publish a half-blank row. The repository is not recorded as done either. The
        cloning step collects it again, and until then the file history step clones it for its
        Git columns and AI columns."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        finished = self.tmp / "list of finished repositories.jsonl"
        real = _mod.history.GitFileMetadata._run_git_command

        def failing_follow(self_, cmd, cwd, **kwargs):
            if "--follow" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "",
                                                   "fatal: could not fetch abc from promisor remote")
            return real(self_, cmd, cwd, **kwargs)

        with mock.patch.object(_mod.history.GitFileMetadata, "_run_git_command",
                               failing_follow), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _, fail_rows, facts, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                    finished_path=finished)
        self.assertEqual(fail_rows, [])
        self.assertEqual(facts, [], "no row rather than a blank one")
        self.assertIn("The commit history query of owner/repo for CLAUDE.md",
                      err.getvalue())
        self.assertIn("exit 128", err.getvalue())
        self.assertEqual(self._shard()[1]["history_failed"], ["CLAUDE.md"])
        self.assertTrue(_resume.history_needs_reread(self.commits, "owner/repo"))

        again = io.StringIO()
        with contextlib.redirect_stderr(again):
            _, fail_rows, facts, _ = run_collection(self.cfg, ["owner/repo"], workers=1,
                                                    finished_path=finished)
        self.assertIn("Collecting 1 repositories again to retry them", again.getvalue())
        self.assertEqual([r["artifact"] for r in facts], ["CLAUDE.md"])
        self.assertEqual(self._shard()[1]["history_failed"], [])
        self.assertEqual(len(self._shard()[1]["ai_rows"]), 1)

    def test_git_that_could_not_be_started_at_a_repository_question_is_a_collector_error(self):
        """If `rev-parse HEAD` returned '' under a refused `fork()`, the collector would record
        a repository with no `head_sha`, the column that identifies the snapshot. A refused
        query for the root commit would record a blank `first_commit_sha`. Both would be
        recorded in ok lines that no later run redoes."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        real = subprocess.run

        def refusing_head(cmd, *args, **kwargs):
            if "rev-parse" in cmd and cmd[-1] == "HEAD":
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real(cmd, *args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=refusing_head), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _, _, fail_row, _, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(_resume.outcome_of(fail_row), "collector_error")
        self.assertIn("BlockingIOError", err.getvalue())
        bounded = _mod.bounds.run_bounded

        def refusing_root(cmd, **kwargs):
            if "rev-list" in cmd and "--max-parents=0" in cmd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return bounded(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing_root), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _, _, fail_row, _, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(_resume.outcome_of(fail_row), "collector_error")
        self.assertIn("root commit", err.getvalue())

    def test_git_that_could_not_be_started_for_a_file_is_a_collector_error(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        bounded = _mod.bounds.run_bounded

        def refusing_follow(cmd, **kwargs):
            if "--follow" in cmd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return bounded(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing_follow), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _, _, fail_row, facts, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertEqual(_resume.outcome_of(fail_row), "collector_error")
        self.assertIn("Could not start git for 1 of the 1 files", err.getvalue())

    def test_readable_shards_and_shard_rows_are_the_one_reader(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        csv_path, json_path = commits.shard_paths(self.commits, "owner/torn")
        json_path.write_text('{"repo_name": "owner/torn", "outc')
        csv_path.write_text(",".join(commits.COMMIT_COLUMNS) + "\nowner/torn,t,b,s,l,m,Claude,claude-trailer-02,Claude:standard\n")
        commits.shard_paths(self.commits, "owner/orphan")[0].write_text(
            ",".join(commits.COMMIT_COLUMNS) + "\nowner/orphan,t,b,s,l,m,Claude,claude-trailer-02,Claude:standard\n")
        old_csv, old_json = commits.shard_paths(self.commits, "owner/old")
        old_csv.write_text("repo_name,commit_sha,extra\nowner/old,abc,x\n")
        old_json.write_text(json.dumps({"repo_name": "owner/old", "outcome": "history_read",
                                        "unmatched": [], "no_signal_shas": [], "ai_rows": []}) + "\n")
        # The reader refuses a per-repository commit record without the keys that the cloning
        # step writes rather than read it as having no unmatched attributions. It also refuses a
        # record of an earlier run that has them under the key `unexplained`.
        plain_csv, plain_json = commits.shard_paths(self.commits, "owner/plain")
        shutil.copy(commits.shard_paths(self.commits, "owner/repo")[0], plain_csv)
        plain_json.write_text(json.dumps({"repo_name": "owner/plain", "outcome": "history_read"}) + "\n")
        earlier_csv, earlier_json = commits.shard_paths(self.commits, "owner/earlier")
        shutil.copy(commits.shard_paths(self.commits, "owner/repo")[0], earlier_csv)
        earlier_json.write_text(json.dumps({
            "repo_name": "owner/earlier", "outcome": "history_read", "no_signal_shas": [],
            "ai_rows": [], "unexplained": [{"kind": "trailer", "token": "Bot", "commits": 1,
                                            "shas": ["abc"], "lines": ["Co-authored-by: Bot"]}]}) + "\n")
        null_csv, null_json = commits.shard_paths(self.commits, "owner/nulls")
        shutil.copy(commits.shard_paths(self.commits, "owner/repo")[0], null_csv)
        null_json.write_text(json.dumps({"repo_name": "owner/nulls", "outcome": "history_read",
                                         "no_signal_shas": None}) + "\n")
        bom_csv, bom_json = commits.shard_paths(self.commits, "owner/bom")
        bom_csv.write_bytes(b"\xef\xbb\xbf" + commits.shard_paths(self.commits, "owner/repo")[0].read_bytes())
        bom_json.write_text(json.dumps({"repo_name": "owner/bom", "outcome": "history_read",
                                        "unmatched": [], "no_signal_shas": [], "ai_rows": []}) + "\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            shards = commits.readable_shards(self.commits)
        by_repo = {repo: (csv_path, record["outcome"]) for repo, csv_path, record in shards}
        self.assertEqual(set(by_repo), {"owner/repo", "owner/torn", "owner/old", "owner/plain",
                                        "owner/earlier", "owner/nulls", "owner/bom"},
                         "a csv without a per-repository commit record is what a killed "
                         "worker left, not a shard")
        self.assertEqual(by_repo["owner/torn"], (None, "shard_unreadable"))
        self.assertEqual(by_repo["owner/nulls"], (None, "shard_unreadable"),
                         "a record with another value where a list belongs does not have the "
                         "columns that the cloning step writes")
        self.assertEqual(by_repo["owner/plain"], (None, "shard_unreadable"))
        self.assertEqual(by_repo["owner/earlier"], (None, "shard_unreadable"),
                         "a record with the earlier key would lose its unmatched attributions")
        self.assertEqual(by_repo["owner/old"][1], "history_read")
        self.assertEqual(err.getvalue().count("shard has the columns"), 1,
                         "the header of the csv, and only a header that differs")
        self.assertIn("owner/old's shard has the columns", err.getvalue())
        self.assertNotIn("--fresh", err.getvalue())
        rows = list(commits.shard_rows(by_repo["owner/old"][0]))
        self.assertEqual(rows, [{**{c: "" for c in commits.COMMIT_COLUMNS},
                                 "repo_name": "owner/old", "commit_sha": "abc"}])
        self.assertEqual([r["repo_name"] for r in commits.shard_rows(by_repo["owner/bom"][0])],
                         ["owner/repo"], "a byte-order mark does not rename the first column")
        self.assertEqual([r for r, _, _ in commits.readable_shards(self.commits, ["owner/repo"])],
                         ["owner/repo"])
        self.assertNotIn("columns", self._shard()[1])

    def test_remove_shards_takes_what_a_killed_worker_leaves(self):
        csv_path, json_path = commits.shard_paths(self.commits, "owner/repo")
        self.commits.mkdir()
        for name in (csv_path.name, json_path.name, csv_path.name + ".tmp",
                     "owner§repo.log", json_path.name + ".abcd1234.tmp",
                     # a repository named owner/repo.json, which another worker is reading
                     "owner§repo.json.csv.tmp", "owner§repo.json.json.efgh5678.tmp"):
            (self.commits / name).write_text("x")
        commits.remove_shards(self.commits, "owner/repo")
        self.assertEqual(sorted(p.name for p in self.commits.iterdir()),
                         ["owner§repo.json.csv.tmp", "owner§repo.json.json.efgh5678.tmp"],
                         "another repository's shards in progress are not this repository's")

    def test_the_shards_are_written_in_one_step(self):
        """A worker killed between two writes, at the end of the grace period or by the kernel,
        must not leave a torn per-repository commit record. The assembler would read that record
        after the whole collection, on every resumed run. The collector therefore writes the
        record and the shard under their names by a rename."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        with mock.patch.object(_mod.caches, "replace_file",
                               wraps=_mod.caches.replace_file) as replace, \
                mock.patch.object(_mod.os, "replace", wraps=os.replace) as rename:
            process_repo(self.cfg, "owner/repo")
        csv_path, json_path = commits.shard_paths(self.commits, "owner/repo")
        self.assertIn(json_path, [c.args[0] for c in replace.call_args_list])
        other_path = commits.other_shard_path(self.commits, "owner/repo")
        self.assertIn(csv_path, [c.args[1] for c in rename.call_args_list])
        self.assertIn(other_path, [c.args[1] for c in rename.call_args_list])
        self.assertEqual(sorted(str(p.relative_to(self.commits)) for p in self.commits.rglob("*")),
                         ["other", "other/owner§repo.csv", "owner§repo.csv", "owner§repo.json",
                          "owner§repo.log.xz"],
                         "nothing partial left")

    def test_a_per_repository_commit_record_that_does_not_parse_is_reported_not_fatal(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        csv_path, json_path = commits.shard_paths(self.commits, "owner/torn")
        json_path.write_text('{"repo_name": "owner/torn", "outc')
        csv_path.write_text(",".join(commits.COMMIT_COLUMNS) + "\nowner/torn,t,b,s,l,m,Claude,claude-trailer-02,Claude:standard\n")
        out = self.tmp / "output" / "all_ai_commits.csv"
        results = {"owner/repo": (_ROW, None, [], []), "owner/torn": (_ROW, None, [], [])}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            commits.assemble_ai_commits(self.commits, results, out, None)
        self.assertIn("owner§torn.json is not a readable per-repository commit record",
                      err.getvalue())
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        self.assertEqual({r["repo_name"] for r in rows}, {"owner/repo"})
        repos = {r["repo_name"]: r["outcome"] for r in
                  csv.DictReader(out.with_name("ai_commits-repos.csv").open(encoding="utf-8"))}
        self.assertEqual(repos, {"owner/repo": "history_read", "owner/torn": "shard_unreadable"})

    def test_the_csv_contains_only_the_input_list_s_shards(self):
        """The assembler reads only the shards of the input. Otherwise, a repository dropped
        from the input would keep its rows in the CSV although no step published it."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        csv_path, json_path = commits.shard_paths(self.commits, "owner/dropped")
        shutil.copy(commits.shard_paths(self.commits, "owner/repo")[0], csv_path)
        shutil.copy(commits.shard_paths(self.commits, "owner/repo")[1], json_path)
        out = self.tmp / "output" / "all_ai_commits.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            commits.assemble_ai_commits(self.commits, {"owner/repo": (_ROW, None, [], [])},
                                     out, None, repos=["owner/repo"])
        rows = list(csv.DictReader(out.open(encoding="utf-8")))
        self.assertEqual({r["repo_name"] for r in rows}, {"owner/repo"})
        repos = [r["repo_name"] for r in
                  csv.DictReader(out.with_name("ai_commits-repos.csv").open(encoding="utf-8"))]
        self.assertEqual(repos, ["owner/repo"])

    def test_a_collection_that_fails_removes_the_repository_s_earlier_shards(self):
        """A repository with a commit history that was read once, and that failed with
        `clone_failed` later, must not keep its rows and its counts."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        self._redirect()
        process_repo(self.cfg, "owner/repo")
        self.assertIsNotNone(self._shard()[1])
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/nonexistent/nowhere.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            _, _, fail_row, _, _, _measures = process_repo(self.cfg, "owner/repo")
        self.assertIsNotNone(fail_row)
        self.assertEqual(self._shard(), (None, None))

    def test_main_writes_the_submodules_before_the_commit_csv_and_reports_a_failed_assembly(self):
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\n")
        self._redirect()
        with mock.patch.object(commits, "assemble_ai_commits", side_effect=RuntimeError("torn")):
            code = self._run_main()
        self.assertEqual(code, 1)
        self.assertTrue((self.tmp / "sub.csv").is_file())
        self.assertTrue((self.tmp / "collected" / "repos.csv").is_file())
        self.assertIn("ERROR repository_cloning: Could not assemble the commit CSV", self.err)
        self.assertIn("RuntimeError: torn", self.err)

    def test_fresh_removes_what_a_collection_leaves(self):
        """`--fresh` removes the shards, the clone logs and the collected repositories of the
        earlier collection together with the list of finished repositories. Otherwise, the CSV
        would contain commits that the new run did not read again."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\n")
        self._redirect()
        self.assertEqual(self._run_main(), 0)
        finished = self.tmp / "list of finished repositories.jsonl"
        collected = self.tmp / "collected"
        self.assertTrue(finished.is_file() and (collected / "owner§repo").is_dir()
                        and (self.tmp / "logs" / "owner§repo.log").is_file()
                        and self._shard()[1] is not None)
        (self.tmp / "logs" / "stale.log").write_text("x")
        cfg = dataclasses.replace(self.cfg, collected_dir=collected, log_dir=self.tmp / "logs")
        self.assertTrue(_resume.ended_path(finished).is_file())
        removed = _resume.fresh_start(finished, cfg)
        self.assertFalse(finished.exists())
        self.assertFalse(_resume.ended_path(finished).exists())
        self.assertFalse(self.commits.exists())
        self.assertFalse((self.tmp / "logs").exists())
        self.assertFalse((collected / "owner§repo").exists())
        self.assertTrue((collected / "repos.csv").is_file(), "the lists stay")
        self.assertTrue((collected / "clone_failed.csv").is_file())
        self.assertEqual(len(removed), 5)
        self.assertIn("1 collected repositories", " ".join(removed))
        self.assertEqual(_resume.fresh_start(finished, cfg), [])

    def test_fresh_removes_the_file_lists_and_the_workflow_files_too(self):
        """The file lists and the workflow files are records of the earlier collection's
        clones, which the new run makes again. Left in place, they would keep a list of a clone
        that this run did not make for a repository that the new run no longer reaches."""
        lists, workflows = self.tmp / "file_lists", self.tmp / "workflows"
        (lists / "owner§gone.ls-tree.xz").parent.mkdir(parents=True)
        (lists / "owner§gone.ls-tree.xz").write_bytes(b"x")
        (workflows / "owner§gone").mkdir(parents=True)
        cfg = dataclasses.replace(self.cfg, collected_dir=self.tmp / "none",
                                  log_dir=self.tmp / "nologs", file_lists_dir=lists,
                                  workflows_dir=workflows)
        removed = _resume.fresh_start(self.tmp / "absent.jsonl", cfg)
        self.assertFalse(lists.exists())
        self.assertFalse(workflows.exists())
        self.assertIn("the file lists", " ".join(removed))
        self.assertIn("the workflow files", " ".join(removed))

    def test_the_help_of_fresh_names_everything_that_it_removes(self):
        """The help names everything that `fresh_start` removes, including the file lists, the
        workflow files and the path heuristics of the collection."""
        finished = self.tmp / "finished.jsonl"
        finished.write_text("")
        names = ("commits", "logs", "lists", "workflows", "heuristics")
        for name in names:
            (self.tmp / name).mkdir()
        (self.tmp / "collected" / "owner§repo").mkdir(parents=True)
        cfg = dataclasses.replace(self.cfg, collected_dir=self.tmp / "collected",
                                  **{f"{field}_dir": self.tmp / name for field, name in zip(
                                      ("commits", "log", "file_lists", "workflows", "heuristics"),
                                      names)})
        self.assertEqual(len(_resume.fresh_start(finished, cfg)), 7)
        text = " ".join(_mod._build_arg_parser().format_help().split())
        start = text.rindex("--fresh")  # its entry, after the usage line
        fresh = text[start:text.index("--clone-dir", start)]
        for removed in ("finished repositories", "shards", "clone logs", "file lists",
                        "workflow files", "path heuristics", "collected repositories"):
            self.assertIn(removed, fresh)

    def test_fresh_leaves_a_symlink_named_like_a_repository_and_logs_it(self):
        # `is_dir()` follows a link, and `rmtree` refuses a link. The collected file cleanup
        # treats such a link as a stray through `repos.split`, and so does `fresh_start`.
        collected = self.tmp / "collected"
        (collected / "owner§real").mkdir(parents=True)
        (collected / "elsewhere").mkdir()
        (collected / "owner§link").symlink_to(collected / "elsewhere")
        (collected / "repos.csv").write_text("repo_name\n")
        cfg = dataclasses.replace(self.cfg, collected_dir=collected, log_dir=self.tmp / "nologs")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            removed = _resume.fresh_start(self.tmp / "absent.jsonl", cfg)
        self.assertFalse((collected / "owner§real").exists())
        self.assertTrue((collected / "owner§link").is_symlink())
        self.assertTrue((collected / "elsewhere").is_dir())
        self.assertIn("--fresh left " + str(collected / "owner§link") + " in place, because it "
                      "is not a repository directory.", err.getvalue())
        self.assertIn("1 collected repositories", " ".join(removed))

    def test_fresh_removes_the_directories_that_dsm_writes_into(self):
        """Synology DSM writes an `@eaDir` into a directory of a shared folder at any time. An
        `@eaDir` written after `shutil.rmtree` listed a directory makes the removal of that
        directory fail with ENOTEMPTY, and `--fresh` would stop with a traceback before the new
        collection began."""
        collected, logs_dir = self.tmp / "collected", self.tmp / "logs"
        (collected / "owner§repo" / ".claude").mkdir(parents=True)
        (collected / "owner§repo" / ".claude" / "settings.json").write_text("{}\n")
        logs_dir.mkdir()
        (logs_dir / "owner§repo.log").write_text("x")
        cfg = dataclasses.replace(self.cfg, collected_dir=collected, log_dir=logs_dir)
        with dsm_writes_during_removal("settings.json") as into_the_tree, \
                dsm_writes_during_removal("owner§repo.log") as into_the_logs:
            removed = _resume.fresh_start(self.tmp / "absent.jsonl", cfg)
        self.assertEqual((len(into_the_tree), len(into_the_logs)), (1, 1))
        self.assertFalse((collected / "owner§repo").exists())
        self.assertFalse(logs_dir.exists())
        self.assertIn("1 collected repositories", " ".join(removed))

    def test_a_resumed_run_keeps_the_shards_of_what_finished(self):
        """The shards of a partial run remain, and the next run's CSV contains the rows of both
        repositories."""
        _commit_message(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90, self.CLAUDE)
        other = self.tmp / "other"
        _init_git_repo(other)
        _commit_message(other, "AGENTS.md", "# agents\n" + "z" * 90,
                        "fix: z\n\nCo-authored-by: Claude <noreply@anthropic.com>\n")
        (self.tmp / "list.csv").write_text("repo_name\nowner/repo\nowner/other\n")
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"
        os.environ["GIT_CONFIG_KEY_1"] = f"url.file://{other}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/owner/other.git"
        # The first run stops for low free space after one repository.
        original = _guards.free_bytes
        answers = [100 * 1024 ** 3, 0]
        _guards.free_bytes = lambda path: answers.pop(0) if len(answers) > 1 else answers[0]
        self.addCleanup(setattr, _guards, "free_bytes", original)
        self.assertEqual(self._run_main("--min-free-gb", "1", "--disk-check-every", "1"), 3)
        first = {r["repo_name"] for r in csv.DictReader(
            (self.tmp / "output" / "all_ai_commits.csv").open(encoding="utf-8"))}
        self.assertEqual(len(first), 1)
        _guards.free_bytes = original
        self.assertEqual(self._run_main(), 0)
        second = {r["repo_name"] for r in csv.DictReader(
            (self.tmp / "output" / "all_ai_commits.csv").open(encoding="utf-8"))}
        self.assertEqual(second, {"owner/repo", "owner/other"})



def _process_running(pid: int) -> bool:
    """Return whether the process runs. An absent process or a Linux zombie has finished
    executing."""
    if Path("/proc/self/stat").is_file():
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except FileNotFoundError:
            return False
        return stat.rsplit(")", 1)[1].split()[0] != "Z"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(Path("/proc/self/stat").is_file(), "Linux process states")
class TestProcessState(unittest.TestCase):
    def test_a_zombie_has_exited_while_a_live_child_is_running(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertTrue(_process_running(child.pid))
        child.kill()
        # WNOWAIT leaves the exit status for Popen.wait() and keeps the zombie.
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        self.assertFalse(_process_running(child.pid))
        child.wait()
        self.assertFalse(_process_running(child.pid))


class TestAStopEndsTheWorkers(unittest.TestCase):
    """SIGTERM to the collector ends its workers and the git processes that they started.

    Workers are separate processes. A SIGTERM that ended the parent alone would leave its
    workers cloning into a scratch directory that the resumed run then shares with them. A
    worker of the killed run could then delete the clone that a worker of the resumed run is
    reading. A checkout hook that sleeps keeps the clone in these tests running, so that the
    stop arrives while a worker is inside git.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        _add_commit(self.remote, "CLAUDE.md", "# Claude")
        hooks = self.tmp / "hooks"
        hooks.mkdir()
        self.marker = self.tmp / "hook.pid"
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\necho $$ > {self.marker}\nsleep 60\n")
        hook.chmod(0o755)
        self.list = self.tmp / "list.csv"
        self.list.write_text("repo_name\nowner/repo\nowner/other\n")
        self.env = {k: v for k, v in os.environ.items()
                    if k not in _ambient_env.CREDENTIAL_VARS}
        self.env.update({
            "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": f"url.file://{self.remote}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/owner/repo.git",
            "GIT_CONFIG_KEY_1": f"url.file://{self.remote}.insteadOf",
            "GIT_CONFIG_VALUE_1": "https://github.com/owner/other.git",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "GIT_CONFIG_VALUE_2": str(hooks),
        })

    def _start(self, workers: int = 2) -> subprocess.Popen:
        out = self.tmp / "out"
        return subprocess.Popen(
            [sys.executable, str(Path(_mod.__file__)), "--repo-csv", str(self.list),
             "--collected-dir", str(out), "--collected-csv", str(out / "repos.csv"),
             "--fail-csv", str(out / "clone_failed.csv"), "--finished", str(self.tmp / "l.jsonl"),
             "--file-history", str(self.tmp / "fh.csv"), "--submodules", str(self.tmp / "s.csv"),
             *_ai_commit_flags(self.tmp),
             "--clone-dir", str(self.tmp / "scratch"), "-l", str(self.tmp / "logs"),
             "-w", str(workers), "--max-tries", "1", "--min-free-gb", "0"],
            env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _stop_while_git_runs(self, workers: int) -> None:
        """Start the collector, send SIGTERM once a clone's hook runs, and assert that the
        collector exits with the signal's code and the hook is gone."""
        proc = self._start(workers)
        self.addCleanup(proc.kill)
        deadline = time.time() + 60
        while (not (self.marker.is_file() and self.marker.read_text().strip())
               and time.time() < deadline):
            time.sleep(0.1)
        self.assertTrue(self.marker.is_file(), "no worker reached the clone's hook")
        hook_pid = int(self.marker.read_text().strip())
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=30), 143)
        deadline = time.monotonic() + 5
        while _process_running(hook_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _process_running(hook_pid):
            os.kill(hook_pid, signal.SIGKILL)
            self.fail("the hook that the killed collector's git started is still running")

    def test_sigterm_ends_the_workers_and_their_git(self):
        self._stop_while_git_runs(workers=2)

    def test_sigterm_in_serial_mode_ends_the_git_it_started(self):
        """With one worker, the collector clones on its own main thread. Its handler has to
        kill the git processes that it started before it exits, because git runs in a session
        of its own and the signal to the collector never reaches git."""
        self._stop_while_git_runs(workers=1)


class _Tee:
    """A stderr for a helper that could otherwise discard the collector's lines. Writes go to
    the stderr in place when the helper is called. An outer capture receives them,
    and a plain call shows nothing."""

    def __init__(self, outer):
        self.outer = outer

    def write(self, text):
        if isinstance(self.outer, io.StringIO):
            self.outer.write(text)

    def flush(self):
        pass


def _breaks_at_submit(after: int) -> type:
    """A pool that runs the first `after` repositories that it is handed in the calling process
    and refuses every later repository at submit, as a pool does once it has detected a dead
    worker."""

    class BreaksAtSubmit:
        calls = 0

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def submit(self, fn, *args):
            BreaksAtSubmit.calls += 1
            if BreaksAtSubmit.calls > after:
                raise BrokenProcessPool("the pool is not usable anymore")
            future = Future()
            future.set_result(fn(*args))
            return future

    return BreaksAtSubmit


class _BrokenPool:
    """A pool in which every future is a worker that the kernel ended."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, fn, *args):
        future = Future()
        future.set_exception(BrokenProcessPool("a worker died"))
        return future


class _SixRemotes(unittest.TestCase):
    """The fixture of the tests of the free-space minimum and of the failure guards: six
    repositories, each with one commit of a CLAUDE.md and reachable through the redirect, and
    a list of finished repositories."""

    GIB = 1024 ** 3

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remotes = {}
        for name in ("o/a", "o/b", "o/c", "o/d", "o/e", "o/f"):
            remote = self.tmp / name.replace("/", "_")
            _init_git_repo(remote)
            _add_commit(remote, "CLAUDE.md", "# Claude")
            self.remotes[name] = remote
        self.finished = self.tmp / "list of finished repositories.jsonl"
        self._orig_free = _guards.free_bytes

    def tearDown(self):
        _guards.free_bytes = self._orig_free
        os.environ.pop("GIT_CONFIG_COUNT", None)
        for i in range(6):
            os.environ.pop(f"GIT_CONFIG_KEY_{i}", None)
            os.environ.pop(f"GIT_CONFIG_VALUE_{i}", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect_all(self) -> None:
        os.environ["GIT_CONFIG_COUNT"] = str(len(self.remotes))
        for i, (name, remote) in enumerate(self.remotes.items()):
            os.environ[f"GIT_CONFIG_KEY_{i}"] = f"url.file://{remote}.insteadOf"
            os.environ[f"GIT_CONFIG_VALUE_{i}"] = f"https://github.com/{name}.git"


class TestLowDisk(_SixRemotes):
    """The step measures the free space where the clones go, before the first clone and after
    every N finished repositories.

    Below the minimum, no further clone starts. The running clones finish and reach the list of
    finished repositories, and the step writes the rows that it reached and exits 3. The step
    lets the running clones finish rather than stop them, because a clone killed halfway is a
    repository to redo, while a line of the list of finished repositories written after the
    worker returned is never torn.
    """

    def _cfg(self, minimum_free_gb: float = 40, every: int = 1) -> CollectionConfig:
        return CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "scratch",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
            min_free_bytes=int(minimum_free_gb * self.GIB), disk_check_every=every)

    def _free_sequence(self, *values_gb):
        """Make `free_bytes` return these values in turn and then the last value, and return
        the list of the paths that it measures."""
        answers = [int(v * self.GIB) for v in values_gb]
        asked = []

        def free(path):
            asked.append(Path(path))
            return answers[min(len(asked), len(answers)) - 1]
        _guards.free_bytes = free
        return asked

    def test_low_before_the_first_clone_starts_nothing(self):
        self._redirect_all()
        asked = self._free_sequence(1)
        with self.assertRaises(_guards.LowDiskStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(), list(self.remotes), workers=1,
                           finished_path=self.finished)
        self.assertEqual((caught.exception.done, caught.exception.remaining), (0, 6))
        self.assertEqual(caught.exception.rows, ([], [], [], []))
        self.assertFalse(self.finished.exists())
        self.assertEqual(asked, [self.tmp / "scratch"], "measured somewhere else")

    def test_the_repositories_in_flight_finish_and_reach_the_finished(self):
        self._redirect_all()
        self._free_sequence(100, 1)
        with self.assertRaises(_guards.LowDiskStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(every=1), list(self.remotes), workers=1,
                           finished_path=self.finished)
        stop = caught.exception
        self.assertEqual((stop.done, stop.remaining), (1, 5))
        main_rows, fail_rows, _, _ = stop.rows
        self.assertEqual(len(main_rows), 1)
        self.assertEqual(fail_rows, [])
        self.assertEqual(len(read_finished(self.finished)), 1)
        self.assertIn("1.0 GiB free", str(stop))

    def test_a_pool_drains_rather_than_stops(self):
        self._redirect_all()
        self._free_sequence(100, 1)
        with self.assertRaises(_guards.LowDiskStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(every=1), list(self.remotes), workers=2,
                           finished_path=self.finished)
        stop = caught.exception
        # One repository per worker was submitted before anything finished. The measurement
        # after the first finished repository stops the rest from starting, and the clones that
        # the workers took finish.
        self.assertEqual(stop.done, len(read_finished(self.finished)))
        self.assertEqual((stop.done, stop.remaining), (2, 4))
        for main_row in stop.rows[0]:
            self.assertEqual(_row_field(main_row, "claude"), "TRUE")

    def test_low_after_the_last_repository_is_no_stop(self):
        """Free space below the minimum after the last repository is no reason to exit 3,
        because nothing is left to start."""
        self._redirect_all()
        self._free_sequence(100, 100, 100, 100, 100, 100, 1)
        repo_csv = self.tmp / "list.csv"
        repo_csv.write_text("repo_name\n" + "\n".join(self.remotes) + "\n")
        collected = self.tmp / "collected"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = main(["--repo-csv", str(repo_csv),
                         "--collected-dir", str(collected),
                         "--collected-csv", str(collected / "repos.csv"),
                         "--fail-csv", str(collected / "clone_failed.csv"),
                         "--file-history", str(self.tmp / "fh.csv"),
                         "--submodules", str(self.tmp / "sub.csv"),
                         *_ai_commit_flags(self.tmp),
                         "--finished", str(self.finished), "--clone-dir", str(self.tmp / "scratch"),
                         "-l", str(self.tmp / "logs"), "-w", "1", "--min-free-gb", "40",
                         "--disk-check-every", "1"])
        self.assertEqual(code, 0)
        self.assertEqual((collected / "repos.csv").read_text().count("\n"), 7)
        self.assertIn("after the last repository finished", err.getvalue())

    def test_the_measurement_is_taken_every_n_finished(self):
        self._redirect_all()
        asked = self._free_sequence(100)
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(every=2), list(self.remotes), workers=1,
                           finished_path=self.finished)
        # The step measures before the first repository and after the second, fourth and sixth.
        self.assertEqual(len(asked), 4)

    def test_a_minimum_of_zero_never_measures_free_space(self):
        self._redirect_all()
        asked = self._free_sequence(0)
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, _, _, _ = run_collection(self._cfg(minimum_free_gb=0), list(self.remotes),
                                                workers=1, finished_path=self.finished)
        self.assertEqual(len(main_rows), 6)
        self.assertEqual(asked, [])

    def test_a_resume_continues_where_the_stop_left_off(self):
        self._redirect_all()
        self._free_sequence(100, 1)
        with self.assertRaises(_guards.LowDiskStop), contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(every=1), list(self.remotes), workers=1,
                           finished_path=self.finished)
        self._free_sequence(100)
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, _, _, _ = run_collection(self._cfg(every=1), list(self.remotes),
                                                workers=1, finished_path=self.finished)
        self.assertEqual(len(main_rows), 6)
        self.assertEqual(len(read_finished(self.finished)), 6)

    def test_min_free_gb_uses_gibibytes(self):
        self._redirect_all()
        _guards.free_bytes = lambda path: 41_000_000_000
        repo_csv = self.tmp / "list.csv"
        repo_csv.write_text("repo_name\n" + "\n".join(self.remotes) + "\n")
        collected = self.tmp / "collected"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(["--repo-csv", str(repo_csv),
                         "--collected-dir", str(collected),
                         "--collected-csv", str(collected / "repos.csv"),
                         "--fail-csv", str(collected / "clone_failed.csv"),
                         "--file-history", str(self.tmp / "fh.csv"),
                         "--submodules", str(self.tmp / "sub.csv"),
                         *_ai_commit_flags(self.tmp),
                         "--finished", str(self.finished), "--clone-dir", str(self.tmp / "scratch"),
                         "-l", str(self.tmp / "logs"), "-w", "1", "--min-free-gb", "40"])
        self.assertEqual(code, 3)
        self.assertEqual((collected / "repos.csv").read_text().count("\n"), 1)

        self.assertIn("has 38.2 GiB free, under the 40.0 GiB minimum", err.getvalue())

    def test_main_exits_3_and_writes_what_it_reached(self):
        self._redirect_all()
        self._free_sequence(1)
        repo_csv = self.tmp / "list.csv"
        repo_csv.write_text("repo_name\n" + "\n".join(self.remotes) + "\n")
        collected = self.tmp / "collected"
        with contextlib.redirect_stderr(io.StringIO()):
            code = main(["--repo-csv", str(repo_csv),
                         "--collected-dir", str(collected),
                         "--collected-csv", str(collected / "repos.csv"),
                         "--fail-csv", str(collected / "clone_failed.csv"),
                         "--file-history", str(self.tmp / "fh.csv"),
                         "--submodules", str(self.tmp / "sub.csv"),
                         *_ai_commit_flags(self.tmp),
                         "--finished", str(self.finished), "--clone-dir", str(self.tmp / "scratch"),
                         "-l", str(self.tmp / "logs"), "-w", "1", "--min-free-gb", "40"])
        self.assertEqual(code, 3)
        self.assertEqual((collected / "repos.csv").read_text().count("\n"), 1)

    def test_the_runner_and_the_collector_name_one_storage_minimum(self):
        from agentcontext import runner
        self.assertEqual(_guards.MIN_FREE_GB, runner.MIN_FREE_DISK_GB)

    def test_the_runner_and_the_collector_name_one_failure_ceiling(self):
        # --mark-complete refuses a collected tree above the ceiling at which the step exits 4.
        from agentcontext import runner
        self.assertEqual(_guards.CLONE_FAILURE_SHARE, runner.CLONE_FAILURE_SHARE)
        for repositories in (0, 1, 6, 67, 719, 720, 10_000, 36_710, 100_000):
            with self.subTest(repositories=repositories):
                self.assertEqual(_guards.clone_failure_ceiling(repositories),
                                 runner.clone_failure_ceiling(repositories))
        self.assertEqual(_resume.TRANSIENT_OUTCOMES, runner.TRANSIENT_OUTCOMES)


class TestTheFailureGuards(_SixRemotes):
    """When the container can no longer fork, every clone fails within minutes. Recording each
    failed repository as done would make a resumed run skip them all. The fixture is the one of
    the free-space minimum: six remotes and a list of finished repositories."""

    def _redirect_to_nowhere(self, names) -> None:
        # The named repositories clone from a port that nothing listens on, a network failure
        # with the transient outcome clone_failed, and the other repositories resolve. A path
        # with no repository behind it would be a repository that is gone, not a failure to
        # retry.
        os.environ["GIT_CONFIG_COUNT"] = str(len(self.remotes))
        for i, (name, remote) in enumerate(self.remotes.items()):
            target = "http://127.0.0.1:9/nonexistent/nowhere" if name in names else f"file://{remote}"
            os.environ[f"GIT_CONFIG_KEY_{i}"] = f"url.{target}.insteadOf"
            os.environ[f"GIT_CONFIG_VALUE_{i}"] = f"https://github.com/{name}.git"

    NOWHERE = "http://127.0.0.1:9/nonexistent/probe"

    def _cfg(self, **overrides) -> CollectionConfig:
        # The probe URL leads nowhere, and failed clones in a row stop the step as
        # they would without a network.
        base = dict(collected_dir=self.tmp / "out", clone_dir=self.tmp / "scratch",
                    log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
                    probe_url=self.NOWHERE)
        base.update(overrides)
        return CollectionConfig(**base)

    def test_a_streak_of_failed_clones_probes_the_network_before_the_step_stops(self):
        """Many repositories of one owner that GitHub closed fail one after the other on a
        healthy network, because the input is sorted by owner. A stop there would cost a Start
        by hand for nothing. The step lists one ref of the probe URL and continues when the
        listing succeeds."""
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0], names[1], names[2]})
        err = io.StringIO()
        with mock.patch.object(_guards, "remote_responds", wraps=_guards.remote_responds) as probe, \
                contextlib.redirect_stderr(err):
            main_rows, fail_rows, _, _ = run_collection(
                self._cfg(max_consecutive_failures=2,
                          probe_url=f"file://{self.remotes[names[5]]}"),
                names, workers=1, finished_path=self.finished)
        self.assertEqual(len(main_rows), len(names), "the run went on to the end")
        self.assertEqual(len(fail_rows), 3)
        self.assertEqual(probe.call_count, 1,
                         "probed once after the failed clones in a row, and the count restarted")
        self.assertIn("unknown cause, but git could list a ref of", err.getvalue())
        self.assertIn(str(self._cfg().log_dir), err.getvalue())

    def test_a_failed_probe_is_repeated_after_each_wait_before_the_step_stops(self):
        """An outage of the network or of GitHub that passes within minutes fails the probe
        once. A stop there would end an unattended run until a Start by hand. The step therefore
        waits and probes again, and it goes on when a later probe succeeds."""
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0], names[1], names[2]})
        err = io.StringIO()
        with mock.patch.object(_guards, "remote_responds",
                               side_effect=[False, False, True]) as probe, \
                contextlib.redirect_stderr(err):
            main_rows, fail_rows, _, _ = run_collection(
                self._cfg(max_consecutive_failures=2, probe_waits=(0.01, 0.01, 0.01)),
                names, workers=1, finished_path=self.finished)
        self.assertEqual(len(main_rows), len(names), "the run went on to the end")
        self.assertEqual(len(fail_rows), 3)
        self.assertEqual(probe.call_count, 3, "probed again after two waits")
        self.assertIn("Probing again in 0:00:00 (wait 1 of 3)", err.getvalue())
        self.assertIn("Probing again in 0:00:00 (wait 2 of 3)", err.getvalue())
        self.assertNotIn("wait 3 of 3", err.getvalue())
        self.assertIn("unknown cause, but git could list a ref of", err.getvalue())

    def test_the_step_stops_when_every_probe_after_the_waits_fails(self):
        names = list(self.remotes)
        self._redirect_to_nowhere(set(names))
        err = io.StringIO()
        with mock.patch.object(_guards, "remote_responds", return_value=False) as probe, \
                self.assertRaises(_guards.ConsecutiveFailuresStop) as caught, \
                contextlib.redirect_stderr(err):
            run_collection(self._cfg(max_consecutive_failures=2, probe_waits=(0.01, 0.01)),
                           names, workers=1, finished_path=self.finished)
        self.assertEqual(probe.call_count, 3, "the first probe and one after each wait")
        self.assertEqual(caught.exception.finished, 2, "no clone started during the waits")
        self.assertIn("(wait 2 of 2)", err.getvalue())

    def test_main_waits_and_probes_again_before_it_exits_4(self):
        # The command line turns the waits on, and the runner's step waits out an
        # outage.
        self._redirect_to_nowhere(set(self.remotes))
        with mock.patch.object(_guards, "remote_responds", return_value=False) as probe:
            code = self._main_capturing("--max-consecutive-failures", "2", probe_waits=(0.01,))
        self.assertEqual(code, 4)
        self.assertEqual(probe.call_count, 2)
        self.assertIn("(wait 1 of 1)", self.err)
        self.assertIn("could not list a ref of", self.err)

    def test_clones_past_the_time_bound_in_a_row_probe_the_network(self):
        """A network that stalls transfers ends every clone at the time bound rather than with
        an error. Counted as a success, such clones would reset the count of failed clones in a
        row, and the step would record the whole input list as time_bound_exceeded without
        probing the network."""
        names = list(self.remotes)
        self._redirect_all()
        with mock.patch.object(_guards, "remote_responds", return_value=False) as probe, \
                self.assertRaises(_guards.ConsecutiveFailuresStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(max_consecutive_failures=2, max_tries=2,
                                     clone_timeout=0.001),
                           names, workers=1, finished_path=self.finished)
        self.assertEqual(caught.exception.streak, 2)
        self.assertEqual(caught.exception.finished, 2)
        probe.assert_called_once()

    def test_the_probe_lists_one_ref_under_the_clones_environment(self):
        names = list(self.remotes)
        with mock.patch.object(_mod.bounds, "run_bounded",
                               wraps=_mod.bounds.run_bounded) as bounded:
            self.assertTrue(_guards.remote_responds(f"file://{self.remotes[names[0]]}",
                                                "SECRET", False))
        argv = bounded.call_args.args[0]
        self.assertEqual(argv[:3], ["git", "ls-remote", "--exit-code"])
        self.assertEqual(argv[-1], "HEAD")
        self.assertEqual(bounded.call_args.kwargs["env"]["GIT_ASKPASS_TOKEN"], "SECRET")
        self.assertFalse(_guards.remote_responds(self.NOWHERE, None, False))
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=OSError(11, "x")):
            self.assertFalse(_guards.remote_responds(f"file://{self.remotes[names[0]]}",
                                                 None, False), "a git that cannot start")

    def test_the_probe_uses_the_transport_the_clones_use(self):
        """Probing over https while the clones go over ssh would test a transport that the run
        does not use. An SSH key that does not work would then reset the count on every probe,
        and the step would record clone_failed for the whole input list. The probe rewrites
        every https GitHub URL, not only the default, because `--probe-url` with another URL
        would have the same effect."""
        for url, expected in ((_guards.PROBE_URL, "git@github.com:git/git.git"),
                              ("https://github.com/owner/repo.git",
                               "git@github.com:owner/repo.git")):
            with self.subTest(url=url):
                with mock.patch.object(_mod.bounds, "run_bounded",
                                       side_effect=OSError(2, "no git here")) as bounded:
                    _guards.remote_responds(url, None, True)
                self.assertEqual(bounded.call_args.args[0][3], expected)
        # A URL that is not a GitHub https URL is passed through as it was given.
        with mock.patch.object(_mod.bounds, "run_bounded",
                               side_effect=OSError(2, "no git here")) as bounded:
            _guards.remote_responds("file:///mirrors/owner/repo", None, True)
        self.assertEqual(bounded.call_args.args[0][3], "file:///mirrors/owner/repo")

    def test_collector_errors_among_the_recent_finished_stop_the_step(self):
        """A defect that affects only the repositories with copied files never causes twenty
        errors in a row, because those repositories are a minority of the sample, spread over
        the run. The defect does cause twenty among the last two hundred. Without that window,
        the run would clone the whole sample to record the defect."""
        names = list(self.remotes)
        self._redirect_all()
        cfg = self._cfg(max_consecutive_failures=3, probe_url=f"file://{self.remotes[names[5]]}")
        real = paths.collect_repo
        calls = []

        def every_other(*args, **kwargs):
            calls.append(1)
            if len(calls) % 2 == 0:
                raise RuntimeError("a defect that reaches every second repository")
            return real(*args, **kwargs)

        with mock.patch.object(paths, "collect_repo", side_effect=every_other), \
                mock.patch.object(_guards, "remote_responds") as probe, \
                self.assertRaises(_guards.ConsecutiveFailuresStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=1, finished_path=self.finished)
        self.assertEqual(caught.exception.kind, "collector_error")
        self.assertEqual(caught.exception.streak, 3)
        self.assertIn("among the last", str(caught.exception))
        self.assertEqual(caught.exception.finished, 6, "ok, error, ok, error, ok, error")
        probe.assert_not_called()

    def test_main_names_the_defect_when_collector_errors_stop_the_step(self):
        # The log message that the runbook instructs the operator to act on names the defect, not
        # the network and the probe, which did not run.
        self._redirect_all()
        with mock.patch.object(paths, "collect_repo", side_effect=RuntimeError("a defect")), \
                mock.patch.object(_guards, "remote_responds", return_value=True) as probe:
            code = self._main_capturing("--max-consecutive-failures", "2")
        self.assertEqual(code, 4)
        probe.assert_not_called()
        self.assertIn("WARNING repository_cloning: 2 repositories ended in collector_error among "
                      "the last", self.err)
        self.assertIn("Stopped the collection. Fix the cause that the tracebacks above", self.err)
        self.assertNotIn("could not list a ref", self.err)

    def test_histories_to_reread_count_toward_the_ceiling_and_are_named(self):
        """The step counts a commit history that git could not start reading rather than leave
        it as an ok line with `redo` that nothing counts. Otherwise, the run would exit 0, and
        the runner would record the step as complete. Every later Start would then skip the
        step and leave the repository out of the commit CSV permanently."""
        self._redirect_all()
        bounded = _mod.bounds.run_bounded

        def refusing_read(cmd, **kwargs):
            if "i18n.logOutputEncoding=UTF-8" in cmd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return bounded(cmd, **kwargs)

        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing_read):
            code = self._main_capturing("--max-clone-failures", "5")
        self.assertEqual(code, 4)
        self.assertIn("6 repositories remain to collect again", self.err)
        self.assertIn("WARNING repository_cloning: 6 repositories remain to retry",
                      self.err)
        with mock.patch.object(_mod.bounds, "run_bounded", side_effect=refusing_read):
            code = self._main_capturing("--max-clone-failures", "6")
        self.assertEqual(code, 0, "at the ceiling the run is complete and logs what it leaves")
        self.assertIn("6 repositories remain to collect again", self.err)

    def test_collector_errors_in_a_row_stop_the_step_without_the_probe(self):
        """A defect after the clone is not a network failure. The probe would succeed, and the
        run would clone the whole sample to record the defect once per repository."""
        names = list(self.remotes)
        self._redirect_all()
        cfg = self._cfg(max_consecutive_failures=2,
                        probe_url=f"file://{self.remotes[names[5]]}")
        cfg.collected_dir.write_text("a file where the tree goes")
        with mock.patch.object(_guards, "remote_responds", wraps=_guards.remote_responds) as probe, \
                self.assertRaises(_guards.ConsecutiveFailuresStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=1, finished_path=self.finished)
        self.assertEqual(caught.exception.streak, 2)
        probe.assert_not_called()

    def test_the_network_is_not_probed_again_once_the_step_is_stopping(self):
        # Every failure among the running clones keeps the count over the limit. Probing again
        # each time would cost the probe's timeout per probe on a network that does not work.
        names = list(self.remotes)
        self._redirect_to_nowhere(set(names))
        with mock.patch.object(_guards, "remote_responds", return_value=False) as probe, \
                self.assertRaises(_guards.ConsecutiveFailuresStop), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(max_consecutive_failures=2), names, workers=2,
                           finished_path=self.finished)
        self.assertEqual(probe.call_count, 1)

    def test_a_pool_that_breaks_at_submit_ends_the_step_the_same_way(self):
        # The pool detects the dead worker between one good result and the next submit, and
        # `submit` raises. The exception must not escape as a traceback with no lists written.
        names = list(self.remotes)
        self._redirect_all()
        with mock.patch.object(_mod, "ProcessPoolExecutor", _breaks_at_submit(2)), \
                self.assertRaises(_guards.WorkerLostStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(), names, workers=2, finished_path=self.finished)
        recorded = read_finished(self.finished)
        self.assertEqual(len(recorded), 2, "the two that finished are recorded as ok")
        self.assertEqual({_resume.outcome_of(v[1]) for v in recorded.values()}, {"ok"})
        self.assertEqual(caught.exception.finished + caught.exception.remaining, len(names))

    def test_a_retry_that_the_pool_refuses_at_submit_keeps_its_line(self):
        """`start` marks the line of a retry `retrying` before the pool is handed the retry. If
        the pool refuses the retry at submit, the mark must not stay on a retry that no worker
        took. Otherwise, the next run would refuse to keep the repository's earlier collection
        when its clone failed."""
        names = list(self.remotes)
        cfg = self._retries_of_moved_heads(names)
        before = read_finished(self.finished)
        with mock.patch.object(_mod, "ProcessPoolExecutor", _breaks_at_submit(2)), \
                self.assertRaises(_guards.WorkerLostStop), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=2, finished_path=self.finished)
        recorded = read_finished(self.finished)
        refused = _mod.processing_order(names)[2]
        self.assertEqual([name for name in names if recorded[name][7]], [])
        self.assertEqual(recorded[refused], before[refused])
        self.assertFalse((cfg.collected_dir / repo_name_to_dir(refused) / "AGENTS.md").exists(),
                         "no worker cloned the moved head")

    def test_a_collector_error_is_recorded_printed_and_redone(self):
        """An exception after the clone is not `clone_failed` with nothing logged. It has its
        own outcome, its traceback goes to the console and to the clone log, and the next run
        collects the repository again."""
        names = list(self.remotes)
        self._redirect_all()
        cfg = self._cfg()
        cfg.collected_dir.write_text("a file where the tree goes")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            _, fail_rows, _, _ = run_collection(cfg, names[:1], workers=1,
                                                finished_path=self.finished)
        self.assertEqual([_resume.outcome_of(r) for r in fail_rows], ["collector_error"])
        self.assertRegex(err.getvalue(), r"The collection of \S+ failed in")
        self.assertIn("FileExistsError", err.getvalue())
        log = (cfg.log_dir / "o§a.log").read_text()
        self.assertIn("COLLECTOR_ERROR", log)
        self.assertIn("FileExistsError", log)
        cfg.collected_dir.unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            main_rows, fail_rows, _, _ = run_collection(cfg, names[:1], workers=1,
                                                        finished_path=self.finished)
        self.assertIn("Collecting 1 repositories again to retry them (outcome clone_failed or "
                      "collector_error",
                      err.getvalue())
        self.assertEqual(fail_rows, [])
        self.assertTrue((cfg.collected_dir / "o§a" / "CLAUDE.md").is_file())

    def test_collector_errors_count_toward_the_streak(self):
        names = list(self.remotes)
        self._redirect_all()
        cfg = self._cfg(max_consecutive_failures=2)
        cfg.collected_dir.write_text("a file where the tree goes")
        with self.assertRaises(_guards.ConsecutiveFailuresStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=1, finished_path=self.finished)
        self.assertEqual(caught.exception.streak, 2)

    def test_a_worker_the_kernel_ends_stops_the_step(self):
        """A worker that the kernel killed, such as for lack of memory, makes the pool raise
        BrokenProcessPool. The exception must not leave run_collection as a traceback with exit
        1 and no lists. It ends the step as failed clones in a row do, and the repositories in
        progress are recorded as clone_failed for the next run."""
        names = list(self.remotes)
        self._redirect_all()
        with mock.patch.object(_mod, "ProcessPoolExecutor", _BrokenPool), \
                self.assertRaises(_guards.WorkerLostStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(), names, workers=2, finished_path=self.finished)
        recorded = read_finished(self.finished)
        self.assertTrue(recorded, "what was in flight is in the list of finished repositories")
        self.assertEqual({_resume.outcome_of(v[1]) for v in recorded.values()}, {"clone_failed"})
        self.assertEqual(caught.exception.finished + caught.exception.remaining, len(names))
        self.assertEqual(len(caught.exception.rows[1]), len(recorded))

    def _retries_of_moved_heads(self, names: list) -> CollectionConfig:
        """Collect `names` and move the head of each repository, so that the next run retries
        every one of them as a retry of an ok line."""
        self._redirect_all()
        cfg = self._cfg()
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=1, finished_path=self.finished)
        for name in names:
            _add_commit(self.remotes[name], "AGENTS.md", "# moved")
        return cfg

    def test_a_stop_marks_only_the_retries_that_the_workers_took(self):
        """`start` marks the line of a retry `retrying` when the pool is handed the retry. The
        test hands over twice as many retries as there are workers. A stop must not leave the
        mark on retries that no worker had begun, because the next run would then refuse to
        keep their earlier collections when their clones failed."""
        names = list(self.remotes)[:4]
        cfg = self._retries_of_moved_heads(names)

        class Stop(Exception):
            pass

        marked = []

        def stop_at_the_first_wait(futures, return_when):
            marked.extend(r for r, line in read_finished(self.finished).items() if line[7])
            raise Stop

        with mock.patch.object(_mod, "wait", stop_at_the_first_wait), \
                self.assertRaises(Stop), contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=2, finished_path=self.finished)
        self.assertEqual(len(marked), 2, marked)

    def test_a_lost_worker_records_no_retry_that_no_worker_took(self):
        """A pool that breaks fails every future in it. The retries handed to the pool beyond
        its workers must not be recorded as `clone_failed`, because no worker had begun them and
        their earlier collections would lose their lines."""
        names = list(self.remotes)
        cfg = self._retries_of_moved_heads(names)
        with mock.patch.object(_mod, "ProcessPoolExecutor", _BrokenPool), \
                self.assertRaises(_guards.WorkerLostStop), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, names, workers=2, finished_path=self.finished)
        recorded = read_finished(self.finished)
        failed = [name for name in names if recorded[name][1] is not None]
        self.assertEqual(len(failed), 2, failed)
        self.assertEqual({(recorded[name][1], recorded[name][7]) for name in names
                          if name not in failed}, {(None, False)})

    def test_main_exits_4_when_a_worker_is_lost(self):
        self._redirect_all()
        with mock.patch.object(_mod, "ProcessPoolExecutor", _BrokenPool):
            code = self._main("-w", "2")
        self.assertEqual(code, 4)
        self.assertTrue((self.tmp / "collected" / "clone_failed.csv").is_file())

    def test_serial_mode_responds_to_a_stop_and_restores_the_handler(self):
        """With one worker, the collector starts git itself. Under the default disposition, the
        collector would end while the running clone went on. The collector installs the pool
        worker's handler for the loop and restores the earlier handler afterwards."""
        names = list(self.remotes)
        self._redirect_all()
        seen = []
        real = collector.process_repo

        def spy(config, repo, preserve_on=frozenset()):
            seen.append(signal.getsignal(signal.SIGTERM))
            return real(config, repo, preserve_on)

        before = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(collector, "process_repo", spy), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(), names[:2], workers=1, finished_path=self.finished)
        self.assertEqual(seen, [_guards.worker_stop, _guards.worker_stop])
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_a_transient_failure_is_pending_again_on_the_next_run(self):
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0]})
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(self._cfg(max_consecutive_failures=0),
                                                names, workers=1, finished_path=self.finished)
        self.assertEqual([_resume.outcome_of(r) for r in fail_rows], ["clone_failed"])
        # In the next run, the remote is back. The failed repository is collected again, and
        # the other repositories are not.
        self._redirect_all()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            main_rows, fail_rows, _, _ = run_collection(self._cfg(), names, workers=1,
                                                        finished_path=self.finished)
        self.assertIn("Collecting 1 repositories again to retry them (outcome clone_failed or "
                      "collector_error",
                      err.getvalue())
        self.assertEqual(fail_rows, [])
        self.assertEqual(len(main_rows), len(names))
        records = [json.loads(l) for l in self.finished.read_text().splitlines() if l.strip()]
        self.assertEqual(len(records), len(names) + 1, "the redo appends, it does not rewrite")
        self.assertIsNone(read_finished(self.finished)[names[0]][1],
                          "the newer record replaces the older one")

    def test_a_permanent_outcome_is_not_retried(self):
        """GitHub refuses a repository that is gone or private, under a DMCA takedown, or
        disabled. Each of these reasons is a fact about the repository that a later run keeps
        while git returns no head commit for it."""
        names = list(self.remotes)
        self._redirect_all()
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(), names, workers=1, finished_path=self.finished)
        first = self.finished.read_text()
        self._redirect_to_nowhere({json.loads(first.splitlines()[0])["repo"]})
        for reason in ("not_found_or_no_access", "dmca_takedown", "disabled_by_github"):
            with self.subTest(reason=reason):
                records = [json.loads(l) for l in first.splitlines() if l.strip()]
                records[0]["fail_row"] = f"{records[0]['repo']},https://x,{reason}"
                self.finished.write_text("".join(json.dumps(r) + "\n" for r in records))
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    run_collection(self._cfg(), names, workers=1, finished_path=self.finished)
                self.assertIn(f"records {len(names)} of {len(names)} repositories as finished, "
                              f"with 0 pending",
                              err.getvalue())

    def test_a_refusal_that_github_no_longer_gives_is_collected_again(self):
        """A repository that was made public again, or restored, between two collections is
        collected again."""
        names = list(self.remotes)
        self._redirect_all()
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(), names, workers=1, finished_path=self.finished)
        records = [json.loads(l) for l in self.finished.read_text().splitlines() if l.strip()]
        records[0]["fail_row"] = f"{records[0]['repo']},https://x,not_found_or_no_access"
        self.finished.write_text("".join(json.dumps(r) + "\n" for r in records))
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(self._cfg(), names, workers=1,
                                                finished_path=self.finished)
        self.assertEqual(fail_rows, [])

    def test_clones_failing_in_a_row_stop_the_step(self):
        names = list(self.remotes)
        self._redirect_to_nowhere(set(names))
        with self.assertRaises(_guards.ConsecutiveFailuresStop) as caught, \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(self._cfg(max_consecutive_failures=3), names, workers=1,
                           finished_path=self.finished)
        self.assertEqual(caught.exception.streak, 3)
        self.assertEqual(caught.exception.finished, 3)
        self.assertEqual(caught.exception.remaining, len(names) - 3)
        self.assertEqual(len(caught.exception.rows[1]), 3, "the three failures are recorded")
        self.assertEqual(len(read_finished(self.finished)), 3)

    def test_a_success_resets_the_streak(self):
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0], names[1], names[3], names[4]})
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(self._cfg(max_consecutive_failures=3), names,
                                                workers=1, finished_path=self.finished)
        self.assertEqual(len(fail_rows), 4)

    def test_the_guard_is_off_at_zero(self):
        names = list(self.remotes)
        self._redirect_to_nowhere(set(names))
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = run_collection(self._cfg(max_consecutive_failures=0), names,
                                                workers=1, finished_path=self.finished)
        self.assertEqual(len(fail_rows), len(names))

    def _main(self, *extra, probe_waits: tuple = ()) -> int:
        # The waits after a failed probe (`guards.PROBE_WAITS`) last hours. The tests therefore
        # run the command line under `probe_waits`, which has no waits by default.
        repo_csv = self.tmp / "list.csv"
        repo_csv.write_text("repo_name\n" + "\n".join(self.remotes) + "\n")
        collected = self.tmp / "collected"
        with contextlib.redirect_stderr(_Tee(sys.stderr)), \
                mock.patch.object(_guards, "PROBE_WAITS", probe_waits):
            return main(["--repo-csv", str(repo_csv),
                         "--collected-dir", str(collected),
                         "--collected-csv", str(collected / "repos.csv"),
                         "--fail-csv", str(collected / "clone_failed.csv"),
                         "--file-history", str(self.tmp / "fh.csv"),
                         "--submodules", str(self.tmp / "sub.csv"),
                         *_ai_commit_flags(self.tmp),
                         "--finished", str(self.finished), "--clone-dir", str(self.tmp / "scratch"),
                         "-l", str(self.tmp / "logs"), "-w", "1", "--max-tries", "1",
                         "--min-free-gb", "0", "--retry-pause", "0",
                         "--probe-url", self.NOWHERE, *extra])

    def _main_capturing(self, *extra, probe_waits: tuple = ()) -> int:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = self._main(*extra, probe_waits=probe_waits)
        self.err = err.getvalue()
        return code

    def test_the_outputs_are_synced_before_the_collection_ends(self):
        """Every CSV that the step wrote is on disk before the step ends its collection
        (`resume.close_collection`). The runner syncs the declared outputs of the step before it
        records the step, and the commit CSVs are not among them. Without this sync, a power
        loss after the exit could leave the runner to record the step from its record of the
        exit (`runner.record_exited_step`) over a collection that ended without them. The step
        syncs each file and does not depend on a sync of the whole file system, which never
        returns in this test."""
        self._redirect_to_nowhere(set())
        synced, at_close, fsync, close = set(), [], os.fsync, _resume.close_collection

        def recorded(fd):
            synced.add((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return fsync(fd)

        def closed(path):
            at_close.append(set(synced))
            return close(path)

        with file_system_sync_never_returns(), \
                mock.patch.object(os, "fsync", recorded), \
                mock.patch.object(_resume, "close_collection", closed):
            self.assertEqual(self._main("--retry-rounds", "0"), 0)
        self.assertFalse(_resume.collection_path(self.finished).exists())
        written = [*(self.tmp / "output").iterdir(), self.tmp / "collected" / "repos.csv",
                   self.tmp / "collected" / "clone_failed.csv"]
        self.assertIn(self.tmp / "output" / "all_ai_commits.csv", written)
        for path in written:
            with self.subTest(path=path.relative_to(self.tmp).as_posix()):
                self.assertIn((path.stat().st_dev, path.stat().st_ino), at_close[0])

    def test_main_exits_4_on_a_streak_and_writes_what_it_reached(self):
        self._redirect_to_nowhere(set(self.remotes))
        self.assertEqual(self._main("--max-consecutive-failures", "2"), 4)
        self.assertEqual((self.tmp / "collected" / "clone_failed.csv").read_text().count("\n"), 3)

    def test_main_exits_4_when_more_clones_failed_than_the_ceiling_allows(self):
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0], names[1], names[2]})
        self.assertEqual(self._main("--max-consecutive-failures", "0",
                                    "--max-clone-failures", "2"), 4)
        self.assertEqual(self._main("--max-consecutive-failures", "0",
                                    "--max-clone-failures", "3"), 0)

    def test_heads_that_git_did_not_return_count_toward_the_ceiling(self):
        """Repositories without a head commit from git are taken over with their earlier
        collection, which is not a collection of the day of the run. A run that kept many of
        them is not recorded as done. Above the ceiling, the head check first waits
        through every round, and at the ceiling it does not wait."""
        names = list(self.remotes)
        self._redirect_to_nowhere(set())
        self.assertEqual(self._main("--retry-rounds", "0"), 0)
        # A new snapshot of the sample, which queries the heads, as `--from repository_cloning`
        # requests it.
        _resume.forget_ended_collection(self.finished)
        self._redirect_to_nowhere({names[0], names[1], names[2]})
        clock = _HeadCheckClock()
        with mock.patch.object(_mod.reuse, "time", clock):
            self.assertEqual(self._main_capturing("--retry-rounds", "0",
                                                  "--max-consecutive-failures", "0",
                                                  "--max-clone-failures", "2"), 4)
        self.assertRegex(self.err, r"No head commit from git for 3 repositories, such as \S+, "
                                   r"after the retry rounds")
        self.assertEqual(clock.waits, [60.0, 120.0, 240.0, 480.0, 960.0, 1920.0])
        self.assertRegex(self.err, r"No head commit from git for 3 repositories, such as \S+, "
                                   r"after 6 rounds of waiting")
        clock = _HeadCheckClock()
        with mock.patch.object(_mod.reuse, "time", clock):
            self.assertEqual(self._main("--retry-rounds", "0", "--max-consecutive-failures", "0",
                                        "--max-clone-failures", "3"), 0)
        self.assertEqual(clock.waits, [])

    def _unresponsive_heads(self, names, until: float, clock) -> object:
        """A `clone.remote_head` that returns no head commit for `names` until `clock` reaches
        `until` seconds, and the real head otherwise."""
        real = _mod.reuse.clone.remote_head

        def remote_head(repo, token=None, use_ssh=False, authenticate=False):
            if repo in names and clock.now < until:
                return None
            return real(repo, token, use_ssh, authenticate=authenticate)
        return remote_head

    def test_a_burst_of_heads_without_a_response_is_waited_out(self):
        """GitHub can limit the rate of the head check's queries, or the network can fail for
        a few minutes, while the step queries the heads of thousands of repositories. In this
        test, git returns no head commit for five of the six heads for three minutes. The step
        waits, queries them again and takes every repository over. It does not keep five lines
        marked `head_no_response`, more than the ceiling of one, and exit 4."""
        names = list(self.remotes)
        self._redirect_to_nowhere(set())
        self.assertEqual(self._main("--retry-rounds", "0"), 0)
        lines = self.finished.read_text()
        _resume.forget_ended_collection(self.finished)
        clock = _HeadCheckClock()
        with mock.patch.object(_mod.reuse, "time", clock), \
                mock.patch.object(_mod.reuse.clone, "remote_head",
                                  self._unresponsive_heads(names[1:], 180.0, clock)):
            self.assertEqual(self._main_capturing("--retry-rounds", "0"), 0)
        self.assertEqual(clock.waits, [60.0, 120.0])
        self.assertIn("No head commit from git for 5 of 6 repositories, more than the 1 that "
                      "--max-clone-failures allows to retry. Querying them again in 60 s",
                      self.err)
        self.assertEqual(self.finished.read_text(), lines,
                         "no clone, and no line without a head commit")
        self.assertFalse(_resume.collection_path(self.finished).exists(), "the collection ended")

    def test_a_few_heads_without_a_response_cost_no_wait(self):
        """Repositories for which git never returns a head commit, as many as the ceiling
        allows, are taken over with their earlier collection without a wait. The retry rounds
        and the next collection query their heads again."""
        names = list(self.remotes)
        self._redirect_to_nowhere(set())
        self.assertEqual(self._main("--retry-rounds", "0"), 0)
        collected = self.tmp / "collected"
        rows = (collected / "repos.csv").read_text()
        _resume.forget_ended_collection(self.finished)
        clock = _HeadCheckClock()
        with mock.patch.object(_mod.reuse, "time", clock), \
                mock.patch.object(_mod.reuse.clone, "remote_head",
                                  self._unresponsive_heads(names[:2], float("inf"), clock)):
            self.assertEqual(self._main_capturing("--max-clone-failures", "2"), 0)
        self.assertEqual(clock.waits, [])
        finished = read_finished(self.finished)
        self.assertEqual([name for name in names if finished[name][8]], names[:2])
        self.assertEqual((collected / "repos.csv").read_text(), rows)
        for name in names[:2]:
            self.assertTrue((collected / repo_name_to_dir(name) / "CLAUDE.md").is_file())

    def test_from_repository_cloning_during_a_continued_collection_takes_a_new_snapshot(self):
        """A new image that changes the collector's code or the heuristics runs the step after
        its collection ended, and the step continues that collection. A stop, a kill, a power
        loss or any exit other than 0 leaves the continued collection in progress.
        `--from repository_cloning` at the next Start then requests a new snapshot
        (`runner.drop_completed_from`). The step begins a new collection, which queries every
        head and collects again the repository with a head that moved after the collection
        ended. It does not resume the continued collection, which would take over every
        repository of the earlier snapshot without querying its head."""
        from agentcontext import runner
        self.finished = self.tmp / _resume.FINISHED_NAME
        self._redirect_to_nowhere(set())
        self.assertEqual(self._main("--retry-rounds", "0"), 0)
        ended = _resume.read_collection(_resume.ended_path(self.finished))["collection"]
        _add_commit(self.remotes["o/a"], "CLAUDE.md", "# Claude, changed")
        moved = subprocess.run(["git", "-C", str(self.remotes["o/a"]), "rev-parse", "HEAD"],
                               capture_output=True, text=True, check=True).stdout.strip()
        # The continued collection that the new image runs, stopped before it collected anything.
        with contextlib.redirect_stderr(io.StringIO()):
            continued = _resume.open_collection(self.finished, list(self.remotes))
        self.assertEqual(continued["collection"], ended)
        step = runner.Step("repository_cloning", sys.executable, ["x.py"], produces=("x",),
                           root=str(self.tmp), completion=runner.COMPLETION_OUTPUT)
        runner.drop_completed_from({"completed": {"repository_cloning": "output:x"}}, [step],
                                   "repository_cloning")
        self.assertFalse(_resume.collection_path(self.finished).exists())
        self.assertEqual(_resume.replaced_collection(self.finished), ended)
        self.assertEqual(self._main_capturing("--retry-rounds", "0"), 0)
        self.assertIn("Starting the collection", self.err)
        line = read_finished(self.finished)["o/a"]
        self.assertNotEqual(line[10], ended)
        self.assertEqual(paths.row_column(line[0], "head_sha"), moved)

    def test_the_defaults_are_the_documented_ceilings(self):
        args = _mod._build_arg_parser().parse_args(["--repo-csv", "x.csv"])
        self.assertEqual(args.max_consecutive_failures, _guards.MAX_CONSECUTIVE_FAILURES)
        self.assertIsNone(args.max_clone_failures, "the share of the repositories read")
        self.assertEqual((args.retry_rounds, args.retry_pause),
                         (_guards.RETRY_ROUNDS, _guards.RETRY_PAUSE_SECONDS))
        self.assertEqual((_guards.MAX_CONSECUTIVE_FAILURES, _guards.RETRY_ROUNDS,
                          _guards.RETRY_PAUSE_SECONDS), (20, 2, 300.0))
        # The share is three times the share of clone failures in the March collection
        # (guards.CLONE_FAILURE_SHARE).
        self.assertEqual([_guards.clone_failure_ceiling(n) for n in (6, 67, 10_000, 36_710)],
                         [1, 1, 14, 51])

    def test_by_default_one_repository_in_six_to_retry_is_under_the_ceiling(self):
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0]})
        self.assertEqual(self._main("--max-consecutive-failures", "0"), 0)

    def test_by_default_two_repositories_in_six_to_retry_are_over_it(self):
        # A fixed ceiling of 50 would let most of a 67-repository run fail.
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0], names[1]})
        self.assertEqual(self._main_capturing("--max-consecutive-failures", "0"), 4)
        self.assertIn("more than the 1 that --max-clone-failures allows for 6 repositories", self.err)

    def test_a_clone_the_network_failed_is_collected_again_before_the_step_ends(self):
        """GitHub can return HTTP 400 to fetches for a few minutes, and the repositories that
        fail during that time would wait for an operator's Start. In this test, the failure ends
        during the pause, as a burst does, and the retry round collects the repositories in the
        same call."""
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0], names[1]})
        paused = []

        def pause(seconds):
            paused.append(seconds)
            self._redirect_to_nowhere(set())

        cfg = self._cfg(max_consecutive_failures=0, retry_rounds=2, retry_pause=300.0)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            main_rows, fail_rows, _, _ = _mod.collect_with_retries(
                cfg, names, workers=1, finished_path=self.finished, sleep=pause)
        self.assertEqual(fail_rows, [])
        self.assertEqual(paused, [300.0])
        self.assertIn("Retry round 1 of 2 collects 2 repositories again after a pause of 300 s",
                      err.getvalue())
        self.assertEqual(_mod.to_retry(cfg, names, _resume.read_finished(self.finished)), set())
        self.assertTrue(all(_row_field(r, "claude") == "TRUE" for r in main_rows))

    def test_a_failure_that_does_not_pass_is_tried_in_every_round_and_left(self):
        names = list(self.remotes)
        self._redirect_to_nowhere({names[0]})
        paused = []
        cfg = self._cfg(max_consecutive_failures=0, retry_rounds=2, retry_pause=300.0)
        with contextlib.redirect_stderr(io.StringIO()):
            _, fail_rows, _, _ = _mod.collect_with_retries(
                cfg, names, workers=1, finished_path=self.finished, sleep=paused.append)
        self.assertEqual(paused, [300.0, 300.0])
        self.assertEqual(fail_rows, [f"{names[0]},https://github.com/{names[0]},clone_failed"])

    def test_a_run_with_nothing_to_redo_does_not_pause(self):
        self._redirect_to_nowhere(set())
        paused = []
        cfg = self._cfg(retry_rounds=2, retry_pause=300.0)
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.collect_with_retries(cfg, list(self.remotes), workers=1,
                                      finished_path=self.finished, sleep=paused.append)
        self.assertEqual(paused, [])

    def test_repositories_are_taken_in_the_order_of_their_hash_and_written_in_input_order(self):
        """The input is the sample in alphabetical order. Taken in that order, the step would
        collect one owner's repositories back to back, and a stopped run would cover only the
        first names."""
        self._redirect_to_nowhere(set())
        names = list(self.remotes)
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, _, _, _ = run_collection(self._cfg(), names, workers=1,
                                                finished_path=self.finished)
        taken = [json.loads(line)["repo"] for line in self.finished.read_text().splitlines()]
        self.assertEqual(taken, ["o/d", "o/e", "o/f", "o/b", "o/a", "o/c"])
        self.assertNotEqual(taken, names)
        self.assertEqual(_mod.processing_order(list(reversed(names))), taken)
        for seed in ("1", "2"):
            result = subprocess.run(
                [sys.executable, "-c", "from agentcontext.steps.repository_cloning import processing_order; "
                 "print(*processing_order(['o/a','o/b','o/c','o/d','o/e','o/f']))"],
                env={**os.environ, "PYTHONHASHSEED": seed}, capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.split(), ["o/d", "o/e", "o/f", "o/b", "o/a", "o/c"])
        self.assertEqual(_mod.processing_order(["aa/1", "aa/2", "aa/3", "bb/1", "bb/2", "bb/3"]),
                         ["aa/2", "bb/2", "aa/3", "bb/3", "aa/1", "bb/1"])
        self.assertEqual([_row_field(r, "repo_name") for r in main_rows], names)

    def test_a_sample_without_a_repository_found_gone_continues_the_ended_collection(self):
        """A labeling that ran again after the collection ended found o/a gone, and the sample
        lacks it. The step reads the excluded repositories beside the sample and continues the
        collection that ended. It queries no head that the collection collected, so that the
        moved head of o/b is not collected again, and it removes the files of o/a. A sample that
        lacks o/c, which GitHub still serves, starts a new collection, which queries the heads."""
        self._redirect_to_nowhere(set())
        self.assertEqual(self._main_capturing("--retry-rounds", "0"), 0, self.err)
        ended = _resume.read_collection(_resume.ended_path(self.finished))["collection"]
        _add_commit(self.remotes["o/b"], "AGENTS.md", "# moved")
        sample = self.tmp / "classification" / "repos_annotated_included.csv"
        sample.parent.mkdir()
        (sample.parent / _mod.EXCLUDED_CSV_NAME).write_text(
            "repo_name,repo_url,reason,detected_language\n"
            "o/a,https://github.com/o/a,not_found_or_no_access,\n")
        queried, real = [], _mod.reuse.clone.remote_head

        def remote_head(repo, token=None, use_ssh=False, authenticate=False):
            queried.append(repo)
            return real(repo, token, use_ssh, authenticate=authenticate)

        sample.write_text("repo_name\n" + "".join(f"{name}\n" for name in self.remotes
                                                  if name != "o/a"))
        with mock.patch.object(_mod.reuse.clone, "remote_head", remote_head):
            self.assertEqual(self._main_capturing("--retry-rounds", "0", "--repo-csv",
                                                  str(sample)), 0, self.err)
        self.assertIn(f"Continuing the collection {ended}, which ended,", self.err)
        self.assertEqual(queried, [])
        rows = (self.tmp / "collected" / "repos.csv").read_text()
        self.assertNotIn("o/a,", rows)
        self.assertNotIn("TRUE", [_row_field(row, "agents.md") for row in rows.splitlines()[1:]])
        self.assertFalse((self.tmp / "collected" / "o§a").exists())
        sample.write_text("repo_name\n" + "".join(f"{name}\n" for name in self.remotes
                                                  if name not in ("o/a", "o/c")))
        with mock.patch.object(_mod.reuse.clone, "remote_head", remote_head):
            self.assertEqual(self._main_capturing("--retry-rounds", "0", "--repo-csv",
                                                  str(sample)), 0, self.err)
        self.assertIn("Starting the collection", self.err)
        self.assertIn("o/b", queried)

    def test_a_new_path_heuristic_after_the_collection_ended_collects_only_what_it_reaches(
            self):
        """The runner runs the step after a change of the path heuristics, and the collection
        over the list ended. The step continues that collection, queries no head and collects
        again only o/b, with a file that the new path pattern matches, although every head
        moved."""
        _add_commit(self.remotes["o/b"], "NEWLY-MATCHED.md", "# new")
        self._redirect_to_nowhere(set())
        self.assertEqual(self._main_capturing("--retry-rounds", "0"), 0, self.err)
        ended = _resume.read_collection(_resume.ended_path(self.finished))["collection"]
        for name in self.remotes:
            _add_commit(self.remotes[name], "MOVED.txt", "moved")
        before = self.finished.read_text().splitlines()
        real = _mod.heuristics.load_config_path_patterns()
        row = dict(real[0], tool="claude", mechanism="context_file", match_mode="basename",
                   content_format="markdown", path="NEWLY-MATCHED.md")
        queried, head = [], _mod.reuse.clone.remote_head

        def remote_head(repo, token=None, use_ssh=False, authenticate=False):
            queried.append(repo)
            return head(repo, token, use_ssh, authenticate=authenticate)
        with mock.patch.object(_mod.reuse.heuristics, "load_config_path_patterns",
                               lambda *a, **k: [*real, row]), \
                mock.patch.object(_mod.heuristics, "load_config_path_patterns",
                                  lambda *a, **k: [*real, row]), \
                mock.patch.object(_mod.reuse.clone, "remote_head", remote_head):
            self.assertEqual(self._main_capturing("--retry-rounds", "0"), 0, self.err)
        added = self.finished.read_text().splitlines()[len(before):]
        self.assertIn(f"Continuing the collection {ended}, which ended, over its list",
                      self.err)
        self.assertEqual(queried, [])
        self.assertEqual(sorted({json.loads(line)["repo"] for line in added}), ["o/b"])
        self.assertIn("1 with changed path heuristics that match their files", self.err)


class TestRunCollection(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote_a = self.tmp / "remote_a"
        self.remote_b = self.tmp / "remote_b"
        _init_git_repo(self.remote_a)
        _init_git_repo(self.remote_b)
        _add_commit(self.remote_a, "CLAUDE.md", "# Claude")
        _add_commit(self.remote_b, "GEMINI.md", "# Gemini")

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG_"):
                os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect_both(self) -> None:
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote_a}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/o/a.git"
        os.environ["GIT_CONFIG_KEY_1"] = f"url.file://{self.remote_b}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/o/b.git"

    def _cfg(self, name: str) -> "CollectionConfig":
        return CollectionConfig(
            collected_dir=self.tmp / f"out_{name}",
            clone_dir=self.tmp / f"repos_{name}",
            log_dir=self.tmp / f"logs_{name}",
            max_tries=1, base_delay=0,
        )

    def test_serial_preserves_input_order(self):
        self._redirect_both()
        main_rows, fail_rows, _facts, _subs = run_collection(self._cfg("s"), ["o/a", "o/b"], workers=1)
        self.assertEqual(_row_field(main_rows[0], "repo_name"), "o/a")
        self.assertEqual(_row_field(main_rows[1], "repo_name"), "o/b")
        self.assertEqual(fail_rows, [])

    def test_serial_sets_correct_flags(self):
        self._redirect_both()
        main_rows, _, _facts, _subs = run_collection(self._cfg("s"), ["o/a", "o/b"], workers=1)
        self.assertEqual(_row_field(main_rows[0], "claude"), "TRUE")
        self.assertEqual(_row_field(main_rows[1], "gemini"), "TRUE")

    def test_parallel_matches_serial(self):
        """The serial and the parallel run agree in every column but `scanned_at`. Each run
        reads `scanned_at` from its own clock, and the two runs differ in it whenever they
        straddle a second."""
        self._redirect_both()
        serial, _, _facts, _subs = run_collection(self._cfg("s"), ["o/a", "o/b"], workers=1)
        parallel, _, _facts, _subs = run_collection(self._cfg("p"), ["o/a", "o/b"], workers=2)
        self.assertEqual([_without_scan_time(r) for r in serial],
                         [_without_scan_time(r) for r in parallel])
        for row in serial + parallel:
            self.assertRegex(_row_field(row, "scanned_at"),
                             r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

    def test_parallel_preserves_input_order(self):
        self._redirect_both()
        main_rows, _, _facts, _subs = run_collection(self._cfg("p"), ["o/a", "o/b"], workers=2)
        self.assertEqual(_row_field(main_rows[0], "repo_name"), "o/a")
        self.assertEqual(_row_field(main_rows[1], "repo_name"), "o/b")

    def test_failed_repo_recorded_in_fail_rows(self):
        # o/a redirects to a valid remote. Without a redirect, o/c would reach the network. The
        # test redirects it to a port that nothing listens on, which keeps it offline and failing.
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote_a}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/o/a.git"
        os.environ["GIT_CONFIG_KEY_1"] = "url.http://127.0.0.1:9/nonexistent/c.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/o/c.git"
        main_rows, fail_rows, _facts, _subs = run_collection(self._cfg("s"), ["o/a", "o/c"], workers=1)
        self.assertEqual(len(main_rows), 2)
        self.assertEqual(fail_rows, ["o/c,https://github.com/o/c,clone_failed"])


class TestRunCollectionFinished(unittest.TestCase):
    """How run_collection resumes from the list of finished repositories."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote_a = self.tmp / "remote_a"
        self.remote_b = self.tmp / "remote_b"
        _init_git_repo(self.remote_a)
        _init_git_repo(self.remote_b)
        _add_commit(self.remote_a, "CLAUDE.md", "# Claude")
        _add_commit(self.remote_b, "GEMINI.md", "# Gemini")
        self.finished = self.tmp / "list of finished repositories.jsonl"

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG_"):
                os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _redirect_both(self) -> None:
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote_a}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/o/a.git"
        os.environ["GIT_CONFIG_KEY_1"] = f"url.file://{self.remote_b}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/o/b.git"

    def _cfg(self, name: str) -> "CollectionConfig":
        return CollectionConfig(
            collected_dir=self.tmp / f"out_{name}",
            clone_dir=self.tmp / f"repos_{name}",
            log_dir=self.tmp / f"logs_{name}",
            max_tries=1, base_delay=0,
        )

    def test_finished_records_every_repo(self):
        self._redirect_both()
        run_collection(self._cfg("l"), ["o/a", "o/b"], workers=1,
                       finished_path=self.finished)
        self.assertEqual(set(read_finished(self.finished)), {"o/a", "o/b"})

    def test_no_finished_path_writes_nothing(self):
        self._redirect_both()
        run_collection(self._cfg("n"), ["o/a"], workers=1)
        self.assertFalse(self.finished.exists())

    def _collect(self, cfg, repos, finished=None) -> list:
        with contextlib.redirect_stderr(io.StringIO()):
            return run_collection(cfg, repos, workers=1, finished_path=finished or self.finished)[0]

    def test_second_run_takes_over_a_repository_whose_head_has_not_moved(self):
        """The second run takes the repository over without a clone, and the list of finished
        repositories gains no line."""
        self._redirect_both()
        first = self._collect(self._cfg("r"), ["o/a"])
        lines = self.finished.read_text()
        self.assertEqual(self._collect(self._cfg("r"), ["o/a"]), first)
        self.assertEqual(self.finished.read_text(), lines)

    def test_resume_processes_only_pending_repo(self):
        self._redirect_both()
        first = self._collect(self._cfg("r2"), ["o/a"])
        main_rows = self._collect(self._cfg("r2"), ["o/a", "o/b"])
        self.assertEqual(main_rows[0], first[0])           # taken over, from the list of finished repositories
        self.assertEqual(_row_field(main_rows[1], "gemini"), "TRUE")  # collected by this run
        lines = [json.loads(l)["repo"] for l in self.finished.read_text().splitlines()]
        self.assertEqual(lines, ["o/a", "o/b"])

    def test_a_head_that_moved_is_collected_again(self):
        self._redirect_both()
        first = self._collect(self._cfg("m"), ["o/a"])
        _add_commit(self.remote_a, "AGENTS.md", "# Agents")
        again = self._collect(self._cfg("m"), ["o/a"])
        self.assertNotEqual(_row_field(again[0], "head_sha"), _row_field(first[0], "head_sha"))
        self.assertEqual(_row_field(again[0], "agents.md"), "TRUE")

    def test_a_resumed_collection_takes_over_what_it_confirmed_whatever_the_head(self):
        """A collection that queried a repository's head and took the repository over from an
        earlier collection records the takeover. A resumed run of the same collection takes the
        repository over without querying the head again, although the head moved in between.
        The collection queries the head of a repository of an earlier collection that it has
        not reached yet."""
        self._redirect_both()
        cfg = self._cfg("t")
        self._collect(cfg, ["o/a", "o/b"])
        with mock.patch.object(_resume.logs, "info"):
            collection = _resume.open_collection(self.finished, ["o/a", "o/b"])
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, ["o/a"], workers=1, finished_path=self.finished,
                           collection=collection)
        lines = self.finished.read_text()
        _add_commit(self.remote_a, "AGENTS.md", "# moved")
        _add_commit(self.remote_b, "AGENTS.md", "# moved")
        with mock.patch.object(_resume.logs, "info"):
            resumed = _resume.open_collection(self.finished, ["o/a", "o/b"])
        self.assertEqual(resumed["collection"], collection["collection"])
        self.assertEqual(resumed["taken_over"], {"o/a"})
        with contextlib.redirect_stderr(io.StringIO()):
            rows = run_collection(cfg, ["o/a", "o/b"], workers=1, finished_path=self.finished,
                                  collection=resumed)[0]
        added = [json.loads(l)["repo"] for l in self.finished.read_text()[len(lines):].splitlines()]
        self.assertEqual(set(added), {"o/b"}, "o/a was collected again, or o/b was not")
        self.assertEqual(_row_field(rows[0], "agents.md"), "FALSE")
        self.assertEqual(_row_field(rows[1], "agents.md"), "TRUE")

    def test_a_confirmed_repository_whose_line_still_has_no_head_is_queried_again(self):
        """The step saves the record of the collection after the head query and before it
        writes the line without `head_no_response`. A stop in between leaves the repository in
        `taken_over` with the mark on its line. A resumed run that took such a repository over
        without a query would never clear the mark and would count the repository toward the
        ceiling on every Start."""
        self._redirect_both()
        cfg = self._cfg("w3")
        self._collect(cfg, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        append_finished(self.finished, "o/a", *line[:4], collected_under=line[6],
                        head_no_response=True, collection="an earlier collection")
        with mock.patch.object(_resume.logs, "info"):
            collection = _resume.open_collection(self.finished, ["o/a"])
        collection["taken_over"].add("o/a")
        _resume.save_collection(self.finished, collection)
        with mock.patch.object(_resume.logs, "info"):
            resumed = _resume.open_collection(self.finished, ["o/a"])
        self.assertEqual(resumed["taken_over"], {"o/a"})
        queried, real = [], _mod.reuse.clone.remote_head

        def remote_head(repo, token=None, use_ssh=False, authenticate=False):
            queried.append(repo)
            return real(repo, token, use_ssh, authenticate=authenticate)

        with mock.patch.object(_mod.reuse.clone, "remote_head", remote_head), \
                contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, ["o/a"], workers=1, finished_path=self.finished,
                           collection=resumed)
        self.assertEqual(queried, ["o/a"])
        self.assertFalse(read_finished(self.finished)["o/a"][8])
        self.assertEqual(_mod.heads_without_response(["o/a"], read_finished(self.finished)), [])

    def test_a_line_without_what_it_was_collected_under_is_collected_again(self):
        """A line without `collected_under`, as an earlier collector wrote it, does not show
        what its tree was collected under."""
        append_finished(self.finished, "o/a", "SENTINEL-WITHOUT-RECORD", None)
        self._redirect_both()
        main_rows = self._collect(self._cfg("w"), ["o/a"])
        self.assertEqual(_row_field(main_rows[0], "claude.md"), "TRUE")

    def test_a_repository_collected_by_other_code_is_collected_again(self):
        self._redirect_both()
        self._collect(self._cfg("c"), ["o/a"])
        with mock.patch.object(_mod.reuse, "code_fingerprint", _another_collector()):
            self._collect(self._cfg("c"), ["o/a"])
        self.assertEqual(len(self.finished.read_text().splitlines()), 3,
                         "the line written before the clone, and the clone's")

    def test_a_recorded_change_of_the_collector_collects_again_only_what_it_reaches(self):
        """An image with a later version of the collector's code takes the repository over when
        the record of the versions declares a change that does not reach the repository, either
        because the change reaches no repository or because the file list of the repository does
        not meet the predicate of the change. The list of finished repositories then gains no
        line. A change that reaches every repository collects the repository again."""
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("v"), file_lists_dir=self.tmp / "lists_v")
        first = self._collect(cfg, ["o/a"])
        earlier = read_finished(self.finished)["o/a"][6]["collector"]
        lines = self.finished.read_text()
        for reach, again in ((_mod.reuse.code_versions.NO_REPOSITORY, False),
                             (_mod.reuse.code_versions.linked_skill_file, False),
                             (_mod.reuse.code_versions.EVERY_REPOSITORY, True)):
            record = (_mod.reuse.code_versions.Version(earlier, "c1", "earlier", None),
                      _mod.reuse.code_versions.Version("another collector", "c2", "a change",
                                                       reach))
            with self.subTest(reach=getattr(reach, "__name__", reach)), \
                    mock.patch.object(_mod.reuse, "code_fingerprint", _another_collector()), \
                    mock.patch.object(_mod.reuse.code_versions, "COLLECTOR", record):
                rows = self._collect(cfg, ["o/a"])
                if again:
                    self.assertEqual(_row_field(rows[0], "claude.md"), "TRUE")
                    self.assertEqual(len(self.finished.read_text().splitlines()), 3,
                                     "the line written before the clone, and the clone's")
                    self.assertEqual(read_finished(self.finished)["o/a"][6]["collector"],
                                     "another collector")
                else:
                    self.assertEqual(rows, first, "the row of the earlier collection")
                    self.assertEqual(self.finished.read_text(), lines)

    def _lines(self) -> list:
        return [json.loads(l) for l in self.finished.read_text().splitlines() if l.strip()]

    def test_a_head_that_git_does_not_return_is_queried_again_rather_than_cloned(self):
        """When GitHub cannot be reached during the head check, the step keeps the earlier
        collection, and the line records that git returned no head commit. The next run queries
        the head again and takes the repository over when its head has not moved, rather than
        cloning it again whatever its head."""
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("u"), commits_dir=self.tmp / "commits_u")
        first = self._collect(cfg, ["o/a"])
        tree = cfg.collected_dir / "o§a" / "CLAUDE.md"
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        self.assertEqual(self._collect(cfg, ["o/a"]), first)
        lines = self._lines()
        self.assertEqual(len(lines), 2,
                         "one line, which records that git returned no head commit")
        self.assertTrue(lines[-1].get("head_no_response"))
        self.assertFalse(lines[-1].get("redo") or lines[-1].get("retrying"))
        self.assertEqual(lines[-1]["row"], lines[0]["row"])
        self.assertTrue(tree.is_file())
        self._redirect_both()
        self.assertEqual(self._collect(cfg, ["o/a"]), first)
        lines = self._lines()
        self.assertEqual(len(lines), 3, "no clone, a line without the mark")
        self.assertFalse(lines[-1].get("head_no_response"))
        self.assertFalse(lines[-1].get("retrying"))

    def test_a_head_that_github_refuses_collects_the_repository_again(self):
        """The step collects a repository that is gone from GitHub again, and the clone records
        the refusal. The step does not keep it as a repository for which git returned no head
        commit."""
        self._redirect_both()
        cfg = self._cfg("r3")
        self._collect(cfg, ["o/a"])
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.tmp / 'gone'}.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            _rows, fail_rows, _facts, _subs = run_collection(cfg, ["o/a"], workers=1,
                                                            finished_path=self.finished)
        self.assertEqual(fail_rows, ["o/a,https://github.com/o/a,not_found_or_no_access"])
        self.assertFalse((cfg.collected_dir / "o§a").exists())

    def test_the_retry_rounds_query_a_head_that_git_did_not_return_again(self):
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("q"), retry_rounds=1, retry_pause=0)
        self._collect(cfg, ["o/a"])
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.collect_with_retries(cfg, ["o/a"], workers=1, finished_path=self.finished,
                                 sleep=lambda seconds: self._redirect_both())
        self.assertFalse(read_finished(self.finished)["o/a"][8])
        self.assertFalse(any(line.get("retrying") for line in self._lines()))

    def _left_cfg(self) -> "CollectionConfig":
        return dataclasses.replace(self._cfg("left"), commits_dir=self.tmp / "commits_left",
                                   file_lists_dir=self.tmp / "lists_left",
                                   workflows_dir=self.tmp / "workflows_left")

    def test_a_name_that_changed_only_in_case_keeps_its_new_collection(self):
        """GitHub's casing of a name can change between retrievals. On a file system that folds
        case, such as the default file system of macOS, the files of the name that left the
        input are the files of the new name, and removing them would remove the new collection.
        On a file system that does not fold case, the old name has files of its own, and the
        step removes them."""
        os.environ["GIT_CONFIG_COUNT"] = "2"
        for i, name in enumerate(("o/a", "O/A")):
            os.environ[f"GIT_CONFIG_KEY_{i}"] = f"url.file://{self.remote_a}.insteadOf"
            os.environ[f"GIT_CONFIG_VALUE_{i}"] = f"https://github.com/{name}.git"
        cfg = self._left_cfg()
        self._collect(cfg, ["o/a"])
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.collect_with_retries(cfg, ["O/A"], workers=1, finished_path=self.finished)
            _mod.reuse.remove_left(cfg, ["O/A"], read_finished(self.finished))
        names = {path.name for path in cfg.collected_dir.iterdir()}
        self.assertIn("O§A", names)
        self.assertTrue((cfg.collected_dir / "O§A" / "CLAUDE.md").is_file())
        self.assertTrue(commits.shard_paths(cfg.commits_dir, "O/A")[1].is_file())
        self.assertTrue((cfg.file_lists_dir / f"O§A{_clone.FILE_LIST_SUFFIX}").is_file())
        self.assertNotIn("o§a", names)

    def test_a_repository_that_left_with_a_line_and_no_tree_loses_its_records(self):
        """A repository from which the collector copied nothing has no tree, and its
        per-repository commit record, file list and clone log must not stay for the next
        assembly to read."""
        self._redirect_both()
        cfg = self._left_cfg()
        self._collect(cfg, ["o/a"])
        shutil.rmtree(cfg.collected_dir / "o§a")
        record = commits.shard_paths(cfg.commits_dir, "o/a")[1]
        self.assertTrue(record.is_file())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_mod.reuse.remove_left(cfg, ["o/b"], read_finished(self.finished)), 1)
        self.assertFalse(record.exists())
        self.assertFalse((cfg.file_lists_dir / f"o§a{_clone.FILE_LIST_SUFFIX}").exists())

    def test_a_file_of_the_list_under_another_case_is_kept_and_the_others_go(self):
        """On any file system, the step keeps a file of the repository that left where it is
        the same file as the file of the repository in the list, and removes it where it is
        not."""
        cfg = self._left_cfg()
        for name in ("o/a", "O/A"):
            tree = cfg.collected_dir / name.replace("/", "§")
            tree.mkdir(parents=True, exist_ok=True)
            (tree / "CLAUDE.md").write_text(f"# {name}\n")
            cfg.file_lists_dir.mkdir(exist_ok=True)
            (cfg.file_lists_dir / f"{name.replace('/', '§')}{_clone.FILE_LIST_SUFFIX}").write_text(name)
        folds = lambda path, other: str(path).casefold() == str(other).casefold()
        with mock.patch.object(_mod.reuse, "_same_file", folds), \
                contextlib.redirect_stderr(io.StringIO()):
            _mod.reuse.remove_left(cfg, ["O/A"], {})
        self.assertTrue((cfg.collected_dir / "o§a" / "CLAUDE.md").is_file())
        self.assertTrue((cfg.file_lists_dir / f"o§a{_clone.FILE_LIST_SUFFIX}").is_file())
        with mock.patch.object(_mod.reuse, "_same_file", lambda path, other: False), \
                contextlib.redirect_stderr(io.StringIO()):
            _mod.reuse.remove_left(cfg, ["O/A"], {})
        self.assertFalse((cfg.file_lists_dir / f"o§a{_clone.FILE_LIST_SUFFIX}").exists())

    def test_a_line_marked_to_redo_is_collected_again(self):
        """`redo` alone makes the step collect a repository again, even with the tree on disk
        and the per-repository commit record complete. A line gets the mark, for example, for a
        history that a machine failure left unread or for a failed retry after the head
        moved."""
        self._redirect_both()
        cfg = self._cfg("d")
        self._collect(cfg, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        append_finished(self.finished, "o/a", *line[:4], redo=True, collected_under=line[6])
        self._collect(cfg, ["o/a"])
        lines = self._lines()
        self.assertTrue(lines[-2].get("retrying"), "the line written before the clone")
        self.assertFalse(lines[-1].get("redo"))

    def test_a_failed_retry_keeps_the_earlier_snapshot_its_record_and_the_mark_to_redo(self):
        """A repository is collected again for a changed collector, and the clone fails on the
        network. The earlier snapshot stays with the record of what it was collected under, so
        that no run takes it for a collection of the new collector. Its line is marked so that
        the retry rounds and the next run try again."""
        self._redirect_both()
        cfg = self._cfg("p")
        self._collect(cfg, ["o/a"])
        under = read_finished(self.finished)["o/a"][6]
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        with mock.patch.object(_mod.reuse, "code_fingerprint", _another_collector()):
            self._collect(cfg, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        self.assertIsNone(line[1])
        self.assertTrue(line[4], "marked to redo")
        self.assertEqual(line[6], under)
        self.assertTrue((cfg.collected_dir / "o§a" / "CLAUDE.md").is_file())

    def test_a_retry_whose_tree_is_gone_and_whose_clone_fails_is_a_clone_failure(self):
        """The step keeps the earlier snapshot only where its tree is on disk. Without the tree,
        the line would record that the repository was collected with nothing there."""
        self._redirect_both()
        cfg = self._cfg("t")
        self._collect(cfg, ["o/a"])
        shutil.rmtree(cfg.collected_dir / "o§a")
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            _rows, fail_rows, _facts, _subs = run_collection(cfg, ["o/a"], workers=1,
                                                            finished_path=self.finished)
        self.assertEqual(fail_rows, ["o/a,https://github.com/o/a,clone_failed"])
        self.assertEqual(read_finished(self.finished)["o/a"][1],
                         "o/a,https://github.com/o/a,clone_failed")

    def test_a_stop_before_a_retry_starts_leaves_the_earlier_collection_to_keep(self):
        """The step writes the line marked `retrying` right before its repository starts. A stop
        of the run before then, here for low free space before the first clone, leaves the line
        of the earlier collection in force. A retry of the next run that the time bound stops
        then keeps that collection. Lines written for every pending repository at the start of
        the run would tell the next run that each retry had begun, and the step would remove
        the tree, the shards and the file list of each repository that it could not clone
        again."""
        self._redirect_both()
        for workers in (1, 2):
            with self.subTest(workers=workers):
                name = f"stop{workers}"
                cfg = dataclasses.replace(self._cfg(name), max_tries=6,
                                          commits_dir=self.tmp / f"commits_{name}",
                                          file_lists_dir=self.tmp / f"lists_{name}")
                finished = self.tmp / f"{name}.jsonl"
                self._collect(cfg, ["o/a"], finished)
                kept = [cfg.collected_dir / "o§a" / "CLAUDE.md",
                        commits.shard_paths(cfg.commits_dir, "o/a")[1],
                        cfg.file_lists_dir / f"o§a{_clone.FILE_LIST_SUFFIX}"]
                with mock.patch.object(_mod.reuse, "code_fingerprint", _another_collector()):
                    with contextlib.redirect_stderr(io.StringIO()), \
                            self.assertRaises(_guards.LowDiskStop):
                        run_collection(dataclasses.replace(cfg, min_free_bytes=1 << 60),
                                       ["o/a"], workers=workers, finished_path=finished)
                    self.assertFalse(read_finished(finished)["o/a"][7], "no retry began")
                    with contextlib.redirect_stderr(io.StringIO()):
                        _rows, fail_rows, _, _ = run_collection(
                            dataclasses.replace(cfg, clone_timeout=0.001), ["o/a"],
                            workers=workers, finished_path=finished)
                self.assertEqual(fail_rows, [])
                self.assertEqual([path for path in kept if not path.is_file()], [])
                self.assertEqual(read_finished(finished)["o/a"][9], 0.001)

    def test_a_repository_without_configuration_keeps_its_collection_past_the_time_bound(self):
        """The earlier collection of a repository from which the collector copied nothing is
        its row and its file list, with no directory in the collected tree. A retry for a head
        that moved, which the time bound stops, keeps both. If the step removed them, the
        repository would go to `clone_failed.csv` and leave the denominator of the adoption
        shares, while a configured repository would stay in it with its earlier collection."""
        remote = self.tmp / "remote_plain"
        _init_git_repo(remote)
        os.environ.update({"GIT_CONFIG_COUNT": "1",
                           "GIT_CONFIG_KEY_0": f"url.file://{remote}.insteadOf",
                           "GIT_CONFIG_VALUE_0": "https://github.com/o/plain.git"})
        cfg = dataclasses.replace(self._cfg("plain"), max_tries=6,
                                  commits_dir=self.tmp / "commits_plain",
                                  file_lists_dir=self.tmp / "lists_plain")
        first = self._collect(cfg, ["o/plain"])
        listing = cfg.file_lists_dir / f"o§plain{_clone.FILE_LIST_SUFFIX}"
        self.assertTrue(listing.is_file())
        self.assertFalse((cfg.collected_dir / "o§plain").exists())
        _add_commit(remote, "src/main.py", "print()\n")
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, fail_rows, _, _ = run_collection(
                dataclasses.replace(cfg, clone_timeout=0.001), ["o/plain"], workers=1,
                finished_path=self.finished)
        self.assertEqual(fail_rows, [])
        self.assertEqual(main_rows, first)
        self.assertTrue(listing.is_file())
        self.assertEqual(read_finished(self.finished)["o/plain"][9], 0.001)

    def test_a_retry_for_changed_path_heuristics_keeps_nothing_of_the_earlier_collection(self):
        """A path pattern added since the collection matches a file of the repository's file
        list, and the clone of the retry fails on the network. A kept earlier collection would
        keep the path heuristics that it was collected under, and the published dataset would
        lack the files that the new path pattern matches. The repository is therefore a clone
        failure, which a later run retries."""
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("ph"), heuristics_dir=self.tmp / "heuristics_ph",
                                  file_lists_dir=self.tmp / "lists_ph")
        self._collect(cfg, ["o/a"])
        rows = _mod.heuristics.load_config_path_patterns()
        added = [*rows, dict(rows[0], path="README.md", match_mode="basename")]
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        with mock.patch.object(_mod.heuristics, "load_config_path_patterns",
                               lambda *args, **kwargs: added), \
                contextlib.redirect_stderr(io.StringIO()):
            _rows, fail_rows, _, _ = run_collection(cfg, ["o/a"], workers=1,
                                                    finished_path=self.finished)
        self.assertEqual(fail_rows, ["o/a,https://github.com/o/a,clone_failed"])
        self.assertFalse((cfg.collected_dir / "o§a").exists())
        self.assertFalse((cfg.file_lists_dir / f"o§a{_clone.FILE_LIST_SUFFIX}").exists())

    def test_a_failed_retry_of_a_line_from_before_the_imports_keeps_its_collection(self):
        """A line from a collector without the imported tree records no import heuristics and
        no imported tree. When the step retries such a line for its other records and the clone
        fails on the network, the line keeps its earlier collection, as any other line does.
        The import heuristics decide what the imported tree holds, not the collection of the
        configuration artifacts, and the import detection step reads the imports from what the
        trees hold."""
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("pre"), heuristics_dir=self.tmp / "heuristics_pre",
                                  file_lists_dir=self.tmp / "lists_pre",
                                  imported_dir=self.tmp / "imported_pre")
        self._collect(cfg, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        under = {key: value for key, value in line[6].items() if key != "imports"}
        under["records"] = {key: value for key, value in under["records"].items()
                            if key != "imported"}
        append_finished(self.finished, "o/a", *line[:4], collected_under=under)
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            _rows, fail_rows, _, _ = run_collection(cfg, ["o/a"], workers=1,
                                                    finished_path=self.finished)
        self.assertEqual(fail_rows, [])
        self.assertTrue((cfg.collected_dir / "o§a" / "CLAUDE.md").is_file())
        self.assertEqual(read_finished(self.finished)["o/a"][6], under)

    def test_a_default_branch_renamed_at_the_same_commit_is_collected_again(self):
        """The row's branch is the ref that `github_link` names, and the head check therefore
        compares the branch as well as the commit."""
        self._redirect_both()
        cfg = self._cfg("b")
        first = self._collect(cfg, ["o/a"])
        subprocess.run(["git", "-C", str(self.remote_a), "branch", "-m",
                        _row_field(first[0], "branch"), "trunk"], capture_output=True, check=True)
        again = self._collect(cfg, ["o/a"])
        self.assertEqual(_row_field(again[0], "branch"), "trunk")
        self.assertEqual(_row_field(again[0], "head_sha"), _row_field(first[0], "head_sha"))

    def test_a_head_that_git_returns_on_the_second_try_is_taken_over(self):
        """When one `git ls-remote` returns no head commit, the step queries the head again
        before it marks the line `head_no_response`."""
        self._redirect_both()
        cfg = self._cfg("h2")
        self._collect(cfg, ["o/a"])
        lines = self.finished.read_text()
        real, tried = _mod.reuse.clone.remote_head, set()

        def first_try_unanswered(repo, token=None, use_ssh=False, authenticate=False):
            if repo not in tried:
                tried.add(repo)
                return None
            return real(repo, token, use_ssh, authenticate=authenticate)

        with mock.patch.object(_mod.reuse.clone, "remote_head", first_try_unanswered):
            self._collect(cfg, ["o/a"])
        self.assertEqual(self.finished.read_text(), lines)

    def test_a_repository_whose_commits_cannot_be_classified_again_is_collected_again(self):
        """The commit heuristics changed, the per-repository commit record is older than the
        recording of the scope, and the commit log is gone. The commits cannot be classified
        again from that record."""
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("n"), commits_dir=self.tmp / "commits_n")
        self._collect(cfg, ["o/a"])
        record_path = commits.shard_paths(cfg.commits_dir, "o/a")[1]
        record = json.loads(record_path.read_text())
        record.pop("commit_evidence", None)
        record_path.write_text(json.dumps(dict(record, classified_by="earlier heuristics")))
        commits.stored_log_path(cfg.commits_dir, "o/a").unlink()
        lines = len(self._lines())
        self._collect(cfg, ["o/a"])
        self.assertGreater(len(self._lines()), lines)
        self.assertTrue(commits.stored_log_path(cfg.commits_dir, "o/a").is_file())

    def test_a_repository_collected_under_another_git_is_collected_again(self):
        """`created_at`, `#commits` and `first_commit_sha` change with the version of git. A
        line taken over from another version would keep that version's results beside the
        results of the new version."""
        self._redirect_both()
        self._collect(self._cfg("g"), ["o/a"])
        self.assertEqual(read_finished(self.finished)["o/a"][6]["git"], _clone.git_version())
        with mock.patch.object(_mod.reuse.clone, "git_version", lambda: "git version 0.0.0"):
            self._collect(self._cfg("g"), ["o/a"])
        self.assertEqual(len(self.finished.read_text().splitlines()), 3,
                         "the line written before the clone, and the clone's")
        self.assertEqual(read_finished(self.finished)["o/a"][6]["git"], "git version 0.0.0")

    def test_a_repository_collected_with_other_records_is_collected_again(self):
        """A run that kept no file lists leaves nothing against which a later run can compare
        changed path heuristics with a repository's files."""
        self._redirect_both()
        self._collect(self._cfg("k"), ["o/a"])
        self._collect(dataclasses.replace(self._cfg("k"), file_lists_dir=self.tmp / "lists_k"),
                      ["o/a"])
        self.assertEqual(len(self.finished.read_text().splitlines()), 3)
        self.assertTrue(read_finished(self.finished)["o/a"][6]["records"]["file_list"])

    def _heuristics_cfg(self, name: str) -> "CollectionConfig":
        return dataclasses.replace(self._cfg(name), file_lists_dir=self.tmp / f"lists_{name}",
                                   heuristics_dir=self.tmp / f"heuristics_{name}")

    def test_path_heuristics_are_held_against_the_file_list(self):
        """A changed path pattern that matches none of the repository's paths leaves the
        repository taken over, and a path pattern that matches one makes the step collect the
        repository again."""
        self._redirect_both()
        cfg = self._heuristics_cfg("h")
        self._collect(cfg, ["o/a", "o/b"])
        real = _mod.heuristics.load_config_path_patterns()
        row = dict(real[0], tool="claude", mechanism="context_file", match_mode="basename",
                   content_format="markdown")
        cases = [("NOT-IN-EITHER.md", set()), ("CLAUDE.md", {"o/a"})]
        for path, again in cases:
            with self.subTest(path=path):
                before = self.finished.read_text().splitlines()
                with mock.patch.object(_mod.reuse.heuristics, "load_config_path_patterns",
                                       lambda: [*real, dict(row, path=path)]):
                    self._collect(cfg, ["o/a", "o/b"])
                added = self.finished.read_text().splitlines()[len(before):]
                self.assertEqual({json.loads(l)["repo"] for l in added}, again)
        kept = [json.loads(path.read_text()) for path in cfg.heuristics_dir.glob("*.json")]
        self.assertEqual(sum("rows" in each for each in kept), 3,
                         "one version of the path heuristics for each of the three runs")

    def test_changed_path_heuristics_reach_a_collection_that_the_time_bound_kept(self):
        """A repository that kept its earlier collection when the time bound stopped its retry is
        not cloned again under the same bound for the rest of the collection. A path pattern
        added within the collection that matches a file of the repository's file list must not
        leave the line taken over, because the published dataset would lack the files that the
        new path pattern matches. The retry keeps nothing of a collection under other path
        heuristics."""
        self._redirect_both()
        cfg = self._heuristics_cfg("tb")
        self._collect(cfg, ["o/a"])
        short = dataclasses.replace(cfg, clone_timeout=0.001, max_tries=6)
        with mock.patch.object(_mod.reuse, "code_fingerprint", _another_collector()):
            self._collect(short, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        self.assertEqual((line[1], line[9]), (None, 0.001), "the earlier collection is kept")
        real = _mod.heuristics.load_config_path_patterns()
        row = dict(real[0], tool="claude", mechanism="context_file", path="CLAUDE.md",
                   match_mode="basename", content_format="markdown")
        # Each clone starts its log afresh. Only a run that skips the repository keeps the
        # line below.
        log = cfg.log_dir / "o§a.log"
        log.write_text("not cloned since\n", encoding="utf-8")
        with mock.patch.object(_mod.reuse.heuristics, "load_config_path_patterns",
                               lambda: [*real, row]):
            self._collect(short, ["o/a"])
        self.assertNotIn("not cloned since", log.read_text())
        self.assertEqual(_resume.outcome_of(read_finished(self.finished)["o/a"][1]),
                         _clone.TIME_BOUND_EXCEEDED)
        self.assertFalse((cfg.collected_dir / "o§a").exists())

    def test_a_planning_row_added_within_the_collection_reaches_a_collection_that_the_time_bound_kept(self):
        """This test repeats the test above with a heuristic of the planning documents in place
        of a path pattern. The decision to keep the earlier collection after the failed retry
        has to read the heuristics of the planning documents too. Otherwise, it would keep the
        collection without the planning document that the new heuristic copies."""
        _add_commit(self.remote_a, "notes/plan-1.md", "# Plan\n")
        self._redirect_both()
        cfg = self._heuristics_cfg("tp")
        self._collect(cfg, ["o/a"])
        short = dataclasses.replace(cfg, clone_timeout=0.001, max_tries=6)
        with mock.patch.object(_mod.reuse, "code_fingerprint",
                               lambda files, **options: "another collector"):
            self._collect(short, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        self.assertEqual((line[1], line[9]), (None, 0.001), "the earlier collection is kept")
        real = _mod.reuse.planning.load_planning_document_patterns()
        row = dict(real[0], framework="notes", document="plan", pattern=r"notes/[^/]+\.md",
                   match_mode="any_depth", anchor="", example="notes/plan-1.md")
        with mock.patch.object(_mod.reuse.planning, "load_planning_document_patterns",
                               lambda path=None: [*real, row]):
            self._collect(short, ["o/a"])
        self.assertEqual(_resume.outcome_of(read_finished(self.finished)["o/a"][1]),
                         _clone.TIME_BOUND_EXCEEDED)
        self.assertFalse((cfg.collected_dir / "o§a").exists(),
                         "the collection without notes/plan-1.md was kept")

    def test_a_retry_keeps_no_collection_from_before_the_planning_documents(self):
        """A repository collected without the heuristics of the planning documents is collected
        again where these heuristics match its files. A retry that the time bound stops keeps
        nothing of the collection without the planning documents."""
        _add_commit(self.remote_a, "openspec/specs/auth/spec.md", "# Auth\n")
        self._redirect_both()
        cfg = self._heuristics_cfg("pe")
        with mock.patch.object(_mod.reuse.planning, "load_planning_document_patterns",
                               lambda path=None: []):
            self._collect(cfg, ["o/a"])
        self.assertFalse((cfg.collected_dir / "o§a/openspec").exists())
        self._collect(dataclasses.replace(cfg, clone_timeout=0.001, max_tries=6), ["o/a"])
        self.assertEqual(_resume.outcome_of(read_finished(self.finished)["o/a"][1]),
                         _clone.TIME_BOUND_EXCEEDED)
        self.assertFalse((cfg.collected_dir / "o§a").exists(),
                         "the collection without openspec/specs/auth/spec.md was kept")

    def test_unchanged_path_heuristics_need_no_file_list(self):
        """A file list that git could not write is no reason to clone again while the path
        heuristics are unchanged since the repository's collection."""
        self._redirect_both()
        cfg = self._heuristics_cfg("n")
        self._collect(cfg, ["o/a", "o/b"])
        for path in cfg.file_lists_dir.iterdir():
            path.unlink()
        lines = self.finished.read_text()
        self._collect(cfg, ["o/a", "o/b"])
        self.assertEqual(self.finished.read_text(), lines)

    def test_a_change_to_the_commit_heuristics_classifies_again_without_a_clone(self):
        self._redirect_both()
        cfg = dataclasses.replace(self._cfg("k"), commits_dir=self.tmp / "commits_k")
        self._collect(cfg, ["o/a"])
        record_path = commits.shard_paths(cfg.commits_dir, "o/a")[1]
        record = json.loads(record_path.read_text())
        record_path.write_text(json.dumps(dict(record, classified_by="earlier heuristics")))
        lines = self.finished.read_text()
        self._collect(cfg, ["o/a"])
        self.assertEqual(self.finished.read_text(), lines)
        self.assertEqual(json.loads(record_path.read_text())["classified_by"],
                         commits.CLASSIFIED_BY)

    def test_resumed_result_matches_uninterrupted_run(self):
        """The resumed run matches the uninterrupted run in every column but `scanned_at`, which
        the resumed run reads from its own clock for the repository that it collects itself.
        The repository that it takes from the list of finished repositories keeps the scan time
        of the run that collected it, and that row matches outright."""
        self._redirect_both()
        full, full_fail, _facts, _subs = run_collection(self._cfg("full"), ["o/a", "o/b"], workers=1,
                                         finished_path=self.tmp / "full.jsonl")
        # Simulate a crash after o/a: a list of finished repositories with only the first
        # repository.
        partial = self.tmp / "partial.jsonl"
        first = read_finished(self.tmp / "full.jsonl")["o/a"]
        append_finished(partial, "o/a", first[0], first[1], collected_under=first[6])
        # The resumed run uses the same tree, because a resumed run that finds a recorded
        # repository's directory gone collects the repository again.
        resumed, resumed_fail, _facts, _subs = run_collection(self._cfg("full"), ["o/a", "o/b"],
                                               workers=1, finished_path=partial)
        self.assertEqual([_without_scan_time(r) for r in resumed],
                         [_without_scan_time(r) for r in full])
        self.assertEqual(resumed[0], full[0])
        self.assertEqual(resumed_fail, full_fail)

    def test_rows_assembled_in_input_order_regardless_of_finished_order(self):
        # The finished repositories are written b first, then a, and the output must still
        # follow the input list.
        self._redirect_both()
        run_collection(self._cfg("o1"), ["o/b"], workers=1, finished_path=self.finished)
        run_collection(self._cfg("o2"), ["o/a"], workers=1, finished_path=self.finished)
        main_rows, _, _facts, _subs = run_collection(self._cfg("o3"), ["o/a", "o/b"], workers=1,
                                      finished_path=self.finished)
        self.assertEqual(_row_field(main_rows[0], "repo_name"), "o/a")
        self.assertEqual(_row_field(main_rows[1], "repo_name"), "o/b")

    def test_finished_entry_not_in_input_is_ignored(self):
        # A smaller input CSV must not put stale repositories into the output. A changed input
        # therefore needs no versioning of the list of finished repositories.
        append_finished(self.finished, "o/gone", "STALE-ROW", None)
        self._redirect_both()
        main_rows, _, _facts, _subs = run_collection(self._cfg("g"), ["o/a"], workers=1,
                                      finished_path=self.finished)
        self.assertEqual(len(main_rows), 1)
        self.assertEqual(_row_field(main_rows[0], "repo_name"), "o/a")

    def test_fail_rows_survive_a_resume(self):
        os.environ["GIT_CONFIG_COUNT"] = "2"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote_a}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/o/a.git"
        os.environ["GIT_CONFIG_KEY_1"] = "url.http://127.0.0.1:9/nonexistent/c.insteadOf"
        os.environ["GIT_CONFIG_VALUE_1"] = "https://github.com/o/c.git"
        first, first_fail, _facts, _subs = run_collection(self._cfg("f1"), ["o/a", "o/c"], workers=1,
                                           finished_path=self.finished)
        self.assertEqual(len(first_fail), 1)
        # Run again with everything already in the list of finished repositories: no work, and
        # the same result.
        again, again_fail, _facts, _subs = run_collection(self._cfg("f1"), ["o/a", "o/c"], workers=1,
                                           finished_path=self.finished)
        # The comparison goes through `_without_scan_time`, as the parallel comparison above
        # does. The resumed run retries the clone that failed, and `process_repo` takes a fresh
        # wall-clock time for it. The two rows therefore agree in everything but `scanned_at`,
        # and in `scanned_at` only when both runs fall in the same second.
        self.assertEqual([_without_scan_time(r) for r in again],
                         [_without_scan_time(r) for r in first])
        self.assertEqual(again_fail, first_fail)


class TestWhereTheCollectorWrites(unittest.TestCase):
    """The two lists, the tree that contains them, and the names that the next step expects
    there.

    `agentcontext/steps/collected_file_cleanup.py` defines every one of these names a second
    time, because it copies the repository directories out of the tree and has to distinguish
    the lists from strays. A name changed on one side only causes no error. The file is then
    copied into the validated tree and published, or it is logged as a stray on every run, in
    the message meant for a real accident.
    """

    def test_both_lists_sit_inside_the_collected_tree(self):
        for name in (COLLECTED_CSV_NAME, FAIL_CSV_NAME):
            with self.subTest(name):
                self.assertEqual(str(Path(name).parent), _mod.COLLECTED_DIR_NAME)

    def test_the_tree_files_are_the_basenames_of_the_two_lists(self):
        self.assertEqual(_mod.COLLECTED_TREE_FILES,
                         (Path(COLLECTED_CSV_NAME).name, Path(FAIL_CSV_NAME).name))

    def test_the_next_step_expects_the_same_names(self):
        validate = importlib.import_module("agentcontext.steps.collected_file_cleanup")
        self.assertEqual(validate.COLLECTED_TREE_FILES, _mod.COLLECTED_TREE_FILES)
        self.assertEqual(validate.COLLECTED_DIR, _mod.COLLECTED_DIR_NAME)
        self.assertEqual(validate.COLLECTED_CSV, COLLECTED_CSV_NAME)

    def test_the_scratch_clone_directory_stays_out_of_the_data_tree(self):
        """`--clone-dir` contains a clone only while its repository is processed, and no step
        reads it. The directory is not part of the sample and must not be beside
        it."""
        clone_default = _mod._build_arg_parser().get_default("clone_dir")
        self.assertNotIn("data", Path(clone_default).parts)


class TestMainCli(unittest.TestCase):
    """End-to-end CLI tests against local file:// remotes (serial, no network)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote_a = self.tmp / "remote_a"
        self.remote_b = self.tmp / "remote_b"
        _init_git_repo(self.remote_a)
        _init_git_repo(self.remote_b)
        self.out = self.tmp / "out"
        # The two lists are at the root of the tree that they list, and the fixture puts them
        # there too. They are distinct from the input repo-csv (tmp/input.csv).
        self.collected_csv = self.out / "repos.csv"
        self.fail_csv = self.out / "clone_failed.csv"
        # The list of finished repositories is in tmp too. At its default path, the tests would
        # write it into the real working directory.
        self.finished = self.tmp / "list of finished repositories.jsonl"
        # The two caches are in tmp as well. Both default to the stage directory, where the
        # tracked `file_history.csv` and `submodules.csv` are, and a run without these flags
        # would merge fixture rows into the tracked cache.
        self.cache = self.tmp / "file_history.csv"
        self.submodules = self.tmp / "submodules.csv"

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("GIT_CONFIG_"):
                os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_input(self, repos: list[str]) -> Path:
        p = self.tmp / "input.csv"
        p.write_text("repo_name\n" + "\n".join(repos) + "\n")
        return p

    def _redirect(self, mapping: dict) -> None:
        """mapping: {original_github_url: local_base_url}."""
        os.environ["GIT_CONFIG_COUNT"] = str(len(mapping))
        for i, (orig, base) in enumerate(mapping.items()):
            os.environ[f"GIT_CONFIG_KEY_{i}"] = f"url.{base}.insteadOf"
            os.environ[f"GIT_CONFIG_VALUE_{i}"] = orig

    def _args(self, input_csv: Path) -> list[str]:
        return [
            "--repo-csv", str(input_csv),
            "--collected-dir", str(self.out),
            "--collected-csv", str(self.collected_csv),
            "--fail-csv", str(self.fail_csv),
            "--finished", str(self.finished),
            "--file-history", str(self.cache),
            "--submodules", str(self.submodules),
            *_ai_commit_flags(self.tmp),
            "--clone-dir", str(self.tmp / "repos"),
            "-l", str(self.tmp / "logs"),
            "-w", "1",
            "--max-tries", "1",
            # The minimum is a setting for a run over a volume, and the CI runner's volume has
            # less free space than the default requires.
            "--min-free-gb", "0",
        ]

    def _read_rows(self, path: Path) -> list[str]:
        return path.read_text().splitlines()

    def test_creates_the_collected_csv(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        rc = main(self._args(inp))
        self.assertEqual(rc, 0)
        self.assertTrue((self.collected_csv).exists())

    def test_collected_csv_header_matches(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        main(self._args(inp))
        rows = self._read_rows(self.collected_csv)
        self.assertEqual(rows[0], paths.COLLECTED_CSV_HEADER)

    def test_collected_csv_records_flag(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        main(self._args(inp))
        rows = self._read_rows(self.collected_csv)
        self.assertEqual(_row_field(rows[1], "repo_name"), "o/a")
        self.assertEqual(_row_field(rows[1], "claude"), "TRUE")

    def test_creates_clone_failed_csv_with_header(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        main(self._args(inp))
        rows = self._read_rows(self.fail_csv)
        self.assertEqual(rows[0], FAIL_CSV_HEADER)

    def test_row_order_matches_input(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        _add_commit(self.remote_b, "GEMINI.md", "# g")
        self._redirect({
            "https://github.com/o/a.git": f"file://{self.remote_a}",
            "https://github.com/o/b.git": f"file://{self.remote_b}",
        })
        inp = self._write_input(["o/a", "o/b"])
        main(self._args(inp))
        rows = self._read_rows(self.collected_csv)
        self.assertEqual(_row_field(rows[1], "repo_name"), "o/a")
        self.assertEqual(_row_field(rows[2], "repo_name"), "o/b")

    def test_output_row_count_matches_input(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        _add_commit(self.remote_b, "GEMINI.md", "# g")
        self._redirect({
            "https://github.com/o/a.git": f"file://{self.remote_a}",
            "https://github.com/o/b.git": f"file://{self.remote_b}",
        })
        inp = self._write_input(["o/a", "o/b"])
        main(self._args(inp))
        rows = self._read_rows(self.collected_csv)
        self.assertEqual(len(rows), 3)  # the header and two repositories

    def test_cli_writes_finished(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        main(self._args(inp))
        self.assertEqual(set(read_finished(self.finished)), {"o/a"})

    def test_cli_rerun_is_identical_and_reclones_nothing(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        main(self._args(inp))
        first, lines = self.collected_csv.read_text(), self.finished.read_text()
        rc = main(self._args(inp))
        self.assertEqual(rc, 0)
        self.assertEqual(self.collected_csv.read_text(), first)
        self.assertEqual(self.finished.read_text(), lines, "a clone appends a line")

    def test_a_repository_that_left_the_input_leaves_with_its_files(self):
        """The step removes the tree of a repository that left the input. Otherwise, the
        collected file cleanup would log the tree on every run as a directory without a
        row."""
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        _add_commit(self.remote_b, "GEMINI.md", "# g")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/b.git": f"file://{self.remote_b}"})
        main(self._args(self._write_input(["o/a", "o/b"])))
        tree = self.collected_csv.parent / "o§b"
        self.assertTrue(tree.is_dir())
        self.assertEqual(main(self._args(self._write_input(["o/a"]))), 0)
        self.assertFalse(tree.exists())
        self.assertTrue((self.collected_csv.parent / "o§a").is_dir())

    def test_a_repository_that_returns_to_the_input_gets_its_files_back(self):
        """A repository with files under `.agents/` alone has no flag in its row. Once its files
        are removed with the repository that left the input, neither the row nor a
        per-repository commit record shows that they are gone. Back in the input with the same
        head, the repository must not be taken over with nothing on disk."""
        _add_commit(self.remote_a, ".agents/skills/x/SKILL.md",
                    "---\nname: x\ndescription: Does x\n---\nDo x.\n")
        _add_commit(self.remote_b, "CLAUDE.md", "# b")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/b.git": f"file://{self.remote_b}"})
        skill = self.out / "o§a" / ".agents" / "skills" / "x" / "SKILL.md"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 0)
            self.assertTrue(skill.is_file())
            self.assertEqual(main(self._args(self._write_input(["o/b"]))), 0)
            self.assertFalse(skill.exists())
            lines = self.finished.read_text()
            self.assertEqual(main(self._args(self._write_input(["o/b"]))), 0)
            self.assertEqual(self.finished.read_text(), lines, "a line on every run")
            self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 0)
        self.assertTrue(skill.is_file())

    def test_a_stop_while_the_files_of_a_repository_that_left_go_keeps_it_collectable(self):
        """`reuse.remove_left` marks the line of a repository that left the input to redo and
        then removes its files. If it removed the files first, a stop in between would leave the
        ok line with `collected_under` and no files. The repository, which has no flag in its
        row, would then be taken over with nothing on disk once it returned. A stop at the line
        keeps the files, and a stop at the removal keeps the line, which collects the
        repository again."""
        _add_commit(self.remote_a, ".agents/skills/x/SKILL.md",
                    "---\nname: x\ndescription: Does x\n---\nDo x.\n")
        _add_commit(self.remote_b, "CLAUDE.md", "# b")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/b.git": f"file://{self.remote_b}"})
        skill = self.out / "o§a" / ".agents" / "skills" / "x" / "SKILL.md"
        tree = self.out / "o§a"
        real_append, real_rmtree = _resume.append_finished, shutil.rmtree

        class Stop(Exception):
            pass

        def stop_at_the_line(path, repo, *args, **kwargs):
            if repo == "o/a":
                raise Stop
            return real_append(path, repo, *args, **kwargs)

        def stop_at_the_removal(path, *args, **kwargs):
            if Path(path).resolve() == tree.resolve():
                raise Stop
            return real_rmtree(path, *args, **kwargs)

        stops = {"the line": mock.patch.object(_resume, "append_finished", stop_at_the_line),
                 "the removal": mock.patch("shutil.rmtree", stop_at_the_removal)}
        for stopped_at, stop in stops.items():
            with self.subTest(stopped_at=stopped_at), contextlib.redirect_stderr(io.StringIO()):
                both = self._args(self._write_input(["o/a", "o/b"]))
                self.assertEqual(main([*both, "--fresh"]), 0)
                with stop, self.assertRaises(Stop):
                    main(self._args(self._write_input(["o/b"])))
                line = read_finished(self.finished)["o/a"]
                if stopped_at == "the line":
                    self.assertTrue(skill.is_file(), "the files stay until the line is written")
                else:
                    self.assertEqual((line[4], line[6]), (True, None))
                self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 0)
                self.assertTrue(skill.is_file())

    def test_a_tree_that_raises_on_removal_leaves_the_step_finished_and_the_line_to_redo(self):
        """Synology DSM can write an `@eaDir` directory into the tree of a repository that left
        the input list while `reuse.remove_left` removes the tree, and the removal then raises.
        The error must not end the step with exit 1 after the whole collection. `remove_left`
        removes what DSM wrote together with the tree. A tree that keeps another entry stays
        with a warning, and the line marked to redo keeps the repository collectable."""
        _add_commit(self.remote_a, ".agents/skills/x/SKILL.md",
                    "---\nname: x\ndescription: Does x\n---\nDo x.\n")
        _add_commit(self.remote_b, "CLAUDE.md", "# b")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/b.git": f"file://{self.remote_b}"})
        skill = self.out / "o§a" / ".agents" / "skills" / "x" / "SKILL.md"
        tree = self.out / "o§a"
        for written in ("@eaDir/SKILL.md@SynoEAStream", "notes.md"):
            with self.subTest(written=written):
                with contextlib.redirect_stderr(io.StringIO()):
                    both = self._args(self._write_input(["o/a", "o/b"]))
                    self.assertEqual(main([*both, "--fresh"]), 0)
                err = io.StringIO()
                with dsm_writes_during_removal("SKILL.md", entry=written), \
                        contextlib.redirect_stderr(err):
                    self.assertEqual(main(self._args(self._write_input(["o/b"]))), 0)
                line = read_finished(self.finished)["o/a"]
                self.assertEqual((line[4], line[6]), (True, None))
                resisted = written == "notes.md"
                self.assertEqual(tree.exists(), resisted)
                self.assertEqual("Removing the directory" in err.getvalue(), resisted,
                                 err.getvalue())
                with contextlib.redirect_stderr(io.StringIO()):
                    lines = self.finished.read_text()
                    self.assertEqual(main(self._args(self._write_input(["o/b"]))), 0)
                    self.assertFalse(tree.exists())
                    self.assertEqual(self.finished.read_text(), lines, "one line marked to redo")
                    self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 0)
                self.assertTrue(skill.is_file())

    def _collected_once(self) -> tuple:
        """Collect o/a and o/b, and return the list of finished repositories and the
        collected list that the run wrote."""
        _add_commit(self.remote_a, "CLAUDE.md", "# a")
        _add_commit(self.remote_b, "GEMINI.md", "# b")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/b.git": f"file://{self.remote_b}"})
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 0)
        return self.finished.read_bytes(), self.collected_csv.read_bytes()

    def test_a_git_that_prints_no_version_stops_the_step_before_it_decides_anything(self):
        """A fork can fail on the NAS, and `git --version` then returns "". Compared with the
        version that every line records, "" would make the step collect every repository again
        and write "" into each new line. The next Start would then collect everything again
        too."""
        finished, collected = self._collected_once()
        record = _resume.collection_path(self.finished)
        err = io.StringIO()
        with git_version_fails() as failures, contextlib.redirect_stderr(err):
            self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 1)
        self.assertEqual(len(failures), _clone.GIT_VERSION_TRIES)
        self.assertEqual(self.finished.read_bytes(), finished)
        self.assertEqual(self.collected_csv.read_bytes(), collected)
        self.assertFalse(record.exists(), "no collection was opened")
        self.assertIn("git --version printed no version", err.getvalue())
        self.assertNotIn("Collecting", err.getvalue())

    def test_a_git_that_fails_once_takes_the_earlier_collection_over(self):
        finished, _collected = self._collected_once()
        err = io.StringIO()
        with git_version_fails(times=1) as failures, contextlib.redirect_stderr(err):
            self.assertEqual(main(self._args(self._write_input(["o/a", "o/b"]))), 0)
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.finished.read_bytes(), finished, "no line was written again")
        self.assertIn("Took over 2 repositories", err.getvalue())

    def test_cli_fresh_ignores_existing_finished(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        append_finished(self.finished, "o/a", "STALE-SENTINEL", None)
        main(self._args(inp) + ["--fresh"])
        rows = self._read_rows(self.collected_csv)
        self.assertNotIn("STALE-SENTINEL", rows)
        self.assertEqual(_row_field(rows[1], "claude"), "TRUE")

    def test_missing_column_returns_nonzero(self):
        p = self.tmp / "bad.csv"
        p.write_text("wrong_col\nval\n")
        rc = main(["--repo-csv", str(p), "--collected-dir", str(self.out), "-w", "1",
                   "--min-free-gb", "0"])
        self.assertNotEqual(rc, 0)

    def test_missing_csv_returns_nonzero(self):
        rc = main(["--repo-csv", str(self.tmp / "nope.csv"),
                   "--collected-dir", str(self.out), "-w", "1"])
        self.assertNotEqual(rc, 0)

    def test_returns_zero_on_success(self):
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        self.assertEqual(main(self._args(inp)), 0)

    def test_a_resumed_collection_takes_over_what_it_collected_whatever_the_head(self):
        """A run that exits 4, with repositories left to retry, leaves its collection in
        progress. The next run resumes it and retries the failures. It takes over the repository
        that the collection already collected without querying its head, which moved in
        between, and a resumed run costs only what was left. The collection ends with
        the run that exits 0. A run after it over the same list continues the collection that
        ended and still does not query the head. Only a run after `--from repository_cloning`,
        which marks the collection that ended as replaced (`resume.forget_ended_collection`),
        starts a new collection, which queries the head and collects the repository again."""
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/c.git": "http://127.0.0.1:9/nonexistent/c",
                        "https://github.com/o/d.git": "http://127.0.0.1:9/nonexistent/d"})
        inp = self._write_input(["o/a", "o/c", "o/d"])
        args = self._args(inp) + ["--retry-rounds", "0"]
        record = _resume.collection_path(self.finished)

        def lines_of(repo):
            return [l for l in self.finished.read_text().splitlines()
                    if json.loads(l)["repo"] == repo]

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 4)
        self.assertTrue(record.is_file(), "the collection did not stay in progress")
        name = json.loads(record.read_text())["collection"]
        self.assertEqual({json.loads(l).get("collection") for l in lines_of("o/a")}, {name})
        _add_commit(self.remote_a, "AGENTS.md", "# moved")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 4)
        self.assertEqual(len(lines_of("o/a")), 1, "the resumed collection cloned o/a again")
        self.assertEqual(len(lines_of("o/c")), 2, "the resumed collection did not retry o/c")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args + ["--max-clone-failures", "0"]), 0)
        self.assertFalse(record.exists(), "the collection stayed in progress after exit 0")
        with contextlib.redirect_stderr(io.StringIO()):
            main(args + ["--max-clone-failures", "0"])
        self.assertEqual(len(lines_of("o/a")), 1, "the continued collection cloned o/a again")
        _resume.forget_ended_collection(self.finished)
        with contextlib.redirect_stderr(io.StringIO()):
            main(args + ["--max-clone-failures", "0"])
        self.assertEqual(len(lines_of("o/a")), 3,
                         "a new collection did not clone o/a with its moved head, after the line "
                         "written before the clone")
        self.assertEqual(_row_field(self._read_rows(self.collected_csv)[1], "agents.md"), "TRUE")

    def test_a_kept_collection_whose_commit_log_is_lost_does_not_stop_every_start(self):
        """In the collection in progress, a line kept the earlier collection when the time bound
        stopped its retry. Beside it are a per-repository commit record of a commit history that
        was read and no commit log, as after a full disk or a restore without the logs. The
        step exits 4 while a commit log is missing, whatever the ceiling allows. A line that the
        bound kept from every retry would therefore make every Start exit 4. The step collects
        the repository again whatever the bound. When the time bound stops that clone too, the
        step records `time_bound_exceeded` with the bound and without `redo`, as for every such
        line."""
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self._args(inp)), 0)
            collection = _resume.open_collection(self.finished, ["o/a"])
        line = read_finished(self.finished)["o/a"]
        append_finished(self.finished, "o/a", *line[:4], collected_under=line[6],
                        time_bound=600.0, collection=collection["collection"])
        commits_dir = self.tmp / "collect_commits"
        commits.stored_log_path(commits_dir, "o/a").unlink()
        # Two attempts that the time bound ends make `time_bound_exceeded`.
        args = [*self._args(inp), "--clone-timeout", "0.001", "--max-tries", "6",
                "--retry-pause", "0"]
        with contextlib.redirect_stderr(io.StringIO()):
            first = main(args)
        line = read_finished(self.finished)["o/a"]
        self.assertEqual((first, _resume.outcome_of(line[1]), line[4], line[9]),
                         (0, _clone.TIME_BOUND_EXCEEDED, False, 0.001))
        self.assertFalse(commits.shard_paths(commits_dir, "o/a")[1].exists(),
                         "the record without its commit log was kept")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(args), 0)

    def test_retries_that_the_time_bound_stops_do_not_make_every_start_exit_4(self):
        """Two repositories that lost their commit logs, as after a full disk, are collected
        again to read their commit histories, and the time bound stops both retries. Within the
        collection, the bound keeps every round and resumed run from cloning them again. A mark
        to redo on their `time_bound_exceeded` lines would count them toward a ceiling of one on
        every Start, and every Start would exit 4 with nothing retried."""
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        _add_commit(self.remote_b, "CLAUDE.md", "# b")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}",
                        "https://github.com/o/b.git": f"file://{self.remote_b}"})
        inp = self._write_input(["o/a", "o/b"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self._args(inp)), 0)
        commits_dir = self.tmp / "collect_commits"
        for repo in ("o/a", "o/b"):
            commits.stored_log_path(commits_dir, repo).unlink()
        # Two attempts that the time bound ends make `time_bound_exceeded`. Without a pause
        # between them, the test stays short.
        args = [*self._args(inp), "--clone-timeout", "0.001", "--max-tries", "6",
                "--retry-pause", "0", "--max-clone-failures", "1"]
        codes = []
        with mock.patch.object(collector, "CollectionConfig",
                               functools.partial(CollectionConfig, base_delay=0)):
            for _ in range(3):
                with contextlib.redirect_stderr(io.StringIO()):
                    codes.append(main(args))
        self.assertNotIn(4, codes[1:])
        lines = read_finished(self.finished)
        self.assertEqual({repo: (_resume.outcome_of(lines[repo][1]), lines[repo][4])
                          for repo in ("o/a", "o/b")},
                         {repo: (_clone.TIME_BOUND_EXCEEDED, False) for repo in ("o/a", "o/b")})

    def test_a_commit_log_that_the_reclassification_found_failed_is_collected_again(self):
        """The reclassification's output beside the commit CSV names the repository as `failed`
        with the SHA-256 of its per-repository commit record. The next collection therefore
        clones the repository although its head did not move."""
        _add_commit(self.remote_a, "CLAUDE.md", "# c")
        self._redirect({"https://github.com/o/a.git": f"file://{self.remote_a}"})
        inp = self._write_input(["o/a"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self._args(inp)), 0)
        record = commits.shard_paths(self.tmp / "collect_commits", "o/a")[1]
        (self.tmp / "output" / _resume.RECLASSIFICATION_REPORT_NAME).write_text(json.dumps(
            {"repositories": {"o/a": {"outcome": "failed", "error": "EOFError (truncated)",
                                      "left_out": True,
                                      "record_sha256": hashlib.sha256(
                                          record.read_bytes()).hexdigest()}}}))
        # Each clone starts its log afresh. Only a run that skips the repository keeps the
        # line below.
        log = self.tmp / "logs" / "o§a.log"
        log.write_text("not cloned since\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(self._args(inp)), 0)
        self.assertNotIn("not cloned since", log.read_text())


class TestTheParentsOwnGitIsStopped(unittest.TestCase):
    """`stop_pool` ends every worker and exits. The parent runs one git process of its own,
    the probe after failed clones in a row, in a session of its own. The terminal's signal
    never reaches the probe, and `os._exit` skips the `finally` that would otherwise
    reap it."""

    def test_stop_pool_kills_the_commands_this_process_started(self):
        from unittest import mock as _mock
        killed = []
        with _mock.patch.object(_mod.bounds, "stop_running",
                                lambda: killed.append(True) or 0), \
                _mock.patch.object(_mod.multiprocessing, "active_children", lambda: []), \
                _mock.patch.object(_mod.os, "_exit", lambda code: None):
            _mod.stop_pool(15, None)
        self.assertEqual(killed, [True],
                         "the parent's own git was left running")


class TestTheWorkflowFilesLeaveTheCollectionAlone(unittest.TestCase):
    """End to end, the collector copies the workflow files beside the collection, and the
    tree, the row and the flags are the same whether it copied them or not."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")])

    def _config(self, workflows):
        return CollectionConfig(
            collected_dir=self.tmp / "out", clone_dir=self.tmp / "repos_tmp",
            log_dir=self.tmp / "logs", max_tries=1, base_delay=0, measure_git=False,
            workflows_dir=workflows)

    def _redirect(self):
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"

    def _remote_with_a_workflow(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        _add_commit(self.remote, ".github/workflows/claude.yml",
                    "on: issue_comment\njobs:\n  claude:\n    steps:\n"
                    "      - uses: anthropics/claude-code-action@v1\n"
                    "        with:\n          prompt: Fix the issue\n")
        _add_commit(self.remote, ".github/workflows/release.yml",
                    "on: push\njobs:\n  release:\n    steps:\n      - run: make dist\n")
        self._redirect()

    def test_the_documented_one_is_configuration_and_both_are_kept_beside(self):
        """`claude.yml` matches a path pattern of the mechanism `settings`, and the collection
        copies and keeps it. `release.yml` reaches the collected tree too, because
        `copy_dir_gated` copies the whole `.github` once it contains a path that a heuristic
        names. The collected file cleanup then deletes `release.yml` there as an irrelevant file
        in `.github`. Only the tree beside the collection keeps it, so that a person can read
        it."""
        self._remote_with_a_workflow()
        kept = self.tmp / "workflows"
        _, _main_row, fail_row, _facts, _subs, _measures = process_repo(self._config(kept), "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(sorted(p.name for p in (kept / "owner§repo").iterdir()),
                         ["claude.yml", "release.yml"])
        self.assertIn("prompt", (kept / "owner§repo" / "claude.yml").read_text(encoding="utf-8"))
        collected = self.tmp / "out" / "owner§repo" / ".github" / "workflows"
        self.assertTrue((collected / "claude.yml").is_file(), "the documented workflow is configuration")

    def test_recollection_replaces_workflow_files(self):
        self._remote_with_a_workflow()
        kept = self.tmp / "workflows"
        cfg = self._config(kept)
        self.assertIsNone(process_repo(cfg, "owner/repo")[2])
        subprocess.run(["git", "-C", str(self.remote), "rm", ".github/workflows/release.yml"],
                       check=True, capture_output=True)
        _add_commit(self.remote, ".github/workflows/new.yaml", "on: push\njobs: {}\n")
        self.assertIsNone(process_repo(cfg, "owner/repo")[2])
        self.assertEqual({p.name for p in (kept / "owner§repo").iterdir()},
                         {"claude.yml", "new.yaml"})

    def test_failed_recollection_removes_workflow_files(self):
        self._remote_with_a_workflow()
        kept = self.tmp / "workflows"
        cfg = self._config(kept)
        self.assertIsNone(process_repo(cfg, "owner/repo")[2])
        shutil.rmtree(self.remote)
        self.assertIsNotNone(process_repo(cfg, "owner/repo")[2])
        self.assertFalse((kept / "owner§repo").exists())

    def test_failed_repair_retains_workflow_files(self):
        self._remote_with_a_workflow()
        kept = self.tmp / "workflows"
        cfg = self._config(kept)
        self.assertIsNone(process_repo(cfg, "owner/repo")[2])
        shutil.rmtree(self.remote)
        with mock.patch.object(_mod.clone, "clone_with_retry", return_value="clone_failed"):
            result = process_repo(cfg, "owner/repo", preserve_on=frozenset({"clone_failed"}))
        self.assertTrue(result[5]["preserved_previous"])
        self.assertEqual({p.name for p in (kept / "owner§repo").iterdir()},
                         {"claude.yml", "release.yml"})

    def test_none_copies_none_and_changes_no_row(self):
        """Without a directory for the workflow files, the collector writes no workflow file
        anywhere but in the collected tree, which contains the same files as with the copy. The
        second run has a directory of its own and runs in it. A relative default would
        therefore be written there too, where the check finds it."""
        self._remote_with_a_workflow()
        _, with_copy, _, _, _, _measures = process_repo(self._config(self.tmp / "workflows"), "owner/repo")
        fresh = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fresh, True)
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(fresh)
        stage_default = STAGE_DIR / "data" / "workflows" / "owner§repo"
        existed = stage_default.exists()
        config = dataclasses.replace(self._config(None), collected_dir=fresh / "out",
                                     clone_dir=fresh / "repos_tmp", log_dir=fresh / "logs")
        _, without, _, _, _, _measures = process_repo(config, "owner/repo")
        self.assertEqual(with_copy.split(",")[3:], without.split(",")[3:],
                         "the copy is beside the collection, so the row cannot change")

        def files(root):
            return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

        self.assertEqual(files(fresh / "out"), files(self.tmp / "out"))
        self.assertEqual([p for p in files(fresh) if not p.startswith(("out/", "logs/"))], [])
        self.assertEqual(stage_default.exists(), existed)

    def test_a_directory_that_cannot_be_written_is_a_warning_not_a_failure(self):
        """The purpose of the run is the collection, and the workflow files are a supplementary
        record of it."""
        self._remote_with_a_workflow()
        blocked = self.tmp / "blocked"
        blocked.write_text("not a directory\n", encoding="utf-8")
        _, main_row, fail_row, _facts, _subs, _measures = process_repo(self._config(blocked), "owner/repo")
        self.assertIsNone(fail_row, "the repository was still collected")
        self.assertIn("TRUE", main_row)



class TestTheFileListAndWhatARepositoryCost(unittest.TestCase):
    """End to end, the collector writes the file list of every repository beside the
    collection, including the repositories that contributed nothing, and the list of finished
    repositories keeps what each repository cost."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.remote = self.tmp / "remote"
        _init_git_repo(self.remote)
        self.lists = self.tmp / "file_lists"
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")])

    def _config(self, **overrides):
        return CollectionConfig(**{
            "collected_dir": self.tmp / "out", "clone_dir": self.tmp / "repos_tmp",
            "log_dir": self.tmp / "logs", "max_tries": 1, "base_delay": 0,
            "file_lists_dir": self.lists, **overrides})

    def _redirect(self, remote=None):
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{remote or self.remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"

    def _list(self) -> set:
        return set(_clone.read_file_list(self.lists / f"owner§repo{_clone.FILE_LIST_SUFFIX}"))

    def test_the_list_contains_every_file_link_and_gitlink_of_the_collected_commit(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        _add_commit(self.remote, "src/app.py", "print(1)\n")
        (self.remote / "link").symlink_to("src/app.py")
        subprocess.run(["git", "-C", str(self.remote), "add", "link"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.remote), "update-index", "--add", "--cacheinfo",
                        "160000", "1" * 40, "lib/dep"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.remote), "commit", "-q", "-m", "link and dep"],
                       check=True, capture_output=True)
        self._redirect()
        _, _, fail_row, _, _, measures = process_repo(self._config(), "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(self._list(), {("100644", "README.md"), ("100644", "CLAUDE.md"),
                                        ("100644", "src/app.py"), ("120000", "link"),
                                        ("160000", "lib/dep")})
        self.assertEqual(measures["entries"], 5)

    def test_a_repository_that_contributed_nothing_still_has_its_list(self):
        """The file list makes a path heuristic countable over the repositories without
        configuration, for which the collected tree records nothing."""
        _add_commit(self.remote, "README.md", "# A project\n")
        self._redirect()
        _, main_row, fail_row, _, _, _ = process_repo(self._config(), "owner/repo")
        self.assertIsNone(fail_row)
        self.assertFalse((self.tmp / "out" / "owner§repo").exists())
        self.assertEqual(self._list(), {("100644", "README.md")})

    def test_a_failed_clone_leaves_no_list_of_an_earlier_collection(self):
        stale = self.lists / f"owner§repo{_clone.FILE_LIST_SUFFIX}"
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")
        self._redirect(self.tmp / "gone")
        _, _, fail_row, _, _, measures = process_repo(self._config(), "owner/repo")
        self.assertIsNotNone(fail_row)
        self.assertFalse(stale.exists())
        self.assertIn("clone_seconds", measures)
        self.assertNotIn("clone_bytes", measures)

    def test_none_writes_none(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        process_repo(self._config(file_lists_dir=None), "owner/repo")
        self.assertFalse(self.lists.exists())

    def test_an_empty_repository_has_an_empty_list_rather_than_a_failed_one(self):
        # An empty repository has no HEAD, and `ls-tree HEAD` fails there. Without a list for
        # an empty repository, a missing list could mean either an empty repository or a
        # repository with files that were not listed.
        empty = self.tmp / "empty-remote"
        subprocess.run(["git", "init", "-q", str(empty)], check=True, capture_output=True)
        self._redirect(empty)
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            _, main_row, fail_row, _, submodules, measures = process_repo(
                self._config(measure_git=False), "owner/repo")
        self.assertIsNone(fail_row)
        self.assertEqual(_row_field(main_row, "head_sha"), "")
        self.assertEqual(submodules, [])
        self.assertEqual(measures["entries"], 0)
        self.assertEqual(self._list(), set())
        self.assertNotIn("ls-tree", errors.getvalue())
        self.assertNotIn("FILE_LIST_ERROR", (self.tmp / "logs/owner§repo.log").read_text())

    def test_listing_failure_retains_the_log_messages_and_copied_configuration(self):
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        cfg = self._config(measure_git=False)
        inventory = self.lists / "owner§repo.ls-tree.xz"
        inventory.parent.mkdir()
        inventory.write_bytes(b"stale")
        error = subprocess.CalledProcessError(
            128, ["git", "ls-tree"],
            stderr=b"fatal: cannot read object\nhttps://secret@github.com/owner/repo.git\n")
        messages = io.StringIO()
        with mock.patch.object(_clone, "tree_listing", side_effect=error), \
                mock.patch.object(_clone, "submodule_rows") as submodules, \
                contextlib.redirect_stderr(messages):
            _, main_row, fail_row, _, _, _ = process_repo(cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertIn("TRUE", main_row)
        self.assertTrue((cfg.collected_dir / "owner§repo/CLAUDE.md").is_file())
        self.assertFalse(inventory.exists())
        submodules.assert_not_called()
        retained = (cfg.log_dir / "owner§repo.log").read_text()
        for text in (messages.getvalue(), retained):
            self.assertIn("ls-tree -r -z HEAD. Continuing without", text)
            self.assertIn("Git exited 128 (fatal: cannot read object", text)
            self.assertIn("fatal: cannot read object", text)
            self.assertIn(str(inventory), text)
            self.assertNotIn("secret", text)

    def test_a_failure_to_write_the_inventory_identifies_output_path_and_io_error(self):
        self._redirect()
        inventory = self.lists / "owner§repo.ls-tree.xz"
        inventory.parent.mkdir()
        inventory.write_bytes(b"stale")
        messages = io.StringIO()
        with mock.patch.object(_clone, "write_file_list", side_effect=OSError("disk full")), \
                contextlib.redirect_stderr(messages):
            _, _, fail_row, _, _, _ = process_repo(self._config(), "owner/repo")
        self.assertIsNone(fail_row)
        self.assertFalse(inventory.exists())
        self.assertIn(str(inventory), messages.getvalue())
        self.assertIn("disk full", messages.getvalue())

    def test_the_list_of_finished_repositories_keeps_what_each_one_cost(self):
        """The line of a repository with a commit history that was read records every part of
        the work that it went through, with the seconds and bytes that an analysis of a run
        needs."""
        _add_commit(self.remote, "CLAUDE.md", "# Claude\n" + "x" * 90)
        self._redirect()
        finished = self.tmp / "finished.jsonl"
        run_collection(self._config(commits_dir=self.tmp / "commits"), ["owner/repo"],
                       workers=1, finished_path=finished)
        line = json.loads(finished.read_text(encoding="utf-8").splitlines()[-1])
        measures = line["measures"]
        self.assertEqual(set(measures), {"seconds", "clone_seconds", "clone_bytes", "entries",
                                         "history_seconds", "history_bytes", "copied",
                                         "history_read_seconds"})
        self.assertGreater(measures["clone_bytes"], 0)
        self.assertGreaterEqual(measures["history_bytes"], measures["clone_bytes"])
        # README.md, which the fixture starts with, and CLAUDE.md, the one file copied.
        self.assertEqual((measures["entries"], measures["copied"]), (2, 1))
        self.assertGreaterEqual(measures["seconds"], measures["clone_seconds"])
        # A resumed run also reads a line with the key, as it reads a line without it.
        self.assertIn("owner/repo", _resume.read_finished(finished))



class TestFailedHistoryRepairKeepsCollectedData(_SixRemotes):

    def test_a_failed_retry_clone_preserves_the_previous_snapshot(self):
        import json
        with mock.patch.dict(os.environ):
            os.environ.pop('GITHUB_TOKEN', None)
            for workers in (1, 2):
                with self.subTest(workers=workers):
                    self._redirect_all()
                    root = self.tmp / f'run-{workers}'
                    cfg = CollectionConfig(collected_dir=root / 'out', clone_dir=root / 'scratch',
                        log_dir=root / 'logs', commits_dir=root / 'commits',
                        file_lists_dir=root / 'files', max_tries=1, base_delay=0)
                    finished = root / 'finished.jsonl'
                    before = run_collection(cfg, ['o/a'], workers=workers, finished_path=finished)
                    self.assertEqual(before[1], [])
                    record_path = cfg.commits_dir / 'o§a.json'
                    record = json.loads(record_path.read_text())
                    record['history_failed'] = ['CLAUDE.md']
                    record_path.write_text(json.dumps(record))
                    saved = {p.relative_to(root): p.read_bytes()
                             for folder in (cfg.collected_dir, cfg.commits_dir, cfg.file_lists_dir)
                             for p in folder.rglob('*') if p.is_file()}
                    os.environ['GIT_CONFIG_KEY_0'] = 'url.http://127.0.0.1:9/unavailable.insteadOf'
                    after = run_collection(cfg, ['o/a'], workers=workers, finished_path=finished)
                    self.assertEqual(after, before)
                    for rel, content in saved.items():
                        self.assertTrue((root / rel).is_file(), rel)
                        self.assertEqual((root / rel).read_bytes(), content, rel)
                    done = read_finished(finished)['o/a']
                    self.assertIsNone(done[1])
                    self.assertTrue(done[4], 'The incomplete history must remain eligible for retry')
                    self._redirect_all()
                    repaired = run_collection(cfg, ['o/a'], workers=workers, finished_path=finished)
                    self.assertEqual(repaired[1], [])
                    self.assertFalse(read_finished(finished)['o/a'][4])
                    self.assertEqual(json.loads(record_path.read_text())['history_failed'], [])

    def test_a_retry_that_was_stopped_after_its_clone_keeps_no_tree_on_a_failed_retry(self):
        """The retry after a head moved removes the earlier commit shards and tree before it
        writes its own. A retry stopped after its copy would leave the new tree. A failed clone
        in the next run would then keep that tree as the earlier snapshot and publish it under
        the earlier row."""
        with mock.patch.dict(os.environ):
            os.environ.pop('GITHUB_TOKEN', None)
            self._redirect_all()
            root = self.tmp / 'stopped'
            cfg = CollectionConfig(collected_dir=root / 'out', clone_dir=root / 'scratch',
                log_dir=root / 'logs', commits_dir=root / 'commits',
                file_lists_dir=root / 'files', max_tries=1, base_delay=0)
            finished = root / 'finished.jsonl'
            run_collection(cfg, ['o/a'], workers=1, finished_path=finished)
            under = read_finished(finished)['o/a'][6]
            _add_commit(self.remotes['o/a'], 'AGENTS.md', '# Agents, from the moved head')
            stopped = mock.patch('agentcontext.collector.measure_collected_files',
                                 side_effect=KeyboardInterrupt('a stop after the copy'))
            with stopped, self.assertRaises(KeyboardInterrupt):
                run_collection(cfg, ['o/a'], workers=1, finished_path=finished)
            line = read_finished(finished)['o/a']
            self.assertTrue(line[7], 'the line before the retry records that a retry began')
            self.assertEqual(line[6], under, 'and keeps what the repository was collected under')
            self.assertTrue((cfg.collected_dir / 'o§a' / 'AGENTS.md').is_file())
            os.environ['GIT_CONFIG_KEY_0'] = 'url.http://127.0.0.1:9/unavailable.insteadOf'
            main_rows, fail_rows, facts, _ = run_collection(cfg, ['o/a'], workers=1,
                                                            finished_path=finished)
            self.assertEqual(fail_rows, ['o/a,https://github.com/o/a,clone_failed'])
            self.assertEqual(facts, [])
            self.assertFalse((cfg.collected_dir / 'o§a').exists())


if __name__ == "__main__":
    unittest.main()
