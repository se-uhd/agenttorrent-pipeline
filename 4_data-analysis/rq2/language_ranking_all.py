"""Rank programming languages across the repositories in the sample.

The script calls build_repo_metadata() (from _repo_metadata) to assemble per-repository
metadata for every row of the classification table, engineered or not. It keeps the
repositories in the sample, which sample_names() names as the repositories labeled
engineered_project=true, and writes the ranking below. Languages with the same count are ranked
by name, so that the ranking does not depend on the order of the rows:

  - language_ranking_all.csv: rank, language, count for all languages
  - top5_languages_all.tex: enumerate environment for the top five

Outputs (in ../../data/analysis/rq2/):
  language_ranking_all.csv
  top5_languages_all.tex
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _repo_metadata import build_repo_metadata, sample_names
from _shared import latex_text, output_dir
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
# The languages that the list names, and the decimals of their shares.
TOP_LANGUAGES = 5
SHARE_DECIMALS = 1


def main() -> None:
    """Rank the languages and write the CSV and the LaTeX list of the top five."""
    metadata = build_repo_metadata()
    # The population is the sample, from which the collection draws the configured
    # repositories that `language_tool_association.py` ranks. The classification table also
    # contains the repositories labeled `false` or `unsure`, which no analysis counts.
    df = metadata[metadata["repo_name"].isin(sample_names())]
    logs.info("rq2_language_ranking_all", f"{len(df):,} of the {len(metadata):,} annotated "
                                          f"repositories in the sampling frame are in the sample.")

    lang_counts = (df["main_language"].value_counts().rename_axis("language").reset_index(name="count")
                   .sort_values(["count", "language"], ascending=[False, True], ignore_index=True))
    lang_counts.insert(0, "rank", range(1, len(lang_counts) + 1))

    lang_counts.to_csv(OUTPUT_DIR / "language_ranking_all.csv", index=False)
    logs.info("rq2_language_ranking_all", f"Wrote language_ranking_all.csv ({len(lang_counts):,} languages).")

    # --- LaTeX snippet: Top 5 Languages ---
    total = len(df)
    top5 = lang_counts.head(TOP_LANGUAGES)
    latex = "\\begin{enumerate}\n"
    for _, row in top5.iterrows():
        pct = row["count"] / total * 100
        latex += (f'  \\item {latex_text(row["language"])} (n={row["count"]}, '
                  f'{pct:.{SHARE_DECIMALS}f}\\%)\n')
    latex += "\\end{enumerate}\n"
    (OUTPUT_DIR / "top5_languages_all.tex").write_text(latex)
    logs.info("rq2_language_ranking_all", "Wrote top5_languages_all.tex.")


if __name__ == "__main__":
    main()
