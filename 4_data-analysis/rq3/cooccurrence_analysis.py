"""Co-occurrence, creation order, and reference network for Context Files.

Analyzes how the Context File categories of _shared.CONTEXT_FILE_CATEGORIES
(CLAUDE.md, AGENTS.md, GEMINI.md, Copilot Instructions, .cursorrules, Gemini
Style Guide, Cursor BUGBOT, REVIEW.md, Named in Configuration) co-occur within
repositories, the order in which they are created, and how they reference
each other. Produces three PDF charts. The categories and DATA_DIR come from _shared.py. A row
without a created_at is left out of the creation order sequences, which the script logs with a
count, and stays in the other two analyses, which do not read the date. The sequences order the
categories by the created_at of their earliest file, the date of the commit that created the
file under whatever name it had then (docs/git.md), and the title of the figure says so.

Input:
  ../../data/collection/output/context-files.csv

Outputs (in ../../data/analysis/rq3/):
  creation_order_sequences.pdf
  reference_network.pdf
  cooccurrence_matrix.pdf
"""

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import (COLORS, CONTEXT_FILE_CATEGORIES, DATA_DIR, context_file_category,
                     context_file_row_category, output_dir, save_figure)
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The categories that a repository needs for an order of creation, and the most frequent orders
# that the figure draws before the bar of the others.
MIN_SEQUENCE_CATEGORIES = 2
TOP_SEQUENCES = 5

CATEGORY_ORDER = list(CONTEXT_FILE_CATEGORIES.keys())


def to_bool(value: object) -> bool:
    """Read a CSV boolean cell, TRUE or FALSE, whether parsed or still a string."""
    return str(value).upper() == "TRUE"


def classify_row(context_file: str) -> str:
    """Classify a context_file value into one of the categories of `CATEGORY_ORDER`."""
    return context_file_row_category(context_file)


def classify_referenced_file(ref: str) -> str:
    """Classify a single referenced_file value into a category or 'Other'.

    By path alone, because a referenced file is not a row, and nothing records whether a
    configuration names it.
    """
    return context_file_category(ref) or "Other"


def load_data() -> pd.DataFrame:
    """Load `context-files.csv` and give every row its category."""
    df = pd.read_csv(
        DATA_DIR / "context-files.csv",
        usecols=["repo_name", "context_file", "created_at",
                 "is_reference", "referenced_file"],
    )
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)
    # Every row has a category: A row that no path pattern matches is a file that a
    # configuration named. `_shared` derives the map from the heuristics, and its import fails
    # when a context file heuristic has no category.
    df["category"] = df["context_file"].apply(classify_row)
    return df


def analysis_creation_order(df: pd.DataFrame) -> None:
    """Analysis 1: Creation order sequences across repositories.

    A row without a created_at cannot be placed in a sequence. It is left out before the
    earliest date per category is taken, and the number left out is logged. A repository
    counts among the repositories with 2+ categories only when two of its categories are
    dated, because an undated category would count toward the two and then be missing from
    the sequence, as grouping by date drops a `NaT` key.
    """
    logs.info("rq3_cooccurrence_analysis", "Analyzing the creation order sequences of the context "
                                           "file categories.")

    undated = df["created_at"].isna()
    logs.info("rq3_cooccurrence_analysis", f"{int(undated.sum()):,} rows without created_at were left out of the sequences.")

    # Earliest created_at per (repo, category), truncated to its date
    first = (
        df.loc[~undated].groupby(["repo_name", "category"])["created_at"]
        .min()
        .reset_index()
    )
    first["date"] = first["created_at"].dt.normalize()

    # Build ordered sequences per repo
    sequences = []
    for repo, group in first.groupby("repo_name"):
        cats = group[["date", "category"]].sort_values("date")
        if cats["category"].nunique() < MIN_SEQUENCE_CATEGORIES:
            continue
        # Group categories by date into sorted tuples
        date_groups = (
            cats.groupby("date")["category"]
            .apply(lambda x: tuple(sorted(x)))
            .tolist()
        )
        sequences.append(tuple(date_groups))

    logs.info("rq3_cooccurrence_analysis", f"{len(sequences):,} repositories have dated context "
                                           f"files of at least two categories.")

    # Count sequence frequencies
    seq_counts = pd.Series(sequences).value_counts()
    top = seq_counts.head(TOP_SEQUENCES)
    other_count = (seq_counts.iloc[TOP_SEQUENCES:].sum() if len(seq_counts) > TOP_SEQUENCES
                   else 0)

    # Format labels
    def fmt_seq(seq):
        parts = []
        for group in seq:
            if len(group) == 1:
                parts.append(group[0])
            else:
                parts.append("{" + ",\n  ".join(group) + "}")
        return "\nthen ".join(parts)

    # The same sequence on one line, for the log.
    def log_seq(seq):
        return " then ".join(group[0] if len(group) == 1 else "{" + ", ".join(group) + "}"
                             for group in seq)

    labels = [fmt_seq(s) for s in top.index]
    logged = [log_seq(s) for s in top.index]
    values = list(top.values)
    if other_count > 0:
        labels.append("Other")
        logged.append("Other")
        values.append(other_count)

    logs.info("rq3_cooccurrence_analysis", f"Repositories per creation order sequence, for the "
                                           f"{len(top):,} most frequent sequences and Other.")
    for label, count in zip(logged, values):
        logs.info("rq3_cooccurrence_analysis", f"  {count:4,d}  {label}")

    fig, ax = plt.subplots(figsize=(12, 6))
    y_pos = range(len(labels))
    bars = ax.barh(y_pos, values, color=COLORS["CLAUDE.md"], edgecolor="white")
    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, fontsize=12)
    ax.invert_yaxis()
    ax.set_xlabel("Number of Repositories", fontsize=13)
    ax.set_title(f"Top {TOP_SEQUENCES} Creation Order Sequences "
                 f"(n = {len(sequences):,} repositories)\n"
                 f"by the creation date of the earliest file of each category, under any earlier "
                 f"name", fontsize=14)
    ax.bar_label(bars, padding=3, fontsize=11)
    ax.tick_params(axis="x", labelsize=11)
    ax.grid(True, axis="x", alpha=0.3)
    plt.tight_layout()

    path = OUTPUT_DIR / "creation_order_sequences.pdf"
    save_figure(fig, path)
    logs.info("rq3_cooccurrence_analysis", f"Wrote {path}.")
    plt.close(fig)


def analysis_reference_network(df: pd.DataFrame) -> None:
    """Analysis 2: Reference network heatmap."""
    logs.info("rq3_cooccurrence_analysis", "Analyzing the reference network of the context files.")

    refs = df[df["is_reference"].apply(to_bool)].copy()
    logs.info("rq3_cooccurrence_analysis", f"{len(refs):,} rows of context-files.csv are "
                                           f"references.")

    # Build edges from source category to target category
    edges = []
    for _, row in refs.iterrows():
        source = row["category"]
        ref_str = str(row["referenced_file"])
        for ref in ref_str.split(";"):
            ref = ref.strip()
            if ref:
                target = classify_referenced_file(ref)
                edges.append({"source": source, "target": target})

    # Named columns, so that a DataFrame without a reference row still has a source and a
    # target to cross-tabulate.
    edge_df = pd.DataFrame(edges, columns=["source", "target"])
    logs.info("rq3_cooccurrence_analysis", f"The reference network has {len(edge_df):,} edges, one "
                                           f"per file that a reference row names.")

    # Build cross-tabulation
    target_order = CATEGORY_ORDER + ["Other"]
    cross = pd.crosstab(
        edge_df["source"],
        edge_df["target"],
    )
    # Reindex to ensure consistent order
    cross = cross.reindex(index=CATEGORY_ORDER, columns=target_order, fill_value=0)

    # Shorten label for this figure only
    short = {"Copilot Instructions": "Instructions"}
    cross = cross.rename(index=short, columns=short)

    logs.info("rq3_cooccurrence_analysis", "References by source and target category.")
    logs.info("rq3_cooccurrence_analysis", cross.to_string())

    fig, ax = plt.subplots(figsize=(9, 6))
    ax.set_facecolor("#f7f7f7")
    sns.heatmap(
        cross,
        annot=True,
        fmt="d",
        cmap="YlOrRd",
        linewidths=0.5,
        mask=cross == 0,
        ax=ax,
    )
    # Add "0" annotations on masked cells
    for i in range(cross.shape[0]):
        for j in range(cross.shape[1]):
            if cross.iloc[i, j] == 0:
                ax.text(j + 0.5, i + 0.5, "0", ha="center", va="center",
                        color="black", fontsize=10)
    ax.set_xlabel("Referenced File Category $\\mathbf{(Target)}$")
    ax.set_ylabel("Referencing File Category $\\mathbf{(Source)}$")
    # The category names do not fit side by side under the columns.
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="y", rotation=0)
    n_pairs = len(edge_df)
    ax.set_title(f"Reference Network Between Context Files (n = {n_pairs:,} reference pairs)")
    plt.tight_layout()

    path = OUTPUT_DIR / "reference_network.pdf"
    save_figure(fig, path)
    logs.info("rq3_cooccurrence_analysis", f"Wrote {path}.")
    plt.close(fig)


def analysis_cooccurrence_matrix(df: pd.DataFrame) -> None:
    """Analysis 3: Co-Occurrence matrix."""
    logs.info("rq3_cooccurrence_analysis", "Analyzing the co-occurrence of the context file "
                                           "categories.")

    # Binary presence per repository and category
    presence = (
        df.groupby(["repo_name", "category"])
        .size()
        .unstack(fill_value=0)
        .clip(upper=1)
    )
    presence = presence.reindex(columns=CATEGORY_ORDER, fill_value=0)

    logs.info("rq3_cooccurrence_analysis", f"{len(presence):,} repositories have a context file.")
    logs.info("rq3_cooccurrence_analysis", f"{(presence.sum(axis=1) >= 2).sum():,} repositories "
                                           f"have context files of at least two categories.")
    logs.info("rq3_cooccurrence_analysis", "Repositories per category.")
    for cat in CATEGORY_ORDER:
        logs.info("rq3_cooccurrence_analysis", f"  {cat:<22} {presence[cat].sum():>7,}")

    # Symmetric co-occurrence
    cooccurrence = presence.T.dot(presence)

    logs.info("rq3_cooccurrence_analysis", "Repositories per pair of categories.")
    logs.info("rq3_cooccurrence_analysis", cooccurrence.to_string())

    # Shorten label for this figure only
    short_labels = {c: c for c in cooccurrence.columns}
    short_labels["Copilot Instructions"] = "Instructions"
    cooccurrence = cooccurrence.rename(index=short_labels, columns=short_labels)

    # Mask upper triangle + diagonal for the color-coded heatmap
    mask = np.triu(np.ones_like(cooccurrence, dtype=bool), k=0)

    fig, ax = plt.subplots(figsize=(8, 6))
    sns.heatmap(
        cooccurrence,
        annot=True,
        fmt="d",
        cmap="Blues",
        linewidths=0.5,
        mask=mask,
        ax=ax,
    )
    # Add diagonal values with neutral styling
    n = len(cooccurrence)
    for i in range(n):
        val = cooccurrence.iloc[i, i]
        ax.add_patch(plt.Rectangle((i, i), 1, 1, fill=True,
                                   facecolor="#f7f7f7", edgecolor="white",
                                   linewidth=0.5))
        ax.text(i + 0.5, i + 0.5, f"{val}", ha="center", va="center",
                color="black", fontsize=10)
    ax.set_xlabel("")
    ax.set_ylabel("")
    # The category names do not fit side by side under the columns.
    plt.setp(ax.get_xticklabels(), rotation=45, ha="right", rotation_mode="anchor")
    ax.tick_params(axis="y", rotation=0)
    # The categories of one mechanism, the context files, not the configuration mechanisms.
    ax.set_title(f"Co-Occurrence of Context File Categories Across Repositories "
                 f"(n = {len(presence):,})")
    plt.tight_layout()

    path = OUTPUT_DIR / "cooccurrence_matrix.pdf"
    save_figure(fig, path)
    logs.info("rq3_cooccurrence_analysis", f"Wrote {path}.")
    plt.close(fig)


def print_context_file_summary(df: pd.DataFrame) -> None:
    """Log context file and repository counts by category."""
    logs.info("rq3_cooccurrence_analysis", "Context files per category.")
    total = len(df)
    for cat in CATEGORY_ORDER:
        cat_df = df[df["category"] == cat]
        n_files = len(cat_df)
        n_repos = cat_df["repo_name"].nunique()
        pct = n_files / total * 100
        logs.info("rq3_cooccurrence_analysis", f"  {cat:<25s} {n_files:5,d} files ({pct:4.1f}%), "
                                               f"{n_repos:5,d} repositories")
    logs.info("rq3_cooccurrence_analysis", f"  {'TOTAL':<25s} {total:5,d} files, "
                                           f"{df['repo_name'].nunique():5,d} repositories")


def main() -> None:
    """Run all three Context File co-occurrence analyses."""
    df = load_data()
    logs.info("rq3_cooccurrence_analysis", f"Loaded {len(df):,} rows of {df['repo_name'].nunique():,} repositories from context-files.csv.")

    print_context_file_summary(df)
    analysis_creation_order(df)
    analysis_reference_network(df)
    analysis_cooccurrence_matrix(df)


if __name__ == "__main__":
    main()
