"""Tests for agentcontext.steps.commit_reclassification and the commit log that it reads. The
cloning step keeps the commits that it read, and after the clone is deleted, a change to the
commit heuristics reaches the commit CSV and the AI columns through that log.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_steps_commit_reclassification -v
"""

import contextlib
import csv
import hashlib
import io
import json
import lzma
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from pathlib import Path

from agentcontext import caches, commits, paths, resume, reuse
from agentcontext.checks import commits as commit_validation
from agentcontext.collector import CollectionConfig
from agentcontext.git import history
from agentcontext.steps import commit_reclassification as _mod
from agentcontext.steps import detection, repository_cloning
from agentcontext.steps.repository_cloning import run_collection
from tests import _ambient_env
from tests._fixtures import _add_commit, _init_git_repo, report_counts


def _git(repo: Path, *args, env=None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


def _commit(repo: Path, rel: str, content: str, message: str) -> str:
    """Commit one file as a person with `message`, and return the hash."""
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="A Person", GIT_AUTHOR_EMAIL="a@example.com",
               GIT_COMMITTER_NAME="A Person", GIT_COMMITTER_EMAIL="a@example.com")
    _git(repo, "add", ".")
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-F", "-"], input=message,
                   text=True, capture_output=True, check=True, env=env)
    return _git(repo, "rev-parse", "HEAD")


def _run(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


class _HistoryRead(unittest.TestCase):
    """A test case with one repository. `_read_history` reads its commit history as the
    cloning step does and records the history of CLAUDE.md in the per-repository commit
    record. A test can then delete the clone."""

    REPO = "owner/repo"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        _git(self.clone, "init", "-q", "-b", "main")
        self.plain = _commit(self.clone, "README.md", "x\n", "docs: readme")
        self.trailer = _commit(self.clone, "CLAUDE.md", "rules\n",
                               "Add CLAUDE.md\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n")
        self.developed = _commit(self.clone, "CLAUDE.md", "rules, more\n",
                                 "Tighten the rules\n\n"
                                 "Co-developed-by: Claude <noreply@anthropic.com>\n")
        self.commits_dir = self.tmp / "collect_commits"
        self.head = _git(self.clone, "rev-parse", "HEAD")

    def _read_history(self, detect=None) -> dict:
        """Read the clone's commit history, with `detect` in place of the commit patterns of
        the six tools when given, and record the commits of CLAUDE.md in the per-repository
        commit record as the cloning step does."""
        original = commits.detect_ai_tools_in_fields
        if detect is not None:
            commits.detect_ai_tools_in_fields = detect
        try:
            record = commits.read_commit_history(self.REPO, self.head, "main", self.clone,
                                          self.commits_dir)
            path_history = history.path_commits(_run, str(self.clone), "CLAUDE.md")
            record["ai_rows"] = [dict(
                commits.caches.AiAuthorshipKey(self.REPO, self.head, "CLAUDE.md").as_row(),
                **commits.ai_authorship_from(path_history).as_row())]
            record["path_commits"] = [commits.path_commits_entry("CLAUDE.md", path_history)]
            record["collected_artifacts"] = ["CLAUDE.md"]
            commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1],
                                       record)
        finally:
            commits.detect_ai_tools_in_fields = original
        return record

    def _record(self) -> dict:
        return json.loads(commits.shard_paths(self.commits_dir, self.REPO)[1].read_text())

    def _shas(self) -> list:
        with open(commits.shard_paths(self.commits_dir, self.REPO)[0], newline="",
                  encoding="utf-8") as f:
            return sorted(row["commit_sha"] for row in csv.DictReader(f))


class TestTheCodeThatTheRunnerRecords(unittest.TestCase):
    """The runner records the reclassification and the checks after it under the code of their
    module closures. An edit to the collector or to a module that only schedules the collection
    (`reuse.SCHEDULING_MODULES`) collects nothing again. If that code were in these closures,
    such an edit would run the reclassification, the cleanup, the detection, the file history,
    the import detection and the checks again."""

    def test_the_names_of_the_cloning_step_s_outputs_are_the_cloning_step_s(self):
        self.assertEqual((_mod.COMMITS_DIR_NAME, _mod.COLLECTED_CSV_NAME, _mod.FAIL_CSV_NAME,
                          _mod.AI_COMMITS_CSV_NAME),
                         (repository_cloning.COMMITS_DIR_NAME,
                          repository_cloning.COLLECTED_CSV_NAME, repository_cloning.FAIL_CSV_NAME,
                          repository_cloning.AI_COMMITS_CSV_NAME))

    def test_the_collector_and_the_scheduling_of_the_collection_are_not_in_it(self):
        from agentcontext import runner
        root = Path(runner.__file__).resolve().parent
        left_out = {"collector.py", "reuse.py", "steps/repository_cloning.py"}
        for module in ("agentcontext.steps.commit_reclassification", "agentcontext.checks.snapshot",
                       "agentcontext.checks.unmatched", "agentcontext.checks.deletions"):
            with self.subTest(module=module):
                closure = {path.relative_to(root).as_posix()
                           for path in runner.module_closure(module)}
                self.assertIn(module.split(".", 1)[1].replace(".", "/") + ".py", closure)
                self.assertEqual(closure & left_out, set())


class TestClassificationInputs(unittest.TestCase):
    def test_copy_counts_ignore_torn_records_and_records_without_collection_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            finished = Path(tmp) / "finished.jsonl"
            rows = [{"repo": "o/r", "row": "row", "measures": {"copied": 2}},
                    {"repo": "no/files", "row": "row", "measures": {"copied": 0}},
                    {"repo": "o/r", "row": "row", "measures": {"copied": 5}},
                    {"repo": "o/r", "measures": {"copied": 0}}]
            finished.write_text("".join(json.dumps(row) + "\n" for row in rows) + '{"repo":')
            self.assertEqual(_mod.finished_copied(finished), {"o/r": 5, "no/files": 0})


class TestTheClassificationDigest(unittest.TestCase):
    """`commits.CLASSIFIED_BY` changes with a column of the two commit heuristics files that
    decides how a commit is classified, and not with a column that records where a row comes
    from. Reading a page again changes `doc_verified`, and a note that gains a quotation
    classifies no commit, so neither may make every per-repository commit record due for
    reclassification. Each case writes a copy of both files with one value changed and computes
    the digest over the copies."""

    PATHS = ("AI_COMMIT_PATTERNS_PATH", "OTHER_AI_COMMIT_PATTERNS_PATH")

    def _digest(self, path_name: str = "", change=None) -> str:
        """The digest over copies of the two files, the file of `path_name` with `change`
        applied to its rows."""
        from agentcontext import heuristics
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        patches = []
        for name in self.PATHS:
            with open(getattr(heuristics, name), newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                fieldnames, rows = reader.fieldnames, list(reader)
            if name == path_name:
                change(rows)
            copy = tmp / getattr(heuristics, name).name
            with open(copy, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, lineterminator="\n")
                writer.writeheader()
                writer.writerows(rows)
            patches.append(mock.patch.object(heuristics, name, copy))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            return commits._classified_by()

    @staticmethod
    def _change(column: str, value, condition=lambda row: True):
        """A change that sets `column` of the first heuristic that meets `condition`, not a
        comment, to `value(row)`."""
        def change(rows: list) -> None:
            row = next(row for row in rows if row["pattern"]
                       and not row["pattern"].startswith("#") and condition(row))
            row[column] = value(row)
        return change

    @staticmethod
    def _other_attribution(row: dict) -> str:
        """An attribution other than the row's that the loader accepts for its scope."""
        allowed = (("account", "convention") if row["scope"] == "author"
                   else ("reviewer", "standard", "convention"))
        return next(value for value in allowed if value != row["attribution"])

    def test_the_unchanged_copies_give_the_digest_of_the_files(self):
        self.assertEqual(self._digest(), commits.CLASSIFIED_BY)

    def test_where_a_row_comes_from_leaves_the_digest_unchanged(self):
        cited = lambda row: bool(row["doc_url"])
        changes = {
            "notes": self._change("notes", lambda row: row["notes"] + " Read again on 2026-11-01."),
            "doc_url": self._change("doc_url", lambda row: "https://example.com/a-moved-page",
                                    cited),
            "doc_verified": self._change(
                "doc_verified",
                lambda row: "2026-01-03" if row["doc_verified"] == "2026-01-02" else "2026-01-02",
                cited),
            "evidence": self._change(
                "evidence", lambda row: (row["evidence"] + " https://github.com/example/example/"
                                         "commit/0123456789ab").strip()),
        }
        for name in self.PATHS:
            for column, change in changes.items():
                with self.subTest(file=name, column=column):
                    self.assertEqual(self._digest(name, change), commits.CLASSIFIED_BY)

    def test_what_a_row_classifies_changes_the_digest(self):
        changes = {
            "pattern": self._change("pattern", lambda row: row["pattern"] + "|never-a-commit-line"),
            # The upper case keeps the prefix of the id, which the loader derives from the tool.
            "tool": self._change("tool", lambda row: row["tool"].upper()),
            "attribution": self._change("attribution", self._other_attribution),
            "id": self._change("id", lambda row: row["id"].rsplit("-", 1)[0] + "-990"),
        }
        for name in self.PATHS:
            for column, change in changes.items():
                with self.subTest(file=name, column=column):
                    self.assertNotEqual(self._digest(name, change), commits.CLASSIFIED_BY)

    def test_the_order_of_the_rows_changes_the_digest(self):
        """`matched_heuristics` joins the ids of the matching heuristics in file order, so two
        rows that change places change a published column without changing any value."""
        def swap(rows: list) -> None:
            first, second = [i for i, row in enumerate(rows)
                             if row["pattern"] and not row["pattern"].startswith("#")][:2]
            rows[first], rows[second] = rows[second], rows[first]
        for name in self.PATHS:
            with self.subTest(file=name):
                self.assertNotEqual(self._digest(name, swap), commits.CLASSIFIED_BY)


class TestReadingTheHistoryKeepsItsLog(_HistoryRead):

    def test_every_commit_the_cloning_step_read_is_in_the_commit_log(self):
        self._read_history()
        stored = commits.stored_log_path(self.commits_dir, self.REPO)
        with lzma.open(stored, "rb") as f:
            records = list(commits._records_from(f, "nul-v1"))
        self.assertEqual(sorted(r["hash"] for r in records),
                         sorted([self.plain, self.trailer, self.developed]))
        self.assertIn("Co-developed-by: Claude",
                      next(r for r in records if r["hash"] == self.developed)["body"])
        self.assertFalse((self.commits_dir / "owner§repo.log").exists(),
                         "the uncompressed commit log is not left behind")

    def test_the_per_repository_commit_record_names_the_heuristics_it_was_classified_under(self):
        self.assertEqual(self._read_history()["classified_by"], commits.CLASSIFIED_BY)

    def test_a_repository_collected_again_leaves_no_log_of_the_last_collection(self):
        self._read_history()
        commits.remove_shards(self.commits_dir, self.REPO)
        self.assertFalse(commits.stored_log_path(self.commits_dir, self.REPO).exists())

    def test_evidence_records_revision_refs_source_and_retrieval_date(self):
        _git(self.clone, "tag", "tree", "HEAD^{tree}")
        _git(self.clone, "tag", "-a", "snapshot", self.plain, "-m", "Snapshot",
             env=dict(os.environ, GIT_COMMITTER_NAME="Tagger", GIT_COMMITTER_EMAIL="t@example.com"))
        record = self._read_history()
        evidence = record["commit_evidence"]
        self.assertEqual(evidence["head_sha"], self.head)
        self.assertEqual(evidence["source_url"], "https://github.com/owner/repo.git")
        self.assertEqual(evidence["refs"]["refs/heads/main"]["commit_sha"], self.head)
        self.assertEqual(evidence["refs"]["refs/tags/snapshot"]["commit_sha"], self.plain)
        self.assertEqual(evidence["refs"]["refs/tags/snapshot"]["object_sha"],
                         _git(self.clone, "rev-parse", "snapshot"))
        self.assertEqual(evidence["refs"]["refs/tags/tree"]["object_sha"],
                         _git(self.clone, "rev-parse", "HEAD^{tree}"))
        self.assertEqual(evidence["refs"]["refs/tags/tree"]["commit_sha"], "")
        datetime.strptime(evidence["retrieved_at"], "%Y-%m-%dT%H:%M:%SZ")
        self.assertEqual(evidence["commit_count"], 3)
        self.assertFalse(evidence["shallow"])

    def test_detached_head_and_another_branch_are_both_retained(self):
        _git(self.clone, "checkout", "-q", "--detach", self.plain)
        detached = _commit(self.clone, "scratch", "x", "Detached commit")
        self.head = detached
        self._read_history()
        with lzma.open(commits.stored_log_path(self.commits_dir, self.REPO), "rb") as stream:
            hashes = {row["hash"] for row in commits._records_from(stream, "nul-v1")}
        self.assertEqual(hashes, {self.plain, self.trailer, self.developed, detached})

    def test_messages_with_record_separators_and_raw_identities_survive(self):
        message = "Change instructions\n\nControl bytes: \x1e and \x1f\n"
        (self.clone / "CLAUDE.md").write_text("Updated instructions\n")
        _git(self.clone, "add", ".")
        identity = dict(os.environ, GIT_AUTHOR_NAME="copilot-swe-agent[bot]",
                        GIT_AUTHOR_EMAIL="bot@example.com", GIT_COMMITTER_NAME="Human",
                        GIT_COMMITTER_EMAIL="human@example.com")
        subprocess.run(["git", "-C", str(self.clone), "commit", "-q", "-F", "-"],
                       input=message, text=True, capture_output=True, check=True, env=identity)
        self.head = _git(self.clone, "rev-parse", "HEAD")
        self._read_history()
        with lzma.open(commits.stored_log_path(self.commits_dir, self.REPO), "rb") as stream:
            rows = list(commits._records_from(stream, "nul-v1"))
        row = next(row for row in rows if row["hash"] == self.head)
        self.assertEqual(row["message"], message)
        self.assertEqual((row["author_name"], row["author_email"], row["committer_name"],
                          row["committer_email"]),
                         ("copilot-swe-agent[bot]", "bot@example.com", "Human", "human@example.com"))
        self.assertEqual(len(rows), 4)
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "current")

    def test_a_shallow_history_cannot_claim_complete_evidence(self):
        shallow = self.tmp / "shallow"
        _git(self.clone, "clone", "-q", "--depth", "1", self.clone.as_uri(), str(shallow))
        record = commits.read_commit_history(self.REPO, self.head, "main", shallow, self.commits_dir)
        self.assertEqual(record["outcome"], "history_read_failed")
        self.assertIn("no complete commit history", record["detail"])


class TestAChangedHeuristicReachesTheCollectionWithoutAClone(_HistoryRead):
    """The setup reads the commit history under heuristics that match nothing, which stand in
    for the heuristics of an older image, and then deletes the clone. Only the commit log is
    left to read."""

    def setUp(self):
        super().setUp()
        self.record = self._read_history(detect=lambda *fields, **kwargs: [])
        record = self._record()
        record["classified_by"] = "an older image"
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)
        shutil.rmtree(self.clone)

    def test_the_commits_the_current_heuristics_match_reach_the_shard(self):
        self.assertEqual(self._shas(), [])
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "reclassified")
        self.assertIn(self.trailer, self._shas())
        self.assertEqual(self._record()["classified_by"], commits.CLASSIFIED_BY)
        self.assertEqual(self._record()["commits_read"], 3)

    def test_the_ai_columns_of_a_configuration_file_are_read_again(self):
        self.assertEqual(self.record["ai_rows"][0]["first_commit_ai_created"], "FALSE",
                         "under the older heuristics nothing was an AI commit")
        with mock.patch.object(commits.bounds, "run_bounded",
                               side_effect=AssertionError("reclassification must use stored evidence")):
            commits.reclassify(self.commits_dir, self.REPO)
        row = self._record()["ai_rows"][0]
        self.assertEqual(row["artifact"], "CLAUDE.md")
        self.assertEqual(row["first_commit_ai_created"], "TRUE")
        self.assertEqual(row["first_commit_ai_tool"], "Claude")
        self.assertEqual(row["#ai_commits"], "2")

    def test_reclassification_removes_attributions_that_the_current_heuristics_explain(self):
        self.assertEqual(sorted(entry["token"] for entry in self._record()["unmatched"]),
                         ["co-authored-by: claude", "co-developed-by: claude"])
        commits.reclassify(self.commits_dir, self.REPO)
        self.assertEqual(self._record()["unmatched"], [])
        self.assertTrue(self.record["mentions"])
        self.assertEqual(self._record()["mentions"], [])

    def test_a_second_pass_finds_the_shard_current(self):
        commits.reclassify(self.commits_dir, self.REPO)
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "current")


class TestTheReclassificationEqualsTheCloningStep(_HistoryRead):

    def test_the_same_heuristics_write_the_same_shards(self):
        """What the step rewrites from the log is what the cloning step wrote from the clone."""
        self._read_history()
        csv_path, json_path = commits.shard_paths(self.commits_dir, self.REPO)
        other_path = commits.other_shard_path(self.commits_dir, self.REPO)
        written = (csv_path.read_bytes(), other_path.read_bytes(), json.loads(json_path.read_text()))
        stale = dict(written[2], classified_by="an older image")
        commits.write_shard_record(json_path, stale)
        shutil.rmtree(self.clone)
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "reclassified")
        self.assertEqual(csv_path.read_bytes(), written[0])
        self.assertEqual(other_path.read_bytes(), written[1])
        self.assertEqual(json.loads(json_path.read_text()), written[2])


class TestWhatCannotBeClassifiedAgain(_HistoryRead):

    def test_a_repository_whose_history_was_not_read(self):
        self.commits_dir.mkdir(parents=True)
        commits.write_shard_record(
            commits.shard_paths(self.commits_dir, self.REPO)[1],
            commits.unavailable_record(self.REPO, self.head, "main", "history_unavailable"))
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "history_not_read")

    def test_a_repository_without_a_commit_log_keeps_its_shards(self):
        self._read_history()
        before = self._shas()
        record = dict(self._record(), classified_by="an older image")
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)
        commits.stored_log_path(self.commits_dir, self.REPO).unlink()
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "no_log")
        self.assertEqual(self._shas(), before)


class TestEvidenceValidation(_HistoryRead):
    def _change_record(self, change):
        record = self._record()
        change(record)
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)

    def test_current_classification_still_requires_its_log(self):
        self._read_history()
        commits.stored_log_path(self.commits_dir, self.REPO).unlink()
        self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "no_log")

    def test_current_classification_checks_log_integrity(self):
        self._read_history()
        log = commits.stored_log_path(self.commits_dir, self.REPO)
        log.write_bytes(log.read_bytes()[:40])
        with self.assertRaisesRegex(ValueError, "checksum"):
            commits.reclassify(self.commits_dir, self.REPO)

    def test_missing_scope_path_history_and_failed_queries_are_incomplete(self):
        for key, value, outcome in (("commit_evidence", None, "no_scope"),
                                    ("path_commits", None, "no_path_history"),
                                    ("collected_artifacts", None, "no_path_history"),
                                    ("path_commits", [], "no_path_history"),
                                    ("history_failed", ["CLAUDE.md"], "history_incomplete")):
            with self.subTest(key=key, value=value):
                self._read_history()
                self._change_record(lambda record: record.pop(key, None) if value is None
                                    else record.update({key: value}))
                self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), outcome)

    def test_wrong_revision_repository_and_shallow_provenance_are_rejected(self):
        for key, value in (("repo_name", "another/repo"), ("head_sha", self.plain)):
            with self.subTest(key=key):
                self._read_history()
                self._change_record(lambda record: record.update({key: value}))
                with self.assertRaises(ValueError):
                    commits.reclassify(self.commits_dir, self.REPO)
        self._read_history()
        self._change_record(lambda record: record["commit_evidence"].update({"shallow": True}))
        with self.assertRaises(ValueError):
            commits.reclassify(self.commits_dir, self.REPO)

    def test_count_and_required_path_or_ref_commits_are_checked_before_replacement(self):
        for change in (
                lambda record: record["commit_evidence"].update({"commit_count": 99}),
                lambda record: record["path_commits"][0]["commits"].append("a" * 40),
                lambda record: record["commit_evidence"]["refs"].update(
                    {"refs/heads/missing": {"object_sha": "b" * 40, "commit_sha": "b" * 40}})):
            for current in (True, False):
                with self.subTest(change=change, current=current):
                    self._read_history()
                    csv_path, json_path = commits.shard_paths(self.commits_dir, self.REPO)
                    other = commits.other_shard_path(self.commits_dir, self.REPO)
                    self._change_record(change)
                    if not current:
                        self._change_record(lambda record: record.update({"classified_by": "old"}))
                    before = [path.read_bytes() for path in (csv_path, other, json_path)]
                    with self.assertRaises(ValueError):
                        commits.reclassify(self.commits_dir, self.REPO)
                    self.assertEqual([path.read_bytes() for path in (csv_path, other, json_path)], before)
                    # The reclassification writes beside the shards and renames at the end.
                    # A rejection at the end of the log must leave no partial file behind.
                    self.assertEqual(sorted(p.name for p in self.commits_dir.rglob("*.tmp")), [])

    def test_a_shard_that_differs_from_its_record_is_written_again_from_the_commit_log(self):
        """A power loss can leave a shard empty or cut short after the per-repository commit
        record reached the disk. The shards are derived from the commit log that the record
        validates. The reclassification therefore writes the shards again from that log rather
        than failing for the repository at every reclassification until a new collection."""
        self._read_history()
        csv_path, _ = commits.shard_paths(self.commits_dir, self.REPO)
        other = commits.other_shard_path(self.commits_dir, self.REPO)
        originals = [csv_path.read_bytes(), other.read_bytes()]
        for name, damage in (
                ("changed", lambda: csv_path.write_text(
                    csv_path.read_text().replace("Claude", "Copilot"))),
                ("empty", lambda: other.write_bytes(b"")),
                ("missing", csv_path.unlink)):
            with self.subTest(damage=name):
                damage()
                self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "reclassified")
                self.assertEqual([csv_path.read_bytes(), other.read_bytes()], originals)
                self.assertEqual(commits.reclassify(self.commits_dir, self.REPO), "current")

    def test_the_shards_and_the_log_reach_the_disk_before_the_record(self):
        """The per-repository commit record gives the checksums of the shards and the log. A
        record that reached the disk before these files would describe files that a power loss
        left empty."""
        synced, real = [], os.fsync

        def recorded(fd):
            synced.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            return real(fd)

        with mock.patch.object(os, "fsync", recorded):
            self._read_history()
        csv_path, json_path = commits.shard_paths(self.commits_dir, self.REPO)
        files = {path.name: (path.stat().st_dev, path.stat().st_ino)
                 for path in (csv_path, commits.other_shard_path(self.commits_dir, self.REPO),
                              commits.stored_log_path(self.commits_dir, self.REPO), json_path)}
        record_at = synced.index(files.pop(json_path.name))
        for name, inode in files.items():
            with self.subTest(file=name):
                self.assertIn(inode, synced[:record_at])

    def test_current_configuration_authorship_must_agree_with_evidence(self):
        self._read_history()
        self._change_record(lambda record: record["ai_rows"][0].update({"#ai_commits": "0"}))
        with self.assertRaisesRegex(ValueError, "authorship"):
            commits.reclassify(self.commits_dir, self.REPO)


class TestTheRevisionsOfTheCommitLog(_HistoryRead):
    def test_the_revisions_reach_git_on_its_standard_input(self):
        """Passed on the command line, the commits of tens of thousands of tags exceed the
        system's limit on the size of the arguments. `Popen` would then fail with E2BIG on every
        run, as if the machine had failed."""
        _git(self.clone, "checkout", "-q", "-b", "side")
        _commit(self.clone, "NOTES.md", "notes\n", "docs: notes")
        _git(self.clone, "tag", "v1")
        _git(self.clone, "checkout", "-q", "main")
        _git(self.clone, "branch", "-q", "-D", "side")
        commands, real = [], commits.bounds.run_bounded

        def recorded(cmd, **kwargs):
            commands.append(cmd)
            return real(cmd, **kwargs)

        with mock.patch.object(commits.bounds, "run_bounded", recorded):
            record = commits.read_commit_history(self.REPO, self.head, "main", self.clone,
                                                 self.commits_dir)
        log = next(cmd for cmd in commands if "log" in cmd)
        self.assertIn("--stdin", log)
        self.assertEqual([arg for arg in log if len(arg) == 40 and set(arg) <= set("0123456789abcdef")], [])
        # The three commits of main and the commit that only the tag reaches.
        self.assertEqual(record["commits_read"], 4)


class TestTheCountOfARenamedFile(_HistoryRead):
    """`#commits` counts every commit that touched a file under any of its names. The record
    keeps these commits by hash, and the reclassification therefore corrects, without a clone,
    a collection that counted the commits of the current name only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.clone = self.tmp / "clone"
        self.clone.mkdir()
        _git(self.clone, "init", "-q", "-b", "main")
        self.plain = _commit(self.clone, "README.md", "x\n", "docs: readme")
        self.created = _commit(self.clone, "OLD.md", "guidance\n" + "x" * 80 + "\n",
                               "docs: guidance")
        _git(self.clone, "mv", "OLD.md", "CLAUDE.md")
        self.renamed = _commit(self.clone, "CLAUDE.md", "guidance\n" + "x" * 80 + "\n",
                               "docs: rename the guidance")
        self.edited = _commit(self.clone, "CLAUDE.md", "guidance\n" + "x" * 80 + "\nmore\n",
                              "docs: more guidance")
        self.commits_dir = self.tmp / "collect_commits"
        self.head = _git(self.clone, "rev-parse", "HEAD")

    def test_the_reclassification_counts_the_commits_under_every_name(self):
        record = self._read_history()
        self.assertEqual(sorted(record["path_commits"][0]["commits"]),
                         sorted([self.created, self.renamed, self.edited]))
        TestTheStep._stage_files(self)
        key = caches.FileHistoryKey(self.REPO, self.head, "", "CLAUDE.md")
        other = caches.FileHistoryKey(self.REPO, self.head, "", "README.md")
        history_cache = caches.FileHistoryCache(path=self.tmp / "file_history.csv")
        # What the collection measured when it counted the current name only: the rename and
        # the edit.
        history_cache.put(key, caches.FileHistory("2026-01-01T00:00:00Z", "2", self.created,
                                                  self.edited))
        history_cache.put(other, caches.FileHistory("2026-01-01T00:00:00Z", "1", self.plain,
                                                    self.plain))
        history_cache.save()
        code, err = TestTheStep._main(self)
        self.assertEqual(code, 0, err)
        after = caches.FileHistoryCache.load(self.tmp / "file_history.csv")
        self.assertEqual(after.get(key).commits, "3")
        self.assertEqual(after.get(key).created_at, "2026-01-01T00:00:00Z")
        self.assertEqual(after.get(other).commits, "1")


class TestTheStep(_HistoryRead):

    def _stage_files(self, tree: bool = True):
        """Write the collected list, the list of finished repositories and a clone_failed.csv
        without rows as the cloning step leaves them. With `tree`, also write the repository's
        collected tree with the one file that the cloning step recorded."""
        (self.tmp / "data" / "collected").mkdir(parents=True)
        if tree:
            (self.tmp / "data" / "collected" / "owner§repo").mkdir()
            (self.tmp / "data" / "collected" / "owner§repo" / "CLAUDE.md").write_text("rules, more\n")
        main_row = paths.build_csv_row(self.REPO, "2026-09-28T00:00:00Z", "main", self.head,
                                       "2026-09-28T00:00:00Z", "", set())
        (self.tmp / "data" / "collected" / "repos.csv").write_text(
            paths.COLLECTED_CSV_HEADER + "\n" + main_row + "\n", encoding="utf-8")
        (self.tmp / "finished.jsonl").write_text(
            json.dumps({"repo": self.REPO, "row": main_row, "fail_row": None,
                        "measures": {"copied": 1}}) + "\n",
            encoding="utf-8")
        (self.tmp / "data/collected/clone_failed.csv").write_text("repo_name,repo_url,reason\n")

    def _main(self) -> tuple:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _mod.main([
                "--commits-dir", str(self.commits_dir),
                "--finished", str(self.tmp / "finished.jsonl"),
                "--collected-csv", str(self.tmp / "data" / "collected" / "repos.csv"),
                "--fail-csv", str(self.tmp / "data" / "collected" / "clone_failed.csv"),
                "--ai-commits-csv", str(self.tmp / "output" / "all_ai_commits.csv"),
                "--ai-authorship", str(self.tmp / "ai_authorship.csv"),
                "--file-history", str(self.tmp / "file_history.csv"),
                "--workers", "2"])
        return code, err.getvalue()

    def test_missing_september_reclassification_inputs_cannot_report_success(self):
        self._read_history()
        record = self._record()
        for key in ("classified_by", "path_commits"):
            record.pop(key, None)
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)
        commits.stored_log_path(self.commits_dir, self.REPO).unlink()
        self._stage_files()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["counts"], report_counts(no_log=1))
        self.assertEqual(report["repositories"][self.REPO]["outcome"], "no_log")
        # A record that names no heuristics cannot show that the current heuristics
        # classified its shard. The shard stays out of the commit CSV.
        self.assertTrue(report["repositories"][self.REPO]["left_out"])
        with (self.tmp / "output/all_ai_commits.csv").open() as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def _assemble_as_the_cloning_step(self) -> None:
        """Assemble the shards as the cloning step does at the end of its run, which puts the
        rows and AI rows of every shard where this step reads them."""
        finished = resume.read_finished(self.tmp / "finished.jsonl")
        commits.assemble_ai_commits(self.commits_dir, finished,
                                    self.tmp / "output" / "all_ai_commits.csv",
                                    self.tmp / "ai_authorship.csv", repos=list(finished))

    def _ai_rows(self) -> list:
        with (self.tmp / "ai_authorship.csv").open(newline="", encoding="utf-8") as stream:
            return [row for row in csv.DictReader(stream) if row["repo_name"] == self.REPO]

    def _outcome_in_repos_csv(self) -> str:
        with (self.tmp / "output" / "ai_commits-repos.csv").open(newline="") as stream:
            return {row["repo_name"]: row["outcome"] for row in csv.DictReader(stream)}[self.REPO]

    def test_a_truncated_log_is_failed_and_its_rows_stay_out(self):
        """A log that fails validation gives the repository the outcome `failed` and does not
        fail the step. The step assembles the other shards and, within the ceiling, exits 0
        with an incomplete reclassification, so that the steps after it run. Although the
        current heuristics classified the shard, the evidence does not support it, and the
        step publishes neither its commits nor its AI rows."""
        self._read_history()
        log = commits.stored_log_path(self.commits_dir, self.REPO)
        log.write_bytes(log.read_bytes()[:40])
        self._stage_files()
        self._assemble_as_the_cloning_step()
        self.assertTrue(self._ai_rows())
        code, err = self._main()
        self.assertEqual(code, 0, err)
        self.assertIn("owner/repo", err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["repositories"][self.REPO]["outcome"], "failed")
        self.assertIn("error", report["repositories"][self.REPO])
        self.assertTrue(report["repositories"][self.REPO]["left_out"])
        self.assertEqual((report["complete"], report["assembly_complete"]), (False, True))
        with (self.tmp / "output" / "all_ai_commits.csv").open() as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])
        self.assertEqual(self._ai_rows(), [])
        self.assertEqual(self._outcome_in_repos_csv(), "history_read",
                         "the cloning step read the commit history")
        self.assertTrue(commits.shard_paths(self.commits_dir, self.REPO)[0].is_file(),
                        "the shard stays on disk")

    def test_a_repository_left_out_counts_no_commit_of_the_commit_csvs(self):
        """For a repository with shards that stay out of the commit CSVs,
        `ai_commits-repos.csv` counts no matched commit. With the count of matched commits from
        the per-repository commit record, a reader of the counts, such as the detection accuracy
        evaluation, would find commits that no published row has. The count of the commits that
        the cloning step read stays."""
        read = self._read_history()
        self.assertGreater(read["commits_matched"], 0)
        log = commits.stored_log_path(self.commits_dir, self.REPO)
        log.write_bytes(log.read_bytes()[:40])
        self._stage_files()
        self._assemble_as_the_cloning_step()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        with (self.tmp / "output" / "ai_commits-repos.csv").open(newline="") as stream:
            row = {r["repo_name"]: r for r in csv.DictReader(stream)}[self.REPO]
        self.assertEqual((row["outcome"], row["commits_read"], row["commits_matched"],
                          row["commits_other_tools"]),
                         ("history_read", str(read["commits_read"]), "0", "0"))

    def test_a_current_record_with_a_failed_log_is_collected_again_by_the_next_collection(self):
        """The cloning step classifies a taken-over repository again only when other commit
        heuristics classified it (`reuse.reclassify`). The per-repository commit record of a
        truncated log names the current heuristics. The cloning step therefore reads the
        report, and it collects the repository again as long as the report names the
        repository's current record as `failed`."""
        self._read_history()
        log = commits.stored_log_path(self.commits_dir, self.REPO)
        log.write_bytes(log.read_bytes()[:40])
        self._stage_files()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        config = CollectionConfig(
            collected_dir=self.tmp / "data" / "collected", clone_dir=self.tmp / "clones",
            log_dir=self.tmp / "logs", commits_dir=self.commits_dir,
            reclassification_report=self.tmp / "output" / resume.RECLASSIFICATION_REPORT_NAME)
        done = resume.read_finished(self.tmp / "finished.jsonl")
        self.assertEqual(repository_cloning.to_retry(config, [self.REPO], done, "the next one"),
                         {self.REPO})
        # A new collection of the repository writes another record, which the report does not
        # name.
        record = commits.shard_paths(self.commits_dir, self.REPO)[1]
        record.write_text(json.dumps(dict(self._record(), retried=True)), encoding="utf-8")
        self.assertEqual(repository_cloning.to_retry(config, [self.REPO], done, "the next one"),
                         set())

    def test_a_record_that_does_not_parse_is_left_out_and_collected_again(self):
        """A per-repository commit record that a power loss left empty makes the
        reclassification of its repository fail. Without `left_out` and the SHA-256 of the
        record's bytes, the cloning step would pass over the repository, and the repository
        would stay `failed` in every later collection, outside the count of repositories left
        out."""
        self._read_history()
        record = commits.shard_paths(self.commits_dir, self.REPO)[1]
        record.write_bytes(b"")
        self._stage_files()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        result = report["repositories"][self.REPO]
        self.assertEqual((result["outcome"], result.get("left_out"), result.get("record_sha256")),
                         ("failed", True, hashlib.sha256(b"").hexdigest()))
        self.assertEqual(self._outcome_in_repos_csv(), "shard_unreadable")
        config = CollectionConfig(
            collected_dir=self.tmp / "data" / "collected", clone_dir=self.tmp / "clones",
            log_dir=self.tmp / "logs", commits_dir=self.commits_dir,
            reclassification_report=self.tmp / "output" / resume.RECLASSIFICATION_REPORT_NAME)
        self.assertEqual(resume.reclassification_failed(config.reclassification_report,
                                                        self.commits_dir), {self.REPO})
        done = resume.read_finished(self.tmp / "finished.jsonl")
        self.assertEqual(repository_cloning.to_retry(config, [self.REPO], done, "the next one"),
                         {self.REPO})

    def test_a_record_that_does_not_parse_leaves_no_ai_row_of_its_earlier_record(self):
        """The cloning step's assembly puts the AI rows of the readable record into
        ai_authorship.csv, and a power loss then leaves the record empty. The step leaves the
        repository's commits out. If the rows of the earlier record stayed in the file,
        context-files.csv would publish AI columns for a repository without published commits.
        The rows of another repository stay."""
        self._read_history()
        self._stage_files()
        self._assemble_as_the_cloning_step()
        self.assertTrue(self._ai_rows())
        other = caches.AiAuthorshipCache.load(self.tmp / "ai_authorship.csv")
        other_key = caches.AiAuthorshipKey("owner/other", "c" * 40, "AGENTS.md")
        other.put(other_key, caches.AiAuthorship("TRUE", "claude", "1", "claude"))
        other.save()
        commits.shard_paths(self.commits_dir, self.REPO)[1].write_bytes(b"")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertTrue(report["repositories"][self.REPO]["left_out"])
        self.assertEqual(self._ai_rows(), [])
        self.assertIn(other_key, caches.AiAuthorshipCache.load(self.tmp / "ai_authorship.csv"))

    def test_a_directory_that_synology_dsm_wrote_into_the_tree_is_no_missing_history(self):
        """DSM writes `@eaDir/<name>@SynoEAStream` beside a file of a directory that File Station
        visits on the NAS. No per-repository commit record names such a file. The record stays
        `current`, and its repository does not join the incomplete repositories, which above the
        ceiling would make the step exit 2 on every Start."""
        self._read_history()
        self._stage_files()
        tree = self.tmp / "data/collected/owner§repo"
        (tree / "@eaDir").mkdir()
        (tree / "@eaDir" / "CLAUDE.md@SynoEAStream").write_bytes(b"\0")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["counts"], report_counts(current=1))
        self.assertTrue(report["complete"])

    def test_a_taken_over_repository_with_a_directory_that_dsm_wrote_is_classified_again(self):
        """When the cloning step takes over a repository that other commit heuristics
        classified, it classifies the repository's commits again and compares the record with
        the tree as this step does. A file below `@eaDir` must not make the comparison fail,
        because the cloning step would then clone the repository again."""
        record = self._read_history()
        record["classified_by"] = "earlier heuristics"
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)
        self._stage_files()
        tree = self.tmp / "data/collected/owner§repo"
        (tree / ".claude" / "@eaDir").mkdir(parents=True)
        (tree / ".claude" / "@eaDir" / "settings.json@SynoEAStream").write_bytes(b"\0")
        config = CollectionConfig(collected_dir=self.tmp / "data" / "collected",
                                  clone_dir=self.tmp / "clones", log_dir=self.tmp / "logs",
                                  commits_dir=self.commits_dir)
        done = resume.read_finished(self.tmp / "finished.jsonl")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(reuse.reclassify(config, [self.REPO], done, 1), set())
        self.assertEqual(self._record()["classified_by"], commits.CLASSIFIED_BY)

    def test_a_stale_shard_that_cannot_be_classified_again_stays_out(self):
        """Last month's heuristics classified the shard, and one of its rows names a heuristic
        id that the current heuristics no longer have. The repository has a failed file history
        query, which keeps the step from classifying the shard again. If the step assembled the
        row, the row would stop the run at the commit attribution validation, after the
        detection step had rebuilt the commit CSV from the shards. The shard stays on disk and
        out of the commit CSVs, the AI cache and the detection step's CSV, and the
        reclassification is incomplete."""
        self._read_history()
        csv_path, json_path = commits.shard_paths(self.commits_dir, self.REPO)
        rows = list(commits.shard_rows(csv_path))
        self.assertTrue(rows)
        with csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(commits.COMMIT_COLUMNS),
                                    lineterminator="\n")
            writer.writeheader()
            writer.writerows(dict(row, matched_heuristics="a-removed-heuristic") for row in rows)
        commits.write_shard_record(json_path, dict(self._record(), classified_by="an older image",
                                                   history_failed=["CLAUDE.md"]))
        self._stage_files()
        self._assemble_as_the_cloning_step()
        output = self.tmp / "output"
        validate = ["-i", str(output / "all_ai_commits.csv"),
                    "--other-input", str(output / "other_ai_commits.csv"), "--summary-only"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(commit_validation.main(validate), 2, "the stale row is assembled")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((output / "commit-reclassification.json").read_text())
        result = report["repositories"][self.REPO]
        self.assertEqual(result["outcome"], "history_incomplete")
        self.assertTrue(result["left_out"])
        self.assertFalse(report["complete"])
        self.assertEqual(self._ai_rows(), [])
        self.assertEqual(self._outcome_in_repos_csv(), "history_read")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(commit_validation.main(validate), 0)
            # The detection step rebuilds both commit CSVs from the shards, after this step.
            detection.restrict_ai_commits(output / "all_ai_commits.csv", {self.REPO},
                                          self.commits_dir)
            detection.restrict_ai_commits(output / "other_ai_commits.csv", {self.REPO},
                                          self.commits_dir, other_tools=True)
            self.assertEqual(commit_validation.main(validate), 0)
        with (output / "all_ai_commits.csv").open() as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])
        self.assertEqual([row["matched_heuristics"] for row in commits.shard_rows(csv_path)],
                         ["a-removed-heuristic"] * len(rows), "the shard stays on disk")
        # A new collection of the repository writes another record, and its shards count
        # again.
        commits.write_shard_record(json_path, dict(self._record(), history_failed=[],
                                                   classified_by=commits.CLASSIFIED_BY))
        with contextlib.redirect_stderr(io.StringIO()):
            detection.restrict_ai_commits(output / "all_ai_commits.csv", {self.REPO},
                                          self.commits_dir)
        with (output / "all_ai_commits.csv").open() as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), len(rows))

    def test_failed_clones_keep_outcomes_and_stray_shards_are_excluded(self):
        self._read_history()
        self._stage_files()
        failure = "gone/repo,https://github.com/gone/repo,not_found_or_no_access"
        (self.tmp / "data/collected/clone_failed.csv").write_text(
            "repo_name,repo_url,reason\n" + failure + "\n")
        with (self.tmp / "finished.jsonl").open("a") as stream:
            stream.write(json.dumps({"repo": "gone/repo", "fail_row": failure,
                "row": paths.build_csv_row("gone/repo", "2026-09-28T00:00:00Z",
                                           "", "", "", "", set())}) + "\n")
        for src, dst in zip(commits.shard_paths(self.commits_dir, self.REPO),
                            commits.shard_paths(self.commits_dir, "old/dropped")):
            dst.write_text(src.read_text().replace("owner/repo", "old/dropped"))
        code, err = self._main()
        self.assertEqual(code, 0, err)
        with (self.tmp / "output/ai_commits-repos.csv").open() as stream:
            self.assertEqual({row["repo_name"]: row["outcome"] for row in csv.DictReader(stream)},
                             {"owner/repo": "history_read", "gone/repo": "not_found_or_no_access"})
        with (self.tmp / "output/all_ai_commits.csv").open() as stream:
            self.assertEqual({row["repo_name"] for row in csv.DictReader(stream)}, {"owner/repo"})

    def test_a_failed_clone_is_unavailable_whatever_an_older_record_states(self):
        # The cloning step removes the record of a repository when its clone fails. A record
        # beside a failure row therefore belongs to an earlier clone. If the step classified
        # the record, the history would count as read in this collection, and the population
        # would be complete or incomplete by a rule that the outcome counts do not show.
        self._read_history()
        self._stage_files()
        failure = f"{self.REPO},https://github.com/{self.REPO},clone_failed"
        (self.tmp / "data/collected/repos.csv").write_text(
            paths.COLLECTED_CSV_HEADER + "\n" + paths.build_csv_row(
                self.REPO, "2026-09-28T00:00:00Z", "", "", "", "", set()) + "\n")
        (self.tmp / "data/collected/clone_failed.csv").write_text(
            "repo_name,repo_url,reason\n" + failure + "\n")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["repositories"][self.REPO],
                         {"outcome": "unavailable", "reason": "clone_failed"})
        self.assertEqual((report["complete"], report["population_complete"]), (True, False))
        # Nor do its commits reach the commit CSV, which would credit them to this collection.
        with (self.tmp / "output/all_ai_commits.csv").open() as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def test_a_stale_collection_is_classified_again_and_assembled(self):
        self._read_history(detect=lambda *fields, **kwargs: [])
        record = dict(self._record(), classified_by="an older image")
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)
        shutil.rmtree(self.clone)
        self._stage_files()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        self.assertRegex(err, r"Reclassified 1 of \d+ per-repository commit records under the "
                              r"current heuristics\.")
        with open(self.tmp / "output" / "all_ai_commits.csv", newline="", encoding="utf-8") as f:
            self.assertIn(self.trailer, [row["commit_sha"] for row in csv.DictReader(f)])
        with open(self.tmp / "ai_authorship.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["artifact"], r["first_commit_ai_created"]) for r in rows],
                         [("CLAUDE.md", "TRUE")])

    def test_right_after_the_cloning_step_nothing_is_classified_twice(self):
        self._read_history()
        self._stage_files()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        self.assertRegex(err, r"Reclassified 0 of \d+ per-repository commit records under the "
                              r"current heuristics\. 1 were already classified under them,")

    def test_missing_record_for_a_collected_tree_is_reported(self):
        self._read_history()
        self._stage_files()
        commits.shard_paths(self.commits_dir, self.REPO)[1].unlink()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["requested_repositories"], 1)
        self.assertEqual(report["repositories"][self.REPO]["outcome"], "missing_record")

    def test_history_fetch_failure_is_incomplete(self):
        """A history that the cloning step reads again on the next run, here a history with
        no recorded cause, counts toward the ceiling. One repository in a sample of one does
        not exceed the ceiling. The cloning step exits 0 with it, and so does this step, which
        records the reclassification as incomplete."""
        self._read_history()
        self._stage_files()
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1],
            commits.unavailable_record(self.REPO, self.head, "main", "history_unavailable"))
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["counts"], report_counts(history_not_read=1))
        self.assertEqual((report["bounded_histories"], report["left_to_retry"],
                          report["retry_ceiling"]), (0, 1, 1))

    def _add_repository(self, repo: str, record: dict) -> None:
        """Add a repository with one collected file and `record` as its per-repository commit
        record to the collected list and the list of finished repositories that `_stage_files`
        wrote."""
        tree = self.tmp / "data" / "collected" / repo.replace("/", "§")
        tree.mkdir(parents=True)
        (tree / "CLAUDE.md").write_text("rules\n")
        row = paths.build_csv_row(repo, "2026-09-28T00:00:00Z", "main", self.head,
                                  "2026-09-28T00:00:00Z", "", set())
        with (self.tmp / "data" / "collected" / "repos.csv").open("a", encoding="utf-8") as f:
            f.write(row + "\n")
        with (self.tmp / "finished.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"repo": repo, "row": row, "fail_row": None,
                                "measures": {"copied": 1}}) + "\n")
        commits.write_shard_record(commits.shard_paths(self.commits_dir, repo)[1], record)

    def _unread(self, repo: str, cause: str) -> dict:
        """Return the per-repository commit record of a history fetch that failed with
        `cause`, as the cloning step writes it."""
        record = commits.unavailable_record(repo, self.head, "main", "history_unavailable")
        record.update(cause=cause, transient=cause == "exit", time_bound=3600,
                      size_bound=32 * 1024 ** 3)
        return record

    def test_histories_that_the_bounds_stopped_are_accepted_beyond_the_ceiling(self):
        """The cloning step does not fetch again a history that the time bound or the size
        bound stopped, and it does not count that history toward its ceiling. A monorepo over
        a bound therefore cannot stop every run at this step. The reclassification stays
        incomplete."""
        self._read_history()
        self._stage_files()
        self._add_repository("owner/monorepo", self._unread("owner/monorepo", "time_bound"))
        self._add_repository("owner/large", self._unread("owner/large", "size_bound"))
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["counts"], report_counts(current=1, history_not_read=2))
        self.assertEqual((report["complete"], report["assembly_complete"]), (False, True))
        self.assertEqual((report["bounded_histories"], report["left_to_retry"],
                          report["retry_ceiling"]), (2, 0, 1))
        self.assertTrue(report["repositories"]["owner/monorepo"]["bounded"])

    def test_more_repositories_to_retry_than_the_ceiling_exit_2(self):
        """A later collection retries the histories that a network failure kept the cloning
        step from reading, and these histories count toward the ceiling. Above the ceiling, the
        step exits 2, as the cloning step exits 4, after it has assembled the CSVs and recorded
        the report."""
        self._read_history()
        self._stage_files()
        self._add_repository("owner/one", self._unread("owner/one", "exit"))
        self._add_repository("owner/two", self._unread("owner/two", "exit"))
        code, err = self._main()
        self.assertEqual(code, 2, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual((report["bounded_histories"], report["left_to_retry"],
                          report["retry_ceiling"]), (0, 2, 1))
        self.assertEqual((report["complete"], report["assembly_complete"]), (False, True))
        self.assertIn("may leave to retry (exit code 2)", err)

    def test_path_evidence_must_cover_the_collected_tree(self):
        self._read_history()
        self._stage_files()
        tree = self.tmp / "data/collected/owner§repo"
        (tree / "AGENTS.md").write_text("Additional instructions")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["counts"], report_counts(no_path_history=1))

    def test_a_recorded_finder_file_the_handover_left_out_is_not_missing_history(self):
        """A per-repository commit record can name a `.cursor/.DS_Store`, a file that a
        handover leaves out and no step reads. The step checks a `.DS_Store` against neither
        side and still checks every other path."""
        _commit(self.clone, ".cursor/.DS_Store", "Bud1\n", "Add Finder metadata")
        self.head = _git(self.clone, "rev-parse", "HEAD")
        record = self._read_history()
        path_history = history.path_commits(_run, str(self.clone), ".cursor/.DS_Store")
        record["path_commits"].append(commits.path_commits_entry(".cursor/.DS_Store", path_history))
        record["ai_rows"].append(dict(
            commits.caches.AiAuthorshipKey(self.REPO, self.head, ".cursor/.DS_Store").as_row(),
            **commits.ai_authorship_from(path_history).as_row()))
        record["collected_artifacts"].append(".cursor/.DS_Store")
        commits.write_shard_record(commits.shard_paths(self.commits_dir, self.REPO)[1], record)
        self._stage_files()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"], report_counts(current=1))
        (self.tmp / "data/collected/owner§repo/CLAUDE.md").unlink()
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["counts"], report_counts(no_path_history=1))

    def test_a_record_whose_collected_tree_is_missing_is_incomplete(self):
        # A repository with a commit history that was read and a lost tree has nothing left
        # to check its path records against. Its reclassification is not complete.
        self._read_history()
        self._stage_files()
        shutil.rmtree(self.tmp / "data/collected/owner§repo")
        csv_path, json_path = commits.shard_paths(self.commits_dir, self.REPO)
        before = [path.read_bytes() for path in (csv_path, json_path)]
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["repositories"][self.REPO], {"outcome": "missing_tree"})
        self.assertEqual((report["complete"], report["population_complete"]), (False, False))
        self.assertIn("owner§repo", err)
        self.assertEqual([path.read_bytes() for path in (csv_path, json_path)], before)

    def test_current_log_loss_is_incomplete(self):
        self._read_history()
        self._stage_files()
        commits.stored_log_path(self.commits_dir, self.REPO).unlink()
        code, err = self._main()
        self.assertEqual(code, 0, err)

    def test_collection_revision_mismatch_is_failed(self):
        self._read_history()
        self._stage_files()
        path = self.tmp / "data/collected/repos.csv"
        path.write_text(path.read_text().replace(self.head, self.plain))
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertFalse(report["complete"])
        self.assertEqual(report["repositories"][self.REPO]["outcome"], "failed")

    def test_no_files_is_explicitly_outside_the_required_histories(self):
        self._read_history()
        self._stage_files(tree=False)
        commits.remove_shards(self.commits_dir, self.REPO)
        finished = self.tmp / "finished.jsonl"
        finished.write_text(finished.read_text().replace('"copied": 1', '"copied": 0'))
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertTrue(report["complete"])
        self.assertEqual(report["counts"], report_counts(not_applicable=1))

    def test_empty_configuration_gitlink_requires_no_history(self):
        self._read_history()
        self._stage_files(tree=False)
        commits.remove_shards(self.commits_dir, self.REPO)
        finished = self.tmp / "finished.jsonl"
        finished.write_text(finished.read_text().replace('"copied": 1', '"copied": 0'))
        path = self.tmp / "data/collected/repos.csv"
        with path.open(newline="") as stream:
            rows = list(csv.DictReader(stream))
        rows[0]["claude"] = "TRUE"
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        (path.parent / "owner§repo/.claude/worktrees/empty").mkdir(parents=True)
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["counts"], report_counts(not_applicable=1))

    def test_zero_copy_count_cannot_hide_a_collected_file(self):
        self._read_history()
        self._stage_files(tree=False)
        commits.remove_shards(self.commits_dir, self.REPO)
        finished = self.tmp / "finished.jsonl"
        finished.write_text(finished.read_text().replace('"copied": 1', '"copied": 0'))
        tree = self.tmp / "data/collected/owner§repo"
        tree.mkdir(parents=True)
        (tree / "AGENTS.md").write_text("Instructions")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["counts"], report_counts(missing_record=1))

    def test_missing_failure_list_cannot_establish_coverage(self):
        self._read_history()
        self._stage_files()
        (self.tmp / "data/collected/clone_failed.csv").unlink()
        code, err = self._main()
        self.assertEqual(code, 1, err)

    def test_absent_copy_measurement_cannot_establish_no_files(self):
        self._read_history()
        self._stage_files()
        commits.remove_shards(self.commits_dir, self.REPO)
        finished = self.tmp / "finished.jsonl"
        record = json.loads(finished.read_text())
        record.pop("measures")
        finished.write_text(json.dumps(record) + "\n")
        code, err = self._main()
        self.assertEqual(code, 0, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["counts"], report_counts(missing_record=1))

    def test_assembly_failure_cannot_leave_a_complete_report(self):
        self._read_history()
        self._stage_files()
        def failed_assembly(*args, **kwargs):
            pending = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
            self.assertFalse(pending["complete"])
            self.assertFalse(pending["population_complete"])
            self.assertFalse(pending["assembly_complete"])
            raise OSError("assembly failed")
        with mock.patch.object(commits, "assemble_ai_commits", side_effect=failed_assembly):
            code, err = self._main()
        self.assertEqual(code, 1, err)
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertFalse(report["complete"])
        self.assertIn("assembly failed", report["assembly_error"])

    def test_no_collection_is_one(self):
        code, err = self._main()
        self.assertEqual(code, 1)
        self.assertIn("ERROR commit_reclassification:", err)


class TestARepositoryWithoutFilesAfterAHeadQuery(unittest.TestCase):
    """When git returns no head commit for a repository that the cloning step takes over, the
    step writes the repository's line without the count of copied files. A short outage during
    the head queries leaves many such lines."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        saved = {}
        _ambient_env.take(saved)
        self.addCleanup(_ambient_env.restore, saved)
        for key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.addCleanup(os.environ.pop, key, None)

    def test_a_repository_without_files_whose_head_git_did_not_return_is_not_applicable(self):
        remote = self.tmp / "remote"
        _init_git_repo(remote)
        _add_commit(remote, "README.md", "# A project without configuration")
        os.environ.update({"GIT_CONFIG_COUNT": "1",
                           "GIT_CONFIG_KEY_0": f"url.file://{remote}.insteadOf",
                           "GIT_CONFIG_VALUE_0": "https://github.com/owner/repo.git"})
        collected = self.tmp / "data" / "collected"
        cfg = CollectionConfig(collected_dir=collected, clone_dir=self.tmp / "scratch",
                               log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
                               commits_dir=self.tmp / "collect_commits")
        finished = self.tmp / "finished.jsonl"
        with contextlib.redirect_stderr(io.StringIO()):
            run_collection(cfg, ["owner/repo"], workers=1, finished_path=finished)
        self.assertEqual(json.loads(finished.read_text().splitlines()[-1])["measures"]["copied"], 0)
        os.environ["GIT_CONFIG_KEY_0"] = "url.http://127.0.0.1:9/unavailable.insteadOf"
        with contextlib.redirect_stderr(io.StringIO()):
            main_rows, fail_rows, _, _ = run_collection(cfg, ["owner/repo"], workers=1,
                                                        finished_path=finished)
        last = json.loads(finished.read_text().splitlines()[-1])
        self.assertTrue(last.get("head_no_response"))
        self.assertNotIn("measures", last)
        repository_cloning._write_csv(collected / "repos.csv", paths.COLLECTED_CSV_HEADER,
                                      main_rows)
        repository_cloning._write_csv(collected / "clone_failed.csv",
                                      repository_cloning.FAIL_CSV_HEADER, fail_rows)
        cfg.commits_dir.mkdir(exist_ok=True)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _mod.main([
                "--commits-dir", str(cfg.commits_dir), "--finished", str(finished),
                "--collected-csv", str(collected / "repos.csv"),
                "--fail-csv", str(collected / "clone_failed.csv"),
                "--ai-commits-csv", str(self.tmp / "output" / "all_ai_commits.csv"),
                "--ai-authorship", str(self.tmp / "ai_authorship.csv"),
                "--file-history", str(self.tmp / "file_history.csv"), "--workers", "1"])
        self.assertEqual(code, 0, err.getvalue())
        report = json.loads((self.tmp / "output/commit-reclassification.json").read_text())
        self.assertEqual(report["repositories"]["owner/repo"],
                         {"outcome": "not_applicable", "reason": "no_files"})
        self.assertTrue(report["complete"])


class TestCommitLogRecordsAcrossBufferBoundaries(unittest.TestCase):
    def test_nul_logs_preserve_complete_messages_across_buffers(self):
        hashes = [f"{n:040x}" for n in range(3000)]
        body = "x" * 1000 + "\x1e\x1f\n"
        message = "subject\n\n" + body
        raw = b"".join(b"\0" + "\0".join([
            sha, "Author", "a@example.com", "Committer", "c@example.com",
            "2026-09-28T00:00:00Z", "subject", body, message, ""]).encode() + b"\n"
            for sha in hashes)
        self.assertGreater(len(raw), 2 * (1 << 20))
        for stream in (io.BytesIO(raw), lzma.open(io.BytesIO(lzma.compress(raw)), "rb")):
            with stream:
                rows = list(commits._records_from(stream, "nul-v1"))
            self.assertEqual([row["hash"] for row in rows], hashes)
            self.assertEqual({row["body"] for row in rows}, {body})
            self.assertEqual({row["message"] for row in rows}, {message})

    def test_plain_and_compressed_logs_preserve_every_record_across_buffers(self):
        hashes = [f"{n:040x}" for n in range(3000)]
        body = "x" * 1000
        raw = b"".join(b"\x1e" + "\x1f".join([
            sha, "A Person", "a@example.com", "A Person", "a@example.com",
            "2026-09-28T00:00:00Z", "subject", body, ""]).encode() for sha in hashes)
        self.assertGreater(len(raw), 2 * (1 << 20))
        streams = (io.BytesIO(raw), lzma.open(io.BytesIO(lzma.compress(raw)), "rb"))
        for stream in streams:
            with stream:
                rows = list(commits._records_from(stream))
            self.assertEqual(len(rows), 3000)
            self.assertEqual([row["hash"] for row in rows], hashes)
            self.assertEqual({row["body"] for row in rows}, {body})
            self.assertEqual({row["subject"] for row in rows}, {"subject"})


class TestOutcomeCounts(unittest.TestCase):
    def test_an_outcome_that_no_repository_has_counts_zero(self):
        counts = _mod.outcome_counts(["reclassified", "reclassified", "no_path_history"])
        self.assertEqual(counts, report_counts(reclassified=2, no_path_history=1))

    def test_an_unknown_outcome_is_refused(self):
        with self.assertRaisesRegex(ValueError, "classified_again"):
            _mod.outcome_counts(["reclassified", "classified_again"])


if __name__ == "__main__":
    unittest.main()
