"""
Tests for agentcontext.git.history: the history fetch and the per-path queries.

Strategy:
  - The client is the one part of the pipeline that runs git, and every test therefore
    builds a real repository with `git init` and reads it. Nothing clones from a network.
  - What matters is the difference between a result and a failure. Every query method
    returns a falsy default when its command raises, and a falsy default is what "this
    file has no history" looks like. The tests assert on the counters that
    distinguish the two as much as on the values.
  - `_clone_outcome` decides whether a repository is pruned from the dataset. Its inputs
    are a server's stderr and two counters, and it is therefore tested on those directly
    rather than through a clone, which would make the result depend on what a server
    returned that day.
  - `git log` is the whole subject: the module never reads the configuration artifact's own
    bytes, and one test asserts that.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_git_history -v
"""

import contextlib
import dataclasses
import importlib
import io
import os
import shutil
import subprocess
import sys
import socket
import threading
import time
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from agentcontext import STAGE_DIR
from tests._fixtures import _add_noise, ext_redirect, time_bound_at_stall, upload_pack
_meta = importlib.import_module("agentcontext.git.history")
_clone = importlib.import_module("agentcontext.git.clone")
_ambient_env = importlib.import_module("tests._ambient_env")


def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q", "-b", "main"], cwd=path)
    _run(["git", "config", "user.email", "t@example.invalid"], cwd=path)
    _run(["git", "config", "user.name", "Test"], cwd=path)


def _commit(repo: Path, relative: str, content: str, date: str) -> str:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _run(["git", "add", relative], cwd=repo)
    _run(["git", "commit", "-q", "-m", f"touch {relative}"], cwd=repo,
         env={**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return _run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


class _RepoCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        _init_repo(self.repo)
        self.extractor = _meta.GitFileMetadata()


# ---------------------------------------------------------------------------
# The path is a name, not a pattern
# ---------------------------------------------------------------------------
class TestRequestedSubmodules(_RepoCase):
    def shallow_submodule(self):
        remote = Path(self.tmp.name) / "submodule"
        _init_repo(remote)
        first = _commit(remote, "CLAUDE.md", "first\n", "2024-01-01T00:00:00Z")
        second = _commit(remote, "CLAUDE.md", "second\n", "2024-02-01T00:00:00Z")
        _commit(remote, "CLAUDE.md", "third\n", "2024-03-01T00:00:00Z")
        _run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
              remote.as_uri(), "vendor/inner"], cwd=self.repo)
        _run(["git", "checkout", "-q", second], cwd=self.repo / "vendor/inner")
        _run(["git", "add", "vendor/inner"], cwd=self.repo)
        _run(["git", "commit", "-qm", "Pin submodule"], cwd=self.repo)
        clone = Path(self.tmp.name) / "clone"
        _run(["git", "clone", "-q", str(self.repo), str(clone)])
        with unittest.mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            self.extractor.update_submodules(str(clone), ["vendor/inner/CLAUDE.md"])
        self.assertTrue(self.extractor.is_shallow_repo(str(clone / "vendor/inner")))
        return clone, first, second

    def test_a_requested_submodule_needs_full_history_at_its_pinned_commit(self):
        clone, first, second = self.shallow_submodule()
        with unittest.mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            facts = _meta.measure_artifact(self.extractor, str(clone), "vendor/inner/CLAUDE.md")
        self.assertEqual(facts.first_commit_sha, first)
        self.assertEqual(facts.last_commit_sha, second)
        self.assertEqual(facts.commits, "3")

    def test_failed_submodule_history_fetch_is_unavailable_rather_than_a_zero_or_short_count(self):
        clone, _, _ = self.shallow_submodule()
        real = self.extractor._run_git_command

        def rejected_fetch(cmd, **kwargs):
            if "--unshallow" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: fetch rejected")
            return real(cmd, **kwargs)

        with unittest.mock.patch.object(self.extractor, "_run_git_command", rejected_fetch), \
                contextlib.redirect_stderr(io.StringIO()):
            facts = _meta.measure_artifact(self.extractor, str(clone), "vendor/inner/CLAUDE.md")
        self.assertEqual(facts, _meta.caches.FileHistory())

    def test_only_the_submodule_containing_a_requested_path_is_initialized(self):
        for name in ("wanted", "other"):
            remote = Path(self.tmp.name) / name
            _init_repo(remote)
            _commit(remote, "README.md", f"{name}\n", "2021-01-01T09:00:00+00:00")
            _run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
                  str(remote), f"vendor/{name}"], cwd=self.repo)
        _run(["git", "commit", "-qm", "Add submodules"], cwd=self.repo)
        clone = Path(self.tmp.name) / "clone"
        _run(["git", "clone", "-q", str(self.repo), str(clone)])
        with unittest.mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file"}):
            self.extractor.update_submodules(str(clone), ["vendor/wanted/README.md"])
        self.assertEqual((clone / "vendor/wanted/README.md").read_text(), "wanted\n")
        self.assertEqual(list((clone / "vendor/other").iterdir()), [])

    def test_a_submodule_named_by_an_ssh_url_of_github_is_fetched_over_https(self):
        """A submodule that `.gitmodules` names by an SSH URL of GitHub is fetched over https,
        which needs no SSH key. A rewrite in the configuration of the clone that contains it
        does not reach the `git clone` of the submodule, which went over SSH. Here the https
        address of GitHub is a directory of local repositories, and the SSH command records
        that it ran."""
        remotes = Path(self.tmp.name) / "remotes"
        work = Path(self.tmp.name) / "work"
        _init_repo(work)
        _commit(work, "CLAUDE.md", "guidance\n", "2024-01-01T00:00:00Z")
        _run(["git", "clone", "-q", "--bare", str(work), str(remotes / "owner" / "sub.git")])
        _run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
              (remotes / "owner" / "sub.git").as_uri(), "vendor/inner"], cwd=self.repo)
        _run(["git", "config", "-f", ".gitmodules", "submodule.vendor/inner.url",
              "ssh://git@github.com/owner/sub.git"], cwd=self.repo)
        _run(["git", "add", ".gitmodules"], cwd=self.repo)
        _run(["git", "commit", "-qm", "Add submodule"], cwd=self.repo)
        clone = Path(self.tmp.name) / "clone"
        _run(["git", "clone", "-q", str(self.repo), str(clone)])
        ran = Path(self.tmp.name) / "ssh-ran"
        ssh = Path(self.tmp.name) / "ssh.sh"
        ssh.write_text(f"#!/bin/sh\ntouch '{ran}'\nexit 1\n", encoding="utf-8")
        ssh.chmod(0o755)
        with unittest.mock.patch.dict(os.environ, {"GIT_ALLOW_PROTOCOL": "file:ssh",
                                                   "GIT_SSH_COMMAND": str(ssh)}), \
                unittest.mock.patch.object(_meta, "GITHUB_HTTPS", remotes.as_uri() + "/"), \
                contextlib.redirect_stderr(io.StringIO()):
            self.extractor.update_submodules(str(clone), ["vendor/inner/CLAUDE.md"])
        self.assertFalse(ran.exists(), "the submodule was fetched over SSH")
        self.assertEqual((clone / "vendor/inner/CLAUDE.md").read_text(), "guidance\n")
        self.assertEqual(self.extractor.transient_failures, 0)

    def test_an_unavailable_requested_submodule_is_a_measurement_failure(self):
        remote = Path(self.tmp.name) / "submodule"
        _init_repo(remote)
        _commit(remote, "README.md", "guidance\n", "2021-01-01T09:00:00+00:00")
        _run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
              str(remote), "vendor/inner"], cwd=self.repo)
        _run(["git", "commit", "-qm", "Add submodule"], cwd=self.repo)
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        shutil.rmtree(remote)
        env = {"GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_COUNT": "1",
               "GIT_CONFIG_KEY_0": f"url.{self.repo.as_uri()}.insteadOf",
               "GIT_CONFIG_VALUE_0": "https://github.com/owner/repo.git"}
        with unittest.mock.patch.dict(os.environ, env), contextlib.redirect_stderr(io.StringIO()):
            measured, error = _meta.measure_repo(
                "owner/repo", head, "", "main", ["vendor/inner/README.md"], None,
                sleep=lambda _: None, scratch_dir=Path(self.tmp.name) / "scratch")
        self.assertEqual(measured, {})
        self.assertEqual(error, "git_unavailable")


class TestTheRootCommit(_RepoCase):

    def test_of_two_root_commits_it_is_the_older(self):
        """`git rev-list --max-parents=0` lists the roots newest first, and the repository's
        history starts at the oldest."""
        oldest = _commit(self.repo, "a.md", "a\n", "2020-01-01T00:00:00Z")
        _run(["git", "checkout", "-q", "--orphan", "other"], cwd=self.repo)
        _run(["git", "rm", "-q", "-rf", "."], cwd=self.repo)
        _commit(self.repo, "b.md", "b\n", "2024-01-01T00:00:00Z")
        _run(["git", "merge", "-q", "--allow-unrelated-histories", "-m", "join", "main"],
             cwd=self.repo, env={**os.environ, "GIT_AUTHOR_DATE": "2025-01-01T00:00:00Z",
                                 "GIT_COMMITTER_DATE": "2025-01-01T00:00:00Z"})
        self.assertEqual(self.extractor.get_root_commit_sha(str(self.repo)), oldest)


class TestThePathIsLiteral(_RepoCase):
    """`--` stops option parsing but does not make the argument literal, and git matches a
    pathspec with wildmatch. A repository can commit GitHub's
    `.github/instructions/*.instructions.md` placeholder verbatim, and a row that selected
    whatever matched would measure every column from another file."""

    def test_a_name_containing_a_star_measures_only_itself(self):
        star = _commit(self.repo, ".github/instructions/*.instructions.md", "placeholder\n",
                       "2021-01-01T09:00:00+00:00")
        for i in range(3):
            _commit(self.repo, f".github/instructions/real{i}.instructions.md", f"{i}\n",
                    f"2022-0{i + 1}-02T09:00:00+00:00")
        facts = _meta.measure_artifact(self.extractor, str(self.repo),
                                       ".github/instructions/*.instructions.md")
        self.assertEqual(facts.commits, "1", "the name was matched as a pattern")
        self.assertEqual(facts.first_commit_sha, star)
        self.assertEqual(facts.last_commit_sha, star)

    def test_an_ordinary_name_is_unaffected(self):
        first = _commit(self.repo, "CLAUDE.md", "a\n", "2021-01-01T09:00:00+00:00")
        last = _commit(self.repo, "CLAUDE.md", "b\n", "2022-01-01T09:00:00+00:00")
        facts = _meta.measure_artifact(self.extractor, str(self.repo), "CLAUDE.md")
        self.assertEqual((facts.commits, facts.first_commit_sha, facts.last_commit_sha),
                         ("2", first, last))


# ---------------------------------------------------------------------------
# The history of one path in a checkout
# ---------------------------------------------------------------------------
class TestMeasureArtifact(_RepoCase):
    def test_a_tracked_file_gets_all_four_values(self):
        first = _commit(self.repo, "CLAUDE.md", "# guidance\n" + "x" * 80,
                        "2021-01-01T09:00:00+00:00")
        last = _commit(self.repo, "CLAUDE.md", "# guidance\n" + "y" * 90,
                       "2022-02-02T09:00:00+00:00")
        facts = _meta.measure_artifact(self.extractor, str(self.repo), "CLAUDE.md")
        self.assertEqual(facts.created_at, "2021-01-01T09:00:00Z")
        self.assertEqual(facts.commits, "2")
        self.assertEqual(facts.first_commit_sha, first)
        self.assertEqual(facts.last_commit_sha, last)

    def test_a_path_git_has_never_seen_reads_as_no_history(self):
        _commit(self.repo, "README.md", "# hello\n" + "z" * 80,
                "2021-01-01T09:00:00+00:00")
        facts = _meta.measure_artifact(self.extractor, str(self.repo), "absent.md")
        self.assertEqual(facts.created_at, "")
        self.assertEqual(facts.commits, "0")

    def test_directory_symlink_children_use_committed_target_history(self):
        first = _commit(self.repo, "shared/skills/demo/SKILL.md", "# Demo\n",
                        "2021-01-01T00:00:00Z")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude/skills").symlink_to("../shared/skills", target_is_directory=True)
        _run(["git", "add", ".claude/skills"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "link skills"], cwd=self.repo)
        path_history = _meta.read_artifact_history(self.extractor, str(self.repo),
                                     ".claude/skills/demo/SKILL.md")
        self.assertEqual(path_history.first()["hash"], first)
        self.assertEqual(_meta.history_from(path_history).commits, "1")
        from agentcontext import commits
        entry = commits.path_commits_entry(".claude/skills/demo/SKILL.md", path_history)
        self.assertEqual(entry["artifact"], ".claude/skills/demo/SKILL.md")
        self.assertEqual(entry["source_path"], "shared/skills/demo/SKILL.md")
        self.assertEqual(entry["commits"], [first])

    def test_file_symlink_keeps_its_own_history(self):
        _commit(self.repo, "guidance.md", "# Guidance\n", "2021-01-01T00:00:00Z")
        (self.repo / "AGENTS.md").symlink_to("guidance.md")
        _run(["git", "add", "AGENTS.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "link guidance"], cwd=self.repo)
        linked = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        path_history = _meta.read_artifact_history(self.extractor, str(self.repo), "AGENTS.md")
        self.assertEqual(path_history.first()["hash"], linked)

    def test_directory_symlink_outside_repository_is_not_measured(self):
        _commit(self.repo, "README.md", "# Repository\n", "2021-01-01T00:00:00Z")
        outside = Path(self.tmp.name) / "outside"
        _init_repo(outside)
        _commit(outside, "AGENTS.md", "# Outside\n", "2020-01-01T00:00:00Z")
        (self.repo / "linked").symlink_to(outside, target_is_directory=True)
        path_history = _meta.read_artifact_history(self.extractor, str(self.repo), "linked/AGENTS.md")
        self.assertIsNone(path_history.first())

    def test_the_file_itself_is_never_read(self):
        """`is_empty` and `language` are not read here, from a clone made later than the tree
        that every other column of the row describes. The collected file cleanup settles the
        first by deleting every empty file before detection runs, and the detection step reads
        the second from `data/validated/`. A file with no content is measured like any
        other."""
        _commit(self.repo, ".claude/settings.json", "   \n", "2021-01-01T09:00:00+00:00")
        facts = _meta.measure_artifact(self.extractor, str(self.repo),
                                       ".claude/settings.json")
        self.assertEqual(facts.commits, "1")
        self.assertEqual([f.name for f in dataclasses.fields(facts)],
                         ["created_at", "commits", "first_commit_sha", "last_commit_sha"])

    def test_a_path_inside_a_submodule_is_read_from_the_submodule(self):
        inner = Path(self.tmp.name) / "inner"
        _init_repo(inner)
        inner_sha = _commit(inner, "rules/one.mdc", "# rule\n" + "q" * 80,
                            "2020-05-05T09:00:00+00:00")
        _commit(self.repo, "README.md", "# hello\n", "2021-01-01T09:00:00+00:00")
        _run(["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q",
              f"file://{inner}", "vendor"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "add submodule"], cwd=self.repo,
             env={**os.environ, "GIT_AUTHOR_DATE": "2021-06-06T09:00:00+00:00",
                  "GIT_COMMITTER_DATE": "2021-06-06T09:00:00+00:00"})
        facts = _meta.measure_artifact(self.extractor, str(self.repo),
                                       "vendor/rules/one.mdc")
        self.assertEqual(facts.created_at, "2020-05-05T09:00:00Z")
        self.assertEqual(facts.first_commit_sha, inner_sha)


# ---------------------------------------------------------------------------
# Distinguishing a failure from a result
# ---------------------------------------------------------------------------
class TestPathCommits(_RepoCase):
    """The history of one path, from which every measured column of its row is derived."""

    def test_a_query_that_exits_non_zero_is_a_failure_not_an_empty_history(self):
        """A non-zero exit is a failure, not "the path has no history". On a blobless clone, a
        failed network fetch of the blob that `--follow` needs exits 128, and a row from it,
        with two columns filled and two blank, would be cached and published."""
        _commit(self.repo, "CLAUDE.md", "one\n", "2024-01-01T00:00:00Z")
        calls = []

        def run(cmd, cwd):
            calls.append(cmd)
            if "--follow" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "",
                                                   "fatal: could not fetch abc from promisor remote")
            return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)

        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(_meta.path_commits(run, str(self.repo), "CLAUDE.md"))
        self.assertIn("exit 128", err.getvalue())
        self.assertIn("promisor", err.getvalue())
        history = _meta.history_from(None)
        self.assertEqual((history.created_at, history.commits, history.first_commit_sha,
                          history.last_commit_sha), ("", "", "", ""))

    def test_the_creation_commit_is_the_first_followed_record(self):
        first = _commit(self.repo, "CLAUDE.md", "# one\n" + "x" * 80,
                        "2021-01-01T09:00:00+00:00")
        last = _commit(self.repo, "CLAUDE.md", "# two\n" + "y" * 80,
                       "2022-02-02T09:00:00+00:00")
        _commit(self.repo, "other.md", "# other\n", "2023-03-03T09:00:00+00:00")
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertEqual(commits.first()["hash"], first)
        self.assertEqual(_meta.history_from(commits).created_at, "2021-01-01T09:00:00Z")
        self.assertEqual(commits.last()["hash"], last)
        self.assertEqual(len(commits.exact), 2)

    def test_a_creation_commit_in_a_zone_past_24_hours_is_its_epoch_in_utc(self):
        """git prints a commit's offset of +2500 as `+25:00` in `%cI`, which `datetime` refuses
        as a time zone. `created_at` is the instant of the commit's epoch seconds, as `%ct`
        prints them, rather than the unconverted value, which the file history step would
        refuse to write."""
        from datetime import datetime, timezone
        from tests._fixtures import _add_commit_in_zone
        _commit(self.repo, "README.md", "x\n", "2021-01-01T09:00:00+00:00")
        _add_commit_in_zone(self.repo, "CLAUDE.md", "# one\n", 1700000000, "+2500")
        epoch = _run(["git", "show", "-s", "--format=%ct", "HEAD"], cwd=self.repo).stdout
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertTrue(commits.first()["committer_date"].endswith("+25:00"))
        self.assertEqual(_meta.history_from(commits).created_at, datetime.fromtimestamp(
            int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_a_commit_after_the_checkout_reaches_the_count_and_nothing_else(self):
        """The checkout bounds `last_commit_sha` and `created_at`. `#commits` includes
        `git log --all` and does not depend on the checkout. The file history step checks a
        clone out at the recorded commit, and a measurement taken there has to reflect that."""
        first = _commit(self.repo, "CLAUDE.md", "# one\n", "2021-01-01T09:00:00+00:00")
        _commit(self.repo, "CLAUDE.md", "# two\n", "2022-02-02T09:00:00+00:00")
        _run(["git", "checkout", "-q", "--detach", first], cwd=self.repo)
        history = _meta.history_from(
            self.extractor.get_path_commits(str(self.repo), "CLAUDE.md"))
        self.assertEqual(history.last_commit_sha, first)
        self.assertEqual(history.first_commit_sha, first)
        self.assertEqual(history.commits, "2")

    def test_a_path_with_no_history_returns_empty_lists(self):
        _commit(self.repo, "a.md", "# a\n", "2021-01-01T09:00:00+00:00")
        commits = self.extractor.get_path_commits(str(self.repo), "absent.md")
        self.assertEqual((commits.followed, commits.exact, commits.everywhere),
                         ([], [], []))
        self.assertIsNone(commits.first())
        self.assertIsNone(commits.last())

    def test_a_renamed_file_was_created_by_its_first_commit_under_the_old_name(self):
        """`git log --follow --reverse` over a renamed file lists the rename commit and
        nothing else (git 2.55 here), which would make a renamed file's `created_at` and
        `first_commit_sha` the rename's and leave the commits after the rename out of the
        followed list. The follow query lists the newest commit first, and Python reverses the
        list. `last_commit_sha` stays with the current name, and `#commits` counts the commits
        under both names."""
        created = _commit(self.repo, "OLD.md", "# guidance\n" + "x" * 80,
                          "2021-01-01T09:00:00+00:00")
        _run(["git", "mv", "OLD.md", "CLAUDE.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "rename"], cwd=self.repo,
             env={**os.environ, "GIT_AUTHOR_DATE": "2022-02-02T09:00:00+00:00",
                  "GIT_COMMITTER_DATE": "2022-02-02T09:00:00+00:00"})
        renamed = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        edited = _commit(self.repo, "CLAUDE.md", "# guidance\n" + "x" * 80 + "\nmore\n",
                         "2023-03-03T09:00:00+00:00")
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertEqual([c["hash"] for c in commits.followed], [created, renamed, edited])
        history = _meta.history_from(commits)
        self.assertEqual(history.created_at, "2021-01-01T09:00:00Z")
        self.assertEqual(history.first_commit_sha, created)
        self.assertEqual(history.last_commit_sha, edited)
        self.assertEqual(history.commits, "3")

    def test_a_copy_of_another_file_was_created_by_the_commit_that_copied_it(self):
        """`git log --follow` crosses a copy as it crosses a rename: an AGENTS.md written as
        a copy of the CLAUDE.md beside it would get the CLAUDE.md's creation commit. The
        follow query stops at the commit with the status of a copy."""
        guidance = "# Guidance\n\nRun the tests before every commit.\n" + "x" * 80 + "\n"
        _commit(self.repo, "CLAUDE.md", guidance, "2021-01-01T09:00:00+00:00")
        copied = _commit(self.repo, "AGENTS.md", guidance, "2022-02-02T09:00:00+00:00")
        status = _run(["git", "log", "--follow", "--name-status", "--format=", "--",
                       "AGENTS.md"], cwd=self.repo).stdout
        self.assertIn("C100\tCLAUDE.md\tAGENTS.md", status,
                      "git no longer follows a copy, so this test checks nothing")
        commits = self.extractor.get_path_commits(str(self.repo), "AGENTS.md")
        self.assertEqual([c["hash"] for c in commits.followed], [copied])
        history = _meta.history_from(commits)
        self.assertEqual(history.created_at, "2022-02-02T09:00:00Z")
        self.assertEqual(history.first_commit_sha, copied)

    def test_a_path_added_again_as_a_copy_dates_from_the_commit_that_first_added_it(self):
        """A commit that adds a deleted AGENTS.md again with the text that it moves out of the
        CLAUDE.md beside it has the status of a copy, as in angular-eslint/angular-eslint, and
        the follow query stops there. The exact query lists the commits of the path before the
        deletion, which `#commits` and the AI columns count, and `created_at`,
        `first_commit_sha` and the creation commit of the AI columns name the first of them."""
        from agentcontext import commits as commit_heuristics
        added = _commit(self.repo, "AGENTS.md", "# Agents\n\nThe first version.\n" + "a" * 80,
                        "2021-01-01T09:00:00+00:00")
        _commit(self.repo, "AGENTS.md", "# Agents\n\nThe second version.\n" + "b" * 80,
                "2021-06-01T09:00:00+00:00")
        guidance = "# Guidance\n\nRun the tests before every commit.\n" + "x" * 160 + "\n"
        _commit(self.repo, "CLAUDE.md", guidance + "Keep the changelog current.\n",
                "2022-01-01T09:00:00+00:00")
        dated = {**os.environ, "GIT_AUTHOR_DATE": "2023-01-01T09:00:00+00:00",
                 "GIT_COMMITTER_DATE": "2023-01-01T09:00:00+00:00"}
        _run(["git", "rm", "-q", "AGENTS.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "remove AGENTS.md"], cwd=self.repo, env=dated)
        (self.repo / "AGENTS.md").write_text(guidance, encoding="utf-8")
        (self.repo / "CLAUDE.md").write_text("See AGENTS.md.\n", encoding="utf-8")
        _run(["git", "add", "AGENTS.md", "CLAUDE.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "move the guidance into AGENTS.md"], cwd=self.repo,
             env={**dated, "GIT_AUTHOR_DATE": "2024-01-01T09:00:00+00:00",
                  "GIT_COMMITTER_DATE": "2024-01-01T09:00:00+00:00"})
        moved = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        status = _run(["git", "log", "--follow", "--name-status", "--format=", "--",
                       "AGENTS.md"], cwd=self.repo).stdout
        self.assertRegex(status.splitlines()[0], r"^C\d+\tCLAUDE\.md\tAGENTS\.md$",
                         "git no longer reads the commit as a copy, so this test checks nothing")

        path_history = self.extractor.get_path_commits(str(self.repo), "AGENTS.md")
        self.assertEqual([c["hash"] for c in path_history.followed], [moved])
        history = _meta.history_from(path_history)
        self.assertEqual(history.created_at, "2021-01-01T09:00:00Z")
        self.assertEqual(history.first_commit_sha, added)
        self.assertEqual(history.last_commit_sha, moved)
        self.assertEqual(history.commits, "4")
        self.assertEqual(
            commit_heuristics.path_commits_entry("AGENTS.md", path_history)["first"], added)

    def test_a_rename_with_an_earlier_author_date_keeps_the_creation_commit(self):
        """The follow query lists the rename, which is the first commit of the exact query, so
        the creation commit under the old name stays first, also where a rebase left the rename
        with an author date before that of the creation commit."""
        created = _commit(self.repo, "OLD.md", "# guidance\n" + "x" * 80,
                          "2021-01-01T09:00:00+00:00")
        _run(["git", "mv", "OLD.md", "CLAUDE.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "rename"], cwd=self.repo,
             env={**os.environ, "GIT_AUTHOR_DATE": "2020-02-02T09:00:00+00:00",
                  "GIT_COMMITTER_DATE": "2022-02-02T09:00:00+00:00"})
        renamed = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertEqual([c["hash"] for c in commits.exact], [renamed])
        self.assertEqual(commits.first()["hash"], created)
        self.assertEqual(_meta.history_from(commits).created_at, "2021-01-01T09:00:00Z")

    def test_a_path_a_merge_added_still_has_a_creation_commit(self):
        """`git log --reverse --follow` lists nothing, at exit 0, for a path that a merge
        added and that neither parent contained. The creation commit then comes from the
        other queries, because a row with `#commits` and `last_commit_sha` filled and
        `created_at` and `first_commit_sha` blank is one that `file_history` rejects, and the
        four AI columns read the same `first()`."""
        _commit(self.repo, "base.md", "# base\n", "2020-01-01T09:00:00+00:00")
        trunk = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo).stdout.strip()
        _run(["git", "checkout", "-q", "-b", "side"], cwd=self.repo)
        _run(["git", "commit", "-q", "--allow-empty", "-m", "side"], cwd=self.repo)
        _run(["git", "checkout", "-q", trunk], cwd=self.repo)
        _run(["git", "commit", "-q", "--allow-empty", "-m", "trunk"], cwd=self.repo)
        subprocess.run(["git", "merge", "--no-commit", "--no-ff", "side"], cwd=self.repo,
                       capture_output=True, text=True)
        (self.repo / "AGENTS.md").write_text("# only in the merge\n", encoding="utf-8")
        _run(["git", "add", "AGENTS.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "--no-edit"], cwd=self.repo,
             env={**os.environ, "GIT_AUTHOR_DATE": "2021-03-03T09:00:00+00:00",
                  "GIT_COMMITTER_DATE": "2021-03-03T09:00:00+00:00"})
        merge = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()

        commits = self.extractor.get_path_commits(str(self.repo), "AGENTS.md")
        self.assertEqual(commits.followed, [], "the fixture no longer reproduces the case")
        self.assertEqual([c["hash"] for c in commits.exact], [merge])
        self.assertEqual(commits.first()["hash"], merge)
        history = _meta.history_from(commits)
        self.assertTrue(history.is_complete())
        self.assertEqual(history.created_at, "2021-03-03T09:00:00Z")
        self.assertEqual(history.first_commit_sha, merge)
        self.assertEqual(history.last_commit_sha, merge)

    def test_the_union_contains_every_commit_once_oldest_first(self):
        first = _commit(self.repo, "CLAUDE.md", "# one\n", "2021-01-01T09:00:00+00:00")
        second = _commit(self.repo, "CLAUDE.md", "# two\n", "2022-02-02T09:00:00+00:00")
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertEqual([c["hash"] for c in commits.union()], [first, second])
        self.assertEqual(sorted(commits.union()[0]), sorted(_meta.PATH_COMMIT_FIELDS))

    def test_a_body_with_newlines_and_trailers_stays_one_record(self):
        target = self.repo / "CLAUDE.md"
        target.write_text("# hi\n", encoding="utf-8")
        _run(["git", "add", "CLAUDE.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-F", "-"], cwd=self.repo,
             input="feat: x\n\nline one\nline two\n\nCo-authored-by: Claude <n@a.i>\n")
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertEqual(len(commits.exact), 1)
        self.assertEqual(commits.exact[0]["subject"], "feat: x")
        self.assertIn("Co-authored-by: Claude <n@a.i>", commits.exact[0]["body"])

    def test_output_that_is_not_utf8_is_read_rather_than_dropped(self):
        """A commit message in another encoding occurs in the sample, and git prints identity
        fields as they are. git 2.55 re-encodes both to UTF-8 on output, and a Latin-1 commit
        made here therefore cannot reproduce the case, while older versions pass the bytes
        through. Latin-1 output requested from git gives the same bytes on every version. With
        strict decoding, the history query would raise inside run_bounded without any count of
        a failure, and the path would have no history while the row read as complete."""
        target = self.repo / "CLAUDE.md"
        target.write_text("# hi\n", encoding="utf-8")
        _run(["git", "add", "CLAUDE.md"], cwd=self.repo)
        _run(["git", "commit", "-q", "-m", "caf\u00e9 au lait"], cwd=self.repo)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", "i18n.logOutputEncoding"),
                           ("GIT_CONFIG_VALUE_0", "latin1")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        commits = self.extractor.get_path_commits(str(self.repo), "CLAUDE.md")
        self.assertIsNotNone(commits)
        self.assertEqual(len(commits.exact), 1)
        self.assertEqual(commits.exact[0]["subject"], "caf\ufffd au lait")
        self.assertEqual(_meta._failures(self.extractor), _meta._ZERO_FAILURES)

    def test_a_history_query_that_failed_measures_blank_not_zero(self):
        history = _meta.history_from(None)
        self.assertEqual(history, _meta.caches.FileHistory())
        self.assertFalse(history.is_complete())

    def test_history_from_a_path_with_no_history_counts_zero(self):
        history = _meta.history_from(_meta.PathCommits([], [], []))
        self.assertEqual(history.commits, "0")
        self.assertEqual(history.created_at, "")


class TestFailures(_RepoCase):
    def test_a_fresh_client_has_no_failures(self):
        self.assertEqual(_meta._failures(self.extractor), _meta._ZERO_FAILURES)

    def test_a_history_query_that_raises_returns_none_and_increments_a_counter(self):
        original = _meta.bounds.run_bounded

        def always_time_out(cmd, *args, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 1)

        _meta.bounds.run_bounded = always_time_out
        self.addCleanup(setattr, _meta.bounds, "run_bounded", original)
        self.assertIsNone(self.extractor.get_path_commits(str(self.repo), "CLAUDE.md"))
        self.assertNotEqual(_meta._failures(self.extractor), _meta._ZERO_FAILURES)

    def test_a_failed_history_query_measures_blank_rather_than_zero(self):
        """`0` is a measurement: a path that no commit touched. A caller that writes a failure
        down as `0` records it as complete."""
        original = _meta.bounds.run_bounded
        _meta.bounds.run_bounded = lambda cmd, *a, **k: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd, 1))
        self.addCleanup(setattr, _meta.bounds, "run_bounded", original)
        facts = _meta.measure_artifact(self.extractor, str(self.repo), "x.md")
        self.assertEqual(facts.commits, "")
        self.assertNotEqual(_meta._failures(self.extractor), _meta._ZERO_FAILURES)


class TestMeasureRepoScratch(_RepoCase):
    def test_the_clone_goes_under_the_scratch_dir_and_is_removed(self):
        _commit(self.repo, "CLAUDE.md", "# hi\n" + "x" * 80, "2021-01-01T09:00:00+00:00")
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.repo).stdout.strip()
        scratch = Path(self.tmp.name) / "volume" / "scratch"
        # The CI runner has a GITHUB_TOKEN, which puts a placeholder user into the
        # clone URL and takes it past the redirect below to github.com.
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{self.repo}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        seen = []
        original = _meta.tempfile.mkdtemp

        def watched(*args, **kwargs):
            path = original(*args, **kwargs)
            # The clone, and not the empty working tree of the queries (`path_commits`).
            if not Path(path).name.startswith("no-worktree-"):
                seen.append(Path(path))
            return path
        _meta.tempfile.mkdtemp = watched
        self.addCleanup(setattr, _meta.tempfile, "mkdtemp", original)
        measured, error = _meta.measure_repo("owner/repo", head, "2021-01-01T09:00:00Z",
                                             "main", ["CLAUDE.md"], None,
                                             sleep=lambda s: None, scratch_dir=scratch)
        self.assertEqual(error, "")
        self.assertEqual(measured["CLAUDE.md"].commits, "1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].parent, scratch)
        self.assertEqual(list(scratch.iterdir()), [], "the clone was left behind")


class TestTheTwoCloneFormsReadTheSameCommits(_RepoCase):
    """The two clone forms over one fixture repository, so that the comparison runs in CI.

    The file history step's clone (`_scan_clone`) is `--single-branch --branch <branch>` and
    reads `git log --all`, and the cloning step clones `--depth 1 --single-branch
    --filter=blob:none` of the default branch and fetches the full history. Both clones have
    one ref. Over a repository with a second branch, a tag and a merge, the two histories
    have to contain the same commits when they name the same branch. When they do not, they
    differ by exactly the other branch's own commits, as the comparison over the local Git
    mirrors shows.
    """

    def _remote(self):
        _commit(self.repo, "a.md", "one\n", "2021-01-01T09:00:00+00:00")
        _run(["git", "checkout", "-q", "-b", "case"], cwd=self.repo)
        case = _commit(self.repo, "b.md", "two\n", "2021-02-02T09:00:00+00:00")
        _run(["git", "checkout", "-q", "main"], cwd=self.repo)
        _commit(self.repo, "c.md", "three\n", "2021-03-03T09:00:00+00:00")
        _run(["git", "merge", "-q", "--no-ff", "-m", "merge case", "case"],
             cwd=self.repo, env={**os.environ, "GIT_AUTHOR_DATE": "2021-04-04T09:00:00+00:00",
                                 "GIT_COMMITTER_DATE": "2021-04-04T09:00:00+00:00"})
        _run(["git", "tag", "v1"], cwd=self.repo)
        _run(["git", "checkout", "-q", "-b", "unmerged"], cwd=self.repo)
        unmerged = _commit(self.repo, "d.md", "four\n", "2021-05-05T09:00:00+00:00")
        _run(["git", "checkout", "-q", "main"], cwd=self.repo)
        return case, unmerged

    @staticmethod
    def _shas(repo_dir) -> set:
        return set(_run(["git", "log", "--all", "--format=%H"], cwd=repo_dir)
                   .stdout.split())

    def _scan_clone(self, branch) -> Path:
        dest = Path(self.tmp.name) / f"scan-{branch}"
        _run(["git", "clone", "-q", "--single-branch", "--branch", branch,
              f"file://{self.repo}", str(dest)])
        return dest

    def _collect_clone(self) -> Path:
        dest = Path(self.tmp.name) / 'collection'
        _run(["git", "clone", "-q", "--depth", "1", "--single-branch", "--filter=blob:none",
              f"file://{self.repo}", str(dest)])
        _run(["git", "fetch", "-q", "--unshallow"], cwd=dest)
        return dest

    def test_the_same_branch_gives_the_same_commit_set(self):
        _, unmerged = self._remote()
        scan, collect = self._shas(self._scan_clone("main")), self._shas(self._collect_clone())
        self.assertEqual(scan, collect)
        self.assertEqual(len(scan), 4, "the merged case commit is reached through main")
        self.assertNotIn(unmerged, scan)

    def test_a_different_branch_differs_by_that_branchs_own_commits(self):
        _, unmerged = self._remote()
        scan, collect = self._shas(self._scan_clone("unmerged")), self._shas(self._collect_clone())
        self.assertEqual(scan - collect, {unmerged})
        self.assertEqual(collect - scan, set())


class TestCollectedRevision(_RepoCase):
    """Metadata must belong to the collected commit and complete history."""

    def setUp(self):
        super().setUp()
        self.first = _commit(self.repo, "CLAUDE.md", "first\n", "2024-01-01T00:00:00Z")
        self.last = _commit(self.repo, "CLAUDE.md", "last\n", "2024-02-01T00:00:00Z")
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        self.env = {"GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.{self.repo.as_uri()}.insteadOf",
                    "GIT_CONFIG_VALUE_0": "https://github.com/owner/repo.git"}

    def measure(self, head, date=""):
        path_histories = {}
        with unittest.mock.patch.dict(os.environ, self.env), \
                contextlib.redirect_stderr(io.StringIO()) as output:
            measured, error = _meta.measure_repo(
                "owner/repo", head, date, "main", ["CLAUDE.md"], None,
                sleep=lambda _: None, scratch_dir=Path(self.tmp.name) / "scratch",
                path_histories=path_histories)
        return measured, error, path_histories, output.getvalue()

    def test_an_available_collected_commit_is_measured_instead_of_the_branch_tip(self):
        measured, error, path_history, _ = self.measure(self.first)
        self.assertEqual(error, "")
        self.assertEqual(measured["CLAUDE.md"].first_commit_sha, self.first)
        self.assertEqual(measured["CLAUDE.md"].last_commit_sha, self.first)
        self.assertEqual(path_history["CLAUDE.md"].exact[-1]["hash"], self.first)

    def test_a_missing_commit_cannot_be_replaced_by_a_date_or_branch_tip(self):
        for date in ("2024-01-01T00:00:00Z", ""):
            with self.subTest(date=date):
                measured, error, path_history, output = self.measure("f" * 40, date)
                self.assertEqual(measured, {})
                self.assertEqual(path_history, {})
                self.assertEqual(error, "revision_unavailable")
                self.assertIn("f" * 40, output)
                self.assertIn("owner/repo", output)

    def test_a_missing_hash_or_mutable_ref_cannot_identify_a_collected_commit(self):
        for head in ("", "main"):
            with self.subTest(head=head):
                measured, error, path_history, _ = self.measure(head, "2024-01-01T00:00:00Z")
                self.assertEqual((measured, path_history), ({}, {}))
                self.assertEqual(error, "revision_unavailable")

    def test_failed_revision_resolution_cannot_report_a_successful_checkout(self):
        real = self.extractor._run_git_command

        def cannot_resolve(cmd, **kwargs):
            if f"{self.first}^{{commit}}" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: cannot resolve commit")
            return real(cmd, **kwargs)

        with unittest.mock.patch.object(self.extractor, "_run_git_command", cannot_resolve), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.extractor.checkout_ref(str(self.repo), self.first))

    def test_a_branch_named_as_a_hash_cannot_substitute_for_that_commit(self):
        missing = "f" * 40
        _run(["git", "branch", missing], cwd=self.repo)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(self.extractor.checkout_ref(str(self.repo), missing))

    def test_failed_unshallowing_does_not_produce_history_or_authorship(self):
        real = _meta.GitFileMetadata._run_git_command

        def shallow_clone(extractor, repo_name, branch, destination):
            return real(extractor, ["git", "clone", "--depth", "1", self.repo.as_uri(),
                                    destination], cwd=".").returncode == 0

        def rejected_fetch(extractor, cmd, **kwargs):
            if "--unshallow" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: fetch rejected")
            return real(extractor, cmd, **kwargs)

        with unittest.mock.patch.object(_meta.GitFileMetadata, "clone_repo", shallow_clone), \
                unittest.mock.patch.object(_meta.GitFileMetadata, "_run_git_command", rejected_fetch):
            measured, error, path_history, output = self.measure(self.last)
        self.assertEqual((measured, path_history), ({}, {}))
        self.assertEqual(error, "git_unavailable")
        self.assertIn("history", output)

    def test_an_unrecognized_shallow_status_does_not_establish_complete_history(self):
        for output in ("", "unknown\n"):
            with self.subTest(output=output), \
                    unittest.mock.patch.object(self.extractor, "_run_git_command",
                        return_value=subprocess.CompletedProcess([], 0, output, "")), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertIsNone(self.extractor.is_shallow_repo(str(self.repo)))

    def test_completed_history_is_usable_after_a_fetch_timeout(self):
        def shallow_clone(extractor, repo_name, branch, destination):
            return extractor._run_git_command(
                ["git", "clone", "--depth", "1", self.repo.as_uri(), destination],
                cwd=".").returncode == 0

        real = _meta.bounds.run_bounded
        stopped = []

        def late_timeout(cmd, **kwargs):
            result = real(cmd, **kwargs)
            if "--unshallow" in cmd and not stopped:
                stopped.append(True)
                raise subprocess.TimeoutExpired(cmd, 1)
            return result

        with unittest.mock.patch.object(_meta.GitFileMetadata, "clone_repo", shallow_clone), \
                unittest.mock.patch.object(_meta.bounds, "run_bounded", late_timeout):
            measured, error, _, _ = self.measure(self.last)
        self.assertEqual(error, "")
        self.assertEqual(measured["CLAUDE.md"].first_commit_sha, self.first)
        self.assertEqual(measured["CLAUDE.md"].commits, "2")


# ---------------------------------------------------------------------------
# Whether a repository is gone or the run failed
# ---------------------------------------------------------------------------
class TestCloneOutcome(unittest.TestCase):
    """Only the first prunes rows from the dataset, and the two must not be
        confused."""

    def _extractor(self, stderr="", transient=0, size_bound_exceeded=0):
        extractor = _meta.GitFileMetadata()
        extractor.last_clone_stderr = stderr
        extractor.transient_failures = transient
        extractor.size_bound_exceeded = size_bound_exceeded
        return extractor

    def test_a_repository_the_server_does_not_have_is_gone(self):
        for stderr in ("remote: Repository not found.",
                       "fatal: '/nowhere' does not appear to be a git repository",
                       # GitHub's closure messages, which the cloning step records as
                       # permanent failures.
                       "remote: Repository unavailable due to DMCA takedown.",
                       "remote: Access to this repository has been disabled by GitHub staff."):
            with self.subTest(stderr=stderr):
                self.assertEqual(_meta._clone_outcome(self._extractor(stderr)),
                                 "clone_failed")

    def test_a_missing_branch_is_not_a_repository_that_is_gone(self):
        """git names the branch because the repository responded. A recorded branch that is
        gone prunes nothing, as the step documents, and a repository that renamed its branch
        keeps its rows."""
        self.assertEqual(
            _meta._clone_outcome(self._extractor(
                "fatal: Remote branch main not found in upstream origin")),
            "git_unavailable")

    def test_an_ssh_connection_that_failed_is_not_a_repository_that_is_gone(self):
        """git prints this whenever it cannot read the ref advertisement over SSH, for a
        refused connection or a failed host-key check as much as for a missing repository,
        which has its own line, "Repository not found", that decides that case."""
        self.assertEqual(
            _meta._clone_outcome(self._extractor(
                "fatal: Could not read from remote repository.")),
            "git_unavailable")

    def test_a_network_failure_is_the_run_failing(self):
        self.assertEqual(
            _meta._clone_outcome(self._extractor("Could not resolve host", transient=1)),
            "git_unavailable")

    def test_a_size_bound_breach_is_the_run_failing(self):
        self.assertEqual(
            _meta._clone_outcome(self._extractor("Repository not found.", size_bound_exceeded=1)),
            "git_unavailable")

    def test_a_clone_that_a_bound_stopped_is_not_measured_again(self):
        """Another attempt would run under the same bounds, and the file history step therefore
        neither retries it nor waits for the network on its behalf."""
        for bound in ("time_bound", "size_bound"):
            with self.subTest(bound=bound):
                extractor = self._extractor("", transient=1)
                extractor.last_clone_bound = bound
                self.assertEqual(_meta._clone_outcome(extractor), "bound_exceeded")

    def test_an_unrecognized_failure_keeps_the_rows(self):
        self.assertEqual(_meta._clone_outcome(self._extractor("something else")),
                         "git_unavailable")


class TestCloneWithRetries(unittest.TestCase):
    """The git client's clone and its retries, against a repository built with `git init`."""

    def test_a_clone_that_the_time_bound_stopped_is_not_tried_again(self):
        """The next attempt runs under the same time bound, and in the file history step every
        attempt of a repository that the bound stops holds a worker for the whole bound, on
        every run. The first attempt's upload-pack stops once the pack has started, and the
        time bound kills the clone in flight at that stall. The second attempt, which would not
        stall, is never made."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        remote = root / "remote"
        _init_repo(remote)
        _commit(remote, "CLAUDE.md", "# Claude\n", "2024-01-01T00:00:00Z")
        _add_noise(remote, "large.bin", 2_000_000)
        script = upload_pack(root, stall="first")
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key, value in ext_redirect(script, remote,
                                       "https://github.com/owner/repo.git").items():
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        extractor = _meta.GitFileMetadata()
        temp_dir = root / "clone"
        temp_dir.mkdir()
        with time_bound_at_stall(script), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            cloned = _meta._clone_with_retries(extractor, "owner/repo", "main", str(temp_dir),
                                               retries=2, sleep=lambda s: None)
        self.assertTrue(script.with_suffix(".stalled").exists(), "the first attempt did not stall")
        self.assertFalse(cloned, "the clone was tried again after the time bound stopped it")
        self.assertEqual(extractor.transient_failures, 1)
        self.assertEqual(extractor.last_clone_bound, "time_bound")
        self.assertFalse((temp_dir / "CLAUDE.md").exists())
        self.assertEqual(_meta._clone_outcome(extractor), "bound_exceeded",
                         "the file history step measures the repository again")

    def test_a_clone_that_fails_every_attempt_logs_what_git_said(self):
        """The operator reads the cause of `git_unavailable` in the history log, such as an
        outage of the network that git reports, and a count of retries alone names none."""
        class Failing:
            last_clone_bound = None
            last_clone_stderr = ("Cloning into '.'...\nfatal: unable to access "
                                 "'https://github.com/owner/repo/': Failed to connect to "
                                 "github.com port 443\n")

            def clone_repo(self, *args):
                return False

        err = io.StringIO()
        with tempfile.TemporaryDirectory() as temp_dir, contextlib.redirect_stderr(err):
            cloned = _meta._clone_with_retries(Failing(), "owner/repo", "main", temp_dir,
                                               retries=2, sleep=lambda s: None)
        self.assertFalse(cloned)
        self.assertIn("WARNING history:     Git could not clone owner/repo in 2 attempts "
                      "(fatal: unable to access 'https://github.com/owner/repo/': Failed to "
                      "connect to github.com port 443)", err.getvalue())

    def test_a_clone_stopped_by_a_bound_is_attempted_once(self):
        """A fake clone that raises at the time bound or the size bound: one call of git, and
        no wait for an attempt that does not come."""
        for error in (subprocess.TimeoutExpired(["git", "clone"], 3600),
                      _meta.bounds.GitSizeBoundExceeded("clone", 2 * 1024 ** 3, 1024 ** 3)):
            with self.subTest(error=type(error).__name__):
                calls, waits = [], []

                def stopped(cmd, **kwargs):
                    calls.append(cmd)
                    raise error

                with tempfile.TemporaryDirectory() as tmp, \
                        unittest.mock.patch.object(_meta.bounds, "run_bounded", stopped), \
                        contextlib.redirect_stderr(io.StringIO()):
                    cloned = _meta._clone_with_retries(_meta.GitFileMetadata(), "owner/repo",
                                                       "main", tmp, retries=3,
                                                       sleep=waits.append)
                self.assertFalse(cloned)
                self.assertEqual(len(calls), 1)
                self.assertEqual(waits, [])

    def _local_repository(self) -> Path:
        """A repository built with `git init`, which a clone of github.com/owner/repo reads."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        remote = root / "remote"
        _init_repo(remote)
        _commit(remote, "CLAUDE.md", "# Claude\n", "2024-01-01T00:00:00Z")
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key, value in (("GIT_CONFIG_COUNT", "2"),
                           ("GIT_CONFIG_KEY_0", f"url.{remote.as_uri()}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git"),
                           ("GIT_CONFIG_KEY_1", "uploadpack.allowFilter"),
                           ("GIT_CONFIG_VALUE_1", "true")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        (root / "clone").mkdir()
        return root / "clone"

    def _clones(self, temp_dir: Path, branch: str, fail_first: bool) -> tuple:
        """Whether `_clone_with_retries` cloned, and every clone command that it ran, with the
        first clone failing on the network when `fail_first` holds."""
        real, clones = _meta.bounds.run_bounded, []

        def bounded(cmd, **kwargs):
            if "clone" in cmd:
                clones.append(cmd)
                if fail_first and len(clones) == 1:
                    return subprocess.CompletedProcess(
                        cmd, 128, "", "error: RPC failed; curl 18 transfer closed\n"
                                      "fatal: early EOF\n")
            return real(cmd, **kwargs)

        with unittest.mock.patch.object(_meta.bounds, "run_bounded", side_effect=bounded), \
                contextlib.redirect_stderr(io.StringIO()), \
                contextlib.redirect_stdout(io.StringIO()):
            cloned = _meta._clone_with_retries(_meta.GitFileMetadata(), "owner/repo", branch,
                                               str(temp_dir), retries=2, sleep=lambda s: None)
        return cloned, clones

    def test_a_clone_that_failed_on_the_network_is_tried_again_without_the_full_history(self):
        """A full clone after an early EOF would fetch every version of every file, and on a
        large repository the size bound or the time bound would end the retries."""
        temp_dir = self._local_repository()
        cloned, clones = self._clones(temp_dir, "main", fail_first=True)
        self.assertTrue(cloned)
        self.assertEqual([cmd for cmd in clones if "--filter=blob:none" not in cmd], [])
        self.assertTrue((temp_dir / "CLAUDE.md").exists())

    def test_a_recorded_branch_that_is_gone_clones_the_default_branch(self):
        temp_dir = self._local_repository()
        cloned, clones = self._clones(temp_dir, "renamed", fail_first=False)
        self.assertTrue(cloned)
        self.assertNotIn("--branch", clones[-1])
        self.assertTrue((temp_dir / "CLAUDE.md").exists())

    def test_the_backoff_is_between_attempts_and_not_after_the_last(self):
        """The loop does not sleep after its last attempt, because no attempt comes after it."""
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.{(root / 'absent').as_uri()}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        temp_dir = root / "clone"
        temp_dir.mkdir()
        waits = []
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            cloned = _meta._clone_with_retries(_meta.GitFileMetadata(), "owner/repo", "main",
                                               str(temp_dir), retries=3, backoff=10,
                                               sleep=waits.append)
        self.assertFalse(cloned)
        self.assertEqual(waits, [10, 20])


class TestAGitmodulesThatGitCannotParse(unittest.TestCase):
    """A `.gitmodules` with committed merge-conflict markers beside a gitlink. A git command
    that compares trees across the gitlink reads the `.gitmodules` of the working tree and exits
    128 on it. The collection of the repository would then fail with a collector error at every
    Start, and the history of its files would stay unread."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        _init_repo(self.remote)
        _commit(self.remote, "README.md", "one\n", "2024-01-01T00:00:00Z")
        (self.remote / ".gitmodules").write_text(
            '<<<<<<< HEAD\n[submodule "a"]\n\tpath = vendor/lib\n=======\n'
            '[submodule "b"]\n\tpath = vendor/lib\n>>>>>>> other\n', encoding="utf-8")
        _run(["git", "add", ".gitmodules"], cwd=self.remote)
        _run(["git", "update-index", "--add", "--cacheinfo",
              f"160000,{'a' * 40},vendor/lib"], cwd=self.remote)
        _run(["git", "commit", "-q", "-m", "vendor"], cwd=self.remote,
             env={**os.environ, "GIT_AUTHOR_DATE": "2024-02-01T00:00:00Z",
                  "GIT_COMMITTER_DATE": "2024-02-01T00:00:00Z"})
        _commit(self.remote, "CLAUDE.md", "Use tabs.\n", "2024-03-01T00:00:00Z")

    def test_the_date_of_the_head_commit_is_read(self):
        clone = self.root / "clone"
        _run(["git", "clone", "-q", "--depth", "1", f"file://{self.remote}", str(clone)])
        branch, date, head = _clone.read_snapshot_metadata(clone)
        self.assertEqual((branch, date[:10]), ("main", "2024-03-01"))
        self.assertEqual(len(head), 40)

    def test_the_history_of_a_file_is_read(self):
        clone = self.root / "clone"
        _run(["git", "clone", "-q", f"file://{self.remote}", str(clone)])
        found = _meta.GitFileMetadata().get_path_commits(str(clone), "CLAUDE.md")
        self.assertIsNotNone(found)
        self.assertEqual([record["subject"] for record in found.followed], ["touch CLAUDE.md"])


class TestEnsureFullHistory(unittest.TestCase):
    """Whether the history is there is read from the clone, not from the fetch.

    A `--depth 1` clone stays at one commit when its `git fetch --unshallow` is killed at the
    timeout during the transfer, and every file measured against that commit would be cached
    as complete and never measured again.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.remote = self.root / "remote"
        _init_repo(self.remote)
        _commit(self.remote, "CLAUDE.md", "one\n", "2024-01-01T00:00:00Z")
        _commit(self.remote, "CLAUDE.md", "two\n", "2024-02-01T00:00:00Z")
        _commit(self.remote, "CLAUDE.md", "three\n", "2024-03-01T00:00:00Z")

    def _shallow_clone(self) -> Path:
        dest = self.root / "clone"
        _run(["git", "clone", "-q", "--depth", "1", f"file://{self.remote}", str(dest)])
        return dest

    def test_a_fetch_that_brings_the_history_is_true(self):
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        self.assertTrue(meta.is_shallow_repo(str(clone)))
        self.assertTrue(meta.ensure_full_history(str(clone)))
        self.assertFalse(meta.is_shallow_repo(str(clone)))

    def test_a_gitmodules_that_git_cannot_parse_does_not_stop_the_fetch(self):
        """A fetch that recurses into submodules, git's default, parses the `.gitmodules` of the
        working tree before it fetches, and a `.gitmodules` with committed merge-conflict
        markers fails it on every attempt. The history fetch reads no submodule, and the
        history is fetched."""
        _commit(self.remote, ".gitmodules",
                '<<<<<<< HEAD\n[submodule "a"]\n\tpath = a\n=======\n'
                '[submodule "b"]\n\tpath = b\n>>>>>>> other\n', "2024-04-01T00:00:00Z")
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        self.assertTrue(meta.ensure_full_history(str(clone)))
        self.assertIsNone(meta.history_failure)
        self.assertFalse(meta.is_shallow_repo(str(clone)))

    def test_a_fetch_that_stalls_ends_as_a_failure_of_the_network(self):
        """A connection that stays open after the network dropped moves nothing. Without git's
        low-speed bound, the fetch would wait for its time bound on both attempts, and the
        collection would record the history as too large for the time bound, which no later
        run of the collection fetches again."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        held = []

        def accept() -> None:
            while True:
                try:
                    held.append(listener.accept()[0])
                except OSError:
                    return

        threading.Thread(target=accept, daemon=True).start()

        def close() -> None:
            listener.close()
            for connection in held:
                connection.close()

        self.addCleanup(close)
        clone = self._shallow_clone()
        _run(["git", "-C", str(clone), "remote", "set-url", "origin",
              f"http://127.0.0.1:{listener.getsockname()[1]}/owner/repo.git"])
        meta = _meta.GitFileMetadata()
        started = time.monotonic()
        with unittest.mock.patch.object(_meta.bounds, "LOW_SPEED_SECONDS", 2), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(meta.ensure_full_history(str(clone), timeout=60))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(meta.history_failure, "exit")

    def test_a_complete_clone_fetches_nothing(self):
        clone = self.root / "full"
        _run(["git", "clone", "-q", f"file://{self.remote}", str(clone)])
        meta = _meta.GitFileMetadata()
        with unittest.mock.patch.object(meta, "_run_git_command",
                                        wraps=meta._run_git_command) as git:
            self.assertTrue(meta.ensure_full_history(str(clone)))
        self.assertNotIn("fetch", [c.args[0][1] for c in git.call_args_list])

    def test_a_fetch_that_fails_leaves_false_and_the_clone_shallow(self):
        clone = self._shallow_clone()
        shutil.rmtree(self.remote)
        meta = _meta.GitFileMetadata()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(meta.ensure_full_history(str(clone)))
        self.assertTrue(meta.is_shallow_repo(str(clone)))
        self.assertIn("attempt 1/2", err.getvalue())
        self.assertIn("attempt 2/2", err.getvalue())

    def test_a_fetch_that_fails_after_the_history_arrived_is_not_tried_again(self):
        """A fetch that git reports as failed can have brought the history, and a second
        attempt would warn that `--unshallow` makes no sense on a complete repository. The
        fetch runs for real, and its exit code is replaced with GitHub's refusal."""
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        fetches = []
        real = _meta.bounds.run_bounded

        def bounded(cmd, **kwargs):
            result = real(cmd, **kwargs)
            if "fetch" in cmd:
                fetches.append(cmd)
                return subprocess.CompletedProcess(
                    cmd, 128, "", "fatal: unable to access 'https://github.com/o/r/': The "
                                  "requested URL returned error: 400")
            return result

        with unittest.mock.patch.object(_meta.bounds, "run_bounded", side_effect=bounded), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(meta.ensure_full_history(str(clone)))
        self.assertEqual(len(fetches), 1)
        self.assertIn("attempt 1/2", err.getvalue())
        self.assertNotIn("attempt 2/2", err.getvalue())
        self.assertIsNone(meta.history_failure)

    def _stalled_clone(self, stall: str) -> tuple:
        """A depth-1 clone with a history fetch that goes through a stand-in for upload-pack.

        The stand-in stops sending once the pack has started, which is when git holds
        `.git/shallow.lock`, and a time bound kills the fetch there. The history has a file of two
        megabytes that the depth-1 clone lacks. The pack therefore spans many reads, and the stall
        comes before its end. Returns the clone and the stand-in's path.
        """
        _add_noise(self.remote, "large.bin", 2_000_000)
        _run(["git", "rm", "-q", "large.bin"], cwd=self.remote)
        _run(["git", "commit", "-q", "-m", "remove large.bin"], cwd=self.remote)
        clone = self._shallow_clone()
        script = upload_pack(self.root, stall=stall)
        _run(["git", "config", "remote.origin.uploadpack", f"{sys.executable} {script}"],
             cwd=clone)
        return clone, script

    def test_the_retry_after_a_timeout_runs_despite_the_lock_the_killed_fetch_left(self):
        """A fetch killed at the timeout leaves `.git/shallow.lock`, because git cannot clean up
        on SIGKILL. A second attempt that failed on the lock at once would record the timeout
        as a network failure, which every later run fetches again. The first fetch is stalled
        for real rather than mocked, and the time bound kills it at the stall. The retry runs
        under a time bound that only catches a hang."""
        clone, script = self._stalled_clone("first")
        meta = _meta.GitFileMetadata()
        with time_bound_at_stall(script), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertTrue(meta.ensure_full_history(str(clone), timeout=600))
        self.assertTrue(script.with_suffix(".stalled").exists(), "the first fetch did not stall")
        self.assertIn("attempt 1/2 raised TimeoutExpired", err.getvalue())
        self.assertNotIn("attempt 2/2", err.getvalue())
        self.assertIsNone(meta.history_failure)
        self.assertFalse(meta.is_shallow_repo(str(clone)))
        self.assertEqual(list((clone / ".git").rglob("*.lock")), [])

    def test_two_fetches_the_clock_cut_are_the_clock_and_leave_no_lock(self):
        """Both attempts stall and are killed at the time bound that they were given, with the clone
        as the directory under their size bound. A mocked timeout leaves no lock behind, and a test
        that mocks one would pass with the locks left in place, where the second attempt
        fails at once and the timeout is recorded as a network failure."""
        clone, _ = self._stalled_clone("always")
        meta = _meta.GitFileMetadata()
        with unittest.mock.patch.object(_meta.bounds, "run_bounded",
                                        wraps=_meta.bounds.run_bounded) as bounded, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertFalse(meta.ensure_full_history(str(clone), timeout=1.5,
                                                      bounded_path=str(clone)))
        fetches = [c for c in bounded.call_args_list if "fetch" in c.args[0]]
        self.assertEqual(len(fetches), 2)
        self.assertEqual({c.kwargs["timeout"] for c in fetches}, {1.5})
        self.assertEqual({c.kwargs["bounded_path"] for c in fetches}, {str(clone)})
        self.assertIn("attempt 2/2 raised TimeoutExpired", err.getvalue())
        self.assertEqual(meta.history_failure, "time_bound")
        self.assertEqual(list((clone / ".git").rglob("*.lock")), [])
        self.assertTrue(meta.is_shallow_repo(str(clone)))

    def test_a_size_bound_breach_is_false_and_not_tried_again(self):
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        fetches = []

        def bounded(cmd, **kwargs):
            if "fetch" in cmd:
                fetches.append(cmd)
                raise _meta.bounds.GitSizeBoundExceeded(clone, 10, 5)
            return real(cmd, **kwargs)

        real = _meta.bounds.run_bounded
        with unittest.mock.patch.object(_meta.bounds, "run_bounded", side_effect=bounded), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(meta.ensure_full_history(str(clone)))
        self.assertEqual(len(fetches), 1)

    def test_a_shallow_query_without_a_response_from_git_is_none_not_false(self):
        """A `fork()` refused at `is_shallow_repo`'s own git command gives no result, not "the
        history is here", which for a depth-1 clone would measure every file against one
        commit and cache the result as complete."""
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        with unittest.mock.patch.object(meta, "_run_git_command",
                                        side_effect=BlockingIOError(11, "no fork")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(meta.is_shallow_repo(str(clone)))
            self.assertFalse(meta.ensure_full_history(str(clone)))
        broken = subprocess.CompletedProcess(["git"], 128, "", "fatal: not a git repository")
        with unittest.mock.patch.object(meta, "_run_git_command", return_value=broken), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertIsNone(meta.is_shallow_repo(str(clone)))
        self.assertTrue(meta.is_shallow_repo(str(clone)), "queried for real, the clone is shallow")

    def _unanswered_after_the_fetch(self, failure):
        """`run_bounded` with the shallowness query after the fetch replaced by `failure`, a
        result or an exception, and every other command run for real."""
        real, fetched = _meta.bounds.run_bounded, []

        def bounded(cmd, **kwargs):
            if "--is-shallow-repository" in cmd and fetched:
                if isinstance(failure, BaseException):
                    raise failure
                return failure
            if "fetch" in cmd:
                fetched.append(cmd)
            return real(cmd, **kwargs)
        return bounded

    def test_a_shallow_query_after_the_fetch_without_a_response_from_git_is_no_history(self):
        """Only git's own `false` establishes that the history is there."""
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        refused = subprocess.CompletedProcess(["git"], 128, "", "fatal: index file corrupt")
        with unittest.mock.patch.object(_meta.bounds, "run_bounded",
                                        side_effect=self._unanswered_after_the_fetch(refused)), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(meta.ensure_full_history(str(clone)))

    def test_git_that_could_not_be_started_after_the_fetch_is_the_machines_failure(self):
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        refused = BlockingIOError(11, "Resource temporarily unavailable")
        with unittest.mock.patch.object(_meta.bounds, "run_bounded",
                                        side_effect=self._unanswered_after_the_fetch(refused)), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(_meta.MachineFailure):
            meta.ensure_full_history(str(clone))

    def test_git_that_could_not_be_started_is_the_machines_failure_not_the_repositorys(self):
        """A `fork()` refused at the fetch, or at the query before it, is not "the history is
        not there", which the cloning step would record as a repository with a history that
        did not arrive under the time bound, and would not collect again."""
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()
        real = _meta.bounds.run_bounded

        def refusing_fetch(cmd, **kwargs):
            if "fetch" in cmd:
                raise BlockingIOError(11, "Resource temporarily unavailable")
            return real(cmd, **kwargs)

        with unittest.mock.patch.object(_meta.bounds, "run_bounded", side_effect=refusing_fetch), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(_meta.MachineFailure):
            meta.ensure_full_history(str(clone))
        self.assertEqual(meta.machine_failures, 1)
        with unittest.mock.patch.object(_meta.bounds, "run_bounded",
                                        side_effect=BlockingIOError(11, "no fork")), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(_meta.MachineFailure):
            meta.ensure_full_history(str(clone))
        # A timeout remains the repository's failure: False, and no exception. Only the fetch
        # times out, because a time bound on every command would stop the query before it,
        # which returns None, and the fetch's own handling of a timeout would not be reached.
        def slow_fetch(cmd, **kwargs):
            if "fetch" in cmd:
                raise subprocess.TimeoutExpired(cmd, 1)
            return real(cmd, **kwargs)

        with unittest.mock.patch.object(_meta.bounds, "run_bounded", side_effect=slow_fetch), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertFalse(meta.ensure_full_history(str(clone)))
        self.assertEqual(meta.history_failure, "time_bound")

    def test_measure_repo_leaves_out_a_file_whose_query_exited_non_zero(self):
        # A `--follow` that exits 128 leaves the file out rather than caching four blanks.
        _commit(self.remote, "CLAUDE.md", "four\n", "2024-04-01T00:00:00Z")
        head = _run(["git", "rev-parse", "HEAD"], cwd=self.remote).stdout.strip()
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{self.remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        real = _meta.GitFileMetadata._run_git_command

        def failing_follow(self_, cmd, cwd, **kwargs):
            if "--follow" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", "fatal: could not fetch")
            return real(self_, cmd, cwd, **kwargs)

        with unittest.mock.patch.object(_meta.GitFileMetadata, "_run_git_command", failing_follow), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            measured, error = _meta.measure_repo("owner/repo", head, "2024-04-01T00:00:00Z",
                                                 "main", ["CLAUDE.md"], None,
                                                 sleep=lambda s: None,
                                                 scratch_dir=self.root / "scratch")
        self.assertEqual(error, "git_unavailable", "the file history step measures it again")
        self.assertEqual(measured, {})
        self.assertIn("Could not read the history of CLAUDE.md", err.getvalue())

    def test_a_history_fetch_that_a_bound_stopped_is_not_measured_again(self):
        """A fetch that exits non-zero belongs to the network on that day and is measured
        again. One that the time bound or the size bound stopped would be stopped again."""
        for failure, outcome in (("time_bound", "bound_exceeded"),
                                 ("size_bound", "bound_exceeded"),
                                 ("exit", "git_unavailable")):
            def no_history(extractor, *args, **kwargs):
                extractor.history_failure = failure
                return False

            with self.subTest(failure=failure), \
                    unittest.mock.patch.object(_meta, "_clone_with_retries", return_value=True), \
                    unittest.mock.patch.object(_meta.GitFileMetadata, "ensure_full_history",
                                               no_history), \
                    contextlib.redirect_stderr(io.StringIO()):
                measured, error = _meta.measure_repo("owner/repo", "a" * 40, "", "main",
                                                     ["CLAUDE.md"], None, sleep=lambda s: None,
                                                     scratch_dir=self.root / "scratch")
                self.assertEqual((measured, error), ({}, outcome))

    def test_a_fetch_killed_after_the_history_arrived_is_true(self):
        # The method checks the clone, not the exit code: a `git fetch --unshallow` can time
        # out after git has rewritten `.git/shallow`, and the rows measured then are correct.
        clone = self._shallow_clone()
        meta = _meta.GitFileMetadata()

        def bounded(cmd, **kwargs):
            if "fetch" in cmd:
                _run(["git", "fetch", "-q", "--unshallow"], cwd=clone)
                raise subprocess.TimeoutExpired(cmd, 1)
            return real(cmd, **kwargs)

        real = _meta.bounds.run_bounded
        with unittest.mock.patch.object(_meta.bounds, "run_bounded", side_effect=bounded), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(meta.ensure_full_history(str(clone)))


if __name__ == "__main__":
    unittest.main()
