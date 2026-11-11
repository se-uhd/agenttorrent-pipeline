# The snapshot record

`output/snapshot.json` records the current collection, the population of the outputs and the
history coverage. The runner's `collection_summary` step writes it with `snapshot write` after
the detection step and the file history step, before the publication. `snapshot write` takes the
collection dates from the `scanned_at` column of the collected list rather than from an older
snapshot file copied into a handover. It removes the keys `note`, `reconstructed_from` and
`reconstructed_on` of the collection section, which describe an earlier input, and computes
`artifact_mtime_window`, the time bounds of the collected files that the collection scanned, again.

## Input provenance and verification

Every section has `derived_from` entries that list its inputs and the counts read from them. A
section with a missing input is not checked, because the input cannot show that the recorded data
agrees with it. The collection section reads the collected tree, the collected list, the
`clone_failed.csv` beside that list and the validated list. `clone_failed` counts the distinct
repositories that the failure CSV records, and `missing_head_sha` separately counts the
repositories without a collected commit hash. Neither missing metadata nor a tree kept from an
earlier run shows the outcome of a clone attempt. A missing failure CSV leaves the collection
section unchecked. The `configured` and metadata sections need the output CSVs.
`snapshot write --check --outputs-only` checks those sections in a Git checkout and does not check
the collected input.

The `collection_summary` step reads the current collection and the output CSVs and does not
compare the dataset with earlier publications. The `configured` section records the number of
configured repositories and the provenance of its input, `output/repos.csv`, which has one row for
each configured repository. The section is not named `sample`, because the sample is the
repositories that the classification labels as engineered projects (GLOSSARY.md), and the configured
repositories are only part of it. `snapshot write` removes the sections that no command writes, such
as a `sample` beside `configured` (`RETIRED_SECTIONS`), and keeps the sections of the other
commands, such as `validation`.

When `output/commit-reclassification.json` is present, the `commit_reclassification` section records
the report's classification digest, its outcome counts, `complete`, `left_out`, `repositories`, and
`population_complete` when the report has it. `left_out` counts, per outcome, the repositories in
`output/repos.csv` with a result that the report marks as `left_out`. Like the outcome counts, it
lists every outcome, including those with a count of 0. Reclassification excludes these
repositories' shards from the commit CSVs, and detection also excludes these repositories.
None of their commits is published. The outcome counts give the missing evidence and not
the missing commits, and the README therefore states in the evidence limit of each outcome how many
of its repositories have no published commits. A separate evidence limit would name these
repositories twice. A repository without a
row in `output/repos.csv` has no published commits, whatever its result. The README names such a
repository by its outcome, such as `missing_tree`, and `left_out` does not count it. Each repository
therefore appears in one evidence limit of the README.

`repositories` lists the repositories of each outcome that keeps evidence out of the release
(`NAMED_OUTCOMES`): the incomplete outcomes, `failed` and the clone failures (`unavailable`). Each
list is sorted by name and holds the repositories of its outcome that the outcome counts include.
The list of an outcome with a count of 0 is empty. `output/repos.csv` does not mark the configured
repositories that `repositories` lists. Instead, the evidence limit of each outcome in the release
README names all repositories that `repositories` lists for the outcome, so that a user can leave
these repositories out of a count of AI commits.

The report's `collection_sha256` must be the SHA-256 of the current collected list.
`snapshot write` counts the repository outcomes again with the reclassification step's own
function, `commit_reclassification.outcome_counts`, which returns every outcome with its count, 0
included. It applies the step's coverage function, `commit_reclassification.coverage`, to these
counts and records as incomplete every outcome that the step counts as incomplete. When the report
describes another collected list, when its summary disagrees with its outcomes, or when it records
a failed assembly of the commit CSV, `snapshot write` raises `InvalidReport`. It then logs a
message that names the report and what it contradicts, and exits 1 without changing the snapshot.
`snapshot write` derives every section before it writes any and writes all of them in one
replacement of the file.

The section describes the published dataset, and the release states its evidence limits from the
section. Once the report's summary agrees with all its outcomes, the outcome counts, `complete` and
`population_complete` count the repositories that `output/repos.csv` lists. They also count the
repositories in the sample that have no row because a gap in the collection keeps them out of the
release: the clone failures (`unavailable`), and the repositories with a per-repository commit
record and no directory in the collected tree (`missing_tree`), from which the detection step
reads no file. `left_out`, by contrast, counts only the repositories that `output/repos.csv`
lists. A repository that the collected file cleanup dropped has no row and no commit in the
release. If the section counted it, a limit of the README could name more repositories than the
release publishes. If the section left out a `missing_tree` repository in the same way, it would
record a complete reclassification, and the README would name no evidence limit for the
repository. Without `output/repos.csv`, the section is not checked.

`snapshot write` computes the collection counts that come from the collected trees, `archived`,
`flagged_without_a_directory` and `rows_without_files`, and `artifact_mtime_window` only from the
directories of the repositories that the collected list contains (`listed_trees`). When the
cloning step cannot remove the tree of a repository that left the input list
(`reuse.remove_left`), the tree stays until a later run removes it. That tree belongs to no
collection that the list describes, and the collected file cleanup does not copy it either.
Counted, it would raise `archived` and move the start of the window with the files of an earlier
collection.

`artifact_mtime_window` also leaves out the fetched files and markers that
`data/collection/fetched_files.csv` lists (`--fetched-csv`, `fetched_paths`). A fetched file was
copied into the collected tree of a working copy after the collection, and its time is the time
of the fetch rather than of a scan. Counted, the fetched files would move the end of the window
past the last scan, and `snapshot validation` would count the timestamps of the days of the fetch
as times of the collection. The collection section names the list in `derived_from` where it
exists. A collection without fetched files has no such list. A subsample run collects its own
tree in a checkout that tracks the list of the development dataset, and its collection summary
runs `--without-fetched-files`, which reads no list
([subsample run](../tools/mirror_run.md)).

The same counts keep their recorded values when `data/collected/` contains the lists but no
repository's tree, as in a checkout. `snapshot write` then logs them as not checked, as it logs
`validated` without the validated list. Counted against such a directory, no repository would be
archived and every flagged repository would have no directory.

The metadata section's `artifact_rows` counts the rows of every mechanism CSV, `other.csv`
included. Its `rows_without_commit_metadata` counts the rows with a blank `last_commit_sha`,
`created_at` or `head_commit_date` (`COMMIT_METADATA_COLUMNS`), but only in the CSVs of the named
mechanisms (`NAMED_MECHANISMS`). The release publishes these CSVs, states the count among the
evidence limits of its README and withholds `other.csv`. `last_commit_sha` is blank where the
history of the configuration artifact was not read. The detection step and the file history step
leave a date blank where git printed it in a form that does not parse, such as for a commit of the
year 10000. A CSV without one of the three columns is not checked, because every row would count.
The section keeps a key that a person wrote beside these counts, such as the note on what a count
means in the record of September 2026, and `snapshot write` rewrites only the counts.

The `planning_documents` section counts `output/planning-documents.csv` apart from the
configuration artifacts (`planning_counts`): its rows, its repositories, its rows per framework
and its rows with a blank in one of `COMMIT_METADATA_COLUMNS`. The release states the last count
among its evidence limits. Planning documents are not configuration artifacts, and neither
`artifact_rows` nor the `configured` section counts them. Without the CSV, or with a CSV that lacks a
column that the counts read, the section is not checked.

The `imports` section counts `output/imports.csv` the same way (`imports_counts`): its rows, its
repositories, and its rows per tool and per status of the target. Imports are not
configuration artifacts either. Without the CSV, the section is not checked and `--check` exits 1.

Each command that writes the snapshot replaces only its own sections and keeps the others
unchanged. It writes the file atomically, under an `flock` lock on a permanent file beside the
snapshot. The kernel releases the lock when a process exits. The command syncs the temporary file
before it renames the file over the snapshot, and syncs the directory after renaming it. If a
power loss came after the new name reached the disk and before the data did, the snapshot would
be empty, and `load` would raise `SnapshotError` for it at every later run. `--output-dir`
selects both the output CSVs that a check reads and their snapshot. A check of an external working
copy therefore never rewrites the repository's snapshot.

## Checking the snapshot against GitHub

`snapshot validation` checks whether GitHub still exposes the commits recorded in
`data/collected/repos.csv`. It queries the API rather than cloning repositories:

```shell
python3 -m agentcontext snapshot validation
```

For each repository, the command compares the recorded commit with the recorded branch, then with
the current default branch, and then with the commits of the configuration artifacts from the
mechanism CSVs. A comparison that returns `identical` or `ahead` shows that the commit is
reachable. The names of the mechanism CSVs come from `heuristics.MECHANISM_CSVS`. The recorded
`head_commit_date` keeps the committer's offset, while GitHub returns UTC. The date check and the
check of the collection window therefore compare instants rather than strings. The instant comes
from the steps' own conversion, `repos.utc_instant`. Git accepts an offset of 24 hours or more in
a commit and `datetime` rejects it as a time zone, but the conversion gives the commit's instant
for such an offset too. A value that does not parse, or that names an instant outside the years 1
to 9999, such as `9999-12-31T23:30:00-01:00` for a commit of the year 10000, matches no date.

| Outcome | Meaning |
|---|---|
| `OK` | Recorded commit remains reachable through the recorded branch |
| `RENAMED` | Recorded commit is reachable through the current default branch |
| `RESCUED_BY_FILE_SHA` | A recorded configuration commit remains reachable |
| `GONE` | None of the checked commits is reachable |
| `REPO_GONE` | The repository is no longer readable |
| `NO_SHA` | No collection commit was recorded |
| `ERROR` | Requests failed and reachability could not be determined |

The results go to `output/snapshot-validation.csv` and to the snapshot's `validation` section.
`ERROR` is no evidence that a repository disappeared. When a request fails, the command exits 1,
and a run of the command over the same result file checks every `ERROR` row again.
`MAX_ATTEMPTS` bounds the attempts per request. The bound is a ceiling rather than a tuned value:
No repository used all of its attempts in a run over the whole sample, which ended without an
`ERROR` row.

Before it makes requests, the command replaces the result file atomically and keeps the existing
results. It then appends and flushes each new result, and an interrupted run can resume from them.
To compute the summary again without repeating the API requests, run:

```shell
python3 -m agentcontext snapshot validation --summarize-only
```

With `--summarize-only`, the command compares the repositories of the result file with the
repositories in scope in the collected list, and exits 1 when the file has no result for one of
them. It keeps the date of the run that gathered the results. It derives the `incomplete` marker
from the result file, as it derives every other value. The marker is present exactly when the file
contains `ERROR` rows, which `--record-with-errors` records, and it counts those rows. The
reachability results describe
the inputs and the date of one validation run. The validation counts of earlier runs do not
describe the current sample.

`head_commit_date` is the committer date of the collected commit, and `scanned_at` records the
time of the collection. The two columns describe different events and must stay separate.

The GitHub access tokens come from `GITHUB_TOKENS` and `GITHUB_TOKEN`, and then from the
credentials file that `--credentials` names, by default `credentials.csv` at the repository root
(`agentcontext/credentials.py`). The command rotates through the GitHub access tokens and pauses a
rate-limited GitHub access token until its reset time. `github_api.rate_limit_wait` determines
whether a 403 or a 429 is a rate limit and how long it lasts, as it does for the other steps that
query GitHub ([credentials](../credentials.md)). With this function, a secondary rate limit that
only its message marks pauses the GitHub access token for a minute instead of ending the
repository's check as `ERROR`. The credentials file is never tracked.
