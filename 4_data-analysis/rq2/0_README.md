# Data analysis: RQ2

Use [the development guide](../../docs/development.md) for native macOS, native
Linux and x86-64 container setup. Run these commands with that environment's
Python interpreter.

The scripts here analyze how the repositories in the sample adopt the configuration mechanisms of the tools, and how the repositories that adopt them differ from the rest.

`_repo_metadata.py` builds `repo_metadata.csv` by merging `data/classification/repos_annotated.csv` with `data/sampling/ghs_filtered.json`, and identifies the repositories in the sample with the engineered project selection's own function. `repo_metadata_comparison.py` and `language_ranking_all.py` import it, and its spec is `repo-metadata-spec.md`. Where git tracks `repos_annotated.7z` or `ghs_filtered.json.7z` beside the extracted file, as in a checkout, a file that differs from its archive stops the module, and `language_tool_association.py` stops the same way on the sampling frame ([archives](../../docs/archives.md)).

The constants of the tools and the configuration mechanisms (i.e., `TOOLS`, `TOOL_LABELS`, `SPECIFIC_TOOLS`, `MECHANISMS` and `MECHANISM_LABELS`), the paths and the UpSet renderer come from `../_shared.py`, the one module that the analysis scripts share, which `../0_README.md` describes. The root [CLAUDE.md](../../CLAUDE.md#constants) explains the constants. `_repo_metadata.py` measures the age of a repository on the day on which the GHS retrieval was downloaded, and it reads that date from `ghs_filtered.json`.

From the repository root, install the dependency lock `locks/development.txt`, which locks `requirements.txt`, and the `agentcontext` package, which every analysis script imports for its log messages, into `.venv`:

```shell
mise exec -- bash dev/setup.sh
```

Run `repo_metadata_comparison.py` to compare repository age, contributors, commits and size between the repositories that configure each tool and the repositories that do not, with Mann-Whitney U tests. It builds `repo_metadata.csv` through `_repo_metadata.py`, reads it with `repos.csv`, and writes `repo_metadata_comparison.csv` and `repo_metadata_comparison.tex` with the U statistic and p-value of each test, the median and quartiles of each group, and Cliff's delta with its magnitude. For a tool with no adopting repository, the script writes `n/a` and marks the tool's four comparisons as not tested, with `--` for their Cliff's delta in the TeX. It leaves these comparisons out of the Benjamini-Hochberg adjustment, which covers only the comparisons that were tested. The fixture contains repositories with shared configuration only, so that the `no_specific_tool` group is not empty over the fixture. The script's spec is `repo-metadata-comparison-spec.md`.

```shell
python3 repo_metadata_comparison.py
```

Run `language_ranking_all.py` to rank the programming languages of the sample and write a LaTeX snippet of the top <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages-->5<!--/s-->. It joins `data/classification/repos_annotated.csv` with `data/sampling/ghs_filtered.json` through `_repo_metadata.py`, which also writes `repo_metadata.csv`. It then ranks the languages of the joined repositories that are in the sample (i.e., labeled `engineered_project=true`) by frequency and writes the ranking to `language_ranking_all.csv` and `top5_languages_all.tex`. The script's spec is `language-ranking-all-spec.md`.

```shell
python3 language_ranking_all.py
```

Run `language_tool_association.py` to find the programming languages most often associated with each tool. It reads `data/sampling/ghs_filtered.json` and `data/collection/output/repos.csv` and writes:

- `language_ranking.csv`, the ranking of the languages of the configured repositories
- `top5_languages.tex`, a LaTeX snippet of the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.top_languages-->5<!--/s--> languages
- `language_tool_heatmap.pdf`, a heatmap of the tools over the top <!--s:parameters.4_data_analysis.rq2.language_tool_association.heatmap_languages-->10<!--/s--> languages
- `context_file_language_adoption.csv`, the context file adoption rate per language

The script's spec is `language-tool-association-spec.md`.

```shell
python3 language_tool_association.py
```

Run `config_tool_heatmap.py` to draw a heatmap of the configuration mechanisms that the repositories use with each tool. It reads `repos.csv` and the `tool` column of the ten mechanism CSVs and writes `config_tool_heatmap.pdf`. A cell of the heatmap counts the repositories with a configuration artifact of the mechanism attributed to the tool, and a last column counts the repositories with no specific tool. The script's spec is `config-tool-heatmap-spec.md`.

```shell
python3 config_tool_heatmap.py
```

Run `config_cooccurrence.py` to test which configuration mechanisms the repositories use together. Over the repositories in `repos.csv` with at least one mechanism, it tests each pair of mechanisms with the chi-squared test, or with Fisher's exact test where a cell has fewer than <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.MIN_CELL_COUNT-->5<!--/s--> observed or expected repositories. The signed phi coefficient and Cramér's V measure the effect, and the Benjamini-Hochberg correction adjusts the p-values of the tested pairs. The script writes `config_cooccurrence.csv` and `config_cooccurrence.tex`, and its spec is `config-cooccurrence-spec.md`.

```shell
python3 config_cooccurrence.py
```

Run `tool_count_distribution.py` to count how many tools each repository configures. From `repos.csv` it draws a bar chart of the repositories by their number of configured tools, from 0 to <!--s:parameters.4_data_analysis.rq2.tool_count_distribution.max_tools-->6<!--/s-->, with counts and percentages on the bars. A count of 0 means that none of the six tool flags is set. The script writes `tool_count_distribution.pdf`, `tool_count_distribution.csv` and `tool_count_distribution.tex`, and its spec is `tool-count-distribution-spec.md`.

```shell
python3 tool_count_distribution.py
```

Run `tool_cooccurrence.py` to count how often each pair of tools is configured in the same repository. From `repos.csv` it builds a symmetric 7×7 matrix over the six tools and the repositories with no specific tool, with each count also given as a percentage of the smaller tool's total. It writes `tool_cooccurrence.pdf`, an annotated heatmap, and `tool_cooccurrence.csv` and `tool_cooccurrence.tex`. The script's spec is `tool-cooccurrence-spec.md`.

```shell
python3 tool_cooccurrence.py
```

Run `venn_diagram.py` to show how many repositories configure several tools at once. From `repos.csv` it draws a Venn diagram of the overlaps between the six tools, with AGENTS.md shown separately, and writes it to `venn-tool-usage.pdf`. The script's spec is `venn-diagram-spec.md`.

```shell
python3 venn_diagram.py
```

Run `upset_plot.py` to draw an UpSet plot of the intersections of seven categories, the six tools and AGENTS.md. It reads `repos.csv` and writes `upset-tool-usage.pdf`. The plot leaves out intersections of fewer than <!--s:parameters.4_data_analysis.rq2.upset_plot.MIN_SUBSET_SIZE:words-->five<!--/s--> repositories unless `--min-count` lowers that threshold, as a small input such as the fixture under `../testdata/` requires. The script's spec is `upset-plot-spec.md`.

```shell
python3 upset_plot.py
python3 upset_plot.py --min-count 1
```

Run `config_mechanism_upset.py` to draw an UpSet plot of how the ten configuration mechanisms (i.e., Context Files, Settings, Skills, Subagents, Commands, Hooks, Rules, MCP, Plugins, LSP) co-occur in the repositories with at least one mechanism. It reads `repos.csv` and writes `config_mechanism_upset.pdf` with a log-scale y-axis. The script's spec is `config-mechanism-upset-spec.md`.

```shell
python3 config_mechanism_upset.py
```

Run `content_quality.py` to count the configuration artifacts of each mechanism and their length in lines. It reads the ten mechanism CSVs in `data/collection/output/` and every file that they list from the validated tree at `data/collection/data/validated/`, which a working copy of the current handover supplies ([development](../../docs/development.md#handover-working-copy)). It writes per-mechanism statistics to `content_quality.csv` and `content_quality.tex`. The collected file cleanup deletes every empty file before detection and records it in the deletion log. The cleanup keeps an empty import of a GitHub Agentic Workflow, the prompt file of an agent action, a planning document or its anchor, and a CLAUDE file that determines whether Claude Code reads an AGENTS.md. The script counts such a file with 0 lines when a CSV lists it. Any other listed file that is empty (i.e., whitespace-only text, an empty JSON object or array, whitespace-only TOML) stops the script before it writes anything. The script's spec is `content-quality-spec.md`.

```shell
python3 content_quality.py
```

## Tests

Every script here has a spec beside it, `<name-with-dashes>-spec.md`, and a stdlib `unittest` suite in the directory above, `../test_rq2_<name>.py`. A suite loads its script by path, sets `ANALYSIS_OUTPUT_DIR` to a temporary directory before the script's `OUTPUT_DIR` is bound, and replaces the module's input paths with small CSVs written under a temporary directory. No test case therefore reads the validated tree or writes into this directory. Only the fixture test case of `test_rq2_repo_metadata_comparison` reads the extracted `data/classification/repos_annotated.csv` and `data/sampling/ghs_filtered.json`, and it is skipped when the two files are not extracted. The suites also run each script that computes its numbers inside `main()` as a subprocess over the fixture under `../testdata/output/` and compare its output with values derived from the fixture.

- `test_rq2_repo_metadata`: the reference date, the join and the sample of `_repo_metadata.py`
- `test_rq2_config_cooccurrence`: Cramér's V and phi, the choice between the chi-squared and Fisher's exact test, the <!--s:analysis.config_cooccurrence.pairs-->45<!--/s--> pairs and the pairs not tested, the combination of duplicate rows with OR, and the TeX matrix
- `test_rq2_config_mechanism_upset`: the flags, the intersections passed to the renderer, the PDF
- `test_rq2_config_tool_heatmap`: the count and share matrices passed to seaborn, the PDF
- `test_rq2_content_quality`: emptiness by content and extension, the per-mechanism statistics and bins, the TeX, an empty file that the collected file cleanup keeps counted with 0 lines, such as an import of a GitHub Agentic Workflow, a prompt file of the Codex action or an anchor of planning documents, the exit 1 over another empty file or an unread file, the exit when the tree is absent
- `test_rq2_language_ranking_all`: the ranking and the top <!--s:parameters.4_data_analysis.rq2.language_ranking_all.top_languages:words-->five<!--/s--> over a stubbed and over the real helper, the sample as the population, and a name such as C# written as LaTeX that prints it
- `test_rq2_language_tool_association`: the join, the ranking, a name such as C# written as LaTeX that prints it, the heatmap cells, the adoption rates
- `test_rq2_repo_metadata_comparison`: the seven helpers, the <!--s:analysis.repo_metadata_comparison.comparisons-->28<!--/s--> comparisons and their adjustment, the `n/a` of an empty group, its comparisons marked as not tested, and the adjustment without them
- `test_rq2_tool_cooccurrence`: the <!--s:analysis.tool_cooccurrence.pairs-->21<!--/s--> pairs, the diagonal, the share of the smaller tool
- `test_rq2_tool_count_distribution`: the <!--s:analysis.tool_count_distribution.buckets:words-->seven<!--/s--> buckets, the synthesized `no_specific_tool`, the TeX
- `test_rq2_upset_plot`: the rename limited to the seven columns, `--min-count`, the fixture with and without it
- `test_rq2_venn_diagram`: the loader's coercion against the fixture's header, the callout

Run the suites from this directory:

```shell
cd ..
python3 -m unittest test_rq2_repo_metadata test_rq2_config_cooccurrence \
  test_rq2_config_mechanism_upset test_rq2_config_tool_heatmap test_rq2_content_quality \
  test_rq2_language_ranking_all test_rq2_language_tool_association \
  test_rq2_repo_metadata_comparison test_rq2_tool_cooccurrence \
  test_rq2_tool_count_distribution test_rq2_upset_plot test_rq2_venn_diagram -v
```
