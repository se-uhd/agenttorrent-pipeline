# Pairwise association of the configuration mechanisms

## Context

`config_cooccurrence.py` tests each of the <!--s:analysis.config_cooccurrence.pairs-->45<!--/s--> pairs of the ten configuration mechanisms in `repos.csv` for association, over the repositories with at least one mechanism. A pair's 2x2 table counts the repositories with both, one or neither mechanism. The script tests it with a chi-squared test without continuity correction, or with Fisher's exact test where a cell has fewer than <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.MIN_CELL_COUNT-->5<!--/s--> observed or expected repositories. It adjusts the p-values of the tested pairs with Benjamini-Hochberg. The phi coefficient gives the direction of an association, Cramér's V its strength, and Cohen's thresholds the magnitude of V. The script writes the per-pair table as CSV and, for the dataset release, a symmetric 10x10 LaTeX matrix with phi above the diagonal, the adjusted p below it, and the number of repositories with each mechanism on it.

## Key decisions

- **Language:** Python 3.14, in the single script `config_cooccurrence.py`, with `pandas`, `numpy`, `scipy` (`chi2_contingency` and `fisher_exact`), and `statsmodels` through `_shared.adjust_tested`
- The population is the repositories with at least one mechanism, because the analysis studies which mechanisms a repository combines once it configures any. Over the sample, tens of thousands of repositories without any mechanism would enter every `neither` cell and make every pair look associated. The script drops every row of `repos.csv` with every mechanism flag FALSE, such as a repository with only an `other` configuration artifact, before it builds the 2x2 tables. `N` in the table note is the count after that filter, and the note and the caption name its population, `repositories with at least one mechanism`. [Known limitations](#known-limitations) describes how the restriction affects the signs
- Duplicate rows of a repository are combined with a logical OR on `repo_name` before the filter, so that a repository is one observation however many rows name it
- The mechanisms are `_shared.MECHANISMS`, in that order, which is the row and column order of the matrix. `other` is not among them
- The chi-squared test runs without continuity correction (`correction=False`). For a 2x2 table, `chi2 / N` is then Cramér's V squared
- A pair with a mechanism that no repository or every repository has is not tested. Its table has an empty row or column, for which the test is undefined and `chi2_contingency` raises. The pair keeps its four counts, has no statistic, has `not tested` as its magnitude and `--` in both of its cells, and is left out of the adjustment. The note gives the number of pairs that the adjustment covers
- The p-value comes from the chi-squared test only where every cell has at least `MIN_CELL_COUNT` observed and expected repositories, and from Fisher's exact test otherwise. The documentation of the `scipy` that `requirements.txt` pins, 1.18.1, states the guideline in the Notes of `chi2_contingency`: "An often quoted guideline for the validity of this calculation is that the test should be used only if the observed and expected frequencies in each cell are at least 5." The See Also of `fisher_exact` names the chi-squared test as the alternative "when the numbers in the table are large", and its References give Fisher's *The Design of Experiments* (1935). Fisher's test is exact at any count, and a pair that the guideline excludes from the chi-squared test therefore loses no validity. Over the current collection, the nine pairs with LSP, which <!--s:collection.flags.lsp:words-->sixteen<!--/s--> repositories configure, have a cell below the guideline, and Commands with LSP would be significant under the chi-squared p but is not under Fisher's p. The CSV names the test of each pair in `test`, and the TeX marks a Fisher p with a dagger. The script computes `chi2` and V from the table of every tested pair, because they describe the table whichever test gives the p
- The upper triangle is the phi coefficient with its sign. Cramér's V is the absolute value of phi for a 2x2 table and has no direction. The Notes of `scipy`'s `association` say of V that it measures "how related the variables are", while correlation measures "in what way two variables are related". In a table titled co-occurrence, the V of a pair that occurs together less often than independence predicts reads as co-occurrence. Context Files with Skills, with Settings and with Rules are such pairs. Phi is the correlation of the two flags, negative for such a pair, and the CSV has it beside `cramers_v`. The magnitude stays Cohen's on `|phi|`, which is V
- Bold marks a medium effect, `|phi| >= 0.3`, which is `V >= 0.3`. Stars mark the adjusted p at <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.stars.one_star_below-->0.05<!--/s-->, <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.stars.two_stars_below-->0.01<!--/s--> and <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.stars.three_stars_below-->0.001<!--/s-->. A p below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.p_format.lt_below-->0.001<!--/s--> is written `$<$.001`, a p below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.p_format.three_decimals_below-->0.01<!--/s--> with <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.p_format.small_decimals:words-->three<!--/s--> decimals, and any other p with <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.p_format.decimals:words-->two<!--/s-->

## Script: `config_cooccurrence.py`

### CLI interface

No CLI. The script reads the CSV from `_shared.DATA_DIR`, which `DATA_COLLECTION_OUTPUT` overrides, and writes its outputs to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

`main()` calls eight pure helpers: `stars`, `cramers_v`, `v_magnitude`, `phi_coefficient`, `association_cell` and `p_cell` take numbers, and `testable` and `which_test` take a table. `main` reads the CSV, loops over `itertools.combinations(MECHANISMS, 2)`, and writes the CSV and the TeX.

### Module-level data

`OUTPUT_DIR`. `MIN_CELL_COUNT` (<!--s:parameters.4_data_analysis.rq2.config_cooccurrence.MIN_CELL_COUNT-->5<!--/s-->) is the smallest observed and expected count of a cell at which the chi-squared test is used. `CHI_SQUARED` and `FISHER` are the names of the two tests in the `test` column. `MECHANISMS`, `MECHANISM_LABELS`, `DATA_DIR`, `adjust_tested` and `latex_text`, through which the TeX writes each label, are imported from `_shared`.

### Function structure

**`stars(p)`**: `***` below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.stars.three_stars_below-->0.001<!--/s-->, `**` below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.stars.two_stars_below-->0.01<!--/s-->, `*` below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.stars.one_star_below-->0.05<!--/s-->, else empty

**`cramers_v(chi2, n, k, r)`**: `sqrt(chi2 / (n * min(k - 1, r - 1)))`

**`v_magnitude(v)`**: `negligible` below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.v_magnitude.negligible_below-->0.1<!--/s-->, `small` below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.v_magnitude.small_below-->0.3<!--/s-->, `medium` below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.v_magnitude.medium_below-->0.5<!--/s-->, else `large`

**`testable(table)`**: whether every row and every column of the table has a repository

**`which_test(table, expected)`**: `FISHER` when a cell has fewer than `MIN_CELL_COUNT` observed or expected repositories, else `CHI_SQUARED`

**`phi_coefficient(both, only_a, only_b, neither)`**: `(both * neither - only_a * only_b) / sqrt(r1 r2 c1 c2)`, positive when the two mechanisms occur together more often than independence predicts

**`association_cell(phi)`**: phi to two decimals, `$-$` before a negative phi that does not round to zero, bold where `|phi|` is at least <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.v_magnitude.small_below-->0.3<!--/s-->

**`p_cell(p, test)`**: the adjusted p in the format of [Key decisions](#key-decisions), with its stars and a `\dagger` for `FISHER` in one superscript

**`main()`**: reads, coerces, combines duplicate rows, filters, tests the <!--s:analysis.config_cooccurrence.pairs-->45<!--/s--> pairs, adjusts, writes both files, and logs the significant pairs

### Processing pipeline

1. Read `repos.csv` and coerce the ten mechanism columns with `str(v).upper() == "TRUE"`
2. If a `repo_name` repeats, group by it and take the maximum of each flag
3. Keep the rows with at least one TRUE and log their count
4. For each pair `(a, b)`, count the repositories with both, only a, only b and neither. When the table is testable, run `chi2_contingency` on `[[both, only_a], [only_b, neither]]` without correction, let `which_test` choose the test from the table and its expected counts, and for `FISHER` take the two-sided `fisher_exact` p in place of the chi-squared p. Compute phi from the four counts and V from the statistic and `N`. An untestable table gets no statistic
5. Adjust with `adjust_tested(p_raw)`, Benjamini-Hochberg over the tested rows, and keep the adjusted p and the reject flag. An untested row gets no adjusted p and is not significant
6. Write `config_cooccurrence.csv`, then log the significant pairs with phi and adjusted p, and the number of pairs tested with Fisher's test
7. Build the matrix. The diagonal contains `($n$\,=\,count)`, the upper triangle phi with its sign to <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.v_decimals:words-->two<!--/s--> decimals (bold at `|phi|` <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.v_magnitude.small_below-->0.3<!--/s-->), and the lower triangle the adjusted p with stars and a dagger for Fisher's test, with `--` for an untested pair. The caption names the population and states that a negative phi marks a pair that occurs together less often than independence predicts. The note names both tests and when Fisher's test applies, the number of comparisons, `N` and its population, and what `--` means
8. Write `config_cooccurrence.tex`

### Output

`config_cooccurrence.csv`, <!--s:analysis.config_cooccurrence.pairs-->45<!--/s--> rows, columns `option_a,option_b,both,only_a,only_b,neither,chi2,test,p_raw,phi,cramers_v,v_magnitude,p_adjusted,significant`. In every row the four counts sum to `N`. `test` is `chi-squared` or `Fisher`, and for a pair that was not tested it is blank, as are the statistics.

`config_cooccurrence.tex` contains the matrix that step 7 builds, with its caption and note. A p from Fisher's test has a dagger, and `--` marks a pair that was not tested because one of its mechanisms is absent from every repository or present in every repository.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` absent | `FileNotFoundError` from pandas |
| A mechanism TRUE in every kept row, or in none | Its pairs are not tested: blank statistics, `not tested`, `--` in the TeX, and left out of the adjustment |
| No row has a mechanism | No pair is tested, and every row of the CSV is blank past its four counts |
| Duplicate `repo_name` rows | Combined with `max` per flag |
| A flag spelled `true` or `True` | Read as TRUE, and any other value, an empty cell included, as FALSE |
| Last digits of `chi2` and `p_raw` | Differ between macOS and the Linux runner, and CI therefore checks reproduction alone (see `../0_README.md`) |

## Files

| File | Action |
|------|--------|
| `config_cooccurrence.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `config_cooccurrence.csv` | Write |
| `config_cooccurrence.tex` | Write |
| `../test_rq2_config_cooccurrence.py` | Tests |

## Usage

```bash
python3 config_cooccurrence.py

# Over the fixture, writing under /tmp/figures/rq2/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 config_cooccurrence.py
```

## Tests

See `../test_rq2_config_cooccurrence.py`, which uses the standard library's `unittest` over pandas, scipy and statsmodels. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. The in-process test cases write a ten-row `repos.csv` in the columns of the fixture's `repos.csv` and point `DATA_DIR` and `OUTPUT_DIR` at temporary directories. In that file, every mechanism but LSP is TRUE in some rows and FALSE in others, and LSP is TRUE in none. One test class runs the script as a subprocess over `testdata/output/`.

- **`TestStars`** (1), **`TestCramersV`** (4): The thresholds, and `cramers_v(4.0, 100, 2, 2)` is 0.2. The smaller dimension sets the denominator, and the magnitudes follow Cohen's boundaries
- **`TestTheTableHelpers`** (5): A table with an empty row or an empty column is not testable. The chi-squared test needs five observed and five expected repositories in every cell, so a table with all observed counts of at least five but one expected count below five goes to Fisher's test, as does a table with expected counts of five and observed zeros. Phi has the sign. An upper cell is bold from `|phi|` of 0.3, negative or not, and a value that rounds to zero has no sign. A lower cell has its stars and a dagger for Fisher's test
- **`TestMainOverASmallReposCsv`** (13): A row per pair of mechanisms, with the columns of `config_cooccurrence.csv`. The nine pairs with LSP have their counts and no statistic or test, `not tested` and `--` in the TeX, and the adjustment runs over the other 36. A mechanism that every repository has is not tested either. The four cells sum to the population, and one pair's cells are counted by hand. V is the root of `chi2 / N`, and phi is the signed phi of the table counted in the test, with V its absolute value. chi2, the Fisher p of 1 and V of the context_file/skills table are stated by hand, every pair of the ten repositories is tested by Fisher's test, and every raw and adjusted p is checked against `fisher_exact` on the counted table and Benjamini-Hochberg written out in the test. The adjusted p is in `[0, 1]` and never below the raw p. A repository with no mechanism is left out of `N` and of V, and the note names the population. Duplicate rows are combined with OR. The TeX has a row per mechanism with its count on the diagonal, and four exact cells above and below the diagonal, one of them bold and one negative. Nothing is written beside the script
- **`TestAStrongAssociation`** (1): the ten rows with fifty added so that Skills and Subagents associate strongly: an exact bold cell, a bold negative cell, a cell below the bold threshold, the Fisher p-values with three stars and a dagger, and three adjusted p-values computed independently of the suite
- **`TestTheChiSquaredPath`** (2): thirty repositories over which one pair has every cell at five or more: its chi2, p, phi and adjusted p by hand under the chi-squared test, and the two pairs with an empty cell under Fisher's test with their p from the hypergeometric probabilities by hand. The cells of the TeX, one of them bold and negative, the note naming when Fisher's test applies, and the count of the Fisher pairs in the log
- **`TestOverTheFixture`** (3): Exit 0, a row per pair of mechanisms, and every pair without LSP tested, because LSP is FALSE throughout the fixture. The `context_file`/`settings` cells, the `N` that the note names and the `Commands` diagonal are the counts of the fixture's `repos.csv`

Run with:

```shell
python3 -m unittest test_rq2_config_cooccurrence -v
```

## Verification

1. Derive the analysis population from the rows of the current `repos.csv` with at least one named mechanism.
2. Check that `both + only_a + only_b + neither` equals that population for every mechanism pair.
3. Check the pair counts and the diagonal counts against flags counted independently of the script. A pair with a mechanism that every repository or no repository has must have the blank statistics that [Key decisions](#key-decisions) describes.
4. Recompute each tested pair's p with the standard library: Fisher's two-sided p from the hypergeometric probabilities where a cell has fewer than five observed or expected repositories, and the chi-squared tail `erfc(sqrt(chi2 / 2))` elsewhere. Apply Benjamini-Hochberg by hand and compute phi from the four counts. Over the October 5-6 collection, the CSV agreed with the recomputation to a relative difference of 1.1e-13.
5. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/config_cooccurrence`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- A mechanism that no repository has gets a row and a column of `--` rather than being dropped from the matrix
- The restriction of the population biases phi downward. Every repository in it has a mechanism. A repository without a context file therefore has some other mechanism, and a pair of mechanisms looks less associated than over a population that the selection did not depend on. A negative phi between Context Files and another mechanism means that the configured repositories rarely combine those two mechanisms, not that the mechanisms exclude each other in the sample
- The analysis script generates this table, and the website does not rebuild it
