"""
Tests that the layout of the data trees in git is the layout that the steps write.

Background:
  - The collected, validated and excluded trees are under `data/`, and each step's list is at
    the root of the tree that it lists: `data/collected/repos.csv`,
    `data/collected/clone_failed.csv` and `data/validated/repos.csv`. The three lists are
    tracked, and the repository directories of the trees around them are not. A `.gitignore`
    can state this layout only as `dir/*` plus a negation. An entry of the form `dir/` excludes
    the directory itself. Git then never descends into the directory, and a negation below it
    has no effect. Both forms ignore the repository directories, and only `dir/*` keeps the
    lists in the index.
  - No other check detects the wrong form. A tracked list stays tracked, and `git status` stays
    clean while every other suite passes. The loss appears at the next `git clean -xdf`, or on
    the replication machine, where the file is regenerated and not restored.
  - The same test applies to one entry under `output/`: the tracked archive of the last
    collection's unmatched attributions. A rule ignores the extracted CSV, and that rule must
    not match the archive.

Querying git:
  - `git check-ignore` consults the index by default, and reports every tracked path as not
    ignored whatever the patterns say. Queried that way, these assertions would pass with the
    negations deleted. `--no-index` tests the patterns instead.
  - `-v` changes the exit code as well as the output: A path that a negation matches exits 0
    with `-v`, the same as a path that is ignored. The result therefore comes from `-q`, and
    `-v` runs again only to name the rule that decided a failing path.
  - `git ls-files` tests tracking, which is separate from matching.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_data_layout -v
"""

import importlib
import subprocess
import sys
import unittest
from pathlib import Path
from agentcontext import REPO_ROOT, STAGE_DIR

_collect = importlib.import_module("agentcontext.steps.repository_cloning")
_validate = importlib.import_module("agentcontext.steps.collected_file_cleanup")
_unmatched = importlib.import_module("agentcontext.checks.unmatched")
repos = importlib.import_module("agentcontext.repos")


def _stage_path(relative: str) -> str:
    """A path that the steps write relative to the stage directory, as git names it relative to
    the repository root."""
    return (STAGE_DIR / relative).relative_to(REPO_ROOT).as_posix()


# The paths come from the constants that the steps write through rather than being typed
# again, so that renaming a tree changes the paths that these tests check.
VALIDATED_CSV = _stage_path(_validate.VALIDATED_CSV)
COLLECTED_CSV = _stage_path(_validate.COLLECTED_CSV)
CLONE_FAILED_CSV = _stage_path(_collect.FAIL_CSV_NAME)
# Both archives are beside the tree that they restore rather than inside it, so that
# `data/collected/*` does not match them.
COLLECTED_ARCHIVE = _stage_path(_validate.COLLECTED_DIR + ".7z")
EXCLUDED_ARCHIVE = _stage_path(_validate.EXCLUDED_DIR + ".7z")

# One repository directory in each tree, named as the collection names them. The
# directories need not exist, because the tests check what the ignore rules match.
_SAMPLE_REPO = repos.repo_name_to_dir("owner/repo")
VALIDATED_REPO_DIR = _stage_path(f"{_validate.VALIDATED_DIR}/{_SAMPLE_REPO}")
COLLECTED_REPO_DIR = _stage_path(f"{_validate.COLLECTED_DIR}/{_SAMPLE_REPO}")
EXCLUDED_REPO_DIR = _stage_path(f"{_validate.EXCLUDED_DIR}/{_SAMPLE_REPO}")

# Everything under data/ that git tracks: the three lists. The repository directories
# around them are restored from the archives.
TRACKED = (VALIDATED_CSV, CLONE_FAILED_CSV, COLLECTED_CSV)

# The unmatched attributions of the last collection, under output/ with the day on which the
# collection started in the name, as the git pathspec that matches the archive for any day.
TRACKED_UNMATCHED = _stage_path(f"output/{_unmatched.TRACKED_GLOB}.7z")


def _git(*args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True,
                          cwd=str(REPO_ROOT))


def _inside_checkout() -> bool:
    result = _git("rev-parse", "--is-inside-work-tree")
    return result.returncode == 0 and result.stdout.strip() == "true"


class _LayoutCase(unittest.TestCase):
    """Skipped outside a checkout, where there are no ignore rules and no index to query."""

    @classmethod
    def setUpClass(cls):
        if not _inside_checkout():
            raise unittest.SkipTest("not a git checkout, so there is no .gitignore to test")

    def matched_by_the_ignore_rules(self, path: str) -> bool:
        """Whether the patterns ignore a path, with the index left out of the query."""
        result = _git("check-ignore", "-q", "--no-index", path)
        self.assertIn(result.returncode, (0, 1),
                      f"git check-ignore failed for {path}: {result.stderr}")
        return result.returncode == 0

    def deciding_rule(self, path: str) -> str:
        """The rule that decided a path, for a failure message that names where to look."""
        result = _git("check-ignore", "-v", "--no-index", path)
        return result.stdout.strip() or "no ignore rule matched"

    def assert_not_ignored(self, path: str) -> None:
        self.assertFalse(
            self.matched_by_the_ignore_rules(path),
            f"{path} is tracked, and the ignore rules match it. It stays in the index "
            f"until it is removed, and is not added again after that "
            f"({self.deciding_rule(path)})")

    def assert_ignored(self, path: str) -> None:
        self.assertTrue(
            self.matched_by_the_ignore_rules(path),
            f"{path} is collected data that an archive restores, and the ignore rules do "
            f"not match it")


class TestTheTrackedListsAreNotIgnored(_LayoutCase):
    """The paths that an ignore entry of the form `dir/` would drop without a warning."""

    def test_the_validated_list_is_not_ignored(self):
        self.assert_not_ignored(VALIDATED_CSV)

    def test_the_clone_failure_list_is_not_ignored(self):
        self.assert_not_ignored(CLONE_FAILED_CSV)

    def test_the_collected_archive_is_not_ignored(self):
        """The archive that restores the tree is beside `data/collected/`, not in it, and the
        rule of the tree must therefore not match the archive."""
        self.assert_not_ignored(COLLECTED_ARCHIVE)

    def test_the_excluded_archive_is_not_ignored(self):
        self.assert_not_ignored(EXCLUDED_ARCHIVE)


class TestTheRepositoryDirectoriesAreIgnored(_LayoutCase):
    """The paths that the ignore rules must match, so that a rule that ignored nothing would
    fail the suite."""

    def test_the_collected_list_is_not_ignored(self):
        self.assert_not_ignored(COLLECTED_CSV)

    def test_a_validated_repository_directory_is_ignored(self):
        self.assert_ignored(VALIDATED_REPO_DIR)

    def test_a_collected_repository_directory_is_ignored(self):
        self.assert_ignored(COLLECTED_REPO_DIR)

    def test_an_excluded_repository_directory_is_ignored(self):
        self.assert_ignored(EXCLUDED_REPO_DIR)

    def test_a_file_inside_a_repository_directory_is_ignored(self):
        """`dir/*` matches the directory, and git does not descend into it. The rule also has
        to match the files below the directory."""
        self.assert_ignored(f"{VALIDATED_REPO_DIR}/CLAUDE.md")
        self.assert_ignored(f"{COLLECTED_REPO_DIR}/.claude/settings.json")


class TestTheIndexContainsOnlyTheTrackedLists(_LayoutCase):
    """The tests check tracking separately, because matching no rule is not the same as being
    tracked."""

    def test_every_tracked_path_is_in_the_index(self):
        for path in TRACKED:
            with self.subTest(path=path):
                result = _git("ls-files", "--error-unmatch", path)
                self.assertEqual(result.returncode, 0,
                                 f"{path} is not tracked: {result.stderr.strip()}")

    def test_nothing_else_under_data_is_tracked(self):
        """The opposite accident: repository directories added to the index with force,
        which no ignore rule undoes."""
        result = _git("ls-files", "--", _stage_path("data"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(result.stdout.splitlines()), sorted(TRACKED))


class TestTheTrackedUnmatchedAttributions(_LayoutCase):
    """The archive that the rows of not_ai_commits.csv are checked against is tracked, and no
    ignore rule matches it. The CSV that it extracts to is ignored, as is
    `output/all_ai_commits.csv` beside its archive. An ignore rule for the CSV that also
    matched the archive would leave the archive in the index until it was removed, and the suite
    that reads the archive would then fail in a clean checkout rather than here."""

    def tracked(self) -> list:
        result = _git("ls-files", "--", TRACKED_UNMATCHED)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_one_archive_is_tracked_and_no_rule_ignores_it(self):
        paths = self.tracked()
        self.assertEqual(len(paths), 1, f"{TRACKED_UNMATCHED} matches {paths} in the index")
        self.assert_not_ignored(paths[0])

    def test_the_csv_it_extracts_to_is_ignored(self):
        for path in self.tracked():
            with self.subTest(path=path):
                self.assert_ignored(path.removesuffix(".7z"))


class TestTheCheckIsNotVacuous(_LayoutCase):
    """Both helpers have to be able to return either result, or every test above would pass
    whatever the rules are."""

    def test_the_ignore_check_can_return_either_result(self):
        self.assertTrue(self.matched_by_the_ignore_rules(COLLECTED_REPO_DIR))
        self.assertFalse(self.matched_by_the_ignore_rules(VALIDATED_CSV))

    def test_a_path_no_rule_mentions_is_not_ignored(self):
        self.assertFalse(self.matched_by_the_ignore_rules(
            _stage_path("0_README.md")))


# Each stage's default data paths, printed by a child process under another data root. A module
# of a stage is loaded by its path, because a name that starts with a digit is not an
# identifier. `1_classify_repos.py` imports the packages of its stage, which CI does not install
# for this suite, and `2_evaluate_labels.py` imports `1_classify_repos.py`. The suites of the two
# scripts therefore check their defaults (`TestDataRoot` in test_classify_repos.py and
# test_evaluate_labels.py).
_DEFAULTS = """
import importlib.util, sys
from pathlib import Path
root = Path(sys.argv[1])
def load(rel, name):
    sys.path.insert(0, str(root / Path(rel).parent))
    spec = importlib.util.spec_from_file_location(name, root / rel)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
import agentcontext
cache = load("1_sampling/ghs_cache.py", "ghs_cache")
compare = load("1_sampling/3_compare_retrievals.py", "compare")
download = load("1_sampling/1_download_ghs.py", "download")
labels = load("2_classification/label_cache.py", "label_cache")
shared = load("4_data-analysis/_shared.py", "_shared")
from agentcontext.steps import engineered_project_selection, release
for path in (agentcontext.STAGE_DIR, cache.CACHE_DIR, cache.POINTER, compare.CHANGES,
             compare.SUMMARY, download.REPORT, labels.CACHE_DIR,
             shared.output_dir(root / "4_data-analysis" / "rq2" / "x.py"),
             shared.DATA_DIR, engineered_project_selection.DEFAULT_OUTPUT,
             release.DEFAULT_GHS_JSON, release.DEFAULT_ANNOTATED_CSV):
    print(path)
"""


class TestEveryStageWritesUnderTheDataRoot(unittest.TestCase):
    """Each default data path of a stage's scripts is under the data root that the container
    names in `AGENTCONTEXT_DATA_ROOT`, not under the directory of the scripts' code."""

    def test_every_default_data_path_is_under_the_data_root_that_the_variable_names(self):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENTCONTEXT_DATA_ROOT=tmp, MPLBACKEND="Agg")
            env.pop("DATA_COLLECTION_OUTPUT", None)
            env.pop("ANALYSIS_OUTPUT_DIR", None)
            result = subprocess.run([sys.executable, "-c", _DEFAULTS, str(REPO_ROOT)],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            paths = [Path(line) for line in result.stdout.splitlines()]
            self.assertEqual(len(paths), 12)
            for path in paths:
                with self.subTest(path=str(path)):
                    self.assertTrue(path.is_relative_to(Path(tmp).resolve()), path)


if __name__ == "__main__":
    unittest.main()
