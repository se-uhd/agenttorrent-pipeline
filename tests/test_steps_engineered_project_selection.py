"""
Tests for agentcontext.steps.engineered_project_selection, which derives the sample from the
labels of the classification.

Strategy:
  - The step applies one predicate to one CSV. Every test builds a small annotated CSV under a
    temporary directory and reads the sample back, and no test reads the full dataset.
  - The tests assert which rows the filter drops as much as which rows it keeps. The
    classification stage writes three labels, `true`, `false` and `unclear`, and a looser
    match would admit the rows labeled `unclear`.
  - By default, the step accepts the population that the classification labeled, whatever
    its size. With `--expect-rows`, it accepts a matching input and keeps an earlier output
    on a mismatch.
  - The tests run `--check` for each of its outcomes, because its exit code is its result: 0
    when the file on disk is the sample, 2 when it is not, and 1 when the step could not
    determine the sample.
  - The tests check what the write leaves behind. The collector reads the sample once at the
    start of a long run. A temporary file left in the classification directory, or a sample
    appended to instead of replaced, would be a shorter or longer sample that nothing
    reports.
  - One test reads the collector's default, because the step exists to write the file that
    the collector reads. The test compares the two modules' spellings of that path rather
    than trusting either on its own.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_steps_engineered_project_selection -v
"""

import contextlib
import csv
import importlib
import io
import os
import stat
import subprocess
import sys
import tempfile
import zlib
import unittest
from unittest import mock
from pathlib import Path

_mod = importlib.import_module("agentcontext.steps.engineered_project_selection")
from agentcontext import commits
from agentcontext import STAGE_DIR
from tests import _logged
_collector = importlib.import_module("agentcontext.steps.repository_cloning")

# The columns that 2_classification/1_classify_repos.py writes, in the order in which it
# writes them.
HEADER = ["repo_name", "github_id", "default_branch", "engineered_project", "justification"]


def _row(repo_name, label="true", branch="main", justification="why", github_id=None):
    """A row of the annotated list, with a GitHub id derived from the name unless `github_id`
    gives one."""
    return {"repo_name": repo_name,
            "github_id": str(zlib.crc32(repo_name.encode()) if github_id is None else github_id),
            "default_branch": branch, "engineered_project": label,
            "justification": justification}


class _SampleSelectionCase(unittest.TestCase):
    """An annotated CSV and a sample path, both under a temporary directory.

    The sample's parent directory does not exist until a run creates it. Every test that
    writes the sample therefore also checks that the run creates the directory.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.annotated = self.dir / "repos_annotated.csv"
        self.out = self.dir / "classification" / "repos_annotated_included.csv"
        self.included = 0

    def write_annotated(self, rows, header=HEADER):
        """Write the annotated CSV and record how many of its rows the sample keeps."""
        with open(self.annotated, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        self.included = sum(1 for r in rows if r.get("engineered_project") == "true")

    def run_sample_selection(self, *args, expect_rows=None):
        """Run main() over the fixture and return the exit code, the INFO lines of its log and
        its WARNING and ERROR lines.

        `--expect-rows` defaults to the number of `true` rows in the fixture. A test that needs
        the count check to fail passes another number.
        """
        wanted = self.included if expect_rows is None else expect_rows
        argv = ['engineered_project_selection', "--annotated-csv", str(self.annotated),
                "-o", str(self.out), "--expect-rows", str(wanted), *args]
        out, err = io.StringIO(), io.StringIO()
        saved, sys.argv = sys.argv, argv
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = _mod.main()
        finally:
            sys.argv = saved
        return (code, *_logged.split(out.getvalue() + err.getvalue()))

    def read_sample(self):
        """The written sample as (header, rows)."""
        with open(self.out, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return list(reader.fieldnames or []), list(reader)

    def beside_the_sample(self):
        """The names in the sample's directory other than the sample. The tests expect
        none."""
        if not self.out.parent.is_dir():
            return []
        return sorted(p.name for p in self.out.parent.iterdir() if p != self.out)


# ---------------------------------------------------------------------------
# The filter
# ---------------------------------------------------------------------------
class TestTheFilter(_SampleSelectionCase):
    """The filter applies one predicate, keeps the order of the rows and leaves the columns
    unchanged."""

    def test_only_an_engineered_row_reaches_the_sample(self):
        self.write_annotated([_row("a/one", "true"), _row("b/two", "false"),
                              _row("c/three", "true")])
        code, out, err = self.run_sample_selection()
        self.assertEqual(code, 0, err)
        _, rows = self.read_sample()
        self.assertEqual([r["repo_name"] for r in rows], ["a/one", "c/three"])

    def test_a_justification_longer_than_the_default_limit_of_the_csv_module_is_read(self):
        """Nothing bounds the length of the model's justification. The test runs the step in a
        process of its own, as the runner does, because the csv module's field limit is then
        the one that the step's imports set."""
        self.write_annotated([_row("a/one", justification="Because " * 20_000)])
        program = ("import sys\n"
                   "from pathlib import Path\n"
                   "from agentcontext.steps import engineered_project_selection as step\n"
                   "header, rows = step.derive_sample(Path(sys.argv[1]))\n"
                   "print([row['repo_name'] for row in rows])\n")
        result = subprocess.run([sys.executable, "-c", program, str(self.annotated)],
                                capture_output=True, text=True,
                                env={**os.environ,
                                     "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.assertEqual((result.returncode, result.stdout), (0, "['a/one']\n"), result.stderr)

    def test_an_unclear_row_is_not_a_sample_row(self):
        """The classification stage writes three labels. A truthiness test or a `!= "false"`
        would admit every row labeled `unclear`."""
        self.write_annotated([_row("a/one", "true"), _row("b/two", "unclear")])
        self.run_sample_selection()
        _, rows = self.read_sample()
        self.assertEqual([r["repo_name"] for r in rows], ["a/one"])

    def test_the_label_is_matched_exactly(self):
        """The classification stage writes `true`. Any other spelling is another value, and
        reading it as `true` would put rows in the sample that the labels do not mark as
        engineered projects."""
        self.write_annotated([_row("a/one", "TRUE"), _row("b/two", "True"),
                              _row("c/three", " true"), _row("d/four", "true")])
        self.run_sample_selection(expect_rows=1)
        _, rows = self.read_sample()
        self.assertEqual([r["repo_name"] for r in rows], ["d/four"])

    def test_the_order_of_the_annotated_csv_is_the_order_of_the_sample(self):
        """The classification writes the annotated CSV in the order of the sampling frame, and
        the filter does not sort the rows again. A sample in another order is a different file
        byte for byte, and `--check` would report the sample on disk as stale on every run."""
        names = ["z/last", "a/first", "m/middle"]
        self.write_annotated([_row(name) for name in names])
        self.run_sample_selection()
        _, rows = self.read_sample()
        self.assertEqual([r["repo_name"] for r in rows], names)

    def test_every_column_is_copied_unchanged(self):
        self.write_annotated([_row("a/one", "true", branch="trunk",
                                   justification="ships a test suite")])
        self.run_sample_selection()
        header, rows = self.read_sample()
        self.assertEqual(header, HEADER)
        self.assertEqual(rows[0], _row("a/one", "true", branch="trunk",
                                       justification="ships a test suite"))

    def test_a_column_that_the_filter_does_not_list_survives(self):
        """The step takes the header from the file rather than declaring its own. A column that
        a classification run adds therefore stays in the sample instead of disappearing without
        a warning."""
        header = HEADER + ["model"]
        row = _row("a/one")
        row["model"] = "gpt-5"
        self.write_annotated([row], header=header)
        self.run_sample_selection()
        written_header, rows = self.read_sample()
        self.assertEqual(written_header, header)
        self.assertEqual(rows[0]["model"], "gpt-5")

    def test_a_row_with_a_missing_trailing_field_keeps_its_place(self):
        """A row that the CSV writer left short is still a repository that the classification
        labeled. The step writes it with the field empty rather than dropping it."""
        self.annotated.write_text(
            "repo_name,github_id,default_branch,engineered_project,justification\n"
            "a/one,1,main,true\n", encoding="utf-8")
        self.included = 1
        code, _, err = self.run_sample_selection()
        self.assertEqual(code, 0, err)
        _, rows = self.read_sample()
        self.assertEqual(rows[0]["repo_name"], "a/one")
        self.assertEqual(rows[0]["justification"], "")

    def test_an_empty_sample_is_still_a_sample(self):
        """A sample with no row labeled `true` is a valid result, and the step writes
        it rather than treating zero as a failure. An explicit `--expect-rows` can
        require a nonzero population."""
        self.write_annotated([_row("a/one", "false")])
        code, _, err = self.run_sample_selection(expect_rows=0)
        self.assertEqual(code, 0, err)
        header, rows = self.read_sample()
        self.assertEqual(header, HEADER)
        self.assertEqual(rows, [])


# ---------------------------------------------------------------------------
# The row assertion
# ---------------------------------------------------------------------------
class TestTheRowAssertion(_SampleSelectionCase):
    """The step accepts a population of any size unless the caller requests a count check."""

    def test_default_accepts_the_classified_population(self):
        self.write_annotated([_row("a/one"), _row("b/two", "false"),
                              _row("c/three", "unclear"), _row("d/four")])
        code = _mod.main(["--annotated-csv", str(self.annotated), "-o", str(self.out)])
        self.assertEqual(code, 0)
        self.assertEqual([r["repo_name"] for r in self.read_sample()[1]],
                         ["a/one", "d/four"])

    def test_default_check_accepts_a_changed_population(self):
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        self.write_annotated([_row("a/one"), _row("b/two")])
        args = ["--annotated-csv", str(self.annotated), "-o", str(self.out)]
        self.assertEqual(_mod.main([*args, "--check"]), 2)
        self.assertEqual(_mod.main(args), 0)
        self.assertEqual(_mod.main([*args, "--check"]), 0)
        self.assertEqual(len(self.read_sample()[1]), 2)

    def test_default_writes_an_empty_classified_population(self):
        self.write_annotated([_row("a/one", "false"), _row("b/two", "unclear")])
        self.assertEqual(_mod.main(["--annotated-csv", str(self.annotated),
                                    "-o", str(self.out)]), 0)
        self.assertEqual(self.read_sample(), (HEADER, []))

    def test_a_count_other_than_the_expected_one_returns_one(self):
        self.write_annotated([_row("a/one"), _row("b/two")])
        code, _, err = self.run_sample_selection(expect_rows=3)
        self.assertEqual(code, 1)
        # The test asserts the phrases rather than the digits. A 2 and a 3 occur in a temporary
        # path often enough that the bare digits would match with the counts left out of the
        # message.
        self.assertIn("has 2 row(s)", err)
        self.assertIn("--expect-rows expected 3", err)

    def test_a_refused_count_writes_nothing(self):
        self.write_annotated([_row("a/one"), _row("b/two")])
        self.run_sample_selection(expect_rows=3)
        self.assertFalse(self.out.exists(),
                         "a sample that nobody expected was written anyway")
        self.assertEqual(self.beside_the_sample(), [])

    def test_a_refused_count_leaves_an_earlier_sample_alone(self):
        """The collector reads whichever file is on disk. A refusal that truncated or replaced
        the previous sample would give the collector a list that nobody checked."""
        self.write_annotated([_row("a/one"), _row("b/two")])
        self.run_sample_selection()
        before = self.out.read_bytes()
        self.write_annotated([_row("a/one")])
        code, _, _ = self.run_sample_selection(expect_rows=2)
        self.assertEqual(code, 1)
        self.assertEqual(self.out.read_bytes(), before)

    def test_an_explicit_count_accepts_matching_input(self):
        """A caller can require a known size without changing the default behavior."""
        self.write_annotated([_row("a/one"), _row("b/two"), _row("c/three")])
        code, out, err = self.run_sample_selection(expect_rows=3)
        self.assertEqual(code, 0, err)
        self.assertIn("Selected 3 repositories labeled engineered_project=true", out)
        self.assertEqual(len(self.read_sample()[1]), 3)

    def test_the_refusal_states_why_nothing_was_written(self):
        self.write_annotated([_row("a/one")])
        _, _, err = self.run_sample_selection(expect_rows=2)
        self.assertIn("Nothing was written", err)
        self.assertIn("--expect-rows", err)


# ---------------------------------------------------------------------------
# The header
# ---------------------------------------------------------------------------
class TestTheHeaderRefusal(_SampleSelectionCase):
    """The step checks each required column, because a file without one of them fails in a
    different way."""

    def test_an_annotated_csv_without_the_label_column_is_refused(self):
        """Without `engineered_project`, every row compares unequal to `true`, and the filter
        would write an empty file as the sample."""
        header = ["repo_name", "default_branch", "justification"]
        self.annotated.write_text(",".join(header) + "\na/one,main,why\n",
                                  encoding="utf-8")
        code, _, err = self.run_sample_selection(expect_rows=0)
        self.assertEqual(code, 1)
        self.assertIn("engineered_project", err)

    def test_an_annotated_csv_without_the_repo_column_is_refused(self):
        """The collector reads the repositories of this file from the column that `--repo-col`
        names, repo_name by default. A sample without that column names no repository to
        clone."""
        header = ["github_id", "default_branch", "engineered_project", "justification"]
        self.annotated.write_text(",".join(header) + "\n1,main,true,why\n",
                                  encoding="utf-8")
        code, _, err = self.run_sample_selection(expect_rows=1)
        self.assertEqual(code, 1)
        self.assertIn("repo_name", err)

    def test_the_refusal_prints_the_header_it_read(self):
        header = ["repo", "github_id", "engineered_project"]
        self.annotated.write_text(",".join(header) + "\na/one,1,true\n",
                                  encoding="utf-8")
        _, _, err = self.run_sample_selection(expect_rows=1)
        # The test asserts the whole list, because `repo` alone also occurs in the name of the
        # file at the start of the message.
        self.assertIn("header is ['repo', 'github_id', 'engineered_project']", err)
        self.assertIn("lacks the column(s) ['repo_name']", err)

    def test_a_list_without_github_ids_is_refused(self):
        """Without the GitHub ids, two names of one repository pass without a warning."""
        header = ["repo_name", "default_branch", "engineered_project", "justification"]
        self.annotated.write_text(",".join(header) + "\na/one,main,true,why\n",
                                  encoding="utf-8")
        code, _, err = self.run_sample_selection(expect_rows=1)
        self.assertEqual(code, 1)
        self.assertIn("lacks the column(s) ['github_id']", err)

    def test_two_names_of_one_repository_are_refused(self):
        """apache/activemq-artemis and apache/artemis, in the October 5-6 collection, are
        GitHub's repository 36057260 under its old and its new name."""
        self.write_annotated([_row("apache/activemq-artemis", github_id="36057260"),
                              _row("o/other"),
                              _row("apache/artemis", github_id="36057260"),
                              _row("o/labeled-false", "false", github_id="36057260")])
        code, _, err = self.run_sample_selection()
        self.assertEqual(code, 1)
        self.assertIn("1 GitHub id(s) appear in two rows of the sample", err)
        self.assertIn("apache/activemq-artemis and apache/artemis with id 36057260", err)
        self.assertFalse(self.out.exists())

    def test_names_that_github_did_not_find_have_no_id(self):
        self.write_annotated([_row("gone/one", github_id=""), _row("gone/two", github_id="")])
        code, _, err = self.run_sample_selection()
        self.assertEqual(code, 0, err)
        self.assertEqual([r["repo_name"] for r in self.read_sample()[1]], ["gone/one", "gone/two"])

    def test_a_refused_header_writes_nothing(self):
        self.annotated.write_text("repo_name\na/one\n", encoding="utf-8")
        self.run_sample_selection(expect_rows=0)
        self.assertFalse(self.out.exists())

    def test_an_empty_file_is_refused_rather_than_read_as_an_empty_sample(self):
        self.annotated.write_text("", encoding="utf-8")
        code, _, err = self.run_sample_selection(expect_rows=0)
        self.assertEqual(code, 1)
        self.assertIn("lacks the column", err)

    def test_an_absent_annotated_csv_names_the_archive_it_comes_from(self):
        code, _, err = self.run_sample_selection(expect_rows=0)
        self.assertEqual(code, 1)
        self.assertIn("repos_annotated.7z", err)

    def test_an_unreadable_annotated_csv_is_named_with_the_error(self):
        """The text of an OSError names the error alone, and the line names the file."""
        self.write_annotated([_row("a/one")])
        with mock.patch.object(_mod, "derive_sample",
                               side_effect=OSError(13, "Permission denied")):
            code, _, err = self.run_sample_selection(expect_rows=1)
        self.assertEqual(code, 1)
        self.assertIn(f"Could not read {self.annotated} ([Errno 13] Permission denied)", err)


# ---------------------------------------------------------------------------
# --check
# ---------------------------------------------------------------------------
class TestCheck(_SampleSelectionCase):
    """The exit code of `--check` is its result, and the tests assert each of its three exit
    codes."""

    def test_a_sample_that_matches_exits_zero(self):
        self.write_annotated([_row("a/one"), _row("b/two")])
        self.run_sample_selection()
        code, out, err = self.run_sample_selection("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("2 rows", out)

    def test_a_carriage_return_inside_a_field_matches_after_the_step_writes_the_sample(self):
        """Universal newlines would turn the carriage return in a quoted justification into a
        line feed, and the check would report the sample that the step had just written as a
        difference. CRLF line endings still match."""
        self.write_annotated([_row("a/one", justification="line one\rline two"),
                              _row("b/two", justification="line one\r\nline two")])
        self.run_sample_selection()
        code, _, err = self.run_sample_selection("--check")
        self.assertEqual(code, 0, err)
        self.out.write_bytes(self.out.read_bytes().replace(b'"\n', b'"\r\n'))
        code, _, err = self.run_sample_selection("--check")
        self.assertEqual(code, 0, err)

    def test_a_sample_that_differs_exits_two(self):
        self.write_annotated([_row("a/one"), _row("b/two")])
        self.run_sample_selection()
        self.write_annotated([_row("a/one"), _row("b/two"), _row("c/three")])
        code, _, err = self.run_sample_selection("--check")
        self.assertEqual(code, 2)
        self.assertIn("differs from the sample derived from", err)
        # A checkout that extracts a newer repos_annotated.7z keeps the sample derived from the
        # earlier labels, and the message names the run that writes it again.
        self.assertIn("Run engineered-project-selection without --check", err)

    def test_a_sample_that_is_not_there_exits_two(self):
        """A run would write the sample, and the missing file therefore differs from what the
        step writes. The operator fixes such a difference by running the step. It is not a
        failure to check."""
        self.write_annotated([_row("a/one")])
        code, _, err = self.run_sample_selection("--check")
        self.assertEqual(code, 2)
        self.assertIn("does not exist", err)

    def test_an_absent_annotated_csv_exits_one(self):
        """An absent input leaves nothing to compare against, and that is not agreement.
        tests/test_check_modes.py requires the same of every command that reports a verdict
        through its exit code."""
        code, _, _ = self.run_sample_selection("--check", expect_rows=0)
        self.assertEqual(code, 1)

    def test_a_refused_row_count_exits_one_rather_than_reporting_a_difference(self):
        """When the step cannot determine the sample, it reports that it could not check
        instead of reporting the file on disk as stale."""
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        code, _, _ = self.run_sample_selection("--check", expect_rows=2)
        self.assertEqual(code, 1)

    def test_check_writes_nothing(self):
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        before = self.out.read_bytes()
        self.write_annotated([_row("a/one"), _row("b/two")])
        self.run_sample_selection("--check")
        self.assertEqual(self.out.read_bytes(), before)
        self.assertEqual(self.beside_the_sample(), [])

    def test_check_does_not_create_the_output_directory(self):
        self.write_annotated([_row("a/one")])
        self.run_sample_selection("--check")
        self.assertFalse(self.out.parent.exists(),
                         "a read-only check created a directory")


# ---------------------------------------------------------------------------
# The write
# ---------------------------------------------------------------------------
class TestTheWrite(_SampleSelectionCase):
    """The collector treats the sample as its immutable input, and the step therefore replaces
    the file in one operation."""

    def test_no_temporary_file_is_left_behind(self):
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        self.assertEqual(self.beside_the_sample(), [])

    def test_an_existing_sample_is_replaced_rather_than_appended_to(self):
        self.write_annotated([_row("a/one"), _row("b/two"), _row("c/three")])
        self.run_sample_selection()
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        _, rows = self.read_sample()
        self.assertEqual([r["repo_name"] for r in rows], ["a/one"])
        self.assertEqual(self.beside_the_sample(), [])

    def test_the_directory_the_sample_goes_in_is_created(self):
        """The classification stage's directory exists in this repository. The option can name
        any path, however, and a clean replication tree contains nothing."""
        self.out = self.dir / "one" / "two" / "repos_annotated_included.csv"
        self.write_annotated([_row("a/one")])
        code, _, err = self.run_sample_selection()
        self.assertEqual(code, 0, err)
        self.assertTrue(self.out.is_file())

    def test_the_sample_is_readable_by_everyone(self):
        """`mkstemp` creates a file with mode 0600. The collector reads the sample, and a
        replication package ships it. The step therefore sets the mode rather than keeping the
        mode of the temporary file."""
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        self.assertEqual(stat.S_IMODE(os.stat(self.out).st_mode), 0o644)

    def test_the_sample_ends_every_row_with_a_newline(self):
        """csv.DictWriter writes CRLF by default. The sample on disk uses LF, as does
        everything that the classification stage writes."""
        self.write_annotated([_row("a/one")])
        self.run_sample_selection()
        self.assertNotIn(b"\r\n", self.out.read_bytes())
        self.assertTrue(self.out.read_bytes().endswith(b"\n"))

    def test_a_row_wider_than_the_header_writes_nothing(self):
        """csv.DictReader collects the extra fields under a key for which the writer has no
        column. The run stops there on purpose, because a row that the CSV cannot represent
        belongs to a CSV that this step should not filter."""
        self.annotated.write_text(
            "repo_name,github_id,default_branch,engineered_project,justification\n"
            "a/one,1,main,true,why,extra\n", encoding="utf-8")
        self.included = 1
        with self.assertRaises(ValueError):
            self.run_sample_selection()
        self.assertFalse(self.out.exists())
        self.assertEqual(self.beside_the_sample(), [])


# ---------------------------------------------------------------------------
# The path that the collector reads
# ---------------------------------------------------------------------------
class TestTheDefaultsNameTheCollectorsInput(unittest.TestCase):
    """The step exists to write the file that the collector reads, and no other file."""

    def test_the_default_output_is_the_file_the_collector_reads(self):
        """Two modules spell this path, and the test compares the two spellings rather than
        trusting either. A step that wrote beside the collector's input would exit 0, and the
        collector would then refuse a run on a clean checkout with the step's file present."""
        collector_input = (_collector.STAGE_DIR / _collector.DEFAULT_REPO_CSV).resolve()
        self.assertEqual(_mod.DEFAULT_OUTPUT.resolve(), collector_input)

    def test_both_defaults_are_in_the_classification_stages_data_directory(self):
        """The runner starts steps from the collection stage's directory, but an operator can
        run the step from anywhere. A relative default would then name a file in the shell's
        working directory."""
        for default in (_mod.DEFAULT_ANNOTATED, _mod.DEFAULT_OUTPUT):
            with self.subTest(default=str(default)):
                self.assertTrue(default.is_absolute())
                self.assertEqual(default.parent, _mod.CLASSIFICATION_DIR)

    def test_the_two_files_are_not_the_same_file(self):
        """The filter reads one file and replaces the other. An input and an output at one path
        would rewrite the classification stage's output."""
        self.assertNotEqual(_mod.DEFAULT_ANNOTATED, _mod.DEFAULT_OUTPUT)


if __name__ == "__main__":
    unittest.main()
