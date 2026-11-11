# Run the sampling stage

## Context

`run_sampling.py` passes the four sampling scripts to `agentcontext/runner.py` as an ordered list of steps. They take the GHS retrieval, query GitHub, compare retrievals, and filter the result into the sampling frame. `run_pipeline.py` at the repository root starts the full pipeline with the same steps. The analysis stage renders the paper's table of the filter steps in a step of its own (`../4_data-analysis/dataset/filter-steps-table-spec.md`).

A retrieval cannot be reproduced, because GHS updates a repository whenever it crawls a push of that repository. A retrieval taken again differs from the first. The cache of retrievals keeps every retrieval (`ghs-cache-spec.md`), and the pointer, `../data/sampling/ghs_retrieval.json`, names the retrieval that the stage reads. At the start of a run, the runner records the download as complete when the pointer is on disk, and `--from ghs_download` takes a new retrieval. The sampling window moves with every collection, and `docs/refresh.md` describes when a run takes a new retrieval.

## Key decisions

- **Language:** Python 3.14, in the single script `run_sampling.py`, with the standard library and `agentcontext.runner`, imported from the installed package
- The script defines only the list of steps. `agentcontext/runner.py` provides the lock, the pipeline execution state, the resume rules, the pre-checks and the stop handling, and `test_runner` tests them. The suite of this script tests the list
- The scripts are in this directory and their data in `../data/sampling/`. Each step names this directory as its `code` and `sampling` as its `root` under the data root. The runner therefore runs each step in the data directory and starts its script by its full path. The paths in this document are relative to this directory. No step takes an argument, because each script defaults to its configuration beside it and to its data under the data root
- `share_top_languages` passes a step argument `--top-languages N` of the filter to the download as well. The download checks over the rows of the GHS search result whether the filter would keep a language that the end date left out (`download-ghs-spec.md`). Without N, the download would count the config's number of languages while the filter keeps N. `share_top_languages` parses the words of the step argument as the filter's argparse parses them. The last value counts, and so does `--top-languages=N`. An abbreviation such as `--top N` does not count, because the filter takes long options by their full names only (`agentcontext.cli.ArgumentParser`). `share_top_languages` passes on neither a value that is not a whole number nor an abbreviation, and the filter rejects either. `run_pipeline.py` passes the number in the same way
- The download is irreproducible, and the runner records it by its output, the pointer. The step `produces` the pointer and declares `../data/sampling/ghs_retrieval_report.json` as its report. `agentcontext/runner.py` copies the report into the step's entry of `../data/sampling/run_logs/state.json`. The report contains the outcome `retrieved`, or `fallback` with the reason and `window_moved` set to false
- The runner records the GitHub query, the comparison and the filter by the retrieval that they read: the contents of the pointer, and `ghs_cache.py`, which each of them imports, as a `code_inputs` entry beside the script. None of the three consumes the download. A new retrieval changes the pointer. `agentcontext/runner.py` checks such a step's inputs again just before it skips the step, and the runner therefore runs all three in the same run as the download. A fallback writes the same bytes into the pointer. The runner then runs none of the three, because the stage continues over the previous sampling frame. The filter is also recorded by its config, the reviewed licenses and the tracked SPDX list, and the runner runs the filter again after an edit of any of them
- The runner also records the three steps by the files of the cache that they read. The query writes GitHub's responses into the cache under a name that the retrieval gives them, which no `produces` entry can name, and it leaves the pointer unchanged. The query and the filter therefore name `pointed_rows` as their `inputs_of`, and the comparison names `compared_rows`, which starts with the same entry. The entry is a digest of the checksums that the index has for the retrieval's rows and GitHub's responses, and it is absent while either file is not in the cache. When the query writes new responses, the runner runs the comparison and the filter again in the same run. When the query writes the same responses, the runner runs neither again. When the responses are gone, the runner runs the query again. With the pointer as their only record of the retrieval, the comparison and the filter would be skipped after the query wrote new responses, and the query would be skipped when its responses were gone. No `consumes` entry joins the comparison and the filter to the query. Such an entry would make the runner run both, and every step after them, whenever the query ran, even when the query found its responses in place
- `compared_rows` also records the comparison by the rows and GitHub's responses of the usable retrieval before the pointed one. Without such a retrieval, the entry is `absent`, a value of its own, because an earlier retrieval that appears later changes what the comparison writes
- The comparison also reads the collected list, and the runner leaves the list out of the comparison's record on purpose. The refresh strategy reads the comparison of each new retrieval with the collection before it (`../docs/refresh.md`). A monthly run makes that comparison, because its new retrieval changes the pointer and the sampling stage runs before the cloning step, while the list on disk is still that of the previous collection. The cloning step rewrites the list later in the same run. If the list were part of the record, the runner would run the comparison again at the first Start after every completed run. The comparison would then compare the retrieval with the run's own collection and move its files on the previous collection to the `.previous` names. Because that Start ran a step, the entrypoint would also pack another handover of the completed run. A record of the list as it was when the comparison ran would differ from the list on disk after the cloning step in the same way. After a run that falls back to the previous retrieval, the comparison's files on disk stay as they are. `--from retrieval_comparison` compares the retrieval with the collected list on disk
- The GitHub query needs a GitHub access token. Its `env` entry names `GITHUB_TOKENS` and `GITHUB_TOKEN` as alternatives, and the pre-checks also read a row of `credentials.csv` (`docs/credentials.md`)
- Each step declares what it needs. The download's interpreter has to import `requests` and `yaml`, the query's `requests`, and the filter's `requests` and `yaml`. The download reads its config. The filter reads its config, the reviewed licenses and the tracked SPDX list, `config/spdx_licenses.json`, which it never downloads, and a missing SPDX list stops the filter. Before the first pending step starts, the pre-checks verify the imports and the pointer for the pending steps, and the <!--s:parameters.1_sampling.run_sampling.MIN_FREE_DISK_GB:words-->two<!--/s-->-gigabyte minimum of free disk space

## Script: `run_sampling.py`

### CLI interface

| Flag | Default | Description |
|-------|---------|-------------|
| `--list` | off | Print the stage with its four steps and exit |
| `--from NAME` | *(none)* | Run the step again with every step computed from it, or every step of the stage with `sampling`. With `ghs_download`, take a new retrieval. Repeatable |
| `--step-arg NAME:ARGS` | *(none)*, repeatable | Append arguments to one step, for example `repository_filtering:"--top-languages 5"`, which reaches the download as well |
| `--mark-complete STEP` | *(none)* | Record STEP as complete from its output on disk, without running it. The runner rejects it together with `--list`, `--from` or `--step-arg`, which it would ignore |

### Step list

Every step names `sampling` as its stage.

| Step | Script | Produces | Recorded by | Needs |
|-------|--------|----------|-------------|-------|
| `ghs_download` | `1_download_ghs.py` | `../data/sampling/ghs_retrieval.json`; reports `../data/sampling/ghs_retrieval_report.json` | its output, adopted from disk | `requests`, `yaml`; the config, the previous sampling frame where it is on disk, for the languages that it kept, and, when the end date leaves a language out, `4_filter_ghs.py` with the filter's config, `config/licenses.csv` and `config/spdx_licenses.json` |
| `github_query` | `2_query_github.py` | GitHub's responses in the cache | the pointer, `ghs_cache.py`, and the retrieval's rows and GitHub's responses in the cache | `requests`; `GITHUB_TOKENS`, `GITHUB_TOKEN` or `credentials.csv` |
| `retrieval_comparison` | `3_compare_retrievals.py` | `../data/sampling/ghs_changes.csv`, `../data/sampling/ghs_collection_changes.csv`, `../data/sampling/ghs_changes.json` | the pointer, `ghs_cache.py`, the retrieval's rows and GitHub's responses, and the rows and GitHub's responses of the retrieval before it | n/a |
| `repository_filtering` | `4_filter_ghs.py` | `../data/sampling/ghs_filtered.json`, `../data/sampling/ghs_licenses.csv` | the pointer, `ghs_cache.py`, the retrieval's rows and GitHub's responses, the config, the reviewed licenses and the SPDX list | `requests`, `yaml`; the config, `config/licenses.csv` and `config/spdx_licenses.json` |

Every step runs in `../data/sampling/` under the step interpreter of `agentcontext/runner.py`, which is `DATA_COLLECTION_PYTHON` when that variable is set, else the interpreter that runs the runner.

### Function structure

**`pointed_rows(step)`**: `[(POINTED_ROWS, digest)]`, where the digest is the JSON of the `ghs_file_sha256` and `github_file_sha256` that the index has for the retrieval that the pointer names. The digest is `None` when the pointer, its index row or either file is absent. An `OSError` while the index or the pointer is read raises `runner.UnreadableOutput`

**`compared_rows(step)`**: `pointed_rows`, then `(PREVIOUS_ROWS, ...)` for the usable retrieval before it, `absent` when there is none

**`share_top_languages(steps)`**: the steps, with the filter's `--top-languages` from a step argument appended to the download's arguments

**`build_parser()`**, **`main(argv=None)`**: the flags, and the call of `runner.run` with the list, the stage's `run_logs/` under the data root and the <!--s:parameters.1_sampling.run_sampling.MIN_FREE_DISK_GB:words-->two<!--/s-->-gigabyte minimum

### Output

`../data/sampling/run_logs/state.json`, with `completed` keyed by step, the run's provenance and, in the download's entry of `steps`, its report. The runner also writes the logs `../data/sampling/run_logs/<step>.log` and the run lock `../data/sampling/run_logs/run.lock`.

## Error handling & edge cases

`agentcontext/runner.py` handles the errors (`../docs/runner.md`). For this step list, the runner records the download as complete when the pointer is on disk and logs `INFO runner: ghs_download is irreproducible, and its output is on disk`. A download that falls back exits 0, and its report contains the fallback. The pointer then has the same bytes, and the steps after the download stay recorded as complete. When the GitHub query is pending and neither variable nor `credentials.csv` contains a GitHub access token, the pre-checks stop the run before the download starts. Without `config/ghs_download.yaml`, the download exits 1. The download also exits 1 when it cannot write a file on this host, such as on a full disk, and the run stops rather than falling back.

## Files

| File | Action |
|------|--------|
| `run_sampling.py` | Main script |
| `../agentcontext/runner.py` | Imported: `Step`, the completion rules and `run` |
| `1_download_ghs.py`, `2_query_github.py`, `3_compare_retrievals.py`, `4_filter_ghs.py` | Executed as steps |
| `config/ghs_download.yaml`, `config/ghs_filter.yaml`, `config/licenses.csv` | Read by the steps |
| `config/spdx_licenses.json` | The SPDX list that the filter reads, a code input of its step. `4_filter_ghs.py --refresh-spdx` replaces it before a NAS run |
| `../data/sampling/run_logs/` | Written, not tracked: the pipeline execution state, the run lock and the logs |
| `test_run_sampling.py` | Tests |

## Usage

```bash
python3 run_sampling.py --list
python3 run_sampling.py
python3 run_sampling.py --from ghs_download
python3 run_sampling.py --from repository_filtering
```

## Tests

See `test_run_sampling.py`, which uses the standard library's `unittest`. The suite checks the list against the scripts and configs on disk. Its runs execute stand-ins with the columns of the real list: small scripts in a temporary directory, so that no test reads the real cache or the real configs or reaches the network. The suite runs without site packages, so the tests that compare the step of the filter with the filter's config, which they read with `yaml`, are in `test_filter_ghs.py` (`TestTheStepOfTheRunner`).

- **`TestStepList`**: four steps in pipeline order with their scripts here and their data apart, all named for the log; the download is irreproducible, recorded by the pointer and reports; no step takes an argument; the steps after it are recorded by the retrieval that they read, its rows and GitHub's responses, and the comparison by the retrieval before it, and none consumes another; the GitHub query needs one of the two variables for GitHub access tokens; what each step reads is on disk or written before it
- **`TestList`**: the list prints the four steps under the stage with their data directory and their scripts; the runner lists under an interpreter that imports no site packages, with the checkout on `PYTHONPATH`
- **`TestARun`**: a run records every step and the download's report; a retrieval already named is adopted rather than taken; a new retrieval redoes every step after the download in the same run; a fallback to the same retrieval redoes nothing after the download; a second run skips what is recorded; `--mark-complete` rejects a step argument that it would ignore; a step argument reaches the step; the filter's `--top-languages` reaches the download too
- **`TestWhatTheStepsAfterTheDownloadRead`**: over a real cache of retrievals, new responses change the fingerprints of the query, the comparison and the filter; responses that are gone leave the query without a fingerprint; an earlier retrieval changes the comparison's fingerprint alone, and a new collection changes no fingerprint; through the runner, a query that writes other responses redoes the comparison and the filter in the same run, and a query that writes the same responses redoes neither

Run with:

```shell
python3 -m unittest test_run_sampling -v
```

## Verification

1. `python3 run_sampling.py --list` prints `The stage sampling runs these steps in order.` and its four steps, each with its script under `1_sampling/` and `(data directory sampling)`
2. After a run, `../data/sampling/run_logs/state.json` contains the download's report in its entry of `steps`

## Known limitations

- A run that takes a new retrieval spends most of its time in the GitHub query. For the retrieval of 2026-10-05, the index of the cache records <!--s:retrieval_2026_10_05.download_step_minutes-->19<!--/s--> minutes for the download step and <!--s:retrieval_2026_10_05.index_query_minutes-->172<!--/s--> minutes for the GitHub query, from the start of its first run to the end of its last run. The steps after the query take minutes. A run that is stopped resumes the query from its partial responses
