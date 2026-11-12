"""Cumulative number of repositories by the creation month of their earliest file in each family.

Plots, for each family of configuration artifacts, the cumulative number of repositories per month,
counting each repository from the month of its earliest created_at in the family. created_at is
the date of the commit that created the file under whatever name it had then (docs/git.md). A
file renamed into a family keeps the date of the commit that created it under its earlier name,
and a file that a configuration names keeps the date on which it was created, not the date on
which the configuration began to name it. The figure therefore does not show when a repository
adopted a family, and its title and the label of its x axis say what it shows. The families are
those of the timeline of AgentConfigs 1.0.0, the context file categories of
_shared.ADOPTION_CONTEXT_FILE_CATEGORIES, Skills and Subagents, and the Planning Documents, which
come from separate CSVs. A family with no row is left out. The families, ADOPTION_START and
DATA_DIR come from _shared.py.

A row without a created_at cannot be placed in time, and a file created before ADOPTION_START was
created for another purpose and only later renamed into a configuration or named by one. Both are
left out before the earliest date is taken. The script logs how many rows of each CSV it left
out, and the logged repository counts and the title's n therefore count what is plotted.

Inputs:
  ../../data/collection/output/context-files.csv
  ../../data/collection/output/skills.csv
  ../../data/collection/output/subagents.csv
  ../../data/collection/output/planning-documents.csv

Output:
  cumulative_repo_adoption.pdf
"""

import sys
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import (ADOPTION_CONTEXT_FILE_CATEGORIES, ADOPTION_START, COLORS, DATA_DIR,
                     context_file_row_category, output_dir, save_figure)
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

# One marker per plotted series. `_assert_every_series_has_a_style` checks that every family is
# here.
MARKERS = {
    "CLAUDE.md": "o",
    "AGENTS.md": "s",
    "GEMINI.md": "^",
    "Copilot Instructions": "D",
    ".cursorrules": "v",
    "Skills": "P",
    "Subagents": "X",
    "Planning Documents": "*",
}

# The maximum number of labels on the x axis. Over dates that span years, a label for every month
# would overlap the next, so `AutoDateLocator` chooses months, quarters or years and draws no more
# labels than this.
MAX_DATE_TICKS = 12

# The CSVs of the families plotted beside the context file categories.
MECHANISM_CSVS = {
    "Skills": "skills.csv",
    "Subagents": "subagents.csv",
    "Planning Documents": "planning-documents.csv",
}


def dated_rows(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """Parse created_at in UTC and drop the rows without one and the rows created before
    ADOPTION_START, logging how many there were.

    A repository with only undated rows in a family is left out rather than kept with a `NaT`
    as its earliest date, which would count it in the logged count and the title's n and not
    in the plotted line.
    """
    df = df.copy()
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    undated = int(df["created_at"].isna().sum())
    df = df.dropna(subset=["created_at"])
    early = df["created_at"] < pd.Timestamp(ADOPTION_START, tz="UTC")
    logs.info("rq3_adoption_timeline", f"{undated:,} rows of {source} without created_at and "
                                       f"{int(early.sum()):,} created before {ADOPTION_START} "
                                       f"were left out.")
    return df[~early]


def load_first_adoption(csv_path: Path) -> pd.Series:
    """Load a CSV and return the earliest created_at per repository, truncated to its month."""
    df = dated_rows(pd.read_csv(csv_path, usecols=["repo_name", "created_at"]), csv_path.name)
    first = df.groupby("repo_name")["created_at"].min()
    first = first.dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()
    return first


def load_context_file_categories() -> dict[str, pd.Series]:
    """Load context files CSV and split into category-level first-adoption series."""
    df = pd.read_csv(
        DATA_DIR / "context-files.csv",
        usecols=["repo_name", "context_file", "created_at"],
    )
    df = dated_rows(df, "context-files.csv")

    results = {}
    categories = df["context_file"].apply(context_file_row_category)
    for label in ADOPTION_CONTEXT_FILE_CATEGORIES:
        subset = df[categories == label]
        if subset.empty:
            continue
        first = subset.groupby("repo_name")["created_at"].min()
        first = first.dt.tz_localize(None).dt.to_period("M").dt.to_timestamp()
        results[label] = first
    return results


def compute_cumulative(first_adoption: pd.Series) -> pd.Series:
    """The cumulative count of first-adoption dates, which are truncated to their month."""
    counts = first_adoption.value_counts().sort_index()
    return counts.cumsum()


def _assert_every_series_has_a_style() -> None:
    """Refuse to plot before a series with no marker or no color is reached.

    The style maps are keyed by label and read inside the plotting loop. A family added to
    `ADOPTION_CONTEXT_FILE_CATEGORIES` or `MECHANISM_CSVS` and to neither map would raise a
    KeyError with some of the series already drawn.
    """
    labels = list(ADOPTION_CONTEXT_FILE_CATEGORIES) + list(MECHANISM_CSVS)
    for name, style in (("MARKERS", MARKERS), ("COLORS", COLORS)):
        missing = [label for label in labels if label not in style]
        if missing:
            raise KeyError(f"{name} has no entry for {missing}")


def main() -> None:
    """Build the plot of the creation month of the earliest file per family, and save the PDF."""
    _assert_every_series_has_a_style()

    # One first-adoption series per plotted line, read once, which the
    # repository count below reuses.
    first_adoption = load_context_file_categories()
    for label, filename in MECHANISM_CSVS.items():
        first_adoption[label] = load_first_adoption(DATA_DIR / filename)

    logs.info("rq3_adoption_timeline", "Unique repositories per series of the adoption timeline.")
    cumulative_data = {}
    for label, first in first_adoption.items():
        cumulative_data[label] = compute_cumulative(first)
        logs.info("rq3_adoption_timeline", f"  {label:<22} {len(first):>7,}")

    # Align all series to a common date range
    all_dates = pd.DatetimeIndex(
        sorted(set().union(*(s.index for s in cumulative_data.values())))
    )

    fig, ax = plt.subplots(figsize=(10, 5))

    for label, series in cumulative_data.items():
        # Reindex to all dates, forward-fill, and drop leading zeros, so that the lines
        # start at the first actual adoption month
        aligned = series.reindex(all_dates).ffill().fillna(0).astype(int)
        first_nonzero = aligned[aligned > 0]
        if first_nonzero.empty:
            continue
        aligned = aligned.loc[first_nonzero.index[0]:]
        ax.plot(
            aligned.index,
            aligned.values,
            label=label,
            color=COLORS[label],
            marker=MARKERS[label],
            markersize=5,
            linewidth=1.5,
        )

    # Count unique repositories across all categories
    all_repos = set()
    for first in first_adoption.values():
        all_repos.update(first.index)
    n_repos = len(all_repos)

    ax.set_xlabel("Month of the Commit That Created the Earliest File, Under Any Earlier Name")
    ax.set_ylabel("Cumulative Unique Repositories")
    ax.set_title(f"Cumulative Repositories by the Creation Month of Their Earliest File in Each "
                 f"Family (n = {n_repos:,})")
    ax.legend()
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=MAX_DATE_TICKS))
    fig.autofmt_xdate(rotation=45)
    plt.tight_layout()

    output_path = OUTPUT_DIR / "cumulative_repo_adoption.pdf"
    save_figure(fig, output_path)
    logs.info("rq3_adoption_timeline", f"Wrote {output_path}.")
    plt.close(fig)


if __name__ == "__main__":
    main()
