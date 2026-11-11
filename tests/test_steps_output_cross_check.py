"""
Tests for agentcontext.steps.output_cross_check: the mechanism CSVs against repos.csv.

Strategy:
  - The default of `--output-dir` is the module-level constant OUTPUT_DIR. The tests point it
    at a temporary directory, write small mechanism CSVs and a reference CSV there, and
    restore it in tearDown.
  - The tests assert the exit codes that the runner and the verification suite depend on: 2
    exactly when the two sets disagree, and 1 when a CSV cannot be read.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_steps_output_cross_check -v
"""

import contextlib
import csv
import os
import hashlib
import importlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

_mod = importlib.import_module("agentcontext.steps.output_cross_check")
runner = importlib.import_module("agentcontext.runner")
MECHANISM_FILES = _mod.MECHANISM_FILES
PLANNING_FILE = "planning-documents.csv"
read_repo_names = _mod.read_repo_names
main = _mod.main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _OutputCase(unittest.TestCase):
    """A test case with the module's OUTPUT_DIR pointed at a temporary directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "output"
        self.output.mkdir()
        self._orig = _mod.OUTPUT_DIR
        _mod.OUTPUT_DIR = self.output

    def tearDown(self):
        _mod.OUTPUT_DIR = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    TOOLS = ("claude", "gemini", "codex", "copilot", "cursor", "opencode")

    def _write(self, filename: str, repo_names: list, heads: dict = None,
               tools: dict = None) -> Path:
        """Write a minimal mechanism CSV or repos.csv with each repository's name, collected
        commit and link. `heads` replaces the collected commit of the repositories that it
        names. `tools` maps a repository to the `tool` value of its row in a mechanism CSV, or
        to its set of tool flags in repos.csv. A repository that `tools` does not name gets
        the tool `any` and no flag."""
        p = self.output / filename
        reference = filename == "repos.csv"
        # The planning documents have no tool column.
        planned = filename == PLANNING_FILE
        with p.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["repo_name", "head_sha",
                        *(self.TOOLS if reference else [] if planned else ["tool"]),
                        "github_link"])
            for name in repo_names:
                head = (heads or {}).get(name, hashlib.sha1(name.encode()).hexdigest())
                if reference:
                    flags = (tools or {}).get(name, set())
                    tool = ["TRUE" if t in flags else "FALSE" for t in self.TOOLS]
                elif planned:
                    tool = []
                else:
                    tool = [(tools or {}).get(name, "any")]
                w.writerow([name, head, *tool, f"https://github.com/{name}"])
        return p

    def _write_all(self, detail: dict, reference: list) -> None:
        """Write repos.csv, every mechanism CSV and the CSV of the planning documents. A CSV
        that `detail` does not name has no rows."""
        for filename in (*MECHANISM_FILES, PLANNING_FILE):
            self._write(filename, detail.get(filename, []))
        self._write("repos.csv", reference)

    def _run(self) -> int:
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return main([])

    def _report_rows(self) -> list:
        with (self.output / "consistency-report.csv").open(
                newline="", encoding="utf-8") as f:
            return list(csv.reader(f))

    def _mismatch_rows(self) -> list:
        with (self.output / "consistency-mismatch.csv").open(
                newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# read_repo_names
# ---------------------------------------------------------------------------

class TestReadRepoNames(_OutputCase):

    def test_returns_the_repo_name_column_as_a_set(self):
        p = self._write("commands.csv", ["o/a", "o/b"])
        self.assertEqual(read_repo_names(p), {"o/a", "o/b"})

    def test_deduplicates(self):
        p = self._write("commands.csv", ["o/a", "o/a", "o/b"])
        self.assertEqual(read_repo_names(p), {"o/a", "o/b"})

    def test_header_only_file_is_empty(self):
        p = self._write("commands.csv", [])
        self.assertEqual(read_repo_names(p), set())


# ---------------------------------------------------------------------------
# main  (the union check, its outputs, and the exit-code contract)
# ---------------------------------------------------------------------------

class TestMain(_OutputCase):

    def test_matching_sets_return_zero(self):
        self._write_all({"commands.csv": ["o/a", "o/b"]}, ["o/a", "o/b"])
        self.assertEqual(self._run(), 0)

    def test_union_spans_all_mechanism_csvs(self):
        # No single file contains every repository, and their union must still match.
        self._write_all({"commands.csv": ["o/a"], "skills.csv": ["o/b"]},
                        ["o/a", "o/b"])
        self.assertEqual(self._run(), 0)

    def test_repo_only_in_a_mechanism_csv_fails(self):
        self._write_all({"commands.csv": ["o/a", "o/b"]}, ["o/a"])
        self.assertEqual(self._run(), 2)

    def test_repo_only_in_repos_csv_fails(self):
        self._write_all({"commands.csv": ["o/a"]}, ["o/a", "o/b"])
        self.assertEqual(self._run(), 2)

    def test_report_is_written_on_a_clean_run(self):
        self._write_all({"commands.csv": ["o/a", "o/b"]}, ["o/a", "o/b"])
        self._run()
        rows = dict(self._report_rows()[1:])  # skip the header
        self.assertEqual(rows["commands.csv"], "2")
        self.assertEqual(rows["TOTAL (mechanism CSVs)"], "2")
        self.assertEqual(rows["repos.csv"], "2")

    def test_a_repeated_check_writes_the_same_report_again(self):
        """The runner records the step only when the step wrote every output that it
        produces. A second run over the same CSVs, such as on a Start after an image update,
        therefore writes the report again although its content is unchanged."""
        self._write_all({"commands.csv": ["o/a", "o/b"]}, ["o/a", "o/b"])
        self._run()
        report = self.output / "consistency-report.csv"
        os.utime(report, ns=(1_000_000_000, 1_000_000_000))
        before = runner._output_signature(report)
        self._run()
        self.assertTrue(runner._wrote_output(report, before))

    def test_the_report_ends_each_record_with_lf(self):
        """The report ends each record with LF, as every CSV of the pipeline does, rather than
        with the CRLF that the csv module writes by default."""
        self._write_all({"commands.csv": ["o/a", "o/b"]}, ["o/a", "o/b"])
        self.assertEqual(self._run(), 0)
        self.assertNotIn(b"\r\n", (self.output / "consistency-report.csv").read_bytes())
        self.assertEqual(dict(self._report_rows()[1:])["commands.csv"], "2")

    def test_the_mismatch_file_ends_each_record_with_lf(self):
        """The mismatch file ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default."""
        self._write_all({"commands.csv": ["o/a", "o/extra"]}, ["o/a", "o/missing"])
        self.assertEqual(self._run(), 2)
        self.assertNotIn(b"\r\n", (self.output / "consistency-mismatch.csv").read_bytes())
        self.assertEqual({r["repo_name"] for r in self._mismatch_rows()}, {"o/extra", "o/missing"})

    def test_report_is_written_on_a_mismatch(self):
        self._write_all({"commands.csv": ["o/a", "o/b"]}, ["o/a"])
        self._run()
        self.assertTrue((self.output / "consistency-report.csv").is_file())

    def test_mismatch_file_lists_both_sides(self):
        self._write_all({"commands.csv": ["o/a", "o/extra"]}, ["o/a", "o/missing"])
        self._run()
        by_repo = {r["repo_name"]: r["source"] for r in self._mismatch_rows()}
        self.assertEqual(by_repo["o/extra"], "mechanism_only")
        self.assertEqual(by_repo["o/missing"], "reference_only")

    def test_no_mismatch_file_on_a_clean_run(self):
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        self._run()
        self.assertFalse((self.output / "consistency-mismatch.csv").exists())

    def test_stale_mismatch_file_is_removed_on_a_clean_run(self):
        (self.output / "consistency-mismatch.csv").write_text("stale\n")
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        self.assertEqual(self._run(), 0)
        self.assertFalse((self.output / "consistency-mismatch.csv").exists())

    def test_per_file_counts_are_distinct_repos_not_rows(self):
        # commands.csv has three rows but two repositories.
        self._write_all({"commands.csv": ["o/a", "o/a", "o/b"]}, ["o/a", "o/b"])
        self._run()
        rows = dict(self._report_rows()[1:])
        self.assertEqual(rows["commands.csv"], "2")

    def test_a_mechanism_row_from_another_commit_fails(self):
        # Rows of one repository from two collected commits are two collections joined under
        # one name. The repository names alone cannot show the join.
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        self._write("skills.csv", ["o/a"], heads={"o/a": "b" * 40})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "head_sha_differs")])

    def test_a_repository_without_its_collected_commit_fails(self):
        # A published repository with rows that lack the collected commit cannot be traced to
        # the files that the rows describe.
        self._write_all({}, [])
        for filename in ("commands.csv", "repos.csv"):
            self._write(filename, ["o/a", "o/b"], heads={"o/b": ""})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/b", "head_sha_missing")])

    def test_tool_flags_that_are_the_tools_of_the_rows_pass(self):
        """The flags of o/a are the union of the tools of its rows in two CSVs. One of these
        rows names two tools, because both read the file through a link. The only row of o/b
        is a shared file, which sets no flag."""
        self._write_all({}, [])
        self._write("commands.csv", ["o/a", "o/b"], tools={"o/a": "claude|cursor"})
        self._write("skills.csv", ["o/a"], tools={"o/a": "codex"})
        self._write("repos.csv", ["o/a", "o/b"], tools={"o/a": {"claude", "codex", "cursor"}})
        self.assertEqual(self._run(), 0)

    def test_a_tool_flag_without_a_row_fails(self):
        # A flag without a row credits a tool with no configuration artifact.
        self._write_all({"commands.csv": ["o/a"]}, [])
        self._write("repos.csv", ["o/a"], tools={"o/a": {"gemini"}})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "tool_flags_differ")])

    def test_an_other_row_sets_no_flag(self):
        """other.csv lists files that no detector of a tool reads, and the release does not
        publish it. No published row would explain a flag that only an `other` row sets."""
        self._write_all({"context-files.csv": ["o/a"]}, [])
        self._write("other.csv", ["o/a"], tools={"o/a": "copilot"})
        self._write("repos.csv", ["o/a"])
        self.assertEqual(self._run(), 0)
        self._write("repos.csv", ["o/a"], tools={"o/a": {"copilot"}})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "tool_flags_differ")])

    def test_a_flag_of_a_repository_with_no_named_row_fails(self):
        """The check compares the flags of every repository in repos.csv. A repository with
        rows in other.csv alone is among them, although no row of the ten mechanisms gives it
        a tool."""
        self._write_all({"other.csv": ["o/a"]}, [])
        self._write("repos.csv", ["o/a"], tools={"o/a": {"gemini"}})
        self.assertEqual(self._run(), 2)
        self.assertIn(("o/a", "tool_flags_differ"),
                      [(r["repo_name"], r["source"]) for r in self._mismatch_rows()])

    def test_a_row_tool_without_its_flag_fails(self):
        self._write_all({}, ["o/a"])
        self._write("hooks.csv", ["o/a"], tools={"o/a": "copilot"})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "tool_flags_differ")])

    def _could_not_check(self) -> str:
        """Run the check, assert that it returns 1 (could not check) with one error line
        rather than a traceback, and return that line."""
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(main([]), 1)
        self.assertTrue(err.getvalue().splitlines()[-1].startswith("ERROR output_cross_check: "),
                        err.getvalue()[-300:])
        return err.getvalue().splitlines()[-1]

    def test_a_tool_value_detection_cannot_write_is_one(self):
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        self._write("rules.csv", ["o/a"], tools={"o/a": "cursor|claude"})
        self.assertIn("rules.csv", self._could_not_check())

    def test_a_missing_mechanism_csv_is_one(self):
        # Only repos.csv exists. A missing mechanism CSV means that the detection step or the
        # file history step did not finish. The check then could not run, and that outcome is
        # not a difference.
        self._write("repos.csv", ["o/a"])
        self.assertIn("No such file", self._could_not_check())

    def test_a_missing_repos_csv_is_one(self):
        self._write_all({"commands.csv": ["o/a"]}, [])
        (self.output / "repos.csv").unlink()
        self.assertIn("repos.csv", self._could_not_check())

    def test_a_repos_csv_whose_collected_commit_is_another_fails(self):
        """repos.csv has `head_sha` too. A repository that it lists at another commit than
        its rows is two collections under one name."""
        self._write_all({"commands.csv": ["o/a"]}, [])
        self._write("repos.csv", ["o/a"], heads={"o/a": "b" * 40})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "head_sha_differs")])

    def test_a_repos_csv_row_without_its_collected_commit_fails(self):
        self._write_all({"commands.csv": ["o/a"]}, [])
        self._write("repos.csv", ["o/a"], heads={"o/a": ""})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "head_sha_missing")])

    def test_a_repository_with_rows_in_other_csv_alone_is_in_the_mechanism_csvs(self):
        """The check compares repos.csv with the union of the mechanism CSVs, other.csv
        included. The detection step lists a repository that has only unclassified files, and
        `file_history.reconcile_repos_csv` keeps it."""
        self._write_all({"other.csv": ["o/a"]}, ["o/a"])
        self.assertEqual(self._run(), 0)
        rows = dict(self._report_rows()[1:])
        self.assertEqual(rows["other.csv"], "1")
        self.assertEqual(rows["TOTAL (mechanism CSVs)"], "1")

    def test_planning_documents_of_a_configured_repository_pass(self):
        """A configured repository with planning documents passes. The report counts the
        repositories with planning documents on a row of its own, outside the total of the
        mechanism CSVs."""
        self._write_all({"commands.csv": ["o/a"], PLANNING_FILE: ["o/a", "o/a"]}, ["o/a"])
        self.assertEqual(self._run(), 0)
        rows = dict(self._report_rows()[1:])
        self.assertEqual(rows[PLANNING_FILE], "1")
        self.assertEqual(rows["TOTAL (mechanism CSVs)"], "1")

    def test_a_repository_with_planning_documents_alone_fails(self):
        """A planning document does not make a repository configured. The release would
        publish the planning documents of a repository without a row in repos.csv, and no
        output would count that repository as configured."""
        self._write_all({"commands.csv": ["o/a"], PLANNING_FILE: ["o/a", "o/planned"]},
                        ["o/a"])
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/planned", "planning_only")])

    def test_planning_documents_do_not_make_a_repository_configured(self):
        """The check compares repos.csv with the union of the mechanism CSVs alone. A row of
        repos.csv for a repository with planning documents and no configuration artifact is
        therefore a difference."""
        self._write_all({"commands.csv": ["o/a"], PLANNING_FILE: ["o/b"]}, ["o/a", "o/b"])
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/b", "reference_only")])
        rows = dict(self._report_rows()[1:])
        self.assertEqual((rows["TOTAL (mechanism CSVs)"], rows[PLANNING_FILE]), ("1", "1"))

    def test_a_planning_document_from_another_commit_fails(self):
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        self._write(PLANNING_FILE, ["o/a"], heads={"o/a": "c" * 40})
        self.assertEqual(self._run(), 2)
        self.assertEqual([(r["repo_name"], r["source"]) for r in self._mismatch_rows()],
                         [("o/a", "head_sha_differs")])

    def test_missing_planning_documents_are_one(self):
        """The detection step writes the CSV of the planning documents together with the
        mechanism CSVs. A missing CSV of the planning documents means that the step did not
        finish."""
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        (self.output / PLANNING_FILE).unlink()
        self.assertIn(PLANNING_FILE, self._could_not_check())

    def test_another_output_directory_is_read_and_written(self):
        """With `--output-dir`, a test can check how the command handles absent inputs."""
        self._write_all({"commands.csv": ["o/a"]}, ["o/a"])
        elsewhere = self.tmp / "elsewhere"
        self.output.rename(elsewhere)
        self.output.mkdir()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--output-dir", str(elsewhere)]), 0)
        self.assertTrue((elsewhere / "consistency-report.csv").is_file())
        self.assertFalse((self.output / "consistency-report.csv").exists())


class TestAFieldLongerThanTheDefaultLimit(unittest.TestCase):
    """The detection step joins every file that a context file references into
    `referenced_file`, with no bound on its length. The test runs in a process of its own,
    because the csv module's field limit is global and the modules that other suites import
    raise it."""

    PROGRAM = ('import sys\n'
               'from agentcontext.steps import output_cross_check\n'
               'output_cross_check.read_repo_commits(sys.argv[1])\n')

    def test_the_step_reads_it(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context-files.csv"
            references = ";".join(f"docs/d{number}.md" for number in range(20_000))
            path.write_text("repo_name,head_sha,referenced_file\n"
                            f"o/r,{'a' * 40},\"{references}\"\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", self.PROGRAM, str(path)], capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
