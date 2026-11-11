# Analysis

The analysis stage reads the output CSVs of the current collection.
[docs/dataset.md](../docs/dataset.md) names that collection and gives the provenance of these
inputs and the coverage of the reclassification. `run_analysis.py` regenerates the figures and tables of each dataset release
from these inputs and checks that they reproduce. After a grounded heuristic change, extract the
inputs that [Execution](#execution) names and run `python3 check_reproduction.py` in this directory. It regenerates
every figure and table and names affected outputs in its `CHANGED` lines.
The scripts neither audit earlier papers nor compare results with published values. Agreement
with an earlier paper's counts does not establish correctness.

The directory contains one subdirectory per group of analyses, each with its scripts, specs and
README:

* `rq2/`: adoption patterns of the configuration mechanisms of the tools and the characteristics of the repositories that adopt them.
* `rq3/`: when the earliest configuration artifacts of repositories were created, how the context file categories co-occur, and how many configuration artifacts a repository has.
* `dataset/`: the figures and tables of a dataset release, such as tool adoption, the monthly AI commit volume and the imports per tool, and the paper's table of the filter steps of the sampling stage.

## Prerequisites

Set up the development environment with the [macOS](../docs/development.md#macos) or
[Linux](../docs/development.md#linux-ubuntu) instructions, or use the
[x86-64 development container](../docs/development.md#linux-x86-64-development-container), which
runs every stage and the verification suite.

Every analysis script reads the output CSVs of the collection stage in `data/collection/output/`.
The scripts and `_shared.py` import the `agentcontext` package at the repository root for their
log messages, and the runner imports `agentcontext/runner.py` from it. Each subdirectory pins its
direct Python dependencies in its own `requirements.txt`, and the dependency lock
`locks/development.txt` locks the three files with those of every other stage. From the
repository root, install the development lock and the package into `.venv` ([Python
environments](../docs/development.md#python-environments)):

```shell
mise exec -- bash dev/setup.sh
```

## The shared module

`_shared.py` is the one module that every analysis script imports, so that `rq2/`, `rq3/` and
`dataset/` keep no copies of their own. The module contains:

* the paths, and the field size limit of the csv module raised at import, because a
  `referenced_file` or a commit message of the output CSVs can be longer than the default
* the tool and mechanism constants that [CLAUDE.md](../CLAUDE.md#constants) states
* the context file categories of the timelines
* the plot styling and the UpSet renderer
* `latex_text`, which escapes a name for LaTeX, such as the language C#, for every script that
  writes a name into a LaTeX table or snippet
* `checked_copy`, which stops a script on an extracted commit CSV, sampling frame or
  `repos_annotated.csv` that differs from the archive that git tracks beside it
  ([archives](../docs/archives.md))
* the loader of the commit CSV
* `adjust_tested`, the Benjamini-Hochberg adjustment over the comparisons that were tested, which
  the two scripts with a table of tests share

The context file categories come from `heuristics/config_path_patterns.csv`.
`CATEGORY_OF_HEURISTIC_PATH` gives every `context_file` row of the heuristics a category. Importing
the module raises a `RuntimeError` when a row has none, or when the mapping names a path that the
heuristics do not list as a context file, so that no context file is left out of a timeline
unnoticed. A root `REVIEW.md` has a category of its own, because Claude Code Review and Copilot
code review both read it. A file gets the category of the heuristic that matches its path under
the heuristic's match mode. A nested file with the name of a heuristic that matches only at the
root therefore gets no category from that heuristic. A heuristic with the match mode `nested:`,
such as the heuristic of Copilot CLI's `.github/copilot-instructions.md` below the root, gives its
category to the files that it matches below the root. `test_shared.py` checks the module against
`CLAUDE.md`, the heuristics and every `context_file` value of the output CSVs on disk.

Every script reads two environment variables through the module. `DATA_COLLECTION_OUTPUT` names a
directory of output CSVs to read instead of `data/collection/output/`, and `ANALYSIS_OUTPUT_DIR`
names a root to write under instead of `data/analysis/`. Under that root, each script writes into
a directory with the name of its own directory, so that the outputs of `rq2/` and `rq3/` stay
apart. The only path that a script sets itself is `OUTPUT_DIR = output_dir(__file__)`.

Run the suite of the module from this directory:

```shell
python3 -m unittest test_shared -v
```

## Reproducing every output

`check_reproduction.py` runs every analysis script twice and lists every output that changed or
did not reproduce. An output changed when the first run wrote other bytes than were on disk before
it, and it did not reproduce when the second run wrote other bytes than the first. After a data
change, the changed outputs are therefore the figures that the change affects. The check sets
`SOURCE_DATE_EPOCH=0` and `MPLBACKEND=Agg`, under which the PDFs are byte-identical between runs.
It compares bytes rather than the text that `pdftotext` extracts, because `pdftotext` gave a false
difference on a figure.

The check exits 0 when every script ran, every output reproduced and nothing changed. It exits 2
when an output changed, did not reproduce or is a figure or table that no script wrote. It exits 1
when a script failed or an input that it needs is missing. `--skip-missing` turns a missing input
into a skip, but a run that skipped every selected script still exits 1, because it checked
nothing.

Run the check from this directory:

```shell
python3 check_reproduction.py                                    # the local check, every script
python3 check_reproduction.py --skip-missing --reproduce-only    # what CI runs
python3 check_reproduction.py --list             # the scripts, their inputs, which can run here
```

CI runs the scripts over the tracked inputs, which are the output CSVs and the commit CSV in its
archive. It skips the scripts that read `data/classification/repos_annotated.csv`,
`data/sampling/ghs_filtered.json` or the validated tree, because a checkout contains these inputs
only as archives or not at all. Only the local check runs these scripts. CI also checks only
whether each output reproduces, not whether it changed, because the bytes of the tracked PDFs and
of `config_cooccurrence.csv` depend on the machine that wrote them. On the Linux runner, PDFs and
the last digits of the CSV differ while every script reproduces. Neither the local check nor CI
runs the scripts in `NOT_RUN` of `check_reproduction.py`, which gives the reason for each.

## The fixture

`testdata/output/` contains the output CSVs of a run on 2026-09-12 over the development subsample as
it was then, and the run's `repos.csv` lists the repositories of that subsample with
configuration artifacts. Detection ran again over
the same clones on 2026-09-15, when plugins became a mechanism, and that date is the
`scanned_at` of those rows. The fixture also contains the commit CSV of the run of 2026-09-12 as
`all_ai_commits.csv.7z`, which `read_commits_csv` extracts when the CSV is absent. The fixture is
not a run over the current list, because `subsample` has since written a new selection to
`data/collection/dev_subsample.csv`.

The fixture's output CSVs also contain the rows of repositories from the March output
CSVs, because the run of 2026-09-12 had no AGENTS.md-only repository and no row without a
`created_at`. `47ng/nuqs`, `Automattic/harper` and `CodebuffAI/codebuff` are AGENTS.md-only.
`clickhouse/clickhouse` has a `.cursor/BUGBOT.md` that had no `created_at` in the March output CSVs
when its rows were copied, and the file has none in the fixture. The fixture is historical test
input and is separate from the current development dataset.
`testdata/sampling/ghs_filtered.json` contains the rows of the fixture's repositories in the
sampling frame of 2026-03-17, with the fields `name`, `mainLanguage`, `createdAt`,
`contributors`, `commits` and `size` that `rq2/_repo_metadata.py` reads, and
`testdata/classification/repos_annotated.csv` contains the labels that the classification gave
these repositories in March 2026. The test of `repo_metadata_comparison.py` over the fixture
reads both files through a data root of its own, so that its numbers do not depend on the dataset
that a checkout extracts.

The `lsp` mechanism came after the run of 2026-09-12. The fixture's `lsp.csv` contains only its
header, and the `lsp` column of `repos.csv` is `FALSE` throughout, as the run would have written
it. The `tool` column of the mechanism CSVs also came after that run. In the fixture, each row
names the tool that `tool_for_path` gives its path, or `any` for a shared file. The tool flags of
`repos.csv` are the union of the tools of a repository's rows, and `repos.csv` has
`no_specific_tool` in place of `agents_md_only`. Taking the union changed the flags of `LMCache/LMCache`, which gained
Codex, and of `yamadashy/repomix`, which gained Gemini. OpenCode became one of the tools after that run,
which did not collect its paths. The `opencode` column of `repos.csv` is `FALSE` throughout, and
no row of the mechanism CSVs is OpenCode's.

To run the reproduction check over the fixture, give it an output directory. Over the fixture,
`rq2/upset_plot.py` fails, because its default `--min-count 5` exceeds every intersection, and the
command exits 1:

```shell
python3 check_reproduction.py --data-dir testdata/output --output-dir /tmp/figures --skip-missing
```

To render that plot over the fixture, lower its threshold:

```shell
DATA_COLLECTION_OUTPUT=testdata/output ANALYSIS_OUTPUT_DIR=/tmp/figures \
  python3 rq2/upset_plot.py --min-count 1
```

## The runner

`run_analysis.py` runs the analysis stage as steps over `../agentcontext/runner.py`: every analysis
script, the filter steps table and the reproduction check. `../run_pipeline.py` runs it after the
publication stage. The runner records each script's step by a hash of the script and of the files
that it reads. A run after an input changed therefore redoes exactly the figures that read the
input and skips the rest. The reproduction check runs last, over every script.
`run-analysis-spec.md` lists the steps, and `test_run_analysis.py` checks the runner's steps
against the scripts, the tracked outputs and the check's own list.

The hash of every step also covers `_shared.py` and `heuristics/config_path_patterns.csv`, and the
hash of each script that imports `rq2/_repo_metadata.py` covers that module. For a commit archive
split into volumes, the hash covers every volume, also when the extracted CSV is beside it, and a
change to any volume reruns the figures that read the commit CSV. The hash of a step that reads
the labels or the sampling frame covers the tracked archive beside the extracted file the same
way. A merge or a pull that replaces an archive therefore reruns the steps that read it, and
their scripts stop on an extracted file that no longer matches the archive. The script of the
monthly AI commit volume reads `repos.csv` beside the commit CSV, and its step declares both. The
hash of the content quality step also covers every file of the validated tree that the mechanism
CSVs list or that a marker among them names, and every file of their repositories that the
collected file cleanup keeps even when it is empty. The script reads the listed files, and
whether it counts a listed empty file with 0 lines depends on the set of the kept files.

The runner names its steps with noun phrases in its progress messages. In `run_analysis.py`,
`SAMPLING_FRAME` is the path of the GHS input with its tracked archive as an alternative, and
`MECHANISM_CSVS` holds the paths of the mechanism CSVs that the steps read. The runner records the outcome of a run in its logs and its
pipeline execution state, and it sends no push notification. Run it from this directory:

```shell
python3 run_analysis.py --list
python3 run_analysis.py
python3 run_analysis.py --from rq3_adoption_timeline
```

## Execution

The scripts in each directory are independent of each other, and you can run them in any order.
Some need more than the output CSVs:

- `rq2/`: `content_quality.py` also needs the validated tree at `data/collection/data/validated/`, which a working copy of the current handover supplies ([development](../docs/development.md#handover-working-copy)). `language_tool_association.py` needs the extracted `data/sampling/ghs_filtered.json`, and `language_ranking_all.py` and `repo_metadata_comparison.py` need the same file and the extracted `data/classification/repos_annotated.csv`.
- `dataset/`: The two scripts over the commit CSV read `data/collection/output/all_ai_commits.csv` and extract the archive beside it when the CSV is absent.

`dev/setup.sh` extracts each of the two files that is missing, and it keeps one that differs from
its archive ([development](../docs/development.md#macos)). Extract them again by hand after a
merge, a pull or a switch of branches that changes an archive, at the repository root:

```shell
7zz x data/sampling/ghs_filtered.json.7z -odata/sampling -y
7zz x data/classification/repos_annotated.7z -odata/classification -y
```

In a checkout, a script stops on an extracted commit CSV, sampling frame or `repos_annotated.csv`
that differs from the archive that git tracks beside it. Its message names the command that
extracts the archive again and, for an extracted file that is the newer input, the section of
[archives](../docs/archives.md#packing-the-archives-again) with the command that packs the
archive again. `bash dev/setup.sh --data-only` extracts a differing commit CSV again. After you
extract the labels again, also run `python3 -m agentcontext engineered-project-selection`, which
derives the list of the sample from them. In the data root of a run and in a working copy, git
tracks no archive, and the scripts read the files that the steps wrote
([archives](../docs/archives.md)).

The `0_README.md` of each subdirectory gives the instructions for its scripts.

## Tests

Every script of the stage has a spec beside it and a standard library `unittest` suite at the top
of this directory, named `test_<directory>_<script>.py` for the analyses and `test_<script>.py`
for the rest. Every test case runs offline and writes only under a temporary directory. The tests
call the pure functions on DataFrames that they build. A script that computes its numbers inside
`main()` runs as a subprocess over the fixture `testdata/output` with `DATA_COLLECTION_OUTPUT` and
`ANALYSIS_OUTPUT_DIR` set. The suites over the figure scripts need the packages in the three
`requirements.txt` files. The test cases that read the fixture's commit archive need `7zz` and skip
without it.

- `test_shared.py`: the shared module against CLAUDE.md, the heuristics and the output CSVs, the escaping of `latex_text`, the TrueType fonts that `save_figure` embeds, and the loader of the commit CSV, which stops on a CSV that differs from the archive that git tracks beside it and reads the CSV beside an archive that git does not track
- `test_run_analysis.py`: the runner's steps against the scripts, the tracked outputs and the check's list
- `test_rq2_repo_metadata.py`, `test_rq2_config_cooccurrence.py`, `test_rq2_config_mechanism_upset.py`, `test_rq2_config_tool_heatmap.py`, `test_rq2_content_quality.py`, `test_rq2_language_ranking_all.py`, `test_rq2_language_tool_association.py`, `test_rq2_repo_metadata_comparison.py`, `test_rq2_tool_cooccurrence.py`, `test_rq2_tool_count_distribution.py`, `test_rq2_upset_plot.py`, `test_rq2_venn_diagram.py`: the twelve `rq2/` modules
- `test_rq3_adoption_timeline.py`, `test_rq3_cooccurrence_analysis.py`, `test_rq3_count_distribution.py`: the three `rq3/` scripts
- `test_dataset_artifact_distribution.py`, `test_dataset_tool_adoption.py`, `test_dataset_monthly_ai_commit_volume.py`, `test_dataset_commit_message_first_words.py`, `test_dataset_filter_steps_table.py`, `test_dataset_planning_documents.py`, `test_dataset_imports.py`: the seven `dataset/` scripts
- `test_check_reproduction.py`: the check's verdicts over stand-in scripts in a temporary tree, among them a script that writes the same bytes in both runs but fails on the second, which the check counts as failed with exit 1

CI runs every suite with this command from the repository root:

```shell
cd 4_data-analysis
python3 -m unittest \
  test_shared test_run_analysis \
  test_rq2_repo_metadata test_rq2_config_cooccurrence test_rq2_config_mechanism_upset \
  test_rq2_config_tool_heatmap test_rq2_content_quality test_rq2_language_ranking_all \
  test_rq2_language_tool_association test_rq2_repo_metadata_comparison test_rq2_tool_cooccurrence \
  test_rq2_tool_count_distribution test_rq2_upset_plot test_rq2_venn_diagram \
  test_rq3_adoption_timeline test_rq3_cooccurrence_analysis test_rq3_count_distribution \
  test_dataset_artifact_distribution test_dataset_tool_adoption \
  test_dataset_monthly_ai_commit_volume test_dataset_commit_message_first_words \
  test_dataset_filter_steps_table test_dataset_planning_documents test_dataset_imports \
  test_check_reproduction
```
