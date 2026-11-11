# Monthly AI commit volume

## Context

`monthly_ai_commit_volume.py` plots the number of AI commits per month in the commit CSV `data/collection/output/all_ai_commits.csv`, from a start month through the month before the month of the latest scan. The latest scan is the latest `scanned_at` of `data/collection/output/repos.csv`. The title names the first and the last month and the numbers of commits and of distinct repositories in the plotted window. The counts come from the current commit CSV and do not depend on the totals of earlier publications.

## Key decisions

- **Language:** Python 3.14, in the single script `monthly_ai_commit_volume.py`, with `pandas` for the CSV, `matplotlib` for the figure, and `seaborn` through `_shared.configure_plot_style`
- The commit CSV comes through `_shared.read_commits_csv`, which reads `all_ai_commits.csv` and, when the CSV is absent, extracts the archive beside it with the `7zz` binary: the numbered volumes from `all_ai_commits.csv.7z.001` on, or a single `all_ai_commits.csv.7z`. The CSV is gitignored and the archive is tracked. In a clean checkout, the script therefore draws the figure without extracting the archive by hand. Where git tracks the archive beside the CSV, as in a checkout, a CSV that differs from it stops the script (`_shared.checked_copy`), because the figure would not be that of the commit. The step declares the archive as an alternative of the CSV, and a changed archive runs the step again ([run-analysis-spec.md](../run-analysis-spec.md))
- The script reads the two columns of the commit CSV that it uses, `repo_name` and `commit_timestamp` (`COMMIT_COLUMNS`), through the `columns` argument of `read_commits_csv`, which reads them as text and in chunks. The commit messages make up most of the CSV. Reading every column in one pass would hold the tokens of the whole file and every message in memory, for a figure that only counts commits per month
- The comparison is by month. Each row is truncated to the first of its month in UTC before the comparison with `--plot-start`, and a start inside a month therefore leaves that whole month out
- A commit without a readable timestamp is left out with a count rather than stopping the run. `errors="coerce"` and `dropna` drop the row, and the script logs `INFO dataset_monthly_ai_commit_volume: <n> commits without a readable timestamp were left out.`, as the adoption timeline counts its undated rows. Such a commit cannot be placed in a month and is in no count of the figure
- The window ends at the month before the month of the latest scan, the latest `scanned_at` of `repos.csv` in UTC (`latest_scan`). The collection reads a repository's commits when it scans the repository, and it therefore read the month of the latest scan only up to the moment at which it scanned each repository. Plotted, the part of the month that the collection read would look like a drop in the count. The script logs the commits of that month as `INFO dataset_monthly_ai_commit_volume: <n> commits of <month>, the month of the latest scan, were left out.`. The earliest `scanned_at` is not the bound. A repository that a collection takes over keeps the `scanned_at` of the run that collected it, and the earliest `scanned_at` can therefore be a month before the collection and would leave out a complete month
- A commit dated after the latest scan is left out with a count, `INFO dataset_monthly_ai_commit_volume: <n> commits dated after the latest scan (<time> UTC) were left out.`. The collection read every commit by then. `commit_timestamp` is the author date (`%ad`), which the author sets, and a commit can be dated after the collection read it. The latest commit is not the bound for that reason: One commit dated months after the collection would end the window in the month before its own, with the incomplete month of the collection and zero months after it
- A `scanned_at` with a date and no time, as the collection of March 2026 wrote, stands for the whole day and bounds the commits at the day's last second. Read as midnight, it would count the commits of that day as dated after the scan. Without a readable `scanned_at` in `repos.csv`, the month in which the collection ended is unknown, and the script stops with `SystemExit` when the commit CSV has rows
- A month without a commit is plotted as 0. Every month from the first month that the window admits to the month before the month of the latest scan is a point. Counting only the months with a commit would leave out a month in the window without one and draw the line straight across it, as if the count had never been zero
- The title names the start that the window admits, and its last month. The start is the year alone when the first month is a January, which keeps the default title, and the month and year otherwise, such as `Since June 2024`. For a start inside a month, the title names the month after it, which is the first month plotted. `Through <month year>` names the last month plotted
- The defaults are bound at import. `DATA_DIR` and `OUTPUT_DIR` are read from `_shared` when the module loads. `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` therefore take effect when they are set before the interpreter starts, as the check and the runner set them. `--data-dir` and `--output-path` override the defaults for one run
- `main` takes `argv` and returns 0, so that a suite can call it without a subprocess
- The PDF embeds TrueType fonts (`pdf.fonttype 42`), as every figure of the stage does

## Script: `monthly_ai_commit_volume.py`

### CLI interface

```
python3 monthly_ai_commit_volume.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--data-dir` | `_shared.DATA_DIR` | The directory with `all_ai_commits.csv` or its archive, and `repos.csv` |
| `--output-path` | `<OUTPUT_DIR>/monthly_ai_commit_volume.pdf` | Where the figure is written |
| `--plot-start` | `2025-01-01` | The first month plotted |
| `--show` | off | Also display the figure |

### Architecture

`monthly_ai_commit_volume` is a function with keyword arguments that does the work. `_parse_args` maps the command line onto those arguments, and `main` calls the function with the parsed arguments.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/dataset/` or `$ANALYSIS_OUTPUT_DIR/dataset/`

**`COMPONENT`**: `"dataset_monthly_ai_commit_volume"`, the component of the log messages

**`DEFAULT_PLOT_START`**: `"2025-01-01"`

**`DATE_ONLY`**: the pattern of a `scanned_at` with a date and no time

**`COMMIT_COLUMNS`**: `["repo_name", "commit_timestamp"]`, the columns of the commit CSV that the figure reads

Imported from `_shared`: `DATA_DIR`, `configure_plot_style`, `output_dir`, `read_commits_csv`.

### Function structure

**`latest_scan(repos)`**: the latest `scanned_at` in UTC, a date without a time read as the last second of that day, or `None` when no row has a readable `scanned_at`

**`monthly_ai_commit_volume(data_dir, *, output_path, plot_start, show)`**: reads the CSV and the latest scan of `repos.csv`, parses `commit_timestamp` in UTC and drops the rows that do not parse, truncates each row to its month, leaves out the commits dated after the latest scan and the month of the latest scan, keeps the months at or after `plot_start`, counts per month with a zero for every month of the window without a commit, plots the counts and saves the figure

**`_parse_args(argv)`**: the four flags

**`main(argv)`**: the function over the parsed flags, which returns 0

### Processing pipeline

1. Apply the shared plot style, read the commit CSV, and read the latest `scanned_at` of `repos.csv`
2. Parse `commit_timestamp` in UTC and drop the rows that do not parse
3. Truncate each row to its month. When a row is left, stop with `SystemExit` for a `repos.csv` without a readable `scanned_at`, leave out the rows dated after the latest scan and then the rows of the month of the latest scan, each with an `INFO` line counting them, and keep the months at or after `plot_start`
4. Count rows per month, and give a count to every month from the first month that the window admits to the month before the month of the latest scan, zero for a month without a row. `n` is the number of rows kept, with the distinct `repo_name` values among them
5. Plot the counts as a line with markers, a month locator and `%b %Y` labels, titled `Since <start> Through <last month>`: the start as the year when the first month is a January, the month and year otherwise
6. Save the figure at `output_path`, and show it with `--show`

### Output

`monthly_ai_commit_volume.pdf`: one line, `AI Commits` against `Month`, titled `Monthly Volume of AI Commits Since <start> Through <last month> (n = <commits> commits from <repositories> repositories)`. The script logs `Wrote <path>`.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| Neither the CSV nor the archive in `data_dir` | `SystemExit` from `read_commits_csv` naming the CSV |
| A CSV that differs from the archive that git tracks beside it | `SystemExit` naming both files and the command that extracts the archive again |
| The archive present and `7zz` not on PATH | `FileNotFoundError` from `subprocess`, exit 1 |
| A `commit_timestamp` that does not parse, or is empty | The row is dropped before the count |
| A `--plot-start` inside a month | That month is left out, because its rows are truncated to the first of the month, which is before the start. The title names the month after it |
| A month of the window with no commit | Plotted as 0 |
| The month of the latest scan | Left out, with an `INFO` line counting its commits |
| A commit dated after the latest scan | Left out, with an `INFO` line counting such commits |
| A `scanned_at` with a date and no time | Read as the last second of that day |
| No readable `scanned_at` in `repos.csv`, and a commit to place | `SystemExit` naming `repos.csv` |
| No `repos.csv` in `data_dir` | `FileNotFoundError` from pandas, exit 1 |
| An offset in a timestamp | Read in UTC before the month is taken |
| No row at or after the start | An empty plot with `n = 0 commits from 0 repositories` |
| The output directory absent | `FileNotFoundError` from matplotlib, exit 1 |

## Files

| File | Action |
|------|--------|
| `monthly_ai_commit_volume.py` | Main script |
| `../../data/collection/output/all_ai_commits.csv`, or the archive beside it (`all_ai_commits.csv.7z.001` and its volumes, or `all_ai_commits.csv.7z`) | Read: `repo_name`, `commit_timestamp` |
| `../../data/collection/output/repos.csv` | Read: `scanned_at` |
| `../_shared.py` | Imported: `DATA_DIR`, `configure_plot_style`, `output_dir`, `read_commits_csv` |
| `monthly_ai_commit_volume.pdf` | Written: the figure, tracked |
| `../test_dataset_monthly_ai_commit_volume.py` | Tests |

## Usage

```bash
cd 4_data-analysis/dataset
python3 monthly_ai_commit_volume.py
python3 monthly_ai_commit_volume.py --plot-start 2024-06-01 --output-path /tmp/volume.pdf

# Against the fixture, writing under /tmp/figures/dataset/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 monthly_ai_commit_volume.py
```

## Tests

See `../test_dataset_monthly_ai_commit_volume.py`, a standard library `unittest` suite that needs pandas, matplotlib and seaborn. The suite loads the script by path with the Agg backend. Because the script buckets the months and filters them inside the function, the suite runs the function over a commit CSV built in a temporary directory, with pyplot's `subplots` replaced by a recording stand-in, and reads the plotted months, the counts and the title back. The test cases over the fixture read `testdata/output/all_ai_commits.csv.7z` through `read_commits_csv` and skip when `7zz` is missing.

- **`TestParseArgs`**: the defaults and every flag, and argparse exits 2 on an abbreviated flag
- **`TestTheCounts`**: Rows before the start and rows without a date are left out, and a month without a commit is a zero. The month of the latest scan is left out and counted in the log, and the months before it are plotted with their zeros. Commits through September 2026 and one dated March 2027, with the latest scan on 2026-10-06, end the window in September 2026 and count the 2027 commit in the log. A scan with a date and no time bounds the commits at the end of its day, and a `repos.csv` without a readable scan stops the script. The rows without a date are counted in the log. An earlier start admits the earlier row and names its year, and a start inside a month leaves that whole month out and names the month after it. The figure is written at the given path, and a missing commit CSV stops the script. Over a commit CSV with long commit messages, the run allocates less than half of the size of the messages, which `tracemalloc` measures after a first run has loaded the plotting modules
- **`TestOverTheFixture`**: The window since 2025 over the fixture has the counts computed by hand: the commits and their repositories through August 2026, January 2025 plotted although it has no commit, and the commits of September 2026, the month of the fixture's latest scan, left out. `main` passes `--plot-start` through, plotting from June 2024 through August 2026 with the earlier commit in September 2024, returns 0 and writes the figure. A subprocess with the two environment variables set writes under the temporary root, leaves nothing new beside the script and does not extract the archive beside itself

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_dataset_monthly_ai_commit_volume -v
```

## Verification

1. Count, independently of the script, the commits and the distinct repositories in the requested window, and compare the monthly values and the commit and repository counts in the title.
2. From `4_data-analysis/`, run `python3 check_reproduction.py --only dataset/monthly_ai_commit_volume`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked figure.
3. Read the results together with the coverage of the commit reclassification that `docs/dataset.md` states.

## Known limitations

- No function returns the month counts. A suite reads them through a stand-in for pyplot, and a function `monthly_counts(df, plot_start)` would let it assert them directly
- pandas warns that the period conversion drops the time zone. Parsing in UTC already applied the time zone. The warning adds no information, and the suite silences it
- The window's `n` is not a published number, and the title does not state how many commits the CSV contains in all
- A collection that runs across the end of a month reads that month only in part for the repositories that it scanned before the month ended, and the window includes that month as the month before the month of the latest scan
