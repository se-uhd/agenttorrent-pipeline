# Compare two GHS retrievals

## Context

`3_compare_retrievals.py` is the third script of the sampling stage. It lists what changed between the retrieval that `../data/sampling/ghs_retrieval.json` names and the usable retrieval before it in the cache, and it compares the collected sample with the new retrieval. The refresh strategy (`docs/refresh.md`) is based on its counts of the repositories that entered and left between two windows and of the collected repositories with a HEAD commit that differs from their collected commit.

Because GHS keeps no history, a change is visible only between two retrievals. With the dates of the query fixed, a repository leaves a later GHS search result only when GHS's weekly cleanup deleted it or when a crawl after a push changed a value that the query filters on. The cleanup checks the rows not crawled for more than <!--s:ghs.cleanup_uncrawled_days-->35<!--/s--> days and deletes a repository that it finds deleted or private on GitHub (`CleanUpProjectsJob.java:51-103`, `GitRepoRepository.java:40`). A repository that GHS neither crawled nor deleted stays. When the window moves, repositories also enter by age and leave by inactivity.

## Key decisions

- **Language:** Python 3.14, in the single script `3_compare_retrievals.py`, with the standard library and `ghs_cache.py`
- The comparison matches a repository by GitHub's id where both retrievals have GitHub's responses, and otherwise by GHS's name or the current name, regardless of case. GHS keeps the row of an old name when it crawls a repository under a new name, and GitHub resolves both rows to one id. The comparison takes the row under the current name for the repository and keeps every name merged into that row (`aliases`). A rename therefore appears as a change of `name` rather than as a removal and an addition. The comparison first pairs every repository by GitHub's id and then matches the remaining repositories by name. If it matched in row order instead, a row that shares a name with an old row could take that old row from a later row with the same GitHub id. It never pairs two rows with different GitHub ids by name: a repository keeps its id when it is renamed, so a new id under an old name is another repository that took the name, and the comparison reports the old repository as removed and the new one as added. A name therefore matches only where one of the two rows has no GitHub id, in a retrieval without GitHub's responses or for a row that GitHub did not find or left `unresolved`
- The comparison reads the fields that the filter, the classification and the collection read: the name, the default branch and its HEAD commit, the day of the last push, the creation day, the archived, disabled, locked, fork, mirror and private flags, the license, the primary language, the stars, the commit, contributor, issue, pull request and watcher counts, the topics, the description and whether GitHub has the repository. Each field comes from GitHub's response where there is one and from GHS's row otherwise. GHS has no description and no mirror or private flag, so the comparison compares these fields only between two retrievals with GitHub's responses. It reads the description as the classification does (`1_classify_repos.py`): without its surrounding white space, and as an empty description where GitHub returns null. A removed description is therefore a change, and a description that differs only in the space around it is none. The responses of a retrieval queried before the GitHub query requested the description lack the field, and the comparison leaves the description of such a retrieval out. Whether GitHub has the repository is unknown for a retrieval without GitHub's responses and for a row that the GitHub query left `unresolved` (`query-github-spec.md`). The comparison compares such a repository on GHS's row and never records it as `gone_on_github`. It compares a license without its double quotes, because GitHub writes them and GHS does not. Of the <!--s:retrieval_2026_10_05.license_changed_verbatim-->7,850<!--/s--> license changes between March and October 2026 in a verbatim comparison, <!--s:retrieval_2026_10_05.license_changed_quotes_only-->6,472<!--/s--> were differences of the quotes alone. The comparison leaves out `updatedAt`, because GitHub changes it for every new star
- The comparison compares a name regardless of case, because GHS records many names in a different case from GitHub. Of the <!--s:retrieval_2026_10_05.name_differences-->26,692<!--/s--> name differences between the retrievals of March and October 2026, <!--s:retrieval_2026_10_05.name_differences_case_only-->25,175<!--/s--> were differences of case alone
- The comparison compares timestamps by day. GHS writes its times in UTC, as GitHub does, but with the hour on a 12-hour clock without AM/PM. A GHS time therefore falls on the same day as GitHub's time for the same event, and its hour is known only modulo 12
- Each addition and removal has a reason. An addition is `created_after_previous_window` when the repository was created on or after the previous window's `created_max`. Otherwise it is `revived_or_newly_matched`, which covers a new commit that brought the repository into the window, a first crawl by GHS and a change of a filtered value. A removal is `inactive_before_window` when its last commit in the previous retrieval falls before the new window's `committed_min`, and `deleted_or_no_longer_matching` otherwise
- The comparison compares the collected sample with the new retrieval by name. A collected repository is `moved` when GitHub's HEAD, or GHS's `lastCommitSHA` for a retrieval without GitHub's responses, differs from the collected `head_sha`. The other changes are `branch_changed`, `renamed`, `gone_on_github`, `absent_from_retrieval`, and `not_collected` for a row without a collected commit. `moved_share` is the share of collected repositories that are `moved`
- `caches.replace_file` writes the two CSVs and the summary, each through a temporary file that it syncs to disk before the rename, and it syncs the directory after the rename. After a power loss, the name holds the previous file or the new file, never an empty file
- The comparison writes the differences that it finds and exits 0. It exits 1 only when it cannot read its inputs. After a fallback, the pointer names the same retrieval as before, and the runner does not run the comparison again (`run-sampling-spec.md`). When `--old` names the retrieval that the pointer names, the comparison compares only the collection, because a retrieval compared with itself would show every repository and the window unchanged

## Script: `3_compare_retrievals.py`

### CLI interface

```
python3 3_compare_retrievals.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--cache-dir` | `../data/sampling/ghs_retrievals/` | The cache of retrievals |
| `--pointer` | `../data/sampling/ghs_retrieval.json` | The file that names the new retrieval |
| `--old` | the usable retrieval before the new one | The retrieval to compare against |
| `--collected` | `../data/collection/data/collected/repos.csv` | The CSV of collected repositories, skipped when absent |
| `--changes`, `--collection-changes`, `--summary` | `../data/sampling/ghs_changes.csv`, `../data/sampling/ghs_collection_changes.csv`, `../data/sampling/ghs_changes.json` | The outputs |

### Function structure

**`view(ghs, github)`**, **`views(cache_dir, row)`**: a repository as the comparison reads it

**`by_repository(records)`**, **`match(old, new)`**, **`differences(old, new)`**: the key, the matching and the fields that differ

**`compare_retrievals(old_row, old, new_row, new)`**, **`compare_collection(collected, new)`**: the two reports

**`write_csv(path, columns, rows)`**, **`main(argv)`**: the outputs and the run

### Output

`../data/sampling/ghs_changes.csv`: `repository`, `github_id`, `change` (`added`, `removed`, `changed`), `reason`, `fields`, `old`, `new` (the values of the changed fields as JSON). `../data/sampling/ghs_collection_changes.csv`: `repository`, `github_id`, `changes`, `collected_sha`, `current_sha`, `collected_branch`, `current_branch`, `current_name`, for every collected repository that is not unchanged. `../data/sampling/ghs_changes.json`: `new_retrieval`, `old_retrieval`, `window_moved`, `retrievals` (the counts, the reasons and the fields) and `collection` (the counts and `moved_share`).

`ghs_changes.json` contains the inputs of the reports: `new_retrieval`, `old_retrieval` and `collected_sha256`, the SHA-256 of the collected list. When the inputs of a run differ from the inputs of the reports on disk, the run first moves those reports to `ghs_changes.previous.csv`, `ghs_collection_changes.previous.csv` and `ghs_changes.previous.json`, and it logs the input that changed. The runner runs the comparison for each new retrieval and not after a collection (`run-sampling-spec.md`). The collection report therefore compares the new retrieval with the collection before it, which is the collection in the collected list when the sampling stage runs. The reports of the previous retrieval stay beside the new reports under the `.previous` names, and so do the reports that a run with `--from retrieval_comparison` replaces after a collection. A run over the same inputs replaces its own reports.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| The first retrieval of the cache | Nothing to compare it with. The collection is still compared |
| `--old` names the retrieval that the pointer names | Only the collection is compared, and `INFO retrieval_comparison: Retrieval ... is both the new and the old retrieval. Nothing to compare.` names the retrieval |
| No collected list | The collection is not compared, and `INFO retrieval_comparison: No CSV of collected repositories at ...` names the path |
| Reports on disk made from other inputs | Moved to `.previous` names, replacing the older previous reports, and the changed input named |
| A collected list without `repo_name`, `branch` or `head_sha` | `ERROR retrieval_comparison: Could not compare the retrievals (...).`, exit 1 |
| No pointer, or a cache that cannot be read | Exit 1 |

## Files

| File | Action |
|------|--------|
| `3_compare_retrievals.py` | Main script |
| `ghs_cache.py` | Imported |
| `../data/sampling/ghs_retrieval.json`, `../data/sampling/ghs_retrievals/` | Read |
| `../data/collection/data/collected/repos.csv` | Read when present |
| `../data/sampling/ghs_changes.csv`, `../data/sampling/ghs_collection_changes.csv`, `../data/sampling/ghs_changes.json` | Written |
| `../data/sampling/ghs_changes.previous.csv`, `../data/sampling/ghs_collection_changes.previous.csv`, `../data/sampling/ghs_changes.previous.json` | Written when the reports on disk were made from other inputs. The ignore rules of `../data/sampling/.gitignore` cover them, as they cover the reports |
| `test_compare_retrievals.py` | Tests |

## Usage

```bash
python3 3_compare_retrievals.py
python3 3_compare_retrievals.py --old 2026-03-16T180701Z
```

## Tests

See `test_compare_retrievals.py`, which uses the standard library's `unittest`. The suite writes two retrievals into a temporary cache: an earlier retrieval without GitHub's responses, as the imported GHS search result of March is, and a later retrieval with them. The tests check that a file is synced before its rename by recording each call of the real `os.fsync` and `os.replace` (`_script_loader.synced_writes`).

- **`TestTwoRetrievals`**: each repository is added, removed, changed or unchanged, with the reasons and the changed fields; a rename is matched rather than removed and added; GHS's 12-hour times are compared by day; a name in another case is not a change; a retrieval without a usable one before it is the first; each file is synced to disk before its rename and the directory after it; the same retrieval again is not compared
- **`TestTheMatch`**: GitHub's id is matched before a name; a name still matches a row without an id; two rows with different ids are not matched by name
- **`TestANameThatAnotherRepositoryTook`**: a repository that took the name of another between two retrievals with GitHub's responses is added, and the other is removed
- **`TestTheLicense`**: a license spelled with quotes is no change, another license is
- **`TestTheCommandLine`**: an abbreviated option is a usage error, because the step takes long options by their full names only
- **`TestTheBounds`**: the bound days fall on the side that GHS puts them; topics in another order are no change
- **`TestTheFieldsOfGitHubAlone`**: a new description, a new mirror flag and a new private flag are changes; a removed description is a change; a description that differs only in the space around it is no change; a response without the description is not compared on it
- **`TestTheViews`**: the row under GitHub's current name represents the repository
- **`TestTheCollectedSample`**: every collected repository is compared with the new retrieval, with the statuses `moved`, `branch_changed`, `gone_on_github`, `renamed`, `absent_from_retrieval` and `not_collected`; a collected list without the columns is an error; a retrieval without GitHub's responses gives no value for whether GitHub has the repository; a row that the GitHub query left unresolved is not gone; reports of other inputs are kept as the previous version; git ignores every report and its previous version

Run with:

```shell
python3 -m unittest test_compare_retrievals -v
```

## Verification

1. With the pointer naming the March retrieval and the `data/collected/repos.csv` of the collection of October 2–3, the summary counts <!--s:refresh_2026_10_05.moved_since_march-->33,001<!--/s--> of <!--s:collection_2026_10_02.repositories_with_head-->36,617<!--/s--> collected repositories as `moved` (<!--s:refresh_2026_10_05.moved_since_march_share-->90.1%<!--/s-->) and <!--s:refresh_2026_10_05.branch_changed_since_march-->986<!--/s--> as `branch_changed`. These are the counts measured by hand on 2026-10-05

## Known limitations

- Without GitHub's responses in the earlier retrieval, the comparison matches a repository renamed between the two retrievals only when GHS kept the old row in the new retrieval
- The reasons for additions and removals are inferred from the two windows and the values of the retrievals, not observed
