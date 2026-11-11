# Cross-check `output/repos.csv` against the mechanism CSVs

## Context

In this document, a bare `repos.csv` is `output/repos.csv`, the only file of that name that the script reads. The pipeline has two other files named `repos.csv`, which earlier steps write: the collected list at `data/collected/repos.csv` and the validated list at `data/validated/repos.csv`.

Two steps write the list of repositories before this script runs. `agentcontext/steps/detection.py` writes `repos.csv` and the mechanism CSVs in one pass of the detectors. `agentcontext/steps/file_history.py` then removes the rows of every repository that git reports as gone when the step clones it again, and it derives `repos.csv` again from the remaining rows. `repos.csv` and the mechanism CSVs should name the same repositories. Every repository in `repos.csv` appears in at least one mechanism CSV, and every repository in a mechanism CSV appears in `repos.csv`. If the two sets differ, an earlier step wrote inconsistent data, and every later count, the paper's figures included, would come from two sets that do not match.

The script reads the `repo_name` column of every mechanism CSV, takes the union, and compares it with the repositories of `repos.csv`. `planning-documents.csv` is not one of the mechanism CSVs, because a planning document is no configuration artifact and makes no repository configured. Every repository in it still has to be in `repos.csv`. The file history step rebuilds the tool flags after it removes rows, and the script therefore also compares each repository's tool flags with the tools that its rows name. The script writes `consistency-report.csv` with a count per file, and `consistency-mismatch.csv` with the mismatches when the sets differ. On any difference it exits `2`, and the runner stops instead of running the next step over inconsistent CSVs. The check runs as a step for this exit code, because a count alone would not stop the run.

## Key decisions

- **Language:** Python 3.14 (single script `agentcontext/steps/output_cross_check.py`), with no dependencies outside the standard library
- **CLI:** `python -m agentcontext output-cross-check` takes `--output-dir`, the directory that contains the mechanism CSVs and `repos.csv` and that receives the report. The default is `OUTPUT_DIR`, which resolves against `STAGE_DIR` whatever the working directory. A test also uses the option to check how the command handles absent inputs
- **The list of CSVs comes from the heuristics:** `MECHANISM_FILES` is `heuristics.csv_filenames()`, the filenames that `heuristics.MECHANISM_CSVS` declares. The step checks a mechanism added to the heuristics without an edit here
- **Set membership counts each repository once:** the check compares which repositories exist, and a repository with ten configuration artifacts counts once. The number for a file is the count of distinct repositories in that file
- **Union across every mechanism CSV:** a repository is "in the mechanism CSVs" if it appears in any one of them. `reconcile_repos_csv` keeps a repository by the same test
- **The planning documents are outside the union:** a repository in `planning-documents.csv` and not in `repos.csv` is a difference, `planning_only`, because the release would publish planning documents for a repository that the dataset does not count as configured. A repository in `repos.csv` without planning documents is the common case and no difference
- **One collected commit per repository:** every row has `head_sha`, and a repository's rows in every CSV that the step reads, `planning-documents.csv` included, must name the same non-empty commit. Rows of two collections under one name agree by name but describe two snapshots. The rows of a repository published without the commit that its files came from describe no snapshot. A repository without that commit appears when a validation run copies a directory that the collected list does not name
- **The tool flags are the tools of the rows:** a repository's six tool flags in `repos.csv` have to equal the union of the `tool` column of its rows in the CSVs of the named mechanisms, and `any` names no tool. `other.csv` sets no flag, because it contains files that no detector of a tool reads, and it is not published. The step compares every repository of `repos.csv`, including a repository without a row of a named mechanism, for which any flag is a difference. A flag that no row sets credits a tool with no configuration artifact, and a row with a tool that has no flag is missing from every count by tool. A `tool` value that the detection step cannot write, such as a blank, an unknown tool or tools out of column order, raises a `ValueError`, and the step logs it and exits `1`
- **A mismatch fails the step, with the exit code of a difference:** a difference means that an earlier step produced inconsistent output. The exit code `2` makes `agentcontext/runner.py` stop here, before `collected_files_archive` packs the sample from sets that do not match. When a CSV cannot be read, the step logs one `ERROR` line instead of a traceback and returns `1`, the exit code of a check that could not run
- **The mismatch CSV is written only when there is a mismatch, and a clean run removes a stale mismatch CSV:** a mismatch CSV on disk therefore shows a failure of the latest run that compared the CSVs, and a run after the fix leaves no outdated file behind
- **The report CSV is written on every run that reads its inputs:** it records the counts whatever the comparison finds

## `agentcontext/steps/output_cross_check.py`

### Architecture

#### The comparison

`read_repo_commits` reads the `repo_name` and `head_sha` columns of one CSV into a map from each repository to the commits that its rows name, and it raises a `ValueError` for a CSV without `head_sha`. `read_repo_names` returns the keys of that map. `read_outputs` builds `mechanism_repos` as the union over every entry in `MECHANISM_FILES` and reads the repositories of `repos.csv`, which `main` compares as `output_repos`. `main` computes two differences, `only_in_mechanisms` (in a mechanism CSV but absent from `repos.csv`) and `only_in_reference` (in `repos.csv` but in no mechanism CSV). Across all these CSVs, it also collects the repositories with a blank `head_sha` (`head_sha_missing`) and the repositories with rows that name more than one commit (`head_sha_differs`). `read_repo_tools` reads the `tool` column of each mechanism CSV through `heuristics.value_tools`, and `read_tool_flags` reads the six tool flags of `repos.csv`. A repository in `repos.csv` goes to `tool_flags_differ` when its flags differ from the tools of its rows. A repository in `planning-documents.csv` that `repos.csv` does not list goes to `planning_only`. The run is clean exactly when all these lists are empty.

#### Outputs and exit code

Every run that reads the CSVs writes `consistency-report.csv`, even when the file already holds the same counts, because the runner records the step only when the step wrote every output that it produces. Only a run with a mismatch writes `consistency-mismatch.csv`, and a clean run deletes a copy that an earlier run left. [Output](#output) gives the columns of both files. The exit code is `2` when any list of [the comparison](#the-comparison) is non-empty, `0` otherwise, and `1` when a CSV could not be read.

### Output

`consistency-report.csv` has the columns `file` and `unique_repos`:

| Row | Meaning |
|-----|---------|
| one per mechanism CSV | distinct repositories in that file |
| `TOTAL (mechanism CSVs)` | distinct repositories across the union of all mechanism CSVs |
| `repos.csv` | distinct repositories in the reference |
| `planning-documents.csv` | distinct repositories with planning documents, which the total leaves out |

`consistency-mismatch.csv` (only on a mismatch) has one row per repository and difference, with the columns `repo_name` and `source`, where `source` is `mechanism_only`, `reference_only`, `head_sha_missing`, `head_sha_differs`, `tool_flags_differ` or `planning_only`.

The exit code is `0` when the two sets are equal, every repository has one collected commit, its tool flags are the tools of its rows and every repository with planning documents is in `repos.csv`. It is `2` otherwise, and `1` when a CSV could not be read.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| Sets agree | Logs that every repository matches, writes only the report, deletes any stale `consistency-mismatch.csv`, returns `0` |
| A repository is in a mechanism CSV but not in `repos.csv` | Listed under `mechanism_only`, written to the mismatch CSV, returns `2` |
| A repository is in `repos.csv` but in no mechanism CSV | Listed under `reference_only`, written to the mismatch CSV, returns `2` |
| A row has no `head_sha`, in a mechanism CSV or in `repos.csv` | Its repository is listed under `head_sha_missing`, returns `2` |
| A repository's rows name two commits, `repos.csv` included | Listed under `head_sha_differs`, returns `2` |
| A CSV has no `head_sha` column | `ERROR` naming the file, returns `1` |
| A repository's tool flags differ from the tools of its rows | Listed under `tool_flags_differ`, returns `2` |
| A repository has planning documents and no row of `repos.csv` | Listed under `planning_only`, returns `2` |
| A repository of `repos.csv` has planning documents | Counted in the report's row for `planning-documents.csv` and not in the total of the mechanism CSVs, which is not an error |
| A mechanism CSV has no `tool` column, or a row has a value that the detection step cannot write | `ERROR` naming the file, returns `1` |
| A mechanism CSV is missing | `ERROR` naming the file, returns `1` and writes nothing, because a missing CSV means that the detection step did not complete |
| `repos.csv` is missing | The same |
| `planning-documents.csv` is missing | The same, because the detection step writes the CSV on every run, with a header alone where no repository has planning documents |
| A field of a CSV longer than the default limit of the csv module, such as a `referenced_file` with thousands of references | Read whole, because the module sets `csv.field_size_limit(sys.maxsize)` at import |
| A CSV has a header but no rows | Contributes the empty set, which is not an error |
| A repository has rows in `other.csv` alone | In the mechanism CSVs, because `other.csv` is one of them, and in agreement with a `repos.csv` that lists it |
| The same repository appears many times in one CSV | Counted once, because the check is set membership |
| A prior run left a `consistency-mismatch.csv` and this run is clean | The stale file is removed. A mismatch CSV on disk therefore always comes from a latest run that failed |
