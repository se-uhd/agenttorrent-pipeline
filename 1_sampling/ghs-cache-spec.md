# The cache of GHS retrievals

## Context

`ghs_cache.py` keeps every retrieval that the sampling stage took from SEART GitHub Search (GHS), and GitHub's response for each repository in it, as plain files under `../data/sampling/ghs_retrievals/`. A retrieval is the GHS search result for one query, taken at one time: the JSON file that GHS returns for the query, with the matching repositories under `items` and the query under `parameters`. GHS updates each repository's row in place when it crawls a push and keeps no earlier version. You can compare retrievals only if you keep both. When a new retrieval fails, the download falls back to a retrieval of the cache, and `3_compare_retrievals.py` compares a new retrieval with the previous one in the cache.

The download (`1_download_ghs.py`) writes a retrieval and the first columns of its index row. The GitHub query (`2_query_github.py`) writes GitHub's responses and the `github_` columns. The comparison and the filter read both. Every script reads the retrieval that the pointer, `../data/sampling/ghs_retrieval.json`, names.

## Key decisions

- **Language:** Python 3.14, in the single module `ghs_cache.py`, with the standard library and `agentcontext.logs`. The module reads the files into memory or streams them, without a database, as the three caches under `data/collection/` are read (`docs/caches.md`)
- The cache keeps two files per retrieval for good, each xz-compressed with one JSON object per line: `<id>.ghs.jsonl.xz` with GHS's rows and `<id>.github.jsonl.xz` with GitHub's responses. Every retrieval is committed to git, because the size of these files is no constraint for the git repository
- The id is the UTC time of the retrieval's start, such as `2026-03-16T180701Z`, which is unique per run, sortable and readable as a date. `check_id` rejects any other value, so that an id never names a path outside the cache
- The rows keep GHS's order, by ascending GHS id, and are not sorted by name. That order is deterministic, and a reader that depends on the input order reads the same order from the cache as from the download. The key of a repository is its lowercased `owner/name` (`name_key`), because GHS and GitHub find a repository by name regardless of case
- `normalize` sorts `metrics` by language, because GHS builds that list from a hash set (`GitRepoToDtoConverter.java:55-64` of GHS v1.17.1). Two GHS search results can otherwise list the languages of an unchanged repository in different orders
- The module writes each file whole under a temporary name, syncs it to disk and renames it, and then syncs the directory. `write_rows` writes the rows in this way and syncs each volume before its rename, and `caches.replace_file` writes the index and the pointer. Without the sync, a power loss after the rename had reached the disk but before the data had would leave an empty or truncated file under the name, and an index or a pointer that cannot be read would stop every later download. A failed write leaves the previous file and no temporary file. The module removes the previous file and its volumes only once the new file is in place, because a stop before the rename would otherwise leave neither file. For a file split into volumes, a stop between two renames leaves a mix of volumes, which fails the check against the SHA-256 in the index
- A file that would reach GitHub's <!--s:github.file_size_limit_mb-->100<!--/s--> MiB limit is stored as numbered volumes. `write_rows` splits a file of `VOLUME_BYTES` (<!--s:parameters.1_sampling.ghs_cache.VOLUME_BYTES.mb-->95<!--/s--> MB) or more into `<name>.001`, `<name>.002` and onward. `read_rows` reads the volumes back as one stream and rejects them when the numbering has a gap. The March retrieval is <!--s:release_2026_03.ghs_file_mb-->42<!--/s--> MB at <!--s:release_2026_03.ghs_repositories-->187,304<!--/s--> rows, and a single file therefore holds about <!--s:release_2026_03.single_file_capacity_rows-->421<!--/s--> thousand such rows before it is split
- The index has a checksum of every file, the SHA-256 of its compressed bytes. When the caller passes the checksum, `read_rows` rejects a file that changed after it was written, before it reads the first row
- The pointer contains only the id, `{"retrieval_id": "..."}`. A run that falls back to the retrieval that the pointer already names therefore writes the same bytes, and the steps that the runner records by the pointer's contents stay complete
- `import` adds a GHS search result that the earlier download wrote to the cache. The GHS search result of March, `ghs_complete.json` with its `_metadata`, is the first entry of the cache, `2026-03-16T180701Z`, with the outcome `imported` and, in `imported_from`, the SHA-256 of the file that it came from

## Module: `ghs_cache.py`

### CLI interface

```
python3 ghs_cache.py import SEARCH_RESULT --end-date YYYY-MM-DD [--cache-dir DIR]
```

| Flag | Default | Description |
|------|---------|-------------|
| `SEARCH_RESULT` | *(required)* | A GHS search result that the earlier download wrote, with `_metadata.downloaded_at` and an `items` list |
| `--end-date` | *(required)* | The end date of the sampling window of the GHS search result |
| `--cache-dir` | `../data/sampling/ghs_retrievals/` | The cache |

### Layout

| Path under `../data/sampling/ghs_retrievals/` | Written by | Contains |
|------|------|-------------|
| `index.csv` | every writer | One row per retrieval attempt, keyed by `retrieval_id`, sorted by it |
| `<id>.ghs.jsonl.xz` | the download, the import | GHS's rows |
| `<id>.github.jsonl.xz` | the GitHub query | GitHub's response for each GHS row, in the same order |
| `<id>.github.partial.jsonl` | the GitHub query | The responses of a query that has not finished, not tracked |

### The index

The columns of `INDEX_COLUMNS`, with each value that is a list or an object written as JSON:

| Column | Contains |
|------|------|
| `retrieval_id`, `outcome` | The id, and `retrieved`, `failed` or `imported` |
| `started_at`, `finished_at` | UTC, ISO 8601 with a `Z`, to the second |
| `end_date_source`, `end_date`, `created_max`, `committed_min` | The window: where the end date came from (`date`, `crawl_date:<language>`, `imported`), the end date and the two bounds |
| `query`, `server_parameters` | The parameters sent, and the `parameters` block that GHS echoed |
| `ghs_version`, `crawl_dates` | GHS's version and build time, and the crawl date of each language. `crawl_dates` is empty for a retrieval with a date as its end date, which requests no crawl date |
| `count_before`, `count_after`, `item_count` | The search count before and after the download, and the number of rows written. On a `failed` row for a GHS search result that the download's size check refused, `item_count` is the number of rows in that GHS search result, none of which were written |
| `bytes_received`, `body_sha256`, `ghs_file_sha256` | The size of the compressed stream, the SHA-256 of the decompressed body, and the SHA-256 of the cache file |
| `attempts`, `error`, `fallback`, `imported_from` | Every attempt with its error, the reason of a failure, the retrieval used instead, and the file that an import came from |
| `github_started_at`, `github_finished_at`, `github_rows`, `github_found`, `github_not_found`, `github_unresolved`, `github_requests`, `github_cost`, `github_file_sha256` | The GitHub query: when it ran, how many rows it covered, how many repositories GitHub found and how many it did not find or blocks, how many rows the query left unresolved, the requests and GraphQL points spent, and the checksum of the file of responses |

### Function structure

**`retrieval_id(moment)`**, **`check_id(value)`**, **`utc_now()`**, **`iso(moment)`**, **`name_key(name)`**, **`license_key(name)`**, **`normalize(item)`**: ids, times and keys

**`rows_path(cache_dir, rid, kind)`**, **`volumes(path)`**, **`has_rows(...)`**, **`file_sha256(...)`**: where a file is and what it contains

**`write_rows(cache_dir, rid, kind, rows)`**: writes the rows, returns their count and the file's SHA-256

**`read_rows(cache_dir, rid, kind, expected_sha256="")`**: the rows, streamed, with the checksum checked first when it is given

**`read_index(cache_dir)`**, **`write_index(rows, cache_dir)`**, **`put_index_row(values, cache_dir)`**, **`index_row(rid, cache_dir)`**: the index, read and validated, written whole, and one row added or updated

**`usable(row)`**, **`previous_retrieval(rid, cache_dir)`**, **`latest_retrieval(cache_dir)`**: the retrievals that a reader can use, retrieved or imported

**`read_pointer(path)`**, **`write_pointer(rid, path)`**, **`pointed_retrieval(path, cache_dir)`**: the pointer

**`import_search_result(search_result, end_date, cache_dir)`**, **`main(argv)`**: the import

## Error handling & edge cases

Every failure raises `CacheError` with the reason, and the scripts log it and exit 1.

| Scenario | Behavior |
|----------|-----------|
| An id that is not a UTC time of this form | Rejected before a path is built |
| `index.csv` with other columns, a second row for one id, or an unknown outcome | Rejected, naming the line |
| A file with a checksum that differs from the index | Rejected before the first row |
| Volumes with a gap in their numbering | Rejected |
| A line that is not JSON | Rejected, naming the line |
| A write that fails part way | The previous file stays, and no temporary file is left |
| A power loss during a write | The previous file or the new one under the name, because the data is synced before the rename and the directory after it |
| No pointer | `pointed_retrieval` raises `CacheError` with the instruction to run the download first |
| A GHS search result imported twice | Rejected |

## Files

| File | Action |
|------|--------|
| `ghs_cache.py` | The module |
| `../data/sampling/ghs_retrievals/` | Read and written: the cache, tracked |
| `../data/sampling/ghs_retrieval.json` | Read and written: the pointer, not tracked |
| `test_ghs_cache.py` | Tests |

## Usage

```bash
python3 ghs_cache.py import ghs_complete.json --end-date 2026-03-16
```

## Tests

See `test_ghs_cache.py`, which uses the standard library's `unittest`. Every test works in a temporary directory. The tests check that a file is synced before its rename by recording each call of the real `os.fsync` and `os.replace` (`_script_loader.synced_writes`).

- **`TestIds`**: an id is the UTC start to the second; anything else is rejected; a path cannot leave the cache
- **`TestNormalize`**: metrics sorted by language; the key ignores case
- **`TestRows`**: rows round-trip in the order written; a file over the volume size is split and read back whole; a small rewrite leaves no volume behind; a changed file is rejected against its checksum; a gap in the volumes is rejected; a failed write leaves the previous file; the error for missing rows names their kind; a write stopped before its rename leaves the previous file whole, single or in volumes; the rows are synced to disk before the rename, each volume before its own, and the directory after the renames
- **`TestIndex`**: rows added, updated and kept in id order; the previous and latest retrievals skip failures; another header, a second row and an unknown outcome or column are rejected; the index is synced to disk before the rename and the directory after it; no index is an empty cache
- **`TestPointer`**: the pointer contains the id alone and is written with the same bytes again; the pointer is synced to disk before the rename and the directory after it; a missing pointer is None and a bad one is rejected
- **`TestImport`**: an earlier GHS search result becomes an imported retrieval with its window, query and source; a GHS search result is not imported twice; a bad end date on the command line is an error, exit 1

Run with:

```shell
python3 -m unittest test_ghs_cache -v
```

## Verification

1. `read_index()` over the tracked cache returns the March retrieval, `2026-03-16T180701Z`, with <!--s:release_2026_03.ghs_repositories-->187,304<!--/s--> rows
2. `read_rows(CACHE_DIR, "2026-03-16T180701Z", "ghs", row["ghs_file_sha256"])` yields <!--s:release_2026_03.ghs_repositories-->187,304<!--/s--> rows in ascending GHS id

## Known limitations

- The March retrieval has no GitHub responses. A query made now would return GitHub's values of later in 2026, which differ from the March values
- The cache contains GHS's rows as GHS wrote them, with every timestamp on a 12-hour clock without AM/PM (`JacksonConfig.java:28`). GitHub's responses have the exact times
