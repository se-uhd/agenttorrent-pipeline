# CLAUDE.md

This file contains the project instructions that Claude Code and Codex share, and it describes how
to work in this repository. Claude Code loads it directly, and `AGENTS.md` instructs Codex to read
it in full. `docs/` and the specs beside the scripts describe what the code does, and this file
points there rather than repeating them.

## The project

The pipeline builds and analyzes a dataset of the configuration artifacts of six tools
(Claude Code, GitHub Copilot, OpenAI Codex, Cursor, Gemini and OpenCode) in GitHub
repositories, and of the commits that those tools contributed to. The dataset has two extensions
beside the configuration artifacts: the files that they import, and the planning documents that
spec-driven development frameworks and the tools' planning features write. The pipeline has five
stages: sampling, classification, collection, publication and analysis. `docs/heuristics.md`
defines which files count as configuration artifacts, imports and planning documents, and which
commits count as contributions.

| Where | What | Read first |
|---|---|---|
| `1_sampling/`, `2_classification/`, `4_data-analysis/` | The code of three stages. Each script outside `2_classification/diagnostics/` has a spec beside it, except the helper modules `4_data-analysis/_shared.py` and `1_sampling/_script_loader.py`. A spec is named after its script without the number prefix or leading underscore and with hyphens, such as `classify-repos-spec.md` or `repo-metadata-spec.md` | the stage's `0_README.md` |
| `agentcontext/` | The package: collection, publication and the one command line, `python -m agentcontext <command>`, with a `--help` that lists every step, check and tool | `docs/architecture.md`, then `docs/<module>.md` |
| `heuristics/` | The heuristics that the steps read: path patterns, commit patterns, import patterns, the patterns of the planning documents and their negatives | `docs/heuristics.md` |
| `data/` | The data root, one directory per stage: everything that a run reads and writes | `data/collection/0_README.md` |
| `tests/` | One suite per module of the package, and suites over the repository as a whole | `docs/development.md` |
| `deploy/`, `dev/` | The container, its runbook and the host setup | `docs/deploy.md`, `docs/development.md` |
| `locks/` | The locks of every Python distribution that an environment installs, with their hashes | `locks/0_README.md` |

`run_pipeline.py --list` shows the steps of all five stages. The package reads its data root from
`AGENTCONTEXT_DATA_ROOT` when that variable is set. An editable install binds `import agentcontext`
to this checkout. For a run in another checkout, put that checkout first on `PYTHONPATH`.

Read a module's document before changing the module, and move a decision there when the code
cannot express it. No data is stored beside the scripts, and the container image contains none.

## Development dataset

Use the latest complete handover as development input. `docs/dataset.md` names it, with its
checksum, sizes and evidence limits. Use the handover to improve the pipeline, the heuristics, the
tests and the verification. Corrected behavior may change any dataset result, and the published
counts of an earlier release are not acceptance criteria.

Preserve the original archive and process a working copy. Extract a handover with `7zz x -snld` into
an empty directory, on macOS only inside a Linux Docker volume
([development](docs/development.md#handover-working-copy)). Without `-snld`, 7-Zip writes a 0-byte
file for each symlink that it refuses. The file count still matches the archive, and the cleanup
deletes the placeholders as empty files. Compare the symlinks that `find -type l` lists with the
symlinks of the archive listing. Review the unmatched paths, the deletions, the commit attribution
differences, the missing metadata and the licenses that
`1_sampling/4_filter_ghs.py --check-licenses` names. Check them against the collected files, the
file lists, the commit evidence, the vendor documentation and the license texts. Turn a confirmed
defect into a focused test that fails without the fix, and update the verification when an
existing check missed it. Never rewrite the regression manifest to silence a difference.
[docs/development.md](docs/development.md#continuous-improvement-from-handovers) gives the commands.

Run the `python3 -m agentcontext` commands of this section from the working copy's
`data/collection/` directory, with `AGENTCONTEXT_DATA_ROOT` set to the working copy's data root,
as the container command of [development](docs/development.md#handover-working-copy) does. The
updates in the checkout, step 3 of the numbered list below, are the exception. `data/`, `output/`
and `finished_repositories.jsonl` are relative to the `data/collection/` directory. Without the
variable, steps such as `snapshot write`, `commit-reclassification`, `file-history` and `run` read
and write the checkout's `data/collection/`. When the data root contains
`sampling/ghs_licenses.csv`, as the handover of a run of every stage does, run
`python3 1_sampling/4_filter_ghs.py --check-licenses`
from the checkout (`/code` in the development container) with the same variable. Record each
license that it names in the checkout's `1_sampling/config/licenses.csv`. A handover of a
collection over the March sampling frame has no such file.

The `classified_by` digest changes with the columns of `ai_commit_patterns.csv` and
`other_ai_commit_patterns.csv` that the classification reads, `id`, `tool`, `scope`, `attribution`
and `pattern` (`commits.CLASSIFICATION_COLUMNS`), with the order of their rows, and with the code
of `agentcontext/commits.py` and `agentcontext/heuristics.py`, a refactoring included. The
release step exits 1 on a stale digest, and such a change therefore needs
`commit-reclassification` in the working copy. A change to `source`, `doc_url`, `doc_verified`,
`evidence` or `notes`, and a comment, a docstring or a log message in the two modules, leave the
digest unchanged. After a grounded heuristic change, regenerate the working copy:
`collected-file-cleanup`, `commit-reclassification` when the digest changed, then `detection`,
`file-history`, `import-detection`, `output-cross-check`, `unmatched paths`, `unmatched commits`,
`deleted-files-report`, `snapshot write` and `statistics --collection --archive` with the
downloaded handover, and after a commit reclassification also `unmatched reduce`.

Inspect the changes in the working copy, then bring them into the checkout as
[development](docs/development.md#continuous-improvement-from-handovers) describes:

1. Regenerate the development subsample in the working copy, which holds the trees.
2. Copy the changed files and the validated tree back, and rebuild the tracked archives.
3. In the checkout, without `AGENTCONTEXT_DATA_ROOT`, update `output/baseline.json` with
   `regressions check --write`, update the analysis outputs on macOS, because the verification
   compares their bytes, and update the stated statistics.

A commit reclassification runs where the per-repository commit records are. Missing commit logs
make the reclassification incomplete, and they are no reason to keep old expectations. Never edit a
derived count of `output/snapshot.json` by hand.

`collected-file-cleanup` rebuilds `data/validated/` from `data/collected/` alone. Before you run
it, fetch into `data/collected/` every fetched file that `data/collection/fetched_files.csv` lists
and the directory lacks, at the commit that its row records.

A path pattern change that matches paths in a repository's file list changes what the cloning step
copies, and only a new clone collects the newly matched files. Do not run `repository-cloning` in a
working copy to get them: It needs network access and collects the repositories that the change
reaches at their current heads. Where no collection over the list is recorded, as after
`--mark-complete`, it begins a new collection, which queries the head of every repository and
collects again every repository with a head that moved, with a line in
`finished_repositories.jsonl` that has no `collected_under`, or with a line written under a version
of the collector's code that `agentcontext/code_versions.py` does not name
(`agentcontext/reuse.py`). Before the regeneration
above, fetch the newly matched files at each repository's collected commit into `data/collected/`,
add a row for each fetched file to `data/collection/fetched_files.csv`, and describe the fetch in
`docs/dataset.md`. Then record the cloning step with
`python3 -m agentcontext run --mark-complete repository_cloning`, so that `run` does not collect the
sample again. The record covers `data/collected/`, `data/imported/` and what `collection_inputs`
in `agentcontext/runner.py` lists:

- the collector's code
- the version of git
- the path heuristics with `vendored_dirs.txt`, `local_state_paths.csv` and the heuristics of the
  planning documents
- the import heuristics with the code of `agentcontext/imports.py`
- the list of repositories to collect

A change to the collector's code or to the code of the imports alters a later collection only in
the repositories that it reaches. Record it in the same commit as a version in
`agentcontext/code_versions.py` with the reach of the change: no repository, every repository,
a predicate over the file list, or a predicate over the per-repository commit record
([repository cloning](docs/steps/repository_cloning.md#recording-a-change-of-the-collector)).
`tests/test_code_versions.py` fails until the version is there. A change to other code, such
as the collected file cleanup or detection, needs no version. In a working copy, fetch
the files that a recorded change copies, as for a path pattern.

In a working copy that has to stay offline, record the cloning step again after every
change to one of these inputs. When a change alters which files the collector copies, record the
step only once `data/collected/` and `data/imported/` hold those files, as after fetching the
files above. Otherwise, record it at once. Name in `docs/dataset.md` each change that the tree
predates.

`file-history` then clones the repositories that gained files, which needs network access. A
repository from which the collection copied no file has no commit log, and its commits stay missing
until a new collection. A repository that has a per-repository commit record and gains files in
`data/collected/` after the collection fails the path check of `commit-reclassification`
(`no_path_history`). `complete` then stays false until a new collection, and the step exits 2 only
when the repositories with incomplete evidence exceed the cloning step's ceiling. Name the
repositories of both kinds among the evidence limits of `docs/dataset.md`.

## Heuristics

- A configuration path is a row of `heuristics/config_path_patterns.csv` first, and a step reads
  it from there through `agentcontext/heuristics.py`. `tests/test_heuristics.py` fails when the
  code and the heuristics disagree.
- Every row is grounded. A path pattern cites the vendor page that states it in `doc_url`, with the
  date on which the page was read in `doc_verified`, and links files in `evidence` where the page
  does not state the whole path. In the other heuristics files, a `doc` row cites the vendor page
  and the date on which it was read, and a `sample` or `dropped` row links the commits or files
  that it was read off in `evidence`. When a page does not settle a row, read the vendor's shipped
  code, archived pages or forum before calling the row ungrounded, and quote what decides it in
  `notes`. What a tool does not read goes into the negatives, `not_configuration.csv` and
  `not_ai_commits.csv`, with the reason.
- A commit heuristic has an `id` that no other row is ever given, and an `attribution`: `account`,
  `reviewer`, `standard` or `convention`, as the Attributions table in [AI contribution detection](docs/heuristics.md#ai-contribution-detection) defines them.
- Configuration artifact detection and AI contribution detection have separate criteria. Never
  apply the criteria of one to the other.
- `python -m agentcontext docs` re-reads the vendor pages that the heuristics cite. When it exits
  2, read the pages that changed. `unmatched paths` and `unmatched commits` rank what no heuristic
  explains. `docs/heuristics.md` gives the commands for each heuristics file.

## Tests and verification

- Run the affected suites while you edit, as `mise exec -- python -m unittest tests.test_<name>`
  from the root, so that they run in the checkout's `.venv`, the environment that CI builds.
  The `0_README.md` of `1_sampling/`, `2_classification/`, `4_data-analysis/`, `deploy/` and
  `_website/` gives the commands of its suites, which run from that directory, and
  `test_run_pipeline` runs from the root. No test reaches the network.
- Every code fix needs a regression test that fails without it. Check that it does by
  reintroducing the defect.
- Test observable behavior, with expectations derived independently of the implementation. Do
  not assert an imported constant against its source, count occurrences in source text, or use
  an earlier release's totals as correctness criteria.
- Finish the batch, its documents and its stated statistics, commit it, then run the full
  verification, `mise exec -- bash dev/verify.sh` on macOS, and review the committed batch as a
  whole. The verification exits 0 only when every suite and check passes and the tree is clean.
  Before every push it must pass on the committed state being pushed, and again when that state
  changes.
- A command that reports a verdict through its exit code exits 0 when its inputs are present and
  unchanged, 2 when it found a difference, and 1 when it could not check.
- The pipeline runs on Python 3.14, at the release that `mise.toml` pins, and every package has one
  exact pin that is the same in every `requirements.txt`. `locks/` locks every distribution with
  its hashes, `dev/lock.sh` writes the locks, and CI, both images and `dev/setup.sh` install only
  from them. `tests/test_tech_stack.py` checks all of it, and the verification refuses an
  environment that differs from `locks/development.txt`.
- CI runs every suite on every push, and `.github/workflows/pipeline-tests.yml` lists every test
  module by name. A new suite needs its module in that list, and a new directory of suites needs a
  block of its own. `test_runner.TestContinuousIntegrationCoverage` checks both. A new directory
  of suites also needs an entry in `SUITES` of `agentcontext/tools/verify.py`, with the name of
  its block in the workflow and the directory that it runs from. `tests/test_tools_verify.py`
  checks that the workflow and `SUITES` have the same blocks.

## Constants

The analysis scripts import these constants from `4_data-analysis/_shared.py`, and
`4_data-analysis/test_shared.py` checks the block below against the module:

```python
TOOLS = ["claude", "copilot", "no_specific_tool", "cursor", "gemini", "codex", "opencode"]
TOOL_LABELS = {"claude": "Claude", "copilot": "Copilot", "no_specific_tool": "No specific tool", "cursor": "Cursor", "gemini": "Gemini", "codex": "Codex", "opencode": "OpenCode"}

MECHANISMS = ["context_file", "skills", "subagents", "commands", "rules", "settings", "hooks", "mcp", "plugins", "lsp"]
MECHANISM_LABELS = {"context_file": "Context Files", "skills": "Skills", "subagents": "Subagents",
                    "commands": "Commands", "rules": "Rules", "settings": "Settings",
                    "hooks": "Hooks", "mcp": "MCP", "plugins": "Plugins", "lsp": "LSP"}
```

- `other` is a column of `repos.csv` but no mechanism, and `MECHANISMS` leaves it out.
- `no_specific_tool` is TRUE for a repository with configuration and none of the six tool flags,
  that is, a repository with shared configuration artifacts only, such as `AGENTS.md`. An analysis
  of tool-specific behavior uses the six tool columns. An analysis of adoption may add
  `no_specific_tool` as a seventh category.
- `.claude/commands/*.md` counts under `skills`, because Claude Code merged its commands into
  skills. `commands` therefore means Cursor, Gemini, Copilot and OpenCode commands.

## Analysis scripts

- Derive data paths from `agentcontext.SAMPLING_DIR`, `CLASSIFICATION_DIR` and `ANALYSIS_DIR`
  (through `_shared.output_dir` for an analysis), read the collection's output CSVs from
  `_shared.DATA_DIR`, and derive code paths from `Path(__file__).resolve()`.
- Boolean columns in the CSVs are strings: convert them with `str(v).upper() == "TRUE"`.
- Figures are PDF from matplotlib or seaborn with `bbox_inches="tight"`, data is CSV written with
  `index=False`, and tables are LaTeX snippets. A script logs a summary through `agentcontext.logs`
  and prints nothing.
- The statistics are Mann-Whitney U tests with Cliff's delta, and chi-squared tests, or Fisher's
  exact test where an observed or expected cell count is below <!--s:parameters.4_data_analysis.rq2.config_cooccurrence.MIN_CELL_COUNT-->5<!--/s-->, with Benjamini-Hochberg correction and
  Cramér's V (Cohen's 1988 thresholds).
- Every script follows one style:
  - a module docstring that states what the script does, its inputs and its outputs, and ties it to
    no paper section, and a one-line docstring and type hints on every function, with plain
    `pd.DataFrame` and `pd.Series`
  - pandas for tables, and the helpers of `_shared.py` and `rq2/_repo_metadata.py` rather than
    copies
  - `Path(__file__).resolve().parents[N]` for code paths, and one path of its own,
    `OUTPUT_DIR = output_dir(__file__)`
  - `if __name__ == "__main__": main()`, `sorted(..., reverse=True)` rather than negated keys,
    and widths derived from format strings rather than written as numbers
- After changing analysis code, run `4_data-analysis/check_reproduction.py`, which runs every
  script twice and requires byte-identical outputs.
- The analyses describe the current dataset release and compare it with no earlier publication.
  Intersections across the six tools and `AGENTS.md` use the tool UpSet figure.

## Releases and versioning

`CHANGELOG.md` follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and the
version follows [SemVer 2.0.0](https://semver.org/spec/v2.0.0.html) and describes the dataset,
because a user cannot tell a changed script from a changed snapshot:

| Bump | When |
|---|---|
| Major | The meaning or membership of a published column or row changes. A new collection snapshot is always major, and so is a detection heuristic that reclassifies existing configuration artifacts. |
| Minor | Something is added without changing what is already there: a new column, a previously unclassified mechanism, a new script. |
| Patch | No published value changes: documentation, tests, refactoring, tooling. |

Work accumulates under `## [Unreleased]` until a release is published. Every release is
git-tagged and recorded in the `dataset_version` column of `repos.csv` and of every mechanism
CSV. `v1.0.0` is the AIware 2026 dataset paper release.

## Documents

- Every number that a document states comes from `docs/statistics.json`, with a source that
  computes it from tracked data, from the data root of the latest handover or from a quote of a
  vendor page, or that reads a value of an earlier state from `docs/history/values-at-commits.json`.
  Mark it as `<!--s:KEY-->36,710<!--/s-->`,
  give its entry such a source under `from`, and run `python -m agentcontext statistics`.
  [docs/tools/statistics.md](docs/tools/statistics.md) gives the format.
- `README.md`, this file and `GLOSSARY.md` describe current behavior, requirements, evidence
  limits and design reasons. Remove completed task lists, session narratives and obsolete
  instructions, which the history of the private repository keeps. `docs/dataset.md` contains
  the dataset's limitations.
- `docs/development.md` contains executable instructions for native macOS, native Ubuntu and the
  Linux x86-64 container, updated whenever the setup or the checks change. Record the archive,
  checksum and evidence limits of each new complete handover in `docs/dataset.md`.
- Address the reader as "you". Use the terms that GLOSSARY.md defines, such as "per-repository
  commit records" for the files that store commit evidence, and none of the words that it retires.

## Rules

- Names follow `GLOSSARY.md`, in code, documents and commit messages alike. The glossary check
  reads the latest commit message too. Keep vendor keys and published schemas unchanged, and add
  no compatibility aliases.
- Judge an existing name, module, format or output by the glossary and the current design, not by
  the fact that the repository or an earlier release has it. Except for a vendor key or a
  published schema, rename or restructure it where the result is clearer. `regressions check`
  and the subsample run show whether such a change altered a published value.
- When you change a module of the package, update its document under `docs/` in the same commit,
  and `docs/architecture.md` when the change crosses modules. Keep the `0_README.md` and the spec
  of a script of the other stages current the same way. When you change the layout, update
  `README.md` and the section [The project](#the-project).
- A code comment or a docstring, in a test as well, states what the code does and why. It names
  no measured size, count or duration, and it does not tell what the code did before or how a
  defect was found, which the history of the private repository keeps. It may state a number that the code uses,
  such as a default or an exit code, and quote what grounds a heuristic.
- Every change that a dataset user would notice, such as a detection heuristic, a column or a
  published count, gets an entry under `## [Unreleased]` in `CHANGELOG.md` and a sentence in the
  `release-notes` section of `_website/index.html`, in the same commit. Counts come from the
  release inputs. Keep citation information out of the release notes.
- A step's command, its module under `agentcontext/steps/`, its completion key and its progress
  messages through `logs.step_name` use one noun phrase, the step's name. A step that runs a check,
  such as `collection_summary` with `snapshot write`, keeps the check's module and command.
- `agentcontext/repos.py` defines the `owner§repo` directory naming and the test for whether a
  directory entry is a repository. Import it rather than restating the separator.
- The runner reports outcomes in its logs and state, without a push notification service.
- Start an agent that works from the remote repository, such as a cloud agent, or dispatch a
  workflow by hand only when the user has authorized that agent or workflow. The authorization
  lasts for the rest of the conversation, including a restart after a fix, and does not cover a
  different agent or workflow. It covers the commit, the verification and the push that the agent
  or the workflow needs, which you do before you start it.
