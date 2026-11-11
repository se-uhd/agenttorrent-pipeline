# Filter steps table

## Context

`filter_steps_table.py` writes the paper's table of the repository filtering as LaTeX. It reads the `_metadata` block that `1_sampling/4_filter_ghs.py` records in the sampling frame and writes `filter_steps_table.tex`: the retrieval's repository count, one row per filter step with what the step removed and what remained, and the size of the sampling frame. `run_analysis.py` runs it as the `dataset_filter_steps_table` step of the analysis stage, after the sampling stage has written the sampling frame.

## Key decisions

- **Language:** Python 3.14, in the single script `filter_steps_table.py`, with the standard library, `agentcontext.logs`, and `_shared` for the paths and `latex_text`
- The table belongs to the analysis stage, because it exists for the paper, as the other LaTeX tables and figures of this stage do. The sampling stage ends with the sampling frame
- Every label is built from the step's key and its recorded values, or from the description of a step that has no key. The filter records a `key` for each step and the `values` that the step applied, in the list `_metadata.stages`. Reading the thresholds out of a description with a regular expression would fail on a description in another form. A key that the script does not know is its own label, escaped as a description is
- The activity row is labeled `Commits $<N$ $\lor$ Watchers $<M$` from `commits_min` and `watchers_min`, and a quantile between two integers is printed as it is
- The language row is labeled `Non-Top-N Languages` from `top`, followed by `/ L Left Out by End Date` when `values.left_out` names languages, where L lists them by name with LaTeX's special characters escaped (`languages_label`). The filter's step `languages` ranks only the languages that the end date of the sampling window covers. It records under `left_out` each language that it passed over and would otherwise have kept (`1_sampling/filter-ghs-spec.md`). A label of the number alone would state that the step kept these languages. A step with `filter_languages: false` records no `top`, and it is labeled `L Left Out by End Date` alone when it left out a language
- The gone row is labeled `Gone from GitHub`, followed by `/ N Unresolved` when `values.unresolved` is above 0, by `/ M Without ID` when `values.without_id` is above 0, and by `/ K Created After Window` when `values.created_after_window` is above 0 (`GONE_PARTS`). Besides the repositories that GitHub does not have, the step removes the rows that the GitHub query left unresolved, which no response shows to be gone from GitHub. It also removes the repositories that GitHub found without an id and the repositories that GitHub created after the window under a name that GHS crawled for an earlier repository, and GitHub has both. A label of `Gone from GitHub` alone would therefore state that these rows are gone
- The row of the step `mirrors`, which the filter of the sampling frame of 2026-10-23 still had, is labeled `Later Copies at a Shared Head Commit`. The step kept, of the repositories at one head commit, the one that GitHub created first, and it removed sqlalchemy/sqlalchemy beside the earlier zzzeek/sqlalchemy. A mirror is a classification criterion and a flag of GitHub, and a repository that GitHub marks as a mirror stays in the sampling frame (`GLOSSARY.md`), so the label does not call these repositories mirrors
- The earlier filter wrote the sampling frame of 2026-03-17, that of release 1.0.0, and its steps have a description and no key. No run can write that sampling frame again, and the script therefore names such a step by its description. It escapes LaTeX's special characters through `_shared.latex_text`, as in the language step, which names C#
- A step that removed nothing is written as a LaTeX comment without a number, so that the numbered rows stay consecutive
- **Arguments:** the sampling frame's path, `data/sampling/ghs_filtered.json` by default, and the table's path, `filter_steps_table.tex` in `OUTPUT_DIR` by default, both optional
- A sampling frame without filter steps is an error: The script exits 1 and writes nothing
- The script reads the sampling frame through `_shared.checked_copy`, with the `.7z` archive of its name beside it. Where git tracks that archive, as `ghs_filtered.json.7z` in a checkout, a sampling frame that differs from it stops the script with the command that extracts the archive again, because the table would describe a sampling frame that the commit does not contain. A sampling frame named on the command line without such an archive is read as it is

## Script: `filter_steps_table.py`

### CLI interface

```
python3 dataset/filter_steps_table.py [SAMPLING_FRAME [TABLE]]
```

No flags. `ANALYSIS_OUTPUT_DIR` reaches the default output through `_shared`.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or `$ANALYSIS_OUTPUT_DIR/dataset/`

**`DEFAULT_INPUT`**: `SAMPLING_DIR / "ghs_filtered.json"`

**`DEFAULT_OUTPUT`**: `OUTPUT_DIR / "filter_steps_table.tex"`

**`GONE_PARTS`**: the values of the gone step that its label names beside `Gone from GitHub`, each with its label: `unresolved`, `without_id` and `created_after_window`

### Function structure

**`format_latex_number(n)`**: `187304` to `$187{,}304$`

**`languages_label(values)`**: the label of the language step, with the languages that the end date left out

**`step_label(step)`**: the label of one filter step from its key and values

**`render(meta)`**: the table's lines

**`main(argv)`**: reads the sampling frame, writes the table and logs its path, and returns the exit code

### Processing pipeline

1. Read `_metadata.input_source.item_count`, `_metadata.stages` and `_metadata.final_count`
2. Write the header, the initial row, one row per filter step, numbered unless the step removed nothing, the final row in bold, the caption and the label `tab:filtering-table`

### Output

A `table` environment with a three-column `tabular`, `\toprule`, `\midrule` between rows, a double `\midrule` before the final row, `\bottomrule`, the caption and the label, in `filter_steps_table.tex`. The script logs `Wrote <path> (<n> lines)`.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| The sampling frame is missing, not JSON, or has no filter steps | `ERROR dataset_filter_steps_table: Could not read the filter steps of ...`, exit 1, nothing written |
| The sampling frame differs from the archive that git tracks beside it | `SystemExit` naming both files and the command that extracts the archive again, exit 1, nothing written |
| A step removed nothing | Its row is a LaTeX comment |
| An activity step with one threshold | The label names that threshold |
| A gone step with unresolved rows | The label names their number beside `Gone from GitHub` |
| A gone step with repositories without an id | The label names their number beside `Gone from GitHub`, after the unresolved rows |
| A gone step with repositories that GitHub created after the window | The label names their number beside `Gone from GitHub`, after the repositories without an id, because GitHub has them under a name that GHS crawled for an earlier repository |
| A language step that left out languages that the end date of the sampling window does not cover | The label names them beside `Non-Top-N Languages`, or alone for a step that keeps every language |

## Files

| File | Action |
|------|--------|
| `filter_steps_table.py` | Main script |
| `../../data/sampling/ghs_filtered.json` | Read |
| `filter_steps_table.tex` | Written, tracked |
| `../test_dataset_filter_steps_table.py` | Tests |

## Usage

```bash
python3 dataset/filter_steps_table.py
python3 dataset/filter_steps_table.py ../data/sampling/ghs_filtered.json filter_steps_table.tex
```

## Tests

See `../test_dataset_filter_steps_table.py`, a standard library `unittest` suite. Every test case writes a metadata block in the format that `4_filter_ghs.py` saves and reads the table back from the file.

- **`TestFormatLatexNumber`** (1): thousands get the LaTeX separator
- **`TestTheTable`** (11): Every filter step is a numbered line, and a step that removed nothing is a comment, with the labels built from the keys and values. The step `mirrors` names the later copies at a shared head commit and no mirror. The gone step names the rows that the GitHub query left unresolved, and the repositories that GitHub found without an id, alone and after the unresolved rows. The language step names the languages that the end date left out, one, two or three of them, escaped, and alone for a step that keeps every language. An absolute threshold and a single threshold are labeled. A step of the March sampling frame is named by its description, written as LaTeX that prints it. A sampling frame without filter steps is an error and writes nothing. A sampling frame beside a tracked archive of another sampling frame stops the script and writes nothing, and the sampling frame extracted again is rendered. That case needs `7zz` and git and skips without them. Without an output argument the table is written under `ANALYSIS_OUTPUT_DIR` and nowhere else

Run with:

```shell
python3 -m unittest test_dataset_filter_steps_table -v
```

## Verification

1. After the sampling stage has run, `python3 dataset/filter_steps_table.py` writes a table in which the numbered rows and the comments together are the filter steps of the sampling frame and the final count is `_metadata.final_count`. The table has eight filter steps under the current filter and nine for the tracked sampling frame of 2026-10-23, because the filter of that sampling frame still had the step `mirrors`

## Known limitations

- The table of the sampling frame of 2026-03-17 labels its steps with that sampling frame's own descriptions, in the earlier filter's words, and has the earlier filter's seven steps rather than the eight steps of the current filter
