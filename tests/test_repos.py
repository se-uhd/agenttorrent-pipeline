"""
Tests for agentcontext.repos: the directory name, what counts as a repository, and the timestamps.

Strategy:
  - The conversions are pure string functions and are tested directly.
  - split() reads a directory, and each test therefore builds one in a temp directory.
  - What matters is the distinction between a repository directory and "a directory":
    a tree such as data/validated/ can contain a stray input.csv, a .DS_Store or a staging
    directory from an interrupted run.
  - Each step writes its list at the root of the tree that it lists, and the list is
    therefore the one entry other than a repository that is not an accident. A caller names
    the list in `expected`, and the tests below check both sides: the named list is not a
    stray, and every other entry still is.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_repos -v
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path

from agentcontext import repos


class TestNameConversion(unittest.TestCase):
    def test_a_repo_name_becomes_a_directory_name(self):
        self.assertEqual(repos.repo_name_to_dir("owner/repo"), "owner§repo")

    def test_and_back_again(self):
        self.assertEqual(repos.dir_name_to_repo("owner§repo"), "owner/repo")

    def test_the_round_trip_is_exact_for_every_character_github_allows(self):
        for name in ("a.b-c_d/e.f-g_h", "0/1", "A-Z/a_z", "x.y.z/w-1.2"):
            self.assertEqual(
                repos.dir_name_to_repo(repos.repo_name_to_dir(name)), name)

    def test_a_directory_name_is_recognized(self):
        self.assertTrue(repos.is_repo_dir_name("owner§repo"))

    def test_a_stray_file_is_not(self):
        for name in ("input.csv", ".DS_Store", "repos_dataset_staging", "output"):
            self.assertFalse(repos.is_repo_dir_name(name), name)


class TestUrlPath(unittest.TestCase):
    """A branch or a path as it goes into a GitHub link."""

    def test_what_is_not_valid_in_a_link_is_encoded_and_the_rest_kept(self):
        from agentcontext.repos import url_path
        self.assertEqual(url_path(" 🎭 a b.md"), "%20%F0%9F%8E%AD%20a%20b.md")
        self.assertEqual(url_path("fix#1/a?b%c"), "fix%231/a%3Fb%25c")
        self.assertEqual(url_path("packages/@scope/(app)/c++/*.md"),
                         "packages/@scope/(app)/c++/*.md")


class TestSplit(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dir(self, name):
        (self.tmp / name).mkdir()

    def _file(self, name):
        (self.tmp / name).write_text("x\n", encoding="utf-8")

    def test_repositories_are_returned_sorted(self):
        for name in ("z§b", "a§b", "m§b"):
            self._dir(name)
        self.assertEqual(repos.split(self.tmp), (["a§b", "m§b", "z§b"], []))

    def test_a_stray_file_is_separated_out(self):
        self._dir("o§a")
        self._file("input.csv")
        self.assertEqual(repos.split(self.tmp), (["o§a"], ["input.csv"]))

    def test_a_stray_directory_is_separated_out(self):
        self._dir("o§a")
        self._dir("repos_dataset_staging")
        self.assertEqual(repos.split(self.tmp), (["o§a"], ["repos_dataset_staging"]))

    def test_a_file_with_the_separator_in_its_name_is_not_a_repository(self):
        self._file("o§a")
        self.assertEqual(repos.split(self.tmp), ([], ["o§a"]))

    def test_a_symlink_to_a_directory_is_not_a_repository(self):
        self._dir("o§a")
        (self.tmp / "o§link").symlink_to(self.tmp / "o§a")
        self.assertEqual(repos.split(self.tmp), (["o§a"], ["o§link"]))

    def test_an_empty_directory_yields_two_empty_lists(self):
        self.assertEqual(repos.split(self.tmp), ([], []))


class TestExpectedNames(unittest.TestCase):
    """The list that each step writes at the root of the tree that it lists.

    Without `expected`, every run of the collected file cleanup, detection and the collected
    files archive would log a warning about entries that are not repository directories, naming
    the step's own list. A warning that appears on every run goes unread, and this warning
    exists to report a real accident.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _dir(self, name):
        (self.tmp / name).mkdir()

    def _file(self, name):
        (self.tmp / name).write_text("x\n", encoding="utf-8")

    def test_an_expected_list_is_in_neither_list(self):
        self._dir("o§a")
        self._file("repos.csv")
        self.assertEqual(repos.split(self.tmp, expected=("repos.csv",)), (["o§a"], []))

    def test_a_genuine_stray_is_still_reported(self):
        """The accidents that the split reports: an archive extracted in place leaves an
        input.csv, macOS leaves a .DS_Store, and an interrupted archive run leaves its staging
        tree."""
        self._dir("o§a")
        self._file("repos.csv")
        self._file("input.csv")
        self._file(".DS_Store")
        self._dir("repos_dataset_staging")
        self.assertEqual(
            repos.split(self.tmp, expected=("repos.csv",)),
            (["o§a"], [".DS_Store", "input.csv", "repos_dataset_staging"]))

    def test_every_name_a_step_writes_can_be_expected_at_once(self):
        """The collected tree contains two lists, `repos.csv` and `clone_failed.csv`."""
        self._dir("o§a")
        self._file("repos.csv")
        self._file("clone_failed.csv")
        self.assertEqual(
            repos.split(self.tmp, expected=("repos.csv", "clone_failed.csv")),
            (["o§a"], []))

    def test_expecting_a_name_that_is_absent_is_not_an_error(self):
        """A cloning run without a clone failure writes the header and nothing else, and a
        caller does not need to know whether the file exists."""
        self._dir("o§a")
        self.assertEqual(
            repos.split(self.tmp, expected=("repos.csv", "clone_failed.csv")),
            (["o§a"], []))

    def test_expecting_nothing_is_the_default_and_reports_the_list(self):
        """The parameter has to be passed to have an effect. A caller that forgets it gets
        the warning rather than a silent pass."""
        self._dir("o§a")
        self._file("repos.csv")
        self.assertEqual(repos.split(self.tmp), (["o§a"], ["repos.csv"]))

    def test_an_expected_name_that_is_a_repository_directory_is_still_dropped(self):
        """`expected` names entries, not files. A repository directory with an expected name
        should not occur. If one does, the caller's declaration decides rather than the
        separator in the name."""
        self._dir("o§a")
        self._dir("o§list")
        self.assertEqual(repos.split(self.tmp, expected=("o§list",)), (["o§a"], []))


class TestTimestamps(unittest.TestCase):
    """The conversion to UTC that every step applies to git's `%cI`."""

    def test_an_offset_of_24_hours_or_more_is_subtracted_in_either_direction(self):
        """git prints the local time of a commit with an offset of +2500 as the epoch seconds
        plus 25 hours, and of -2530 as the epoch seconds minus 25 hours and 30 minutes."""
        for value in ("2023-11-15T23:13:20+25:00", "2023-11-13T20:43:20-25:30",
                      "2023-11-14T23:13:20+0100", "2023-11-14T22:13:20Z"):
            with self.subTest(value=value):
                self.assertEqual(repos.parse_and_convert_to_utc(value), "2023-11-14T22:13:20Z")

    def test_a_value_that_is_not_a_timestamp_is_kept_with_a_warning(self):
        """The file history cache keeps git's text, so that its row records a history that
        was read."""
        with mock.patch.object(repos.logs, "warning") as warning:
            self.assertEqual(repos.parse_and_convert_to_utc("2023-13-40T25:00:00+25:00"),
                             "2023-13-40T25:00:00+25:00")
        self.assertIn("2023-13-40T25:00:00+25:00", warning.call_args.args[1])

    def test_an_instant_outside_the_years_of_datetime_is_kept_with_a_warning(self):
        """`datetime` holds the years 1 to 9999. An OverflowError that no caller caught would
        stop the detection step, the file history step and `snapshot validation` with a
        traceback rather than with the handling of a value that does not convert. git
        prints the first value for a commit of the year 10000 in the offset -0100, and the same
        commit in the offset +0100 with the year 10000, the last value, which does not
        parse."""
        for value in ("9999-12-31T23:30:00-01:00", "9999-12-31T00:00:00-25:00",
                      "0001-01-01T00:30:00+01:00", "0001-01-01 00:30:00+01:00",
                      "10000-01-01T00:30:00+01:00"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    repos.utc_instant(value)
                with mock.patch.object(repos.logs, "warning") as warning:
                    self.assertEqual(repos.parse_and_convert_to_utc(value), value)
                warning.assert_called_once()

    def test_a_published_value_that_does_not_parse_is_left_blank_and_returned(self):
        """A step that publishes a date leaves blank a value that does not parse, rather than
        stopping at it, and names the value so that its warning can name the repository. A
        row without the column does not gain it."""
        rows = [{"repo_name": "o/converted", "head_commit_date": "2023-11-14T23:13:20+01:00"},
                {"repo_name": "o/plus", "head_commit_date": "10000-01-01T00:30:00+01:00"},
                {"repo_name": "o/minus", "head_commit_date": " 9999-12-31T23:30:00-01:00"},
                {"repo_name": "o/damaged", "head_commit_date": "the day before"},
                {"repo_name": "o/blank", "head_commit_date": ""},
                {"repo_name": "o/absent"}]
        blanked = repos.to_utc_or_blank(rows, ("head_commit_date",))
        self.assertEqual([row.get("head_commit_date") for row in rows],
                         ["2023-11-14T22:13:20Z", "", "", "", "", None])
        self.assertEqual([(row["repo_name"], column, value) for row, column, value in blanked],
                         [("o/plus", "head_commit_date", "10000-01-01T00:30:00+01:00"),
                          ("o/minus", "head_commit_date", "9999-12-31T23:30:00-01:00"),
                          ("o/damaged", "head_commit_date", "the day before")])
        message = repos.blanked_dates_message("repos.csv", blanked)
        self.assertTrue(message.startswith("repos.csv had 3 head_commit_date value(s) that are "
                                           "not ISO 8601 timestamps"),
                        message)
        self.assertTrue(message.endswith("in 3 repositories (o/damaged, o/minus, o/plus)."),
                        message)

    def test_the_first_and_the_last_instant_of_datetime_convert_in_any_offset(self):
        for value, instant in (("9999-12-31T22:59:59-01:00", datetime.max),
                               ("9999-12-30T22:59:59-25:00", datetime.max),
                               ("0001-01-01T01:00:00+01:00", datetime.min),
                               ("0001-01-01 01:00:00+01:00", datetime.min)):
            with self.subTest(value=value):
                self.assertEqual(repos.utc_instant(value),
                                 instant.replace(microsecond=0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
