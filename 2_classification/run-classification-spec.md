# Run the classification stage

## Context

`run_classification.py` runs the two steps of the classification stage through `../agentcontext/runner.py`. It uses the lock, the pipeline execution state, the pre-checks, the resume rules and the stop handling of the runner unchanged (`../docs/runner.md`). `../run_pipeline.py` runs the same steps after the sampling stage and before the collection.

## Key decisions

- The stage has two steps, the labeling and the selection. `repository_labeling` runs `1_classify_repos.py` (`classify-repos-spec.md`). A run of that script over the whole sampling frame ends by comparing its labels with the labels of the previous run and stating why each changed label changed (`changed-labels-spec.md`). The comparison is not a step of its own, because it needs the finished run, which exists only at the end of the labeling, and because it labels each changed repository again with the classifier of the run. `engineered_project_selection` then writes the sample, the repositories labeled `true`, which the collection clones (`../docs/steps/engineered_project_selection.md`). The module of `engineered_project_selection` is part of the package, because the container runs the step list of the collection alone and that list starts with the same step. `run_classification.py` takes the step from that list unchanged and adds only that the step consumes the labeling. The label evaluation (`evaluate-labels-spec.md`) is not a step, because it runs once for a model and a prompt, not for every sampling frame
- The runner records the labeling with its inputs (`COMPLETION_INPUTS`): the sampling frame and, as `code_inputs`, the two prompts and the modules that the script imports or starts, `label_cache.py`, `readmes.py`, `forks.py`, `classification_tools.py`, `endpoints.py`, `mcp_bridge.py`, `changed_labels.py` and `costs.py`, beside the script itself. The runner therefore runs the labeling again after a new sampling frame or a changed prompt or module, also after a change that decides no label, such as one to the cost record. No other change needs the step again. Over an unchanged sampling frame, model and classification parameters, the step that runs again completes the latest finished run without a new run when the current rules derive each of its decisions unchanged. It then requests no README and writes its outputs again from the cache of labels, and the sample stays (`complete_finished_run`, `classify-repos-spec.md`). A new run would request every README again, drop from the sample each repository that GitHub has stopped serving, and make the cloning step begin a new collection. A decision that the current rules derive differently starts a new run, and so does `--step-arg repository_labeling:--new-run`. The prices of the models, `config/model_prices.csv`, are no input of the step, because they decide no label, and `costs.py` writes the cost record of any run again from the token counts in the tokens file of the run, `<id>.tokens.csv`, and in `../data/classification/labels/index.csv` (`costs-spec.md`). The runner never records the step as complete from an output on disk, because the outputs of the previous sampling frame are on disk after every run. It records the selection by its script
- Before the first step starts, the pre-checks test for what the labeling needs: an OpenRouter API key, for the default model, and a GitHub access token, in the environment or in `credentials.csv`, and the packages `openai`, `mcp`, `pydantic`, `lingua`, `html2text` and `requests`. The labeling needs no binary, because GitHub's API returns the README and the outline of the files, and neither git nor Linguist runs
- The labeling names the sampling frame in `reads`, at the path where the sampling stage writes it, so that the pre-checks require the file. `run_pipeline.py` gives the labeling no edge to the filter of the sampling stage. It records the labeling by the repositories in the sampling frame and by the values of its `_metadata` that the labeling reads (`sampling_frame_contents`). A run of the filter that writes the same repositories therefore leaves the labeling complete
- The selection alone writes `../data/classification/repos_annotated_included.csv`, which it derives from `../data/classification/repos_annotated.csv`
- **Disk:** <!--s:parameters.2_classification.run_classification.MIN_FREE_DISK_GB:words-->two<!--/s--> gigabytes, as the sampling stage and the analysis stage require. The labeling clones nothing. It writes the partial file of the run, one decision per repository at about <!--s:classification_2026_10_23.partial_kb_per_decision-->29<!--/s--> kB each in run `2026-10-23T093036Z`, and the cache of labels

## Script: `run_classification.py`

### CLI interface

```
python3 run_classification.py [--list] [--from NAME] [--step-arg NAME:ARGS] [--mark-complete STEP]
```

| Flag | Description |
|------|-------------|
| `--list` | Print the stage with its steps and exit |
| `--from` | Run this step again with every step computed from it, or both steps with `--from classification` |
| `--step-arg` | Append arguments to one step for this run, such as `repository_labeling:"--workers 4"` |
| `--mark-complete` | Record a step as complete from the output already on disk. The runner exits 1 when the command line also has `--list`, `--from` or `--step-arg`, which `--mark-complete` would ignore |

### Steps

| Step | Script | Recorded by | Reads | Needs |
|-------|--------|-------------|-------|-------|
| `repository_labeling` | `1_classify_repos.py` | the sampling frame, the two prompts, the script and its eight modules | `../data/sampling/ghs_filtered.json` | `OPENROUTER_API_KEY`, `GITHUB_TOKENS` or `GITHUB_TOKEN`, or `credentials.csv`; `openai`, `mcp`, `pydantic`, `lingua`, `html2text`, `requests` |
| `engineered_project_selection` | `python3 -m agentcontext.steps.engineered_project_selection` | its script | `../data/classification/repos_annotated.csv` | n/a |

## Error handling & edge cases

The runner handles errors as `../docs/runner.md` describes. When the pre-checks find a credential or a package missing, the run stops before the labeling starts.

## Files

| File | Action |
|------|--------|
| `run_classification.py` | The runner |
| `../data/classification/run_logs/` | The lock, the pipeline execution state and the logs |
| `test_run_classification.py` | Tests |

## Usage

```bash
python3 run_classification.py --list
python3 run_classification.py
python3 run_classification.py --step-arg repository_labeling:"--workers 4"
```

## Tests

See `test_run_classification.py`, which uses the standard library's `unittest`. The tests compare the step list with the scripts and with the commands of `0_README.md`. One test runs the stage in a temporary directory, with a stand-in step that writes `repos_annotated.csv` in place of the labeling.

- **`TestStepList`** (8): the stage labels, compares and selects; the selection is the package's step and follows the labeling; the labeling's command is the one that the README documents; what decides a label runs the labeling again; the sampling frame is read where the sampling stage writes it; the credentials and packages are checked before the labeling runs; no binary is needed; the included list is declared by the selection alone
- **`TestListAndARun`** (3): the list prints the stage and its two steps; a run over a stand-in records the step as complete in the stage's pipeline execution state; `--mark-complete` refuses a step argument that it would ignore

Run with:

```shell
python3 -m unittest test_run_classification -v
```
