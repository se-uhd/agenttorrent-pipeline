# Languages of the configured repositories and their tools

## Context

`language_tool_association.py` attaches the GHS main language to each row of `repos.csv` and writes four outputs: the language ranking of the configured repositories, the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> as a LaTeX list, a heatmap of the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages:words-->ten<!--/s--> languages against the six tool flags, and the per-language context file adoption rate. The script derives every ranking, share and adoption rate from the current release inputs.

## Key decisions

- **Language:** Python 3.14, in the single script `language_tool_association.py`, with `pandas`, `matplotlib` and `seaborn`
- The join is an inner join on `repo_name == name`. A configured repository that the snapshot does not list is therefore absent from every output, and the population `n` is the joined count. The join is one-to-one: A name listed twice in either input stops the script with `SystemExit`, because the repository would otherwise count twice
- The snapshot is `_shared.SAMPLING_DIR / "ghs_filtered.json"`, which `DATA_COLLECTION_OUTPUT` does not redirect. `check_reproduction.py` lists the snapshot as the input and skips the script where the snapshot is not extracted
- The script reads the snapshot through `_shared.checked_copy`. Where git tracks `ghs_filtered.json.7z` beside it, as in a checkout, a snapshot that differs from the archive stops the script, because the ranking would describe a sampling frame that the commit does not contain
- The seven tool flags are coerced at load and the mechanism flags where they are used: `load_data` coerces `TOOLS`, and `write_context_file_adoption_per_language` coerces `context_file` itself
- Shares in the heatmap are of the tool's repositories. For a tool with no repository the division is by zero and gives NaN cells, which seaborn draws blank
- The ranking counts repositories with a language. The top-<!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> shares are of the joined population, which is the same set because GHS records a language for every configured repository in the current sample
- The adoption rate is the fraction `repos_with_context_file / total_repos`, written as a fraction and logged as a percentage
- Each language in `top5_languages.tex` is written as LaTeX that prints it, through `_shared.latex_text`, because a name comes from the data and `#` is a parameter character in LaTeX. A bare `C#` breaks the snippet, and C# is among the languages that the filter keeps
- Ties are ranked by the language's name, in the ranking and in the adoption table. Without the name as a second key, both tables would keep the order in which the rows first named the languages. Which of two tied languages ranks first would then depend on the row order of `repos.csv`, and so would which of them enters the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> or the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages:words-->ten<!--/s-->. The current sample has no tie among its <!--s:analysis.language_tool_association.languages:words-->ten<!--/s--> languages, and the second key leaves the tracked outputs unchanged

## Script: `language_tool_association.py`

### CLI interface

No CLI. The script reads `repos.csv` from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and the snapshot from `_shared.SAMPLING_DIR`, and writes its outputs to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

`load_data` reads and joins the inputs. Each of the four writers takes the DataFrame, two of them also take the ranking, and `main` runs them in order.

### Module-level data

`OUTPUT_DIR`, and `DATA_DIR`, `SAMPLING_DIR`, `TOOLS`, `TOOL_LABELS` and `latex_text` from `_shared`.

### Function structure

**`load_data()`**: `repos.csv` inner-joined with the snapshot's `name` and `mainLanguage`, the seven tool columns as booleans

**`write_language_ranking(df)`**: the `rank,language,count` DataFrame, written to `language_ranking.csv` and returned

**`write_top5_latex(df, lang_counts)`**: `top5_languages.tex`, `\item Language (n=count, pct\%)`, the language through `latex_text`, for the first <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> rows, `pct` of `len(df)`

**`write_heatmap(df, lang_counts)`**: `language_tool_heatmap.pdf`, the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages:words-->ten<!--/s--> languages by the seven tools, each cell `count\n(pct%)` of the tool's total

**`write_context_file_adoption_per_language(df)`**: logs the per-language table and writes `context_file_language_adoption.csv`

**`main()`**: the loader and the four writers, in order

### Processing pipeline

1. Read `repos.csv`, parse `ghs_filtered.json`, join on the name, and coerce the tool flags
2. `value_counts` of `mainLanguage`, sorted by count descending and then by language, with a rank column, written to the CSV
3. The top <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> as an `enumerate`, written to the TeX
4. Filter to the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages:words-->ten<!--/s--> languages, sum the tool flags per language, divide by the per-tool totals, and draw with the counts and shares as labels, `Tool\n(n=total)` column labels and the title `Agentic AI Tool Usage by Programming Language (n = distinct repositories)`
5. Group by language (distinct repositories, repositories with `context_file` TRUE, the ratio), sort by total descending and then by language, log the table, and write the CSV

### Output

`language_ranking.csv`, columns `rank,language,count`, count descending.

`top5_languages.tex`, an `enumerate` of up to <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> `\item` lines.

`language_tool_heatmap.pdf`, a <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages-->10<!--/s-->x7 heatmap, columns in `TOOLS` order.

`context_file_language_adoption.csv`, columns `language,total_repos,repos_with_context_file,adoption_rate`, sorted by `total_repos` descending, with the rate as a fraction.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` or the snapshot absent | `FileNotFoundError`, and nothing is written |
| A configured repository that the snapshot does not list | Dropped by the join |
| A repository named twice in either input | `SystemExit` naming the duplicated key, and nothing is written |
| The snapshot differs from the archive that git tracks beside it | `SystemExit` naming both files and the command that extracts the archive again, and nothing is written |
| A tool with no repository | `RuntimeWarning` from the division and NaN cells labeled `0\n(nan%)`, and the figure is written |
| A repository without a language | In `len(df)`, and in no ranking, heatmap or adoption row |
| Fewer than <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages:words-->ten<!--/s--> languages | A shorter heatmap |
| A flag spelled `true` | Read as TRUE |

## Files

| File | Action |
|------|--------|
| `language_tool_association.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `../../data/sampling/ghs_filtered.json` | Read, extracted from `ghs_filtered.json.7z` |
| `language_ranking.csv` | Write |
| `top5_languages.tex` | Write |
| `language_tool_heatmap.pdf` | Write |
| `context_file_language_adoption.csv` | Write |
| `../test_rq2_language_tool_association.py` | Tests |

## Usage

```bash
# After extracting ghs_filtered.json.7z
python3 language_tool_association.py
```

## Tests

See `../test_rq2_language_tool_association.py`, which uses the standard library's `unittest` over pandas, matplotlib and seaborn, under `MPLBACKEND=Agg`. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. Every test case points `DATA_DIR`, `SAMPLING_DIR` and `OUTPUT_DIR` at a nine-row `repos.csv` and a nine-item snapshot written under a temporary directory, with eight repositories in both, one in the CSV alone and one in the snapshot alone. The suite reads the heatmap's matrices off a wrapped `seaborn.heatmap`.

- **`TestLoadData`** (5): The join is inner both ways, and a repository named twice in the sampling frame stops the script. The language is attached. `TRUE` and `true` both read as True, and the tool columns are boolean. In a git repository in the temporary directory with an archive of another sampling frame added to its index, the snapshot stops the script, and the snapshot extracted again is read. That case needs `7zz` and git and skips without them
- **`TestTheRanking`** (3): columns, order and ranks in the DataFrame and the file, with Go and Rust tied at one and ranked by name, and the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages:words-->five<!--/s--> as shares of the eight joined repositories. C# ranks first and is written as `C\#`
- **`TestTheHeatmap`** (5): The axes and four cells with their `count\n(pct%)` labels, two of them in the No specific tool column. The column labels, a name and a count each, are turned by 45 degrees, so that they do not overlap. The PDF embeds TrueType fonts and no Type 3 font. The column labels with each tool's `n`, in order, and the title's `n`. A tool with no repository gives NaN cells, and the PDF is still written
- **`TestContextFileAdoption`** (2): Twenty languages, most of them tied, in the order of their totals and then their names. The four columns, the totals, the counts and the rates per language, the tied languages by name, and the overall line logged
- **`TestMain`** (2): the four files, and nothing written beside the script

Run with:

```shell
python3 -m unittest test_rq2_language_tool_association -v
```

## Verification

1. Join the current configured repositories with `ghs_filtered.json` independently of the script, count the non-missing language values, and compare the counts with `language_ranking.csv`.
2. Check the top-language percentages against the joined population and the heatmap counts against the tool flags. No particular language has to rank first.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/language_tool_association`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- For a tool with no repository, the heatmap draws NaN cells blank and labels them `nan%`. This case does not occur over the current sample, because every tool has repositories
- The population of the ranking and the population of `language_ranking_all.py` differ, and neither output file notes the difference
