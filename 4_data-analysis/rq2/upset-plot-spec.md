# UpSet plot of tool usage

## Context

`upset_plot.py` draws how seven categories of tool usage, the six tools and the `agents.md` context file flag, co-occur across the repositories that use at least one of them. The plot has one bar per distinct combination over a membership matrix, with the dots colored per category. `_shared.compute_intersections` does the arithmetic and `_shared.plot_upset` the drawing. This script coerces the seven columns, renames them to their display labels, and passes them to those two functions. The per-category totals are the current counts of the flags. The `n` of the title is the number of repositories with at least one of the seven categories, and the plot leaves out a repository with a mechanism but none of these seven flags.

## Key decisions

- **Language:** Python 3.14, in the single script `upset_plot.py`, with `pandas` and `matplotlib`, and with the backend set to `Agg` before pyplot is imported
- The seventh category is the `agents.md` column, which marks the file, rather than the `no_specific_tool` flag. The rename covers only the six tools and `agents.md`, because a rename over every column would turn the `no_specific_tool` flag into a second `AGENTS.md` column, and the plot would count the wrong column
- The plot leaves out intersections below `MIN_SUBSET_SIZE = 5`, the default of `--min-count`. Over the fixture, every intersection is below the default, and a run with the default therefore draws nothing and exits 1. A small sample needs `--min-count 1`, which is also how the suite runs the script over the fixture. The tracked figure uses the default
- `top_n` stays at the default of the renderer, <!--s:parameters.4_data_analysis._shared.plot_upset.top_n-->25<!--/s-->
- A repository with none of the seven categories is left out of the bars (`drop_empty=True`) and of the `n` of the title, which is the sum of the intersection counts
- `main(argv)` takes the argument list, so that a caller can pass `[]` or `["--min-count", "1"]` without touching `sys.argv`

## Script: `upset_plot.py`

### CLI interface

```
python3 upset_plot.py [--min-count N]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--min-count` | `5` (`MIN_SUBSET_SIZE`) | Smallest intersection plotted |

The script reads the CSV from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and writes its output to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

The script is one `main(argv=None)`, which parses the arguments, reads, coerces, renames, and calls `compute_intersections` and `plot_upset`.

### Module-level data

`CATS` lists the seven display labels in order: Claude, Copilot, Cursor, Gemini, Codex, OpenCode, AGENTS.md. `CAT_COLORS` gives one color per label, and `MIN_SUBSET_SIZE` is <!--s:parameters.4_data_analysis.rq2.upset_plot.MIN_SUBSET_SIZE-->5<!--/s-->. `OUTPUT_DIR` is the output directory, and `TOOLS` (an alias of `SPECIFIC_TOOLS`), `DATA_DIR`, `TOOL_LABELS`, `compute_intersections` and `plot_upset` come from `_shared`.

### Function structure

**`main(argv=None)`**: parses `--min-count`, reads `repos.csv`, coerces and renames the seven columns, computes the intersections, logs the `n`, and draws `upset-tool-usage.pdf`

### Processing pipeline

1. Parse `argv` (`sys.argv[1:]` when `None`)
2. Read `repos.csv`, and coerce the six tool columns and `agents.md`
3. Rename the six tools to `TOOL_LABELS` and `agents.md` to `AGENTS.md`
4. `compute_intersections(df[CATS], CATS, drop_empty=True)`, `using = sum of counts`, and log `using repositories use at least one of ...`
5. `plot_upset(...)` with the title `Agentic Tool Usage Across Repositories (n=using)`, `min_count=args.min_count`, `set_colors=CAT_COLORS`
6. Log `Wrote <path>`

### Output

`upset-tool-usage.pdf`: up to <!--s:parameters.4_data_analysis._shared.plot_upset.top_n-->25<!--/s--> bars, tallest first, each at least `--min-count` repositories, over a seven-row dot matrix colored per category.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos.csv` absent | `FileNotFoundError` from pandas |
| No intersection reaches `--min-count` | `ValueError: No intersection has at least <min_count> repositories. Lower --min-count.`, exit 1, and nothing is written |
| `agents.md` column absent | `KeyError` at the coercion |
| A row with none of the seven flags | Not a bar and not in `n` |
| An unknown flag | argparse exits 2 |
| A `--min-count` of 0 or below | Every intersection passes the filter |

## Files

| File | Action |
|------|--------|
| `upset_plot.py` | Main script |
| `../../data/collection/output/repos.csv` | Read |
| `upset-tool-usage.pdf` | Written |
| `../test_rq2_upset_plot.py` | Tests |

## Usage

```bash
python3 upset_plot.py                    # the tracked figure, intersections of five or more
python3 upset_plot.py --min-count 1      # every intersection, up to 25

# The fixture's largest intersection is 3, so the default draws nothing there
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 upset_plot.py --min-count 1
```

## Tests

See `../test_rq2_upset_plot.py`, a standard library `unittest` suite that needs pandas and matplotlib. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. `main(argv)` runs in-process over a nine-row `repos.csv` with `DATA_DIR` and `OUTPUT_DIR` replaced: five repositories with Claude alone, two with Claude and an `AGENTS.md`, one with both the `agents.md` and the `no_specific_tool` flag set, and one with nothing. The test cases that check the intersections replace `plot_upset` with a recorder, and the test cases that check the PDF keep it. One test class runs the script as a subprocess over `testdata/output/` twice, with and without `--min-count 1`.

- **`TestTheCommandLine`** (4): The default `min_count` is the constant, and `--min-count` reaches the renderer. An unknown flag and an abbreviated flag exit
- **`TestTheIntersections`** (3): The bars sum to the eight repositories with a flag, and the title states 8. `AGENTS.md` counts the `agents.md` column and not `no_specific_tool`, so the combinations are Claude 5, Claude+AGENTS.md 2, AGENTS.md 1. The colors and the path reach the renderer
- **`TestThePdf`** (3): The default draws when an intersection reaches <!--s:parameters.4_data_analysis.rq2.upset_plot.MIN_SUBSET_SIZE:words-->five<!--/s-->. A `--min-count` above the largest intersection raises and writes nothing, and nothing is written beside the script
- **`TestOverTheFixture`** (2): The default exits 1 naming `min_count` and writes nothing. `--min-count 1` exits 0, logs the repositories of the fixture, and writes the one PDF

Run with:

```shell
python3 -m unittest test_rq2_upset_plot -v
```

## Verification

1. Count the repositories with a named tool flag or `agents.md` set, and compare that count with the `n` that the script logs.
2. Count each exact combination of categories yourself, and check the plotted intersections against those counts and the requested minimum size, including the error when no intersection reaches it.
3. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/upset_plot`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- The `n` in the title counts the repositories with one of the seven categories, not the rows of `repos.csv`. Over the current output CSVs, the `n` is <!--s:analysis.upset_tool_usage.repositories-->10,844<!--/s--> of <!--s:collection.configured_repositories-->10,944<!--/s-->. The plot leaves out the <!--s:collection.no_specific_tool_without_agents_md-->100<!--/s--> repositories with no specific tool and no `AGENTS.md`, and the figure does not name them
- The script writes the numbers only into the PDF, and no CSV or table
