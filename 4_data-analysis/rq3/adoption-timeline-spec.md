# Cumulative adoption timeline

## Context

`adoption_timeline.py` draws, for each family of configuration artifacts, the cumulative number of repositories per month, counting each repository from the month in which its earliest file of the family was created. The families are those of the timeline of release 1.0.0: the context file categories that `_shared.ADOPTION_CONTEXT_FILE_CATEGORIES` lists, CLAUDE.md, AGENTS.md, GEMINI.md, Copilot Instructions and `.cursorrules`, read from `context-files.csv`, and Skills and Subagents, read from `skills.csv` and `subagents.csv`. The planning documents, read from `planning-documents.csv`, are the family that release 2.0.0 added. A repository's month for a family is the month of its earliest `created_at` in that family from `_shared.ADOPTION_START` on.

`created_at` is the date of the commit that created the file under whatever name it had then ([git](../../docs/git.md)). A file renamed into a family keeps the date of its earlier name, such as an `AGENTS.md` that began as `.github/copilot-instructions.md` or a `claude.md` that began as `docs/development.md`. A file that a configuration names keeps the date on which the file itself was created, which can be years before the configuration, such as a `docs/source/ca_examples.rst` of 2016 that an `opencode.json` of 2026 names. A file created before `ADOPTION_START`, the first of January 2023, was created for another purpose and only later renamed into a family or named by a configuration, as the cases of kdeldycke/meta-package-manager, spyder-ide/spyder and mathiasertl/django-ca show, and the figure leaves it out, because its creation is no adoption. The figure therefore shows when the earliest file of each family was created, not when the repository adopted the family, and its title and the label of its x axis say so. No published column records when a file entered its family.

The `n` in the title counts the distinct repositories with a dated row in any plotted series, and the title does not state the number of series. `_website/build_site.py` rebuilds the timeline from the published CSVs rather than reading this PDF.

## Key decisions

- **Language:** Python 3.14, in the single script `adoption_timeline.py`, with `pandas` for the output CSVs and `matplotlib` for the figure
- The families come from `_shared`, so that `_website/build_site.py` plots the same families from the same constants. The context file categories of the timeline of release 1.0.0 stay, and the categories of the code review products and the files that a configuration names are context files of no plotted family. A family with no row in its CSV is left out rather than drawn as an empty series
- The script checks the style maps before it draws anything. The plotting loop reads `MARKERS` here and `COLORS` in `_shared` by label, and a label missing from either map would raise a bare `KeyError` partway through the loop, before `savefig`. `_assert_every_series_has_a_style` raises the error before the loop, with the map and the labels in its message
- The script reads each CSV once, into the dictionary that both the plot and the logged counts use. If the logged counts came from a second reading of every CSV, a renamed CSV would have to be corrected in two places, and a rename corrected in only one of them would still run
- The month is taken in UTC. The script parses `created_at` with `utc=True`, drops the zone and truncates the timestamp to the first of its month
- `dated_rows` leaves out a row without a `created_at`, which cannot be placed in time, and a row created before `ADOPTION_START`, before the earliest date is taken, and logs per CSV how many rows of each kind it left out. Every logged count and the `n` therefore count only what is plotted. A repository with only undated rows in a family would otherwise keep `NaT` as its earliest date and be in the logged count and the title's `n` but not in the plotted line. Over the March output CSVs, for example, `clickhouse/clickhouse`'s `.cursor/BUGBOT.md` had no date, and the logged count of the Cursor BUGBOT repositories would have included the repository while the plotted line did not. The current collection dates that row, and the fixture keeps it undated
- A series starts at its first adoption month. The script aligns the series on the union of their months, forward-fills them and drops the leading zeros
- The x axis has at most `MAX_DATE_TICKS` labels, which `AutoDateLocator` places at months, quarters or years, because over the years from 2023 on a label for every month would overlap the next
- The figure embeds TrueType fonts (`pdf.fonttype 42`), as every figure of the stage does, so that the text in the exported PDF remains text

## Script: `adoption_timeline.py`

### CLI interface

The script takes no arguments. Through `_shared`, `DATA_COLLECTION_OUTPUT` names the directory of output CSVs to read, and `ANALYSIS_OUTPUT_DIR` names a root to write under, below which the script keeps its `rq3/` directory.

### Architecture

`load_context_file_categories` builds the first-adoption series of the context file categories, and `load_first_adoption` the series of the CSVs of `MECHANISM_CSVS`. `compute_cumulative` turns each series into a cumulative count, and `main` draws one plot over the aligned series.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/rq3/` or `$ANALYSIS_OUTPUT_DIR/rq3/`

**`MARKERS`**: one marker per plotted label, the context file categories plus Skills, Subagents and Planning Documents

**`MAX_DATE_TICKS`**: the maximum number of labels on the x axis, which `AutoDateLocator` places

**`MECHANISM_CSVS`**: `{"Skills": "skills.csv", "Subagents": "subagents.csv", "Planning Documents": "planning-documents.csv"}`, the CSVs plotted beside the categories

Imported from `_shared`: `ADOPTION_CONTEXT_FILE_CATEGORIES`, `ADOPTION_START`, `COLORS`, `DATA_DIR`, `context_file_row_category`.

### Function structure

**`dated_rows(df, source)`**: parses `created_at` in UTC, logs `<n> rows of <source> without created_at and <m> created before <ADOPTION_START> were left out`, and returns the rows that have one from `ADOPTION_START` on

**`load_first_adoption(csv_path)`**: reads `repo_name` and `created_at` and returns the earliest `created_at` per repository over the dated rows, truncated to the first of its month, without a zone

**`load_context_file_categories()`**: the same per category over `context-files.csv`, with each row in the category that `_shared.context_file_row_category` gives it and without the categories that have no row

**`compute_cumulative(first_adoption)`**: repositories per month, sorted by month, summed cumulatively

**`_assert_every_series_has_a_style()`**: raises `KeyError` naming the map and the labels when a series has no marker or no color

**`main()`**: the check of the style maps, one series per label with a row, one logged count per series, the aligned plot, the PDF

### Processing pipeline

1. Check that every label has a marker and a color
2. Read the first-adoption series over the dated rows from `ADOPTION_START` on, one for each label that has a row, out of the <!--s:parameters.4_data_analysis.rq3.adoption_timeline.MARKERS.length:words-->eight<!--/s--> labels of `MARKERS`, and log per CSV how many rows had no date and how many were created before `ADOPTION_START`
3. Log the number of repositories in each series
4. Compute the cumulative count per series and align the series on the union of their months, forward-filled
5. Plot each series from its first non-zero month, with at most `MAX_DATE_TICKS` labels in the format `%b %Y` on the x axis, which `AutoDateLocator` places at months, quarters or years
6. Save `cumulative_repo_adoption.pdf` with `n` the distinct repositories across all series

### Output

`cumulative_repo_adoption.pdf`: one line per series with a legend, `Cumulative Unique Repositories` against `Month of the Commit That Created the Earliest File, Under Any Earlier Name`, titled `Cumulative Repositories by the Creation Month of Their Earliest File in Each Family (n = <n>)`. The script logs one `<n> rows of <CSV> without created_at and <m> created before 2023-01-01 were left out` line per CSV read, then the table `Unique repositories per series of the adoption timeline` with one row of the label and the count per series, and the path of the saved PDF.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| A label without a marker or a color | `KeyError` from `_assert_every_series_has_a_style` before any drawing, and nothing is written |
| A category with no row in `context-files.csv` | Left out of the figure and of the logged summary |
| A mechanism CSV with no rows | Logged with the count `0` and not drawn |
| A `created_at` with an offset | Read in UTC before the month is taken: 01:00 on the first with `+02:00` is the previous month |
| A row with an empty `created_at` | Left out before the earliest date is taken and counted in the CSV's `rows without created_at` line. A repository with only undated rows in a family is in neither that family's count, its line nor the title's `n` |
| A row created before `ADOPTION_START` | Left out before the earliest date is taken and counted in the CSV's line as created before 2023-01-01, with the same consequences for the counts, the lines and the `n` |
| A CSV missing from `DATA_DIR` | `FileNotFoundError` from pandas, exit 1 |
| A CSV without `created_at` or `repo_name` | `ValueError` from pandas' `usecols`, exit 1 |

## Files

| File | Action |
|------|--------|
| `adoption_timeline.py` | Main script |
| `../../data/collection/output/context-files.csv` | Read: `repo_name`, `context_file`, `created_at` |
| `../../data/collection/output/skills.csv`, `subagents.csv`, `planning-documents.csv` | Read: `repo_name`, `created_at` |
| `../_shared.py` | Imported: the families, `ADOPTION_START`, the colors, `DATA_DIR`, `output_dir` |
| `cumulative_repo_adoption.pdf` | Written: the figure, tracked |
| `../test_rq3_adoption_timeline.py` | Tests |

## Usage

```bash
cd 4_data-analysis/rq3
python3 adoption_timeline.py

# Against the fixture, writing under /tmp/figures/rq3/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 adoption_timeline.py
```

## Tests

See `../test_rq3_adoption_timeline.py`, a standard library `unittest` suite that needs pandas and matplotlib. It loads the script by path with the Agg backend and replaces `DATA_DIR` and `OUTPUT_DIR` with temporary directories, so that no test case reads the stage's output CSVs or writes into `rq3/`.

- **`TestLoadFirstAdoption`**: The earliest row per repository is truncated to its month, and the result has no zone. An offset is read in UTC before the month is taken
- **`TestLoadContextFileCategories`**: One series per category, with an empty category left out, and the categories are the five of release 1.0.0 in figure order
- **`TestComputeCumulative`**: One entry per month, and the last is the repository count. A month without an adoption is not a row, and the count never decreases
- **`TestTheStyleGuard`**: The shipped maps cover every series, and every mechanism CSV has a style. A series without a marker or without a color raises `KeyError` before plotting
- **`TestMainOverSmallMechanismCsvs`**: The logged counts and the PDF over three small CSVs, every plotted line month by month, carried forward through a month without an adoption, and the title's `n`. A row without a date is left out and counted, and the logged count, the plotted line and the title's `n` agree. A file created before 2023, such as the renamed development document of kdeldycke/meta-package-manager, is left out and counted, and the title and the label of the x axis name the creation of the earliest file under any earlier name, not an adoption. Over the years from 2023 to 2026, the x axis has at most `MAX_DATE_TICKS` labels
- **`TestMainOverTheFixture`**: Every plotted line over `testdata/output`, counted from the fixture's `created_at` months, and the title's `n`. A subprocess over `testdata/output` logs the count of each series and the rows of `context-files.csv` without a `created_at`, as computed from the fixture by hand, and writes the PDF under the temporary root, with nothing new beside the script

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_rq3_adoption_timeline -v
```

## Verification

1. `python3 check_reproduction.py --only rq3/adoption_timeline` from `4_data-analysis/` exits 0, which means that the PDF reproduces byte for byte and matches the tracked PDF
2. The logged count for each category equals the number of distinct `repo_name` values in `context-files.csv` with a `context_file` that the category matches and a `created_at` from 2023 on, and Skills, Subagents and Planning Documents equal the distinct repositories of their CSVs with such a row

## Known limitations

- This script and `_website/build_site.py` implement the figure separately from the same families and the same `ADOPTION_START`, and their counting can therefore disagree without either failing
- Because a repository is dated by its earliest file in a family, the figure does not show whether that file is still the one on the default branch or how many files followed it
- A file renamed into a family and a file that a configuration names are dated by their creation under any earlier name. The figure leaves out the files created before 2023, but a file created for another purpose from 2023 on and renamed into a family later still counts from its creation
