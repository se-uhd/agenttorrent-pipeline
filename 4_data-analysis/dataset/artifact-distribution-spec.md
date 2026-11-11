# Configuration artifact distribution per mechanism

## Context

`artifact_distribution.py` draws two bars for each of the ten configuration mechanisms of the current dataset release: the number of detected configuration artifacts, labeled `Configuration artifacts` in the legend, and the number of distinct repositories that have at least one. The count of a mechanism is the row count of its mechanism CSV, and the repository count is the number of distinct `repo_name` values in that CSV. The title states the sum of the configuration artifact counts and the number of distinct repositories across all ten mechanism CSVs. A mechanism with no detected configuration artifact gets bars of height zero.

The counts come from the current mechanism CSVs alone. The tests check them against the fixture rather than against the values of earlier publications.

## Key decisions

- **Language:** Python 3.14, in the single script `artifact_distribution.py`, with `numpy` and `matplotlib` for the figure, `seaborn` through `_shared.configure_plot_style`, and the standard library's `csv` module for the mechanism CSVs
- **Stdlib `csv` instead of pandas:** The work is a row count and a set of names per CSV, and reading through `csv.DictReader` keeps every cell a string
- The script stops on a missing CSV rather than counting it as zero. `load_artifact_stats` raises `SystemExit` naming the missing path and the command `python3 -m agentcontext detection`. Every other analysis script also stops on a missing output CSV, most through pandas' `FileNotFoundError`. An all-zero figure is byte-reproducible, and a script that counted a missing CSV as zero would therefore pass the reproduction check with a figure titled "n = 0 configuration artifacts from 0 repositories"
- The repository count comes from the mechanism CSV rather than from the `repos.csv` flag column. The two counts must agree, and the fixture tests check that they do
- The bars are in reverse order, so that the first mechanism of `MECHANISM_CSVS` is at the top, and each bar is labeled with its count, with thousands separators and no percentage
- The PDF embeds TrueType fonts (`pdf.fonttype 42`), as every figure of the stage does

## Script: `artifact_distribution.py`

### CLI interface

No CLI. `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` reach it through `_shared`.

### Architecture

`load_artifact_stats` reads the ten mechanism CSVs into `{label: {"configuration_artifacts", "repos"}}` and the set of distinct repositories across them, and `main` draws the paired horizontal bars from these counts.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or `$ANALYSIS_OUTPUT_DIR/dataset/`

**`MECHANISM_CSVS`**: the label and CSV of each mechanism, in figure order: Context Files, Skills, Commands, Subagents, Rules, Settings, Hooks, MCP, Plugins, LSP

Imported from `_shared`: `DATA_DIR`, `configure_plot_style`, `output_dir`.

### Function structure

**`load_artifact_stats()`**: per label, the row count and the distinct `repo_name` count of its CSV, and the distinct repositories across all CSVs, and `SystemExit` for a missing CSV or a CSV without `repo_name`

**`main()`**: the style, the stats, the two bars per mechanism with their labels, the title, the PDF

### Processing pipeline

1. Apply the shared plot style
2. Read the ten mechanism CSVs, count the rows and the distinct repositories of each, and collect the union of their repositories
3. Reverse the label order so that the first mechanism of `MECHANISM_CSVS` is on top
4. Draw a configuration artifact bar and a repository bar per mechanism, and label every bar with its count, a zero included
5. Save `artifact_distribution.pdf` with `n = <configuration artifacts> configuration artifacts from <repositories> repositories` in the title

### Output

`artifact_distribution.pdf`: two horizontal bars per mechanism, `Count` against `Configuration Mechanism`, legend `Configuration artifacts` and `Repositories`. The script logs `Wrote <path>` and no warning, because it stops on a missing CSV.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| A CSV missing from `DATA_DIR` | `SystemExit` naming the missing path and the command `python3 -m agentcontext detection`. Python prints the message without a level or a component, and `../../docs/logs.md` explains why |
| A CSV without a `repo_name` column | `SystemExit` with `<path> has no repo_name column and is not a mechanism CSV.` Without this check, a truncated CSV would pass without a message |
| A CSV with a header and no rows | Zero configuration artifacts and zero repositories, without a warning |
| A bar at zero | Labeled `0`, because without a label a reader could not tell a mechanism that no repository uses from a mechanism that the figure omitted |
| Bytes that are not UTF-8 | Read with `errors="ignore"`, and the row still counts |
| A field longer than the default limit of the csv module, such as a `referenced_file` with thousands of references | Read whole, because `_shared.py` raises the limit at import |
| Every CSV missing | `SystemExit` naming the first missing CSV, and no figure |

## Files

| File | Action |
|------|--------|
| `artifact_distribution.py` | Main script |
| `../../data/collection/output/context-files.csv`, `skills.csv`, `commands.csv`, `subagents.csv`, `rules.csv`, `settings.csv`, `hooks.csv`, `mcp.csv`, `plugins.csv`, `lsp.csv` | Read: every row, and `repo_name` |
| `../_shared.py` | Imported: `DATA_DIR`, `configure_plot_style`, `output_dir` |
| `artifact_distribution.pdf` | Written: the figure, tracked |
| `../test_dataset_artifact_distribution.py` | Tests |

## Usage

```bash
cd 4_data-analysis/dataset
python3 artifact_distribution.py

# Against the fixture, writing under /tmp/figures/dataset/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 artifact_distribution.py
```

## Tests

See `../test_dataset_artifact_distribution.py`, a standard library `unittest` suite that needs numpy, matplotlib and seaborn. The suite loads the script by path with the Agg backend. It tests `load_artifact_stats` over small CSVs with `DATA_DIR` replaced, and it runs `main` with the module's `plt` replaced by a recording stand-in, so that it can read the bar labels and the title without a file.

- **`TestLoadArtifactStats`**: Configuration artifacts are rows, and repositories are distinct names. A missing CSV stops the script rather than counting as zero, and so does a CSV without `repo_name`. The ten output CSVs are the mechanism CSVs, and a mechanism with no row gets two bars labeled `0`. A field longer than the default limit of the csv module is read in a process of its own
- **`TestOverTheFixture`**: The configuration artifacts and the repositories of each mechanism, and the repositories in all, are the counts computed from `testdata/output` by hand, with none for LSP. The repository counts agree with the `repos.csv` flag columns. Each bar's width, and the label beside it, is the count of the mechanism on its tick, and the title names the totals
- **`TestMainOverTheFixture`**: a subprocess over the fixture with `ANALYSIS_OUTPUT_DIR` set writes the PDF under the temporary root, logs no warning, and leaves nothing new beside the script

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_dataset_artifact_distribution -v
```

## Verification

1. Count, independently of the script, the configuration artifacts in each mechanism CSV and the distinct repositories that use each mechanism, and compare both series with the figure.
2. From `4_data-analysis/`, run `python3 check_reproduction.py --only dataset/artifact_distribution`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked figure. The totals that earlier papers published are not expected results.

## Known limitations

- The repository counts come from the mechanism CSVs. The flags of `repos.csv` count the repositories independently, and the `output-cross-check` step compares the repositories that the mechanism CSVs and the flags list.
- The title's total is the sum of ten row counts. A configuration artifact that appears in two mechanisms counts in both, as `gemini-extension.json` does when it is both a settings row and a plugins row, and the total therefore does not count distinct files
