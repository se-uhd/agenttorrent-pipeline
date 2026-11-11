"""Imports: the imports of each tool by the status of their target.

Counts the rows of `imports.csv` per tool and status of the target (found, missing, outside the
repository, not read, over the tool's limit, pruned or not copied), with the importing files and
the repositories that have them, and writes the counts as a CSV and as a LaTeX table. Imports
are no configuration artifacts, and the release publishes them separately. The table describes
the current dataset release.

Input:
  ../../data/collection/output/imports.csv

Output:
  imports.csv
  imports.tex
"""

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _shared import DATA_DIR, latex_text, output_dir
from agentcontext import logs
from agentcontext.imports import STATUSES

OUTPUT_DIR = output_dir(__file__)
COMPONENT = "dataset_imports"


def tool_counts(imports: pd.DataFrame) -> pd.DataFrame:
    """Per tool: the imports by status, the importing files and the repositories."""
    by_status = (imports.pivot_table(index="tool", columns="target_status", values="import",
                                     aggfunc="size", fill_value=0)
                 .reindex(columns=STATUSES, fill_value=0))
    # The distinct importing files per tool as a Series, which `assign` takes also when the CSV
    # has no row. A function applied per group returns an empty DataFrame over no row, which
    # `assign` refuses.
    files = imports.drop_duplicates(["tool", "repo_name", "importer"]).groupby("tool").size()
    counts = by_status.assign(imports=by_status.sum(axis=1), importing_files=files,
                              repositories=imports.groupby("tool")["repo_name"].nunique())
    counts = counts.reset_index()[["tool", "imports", *STATUSES, "importing_files",
                                   "repositories"]]
    return counts.sort_values(["imports", "tool"], ascending=[False, True], ignore_index=True)


def latex_table(counts: pd.DataFrame, importing_files: int, repositories: int) -> str:
    """The imports, the found targets, the importing files and the repositories per tool as a
    LaTeX table with a total row, which counts a file that several tools read once."""
    lines = [
        "\\begin{table}[t]",
        "\\centering",
        "\\caption{Imports of the configuration artifacts per tool.}",
        "\\label{tab:imports}",
        "\\begin{tabular}{lrrrr}",
        "\\toprule",
        "\\textbf{Tool} & \\textbf{Imports} & \\textbf{Found} & \\textbf{Files} & "
        "\\textbf{Repos} \\\\",
        "\\midrule",
    ]
    for row in counts.itertuples(index=False):
        lines.append(f"{latex_text(row.tool)} & {row.imports:,} & {row.found:,} & "
                     f"{row.importing_files:,} & {row.repositories:,} \\\\")
    lines += [
        "\\midrule",
        f"\\textbf{{Total}} & \\textbf{{{counts['imports'].sum():,}}} & "
        f"\\textbf{{{counts['found'].sum():,}}} & "
        f"\\textbf{{{importing_files:,}}} & \\textbf{{{repositories:,}}} \\\\",
        "\\bottomrule",
        "\\end{tabular}",
        "\\end{table}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    """Count the imports per tool and status and write the CSV and the LaTeX table."""
    imports = pd.read_csv(DATA_DIR / "imports.csv", keep_default_na=False, na_values=[""],
                          dtype=str)
    counts = tool_counts(imports)
    counts.to_csv(OUTPUT_DIR / "imports.csv", index=False)
    repositories = imports["repo_name"].nunique()
    importing_files = len(set(zip(imports["repo_name"], imports["importer"])))
    (OUTPUT_DIR / "imports.tex").write_text(latex_table(counts, importing_files, repositories))
    logs.info(COMPONENT, f"Wrote imports.csv and imports.tex ({len(imports):,} imports of "
                         f"{len(counts):,} tools in {repositories:,} repositories).")


if __name__ == "__main__":
    main()
