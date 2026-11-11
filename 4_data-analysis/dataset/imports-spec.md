# Imports

## Context

`imports.py` counts the imports of the current dataset release by the tool that reads them and
by the status of their target, with the importing files and the configured repositories that
have them. The tools are `claude` for Claude Code and Claude Code Review, `copilot`, `gemini` for
Gemini CLI and Antigravity, and `cursor` for Bugbot. Imports are not configuration artifacts
(`docs/steps/import_detection.md`). No figure of the mechanisms counts them, and the release
publishes them separately in `imports.csv`.

## Key decisions

- **Language:** Python 3.14, in the single script `imports.py`, with `pandas` for the CSV
- A row of `imports.csv` is one import of one tool's chain. An import that two tools read
  therefore counts once for each tool, as the CSV records it
- The statuses are those of `agentcontext.imports.STATUSES`, imported rather than copied and kept
  in its order. A status without a row is then a column of zeros rather than missing, and a status
  that `agentcontext/imports.py` adds to `STATUSES` becomes a column of `imports.csv`
- An importing file counts once per tool. The total row of the table counts each importing file
  and each repository once, even where several tools read it
- The tools are ordered by their number of imports, the most first, and by name where two tools
  have equally many

## Script: `imports.py`

### CLI interface

No CLI. `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` reach it through `_shared`.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or
`$ANALYSIS_OUTPUT_DIR/dataset/`

**`STATUSES`**: the statuses of a target in the order of the CSV's columns, from
`agentcontext.imports`

### Function structure

- **`tool_counts(imports)`**: per tool, the imports by status, their total, the importing files
  and the repositories, sorted
- **`latex_table(counts, importing_files, repositories)`**: the imports, the found targets, the
  importing files and the repositories per tool, with a total row
- **`main()`**: reads `imports.csv`, writes `imports.csv` and `imports.tex` under the output
  directory, and logs the totals

## Outputs

| File | Content |
|---|---|
| `imports.csv` | `tool`, `imports`, one column per status, `importing_files`, `repositories` |
| `imports.tex` | The imports, the found targets, the importing files and the repositories per tool as a LaTeX table with a total row |

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `imports.csv` is missing | `FileNotFoundError`, exit 1 |
| A status has no row | Its column is zero for every tool |
| `imports.csv` has its header alone, as in a release in which no configuration artifact imports a file | `imports.csv` has its header alone, and `imports.tex` has no tool row and a total row of zeros. `tool_counts` counts the importing files per tool with `groupby(...).size()`, which returns a Series even over no rows, and `assign` accepts that Series |
