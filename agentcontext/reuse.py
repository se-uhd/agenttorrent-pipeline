"""Which repositories of an earlier collection the cloning step takes over without a clone.

Each line that the cloning step writes to the list of finished repositories records what the
repository was collected under, `collected_under`: the fingerprint of the collector's code
(`collector_fingerprint`), the SHA-256 of the path heuristics as the collector reads them, the
records that the run kept beside the collection, such as the commit logs and the file lists, and
the version of git that read the histories (`clone.git_version`). A later run takes over the
repository of a line when the kept records and git are the same, the collector's code is the same
or differs only in changes that do not reach the repository (`collector_reach`), the path
heuristics are the same or differ only in rows that no path of the repository's file list
matches (`affected`), and the repository's default branch is still at the commit that the line
records (`clone.remote_head`). Every other repository is collected again, including the
repository of a line without `collected_under`, which an earlier collector wrote. The same test
of the path heuristics, `heuristics_reach`, decides whether a retry with a failed clone may keep
the earlier collection.
While git returns no head commit for more repositories than the step's ceiling allows, the head
check waits and queries those heads again (`remote_heads`).

A change to the commit heuristics needs no clone, because the commits of a repository that is
taken over are classified again from the commit log that its collection kept (`reclassify`). The
files of the repositories that left the input list are removed, and their lines marked to redo
(`remove_left`).
"""

import concurrent.futures
import functools
import hashlib
import json
import lzma
import multiprocessing
import os
import posixpath
import re
import time
from collections import Counter
from pathlib import Path

from agentcontext import (caches, code_versions, commits, guards, heuristics, imports, logs, paths,
                          planning, resume)
from agentcontext.fingerprints import PACKAGE_ROOT, code_fingerprint, module_closure
from agentcontext.git import clone
from agentcontext.repos import dir_name_to_repo, repo_name_to_dir, split

COMPONENT = "repository_cloning"
# The module that decides what a repository's collection contains. Its module closure is the
# collector's code.
COLLECTOR_MODULE = "agentcontext.collector"
# The modules that decide which repositories a run collects, in which order and when it stops,
# but not what a repository's collection contains. They stay outside the collector's code, so
# that a fix to one of them during a run does not make the next run collect the sample again.
# The cloning step's module is one of them. The collector imports others, such as `guards` for
# the defaults of `CollectionConfig`.
SCHEDULING_MODULES = frozenset({"guards.py", "logs.py", "resume.py", "reuse.py", "fingerprints.py",
                                "steps/repository_cloning.py"})
# The `__init__.py` files of the packages `agentcontext` and `agentcontext.git`, which the closure
# contains because every import of a module of a package runs them. `__init__.py` names the data
# root and the directories of the stages, and `git/__init__.py` holds the documentation of the git
# modules. Neither decides which files a collection copies or what its records contain. The
# collector receives every directory that it writes in `CollectionConfig`. The heuristics
# directory that `heuristics.py` derives from `REPO_ROOT` is also the directory of the path
# heuristics that `current` records.
PACKAGE_FILES = frozenset({"__init__.py", "git/__init__.py"})
# The module of the imports. Its code, with what only it imports, is recorded with the import
# heuristics (`import_heuristics`) rather than with the collector's code, so that a change to it
# collects again only the repositories with a file that can import (`imports_reach`).
IMPORTS_MODULE = "agentcontext.imports"
# The columns of `config_path_patterns.csv` that decide what the collector copies. The other
# columns record where a row comes from and when its page was read, and the weekly
# documentation watch changes them without changing what is collected.
COLLECTION_COLUMNS = ("tool", "mechanism", "path", "match_mode", "content_format")
# Every version of the path heuristics that a repository was collected under, named by its
# SHA-256, in the stage directory, so that a later run can determine which rows changed.
HEURISTICS_DIR_NAME = "collection_heuristics"
# How many heads the step queries at a time. Each query is a network round trip
# (docs/steps/repository_cloning.md gives its duration), and querying the heads of a whole
# sample one at a time would take many times as long.
HEAD_CHECK_THREADS = 32
# How many times the step queries a head before it records that git returned no head commit for the
# repository. The first query sends the GitHub access token with its first request. GitHub limits
# anonymous git requests over HTTPS more than authenticated ones and may refuse them during unusual
# traffic, and the step queries the head of every repository that it would take over, many at a
# time, from one address. Every further query is anonymous, as a clone is, and sends the GitHub
# access token only after GitHub responds with 401. An organization with an IP allow list refuses an
# authenticated request from an address outside the list, also for its public repositories, and a
# GitHub access token can expire after the pre-checks verified it. In both cases GitHub
# returns the head to an anonymous query of a public repository (docs/steps/repository_cloning.md).
HEAD_CHECK_TRIES = 2
# How long the step waits, in seconds, before each round in which it queries again the heads for
# which git returned no head commit, while they are more than the step's ceiling allows
# (`remote_heads`). GROUNDING: a policy. The first wait is the pause that `github_api` gives a
# rate limit for which GitHub names no wait, and each wait doubles the one before. Together they
# last about an hour, many times as long as the bursts of HTTP 400 that
# docs/steps/repository_cloning.md describes. At or below the ceiling, the step does not wait,
# and its retry rounds query those heads again.
HEAD_WAITS = (60.0, 120.0, 240.0, 480.0, 960.0, 1920.0)
# The fewest threads with which a round after a wait queries heads. Each round halves the
# threads of the round before, so that the step sends its queries at a lower rate in case
# GitHub limits the rate. GROUNDING: the GitHub query of the sampling stage runs with four
# requests in flight (docs/refresh.md).
HEAD_WAIT_THREADS = 4


def path_heuristics(rows: list, vendored, planning_rows=(), local_state=()) -> dict:
    """The path heuristics as the collector reads them. `rows` holds the columns of each row
    that decide what the collector copies, in file order, and `vendored` the names of the
    vendored directories. `planning` holds the pattern, the match mode and the anchor of each
    heuristic of the planning documents (`planning_document_patterns.csv`), which decide which
    files the collector copies whatever their order. `local_state` holds the places of the
    tools' local state (`local_state_paths.csv`), which the collector leaves out."""
    return {"rows": [[row[column] for column in COLLECTION_COLUMNS] for row in rows],
            "vendored": sorted(vendored),
            "planning": sorted({(row["pattern"], row["match_mode"], row["anchor"])
                                for row in planning_rows}),
            "local_state": sorted(set(local_state))}


def current_path_heuristics(vendored) -> dict:
    """This run's path heuristics as the collector reads them (`path_heuristics`): the rows of
    `config_path_patterns.csv`, the vendored directories `vendored`, the heuristics of the
    planning documents and the places of the tools' local state. `current`, `outdated` and the
    cloning step's `preserved_on` compare a line with these, so that the decision to take a
    repository over and the decision to keep its earlier collection after a failed retry read
    the same heuristics."""
    return path_heuristics(heuristics.load_config_path_patterns(), vendored,
                           planning.load_planning_document_patterns(),
                           heuristics.LOCAL_STATE_PATHS)


def heuristics_sha(kept: dict) -> str:
    """The SHA-256 of path heuristics in their canonical JSON."""
    text = json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def keep_heuristics(directory: Path | None, kept: dict) -> str:
    """Write `kept` to `directory` under its SHA-256, unless it is there, and return the SHA-256."""
    sha = heuristics_sha(kept)
    if directory is not None and not (directory / f"{sha}.json").is_file():
        directory.mkdir(parents=True, exist_ok=True)
        caches.replace_file(directory / f"{sha}.json", json.dumps(kept, sort_keys=True) + "\n")
    return sha


def read_heuristics(directory: Path | None, sha: str) -> dict | None:
    """The path heuristics kept under `sha`, or None where none are kept or where the content of
    the file does not have the SHA-256 that its name gives."""
    if directory is None or not sha:
        return None
    try:
        kept = json.loads((directory / f"{sha}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return kept if isinstance(kept, dict) and heuristics_sha(kept) == sha else None


def records(config) -> dict:
    """Which records the run keeps beside the collection. A repository taken over must have them."""
    return {"commit_history": config.commits_dir is not None, "history": config.measure_git,
            "file_list": config.file_lists_dir is not None,
            "workflows": config.workflows_dir is not None,
            "imported": config.imported_dir is not None}


def import_heuristics(rows: list, root: Path = PACKAGE_ROOT) -> dict:
    """The import heuristics as the collector reads them: the columns of each row of
    `import_patterns.csv` that decide what it copies to the imported tree, in file order, and
    the fingerprint of the code that reads them (`import_code_fingerprint`)."""
    return {"imports": [[row[column] for column in imports.COLLECTION_COLUMNS] for row in rows],
            "code": import_code_fingerprint(root)}


def collector_files(root: Path = PACKAGE_ROOT) -> list:
    """The files of the collector's code: the module closure of `COLLECTOR_MODULE` in the
    package at `root`, without `SCHEDULING_MODULES`, without `PACKAGE_FILES` and without the
    code of the imports (`import_files`)."""
    return [path for path in module_closure(COLLECTOR_MODULE, root, stop={IMPORTS_MODULE})
            if path.relative_to(root).as_posix() not in SCHEDULING_MODULES | PACKAGE_FILES]


def import_files(root: Path = PACKAGE_ROOT) -> list:
    """The files of the code of the imports: the module closure of `IMPORTS_MODULE` in the
    package at `root`, without the collector's code, `SCHEDULING_MODULES` and `PACKAGE_FILES`.
    These files are `imports.py` and `prune.py`, which holds the paths that the archive step
    prunes and the copy of the imports leaves out. The test of those paths is in a module of its
    own rather than in the archive step, so that an edit to the archive step or to the command
    line that it imports collects nothing again."""
    collector = set(collector_files(root))
    return [path for path in module_closure(IMPORTS_MODULE, root)
            if path not in collector
            and path.relative_to(root).as_posix() not in SCHEDULING_MODULES | PACKAGE_FILES]


def collector_fingerprint(root: Path = PACKAGE_ROOT) -> str:
    """The fingerprint of the collector's code in the package at `root`: its files
    (`collector_files`) without their docstrings, log statements and exception texts
    (`fingerprints.code_fingerprint`). Rewording a message therefore collects nothing again.
    `code_versions.COLLECTOR` records every fingerprint that a collection was made under."""
    return code_fingerprint(collector_files(root), exception_texts=False, root=root)


def import_code_fingerprint(root: Path = PACKAGE_ROOT) -> str:
    """The fingerprint of the code of the imports in the package at `root` (`import_files`),
    taken as `collector_fingerprint` takes the collector's. `code_versions.IMPORTS` records
    every fingerprint that a collection was made under."""
    return code_fingerprint(import_files(root), exception_texts=False, root=root)


def current(config) -> dict:
    """What a repository that this run collects is collected under. The path heuristics are
    kept under the run's heuristics directory.

    git is part of it because `created_at`, `#commits` and `first_commit_sha` come from its
    `git log --follow`, which follows a renamed path differently from one version to the next
    (`docs/git.md` gives an example). A line taken over from another version of git would put
    the results of two versions into one cache.
    """
    return {"collector": collector_fingerprint(),
            "heuristics": keep_heuristics(config.heuristics_dir,
                                          current_path_heuristics(config.vendored_dirs)),
            "imports": keep_heuristics(config.heuristics_dir,
                                       import_heuristics(imports.load_import_patterns())),
            "records": records(config), "git": clone.git_version()}


# Where the components of a path that `_glob` matches start and end in a file list, one path
# per line: at the start of a line or after a `/`, and before a `/` or at the end of a line.
_GLOB_START, _GLOB_END = "(?:^|/)", "(?:/|$)"


def _glob_components(path: str) -> str:
    """A heuristic row's path as a regular expression over the components of a path: a
    placeholder such as `<claude-plugin>` and a `*` stand for any one component's name or part
    of one."""
    parts = []
    for part in (p for p in path.split("/") if p):
        if part.startswith("<") and part.endswith(">"):
            parts.append("[^/\n]*")
        else:
            parts.append("".join("[^/\n]*" if c == "*" else "[^/\n]" if c == "?" else re.escape(c)
                                 for c in part))
    return "/".join(parts)


def _glob(path: str) -> str:
    """A heuristic row's path as a regular expression over a file list's path, matching the
    path's components anywhere in it (`_glob_components`)."""
    return _GLOB_START + _glob_components(path) + _GLOB_END


def _reaches(globs: list, text: str) -> bool:
    """Whether any of the regular expressions of `_glob` matches a path of `text`, one per line."""
    return bool(globs) and re.search("|".join(globs), text, re.MULTILINE | re.IGNORECASE) is not None


# The lines of a run were collected under few versions of the path heuristics, and a run
# compares few sets of changed heuristics of the planning documents.
@functools.lru_cache(maxsize=8)
def _compiled_planning_heuristics(planned: tuple) -> tuple:
    """The heuristics of the planning documents `planned`, sorted `(pattern, match mode,
    anchor)` triples, compiled for `_planning_reaches` as `(regex, by_directory, linked)`.
    `regex` is a regular expression over a file list, one path per line, for the markers and the
    settings files, or None where no heuristic has a marker or a setting. `linked` is a regular
    expression for an entry with the name of a tool directory that a heuristic's pattern, marker
    or settings file begins with, such as `.claude` for `.claude/settings.json`, or None.
    Where such an entry is a link, `planning.find` reads the directory that it leads to through
    the link, and a file list does not record where a link leads. `by_directory` holds
    `(start, at_root, match)` for each other heuristic under the first directory that every
    path that it matches contains, or under None where its pattern names no directory.

    `match` matches a path as the matching does (`planning.find`). Every path that it matches,
    with a `/` put before it, contains `start`, a `/` and the text that every match begins with
    (`planning.leading_literal`). `start` is at the beginning for a heuristic of the root
    (`at_root`) and anywhere for a heuristic of any depth. The regular expression of the
    markers matches where `_glob` of any of them matches. It tests the start and the end that
    the globs share once rather than once per marker, which makes the search of a long file
    list many times faster."""
    markers, by_directory, heads = set(), {}, set()
    for pattern, mode, *_anchor in planned:
        if mode.startswith(planning.WITH_MODE):
            named = mode[len(planning.WITH_MODE):].split("|")
            markers.update(named)
        elif mode.startswith(planning.SETTING_MODE):
            named = [mode[len(planning.SETTING_MODE):].split(":")[0]]
            markers.update(named)
        else:
            at_root = mode == planning.ROOT_MODE
            match = (re.compile(pattern).fullmatch if at_root
                     else re.compile(r"(?:^|(?<=/))(?:" + pattern + r")\Z").search)
            literal = planning.leading_literal(pattern)
            by_directory.setdefault(literal.split("/")[0] if "/" in literal else None,
                                    []).append(("/" + literal, at_root, match))
            named = [literal]
        heads.update(name.split("/")[0] for name in named if "/" in name)
    names = "|".join(sorted({_glob_components(marker) for marker in markers}))
    regex = (re.compile(f"{_GLOB_START}(?:{names}){_GLOB_END}", re.MULTILINE | re.IGNORECASE)
             if markers else None)
    tools = sorted(heads & set(paths.TOOL_DIRECTORIES))
    linked = (re.compile(rf"(?:^|/)(?:{'|'.join(map(re.escape, tools))})$",
                         re.MULTILINE | re.IGNORECASE) if tools else None)
    return regex, by_directory, linked


def _planning_reaches(planned, paths: list, text: str) -> bool:
    """Whether a heuristic of the planning documents of `planned`, `(pattern, match mode,
    anchor)` triples, can copy a file of a repository with the file list `paths`. `text` is the
    same list, one path per line. A heuristic with a marker or a setting reaches the repository
    when the marker or the settings file is anywhere in the list, in any case. Any other
    heuristic reaches it when its pattern matches a path from the root or from any directory,
    as the matching reads it. A heuristic with a pattern, a marker or a settings file that
    begins with a tool directory, such as `.claude/settings.json`, also reaches a repository
    with an entry of that name at any depth, in any case, which can be a link to a directory
    that the matching reads through the link. The test leaves the anchor of the heuristic aside
    and therefore reaches more repositories than the collector copies from.

    The markers and the settings files of every heuristic are one search of `text`, and so are
    the entries with the name of a tool directory. Each path
    is read once and matched against the heuristics that name no directory and the heuristics
    of the directories that it contains, as `planning.find` selects them. Each heuristic is
    tried only where the path contains the text that every match of the heuristic begins with
    (`_compiled_planning_heuristics`). The file list is therefore read once however many
    heuristics changed."""
    markers, by_directory, linked = _compiled_planning_heuristics(tuple(sorted(planned)))
    if markers is not None and markers.search(text):
        return True
    if linked is not None and linked.search(text):
        return True
    if not by_directory:
        return False
    for path in paths:
        slashed = "/" + path
        for directory in (None, *path.split("/")[:-1]):
            for start, at_root, match in by_directory.get(directory, ()):
                if ((slashed.startswith(start) if at_root else start in slashed)
                        and match(path)):
                    return True
    return False


def _plugin_row(row) -> bool:
    """A row of a plugin's part, which the plugin's manifest can name anywhere: a placeholder in
    its path, or a file beside a plugin's root."""
    return "<" in row[2] or row[3].split(":")[0] == "plugin_sibling"


def _manifest_row(row) -> bool:
    """A row of the manifest that identifies a plugin, such as `<copilot-plugin>/plugin.json`: a
    row of the mechanism `plugins` that names one file under its placeholder."""
    head, _, name = row[2].partition("/")
    return (row[1] == "plugins" and head.startswith("<") and head.endswith(">") and name != ""
            and not any(c in name for c in "*?[|/"))


def _links_reach(heads, text: str) -> bool:
    """Whether the file list `text`, one path per line, has an entry at any depth, in any case,
    with the name of a tool directory of `heads`. Where such an entry is a link, the cloning
    step reads the directory that it leads to through the link
    (`paths.linked_tool_directories`), and a file list does not record where a link leads."""
    tools = sorted(set(heads) & set(paths.TOOL_DIRECTORIES))
    return bool(tools) and re.search(rf"(?:^|/)(?:{'|'.join(map(re.escape, tools))})$", text,
                                     re.MULTILINE | re.IGNORECASE) is not None


def affected(entries: list, old: dict, new: dict) -> bool:
    """Whether the path heuristics `old`, which a repository was collected under, and `new` can
    collect different files from it, by its file list `entries`, `(mode, path)` pairs.

    A row that changed reaches a repository when a path of the file list contains the row's path
    anywhere in it, in any case. That is more than the collector copies, because a file named at
    the root is reached in every directory. A row that names a file that declares other paths,
    such as a settings file, a registration file or a workflow, reaches the repository that
    contains that file. A plugin's parts can be anywhere that its manifest names. A row of a
    plugin therefore reaches every repository that the path of any other row reaches, because a
    plugin's manifest is at such a path. A row of the manifest itself (`_manifest_row`) decides
    whether a file of its name is a plugin, and the plugin's parts follow from that file. Such a
    row reaches a repository with a file of the manifest's name. A row of the parts of an
    Antigravity plugin reaches a repository with a `plugin.json`, which every Antigravity plugin
    has. A vendored directory that changed reaches a repository with a directory of that name, and
    a changed place of the tools' local state reaches a repository with a path in it. A changed
    row, place or vendored directory that begins with a tool directory, such as `.claude/commands`,
    also reaches a repository with an entry of that name at any depth, which can be a link that the
    collector reads the row through (`_links_reach`). The same rows in another order reach every
    repository. A heuristic of the planning documents that changed reaches a repository with a path
    that its pattern matches, or with its marker or its settings file (`_planning_reaches`).
    """
    old_rows, new_rows = Counter(map(tuple, old["rows"])), Counter(map(tuple, new["rows"]))
    changed = list((old_rows - new_rows) + (new_rows - old_rows))
    if not changed and old["rows"] != new["rows"]:
        return True
    text = "\n".join(path for _mode, path in entries)
    vendored = set(old["vendored"]) ^ set(new["vendored"])
    if _reaches([_glob(name) for name in sorted(vendored)], text):
        return True
    # A kept version without a "local_state" key left no place out, and each of the current
    # places is a change.
    local = set(old.get("local_state", ())) ^ set(new.get("local_state", ()))
    if _reaches([_glob(place) for place in sorted(local)], text):
        return True
    heads = ({row[2].split("/")[0] for row in changed} | {place.split("/")[0] for place in local}
             | {name.split("/")[0] for name in vendored})
    if _links_reach(heads, text):
        return True
    # A kept version without a "planning" key has no heuristics of the planning documents, and
    # each of the current heuristics is a change.
    planned = ({tuple(p) for p in old.get("planning", ())}
               ^ {tuple(p) for p in new.get("planning", ())})
    if planned and _planning_reaches(planned, [path for _mode, path in entries], text):
        return True
    globs = [_glob(row[2]) for row in changed if not _plugin_row(row)]
    globs += [_glob(row[2].partition("/")[2]) for row in changed if _manifest_row(row)]
    parts = [row for row in changed if _plugin_row(row) and not _manifest_row(row)]
    # Every Antigravity plugin has a plugin.json, at a default place or where a plugins.json
    # registers it (`_plugin_import_reaches`), so a row of its parts reaches a repository with
    # such a file.
    globs += [_glob("plugin.json") for row in parts
              if row[2].partition("/")[0] == ANTIGRAVITY_PLUGIN]
    if any(row[2].partition("/")[0] != ANTIGRAVITY_PLUGIN for row in parts):
        globs += [_glob(row[2]) for row in {*old_rows, *new_rows} if not _plugin_row(row)]
    return _reaches(sorted(set(globs)), text)


def file_list(config, repo: str) -> list | None:
    """The repository's file list at its collected commit, or None where none was kept."""
    if config.file_lists_dir is None:
        return None
    path = config.file_lists_dir / f"{repo_name_to_dir(repo)}{clone.FILE_LIST_SUFFIX}"
    # `lzma.LZMAError` is none of the other three, and a truncated, empty or garbled list would
    # raise it out of `outdated`, before any clone, at every Start.
    try:
        return clone.read_file_list(path)
    except (OSError, ValueError, EOFError, lzma.LZMAError):
        return None


def recorded_path_commits(config, repo: str, head_sha: str = "") -> list | None:
    """The commits of each copied path that the per-repository commit record of `repo` keeps
    (`path_commits`), for a change of the collector with a reach over the record
    (`code_versions.RecordReach`).

    The list is empty where the collection copied no file, because such a collection keeps no
    record and has no path with a history that a change could alter. It is None, and the change
    reaches the repository, where a collection that copied a file has no record or no commit log
    beside it, where the record cannot be read or names another commit than `head_sha`, the
    collected commit of the line, and where the record does not show that the commit history was
    read completely. The function reads the record and not the commit log, and it lists the
    collected tree only for a repository without a record."""
    commits_dir = config.commits_dir
    record_path = commits.shard_paths(commits_dir, repo)[1] if commits_dir is not None else None
    if record_path is None or not record_path.is_file():
        return None if _copied_a_file(config, repo) else []
    if not commits.stored_log_path(commits_dir, repo).is_file():
        return None
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None
    if (not isinstance(record, dict) or record.get("outcome") != "history_read"
            or record.get("history_failed") or (head_sha and record.get("head_sha") != head_sha)):
        return None
    entries = record.get("path_commits")
    return entries if isinstance(entries, list) else None


def kept_workflows(config, repo: str) -> dict | None:
    """The text of each workflow file that the collection of `repo` kept beside the collected
    tree (`paths.copy_workflow_files`), by its file name, for a change of the collector with a
    reach over the workflows (`code_versions.WorkflowReach`).

    The dictionary is empty where the file list lists no workflow file. It is None, and the
    change reaches the repository, where the run keeps no workflow files or no file list is
    kept, and where a workflow file that the file list lists as a file rather than a link is not
    kept or cannot be read. The cloning step leaves out a workflow file above
    `paths.MAX_WORKFLOW_BYTES`, which `paths.action_prompt_files` does not read either, and every
    workflow file after the first `paths.MAX_WORKFLOW_FILES`, which `paths.action_prompt_files`
    reads. The file list does not show which of the two limits left a workflow file out, and a
    listed workflow file that was not kept therefore makes the result None."""
    if config.workflows_dir is None:
        return None
    entries = file_list(config, repo)
    if entries is None:
        return None
    prefix = paths.WORKFLOWS_SUBPATH + "/"
    listed = {path[len(prefix):] for mode, path in entries
              if mode != code_versions.LINK_MODE and path.startswith(prefix)
              and "/" not in path[len(prefix):]
              and posixpath.splitext(path)[1].lower() in paths.WORKFLOW_SUFFIXES}
    kept = {}
    directory = config.workflows_dir / repo_name_to_dir(repo)
    for name in sorted(listed):
        try:
            kept[name] = (directory / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return kept


def collector_reach(config, repo: str, under: dict, key: dict, head_sha: str = "") -> bool:
    """Whether a change of the collector's code between the version that the line of `repo`
    records in `under`, `collected_under`, and this run's version in `key` (`current`) can alter
    the collection of `repo` (`code_versions.COLLECTOR`). True where either version is not
    recorded, where a change declares its reach by a predicate over the file list and the
    repository has no file list, where a change declares its reach by a predicate over the
    per-repository commit record and the record cannot show the commits of the paths
    (`recorded_path_commits`), and where a change declares its reach by a predicate over the
    workflows and a workflow is not kept (`kept_workflows`). The file list, the record and the
    workflows are read only for such changes."""
    if under.get("collector") == key["collector"]:
        return False
    return code_versions.reached(
        code_versions.changes(code_versions.COLLECTOR, under.get("collector"), key["collector"]),
        lambda: file_list(config, repo), lambda: recorded_path_commits(config, repo, head_sha),
        lambda: kept_workflows(config, repo))


# The decisions of `heuristics_reach` in this process, by `_reach_key`: whether the import
# heuristics reach the repository, under "imports", and whether the path heuristics do, under
# "paths", each once it is decided.
_REACHED: dict = {}


def _reach_key(config, repo: str, under: dict, key: dict, new: dict) -> bytes:
    """A digest of what `heuristics_reach` reads for `repo`: the line's `collected_under`
    `under`, this run's key `key` and path heuristics `new`, the directories of the kept
    heuristics and of the file lists, the bytes of the repository's file list, and whether its
    collection copied a file (`_copied_a_file`), which `imports_reach` reads. The kept
    heuristics are named by the SHA-256 of their content, which `under` and `key` record."""
    path = (config.file_lists_dir / f"{repo_name_to_dir(repo)}{clone.FILE_LIST_SUFFIX}"
            if config.file_lists_dir is not None else None)
    try:
        listed = hashlib.sha256(path.read_bytes()).hexdigest() if path is not None else None
    except OSError:
        listed = None
    inputs = [repo, under, key, heuristics_sha(new), str(config.heuristics_dir),
              str(config.file_lists_dir), listed, _copied_a_file(config, repo)]
    text = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).digest()


def heuristics_reach(config, repo: str, under: dict | None, key: dict, new: dict,
                     imports_too: bool = True) -> bool:
    """Whether this run's path heuristics, `key` (`current`) with the rows `new`
    (`path_heuristics`), can collect other files from `repo` than the path heuristics that its
    line records in `under`, `collected_under`. With `imports_too`, the function also tests
    whether the import heuristics can copy other imported files (`imports_reach`).

    True where the two differ in a row that reaches a path of the repository's file list
    (`affected`), and where that cannot be decided: the line records no path heuristics, the
    heuristics that it names are not kept, or the repository has no file list.

    Where the line records other heuristics than `key`, the decisions for the import
    heuristics and for the path heuristics are kept for the rest of the process under a digest
    of what they depend on (`_reach_key`), so that the retry rounds of the cloning step and its
    test of whether a failed retry keeps the earlier collection do not read and match the file
    list again. A line or a file list that changes during the run has another digest.
    """
    if not under or not under.get("heuristics"):
        return True
    if under["heuristics"] == key["heuristics"] and (
            not imports_too or under.get("imports") == key.get("imports")):
        return imports_too and imports_reach(config, repo, under, key)
    known = _REACHED.setdefault(_reach_key(config, repo, under, key, new), {})
    if imports_too:
        if "imports" not in known:
            known["imports"] = imports_reach(config, repo, under, key)
        if known["imports"]:
            return True
    if under["heuristics"] == key["heuristics"]:
        return False
    if "paths" not in known:
        old = read_heuristics(config.heuristics_dir, under["heuristics"])
        entries = file_list(config, repo) if old is not None else None
        known["paths"] = old is None or entries is None or affected(entries, old, new)
    return known["paths"]


# The placeholder of an import row with the parts of an Antigravity plugin as its importing
# files, such as `<antigravity-plugin>/rules`.
ANTIGRAVITY_PLUGIN = "<antigravity-plugin>"


def _copied_a_file(config, repo: str) -> bool:
    """Whether the collection of `repo` copied a file to its directory of the collected tree,
    among which the cloning step looks for the importing files (`imports.collect_imports`)."""
    tree = config.collected_dir / repo_name_to_dir(repo)
    return tree.is_dir() and bool(paths.tree_files(tree))


def _plugin_import_reaches(path: str, listed: list) -> bool:
    """Whether an import row with a placeholder in its path `path` can name an importing file of
    a repository with the file list `listed`, its paths in lower case.

    A row of `ANTIGRAVITY_PLUGIN`, such as `<antigravity-plugin>/rules`, reaches a repository
    with a path at the row's place in a directory with a `plugin.json`, or above a
    `.antigravity-plugin` or `.gemini-plugin` folder with one (`paths.antigravity_plugin_dir`).
    Every plugin that `paths.plugin_roots` finds for the placeholder has one, because
    `paths.plugin_roots` finds a plugin of a default place, such as `.agents/plugins/<name>`, by
    the manifest, and a plugin that a plugins.json registers by the `plugin.json` that the
    registration selects. A row of any
    other placeholder reaches a repository where a path contains the row's path anywhere, as
    `affected` reads a path pattern."""
    token, _, rest = path.partition("/")
    if token != ANTIGRAVITY_PLUGIN:
        return _reaches([_glob(path)], "\n".join(listed))
    manifests = [p for p in listed if posixpath.basename(p) == "plugin.json"]
    bases = ({posixpath.dirname(p) for p in manifests}
             | {paths.antigravity_plugin_dir(p) for p in manifests})
    places = {posixpath.join(base, rest.lower()) for base in bases}
    below = tuple(f"{place}/" for place in places)
    return any(p in places or p.startswith(below) for p in listed)


def imports_reach(config, repo: str, under: dict, key: dict) -> bool:
    """Whether this run's import heuristics, which `key` records, can copy other imported files
    from `repo` than the import heuristics that its line records in `under`.

    A row that changed reaches a repository where a path of its file list contains the row's
    importing file anywhere, in any case, as `affected` reads a path pattern. A row that names
    its importing files in a settings file, `declared_names:`, reaches every repository with that
    settings file. A row of the parts of an Antigravity plugin reaches a repository where such a
    path is in the directory of a plugin's manifest (`_plugin_import_reaches`), rather than every
    repository with a folder of the row's name, such as any `rules` folder. A change to the
    code of the imports that reaches the repository (`code_versions.IMPORTS`) counts as a change
    of every row, and so does code of a version that the record does not name. A change that
    does not reach it leaves the rows to decide. No row reaches a repository from which
    the collection copied no file, because the importing files are among the copied files. True
    also where that cannot be decided: the line records no import heuristics, as a line
    collected without the imported tree does, the heuristics that it names are not kept, or the
    repository has no file list.
    """
    if under.get("imports") == key.get("imports"):
        return False
    if not _copied_a_file(config, repo):
        return False
    old = read_heuristics(config.heuristics_dir, under.get("imports") or "")
    new = read_heuristics(config.heuristics_dir, key.get("imports") or "")
    entries = file_list(config, repo) if old is not None and new is not None else None
    if entries is None:
        return True
    old_rows, new_rows = Counter(map(tuple, old["imports"])), Counter(map(tuple, new["imports"]))
    if old.get("code") != new.get("code") and code_versions.reached(
            code_versions.changes(code_versions.IMPORTS, old.get("code"), new.get("code")),
            lambda: entries):
        changed = list(old_rows | new_rows)
    else:
        changed = list((old_rows - new_rows) + (new_rows - old_rows))
        if not changed:
            return old["imports"] != new["imports"]
    path_column = imports.COLLECTION_COLUMNS.index("path")
    listed = [path for _mode, path in entries]
    changed_paths = {row[path_column] for row in changed}
    if _reaches(sorted({_glob(path) for path in changed_paths if not path.startswith("<")}),
                "\n".join(listed)):
        return True
    placeholders = sorted(path for path in changed_paths if path.startswith("<"))
    lowered = [path.lower() for path in listed] if placeholders else []
    return any(_plugin_import_reaches(path, lowered) for path in placeholders)


def _head(job: tuple) -> tuple:
    """The repository and what `clone.remote_head` returns for it, from a query that sends the
    GitHub access token and, while git returns no head commit, from anonymous ones."""
    repo, token, use_ssh = job
    response = None
    for attempt in range(HEAD_CHECK_TRIES):
        response = clone.remote_head(repo, token, use_ssh, authenticate=attempt == 0)
        if response is not None:
            break
    return repo, response


def _query_heads(config, repos: list, threads: int) -> dict:
    """{repo: what `clone.remote_head` returns} for each of `repos`, queried `threads` at a
    time."""
    responses, pace = {}, logs.Pace()
    with concurrent.futures.ThreadPoolExecutor(max(1, threads)) as pool:
        jobs = [(repo, config.token, config.use_ssh) for repo in repos]
        for number, (repo, response) in enumerate(pool.map(_head, jobs), 1):
            responses[repo] = response
            if pace.due():
                logs.info(COMPONENT, f"Queried GitHub for the head commits of {number:,} of "
                                     f"{len(repos):,} repositories.")
    return responses


def remote_heads(config, repos: list, threads: int = HEAD_CHECK_THREADS,
                 ceiling: int = 0, waits: tuple = ()) -> dict:
    """{repo: what `clone.remote_head` returns} for each of `repos`, queried `threads` at a
    time: (branch, head commit), the reason of GitHub's refusal, or None when git returned no
    head commit.

    While git returned no head commit for more of the heads than `ceiling`, the step's ceiling,
    the function waits each of `waits` in turn and queries those heads again. Each round has
    half the threads of the round before and at least `HEAD_WAIT_THREADS`. Each such head counts
    toward the ceiling. Without the waits, a burst of GitHub's errors or a short network failure
    during the check would make the step exit 4 unless its retry rounds got a response for those
    heads, and those rounds query the heads with all threads again a few minutes apart.

    A round first queries as many heads as it has threads, spread over the heads. When git
    returns no head commit for any of them, the round queries none of the others and the next
    wait begins, so that a network that is down costs a few queries per round rather than one
    per head. At or below the ceiling, or with a ceiling of 0, the function does not wait. A few
    heads that fail every time then cost no wait, and the retry rounds of the step query them
    again.
    """
    responses = _query_heads(config, repos, threads)
    missing = [repo for repo in repos if responses[repo] is None]
    for round_, wait in enumerate(waits if ceiling else (), 1):
        if len(missing) <= ceiling:
            break
        round_threads = min(max(1, threads), max(HEAD_WAIT_THREADS, threads >> round_))
        logs.info(COMPONENT, f"No head commit from git for {len(missing):,} of "
                             f"{len(repos):,} repositories, more than the {ceiling:,} that "
                             f"--max-clone-failures allows to retry. Querying them again in "
                             f"{wait:g} s with {round_threads:,} threads (round {round_} of "
                             f"{len(waits)}).")
        time.sleep(wait)
        # Spread over the heads and shifted by one each round, so that heads that fail every
        # time, such as the heads of one owner, which sort together, are not the whole sample.
        spacing = max(1, len(missing) // round_threads)
        sample = missing[(round_ - 1) % spacing::spacing][:round_threads]
        first = _query_heads(config, sample, round_threads)
        responses.update(first)
        if all(response is None for response in first.values()):
            logs.info(COMPONENT, f"In round {round_}, git returned no head commit for any of "
                                 f"{len(first):,} repositories spread over the "
                                 f"{len(missing):,} without one. Waiting again without querying "
                                 f"the other {len(missing) - len(first):,}.")
            continue
        rest = [repo for repo in missing if repo not in first]
        responses.update(_query_heads(config, rest, round_threads))
        missing = [repo for repo in missing if responses[repo] is None]
    if ceiling and waits and len(missing) > ceiling:
        logs.warning(COMPONENT, f"No head commit from git for {len(missing):,} repositories, "
                                f"such as {missing[0]}, after {len(waits):,} rounds of waiting. "
                                f"Kept their earlier collection. The retry rounds query their "
                                f"heads again.")
    return responses


# What `outdated` records for each head that it queried.
SAME, MOVED, REFUSED, NO_RESPONSE = "same", "moved", "refused", "no_response"


def outdated(config, repos: list, done: dict, skip: set, key: dict, check_heads: bool = True,
             threads: int = HEAD_CHECK_THREADS, queried: dict | None = None,
             collection: dict | None = None, head_waits: tuple = ()) -> set:
    """The repositories of `repos` with a line in `done`, the list of finished repositories, that
    this run collects again rather than takes over, besides `skip`, which the functions of
    `resume` select to collect again anyway. `key` is what this run collects under (`current`).
    Without `check_heads`, the step queries only the heads for which git returned no head
    commit before, which is what a retry round after the run's own check needs. `queried`
    receives the state of each queried head of an ok line: `SAME`, `MOVED`, `REFUSED` or
    `NO_RESPONSE`. `collection` is the collection in progress (`resume.open_collection`).
    `head_waits` are the waits of `remote_heads` while git returned no head commit for more
    repositories than the step's ceiling allows, none by default and `HEAD_WAITS` in the step.

    A clone that the size bound or the time bound stopped is left to `resume.clone_needs_redo`,
    and so is an ok line that kept the earlier collection while the time bound that stopped its
    retry holds (`resume.time_bound_holds`), unless the path heuristics changed in a row that
    reaches the repository's files (`heuristics_reach`). A refusal by GitHub is taken over while
    git still returns no head commit for the repository. A repository for which git returned no
    head commit is taken over, and the retry rounds and the next run query its head again. If
    such repositories were collected again, a network outage of a few minutes would cost a clone
    of each of them, and the line written before each clone would mark the repository for the
    next run to clone again whatever its head.

    The head of a line that the collection in progress wrote, or of a line that it took over
    after querying the head, is not queried again. The collection is a snapshot of the heads
    that it collected or confirmed. A resumed run that queried them again would clone every
    repository with a head that moved since then, which a crash should not cost. A line
    marked `head_no_response` is queried whatever collection it names. The function adds every
    repository that it takes over after a head query to the collection's `taken_over`.
    """
    new = current_path_heuristics(config.vendored_dirs)
    again, heads, reasons = set(), {}, Counter()
    current = collection["collection"] if collection else None
    confirmed = collection["taken_over"] if collection else set()
    unqueried = 0
    for repo in repos:
        if repo not in done or repo in skip:
            continue
        main_row, fail_row, under = done[repo][0], done[repo][1], done[repo][6]
        outcome = resume.outcome_of(fail_row)
        if outcome in (clone.SIZE_BOUND_EXCEEDED, clone.TIME_BOUND_EXCEEDED):
            continue
        if resume.time_bound_holds(done[repo][9], config.clone_timeout, done[repo][10], current):
            # An ok line that kept the earlier collection after the time bound stopped its retry
            # in this collection. Whatever else made the step retry it, a clone under the same
            # bound would be stopped again, and the collection therefore keeps the earlier
            # collection as it is. Path heuristics that changed in a row that reaches its files
            # are the exception. Taken over, the collection would publish the files of the
            # earlier heuristics, and a retry keeps nothing of it (`preserved_on` in the cloning
            # step). The import heuristics are no exception, as for `preserved_on`, which keeps
            # the earlier collection after the time bound whatever import heuristics it records.
            # Tested with them, the line would be cloned again in every retry round and Start.
            if heuristics_reach(config, repo, under, key, new, imports_too=False):
                again.add(repo)
                reasons["with changed path heuristics that match their files"] += 1
            else:
                unqueried += 1
            continue
        if not under:
            again.add(repo)
            reasons["with no record of how they were collected"] += 1
            continue
        if under.get("records") != key["records"]:
            again.add(repo)
            reasons["collected with other records"] += 1
            continue
        if collector_reach(config, repo, under, key,
                           paths.row_fields(main_row)[3] if main_row else ""):
            again.add(repo)
            reasons["collected by code of an unrecorded version"
                    if code_versions.changes(code_versions.COLLECTOR, under.get("collector"),
                                             key["collector"]) is None
                    else "with changes of the collector's code that reach their files"] += 1
            continue
        if under.get("git") != key["git"]:
            again.add(repo)
            reasons["collected with another version of git"] += 1
            continue
        # Whether the repository belongs to the collection in progress, by the line or by a head
        # query of an earlier run of the collection. A line taken over while git returned no
        # head commit never belongs to it, and its head is queried again. The record of the
        # collection is saved before the lines that lose the mark are written. A stop in between
        # leaves a repository in `taken_over` with a line that still has the mark, and no later
        # run would otherwise query its head.
        ours = not done[repo][8] and ((current is not None and done[repo][10] == current)
                                      or repo in confirmed)
        if outcome != "ok":
            if ours:
                unqueried += 1
            else:
                heads[repo] = None
            continue
        if heuristics_reach(config, repo, under, key, new, imports_too=False):
            again.add(repo)
            reasons["with changed path heuristics that match their files"] += 1
            continue
        if imports_reach(config, repo, under, key):
            again.add(repo)
            reasons["with changed import heuristics that match their files"] += 1
            continue
        if ours:
            unqueried += 1
            continue
        fields = paths.row_fields(main_row)
        heads[repo] = (fields[2], fields[3])
    if not check_heads:
        heads = {repo: recorded for repo, recorded in heads.items() if done[repo][8]}
    no_response = 0
    if heads:
        responses = remote_heads(config, sorted(heads), threads,
                                 guards.failure_ceiling(config.max_clone_failures, len(repos)),
                                 head_waits)
        for repo, recorded in heads.items():
            response = responses.get(repo)
            if recorded is None:
                if isinstance(response, tuple):
                    again.add(repo)
                    reasons["with a refusal that GitHub no longer gives"] += 1
                elif response is not None and collection is not None:
                    confirmed.add(repo)
                continue
            if response is None:
                state = NO_RESPONSE
                no_response += 1
            elif isinstance(response, str):
                state = REFUSED
                again.add(repo)
                reasons["refused by GitHub"] += 1
            elif response != recorded:
                state = MOVED
                again.add(repo)
                reasons["with a head that moved"] += 1
            else:
                state = SAME
                if collection is not None:
                    confirmed.add(repo)
            if queried is not None:
                queried[repo] = state
    taken = sum(1 for repo in repos if repo in done and repo not in skip and repo not in again)
    if done:
        why = ", ".join(f"{count:,} {reason}" for reason, count in reasons.most_common())
        logs.info(COMPONENT, f"Took over {taken:,} repositories from the earlier collection"
                             + (f" ({no_response:,} of them without a head commit from git, to "
                                f"query again)" if no_response else "") + ".")
        if unqueried:
            logs.info(COMPONENT, f"Queried no head commit again for {unqueried:,} of them, which "
                                 f"this collection collected or took over earlier.")
        if again:
            logs.info(COMPONENT, f"Collecting {len(again):,} repositories of the earlier "
                                 f"collection again ({why}).")
    return again


def _reclassify(job: tuple) -> tuple:
    """Reclassify one repository in a worker. Returns (repo, the outcome of
    `commits.reclassify` or "failed", the error or "")."""
    commits_dir, repo, head_sha, artifacts = job
    try:
        return repo, commits.reclassify(commits_dir, repo, head_sha, artifacts), ""
    except Exception as error:  # noqa: BLE001 - the repository is collected again
        return repo, "failed", f"{error.__class__.__name__} ({error})"


def reclassify(config, repos: list, done: dict, workers: int) -> set:
    """Classify again the commits of the repositories of `repos` that this run takes over, where
    the commit heuristics changed since their commit history was read, from the commit log kept
    then. Returns the repositories that cannot be classified again, which this run collects
    again."""
    if config.commits_dir is None:
        return set()
    jobs = []
    for repo in repos:
        record_path = commits.shard_paths(config.commits_dir, repo)[1]
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("classified_by") == commits.CLASSIFIED_BY:
            continue
        tree = config.collected_dir / repo_name_to_dir(repo)
        artifacts = paths.tree_files(tree) if tree.is_dir() else []
        jobs.append((config.commits_dir, repo, paths.row_fields(done[repo][0])[3], artifacts))
    if not jobs:
        return set()
    failed = set()
    with concurrent.futures.ProcessPoolExecutor(
            max(1, workers), mp_context=multiprocessing.get_context("spawn")) as pool:
        for repo, outcome, error in pool.map(_reclassify, jobs, chunksize=8):
            if outcome not in ("reclassified", "current", "history_not_read"):
                failed.add(repo)
                result = f"raised {error}" if error else f"ended in outcome {outcome}"
                logs.warning(COMPONENT, f"Classifying the commits of {repo} again from its "
                                        f"commit log {result}. Collecting the repository again.")
    logs.info(COMPONENT, f"Classified the commits of {len(jobs) - len(failed):,} repositories "
                         f"again under the current commit heuristics.")
    return failed


def _same_file(path: Path, other: Path) -> bool:
    """Whether two names are one file or directory, as a file system that folds case makes
    `owner§repo` and `Owner§Repo`. False when either does not exist."""
    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def _remove_directory(path: Path, repo: str) -> bool:
    """Remove the directory `path` of `repo` with its contents through `paths.remove_tree`,
    which removes what Synology DSM writes into the collected tree during the removal, and log
    a warning when the removal fails. Returns whether the directory is gone."""
    try:
        paths.remove_tree(path)
        return True
    except OSError as error:
        logs.warning(COMPONENT, f"Removing the directory {path} of {repo} raised "
                                f"{error.__class__.__name__} ({error}). The directory stays "
                                f"until a later run removes it or collects the repository "
                                f"again.")
        return False


def remove_left(config, repos: list, done: dict, finished_path: Path | None = None) -> int:
    """Remove the collected tree, the shards, the file list, the workflow files, the imported
    files and the clone log of every repository that is not in `repos`, the input list, and has
    a line in `done` or a directory in the collected tree. Returns how many repositories left.

    A file of a repository that left is kept where it is also the file of a repository in the
    input list under a name that differs only in case. A file system that folds case, such as
    macOS's by default, stores both names as one. GitHub's casing of a name changes between
    retrievals, and removing the files of the old name would remove the collection of the new
    name.

    For each repository that left with an ok line, the function appends the line again to
    `finished_path`, the list of finished repositories, with `redo` and without
    `collected_under`. A later input list that names the repository again then collects it
    rather than taking over a collection that is no longer on disk, and keeps nothing of it
    after a failed clone. The row alone cannot show that the files are gone, because a
    repository with files under `.agents/` alone has no flag. The line is appended before the
    files are removed, and a stop in between leaves files that the next collection of the
    repository replaces. The other order would leave an ok line with `collected_under` for files
    that are gone, and a later input list that names the repository again would take it over
    with nothing on disk.

    A directory that cannot be removed (`_remove_directory`) stays with a warning, and the
    function goes on. The step has finished the collection by then, and the
    line marked to redo already keeps a later collection from taking the repository over.

    The log counts the repositories of which this call removed files and kept none. The list of
    finished repositories keeps the line of a repository that left in an earlier run after its
    files are gone, and the repositories that left include it in every later run.
    """
    wanted = set(repos)
    left = {repo for repo in done if repo not in wanted}
    if config.collected_dir.is_dir():
        names, _strays = split(config.collected_dir, expected=("repos.csv", "clone_failed.csv"))
        left |= {dir_name_to_repo(name) for name in names} - wanted
    folded = {}
    for repo in repos:
        folded.setdefault(repo.casefold(), []).append(repo)

    def path_of(repo: str, kind: str) -> Path | None:
        dir_name = repo_name_to_dir(repo)
        if kind == "tree":
            return config.collected_dir / dir_name
        if kind == "record":
            return (commits.shard_paths(config.commits_dir, repo)[1]
                    if config.commits_dir is not None else None)
        if kind == "file list":
            return (config.file_lists_dir / f"{dir_name}{clone.FILE_LIST_SUFFIX}"
                    if config.file_lists_dir is not None else None)
        if kind == "clone log":
            return config.log_dir / f"{dir_name}.log"
        if kind == "stub list":
            return (imports.stub_list_path(config.imported_dir / dir_name)
                    if config.imported_dir is not None else None)
        if kind == "imported":
            return config.imported_dir / dir_name if config.imported_dir is not None else None
        return config.workflows_dir / dir_name if config.workflows_dir is not None else None

    kept, stayed, removed = set(), set(), set()
    for repo in left:
        # Before the removal, as the docstring states, and once, because a line that already
        # has `redo` and no `collected_under` is this function's.
        line = done.get(repo)
        if (finished_path is not None and line is not None and line[1] is None
                and not (line[4] and not line[6])):
            resume.append_finished(finished_path, repo, *line[:4], redo=True,
                                   collection=line[10])
        twins = [twin for twin in folded.get(repo.casefold(), ()) if twin != repo]

        def shared(kind: str) -> bool:
            path = path_of(repo, kind)
            if path is None or not twins:
                return False
            if any(_same_file(path, path_of(twin, kind)) for twin in twins):
                kept.add(repo)
                return True
            return False

        tree = path_of(repo, "tree")
        if tree.is_dir() and not tree.is_symlink() and not shared("tree"):
            (removed if _remove_directory(tree, repo) else stayed).add(repo)
        if not shared("record"):
            if config.commits_dir is not None and any(
                    path.exists() for path in (*commits.shard_paths(config.commits_dir, repo),
                                               commits.other_shard_path(config.commits_dir, repo),
                                               commits.stored_log_path(config.commits_dir, repo))):
                removed.add(repo)
            commits.remove_shards(config.commits_dir, repo)
        for kind in ("file list", "clone log", "stub list"):
            path = path_of(repo, kind)
            if path is not None and not shared(kind):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                removed.add(repo)
        for kind in ("workflows", "imported"):
            directory = path_of(repo, kind)
            if directory is not None and os.path.lexists(directory) and not shared(kind):
                (removed if _remove_directory(directory, repo) else stayed).add(repo)
    gone = removed - kept - stayed
    if gone:
        noun = "repository that is" if len(gone) == 1 else "repositories that are"
        logs.info(COMPONENT, f"Removed the files of {len(gone):,} {noun} not in the input list.")
    if kept:
        logs.info(COMPONENT, f"Kept the files of {len(kept):,} repositories that are not in "
                             f"the input list. Their names differ from listed names only in "
                             f"case, and this file system stores both names as one.")
    return len(left)

