# Context file co-occurrence, creation order and references

## Context

`cooccurrence_analysis.py` reads `context-files.csv` once and writes one PDF each for the order in which repositories create the context file categories, the references between the categories, and how often the categories co-occur in one repository. It also logs a summary of the files and repositories per category.

The categories are the <!--s:parameters.4_data_analysis._shared.CATEGORY_ORDER.length:words-->nine<!--/s--> of `_shared.CATEGORY_ORDER`, and `_shared.context_file_row_category` assigns every `context_file` value to one of them. A value that no path pattern matches is a file that the repository's configuration names, and it goes into the category `Named in Configuration`. A referenced file is classified by path alone and is `Other` where no path pattern matches it.

## Key decisions

- **Language:** Python 3.14, in the single script `cooccurrence_analysis.py`, with `pandas` and `numpy` for the output CSVs, and `matplotlib` and `seaborn` for the figures
- Every `context_file` value has a category. `_shared` derives the category map from the heuristics, and its import fails when a context file heuristic has no category. The map therefore covers every context file heuristic. Because the detection step also lists a file that the repository's configuration names as a context file, a value that no path pattern matches is such a named file rather than a path that no heuristic covers
- Creation order is by day. The earliest `created_at` per repository and category is normalized to its date, and categories created on the same day form one sorted group in the sequence. The figure shows no order within a day
- `created_at` is the date of the commit that created the file under whatever name it had then ([git](../../docs/git.md)). An `AGENTS.md` that a repository renamed from its `CLAUDE.md` or its `.github/copilot-instructions.md` keeps the date of that file, and the repository can count toward `AGENTS.md then CLAUDE.md` although it created its first `CLAUDE.md` before the `AGENTS.md`. The second line of the title therefore says that a category is dated by the creation of its earliest file under any earlier name. No published column records when a file entered its category.
- A sequence needs <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.min_sequence_categories:words-->two<!--/s--> categories. A repository with one category has no order and is left out of the count and the figure. The figure draws the <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.top_sequences:words-->five<!--/s--> most frequent sequences and combines the rest into one `Other` bar
- The script leaves a row without a `created_at`, which cannot be placed in time, out of the sequences and logs how many rows it left out. If the row were kept, its category would count toward a repository's <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.min_sequence_categories:words-->two<!--/s--> categories and then be missing from its sequence, because grouping by date drops a `NaT` key. In the March output CSVs, `clickhouse/clickhouse`'s `.cursor/BUGBOT.md` was such a row, and the fixture keeps it undated. A repository counts among the repositories with <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.min_sequence_categories:words-->two<!--/s--> or more categories only when <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.min_sequence_categories:words-->two<!--/s--> of its categories are dated, and the logged count is therefore the `n` of the figure. The reference network and the co-occurrence matrix do not read the date and keep the row
- A reference row can list several files in `referenced_file`. The script splits the value on `;`, classifies each target with `Other` as the fallback and reindexes the cross-tabulation to the fixed category order with `Other` last, so that the heatmap has the same axes for any input
- Presence is clipped to one. The co-occurrence matrix is `presence.T.dot(presence)` over a repository by category matrix of zeros and ones. A repository with two `CLAUDE.md` files counts once, and the diagonal is the number of repositories per category
- `Copilot Instructions` is shortened to `Instructions` on the axes of the two heatmaps only, and the logged tables and the creation-order labels keep the full name
- The figures embed TrueType fonts (`pdf.fonttype 42`), as every figure of the stage does

## Script: `cooccurrence_analysis.py`

### CLI interface

The script takes no arguments. It reads `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` through `_shared`.

### Architecture

`load_data` reads and classifies the rows once. The three `analysis_*` functions each take the DataFrame, log their counts and save one PDF. `print_context_file_summary` logs the per-category breakdown. `main` runs the summary first and the three analyses in order.

### Module-level data

**`OUTPUT_DIR`**: `output_dir(__file__)`, which is `../data/analysis/rq3/` or `$ANALYSIS_OUTPUT_DIR/rq3/`

**`CATEGORY_ORDER`**: the keys of `_shared.CONTEXT_FILE_CATEGORIES`, the axis order of every figure

Imported from `_shared`: `COLORS`, `CONTEXT_FILE_CATEGORIES`, `DATA_DIR`, `context_file_category`, `context_file_row_category`.

### Function structure

**`to_bool(value)`**: `TRUE` in any case is true, and anything else, an empty cell and `NaN` included, is false

**`classify_row(context_file)`**: `_shared.context_file_row_category`, which returns the first category with a path pattern that matches, else `Named in Configuration`, so that every row has a category

**`classify_referenced_file(ref)`**: `_shared.context_file_category` by path alone, with `"Other"` for a path that no category matches

**`load_data()`**: reads five columns of `context-files.csv`, parses `created_at` in UTC and adds `category`, which every row has

**`analysis_creation_order(df)`**: logs the number of rows without `created_at` and leaves them out, builds the date-ordered category sequence per repository with <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.min_sequence_categories:words-->two<!--/s--> or more dated categories, and draws the top <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.top_sequences:words-->five<!--/s--> and `Other` as a horizontal bar chart

**`analysis_reference_network(df)`**: source category to target category edges from the reference rows, cross-tabulated as a heatmap with zeros written in

**`analysis_cooccurrence_matrix(df)`**: the symmetric category by category repository count, with the lower triangle colored, the diagonal in gray, and a title with the context file categories and the repository count

**`print_context_file_summary(df)`**: files, share of all files and repositories per category, and the totals

**`main()`**: the load, the summary, the three analyses

### Processing pipeline

1. Read `repo_name`, `context_file`, `created_at`, `is_reference` and `referenced_file`, and classify every row
2. Log the per-category summary
3. Log the number of rows without `created_at`, leave them out, build the creation-order sequences, count them, draw the top <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.top_sequences:words-->five<!--/s--> plus `Other`
4. Build the reference edges, cross-tabulate, draw the heatmap
5. Build the presence matrix, multiply it by its transpose, draw the co-occurrence heatmap

### Output

`creation_order_sequences.pdf` (bars, `n` the repositories with <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.min_sequence_categories:words-->two<!--/s--> or more categories, and a second line of the title that states how a category is dated), `reference_network.pdf` (heatmap, `n` the reference pairs) and `cooccurrence_matrix.pdf` (heatmap). The script logs the row and repository count, the per-category summary, the sequence counts, the cross-tabulation, the category counts and the matrix, each as text.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| A `context_file` value that no path pattern matches | `Named in Configuration`, because the detection step lists such a file when the repository's configuration names it |
| A repository with one category | Not a sequence, and left out of the creation-order count |
| A row with an empty `created_at` | Left out of the creation-order sequences and counted in `<n> rows without created_at were left out of the sequences`. A repository with one dated category is not a sequence. The row is kept in the summary, the reference network and the matrix |
| Two categories created on the same day | One group in the sequence, sorted by name, logged in braces |
| More than <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.top_sequences:words-->five<!--/s--> distinct sequences | The sixth and later are one `Other` bar |
| An empty `referenced_file`, or an empty piece between semicolons | No edge |
| A referenced path that no category matches | The `Other` column |
| No reference row at all | `0 rows of context-files.csv are references` and `The reference network has 0 edges, one per file that a reference row names`, a cross-tabulation of zeros, and a heatmap with a `0` in every cell titled `n = 0 reference pairs`. The edge DataFrame is built with its two columns named, because from an empty list without columns the cross-tabulation would raise a `KeyError` on `source` |
| `is_reference` read as a boolean or as a string | `to_bool` accepts both |
| A CSV missing from `DATA_DIR` | `FileNotFoundError` from pandas, exit 1 |

## Files

| File | Action |
|------|--------|
| `cooccurrence_analysis.py` | Main script |
| `../../data/collection/output/context-files.csv` | Read: `repo_name`, `context_file`, `created_at`, `is_reference`, `referenced_file` |
| `../_shared.py` | Imported: the categories, the colors, `DATA_DIR`, `output_dir` |
| `creation_order_sequences.pdf`, `reference_network.pdf`, `cooccurrence_matrix.pdf` | Written: the three figures, tracked |
| `../test_rq3_cooccurrence_analysis.py` | Tests |

## Usage

```bash
cd 4_data-analysis/rq3
python3 cooccurrence_analysis.py

# Against the fixture, writing under /tmp/figures/rq3/
DATA_COLLECTION_OUTPUT=../testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures python3 cooccurrence_analysis.py
```

## Tests

See `../test_rq3_cooccurrence_analysis.py`, a standard library `unittest` suite that needs pandas, numpy, matplotlib and seaborn. It loads the script by path with the Agg backend, replaces `DATA_DIR` and `OUTPUT_DIR` with temporary directories, and reads the counts that each analysis logs from its log messages.

- **`TestToBool`**: `TRUE` in any case and the boolean `True` are true, and other strings, empty cells, `None`, `NaN` and numbers are false
- **`TestTheClassifiers`**: Known paths per category. A path that no path pattern matches is `Named in Configuration` for a row and `Other` for a reference
- **`TestLoadData`**: Every row gets its category and a UTC timestamp. A value that no path pattern matches is a file that the configuration named
- **`TestCreationOrder`**: Sequences by date with same-day categories grouped, the title's `n` the repositories with a sequence, and the second line of the title stating how a category is dated. More than <!--s:parameters.4_data_analysis.rq3.cooccurrence_analysis.top_sequences:words-->five<!--/s--> sequences are combined into `Other`. A row without a date is left out and counted, and a repository with one dated category is not a sequence
- **`TestReferenceNetwork`**: Edges split on semicolons and classified, with the cross-tabulation row read back, and the title's `n` the edges. A DataFrame without a reference row logs zeros and draws a heatmap of zeros. The column labels of the reference network and of the co-occurrence matrix are turned by 45 degrees, because the category names do not fit side by side
- **`TestCooccurrenceMatrix`**: The matrix is symmetric, with the repository count per category on the diagonal and a zero row for an absent category. The title names the context file categories and gives the repository count as `n`
- **`TestPrintContextFileSummary`**: files, share and repositories per category and the totals
- **`TestMainOverTheFixture`**: a subprocess over `testdata/output` logs the counts computed from the fixture by hand: the rows of `context-files.csv` and their repositories, the files and repositories of CLAUDE.md and of AGENTS.md, the rows without a `created_at` that the sequences leave out, and the reference rows with the files that they name. It writes the three PDFs under the temporary root, with nothing new beside the script

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_rq3_cooccurrence_analysis -v
```

## Verification

1. `python3 check_reproduction.py --only rq3/cooccurrence_analysis` from `4_data-analysis/` exits 0
2. The `TOTAL` line of the logged summary equals the row count of `context-files.csv` and its distinct `repo_name` count, and the per-category file counts sum to it
3. The diagonal of the logged matrix equals the per-category repository counts logged above it

## Known limitations

- The dates are the `created_at` values that the file history step wrote into `context-files.csv`, and the figure orders files by them. A file renamed into a category is dated by its creation under any earlier name, and a file that a configuration names is dated by its own creation, which can precede the configuration that names it. The category of either file can therefore appear before a category that the repository created first
- The `Other` target column combines the references to every file outside the categories, such as `README.md` and source files, and the figure does not show which files they are
- Because repositories with one category are absent from the creation-order figure, its `n` is smaller than the co-occurrence matrix's
