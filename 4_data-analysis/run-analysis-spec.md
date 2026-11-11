# Run the analysis stage

## Context

`run_analysis.py` runs the analysis stage as steps over `agentcontext/runner.py`: every analysis script, the filter steps table for the paper, and the reproduction check. Because the runner records each step by its inputs, its pipeline execution state shows which figures a changed input affects.

## Key decisions

- **Language:** Python 3.14, in the single script `run_analysis.py`, with the standard library, `agentcontext.runner` and, for the fingerprint of the content quality step, the collected file cleanup's `kept_when_empty` and the detection step's `measured_artifact`, imported from the installed package
- Every step names `analysis` as its stage. The steps of this stage write the figures and LaTeX tables that a paper uses. The table of the sampling stage's filter steps is among them, and the sampling stage therefore ends with the sampling frame
- Each analysis step is recorded by its inputs. The step declares the files that its script reads in `inputs`, relative to its data directory `../data/analysis/`, and `completion=COMPLETION_INPUTS` records a hash of the script and of the contents of those files. A run over changed output CSVs reruns exactly the figures that read them, and a changed script reruns its own figures. The hash of every analysis step also covers `_shared.py` and `heuristics/config_path_patterns.csv`, and the hash of the two analyses of repository metadata covers `rq2/_repo_metadata.py`. With `package_code`, the fingerprint of a script's code also covers the modules of the package that the script and its code inputs import, with their module closures. Such a module can change a table while every input stays the same: The table of the imports takes its columns from `imports.STATUSES`
- The content quality step names `listed_validated_files` as its `inputs_of`. The function returns a digest over the path and the contents of each file that the script reads: every file of the validated tree that the mechanism CSVs list, the file that each listed marker of a symlink names and the script counts (`detection.measured_artifact`), and every file of a listed repository that the collected file cleanup keeps even when it is empty (`collected_file_cleanup.kept_when_empty`). The cleanup keeps the imports of the GitHub Agentic Workflows, the prompt files of their agent actions, the planning documents with their anchors, and the CLAUDE files that determine whether Claude Code reads an AGENTS.md. Whether the script counts a listed empty file with 0 lines or stops depends on the set of the kept files. A tree in which the set changed therefore runs the step again, such as a tree that lost the tasks of cursor-memory-bank beside a listed empty `.cursor/commands/van.md`. A path of the set that is no file is left out. Declaring the tree in `reads` alone is not enough. The pre-checks require a `reads` path on disk, but no fingerprint covers it, and the step would stay recorded as done after the tree changed under the same CSVs. A file of the tree that no CSV lists and that is outside the set is not read and changes nothing. A listed file that is gone leaves the step without a fingerprint. The step then runs, and its script logs the missing file. A listed path that is a symbolic link is digested by the link's text, and by the bytes of the file that it leads to where it leads to one. The script reads a context file that is a link as the link's text, also where the link has no target (`detection.artifact_text`), such as a link that rsync kept inside a directory that the collector copies whole. Such a link is there, and the step stays done while its text and the rest of the tree stay the same
- The commit CSV is declared as `all_ai_commits.csv|all_ai_commits.csv.7z.*|all_ai_commits.csv.7z`, because the CSV is gitignored and a clean checkout has only the archive. The fingerprint covers the first alternative on disk and every archive volume after it on disk (`runner.input_paths`). A checkout keeps the extracted CSV when a merge, a pull or a switch of branches replaces the archive. With the CSV alone in the fingerprint, the two commit steps would stay recorded as done, and their figures would keep describing the earlier archive. The changed archive runs the steps again instead, and the loader then stops on a CSV that differs from the archive that git tracks beside it ([archives](../docs/archives.md)). In the data root of a run and in a working copy, no archive is beside the CSV, and the fingerprint is that of the CSV alone. The step of the monthly AI commit volume also declares `repos.csv`, because the latest `scanned_at` in that file ends the step's window
- The labels and the sampling frame are declared with their tracked archives as alternatives, `repos_annotated.csv|repos_annotated.7z` and `ghs_filtered.json|ghs_filtered.json.7z`, for the same reason. Their scripts read only the extracted file, through `_shared.checked_copy`. With the archive alone on disk, the step runs and its script stops on the missing file, as it would without the archive in the declaration
- The runner hashes no later alternative that is not an archive. The only other step of the pipeline with alternatives, `collection_summary`, declares `output/commit-reclassification.json|output/repos.csv`, and its fingerprint stays as a finished run recorded it
- The check is the last step. `reproduction_check` runs `check_reproduction.py --skip-missing --reproduce-only` after every analysis step and consumes all of them. Every run therefore ends by checking that each script writes the same bytes twice
- The check also runs `filter_steps_table.py` over the sampling frame that a checkout extracts, and the table that the script writes, `filter_steps_table.tex`, is tracked as every other output is

The analysis stage regenerates every statistical table and figure of a dataset release from the
current data and checks that each reproduces. No step audits a paper or
compares a result with a value that an earlier publication reported.

## Script: `run_analysis.py`

### CLI interface

| Flag | Default | Description |
|------|---------|-------------|
| `--list` | off | Print the stage with its steps and exit |
| `--from NAME` | *(none)*, repeatable | Run the step again with every step that reads its output, or every step of the stage for `--from analysis` |
| `--step-arg NAME:ARGS` | *(none)*, repeatable | Append arguments to one step |

### Module-level data

`ROOT` (`analysis`, the stage's data directory under the data root), `CODE` (`4_data-analysis`), `MIN_FREE_DISK_GB` (<!--s:parameters.4_data_analysis.run_analysis.MIN_FREE_DISK_GB-->2<!--/s-->), `OUT` (`../collection/output`, relative to the data directory), `REPOS`, `MECHANISM_CSVS` (the mechanism CSVs that the content and distribution analyses read), `COMMITS`, `ANNOTATED` and `SAMPLING_FRAME` (each an extracted file with its tracked archive as an alternative), `VALIDATED`, `REPO_METADATA` (`rq2/_repo_metadata.py`), `PLOTTING`, `STAGE` (`analysis`), `LISTED_FILES` (the label under which the fingerprint of the content quality step records the files of the validated tree that it covers), and `STEPS`: one step per script under `rq2/`, `rq3/` and `dataset/`, keyed by directory and script name (e.g., `rq2_config_cooccurrence`, `dataset_filter_steps_table`), then `reproduction_check`. `_figure` builds an analysis step.

### Function structure

**`_figure(key, script, inputs, produces, imports, reads, code_inputs, inputs_of)`**: an analysis step recorded by its inputs, with `_shared.py` among its `code_inputs` and `package_code` set

**`listed_validated_files(step)`**: `[(LISTED_FILES, digest)]`. The digest is a sha256 over `(repository, path)` pairs, in order, each with the sha256 of the file under `../collection/data/validated/<owner§repo>/`: every pair that the ten mechanism CSVs list, that `detection.measured_artifact` gives for a listed path, or that `collected_file_cleanup.kept_when_empty` gives for a listed repository and that is a file. `detection.measured_artifact` gives no path for the marker of a link with a target that the tree lacks. The digest is `None` when a CSV or one of these files is absent, and a listed file that cannot be read raises `runner.UnreadableOutput`

**`build_parser()`**, **`main(argv=None)`**: the flags, and the step list that `main` passes to `runner.run` with `data/analysis/run_logs/`

## Error handling & edge cases

The runner handles errors as `agentcontext/runner.py` describes. In this stage, a step with an absent input has no fingerprint and is never recorded as done, and the check's exit 2 stops the run.

## Files

| File | Action |
|------|--------|
| `run_analysis.py` | Main script |
| `../agentcontext/runner.py` | Imported: `Step`, the completion rules and `run` |
| `rq2/*.py`, `rq3/*.py`, `dataset/*.py`, `check_reproduction.py` | Executed as steps |
| `data/analysis/run_logs/` | Written, gitignored: the pipeline execution state, the run lock and the logs |
| `test_run_analysis.py` | Tests |

## Usage

```bash
python3 run_analysis.py --list
python3 run_analysis.py
python3 run_analysis.py --from rq3_adoption_timeline
```

## Tests

See `test_run_analysis.py`, a standard library `unittest` suite that reads `git ls-files` to determine which files are tracked.

- **`TestStepList`**: Every script that the check runs or names in `NOT_RUN` here is a step, and nothing else is. Every step runs in the data directory, and its script is here. The keys are the script paths with the directory as prefix. Every analysis is recorded by its inputs. Every output that a step declares is a tracked file, or an ignored file for a script that the check does not run, and a step declares every tracked output. The output CSVs that every step reads are tracked or alternatives. The check runs last and after every analysis. Every output CSV that a script names in quotes is among its `inputs`
- **`TestEveryOutputCsvAScriptOpensIsDeclared`**: Each script runs once over the fixture, and every output CSV or commit archive that it opens, through Python or through a subprocess such as `7zz`, is among its step's `inputs`. The steps that read the classification labels, the sampling frame or the validated tree are left out when these inputs are not on disk, and so is a script that the check does not run
- **`TestListAndARun`**: `--list` prints the stage and its steps, and the runner reruns the figure that reads a changed output CSV
- **`TestAnalysisInputChanges`**: A change to any volume of the split commit archive invalidates the two commit figures, with or without the extracted CSV beside the volumes, and so does a change to the CSV. A change to the archive of the classification labels invalidates the two steps that read the labels, and a change to the archive of the sampling frame its four, while the extracted files are on disk. A change to `_shared.py` or to the path heuristics invalidates every analysis step, and a change to `rq2/_repo_metadata.py` the two scripts that import it. A change to the classification labels invalidates the two steps that read them, and a change to the sampling frame its four. A changed listed file of the validated tree invalidates the content-quality step, and so do a file that no CSV lists and that a listed repository's GitHub Agentic Workflow imports, the file that a listed rule's marker names, and the removal of the planning document beside a listed empty anchor. A listed skill that is the marker of a link with a target that the tree lacks is fingerprinted as the marker. A listed context file that is a link without a target leaves the step and the reproduction check done, and a link that names another target invalidates the step. Another file that no CSV lists does not, and a listed file that is gone leaves the step without a fingerprint

Run with:

```shell
python3 -m unittest test_run_analysis -v
```

## Verification

1. `python3 run_analysis.py --list` prints `The stage analysis runs these steps in order.` and its steps, each with its script under `4_data-analysis/` and `(data directory analysis)`
2. After a run, edit one row of `data/collection/output/repos.csv` and run `python3 run_analysis.py` again. The runner reruns the steps with `inputs` that name the file and skips the rest
