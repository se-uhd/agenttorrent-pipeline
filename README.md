# AgentTorrent: A Dataset of Context and Harness Engineering Artifacts on GitHub

[![pipeline tests](https://github.com/se-uhd/agenttorrent-pipeline/actions/workflows/pipeline-tests.yml/badge.svg)](https://github.com/se-uhd/agenttorrent-pipeline/actions/workflows/pipeline-tests.yml)

This repository contains the pipeline that builds and analyzes AgentTorrent, a dataset of version-controlled coding agent context in GitHub repositories. It contains configuration artifacts for six tools (i.e., Claude Code, GitHub Copilot, OpenAI Codex, Cursor, Gemini, and OpenCode), their imported files, planning documents in the same repositories, and commits that credit the tools. The dataset was called AgentConfigs until 2026-10-19 and AgentContext until 2026-11-07. The Python package `agentcontext`, its command line `python -m agentcontext`, the container image and the names of the handovers keep the earlier name until the release after 2.0.0.

## Prerequisites

Use the versions of Python 3.14, uv, Git, 7-Zip, and rsync pinned in `mise.toml`.
`mise trust && mise install` installs them, and `mise exec` puts them first on `PATH`
([development setup](docs/development.md)).

- uv writes the dependency locks and installs them into `.venv`.
- Git is installed from conda-forge's package.
- 7-Zip (`7zz`) extracts and creates the dataset archives. mise installs it from the 7-Zip release.
- `rsync` copies files during collection.
- An OpenRouter API key for the classification stage, which calls its default model, DeepSeek V4.1 Flash, through OpenRouter, and GitHub access tokens for the sampling, classification and collection stages, in `credentials.csv` or the environment ([docs/credentials.md](docs/credentials.md))

Each stage lists its direct Python dependencies in `requirements.txt` files and its README. The dependency locks under `locks/` list every distribution with its exact version and hashes. CI, the development and production images, and the checkout's `.venv` install from these locks ([locks/0_README.md](locks/0_README.md)). mise creates `.venv` for each checkout.

For development, follow the [macOS](docs/development.md#macos),
[Linux](docs/development.md#linux-ubuntu) or
[x86-64 container](docs/development.md#linux-x86-64-development-container) instructions.
The development container includes the dependencies of every stage of the pipeline and
runs selected tests as well as the complete verification. Development also requires Poppler
(`pdftotext`), which a test of the analysis stage uses, and the checks of the website's JavaScript require Node.js.

Run the checks that follow a collection in a separate [working copy of its handover](docs/development.md#handover-working-copy),
the archive that the container packs into `handover/` when a run ends. Build production images
and NAS bundles as [the deployment instructions](docs/deploy.md) describe.

## Pipeline

The pipeline has five stages, each a sequence of steps that depends on the previous stage's outputs. Each stage has its own code directory. All inputs and outputs are under the data root, `data/`, in `sampling/`, `classification/`, `collection/`, and `analysis/`. Publication shares `data/collection/` and writes to `data/collection/release/`.

1. **Sampling** (`1_sampling/`): Takes a retrieval of the SEART GitHub Search dataset, queries GitHub for every repository in it, compares it with the previous retrieval, and filters it by license, primary language, activity and engagement. The download keeps every retrieval in `data/sampling/ghs_retrievals/`. The filter writes the sampling frame, `data/sampling/ghs_filtered.json`, and the repository tracks the sampling frame of retrieval `2026-11-02T223052Z` as `data/sampling/ghs_filtered.json.7z`. `run_sampling.py` runs the four steps end to end and records the provenance of the run.
2. **Classification** (`2_classification/`): Reads each repository's README through GitHub's README API and sends it with the description to a model, DeepSeek V4.1 Flash by default. The model gives a value and evidence for every exclusion criterion before judging the evidence of software engineering practices. Its initial input includes issue and pull request activity, pinned issues, GitHub metadata (e.g., topics, license, and template flag), and a file outline. It requests a summary of a page linked from the description, homepage, or README when that page could change a value that the input does not state outright or that rests on an inference. For a repository described as a fork in its description or README, the fork heuristic uses GitHub's data to test independence.

   Labels are cached in `data/classification/labels/` and reused while the README wording, description, model, and classification parameters are unchanged. Each run is compared with the previous run, with a cause recorded for each changed label. Outputs are `repos_annotated.csv` (labels and justifications), `label_changes.csv`, and `repos_annotated_included.csv` (the sample of repositories labeled engineered). When repositories labeled `true` share a head commit, the sample keeps one and leaves the others out of `repos_annotated.csv`. The development labels are tracked in `repos_annotated.7z`, which also contains `repos_excluded.csv`. That CSV lists repositories in the sampling frame omitted from `repos_annotated.csv`, with reasons such as `readme_non_english` for a README that is not in English or `copy` for a repository omitted in favor of another at the same head commit.
3. **Data collection** (`agentcontext/`, over `data/collection/`): Clones the repositories in the sample, detects and extracts the configuration artifacts of ten mechanisms, and reads the available commit history of the collected branch for commits that credit a tool. It adds git metadata to each configuration artifact, such as its creation date, its commit count and the SHAs of its first and last commits. It produces `output/repos.csv` and one CSV per mechanism, each row with the git metadata of its configuration artifact. It also produces `output/planning-documents.csv` with the planning documents of the configured repositories: the specs, plans and tasks that spec-driven development frameworks and the planning features of the tools write at a documented location. Planning documents are not configuration artifacts ([docs/heuristics.md](docs/heuristics.md#planning-documents)).
4. **Publication** (`agentcontext/`, into `data/collection/release/`): Builds the archive of collected files and the release that is uploaded to Zenodo.
5. **Data analysis** (`4_data-analysis/`): General dataset analyses (`rq2/`, `rq3/`), and tables and figures for each release and paper (`dataset/`), such as the table of the filter steps of the sampling stage.

`run_pipeline.py` at the root runs the five stages as one list of steps with one pipeline execution state. `--list` shows the steps under their stage, and `--from` takes a stage or a step. The sampling, the classification and the analysis have a runner each, `run_sampling.py`, `run_classification.py` and `run_analysis.py`, and `agentcontext/runner.py` runs the collection and the publication. Each runner defines its list of steps and uses the lock, the pipeline execution state and the other shared code of `agentcontext/runner.py`. `run-pipeline-spec.md`, the `0_README.md` of each stage and, for the collection stage, `docs/architecture.md` give detailed instructions. Progress messages use the name of each step, a noun phrase such as Repository cloning. The runner records completions and failures in its logs and its pipeline execution state and sends no push notifications. [GLOSSARY.md](GLOSSARY.md) defines the terms that the code, the documents and the commit messages use.

`agentcontext/` is the code of the collection stage as one package, installed editable by `dev/setup.sh`. Every step, check and tool of the collection is a subcommand of `python -m agentcontext`. `heuristics/` contains the detection heuristics, and `data/collection/` contains the files that the package reads and writes. `deploy/` packages the five stages as a Docker image for an unattended run, with a compose file for Synology's Container Manager and the runbook `docs/deploy.md`, and `deploy/0_README.md` describes the directory. The container mounts the data root and `handover/` read-write. It mounts `credentials.csv` and the NAS's `/etc/localtime`, at `/etc/host-localtime`, read-only.

## Testing

Run targeted tests while you implement a fix. When the fixes and their documentation are
complete, commit the batch, then run the full verification and review the committed batch. The
full verification must pass on the committed state before every push.
[The development instructions](docs/development.md) give the commands for each platform.
`dev/setup.sh` installs the dependencies and extracts the tracked inputs. `dev/verify.sh` runs
the full verification. On macOS, it compares the regenerated analysis outputs with the tracked
outputs, and on Linux, it compares two regenerations with each other.

The [current development dataset](docs/dataset.md) is the collection of the run of all five
stages that started on the NAS on 2026-11-03, over the sampling frame of its own retrieval and
the labels of its classification. The tracked outputs are that collection, regenerated with the
current code. [The build records](docs/validation/README.md) identify the images that ran the collection of the
development dataset and the October 5–6 collection, each with its full verification and its
subsample run, and they name the handovers of the earlier collections that the documents cite.
[The development procedure](docs/development.md#continuous-improvement-from-handovers)
explains how to inspect evidence, add regression cases and regenerate outputs.

Both Codex and Claude Code can work on the repository. `CLAUDE.md` contains the shared
instructions, and `AGENTS.md` tells Codex to read them. Neither tool needs project settings of
its own ([agent instruction loading](docs/development.md#codex-and-claude-code)).

The collection stage has a stdlib `unittest` suite under `tests/` for every module of the package, and a document under `docs/` for every module with a design decision. `docs/architecture.md` describes how the modules depend on one another. The tests build local git repositories and require no network access. GitHub Actions runs them on every push and pull request (`.github/workflows/pipeline-tests.yml`, on the Python 3.14 that `mise.toml` names). The workflow has no path filter, because a change elsewhere can break the tests: The tests read `config_path_patterns.csv` under `heuristics/`, and the analysis code reads the CSVs that the earlier stages write.

The four sampling scripts, `ghs_cache.py`, the stage runners, and `run_pipeline.py` also have suites and specs. Every script directly under `2_classification/` has a suite beside it, and every script of the analysis stage has a suite at the top of `4_data-analysis/`. `4_data-analysis/check_reproduction.py` runs every analysis script twice and fails when an output does not reproduce. The same workflow runs all of these suites and the reproduction check. The section [Tests and verification](CLAUDE.md#tests-and-verification) of `CLAUDE.md` gives the commands of the suites under `tests/`. The `0_README.md` of `1_sampling/`, `2_classification/`, `4_data-analysis/`, `deploy/` and `_website/` gives the commands of its suites, and `test_run_pipeline` runs from the root.

## Published dataset and website

- `agentcontext/steps/release.py` builds the publication into `data/collection/release/`. It contains `repos.csv` (the full sampling frame, with the classification and the configuration flags), the CSVs of the ten mechanisms, `planning-documents.csv`, `commits.csv`, and `imports.csv` with `imported_files.7z`, the files that the configuration artifacts import. It also contains the detection and import heuristics, the heuristics of the planning documents, the negatives of the heuristics, the record of the documentation watch, the provenance records, the records of the collection (e.g., the deletion log and the unmatched path globs), a checksum manifest, a `CITATION.cff` and the Zenodo README, with every number computed from the files beside it. The directory is gitignored and uploaded to Zenodo. The step also checks `repos_data.7z`, the archive of collected files that the `collected_files_archive` step writes.
- `_website/` contains the dataset explorer website: `build_site.py` builds the static site into `_site/` and puts the download files of the release into `_assets/`, `upload_assets.sh` uploads `_assets/` to the asset host, and `deploy.sh` copies `_site/` into the GitHub Pages repository (`se-uhd/agenttorrent`, published at https://se-uhd.de/agenttorrent/, to which the earlier addresses https://se-uhd.de/agentcontext-dataset/ and https://se-uhd.de/ai-config-dataset/ forward). See `_website/0_README.md`.

## Releases and versioning

`CHANGELOG.md` records every notable change in [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format, and its [versioning policy](CHANGELOG.md#versioning-policy) defines when a release is major, minor or patch. The version number describes the dataset as well as the code, because a downstream user cannot tell a changed script from a changed snapshot. Released versions are git-tagged (`v1.0.0` is the AIware 2026 dataset paper release) and recorded in the `dataset_version` column of `repos.csv`, of every mechanism CSV, of `planning-documents.csv` and of `imports.csv`. The commit CSVs have no such column, but the `provenance.json` of the release records the version.

## Citation

Cite the dataset with the `CITATION.cff` that each release contains, and the pipeline with
[CITATION.cff](CITATION.cff). Both name the authors in this order: Sebastian Baltes, Seyedmoein
Mohsenimofidi, Levi Böhme, Vahram Nersesyan, Muhammad Auwal Abubakar, Jai Lal Lulla, Matthias
Galster and Christoph Treude. The AIware 2026 dataset paper, *A Dataset of Agentic AI Coding
Tool Configurations*, describes release 1.0.0 ([DOI](https://doi.org/10.1145/3805760.3814922)).
Zenodo archives the releases of the dataset under the concept DOI
[10.5281/zenodo.24058026](https://doi.org/10.5281/zenodo.24058026) and those of the pipeline
under [10.5281/zenodo.24058028](https://doi.org/10.5281/zenodo.24058028).

## License

The code is under the [MIT license](LICENSE), and the data is under [CC BY
4.0](https://creativecommons.org/licenses/by/4.0/). The collected files and the commit messages keep
the licenses of their repositories, and CC BY 4.0 covers the compilation and the metadata and not
this content.

## Acknowledgements

Claude Code, with Claude models from Opus 4.6 onward, was used to write and revise code and documentation for this pipeline. The research design and methodology are the work of the human authors.
