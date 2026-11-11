# Development subsample

`python -m agentcontext subsample` selects repositories for local pipeline tests from the
collected and validated trees, the published CSVs, the collection metadata and GHS's repository
sizes, and writes `data/collection/dev_subsample.csv`. The selected repositories cover every entry
that an eligible repository covers, and the command logs each entry that no repository under the
size limit covers.

## Selection

`match_heuristics` returns the detection heuristics that a path matches, and `heuristic_key` gives
the key of a heuristic in `coverage`. In the summary of the selection, `unmatched_heuristics`
lists the heuristics that no published row matches. `rules` is the name of a configuration
mechanism, not of a heuristic. The entry of a commit heuristic is `commit_heuristic:<id>`, with
the `id` of the row in `heuristics/ai_commit_patterns.csv` that `matched_heuristics` in the commit
CSV names.

A match mode for extensions can name several suffixes separated by `|`. The command matches each
suffix and keeps the scope of the heuristic, recursive or immediate children only.

The command builds a set of `coverage` entries for each repository. The entries name the matched
heuristics, mechanisms, tool flags and the code paths listed below. A path can match several
heuristics at the first depth at which it matches. For the heuristics of declarations and plugin
components, the command also inspects the validated tree, because a path alone does not identify
the configuration or plugin that declares it. The command logs a published row that matches no
heuristic, and that row adds no heuristic entry.

The repository sizes come from `output/repo-sizes.csv`, with positive GHS sizes as a fallback for
repositories that the file lacks. A repository that GitHub did not serve is no candidate for the
cover. It is a repository that `repo-sizes.csv` lists without a size, a repository that the
collection recorded as refused in `output/ai_commits-repos.csv` (i.e., `not_found_or_no_access`,
`dmca_takedown` or `disabled_by_github`), or a repository with a `REPO_GONE` outcome in
`output/snapshot-validation.csv`. Its clone would fail, and the supplement tier already has a
failed clone. A cover entry of such a repository would therefore never be exercised. The command
logs the repository's history outcome as uncovered. The default `--max-size-mib` is <!--s:parameters.agentcontext.tools.subsample.DEFAULT_MAX_SIZE_MIB-->1,200<!--/s-->. An entry with no eligible repository stays uncovered,
and the command logs the entry and keeps the limit.

The greedy selection repeatedly chooses the repository with the most uncovered entries. A tie
goes to the smaller repository, then to the repository name that sorts first. Each selected row
records only the entries that its choice newly covers. Repeated runs with the same inputs write
the same CSV bytes.

For the supplement tier, the command selects the smallest repository with a failed clone, the <!--s:parameters.agentcontext.tools.subsample.supplements.emptied:words-->two<!--/s-->
smallest repositories with an empty file as their only configuration, and the smallest repository
with a collected row that sets no flag. When the cover has already selected a repository that a
supplement needs, the repository's own row records the supplement's entry, and no repository is
listed twice. For example, the cover can already have selected the smallest repository with a
failed clone for its clone outcome. The command logs the current selection counts and sizes, and
the tracked CSV records the selected repositories.

## Coverage entries

The match mode distinguishes heuristics that share a path but load it differently. The entry of
a heuristic contains `tool|mechanism|path|match_mode`. A default component and a manifest
declaration therefore need separate entries, and the matched and unmatched counts use the same
keys. The command credits the plugin components that a manifest selects to their `manifest_field`
row, and a registered or declared context file to the row that declares it. A heuristic of a
default directory is covered only at the depth that it reads.

Some match modes are covered in their own way:

- A `skill_dir_flat` heuristic receives coverage only from a skill marker in an immediate child
  folder.
- The `json_plugin_mcp` mode matches the exact plugin MCP path, and the published rows have
  already passed the detector's check of the server map.
- The selection treats `tree_ext_described` as recursive, and the published rows have already
  passed the front matter test.
- `json_copilot_marketplace` matches its exact path.
- The command reads Antigravity's registrations from the validated tree for skills, subagents,
  commands, plugins and rules. A selected component covers the registration heuristic as well as
  any path heuristic that matches it independently.

In addition to `heuristic:<tool>|<mechanism>|<path>|<match_mode>`, `mechanism:<name>` and
`tool:<flag>`, the selection records these code paths:

| Entry | Source |
|---|---|
| `nested_mechanism_dir` | a heuristic matched below the first path segment, or a heuristic with the match mode `nested:` matched its place below the root |
| `case_variant` | a `file` or `basename` heuristic matched under another spelling |
| `row_without_git_metadata` | a CSV row with a blank `created_at` |
| `non_english_artifact` | a `language` that is neither `English` nor `n/a` |
| `non_utf8_artifact` | a configuration artifact in `data/validated/` that does not decode as UTF-8 |
| `binary_in_skills_dir` | a `skills.csv` row with `scripts` or `assets` `TRUE` |
| `branch_not_main_or_master` | the `branch` column of `output/repos.csv` |
| `submodule_recorded` | a row in `submodules.csv` |
| `deleted:inside_submodule`, `deleted:empty_directory`, `deleted:vendored`, `deleted:not_a_spec_file`, `deleted:irrelevant_github_file`, `deleted:no_mechanism`, `deleted:empty_file` | the cleanup's reasons in `deletion.csv`, without the detail in parentheses. The entries depend on the current collected input |
| `head_sha_unreachable` | outcome `GONE` in `snapshot-validation.csv` |
| `ai_commit:<tool>` | the `ai_tool` column of `all_ai_commits.csv`, split on the comma |
| `zero_ai_commits` | a `repos.csv` row with no row in the commit CSV |
| `many_refs` | <!--s:parameters.agentcontext.tools.subsample.MANY_BRANCHES-->100<!--/s--> or more `branches` in the sampling-frame row |
| `marker_at_dir:resolving`, `marker_at_dir:unresolved` | a collected symlink marker at a directory path in the heuristics, at the root or under a nested anchor, that `_resolve_dir` follows to a directory or does not |
| `marker_inside_dir:resolving`, `marker_inside_dir:unresolved` | a one-line file with no whitespace and no NUL naming a path (`paths.marker_target`), inside a mechanism directory, with a target that exists inside the tree or does not |
| `symlink:inside_tree`, `symlink:outside_tree` | a real symlink in `data/validated/`, by its own target |
| `collect_only_file` | a file under `data/collected/<repo>/` that no CSV row names and that a `collect_only` heuristic matches |
| `deleted:<reason>` for every other reason | each other reason that the cleanup records, as a lowercase slug, such as `deleted:wrong_case_claude.md` or `deleted:macos_finder_file` |
| `commit_heuristic:<id>` | each id in the `matched_heuristics` column of the commit CSV, the heuristic of `ai_commit_patterns.csv` that matched |
| `history:<outcome>` | an outcome of `output/ai_commits-repos.csv` other than `history_read` and `no_files`: `history_unavailable`, `history_read_failed`, a clone outcome |
| `agent_workflow:<tool>` | a workflow under `data/workflows/<repo>/` with a `uses:` line that runs that tool's documented action |
| `unmatched_path_glob` | an `other.csv` path with a path glob that `output/unmatched-paths.csv` lists |
| `stage_failure:merge_added_path` | a repository named in `STEP_FAILURES`, for the files that a merge added and that stopped the file history step of 2026-09-16 in `che-incubator/che-code` and `altinn/altinn-studio`, which no output CSV marks. The entry counts only when the collection that the command reads contains the repository |
| `workflow_command:<tool>` | a workflow under `data/workflows/<repo>/`, not a compiled `.lock.yml`, with a shell step that runs that tool's documented headless invocation |
| `agentic_workflow:<tool>` | a GitHub Agentic Workflow in the validated tree with that tool as its engine |

The marker and plugin checks require the collected and validated trees. An absolute Windows
symlink target counts as outside the tree on every host. The command finds the repository of a
deletion path by its `owner§repo` directory segment, whatever path precedes that segment.

## Output and commands

`dev_subsample.csv` has the columns `repo_name`, `tier`, `coverage` and `size_kb`. `tier` is
`cover` or `supplement`, and `coverage` is semicolon-separated. The rows use LF line endings and
keep the selection order. The command logs every choice, uncovered entry and summary to stderr as
`INFO subsample: ...`, and stdout is empty except for the command help.

The command reads the collected and validated trees, which only the working copy of the current
handover holds in full. Run it in the development container over that working copy, as
[development.md](../development.md#continuous-improvement-from-handovers) describes. First copy
`output/repo-sizes.csv` from the checkout, which `measure sizes` writes outside a run. Then compare
the selection with the tracked list. Changed collection outputs can change the selection, and you
write the list only after you have inspected each difference:

```shell
cp /code/data/collection/output/repo-sizes.csv output/
python3 -m agentcontext subsample --check --subsample /code/data/collection/dev_subsample.csv
python3 -m agentcontext subsample
```

`--check` compares the current selection with the tracked CSV without rewriting it. Exit 0 means
that the list was written or matches. Exit 2 means that the list would change, and exit 1 that the
command could not evaluate the selection.

The command fails on a missing input and names its path. When the plain commit CSV is absent and
only its archive is present, unsplit or in numbered volumes, the command extracts the single
commit CSV into a temporary directory. A missing, ambiguous, empty or malformed archive member
fails the command, because otherwise every repository would look as if it had no AI commits. The
command also fails when the selection has no rows.

The regression test of the command line checks both output channels and the component prefix. The
selection tests check literal choices, tie-breaking, uncovered entries and the reproduction of the
CSV against small fixture trees.

Generate a subsample from the current handover after you review the changed heuristics, and keep
its source lists and reports with that handover. A subsample selected from incomplete input does
not show complete path coverage.
