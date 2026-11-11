"""Configuration artifact and repository counts per mechanism.

Reads each mechanism CSV among the output CSVs of the collection stage and draws, per mechanism,
the number of detected configuration artifacts and the number of distinct repositories that
contain at least one configuration artifact. Every bar shows its count, including a bar at zero.
The figure describes the current dataset release.

Inputs:
  ../../data/collection/output/{context-files,skills,commands,subagents,rules,settings,hooks,mcp,plugins,lsp}.csv

Output:
  artifact_distribution.pdf
"""

import csv
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, configure_plot_style, output_dir, save_figure

from agentcontext import logs

# The component of this script's log messages in the pipeline's one log format,
# `LEVEL component: message`. agentcontext/logs.py and docs/logs.md define the format.
COMPONENT = "dataset_artifact_distribution"

OUTPUT_DIR = output_dir(__file__)

MECHANISM_CSVS = {
    "Context Files": "context-files.csv",
    "Skills": "skills.csv",
    "Commands": "commands.csv",
    "Subagents": "subagents.csv",
    "Rules": "rules.csv",
    "Settings": "settings.csv",
    "Hooks": "hooks.csv",
    "MCP": "mcp.csv",
    "Plugins": "plugins.csv",
    "LSP": "lsp.csv",
}


def load_artifact_stats() -> tuple[dict, int]:
    """Load configuration artifact and distinct repository counts per mechanism."""
    stats = {}
    all_repos = set()
    for label, fname in MECHANISM_CSVS.items():
        fpath = DATA_DIR / fname
        # An absent CSV stops the script rather than counting as zero, because an all-zero
        # figure is byte-reproducible and would pass the reproduction check. A CSV without a
        # `repo_name` column is not a mechanism CSV. The other analysis scripts stop on a
        # missing output CSV too.
        if not fpath.is_file():
            raise SystemExit(f"{fpath} is missing. Run `python3 -m agentcontext detection` first.")
        with open(fpath, encoding="utf-8", errors="ignore") as f:
            reader = csv.DictReader(f)
            if "repo_name" not in (reader.fieldnames or []):
                raise SystemExit(f"{fpath} has no repo_name column and is not a mechanism CSV.")
            rows = list(reader)
        repos = set(r["repo_name"] for r in rows if "repo_name" in r)
        all_repos.update(repos)
        stats[label] = {"configuration_artifacts": len(rows), "repos": len(repos)}
    return stats, len(all_repos)


def main() -> None:
    """Save the configuration artifact and repository counts as a dual bar chart."""
    configure_plot_style()

    import seaborn as sns

    stats, n_repos = load_artifact_stats()
    labels = list(stats.keys())
    configuration_artifacts = [stats[l]["configuration_artifacts"] for l in labels]
    repos = [stats[l]["repos"] for l in labels]
    total_configuration_artifacts = sum(configuration_artifacts)
    colors = ["#666666", "#bbbbbb"]

    # Reversed, so that the first mechanism of MECHANISM_CSVS is at the top
    labels, configuration_artifacts, repos = labels[::-1], configuration_artifacts[::-1], repos[::-1]

    y = np.arange(len(labels))
    height = 0.35

    fig, ax = plt.subplots(figsize=(8, 5))

    configuration_artifact_bars = ax.barh(
        y + height / 2, configuration_artifacts, height,
        label="Configuration artifacts", color=colors[0], edgecolor="black", linewidth=0.5)
    repository_bars = ax.barh(
        y - height / 2, repos, height,
        label="Repositories", color=colors[1], edgecolor="black", linewidth=0.5)

    # Count labels (no percentages), a zero included, because an unlabeled empty bar cannot be
    # told from a mechanism that the figure left out.
    for bar in list(configuration_artifact_bars) + list(repository_bars):
        v = int(bar.get_width())
        ax.text(
            bar.get_width() + max(configuration_artifacts) * 0.01,
            bar.get_y() + bar.get_height() / 2,
            f"{v:,}",
            va="center", ha="left", fontsize=9,
        )

    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel("Count")
    ax.set_ylabel("Configuration Mechanism")
    ax.set_xlim(0, max(configuration_artifacts) * 1.2)
    ax.legend(fontsize=10, loc="lower right")
    ax.set_title(
        f"Configuration Artifact Distribution\n"
        f"(n = {total_configuration_artifacts:,} configuration artifacts from {n_repos:,} repositories)",
        fontsize=14,
    )
    ax.yaxis.grid(False)

    fig.tight_layout()
    output_path = OUTPUT_DIR / "artifact_distribution.pdf"
    save_figure(fig, output_path)
    plt.close(fig)
    logs.info(COMPONENT, f"Wrote {output_path}.")


if __name__ == "__main__":
    main()
