"""Six-set Venn diagram of agentic tool usage across repositories.

Loads repos.csv, treats per-tool boolean columns as set-membership, and
draws a Venn diagram for Claude, Copilot, Cursor, Gemini, Codex and OpenCode.
A separate callout box gives the total AGENTS.md count and the count of repositories with
AGENTS.md and none of the six tools' flags.

Input:
  ../../data/collection/output/repos.csv

Output:
  venn-tool-usage.pdf
"""

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # noqa: E402  (must precede pyplot import)
import matplotlib.pyplot as plt
import pandas as pd
import venn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, NO_SPECIFIC_TOOL, output_dir, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

# Every column of `repos.csv` that is not a tool or mechanism flag. `load_repos` coerces
# whatever is left to boolean, and a text column missing from this list would be read as FALSE
# in every row rather than raising. The suite checks the fixture's header against this list.
NON_FLAG_COLUMNS = [
    "branch",
    "scanned_at",
    "head_sha",
    "head_commit_date",
    "github_link",
    "dataset_version",
]


def load_repos() -> pd.DataFrame:
    """Load repos.csv and coerce per-tool columns to bool."""
    df = pd.read_csv(DATA_DIR / "repos.csv")
    logs.info("rq2_venn_diagram", f"Loaded {len(df):,} rows from repos.csv.")

    dropped = [col for col in NON_FLAG_COLUMNS if col in df.columns]
    df.drop(columns=dropped, inplace=True)
    logs.info("rq2_venn_diagram", f"Dropped the columns of repos.csv that are not flags "
                                  f"({', '.join(dropped)}).")

    bool_columns = [col for col in df.columns if col != "repo_name"]
    for col in bool_columns:
        df[col] = df[col].apply(lambda v: str(v).upper() == "TRUE")
    logs.info("rq2_venn_diagram", f"Read the flag columns of repos.csv as booleans "
                                  f"({', '.join(bool_columns)}).")
    return df


def venn_diagram_for_agent_adoption(df: pd.DataFrame) -> None:
    """Render a six-set Venn diagram of tool usage and save it as a PDF."""
    agents = {
        "Claude": set(df[df["claude"] == True]["repo_name"]),  # noqa: E712
        "Copilot": set(df[df["copilot"] == True]["repo_name"]),  # noqa: E712
        "Cursor": set(df[df["cursor"] == True]["repo_name"]),  # noqa: E712
        "Gemini": set(df[df["gemini"] == True]["repo_name"]),  # noqa: E712
        "Codex": set(df[df["codex"] == True]["repo_name"]),  # noqa: E712
        "OpenCode": set(df[df["opencode"] == True]["repo_name"]),  # noqa: E712
    }

    venn.venn(
        agents,
        fontsize=16,
        cmap=["#87ceeb", "#ef9a9a", "#ffcc80", "#fff176", "#90ee90", "#80cbc4"],
        alpha=0.4,
    )

    # Annotate the AGENTS.md repositories: their total count and the count of the repositories
    # among them with no specific tool, which have only shared configuration artifacts.
    agents_md_count = int(df["agents.md"].sum())
    exclusive_count = (
        int((df["agents.md"] & df[NO_SPECIFIC_TOOL]).sum())
        if NO_SPECIFIC_TOOL in df.columns else 0
    )
    if agents_md_count > 0:
        plt.annotate(
            f"AGENTS.md\n(n={agents_md_count}, {exclusive_count} exclusive)",
            xy=(0.02, 0.02),
            xycoords="axes fraction",
            fontsize=12,
            ha="left",
            va="bottom",
            bbox=dict(boxstyle="round,pad=0.3", facecolor="#e0e0e0", edgecolor="gray", alpha=0.8),
        )

    save_figure(plt.gcf(), OUTPUT_DIR / "venn-tool-usage.pdf", dpi=300)
    logs.info("rq2_venn_diagram", "Wrote venn-tool-usage.pdf.")


def main() -> None:
    """Build the Venn diagram from repos.csv."""
    df = load_repos()
    venn_diagram_for_agent_adoption(df)


if __name__ == "__main__":
    main()
