# Dataset figures

Use [the development guide](../../docs/development.md) for native macOS, native
Linux and x86-64 container setup. Run these commands with that environment's
Python interpreter.

The scripts in this directory make the figures and tables of a dataset release. The figure scripts read `data/collection/output/` and write PDF files. `planning_documents.py` and `imports.py` read the same directory and each write a CSV and a LaTeX table, and `filter_steps_table.py` reads the sampling frame and writes a LaTeX table.

## Scripts

| Script | Input | Output | Description |
|--------|-------|--------|-------------|
| `artifact_distribution.py` | `context-files.csv`, `skills.csv`, ... | `artifact_distribution.pdf` | Configuration artifact count and repository count per configuration mechanism |
| `tool_adoption.py` | `repos.csv` | `tool_adoption.pdf` | Repository count per tool |
| `monthly_ai_commit_volume.py` | `all_ai_commits.csv`, `repos.csv` | `monthly_ai_commit_volume.pdf` | Monthly volume of AI commits since 2025, through the month before the month of the latest scan, without the commits dated after that scan |
| `commit_message_first_words.py` | `all_ai_commits.csv` | `commit_message_first_words.pdf` | Most frequent first words in the messages of AI commits |
| `filter_steps_table.py` | `data/sampling/ghs_filtered.json` | `filter_steps_table.tex` | The filter steps of the sampling stage, with what each removed, as a LaTeX table |
| `planning_documents.py` | `planning-documents.csv` | `planning_documents.csv`, `planning_documents.tex` | The planning documents and the repositories of each framework or tool |
| `imports.py` | `imports.csv` | `imports.csv`, `imports.tex` | The imports of each tool by the status of their target, with the importing files and the repositories |

The paths, the plot styling and the loader of the commit CSV come from `../_shared.py`, the module that every analysis script imports and that `../0_README.md` describes. The loader extracts the numbered `all_ai_commits.csv.7z.001` archive when the CSV is absent, and it stops on a CSV that differs from the archive that git tracks beside it. `filter_steps_table.py` stops the same way on a sampling frame that differs from `ghs_filtered.json.7z`. A script that names the columns that it uses, as the script of the monthly AI commit volume does, gets them as text, read in chunks, so that the script does not hold the commit messages in memory.

## Prerequisites

The commit analysis scripts read `all_ai_commits.csv`, which is tracked as the volumes `data/collection/output/all_ai_commits.csv.7z.001` and on. All numbered volumes must be present. When the CSV is absent, the loader extracts the archive into a temporary directory on every run, and you do not need to extract it yourself. To extract it once instead, run this command from the repository root. It names the output directory, because the archive stores the CSV at its root:

```bash
7zz x data/collection/output/all_ai_commits.csv.7z.001 -odata/collection/output -y
```

Run the command again whenever a merge, a pull or a switch of branches changes the archive, or run `bash dev/setup.sh --data-only`, which extracts a CSV that differs from its archive again. The loader compares the extracted CSV with the archive by size and CRC32 and stops on a CSV that differs, because the figures would otherwise describe the earlier archive while the stated commit counts, which read the archive, describe the current one ([archives](../../docs/archives.md)).

From the repository root, install the dependency lock `locks/development.txt`, which locks `requirements.txt`, and the `agentcontext` package, which every analysis script imports for its log messages, into `.venv`:

```bash
mise exec -- bash dev/setup.sh
```

## Usage

Run the scripts from any working directory:

```bash
python 4_data-analysis/dataset/artifact_distribution.py
python 4_data-analysis/dataset/tool_adoption.py
python 4_data-analysis/dataset/monthly_ai_commit_volume.py
python 4_data-analysis/dataset/commit_message_first_words.py
python 4_data-analysis/dataset/filter_steps_table.py
python 4_data-analysis/dataset/planning_documents.py
python 4_data-analysis/dataset/imports.py
```

A checkout extracts the sampling frame of retrieval `2026-11-02T223052Z`, and `check_reproduction.py` runs `filter_steps_table.py` over it as it runs the other scripts. The sampling frame of 2026-03-17, which an earlier version of the filter wrote, records each step with a description and no key, and `filter_steps_table.py` names such a step by its description. A pipeline run renders the table from the sampling frame that its sampling stage wrote.

The two commit analysis scripts, `monthly_ai_commit_volume.py` and `commit_message_first_words.py`, accept `--data-dir`, `--output-path` and `--show`, which `--help` describes.

## Tests

Each script has a standard library `unittest` suite one directory up and a spec beside it: `artifact-distribution-spec.md`, `tool-adoption-spec.md`, `monthly-ai-commit-volume-spec.md`, `commit-message-first-words-spec.md`, `filter-steps-table-spec.md`, `planning-documents-spec.md` and `imports-spec.md`. Every test case runs offline and writes only under a temporary directory. Where `main()` computes the numbers of a figure, the suite replaces pyplot with a recording stand-in and reads the bar labels and the title back. One subprocess per figure script runs over the fixture `../testdata/output` with `DATA_COLLECTION_OUTPUT` and `ANALYSIS_OUTPUT_DIR` set, so that no test writes into this directory. The two suites over the commit CSV read the fixture's `all_ai_commits.csv.7z` through `_shared.read_commits_csv` and skip the test cases that read the archive when `7zz` is missing.

- `../test_dataset_artifact_distribution.py`: configuration artifacts and repositories per mechanism CSV, a missing mechanism CSV that stops the script rather than counting as zero, the fixture's counts against its `repos.csv` flags
- `../test_dataset_tool_adoption.py`: the six counts and shares over a small `repos.csv` and over the fixture
- `../test_dataset_monthly_ai_commit_volume.py`: the flags, the month bucketing, the start filter, the dropped rows, the commits dated after the latest scan and the month of the latest scan left out, a scan with a date and no time, the fixture's window since 2025
- `../test_dataset_commit_message_first_words.py`: the flags, the first-word normalization, `--top-n`, the fixture's top words
- `../test_dataset_filter_steps_table.py`: the labels from the keys and values of the filter steps, the gone step's unresolved rows and repositories without an id, a step of the March sampling frame named by its description in LaTeX that prints it, the step that removed nothing commented out, the exit 1 over a sampling frame without filter steps, the default output under `ANALYSIS_OUTPUT_DIR`, the stop beside a tracked archive of another sampling frame
- `../test_dataset_planning_documents.py`: the documents and repositories of each framework and the total row over the fixture
- `../test_dataset_imports.py`: the imports of each tool by status, the importing files counted once in the total row, over the fixture, and a total row of zeros over an `imports.csv` with its header alone

Run the suites from this directory:

```shell
cd ..
python3 -m unittest test_dataset_artifact_distribution test_dataset_tool_adoption \
  test_dataset_monthly_ai_commit_volume test_dataset_commit_message_first_words \
  test_dataset_filter_steps_table test_dataset_planning_documents test_dataset_imports -v
```
