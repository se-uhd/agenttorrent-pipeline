"""First-word frequencies in the messages of AI commits.

Reads the messages of the commit CSV one chunk at a time and renders a bar chart of the top-N
most common first words (lowercase, normalized), each bar the word's number of occurrences. Words with the same count
are ranked alphabetically, so that the order of the rows decides neither which of two equally
frequent words makes the top N nor which of them is drawn above the other.
The figure describes the current dataset release.

Input:
  ../../data/collection/output/all_ai_commits.csv, or its 7z archive, which the loader extracts.
      A CSV that differs from the archive that git tracks beside it stops the script

Output:
  commit_message_first_words.pdf
"""

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, configure_plot_style, output_dir, read_commits_chunks, save_figure
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The first words that the figure draws, and the fewest that a smaller --top-n draws.
TOP_N = 12
MIN_TOP_N = 1


def commit_message_first_words(
    data_dir=DATA_DIR,
    *,
    output_path=OUTPUT_DIR / "commit_message_first_words.pdf",
    top_n: int = TOP_N,
    show: bool = False,
) -> None:
    configure_plot_style()
    import matplotlib.pyplot as plt

    import pandas as pd

    # The messages are read and counted one chunk at a time, because the whole commit CSV does
    # not fit into the memory of a GitHub runner once pandas holds it.
    n_commits = 0
    counts = pd.Series(dtype="int64")
    for chunk in read_commits_chunks(data_dir, columns=["commit_message"]):
        messages = chunk["commit_message"].dropna()
        n_commits += int(len(messages))
        first_words = (
            messages
            .astype(str)
            .str.lower()
            .str.replace(r"[^a-z0-9\s]", "", regex=True)
            # The first run of characters that are not whitespace, the token that `split()`
            # would give first, without a list of every token of the message.
            .str.extract(r"(\S+)", expand=False)
            .fillna("")
        )
        counts = counts.add(first_words[first_words != ""].value_counts(), fill_value=0)
    counts = counts.astype("int64")
    ranked = (counts.rename_axis("word").reset_index(name="count")
              .sort_values(["count", "word"], ascending=[False, True]))
    # Reverse the ranking, because barh draws the first row at the bottom and the most frequent
    # word belongs on top.
    top_words = ranked.head(max(MIN_TOP_N, int(top_n))).iloc[::-1]

    import seaborn as sns
    bar_color = sns.color_palette("muted", 1)[0]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.barh(top_words["word"], top_words["count"], color=bar_color, edgecolor="black", linewidth=0.5)
    ax.set_title(
        f"Most Frequent First Words in AI Commit Messages (n = {n_commits:,} commits)",
        fontsize=14,
    )
    ax.set_xlabel("Occurrences")
    ax.set_ylabel("First Word")

    fig.tight_layout()
    save_figure(fig, output_path)
    if show:
        plt.show()
    plt.close(fig)
    logs.info("dataset_commit_message_first_words", f"Wrote {output_path}.")


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    # Long options by their full names only, as in `agentcontext.cli.ArgumentParser`.
    parser = argparse.ArgumentParser(
        description="Plot the most frequent first words of the messages of the AI commits.",
        allow_abbrev=False,
    )
    parser.add_argument("--data-dir", default=str(DATA_DIR),
                        help="Directory with all_ai_commits.csv or its 7z archive "
                             "(default: %(default)s)")
    parser.add_argument("--output-path", default=str(OUTPUT_DIR / "commit_message_first_words.pdf"),
                        help="Path of the figure (default: %(default)s)")
    parser.add_argument("--top-n", type=int, default=TOP_N,
                        help="Number of first words to plot (default: %(default)s)")
    parser.add_argument("--show", action="store_true", help="Display the plot interactively")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Parse args and render the first-word frequency plot."""
    args = _parse_args(argv)
    commit_message_first_words(
        data_dir=args.data_dir,
        output_path=args.output_path,
        top_n=args.top_n,
        show=args.show,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
