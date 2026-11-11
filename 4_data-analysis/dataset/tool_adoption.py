"""Tool adoption: repository count per tool.

Renders a horizontal bar chart with the number and percentage of repositories that adopted
each tool, and of the repositories with no specific tool, which have only shared
configuration artifacts such as AGENTS.md. The figure describes the current dataset release.

Input:
  ../../data/collection/output/repos.csv

Output:
  tool_adoption.pdf
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, TOOL_LABELS, TOOLS, configure_plot_style, output_dir, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The decimals of the share in the label of a bar.
SHARE_DECIMALS = 1


def main() -> None:
    """Render the tool-adoption bar chart and save it as a PDF."""
    configure_plot_style()

    df = pd.read_csv(DATA_DIR / "repos.csv")
    for col in TOOLS:
        df[col] = df[col].apply(lambda v: str(v).upper() == "TRUE")

    n_repos = len(df)
    tool_counts = {TOOL_LABELS[t]: int(df[t].sum()) for t in TOOLS}

    labels = list(tool_counts.keys())
    counts = list(tool_counts.values())

    # Reversed, so that the first tool of TOOLS is at the top
    labels, counts = labels[::-1], counts[::-1]

    y = np.arange(len(labels))
    height = 0.5

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.barh(y, counts, height, color="0.75", edgecolor="black", linewidth=0.5)

    tallest = max(counts)
    for bar, count in zip(bars, counts):
        pct = 100 * count / n_repos
        ax.text(
            bar.get_width() + tallest * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{count:,} ({pct:.{SHARE_DECIMALS}f}%)",
            va="center", ha="left", fontsize=9,
        )

    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Number of Repositories")
    ax.set_ylabel("Tool")
    ax.set_xlim(0, tallest * 1.25)
    ax.set_title(
        f"Tool Adoption Across Repositories (n = {n_repos:,})",
        fontsize=14,
    )
    ax.yaxis.grid(False)

    fig.tight_layout()
    output_path = OUTPUT_DIR / "tool_adoption.pdf"
    save_figure(fig, output_path)
    plt.close(fig)
    logs.info("dataset_tool_adoption", f"Wrote {output_path}.")


if __name__ == "__main__":
    main()
