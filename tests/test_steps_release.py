"""
Tests for agentcontext.steps.release: the release built from output/.

Strategy:
  - Every test builds its own small `output/` and `release/` in a temporary
    directory. The real directories are large, and a suite that read them would
    run only on a machine that had already run the pipeline.
  - The tests treat RELEASE_FILES as data and assert the properties that the
    build relies on, because the list determines the build, the manifest, the
    README and these tests.
  - The end-to-end tests run 7zz and are skipped when 7zz is not on PATH, as in
    test_steps_collected_files_archive.
  - The usage example of the README runs with pandas over the release that the
    test builds, as a user runs it over the published files.

Requirements: Python 3.14, 7zz on PATH for the end-to-end tests, and pandas of
locks/development.txt for the test of the usage example
Run with:  python3 -m unittest tests.test_steps_release -v
"""

import csv
import hashlib
import importlib
import contextlib
import io
import os
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import redirect_stdout
from pathlib import Path
from agentcontext import SEVEN_ZIP, STAGE_DIR, commits, heuristics
from agentcontext.checks import deletions, unmatched

_mod = importlib.import_module("agentcontext.steps.release")
heuristics = importlib.import_module("agentcontext.heuristics")
planning = importlib.import_module("agentcontext.planning")
planning_schema = importlib.import_module("agentcontext.planning_schema")
commit_reclassification = importlib.import_module("agentcontext.steps.commit_reclassification")

ReleaseFile = _mod.ReleaseFile
RELEASE_FILES = _mod.RELEASE_FILES
RELEASE_NAMES = _mod.RELEASE_NAMES

HAVE_7Z = shutil.which(SEVEN_ZIP) is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# The header that the detection step writes, taken from that step's suite, which checks it
# against a real run. A copy typed here would become outdated when the step gains a column.
DETECTED_HEADER = list(importlib.import_module("tests.test_steps_detection").TestMainCli.REPOS_HEADER)


def detected_row(repo_name: str, **overrides) -> dict:
    row = {c: "FALSE" for c in DETECTED_HEADER}
    row.update({
        "repo_name": repo_name,
        "scanned_at": "2026-03-29",
        "branch": "main",
        "head_sha": "a" * 40,
        "head_commit_date": "2026-03-28T10:00:00Z",
        "github_link": f"https://github.com/{repo_name}/tree/main",
        "dataset_version": "1.0.0",
    })
    row.update(overrides)
    return row


# The `_metadata` block of a sampling frame that the sampling stage writes, cut to what a
# release reads. The stage queries GitHub for every repository in its retrieval and records the
# query in `github`.
FRAME_METADATA = {
    "filtered_at": "2026-10-05T14:00:00Z",
    "input_source": {"retrieval_id": "2026-10-05T120000Z", "downloaded_at": "2026-10-05T12:00:00Z",
                     "window": {"end_date": "2026-09-22", "created_max": "2025-03-22",
                                "committed_min": "2026-03-22"},
                     "github": {"started_at": "2026-10-05T12:10:00Z",
                                "finished_at": "2026-10-05T13:00:00Z",
                                "found": 2, "not_found": 0}},
    "stages": [{"stage": 1, "key": "gone", "removed": 1, "remaining": 2},
               {"stage": 6, "key": "languages", "removed": 0, "remaining": 2,
                "values": {"top": 10, "languages": ["C", "C#", "C++", "Go", "Java", "JavaScript",
                                                    "PHP", "Python", "Rust", "TypeScript"]}}],
    "final_count": 2,
}

# The `_metadata` of the sampling frame of March 2026, filtered from the GHS search result
# without querying GitHub.
MARCH_FRAME_METADATA = {
    "filtered_at": "2026-03-17T10:00:00Z",
    "input_source": {"path": "data/raw/ghs_data_new.json", "item_count": 187304,
                     "downloaded_at": "2026-03-16T18:07:01.920742Z",
                     "source_config": {"contributors_min": 2, "pulls_min": 1}},
    "stages": [{"stage": 1, "removed": 1, "remaining": 2}],
    "final_count": 2,
}

# The `collection` section that `snapshot write` records, cut to what the README states.
SNAPSHOT = {"collection": {"counts": {"sampled": 2, "clone_failed": 0}}}


def frame_item(name: str, **overrides) -> dict:
    item = {field: 1 for field in _mod.GHS_SCALAR_FIELDS}
    item.update({
        "name": name,
        "id": 42,
        "mainLanguage": "Python",
        "defaultBranch": "master",
        "license": "MIT",
        "homepage": None,
        "createdAt": "2020-01-01T00:00:00Z",
        "languages": {"Python": 10},
        "topics": ["ai"],
    })
    item.update(overrides)
    return item


# The header that the import detection step writes.
IMPORTS_HEADER = importlib.import_module("agentcontext.steps.import_detection").COLUMNS


def import_row(written: str, target: str, location: str, category: str, line: int) -> dict:
    """A row of `imports.csv` for an import of `target` in the CLAUDE.md of owner/scanned, with
    the target status `found`."""
    return {"repo_name": "owner/scanned", "scanned_at": "2026-03-29", "branch": "main",
            "head_sha": "a" * 40, "head_commit_date": "2026-03-28T10:00:00Z",
            "importer": "CLAUDE.md", "importer_category": "context_file", "root": "CLAUDE.md",
            "tool": "claude", "heuristic": "claude-import-01", "import": written, "line": line,
            "target": target, "target_location": location, "target_status": "found",
            "target_category": category, "depth": 1, "target_line_count": 2,
            "github_link": f"https://github.com/owner/scanned/blob/main/CLAUDE.md#L{line}",
            "dataset_version": "1.0.0"}


def write_csv(path: Path, header, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(header), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class _Fixture(unittest.TestCase):
    """A minimal but complete `output/`, with the sampling frame and the labels."""

    REPOS = ("owner/scanned", "owner/unscanned")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.output = self.tmp / "output"
        self.release = self.tmp / "release"
        self.output.mkdir()
        self.release.mkdir()

        write_csv(self.output / "repos.csv", DETECTED_HEADER,
                  [detected_row("owner/scanned", context_file="TRUE", claude="TRUE")])
        write_csv(self.output / "excluded.csv",
                  ["repo_name", "scanned_at", "head_commit_date", "reason"], [])
        for name in heuristics.csv_filenames():
            self._write_mechanism(name)
        # Two imports of one CLAUDE.md: one of a file that the archive of collected files
        # contains, and one of a file that only the imported tree has.
        self.imported = self.tmp / "data" / "imported"
        (self.imported / "owner§scanned" / "docs").mkdir(parents=True)
        (self.imported / "owner§scanned" / "docs" / "style.md").write_text(
            "# Style\nShort lines.\n", encoding="utf-8")
        (self.imported / "owner§scanned" / "AGENTS.md").write_text("hi\n", encoding="utf-8")
        write_csv(self.output / "imports.csv", IMPORTS_HEADER, [
            import_row("@AGENTS.md", "AGENTS.md", "repos_data.7z", "context_file", 1),
            import_row("@docs/style.md", "docs/style.md", "imported_files.7z", "imported", 2)])
        self._write_planning()
        self._write_records()
        self.heuristics = self.tmp / "config_path_patterns.csv"
        self.heuristics.write_text("tool,mechanism\nclaude,settings\n", encoding="utf-8")
        # provenance.json as the detection step writes it, with the checksums of the heuristics
        # that the step ran with.
        (self.output / "provenance.json").write_text(json.dumps({
            "dataset_version": "1.0.0", "heuristics": "config_path_patterns.csv",
            "heuristics_sha256": hashlib.sha256(self.heuristics.read_bytes()).hexdigest(),
            "pattern_count": 1,
            "planning_heuristics": planning.PLANNING_DOCUMENT_PATTERNS_PATH.name,
            "planning_heuristics_sha256": hashlib.sha256(
                planning.PLANNING_DOCUMENT_PATTERNS_PATH.read_bytes()).hexdigest(),
            "planning_pattern_count": 1,
            "vendored_dirs": "vendored_dirs.txt",
            "vendored_dirs_sha256": hashlib.sha256(
                heuristics.VENDORED_DIRS_PATH.read_bytes()).hexdigest(),
            "local_state": "local_state_paths.csv",
            "local_state_sha256": hashlib.sha256(
                heuristics.LOCAL_STATE_PATHS_PATH.read_bytes()).hexdigest()}), encoding="utf-8")
        self._write_snapshot()

        self.ghs = self.tmp / "ghs_filtered.json"
        self.ghs.write_text(json.dumps(
            {"_metadata": FRAME_METADATA,
             "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")

        self.annotated = self.tmp / "repos_annotated.csv"
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "42", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "github_id": "42", "default_branch": "trunk",
             "engineered_project": "false", "justification": "a tutorial"},
        ])
        self.annotated.with_suffix(".json").write_text(json.dumps(
            {"description": "run 2026-10-12T200831Z of the classification stage",
             "run_id": "2026-10-12T200831Z", "models": ["gpt-6-luna"],
             "response_models": ["gpt-6-luna-2026-09-30"]}), encoding="utf-8")
        self._write_excluded({})

    def _write_excluded(self, reasons: dict) -> None:
        """Write `repos_excluded.csv` beside the labels, in the columns that the classification
        writes, with a row per repository and reason of `reasons`."""
        write_csv(self.annotated.with_name("repos_excluded.csv"),
                  ["repo_name", "repo_url", "reason", "detected_language"],
                  [{"repo_name": name, "repo_url": f"https://github.com/{name}",
                    "reason": reason, "detected_language": ""}
                   for name, reason in reasons.items()])

    def _write_snapshot(self, **sections) -> None:
        """Write `output/snapshot.json` with the collection counts and the given sections.

        A `commit_reclassification` section without `repositories` gets a `repositories` entry
        like the one that `snapshot write` records, with placeholder names. For each outcome that
        the README states as an evidence limit, the entry lists one name per counted repository,
        `owner/<outcome>-1` to `owner/<outcome>-<n>` for a count of n."""
        reclassification = sections.get("commit_reclassification")
        if isinstance(reclassification, dict) and "repositories" not in reclassification:
            counts = reclassification.get("counts", {})
            sections["commit_reclassification"] = {**reclassification, "repositories": {
                outcome: [f"owner/{outcome}-{n}" for n in range(1, counts.get(outcome, 0) + 1)]
                for outcome in _mod.LIMITED_OUTCOMES}}
        (self.output / "snapshot.json").write_text(json.dumps({**SNAPSHOT, **sections}),
                                                   encoding="utf-8")

    def _write_mechanism(self, name: str, rows: int = 1) -> None:
        """Write one mechanism CSV with `rows` rows, in which every cell has a value of its
        own."""
        if name == "context-files.csv":
            write_csv(self.output / name, _mod.CONTEXT_FILES_COLUMN_ORDER, [
                {c: f"{c}-{i}" for c in _mod.CONTEXT_FILES_COLUMN_ORDER}
                | {"repo_name": "owner/scanned",
                   "context_file": "CLAUDE.md" if i == 0 else f"sub{i}/CLAUDE.md"}
                for i in range(rows)])
        else:
            write_csv(self.output / name, ["repo_name", "artifact"],
                      [{"repo_name": "owner/scanned", "artifact": f"x{i}/{name}"}
                       for i in range(rows)])

    def _write_planning(self, rows: int = 1) -> None:
        """Write `planning-documents.csv` with `rows` planning documents of owner/scanned."""
        write_csv(self.output / planning_schema.CSV_NAME, planning_schema.csv_header(), [
            {c: f"{c}-{i}" for c in planning_schema.csv_header()}
            | {"repo_name": "owner/scanned", "framework": "spec-kit", "document": "spec",
               planning_schema.PATH_COLUMN: f"specs/00{i + 1}-x/spec.md"}
            for i in range(rows)])

    def _write_records(self) -> None:
        """Write the records of how the collection was built, in the columns that their steps
        write, and the deletion log beside `output/`."""
        write_csv(self.output / "ai_commits-repos.csv", commits.AI_COMMITS_REPOS_COLUMNS, [
            {"repo_name": "owner/scanned", "head_sha": "a" * 40, "outcome": "history_read",
             "commits_read": 3, "commits_matched": 1, "commits_other_tools": 0,
             "no_signal_shas": "b" * 40}])
        (self.output / "commit-reclassification.json").write_text(json.dumps(
            {"complete": True, "counts": {"reclassified": 1},
             "repositories": {"owner/scanned": {"outcome": "reclassified"}}}), encoding="utf-8")
        write_csv(self.output / "unmatched-paths.csv", unmatched.PATHS_OUT_COLUMNS, [
            {"glob": ".claude/tmp/**", "repos": 2, "files": 3, "found_in": "other.csv"}])
        write_csv(self.output / "all_ai_commits-unmatched.csv", unmatched.UNMATCHED_COLUMNS_READ, [
            {"kind": "trailer", "token": "co-authored-by: imgbot[bot]", "commits": 3, "repos": 2}])
        write_csv(self.output / "deletions.csv", deletions.OUT_COLUMNS, [
            {"kind": "reason", "repo_name": "owner/scanned", "subject": "empty file", "count": 1,
             "note": ""}])
        write_csv(self.tmp / "deletion.csv", ("path", "reason"), [
            {"path": "data/validated/owner§scanned/.claude/empty.md", "reason": "empty file"}])

    def _write_commits(self, messages, name="all_ai_commits.csv", tool="claude") -> None:
        write_csv(self.output / name, list(_mod.commits.COMMIT_COLUMNS),
                  [{"repo_name": "owner/scanned", "commit_timestamp": "2026-01-01T00:00:00Z",
                    "branch": "main", "commit_sha": "b" * 40,
                    "github_link": "https://github.com/owner/scanned/commit/b",
                    "commit_message": m, "ai_tool": tool,
                    "matched_heuristics": f"{tool.lower()}-trailer-01",
                    "attributions": f"{tool}:convention"} for m in messages])

    def _repos(self):
        return _mod.build_repos(self.output, self.ghs, self.annotated)

    def _by_name(self, rows):
        return {row["repo_name"]: row for row in rows}


# ---------------------------------------------------------------------------
# The CSV
# ---------------------------------------------------------------------------

class TestReleaseFiles(unittest.TestCase):

    def test_names_are_unique(self):
        self.assertEqual(len(set(RELEASE_NAMES)), len(RELEASE_NAMES))

    def test_an_unknown_kind_is_rejected(self):
        # A typo in the kind would produce a file that the build never writes and that the
        # README never describes. No other check would detect the typo.
        with self.assertRaises(ValueError):
            ReleaseFile("x.csv", "duplicate", "output/x.csv", "CSVs", "d")

    def test_an_unknown_section_is_rejected(self):
        with self.assertRaises(ValueError):
            ReleaseFile("x.csv", "copy", "output/x.csv", "appendix", "d")

    def test_every_compressed_file_names_a_release_file_built_before_it(self):
        seen = set()
        for entry in RELEASE_FILES:
            if entry.kind == "compress":
                self.assertIn(entry.source, seen,
                              f"{entry.name} compresses {entry.source}, which is not "
                              f"written earlier in RELEASE_FILES")
            seen.add(entry.name)

    def test_every_copied_file_resolves_to_its_own_source(self):
        """A path is always truthy, and an assertion of the resolved path alone would pass with
        provenance.json copied from snapshot.json. The test compares each path with the source
        that it has to name. Every copied file except the heuristics is published
        under the name that it has in `output/`."""
        for entry in RELEASE_FILES:
            if entry.kind == "copy":
                with self.subTest(name=entry.name):
                    expected = (Path("heuristics.csv") if entry.name == "config_path_patterns.csv"
                                else heuristics.HEURISTICS_DIR / entry.name
                                if entry.source.startswith("heuristics/")
                                else Path("out") / entry.name)
                    self.assertEqual(_mod.resolve(entry.source, Path("out"),
                                                  Path("heuristics.csv")), expected)

    def test_resolve_refuses_a_source_it_does_not_know(self):
        with self.assertRaises(ValueError):
            _mod.resolve("somewhere/else.csv", Path("out"), Path("heuristics.csv"))


    def test_only_the_collected_files_archive_is_written_by_another_step(self):
        written_elsewhere = [f.name for f in RELEASE_FILES if f.kind == 'collected_files_archive']
        self.assertEqual(written_elsewhere, [_mod.ARCHIVE_NAME])

    def test_every_release_file_has_a_description(self):
        for entry in RELEASE_FILES:
            with self.subTest(name=entry.name):
                self.assertTrue(entry.description.strip())


class TestTheHeuristicsReachTheRelease(unittest.TestCase):
    """A mechanism added to the heuristics has to be published or withheld on purpose."""

    def test_every_mechanism_csv_is_published_or_withheld(self):
        # The release publishes `context-files.csv` under a name with an underscore. The test
        # therefore matches each CSV by the source of the entry that builds it rather than by
        # the file name.
        built_from = {entry.source for entry in RELEASE_FILES}
        for CSV in heuristics.csv_filenames():
            with self.subTest(CSV=CSV):
                self.assertTrue(
                    f"output/{CSV}" in built_from or CSV in _mod.WITHHELD,
                    f"{CSV} is neither in RELEASE_FILES nor in WITHHELD, so it would "
                    f"be left out of the publication without a record")

    def test_every_mechanism_csv_is_inventoried(self):
        for CSV in heuristics.csv_filenames():
            with self.subTest(CSV=CSV):
                self.assertIn(CSV, _mod.OUTPUT_INVENTORY)

    def test_every_withheld_file_is_inventoried(self):
        for name in _mod.WITHHELD:
            with self.subTest(name=name):
                self.assertIn(name, _mod.OUTPUT_INVENTORY)

    def test_what_the_ai_commit_steps_write_is_inventoried(self):
        """The cloning step and the commit attribution validation write these files into
        output/. An inventory without them would stop the publication after a collection
        because of files that the pipeline wrote itself."""
        for name in ("all_ai_commits-unmatched.csv", "all_ai_commits-validated.csv",
                     "ai_commits-repos.csv"):
            with self.subTest(name=name):
                self.assertIn(name, _mod.OUTPUT_INVENTORY)


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------

class TestInventory(_Fixture):

    def test_a_complete_output_directory_has_no_problems(self):
        self.assertEqual(_mod.inventory_problems(self.output), [])

    def test_a_file_nobody_decided_about_is_reported(self):
        (self.output / "scratch.csv").write_text("a\n", encoding="utf-8")
        problems = _mod.inventory_problems(self.output)
        self.assertEqual(len(problems), 1)
        self.assertIn("scratch.csv", problems[0])

    def test_a_missing_required_file_is_reported(self):
        (self.output / "provenance.json").unlink()
        problems = _mod.inventory_problems(self.output)
        self.assertEqual(len(problems), 1)
        self.assertIn("provenance.json", problems[0])

    def test_a_missing_record_of_the_collection_is_reported(self):
        """The step copies these records after it has removed the manifest. Without a report
        before the first write, a missing record would leave a release without a manifest."""
        for name in ("ai_commits-repos.csv", "commit-reclassification.json",
                     "unmatched-paths.csv", "deletions.csv", "all_ai_commits-unmatched.csv"):
            with self.subTest(name=name):
                content = (self.output / name).read_bytes()
                (self.output / name).unlink()
                problems = _mod.inventory_problems(self.output)
                self.assertEqual(len(problems), 1)
                self.assertIn(name, problems[0])
                if name == "commit-reclassification.json":
                    self.assertIn("--with-commit-reclassification", problems[0])
                (self.output / name).write_bytes(content)

    def test_an_optional_file_may_be_absent(self):
        # all_ai_commits.csv is gitignored and the fixture does not write it.
        self.assertNotIn("all_ai_commits", "".join(_mod.inventory_problems(self.output)))


class TestEveryStepOutputIsInTheInventory(unittest.TestCase):
    """A file that a step writes into `output/` and that the inventory does not name stops the
    publication after every earlier step has run. The fixture of TestInventory writes only the
    files that the inventory already names and cannot detect a step that writes a new file."""

    def test_every_file_a_step_declares_under_output_has_an_entry(self):
        runner = importlib.import_module("agentcontext.runner")
        declared = sorted({Path(path).name
                           for step in runner.CORE_STEPS + runner.COMMIT_RECLASSIFICATION_STEPS
                           for path in step.produces
                           if Path(path).parts[:1] == ("output",) and len(Path(path).parts) == 2})
        # The assertion that the file of the unmatched paths step is among the declared files
        # keeps an empty list from passing.
        self.assertIn("unmatched-paths.csv", declared)
        self.assertEqual([name for name in declared if name not in _mod.OUTPUT_INVENTORY], [])

    def test_every_file_tracked_under_output_has_an_entry(self):
        """A checkout contains every file that git tracks under the stage directory. A tracked
        file that the inventory does not name is in the `output/` of every run from a checkout,
        and it stops every publication. The tracked unmatched attributions are such a
        file, with a dated name."""
        result = subprocess.run(["git", "ls-files", "--", "output"],
                                cwd=str(STAGE_DIR), capture_output=True, text=True)
        if result.returncode != 0:
            self.skipTest("not a git checkout")
        entries = sorted({Path(p).relative_to("output").parts[0]
                          for p in result.stdout.splitlines()})
        # The assertions of both kinds of entry keep an empty listing from passing.
        self.assertTrue(any(e.startswith("all_ai_commits.csv.7z") for e in entries), entries)
        self.assertTrue(any(e.startswith("all_ai_commits-unmatched-") for e in entries), entries)
        self.assertEqual([e for e in entries if not _mod.inventoried(e)], [])

    def test_a_dated_name_is_inventoried_by_its_form_and_no_other_name_is(self):
        """The inventory names the archive that `unmatched reduce` writes and the CSV that the
        archive extracts to. A file with a name that only begins the same way is still a file
        that nobody decided about."""
        unmatched = importlib.import_module("agentcontext.checks.unmatched")
        name = unmatched.tracked_name("2026-11-02")
        self.assertTrue(_mod.inventoried(name))
        self.assertTrue(_mod.inventoried(f"{name}.7z"))
        for stray in ("all_ai_commits-unmatched-latest.csv.7z", f"{name}.7z.partial",
                      "all_ai_commits-unmatched-2026-11.csv"):
            with self.subTest(stray=stray):
                self.assertFalse(_mod.inventoried(stray))


# ---------------------------------------------------------------------------
# repos.csv
# ---------------------------------------------------------------------------

class TestBuildRepos(_Fixture):

    def test_one_row_per_repository_in_the_order_of_the_sampling_frame(self):
        rows = self._repos()
        self.assertEqual([r["repo_name"] for r in rows], list(self.REPOS))

    def test_the_classification_columns_are_joined(self):
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/scanned"]["engineered_project"], "true")
        self.assertEqual(rows["owner/scanned"]["justification"], "it builds")

    def test_default_branch_prefers_the_branch_that_the_classification_recorded(self):
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/unscanned"]["default_branch"], "trunk")

    def test_default_branch_falls_back_to_the_sampling_frame(self):
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "default_branch": "",
             "engineered_project": "true", "justification": "x"}])
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/scanned"]["default_branch"], "master")

    def test_the_two_detection_columns_are_published_under_their_new_names(self):
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/scanned"]["scanned_branch"], "main")
        self.assertIn("copilot_instructions", rows["owner/scanned"])

    def test_the_column_for_unclassified_files_is_not_published(self):
        self.assertNotIn("other", _mod.REPOS_COLUMN_ORDER)

    def test_github_link_points_at_the_scanned_branch(self):
        """Here the scanned branch differs from the default branch, and a link built from the
        default branch alone fails the test."""
        write_csv(self.output / "repos.csv", DETECTED_HEADER,
                  [detected_row("owner/scanned", branch="release-1")])
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/scanned"]["default_branch"], "main")
        self.assertEqual(rows["owner/scanned"]["github_link"],
                         "https://github.com/owner/scanned/tree/release-1")

    def test_github_link_falls_back_to_the_default_branch(self):
        # A repository that was never scanned still gets a link. The release therefore computes
        # the column again rather than copying it from the detection CSV.
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/unscanned"]["github_link"],
                         "https://github.com/owner/unscanned/tree/trunk")

    def test_a_branch_that_is_not_a_url_is_percent_encoded(self):
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/unscanned", "default_branch": "release #2",
             "engineered_project": "true", "justification": "x"}])
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/unscanned"]["github_link"],
                         "https://github.com/owner/unscanned/tree/release%20%232")

    def test_an_unscanned_repository_has_blank_detection_columns(self):
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/unscanned"]["scanned_at"], "")
        self.assertEqual(rows["owner/unscanned"]["claude"], "")
        self.assertEqual(rows["owner/unscanned"]["head_sha"], "")

    def test_the_json_columns_are_serialized(self):
        rows = self._by_name(self._repos())
        self.assertEqual(json.loads(rows["owner/scanned"]["languages"]), {"Python": 10})
        self.assertEqual(json.loads(rows["owner/scanned"]["topics"]), ["ai"])

    def test_a_null_field_is_blank_and_not_the_word_none(self):
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/scanned"]["homepage"], "")

    def test_a_detection_row_with_no_sampling_frame_row_is_refused(self):
        # A left join would drop the row while the published row count stayed that of the
        # sampling frame, and nothing would record that a repository is missing.
        write_csv(self.output / "repos.csv", DETECTED_HEADER,
                  [detected_row("owner/scanned"), detected_row("ghost/repo")])
        with self.assertRaises(ValueError) as caught:
            self._repos()
        self.assertIn("ghost/repo", str(caught.exception))

    def test_an_unknown_detection_column_is_refused(self):
        write_csv(self.output / "repos.csv", DETECTED_HEADER + ["surprise"],
                  [detected_row("owner/scanned") | {"surprise": "x"}])
        with self.assertRaises(ValueError) as caught:
            self._repos()
        self.assertIn("surprise", str(caught.exception))
        self.assertIn("REPOS_COLUMN_ORDER", str(caught.exception))

    def test_a_sampling_frame_that_names_a_repository_twice_is_refused(self):
        self.ghs.write_text(json.dumps(
            {"_metadata": FRAME_METADATA,
             "items": [frame_item(name) for name in (*self.REPOS, "owner/unscanned")]}),
            encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            self._repos()
        self.assertIn("same repository twice", str(caught.exception))

    def test_the_id_of_a_sampling_frame_that_queried_github_is_published(self):
        rows = self._by_name(self._repos())
        self.assertEqual(rows["owner/scanned"]["id"], "42")

    def test_labels_with_another_github_id_than_the_sampling_frame_are_refused(self):
        """The classification copies GitHub's id from the sampling frame that it labels. A label
        with another id was made over another sampling frame."""
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "7", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"}])
        with self.assertRaises(ValueError) as caught:
            self._repos()
        self.assertIn("owner/scanned (42 in the sampling frame, 7 in the labels)",
                      str(caught.exception))

    def test_a_sampling_frame_that_did_not_query_github_publishes_the_labels_github_id(self):
        """The sampling frame of March 2026 has GHS's database id rather than GitHub's:
        apache/artemis is 95560878 there and 36057260 on GitHub. The labels have GitHub's id,
        because the classification records it, and the id is blank for a name that GitHub did
        not find. A repository without a label has no id."""
        self.ghs.write_text(json.dumps(
            {"_metadata": MARCH_FRAME_METADATA,
             "items": [frame_item(name, id=95560878) for name in
                       ("owner/scanned", "owner/unscanned", "owner/unlabeled")]}),
            encoding="utf-8")
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "36057260", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "github_id": "", "default_branch": "trunk",
             "engineered_project": "false", "justification": "a tutorial"}])
        rows = self._by_name(self._repos())
        self.assertEqual({name: row["id"] for name, row in rows.items()},
                         {"owner/scanned": "36057260", "owner/unscanned": "",
                          "owner/unlabeled": ""})

    def test_labels_without_github_ids_are_refused(self):
        columns = [c for c in _mod.ANNOTATED_COLUMNS if c != "github_id"]
        write_csv(self.annotated, columns, [
            {"repo_name": "owner/scanned", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"}])
        with self.assertRaises(ValueError) as caught:
            self._repos()
        self.assertIn("github_id", str(caught.exception))

    def test_the_levels_are_counted_from_the_published_rows(self):
        total, annotated, engineered, study = _mod.levels_of(self._repos())
        self.assertEqual((total, annotated, engineered, study), (2, 2, 1, 1))


# ---------------------------------------------------------------------------
# context_files.csv
# ---------------------------------------------------------------------------

class TestBuildContextFiles(_Fixture):

    def test_the_rows_are_returned(self):
        rows = _mod.build_context_files(self.output)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["context_file"], "CLAUDE.md")

    def test_a_missing_column_is_refused(self):
        columns = [c for c in _mod.CONTEXT_FILES_COLUMN_ORDER if c != "language"]
        write_csv(self.output / "context-files.csv", columns,
                  [{c: "" for c in columns}])
        with self.assertRaises(ValueError) as caught:
            _mod.build_context_files(self.output)
        self.assertIn("language", str(caught.exception))

    def test_an_extra_column_is_refused(self):
        columns = list(_mod.CONTEXT_FILES_COLUMN_ORDER) + ["surprise"]
        write_csv(self.output / "context-files.csv", columns,
                  [{c: "" for c in columns}])
        with self.assertRaises(ValueError) as caught:
            _mod.build_context_files(self.output)
        self.assertIn("surprise", str(caught.exception))


# ---------------------------------------------------------------------------
# commits.csv
# ---------------------------------------------------------------------------

class TestBuildCommits(_Fixture):

    def _build(self):
        return _mod.build_commits(self.output / "all_ai_commits.csv",
                                  self.release / "commits.csv")

    def _problems(self) -> list[str]:
        return _mod.commit_csv_problems(self.output / "all_ai_commits.csv",
                                        heuristics.HEURISTICS_DIR / "ai_commit_patterns.csv")

    def test_a_commit_csv_in_other_columns_is_refused(self):
        """A CSV without `matched_heuristics` was written before the heuristics had ids.
        Padding it with an empty column would publish it as current, with its positions read
        as ids or its attributions missing. The refusal names the step that rewrites the
        CSV."""
        for left_out in ("matched_heuristics", "attributions"):
            with self.subTest(left_out=left_out):
                columns = [c for c in _mod.commits.COMMIT_COLUMNS if c != left_out]
                write_csv(self.output / "all_ai_commits.csv", columns,
                          [{c: "value" for c in columns}])
                problems = self._problems()
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("commit-reclassification", problems[0])

    def test_an_id_that_the_published_heuristics_lack_is_named(self):
        """`matched_heuristics` names the rows of the published commit heuristics by id. The
        fixture's `claude-trailer-01` is such a row, and `a-removed-heuristic` is none."""
        self._write_commits(["a commit"])
        self.assertEqual(self._problems(), [])
        write_csv(self.output / "all_ai_commits.csv", list(_mod.commits.COMMIT_COLUMNS),
                  [{c: "value" for c in _mod.commits.COMMIT_COLUMNS}
                   | {"matched_heuristics": "claude-trailer-01;a-removed-heuristic"}])
        problems = self._problems()
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("1 heuristic id that all_ai_commits.csv names is not in "
                      "ai_commit_patterns.csv (a-removed-heuristic)", problems[0])

    def test_a_line_break_inside_a_message_is_kept_in_a_quoted_field(self):
        """Under RFC 4180, a field that contains a line break is quoted, and a CSV reader reads
        the field back."""
        self._write_commits(['subject "one", two\n\nbody'])
        total, changed, _ = self._build()
        self.assertEqual((total, changed), (1, 0))
        written = (self.release / "commits.csv").read_text(encoding="utf-8")
        self.assertIn('"subject ""one"", two\n\nbody"', written)
        with (self.release / "commits.csv").open(newline="", encoding="utf-8") as f:
            row = next(csv.DictReader(f))
        self.assertEqual(row["commit_message"], 'subject "one", two\n\nbody')

    def test_a_carriage_return_inside_a_message_is_a_newline(self):
        """A commit message can contain a carriage return with no newline after it, and a
        reader that splits on either character takes it for a different kind of line break.
        The release writes such a carriage return as a newline, because it is a line
        break."""
        self._write_commits(["subject\rmore", "subject\rmore\r\nbody"])
        total, changed, _ = self._build()
        self.assertEqual((total, changed), (2, 2))
        with (self.release / "commits.csv").open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([row["commit_message"] for row in rows],
                         ["subject\nmore", "subject\nmore\nbody"])
        self.assertNotIn(b"\r", (self.release / "commits.csv").read_bytes())

    def test_an_already_escaped_message_is_left_alone(self):
        self._write_commits(["subject\\n\\nbody"])
        total, changed, _ = self._build()
        self.assertEqual((total, changed), (1, 0))

    def test_every_row_is_written(self):
        self._write_commits(["one", "two", "three"])
        total, _, _ = self._build()
        self.assertEqual(total, 3)


class TestCommitsSource(_Fixture):

    def test_the_csv_is_preferred(self):
        self._write_commits(["one"])
        with tempfile.TemporaryDirectory() as work:
            self.assertEqual(_mod.commits_source(self.output, Path(work)),
                             self.output / "all_ai_commits.csv")

    @unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
    def test_the_archive_is_extracted_when_the_csv_is_absent(self):
        self._write_commits(["one"])
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(self.output / "all_ai_commits.csv.7z"),
                        str(self.output / "all_ai_commits.csv")], check=True)
        (self.output / "all_ai_commits.csv").unlink()
        with tempfile.TemporaryDirectory() as work:
            with redirect_stdout(io.StringIO()):
                found = _mod.commits_source(self.output, Path(work))
            self.assertEqual(found, Path(work) / "all_ai_commits.csv")
            self.assertTrue(found.is_file())

    @unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
    def test_the_file_is_found_under_the_path_the_archive_stores_it_at(self):
        """An earlier tracked archive contains `3_data-collection/output/all_ai_commits.csv`,
        not a file at its root, and a publication from a clean checkout has to find it there."""
        self._write_commits(["one"])
        staged = self.tmp / "stage" / "3_data-collection" / "output"
        staged.mkdir(parents=True)
        shutil.move(self.output / "all_ai_commits.csv", staged / "all_ai_commits.csv")
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(self.output / "all_ai_commits.csv.7z"),
                        "3_data-collection/output/all_ai_commits.csv"],
                       cwd=str(self.tmp / "stage"), check=True)
        with tempfile.TemporaryDirectory() as work:
            with redirect_stdout(io.StringIO()):
                found = _mod.commits_source(self.output, Path(work))
            self.assertEqual(found, Path(work) / "3_data-collection" / "output"
                             / "all_ai_commits.csv")
            self.assertIn("one", found.read_text(encoding="utf-8"))

    @unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
    def test_split_archives_and_other_tool_commits_are_read_without_plain_csvs(self):
        for name in ("all_ai_commits.csv", "other_ai_commits.csv"):
            with self.subTest(name=name):
                plain = self.output / name
                plain.write_text("repo_name,commit_message\nowner/repo,evidence\n")
                archive = self.output / (name + ".7z")
                subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), str(plain)],
                               check=True, capture_output=True)
                expected = plain.read_bytes()
                raw = archive.read_bytes()
                size = len(raw) // 2
                for index, start in enumerate(range(0, len(raw), size), 1):
                    Path(str(archive) + f".{index:03}").write_bytes(raw[start:start + size])
                archive.unlink()
                plain.unlink()
                with tempfile.TemporaryDirectory() as work:
                    self.assertEqual(_mod.commits_source(self.output, Path(work), *(() if name == "all_ai_commits.csv" else (name,))).read_bytes(),
                                     expected)
                Path(str(archive) + ".002").unlink()
                with tempfile.TemporaryDirectory() as work, self.assertRaises(RuntimeError):
                    _mod.commits_source(self.output, Path(work), name)

    def test_neither_is_refused(self):
        with tempfile.TemporaryDirectory() as work:
            with self.assertRaises(FileNotFoundError):
                _mod.commits_source(self.output, Path(work))


# ---------------------------------------------------------------------------
# The generated files
# ---------------------------------------------------------------------------

class TestGeneratedFiles(_Fixture):

    def test_the_citation_names_the_authors_of_the_release_in_their_order(self):
        """The release and its MSR 2027 data showcase paper list their authors in this order,
        which differs from the order of the AIware 2026 dataset paper."""
        text = _mod.render_citation("1.0.0")
        self.assertIn("cff-version: 1.2.0", text)
        self.assertEqual(re.findall(r"family-names: (.+)", text),
                         ["Baltes", "Mohsenimofidi", "Böhme", "Nersesyan", "Abubakar", "Lulla", "Galster",
                          "Treude"])
        self.assertIn("  - given-names: Vahram\n    family-names: Nersesyan", text)

    def test_the_pipeline_is_cited_and_licensed_by_the_authors_of_the_release(self):
        """The repository's `CITATION.cff` cites the pipeline, and its `LICENSE` grants the MIT
        license, with the authors of the release in the same order, under the title of the
        dataset."""
        root = Path(__file__).resolve().parents[1]
        authors = ["Baltes", "Mohsenimofidi", "Böhme", "Nersesyan", "Abubakar", "Lulla",
                   "Galster", "Treude"]
        citation = (root / "CITATION.cff").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"family-names: (.+)", citation), authors)
        self.assertIn("\ntype: software\n", citation)
        self.assertIn("\nlicense: MIT\n", citation)
        self.assertIn('\ntitle: "AgentTorrent: A Dataset of Context and Harness Engineering '
                      'Artifacts on GitHub"\n', citation)
        license_text = " ".join((root / "LICENSE").read_text(encoding="utf-8").split())
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 Sebastian Baltes, Seyedmoein Mohsenimofidi, Levi Böhme, "
                      "Vahram Nersesyan, Muhammad Auwal Abubakar, Jai Lal Lulla, Matthias "
                      "Galster, Christoph Treude ", license_text)

    def test_the_citation_contains_no_doi(self):
        # Zenodo assigns the DOI at deposit, and nobody would have assigned a DOI written here.
        self.assertNotIn("doi", _mod.render_citation("1.0.0").lower())

    def test_the_manifest_is_sorted_and_in_sha256sum_format(self):
        for name in ("b.csv", "a.csv"):
            (self.release / name).write_text(name, encoding="utf-8")
        text = _mod.render_checksums(self.release, ["b.csv", "a.csv"])
        lines = text.splitlines()
        self.assertEqual([line.split("  ")[1] for line in lines], ["a.csv", "b.csv"])
        for line in lines:
            self.assertRegex(line, r"^[0-9a-f]{64}  \S+$")


@unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
class TestArchiveFileCount(unittest.TestCase):
    """`7zz l` ends in `N files, M folders`, or in `N files` alone when the archive contains no
    folder. The pattern accepts both summaries, and an archive of files alone does not
    raise."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "repos" / "o§a" / ".claude").mkdir(parents=True)
        for rel in ("a.md", "b.md", "repos/o§a/CLAUDE.md", "repos/o§a/.claude/settings.json"):
            (self.tmp / rel).write_text("x\n", encoding="utf-8")
        (self.tmp / "empty" / "sub").mkdir(parents=True)

    def _count(self, *members) -> int:
        archive = self.tmp / "counted.7z"
        archive.unlink(missing_ok=True)
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), *members], cwd=self.tmp,
                       check=True)
        return _mod.archive_file_count(archive)

    def test_files_and_folders(self):
        self.assertEqual(self._count("repos"), 2)

    def test_files_and_no_folder(self):
        self.assertEqual(self._count("a.md", "b.md"), 2)

    def test_folders_and_no_file(self):
        self.assertEqual(self._count("empty"), 0)

    def test_a_file_named_like_the_summary_is_not_the_count(self):
        """The listing names each file on a line of its own. A file called `7 files` ends a
        line the way the summary does, and that line comes before the summary."""
        (self.tmp / "7 files").write_text("x\n", encoding="utf-8")
        self.assertEqual(self._count("7 files", "a.md"), 2)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------

@unittest.skipUnless(HAVE_7Z, "7zz is not on PATH")
class TestPublish(_Fixture):

    def setUp(self):
        super().setUp()
        self._write_commits(["a commit"])
        sample = self.tmp / "repos"
        (sample / "owner§scanned" / ".claude").mkdir(parents=True)
        (sample / "owner§scanned" / "CLAUDE.md").write_text("hi\n", encoding="utf-8")
        (sample / "owner§scanned" / ".claude" / "settings.json").write_text(
            "{}\n", encoding="utf-8")
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(self.release / _mod.ARCHIVE_NAME), str(sample)], check=True)

    def _run(self) -> int:
        with redirect_stdout(io.StringIO()):
            return _mod.publish(self.output, self.release, self.ghs,
                                self.annotated, self.heuristics, imported_dir=self.imported)

    def _run_main(self) -> int:
        """Run the release through `main`, where a refusal reaches the `except` clause that
        logs it and returns 1."""
        with redirect_stdout(io.StringIO()):
            return _mod.main(["--output-dir", str(self.output), "--release-dir", str(self.release),
                              "--ghs-json", str(self.ghs), "--annotated-csv", str(self.annotated),
                              "--heuristics", str(self.heuristics),
                              "--imported-dir", str(self.imported)])

    def test_a_run_that_stops_part_way_leaves_no_manifest(self):
        """A failure after the first CSV must not leave this run's CSVs beside the previous
        run's checksums.sha256, where `sha256sum -c` would report mismatches in a directory
        that nobody had edited. A release that was not finished has no manifest."""
        self._run()
        self.assertTrue((self.release / "checksums.sha256").is_file())
        with mock.patch.object(_mod, "build_commits",
                               side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(OSError):
                self._run()
        self.assertFalse((self.release / "checksums.sha256").exists(),
                         "the previous run's manifest survived a failed rebuild")

    def _extract(self, name: str) -> dict:
        """Map the name of every file in the 7z archive `release/<name>` to its bytes."""
        work = Path(tempfile.mkdtemp(dir=self.tmp))
        subprocess.run([SEVEN_ZIP, "x", "-bso0", "-bsp0", str(self.release / name), f"-o{work}"],
                       check=True)
        return {p.relative_to(work).as_posix(): p.read_bytes()
                for p in work.rglob("*") if p.is_file()}

    def _published_rows(self, name: str) -> tuple[list, list]:
        with (self.release / name).open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return reader.fieldnames, list(reader)

    def test_the_other_tools_commits_are_published_as_the_cloning_step_wrote_them(self):
        self._write_commits(["Co-authored-by: Amp <amp@ampcode.com>"],
                            name="other_ai_commits.csv", tool="Amp")
        self.assertEqual(self._run(), 0)
        header, rows = self._published_rows("other_commits.csv")
        self.assertEqual([(r["ai_tool"], r["commit_message"]) for r in rows],
                         [("Amp", "Co-authored-by: Amp <amp@ampcode.com>")])

    def test_a_collection_before_the_other_tools_commit_csv_publishes_its_header_alone(self):
        """A collection may have written no `other_ai_commits.csv`. The release still includes
        the file, with its header alone, so that the release has the same files whichever
        collection built it."""
        self.assertFalse((self.output / "other_ai_commits.csv").exists())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run(), 0)
        header, rows = self._published_rows("other_commits.csv")
        self.assertEqual((header, rows), (list(_mod.commits.COMMIT_COLUMNS), []))

    def test_every_release_file_is_written(self):
        """The test compares each file with what it is made from. An assertion that each name
        exists would pass copies of the header alone, blanked cells in repos.csv and
        context_files.csv, both archives compressing context_files.csv, and provenance.json
        copied from snapshot.json."""
        corpus = (self.release / _mod.ARCHIVE_NAME).read_bytes()
        self.assertEqual(self._run(), 0)
        self.assertEqual(sorted(os.listdir(self.release)), sorted(RELEASE_NAMES))

        for entry in RELEASE_FILES:
            if entry.kind == "copy":
                source = (self.heuristics if entry.name == "config_path_patterns.csv"
                          else heuristics.HEURISTICS_DIR / entry.name
                          if entry.source.startswith("heuristics/")
                          else self.output / entry.name)
                with self.subTest(copied=entry.name):
                    self.assertEqual((self.release / entry.name).read_bytes(),
                                     source.read_bytes())

        header, rows = self._published_rows("repos.csv")
        self.assertEqual(header, list(_mod.REPOS_COLUMN_ORDER))
        self.assertEqual(rows, [{c: row[c] for c in _mod.REPOS_COLUMN_ORDER}
                                for row in self._repos()])
        scanned = self._by_name(rows)["owner/scanned"]
        self.assertEqual((scanned["claude"], scanned["justification"], scanned["scanned_at"]),
                         ("TRUE", "it builds", "2026-03-29"))

        with (self.output / "context-files.csv").open(newline="", encoding="utf-8") as f:
            context_source = list(csv.DictReader(f))
        self.assertEqual(self._published_rows("context_files.csv"),
                         (list(_mod.CONTEXT_FILES_COLUMN_ORDER), context_source))

        with (self.output / "all_ai_commits.csv").open(newline="", encoding="utf-8") as f:
            commits_source = list(csv.DictReader(f))
        self.assertEqual(self._published_rows("commits.csv")[1], commits_source)

        for archive, member in (("repos.csv.7z", "repos.csv"), ("commits.csv.7z", "commits.csv")):
            with self.subTest(archive=archive):
                self.assertEqual(self._extract(archive),
                                 {member: (self.release / member).read_bytes()})

        self.assertEqual(json.loads((self.release / "sampling.json").read_text(encoding="utf-8")),
                         FRAME_METADATA)

        self.assertEqual((self.release / _mod.ARCHIVE_NAME).read_bytes(), corpus,
                         "the collected_files_archive step's file is not this step's to write")
        self.assertEqual((self.release / "CITATION.cff").read_text(encoding="utf-8"),
                         _mod.render_citation("1.0.0"))

    def test_a_reader_gets_what_shows_how_the_data_was_built(self):
        """The names are spelled out rather than read from `RELEASE_FILES`, because the test
        states what the release has to contain: A reader without the repository of the pipeline
        can audit the data only with these files. Each has the bytes of its source, the manifest
        covers it, and the provenance table of the README describes it."""
        self.assertEqual(self._run(), 0)
        sources = {name: self.output / name
                   for name in ("ai_commits-repos.csv", "commit-reclassification.json",
                                "unmatched-paths.csv", "deletions.csv")}
        sources |= {name: heuristics.HEURISTICS_DIR / name
                    for name in ("not_configuration.csv", "not_ai_commits.csv",
                                 "not_imports.csv", "not_planning_documents.csv",
                                 "doc_hashes.json", "doc_quotes.json")}
        manifest = (self.release / "checksums.sha256").read_text(encoding="utf-8")
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        table = readme.split("These files record how the data above was produced:", 1)[1]
        table = table.split("The release leaves out", 1)[0]
        for name, source in sources.items():
            with self.subTest(name=name):
                self.assertEqual((self.release / name).read_bytes(), source.read_bytes())
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                self.assertIn(f"{digest}  {name}\n", manifest)
                self.assertIn(f"| `{name}` | ", table)

    def test_the_deletion_log_is_published_as_an_archive_of_the_log_alone(self):
        """The archive holds the bytes of the log under its bare name, whatever directory the log
        is in, and the release does not contain the log a second time beside the archive."""
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._extract("deletion.csv.7z"),
                         {"deletion.csv": (self.tmp / "deletion.csv").read_bytes()})
        self.assertFalse((self.release / "deletion.csv").exists())
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        section = readme.split("## Compressed Archives", 1)[1].split("\n## ", 1)[0]
        self.assertRegex(section, r"\| `deletion\.csv\.7z` \| [\d.]+ MiB \| `deletion\.csv`")

    def test_the_archive_of_the_deletion_log_has_the_same_bytes_whenever_the_log_does(self):
        """The cleanup writes the log anew on every run, and the archive leaves its time out, so
        that the manifest changes only with the content of the log."""
        self.assertEqual(self._run(), 0)
        first = (self.release / "deletion.csv.7z").read_bytes()
        os.utime(self.tmp / "deletion.csv", (0, 0))
        self.assertEqual(self._run(), 0)
        self.assertEqual((self.release / "deletion.csv.7z").read_bytes(), first)

    def test_a_missing_deletion_log_leaves_the_previous_release_as_it_was(self):
        """The manifest of the previous release survives, because the step refuses before it
        writes anything, and the refusal names the log."""
        self.assertEqual(self._run(), 0)
        before = (self.release / "checksums.sha256").read_bytes()
        (self.tmp / "deletion.csv").unlink()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self._run_main(), 1)
        self.assertIn("deletion.csv does not exist", err.getvalue())
        self.assertEqual((self.release / "checksums.sha256").read_bytes(), before)

    def test_the_lines_that_name_a_bot_or_a_tool_are_published(self):
        """The release publishes the lines of the full list that the rule of `unmatched reduce`
        keeps, with the columns that the tracked archive has, and the manifest covers the file."""
        write_csv(self.output / "all_ai_commits-unmatched.csv",
                  ("kind", "token", "commits", "repos", "shas", "lines"),
                  [{"kind": "trailer", "token": "co-authored-by: ada example", "commits": 3,
                    "repos": 2, "shas": "a1", "lines": "Co-authored-by: Ada Example <a@x.test>"},
                   {"kind": "trailer", "token": "co-authored-by: imgbot[bot]", "commits": 7,
                    "repos": 5, "shas": "b2", "lines": "Co-authored-by: imgbot[bot] <i@x.test>"},
                   {"kind": "author", "token": "imgbot[bot] <ada@example.org>", "commits": 1,
                    "repos": 1, "shas": "c3", "lines": "imgbot[bot] <ada@example.org>"}])
        self.assertEqual(self._run(), 0)
        with (self.release / "unmatched-attributions.csv").open(newline="", encoding="utf-8") as f:
            published = list(csv.reader(f))
        self.assertEqual(published, [["kind", "token", "commits", "repos"],
                                     ["trailer", "co-authored-by: imgbot[bot]", "7", "5"]])
        self.assertIn("unmatched-attributions.csv",
                      (self.release / "checksums.sha256").read_text(encoding="utf-8"))
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("| `unmatched-attributions.csv` | ", readme)
        self.assertIn("### unmatched-attributions.csv", readme)

    def test_a_full_list_that_cannot_be_read_stops_the_release(self):
        (self.output / "all_ai_commits-unmatched.csv").write_text("kind,token\n", encoding="utf-8")
        self.assertEqual(self._run(), 1)

    def test_the_lists_that_name_people_stay_out_of_the_release(self):
        """The unmatched attributions name the co-authors of commits that no heuristic matched,
        which have no row in the commit CSVs. No file of the release may carry the lists, and the
        README states why the ranked list is left out."""
        person = "co-authored-by: ada example"
        address = "ada@example.org"
        write_csv(self.output / "unmatched-commits.csv", ("kind", "token", "repos", "commits"),
                  [{"kind": "trailer", "token": person, "repos": 2, "commits": 3}])
        for name in ("all_ai_commits-unmatched.csv", "all_ai_commits-unmatched-2026-11-04.csv"):
            write_csv(self.output / name, ("kind", "token", "commits", "repos"),
                      [{"kind": "author", "token": f"ada example <{address}>", "commits": 3,
                        "repos": 2},
                       {"kind": "trailer", "token": person, "commits": 3, "repos": 2}])
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(self.output / "all_ai_commits-unmatched-2026-11-04.csv.7z"),
                        str(self.output / "all_ai_commits-unmatched-2026-11-04.csv")], check=True)
        write_csv(self.output / "all_ai_commits-mentions.csv", ("tool", "pattern", "line"),
                  [{"tool": "Claude", "pattern": "x", "line": f"ada example <{address}>"}])
        self.assertEqual(self._run(), 0)
        for path in self.release.iterdir():
            with self.subTest(name=path.name):
                content = path.read_bytes()
                self.assertNotIn(person.encode(), content)
                self.assertNotIn(address.encode(), content)
        for archive in ("deletion.csv.7z", "repos.csv.7z", "commits.csv.7z"):
            with self.subTest(archive=archive):
                for content in self._extract(archive).values():
                    self.assertNotIn(address.encode(), content)
        self.assertEqual(sorted(os.listdir(self.release)), sorted(RELEASE_NAMES))
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("- `unmatched-commits.csv`: the ranked list of the attributions", readme)
        self.assertIn("Most of them name people", readme)

    def test_the_readme_defines_the_columns_of_every_record(self):
        """The columns come from the code that writes each file and from the tracked files, not
        from the README, so that a column that a step adds fails here until the README defines
        it. The fields of `commit-reclassification.json` and of its repositories come from the
        tracked report."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        sections = {match.group(1): match.group(2)
                    for match in re.finditer(r"^### (.+?)\n(.*?)(?=^##)", readme, re.M | re.S)}

        def section_of(name: str) -> str:
            found = [text for heading, text in sections.items() if name in heading]
            self.assertEqual(len(found), 1, f"{name} has no section of its own in the README")
            return found[0]

        def negatives_columns(name: str) -> list:
            with (heuristics.HEURISTICS_DIR / name).open(newline="", encoding="utf-8") as f:
                return next(csv.reader(f))

        columns = {"ai_commits-repos.csv": commits.AI_COMMITS_REPOS_COLUMNS,
                   "unmatched-paths.csv": unmatched.PATHS_OUT_COLUMNS,
                   "unmatched-attributions.csv": unmatched.UNMATCHED_COLUMNS_READ,
                   "deletions.csv": deletions.OUT_COLUMNS,
                   "deletion.csv": ("path", "reason")}
        for name in ("not_configuration.csv", "not_ai_commits.csv", "not_planning_documents.csv"):
            columns[name] = negatives_columns(name)
        report = json.loads((STAGE_DIR / "output" / "commit-reclassification.json")
                            .read_text(encoding="utf-8"))
        columns["commit-reclassification.json"] = [
            *report, *{field for entry in report["repositories"].values() for field in entry}]
        for name, names in columns.items():
            text = section_of(name)
            for column in names:
                with self.subTest(file=name, column=column):
                    self.assertIn(f"`{column}`", text)
        for outcome in commit_reclassification.OUTCOMES:
            with self.subTest(outcome=outcome):
                self.assertRegex(section_of("commit-reclassification.json"),
                                 rf"(?m)^- `{outcome}`: \S")
        for name in ("doc_hashes.json", "doc_quotes.json"):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", section_of("doc_hashes.json"))

    def test_a_sampling_frame_without_its_provenance_writes_nothing(self):
        """The release names the GHS retrieval that its sample came from by reading the
        `_metadata` of the sampling frame. Without the `_metadata`, the release cannot name the
        retrieval and writes nothing."""
        self.ghs.write_text(json.dumps({"items": [frame_item(n) for n in self.REPOS]}),
                            encoding="utf-8")
        before = sorted(os.listdir(self.release))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self._run_main(), 1)
        self.assertIn("has no _metadata.input_source", err.getvalue())
        self.assertEqual(sorted(os.listdir(self.release)), before)

    def test_the_manifest_covers_every_other_file_and_verifies(self):
        """The test computes the digests itself. With the module's own `sha256_of`, a manifest
        of SHA3-256 digests would verify against itself."""
        self.assertEqual(self._run(), 0)
        listed = {}
        for line in (self.release / "checksums.sha256").read_text().splitlines():
            digest, name = line.split("  ", 1)
            listed[name] = digest
        self.assertEqual(set(listed), set(RELEASE_NAMES) - {"checksums.sha256"})
        for name, digest in listed.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((self.release / name).read_bytes()).hexdigest(),
                                 digest)

    def test_a_second_run_writes_the_same_bytes(self):
        self._run()
        first = {name: (self.release / name).read_bytes() for name in RELEASE_NAMES}
        self._run()
        for name in RELEASE_NAMES:
            with self.subTest(name=name):
                self.assertEqual((self.release / name).read_bytes(), first[name])

    def test_the_archive_does_not_contain_the_sources_timestamps(self):
        """`compress` passes `-mtc=off -mtm=off -mta=off`, which keeps the source's timestamps
        out of the archive and therefore out of the checksum manifest. The whole-release test
        above cannot detect a missing flag, because its two runs finish within one second, the
        granularity of 7zz, and the archives match either way. This test sets the mtime back to
        1970 between the two calls."""
        source = self.release / "moving.csv"
        self.release.mkdir(parents=True, exist_ok=True)
        source.write_text("repo_name\no/r\n", encoding="utf-8")
        _mod.compress(self.release, "moving.7z", "moving.csv")
        first = (self.release / "moving.7z").read_bytes()
        os.utime(source, (0, 0))
        _mod.compress(self.release, "moving.7z", "moving.csv")
        self.assertEqual((self.release / "moving.7z").read_bytes(), first)

    def test_a_second_run_rewrites_every_copied_file_so_that_the_runner_detects_it(self):
        # The runner decides from the file's mtime, inode and size before and after the step
        # whether a step wrote a declared output. shutil.copy2 keeps the source's mtime. Over a
        # destination that already contained the same bytes, such a copy leaves the signature
        # as it was, and the runner then reports that the step exited 0 but did not write the
        # file.
        self._run()
        old = 1_600_000_000
        copied = [e for e in _mod.RELEASE_FILES if e.kind == "copy"]
        for entry in copied:
            for path in (_mod.resolve(entry.source, self.output, self.heuristics),
                         self.release / entry.name):
                os.utime(path, (old, old))
        self._run()
        for entry in copied:
            with self.subTest(name=entry.name):
                self.assertGreater((self.release / entry.name).stat().st_mtime, old)

    def test_the_readme_names_every_csv_with_its_row_count(self):
        """The test checks every row of the overview, the four provenance levels and the file
        count of the archive of collected files. A count one too high for every copied CSV, the
        four levels in reverse order or a file count of 0 fails the test. Every CSV here has a
        row count of its own, and the four levels differ: four repositories in the
        sampling frame, three labeled, two engineered and one scanned."""
        names = ("owner/scanned", "owner/unscanned", "owner/engineered", "owner/unlabeled")
        self.ghs.write_text(json.dumps({"_metadata": FRAME_METADATA,
                                        "items": [frame_item(n) for n in names]}),
                            encoding="utf-8")
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "default_branch": "trunk",
             "engineered_project": "false", "justification": "a tutorial"},
            {"repo_name": "owner/engineered", "default_branch": "main",
             "engineered_project": "true", "justification": "it ships"},
        ])
        self._write_excluded({"owner/unlabeled": "empty_readme"})
        self._write_commits(["one", "two", "three"])
        self._write_commits(["four", "five"], name="other_ai_commits.csv", tool="Devin")
        self._write_snapshot(collection={"counts": {"sampled": 3, "clone_failed": 1}})
        expected = {"repos.csv": 4, "commits.csv": 3, "other_commits.csv": 2}
        for rows, name in enumerate(heuristics.csv_filenames(), start=5):
            self._write_mechanism(name, rows)
            expected["context_files.csv" if name == "context-files.csv" else name] = rows
        expected["imports.csv"] = 2
        self._write_planning(rows + 1)
        expected[planning_schema.CSV_NAME] = rows + 1
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")

        lines = readme.splitlines()
        start = lines.index("| File | Rows | Description |") + 2
        overview = lines[start:lines.index("", start)]
        self.assertEqual(overview, [f"| `{f.name}` | {expected[f.name]:,} | {f.description} |"
                                    for f in RELEASE_FILES if f.section == "CSVs"])
        # The count of the detection level is every repository in the sample that git could
        # clone, and the sentence states how many repositories git could not clone.
        for sentence in ("in 1 GitHub repository.",
                         "1. **Sampling frame** (4 repos)",
                         "2. **Classification** (3 annotated)",
                         "2 were classified as engineered projects. 1 repository in the "
                         "sampling frame has no published label: 1 with an empty README.\n",
                         "3. **Configuration detection** (1 repo): Of the 3 repositories in "
                         "the sample, 2 were cloned and scanned for configuration "
                         "artifacts, and git could not clone 1.",
                         "1 repository contained at least one configuration artifact.",
                         "- All 4 rows have GitHub metadata",
                         "- 3 rows have classification data",
                         "- 1 row has detection flags of tools, context files and mechanisms",
                         "for 1 repository (2 files)",
                         "in `imports.csv`, for 1 repository (1 file)"):
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, readme)

    def test_the_archive_of_the_imported_files_contains_what_imports_csv_places_there(self):
        """The archive contains the target that only the imported tree has, under
        `imported/<owner§repo>/`, and not the target that the archive of collected files
        contains."""
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._extract(_mod.IMPORTED_ARCHIVE_NAME),
                         {"imported/owner§scanned/docs/style.md": b"# Style\nShort lines.\n"})
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("### imports.csv", readme)
        self.assertIn("| `target_status` |", readme)

    def test_the_readme_states_the_branch_from_which_code_review_includes_files(self):
        """Claude Code Review includes the files that a root REVIEW.md imports only when it
        reads REVIEW.md from a protected branch. A reader of `imports.csv` cannot see this
        condition in the rows."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("`claude` (Claude Code and Claude Code Review)", readme)
        self.assertIn("only when it reads REVIEW.md from a protected branch", readme)

    def test_a_path_that_the_archive_step_prunes_is_not_packed(self):
        """An import detection step without the status `pruned` can write a row that places a
        log in the archive of the imported files. Such a row publishes nothing that the archive
        of collected files leaves out."""
        (self.imported / "owner§scanned" / "debug.log").write_text("secret\n", encoding="utf-8")
        write_csv(self.output / "imports.csv", IMPORTS_HEADER, [
            import_row("@AGENTS.md", "AGENTS.md", "repos_data.7z", "context_file", 1),
            import_row("@docs/style.md", "docs/style.md", "imported_files.7z", "imported", 2),
            import_row("@debug.log", "debug.log", "imported_files.7z", "imported", 1)])
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._extract(_mod.IMPORTED_ARCHIVE_NAME),
                         {"imported/owner§scanned/docs/style.md": b"# Style\nShort lines.\n"})

    def test_an_imported_file_behind_a_link_is_packed_as_the_file_that_the_tool_read(self):
        """Where an import reaches a file through a link, the cloning step writes the link into
        the imported tree. The archive contains the file that the link leads to, at the path
        that the row names."""
        style = self.imported / "owner§scanned" / "docs" / "style.md"
        real = self.imported / "owner§scanned" / "shared" / "style.md"
        real.parent.mkdir()
        style.rename(real)
        style.symlink_to("../shared/style.md")
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._extract(_mod.IMPORTED_ARCHIVE_NAME),
                         {"imported/owner§scanned/docs/style.md": b"# Style\nShort lines.\n"})

    def _release_state(self) -> dict:
        """Map each entry of `release/` to the SHA-256 of its bytes and its modification time in
        nanoseconds, so that a test can tell whether a run wrote anything there."""
        return {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(),
                            path.stat().st_mtime_ns)
                for path in sorted(self.release.iterdir())}

    def test_a_row_whose_file_the_imported_tree_lacks_leaves_the_release_unchanged(self):
        """The release checks the imported tree before it removes the manifest and rewrites the
        first CSV. A target that the tree lacks therefore leaves the previous release whole,
        with its manifest, rather than half rebuilt."""
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        (self.imported / "owner§scanned" / "docs" / "style.md").unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._run_main(), 1)
        self.assertIn("output/imports.csv places 1 target in imported_files.7z that the imported "
                      f"tree {self.imported} lacks (docs/style.md of owner§scanned)",
                      err.getvalue())
        self.assertEqual(self._release_state(), before)

    def test_a_row_that_places_a_stub_leaves_the_release_unchanged(self):
        """The cloning step names each stub in a list beside the repository's directory in the
        imported tree. An import detection step that does not read the list can write a row
        that places a stub in the archive of the imported files, and the release would then
        publish NUL bytes as the file. The release refuses such a row before it writes
        anything."""
        from agentcontext import imports
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        style = self.imported / "owner§scanned" / "docs" / "style.md"
        style.write_bytes(bytes(len(style.read_bytes())))
        imports.stub_list_path(self.imported / "owner§scanned").write_text('["docs/style.md"]\n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._run_main(), 1)
        self.assertIn("output/imports.csv places 1 target in imported_files.7z of which the "
                      f"imported tree {self.imported} holds only a stub", err.getvalue())
        self.assertIn("(docs/style.md of owner§scanned)", err.getvalue())
        self.assertEqual(self._release_state(), before)

    def _assert_refused_before_any_write(self, before: dict, message: str) -> None:
        """Run the release through `main`, and assert that it returns 1, logs `message` and
        leaves every entry of `release/` as `before` records it."""
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._run_main(), 1)
        self.assertIn(message, err.getvalue())
        self.assertEqual(self._release_state(), before)

    def test_a_detection_row_outside_the_sampling_frame_leaves_the_release_unchanged(self):
        """The archive of collected files holds the repository too, so that only the comparison
        of the detection rows with the sampling frame refuses it."""
        self.assertEqual(self._run(), 0)
        write_csv(self.output / "repos.csv", DETECTED_HEADER,
                  [detected_row("owner/scanned", context_file="TRUE", claude="TRUE"),
                   detected_row("ghost/repo", context_file="TRUE", claude="TRUE")])
        sample = self.tmp / "repos"
        (sample / "ghost§repo").mkdir()
        (sample / "ghost§repo" / "CLAUDE.md").write_text("hi\n", encoding="utf-8")
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0",
                        str(self.release / _mod.ARCHIVE_NAME), str(sample)], check=True)
        before = self._release_state()
        self._assert_refused_before_any_write(
            before, "1 repository has rows in output/repos.csv and is not in "
                    "ghs_filtered.json (ghost/repo).")

    def test_labels_with_another_github_id_leave_the_release_unchanged(self):
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "7", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "github_id": "42", "default_branch": "trunk",
             "engineered_project": "false", "justification": "a tutorial"}])
        self._assert_refused_before_any_write(
            before, "1 repository has another GitHub id in repos_annotated.csv than in "
                    "ghs_filtered.json. It is owner/scanned (42 in the sampling frame, 7 in the "
                    "labels).")

    def test_a_column_added_to_the_context_files_leaves_the_release_unchanged(self):
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        columns = [*_mod.CONTEXT_FILES_COLUMN_ORDER, "surprise"]
        write_csv(self.output / "context-files.csv", columns,
                  [{c: f"{c}-0" for c in columns} | {"repo_name": "owner/scanned"}])
        self._assert_refused_before_any_write(
            before, "output/context-files.csv has column(s) ['surprise']")

    def test_a_commit_csv_in_other_columns_leaves_the_release_unchanged(self):
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        columns = [c for c in _mod.commits.COMMIT_COLUMNS if c != "attributions"]
        write_csv(self.output / "all_ai_commits.csv", columns, [{c: "value" for c in columns}])
        self._assert_refused_before_any_write(
            before, "Run commit-reclassification to write it under the current heuristics.")

    def test_a_heuristic_id_that_the_release_lacks_leaves_the_release_unchanged(self):
        """Each commit CSV is compared with its own commit heuristics, `commits.csv` with
        `ai_commit_patterns.csv` and `other_commits.csv` with
        `other_ai_commit_patterns.csv`."""
        for name, patterns in (("all_ai_commits.csv", "ai_commit_patterns.csv"),
                               ("other_ai_commits.csv", "other_ai_commit_patterns.csv")):
            with self.subTest(name=name):
                self._write_commits(["a commit"])
                (self.output / "other_ai_commits.csv").unlink(missing_ok=True)
                self.assertEqual(self._run(), 0)
                before = self._release_state()
                write_csv(self.output / name, list(_mod.commits.COMMIT_COLUMNS),
                          [{c: "value" for c in _mod.commits.COMMIT_COLUMNS}
                           | {"matched_heuristics": "a-removed-heuristic"}])
                self._assert_refused_before_any_write(
                    before, f"1 heuristic id that {name} names is not in {patterns} "
                            f"(a-removed-heuristic)")

    def test_a_copied_csv_without_repo_name_leaves_the_release_unchanged(self):
        """An empty `settings.csv` is not a CSV that the detection step wrote."""
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        (self.output / "settings.csv").write_text("", encoding="utf-8")
        self._assert_refused_before_any_write(
            before, f"{self.output / 'settings.csv'} has no repo_name column and is not a CSV "
                    f"that the detection step wrote. Run `python -m agentcontext detection` "
                    f"again.")

    def test_the_archive_of_the_collected_files_is_left_as_it_was(self):
        """The imports change neither `repos_data.7z`, which the collected_files_archive step
        writes, nor the file count that the README states for it."""
        before = (self.release / _mod.ARCHIVE_NAME).read_bytes()
        self.assertEqual(self._run(), 0)
        self.assertEqual((self.release / _mod.ARCHIVE_NAME).read_bytes(), before)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("for 1 repository (2 files). Each subdirectory", readme)

    def test_the_readme_names_the_model_that_the_classification_record_names(self):
        """The README names the model from the classification record rather than from a
        string in the code, because a run of the classification stage can label with another
        model."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("Repositories were classified by `gpt-6-luna` (run 2026-10-12T200831Z of "
                      "the classification stage)", readme)
        self.assertIn("| `justification` | The justification that `gpt-6-luna` gave in its "
                      "response, followed by the classification's verdict", readme)
        self.assertNotIn("GPT-5.2", readme)

    def test_the_readme_names_two_models_as_a_list_and_each_justification_by_its_labeler(self):
        """With the second model among the models of the record, the README joins the models
        of the classification step as an English list. A justification is the response of the
        one model that labeled the repository, and the description of the column names that
        model rather than both, because the sentences after the classification step name the
        repositories of the second model."""
        record = self.annotated.with_suffix(".json")
        record.write_text(json.dumps({**json.loads(record.read_text()),
                                      "models": ["deepseek/deepseek-v4.1-flash",
                                                 "z-ai/glm-5.3"]}))
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("Repositories were classified by `deepseek/deepseek-v4.1-flash` and "
                      "`z-ai/glm-5.3` (run 2026-10-12T200831Z of the classification stage)",
                      readme)
        self.assertIn("| `justification` | The justification that the model that labeled the "
                      "repository gave in its response, followed by the classification's "
                      "verdict", readme)
        self.assertNotIn("`deepseek/deepseek-v4.1-flash`, `z-ai/glm-5.3`", readme)

    def test_the_readme_lists_the_repositories_that_the_second_model_labeled(self):
        """The classification's record lists the repositories that the classifying model
        labeled at its higher temperature, those that the second model labeled, because the
        classifying model left them without a decision, and those that the second model left
        without a decision too. The README names the classifying model, states how many there
        are and names each with how the classifying model's response failed, from the record. A
        record that lists none adds no sentence."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertNotIn("The second model", readme)
        self.assertNotIn("higher temperature", readme)
        record = self.annotated.with_suffix(".json")
        deepseek = "deepseek/deepseek-v4.1-flash"
        labeled = [{"repo_name": "IBM/ansible-storage-protect", "github_id": 839880994,
                    "classifying_model": deepseek, "model": "z-ai/glm-5.3",
                    "reason": "output_limit"},
                   {"repo_name": "acme/widget", "github_id": 7, "classifying_model": deepseek,
                    "model": "z-ai/glm-5.3", "reason": "response_format"}]
        hotter = [{"repo_name": "acme/gadget", "github_id": 8, "classifying_model": deepseek,
                   "model": deepseek, "temperature": 1.0, "reason": "tool_call"},
                  {"repo_name": "acme/gizmo", "github_id": 10, "classifying_model": deepseek,
                   "model": deepseek, "temperature": 1.0, "reason": "output_limit"}]
        failed = [{"repo_name": "acme/broken", "github_id": 9, "classifying_model": deepseek,
                   "model": "z-ai/glm-5.3", "reason": "output_limit"}]
        for higher, listed, unlabeled, sentence in (
                ([], labeled[:1], [], "The second model, `z-ai/glm-5.3`, labeled 1 repository "
                                      "that the classifying model, "
                                      "`deepseek/deepseek-v4.1-flash`, left without a decision "
                                      "after its retry rounds. The repository, with how the last "
                                      "of the classifying model's responses there failed, is "
                                      "`IBM/ansible-storage-protect` (a response stopped at its "
                                      "output limit before the model submitted its checklist)."),
                ([], labeled, [], "The second model, `z-ai/glm-5.3`, labeled 2 repositories "
                                  "that the classifying model, `deepseek/deepseek-v4.1-flash`, "
                                  "left without a decision after its retry rounds. The "
                                  "repositories, each with how the last of the classifying "
                                  "model's responses there failed, are `acme/widget` (a final "
                                  "response did not follow the response format) and "
                                  "`IBM/ansible-storage-protect` (a response stopped at its "
                                  "output limit before the model submitted its checklist)."),
                (hotter[:1], labeled[:1], [], "The classifying model, "
                                              "`deepseek/deepseek-v4.1-flash`, labeled 1 "
                                              "repository at its higher temperature, 1.0, after "
                                              "one of its responses at the repository failed. At "
                                              "1 repository, the model called a tool that its "
                                              "request did not offer: `acme/gadget`. The second "
                                              "model, "
                                              "`z-ai/glm-5.3`, labeled 1 repository that the "
                                              "classifying model"),
                (hotter, [], [], "The classifying model, `deepseek/deepseek-v4.1-flash`, "
                                 "labeled 2 repositories at its higher temperature, 1.0, after "
                                 "one of its responses at each repository failed. At 1 "
                                 "repository, the model called a tool that its request did not "
                                 "offer: `acme/gadget`. At 1 repository, a response stopped at "
                                 "its output limit before the model submitted its checklist: "
                                 "`acme/gizmo`."),
                ([], [], failed, "Of the repositories that the classifying model, "
                                 "`deepseek/deepseek-v4.1-flash`, left without a decision after "
                                 "its retry rounds, the second model, `z-ai/glm-5.3`, left 1 "
                                 "repository without a decision too. The repository, with how "
                                 "the last of the classifying model's responses there failed, "
                                 "is `acme/broken` (a response stopped at its output limit "
                                 "before the model submitted its checklist).")):
            with self.subTest(higher=len(higher), repositories=len(listed),
                              unlabeled=len(unlabeled)):
                record.write_text(json.dumps({**json.loads(record.read_text()),
                                              "higher_temperature_labels": higher,
                                              "second_model_labels": listed,
                                              "second_model_failures": unlabeled}))
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                self.assertIn(f"to determine whether each repository is an engineered software "
                              f"project. {sentence}", readme)

    def test_the_readme_counts_the_repositories_that_a_backup_provider_served(self):
        """The classification's record lists under `backup_provider_labels` the repositories with
        a label that a backup provider served while the pinned provider of a model failed. The
        README counts them and those that the backup providers served wholly, and points to the
        list instead of naming each repository, because through an outage of a pinned provider
        the list can hold most of the sample. The release does not contain the record, and the
        README names the archive of the pipeline that does. A record without them adds
        nothing."""
        record = self.annotated.with_suffix(".json")
        served = [{"repo_name": f"acme/widget-{number}", "github_id": number,
                   "model": "deepseek/deepseek-v4.1-flash",
                   "classifying_model": "deepseek/deepseek-v4.1-flash",
                   "providers": ["deepinfra/fp8"], "wholly": number % 3 != 0}
                  for number in range(1, 1201)]
        def pointer(each: str) -> str:
            return (" The record `repos_annotated.json`, which the classification of the "
                    f"pipeline writes beside its labels, names {each} with its backup providers "
                    "under `backup_provider_labels`. This release does not contain the record, "
                    f"and the pipeline at {_mod.PIPELINE_REPOSITORY} tracks it in the archive "
                    "`data/classification/repos_annotated.7z` with the labels that the release "
                    "was built from.")
        for listed, sentence in (
                (served, " While the pinned provider of a model failed, a backup provider of the "
                         "same weights in the same quantization served requests of 1,200 "
                         "repositories with the same settings, and every request of 800 of them."
                         + pointer("each of the repositories")),
                (served[:1], " While the pinned provider of a model failed, a backup provider "
                             "of the same weights in the same quantization served requests of 1 "
                             "repository with the same settings, and every request of 1 of them."
                             + pointer("each repository"))):
            with self.subTest(repositories=len(listed)):
                record.write_text(json.dumps({**json.loads(record.read_text()),
                                              "backup_provider_labels": listed}))
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                self.assertIn(f"to determine whether each repository is an engineered software "
                              f"project.{sentence}", readme)
                self.assertNotIn("acme/widget-1", readme)
        record.write_text(json.dumps({key: value
                                      for key, value in json.loads(record.read_text()).items()
                                      if key != "backup_provider_labels"}))
        self.assertEqual(self._run(), 0)
        self.assertNotIn("backup provider", (self.release / "0_README.md").read_text())

    def test_the_readme_defines_each_criterion_that_a_justification_names(self):
        """The verdict of a justification names the criteria that decided the label, by keys
        such as `inactive`, and the README says what each key means."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("The criteria that the verdict of a `justification` names:", readme)
        self.assertIn("- `inactive`: the repository is described as no longer developed or "
                      "maintained, or a successor is recommended to all of its new users", readme)
        self.assertIn("- `fork`: the repository is described as a fork of another project or has the "
                      "README of another project, and the fork heuristic finds", readme)

    # The justification of zzzeek/mako in run 2026-10-23T093036Z, which labeled under the
    # criterion closed_to_contributions before the classification removed it on 2026-10-31.
    MAKO = ("The README states that Mako is a Python template library and includes a usage "
            "example. The classification labels it false, because the criterion "
            "closed_to_contributions applies.")

    def test_the_readme_defines_a_removed_criterion_that_a_verdict_names(self):
        """Labels made before 2026-10-31 name the criterion closed_to_contributions, which the
        classification no longer has. The README defines the key, says that it was removed, and
        leaves it out of a release whose verdicts do not name it."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertNotIn("closed_to_contributions", readme)
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "42", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "github_id": "42", "default_branch": "trunk",
             "engineered_project": "false", "justification": self.MAKO}])
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        criteria = readme[readme.index("The criteria that the verdict of a `justification` "
                                       "names:"):]
        criteria = criteria[:criteria.index("\n### ")]
        lines = [line for line in criteria.splitlines()
                 if line.startswith("- `closed_to_contributions`: ")]
        self.assertEqual(len(lines), 1, criteria)
        self.assertIn("accepting neither pull requests nor issues from the public", lines[0])
        self.assertIn("removed this criterion on 2026-10-31", lines[0])

    def test_a_verdict_that_names_an_undefined_criterion_leaves_the_release_unchanged(self):
        """The release reads the criteria that each published verdict names. A key that the
        README does not define would reach a reader as a word without a meaning, so the release
        refuses it before anything is written."""
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "42", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "github_id": "42", "default_branch": "trunk",
             "engineered_project": "false",
             "justification": "A read-only copy. The classification labels it false, because "
                              "the criteria mirror and semi_private apply."}])
        self._assert_refused_before_any_write(
            before, "The verdict of 1 label names a criterion that the README does not define, "
                    "namely semi_private (owner/unscanned).")

    def test_the_readme_describes_how_the_memory_of_a_subagent_is_read(self):
        """Claude Code's `memory` field of an agent's front matter selects a scope: "`memory` |
        No | Persistent memory scope: `user`, `project`, or `local`"
        (https://code.claude.com/docs/en/sub-agents). The column does not read that field. It
        is TRUE when the project or the local memory directory of the agent holds a file, which
        a repository can contain without the field, and `n/a` for every agent that no
        `.claude/agents` directory holds, such as an agent of a plugin or of another tool. The
        `.claude/agents` of LedgerHQ/ledger-live links to `.agents/agents`, and the rows of its
        agents have the paths in that folder and the value FALSE."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        line = next(line for line in readme.splitlines()
                    if line.startswith("- **subagents.csv**"))
        self.assertNotIn("whether a memory file is configured", line)
        for words in ("for an agent in a `.claude/agents` directory or in the folder that "
                      "such a directory links to",
                      "TRUE when `.claude/agent-memory/<name>/` or "
                      "`.claude/agent-memory-local/<name>/`",
                      "whether or not the front matter of the agent sets `memory`",
                      "`n/a` for every other agent"):
            with self.subTest(words=words):
                self.assertIn(words, line)

    def test_the_readme_states_the_names_and_the_separator_of_the_ai_tool_columns(self):
        """The first commit of a context file can credit several tools, as `Claude,Copilot`,
        and the commit CSV names a tool as `ai_commit_patterns.csv` does, `Claude` or
        `OpenCode`, rather than by the lowercase key of `tool`. A reader who filters for
        `claude` would find no row."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        rows = {line.split("`")[1]: line for line in readme.splitlines()
                if line.startswith("| `")}
        self.assertIn("(`Claude`, `Codex`, `Copilot`, `Cursor`, `Gemini` and `OpenCode`)",
                      rows["ai_tool"])
        for column in ("first_commit_ai_tool", "ai_commit_tools"):
            with self.subTest(column=column):
                self.assertIn("by the names of `ai_tool` in `commits.csv`, joined with a comma",
                              rows[column])
                self.assertNotIn("The tool of the six", rows[column])
        self.assertIn("blank when the first commit credits none of them",
                      rows["first_commit_ai_tool"])
        self.assertIn("blank when none of these commits credits one of them",
                      rows["ai_commit_tools"])

    def test_the_readme_states_when_the_line_counts_are_blank(self):
        """GHS records no line counts for some repositories, such as dotnet/dotnet, and the
        published columns are blank for them. A reader who converts them to integers would
        otherwise fail on the blank or read it as 0."""
        self.ghs.write_text(json.dumps(
            {"_metadata": FRAME_METADATA,
             "items": [frame_item("owner/scanned", codeLines=None, blankLines=None,
                                  commentLines=None), frame_item("owner/unscanned")]}),
            encoding="utf-8")
        self.assertEqual(self._run(), 0)
        with (self.release / "repos.csv").open(newline="", encoding="utf-8") as f:
            scanned = next(row for row in csv.DictReader(f) if row["repo_name"] == "owner/scanned")
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        rows = {line.split("`")[1]: line for line in readme.splitlines()
                if line.startswith("| `")}
        for column in ("codeLines", "blankLines", "commentLines"):
            with self.subTest(column=column):
                self.assertEqual(scanned[column], "")
                self.assertIn("blank where GHS recorded no count", rows[column])

    def test_labels_without_their_record_are_refused_before_anything_is_written(self):
        self.annotated.with_suffix(".json").unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._run_main(), 1)
        self.assertIn("repos_annotated.json does not exist", err.getvalue())
        self.assertFalse((self.release / "0_README.md").exists())
        self.assertFalse((self.release / "checksums.sha256").exists())

    def test_the_readme_states_why_the_repositories_without_a_label_have_none(self):
        """A repository in the sampling frame that the classification did not label has a blank
        `engineered_project`, and `repos_excluded.csv` beside the labels gives the reason. The
        README counts these repositories by reason, the largest count first, where it describes
        the classification and the column. In the fixture, 2 of the 7 repositories are labeled,
        2 have a README that is not in English, 1 has none, GitHub did not find 1, and the
        classification reached no decision for 1. Without the counts, a reader could not tell
        why the sampling frame has 5 more rows than the labels. The reason of an undecided
        repository holds also for one that the second model never labeled."""
        names = ("owner/scanned", "owner/unscanned", "owner/russian", "owner/chinese",
                 "owner/bare", "owner/deleted", "owner/undecided")
        self.ghs.write_text(json.dumps({"_metadata": FRAME_METADATA,
                                        "items": [frame_item(n) for n in names]}),
                            encoding="utf-8")
        self._write_excluded({"owner/russian": "readme_non_english",
                              "owner/deleted": "not_found_or_no_access",
                              "owner/chinese": "readme_non_english",
                              "owner/bare": "no_readme_found",
                              "owner/undecided": "undecided",
                              "owner/elsewhere": "empty_readme"})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("2. **Classification** (2 annotated)", readme)
        self.assertIn(
            "1 was classified as an engineered project. 5 repositories in the sampling frame "
            "have no published label: 2 with a README that the language check found not to be in English, "
            "1 without a README, 1 that GitHub did not find or did not give access to, and 1 for "
            "which the classification reached no decision after its retry rounds and, where the "
            "second model applied, after the second model.\n",
            readme)
        self.assertIn("| `engineered_project` | Whether the repo was classified as an engineered "
                      "project: `true` for 1, `false` for 1. Blank for the 5 repositories in the "
                      "sampling frame without a published label, which the Provenance section "
                      "counts by reason |", readme)
        # owner/elsewhere is not in the sampling frame and has no row of repos.csv to explain.
        self.assertNotIn("empty README", readme)
        self._assert_the_classification_step_agrees_with_the_labels(readme)

    def _assert_the_classification_step_agrees_with_the_labels(self, readme: str) -> None:
        """The number of annotated repositories that the README states for the classification
        step is the number of rows of the release's `repos.csv` with a label and the sum of the
        labels that the description of `engineered_project` states, and the count of the
        engineered projects in that step is the number of those labels that are `true`."""
        with (self.release / "repos.csv").open(encoding="utf-8", newline="") as f:
            labels = [row["engineered_project"] for row in csv.DictReader(f)
                      if row["engineered_project"]]
        step = re.search(r"^2\. \*\*Classification\*\* \(([\d,]+) annotated\): (.*)$", readme,
                         re.M)
        annotated = int(step[1].replace(",", ""))
        engineered = re.search(r"(?:Of the ([\d,]+) annotated repositor(?:y|ies), )?\b([\d,]+) "
                               r"(?:was classified as an engineered project|were classified as "
                               r"engineered projects)\.", step[2])
        row = re.search(r"^\| `engineered_project` \| [^:|]*: ([^.|]*)", readme, re.M)
        stated = {label: int(count.replace(",", ""))
                  for label, count in re.findall(r"`(\w+)` for ([\d,]+)", row[1])}
        self.assertEqual(annotated, len(labels))
        self.assertEqual(sum(stated.values()), annotated)
        self.assertEqual(stated.get("true", 0), labels.count("true"))
        self.assertEqual(int(engineered[2].replace(",", "")), stated.get("true", 0))
        if engineered[1]:
            self.assertEqual(int(engineered[1].replace(",", "")), annotated)

    def test_the_readme_says_that_the_classification_labeled_a_copy_true(self):
        """The classification labels a copy at the head commit of an engineered project in the
        sample `true` and leaves it out under the reason `copy`. Its `engineered_project` is
        blank, and the README does not say that the classification did not label it. The README
        counts the engineered projects among the annotated repositories, so that its counts
        agree with the labels that it states for `engineered_project`, and names the copy as
        labeled `true` among the repositories without a published label."""
        names = ("owner/scanned", "owner/unscanned", "owner/copy")
        self.ghs.write_text(json.dumps({"_metadata": FRAME_METADATA,
                                        "items": [frame_item(n) for n in names]}),
                            encoding="utf-8")
        self._write_excluded({"owner/copy": "copy"})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("Of the 2 annotated repositories, 1 was classified as an engineered "
                      "project. 1 repository in the sampling frame has no published label: 1 "
                      "that the classification labeled `true` and left out of the sample, "
                      "because the sample keeps another repository at the same head commit.",
                      readme)
        self.assertIn("Blank for the 1 repository in the sampling frame without a published "
                      "label, which the Provenance section counts by reason |", readme)
        self.assertNotIn("did not label", readme[readme.index("`engineered_project`"):])
        self._assert_the_classification_step_agrees_with_the_labels(readme)

    def test_a_readme_without_unlabeled_repositories_states_no_reasons(self):
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("1 was classified as an engineered project.\n", readme)
        self.assertIn("| `engineered_project` | Whether the repo was classified as an engineered "
                      "project: `true` for 1, `false` for 1 |", readme)
        self._assert_the_classification_step_agrees_with_the_labels(readme)

    def test_a_repository_without_a_label_or_a_reason_leaves_the_release_unchanged(self):
        """Labels and exclusions from different runs of the classification can leave a
        repository in the sampling frame without a label and without a reason. The README would
        count it without saying why it has no label."""
        self.assertEqual(self._run(), 0)
        before = self._release_state()
        self.ghs.write_text(json.dumps(
            {"_metadata": FRAME_METADATA,
             "items": [frame_item(n) for n in (*self.REPOS, "owner/new", "owner/bare")]}),
            encoding="utf-8")
        self._write_excluded({"owner/bare": "no_readme_found"})
        self._assert_refused_before_any_write(
            before, "1 repository in the sampling frame has neither a label nor a row in "
                    "repos_excluded.csv (owner/new)")

    def test_labels_without_their_exclusions_are_refused_before_anything_is_written(self):
        self.annotated.with_name("repos_excluded.csv").unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self._run_main(), 1)
        self.assertIn("repos_excluded.csv does not exist", err.getvalue())
        self.assertFalse((self.release / "0_README.md").exists())
        self.assertFalse((self.release / "checksums.sha256").exists())

    def test_the_readme_states_what_is_withheld_and_why(self):
        self._run()
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        for name, reason in _mod.WITHHELD.items():
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", readme)
                self.assertIn(reason.split(":")[0], readme)

    def test_the_readme_does_not_call_the_withheld_csvs_all_that_the_release_leaves_out(self):
        """`output/` holds more files than the release publishes or withholds, such as
        `ai_commits-repos.csv` and the reading lists of the heuristics. The README introduces
        the withheld CSVs without a count that would claim them to be all, and it says that the
        release publishes none of the other files of that directory and where they are
        described."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("The release withholds these pipeline CSVs:\n\n- `",
                      readme)
        self.assertIn("Other files in the pipeline output directory support building and checking "
                      "the dataset but are not inputs to any release file. They are not published. "
                      "`OUTPUT_INVENTORY` in "
                      "`agentcontext/steps/release.py` at https://github.com/se-uhd/"
                      "agenttorrent-pipeline describes each one.", readme)
        self.assertNotIn("Two CSVs", readme)

    def test_the_readme_states_where_a_line_of_each_line_count_ends(self):
        """The detection step ends a line at LF, CRLF or CR. Python's `str.splitlines` also
        ends a line at a form feed or U+2028, and a reader who counts the lines with it would
        count more lines than the column for a file with such a character."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        rule = ("Lines end at LF, CRLF or CR, not at another character, such as a form feed or "
                "U+2028 |")
        for heading, column in (("### context_files.csv", "line_count"),
                                ("### imports.csv", "target_line_count"),
                                (f"### {planning_schema.CSV_NAME}", "line_count")):
            with self.subTest(heading=heading):
                section = readme[readme.index(heading) + len(heading):]
                section = section[:section.index("\n#")]
                row = next(line for line in section.split("\n")
                           if line.startswith(f"| `{column}` |"))
                self.assertTrue(row.endswith(rule), row)

    def test_the_readme_gives_an_extraction_command_for_every_archive(self):
        """The README lists the archives of the release in a table, and the text below the
        table tells how to extract each of them. Without a command for `imported_files.7z`, a
        reader cannot tell whether it needs the switch for links that `repos_data.7z` needs."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        section = readme.split("## Compressed Archives", 1)[1].split("\n## ", 1)[0]
        archives = re.findall(r"^\| `([^`]+\.7z)` \|", section, re.MULTILINE)
        self.assertEqual(set(archives), {"repos.csv.7z", "commits.csv.7z", "repos_data.7z",
                                         "imported_files.7z", "deletion.csv.7z"})
        prose = " ".join(line for line in section.splitlines() if not line.startswith("|"))
        sentences = re.split(r"(?<=[.:])\s+", prose)
        for archive in archives:
            with self.subTest(archive=archive):
                self.assertTrue(any(archive in sentence and "`7z x" in sentence
                                    for sentence in sentences), prose)

    def test_the_readme_describes_when_the_text_columns_are_blank(self):
        """The detection step leaves `line_count`, `is_reference` and `referenced_file` of a
        context file blank when the file is no text, such as a CLAUDE.md that git-crypt
        encrypted, or could not be read, and the `language` of a skill, a subagent, a command or
        a rule blank when it is a link to a file that the collection did not copy. A reader who
        converts a blank `is_reference` to a boolean would otherwise count it as FALSE."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        section = readme[readme.index("### context_files.csv"):]
        section = section[:section.index("\n### ")]
        for row in ("| `language` | Detected natural language of the file content, `n/a` for a "
                    "text without a detectable language and for a file that is not text, such as "
                    "one that git-crypt encrypted, and blank for a file that could not be read.",
                    "| `line_count` | Number of lines in the file, blank for a file that is not "
                    "text, such as one that git-crypt encrypted, and for a file that could not be "
                    "read.",
                    "| `is_reference` | Whether the file is a reference to another file, blank "
                    "where `line_count` is blank. A blank value is unknown, not FALSE.",
                    "| `referenced_file` | Path of the referenced file if `is_reference` is TRUE, "
                    "blank otherwise."):
            with self.subTest(row=row):
                self.assertIn(row, section)
        self.assertIn("`language` is the detected natural language of the configuration "
                      "artifact, `n/a` for a text without a detectable language and for a file "
                      "that is not text, and blank for a link to a file that the collection did "
                      "not copy and for a file that could not be read.", readme)

    def test_the_readme_describes_a_context_file_that_is_a_symbolic_link(self):
        """gomlx/gomlx's `.agents/CLAUDE.md` is a symbolic link to the `AGENTS.md` beside it,
        and `repos_data.7z` stores it as a link. The detection step reads such a context file as
        the one-line file that names its target, so its row has `line_count` 1, `is_reference`
        TRUE, `referenced_file` `AGENTS.md` and `language` `n/a`. A reader who counts the lines of
        the extracted link reads the lines of AGENTS.md through it, and the README tells the
        reader why the column differs."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        section = readme[readme.index("### context_files.csv"):]
        section = section[:section.index("\n### ")]
        rows = {line.split("`")[1]: line for line in section.splitlines()
                if line.startswith("| `")}
        expected = {
            "line_count": "A context file that is a symbolic link counts one line, the path of "
                          "its target, whether `repos_data.7z` holds the link or a one-line file "
                          "that names the target. A count of the lines of such a link extracted "
                          "from the archive, such as with `wc -l`, reads its target and differs.",
            "is_reference": "A context file that is a symbolic link to a path that ends in `.md` "
                            "is a reference to that path |",
            "referenced_file": "For a context file that is a symbolic link, the path of its "
                               "target as the link names it |",
            "language": "A context file that is a symbolic link has the language detected in "
                        "the path of its target, `n/a` for a short path such as `AGENTS.md` |",
        }
        for column, sentence in expected.items():
            with self.subTest(column=column):
                self.assertIn(sentence, rows[column])

    def test_the_readme_names_every_file_whose_checksum_provenance_json_records(self):
        """`provenance.json` records the SHA-256 of every heuristics file that the detection ran
        with, under a key that ends in `_sha256` beside a key that names the file. The README's
        description of `provenance.json` names each of these files."""
        self.assertEqual(self._run(), 0)
        provenance = json.loads((self.release / "provenance.json").read_text(encoding="utf-8"))
        files = [provenance[key.removesuffix("_sha256")] for key in provenance
                 if key.endswith("_sha256")]
        self.assertEqual(len(files), 4)
        row = self._readme_row("provenance.json")
        for name in files:
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", row)
        self.assertIn("SHA-256", row)

    def _readme_row(self, name: str) -> str:
        """The row of the README's provenance table for the file `name`."""
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        return next(line for line in readme.splitlines() if line.startswith(f"| `{name}` |"))

    def test_the_readme_counts_the_ai_columns_for_the_six_tools_alone(self):
        """The four AI columns of context_files.csv read ai_commit_patterns.csv alone. The first
        commit of PrefectHQ/prefect's src/prefect/runner/AGENTS.md credits Devin, and
        other_commits.csv lists it, so the file has first_commit_ai_created FALSE. The README
        says so."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        for column in ("first_commit_ai_created", "first_commit_ai_tool", "#ai_commits",
                       "ai_commit_tools"):
            with self.subTest(column=column):
                row = next(line for line in readme.splitlines()
                           if line.startswith(f"| `{column}` |"))
                self.assertIn("of the six", row)
                self.assertNotIn("an AI coding tool", row)

    def test_the_readme_names_every_tool_with_commands_and_every_kind_of_other_commit(self):
        """commands.csv holds the command files of every tool with a `commands` row in
        config_path_patterns.csv, OpenCode's among them, and other_commits.csv the commits that
        review apps' co-author trailers and the rows of the message body credit."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        row = next(line for line in readme.splitlines() if line.startswith("| `commands.csv` |"))
        labels = {"copilot": "Copilot", "cursor": "Cursor", "gemini": "Gemini",
                  "opencode": "OpenCode", "claude": "Claude", "codex": "Codex"}
        tools = {r["tool"] for r in heuristics.load_config_path_patterns()
                 if r["mechanism"] == "commands"}
        for tool in sorted(tools):
            with self.subTest(tool=tool):
                self.assertIn(labels[tool], row)
        section = readme.split("### other_commits.csv", 1)[1].split("\n### ", 1)[0]
        rows = commits.OTHER_AI_COMMIT_PATTERNS
        if any(r["attribution"] == "reviewer" for r in rows):
            self.assertIn("review app's suggestion", section)
        if any(r["scope"] == "full_body" for r in rows):
            self.assertIn("in the message body", section)

    def test_the_readme_defines_the_ai_tool_values_that_name_no_product(self):
        """In other_commits.csv, `ai_tool` is `Gemini (unspecified)` for a credit that names Gemini
        but none of Gemini CLI, Gemini Code Assist and Antigravity, such as `Generated-by: Gemini
        Pro`, and `tool not named` for a credit to AI that names no tool. The README defines each
        such value of `ai_tool`."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        section = readme.split("### other_commits.csv", 1)[1].split("\n### ", 1)[0]
        values = {r["tool"] for r in commits.OTHER_AI_COMMIT_PATTERNS
                  if r["tool"] == commits.UNNAMED_TOOL or "(unspecified)" in r["tool"]}
        self.assertIn("Gemini (unspecified)", values)
        for value in sorted(values):
            with self.subTest(value=value):
                self.assertIn(f"`{value}`", section)

    def test_the_readme_describes_the_scan_window_of_snapshot_json(self):
        """snapshot.json records no sampling window. Its `scanned_at_window` spans the scans of
        every collection that a taken-over row comes from, here a row of September beside the
        rows of October. The README describes `scanned_at_window` as this span."""
        self._write_snapshot(collection={
            "counts": {"sampled": 2, "clone_failed": 0},
            "scanned_at_window": {"first": "2026-09-24T01:00:00Z",
                                  "last": "2026-10-19T03:00:00Z"}})
        self.assertEqual(self._run(), 0)
        published = json.loads((self.release / "snapshot.json").read_text(encoding="utf-8"))
        self.assertIn("scanned_at_window", published["collection"])
        self.assertNotIn("sampling_window", published["collection"])
        row = self._readme_row("snapshot.json")
        self.assertIn("`scanned_at_window`", row)
        self.assertIn("took over from an earlier collection", row)
        self.assertNotIn("its sampling window", row)

    def test_the_readme_names_the_files_that_an_antigravity_exclude_leaves_in_other_csv(self):
        """The top-level `exclude` of an Antigravity registration file can leave out an item of
        a default place. The detection step writes such an item into other.csv, although a
        detection heuristic matches its path. The README's reason for withholding other.csv
        has to name these files too."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        withheld = next(line for line in readme.splitlines() if line.startswith("- `other.csv`:"))
        self.assertIn("`exclude` of an Antigravity registration file", withheld)
        self.assertNotIn("paths that no detection heuristic explains", withheld)

    def test_the_readme_discloses_incomplete_commit_reclassification(self):
        """The commit reclassification leaves out of the commit CSVs every shard with commits
        that other commit heuristics classified, and the release step refuses a snapshot that
        records a classification under other commit heuristics. The README therefore states an
        incomplete reclassification as missing evidence, not as published labels that the
        current heuristics did not validate."""
        self._write_snapshot(
            commit_reclassification={"complete": False, "counts": {"no_log": 2}},
            metadata={"rows_without_commit_metadata": {"total": 3}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("Commit reclassification is incomplete, because the commit evidence of some "
                      "repositories is missing or incomplete. Even so, every commit in "
                      "`commits.csv` and `other_commits.csv` was classified with the current "
                      "commit heuristics.",
                      readme)
        self.assertNotIn("archived commit labels", readme)
        self.assertIn("2 repositories lack commit logs", readme)
        self.assertIn("3 configuration rows have missing commit metadata", readme)

    def test_the_readme_names_every_incomplete_reclassification_outcome(self):
        # The README names every incomplete outcome, not only a missing log. A reclassification
        # that is incomplete because one history was not fetched, or because one repository's
        # evidence could not be processed, then publishes its reason.
        from agentcontext.steps import commit_reclassification
        for number, outcome in enumerate((*commit_reclassification.INCOMPLETE_OUTCOMES, "failed"), 2):
            with self.subTest(outcome=outcome):
                self._write_snapshot(commit_reclassification={
                    "complete": False, "population_complete": False,
                    "counts": {"current": 1, outcome: number, "unavailable": 7}})
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                self.assertIn(f"{number} {_mod.RECLASSIFICATION_GAPS[outcome][1]}.", readme)
                self.assertIn("7 repositories in the sample have a recorded clone or "
                              "collection failure", readme)

    def test_an_unread_commit_history_is_not_called_unfetched(self):
        """`history_not_read` counts both a history that the fetch did not retrieve and a
        history that was fetched and could not be read (`history_read_failed`). The README
        names both causes."""
        self._write_snapshot(commit_reclassification={
            "complete": False, "counts": {"current": 1, "history_not_read": 2}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("2 repositories lack their commit history, because the cloning step could "
                      "not fetch or read it.", readme)
        self.assertNotIn("not fetched", readme)

    def test_the_readme_states_the_repositories_left_out_of_the_commit_csvs(self):
        """The commit reclassification and the detection step both leave the shards of a
        repository with failed evidence out of the commit CSVs, and none of the repository's
        commits is published. The README states this in the limit of the repository's outcome.
        When the snapshot counts no left-out repository for an outcome, the README contains no
        such sentence."""
        self._write_snapshot(commit_reclassification={
            "complete": False, "counts": {"current": 3, "failed": 1}, "left_out": {}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("1 repository could not be reclassified", readme)
        self.assertNotIn("contains any of", readme)
        self._write_snapshot(commit_reclassification={
            "complete": False, "counts": {"current": 3, "failed": 1}, "left_out": {"failed": 1}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("## Evidence limits", readme)
        self.assertIn("1 repository could not be reclassified, because its evidence could not be "
                      "processed. Neither `commits.csv` nor `other_commits.csv` contains any of "
                      "its commits. The repository is `owner/failed-1`.\n", readme)

    def test_the_readme_states_that_a_repository_without_a_record_has_no_published_commits(self):
        """A repository without a per-repository commit record has no shard, so the commit
        reclassification leaves none out, and none of its commits is in the commit CSVs. The limit
        of its outcome in the README says so, as it has to for
        imperugo/StackExchange.Redis.Extensions, which has no such record."""
        self._write_snapshot(commit_reclassification={
            "complete": False, "population_complete": False,
            "counts": {"reclassified": 9, "missing_record": 1},
            "left_out": {"missing_record": 0}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("1 repository lacks a per-repository commit record. Neither `commits.csv` "
                      "nor `other_commits.csv` contains any of its commits.", readme)

    def test_the_readme_names_the_repositories_of_each_evidence_limit(self):
        """A user who counts AI commits per repository has to leave out the repositories
        without published commits, which repos.csv lists among the configured repositories. The
        README names the repositories of each incomplete outcome in the limit of that outcome,
        and the clone failures in their own limit, as the snapshot lists them."""
        self._write_snapshot(commit_reclassification={
            "complete": False, "population_complete": False,
            "counts": {"reclassified": 9, "no_path_history": 2, "missing_record": 1,
                       "unavailable": 1},
            "left_out": {"no_path_history": 2},
            "repositories": {"no_path_history": ["AztecProtocol/aztec-packages", "coder/coder"],
                             "missing_record": ["imperugo/StackExchange.Redis.Extensions"],
                             "unavailable": ["owner/gone"]}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("2 repositories lack a complete commit history of their configuration "
                      "files. Neither `commits.csv` nor `other_commits.csv` contains any of their "
                      "commits, because other commit heuristics classified the commits and the "
                      "current heuristics could not classify them again. The repositories are "
                      "`AztecProtocol/aztec-packages` and `coder/coder`.\n", readme)
        self.assertIn("1 repository lacks a per-repository commit record. Neither `commits.csv` "
                      "nor `other_commits.csv` contains any of its commits. The repository is "
                      "`imperugo/StackExchange.Redis.Extensions`.\n", readme)
        self.assertIn("1 repository in the sample has a recorded clone or collection failure, and "
                      "neither its configuration nor its commits are included. The repository "
                      "is `owner/gone`.\n", readme)

    def test_a_snapshot_that_does_not_name_the_repositories_of_a_limit_is_refused(self):
        """A snapshot that names no repositories for a counted outcome, or fewer repositories
        than it counts, cannot give the README the names in the limit of that outcome."""
        for named in ({}, {"no_path_history": ["coder/coder"]}):
            with self.subTest(named=named):
                self._write_snapshot(commit_reclassification={
                    "complete": False, "counts": {"reclassified": 9, "no_path_history": 2},
                    "left_out": {"no_path_history": 2}, "repositories": named})
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self._run_main(), 1)
                self.assertIn("commit_reclassification.repositories", err.getvalue())
                self.assertIn("no_path_history", err.getvalue())
                self.assertIn("snapshot write", err.getvalue())
                self.assertFalse((self.release / "0_README.md").exists())

    def test_the_readme_states_the_context_files_with_blank_ai_columns(self):
        """The detection step leaves the AI columns of a context file blank where the current
        commit heuristics did not classify the commits that touched the file, and a reader of the
        CSV could take a blank for `FALSE`. The README counts these rows among its evidence limits
        and says in the description of the columns that they can be blank. A release without such
        a row states no such limit."""
        filled = {c: f"{c}-0" for c in _mod.CONTEXT_FILES_COLUMN_ORDER}
        rows = [filled | {"repo_name": "owner/scanned", "context_file": "CLAUDE.md"}]
        rows += [filled | {"repo_name": "owner/scanned", "context_file": f"sub{i}/CLAUDE.md"}
                 | {c: "" for c in heuristics.AI_COLUMNS} for i in (1, 2)]
        for blank in (False, True):
            with self.subTest(blank=blank):
                write_csv(self.output / "context-files.csv", _mod.CONTEXT_FILES_COLUMN_ORDER,
                          rows if blank else rows[:1])
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                limit = ("2 rows of `context_files.csv`, in 1 repository, have blank "
                         "`first_commit_ai_created`, `first_commit_ai_tool`, `#ai_commits` and "
                         "`ai_commit_tools`, because the current commit heuristics did not "
                         "classify the commits that touched these files. A blank in these "
                         "columns means that the value is unknown, not that it is `FALSE` or 0.")
                self.assertEqual(limit in readme, blank)
                self.assertEqual("## Evidence limits" in readme, blank)
        self.assertIn("Blank where the current commit heuristics did not classify the commits "
                      "that touched the file", self._readme_row("first_commit_ai_created"))
        self.assertIn("Blank where `first_commit_ai_created` is blank",
                      self._readme_row("#ai_commits"))

    def test_the_readme_defines_the_attributions_of_an_author_or_a_committer(self):
        """An agent account or an app that its own page or its project's own configuration
        grounds as running a tool, such as Cloudflare's ask-bonk[bot] for OpenCode or the app
        under which UKGovernmentBEIS/inspect_evals runs Claude Code's GitHub Action, credits the
        tool as `account`, and a name or an address that names the tool and that users chose for
        an author or a committer, such as `Edgar (OpenClaw)`, credits it as `convention`. An
        address that GitHub links to a vendor's account counts as `account` only when the tool's
        code or environment writes it, as the environment of Codex cloud writes codex@openai.com.
        The README's description of `attributions` and its list of the kinds of heuristic of
        other_commits.csv state these definitions."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        attributions = next(line for line in readme.splitlines()
                            if line.startswith("| `attributions` |"))
        self.assertIn("the identity (name and address together) that the tool's code or "
                      "environment writes", attributions)
        self.assertIn("or an agent account or a GitHub App that its own page or the configuration "
                      "of its project identifies as running the tool", attributions)
        self.assertIn("An address that GitHub links to a vendor's account is `account` only when "
                      "the tool's code or environment writes it.", attributions)
        self.assertIn("and any other name or address that names the tool and that users chose for "
                      "an author or a committer", attributions)
        other = readme[readme.index("### other_commits.csv"):readme.index("### context_files.csv")]
        self.assertIn("or an agent account that its own page identifies as running the tool",
                      other)
        self.assertIn("or a name or an address that names the tool and that users chose for an "
                      "author or a committer", other)

    def test_the_readme_describes_every_label_of_engineered_project(self):
        """A user who reads `false` as not engineered would miss the rows labeled `unclear`,
        so the description of `engineered_project` names each label with its count and states
        when each label is given, including when the model itself judged the evidence unclear."""
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "42", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"},
            {"repo_name": "owner/unscanned", "github_id": "42", "default_branch": "trunk",
             "engineered_project": "unclear", "justification": "the evidence is unclear"},
        ])
        self.assertEqual(self._run(), 0)
        row = self._readme_row("engineered_project")
        self.assertIn("`true` for 1, `unclear` for 1.", row)
        self.assertIn("Otherwise it is `unclear` when one of these is unclear or the model judged "
                      "the evidence of software engineering practices unclear", row)

    def test_the_usage_example_selects_the_engineered_projects_without_an_unclear_label(self):
        """The labels of the fixture are `true` and `false` alone, as in a run in which the model
        gave no label `unclear`. pandas then reads `engineered_project` as booleans unless the
        example reads the column as text. The usage example of the README, run in the release,
        selects the repositories that the published `repos.csv` labels `true`."""
        self.assertEqual(self._run(), 0)
        _, rows = self._published_rows("repos.csv")
        labels = [row["engineered_project"] for row in rows]
        self.assertEqual(sorted(labels), ["false", "true"])
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        usage = readme[readme.index("## Usage Examples"):]
        code = usage[usage.index("```python\n") + len("```python\n"):usage.index("\n```\n")]
        result = subprocess.run([sys.executable, "-c", code + "\nprint(len(engineered))"],
                                cwd=self.release, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[-1], "1")

    def test_the_readme_counts_each_left_out_repository_once(self):
        """A repository with commits that other commit heuristics classified and evidence that
        keeps the current heuristics from classifying them again is left out of the commit
        CSVs, and it has an incomplete outcome. The README names it in the limit of that outcome,
        not in a second limit of its own, which a reader would add to the first. For example, 55
        repositories without a complete commit history of their configuration files, all of them
        left out, are 55 in the README and not 110. Of 3 repositories without commit logs, 2 are
        left out here, and that limit names the 2 among the 3."""
        self._write_snapshot(commit_reclassification={
            "complete": False, "population_complete": False,
            "counts": {"reclassified": 9, "no_path_history": 55, "no_log": 3, "unavailable": 2},
            "left_out": {"no_path_history": 55, "no_log": 2}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        limits = readme.split("## Evidence limits\n\n", 1)[1].split("\n## ", 1)[0]
        # Remove the sentences that name the repositories. The placeholder names that
        # `_write_snapshot` gives, such as `owner/no_path_history-55`, would change the paragraphs
        # and the count of 55 below.
        limits = re.sub(r" The repositor(?:y is|ies are) [^\n]*", "", limits)
        paragraphs = limits.strip().split("\n\n")
        self.assertIn(
            "55 repositories lack a complete commit history of their configuration files. "
            "Neither `commits.csv` nor `other_commits.csv` contains any of their commits, because "
            "other commit heuristics classified the commits and the current heuristics could not "
            "classify them again.", paragraphs)
        self.assertIn(
            "3 repositories lack commit logs. Neither `commits.csv` nor `other_commits.csv` "
            "contains any of the commits of 2 of them, because other commit heuristics classified "
            "the commits and the current heuristics could not classify them again.", paragraphs)
        # Every count of repositories in the limits is the count of one outcome, each stated
        # once: the 55, the 3 without commit logs and the 2 clone failures.
        stated = [int(n.replace(",", "")) for n in re.findall(r"(?<![\w.])(\d[\d,]*) repositor",
                                                                limits)]
        self.assertEqual(sorted(stated), [2, 3, 55])
        self.assertEqual(limits.count("55"), 1)

    def test_a_snapshot_with_a_single_count_of_left_out_repositories_is_refused(self):
        """The README states the repositories without published commits in the limit of their
        outcome, and a single count in `left_out` does not say which outcome each of them has."""
        self._write_snapshot(commit_reclassification={
            "complete": False, "counts": {"current": 3, "failed": 1}, "left_out": 1})
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self._run_main(), 1)
        self.assertIn("commit_reclassification.left_out", err.getvalue())
        self.assertIn("snapshot write", err.getvalue())
        self.assertFalse((self.release / "0_README.md").exists())

    def test_a_repository_without_its_collected_tree_is_not_counted_as_scanned(self):
        """A repository with the outcome `missing_tree` was cloned, and the detection step read
        none of its files. A README that counted it among the repositories that git could clone
        would state that it was scanned."""
        self._write_snapshot(
            collection={"counts": {"sampled": 5, "clone_failed": 1}},
            commit_reclassification={"complete": False, "population_complete": False,
                                     "counts": {"current": 1, "missing_tree": 2,
                                                "unavailable": 1}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("Of the 5 repositories in the sample, 2 were cloned and scanned for "
                      "configuration artifacts, git could not clone 1, and 2 were not scanned "
                      "because their collected files were missing.", readme)

    def test_the_readme_states_the_rows_that_github_left_unresolved(self):
        """The filter's step `gone` removes the rows of the retrieval for which the GitHub query
        got no response in any round, and `sampling.json` records their number. Because the
        step runs before the filter's other steps, the README states the count as rows that the
        first step removed rather than as repositories that the sampling frame lost. A release
        over a sampling frame without the count, or with a count of 0, states no such limit."""
        self.assertEqual(self._run(), 0)
        self.assertNotIn("no response", (self.release / "0_README.md").read_text())
        for unresolved in (0, 3):
            with self.subTest(unresolved=unresolved):
                stages = [{"stage": 1, "key": "gone", "removed": 1 + unresolved,
                           "remaining": 2, "values": {"unresolved": unresolved}}]
                self.ghs.write_text(json.dumps(
                    {"_metadata": {**FRAME_METADATA, "stages": stages},
                     "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                sentence = ("The filter removed 3 rows of the GHS retrieval in its first step, "
                            "`gone`, because GitHub's API returned no response for them in any "
                            "round of the GitHub query. Whether GitHub still has these "
                            "repositories is unknown. That step runs before the filter's other "
                            "steps, and the count includes rows that those steps would have "
                            "removed.")
                if unresolved:
                    self.assertIn("## Evidence limits", readme)
                    self.assertIn(sentence, readme)
                else:
                    self.assertNotIn("no response", readme)

    def test_the_readme_states_a_sampling_frame_from_a_ghs_download_that_fell_back(self):
        """A GHS download that falls back keeps the previous sampling frame, and the filter does
        not run again. Only the download's report beside the sampling frame then shows that the
        window did not move. The README states the retrieval, the retrieval that the download
        attempted, the reason and how many downloads in a row fell back. A report of a retrieval
        that the download took, or of a fallback to another retrieval than the
        sampling frame's, states no such limit. A later Start that runs the release again, such
        as after an image update, reads the same report. The limit therefore names the
        retrievals rather than "this run"."""
        report = self.ghs.parent / "ghs_retrieval_report.json"
        reason = ("the GHS search result has 150,000 repositories, fewer than 90% of the 205,697 "
                  "of retrieval 2026-10-05T120000Z, which the stage read before, for the same "
                  "query")
        sentence = ("The sampling frame comes from the GHS retrieval `2026-10-05T120000Z`, which "
                    "an earlier GHS download took. The GHS download of the retrieval "
                    "`2026-12-01T010000Z` failed and fell back to it, and the sampling window "
                    "did not move. The download gave this reason: The GHS search result has "
                    "150,000 repositories, fewer than 90% of the 205,697 of retrieval "
                    "2026-10-05T120000Z, which the stage read before, for the same query. The "
                    "GHS search result that the download refused had 150,000 repositories. 3 "
                    "downloads in a row have fallen back to that retrieval.")
        for outcome, retrieval, stated in (("fallback", "2026-10-05T120000Z", True),
                                           ("retrieved", "2026-10-05T120000Z", False),
                                           ("fallback", "2026-09-01T000000Z", False)):
            with self.subTest(outcome=outcome, retrieval=retrieval):
                report.write_text(json.dumps({
                    "outcome": outcome, "retrieval_id": retrieval,
                    "failed_retrieval_id": "2026-12-01T010000Z", "window_moved": False,
                    "reason": reason, "fallbacks_in_a_row": 3, "failed_item_count": 150000}),
                    encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                self.assertEqual(sentence in readme, stated)
                self.assertEqual("## Evidence limits" in readme, stated)
                self.assertNotIn("this run", readme)
        report.write_text("{", encoding="utf-8")
        self.assertEqual(self._run_main(), 1)

    def test_the_readme_states_a_retrieval_that_the_download_took_although_it_was_smaller(self):
        """`--accept-smaller` takes a GHS search result that the size check of the GHS download
        refuses, and the report records the reason as `accepted_smaller`. The sampling frame
        then comes from a retrieval with far fewer rows than the retrieval before it.
        `sampling.json` does not show this difference, and the README therefore states it with
        the counts that the report records. A report of a retrieval that the size check let
        pass, or of another retrieval than the sampling frame's, states no such limit."""
        report = self.ghs.parent / "ghs_retrieval_report.json"
        reason = ("the GHS search result has 150,000 repositories, fewer than 90% of the 205,697 "
                  "of retrieval 2026-09-05T120000Z, which the stage read before, for the same "
                  "query")
        sentence = ("The sampling frame comes from the GHS retrieval `2026-10-05T120000Z` of "
                    "150,000 repositories. The size check of the GHS download refused it as too "
                    "small compared with the retrieval `2026-09-05T120000Z` that the sampling "
                    "stage read before, and the download took it with `--accept-smaller`. The "
                    "sampling frame can therefore lack repositories that the earlier retrieval "
                    "had and that still meet its criteria. The download gave this reason: The "
                    "GHS search result has 150,000 repositories, fewer than 90% of the 205,697 "
                    "of retrieval 2026-09-05T120000Z, which the stage read before, for the same "
                    "query.")
        for accepted, retrieval, stated in ((reason, "2026-10-05T120000Z", True),
                                            (None, "2026-10-05T120000Z", False),
                                            (reason, "2026-11-05T120000Z", False)):
            with self.subTest(accepted=bool(accepted), retrieval=retrieval):
                report.write_text(json.dumps({
                    "outcome": "retrieved", "retrieval_id": retrieval, "item_count": 150000,
                    "window_moved": True, "previous_retrieval_id": "2026-09-05T120000Z",
                    **({"accepted_smaller": accepted} if accepted else {})}), encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                self.assertEqual(sentence in readme, stated)
                self.assertEqual("## Evidence limits" in readme, stated)

    def test_the_readme_states_the_dates_that_git_printed_in_a_form_that_does_not_parse(self):
        """The detection step leaves `head_commit_date` and `commit_timestamp` blank where git
        printed a date that does not parse, such as for a commit of the year 10000. The README
        counts the repositories and the commits with such a blank among its evidence limits and
        says in the column descriptions when the date is blank. A repository that was not
        scanned has no `head_sha` and no date, and it is not counted."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertNotIn("a blank `head_commit_date`", readme)
        self.assertNotIn("a blank `commit_timestamp`", readme)
        write_csv(self.output / "repos.csv", DETECTED_HEADER,
                  [detected_row("owner/scanned", context_file="TRUE", claude="TRUE",
                                head_commit_date="")])
        write_csv(self.output / "all_ai_commits.csv", list(_mod.commits.COMMIT_COLUMNS),
                  [{"repo_name": "owner/scanned", "commit_timestamp": timestamp, "branch": "main",
                    "commit_sha": sha * 40, "github_link": "https://github.com/owner/scanned",
                    "commit_message": "a commit", "ai_tool": "claude",
                    "matched_heuristics": "claude-trailer-01", "attributions": "claude:convention"}
                   for timestamp, sha in (("", "b"), ("", "c"), ("2026-01-01T00:00:00Z", "d"))])
        self._write_commits(["Co-authored-by: Amp <amp@ampcode.com>"],
                            name="other_ai_commits.csv", tool="Amp")
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("## Evidence limits", readme)
        self.assertIn("1 repository in `repos.csv` has a blank `head_commit_date`, because git "
                      "printed the committer date of its `head_sha` in a form that does not "
                      "parse, such as for a commit of the year 10000.", readme)
        self.assertIn("2 commits in `commits.csv` have a blank `commit_timestamp`, because git "
                      "printed their dates in a form that does not parse, such as for a commit "
                      "of the year 10000.", readme)
        note = "Blank where git printed it in a form that does not parse"
        for column in ("head_commit_date", "commit_timestamp"):
            with self.subTest(column=column):
                rows = [line for line in readme.splitlines() if line.startswith(f"| `{column}` |")]
                self.assertTrue(rows)
                self.assertTrue(all(note in row for row in rows), rows)

    def test_every_count_sentence_of_the_readme_agrees_with_a_count_of_one(self):
        """A count of 1 takes the singular in every sentence of the README that states a count,
        in the description of the levels and in every evidence limit. Here each count is 1: a
        sampling frame of one repository, which was labeled, engineered and scanned, and one
        repository, row or commit behind each limit."""
        self.ghs.write_text(json.dumps({
            "_metadata": {**FRAME_METADATA, "stages": [
                {"stage": 1, "key": "gone", "removed": 2, "remaining": 1,
                 "values": {"unresolved": 1, "without_id": 1}}]},
            "items": [frame_item("owner/scanned")]}), encoding="utf-8")
        write_csv(self.annotated, _mod.ANNOTATED_COLUMNS, [
            {"repo_name": "owner/scanned", "github_id": "42", "default_branch": "main",
             "engineered_project": "true", "justification": "it builds"}])
        record = self.annotated.with_suffix(".json")
        record.write_text(json.dumps({**json.loads(record.read_text()), "undecided": 1}))
        write_csv(self.output / "repos.csv", DETECTED_HEADER,
                  [detected_row("owner/scanned", context_file="TRUE", claude="TRUE",
                                head_commit_date="")])
        write_csv(self.output / "all_ai_commits.csv", list(_mod.commits.COMMIT_COLUMNS),
                  [{"repo_name": "owner/scanned", "commit_timestamp": "", "branch": "main",
                    "commit_sha": "b" * 40, "github_link": "https://github.com/owner/scanned",
                    "commit_message": "a commit", "ai_tool": "claude",
                    "matched_heuristics": "claude-trailer-01", "attributions": "claude:convention"}])
        write_csv(self.output / "context-files.csv", _mod.CONTEXT_FILES_COLUMN_ORDER,
                  [{c: f"{c}-0" for c in _mod.CONTEXT_FILES_COLUMN_ORDER}
                   | {"repo_name": "owner/scanned", "context_file": "CLAUDE.md"}
                   | {c: "" for c in heuristics.AI_COLUMNS}])
        self._write_snapshot(
            collection={"counts": {"sampled": 3, "clone_failed": 1}},
            commit_reclassification={
                "complete": False, "population_complete": False, "left_out": {"no_log": 1},
                "counts": {"current": 1, "unavailable": 1,
                           **{outcome: 1 for outcome in _mod.RECLASSIFICATION_GAPS}}},
            metadata={"rows_without_commit_metadata": {"total": 1}})
        (self.ghs.parent / "ghs_retrieval_report.json").write_text(json.dumps({
            "outcome": "fallback", "retrieval_id": "2026-10-05T120000Z",
            "failed_retrieval_id": "2026-12-01T010000Z", "reason": "no response",
            "fallbacks_in_a_row": 1, "failed_item_count": 1}), encoding="utf-8")
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        for sentence in (
                "in 1 GitHub repository.",
                "1. **Sampling frame** (1 repo):",
                "1 was classified as an engineered project.",
                "3. **Configuration detection** (1 repo): Of the 3 repositories in the sample, 1 "
                "was cloned and scanned for configuration artifacts, git could not clone "
                "1, and 1 was not scanned because its collected files were missing. 1 "
                "repository contained at least one configuration artifact.",
                "- The 1 row has GitHub metadata",
                "- 1 row has classification data",
                "- 1 row has detection flags",
                "for 1 repository (2 files)",
                "The GHS search result that the download refused had 1 repository.",
                "1 repository lacks commit logs.",
                "1 repository lacks the collected tree that its per-repository commit record "
                "describes.",
                "1 repository could not be reclassified, because its evidence could not be "
                "processed.",
                "1 repository lacks commit logs. Neither `commits.csv` nor `other_commits.csv` "
                "contains any of its commits, because other commit heuristics classified the "
                "commits and the current heuristics could not classify them again.",
                "1 repository in the sample has a recorded clone or collection failure, and "
                "neither its configuration nor its commits are included.",
                "1 configuration row has missing commit metadata.",
                "1 row of `context_files.csv`, in 1 repository, has blank "
                "`first_commit_ai_created`, `first_commit_ai_tool`, `#ai_commits` and "
                "`ai_commit_tools`, because the current commit heuristics did not classify the "
                "commits that touched the file.",
                "The repository is `owner/no_log-1`.",
                "1 repository in `repos.csv` has a blank `head_commit_date`",
                "1 commit in `commits.csv` has a blank `commit_timestamp`, because git printed "
                "its date",
                "The filter removed 1 row of the GHS retrieval in its first step, `gone`, because "
                "GitHub's API returned no response for it",
                "The filter removed 1 row of the GHS retrieval in its first step, `gone`, because "
                "GitHub's API returned its repository without a repository ID",
                "1 repository in the sampling frame has no label, because the classification "
                "reached no decision for it after its retry rounds and, where the second model "
                "applied, after the second model. It is not in the sample"):
            with self.subTest(sentence=sentence):
                self.assertIn(sentence, readme)
        # The README has no count of 1 before a plural noun or a plural verb. A count after
        # "of", as in "the collected files of 1", agrees with the noun before it.
        self.assertEqual(re.findall(r"(?<!of )\b1 (?:\w+ )?(?:repositories|repos|rows|commits|"
                                    r"files|downloads|were|have|lack)\b", readme), [])

    def test_the_readme_states_the_repositories_that_github_found_without_an_id(self):
        """The filter's step `gone` also removes each repository that GitHub found without its
        id, and `sampling.json` counts these repositories as `without_id`. Because GitHub has
        them, the README states the count as rows that the first step removed rather than as
        rows gone from GitHub. A sampling frame without such a repository states no such
        limit."""
        for without_id in (0, 7):
            with self.subTest(without_id=without_id):
                stages = [{"stage": 1, "key": "gone", "removed": 1 + without_id,
                           "remaining": 2, "values": {"unresolved": 0,
                                                      "without_id": without_id}}]
                self.ghs.write_text(json.dumps(
                    {"_metadata": {**FRAME_METADATA, "stages": stages},
                     "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                sentence = ("The filter removed 7 rows of the GHS retrieval in its first step, "
                            "`gone`, because GitHub's API returned their repositories without a "
                            "repository ID, by which the later stages identify a repository. "
                            "GitHub has these repositories, and the sampling frame leaves them "
                            "out. That step runs before the filter's other steps, and the count "
                            "includes rows that those steps would have removed.")
                self.assertEqual(sentence in readme, bool(without_id))
                self.assertEqual("## Evidence limits" in readme, bool(without_id))

    def test_the_readme_names_the_languages_that_the_sampling_frame_kept(self):
        """The filter keeps the most common primary languages of each retrieval, and
        `sampling.json` records them under the values of the step `languages`. A README that
        named a fixed list would describe another sampling frame than the sampling frame beside
        it. When the filter skipped the step, which then removed no row, the README names no
        language."""
        for values, removed, filters in (
                ({"top": 3, "languages": ["C#", "Go", "Zig"]}, 4,
                 "   - repositories with a primary language other than C#, Go, and Zig\n"),
                ({"top": 2, "languages": ["Go", "Rust"]}, 4,
                 "   - repositories with a primary language other than Go and Rust\n"),
                ({}, 0, None)):
            with self.subTest(values=values):
                stages = [{"stage": 6, "key": "languages", "removed": removed, "remaining": 2,
                           "values": values}]
                self.ghs.write_text(json.dumps(
                    {"_metadata": {**FRAME_METADATA, "stages": stages},
                     "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                if filters is None:
                    self.assertIn("Repositories were sourced from the GitHub Search dataset.\n",
                                  readme)
                    self.assertNotIn("primary language other than", readme)
                else:
                    self.assertIn(filters, readme)
                self.assertNotIn("TypeScript", readme)

    def test_the_readme_names_every_step_of_the_filter_that_removed_rows(self):
        """The README lists what each step of the filter removed from the GHS retrieval, in the
        order of the steps, and points to `sampling.json` for the counts. The steps and their
        parameters come from the `_metadata` of the sampling frame: a step that removed no row,
        such as `missing_data` here, is not listed, and a step that the README has no words for
        is named by the description that the filter recorded. A README that named a fixed set
        of kinds would hide from a reader that the filter of a sampling frame also removed, for
        example, disabled and locked repositories, duplicates, the rows that GitHub no longer
        had or the copies at one head commit."""
        stages = [
            {"stage": 1, "key": "gone", "removed": 255, "remaining": 9,
             "values": {"unresolved": 0, "without_id": 0, "created_after_window": 108}},
            {"stage": 2, "key": "duplicates", "removed": 2264, "remaining": 8,
             "values": {"groups": 2182, "shared_names": 1}},
            {"stage": 3, "key": "cleanup", "removed": 3883, "remaining": 7,
             "values": {"flags": ["isArchived", "isDisabled", "isLocked", "isFork"]}},
            {"stage": 4, "key": "missing_data", "removed": 0, "remaining": 7, "values": {}},
            {"stage": 5, "key": "licenses", "removed": 26679, "remaining": 6,
             "values": {"allowed": ["Apache License 2.0", "MIT License", "The Unlicense"]}},
            {"stage": 6, "key": "languages", "removed": 33244, "remaining": 5,
             "values": {"top": 2, "languages": ["Go", "Python"]}},
            {"stage": 7, "key": "educational", "removed": 1635, "remaining": 4, "values": {}},
            {"stage": 8, "key": "activity", "removed": 93241, "remaining": 3,
             "values": {"commits_min": 359.0, "watchers_min": 5.0}},
            {"stage": 9, "key": "mirrors", "removed": 25, "remaining": 2,
             "values": {"groups": 19}},
            {"stage": 10, "key": "stars", "removed": 3, "remaining": 2,
             "description": "stars below 7", "values": {}}]
        self.ghs.write_text(json.dumps(
            {"_metadata": {**FRAME_METADATA, "stages": stages},
             "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn(
            "1. **Sampling frame** (2 repos): Repositories were sourced from the GitHub Search "
            "dataset. The steps of the filter of the sampling stage left out these rows, and "
            "`sampling.json` records how many each step removed:\n"
            "   - rows for which GitHub returned no public repository with an id under the name, "
            "or a repository that GitHub created after the latest creation date that the "
            "sampling window allows\n"
            "   - duplicate rows, which GitHub resolves to one repository, and the rows of a name "
            "under which GitHub returned two repositories\n"
            "   - archived, disabled, locked, and forked repositories, by GitHub's flags\n"
            "   - repositories without one of the 3 OSI-approved licenses that the filter allows\n"
            "   - repositories with a primary language other than Go and Python\n"
            "   - repositories with an educational or resource topic or name\n"
            "   - repositories with fewer than 359 commits or fewer than 5 watchers\n"
            "   - repositories at the head commit of a repository that GitHub created earlier\n"
            "   - the rows of the step \"stars below 7\"\n"
            "2. **Classification**", readme)
        self.assertNotIn("pull requests or issues", readme)

    def test_the_readme_names_the_rows_of_a_shared_name_only_when_the_filter_removed_some(self):
        """The step `duplicates` records `shared_names` only when GitHub resolved a name to two
        repositories, and only then does the README say that the step removed such rows."""
        stages = [{"stage": 2, "key": "duplicates", "removed": 3, "remaining": 2,
                   "values": {"groups": 3}}]
        self.ghs.write_text(json.dumps(
            {"_metadata": {**FRAME_METADATA, "stages": stages},
             "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("   - duplicate rows, which GitHub resolves to one repository\n", readme)

    def test_the_readme_names_the_steps_of_a_sampling_frame_without_keys(self):
        """The filter of March 2026 recorded its steps with a description and without a key.
        The README names each step that removed rows by that description, and a step without
        one by its number."""
        stages = [{"stage": 1, "description": "Remove archived/disabled/locked",
                   "removed": 2241, "remaining": 3},
                  {"stage": 2, "description": "Remove rows with NULL PR/Issues data",
                   "removed": 0, "remaining": 3},
                  {"stage": 3, "removed": 1, "remaining": 2}]
        self.ghs.write_text(json.dumps(
            {"_metadata": {**MARCH_FRAME_METADATA, "stages": stages},
             "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("removed:\n   - the rows of the step \"Remove archived/disabled/locked\"\n"
                      "   - the rows of step 3\n2. **Classification**", readme)
        self.assertNotIn("NULL", readme)

    def test_the_readme_states_the_languages_that_the_end_date_left_out(self):
        """The filter's step `languages` passes over a language that the end date of the
        sampling window leaves out, that is, a language with a crawl date before the end date
        or with no crawl date. The step records the language under `left_out`
        (`1_sampling/filter-ghs-spec.md`). The sampling frame contains none of the language's
        repositories, and the README therefore names the language with its crawl date and says
        that the step passed over it. With two such languages, the README claims no more for
        the second: Python, Rust, Zig and Go rank in that order, and without the end date a
        step that keeps two languages would keep Python and Rust, not Zig. A sampling frame
        without such a language, or without the record, states no such limit."""
        common = ("GHS has recorded the pushes to repositories in a language up to the "
                  "language's crawl date, the latest commit date that GHS has in it. The "
                  "window covers a language only when its crawl date is on or after the end "
                  "date.")
        for values, sentence in (
                ({"top": 10, "languages": ["Go"], "left_out": {}}, None),
                ({"top": 10, "languages": ["Go"]}, None),
                ({"top": 10, "languages": ["Go"], "left_out": {"Rust": "2026-08-01"}},
                 "The filter's step `languages` left out Rust (crawl date 2026-08-01), because "
                 "the end date of the sampling window, 2026-09-22, does not cover it. " + common
                 + " The step passed over it while it chose the 10 most common primary "
                   "languages that the end date covers. The sampling frame contains no "
                   "repository in Rust."),
                ({"top": 2, "languages": ["Go", "Python"],
                  "left_out": {"Zig": None, "Rust": "2026-08-01"}},
                 "The filter's step `languages` left out Rust (crawl date 2026-08-01) and Zig (no "
                 "crawl date), because the end date of the sampling window, 2026-09-22, does not "
                 "cover them. " + common + " The step passed over them while it chose the 2 most "
                 "common primary languages that the end date covers. The sampling frame contains "
                 "no repository in these languages."),
                ({"left_out": {"Zig": None}},
                 "The filter's step `languages` left out Zig (no crawl date), because the end "
                 "date of the sampling window, 2026-09-22, does not cover it. " + common
                 + " The sampling frame contains no repository in Zig.")):
            with self.subTest(values=values):
                stages = [{"stage": 6, "key": "languages", "removed": 1, "remaining": 2,
                           "values": values}]
                self.ghs.write_text(json.dumps(
                    {"_metadata": {**FRAME_METADATA, "stages": stages},
                     "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text()
                if sentence is None:
                    self.assertNotIn("## Evidence limits", readme)
                    self.assertNotIn("crawl date", readme)
                else:
                    self.assertIn("## Evidence limits", readme)
                    self.assertIn(sentence, readme)

    def test_the_readme_states_the_repositories_that_the_labeling_left_undecided(self):
        """A repository that the classification left without a decision after its retry rounds
        is in the sampling frame but not in the sample. The dataset lacks its configuration, and
        the README says so. The second model labels only the repositories at which one of the
        classifying model's responses failed, and only in an invocation in which it can label,
        so the reason does not say that the second model failed every one of them. A record
        without the count states no such limit."""
        self.assertEqual(self._run(), 0)
        self.assertNotIn("have no label", (self.release / "0_README.md").read_text())
        record = self.annotated.with_suffix(".json")
        record.write_text(json.dumps({**json.loads(record.read_text()), "undecided": 2}))
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text()
        self.assertIn("## Evidence limits", readme)
        self.assertIn("2 repositories in the sampling frame have no label, because the "
                      "classification reached no decision for them after its retry rounds and, "
                      "where the second model applied, after the second model.", readme)

    def test_a_missing_collected_files_archive_refuses_the_run(self):
        (self.release / _mod.ARCHIVE_NAME).unlink()
        self.assertEqual(self._run(), 1)

    def test_a_truncated_mechanism_csv_is_refused_rather_than_published(self):
        """Suppose that the release copied a mechanism CSV without opening it and took the
        line count less one as the row count of the README. A CSV that a killed detection run
        truncated would then be published as it stands and described as having -1 rows."""
        (self.output / "settings.csv").write_text("", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self._run_main(), 1)
        self.assertIn("settings.csv", err.getvalue())
        self.assertIn("repo_name", err.getvalue())

    def test_an_undecided_file_in_output_refuses_the_run(self):
        (self.output / "scratch.csv").write_text("a\n", encoding="utf-8")
        self.assertEqual(self._run(), 1)

    def _refused(self, *expected: str) -> None:
        """Assert that the run returns 1 through `main`, logs each of `expected` and writes
        nothing."""
        before = {name: (self.release / name).read_bytes() for name in os.listdir(self.release)}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self._run_main(), 1)
        for text in expected:
            self.assertIn(text, err.getvalue())
        self.assertEqual({name: (self.release / name).read_bytes()
                          for name in os.listdir(self.release)}, before)

    def test_a_stray_in_the_release_is_refused_and_left_alone(self):
        """A release written beside a stray file would let a person who uploads the directory
        publish a file that the checksum manifest does not cover. The partial archive that a
        killed run of the collected_files_archive step leaves is such a file."""
        for stray in ("notes.txt", _mod.ARCHIVE_NAME + ".building"):
            with self.subTest(stray=stray):
                (self.release / stray).write_text("mine\n", encoding="utf-8")
                self._refused(stray, "Remove it")
                (self.release / stray).unlink()

    def test_the_temporary_files_of_a_stopped_run_are_removed_and_the_release_is_built(self):
        """A Stop during this step can end the step before it removes the temporary file that
        it was writing. The check of `release/` refuses every entry that is not a file of the
        release. Without the removal, every later Start would fail in this step."""
        self.assertEqual(self._run(), 0)
        left = [self.release / f"{name}.k3x9_q2.tmp"
                for name in ("repos.csv", "commits.csv", "other_commits.csv", "context_files.csv")]
        for path in left:
            path.write_text("half a CSV\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self._run_main(), 0)
        self.assertEqual(sorted(os.listdir(self.release)), sorted(RELEASE_NAMES))
        self.assertIn("Removed 4 temporary file(s)", err.getvalue())

    def test_what_a_file_manager_writes_into_release_or_output_is_no_stray(self):
        """Finder writes `.DS_Store` over SMB, and Synology DSM writes `@eaDir` into a directory
        that a person opens on the NAS. Neither is a file of the dataset, and a refusal of
        either would make every later Start fail in this step."""
        for directory in (self.release, self.output):
            (directory / ".DS_Store").write_bytes(b"\0\0\0\1Bud1")
            (directory / "@eaDir" / "repos.csv@SynoEAStream").parent.mkdir()
            (directory / "@eaDir" / "repos.csv@SynoEAStream").write_bytes(b"\0")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self._run_main(), 0)
        self.assertTrue((self.release / "checksums.sha256").is_file())
        self.assertNotIn(".DS_Store", (self.release / "checksums.sha256").read_text())

    def test_a_provenance_without_the_version_is_refused(self):
        """`CITATION.cff` and the README cite the version that `provenance.json` records. A
        release without it would cite no version."""
        provenance = json.loads((self.output / "provenance.json").read_text(encoding="utf-8"))
        del provenance["dataset_version"]
        (self.output / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
        self._refused("provenance.json records no dataset_version")

    def test_heuristics_other_than_the_detection_s_are_refused(self):
        """If the release copied `--heuristics` whatever heuristics `provenance.json` records
        for the detection step, it could publish heuristics that produced none of the detection
        step's rows beside the checksum of the heuristics that did."""
        self.heuristics.write_text("tool,mechanism\ncursor,rules\n", encoding="utf-8")
        self._refused(str(self.heuristics), "provenance.json records")

    def test_heuristics_of_the_planning_documents_other_than_the_detection_s_are_refused(self):
        """The release publishes the heuristics of the planning documents beside the CSV of the
        planning documents. Heuristics that produced none of the CSV's rows would be published
        as if they had produced them."""
        provenance = json.loads((self.output / "provenance.json").read_text(encoding="utf-8"))
        provenance["planning_heuristics_sha256"] = "0" * 64
        (self.output / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
        self._refused(str(planning.PLANNING_DOCUMENT_PATTERNS_PATH), "provenance.json records")

    def test_the_vendored_directories_and_the_local_state_are_published_as_detection_ran(self):
        """Which directories the collection and the detection skipped and which places of the
        local state they left out depend on vendored_dirs.txt and local_state_paths.csv. The
        release publishes both, and it refuses a file other than the one that provenance.json
        records."""
        self.assertEqual(self._run(), 0)
        for name, path in (("vendored_dirs.txt", heuristics.VENDORED_DIRS_PATH),
                           ("local_state_paths.csv", heuristics.LOCAL_STATE_PATHS_PATH)):
            with self.subTest(name=name):
                self.assertEqual((self.release / name).read_bytes(), path.read_bytes())
        for key, path in (("vendored_dirs_sha256", heuristics.VENDORED_DIRS_PATH),
                          ("local_state_sha256", heuristics.LOCAL_STATE_PATHS_PATH)):
            with self.subTest(key=key):
                self.setUp()
                provenance = json.loads((self.output / "provenance.json").read_text(
                    encoding="utf-8"))
                provenance[key] = "0" * 64
                (self.output / "provenance.json").write_text(json.dumps(provenance),
                                                             encoding="utf-8")
                self._refused(str(path), "provenance.json records")

    def test_the_planning_documents_are_published_and_described_as_no_configuration(self):
        """The release publishes the CSV as the detection step wrote it, with the heuristics
        that found its rows. The README defines a planning document, says that it is no
        configuration artifact and makes no repository configured, and counts the repositories
        in the CSV."""
        self._write_planning(3)
        self.assertEqual(self._run(), 0)
        self.assertEqual((self.release / planning_schema.CSV_NAME).read_bytes(),
                         (self.output / planning_schema.CSV_NAME).read_bytes())
        self.assertEqual((self.release / "planning_document_patterns.csv").read_bytes(),
                         planning.PLANNING_DOCUMENT_PATTERNS_PATH.read_bytes())
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        section = readme[readme.index(f"### {planning_schema.CSV_NAME}"):]
        section = section[:section.index("\n## ")]
        for text in ("A planning document is a work product that a spec-driven development framework",
                     "Planning documents are not configuration artifacts, set no tool flag",
                     "They do not make a repository configured",
                     "1 repository in this release",
                     "| `framework` |", "| `document` |", "| `line_count` |"):
            with self.subTest(text=text):
                self.assertIn(text, section)

    def test_rows_of_the_planning_documents_without_commit_metadata_are_an_evidence_limit(self):
        self._write_snapshot(planning_documents={"rows_without_commit_metadata":
                                                 {"total": 2, "repositories": 1}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn(f"2 rows of `{planning_schema.CSV_NAME}` have missing commit metadata.",
                      readme)

    def test_an_archive_of_other_repositories_is_refused(self):
        """Checking only that the archive of collected files is there would let the README
        describe an archive of other repositories as the files of the repositories that the
        CSVs name."""
        other = self.tmp / "other" / "repos"
        (other / "owner§elsewhere").mkdir(parents=True)
        (other / "owner§elsewhere" / "AGENTS.md").write_text("hi\n", encoding="utf-8")
        (self.release / _mod.ARCHIVE_NAME).unlink()
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(self.release / _mod.ARCHIVE_NAME),
                        "repos"], cwd=self.tmp / "other", check=True)
        self._refused("owner§elsewhere", "owner§scanned")

    def test_commits_classified_under_other_heuristics_are_refused(self):
        """The snapshot records which commit heuristics classified the commit CSV. Publishing
        the commit heuristics of the checkout regardless would describe the CSV by other
        heuristics."""
        self._write_snapshot(commit_reclassification={
            "classified_by": "0" * 64, "complete": True, "counts": {"current": 1}})
        self._refused("0" * 64, "commit-reclassification")

    def test_a_heuristic_id_that_the_published_heuristics_lack_is_refused(self):
        for name, tool in (("all_ai_commits.csv", "retired"), ("other_ai_commits.csv", "gone")):
            with self.subTest(name=name):
                self._write_commits(["one"], name=name, tool=tool)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(self._run_main(), 1)
                self.assertIn(f"{tool}-trailer-01", err.getvalue())
                self.assertFalse((self.release / "checksums.sha256").exists())
                self._write_commits(["one"], name=name,
                                    tool="claude" if tool == "retired" else "Amp")

    def test_a_snapshot_without_the_collection_counts_is_refused(self):
        (self.output / "snapshot.json").write_text(json.dumps({}), encoding="utf-8")
        self._refused("collection.counts")

    def test_labels_whose_record_names_no_model_are_refused(self):
        self.annotated.with_suffix(".json").write_text(json.dumps(
            {"description": "a run", "models": []}), encoding="utf-8")
        self._refused("names no model")

    def test_a_relative_release_directory_stores_the_bare_names(self):
        """7zz stores a relative path as it is given. A relative `--release-dir` could therefore
        put `release/repos.csv` into `repos.csv.7z`, where an absolute `--release-dir` puts
        `repos.csv`."""
        cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, cwd)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(_mod.main(["--output-dir", "output", "--release-dir", "release",
                                        "--ghs-json", self.ghs.name,
                                        "--annotated-csv", self.annotated.name,
                                        "--heuristics", self.heuristics.name,
                                        "--imported-dir", "data/imported"]), 0)
        for archive, member in (("repos.csv.7z", "repos.csv"), ("commits.csv.7z", "commits.csv")):
            with self.subTest(archive=archive):
                self.assertEqual(_mod.archive_entries(self.release / archive), [member])

    def test_the_rebuilt_csvs_have_unix_line_endings(self):
        self.assertEqual(self._run(), 0)
        for name in ("repos.csv", "context_files.csv", "commits.csv", "other_commits.csv"):
            with self.subTest(name=name):
                self.assertNotIn(b"\r", (self.release / name).read_bytes())

    def test_a_row_with_a_line_break_in_a_field_counts_once(self):
        """The README counts a copied CSV by its CSV rows, not by its lines."""
        write_csv(self.output / "settings.csv", ["repo_name", "artifact"],
                  [{"repo_name": "owner/scanned", "artifact": "one\ntwo\nthree"}])
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("| `settings.csv` | 1 |", readme)

    def test_the_readme_describes_id_and_created_at_as_the_sampling_frame_records_them(self):
        """The sampling frame of March 2026 has GHS's id, and its `createdAt` has GHS's hour on
        a 12-hour clock. The README describes `id` and `createdAt` as each sampling frame
        records them rather than as GitHub's values for every sampling frame."""
        for metadata, created in ((FRAME_METADATA, "| `createdAt` | Repository creation time "
                                                   "in UTC, as GitHub reports it |"),
                                  (MARCH_FRAME_METADATA, "| `createdAt` | Repository creation "
                                                         "time as GHS recorded it, with no "
                                                         "time zone and the hour on a 12-hour "
                                                         "clock without AM or PM")):
            with self.subTest(github="github" in metadata["input_source"]):
                self.ghs.write_text(json.dumps({"_metadata": metadata,
                                                "items": [frame_item(n) for n in self.REPOS]}),
                                    encoding="utf-8")
                self.assertEqual(self._run(), 0)
                readme = (self.release / "0_README.md").read_text(encoding="utf-8")
                self.assertIn(created, readme)
                self.assertIn("| `id` | GitHub's repository ID", readme)
                self.assertEqual("has GHS's database id instead" in readme,
                                 metadata is MARCH_FRAME_METADATA)

    def test_the_tool_flags_name_the_tool_rather_than_one_of_its_clients(self):
        """The heuristics set `copilot` and `codex` for files that VS Code and the Codex app
        read. The README describes the two flags as more than Copilot CLI and Codex CLI
        configuration."""
        with heuristics.CONFIG_PATH_PATTERNS_PATH.open(newline="", encoding="utf-8") as f:
            pages = {(row["tool"], row["doc_url"]) for row in csv.DictReader(f)}
        self.assertTrue(any(tool == "copilot" and "code.visualstudio.com" in url
                            for tool, url in pages))
        self.assertIn(("codex", "https://learn.chatgpt.com/docs/environments/local-environment"),
                      pages)
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("| `copilot` | Repository has GitHub Copilot configuration, such as for "
                      "Copilot in VS Code,", readme)
        self.assertIn("| `codex` | Repository has OpenAI Codex configuration, such as for the "
                      "Codex CLI, the Codex app", readme)

    def test_the_readme_describes_this_release_and_cites_agentconfigs_1_0_0(self):
        """The AIware 2026 dataset paper describes AgentConfigs 1.0.0, the release of the dataset
        that AgentTorrent succeeds, which an earlier version of the pipeline built from another
        sampling, classification and collection. The README states where the repositories of
        this release come from and when they were scanned, cites the paper for AgentConfigs 1.0.0
        and points to the documents of the pipeline for the method, rather than presenting this
        release as the data of that study with added columns or as a later version of
        AgentConfigs."""
        self._write_snapshot(collection={
            "counts": {"sampled": 2, "clone_failed": 0},
            "scanned_at_window": {"first": "2026-10-26T01:38:17Z", "last": "2026-10-27T12:39:24Z"}})
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        opening = readme[:readme.index("## ")]
        self.assertIn("The repositories come from retrieval `2026-10-05T120000Z` of the GitHub "
                      "Search dataset (GHS), with a sampling window that ends on 2026-09-22, and "
                      "they were scanned from 2026-10-26T01:38:17Z to 2026-10-27T12:39:24Z.",
                      opening)
        self.assertIn("This dataset succeeds AgentConfigs, the dataset that the AIware 2026 "
                      "dataset paper describes. An earlier version of the pipeline built "
                      "AgentConfigs 1.0.0", opening)
        self.assertNotIn("release 1.0.0 of this dataset", readme.lower())
        self.assertIn(f"The documents of the pipeline at {_mod.PIPELINE_REPOSITORY} describe how "
                      f"this release was built", opening)
        self.assertNotIn("extends the data", readme)
        self.assertNotIn("describes how this dataset is built", readme)
        self.assertNotIn("dataset paper describes in detail", readme)
        papers = readme[readme.index("## Associated Papers"):readme.index("## Dataset Overview")]
        self.assertIn("(AIware '26)*. It describes AgentConfigs 1.0.0, the release of the dataset "
                      "that this one succeeds.", papers)

    def test_the_readme_cites_the_associated_papers_as_crossref_records_them(self):
        """Crossref records both AIware 2026 papers in the "Proceedings of the 3rd ACM
        International Conference on AI-Powered Software", published by ACM, under the DOIs
        10.1145/3805760.3814922 and 10.1145/3805760.3814887. It records the MSR 2026 paper in the
        "Proceedings of the 23rd International Conference on Mining Software Repositories", with
        the event acronym "MSR '26" and the publisher ACM, under the DOI
        10.1145/3793302.3793350."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        papers = readme[readme.index("## Associated Papers"):readme.index("## Dataset Overview")]
        aiware = ("*Proceedings of the 3rd ACM International Conference on AI-Powered Software "
                  "(AIware '26)*")
        msr = ("*Proceedings of the 23rd International Conference on Mining Software "
               "Repositories (MSR '26)*")
        self.assertEqual(papers.count(aiware), 2)
        self.assertNotIn("ACM/IEEE International Conference on AI", papers)
        self.assertNotIn("IEEE/ACM International Conference on Mining", papers)
        for title, venue, doi in (
                ("A Dataset of Agentic AI Coding Tool Configurations", aiware,
                 "10.1145/3805760.3814922"),
                ("Configuring Agentic AI Coding Tools: An Exploratory Study", aiware,
                 "10.1145/3805760.3814887"),
                ("Context Engineering for AI Agents in Open-Source Software", msr,
                 "10.1145/3793302.3793350")):
            with self.subTest(doi=doi):
                line = next(line for line in papers.splitlines() if title in line)
                self.assertIn(venue, line)
                self.assertIn(f"DOI: https://doi.org/{doi}", line)

    def test_the_readme_names_the_day_of_a_sampling_frame_without_a_retrieval_id(self):
        """The sampling frame of March 2026 records the day on which its GHS search result was
        downloaded and no retrieval id. Without a scan window in the snapshot, the sentence of
        the README that states where the repositories come from names no scan dates."""
        self.ghs.write_text(json.dumps(
            {"_metadata": MARCH_FRAME_METADATA,
             "items": [frame_item(name) for name in self.REPOS]}), encoding="utf-8")
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        self.assertIn("The repositories come from a search result of the GitHub Search dataset "
                      "(GHS) downloaded on 2026-03-16.", readme)

    def test_the_readme_cites_the_release_with_its_authors_in_their_order(self):
        """A reader of the README, which the Zenodo record shows, finds the citation of the
        release itself beside the papers, with the eight authors in the order of the deposit
        and the version of the release."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        citation = readme[readme.index("## Citation"):readme.index("## Associated Papers")]
        self.assertIn("Cite this release as: Sebastian Baltes, Seyedmoein Mohsenimofidi, Levi "
                      "Böhme, Vahram Nersesyan, Muhammad Auwal Abubakar, Jai Lal Lulla, Matthias "
                      "Galster, Christoph Treude. **AgentTorrent: A Dataset of Context and "
                      "Harness Engineering Artifacts on GitHub.** Version 1.0.0. Zenodo.",
                      citation)

    def test_the_citation_names_the_version_that_the_rows_record(self):
        """`CITATION.cff` and the README cite the version that `provenance.json` records for
        the rows beside them, so that a release built by another version of the detection
        cites that version."""
        provenance = json.loads((self.output / "provenance.json").read_text(encoding="utf-8"))
        provenance["dataset_version"] = "2.1.0"
        (self.output / "provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
        self.assertEqual(self._run(), 0)
        self.assertIn('\nversion: "2.1.0"\n',
                      (self.release / "CITATION.cff").read_text(encoding="utf-8"))
        self.assertIn("Version 2.1.0. Zenodo.",
                      (self.release / "0_README.md").read_text(encoding="utf-8"))

    def test_the_readme_lists_the_dataset_paper_it_refers_to(self):
        """The README lists the paper that it calls "the associated dataset paper"."""
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        papers = readme[readme.index("## Associated Papers"):readme.index("## Dataset Overview")]
        self.assertIn("Matthias Galster, Seyedmoein Mohsenimofidi, Levi Böhme, Jai Lal Lulla, "
                      "Muhammad Auwal Abubakar, Christoph Treude, Sebastian Baltes. **A Dataset "
                      "of Agentic AI Coding Tool Configurations.**", papers)

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7-Zip's 7zz is not on PATH")
    def test_the_readme_s_command_extracts_the_links_of_the_collected_files(self):
        """Unless `-snld` is given, 7-Zip refuses a symbolic link with a target outside its
        directory and writes an empty file in its place. `7z x <archive> -y` is not enough for
        the archive of collected files. This test runs the command that the README
        gives for that archive, with 7-Zip's `7zz` as `7z`."""
        tree = self.tmp / "linked" / "repos" / "owner§scanned"
        (tree / ".claude" / "rules").mkdir(parents=True)
        (tree / ".claude" / "rules" / "style.md").write_text("rule\n", encoding="utf-8")
        (tree / ".cursor").mkdir()
        (tree / ".cursor" / "rules").symlink_to("../.claude/rules")
        (self.release / _mod.ARCHIVE_NAME).unlink()
        subprocess.run([SEVEN_ZIP, "a", "-snl", "-bso0", "-bsp0",
                        str(self.release / _mod.ARCHIVE_NAME), "repos"],
                       cwd=self.tmp / "linked", check=True)
        self.assertEqual(self._run(), 0)
        readme = (self.release / "0_README.md").read_text(encoding="utf-8")
        commands = re.findall(r"`(7z x [^`]*)`", readme)
        command = next((c for c in commands if _mod.ARCHIVE_NAME in c), commands[0])
        command = command.replace("<archive>", _mod.ARCHIVE_NAME)

        bin_dir, work = self.tmp / "bin", self.tmp / "extracted"
        bin_dir.mkdir()
        work.mkdir()
        (bin_dir / "7z").symlink_to(shutil.which(SEVEN_ZIP))
        shutil.copyfile(self.release / _mod.ARCHIVE_NAME, work / _mod.ARCHIVE_NAME)
        subprocess.run(shlex.split(command), cwd=work, capture_output=True,
                       env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        link = work / "repos" / "owner§scanned" / ".cursor" / "rules"
        self.assertTrue(link.is_symlink(), f"{command} did not keep the link")
        self.assertEqual(os.readlink(link), "../.claude/rules")


if __name__ == "__main__":
    unittest.main()
