"""UpSet plot of agentic tool usage and AGENTS.md adoption.

Renders an UpSet-style plot of how often each combination of Claude, Copilot, Cursor, Gemini,
Codex, OpenCode and AGENTS.md occurs across the repositories that use at least one of them. The
renderer is `_shared.plot_upset`, which `config_mechanism_upset.py` uses too.

Input:
  ../../data/collection/output/repos.csv

Output:
  upset-tool-usage.pdf
"""

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # noqa: E402  (must precede pyplot import)
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import SPECIFIC_TOOLS as TOOLS
from _shared import DATA_DIR, TOOL_LABELS, compute_intersections, output_dir, plot_upset
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

CATS = ["Claude", "Copilot", "Cursor", "Gemini", "Codex", "OpenCode", "AGENTS.md"]
CAT_COLORS = {
    "Claude": "#87ceeb",
    "Copilot": "#ef9a9a",
    "Cursor": "#ffcc80",
    "Gemini": "#fff176",
    "Codex": "#90ee90",
    "OpenCode": "#80cbc4",
    "AGENTS.md": "#ce93d8",
}

# The default of `--min-count`: Intersections smaller than this are not plotted. Over a sample
# with a smaller largest intersection, such as the fixture, a lower `--min-count` lets the
# script draw an intersection at all.
MIN_SUBSET_SIZE = 5


def main(argv: list[str] | None = None) -> None:
    """Build the tool-usage UpSet plot and save it as a PDF."""
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(description="Create an UpSet plot of agentic tool usage.",
                                     allow_abbrev=False)
    parser.add_argument("--min-count", type=int, default=MIN_SUBSET_SIZE,
                        help="Minimum number of repositories in a plotted intersection "
                             "(default: %(default)s)")
    args = parser.parse_args(argv)

    df = pd.read_csv(DATA_DIR / "repos.csv")
    logs.info("rq2_upset_plot", f"Loaded {len(df):,} rows from repos.csv.")

    for col in TOOLS:
        df[col] = df[col].apply(lambda v: str(v).upper() == "TRUE")
    df["agents.md"] = df["agents.md"].apply(lambda v: str(v).upper() == "TRUE")

    # Rename columns for display (scoped to TOOLS, so that the no_specific_tool
    # flag column is not also renamed to AGENTS.md)
    rename = {**{t: TOOL_LABELS[t] for t in TOOLS}, "agents.md": "AGENTS.md"}
    df = df.rename(columns=rename)

    intersections = compute_intersections(df[CATS], CATS, drop_empty=True)
    using = sum(it.count for it in intersections)
    logs.info("rq2_upset_plot", f"{using:,} repositories use at least one of "
                                f"{', '.join(CATS[:-1])} or {CATS[-1]}.")

    output_path = OUTPUT_DIR / "upset-tool-usage.pdf"
    plot_upset(
        intersections,
        CATS,
        title=f"Agentic Tool Usage Across Repositories (n={using:,})",
        output_path=output_path,
        min_count=args.min_count,
        set_colors=CAT_COLORS,
    )
    logs.info("rq2_upset_plot", f"Wrote {output_path}.")


if __name__ == "__main__":
    main()
