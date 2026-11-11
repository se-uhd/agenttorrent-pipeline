# Pairwise co-occurrence of the tools

## Context

`tool_cooccurrence.py` counts, for each of the <!--s:analysis.tool_cooccurrence.pairs-->21<!--/s--> pairs of the seven tool flags, the repositories that configure both tools, one of them or neither. It writes the symmetric 7x7 matrix in three forms: a CSV of the pairs, a heatmap colored by the shared count, and a LaTeX table with the count above the diagonal, the share of the smaller tool's repositories below it, and each tool's total on the diagonal. `N` is the row count of the input. `_website/build_site.py` rebuilds the matrix from `release/`.

## Key decisions

- **Language:** Python 3.14, in the single script `tool_cooccurrence.py`, with `pandas`, `matplotlib` and `seaborn`
- The tools are all seven of `_shared.TOOLS`, in that order, including `no_specific_tool`. By definition, `no_specific_tool` shares no repository with any tool, and its counts with every tool are therefore zero
- The share is relative to the smaller tool, `both / min(n_a, n_b) * 100`, that is, the share of the smaller population that is also in the larger. A pair with an empty tool gets 0.0 rather than a division by zero
- The script does not deduplicate, because every row of `repos.csv` is one repository, and `N` is the row count
- The heatmap is colored by count and labeled with the count and the share, `count\n(pct%)` off the diagonal and `n=total` on it. A label turns white above <!--s:parameters.4_data_analysis.rq2.tool_cooccurrence.white_label_share-->60%<!--/s--> of the largest count

## Script: `tool_cooccurrence.py`

### CLI interface

The script takes no arguments. It reads the CSV from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and writes its outputs to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

The script is one `main()`, which reads, coerces, counts the pairs, writes the CSV, builds the two matrices, logs the top <!--s:parameters.4_data_analysis.rq2.tool_cooccurrence.top_pairs:words-->five<!--/s--> pairs, draws the heatmap and writes the TeX.

### Module-level data

The module defines `OUTPUT_DIR` and imports `DATA_DIR`, `TOOLS`, `TOOL_LABELS` and `latex_text`, through which the TeX writes each label, from `_shared`.

### Function structure

**`main()`**: reads `repos.csv`, counts the <!--s:analysis.tool_cooccurrence.pairs-->21<!--/s--> pairs, writes `tool_cooccurrence.csv`, `tool_cooccurrence.pdf` and `tool_cooccurrence.tex`, and logs the totals and the <!--s:parameters.4_data_analysis.rq2.tool_cooccurrence.top_pairs:words-->five<!--/s--> largest pairs

### Processing pipeline

1. Read `repos.csv`, coerce the seven tool columns with `str(v).upper() == "TRUE"`, and set `n = len(repos)`
2. `tool_n[t] = repos[t].sum()`
3. For each pair of `itertools.combinations(TOOLS, 2)`, count both, only a, only b and neither
4. Write the CSV
5. `count_matrix`: the totals on the diagonal, `both` off it. `pct_matrix`: 100 on the diagonal, `both / min(n_a, n_b) * 100` off it, 0.0 when the minimum is 0
6. Log the totals and the <!--s:parameters.4_data_analysis.rq2.tool_cooccurrence.top_pairs:words-->five<!--/s--> pairs with the largest `both`
7. Draw the heatmap over the count matrix with the count and share labels, and save the PDF
8. Build the TeX rows: `($n$\,=\,total)` on the diagonal, the count above, `pct\%` below, and a note with `$N$\,=\,n repositories`
9. Write the TeX

### Output

`tool_cooccurrence.csv`, <!--s:analysis.tool_cooccurrence.pairs-->21<!--/s--> rows, columns `tool_a,tool_b,both,only_a,only_b,neither`. The four counts of every row sum to `N`.

`tool_cooccurrence.pdf`, a 7x7 heatmap with the labels of `TOOL_LABELS`.

`tool_cooccurrence.tex`, a `table` with a `tabular{lrrrrrrr}`, one row per tool in `TOOLS` order, and a `minipage` note naming `N`. The caption names the share's denominator.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` absent | `FileNotFoundError` from pandas |
| A tool with no repository | Its row and column read 0 and `0.0\%`, without a division |
| A flag spelled `true` | Read as TRUE |
| Duplicate `repo_name` rows | Counted twice, and `repos.csv` has none |
| An empty `repos.csv` | `N` = 0 and every cell 0, and the heatmap's white-label threshold compares against 0 |

## Files

| File | Action |
|------|--------|
| `tool_cooccurrence.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `tool_cooccurrence.csv` | Written |
| `tool_cooccurrence.pdf` | Written |
| `tool_cooccurrence.tex` | Written |
| `../test_rq2_tool_cooccurrence.py` | Tests |

## Usage

```bash
python3 tool_cooccurrence.py
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 tool_cooccurrence.py
```

## Tests

See `../test_rq2_tool_cooccurrence.py`, a standard library `unittest` suite that needs pandas, matplotlib and seaborn and runs under `MPLBACKEND=Agg`. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. Because the arithmetic is inline in `main()`, `main()` runs in-process over a six-row `repos.csv` with `DATA_DIR` and `OUTPUT_DIR` replaced, and the test cases assert the CSV and the TeX rows. One test class runs the script as a subprocess over `testdata/output/`.

- **`TestTheCsv`** (3): A row per pair of tool flags, with the columns of `tool_cooccurrence.csv`. The four cells sum to the population, and one pair's cells are counted by hand
- **`TestTheTex`** (4): The diagonal is each tool's total, with the count above and the share of the smaller tool below. A tool with no repository gives `0.0\%` rather than a division, and the note names `N`
- **`TestTheRun`** (2): the three files and the logged top pair, and nothing written beside the script
- **`TestOverTheFixture`** (3): Exit 0 and the three files. The `claude`/`copilot` cells, the totals of the six tools on the diagonal and `N` are the counts of the fixture's `repos.csv`

Run with:

```shell
python3 -m unittest test_rq2_tool_cooccurrence -v
```

## Verification

1. Check the diagonal counts against the current tool flags, and the population of the figure against its input rows.
2. Check that `both + only_a + only_b + neither` equals that population for each pair. Every pair of `no_specific_tool` with a named tool must share no repository.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/tool_cooccurrence`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- Because the share is relative to the smaller tool, a cell is not symmetric in meaning even though the matrix is symmetric in value
