# The collection stage's directory

This directory contains the files that the `agentcontext` package reads and writes when it runs the collection stage: the data trees, the caches and `output/`. It is under the data root, `data/`, beside the data of the other stages. The code is the package `agentcontext/` at the repository root, which `dev/setup.sh` installs into `.venv` with the distributions of `locks/development.txt` (below). The heuristics that the package applies are in `heuristics/`. `docs/` describes the design, with one document per module and `docs/architecture.md` for what spans modules. Every step, check and tool is a subcommand of `python -m agentcontext`, which `python -m agentcontext --help` lists, and each subcommand takes `--help` of its own.

| Path | What it is |
|---|---|
| `data/collected/` | The collected tree that the cloning step wrote, restored from the current handover. `data/collected/repos.csv` is its list, and `clone_failed.csv` lists the repositories that the cloning step could not clone |
| `data/validated/`, `data/excluded/` | The validated tree, which the collected file cleanup derives, with `data/validated/repos.csv`, and the empty `data/excluded/` directory that the collected file cleanup creates |
| `output/` | The output CSVs of the repository cloning, commit reclassification, detection, file history and output cross check steps, `snapshot.json`, the regression manifest `baseline.json`, `provenance.json`, `commit-reclassification.json`, `repo-sizes.csv` from `python -m agentcontext measure`, the three `identity-*.csv` files and the tracked forms of the large files. See `docs/steps/detection.md` and `docs/steps/release.md` |
| `file_history.csv`, `submodules.csv`, `ai_authorship.csv` | The three caches that a collection fills and a regeneration reads (`docs/caches.md`) |
| `finished_repositories.jsonl`, `deletion.csv`, `run_logs/`, `_scratch/` | The files that a run leaves, gitignored. `finished_repositories.jsonl` is the list of finished repositories, from which the cloning step resumes, with the cost of the clone, the history fetch and reading the commit history for each repository. `deletion.csv` is the deletion log of the collected file cleanup, `run_logs/` contains the pipeline execution state and logs, and `_scratch/` contains the clones |
| `collect_commits/`, `clone_logs/` | The per-repository commit records that the cloning step writes and `commit_reclassification` reads, and the per-repository clone logs. Gitignored, and in the handover of a completed run |
| `collection_heuristics/` | Every version of the path heuristics and of the import heuristics that a repository was collected under, as `<sha256>.json`. A later run of the cloning step compares these versions with the file lists to determine which repositories a change affects (`docs/steps/repository_cloning.md`). Gitignored, and in the handover of a completed run |
| `data/workflows/` | Every repository's `.github/workflows/*.yml`, which the cloning step copies beside the collected tree and no step reads. A workflow is configuration when it is one of the <!--s:heuristics.config_path_patterns.workflow_files:words-->six<!--/s--> workflow files matched by name, runs a documented agent action, is a GitHub Agentic Workflow on one of the <!--s:heuristics.config_path_patterns.workflow_engines:words-->seven<!--/s--> engines, or runs a tool's documented headless invocation in its shell. The cloning step also copies those workflows into the collected tree. The collected file cleanup deletes every other workflow, and the copy here keeps it for a person to read. Gitignored, and in the handover |
| `data/imported/` | The imported tree: every file that an import of a copied configuration artifact reaches, as `<owner§repo>/<path>`, which the cloning step copies beside the collected tree, even where the collected tree has the file too. The import detection step reads it with the validated tree and writes `output/imports.csv`, and the release packs the files that `repos_data.7z` lacks into `imported_files.7z` ([import detection](../../docs/steps/import_detection.md)). Gitignored, and in the handover |
| `data/file_lists/` | Every repository's file list at the collected commit, `<owner§repo>.ls-tree.xz`, which the cloning step writes beside the collected tree and no step reads. A path heuristic can then be counted over the whole sample, the repositories without configuration included, without cloning again. `docs/steps/repository_cloning.md` gives the format. Gitignored, and in the handover |
| `output/all_ai_commits-unmatched-<YYYY-MM-DD>.csv.7z` | The unmatched attributions of one collection that name a bot or a tool and no person, named for the day on which that collection started and limited to the <!--s:parameters.agentcontext.checks.unmatched.UNMATCHED_COLUMNS_READ:words-->four<!--/s--> columns that `unmatched commits` reads. The full list stays in the handover. The tracked file records the collection that started on 2026-11-04. It is tracked so that every row of `heuristics/not_ai_commits.csv` is checked against a collection in a clean checkout. After each collection, replace it with `python -m agentcontext unmatched reduce`, as `docs/checks/unmatched.md` describes |
| `deletion.csv.7z` | The deletion log of the collected file cleanup of the collection that [docs/dataset.md](../../docs/dataset.md) names, archived for review |
| `fetched_files.csv` | Every fetched file: a file, or the marker of a link, copied into `data/collected/` of the working copy after the collection, with the collected commit, the day of the fetch and the change that required it, a commit of the private repository `se-uhd/agenttorrent-pipeline-internal`, which no check resolves (`docs/dataset.md`). `snapshot write` leaves these files out of `artifact_mtime_window` (`docs/checks/snapshot.md`) |
| `collection_statistics.json` | The collection statistics file: the values of the stated statistics that only the handover's data root holds the data for, which `python -m agentcontext statistics --collection --archive` writes over the working copy, with the SHA-256 of the snapshot, of the code of each computation and of the handover (`docs/tools/statistics.md`) |
| `dev_subsample.csv` | The development subsample, selected by `python -m agentcontext subsample`. See `docs/tools/subsample.md` |
| `release/` | The publication, built by `python -m agentcontext release`. Gitignored |

## Prerequisites

Set up the development environment as the [macOS](../../docs/development.md#macos)
or [Linux](../../docs/development.md#linux-ubuntu) section of the development guide describes.
The [x86-64 development container](../../docs/development.md#linux-x86-64-development-container)
supports every stage and the complete verification suite.

- Python 3.14, the version that `mise.toml` names
- Git, the version that `mise.toml` pins, following the supported setup in [development.md](../../docs/development.md)
- `7zz`, to create the dataset archives
- `rsync`, the version that `mise.toml` pins (the cloning step copies a directory with it, through `agentcontext/paths.py`)
- `GITHUB_TOKEN` or a comma-separated `GITHUB_TOKENS`, set when the repository cloning or file history step is pending. The pre-checks try the distinct GitHub access tokens in order, and these steps use only the first GitHub access token that passes validation. Local Git tests run with `--no-github` and `GITHUB_TOKEN` unset.
- the `pandas` Python package, which the analysis stage uses and no script or test in this stage imports
- the `lingua-language-detector` Python package, which `agentcontext/steps/detection.py` uses to detect the natural language of every configuration artifact in a CSV that has the column. It is in `agentcontext/requirements.txt`, and without it the step records `n/a`

From the repository root, install the dependency lock `locks/development.txt`, which locks
`agentcontext/requirements.txt` with the requirements files of every other stage, and the package
into `.venv` ([Python environments](../../docs/development.md#python-environments)):

```shell
mise exec -- bash dev/setup.sh
```

## Development data

[docs/dataset.md](../../docs/dataset.md) names the current input, a handover, and identifies its
archive, the current outputs and the coverage of commit reclassification.
[The development procedure](../../docs/development.md#continuous-improvement-from-handovers)
describes how to use the handover to improve the pipeline's behavior, the heuristics, the tests
and the verification. `dev/setup.sh` extracts the current commit CSVs, the sampling frame and the
labels from the archives that git tracks, and it writes the list of the sample from the labels
([development](../../docs/development.md#macos)). The trees come from the handover.

`GLOSSARY.md` defines mechanism, configuration artifact, file and CSV row.
