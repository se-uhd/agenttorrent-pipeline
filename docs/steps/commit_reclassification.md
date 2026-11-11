# Commit reclassification from the per-repository commit records

## Context

The cloning step keeps the full commit log of each repository and a per-repository commit record
with the commits of each copied path. `commit-reclassification` applies the current heuristics to
this evidence and rebuilds:

- `output/all_ai_commits.csv` and `output/other_ai_commits.csv`
- the unmatched attributions and the message mentions
- `ai_commits-repos.csv`
- the configuration authorship values in `ai_authorship.csv`
- the `#commits` of `file_history.csv`, counted from the same path records

The step needs no new clone for this. [Dataset facts](../dataset.md) describe the current inputs
and their limitations.

## Key decisions

- **Language:** Python 3.14 (`agentcontext/steps/commit_reclassification.py`, the classification in
  `agentcontext/commits.py`), standard library only
- **One classification, two callers.** `commits.classify` is the classification loop. The cloning
  step calls it over the log that it has just written, and `commits.reclassify` calls it over the
  same commit log later, and both therefore classify a commit the same way.
  `tests/test_steps_commit_reclassification.py` checks that, under unchanged heuristics, the
  reclassification writes the same bytes into the shards as the cloning step wrote
- **The per-repository commit record contains a digest of the heuristics that classified its
  shards.** `classified_by` in the record is a digest of the columns of the two commit
  heuristics files that the classification reads (`commits.CLASSIFICATION_COLUMNS`), of
  `commits.py` and of `heuristics.py`, which loads the files. A repository classified under the
  current digest keeps its shards only after the step has validated the stored evidence, the shard
  checksums and the configuration authorship. The step writes shards that differ from the record
  again from the validated commit log
- **The AI columns come from the same log.** For each configuration artifact, the per-repository
  commit record keeps the hash of its creation commit and of every commit that touched it
  (`path_commits`), the commits that `ai_authorship_from` reads. Reclassification requires every
  referenced hash, including the creation commits, to be in the commit log, and an absent hash
  therefore cannot silently reduce the AI count
- **`#commits` is counted again from the same records.** The hashes of `path_commits` are every
  commit of the cloned branch that touched the configuration artifact under any of its names,
  the commits that `history.history_from` counts. After assembly, the step sets `#commits` in
  `file_history.csv` (`--file-history`) to that number for every row of a repository with
  validated evidence, whatever mechanism recorded the row, and leaves a blank count blank. A
  collection measured under an earlier definition that counted the current name only gets the
  current definition without a clone
- **A repository that cannot be classified again keeps its shard, and the step counts such repositories.**
  A repository has no commit log when its commit history was not read, was read by an image that
  kept no commit logs, or was read without a log that could be written. It keeps the
  classification that the cloning step gave it, and a later collection reads its history again
- **Only shards that the current heuristics classified from validated evidence reach the CSVs.**
  `left_out_shards` leaves the shards of these repositories on disk and out of the commit CSVs,
  the unmatched attributions, the mentions and `ai_authorship.csv`:
  - a repository with a commit history that was read and a `classified_by` other than the
    current digest, which the step could not classify again
  - a repository with evidence that failed validation (`failed`)

  Without this, a taken-over repository with a shard that last month's heuristics classified and
  a failed file history query would keep that shard. A row that names a heuristic id that has
  since been removed would then stop the run at the commit attribution validation. The evidence of
  a `failed` repository does not support its rows or its AI rows. The outcome of every such
  repository is one of the incomplete outcomes or `failed`, and the reclassification is therefore
  incomplete.

  The step removes from `ai_authorship.csv` the rows that these per-repository commit records
  hold, which the cloning step's assembly put there, and keeps a row with other values, which the
  file history step measured. A record without its AI rows (`ai_rows`), such as a record that
  does not parse, gives no values to compare. The rows of the repository's earlier, readable
  record are still in the file, and `context-files.csv` would publish them for a repository
  without published commits. The step therefore removes every row of such a repository, the rows
  that the file history step measured too, because only the record could tell the two kinds of
  rows apart (`drop_ai_rows`).

  `ai_commits-repos.csv` keeps the outcome, `commits_read` and `no_signal_shas` that each record
  gives, because the cloning step read the commit history. It records 0 in `commits_matched` and
  `commits_other_tools`, which count the repository's rows in the commit CSVs (`published_count`).
  With the record's counts, a reader of the CSV, such as the detection accuracy evaluation, would
  find matched commits that no published row has. A count that the record leaves blank stays
  blank. The report records `left_out` and the record's SHA-256, `record_sha256`, for each such
  repository. The detection step rebuilds the commit CSVs from the shards and leaves out the same
  shards while the record has that SHA-256. `snapshot write` counts these results of the
  published repositories under `left_out`, one count per outcome, and the release states each
  count in the evidence limit of its outcome
- **The step's fingerprint includes the two commit heuristics files and the `classified_by`
  digest.** The runner fingerprints the step by its modules, by `ai_commit_patterns.csv` and
  `other_ai_commit_patterns.csv`, and by `commits.CLASSIFIED_BY` (`runner.classified_by_input`).
  After an edit to either file, the runner therefore runs the step again and, through `consumes`,
  the collected file cleanup, detection and their consumers. The digest hashes the columns
  `id`, `tool`, `scope`, `attribution` and `pattern` of the rows of the two files and the code
  fingerprint of `commits.py` and `heuristics.py`, which leaves out
  comments, docstrings and log calls as the fingerprint of the modules does. After a change to a
  comment in either module, the runner runs nothing again, and after a change to their code, it
  runs the step. Without the digest in the fingerprint, the step would stay complete, and
  `snapshot write` would copy the old digest from the step's report. The release step would then
  fail on that digest in every later run. The collected file cleanup first rebuilds the validated
  tree that a previous run of the file history step may have pruned. The runner runs the step when
  it is given `--with-commit-reclassification`, right after collection and before the detection
  step restricts its CSV to the published repositories
- **The step takes its repositories from the collection.** The collected list and the list of
  clone failures together form the input list over which the cloning step assembled, and the
  step therefore needs no `--repo-csv`
- **Parallel over repositories.** The step runs one worker per CPU by default (`--workers`). A
  repository is one log and one shard, and the workers share nothing
- **The step exits 0 with what the cloning step accepts.** The runner stops every later stage on
  a non-zero exit. The cloning step exits 0 with repositories that it leaves to a later
  collection: up to `guards.clone_failure_ceiling` repositories to retry, and every commit
  history that the time bound or the size bound kept it from reading, which only a run under
  larger bounds reads again. If this step exited 2 for any incomplete repository, it would stop
  the run before detection, the release and the analysis after days of collection. The run would
  then stop at this step again after every later Start, because the cloning step is recorded as
  complete and does not read such a history again under the same bounds. The step therefore
  exits 0 when the assembly completes and the repositories with incomplete evidence, apart from
  those bounded histories, are within that ceiling, and it records `complete: false` in the
  report. It exits 2 above the ceiling, where the cloning step itself would have exited 4. The
  ceiling is the cloning step's default share of the requested repositories, not a
  `--max-clone-failures` that a run gave the cloning step. A repository with evidence that fails
  validation has the outcome `failed` and counts toward the ceiling as missing evidence does. The
  release names the incomplete outcomes among its evidence limits
- **The next collection collects a `failed` repository again.** The cloning step classifies the
  commits of a repository that it takes over again only when other commit heuristics classified
  them. A truncated commit log under the current heuristics would therefore stay `failed` in
  every later collection. The cloning step reads this step's report,
  `commit-reclassification.json` (`resume.RECLASSIFICATION_REPORT_NAME`) beside its
  `--ai-commits-csv`. It collects again every
  repository that the report lists as `failed` while the repository's per-repository commit
  record has the `record_sha256` that the report records (`resume.reclassification_failed`). A
  collection of the repository writes another record with another SHA-256, and the cloning step
  then no longer collects the repository again for this reason. A record that does not parse is
  `failed` too, and its `record_sha256` is the SHA-256 of its bytes

## Commit log

`collect_commits/<owner§repo>.log.xz` retains matched and unmatched commits. Commit logs in the
current format (`nul-v1`) separate the fields for the hash, the author and committer identities,
the author date, the subject, the body and the complete message with NUL bytes, and the record
and field separator bytes of the old format remain valid message content. The reader streams
records across buffer boundaries and rejects incomplete records. Compression uses xz's default
preset. The legacy `rs-us-v1` reader remains available for examining old logs, which lack the
scope provenance that complete reclassification requires.

`commit_evidence` in each per-repository commit record contains:

| Field | Meaning |
|---|---|
| `schema_version`, `log_format` | Evidence schema and field encoding |
| `source_url`, `retrieved_at`, `git_version` | Requested repository URL, UTC evidence retrieval date and Git client |
| `head_sha`, `scope`, `shallow` | Collected revision, `resolved_refs_and_head` scope and confirmation of full history |
| `refs` | Ref names with their object hashes and resolved commit hashes, including annotated tags |
| `log_sha256`, `commit_count` | Compressed log checksum and number of retained commits |

The cloning step resolves the refs before it runs `git log` and passes their commit hashes and
HEAD to it explicitly. A detached HEAD therefore remains covered. The scope covers the refs
present in the clone, not every branch on GitHub. For a shallow clone or a HEAD that differs from
the collected revision, the cloning step records `history_read_failed`.

`collected_artifacts` lists the copied paths whether or not their history queries succeeded.
Reclassification compares that list with `path_commits`, the AI-authorship rows and the collected
tree, which it lists through `paths.tree_files`, as the cloning step does. The listing leaves out
a `.DS_Store` and every file below an `@eaDir`, the directory that Synology DSM writes into a
directory of the collected tree that File Station visits on the NAS, because no record lists such
a file. Counting one of them would make a valid record `no_path_history`, and above the ceiling
the step would exit 2 in every later run. The cloning step writes a record only for a repository
from which it copied files, and the tree is therefore required. A repository with a record but no
tree keeps its shards and is `missing_tree`.

Before it replaces classifications, reclassification validates the checksum, the record count and
the presence of every ref, HEAD and path commit. For current shards, the authorship values must
also agree with the retained evidence. Validation uses no Git or network commands.

A shard can have a checksum that differs from `classified_outputs`, be missing or have another
header, such as a shard that a power loss cut short after the record reached the disk. The step
then writes the shard again from the validated commit log, and the repository is `reclassified`.
Raised as an error, the difference would make the repository `failed` at every reclassification
until a new collection, although the evidence from which the shards are derived is intact.

## Error handling & edge cases

| Case | Behavior |
|---|---|
| `collect_commits/`, the collected list or the list of clone failures is missing | `ERROR commit_reclassification:` naming it, exit 1 |
| A shard classified under the current heuristics | Evidence and derived authorship checked before reusing its shards |
| A repository with no commit log, regardless of classification digest | Keeps its shard, `no_log`, incomplete reclassification |
| A log without recorded scope provenance | Keeps its shard, `no_scope`, incomplete reclassification |
| Missing or incomplete path mappings | Keeps its shard, `no_path_history`, incomplete reclassification |
| A failed file-history query | Keeps its shard, `history_incomplete`, incomplete reclassification |
| One of the four cases above, or `missing_tree`, with a commit history that was read and a `classified_by` other than the current digest, or none | The shard stays on disk and out of the commit CSVs, the unmatched attributions, the mentions, `ai_authorship.csv` and the detection step's commit CSVs, with `left_out` and `record_sha256` in the report and a warning naming the count and the first repository |
| A repository without a fetched history | `history_not_read`, incomplete reclassification |
| A history that the time bound or the size bound stopped, or a reading of the commit history that git refused | `history_not_read` with `bounded`, incomplete reclassification, and outside the ceiling, because the cloning step reads it again only under larger bounds (`bounded_history`) |
| A collected repository missing its per-repository commit record | `missing_record`, incomplete reclassification |
| A per-repository commit record without its collected tree | Keeps its shards, `missing_tree`, a warning naming the count and the first tree, incomplete reclassification |
| Repositories with incomplete evidence, apart from the bounded histories, within `guards.clone_failure_ceiling` of the requested repositories | `complete: false` and exit 0, so that the steps after this one run |
| More such repositories than that ceiling | `complete: false`, a warning naming both numbers, exit 2 after the assembly |
| A recorded clone failure | `unavailable` with its reason, even when an older per-repository commit record remains beside it, and none of that record's commits reaches the commit CSVs. The repository is not among the repositories with a required commit history, and the population remains incomplete |
| A finished repository with an explicit zero copied-file count and no collected files or history rows, including empty configuration gitlinks | `not_applicable` with reason `no_files` |
| A log that cannot be read | `failed` with the error, a warning naming the repository, and the rest are classified and assembled. The repository's shards stay on disk and out of the CSVs and `ai_authorship.csv`, with `left_out`, and it counts toward the ceiling |
| A per-repository commit record that does not parse, such as a record that a power loss left empty | `failed` with the error, and `left_out` with the SHA-256 of the record's bytes, so that the next collection collects the repository again and `snapshot write` counts it among the repositories left out. `ai_commits-repos.csv` records `shard_unreadable`, and every row of the repository leaves `ai_authorship.csv`. Without these fields, the repository would stay `failed` in every later collection |
| Invalid checksum, provenance, count or required commit coverage | `failed`, original classifications preserved on disk and left out of the CSVs and `ai_authorship.csv`, and the repository counts toward the ceiling |
| The assembly fails | `ERROR` with the traceback, exit 1 |
| A commit of the log with an author date that does not parse, such as `10000-01-01T00:30:00Z`, which git prints in UTC for a commit of the year 10000 | Classified as any other commit. The step parses no date, and the shard and the commit CSV therefore keep git's `commit_timestamp`, which the detection step publishes blank ([detection](detection.md)) |

The regression suite changes the `pattern`, the `tool`, the `attribution` and the `id` of a row of each file of the commit heuristics independently and checks that the classification digest changes, and it changes the `notes`, the `doc_url`, the `doc_verified` and the `evidence` of a row and checks that the digest stays the same. Omitting either file or one of these columns from the digest, hashing the bytes of the files, or replacing the digest with a constant, makes a test fail. The collection integration test also checks reclassification against a per-repository commit record written by the real collection worker.

The regression tests of the reclassification cover truncated logs, the preservation of the
outcomes of clone failures, the exclusion of shards outside the collection and the replacement of
stale unmatched attributions and mentions. A log larger than two read buffers must preserve every
commit hash, body and subject in both plain and compressed form.

## Reclassification coverage

`output/commit-reclassification.json` records the requested classification digest, the checksums
of both collection lists, the count of each outcome and every requested repository. `counts`
contains every outcome of `OUTCOMES`, 0 included, so that a reader of the report or of the
snapshot's section finds a count for each outcome. `outcome_counts` raises `ValueError` for an
outcome that `OUTCOMES` does not contain. `complete` requires validated evidence and current
classifications for every repository with a required commit history: no `failed` outcome and
none of the incomplete outcomes in `INCOMPLETE_OUTCOMES` (`no_log`, `no_scope`,
`no_path_history`, `history_incomplete`, `history_not_read`, `missing_record`, `missing_tree`).
`population_complete` also requires no `unavailable` outcome, that is, no recorded clone failure.
`coverage` computes both fields from the outcome counts, and `snapshot write` applies the same
function when it records the report. `bounded_histories` counts the repositories with `bounded`,
`left_to_retry` the other repositories with an incomplete outcome or `failed`, and
`retry_ceiling` is `guards.clone_failure_ceiling` of the requested repositories. A repository
with shards that the step left out has `left_out` and `record_sha256` beside its outcome. The step
exits 2 when `left_to_retry` exceeds `retry_ceiling`, 1 when an input is missing or the assembly
fails, and 0 otherwise, whatever `complete` records. Both completion fields remain false until
assembly succeeds, and an interrupted or failed assembly therefore cannot leave a complete report.

To decide the `no_files` outcome, the step reads, for each repository, the latest explicit
`measures.copied` value in `finished_repositories.jsonl` and the history rows of its line, and
lists its collected tree. A missing count cannot establish that no commit history was required. A
line without a count that repeats the row of the line before it keeps that line's count, because
the row still describes the collection that the count came from. The cloning step writes such a
line when a head query sets or clears `head_no_response`, before a retry, after a retry that the
pool refused, and when a failed retry keeps the earlier collection. Read as a missing count, a
short outage during the head queries would turn every repository without files into
`missing_record`, above the ceiling. The step skips the lines of the list that were only partly
written. It needs both collection CSVs, the collected list and the list of clone failures, to
account for the population.

The readable shards remain on disk for inspection when reclassification is incomplete. Read each
repository's outcome in `output/commit-reclassification.json`, and retry the collection or
history operation that [Recovery](#recovery) gives for it. Repairing file-history cache rows alone does not
recover a missing commit log. A fresh collection writes new logs and per-repository commit
records with its own retrieval dates and revisions.

Configuration flags and empty directories do not establish that a file was copied. In the
October 2–3 collection, the lines of `cryptorubic/rubic-app`, `netivism/neticrm` and
`qiscus/qiscus-sdk-android` in the list of finished repositories explicitly count zero copied
files beneath configuration gitlinks, and these repositories require no commit history. A
repository with collected files, with history rows, without a copy count or without a line in
the list of finished repositories gets `missing_record` rather than `no_files`.

## Recovery

Read `output/commit-reclassification.json` first. Its repository outcomes and errors identify the
evidence to repair. Its two completion fields describe the repositories with a required commit
history and the whole requested population separately.

| Outcome or error | Required action |
|---|---|
| `missing_tree` | Restore `data/collected/<owner§repo>/` from the original handover into the working copy, keeping the record and log beside it, and run `python3 -m agentcontext commit-reclassification` again. If the tree is lost, collect the repository again |
| `no_log`, `no_scope`, `missing_record` | Restore the matching original evidence if it exists. Otherwise collect new evidence with the current collector. Archived classified messages do not replace complete logs or scope provenance |
| `history_not_read` with `bounded` | Run `python3 -m agentcontext repository-cloning` with a larger `--clone-timeout` or `--max-clone-mb` than the `time_bound` or `size_bound` of the per-repository commit record. The cloning step then fetches and reads the history again. Then run `commit-reclassification` again |
| `history_incomplete`, `no_path_history` | Inspect failed path queries and copied paths. Retry recoverable collection history failures. Repairing `file_history.csv` alone does not regenerate the full log and its path mappings |
| `failed` with a checksum, revision, commit-count or required-commit error | Preserve the log and record, compare against the source archive and restore a verified pair for the same collected revision. If that evidence is unavailable, the next run of repository cloning collects the repository again while its record has the `record_sha256` that the report contains. Do not edit checksums or counts to make corrupt evidence pass |
| `unavailable` | Review the recorded clone failure and retry it when recoverable. These repositories remain outside the repositories with a required commit history and keep `population_complete` false. Exit 0 alone establishes neither complete coverage nor complete population coverage. Read `complete` and `population_complete` |
| Assembly or filesystem error | Correct the logged storage, permissions or input problem and run `commit-reclassification` again. Check the new report and every output. Completion remains false after a failed assembly |

When the cloning step resumes, it collects again every repository with a record in the current
format of a history that was read and no commit log, even when the classifications of the
repository were assembled. The cloning step exits 4 while required logs remain missing,
independent of its ceiling of clone failures. Correct the storage error, preserve the existing
evidence and resume the collection. A new collection of the repository records the actual
revision and retrieval date. An earlier record without scope provenance still requires restoring
the original evidence or collecting a new snapshot. Before you use the destructive `--fresh`
option, preserve the original collection and follow
[the clean-collection procedure](../deploy.md#a-clean-run), or collect into a separate project
directory. [The cloning step's resume rules](repository_cloning.md) give the details.

After you repair the evidence, run `python3 -m agentcontext commit-reclassification` and read
`output/commit-reclassification.json`. Exit 2 means that more evidence remains incomplete than the
cloning step accepts. Exit 0 with `complete: false` means that some evidence remains incomplete.
Exit 1 means that an input is missing or that the assembly failed. Readable original
classifications remain available for inspection but do not certify current heuristic coverage.
