"""
Tests for rq2/config_cooccurrence.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - The helpers, `stars`, `cramers_v`, `v_magnitude`, `testable`, `which_test`,
    `phi_coefficient`, `association_cell` and `p_cell`, are tested at their thresholds with
    no data.
  - `main()` is run in-process over a small repos.csv written under a temporary directory,
    with DATA_DIR and OUTPUT_DIR replaced, and once as a subprocess over the fixture under
    testdata/output/, with pair counts stated here from the fixture.

Requirements: Python 3.14 (pandas, numpy, scipy and statsmodels, no network)
Run with:  python3 -m unittest test_rq2_config_cooccurrence -v
"""

import contextlib
import csv
import importlib.util
import io
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, fisher_exact
from agentcontext import logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_config_cooccurrence_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("config_cooccurrence", RQ2 / "config_cooccurrence.py")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


HEADER = (FIXTURE / "repos.csv").read_text(encoding="utf-8").splitlines()[0].split(",")
MECHANISMS = mod.MECHANISMS
COLUMNS = ["option_a", "option_b", "both", "only_a", "only_b", "neither", "chi2", "test", "p_raw",
           "phi", "cramers_v", "v_magnitude", "p_adjusted", "significant"]


def repo(name: str, *flags: str) -> dict:
    return {"repo_name": name, **{flag: "TRUE" for flag in flags}}


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
                         "dataset_version": "2.0.0"})
            full.update(row)
            writer.writerow(full)


# Ten repositories over which every mechanism but LSP is TRUE somewhere and FALSE somewhere.
# The chi-squared test needs both values, because a constant column gives a table with a zero
# row. LSP is TRUE nowhere. Its nine pairs are not tested, and the adjustment runs over the
# other 36 pairs.
ROWS = [
    repo("a/1", "context_file", "skills", "settings"),
    repo("a/2", "context_file", "skills", "subagents", "settings", "hooks"),
    repo("a/3", "context_file", "rules", "plugins"),
    repo("a/4", "context_file", "commands", "mcp"),
    repo("a/5", "context_file", "settings", "hooks", "mcp"),
    repo("a/6", "skills", "subagents"),
    repo("a/7", "context_file", "rules", "settings"),
    repo("a/8", "context_file", "commands"),
    repo("a/9", "settings", "mcp", "plugins"),
    repo("a/10", "context_file", "skills", "subagents", "rules", "hooks"),
]


# ROWS with a strong association added: 20 repositories with Skills and Subagents, 10 with
# Skills alone and 20 with a context file alone. Over the 60 repositories, Skills/Subagents
# is [[23, 11], [0, 26]], and phi = 23 * 26 / sqrt(34 * 26 * 23 * 37) = 0.689. The empty cell
# sends the pair to Fisher's exact test, and every other pair over these 60 repositories goes
# there too.
STRONG = (ROWS + [repo(f"s/{i}", "skills", "subagents") for i in range(20)]
          + [repo(f"k/{i}", "skills") for i in range(10)]
          + [repo(f"c/{i}", "context_file") for i in range(20)])

# Thirty repositories over which context_file/skills is [[10, 5], [5, 10]]. Every cell has at
# least five observed and 7.5 expected repositories, and this pair is the one pair of the suite
# that gets its p from the chi-squared test. context_file/settings and skills/settings are
# [[0, 15], [10, 5]] and go to Fisher's exact test.
LARGE = ([repo(f"l/both{i}", "context_file", "skills") for i in range(10)]
         + [repo(f"l/cf{i}", "context_file") for i in range(5)]
         + [repo(f"l/sk{i}", "skills") for i in range(5)]
         + [repo(f"l/set{i}", "settings") for i in range(10)])


def counted_table(rows: list, a: str, b: str) -> list:
    """The 2x2 table of a pair counted from the rows: [[both, a only], [b only, neither]]."""
    has = [(row.get(a) == "TRUE", row.get(b) == "TRUE") for row in rows]
    return [[sum(x and y for x, y in has), sum(x and not y for x, y in has)],
            [sum(y and not x for x, y in has), sum(not x and not y for x, y in has)]]


def phi(table: list) -> float:
    """(ad - bc) / sqrt(r1 r2 c1 c2), the phi coefficient of a 2x2 table. Its absolute value is
    Cramér's V without continuity correction."""
    (a, b), (c, d) = table
    return (a * d - b * c) / math.sqrt((a + b) * (c + d) * (a + c) * (b + d))


def benjamini_hochberg(pvalues: list) -> list:
    """The step-up adjustment written out: p * m / rank, then the running minimum from the
    largest p down, capped at one."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    adjusted = [0.0] * m
    running = 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        adjusted[i] = running
    return adjusted


def tex_cell(tex: str, row: str, column: str) -> str:
    """The matrix cell in the row of mechanism `row` and the column of mechanism `column`."""
    label = mod.MECHANISM_LABELS[row]
    line = next(l for l in tex.splitlines() if l.startswith(f"\\textbf{{{label}}} &"))
    return line[: -len(" \\\\")].split(" & ")[1 + MECHANISMS.index(column)]


class _Run(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.out = self.tmp / "out"
        self.data.mkdir()
        self.out.mkdir()
        for name, value in (("DATA_DIR", self.data), ("OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)

    def run_main(self, rows: list) -> str:
        write_repos_csv(self.data / "repos.csv", rows)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.main()
        return logs.messages(buffer.getvalue())

    def results(self) -> pd.DataFrame:
        return pd.read_csv(self.out / "config_cooccurrence.csv")

    def tex(self) -> str:
        return (self.out / "config_cooccurrence.tex").read_text(encoding="utf-8")


class TestStars(unittest.TestCase):

    def test_the_three_thresholds(self):
        self.assertEqual(mod.stars(0.0009), "***")
        self.assertEqual(mod.stars(0.001), "**")
        self.assertEqual(mod.stars(0.009), "**")
        self.assertEqual(mod.stars(0.01), "*")
        self.assertEqual(mod.stars(0.049), "*")
        self.assertEqual(mod.stars(0.05), "")
        self.assertEqual(mod.stars(1.0), "")


class TestCramersV(unittest.TestCase):

    def test_a_two_by_two_table_divides_by_n(self):
        self.assertAlmostEqual(mod.cramers_v(4.0, 100, 2, 2), 0.2)

    def test_a_zero_statistic_is_zero(self):
        self.assertEqual(mod.cramers_v(0.0, 50, 2, 2), 0.0)

    def test_the_smaller_dimension_sets_the_denominator(self):
        self.assertAlmostEqual(mod.cramers_v(9.0, 100, 4, 2), 0.3)
        self.assertAlmostEqual(mod.cramers_v(9.0, 100, 2, 4), 0.3)

    def test_the_magnitudes_at_cohens_thresholds(self):
        self.assertEqual(mod.v_magnitude(0.099), "negligible")
        self.assertEqual(mod.v_magnitude(0.1), "small")
        self.assertEqual(mod.v_magnitude(0.299), "small")
        self.assertEqual(mod.v_magnitude(0.3), "medium")
        self.assertEqual(mod.v_magnitude(0.499), "medium")
        self.assertEqual(mod.v_magnitude(0.5), "large")


class TestTheTableHelpers(unittest.TestCase):

    def test_a_table_with_an_empty_row_or_column_is_not_testable(self):
        """An empty row and an empty column each make a table untestable. A check that
        requires a repository in any row rather than in every row fails this test."""
        self.assertTrue(mod.testable(np.array([[3, 2], [1, 4]])))
        self.assertFalse(mod.testable(np.array([[3, 2], [0, 0]])))
        self.assertFalse(mod.testable(np.array([[0, 2], [0, 4]])))

    def test_the_chi_squared_test_needs_five_observed_and_expected_in_every_cell(self):
        """SciPy's guideline: "observed and expected frequencies in each cell are at least 5"."""
        def test(table):
            table = np.array(table)
            return mod.which_test(table, chi2_contingency(table, correction=False)[3])
        self.assertEqual(test([[10, 5], [5, 10]]), "chi-squared")
        # Every cell observed at least 5, the first expected 100 / 65 = 1.5.
        self.assertEqual(test([[5, 5], [5, 50]]), "Fisher")
        # Every cell expected 5, two observed 0.
        self.assertEqual(test([[0, 10], [10, 0]]), "Fisher")

    def test_phi_has_the_sign_of_the_association(self):
        """V is the absolute value of phi. Without the sign, a pair of mechanisms that occur
        together less often than independence predicts would read as co-occurring."""
        self.assertAlmostEqual(mod.phi_coefficient(10, 5, 5, 10), 1 / 3, places=12)
        self.assertAlmostEqual(mod.phi_coefficient(0, 15, 10, 5), -150 / math.sqrt(45000), places=12)
        self.assertEqual(mod.phi_coefficient(4, 4, 1, 1), 0.0)

    def test_an_upper_cell_is_phi_with_its_sign_bold_from_a_medium_effect(self):
        self.assertEqual(mod.association_cell(0.3), "\\textbf{0.30}")
        self.assertEqual(mod.association_cell(-0.3), "\\textbf{$-$0.30}")
        self.assertEqual(mod.association_cell(0.29), "0.29")
        self.assertEqual(mod.association_cell(-0.13), "$-$0.13")
        self.assertEqual(mod.association_cell(-0.001), "0.00")

    def test_a_lower_cell_is_the_adjusted_p_with_its_stars_and_the_test(self):
        self.assertEqual(mod.p_cell(0.0005, "chi-squared"), "$<$.001$^{***}$")
        self.assertEqual(mod.p_cell(0.0005, "Fisher"), "$<$.001$^{***\\dagger}$")
        self.assertEqual(mod.p_cell(0.004, "chi-squared"), "0.004$^{**}$")
        self.assertEqual(mod.p_cell(0.03, "Fisher"), "0.03$^{*\\dagger}$")
        self.assertEqual(mod.p_cell(0.2, "Fisher"), "0.20$^{\\dagger}$")
        self.assertEqual(mod.p_cell(0.2, "chi-squared"), "0.20")


class TestMainOverASmallReposCsv(_Run):

    def test_one_row_per_pair_of_the_ten_mechanisms(self):
        self.run_main(ROWS)
        results = self.results()
        self.assertEqual(len(results), 45)
        self.assertEqual(list(results.columns), COLUMNS)
        pairs = set(zip(results["option_a"], results["option_b"]))
        self.assertEqual(len(pairs), 45)
        self.assertTrue(all(a in MECHANISMS and b in MECHANISMS for a, b in pairs))

    def test_a_pair_with_a_mechanism_no_repository_has_is_not_tested(self):
        """The chi-squared test is undefined for a table with an empty row, and scipy raises
        on such a table. The pair keeps its counts, has no statistic, and is left out of the
        adjustment."""
        out = self.run_main(ROWS)
        results = self.results()
        untested = results[(results["option_a"] == "lsp") | (results["option_b"] == "lsp")]
        self.assertEqual(len(untested), 9)
        self.assertTrue(untested[["chi2", "test", "p_raw", "phi", "cramers_v", "p_adjusted"]]
                        .isna().all().all())
        self.assertEqual(set(untested["v_magnitude"]), {"not tested"})
        self.assertEqual(set(untested["significant"].map(str)), {"False"})
        self.assertTrue((untested["both"] == 0).all())
        self.assertIn("0 of the 36 tested pairs of mechanisms are significant (45 pairs in all)",
                      out)
        tex = self.tex()
        self.assertEqual(tex_cell(tex, "context_file", "lsp"), "--")
        self.assertEqual(tex_cell(tex, "lsp", "context_file"), "--")
        self.assertIn("--: not tested", tex)

    def test_a_mechanism_every_repository_has_is_not_tested_either(self):
        rows = [dict(row, lsp="TRUE") for row in ROWS]
        self.run_main(rows)
        results = self.results()
        tested = results["p_raw"].notna()
        self.assertEqual(int(tested.sum()), 36)
        self.assertFalse(((results["option_a"] == "lsp") | (results["option_b"] == "lsp"))
                         [tested].any())

    def test_the_four_cells_of_every_pair_sum_to_the_population(self):
        self.run_main(ROWS)
        results = self.results()
        sums = results[["both", "only_a", "only_b", "neither"]].sum(axis=1)
        self.assertTrue((sums == len(ROWS)).all())

    def test_the_cells_are_the_counts_of_the_table(self):
        self.run_main(ROWS)
        row = self.results().set_index(["option_a", "option_b"]).loc[("context_file", "skills")]
        self.assertEqual((int(row["both"]), int(row["only_a"]), int(row["only_b"]),
                          int(row["neither"])), (3, 5, 1, 1))

    def test_v_is_the_root_of_chi2_over_n_and_phi_its_signed_value(self):
        self.run_main(ROWS)
        results = self.results()
        for _, row in results[results["p_raw"].notna()].iterrows():
            table_phi = phi(counted_table(ROWS, row["option_a"], row["option_b"]))
            self.assertAlmostEqual(row["cramers_v"], math.sqrt(row["chi2"] / len(ROWS)))
            self.assertAlmostEqual(row["cramers_v"], abs(table_phi))
            self.assertAlmostEqual(row["phi"], table_phi)
            self.assertEqual(row["v_magnitude"], mod.v_magnitude(row["cramers_v"]))

    def test_chi2_and_the_p_values_of_a_stated_table(self):
        """chi2 and the p-values are compared with values computed here, and a script that
        applies Yates' correction or a Bonferroni adjustment fails. The context_file/skills
        table of ROWS is [[3, 5], [1, 1]], and without the correction chi2 is
        10 (3 * 1 - 5 * 1)^2 / (8 * 2 * 4 * 6) = 40 / 384. No cell of a table of ten has five
        expected repositories, and Fisher's exact test gives every p here. With the margins
        fixed, the contingency tables with 2, 3 and 4 repositories in both are 6, 24 and 15 of
        the 45. None is more probable than the observed 24, and p is therefore 1."""
        self.run_main(ROWS)
        results = self.results().set_index(["option_a", "option_b"])
        results = results[results["p_raw"].notna()]
        row = results.loc[("context_file", "skills")]
        self.assertAlmostEqual(row["chi2"], 40 / 384, places=12)
        self.assertEqual(row["test"], "Fisher")
        self.assertAlmostEqual(row["p_raw"], 1.0, places=12)
        self.assertAlmostEqual(row["cramers_v"], math.sqrt(40 / 384 / 10), places=12)
        self.assertEqual(set(results["test"]), {"Fisher"})
        # Every pair against scipy's Fisher test on the table counted here, adjusted by the
        # step-up written out above.
        pairs = list(results.index)
        raw = [fisher_exact(counted_table(ROWS, a, b)).pvalue for a, b in pairs]
        for (a, b), p, q in zip(pairs, raw, benjamini_hochberg(raw)):
            with self.subTest(pair=(a, b)):
                self.assertAlmostEqual(results.loc[(a, b), "p_raw"], p, places=12)
                self.assertAlmostEqual(results.loc[(a, b), "p_adjusted"], q, places=12)

    def test_the_adjusted_p_values_are_probabilities_and_significance_is_a_verdict(self):
        self.run_main(ROWS)
        results = self.results()
        results = results[results["p_raw"].notna()]
        self.assertTrue(((results["p_adjusted"] >= 0) & (results["p_adjusted"] <= 1)).all())
        self.assertTrue((results["p_adjusted"] >= results["p_raw"] - 1e-12).all())
        self.assertTrue(results["significant"].map(str).isin(["True", "False"]).all())

    def test_a_repository_with_no_mechanism_is_left_out_of_n(self):
        """A repository with no mechanism is in no table. A V that divides by every row of
        repos.csv therefore fails."""
        out = self.run_main(ROWS + [repo("z/none")])
        self.assertIn(f"{len(ROWS)} repositories have at least one mechanism", out)
        results = self.results()
        self.assertTrue((results[["both", "only_a", "only_b", "neither"]].sum(axis=1)
                         == len(ROWS)).all())
        for _, row in results[results["p_raw"].notna()].iterrows():
            self.assertAlmostEqual(row["cramers_v"],
                                   abs(phi(counted_table(ROWS, row["option_a"], row["option_b"]))))
        self.assertIn(f"$N$\\,=\\,{len(ROWS)} repositories with at least one mechanism", self.tex())

    def test_duplicate_rows_of_one_repository_are_or_folded(self):
        rows = ROWS + [repo("a/6", "rules"), repo("a/6", "mcp")]
        self.run_main(rows)
        results = self.results().set_index(["option_a", "option_b"])
        self.assertTrue((results[["both", "only_a", "only_b", "neither"]].sum(axis=1)
                         == len(ROWS)).all())
        # Folded with its two extra rows, a/6 has skills, subagents, rules and mcp.
        self.assertEqual(int(results.loc[("skills", "rules"), "both"]), 2)
        self.assertEqual(int(results.loc[("rules", "mcp"), "both"]), 1)

    def test_the_tex_table_has_a_row_per_mechanism_with_its_count_on_the_diagonal(self):
        self.run_main(ROWS)
        tex = self.tex()
        for mechanism in MECHANISMS:
            with self.subTest(mechanism=mechanism):
                label = mod.MECHANISM_LABELS[mechanism]
                line = next(l for l in tex.splitlines() if l.startswith(f"\\textbf{{{label}}} &"))
                count = sum(1 for row in ROWS if row.get(mechanism) == "TRUE")
                self.assertIn(f"($n$\\,=\\,{count})", line)
        self.assertIn("(36 comparisons", tex)
        self.assertIn(f"$N$\\,=\\,{len(ROWS)}", tex)
        self.assertTrue(tex.startswith("\\begin{table*}[t]\n"))
        self.assertTrue(tex.endswith("\\end{table*}\n"))

    def test_the_upper_triangle_is_phi_and_the_lower_is_the_adjusted_p(self):
        """The cells are matched whole, the bold and the stars included. Over ROWS,
        context_file/skills has phi (3 - 5) / sqrt(384) = -0.102, and skills/subagents,
        [[3, 1], [0, 6]], has 18 / sqrt(4 * 6 * 3 * 7) = 0.802, in bold. Fisher's test gives
        both pairs a p that the adjustment over 36 pairs raises to 1."""
        self.run_main(ROWS)
        tex = self.tex()
        self.assertEqual(tex_cell(tex, "context_file", "skills"), "$-$0.10")
        self.assertEqual(tex_cell(tex, "skills", "context_file"), "1.00$^{\\dagger}$")
        self.assertEqual(tex_cell(tex, "skills", "subagents"), "\\textbf{0.80}")
        self.assertEqual(tex_cell(tex, "subagents", "skills"), "1.00$^{\\dagger}$")

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_main(ROWS)
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestAStrongAssociation(_Run):
    """No adjusted p over ROWS is below .05, and the stars are tested over these rows."""

    def test_the_bold_cells_the_signs_and_the_stars(self):
        """Every pair over the 60 repositories has a cell below five, and Fisher's exact test
        gives every p. The adjusted values below are Benjamini-Hochberg's over the 36 Fisher
        p-values, computed outside this suite from the hypergeometric probabilities in
        integers."""
        self.run_main(STRONG)
        tex = self.tex()
        # Skills/Subagents: phi 0.689, bold, and adjusted p 2.5e-7
        self.assertEqual(tex_cell(tex, "skills", "subagents"), "\\textbf{0.69}")
        self.assertEqual(tex_cell(tex, "subagents", "skills"), "$<$.001$^{***\\dagger}$")
        # Context files/Skills, [[3, 25], [31, 1]]: phi -0.867, together far less often than
        # independence predicts, and adjusted p 5.4e-11
        self.assertEqual(tex_cell(tex, "context_file", "skills"), "\\textbf{$-$0.87}")
        self.assertEqual(tex_cell(tex, "skills", "context_file"), "$<$.001$^{***\\dagger}$")
        # Context files/Rules, [[3, 25], [0, 32]]: phi 0.245, below the bold threshold, and
        # adjusted p 0.322
        self.assertEqual(tex_cell(tex, "context_file", "rules"), "0.25")
        self.assertEqual(tex_cell(tex, "rules", "context_file"), "0.32$^{\\dagger}$")
        results = self.results().set_index(["option_a", "option_b"])
        self.assertAlmostEqual(results.loc[("skills", "subagents"), "p_adjusted"],
                               2.490108849e-07, places=15)
        self.assertAlmostEqual(results.loc[("context_file", "skills"), "p_adjusted"],
                               5.419613336e-11, places=19)
        self.assertAlmostEqual(results.loc[("settings", "hooks"), "p_adjusted"],
                               0.1178258328, places=10)


class TestTheChiSquaredPath(_Run):
    """Over LARGE, context_file/skills, [[10, 5], [5, 10]], is a table for the chi-squared test:
    chi2 = 30 (100 - 25)^2 / 15^4 = 10 / 3, p = erfc(sqrt(5 / 3)) = 0.0679, phi = 1 / 3. Both
    pairs with Settings, [[0, 15], [10, 5]], go to Fisher's exact test. Of the contingency
    tables with the margins fixed, the table with no repository in both and the table with ten
    are the least probable, at C(15, 10) / C(30, 10) each. That gives p = 6006 / 30045015 and
    phi = -150 / sqrt(45000). Benjamini-Hochberg over the three pairs multiplies the two
    Fisher p-values by 3 / 2 and the chi-squared p-value by 3 / 3."""

    def test_the_statistics_and_the_tests(self):
        self.run_main(LARGE)
        results = self.results().set_index(["option_a", "option_b"])
        tested = results[results["p_raw"].notna()]
        self.assertEqual(sorted(tested.index), [("context_file", "settings"),
                                                ("context_file", "skills"), ("skills", "settings")])
        row = tested.loc[("context_file", "skills")]
        self.assertEqual(row["test"], "chi-squared")
        self.assertAlmostEqual(row["chi2"], 10 / 3, places=12)
        self.assertAlmostEqual(row["p_raw"], math.erfc(math.sqrt(5 / 3)), places=12)
        self.assertAlmostEqual(row["phi"], 1 / 3, places=12)
        self.assertAlmostEqual(row["p_adjusted"], math.erfc(math.sqrt(5 / 3)), places=12)
        for pair in (("context_file", "settings"), ("skills", "settings")):
            with self.subTest(pair=pair):
                row = tested.loc[pair]
                self.assertEqual(row["test"], "Fisher")
                self.assertAlmostEqual(row["p_raw"], 6006 / 30045015, places=15)
                self.assertAlmostEqual(row["p_adjusted"], 6006 / 30045015 * 3 / 2, places=15)
                self.assertAlmostEqual(row["phi"], -150 / math.sqrt(45000), places=12)
                self.assertAlmostEqual(row["cramers_v"], 150 / math.sqrt(45000), places=12)

    def test_the_cells_and_the_note(self):
        out = self.run_main(LARGE)
        tex = self.tex()
        self.assertEqual(tex_cell(tex, "context_file", "skills"), "\\textbf{0.33}")
        self.assertEqual(tex_cell(tex, "skills", "context_file"), "0.07")
        self.assertEqual(tex_cell(tex, "context_file", "settings"), "\\textbf{$-$0.71}")
        self.assertEqual(tex_cell(tex, "settings", "context_file"), "$<$.001$^{***\\dagger}$")
        self.assertIn("Fisher's exact test ($^{\\dagger}$) where a cell has fewer than 5 observed "
                      "or expected repositories", tex)
        self.assertIn("2 pairs were tested with Fisher's exact test", out)


class TestOverTheFixture(unittest.TestCase):
    """The script as the check runs it, over the 32 repositories of testdata/output/."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(cls.tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        cls.result = subprocess.run([sys.executable, str(RQ2 / "config_cooccurrence.py")],
                                    cwd=str(RQ2), env=env, capture_output=True, text=True)
        cls.out = cls.tmp / "rq2"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_run_succeeds(self):
        self.assertEqual(self.result.returncode, 0, self.result.stderr)

    def test_the_counts_of_the_fixture(self):
        # 31 of the 32 fixture repositories have a context file and 21 have settings. 20
        # have both, 11 the context file alone, 1 settings alone and none neither.
        results = pd.read_csv(self.out / "config_cooccurrence.csv")
        self.assertEqual(len(results), 45)
        self.assertEqual(int(results["p_raw"].notna().sum()), 36)
        row = results.set_index(["option_a", "option_b"]).loc[("context_file", "settings")]
        self.assertEqual((int(row["both"]), int(row["only_a"]), int(row["only_b"]),
                          int(row["neither"])), (20, 11, 1, 0))
        self.assertTrue((results[["both", "only_a", "only_b", "neither"]].sum(axis=1) == 32).all())

    def test_the_tex_names_the_population(self):
        tex = (self.out / "config_cooccurrence.tex").read_text(encoding="utf-8")
        self.assertIn("$N$\\,=\\,32", tex)
        self.assertIn("\\textbf{Commands} & ", tex)
        self.assertIn("($n$\\,=\\,2)", tex)


if __name__ == "__main__":
    unittest.main()
