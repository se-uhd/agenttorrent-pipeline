# Configuration mechanisms per tool, as a heatmap

## Context

`config_tool_heatmap.py` draws the ten configuration mechanisms against the six tools and the repositories with no specific tool. Each cell contains the number of repositories with a configuration artifact of the mechanism attributed to the tool, and that number as a share of the tool's repositories. `_website/build_site.py` rebuilds the same matrix from `release/` for the explorer through the same function, `_shared.mechanism_tool_counts`.

A cell counts the repositories by the tool of their configuration artifacts, which the detection step writes into the mechanism CSVs. Combining the repository flags, `mechanism & tool`, would count a repository with a Claude context file and a Copilot prompt in the cell of Claude and Commands. In the collection, <!--s:collection.claude_and_commands_flags_repositories-->155<!--/s--> repositories without a Claude command would be counted in that cell, because Claude Code's commands count under skills.

## Key decisions

- **Language:** Python 3.14, in the single script `config_tool_heatmap.py`, with `pandas`, `matplotlib` and `seaborn`. `_shared.save_figure` writes the figure with `pdf.fonttype` and `ps.fonttype` set to <!--s:parameters.4_data_analysis.shared.save_figure.pdf_fonttype-->42<!--/s-->, so that the PDF embeds TrueType fonts
- A cell counts the files attributed to the tool. `(mechanism, tool)` is the number of repositories with a row in the mechanism's CSV that names the tool in its `tool` column. A file that several tools read through their own mechanisms, written `claude|cursor`, counts for each, and a shared file, `any`, counts for no tool. A repository's tool flags are the union of its rows' tools. In a tool's column, a repository counts only under the mechanisms of its files for that tool
- The columns are `_shared.SPECIFIC_TOOLS` and then `no_specific_tool`. The last column counts, per mechanism, the repositories with the `no_specific_tool` flag that have the mechanism, and every file of such a repository is shared. A repository with a tool's own files and shared files counts under the tool, for the mechanisms of the tool's files only
- The share is a share of the tool's repositories, `count / tool_total * 100`, where the total is the number of repositories with the tool's flag. A column therefore reads as "of the repositories that configure this tool, how many have a file of each mechanism for it". A tool with no repository gets 0 rather than a division by zero
- The color shows the share, which makes the scale comparable across tools of different size, and the label shows both, `count` over `(share%)`
- Zero cells are drawn in light gray over the colormap, with the label redrawn in black, so that a `0 (0.0%)` stays readable on the pale end of YlOrRd
- The title's `n` is the row count of `repos.csv`

## Script: `config_tool_heatmap.py`

### CLI interface

No CLI. The script reads `repos.csv` and the ten mechanism CSVs that `_shared.MECHANISM_CSV_NAMES` names from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`), and writes the figure to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

One `main()` that reads the output CSVs, takes the cells and the totals from `_shared.mechanism_tool_counts`, builds the DataFrames of the counts, the shares and the annotation strings inline, and passes the shares and the annotations to `seaborn.heatmap`.

### Module-level data

`MECHANISM_LABELS` lists the `_shared.MECHANISM_LABELS` names in `MECHANISMS` order, and `TOOLS` lists `SPECIFIC_TOOLS` and then `NO_SPECIFIC_TOOL`. `OUTPUT_DIR` is the output directory, and `MECHANISMS`, `MECHANISM_CSV_NAMES`, `DATA_DIR` and `TOOL_LABELS` come from `_shared`.

### Function structure

**`main()`**: reads `repos.csv` and the `repo_name` and `tool` columns of the ten mechanism CSVs, coerces the seven tool and ten mechanism columns, builds the count, share and annotation matrices, draws the heatmap, grays the zero cells, saves `config_tool_heatmap.pdf`

**`_shared.mechanism_tool_counts(repos, dataframes)`**: `(cells, totals)`, the cells keyed by `(mechanism, tool)` over the seven columns and the totals by tool

### Processing pipeline

1. Read `repos.csv`, coerce `TOOLS + MECHANISMS` with `str(v).upper() == "TRUE"`, and read each mechanism CSV's `repo_name` and `tool` as strings
2. `counts[mechanism, tool]` = the repositories of `repos.csv` with a row of the mechanism that names the tool in its `tool` column, split on `|`. `counts[mechanism, no_specific_tool]` = the repositories with that flag and a row of the mechanism
3. `tool_totals[tool] = df[tool].sum()`, for the seven columns
4. `pct[mechanism, tool] = counts / total * 100`, or 0 when the total is 0
5. `annot[mechanism, tool] = f"{count:,}\n({pct:.1f}%)"`
6. `sns.heatmap(pct, annot=annot, fmt="", cmap="YlOrRd")`, with a gray rectangle and a black label over every zero cell
7. Column labels `Tool\n(n=total)`, turned by 45 degrees so that they do not overlap, the title `Configuration Mechanisms per Agentic AI Tool (n = rows)`, save, and log `Wrote config_tool_heatmap.pdf`

### Output

`config_tool_heatmap.pdf`, a heatmap with one row per mechanism, in `MECHANISMS` order, columns Claude, Copilot, Cursor, Gemini, Codex, OpenCode and No specific tool.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` or a mechanism CSV absent | `FileNotFoundError` from pandas |
| A mechanism CSV without `tool`, from a detection step without the `tool` column | `ValueError` from `usecols`. Run the detection step again |
| A row's repository is not in `repos.csv` | Not counted |
| A tool with no repository | A column of `0 (0.0%)`, all gray |
| A flag spelled `true` | Read as TRUE |
| A mechanism column missing from the CSV | `KeyError` at the coercion |
| Every row FALSE everywhere | A gray matrix, `n` the row count |

## Files

| File | Action |
|------|--------|
| `config_tool_heatmap.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `../../data/collection/output/<mechanism>.csv` | Read: `repo_name` and `tool` |
| `config_tool_heatmap.pdf` | Write |
| `../test_rq2_config_tool_heatmap.py` | Tests |

## Usage

```bash
python3 config_tool_heatmap.py
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 config_tool_heatmap.py
```

## Tests

See `../test_rq2_config_tool_heatmap.py`, which uses the standard library's `unittest` over pandas, matplotlib and seaborn, under `MPLBACKEND=Agg`. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. Because the script writes only a PDF, the suite wraps `seaborn.heatmap` and asserts the share matrix and the annotation DataFrame that the script passes to it. The input is a six-row `repos.csv` and the ten mechanism CSVs, with Claude in four repositories, Cursor in two, Copilot in one, one repository with no specific tool, and Gemini and Codex in none. One repository has a Claude context file and a Copilot command.

- **`TestTheMatrices`** (6): The axes. `Rules/Claude` is 2 of 4 and `Skills/Cursor` 2 of 2, with the `count\n(pct%)` labels, and the two empty tools give `0 (0.0%)` without a division. A cell counts the files attributed to the tool, so the Copilot command counts under Copilot and not Claude. A file of two tools counts for each, and shared files count only for the repository with no specific tool
- **`TestTheLabels`** (3): the column labels and the row labels read back from the axes in order, with each tool's `n` in its column label, the column labels turned by 45 degrees and aligned right, and the title's `n` of six rows
- **`TestThePdf`** (3): the file starts with `%PDF` and the name is logged, the PDF embeds TrueType fonts and no Type 3 font, and nothing is written beside the script
- **`TestOverTheFixture`** (1): a subprocess over `testdata/output/` writes the one PDF

Run with:

```shell
python3 -m unittest test_rq2_config_tool_heatmap -v
```

## Verification

1. Check each tool's column count against the flags of the current `repos.csv`.
2. For each mechanism/tool cell, independently count the repositories with a row in the mechanism CSV that names the tool in its `tool` value, split on `|`, and for the last column the repositories with `no_specific_tool` and a row of the mechanism. Divide by that tool's repository count to check the percentage, and check that a tool with no repositories gets `0 (0.0%)`.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/config_tool_heatmap`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- The count, share and annotation matrices are built inline. A pure `counts_and_shares(df)` would let the suite assert the numbers without wrapping seaborn
- The shares are computed from the current dataset for each release
