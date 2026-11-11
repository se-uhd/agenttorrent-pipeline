"""Configuration mechanism adoption per agentic tool, as a heatmap.

For each (configuration mechanism, tool) cell, counts the repositories with a
configuration artifact of the mechanism that is attributed to the tool, by the `tool` column of
the mechanism CSV, and the percentage relative to that tool's repository count. A last column
counts the mechanisms of the repositories with no specific tool, which have only shared
configuration artifacts such as AGENTS.md. Zero cells are shown in light gray, so that they
stay readable on the YlOrRd palette.

Input:
  ../../data/collection/output/repos.csv
  ../../data/collection/output/<mechanism>.csv, the ten mechanism CSVs

Output:
  config_tool_heatmap.pdf
"""

import sys
from pathlib import Path

import matplotlib.patches as patches
import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import MECHANISM_LABELS as CONFIG_LABEL_MAP
from _shared import MECHANISMS, DATA_DIR, TOOL_LABELS, output_dir
from _shared import MECHANISM_CSV_NAMES, NO_SPECIFIC_TOOL, SPECIFIC_TOOLS, mechanism_tool_counts, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

MECHANISM_LABELS = [CONFIG_LABEL_MAP[c] for c in MECHANISMS]
# The six tools, then the repositories that have only shared configuration artifacts.
TOOLS = SPECIFIC_TOOLS + [NO_SPECIFIC_TOOL]


def main() -> None:
    """Build and save the configuration-mechanism-per-tool heatmap."""
    df = pd.read_csv(DATA_DIR / "repos.csv")
    for col in TOOLS + MECHANISMS:
        df[col] = df[col].apply(lambda v: str(v).upper() == "TRUE")
    dataframes = {m: pd.read_csv(DATA_DIR / MECHANISM_CSV_NAMES[m], usecols=["repo_name", "tool"],
                             dtype=str, keep_default_na=False)
              for m in MECHANISMS}
    cells, totals = mechanism_tool_counts(df, dataframes)

    # Build count matrix (rows = mechanisms, cols = tools)
    counts = pd.DataFrame(
        index=MECHANISM_LABELS,
        columns=[TOOL_LABELS[t] for t in TOOLS],
        dtype=int,
    )
    for i, cfg in enumerate(MECHANISMS):
        for j, tool in enumerate(TOOLS):
            counts.iloc[i, j] = cells[(cfg, tool)]

    # Tool totals: number of repositories per tool
    tool_totals = {TOOL_LABELS[t]: totals[t] for t in TOOLS}

    # Percentage matrix: count / tool_total * 100
    pct_matrix = counts.copy().astype(float)
    for col in counts.columns:
        total = tool_totals[col]
        pct_matrix[col] = counts[col] / total * 100 if total > 0 else 0
    pct_matrix = pct_matrix.fillna(0)

    # Annotation strings
    annot_labels = counts.copy().astype(str)
    for col in counts.columns:
        for row in counts.index:
            count = int(counts.loc[row, col])
            total = tool_totals[col]
            pct = count / total * 100 if total > 0 else 0
            annot_labels.loc[row, col] = f"{count:,}\n({pct:.1f}%)"

    fig, ax = plt.subplots(figsize=(10.5, 7))
    sns.heatmap(
        pct_matrix,
        annot=annot_labels,
        fmt="",
        cmap="YlOrRd",
        linewidths=0.5,
        ax=ax,
        annot_kws={"size": 11},
    )

    # Gray out zero cells
    for i in range(len(MECHANISM_LABELS)):
        for j in range(len(TOOLS)):
            if counts.iloc[i, j] == 0:
                ax.add_patch(
                    patches.Rectangle(
                        (j, i),
                        1,
                        1,
                        facecolor="#f0f0f0",
                        edgecolor="white",
                        linewidth=0.5,
                        zorder=2,
                    )
                )
                ax.text(
                    j + 0.5,
                    i + 0.5,
                    annot_labels.iloc[i, j],
                    ha="center",
                    va="center",
                    fontsize=11,
                    color="black",
                    zorder=3,
                )

    tool_labels_with_n = [
        f"{TOOL_LABELS[t]}\n(n={tool_totals[TOOL_LABELS[t]]:,})" for t in TOOLS
    ]
    n_repos = len(df)

    ax.set_title(
        f"Configuration Mechanisms per Agentic AI Tool (n = {n_repos:,})",
        fontsize=14,
    )
    ax.set_ylabel("")
    ax.set_yticklabels(MECHANISM_LABELS, rotation=0, fontsize=12)
    # Each column label holds a tool name over its count, and horizontal labels of neighboring
    # columns, such as "OpenCode" and "No specific tool", run into each other.
    ax.set_xticklabels(tool_labels_with_n, rotation=45, ha="right", rotation_mode="anchor",
                       fontsize=12)
    plt.tight_layout()
    save_figure(fig, OUTPUT_DIR / "config_tool_heatmap.pdf")
    plt.close(fig)
    logs.info("rq2_config_tool_heatmap", "Wrote config_tool_heatmap.pdf.")


if __name__ == "__main__":
    main()
