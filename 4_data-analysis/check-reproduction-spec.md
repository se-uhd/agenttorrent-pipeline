# The reproduction check

## Context

`check_reproduction.py` runs every analysis script twice and logs every output that changed or did not reproduce. The check enforces the convention of [Analysis scripts](../CLAUDE.md#analysis-scripts) that a CSV or TeX output is byte-identical between two runs over the same inputs. A PDF is byte-identical too once `SOURCE_DATE_EPOCH` is fixed, and the check fixes it for every run.

An output *changed* when the first run wrote other bytes than were on disk before it. After a data change, the changed outputs are therefore the figures that the change affects. An output *did not reproduce* when the second run wrote other bytes than the first, and a script *failed* when it exited non-zero. A whole local run also logs each `.pdf`, `.csv` or `.tex` file in an output directory that no script wrote. The comparison covers only what the scripts write, and without this log message a figure that a script stopped writing would stay on disk unchecked. The exit code is a verdict. It is 0 when every script ran, every output reproduced and nothing changed, and 2 when an output changed, did not reproduce or was written by no script. It is 1 when a script failed, an input that it needs is missing, or every selected script was skipped.

CI runs the check with `--skip-missing --reproduce-only`, and `run_analysis.py` runs it with the same flags as its last step. The scripts that read extracted archives or the validated tree run only locally. Only the machine that wrote the tracked outputs can check whether an output changed, because on the CI runner the PDF bytes and the last digits of `rq2/config_cooccurrence.csv` differ from that machine's while every script still reproduces.

## Key decisions

- **Language:** Python 3.14, in the single script `check_reproduction.py`, with the standard library and `agentcontext.logs` for the log format. The analysis scripts run as subprocesses
- The list is in the script. `SCRIPTS` names every analysis script with the inputs that it reads beyond the output CSVs under the data directory. An input is a glob relative to the data root, in which `{data}` stands for the data directory and `|` separates alternatives of which one is enough. `test_run_analysis.py` checks the runner's step list against `SCRIPTS`
- Every run gets the same environment. With `SOURCE_DATE_EPOCH=0`, matplotlib writes the same creation date. With `MPLBACKEND=Agg`, no window opens, and with `PYTHONDONTWRITEBYTECODE=1`, Python writes no cache beside a script. `DATA_COLLECTION_OUTPUT` names the output CSVs. `ANALYSIS_OUTPUT_DIR` is set when `--output-dir` is given and removed from the environment otherwise, so that a root left in the caller's shell cannot send the outputs elsewhere while the tracked directory looks untouched
- An output that was not there before counts as changed. No earlier bytes are on disk to compare with, and the check logs a new file that a script starts writing as it logs a changed file
- Under `--output-dir`, the root is empty before the first run. No output can change there, and the check does not test whether an output changed. `--reproduce-only` skips that test beside the tracked outputs, for a machine other than the machine that wrote them
- A failure takes precedence over a difference. The check returns 1 when a script failed, or when a needed input is missing without `--skip-missing`, before it considers changed or differing outputs, because it could not compare every output of such a run
- A run that skipped every selected script exits 1, with or without `--skip-missing`, and logs an `ERROR` that states that nothing was checked. Exit 0 states that every script ran and reproduced, and a run over a data root without its inputs compared nothing
- A run wrote a file of the script's output directory when the file's modification time is at or after the start of the run. `.py` and `.pyc` files and dotfiles are never outputs
- A script runs from its own directory, as a person runs it, so that a relative path in the script resolves the same way for the check as for the person

## Script: `check_reproduction.py`

### CLI interface

```
python3 check_reproduction.py [OPTIONS]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--skip-missing` | off | Skip a script with a missing input instead of failing |
| `--reproduce-only` | off | Check only whether each output reproduces, not whether it changed |
| `--only TEXT` | *(all)* | Run only the scripts with a path that contains `TEXT` |
| `--data-dir DIR` | `data/collection/output` | The output CSVs to read |
| `--output-dir DIR` | *(the data root's `analysis/`)* | Write under `DIR/<script directory>` instead |
| `--list` | off | Print the scripts, their inputs and which of them run here, and run nothing |

### Architecture

`main` builds the environment, selects the scripts and records the digests on disk before anything runs. For each script, it then checks the inputs, runs the script twice through `run_script`, and compares the three sets of digests. It logs a line per script as it goes, the changed and differing outputs after them, and the summary line last.

### Module-level data

**`ROOT`, `DEFAULT_DATA_DIR`**: the repository root and `data/collection/output/`

**`SCRIPTS`**: the tuple of `(path, needs)` pairs for <!--s:parameters.4_data_analysis.check_reproduction.SCRIPTS.length:words-->twenty-one<!--/s--> analysis scripts under `4_data-analysis/`

**`NOT_RUN`**: the scripts of `rq2/`, `rq3/`, `dataset/` and `../2_classification/diagnostics/` that the check does not run, each with the reason: the four `calibration_*.py` diagnostics. A script in neither list fails `test_check_reproduction`

**`IGNORED_SUFFIXES`**: `.py` and `.pyc`, never an output

### Function structure

**`missing_inputs(needs, data_dir)`**: the needs with no match on disk, each `|` group given as the single string that the list contains

**`output_dir_of(script, output_root)`**: where `_shared.output_dir` sends the script's outputs, `../data/analysis/<directory name>` or `output_root/<directory name>`

**`digests(directory)`**: sha256 per file of the directory, skipping scripts, caches and dotfiles, and empty for an absent directory

**`run_script(script, env, out_dir)`**: one run from the script's directory, which returns the exit code, the last <!--s:parameters.4_data_analysis.check_reproduction.stderr_tail_lines:words-->three<!--/s--> lines of stderr, and the digests of the files written

**`main(argv)`**: the flags, the environment, the runs, the log messages and the verdict

### Processing pipeline

1. Parse the flags, resolve the data directory and the output root, and build the environment
2. Select the scripts by `--only`, and return 1 when the selector matches none of them, because exit 0 states that every script ran and every output reproduced. With `--list`, log each script with `can run here`, or with `cannot run here` and its missing inputs, and return 0
3. Record the digests of every selected script's output directory
4. For each script, skip it when an input is missing. Otherwise run it, record a failure on a non-zero exit, and run it again. Compare the two runs, and, when the check tests whether outputs changed, compare the first run with the digests recorded before it
5. When the check tests whether outputs changed and the run was whole, with no `--only`, no skip and no failure, list the `.pdf`, `.csv` and `.tex` files that were in a script directory before the run and that no first run wrote
6. Log the `CHANGED`, `DIFFERS` and `UNWRITTEN` lines and the summary. Return 1 on a failure, on a skip without `--skip-missing` or when every selected script was skipped, else 2 on a difference, else 0

### Output

The check logs `INFO reproduction_check:` lines: one `ok`, `SKIP` or `FAIL` line per script, then `CHANGED  <path>.`, `DIFFERS  <path>.` and `UNWRITTEN <path> (no script wrote it).` lines, and last `<n> script(s) ran twice, <n> skipped, <n> failed. <n> output(s) changed, <n> did not reproduce.`, with `, <n> written by no script` before the final period when there are any. The scripts write their outputs under the data root (`_shared.output_dir`) or under `--output-dir`.

## Error handling & edge cases

| Scenario | Behavior |
|----------|----------|
| Every script ran, every output reproduced, nothing changed | Exit 0 |
| An output changed, or did not reproduce | Exit 2, the paths listed |
| A script exited non-zero, on either run | `FAIL` with the exit code and the stderr tail, and exit 1 |
| An input missing | `SKIP` naming it, and exit 1, or a skip under `--skip-missing` |
| Every selected script skipped, under `--skip-missing` too | `ERROR reproduction_check: All <n> selected script(s) were skipped. Nothing was checked.`, and exit 1 |
| A failure and a difference in one run | Exit 1 |
| A skip under `--skip-missing` and a difference | Exit 2 |
| An output written for the first time | `CHANGED`, unless `--reproduce-only` or `--output-dir` |
| A `.pdf`, `.csv` or `.tex` file in an output directory that no script wrote | `UNWRITTEN`, and exit 2. Checked only in a whole run without `--reproduce-only` or `--output-dir` |
| `ANALYSIS_OUTPUT_DIR` in the caller's environment and no `--output-dir` | Removed for the runs |
| A `.pyc`, a `.py` or a dotfile in the output directory | Not an output |
| `--list` | Runs nothing, writes nothing, returns 0 |

## Files

| File | Action |
|------|--------|
| `check_reproduction.py` | Main script |
| The scripts that `SCRIPTS` names under `rq2/`, `rq3/` and `dataset/` | Executed twice each |
| The output CSVs under `--data-dir`, and the inputs that `SCRIPTS` names | Read by the scripts |
| Every output under `../data/analysis/`, or under `--output-dir` | Written by the scripts, twice |
| `test_check_reproduction.py` | Tests |

## Usage

```bash
cd 4_data-analysis
python3 check_reproduction.py                                    # the local check, every script
python3 check_reproduction.py --skip-missing --reproduce-only    # what CI runs
python3 check_reproduction.py --only rq3                         # a subset, by substring
python3 check_reproduction.py --data-dir testdata/output --output-dir /tmp/figures --skip-missing
python3 check_reproduction.py --list
```

## Tests

See `test_check_reproduction.py`, a standard library `unittest` suite. It loads the check by path and replaces its `SCRIPTS` and `ROOT` with a temporary tree of small stand-in scripts. The stand-ins write the same bytes on every run, write other bytes on every run, lack an input, exit non-zero, or write the environment that they run under. Every verdict is then one run of `main` in-process, and no real script runs. The suite checks the real list against the scripts on disk and prints it with `--list`. `tests/test_check_modes.py` runs one real script through the check in both directions.

- **`TestMissingInputs`**: A script with no needs misses nothing, and `{data}` stands for the data directory. One alternative is enough, and a missing group is named whole. A relative glob resolves against the data root, and a recursive glob finds a nested file
- **`TestOutputDirAndDigests`**: Without a root, a script writes under the data root, and under a root the directory name is kept. An absent directory has no digests, and scripts, caches and dotfiles are not outputs
- **`TestTheVerdicts`**: A script that reproduces and changes nothing exits 0, and an output that was not there before counts as changed. An output that changed exits 2, and `--reproduce-only` does not check it. An output that does not reproduce exits 2, with `--reproduce-only` too. A missing input exits 1, is a skip under `--skip-missing` beside a script that ran, and exits 1 when every script was skipped, and the script runs once the input is there. A failing script exits 1, and so does a script that fails on its second run even when its bytes match, and an `--only` that matches no script. A failure outranks a difference, and an allowed skip does not hide a difference. A figure that no script wrote exits 2, and neither `--only` nor `--reproduce-only` checks for it. A CSV or TeX table that no script wrote exits 2, and another file does not
- **`TestSelectionAndListing`**: `--only` selects by substring. `--list` names every script, runs none and respects `--only`
- **`TestTheOutputRoot`**: `--output-dir` keeps the tracked directory untouched, and a difference is still logged under it. A script runs under the environment that the check sets, and without `--output-dir` the override is cleared
- **`TestTheRealList`**: Every script of the analysis directories and the diagnostics is run or named in `NOT_RUN`, and a script that is not run is named in its directory's README. Every listed script is on disk, the commit scripts accept the archive, and `--list` over the real list names every script

Run with:

```shell
cd 4_data-analysis
python3 -m unittest test_check_reproduction -v
```

## Verification

1. `python3 check_reproduction.py --skip-missing --reproduce-only` exits 0 on any checkout with the output CSVs
2. On the machine that wrote the tracked outputs, `python3 check_reproduction.py` exits 0 after the archives are extracted and the validated tree is restored
3. In a disposable checkout, so that no unrelated edit is lost, edit one row of `data/collection/output/repos.csv` and run the check again. It exits 2, with `CHANGED` lines that name the figures that read the file

## Known limitations

- The bytes of the tracked PDFs depend on the machine that wrote them. On the Linux runner, PDFs and the last digits of `rq2/config_cooccurrence.csv` differ while every script reproduces. Only a local run therefore checks whether an output changed, and CI checks reproduction alone
- `rq2/upset_plot.py` fails over the fixture, because the smallest intersection that it plots is above the fixture's largest. `rq2/repo_metadata_comparison.py` needs repositories with no specific tool, which the fixture contains for this reason. The four `calibration_*.py` diagnostics are in `NOT_RUN`, with the reasons that the list gives
- The check does not see what a script writes into a directory other than its output directory, because it takes the digests over that directory alone
