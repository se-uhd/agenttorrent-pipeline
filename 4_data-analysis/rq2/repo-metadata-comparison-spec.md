# Repository metadata by tool: adopters against non-adopters

## Context

`repo_metadata_comparison.py` compares, for each of the seven tool flags of `repos.csv`, the repositories that have the flag against the repositories without it, on <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.METRICS.length:words-->four<!--/s--> metrics from the GHS snapshot: age in years, contributors, commits and size. Each of the <!--s:analysis.repo_metadata_comparison.comparisons-->28<!--/s--> comparisons is a two-sided Mann-Whitney U test with Cliff's delta as the effect size. Benjamini-Hochberg adjusts the p-values of the tested comparisons as one family, which contains all <!--s:analysis.repo_metadata_comparison.comparisons-->28<!--/s--> when no group is empty. The script writes the per-comparison CSV and the single-column LaTeX table for the dataset release. The table has a baseline row over every repository, then three rows per tool with the medians and stars, the interquartile ranges and the deltas. Significant cells are set in bold.

The script reads the labels of the classification and the GHS snapshot through `_repo_metadata.build_repo_metadata()` and joins the DataFrame of that helper with the tool flags of `repos.csv`. The population is the configured repositories that are present in both inputs.

## Key decisions

- **Language:** Python 3.14, in the single script `repo_metadata_comparison.py`, with `pandas`, `numpy`, `scipy` (`mannwhitneyu`), and `statsmodels` through `_shared.adjust_tested`
- The tools are all seven of `_shared.TOOLS`, including `no_specific_tool`, because the repositories with only shared configuration artifacts are a population to compare in the same way as the others
- The script combines the duplicate rows of a repository with a logical OR on `repo_name` before the join
- Age is `repo_age_days / 365.25`, and this script is the one place that converts the day count of the helper into years
- The script writes the statistics of a group with no repository as `n/a`. The median and quartiles of an empty Series are NaN, and `math.floor(NaN)` raises a `ValueError`. `_fmt_num` therefore returns `n/a` for a value that `pd.isna` accepts, and the script still writes all three files. A comparison with an empty group is not tested, just as `config_cooccurrence.py` does not test a pair with a mechanism that no repository or every repository has. The script runs no `mannwhitneyu` for such a comparison, and Cliff's delta is NaN without a division by zero. `delta_magnitude` gives `not tested`, and the TeX writes `--` for the delta, with a note that explains what `--` means. The adjustment leaves the four comparisons of that tool out
- Numbers are written round-half-up, as `math.floor(x + 0.5)`, rather than with Python's round-to-even. For example, `10.5` is written as `11` and `12,500` as `13k`
- **Three formats by magnitude:** <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.fmt.decimals:words-->one<!--/s--> decimal below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.fmt.decimal_below-->10<!--/s-->, thousands with a `k` from <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.fmt.thousands_from-->10,000<!--/s-->, and a comma-separated integer in between
- The adjustment covers the comparisons that were tested. `_shared.adjust_tested` adjusts the p-values that are not NaN among themselves and leaves the others NaN and not significant. The note counts the comparisons that it adjusted. `multipletests` over all of them would return NaN for every adjusted p-value when one raw p-value is NaN, and one tool that no repository or every repository adopts would then remove the stars and the bold of every other tool
- The stars come from the adjusted p and the bold from the reject flag of the same adjustment, and a starred cell is therefore always bold

## Script: `repo_metadata_comparison.py`

### CLI interface

The script takes no arguments. `repos.csv` comes from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`). The inputs of the helper are fixed paths under `data/classification/` and `data/sampling/`. The script writes its outputs to `data/analysis/rq2/`, or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

The script consists of seven pure helpers and `main()`. `cliffs_delta`, `delta_magnitude` and `stars` take numbers. `_fmt_num` formats one number, and `fmt_iqr`, `fmt_median` and `fmt_iqr_only` format a Series through it. `main` joins, tests, adjusts through `_shared.adjust_tested`, writes the CSV, logs a per-tool summary, and writes the TeX.

### Module-level data

`METRICS` lists the <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.METRICS.length:words-->four<!--/s--> metric columns, and `METRIC_LABELS` their long names. `OUTPUT_DIR` is the output directory. `DATA_DIR`, `TOOLS`, `TOOL_LABELS`, `adjust_tested` and `latex_text`, through which the TeX writes each label, come from `_shared`, and `build_repo_metadata` from `_repo_metadata`. `METRIC_COL_LABELS`, the short column heads of the TeX table, is local to `main`.

### Function structure

**`cliffs_delta(x, y)`**: `(#x > y - #x < y) / (|x| |y|)` by broadcasting, and NaN when either group is empty

**`delta_magnitude(d)`**: Romano et al. (2006): `negligible` below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.delta_magnitude.negligible_below-->0.147<!--/s-->, `small` below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.delta_magnitude.small_below-->0.33<!--/s-->, `medium` below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.delta_magnitude.medium_below-->0.474<!--/s-->, else `large`, on `|d|`, and `not tested` for NaN

**`stars(p)`**: `***` below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.stars.three_stars_below-->0.001<!--/s-->, `**` below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.stars.two_stars_below-->0.01<!--/s-->, `*` below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.stars.one_star_below-->0.05<!--/s-->, else empty

**`_fmt_num(v)`**: `n/a` for NaN or None, `Nk` from <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.fmt.thousands_from-->10,000<!--/s-->, <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.fmt.decimals:words-->one<!--/s--> decimal below <!--s:parameters.4_data_analysis.rq2.repo_metadata_comparison.fmt.decimal_below-->10<!--/s-->, and `N,NNN` otherwise, with halves rounded up

**`fmt_iqr(series)`**: `median (Q1--Q3)`

**`fmt_median(series)`**: the median alone

**`fmt_iqr_only(series)`**: `(Q1--Q3)`

**`main()`**: builds the metadata, joins the tool flags, runs the <!--s:analysis.repo_metadata_comparison.comparisons-->28<!--/s--> tests, adjusts the p-values of the tested comparisons, writes `repo_metadata_comparison.csv` and `repo_metadata_comparison.tex`, and logs the population and which metrics are significant per tool

### Processing pipeline

1. `metadata = build_repo_metadata()`, which writes `repo_metadata.csv`
2. Read `repos.csv`, coerce the seven tool columns, and combine duplicate names with `max`
3. Inner-join the metadata with `repo_name` and the seven flags, compute `repo_age_years = repo_age_days / 365.25`, and log the population
4. For each tool and metric: the adopter and non-adopter Series without NaN, `mannwhitneyu(..., alternative="two-sided")` when neither is empty, Cliff's delta and its magnitude, the medians and quartiles of both groups
5. `adjust_tested(p_raw)`: Benjamini-Hochberg over the p-values that are not NaN, which gives `p_adjusted` and `significant`, NaN and False for a comparison that was not tested
6. Write the CSV, and log one line per tool
7. Build the TeX. It starts with a baseline `All` row and its `($n$=N)` IQR row. Each tool has a median row with stars, an IQR row with `($n$=adopters)` and a delta row with `--` for a comparison that was not tested, and the significant cells are bold. The note names the test and the number of comparisons that were adjusted, and the meaning of `--` when a comparison was not tested
8. Write the TeX

### Output

`repo_metadata_comparison.csv`, <!--s:analysis.repo_metadata_comparison.comparisons-->28<!--/s--> rows in `TOOLS` then `METRICS` order, columns `tool,metric,n_with,n_without,median_with,q1_with,q3_with,median_without,q1_without,q3_without,U,p_raw,cliffs_delta,delta_magnitude,p_adjusted,significant`.

`repo_metadata_comparison.tex`, a `table` with a `tabular{lrrrr}` of the columns `Tool`, `Age (yr)`, `Contrib.`, `Commits` and `Size (KB)`. It has the `All` row, a row with the size of the joined population, three rows per tool separated by `\addlinespace`, and a `minipage` note.

`repo_metadata.csv`, which the helper writes.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` or a helper input absent | `FileNotFoundError`, and nothing is written |
| A tool with no adopter | NaN statistics and `not tested` as the magnitude for its four rows, `n/a` in its median and IQR cells, `--` in its delta cells, `($n$=0)`, NaN `p_adjusted` and not significant for its four rows. The other 24 comparisons are adjusted among themselves, and the note states 24 comparisons and the meaning of `--` |
| A tool with one adopter | `mannwhitneyu` runs, and the quartiles are that one value |
| A group with NaN metrics | Dropped per metric by `dropna()`, and `n_with` still counts the repository |
| Duplicate `repo_name` rows | Combined with `max` per flag |
| A configured repository that the snapshot does not list | Absent from the population |

## Files

| File | Action |
|------|--------|
| `repo_metadata_comparison.py` | Main script |
| `_repo_metadata.py` | Imported, and reads the two inputs below |
| `../../data/collection/output/repos.csv` | Read |
| `../../data/classification/repos_annotated.csv` | Read, by the helper |
| `../../data/sampling/ghs_filtered.json` | Read, by the helper |
| `repo_metadata_comparison.csv` | Written |
| `repo_metadata_comparison.tex` | Written |
| `repo_metadata.csv` | Written, by the helper |
| `../test_rq2_repo_metadata_comparison.py` | Tests |

## Usage

```bash
# After extracting repos_annotated.7z and ghs_filtered.json.7z
python3 repo_metadata_comparison.py

# Over the fixture
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 repo_metadata_comparison.py
```

## Tests

See `../test_rq2_repo_metadata_comparison.py`, a standard library `unittest` suite that needs pandas, numpy, scipy and statsmodels. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory and tests the helpers without input data. `main()` runs in-process over a twelve-row `repos.csv`, a twelve-row annotated table and a twelve-item snapshot under a temporary directory, with the module's `DATA_DIR` and `OUTPUT_DIR` and the three paths of `_repo_metadata` replaced, so that neither extracted archive is needed. One test class runs the script as a subprocess over the fixture with the real inputs and skips when those inputs are not extracted.

- **`TestCliffsDelta`** (6): Disjoint groups give -1 and 1, identical groups 0, and a partial overlap 0.5. Romano's thresholds apply to `|d|`, and NaN is `not tested`. An empty group gives NaN without a warning
- **`TestStars`** (1): the thresholds, and NaN gives none
- **`TestTheNumberFormat`** (7): The three magnitude formats, with halves rounded up. NaN, `np.nan`, `None` and `np.float64("nan")` give `n/a`. The three Series formats, and an empty Series gives `n/a` in every position
- **`TestMainOverASmallPopulation`** (12): A row per tool and metric, with the columns of `repo_metadata_comparison.csv`, and eight adopters and four non-adopters everywhere. Three medians, the age among them, and a quartile by hand. U, the exact two-sided p and delta of Claude's contributors by hand, and delta and magnitude agree with the helpers. The adjusted p is in `[0, 1]`, and every adjusted p matches Benjamini-Hochberg written out in the test. The non-adopters' quartiles of Claude's contributors by hand. The three files and the logged counts, the two baseline rows in full and three rows per tool. Duplicate rows are combined with OR, and nothing is written beside the script
- **`TestAToolThatSeparatesOnOneMetric`** (1): over 40 repositories, Codex's adopters are the 20 largest, with its three TeX rows in full, the size cells bold with three stars and the other three plain
- **`TestAToolWithNoAdopter`** (3): The run finishes, its CSV rows are NaN, and its TeX rows read `n/a`. The other tools keep their statistics, and their 24 adjusted p-values are Benjamini-Hochberg's over those 24 alone, while the empty group's are NaN and the note counts 24 comparisons. Its four comparisons are `not tested` with a NaN delta, its TeX delta row reads `--` four times, the note says what `--` means, the TeX contains no `nan`, and the run raises no `RuntimeWarning`
- **`TestOverTheFixture`** (1): A subprocess over `testdata/output/` exits 0 and writes the three files. The repositories with no specific tool against the others give the median and quartile lines stated from their GHS rows by hand, and no adjusted p is NaN

Run with:

```shell
python3 -m unittest test_rq2_repo_metadata_comparison -v
```

## Verification

1. Join the repository metadata with the current configured repositories yourself, and check the population that the script logs against that join.
2. Check that `n_with + n_without` equals the joined population and that each tool's `n_with` agrees with its flags in that population.
3. Check the statistics against the metric values that are not missing in each group. An empty or degenerate group must produce the `n/a` and NaN results that the table of edge cases above lists.
4. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/repo_metadata_comparison`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- The population depends on the snapshot: A configured repository absent from `ghs_filtered.json` is not compared
