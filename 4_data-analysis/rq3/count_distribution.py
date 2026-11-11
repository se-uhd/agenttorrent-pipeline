"""Distribution of Context Files, Skills, and Subagents per repository.

Shows how many context files, skills and subagents a repository has, as a grouped bar chart
with percentages on the y-axis. Counts 1..7 get individual bars, and 8+ is a single overflow
bin.

Inputs:
  ../../data/collection/output/context-files.csv
  ../../data/collection/output/skills.csv
  ../../data/collection/output/subagents.csv

Output:
  count_distribution.pdf
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, output_dir, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

# The colors of `_shared.COLORS`: Skills and Subagents as the adoption timeline draws
# them, and the CLAUDE.md blue for Context Files
COLORS = {
    "Context Files": "#1f77b4",
    "Skills": "#17becf",
    "Subagents": "#bcbd22",
}

MAX_INDIVIDUAL = 7  # show bars for 1..7, then "8+"
# The bin of every count above MAX_INDIVIDUAL.
OVERFLOW_BIN = MAX_INDIVIDUAL + 1


def count_per_repo(csv_path: Path, item_col: str) -> pd.Series:
    """Count rows per repo_name, returning a Series indexed by repo_name."""
    df = pd.read_csv(csv_path, usecols=["repo_name", item_col])
    return df.groupby("repo_name").size()


def build_distribution(counts: pd.Series) -> pd.Series:
    """Convert per-repo counts into a distribution over bins 1..7, 8+."""
    clipped = counts.clip(upper=OVERFLOW_BIN)
    dist = clipped.value_counts().sort_index()
    # Ensure all bins 1..8 exist
    dist = dist.reindex(range(1, OVERFLOW_BIN + 1), fill_value=0)
    return dist


def main() -> None:
    """Build and save the distribution bar chart."""
    # Load per-repo counts
    categories = {
        "Context Files": count_per_repo(DATA_DIR / "context-files.csv", "context_file"),
        "Skills": count_per_repo(DATA_DIR / "skills.csv", "skills.md"),
        "Subagents": count_per_repo(DATA_DIR / "subagents.csv", "subagent"),
    }

    # Log the summary statistics
    logs.info("rq3_count_distribution", "Configuration artifacts per repository, over the "
                                        "repositories with at least one.")
    for label, counts in categories.items():
        logs.info("rq3_count_distribution", f"  {label} (n={len(counts):,})  min={counts.min():,}, "
                                            f"max={counts.max():,}, median={counts.median():.0f}, "
                                            f"mean={counts.mean():.2f}.")

    # Build distributions as percentages
    distributions = {}
    for label, counts in categories.items():
        dist = build_distribution(counts)
        distributions[label] = dist / len(counts) * 100

    bin_labels = [str(i) for i in range(1, MAX_INDIVIDUAL + 1)] + [f"{OVERFLOW_BIN}+"]

    # Evenly spaced positions, one per bin, no gap before 8+
    positions = list(range(MAX_INDIVIDUAL + 1))  # 0..7

    n_cats = len(categories)
    bar_width = 0.25
    offsets = np.arange(n_cats) - (n_cats - 1) / 2

    plt.rcParams.update({"font.size": 15})
    fig, ax = plt.subplots(figsize=(12, 5))

    for i, (label, dist) in enumerate(distributions.items()):
        x = np.array(positions) + offsets[i] * bar_width
        ax.bar(
            x,
            dist.values,
            width=bar_width,
            label=f"{label} (n={len(categories[label]):,})",
            color=COLORS[label],
            edgecolor="white",
            linewidth=0.5,
        )

    ax.set_xticks(positions)
    ax.set_xticklabels(bin_labels)
    ax.set_xlabel("Number of Configuration Artifacts per Repository")
    ax.set_ylabel("Percentage of Repositories")
    ax.set_title("Distribution of Context Files, Skills, and Subagents per Repository")
    ax.legend()
    ax.set_axisbelow(True)
    ax.grid(True, axis="y", alpha=0.3)

    output_path = OUTPUT_DIR / "count_distribution.pdf"
    save_figure(fig, output_path)
    logs.info("rq3_count_distribution", f"Wrote {output_path}.")
    plt.close(fig)


if __name__ == "__main__":
    main()
