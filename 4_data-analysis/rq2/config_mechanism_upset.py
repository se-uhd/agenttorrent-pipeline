"""UpSet plot of configuration mechanism co-occurrence across repositories.

Computes per-repository configuration-mechanism membership across the ten mechanisms (Context
Files, Settings, Skills, Subagents, Commands, Hooks, Rules, MCP, Plugins, LSP), then renders an
UpSet-style plot of the most common intersections. Its population, the `n` of the title, is the
repositories with at least one mechanism.

Input:
  ../../data/collection/output/repos.csv

Output:
  config_mechanism_upset.pdf
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import MECHANISM_LABELS as CONFIG_LABEL_MAP
from _shared import DATA_DIR, UPSET_TOP_N, compute_intersections, output_dir, plot_upset
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

# The order of the mechanisms in the plot, which is the column order of repos.csv.
CONFIG_COLUMNS = ["context_file", "settings", "skills", "subagents", "commands", "hooks",
                  "rules", "mcp", "plugins", "lsp"]
MECHANISM_LABELS = [CONFIG_LABEL_MAP[c] for c in CONFIG_COLUMNS]
# The default of `--min-count`, which plots every intersection that some repository has.
MIN_SUBSET_SIZE = 1


def main() -> None:
    """Build the configuration-mechanism UpSet plot from repos.csv."""
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(
        description="Create an UpSet plot of configuration mechanism co-occurrence.",
        allow_abbrev=False,
    )
    parser.add_argument("--top-n", type=int, default=UPSET_TOP_N,
                        help="Number of intersections to plot (default: %(default)s)")
    parser.add_argument("--min-count", type=int, default=MIN_SUBSET_SIZE,
                        help="Minimum number of repositories in a plotted intersection "
                             "(default: %(default)s)")
    parser.add_argument("--show", action="store_true", help="Display the plot interactively")
    args = parser.parse_args()

    df = pd.read_csv(DATA_DIR / "repos.csv")
    for col in CONFIG_COLUMNS:
        df[col] = df[col].apply(lambda v: str(v).upper() == "TRUE")

    # The population is the repositories with a mechanism, which the bars partition. A row of
    # repos.csv with none, such as a row with only an `other` configuration artifact, is in no
    # bar, and counting it in `n` would state more repositories than the figure draws.
    n_repos = int(df[CONFIG_COLUMNS].any(axis=1).sum())
    intersections = compute_intersections(df[CONFIG_COLUMNS], CONFIG_COLUMNS, drop_empty=True)

    output_path = OUTPUT_DIR / "config_mechanism_upset.pdf"
    plot_upset(
        intersections,
        MECHANISM_LABELS,
        title=f"Configuration Mechanism Usage Across Repositories (n={n_repos:,})",
        output_path=output_path,
        top_n=args.top_n,
        min_count=args.min_count,
        show=args.show,
        y_axis_log_scale=True,
    )
    logs.info("rq2_config_mechanism_upset", f"Wrote {output_path}.")


if __name__ == "__main__":
    main()
