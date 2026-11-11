"""
Tests for 4_filter_ghs.py

Strategy:
  - `_script_loader` loads the script by path and stubs `requests` and `yaml` where they are
    absent. Without `yaml`, the tests that read a YAML config are skipped.
  - Every filter step takes a list of rows and a config and returns the rows that it kept and
    its record. Each step is tested over a few rows built here, each merged from a GHS row and
    GitHub's response as the script merges them.
  - A run reads a retrieval and GitHub's responses from a cache written in a temporary
    directory. The duplicates step is tested with two pairs from the GHS search result of
    March: a repository renamed only in case and a repository transferred under a new name. It
    is also tested with two unrelated repositories that Apache created in one second.
  - The refresh of the SPDX list is tested against a fake `requests.get`.
  - `_script_loader.synced_writes` records each call of the real `os.fsync` and `os.replace`,
    and the tests read from these calls that a file is on disk before its rename.
  - The step of run_sampling.py that runs the filter is compared with the shipped config here,
    because the tests read the config with yaml and the suite of the sampling runner runs
    without site packages.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_filter_ghs -v
"""

import contextlib
import importlib
import importlib.util
import io
import os
import json
import re
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _script_loader import is_stub, load_script, synced_writes  # noqa: E402

_mod = load_script("4_filter_ghs.py")
import ghs_cache  # noqa: E402

HERE = Path(__file__).resolve().parent
RID = "2026-10-05T120000Z"
# The window's bound on the creation date in the retrieval of these tests.
CREATED_MAX = "2025-03-22"


def _ghs(name, ghs_id, **fields):
    row = {"id": ghs_id, "name": name, "isArchived": False, "isDisabled": False,
           "isLocked": False, "isFork": False, "totalPullRequests": 1, "totalIssues": 1,
           "license": "MIT License", "mainLanguage": "Python", "topics": [], "commits": 10,
           "watchers": 5, "createdAt": "2020-01-01T04:00:00", "metrics": [], "labels": []}
    row.update(fields)
    return row


def _found(name, github_id, **fields):
    repository = {"id": github_id, "name": name, "createdAt": "2020-01-01T16:00:00Z",
                  "pushedAt": "2026-10-01T00:00:00Z", "updatedAt": "2026-10-02T00:00:00Z",
                  "isArchived": False, "isDisabled": False, "isLocked": False, "isFork": False,
                  "isPrivate": False, "isMirror": False,
                  "license": "MIT License", "licenseSpdxId": "MIT", "primaryLanguage": "Python",
                  "stargazers": 50, "defaultBranch": "main", "headSha": f"{github_id:040x}",
                  "headCommittedAt": "2026-09-30T00:00:00Z", "description": "A tool."}
    repository.update(fields)
    return {"status": "found", "repository": repository}


def _row(ghs_name, ghs_id, github_id=None, ghs=None, current=None, **github):
    """A GHS row merged with GitHub's response for it. The response names the repository
    `current` when GitHub returns another name for it."""
    response = _found(current or ghs_name, github_id or 1000 + ghs_id, **github)
    return _mod.merge(_ghs(ghs_name, ghs_id, **(ghs or {})), {"ghsName": ghs_name, **response})


def _config(**overrides):
    return _mod.FilterConfig(**overrides)


class TestMerge(unittest.TestCase):

    def test_the_sampling_frame_contains_githubs_response_and_keeps_ghss_id_and_name(self):
        item = _row("Old/Name", 7, github_id=649369073, current="new/Name", isArchived=True,
                    primaryLanguage="Rust", headSha="b" * 40)
        self.assertEqual((item["id"], item["ghsId"]), (649369073, 7))
        self.assertEqual((item["name"], item["ghsName"]), ("new/Name", "Old/Name"))
        self.assertEqual(item["createdAt"], "2020-01-01T16:00:00Z")
        self.assertEqual((item["isArchived"], item["mainLanguage"], item["lastCommitSHA"]),
                         (True, "Rust", "b" * 40))
        self.assertEqual(item["commits"], 10)

    def test_githubs_description_is_kept_and_none_when_github_returns_none(self):
        self.assertEqual(_row("o/a", 1, description="A formatter.")["description"], "A formatter.")
        self.assertIsNone(_row("o/a", 1, description=None)["description"])

    def test_a_row_github_does_not_have_keeps_ghss_values(self):
        item = _mod.merge(_ghs("gone/repo", 3), {"ghsName": "gone/repo", "status": "not_found"})
        self.assertEqual((item["githubStatus"], item["id"], item["name"]), ("not_found", 3,
                                                                             "gone/repo"))


class TestLicenseRules(unittest.TestCase):

    def test_a_direct_match_and_githubs_spellings(self):
        self.assertTrue(_mod.is_osi_approved("MIT License", {"MIT License"}))
        self.assertTrue(_mod.is_osi_approved("GNU General Public License v3.0",
                                             {"GNU General Public License v3.0 only"}))
        self.assertFalse(_mod.is_osi_approved("Other", {"MIT License"}))
        self.assertFalse(_mod.is_osi_approved(None, {"MIT License"}))

    def test_a_license_name_counts_in_either_spelling(self):
        """GitHub's GraphQL API writes the BSD names with quotes, as SPDX does, and GHS writes
        them without. The exclusion `BSD 4-Clause Original or Old License` applies to both
        spellings."""
        osi = {'BSD 3-Clause "New" or "Revised" License', 'BSD 4-Clause "Original" or "Old" License'}
        self.assertTrue(_mod.is_osi_approved("BSD 3-Clause New or Revised License", osi))
        self.assertTrue(_mod.is_osi_approved('BSD 3-Clause "New" or "Revised" License', osi))
        items = [{"license": 'BSD 4-Clause "Original" or "Old" License'},
                 {"license": "BSD 4-Clause Original or Old License"},
                 {"license": 'BSD 3-Clause "New" or "Revised" License'}]
        self.assertEqual(_mod.allowed_licenses(items, osi, ["BSD 4-Clause Original or Old License"], 0),
                         {'BSD 3-Clause "New" or "Revised" License'})

    def test_allowed_is_osi_minus_exclusions_minus_the_rare(self):
        items = [{"license": "MIT License"}] * 3 + [{"license": "Apache License 2.0"}] * 3 \
            + [{"license": "Other"}]
        osi = {"MIT License", "Apache License 2.0"}
        self.assertEqual(_mod.allowed_licenses(items, osi, ["MIT License"], 0),
                         {"Apache License 2.0"})
        rare = [{"license": "MIT License"}] * 10 + [{"license": "Apache License 2.0"}]
        self.assertEqual(_mod.allowed_licenses(rare, osi, [], 0), osi)
        self.assertEqual(_mod.allowed_licenses(rare, osi, [], 0.5), {"MIT License"})

    def test_the_quantile_interpolates_as_numpy_does(self):
        self.assertEqual(_mod.quantile([1, 2, 3, 4], 0.5), 2.5)
        self.assertEqual(_mod.quantile([5, 1, 3], 0.5), 3)
        self.assertEqual(_mod.quantile([0, 10], 0.25), 2.5)


class TestFilterSteps(unittest.TestCase):

    def test_gone_removes_what_github_does_not_have_or_blocks(self):
        items = [_row("a/a", 1),
                 _mod.merge(_ghs("b/b", 2), {"ghsName": "b/b", "status": "not_found"}),
                 _mod.merge(_ghs("c/c", 3), {"ghsName": "c/c", "status": "blocked"}),
                 _row("d/d", 4, isPrivate=True)]
        kept, result = _mod.step_gone(items, _config(), CREATED_MAX)
        self.assertEqual([i["name"] for i in kept], ["a/a"])
        self.assertEqual((result.removed, result.remaining), (3, 1))
        self.assertEqual(result.values, {"unresolved": 0, "without_id": 0,
                                         "created_after_window": 0})

    def test_gone_removes_the_unresolved_rows_under_their_name(self):
        """No response says whether GitHub has a row that the GitHub query left unresolved. The
        step removes such a row and counts it apart from the repositories that GitHub does not
        have."""
        items = [_row("a/a", 1),
                 _mod.merge(_ghs("b/b", 2), {"ghsName": "b/b", "status": "not_found"}),
                 _mod.merge(_ghs("c/c", 3), {"ghsName": "c/c", "status": "unresolved",
                                             "error": "GitHub returned no response"})]
        kept, result = _mod.step_gone(items, _config(), CREATED_MAX)
        self.assertEqual([i["name"] for i in kept], ["a/a"])
        self.assertEqual((result.removed, result.values),
                         (2, {"unresolved": 1, "without_id": 0, "created_after_window": 0}))

    def test_gone_removes_the_repositories_that_github_found_without_an_id(self):
        """GitHub's GraphQL schema declares `databaseId` as a nullable `Int`. Two repositories
        that GitHub found without an id and that were created in one second are not one
        repository. The later stages key a repository by its id. The sampling frame therefore
        keeps neither repository, and the step counts both."""
        items = [_row("a/a", 1),
                 _row("b/b", 2, id=None, createdAt="2014-01-01T10:00:00Z"),
                 _row("c/c", 3, id=None, createdAt="2014-01-01T10:00:00Z")]
        config = _config(filter_licenses=False, filter_languages=False,
                         filter_educational=False, filter_activity=False)
        with contextlib.redirect_stderr(io.StringIO()):
            kept, results = _mod.run_steps(items, config, set(), created_max=CREATED_MAX)
        self.assertEqual([(i["name"], i["id"]) for i in kept], [("a/a", 1001)])
        gone = next(result for result in results if result.key == "gone")
        self.assertEqual((gone.removed, gone.values),
                         (2, {"unresolved": 0, "without_id": 2, "created_after_window": 0}))

    def test_gone_removes_a_repository_that_github_created_after_the_window(self):
        """GHS's window admitted the row of google/cel-go by the creation date of the repository
        that GHS crawled, in 2017. For google/cel-go, GitHub returns a repository that it created
        on 2026-09-03, after the window's bound of 2025-03-22. For o/day-after, GitHub returns a
        repository that it created in the first hour of the day after the bound. Neither is the
        repository that GHS crawled, and the step removes both and counts them. A repository
        that GitHub created late on the bound's day stays, because the step compares days. GHS
        records its creation time as 11:59:59 on its 12-hour clock."""
        items = [_row("google/cel-go", 1, ghs={"createdAt": "2017-12-12T10:22:47"},
                      createdAt="2026-09-03T22:41:59Z"),
                 _row("o/day-after", 2, ghs={"createdAt": "2016-05-01T08:00:00"},
                      createdAt="2025-03-23T00:30:00Z"),
                 _row("o/same-day", 3, ghs={"createdAt": "2025-03-22T11:59:59"},
                      createdAt="2025-03-22T23:59:59Z"),
                 _row("o/older", 4)]
        kept, result = _mod.step_gone(items, _config(), CREATED_MAX)
        self.assertEqual([i["name"] for i in kept], ["o/same-day", "o/older"])
        self.assertEqual((result.removed, result.values),
                         (2, {"unresolved": 0, "without_id": 0, "created_after_window": 2}))

    def test_duplicates_are_one_repository_on_github_and_keep_the_current_name(self):
        # The pairs of the GHS search result of March: renamed in case only, and transferred
        # under a new name.
        items = [_row("pagehelper/mybatis-pagehelper", 1272041, github_id=1,
                      current="pagehelper/Mybatis-PageHelper"),
                 _row("pagehelper-org/Mybatis-PageHelper", 95645171, github_id=1,
                      current="pagehelper/Mybatis-PageHelper"),
                 _row("fm-sys/snapdrop-android", 10, github_id=2, current="fm-sys/pairdrop-android"),
                 _row("fm-sys/pairdrop-android", 11, github_id=2, current="fm-sys/pairdrop-android")]
        kept, result = _mod.step_duplicates(items, _config())
        self.assertEqual([i["ghsName"] for i in kept],
                         ["pagehelper/mybatis-pagehelper", "fm-sys/pairdrop-android"])
        self.assertEqual([i["name"] for i in kept],
                         ["pagehelper/Mybatis-PageHelper", "fm-sys/pairdrop-android"])
        self.assertEqual((result.removed, result.values["groups"]), (2, 2))

    def test_two_repositories_under_one_name_are_both_left_out(self):
        """One repository gave up `a/x`, and another took the name while the retrieval's
        responses were gathered. The later stages join by name, and neither response tells
        which repository holds the name now. Both repositories are left out and counted."""
        items = [_row("a/x", 1, github_id=1, createdAt="2020-01-01T00:00:00Z"),
                 _row("a/x-next", 2, github_id=2, current="A/X", createdAt="2021-01-01T00:00:00Z"),
                 _row("a/y", 3)]
        kept, result = _mod.step_duplicates(items, _config())
        self.assertEqual([i["name"] for i in kept], ["a/y"])
        self.assertEqual((result.removed, result.values),
                         (2, {"groups": 0, "shared_names": 1}))

    def test_repositories_created_in_one_second_are_not_duplicates(self):
        # apache/karaf and apache/maven-surefire share a creation second in the GHS search result
        # of March.
        items = [_row("apache/karaf", 1, createdAt="2014-01-01T10:00:00Z"),
                 _row("apache/maven-surefire", 2, createdAt="2014-01-01T10:00:00Z")]
        kept, result = _mod.step_duplicates(items, _config())
        self.assertEqual(len(kept), 2)
        self.assertEqual(result.removed, 0)

    def test_without_the_current_name_the_most_commits_then_the_lowest_ghs_id_win(self):
        items = [_row("x/old", 5, github_id=9, current="y/new", ghs={"commits": 3}),
                 _row("x/older", 4, github_id=9, current="y/new", ghs={"commits": 7}),
                 _row("x/oldest", 3, github_id=9, current="y/new", ghs={"commits": 7})]
        kept, _ = _mod.step_duplicates(items, _config())
        self.assertEqual([i["ghsId"] for i in kept], [3])

    def test_cleanup_reads_githubs_flags(self):
        items = [_row("a/a", 1, ghs={"isArchived": True}), _row("b/b", 2, isArchived=True),
                 _row("c/c", 3, isDisabled=True), _row("d/d", 4, isLocked=True),
                 _row("e/e", 5, isFork=True)]
        kept, result = _mod.step_cleanup(items, _config())
        self.assertEqual([i["name"] for i in kept], ["a/a"])
        self.assertEqual(result.values["flags"], ["isArchived", "isDisabled", "isLocked", "isFork"])
        kept, _ = _mod.step_cleanup(items, _config(exclude_forks=False))
        self.assertEqual([i["name"] for i in kept], ["a/a", "e/e"])
        kept, _ = _mod.step_cleanup(items, _config(filter_cleanup=False))
        self.assertEqual(len(kept), 5)

    def test_missing_data_drops_a_row_without_issue_or_pull_counts(self):
        items = [_row("a/a", 1), _row("b/b", 2, ghs={"totalIssues": None})]
        self.assertEqual(len(_mod.step_missing_data(items, _config())[0]), 1)

    def test_licenses_read_githubs_license(self):
        items = [_row("a/a", 1, license="Other"), _row("b/b", 2, ghs={"license": "Other"}),
                 _row("c/c", 3, license=None)]
        kept, result = _mod.step_licenses(items, _config(), {"MIT License"})
        self.assertEqual([i["name"] for i in kept], ["b/b"])
        self.assertEqual(result.values["allowed"], ["MIT License"])

    def test_languages_keep_the_top_n_and_a_tie_goes_by_name(self):
        items = [_row(f"r/{i}", i, primaryLanguage=lang)
                 for i, lang in enumerate(["Go", "Go", "Rust", "C", None])]
        kept, result = _mod.step_languages(items, _config(top_n_languages=2))
        self.assertEqual(result.values["languages"], ["C", "Go"])
        self.assertEqual(result.values["left_out"], {})
        self.assertEqual(len(kept), 3)

    def test_languages_rank_only_the_languages_that_the_end_date_covers(self):
        """GHS lacks the pushes of a language after its crawl date. The step passes over a
        language that the end date left out and keeps the next most common language. Zig ranks
        before C, which the step keeps, and the warning names Zig as well. Java ranks after the
        last language kept, and the warning does not name it. Without the end date, the step
        would keep Rust and Go, not Zig. The warning therefore says that the step passes over a
        language while it chooses among the languages that the end date covers, which holds for
        Zig too."""
        languages = ["Rust"] * 4 + ["Go"] * 3 + ["Zig"] * 3 + ["C"] * 2 + ["Java", None]
        items = [_row(f"r/{i}", i, primaryLanguage=lang) for i, lang in enumerate(languages)]
        left_out = {"Rust": "2026-08-01", "Zig": None, "Java": "2026-07-01", "Dart": None}
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            kept, result = _mod.step_languages(items, _config(top_n_languages=2), left_out)
        self.assertEqual(result.values, {"top": 2, "languages": ["C", "Go"],
                                         "left_out": {"Rust": "2026-08-01", "Zig": None}})
        self.assertEqual({item["mainLanguage"] for item in kept}, {"C", "Go"})
        self.assertEqual((result.removed, result.remaining), (9, 5))
        self.assertEqual(result.description, "not one of the top 2 languages that the end date "
                                             "covers")
        self.assertIn("WARNING repository_filtering: Filter step 6 (languages) left out Rust, "
                      "because the crawl date of Rust, 2026-08-01, is before the end date of the "
                      "sampling window. Kept the next most common language in its place.\n",
                      err.getvalue())
        self.assertIn("left out Zig, because GHS returned no crawl date for Zig. Kept the next "
                      "most common language in its place.\n", err.getvalue())
        self.assertNotIn("Java", err.getvalue())

    def test_without_the_language_filter_the_step_keeps_every_language_the_end_date_covers(self):
        """`filter_languages: false` keeps every language. A language that the end date left
        out lacks the same pushes, and the step leaves out only that language's rows. Without
        such a language, the step is skipped."""
        items = [_row(f"r/{i}", i, primaryLanguage=lang)
                 for i, lang in enumerate(["Go", "Rust", "C", None])]
        config = _config(filter_languages=False)
        kept, result = _mod.step_languages(items, config, {})
        self.assertEqual((len(kept), result.description, result.values), (4, "skipped", {}))
        with contextlib.redirect_stderr(io.StringIO()):
            kept, result = _mod.step_languages(items, config, {"Rust": "2026-08-01"})
        self.assertEqual([item["mainLanguage"] for item in kept], ["Go", "C", None])
        self.assertEqual(result.values, {"left_out": {"Rust": "2026-08-01"}})

    def test_educational_matches_topics_and_the_current_name(self):
        config = _config(educational_topic_regex="tutorial", educational_name_regex="course")
        items = [_row("a/a", 1, ghs={"topics": ["tutorial"]}),
                 _row("b/b", 2, current="b/Python-Course"), _row("c/c", 3)]
        kept, _ = _mod.step_educational(items, config)
        self.assertEqual([i["name"] for i in kept], ["c/c"])

    def test_activity_takes_the_absolute_value_over_the_quantile(self):
        items = [_row(f"r/{i}", i, ghs={"commits": c, "watchers": 10})
                 for i, c in enumerate([1, 5, 9])]
        kept, result = _mod.step_activity(items, _config())
        self.assertEqual([i["commits"] for i in kept], [5, 9])
        self.assertEqual(result.values, {"commits_min": 5, "watchers_min": 10})
        kept, result = _mod.step_activity(items, _config(commits_min=9))
        self.assertEqual([i["commits"] for i in kept], [9])


class _RunCase(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cache = self.tmp / "ghs_retrievals"
        self.pointer = self.tmp / "ghs_retrieval.json"
        self.output = self.tmp / "ghs_filtered.json"
        self.config = self.tmp / "filter.yaml"
        self.config.write_text("filter_licenses: false\nfilter_activity: false\n")

    def retrieval(self, ghs_rows, github_rows, responded=True, not_found=1, unresolved=0,
                  crawl_dates=None):
        """Write the retrieval of this test into the cache, as the download and the GitHub
        query write it. `crawl_dates` maps each language to its crawl date, which the download
        records with the end date `crawl_date`. None records no crawl dates, as the download
        does with a date as the end date."""
        _, sha = ghs_cache.write_rows(self.cache, RID, "ghs", ghs_rows)
        values = {"retrieval_id": RID, "outcome": "retrieved", "ghs_file_sha256": sha,
                  "started_at": "2026-10-05T12:00:00Z", "end_date": "2026-09-22",
                  "end_date_source": "crawl_date:Python", "created_max": "2025-03-22",
                  "committed_min": "2026-03-22", "query": {"contributorsMin": 2},
                  "ghs_version": "1.17.1", "item_count": len(ghs_rows),
                  "crawl_dates": crawl_dates}
        if responded:
            _, github_sha = ghs_cache.write_rows(self.cache, RID, "github", github_rows)
            values.update(github_file_sha256=github_sha, github_found=2,
                          github_not_found=not_found, github_unresolved=unresolved,
                          github_started_at="2026-10-05T13:00:00Z",
                          github_finished_at="2026-10-05T13:20:00Z")
        ghs_cache.put_index_row(values, self.cache)
        ghs_cache.write_pointer(RID, self.pointer)

    def run_main(self, *extra):
        """Run `main` over this test's retrieval, with the stage's data directory in the
        temporary directory. The license table is then written there rather than over the
        tracked table in data/sampling/."""
        err = io.StringIO()
        self.data = self.tmp / "data"
        self.data.mkdir(exist_ok=True)
        with mock.patch.object(_mod, "SAMPLING_DIR", self.data), contextlib.redirect_stderr(err):
            code = _mod.main(["--config", str(self.config), "--cache-dir", str(self.cache),
                              "--pointer", str(self.pointer), "--output", str(self.output),
                              *extra])
        return code, err.getvalue()

    def test_a_run_writes_its_table_in_the_stages_data_directory_it_was_given(self):
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **_found("a/a", 11)}])
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        self.assertTrue((self.data / "ghs_licenses.csv").is_file())


@unittest.skipIf(is_stub("yaml"), "yaml is not installed")
class TestARun(_RunCase):

    def test_a_run_writes_the_sampling_frame_with_its_provenance(self):
        self.retrieval([_ghs("a/a", 1), _ghs("b/b", 2), _ghs("c/c", 3)],
                       [{"ghsName": "a/a", **_found("a/a", 11)},
                        {"ghsName": "b/b", "status": "not_found", "restChecked": True},
                        {"ghsName": "c/c", **_found("c/new", 13)}])
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        frame = json.loads(self.output.read_text())
        self.assertEqual([(i["name"], i["id"], i["ghsId"]) for i in frame["items"]],
                         [("a/a", 11, 1), ("c/new", 13, 3)])
        meta = frame["_metadata"]
        self.assertEqual(meta["input_source"]["retrieval_id"], RID)
        self.assertEqual(meta["input_source"]["downloaded_at"], "2026-10-05T12:00:00Z")
        self.assertEqual(meta["input_source"]["window"]["committed_min"], "2026-03-22")
        self.assertEqual(meta["input_source"]["github"]["found"], 2)
        self.assertEqual([s["key"] for s in meta["stages"]],
                         ["gone", "duplicates", "cleanup", "missing_data", "licenses",
                          "languages", "educational", "activity"])
        self.assertEqual((meta["stages"][0]["removed"], meta["final_count"]), (1, 2))

    def test_the_record_counts_an_unresolved_row_apart_from_the_repositories_not_found(self):
        """The release publishes the sampling frame's record as `sampling.json`. The record
        names the rows that the GitHub query left unresolved apart from the repositories that
        GitHub did not find, because no response says that an unresolved repository is gone."""
        self.retrieval([_ghs("a/a", 1), _ghs("b/b", 2), _ghs("c/c", 3)],
                       [{"ghsName": "a/a", **_found("a/a", 11)},
                        {"ghsName": "b/b", "status": "unresolved",
                         "error": "GitHub returned no response"},
                        {"ghsName": "c/c", **_found("c/c", 13)}], not_found=0, unresolved=1)
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        meta = json.loads(self.output.read_text())["_metadata"]
        github = meta["input_source"]["github"]
        self.assertEqual((github["found"], github["not_found"], github["unresolved"]), (2, 0, 1))
        self.assertEqual((meta["stages"][0]["removed"], meta["stages"][0]["values"]),
                         (1, {"unresolved": 1, "without_id": 0, "created_after_window": 0}))

    def test_a_repository_that_github_created_after_the_window_is_not_in_the_sampling_frame(self):
        """GHS's row of google/cel-go describes the repository created in 2017 that the window
        admitted. Under the name, GitHub has another repository, created on 2026-09-03, after
        the window's bound of 2025-03-22 in the retrieval's index row. The sampling frame leaves
        the row out, and its record counts it under the step `gone`. A repository stays when
        its creation times in GHS and GitHub differ only within the bound's day."""
        self.retrieval([_ghs("google/cel-go", 1, createdAt="2017-12-12T10:22:47"),
                        _ghs("o/same-day", 2, createdAt="2025-03-22T11:59:59"),
                        _ghs("o/older", 3)],
                       [{"ghsName": "google/cel-go",
                         **_found("google/cel-go", 11, createdAt="2026-09-03T22:41:59Z")},
                        {"ghsName": "o/same-day",
                         **_found("o/same-day", 12, createdAt="2025-03-22T23:59:59Z")},
                        {"ghsName": "o/older", **_found("o/older", 13)}], not_found=0)
        code, err = self.run_main()
        self.assertEqual(code, 0, err)
        sampling_frame = json.loads(self.output.read_text())
        self.assertEqual([i["name"] for i in sampling_frame["items"]], ["o/same-day", "o/older"])
        gone = sampling_frame["_metadata"]["stages"][0]
        self.assertEqual((gone["key"], gone["removed"], gone["values"]),
                         ("gone", 1, {"unresolved": 0, "without_id": 0,
                                      "created_after_window": 1}))

    def test_a_bound_on_the_creation_date_that_is_not_a_date_is_refused(self):
        """A blank bound, compared as a day, would remove every row. An index row without a
        date as `created_max` therefore stops the filter, as crawl dates that cannot be read
        do."""
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **_found("a/a", 11)}])
        for value in ("", "2025-3-22", "March 2025"):
            with self.subTest(created_max=value):
                ghs_cache.put_index_row({"retrieval_id": RID, "created_max": value}, self.cache)
                code, err = self.run_main()
                self.assertEqual(code, 1, err)
                self.assertIn(f"The retrieval cannot be read. The index row of retrieval {RID} "
                              f"records the created_max {value!r}, not a date YYYY-MM-DD", err)
                self.assertFalse(self.output.exists())

    def test_a_relative_output_path_of_the_config_is_under_the_stages_data_directory(self):
        """The runner starts the filter in the data directory. A run by hand from another
        directory, such as the scripts' directory, writes the sampling frame into the data
        directory too rather than beside the scripts."""
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **_found("a/a", 11)}])
        self.config.write_text("filter_licenses: false\nfilter_activity: false\n"
                               "output_path: frame.json\n")
        data, elsewhere = self.tmp / "data" / "sampling", self.tmp / "elsewhere"
        data.mkdir(parents=True)
        elsewhere.mkdir()
        err = io.StringIO()
        cwd = os.getcwd()
        os.chdir(elsewhere)
        try:
            with mock.patch.object(_mod, "SAMPLING_DIR", data), contextlib.redirect_stderr(err):
                code = _mod.main(["--config", str(self.config), "--cache-dir", str(self.cache),
                                  "--pointer", str(self.pointer)])
        finally:
            os.chdir(cwd)
        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue((data / "frame.json").is_file())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_a_response_without_a_field_of_the_query_is_refused(self):
        """A response without the description is rejected. Merged into the rows, such a
        response would leave every repository in the sampling frame without a description. The
        classification would then send the model the line for a repository without a
        description."""
        response = _found("a/a", 11)
        del response["repository"]["description"]
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **response}])
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"The retrieval cannot be read. GitHub's response in row 1 of retrieval "
                      f"{RID} has no description. Run 2_query_github.py again", err)
        self.assertFalse(self.output.exists())

    def test_a_retrieval_without_githubs_responses_is_refused(self):
        self.retrieval([_ghs("a/a", 1)], [], responded=False)
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("has no responses from GitHub. Run 2_query_github.py first", err)
        self.assertFalse(self.output.exists())

    def test_responses_out_of_step_with_the_rows_are_refused(self):
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "z/z", **_found("z/z", 1)}])
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"GitHub's response in row 1 of retrieval {RID} is for 'z/z', but the GHS "
                      f"row is for 'a/a'", err)

    def test_a_dry_run_writes_nothing(self):
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **_found("a/a", 1)}])
        code, err = self.run_main("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.output.exists())

    # Python and Rust are the most common languages, and Go and Zig follow. Python was crawled
    # to the end date 2026-09-22 and Go after it. GHS's crawl of Rust stopped on 2026-08-01, and
    # GHS returned no crawl date for Zig. The end date left both languages out, and the download
    # made its decision for a filter that keeps one language.
    LEFT_OUT = {"Python": "2026-09-22", "Go": "2026-09-25", "Rust": "2026-08-01", "Zig": None}

    def _languages(self, crawl_dates):
        self.retrieval(
            [_ghs("a/a", 1), _ghs("b/b", 2), _ghs("c/c", 3, mainLanguage="Rust"),
             _ghs("d/d", 4, mainLanguage="Zig"), _ghs("e/e", 5, mainLanguage="Rust"),
             _ghs("f/f", 6, mainLanguage="Go")],
            [{"ghsName": "a/a", **_found("a/a", 11)}, {"ghsName": "b/b", **_found("b/b", 12)},
             {"ghsName": "c/c", **_found("c/c", 13, primaryLanguage="Rust")},
             {"ghsName": "d/d", **_found("d/d", 14, primaryLanguage="Zig")},
             {"ghsName": "e/e", **_found("e/e", 15, primaryLanguage="Rust")},
             {"ghsName": "f/f", **_found("f/f", 16, primaryLanguage="Go")}],
            crawl_dates=crawl_dates)

    def _languages_step(self) -> dict:
        """The record of the step `languages` in the sampling frame that the run wrote."""
        stages = json.loads(self.output.read_text())["_metadata"]["stages"]
        return next(stage for stage in stages if stage["key"] == "languages")

    def test_a_language_that_the_end_date_left_out_is_not_kept(self):
        """`--from repository_filtering` with a larger `--top-languages`, or an image that
        raises `top_n_languages` in the middle of a month, runs the filter over a retrieval with
        an end date that the download chose for fewer languages. If the step kept Rust, the
        sampling frame would get repositories for which GHS lacks the pushes after August 1. A
        filter that refused the retrieval would stop every Start, because the download, run
        again, decides the same way. The step passes over Rust, and over Zig once Zig ranks
        before the last language kept. It keeps Go in their place, and the run writes the
        sampling frame. Of three languages, the step keeps the two that the end date covers."""
        self._languages(self.LEFT_OUT)
        rust = ("left out Rust, because the crawl date of Rust, 2026-08-01, is before the end "
                "date of the sampling window. Kept the next most common language in its place")
        zig = ("left out Zig, because GHS returned no crawl date for Zig. Kept the next most "
               "common language in its place")
        for top, kept, languages, left_out, named in (
                ("1", ["a/a", "b/b"], ["Python"], {}, []),
                ("2", ["a/a", "b/b", "f/f"], ["Go", "Python"], {"Rust": "2026-08-01"}, [rust]),
                ("3", ["a/a", "b/b", "f/f"], ["Go", "Python"],
                 {"Rust": "2026-08-01", "Zig": None}, [rust, zig])):
            with self.subTest(top=top):
                code, err = self.run_main("--top-languages", top)
                self.assertEqual(code, 0, err)
                self.assertEqual([i["name"] for i in json.loads(self.output.read_text())["items"]],
                                 kept)
                self.assertEqual(self._languages_step()["values"],
                                 {"top": int(top), "languages": languages, "left_out": left_out})
                for text in named:
                    self.assertIn(f"WARNING repository_filtering: Filter step 6 (languages) "
                                  f"{text}.\n", err)
                self.assertEqual(err.count("WARNING repository_filtering: Filter step 6"),
                                 len(named))

    def test_a_language_that_ranks_higher_after_the_earlier_steps_gives_way(self):
        """The download decides whether the filter would keep a language that the end date left
        out (`would_keep` in 1_download_ghs.py). It decides from GHS's rows under the licenses
        that the filter keeps. Go and Rust have two rows each there. A filter of two languages
        keeps Go, and the download took the retrieval. GitHub reports one repository in Go as
        archived, and after the cleanup step Rust ranks second. A filter that stopped because
        it could not keep Rust would stop the run again after every download, because the
        download, run again, takes the same decision. The step keeps Go, the next language that
        the end date covers, and records Rust. The table of the filter steps and the README of
        the release name Rust."""
        ghs = [_ghs("a/a", 1), _ghs("b/b", 2), _ghs("c/c", 3),
               _ghs("d/d", 4, mainLanguage="Go"), _ghs("e/e", 5, mainLanguage="Go"),
               _ghs("f/f", 6, mainLanguage="Rust"), _ghs("g/g", 7, mainLanguage="Rust")]
        self.assertEqual(load_script("1_download_ghs.py").would_keep(ghs, 2),
                         frozenset({"Python", "Go"}))
        self.retrieval(
            ghs,
            [{"ghsName": "a/a", **_found("a/a", 11)}, {"ghsName": "b/b", **_found("b/b", 12)},
             {"ghsName": "c/c", **_found("c/c", 13)},
             {"ghsName": "d/d", **_found("d/d", 14, primaryLanguage="Go")},
             {"ghsName": "e/e", **_found("e/e", 15, primaryLanguage="Go", isArchived=True)},
             {"ghsName": "f/f", **_found("f/f", 16, primaryLanguage="Rust")},
             {"ghsName": "g/g", **_found("g/g", 17, primaryLanguage="Rust")}],
            crawl_dates={"Python": "2026-09-22", "Go": "2026-09-25", "Rust": "2026-08-01"})
        code, err = self.run_main("--top-languages", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("Filter step 6 (languages) left out Rust, because the crawl date of Rust, "
                      "2026-08-01, is before the end date of the sampling window. Kept the next "
                      "most common language in its place.\n", err)
        sampling_frame = json.loads(self.output.read_text())
        self.assertEqual([i["name"] for i in sampling_frame["items"]],
                         ["a/a", "b/b", "c/c", "d/d"])
        step = self._languages_step()
        self.assertEqual(step["values"], {"top": 2, "languages": ["Go", "Python"],
                                          "left_out": {"Rust": "2026-08-01"}})
        self.assertEqual((step["removed"], step["remaining"]), (2, 4))
        path = HERE.parent / "4_data-analysis" / "dataset" / "filter_steps_table.py"
        spec = importlib.util.spec_from_file_location("_sampling_filter_steps_table", path)
        table = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(table)
        self.assertIn(r"Non-Top-2 Languages / Rust Left Out by End Date & $2$ & $4$ \\",
                      "\n".join(table.render(sampling_frame["_metadata"])))
        from agentcontext.steps import release
        limits = release.evidence_limits({}, sampling_frame["_metadata"], {}, None, {})
        self.assertIn("The filter's step `languages` left out Rust (crawl date 2026-08-01), "
                      "because the end date of the sampling window, 2026-09-22, does not cover "
                      "it.", "\n".join(limits))

    def test_a_retrieval_without_crawl_dates_leaves_no_language_out(self):
        """With a date as its end date, the download records no crawl date."""
        self._languages(None)
        code, err = self.run_main("--top-languages", "3")
        self.assertEqual(code, 0, err)
        self.assertEqual(self._languages_step()["values"],
                         {"top": 3, "languages": ["Go", "Python", "Rust"], "left_out": {}})

    def test_a_crawl_date_on_the_end_date_is_not_left_out(self):
        self.assertEqual(_mod.left_out_languages(
            {"retrieval_id": RID, "end_date": "2026-09-22",
             "crawl_dates": '{"C#": "2026-09-22", "Go": "2026-09-21", "Zig": null}'}),
            {"Go": "2026-09-21", "Zig": None})

    def test_crawl_dates_that_cannot_be_read_are_refused(self):
        self._languages(self.LEFT_OUT)
        ghs_cache.put_index_row({"retrieval_id": RID, "crawl_dates": "not json"}, self.cache)
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn(f"The retrieval cannot be read. The crawl dates of retrieval {RID} in the "
                      f"index are not JSON", err)
        self.assertFalse(self.output.exists())

    def test_a_config_key_the_filter_does_not_read_is_refused(self):
        self.config.write_text("filter_redirects: true\n")
        code, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("filter_redirects", err)

    def test_the_shipped_config_loads_and_reads_the_tracked_spdx_list(self):
        config = _mod.FilterConfig.from_yaml(HERE / "config" / "ghs_filter.yaml")
        self.assertTrue(config.exclude_forks and config.filter_gone and config.filter_duplicates)
        listed = _mod.read_spdx_list(HERE / config.spdx_list_path)
        self.assertRegex(listed["license_list_version"], r"^\d+\.\d+")
        self.assertIn("MIT License", listed["osi_approved"])


class TestTheSpdxList(unittest.TestCase):
    """A run in an empty data root reads the reviewed SPDX list, because the filter reads the
    list that the image contains and never the network. `--refresh-spdx` replaces the list
    before a run and names every approval that changed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = self.tmp / "spdx_licenses.json"
        self.saved = _mod.requests.get
        self.addCleanup(setattr, _mod.requests, "get", self.saved)
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def _tracked(self, licenses, version="3.28.0"):
        self.path.write_text(json.dumps({"license_list_version": version, "release_date": "",
                                         "read": "2026-03-14", "source": "",
                                         "osi_approved": licenses}))

    def _spdx_responds_with(self, licenses, version="3.29.0"):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"licenseListVersion": version, "releaseDate": "2026-09-16",
                        "licenses": [{"name": n, "isOsiApproved": True} for n in licenses]
                        + [{"name": "Not OSI", "isOsiApproved": False}]}
        _mod.requests.get = lambda url, timeout=None: Response()

    def test_a_list_without_a_license_or_a_version_is_refused(self):
        for listed, version in (([], "3.28.0"), (["MIT License"], "")):
            with self.subTest(listed=listed, version=version):
                self._tracked(listed, version)
                with self.assertRaises(ValueError):
                    _mod.read_spdx_list(self.path)
        with self.assertRaises(OSError):
            _mod.read_spdx_list(self.tmp / "absent.json")

    def test_a_refresh_writes_the_current_list_and_names_what_changed(self):
        self._tracked(["MIT License", "Apache License 2.0"])
        self._spdx_responds_with(["MIT License", "curl License"])
        self.assertEqual(_mod.refresh_spdx_list(self.path), 2)
        listed = _mod.read_spdx_list(self.path)
        self.assertEqual((listed["license_list_version"], listed["osi_approved"]),
                         ("3.29.0", ["MIT License", "curl License"]))
        self.assertIn("The SPDX list now marks curl License as OSI-approved", self.err.getvalue())
        self.assertIn("The SPDX list no longer marks Apache License 2.0 as OSI-approved",
                      self.err.getvalue())

    def test_the_list_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        """An empty list after a power loss would stop every later filter, because the filter
        reads the list."""
        self._tracked(["MIT License"])
        self._spdx_responds_with(["MIT License", "curl License"])
        before = self.path.stat().st_ino
        calls = synced_writes(lambda: _mod.refresh_spdx_list(self.path), self.tmp)
        after = self.path.stat().st_ino
        self.assertNotEqual(after, before)
        self.assertEqual(calls, [("file", after), ("rename", after, self.path.name),
                                 ("directory", True)])
        self.assertEqual(_mod.read_spdx_list(self.path)["license_list_version"], "3.29.0")

    def test_a_refresh_with_the_same_approvals_exits_0(self):
        self._tracked(["MIT License"])
        self._spdx_responds_with(["MIT License"])
        self.assertEqual(_mod.refresh_spdx_list(self.path), 0)
        self.assertEqual(_mod.read_spdx_list(self.path)["license_list_version"], "3.29.0")

    def test_a_refresh_that_cannot_fetch_or_reads_no_license_changes_nothing(self):
        self._tracked(["MIT License"])
        before = self.path.read_text()
        self._spdx_responds_with([])
        self.assertEqual(_mod.refresh_spdx_list(self.path), 1)

        def offline(url, timeout=None):
            raise _mod.requests.RequestException("offline")
        _mod.requests.get = offline
        self.assertEqual(_mod.refresh_spdx_list(self.path), 1)
        self.assertEqual(self.path.read_text(), before)


def _review(path: Path, rows: list) -> Path:
    """Write a file of reviewed licenses at `path`, with a row for each (license, decision)
    pair of `rows`."""
    import csv
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(_mod.LICENSE_COLUMNS), lineterminator="\n")
        writer.writeheader()
        for license_name, decision in rows:
            writer.writerow({"license": license_name, "spdx_id": "", "decision": decision,
                             "reason": "a reason", "source": "https://example.test/page",
                             "read": "2026-10-14"})
    return path


class TestTheWrites(unittest.TestCase):
    """A power loss shortly after a rename that reached the disk before the data would leave an
    empty file under the name. An empty sampling frame stops the classification, and an empty
    license table loses the counts that the next table compares with. Each file is synced under
    a temporary name before the rename, and the directory after the rename."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_each_file_is_on_disk_before_the_rename_and_the_rename_before_the_return(self):
        frame, table = self.tmp / "ghs_filtered.json", self.tmp / "ghs_licenses.csv"
        row = {"license": "MIT License", "repositories": 3, "osi_approved": True,
               "decision": "kept", "reviewed": "software", "examples": ["o/a"]}
        writes = ((frame, lambda: _mod.save_output(frame, {"final_count": 1}, [{"name": "a/a"}])),
                  (table, lambda: _mod.write_license_report(table, [row])))
        with contextlib.redirect_stderr(io.StringIO()):
            for path, write in writes:
                with self.subTest(path=path.name):
                    path.write_text("earlier")
                    before = path.stat().st_ino
                    calls = synced_writes(write, self.tmp)
                    after = path.stat().st_ino
                    self.assertNotEqual(after, before)
                    self.assertEqual(calls, [("file", after), ("rename", after, path.name),
                                             ("directory", True)])
        self.assertEqual(json.loads(frame.read_text())["items"], [{"name": "a/a"}])
        self.assertEqual(_mod.read_license_report(table)[0]["license"], "MIT License")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [frame.name, table.name])


class TestTheReviewedLicenses(unittest.TestCase):
    """config/licenses.csv records the review of a sampling frame's licenses. The license step
    records every license that it read, so that a person reads each license that is new to a
    retrieval."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.osi = {"MIT License", "SIL Open Font License 1.1", "zlib License"}

    def test_an_excluded_osi_approved_license_is_removed_and_an_unreviewed_one_kept(self):
        reviewed = _mod.load_reviewed_licenses(_review(self.tmp / "l.csv", [
            ("MIT License", "software"), ("SIL Open Font License 1.1", "excluded")]))
        items = [_row("a/a", 1, license="MIT License"),
                 _row("b/b", 2, license="SIL Open Font License 1.1"),
                 _row("c/c", 3, license="zlib License"), _row("d/d", 4, license="Other")]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            kept, result = _mod.step_licenses(items, _config(), self.osi, reviewed)
        self.assertEqual([i["name"] for i in kept], ["a/a", "c/c"])
        self.assertEqual(result.values["unreviewed"], ["zlib License"])
        self.assertIn("The license step kept 1 repositories under zlib License, which "
                      "config/licenses.csv does not cover", err.getvalue())
        table = {row["license"]: (row["decision"], row["reviewed"])
                 for row in result.values["licenses"]}
        self.assertEqual(table, {"MIT License": ("kept", "software"),
                                 "SIL Open Font License 1.1": ("excluded", "excluded"),
                                 "zlib License": ("kept", ""),
                                 "Other": ("not_osi_approved", "")})

    def test_the_examples_are_the_same_whatever_the_order_and_at_most_five(self):
        names = [f"o/r{i}" for i in range(12)]
        self.assertEqual(_mod.license_examples(names), _mod.license_examples(names[::-1]))
        self.assertEqual(len(_mod.license_examples(names)), _mod.EXAMPLES)

    def test_a_review_without_a_reason_or_with_a_license_twice_is_refused(self):
        path = _review(self.tmp / "l.csv", [("MIT License", "software"),
                                            ('BSD 3-Clause "New" or "Revised" License', "software"),
                                            ("BSD 3-Clause New or Revised License", "excluded")])
        with self.assertRaisesRegex(ValueError, "is listed a second time in line 4 of"):
            _mod.load_reviewed_licenses(path)
        path.write_text(path.read_text().replace("software", "maybe", 1))
        with self.assertRaisesRegex(ValueError, "is not one of"):
            _mod.load_reviewed_licenses(path)
        _review(path, [("MIT License", "software")])
        path.write_text(path.read_text().replace("a reason", ""))
        with self.assertRaisesRegex(ValueError, "no reason"):
            _mod.load_reviewed_licenses(path)
        _review(path, [("MIT License", "software")])
        path.write_text(path.read_text().replace(",2026-10-14", ","))
        with self.assertRaisesRegex(ValueError, "no reason, source or date"):
            _mod.load_reviewed_licenses(path)
        path.write_text("license,spdx_id,decision,reason,source\nMIT License,MIT,software,r,s\n")
        with self.assertRaisesRegex(ValueError, "lacks the columns read"):
            _mod.load_reviewed_licenses(path)

    def test_the_examples_are_the_five_smallest_by_the_hash_of_the_name(self):
        """The first five by name would put every example at the start of the alphabet."""
        import hashlib
        names = [f"owner{i}/repository" for i in range(30)]
        expected = sorted(names, key=lambda n: hashlib.sha256(n.encode()).hexdigest())[:5]
        self.assertEqual(_mod.license_examples(names), expected)
        self.assertNotEqual(expected, sorted(names)[:5])

    def test_the_table_records_each_licenses_count_in_the_table_it_replaces(self):
        report = self.tmp / "ghs_licenses.csv"
        row = {"license": "MIT License", "repositories": 3, "osi_approved": True,
               "decision": "kept", "reviewed": "software", "examples": ["o/a"]}
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.write_license_report(report, [row])
            _mod.write_license_report(report, [{**row, "repositories": 5},
                                               {**row, "license": "zlib License"}])
        rows = {r["license"]: r for r in _mod.read_license_report(report)}
        self.assertEqual((rows["MIT License"]["repositories"],
                          rows["MIT License"]["previous_repositories"]), ("5", "3"))
        self.assertEqual(rows["zlib License"]["previous_repositories"], "")

    def _check(self, table: list, review: list) -> list:
        report = self.tmp / "ghs_licenses.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            _mod.write_license_report(report, table)
        return _mod.check_licenses(report, _review(self.tmp / "l.csv", review), self.osi)

    def _row(self, name, decision="kept", approved=True):
        return {"license": name, "repositories": 2, "osi_approved": approved,
                "decision": decision, "reviewed": "", "examples": []}

    def test_the_check_names_a_kept_license_that_nobody_reviewed(self):
        findings = self._check([self._row("MIT License"), self._row("zlib License")],
                               [("MIT License", "software")])
        self.assertEqual(len(findings), 1)
        self.assertIn("zlib License", findings[0])

    def test_the_check_names_a_software_license_the_spdx_list_does_not_approve(self):
        findings = self._check([self._row("MIT License")],
                               [("MIT License", "software"), ("Vim License", "software")])
        self.assertEqual(len(findings), 1)
        self.assertIn("l.csv lists Vim License as software, but the SPDX list does not mark it "
                      "as OSI-approved", findings[0])

    def test_the_check_names_an_osi_approval_that_the_tracked_list_no_longer_gives(self):
        findings = self._check([self._row("MIT License"),
                                self._row("Python License 2.0.1", "excluded")],
                               [("MIT License", "software"), ("Python License 2.0.1", "excluded")])
        self.assertEqual(len(findings), 1)
        self.assertIn("The license table records the OSI approval of Python License 2.0.1 as "
                      "True, but the tracked SPDX list gives False", findings[0])

    def test_the_check_names_an_approval_the_tracked_list_gives_and_the_table_does_not(self):
        findings = self._check([self._row("MIT License"),
                                self._row("zlib License", "not_osi_approved", approved=False)],
                               [("MIT License", "software")])
        self.assertEqual(len(findings), 1)
        self.assertIn("The license table records the OSI approval of zlib License as False, but "
                      "the tracked SPDX list gives True", findings[0])

    def test_the_check_names_a_kept_license_that_the_review_excludes(self):
        findings = self._check([self._row("MIT License"), self._row("zlib License")],
                               [("MIT License", "software"), ("zlib License", "excluded")])
        self.assertEqual(len(findings), 1)
        self.assertIn("The sampling frame keeps 2 repositories under zlib License, which l.csv "
                      "does not list as software", findings[0])

    def test_a_sampling_frame_with_every_license_reviewed_has_no_finding(self):
        self.assertEqual(self._check([self._row("MIT License"),
                                      self._row("Other", "not_osi_approved", False)],
                                     [("MIT License", "software")]), [])


@unittest.skipIf(is_stub("yaml"), "yaml is not installed")
class TestTheLicenseCheck(_RunCase):
    """`--check-licenses` reads the table that a run wrote. It exits 0 when every license is
    reviewed, 2 when a license needs a person's review and 1 when there is no table."""

    def setUp(self):
        super().setUp()
        self.spdx = self.tmp / "spdx_licenses.json"
        self.spdx.write_text(json.dumps(
            {"license_list_version": "3.28.0", "release_date": "", "read": "", "source": "",
             "osi_approved": ["MIT License", "zlib License", "SIL Open Font License 1.1"]}))
        # A run reads the list beside the config and never fetches a list from the network.
        saved = _mod.requests.get
        self.addCleanup(setattr, _mod.requests, "get", saved)

        def refused(url, timeout=None):
            raise AssertionError(f"the filter requested {url}")
        _mod.requests.get = refused

    def _run(self, *extra):
        data = self.tmp / "data"
        data.mkdir(exist_ok=True)
        err = io.StringIO()
        with mock.patch.object(_mod, "SAMPLING_DIR", data), contextlib.redirect_stderr(err):
            code = _mod.main(["--config", str(self.config), "--cache-dir", str(self.cache),
                              "--pointer", str(self.pointer), "--output", str(self.output),
                              *extra])
        return code, err.getvalue(), data

    def test_a_run_writes_the_table_and_the_check_reads_it(self):
        review = _review(self.tmp / "licenses.csv", [("MIT License", "software"),
                                                     ("SIL Open Font License 1.1", "excluded")])
        self.config.write_text(f"filter_activity: false\nlicenses_path: {review}\n"
                               f"spdx_list_path: {self.spdx}\n")
        self.retrieval([_ghs("a/a", 1), _ghs("b/b", 2), _ghs("c/c", 3)],
                       [{"ghsName": "a/a", **_found("a/a", 11)},
                        {"ghsName": "b/b", **_found("b/b", 12, license="zlib License")},
                        {"ghsName": "c/c", **_found("c/c", 13,
                                                    license="SIL Open Font License 1.1")}])
        code, err, data = self._run()
        self.assertEqual(code, 0, err)
        frame = json.loads(self.output.read_text())
        # The review in the file excludes the font license, which the SPDX list marks as
        # OSI-approved.
        self.assertEqual([i["name"] for i in frame["items"]], ["a/a", "b/b"])
        step = next(s for s in frame["_metadata"]["stages"] if s["key"] == "licenses")
        self.assertEqual(step["values"]["spdx_list_version"], "3.28.0")
        self.assertEqual(step["values"]["review_sha256"],
                         __import__("hashlib").sha256(review.read_bytes()).hexdigest())
        table = {r["license"]: r for r in _mod.read_license_report(data / "ghs_licenses.csv")}
        self.assertEqual(set(table), {"MIT License", "zlib License", "SIL Open Font License 1.1"})
        self.assertEqual(table["zlib License"]["reviewed"], "")
        code, err, _ = self._run("--check-licenses")
        self.assertEqual(code, 2, err)
        self.assertIn("zlib License", err)
        _review(review, [("MIT License", "software"), ("zlib License", "software"),
                         ("SIL Open Font License 1.1", "excluded")])
        self.assertEqual(self._run("--check-licenses")[0], 0)
        # A review that no longer excludes a license that the sampling frame excludes requires
        # another run of the filter.
        _review(review, [("MIT License", "software"), ("zlib License", "software"),
                         ("SIL Open Font License 1.1", "software")])
        code, err, _ = self._run("--check-licenses")
        self.assertEqual(code, 2, err)
        self.assertIn("The sampling frame excludes 1 repositories under SIL Open Font License 1.1, "
                      "which licenses.csv no longer excludes", err)

    def test_a_dry_run_writes_no_table(self):
        review = _review(self.tmp / "licenses.csv", [("MIT License", "software")])
        self.config.write_text(f"filter_activity: false\nlicenses_path: {review}\n"
                               f"spdx_list_path: {self.spdx}\n")
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **_found("a/a", 11)}])
        code, err, data = self._run("--dry-run")
        self.assertEqual(code, 0, err)
        self.assertFalse((data / "ghs_licenses.csv").exists())

    def test_a_run_without_the_license_filter_still_writes_the_license_table(self):
        """The runner requires the table that the step declares."""
        self.config.write_text("filter_activity: false\nfilter_licenses: false\n")
        self.retrieval([_ghs("a/a", 1)], [{"ghsName": "a/a", **_found("a/a", 11)}])
        code, err, data = self._run()
        self.assertEqual(code, 0, err)
        self.assertEqual([r["license"] for r in _mod.read_license_report(
            data / "ghs_licenses.csv")], ["MIT License"])

    def test_a_table_without_a_license_cannot_be_checked(self):
        data = self.tmp / "data"
        data.mkdir()
        (data / "ghs_licenses.csv").write_text(",".join(_mod.REPORT_COLUMNS) + "\n")
        self.config.write_text(f"filter_activity: false\nspdx_list_path: {self.spdx}\n")
        code, err, _ = self._run("--check-licenses")
        self.assertEqual(code, 1, err)
        self.assertIn("names no license", err)

    def test_without_a_table_the_check_cannot_check(self):
        self.config.write_text("filter_activity: false\n")
        code, err, _ = self._run("--check-licenses")
        self.assertEqual(code, 1, err)

    def test_a_flag_that_the_check_does_not_have_is_no_finding(self):
        """Plain argparse would exit 2 for an unknown flag, and 2 is the exit code that the
        check returns for a license to review."""
        with self.assertRaises(SystemExit) as stop:
            self._run("--check-licences")
        self.assertEqual(stop.exception.code, 1)


def _alternatives(pattern: str) -> list:
    """Split a regular expression into its top-level alternatives at each `|` outside any
    group or character class."""
    parts, depth, start, escaped, in_class = [], 0, 0, False, False
    for i, char in enumerate(pattern):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append(pattern[start:i])
            start = i + 1
    return parts + [pattern[start:]]


@unittest.skipIf(is_stub("yaml"), "yaml is not installed")
class TestTheEducationalRules(unittest.TestCase):
    """The tests check the shipped patterns of step 7 against the hand labels of 2026-10-15 in
    data/sampling/educational/labels.csv, so that every alternative rests on labeled evidence."""

    def setUp(self):
        import csv
        self.config = _mod.FilterConfig.from_yaml(HERE / "config" / "ghs_filter.yaml")
        with open(_mod.SAMPLING_DIR / "educational" / "labels.csv", newline="",
                  encoding="utf-8") as f:
            self.labels = list(csv.DictReader(f))

    def _item(self, name, topics=()):
        return {**_row(name, 1), "name": name, "topics": list(topics)}

    def _removed(self, labels, topic, name):
        found = []
        for row in labels:
            topics = row["topics"].split()
            if (topic and any(re.search(topic, t) for t in topics)) or (
                    name and re.search(name, row["name"].split("/", 1)[1], re.I)):
                found.append(row)
        return found

    def test_every_alternative_removes_a_labeled_educational_repository(self):
        for field in ("educational_topic_regex", "educational_name_regex"):
            for alternative in _alternatives(getattr(self.config, field)):
                with self.subTest(field=field, alternative=alternative):
                    topic = alternative if field == "educational_topic_regex" else None
                    name = alternative if field == "educational_name_regex" else None
                    self.assertTrue(any(r["label"] == "educational" for r in
                                        self._removed(self.labels, topic, name)))

    def test_nine_in_ten_labeled_removals_are_educational(self):
        removed = self._removed(self.labels, self.config.educational_topic_regex,
                                self.config.educational_name_regex)
        share = sum(r["label"] == "educational" for r in removed) / len(removed)
        self.assertGreaterEqual(share, 0.9)

    def test_software_that_the_earlier_patterns_removed_stays(self):
        """`\\blearning-` would match inside machine-learning-library, and `course` would match
        inside concourse. A pattern that read the owner part of the name would remove
        MercuryWorkshop's software."""
        kept = [self._item("davisking/dlib", ["machine-learning-library"]),
                self._item("concourse/concourse", ["continuous-integration"]),
                self._item("discourse/discourse", ["forum"]),
                self._item("MercuryWorkshop/scramjet"),
                self._item("Nocsy-Workshop/nocsy"),
                self._item("sc-workshop/router"),
                self._item("ls1intum/Artemis", ["education"])]
        out, _ = _mod.step_educational(kept, self.config)
        self.assertEqual([i["name"] for i in out], [i["name"] for i in kept])

    def test_tutorials_courses_exercises_and_example_code_go(self):
        gone = [self._item("yangshun/tech-interview-handbook"),
                self._item("genkit-ai/samples"),
                self._item("emilybache/Tennis-Refactoring-Kata"),
                self._item("angular-university/angular-course"),
                self._item("o/notes", ["interview-prep"])]
        out, _ = _mod.step_educational(gone, self.config)
        self.assertEqual(out, [])


class TestCopies(unittest.TestCase):
    """Rows of different repositories at one head commit, such as zzzeek/sqlalchemy, a personal
    copy created first, and sqlalchemy/sqlalchemy, all stay in the sampling frame, because the
    classification decides which of them is the project."""

    def test_the_repositories_at_one_head_commit_all_stay(self):
        items = [_row("zzzeek/sqlalchemy", 1, github_id=10289171,
                      createdAt="2013-05-25T00:00:00Z", headSha="b" * 40),
                 _row("sqlalchemy/sqlalchemy", 2, github_id=159271175,
                      createdAt="2018-11-27T00:00:00Z", headSha="b" * 40),
                 _row("o/other", 3, headSha="c" * 40)]
        frame, results = _mod.run_steps(items, _config(filter_activity=False,
                                                       filter_licenses=False),
                                        set(), created_max=CREATED_MAX)
        self.assertEqual([i["name"] for i in frame],
                         ["zzzeek/sqlalchemy", "sqlalchemy/sqlalchemy", "o/other"])
        self.assertEqual(sum(result.removed for result in results), 0)


class TestTheTrackedReview(unittest.TestCase):
    """In config/licenses.csv as tracked, every license reviewed as software is OSI-approved in
    the tracked SPDX list, and every row records the reason, the page that it was read on and
    the date."""

    def test_every_software_license_is_osi_approved_and_every_row_is_grounded(self):
        reviewed = _mod.load_reviewed_licenses(HERE / "config" / "licenses.csv")
        osi = set(_mod.read_spdx_list(HERE / "config" / "spdx_licenses.json")["osi_approved"])
        for row in reviewed.values():
            with self.subTest(license=row["license"]):
                self.assertTrue(row["source"].startswith("https://"))
                if row["decision"] == "software":
                    self.assertTrue(_mod.is_osi_approved(row["license"], osi))


@unittest.skipIf(is_stub("yaml"), "yaml is not installed")
class TestTheStepOfTheRunner(unittest.TestCase):
    """The step `repository_filtering` of run_sampling.py declares every file that this script
    reads and writes, as the shipped config names them. The runner runs the step again when a
    file that the step reads changes, and only when the step declares that file. The tests read
    the config with yaml, so they are here and not in the suite of the sampling runner, which
    runs without site packages."""

    def setUp(self):
        import yaml
        self.filtering = yaml.safe_load((HERE / "config" / "ghs_filter.yaml").read_text())
        steps = importlib.import_module("run_sampling").STEPS
        self.step = next(s for s in steps if s.key == "repository_filtering")

    def test_the_filter_config_names_the_file_the_step_declares(self):
        self.assertEqual(self.filtering["output_path"], self.step.produces[0])

    def test_the_filter_step_declares_every_file_the_filter_reads_and_writes(self):
        """The filter decides by the review of the licenses, the SPDX list and the config, and
        it writes the sampling frame and the license table."""
        for read in ("config/ghs_filter.yaml", self.filtering["licenses_path"],
                     self.filtering["spdx_list_path"]):
            with self.subTest(read=read):
                self.assertIn(read, self.step.code_inputs)
        for written in (self.filtering["output_path"], self.filtering["license_report_path"]):
            with self.subTest(written=written):
                self.assertIn(written, self.step.produces)


if __name__ == "__main__":
    unittest.main()
