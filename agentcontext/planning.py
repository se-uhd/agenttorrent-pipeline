"""Planning documents: the heuristics that find them, and the matching that the cloning step, the
collected file cleanup and the detection step share.

A planning document is a file that a spec-driven development framework, or a planning feature of one
of the six tools, writes into the repository at a location that the framework or the tool documents,
as a work product that agents read through a command or a reference rather than as configuration,
such as the `spec.md` of a Spec Kit feature or a plan that Cursor saves to the workspace. Planning
documents are not configuration artifacts: they set no tool flag, do not make a repository
configured and are in no mechanism CSV. The detection step writes them to
`output/planning-documents.csv`, and the section "Planning documents" of docs/heuristics.md states
the scope and the source of each heuristic.

This module loads the positive heuristics from `heuristics/planning_document_patterns.csv`.
`planning_schema` loads the layouts that are left out on purpose, each with the reason, from
`heuristics/not_planning_documents.csv`, together with the columns of `planning-documents.csv`.
This module is part of the collector's code. What only the steps after the collection read is
therefore in `planning_schema`, where a change collects nothing again. The module is separate
from `heuristics.py` because the code of `heuristics.py` is part of the `classified_by` digest
of the commit heuristics.

Columns of planning_document_patterns.csv
-----------------------------------------
framework     the framework or the tool that writes the file: one of the six tools for a
              tool's own planning feature, such as `cursor`, or the framework's name, such as
              `spec-kit`
document      the source's name for the document, such as `spec`, `plan` or `tasks`
pattern       a regular expression that the file's path, relative to a base that the match
              mode gives, matches in full, with `/` between the path's segments
match_mode    where the bases are (below)
anchor        blank, or a condition that the file has to meet beyond its path (below)
example       a path relative to the repository root that the row matches, from a repository
              of a collection where one has the layout
doc_url       the page that states the location
doc_verified  the ISO date on which the page was read
evidence      links to files at the path in repositories, each at a fixed commit, separated by
              spaces
notes         the quote that decides the row, and anything else that a reader needs

The rows are read in file order, and the first row that matches a file gives its framework and
document.

Match modes
-----------
root          the repository root
any_depth     the repository root and every directory below it
with:m|n      every directory, the root included, that contains one of the markers, each a path
              of a file or a directory relative to that directory, such as `.specify`
setting:f:k   the directory that the key `k`, a dotted path into the JSON of the file `f`,
              names. `f` and the value are both relative to the project root, which is the
              repository root or a directory below it, as for Claude Code's
              `.claude/settings.json` and its `plansDirectory`. The file is read in the format
              that `config_path_patterns.csv` gives it. A value that is not a string, that
              leaves the project root or that names the project root itself names no directory

Anchors
-------
beside:a|b         the file's own directory contains a file or a directory named `a` or `b`
repository:a|b     the repository contains the path `a` or `b` below some directory, the root
                   included
header:r           the first `HEAD_BYTES` of the file, read as UTF-8, contain a match of the
                   regular expression `r`, read with `re.MULTILINE`. A link has no header
folder_header:a|b:r  the folder that ends where the group `folder` of the pattern ends contains
                   a file named `a` or `b` with a header that matches `r`

A marker, the path of a `beside:` or `repository:` anchor and the settings file of a `setting:`
mode are the anchors of a planning document. An anchor is no planning document. The cloning step
copies it with the documents that it anchors, and the collected file cleanup keeps it, so that
the cleanup and the detection step find the same planning documents in the copy as the cloning
step found in the clone. For a directory, the anchor is the first file below it, in path order,
among the files that the matching lists.

A tool directory that is a link, such as a `.claude` that leads to `.agents`, is read through the
link, as the tool reads it: the files of the directory that it leads to are also matched at
their paths through the link, such as `.claude/plans/x.md` for `.agents/plans/x.md`, and a
planning document or an anchor found that way is reported at its own path (`find`).
"""

import csv
import os
import posixpath
import re
from pathlib import Path

from agentcontext import heuristics, jsonc, logs, paths, repos
from agentcontext.heuristics import HEURISTICS_DIR

PLANNING_DOCUMENT_PATTERNS_PATH = HEURISTICS_DIR / "planning_document_patterns.csv"

FIELDNAMES = ["framework", "document", "pattern", "match_mode", "anchor", "example", "doc_url",
              "doc_verified", "evidence", "notes"]

ROOT_MODE = "root"
ANY_DEPTH_MODE = "any_depth"
WITH_MODE = "with:"
SETTING_MODE = "setting:"

BESIDE_ANCHOR = "beside:"
REPOSITORY_ANCHOR = "repository:"
HEADER_ANCHOR = "header:"
FOLDER_HEADER_ANCHOR = "folder_header:"
# The bytes of a file that a header anchor reads: enough for a front matter and the opening
# lines of a document.
HEAD_BYTES = 4096

NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DOCUMENT = re.compile(r"^[a-z0-9]+(?: [a-z0-9]+)*$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GLOB_CHARACTERS = frozenset("*?[")


# ---------------------------------------------------------------------------
# Loading the heuristics
# ---------------------------------------------------------------------------

def path_problems(path: str) -> list:
    """What is wrong with a literal path, such as a marker."""
    segments = path.split("/")
    found = []
    if not path or path.startswith("/") or "" in segments:
        found.append(f"{path!r} is empty, absolute or has an empty segment")
    if any(segment in (".", "..") for segment in segments):
        found.append(f"{path!r} has a `.` or `..` segment")
    if _GLOB_CHARACTERS & set(path):
        found.append(f"{path!r} is a glob, but an anchor names one path")
    return found


def mode_problems(match_mode: str) -> list:
    """What is wrong with a match mode."""
    if match_mode in (ROOT_MODE, ANY_DEPTH_MODE):
        return []
    if match_mode.startswith(WITH_MODE):
        markers = match_mode[len(WITH_MODE):].split("|")
        return [problem for marker in markers for problem in path_problems(marker)]
    if match_mode.startswith(SETTING_MODE):
        parts = match_mode[len(SETTING_MODE):].split(":")
        if len(parts) != 2 or not all(parts) or path_problems(parts[0]):
            return [f"{match_mode!r} is not setting:<file>:<key>"]
        return []
    return [f"the match mode {match_mode!r} is none of {ROOT_MODE}, {ANY_DEPTH_MODE}, "
            f"{WITH_MODE}<markers> and {SETTING_MODE}<file>:<key>"]


def _regex_problem(text: str, what: str) -> str | None:
    """Why `text` is no regular expression, or None."""
    try:
        re.compile(text)
    except re.error as e:
        return f"the {what} {text!r} is no regular expression ({e})"
    return None


def anchor_problems(anchor: str, pattern: str) -> list:
    """What is wrong with an anchor of a row with the pattern `pattern`."""
    if not anchor:
        return []
    for kind in (BESIDE_ANCHOR, REPOSITORY_ANCHOR):
        if anchor.startswith(kind):
            return [problem for name in anchor[len(kind):].split("|")
                    for problem in path_problems(name)]
    if anchor.startswith(HEADER_ANCHOR):
        problem = _regex_problem(anchor[len(HEADER_ANCHOR):], "header")
        return [problem] if problem else []
    if anchor.startswith(FOLDER_HEADER_ANCHOR):
        names, _, regex = anchor[len(FOLDER_HEADER_ANCHOR):].partition(":")
        found = [problem for name in names.split("|") for problem in path_problems(name)]
        problem = _regex_problem(regex, "header")
        if problem:
            found.append(problem)
        elif not regex:
            found.append(f"{anchor!r} is not folder_header:<names>:<regex>")
        try:
            if "folder" not in re.compile(pattern).groupindex:
                found.append(f"{anchor!r} needs a group named folder in the pattern")
        except re.error:
            pass
        return found
    return [f"the anchor {anchor!r} is none of {BESIDE_ANCHOR}, {REPOSITORY_ANCHOR}, "
            f"{HEADER_ANCHOR} and {FOLDER_HEADER_ANCHOR}"]


def _problems(rows: list, path: Path) -> list:
    """Every problem of the heuristics, rather than only the first."""
    found, seen = [], set()
    for number, row in enumerate(rows, start=2):
        where = f"{path.name}:{number} ({row.get('framework')}/{row.get('pattern')})"
        if not NAME.match(row["framework"] or ""):
            found.append(f"{where} has the framework {row['framework']!r}, which is not a "
                         f"lowercase name with hyphens")
        if not _DOCUMENT.match(row["document"] or ""):
            found.append(f"{where} has the document {row['document']!r}, which is not lowercase "
                         f"words")
        problem = _regex_problem(row["pattern"] or "", "pattern")
        if problem or not row["pattern"]:
            found.append(f"In {where}, {problem}" if problem else f"{where} has no pattern")
        found += [f"In {where}, {problem}" for problem in mode_problems(row["match_mode"])]
        found += [f"In {where}, {problem}" for problem in anchor_problems(row["anchor"],
                                                                         row["pattern"])]
        key = (row["pattern"], row["match_mode"], row["anchor"])
        if key in seen:
            found.append(f"{where} repeats the pattern, the match mode and the anchor of an "
                         f"earlier row")
        seen.add(key)
        if not row["example"] or path_problems(row["example"].replace("*", "x")):
            found.append(f"{where} has the example {row['example']!r}, which is no repository "
                         f"path")
        if not (row["doc_url"] or "").startswith("https://"):
            found.append(f"{where} has the doc_url {row['doc_url']!r}, which is not an https "
                         f"link to the page that states the location")
        if not DATE.match(row["doc_verified"] or ""):
            found.append(f"{where} has the doc_verified {row['doc_verified']!r}, which is not an "
                         f"ISO date")
        if not (row["notes"] or "").strip():
            found.append(f"{where} has no note that quotes what decides the row")
        found += heuristics.evidence_problems(row, where)
    return found


def read_rows(path: Path, fieldnames: list) -> list:
    """The rows of a heuristics file, without the rows that a `#` comments out."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(fieldnames) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the columns {sorted(missing)}")
        first = fieldnames[0]
        return [r for r in reader
                if any((r[c] or "").strip() for c in fieldnames)
                and not (r[first] or "").startswith("#")]


# The heuristics that `load_planning_document_patterns` read, kept for the process under the
# file's path, modification time and size, so that an edited file is read again. The cleanup,
# the detection step, two checks and the fingerprint of the content quality analysis find the
# planning documents of every repository, and loading and validating the file for each
# repository would slow every pass over the sample.
_LOADED: dict = {}


def load_planning_document_patterns(path=None) -> list:
    """Every heuristic as a dict, in file order, or raise ValueError with every problem. A file
    that is unchanged since the last call of the process is not read again."""
    path = Path(path) if path else PLANNING_DOCUMENT_PATTERNS_PATH
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        stamp = None
    kept = _LOADED.get(str(path))
    if stamp is not None and kept is not None and kept[0] == stamp:
        return kept[1]
    rows = read_rows(path, FIELDNAMES)
    if not rows:
        raise ValueError(f"{path} has no rows")
    problems = _problems(rows, path)
    if problems:
        raise ValueError(f"Found {len(problems):,} problem(s) in {path}.\n  "
                         + "\n  ".join(problems))
    if stamp is not None:
        _LOADED[str(path)] = (stamp, rows)
    return rows


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def leading_literal(pattern: str) -> str:
    """The text that every match of a pattern starts with: its characters up to the first one
    that is special in a regular expression, with escapes resolved, and without a last
    character that a quantifier makes optional."""
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and not pattern[i + 1].isalnum():
            out.append(pattern[i + 1])
            i += 2
            continue
        if c in ".^$*+?{}[]()|\\":
            break
        out.append(c)
        i += 1
    if i < len(pattern) and pattern[i] in "*?{" and out:
        out.pop()
    return "".join(out)


class _Rule:
    """A heuristic, compiled for matching."""

    def __init__(self, row: dict):
        self.row = row
        self.mode = row["match_mode"]
        pattern = row["pattern"]
        if self.mode == ANY_DEPTH_MODE:
            # From any segment boundary to the end of the path.
            self.regex = re.compile(r"(?:^|(?<=/))(?:" + pattern + r")\Z")
        else:
            self.regex = re.compile(pattern)
        literal = leading_literal(pattern)
        # A directory that every match starts with, which a path has to contain.
        self.segment = literal.split("/")[0] if "/" in literal else None
        anchor = row["anchor"]
        self.anchor_kind = next((kind for kind in (BESIDE_ANCHOR, REPOSITORY_ANCHOR,
                                                   HEADER_ANCHOR, FOLDER_HEADER_ANCHOR)
                                 if anchor.startswith(kind)), None)
        self.names, self.header = (), None
        if self.anchor_kind in (BESIDE_ANCHOR, REPOSITORY_ANCHOR):
            self.names = tuple(anchor[len(self.anchor_kind):].split("|"))
        elif self.anchor_kind == HEADER_ANCHOR:
            self.header = re.compile(anchor[len(HEADER_ANCHOR):], re.MULTILINE)
        elif self.anchor_kind == FOLDER_HEADER_ANCHOR:
            names, _, regex = anchor[len(FOLDER_HEADER_ANCHOR):].partition(":")
            self.names = tuple(names.split("|"))
            self.header = re.compile(regex, re.MULTILINE)


def listed_files(root: Path, vendored_dirs) -> list:
    """The repository-relative paths of the files and links of a repository's tree, sorted, with
    `/` between their components. The listing leaves out the `.git` directory, the vendored
    directories, a directory that a link names, the places of the tools' local state and the
    paths that `paths.is_discarded_path` names, and it is the same in a clone, in the collected
    tree and in the validated tree."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d != ".git" and not paths.is_pruned(Path(dirpath) / d,
                                                                    vendored_dirs, root))
        base = Path(dirpath).relative_to(root).as_posix()
        for name in filenames:
            rel = name if base == "." else f"{base}/{name}"
            if not paths.is_discarded_path(rel) and not paths.is_local_state_file(
                    Path(dirpath) / name, root):
                found.append(rel)
    return sorted(found)


def _named_at(name: str, rel: str) -> str | None:
    """The directory relative to which `rel` is `name`, `""` for the root, else None."""
    if rel == name:
        return ""
    if rel.endswith("/" + name):
        return rel[:-len(name) - 1]
    return None


# The format of each settings file that a configuration row gives one, read on first use.
_CONTENT_FORMATS: dict = {}


def _content_format(settings_file: str) -> str:
    """The format in which the tool reads the settings file: the `content_format` that
    `config_path_patterns.csv` gives the file, strict JSON where no row names it."""
    if not _CONTENT_FORMATS:
        for row in heuristics.load_config_path_patterns():
            if row.get("content_format"):
                _CONTENT_FORMATS.setdefault(row["path"], row["content_format"])
        _CONTENT_FORMATS.setdefault("", "")
    return _CONTENT_FORMATS.get(settings_file, "")


def _settings_text(path: Path) -> str | None:
    """The text of a settings file that is a regular file, or None.

    A settings file that is a link names no folder. The cloning step stores a link as its
    marker, and the collected file cleanup and the detection step read no setting from a
    marker. A folder read through the link in the clone alone would therefore give the cleanup
    files to delete.
    Reading the file here does not make the cloning step store it with its content
    (`paths.store_declaring_files`), which would change how the detection step reads it as
    configuration."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def read_head(path: Path) -> str | None:
    """The first `HEAD_BYTES` of a regular file as text, or None for a link or a file that
    cannot be read. The copy stores a link as its marker, and the link's header in the clone
    would therefore be read nowhere else."""
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with open(path, "rb") as f:
            data = f.read(HEAD_BYTES)
    except OSError:
        return None
    text = data.decode("utf-8", errors="replace").removeprefix("﻿")
    # The copy stores a link as its marker. Reading the marker as None matches the clone, where
    # the link itself is read as None above.
    return None if paths.link_marker(text) else text


def setting_value(data, key: str):
    """The value under the dotted `key` of a parsed JSON document, or None."""
    for part in key.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def named_directory(project: str, value) -> str | None:
    """The repository-relative directory that a setting's `value` names, relative to the
    project root `project` (`""` for the repository root), or None where it names none: a value
    that is not a non-empty string, an absolute path, and a path that leaves the project root
    or is the project root itself."""
    if not isinstance(value, str) or not value.strip() or value.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(project, value) if project else value)
    if project:
        inside = joined.startswith(project + "/")
    else:
        inside = joined not in (".", "") and not joined.startswith("../") and joined != ".."
    return joined if inside else None


class _Tree:
    """What the matching reads of one repository's tree: its files, its directories, the first
    file below each directory, and the headers of the files that it reads, read once.
    `through` maps a path through a linked tool directory to the file's own path, at which a
    header or a setting is read."""

    def __init__(self, root: Path, files: list, head_reader, through=None):
        self.root = root
        self.through = through or {}
        self.files = files
        self.file_set = set(files)
        self.first_below: dict = {}
        for rel in files:
            parts = rel.split("/")[:-1]
            for k in range(1, len(parts) + 1):
                self.first_below.setdefault("/".join(parts[:k]), rel)
        self.present = self.file_set | set(self.first_below)
        # The present paths by their last segment, in path order, for finding a marker.
        self.by_name: dict = {}
        for rel in sorted(self.present):
            self.by_name.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
        self._heads: dict = {}
        self._head_reader = head_reader or (lambda rel: read_head(self.root / rel))
        self._bases: dict = {}
        self._repository: dict = {}

    def witness(self, rel: str) -> str:
        """The file that makes the path `rel` present: the file itself, or the first file below
        the directory."""
        return rel if rel in self.file_set else self.first_below[rel]

    def head(self, rel: str) -> str | None:
        if rel not in self._heads:
            self._heads[rel] = self._head_reader(rel)
        return self._heads[rel]

    def bases(self, mode: str) -> dict | None:
        """{base directory: the anchor that makes it a base} for a match mode, with `""` for the
        repository root, which has the anchor None in the mode `root`. Returns None for
        `any_depth`, in which every directory is a base."""
        if mode == ANY_DEPTH_MODE:
            return None
        if mode not in self._bases:
            self._bases[mode] = self._compute_bases(mode)
        return self._bases[mode]

    def _compute_bases(self, mode: str) -> dict:
        if mode == ROOT_MODE:
            return {"": None}
        bases = {}
        if mode.startswith(WITH_MODE):
            for marker in mode[len(WITH_MODE):].split("|"):
                for rel in self.by_name.get(marker.rsplit("/", 1)[-1], ()):
                    base = _named_at(marker, rel)
                    if base is not None:
                        bases.setdefault(base, self.witness(rel))
            return bases
        settings_file, key = mode[len(SETTING_MODE):].split(":")
        for rel in self.files:
            project = _named_at(settings_file, rel)
            if project is None:
                continue
            text = _settings_text(self.root / self.through.get(rel, rel))
            if text is None:
                continue
            try:
                data = jsonc.loads(text, _content_format(settings_file))
            except (ValueError, RecursionError):
                continue
            named = named_directory(project, setting_value(data, key))
            if named is not None:
                bases.setdefault(named, rel)
        return bases

    def in_repository(self, name: str) -> str | None:
        """The file that makes `name` present below some directory, or None."""
        if name not in self._repository:
            self._repository[name] = next(
                (self.witness(rel) for rel in self.by_name.get(name.rsplit("/", 1)[-1], ())
                 if _named_at(name, rel) is not None), None)
        return self._repository[name]


def _match(rule: _Rule, rel: str, tree: _Tree) -> tuple | None:
    """`(match, base)` where the path `rel` matches the heuristic's pattern below a base, or
    None."""
    if rule.mode == ANY_DEPTH_MODE:
        match = rule.regex.search(rel)
        return (match, "") if match else None
    for base in tree.bases(rule.mode):
        rest = rel if not base else (rel[len(base) + 1:] if rel.startswith(base + "/") else None)
        if rest is None or (rule.segment is not None
                            and not rest.startswith(rule.segment + "/")):
            continue
        match = rule.regex.fullmatch(rest)
        if match:
            return match, base
    return None


def _anchored(rule: _Rule, rel: str, match, base: str, tree: _Tree) -> list | None:
    """The anchors of the file `rel` that `rule` matched, or None where the anchor fails."""
    found = []
    if rule.mode not in (ROOT_MODE, ANY_DEPTH_MODE):
        anchor = tree.bases(rule.mode)[base]
        if anchor:
            found.append(anchor)
    if rule.anchor_kind == BESIDE_ANCHOR:
        folder = posixpath.dirname(rel)
        beside = [f"{folder}/{name}" if folder else name for name in rule.names]
        present = [path for path in beside if path in tree.present]
        if not present:
            return None
        found.append(tree.witness(present[0]))
    elif rule.anchor_kind == REPOSITORY_ANCHOR:
        witness = next((w for w in map(tree.in_repository, rule.names) if w), None)
        if witness is None:
            return None
        found.append(witness)
    elif rule.anchor_kind == HEADER_ANCHOR:
        head = tree.head(rel)
        if head is None or not rule.header.search(head):
            return None
    elif rule.anchor_kind == FOLDER_HEADER_ANCHOR:
        end = match.end("folder")
        folder = rel[:end] if rule.mode == ANY_DEPTH_MODE else (
            f"{base}/{match.string[:end]}" if base else match.string[:end])
        named = [f"{folder}/{name}" for name in rule.names]
        header = next((path for path in named if path in tree.file_set
                       and rule.header.search(tree.head(path) or "")), None)
        if header is None:
            return None
        found.append(header)
    return found


def find(root: Path, vendored_dirs=None, heuristic_rows=None, files=None,
         head_reader=None) -> tuple:
    """`(documents, anchors)` of a repository's tree: `(path, heuristic row)` for every
    planning document, in path order, each with the first row in file order that matches it,
    and the set of the anchors of these documents that are no planning documents themselves.

    `files` are the tree's paths, `listed_files` by default, and `head_reader` reads the header
    of a path, from the file below `root` by default. A file below a vendored directory is no
    planning document, as it is no configuration artifact.

    Where a tool directory is a link in the clone, or the collection's marker of one
    (`paths.linked_tool_directory_pairs`), the files of the directory that it leads to are
    matched at their paths through the link as well, as the tool reads them, and a document or an
    anchor found that way is reported at its own path. samanhappy/mcphub's
    `.agents/plans/<name>.md` is Claude Code's plan `.claude/plans/<name>.md` through
    `.claude -> .agents`. `listed_files` does not follow a link, and the file's own path decides
    where the cloning step copies it.
    """
    root = Path(root)
    vendored_dirs = heuristics.VENDORED_DIRS if vendored_dirs is None else vendored_dirs
    rows = load_planning_document_patterns() if heuristic_rows is None else heuristic_rows
    files = listed_files(root, vendored_dirs) if files is None else sorted(files)
    pairs = paths.linked_tool_directory_pairs(root, vendored_dirs)
    read_at = {rel: [link + rel[len(directory):] for link, directory in pairs
                     if rel.startswith(directory + "/")] for rel in files}
    # A place of the linking tool's local state, such as a checkout in `.claude/worktrees/` of a
    # `.claude` that links to `.agents`, is left out at both paths, as `listed_files` leaves it
    # out of the tool directory itself and the cloning step out of the copy of the directory
    # (`paths.remove_local_state`).
    own = {rel for rel in files
           if not any(paths.local_state(path) is not None for path in read_at[rel])}
    through = {}
    for rel in sorted(own):
        for path in read_at[rel]:
            if path not in own:
                through.setdefault(path, rel)
    files = sorted(own | set(through))
    reader = head_reader or (lambda rel: read_head(root / rel))
    tree = _Tree(root, files, lambda rel: reader(through.get(rel, rel)), through)
    rules = [_Rule(row) for row in rows]
    # A heuristic with a leading directory applies only to a path that contains the directory.
    by_segment, unsegmented = {}, []
    for number, rule in enumerate(rules):
        if rule.segment is None:
            unsegmented.append(number)
        else:
            by_segment.setdefault(rule.segment, []).append(number)
    documents, anchors = [], set()
    # The files at their own paths first, so that a file that a heuristic matches there keeps
    # that heuristic's row.
    for rel in sorted(files, key=lambda path: path in through):
        candidates = set(unsegmented)
        for directory in rel.split("/")[:-1]:
            candidates.update(by_segment.get(directory, ()))
        for number in sorted(candidates):
            rule = rules[number]
            matched = _match(rule, rel, tree)
            if matched is None:
                continue
            found = _anchored(rule, rel, *matched, tree)
            if found is None:
                continue
            documents.append((through.get(rel, rel), rule.row))
            anchors.update(through.get(anchor, anchor) for anchor in found)
            break
    documents = sorted(dict(reversed(documents)).items())
    anchors -= {rel for rel, _row in documents}
    return documents, anchors


def planning_documents(root: Path, vendored_dirs=None, heuristic_rows=None, files=None,
                       head_reader=None) -> list:
    """`(path, heuristic row)` for every planning document of a repository's tree (`find`)."""
    return find(root, vendored_dirs, heuristic_rows, files, head_reader)[0]


# ---------------------------------------------------------------------------
# The cloning step
# ---------------------------------------------------------------------------

def collect_planning_documents(repo: Path, out: Path, vendored_dirs, print_matches=False) -> set:
    """Copy every planning document of the clone `repo`, and the anchors that they need, into
    `out` at its path, as `paths.copy_file` copies a file, which turns a link into its marker.
    A file that an earlier pass copied stays as it is. Sets no flag, because a planning
    document is no configuration artifact."""
    documents, anchors = find(repo, vendored_dirs)
    for rel in sorted({rel for rel, _row in documents} | anchors):
        dest = out / rel
        if dest.exists() or dest.is_symlink():
            continue
        if print_matches:
            logs.info('repository_cloning', f"Copied the planning document or anchor {rel} of "
                                            f"{repos.dir_name_to_repo(repo.name)}.")
        paths.copy_file(repo / rel, dest)
    return set()
