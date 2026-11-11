"""The heuristics applied to a clone, and the row that the cloning step writes for it.

The per-tool collectors copy the paths that `config_path_patterns.csv` names out of a clone
into the repository's directory under `data/collected/`, and mark what they found in the flags
of the collected list, `data/collected/repos.csv`. They store a symlink as a marker file that
contains its target and leave out every name that is not UTF-8. A symlink to a file of the
repository with content that decided what they copy is the exception: they store it with that
content (`store_declaring_files`). This module also defines the layout of that list: the
header, the metadata block that precedes the flags, and the column at which the flags begin.
"""

import contextvars
import csv
import errno
import functools
import io
import json
import ntpath
import os
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import time
import tomllib
from collections import defaultdict
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import unquote, urlsplit
from agentcontext.heuristics import (LOCAL_STATE_PATHS, MANIFEST_FIELD_MODE, PLUGIN_PARENTS,
                                  PLUGIN_ROOT_SKILL_MODE, PLUGIN_ROOTS, SKILL_SUPPORT_DIRS,
                                  agent_plugins_names, context_declaration_heuristics,
                                  declaration_heuristics, default_place_row,
                                  manifest_key, nested_inner, path_alternatives,
                                  is_agent_plugins_document, is_directory_mode,
                                  is_folder_agent_file, load_config_path_patterns,
                                  manifest_field_rows, one_path, paths_for, paths_under,
                                  plugin_placeholder, rules_json_rows, spec_dir_paths,
                                  spec_dir_places, workflow_actions, workflow_commands,
                                  workflow_engines)
from agentcontext.repos import dir_name_to_repo
from agentcontext import jsonc, logs

COLLECTED_CSV_HEADER = ("repo_name,scanned_at,branch,head_sha,head_commit_date,first_commit_sha,"
                   "claude,gemini,codex,copilot,cursor,opencode,claude.md,gemini.md,agents.md,"
                   "plans.md,copilot-instructions,cursorrules")
# Named rather than sliced off a literal count, so that adding a metadata
# column cannot silently shift the first flag into the metadata block.
METADATA_COLUMNS = ("repo_name", "scanned_at", "branch", "head_sha", "head_commit_date",
                     "first_commit_sha")
FLAG_COLUMNS = COLLECTED_CSV_HEADER.split(",")[len(METADATA_COLUMNS):]
FLAG_COLUMNS_START = len(METADATA_COLUMNS)


def row_fields(main_row: str) -> list[str]:
    """The fields of a collected-list row, read the way a CSV reader reads them.

    Not `str.split(",")`, because git accepts a comma in a branch name. `build_csv_row` quotes
    the field that contains one, and a bare split on such a row shifts every column after it.
    """
    return next(csv.reader([main_row]), [])


def row_column(main_row: str, column: str) -> str:
    """One column of a collected-list row, by name."""
    return row_fields(main_row)[COLLECTED_CSV_HEADER.split(",").index(column)]


# The paths that this step copies come from config_path_patterns.csv, the heuristics that
# the collected file cleanup and the detection step read too. A mechanism added there is
# collected here without a second edit.
_PATTERNS = load_config_path_patterns()
# Each Copilot path inside .github, and whether it names a directory.
_GITHUB_KEEP = paths_under(_PATTERNS, ".github")
# The tool directories that a per-tool pass or `collect_shared` copies whole wherever it is, the
# rows of the heuristics with a mode of `SPEC_DIR_MODES`: `.claude`, `.gemini`, `.cursor`,
# `.codex`, `.opencode` and the Antigravity customization roots `.agents`, `.agent`, `_agents`
# and `_agent`. A real directory named `_agents` or `_agent` is one only where it contains a
# place of Antigravity (`is_spec_directory`). A tool reads such a directory through a symlink as
# the directory that the link leads to, and `collect_symlink_targets` collects a linked one.
# `.github` is no such directory: the Copilot pass copies it only with a Copilot path in it, and
# the collected file cleanup keeps only the Copilot paths and the agent workflows of it.
TOOL_DIRECTORIES = tuple(spec_dir_paths(_PATTERNS))
# The tool directories of the `spec_dir_with_place` rows, with the places that make each a
# tool directory (`heuristics.spec_dir_places`).
SPEC_DIR_PLACES = spec_dir_places(_PATTERNS)


def is_spec_directory(directory: Path) -> bool:
    """Whether `directory`, a real directory with the name of a tool directory, is one, which
    the steps copy and read whole.

    A directory of a `spec_dir_with_place` row, Antigravity's `_agents` or `_agent`, is one only
    where its listing names one of its places (`SPEC_DIR_PLACES`), such as `skills` or
    `mcp_config.json`, with a file or a link at or below the place. The customization discovery
    page places the rules, skills, agents, plugins, hooks and MCP servers of a customization
    folder there (https://antigravity.google/docs/customization-discovery, read 2026-11-05),
    and a private Python package or a documentation partial can have the same name, such as
    NVIDIA-NeMo/Curator's `nemo_curator/stages/audio/_agent/`. A place with only
    empty folders below it, such as a `skills/` with a `.gitkeep` that the collected file
    cleanup deleted as an empty file, makes no tool directory, so that the cleanup and the
    detection step decide alike.
    """
    places = SPEC_DIR_PLACES.get(directory.name)
    if places is None:
        return True
    try:
        names = os.listdir(directory)
    except OSError:
        return False
    return any(_holds_an_entry(directory / name) for name in names if name in places)


def _holds_an_entry(path: Path) -> bool:
    """Whether `path` is a file or a link, or a directory with a file or a link below it."""
    if path.is_symlink() or not path.is_dir():
        return True
    return any(filenames or any(os.path.islink(os.path.join(dirpath, d)) for d in dirnames)
               for dirpath, dirnames, filenames in os.walk(path))


def copy_file(src: Path, dest: Path) -> None:
    """Copy a single file. Symlinks become regular files containing the target string, which
    `store_declaring_files` replaces with the content for a file whose content the collector
    read."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        write_marker(dest, os.readlink(src))
    else:
        # Copied through a link at `dest`, the file would replace the file that the link
        # points at.
        if dest.is_symlink():
            dest.unlink()
        shutil.copy2(src, dest)


def write_marker(dest: Path, target: str) -> None:
    """Write the marker of a symlink with `target`: a one-line file that contains the target.

    rsync copies a link inside a directory that the collector copies whole as a link
    (`--links --safe-links`), and a later copy of the same path by name finds that link at
    `dest`. A link that leads to a file of the collected tree stays as rsync copied it, as
    `collect_symlink_targets` keeps it, because writing through it would replace that file,
    such as the `CLAUDE.md` that a `.claude/AGENTS.md` beside it links to. A link that leads
    nowhere is replaced by the marker, because writing through it would create its target."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink():
        if dest.exists():
            return
        dest.unlink()
    # The target is bytes that git stored, and a target that is not UTF-8 would raise here on
    # every run. Escaped, it is a string that a marker can contain.
    text = target.encode("utf-8", "surrogateescape").decode("utf-8", "backslashreplace")
    dest.write_text(text + "\n", encoding="utf-8")


def marker_target(text: str, base=None) -> str | None:
    """The target that the text of a symlink's marker (`write_marker`) names, or None where the
    text can be no marker: empty, of more than one line, with a NUL, or with a space, unless
    `base`, the folder of the marker, is given and the text names a path there. Every step that
    reads a marker decides with this function.

    The target is the text without the whitespace around it, unless `base` is given and the
    text without the line end that `write_marker` adds names a path there. A target can begin
    or end with a space, such as `../../shared/pdf `, and the collector copies it as git stored
    it. The stripped text would then name a path that the collected tree lacks.

    A text with a space is prose rather than a link, such as a rule of one sentence, except
    where it names a path that the collector copied, such as `../../Shared Skills/pdf`, which
    the step that resolves the marker would otherwise leave behind. A symlink's target cannot
    contain a NUL, and the path functions raise ValueError for a path with one. Without this
    test, a file of NUL bytes in a folder of rules or commands would stop the step that reads it
    as a marker."""
    written = text.removesuffix("\n")
    text = text.strip()
    if not text or "\n" in text or "\0" in text:
        return None
    if (written != text and base is not None and "\n" not in written
            and os.path.lexists(os.path.join(base, written))):
        return written
    if " " in text and (base is None or not os.path.lexists(os.path.join(base, text))):
        return None
    return text


def link_marker(text: str) -> bool:
    """Whether `text` is the marker of a link (`write_marker`) rather than a file's own text: a
    target that `marker_target` accepts, with a `/`, that is no import or reference of a file of one
    line, such as `@docs/notes.md`, `#file:docs/notes.md`, `(@docs/notes.md)` or
    `[notes](docs/notes.md)`, and that does not open with a byte order mark, which an editor writes
    and `write_marker` never does. A text without a `/` would name a file in the marker's own
    folder, which the collector copies with the marker, and a text of one word, such as `hello`, is
    therefore the file's own text. The detection step, the import detection step and the reader of
    the planning documents' headers decide with this function, so that a link reads as no text in
    each, as the clone's link does."""
    target = marker_target(text)
    return (target is not None and "/" in target and "](" not in target
            and not target.startswith(("@", "#", "(", "[", "\ufeff")))


def in_git_directory(rel) -> bool:
    """Whether the repository-relative path `rel` is in a `.git` directory.

    Such a file is the clone's own metadata and no file of the repository. git stores a symlink
    to `.git/logs/HEAD` as it was committed, and the file behind it in a clone is the log of
    that clone. No step reads it, and the collected tree contains none.
    """
    return ".git" in Path(rel).parts


def _mark_the_end_of_a_chain(link: Path, repo: Path, out: Path, end: Path) -> None:
    """Rewrite the marker of `link` to name `end`, the repository-relative path that the link
    resolves to, where its target, read without following links, is another path, because the
    link leads to another link or through a linked directory. Only `end` is copied, and the marker
    of the first hop would name a path that the collected tree lacks, such as the
    `../../.agents/skills/<name>` of NVIDIA/Model-Optimizer's `.claude/skills/<name>`, where
    `.agents/skills` links to `../plugins/modelopt/skills`. A marker that a pass replaced with
    the file's content, or a link that rsync kept, stays as it is."""
    literal = os.readlink(link)
    link_rel = link.relative_to(repo).as_posix()
    parent = posixpath.dirname(link_rel)
    if posixpath.normpath(posixpath.join(parent, literal)) == end.as_posix():
        return
    marker = out / link_rel
    if marker.is_symlink() or not marker.is_file():
        return
    try:
        # Compared as the marker's own text, which names the first hop, a path that the
        # collected tree lacks. `marker_target` with its folder would refuse such a path when it
        # contains a space.
        if marker.read_text(encoding="utf-8").removesuffix("\n") != literal:
            return
    except (OSError, UnicodeDecodeError):
        return
    write_marker(marker, posixpath.relpath(end.as_posix(), parent or "."))


def copy_symlink_target(link: Path, repo: Path, out: Path,
                        vendored_dirs: set[str], read_as=()) -> Path | None:
    """Copy what a symlink points at, when the target is inside the repository.

    The link itself is still recorded as a marker that contains its target string, but the
    marker alone loses the configuration artifact. A `.claude/skills` that points at a shared
    `.agents/skills` would reach the detector as a one-line text file and read as no skills at
    all. A directory is copied without the places of the tools' local state named from it and
    from each name of `read_as` (`copy_dir`). Returns the copied path, or None when the target is
    missing, outside the repository, in its `.git` directory (`in_git_directory`), contains
    the link itself, or is already collected.
    """
    try:
        target = (link.parent / os.readlink(link)).resolve()
        target.relative_to(repo.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    if not target.exists():
        return None
    rel = target.relative_to(repo.resolve())
    if in_git_directory(rel):
        return None
    # A target that contains the link is not a shared mechanism directory. Copying it writes
    # the surrounding tree into the collected configuration artifacts, source files and
    # dotfiles such as .env included, and rsync then replaces the marker written a moment
    # earlier with a live symlink, which copy_file never leaves behind. The repository root
    # is the widest such target, and an enclosing package directory is another.
    if link.parent.resolve().is_relative_to(target):
        return None
    _mark_the_end_of_a_chain(link, repo, out, rel)
    dest = out / rel
    # A directory is merged rather than skipped. Source and destination are the same
    # repository-relative path, and rsync's --delete removes nothing real. Skipping
    # on mere existence would lose the skills beside a file that an earlier pass already wrote
    # there, such as an AGENTS.md.
    if dest.is_file():
        return None
    if target.is_dir():
        copy_dir(target, dest, vendored_dirs, read_as)
    else:
        copy_file(target, dest)
    return rel


def _contains_skill(directory: Path) -> bool:
    """Whether a directory contains a SKILL.md of its own, in any case, which makes it a skill."""
    marker = _SKILL_MARKER.lower()
    try:
        return any(p.name.lower() == marker and p.is_file() for p in directory.iterdir())
    except OSError:
        return False


def is_vendored(directory: Path, vendored_dirs) -> bool:
    """Whether a directory is a vendored or build-output tree: its name is on the list, and it
    is not a skill folder. A skill is a folder with a SKILL.md, named for the skill, and
    unoplatform/uno's Claude Code skill `.claude/skills/docs-build` is collected
    whatever its name. A folder that contains only skills, such as
    open-metadata/openmetadata's `skills/vendor`, is still vendored."""
    return directory.name in vendored_dirs and not _contains_skill(directory)


def vendored_part(root: Path, rel, vendored_dirs) -> str | None:
    """The first part of `rel`, a path below `root`, that names a vendored directory, or None.
    A part that is no directory on disk, such as a file's name, is judged by its name alone."""
    current = root
    for part in Path(rel).parts:
        current = current / part
        if part in vendored_dirs and (not current.is_dir() or is_vendored(current, vendored_dirs)):
            return part
    return None


# The places of the tools' local state (`heuristics.load_local_state_paths`) by the tool
# directory that each starts with: (place, its components, whether it names a directory).
_LOCAL_STATE = {}
for _place in LOCAL_STATE_PATHS:
    _components = tuple(_place.rstrip("/").split("/"))
    _LOCAL_STATE.setdefault(_components[0], []).append(
        (_place, _components, _place.endswith("/")))


def local_state(path: str, is_dir: bool = False) -> str | None:
    """The place of a tool's local state that `path` is at or below, or None.

    `path` has `/` between its components. A directory place matches a directory with the
    place's components (`is_dir`) and every path below one, and a file place matches a file with
    its components, each component compared with `fnmatchcase`. A place starts with the name of
    a tool directory, and it matches wherever a component of `path` has that name, as the
    collector copies a tool directory at any depth.
    """
    parts = path.split("/")
    for i, part in enumerate(parts):
        for place, components, directory in _LOCAL_STATE.get(part, ()):
            end = i + len(components)
            if end > len(parts) or not all(fnmatchcase(name, pattern) for name, pattern
                                           in zip(parts[i + 1:end], components[1:])):
                continue
            if directory and (end < len(parts) or is_dir):
                return place
            if not directory and end == len(parts) and not is_dir:
                return place
    return None


def _named_from(root: Path, path: Path, name: str | None = None) -> str:
    """`path`, below `root`, relative to the directory that contains `root`, with `/` between its
    components, so that the name of `root`, or `name` where it is given, is its first component.
    A listing of a tool directory that the collector copies whole then starts with the
    directory's name, which `local_state` matches. The directories above `root` are left out, so
    that a clone below a directory with a place's name, such as a checkout in a tool's worktree,
    prunes nothing."""
    name = root.name if name is None else name
    rel = Path(os.path.relpath(path, root)).as_posix()
    if rel == ".":
        return name
    return f"{name}/{rel}" if name else rel


def is_pruned(directory: Path, vendored_dirs, root: Path, read_as=()) -> bool:
    """Whether a listing of `root` leaves out `directory`: a vendored tree (`is_vendored`) or a
    place of a tool's local state (`local_state`), named from `root` and from each name of
    `read_as`, the names of the links through which a tool reads `root`."""
    return (is_vendored(directory, vendored_dirs)
            or any(local_state(_named_from(root, directory, name), is_dir=True) is not None
                   for name in (root.name, *read_as)))


def is_local_state_file(path: Path, root: Path) -> bool:
    """Whether a listing of `root` leaves out the file `path` as a place of a tool's local
    state."""
    return local_state(_named_from(root, path)) is not None


def local_state_excludes(name: str) -> list[str]:
    """The rsync patterns that leave the places of local state out of a copy of the directory
    `name`: each place below `name` anchored at the copy's root, and every place unanchored, which
    rsync matches at the end of a path at any depth, for a tool directory nested in the copy."""
    anchored = [place[len(name):] for place in LOCAL_STATE_PATHS if place.startswith(name + "/")]
    return anchored + list(LOCAL_STATE_PATHS)


def list_repo_files(root: Path, vendored_dirs: set[str], read_as=()) -> Iterator[Path]:
    """Yield every file under root and every symlink that leads to a file or to nothing,
    skipping .git, vendored subtrees and the places of the tools' local state, named from `root`
    and from each name of `read_as` (`is_pruned`).

    A symlink that leads to a directory is no file of the listing, because `os.walk` lists it
    among the directories, and the passes that look for a file by its name would otherwise
    match a directory. `directory_links` yields such links."""
    names = (root.name, *read_as)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != ".git"
                       and not is_pruned(Path(dirpath) / d, vendored_dirs, root, read_as)]
        here = [_named_from(root, Path(dirpath), name) for name in names]
        for fname in filenames:
            if all(local_state(f"{named}/{fname}") is None for named in here):
                yield Path(dirpath) / fname


def directory_links(root: Path, vendored_dirs: set[str], read_as=()) -> Iterator[Path]:
    """Yield every symlink under root that leads to a directory, skipping what
    `list_repo_files` skips. `os.walk` lists such a link among the directories and does not
    descend into it."""
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != ".git"
                       and not is_pruned(Path(dirpath) / d, vendored_dirs, root, read_as)]
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                yield Path(dirpath) / d


class CopyFailed(subprocess.CalledProcessError):
    """rsync could not copy a configuration directory, with the directories and its error.

    A `CalledProcessError`, the error of any failed rsync, and not an `OSError`, because
    `collect_symlink_targets` passes over an `OSError` from one unreadable link, and a
    directory copied in part has to end the repository as `collector_error` instead.
    """

    def __init__(self, cmd: list, returncode: int, stderr: bytes, message: str):
        super().__init__(returncode, cmd, stderr=stderr)
        self.message = message

    def __str__(self) -> str:
        return self.message


# Files that the collector does not copy out of a configuration directory. Finder writes
# `.DS_Store` beside the files of a directory that a macOS user opened. No coding agent reads it,
# and the collected file cleanup deletes it wherever it is. The handover leaves it out too. A
# copied `.DS_Store` would be named in a per-repository commit record and be absent from the
# handover's tree, which would leave the reclassification of that record incomplete.
UNCOLLECTED_FILES = (".DS_Store",)


def is_uncollected(path: str) -> bool:
    """Whether a repository-relative path names a file that the collector does not copy."""
    return os.path.basename(path) in UNCOLLECTED_FILES


# Directories that Synology DSM writes into the directories that its File Station and indexing
# services visit, which on the NAS include the collected tree and the validated tree. DSM keeps
# the extended attributes and thumbnails of the files beside such a directory in it, as in
# `@eaDir/CLAUDE.md@SynoEAStream`. No coding agent reads such a directory, and no per-repository
# commit record names a file in one. The collected file cleanup deletes each from the validated
# tree with the reason `Synology DSM directory`. Only a directory is discarded, because DSM
# writes a directory, and a file or a link with the name is an ordinary entry.
DISCARDED_DIRECTORIES = frozenset({"@eaDir"})


def is_discarded_path(path: str) -> bool:
    """Whether a repository-relative path, with `/` between its components, names a file that a
    file manager wrote rather than the collector: a file that the collector does not copy
    (`is_uncollected`), or a file below a directory of `DISCARDED_DIRECTORIES`."""
    return (is_uncollected(path)
            or any(part in DISCARDED_DIRECTORIES for part in path.split("/")[:-1]))


def tree_files(tree: Path) -> list[str]:
    """The repository-relative paths of the files and links in a repository's directory of the
    collected tree or the validated tree, sorted and with `/` between their components, without
    the paths that `is_discarded_path` names.

    Every comparison of such a directory with a per-repository commit record lists the
    directory through this function, so that a file that DSM or Finder wrote into it on the NAS
    does not count as a copied file that the record lacks. The test is
    `is_symlink() or is_file()`, because `is_file()` follows a link and returns False for a
    broken one, and the collected trees contain broken links on purpose, such as the symlink
    marker of `dotnet/sdk`'s `.github/skills`. `rglob` does not follow a link to a directory,
    and every component of a listed path is a directory.
    """
    listed = (path.relative_to(tree).as_posix() for path in tree.rglob("*")
              if path.is_symlink() or path.is_file())
    return sorted(path for path in listed if not is_discarded_path(path))


# How many times `remove_tree` removes a directory again after DSM wrote into it during the
# removal, and how long it waits before each further try. DSM writes the files of one directory
# of `DISCARDED_DIRECTORIES` one after the other, and a removal right after the first file can
# meet the next one.
REMOVAL_TRIES = 5
REMOVAL_WAIT = 1.0


def remove_tree(path) -> None:
    """Remove the directory `path` with its contents, as `shutil.rmtree` does, while Synology
    DSM may write into it.

    DSM writes a directory of `DISCARDED_DIRECTORIES` into any directory of a shared folder that
    its indexing service visits, at any time, the trees of the pipeline included. Such a
    directory, written into a directory after `rmtree` listed it, makes the removal fail with
    ENOTEMPTY, and the step would then exit 1, after the cloning step's whole collection. The
    function removes such a directory and then the directory that contained it again, up to
    `REMOVAL_TRIES` times. Below a directory of `DISCARDED_DIRECTORIES`, every new
    entry is DSM's. An entry that is gone before its removal counts as removed. Any other error
    raises, and so does a directory that keeps any other new entry, which DSM did not write.
    """
    parent = os.path.dirname(os.path.abspath(path))

    def below_discarded(target: str) -> bool:
        relative = os.path.relpath(os.path.abspath(target), parent)
        return any(part in DISCARDED_DIRECTORIES for part in relative.split(os.sep))

    def written_by_dsm(target: str) -> bool:
        """Whether every entry of the directory `target` is one that DSM wrote."""
        if below_discarded(target):
            return True
        with os.scandir(target) as entries:
            return all(entry.name in DISCARDED_DIRECTORIES and entry.is_dir(follow_symlinks=False)
                       for entry in entries)

    def remove_again(function, target, error):
        if isinstance(error, FileNotFoundError):
            return
        if function is not os.rmdir or error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
            raise error
        for attempt in range(REMOVAL_TRIES):
            if attempt:
                time.sleep(REMOVAL_WAIT)
            try:
                if not written_by_dsm(target):
                    raise error
                with os.scandir(target) as entries:
                    for entry in list(entries):
                        if entry.is_dir(follow_symlinks=False):
                            shutil.rmtree(entry.path, onexc=remove_again)
                        else:
                            os.unlink(entry.path)
                os.rmdir(target)
                return
            except FileNotFoundError:
                return
            except OSError as again:
                if again is error or again.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                error = again
        raise error

    shutil.rmtree(path, onexc=remove_again)


def file_manager_directories(directory, names) -> set:
    """The names in `directory` that a copy or a listing of a tree leaves out: the directories,
    not links, named in `DISCARDED_DIRECTORIES`, and such a name that is gone, because DSM
    removed the directory after the listing that returned the name. A file with the name is
    kept. The signature is the one that `shutil.copytree` gives `ignore`."""
    left_out = set()
    for name in names:
        path = os.path.join(directory, name)
        if name in DISCARDED_DIRECTORIES and not os.path.islink(path) and (
                os.path.isdir(path) or not os.path.lexists(path)):
            left_out.add(name)
    return left_out


def copy_tree(src, dst) -> None:
    """Copy the directory `src` to `dst`, which must not exist, with each symlink copied as a
    symlink and without the directories that `file_manager_directories` names, as
    `shutil.copytree(src, dst, symlinks=True, ignore=file_manager_directories)` copies it.

    `copytree` calls itself once per level of the tree, and rsync copies a configuration
    directory of a clone whatever its depth. Over a directory nested deeper than the recursion
    limit, `copytree` would raise RecursionError and end the step that copies the tree. This
    copy lists the tree with `os.walk`, which keeps a stack of its own. It creates each
    directory before its contents and gives each directory the mode and times of its source
    after its contents, so that a source directory without write permission does not deny the
    writes into the copy. Leaving out what DSM writes keeps DSM from ending the copy too,
    because DSM can remove a file of an `@eaDir` between the listing and the copy, or write an
    `@eaDir` into a directory of `dst` before the copy reaches that directory of `src`. No step
    reads what DSM writes.
    """
    def fail(error: OSError):
        raise error

    def copy_link(source: str, destination: str) -> None:
        os.symlink(os.readlink(source), destination)
        shutil.copystat(source, destination, follow_symlinks=False)

    src, dst = os.fspath(src), os.fspath(dst)
    os.makedirs(dst)
    copied = []
    for current, dirs, files in os.walk(src, onerror=fail):
        target = os.path.normpath(os.path.join(dst, os.path.relpath(current, src)))
        copied.append((current, target))
        left_out = file_manager_directories(current, dirs)
        descended = []
        # `os.walk` lists a link to a directory among the directories and does not follow it.
        for name in dirs:
            source, destination = os.path.join(current, name), os.path.join(target, name)
            if name in left_out:
                continue
            if os.path.islink(source):
                copy_link(source, destination)
            else:
                os.mkdir(destination)
                descended.append(name)
        dirs[:] = descended
        for name in files:
            source, destination = os.path.join(current, name), os.path.join(target, name)
            if os.path.islink(source):
                copy_link(source, destination)
            else:
                shutil.copy2(source, destination)
    # In reverse, so that every directory gets its times after its contents were written.
    for source, target in reversed(copied):
        shutil.copystat(source, target)


def copy_dir(src: Path, dest: Path, vendored_dirs: set[str], read_as=()) -> None:
    """Copy a directory tree via rsync, excluding .git, vendored subtrees, the places of the
    tools' local state and `UNCOLLECTED_FILES`. The places are named from `src` and from each
    name of `read_as`, the names of the links through which a tool reads `src`, such as the
    `.claude` that leads to `config/claude`.

    A failed copy raises `CopyFailed` naming both directories, the exit status and the end
    of rsync's stderr. The cloning step records it as `collector_error` with that text.
    """
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        "rsync", "-a", "--links", "--safe-links", "--delete",
        "--exclude", ".git/",
    ]
    # A skill folder named like a vendored directory is copied. rsync takes the first filter
    # rule that matches, and the folder's anchored include comes before the excludes
    # by name.
    for dirpath, dirnames, _ in os.walk(src):
        dirnames[:] = [d for d in dirnames
                       if d != ".git"
                       and not is_pruned(Path(dirpath) / d, vendored_dirs, src, read_as)]
        for d in sorted(dirnames):
            if d in vendored_dirs:
                cmd += ["--include", f"/{(Path(dirpath) / d).relative_to(src).as_posix()}/"]
    for name in sorted(vendored_dirs):
        cmd += ["--exclude", name]
    for pattern in dict.fromkeys(pattern for name in (src.name, *read_as)
                                 for pattern in local_state_excludes(name)):
        cmd += ["--exclude", pattern]
    for name in UNCOLLECTED_FILES:
        cmd += ["--exclude", name]
    cmd += [f"{src}/", f"{dest}/"]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        # The end of stderr: rsync writes one line per file that it could not copy, and the
        # last lines contain its summary of why it stopped.
        error = result.stderr.decode("utf-8", "replace").strip()[-500:]
        raise CopyFailed(cmd, result.returncode, result.stderr,
                         f"rsync from {src} to {dest} exited {result.returncode} before copying "
                         f"the whole directory ({error or 'no error text'})")
    # --safe-links omits links that leave this copied directory, even when their
    # targets stay in the repository. Preserve the link text as copy_file does.
    for link in list_repo_files(src, vendored_dirs, read_as):
        if not link.is_symlink() or is_vendored(link, vendored_dirs):
            continue
        marker = dest / link.relative_to(src)
        if not marker.exists() and not marker.is_symlink():
            copy_file(link, marker)
    # Preserve the link text of a link to a directory as well, which `os.walk` lists among the
    # directories. While `collect_repo` runs, the marker is written only after every pass has
    # copied (`write_directory_link_markers`), because a pass can copy a file below the link's
    # path, such as a context file that a `.codex/config.toml` names through
    # `.codex/shared -> ../docs`, and a marker there would leave no directory for it.
    waiting = _DIRECTORY_LINKS.get()
    for link in directory_links(src, vendored_dirs, read_as):
        marker = dest / link.relative_to(src)
        if marker.exists() or marker.is_symlink():
            continue
        if waiting is None:
            copy_file(link, marker)
        else:
            waiting.append((link, marker))


# The links to directories that `copy_dir` left without a marker while `collect_repo` collects a
# clone, as (link, marker). `collect_repo` writes each marker where no pass has copied anything
# at its path (`write_directory_link_markers`). None outside `collect_repo`, where `copy_dir`
# writes the marker at once.
_DIRECTORY_LINKS: contextvars.ContextVar = contextvars.ContextVar("directory_links", default=None)


def write_directory_link_markers(links) -> None:
    """Write the marker of each link to a directory of `links`, (link, marker), that `copy_dir`
    left out, where no pass has copied a file or a directory at its path. A file that a pass copied
    below the path, through the link, stays, and the link gets no marker. A directory of the marker
    that a pass has removed since, such as a place of a tool's local state behind a linked tool
    directory (`remove_local_state`), stays removed."""
    for link, marker in links:
        if marker.parent.is_dir() and not marker.exists() and not marker.is_symlink():
            copy_file(link, marker)


def find_dirs_by_name(root: Path, name: str, vendored_dirs: set[str]) -> Iterator[Path]:
    """Yield real directories named `name` anywhere under root, skipping .git, vendored subtrees
    and the places of the tools' local state.

    A symlink to a directory is skipped, because a pass copies a directory at its own path.
    `collect_symlink_targets` collects a tool directory that is a link as the link's marker and
    the directory that it leads to, at that directory's path (`linked_tool_directories`). An
    empty directory is skipped too, because git tracks no empty
    directory, and an empty directory in a checkout is a submodule that the clone did not
    check out. Nothing in it can be collected, and a submodule named `.claude` sets no `claude`
    flag.
    """
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != ".git" and not is_pruned(Path(dirpath) / d, vendored_dirs, root)]
        for d in dirnames:
            full = Path(dirpath) / d
            if d == name and not full.is_symlink() and any(full.iterdir()):
                yield full


def _log_match(repo_display: str, src: Path, repo_root: Path, label: str) -> None:
    """Log a match line for each copy (enabled by --print-matches)."""
    rel = src.relative_to(repo_root)
    if src.is_symlink():
        logs.info('repository_cloning', f"Copied {rel} of {repo_display} as a symlink target "
                                        f"string ({label}).")
    else:
        logs.info('repository_cloning', f"Copied {rel} of {repo_display} ({label}).")


# The directories in which the collector looks for a SKILL.md. A plugin placeholder names no
# directory of the tree, and `collect_plugin_parts` copies a plugin's skills with the plugin.
_SKILL_MD_SPEC_DIRS = tuple(dict.fromkeys(
    r["path"].split("/")[0] for r in _PATTERNS
    if r["mechanism"] == "skills" and r["match_mode"] in ("skill_dir", "skill_dir_flat")
    and not plugin_placeholder(r["path"])))
_CLAUDE_PLUGIN_SIBLINGS = tuple(paths_for(_PATTERNS, "collect_only", "plugin_sibling"))
_SKILL_MARKER = one_path(_PATTERNS, "skills", "skill_marker")


def collect_skill_md(repo: Path, out: Path, print_matches: bool = False) -> set[str]:
    """Copy SKILL.md files found inside spec directories. Sets no flags."""
    display = dir_name_to_repo(repo.name)
    for spec_dir in _SKILL_MD_SPEC_DIRS:
        root = repo / spec_dir
        # Skip a missing or symlinked spec dir. `collect_symlink_targets` copies the directory
        # that a linked tool directory leads to whole, its SKILL.md files with it.
        if not root.is_dir() or root.is_symlink():
            continue
        for f in list_repo_files(root, set()):
            if f.name.lower() == "skill.md":
                if print_matches:
                    _log_match(display, f, repo, "SKILL.md")
                copy_file(f, out / f.relative_to(repo))
    return set()


_AGENT_PLUGINS_NAMES = agent_plugins_names(_PATTERNS)


def agent_plugins_document(match_mode: str, text: str) -> bool:
    """`heuristics.is_agent_plugins_document`, with a document nested deeper than the JSON
    parser reads, which raises RecursionError, counted as one that does not parse.

    The collector, the collected file cleanup and the detection step call this function. The
    test itself is in `heuristics.py`, and the guard is here because the code of
    `heuristics.py` is part of the `classified_by` digest of every per-repository commit record.
    """
    try:
        return is_agent_plugins_document(match_mode, text)
    except RecursionError:
        return False


def collect_agent_plugins(repo: Path, out: Path, vendored_dirs: set[str],
                          print_matches: bool = False) -> set[str]:
    """Copy the portable Agent Plugins documents, which their content identifies. Sets no flags.

    `plugin.json` and `mcp.json` are in the plugin root rather than in a vendor directory, and
    programs that have nothing to do with agents use both names. The function therefore copies
    only a document that declares the schema that the standard requires of it. The file is
    read here rather than copied and sorted out later, because the collected file cleanup would
    otherwise have to keep or delete a file that its name does not identify.

    The standard names no tool, as `AGENTS.md` and `SKILL.md` name none. A repository with a
    portable manifest as its only configuration artifact gets no tool flag and is
    kept for the mechanism that the detector finds in it.
    """
    display = dir_name_to_repo(repo.name)
    for f in list_repo_files(repo, vendored_dirs):
        mode = _AGENT_PLUGINS_NAMES.get(f.name)
        if mode is None:
            continue
        text = _repository_text(repo, f, errors="replace")
        if text is None or not agent_plugins_document(mode, text):
            continue
        if print_matches:
            _log_match(display, f, repo, f"{f.name} of Agent Plugins")
        copy_file(f, out / f.relative_to(repo))
    return set()


_CLAUDE_CONTEXT_NAMES = {r["path"].lower() for r in _PATTERNS
                         if r["tool"] == "claude"
                         and r["mechanism"] == "context_file"
                         and r["match_mode"] == "basename"}


_CLAUDE_ROOT_FILES = [r["path"] for r in _PATTERNS if r["tool"] == "claude"
                              and r["mechanism"] in ("context_file", "other")
                              and r["match_mode"] == "root_file"]


def collect_claude(repo: Path, out: Path, vendored_dirs: set[str],
                   print_matches: bool = False) -> set[str]:
    """Collect Claude Code configuration artifacts. Returns the set of flag names set to TRUE."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()

    for d in find_dirs_by_name(repo, ".claude", vendored_dirs):
        if print_matches:
            _log_match(display, d, repo, ".claude directory")
        copy_dir(d, out / d.relative_to(repo), vendored_dirs)
        flags.add("claude")

    mcp_json = repo / ".mcp.json"
    # A file or a link, which is copied as a marker. A directory with the name is no
    # configuration file of that path, and `copy_file` cannot copy one.
    if mcp_json.is_symlink() or mcp_json.is_file():
        if print_matches:
            _log_match(display, mcp_json, repo, ".mcp.json")
        copy_file(mcp_json, out / ".mcp.json")

    for f in list_repo_files(repo, vendored_dirs):
        if f.name == "managed-mcp.json":
            if print_matches:
                _log_match(display, f, repo, "managed-mcp.json")
            copy_file(f, out / f.relative_to(repo))

    for f in list_repo_files(repo, vendored_dirs):
        if f.name.lower() in _CLAUDE_CONTEXT_NAMES:
            if print_matches:
                _log_match(display, f, repo, f.name)
            copy_file(f, out / f.relative_to(repo))
            flags.add("claude")
            flags.add("claude.md")

    # A file of `.claude` that this pass copies by its path, such as `.claude/loop.md`, is copied
    # at its own path where `.claude` is a link, as `collect_symlink_targets` copies the
    # directory that the link leads to. Copied at the link's path, it would leave no place for
    # the link's marker.
    for rel in filter(None, (at_its_own_path(repo, rel) for rel in _CLAUDE_ROOT_FILES)):
        if (repo / rel).is_file():
            copy_file(repo / rel, out / rel)
            flags.add("claude")

    for d in find_dirs_by_name(repo, ".claude-plugin", vendored_dirs):
        if print_matches:
            _log_match(display, d, repo, ".claude-plugin directory")
        copy_dir(d, out / d.relative_to(repo), vendored_dirs)
        for sibling_name in _CLAUDE_PLUGIN_SIBLINGS:
            sibling = d.parent / sibling_name
            if sibling.is_file():
                if print_matches:
                    _log_match(display, sibling, repo, f".claude-plugin sibling {sibling_name}")
                copy_file(sibling, out / sibling.relative_to(repo))

    return flags


# Gemini paths that are copied but classify nothing: the two ignore files and
# the extension manifest. GEMINI.md is a context file and handled separately.
_GEMINI_MISC_NAMES = {r["path"] for r in _PATTERNS
                      if r["tool"] == "gemini"
                      and r["mechanism"] != "context_file"
                      and r["match_mode"] in ("basename", "root_file")}


def collect_gemini(repo: Path, out: Path, vendored_dirs: set[str],
                   print_matches: bool = False) -> set[str]:
    """Collect Gemini CLI configuration artifacts. Returns the set of flag names set to TRUE."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()

    for d in find_dirs_by_name(repo, ".gemini", vendored_dirs):
        if print_matches:
            _log_match(display, d, repo, ".gemini directory")
        copy_dir(d, out / d.relative_to(repo), vendored_dirs)
        flags.add("gemini")

    for f in list_repo_files(repo, vendored_dirs):
        if f.name in _GEMINI_MISC_NAMES:
            if print_matches:
                _log_match(display, f, repo, f.name)
            copy_file(f, out / f.relative_to(repo))

    for f in list_repo_files(repo, vendored_dirs):
        if f.name.lower() == "gemini.md":
            if print_matches:
                _log_match(display, f, repo, "GEMINI.md")
            copy_file(f, out / f.relative_to(repo))
            flags.add("gemini")
            flags.add("gemini.md")

    return flags


def collect_codex(repo: Path, out: Path, vendored_dirs: set[str],
                  print_matches: bool = False) -> set[str]:
    """Collect OpenAI Codex CLI configuration artifacts. Returns the set of flag names set to
    TRUE."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()

    for d in find_dirs_by_name(repo, ".codex", vendored_dirs):
        if print_matches:
            _log_match(display, d, repo, ".codex directory")
        copy_dir(d, out / d.relative_to(repo), vendored_dirs)
        flags.add("codex")

    for f in list_repo_files(repo, vendored_dirs):
        if f.name.lower() in {"agents.md", "agents.override.md"}:
            if print_matches:
                _log_match(display, f, repo, f.name)
            copy_file(f, out / f.relative_to(repo))
            flags.add("agents.md")

    if "codex" in flags or "agents.md" in flags:
        for f in list_repo_files(repo, vendored_dirs):
            if f.name.lower() == "plans.md":
                if print_matches:
                    _log_match(display, f, repo, "PLANS.md")
                copy_file(f, out / f.relative_to(repo))
                flags.add("plans.md")

    return flags


# Copilot's files outside .github, read at the root only, as .mcp.json is. A .vscode elsewhere
# in the tree belongs to a vendored package rather than to this repository's workspace, and the
# CLI looks for a bare marketplace.json at the root of the repository that it reads.
COPILOT_ROOT_FILES = [r["path"] for r in _PATTERNS
                 if r["path"].startswith(".vscode/")
                 or (r["tool"] == "copilot" and r["match_mode"] in ("file", "json_copilot_marketplace") and "/" not in r["path"])]


def collect_copilot(repo: Path, out: Path, vendored_dirs: set[str],
                    print_matches: bool = False) -> set[str]:
    """Collect GitHub Copilot configuration artifacts. Returns the set of flag names set to
    TRUE."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()
    github = repo / ".github"

    # Any path in the heuristics under .github is a reason to copy the directory. The
    # check exists because .github also contains workflows, issue templates and
    # everything else that a project keeps there.
    try:
        entries = {entry.name.lower(): entry for entry in github.iterdir()}
    except OSError:
        entries = {}

    def github_path(rel: str) -> Path | None:
        head, _, tail = rel.partition("/")
        entry = entries.get(head.lower())
        return entry / tail if entry is not None else None

    trigger = any((target.is_dir() if is_dir else target.exists())
                  for rel, is_dir in _GITHUB_KEEP
                  if (target := github_path(rel)) is not None)

    for rel in COPILOT_ROOT_FILES:
        f = repo / rel
        # A file or a link, as for `.mcp.json`, and not a directory with the name.
        if f.is_symlink() or f.is_file():
            if print_matches:
                _log_match(display, f, repo, rel)
            copy_file(f, out / rel)
            flags.add("copilot")

    if trigger and github.is_dir():
        if print_matches:
            _log_match(display, github, repo, ".github with a Copilot trigger")
        copy_dir(github, out / ".github", vendored_dirs)
        flags.add("copilot")
    elif print_matches:
        logs.info('repository_cloning', f"{display} has no Copilot trigger. Skipped .github.")

    instructions_file = github_path("copilot-instructions.md")
    instructions_dir = github_path("instructions")
    if ((instructions_file is not None and instructions_file.exists()) or
            (instructions_dir is not None and instructions_dir.is_dir())):
        flags.add("copilot-instructions")

    return flags


# The rows that a tool reads below the repository root, `nested:` in their match mode: the places
# of `.github` and the `.mcp.json` that Copilot CLI reads in its working directory and in each
# directory between it and the root.
NESTED_ROWS = [r for r in _PATTERNS if nested_inner(r["match_mode"]) is not None]
# The directories that their own rows read whole, at any depth. A place below one of them is a
# part of that directory, such as a `.github` inside `.claude`, and the root's `.github` is the
# rows' own place at the root.
_WHOLE_DIRS = frozenset(spec_dir_paths(_PATTERNS)
                        + paths_for(_PATTERNS, "other", "unclassified_dir"))


def nested_places(root: Path, vendored_dirs=frozenset()) -> list[tuple[str, dict]]:
    """Every place below the repository root that a `nested:` row reads, with the row, sorted.

    A row's path that starts with a directory, such as `.github`, names a place below a
    directory with that name in every directory below the root: a directory, real and not
    empty, outside `.git`, the vendored directories and the directories of `_WHOLE_DIRS`, which
    `find_dirs_by_name` finds. Below it, the first segment of the rest of the path is matched
    without regard to case, as `collect_copilot` matches the root's `.github`, and the place
    has to be a directory for a directory mode and a file otherwise (`_nested_place`). A row's
    path without a directory, such as `.mcp.json`, names a file with that name in every
    directory below the root outside the same directories, spelled as the root's file is copied
    (`collect_claude`).

    A place can be a link that leads to a file or a directory inside the repository, because
    Copilot CLI reads the place through the link, as it reads the root's. In dotnet/dotnet, for
    example, `src/msbuild/.github/copilot-instructions.md` links to `../AGENTS.md`. The
    collected tree holds such a link as its marker, which is a file (`collect_nested_places`),
    and there the marker of a link to a directory is a place of a directory mode.
    """
    root = Path(root)
    found = {}
    by_head = defaultdict(list)
    by_name = defaultdict(list)
    for heuristic_row in NESTED_ROWS:
        head, _, rest = heuristic_row["path"].partition("/")
        if rest:
            by_head[head].append((rest, heuristic_row))
        elif not is_directory_mode(heuristic_row["match_mode"]):
            by_name[head].append(heuristic_row)
    if by_name:
        for file in list_repo_files(root, set(vendored_dirs)):
            rel = file.relative_to(root).as_posix()
            above = rel.split("/")[:-1]
            if (file.name not in by_name or not above or any(part in _WHOLE_DIRS for part in above)
                    or not _nested_place(root, rel, directory=False)):
                continue
            for heuristic_row in by_name[file.name]:
                found[(rel, id(heuristic_row))] = heuristic_row
    for head, rows in by_head.items():
        for directory in find_dirs_by_name(root, head, set(vendored_dirs)):
            rel_dir = directory.relative_to(root).as_posix()
            above = rel_dir.split("/")[:-1]
            if not above or any(part in _WHOLE_DIRS for part in above):
                continue
            entries = {entry.name.lower(): entry for entry in directory.iterdir()}
            for rest, heuristic_row in rows:
                first, _, tail = rest.partition("/")
                entry = entries.get(first.lower())
                if entry is None:
                    continue
                place = (entry / tail if tail else entry).relative_to(root).as_posix()
                if not _nested_place(root, place,
                                     directory=is_directory_mode(heuristic_row["match_mode"])):
                    continue
                found[(place, id(heuristic_row))] = heuristic_row
    return sorted(((rel, heuristic_row) for (rel, _), heuristic_row in found.items()),
                  key=lambda item: (item[0], item[1]["mechanism"]))


def _nested_place(root: Path, rel: str, directory: bool) -> bool:
    """Whether the entry at `rel` is a place of a `nested:` row (`nested_places`).

    For a directory mode, the entry is a directory, or a link or the collection's marker of one
    that leads to a directory of the repository (`linked_nested_directory`). Otherwise, it is a
    regular file, such as a marker, or a link that leads to a regular file of the repository
    outside its `.git`. A link out of the repository leads to a file of the collecting machine,
    and a file in the `.git` of the clone belongs to the clone (`in_git_directory`)."""
    path = root / rel
    if directory:
        return ((path.is_dir() and not path.is_symlink())
                or linked_nested_directory(root, rel) is not None)
    if not path.is_symlink():
        return path.is_file()
    try:
        real = path.resolve()
        return (real.is_relative_to(root.resolve()) and real.is_file()
                and not in_git_directory(real.relative_to(root.resolve())))
    except (OSError, RuntimeError):
        return False


def linked_nested_directory(root: Path, rel: str) -> str | None:
    """The repository-relative directory that the entry at `rel` leads to, where the entry is a
    link or the collection's marker of one (`_linked_directory`). None where the entry is a
    directory itself, and None where it leads to no directory of the repository, to the root,
    into a `.git` directory or to a directory that contains it, because the collector copies no
    such target (`copy_symlink_target`)."""
    path = root / rel
    if path.is_dir() and not path.is_symlink():
        return None
    target = _linked_directory(root, rel)
    if not target or in_git_directory(target):
        return None
    parent = posixpath.dirname(rel)
    if parent == target or parent.startswith(target + "/"):
        return None
    return target


def collect_nested_places(repo: Path, out: Path, vendored_dirs: set[str],
                          print_matches: bool = False) -> set[str]:
    """Copy each place below the root that a `nested:` row reads (`nested_places`): a directory
    whole, as the root's `.github` is copied whole, and a file on its own. A place that is a
    link is copied as its marker, and the directory that a link at a place of a directory mode
    leads to is copied at its own path, as `collect_symlink_targets` copies the target of the
    root's `.github/agents` that is a link. Returns the flags of the rows' tools."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()
    for rel, heuristic_row in nested_places(repo, vendored_dirs):
        source = repo / rel
        if print_matches:
            _log_match(display, source, repo, f"{heuristic_row['path']} below the root")
        if source.is_dir() and not source.is_symlink():
            copy_dir(source, out / rel, vendored_dirs)
        else:
            copy_file(source, out / rel)
            target = (linked_nested_directory(repo, rel)
                      if is_directory_mode(heuristic_row["match_mode"]) else None)
            if target is not None and not (out / target).is_file():
                if source.is_symlink():
                    _mark_the_end_of_a_chain(source, repo, out, Path(target))
                copy_dir(repo / target, out / target, vendored_dirs)
        if heuristic_row["tool"] != "any":
            flags.add(heuristic_row["tool"])
    return flags


# The Cursor files outside .cursor: the deprecated rules file and the
# two ignore files. Only .cursorrules sets a flag.
_CURSOR_MISC_NAMES = {r["path"] for r in _PATTERNS
                      if r["tool"] == "cursor"
                      and r["match_mode"] in ("basename", "root_file")}


def collect_cursor(repo: Path, out: Path, vendored_dirs: set[str],
                   print_matches: bool = False) -> set[str]:
    """Collect Cursor configuration artifacts. Returns the set of flag names set to TRUE."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()

    for d in find_dirs_by_name(repo, ".cursor", vendored_dirs):
        if print_matches:
            _log_match(display, d, repo, ".cursor directory")
        copy_dir(d, out / d.relative_to(repo), vendored_dirs)
        flags.add("cursor")

    for f in list_repo_files(repo, vendored_dirs):
        if f.name in _CURSOR_MISC_NAMES:
            if print_matches:
                _log_match(display, f, repo, f.name)
            copy_file(f, out / f.relative_to(repo))
            if f.name == ".cursorrules":
                flags.add("cursorrules")
                flags.add("cursor")

    return flags


# OpenCode's files outside .opencode: its configuration and the settings of its terminal
# interface, which the rows read at the repository root.
OPENCODE_ROOT_FILES = [r["path"] for r in _PATTERNS
                        if r["tool"] == "opencode" and r["match_mode"] == "root_file"
                        and "/" not in r["path"]]


def collect_opencode(repo: Path, out: Path, vendored_dirs: set[str],
                     print_matches: bool = False) -> set[str]:
    """Collect OpenCode configuration artifacts. Returns the set of flag names set to TRUE."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()

    for d in find_dirs_by_name(repo, ".opencode", vendored_dirs):
        if print_matches:
            _log_match(display, d, repo, ".opencode directory")
        copy_dir(d, out / d.relative_to(repo), vendored_dirs)
        flags.add("opencode")

    for rel in OPENCODE_ROOT_FILES:
        f = repo / rel
        # A file or a link, which is copied as a marker, and not a directory with the name.
        if f.is_symlink() or f.is_file():
            if print_matches:
                _log_match(display, f, repo, rel)
            copy_file(f, out / rel)
            flags.add("opencode")

    # The prompt files of the agents, which no other pass copies outside `.opencode`.
    for rel in opencode_prompt_files(repo, vendored_dirs):
        dest = out / rel
        if not (dest.exists() or dest.is_symlink()):
            if print_matches:
                _log_match(display, repo / rel, repo, "prompt file of an OpenCode agent")
            copy_file(repo / rel, dest)

    return flags


# The places and keys of OpenCode's agents: the `json_servers:` rows of the subagents of
# OpenCode, such as `agent` in `opencode.json`, with the content format of each file.
_OPENCODE_AGENT_KEYS = defaultdict(list)
for _row in _PATTERNS:
    if (_row["tool"] == "opencode" and _row["mechanism"] == "subagents"
            and _row["match_mode"].startswith("json_servers:")):
        _OPENCODE_AGENT_KEYS[(_row["path"], _row["content_format"])].append(
            _row["match_mode"].partition(":")[2])
# The keys of an OpenCode agent that hold its prompt: `prompt` in the format of OpenCode 1,
# `prompt: Schema.optional(Schema.String)` (packages/core/src/v1/config/agent.ts of
# anomalyco/opencode v2.2.6), and `system` in the format of OpenCode 2,
# `system: Schema.String.pipe(optional)` (packages/schema/src/config/agent.ts of v2.2.6).
_OPENCODE_PROMPT_KEYS = ("prompt", "system")
# A reference to a file in the text of an OpenCode configuration, which OpenCode replaces with
# the file's content: `text.matchAll(/\{file:[^}]+\}/g)` in packages/core/src/config/variable.ts of
# v2.2.6 and packages/opencode/src/config/variable.ts of v1.19.5.
_OPENCODE_FILE_REFERENCE = re.compile(r"\{file:([^}]+)\}")


def opencode_prompt_files(repo: Path, vendored_dirs=frozenset()) -> list:
    """The repository-relative paths of the files that the prompts of the agents of the OpenCode
    configurations name, each once, in the order of the configurations and their agents.

    OpenCode replaces a `{file:path}` reference in a configuration file with the content of the
    file: "Use {file:path/to/file} to substitute the contents of a file" and "File paths can be:
    Relative to the config file directory" (opencode.ai/docs/config, read 2026-11-07), as
    `path.resolve(configDir, expandedPath)` with
    `const configDir = input.type === "path" ? path.dirname(input.path) : input.dir` resolves it
    (packages/core/src/config/variable.ts of v2.2.6). cakmoel/Scriptlog's `opencode.json` gives
    the subagent playwright-test-generator the prompt
    `{file:.opencode/prompts/playwright-test-generator.md}`. The file is a part of the agent that
    names it, as the prompt file of an agent action is a part of its workflow
    (`action_prompt_files`). The collector copies it, the cleanup keeps it, and the detection
    counts it with the configuration that names it, also for a built-in agent that the
    configuration adjusts. A Markdown agent substitutes no reference, because its body is its
    prompt, `prompt: md.content.trim()` (packages/opencode/src/config/agent.ts of v1.19.5). The
    configurations are the files of the `json_servers:` rows of OpenCode's subagents, a bare
    filename at the root and a file of `.opencode` in every `.opencode` directory
    (`find_dirs_by_name`). A path in the home directory, an absolute path and a path that names no
    file of the repository name nothing (`_named_file`)."""
    repo = Path(repo)
    folders = {}
    found = []
    for (place, content_format), keys in _OPENCODE_AGENT_KEYS.items():
        head, _, rest = place.partition("/")
        if rest and head not in folders:
            folders[head] = [d.relative_to(repo).as_posix()
                             for d in find_dirs_by_name(repo, head, vendored_dirs)]
        configurations = [f"{folder}/{rest}" for folder in folders[head]] if rest else [place]
        for configuration in configurations:
            text = _repository_text(repo, repo / configuration)
            if text is None:
                continue
            try:
                data = jsonc.loads(text, content_format)
            except (ValueError, RecursionError):
                continue
            if not isinstance(data, dict):
                continue
            for key in keys:
                agents = data.get(key)
                for agent in (agents.values() if isinstance(agents, dict) else ()):
                    if not isinstance(agent, dict):
                        continue
                    for prompt_key in _OPENCODE_PROMPT_KEYS:
                        prompt = agent.get(prompt_key)
                        for named in (_OPENCODE_FILE_REFERENCE.findall(prompt)
                                      if isinstance(prompt, str) else ()):
                            target = _named_file(repo, configuration, named, False, vendored_dirs)
                            if target is not None and target not in found:
                                found.append(target)
    return found


# ---------------------------------------------------------------------------
# Context files that configuration names
# ---------------------------------------------------------------------------
# Gemini CLI reads the files that `context.fileName` names beside GEMINI.md, and an extension
# reads the file that its manifest names. Codex reads the file that `model_instructions_file`
# names and, where a directory has no AGENTS.md, the first of `project_doc_fallback_filenames`
# that it finds there. Which file that is depends on the value in the configuration. A path
# therefore cannot decide the declaring rows, and every step calls `declared_context_files`
# instead: the cloning step over the clone, the collected file cleanup over the collected tree,
# and the detection step and the subsample over the validated tree.
_CONTEXT_DECLARATION_HEURISTICS = context_declaration_heuristics(_PATTERNS)
# Codex's standard context names, which take precedence over configured fallback names: its own
# override file and the shared AGENTS.md.
_CODEX_CONTEXT_NAMES = frozenset(r["path"] for r in _PATTERNS
                                  if r["tool"] in ("any", "codex") and r["mechanism"] == "context_file"
                                  and r["match_mode"] == "basename")


def _declared_values(root: Path, path: Path, content_format: str, key: str) -> list:
    """The values under the dotted `key` of a JSON or TOML file of `root`, in the order of the
    file. A `*` in `key` stands for every key at its level, such as the name of each role in
    `agents.*.config_file`, and a key without one has one value at most.

    A file that does not parse declares nothing, as its tool reads none of it, and neither
    does a file that `_repository_text` does not read. A TOML file is recognized by its
    extension, and JSON is read in the format that the row gives.
    """
    text = _repository_text(root, path)
    if text is None:
        return []
    try:
        data = (tomllib.loads(text) if path.suffix == ".toml"
                else jsonc.loads(text, content_format))
    except (ValueError, RecursionError):
        # json.JSONDecodeError and tomllib.TOMLDecodeError are ValueErrors.
        return []
    values = [data]
    for part in key.split("."):
        values = [child for value in values if isinstance(value, dict)
                  for child in (value.values() if part == "*" else (value.get(part),))]
    return [value for value in values if value is not None]


def _declared_value(root: Path, path: Path, content_format: str, key: str):
    """The value under the dotted `key` of a JSON or TOML file of `root`, or None where there
    is none (`_declared_values`)."""
    values = _declared_values(root, path, content_format, key)
    return values[0] if values else None


def _declared_names(value) -> list[str]:
    """The filenames that a value names: one string, or the strings of a list, in order."""
    names = [value] if isinstance(value, str) else value if isinstance(value, list) else []
    return list(dict.fromkeys(n.strip() for n in names if isinstance(n, str) and n.strip()))


def _stays_inside(root: Path, rel: str) -> bool:
    """Whether the repository-relative `rel` resolves inside `root`.

    Git stores a symlink as it was committed, and a folder of a clone can lead to `/`, to
    the user's home or, through `../..`, into the clones beside it. What such a path names belongs
    to the collecting machine, and `docs/steps/repository_cloning.md` states that a target outside
    the repository is never copied.
    """
    try:
        return (root / rel).resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _repository_text(root: Path, path: Path, errors: str = "strict") -> str | None:
    """The text of the file at `path`, or None where it does not resolve to a regular file
    inside `root` and outside its `.git` directory (`in_git_directory`), or is not UTF-8 under
    `errors`.

    A file of a clone is a symlink where git stored one, and it can lead to `/dev/zero`, which
    has no end, to a FIFO, which blocks the reader, to a file of the collecting machine, which
    is not the repository's (`_stays_inside`), or to the clone's own log in `.git`. Every reader
    of a file with content that decides what the steps select, such as a registration file or a
    settings file that names a context file, reads it through this function.
    `store_declaring_files` stores a file that this function read with that content. A file
    that it did not read stays a marker, from which the collected file cleanup and the detection
    step read nothing, as the collector did.
    """
    try:
        real = Path(path).resolve()
        real_root = Path(root).resolve()
        if (not real.is_relative_to(real_root) or in_git_directory(real.relative_to(real_root))
                or not stat.S_ISREG(real.stat().st_mode)):
            return None
        text = real.read_text(encoding="utf-8", errors=errors)
    except (OSError, RuntimeError, UnicodeDecodeError):
        return None
    declaring = _DECLARING_FILES.get()
    if declaring is not None:
        declaring.add(Path(path))
    return text


# The files that `_repository_text` reads while `collect_repo` collects a clone, each a file whose
# content decides what the cloning step copies (`store_declaring_files`). None outside
# `collect_repo`, where the collected file cleanup and the detection step read a collected tree.
_DECLARING_FILES: contextvars.ContextVar = contextvars.ContextVar("declaring_files", default=None)


def _named_at(name: str, rel: str):
    """The directory relative to which `rel` is `name`, else None."""
    if rel == name:
        return ""
    if rel.endswith("/" + name):
        return rel[:-len(name) - 1]
    return None


def _expand_braces(pattern: str) -> list[str]:
    """The patterns that the braces of a glob pattern stand for, as minimatch expands
    `{a,b}`: the first group of alternatives with a comma, nested groups included, each
    expanded in turn. A group without a comma stays as written."""
    depth, start = 0, None
    for i, c in enumerate(pattern):
        if c == "\\":
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}" and depth:
            depth -= 1
            if depth == 0:
                inner, parts, level, last = pattern[start + 1:i], [], 0, 0
                for j, d in enumerate(inner):
                    level += {"{": 1, "}": -1}.get(d, 0)
                    if d == "," and level == 0:
                        parts.append(inner[last:j])
                        last = j + 1
                if not parts:
                    continue
                parts.append(inner[last:])
                return [expanded for part in parts for expanded in _expand_braces(
                    pattern[:start] + part + pattern[i + 1:])]
    return [pattern]


def _glob_component(component: str) -> str:
    """A regular expression for one component of a glob pattern: `*` for any name or part of
    one, `?` for one character, a bracket expression for one character of a set, `\\` before
    a character for the character itself."""
    out, i = [], 0
    while i < len(component):
        c = component[i]
        if c == "\\" and i + 1 < len(component):
            out.append(re.escape(component[i + 1]))
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[":
            # A `]` right after `[`, or after its `!` or `^`, is a member of the set.
            first = i + 2 if component[i + 1:i + 2] in ("!", "^") else i + 1
            end = component.find("]", first + 1 if component[first:first + 1] == "]" else first)
            if end == -1:
                out.append(re.escape(c))
                i += 1
                continue
            members = component[first:end]
            negated = first == i + 2
            out.append("[" + ("^/" if negated else "")
                       + "".join("\\" + m if m in "\\^[]" else m for m in members) + "]")
            i = end + 1
            continue
        else:
            out.append(re.escape(c))
        i += 1
    return "".join(out)


def glob_paths(pattern: str) -> re.Pattern | None:
    """A regular expression over repository-relative paths for a glob pattern that a tool
    matches against the repository root, as node-glob matches it with `dot: true`: a `**`
    component for any number of directories, dotted names included, and braces, `*`, `?` and
    bracket expressions as `_expand_braces` and `_glob_component` read them. None where the
    pattern names no file of the repository: a URL, a path in the home directory, an absolute
    path, or a path with a `..` component."""
    pattern = pattern.strip()
    if (not pattern or "\0" in pattern or pattern.startswith(("~", "/", "\\"))
            or ntpath.isabs(pattern) or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", pattern)):
        return None
    alternatives = []
    for expanded in _expand_braces(pattern):
        components = [c for c in expanded.split("/") if c not in ("", ".")]
        if not components or ".." in components:
            continue
        parts = []
        for index, component in enumerate(components):
            last = index == len(components) - 1
            if component == "**":
                parts.append("(?:[^/]+/)*[^/]+" if last else "(?:[^/]+/)*")
            else:
                parts.append(_glob_component(component) + ("" if last else "/"))
        alternatives.append("".join(parts))
    return re.compile("|".join(f"(?:{a})" for a in alternatives)) if alternatives else None


def at_its_own_path(root: Path, rel: str) -> str | None:
    """`rel` with each tool directory of it that is a symlink, or the collection's marker of one,
    replaced by the directory that it leads to (`_linked_directory`), or `rel` itself where no
    directory of it is such a link. None where such a link leads to no directory of the
    repository, to its root, into a `.git` directory or to a directory that contains the link.

    Where a tool directory is a link, the collector copies the directory that the link leads to at
    that directory's own path (`collect_symlink_targets`), and a step that reads a file of the tool
    directory by its path, such as the `.codex/config.toml` of a `.codex` that links to
    `config/codex`, reads it there, `config/codex/config.toml`, in the clone and in the collected
    tree alike."""
    parts = rel.split("/")
    for end in range(1, len(parts)):
        if parts[end - 1] not in TOOL_DIRECTORIES:
            continue
        head = "/".join(parts[:end])
        if not ((root / head).is_symlink() or (root / head).is_file()):
            continue
        directory = _linked_directory(root, head)
        parent = posixpath.dirname(head)
        if (not directory or in_git_directory(directory) or parent == directory
                or parent.startswith(directory + "/")):
            return None
        return at_its_own_path(root, "/".join([directory, *parts[end:]]))
    return rel


def _named_file(root: Path, source: str, candidate, local: bool, vendored_dirs) -> str | None:
    """The repository-relative file that the value `candidate` of the declaring file `source`
    names relative to the directory of `source`, or None where it names no file of the tree.

    A `declared_local:` value (`local`) names a file strictly inside that directory, with no
    parent reference, as Gemini validates the original filename. A `declared_path:` value is one
    stripped string that may lead out of the directory, as Codex reads `model_instructions_file`.
    An absolute path, a path in the home directory, a path out of the repository and a path into
    `.git` or a vendored directory name nothing. A folder on the way must remain in the clone, and
    a file symlink is copied as a marker, as for the other declared files."""
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    named = candidate if local else candidate.strip()
    if local:
        decoded = named
        if not re.search(r"%(?![0-9a-fA-F]{2})", named):
            try:
                decoded = unquote(named, errors="strict")
            except UnicodeError:
                pass
        if ("\0" in named or ntpath.isabs(named) or named.startswith("\\")
                or ".." in named or ".." in decoded):
            return None
    base = posixpath.dirname(source)
    target = posixpath.normpath(posixpath.join(base, named))
    if posixpath.isabs(named) or (not local and named.startswith("~")) or target.startswith(".."):
        return None
    if local and (target == (base or ".") or (base and not target.startswith(base + "/"))):
        return None
    if (((root / target).is_file() or (root / target).is_symlink())
            and _stays_inside(root, posixpath.dirname(target))
            and target.split("/")[0] != ".git"
            and not set(target.split("/")[:-1]) & set(vendored_dirs)):
        return target
    return None


def declared_context_files(root: Path, vendored_dirs=frozenset(),
                           heuristic_rows=None) -> list[tuple[str, dict]]:
    """Every context file named by configuration in `root`, with the row that reads that configuration.

    Returns `[(repo-relative path, heuristics row)]`, sorted, one pair per row that names the
    path. A declared path that is not in the tree, that leads out of it, or that is under a
    vendored directory names nothing, because the tool would read no file there either.
    `heuristic_rows` are the declaring rows to apply, by default the declaring rows of the
    heuristics. A declaring file in a tool directory that is a link is read in the directory
    that the link leads to (`at_its_own_path`), and its paths resolve there.
    """
    heuristic_rows = _CONTEXT_DECLARATION_HEURISTICS if heuristic_rows is None else heuristic_rows
    root = Path(root)
    files: list | None = None

    def tree() -> list:
        """Every file of the tree, listed once and only when a row requires it."""
        nonlocal files
        if files is None:
            files = sorted(p.relative_to(root).as_posix()
                           for p in list_repo_files(root, vendored_dirs))
        return files

    found = set()
    for index, heuristic_row in enumerate(heuristic_rows):
        mode, key = heuristic_row["match_mode"].split(":", 1)
        if "/" in heuristic_row["path"] or mode == "declared_globs":
            source = at_its_own_path(root, heuristic_row["path"])
            declaring = [source] if source is not None and (root / source).is_file() else []
        else:
            declaring = [f for f in tree() if f.rsplit("/", 1)[-1] == heuristic_row["path"]]
        for source in declaring:
            value = _declared_value(root, root / source, heuristic_row["content_format"], key)
            if mode == "declared_globs":
                # OpenCode globs each relative entry from its working directory, the root,
                # with node-glob's `nodir` and `dot` (packages/opencode/src/session/
                # instruction.ts and util/filesystem.ts, anomalyco/opencode da6683fe).
                for pattern in filter(None, map(glob_paths, _declared_names(value))):
                    found.update((rel, index) for rel in tree()
                                 if pattern.fullmatch(rel) and rel.split("/")[0] != ".git"
                                 and _stays_inside(root, posixpath.dirname(rel)))
                continue
            if mode in ("declared_path", "declared_local"):
                local = mode == "declared_local"
                candidates = ((value if isinstance(value, list) else [value]) if local
                              else _declared_values(root, root / source,
                                                    heuristic_row["content_format"], key))
                for candidate in candidates:
                    target = _named_file(root, source, candidate, local, vendored_dirs)
                    if target is not None:
                        found.add((target, index))
                continue
            names = _declared_names(value)
            if not names:
                continue
            if mode == "declared_names":
                found.update((rel, index) for rel in tree()
                             if any(_named_at(name, rel) is not None for name in names))
                continue
            # declared_fallback: per directory, the first name there, where no shared
            # context file is. Codex skips an entry that is not a filename, "ignoring
            # project_doc_fallback_filenames entry that is not a filename"
            # (codex-rs/core/src/agents_md.rs, openai/codex 6ae8b8aa7c60).
            names = [name for name in names
                     if "/" not in name and "\0" not in name and name not in (".", "..")]
            present = set(tree())
            by_dir = defaultdict(dict)
            for rel in tree():
                for name in names:
                    directory = _named_at(name, rel)
                    if directory is not None:
                        by_dir[directory][name] = rel
            for directory, named in by_dir.items():
                if any(posixpath.join(directory, shared) in present
                       for shared in _CODEX_CONTEXT_NAMES):
                    continue
                first = next(name for name in names if name in named)
                found.add((named[first], index))
    return [(rel, heuristic_rows[index]) for rel, index in sorted(found)]


# ---------------------------------------------------------------------------
# Subagent files that configuration names
# ---------------------------------------------------------------------------
# A Codex agent role is a TOML config layer: a TOML file below `.codex/agents`, which Codex
# discovers in `config_folder.join("agents")`, or the file that the `config_file` of a table
# under `agents` in the project's `.codex/config.toml` names, relative to its `.codex` folder
# (`load_agent_roles` and `agents_toml_from_layer` in codex-rs/core/src/config/agent_roles.rs of
# openai/codex rust-v0.149.0). Up to rust-v0.148.0, Codex read the role's
# `model_instructions_file` as the base instructions of the subagent: `role.rs` reloaded the
# config with the role layer, `preserve_current_base_instructions =
# role_layer_toml.get("instructions").is_none() && role_layer_toml.get("model_instructions_file")
# .is_none()`, after `resolve_relative_paths_in_config_toml(role_config_toml,
# role_config_base,)` with the folder of the role file as the base
# (codex-rs/core/src/agent/role.rs of openai/codex rust-v0.148.0). rust-v0.149.0 applies only the
# overrides that its `AgentRoleOverrides` lists, without the key (#39299). The dataset counts the
# retired format, as it counts other deprecated configuration, and every step calls
# `declared_subagent_files` to find what configuration names. The instructions are a part of
# the role, as the prompt file of an OpenCode agent is a part of the agent
# (`opencode_prompt_files`): the cloning step copies them, the collected file cleanup keeps them,
# and in the detection step they are neither a row of their own nor an `other` row.
_SUBAGENT_DECLARATION_HEURISTICS = declaration_heuristics("subagents", _PATTERNS)
# The paths that a row of the heuristics reads as a directory, such as `.codex/agents` with its
# TOML files. A declaring row at such a path reads the role files of the directory.
_DIRECTORY_PATHS = frozenset(r["path"] for r in _PATTERNS if is_directory_mode(r["match_mode"]))


def declared_subagent_files(root: Path, vendored_dirs=frozenset(),
                            heuristic_rows=None) -> list[tuple[str, dict]]:
    """Every subagent file that configuration in `root` names, and every part of a subagent that
    it names, with the row that reads the configuration.

    Returns `[(repo-relative path, heuristics row)]`, sorted, for each `declared_path:` row of
    the subagents. A row with a file at its `path` names the files under its key, relative to the
    folder of the file, such as each role file that `agents.*.config_file` of Codex's
    `.codex/config.toml` names. A row with a directory at its `path`, a path that a row of the
    heuristics reads as a directory (`_DIRECTORY_PATHS`), names the file under its key in each
    role file of the directory, relative to the folder of the role file, such as the
    `model_instructions_file` of a Codex role, a part of the role. The role files of a directory
    are the TOML files below it at any depth, as `collect_agent_role_files` of Codex finds them,
    and the files that the rows of the same tool with a file at their `path` name, also where the
    directory is absent. A tool directory that is a link is read in the directory that the link
    leads to (`at_its_own_path`), and the paths resolve as those of `declared_context_files` do
    (`_named_file`)."""
    heuristic_rows = _SUBAGENT_DECLARATION_HEURISTICS if heuristic_rows is None else heuristic_rows
    root = Path(root)
    places = []
    for index, heuristic_row in enumerate(heuristic_rows):
        mode, key = heuristic_row["match_mode"].split(":", 1)
        if mode == "declared_path":
            places.append((index, heuristic_row, key, at_its_own_path(root, heuristic_row["path"])))
    found = set()
    named_by_tool = defaultdict(set)
    for index, heuristic_row, key, own in places:
        if heuristic_row["path"] in _DIRECTORY_PATHS or own is None or not (root / own).is_file():
            continue
        for value in _declared_values(root, root / own, heuristic_row["content_format"], key):
            target = _named_file(root, own, value, False, vendored_dirs)
            if target is not None:
                found.add((target, index))
                named_by_tool[heuristic_row["tool"]].add(target)
    for index, heuristic_row, key, own in places:
        if heuristic_row["path"] not in _DIRECTORY_PATHS:
            continue
        roles = ({p.relative_to(root).as_posix() for p in (root / own).rglob("*.toml")
                  if p.is_file() and not set(p.relative_to(root).parts[:-1]) & set(vendored_dirs)}
                 if own is not None and (root / own).is_dir() else set())
        for role in sorted(roles | named_by_tool[heuristic_row["tool"]]):
            value = _declared_value(root, root / role, heuristic_row["content_format"], key)
            target = _named_file(root, role, value, False, vendored_dirs)
            if target is not None:
                found.add((target, index))
    return [(rel, heuristic_rows[index]) for rel, index in sorted(found)]


# ---------------------------------------------------------------------------
# The items that Antigravity's registration files select
# ---------------------------------------------------------------------------
# Antigravity scans only the items directly in its default places, such as the Markdown files
# directly in .agents/rules, and reads the rest from what its registration files register:
# rules.json "to share rules stored outside .agents/rules/ or to include nested subdirectories
# of rules", and skills.json, agents.json, workflows.json and plugins.json with the same schema.
# Which files a registration selects depends on the content of the file, and every step
# therefore calls these functions, as it calls `declared_context_files`. The section
# "Configuration artifact detection" of docs/heuristics.md records what Antigravity CLI 1.4.2 did
# with each form that these functions read.
_RULES_JSON_ROWS = rules_json_rows(_PATTERNS)
_REGISTRATION_ROWS = [r for r in _PATTERNS
                      if r["match_mode"] == "declared_rules"
                      or r["match_mode"].startswith("registered:")]
_REGISTRATION_NAMES = {posixpath.basename(r["path"]) for r in _REGISTRATION_ROWS}
# Antigravity's workspace customization roots, such as `.agents`.
_ANTIGRAVITY_ROOTS = tuple(dict.fromkeys(parent.split("/")[0]
                                         for parent in PLUGIN_PARENTS["<antigravity-plugin>"]))
# The mechanisms with Markdown files as items. A skill is a folder, and a plugin a directory.
_MARKDOWN_ITEMS = ("rules", "commands", "subagents")
# The default places of Antigravity's items, which a registration file's top-level `exclude`
# applies to as well: the directory rows of its customization roots and of its plugins.
# `.agents/skills` is not one of them, because other tools read it too, and a skill there stays
# a configuration artifact of theirs whatever Antigravity leaves out.
_ANTIGRAVITY_PLACES = [r for r in _PATTERNS
                       if r["tool"] == "gemini" and is_directory_mode(r["match_mode"])
                       and (r["path"].split("/")[0] in _ANTIGRAVITY_ROOTS
                            or plugin_placeholder(r["path"]) == "<antigravity-plugin>")]


def _json_object(root: Path, path: Path, content_format: str) -> dict:
    """The object that a JSON file of `root` contains, or an empty dict where the file is not
    read (`_repository_text`) or does not parse to an object."""
    text = _repository_text(root, path)
    if text is None:
        return {}
    try:
        data = jsonc.loads(text, content_format)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


# The keys of a registration file and of each of its path entries that Antigravity reads, with
# the JSON type of each value.
_REGISTRATION_KEYS = {"entries": "path entries", "inherits": "path entries", "exclude": "names"}
_PATH_ENTRY_KEYS = {"path": "string", "include_only": "names", "exclude": "names"}


def _decoded(value: dict, keys: dict) -> dict:
    """The values of `keys` that the object `value` sets. Raises ValueError where one of them
    has a value of another type.

    Antigravity CLI 1.4.2 decoded a registration file as Go's encoding/json decodes a struct (the
    section "Configuration artifact detection" of docs/heuristics.md): a name matches a key in any
    case, a null is an empty value, a name that is no key is ignored, and a value of another type,
    such as a number for a `path`, fails the whole file.
    """
    found = {}
    for name, item in value.items():
        key = name.casefold()
        kind = keys.get(key)
        if kind is None:
            continue
        if item is None:
            found[key] = "" if kind == "string" else []
        elif kind == "string" and isinstance(item, str):
            found[key] = item
        elif kind == "names" and isinstance(item, list) \
                and all(v is None or isinstance(v, str) for v in item):
            found[key] = [v for v in item if v is not None]
        elif kind == "path entries" and isinstance(item, list) \
                and all(v is None or isinstance(v, dict) for v in item):
            found[key] = [_decoded(v or {}, _PATH_ENTRY_KEYS) for v in item]
        else:
            raise ValueError(f"{name} is not a value of {kind}")
    return found


def _registration_document(root: Path, rel: str, content_format: str) -> dict | None:
    """What Antigravity reads from the registration file at `rel`: its `entries`, `inherits`
    and top-level `exclude`, or None where it reads nothing from the file.

    An empty dict is a file that it reads and that sets none of the keys, such as a
    package.json. CLI 1.4.2 read a `.json` file in the format of its row, with comments and
    trailing commas, and failed one that does not decode (`_decoded`). It read an inherited
    `.txt` file as path entries, one per line, without blank lines and lines that start with
    `#`, and reported any other file type as unsupported.
    """
    if rel.endswith(".txt"):
        text = _repository_text(root, root / rel, errors="replace")
        if text is None:
            return None
        lines = [line.strip() for line in text.split("\n")]
        entries = [{"path": line} for line in lines if line and not line.startswith("#")]
        return {"entries": entries} if entries else {}
    if not rel.endswith(".json"):
        return None
    text = _repository_text(root, root / rel)
    if text is None:
        return None
    try:
        data = jsonc.loads(text, content_format)
        if data is None:
            return {}
        return _decoded(data, _REGISTRATION_KEYS) if isinstance(data, dict) else None
    except (ValueError, RecursionError):
        return None


def _inside(root_relative: str) -> bool:
    """Whether a normalized path stays inside the repository."""
    return not (root_relative.startswith("../") or root_relative in ("..", "")
                or posixpath.isabs(root_relative))


def _a_list(value) -> list:
    """`value` where it is a list, else an empty list. A key that should contain a list and
    contains a number or a boolean names nothing, as a file that does not parse names nothing."""
    return value if isinstance(value, list) else []


def _item_names(value) -> set[str]:
    """The names that an `include_only` or `exclude` list of a registration file gives, each
    without a trailing `/`, or none.

    A name is compared as it is written, neither as a regular expression nor as a glob, and is not
    stripped of spaces. Antigravity CLI 1.4.2 reads it this way (the section "Configuration artifact
    detection" of docs/heuristics.md), and a regular expression from a repository, matched with
    backtracking, can take time exponential in the length of a directory name.
    """
    return {v.rstrip("/") for v in value if isinstance(v, str) and v.rstrip("/")} \
        if isinstance(value, list) else set()


def _item_forms(path: str) -> set[str]:
    """`path`, and for a Markdown file also `path` without `.md`, the forms in which a
    registration names an item."""
    return {path, path.removesuffix(".md")} if path.endswith(".md") else {path}


def _names_item(names: set[str], item: str) -> bool:
    """Whether one of `names` is the end of the item's path in whole segments, with or without
    `.md`: `deep`, `cat/deep` and `shared/cat/deep` each name `shared/cat/deep`.

    Antigravity CLI 1.4.2 compared every `exclude` value this way, as well as an `include_only`
    value of an `inherits` object and an `include_only` value of an entry that names the item
    itself.
    """
    for form in _item_forms(item):
        parts = form.split("/")
        if any("/".join(parts[i:]) in names for i in range(len(parts))):
            return True
    return False


# ---------------------------------------------------------------------------
# The front matter that a tool requires of a Markdown item
# ---------------------------------------------------------------------------
# The keys are read by lines. Where a tool's loader drops an item with a front matter that does
# not parse, the test also parses it as YAML (`yaml_parses`), as for a rule of Antigravity
# (`has_trigger`) and a skill of Cursor (`detection._cursor_skill_loads`). The collector reads a
# registered item through these functions in the clone, and the collected file cleanup and the
# detection step in the trees that they read, so that the three select the same items.

# YAML 1.2's core schema: a plain scalar of one of these forms is not a string.
_YAML_NOT_A_STRING = re.compile(
    r"^(?:~|null|Null|NULL|true|True|TRUE|false|False|FALSE|[-+]?[0-9]+|0o[0-7]+|0x[0-9a-fA-F]+"
    r"|[-+]?(?:\.[0-9]+|[0-9]+\.[0-9]*|[0-9]+)(?:[eE][-+]?[0-9]+)?"
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$")


def _item_front_matter_lines(text: str) -> list | None:
    """The lines between the first line of `text`, after a byte order mark, and the next line,
    where both are `---` with optional white space after it, or None."""
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == "---"), None)
    return None if end is None else lines[1:end]


def front_matter(text: str) -> tuple[list, int] | None:
    """(lines, indent) of the YAML front matter that `text` opens with, after a byte order mark,
    without blank and comment lines, or None when the first line is not `---`, the front matter
    is not closed or it is empty."""
    lines = _item_front_matter_lines(text)
    if lines is None:
        return None
    front = [line for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not front:
        return None
    return front, min(len(line) - len(line.lstrip()) for line in front)


@functools.cache
def _yaml_loader() -> tuple:
    """(yaml, loader): the module of PyYAML and its safe loader, extended so that it refuses a
    key that its mapping repeats and reads a timestamp as its text (`load_yaml`). The function
    imports PyYAML when a test first parses a front matter, so that the modules that import
    `paths` and parse no YAML, such as the runner and the commit classification, import with the
    standard library alone."""
    import yaml

    class UniqueKeyLoader(yaml.SafeLoader):
        """PyYAML's safe loader, which also refuses a key that its mapping repeats and reads a
        timestamp as its text."""

        def construct_mapping(self, node, deep=False):
            """The mapping of `node`, or a ConstructorError where two of its own keys are equal.
            A key that a merge key `<<` brings in can repeat one of them, as YAML's merge
            allows."""
            if isinstance(node, yaml.MappingNode):
                seen = set()
                for key_node, _value_node in node.value:
                    if key_node.tag == "tag:yaml.org,2002:merge":
                        continue
                    key = self.construct_object(key_node, deep=True)
                    try:
                        repeated = key in seen
                        seen.add(key)
                    except TypeError:
                        continue  # an unhashable key, which the base class refuses
                    if repeated:
                        raise yaml.constructor.ConstructorError(
                            "while constructing a mapping", node.start_mark,
                            f"found the key {key!r} twice", key_node.start_mark)
            return super().construct_mapping(node, deep=deep)

    # A timestamp that is no date, such as 2025-02-30, is valid YAML, and PyYAML's constructor
    # would raise ValueError for it.
    UniqueKeyLoader.add_constructor("tag:yaml.org,2002:timestamp",
                                    yaml.SafeLoader.construct_yaml_str)
    return yaml, UniqueKeyLoader


def load_yaml(text: str) -> tuple[bool, object]:
    """(True, value) where `text` is one YAML document that PyYAML's safe loader reads without a
    key twice in one mapping, and (False, None) otherwise.

    go-yaml v3 and js-yaml 3 refuse a key twice as well, but they compare keys in other ways:
    go-yaml v3 compares the kind and the text of two keys and js-yaml 3 their strings, where this
    function compares the constructed keys. Both tools refuse `1: a` with `"1": b`, which this
    function reads as {1: 'a', '1': 'b'}, and go-yaml v3 reads `true: a` with `True: b`, which
    this function refuses. Antigravity decodes a rule into a struct, and go-yaml v3 does not test
    the keys of a mapping under a key for which the struct has no field, such as `metadata`, where
    this function refuses a key twice as well. The section "The YAML parsers of the tools" of
    docs/heuristics.md names these and the other forms on which the parsers differ."""
    yaml, loader = _yaml_loader()
    try:
        return True, yaml.load(text, Loader=loader)
    except (yaml.YAMLError, ValueError, RecursionError):
        return False, None


def yaml_parses(text: str) -> bool:
    """Whether `text` is one YAML document that PyYAML's safe loader reads (`load_yaml`)."""
    return load_yaml(text)[0]


def top_level_keys(front: list, indent: int, key: str) -> list:
    """[(index, value)] of the front matter's lines that set `key` at the top level, where
    `value` is what follows the colon and the white space after it, without trailing white
    space, and "" where nothing follows.

    The value is matched greedily and stripped afterwards. A lazy value followed by optional
    white space and the end of the line takes time quadratic in a run of white space inside
    the value, and the collector and the detection step read the front matter of every file in
    a rules or agents folder, whatever its size.
    """
    pattern = re.compile(r"""^\s*["']?""" + re.escape(key) + r"""["']?\s*:(?:\s+(.*))?$""")
    keys = [(i, pattern.match(line))
            for i, line in enumerate(front) if len(line) - len(line.lstrip()) == indent]
    return [(i, (m.group(1) or "").rstrip()) for i, m in keys if m]


def has_string(text: str, key: str) -> bool:
    """Whether `text` opens with a YAML front matter that sets `key` once, at the top level, to
    a non-empty string."""
    found = front_matter(text)
    if found is None:
        return False
    front, indent = found
    keys = top_level_keys(front, indent, key)
    if len(keys) != 1:
        return False  # no key fails "Required", and two are a duplicate key, which does not parse
    i, value = keys[0]
    value = value.strip()
    below = []  # the value's own lines, indented deeper than the key
    for line in front[i + 1:]:
        if len(line) - len(line.lstrip()) <= indent:
            break
        below.append(line)
    if value[:1] in ("'", '"'):
        quoted = "\n".join([value, *below])
        quote = quoted[0]
        index = 1
        while index < len(quoted):
            char = quoted[index]
            if quote == '"' and char == "\\":
                index += 2
                continue
            if char == quote:
                if quote == "'" and quoted[index + 1:index + 2] == "'":
                    index += 2
                    continue
                tail = quoted[index + 1:]
                return index > 1 and (not tail.strip() or bool(re.fullmatch(r"\s+#.*", tail)))
            index += 1
        return False
    value = re.sub(r"(^|\s)#.*$", "", value).strip()
    if value[:1] in ("|", ">"):
        return bool(below)
    if not value:
        first = below[0].strip() if below else ""
        return bool(first) and not first.startswith("-") and not re.match(r"^[^\s:]+:(\s|$)", first)
    return value[:1] not in ("[", "{") and not _YAML_NOT_A_STRING.match(value)


# GROUNDING: Antigravity's rules page, read 2026-10-13: "Every .md file inside rules/ must start
# with YAML frontmatter declaring a valid trigger", the values "always_on | model_decision | glob
# | manual", and "If a file in .agents/rules/*.md omits frontmatter or specifies an unrecognized
# trigger value (such as camelCase alwaysOn or modelDecision), Antigravity silently discards the
# rule." https://antigravity.google/docs/rules
# Antigravity CLI 1.4.2, run offline on 2026-10-19, counted no rule without such a trigger in a
# plugin's `rules/` or in what a plugin's rules.json registers, and `antigravity plugin
# validate` reported "no valid trigger in the frontmatter, so the rule never applies" for each.
RULE_TRIGGERS = ("always_on", "model_decision", "glob", "manual")

# GROUNDING: Antigravity CLI 1.4.10 (linux_arm64,
# https://storage.googleapis.com/antigravity-public/antigravity-cli/1.4.10-5501573669978112/linux-arm/cli_linux_arm64.tar.gz),
# read 2026-11-07. getRules, processPluginRules and (*ruleCollector).tryAdd of the package
# customizations read every rule through (*Manager).parseRuleFile, which calls parseRuleInfo.
# parseRuleInfo applies strings.TrimSpace to the block that SplitMarkdownFrontmatter splits off,
# replaces `(?m)^(\s*globs?\s*:\s*)([*][^\n]*)` with `${1}"${2}"`, applies
# workflowparse.NormalizeFrontmatterDescription and decodes the result with go-yaml v3
# (google3/third_party/golang/yamlv3) into a struct with the type descriptor `Trigger string
# yaml:"trigger"`, `Description string yaml:"description"`, `Globs string yaml:"globs"` and
# `Glob string yaml:"glob"`. On an error, also for a list or a mapping under one of these keys,
# it returns "failed to parse frontmatter yaml", and getRules logs "Failed to parse rule file %s:
# %v" and reads the next file.
# NormalizeFrontmatterDescription returns a front matter that go-yaml v3 decodes as it is.
# Otherwise it replaces each line that matches
# `(?m)^([ \t]*description[ \t]*:[ \t]*)([^"'>|\-[{\s\n][^\n\r]*(?::[ \t][^\n\r]*|:))\r?$` and
# does not decode alone with the first group and strconv.Quote of the second group without the
# white space around it. The binary's changelog for 1.3.6 reads "Fixed skills, rules, workflows,
# and Markdown agents failing to load with `mapping values are not allowed in this context` when
# an unquoted `description:` contains a colon". The rules page requests the quotes that the first
# replacement adds: "Wrap patterns starting with * in quotes so YAML does not treat * as an alias
# anchor." (https://antigravity.google/docs/rules, read 2026-11-07)
# What `\s` matches in Go's regexp.
_GO_SPACE = "\t\n\f\r "
# A `globs` or `glob` key after white space or at the start of the text, up to a value that
# starts with `*`.
_ANTIGRAVITY_GLOB_KEY = re.compile(r"(?<![^\t\n\f\r ])globs?[\t\n\f\r ]*:[\t\n\f\r ]*(?=\*)")
_ANTIGRAVITY_DESCRIPTION = re.compile(
    r"""(?m)^([ \t]*description[ \t]*:[ \t]*)([^"'>|\-\[{\t\n\f\r ][^\n\r]*(?::[ \t][^\n\r]*|:))\r?$""")
# The keys of the struct that Antigravity decodes a rule's front matter into, each a string.
_ANTIGRAVITY_RULE_KEYS = ("trigger", "description", "globs", "glob")


def _quoted_globs(block: str) -> str:
    """`block` with each value of a `globs` or `glob` key that starts with `*` in double quotes up
    to the end of its line, as the first replacement of Antigravity's parseRuleInfo quotes it.
    Only white space, line breaks included, separates such a key from the start of a line. The
    function finds the keys first, which takes time linear in the block, where Antigravity's
    pattern, tried at each line of a run of blank lines, would read the rest of the run each
    time."""
    out, done = [], 0
    for match in _ANTIGRAVITY_GLOB_KEY.finditer(block):
        start = match.start()
        if start < done:
            continue
        while start > done and block[start - 1] in _GO_SPACE:
            start -= 1
        if start > 0 and "\n" not in block[start:match.start()]:
            continue
        end = block.find("\n", match.end())
        end = len(block) if end == -1 else end
        out.append(f'{block[done:match.end()]}"{block[match.end():end]}"')
        done = end
    return "".join(out) + block[done:]


def _quoted_descriptions(block: str) -> str:
    """`block` with the `description` values that Antigravity's NormalizeFrontmatterDescription
    quotes in a front matter that does not parse: a value that holds `: ` or ends with `:` on a
    line that does not parse alone. The quotes are a JSON string, which YAML reads as
    strconv.Quote's string, with the one character outside YAML's printable set that JSON leaves
    as it is escaped."""
    if not _ANTIGRAVITY_DESCRIPTION.search(block) or yaml_parses(block):
        return block

    def quoted(match):
        if yaml_parses(match.group(0)):
            return match.group(0)
        return match.group(1) + json.dumps(match.group(2).strip()).replace("\x7f", "\\x7f")

    return _ANTIGRAVITY_DESCRIPTION.sub(quoted, block)


def antigravity_rule_front_matter(block: str) -> str:
    """The front matter `block` of a rule as Antigravity decodes it: without the white space
    around it, with a `globs` value that starts with `*` quoted (`_quoted_globs`), and with a
    `description` with a colon quoted where the front matter does not parse
    (`_quoted_descriptions`)."""
    return _quoted_descriptions(_quoted_globs(block.strip()))


def _decodes_as_a_rule(block: str) -> bool:
    """Whether the front matter `block`, as Antigravity has quoted it, decodes as its rules do:
    a mapping that parses as YAML, with no list or mapping under one of the keys of
    `_ANTIGRAVITY_RULE_KEYS`, which take a string, and other keys of any value."""
    parsed, value = load_yaml(block)
    return parsed and isinstance(value, dict) and not any(
        isinstance(value.get(key), (dict, list, set)) for key in _ANTIGRAVITY_RULE_KEYS)


def has_trigger(text: str) -> bool:
    """Whether a rule's YAML front matter sets `trigger` to one of `RULE_TRIGGERS`, once, and
    decodes as Antigravity decodes a rule once it has quoted its values
    (`antigravity_rule_front_matter`, `_decodes_as_a_rule`), which Antigravity requires of every
    rule that it reads."""
    found = front_matter(text)
    if found is None:
        return False
    keys = top_level_keys(*found, "trigger")
    if len(keys) != 1:
        return False
    value = re.sub(r"(^|\s)#.*$", "", keys[0][1]).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value in RULE_TRIGGERS and _decodes_as_a_rule(
        antigravity_rule_front_matter("\n".join(_item_front_matter_lines(text))))


# GROUNDING: Antigravity CLI 1.3.5 (linux_amd64, from the installer's manifest on 2026-10-14),
# run offline over a trusted workspace: `antigravity agents` listed the agents with a non-empty
# `name` and `description` in their front matter and left out one without front matter, one with
# `name` alone, one with `description` alone, one with an empty description and one with front
# matter that is not YAML. The binary's message for them is "Markdown agent at %s is missing name or
# description in frontmatter", beside "Failed to parse markdown agent from %s: %v". The
# subagents page marks both keys "(Required)": https://antigravity.google/docs/subagents
# Antigravity CLI 1.4.2, run offline on 2026-10-19, listed no registered agent and counted no
# agent of a plugin without them, and `antigravity plugin validate` reported "the frontmatter has
# no name or description. The loader skips this agent".
def has_name_and_description(text: str) -> bool:
    """Whether an Antigravity agent's YAML front matter has a non-empty `name` and a non-empty
    `description`, without which Antigravity does not load the agent."""
    return has_string(text, "name") and has_string(text, "description")


# The test that Antigravity applies to each Markdown item of a mechanism wherever it reads the
# item: in a default place, in a plugin and where a registration file selects it.
_ITEM_TESTS = {"rules": has_trigger, "subagents": has_name_and_description}


# The folders that can contain an Antigravity plugin's manifest in place of the plugin's root.
_ANTIGRAVITY_MANIFEST_FOLDERS = tuple(manifest.rsplit("/", 1)[0]
                                      for manifest in PLUGIN_ROOTS["<antigravity-plugin>"]
                                      if "/" in manifest)


def antigravity_plugin_dir(manifest: str) -> str:
    """The directory of the Antigravity plugin with its manifest at `manifest`."""
    folder = posixpath.dirname(manifest)
    if posixpath.basename(folder) in _ANTIGRAVITY_MANIFEST_FOLDERS:
        return posixpath.dirname(folder)
    return folder


def _tree_index(root: Path, vendored_dirs) -> tuple[list[str], set[str], set[str], dict]:
    """The files of the tree, sorted, the same as a set, the directories that contain them, and
    {link: directory} for each Antigravity customization root that is a symlink to a directory
    of the repository, or the collection's marker of one (`_linked_directory`).

    The files are the files that `list_repo_files` lists. One `os.walk` lists them together with
    the linked roots, because it leaves a symlinked directory in `dirnames` without descending
    into it. A marker is a file of the tree.
    """
    files, linked_roots = [], []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != ".git" and not is_pruned(Path(dirpath) / d, vendored_dirs, root)]
        base = Path(dirpath)
        here = _named_from(root, base)
        files.extend(base / name for name in filenames
                     if local_state(f"{here}/{name}") is None)
        linked_roots.extend(base / name for name in dirnames + filenames
                            if name in _ANTIGRAVITY_ROOTS
                            and ((base / name).is_symlink() or name in filenames))
    tree = sorted(p.relative_to(root).as_posix() for p in files)
    directories = set()
    for rel in tree:
        parent = posixpath.dirname(rel)
        while parent and parent not in directories:
            directories.add(parent)
            parent = posixpath.dirname(parent)
    linked = {}
    for path in sorted(linked_roots):
        rel = path.relative_to(root).as_posix()
        target = _linked_directory(root, rel)
        if target is not None:
            linked[rel] = target
    return tree, set(tree), directories, linked


# The size up to which a file can be the collection's marker of a symlink, as the detection step
# and the collected file cleanup read markers.
MARKER_MAX_BYTES = 4096


def _linked_directory(root: Path, rel: str) -> str | None:
    """The directory of the repository, repository-relative and `""` for its root, that the
    symlink at `rel` leads to, or that the collection's marker of one names, following a chain
    of either. None where it leads out of the repository or to no directory.

    The cloning step stores a symlink as a marker, a one-line file that contains the link's
    target (`copy_file`), and the collected file cleanup and the detection step read the marker
    to follow a linked customization root where the collector followed the symlink in the clone.
    """
    real_root = root.resolve()
    path = root / rel
    for _ in range(8):
        try:
            linked = path.is_symlink()
            real = path.resolve()
            if not real.is_relative_to(real_root):
                return None
            if real.is_dir():
                inner = real.relative_to(real_root).as_posix()
                return "" if inner == "." else inner
            status = real.stat()
            if linked or not stat.S_ISREG(status.st_mode) or status.st_size > MARKER_MAX_BYTES:
                return None
            text = marker_target(real.read_text(encoding="utf-8"), real.parent)
        except (OSError, RuntimeError, UnicodeDecodeError):
            return None
        if text is None:
            return None
        path = real.parent / text
    return None


def _resolved(named, base: str, present: set[str], directories: set[str], *,
              markdown: bool = False) -> str | None:
    """The repository path that a `path` of a registration file names, or None.

    Antigravity CLI 1.4.2's guide states: "Relative `path` entries resolve against the directory
    containing the dot-dir that declares them, falling back to the project root when nothing
    exists there." `base` is that directory for the file that starts the chain, or the root of
    the plugin with the file, and an inherited file resolves against the same `base`. Where
    nothing exists at either place, `markdown` reads the path as a Markdown file named without
    `.md` under `base`. A path that resolves to the repository root, such as `.` at the
    workspace root, is `""`, as CLI 1.4.2 listed the agents directly in the workspace for
    `.` and counted the root skill of a plugin at the workspace root.
    """
    if not isinstance(named, str) or not named.strip():
        return None
    named = named.strip()
    for anchor in dict.fromkeys((base, "")):
        rel = posixpath.normpath(posixpath.join(anchor, named))
        if rel == ".":
            return ""
        if _inside(rel) and (rel in present or rel in directories):
            return rel
    rel = posixpath.normpath(posixpath.join(base, named)) + ".md"
    return rel if markdown and _inside(rel) and rel in present else None


def _registration_candidates(tree: list[str], mechanism: str) -> list[tuple[str, str]]:
    """(file, item) for each file of the tree that makes an item of `mechanism`: a SKILL.md
    with its folder, a plugin manifest with the plugin's directory, a subagent's agent.md in any
    letter case (`heuristics.is_folder_agent_file`) with its folder, and any other Markdown file
    of a rule, a workflow or a subagent with itself."""
    candidates = []
    for rel in tree:
        name = posixpath.basename(rel)
        if mechanism == "skills" and name.lower() == _SKILL_MARKER.lower():
            candidates.append((rel, posixpath.dirname(rel)))
        elif mechanism == "plugins" and name == "plugin.json":
            candidates.append((rel, antigravity_plugin_dir(rel)))
        elif mechanism in _MARKDOWN_ITEMS and name.endswith(".md"):
            item = (posixpath.dirname(rel) if mechanism == "subagents" and is_folder_agent_file(name)
                    else rel)
            candidates.append((rel, item))
    return candidates


def _entry_items(resolved: str, entry: dict, mechanism: str, candidates: list,
                 present: set[str], loads=None) -> list[tuple[str, str]]:
    """The (file, item) pairs that one registration entry selects at its resolved path.

    A file is the item that it is or that it makes, such as a skill's SKILL.md, and a directory
    that is a skill or a plugin is that item. Antigravity CLI 1.4.2 loaded each such item, and
    `include_only` and `exclude` name it as `_names_item` reads them. Any other directory gives
    the items directly in it, or with `include_only` the items that it names by their path
    below the directory, nested ones among them, and `exclude` leaves out the items that it
    names. A directory with an agent.md gives its Markdown files, agent.md among them, and no
    folder with an agent.md below it, which the CLI listed the same way. `""` is the
    repository root. `loads`, where it is given, is the test that a file has to pass, such as
    the front matter of a rule or an agent (`_ITEM_TESTS`). The CLI listed and counted no
    registered rule or agent without it, and `.` at the root selects no README.md.
    """
    return [(rel, item) for rel, item in _entry_selection(resolved, entry, mechanism, candidates,
                                                          present)
            if loads is None or loads(rel)]


def _entry_selection(resolved: str, entry: dict, mechanism: str, candidates: list,
                     present: set[str]) -> list[tuple[str, str]]:
    """The (file, item) pairs that one registration entry names at its resolved path, before
    the test of `_entry_items`."""
    only, exclude = _item_names(entry.get("include_only")), _item_names(entry.get("exclude"))
    markdown = mechanism in _MARKDOWN_ITEMS
    if resolved in present or not markdown:
        itself = [(rel, rel if markdown else item) for rel, item in candidates
                  if (rel if resolved in present else item) == resolved]
        if itself or resolved in present:
            return [(rel, item) for rel, item in itself
                    if (not only or _names_item(only, item)) and not _names_item(exclude, item)]
    # A subagents directory with an agent.md, a folder agent when its parent is scanned.
    agent_folder = any(item == resolved for _rel, item in candidates)
    prefix = f"{resolved}/" if resolved else ""
    selected = []
    for rel, item in candidates:
        if item == resolved:
            item = rel
        if not item.startswith(prefix):
            continue
        inner = item[len(prefix):]
        # `include_only` names an item by its path below the directory, or by the path of the
        # file that makes it one, such as its SKILL.md.
        if only:
            if not only & (_item_forms(inner) | {rel[len(prefix):]}):
                continue
        elif "/" in inner or (agent_folder and item != rel):
            continue
        if not _names_item(exclude, item):
            selected.append((rel, item))
    return selected


def _registration_sources(root: Path, vendored_dirs, heuristic_row: dict, tree: list[str],
                          present: set[str], linked: dict) -> list[tuple[str, str, tuple, str]]:
    """(file, base, scope, link) for each registration file of the row: `base` is where its
    paths resolve, the directory that contains its customization root or the root of its
    plugin, `scope` is ("workspace", base) or ("plugin", base), the items that its top-level
    `exclude` applies to (`_excluded_here`), and `link` is the linked customization root through
    which the file is read, or None.

    Antigravity CLI 1.4.2 read the registration files and the default places of an `.agents`
    that links to `config/agents`, and resolved the paths of `config/agents/agents.json` against
    the directory that contains the link, as for an `.agents` directory there. Such a file is
    reported where it is, at its path in the repository (`linked`, from `_tree_index`).
    """
    token = plugin_placeholder(heuristic_row["path"])
    if token:
        tail = heuristic_row["path"].split("/", 1)[1]
        sources = ((f"{base}/{tail}" if base else tail, base)
                   for base in plugin_roots(root, vendored_dirs)[token])
        return [(rel, base, ("plugin", base), None) for rel, base in sources if rel in present]
    path = heuristic_row["path"]
    found = [(rel, rel[:-len(path)].rstrip("/"), None)
             for rel in tree if rel == path or rel.endswith("/" + path)]
    head, tail = path.split("/", 1)
    for link, directory in linked.items():
        rel = f"{directory}/{tail}" if directory else tail
        if posixpath.basename(link) == head and rel in present:
            found.append((rel, posixpath.dirname(link), link))
    return [(rel, base, ("workspace", base), link) for rel, base, link in found]


def _registrations(root: Path, vendored_dirs, heuristic_rows: list,
                   index_of_tree=None) -> tuple[list, set, dict, set]:
    """What the registration files of the rows select, before any top-level `exclude`.

    Returns (selected, read, excludes, links): (file, item, row index, scope, root skill) for
    each selected item, (file, row index) for each registration file read, the names of the
    top-level `exclude` lists by (mechanism, scope), and (link, row index) for each linked
    customization root through which a registration file is read (`_registration_sources`).
    Each chain resolves its paths against the `base` of the file that starts it (`_resolved`),
    reads an inherited file once, the first time that it is named, and applies the
    `include_only` and `exclude` of every `inherits` object above an item to that item, as CLI
    1.4.2 did, and not to the entries of the file with the `inherits` object. A rule or an
    agent that an entry selects counts only with the front matter that Antigravity requires of
    it (`_ITEM_TESTS`).

    The file that starts a chain is a configuration artifact by its path. An inherited file is
    one only where Antigravity reads an entry, an inherited file or an exclusion from it
    (`_registration_document`). CLI 1.4.2 read nothing from a package.json that an `inherits`
    object names, and a file that does not decode ended neither the chain nor the session.

    A skill is a root skill where its folder contains the file that starts its chain or the
    file with the entry that selects it, such as a SKILL.md at the repository root that
    `.agents/skills.json` names. Such a folder is a whole repository or package, and the skill
    is its SKILL.md with `SKILL_SUPPORT_DIRS` (`registered_root_skills`), as a
    plugin's root skill is (`plugin_components`).
    """
    root = Path(root)
    tree, present, directories, linked = index_of_tree or _tree_index(root, vendored_dirs)
    real_root = root.resolve()
    selected, read_files, links = [], set(), set()
    excludes = defaultdict(set)

    def of_content(test):
        """`test` applied to the content of a selected file, read inside the repository only."""
        return lambda rel: test(_repository_text(root, root / rel, errors="replace") or "")

    for index, heuristic_row in enumerate(heuristic_rows):
        mechanism = heuristic_row["mechanism"]
        test = _ITEM_TESTS.get(mechanism)
        item_test = of_content(test) if test else None
        candidates = None
        for source, base, scope, link in _registration_sources(root, vendored_dirs, heuristic_row,
                                                               tree, present, linked):
            if candidates is None:
                candidates = _registration_candidates(tree, mechanism)
            if link is not None:
                links.add((link, index))
            read = set()
            # Depth first in the order of the `inherits` lists, without recursion, so that a
            # chain of any length is read.
            pending = [(source, ())]
            while pending:
                current, filters = pending.pop()
                if current in read:
                    continue
                read.add(current)
                data = _registration_document(root, current, heuristic_row["content_format"])
                if current == source or data:
                    read_files.add((current, index))
                if not data:
                    continue
                names = _item_names(data.get("exclude"))
                if names:
                    excludes[(mechanism, scope)] |= names
                inherited = []
                for parent in data.get("inherits", []):
                    target = _resolved(parent.get("path"), base, present, directories)
                    if target in present:
                        inherited.append((target, (*filters, (_item_names(parent.get("include_only")),
                                                              _item_names(parent.get("exclude"))))))
                pending.extend(reversed(inherited))
                for entry in data.get("entries", []):
                    resolved = _resolved(entry.get("path"), base, present, directories,
                                         markdown=True)
                    if resolved is None:
                        continue
                    for rel, item in _entry_items(resolved, entry, mechanism, candidates, present,
                                                  item_test):
                        if all((not only or _names_item(only, item)) and not _names_item(exclude, item)
                               for only, exclude in filters) \
                                and (root / rel).resolve().is_relative_to(real_root):
                            root_skill = mechanism == "skills" and (
                                not item or any(f.startswith(item + "/") for f in (source, current)))
                            selected.append((rel, item, index, scope, root_skill))
    return selected, read_files, excludes, links


def _excluded_here(excludes: dict, mechanism: str, scope: tuple) -> set[str]:
    """The top-level `exclude` names that apply to an item of `mechanism` in `scope`.

    A plugin's registration files apply to its own items only, and those of the workspace to
    no plugin's item. A workspace session reads the registration files of the customization
    roots between its working directory and the workspace root and applies the top-level
    `exclude` of each, of an inherited file as well, to every item of the session, as CLI 1.4.2
    did. An item of a customization root in a directory is left out by the files of
    that directory and of every directory above it, and a file below the directory leaves it
    out only of the sessions started below. The item therefore stays a configuration artifact.
    """
    kind, where = scope
    names = set()
    for (named_mechanism, (named_kind, named_where)), values in excludes.items():
        if named_mechanism == mechanism and named_kind == kind and (
                named_where == where if kind == "plugin"
                else not named_where or where == named_where or where.startswith(named_where + "/")):
            names |= values
    return names


def registered_configuration_files(root: Path, vendored_dirs=frozenset(),
                        heuristic_rows=None, *, include_registering_files=False) -> list[tuple[str, dict]]:
    """Files selected by Antigravity registrations, with the heuristic that selects them.

    `_registrations` reads what the registration files select, and the top-level `exclude`
    lists leave items out (`_excluded_here`). Registered skills use SKILL.md, plugins use
    plugin.json, and subagent folders use agent.md. With `include_registering_files`, the
    function also returns the registration files, inherited registration files included, which
    the cleanup keeps for detection.
    """
    heuristic_rows = _REGISTRATION_ROWS if heuristic_rows is None else heuristic_rows
    if not heuristic_rows:
        return []
    found, read, _root_skills, _links = _registered_items(root, vendored_dirs, heuristic_rows)
    if include_registering_files:
        found |= read
    return [(rel, heuristic_rows[index]) for rel, index in sorted(found)]


def _registered_items(root: Path, vendored_dirs, heuristic_rows: list) -> tuple[set, set, set, set]:
    """(selected, read, root skills, links): (file, row index) for each item that the
    registration files of the rows select and no top-level `exclude` leaves out
    (`_excluded_here`), the same for each registration file read, the SKILL.md of each selected
    root skill, and (link, row index) for each linked customization root that a registration
    file is read through (`_registrations`)."""
    selected, read, excludes, links = _registrations(root, vendored_dirs, heuristic_rows)
    found, root_skills = set(), set()
    for rel, item, index, scope, root_skill in selected:
        if not _names_item(_excluded_here(excludes, heuristic_rows[index]["mechanism"], scope),
                           item):
            found.add((rel, index))
            if root_skill:
                root_skills.add(rel)
    return found, read, root_skills, links


def _place_items(tree: list[str], place: str, match_mode: str) -> list[tuple[str, str]]:
    """(file, item) for each item that a default place with `match_mode` can read: the files
    with the mode's extension directly in it, each folder directly in it with an agent.md in any
    letter case for `dir_agent`, and each folder with a SKILL.md below it for a skill
    directory."""
    extensions = tuple(match_mode.split(":", 1)[1].split("|")) if ":" in match_mode else ()
    found = []
    for rel in tree:
        if not rel.startswith(place + "/"):
            continue
        inner, name = rel[len(place) + 1:], posixpath.basename(rel)
        if match_mode.startswith("skill_dir"):
            if name.lower() == _SKILL_MARKER.lower():
                found.append((rel, posixpath.dirname(rel)))
        elif "/" not in inner:
            if extensions and rel.endswith(extensions):
                found.append((rel, rel))
        elif (match_mode.startswith("dir_agent:") and inner.count("/") == 1
              and is_folder_agent_file(name)):
            found.append((rel, posixpath.dirname(rel)))
    return found


def antigravity_left_out(root: Path, vendored_dirs=frozenset()) -> dict[str, str]:
    """{file: its default place} for each item of an Antigravity default place that the
    top-level `exclude` of a registration file leaves out.

    CLI 1.4.2 listed no `.agents/agents/defaultone.md` under an `.agents/agents.json` with
    `"exclude": ["defaultone"]`, and no skill, rule or subagent of a plugin's default places
    that the plugin's own registration file excluded (`_excluded_here`). The detection step
    reads no such file as an item of the place.
    """
    root = Path(root)
    index_of_tree = _tree_index(root, vendored_dirs)
    # Most trees have no registration file, and for them the plugin roots are not looked for.
    if not any(posixpath.basename(rel) in _REGISTRATION_NAMES for rel in index_of_tree[0]):
        return {}
    _selected, _read, excludes, _links = _registrations(root, vendored_dirs, _REGISTRATION_ROWS,
                                                        index_of_tree)
    if not excludes:
        return {}
    tree, _present, directories, _linked = index_of_tree
    left_out = {}
    for heuristic_row in _ANTIGRAVITY_PLACES:
        path = heuristic_row["path"]
        token = plugin_placeholder(path)
        if token:
            tail = path.split("/", 1)[1]
            places = [(f"{base}/{tail}" if base else tail, ("plugin", base))
                      for base in plugin_roots(root, vendored_dirs)[token]]
        else:
            places = [(d, ("workspace", d[:-len(path)].rstrip("/"))) for d in sorted(directories)
                      if d == path or d.endswith("/" + path)]
        for place, scope in places:
            names = _excluded_here(excludes, heuristic_row["mechanism"], scope)
            if names:
                left_out.update((rel, place) for rel, item
                                in _place_items(tree, place, heuristic_row["match_mode"])
                                if _names_item(names, item))
    return left_out


def declared_rule_files(root: Path, vendored_dirs=frozenset(), heuristic_rows=None,
                        *, include_registering_files=False) -> list[tuple[str, dict]]:
    """The rule files, and optionally the registration files, that Antigravity selects."""
    return registered_configuration_files(
        root, vendored_dirs, _RULES_JSON_ROWS if heuristic_rows is None else heuristic_rows,
        include_registering_files=include_registering_files)


_REGISTERED_SKILL_ROWS = [r for r in _REGISTRATION_ROWS if r["mechanism"] == "skills"]


def _contains_its_parent(root: Path, rel: str) -> bool:
    """Whether the directory at `rel` is the directory that contains it or one above, as a
    symlink such as `scripts -> .` or `scripts -> ..` makes it.

    Copied, such a directory would put the tree around it into the collected files, its source
    code and its `.env` included, as `copy_symlink_target` states for the target of a linked
    mechanism directory, which it does not copy either.
    """
    try:
        return (root / rel).parent.resolve().is_relative_to((root / rel).resolve())
    except (OSError, RuntimeError):
        return True


def _support_dirs(root: Path, prefix: str, vendored_dirs) -> list[str]:
    """Each of `SKILL_SUPPORT_DIRS` in the folder `prefix` that is a directory of the
    repository, not vendored, and not the folder or one above it (`_contains_its_parent`)."""
    return [f"{prefix}{d}" for d in SKILL_SUPPORT_DIRS
            if d not in vendored_dirs and (root / f"{prefix}{d}").is_dir()
            and _stays_inside(root, f"{prefix}{d}")
            and not _contains_its_parent(root, f"{prefix}{d}")]


def _root_skill_parts(root: Path, skill_md: str, vendored_dirs) -> list[str]:
    """The parts of the root skill with `skill_md`: the file and its support directories
    (`_support_dirs`)."""
    folder = posixpath.dirname(skill_md)
    return [skill_md] + _support_dirs(root, f"{folder}/" if folder else "", vendored_dirs)


def registered_root_skills(root: Path, vendored_dirs=frozenset()) -> list[str]:
    """The SKILL.md of every root skill that an Antigravity registration selects: a skill with
    a folder that contains the registration file that selects it (`_registrations`)."""
    return sorted(_registered_items(Path(root), vendored_dirs, _REGISTERED_SKILL_ROWS)[2])


def registered_skill_folders(root: Path, vendored_dirs=frozenset()) -> list[str]:
    """The parts of each skill that an Antigravity registration selects: its folder, or for a
    root skill its SKILL.md and support directories (`_root_skill_parts`), which the cleanup
    keeps whole."""
    root = Path(root)
    found, _read, root_skills, _links = _registered_items(root, vendored_dirs,
                                                          _REGISTERED_SKILL_ROWS)
    parts = set()
    for rel, _index in found:
        parts.update(_root_skill_parts(root, rel, vendored_dirs) if rel in root_skills
                     else [posixpath.dirname(rel)])
    return sorted(parts)


def collect_declared_rules(repo: Path, out: Path, vendored_dirs: set[str],
                           print_matches: bool = False) -> set[str]:
    """Copy registered components and their registration files. Return their tools.

    A selected skill is copied as its folder, and a root skill as its parts
    (`_root_skill_parts`), because its folder can be the whole repository. A linked
    customization root through which a registration file is read is copied as a marker
    (`write_marker`), and the registration file at its own path. The collected file cleanup,
    which keeps the marker as a file with the name of a spec directory, and the detection step
    then read the registration file as this step does (`_linked_directory`). The marker names
    the directory that the link leads to, relative to the directory that contains the link,
    rather than the link's own target. The target of `.agents -> lnk` is `lnk`, a link that
    leads on to `config/agents`, and the collected tree contains no `lnk` to follow.
    """
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()
    found, read, root_skills, links = _registered_items(repo, vendored_dirs, _REGISTRATION_ROWS)
    for link, index in sorted(links):
        dest = out / link
        directory = _linked_directory(repo, link)
        if directory is not None and not (dest.exists() or dest.is_symlink()):
            if print_matches:
                _log_match(display, repo / link, repo,
                           f"linked customization root of {_REGISTRATION_ROWS[index]['path']}")
            write_marker(dest, posixpath.relpath(directory or ".", posixpath.dirname(link) or "."))
    for rel, index in sorted(found | read):
        heuristic_row = _REGISTRATION_ROWS[index]
        flags.add(heuristic_row["tool"])
        for part in (_root_skill_parts(repo, rel, vendored_dirs) if rel in root_skills else [rel]):
            dest = out / part
            if dest.exists() or dest.is_symlink():
                continue
            if print_matches:
                _log_match(display, repo / part, repo, f"registered by {heuristic_row['path']}")
            if part != rel:
                copy_dir(repo / part, dest, vendored_dirs)
            elif (heuristic_row["mechanism"] == "skills" and rel not in root_skills
                  and Path(rel).name.lower() == _SKILL_MARKER.lower()):
                copy_dir((repo / rel).parent, dest.parent, vendored_dirs)
            else:
                copy_file(repo / rel, dest)
    return flags


def collect_declared_context(repo: Path, out: Path, vendored_dirs: set[str],
                             print_matches: bool = False) -> set[str]:
    """Copy every context file that the clone's configuration names. Returns the tools' flags."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()
    for rel, heuristic_row in declared_context_files(repo, vendored_dirs):
        dest = out / rel
        if not (dest.exists() or dest.is_symlink()):
            if print_matches:
                _log_match(display, repo / rel, repo, f"named by {heuristic_row['path']}")
            copy_file(repo / rel, dest)
        flags.add(heuristic_row["tool"])
    return flags


def collect_declared_subagents(repo: Path, out: Path, vendored_dirs: set[str],
                               print_matches: bool = False) -> set[str]:
    """Copy every subagent file and every part of a subagent that configuration of the clone
    names (`declared_subagent_files`), such as a role file that `config_file` of
    `.codex/config.toml` names and the instructions that the `model_instructions_file` of a Codex
    role names outside `.codex`, which no other pass copies. Returns the tools' flags."""
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()
    for rel, heuristic_row in declared_subagent_files(repo, vendored_dirs):
        dest = out / rel
        if not (dest.exists() or dest.is_symlink()):
            if print_matches:
                key = heuristic_row["match_mode"].partition(":")[2]
                _log_match(display, repo / rel, repo, f"named by {key} in {heuristic_row['path']}")
            copy_file(repo / rel, dest)
        flags.add(heuristic_row["tool"])
    return flags


# ---------------------------------------------------------------------------
# What a plugin contains
# ---------------------------------------------------------------------------
# A plugin is a directory with a manifest, and each vendor documents where under it the plugin's
# skills, agents, commands, hooks and MCP servers go. A repository can put a plugin in any
# directory, such as `plugins/<name>/` beside a marketplace or its own root. The rows
# name those parts under a placeholder, such as `<claude-plugin>/skills`, and the functions
# below find the directories that the placeholder stands for.
# The vendors' manifest directories, where a `plugin.json` is that vendor's and not the
# portable Agent Plugins manifest.
_MANIFEST_DIRS = tuple(sorted({manifest.rsplit("/", 1)[0]
                               for token, manifests in PLUGIN_ROOTS.items()
                               if token != "<agent-plugin>"
                               for manifest in manifests if "/" in manifest}))
_PLUGIN_PART_HEURISTICS = [r for r in _PATTERNS if r["path"].split("/", 1)[0] in PLUGIN_ROOTS
                      and not r["match_mode"].startswith(MANIFEST_FIELD_MODE)]
_MANIFEST_FIELD_HEURISTICS = manifest_field_rows(_PATTERNS)
# The tools that read a portable plugin through its Agent Plugins manifest, by the placeholder
# of their own plugins. A portable plugin that is also a root of that placeholder, because the
# tool's manifest is beside the portable one or the tool's marketplace lists the folder
# (`plugin_roots`), is that tool's plugin as well. Codex then reads `./skills` and `./mcp.json`
# (`parse_agent_plugin_manifest_uri` in codex-rs/core-plugins/src/agent_plugin_manifest.rs), and
# Copilot CLI "Agent Plugins: skills/ (fixed, no root SKILL.md fallback)" and "Agent Plugins:
# mcp.json" (CLI plugin reference, read 2026-11-07).
_PORTABLE_READERS = {"<codex-plugin>": "codex", "<copilot-plugin>": "copilot"}
# The rows of the portable parts that these tools read, `<agent-plugin>/skills` and
# `<agent-plugin>/mcp.json`, each with the tool in place of `any`, by (tool, path, match mode).
_PORTABLE_PART_ROWS = {(tool, r["path"], r["match_mode"]): {**r, "tool": tool}
                       for r in _PLUGIN_PART_HEURISTICS
                       if r["path"].split("/", 1)[0] == "<agent-plugin>" and r["tool"] == "any"
                       and r["mechanism"] in ("skills", "mcp")
                       for tool in _PORTABLE_READERS.values()}


def _claude_marketplace_entries(root: Path, vendored_dirs) -> dict:
    """{plugin root: [(entry, whether the root is the marketplace's own root, the marketplace
    file)]} for the local plugins that Claude Code marketplaces list."""
    entries = {}
    suffix = ".claude-plugin/marketplace.json"
    for path in list_repo_files(root, vendored_dirs):
        rel = path.relative_to(root).as_posix()
        if rel != suffix and not rel.endswith("/" + suffix):
            continue
        catalog_root = rel[:-len(suffix)].rstrip("/")
        doc = _json_object(root, path, "")
        for entry in _a_list(doc.get("plugins")):
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            source = entry.get("source")
            if not isinstance(source, str) or not source.startswith("./"):
                continue
            base = _inside_root(catalog_root, source)
            if base is None or vendored_part(root, base, vendored_dirs) is not None:
                continue
            target = root / base
            if not target.is_dir() or not target.resolve().is_relative_to((root / catalog_root).resolve()):
                continue
            base = "" if base == "." else base
            entries.setdefault(base, []).append((entry, base == catalog_root, rel))
    return entries


# Cursor's marketplace manifest, which a repository with several plugins keeps at its root:
# "A single Git repository can contain multiple plugins using a marketplace manifest. Place it
# at .cursor-plugin/marketplace.json".
_CURSOR_MARKETPLACE = ".cursor-plugin/marketplace.json"
# The component fields that a Cursor marketplace entry can have.
_CURSOR_ENTRY_COMPONENTS = ("skills", "rules", "agents", "commands", "hooks", "mcpServers")


def _cursor_marketplace_entries(root: Path) -> dict:
    """{plugin directory: entry} for the local plugins that the root Cursor marketplace lists.

    "For a marketplace entry with "source": "my-plugin": The parser looks for
    my-plugin/.cursor-plugin/plugin.json If found, the per-plugin manifest is merged with the
    marketplace entry (manifest values take precedence) Component discovery runs within the
    my-plugin/ directory, using manifest paths if specified or folder-based discovery as fallback"
    (cursor.com/docs/reference/plugins). A listed directory is a plugin whether or not it
    has a manifest. `metadata.pluginRoot` is the "prefix path for all plugin sources", and a source
    is a path or an object with the path.
    """
    doc = _json_object(root, root / _CURSOR_MARKETPLACE, "")
    metadata = doc.get("metadata")
    prefix = metadata.get("pluginRoot") if isinstance(metadata, dict) else None
    prefix = prefix.strip() if isinstance(prefix, str) else ""
    entries = {}
    for entry in _a_list(doc.get("plugins")):
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        source = entry.get("source")
        if isinstance(source, dict):
            source = source.get("path")
        if not isinstance(source, str) or not source.strip() or "\\" in source:
            continue
        base = posixpath.normpath(posixpath.join(prefix or ".", source.strip()))
        if base != "." and not _inside(base):
            continue
        base = "" if base == "." else base
        if (root / base).is_dir() and _stays_inside(root, base or "."):
            entries.setdefault(base, entry)
    return entries


# Codex's marketplace below the root of a marketplace, which is the repository root for the
# repository's own list: "Use $REPO_ROOT/.agents/plugins/marketplace.json for a repo-scoped list"
# (https://developers.openai.com/plugins/build/plugins, read 2026-11-07). Below a root, Codex
# reads one marketplace, the first entry of `MARKETPLACE_MANIFEST_RELATIVE_PATHS` that exists:
# this place, then `.agents/plugins/api_marketplace.json`, `.claude-plugin/marketplace.json` and
# `.cursor-plugin/marketplace.json`. It reads a later entry only where every earlier one is
# missing (`find_marketplace_manifest_path` in codex-rs/core-plugins/src/marketplace.rs of
# openai/codex rust-v0.167.0, read 2026-11-08). The cloning step and the detection read only this
# first entry as Codex's marketplace, as long as it is undecided which of the later entries name
# Codex as a reader.
_CODEX_MARKETPLACE = ".agents/plugins/marketplace.json"
# The manifests that Codex reads for a plugin without the portable Agent Plugins manifest, in the
# order in which `find_plugin_manifest_path` tries them: `DISCOVERABLE_PLUGIN_MANIFEST_PATHS` in
# codex-rs/exec-server-protocol/src/protocol.rs of openai/codex rust-v0.167.0 (read 2026-11-07),
# `".codex-plugin/plugin.json", ".claude-plugin/plugin.json", ".cursor-plugin/plugin.json"`.
_CODEX_MANIFEST_ORDER = (".codex-plugin/plugin.json", ".claude-plugin/plugin.json",
                         ".cursor-plugin/plugin.json")
# The fields of a Codex marketplace entry that are no fields of the manifest that Codex writes
# from the entry (`_codex_entry_manifest`), `name`, `source`, `policy` and `category` of
# `RawMarketplaceManifestPlugin` in codex-rs/core-plugins/src/marketplace.rs of rust-v0.167.0.
_CODEX_ENTRY_FIELDS = ("name", "source", "policy", "category")


def _codex_source(catalog_root: str, source) -> str | None:
    """The repository-relative folder that the local source `source` of a Codex marketplace
    names, resolved against `catalog_root`, the root of the marketplace, or None where Codex
    refuses the path.

    "Keep source.path relative to the marketplace root, start it with ./, and keep it inside
    that root" (plugins page, read 2026-11-07). `resolve_local_plugin_source_path` in
    codex-rs/core-plugins/src/marketplace.rs of rust-v0.167.0 takes `.` and `./` as the
    marketplace root, refuses another path without the `./` prefix ("local plugin source path
    must start with `./`") or with a component that is no name after it ("local plugin source path
    must stay within the marketplace root"), and joins the rest to the marketplace root. Rust's
    `Path::components` drops a `.` and an empty component inside a path, but not a `.` at its
    start."""
    if not isinstance(source, str):
        return None
    if source in (".", "./"):
        return catalog_root
    rest = source.removeprefix("./")
    if rest == source or rest.startswith("/"):
        return None
    parts = rest.split("/")
    if parts[0] == "." or ".." in parts:
        return None
    parts = [part for part in parts if part not in ("", ".")]
    return "/".join([catalog_root, *parts] if catalog_root else parts)


def _file_paths(root: Path, vendored_dirs, listed) -> list[str]:
    """`listed`, repository-relative paths of the tree's files, or the paths of every file of
    the tree (`list_repo_files`) where `listed` is None."""
    if listed is not None:
        return list(listed)
    return [path.relative_to(root).as_posix() for path in list_repo_files(root, vendored_dirs)]


def _has_one_of(root: Path, base: str, manifests) -> bool:
    """Whether the folder `base` of the tree at `root` has one of `manifests`."""
    return any((root / (f"{base}/{manifest}" if base else manifest)).is_file()
               for manifest in manifests)


# The values of the policy of a Codex marketplace entry: `MarketplacePluginInstallPolicy` and
# `MarketplacePluginAuthPolicy` in codex-rs/core-plugins/src/marketplace.rs, and `Product` in
# codex-rs/protocol/src/protocol.rs, which serde reads in lower case, `rename_all = "lowercase"`,
# or in capitals, `#[serde(alias = "CODEX")]` (openai/codex c9b4267c370a, read 2026-11-08).
_CODEX_INSTALL_POLICIES = ("NOT_AVAILABLE", "AVAILABLE", "INSTALLED_BY_DEFAULT")
_CODEX_AUTH_POLICIES = ("ON_INSTALL", "ON_USE")
_CODEX_PRODUCTS = ("chatgpt", "codex", "atlas")
# The product of a Codex CLI session: `restriction_product` returns `Some(Product::Codex)` for
# `SessionSource::Cli`, `VSCode`, `Exec` and `Mcp` (codex-rs/protocol/src/protocol.rs), and the
# thread manager passes it to the plugins manager (codex-rs/core/src/thread_manager.rs).
_CODEX_PRODUCT = "codex"
# A segment of a plugin ID: ASCII letters, digits, `_` and `-`, and in a plugin's name also `.`
# between other characters (`validate_plugin_segment` in
# codex-rs/core-plugin-common/src/plugin_id.rs of openai/codex c9b4267c370a, read 2026-11-08).
_CODEX_MARKETPLACE_NAME = re.compile(r"[A-Za-z0-9_-]+")
_CODEX_PLUGIN_NAME = re.compile(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*")
# The fields that each form of a remote source of a Codex marketplace entry requires as strings,
# by its `source` (`RawMarketplaceManifestPluginSourceObject` in
# codex-rs/core-plugins/src/marketplace.rs of openai/codex c9b4267c370a, read 2026-11-08), and
# the fields that it reads as an optional string.
_CODEX_REMOTE_SOURCES = {"url": (("url",), ("path", "ref", "sha")),
                         "git-subdir": (("url", "path"), ("ref", "sha")),
                         "npm": (("package",), ("version", "registry"))}


def _is_optional_string(value) -> bool:
    """Whether `value` is what serde reads as an `Option<String>`: a string or null."""
    return value is None or isinstance(value, str)


def codex_marketplace_plugins(doc) -> list | None:
    """The entries of `doc`, a parsed `.agents/plugins/marketplace.json`, where Codex reads it as
    a marketplace, or None where it reads none of the file's plugins.

    Codex parses the file with `serde_json::from_str` into `RawMarketplaceManifest` in
    codex-rs/core-plugins/src/marketplace.rs of openai/codex c9b4267c370a (read 2026-11-08): a
    string `name` and a list of `plugins`, each an object with a string `name`, a `source` of any
    form and an optional `category` string. An entry's `policy` is an object or absent. Its
    `installation` and `authentication` are absent or one of the values of
    `_CODEX_INSTALL_POLICIES` and `_CODEX_AUTH_POLICIES`, and its `products` is absent, null or a
    list of the products of `_CODEX_PRODUCTS`, in lower case or in capitals. A field that does not
    parse makes the whole file fail, "invalid marketplace file". The test does not read the
    `interface` of the file."""
    if not isinstance(doc, dict) or not isinstance(doc.get("name"), str):
        return None
    plugins = doc.get("plugins")
    if not isinstance(plugins, list):
        return None
    for entry in plugins:
        if (not isinstance(entry, dict) or not isinstance(entry.get("name"), str)
                or "source" not in entry or not _is_optional_string(entry.get("category"))):
            return None
        if "policy" not in entry:
            continue
        policy = entry["policy"]
        if not isinstance(policy, dict):
            return None
        if ("installation" in policy and policy["installation"] not in _CODEX_INSTALL_POLICIES
                or "authentication" in policy
                and policy["authentication"] not in _CODEX_AUTH_POLICIES):
            return None
        products = policy.get("products")
        if products is not None and (not isinstance(products, list) or not all(
                isinstance(product, str)
                and product in _CODEX_PRODUCTS + tuple(p.upper() for p in _CODEX_PRODUCTS)
                for product in products)):
            return None
    return plugins


def codex_installs(entry: dict, marketplace_name: str) -> bool:
    """Whether Codex CLI installs the plugin of `entry`, an entry of the marketplace
    `marketplace_name` that Codex reads (`codex_marketplace_plugins`), by the tests that do not
    depend on the entry's source.

    `PluginId::new` refuses a plugin name or a marketplace name with other characters than
    `_CODEX_PLUGIN_NAME` and `_CODEX_MARKETPLACE_NAME` allow, and Codex skips the entry with the
    warning "skipping invalid marketplace plugin". `find_installable_marketplace_plugin` in
    codex-rs/core-plugins/src/marketplace.rs of openai/codex c9b4267c370a (read 2026-11-08)
    refuses an entry with `resolved.policy.installation ==
    MarketplacePluginInstallPolicy::NotAvailable` and an entry with `products` that do not admit
    the session's product, `_CODEX_PRODUCT`: `None => true`, `Some([]) => false`, and otherwise
    `restriction_product.is_some_and(|product| product.matches_product_restriction(products))`."""
    if (_CODEX_MARKETPLACE_NAME.fullmatch(marketplace_name) is None
            or _CODEX_PLUGIN_NAME.fullmatch(entry["name"]) is None):
        return False
    policy = entry.get("policy") or {}
    if policy.get("installation") == "NOT_AVAILABLE":
        return False
    products = policy.get("products")
    return products is None or _CODEX_PRODUCT in {product.lower() for product in products}


def codex_resolves_source(catalog_root: str, source) -> bool:
    """Whether Codex resolves `source`, the source of an entry of a Codex marketplace with the
    marketplace root `catalog_root`, to a plugin that it can install: a local source that
    `_codex_source` resolves, or a remote source of a form of `_CODEX_REMOTE_SOURCES` with the
    fields that the form requires. `resolve_supported_plugin_source` in
    codex-rs/core-plugins/src/marketplace.rs of openai/codex c9b4267c370a (read 2026-11-08) skips
    an entry with a source of another form, "skipping marketplace plugin with unsupported
    source", or with a source that it cannot resolve. The test does not apply Codex's checks of
    a URL, a remote path or an npm package name."""
    if isinstance(source, str):
        return _codex_source(catalog_root, source) is not None
    if not isinstance(source, dict):
        return False
    if source.get("source") == "local":
        return (isinstance(source.get("path"), str)
                and _codex_source(catalog_root, source["path"]) is not None)
    required, optional = _CODEX_REMOTE_SOURCES.get(source.get("source"), (None, ()))
    return (required is not None and all(isinstance(source.get(key), str) for key in required)
            and all(_is_optional_string(source.get(key)) for key in optional))


def codex_marketplace_installs(doc, catalog_root: str) -> bool:
    """Whether `doc`, a parsed `.agents/plugins/marketplace.json` with the marketplace root
    `catalog_root`, is a marketplace that Codex reads (`codex_marketplace_plugins`) with at least
    one entry that Codex installs (`codex_installs`) from a source that it resolves
    (`codex_resolves_source`). A marketplace in which Codex can install no plugin, such as
    UMEP-dev/SUEWS's, with the only entry set to `NOT_AVAILABLE`, gives Codex nothing to load."""
    plugins = codex_marketplace_plugins(doc)
    return plugins is not None and any(
        codex_installs(entry, doc["name"]) and codex_resolves_source(catalog_root, entry["source"])
        for entry in plugins)


def _codex_marketplace_entries(root: Path, vendored_dirs, listed=None) -> dict:
    """{plugin root: (entry, marketplace file)} for the local plugins that the Codex marketplaces
    of the tree list, the first entry of the tree's first marketplace in path order for a root
    that two entries name. `listed` gives the paths of the tree's files (`_file_paths`).

    A Codex marketplace is a `.agents/plugins/marketplace.json` at any depth, and its plugins
    resolve against its marketplace root, the directory that contains `.agents/`: "Codex resolves
    source.path relative to the marketplace root, not relative to the .agents/plugins/ folder"
    (plugins page, read 2026-11-07). A local source is a path or an object of the source `local`,
    "For local entries, source can also be a plain string path such as "./plugins/my-plugin"",
    and `_codex_source` resolves it. Codex loads none of the file's plugins where the file does
    not parse as its marketplace (`codex_marketplace_plugins`), and it installs no plugin of an
    entry that `codex_installs` refuses, such as an entry with the installation policy
    `NOT_AVAILABLE`.
    """
    entries = {}
    marketplaces = sorted(rel for rel in _file_paths(root, vendored_dirs, listed)
                          if rel == _CODEX_MARKETPLACE or rel.endswith("/" + _CODEX_MARKETPLACE))
    for rel in marketplaces:
        catalog_root = rel[:-len(_CODEX_MARKETPLACE)].rstrip("/")
        doc = _json_object(root, root / rel, "")
        plugins = codex_marketplace_plugins(doc)
        if plugins is None:
            continue
        for entry in plugins:
            if not codex_installs(entry, doc["name"]):
                continue
            source = entry["source"]
            if isinstance(source, dict):
                source = source.get("path") if source.get("source") == "local" else None
            base = _codex_source(catalog_root, source)
            if (base is None or vendored_part(root, base, vendored_dirs) is not None
                    or not (root / base).is_dir() or not _stays_inside(root, base or ".")):
                continue
            entries.setdefault(base, (entry, rel))
    return entries


def _codex_entry_manifest(entry) -> dict | None:
    """The manifest that Codex writes from the marketplace entry `entry` for a plugin folder
    without a manifest, or None where it writes none.

    `marketplace_plugin_manifest_fallback` in codex-rs/core-plugins/src/marketplace.rs of
    rust-v0.167.0 takes the entry's fields other than `_CODEX_ENTRY_FIELDS` with its name, and
    only where there is such a field or a category (`has_metadata`). The store installs it as
    the plugin's `.codex-plugin/plugin.json`, "A real plugin manifest always wins. The fallback
    only fills the gap for marketplace sources that cannot be changed in place"
    (codex-rs/core-plugins/src/store.rs). A folder without a manifest and without such fields
    loads no plugin, "missing or invalid plugin.json" (codex-rs/core-plugins/src/loader.rs)."""
    fields = {key: value for key, value in entry.items() if key not in _CODEX_ENTRY_FIELDS}
    if not fields and entry.get("category") is None:
        return None
    return {**fields, "name": entry["name"]}


# Copilot CLI's marketplaces in the order in which the CLI looks for one in a marketplace's
# directory, "marketplace.json, .plugin/marketplace.json, .github/plugin/marketplace.json, or
# .claude-plugin/marketplace.json (checked in this order)" (CLI plugin reference, read
# 2026-11-07), without the last, which is Claude Code's marketplace.
_COPILOT_MARKETPLACES = ("marketplace.json", ".plugin/marketplace.json",
                         ".github/plugin/marketplace.json")


def copilot_marketplace_document(doc) -> bool:
    """Whether `doc`, a parsed JSON document, has the fields that distinguish a Copilot CLI
    marketplace from other catalogs: a nonempty `name`, an `owner` object with a nonempty `name`
    and a `plugins` array, the required fields of the reference's table "Top-level fields"."""
    return (isinstance(doc, dict) and isinstance(doc.get("name"), str) and bool(doc["name"])
            and isinstance(doc.get("owner"), dict) and isinstance(doc["owner"].get("name"), str)
            and bool(doc["owner"]["name"]) and isinstance(doc.get("plugins"), list))


def _copilot_marketplace_files(root: Path, vendored_dirs, listed=None) -> dict:
    """{marketplace directory: the marketplace file that Copilot CLI reads there,
    repository-relative} for the Copilot CLI marketplaces of the tree. `listed` gives the paths
    of the tree's files (`_file_paths`).

    A marketplace's directory is the repository root, with any of `_COPILOT_MARKETPLACES`, or a
    directory with `.plugin/marketplace.json` or `.github/plugin/marketplace.json`, and the CLI
    reads the first of `_COPILOT_MARKETPLACES` that exists there: `Dqe` of Copilot CLI 1.0.63
    (app.js) tries the files in this order and returns an error, "Invalid marketplace.json", for
    an invalid first file."""
    directories = {""}
    for rel in _file_paths(root, vendored_dirs, listed):
        for name in _COPILOT_MARKETPLACES[1:]:
            if rel == name or rel.endswith("/" + name):
                directories.add(rel[:-len(name)].rstrip("/"))
    found = {}
    for directory in sorted(directories):
        prefix = f"{directory}/" if directory else ""
        first = next((f"{prefix}{name}" for name in _COPILOT_MARKETPLACES
                      if (root / f"{prefix}{name}").is_file()), None)
        if first is not None:
            found[directory] = first
    return found


def _copilot_marketplace_entries(root: Path, vendored_dirs, listed=None) -> dict:
    r"""{plugin root: entry} for the local plugins that the Copilot CLI marketplaces of the tree
    list (`_copilot_marketplace_files`). `listed` gives the paths of the tree's files
    (`_file_paths`).

    A plugin's `source` is a path "relative to the root of the repository. It is not necessary to
    use ./ at the start of the path", and an object names a GitHub repository or a Git URL (CLI
    plugin reference, read 2026-11-07). `metadata.pluginRoot` prefixes every path, and the folder
    has to exist inside the marketplace's directory: `dFt` of Copilot CLI 1.0.63 (app.js) builds
    `./${u}/${d}` from the prefix and the path, each without its `./`, resolves the result
    without its `./` against the directory, `a=s.replace(/^\.\//,"")` and `Vs.resolve(o,a)`,
    and refuses a folder outside the directory, "Plugin path escapes marketplace directory".
    Without a prefix, a path that is empty without its `./`, such as `./`, therefore names the
    marketplace's directory, as `.` does.
    """
    entries = {}
    for directory, marketplace in _copilot_marketplace_files(root, vendored_dirs, listed).items():
        doc = _json_object(root, root / marketplace, "")
        if not copilot_marketplace_document(doc):
            continue
        metadata = doc.get("metadata")
        plugin_root = metadata.get("pluginRoot") if isinstance(metadata, dict) else None
        if plugin_root is not None and not isinstance(plugin_root, str):
            continue
        for entry in doc["plugins"]:
            source = entry.get("source") if isinstance(entry, dict) else None
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) \
                    or not isinstance(source, str):
                continue
            path = (f"{plugin_root.removeprefix('./')}/{source.removeprefix('./')}" if plugin_root
                    else source.removeprefix("./") or ".")
            base = _inside_root(directory, path)
            base = "" if base == "." else base
            if (base is None or vendored_part(root, base, vendored_dirs) is not None
                    or not (root / base).is_dir() or not _stays_inside(root, base or ".")):
                continue
            entries.setdefault(base, entry)
    return entries


def _claude_plugin_documents(manifest, entries):
    """(document, omitted defaults, retained defaults) for each effective declaration of a
    Claude Code plugin's components, from its manifest and its marketplace entries.

    A plugin without a manifest takes its entry as the manifest: "No `plugin.json`: the entry
    is the manifest, regardless of `strict`" (marketplace reference, read 2026-11-05). Such a
    document names the marketplace file under `_declared_in`, the file that holds what the
    entry configures inline."""
    if not entries:
        return [(manifest, set(), set())]
    documents = []
    appended = ("commands", "agents", "skills", "outputStyles", "themes")
    for entry, marketplace_root, marketplace in entries:
        if manifest is not None and entry.get("strict") is False and any(
                key in entry for key in (*appended, "hooks")):
            continue
        if "hooks" in entry and not isinstance(entry["hooks"], dict):
            continue
        retain = set()
        if manifest is None:
            doc = dict(entry)
            doc["_declared_in"] = marketplace
        else:
            doc = dict(manifest)
            for key in appended:
                if key not in entry:
                    continue
                if key not in manifest:
                    doc[key] = entry[key]
                    retain.add(key)
                elif isinstance(manifest[key], dict) and isinstance(entry[key], dict):
                    doc[key] = {**manifest[key], **entry[key]}
                else:
                    old_paths, _ = _manifest_paths(manifest[key], "adds")
                    new_paths, _ = _manifest_paths(entry[key], "adds")
                    doc[key] = old_paths + new_paths
            # Inline entry hooks add no repository path. Manifest and default hook
            # files remain inputs even when an entry replaces individual events.
        command_names = {}
        for source in (manifest or {}, entry):
            commands = source.get("commands")
            if isinstance(commands, dict):
                command_names.update(commands)
        doc["_command_names"] = command_names
        omit = {"skills"} if marketplace_root and "skills" in entry else set()
        documents.append((doc, omit, retain))
    return documents


def plugin_command_names(root: Path, vendored_dirs=frozenset()) -> dict[str, str]:
    """{source file: command name} for the commands that the plugins declare. Of several names
    for one file, the alphabetically first counts.

    A name that does not encode as UTF-8 is ignored with a warning, so that the file keeps its
    stem as its name. JSON can escape a lone surrogate in a key, such as `"de\\ud800ploy"`, and
    the strict UTF-8 write of `skills.csv` would stop the detection step at every Start, after
    it had replaced `repos.csv` and the CSVs written before.
    """
    entries = _claude_marketplace_entries(root, vendored_dirs)
    names = {}
    for base in plugin_roots(root, vendored_dirs)["<claude-plugin>"]:
        manifest = _manifest(root, base, "<claude-plugin>")
        for doc, _omitted, _retained in _claude_plugin_documents(manifest, entries.get(base, [])):
            if not doc or not _claude_component_paths_valid(root, base, doc):
                continue
            commands = doc.get("_command_names", doc.get("commands"))
            if not isinstance(commands, dict):
                continue
            for name, entry in sorted(commands.items()):
                source = entry.get("source") if isinstance(entry, dict) else None
                rel = _inside_root(base, source) if isinstance(source, str) else None
                if rel is None or not _stays_inside(root, rel):
                    continue
                try:
                    name.encode("utf-8")
                except UnicodeEncodeError:
                    logs.warning('detection', f"Ignored the command name {ascii(name)} that the "
                                              f"plugin of {dir_name_to_repo(Path(root).name)} at "
                                              f"{base or '.'} declares for {rel}, because it does "
                                              f"not encode as UTF-8. The file keeps its stem as "
                                              f"its name.")
                    continue
                names[rel] = min(name, names.get(rel, name))
    return names


def plugin_roots(root: Path, vendored_dirs=frozenset()) -> dict:
    """{placeholder: [repo-relative plugin root, ...]} for every plugin in the tree.

    `""` is the repository itself. A root is a directory with the manifest that `PLUGIN_ROOTS`
    names for the placeholder and, where `PLUGIN_PARENTS` names a directory for the
    placeholder, a folder of that directory. The Agent Plugins manifest counts only where its
    schema identifies it as one, and not inside the manifest directory of another vendor. An
    Antigravity plugin is also a directory that a plugins.json selects, and none that the
    top-level `exclude` of a plugins.json leaves out. A Copilot CLI plugin is also a directory
    with a `plugin.json` that the content of a legacy manifest identifies
    (`copilot_legacy_manifest`).

    A local source of a vendor's marketplace is a plugin of that vendor as well: of Claude Code
    and of Cursor in every case, of Codex where Codex loads the folder, with a manifest of the
    portable layout or of `_CODEX_MANIFEST_ORDER` or with the manifest that Codex writes from the
    entry (`_codex_entry_manifest`), and of Copilot CLI where the folder has the portable manifest
    or one of `_COPILOT_MANIFEST_ORDER`: "All plugins consist of a plugin directory containing a
    manifest file named plugin.json" (CLI plugin reference, read 2026-11-07). A root of
    `<codex-plugin>` or `<copilot-plugin>` that is a root of `<agent-plugin>` as well is a plugin
    that the tool reads through the portable manifest (`plugin_components`).
    """
    root = Path(root)
    found = {token: set() for token in PLUGIN_ROOTS}
    legacy = []
    # The files named `marketplace.json`, which `_codex_marketplace_entries` and
    # `_copilot_marketplace_entries` read, so that they do not list the tree again.
    marketplaces = []
    for path in list_repo_files(root, vendored_dirs):
        rel = path.relative_to(root).as_posix()
        if path.name == "marketplace.json":
            marketplaces.append(rel)
        for token, manifests in PLUGIN_ROOTS.items():
            for manifest in manifests:
                if rel != manifest and not rel.endswith("/" + manifest):
                    continue
                base = rel[:-len(manifest)].rstrip("/")
                parent, _, name = base.rpartition("/")
                if token in PLUGIN_PARENTS and (parent not in PLUGIN_PARENTS[token] or not name):
                    continue
                if token == "<agent-plugin>":
                    if any(base == d or base.endswith("/" + d) for d in _MANIFEST_DIRS):
                        continue
                    text = _repository_text(root, path, errors="replace")
                    if text is None or not agent_plugins_document("basename_json_plugin_manifest",
                                                                  text):
                        continue
                found[token].add(base)
        if path.name == _COPILOT_LEGACY_MANIFEST:
            legacy.append(path)
    # A plugins.json's top-level `exclude` leaves out a plugin of a default directory as well,
    # and every default directory is in a customization root at the workspace root.
    registration_rows = [r for r in _REGISTRATION_ROWS if r["mechanism"] == "plugins"]
    selected, _read, excludes, _links = _registrations(root, vendored_dirs, registration_rows)
    left_out = _excluded_here(excludes, "plugins", ("workspace", ""))
    found["<antigravity-plugin>"] = {base for base in found["<antigravity-plugin>"]
                                     if not _names_item(left_out, base)}
    found["<antigravity-plugin>"].update(
        antigravity_plugin_dir(rel) for rel, item, _index, scope, _root_skill in selected
        if not _names_item(_excluded_here(excludes, "plugins", scope), item))
    found["<claude-plugin>"].update(_claude_marketplace_entries(root, vendored_dirs))
    found["<cursor-plugin>"].update(base for base in _cursor_marketplace_entries(root)
                                    if vendored_part(root, base, vendored_dirs) is None)
    found["<codex-plugin>"].update(
        base for base, (entry, _marketplace)
        in _codex_marketplace_entries(root, vendored_dirs, marketplaces).items()
        if base in found["<agent-plugin>"] or _has_one_of(root, base, _CODEX_MANIFEST_ORDER)
        or _codex_entry_manifest(entry) is not None)
    found["<copilot-plugin>"].update(
        base for base in _copilot_marketplace_entries(root, vendored_dirs, marketplaces)
        if base in found["<agent-plugin>"] or _has_one_of(root, base, _COPILOT_MANIFEST_ORDER))
    # A plugin.json that is another kind's manifest, Antigravity's or the portable one, or that
    # is in a vendor's manifest directory, one of `_MANIFEST_DIRS` or another directory named
    # `.<name>-plugin` (`copilot_legacy_manifest`), is no legacy Copilot CLI manifest.
    taken = found["<antigravity-plugin>"] | found["<agent-plugin>"]
    for path in legacy:
        base = path.parent.relative_to(root).as_posix()
        base = "" if base == "." else base
        if (base not in taken | found["<copilot-plugin>"]
                and not any(base == d or base.endswith("/" + d) for d in _MANIFEST_DIRS)
                and copilot_legacy_manifest(root, base, _repository_text(root, path,
                                                                         errors="replace"))):
            found["<copilot-plugin>"].add(base)
    return {token: sorted(bases) for token, bases in found.items()}


def plugin_manifests(root: Path, vendored_dirs=frozenset()) -> list[str]:
    """The manifest of every plugin in the tree, repository-relative and sorted.

    The manifest directories are copied wherever they are, but `.github` is copied at the root
    only. Without this list, a Copilot CLI plugin under `plugins/<name>/.github/plugin/` would
    keep its parts and lose its manifest. The list also holds each Copilot CLI marketplace that
    the CLI reads and that has the fields of one (`_copilot_marketplace_files`,
    `copilot_marketplace_document`), so that the collected file cleanup and the detection step
    find the plugins that it lists in the copy as the cloning step finds them in the clone, also
    through a `.github` below the root.
    """
    root = Path(root)
    found = set()
    for token, bases in plugin_roots(root, vendored_dirs).items():
        for base in bases:
            for manifest in PLUGIN_ROOTS[token]:
                rel = f"{base}/{manifest}" if base else manifest
                if (root / rel).is_file():
                    found.add(rel)
    found.update(copilot_marketplaces(root, vendored_dirs))
    return sorted(found)


def copilot_marketplaces(root: Path, vendored_dirs=frozenset()) -> list[str]:
    """The Copilot CLI marketplaces of the tree that the CLI reads and that have the fields of
    one (`_copilot_marketplace_files`, `copilot_marketplace_document`), repository-relative and
    sorted."""
    root = Path(root)
    return sorted(rel for rel in _copilot_marketplace_files(root, vendored_dirs).values()
                  if copilot_marketplace_document(_json_object(root, root / rel, "")))


def plugin_parts(root: Path, vendored_dirs=frozenset(), heuristic_rows=None) -> list[tuple[str, dict]]:
    """Every part of a plugin in the tree that its tool reads, with the row that reads it.

    Returns `[(repo-relative path, heuristics row)]`, a directory or a file, sorted: the default
    places that the placeholder rows name, the places that a manifest names in addition to them
    or instead of them, and a plugin's root SKILL.md, as `plugin_components` decides.
    """
    return sorted({(rel, id(heuristic_row)): (rel, heuristic_row)
                   for rel, heuristic_row, _kind in plugin_components(root, vendored_dirs, heuristic_rows)}.values(),
                  key=lambda item: (item[0], item[1]["tool"], item[1]["mechanism"]))


# The values of a manifest key that name no path of this repository.
_NOT_A_PATH = ("://", ".mcpb", ".dxt")


def _falsy_in_js(value) -> bool:
    """Whether Copilot CLI, which is JavaScript, reads a manifest value as false: null, false,
    0 and "", but not an empty list or object."""
    return value is None or value is False or value == "" or (
        type(value) in (int, float) and value == 0)


def _manifest_paths(value, how: str) -> tuple[list[str], bool]:
    """(the paths that a manifest key's value names, whether it replaces the default place).

    The paths are a string, the strings of a list, the `source` of each entry of an object map
    as Claude Code's `commands` takes, and for Copilot CLI's form the `paths` of
    `{"paths": [...]}`. Inline configuration and URLs name no path.
    """
    replaces = how in ("replaces", "files", "paths")
    if how == "paths" and _falsy_in_js(value):
        # Copilot CLI's resolver opens with `if(!t)return[YB.join(r,n)]`: an empty or false
        # value is the default place.
        return [], False
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [v for v in value if isinstance(v, str)]
    elif isinstance(value, dict) and how == "paths":
        listed = value.get("paths")
        # Any other object is refused, "Invalid ${n} object form: expected { paths: string[] }",
        # and a string there is not the list of its characters.
        if not (isinstance(listed, list) and all(isinstance(v, str) for v in listed)):
            return [], True
        values = listed
        replaces = bool(value.get("exclusive"))
    elif isinstance(value, dict):
        values = [v["source"] for v in value.values()
                  if isinstance(v, dict) and isinstance(v.get("source"), str)]
    else:
        values = []
    return [v for v in values if v.strip() and not any(m in v for m in _NOT_A_PATH)], replaces


def _inside_root(base: str, value: str) -> str | None:
    """`value` resolved against the plugin root `base`, repository-relative, or None when it
    leaves the root: "Every component path must resolve inside the plugin root"."""
    if value.startswith("/") or "\\" in value:
        return None
    rel = posixpath.normpath(posixpath.join(base or ".", value))
    if rel == "..":
        return None
    if base:
        return rel if rel == base or rel.startswith(base + "/") else None
    return None if rel.startswith("../") else rel


def _skill_folders(root: Path, rel: str, vendored_dirs, *, recursive=True, stop_at_skill=False) -> list[str]:
    """Every folder at or below `rel` that contains a SKILL.md, repository-relative. A declared
    skills entry is a directory of skills or a skill itself."""
    folders = set()
    base = root / rel if rel != "." else root
    marker = _SKILL_MARKER.lower()
    if (stop_at_skill or not recursive) and any(p.is_file() and p.name.lower() == marker for p in base.iterdir()):
        return [rel]
    candidates = list_repo_files(base, set(vendored_dirs)) if recursive else base.glob("*/*")
    for path in candidates:
        if vendored_part(base, path.relative_to(base), vendored_dirs) is not None:
            continue
        if path.name.lower() == marker and path.is_file():
            folders.add(path.parent.relative_to(root).as_posix())
    return sorted(folders)


def _string_fields(value, fields):
    return all(key not in value or isinstance(value[key], str) for key in fields)


def _string_map(value):
    return isinstance(value, dict) and all(isinstance(v, str) for v in value.values())


def _string_list(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _positive_fields(value, fields):
    return all(key not in value or (type(value[key]) in (int, float) and value[key] > 0)
               for key in fields)


_COPILOT_HOOK_EVENTS = frozenset((
    "sessionStart", "sessionEnd", "userPromptSubmitted", "preToolUse", "preMcpToolCall",
    "postToolUse", "postToolUseFailure", "errorOccurred", "agentStop", "subagentStop",
    "subagentStart", "preCompact", "permissionRequest", "notification"))
_COPILOT_MATCHER_EVENTS = frozenset((
    "preToolUse", "preMcpToolCall", "postToolUse", "subagentStart", "preCompact",
    "permissionRequest", "notification"))


def _copilot_hook(entry, event):
    """Whether an entry satisfies Copilot CLI's hook schema, excluding host policies."""
    if not isinstance(entry, dict):
        return False
    kind = entry.get("type", "command")
    if kind == "prompt":
        return (event == "sessionStart" and isinstance(entry.get("prompt"), str)
                and bool(entry["prompt"]))
    if kind not in ("command", "http"):
        return False
    if not _positive_fields(entry, ("timeout", "timeoutSec")):
        return False
    if "_vsCodeCompat" in entry and entry["_vsCodeCompat"] not in (
            "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PreMcpToolCall",
            "PostToolUse", "PostToolUseFailure", "ErrorOccurred", "Stop", "SubagentStop",
            "SubagentStart", "PreCompact", "PermissionRequest", "Notification"):
        return False
    if event in _COPILOT_MATCHER_EVENTS and "matcher" in entry:
        if not isinstance(entry["matcher"], str) or not entry["matcher"]:
            return False
    if kind == "http":
        url = entry.get("url")
        if not isinstance(url, str) or re.search(r"\s", url):
            return False
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return False
            parsed.port  # Access validates a port when one is supplied.
        except ValueError:
            return False
        return (("headers" not in entry or _string_map(entry["headers"]))
                and ("allowedEnvVars" not in entry or _string_list(entry["allowedEnvVars"])))
    if not _string_fields(entry, ("bash", "powershell", "command", "exec", "cwd")):
        return False
    shell = any(key in entry for key in ("bash", "powershell", "command"))
    executable = "exec" in entry
    return (shell != executable
            and ("args" not in entry or _string_list(entry["args"]))
            and ("env" not in entry or _string_map(entry["env"])))


def _copilot_hooks(value):
    """Whether every entry of a known hook event satisfies Copilot CLI's schema. Copilot keeps
    unknown event keys."""
    return isinstance(value, dict) and all(
        isinstance(entries, list) and all(_copilot_hook(entry, event) for entry in entries)
        for event, entries in value.items() if event in _COPILOT_HOOK_EVENTS)


def _copilot_lsp(value):
    """Whether the LSP document satisfies Copilot CLI's server-map schema."""
    if not isinstance(value, dict) or not isinstance(value.get("lspServers"), dict):
        return False
    for name, server in value["lspServers"].items():
        if not re.fullmatch(r"[0-9a-zA-Z_-]+", name) or not isinstance(server, dict):
            return False
        if server.get("disabled") is True:
            continue
        commands = ("command", "bash", "powershell")
        if not any(key in server for key in commands):
            return False
        if not _string_fields(server, (*commands, "cwd", "rootUri")):
            return False
        if any(key in server and not server[key] for key in commands):
            return False
        if not _string_map(server.get("fileExtensions")):
            return False
        if "args" in server and not _string_list(server["args"]):
            return False
        if "env" in server and not _string_map(server["env"]):
            return False
        if not _positive_fields(server, ("requestTimeoutMs", "initializationTimeoutMs",
                                         "spawnTimeoutMs", "warmupTimeoutMs")):
            return False
    return True


def _copilot_component(value, mechanism):
    if mechanism == "lsp":
        return _copilot_lsp(value)
    if mechanism == "hooks":
        return (isinstance(value, dict) and _copilot_hooks(value.get("hooks"))
                and ("version" not in value or (type(value["version"]) in (int, float) and value["version"] == 1))
                and ("disableAllHooks" not in value or type(value["disableAllHooks"]) is bool))
    return True


def _parses(root: Path, path: Path, *, copilot_mechanism=None) -> bool:
    """Whether a component file of `root` parses and satisfies the requested Copilot schema."""
    if not path.is_file():
        return False
    if path.suffix != ".json" and not copilot_mechanism:
        return True
    text = _repository_text(root, path, errors="replace")
    if text is None:
        return False
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return False
    return _copilot_component(value, copilot_mechanism) if copilot_mechanism else True


# The manifests of a Copilot CLI plugin in the order in which the CLI checks them: "Legacy
# plugins: .plugin/plugin.json, plugin.json, .github/plugin/plugin.json, or
# .claude-plugin/plugin.json (checked in this order)" (CLI plugin reference, read 2026-11-07).
# A `plugin.json` at the plugin root makes a plugin only where its content identifies it
# (`copilot_legacy_manifest`), because many programs use the name. Where
# `.github/plugin/plugin.json` makes the directory a plugin, however, the CLI reads the root
# `plugin.json` in its place. `.claude-plugin/plugin.json`, Claude Code's manifest, is the
# manifest that the CLI reads only in a folder that a Copilot marketplace lists and that has none
# of the other three, because only such a marketplace makes it a Copilot plugin (`plugin_roots`).
_COPILOT_MANIFEST_ORDER = (".plugin/plugin.json", "plugin.json", ".github/plugin/plugin.json",
                           ".claude-plugin/plugin.json")
_COPILOT_LEGACY_MANIFEST = "plugin.json"
# The name of a vendor's manifest directory, `.<name>-plugin`, such as `.claude-plugin`,
# `.cursor-plugin`, `.codex-plugin` or `.grok-plugin`. The plugin root is the parent of such a
# directory, and a plugin.json in it is that vendor's manifest. A legacy Copilot CLI manifest is
# in the plugin directory itself: the reference's default layout puts `plugin.json` directly in
# `my-plugin/`, and the reference's other places of a legacy manifest, `.plugin/` and
# `.github/plugin/`, are manifest directories in the plugin root.
_VENDOR_MANIFEST_DIRECTORY = re.compile(r"\.[^/]+-plugin")
# The required field of a legacy manifest: "Kebab-case plugin name (letters, numbers, and
# hyphens only). Maximum 64 characters." (CLI plugin reference, Legacy manifest fields).
_COPILOT_PLUGIN_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
# The component path fields of a legacy manifest, which "tell the CLI where to find your
# plugin's components", with the form of each: a path, a list of paths or `{"paths": [...]}`
# for a directory field, which the resolver of Copilot CLI 1.0.63 reads (`_manifest_paths`), and
# a path or an inline object for a configuration file field, of which only a path names a
# place. The reference's table of fields leaves out `rules`, which the CLI reads as it reads
# `commands`. `extensions` is left out: Grafana's plugin.json has an object of its own under
# that key, and no row reads an extension of Copilot CLI.
_COPILOT_PATH_FIELDS = {"agents": "directory", "skills": "directory", "commands": "directory",
                        "rules": "directory", "hooks": "file", "mcpServers": "file",
                        "lspServers": "file"}


def copilot_legacy_manifest(root: Path, base: str, text: str | None) -> bool:
    """Whether `text`, the `plugin.json` of the directory `base` of the repository at `root`,
    is a legacy manifest of Copilot CLI: a JSON object without the Agent Plugins schema, outside
    a directory named `.<name>-plugin`, with a `name` that the reference allows, and with a
    component path field in the form that the CLI resolves that names a place inside the plugin
    that exists.

    Copilot CLI installs any directory with a `plugin.json` that parses, also one that sets no
    field of a legacy manifest, and it would read Grafana's and NodeBB's plugin.json as well.
    The fields that tell the CLI where the plugin's components are, in the forms that the CLI
    reads, distinguish a Copilot plugin from these programs, whose `hooks` and `commands` are
    lists of objects or of event names. A field that names no place of the plugin, such as
    `./skills/` in a template beside no `skills` directory, configures nothing. A plugin.json in
    a directory named `.<name>-plugin`, such as `skills/.grok-plugin/plugin.json`, is the
    manifest of that vendor's plugin with its root in the parent directory
    (`_VENDOR_MANIFEST_DIRECTORY`), whether or not `_MANIFEST_DIRS` lists the vendor."""
    if text is None or _VENDOR_MANIFEST_DIRECTORY.fullmatch(posixpath.basename(base)):
        return False
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return False
    if (not isinstance(doc, dict) or agent_plugins_document("basename_json_plugin_manifest", text)
            or not isinstance(doc.get("name"), str)
            or _COPILOT_PLUGIN_NAME.fullmatch(doc["name"]) is None):
        return False
    for field, kind in _COPILOT_PATH_FIELDS.items():
        value = doc.get(field)
        if isinstance(value, str):
            named = [value]
        elif kind == "directory" and isinstance(value, list):
            named = value
        elif kind == "directory" and isinstance(value, dict):
            named = value.get("paths")
        else:
            continue
        if not isinstance(named, list) or not all(isinstance(v, str) for v in named):
            continue
        for place in named:
            if not place.strip() or any(marker in place for marker in _NOT_A_PATH):
                continue
            rel = _inside_root(base, place.strip())
            if rel is not None and _stays_inside(root, rel) and (root / rel).exists():
                return True
    return False


# The format in which a plugin's tool reads its manifest: the `content_format` of the row that
# names the manifest under the plugin's placeholder, such as `<antigravity-plugin>/plugin.json`,
# and strict JSON where no row names it.
_MANIFEST_FORMATS = {r["path"]: r["content_format"] for r in _PATTERNS
                     if plugin_placeholder(r["path"]) and r["mechanism"] == "plugins"}


def _parsed_manifest(root: Path, path: Path, content_format: str):
    """The object that the manifest at `path` contains, or None where it is not read
    (`_repository_text`) or does not parse to an object in `content_format`."""
    text = _repository_text(root, path, errors="replace")
    if text is None:
        return None
    try:
        doc = jsonc.loads(text, content_format)
    except (ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _codex_manifest(root: Path, base: str, entries: dict):
    """The parsed manifest that Codex reads for the plugin at `base` outside the portable layout,
    or None: the first of `_CODEX_MANIFEST_ORDER` that exists, or, for a folder without any of
    them that a Codex marketplace lists, the manifest that Codex writes from the entry of
    `entries` (`_codex_marketplace_entries`, `_codex_entry_manifest`). A manifest that does not
    parse gives None, as `_manifest` does. A document read from another file than
    `.codex-plugin/plugin.json` names that file, the manifest of another vendor or the
    marketplace, under `_declared_in`, the file that holds what it configures inline."""
    prefix = f"{base}/" if base else ""
    for manifest in _CODEX_MANIFEST_ORDER:
        path = root / f"{prefix}{manifest}"
        if path.is_file():
            doc = _parsed_manifest(root, path, _MANIFEST_FORMATS.get(f"<codex-plugin>/{manifest}", ""))
            if doc is not None and manifest not in PLUGIN_ROOTS["<codex-plugin>"]:
                doc["_declared_in"] = f"{prefix}{manifest}"
            return doc
    entry, marketplace = entries.get(base, (None, None))
    doc = _codex_entry_manifest(entry) if entry is not None else None
    if doc is not None:
        doc["_declared_in"] = marketplace
    return doc


def borrowed_manifest(root: Path, base: str, token: str, portable_roots) -> str | None:
    """The manifest of another vendor that the tool of `token`, `<codex-plugin>` or
    `<copilot-plugin>`, reads as the manifest of its plugin at `base`, repository-relative, or
    None: the first manifest of the tool's order (`_CODEX_MANIFEST_ORDER`,
    `_COPILOT_MANIFEST_ORDER`) that exists, where it is in another vendor's manifest directory,
    such as the `.claude-plugin/plugin.json` of a folder that the tool's marketplace lists and
    that has no manifest of the tool's own and no portable manifest. A plugin of
    `portable_roots`, the roots of `<agent-plugin>`, has none, because both tools read its
    portable manifest first, and the portable manifest names no tool."""
    order = {"<codex-plugin>": _CODEX_MANIFEST_ORDER,
             "<copilot-plugin>": _COPILOT_MANIFEST_ORDER}[token]
    if base in portable_roots:
        return None
    prefix = f"{base}/" if base else ""
    first = next((manifest for manifest in order if (root / f"{prefix}{manifest}").is_file()), None)
    if first is None or first.rsplit("/", 1)[0] not in _MANIFEST_DIRS or first in PLUGIN_ROOTS[token]:
        return None
    return f"{prefix}{first}"


def _manifest(root: Path, base: str, token: str):
    """The parsed manifest of the plugin at `base`, or None. A Cursor plugin's manifest is
    merged with its marketplace entry, the manifest's values first, and an entry stands in for
    the manifest of a plugin that has none. Where the servers come from the entry, the document
    names the marketplace file under `_declared_in`, the file that holds them."""
    doc = None
    for manifest in _COPILOT_MANIFEST_ORDER if token == "<copilot-plugin>" else PLUGIN_ROOTS[token]:
        path = root / (f"{base}/{manifest}" if base else manifest)
        if path.is_file():
            doc = _parsed_manifest(root, path, _MANIFEST_FORMATS.get(f"{token}/{manifest}", ""))
            if doc is None:
                return None
            break
    if token == "<cursor-plugin>":
        entry = _cursor_marketplace_entries(root).get(base)
        if entry is not None:
            components = {key: entry[key] for key in _CURSOR_ENTRY_COMPONENTS if key in entry}
            if "mcpServers" in components and "mcpServers" not in (doc or {}):
                components["_declared_in"] = _CURSOR_MARKETPLACE
            doc = {**components, **(doc or {})}
    return doc


def _claude_component_paths_valid(root: Path, base: str, doc) -> bool:
    """Whether every component path that a Claude Code plugin's manifest declares can load. A
    plugin with a path that cannot load is rejected."""
    if doc is None:
        return True
    plugin_root = (root / base).resolve()

    def valid_path(value, key):
        if key == "mcpServers" and (value.startswith("https://")
                                    or value.endswith((".mcpb", ".dxt"))):
            return True
        if not value.startswith("./") and not (key == "skills" and value == "."):
            return False
        if ".." in value.split("/"):
            return False
        rel = _inside_root(base, value)
        if rel is None:
            return False
        target = root / rel
        if not target.exists() or not target.resolve().is_relative_to(plugin_root):
            return False
        return key != "agents" or target.is_file()

    def valid(value, key):
        if isinstance(value, str):
            return valid_path(value, key)
        if isinstance(value, list):
            return all(valid(item, key) for item in value)
        if isinstance(value, dict):
            if key == "commands":
                return all(isinstance(entry, dict)
                           and ("source" not in entry or
                                isinstance(entry["source"], str) and valid_path(entry["source"], key))
                           for entry in value.values())
            return key in ("hooks", "mcpServers", "lspServers")
        return False

    keys = ("skills", "commands", "agents", "hooks", "mcpServers", "lspServers",
            "outputStyles", "workflows")
    try:
        return all(valid(doc[key], key) for key in keys if key in doc)
    except RecursionError:
        # Lists nested deeper than the interpreter's recursion limit: no declaration that can
        # be shown to load.
        return False


def _resolve_declared(root: Path, rel: str, mode: str, vendored_dirs, *, stop_at_skill=False) -> list[str]:
    """What the row that reads a default place with `mode` reads at the declared place `rel`."""
    if vendored_part(root, rel, vendored_dirs) is not None:
        return []
    # A declared place that a link leads out of the repository, such as `skills -> ../..`, names
    # files of the collecting machine, which are never collected (`_stays_inside`). Listing it
    # would read the collected trees and the clones beside the repository, or the whole file
    # system, for a result that the collector then discards.
    if not _stays_inside(root, rel):
        return []
    target = root / rel if rel != "." else root
    if mode in ("skill_dir", "skill_dir_flat"):
        return (_skill_folders(root, rel, vendored_dirs, recursive=mode == "skill_dir",
                               stop_at_skill=stop_at_skill) if target.is_dir() else [])
    if is_directory_mode(mode):
        extensions = tuple(mode.split(":", 1)[1].split("|")) if ":" in mode else ()
        if target.is_file():
            return [rel] if rel.endswith(extensions) else []
        if not target.is_dir():
            return []
        # The default place's own depth: a `dir_ext` row reads a directory's children only.
        files = (target.iterdir() if mode.startswith(("dir_ext:", "dir_ext_described:",
                                                       "dir_ext_triggered:"))
                 else list_repo_files(target, set(vendored_dirs)))
        return sorted(p.relative_to(root).as_posix() for p in files
                      if p.is_file() and p.name.endswith(extensions))
    return [rel] if target.is_file() else []


# The registration row of an Antigravity plugin's skills.json, the file with the top-level
# `exclude` that also leaves out the skills that the plugin's manifest lists.
_ANTIGRAVITY_PLUGIN_SKILL_ROWS = [r for r in _REGISTRATION_ROWS if r["mechanism"] == "skills"
                                  and plugin_placeholder(r["path"]) == "<antigravity-plugin>"]


def _antigravity_skill_entries(value) -> list[str]:
    """The entries of the `skills` value of an Antigravity plugin's manifest, decoded as
    Antigravity CLI 1.4.9 decodes them: a string is one entry and a list of strings is one entry
    per string, each without the white space around it and the `/` at its end, and an empty
    entry is left out. The CLI fails to decode a value of another type and a list with a value
    that is neither a string nor null, and the steps read no entry from such a value."""
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or not all(v is None or isinstance(v, str) for v in values):
        return []
    return [entry for entry in (v.strip().rstrip("/") for v in values if v is not None) if entry]


def _antigravity_manifest_skills(root: Path, base: str, value, index_of_tree,
                                 vendored_dirs) -> list[tuple[str, str, bool]]:
    """(SKILL.md, skill folder, whether it is a root skill) for each skill that the `skills`
    value of the manifest of the Antigravity plugin at `base` lists.

    Antigravity CLI 1.4.9 resolves the entries with the code that resolves the entries of the
    plugin's skills.json, with the plugin root as the base, and applies the top-level `exclude` of
    that file to them (the section "The places that a plugin's manifest names" of
    docs/heuristics.md). An entry therefore resolves against the plugin root, and against the
    workspace root where nothing exists there (`_resolved`). It selects a skill folder or its
    SKILL.md, or the skill folders directly in another folder (`_entry_items`). A skill with a
    folder that is the plugin root or a folder above it is a root skill, as a registered one is
    (`_registrations`).
    """
    entries = _antigravity_skill_entries(value)
    if not entries:
        return []
    tree, present, directories, _linked = index_of_tree
    candidates = _registration_candidates(tree, "skills")
    _selected, _read, excludes, _links = _registrations(root, vendored_dirs,
                                                        _ANTIGRAVITY_PLUGIN_SKILL_ROWS, index_of_tree)
    left_out = _excluded_here(excludes, "skills", ("plugin", base))
    real_root = root.resolve()
    found = []
    for entry in entries:
        resolved = _resolved(entry, base, present, directories)
        if resolved is None:
            continue
        for skill_md, item in _entry_items(resolved, {}, "skills", candidates, present):
            if _names_item(left_out, item) or not (root / skill_md).resolve().is_relative_to(real_root):
                continue
            found.append((skill_md, item, not item or base == item or base.startswith(item + "/")))
    return found


def _codex_component_paths(value, key: str) -> tuple[list[str], bool, bool]:
    """(the candidate paths, whether they replace the default place, whether the value has
    inline configuration) of a Codex plugin manifest's value under `key`."""
    values = [value] if isinstance(value, str) else value
    if isinstance(values, list) and all(isinstance(item, str) for item in values):
        paths = [item for item in values if item.startswith("./") and item != "./"
                 and ".." not in item.split("/") and "\\" not in item]
        return paths, bool(paths), False
    if key == "mcpServers" and isinstance(value, dict):
        return [], True, bool(value)
    hooks = [value] if isinstance(value, dict) else value
    if key == "hooks" and isinstance(hooks, list) and hooks and all(
            isinstance(item, dict) and isinstance(item.get("hooks", {}), dict)
            and all(isinstance(entries, list) for entries in item.get("hooks", {}).values())
            for item in hooks):
        return [], True, any(item.get("hooks") for item in hooks)
    return [], False, False


def has_a_grouped_hook(events) -> bool:
    """Whether a map of hook events holds a hook in the three levels of event, matcher group
    and handler that Claude Code, Codex and Gemini CLI read: an event with a list of matcher
    groups, one of which has a handler object in its `hooks` list.

    An event with a value that is no list holds no hook. Claude Code 2.1.318 drops it with
    "must be an array of matchers; received ...; entry ignored", Gemini CLI skips it ("is not an
    array. Skipping.", hookRegistry.ts at f4df88a7), and Codex reads each event as a list of
    `MatcherGroup` (codex-rs/protocol/src/hooks.rs at be765f1a). A handler in place of a
    matcher group holds none either. Claude Code and Gemini CLI discard an entry without a
    `hooks` list ("Hook matcher "hooks" must be an array of hook entries", "Discarding invalid
    hook definition"), and Codex reads such an entry as a group with no handler, because
    `MatcherGroup` has `#[serde(default)] pub hooks: Vec<HookHandlerConfig>`."""
    return isinstance(events, dict) and any(
        isinstance(groups, list) and any(
            isinstance(group, dict) and isinstance(group.get("hooks"), list)
            and any(isinstance(handler, dict) for handler in group["hooks"])
            for group in groups)
        for groups in events.values())


def _string(value) -> bool:
    """Whether a JSON value is a string."""
    return isinstance(value, str)


def _boolean(value) -> bool:
    """Whether a JSON value is `true` or `false`."""
    return isinstance(value, bool)


def _strings(value) -> bool:
    """Whether a JSON value is a list of strings."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _string_map(value) -> bool:
    """Whether a JSON value is an object with strings as its values."""
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def _positive_integer(value) -> bool:
    """Whether a JSON value is a number that JavaScript reads as a safe positive integer, as
    `v().int().positive()` of Zod requires. JavaScript reads `30.0` as the integer 30."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 < value <= 2**53 - 1 and float(value).is_integer())


def _server_id(value) -> bool:
    """Whether a JSON value is the `id` of a `claudeai-proxy` server, `o().regex(ia)` with
    `ia=/^[A-Za-z0-9_-]+$/` in Claude Code 2.1.319."""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]+", value) is not None


# The fields of a plain JSON type that the schemas of the server types that Claude Code loads
# declare beside the command or the url, with their tests. Claude Code 2.1.319 declares
# `abt=p(()=>u({type:T("stdio").optional(),command:o().min(1,"Command cannot be empty"),
# args:A(o()).default([]),env:me(o(),o()).optional(),timeout:pt().optional(),
# alwaysLoad:O().optional(),bareElicitationCapability:O().optional(),role:Pt()}))`, and the
# schemas of `sse` and `http`, which `streamable-http` shares, declare `headers:me(o(),o())
# .optional(),headersHelper:o().optional()`, the same `timeout`, `alwaysLoad` and
# `bareElicitationCapability`, and `discoveryCache:O().optional()`, which the schema of `ws`
# leaves out. A field that a schema does not declare is dropped, and the fields with a
# `.catch`, such as `role`, never fail.
_CLAUDE_STDIO_FIELDS = {"args": _strings, "env": _string_map, "timeout": _positive_integer,
                        "alwaysLoad": _boolean, "bareElicitationCapability": _boolean}
_CLAUDE_WS_FIELDS = {"headers": _string_map, "headersHelper": _string,
                     "timeout": _positive_integer, "alwaysLoad": _boolean,
                     "bareElicitationCapability": _boolean}
_CLAUDE_HTTP_FIELDS = {**_CLAUDE_WS_FIELDS, "discoveryCache": _boolean}
_CLAUDE_URL_TYPES = {"sse": _CLAUDE_HTTP_FIELDS, "http": _CLAUDE_HTTP_FIELDS,
                     "streamable-http": _CLAUDE_HTTP_FIELDS, "ws": _CLAUDE_WS_FIELDS}
# The fields that the schemas of the types that Claude Code parses and then drops from a file or
# a plugin require: `ENs=p(()=>u({type:T("sdk"),name:o(),...`, `Hm=p(()=>u({type:T("sse-ide"),
# url:It(),ideName:o(),...` and the same for `ws-ide`, and `kNs=p(()=>u({type:
# T("claudeai-proxy"),url:It(),id:o().regex(ia),...`. Claude Code 2.1.319 drops a server of
# these types from a plugin, because `type "${f.data.type}" servers are registered by the host
# application at runtime and cannot be declared by a plugin` and `type "claudeai-proxy" is used
# only by claude.ai connectors`.
_CLAUDE_DROPPED_TYPES = {"sdk": {"name": _string},
                         "sse-ide": {"url": _string, "ideName": _string},
                         "ws-ide": {"url": _string, "ideName": _string},
                         "claudeai-proxy": {"url": _string, "id": _server_id}}


def _claude_loads_server(server) -> bool:
    """Whether Claude Code loads the MCP server entry `server`: an entry with a `command` and no
    `type` or the type "stdio", or an entry with a `url` and a type of `_CLAUDE_URL_TYPES`, with
    the fields that its schema declares in their types.

    Claude Code 2.1.319 takes an entry without a type for a stdio server,
    `"type"in Ce&&typeof Ce.type==="string"?Ce.type:"stdio"`, whose schema requires the command.
    A `type` that is not a string, such as a list, therefore selects the stdio schema, which
    rejects it. The MCP page states that "A JSON entry that has a url but no type is a
    configuration error, because Claude Code reads an entry with no type as a stdio server.
    Claude Code skips that server" (code.claude.com/docs/en/mcp, read 2026-11-07). The test
    reads the fields of a plain JSON type (`_CLAUDE_STDIO_FIELDS`, `_CLAUDE_URL_TYPES`) and not
    the objects `oauth`, `tools` and `toolPermissions`."""
    if not isinstance(server, dict):
        return False
    if "type" not in server or server["type"] == "stdio":
        fields = _CLAUDE_STDIO_FIELDS
        if not isinstance(server.get("command"), str) or not server["command"]:
            return False
    elif isinstance(server["type"], str) and server["type"] in _CLAUDE_URL_TYPES:
        fields = _CLAUDE_URL_TYPES[server["type"]]
        if not isinstance(server.get("url"), str):
            return False
    else:
        return False
    return all(test(server[key]) for key, test in fields.items() if key in server)


def claude_loads_a_server(servers) -> bool:
    """Whether Claude Code loads a server of a map of MCP servers that it reads from a file
    (`_claude_loads_server`). Claude Code 2.1.319 parses each server of a file on its own and
    skips one that fails its schema, `let He=xe().safeParse(Ce);if(!He.success){` for a
    `.mcp.json` of a directory and `let Ce=Bie().safeParse(Ee);if(Ce.success)ke[ne]=Ce.data;
    else` for the MCP file of a plugin."""
    return isinstance(servers, dict) and any(_claude_loads_server(server)
                                             for server in servers.values())


def _claude_loads_inline_servers(maps: list) -> bool:
    """Whether Claude Code loads a server of the inline maps of MCP servers that a plugin manifest
    or a marketplace entry configures: a map with a server that it loads
    (`_claude_loads_server`), and no entry in any of the maps that fails the schemas of its
    type, those of the types that it loads and those of `_CLAUDE_DROPPED_TYPES`.

    Claude Code 2.1.319 parses each inline map as a whole, `me(o(),Bie())` in the schema of the
    manifest key `mcpServers`, and refuses the manifest or the marketplace entry when one entry
    of a map fails the union of the schemas, `Bie=p(()=>Be([abt(),Wan(),Hm(),zm(),LBt(),vGn(),
    ENs(),kNs()]))`. It then loads no component of the plugin. `claude plugin validate` of Claude
    Code 2.1.319 reports "mcpServers: Invalid input" for a manifest that has a server with a
    `url` and no `type` beside a server with a `command`, and for a stdio server with a boolean
    in its `env`, as e2e-test-quest/uuv's `.claude-plugin/plugin.json` has it."""
    def parses(server):
        if _claude_loads_server(server):
            return True
        kind = server.get("type") if isinstance(server, dict) else None
        fields = _CLAUDE_DROPPED_TYPES.get(kind) if isinstance(kind, str) else None
        return fields is not None and all(key in server and test(server[key])
                                          for key, test in fields.items())

    return (all(parses(server) for servers in maps for server in servers.values())
            and any(claude_loads_a_server(servers) for servers in maps))


def _inline_configuration(value, key: str, token: str) -> bool:
    """Whether the value of the `mcpServers`, `lspServers` or `hooks` key of a manifest or a
    marketplace entry of a plugin of the kind `token` configures a server or a hook inline: the
    value, or an object in a list, that is an inline map of MCP servers with a server that the
    plugin's tool loads, of LSP servers with a server that has a `command` and an
    `extensionToLanguage` map, or an event map of hooks. Claude Code loads the inline MCP
    servers of a Claude Code plugin only where it accepts every server of the maps
    (`_claude_loads_inline_servers`), and Cursor loads a server with a `command` or a `url`.

    The manifest reference gives each key the type "Path, object, or array of either" and reads
    an inline `mcpServers` object as "Inline map ... Uses the map as server configs keyed by
    name". It requires the LSP fields `command` and `extensionToLanguage`, "Map of file extension
    to LSP language ID, at least one entry", and for `hooks` "An inline object is the event map
    itself, with no wrapper" (code.claude.com/docs/en/plugins/manifest-reference, read
    2026-11-05). Cursor reads `mcpServers` in the same forms: "Path to MCP config file, inline MCP
    server config, or an array of either." (cursor.com/docs/reference/plugins, read 2026-11-06)."""
    items = value if isinstance(value, list) else [value]
    if key == "mcpServers" and token == "<claude-plugin>":
        return _claude_loads_inline_servers([item for item in items if isinstance(item, dict)])

    def configures(item):
        if not isinstance(item, dict):
            return False
        if key == "hooks":
            return has_a_grouped_hook(item)
        servers = [server for server in item.values() if isinstance(server, dict)]
        if key == "mcpServers":
            return any(isinstance(server.get(field), str) for server in servers
                       for field in ("command", "url"))
        return key == "lspServers" and any(
            isinstance(server.get("command"), str)
            and isinstance(server.get("extensionToLanguage"), dict)
            and bool(server["extensionToLanguage"]) for server in servers)

    return any(configures(item) for item in items)


def plugin_components(root: Path, vendored_dirs=frozenset(), heuristic_rows=None) -> list[tuple[str, dict, str]]:
    """Every place of every plugin in the tree that its tool reads, with the row and its kind.

    Returns `[(repo-relative path, row, kind)]`, kind being `default` for a default place that
    a placeholder row names, `declared` for a place that the manifest names, which has the row
    of the default place so that it is read the same way, `root_skill` for a plugin's root
    SKILL.md, `root_skill_folder` for each of `SKILL_SUPPORT_DIRS` beside it and `inline` for
    the file that configures MCP servers, LSP servers or hooks inline, with the row of the
    default place: a Codex, Claude Code or Cursor manifest, or the marketplace file with the entry
    that holds what a Claude Code plugin without a manifest or a Cursor plugin configures. For each
    plugin, and for
    each row with the plugin's placeholder:

    - A path that lists alternatives, `a|b`, is the first of them that exists and parses.
    - A `manifest_field:k:h` row with a key that the manifest sets names more places, kept
      inside the plugin root and broken into what the default row reads, and `h` decides whether the
      default place is still read (the section "The parts of a plugin" of docs/heuristics.md). An
      Antigravity plugin's `skills` key names skills as the entries of its skills.json do
      (`_antigravity_manifest_skills`).
    - A `plugin_root_skill` row is the root SKILL.md, where there is no `skills/` directory
      and no `skills` key, with the skill's own directories beside it.
    - A default place of a portable plugin that an `<agent-plugin>` row with the tool `any`
      reads, `skills/` or `mcp.json`, is returned again with that row's copy for each tool that
      reads the plugin through its portable manifest (`_PORTABLE_READERS`,
      `_PORTABLE_PART_ROWS`).

    A Codex plugin outside the portable layout takes its keys from the manifest that Codex reads
    (`_codex_manifest`). `heuristic_rows`, when given, limits the default rows and reads no
    manifest.
    """
    root = Path(root)
    found = []
    # The files of the tree, listed once and only for an Antigravity manifest with a `skills` key.
    index_of_tree = None
    defaults = _PLUGIN_PART_HEURISTICS if heuristic_rows is None else heuristic_rows
    manifest_rows = _MANIFEST_FIELD_HEURISTICS if heuristic_rows is None else []
    marketplace_entries = _claude_marketplace_entries(root, vendored_dirs)
    roots = plugin_roots(root, vendored_dirs)
    # The entries of the Codex marketplaces, read only where a Codex plugin outside the portable
    # layout has no manifest of `_CODEX_MANIFEST_ORDER` and Codex reads the manifest that it
    # writes from its entry.
    codex_entries = (_codex_marketplace_entries(root, vendored_dirs)
                     if any(base not in roots["<agent-plugin>"]
                            and not _has_one_of(root, base, _CODEX_MANIFEST_ORDER)
                            for base in roots["<codex-plugin>"]) else {})
    # The tools that read each portable plugin through its Agent Plugins manifest, because the
    # plugin is a root of their placeholder as well.
    portable_readers = {base: [tool for token, tool in _PORTABLE_READERS.items()
                               if base in roots[token]]
                        for base in roots["<agent-plugin>"]}
    for token, bases in roots.items():
        token_defaults = [r for r in defaults if r["path"].split("/", 1)[0] == token]
        token_manifest_rows = [r for r in manifest_rows if r["path"].split("/", 1)[0] == token]
        for base in bases:
            # Codex reads a plugin through the Agent Plugins manifest where the root `plugin.json`
            # is one, before `.codex-plugin/plugin.json` (`find_plugin_manifest_path` in
            # codex-rs/utils/plugins/src/plugin_namespace.rs), and then takes its skills from
            # `./skills` and its MCP servers from `./mcp.json`. The `.codex-plugin` manifest
            # sets only apps, hooks and the interface (`parse_agent_plugin_manifest_uri` in
            # codex-rs/core-plugins/src/agent_plugin_manifest.rs, read 2026-10-13). The
            # `<agent-plugin>` rows read those, as Codex's parts as well (`_PORTABLE_READERS`),
            # and the compatibility layout is not read there.
            if token == "<codex-plugin>" and base in roots.get("<agent-plugin>", ()):
                continue
            # Copilot CLI reads such a plugin through the Agent Plugins manifest as well: "A root
            # plugin.json that targets Agent Plugins takes precedence over .plugin/plugin.json
            # and .claude-plugin/plugin.json per spec §5.1". The CLI's own parts of such a plugin
            # are under com.github.copilot/, which the `<agent-plugin>` rows read. Copilot CLI
            # 1.0.101, run with --plugin-dir on 2026-10-21, read skills/ and
            # com.github.copilot/commands/ of such a plugin and not the skills that its legacy
            # manifest names or its commands/, with either legacy manifest.
            if token == "<copilot-plugin>" and base in roots.get("<agent-plugin>", ()):
                continue
            prefix = f"{base}/" if base else ""
            reads_manifest = bool(token_manifest_rows) or heuristic_rows is None
            if token == "<codex-plugin>":
                doc = _codex_manifest(root, base, codex_entries) if reads_manifest else None
            else:
                doc = _manifest(root, base, token) if reads_manifest else None
            documents = (_claude_plugin_documents(doc, marketplace_entries.get(base, []))
                         if token == "<claude-plugin>" else [(doc, set(), set())])
            for doc, omitted_defaults, retained_defaults in documents:
                if token == "<claude-plugin>" and not _claude_component_paths_valid(root, base, doc):
                    continue
                for heuristic_row in token_defaults:
                    if heuristic_row["match_mode"] == PLUGIN_ROOT_SKILL_MODE:
                        rest = heuristic_row["path"].split("/", 1)[1]
                        # Copilot CLI falls back to the root SKILL.md where `!t.skills`, which
                        # includes an empty value. Claude Code falls back where the manifest
                        # has no skills key.
                        skills = (doc or {}).get("skills")
                        if (heuristic_rows is None
                                and (_falsy_in_js(skills) if token == "<copilot-plugin>"
                                     else skills is None)
                                and not (root / f"{prefix}skills").is_dir()
                                and (root / f"{prefix}{rest}").is_file()):
                            found.append((f"{prefix}{rest}", heuristic_row, "root_skill"))
                            found.extend((folder, heuristic_row, "root_skill_folder")
                                         for folder in _support_dirs(root, prefix, vendored_dirs))
                        continue
                    alternatives = path_alternatives(heuristic_row["path"])
                    if len(alternatives) > 1:
                        default = next((f"{prefix}{a}" for a in alternatives
                                        if _parses(root, root / f"{prefix}{a}",
                                                   copilot_mechanism=heuristic_row["mechanism"]
                                                   if token == "<copilot-plugin>" else None)), None)
                    else:
                        target = root / f"{prefix}{alternatives[0]}"
                        default = (f"{prefix}{alternatives[0]}"
                                   if target.is_dir() or target.is_file() else None)
                        # A root plugin.json is a legacy manifest by its content alone, also where
                        # another manifest of the CLI makes the directory a plugin, because many
                        # programs use the name.
                        if (default is not None and token == "<copilot-plugin>"
                                and alternatives[0] == _COPILOT_LEGACY_MANIFEST
                                and not copilot_legacy_manifest(
                                    root, base, _repository_text(root, target, errors="replace"))):
                            default = None
                    keep_default = default is not None and not (
                        heuristic_row["mechanism"] == "skills" and "skills" in omitted_defaults
                        and heuristic_row["path"].endswith("/skills"))
                    for field_row in token_manifest_rows:
                        if default_place_row(field_row, _PATTERNS) is not heuristic_row:
                            continue
                        key, how = manifest_key(field_row)
                        component_doc = doc
                        source = f"{prefix}{PLUGIN_ROOTS[token][0]}"
                        if heuristic_row["tool"] == "codex" and token == "<agent-plugin>":
                            extensions = (doc or {}).get("extensions", {})
                            extension = extensions.get("com.openai") if isinstance(extensions, dict) else None
                            if isinstance(extension, dict):
                                component_doc = extension
                            else:
                                component_doc = _manifest(root, base, "<codex-plugin>")
                                source = f"{prefix}{PLUGIN_ROOTS['<codex-plugin>'][0]}"
                        if component_doc is None or component_doc.get(key) is None:
                            continue
                        value = component_doc[key]
                        if token == "<antigravity-plugin>":
                            # The skills that the key names are read in addition to the skills of
                            # the default place. A skill at the depth that the default place reads
                            # is read with the default place and not added again, and a skill
                            # nested deeper in it is added.
                            if index_of_tree is None:
                                index_of_tree = _tree_index(root, vendored_dirs)
                            for skill_md, item, root_skill in _antigravity_manifest_skills(
                                    root, base, value, index_of_tree, vendored_dirs):
                                if root_skill:
                                    folder = posixpath.dirname(skill_md)
                                    found.append((skill_md, heuristic_row, "root_skill"))
                                    found.extend((d, heuristic_row, "root_skill_folder")
                                                 for d in _support_dirs(root, f"{folder}/" if folder else "",
                                                                        vendored_dirs))
                                elif not (keep_default and (
                                        item == default or item.startswith(default + "/")
                                        and (heuristic_row["match_mode"] != "skill_dir_flat"
                                             or item.count("/") == default.count("/") + 1))):
                                    found.append((item, heuristic_row, "declared"))
                            continue
                        if heuristic_row["tool"] == "codex":
                            values, replaces, inline = _codex_component_paths(value, key)
                            if inline:
                                found.append((component_doc.get("_declared_in", source),
                                              heuristic_row, "inline"))
                        else:
                            values, replaces = _manifest_paths(value, how)
                            # Claude Code loads the servers and hooks that a manifest, or the
                            # entry of a plugin without one, configures inline, together with
                            # the default file. Cursor loads the MCP servers that a manifest or a
                            # marketplace entry configures inline, and its hooks have no matcher
                            # group for `has_a_grouped_hook` to read.
                            if ((token == "<claude-plugin>"
                                 or token == "<cursor-plugin>" and key == "mcpServers")
                                    and _inline_configuration(value, key, token)):
                                found.append((doc.get("_declared_in", source), heuristic_row, "inline"))
                        if how == "follows" and default is not None:
                            continue
                        if how == "precedes" and (
                                _copilot_hooks(doc[key])
                                or (isinstance(doc[key], str) and values
                                    and _inside_root(base, values[0]) is None)):
                            # Copilot reads valid inline hooks instead of its default files.
                            # A path that leaves the plugin root prevents any hooks file from
                            # loading.
                            keep_default = False
                            continue
                        declared = []
                        for value in values:
                            rel = _inside_root(base, value)
                            # Claude Code's `agents`: "Directories aren't accepted".
                            if rel is not None and (how != "files" or (root / rel).is_file()):
                                declared.extend(_resolve_declared(root, rel, heuristic_row["match_mode"],
                                                                  vendored_dirs,
                                                                  stop_at_skill=token == "<copilot-plugin>"))
                        if token == "<copilot-plugin>" and heuristic_row["mechanism"] in ("hooks", "lsp"):
                            declared = [rel for rel in declared
                                        if _parses(root, root / rel, copilot_mechanism=heuristic_row["mechanism"])]
                        if how == "precedes" and not declared:
                            continue
                        if (replaces or how == "precedes") and key not in retained_defaults:
                            keep_default = False
                        default_places = [f"{prefix}{a}" for a in alternatives]
                        for rel in declared:
                            # The contents of the default place are read with it, unless the
                            # key replaced the default place.
                            if keep_default and any(rel == d or (rel.startswith(d + "/")
                                                    and (heuristic_row["match_mode"] != "skill_dir_flat"
                                                         or rel.count("/") == d.count("/") + 1))
                                                    for d in default_places):
                                continue
                            if heuristic_row["match_mode"] in ("skill_dir", "skill_dir_flat") and rel == (base or "."):
                                root_rule = next(r for r in token_defaults
                                                 if r["match_mode"] == PLUGIN_ROOT_SKILL_MODE)
                                skill = next(p for p in sorted((root / rel).iterdir())
                                             if p.is_file() and p.name.lower() == _SKILL_MARKER.lower())
                                # A declared root skill includes its support directories. Copying
                                # the whole root would collect unrelated source files and dotfiles.
                                found.append((skill.relative_to(root).as_posix(), root_rule,
                                              "root_skill"))
                                found.extend((folder, root_rule, "root_skill_folder")
                                             for folder in _support_dirs(root, prefix,
                                                                         vendored_dirs))
                                continue
                            found.append((rel, heuristic_row, "declared"))
                    if keep_default:
                        found.append((default, heuristic_row, "default"))
                        # A portable part is also a part of each tool that reads the plugin
                        # through its portable manifest (`_PORTABLE_READERS`).
                        for tool in portable_readers.get(base, ()) if token == "<agent-plugin>" else ():
                            row = _PORTABLE_PART_ROWS.get(
                                (tool, heuristic_row["path"], heuristic_row["match_mode"]))
                            if row is not None:
                                found.append((default, row, "default"))
    # A part, a declared place or a folder beside a root skill that a symlink leads out of the
    # clone is not the repository's: the tools would read the machine's files there.
    return [(rel, heuristic_row, kind) for rel, heuristic_row, kind in found if _stays_inside(root, rel)]


def collect_plugin_parts(repo: Path, out: Path, vendored_dirs: set[str],
                         print_matches: bool = False) -> set[str]:
    """Copy each plugin manifest and every part that `plugin_parts` returns.

    The per-tool passes copy the manifest directories and the files beside them but not the
    plugin's parts. Without this function, a repository that keeps a plugin under
    `plugins/<name>/` would publish the manifest and none of the plugin's contents. Returns the
    tools' flags.
    """
    display = dir_name_to_repo(repo.name)
    flags: set[str] = set()
    for rel in plugin_manifests(repo, vendored_dirs):
        if print_matches:
            _log_match(display, repo / rel, repo, "plugin manifest")
        copy_file(repo / rel, out / rel)
    for rel, heuristic_row in plugin_parts(repo, vendored_dirs):
        source = repo / rel
        if print_matches:
            _log_match(display, source, repo, f"plugin part {heuristic_row['path']}")
        if source.is_dir():
            copy_dir(source, out / rel, vendored_dirs)
        else:
            copy_file(source, out / rel)
        if heuristic_row["tool"] != "any":
            flags.add(heuristic_row["tool"])
    return flags


def build_csv_row(repo_name: str, scanned_at: str, branch: str, head_sha: str,
                  head_commit_date: str, first_commit_sha: str, flags: set[str]) -> str:
    """Assemble one CSV row from the repository's metadata and the collected flags.

    `scanned_at` is the wall-clock time at which this repository was collected, and
    `head_commit_date` the committer date of `head_sha`.
    """
    bools = ["TRUE" if col in flags else "FALSE" for col in FLAG_COLUMNS]
    # Through a CSV writer, so that a value that contains a comma is quoted rather than becoming two
    # columns. A value without a comma is written unchanged, and git accepts a comma in a
    # branch name.
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="").writerow(
        [repo_name, scanned_at, branch, head_sha, head_commit_date, first_commit_sha,
         *bools])
    return buffer.getvalue()


_SHARED_DIRS = tuple(spec_dir_paths(_PATTERNS)
                     + paths_for(_PATTERNS, "collect_only", "copy_dir"))
# Files at the root that several tools read and none owns, such as REVIEW.md, which Claude Code
# Review and Copilot code review both read.
_SHARED_ROOT_FILES = [r["path"] for r in _PATTERNS
                      if r["tool"] == "any" and r["match_mode"] == "root_file"]
# Directories that a per-tool pass already copies, which collect_shared skips.
_PER_TOOL_DIRS = frozenset((".claude", ".gemini", ".codex", ".cursor", ".opencode",
                            ".github", ".claude-plugin"))


def collect_shared(repo: Path, out: Path, vendored_dirs: set[str],
                   print_matches: bool = False) -> set[str]:
    """Collect the directories that belong to no single tool.

    `.agents/` is the directory analogue of AGENTS.md, which Codex and Cursor read and which
    symlinked skills directories lead to. It therefore sets no tool flag, as AGENTS.md sets
    none. The per-tool passes collect the per-tool directories, and this function collects
    whatever they leave, including the plugin manifests that each tool copied from Claude.
    """
    display = dir_name_to_repo(repo.name)
    for name in _SHARED_DIRS:
        if name in _PER_TOOL_DIRS:
            continue
        for d in find_dirs_by_name(repo, name, vendored_dirs):
            # A Python package named `_agent`, without a place of Antigravity, is no
            # customization root.
            if not is_spec_directory(d):
                continue
            if print_matches:
                _log_match(display, d, repo, f"{name} directory")
            copy_dir(d, out / d.relative_to(repo), vendored_dirs)
    for rel in _SHARED_ROOT_FILES:
        if (repo / rel).is_file():
            if print_matches:
                _log_match(display, repo / rel, repo, rel)
            copy_file(repo / rel, out / rel)
    return set()


# The match modes of a folder with files as its items, such as rules, agents and commands,
# rather than folders, as skills are. The rules and agents that a front matter test selects are
# among them.
_FILE_ITEM_MODES = ("dir_ext:", "dir_ext_described:", "dir_ext_triggered:", "dir_agent:",
                    "tree_ext:", "tree_ext_described:", "tree_suffix:")
# The rows with one of these modes, under the folder that each names.
_FILE_ITEM_ROWS = {path: tuple(r for r in _PATTERNS
                               if r["path"] == path and r["match_mode"].startswith(_FILE_ITEM_MODES))
                   for path in dict.fromkeys(r["path"] for r in _PATTERNS
                                             if r["match_mode"].startswith(_FILE_ITEM_MODES))}
_FILE_ITEM_PARENTS = frozenset(_FILE_ITEM_ROWS)
# The directories in which the cloning step follows a link: each folder of items that a heuristic
# names and each plugin manifest folder (`link_parents`).
_SYMLINK_PARENTS = tuple(dict.fromkeys(
    r["path"] for r in _PATTERNS
    if r["match_mode"] in ("skill_dir", "skill_dir_flat", "copy_dir")
    or r["path"] in _FILE_ITEM_PARENTS))
# The skills directories, in which each item is a folder with a SKILL.md.
_SKILL_PARENTS = tuple(dict.fromkeys(
    r["path"] for r in _PATTERNS
    if r["match_mode"] in ("skill_dir", "skill_dir_flat") and not plugin_placeholder(r["path"])))


def skill_files(base: Path) -> list[Path]:
    """The SKILL.md, in any letter case, of each folder directly in the skills directory `base`,
    where the folder is no link.

    A skill folder can keep its SKILL.md as a link to a file elsewhere in the repository, as
    feast-dev/feast's `.claude/skills/feast-dev/SKILL.md` links to
    `../../../skills/feast-dev/SKILL.md`, and the tool reads the skill through the link.
    """
    marker = _SKILL_MARKER.lower()
    return [file for entry in sorted(base.iterdir()) if entry.is_dir() and not entry.is_symlink()
            for file in sorted(entry.iterdir()) if file.name.lower() == marker]


def collect_symlink_targets(repo: Path, out: Path, vendored_dirs: set[str],
                            print_matches: bool = False) -> set[str]:
    """Copy the targets inside the repository of symlinked tool directories
    (`tool_directory_links`), such as a `.claude` that links to `.agents`, of symlinked
    mechanism directories, of the links directly in a mechanism directory or a directory of a
    plugin's parts (`link_parents`), such as a rule in `.agent/rules` that links to `AGENTS.md`,
    and of a SKILL.md that is a link in a skill folder (`skill_files`).

    Runs after the per-tool passes, when the marker files are already in place, and adds only
    what they point at. A tool directory that is a link has no marker yet, because no pass
    copies a link to a directory, and this pass writes it. The target of such a link is copied
    as the per-tool pass copies the directory, without the places of the tool's local state
    named from the link (`copy_dir`, `remove_local_state`), because the tool reads and writes
    the directory through the link.
    """
    display = dir_name_to_repo(repo.name)
    for rel, link in _symlink_candidates(repo, vendored_dirs):
        # rsync runs with --safe-links and drops rather than records a symlink that points
        # outside the directory being copied. Writing the marker here keeps the evidence that
        # the directory existed and where it pointed, which attributes the target's
        # configuration artifacts to the tool that linked to them.
        try:
            marker = out / rel
            # rsync --safe-links keeps a link with a target that only looks relative, such as
            # a Windows path, and the marker path can already be a broken symlink.
            # Writing through it would try to create the target that it points at.
            if marker.is_symlink() and not marker.exists():
                marker.unlink()
            if not marker.exists():
                copy_file(link, marker)
                if print_matches:
                    _log_match(display, link, repo, "link that rsync left out")

            read_as = (link.name,) if link.name in TOOL_DIRECTORIES else ()
            copied = copy_symlink_target(link, repo, out, vendored_dirs, read_as)
            if copied and print_matches:
                _log_match(display, repo / copied, repo, f"symlink target of {rel}")
        except OSError as exc:
            # One unreadable link is not a reason to lose the repository. A volume that
            # cannot take the copy is the machine's failure, and a repository copied in part
            # would be recorded as collected.
            if exc.errno in _STORAGE_ERRNOS:
                raise
            logs.warning('repository_cloning', f"Could not follow the link {rel} of {display} "
                                               f"({exc}).")
    # A later copy into a directory that a tool directory links to, through a second such link or
    # the link of a mechanism directory inside it, runs `rsync --delete` with the exclusions of its
    # own link alone and copies back the local state that the copy through the first link left out.
    # The places of the local state, named from every tool directory that links to the directory,
    # are therefore removed after every link is followed.
    names_of = defaultdict(set)
    for link_rel, directory in linked_tool_directory_pairs(repo, vendored_dirs):
        names_of[directory].add(posixpath.basename(link_rel))
    for directory, names in sorted(names_of.items()):
        if (out / directory).is_dir() and not (out / directory).is_symlink():
            remove_local_state(out / directory, sorted(names))
    return set()


# The errors that report that the destination cannot be written, rather than that a link or its
# target cannot be read.
_STORAGE_ERRNOS = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EROFS, errno.EIO})


def _symlink_candidates(repo: Path, vendored_dirs: set[str]) -> list:
    """Return every symlink worth following, as (repo-relative path, link).

    A candidate is a tool directory that is a link (`tool_directory_links`), a mechanism
    directory that is itself a link, or a single entry inside one, because repositories link a
    whole `.claude` or `.claude/skills` at a shared directory and also link one skill or one rule
    at a time. Tool and mechanism directories are looked for at any depth, because a monorepo
    keeps them per package. The directories of a plugin's parts with files as items are among
    them (`link_parents`). A SKILL.md that is a link in a skill folder is a candidate too
    (`skill_files`).

    `vendored_dirs` prunes the search, as it does for the other callers of
    `find_dirs_by_name`, so that no marker or target is collected from inside `node_modules`. The
    collected file cleanup would drop both afterwards, but the step's completion manifest is a
    checksum of the collected tree.
    """
    found = {}
    for link in tool_directory_links(repo, vendored_dirs):
        if link.is_symlink():
            found.setdefault(str(link.relative_to(repo)), link)
    for base, _rows, skills in link_parents(repo, vendored_dirs):
        if base.is_symlink():
            found.setdefault(str(base.relative_to(repo)), base)
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_symlink():
                found.setdefault(str(entry.relative_to(repo)), entry)
        for file in skill_files(base) if skills else ():
            if file.is_symlink():
                found.setdefault(str(file.relative_to(repo)), file)
    return list(found.items())


def tool_directory_links(root: Path, vendored_dirs) -> list[Path]:
    """Every entry of the tree with the name of a tool directory (`TOOL_DIRECTORIES`) that is a
    symlink or a file, at any depth outside `.git`, the vendored directories and the places of
    the tools' local state, sorted.

    In a clone, such an entry is a link, or a file that no tool reads as its directory. In the
    collected tree, the collector has stored every such link as its marker, a file. `os.walk`
    lists a link to a directory among the directories and does not descend into it.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d != ".git" and not is_pruned(Path(dirpath) / d, vendored_dirs, root)]
        base = Path(dirpath)
        found.extend(base / name for name in dirnames + filenames
                     if name in TOOL_DIRECTORIES
                     and (name in filenames or (base / name).is_symlink()))
    return sorted(found)


def linked_tool_directories(root: Path, vendored_dirs) -> dict[str, list[Path]]:
    """{name: [directory, ...]}: for each tool directory of the tree that is a symlink, or the
    collection's marker of one (`tool_directory_links`), the directory of the repository that
    it leads to (`_linked_directory`), under the name of the tool directory.

    A tool reads such a directory through the link as its own: Claude Code reads the
    `.agents/skills` of a repository with `.claude -> .agents` as `.claude/skills`, and Cursor
    reads the `.agents/agents` of a repository with `.cursor -> .agents` as `.cursor/agents`. A
    link that leads out of the repository, to no
    directory, to the repository root, into a `.git` directory, or to a directory that contains
    the link is left out, because the collector copies no such target (`copy_symlink_target`).
    """
    found = {}
    for link, directory in linked_tool_directory_pairs(root, vendored_dirs):
        found.setdefault(posixpath.basename(link), []).append(root / directory)
    return found


def linked_tool_directory_pairs(root: Path, vendored_dirs) -> list[tuple[str, str]]:
    """(link, directory), both repository-relative, for each tool directory of the tree that is
    a symlink, or the collection's marker of one, and the directory that it leads to, without
    the links that `linked_tool_directories` leaves out."""
    found = []
    for link in tool_directory_links(root, vendored_dirs):
        rel = link.relative_to(root).as_posix()
        directory = _linked_directory(root, rel)
        if not directory or in_git_directory(directory):
            continue
        parent = posixpath.dirname(rel)
        if parent == directory or parent.startswith(directory + "/"):
            continue
        found.append((rel, directory))
    return found


def remove_local_state(directory: Path, names) -> None:
    """Remove from `directory` of the collected tree the places of the tools' local state named
    from each of `names`, the names of the tool directories that link to it, such as Claude
    Code's `shell-snapshots/` and `scheduled_tasks.json` in the `.agents` that `.claude` links to.

    The tool writes its local state through the link into the directory. The copy of the
    directory leaves these places out (`copy_dir`), but a pass that copied the directory under
    its own name before, such as `collect_shared` for `.agents`, copied them, and `rsync --delete`
    keeps a file at the receiver that an `--exclude` matches."""
    for dirpath, dirnames, filenames in os.walk(directory):
        kept = []
        for name in dirnames:
            path = Path(dirpath) / name
            if any(local_state(_named_from(directory, path, tool), is_dir=True) is not None
                   for tool in names):
                if path.is_symlink():
                    path.unlink()
                else:
                    remove_tree(path)
            elif not path.is_symlink():
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            if any(local_state(_named_from(directory, path, tool)) is not None for tool in names):
                path.unlink()


def link_parents(root: Path, vendored_dirs) -> list[tuple[Path, tuple, bool]]:
    """(directory, the heuristics rows that read its items as files, whether it is a skills
    directory) for each directory of the tree in which the cloning step follows a link, in the
    order of `_SYMLINK_PARENTS` and then of the plugins' parts. The rows are empty for a
    directory with folders as items alone, such as most skills directories, and for a directory
    that is copied whole.

    A directory of `_SYMLINK_PARENTS` is looked for at any depth, and it can be a link itself,
    which the cloning step follows as well. Where a tool directory is a link, a directory of
    `_SYMLINK_PARENTS` is also looked for in the directory that the link leads to
    (`linked_tool_directories`), at its path there, such as the `skills/` of the `config/claude`
    that `.claude` links to. The cloning step also follows a link in a directory of a plugin's
    parts with files as items, such as an Antigravity plugin's `rules/` or `agents/`, because
    Antigravity CLI 1.4.2 counted a rule and an agent of a plugin that are links to files
    elsewhere in the workspace. The collected file cleanup keeps the file that a marker directly
    in a directory of file items names, where a row of the directory reads the marker as an item
    and the detection step reads the item's text through it
    (`collected_file_cleanup.reads_through_marker`).
    """
    found = {}
    linked = linked_tool_directories(root, vendored_dirs)
    directories = {}  # the directories of each name, listed once
    for rel in _SYMLINK_PARENTS:
        if "*" in rel or plugin_placeholder(rel):
            continue  # a plugin bundle's directories are copied with the bundle
        head, _, tail = rel.partition("/")
        if head not in directories:
            directories[head] = [*find_dirs_by_name(root, head, vendored_dirs),
                                 *linked.get(head, ())]
        for parent in directories[head]:
            base = parent / tail if tail else parent
            if base.is_symlink() or base.is_dir():
                entry = found.setdefault(base, [[], False])
                entry[0].extend(_FILE_ITEM_ROWS.get(rel, ()))
                entry[1] = entry[1] or rel in _SKILL_PARENTS
    for rel, heuristic_row in plugin_parts(root, vendored_dirs):
        base = root / rel
        if (heuristic_row["match_mode"].startswith(_FILE_ITEM_MODES)
                and base.is_dir() and not base.is_symlink()):
            found.setdefault(base, [[], False])[0].append(heuristic_row)
    return [(base, tuple(rows), skills) for base, (rows, skills) in found.items()]


# The workflow files of a repository, kept beside the collection rather than in it. The
# heuristics match some workflow files by name, and a workflow under any other name counts when
# it runs a documented agent action, is a GitHub Agentic Workflow on an engine that a row names, or
# runs a tool's documented headless invocation in its shell. `collect_agent_workflows` copies
# these workflows into the collected tree. Every other workflow is ordinary CI that the cleanup
# deletes. All workflow files are therefore also copied whole into a tree that no step reads, for
# a person to read afterwards, and the published CSVs do not change. The copy is bounded per
# repository, so that a repository with many generated workflows cannot fill the volume.
WORKFLOWS_SUBPATH = ".github/workflows"
WORKFLOW_SUFFIXES = (".yml", ".yaml")
# The suffix of a GitHub Agentic Workflow, the Markdown that `gh aw compile` turns into a
# `.lock.yml`.
AGENTIC_WORKFLOW_SUFFIX = ".md"
MAX_WORKFLOW_FILES = 50
MAX_WORKFLOW_BYTES = 256 * 1024
# The most files that one agentic workflow imports, its imports' imports included, that are
# read for its engine and copied with it. For a workflow that imports more, `agentic_workflow`
# returns no engine.
MAX_WORKFLOW_IMPORTS = 100
# gh-aw resolves an import inside these two folders only, "security: path %s must be within
# .github or .agents folder" (`ResolveIncludePath` in `pkg/parser` of github/gh-aw).
_IMPORT_FOLDERS = (".github/", ".agents/")
# An import embedded in the gh-aw binary rather than read from the repository.
_BUILTIN_IMPORT_PREFIX = "@builtin:"


def _front_matter_lines(text: str) -> list | None:
    """The lines between a Markdown file's opening `---` and the next, or None without both."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    return None if end is None else lines[1:end]


def _uncommented(value: str) -> str:
    """A YAML value without a trailing comment, which starts at a `#` after whitespace."""
    return re.sub(r"(?:^|\s+)#.*$", "", value).strip()


def _key_block(lines: list, key: str) -> tuple | None:
    """The value of `key` at the indentation of the first line that contains a key, and the
    more indented lines below it, or None when no such line names it."""
    level = next((len(line) - len(line.lstrip()) for line in lines
                  if line.strip() and not line.strip().startswith("#")), 0)
    for i, line in enumerate(lines):
        if len(line) - len(line.lstrip()) != level:
            continue
        found = re.match(rf"^\s*{re.escape(key)}\s*:(.*)$", line)
        if not found:
            continue
        block = []
        for nested in lines[i + 1:]:
            if nested.strip() and len(nested) - len(nested.lstrip()) <= level:
                break
            block.append(nested)
        return _uncommented(found.group(1)), block
    return None


def _engine_selection(front: list) -> str | None:
    """The engine that a front matter selects, or None when it selects none.

    gh-aw's compiler counts `engine:` as a selection when it is a string, or a mapping that has
    `id:` or a `runtime:` with an `id:`. A mapping of preferences alone, such as `model:`,
    selects nothing and leaves the engine to the imports or the default
    (`frontmatterSelectsEngine` in `pkg/workflow/compiler_orchestrator_engine.go`).
    """
    found = _key_block(front, "engine")
    if found is None:
        return None
    value, block = found
    if value.startswith("{"):
        inline = re.search(r"\bid\s*:\s*['\"]?([\w.-]+)", value)
        return inline.group(1).lower() if inline else None
    if value.strip("'\""):
        return value.strip("'\"").lower()
    for key in ("id", "runtime"):
        nested = _key_block(block, key)
        if nested is None:
            continue
        if key == "runtime":
            nested = _key_block(nested[1], "id")
            if nested is None:
                continue
        name = nested[0].strip("'\"")
        if re.fullmatch(r"[\w.-]+", name):
            return name.lower()
    return None


def _engine_command(front: list) -> str | None:
    """The `command:` of the `engine:` mapping of a front matter, the program that gh-aw runs
    in place of the engine's own, or None without one."""
    found = _key_block(front, "engine")
    if found is None:
        return None
    value, block = found
    if value.startswith("{"):
        inline = re.search(r"\bcommand\s*:\s*['\"]?([^,'\"}]+)", value)
        return inline.group(1).strip() if inline else None
    nested = _key_block(block, "command")
    return nested[0].strip("'\"") or None if nested else None


def _runs_engine(engine: str, command: str | None) -> bool:
    """Whether a workflow on this engine runs the engine's own program.

    `engine.command` replaces the program: "Override the default engine executable using the
    `command` field. Useful for testing pre-release versions, custom builds, or non-standard
    installations. Engine installation steps are automatically skipped" (gh-aw's engines
    reference, read 2026-10-14). The names of the tools' programs begin with gh-aw's engine ids,
    `claude`, `copilot`, `codex`, `gemini`, `agy`, `cursor-agent` and `opencode`. A command with a
    file name that begins with the id is a build of that program, such as
    `/usr/local/bin/copilot-dev` in the reference. Any other command runs another program, as
    pydantic/pydantic-ai's `/tmp/gh-aw/bin/pydantic-ai-runner-launch` runs pydantic-ai's own
    agent runner.
    """
    return command is None or posixpath.basename(command.split()[0]).lower().startswith(engine)


def _list_items(lines: list) -> list:
    """The items of the YAML block list in these lines: each string item, and the `path:` or
    `uses:` of each mapping item, the two forms gh-aw reads (`parseImportSpecsFromArray`)."""
    items, level, key_column, pending = [], None, None, False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if level is None:
            if not stripped.startswith("-"):
                return items
            level = indent
        if indent < level:
            break
        if indent == level and stripped.startswith("-"):
            rest = stripped[1:].lstrip()
            key_column = indent + len(stripped) - len(rest)
            pending = False
            found = re.match(r"^(?:path|uses)\s*:(.*)$", rest)
            if found:
                items.append(_uncommented(found.group(1)).strip("'\""))
            elif rest and not re.match(r"^[\w-]+\s*:", rest):
                items.append(_uncommented(rest).strip("'\""))
            else:
                pending = True
        elif pending and indent == key_column:
            found = re.match(r"^(?:path|uses)\s*:(.*)$", stripped)
            if found:
                items.append(_uncommented(found.group(1)).strip("'\""))
                pending = False
    return [item for item in items if item]


def _import_specs(front: list) -> list:
    """The paths that a front matter's `imports:` names: its list, inline or as a block, or the
    `aw:` list of `imports:` as a mapping, the one key of it that gh-aw reads as workflow
    imports (`frontmatterDeclaresImports`)."""
    found = _key_block(front, "imports")
    if found is None:
        return []
    value, block = found
    if not value:
        listed = _list_items(block)
        if listed or not block:
            return listed
        found = _key_block(block, "aw")
        if found is None:
            return []
        value, block = found
        if not value:
            return _list_items(block)
    if value.startswith("[") and value.endswith("]"):
        return [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
    return []


def _is_remote_import(path: str) -> bool:
    """Whether gh-aw fetches this import from another repository, as `IsWorkflowSpec` in
    `pkg/parser` decides: three or more parts before any `#` and `@`, the first not `.`, `/`
    or `shared/`, or a URL."""
    clean = path.split("#", 1)[0].split("@", 1)[0]
    parts = clean.split("/")
    if len(parts) < 3:
        return False
    if "://" in clean:
        return True
    if clean.startswith((".", "shared/", "/")):
        return False
    return bool(parts[0] and parts[1])


def _resolve_import(spec: str, importer: str, workflow_dir: str) -> tuple | None:
    """The repository-relative path that an import names and whether it is optional, or None
    when the repository does not contain it: a remote or built-in import, or a path outside the
    two folders that gh-aw reads imports from.

    A trailing `?` makes an import optional and `#Section` selects a section of the file. A
    path under `.github/` or starting with `/` is read from the repository root, and any other
    from the workflow's directory, except that an import of an imported file without a `/`,
    or starting with `./`, is read from that file's directory (`determineNestedBaseDir`).
    """
    path = spec.strip()
    optional = path.endswith("?")
    path = path.rstrip("?").split("#", 1)[0].strip()
    if not path or path.startswith(_BUILTIN_IMPORT_PREFIX) or _is_remote_import(path):
        return None
    if path.startswith(".github/"):
        rel = path
    elif path.startswith("/"):
        rel = path[1:]
    elif importer != workflow_dir and ("/" not in path or path.startswith("./")):
        rel = posixpath.join(posixpath.dirname(importer), path)
    else:
        rel = posixpath.join(workflow_dir, path)
    rel = posixpath.normpath(rel)
    return (rel, optional) if rel.startswith(_IMPORT_FOLDERS) else None


# A macro of the Markdown body that gh-aw replaces with the content of a file when the workflow
# runs, `{{#runtime-import path}}` or `{{#runtime-import? path}}` for a file that may be missing,
# and the deprecated `{{#import path}}`, `{{#import: path}}`, `{{#import? path}}` and
# `{{#import?: path}}`, which gh-aw rewrites to the first form. The patterns are those of
# `processRuntimeImports` in `actions/setup/js/runtime_import.cjs` of github/gh-aw a454a6b30e,
# read 2026-10-31.
_RUNTIME_IMPORT = re.compile(r"\{\{#runtime-import\??[ \t]+([^}]+?)\}\}")
_BODY_IMPORT = re.compile(r"\{\{#import\??(?:[ \t]+|[ \t]*:[ \t]*)([^{}]+?)\}\}")
# A code span on one line, in which gh-aw leaves a `{{#import}}` as it is.
_CODE_SPAN = re.compile(r"`[^`\n]+`")
# The line range that may follow the path of a runtime import, `path:45-52`.
_LINE_RANGE = re.compile(r"(.+?):\d+-\d+")


def _runtime_import_specs(text: str) -> list:
    """The paths named by the runtime imports of a Markdown file, in the order of the file.

    gh-aw reads them anywhere in the body, a fenced code block included, after it removes the
    front matter and the HTML comments (`processRuntimeImport`). Only a `{{#import}}` in a code
    span on one line stays as it is.
    """
    front = _front_matter_lines(text)
    body = "\n".join(text.splitlines()[len(front) + 2:]) if front is not None else text
    previous = None
    while body != previous:
        previous, body = body, re.sub(r"<!--[\s\S]*?-->", "", body)
    spans = _CODE_SPAN.sub(lambda found: " " * len(found.group()), body)
    found = [(m.start(), m.group(1).strip()) for m in _RUNTIME_IMPORT.finditer(body)]
    found += [(m.start(), m.group(1).strip()) for m in _BODY_IMPORT.finditer(spans)]
    return [spec for _position, spec in sorted(found)]


def _resolve_runtime_import(spec: str) -> str | None:
    """The repository-relative path of the file that a runtime import names, or None for a URL
    or a path outside the two folders that gh-aw reads it from.

    A path under `.github/` or `.agents/` is read from the repository root, also after a
    leading `/`, and any other path from `.github/workflows/`, and the path has to stay inside
    its folder (`resolveRuntimeImportFilePath` in `runtime_import.cjs`). The templating page of
    gh-aw states `.github/` as the folder of every path, but gh-aw has read a path that starts
    with neither `.github/` nor `.agents/` from `.github/workflows/` since its commit 532ca33448
    of 2026-02-11, and the compiled workflows of the collection name the file there.
    """
    path = spec.strip()
    ranged = _LINE_RANGE.fullmatch(path)
    if ranged:
        path = ranged.group(1)
    if re.match(r"https?://", path, re.I):
        return None
    if path.startswith("/"):
        path = path.lstrip("/")
        if not path.startswith(_IMPORT_FOLDERS):
            return None
    folder = next((f for f in _IMPORT_FOLDERS if path.startswith(f)), None)
    if folder is None:
        folder, path = ".github/", posixpath.join(WORKFLOWS_SUBPATH, path)
    rel = posixpath.normpath(path)
    return rel if rel.startswith(folder) else None


def agentic_workflow(text: str, read=None) -> tuple:
    """The engine that a GitHub Agentic Workflow runs and the repository files that it imports,
    or (None, []) for a file that is not one.

    GitHub Agentic Workflows keeps a workflow as Markdown in `.github/workflows/`, a YAML front
    matter above the prompt, and `gh aw compile` writes the `.lock.yml` that Actions runs. A
    file is one when its front matter has `on:`, which a README beside the workflows does
    not. The engine is the workflow's own `engine:`. Where the workflow names none, the engine
    is the engine that its imports select, its imports' imports included, because the compiler
    takes "the first real included engine specification" when the workflow has none
    (`validateSingleEngineSpecification` in `pkg/workflow/engine_validation.go`). Where neither
    names one, the engine is Copilot: "Copilot CLI is the default, so `engine:` can be omitted
    when using Copilot." The engine is None where the repository's files cannot decide it: a
    required import that the repository does not contain or that is not one of its files, such
    as a remote import, imports that select two engines, which gh-aw refuses to compile, or
    more than `MAX_WORKFLOW_IMPORTS` of them. It is None as well where the engine's `command:`
    runs another program than the engine's (`_runs_engine`).

    The imported files also include the files named by the runtime imports in the body of the
    workflow and in the bodies of its imports, and in turn the files named by the runtime imports
    of those files (`_runtime_import_specs`), because gh-aw puts their text into the prompt when
    the workflow runs. They do not take part in the choice of the engine, because gh-aw removes
    the front matter of a file that it reads this way. A runtime import of a file that the
    repository does not contain is left out, also where it is required, because it cannot change
    the engine. The list holds at most `MAX_WORKFLOW_IMPORTS` files.

    `read` takes a repository-relative path and returns the file's text, or None when the
    repository contains no such file. Without `read`, no import is read. The front matter is
    read by lines and not parsed as YAML.
    """
    front = _front_matter_lines(text)
    if front is None or not any(re.match(r"""^["']?on["']?\s*:""", line) for line in front):
        return None, []
    own, own_command = _engine_selection(front), _engine_command(front)
    workflow_dir = WORKFLOWS_SUBPATH
    queue = [(spec, workflow_dir) for spec in _import_specs(front)]
    imported, selected, commands, answerable = [], [], [], True
    texts = [text]
    while queue:
        spec, importer = queue.pop(0)
        resolved = _resolve_import(spec, importer, workflow_dir)
        if resolved is None:
            answerable = False
            continue
        rel, optional = resolved
        if rel in imported:
            continue
        imported_text = read(rel) if read is not None else None
        if imported_text is None:
            answerable = answerable and optional
            continue
        if len(imported) == MAX_WORKFLOW_IMPORTS:
            answerable = False
            break
        imported.append(rel)
        texts.append(imported_text)
        nested = _front_matter_lines(imported_text) or []
        engine = _engine_selection(nested)
        if engine and engine not in selected:
            selected.append(engine)
            commands.append(_engine_command(nested))
        queue.extend((nested_spec, rel) for nested_spec in _import_specs(nested))
    while texts and read is not None:
        for spec in _runtime_import_specs(texts.pop(0)):
            rel = _resolve_runtime_import(spec)
            if rel is None or rel in imported or len(imported) >= MAX_WORKFLOW_IMPORTS:
                continue
            injected = read(rel)
            if injected is not None:
                imported.append(rel)
                texts.append(injected)
    if own:
        engine, command = own, own_command
    elif not answerable or len(selected) > 1:
        return None, imported
    else:
        engine = selected[0] if selected else "copilot"
        command = own_command or (commands[0] if commands else None)
    return (engine if _runs_engine(engine, command) else None), imported


def agentic_workflow_engine(text: str, read=None) -> str | None:
    """The engine of a GitHub Agentic Workflow, the first element that `agentic_workflow`
    returns."""
    return agentic_workflow(text, read)[0]


def repository_reader(repo: Path):
    """A `read` for `agentic_workflow` over a repository on disk: the text of a regular file
    inside it within `MAX_WORKFLOW_BYTES`, and None for anything else, including a link."""
    root = repo.resolve()

    def read(rel: str) -> str | None:
        path = repo / rel
        try:
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                return None
            if path.stat().st_size > MAX_WORKFLOW_BYTES:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return read


# A compiled GitHub Agentic Workflow. `gh aw compile` writes it from the Markdown beside it,
# which is the configuration. The compiled file runs the engine's command-line tool itself.
COMPILED_WORKFLOW_SUFFIX = ".lock.yml"
# Where a shell command starts: a line of a `run:` block, a `run:` value, or the text after a
# separator or a command substitution, and after the reserved words of the shell that a command
# can follow (POSIX Shell Command Language 2.4 and 2.9.2: `if`, `then`, `elif`, `else`, `do`,
# `while`, `until` and the `!` of a pipeline) or `timeout <duration>`, which runs the command
# that follows it. `npx` runs a package's command at any position on a line. `sudo`, `env`, an
# assignment, `bunx` and `pnpm dlx` are not read as prefixes, and `docs/heuristics.md` names the
# workflows of the collection that ground both lists.
_COMMAND_START = (r"(?:(?:^[ \t]*(?:-[ \t]*)?(?:run[ \t]*:[ \t]*[|>]?[-+]?[ \t]*)?"
                  r"|[;&|(`][ \t]*|\$\([ \t]*)"
                  r"(?:(?:if|then|elif|else|do|while|until|!)[ \t]+)*"
                  r"(?:timeout(?:[ \t]+-\S+)*[ \t]+\d+(?:\.\d+)?[smhd]?[ \t]+)?"
                  r"|\bnpx[ \t]+(?:(?:-y|--yes)[ \t]+)?)")


def runs_command(text: str, invocation: str) -> bool:
    """Whether a workflow's shell runs this command, rather than naming it.

    The first word of `invocation` has to be at a position where a shell command starts, and
    each further word, a flag such as `-p` or a subcommand such as `exec`, has to follow it on
    the same line. A package name as the first word, `@anthropic-ai/claude-code`, is what `npx`
    runs, with or without a version. A comment line is not read, and a workflow that mentions
    `claude -p` in a comment does not run it. Neither does a workflow that echoes it
    in the middle of a line.
    """
    first, *rest = invocation.split()
    word = re.escape(first) + (r"(?:@[\w.^~-]+)?" if first.startswith("@") else "")
    after = "".join(rf"(?=[^\n]*?(?<![\w@./-]){re.escape(token)}(?![\w./-]))" for token in rest)
    pattern = re.compile(_COMMAND_START + word + r"(?![\w./@-])" + after, re.M)
    code = "\n".join("" if line.lstrip().startswith("#") else line for line in text.split("\n"))
    return pattern.search(code) is not None


def workflow_tool(entry: Path, actions, engines, commands=None) -> str | None:
    """The tool that a workflow file runs, or None for a file that runs none of them.

    A YAML workflow runs a tool when a `uses:` line names its action or a shell step runs one of
    its documented invocations, and a GitHub Agentic Workflow when its engine is that tool's.
    A compiled `.lock.yml` is read for its actions only, because the Markdown that it was
    compiled from is the configuration and the compiled file runs the engine's command itself.
    The collector calls this function for every file in `.github/workflows/`, the collected file
    cleanup for each file that it is about to judge, and the detection step for each file that
    it reports, so that the three cannot disagree about which workflow is an agent's. A link, a file
    too large to be a workflow, and a file that cannot be read run no tool. An agentic
    workflow's imports are read in the repository that contains `entry`, three directories up.
    """
    if entry.is_symlink() or not entry.is_file():
        return None
    suffix = entry.suffix.lower()
    if suffix not in WORKFLOW_SUFFIXES + (AGENTIC_WORKFLOW_SUFFIX,):
        return None
    try:
        if entry.stat().st_size > MAX_WORKFLOW_BYTES:
            return None
        text = entry.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if suffix == AGENTIC_WORKFLOW_SUFFIX:
        engine = agentic_workflow_engine(text, repository_reader(entry.parents[2]))
        return engines.get(engine) if engine else None
    tool = next((tool for action, tool in actions.items() if uses_action(text, action)), None)
    if tool or entry.name.lower().endswith(COMPILED_WORKFLOW_SUFFIX):
        return tool
    return next((tool for tool, invocations in (commands or {}).items()
                 if any(runs_command(text, invocation) for invocation in invocations)), None)


def agent_workflow_files(repo: Path, actions, engines=None, commands=None) -> list:
    """Return the workflow files that run an agent: a `uses:` naming one of these GitHub
    Actions, a shell step running one of these commands, or a GitHub Agentic Workflow on one
    of these engines.

    A workflow that runs an agent is that tool's configuration wherever the repository put it,
    and the file name is not the evidence, because the Gemini action's example workflows each
    use it under a name that the repository may change. The vendor publishes the action
    reference, which survives a rename, and this function reads that reference.

    The whole file is read, because a `uses:` line can be under any job and the files are
    bounded at `MAX_WORKFLOW_BYTES` anyway. A file that cannot be read is not a match.
    """
    source = repo / WORKFLOWS_SUBPATH
    engines = engines or {}
    if not source.is_dir() or not (actions or engines or commands):
        return []
    return [entry for entry in sorted(source.iterdir())
            if workflow_tool(entry, actions, engines, commands)]


def agentic_workflow_imports(repo: Path, engines) -> list:
    """The repository-relative paths of the files that the GitHub Agentic Workflows on these
    engines import, each once, in the order in which the workflows and their imports name them.

    An imported file is a part of the workflow that `gh aw compile` merges into it, such as its
    tools, MCP servers and prompt, and it can select the workflow's engine. A file that a
    runtime import of the body names, such as a policy that a Claude workflow loads into its
    prompt, is a part of the prompt when the workflow runs. The collector therefore copies both
    with the workflow, the cleanup keeps them, and the detection counts them as the workflow's
    rather than as files of their own.
    """
    source = repo / WORKFLOWS_SUBPATH
    if not source.is_dir():
        return []
    read = repository_reader(repo)
    found = []
    for entry in sorted(source.iterdir()):
        if entry.suffix.lower() != AGENTIC_WORKFLOW_SUFFIX or not workflow_tool(entry, {}, engines):
            continue
        _, imported = agentic_workflow(read(f"{WORKFLOWS_SUBPATH}/{entry.name}") or "", read)
        found.extend(rel for rel in imported if rel not in found)
    return found


# The input of an agent action that names a file of the repository as the prompt, by the action:
# "prompt-file: Path to file that contains the prompt to pass to Codex" (action.yml of
# openai/codex-action c05cfd74ad95, read 2026-10-21), and "prompt_file: Path to a file containing
# the prompt to send to Claude Code" in the action.yml of anthropics/claude-code-base-action
# 33edbb44fb53 and of the folder `base-action` of anthropics/claude-code-action d535f5172dec (read
# 2026-11-01). The action.yml of anthropics/claude-code-action itself has no such input. Each
# action reads the file with the step's working directory, the checkout of the repository, as
# the base of a relative path. A `workflow_uses:` row has to name the action or the repository of
# its folder (`uses_action`), so that the collector copies the workflow that names the file.
_PROMPT_FILE_INPUTS = {"openai/codex-action": "prompt-file",
                       "anthropics/claude-code-base-action": "prompt_file",
                       "anthropics/claude-code-action/base-action": "prompt_file"}
# The expression that names the checkout of the repository in a path, `${{ github.workspace }}`.
_WORKSPACE_PREFIX = re.compile(r"^\$\{\{\s*github\.workspace\s*\}\}/")


def _step_lines(lines: list, index: int) -> list:
    """The lines of the workflow step that contains the line at `index`: from the line that
    starts the step's list item to the last line indented deeper than its dash."""
    start = next((i for i in range(index, -1, -1)
                  if re.match(r"^\s*-\s", lines[i] + " ")
                  and len(lines[i]) - len(lines[i].lstrip()) <= len(lines[index]) - len(lines[index].lstrip())),
                 None)
    if start is None:
        return []
    dash = len(lines[start]) - len(lines[start].lstrip())
    end = start + 1
    while end < len(lines) and (not lines[end].strip()
                                or len(lines[end]) - len(lines[end].lstrip()) > dash):
        end += 1
    return lines[start:end]


def action_prompt_files(repo: Path) -> list:
    """The repository-relative paths of the prompt files that the agent actions of the
    workflows read, each once, in the order of the workflows and their steps.

    A step that runs an action of `_PROMPT_FILE_INPUTS` names its prompt file in that action's
    input, such as `prompt-file: .github/codex/prompts/review.md` for the Codex action or
    `prompt_file: claude_md/ci_recovery_prompt.md` for the Claude Code Base Action, and its
    `uses:` line names the action, also through a YAML anchor that a later step's alias reuses.
    The prompt is what the agent runs with, and the file is a part of the workflow that names it,
    as the imports of a GitHub Agentic Workflow are. The collector copies it with the workflow, the
    cleanup keeps it, and the detection counts it as the workflow's. A path that starts with
    `${{ github.workspace }}/` is read relative to the repository. A path with any other
    expression, an absolute path and a path that leaves the repository name no file of it. The
    file has to be a regular file of the repository (`repository_reader`).
    """
    source = repo / WORKFLOWS_SUBPATH
    if not source.is_dir():
        return []
    actions = [action for action in _PROMPT_FILE_INPUTS
               if any(action == row or action.startswith(row + "/") for row in _WORKFLOW_ACTIONS)]
    if not actions:
        return []
    read = repository_reader(repo)
    found = []
    for entry in sorted(source.iterdir()):
        if entry.suffix.lower() not in WORKFLOW_SUFFIXES:
            continue
        text = read(f"{WORKFLOWS_SUBPATH}/{entry.name}")
        if not text:
            continue
        lines = text.splitlines()
        anchors = {}
        for index, line in enumerate(lines):
            uses = re.match(r"^\s*(?:-\s*)?uses\s*:\s*(?:&(\S+)\s+)?(.*)$", line)
            if uses is None:
                continue
            anchor, value = uses.group(1), _uncommented(uses.group(2))
            if value.startswith("*"):
                # An alias names the action of the anchor that an earlier `uses:` defines.
                action = anchors.get(value[1:])
            else:
                action = next((a for a in actions
                               if re.match(rf"['\"]?{re.escape(a)}(?:@|['\"]|$)", value)), None)
                if anchor and action:
                    anchors[anchor] = action
            if action is None:
                continue
            key = _PROMPT_FILE_INPUTS[action]
            for step_line in _step_lines(lines, index):
                value = re.match(rf"^\s*{re.escape(key)}\s*:(.*)$", step_line)
                if value is None:
                    continue
                path = _WORKSPACE_PREFIX.sub("", _uncommented(value.group(1)).strip("'\""))
                if not path or "${{" in path or path.startswith(("/", "|", ">")):
                    continue
                rel = posixpath.normpath(path)
                if rel == ".." or rel.startswith("../") or rel in found:
                    continue
                if read(rel) is not None:
                    found.append(rel)
    return found


def uses_action(text: str, action: str) -> bool:
    """Whether a workflow runs this action, rather than merely naming it.

    `uses:` makes a step run an action, and the identifier has to be on that line. A repository
    that names the action in a comment, or in the prose of a workflow that documents how to
    install it, does not run it. The quoting is the vendors' own, `uses: 'owner/repo'`
    in the Gemini examples and bare in Claude Code's. A YAML anchor can come before the
    identifier, as in `uses: &codex-action-pin openai/codex-action@52fe01ec`, so that other steps
    name the same action by its alias.
    """
    return re.search(rf"^\s*(?:-\s*)?uses\s*:\s*(?:&\S+\s+)?['\"]?{re.escape(action)}", text,
                     re.M) is not None


def copy_workflow_files(repo: Path, out: Path) -> int:
    """Copy a clone's `.github/workflows/*.yml` into `out`. Returns how many were copied.

    The function copies only the files directly in that directory with those two suffixes, and
    no symlink, because a link here would point into the clone that the cloning step deletes.
    Nothing is written when the repository has no workflows, and an empty directory
    never appears.
    """
    source = repo / WORKFLOWS_SUBPATH
    if not source.is_dir():
        return 0
    copied = 0
    for entry in sorted(source.iterdir()):
        if copied >= MAX_WORKFLOW_FILES:
            break
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.suffix.lower() not in WORKFLOW_SUFFIXES:
            continue
        try:
            if entry.stat().st_size > MAX_WORKFLOW_BYTES:
                continue
            out.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry, out / entry.name)
        except OSError:
            continue
        copied += 1
    return copied


def collect_agent_workflows(repo: Path, out: Path, print_matches: bool = False) -> set[str]:
    """Copy each workflow that runs an agent, and flag its tool: one that runs a documented
    agent action, one with a shell step that runs a tool's documented headless invocation, and
    a GitHub Agentic Workflow on an engine that a row names.

    The other passes copy a directory or a name. This pass copies a file for its content, as
    `json_hooks` and `toml_hooks` detect a file by its content, because the vendors publish an
    action identifier, a command or an engine and not a file name. The Gemini action's example
    workflows, for example, each use it under a name that the repository may change. `.github`
    itself is copied only when a Copilot path is already there. Without this pass, a Claude
    workflow in a repository with no Copilot configuration would reach no tree at all.

    The files that an agentic workflow imports are copied with it, because its engine can come
    from one of them, and the detection reads them in the collected tree. The prompt files that
    an agent action of a workflow reads (`action_prompt_files`), the prompt that the agent runs
    with, are copied with the workflow too.
    """
    flags: set[str] = set()
    actions, engines, commands = _WORKFLOW_ACTIONS, _WORKFLOW_ENGINES, _WORKFLOW_COMMANDS
    if not (actions or engines or commands):
        return flags
    display = dir_name_to_repo(repo.name)
    for entry in agent_workflow_files(repo, actions, engines, commands):
        relative = entry.relative_to(repo)
        if print_matches:
            _log_match(display, entry, repo, "agent workflow")
        target = out / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        # The tool is the tool that the file runs, by the test that selected the file, and not a
        # substring of the file. A Claude workflow that names openai/codex-action in a comment
        # is not Codex's, and the detector does not read it as Codex's either.
        tool = workflow_tool(entry, actions, engines, commands)
        if tool:
            flags.add(tool)
    for rel in agentic_workflow_imports(repo, engines):
        if print_matches:
            _log_match(display, repo / rel, repo, "import of a GitHub Agentic Workflow")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, target)
    for rel in action_prompt_files(repo):
        if print_matches:
            _log_match(display, repo / rel, repo, "prompt file of an agent action")
        target = out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repo / rel, target)
    return flags


_WORKFLOW_ACTIONS = workflow_actions(_PATTERNS)
_WORKFLOW_ENGINES = workflow_engines(_PATTERNS)
_WORKFLOW_COMMANDS = workflow_commands(_PATTERNS)


def collect_repo(repo: Path, out: Path, vendored_dirs: set[str],
                 print_matches: bool = False) -> set[str]:
    """Run every collection pass over a cloned repository, tighten the flags and return them.

    The last pass copies the planning documents (`planning.collect_planning_documents`), which
    set no flag, and only when an earlier pass copied a file. A planning document keeps no
    repository, and the cleanup drops a repository without configuration whatever planning
    documents it has. Copying them would only make the cloning step fetch the history and read
    the history of every file before the cleanup drops the repository. `planning` is imported
    here because it imports this module."""
    from agentcontext import planning
    flags: set[str] = set()
    declaring: set[Path] = set()
    waiting_links: list = []
    token = _DECLARING_FILES.set(declaring)
    links_token = _DIRECTORY_LINKS.set(waiting_links)
    try:
        flags |= collect_skill_md(repo, out, print_matches)
        flags |= collect_agent_plugins(repo, out, vendored_dirs, print_matches)
        flags |= collect_claude(repo, out, vendored_dirs, print_matches)
        flags |= collect_gemini(repo, out, vendored_dirs, print_matches)
        flags |= collect_codex(repo, out, vendored_dirs, print_matches)
        flags |= collect_copilot(repo, out, vendored_dirs, print_matches)
        flags |= collect_nested_places(repo, out, vendored_dirs, print_matches)
        flags |= collect_cursor(repo, out, vendored_dirs, print_matches)
        flags |= collect_opencode(repo, out, vendored_dirs, print_matches)
        flags |= collect_declared_context(repo, out, vendored_dirs, print_matches)
        flags |= collect_declared_subagents(repo, out, vendored_dirs, print_matches)
        flags |= collect_declared_rules(repo, out, vendored_dirs, print_matches)
        flags |= collect_plugin_parts(repo, out, vendored_dirs, print_matches)
        flags |= collect_shared(repo, out, vendored_dirs, print_matches)
        flags |= collect_agent_workflows(repo, out, print_matches)
        flags |= collect_symlink_targets(repo, out, vendored_dirs, print_matches)
        # The markers are written before the planning documents, which are copied at their own
        # paths and never through a link to a directory, so that
        # `planning.collect_planning_documents` keeps a marker as a file that an earlier pass
        # copied, as it keeps the marker of a link to a file.
        write_directory_link_markers(waiting_links)
        if out.is_dir() and tree_files(out):
            flags |= planning.collect_planning_documents(repo, out, vendored_dirs, print_matches)
    finally:
        _DECLARING_FILES.reset(token)
        _DIRECTORY_LINKS.reset(links_token)
    store_declaring_files(repo, out, declaring)
    tighten_flags(flags)
    return flags


def store_declaring_files(repo: Path, out: Path, declaring) -> None:
    """Store each of the `declaring` files that is a symlink inside the repository with the
    content that the collector read through the link, in place of the link's marker.

    `declaring` are the files with content that decided what the passes copied, which
    `_repository_text` read: a registration file, a settings file that names a context file, a
    marketplace, a plugin manifest, a portable plugin document, and a registered rule or agent
    with its front matter. The collected file cleanup and the detection step decide with the
    same content. Over a marker's one line, they would select nothing of what the file
    declares, and the cleanup would then delete what the collector copied for it. A link that
    the collector did not read through stays a marker (`copy_file`): a link that leads out of
    the repository or into its `.git` directory, a link to a file that is not UTF-8 where the
    reader requires it, and every other link.
    """
    real_repo = repo.resolve()
    for path in sorted(declaring):
        try:
            rel = Path(path).relative_to(repo)
        except ValueError:
            continue
        link, dest = repo / rel, out / rel
        if not link.is_symlink() or dest.is_symlink() or not dest.is_file():
            continue
        try:
            real = link.resolve()
            if real.is_relative_to(real_repo) and stat.S_ISREG(real.stat().st_mode):
                shutil.copy2(real, dest)
        except (OSError, RuntimeError) as exc:
            # A volume that cannot take the copy is the machine's failure, and a repository
            # copied in part would be recorded as collected.
            if isinstance(exc, OSError) and exc.errno in _STORAGE_ERRNOS:
                raise
            logs.warning('repository_cloning',
                         f"Could not read {rel.as_posix()} of {dir_name_to_repo(repo.name)} "
                         f"through its link ({exc}).")


def tighten_flags(flags: set[str]) -> None:
    """Add the tool flag that a file flag implies: claude.md implies claude, gemini.md gemini
    and cursorrules cursor."""
    if "claude.md" in flags:
        flags.add("claude")
    if "gemini.md" in flags:
        flags.add("gemini")
    if "cursorrules" in flags:
        flags.add("cursor")
def undecodable_name(path: str) -> bool:
    """Whether a path from the file system contains bytes that are not UTF-8.

    Python represents such bytes as lone surrogates. `os.walk` and `rglob` yield them without
    complaint, and no UTF-8 file accepts them: the JSON of `finished_repositories.jsonl` escapes
    them, and a cache writer that meets one raises at the end of the run and again on every
    resume.
    """
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def printable_name(path: str) -> str:
    """A path from the file system, with each byte that is not UTF-8 escaped as `\\xNN`.

    Escaping the string instead escapes the lone surrogate that Python stores the byte as,
    which reports `caf\\xe9.md` as `caf\\udce9.md`, a name that no file on the disk has.
    """
    return os.fsencode(path).decode("utf-8", "backslashreplace")


def remove_undecodable_names(root: Path) -> list[str]:
    """Delete every entry under `root` with a path that is not UTF-8, deepest first.

    Returns the paths removed, relative to `root`, with the offending bytes escaped so that the list
    can be logged. `.github/` is copied whole for every Copilot path, and one Latin-1 name anywhere
    under it reaches the tree. No CSV of this pipeline can contain the name of such a
    file, and every later step reads the tree without it.
    """
    found = [path for path in root.rglob("*") if undecodable_name(str(path))]
    removed = []
    for path in sorted(found, key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed.append(printable_name(str(path.relative_to(root))))
    return removed
