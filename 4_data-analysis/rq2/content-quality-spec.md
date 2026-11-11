# Content quality of the configuration artifacts

## Context

`content_quality.py` reads from the validated tree every file that the ten mechanism CSVs name. For a file that is the collection's marker of a symlink, except a context file, it reads the file that the marker names in the repository. It logs, per mechanism, the number of configuration artifacts, the mean, median and quartiles of the line count, <!--s:parameters.4_data_analysis.rq2.content_quality.bins.count:words-->nine<!--/s--> line-count bins and the number of files over <!--s:parameters.4_data_analysis.rq2.content_quality.gt_500-->500<!--/s--> lines, and writes them as a CSV and a LaTeX table. It also tests whether each file is empty (i.e., whitespace-only text, an empty JSON object or array, or whitespace-only TOML). It counts with 0 lines an empty file that the collected file cleanup keeps, and writes no table when another listed file is empty.

## Key decisions

- **Language:** Python 3.14, in the single script `content_quality.py`, with `pandas` and `numpy`
- The tree is `_shared.VALIDATED_DIR`, `data/collection/data/validated/`, which `DATA_COLLECTION_OUTPUT` does not redirect. With `DATA_COLLECTION_OUTPUT` pointed at the fixture, the script reads the fixture's CSVs and looks for their files under the real tree. `check_reproduction.py` lists `data/collection/data/validated/*§*` as the input and skips the script where the tree is absent
- The table counts no empty file. A listed file that is empty and that the collected file cleanup does not keep stops the run. The cleanup deletes every empty file before detection, with the test that `is_empty_file` applies here, except the files of `collected_file_cleanup.kept_when_empty`:
  - a file that a GitHub Agentic Workflow on one of the engines imports, because gh-aw does not compile a workflow without a required import
  - the prompt file that an agent action reads, that is, the file named by the `prompt-file` input of the Codex action or by the `prompt_file` input of the Claude Code Base Action
  - a planning document or its anchor, such as the `.cursor/commands/van.md` beside which the tasks of cursor-memory-bank count
  - a CLAUDE file that determines whether Claude Code reads an AGENTS.md (`imports.deciding_claude_files`)
- The detection step lists a file of this set when its path is a configuration artifact, such as an agent of the flat `.github/agents` pass, a prompt of the `.github/prompts` pass or a Cursor command. `kept_when_empty` reads the set in the validated tree with the cleanup's function, and the script therefore exempts the files that the cleanup keeps. The script reads the set only for a listed file that is empty. Such a file counts with 0 lines, whatever whitespace it contains, and in no bin. `main` logs the number of such files per mechanism. Treated as an empty file that stops the run, such a file would fail the step again in every run. Columns for empty files would count the files of the set alone, because the tree has lost every other empty file
- The deletion log of a collection records empty files at the paths of configuration artifacts, such as a nested `CLAUDE.md`, `.claude/settings.json` and `.cursor/commands/*.md`, which no count of this table could include. The script does not count them per mechanism from that log, `data/collection/deletion.csv`, because the log is a gitignored record that every run rewrites, and assigning a deleted file to a mechanism would require the detection's content tests over a file that no longer exists
- When a listed file is empty and outside the set, `main` exits 1 with an `ERROR` that names these files per mechanism and writes neither output, because the tree is then not the tree that the CSVs describe
- A missing file is counted as unread. It stays in `total`, has no line count, and is logged once per mechanism on stderr as `WARNING rq2_content_quality: Could not read k of the N files that <mechanism CSV> lists from <validated tree>.`. `detection.read_artifact`, through which the script reads the file, also logs a `WARNING detection:` line that names it. Counting it as a non-empty file would claim content for a file that nobody read. When a file is unread, `main` exits 1 with an `ERROR` that names the unread count per mechanism and writes neither output, because a written table would state that every listed file was read
- Emptiness depends on content and extension. Every file is empty when `not content.strip()`, and a `.json` file also when it parses to `{}` or `[]`, with a parse error read as content. A `.toml` file gets the whitespace test alone, and a TOML file of comments alone is therefore content, which the suite checks. A parse error is a `ValueError` or the `RecursionError` of a document nested deeper than the parser reads. The collected file cleanup reads both as content, and the cleanup and the detection step keep such a file. Catching only the `JSONDecodeError` would let a listed settings file nested that deep fail the step in every run. No other analysis script parses a collected file
- Line counts are `len(detection.split_lines(content))`, the count of the detection step, which gives an empty file 0 lines and counts a whitespace-only line as one. A line ends at LF, CRLF or CR, and a form feed, U+001D or U+2028 inside a line, such as the U+001D in a skill of EndstoneMC/endstone, stays in its line. `str.splitlines` would end a line at each of them
- For a listed file that is the collection's marker of a symlink, a file of one line that contains the link's target, the script counts the lines of the file that the marker names inside the repository. That file is the file that the detection step reads for the `language` column (`detection.measured_artifact`). The tool reads the agent, the rule, the command or the skill through the link, and the row exists because of the front matter of that file. Counted at the link's path, each such configuration artifact would be a file of one line. A context file that is a link counts as the marker, because the detection step publishes it as a reference to the file that it links to. The script takes the text from `detection.artifact_text`, the text that the detection step measures, which is the marker also where the tree holds the link as a real symlink. rsync keeps a link inside a directory that the collector copies whole, such as gomlx/gomlx's `.agents/CLAUDE.md`, which links to the `AGENTS.md` beside it. Read through the link, that context file would count the lines of the AGENTS.md. The emptiness test and the test for the set that the cleanup keeps read the file that is counted. For a real symlink, the emptiness test reads through the link as the cleanup's `is_empty_file` does, and a real symlink without a target is therefore not empty and counts as its marker
- The marker of a link with a target that the tree lacks counts in `total` and `target_absent` with no line count. `detection.measured_artifact` returns None for such a marker, as for each of mathlab/pina's skills, which link to `.opencode/skills`. The marker's one line is a path, and the detection step therefore publishes the row's `language` blank, which records that the file was not read. Counted as the marker, each such skill would be a file of one line. As for an unread file, `analyze_artifact_type` drops the blank line count before it computes the mean, the median and the quartiles. These statistics therefore describe the files with a line count, and the bins and `gt_500` count only such files. Unlike an unread file, such a marker does not stop the run. The tree is the tree that the CSVs describe, and the collector never copies a target outside the repository. Stopping on such a marker would therefore fail the step again in every run. `main` logs the count per mechanism, and the CSV leaves it out
- A file that is no text has a NUL byte and does not open with a UTF-16 byte order mark (`detection.no_text`), such as the `CLAUDE.md` of vuejs/router, which git-crypt encrypted. It counts in `total` and `no_text` with no line count, as such a marker does. The detection step publishes its `line_count` blank, because lines read from its bytes would be counted from noise. `main` logs the count per mechanism, and the CSV leaves it out
- The bins are over the non-empty files with a line count, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_1.low-->1<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_1.high-->5<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_2.low-->6<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_2.high-->10<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_3.low-->11<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_3.high-->20<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_4.low-->21<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_4.high-->50<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_5.low-->51<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_5.high-->100<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_6.low-->101<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_6.high-->200<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_7.low-->201<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_7.high-->500<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_8.low-->501<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_8.high-->1,000<!--/s-->, <!--s:parameters.4_data_analysis.rq2.content_quality.bin_9.low-->1,001<!--/s--> and up. `gt_500` is the last two together
- `main()` exits 1 before reading any CSV when the tree directory is absent, with a message that names the missing directory and the procedure for a working copy of the handover in `docs/development.md#handover-working-copy`
- The repository directory is `agentcontext.repos.repo_name_to_dir`, the naming that `agentcontext/repos.py` defines
- `run_analysis.py` fingerprints the files that the script reads. The `inputs_of` of the step covers the files of the validated tree that the mechanism CSVs list, the files that the markers among them name, and the files of their repositories that the collected file cleanup keeps even when they are empty (`../run-analysis-spec.md`). A change to one of these files therefore reruns the step even when the CSVs are unchanged

## Script: `content_quality.py`

### CLI interface

No CLI. The script reads the output CSVs from `_shared.DATA_DIR` (`DATA_COLLECTION_OUTPUT`) and the files from `_shared.VALIDATED_DIR`, and writes its outputs to `data/analysis/rq2/` or under `$ANALYSIS_OUTPUT_DIR/rq2/`.

### Architecture

`is_empty_file`, `kept_when_empty`, `measured_file` and `read_file` are the per-file steps. `analyze_artifact_type` runs them over one mechanism CSV and returns a dict of statistics. `print_summary_table`, `write_csv` and `write_latex` render the list of dicts, and `_lines` formats one line count for the TeX. `main` checks that the tree exists, runs `analyze_artifact_type` over the ten mechanisms, and writes nothing when a listed file is unread, or empty and outside the set that the cleanup keeps.

### Module-level data

`MECHANISM_OF_CSV` maps each mechanism CSV to its mechanism, from `heuristics.MECHANISM_CSVS`, for `detection.measured_artifact`. `ARTIFACT_TYPES` lists ten `(csv_name, file_column, label)` triples: `context-files.csv`/`context_file`, `rules.csv`/`rule`, `skills.csv`/`skills.md`, `commands.csv`/`command`, `subagents.csv`/`subagent`, `settings.csv`/`settings`, `mcp.csv`/`mcp`, `hooks.csv`/`hook`, `plugins.csv`/`plugin`, `lsp.csv`/`lsp`. `CHECKED_ONLY` names the five counts of the statistics dict that the CSV leaves out, `unread`, `empty`, `empty_kept`, `target_absent` and `no_text`. `OUTPUT_DIR` is the output directory. `DATA_DIR`, `VALIDATED_DIR` and `latex_text`, through which the TeX writes each label, come from `_shared`, `repo_name_to_dir` comes from `agentcontext.repos`, and `heuristics`, `collected_file_cleanup` and `detection` come from the package.

### Function structure

**`is_empty_file(content, filepath)`**: True for `None`, empty or whitespace-only content, and for a `.json` path also for `{}` and `[]`. False otherwise, including JSON that does not parse or is nested deeper than the parser reads

**`kept_when_empty(repo_name)`**: the set of repository-relative paths that the collected file cleanup keeps even when they are empty, read in the repository's directory of the validated tree with `collected_file_cleanup.kept_when_empty`

**`measured_file(repo_name, filepath, mechanism)`**: the path, relative to the repository, of the file with the lines that count for a listed configuration artifact, from `detection.measured_artifact`: the file that a marker at `filepath` names inside the repository, `filepath` itself for a context file or a file that is no marker, and `None` for a marker with a target that the tree lacks

**`read_file(repo_name, filepath)`**: the text of `VALIDATED_DIR / owner§repo / filepath` with undecodable bytes replaced, or `None` when it is not a file or cannot be read

**`analyze_artifact_type(csv_name, file_col, label)`**: the statistics dict for one mechanism CSV: `type`, `total`, `lc_mean`, `lc_median`, `lc_q1`, `lc_q3`, <!--s:parameters.4_data_analysis.rq2.content_quality.bins.count:words-->nine<!--/s--> `bin_*` counts, `gt_500`, and `unread`, `empty` (the empty files outside the set that the cleanup keeps), `empty_kept`, `target_absent` and `no_text`, which the CSV leaves out

**`print_summary_table(results)`**: two logged tables, the counts and the line-count statistics with a TOTAL row, and the bins

**`write_csv(results)`**: `content_quality.csv`, one row per dict

**`_lines(value)`**: a line count without decimals when it is whole, with one otherwise, and a thousands separator

**`write_latex(results)`**: `content_quality.tex`, one row per mechanism and a bold Total row

**`main()`**: exits 1 when the tree directory is absent, and otherwise runs the ten mechanisms, exits 1 when a listed file is unread, or empty and outside the set that the cleanup keeps, logs the empty files of the set, the markers with a target that the tree lacks and the files that are no text, and runs the three renderers

### Processing pipeline

1. Check `VALIDATED_DIR.is_dir()`, and otherwise log the error and exit 1
2. For each of the ten mechanisms: read the CSV, and for each row take the file that it counts (`measured_file`) and the text that the detection step measures in it (`detection.artifact_text`), which reads a file in UTF-16 with its byte order mark and a context file that is a real symlink as its marker. A `None` path is a marker with a target that the tree lacks, and a `None` text is a file that is no text (`detection.no_text`) or else a missing file. Both count with no line count. Test the file for emptiness (`read_file`, `is_empty_file`), and for an empty file read the set that the cleanup keeps in the repository (`kept_when_empty`). Count the lines of the text with the detection step's `split_lines`, and count 0 for an empty file of the set
3. Per mechanism, compute the counts, the line-count statistics over the files with a line count, and the bins over the non-empty files among them
4. If a listed file is unread, or else if a listed file is empty and outside the set, log the error and exit 1. Log the count of the empty files of the set, of the markers with a target that the tree lacks and of the files that are no text
5. Log the two tables
6. Write the CSV and the TeX

### Output

`content_quality.csv`, <!--s:analysis.content_quality.rows:words-->ten<!--/s--> rows, columns `type,total,lc_mean,lc_median,lc_q1,lc_q3,bin_1--5,bin_6--10,bin_11--20,bin_21--50,bin_51--100,bin_101--200,bin_201--500,bin_501--1000,bin_1001+,gt_500`, in `ARTIFACT_TYPES` order.

`content_quality.tex`, a `table` with a `tabular{lrrr}`: `Mechanism`, `Configuration artifacts`, `Lines, median (Q1--Q3)` and `><!--s:parameters.4_data_analysis.rq2.content_quality.gt_500-->500<!--/s--> lines`. It has one row per mechanism, with `--` for the line counts of a mechanism without a file, and a `Total` row with the sums of the configuration artifacts and of the files over <!--s:parameters.4_data_analysis.rq2.content_quality.gt_500-->500<!--/s--> lines.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| The validated tree directory absent | `ERROR rq2_content_quality: The validated tree <path> is missing. Extract a working copy of a handover and run python3 -m agentcontext collected-file-cleanup (docs/development.md#handover-working-copy).` on stderr, exit 1, nothing read or written |
| A mechanism CSV absent | `FileNotFoundError` from pandas after the earlier mechanisms ran, and nothing is written |
| A file that the CSV names is not on disk | Counted in `total` and `unread`, no line count, a `WARNING detection:` line per file and one `WARNING rq2_content_quality:` line per mechanism on stderr. `main` exits 1 and writes nothing |
| A context file that the tree holds as a real symlink, such as gomlx/gomlx's `.agents/CLAUDE.md`, with or without a target in the tree | Counted as its marker, a file of one line |
| A file that the CSV names is empty and outside the set that the cleanup keeps | Counted in `empty`, then `ERROR rq2_content_quality: <n> listed file(s) are empty (<n> in <mechanism>, ...). The collected file cleanup deletes such files. The validated tree does not match the mechanism CSVs, and no table was written.`, exit 1, nothing written |
| A file that the CSV names is empty and in the set that the cleanup keeps: an import of a GitHub Agentic Workflow, the prompt file of an agent action, a planning document or an anchor, or a CLAUDE file that determines whether Claude Code reads an AGENTS.md | Counted in `total` and `empty_kept` with 0 lines and in no bin, and `INFO rq2_content_quality: <n> listed file(s) are empty (<n> in <mechanism>, ...), and the collected file cleanup keeps them. Counted them with 0 lines.` |
| A file that cannot be decoded | Read with replacement characters |
| A `.json` file nested deeper than the parser reads | Content, counted with its lines |
| An empty mechanism CSV | Every count 0, the line-count statistics blank rather than 0.0, which would read as measured, and `--` for its TeX line counts |
| Every mechanism CSV empty | A table of zeros and blanks |
| A path in the CSV that is a directory on disk | Treated as missing |
| A listed file that is the marker of a link with a target that the tree lacks, such as mathlab/pina's `.claude/skills/condition-setup/SKILL.md`, `../../../.opencode/skills/condition-setup/SKILL.md` | Counted in `total` and `target_absent`, with no line count and in no bin, and `INFO rq2_content_quality: <n> listed file(s) are markers of links to a target that the validated tree lacks (<n> in <mechanism>, ...). Counted them without a line count.` The run goes on |
| A listed file that is no text, such as a `CLAUDE.md` that git-crypt encrypted | Counted in `total` and `no_text`, with no line count and in no bin, and `INFO rq2_content_quality: <n> listed file(s) are not text (<n> in <mechanism>, ...), such as files that git-crypt encrypted. Counted them without a line count.` The run goes on |

## Files

| File | Action |
|------|--------|
| `content_quality.py` | Main script |
| `../../data/collection/output/context-files.csv`, `rules.csv`, `skills.csv`, `commands.csv`, `subagents.csv`, `settings.csv`, `mcp.csv`, `hooks.csv`, `plugins.csv`, `lsp.csv` | Read |
| `../../data/collection/data/validated/<owner>§<repo>/...` | Read, every file that the ten output CSVs name or that a marker among them names, and for a listed empty file the files from which `collected_file_cleanup.kept_when_empty` derives the set that the cleanup keeps, such as the repository's workflows and the files that they import, and its planning documents and their anchors |
| `content_quality.csv` | Write |
| `content_quality.tex` | Write |
| `../test_rq2_content_quality.py` | Tests |

## Usage

```bash
# After the collected file cleanup of the current handover has rebuilt data/validated/
python3 content_quality.py
```

## Tests

See `../test_rq2_content_quality.py`, which uses the standard library's `unittest` over pandas and numpy. The suite loads the script by path with `ANALYSIS_OUTPUT_DIR` set to a temporary directory. Every test case that reads a CSV or a file points `DATA_DIR`, `VALIDATED_DIR` and `OUTPUT_DIR` at a tree of a few files and CSVs of a few rows written under a temporary directory. No test case therefore reads the real tree.

- **`TestIsEmptyFile`** (8): Nothing and whitespace are empty, and text is not. `{}` and `[]` in a `.json` file are empty whatever the case of the extension, and JSON with content, `null`, malformed JSON and JSON nested deeper than the parser reads are not. `{}` in a Markdown file is content. A comment-only TOML file is content, and a whitespace-only TOML file is not
- **`TestReadFile`** (5): The `owner§repo` directory, with the name that `agentcontext.repos` gives it. A missing file, a directory, and undecodable bytes replaced
- **`TestAnalyzeArtifactType`** (13): Over five rows with one missing file, the totals, the empty and the unread counts, no share among the statistics, and the warning. The missing file has no line count, and the statistics are over the four files read. The bins and `gt_500`, and every bin edge from <!--s:parameters.4_data_analysis.rq2.content_quality.bin_1.high-->5<!--/s--> to <!--s:parameters.4_data_analysis.rq2.content_quality.bin_9.low-->1,001<!--/s-->. An empty mechanism CSV gives zero counts and blank line counts, and the dict has the CSV columns, `unread`, `empty`, `empty_kept`, `target_absent` and `no_text`. A Claude agent and an Antigravity rule that are markers count the lines of the files that they link to, and a context file that is a marker counts its one line. A context file that is a real symlink counts its marker's one line, with a target in the tree and without one, and the link without a target is neither unread nor empty. A skill that is the marker of a link with a target that the tree lacks counts in `total` and `target_absent`, with no line count and in no bin, and is not unread. A context file that git-crypt encrypted counts in `total` and `no_text`, with no line count and in no bin. A context file in UTF-16 with CRLF counts the lines that the detection step counts, and so does a file with a form feed, a NEL, a vertical tab, U+001D or U+2028 inside a line
- **`TestTheWriters`** (3): A CSV row per mechanism. The TeX rows, a line count with and without decimals and with a thousands separator, the Total row, and no `Empty` column. The logged tables
- **`TestTheArtifactTypes`** (1): every file column of `ARTIFACT_TYPES` is in the header of its CSV under `testdata/output/`
- **`TestMain`** (11): Over ten one-row mechanism CSVs and a tree of two files, ten CSV rows with the columns, and the settings row and the Total row of the TeX. A settings file nested deeper than the parser reads is counted with its one line. A listed file that is empty, a `{}` settings file, writes no table. An empty `.github/agents` file that a GitHub Agentic Workflow imports and `subagents.csv` lists gives a table with the subagent at 0 lines and in no bin, and without the workflow the same file writes no table. An empty `.cursor/commands/van.md` beside cursor-memory-bank's `memory-bank/tasks.md` and an empty Copilot prompt file that the `prompt-file` input of the Codex action names, each listed in `commands.csv`, give a table with the command at 0 lines, and without the tasks the empty `van.md` writes no table. A mechanism with no file has `--` for its line counts in the TeX and adds nothing to the Total row. A rule that is the marker of a link with a target that the tree lacks writes both outputs, with the rule in the total and in no bin, and the count logged. A listed file that cannot be read writes no table, an absent tree exits 1 before writing, and nothing is written beside the script

Run with:

```shell
python3 -m unittest test_rq2_content_quality -v
```

## Verification

1. From `4_data-analysis/rq2/`, run `python3 content_quality.py` over the validated tree that matches the CSVs. It exits 0, and its log has no `WARNING` about a file that it could not read.
2. Check each mechanism's `total` against the row count of its CSV, the line counts against files of known length, and that an empty listed file stops the run.
3. Count the `empty file` rows of the deletion log at configuration paths, which this table does not count.
4. From `4_data-analysis/`, run `python3 check_reproduction.py --only rq2/content_quality`. It exits 0, with `--reproduce-only` on a machine other than the one that wrote the tracked outputs.

## Known limitations

- Because `DATA_COLLECTION_OUTPUT` does not redirect the tree, a run over the fixture's CSVs reads the real tree. A reproduction over the fixture needs the subsample's repositories under that tree
- A TOML file of comments alone counts as content, because `.toml` gets the whitespace test rather than a parse
