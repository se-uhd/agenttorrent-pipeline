"""
Tests for rq2/repo_metadata_comparison.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - The helpers, `cliffs_delta`, `delta_magnitude`, `stars`, `_fmt_num`, `fmt_iqr`,
    `fmt_median` and `fmt_iqr_only`, are tested over values chosen at their thresholds and
    over an empty Series, which is what a tool with no adopter gives them.
  - `main()` is run in-process over a small repos.csv, repos_annotated.csv and
    ghs_filtered.json written under a temporary directory, reached by replacing the
    module's DATA_DIR and OUTPUT_DIR and `_repo_metadata`'s CLASSIFICATION_DIR, SAMPLING_DIR
    and OUTPUT_DIR. Neither extracted archive is needed. A second population of 40
    repositories, with one tool's adopters as the 20 largest, renders a bold and starred
    cell.
  - One subprocess run over the fixture, in which three repositories have no specific tool.
    The run skips when the two extracted inputs are absent from the checkout.

Requirements: Python 3.14 (pandas, numpy, scipy and statsmodels, no network)
Run with:  python3 -m unittest test_rq2_repo_metadata_comparison -v
"""

import contextlib
import csv
import importlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from agentcontext import logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_repo_metadata_comparison_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("repo_metadata_comparison", RQ2 / "repo_metadata_comparison.py")
rm = importlib.import_module("_repo_metadata")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


HEADER = (FIXTURE / "repos.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
TOOLS = mod.TOOLS
CSV_COLUMNS = ["tool", "metric", "n_with", "n_without", "median_with", "q1_with", "q3_with",
               "median_without", "q1_without", "q3_without", "U", "p_raw", "cliffs_delta",
               "delta_magnitude", "p_adjusted", "significant"]


def write_repos_csv(path: Path, rows: list) -> None:
    """A repos.csv with the fixture's columns, every flag FALSE unless the row names it."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            full = {column: "FALSE" for column in HEADER}
            full.update({"scanned_at": "2026-09-12T00:00:00Z", "branch": "main",
                         "head_sha": "0" * 40, "head_commit_date": "2026-09-01T00:00:00Z",
                         "github_link": "https://github.com/" + row["repo_name"],
                         "dataset_version": "1.0.0"})
            full.update(row)
            writer.writerow(full)


def population(n: int, adopters) -> list:
    """`n` repositories, in which repository `i` adopts tool `t` (by index in TOOLS) when
    `adopters(i, t)`."""
    rows = []
    for i in range(n):
        row = {"repo_name": f"o/r{i}"}
        for t, tool in enumerate(TOOLS):
            if adopters(i, t):
                row[tool] = "TRUE"
        rows.append(row)
    return rows


def write_metadata_inputs(root: Path, n: int) -> None:
    classification = root / "2_classification"
    sampling = root / "1_sampling"
    classification.mkdir(parents=True, exist_ok=True)
    sampling.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"repo_name": [f"o/r{i}" for i in range(n)], "default_branch": "main",
                  "engineered_project": "true", "justification": "t"}) \
        .to_csv(classification / "repos_annotated.csv", index=False)
    items = [{"name": f"o/r{i}", "mainLanguage": "Go", "createdAt": f"{2010 + i}-01-01T00:00:00",
              "contributors": i + 1, "commits": 10 * (i + 1), "size": 100 * (i + 1)}
             for i in range(n)]
    ghs = {"_metadata": {"input_source": {"downloaded_at": "2026-03-16T18:07:01Z"}}, "items": items}
    (sampling / "ghs_filtered.json").write_text(json.dumps(ghs), encoding="utf-8")


def write_separating_inputs(root: Path, n: int) -> None:
    """Metadata for `n` repositories in which size rises with the index and the other three
    metrics are shuffled: contributors (7i mod n) + 1, commits 10 ((13i mod n) + 1), and a
    creation year 2000 + (11i mod 20)."""
    classification = root / "2_classification"
    sampling = root / "1_sampling"
    classification.mkdir(parents=True, exist_ok=True)
    sampling.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"repo_name": [f"o/r{i}" for i in range(n)], "default_branch": "main",
                  "engineered_project": "true", "justification": "t"}) \
        .to_csv(classification / "repos_annotated.csv", index=False)
    items = [{"name": f"o/r{i}", "mainLanguage": "Go",
              "createdAt": f"{2000 + (11 * i) % 20}-01-01T00:00:00",
              "contributors": (7 * i) % n + 1, "commits": 10 * ((13 * i) % n + 1),
              "size": 100 * (i + 1)} for i in range(n)]
    ghs = {"_metadata": {"input_source": {"downloaded_at": "2026-03-16T18:07:01Z"}}, "items": items}
    (sampling / "ghs_filtered.json").write_text(json.dumps(ghs), encoding="utf-8")


def age_years(year: int) -> float:
    """The age at the download day, 2026-03-16, of a repository created on 1 January."""
    return (date(2026, 3, 16) - date(year, 1, 1)).days / 365.25


def benjamini_hochberg(p_values: list) -> list:
    """The adjusted p-values written out: the k-th smallest of m times m / k, the running
    minimum from the largest down, at most 1, in the order given."""
    m = len(p_values)
    order = sorted(range(m), key=lambda i: p_values[i])
    adjusted, running = [0.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, p_values[i] * m / rank)
        adjusted[i] = running
    return adjusted


class TestCliffsDelta(unittest.TestCase):

    def test_disjoint_groups_are_minus_one_and_one(self):
        self.assertEqual(mod.cliffs_delta(np.array([1, 2, 3]), np.array([4, 5, 6])), -1.0)
        self.assertEqual(mod.cliffs_delta(np.array([4, 5, 6]), np.array([1, 2, 3])), 1.0)

    def test_identical_groups_are_zero(self):
        self.assertEqual(mod.cliffs_delta(np.array([1, 2, 3]), np.array([1, 2, 3])), 0.0)

    def test_a_partial_overlap(self):
        # 2 > 1 and 2 < 3, 4 > 1 and 4 > 3: three more, one less, over four pairs
        self.assertAlmostEqual(mod.cliffs_delta(np.array([2, 4]), np.array([1, 3])), 0.5)

    def test_the_magnitudes_at_romanos_thresholds(self):
        self.assertEqual(mod.delta_magnitude(0.146), "negligible")
        self.assertEqual(mod.delta_magnitude(0.147), "small")
        self.assertEqual(mod.delta_magnitude(0.329), "small")
        self.assertEqual(mod.delta_magnitude(0.33), "medium")
        self.assertEqual(mod.delta_magnitude(0.473), "medium")
        self.assertEqual(mod.delta_magnitude(0.474), "large")
        self.assertEqual(mod.delta_magnitude(-0.9), "large")

    def test_a_missing_delta_is_not_tested(self):
        """A comparison with an empty group has no delta. NaN is below no threshold, and a
        test of the thresholds alone would call the missing delta large."""
        self.assertEqual(mod.delta_magnitude(float("nan")), "not tested")

    def test_an_empty_group_has_no_delta(self):
        """An empty group gives no pair to compare, and the share of pairs is undefined
        rather than a division by zero."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            for x, y in ((np.array([]), np.array([1, 2])), (np.array([1, 2]), np.array([]))):
                with self.subTest(x=x, y=y):
                    self.assertTrue(np.isnan(mod.cliffs_delta(x, y)))


class TestStars(unittest.TestCase):

    def test_the_three_thresholds(self):
        self.assertEqual(mod.stars(0.0009), "***")
        self.assertEqual(mod.stars(0.001), "**")
        self.assertEqual(mod.stars(0.01), "*")
        self.assertEqual(mod.stars(0.05), "")
        self.assertEqual(mod.stars(float("nan")), "")


class TestTheNumberFormat(unittest.TestCase):

    def test_ten_thousand_and_above_is_in_thousands(self):
        self.assertEqual(mod._fmt_num(12345), "12k")
        self.assertEqual(mod._fmt_num(15500), "16k")
        self.assertEqual(mod._fmt_num(1_234_567), "1,235k")

    def test_below_ten_has_one_decimal(self):
        self.assertEqual(mod._fmt_num(2.5), "2.5")
        self.assertEqual(mod._fmt_num(0), "0.0")
        self.assertEqual(mod._fmt_num(9.99), "10.0")

    def test_between_is_an_integer_with_thousands_separators(self):
        self.assertEqual(mod._fmt_num(1500), "1,500")
        self.assertEqual(mod._fmt_num(10), "10")
        self.assertEqual(mod._fmt_num(9999.4), "9,999")

    def test_halves_round_up_rather_than_to_even(self):
        self.assertEqual(mod._fmt_num(10.5), "11")
        self.assertEqual(mod._fmt_num(2.25), "2.3")
        self.assertEqual(mod._fmt_num(12500), "13k")

    def test_a_value_that_is_not_a_number_is_n_a(self):
        for value in (float("nan"), np.nan, None, np.float64("nan")):
            with self.subTest(value=value):
                self.assertEqual(mod._fmt_num(value), "n/a")

    def test_the_series_formats(self):
        series = pd.Series([1, 2, 3, 4, 5])
        self.assertEqual(mod.fmt_iqr(series), "3.0 (2.0--4.0)")
        self.assertEqual(mod.fmt_median(series), "3.0")
        self.assertEqual(mod.fmt_iqr_only(series), "(2.0--4.0)")
        tens = pd.Series([10, 20, 30, 40, 50])
        self.assertEqual(mod.fmt_iqr(tens), "30 (20--40)")

    def test_an_empty_group_formats_as_n_a(self):
        empty = pd.Series([], dtype=float)
        self.assertEqual(mod.fmt_median(empty), "n/a")
        self.assertEqual(mod.fmt_iqr_only(empty), "(n/a--n/a)")
        self.assertEqual(mod.fmt_iqr(empty), "n/a (n/a--n/a)")


class _Run(unittest.TestCase):

    N = 12

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "output"
        self.out = self.tmp / "out"
        self.data.mkdir()
        self.out.mkdir()
        write_metadata_inputs(self.tmp, self.N)
        for module, name, value in ((mod, "DATA_DIR", self.data), (mod, "OUTPUT_DIR", self.out),
                                    (rm, "CLASSIFICATION_DIR", self.tmp / "2_classification"),
                                    (rm, "SAMPLING_DIR", self.tmp / "1_sampling"),
                                    (rm, "OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)

    def run_main(self, rows: list) -> str:
        write_repos_csv(self.data / "repos.csv", rows)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            mod.main()
        return logs.messages(buffer.getvalue())

    def results(self) -> pd.DataFrame:
        return pd.read_csv(self.out / "repo_metadata_comparison.csv")

    def tex(self) -> str:
        return (self.out / "repo_metadata_comparison.tex").read_text(encoding="utf-8")


class TestMainOverASmallPopulation(_Run):
    """Every tool with eight adopters and four non-adopters among twelve repositories."""

    ROWS = population(12, lambda i, t: (i + t) % 3 != 0)

    def test_one_row_per_tool_and_metric(self):
        self.run_main(self.ROWS)
        results = self.results()
        self.assertEqual(len(results), 28)
        self.assertEqual(list(results.columns), CSV_COLUMNS)
        self.assertEqual(list(results["tool"]), [t for t in TOOLS for _ in mod.METRICS])
        self.assertEqual(list(results["metric"]), mod.METRICS * len(TOOLS))

    def test_the_groups_partition_the_population(self):
        self.run_main(self.ROWS)
        results = self.results()
        self.assertTrue((results["n_with"] == 8).all())
        self.assertTrue((results["n_without"] == 4).all())

    def test_the_medians_are_of_the_joined_metadata(self):
        self.run_main(self.ROWS)
        results = self.results().set_index(["tool", "metric"])
        # claude (t=0) is adopted by i in {1,2,4,5,7,8,10,11}: contributors i+1
        self.assertEqual(results.loc[("claude", "contributors"), "median_with"], 7.0)
        self.assertEqual(results.loc[("claude", "contributors"), "median_without"], 5.5)
        # commits 20, 30, 50, 60, 80, 90, 110, 120: Q1 at position 1.75 is 45
        self.assertEqual(results.loc[("claude", "commits"), "q1_with"], 45.0)
        # The adopters were created in 2011, 2012, 2014, 2015, 2017, 2018, 2020 and 2021. The
        # median age is halfway between the 2015 and 2017 repositories.
        age = results.loc[("claude", "repo_age_years"), "median_with"]
        self.assertAlmostEqual(age, (age_years(2015) + age_years(2017)) / 2, places=12)

    def test_u_p_and_delta_of_one_row_by_hand(self):
        """U, delta and p are computed by hand, and a script with swapped arguments to
        cliffs_delta or with a one-sided test fails. Claude's contributors are
        2, 3, 5, 6, 8, 9, 11, 12 against 1, 4, 7, 10. In 20 of the 32 pairs the adopter is
        larger, which gives U = 20 and delta = (20 - 12) / 32 = 0.25. Of the 495 equally likely
        ways to place four ranks out of twelve, 141 give U >= 20, and the exact two-sided p is
        2 * 141 / 495."""
        self.run_main(self.ROWS)
        row = self.results().set_index(["tool", "metric"]).loc[("claude", "contributors")]
        self.assertEqual(row["U"], 20.0)
        self.assertAlmostEqual(row["cliffs_delta"], 0.25, places=12)
        self.assertAlmostEqual(row["p_raw"], 282 / 495, places=12)

    def test_delta_and_its_magnitude_agree_with_the_helpers(self):
        self.run_main(self.ROWS)
        for _, row in self.results().iterrows():
            self.assertEqual(row["delta_magnitude"], mod.delta_magnitude(row["cliffs_delta"]))
            self.assertTrue(-1 <= row["cliffs_delta"] <= 1)

    def test_the_adjusted_p_values_are_probabilities(self):
        self.run_main(self.ROWS)
        results = self.results()
        self.assertTrue(((results["p_adjusted"] >= 0) & (results["p_adjusted"] <= 1)).all())
        self.assertTrue((results["p_adjusted"] >= results["p_raw"] - 1e-12).all())
        self.assertTrue(results["significant"].map(str).isin(["True", "False"]).all())

    def test_the_adjusted_p_values_are_benjamini_hochbergs_over_the_28(self):
        self.run_main(self.ROWS)
        results = self.results()
        for got, want in zip(results["p_adjusted"], benjamini_hochberg(list(results["p_raw"]))):
            self.assertAlmostEqual(got, want, places=12)

    def test_the_quartiles_of_the_non_adopters(self):
        """The complement's quartiles are stated too, and a first quartile taken at another
        fraction fails. Claude's non-adopters have 1, 4, 7 and 10 contributors, and the
        quartiles at positions 0.75 and 2.25 are 3.25 and 7.75."""
        self.run_main(self.ROWS)
        row = self.results().set_index(["tool", "metric"]).loc[("claude", "contributors")]
        self.assertAlmostEqual(row["q1_without"], 3.25, places=12)
        self.assertAlmostEqual(row["q3_without"], 7.75, places=12)

    def test_the_three_files_are_written(self):
        out = self.run_main(self.ROWS)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["repo_metadata.csv", "repo_metadata_comparison.csv",
                          "repo_metadata_comparison.tex"])
        self.assertIn("The comparison covers the 12 configured repositories with GHS metadata",
                      out)
        self.assertIn("Wrote repo_metadata_comparison.csv (28 rows)", out)

    def test_the_tex_has_a_baseline_and_three_lines_per_tool(self):
        """The baseline is checked whole, and a baseline of maxima fails. The twelve
        repositories have ages from 1,900 to 5,918 days, contributors 1 to 12, commits 10 to 120
        and size 100 to 1,200, with quartiles at positions 2.75 and 8.25. The age median is
        halfway between the 2015 and 2016 repositories, 10.7 years."""
        self.run_main(self.ROWS)
        lines = self.tex().splitlines()
        self.assertEqual(lines[9], "All & 11 & 6.5 & 65 & 650 \\\\")
        # Age quartiles: 2019 plus three quarters of a year, 7.95, and 2013 plus a quarter of
        # the leap year 2012, 13.45. Contributors 3.75 and 9.25, rounded half up.
        self.assertEqual(lines[10], "($n$=12) & (8.0--13) & (3.8--9.3) & (38--93) & (375--925) \\\\")
        for tool in TOOLS:
            with self.subTest(tool=tool):
                label = mod.TOOL_LABELS[tool]
                index = next(i for i, l in enumerate(lines) if l.startswith(f"{label} & "))
                self.assertTrue(lines[index + 1].startswith("($n$=8) & "))
                self.assertTrue(lines[index + 2].startswith(" & $\\delta$=")
                                or lines[index + 2].startswith(" & \\textbf{$\\delta$="))
        self.assertIn("(28 comparisons)", self.tex())

    def test_duplicate_rows_of_one_repository_are_or_folded(self):
        rows = self.ROWS + [{"repo_name": "o/r0", "claude": "TRUE"}]
        # o/r0 has no claude in ROWS ((0 + 0) % 3 == 0). The extra row gives it one.
        self.run_main(rows)
        results = self.results().set_index(["tool", "metric"])
        self.assertEqual(int(results.loc[("claude", "contributors"), "n_with"]), 9)
        self.assertEqual(int(results.loc[("claude", "contributors"), "n_without"]), 3)

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_main(self.ROWS)
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestAToolThatSeparatesOnOneMetric(_Run):
    """Nothing in the small population is significant, and this population renders the bold
    and starred cell. Codex is adopted by the upper half of 40 repositories with a size of
    100 (i + 1) KB, and its adopters are the 20 largest. That gives U = 400 of 400 and delta 1,
    a raw p of 6.8e-8, and an adjusted p below 28 times that, under .001. The other three
    metrics are shuffled against the size."""

    N = 40
    ROWS = population(40, lambda i, t: i >= 20 if TOOLS[t] == "codex" else (i + t) % 3 != 0)

    def setUp(self):
        super().setUp()
        write_separating_inputs(self.tmp, self.N)

    def test_the_size_cells_are_bold_and_starred_and_the_others_are_not(self):
        self.run_main(self.ROWS)
        lines = self.tex().splitlines()
        index = next(i for i, l in enumerate(lines) if l.startswith("Codex & "))
        self.assertEqual(lines[index], "Codex & 17 & 22 & 185 & \\textbf{3,050$^{***}$} \\\\")
        # Adopters' size 2,100 to 4,000: quartiles 2,575 and 3,525. The age median is halfway
        # between the 2009 and 2010 repositories. Contributors 21 and 23, commits 180 and 190.
        self.assertEqual(lines[index + 1],
                         "($n$=20) & (12--21) & (13--31) & (68--293) & \\textbf{(2,575--3,525)} \\\\")
        # Contributors: U = 220, delta (440 - 400) / 400. Commits: U = 160, delta -0.20. The
        # creation years fall one in each group, so the age delta is 0.
        self.assertEqual(lines[index + 2], " & $\\delta$=0.00 & $\\delta$=0.10 & $\\delta$=-0.20 & "
                                           "\\textbf{$\\delta$=1.00} \\\\")
        results = self.results().set_index(["tool", "metric"])
        self.assertEqual(results.loc[("codex", "size"), "U"], 400.0)
        self.assertTrue(results.loc[("codex", "size"), "significant"])
        self.assertLess(results.loc[("codex", "size"), "p_adjusted"], 0.001)
        self.assertAlmostEqual(results.loc[("codex", "repo_age_years"), "median_with"],
                               (age_years(2009) + age_years(2010)) / 2, places=12)


class TestAToolWithNoAdopter(_Run):
    """A tool that no repository adopts: the group is empty, and the test statistic, the median
    and the quartiles are NaN."""

    ROWS = population(12, lambda i, t: t != 2 and (i + t) % 3 != 0)

    def test_the_script_finishes_and_prints_n_a(self):
        self.run_main(self.ROWS)
        results = self.results().set_index(["tool", "metric"])
        self.assertEqual(int(results.loc[("no_specific_tool", "size"), "n_with"]), 0)
        self.assertTrue(pd.isna(results.loc[("no_specific_tool", "size"), "median_with"]))
        self.assertTrue(pd.isna(results.loc[("no_specific_tool", "size"), "p_raw"]))
        lines = self.tex().splitlines()
        index = next(i for i, l in enumerate(lines) if l.startswith("No specific tool & "))
        self.assertEqual(lines[index], "No specific tool & n/a & n/a & n/a & n/a \\\\")
        self.assertEqual(lines[index + 1], "($n$=0) & (n/a--n/a) & (n/a--n/a) & (n/a--n/a) & (n/a--n/a) \\\\")

    def test_the_other_tools_are_adjusted_among_the_twenty_four_tests_that_ran(self):
        """The four tests of the empty group have no p-value, and one NaN among the 28 would
        make `multipletests` return NaN for all 28. The other 24 are adjusted among themselves,
        and the note counts 24 comparisons."""
        self.run_main(self.ROWS)
        results = self.results()
        others = results[results["tool"] != "no_specific_tool"]
        self.assertFalse(others["median_with"].isna().any())
        self.assertFalse(others["p_raw"].isna().any())
        self.assertFalse(others["cliffs_delta"].isna().any())
        expected = benjamini_hochberg(list(others["p_raw"]))
        for got, want in zip(others["p_adjusted"], expected):
            self.assertAlmostEqual(got, want, places=12)
        empty = results[results["tool"] == "no_specific_tool"]
        self.assertTrue(empty["p_adjusted"].isna().all())
        self.assertFalse(empty["significant"].any())
        self.assertIn("(24 comparisons)", self.tex())

    def test_the_comparisons_of_the_empty_group_are_not_tested(self):
        """The four comparisons of the tool without an adopter have no delta. The CSV and the
        TeX mark them as not tested, as the co-occurrence table does, rather than calling them a
        large effect of NaN. The run divides nothing by zero."""
        write_repos_csv(self.data / "repos.csv", self.ROWS)
        with contextlib.redirect_stderr(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            mod.main()
        results = self.results()
        empty = results[results["tool"] == "no_specific_tool"]
        self.assertTrue(empty["cliffs_delta"].isna().all())
        self.assertEqual(list(empty["delta_magnitude"]), ["not tested"] * 4)
        others = results[results["tool"] != "no_specific_tool"]
        self.assertNotIn("not tested", list(others["delta_magnitude"]))
        lines = self.tex().splitlines()
        index = next(i for i, l in enumerate(lines) if l.startswith("No specific tool & "))
        self.assertEqual(lines[index + 2], " & -- & -- & -- & -- \\\\")
        self.assertNotIn("nan", self.tex())
        self.assertIn("--: not tested, since no repository or every repository uses the tool.",
                      self.tex())


class TestOverTheFixture(unittest.TestCase):
    """The script as the check runs it, over the 32 repositories of testdata/output/ joined
    with their rows of the classification table and of the GHS sampling frame of March 2026,
    which testdata/classification/ and testdata/sampling/ contain. The group with no specific tool
    is 47ng/nuqs, Automattic/harper and CodebuffAI/codebuff. Their GHS rows give 68, 83 and 25
    contributors, 1,237, 3,989 and 6,141 commits, 38,886, 36,552 and 144,515 KB, and creation
    dates 2020-05-11T03:52:32, 2023-10-22T02:58:35 and 2024-07-09T09:21:56. At the download
    day, 2026-03-16, their ages are 2,134, 875 and 614 whole days."""

    def test_the_no_specific_tool_group_has_its_numbers(self):
        """Over the fixture, the group with no specific tool has a number in every cell, and
        every adjusted p-value of a tool with adopters is a number. The fixture predates
        OpenCode, and no repository in it adopts OpenCode. The four tests of OpenCode therefore
        have no p-value."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        # A data root of the test's own, so that the script reads the fixture's sampling frame
        # and classification table rather than those of the dataset that the checkout extracts.
        root = tmp / "data"
        for stage in ("sampling", "classification"):
            shutil.copytree(FIXTURE.parent / stage, root / stage)
        env = dict(os.environ, AGENTCONTEXT_DATA_ROOT=str(root),
                   DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(RQ2 / "repo_metadata_comparison.py")],
                                cwd=str(RQ2), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = tmp / "rq2"
        self.assertEqual(sorted(p.name for p in out.iterdir()),
                         ["repo_metadata.csv", "repo_metadata_comparison.csv",
                          "repo_metadata_comparison.tex"])
        results = pd.read_csv(out / "repo_metadata_comparison.csv")
        self.assertEqual(len(results), 28)
        agents = results[results["tool"] == "no_specific_tool"]
        self.assertTrue((agents["n_with"] == 3).all())
        self.assertTrue((agents["n_without"] == 29).all())
        adopted = results["tool"] != "opencode"
        self.assertFalse(results.loc[adopted, "p_adjusted"].isna().any())
        self.assertTrue(results.loc[~adopted, "p_adjusted"].isna().all())
        lines = (out / "repo_metadata_comparison.tex").read_text(encoding="utf-8").splitlines()
        index = next(i for i, l in enumerate(lines) if l.startswith("No specific tool & "))
        self.assertEqual(lines[index], "No specific tool & 2.4 & 68 & 3,989 & 39k \\\\")
        # Medians 875 / 365.25 = 2.40 years, 68, 3,989 and 38,886 KB. Quartiles halfway
        # between the neighbors, rounded half up: 46.5 is 47 and 37,719 KB is 38k.
        self.assertEqual(lines[index + 1],
                         "($n$=3) & (2.0--4.1) & (47--76) & (2,613--5,065) & (38k--92k) \\\\")


if __name__ == "__main__":
    unittest.main()
