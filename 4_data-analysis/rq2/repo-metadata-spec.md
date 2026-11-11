# Build the per-repository metadata table

## Context

`_repo_metadata.py` joins the classification stage's full table, `data/classification/repos_annotated.csv` (in the development dataset, <!--s:classification_2026_10_23.labeled-->44,842<!--/s--> rows, one per labeled repository), with the GHS snapshot that the sampling stage filtered, `data/sampling/ghs_filtered.json`, and writes one row per repository with its main language, its age in days, and its contributor, commit and size counts as GHS recorded them. The module is a helper and draws no figure: `repo_metadata_comparison.py` reads the DataFrame that it returns, and `language_ranking_all.py` ranks the languages in that DataFrame. Both call `build_repo_metadata()`, and each call writes `repo_metadata.csv` to `data/analysis/rq2/`.

The module measures the age at the day on which the snapshot was downloaded, which it reads from the snapshot's own `_metadata.input_source.downloaded_at`.

## Key decisions

- **Language:** Python 3.14, in the single module `_repo_metadata.py`, with `pandas` for the join
- The population is every classified repository rather than the sample. The module keeps every row of `repos_annotated.csv` that the GHS snapshot lists, whatever its `engineered_project` label, and the CSV therefore contains `false` and `unclear` rows. A caller narrows the population itself: `repo_metadata_comparison.py` joins `repos.csv` on top for the configured repositories, and `language_ranking_all.py` keeps the names that `sample_names` returns for the sample
- The join is inner on the repository name. It drops a classified repository that the snapshot does not list, and a snapshot item that was never classified, without a message. The module stops with `SystemExit` instead and leaves `repo_metadata.csv` as it was when the join keeps fewer than <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_JOINED_SHARE.percent-->90%<!--/s--> (`MIN_JOINED_SHARE`) of an annotated table of at least <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_ROWS_FOR_SHARE-->1,000<!--/s--> rows (`MIN_ROWS_FOR_SHARE`). Every annotated row matches the sampling frame that it was derived from, and a loss of that size therefore means that a key stopped matching rather than that the sample shrank. The join is one-to-one. A name listed twice in either input stops the module in the same way, because the repository would otherwise have two rows and count twice in every statistic
- The reference date comes from the snapshot rather than from a constant or the clock, because `createdAt`, `contributors`, `commits` and `size` describe the repository on the download day, and the age beside them has to end on that day too. A snapshot without `downloaded_at` stops the module with `SystemExit`, and the module never measures against a guessed day
- The module uses only the date part of `downloaded_at`, and `repo_age_days` is a whole number of days between two dates
- Every call writes the CSV, although the two callers read the returned DataFrame. The file under `../data/analysis/rq2/` is for a reader of the analysis, and `run_analysis.py` declares it as an output of both callers
- `OUTPUT_DIR = output_dir(__file__)` is the one path that the module owns. The two inputs come from `_shared.CLASSIFICATION_DIR` and `_shared.SAMPLING_DIR`, which `DATA_COLLECTION_OUTPUT` does not redirect

## Script: `_repo_metadata.py`

### CLI interface

The module takes no arguments. `python3 _repo_metadata.py` calls `build_repo_metadata()` once. The inputs are fixed paths under `data/classification/` and `data/sampling/`, and the module writes its output to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/` when that variable is set.

### Architecture

The module has three functions. `reference_date` reads the download day out of the parsed snapshot. `sample_names` reads the names of the sample out of the annotated table. `build_repo_metadata` reads both inputs, joins them, derives the age, renames `mainLanguage`, writes the CSV and returns the DataFrame.

### Module-level data

`OUTPUT_DIR` comes from `_shared.output_dir`, and `CLASSIFICATION_DIR` and `SAMPLING_DIR` are imported from `_shared`. The suite replaces all three on the module. `derive_sample` comes from `agentcontext.steps.engineered_project_selection`.

### Function structure

**`reference_date(ghs)`**: `pd.Timestamp` of the date, `YYYY-MM-DD`, at the start of `_metadata.input_source.downloaded_at`, and `SystemExit` naming the field when it is absent or empty

**`labels_csv()`**: `CLASSIFICATION_DIR / "repos_annotated.csv"`, through `_shared.checked_copy` with `repos_annotated.7z` beside it as the archive. Where git tracks that archive, as in a checkout, labels that differ from the file of their name in it stop the module with `SystemExit`, because the analysis would draw the sample from labels that the commit does not contain. The archive is named for the labels without the extension of the CSV, because it also holds `repos_annotated.json` and `repos_excluded.csv`

**`sampling_frame_json()`**: `SAMPLING_DIR / "ghs_filtered.json"`, through `_shared.checked_copy` with `ghs_filtered.json.7z` beside it as the archive

**`sample_names()`**: the set of `repo_name` values of the rows of `labels_csv()` that `engineered_project_selection.derive_sample` keeps, that is, the rows labeled `engineered_project=true`. The selection step's function selects the rows of the sample, so that the analyses and the collection draw the sample from the same labels

**`build_repo_metadata()`**: the joined DataFrame with the columns `repo_name`, `main_language`, `repo_age_days`, `contributors`, `commits`, `size`. It writes `OUTPUT_DIR / "repo_metadata.csv"` and logs the row count

### Processing pipeline

1. Read `repos_annotated.csv` with pandas
2. Parse `ghs_filtered.json` and take `name`, `mainLanguage`, `createdAt`, `contributors`, `commits`, `size` from `items`
3. Inner-join on `repo_name == name`
4. Parse `createdAt` in UTC without the zone, the form of the reference date, subtract it from `reference_date(ghs)`, and keep the day count as `repo_age_days`. A sampling frame that the sampling stage writes has GitHub's creation time with a `Z`, and the March sampling frame has the GHS time without one
5. Select the six columns and rename `mainLanguage` to `main_language`
6. Stop with `SystemExit` when the join kept under <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_JOINED_SHARE.percent-->90%<!--/s--> of an annotated table of <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_ROWS_FOR_SHARE-->1,000<!--/s--> rows or more
7. Write the CSV with `index=False` to a temporary file that replaces the old one in one step, log `Wrote repo_metadata.csv (N rows)`, and return the DataFrame

### Output

`repo_metadata.csv`, columns `repo_name,main_language,repo_age_days,contributors,commits,size`, one row per repository in both inputs, in the order of `repos_annotated.csv`. `main_language` is empty where GHS records no language.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `repos_annotated.csv` or `ghs_filtered.json` absent | `FileNotFoundError` from pandas or `open`. The module docstring names the archives to extract |
| `repos_annotated.csv` or `ghs_filtered.json` differs from the archive that git tracks beside it | `SystemExit` naming both files and the command that extracts the archive again, before anything is written |
| `downloaded_at` absent or empty | `SystemExit` naming `_metadata.input_source.downloaded_at`, and nothing is written |
| A repository in one input only | Dropped by the inner join |
| A repository named twice in either input | `SystemExit` naming the duplicated key, and `repo_metadata.csv` is left as it was |
| The join keeps under <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_JOINED_SHARE.percent-->90%<!--/s--> of an annotated table of <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_ROWS_FOR_SHARE-->1,000<!--/s--> rows or more | `SystemExit` naming both counts and the share, and `repo_metadata.csv` is left as it was |
| A snapshot item without `mainLanguage` | Kept, with `main_language` written empty |
| `createdAt` after the download day | A negative age, not checked |

## Files

| File | Action |
|------|--------|
| `_repo_metadata.py` | Main script |
| `../../data/classification/repos_annotated.csv` | Read, extracted from `repos_annotated.7z` |
| `../../data/sampling/ghs_filtered.json` | Read, extracted from `ghs_filtered.json.7z` |
| `repo_metadata.csv` | Written |
| `../test_rq2_repo_metadata.py` | Tests |

## Usage

```bash
# From rq2/, after extracting both archives
python3 _repo_metadata.py

# Write under another root instead of the data root
ANALYSIS_OUTPUT_DIR=/tmp/figures python3 _repo_metadata.py     # /tmp/figures/rq2/repo_metadata.csv
```

## Tests

See `../test_rq2_repo_metadata.py`, a standard library `unittest` suite that needs pandas. The suite loads the module by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. Every test case that reads input points `CLASSIFICATION_DIR` and `SAMPLING_DIR` at a four-row annotated table and a four-item snapshot under a temporary directory, so that no test case reads an extracted archive or writes into `rq2/`.

- **`TestReferenceDate`** (3): The download day, with the time of day dropped. A snapshot without the field, in any of four forms, stops the module
- **`TestBuildRepoMetadata`** (14): A join that lost most of its rows stops the module, and so does a repository named twice, which would otherwise count twice. The CSV is replaced in one step and has the six columns. The join is inner, and a `false` and an `unclear` row are kept. The age is the day count to the download day, also for a creation time in UTC with a zone. The rename, an empty language and the unchanged GHS counts. The CSV equals the returned DataFrame, and the row count is logged. A snapshot without a date writes nothing, and nothing is written beside the script
- **`TestSampleNames`** (1): the sample is the rows labeled `true`, without the `false` and the `unclear` row, whether the snapshot lists the repository or not
- **`TestTheTrackedArchives`** (3): In a git repository in the temporary directory with an archive of other labels added to its index, `build_repo_metadata` and `sample_names` both stop, and so does `build_repo_metadata` beside a tracked archive of another sampling frame. Files that match their tracked archives are read. The case needs `7zz` and git and skips without them
- **`TestNoCommandLine`** (2): no argument parsing, and the output directory is under the override

Run with:

```shell
python3 -m unittest test_rq2_repo_metadata -v
```

## Verification

1. Check the repository names of the output against the join of the classification and sampling inputs.
2. Check `repo_age_days` against `createdAt` and the download date in the metadata of the snapshot.
3. Check the empty languages against the null `mainLanguage` values of the input. Test a missing join key and a missing date over small inputs that you write for the test.

## Known limitations

- The module does not record the release of the classification table that it read. The logged row count and the `SystemExit` for a join that keeps under <!--s:parameters.4_data_analysis.rq2._repo_metadata.MIN_JOINED_SHARE.percent-->90%<!--/s--> are the only checks
- Each of the two callers rewrites the CSV, and a run of `language_ranking_all.py` after `repo_metadata_comparison.py` therefore writes the same bytes twice
