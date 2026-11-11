"""Pairwise co-occurrence of the tools across repositories.

For every pair of tools, counts the repositories that use both, only the first,
only the second, and neither, then writes:

  - tool_cooccurrence.csv: one row per pair with the four counts
  - tool_cooccurrence.pdf: heatmap (count + percent of the smaller tool)
  - tool_cooccurrence.tex: 7x7 LaTeX table (upper = count, lower = %,
    diagonal = tool total)

Input:
  ../../data/collection/output/repos.csv

Outputs (in ../../data/analysis/rq2/):
  tool_cooccurrence.csv
  tool_cooccurrence.pdf
  tool_cooccurrence.tex
"""

from itertools import combinations
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, TOOL_LABELS, TOOLS, latex_text, output_dir, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The pairs with the most repositories that configure both tools, which the summary logs.
TOP_PAIRS = 5
# The share of the largest count above which a label of the heatmap is white, so that it stays
# readable on the dark cells.
WHITE_LABEL_SHARE = 0.6


def main() -> None:
    """Count the pairwise co-occurrence of the tools and write the CSV, the PDF and the LaTeX
    table."""
    # Load data
    repos = pd.read_csv(DATA_DIR / "repos.csv")
    logs.info("rq2_tool_cooccurrence", f"Loaded {len(repos):,} rows from repos.csv.")

    for col in TOOLS:
        repos[col] = repos[col].apply(lambda v: str(v).upper() == "TRUE")

    n = len(repos)

    # Pairwise co-occurrence
    tool_n = {t: int(repos[t].sum()) for t in TOOLS}

    pairs = list(combinations(TOOLS, 2))
    rows = []
    for a, b in pairs:
        both = int((repos[a] & repos[b]).sum())
        only_a = int((repos[a] & ~repos[b]).sum())
        only_b = int((~repos[a] & repos[b]).sum())
        neither = int((~repos[a] & ~repos[b]).sum())
        rows.append({
            "tool_a": a,
            "tool_b": b,
            "both": both,
            "only_a": only_a,
            "only_b": only_b,
            "neither": neither,
        })

    results = pd.DataFrame(rows)

    # CSV output
    results.to_csv(OUTPUT_DIR / "tool_cooccurrence.csv", index=False)
    logs.info("rq2_tool_cooccurrence", f"Wrote tool_cooccurrence.csv ({len(results):,} rows).")

    # Build matrices for heatmap
    count_matrix = pd.DataFrame(0, index=TOOLS, columns=TOOLS, dtype=int)
    pct_matrix = pd.DataFrame(0.0, index=TOOLS, columns=TOOLS)

    # Diagonal: total repositories per tool
    for t in TOOLS:
        count_matrix.loc[t, t] = tool_n[t]
        pct_matrix.loc[t, t] = 100.0

    # Off-diagonal: co-occurrence count and percentage of the smaller tool's repositories
    for _, r in results.iterrows():
        a, b, both = r["tool_a"], r["tool_b"], r["both"]
        count_matrix.loc[a, b] = both
        count_matrix.loc[b, a] = both
        min_n = min(tool_n[a], tool_n[b])
        p = (both / min_n * 100) if min_n > 0 else 0.0
        pct_matrix.loc[a, b] = p
        pct_matrix.loc[b, a] = p

    # Logged summary
    logs.info("rq2_tool_cooccurrence", f"Repositories per tool (N={n:,}).")
    for t in TOOLS:
        logs.info("rq2_tool_cooccurrence", f"  {TOOL_LABELS[t]:<16} {tool_n[t]:>7,}")
    logs.info("rq2_tool_cooccurrence",
              "Pairs of tools with the most repositories that configure both.")
    top = results.nlargest(TOP_PAIRS, "both")
    for _, r in top.iterrows():
        min_n = min(tool_n[r["tool_a"]], tool_n[r["tool_b"]])
        pct = r["both"] / min_n * 100 if min_n > 0 else 0
        logs.info("rq2_tool_cooccurrence", f"  {r['both']:,} repositories configure "
                                           f"{TOOL_LABELS[r['tool_a']]} and "
                                           f"{TOOL_LABELS[r['tool_b']]} ({pct:.1f}% of the smaller "
                                           f"tool's repositories).")

    # Heatmap
    labels = [TOOL_LABELS[t] for t in TOOLS]

    sns.set_style("whitegrid")
    fig, ax = plt.subplots(figsize=(7, 5.5))

    sns.heatmap(
        count_matrix.values.astype(float),
        annot=False,
        cmap="YlOrRd",
        xticklabels=labels,
        yticklabels=labels,
        linewidths=0.5,
        linecolor="white",
        ax=ax,
        cbar_kws={"label": "Number of repositories"},
    )

    # Annotate each cell with count + percentage
    for i in range(len(TOOLS)):
        for j in range(len(TOOLS)):
            cnt = count_matrix.iloc[i, j]
            pct = pct_matrix.iloc[i, j]
            if i == j:
                text = f"n={cnt:,}"
            else:
                text = f"{cnt:,}\n({pct:.1f}%)"
            ax.text(
                j + 0.5, i + 0.5, text,
                ha="center", va="center", fontsize=8,
                color="white" if cnt > count_matrix.values.max() * WHITE_LABEL_SHARE else "black",
            )

    ax.set_xticklabels(labels, rotation=45, ha="right")
    fig.tight_layout()
    save_figure(fig, OUTPUT_DIR / "tool_cooccurrence.pdf")
    logs.info("rq2_tool_cooccurrence", "Wrote tool_cooccurrence.pdf.")
    plt.close(fig)

    # LaTeX table (7x7 symmetric matrix, the six tools and no specific tool)
    # Upper triangle: co-occurrence count
    # Lower triangle: % of the smaller tool's repositories
    # Diagonal: n
    col_headers = " & ".join(f"\\textbf{{{latex_text(TOOL_LABELS[t])}}}" for t in TOOLS)

    tex_lines = [
        "\\begin{table}[t]",
        "\\centering",
        "\\small",
        "\\caption{Tool co-occurrence matrix. "
        "Upper triangle: number of repositories that use both tools. "
        "Lower triangle: percentage of the repositories of the smaller tool. "
        "Diagonal: number of repositories per tool.}",
        "\\label{tab:tool-cooccurrence}",
        f"\\begin{{tabular}}{{l{'r' * len(TOOLS)}}}",
        "\\toprule",
        f" & {col_headers} \\\\",
        "\\midrule",
    ]

    for i, ti in enumerate(TOOLS):
        cells = []
        for j, tj in enumerate(TOOLS):
            if i == j:
                cells.append(f"($n$\\,=\\,{tool_n[ti]:,})")
            elif i < j:
                # Upper triangle: count
                cells.append(f"{count_matrix.loc[ti, tj]:,}")
            else:
                # Lower triangle: percentage
                cells.append(f"{pct_matrix.loc[ti, tj]:.1f}\\%")
        tex_lines.append(
            f"\\textbf{{{latex_text(TOOL_LABELS[ti])}}} & {' & '.join(cells)} \\\\"
        )

    tex_lines += [
        "\\bottomrule",
        "\\end{tabular}",
        f"\\vspace{{2pt}}",
        "\\begin{minipage}{\\columnwidth}",
        "\\centering",
        "\\footnotesize",
        f"\\textit{{Note:}} $N$\\,=\\,{n:,} repositories.",
        "\\end{minipage}",
        "\\end{table}",
    ]

    tex = "\n".join(tex_lines) + "\n"
    (OUTPUT_DIR / "tool_cooccurrence.tex").write_text(tex)
    logs.info("rq2_tool_cooccurrence", "Wrote tool_cooccurrence.tex.")


if __name__ == "__main__":
    main()
