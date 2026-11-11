"""Pairwise associations between configuration mechanisms.

For every pair of the ten mechanisms (Context Files, Skills, Subagents, Commands, Rules,
Settings, Hooks, MCP, Plugins, LSP), the script computes a 2x2 contingency table over the
repositories with at least one mechanism, the phi coefficient with its sign, and Cramer's V
from the chi-squared statistic. The p-value comes from the chi-squared test where every cell
has at least MIN_CELL_COUNT observed and expected repositories, and from Fisher's exact test
where a cell does not. A pair with a mechanism that no repository or every repository has is
not tested, because the test is undefined for a table with an empty row or column. The
p-values are adjusted with Benjamini-Hochberg over the tested pairs.

Input:
  ../../data/collection/output/repos.csv

Outputs (in ../../data/analysis/rq2/):
  config_cooccurrence.csv: per-pair table with chi2, the test, p, phi, V, magnitude
  config_cooccurrence.tex: LaTeX symmetric phi-matrix table with stars
"""

from itertools import combinations
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import chi2_contingency, fisher_exact

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import MECHANISM_LABELS, MECHANISMS, DATA_DIR, adjust_tested, latex_text, output_dir
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

# The smallest observed and expected count of a cell over which the chi-squared test is used.
# SciPy's chi2_contingency: "An often quoted guideline for the validity of this calculation is
# that the test should be used only if the observed and expected frequencies in each cell are at
# least 5." Below it, Fisher's exact test gives the p-value. SciPy's fisher_exact states that
# the chi-squared test "can be used as an alternative to `fisher_exact` when the numbers in the
# table are large".
MIN_CELL_COUNT = 5
CHI_SQUARED = "chi-squared"
FISHER = "Fisher"
# Cohen's (1988) thresholds of Cramer's V for df* = 1: a V below the first is negligible, below
# the second small, below the third medium, and from the third on large. Bold marks a medium or
# large effect.
V_THRESHOLDS = (0.1, 0.3, 0.5)
V_DECIMALS = 2
# The adjusted p below which a pair gets three, two and one stars.
SIGNIFICANCE_LEVELS = (0.001, 0.01, 0.05)
# A p below P_FLOOR is written as that bound, a p below SMALL_P_BELOW with SMALL_P_DECIMALS
# decimals, and any other p with P_DECIMALS.
P_FLOOR = 0.001
SMALL_P_BELOW = 0.01
SMALL_P_DECIMALS = 3
P_DECIMALS = 2


def stars(p: float) -> str:
    """Significance stars based on an (adjusted) p-value."""
    if p < SIGNIFICANCE_LEVELS[0]:
        return "***"
    if p < SIGNIFICANCE_LEVELS[1]:
        return "**"
    if p < SIGNIFICANCE_LEVELS[2]:
        return "*"
    return ""


def cramers_v(chi2: float, n: int, k: int, r: int) -> float:
    """Cramer's V from a chi-squared statistic and 2D table dimensions."""
    return np.sqrt(chi2 / (n * min(k - 1, r - 1)))


def v_magnitude(v: float) -> str:
    """Cohen (1988) magnitude classification for Cramer's V (df*=1)."""
    if v < V_THRESHOLDS[0]:
        return "negligible"
    if v < V_THRESHOLDS[1]:
        return "small"
    if v < V_THRESHOLDS[2]:
        return "medium"
    return "large"


def testable(table: np.ndarray) -> bool:
    """Whether every row and column of a contingency table has a repository."""
    return bool((table.sum(axis=0) > 0).all() and (table.sum(axis=1) > 0).all())


def which_test(table: np.ndarray, expected: np.ndarray) -> str:
    """The test that gives a table's p-value: chi-squared when every cell has at least
    MIN_CELL_COUNT observed and expected repositories, Fisher's exact test otherwise."""
    if (table < MIN_CELL_COUNT).any() or (expected < MIN_CELL_COUNT).any():
        return FISHER
    return CHI_SQUARED


def phi_coefficient(both: int, only_a: int, only_b: int, neither: int) -> float:
    """The phi coefficient of a 2x2 table, the correlation of the two flags: positive when the
    two mechanisms are together more often than independence predicts, negative when less.
    Its absolute value is Cramer's V of the table."""
    margins = (both + only_a) * (only_b + neither) * (both + only_b) * (only_a + neither)
    return (both * neither - only_a * only_b) / math.sqrt(margins)


def association_cell(phi: float) -> str:
    """An upper-triangle cell: phi to two decimals with its sign, bold from a medium effect."""
    cell = f"{abs(phi):.{V_DECIMALS}f}"
    if phi < 0 and float(cell) > 0:
        cell = f"$-${cell}"
    if abs(phi) >= V_THRESHOLDS[1]:
        cell = f"\\textbf{{{cell}}}"
    return cell


def p_cell(p: float, test: str) -> str:
    """A lower-triangle cell: the adjusted p, its stars, and a dagger for Fisher's exact test."""
    if p < P_FLOOR:
        cell = "$<$.001"
    elif p < SMALL_P_BELOW:
        cell = f"{p:.{SMALL_P_DECIMALS}f}"
    else:
        cell = f"{p:.{P_DECIMALS}f}"
    marks = stars(p) + ("\\dagger" if test == FISHER else "")
    if marks:
        cell += f"$^{{{marks}}}$"
    return cell


def main() -> None:
    """Compute the associations of the mechanisms and write the CSV and the LaTeX table."""
    # Load data
    repos = pd.read_csv(DATA_DIR / "repos.csv")

    for col in MECHANISMS:
        repos[col] = repos[col].apply(lambda v: str(v).upper() == "TRUE")

    # Deduplicate on repo_name (OR across the mechanism columns)
    if repos["repo_name"].duplicated().any():
        repos = repos.groupby("repo_name", as_index=False).agg(
            {c: "max" for c in MECHANISMS}
        )

    # Filter to the repositories with at least one mechanism TRUE
    mask = repos[MECHANISMS].any(axis=1)
    df = repos.loc[mask].copy()
    logs.info("rq2_config_cooccurrence", f"{len(df):,} repositories have at least one mechanism.")

    # Pairwise statistics
    pairs = list(combinations(MECHANISMS, 2))
    rows = []
    for a, b in pairs:
        both = (df[a] & df[b]).sum()
        only_a = (df[a] & ~df[b]).sum()
        only_b = (~df[a] & df[b]).sum()
        neither = (~df[a] & ~df[b]).sum()
        table = np.array([[both, only_a], [only_b, neither]])
        if testable(table):
            chi2, p_raw, dof, expected = chi2_contingency(table, correction=False)
            test = which_test(table, expected)
            if test == FISHER:
                p_raw = fisher_exact(table, alternative="two-sided").pvalue
            phi = phi_coefficient(int(both), int(only_a), int(only_b), int(neither))
            v = cramers_v(chi2, len(df), 2, 2)
            magnitude = v_magnitude(v)
        else:
            chi2, test, p_raw, phi, v, magnitude = np.nan, "", np.nan, np.nan, np.nan, "not tested"
        rows.append({
            "option_a": a,
            "option_b": b,
            "both": int(both),
            "only_a": int(only_a),
            "only_b": int(only_b),
            "neither": int(neither),
            "chi2": chi2,
            "test": test,
            "p_raw": p_raw,
            "phi": phi,
            "cramers_v": v,
            "v_magnitude": magnitude,
        })

    results = pd.DataFrame(rows)

    # Benjamini-Hochberg correction, over the pairs that were tested
    tested = results["p_raw"].notna()
    results["p_adjusted"], results["significant"] = adjust_tested(results["p_raw"])

    # CSV output
    results.to_csv(OUTPUT_DIR / "config_cooccurrence.csv", index=False)
    logs.info("rq2_config_cooccurrence", f"Wrote config_cooccurrence.csv ({len(results):,} rows).")

    # Logged summary
    sig = results[results["significant"]]
    fisher = int((results["test"] == FISHER).sum())
    logs.info("rq2_config_cooccurrence",
              f"{len(sig):,} of the {int(tested.sum()):,} tested pairs of mechanisms are "
              f"significant ({len(results):,} pairs in all). {fisher:,} pairs were tested with "
              f"Fisher's exact test.")
    for _, r in sig.iterrows():
        logs.info("rq2_config_cooccurrence", f"  {r['option_a']} and {r['option_b']} "
                                             f"(phi={r['phi']:.3f}, p_adj={r['p_adjusted']:.4f}).")

    # LaTeX table (symmetric matrix, one row and column per mechanism)
    # Upper triangle: the phi coefficient (its absolute value is Cramér's V)
    # Lower triangle: adjusted p-values with significance stars, and a dagger for Fisher's test
    # Diagonal: number of repositories with that mechanism
    # Bold: medium or large effect size (|phi| = V >= 0.3, Cohen 1988)

    # Build lookup dicts
    phi_lookup = {}
    p_lookup = {}
    test_lookup = {}
    for _, r in results.iterrows():
        key = (r["option_a"], r["option_b"])
        phi_lookup[key] = r["phi"]
        p_lookup[key] = r["p_adjusted"]
        test_lookup[key] = r["test"]

    option_n = {col: int(df[col].sum()) for col in MECHANISMS}

    col_headers = " & ".join(
        f"\\textbf{{{latex_text(MECHANISM_LABELS[c])}}}" for c in MECHANISMS
    )

    tex_lines = []
    tex_lines.append("\\begin{table*}[t]")
    tex_lines.append("\\centering")
    tex_lines.append("\\small")
    tex_lines.append("\\caption{Co-occurrence of configuration mechanisms among the repositories "
                      "with at least one. "
                      "Upper triangle: $\\phi$, negative where two mechanisms occur together less "
                      "often than expected under independence, with Cramér's V as its absolute "
                      "value. Lower triangle: BH-adjusted $p$-values. "
                      "Diagonal: number of repositories with the mechanism.}")
    tex_lines.append("\\label{tab:config-cooccurrence}")
    tex_lines.append(f"\\begin{{tabular}}{{l{'r' * len(MECHANISMS)}}}")
    tex_lines.append("\\toprule")
    tex_lines.append(f" & {col_headers} \\\\")
    tex_lines.append("\\midrule")

    for i, opt_i in enumerate(MECHANISMS):
        cells = []
        for j, opt_j in enumerate(MECHANISMS):
            if i == j:
                # Diagonal: count
                cells.append(f"($n$\\,=\\,{option_n[opt_i]:,})")
            elif i < j:
                # Upper triangle: phi with its sign
                phi = phi_lookup[(opt_i, opt_j)]
                cells.append("--" if pd.isna(phi) else association_cell(phi))
            else:
                # Lower triangle: adjusted p-value with stars
                key = (opt_j, opt_i)
                p = p_lookup[key]
                cells.append("--" if pd.isna(p) else p_cell(p, test_lookup[key]))

        label = latex_text(MECHANISM_LABELS[opt_i])
        tex_lines.append(f"\\textbf{{{label}}} & {' & '.join(cells)} \\\\")

    tex_lines.append("\\bottomrule")
    tex_lines.append("\\end{tabular}")

    # Table note
    tex_lines.append("\\vspace{2pt}")
    tex_lines.append("\\begin{minipage}{\\textwidth}")
    tex_lines.append("\\centering")
    tex_lines.append("\\footnotesize")
    tex_lines.append("\\textit{Note:} "
                      "Chi-squared test, or Fisher's exact test ($^{\\dagger}$) where a cell has "
                      f"fewer than {MIN_CELL_COUNT} observed or expected repositories, "
                      f"with Benjamini--Hochberg FDR correction ({int(tested.sum())} comparisons, "
                      f"$N$\\,=\\,{len(df):,} repositories with at least one mechanism). "
                      + ("--: not tested, since no repository or every repository has the mechanism. "
                         if not tested.all() else "") +
                      "Bold: $|\\phi| = V \\geq 0.3$ (medium effect, Cohen 1988). "
                      "$^{*}$\\,$p<.05$, $^{**}$\\,$p<.01$, $^{***}$\\,$p<.001$.")
    tex_lines.append("\\end{minipage}")
    tex_lines.append("\\end{table*}")

    tex = "\n".join(tex_lines) + "\n"
    (OUTPUT_DIR / "config_cooccurrence.tex").write_text(tex)
    logs.info("rq2_config_cooccurrence", "Wrote config_cooccurrence.tex.")


if __name__ == "__main__":
    main()
