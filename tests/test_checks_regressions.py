"""Tests for agentcontext.checks.regressions: the manifest of the tracked outputs and its
`check`, and `compare` over two stage directories.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_checks_regressions -v
"""

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentcontext import STAGE_DIR, heuristics
from agentcontext.checks import regressions as _mod
from agentcontext.checks.regressions import build, compare, describe

REPOS_COLUMNS = ["repo_name", "scanned_at", "branch", "claude", "skills"]


class _CsvCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def write(self, name, columns, rows):
        path = self.dir / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(columns)
            w.writerows(rows)
        return path


class TestDescribe(_CsvCase):
    def test_it_counts_rows_and_repositories(self):
        path = self.write("repos.csv", ["repo_name", "skills"],
                          [["a/one", "TRUE"], ["a/two", "FALSE"], ["a/one", "TRUE"]])
        entry = describe(path)
        self.assertEqual(entry["rows"], 3)
        self.assertEqual(entry["repositories"], 2)

    def test_it_counts_true_in_boolean_columns(self):
        path = self.write("repos.csv", ["repo_name", "skills", "rules"],
                          [["a/one", "TRUE", "FALSE"], ["a/two", "TRUE", "TRUE"]])
        self.assertEqual(describe(path)["true_counts"], {"skills": 2, "rules": 1})

    def test_a_column_that_is_not_boolean_is_not_counted(self):
        path = self.write("repos.csv", ["repo_name", "branch"],
                          [["a/one", "main"], ["a/two", "trunk"]])
        self.assertEqual(describe(path)["true_counts"], {})

    def test_an_empty_file_has_no_boolean_columns(self):
        # Every column of a zero-row file is vacuously boolean, and counting such columns would
        # add counts for columns that contain text.
        path = self.write("excluded.csv", ["repo_name", "reason"], [])
        entry = describe(path)
        self.assertEqual(entry["rows"], 0)
        self.assertEqual(entry["true_counts"], {})

    def test_the_digest_changes_with_the_bytes(self):
        first = describe(self.write("a.csv", ["repo_name"], [["a/one"]]))["sha256"]
        second = describe(self.write("a.csv", ["repo_name"], [["a/two"]]))["sha256"]
        self.assertNotEqual(first, second)


class TestBuild(_CsvCase):
    def test_it_keys_by_mechanism_not_by_filename(self):
        self.write("context-files.csv", ["repo_name"], [["a/one"]])
        self.assertEqual(list(build(self.dir)), ["context_files"])

    def test_a_missing_file_is_simply_absent(self):
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        self.assertEqual(list(build(self.dir)), ["rules"])


class TestCompare(unittest.TestCase):
    @staticmethod
    def entry(**overrides):
        base = {"file": "rules.csv", "sha256": "aa", "rows": 10, "columns": ["repo_name"],
                "true_counts": {}, "repositories": 5}
        base.update(overrides)
        return base

    def test_an_identical_manifest_reports_nothing(self):
        m = {"rules": self.entry()}
        self.assertEqual(compare(m, m), [])

    def test_a_rename_alone_compares_equal(self):
        # The manifest is keyed by mechanism for this case. A file renamed with identical
        # contents has the same digest and gives no finding.
        was = {"rules": self.entry(file="rules.csv")}
        now = {"rules": self.entry(file="rules_extended.csv")}
        self.assertEqual(compare(was, now), [])

    def test_a_rename_with_changed_contents_produces_both_findings(self):
        was = {"rules": self.entry(file="rules.csv")}
        now = {"rules": self.entry(file="new.csv", sha256="bb", rows=11)}
        findings = compare(was, now)
        self.assertIn("rules was renamed from rules.csv to new.csv", findings)
        self.assertIn("The rows of rules went from 10 to 11 (+1)", findings)

    def test_a_row_count_change_is_signed(self):
        was = {"rules": self.entry(rows=100)}
        now = {"rules": self.entry(rows=90, sha256="bb")}
        self.assertIn("The rows of rules went from 100 to 90 (-10)", compare(was, now))

    def test_a_reclassification_shows_on_both_mechanisms(self):
        was = {"rules": self.entry(rows=100), "other": self.entry(file="other.csv", rows=50)}
        now = {"rules": self.entry(rows=120, sha256="bb"),
               "other": self.entry(file="other.csv", rows=30, sha256="cc")}
        findings = compare(was, now)
        self.assertIn("The rows of rules went from 100 to 120 (+20)", findings)
        self.assertIn("The rows of other went from 50 to 30 (-20)", findings)

    def test_an_added_column_is_named(self):
        was = {"repos": self.entry(file="repos.csv", columns=["repo_name"])}
        now = {"repos": self.entry(file="repos.csv", sha256="bb",
                                   columns=["repo_name", "head_sha"])}
        self.assertIn("repos gained the column(s) head_sha", compare(was, now))

    def test_a_removed_column_is_named(self):
        was = {"repos": self.entry(file="repos.csv", columns=["repo_name", "is_empty"])}
        now = {"repos": self.entry(file="repos.csv", sha256="bb", columns=["repo_name"])}
        self.assertIn("repos lost the column(s) is_empty", compare(was, now))

    def test_a_true_count_change_is_reported_per_column(self):
        was = {"repos": self.entry(file="repos.csv", true_counts={"skills": 547})}
        now = {"repos": self.entry(file="repos.csv", sha256="bb",
                                   true_counts={"skills": 716})}
        self.assertIn("The count of TRUE in repos.skills went from 547 to 716 (+169)",
                      compare(was, now))

    def test_a_repository_count_change_is_signed(self):
        """When rows of one repository are attributed to another, the row count can stay the
        same while the repository count changes. Only this finding shows the change."""
        was = {"rules": self.entry(repositories=5)}
        now = {"rules": self.entry(repositories=7, sha256="bb")}
        self.assertEqual(compare(was, now), ["The repositories of rules went from 5 to 7 (+2)"])

    def test_a_column_that_stops_being_boolean_is_named_rather_than_skipped(self):
        """A column with a value other than TRUE, FALSE or blank drops out of the counts,
        and skipping it silently would hide a change of its count."""
        was = {"repos": self.entry(file="repos.csv", true_counts={"skills": 547})}
        now = {"repos": self.entry(file="repos.csv", sha256="bb", true_counts={})}
        self.assertEqual(compare(was, now),
                         ["repos.skills is no longer comparable as a boolean column"])
        self.assertEqual(compare(now, was),
                         ["repos.skills is newly comparable as a boolean column"])

    def test_a_new_file_is_reported(self):
        self.assertEqual(compare({}, {"rules": self.entry()}),
                         ["rules is new, with 10 rows in rules.csv"])

    def test_a_vanished_file_is_reported(self):
        self.assertEqual(compare({"rules": self.entry()}, {}),
                         ["rules is gone (it had 10 rows in rules.csv)"])

    def test_changed_bytes_with_no_changed_count_still_get_a_line(self):
        # A reordering or a changed value inside a row changes no count. Passing over such a
        # change silently would hide a reclassification.
        was = {"rules": self.entry(sha256="aa")}
        now = {"rules": self.entry(sha256="bb")}
        self.assertEqual(compare(was, now),
                         ["The contents of rules changed with no change to any count"])


class TestExitCodes(_CsvCase):
    def _main(self, *extra):
        argv = sys.argv
        sys.argv = ["prog", "--output-dir", str(self.dir),
                    "--baseline", str(self.dir / "baseline.json"), *extra]
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                return _mod.main_check()
        finally:
            sys.argv = argv

    def _main_without_baseline(self, *extra):
        argv = sys.argv
        sys.argv = ["prog", "--output-dir", str(self.dir), *extra]
        try:
            import contextlib, io
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                return _mod.main_check()
        finally:
            sys.argv = argv

    def test_the_help_names_the_directory_that_contains_the_default_manifest(self):
        """The command reads the `baseline.json` in `--output-dir`, and the help names that
        directory rather than its parent."""
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit):
            _mod.main_check(["--help"])
        text = " ".join(out.getvalue().split())
        self.assertIn("default: baseline.json in --output-dir", text)

    def test_the_default_manifest_is_the_one_in_the_output_directory(self):
        """A run over a scratch tree compares with the manifest in its own `--output-dir`, and
        `--write` leaves the repository's tracked baseline unchanged. The test points the module
        default at another path, so that a regression of this behavior writes there rather than
        over the tracked manifest."""
        import unittest.mock as _mock
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        elsewhere = self.dir / "must_not_be_written.json"
        with _mock.patch.object(_mod, "DEFAULT_BASELINE", elsewhere):
            self.assertEqual(self._main_without_baseline("--write"), 0)
            self.assertFalse(elsewhere.exists(),
                             "the module default was written rather than --output-dir's")
        self.assertTrue((self.dir / "baseline.json").is_file(),
                        "the manifest was not written to --output-dir")
        with _mock.patch.object(_mod, "DEFAULT_BASELINE", elsewhere):
            self.assertEqual(self._main_without_baseline(), 0)

    def test_a_baseline_that_cannot_be_written_leaves_the_previous_baseline_and_is_one(self):
        """A full disk would truncate a baseline written in place."""
        import errno
        import unittest.mock as _mock
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        self.assertEqual(self._main("--write"), 0)
        before = (self.dir / "baseline.json").read_bytes()
        self.write("rules.csv", ["repo_name"], [["a/one"], ["a/two"]])
        full = OSError(errno.ENOSPC, "No space left on device")
        with _mock.patch("agentcontext.caches.os.replace", side_effect=full):
            self.assertEqual(self._main("--write"), 1)
        self.assertEqual((self.dir / "baseline.json").read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if "baseline" in p.name),
                         ["baseline.json"])

    def test_writing_then_checking_is_zero(self):
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        self.assertEqual(self._main("--write"), 0)
        self.assertEqual(self._main(), 0)

    def test_a_difference_is_two(self):
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        self._main("--write")
        self.write("rules.csv", ["repo_name"], [["a/one"], ["a/two"]])
        self.assertEqual(self._main(), 2)

    def test_a_missing_baseline_is_one_not_zero(self):
        # A check that cannot run must not read as a check that passed.
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        self.assertEqual(self._main(), 1)

    def test_an_unreadable_baseline_is_one(self):
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        (self.dir / "baseline.json").write_text("{ truncated")
        self.assertEqual(self._main(), 1)

    def test_an_output_directory_with_no_tracked_csv_is_one(self):
        self.assertEqual(self._main("--write"), 1)

    def test_the_written_baseline_is_sorted_json(self):
        """The key order of the tracked manifest determines its diff. At both levels, the
        insertion order here differs from the sorted order. Comparing the text therefore
        detects a writer that keeps the insertion order, and parsing the text and checking the
        last newline would not."""
        self.write("rules.csv", ["repo_name"], [["a/one"]])
        self.write("repos.csv", ["repo_name", "claude"], [["a/one", "TRUE"]])
        self._main("--write")
        text = (self.dir / "baseline.json").read_text(encoding="utf-8")
        self.assertEqual(text, json.dumps(json.loads(text), indent=2, sort_keys=True) + "\n")
        self.assertEqual(list(json.loads(text)), ["repos", "rules"])


class TestBooleanColumnsToleratesBlanks(unittest.TestCase):
    """Published CSVs have boolean columns with blank cells. A single blank that dropped the
    column out of true_counts would leave compare() nothing to compare, and compare() would
    find the counts unchanged."""

    def test_a_column_with_a_blank_is_still_boolean(self):
        rows = [{"flag": "TRUE"}, {"flag": ""}, {"flag": "FALSE"}]
        self.assertEqual(_mod.boolean_columns(rows, ["flag"]), ["flag"])

    def test_an_entirely_blank_column_is_not_boolean(self):
        rows = [{"flag": ""}, {"flag": ""}]
        self.assertEqual(_mod.boolean_columns(rows, ["flag"]), [])

    def test_a_column_of_free_text_is_not_boolean(self):
        rows = [{"name": "a"}, {"name": "b"}]
        self.assertEqual(_mod.boolean_columns(rows, ["name"]), [])

    def test_a_blank_does_not_hide_a_changed_true_count(self):
        """describe() counts only a column that boolean_columns returned. A column dropped for
        containing one blank would leave compare() with nothing to compare."""
        import csv as _csv
        tmp = Path(tempfile.mkdtemp())
        try:
            counts = []
            for third in ("TRUE", "FALSE"):
                path = tmp / f"{third}.csv"
                with path.open("w", newline="", encoding="utf-8") as f:
                    w = _csv.DictWriter(f, fieldnames=["repo_name", "flag"])
                    w.writeheader()
                    w.writerow({"repo_name": "o/a", "flag": "TRUE"})
                    w.writerow({"repo_name": "o/b", "flag": ""})
                    w.writerow({"repo_name": "o/c", "flag": third})
                counts.append(_mod.describe(path)["true_counts"].get("flag"))
            self.assertEqual(counts, [2, 1])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_published_csvs_are_read_and_their_flags_counted(self):
        """The test reads the published files by name. `first_commit_ai_created` in
        `context-files.csv`, blank where the commit history was not read, is counted as a
        boolean. The tests above check the tolerance of blanks with rows of their own."""
        output = STAGE_DIR / "output"
        if not output.is_dir():
            self.skipTest("output/ has not been generated")
        read = {}
        for filename in sorted(_mod.TRACKED_FILES.values()):
            path = output / filename
            if path.is_file():
                read[filename] = _mod.describe(path)
        self.assertIn("context-files.csv", read)
        self.assertGreater(read["context-files.csv"]["rows"], 0)
        self.assertIn("first_commit_ai_created", read["context-files.csv"]["true_counts"])
        self.assertGreater(read["context-files.csv"]["true_counts"]["first_commit_ai_created"], 0)

    def test_every_tracked_file_is_in_the_output_directory(self):
        """`build()` skips a tracked file that is absent. An entry that names a file that the
        pipeline no longer writes therefore has no effect at run time, and only this test finds
        the entry."""
        output = STAGE_DIR / "output"
        if not (output / "repos.csv").is_file():
            self.skipTest("output/ has not been generated")
        absent = sorted(name for name in _mod.TRACKED_FILES.values()
                        if not (output / name).is_file())
        self.assertEqual(absent, [])


class _TwoRoots(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.first = Path(self.tmp.name) / "first"
        self.second = Path(self.tmp.name) / "second"
        for root in (self.first, self.second):
            (root / "output").mkdir(parents=True)
            self.write(root, "output/repos.csv", REPOS_COLUMNS, [
                ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "TRUE"],
                ["o/b", "2026-09-12T10:00:00Z", "main", "FALSE", "TRUE"]])
            self.write(root, "file_history.csv",
                       ["repo_name", "head_sha", "mechanism", "artifact", "created_at",
                        "#commits", "first_commit_sha", "last_commit_sha"],
                       [["o/a", "a" * 40, "", "CLAUDE.md", "2026-01-01T00:00:00Z", "3",
                         "b" * 40, "c" * 40]])

    def write(self, root, rel, columns, rows):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f, lineterminator="\n")
            writer.writerow(columns)
            writer.writerows(rows)
        return path

    def run_cli(self, *args):
        result = subprocess.run([sys.executable, "-m", "agentcontext.checks.regressions", "compare", str(self.first),
                                 str(self.second), *args], capture_output=True, text=True)
        return result.returncode, result.stdout + result.stderr


class TestFindings(_TwoRoots):
    def test_identical_roots_have_no_difference(self):
        self.assertEqual(_mod.compare_roots(self.first, self.second, set()), [])

    def test_a_moved_value_names_the_column_and_the_row(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T10:00:00Z", "develop", "FALSE", "TRUE"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("output/repos.csv.branch changed on 1 row(s) (o/b)", findings)

    def test_a_row_that_entered_and_one_that_left_are_named(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "TRUE"],
            ["o/c", "2026-09-12T10:00:00Z", "main", "TRUE", "FALSE"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("output/repos.csv gained 1 row(s) (o/c)", findings)
        self.assertIn("output/repos.csv lost 1 row(s) (o/b)", findings)
        # The manifest's findings come first, with the changed count.
        self.assertTrue(any(line.startswith("The count of TRUE in repos.skills went from 2 to 1")
                            for line in findings), findings)

    def test_the_unmatched_attributions_of_the_release_are_compared_on_their_token(self):
        """The release builds the file by the rule of `unmatched reduce` and adds the credits of
        the commit logs, so a comparison of its source does not show a change in it."""
        rel = "release/unmatched-attributions.csv"
        columns = ["kind", "token", "commits", "repos"]
        self.write(self.first, rel, columns, [["trailer", "co-authored-by: x[bot]", "1", "1"]])
        self.write(self.second, rel, columns, [["trailer", "co-authored-by: y[bot]", "1", "1"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertEqual(findings, [f"{rel} gained 1 row(s) (trailer co-authored-by: y[bot])",
                                    f"{rel} lost 1 row(s) (trailer co-authored-by: x[bot])"])

    def test_a_planning_document_is_named_by_its_repository_and_its_path(self):
        """Two planning documents of one repository are two rows. A change names the planning
        document that changed rather than the repository."""
        columns = ["repo_name", "planning_document", "framework", "line_count"]
        rows = [["o/a", "specs/001-albums/spec.md", "spec-kit", "40"],
                ["o/a", "specs/001-albums/plan.md", "spec-kit", "25"]]
        self.write(self.first, "output/planning-documents.csv", columns, rows)
        self.write(self.second, "output/planning-documents.csv", columns,
                   [rows[0], ["o/a", "specs/001-albums/plan.md", "spec-kit", "31"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("output/planning-documents.csv.line_count changed on 1 row(s) "
                      "(o/a specs/001-albums/plan.md)", findings)

    def test_an_ignored_column_is_left_out(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T11:00:00Z", "main", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T11:00:00Z", "main", "FALSE", "TRUE"]])
        with_it = _mod.compare_roots(self.first, self.second, set())
        self.assertTrue(any(".scanned_at changed on 2 row(s)" in line for line in with_it))
        self.assertIn("The contents of repos changed with no change to any count", with_it)
        # Two runs at different times compare equal, because an ignored column that changed is
        # not a difference. Neither the row comparison nor the manifest's finding for the
        # file's bytes counts the change, and a note names the column.
        notes = []
        without = _mod.compare_roots(self.first, self.second, {"scanned_at"}, notes)
        self.assertEqual(without, [])
        self.assertEqual(notes, ["Only the ignored column(s) scanned_at changed in "
                                 "output/repos.csv, which is not a difference"])

    def test_an_ignored_column_hides_no_other_change(self):
        """A changed row, rows in another order and other line endings stay differences when
        an ignored column changed with them."""
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T11:00:00Z", "develop", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T11:00:00Z", "main", "FALSE", "TRUE"]])
        findings = _mod.compare_roots(self.first, self.second, {"scanned_at"})
        self.assertIn("output/repos.csv.branch changed on 1 row(s) (o/a)", findings)
        self.assertIn("The contents of repos changed with no change to any count", findings)
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/b", "2026-09-12T11:00:00Z", "main", "FALSE", "TRUE"],
            ["o/a", "2026-09-12T11:00:00Z", "main", "TRUE", "TRUE"]])
        self.assertIn("The order of the rows of output/repos.csv changed",
                      _mod.compare_roots(self.first, self.second, {"scanned_at"}))
        path = self.second / "output/repos.csv"
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T11:00:00Z", "main", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T11:00:00Z", "main", "FALSE", "TRUE"]])
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        lf = self.first / "output/repos.csv"
        lf.write_bytes(lf.read_bytes().replace(b"\r\n", b"\n"))
        self.assertIn("The line endings of output/repos.csv changed",
                      _mod.compare_roots(self.first, self.second, {"scanned_at"}))

    def test_a_file_only_one_run_wrote_is_reported(self):
        self.write(self.second, "submodules.csv", ["repo_name", "path", "submodule_repo"],
                   [["o/a", "vendor/x", "x/x"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("submodules.csv is only in the second run", findings)

    def test_the_cache_is_compared_on_its_four_column_key(self):
        self.write(self.second, "file_history.csv",
                   ["repo_name", "head_sha", "mechanism", "artifact", "created_at",
                    "#commits", "first_commit_sha", "last_commit_sha"],
                   [["o/a", "a" * 40, "", "CLAUDE.md", "2026-01-01T00:00:00Z", "4",
                     "b" * 40, "d" * 40]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn(f"file_history.csv.#commits changed on 1 row(s) (o/a {'a' * 40}  "
                      f"CLAUDE.md)", findings)
        self.assertTrue(any(line.startswith("file_history.csv.last_commit_sha changed")
                            for line in findings))

    def test_every_mechanism_csv_is_keyed_on_its_artifact_column(self):
        for mechanism, (filename, artifact) in heuristics.MECHANISM_CSVS.items():
            with self.subTest(CSV=filename):
                self.assertEqual(_mod.KEYED_FILES[f"output/{filename}"],
                                 ("repo_name", artifact))

    def test_a_key_on_a_second_row_is_named_as_a_difference(self):
        """A comparison that mapped the rows by key and kept the first row of each key would
        find a cache equal row by row when the cache has a second row under a key with another
        `#commits`. It would then give the order or the line endings as the change."""
        rows = [["o/a", "a" * 40, "", "CLAUDE.md", "2026-01-01T00:00:00Z", "3",
                 "b" * 40, "c" * 40],
                ["o/a", "a" * 40, "", "CLAUDE.md", "2026-01-01T00:00:00Z", "4",
                 "b" * 40, "c" * 40]]
        self.write(self.second, "file_history.csv",
                   ["repo_name", "head_sha", "mechanism", "artifact", "created_at",
                    "#commits", "first_commit_sha", "last_commit_sha"], rows)
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertEqual(findings, [
            f"1 key(s) of file_history.csv are on more than one row in the second run "
            f"(o/a {'a' * 40}  CLAUDE.md)"])
        # The finding names the run that has the repeated key, whichever run it is.
        findings = _mod.compare_roots(self.second, self.first, set())
        self.assertEqual(findings, [
            f"1 key(s) of file_history.csv are on more than one row in the first run "
            f"(o/a {'a' * 40}  CLAUDE.md)"])

    def test_columns_in_another_order_are_named(self):
        """No column is added or removed. Without this finding, a reordering of the columns
        would show as a change of the row order or the line endings."""
        reordered = ["repo_name", "branch", "scanned_at", "claude", "skills"]
        self.write(self.second, "output/repos.csv", reordered, [
            ["o/a", "main", "2026-09-12T10:00:00Z", "TRUE", "TRUE"],
            ["o/b", "main", "2026-09-12T10:00:00Z", "FALSE", "TRUE"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("The columns of output/repos.csv were reordered", findings)
        self.assertFalse(any("order or the line endings" in line for line in findings),
                         findings)

    def test_a_reordered_file_with_equal_rows_still_gets_a_line(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/b", "2026-09-12T10:00:00Z", "main", "FALSE", "TRUE"],
            ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "TRUE"]])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("The order of the rows of output/repos.csv changed", findings)

    def _scanned_later(self) -> Path:
        """Write the second run's repos.csv with only `scanned_at` changed, and return its path."""
        return self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T11:00:00Z", "main", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T11:00:00Z", "main", "FALSE", "TRUE"]])

    def test_an_ignored_column_hides_no_byte_that_moved_beside_it(self):
        """A field that no column names, a quote and a missing final newline change bytes that
        the rows do not show. Each change stays a difference when `scanned_at` is ignored and
        changed."""
        path = self._scanned_later()
        text = path.read_text(encoding="utf-8")
        for case, changed, said in (
                ("a field that no column names", text.replace("TRUE\no/b", "TRUE,extra\no/b"),
                 "A row of output/repos.csv contains a field that no column names"),
                ("a quote", text.replace("main", '"main"', 1),
                 "The bytes of output/repos.csv in the second run differ from what a CSV "
                 "writer writes for its rows"),
                ("no final newline", text.rstrip("\n"),
                 "The bytes of output/repos.csv in the second run differ from what a CSV "
                 "writer writes for its rows")):
            with self.subTest(case=case):
                path.write_text(changed, encoding="utf-8")
                notes = []
                findings = _mod.compare_roots(self.first, self.second, {"scanned_at"}, notes)
                self.assertTrue(any(line.startswith(said) for line in findings), findings)
                self.assertEqual(notes, [])

    def test_a_field_that_no_column_names_is_named_without_an_ignored_column(self):
        path = self.first / "output/repos.csv"
        text = path.read_text(encoding="utf-8")
        self.second.joinpath("output/repos.csv").write_text(
            text.replace("TRUE\no/b", "TRUE,extra\no/b"), encoding="utf-8")
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn("A row of output/repos.csv contains a field that no column names", findings)

    def test_an_ignored_column_is_left_out_of_the_counts(self):
        """With `--ignore skills`, the note that the column changed replaces
        `The count of TRUE in repos.skills went from 2 to 1 (-1)`."""
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "FALSE"],
            ["o/b", "2026-09-12T10:00:00Z", "main", "FALSE", "TRUE"]])
        notes = []
        self.assertEqual(_mod.compare_roots(self.first, self.second, {"skills"}, notes), [])
        self.assertEqual(notes, ["Only the ignored column(s) skills changed in output/repos.csv, "
                                 "which is not a difference"])
        self.assertTrue(any(line.startswith("The count of TRUE in repos.skills went from 2 to 1")
                            for line in _mod.compare_roots(self.first, self.second, set())))

    def test_each_file_is_compared_on_its_own_key(self):
        """A changed value of a row is a changed column on the row's key, and a new value in a
        column of the key is a removed row and an added row."""
        cases = (
            ("output/excluded.csv", ["repo_name", "reason"],
             [["o/a", "fork"]], [["o/a", "archived"]],
             "output/excluded.csv.reason changed on 1 row(s) (o/a)"),
            ("deletion.csv", ["path", "reason"],
             [["data/validated/o§a/x.md", "empty file"]],
             [["data/validated/o§a/x.md", "vendored"]],
             "deletion.csv gained 1 row(s) (data/validated/o§a/x.md vendored)"),
            ("output/all_ai_commits.csv", ["repo_name", "commit_sha", "ai_tool"],
             [["o/a", "a" * 40, "Claude"], ["o/a", "b" * 40, "Codex"]],
             [["o/a", "a" * 40, "Claude"], ["o/a", "b" * 40, "Copilot"]],
             f"output/all_ai_commits.csv.ai_tool changed on 1 row(s) (o/a {'b' * 40})"))
        for rel, columns, first, second, said in cases:
            with self.subTest(file=rel):
                self.write(self.first, rel, columns, first)
                self.write(self.second, rel, columns, second)
                self.assertIn(said, _mod.compare_roots(self.first, self.second, set()))

    def test_more_than_five_keys_are_counted_rather_than_listed(self):
        rows = [[f"o/{i}", "2026-09-12T10:00:00Z", "main", "TRUE", "TRUE"] for i in range(8)]
        self.write(self.first, "output/repos.csv", REPOS_COLUMNS, rows)
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [])
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertTrue(any("lost 8 row(s)" in line and "and 3 more" in line
                            for line in findings), findings)


class TestEveryRecordIsComparedOnItsKey(_TwoRoots):
    """Every file that a run writes and that a person reads a result from, with the columns
    that identify a row, stated here from what each file contains. A key that lacks a column
    treats two rows as one."""

    KEYS = {
        "output/repos.csv": ("repo_name",),
        "output/excluded.csv": ("repo_name",),
        "output/ai_commits-repos.csv": ("repo_name",),
        "output/deletions.csv": ("kind", "repo_name", "subject"),
        "output/unmatched-paths.csv": ("glob", "found_in"),
        "output/unmatched-commits.csv": ("kind", "token"),
        "output/all_ai_commits.csv": ("repo_name", "commit_sha"),
        "output/other_ai_commits.csv": ("repo_name", "commit_sha"),
        "output/all_ai_commits-unmatched.csv": ("kind", "token"),
        "output/all_ai_commits-mentions.csv": ("tool", "pattern"),
        "output/all_ai_commits-validated.csv": ("repo_name", "commit_sha"),
        "output/other_ai_commits-validated.csv": ("repo_name", "commit_sha"),
        "file_history.csv": ("repo_name", "head_sha", "mechanism", "artifact"),
        "ai_authorship.csv": ("repo_name", "head_sha", "artifact"),
        "submodules.csv": ("repo_name", "path"),
        "deletion.csv": ("path", "reason"),
        "data/collected/repos.csv": ("repo_name",),
        "data/collected/clone_failed.csv": ("repo_name",),
        "data/validated/repos.csv": ("repo_name",),
        "release/repos.csv": ("repo_name",),
        "release/commits.csv": ("repo_name", "commit_sha"),
        "release/other_commits.csv": ("repo_name", "commit_sha"),
        "release/context_files.csv": ("repo_name", "context_file"),
        "release/unmatched-attributions.csv": ("kind", "token"),
        # The mechanism CSVs, on the configuration artifact column that the heuristics define.
        **{f"output/{filename}": ("repo_name", column)
           for filename, column in heuristics.MECHANISM_CSVS.values()},
    }

    def test_a_moved_value_is_named_on_the_key_of_its_row(self):
        """One row and, for each column of the key, a row that differs from it in that column
        alone. The value of the first row changes. A key without one of the columns treats two
        of the rows as one, and a file left out has no finding at all."""
        for rel, key in self.KEYS.items():
            columns = [*key, "value"]
            base = {c: "k" for c in key}
            rows = [{**base, "value": "1"}] + [{**base, column: "k2", "value": "1"}
                                               for column in key]
            moved = [{**rows[0], "value": "2"}, *rows[1:]]
            with self.subTest(file=rel):
                self.write(self.first, rel, columns, [[r[c] for c in columns] for r in rows])
                self.write(self.second, rel, columns, [[r[c] for c in columns] for r in moved])
                findings = [f for f in _mod.compare_roots(self.first, self.second, set())
                            if f.startswith(rel)]
                self.assertEqual(findings,
                                 [f"{rel}.value changed on 1 row(s) "
                                  f"({' '.join('k' for _ in key)})"])
                (self.first / rel).unlink()
                (self.second / rel).unlink()

    def test_the_consistency_report_is_compared_by_position(self):
        rel = "output/consistency-report.csv"
        self.write(self.first, rel, ["file", "unique_repos"], [["a.csv", "1"], ["b.csv", "2"]])
        self.write(self.second, rel, ["file", "unique_repos"], [["a.csv", "1"], ["b.csv", "3"]])
        self.assertIn(f"{rel}.unique_repos changed on 1 row(s) (1)",
                      _mod.compare_roots(self.first, self.second, set()))


class TestTheJsonRecords(_TwoRoots):
    """The snapshot, the provenance record, the reclassification report and the sampling
    record, compared leaf by leaf."""

    RECORDS = ("output/snapshot.json", "output/provenance.json",
               "output/commit-reclassification.json", "release/sampling.json")

    def write_json(self, root, rel, document, **dump):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, **dump), encoding="utf-8")

    def test_every_record_is_compared(self):
        for rel in self.RECORDS:
            with self.subTest(file=rel):
                self.write_json(self.first, rel, {"counts": {"current": 90}})
                self.write_json(self.second, rel, {"counts": {"current": 89}})
                self.assertIn(f"1 value(s) of {rel} changed (/counts/current from 90 to 89)",
                              _mod.compare_roots(self.first, self.second, set()))

    def test_a_key_with_a_slash_is_one_step_of_the_pointer(self):
        rel = "output/commit-reclassification.json"
        self.write_json(self.first, rel, {"repositories": {"o/a": {"outcome": "current"}}})
        self.write_json(self.second, rel, {"repositories": {"o/a": {"outcome": "no_log"}},
                                           "complete": False})
        findings = _mod.compare_roots(self.first, self.second, set())
        self.assertIn(f'1 value(s) of {rel} changed (/repositories/o~1a/outcome from "current" '
                      f'to "no_log")', findings)
        self.assertIn(f"{rel} gained 1 key(s) (/complete)", findings)

    def test_an_ignored_key_moves_without_a_difference(self):
        rel = "output/snapshot.json"
        self.write_json(self.first, rel, {"collection": {
            "scanned_at_window": {"first": "2026-10-14", "last": "2026-10-14"},
            "counts": {"sampled": 94}}})
        self.write_json(self.second, rel, {"collection": {
            "scanned_at_window": {"first": "2026-10-15", "last": "2026-10-15"},
            "counts": {"sampled": 94}}})
        notes = []
        self.assertEqual(_mod.compare_roots(self.first, self.second,
                                            {"scanned_at_window"}, notes), [])
        self.assertEqual(notes, [f"Only the ignored key(s) scanned_at_window changed in {rel}, "
                                 f"which is not a difference"])
        self.write_json(self.second, rel, {"collection": {
            "scanned_at_window": {"first": "2026-10-15", "last": "2026-10-15"},
            "counts": {"sampled": 93}}})
        self.assertIn(f"1 value(s) of {rel} changed (/collection/counts/sampled from 94 to 93)",
                      _mod.compare_roots(self.first, self.second, {"scanned_at_window"}))

    def test_a_record_written_in_another_layout_is_named(self):
        rel = "output/provenance.json"
        self.write_json(self.first, rel, {"a": 1, "b": 2}, indent=2)
        self.write_json(self.second, rel, {"b": 2, "a": 1})
        self.assertEqual([f for f in _mod.compare_roots(self.first, self.second, set())
                          if rel in f],
                         [f"The bytes of {rel} differ with every key and value equal"])

    def test_a_record_in_one_run_alone_is_reported(self):
        rel = "release/sampling.json"
        self.write_json(self.second, rel, {"final_count": 1})
        self.assertIn(f"{rel} is only in the second run",
                      _mod.compare_roots(self.first, self.second, set()))

    def test_a_record_that_is_not_json_exits_1(self):
        rel = "output/snapshot.json"
        self.write_json(self.first, rel, {"a": 1})
        (self.second / rel).write_text("{not json", encoding="utf-8")
        code, out = self.run_cli()
        self.assertEqual(code, 1, out)
        self.assertIn("is not readable JSON", out)
        self.assertNotIn("Traceback", out)

    def test_a_root_with_only_a_record_is_not_empty(self):
        rel = "output/snapshot.json"
        for path in ("output/repos.csv", "file_history.csv"):
            (self.second / path).unlink()
            (self.first / path).unlink()
        self.write_json(self.first, rel, {"a": 1})
        self.write_json(self.second, rel, {"a": 1})
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)


class TestTheCommandLine(_TwoRoots):
    def test_no_difference_exits_0(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0, out)
        self.assertIn("0 difference(s)", out)

    def test_a_difference_exits_2_and_writes_the_report(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "FALSE"],
            ["o/b", "2026-09-12T10:00:00Z", "main", "FALSE", "TRUE"]])
        report = Path(self.tmp.name) / "report.txt"
        code, out = self.run_cli("--report", str(report))
        self.assertEqual(code, 2, out)
        self.assertIn("output/repos.csv.skills changed on 1 row(s) (o/a)", report.read_text())

    def test_a_key_on_a_second_row_exits_2(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T10:00:00Z", "main", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T10:00:00Z", "main", "FALSE", "TRUE"],
            ["o/b", "2026-09-12T10:00:00Z", "main", "FALSE", "TRUE"]])
        code, out = self.run_cli()
        self.assertEqual(code, 2, out)
        self.assertIn("1 key(s) of output/repos.csv are on more than one row in the second run "
                      "(o/b)",
                      out)

    def test_a_root_without_any_compared_file_exits_1(self):
        """Two stage directories that compare equal because one of them is empty are not two
        runs that agree."""
        for rel in ("output/repos.csv", "file_history.csv"):
            (self.second / rel).unlink()
        code, out = self.run_cli()
        self.assertEqual(code, 1, out)
        self.assertIn(f"Nothing to compare, because only {self.first} has a file", out)

    def test_ignore_is_repeatable_and_named_in_the_report(self):
        self.write(self.second, "output/repos.csv", REPOS_COLUMNS, [
            ["o/a", "2026-09-12T11:00:00Z", "develop", "TRUE", "TRUE"],
            ["o/b", "2026-09-12T11:00:00Z", "develop", "FALSE", "TRUE"]])
        code, out = self.run_cli("--ignore", "scanned_at", "--ignore", "branch")
        self.assertEqual(code, 0, out)
        self.assertIn("0 difference(s)", out)
        self.assertIn("ignoring scanned_at, branch", out)
        self.assertIn("Only the ignored column(s) branch, scanned_at changed in "
                      "output/repos.csv, which is not a difference", out)
        self.assertNotIn(".branch changed", out)
        code, out = self.run_cli("--ignore", "scanned_at")
        self.assertEqual(code, 2, out)
        self.assertIn(".branch changed on 2 row(s)", out)

    def test_a_missing_root_exits_1(self):
        result = subprocess.run([sys.executable, "-m", "agentcontext.checks.regressions", "compare", str(self.first),
                                 str(Path(self.tmp.name) / "nowhere")],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not a stage directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
