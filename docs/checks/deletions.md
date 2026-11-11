# The deletions check

`agentcontext/checks/deletions.py` provides `deleted-files-report`. The command checks
`deletion.csv` against the collected and validated trees, the mechanism CSVs and
`planning-documents.csv`. `deletion.csv` is the collected file cleanup's log of every file, empty
directory and repository that the cleanup removed.

```shell
python3 -m agentcontext deleted-files-report                       # exit 2 when an assertion fails
python3 -m agentcontext deleted-files-report --out deletions.csv   # and the CSV below
```

## Inputs and log messages

The check reads `deletion.csv`, `data/collected/` with `repos.csv` and `clone_failed.csv`,
`data/validated/` with its `repos.csv`, the mechanism CSVs and `planning-documents.csv` under
`output/`, and two heuristics files, `config_path_patterns.csv` and `not_configuration.csv`.

The check logs the rows per reason, with the number of repositories that have rows of that reason
and the repository with the most rows. It counts a reason without the detail that a run writes in
parentheses after it, and the rows of all submodules therefore make one line. The `--out` CSV
lists every repository for every reason.

A row `no detected mechanism, repository directory removed` records a repository but none of the
files that the repository still contained when the cleanup removed it. The check reads those files
from `data/collected/`. It leaves out every file and directory that the log names on a row of its
own, and every file that a file manager wrote after the cleanup, such as below an `@eaDir` that
Synology DSM writes on the NAS (`paths.is_discarded_path`). It groups the remaining files by path
glob, as `unmatched paths` does, and logs for each glob which file already names it:
`not_configuration.csv`, `config_path_patterns.csv`, `other.csv`, or none. It also counts the
removed repositories with no other row, for which the repository row is the only record.

For each of the three assertions below, the check logs `PASS`, `FAIL` with what failed, or
`NOT CHECKED` with the input that was missing. With `--out`, it logs the counts and the verdicts
and writes the lists to the file: the reasons, the path globs and the findings of a failed
assertion. The runner passes a file, and the container's log therefore contains the number of
entries and the file's path but no repository or path from the file. Without `--out`, the check
logs every row, because a person at a terminal has requested the lists.

## The reasons

The table in `docs/steps/collected_file_cleanup.md` defines the reasons, and
`tests/test_steps_collected_file_cleanup.py` checks that table against the strings that the step
writes. For a reader of the counts, the table below adds the heuristic or step that decides each
reason and whether that decision is a judgment call that a person could make the other way.

| Reason | Decided by | A judgment call |
|---|---|---|
| `symlink leading outside the repository` | Where the link resolves. Its target is in no tree of the repository | No |
| `empty file` | The file's content: whitespace, or JSON that parses to `{}` or `[]` | No |
| `non-JSON file in .github/hooks` | The `.github/hooks` heuristic, which matches the `.json` files there | No |
| `macOS Finder file` | The file's name, `.DS_Store` | No |
| `Synology DSM directory` | The directory's name, `@eaDir`, which Synology DSM writes into the directories that it visits | No |
| `inside submodule (<path>)` | `submodules.csv`: a file below a gitlink belongs to the repository that the link names | No |
| `inside vendored directory (<name>)` | `heuristics/vendored_dirs.txt` | Yes: A person keeps the list of directory names that contain another project's code |
| `local state of a tool (<place>)` | `heuristics/local_state_paths.csv`, with rows that cite the vendor's page or code | No, except for its `sample` rows, which were read off repositories |
| `wrong letter case of <file> (instead of <FILE>)` | The context file names in `config_path_patterns.csv`, and the root exception for a name in another case | Yes, below the root: A tool on a case-insensitive file system finds the file anyway |
| `SKILL.md outside valid directory` | The cleanup's skill locations, against what the cloning step copied | Sometimes, for a directory that no tool reads skills from. When the row is the target of a skill that a repository links individually, two steps treat one file in opposite ways, and the second assertion below detects the conflict under this reason or any other |
| `file in .github without Copilot configuration or a context file` | The `.github` directory that the collector copied for this decision, which contained no Copilot path and no context file | No |
| `file in .github outside the Copilot paths` | The Copilot paths of `config_path_patterns.csv`, inside a `.github` that contained one | No |
| `not a spec file` | Every file that no keep condition of the cleanup kept | Yes: Most rows are files in a plugin's directory that none of the plugin's parts names, or files beside a skill that a symlink points at. A tool may read either |
| `PLANS.md without Codex configuration or AGENTS.md` | The Codex check on `PLANS.md` | Yes: whether a `PLANS.md` configures anything without Codex beside it |
| `empty directory after cleanup` | The rows above: the topmost directory that each of them emptied | No |
| `no detected mechanism, repository directory removed` | The keep check, which requires one named mechanism | Yes, for the files that the repository still contained |

The rows of a deletion log raise these judgment calls:

- **A skills directory inside another tool's directory.** `effector/patronum` keeps a skill under
  `.cursor/.agents/skills/`, where the `.agents/skills` heuristic did not find it. Detection found
  no mechanism, and the cleanup removed the repository, including `SKILL.md`. Whether such a
  directory is a skills directory is a decision for the heuristics, based on the vendor page.
- **A repository removed with files that the cleanup kept.** The check lists those files because
  the repository row is their only record. Each glob among them that neither
  `not_configuration.csv`, `config_path_patterns.csv` nor `other.csv` names is a mechanism to add
  to `config_path_patterns.csv`, a row with a reason for `not_configuration.csv`, or a project's
  own material under a tool's directory.
- **A glob that only removed repositories have.** Because `other.csv` is built from the kept
  repositories, such a glob is in no output CSV. `unmatched paths` reads the removed repositories
  as its second input, and this check lists the same files, starting from the log.

A glob under a removed repository that no heuristics file names and that occurs in a single
repository gets no row. Such globs include a project's notes, plans and scratch files under a
tool's directory, such as `.claude/2026-modernization-plan.md` or `.cursor/milestones/**`, and
`effector/patronum`'s `.cursor/.agents/skills/`. That directory is none of the skills directories
that the cited pages name (i.e., `.claude/skills`, `.cursor/skills`, `.agents/skills` and the others
in `config_path_patterns.csv`). A glob in one repository is below the threshold of <!--s:parameters.agentcontext.tools.verify.unmatched_paths_fail_above:words-->two<!--/s--> that the verification suite applies with `unmatched paths --fail-above 2`. That threshold
is the standing decision for material that one project keeps under a tool's directory. A glob that
appears in a second repository makes the verification suite fail, and the glob then gets a row in
the heuristics.

## Assertions

| Assertion | Threshold | Why |
|---|---|---|
| No (repository, path) pair that a mechanism CSV or `planning-documents.csv` contains is a path that the cleanup removed, or lies under a directory or a repository that the cleanup removed | no finding | A file that is both published and deleted means that two steps treat one file in opposite ways |
| No `SKILL.md` that the cleanup deleted lies in a directory that a symlink marker of the collected tree names, whatever reason the log records, except `empty file` and `symlink leading outside the repository` | no finding | Such a row means that two steps treat one file in opposite ways: The collector followed a link and copied the skill, and the cleanup deleted it. The cleanup keeps a skill that a repository links individually, and inside a linked directory the vendored-directory condition deletes a file only when the file lies in a vendored directory below the linked directory. The assertion leaves out the rows of two passes that run before any keep condition, wherever a file lies. These rows show nothing about where a skill may be. Outside a linked directory, the cleanup deletes a `SKILL.md` by design, such as in a `.github` directory that no tool reads skills from or directly in a skills directory, and the check counts those rows rather than failing on them. Without the collected tree, the check counts the rows and does not check the assertion |
| Every repository directory of `data/collected/` that is missing from `data/validated/` has a repository row, and every repository row names a directory that is missing from `data/validated/` | equality | A missing directory without a row is a deletion that nothing else records |

The third assertion counts, rather than fails on, the directories that are missing from the
validated tree without a row but for a known reason. The cleanup does not copy a repository that
`clone_failed.csv` names. Neither does it copy a directory that `data/collected/repos.csv` does not
name, which remains from an earlier collection of a repository that is no longer in the input
list. A step after the cleanup can also remove a repository that `data/validated/repos.csv` still
lists: The file history step removes the directory of a repository that git reports as gone and
leaves the list unchanged. These counts have no threshold. They change with every collection, and
a threshold on them would fail a run for the normal change from one collection to the next.

## Key decisions

- **The step logs, and the command gives the verdict.** The runner's `deleted_files_report` step
  runs the check with `--report-only` after `unmatched_paths_report` and before the archive. The
  step writes `output/deletions.csv` and exits 0 whatever the assertions found, with a `WARNING`
  that names what failed. `docs/checks/unmatched.md` gives the same reasons for its step. The
  assertions check data that the run has already written. The archive and the publication that
  follow take minutes and publish nothing on their own: The release is a local directory, and the
  upload to Zenodo is a person's step. A stop would withhold the archive and the publication without
  changing the finding, which needs a change to a step and another run either way. Run by hand without the flag, the check exits 2 on the same findings
- **Not in the verification suite.** The verification suite runs in a clean checkout, which has no
  `deletion.csv`. Every run rewrites the log, which is gitignored, and the deletions check exits 1
  without it. The trees are not tracked either, and the third assertion could not run there. The
  verification suite runs `tests/test_steps_collected_file_cleanup.py`, which checks the reason
  strings that the check reads
- **A tree without repositories leaves an assertion not checked, and the exit code reports it.**
  You can remove the repository directories of both trees to save space. Their lists are tracked,
  and each tree's directory therefore exists in a clean checkout. When either tree contains no
  repository directory, the third assertion is `NOT CHECKED`. The second is `NOT CHECKED` too when
  the collected tree contains no repository directory and the log records a deleted `SKILL.md`. A
  collected tree without repository directories also leaves the files of the removed repositories
  unread. The check logs each of these gaps and exits 1 (could not check) when no assertion
  failed. A failed assertion exits 2, and `--report-only` exits 0. The
  check is about the log and the mechanism CSVs, and without either of them it exits 1 as well
- **The check does not consume `collected_file_cleanup`.** The step opens `data/validated/`. The
  runner's check for modified inputs covers the tree that the file history step prunes and looks
  for the pending steps that consume `collected_file_cleanup`. The deletions check tells the file
  history step's pruning apart from a deletion that the cleanup did not record by the validated
  list, which the file history step leaves unchanged. Running the check alone after the file
  history step is therefore correct, and the runner does not rebuild the tree for it. The check
  consumes `detection` and `file_history` and reads their CSVs. A rerun of
  `collected_file_cleanup` invalidates the check through these two steps
- **The reason strings that it reads are the collected file cleanup's.** The check spells out
  `SKILL.md outside valid directory`, `empty file` and `symlink leading outside the repository`,
  because the cleanup writes them inline. It imports
  `no detected mechanism, repository directory removed` from `agentcontext/checks/unmatched.py`,
  which reads the same rows. `tests/test_checks_deletions.py` runs the cleanup until it has
  written all four reasons

## The CSV

`--out` writes one row per count and per finding, in the columns `kind`, `repo_name`, `subject`,
`count` and `note`. The runner passes `output/deletions.csv`, which the handover includes and the
release publishes with `deletion.csv.7z`, the archive of the log.

| `kind` | `subject` | `count` | `note` |
|---|---|---|---|
| `reason` | The reason, without its detail | The rows of that reason in that repository | Empty |
| `dropped_glob` | A path glob under a repository that the cleanup removed for no detected mechanism | The files under it in that repository that no row names, without its planning documents and their anchors (`planning.find`), which a heuristic explains | The file that names the glob, or empty |
| `published_and_deleted` | The path that the cleanup removed, empty for the repository | The published rows under that path | The log's reason |
| `deleted_skill_md` | The path of a `SKILL.md` that the second assertion reads | 1 | The log's reason, after `linked, ` for a row that the assertion fails on |
| `repository_unrecorded`, `repository_row_still_validated`, `repository_row_never_collected` | Empty | 1 | Empty. The third assertion fails on these |
| `repository_not_copied`, `repository_not_listed`, `repository_removed_later` | Empty | 1 | Empty. `clone_failed.csv` explains `repository_not_copied`, the collected list `repository_not_listed`, and the validated list `repository_removed_later` |

## Error handling & edge cases

| Case | Behavior |
|---|---|
| `deletion.csv` is missing | `ERROR` naming it, exit 1 |
| `deletion.csv` has no `path` or `reason` column | `ERROR` naming the missing columns, exit 1 |
| A mechanism CSV or `planning-documents.csv` is missing, or lacks `repo_name` or its path column | `ERROR` naming it, exit 1. "Nothing published was deleted" is a claim about all of them |
| `data/collected/` or `data/validated/` contains no repository directory | The third assertion is `NOT CHECKED`. Without `data/collected/`, the second block names what it did not read. Exit 2 when another assertion failed, otherwise `ERROR` naming what was not checked and exit 1. Under `--report-only`, a `WARNING` and exit 0 |
| `data/collected/clone_failed.csv` is not UTF-8 | `ERROR` naming it, exit 1 |
| A log row with a path that has no repository segment | Counted under its reason with no repository. The first and the third assertions skip it, and the second names it with no repository |
| An assertion fails | Its findings logged, or counted and written to the file when `--out` names one, and `ERROR` on stderr, exit 2 |
| An assertion fails under `--report-only` | The same, with `WARNING` instead of `ERROR`, exit 0 |

**Language:** Python 3.14

The counts and the verdicts go to the same log stream on stderr. The check does not flush stdout
before a verdict, because it writes no counts there.
