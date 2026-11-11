# Language ranking over the sample

## Context

`language_ranking_all.py` ranks the main languages of the repositories in the sample. `_repo_metadata.build_repo_metadata()` returns the classification stage's full table joined with the GHS snapshot. The script keeps the rows that `_repo_metadata.sample_names()` lists, the repositories labeled `engineered_project=true`. It writes the ranking as CSV and the top <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:words-->five<!--/s--> as a LaTeX `enumerate`. Its population is the comparison population for `language_tool_association.py`, which ranks the configured repositories, a part of the sample.

## Key decisions

- **Language:** Python 3.14, in the single script `language_ranking_all.py`, with `pandas`
- The population is the sample. The helper returns every annotated row that the snapshot lists, whatever its label, and writes `repo_metadata.csv` into `data/analysis/rq2/` as a side effect of the call. The runner declares that file as this step's output too. The script keeps the rows that `sample_names` names, a list that the engineered project selection's own function builds. A ranking of every annotated row, including the repositories labeled `false` or `unsure`, would not match the configured repositories that it is compared with, which come from the sample alone. Over the March classification, the restriction to the sample puts C++ in the top five in place of JavaScript. The log message `INFO rq2_language_ranking_all: <n> of the <m> annotated repositories in the sampling frame are in the sample.` gives the number of repositories that the restriction kept
- The shares are of every repository in the sample, including the repositories without a language. `total = len(df)`, while `value_counts` drops the empty language, and the percentages of the top five therefore do not sum to the share of the ranked rows
- Ties are ranked by the language's name. Under pandas 3, `value_counts` alone keeps tied languages in the order in which the rows first named them, and the ranks of a tie would then depend on the row order of the classification table. The ranking is by count descending, then by name. The tracked ranking has no tie among its <!--s:analysis.language_ranking_all.languages:words-->ten<!--/s--> languages, and the second key leaves the tracked outputs unchanged
- <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:Words-->Five<!--/s--> items or fewer in the LaTeX list, from `head(5)` over the ranking. A population with three languages gives three items
- Each language is written as LaTeX that prints it, through `_shared.latex_text`, because a name comes from the data and `#` is a parameter character in LaTeX. A bare `C#` breaks the snippet, and C# is among the languages that the filter keeps

## Script: `language_ranking_all.py`

### CLI interface

No CLI. The script reads the helper's inputs, `data/classification/repos_annotated.csv` and `data/sampling/ghs_filtered.json`, and writes its outputs to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

One `main()` over `build_repo_metadata` and `sample_names`, imported into the module's namespace.

### Module-level data

`OUTPUT_DIR`, `build_repo_metadata` and `sample_names` from `_repo_metadata`, and `latex_text` from `_shared`.

### Function structure

**`main()`**: calls the helper, keeps the sample, ranks `main_language`, writes `language_ranking_all.csv` and `top5_languages_all.tex`, and logs the language count

### Processing pipeline

1. Call `metadata = build_repo_metadata()`, which also writes `repo_metadata.csv`, keep its rows that `sample_names()` names as `df`, and log both counts
2. `df["main_language"].value_counts()` as a DataFrame with columns `language`, `count`, sorted by count descending and then by language, and a `rank` column `1..n` in front
3. Write the CSV with `index=False`, and log `Wrote language_ranking_all.csv (n languages)`
4. For the first <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:words-->five<!--/s--> rows: `\item Language (n=count, pct\%)` with `pct = count / len(df) * 100` to <!--s:parameters.4_data_analysis.rq2.language_ranking_all.share_decimals:words-->one<!--/s--> decimal
5. Write the `enumerate`, and log `Wrote top5_languages_all.tex`

### Output

`language_ranking_all.csv`, columns `rank,language,count`, one row per distinct language, count descending, ranks `1..n`.

`top5_languages_all.tex`:

```
\begin{enumerate}
  \item Python (n=8288, 22.6\%)
  ...
\end{enumerate}
```

`repo_metadata.csv`, written by the helper.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| Either helper input absent | `FileNotFoundError` from the helper, and nothing is written |
| A repository without a language | In `total`, in no row of the ranking |
| A repository labeled `false` or `unsure` | In `repo_metadata.csv`, in neither `total` nor the ranking |
| Fewer than <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:words-->five<!--/s--> languages | Fewer items |
| An empty population | A CSV of the header alone and an empty `enumerate`, and no division by zero, because the loop over the top <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:words-->five<!--/s--> is empty |

## Files

| File | Action |
|------|--------|
| `language_ranking_all.py` | Main script |
| `_repo_metadata.py` | Imported, and reads the two inputs below |
| `../../data/classification/repos_annotated.csv` | Read, by the helper |
| `../../data/sampling/ghs_filtered.json` | Read, by the helper |
| `language_ranking_all.csv` | Write |
| `top5_languages_all.tex` | Write |
| `repo_metadata.csv` | Write, by the helper |
| `../test_rq2_language_ranking_all.py` | Tests |

## Usage

```bash
# After extracting repos_annotated.7z and ghs_filtered.json.7z
python3 language_ranking_all.py
```

## Tests

See `../test_rq2_language_ranking_all.py`, which uses the standard library's `unittest` over pandas. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. The ranking test cases replace `build_repo_metadata` and `sample_names` in the script's namespace with functions that return a thirteen-row DataFrame, in which one row has no language, and the names of its repositories. The last test class runs the real helper over a four-row annotated table and a four-item snapshot under a temporary directory, with `_repo_metadata`'s three paths replaced. No test case reads an extracted archive.

- **`TestTheRanking`** (4): The columns, the descending order, the three languages tied at one ranked by name, and the ranks. A repository without a language is no row, and the language count is logged. A repository outside the sample is neither ranked nor in the shares, and the counts are logged
- **`TestTheTopFive`** (4): The LaTeX list has <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:words-->five<!--/s--> items with shares of all thirteen repositories, and three languages give three items. C# ranks first and is written as `C\#`, and C++ as it is. Nothing is written beside the script
- **`TestThroughTheHelper`** (1): the two outputs and `repo_metadata.csv` are written, the helper's table keeps the row labeled `false`, and the ranking leaves it out

Run with:

```shell
python3 -m unittest test_rq2_language_ranking_all -v
```

## Verification

1. Check the repository metadata against the join of the current classification and sampling inputs, and the ranked population against the rows of `repos_annotated_included.csv` that the snapshot lists.
2. Check that the language counts sum to the metadata rows with a nonempty language and that the ranking follows the count and the tie order that [Key decisions](#key-decisions) states.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/language_ranking_all`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- The ranking covers the sample joined to GHS. `language_tool_association.py` covers the configured repositories, a part of the sample, and the two rankings therefore compare a population with a part of it
- `repo_metadata.csv` is written twice when this script and `repo_metadata_comparison.py` both run
