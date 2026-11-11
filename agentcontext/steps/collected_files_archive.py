#!/usr/bin/env python3
"""
Prepare the archive of collected files for the publication.

The step copies the repositories in data/validated/ to repos/ in a staging directory, without
the directories that FILE_MANAGER_DIRECTORIES names. It removes from the copy the files that
`prune.is_prunable` matches and the directories left empty, and creates release/repos_data.7z.
The archive also leaves out the directories of FILE_MANAGER_DIRECTORIES that DSM writes into
the staging directory. The step never modifies or renames the validated tree data/validated/.
The archive is rooted at repos/ because 7zz packs that directory from inside the staging
directory. A stop removes the staging directory, as does the next run after a kill, and a
stopped step leaves no copy of the sample behind. The next run starts the step again.

The step copies only the repository directories, and a file beside them in data/validated/ is
not published. The collected file cleanup writes its list, data/validated/repos.csv, inside the
tree that it validates. The step names the list in the `expected` argument of repos.split, and
the list is neither published nor logged. Every other stray entry is logged by name and left
where it is (the docstring of repos.split gives the reason).

An explicit list of what is not configuration decides what is published, not what the
detectors recognize. A gap in a detector therefore deletes no configuration artifact from the
published archive, and an unrecognized configuration artifact is published and can be
classified later.

Produces: release/repos_data.7z
"""

import os
import shutil
import signal
import subprocess
import sys

from agentcontext import SEVEN_ZIP
from agentcontext.repos import is_repo_dir_name, split
from agentcontext import logs
from agentcontext import cli
from agentcontext import paths
from agentcontext.prune import is_prunable


VALIDATED_DIR = "data/validated"
STAGING_DIR = "repos_dataset_staging"
# Inside release/, because the archive is one file of the publication. The release step names
# it in RELEASE_FILES, computes its checksum and describes it in the README.
ARCHIVE = "release/repos_data.7z"
# The top-level directory of the published archive, which is not the name of the tree that the
# archive is packed from. _website/build_site.py looks for exactly `repos` inside the extracted
# archive. A root derived from the validated tree would publish the repository directories under
# data/validated/, and the site build would stop with an error.
ARCHIVE_ROOT = "repos"


class _StopOnSignal:
    """Raise SIGTERM and SIGINT as `SystemExit(128 + signal)` while the step copies and packs.

    A Stop sends SIGTERM, which by default ends the process without running its `finally`
    blocks. The staging copy of the sample would then stay on the volume for every later
    handover to pack, and 7zz would go on packing with no process waiting for it. Raised as an
    exception, the signal ends 7zz (`_run_7zz` kills it on any exception), and the step removes
    the staging copy and the partial archive. Only the first signal raises. The runner forwards
    a Ctrl+C that the step has already received from the process group, and a second exception
    during the removal would leave the copy behind.

    Between `hold` and `release`, the handler records the signal instead of raising it, and
    `release` raises it. `_run_7zz` holds a stop while it starts 7zz, because a stop raised
    during the start would leave a 7zz that no code can kill.
    """

    def __init__(self):
        self.stopping = False
        self.holding = False
        self.held = None

    def __enter__(self):
        self.previous = {sig: signal.signal(sig, self._stop)
                         for sig in (signal.SIGTERM, signal.SIGINT)}
        return self

    def _stop(self, signum, _stack_frame):
        if self.stopping:
            return
        self.stopping = True
        if self.holding:
            self.held = signum
            return
        raise SystemExit(128 + signum)

    def hold(self) -> None:
        """Record a stop from here on rather than raise it."""
        self.holding = True

    def release(self) -> None:
        """Raise the stop that arrived since `hold`, and raise a later stop when it arrives."""
        self.holding = False
        if self.held is not None:
            raise SystemExit(128 + self.held)

    def __exit__(self, *_exc):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)
        return False


def _run_7zz(argv: list, cwd: str, stop: _StopOnSignal) -> subprocess.CompletedProcess:
    """Run 7zz to its end, and return its exit code and its output as text.

    On any exception, such as the `SystemExit` of a stop, 7zz is killed and waited for before
    the exception leaves the function. A stop raised while `subprocess.Popen` starts 7zz would
    leave Popen after the process exists and before Popen returns it, and nothing would then
    kill 7zz. `subprocess.run` has the same gap and the one before its wait begins. The stop is
    therefore held while 7zz starts and raised inside the block that kills 7zz.
    """
    stop.hold()
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
    except BaseException:
        stop.release()
        raise
    # Leaving the block closes the pipes and waits for 7zz, after the kill on an exception.
    with process:
        try:
            stop.release()
            stdout, stderr = process.communicate()
        except BaseException:
            process.kill()
            raise
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


# The directories that Synology DSM writes, with the extended attributes and thumbnails of the
# files beside them, into the directories that its File Station and indexing services visit.
# The collected file cleanup leaves these directories out of its copy and deletes any that DSM
# writes into the validated tree, with the reason `Synology DSM directory`. The runner leaves
# them out of the manifest of the collected tree (`runner.DISCARDED_DIRECTORIES`). A visit to
# data/validated/ after the cleanup, such as after a failed archive or release step, writes them
# again, and the copy leaves them out (`paths.copy_tree`) so that the archive publishes no
# thumbnail. Only a directory with one of the names is left out, because the cleanup deletes only
# directories, and a link with the name is copied as any other link is. `.DS_Store`, the file
# that Finder writes, is in `prune.PRUNE_GLOBS`.
FILE_MANAGER_DIRECTORIES = paths.DISCARDED_DIRECTORIES
# DSM also writes these directories into the staging copy, at any time until the pack ends, and
# 7zz leaves them out too. A trailing `/` limits a 7zz wildcard to directories. A file with one of
# the names is packed, as the copy keeps it, and so is a link, which `-snl` stores as a link.
ARCHIVE_EXCLUDES = [f"-xr!{name}/" for name in sorted(FILE_MANAGER_DIRECTORIES)]


def _list_tree(root, topdown=True):
    """`os.walk` of `root` without the directories that `paths.file_manager_directories`
    names. DSM can write these directories into the staging copy after the copy, and they hold
    no file of a repository."""
    for cur, dirs, files in os.walk(root, topdown=topdown):
        relative = os.path.relpath(cur, root)
        if any(part in FILE_MANAGER_DIRECTORIES for part in relative.split(os.sep)):
            continue
        if topdown:
            left_out = paths.file_manager_directories(cur, dirs)
            dirs[:] = [d for d in dirs if d not in left_out]
        yield cur, dirs, files


def prunable_files(root):
    """Yield (repo_dir_name, repo_relative_path) for every prunable file."""
    # --dry-run uses the repository test of the copy and logs what a real run deletes.
    for dir_name in split(root)[0]:
        repo_path = os.path.join(root, dir_name)
        for cur, _dirs, files in _list_tree(repo_path):
            for name in sorted(files):
                full = os.path.join(cur, name)
                rel = os.path.relpath(full, repo_path)
                if is_prunable(rel):
                    yield dir_name, rel


def clean_empty_dirs(path):
    """Remove empty directories bottom-up and return the number removed.

    A directory that contains only directories that `paths.file_manager_directories` names
    counts as empty. DSM can write such a directory into a directory of the staging copy that
    the prune empties, and 7zz leaves it out. The emptied directory would otherwise be packed as
    an empty directory that a run without DSM does not pack. The removal goes through
    `paths.remove_tree`, because DSM can also write such a directory between the listing and the
    removal.
    """
    removed = 0
    for root, _dirs, _files in _list_tree(path, topdown=False):
        if root == path:
            continue
        names = os.listdir(root)
        if set(names) <= paths.file_manager_directories(root, names):
            paths.remove_tree(root)
            removed += 1
    return removed


def restore_dir_times(copy_root, source_root):
    """Give every directory under copy_root the times of the same directory under source_root.

    7zz records a directory's modification time, and removing a file or a subdirectory sets the
    parent's modification time to the moment of the removal. Every directory that the prune
    changes would otherwise have the time of the run, and two runs over the same tree would pack
    different bytes. The copy preserves the times of files and links, which the prune does not
    touch.
    """
    for cur, _dirs, _files in _list_tree(copy_root):
        rel = os.path.relpath(cur, copy_root)
        st = os.stat(os.path.join(source_root, rel))
        os.utime(cur, ns=(st.st_atime_ns, st.st_mtime_ns))


def main(argv=None):
    parser = cli.ArgumentParser(
        description="Copy the validated tree, remove the files that configure nothing, and pack "
                    "the copy into release/repos_data.7z.")
    parser.add_argument("--validated-dir", default=VALIDATED_DIR,
                        help="The validated tree, with one directory per repository "
                             f"(default: {VALIDATED_DIR})")
    parser.add_argument("--staging-dir", default=STAGING_DIR,
                        help=f"Staging directory for the pruned copy (default: {STAGING_DIR})")
    parser.add_argument("--archive", default=ARCHIVE,
                        help=f"Output archive path (default: {ARCHIVE})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log the files that a run would delete, without copying or "
                             "deleting anything")
    args = parser.parse_args(argv)

    validated_dir = args.validated_dir
    staging_dir = args.staging_dir
    archive = args.archive

    if not os.path.isdir(validated_dir):
        logs.error('collected_files_archive', f"Could not find the validated tree "
                                              f"{validated_dir}.")
        sys.exit(1)
    # The directory has to contain a repository directory, not only exist. The runner records
    # `collected_file_cleanup` by its script rather than its output, and no earlier check
    # detects a validated tree from which the repository directories were deleted. This step
    # would then pack an archive of no repositories and exit 0, and `release` would write the
    # Zenodo README and the checksums for that archive.
    if not any(is_repo_dir_name(e.name) for e in os.scandir(validated_dir) if e.is_dir()):
        logs.error('collected_files_archive', f"{validated_dir} contains no repository directory. Run "
                              f"`python -m agentcontext collected-file-cleanup` again to derive it from data/collected/.")
        sys.exit(1)
    if args.dry_run:
        deleted = 0
        repos = set()
        for dir_name, rel_path in prunable_files(validated_dir):
            logs.info('collected_files_archive', f"  Would delete {dir_name}/{rel_path}.")
            deleted += 1
            repos.add(dir_name)
        logs.info('collected_files_archive', f"Would delete {deleted:,} files in {len(repos):,} repositories.")
        return

    with _StopOnSignal() as stop:
        # Remove a staging directory that a killed run left, with its copy of the sample.
        if os.path.exists(staging_dir):
            logs.info('collected_files_archive', f"Removing the staging directory of an earlier run, {staging_dir}...")
            paths.remove_tree(staging_dir)
        # The collected file cleanup writes its list at the root of the tree, beside the
        # repository directories. Named here as expected, the list is in neither result. It is
        # not published, and it is not logged as a stray on every run, because nobody reads a
        # warning that appears on every run.
        repo_dirs, strays = split(validated_dir, expected=("repos.csv",))
        if strays:
            logs.info('collected_files_archive', f"Not publishing {len(strays):,} entry/entries "
                                                 f"in {validated_dir}/ that are not repository "
                                                 f"directories ({', '.join(strays)}).")
        logs.info('collected_files_archive', f"Copying {len(repo_dirs):,} repositories from {validated_dir}/ to {staging_dir}/{ARCHIVE_ROOT}/...")
        os.makedirs(staging_dir)
        try:
            return _prune_and_pack(validated_dir, staging_dir, archive, repo_dirs, stop)
        finally:
            # Remove the staging directory in every outcome, a Stop included. A failed 7zz or a Stop
            # would otherwise leave a second copy of the repository directories on the volume that
            # the pre-checks measured before the run, and every later handover would pack
            # the copy. A copy that cannot be removed stays until the next run removes it before
            # copying.
            try:
                paths.remove_tree(staging_dir)
            except OSError as error:
                logs.warning('collected_files_archive', f"Could not remove {staging_dir} "
                                                        f"({error}). The next run removes it "
                                                        f"first.")


def _prune_and_pack(validated_dir, staging_dir, archive, repo_dirs, stop):
    """Copy, prune and pack the tree under ARCHIVE_ROOT in the staging directory. The caller
    removes the staging directory, and `stop` is the caller's handler of a stop signal."""
    # Because 7zz records the name that it receives, the step makes the copy under ARCHIVE_ROOT
    # inside the staging directory, and 7zz packs that name from there. Nothing is renamed. A
    # kill at any point leaves the copy inside the staging directory for the next run to remove,
    # and the validated tree stays where it is.
    pack_root = os.path.join(staging_dir, ARCHIVE_ROOT)
    os.makedirs(pack_root)
    for name in repo_dirs:
        paths.copy_tree(os.path.join(validated_dir, name), os.path.join(pack_root, name))
    # The archive records the metadata of its root entry, and makedirs sets the mode from the
    # umask and the mtime to the moment of the run. The copystat runs after the copies, because
    # the validated tree is meant to be read-only, and that mode on an empty directory would deny
    # the writes that the copies still need.
    shutil.copystat(validated_dir, pack_root)

    # Delete the prunable files from the staging copy
    deleted = 0
    repos = set()
    for dir_name, rel_path in prunable_files(pack_root):
        os.remove(os.path.join(pack_root, dir_name, rel_path))
        deleted += 1
        repos.add(dir_name)

    # Clean up empty directories
    dirs_removed = 0
    for name in repo_dirs:
        dirs_removed += clean_empty_dirs(os.path.join(pack_root, name))

    logs.info('collected_files_archive', f"Deleted {deleted:,} files in {len(repos):,} repositories.")
    logs.info('collected_files_archive', f"Removed {dirs_removed:,} empty directories.")

    # Count remaining files
    file_count = 0
    for root, dirs, files in _list_tree(pack_root):
        file_count += len(files)
    logs.info('collected_files_archive', f"{file_count:,} files remain in {pack_root}/.")

    logs.info('collected_files_archive', f"Creating {archive}...")
    # The step packs the archive into a new file every time. `7zz a` updates an existing archive
    # rather than replacing it, and a repository that left the sample since the last run would
    # stay in the file without a warning.
    os.makedirs(os.path.dirname(os.path.abspath(archive)), exist_ok=True)
    # 7zz packs into a file beside the published name, and the step renames the file onto that
    # name only after 7zz reports success. A failed 7zz then leaves the previous archive, not a
    # truncated file, under the published name.
    building = os.path.abspath(archive) + ".building"
    for stale in (building,):
        if os.path.exists(stale):
            os.remove(stale)
    # After the prune, to undo the times that the prune changed.
    restore_dir_times(pack_root, validated_dir)
    try:
        result = _run_7zz(
            # -snl stores a symlink as a link. Without the flag, 7zz follows the link: it
            # expands a linked mechanism directory into a second copy of the tree that the link
            # points at, and it fails on a dangling link. The sample has links, dangling links
            # among them, and the archive contains both cases.
            # The archive path is absolute because 7zz runs in the staging directory.
            # ARCHIVE_ROOT is relative to that directory, and the stored paths start at repos/.
            [SEVEN_ZIP, "a", "-t7z", "-snl", "-mx=9", *ARCHIVE_EXCLUDES, building, ARCHIVE_ROOT],
            cwd=staging_dir, stop=stop,
        )
    except BaseException:
        # A stop or a 7zz that cannot start leaves no partial archive in release/, where the
        # release step refuses every file that is not one of its own.
        if os.path.exists(building):
            os.remove(building)
        raise

    if result.returncode != 0:
        if os.path.exists(building):
            os.remove(building)
        logs.error('collected_files_archive', f"7zz exited {result.returncode} while it packed "
                                              f"{archive} ({result.stderr.strip()}).")
        sys.exit(1)
    os.replace(building, os.path.abspath(archive))

    # The caller's `finally` removes the staging directory.
    logs.info('collected_files_archive', f"Cleaning up {staging_dir}/...")

    size_mb = os.path.getsize(archive) / (1024 * 1024)
    logs.info('collected_files_archive', f"Created {archive} ({size_mb:.1f} MiB).")


if __name__ == "__main__":
    raise SystemExit(main())
