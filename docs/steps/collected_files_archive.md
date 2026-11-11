# Collected files archive

## Context

The validated tree under `data/validated/` contains files that the published dataset should not contain, such as `.gitignore` and `.DS_Store` files, lock files, logs, and the session state that an agent writes as it runs. This step copies the validated tree, removes these files from the copy and packs the rest. It publishes a configuration artifact that no detector classified instead of deleting it, and a later heuristic can therefore still classify the file.

The script copies the repository directories in `data/validated/` to a staging directory, without the `@eaDir` directories that Synology DSM writes. It deletes from the copy the files that `prune.is_prunable` matches, that is, the files in a place of a tool's local state and the files that `PRUNE_GLOBS` matches. It then removes the directories that the deletion empties and packs the result into `release/repos_data.7z`. The step copies only the repository directories, and `data/validated/repos.csv` and any other top-level entry stay out of the archive. The step never modifies or renames the validated tree. Every deletion happens in the staging copy. The step makes the copy in `repos/` inside the staging directory (`ARCHIVE_ROOT`), and `repos/` is the top-level directory of the archive. `--dry-run` logs what the step would delete and changes nothing.

The step packs the archive into a separate file beside the published archive, and it renames that file onto the published name only after 7zz reports success. It removes the staging copy whatever the outcome, also after a SIGTERM or a SIGINT. If the step removed the old archive first and packed onto the published name, a failure of 7zz would leave a truncated file under the published name, with the previous archive already deleted. If it removed the staging copy only after a successful pack, a failure would leave a second copy of the repository directories on the volume that the pre-checks had measured.

## Key decisions

- **Language:** Python 3.14 (`agentcontext/steps/collected_files_archive.py`, with `repos.py` for the test of whether a directory entry is a repository), with no dependencies outside the standard library besides the `7zz` binary
- **The validated tree is never modified:** every deletion happens in the staging copy. The step makes the copy by calling `paths.copy_tree` once per repository directory, as the collected file cleanup does. `paths.copy_tree` keeps symlinks as symlinks and lists the tree with `os.walk`, which keeps a stack of its own. A directory nested deeper than the recursion limit therefore does not end the step with the RecursionError that `shutil.copytree` raises. A failed or interrupted run leaves `data/validated/` intact, and no run renames it
- **Only repositories are published:** the staging copy contains only the entries that `repos.split` returns as repository directories, that is, real directories, not symlinks, with a name that contains the `owner§repo` separator. A copy of the whole validated tree would put anything else at that level into the archive, such as a list of inputs, and extracting the archive in place would put that file back. The collected file cleanup writes its list at the root of the tree that it validates. The step names `repos.csv` in the `expected` argument of `repos.split`, and the file is neither published nor logged. The step logs every other stray by name and leaves it where it is, because a stray is either an accident to remove or a naming change that nobody recorded
- **Everything outside a list of what is not configuration is published:** `PRUNE_GLOBS` lists files that configure nothing, such as `.gitignore`, `.DS_Store`, lock files, logs, `node_modules/` and `__pycache__/`. `heuristics/local_state_paths.csv` lists the places where the tools keep sessions and other local state, which the collected file cleanup already deletes from the validated tree. Everything else is published, including configuration artifacts that no detector currently classifies.
  A delete list taken from `output/other.csv` would delete, without a warning, every configuration artifact that the detector did not recognize. No later heuristic could then reclassify such a file, although the pipeline still treated it as configuration. The release of March 2026, built that way, lost its `.claude/rules` files. `prune.is_prunable` defines the exclusions independently of detection
- **What a file manager writes is not copied:** Synology DSM writes an `@eaDir` directory with the extended attributes and thumbnails of the files beside it into every directory that File Station shows. The collected file cleanup deletes every such directory from the validated tree. When someone opens `data/validated/` in File Station after the cleanup, such as to inspect the tree after a failed archive or release step, DSM writes them again before the next run of this step. The copy leaves out every directory that `FILE_MANAGER_DIRECTORIES` names (`paths.file_manager_directories`), at any depth of a repository directory, and the archive publishes no thumbnail.
  DSM also writes these directories into the staging copy, at any time up to and during the pack. 7zz therefore leaves them out as well (`ARCHIVE_EXCLUDES`, `-xr!@eaDir/`, in which the trailing `/` limits the wildcard to directories), and so does `_list_tree`, which lists the staging copy. `clean_empty_dirs` removes a directory that contains only such directories, and the archive has the content that a run without DSM packs. The copy leaves out only a directory, the form that the cleanup deletes, and it copies and packs a link with the name as a link. `.DS_Store`, which Finder writes, is in `PRUNE_GLOBS`. The release compares only the names of the repository directories in the archive with `output/repos.csv`, and this copy is therefore the only place that keeps such a directory out of the publication
- **A dry run changes nothing:** `--dry-run` logs one line per file that the step would delete and returns before copying, deleting, or archiving anything
- **The top-level directory of the archive is always `repos`:** `ARCHIVE_ROOT` is a constant and is not the name of the tree that the step packs. `_website/build_site.py` looks for exactly `repos` inside the extracted archive and stops with exit `1` when it does not find it. A root derived from `--validated-dir` would publish the repository directories under `data/validated/` and fail the site build
- **Nothing is renamed to pack the archive:** `7zz` records the name that it is given. The step makes the staging copy as `<staging>/repos` and packs it with the staging directory as the working directory. The alternative is to rename the validated tree aside to `<tree>_original` and move the staging copy into its place. For the default `data/validated`, that would create `data/validated_original` without a warning and move a top-level staging directory into `data/`. A process killed with SIGKILL between those two renames would leave the cleaned copy in the place of the validated tree and the validated repositories under a name that no step reads. A copy that the step renamed for the pack into a temporary directory beside the staging directory would stay there after a stop signal, outside what the next run removes, and every later handover would pack it. Because the copy stays inside the staging directory, a process killed at any point leaves the copy where the next run removes it, and the validated tree where it is
- **A stop signal removes the copy:** `_StopOnSignal` turns the first SIGTERM or SIGINT during the copy and the pack into `SystemExit(128 + signal)`. Stopping the container sends a SIGTERM, which the entrypoint passes to the runner and the runner to this step ([deploy](../deploy.md)). By default, a process that receives a SIGTERM ends without running its `finally` blocks. Without the handler, the copy of the sample would stay on the volume, and 7zz would go on packing without a process to wait for it. The exception ends 7zz, because `_run_7zz` kills 7zz and waits for it on any exception. A stop that arrives while `subprocess.Popen` starts 7zz would leave Popen after the process exists and before Popen returns it, where nothing could kill 7zz, and `subprocess.run` has that gap too. `_run_7zz` therefore makes the handler hold a stop while 7zz starts and raises the stop once the process exists, inside the block that kills 7zz. The step then removes the partial `.building` file and the staging directory and exits `143` for SIGTERM. It ignores a second signal, because the runner forwards a Ctrl+C that the step has already received from the process group, and a second exception during the removal would leave the copy behind. The runner does not record a step that a signal ended, and the next run of the runner runs the step again
- **A repository emptied entirely keeps its own directory:** `clean_empty_dirs` prunes empty subdirectories but never the repository directory itself. A repository that the prune empties remains as an empty top-level entry in the archive
- **Every removal of a directory handles what DSM writes:** `paths.remove_tree` removes the staging directory that a killed run left, the directories that the prune empties and the staging directory at the end. It also removes what DSM writes into a directory during its removal. A staging directory that cannot be removed at the end stays, with a warning, and the next run removes it before it copies

## `agentcontext/steps/collected_files_archive.py`

### Architecture

#### Delete list

`prunable_files` lists every repository directory of the tree and yields `(owner§repo, repository-relative path)` for every file that `prune.is_prunable` matches. The dry run and the real deletion both take their files from it and therefore cannot list different files. The step reads no input beyond the tree itself. `PRUNE_GLOBS` and `is_prunable` are defined in `agentcontext/prune.py`, which the cloning step's copy of the imports, the import detection step and the release also use. The code that copies the imports therefore does not import this step.

#### Dry run

When `--dry-run` is set, the script lists the prunable files of `data/validated/` itself, logs `Would delete <dir>/<path>` for each of them and then the total, and returns. It copies, deletes and archives nothing.

#### Real run

The step first removes any staging directory that a killed run left with its copy. It logs every stray by name and does not copy it. It copies each repository directory into `repos/` in the staging directory, with symlinks preserved and without the directories that `FILE_MANAGER_DIRECTORIES` names. `shutil.copystat` then gives `repos/` the mode and times of the source directory. The step sets them after the copies, because `data/validated/` is meant to be read-only, and that mode on an empty directory would deny the writes that the copies still need. The step then deletes and counts every file in the staging copy that `prune.is_prunable` matches. `clean_empty_dirs` prunes the empty subdirectories within each repository directory, and the step logs the number of remaining files.

Removing a file or a directory sets the modification time of its parent to the moment of the removal. 7zz records directory times, and two runs over the same tree would pack different bytes. After the prune, `restore_dir_times` therefore gives every directory of the copy, its root included, the times of the same directory in `data/validated/`. The copy has already preserved the times of files and links, which the prune does not touch.

#### Archiving

The step packs the archive to `<archive>.building` and renames it onto the published name only after `7zz` reports success ([Context](#context) gives the reason). Beforehand, the step removes a stale `.building` file that a killed run left, because `7zz a` updates an archive that it finds and would publish again a repository that has since left the sample. The published archive stays in place until the rename, because the release step reads it. `7zz a -t7z -snl -mx=9 -xr!@eaDir/ <absolute archive> repos`, run with the staging directory as its working directory, packs a tree rooted at `repos/`. When `7zz` exits non-zero, the step removes the `.building` file, logs the stderr of `7zz` and exits `1`. When `7zz` cannot start or a stop signal arrives, the step removes the `.building` file and the exception propagates. On success, the step renames the file onto the archive, removes the staging directory and logs the archive size.

### Output

| Path | Contents |
|------|----------|
| `release/repos_data.7z` (`--archive`) | The cleaned tree, rooted at `repos/`, without the paths that `prune.is_prunable` matches. Files listed in `other.csv` are retained unless `prune.is_prunable` matches them |

The staging directory exists only during a run, and the step removes it at the end, whether the pack succeeded or not. The step never writes `data/validated/`, its list or `output/`, and `--dry-run` produces no files.

Exit codes: `0` on success or a completed dry run, `1` on a missing input or a `7zz` failure, and `128` plus the signal after a SIGTERM or a SIGINT.

## Error handling & edge cases

| Scenario | Behavior |
|----------|-----------|
| `data/validated/` missing | `ERROR collected_files_archive: Could not find the validated tree` and exit `1` |
| `data/validated/` contains no repository directory | `ERROR` naming it and exit `1`. A list of repositories alone cannot supply a release archive. The line names the remedy: run `python -m agentcontext collected-file-cleanup` from `data/collection/` again, which derives the validated tree from `data/collected/`, and then run this step. When `data/collected/` is missing too, extract the collected tree from the handover first ([development](../development.md#handover-working-copy)) |
| `--dry-run` | Logs the would-delete lines and the total. Copies, deletes and archives nothing, and returns `0` |
| A repository with nothing to prune | Copied unchanged |
| A configuration artifact that no detector classified | Published. Only `prune.is_prunable` removes files |
| A repository emptied of every file | Its subdirectories are pruned, but the repository directory itself remains, as an empty entry in the archive |
| A top-level entry that is not a repository directory | Logged by name, not copied, not published, and left where it is. `repos.csv`, which the collected file cleanup writes at that level, is expected there and is neither published nor named |
| A symlink with a name that matches `PRUNE_GLOBS` | Deleted from the staging copy like any other match. `os.remove` unlinks the link itself and leaves the target untouched |
| A symlink with a missing target | Copied as a link and published as a link. The staging copy keeps every link as a link and resolves none. Following a missing target would raise `shutil.Error` before the step writes the archive |
| A symlink to a directory | Copied as a link, not followed. Following the link would write the target's contents a second time under the link's own path in the published archive |
| An existing staging directory | Removed and created again at the start of a real run |
| DSM writes an `@eaDir` into the staging copy, before the prune or during the pack | Left out of the archive. A directory that contains nothing else after the prune is removed, and `restore_dir_times` sets the times of the other directories of the copy only. The archive lists what a run without DSM lists |
| An existing archive | Left in place. The pack writes `<archive>.building` and renames it onto the archive only after `7zz` succeeds |
| `7zz` returns non-zero | The `.building` file and the staging directory are removed, the stderr of `7zz` is logged, and the step exits `1` |
| `7zz` raises (not on `PATH`) | The `.building` file and the staging directory are removed, and the exception propagates |
| SIGTERM or SIGINT during the copy or the pack, such as the SIGTERM that stopping the container sends, including while 7zz starts | 7zz is killed, the `.building` file and the staging directory are removed, and the step exits `128 + signal`. The next run packs the archive |
| Process killed with SIGKILL while it packs | No handler runs, and the cleaned copy stays inside the staging directory, which the next run removes before it copies. The validated tree is never renamed, and nothing has to be recovered by hand |

## Recovery after failed packing

When the pack fails, correct the cause that the `ERROR collected_files_archive` line names, such as
the stderr of `7zz`. Then run the step again before `release`, either as
`python -m agentcontext collected-files-archive` from `data/collection/` or through the next run of
the runner, which keeps a failed `collected_files_archive` pending. A failed pack leaves the
previous `release/repos_data.7z` in place, and an archive on disk therefore does not show that the
step packed the current validated tree. Do not record the step as complete with `--mark-complete`
because an older archive exists.
[Handover recovery](../deploy.md#recovery-from-handover-and-storage-errors) describes the recovery
when the entrypoint fails to pack a handover, which is a different archive.
