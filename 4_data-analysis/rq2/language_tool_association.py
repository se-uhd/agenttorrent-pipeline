"""Programming-language association analysis for the tools.

Joins repos.csv with the SEART GHS snapshot to attach a main language to each configured
repository, then produces:

  - language_ranking.csv: rank, language, count for the configured repositories
  - top5_languages.tex: enumerate environment for the top five
  - language_tool_heatmap.pdf: top-10-languages x tools heatmap
  - context_file_language_adoption.csv: per-language Context File
    adoption count and rate (also logged as a table)

Languages with the same count are ranked by name, in the ranking and in the
adoption table, so that neither order depends on the order of the rows.

Inputs:
  ../../data/collection/output/repos.csv
  ../../data/sampling/ghs_filtered.json, which stops the script where it differs from the
      ghs_filtered.json.7z that git tracks beside it

Outputs (in ../../data/analysis/rq2/):
  language_ranking.csv
  top5_languages.tex
  language_tool_heatmap.pdf
  context_file_language_adoption.csv
"""

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
import seaborn as sns

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import (DATA_DIR, SAMPLING_DIR, TOOL_LABELS, TOOLS, checked_copy, latex_text,
                     output_dir, save_figure)
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The languages that the list names, and the languages that the heatmap draws.
TOP_LANGUAGES = 5
HEATMAP_LANGUAGES = 10


def load_data() -> pd.DataFrame:
    """Load repos.csv joined with the main language from the SEART GHS snapshot."""
    repos = pd.read_csv(DATA_DIR / "repos.csv")
    with open(checked_copy(SAMPLING_DIR / "ghs_filtered.json"), encoding="utf-8") as f:
        ghs = json.load(f)
    ghs_df = pd.DataFrame(ghs["items"])[["name", "mainLanguage"]]
    # One row per repository on each side, because a name listed twice would give the
    # repository two rows, counted twice in the ranking and in every cell of the heatmap.
    try:
        df = repos.merge(ghs_df, left_on="repo_name", right_on="name", how="inner",
                         validate="one_to_one")
    except pd.errors.MergeError as e:
        raise SystemExit(f"repos.csv and ghs_filtered.json do not name each repository "
                         f"once ({e})")
    for tool in TOOLS:
        df[tool] = df[tool].apply(lambda v: str(v).upper() == "TRUE")
    return df


def write_language_ranking(df: pd.DataFrame) -> pd.DataFrame:
    """Write language_ranking.csv and return the full ranking DataFrame."""
    lang_counts = (df["mainLanguage"].value_counts().rename_axis("language").reset_index(name="count")
                   .sort_values(["count", "language"], ascending=[False, True], ignore_index=True))
    lang_counts.insert(0, "rank", range(1, len(lang_counts) + 1))
    lang_counts.to_csv(OUTPUT_DIR / "language_ranking.csv", index=False)
    logs.info("rq2_language_tool_association", f"Wrote language_ranking.csv ({len(lang_counts):,} languages).")
    return lang_counts


def write_top5_latex(df: pd.DataFrame, lang_counts: pd.DataFrame) -> None:
    """Write top5_languages.tex with a numbered list of the top five languages."""
    total = len(df)
    top5_df = lang_counts.head(TOP_LANGUAGES)
    items = []
    for _, row in top5_df.iterrows():
        pct = row["count"] / total * 100
        items.append(f'{latex_text(row["language"])} (n={row["count"]}, {pct:.1f}\\%)')
    latex = "\\begin{enumerate}\n"
    for item in items:
        latex += f"  \\item {item}\n"
    latex += "\\end{enumerate}\n"
    (OUTPUT_DIR / "top5_languages.tex").write_text(latex)
    logs.info("rq2_language_tool_association", "Wrote top5_languages.tex.")


def write_heatmap(df: pd.DataFrame, lang_counts: pd.DataFrame) -> None:
    """Write language_tool_heatmap.pdf for top-10 languages vs. tools."""
    top10 = lang_counts.head(HEATMAP_LANGUAGES)["language"].tolist()
    df_top10 = df[df["mainLanguage"].isin(top10)]

    matrix = df_top10.groupby("mainLanguage")[TOOLS].sum().reindex(top10).astype(int)
    matrix.columns = [TOOL_LABELS[c] for c in matrix.columns]
    matrix.index.name = "Language"

    tool_totals = df[TOOLS].sum()
    tool_totals.index = [TOOL_LABELS[c] for c in tool_totals.index]

    n_repos = df["repo_name"].nunique()

    annot_labels = matrix.copy().astype(str)
    for col in matrix.columns:
        for lang in matrix.index:
            count = matrix.loc[lang, col]
            pct = count / tool_totals[col] * 100
            annot_labels.loc[lang, col] = f"{count}\n({pct:.1f}%)"

    pct_matrix = matrix.div(tool_totals) * 100

    fig, ax = plt.subplots(figsize=(9, 8))
    sns.heatmap(
        pct_matrix,
        annot=annot_labels,
        fmt="",
        cmap="YlOrRd",
        linewidths=0.5,
        ax=ax,
        annot_kws={"size": 11},
    )
    ax.set_title(
        f"Agentic AI Tool Usage by Programming Language (n = {n_repos:,})",
        fontsize=14,
    )
    ax.set_ylabel("")
    ax.set_yticklabels(ax.get_yticklabels(), rotation=0, fontsize=12)
    tool_labels = [f"{col}\n(n={int(tool_totals[col]):,})" for col in matrix.columns]
    ax.set_xticklabels(tool_labels, fontsize=12, rotation=45, ha="right",
                       rotation_mode="anchor")
    plt.tight_layout()
    save_figure(fig, OUTPUT_DIR / "language_tool_heatmap.pdf")
    plt.close(fig)
    logs.info("rq2_language_tool_association", "Wrote language_tool_heatmap.pdf.")


def write_context_file_adoption_per_language(df: pd.DataFrame) -> None:
    """Log and write per-language Context File adoption rates."""
    df = df.copy()
    df["has_context_file"] = df["context_file"].apply(lambda v: str(v).upper() == "TRUE")
    cf_by_lang = (
        df.groupby("mainLanguage")
        .agg(
            total_repos=("repo_name", "nunique"),
            repos_with_context_file=("has_context_file", "sum"),
        )
        .sort_values(["total_repos", "mainLanguage"], ascending=[False, True])
    )
    cf_by_lang["adoption_rate"] = (
        cf_by_lang["repos_with_context_file"] / cf_by_lang["total_repos"]
    )

    overall_total = int(cf_by_lang["total_repos"].sum())
    overall_with_cf = int(cf_by_lang["repos_with_context_file"].sum())
    overall_rate = overall_with_cf / overall_total if overall_total else 0.0
    logs.info("rq2_language_tool_association", f"Over all languages, {overall_with_cf:,} of "
                                               f"{overall_total:,} repositories have a context "
                                               f"file ({100 * overall_rate:.1f}%).")
    logs.info("rq2_language_tool_association", "Context file adoption rate per language.")
    logs.info("rq2_language_tool_association", f"  {'Language':<12} {'Total':>6} "
                                               f"{'With a context file':>19} {'Rate':>7}")
    for lang, row in cf_by_lang.iterrows():
        rate_pct = 100 * row["adoption_rate"]
        logs.info("rq2_language_tool_association", f"  {lang:<12} "
                                                   f"{int(row['total_repos']):>6,} "
                                                   f"{int(row['repos_with_context_file']):>19,} "
                                                   f"{rate_pct:>6.1f}%")

    cf_by_lang.reset_index().rename(columns={"mainLanguage": "language"}).to_csv(
        OUTPUT_DIR / "context_file_language_adoption.csv", index=False
    )
    logs.info("rq2_language_tool_association", "Wrote context_file_language_adoption.csv.")


def main() -> None:
    """Run the full language-tool association analysis."""
    df = load_data()
    lang_counts = write_language_ranking(df)
    write_top5_latex(df, lang_counts)
    write_heatmap(df, lang_counts)
    write_context_file_adoption_per_language(df)


if __name__ == "__main__":
    main()
