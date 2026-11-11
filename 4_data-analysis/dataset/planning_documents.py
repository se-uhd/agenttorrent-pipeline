"""Planning documents: the documents and the repositories of each framework or tool.

Counts the rows of the planning documents per framework, the framework or the tool that writes
them, and the configured repositories that have them, and writes the counts as a CSV and as a
LaTeX table. Planning documents are no configuration artifacts, and the release publishes them
separately in `planning-documents.csv`. The table describes the current dataset release.

Input:
  ../../data/collection/output/planning-documents.csv

Output:
  planning_documents.csv
  planning_documents.tex
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, latex_text, output_dir
from agentcontext import logs

OUTPUT_DIR = output_dir(__file__)
COMPONENT = "dataset_planning_documents"


def framework_counts(documents: pd.DataFrame) -> pd.DataFrame:
    """Documents and repositories per framework, the most documents first."""
    counts = (documents.groupby("framework")
              .agg(documents=("planning_document", "size"),
                   repositories=("repo_name", "nunique"))
              .reset_index())
    return counts.sort_values(["documents", "framework"], ascending=[False, True],
                              ignore_index=True)


def latex_table(counts: pd.DataFrame, documents: int, repositories: int) -> str:
    """The counts as a LaTeX table with a total row."""
    lines = [
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Planning documents per framework or tool.}",
        "\\label{tab:planning-documents}",
        "\\begin{tabular}{lrr}",
        "\\toprule",
        "\\textbf{Framework or tool} & \\textbf{Documents} & \\textbf{Repos} \\\\",
        "\\midrule",
    ]
    for row in counts.itertuples(index=False):
        lines.append(f"{latex_text(row.framework)} & {row.documents:,} & {row.repositories:,} \\\\")
    lines += [
        "\\midrule",
        f"\\textbf{{Total}} & \\textbf{{{documents:,}}} & \\textbf{{{repositories:,}}} \\\\",
        "\\bottomrule",
        "\\end{tabular}",
        "\\end{table}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    """Count the planning documents per framework and write the CSV and the LaTeX table."""
    documents = pd.read_csv(DATA_DIR / "planning-documents.csv", keep_default_na=False,
                            na_values=[""])
    counts = framework_counts(documents)
    counts.to_csv(OUTPUT_DIR / "planning_documents.csv", index=False)
    total_repositories = documents["repo_name"].nunique()
    (OUTPUT_DIR / "planning_documents.tex").write_text(
        latex_table(counts, len(documents), total_repositories))
    logs.info(COMPONENT, f"Wrote planning_documents.csv and planning_documents.tex "
                         f"({len(documents):,} planning documents of {len(counts):,} "
                         f"frameworks in {total_repositories:,} repositories).")


if __name__ == "__main__":
    main()
