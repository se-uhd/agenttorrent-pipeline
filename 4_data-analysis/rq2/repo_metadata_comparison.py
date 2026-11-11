"""Per-tool repository metadata comparison (age, contributors, commits, size).

For each agentic tool, compares the repositories that adopt the tool with the complement
(non-adopters) in a two-sided Mann-Whitney U test on four metrics. Writes adjusted p-values
(Benjamini-Hochberg over the comparisons that were tested, 28 when no group is empty), medians,
IQRs, and Cliff's delta effect sizes with magnitude classification. A tool that no repository
or every repository adopts leaves a group empty, and its comparisons are not tested. Such a
comparison has no U, p-value or delta, and its magnitude is "not tested". The LaTeX table shows
"--" for its delta.

Inputs:
  ../../data/collection/output/repos.csv
  the repository metadata that _repo_metadata.build_repo_metadata() builds

Outputs (in ../../data/analysis/rq2/):
  repo_metadata_comparison.csv
  repo_metadata_comparison.tex
"""

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _repo_metadata import build_repo_metadata
from _shared import DATA_DIR, TOOL_LABELS, TOOLS, adjust_tested, latex_text, output_dir
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

METRICS = ["repo_age_years", "contributors", "commits", "size"]
METRIC_LABELS = {
    "repo_age_years": "Repo Age (years)",
    "contributors": "Contributors",
    "commits": "Commits",
    "size": "Size (KB)",
}
# Romano et al.'s (2006) thresholds of |Cliff's delta|: a delta below the first is negligible,
# below the second small, below the third medium, and from the third on large.
DELTA_THRESHOLDS = (0.147, 0.33, 0.474)
# The adjusted p below which a comparison gets three, two and one stars.
SIGNIFICANCE_LEVELS = (0.001, 0.01, 0.05)
# A number of the table is written with DECIMALS decimals below DECIMAL_BELOW, in thousands with
# a `k` from THOUSANDS_FROM, and as a whole number in between.
DECIMALS = 1
DECIMAL_BELOW = 10
THOUSANDS_FROM = 10_000


def cliffs_delta(x: np.ndarray, y: np.ndarray) -> float:
    """Cliff's delta effect size between two array-likes, NaN when either is empty."""
    nx, ny = len(x), len(y)
    # The delta is a share of the pairs, and a group without a value has no pair.
    if nx == 0 or ny == 0:
        return np.nan
    # Vectorized comparison via broadcasting
    x_arr = np.asarray(x)[:, None]
    y_arr = np.asarray(y)[None, :]
    more = np.sum(x_arr > y_arr)
    less = np.sum(x_arr < y_arr)
    return (more - less) / (nx * ny)


def delta_magnitude(d: float) -> str:
    """Romano et al. (2006) magnitude classification for Cliff's delta, "not tested" for NaN."""
    if pd.isna(d):
        return "not tested"
    ad = abs(d)
    if ad < DELTA_THRESHOLDS[0]:
        return "negligible"
    if ad < DELTA_THRESHOLDS[1]:
        return "small"
    if ad < DELTA_THRESHOLDS[2]:
        return "medium"
    return "large"


def stars(p: float) -> str:
    """Significance stars based on an (adjusted) p-value."""
    if p < SIGNIFICANCE_LEVELS[0]:
        return "***"
    if p < SIGNIFICANCE_LEVELS[1]:
        return "**"
    if p < SIGNIFICANCE_LEVELS[2]:
        return "*"
    return ""


def _fmt_num(v: float) -> str:
    """Format a number: 'k' suffix for >= 10,000, one decimal for < 10, comma thousands otherwise.

    Uses round-half-up (math.floor(x + 0.5)) instead of Python's default
    banker's rounding. A value that is not a number, such as the median and the
    quartiles of a group with no repository, is written as 'n/a'.
    """
    if pd.isna(v):
        return "n/a"
    if abs(v) >= THOUSANDS_FROM:
        rounded = math.floor(v / 1000 + 0.5)
        return f"{rounded:,}k"
    if abs(v) < DECIMAL_BELOW:
        rounded = math.floor(v * 10 ** DECIMALS + 0.5) / 10 ** DECIMALS
        return f"{rounded:.{DECIMALS}f}"
    return f"{math.floor(v + 0.5):,}"


def fmt_iqr(series: pd.Series) -> str:
    """Format median (Q1--Q3), using 'k' suffix for values >= 10 000."""
    q1, med, q3 = series.quantile([0.25, 0.5, 0.75])
    return f"{_fmt_num(med)} ({_fmt_num(q1)}--{_fmt_num(q3)})"


def fmt_median(series: pd.Series) -> str:
    """Format just the median value."""
    return _fmt_num(series.median())


def fmt_iqr_only(series: pd.Series) -> str:
    """Format just (Q1--Q3) in parentheses."""
    q1, q3 = series.quantile([0.25, 0.75])
    return f"({_fmt_num(q1)}--{_fmt_num(q3)})"


def main() -> None:
    """Compare the adopters of each of the seven flags with the other repositories, and write
    the CSV and the LaTeX table."""
    # Load & merge
    metadata = build_repo_metadata()
    repos = pd.read_csv(DATA_DIR / "repos.csv")

    # Convert tool columns to booleans
    for tool in TOOLS:
        repos[tool] = repos[tool].apply(lambda v: str(v).upper() == "TRUE")

    # Deduplicate repos.csv on repo_name (OR across tool columns)
    if repos["repo_name"].duplicated().any():
        repos = repos.groupby("repo_name", as_index=False).agg(
            {t: "max" for t in TOOLS}
        )

    df = metadata.merge(repos[["repo_name"] + TOOLS], on="repo_name", how="inner")
    df["repo_age_years"] = df["repo_age_days"] / 365.25
    logs.info("rq2_repo_metadata_comparison", f"The comparison covers the {len(df):,} configured "
                                              f"repositories with GHS metadata.")

    # Statistical tests
    rows = []
    for tool in TOOLS:
        n_with = df[tool].sum()
        n_without = len(df) - n_with
        for metric in METRICS:
            with_group = df.loc[df[tool], metric].dropna()
            without_group = df.loc[~df[tool], metric].dropna()
            # Both statistics compare the two groups pair by pair. An empty group has no pair,
            # and the comparison is then not tested.
            if len(with_group) and len(without_group):
                u_stat, p_raw = mannwhitneyu(with_group, without_group, alternative="two-sided")
            else:
                u_stat, p_raw = np.nan, np.nan
            delta = cliffs_delta(with_group.values, without_group.values)
            rows.append({
                "tool": tool,
                "metric": metric,
                "n_with": int(n_with),
                "n_without": int(n_without),
                "median_with": with_group.median(),
                "q1_with": with_group.quantile(0.25),
                "q3_with": with_group.quantile(0.75),
                "median_without": without_group.median(),
                "q1_without": without_group.quantile(0.25),
                "q3_without": without_group.quantile(0.75),
                "U": u_stat,
                "p_raw": p_raw,
                "cliffs_delta": delta,
                "delta_magnitude": delta_magnitude(delta),
            })

    results = pd.DataFrame(rows)

    # Benjamini-Hochberg correction over the comparisons that were tested. A tool that no
    # repository or every repository adopts leaves a group empty and its four tests without a
    # p-value, which `adjust_tested` leaves out of the adjustment.
    results["p_adjusted"], results["significant"] = adjust_tested(results["p_raw"])
    n_tested = int(results["p_raw"].notna().sum())

    # CSV output
    results.to_csv(OUTPUT_DIR / "repo_metadata_comparison.csv", index=False)
    logs.info("rq2_repo_metadata_comparison", f"Wrote repo_metadata_comparison.csv ({len(results):,} rows).")

    # Logged summary
    logs.info("rq2_repo_metadata_comparison",
              "Metrics with a significant difference, per tool flag.")
    for tool in TOOLS:
        sub = results[results["tool"] == tool]
        n = sub.iloc[0]["n_with"]
        sig = sub[sub["significant"]]
        sig_str = ", ".join(sig["metric"]) if len(sig) else "none"
        logs.info("rq2_repo_metadata_comparison", f"  {TOOL_LABELS[tool]:>16} (n={int(n):>4,})  "
                                                  f"{sig_str}")

    # Baseline statistics (all repositories)
    baseline = {}
    for metric in METRICS:
        baseline[metric] = fmt_iqr(df[metric].dropna())

    # LaTeX table (single-column layout)
    # Transposed: tools as rows, metrics as columns.
    # Each tool gets two rows: median (with stars) and IQR.
    tool_n = {tool: int(results.loc[results["tool"] == tool, "n_with"].iloc[0]) for tool in TOOLS}

    METRIC_COL_LABELS = {
        "repo_age_years": "Age (yr)",
        "contributors": "Contrib.",
        "commits": "Commits",
        "size": "Size (KB)",
    }

    col_headers = " & ".join(
        f"\\textbf{{{latex_text(METRIC_COL_LABELS[m])}}}" for m in METRICS
    )

    tex_lines = []
    tex_lines.append("\\begin{table}[t]")
    tex_lines.append("\\centering")
    tex_lines.append("\\small")
    tex_lines.append("\\caption{Repository metadata comparison by tool. "
                      "Cells show the median, the IQR and Cliff's $\\delta$ of the repositories that "
                      "use the tool. "
                      "Significant differences (BH-adjusted) are in bold.}")
    tex_lines.append("\\label{tab:repo-metadata-comparison}")
    tex_lines.append(f"\\begin{{tabular}}{{l{'r' * len(METRICS)}}}")
    tex_lines.append("\\toprule")
    tex_lines.append(f"\\textbf{{Tool}} & {col_headers} \\\\")
    tex_lines.append("\\midrule")

    # Baseline row
    baseline_meds = []
    baseline_iqrs = []
    for metric in METRICS:
        s = df[metric].dropna()
        baseline_meds.append(fmt_median(s))
        baseline_iqrs.append(fmt_iqr_only(s))
    tex_lines.append(f"All & {' & '.join(baseline_meds)} \\\\")
    tex_lines.append(f"($n$={len(df):,}) & {' & '.join(baseline_iqrs)} \\\\")
    tex_lines.append("\\midrule")

    for i, tool in enumerate(TOOLS):
        med_cells = []
        iqr_cells = []
        delta_cells = []
        for metric in METRICS:
            row = results[(results["tool"] == tool) & (results["metric"] == metric)].iloc[0]
            s = stars(row["p_adjusted"])
            sig = row["significant"]
            with_series = df.loc[df[tool], metric].dropna()
            med = fmt_median(with_series)
            if s:
                med += f"$^{{{s}}}$"
            if sig:
                med = f"\\textbf{{{med}}}"
            med_cells.append(med)
            iqr = fmt_iqr_only(with_series)
            if sig:
                iqr = f"\\textbf{{{iqr}}}"
            iqr_cells.append(iqr)
            delta = ("--" if pd.isna(row["cliffs_delta"])
                     else f"$\\delta$={row['cliffs_delta']:.2f}")
            if sig:
                delta = f"\\textbf{{{delta}}}"
            delta_cells.append(delta)
        tex_lines.append(f"{latex_text(TOOL_LABELS[tool])} & {' & '.join(med_cells)} \\\\")
        tex_lines.append(f"($n$={tool_n[tool]:,}) & {' & '.join(iqr_cells)} \\\\")
        tex_lines.append(f" & {' & '.join(delta_cells)} \\\\")
        if i < len(TOOLS) - 1:
            tex_lines.append("\\addlinespace")

    tex_lines.append("\\bottomrule")
    tex_lines.append("\\end{tabular}")

    # Table note
    tex_lines.append("\\vspace{2pt}")
    tex_lines.append("\\begin{minipage}{\\columnwidth}")
    tex_lines.append("\\footnotesize")
    tex_lines.append("\\textit{Note:} "
                      "Mann--Whitney U test (two-sided) with Benjamini--Hochberg FDR correction "
                      f"({n_tested} comparisons). "
                      + ("--: not tested, since no repository or every repository uses the tool. "
                         if n_tested < len(results) else "") +
                      "Effect sizes: Cliff's delta. "
                      "$^{*}$ $p<.05$, $^{**}$ $p<.01$, $^{***}$ $p<.001$ (adjusted).")
    tex_lines.append("\\end{minipage}")
    tex_lines.append("\\end{table}")

    tex = "\n".join(tex_lines) + "\n"
    (OUTPUT_DIR / "repo_metadata_comparison.tex").write_text(tex)
    logs.info("rq2_repo_metadata_comparison", "Wrote repo_metadata_comparison.tex.")


if __name__ == "__main__":
    main()
