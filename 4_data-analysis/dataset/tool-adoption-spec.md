# Tool adoption

## Context

`tool_adoption.py` draws tool adoption for the current dataset release: one horizontal bar per tool with the number of configured repositories that adopted it and that number as a share of all configured repositories. `no_specific_tool` is one of the seven bars, in its place in `TOOLS` order. That bar counts the repositories with a detected mechanism and none of the six tool flags. All configuration artifacts of such a repository are shared configuration artifacts, such as `AGENTS.md` or `.agents/skills`. The counts come from the current `repos.csv`.

## Key decisions

- **Language:** Python 3.14, in the single script `tool_adoption.py`, with `pandas` for the CSV, `numpy` and `matplotlib` for the figure, and `seaborn` through `_shared.configure_plot_style`
- The tools and their labels, `TOOLS` and `TOOL_LABELS`, come from `_shared` in the order that [CLAUDE.md](../../CLAUDE.md#constants) states, so that the figure and every other tool analysis name the same seven groups in the same order
- A flag is true when it reads `TRUE` in any letter case and false otherwise, through `str(v).upper() == "TRUE"`, the coercion that [CLAUDE.md](../../CLAUDE.md#analysis-scripts) states. An empty cell is false
- The share is of every row of `repos.csv`, and the bars do not sum to one hundred percent, because a repository with two tools is in two bars
- The bars are in reverse order, so that the first tool of `TOOLS` is at the top, and each bar is labeled `<count> (<share>%)` with the share to <!--s:parameters.4_data_analysis.dataset.tool_adoption.share_decimals:words-->one<!--/s--> decimal
- The PDF embeds TrueType fonts (`pdf.fonttype 42`), as every figure of the stage does

## Script: `tool_adoption.py`

### CLI interface

No CLI. `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` reach it through `_shared`.

### Architecture

The script has one function, `main`, which reads the CSV, coerces the seven tool columns, counts the true flags and draws the bars. Because no helper returns the counts, the suite reads them back through a recording stand-in for `plt`.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or `$ANALYSIS_OUTPUT_DIR/dataset/`

Imported from `_shared`: `DATA_DIR`, `TOOLS`, `TOOL_LABELS`, `configure_plot_style`, `output_dir`.

### Function structure

**`main()`**: the style, `repos.csv`, the seven columns as booleans, `{label: count}`, the bars with `<count> (<share>%)` labels, the title with `n`, the PDF

### Processing pipeline

1. Apply the shared plot style
2. Read `repos.csv` and coerce each of the seven tool columns to booleans
3. Count the true cells per tool. `n` is the number of rows
4. Draw the bars in `TOOLS` order, the first at the top, with the count and the share of `n` beside each
5. Save `tool_adoption.pdf` with `n = <rows>` in the title

### Output

`tool_adoption.pdf`: seven horizontal bars, `Number of Repositories` against `Tool`, each labeled `<count> (<share>%)`. The script logs `Wrote <path>`.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| A tool column missing from `repos.csv` | `KeyError` from pandas, exit 1 |
| A flag spelled `true` or `True` | Counted, because `TRUE` in any letter case is true |
| An empty cell or `NaN` | False |
| A tool with no repository | A bar at zero labeled `0 (0.0%)` |
| `repos.csv` missing from `DATA_DIR` | `FileNotFoundError` from pandas, exit 1 |
| Two rows with the same `repo_name` | Both count. The script removes no duplicates, because the detection step writes one row per repository |

## Files

| File | Action |
|------|--------|
| `tool_adoption.py` | Main script |
| `../../data/collection/output/repos.csv` | Read: the seven tool columns |
| `../_shared.py` | Imported: `DATA_DIR`, `TOOLS`, `TOOL_LABELS`, `configure_plot_style`, `output_dir` |
| `tool_adoption.pdf` | Written: the figure, tracked |
| `../test_dataset_tool_adoption.py` | Tests |

## Usage

```bash
cd 4_data-analysis/dataset
python3 tool_adoption.py

# Against the fixture, writing under /tmp/figures/dataset/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 tool_adoption.py
```

## Tests

See `../test_dataset_tool_adoption.py`, a standard library `unittest` suite that needs pandas, numpy, matplotlib and seaborn. The suite loads the script by path with the Agg backend. Because the arithmetic is in `main`, the suite runs `main` with `DATA_DIR` pointed at a `repos.csv` built in a temporary directory and the module's `plt` replaced by a recording stand-in, and reads the bar labels, the tick labels and the title back.

- **`TestTheCounts`**: Counts and shares over four repositories, with `true` in lower case and an empty cell among the flags. The bars are in tool order, with the first tool of `TOOLS` on top, and a missing tool column stops the script
- **`TestOverTheFixture`**: The counts of each tool and of no specific tool among the fixture's repositories are the ones computed from `testdata/output` by hand, and they agree with a stdlib count of the flags
- **`TestMainOverTheFixture`**: a subprocess over the fixture with `ANALYSIS_OUTPUT_DIR` set writes the PDF under the temporary root and leaves nothing new beside the script

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_dataset_tool_adoption -v
```

## Verification

1. Check the bar values against the current tool flags and the `no_specific_tool` flag, and the `n` in the title against the rows of `repos.csv`.
2. From `4_data-analysis/`, run `python3 check_reproduction.py --only dataset/tool_adoption`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked figure.

## Known limitations

- No function returns the counts. A test that needs them reads them from the figure's labels through a stand-in for pyplot, and a function `tool_counts(df)` would let a suite assert them directly
- The share is of every configured repository, and the figure does not show how many repositories use more than one tool. `rq2/tool_count_distribution.py` shows that number
