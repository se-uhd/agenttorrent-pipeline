"""
Tests for 3_compare_retrievals.py

Strategy:
  - Two retrievals are written into a temporary cache: an earlier retrieval without GitHub's
    responses, as the imported GHS search result of March is, and a later retrieval with them.
    Each test reads the CSVs and the summary that the script writes.
  - The tests cover these changes between two retrievals:
      - a repository renamed between the two retrievals
      - a repository added because the window moved past its creation
      - a repository removed because its last commit fell before the new window
      - a changed field
      - a changed description, mirror flag or private flag, which GitHub alone has
      - a name that a repository with another GitHub id took
      - a timestamp that GHS wrote on its 12-hour clock on the same day as GitHub's
      - collected repositories with a new HEAD commit, with a changed branch, gone from GitHub
        or never collected
  - `_script_loader.synced_writes` records each call of the real `os.fsync` and `os.replace`,
    and the tests read from these calls that a file is on disk before its rename.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_compare_retrievals -v
"""

import contextlib
import csv
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _script_loader import load_script, synced_writes  # noqa: E402

_mod = load_script("3_compare_retrievals.py")
import ghs_cache  # noqa: E402

OLD, NEW = "2026-03-16T180701Z", "2026-10-05T120000Z"


def _ghs(name, ghs_id, **fields):
    row = {"id": ghs_id, "name": name, "defaultBranch": "main", "lastCommitSHA": "a" * 40,
           "lastCommit": "2026-03-01T04:00:00", "pushedAt": "2026-03-01T04:00:00",
           "createdAt": "2020-01-01T04:00:00", "isArchived": False, "isDisabled": False,
           "isLocked": False, "isFork": False, "license": "MIT License", "mainLanguage": "Go",
           "stargazers": 10, "commits": 100, "contributors": 3, "totalIssues": 1,
           "totalPullRequests": 1, "watchers": 2, "topics": ["b", "a"]}
    row.update(fields)
    return row


def _found(ghs_name, github_id, **fields):
    repository = {"id": github_id, "name": ghs_name, "defaultBranch": "main",
                  "headSha": "a" * 40, "pushedAt": "2026-03-01T16:00:00Z",
                  "createdAt": "2020-01-01T16:00:00Z", "isArchived": False, "isDisabled": False,
                  "isLocked": False, "isFork": False, "license": "MIT License",
                  "primaryLanguage": "Go", "stargazers": 10}
    repository.update(fields)
    return {"ghsName": ghs_name, "status": "found", "repository": repository}


class _CompareCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "ghs_retrievals"
        self.pointer = self.tmp / "ghs_retrieval.json"

    def retrieval(self, rid, rows, responses=None, created_max="2024-09-16",
                  committed_min="2025-09-16"):
        _, sha = ghs_cache.write_rows(self.cache, rid, "ghs", rows)
        values = {"retrieval_id": rid, "outcome": "retrieved", "ghs_file_sha256": sha,
                  "created_max": created_max, "committed_min": committed_min}
        if responses is not None:
            values["github_file_sha256"] = ghs_cache.write_rows(self.cache, rid, "github",
                                                                responses)[1]
        ghs_cache.put_index_row(values, self.cache)

    def run_main(self, collected=None, *extra):
        ghs_cache.write_pointer(NEW, self.pointer)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _mod.main(["--cache-dir", str(self.cache), "--pointer", str(self.pointer),
                              "--collected", str(collected or self.tmp / "absent.csv"),
                              "--changes", str(self.tmp / "c.csv"),
                              "--collection-changes", str(self.tmp / "cc.csv"),
                              "--summary", str(self.tmp / "s.json"), *extra])
        return code, err.getvalue()

    def rows(self, name):
        with open(self.tmp / name, encoding="utf-8", newline="") as f:
            return {row["repository"]: row for row in csv.DictReader(f)}

    def summary(self):
        return json.loads((self.tmp / "s.json").read_text())


class TestTwoRetrievals(_CompareCase):

    def setUp(self):
        super().setUp()
        self.retrieval(OLD, [
            _ghs("same/repo", 1),
            _ghs("old-owner/renamed", 2),
            _ghs("quiet/repo", 3, lastCommit="2025-10-01T09:00:00"),
            _ghs("busy/repo", 4, lastCommit="2026-03-10T09:00:00"),
            _ghs("changed/repo", 5),
        ])
        self.retrieval(NEW, [
            _ghs("same/repo", 1),
            _ghs("old-owner/renamed", 2),
            _ghs("new-owner/Renamed", 20),
            _ghs("young/repo", 21, createdAt="2025-01-10T09:00:00"),
            _ghs("revived/repo", 22),
            _ghs("changed/repo", 5),
        ], responses=[
            _found("same/repo", 101),
            dict(_found("old-owner/renamed", 102, name="new-owner/Renamed"),
                 redirectedTo="new-owner/Renamed"),
            _found("new-owner/Renamed", 102),
            _found("young/repo", 121, createdAt="2025-01-10T21:00:00Z"),
            _found("revived/repo", 122),
            _found("changed/repo", 105, isArchived=True, headSha="b" * 40),
        ], created_max="2025-03-22", committed_min="2026-03-05")

    def test_each_repository_is_added_removed_changed_or_unchanged(self):
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        rows = self.rows("c.csv")
        self.assertEqual(set(rows), {"young/repo", "revived/repo", "quiet/repo", "busy/repo",
                                     "changed/repo", "new-owner/Renamed"})
        self.assertEqual(rows["young/repo"]["reason"], "created_after_previous_window")
        self.assertEqual(rows["revived/repo"]["reason"], "revived_or_newly_matched")
        self.assertEqual(rows["quiet/repo"]["reason"], "inactive_before_window")
        self.assertEqual(rows["busy/repo"]["reason"], "deleted_or_no_longer_matching")
        self.assertEqual(rows["changed/repo"]["fields"], "headSha;isArchived")
        self.assertEqual(json.loads(rows["changed/repo"]["new"]),
                         {"headSha": "b" * 40, "isArchived": True})
        counts = self.summary()["retrievals"]
        self.assertEqual((counts["added"], counts["removed"], counts["changed"],
                          counts["unchanged"]), (2, 2, 2, 1))
        self.assertTrue(self.summary()["window_moved"])
        self.assertIn("(2 added, 2 removed, 2 changed, 1 unchanged). The sampling window moved.\n",
                      err)

    def test_a_rename_is_matched_rather_than_removed_and_added(self):
        """GHS keeps the row of the old name when it crawls the new name, and GitHub resolves
        both rows to one repository. The new retrieval contains both names."""
        self.run_main()
        rows = self.rows("c.csv")
        self.assertEqual((rows["new-owner/Renamed"]["change"], rows["new-owner/Renamed"]["fields"]),
                         ("changed", "name"))
        self.assertNotIn("old-owner/renamed", rows)

    def test_ghss_twelve_hour_times_are_compared_by_day(self):
        self.run_main()
        self.assertNotIn("same/repo", self.rows("c.csv"))
        self.assertEqual(_mod.view(_ghs("a/b", 1, createdAt="2023-06-04T04:23:40"), None)
                         ["createdAt"], "2023-06-04")

    def test_a_name_in_another_case_is_not_a_change(self):
        """GHS records names in another case than GitHub's (compare-retrievals-spec.md), and a
        name that differs only in case is the same repository rather than a rename."""
        self.assertEqual(_mod.differences({"name": "square/okhttp"}, {"name": "Square/OkHttp"}),
                         [])
        self.assertEqual(_mod.differences({"name": "square/okhttp"}, {"name": "lysine-dev/okhttp"}),
                         ["name"])

    def test_a_retrieval_without_a_usable_one_before_it_is_the_first(self):
        ghs_cache.put_index_row({"retrieval_id": OLD, "outcome": "failed"}, self.cache)
        code, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("is the first in the cache", err)

    def test_each_file_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """A power loss shortly after a rename that reached the disk before the data would leave
        an empty file under the name. Each of the two CSVs and the summary is synced under a
        temporary name before its rename, and the directory after the rename."""
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        calls = synced_writes(lambda: self.run_main(), self.tmp)
        # `run_main` writes the pointer before the comparison runs.
        names = [self.pointer.name, "c.csv", "cc.csv", "s.json"]
        inodes = {name: (self.tmp / name).stat().st_ino for name in names}
        self.assertEqual(calls, [entry for name in names for entry in (
            ("file", inodes[name]), ("rename", inodes[name], name), ("directory", True))])

    def test_the_same_retrieval_again_is_not_compared(self):
        """Compared with itself, a retrieval would show every repository and the window
        unchanged. The log states instead that there is nothing to compare."""
        code, err = self.run_main(None, "--old", NEW)
        self.assertEqual(code, 0, err)
        self.assertIn(f"Retrieval {NEW} is both the new and the old retrieval. Nothing to "
                      f"compare.\n", err)
        summary = json.loads((self.tmp / "s.json").read_text())
        self.assertNotIn("retrievals", summary)
        self.assertEqual(self.rows("c.csv"), {})


class TestTheMatch(unittest.TestCase):

    def test_githubs_id_is_matched_before_a_name(self):
        """A row without GitHub's response shares the old name of a repository that a later row
        names by GitHub's id. Matched in order, the name would take the old row, and the later
        row would be listed as added."""
        old = {("id", 7): {"aliases": {"o/old", "o/new"}}}
        new = {("name", "o/old"): {"aliases": {"o/old"}},
               ("id", 7): {"aliases": {"o/new"}}}
        self.assertEqual(_mod.match(old, new),
                         ([(("id", 7), ("id", 7))], [("name", "o/old")], []))

    def test_a_name_still_matches_a_row_without_an_id(self):
        old = {("name", "o/r"): {"aliases": {"o/r"}}}
        new = {("id", 3): {"aliases": {"o/r", "o/renamed"}}}
        self.assertEqual(_mod.match(old, new), ([(("name", "o/r"), ("id", 3))], [], []))

    def test_two_rows_with_different_ids_are_not_matched_by_name(self):
        """A repository keeps its GitHub id when it is renamed. A new id under an old name is
        another repository that took the name."""
        old = {("id", 5): {"aliases": {"o/r"}}}
        new = {("id", 9): {"aliases": {"o/r"}}}
        self.assertEqual(_mod.match(old, new), ([], [("id", 9)], [("id", 5)]))


class TestANameThatAnotherRepositoryTook(_CompareCase):

    def test_the_old_repository_is_removed_and_the_new_one_added(self):
        """Between two retrievals with GitHub's responses, the repository o/r was deleted and
        a repository created after the previous window took its name. GitHub gives the two
        repositories different ids."""
        self.retrieval(OLD, [_ghs("o/r", 1)], responses=[_found("o/r", 5)],
                       created_max="2024-09-16")
        self.retrieval(NEW, [_ghs("o/r", 2, createdAt="2026-10-01T09:00:00")],
                       responses=[_found("o/r", 9, createdAt="2026-10-01T21:00:00Z")])
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        with open(self.tmp / "c.csv", encoding="utf-8", newline="") as f:
            rows = sorted((r["change"], r["github_id"], r["reason"]) for r in csv.DictReader(f))
        self.assertEqual(rows, [("added", "9", "created_after_previous_window"),
                                ("removed", "5", "deleted_or_no_longer_matching")])
        counts = self.summary()["retrievals"]
        self.assertEqual((counts["added"], counts["removed"], counts["changed"]), (1, 1, 0))


class TestTheLicense(unittest.TestCase):

    def test_a_license_spelled_with_quotes_is_not_a_change(self):
        """GitHub writes `BSD 3-Clause "New" or "Revised" License`, as SPDX does, and GHS writes
        the name without the quotes. The two names differ in spelling, not in license."""
        bsd = 'BSD 3-Clause "New" or "Revised" License'
        self.assertEqual(_mod.differences({"license": bsd.replace('"', "")}, {"license": bsd}), [])
        self.assertEqual(_mod.differences({"license": "MIT License"}, {"license": bsd}),
                         ["license"])


class TestTheCommandLine(_CompareCase):

    def test_an_abbreviated_option_is_a_usage_error(self):
        """The step takes long options by their full names only, as every parser that the
        pipeline's command lines build does (`agentcontext.cli.ArgumentParser` gives the
        reason)."""
        with self.assertRaises(SystemExit) as stop:
            self.run_main(None, "--summ", str(self.tmp / "other.json"))
        self.assertEqual(stop.exception.code, 2)


class TestTheBounds(_CompareCase):
    """GHS's createdMax and committedMin are closed bounds that resolve to midnight UTC
    (download-ghs-spec.md). A repository created on the old createdMax day was therefore
    created after the old window. A repository with its last commit on the new committedMin day
    is inside the new window."""

    def test_the_bound_days_fall_on_the_side_that_ghs_puts_them(self):
        self.retrieval(OLD, [_ghs("bound/day", 1, lastCommit="2026-03-05T09:00:00"),
                             _ghs("day/before", 2, lastCommit="2026-03-04T23:00:00")],
                       created_max="2024-09-16")
        self.retrieval(NEW, [_ghs("young/repo", 3, createdAt="2024-09-16T09:00:00")],
                       committed_min="2026-03-05")
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        rows = self.rows("c.csv")
        self.assertEqual(rows["young/repo"]["reason"], "created_after_previous_window")
        self.assertEqual(rows["bound/day"]["reason"], "deleted_or_no_longer_matching")
        self.assertEqual(rows["day/before"]["reason"], "inactive_before_window")

    def test_topics_in_another_order_are_no_change(self):
        self.retrieval(OLD, [_ghs("same/repo", 1, topics=["b", "a"])])
        self.retrieval(NEW, [_ghs("same/repo", 1, topics=["a", "b"])])
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.summary()["retrievals"]["unchanged"], 1)


class TestTheFieldsOfGitHubAlone(_CompareCase):
    """The classification reads GitHub's description and mirror flag, and the filter reads the
    private flag. GHS has none of the three, so they are compared between two retrievals with
    GitHub's responses."""

    def compare(self, old_fields, new_fields):
        """The changed fields of one repository with `old_fields` in the earlier responses and
        `new_fields` in the later ones."""
        self.retrieval(OLD, [_ghs("same/repo", 1)], responses=[_found("same/repo", 7, **old_fields)])
        self.retrieval(NEW, [_ghs("same/repo", 1)], responses=[_found("same/repo", 7, **new_fields)])
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        row = self.rows("c.csv").get("same/repo")
        return row["fields"] if row else ""

    def test_a_new_description_and_a_new_mirror_flag_are_changes(self):
        self.assertEqual(self.compare({"description": "Feeds for Rails.", "isMirror": True,
                                       "isPrivate": False},
                                      {"description": "Feeds for Rails (maintenance mode).",
                                       "isMirror": False, "isPrivate": False}),
                         "isMirror;description")
        self.assertEqual(self.summary()["retrievals"]["fields"], {"description": 1, "isMirror": 1})

    def test_a_private_flag_is_a_change(self):
        self.assertEqual(self.compare({"isPrivate": False}, {"isPrivate": True}), "isPrivate")

    def test_a_removed_description_is_a_change(self):
        """GitHub returns null for a repository without a description, and the classification
        reads that as an empty description."""
        self.assertEqual(self.compare({"description": "A tool."}, {"description": None}),
                         "description")
        self.assertEqual(json.loads(self.rows("c.csv")["same/repo"]["new"]), {"description": ""})

    def test_a_description_that_differs_only_in_surrounding_space_is_no_change(self):
        """The classification reads the description stripped (`1_classify_repos.py`), so a
        trailing space that the owner removed changes nothing that it reads."""
        self.assertEqual(self.compare({"description": "A tool. "}, {"description": "A tool."}),
                         "")
        self.assertEqual(self.summary()["retrievals"]["unchanged"], 1)

    def test_a_response_without_the_description_is_not_compared_on_it(self):
        """The responses of a retrieval queried before the query requested the description lack
        the field. The description of such a retrieval is unknown rather than empty."""
        self.assertEqual(self.compare({}, {"description": "A tool."}), "")
        self.assertEqual(self.summary()["retrievals"]["unchanged"], 1)


class TestTheViews(unittest.TestCase):

    def test_the_row_under_githubs_current_name_stands_for_its_id(self):
        """GHS keeps the row of an old name beside the row of the new name. The row with GitHub's
        current name as its GHS name represents the repository, whichever row comes first."""
        old_row = _mod.view(_ghs("old/name", 1), _found("old/name", 7, name="new/Name"))
        new_row = _mod.view(_ghs("new/Name", 2), _found("new/Name", 7))
        keyed = _mod.by_repository([old_row, new_row])
        self.assertEqual(keyed[("id", 7)]["ghsName"], "new/Name")
        self.assertEqual(keyed[("id", 7)]["aliases"], {"old/name", "new/name"})


class TestTheCollectedSample(_CompareCase):

    def test_every_collected_repository_is_placed_against_the_new_retrieval(self):
        self.retrieval(NEW, [_ghs("moved/repo", 1), _ghs("same/repo", 2), _ghs("gone/repo", 3),
                             _ghs("old/name", 4)],
                       responses=[_found("moved/repo", 1, headSha="b" * 40, defaultBranch="dev"),
                                _found("same/repo", 2),
                                {"ghsName": "gone/repo", "status": "not_found"},
                                _found("new/Name", 4)])
        collected = self.tmp / "collected.csv"
        collected.write_text("repo_name,branch,head_sha\n"
                             f"moved/repo,main,{'a' * 40}\n"
                             f"same/repo,main,{'a' * 40}\n"
                             f"gone/repo,main,{'a' * 40}\n"
                             f"Old/Name,main,{'a' * 40}\n"
                             f"left/window,main,{'a' * 40}\n"
                             "failed/clone,,\n")
        code, err = self.run_main(collected)
        self.assertEqual(code, 0, err)
        rows = self.rows("cc.csv")
        self.assertEqual(rows["moved/repo"]["changes"], "moved;branch_changed")
        self.assertEqual(rows["gone/repo"]["changes"], "gone_on_github")
        self.assertEqual(rows["Old/Name"]["changes"], "renamed")
        self.assertEqual(rows["left/window"]["changes"], "absent_from_retrieval")
        self.assertEqual(rows["failed/clone"]["changes"], "not_collected")
        self.assertNotIn("same/repo", rows)
        c = self.summary()["collection"]
        self.assertEqual((c["collected"], c["moved"], c["unchanged"], c["moved_share"]),
                         (5, 1, 1, 0.2))

    def test_without_githubs_responses_a_repository_is_compared_on_ghss_row(self):
        """Without GitHub's responses, a view's onGitHub is None (unknown), and the repository is
        compared on GHS's row rather than counted as gone."""
        self.retrieval(NEW, [_ghs("same/repo", 2)])
        collected = self.tmp / "collected.csv"
        collected.write_text(f"repo_name,branch,head_sha\nsame/repo,main,{'a' * 40}\n")
        code, err = self.run_main(collected)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("cc.csv"), {})
        self.assertEqual(self.summary()["collection"]["unchanged"], 1)

    def test_a_row_that_the_query_left_unresolved_is_not_gone(self):
        """GitHub returned no response for an unresolved row, and whether GitHub has the
        repository is unknown. The repository is compared on GHS's row rather than counted as
        gone."""
        self.retrieval(NEW, [_ghs("same/repo", 2)],
                       responses=[{"ghsName": "same/repo", "status": "unresolved",
                                 "error": "GitHub returned no response"}])
        collected = self.tmp / "collected.csv"
        collected.write_text(f"repo_name,branch,head_sha\nsame/repo,main,{'a' * 40}\n")
        code, err = self.run_main(collected)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.rows("cc.csv"), {})
        self.assertEqual(self.summary()["collection"]["gone_on_github"], 0)

    def test_reports_of_other_inputs_are_kept_as_the_previous_version(self):
        """The reports of other inputs are renamed to `.previous` names. A comparison that runs
        again after a collection then keeps the reports of the changes to the sample before that
        collection. A run over the same inputs replaces its own reports."""
        self.retrieval(NEW, [_ghs("same/repo", 2), _ghs("moved/repo", 3)],
                       responses=[_found("same/repo", 2), _found("moved/repo", 3, headSha="b" * 40)])
        collected = self.tmp / "collected.csv"
        collected.write_text(f"repo_name,branch,head_sha\nmoved/repo,main,{'a' * 40}\n")
        self.assertEqual(self.run_main(collected)[0], 0)
        first = self.summary()
        code, err = self.run_main(collected)
        self.assertEqual(code, 0, err)
        self.assertFalse((self.tmp / "s.previous.json").exists())
        collected.write_text(f"repo_name,branch,head_sha\nmoved/repo,main,{'b' * 40}\n")
        code, err = self.run_main(collected)
        self.assertEqual(code, 0, err)
        self.assertIn("to .previous names. The collected list changed.\n", err)
        previous = json.loads((self.tmp / "s.previous.json").read_text())
        self.assertEqual(previous, first)
        self.assertNotEqual(self.summary()["collected_sha256"], previous["collected_sha256"])
        with open(self.tmp / "cc.previous.csv", newline="", encoding="utf-8") as f:
            self.assertEqual([r["changes"] for r in csv.DictReader(f)], ["moved"])
        self.assertEqual(self.rows("cc.csv"), {})
        self.assertTrue((self.tmp / "c.previous.csv").is_file())

    def test_git_ignores_every_report_and_its_previous_version(self):
        """The comparison writes its reports, and their `.previous` versions, under the stage's
        data directory. git ignores all of them, because a comparison in the checkout, such as a
        run with `--from retrieval_comparison`, would otherwise leave untracked files, and the
        verification fails on a tree that is not clean."""
        from agentcontext import REPO_ROOT
        try:
            directory = _mod.CHANGES.parent.relative_to(REPO_ROOT)
        except ValueError:
            self.skipTest("the data root is outside the checkout")
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=REPO_ROOT,
                                capture_output=True, text=True)
        if inside.returncode or inside.stdout.strip() != "true":
            self.skipTest("not a git checkout, so there are no ignore rules to test")
        self.retrieval(NEW, [_ghs("same/repo", 2)], responses=[_found("same/repo", 2)])
        collected = self.tmp / "collected.csv"
        reports = [path.name for path in (_mod.CHANGES, _mod.COLLECTION_CHANGES, _mod.SUMMARY)]
        named = ["--changes", str(self.tmp / reports[0]),
                 "--collection-changes", str(self.tmp / reports[1]),
                 "--summary", str(self.tmp / reports[2])]
        for head in ("a", "b"):
            collected.write_text(f"repo_name,branch,head_sha\nsame/repo,main,{head * 40}\n")
            code, err = self.run_main(collected, *named)
            self.assertEqual(code, 0, err)
        inputs = {collected.name, self.pointer.name, self.cache.name}
        written = sorted(path.name for path in self.tmp.iterdir() if path.name not in inputs)
        self.assertEqual(len(written), 2 * len(reports), written)
        for name in written:
            with self.subTest(report=name):
                ignored = subprocess.run(["git", "check-ignore", "-q", "--no-index",
                                          (directory / name).as_posix()], cwd=REPO_ROOT)
                self.assertEqual(ignored.returncode, 0, f"git does not ignore {directory / name}")

    def test_a_collected_list_without_the_columns_is_an_error(self):
        self.retrieval(NEW, [_ghs("a/b", 1)])
        collected = self.tmp / "collected.csv"
        collected.write_text("repo_name\na/b\n")
        code, err = self.run_main(collected)
        self.assertEqual(code, 1)
        self.assertIn("has no column branch, head_sha", err)


if __name__ == "__main__":
    unittest.main()
