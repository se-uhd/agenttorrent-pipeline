# UpSet plot of the configuration mechanisms

## Context

`config_mechanism_upset.py` draws how the ten configuration mechanisms co-occur across repositories as one bar per distinct combination, tallest first, over a membership matrix, with the count axis on a log scale. `_shared` contains the membership arithmetic and the drawing (`compute_intersections`, `plot_upset`). This script reads the CSV, orders the mechanisms as `repos.csv` lists them, and passes the flags through.

## Key decisions

- **Language:** Python 3.14, in the single script `config_mechanism_upset.py`, with `pandas` and `matplotlib`. `_shared.plot_upset` writes the figure with `_shared.save_figure`, which sets `pdf.fonttype` and `ps.fonttype` to <!--s:parameters.4_data_analysis.shared.save_figure.pdf_fonttype-->42<!--/s--> so that the PDF embeds TrueType fonts
- The mechanism order is the CSV's, `CONFIG_COLUMNS`, rather than `_shared.MECHANISMS`. The suite checks that the two lists are permutations of each other
- The population is the repositories with at least one mechanism. A repository with none, such as a repository with only an `other` configuration artifact, is in no bar (`drop_empty=True`) and not in the title's `n`, which is the population that the bars partition. The row count of `repos.csv` as `n` would include repositories that no bar contains
- The count axis is logarithmic (`y_axis_log_scale=True`), because the largest mechanism combinations exceed the smaller combinations so far that a linear axis would make the smaller bars hard to tell apart
- The three flags set the renderer's parameters, `--top-n`, `--min-count` and `--show`, and the defaults draw the tracked figure. `main()` reads `sys.argv` directly

## Script: `config_mechanism_upset.py`

### CLI interface

```
python3 config_mechanism_upset.py [--top-n N] [--min-count N] [--show]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--top-n` | `25` | How many intersections to plot, tallest first |
| `--min-count` | `1` | Smallest intersection plotted |
| `--show` | off | Also open the figure in a window |

The script reads the CSV from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and writes the figure to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

One `main()` that parses the flags, reads and coerces the CSV, and calls `compute_intersections` and `plot_upset`.

### Module-level data

`CONFIG_COLUMNS` lists the ten mechanisms in the column order of `repos.csv`, and `MECHANISM_LABELS` their `_shared.MECHANISM_LABELS` names in the same order. `OUTPUT_DIR` is the output directory, and `DATA_DIR`, `compute_intersections` and `plot_upset` come from `_shared`.

### Function structure

**`main()`**: parses `sys.argv`, reads `repos.csv`, coerces the ten columns, computes the intersections and draws them to `config_mechanism_upset.pdf`

### Processing pipeline

1. Parse the three flags
2. Read `repos.csv` and coerce `CONFIG_COLUMNS` with `str(v).upper() == "TRUE"`
3. `compute_intersections(df[CONFIG_COLUMNS], CONFIG_COLUMNS, drop_empty=True)`: a bitmask per row, one `Intersection` per distinct mask, sorted by count descending, then by mask
4. `plot_upset(...)` with the title `Configuration Mechanism Usage Across Repositories (n=<repositories with a mechanism>)`, the flags, and the log axis
5. Log `Wrote <path>`

### Output

`config_mechanism_upset.pdf`: a bar chart of up to `--top-n` intersections with the count above each bar, over a dot matrix with one row per mechanism.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` absent | `FileNotFoundError` from pandas |
| No intersection reaches `--min-count` | `ValueError: No intersection has at least <min_count> repositories. Lower --min-count.` from `plot_upset`, and nothing is written |
| `--top-n 0` or negative | The renderer plots <!--s:parameters.4_data_analysis._shared.plot_upset.min_top_n:words-->one<!--/s--> intersection (`max(1, top_n)`) |
| Every row has no mechanism | No intersection at all, and the same `ValueError` |
| An unknown flag | argparse exits 2 |
| `matplotlib` warns that the axes are incompatible with `tight_layout` | A `UserWarning` on stderr, and the figure is written |

## Files

| File | Action |
|------|--------|
| `config_mechanism_upset.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `config_mechanism_upset.pdf` | Write |
| `../test_rq2_config_mechanism_upset.py` | Tests |

## Usage

```bash
python3 config_mechanism_upset.py
python3 config_mechanism_upset.py --top-n 10 --min-count 5
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 config_mechanism_upset.py
```

## Tests

See `../test_rq2_config_mechanism_upset.py`, which uses the standard library's `unittest` over pandas and matplotlib, under `MPLBACKEND=Agg`. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory and runs `main()` with `sys.argv` replaced over a seven-row `repos.csv`. The test cases that check the intersections replace `plot_upset` with a recorder, and the test cases that check the PDF keep the real function.

- **`TestTheCommandLine`** (4): The defaults <!--s:parameters.4_data_analysis._shared.plot_upset.top_n-->25<!--/s-->, <!--s:parameters.4_data_analysis.rq2.config_mechanism_upset.min_count-->1<!--/s--> and off reach the renderer with the log axis, and so do the three flags. An unknown flag and an abbreviated flag exit
- **`TestTheNumbers`** (3): The bars sum to the repositories with a mechanism, 3, 2 and 1 for the three combinations. The title counts the six repositories that the bars partition and not the seventh row, which has no mechanism. A `--min-count` above the largest intersection raises and writes nothing
- **`TestThePdf`** (2): the file starts with `%PDF` and the path is logged, and nothing is written beside the script
- **`TestOverTheFixture`** (1): a subprocess over `testdata/output/` writes the one PDF

Run with:

```shell
python3 -m unittest test_rq2_config_mechanism_upset -v
```

## Verification

1. Check that the title's `n` is the number of rows of `repos.csv` with at least one of the ten mechanism flags.
2. Count the repositories of each exact mechanism combination independently of the script and compare the counts with the plotted intersections. Which combination is the largest depends on the input.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/config_mechanism_upset`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- Because `main()` takes no `argv` parameter, a caller has to replace `sys.argv`, whereas the `main()` of `upset_plot.py` takes such a parameter
- The numbers are only in the PDF, and the script writes no table
