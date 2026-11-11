"""Tests for agentcontext.checks.deletions: the summary and the assertions over the cleanup's
deletion log.

Strategy:
  - The real collected file cleanup writes the log and the trees that the assertions read, over a
    collected tree built here. The real detection step then writes the mechanism CSVs over the
    validated tree, in the order of a run. A log written by hand would record what the fixture's
    author believes that the step writes, and the check exists to test that belief.
  - Each assertion is tested both ways. The outputs that the steps wrote pass all three
    assertions, and each assertion fails on the situation that it names, which a step produces
    wherever a step can: a SKILL.md that the cleanup deleted, a log written by a later cleanup
    run than the output CSVs, and a repository directory that is missing from the validated
    tree without a row. The file history step's own removal of a repository directory must pass.
  - The check runs in-process through `main`, with the exit code, stdout and stderr that a
    person who runs `python -m agentcontext deleted-files-report` sees.
    tests/test_check_modes.py runs the check as a command.

Requirements: Python 3.14 (stdlib only, and the detection step records `n/a` for the language
where lingua is not installed)
Run with:  python3 -m unittest tests.test_checks_deletions -v
"""

import contextlib
import csv
import importlib
import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from agentcontext import heuristics
from agentcontext.checks import deletions as _mod
from agentcontext.checks.unmatched import DROPPED_REASON
from agentcontext.steps import file_history, collected_file_cleanup

META = list(heuristics.META_COLUMNS)
# Long enough that the cleanup does not treat the file as empty.
CONTEXT = "# Project\n\nRun the tests before every commit.\n"


@contextlib.contextmanager
def _in_dir(path: Path):
    """Run with `path` as the working directory, as the runner runs a step."""
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class _Step(unittest.TestCase):
    """A stage directory in a temporary directory, with the real layout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.collected = self.tmp / "data" / "collected"
        self.validated = self.tmp / "data" / "validated"
        self.output = self.tmp / "output"
        self.log = self.tmp / "deletion.csv"
        self.collected.mkdir(parents=True)
        (self.collected / "clone_failed.csv").write_text("repo_name,repo_url,reason\n",
                                                         encoding="utf-8")
        self._rows = []

    def collect(self, repo_name: str, files: dict) -> None:
        """Add a repository to the collected tree and to its list."""
        root = self.collected / repo_name.replace("/", "§")
        for rel, content in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self._rows.append([repo_name, "2026-03-29", "main", "a" * 40, "2026-01-01T00:00:00Z"])
        with open(self.collected / "repos.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(META)
            writer.writerows(self._rows)

    def run_validation(self) -> None:
        """The collected file cleanup over the collected tree, from the stage directory."""
        with _in_dir(self.tmp), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(collected_file_cleanup.main(["--submodules", str(self.tmp / "submodules.csv")]), 0)

    def detect(self) -> None:
        """The detection step over the validated tree, from the stage directory."""
        detect = importlib.import_module("agentcontext.steps.detection")
        saved = detect.COMMITS_DIR
        try:
            detect.COMMITS_DIR = self.tmp / "collect_commits"
            with _in_dir(self.tmp), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(detect.main([]), 0)
        finally:
            detect.COMMITS_DIR = saved

    def check(self, *extra) -> tuple:
        """(exit code, log messages, log messages) of the check over this stage directory. Every
        line goes through the logger. The tuple keeps a copy for the callers that read the
        counts and a copy for the callers that read the verdict."""
        err = io.StringIO()
        argv = ["--deletion-csv", str(self.log), "--collected-dir", str(self.collected),
                "--validated-dir", str(self.validated), "--output-dir", str(self.output),
                *extra]
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(err):
            code = _mod.main(argv)
        self.assertEqual(out.getvalue(), "", "nothing is printed besides the log messages")
        return code, err.getvalue(), err.getvalue()

    def reasons(self) -> dict:
        """{path: reason}, as the cleanup wrote the log."""
        with open(self.log, newline="", encoding="utf-8") as f:
            return {row["path"]: row["reason"] for row in csv.DictReader(f)}


class TestTheReasonsAreTheValidations(_Step):
    """The check matches four reasons that the step writes. A change to their wording in the
    step has to change the check too. The real step writes them here, because a search of the
    step's source for the strings would also match a comment."""

    def test_the_reasons_are_the_ones_the_validation_writes(self):
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT, "docs/SKILL.md": CONTEXT,
                                    ".claude/agents/empty.md": "\n"})
        os.symlink(self.tmp / "elsewhere.md",
                   self.collected / "owner§kept" / ".claude" / "outside.md")
        self.collect("owner/dropped", {".claude/fixture-notes.md": "notes\n"})
        self.run_validation()
        written = set(self.reasons().values())
        for reason in (_mod.SKILL_MD_REASON, DROPPED_REASON, *_mod.BEFORE_ANY_KEEP_RULE):
            with self.subTest(reason=reason):
                self.assertIn(reason, written)


class TestARunTheStepsWrote(_Step):
    """A log, two trees and the output CSVs that one cleanup run and one detection run wrote
    pass all three assertions. This direction of the tests catches a check that fails on every
    input."""

    def setUp(self):
        super().setUp()
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT,
                                    ".github/workflows/ci.yml": "on: push\n"})
        self.collect("owner/dropped", {".claude/fixture-notes.md": "notes\n"})
        self.run_validation()
        self.detect()

    def test_every_assertion_passes(self):
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(out.count("  PASS  "), 3, out)
        self.assertNotIn("FAIL", out)

    def test_the_rows_are_counted_per_reason(self):
        reasons = self.reasons()
        self.assertIn(DROPPED_REASON, reasons.values(), "the fixture drops no repository")
        _code, out, _err = self.check()
        self.assertIn(f"deletion.csv has {len(reasons)} rows", out)
        for reason in set(reasons.values()):
            self.assertIn(_mod.reason_of(reason), out)

    def test_the_files_a_repository_row_stands_for_are_listed(self):
        """The dropped repository's one file has no row of its own. The repository row is its
        only record, and the check names the file."""
        self.assertFalse(any(p.endswith("fixture-notes.md") for p in self.reasons()))
        out_csv = self.tmp / "deletions.csv"
        _code, out, _err = self.check("--out", str(out_csv))
        self.assertNotIn(".claude/fixture-notes.md", out, "the CSV lists it and the log messages count it")
        self.assertIn(".claude/fixture-notes.md", self.check()[1])
        with open(out_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(tuple(rows[0]), _mod.OUT_COLUMNS)
        self.assertIn({"kind": "dropped_glob", "repo_name": "owner/dropped",
                       "subject": ".claude/fixture-notes.md", "count": "1", "note": ""}, rows)
        self.assertEqual(sum(int(r["count"]) for r in rows if r["kind"] == "reason"),
                         len(self.reasons()))

    def test_the_out_csv_ends_each_record_with_lf(self):
        """The CSV of `--out` ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default."""
        out_csv = self.tmp / "deletions.csv"
        code, out, err = self.check("--out", str(out_csv))
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(b"\r\n", out_csv.read_bytes())
        with open(out_csv, newline="", encoding="utf-8") as f:
            self.assertIn("dropped_glob", {row["kind"] for row in csv.DictReader(f)})

    def test_the_planning_documents_of_a_dropped_repository_are_not_listed(self):
        """The check leaves the planning documents out of the files of a dropped repository that
        no row names, because a planning document is explained by its heuristic."""
        spec = self.collected / "owner§dropped" / "openspec" / "specs" / "auth" / "spec.md"
        spec.parent.mkdir(parents=True)
        spec.write_text("# Auth\n", encoding="utf-8")
        out_csv = self.tmp / "deletions.csv"
        self.check("--out", str(out_csv))
        with open(out_csv, newline="", encoding="utf-8") as f:
            globs = [row["subject"] for row in csv.DictReader(f) if row["kind"] == "dropped_glob"]
        self.assertEqual(globs, [".claude/fixture-notes.md"])

    def test_a_place_below_the_root_is_listed_at_its_place_in_the_tool_directory(self):
        """A file of a `.claude` below the root has the path glob of its path below `.claude`,
        as `unmatched paths` counts it, and the reason of not_configuration.csv for that place
        names it."""
        notes = self.collected / "owner§dropped" / "apps" / "web" / ".claude" / "memory"
        notes.mkdir(parents=True)
        (notes / "MEMORY.md").write_text("notes\n", encoding="utf-8")
        out_csv = self.tmp / "deletions.csv"
        self.check("--out", str(out_csv))
        with open(out_csv, newline="", encoding="utf-8") as f:
            globs = {row["subject"]: row["note"] for row in csv.DictReader(f)
                     if row["kind"] == "dropped_glob"}
        self.assertEqual(globs, {".claude/fixture-notes.md": "",
                                 ".claude/memory/**": "not_configuration.csv"})

    def test_a_directory_that_synology_dsm_wrote_after_the_cleanup_is_not_listed(self):
        """DSM writes an `@eaDir` into a directory of the collected tree that File Station visits
        on the NAS, after the cleanup has read the directory. The files of the `@eaDir` are no
        files of the repository that the cleanup removed, and the path globs that no heuristic
        names leave them out."""
        dsm = self.collected / "owner§dropped" / ".claude" / "@eaDir"
        dsm.mkdir()
        (dsm / "fixture-notes.md@SynoEAStream").write_bytes(b"\0")
        out_csv = self.tmp / "deletions.csv"
        self.check("--out", str(out_csv))
        with open(out_csv, newline="", encoding="utf-8") as f:
            globs = [row["subject"] for row in csv.DictReader(f) if row["kind"] == "dropped_glob"]
        self.assertEqual(globs, [".claude/fixture-notes.md"])

    def test_a_tree_that_contains_no_repository_is_not_checked_rather_than_passed(self):
        """The trees may be removed to save space while their tracked lists stay, and the check
        therefore does not test whether the directory exists. A tree that contains no
        repository leaves the assertion that reads it unchecked. The check logs the unchecked
        assertion and exits 1 (could not check). An exit of 0 while the other two assertions
        hold would suggest that all three held."""
        for name in os.listdir(self.validated):
            if name != "repos.csv":
                shutil.rmtree(self.validated / name)
        code, out, err = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("NOT CHECKED  The repository rows against the trees", out)
        self.assertEqual(out.count("  PASS  "), 2)
        self.assertIn("ERROR deleted_files_report: 1 of the three assertions could not be "
                      "checked", err)

    def test_report_only_exits_zero_when_an_assertion_was_not_checked(self):
        """The runner's step runs the check with `--report-only`. The check logs the finding,
        and the run continues to the archive."""
        for name in os.listdir(self.validated):
            if name != "repos.csv":
                shutil.rmtree(self.validated / name)
        code, _out, err = self.check("--report-only")
        self.assertEqual(code, 0)
        self.assertIn("WARNING deleted_files_report: 1 of the three assertions could not be "
                      "checked", err)


class TestASkillTheValidationDeleted(_Step):
    """A `SKILL.md` that the cleanup deletes in a directory that a collected link names means
    that two steps treat one file in opposite ways, because the collector followed the link and
    copied the skill. The cleanup keeps such a directory. The log that fails here is therefore a
    log that an older cleanup wrote, and the test appends its row in the step's own format. By
    design, the cleanup deletes a `SKILL.md` outside every skills directory that no link names,
    and the check counts it."""

    def setUp(self):
        super().setUp()
        self.collect("owner/linked", {"CLAUDE.md": CONTEXT,
                                      ".claude/skills/review": "../../tools/review\n",
                                      "tools/review/SKILL.md": "---\nname: review\n---\n",
                                      "docs/skills/other/SKILL.md": "---\nname: other\n---\n"})
        self.run_validation()
        self.detect()

    def older_validation(self, reason=_mod.SKILL_MD_REASON) -> None:
        """Delete the linked skill and append its row to the log in the step's own format."""
        kept = self.validated / "owner§linked" / "tools" / "review" / "SKILL.md"
        self.assertTrue(kept.is_file(), "the cleanup deleted the linked skill")
        kept.unlink()
        rows = list(self.reasons())
        prefix = rows[0].split("owner§linked/")[0]
        with open(self.log, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([f"{prefix}owner§linked/tools/review/SKILL.md", reason])
        self.detect()

    def test_a_skill_outside_every_skills_directory_is_counted_and_passes(self):
        self.assertEqual(self.reasons().get(next(p for p in self.reasons()
                                                 if p.endswith("docs/skills/other/SKILL.md"))),
                         _mod.SKILL_MD_REASON)
        code, out, _err = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("PASS  The cleanup deleted no SKILL.md from a directory", out)
        self.assertIn("The cleanup deleted 1 other SKILL.md file(s) by design, outside the "
                      "directories that the collected links name.", out)

    def test_a_linked_skill_the_validation_deleted_fails_the_check_and_is_named(self):
        self.older_validation()
        code, out, err = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn(f"owner/linked tools/review/SKILL.md has the reason "
                      f"'{_mod.SKILL_MD_REASON}'", out)
        self.assertNotIn("owner/linked docs/skills/other/SKILL.md", out)
        self.assertIn("PASS  None of the", out)
        self.assertIn("ERROR deleted_files_report:", err)

    def test_a_linked_skill_deleted_for_any_other_reason_fails_the_check_too(self):
        """The assertion reads the rows of every reason. A linked skill under `skills/vendor/`
        is deleted as vendored rather than under the reason for a `SKILL.md`, because the
        vendored rule overrides the keep rule for a linked directory."""
        self.older_validation("inside vendored directory (vendor)")
        code, out, _err = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("owner/linked tools/review/SKILL.md has the reason 'inside vendored "
                      "directory (vendor)'", out)

    def test_without_the_collected_tree_the_rows_are_not_checked(self):
        self.older_validation()
        shutil.rmtree(self.collected / "owner§linked")
        code, out, _err = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn("NOT CHECKED  The 2 rows of a deleted SKILL.md", out)

    def test_report_only_exits_zero_and_still_logs_the_failure(self):
        """With the options that the runner's step passes, the finding is in the log messages,
        in the CSV and in a warning, and the run continues to the archive and the publication."""
        self.older_validation()
        out_csv = self.tmp / "deletions.csv"
        code, out, err = self.check("--report-only", "--out", str(out_csv))
        self.assertEqual(code, 0)
        self.assertIn("FAIL", out)
        self.assertIn("WARNING deleted_files_report:", err)
        with open(out_csv, newline="", encoding="utf-8") as f:
            notes = {row["subject"]: row["note"] for row in csv.DictReader(f)
                     if row["kind"] == "deleted_skill_md"}
        self.assertEqual(notes, {"tools/review/SKILL.md": f"linked, {_mod.SKILL_MD_REASON}",
                                 "docs/skills/other/SKILL.md": _mod.SKILL_MD_REASON})


class TestAnEmptyLinkedSkill(_Step):
    """A linked `SKILL.md` that the empty-file pass deleted shows no conflict about where a skill
    may be, because the pass runs before any keep rule, wherever a file lies."""

    def test_the_real_cleanup_deletes_it_as_empty_and_the_check_passes(self):
        self.collect("owner/linked", {"CLAUDE.md": CONTEXT,
                                      ".claude/skills/review": "../../tools/review\n",
                                      "tools/review/SKILL.md": "\n",
                                      "tools/review/notes.md": CONTEXT})
        self.run_validation()
        self.detect()
        self.assertIn("empty file", {reason for path, reason in self.reasons().items()
                                     if path.endswith("tools/review/SKILL.md")})
        code, out, _err = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("PASS  The cleanup deleted no SKILL.md from a directory", out)

class TestALogFromAnotherRun(_Step):
    """The output CSVs of one cleanup run are checked against the log of a later run. The second
    cleanup run deleted the context file that the detection published, which had become empty,
    and then dropped its repository."""

    def test_a_published_path_the_log_removed_fails(self):
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT})
        self.collect("owner/other", {"AGENTS.md": CONTEXT})
        self.run_validation()
        self.detect()
        self.assertEqual(self.check()[0], 0)

        (self.collected / "owner§kept" / "CLAUDE.md").write_text("  \n", encoding="utf-8")
        self.run_validation()
        self.assertEqual(self.reasons()["data/validated/owner§kept/CLAUDE.md"], "empty file")
        code, out, _err = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("owner/kept CLAUDE.md has 1 published row(s) and the reason 'empty file'",
                      out)
        self.assertIn(f"owner/kept (the repository) has 1 published row(s) and the reason "
                      f"'{DROPPED_REASON}'", out)
        self.assertNotIn("owner/other", out.split("Checking the three assertions")[1])


class TestTheRepositoryRowsAgainstTheTrees(_Step):
    """A directory missing from the validated tree is explained by a row, by the clone failure
    list, or by the validated list that a later step did not rewrite, and by nothing else."""

    def setUp(self):
        super().setUp()
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT})
        self.collect("owner/second", {"AGENTS.md": CONTEXT})
        self.collect("owner/dropped", {".claude/fixture-notes.md": "notes\n"})

    def _run_the_steps(self):
        self.run_validation()
        self.detect()

    def test_a_directory_that_left_without_a_row_fails(self):
        self._run_the_steps()
        shutil.rmtree(self.validated / "owner§second")
        listed = (self.validated / "repos.csv").read_text(encoding="utf-8").splitlines()
        (self.validated / "repos.csv").write_text(
            "\n".join(l for l in listed if not l.startswith("owner/second,")) + "\n",
            encoding="utf-8")
        code, out, _err = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("owner/second is missing from validated/ and has no row", out)

    def test_a_row_for_a_directory_still_in_the_tree_fails(self):
        self._run_the_steps()
        with open(self.log, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["data/validated/owner§kept", DROPPED_REASON])
        code, out, _err = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("owner/kept has a row and is still in validated/", out)

    def test_the_git_steps_prune_is_accounted_for(self):
        """The file history step removes the directory of a repository that git can no longer
        clone and leaves the validated list unchanged. The unchanged list distinguishes this
        removal from a deletion that the cleanup did not record."""
        self._run_the_steps()
        with contextlib.redirect_stdout(io.StringIO()):
            file_history._prune_from_repos(self.validated, "owner/second")
        self.assertFalse((self.validated / "owner§second").exists())
        code, out, _err = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("1 still in data/validated/repos.csv and removed by a later step", out)

    def test_a_clone_failure_the_validation_did_not_copy_is_accounted_for(self):
        (self.collected / "clone_failed.csv").write_text(
            "repo_name,repo_url,reason\nowner/second,https://github.com/owner/second,"
            "collector_error\n", encoding="utf-8")
        self._run_the_steps()
        self.assertFalse((self.validated / "owner§second").exists())
        code, out, _err = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("1 not copied because clone_failed.csv names them", out)

    def test_a_directory_the_collected_list_does_not_name_is_accounted_for(self):
        """The cleanup skips a tree that the collected list does not name, such as a repository
        removed from the input list. The check reads that list rather than listing the
        directory as a deletion that nothing recorded."""
        listed = (self.collected / "repos.csv").read_text(encoding="utf-8").splitlines()
        (self.collected / "repos.csv").write_text(
            "\n".join(l for l in listed if not l.startswith("owner/second,")) + "\n",
            encoding="utf-8")
        self._run_the_steps()
        self.assertFalse((self.validated / "owner§second").exists())
        code, out, _err = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn("1 not copied because collected/repos.csv does not list them", out)


class TestAPlanningDocumentFromAnotherRun(_Step):
    """The release publishes `planning-documents.csv` beside the mechanism CSVs. A planning
    document that a later cleanup run deleted is therefore a published path that the log
    removed, as a configuration artifact is."""

    def test_a_published_planning_document_the_log_removed_fails(self):
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT,
                                    ".specify/memory/constitution.md": CONTEXT,
                                    "specs/001-photo-albums/spec.md": CONTEXT})
        self.run_validation()
        self.detect()
        self.assertEqual(self.check()[0], 0)

        # The cleanup keeps an empty planning document, which an anchor can need, and removes
        # a link that leads outside the repository.
        spec = self.collected / "owner§kept" / "specs" / "001-photo-albums" / "spec.md"
        spec.unlink()
        spec.symlink_to("../../../../elsewhere.md")
        self.run_validation()
        code, out, _err = self.check()
        self.assertEqual(code, 2, out)
        self.assertIn("owner/kept specs/001-photo-albums/spec.md has 1 published row(s) and the "
                      "reason 'symlink leading outside the repository", out)

    def test_an_absent_csv_of_the_planning_documents_is_one(self):
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT})
        self.run_validation()
        self.detect()
        (self.output / "planning-documents.csv").unlink()
        code, _out, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("planning-documents.csv", err)


class TestTheInputs(_Step):
    """Exit 1 when the log or a mechanism CSV cannot be read, never a clean verdict."""

    def test_an_absent_log_is_one(self):
        code, _out, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("Run collected-file-cleanup to write it", err)

    def test_a_log_without_its_columns_is_one(self):
        self.log.write_text("path\nx\n", encoding="utf-8")
        code, _out, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("reason", err)

    def test_an_absent_per_mechanism_csv_is_one(self):
        """"Nothing published was deleted" is a claim about every mechanism CSV, and a missing
        CSV would restrict the claim to the other CSVs."""
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT})
        self.run_validation()
        self.detect()
        (self.output / "hooks.csv").unlink()
        code, _out, err = self.check()
        self.assertEqual(code, 1)
        self.assertIn("hooks.csv", err)

    def test_a_clone_failure_list_that_is_not_utf8_is_one_line_and_one(self):
        """A clone failure list that cannot be read is logged in one line, not as a
        traceback."""
        self.collect("owner/kept", {"CLAUDE.md": CONTEXT})
        self.run_validation()
        self.detect()
        (self.collected / "clone_failed.csv").write_bytes(b"repo_name\n\xff\xfe\n")
        code, _out, err = self.check()
        self.assertEqual(code, 1)
        self.assertTrue(err.splitlines()[-1].startswith("ERROR deleted_files_report: "),
                        err[-300:])
        self.assertIn("clone_failed.csv is not valid UTF-8", err.splitlines()[-1])


class TestThePieces(unittest.TestCase):

    def test_the_repository_is_the_segment_that_names_one(self):
        self.assertEqual(_mod.split_path("data/validated/o§r/.claude/x.md"), ("o§r", ".claude/x.md"))
        self.assertEqual(_mod.split_path("repos/o§r"), ("o§r", ""))
        self.assertEqual(_mod.split_path("elsewhere/x.md"), ("", "elsewhere/x.md"))

    def test_the_detail_in_parentheses_is_dropped(self):
        self.assertEqual(_mod.reason_of("inside submodule (vendor/next.js)"), "inside submodule")
        self.assertEqual(_mod.reason_of("empty file"), "empty file")

    def test_a_directory_takes_what_is_under_it_and_nothing_beside_it(self):
        published = {"o/r": {".claude/a/x.md": 1, ".claude/ab": 1}}
        log = [("o§r", ".claude/a", "empty directory after cleanup")]
        self.assertEqual(_mod.published_and_deleted(log, published),
                         [("o/r", ".claude/a", 1, "empty directory after cleanup")])



if __name__ == "__main__":
    unittest.main()
