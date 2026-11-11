# Six-set Venn diagram of tool usage

## Context

`venn_diagram.py` draws the six tools as sets of repository names and their overlaps, with AGENTS.md as a callout box rather than a seventh set. The box shows the number of repositories with an `AGENTS.md` and how many of them have no tool flag. The six set sizes and both callout counts come from the current input flags. The `venn` package draws the diagram, and this script loads and coerces the CSV and builds the sets.

## Key decisions

- **Language:** Python 3.14, in the single script `venn_diagram.py`, with `pandas`, `matplotlib` on the `Agg` backend, and `venn`
- The loader coerces every column that is not `repo_name` and not in `NON_FLAG_COLUMNS` to boolean, so that it can read a `repos.csv` with flag columns that it does not list. The cost is that a text column left out of `NON_FLAG_COLUMNS` reads as FALSE in every row without an error. The list names the <!--s:parameters.4_data_analysis.rq2.venn_diagram.NON_FLAG_COLUMNS.length:words-->six<!--/s--> non-flag columns of `repos.csv`, and the suite checks the fixture's header against it, so that a renamed or added text column fails the suite rather than reading as an all-False flag
- The drop skips a missing non-flag column (`if col in df.columns`), and a `repos.csv` without `head_sha` loads
- The script draws the callout only when a repository has an `AGENTS.md`. The exclusive count of the callout is the number of repositories with an `AGENTS.md` and `no_specific_tool`, or 0 when the `no_specific_tool` column is absent. `no_specific_tool` alone also counts the repositories with portable skills or a root `.mcp.json` as their shared files and no `AGENTS.md`, which a callout about `AGENTS.md` leaves out
- The six sets are the six tools in `Claude, Copilot, Cursor, Gemini, Codex, OpenCode` order, with fixed colors. Six sets are the most that the `venn` package draws

## Script: `venn_diagram.py`

### CLI interface

The script takes no arguments. It reads `repos.csv` from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and writes the figure to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

`load_repos` reads and coerces. `venn_diagram_for_agent_adoption` builds the sets, draws, annotates and saves. `main` runs both in order.

### Module-level data

`NON_FLAG_COLUMNS` lists `branch`, `scanned_at`, `head_sha`, `head_commit_date`, `github_link` and `dataset_version`. `OUTPUT_DIR` is the output directory, and `DATA_DIR` comes from `_shared`.

### Function structure

**`load_repos()`**: `repos.csv` with the <!--s:parameters.4_data_analysis.rq2.venn_diagram.NON_FLAG_COLUMNS.length:words-->six<!--/s--> non-flag columns dropped and every other column but `repo_name` coerced with `str(v).upper() == "TRUE"`

**`venn_diagram_for_agent_adoption(df)`**: six sets of `repo_name` by tool, `venn.venn` over them, the AGENTS.md callout when the count is above 0, `venn-tool-usage.pdf`

**`main()`**: both functions in order

### Processing pipeline

1. Read `repos.csv`, drop each of `NON_FLAG_COLUMNS` that is present, and coerce the rest
2. `{"Claude": set of names with claude, ...}` for the six tools
3. `venn.venn(sets, fontsize=16, cmap=..., alpha=0.4)`
4. `agents_md_count = df["agents.md"].sum()` and `exclusive_count = (df["agents.md"] & df["no_specific_tool"]).sum()` or 0, and when `agents_md_count` is above 0, `plt.annotate("AGENTS.md\n(n=<agents_md_count>, <exclusive_count> exclusive)", ...)` in the lower-left corner
5. `_shared.save_figure(plt.gcf(), ..., dpi=300)`, and log `Wrote venn-tool-usage.pdf`

### Output

`venn-tool-usage.pdf`, a six-set diagram with a callout box.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` absent | `FileNotFoundError` from pandas |
| A non-flag column absent | Skipped by the drop |
| A text column absent from `NON_FLAG_COLUMNS` | Coerced to all-False without an error, and the schema test of the suite fails on it |
| `agents.md` column absent | `KeyError` in the annotation |
| No repository with an `AGENTS.md` | No callout |
| A tool with no repository | An empty set, which the package draws |
| A flag spelled `true` | Read as TRUE |

## Files

| File | Action |
|------|--------|
| `venn_diagram.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `venn-tool-usage.pdf` | Written |
| `../test_rq2_venn_diagram.py` | Tests |

## Usage

```bash
python3 venn_diagram.py
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 venn_diagram.py
```

## Tests

See `../test_rq2_venn_diagram.py`, a standard library `unittest` suite that needs pandas, matplotlib and venn. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. `load_repos` runs over a five-row `repos.csv` with `DATA_DIR` replaced. The drawing runs with `OUTPUT_DIR` replaced and `plt.annotate` wrapped, so that the test cases can assert the text of the callout. One test case runs the script as a subprocess over `testdata/output/`.

- **`TestLoadRepos`** (5): The <!--s:parameters.4_data_analysis.rq2.venn_diagram.NON_FLAG_COLUMNS.length:words-->six<!--/s--> columns are dropped, and a `repos.csv` without two of them loads. `repo_name` stays text, and every other column is boolean. `TRUE`, `true` and `True` read as True, and `FALSE`, an empty cell and `yes` as False. The flags of a row
- **`TestTheSchemaAgainstTheFixture`** (2): Every column of the fixture's header outside `NON_FLAG_COLUMNS` and `repo_name` contains `TRUE` or `FALSE` in every row, which [Key decisions](#key-decisions) explains. Every listed column is in the header
- **`TestTheDiagram`** (6): The six sets handed to `venn.venn`, in order, are the repositories of each tool column. The PDF and the logged name. The callout reads `AGENTS.md\n(n=2, 1 exclusive)`, and the exclusive count leaves out a `no_specific_tool` repository without an `AGENTS.md`. No callout when no repository has an `AGENTS.md`, and nothing written beside the script
- **`TestOverTheFixture`** (1): a subprocess over `testdata/output/` logs the rows of the fixture's `repos.csv` and writes the one PDF

Run with:

```shell
python3 -m unittest test_rq2_venn_diagram -v
```

## Verification

1. Check that the columns that the script coerces to boolean are the tool, context file and mechanism flags, and that no date, hash or link column is among them.
2. Check the AGENTS.md callout against the current count of `agents.md` and the number of repositories with both `agents.md` and `no_specific_tool`.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/venn_diagram`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- You have to add a new non-flag column of `repos.csv` to `NON_FLAG_COLUMNS` by hand, and the schema test of the suite fails until you do
- The script writes the set sizes only into the PDF, and no table
