# The regression manifest and the comparison of two runs

`agentcontext/checks/regressions.py` provides `regressions check`, which compares `output/` with
`output/baseline.json`, and `regressions compare`, which lists every difference between two stage
directories.

## The regression manifest

`regressions check --write` records what `output/` contains, and `regressions check` then lists
every difference between that record and the current `output/`:

```shell
python3 -m agentcontext regressions check --write     # record the current output in the regression manifest
python3 -m agentcontext regressions check             # list the differences
```

`--baseline` defaults to `baseline.json` in `--output-dir`, the directory that the manifest
describes. With a fixed default path, a run over a scratch tree would compare against the
repository's tracked manifest, and `--write` would overwrite that manifest. `snapshot write` takes
its file from `--output-dir` for the same reason. `--write` replaces the manifest in one step:
When writing fails, the previous manifest stays and the command exits 1.

The manifest is `output/baseline.json`. Its keys are mechanisms rather than filenames. A file
renamed with identical contents therefore compares equal, and a configuration artifact counted
under `other` instead of `rules` is a difference. Each entry contains the file's SHA-256, its row
count, its distinct repository count, its columns, and the number of `TRUE` in every boolean
column. With these counts, a difference reads as "169 more repositories have a skill" rather than
"the file changed".

The manifest covers each mechanism CSV with its Git metadata columns,
`output/planning-documents.csv` under the key `planning_documents`, `output/imports.csv` under
the key `imports`, `output/repos.csv`, `excluded.csv` and `consistency-report.csv`. [Comparing two runs](#comparing-two-runs) explains why the commit CSVs, `snapshot.json` and `provenance.json` are not in the
manifest. The exit codes are those of the documentation watch, `python -m agentcontext docs`: 0
when the manifest matches, 2 when something differs, and 1 when the check could not run. A check that could not run cannot report a pass.

Before you run `regressions check --write`, inspect each difference in the outputs and trace it to
a named change. The regression manifest only detects changes. Whether a heuristic is correct
follows from repository evidence and from the documented loading behavior of the tools.

## Comparing two runs

`regressions compare` takes the stage directories of two runs, a reference run and a later run,
and lists every difference between them. For the files of `output/` that the manifest tracks, it
first logs the differences in the manifest's counts, as `regressions check` does. It then
compares every file row by row on its key and lists each added row, each removed row and each
changed column with its key. `KEYED_FILES` lists every file that a run writes and that a person
reads a result from, each with its key:

- the records of `output/` beside the mechanism CSVs, the commit CSVs, `ai_commits-repos.csv`,
  the unmatched attributions and mentions, the two CSVs of the commit attribution validation, the
  deletions check's `deletions.csv`, and `unmatched-paths.csv` and `unmatched-commits.csv`
- `file_history.csv`, `ai_authorship.csv`, `submodules.csv` and `deletion.csv`
- the lists at the roots of `data/collected/` and `data/validated/`
- the files that the release step builds, `release/repos.csv`, `commits.csv`, `other_commits.csv`,
  `context_files.csv` and `unmatched-attributions.csv`

The JSON records, `JSON_FILES`, are `output/snapshot.json`, `output/provenance.json`,
`output/commit-reclassification.json` and `release/sampling.json`. The command compares each leaf
of a document on its JSON Pointer. A finding lists the leaves that were added, removed or
changed, and gives both values of a changed leaf. An array counts as one value. For the JSON
records, `--ignore` takes a key and leaves out every leaf below a key of that name.

The command compares the JSON records, the records beside the mechanism CSVs and `release/`. The
comparison leaves out the following files, each for the reason given:

- a release file that the release step copies from `output/` or `heuristics/`, which differs
  between two runs exactly when its source does
- the release's README, with counts computed from the compared files, and its `CITATION.cff`,
  which the code writes
- `checksums.sha256` and the `.7z` archives, because their bytes record when their files were
  written, and the command compares the files that they contain
- the files that a tool writes outside a run: `identity-*.csv`, `repo-sizes.csv` and
  `baseline.json`

The manifest of `regressions check` covers only the CSVs that it lists. The repository tracks the
commit CSVs as archives and extracts them only where a check needs them, and the stated
statistics count them. `snapshot write --check --outputs-only` compares every derivable section of
`snapshot.json` with the outputs, and `tests/test_heuristics.py` checks `provenance.json` against
the heuristics and the mechanism CSVs.

```shell
python3 -m agentcontext regressions compare ~/subset/reference ~/subset/checkout/data/collection
python3 -m agentcontext regressions compare A B --ignore scanned_at --report report.txt
```

A key that appears on more than one row of a file is a difference of its own. The finding gives
the key and the run in which the file repeats it. The row comparison pairs only one row per key
and keeps the first. Without this finding, a second row of `file_history.csv` under an existing
key, with another `#commits`, would appear as a change of the row order or the line endings.

`--ignore` leaves a column out of the comparison. The default ignores nothing: A repeat run changes
`scanned_at` by design, but the list of columns that change on a repeat run is itself a result. A
file that changed only in an ignored column has no difference. For such a file, the command leaves
out the manifest finding "The contents of <key> changed with no change to any count" and the
counts of the ignored column, and the log notes
`Only the ignored column(s) <columns> changed in <file>, which is not a difference`. The file that
`--report` names contains only the differences.

Where the rows are equal and the bytes are not, the command compares the bytes, whether or not a
column is ignored. Each of the following is a difference: rows in another order, other line
endings, a field that no column names, and bytes that a CSV writer would not write for the file's
rows, such as a quote or a missing final newline. The exit codes are those of the other comparison
commands: 0 when nothing differs, 2 when something does, and 1 when a stage directory is missing
or a file cannot be read. The error gives the file that could not be read, also for a CSV that is
not UTF-8 (`compared`). Two runs that differ only in `scanned_at` exit 0 when `--ignore` names
`scanned_at` and the JSON keys that change with it.

The subsample run ignores only `scanned_at` and the JSON keys that change with it,
`scanned_at_window`, `artifact_mtime_window` and `collection_sha256`, as
`docs/tools/mirror_run.md` states. The command compares both commit CSVs by repository and commit
SHA, including their `matched_heuristics` values. Explain every change to the matched heuristics
and every added or removed commit. The command reads the list of mechanism CSVs from
`heuristics.MECHANISM_CSVS`. It compares `output/planning-documents.csv` by repository and path,
as it compares a mechanism CSV, and `output/imports.csv` by repository, tool, importing file at the
start of the chain, importing file, line and import (`import_detection.ROW_KEY`).
