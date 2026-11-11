# Data analysis: RQ3

Use [the development guide](../../docs/development.md) for native macOS, native
Linux and x86-64 container setup. Run these commands with that environment's
Python interpreter.

The scripts here examine when the earliest configuration artifacts of repositories were created, how the context file categories co-occur and reference each other, and how many configuration artifacts a repository has. A file is dated by the commit that created it under whatever name it had then. For a renamed file, or for a file that a configuration names, that date can be years before the file was renamed to its current path or the configuration began to name it.

`DATA_DIR`, `CONTEXT_FILE_CATEGORIES` and `COLORS` come from `../_shared.py`, the module that every analysis script imports, which `../0_README.md` describes. The module derives the categories from `heuristics/config_path_patterns.csv`.

Every analysis script imports `agentcontext` for logging. From the repository root, install the
dependency lock `locks/development.txt`, which locks `requirements.txt`, and the package into
`.venv`:

```shell
mise exec -- bash dev/setup.sh
```

Run the scripts from this directory. Each writes its PDFs into `data/analysis/rq3/` under the data root.

Run `adoption_timeline.py` to plot, for each context file category, for skills and for subagents, the cumulative number of repositories per month, counting each repository from the month in which its earliest file of that category or mechanism was created. It reads `data/collection/output/context-files.csv`, `data/collection/output/skills.csv` and `data/collection/output/subagents.csv` and writes `cumulative_repo_adoption.pdf`.

```shell
python3 adoption_timeline.py
```

Run `cooccurrence_analysis.py` to analyze how the context file categories co-occur within repositories, in what order they are created, and how they reference each other. It reads `data/collection/output/context-files.csv` and writes `creation_order_sequences.pdf` (a horizontal bar chart), `reference_network.pdf` (a heatmap) and `cooccurrence_matrix.pdf` (a symmetric matrix).

```shell
python3 cooccurrence_analysis.py
```

Run `count_distribution.py` to plot how many context files, skills and subagents a repository has, which shows whether repositories tend to have one configuration artifact or several. It reads `data/collection/output/context-files.csv`, `data/collection/output/skills.csv` and `data/collection/output/subagents.csv` and writes `count_distribution.pdf`.

```shell
python3 count_distribution.py
```

## Tests

Each script has a stdlib `unittest` suite one directory up and a spec beside it: `adoption-timeline-spec.md`, `cooccurrence-analysis-spec.md` and `count-distribution-spec.md`. Every test case runs offline and writes only under a temporary directory. The suites test the functions of each script over DataFrames built in the test. They also run each script once as a subprocess over the fixture `../testdata/output`, with `DATA_COLLECTION_OUTPUT` set to the fixture and `ANALYSIS_OUTPUT_DIR` set to the temporary directory.

- `../test_rq3_adoption_timeline.py`: the earliest month per repository, the cumulative count, the style guard, and the per-series counts over the fixture
- `../test_rq3_cooccurrence_analysis.py`: the three classifiers, a path that no path pattern matches read as a file that a configuration named, the sequences, the reference edges, the symmetric matrix, and the counts over the fixture
- `../test_rq3_count_distribution.py`: the bins, the rows per repository, and the statistics over the fixture

Run the suites from `4_data-analysis/`:

```shell
cd ..
python3 -m unittest test_rq3_adoption_timeline test_rq3_cooccurrence_analysis test_rq3_count_distribution -v
```
