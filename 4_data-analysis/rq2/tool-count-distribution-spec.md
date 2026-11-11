# Repositories by number of configured tools

## Context

`tool_count_distribution.py` counts how many of the six tools each repository configures and writes the distribution over 0 to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.max_tools-->6<!--/s--> as a CSV, an annotated bar chart and a LaTeX table. The `0` bucket contains every repository with no named tool flag, including a repository with shared configuration that identifies no tool. The chart and the table label that bucket `0`, and the script logs separately how many repositories have no specific tool.

## Key decisions

- **Language:** Python 3.14, in the single script `tool_count_distribution.py`, with `pandas`, `matplotlib` and `seaborn`
- The tools are `_shared.SPECIFIC_TOOLS`, the six without `no_specific_tool`, and the count per repository runs from 0 to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.max_tools-->6<!--/s-->
- `reindex(range(0, len(TOOLS) + 1), fill_value=0)` keeps every bucket, and an empty bucket is a row of 0 rather than a missing row
- The script reads the `no_specific_tool` column when it is present and synthesizes it as all-False when it is absent. The script uses the column only for the note logged under the distribution, and the column does not enter the counts. A repository with no tool flag is therefore in the `0` bucket whether or not it has `no_specific_tool`
- Because the script rounds the shares to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.share_decimals:words-->one<!--/s--> decimal before it writes them, the `pct` column sums to 100.0 only up to rounding

## Script: `tool_count_distribution.py`

### CLI interface

The script takes no arguments. It reads the CSV from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and writes its outputs to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

The script is one `main()`, which reads, coerces, sums per row, reindexes `value_counts` over 0 to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.max_tools-->6<!--/s-->, and writes the CSV, the chart and the TeX.

### Module-level data

The module-level names are `OUTPUT_DIR`, `DATA_DIR` from `_shared`, and `TOOLS`, an alias of `SPECIFIC_TOOLS`.

### Function structure

**`main()`**: reads `repos.csv`, computes the distribution, writes `tool_count_distribution.csv`, `tool_count_distribution.pdf` and `tool_count_distribution.tex`, and logs the distribution and the note on the repositories with no specific tool

### Processing pipeline

1. Read `repos.csv`, coerce the six tool columns, and coerce or synthesize `no_specific_tool`
2. `tool_count = repos[TOOLS].sum(axis=1)`
3. `dist = tool_count.value_counts().reindex(range(0, len(TOOLS) + 1), fill_value=0).sort_index()`, then `pct = (dist / dist.sum() * 100).round(1)`
4. Log the distribution and `k repositories have none of the six tool flags and only shared configuration artifacts`
5. Write the CSV
6. Draw a bar per bucket, labeled `count\n(pct%)`, with numeric ticks from `0` through `6`, and save the PDF
7. Build the TeX: a row per bucket, `0` for zero named tools, and a bold Total row of `N` and `100.0\%`
8. Write the TeX

### Output

`tool_count_distribution.csv`, <!--s:analysis.tool_count_distribution.buckets:words-->seven<!--/s--> rows, columns `num_tools,num_repos,pct`, `num_tools` 0 to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.max_tools-->6<!--/s-->.

`tool_count_distribution.pdf`, <!--s:analysis.tool_count_distribution.buckets:words-->seven<!--/s--> bars.

`tool_count_distribution.tex`, a `table` with a `tabular{crr}`: `# Tools`, `Repos`, `%`. It has <!--s:analysis.tool_count_distribution.buckets:words-->seven<!--/s--> rows and a Total row.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` absent | `FileNotFoundError` from pandas |
| `no_specific_tool` column absent | Synthesized as False, and the note states 0 |
| A bucket with no repository | A row of `0` and `0.0` |
| A flag spelled `true` | Read as TRUE |
| An empty `repos.csv` | `dist / 0` gives NaN shares, and the chart's `set_ylim` fails on NaN |
| `pct` rounding | The column may sum to 99.9 or 100.1, while the Total row states `100.0\%` regardless |

## Files

| File | Action |
|------|--------|
| `tool_count_distribution.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `tool_count_distribution.csv` | Written |
| `tool_count_distribution.pdf` | Written |
| `tool_count_distribution.tex` | Written |
| `../test_rq2_tool_count_distribution.py` | Tests |

## Usage

```bash
python3 tool_count_distribution.py
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 tool_count_distribution.py
```

## Tests

See `../test_rq2_tool_count_distribution.py`, a standard library `unittest` suite that needs pandas, matplotlib, seaborn and Poppler, which inspects the PDF labels, and runs under `MPLBACKEND=Agg`. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. `main()` runs in-process over an eight-row `repos.csv` with `DATA_DIR` and `OUTPUT_DIR` replaced. The test cases assert the CSV and the TeX, and one test case runs the script as a subprocess over `testdata/output/`.

- **`TestPortableConfiguration`** (1): shared skills and an AGENTS.md, each a repository's only configuration, both count as zero named tools and both as no specific tool, so the note states 2. The PDF names no AGENTS.md, and the LaTeX row is `0 & 2 & 66.7`
- **`TestToolAttribution`** (1): one repository per named tool counts as one, while shared instructions count as zero
- **`TestTheDistribution`** (4): A bucket for each number of named tools from 0 to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.max_tools-->6<!--/s-->, with 2, 3, 2, 0, 0, 1, 0. The buckets partition the population, and the shares sum to 100. A `repos.csv` without `no_specific_tool` runs with the note stating 0, and the note and the header are logged
- **`TestTheTex`** (1): a row per bucket with `0` for zero named tools and the Total row
- **`TestTheRun`** (2): the three files, and nothing written beside the script
- **`TestOverTheFixture`** (1): The repositories of the fixture by zero to six named tools, as its `repos.csv` counts them, with the shares to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.share_decimals:words-->one<!--/s--> decimal, and the same rows and total in the TeX

Run with:

```shell
python3 -m unittest test_rq2_tool_count_distribution -v
```

## Verification

1. Count the six named tool flags of each input repository yourself, and compare the distribution with the CSV that the script writes.
2. Check that `num_repos` sums to the input population and that shared instructions alone contribute zero named tools.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/tool_count_distribution`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- A count of zero named tools does not establish which shared configuration a repository has, or that the repository uses no tool, because the tool flags of the input describe detected configuration.
