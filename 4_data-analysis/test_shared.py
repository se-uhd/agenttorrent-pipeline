"""
Tests for _shared.py, the one module that the analysis scripts import.

Strategy:
  - The constants are compared with the code block that the section "Constants" of CLAUDE.md
    states and that the test executes, so that the two cannot diverge.
  - The context file categories are checked in both directions against the heuristics and
    against every `context_file` value of the output CSVs on disk, the collection stage's and
    the fixture's.
  - The two environment variables are tested in a subprocess, which is how a script reads
    them.
  - The loader of the commit CSV needs pandas and 7zz, and its cases skip without them.
  - The escaping of text for LaTeX is checked against each character that LaTeX reads as
    markup.
  - The Benjamini-Hochberg adjustment and the mechanism-by-tool counts are checked against
    values written out by hand. The adjustment needs statsmodels, the adjustment and the
    counts need pandas, and their cases skip without them.

Requirements: Python 3.14 (stdlib only for every case but the loader's, the adjustment's and
the counts')
Run with:  python3 -m unittest test_shared -v
"""

import csv
import importlib
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentcontext import SEVEN_ZIP, STAGE_DIR

sys.path.insert(0, str(Path(__file__).resolve().parent))

_shared = importlib.import_module("_shared")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
FIXTURE = HERE / "testdata" / "output"
# Where the collection stage writes its output CSVs, from the package rather than from the
# module under test.
STAGE_OUTPUT = STAGE_DIR / "output"


def _has_pandas() -> bool:
    try:
        import pandas  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


class TestConstantsMatchClaudeMd(unittest.TestCase):
    """The section "Constants" of CLAUDE.md states the constants in a Python code block, and the
    module defines them. The test executes the block and fails when either changes without the
    other."""

    def _stated(self) -> dict:
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        section = text.split("## Constants", 1)[1].split("\n## ", 1)[0]
        block = re.search(r"```python\n(.*?)```", section, re.S)
        self.assertIsNotNone(block, "the section Constants of CLAUDE.md has no python block")
        namespace: dict = {}
        exec(block.group(1), namespace)
        return namespace

    def test_the_four_constants_are_the_stated_ones(self):
        stated = self._stated()
        for name in ("TOOLS", "TOOL_LABELS", "MECHANISMS", "MECHANISM_LABELS"):
            with self.subTest(name=name):
                self.assertEqual(getattr(_shared, name), stated[name])

    def test_the_specific_tools_leave_out_no_specific_tool(self):
        self.assertEqual(_shared.SPECIFIC_TOOLS,
                         [t for t in _shared.TOOLS if t != "no_specific_tool"])

    def test_the_section_names_the_module(self):
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        section = text.split("## Constants", 1)[1].split("\n## ", 1)[0]
        self.assertIn("4_data-analysis/_shared.py", section)


class TestTheCategories(unittest.TestCase):

    def _heuristics_paths(self) -> set:
        with open(_shared.CONFIG_PATTERNS_CSV, newline="", encoding="utf-8") as f:
            return {row["path"] for row in csv.DictReader(f) if row["mechanism"] == "context_file"}

    def test_every_heuristics_context_file_has_a_category_and_nothing_else_does(self):
        self.assertEqual(set(_shared.CATEGORY_OF_HEURISTIC_PATH), self._heuristics_paths())

    def test_the_categories_are_the_labels_in_figure_order(self):
        self.assertEqual(tuple(_shared.CONTEXT_FILE_CATEGORIES), _shared.CATEGORY_ORDER)
        self.assertEqual(set(_shared.CATEGORY_OF_HEURISTIC_PATH.values()), set(_shared.CATEGORY_ORDER))

    def test_every_category_has_a_color(self):
        for label in _shared.CATEGORY_ORDER:
            self.assertIn(label, _shared.COLORS)

    def _no_path_rule_matches(self, table: Path) -> list:
        # Both CSVs are tracked. A checkout without one is broken rather than partial, and a
        # default that points nowhere fails rather than skips.
        self.assertTrue(table.is_file(), f"{table} is tracked, and this checkout does not contain it")
        with open(table, newline="", encoding="utf-8") as f:
            return sorted((row["repo_name"], row["context_file"]) for row in csv.DictReader(f)
                          if _shared.context_file_category(row["context_file"]) is None)

    def test_every_published_context_file_is_categorized(self):
        """A path pattern categorizes every file except the files that a configuration names,
        which get a category of their own. A row outside both is a path that no heuristic
        covers."""
        named = self._no_path_rule_matches(STAGE_OUTPUT / "context-files.csv")
        from agentcontext import paths
        validated = STAGE_OUTPUT.parent / "data/validated"
        for repo, path in named:
            root = validated / repo.replace('/', '§')
            if root.is_dir():
                self.assertIn(path, {name for name, _ in paths.declared_context_files(root)}, (repo, path))
        for _repo, path in named:
            self.assertEqual(_shared.context_file_row_category(path),
                             _shared.NAMED_IN_CONFIGURATION)

    def test_every_fixture_context_file_is_categorized(self):
        self.assertEqual(self._no_path_rule_matches(FIXTURE / "context-files.csv"), [])

    def test_the_rules_the_heuristics_states(self):
        cases = {"CLAUDE.md": "CLAUDE.md", "docs/CLAUDE.md": "CLAUDE.md",
                 "CLAUDE.local.md": "CLAUDE.md", "AGENTS.override.md": "AGENTS.md",
                 "REVIEW.md": "REVIEW.md", "nested/REVIEW.md": None, "review.md": None,
                 "GEMINI.md": "GEMINI.md", ".cursorrules": ".cursorrules",
                 ".github/copilot-instructions.md": "Copilot Instructions",
                 ".github/COPILOT-INSTRUCTIONS.md": "Copilot Instructions",
                 ".github/instructions/api.instructions.md": "Copilot Instructions",
                 "pkg/.cursor/BUGBOT.md": "Cursor BUGBOT",
                 ".gemini/styleguide.md": "Gemini Style Guide",
                 "README.md": None, ".github/instructions/notes.md": None,
                 "docs/copilot-instructions.md": None, "opencode.json": None,
                 ".opencode/opencode.jsonc": None, ".gemini/settings.json": None}
        for path, label in cases.items():
            with self.subTest(path=path):
                self.assertEqual(_shared.context_file_category(path), label)

    def test_the_copilot_instructions_below_the_root(self):
        """The `nested:` rows of Copilot CLI's places below the root give a package's
        instructions the category of the root instructions. A row with that match mode matches
        only the place below the root."""
        for path in ("pkg/.github/copilot-instructions.md",
                     "a/b/.github/instructions/api/react.instructions.md"):
            with self.subTest(path=path):
                self.assertEqual(_shared.context_file_category(path), "Copilot Instructions")
        nested = _shared._matcher({"path": ".github/copilot-instructions.md",
                                   "match_mode": "nested:file"})
        self.assertTrue(nested("pkg/.github/copilot-instructions.md"))
        self.assertFalse(nested(".github/copilot-instructions.md"))
        tree = _shared._matcher({"path": ".github/instructions",
                                 "match_mode": "nested:tree_suffix:.instructions.md"})
        self.assertTrue(tree("pkg/.github/instructions/a.instructions.md"))
        self.assertFalse(tree(".github/instructions/a.instructions.md"))

    def test_a_path_matches_at_most_one_category(self):
        for path in ("CLAUDE.md", ".github/copilot-instructions.md", ".cursor/BUGBOT.md"):
            matching = [label for label, m in _shared.CONTEXT_FILE_CATEGORIES.items() if m(path)]
            self.assertEqual(len(matching), 1, path)


class TestThePaths(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _data_dir_seen(self, env_value) -> str:
        env = dict(os.environ)
        env.pop("DATA_COLLECTION_OUTPUT", None)
        if env_value is not None:
            env["DATA_COLLECTION_OUTPUT"] = env_value
        return subprocess.run([sys.executable, "-c", "import _shared; print(_shared.DATA_DIR)"],
                              cwd=str(HERE), env=env, capture_output=True, text=True,
                              check=True).stdout.strip()

    def test_the_csvs_are_read_from_the_stages_output_by_default(self):
        """The default is compared with the stage's directory written out, not with
        `DEFAULT_DATA_DIR`, which is the value under test. The fixture runs set the variable,
        and no other case would detect a misspelled default."""
        self.assertEqual(self._data_dir_seen(None), str(STAGE_OUTPUT))
        self.assertTrue((STAGE_OUTPUT / "repos.csv").is_file())

    def test_the_environment_points_the_scripts_at_other_csvs(self):
        self.assertEqual(self._data_dir_seen(str(self.tmp)), str(self.tmp))

    def test_a_script_writes_under_the_data_root_by_default(self):
        """An analysis writes under the analysis stage's directory, in a directory with the name
        of the script's own directory."""
        env = dict(os.environ, AGENTCONTEXT_DATA_ROOT=str(self.tmp))
        env.pop("ANALYSIS_OUTPUT_DIR", None)
        seen = subprocess.run([sys.executable, "-c",
                               "import _shared; print(_shared.output_dir('rq2/x.py'))"],
                              cwd=str(HERE), env=env, capture_output=True, text=True,
                              check=True).stdout.strip()
        self.assertEqual(seen, str(self.tmp.resolve() / "analysis" / "rq2"))

    def test_under_the_override_the_directory_name_is_kept(self):
        env = dict(os.environ, ANALYSIS_OUTPUT_DIR=str(self.tmp))
        seen = subprocess.run([sys.executable, "-c",
                               "import _shared; print(_shared.output_dir('rq3/x.py'))"],
                              cwd=str(HERE), env=env, capture_output=True, text=True,
                              check=True).stdout.strip()
        self.assertEqual(seen, str(self.tmp / "rq3"))
        self.assertTrue((self.tmp / "rq3").is_dir())


def _has_matplotlib() -> bool:
    try:
        import matplotlib  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


@unittest.skipUnless(_has_matplotlib(), "matplotlib is not installed")
class TestSaveFigure(unittest.TestCase):
    """`save_figure` writes every figure of the analysis stage."""

    def test_the_pdf_embeds_truetype_fonts_and_pdf_fonttype_is_restored(self):
        """The PDF embeds TrueType fonts rather than matplotlib's default Type 3 fonts, and
        `pdf.fonttype` has the same value after the call as before, so that a figure written
        later without `save_figure` gets the default."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        default = matplotlib.rcParams["pdf.fonttype"]
        with tempfile.TemporaryDirectory() as tmp:
            fig, ax = plt.subplots()
            ax.set_title("Configuration Mechanisms")
            _shared.save_figure(fig, Path(tmp) / "figure.pdf")
            plt.close(fig)
            data = (Path(tmp) / "figure.pdf").read_bytes()
        self.assertIn(b"/FontFile2", data)
        self.assertNotIn(b"/Type3", data)
        self.assertEqual(matplotlib.rcParams["pdf.fonttype"], default)


@unittest.skipUnless(_has_pandas() and shutil.which(SEVEN_ZIP), "pandas and 7zz are needed")
class TestReadCommitsCsv(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_csv_is_read_when_there(self):
        (self.tmp / "all_ai_commits.csv").write_text("repo_name,commit_sha,ai_tool\no/r,abc,Claude\n")
        self.assertEqual(len(_shared.read_commits_csv(self.tmp)), 1)

    def test_the_archive_beside_it_is_extracted(self):
        shutil.copy(FIXTURE / "all_ai_commits.csv.7z", self.tmp / "all_ai_commits.csv.7z")
        commits = _shared.read_commits_csv(self.tmp)
        self.assertGreater(len(commits), 1000)
        self.assertIn("matched_heuristics", commits.columns)

    def test_neither_is_refused(self):
        with self.assertRaises(SystemExit):
            _shared.read_commits_csv(self.tmp)

    def test_split_archive_reads_all_rows_and_refuses_a_missing_volume(self):
        content = (FIXTURE / 'all_ai_commits.csv.7z').read_bytes()
        size = (len(content) + 2) // 3
        for number, start in enumerate(range(0, len(content), size), 1):
            (self.tmp / f'all_ai_commits.csv.7z.{number:03}').write_bytes(content[start:start + size])
        expected = _shared.read_commits_csv(FIXTURE)
        actual = _shared.read_commits_csv(self.tmp)
        self.assertEqual(actual.to_dict('records'), expected.to_dict('records'))
        (self.tmp / 'all_ai_commits.csv.7z.002').unlink()
        with self.assertRaises(SystemExit):
            _shared.read_commits_csv(self.tmp)

    def _tracked_archive(self) -> int:
        """The commit archive of the fixture in a git repository at the temporary directory,
        added to its index as a checkout tracks the archive, and the number of its rows."""
        shutil.copy(FIXTURE / "all_ai_commits.csv.7z", self.tmp / "all_ai_commits.csv.7z")
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True, capture_output=True)
        subprocess.run(["git", "add", "all_ai_commits.csv.7z"], cwd=str(self.tmp), check=True,
                       capture_output=True)
        return len(_shared.read_commits_csv(FIXTURE))

    def _extract_beside(self) -> Path:
        """The CSV of the archive, extracted beside it."""
        subprocess.run([SEVEN_ZIP, "x", "-bso0", "-bsp0", "-y", f"-o{self.tmp}",
                        str(self.tmp / "all_ai_commits.csv.7z")], check=True, capture_output=True)
        return self.tmp / "all_ai_commits.csv"

    @unittest.skipUnless(shutil.which("git"), "git is not on PATH")
    def test_a_csv_that_differs_from_the_tracked_archive_beside_it_stops(self):
        """A checkout keeps the extracted CSV when a pull replaces the archive, and the figures
        drawn from that CSV would not be those of the commit."""
        rows = self._tracked_archive()
        csv_path = self._extract_beside()
        lines = csv_path.read_bytes().splitlines(keepends=True)
        csv_path.write_bytes(b"".join(lines[:len(lines) // 2]))
        with self.assertRaises(SystemExit) as caught:
            _shared.read_commits_csv(self.tmp, columns=["repo_name"])
        self.assertIn("all_ai_commits.csv.7z", str(caught.exception.code))
        csv_path.unlink()
        self._extract_beside()
        self.assertEqual(len(_shared.read_commits_csv(self.tmp, columns=["repo_name"])), rows)

    def test_outside_a_checkout_the_csv_beside_an_archive_is_read(self):
        """The data root of a run and a working copy: The detection step wrote the CSV, and an
        older archive beside it decides nothing."""
        shutil.copy(FIXTURE / "all_ai_commits.csv.7z", self.tmp / "all_ai_commits.csv.7z")
        (self.tmp / "all_ai_commits.csv").write_text("repo_name,commit_sha,ai_tool\no/r,abc,Claude\n")
        self.assertEqual(len(_shared.read_commits_csv(self.tmp)), 1)


def _has_statsmodels() -> bool:
    try:
        import statsmodels  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False



class TestReadCommitsChunks(unittest.TestCase):
    """The commit messages make up most of the commit CSV, and a script that reads them holds
    one chunk of rows at a time."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_named_columns_are_read_as_text_a_chunk_at_a_time(self):
        (self.tmp / "all_ai_commits.csv").write_text(
            "repo_name,commit_sha,commit_message\n"
            + "".join(f"o/r,{n:03},\"message {n}\nwith a second line\"\n" for n in range(5)))
        chunks = list(_shared.read_commits_chunks(self.tmp, columns=["commit_message", "commit_sha"],
                                                  rows=2))
        self.assertEqual([len(chunk) for chunk in chunks], [2, 2, 1])
        self.assertEqual({tuple(chunk.columns) for chunk in chunks}, {("commit_sha", "commit_message")})
        self.assertEqual([sha for chunk in chunks for sha in chunk["commit_sha"]],
                         ["000", "001", "002", "003", "004"])

    def test_the_archive_beside_it_is_extracted_for_the_chunks(self):
        shutil.copy(FIXTURE / "all_ai_commits.csv.7z", self.tmp / "all_ai_commits.csv.7z")
        whole = _shared.read_commits_csv(self.tmp, columns=["commit_sha"])
        chunks = list(_shared.read_commits_chunks(self.tmp, columns=["commit_sha"], rows=1000))
        self.assertGreater(len(chunks), 1)
        self.assertEqual([sha for chunk in chunks for sha in chunk["commit_sha"]],
                         list(whole["commit_sha"]))

class TestLatexText(unittest.TestCase):
    """Every script that writes a name into LaTeX escapes it here, and a language name such as
    C# comes from the data."""

    def test_each_character_that_latex_reads_as_markup_prints_itself(self):
        self.assertEqual(_shared.latex_text(r"C# 50% & a_b $x$ {y} ~ ^ \ "),
                         r"C\# 50\% \& a\_b \$x\$ \{y\} \textasciitilde{} \textasciicircum{} "
                         r"\textbackslash{} ")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(_shared.latex_text("C++ / Context Files (yr)"), "C++ / Context Files (yr)")


@unittest.skipUnless(_has_pandas() and _has_statsmodels(), "pandas and statsmodels are not installed")
class TestAdjustTested(unittest.TestCase):
    """Benjamini-Hochberg written out: sort the m p-values that were tested, multiply the k-th
    smallest by m / k, and take the running minimum from the largest down."""

    def adjust(self, values: list) -> tuple:
        import pandas as pd
        p_adjusted, significant = _shared.adjust_tested(pd.Series(values, index=list("abcd")[:len(values)]))
        return p_adjusted, significant

    def test_the_adjusted_p_values_are_benjamini_hochbergs(self):
        """0.03, 0.04 and 0.2 times 3/1, 3/2 and 3/3 are 0.09, 0.06 and 0.2, and the running
        minimum gives 0.06, 0.06 and 0.2. Benjamini-Yekutieli multiplies each by
        1 + 1/2 + 1/3 more."""
        p_adjusted, _ = self.adjust([0.03, 0.04, 0.2])
        for got, want in zip(p_adjusted, [0.06, 0.06, 0.2]):
            self.assertAlmostEqual(got, want, places=12)

    def test_significant_is_an_adjusted_p_below_five_percent(self):
        """Both adjusted p-values of 0.06 lie between 0.05 and 0.1, and a rate of 0.1 would
        call them significant."""
        _, significant = self.adjust([0.03, 0.04, 0.2])
        self.assertEqual(list(significant), [False, False, False])
        p_adjusted, significant = self.adjust([0.001, 0.04, 0.2])
        self.assertAlmostEqual(p_adjusted["a"], 0.003, places=12)
        self.assertEqual(list(significant), [True, False, False])

    def test_a_comparison_without_a_p_value_is_left_out(self):
        """Handed to `multipletests`, one blank among the p-values would make every adjusted
        p-value blank. A comparison with a blank p-value was not tested. It stays blank and not
        significant, and the other comparisons are adjusted as the m that were tested, 0.01
        times 2 / 1 here and not 3 / 1."""
        p_adjusted, significant = self.adjust([0.01, float("nan"), 0.5])
        self.assertAlmostEqual(p_adjusted["a"], 0.02, places=12)
        self.assertTrue(math.isnan(p_adjusted["b"]))
        self.assertAlmostEqual(p_adjusted["c"], 0.5, places=12)
        self.assertEqual(list(significant), [True, False, False])
        self.assertEqual(list(p_adjusted.index), ["a", "b", "c"])

    def test_nothing_tested_is_all_blank(self):
        p_adjusted, significant = self.adjust([float("nan"), float("nan")])
        self.assertTrue(p_adjusted.isna().all())
        self.assertFalse(significant.any())


@unittest.skipUnless(_has_pandas(), "pandas is not installed")
class TestMechanismToolCounts(unittest.TestCase):

    def test_a_file_of_a_repository_outside_repos_is_not_counted(self):
        """The site passes the configured repositories alone, and a mechanism CSV row of
        another repository must count for no cell."""
        import pandas as pd
        repos = pd.DataFrame({"repo_name": ["o/a", "o/b"],
                              **{tool: [tool == "claude", False] for tool in _shared.TOOLS}})
        repos.loc[1, _shared.NO_SPECIFIC_TOOL] = True
        dataframes = {m: pd.DataFrame({"repo_name": [], "tool": []}) for m in _shared.MECHANISMS}
        dataframes["context_file"] = pd.DataFrame({"repo_name": ["o/a", "o/outside", "o/b", "o/gone"],
                                               "tool": ["claude", "claude", "any", "any"]})
        cells, totals = _shared.mechanism_tool_counts(repos, dataframes)
        self.assertEqual(cells[("context_file", "claude")], 1)
        self.assertEqual(cells[("context_file", _shared.NO_SPECIFIC_TOOL)], 1)
        self.assertEqual(totals["claude"], 1)


if __name__ == "__main__":
    unittest.main()
