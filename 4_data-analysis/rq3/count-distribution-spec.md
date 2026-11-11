# Count distribution per repository

## Context

`count_distribution.py` shows how many context files, skills and subagents a repository has, as a grouped bar chart in percent of the repositories that have at least one configuration artifact of that mechanism. Counts one to <!--s:parameters.4_data_analysis.rq3.count_distribution.MAX_INDIVIDUAL:words-->seven<!--/s--> get a bar each and <!--s:parameters.4_data_analysis.rq3.count_distribution.overflow_bin:words-->eight<!--/s--> or more is one bin. `_website/build_site.py` rebuilds the same figure from the release CSVs rather than reading this PDF.

## Key decisions

- **Language:** Python 3.14, in the single script `count_distribution.py`, with `pandas` and `numpy` for the counts and `matplotlib` for the figure
- The denominator is the repositories in the CSV, not the sample. A repository without a skill does not count as a repository with zero skills, and each series is therefore in percent of its own `n`, which the legend gives
- The bins are fixed at `1..7` and `8+`. `build_distribution` clips the counts at <!--s:parameters.4_data_analysis.rq3.count_distribution.overflow_bin:words-->eight<!--/s--> and reindexes over `1..8` with zero for an empty bin. An empty bin then keeps its position on the axis as a bar of height zero
- A count is a row count. `count_per_repo` reads `repo_name` and the CSV's path column and counts the rows per repository. Because the script names the path column, pandas raises an error for a CSV with the wrong columns instead of counting its rows
- `COLORS` repeats the shared colors for Skills and Subagents and uses the CLAUDE.md blue for Context Files

## Script: `count_distribution.py`

### CLI interface

The script takes no arguments. It reads `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` through `_shared`.

### Architecture

`count_per_repo` counts the rows of one CSV per repository, and `build_distribution` bins one such Series. `main` runs both over the three CSVs, logs the summary statistics and draws the grouped bars.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/rq3/` or `$ANALYSIS_OUTPUT_DIR/rq3/`

**`COLORS`**: one color per series, for Context Files, Skills and Subagents

**`MAX_INDIVIDUAL`**: `7`, the last bin for a single count, followed by `8+`

Imported from `_shared`: `DATA_DIR`.

### Function structure

**`count_per_repo(csv_path, item_col)`**: rows per `repo_name`, reading only `repo_name` and `item_col`

**`build_distribution(counts)`**: the counts clipped at `MAX_INDIVIDUAL + 1`, counted per value, reindexed over `1..MAX_INDIVIDUAL + 1` with zero for an empty bin

**`main()`**: the three CSVs, the logged min, max, median and mean per series, the distributions in percent, the figure

### Processing pipeline

1. Count rows per repository in `context-files.csv` (`context_file`), `skills.csv` (`skills.md`) and `subagents.csv` (`subagent`)
2. Log `n`, min, max, median and mean per series
3. Bin each series and divide by its `n` to get percent
4. Draw three bars per bin, side by side, with the bin labels `1` to `7` and `8+`
5. Save `count_distribution.pdf`

### Output

`count_distribution.pdf`: grouped bars, `Percentage of Repositories` against `Number of Configuration Artifacts per Repository`, one legend entry per series with its `n`. The script logs `Configuration artifacts per repository, over the repositories with at least one` with one line per series, and the path of the saved PDF.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| A count above <!--s:parameters.4_data_analysis.rq3.count_distribution.MAX_INDIVIDUAL:words-->seven<!--/s--> | The `8+` bin |
| A bin with no repository | Present, at zero |
| A CSV with no rows | `n=0` and `nan` statistics logged, and the percentages are `NaN`, which leaves the series without visible bars |
| A CSV without the named path column | `ValueError` from pandas' `usecols`, exit 1 |
| A CSV missing from `DATA_DIR` | `FileNotFoundError` from pandas, exit 1 |

## Files

| File | Action |
|------|--------|
| `count_distribution.py` | Main script |
| `../../data/collection/output/context-files.csv` | Read: `repo_name`, `context_file` |
| `../../data/collection/output/skills.csv` | Read: `repo_name`, `skills.md` |
| `../../data/collection/output/subagents.csv` | Read: `repo_name`, `subagent` |
| `../_shared.py` | Imported: `DATA_DIR`, `output_dir` |
| `count_distribution.pdf` | Written: the figure, tracked |
| `../test_rq3_count_distribution.py` | Tests |

## Usage

```bash
cd 4_data-analysis/rq3
python3 count_distribution.py

# Against the fixture, writing under /tmp/figures/rq3/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 count_distribution.py
```

## Tests

See `../test_rq3_count_distribution.py`, a standard library `unittest` suite that needs pandas, numpy and matplotlib. It loads the script by path with the Agg backend and replaces `DATA_DIR` and `OUTPUT_DIR` with temporary directories.

- **`TestBuildDistribution`**: Counts above <!--s:parameters.4_data_analysis.rq3.count_distribution.MAX_INDIVIDUAL:words-->seven<!--/s--> share the last bin. Every bin is present, and the bins sum to the repositories. The boundary is `MAX_INDIVIDUAL`, and an empty Series gives zero in every bin
- **`TestCountPerRepo`**: rows per repository, and the item column must be there
- **`TestMainOverSmallMechanismCsvs`**: the logged statistics and the PDF over three small CSVs, and the bars read back off the axes: every height a percentage of the CSV's repositories, the bin labels `1` to `7` and `8+`, and each legend entry with its `n`
- **`TestMainOverTheFixture`**: The bar heights, the bin labels and the legend over `testdata/output`, from the rows per repository counted by hand. A subprocess over `testdata/output` logs the repository counts and the largest count per repository of Context Files, Skills and Subagents, as computed from the fixture by hand, and writes the PDF under the temporary root, with nothing new beside the script

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_rq3_count_distribution -v
```

## Verification

1. `python3 check_reproduction.py --only rq3/count_distribution` from `4_data-analysis/` exits 0
2. Each logged `n` equals the number of distinct `repo_name` values in its CSV, and each logged `max` equals the largest number of rows that one repository has in that CSV

## Known limitations

- This script and `_website/build_site.py` implement the figure separately, and the two can therefore disagree without either failing
- Because each series is in percent of its own `n`, the three series are not comparable as shares of the sample
