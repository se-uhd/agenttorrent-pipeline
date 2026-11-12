# Run the whole pipeline

## Context

`run_pipeline.py` runs the five stages of the pipeline as one list of steps and keeps one pipeline execution state that records which steps completed. The stages are sampling, classification, collection, publication and analysis. Each stage's runner is a list of `Step` objects that `agentcontext/runner.py` runs. Every stage therefore has that module's lock, its state file with a fingerprint per completed step, its pre-checks, its dependency-based invalidation and its stop handling, and these parts depend on nothing outside the module. Before AgentTorrent 1.0.0, a person ran the classification scripts and the analysis scripts by hand, in the order that their READMEs gave. This script composes the four lists and adds the edges between them. The collection's list also contains the steps of the publication.

## Key decisions

- **Language:** Python 3.14 (single script `run_pipeline.py` at the repository root). It imports the standard library, the three stage runners from their directories, which it adds to `sys.path`, and `agentcontext.runner` from the installed package
- **Five stages of steps.** Each stage is a sequence of steps, and a step names its stage in `Step.stage`. `--list` shows the steps under their stage, and `--from` takes a stage to mean all of its steps and what is computed from them. The pipeline execution state records each step, because a collection stopped after days has to resume at its own step, and each script stays separate. `engineered_project_selection` is the last step of the classification. The collection's list, which `python -m agentcontext run` runs on its own, contains it as well
- **One module, four lists, five pipeline execution states.** `agentcontext/runner.py` defines the `Step` dataclass, the pre-checks, the pipeline execution state and the loop. `run_sampling.py`, `run_classification.py`, `run_analysis.py` and this script are lists over it, each with its own parser. Every stage's runner keeps its pipeline execution state, lock and logs under the stage's `run_logs/` in the data root, and this script keeps them under `run_logs/` at the data root itself, `data/run_logs/`. A run of the whole pipeline and a run of one stage therefore never share a state file
- **A step names the directory that it runs in and the directory of its code.** `Step.root` names the step's data directory relative to the data root, and every data path that the step declares resolves against it. `Step.code` names the directory of its script relative to the repository root. The steps of `agentcontext/runner.py` leave both fields empty. An empty `root` means the collection stage's directory, and an empty `code` means the package. With these two fields, the lists span the stages, and the code and the data stay apart
- **A snapshot step takes two flags.** With `completion=COMPLETION_OUTPUT`, the runner records a step by a checksum manifest of what it produced. `repository_cloning` uses it together with the inputs by which the runner records the step: the collector's code, the version of git, the path heuristics, the import heuristics and the list that it collects. With `irreproducible=True`, the runner also adopts an output already on disk as the step's result at the start of a run, unless the record lists the step as started and not finished. The GHS download uses both flags, and `repository_cloning` uses only the first, because a reconstructed sample is recorded on purpose with `--mark-complete`. To take a snapshot again, name its step in `--from`
- **The analysis steps are recorded by their inputs.** With `completion=COMPLETION_INPUTS`, the runner hashes the script and the contents of every file that the step declares in `inputs`. A changed input file therefore runs exactly the analysis steps that read it again, and the reproduction check runs last over all of them
- **The edges between the lists are added here,** by the directory that contains an input. A step of the classification, the collection or the analysis that reads under one of these directories consumes the step named beside it:
  - `../sampling/`: the filter
  - `../classification/`: the classification
  - the collection's `output/`: the file history step, except `output/all_ai_commits*`, which the detection step produces
  - `data/validated/`: the file history step as well

  The file history step removes the directory of a repository that git reports as gone. The content quality step reads the files that the file history step's CSVs list, in the tree as the file history step leaves it. If the tree were the output of the collected file cleanup, the content quality step would read an output that the file history step modifies. The check for modified inputs would then rebuild the validated tree, and run every step after the cleanup again, in every run in which the content quality step is pending and the cleanup is not. `engineered_project_selection` consumes `repository_labeling` in the classification's list. `compose` leaves each runner's own list as that runner declares it
- **The labeling consumes no step.** `repository_labeling` reads the sampling frame. The runner records it by its script, its prompts and its modules, and in place of the sampling frame's file, by the repositories in the sampling frame, `items`, and the values of its `_metadata` that the labeling reads (`sampling_frame_contents`, `labeling_metadata`). Those values are the ones that `read_sampling_frame` in `2_classification/1_classify_repos.py` and `forks.thresholds_of` read: the retrieval's id, whether GitHub's responses are recorded, the `contributorsMin` of the GHS query and the first day of the window. The filter runs again after every change to its code, its config, `config/licenses.csv` or the tracked SPDX list. Each run of the filter writes its time, its config and the license step's review into `_metadata`, that is, the `reviewed` decision of each license in `licenses`, `unreviewed`, `review_sha256` and `spdx_list_version`. A review of a license is required after `--check-licenses` names one. With an edge to the filter, or recorded by the whole file, the labeling would request the README of every repository in the sampling frame again after such a run, and every step after it would run again, although the repositories and the values that the labeling reads are unchanged. For the same reason, the runner records `repository_cloning` by the contents of its list rather than by an edge to `engineered_project_selection` (`docs/runner.md`). `sampling_frame_contents` digests each item as JSON with sorted keys, and the layout of the file therefore does not change the record. A file that does not parse as a sampling frame is recorded as absent, and the labeling then runs and names the error. When the labeling starts to read another value of `_metadata`, add that value to `labeling_metadata`. `reads` still names the sampling frame, and the pre-checks therefore require it. The classification's own runner records the labeling by the whole file
- **`repos_annotated_included.csv` has one producer.** `engineered_project_selection` derives it from `repos_annotated.csv` and declares it. The step checks a row count only when `--expect-rows` supplies one
- **One step declares no output.** The analysis stage's reproduction check has an empty `produces`. The runner fails a step that exits 0 without writing what it declares, and that check does not apply to the reproduction check

## Script: `run_pipeline.py`

### CLI interface

| Flag | Default | Description |
|------|---------|-------------|
| `--list` | off | Print every stage and under it every step with its interpreter and arguments, and its directory for every step outside the collection's list. Exits 0 |
| `--from NAME` | *(none)* | Run the stage's steps, or the step, again, with everything computed from them, across the lists |
| `--with-commit-reclassification` | off | Reclassify commit logs after `repository_cloning`, before detection, and run both commit checks |
| `--repo-csv PATH` | *(`engineered_project_selection` writes it)* | The collection's input list |
| `--step-arg NAME:ARGS` | *(none)*, repeatable | Append arguments to one step for this run |
| `--mark-complete STEP` | *(none)* | Record a step that is recorded by its output from what is on disk, and exit. Refused together with `--list`, `--from`, `--repo-csv` or `--step-arg`, which it would ignore |
| `--scratch-dir DIR` | `data/collection/_scratch` | The directory that the cloning steps clone into |
| `--min-free-gb GB` | *(the default of `agentcontext/runner.py`)* | Set the free-space minimum for both the collection and the pre-checks |
| `--no-github` | off | For clones from local mirrors, skip the checks of the GitHub access token |

### Function structure

**`producers_of(paths)`**: the step keys that write the given paths, by prefix, through `PRODUCERS`

**`labeling_metadata(metadata)`**: the values of a sampling frame's `_metadata` that the labeling reads

**`sampling_frame_contents(step)`**: what the runner records the labeling by in place of the sampling frame's file, the SHA-256 of `labeling_metadata` and of each item as JSON with sorted keys, or None when the file is absent or does not parse as a sampling frame

**`compose(with_commit_reclassification=False)`**: combines the four lists and adds dependencies with `dataclasses.replace`. It reads the steps of the collection's list from `runner.steps_for(with_commit_reclassification)`, including the optional reclassification step and its dependencies. When you enable the reclassification, or when its inputs change, the runner runs the collected file cleanup and the steps that consume it again. The cleanup rebuilds a tree that an earlier run pruned, without collecting again. The analysis of `all_ai_commits.csv` depends on `detection`, which restricts that CSV to the configured repositories after the reclassification. `compose` refuses duplicate step keys.

**`build_parser()`**, **`main(argv=None)`**: the flags, and the assembly handed to `runner.run` with the data root's `run_logs/`

### Processing pipeline

The processing is that of `agentcontext/runner.py`: the lock, the pipeline execution state, the adoption of snapshots on disk, the check for modified inputs, the pre-checks over the pending steps and the steps that an earlier step can make pending, then each step in order. The runner records a step with its fingerprint when the step exits 0 after writing what it declares.

### Output

`run_logs/state.json` in the data root, with `completed` keyed by step and the run's provenance, a `run_logs/<step>.log` per step, and `run_logs/run.lock`.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| A key shared between two lists | `ValueError` from `compose`, before anything runs |
| An analysis input is absent from the data root | The analysis step fingerprints to nothing and runs. The pre-checks refuse a `reads` path that no pending step produces |
| A run from a clean checkout | `--list` resolves every step. A run stops in the pre-checks before any step starts, with one `ERROR pre_checks:` line for each prerequisite that the checkout lacks |

## Files

| File | Action |
|------|--------|
| `run_pipeline.py` | Main script |
| `agentcontext/runner.py` | Imported: `Step`, `run` and the collection's list, with the selection and the publication |
| `1_sampling/run_sampling.py`, `2_classification/run_classification.py`, `4_data-analysis/run_analysis.py` | Imported: the other three lists |
| `data/run_logs/` | Write: the pipeline execution state, lock and logs (gitignored) |
| `test_run_pipeline.py` | Tests |

## Usage

```bash
python3 run_pipeline.py --list
python3 run_pipeline.py --with-commit-reclassification --scratch-dir /mnt/scratch
python3 run_pipeline.py --from detection
```

## Tests

See `test_run_pipeline.py`. Stdlib `unittest`. The tests check the composition against the four lists, run `--list` in a subprocess from the root and from another directory, and run `main` once over a stand-in step.

- **`TestComposition`**: The four lists are in order, with every key once. Over an empty data root, every step finds what it reads. The reclassification runs before detection when it is requested, and it invalidates its readers without a new collection. The two checks join before the publication when they are requested. The five stages are in order, each a contiguous sequence of steps, with and without the reclassification. Every step has a directory and a script on disk, and every dependency names an earlier step. The tests also cover the edges between the lists. The content quality step reads the tree that the file history step leaves, and the check for modified inputs reports no run of it while it still reports detection over the pruned tree. After a failure or a stop between the file history step and the archive, the check reports nothing, and at the next start the runner resumes at the step that did not finish. The pre-checks cover the steps that a monthly run reaches after the download, and they find the model that the labeling requests. An edit to the filter runs the language figures again, and neither the labeling nor the cloning step. A new sampling frame or new labels run the release again, and the release declares the sampling frame and the labels that it reads. The runners' own lists stay as they are, and a shared key is refused
- **`TestTheLabelingRecord`**: A run of the filter that writes the same repositories at another time leaves the labeling complete, while the filter's own record no longer matches. Another config or license review keeps the labeling complete, and so does another commit minimum of the filter's activity step. Another sampling frame or none runs the labeling again. The tests write the sampling frames through the filter's own writer
- **`TestTheComparisonRecord`**: Over a real cache of retrievals, the comparison of retrievals stays complete after the cloning step rewrites the collected list, and a new retrieval runs it again
- **`TestList`**: The list shows every stage and under it every step, with the directory of each step outside the collection's list. It resolves from the root and from elsewhere under a bare interpreter
- **`TestARun`**: An abbreviated option is a usage error. The free-space override reaches both the collection and the pre-checks, and the filter's `--top-languages` reaches the download too. `--mark-complete` refuses `--repo-csv` and `--step-arg`, which it would ignore. The pipeline execution state is kept in `run_logs/` at the data root

Run with:

```shell
python3 -m unittest test_run_pipeline -v
```

## Verification

1. Run `python3 run_pipeline.py --list` and compare the stages, step names and directories with the lists of the runners that it composes. Run it again with `--with-commit-reclassification`, and check that the list then contains the commit steps.
2. Run `test_run_pipeline` ([Tests](#tests)) and `tests.test_runner`, which check the dependencies, the routing of the arguments and the completion records with temporary inputs.

Progress messages name the steps by the noun phrases of `logs.step_name`. The runner reports outcomes in its logs and its pipeline execution state and sends no push notification. Completion keys and dependency edges use the current step identifiers, which are noun phrases, and a retired identifier has no alias and no migration of the state.
