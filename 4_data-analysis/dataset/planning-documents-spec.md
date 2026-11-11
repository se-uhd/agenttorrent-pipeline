# Planning documents

## Context

`planning_documents.py` counts the planning documents of the current dataset release, and the
configured repositories that have them, per `framework`: the framework or the tool that writes the
documents, such as `openspec` or `cursor`. Planning documents are not configuration artifacts
([docs/heuristics.md](../../docs/heuristics.md#planning-documents)). No figure of the mechanisms counts them, and the
release publishes them separately in `planning-documents.csv`.

## Key decisions

- **Language:** Python 3.14, in the single script `planning_documents.py`, with `pandas` for the CSV
- A row of `planning-documents.csv` is one document, and a repository counts once per framework
  however many documents it has. The total row counts each repository once, even where it has
  documents of two frameworks
- The frameworks are ordered by their number of documents, the most first, and by name where two
  frameworks have equally many
- The table names a framework as the CSV does, escaped for LaTeX (`_shared.latex_text`)

## Script: `planning_documents.py`

### CLI interface

No CLI. `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` reach it through `_shared`.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or
`$ANALYSIS_OUTPUT_DIR/dataset/`

### Function structure

- **`framework_counts(documents)`**: the documents and the repositories per framework, sorted
- **`latex_table(counts, documents, repositories)`**: the counts as a LaTeX table with a total
  row
- **`main()`**: reads `planning-documents.csv`, writes `planning_documents.csv` and
  `planning_documents.tex`, and logs the totals

## Outputs

| File | Content |
|---|---|
| `planning_documents.csv` | `framework`, `documents`, `repositories`, one row per framework |
| `planning_documents.tex` | The same counts as a LaTeX table with a total row |

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `planning-documents.csv` is missing | `FileNotFoundError`, exit 1 |
| No planning document | A `planning_documents.csv` with its header alone and a table with only the total row |
