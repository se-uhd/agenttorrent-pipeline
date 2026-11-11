"""Monthly volume of AI commits since a start month, 2025 by default.

Reads the commit CSV and plots the monthly commit volume from a configurable start date. The
figure plots every month from the first month that the window admits to the month before the
month of the latest scan, the latest `scanned_at` of repos.csv, and a month without a commit as
0. A commit dated after the latest scan is left out with a count: Its date is the author date,
which the author sets, and no collection read a commit after it scanned the repository. The
month of the latest scan is left out too, because the collection read that month only up to its
scans. The title names the first and the last month. It writes the first month as the year
alone when it is a January, as in the default "Since 2025", and as month and year otherwise.
The figure describes the current dataset release.

Input:
  ../../data/collection/output/all_ai_commits.csv, or its 7z archive, which the loader extracts:
      the columns repo_name and commit_timestamp. A CSV that differs from the archive that git
      tracks beside it stops the script
  ../../data/collection/output/repos.csv

Output:
  monthly_ai_commit_volume.pdf
"""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, configure_plot_style, output_dir, read_commits_csv, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)

COMPONENT = "dataset_monthly_ai_commit_volume"

DEFAULT_PLOT_START = "2025-01-01"

# The columns of the commit CSV that the figure reads. The commit messages make up most of the
# CSV, and the figure counts commits per month and repository and needs no other column.
COMMIT_COLUMNS = ["repo_name", "commit_timestamp"]

# A `scanned_at` with a date and no time, which the collection of March 2026 wrote. The scan
# happened on that day and therefore bounds the commits at the day's last second rather than at
# midnight.
DATE_ONLY = r"\d{4}-\d{2}-\d{2}"


def latest_scan(repos: pd.DataFrame) -> Optional[pd.Timestamp]:
    """The latest `scanned_at` of repos.csv in UTC, a date without a time read as the last second
    of that day, or None when no row has a readable one."""
    values = repos["scanned_at"].dropna().astype(str).str.strip()
    times = pd.to_datetime(values, utc=True, format="ISO8601", errors="coerce")
    times = times.where(~values.str.fullmatch(DATE_ONLY),
                        times + pd.Timedelta(hours=23, minutes=59, seconds=59))
    return None if times.dropna().empty else times.max()


def monthly_ai_commit_volume(
    data_dir: Path | str = DATA_DIR,
    *,
    output_path: Path | str = OUTPUT_DIR / "monthly_ai_commit_volume.pdf",
    plot_start: str = DEFAULT_PLOT_START,
    show: bool = False,
) -> None:
    """Plot the AI commits per month of the window from the commit CSV and repos.csv."""
    configure_plot_style()
    import matplotlib.pyplot as plt

    commits = read_commits_csv(data_dir, columns=COMMIT_COLUMNS)
    repos_csv = Path(data_dir) / "repos.csv"
    scanned = latest_scan(pd.read_csv(repos_csv, usecols=["scanned_at"], dtype=str))
    commits["commit_timestamp_dt"] = pd.to_datetime(commits["commit_timestamp"], utc=True, errors="coerce")
    # Logged with a count, as the adoption timeline logs its undated rows, because a commit
    # without a readable timestamp cannot be placed in a month and is in no count of the figure.
    undated = int(commits["commit_timestamp_dt"].isna().sum())
    logs.info(COMPONENT, f"{undated:,} commits without a readable timestamp were left out.")
    commits = commits.dropna(subset=["commit_timestamp_dt"]).copy()
    commits["month"] = commits["commit_timestamp_dt"].dt.to_period("M").dt.to_timestamp()

    # The last month of the window is the month before the month of the latest scan. The
    # collection reads a repository's commits when it scans the repository. A commit dated later
    # therefore carries an author date that its author set ahead, and the collection read the
    # month of the latest scan only up to the moment at which it scanned each repository.
    # Plotted as a month, the part of a month that a collection read would look like a drop in
    # the count.
    # The latest commit is not the bound, because one commit dated months ahead would end the
    # window months after the collection, with the incomplete month of the collection and
    # zeros after it.
    last_month = None
    if len(commits):
        if scanned is None:
            raise SystemExit(f"{repos_csv} has no readable scanned_at. The month in which the "
                             f"collection ended is unknown.")
        later = commits["commit_timestamp_dt"] > scanned
        logs.info(COMPONENT, f"{int(later.sum()):,} commits dated after the latest scan "
                             f"({scanned:%Y-%m-%d %H:%M:%S} UTC) were left out.")
        commits = commits.loc[~later]
        scan_month = scanned.tz_localize(None).to_period("M")
        last_month = (scan_month - 1).to_timestamp()
        partial = commits.loc[commits["month"] > last_month]
        logs.info(COMPONENT, f"{len(partial):,} commits of {scanned:%B %Y}, the month of the "
                             f"latest scan, were left out.")
        commits = commits.loc[commits["month"] <= last_month]

    plot_start_ts = pd.Timestamp(plot_start)
    commits_plot = commits.loc[commits["month"] >= plot_start_ts].copy()
    # The first month that the window admits. Rows are truncated to their month before the
    # comparison, and a start inside a month therefore admits the month after it.
    first_month = plot_start_ts.to_period("M").to_timestamp()
    if first_month < plot_start_ts:
        first_month = (plot_start_ts.to_period("M") + 1).to_timestamp()

    n_commits = int(len(commits_plot))
    n_repos = int(commits_plot["repo_name"].nunique())
    # A month in the window without a commit is a count of zero, not a month to skip.
    monthly = commits_plot.groupby("month").size()
    if len(monthly):
        months = pd.date_range(first_month, last_month, freq="MS")
        monthly = monthly.reindex(months, fill_value=0)
    monthly = monthly.rename_axis("month").reset_index(name="count")
    since = f"{first_month:%Y}" if first_month.month == 1 else f"{first_month:%B %Y}"
    through = f" Through {last_month:%B %Y}" if last_month is not None else ""

    fig, ax = plt.subplots(figsize=(10, 5))
    ax.plot(monthly["month"], monthly["count"], marker="o", linewidth=1.5, markersize=5, color="#457B9D")

    import matplotlib.dates as mdates
    ax.set_xlabel("Month")
    ax.set_ylabel("AI Commits")
    ax.set_title(
        f"Monthly Volume of AI Commits Since {since}{through} "
        f"(n = {n_commits:,} commits from {n_repos:,} repositories)"
    )
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %Y"))
    ax.xaxis.set_major_locator(mdates.MonthLocator())
    fig.autofmt_xdate(rotation=45)
    plt.tight_layout()

    save_figure(fig, output_path)
    if show:
        plt.show()
    plt.close(fig)
    logs.info(COMPONENT, f"Wrote {output_path}.")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """The four flags of the command line."""
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(
        description="Plot the number of AI commits per month.", allow_abbrev=False
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR),
                        help="Directory with all_ai_commits.csv or its 7z archive, and repos.csv "
                             "(default: %(default)s)")
    parser.add_argument("--output-path", default=str(OUTPUT_DIR / "monthly_ai_commit_volume.pdf"),
                        help="Path of the figure (default: %(default)s)")
    parser.add_argument("--plot-start", default=DEFAULT_PLOT_START,
                        help="Lower date bound of the x-axis (default: %(default)s)")
    parser.add_argument("--show", action="store_true", help="Display the plot interactively")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse args and render the monthly AI-commit volume plot."""
    args = _parse_args(argv)
    monthly_ai_commit_volume(
        data_dir=args.data_dir,
        output_path=args.output_path,
        plot_start=args.plot_start,
        show=args.show,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
