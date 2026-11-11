"""Distribution of repositories by number of configured agentic tools.

Counts how many of the six tools (Claude, Copilot, Cursor, Gemini, Codex, OpenCode) each
repository configures, then summarizes the distribution as a CSV, a PDF bar chart and a LaTeX
table. Repositories with `tool_count = 0` have no named tool flag.
The repositories among them with no specific tool, which have only shared
configuration artifacts such as AGENTS.md, are logged separately.

Input:
  ../../data/collection/output/repos.csv

Outputs (in ../../data/analysis/rq2/):
  tool_count_distribution.csv
  tool_count_distribution.pdf
  tool_count_distribution.tex
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import seaborn as sns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, NO_SPECIFIC_TOOL, output_dir, save_figure
from _shared import SPECIFIC_TOOLS as TOOLS
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The decimals to which the shares are rounded before they are written.
SHARE_DECIMALS = 1


def main() -> None:
    """Write the distribution of the tool counts as a CSV, a PDF and a LaTeX table."""
    # Load data
    repos = pd.read_csv(DATA_DIR / "repos.csv")
    logs.info("rq2_tool_count_distribution", f"Loaded {len(repos):,} rows from repos.csv.")

    for col in TOOLS:
        repos[col] = repos[col].apply(lambda v: str(v).upper() == "TRUE")

    # Read the separate category of repositories with no specific tool.
    if NO_SPECIFIC_TOOL in repos.columns:
        repos[NO_SPECIFIC_TOOL] = repos[NO_SPECIFIC_TOOL].apply(lambda v: str(v).upper() == "TRUE")
    else:
        repos[NO_SPECIFIC_TOOL] = False

    # Compute tool count per repo
    repos["tool_count"] = repos[TOOLS].sum(axis=1)

    # Include repositories without a named tool flag.
    dist = (
        repos["tool_count"]
        .value_counts()
        .reindex(range(0, len(TOOLS) + 1), fill_value=0)
        .sort_index()
    )

    total = dist.sum()
    pct = (dist / total * 100).round(SHARE_DECIMALS)

    summary = pd.DataFrame({
        "num_tools": dist.index,
        "num_repos": dist.values,
        "pct": pct.values,
    })

    n_no_specific_tool = int(repos[NO_SPECIFIC_TOOL].sum())
    logs.info("rq2_tool_count_distribution", f"Repositories by number of tools (N={total:,}).")
    logs.info("rq2_tool_count_distribution", summary.to_string(index=False))
    logs.info("rq2_tool_count_distribution", f"{n_no_specific_tool:,} repositories have none of "
                                             f"the six tool flags and only shared configuration "
                                             f"artifacts.")

    # CSV output
    summary.to_csv(OUTPUT_DIR / "tool_count_distribution.csv", index=False)
    logs.info("rq2_tool_count_distribution", "Wrote tool_count_distribution.csv.")

    # Bar chart
    sns.set_style("whitegrid")
    fig, ax = plt.subplots(figsize=(6, 4))

    bars = ax.bar(
        summary["num_tools"],
        summary["num_repos"],
        color=sns.color_palette("muted", len(summary)),
        edgecolor="black",
        linewidth=0.5,
    )

    for bar, count, p in zip(bars, summary["num_repos"], summary["pct"]):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + total * 0.01,
            f"{count:,}\n({p}%)",
            ha="center",
            va="bottom",
            fontsize=9,
        )

    ax.set_xlabel("Number of Named Tools Configured")
    ax.set_ylabel("Number of Repositories")
    ax.set_xticks(summary["num_tools"])
    xlabels = [str(int(x)) for x in summary["num_tools"]]
    ax.set_xticklabels(xlabels)
    ax.yaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.set_ylim(0, summary["num_repos"].max() * 1.20)

    fig.tight_layout()
    save_figure(fig, OUTPUT_DIR / "tool_count_distribution.pdf")
    logs.info("rq2_tool_count_distribution", "Wrote tool_count_distribution.pdf.")
    plt.close(fig)

    # LaTeX table
    tex_lines = [
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Distribution of repositories by number of configured named tools.}",
        "\\label{tab:tool-count-distribution}",
        "\\begin{tabular}{crr}",
        "\\toprule",
        "\\textbf{\\# Tools} & \\textbf{Repos} & \\textbf{\\%} \\\\",
        "\\midrule",
    ]

    for _, row in summary.iterrows():
        n = int(row['num_tools'])
        label = str(n)
        tex_lines.append(
            f"{label} & {int(row['num_repos']):,} & {row['pct']:.1f}\\% \\\\"
        )

    tex_lines += [
        "\\midrule",
        f"\\textbf{{Total}} & \\textbf{{{total:,}}} & \\textbf{{100.0\\%}} \\\\",
        "\\bottomrule",
        "\\end{tabular}",
        "\\end{table}",
    ]

    tex = "\n".join(tex_lines) + "\n"
    (OUTPUT_DIR / "tool_count_distribution.tex").write_text(tex)
    logs.info("rq2_tool_count_distribution", "Wrote tool_count_distribution.tex.")


if __name__ == "__main__":
    main()
