# Collection caches

The collection writes `file_history.csv`, `ai_authorship.csv` and `submodules.csv` in the stage
directory, `data/collection/`. The detection step reads `file_history.csv` and `ai_authorship.csv`
without cloning, and the collected file cleanup reads `submodules.csv`. [The current development
dataset](dataset.md) names the handover that the tracked caches come from and states the evidence
limits of these inputs. None of the caches is reconstructed from the CSVs of the March
publication.

## File history

The key is `(repo_name, head_sha, mechanism, artifact)`, and the values are `created_at`,
`#commits`, `first_commit_sha` and `last_commit_sha`. The cloning step leaves `mechanism` blank,
because it measures a path before detection classifies it. Detection first takes the cloning
step's row of the path at the commit where that row is complete, then the row with its exact key
(`detection.preferred_facts`), and then a measurement of the path under another mechanism
(`for_artifact`). `for_artifact` returns nothing when the measurements of the path under
different mechanisms differ. The file history step removes a row that it measured for a mechanism
where the cloning step has a complete row of the same path at the same commit
(`file_history.without_superseded`).

Blank values in an entry mean that a step examined the path and found no history available. An
absent entry means that no step recorded a measurement. The file history step measures the paths
of incomplete entries again, except under `--offline`, which fetches nothing. The step must not
invent dates for the files expanded from a directory symlink, because Git tracks the path of the
symlink, not a separate history for every file below it.

`created_at` is in UTC, except where git printed a date that does not parse, such as
`9999-12-31T23:30:00-01:00` in the offset `-0100` for a commit of the year 10000. The entry then
keeps git's text (`repos.parse_and_convert_to_utc`) and counts as complete, so that no run clones
the repository for it again. The detection step and the file history step publish such a
`created_at` blank.

## AI authorship

The key is `(repo_name, head_sha, artifact)`, and the values are `first_commit_ai_created`,
`first_commit_ai_tool`, `#ai_commits` and `ai_commit_tools`. Detection joins them into the rows of
the context files. A blank value means that the evidence is unavailable, and `FALSE` in
`first_commit_ai_created` means that a completed measurement found no AI creation.

The cloning step measures the file history and the AI authorship in the same clone.
`commit-reclassification` can compute the AI authorship again from the commit logs and from the
commits of each copied path in the per-repository commit records. When a record lacks those
inputs, the reclassification is incomplete, and the AI rows of the record stay in the cache only
where the current commit heuristics made them. The reclassification removes from the cache the AI
rows of a record with other commit heuristics or none in `classified_by`, and of a record with
evidence that failed validation. A row with other values stays, because the file history step
measured it in a clone of its own ([commit reclassification](steps/commit_reclassification.md)).

## The rows that the two caches keep

`file_history.csv` and `ai_authorship.csv` are keyed by the head commit. When the head of a
repository has moved, a collection adds the rows at the new head beside the rows at the earlier
head. The file history step is the last step of a run that writes the two caches. It keeps the
rows at each `(repo_name, head_sha)` of the collected list or of the validated list and removes
every other row (`current_heads` and `retain_heads` in `agentcontext/steps/file_history.py`). No
later step and no later collection reads a removed row:

- The detection step joins the caches at the heads of the validated list, and the file history
  step fills the output CSVs at the same heads. After a run, every head of the validated list is
  also in the collected list. The file history step reads the validated list too, so that the rows
  that the detection step reads stay when the validated tree comes from another collection than
  the collected list beside it.
- The next collection takes a repository over only at the head that the collected list records
  for it (`agentcontext/reuse.py`), also where a failed clone kept the earlier collection. A
  repository with a head that moved is cloned again, and the clone measures its history at the
  new head.
- A repository that is in the collected list only, because the collected file cleanup left none
  of its files, keeps its rows. The next collection takes it over at the same head. When a
  heuristic then detects one of its files, detection reads the row that the cloning step
  measured.
- A repository that is in neither list has left the input list. The cloning step removes its
  collected files (`reuse.remove_left`). When the repository returns to an input list, the
  collection clones it again and measures its history again.

The tracked caches therefore grow with the sample and not with the number of collections, and
they stay under GitHub's limit on the size of a file. The file history step removes the rows right
after it loads the caches. When it first saves the caches, it writes them without those rows
through `replace_file`, also in a run that measures nothing, and a stop during the save leaves
either the earlier file or the new one. Between the cloning step and the file history step, the
caches hold the rows of both heads, and so does the handover of a run that stopped before the file
history step. When a list is absent or lacks `repo_name` or `head_sha`, the file history step
exits 1 before it changes a cache, because it cannot tell which rows a later step reads. It
defines `current_heads` and `retain_heads` itself rather than in `agentcontext/caches.py`, because
that module belongs to the collector's code (`reuse.COLLECTOR_MODULE`). A change to the code of
`caches.py` needs a version with its reach in `code_versions.COLLECTOR`, and the next collection
collects again the repositories that the change reaches
([recording a change of the collector](steps/repository_cloning.md#recording-a-change-of-the-collector)).

## Submodules

`submodules.csv` records `repo_name`, `path` and `submodule_repo` for each Git tree entry with
mode `160000`, one row per `(repo_name, path)`. The collection does not initialize submodules.
The collected file cleanup removes the files under a recorded gitlink, because the configuration
of the repository that holds the gitlink must not include files of the linked repository. A
collection replaces the rows of every repository that it collects again (`write_submodules`), and
a resumed collection restores the rows of the repositories that `finished_repositories.jsonl`
records. When git cannot list the committed files of a clone, the cloning step logs
`FILE_LIST_ERROR` and continues without the submodule rows, and the repository has no rows until
a later collection lists its files.

## Storage and tests

`agentcontext/caches.py` validates the CSV headers and loads typed keys and values. It saves a
cache through a temporary file beside the destination and an atomic rename, with the rows sorted
by key, so that two runs write the same file and you can review changes in Git. The cache tests
cover missing and malformed input, the isolation of keys, an ambiguous fallback and round trips.
The tests of the file history step cover the rows that the two caches keep. Checks over the
current data verify that the cached values cover the published history rows, without fixing the
number of paths or repositories.

`replace_file`, the function that saves a cache, also writes the per-repository commit records and
the record of the collection in progress. Outside the collection, it writes the index and the
pointer of the cache of GHS retrievals, the files that the sampling stage writes beside them, and
the outputs of the classification. It syncs the temporary file, `<name>.<8 random characters>.tmp`,
to disk before the rename, and the directory after the rename. A power loss therefore leaves the
previous file or the new one under the name, never an empty or truncated file. Before it writes,
`replace_file` removes the temporary files that a killed earlier call left for the same name, and no
temporary file of another name. Because the random part has a fixed length, a call for `o§a.json`
does not remove the temporary file of `o§a.json.json`, which another worker may be writing at the
same time.

`sync_tree` syncs a file that a writer has already renamed into place, or a directory with every
file and directory below it, such as the collected tree of a repository before the cloning step
writes the line that declares the repository finished. For a path that does not exist, it syncs
the directory that contained the path, so that the removal of the path reaches the disk.
`sync_file` syncs one file and raises when the data may not be on disk. No function here syncs a
whole file system. `syncfs(2)` waits for every write that is pending on the volume, and `sync(2)`
also for every other file system of the host, without end when one of them is a remote folder of
Synology DSM with its server offline ([the runner](runner.md)).

## Input and storage failures

| Error or condition | Meaning and recovery |
|---|---|
| `The header of ... names the columns ..., not the expected ...` | The header of the cache differs from its required columns. Keep the file. Restore a valid version of the cache from the handover of the same collection, or, in the checkout, from the commit that `git log -- data/collection/<cache>` lists. Adding the missing names to the header does not restore the missing measurements |
| `... has the key ... twice` | `file_history.csv` or `ai_authorship.csv` has two rows for the same complete key. Loading raises and chooses neither row. Before you delete one of them, compare both with the history of the path at that commit, as the per-repository commit record or a clone shows it |
| A cache file is absent | Loading starts with an empty cache. An empty cache is not the same as a population that was inspected successfully. Detection may lack measurements, and the file history step under `--offline` stops with an error when the cache lacks a row that it needs |
| Permission, storage or rename failure during saving | Saving fails. Until the rename of the temporary file, the previous file of the cache stays at its destination. Other output CSVs may already have changed, because the whole step is not one transaction |

Keep each cache with the collection at the head commits of its keys. To recover, restore the
caches from the handover of that collection, then run the failed step and every step after it
again with `python3 -m agentcontext run --from <step>`, such as `--from file_history`. Use [the
recovery of the file history step](steps/file_history.md#recovery) for missing file history, and
[commit reclassification](steps/commit_reclassification.md#recovery) for AI authorship that the
kept commit logs support. Do not delete all caches as a routine response to one malformed row.

A new entry of these caches requires a complete commit history and a verified checkout of the
collected commit. A failed checkout or a failed fetch of the history adds no entry to
`file_history.csv` or `ai_authorship.csv`.
