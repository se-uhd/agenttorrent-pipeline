# Architecture

The collection stage is one package, `agentcontext`, installed in editable mode from
`pyproject.toml` at the repository root, with one command line, `python -m agentcontext <command>`.
The pipeline reads and writes everything under the data root, `data/`, which has a directory per
stage, and the package reads and writes the stage directory, `data/collection/`. `heuristics/`
contains the detection heuristics that the package applies, `tests/` one suite per module, and
`deploy/` the container. This document describes how the modules depend on one another. The
document named for a module under `docs/` records the decisions of that module, and comments in
the code give the reasons that fit in a sentence.

## The layout

| Module | What it contains |
|---|---|
| `heuristics.py` | The loaders of `heuristics/config_path_patterns.csv`, `heuristics/ai_commit_patterns.csv`, `heuristics/other_ai_commit_patterns.csv`, the two negatives `heuristics/not_configuration.csv` and `heuristics/not_ai_commits.csv`, `heuristics/vendored_dirs.txt` and the places of the local state of the tools, `heuristics/local_state_paths.csv`, and the CSV names and headers derived from the path patterns. It raises the limit of a CSV field on import, because a disclosure row is longer than the default. See `docs/heuristics.md` |
| `imports.py` | The import heuristics, `heuristics/import_patterns.csv` and `heuristics/not_imports.csv`, one parser per syntax, the chain of imports that starts at an importing file, and the copy of the files that the imports reach out of a clone into the imported tree. The cloning step and the import detection step both call it, and the copy and the rows therefore cannot disagree. The detection step calls its rows and chains to decide where Claude Code reads a `.claude/AGENTS.md`. It is outside `heuristics.py`, which is part of the `classified_by` digest. See [Imports](heuristics.md#imports) |
| `prune.py` | `PRUNE_GLOBS` and `is_prunable`, the paths that the collected_files_archive step removes from its copy of the validated tree: the places of the local state of the tools (`paths.local_state`) and housekeeping files. The copy of the imports, the import detection step and the release leave these paths out as well. Of the package, the module imports only `paths`. The code of the imports, which `reuse.import_files` records with the import heuristics, therefore contains this module but neither the archive step nor the command line. See `docs/steps/collected_files_archive.md` |
| `planning.py` | The loader of `heuristics/planning_document_patterns.csv`, the match modes and anchors, and the matching of planning documents and their anchors that the cloning step, the collected file cleanup and the detection step share. It is part of the collector's code (`reuse.collector_files`). It is a module of its own because the code of `heuristics.py` is part of the `classified_by` digest. See [Planning documents](heuristics.md#planning-documents). `load_planning_document_patterns` reads a file once per process while its modification time and size are unchanged, because the cleanup, the detection step, two checks and the fingerprint of the content quality analysis find the planning documents of every repository |
| `planning_schema.py` | The published side of the planning documents: the name and the header of `output/planning-documents.csv`, the key of their history in `file_history.csv`, and the loader of `heuristics/not_planning_documents.csv` with its reasons. Only the steps after the collection read it. It is therefore outside the collector's code, and a new column or reason makes the cloning step collect nothing again |
| `repos.py` | The `owner§repo` directory name, the test for whether a directory entry is a repository, and the timestamps in UTC. The conversion handles a commit offset of 24 hours or more, which git accepts and `datetime` rejects. It raises a ValueError for an instant outside the years 1 to 9999, as for a value that does not parse. `to_utc_or_blank` leaves such a value blank in the published CSVs, and `parse_and_convert_to_utc` keeps git's text in `file_history.csv` |
| `caches.py` | `file_history.csv`, `submodules.csv` and `ai_authorship.csv`, the caches of the collection. See `docs/caches.md` |
| `credentials.py` | The GitHub access tokens, OpenAI API keys and OpenRouter API keys, read from the environment first and then from `credentials.csv` at the repository root or at the path that `AGENTCONTEXT_CREDENTIALS` names. See `docs/credentials.md` |
| `github_api.py` | The pool of GitHub access tokens with the rate limit and pause of each account, the test of whether a response is a rate limit or a block, and the errors of a GraphQL response as objects, for every step that queries GitHub's API. See `docs/credentials.md` |
| `git/auth.py`, `git/bounds.py`, `git/clone.py`, `git/history.py` | How git receives the GitHub access token, the wall-clock and size bounds on a command, the clone with its retries, the history fetch and the per-path queries. See `docs/git.md` |
| `commits.py` | The commit patterns compiled from the heuristics, and the code that reads every commit of a clone. See `docs/commits.md` |
| `paths.py` | The heuristics applied to a clone: the per-tool collectors, the copy and the symlink markers. `marker_target` is the test of the text of a marker that every step applies. It takes a text with a space as a marker only where the text names a path beside the marker, and it keeps the spaces at the start or end of a target where the target with them names a path there. Where the content that the collectors read through a symlink inside the repository decided what they copied, `store_declaring_files` replaces the marker of the symlink with that content. `link_parents` lists the directories in which the cloning step follows a link, each with the rows that read its items, and the collected file cleanup uses them to keep the file that a marker names. `in_git_directory` keeps the `.git` of the clone out of what the steps read and copy. `has_trigger` and `has_name_and_description` test the front matter that Antigravity requires of a rule and of an agent. `declared_context_files` gives the context files that a configuration names, `declared_subagent_files` the subagent files that configuration names and their parts, such as a Codex role file and its instructions, `declared_rule_files` the rule files that Antigravity's rules.json names, `registered_configuration_files` what every Antigravity registration file selects, and `registered_root_skills` the registered skills with the registration file that selects them in their own folder. `antigravity_left_out` gives the items of Antigravity's default places that a top-level `exclude` leaves out. `plugin_roots`, `plugin_components` and `plugin_parts` give the plugins in a tree and the locations in each plugin that its tool reads. `codex_marketplace_installs` tests whether Codex can install a plugin from a Codex marketplace, which detection requires of the marketplace, with the tests of `codex_marketplace_plugins` and `codex_installs` that `plugin_roots` applies to each entry. `nested_places` gives the places below the repository root that the rows with the match mode `nested:` read, such as the `.github/agents` or the `.mcp.json` of a package, which Copilot CLI reads where it works. A place can also be a link to a file or a folder of the repository. `action_prompt_files` gives the prompt files that the agent actions of the workflows read, such as the `prompt-file` of the Codex action, and `opencode_prompt_files` the files that the prompts of the agents of an OpenCode configuration name with `{file:...}`. The cloning step, the cleanup, detection and the subsample all use these functions, and the import heuristics read `nested_places` as well. For every comparison of a tree with a per-repository commit record, `tree_files` lists the directory of a repository without the files that Finder and Synology DSM write. `copy_tree` copies a tree of the collection for the cleanup, the archive and `unmatched paths`, with symlinks kept as symlinks, without the `@eaDir` directories that `file_manager_directories` names and without recursion, so that neither DSM nor the depth of a directory can end the copy. `remove_tree` removes a directory of a tree on the NAS for every step that removes one. DSM writes an `@eaDir` into a directory at any time, and an `@eaDir` written after `shutil.rmtree` listed the directory makes the removal fail with ENOTEMPTY. The function therefore removes what DSM wrote, up to `REMOVAL_TRIES` times, and raises when anything else is left |
| `collector.py` | What the cloning step does with one repository, `process_repo`: the clone, the copy of its configuration artifacts, its file list, submodules and workflow files, the copy of the files that its imports reach, the reading of its commit history, and `CollectionConfig`. Its module closure, without the modules that only schedule a run and the files of the packages, is the collector's code, which `reuse` records with every repository and the runner with the cloning step. `code_versions.py` records the versions of the collector's code. See `docs/steps/repository_cloning.md` |
| `resume.py` | `finished_repositories.jsonl`, with one line per finished repository and the collection that wrote it, the reduction of the file to the lines that its readers use when a collection begins (`reduce_finished`), the records of the collection in progress and of the last collection that ended, which a run over the same list or over the list without repositories that GitHub no longer serves continues until `--from repository_cloning` marks the collection that ended as replaced, and what a resumed collection does again, including the repositories that the last commit reclassification found `failed` |
| `reuse.py` | Which repositories of an earlier collection the cloning step takes over without a clone. It records what each repository was collected under and compares the collector's code, the path heuristics and the import heuristics with the file list of the repository. For a change of the collector with a reach that the per-repository commit record decides (`code_versions.RecordReach`), it reads that record instead of the file list (`recorded_path_commits`), and for a change with a reach that the kept workflow files decide (`code_versions.WorkflowReach`), it reads the workflow files that the collection kept (`kept_workflows`). It queries the head with `git ls-remote` unless the collection in progress already collected or confirmed it. While git returns no head commit for more repositories than the ceiling of the step allows, it waits longer before each further query. For each repository that it takes over, it classifies the commits again from the commit log that the cloning step kept. It removes the files of the repositories that left the input and marks their lines to redo. See `docs/steps/repository_cloning.md` |
| `code_versions.py` | Every version of the collector's code and of the code of the imports that a collection was made under, in order, with the reach of each change: no repository; every repository; a predicate over the file list that reads the definitions of the collector's code, such as `paths._SKILL_PARENTS`; a predicate over the commits of each copied path that the per-repository commit record keeps (`RecordReach`), which `reuse.recorded_path_commits` reads; or a predicate over the workflow files that the collection kept (`WorkflowReach`), which `reuse.kept_workflows` reads. `reuse` takes over a repository collected under an earlier version when no later change reaches it. `code_versions` imports `paths`, and only `reuse` imports `code_versions`. A new version therefore changes neither the collector's fingerprint nor the fingerprint of the code of the imports. See [Recording a change of the collector](steps/repository_cloning.md#recording-a-change-of-the-collector) |
| `fingerprints.py` | The module closure of a step and the fingerprint of the code that it runs, with which the runner records a step and the cloning step records the collector. `code_fingerprint` leaves out comments, docstrings and log messages, and the collector's fingerprint and the fingerprint of the code of the imports also leave out exception texts (`exception_texts=False`) |
| `guards.py` | The progress line, the stop rules, the disk minimum, the process counts and the stop handler of a worker of the cloning step |
| `archives.py` | The comparison of a file extracted from a 7-Zip archive with the archive beside it, by the size and the CRC32 that `7zz l -slt` lists. `check_copy` raises where git tracks the archive and the extracted file differs from it, which in a checkout means that the file is not the committed input, and passes in the data root of a run and in a working copy, where a step wrote the file. The analysis scripts, the draw of the detection accuracy evaluation and `dev/setup.sh` call it, and no step of the collection imports it. See `docs/archives.md` |
| `jsonc.py` | Reading a settings file as the tool that owns it reads it: strict JSON, or JSON with comments where the tool accepts comments, as `content_format` in `heuristics/config_path_patterns.csv` specifies |
| `logs.py` | The single line format of every log message of the package, `LEVEL component: message` on stderr, built on `logging`, and the status in which a running step states what it waits for, which the runner copies into the pipeline execution state (`step_status`). See `docs/logs.md` |
| `steps/` | The steps of the package: `engineered_project_selection`, `repository_cloning`, `commit_reclassification`, `collected_file_cleanup`, `detection`, `file_history`, `import_detection`, `output_cross_check`, `collected_files_archive`, `release`, and the checks in `checks/unmatched.py`, `checks/deletions.py` and `checks/snapshot.py`. Each step has a document under `docs/steps/`, and each check under `docs/checks/` |
| `checks/` | `snapshot` (the record of a collection), `regressions` (the manifest and the comparison of two runs), `unmatched` (the paths and the attributions that no heuristic explains, and the tracked unmatched attributions of the current collection), `deletions` (the deletion log of the cleanup, read back against the outputs), `commits` (the validation of the commit CSV against the commit heuristics). Each has a document under `docs/checks/` |
| `tools/` | `measure` (repository sizes), `subsample` (the development subsample), `docs` (the documentation watch), `mirror_run` ([the subsample run over local mirrors](tools/mirror_run.md)), `statistics` ([the stated statistics](tools/statistics.md)), `evaluation` ([the detection accuracy evaluation](tools/evaluation.md)), `tool_selection` ([the count of the tools in a collection's file lists](tools/tool_selection.md)), `verify` (every suite and check). `docs/tools/` documents measurement, subsampling, mirror runs, the stated statistics, the detection accuracy evaluation, the tool selection and [verification](tools/verify.md). `docs/heuristics.md` documents the documentation watch |
| `statistics/` | The computations of the stated statistics and the readers of their files: `tables`, `commits`, `unmatched`, `labels`, `retrievals`, `code` and `handover` ([stated statistics](tools/statistics.md#the-computations)) |
| `runner.py` | Step definitions, completion, pipeline execution state, locking and the pre-checks. See `docs/runner.md` |
| `cli.py`, `__main__.py` | The one command line, with every step, check and tool as a subcommand |

The package reads its data root from `AGENTCONTEXT_DATA_ROOT` when that variable is set, and
otherwise uses `data/` beside its own directory. The container sets the variable to its data
mount, and the subsample run sets it to the `data/` of its checkout. `SAMPLING_DIR`,
`CLASSIFICATION_DIR`, `STAGE_DIR` and `ANALYSIS_DIR` are the directories of the stages under the
data root. The scripts of every stage derive their data paths from these directories, and a
script run from anywhere therefore writes into the data root and never beside the code. An
editable install binds `import agentcontext` to one checkout. For a run in another checkout, put
that checkout first on `PYTHONPATH`.

## Current development input

The latest complete handover is the evidence for improving the pipeline, the heuristics, the
tests and the verification. The tracked outputs are the collection of the run of all five
stages that started on 2026-10-23, and [dataset.md](dataset.md) identifies that input and its
evidence limits.
[development.md](development.md) describes the workflows on macOS, on Ubuntu and in the container.
Corrected results do not necessarily match the counts of earlier papers.

## The steps, in order

The pipeline has five stages: sampling, classification, collection, publication and analysis.
Each stage is a sequence of steps. The list of steps of the package contains the last step of the
classification and the steps of the collection and the publication, and every step declares its
stage in `Step.stage`. Command arguments and the pipeline execution state identify steps by noun
phrases, and progress messages use the same noun phrases through `logs.step_name`. Renamed
commands and keys have no compatibility aliases or state migrations. Functions that share
heuristic rows take them as `heuristic_rows` parameters, and `heuristics.MECHANISM_CSVS` names the
output filenames and path columns of the mechanisms.

`engineered_project_selection`, the last step of the classification, derives the list of the
sample, `data/classification/repos_annotated_included.csv`, from the annotated CSV of the
classification stage, so that the pipeline can start from a clean checkout. The step logs its
input and its output. The collection stage clones every repository in that list, except a
repository that it takes over from an earlier collection (`reuse.py`). From each clone, it copies
the configuration artifacts and the planning documents that the heuristics match into
`data/collected/`, and it records each repository in `data/collected/repos.csv`. In the same
clone, it measures the git columns of every copied file and reads every commit for the CSV of AI
commits.

```shell
python3 -m agentcontext repository-cloning
```

The cloning step keeps the commit log that it read, `collect_commits/<owner§repo>.log.xz`, and the
`commit_reclassification` step applies the current commit heuristics to those logs. The
reclassification rebuilds the CSV of AI commits and the AI columns of every configuration artifact
without a clone, and a change to a commit heuristic therefore applies to a finished collection
too. Right after a collection, the cloning step has already classified the commits under the
current heuristics, and the step keeps these classifications once it has validated the retained
evidence. It records the resolved refs, retrieval dates, checksums and path coverage per
repository, and missing or invalid evidence prevents a complete reclassification. Some shards stay
out of the commit CSVs and `ai_authorship.csv`: the shards of a repository with evidence that
failed validation, and shards that other commit heuristics classified and that the step could not
classify again. `output/commit-reclassification.json` lists these repositories. The detection
step, which rebuilds the commit CSVs from the shards, reads that file and leaves them out too. The
reclassification does not take a configuration flag or an empty gitlink directory as evidence that
a file was copied. A repository with an explicit zero copied-file count and no retained files or
history rows requires no commit history. On a resumed run, enabling or changing this step
invalidates the collected file cleanup and the steps that consume it. The cleanup rebuilds the
validated tree from the collected files before detection reads it, because an earlier run of the
file history step may have pruned that tree. Repository cloning keeps its recorded snapshot.
`docs/steps/commit_reclassification.md` is the full specification.

Commit logs and path history queries use NUL-delimited fields, so that commit messages can contain
the bytes that separate records and fields. Path mappings therefore keep the commit hashes that
the offline reclassification of the authorship needs.

```shell
python3 -m agentcontext commit-reclassification
```

`agentcontext/steps/collected_file_cleanup.py` then derives the validated tree (`data/validated/`,
with its list at `data/validated/repos.csv`) from `data/collected/` and validates and cleans it.
The cleanup leaves the `@eaDir` directories that Synology DSM writes out of its copy, checks file
locations and deletes invalid files (e.g., files in the wrong case, `.DS_Store` files, an `@eaDir`
that DSM writes into the copy, valid files outside their expected locations, and files outside every
spec directory that match no keep rule). It removes empty files (i.e., files with whitespace-only
content and empty JSON objects or arrays), except the files that `kept_when_empty` names: the
imports of the GitHub Agentic Workflows, the prompt files of their agent actions, and the planning
documents with their anchors. `4_data-analysis/rq2/content_quality.py` finds the same files with
this function. The cleanup removes the repositories that are left empty or in which
`detection.has_detected_mechanism` finds no named mechanism after validation. The cleanup writes no
boolean flag, because detection computes every flag from the validated tree. `deletion.csv` logs
all deletions, and the `deletions` check reads that log back. Every run derives the validated tree
and its list again from the untouched collected files, and the cleanup can therefore run again at
any time without repeating the collection. The cleanup copies the repositories one at a time
rather than the whole directory, and the collected list and the list of clone failures beside them
stay out of the validated tree. `docs/steps/collected_file_cleanup.md` is the full
specification.

```shell
python3 -m agentcontext collected-file-cleanup
```

`agentcontext/steps/detection.py` reads `data/validated/repos.csv` and the validated repository
trees and writes the mechanism CSVs under `output/`. A repository that the list contains without a
directory in `data/validated/` (e.g., after a clone failure or an exclusion during validation)
goes to `output/excluded.csv` and stays out of `output/repos.csv` and the mechanism CSVs. Files in
spec directories that match none of the named configuration mechanisms go to `output/other.csv`.
They are published rather than deleted, and `unmatched paths` reads them for path globs worth
classifying. Each row that detection writes also has the git columns, joined from
`file_history.csv` on `(repo_name, head_sha, mechanism, artifact)`. In every CSV with a `language`
column, the row also has the natural language of the configuration artifact. For a link other
than a context file, detection reads the language in the file that the marker names and leaves it
blank where the tree lacks that file (`detection.measured_artifact`), and the content quality
analysis counts the lines of the same file. A context file that is a link reads as its marker,
also where the tree holds the link as a real symlink (`detection.artifact_text`), in detection
and in the content quality analysis alike. A row for a context file also has the AI columns,
joined from `ai_authorship.csv`, and whether the file is a pointer to another file. For a sample
with cached facts, this step therefore writes complete CSVs without a clone. The planning
documents of the detected repositories go to `output/planning-documents.csv` with the same git
columns, and a file that a path pattern classifies stays in the CSV of its mechanism
([Planning documents](heuristics.md#planning-documents)). Detection writes `output/repos.csv` last, with one row
per detected repository. Every tool, file and configuration flag in it is derived from the
configuration artifacts that the detectors found rather than copied from the validated list, which
detection never modifies. `docs/steps/detection.md` is the full specification.

```shell
python3 -m agentcontext detection
```

In the next step, `agentcontext/steps/file_history.py` fills the git columns that detection could
not fill from the cache, in place in the output CSVs that detection wrote. For each such row, the
step clones the repository once, checks it out at the `head_sha` at which it was scanned, and reads
the creation date, the commit count, and the first and last commit that touched the file. It writes
each measurement back to the cache. It is the last step of a run that writes `file_history.csv`
and `ai_authorship.csv`. It also removes from both caches the rows at a head that neither the
collected list nor the validated list contains, because no later step and no later collection reads
these rows ([caches](caches.md#the-rows-that-the-two-caches-keep)). Over a sample that the cache
covers, nothing is left to measure, and the step writes the same bytes back. With `--offline`, the
step clones nothing and fails, naming the rows for which the cache has no values. With it, every
output CSV is reproduced from a checkout with no credentials. The step also removes the rows of
every repository that git reports as gone. Detection had already written `output/repos.csv`, and
the step therefore finishes by deriving `output/repos.csv` again from the pruned mechanism CSVs.
It drops the repositories with no remaining configuration artifact and recomputes the flags of the
repositories that lost some but not all of their configuration artifacts. It also removes from
`output/planning-documents.csv` the repositories that `repos.csv` no longer lists.
`docs/steps/file_history.md` is the full specification.

The file history step modifies `data/validated/`: It removes the directory of a repository that
git reports as gone, the clone outcome `clone_failed`. A clone stopped by a timeout, a size bound
or a network failure keeps its directory. So does a repository with no misses except rows that the
cache records as blank, which the step only retried. If you need the validated tree as it was,
back it up before you run the step, or derive it again afterward with
`python3 -m agentcontext collected-file-cleanup`.

```shell
python3 -m agentcontext file-history            # the cache first, a clone for what is not in it
python3 -m agentcontext file-history --offline  # no network, and fails naming the rows not cached
```

The step writes no CSV of its own. An offline run writes the committed CSVs back byte for byte,
which `test_steps_file_history.TestTheCommittedMechanismCsvs` asserts.

`agentcontext/steps/import_detection.py` writes `output/imports.csv`, the imports of the
configuration artifacts: one row per tool and import of each chain that an importing file starts,
with the file that the import reaches and its status. The cloning step copies every file inside
the repository that an import reaches to the imported tree, `data/imported/<owner§repo>/`, which
the collected file cleanup does not touch. This step parses the importing files of the validated
tree again with the same functions. An import sets no tool flag and changes no other CSV.
`docs/steps/import_detection.md` is the full specification.

```shell
python3 -m agentcontext import-detection
```

`agentcontext/steps/detection.py` applies the reference heuristic, and detection therefore writes
`line_count`, `is_reference` and `referenced_file` with the rest of the row. Only files of
`MAX_REFERENCE_LINES` (<!--s:parameters.agentcontext.steps.detection.MAX_REFERENCE_LINES-->10<!--/s-->) lines or fewer are reference candidates. No output CSV has an `is_empty`
column, because `agentcontext/steps/collected_file_cleanup.py` deletes every empty file before
detection runs, except the files that its `kept_when_empty` names.

`python3 -m agentcontext output-cross-check` (`agentcontext/steps/output_cross_check.py`) checks
that the mechanism CSVs and `output/repos.csv` contain the same repositories. The step counts the
distinct repositories per CSV, takes their union, and compares the union with `repos.csv`. A
repository in `output/planning-documents.csv` has to be in `repos.csv` too, because planning
documents alone do not make a repository configured. The step always writes
`output/consistency-report.csv`. It writes `output/consistency-mismatch.csv` only when the two sets
disagree, and removes a stale copy when they agree. It exits 2 on any mismatch and 1 when a CSV
cannot be read, so that the pipeline runner stops instead of continuing over a `repos.csv` and
mechanism CSVs that disagree. `docs/steps/output_cross_check.md` is the full specification.

```shell
python3 -m agentcontext output-cross-check
```

`unmatched paths` runs after the file history step and writes `output/unmatched-paths.csv`, the
path globs that the heuristics do not explain in `other.csv` and in the repositories that the
validation removed. [Keeping up with the tools](heuristics.md#keeping-up-with-the-tools) describes what to do with each
glob. `deleted_files_report` runs next. It reads `deletion.csv` back against the two trees and the
mechanism CSVs, and writes into `output/deletions.csv` the rows per reason, the files that a
removed repository contained, and the results of three assertions that `docs/checks/deletions.md`
explains. In a run, both steps exit 0 whatever they find, and the run therefore reaches the
archive and the publication. To get a verdict as an exit code when you run the commands yourself,
pass `--fail-above` to `unmatched paths`, and run `deleted-files-report` without `--report-only`.

`python3 -m agentcontext collected-files-archive` (`agentcontext/steps/collected_files_archive.py`)
prepares the collected files for publication. The step leaves the `@eaDir` directories that
Synology DSM writes out of the copy and out of the archive, because DSM can also write them into
the staging copy. It removes the paths that match `PRUNE_GLOBS` (e.g., version-control and editor
metadata, lock and log files, `node_modules/`, `__pycache__/`) and the places of the local state
of the tools, removes empty directories, and creates `release/repos_data.7z`. A file that no
detector classified is published rather than deleted, and a gap in the detection heuristics
therefore loses no data for good. The step never modifies or renames the original
`data/validated/`. It does all cleaning on a staging copy, which it makes under `repos` inside the
staging directory, and the archive is rooted at `repos/` whatever the name of the validated tree.
The step copies only the repository directories into the staging copy. It expects
`data/validated/repos.csv` at that level and neither publishes nor logs it, and it logs anything
else there by name and leaves it alone. Pass `--dry-run` to list the deletions without modifying
files.
`docs/steps/collected_files_archive.md` is the full specification.

```shell
python3 -m agentcontext collected-files-archive
```

`python3 -m agentcontext release` (`agentcontext/steps/release.py`) builds `release/`, the dataset as a person downloads it. The step joins `output/repos.csv` to the sampling frame and the classification labels, reorders `output/context-files.csv`, and writes every commit message verbatim with its line breaks as newlines. It copies the other <!--s:parameters.agentcontext.steps.release.RELEASE_FILES.copied_mechanism_csvs:words-->nine<!--/s--> mechanism CSVs, `output/imports.csv`, `planning-documents.csv`, the provenance records and <!--s:parameters.agentcontext.steps.release.RELEASE_FILES.heuristics_files:words-->thirteen<!--/s--> of the heuristics files, compresses the <!--s:parameters.agentcontext.steps.release.RELEASE_FILES.compressed_csvs:words-->two<!--/s--> large CSVs, and packs `imported_files.7z` from the imported tree and `deletion.csv.7z` from the deletion log. It also writes a README with every number computed from the files beside it, a `CITATION.cff` and a `checksums.sha256`. `release/` is gitignored, and its contents are uploaded to Zenodo.

The release step checks `output/` against an inventory that lists every file that the directory
may contain. The run therefore stops on a file that nobody decided to publish or withhold, rather
than leaving the file out of the release unnoticed. Before it writes anything, the release step
compares the outputs of the earlier steps with one another and stops on any difference, so that a
refusal leaves the previous release as it was, with its manifest. It stops on a repository with
detection rows but no row in the sampling frame. The join is by name, and a left join would drop
the repository while the row count stayed that of the sampling frame. It compares the labels with
the sampling frame, the repositories without a published label with the exclusions of the
classification, and the ids that `matched_heuristics` names in each commit CSV with the commit
heuristics that the release publishes beside it. The repositories in `release/repos_data.7z`, which
the collected_files_archive step wrote, have to be those of `output/repos.csv`, and every target
that `output/imports.csv` places in `imported_files.7z` has to be in the imported tree. The step
also compares the detection heuristics with the checksum in the `output/provenance.json` of the
detection, and the commit heuristics with the `classified_by` that the collection summary records
from the commit reclassification. The runner records the commit reclassification by the same
digest. When a new image changes the digest, through either file of the commit heuristics or the
code of `commits.py` or `heuristics.py`, the runner runs the reclassification and the collection
summary again before the release compares them. A comment, a docstring or a log message in the
two modules changes neither the digest nor the record.

The inventory names `other.csv` and `excluded.csv` as withheld rather than leaving them out, and
the published README gives the reason. `docs/steps/release.md` is the full specification.

```shell
python3 -m agentcontext collected-files-archive      # release/repos_data.7z
python3 -m agentcontext release      # every other file of the release
```

`python -m agentcontext run` runs the core steps in order and skips the steps with completion
records that are still valid. With `--with-commit-reclassification`, it also runs the
reclassification after the cloning step, and the two checks over the commit CSVs of the cloning
step as the last steps of the collection, before the publication. `docs/runner.md` describes which
step is computed from which, how the runner records a completion and when the record stops being
valid, the lock, the pre-checks and the stop.

The whole-pipeline runner uses the step selection of the collection runner, and with
`--with-commit-reclassification` it reclassifies the commit logs before detection. The figures
computed over `all_ai_commits.csv` depend on detection, because detection restricts that CSV to the
configured repositories. The release declares the sampling frame and the labels among its inputs.
The runner therefore runs the release again after a new sampling frame or new labels, even where
the list of the sample stayed the same. The release declares the report of the GHS download as
well. From that report, its README states when a download fell back and kept the previous
sampling frame, and the entrypoint of the container reads the same report to log a warning at the
end of such a Start. The whole-pipeline runner adds the dependency edges from the filter and the labeling
to the release, as it does for the analysis steps. The labeling and the cloning step are recorded
by the contents of what they read rather than by an edge. The runner computes their records again
when it reaches them, and the pre-checks verify what they need whenever an earlier pending step
writes what they read. `--min-free-gb` sets the minimum for both the collection and the
pre-checks. `test_run_pipeline.py` checks these dependencies and the argument forwarding.

## The repository directory name

A repository is stored under a directory named after it, with the slash replaced by `§`:
`owner/repo` becomes `owner§repo`. `repos.py` defines that naming and the test for whether a
directory entry is a repository at all. Before `repos.py` defined the naming, modules wrote out the separator
themselves, and code tested "is a directory" where it meant "is a repository", wherever the
difference mattered. A link to a directory is not a repository either, because validation deletes
files inside what it scans, and the target of a link is not the tree that the step was given.

## Exit codes of the verification commands

`snapshot write --check`, `regressions check`, `docs`, `unmatched paths`, `deleted-files-report`
and `output-cross-check` give their verdict as an exit code, and they share one convention: 0 when
nothing changed, 2 when something did, and 1 when the check could not run. Exit 1 matters most,
because a check that reported agreement from inputs that it could not read would pass a snapshot
that it never read. `snapshot write --check` once reported agreement on a clean checkout, and
`docs` with an empty hash record. `test_check_modes.py` runs each of these commands with its
inputs absent and fails if any of them exits 0.

A command line that cannot be parsed, such as one with a flag or a subcommand that the command
does not have, exits 1 as well, because the check did not run. Without this change, argparse would
exit 2 on such a command line, and `regressions compare --ignor scanned_at` would then report a
difference between two stage directories that it never compared. `cli.ArgumentParser` is
argparse's parser with that change. It matches a long option by its full name only, where
argparse would take an abbreviation such as `--fro` for `--from`. The entrypoint of the container
recognizes the arguments for one Start in `command:` by their full names
([deployment](deploy.md#deployment-options)). An abbreviation that a runner or a step took for
such an argument would therefore act at every Start. Every command of the package uses the parser,
and so do the five runners and the two checks of the other stages, `1_sampling/4_filter_ghs.py`
and `4_data-analysis/check_reproduction.py`. The other scripts that a step starts set
`allow_abbrev=False` on their own parsers. `cli.main` and the dispatcher of the subcommands exit 1
on an unknown command or subcommand too. `test_check_modes.py` runs every check with a flag that it
does not have.

## Collection caches

The three caches record the measurements of the input collection, and detection uses them without
cloning. `file-history --offline` names the rows for which they have no history. The current
caches come from the collection that [dataset.md](dataset.md) names, and [caches.md](caches.md) defines their keys,
the meaning of missing values and the limits of reclassification.

Paths expanded from an internal directory symlink keep their collected names as cache keys, and
the commit evidence records the committed target path. The cloning step and the file history step
read the commit history of that target for the Git columns and the AI columns. File symlinks keep
the history of the committed link.

## Path heuristics shared by the steps

`paths.plugin_components` gives the cloning step, the cleanup and detection the same boundary for
a skill at the root of a plugin and the same checks of Claude's component paths. An explicit
`skills: "./"` declaration keeps `SKILL.md` and the support directories of the skill, but it does
not select the whole repository when the plugin is at the repository root. An invalid declaration
disables the default components as well as the declared path. A Claude Code or Codex manifest
that configures servers or hooks inline is a component of the kind `inline`, which the cloning
step copies with the manifest and detection lists under the mechanism, and
`paths.has_a_grouped_hook` is the test of a hook in the matcher groups that Claude Code, Codex
and Gemini CLI read, inline and in a hooks file alike. `paths.claude_loads_a_server` is the test
of an MCP server that Claude Code loads, in an MCP file of a plugin or of a directory below the
root and, where every server of the map passes Claude Code's schemas, in a manifest.

When the collector copies a directory, it keeps the file symlinks that rsync omits as marker
files. Validation and detection can therefore classify the path of the link, such as a Copilot
context file that points to `AGENTS.md`, without reading a target outside the repository. The
Copilot collector matches the first path component under `.github` without regard to case, as
validation does, on Linux as well as on macOS. The collection keeps the original spelling of copied
paths.

A tool directory that is a link (`paths.TOOL_DIRECTORIES`), such as a `.claude` that leads to
`config/claude`, is read by the tool as the directory that it leads to. The collector stores the
link as a marker and copies that directory at its own path. `paths.linked_tool_directories`
gives the cloning step and the cleanup the directory of each such link or marker, in which
`paths.link_parents` looks for the links to follow. `paths.at_its_own_path` gives the three steps
the path at which they read a file of such a directory by its path, such as a Codex config that
names a context file. The cleanup keeps the directory whole
(`collected_file_cleanup.find_marker_targets`), except a `.github`, of which it keeps what the
linking tool reads. The cloning step and the cleanup also remove from that directory the places
of the local state of each tool that links to it, such as Claude Code's `shell-snapshots/` in a
`config/claude` that `.claude` leads to (`paths.remove_local_state`,
`collected_file_cleanup.local_state_of`). `planning.find` matches the files of the directory
at their paths through the link for the three steps. Detection reads the mechanism directories and
files below the marker in that directory (`detection._below_a_marker`) and credits the tool of
the link (`detection.marker_directories`).

Detection and the reconciliation of the file history step attribute tools in the same way, by
declaration. A declared context or rule file adds the tool that declares it, and its path adds
another tool only when an independent mechanism also recognizes that path. Support files that are
collected but not classified do not identify a tool.

Detection and the subsample selection read Cursor's component paths and extension lists in the
same way. The overrides of a manifest replace the default locations, and the subsample matches
each listed extension at the depth that the heuristic states. The match mode of a skill heuristic
encodes the depth of skill discovery. The cloning step, the cleanup and marker resolution
recognize both modes: `skill_dir`, which finds nested skills, and `skill_dir_flat`, which reads
immediate children. Detection and the subsample selection keep their different depths.

The discovery of plugin components keeps the scope of each vendor. A declared Copilot skill
directory ends at its own skill marker, and detection accepts a plugin agent named `README.md` when
it has the required description front matter. Copilot discovery separates VS Code's agents, which
are direct children of their directory, from the CLI's agents, which it finds recursively. The
collection copies marketplace candidates by path, and they count only after detection checks their
catalog fields. Codex and Copilot CLI read a plugin with a root `plugin.json` of the Agent Plugins
schema through that manifest, and `paths.plugin_components` therefore reads none of the places of
their own layouts there. Where the tool's manifest is beside the portable one or the tool's
marketplace lists the folder, it returns the portable `skills/` and `mcp.json` as the tool's parts
(`paths._PORTABLE_READERS`), which the cloning step flags and the detection step attributes. A
root `plugin.json` without that schema makes its directory a Copilot CLI plugin only where its
content is that of a legacy manifest
(`paths.copilot_legacy_manifest`), because many programs use the name, and never in a directory
named `.<name>-plugin`, which is a vendor's manifest directory in the plugin root.

The discovery of plugin roots includes the local sources of the marketplaces of Claude Code,
Cursor, Codex and Copilot CLI, and the shared path resolver merges entries and manifests before
the cloning step, the cleanup and detection read them.
Antigravity's MCP files are parsed in a separate format that accepts comments and trailing commas,
and the other tools keep their stricter formats. A JSON or TOML file nested too deeply for the
parser, which raises RecursionError, reads as a file that does not parse in the cloning step, the
cleanup and detection. A Claude Code plugin manifest with component paths nested too deeply to
check declares paths that cannot load. The Agent Plugins test of `heuristics.py` is called through
`paths.agent_plugins_document`, which adds that guard, because the code of `heuristics.py` is
part of the `classified_by` digest.

One resolver handles Antigravity's registrations: their paths relative to the directory of the
customization root or to the root of the plugin, inheritance, entries that name an item, discovery
of immediate children, filters and the front matter that a rule or an agent needs. It follows a
customization root that links to a directory of the repository, in a clone through the symlink and
in a collected tree through its marker (`paths._linked_directory`). The cloning step, the cleanup,
detection and the subsample coverage use the files that it selects, and the discovery of plugin
roots also reads plugin registrations, with the top-level `exclude` of a `plugins.json`. Detection
passes over the items of Antigravity's default places that a top-level `exclude` leaves out
(`paths.antigravity_left_out`). The cloning step and the cleanup keep these files, which are in a
customization root or in a part of a plugin that they copy and keep whole. The cloning step, the
cleanup and detection apply the same path heuristics to Antigravity's customization roots and
registrations. A skill with the registration file that selects it in its own folder is a root
skill. All three read it as its `SKILL.md` and support directories, as they read the root skill
of a plugin, because the folder can be the repository. Like every reader in `paths.py` of a file
with content that decides what the steps select, the resolver reads a registration file only
where it resolves to a regular file inside the repository (`paths._repository_text`). The
cloning step stores a symlink to such a file of the repository with the content that it read
through the link (`paths.store_declaring_files`), so that the cleanup and detection decide with
that content. In its coverage keys, the subsample distinguishes how a tool loads a file, such as
through a manifest declaration or from a default path.

## The container

`deploy/` runs the five stages unattended on the Synology through Container Manager. The package
and the heuristics come from the image, and the files of the run are in the `data/` of the project
folder, mounted as the data root. When the runner ends, the entrypoint packs an archive of the run,
the handover, into the mounted `handover/`. `docs/deploy.md` contains the runbook and the
decisions of the entrypoint.

The development image installs the pinned Python dependencies of every stage and the system tools
that the pipeline uses, and the source code and data are mounted at runtime. Production
deployments use the production image, which runs the five stages. See
[development.md](development.md) for native and container commands, and [deploy.md](deploy.md) for
production runs.

## The tests and verification

[development.md](development.md) gives the setup and verification commands for each host, with
separate sections for macOS, Linux and x86-64 development containers. `dev/verify.sh` exits with
the exit status of the verification suite and leaves the changes in the working tree for
inspection.

`tests/` contains one suite per module, run from the repository root as `tests.test_<name>`
(`test_steps_<name>`, `test_checks_<name>`, `test_tools_<name>` and `test_git_<name>` for a
module under a subpackage). The git fixtures that the suites share are in `tests/_fixtures.py`,
and the fixture that removes an ambient GitHub access token is in `tests/_ambient_env.py`, which
has a suite of its own. The repository also has <!--s:repository.whole_repository_suites:words-->nine<!--/s--> suites that check properties of the repository as a whole rather than of one module:

- `test_tech_stack`: one Python pin everywhere
- `test_check_modes`: no verdict command exits 0 on absent inputs
- `test_data_layout`: the data trees and what is tracked in them
- `test_doc_commit_refs`: a document cites a commit of the private repository only as a record that says so, and no check resolves it
- `test_undefined_names`: no function reads a name that its module never binds, and no script
  contains a string escape that Python rejects
- `test_spec_case_counts`: every entry count that a spec states is the count on disk
- `test_stated_facts`: current dataset documentation, provenance and archive consistency
- `test_glossary`: one word per concept, as `GLOSSARY.md` names it
- `test_public_export`: the export of the public tree, and no document that the public tree keeps
  links a path that it leaves out

`python -m agentcontext verify` first checks that it runs in the environment of CI, the release of
Python that `mise.toml` pins with the distributions of `locks/development.txt`. It then runs every
suite of the repository, the reproduction check over the analysis scripts and the current dataset
verification commands with its own interpreter, and exits 0 only when all of them pass and the
tree is clean.

`python -m agentcontext mirror-run` is the subsample run over the local mirrors. It stops a
collection and resumes it, and it makes the history of one repository fail to fetch through a copy
of its mirror that contains only the tip. It then runs every step that the container runs after
the collection, in the order of the container and including the publication, and compares the
outputs with a reference. The file history step runs offline except for that repository, and it
measures the rows of that repository as a container run does after fetching a history failed on
the network. The run requires the reclassification to exit 0 with exactly the expected incomplete
repository, as the runner of the container continues with it to the publication, and it saves the
coverage report. Deploy an image to the NAS only after a subsample run with the collection code of
the image has passed. See [mirror_run.md](tools/mirror_run.md) and the
[pre-deployment checks](deploy.md#pre-deployment-checks) for the validation requirements. The
full verification checks fixtures and tracked outputs, and it neither performs the subsample run
nor processes the full handover.

`coverage`, pinned in `dev-requirements.txt` and locked in `locks/development.txt`, which
`dev/setup.sh` installs into `.venv`, measures how much of the package the suites cover:

```shell
python3 -m coverage run --source=agentcontext -m unittest discover -s tests -t .
python3 -m coverage report
```

The tests check the full argument lists of the check commands, including the threshold of
`unmatched paths` that CI uses. The tests of `mirror-run` also check that a comparison error or a
comparison killed by a signal stays a failure even when its partial findings match the expected
list.

## The collection summary and the release

`collection_summary` consumes the results of the cloning step, detection, the file history step and
the import detection, and writes `output/snapshot.json`. `release` consumes that step as well as
the import detection, the output cross check and the archive. A run over a new collection therefore
regenerates the dates and counts of the snapshot before it publishes, and it cannot publish an
inherited snapshot. The release packs `imported_files.7z` from the imported tree with the targets
that `imports.csv` places there, beside `repos_data.7z`, which is the same as without imports. The
snapshot counts by outcome the published repositories that the commit reclassification left out
of the commit CSVs (`left_out`), and the README of the release states each count in the evidence
limit of its outcome ([snapshot](checks/snapshot.md)). The snapshot also lists, under
`repositories`, the repositories of each incomplete outcome, `failed` and the clone failures, and
the README names them in the evidence limit of their outcome. The release also reads the
`repos_excluded.csv` that the classification writes beside the labels, and its README counts the
repositories in the sampling frame without a published label by reason. The snapshot checks of the
outputs run without the collected tree only when you pass `--outputs-only`.

The collection counts of the snapshot take the clone failures from
`data/collected/clone_failed.csv` and count missing collected commit hashes separately, as
`missing_head_sha`. Without the list of clone failures, the collection cannot be completely
verified.

## Collection failure messages

When a query for the committed files fails, the cloning step keeps the command, its exit code and
its stderr in the warning and in the per-repository clone log. The copying of configuration can
continue while the file list and the submodule metadata are unavailable, and the cloning step
removes a stale file list. [Git helpers](git.md#committed-file-inventory) define this error
behavior, and [collection](steps/repository_cloning.md) describes its outputs. A warning about a
worker that terminated unexpectedly or about a successful connectivity probe does not state a
cause for the individual failures.

Failure handling separates whether a step completed from whether its results reached a complete
handover. [Runner recovery](runner.md#recovery) covers failures of the state, the logs and the
declared outputs, and [deployment recovery](deploy.md#recovery-from-handover-and-storage-errors)
covers the delivery of the archive. [Commit reclassification recovery](steps/commit_reclassification.md#recovery)
describes how to check the retained evidence, whether or not the assembly of the CSV succeeded.

The processing of Git metadata requires a complete commit history and a verified checkout of the
collected commit before it fills either history cache. A missing revision stays unavailable, and
no date or branch tip replaces it. The history of a requested submodule is completed before its
pinned commit is queried. The snapshot queries of the collection likewise distinguish confirmed
empty repositories from failed Git commands. [Git behavior](git.md#collection-snapshot-queries) and
[metadata recovery](steps/file_history.md#recovery) define the outcomes.

On resume, the cloning step treats a repository as pending when its per-repository commit record
has the current format and records a commit history that was read, but its commit log is missing.
It does so even if the classified CSVs of the repository exist and its line records a time bound
that holds. A retry that fails keeps no such collection. The cloning step exits 4 while required
commit logs remain missing, whatever its ceiling of clone failures. It also treats as pending a
repository that the last commit reclassification found `failed`, while its per-repository commit
record is the record that the reclassification read. Packing the handover requires 7-Zip to exit
0. A 7-Zip warning makes the packing fail instead of delivering an archive that may omit evidence,
and the files in the project folder remain available for recovery.
