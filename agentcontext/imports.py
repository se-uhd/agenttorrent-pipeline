"""Imports: the files that a tool loads into context automatically because a
configuration artifact imports them, such as a file that `@docs/style.md` names in a CLAUDE.md.

An import is an extension of the dataset beside the configuration artifacts. An imported file sets
no tool flag, adds no repository and is in no mechanism CSV. The cloning step parses every copied
file that a row of `heuristics/import_patterns.csv` names as an importing file, resolves each import
as the tool does, and copies every file that an import reaches inside the repository to
`data/imported/<owner§repo>/<path>` (`collect_imports`). The import detection step parses the same
files again over the validated tree and the imported tree and writes `output/imports.csv`
(`agentcontext/steps/import_detection.py`). Both call the functions below, so that the copy and the
rows cannot disagree. The section "Imports" of docs/heuristics.md gives the source of each row, and
`heuristics/not_imports.csv` lists what is left out, each with its reason.

This module is outside `heuristics.py`, because the code of `heuristics.py` is part of the
`classified_by` digest of the commit heuristics.

Columns of import_patterns.csv
------------------------------
id              the row's own id, `<tool>-import-<nn>`, never given to another row, because
                `imports.csv` names the row by it in `heuristic`
tool            the tool of the dataset that reads the imports: `claude` (Claude Code
                and Claude Code Review), `copilot`, `gemini` (Gemini CLI and Antigravity) or
                `cursor` (Bugbot)
path            the importing file, read with `match_mode`
match_mode      one of the match modes of `config_path_patterns.csv` that `IMPORTER_MODES`
                names, read as that file reads it
syntax          how an import is written, one of `PARSERS`
resolution      what a relative path resolves against: `importer_dir`, the directory of the
                file that contains the import; `real_dir`, the directory of the file that its
                links lead to; `imported_real_dir`, the directory of the importing file at the
                start of the chain itself and, for an imported file, of the file that its
                links lead to
max_depth       the deepest hop that the tool reads: 1 for the imports written in the
                importing file, 2 for the imports written in a file that it imports, and so on
max_files       the most files that the tool reads for one importing file over the chain, or
                blank for no such limit
max_bytes       the most bytes of imported files that the tool reads for one importing file
                over the chain, with the importing file itself under the condition
                `importing_file_bytes`, or blank
max_file_bytes  the size of the largest file that the tool reads, or blank
extensions      the extensions of the files that the tool reads, compared in lower case and
                separated by `|`, with `<none>` for a file without one, or `*` for every file
condition       the conditions of the row, separated by `|`, or blank (`CONDITIONS`)
source          `doc` for a vendor page, `forum` for posts of the vendor's staff in its forum
doc_url         the page or the post, which the documentation watch reads again
doc_verified    the ISO date on which it was read
evidence        links that the page does not give, such as the vendor's code at a fixed commit
notes           the quotes that decide the row, and the code facts where the page is silent

Conditions
----------
no_claude_md        the file imports only where no CLAUDE.md, .claude/CLAUDE.md or
                    CLAUDE.local.md is at the repository root or in the file's own directory,
                    and for a row that `ignores_case` no such file at the root in any case
automatic_trigger   the file imports only where its YAML front matter sets a `trigger` with
                    which Antigravity loads the rule without a request, `always_on` or `glob`
                    (`AUTOMATIC_TRIGGERS`)
utf8                the tool reads a target only where its content is valid UTF-8
each_branch         a file that two imports reach is read for each of them, and only an import
                    of a file that its own chain of imports reached already is not read again,
                    which `chain` records once per file. Without it, a file is read once per
                    importing file, and a link to a file that the chain has read is that file
importing_file_bytes
                    `max_bytes` counts the bytes of the importing file at the start of the chain
                    with the bytes of the files that it imports

Columns of not_imports.csv
--------------------------
id, tool, path, syntax, source, doc_url, doc_verified, evidence and notes, as above, for a file
and a syntax that a tool does not read as an import, with the reason in `notes`.
"""

import bisect
import collections
import csv
import functools
import json
import os
import posixpath
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from agentcontext import caches, heuristics, paths
from agentcontext.heuristics import HEURISTICS_DIR
from agentcontext.prune import is_prunable

IMPORT_PATTERNS_PATH = HEURISTICS_DIR / "import_patterns.csv"
NOT_IMPORTS_PATH = HEURISTICS_DIR / "not_imports.csv"

FIELDNAMES = ["id", "tool", "path", "match_mode", "syntax", "resolution", "max_depth",
              "max_files", "max_bytes", "max_file_bytes", "extensions", "condition", "source",
              "doc_url", "doc_verified", "evidence", "notes"]
NOT_IMPORTS_FIELDNAMES = ["id", "tool", "path", "syntax", "source", "doc_url", "doc_verified",
                          "evidence", "notes"]
# The columns that decide what the collector copies. The other columns record where a row comes
# from and when its page was read, and the documentation watch changes them without changing
# what is collected (`reuse.import_heuristics`).
COLLECTION_COLUMNS = ("id", "tool", "path", "match_mode", "syntax", "resolution", "max_depth",
                      "max_files", "max_bytes", "max_file_bytes", "extensions", "condition")
LIMIT_COLUMNS = ("max_files", "max_bytes", "max_file_bytes")

TOOLS = heuristics.TOOLS
SOURCES = ("doc", "forum")
RESOLUTIONS = ("importer_dir", "real_dir", "imported_real_dir")
CONDITIONS = ("no_claude_md", "automatic_trigger", "utf8", "each_branch",
              "importing_file_bytes")
# The triggers of an Antigravity rule with which Antigravity loads it without a request: an
# `always_on` rule on every turn and a `glob` rule when the agent works with a matching file. A
# `model_decision` rule is read on demand and a `manual` rule when the user mentions it
# (antigravity.google/docs/rules), and their includes are no imports
# (`not_imports.csv`).
AUTOMATIC_TRIGGERS = ("always_on", "glob")
# The match modes of `config_path_patterns.csv` with which a row names its importing files.
IMPORTER_MODES = ("file", "root_file", "basename", "root_any_case", "tree_ext:",
                  "dir_ext_triggered:", "declared_names:", "declared_local:", "declared_rules",
                  "nested:file")
NO_EXTENSION = "<none>"
EVERY_EXTENSION = "*"

_ID = re.compile(r"^(?P<tool>[a-z]+)-import-\d{2,}$")
_NOT_ID = re.compile(r"^(?P<tool>[a-z]+)-not-import-\d{2,}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ---------------------------------------------------------------------------
# Loading the heuristics
# ---------------------------------------------------------------------------

def _read(path: Path, fieldnames: list) -> list:
    """Every row of a heuristics file as a dict, in file order, without comment rows."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = set(fieldnames) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} lacks the columns {sorted(missing)}")
        rows = [r for r in reader if r["id"] and not r["id"].startswith("#")]
    if not rows:
        raise ValueError(f"{path} has no rows")
    return rows


def _common_problems(row: dict, where: str, form: re.Pattern, seen: set) -> list:
    """What is wrong with the columns that both files have."""
    found = []
    match = form.match(row["id"] or "")
    if not match or match.group("tool") != row["tool"]:
        found.append(f"{where} has the id {row['id']!r}, not {row['tool']}-"
                     f"{'not-' if form is _NOT_ID else ''}import-<number>")
    if row["id"] in seen:
        found.append(f"{where} has the id {row['id']!r}, which is also the id of another row")
    seen.add(row["id"])
    if row["tool"] not in TOOLS:
        found.append(f"{where} has the tool {row['tool']!r}, which is not one of {TOOLS}")
    if row["source"] not in SOURCES:
        found.append(f"{where} has the source {row['source']!r}, which is not one of {SOURCES}")
    if not row["doc_url"].startswith("https://"):
        found.append(f"{where} has the doc_url {row['doc_url']!r}, which is not an https link "
                     f"to the page or the post that grounds the row")
    if not _DATE.match(row["doc_verified"] or ""):
        found.append(f"{where} has the doc_verified {row['doc_verified']!r}, which is not an ISO "
                     f"date")
    if not (row["notes"] or "").strip():
        found.append(f"{where} has no note that quotes what decides the row")
    found += heuristics.evidence_problems(row, where)
    return found


def load_import_patterns(heuristics_path=None) -> list:
    """The import heuristics, each a dict, in file order, or raise ValueError with every
    problem."""
    path = Path(heuristics_path) if heuristics_path else IMPORT_PATTERNS_PATH
    rows = _read(path, FIELDNAMES)
    found, seen = [], set()
    for number, row in enumerate(rows, start=2):
        where = f"{path.name}:{number} ({row.get('id')})"
        found += _common_problems(row, where, _ID, seen)
        mode = row["match_mode"]
        if not (mode == "declared_rules" or mode.startswith(IMPORTER_MODES)):
            found.append(f"{where} has the match_mode {mode!r}, which is not one of "
                         f"{IMPORTER_MODES}")
        if row["syntax"] not in PARSERS:
            found.append(f"{where} has the syntax {row['syntax']!r}, which is not one of "
                         f"{tuple(PARSERS)}")
        if row["resolution"] not in RESOLUTIONS:
            found.append(f"{where} has the resolution {row['resolution']!r}, which is not one of "
                         f"{RESOLUTIONS}")
        if not (row["max_depth"] or "").isdigit() or int(row["max_depth"]) < 1:
            found.append(f"{where} has the max_depth {row['max_depth']!r}, which is not a whole "
                         f"number of hops of at least 1")
        for column in LIMIT_COLUMNS:
            if row[column] and (not row[column].isdigit() or int(row[column]) < 1):
                found.append(f"{where} has the {column} {row[column]!r}, which is neither blank "
                             f"nor a whole number of at least 1")
        extensions = (row["extensions"] or "").split("|")
        if row["extensions"] != EVERY_EXTENSION and not all(
                e == NO_EXTENSION or re.fullmatch(r"\.[a-z0-9_+-]+", e) for e in extensions):
            found.append(f"{where} has the extensions {row['extensions']!r}, which are not `*` "
                         f"or lowercase extensions and {NO_EXTENSION} separated by `|`")
        unknown = sorted(c for c in conditions(row) if c not in CONDITIONS)
        if unknown:
            found.append(f"{where} names the conditions {unknown}, which are not in "
                         f"{CONDITIONS}")
        if "utf8" in conditions(row) and not (row["max_file_bytes"] or row["max_bytes"]):
            # The stub of a file that the tool does not read keeps whether its text is valid
            # UTF-8 only up to the largest limit of a row (`_write_stub`).
            found.append(f"The condition utf8 of {where} needs max_file_bytes or max_bytes")
        if "importing_file_bytes" in conditions(row) and not row["max_bytes"]:
            found.append(f"The condition importing_file_bytes of {where} needs max_bytes")
    if found:
        raise ValueError(f"Found {len(found):,} problem(s) in {path}.\n  " + "\n  ".join(found))
    return rows


@functools.cache
def import_patterns() -> tuple:
    """The import heuristics of `heuristics/import_patterns.csv`, read once per process, as the
    workers of the cloning step read them for every repository."""
    return tuple(load_import_patterns())


def load_not_imports(heuristics_path=None) -> list:
    """What the tools do not read as an import, each with the reason, in file order, or raise
    ValueError with every problem."""
    path = Path(heuristics_path) if heuristics_path else NOT_IMPORTS_PATH
    rows = _read(path, NOT_IMPORTS_FIELDNAMES)
    found, seen = [], set()
    for number, row in enumerate(rows, start=2):
        where = f"{path.name}:{number} ({row.get('id')})"
        found += _common_problems(row, where, _NOT_ID, seen)
        if not (row["path"] or "").strip() or not (row["syntax"] or "").strip():
            found.append(f"{where} lacks the path or the syntax. A negative names the file and "
                         f"the syntax that it leaves out.")
    if found:
        raise ValueError(f"Found {len(found):,} problem(s) in {path}.\n  " + "\n  ".join(found))
    return rows


def conditions(row: dict) -> set:
    """The conditions that the `condition` column of a row names."""
    return {c for c in (row.get("condition") or "").split("|") if c}


def extensions_read(row: dict, target: str) -> bool:
    """Whether the tool of `row` reads a file at `target` by its extension."""
    if row["extensions"] == EVERY_EXTENSION:
        return True
    extension = posixpath.splitext(posixpath.basename(target))[1].lower()
    return (extension or NO_EXTENSION) in row["extensions"].split("|")


# ---------------------------------------------------------------------------
# Resolving an import
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Import:
    """One import as a parser finds it: the text as written, such as `@docs/a.md`, the line
    on which it starts, counted from 1, and the path that the tool resolves, after the
    syntax's own processing, such as a dropped `#fragment`. `inner` marks an import inside the
    path of the import before it, which the tool reads only where no import before it in the
    same word named a file (`copilot_at`). `too_long` marks the imports of a word that no file
    can have, as `nameable` decides. The parser keeps one of them, shortened.

    The last three fields hold limits that the syntax sets for one kind of its imports, such as
    an included skill of Claude Code Review (`code_review_include`). `max_file_bytes` is the
    size of the largest file that the tool reads for the import, below the row's own limit.
    Without `through_links`, the tool does not read a target with a link on its path.
    `past_count` marks an import beyond the number of such imports that the tool reads."""
    written: str
    line: int
    path: str
    inner: bool = False
    too_long: bool = False
    max_file_bytes: int | None = None
    through_links: bool = True
    past_count: bool = False


# GROUNDING: PATH_MAX and NAME_MAX of Linux (include/uapi/linux/limits.h), where the cloning step
# runs. No file has a path or a name of more bytes, and an import of a longer path therefore names
# no file. The dataset records such an import once and shortened rather than looking it up.
PATH_MAX, NAME_MAX = 4096, 255
# The characters of such an import that its row keeps.
SHOWN = 200


def nameable(path: str) -> bool:
    """Whether a file can have the path `path`: at most PATH_MAX bytes, and no name of more than
    NAME_MAX bytes between its `/`."""
    if len(path) > PATH_MAX:
        return False
    encoded = path.encode("utf-8", "surrogatepass")
    return len(encoded) <= PATH_MAX and all(len(name) <= NAME_MAX
                                            for name in encoded.split(b"/"))


def shown(text: str) -> str:
    """`text`, cut to SHOWN characters with `...` where it is longer."""
    return text if len(text) <= SHOWN else text[:SHOWN] + "..."


# What an import reaches, the `target_status` of `imports.csv`:
#   found       a regular file inside the repository, outside `.git` and every gitlink, that
#               the tool reads, which the cloning step copied to the imported tree
#   missing     no such file at the collected commit: nothing at the path, a directory, a link
#               that leads out of the repository, or a file in `.git` or a gitlink
#   outside     a path that leaves the repository as written: absolute, in the home directory,
#               or above the repository root
#   not_read    a file that the tool does not read: an extension that the row does not list,
#               content that is not valid UTF-8 where the row requires it, or a link on its path
#               where the tool follows none (`Import.through_links`)
#   over_limit  a file that the tool would read, beyond the files or the bytes that it reads for
#               one importing file, beyond the imports of its kind that it reads
#               (`Import.past_count`), or larger than the largest file that it reads
#   pruned      a path that the archive step removes before publication, such as a session
#               transcript of Claude Code or a log (`prune.is_prunable`), which the dataset
#               does not look up, copy or publish, whatever is there
#   not_copied  a file that the tool reads, of which the imported tree holds only a stub,
#               because the cloning step copied the repository's imports under import
#               heuristics that did not read it, such as an earlier collection that a failed
#               retry kept. The chain counts it toward its limits and does not read its imports
FOUND, MISSING, OUTSIDE, NOT_READ, OVER_LIMIT, PRUNED, NOT_COPIED = (
    "found", "missing", "outside", "not_read", "over_limit", "pruned", "not_copied")
STATUSES = (FOUND, MISSING, OUTSIDE, NOT_READ, OVER_LIMIT, PRUNED, NOT_COPIED)

# The syntaxes with a tool that opens a path as written. Copilot CLI 1.0.98 read nothing for
# `@s38.md/` where s38.md is a file, and nothing for `@\a.md` where `\a.md` and `a.md` are files,
# a path that it takes as absolute. Claude Code and Gemini CLI resolve a path with Node's
# `path.resolve`, which drops a trailing `/`.
_AS_WRITTEN = frozenset({"copilot_at"})
# A chain finds a target only where each name of its path matches the entry of its directory in
# case as well, as on the volume that compares case, on which the cloning step runs. The copy and
# the rows therefore do not depend on the file system of the step, and on a file system that
# ignores case, such as the default one of macOS, a chain finds the files that it finds on that
# volume. Claude Code Review compares case on every file system: It states "match the file name
# exactly, including case" (code.claude.com/docs/en/code-review-customize), and for an included
# skill it skips "a path of this form that matches no file", which it tests against the files of
# the commit that it reads, name by name and in case.
#
# The syntaxes of the tools that open an importing file and each import by joining the name, as
# written, to a directory. Claude Code 2.1.303 joins `CLAUDE.md`, `.claude/CLAUDE.md` and
# `CLAUDE.local.md` to each directory that it searches, resolves an import with `at(H,kC(n))` and
# reads it with `e.stat(n)` and `e.readFile(n,{encoding:"utf8"})`. Gemini CLI calls `fs.access`
# on the directory joined with GEMINI.md, and on `path.resolve(basePath, importPath)` before
# `fs.readFile(fullPath, 'utf-8')` (packages/core/src/utils/memoryImportProcessor.ts). A file
# system that ignores case therefore opens a root `claude.md` or `gemini.md` for these tools, and
# the `root_any_case` rows read it (`ignores_case`). Copilot CLI matches the root file in any
# case also on Linux, and its chains compare case.
_JOINED_NAMES = frozenset({"claude_at", "gemini_at"})


def ignores_case(row: dict) -> bool:
    """Whether the tool reads the importing files of `row` only on a file system that ignores
    case, which also opens each import in any case: a `root_any_case` row of a syntax of
    `_JOINED_NAMES`. Such a row starts a chain only at a root file in another case than the
    tool's own name, because an earlier row of the same tool and syntax takes that name, as
    claude-import-01 takes CLAUDE.md (`importing_files`)."""
    return row["match_mode"] == "root_any_case" and row["syntax"] in _JOINED_NAMES


def resolve(base: str, path: str, as_written: bool = False) -> tuple[str, bool]:
    """`(target, inside)`: the repository-relative path that an import of `path` names,
    resolved against the directory `base` and lexically normalized, and whether it stays inside
    the repository. A path that leaves it is returned as it resolves, such as `~/notes.md` or
    `../x.md`. With `as_written`, a path that starts with a backslash leaves the repository,
    and a trailing `/` stays, so that the target names a directory."""
    if (path.startswith(("~", "/")) or re.match(r"[A-Za-z]:[\\/]", path)
            or (as_written and path.startswith("\\"))):
        return path, False
    target = posixpath.normpath(posixpath.join(base, path))
    if target == ".." or target.startswith("../"):
        return target, False
    if as_written and path.endswith("/") and target != ".":
        target += "/"
    return target, True


def _utf8(path: Path) -> bool:
    """Whether a file's content is valid UTF-8."""
    try:
        path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return True


def status_of(row: dict, target: str, inside: bool, tree) -> str:
    """The `target_status` of a resolved import, without the limits of the chain, in `tree`.
    A file that is not there is missing whatever its extension, because Claude Code 2.1.303
    skips a path that it cannot open before it compares the extension of a file that it read. A
    file with a path that differs in case from the entries of its directories is missing too,
    and a chain that `ignores_case` passes the path as the directories spell it (`chain`). A
    stub that the tool would read is `not_copied`, never `found` (`CollectedTrees.stubbed`)."""
    if not inside:
        return OUTSIDE
    if is_prunable(target):
        return PRUNED
    if target == "." or target.endswith("/") or paths.in_git_directory(target):
        return MISSING
    path = tree.locate(target)
    if path is None or not tree.spelled(target):
        return MISSING
    if not extensions_read(row, target):
        return NOT_READ
    if "utf8" in conditions(row):
        # A file larger than the row's limits is `over_limit` in the chain, and its content is
        # not read to decide that.
        limits = [int(row[column]) for column in ("max_file_bytes", "max_bytes") if row.get(column)]
        if (not limits or path.stat().st_size <= min(limits)) and not _utf8(path):
            return NOT_READ
    return NOT_COPIED if tree.stubbed(target) else FOUND


@dataclass(frozen=True)
class Record:
    """One import of a chain: the row that reads it, the importing file at the start of the
    chain, the file that contains the import, its hop, counted from 1 for an import written in
    `root`, and what it reaches."""
    heuristic: str
    tool: str
    root: str
    importer: str
    depth: int
    written: str
    line: int
    target: str
    status: str


def chain(row: dict, root: str, tree) -> list:
    """Every import of the chain that starts at `root`, in the order in which the tool reads
    them: each file's imports in the order of its text, and the imports of a file right after
    the import that reaches it. `tree` reads the files (`CloneTree`, `CollectedTrees`).

    A file is read once per chain, at the first import that reaches it, and its own imports are
    read where that import is above `max_depth`. A later import of a file that the chain has
    read, also through a link to it, is recorded and not read again, which also stops a cycle.
    Once the chain has read `max_files` files, or a file would take the bytes read beyond
    `max_bytes`, an import of a further file is `over_limit`, and a later smaller file can still
    fit. With the condition `importing_file_bytes`, the bytes of `root` count toward
    `max_bytes` from the start. A file larger than `max_file_bytes`, or than the import's own
    `max_file_bytes`, is `over_limit` as well, and so is a file that a `past_count` import
    names. A file that an import without `through_links` reaches through a link is `not_read`.
    An `inner` import after an import of the same word that named a file is no import and has
    no record, and an import of a path that no file can have (`nameable`) is `missing`,
    shortened.

    With the condition `each_branch`, the tool reads a file again for each import that reaches
    it, and the files that it reads grow exponentially with the depth where files
    import each other. The chain records the imports of each file once, breadth first, at the
    smallest depth at which the tool reads the file. It thereby records every import that the
    tool reads and every file that it reaches, without the repetitions.

    A chain of a row that `ignores_case` looks up each name of a target as written, and otherwise
    as the one entry of its directory that matches it ignoring case (`spelling`), and records the
    target as the directories spell it. Every other chain finds a target only in its case.
    """
    parse = PARSERS[row["syntax"]]
    as_written = row["syntax"] in _AS_WRITTEN
    folded = ignores_case(row)
    max_depth = int(row["max_depth"])
    limits = {column: int(row[column]) if row.get(column) else None for column in LIMIT_COLUMNS}
    each_branch = "each_branch" in conditions(row)
    # The files that the chain has read: with `each_branch` by the path, as Gemini CLI keeps its
    # processed files, and otherwise by the path that the links lead to.
    records, reached, read = [], {root if each_branch else tree.real(root)}, {"files": 0,
                                                                              "bytes": 0}
    if "importing_file_bytes" in conditions(row):
        read["bytes"] = tree.size(root) or 0

    def base(importer: str) -> str:
        real = (row["resolution"] == "real_dir"
                or (row["resolution"] == "imported_real_dir" and importer != root))
        return posixpath.dirname(tree.real(importer) if real else importer)

    def admitted(target: str, file_limit: int | None) -> bool:
        """Whether the limits, with `file_limit` for the size of this file, let the chain read
        `target`, which it then counts."""
        size = tree.locate(target).stat().st_size
        if ((file_limit is not None and size > file_limit)
                or (limits["max_file_bytes"] is not None and size > limits["max_file_bytes"])
                or (limits["max_files"] is not None and read["files"] >= limits["max_files"])
                or (limits["max_bytes"] is not None
                    and read["bytes"] + size > limits["max_bytes"])):
            return False
        read["files"] += 1
        read["bytes"] += size
        return True

    def readings(importer: str, depth: int):
        """Record each import of `importer`, and yield each file that the chain reads next,
        right after the record of the import that reaches it."""
        content = tree.text(importer)
        if content is None:
            return
        directory = base(importer)
        word_named_a_file = False
        for each in parse(content):
            if not each.inner:
                word_named_a_file = False
            elif word_named_a_file:
                continue
            if each.too_long or not nameable(each.path):
                records.append(Record(row["id"], row["tool"], root, importer, depth,
                                      shown(each.written), each.line, shown(each.path), MISSING))
                continue
            target, inside = resolve(directory, each.path, as_written)
            if folded and inside and target != ".":
                target = tree.spelling(target) or target
            status = status_of(row, target, inside, tree)
            if status in (FOUND, NOT_COPIED):
                if not each.through_links and tree.real(target) != target:
                    status = NOT_READ
                elif each.past_count:
                    status = OVER_LIMIT
            word_named_a_file = status in (FOUND, NOT_READ, OVER_LIMIT, NOT_COPIED)
            key = target if each_branch else tree.real(target)
            # A stub that the tool would read counts toward the limits as the file would, with
            # the size of the stub, and its imports are not read, because the stub has none.
            follow = status in (FOUND, NOT_COPIED) and key not in reached
            if follow:
                if admitted(target, each.max_file_bytes):
                    reached.add(key)
                else:
                    status, follow = OVER_LIMIT, False
            records.append(Record(row["id"], row["tool"], root, importer, depth,
                                  each.written, each.line, target, status))
            if follow and status == FOUND and depth < max_depth:
                yield target

    if each_branch:
        queue = collections.deque([(root, 1)])
        while queue:
            importer, depth = queue.popleft()
            queue.extend((target, depth + 1) for target in readings(importer, depth))
    else:
        def expand(importer: str, depth: int) -> None:
            for target in readings(importer, depth):
                expand(target, depth + 1)

        expand(root, 1)
    return records


# ---------------------------------------------------------------------------
# The importing files
# ---------------------------------------------------------------------------

_CLAUDE_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md")


def _directory_of(path: str) -> str:
    """The directory that a context file belongs to: its own, or the one above a `.claude`
    that contains it."""
    directory = posixpath.dirname(path)
    if posixpath.basename(directory) == ".claude":
        directory = posixpath.dirname(directory)
    return directory


def _root_claude_files(files: set, ignoring_case: bool) -> set:
    """The CLAUDE files of `files` at the root, in their case, and with `ignoring_case` in any
    case, as a file system that ignores case opens them for Claude Code."""
    if not ignoring_case:
        return {name for name in _CLAUDE_FILES if name in files}
    names = {name.lower() for name in _CLAUDE_FILES}
    return {path for path in files if path.lower() in names}


def deciding_claude_files(files) -> set:
    """The CLAUDE files of `files`, repository-relative paths, that decide whether Claude Code
    reads an AGENTS.md of `files` (`_no_claude_md`): the files at the root, where an AGENTS.md
    is, and the files in the directory of an AGENTS.md. For a root AGENTS.md in another case,
    which Claude Code reads only on a file system that ignores case (`ignores_case`), the files
    at the root count in any case. Such a file keeps Claude Code from reading the AGENTS.md
    even when it is empty."""
    present = set(files)
    found = set()
    for path in present:
        name = posixpath.basename(path)
        if name != "AGENTS.md" and not (posixpath.dirname(path) == "" and name.lower() == "agents.md"):
            continue
        found |= _root_claude_files(present, ignoring_case=name != "AGENTS.md")
        found.update(posixpath.join(_directory_of(path), claude) for claude in _CLAUDE_FILES
                     if posixpath.join(_directory_of(path), claude) in present)
    return found


def _no_claude_md(path: str, files: set, ignoring_case: bool = False) -> bool:
    """Whether no CLAUDE.md, .claude/CLAUDE.md or CLAUDE.local.md is at the repository root,
    the working directory that the dataset reads from, or in the directory of `path`. A CLAUDE
    file of a directory between them leaves the AGENTS.md of the directory below it read. With
    `ignoring_case`, for a chain that a file system that ignores case reads, a file at the root
    counts in any case, such as a `claude.md` that such a file system opens as CLAUDE.md."""
    return not (_root_claude_files(files, ignoring_case)
                or any(posixpath.join(_directory_of(path), name) in files
                       for name in _CLAUDE_FILES))


def _matches(row: dict, path: str) -> bool:
    """Whether the importing file of `row` is at `path`, by the row's match mode. A `file` row names
    a path from the repository root and from every directory below it, as the detection step reads a
    fixed path below the root. A path under `.github` is the exception: it is read at the root
    alone, with the name of the file in any case and `.github` in lower case, as Copilot CLI reads
    it. A `root_file` row names its path at the root alone, in the case that the row gives, as the
    collector copies the file. A `root_any_case` row names a file at the root in any case, and a
    directory row its directory at any depth."""
    mode, pattern = row["match_mode"], row["path"]
    if mode == "file":
        if pattern.startswith(".github/"):
            return path.startswith(".github/") and path.lower() == pattern.lower()
        return path == pattern or path.endswith("/" + pattern)
    if mode == "root_file":
        return path == pattern
    if mode == "root_any_case":
        return "/" not in path and path.lower() == pattern.lower()
    if mode == "basename":
        return posixpath.basename(path) == pattern
    if mode.startswith(("tree_ext:", "dir_ext_triggered:")):
        segments = path.split("/")
        return any(heuristics.heuristic_matches({"match_mode": mode, "path": pattern},
                                                "/".join(segments[depth:]))
                   for depth in range(len(segments)))
    return False


@functools.cache
def _config_rows() -> tuple:
    return tuple(heuristics.load_config_path_patterns())


def config_row(row: dict) -> dict | None:
    """The row of `config_path_patterns.csv` with the path and the match mode of an import row,
    which names the same files, or None."""
    for found in _config_rows():
        if (found["path"], found["match_mode"]) == (row["path"], row["match_mode"]):
            return found
    return None


def rule_trigger(text: str) -> str | None:
    """The trigger of `paths.RULE_TRIGGERS` that a rule's YAML front matter sets, once, read as
    `paths.has_trigger` reads it, or None. The function is here rather than in `paths.py`, so that a
    change to the imports leaves the code of the collection of the configuration artifacts as it is
    (`reuse.collector_files`)."""
    found = paths.front_matter(text)
    keys = paths.top_level_keys(*found, "trigger") if found is not None else []
    if len(keys) != 1:
        return None
    value = re.sub(r"(^|\s)#.*$", "", keys[0][1]).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value if value in paths.RULE_TRIGGERS else None


def importing_files(rows: list, root: Path, files: list, tree,
                    vendored_dirs=frozenset()) -> list:
    """`[(row, path)]`: every file of `files`, the repository-relative paths of the tree at
    `root`, that a row names as an importing file, in the order of the rows and then of the
    paths. A file starts one chain per tool and syntax, of the first row that names it. `tree`
    reads a file for its trigger."""
    present = set(files)
    found, taken = [], set()
    plugins = nested = None
    for row in rows:
        mode, pattern = row["match_mode"], row["path"]
        if heuristics.nested_inner(mode) is not None:
            # A place below the root that the configuration row reads, where
            # `paths.nested_places` finds it, such as a package's .github/copilot-instructions.md.
            nested = paths.nested_places(root, vendored_dirs) if nested is None else nested
            places = [rel for rel, place_row in nested
                      if (place_row["path"], place_row["match_mode"]) == (pattern, mode)]
            candidates = [path for path in files
                          if any(path == place or path.startswith(place + "/") for place in places)
                          and heuristics.heuristic_matches({"match_mode": mode, "path": pattern},
                                                           path)]
        elif mode.startswith(("declared_names:", "declared_local:")):
            named = {rel for rel, _row in paths.declared_context_files(
                root, vendored_dirs, [config_row(row)])}
            candidates = [path for path in files if path in named]
        elif mode == "declared_rules":
            named = {rel for rel, _row in paths.declared_rule_files(
                root, vendored_dirs, [config_row(row)])}
            candidates = [path for path in files if path in named]
        elif pattern.startswith("<"):
            plugins = paths.plugin_roots(root, vendored_dirs) if plugins is None else plugins
            token, _, rest = pattern.partition("/")
            places = [f"{base}/{rest}" if base else rest for base in plugins.get(token, [])]
            candidates = [path for path in files if any(heuristics.heuristic_matches(
                {"match_mode": mode, "path": place}, path) for place in places)]
        else:
            candidates = [path for path in files if _matches(row, path)]
        for path in candidates:
            if "no_claude_md" in conditions(row) and not _no_claude_md(path, present,
                                                                       ignores_case(row)):
                continue
            if mode.startswith("dir_ext_triggered:") and not paths.has_trigger(
                    tree.text(path) or ""):
                continue
            if ("automatic_trigger" in conditions(row)
                    and rule_trigger(tree.text(path) or "") not in AUTOMATIC_TRIGGERS):
                continue
            key = (row["tool"], row["syntax"], path)
            if key not in taken:
                taken.add(key)
                found.append((row, path))
    return found


# ---------------------------------------------------------------------------
# The clone, for the cloning step
# ---------------------------------------------------------------------------

_MAX_LINK_HOPS = 8


def _under(rel: str, gitlinks: frozenset) -> bool:
    """Whether the repository-relative path `rel` is a gitlink or below one."""
    return any(rel == link or rel.startswith(link + "/") for link in gitlinks)


def regular_file(root: Path, rel: str, gitlinks: frozenset = frozenset()) -> Path | None:
    """The real path of the regular file that `rel` names in the directory `root`, after every
    link on its way, or None where the path leads out of `root`, into `.git` or a gitlink, is
    no regular file, or has a name that is not valid UTF-8. No CSV can hold such a name, and the
    cloning step removes it from the collected tree (`paths.remove_undecodable_names`)."""
    if "\0" in rel or _under(rel, gitlinks):
        return None
    try:
        real_root = Path(root).resolve()
        real = (Path(root) / rel).resolve()
        if not real.is_relative_to(real_root):
            return None
        inner = real.relative_to(real_root).as_posix()
        inner.encode("utf-8")
        if paths.in_git_directory(inner) or _under(inner, gitlinks):
            return None
        return real if stat.S_ISREG(real.stat().st_mode) else None
    except (OSError, RuntimeError, ValueError):
        return None


def read_text(path: Path) -> str | None:
    """A file's text, decoded as UTF-8 with every invalid byte replaced, or None."""
    try:
        return path.read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


class CloneTree:
    """A clone, as the tool reads it: through the links inside the repository, and not into
    `.git` or a gitlink."""

    def __init__(self, root: Path, gitlinks=()):
        self.root, self.gitlinks = Path(root), frozenset(gitlinks)

    def locate(self, rel: str) -> Path | None:
        return regular_file(self.root, rel, self.gitlinks)

    def real(self, rel: str) -> str:
        path = self.locate(rel)
        return path.relative_to(self.root.resolve()).as_posix() if path is not None else rel

    def text(self, rel: str) -> str | None:
        path = self.locate(rel)
        return None if path is None else read_text(path)

    def size(self, rel: str) -> int | None:
        """The size in bytes of the file that `text` reads, or None."""
        path = self.locate(rel)
        return None if path is None else path.stat().st_size

    def spelled(self, rel: str) -> bool:
        """Whether each name of `rel` is an entry of the directory above it, in its case too."""
        return _spelled(self.root, rel)

    def spelling(self, rel: str) -> str | None:
        """`rel` as a file system that ignores case opens it (`_spelling`)."""
        return _spelling(self.root, rel)

    def stubbed(self, rel: str) -> bool:
        """Whether the file at `rel` is a stub, which a clone never holds."""
        return False


def _spelled(root: Path, rel: str) -> bool:
    """Whether each name of `rel` is an entry of the directory above it in the directory `root`,
    compared in its case. A file system that ignores case does not compare the case when it
    opens the path."""
    names = rel.split("/")
    try:
        return all(name in os.listdir(Path(root).joinpath(*names[:number]))
                   for number, name in enumerate(names))
    except OSError:
        return False


def _spelling(root: Path, rel: str) -> str | None:
    """`rel` with each name as the directory above it in the directory `root` spells it: the
    entry of that name, and otherwise the one entry that matches the name ignoring case, which a
    file system that ignores case opens. None where a name matches no entry or several, which
    such a file system cannot hold side by side."""
    spelled = []
    for name in rel.split("/"):
        try:
            entries = os.listdir(Path(root).joinpath(*spelled))
        except OSError:
            return None
        if name not in entries:
            matching = [entry for entry in entries if entry.lower() == name.lower()]
            if len(matching) != 1:
                return None
            name = matching[0]
        spelled.append(name)
    return "/".join(spelled)


# The suffix of the list of the stubs of a repository's directory of the imported tree. The list
# is beside the directory rather than in it, because a file of the repository can have any name.
STUB_LIST_SUFFIX = ".stubs.json"


def stub_list_path(imported: Path) -> Path:
    """The list of the stubs that `collect_imports` wrote into `imported`, a repository's
    directory of the imported tree: `<owner§repo>.stubs.json` beside the directory, a JSON array
    of the stubs' repository-relative paths after their links."""
    imported = Path(imported)
    return imported.with_name(imported.name + STUB_LIST_SUFFIX)


def read_stubs(imported: Path) -> frozenset:
    """The paths of the stubs in `imported`, a repository's directory of the imported tree, that
    its list names (`stub_list_path`). A directory without a list holds no stub, because the
    cloning step writes the list wherever it writes a stub. Raises ValueError for a list that is
    no JSON array of paths."""
    path = stub_list_path(imported)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset()
    try:
        stubs = json.loads(text)
    except ValueError as error:
        raise ValueError(f"{path} is not JSON ({error})") from error
    if not isinstance(stubs, list) or not all(isinstance(stub, str) for stub in stubs):
        raise ValueError(f"{path} is not a JSON array of repository-relative paths")
    return frozenset(stubs)


def collect_imports(clone: Path, collected: Path, imported: Path, rows: list | None = None,
                    gitlinks=(), vendored_dirs=frozenset()) -> int:
    """Copy every file that an import of a copied file reaches in the clone into `imported`,
    the repository's directory of the imported tree, and return how many files it copied.

    The importing files are the files of `collected`, the repository's directory of the
    collected tree, that a row of `rows`, by default the import heuristics, names. Each is read
    in the clone, where the tool reads it, and each import is resolved as the row states, up to
    `max_depth` hops. A target that the tool reads is copied where it is a file inside the
    repository, outside `.git`, the gitlinks of `gitlinks` and what the archive step prunes,
    also where the collection copied it too.

    A target that no chain reads, for its extension, its content, its size or a link on its
    path (`NOT_READ`, `OVER_LIMIT`), is a stub that keeps what decides its status
    (`_write_stub`). The import detection step, which reads the imported tree, then gives it the
    status that it has in the clone without the tree holding a file of any size. The list
    beside `imported` names each stub (`stub_list_path`), so that a stub that other import
    heuristics read is never taken for the file.

    A file is copied to the path that its links lead to, and a path that leads there through a
    link is a link in the imported tree as well, relative and inside it. An importing file that
    is a link in the clone and contains an import is such a link too, so that the import
    detection step reads the text that the tool read and resolves its imports where the tool
    resolves them.
    """
    rows = import_patterns() if rows is None else rows
    collected, imported = Path(collected), Path(imported)
    # The list of an earlier collection, whose directory the cloning step has removed.
    stub_list_path(imported).unlink(missing_ok=True)
    if not collected.is_dir():
        return 0
    tree = CloneTree(clone, gitlinks)
    # Without a name that is not valid UTF-8, which the cloning step then removes from the
    # collected tree and which the imported tree cannot hold either.
    files = [rel for rel in paths.tree_files(collected) if not paths.undecodable_name(rel)]
    read, unread = set(), set()
    for row, root in importing_files(rows, tree.root, files, tree, vendored_dirs):
        records = chain(row, root, tree)
        if records and tree.real(root) != root and tree.locate(root) is not None:
            read.add(root)
        read.update(record.target for record in records if record.status == FOUND)
        unread.update(record.target for record in records
                      if record.status in (NOT_READ, OVER_LIMIT))
    whole = {tree.real(rel) for rel in read}
    cap = max((int(row[column]) for row in rows for column in LIMIT_COLUMNS if row[column]),
              default=0)
    copied = set()
    for rel in sorted(read | unread):
        real = tree.real(rel)
        source = tree.locate(rel)
        destination = imported / real
        if real not in copied:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if real in whole:
                shutil.copy2(source, destination)
            else:
                _write_stub(source, destination, cap)
            copied.add(real)
        if real != rel and not os.path.lexists(imported / rel):
            link = imported / rel
            link.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(posixpath.relpath(real, posixpath.dirname(rel) or "."), link)
    stubs = sorted(copied - whole)
    if stubs:
        # In one step, synced: a list that a power loss left empty would stop the import
        # detection step at every Start.
        caches.replace_file(stub_list_path(imported), json.dumps(stubs) + "\n")
    return len(copied)


def _write_stub(source: Path, destination: Path, cap: int) -> None:
    """Write at `destination` a file to which the chains give the status of `source`, a file
    that no chain reads, without its bytes. The stub has the size of `source`, or one byte more
    than `cap`, the largest limit of a row, where `source` is larger. Its first byte is not
    UTF-8 where the text of `source` is not valid UTF-8, and its path keeps the extension. Every
    row that requires valid UTF-8 has a limit, and the text of a file larger than `cap`
    therefore decides nothing."""
    size = source.stat().st_size
    with open(destination, "wb") as f:
        if size <= cap and not _utf8(source):
            f.write(b"\xff")
        f.truncate(min(size, cap + 1))


# ---------------------------------------------------------------------------
# The validated tree and the imported tree, for the import detection step
# ---------------------------------------------------------------------------

class CollectedTrees:
    """A repository's directory of the validated tree and of the imported tree, as the import
    detection step reads them.

    A target is a file that the tool reads where the imported tree has it, through the links
    that the cloning step wrote there, because the cloning step decided in the clone what the
    tool reads. An importing file that is a link in the imported tree is read through the link,
    as the text that the tool read. An importing file that is a file there, copied whole or as
    a stub, is read from the validated tree where the validated tree holds the path, because a
    stub has no text. Otherwise the file is read from the imported tree or from the validated
    tree. In the validated tree, a symlink marker is followed to the file that it names in
    either tree, as the tool reads a file through its link (`paths.marker_target`). A stub that
    the list beside the imported tree names (`read_stubs`) stands for a file that the cloning
    step did not copy.
    """

    def __init__(self, validated: Path, imported: Path):
        self.validated, self.imported = Path(validated), Path(imported)
        self.stubs = read_stubs(self.imported)

    def locate(self, rel: str) -> Path | None:
        return regular_file(self.imported, rel)

    def stubbed(self, rel: str) -> bool:
        """Whether the file that `rel` leads to in the imported tree is a stub of the list."""
        return bool(self.stubs) and self.locate(rel) is not None and self.real(rel) in self.stubs

    def _followed(self, rel: str) -> str:
        """The path that a chain of symlink markers at `rel` in the validated tree leads to, or
        `rel` where the file at `rel` is no marker of a file that either tree contains."""
        current, seen = rel, {rel}
        for _ in range(_MAX_LINK_HOPS):
            if self.locate(current) is not None:
                return current
            path = regular_file(self.validated, current)
            if path is None:
                return current
            content = read_text(path)
            target = (paths.marker_target(content, path.parent) if content is not None
                      else None)
            if target is None:
                return current
            following = posixpath.normpath(posixpath.join(posixpath.dirname(current), target))
            if (following == ".." or following.startswith("../") or following in seen
                    or (self.locate(following) is None
                        and regular_file(self.validated, following) is None)):
                return current
            seen.add(following)
            current = following
        return current

    def real(self, rel: str) -> str:
        path = self.locate(rel)
        if path is not None:
            return path.relative_to(self.imported.resolve()).as_posix()
        return self._followed(rel)

    def _source(self, rel: str) -> Path | None:
        """The file in either tree with the text of `rel`, or None."""
        path = self.locate(rel)
        if path is None:
            followed = self._followed(rel)
            found = self.locate(followed)
            if found is not None:
                return found
            found = regular_file(self.validated, followed)
            # The marker of a link with a target that neither tree holds, such as a target
            # outside the repository, is no text that the tool reads, because the clone's link
            # led to another file.
            if found is not None and found.stat().st_size <= paths.MARKER_MAX_BYTES:
                content = read_text(found)
                if content is not None and paths.link_marker(content):
                    return None
            return found
        if not os.path.islink(self.imported / rel):
            # A file, and no link, at `rel` in the imported tree is the clone's file at `rel`,
            # copied whole or as a stub without its text, and the validated tree holds the file
            # itself wherever it holds the path.
            return regular_file(self.validated, rel) or path
        return path

    def text(self, rel: str) -> str | None:
        path = self._source(rel)
        return None if path is None else read_text(path)

    def size(self, rel: str) -> int | None:
        """The size in bytes of the file that `text` reads, or None."""
        path = self._source(rel)
        return None if path is None else path.stat().st_size

    def spelled(self, rel: str) -> bool:
        """Whether each name of `rel` is an entry of the directory above it in the imported
        tree, where a target is, in its case too."""
        return _spelled(self.imported, rel)

    def spelling(self, rel: str) -> str | None:
        """`rel` as a file system that ignores case opens it in the imported tree, where a
        target is (`_spelling`)."""
        return _spelling(self.imported, rel)


# ---------------------------------------------------------------------------
# Parsing: one function per syntax
# ---------------------------------------------------------------------------
# Each parser takes a file's text and returns its imports in the order of the text. A masked
# character, in code that the tool skips, becomes a space, so that offsets and lines stay.

def _mask(text: str, start: int, end: int) -> str:
    """`text` with the characters from `start` to `end` replaced by spaces, newlines kept."""
    return text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]


class _Lines:
    """The line, counted from 1, of each offset of a text, in time logarithmic in its lines."""

    def __init__(self, text: str):
        self.starts = [0, *(match.end() for match in re.finditer("\n", text))]

    def of(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


class _BacktickRuns:
    """The maximal runs of backticks of a text, to find the next run of a length after an
    offset in time logarithmic in the runs."""

    def __init__(self, text: str):
        self.starts = {}
        for match in re.finditer(r"`+", text):
            self.starts.setdefault(len(match.group()), []).append(match.start())

    def next_of(self, length: int, offset: int) -> int | None:
        """The start of the first run of `length` backticks that starts at `offset` or after."""
        starts = self.starts.get(length, [])
        index = bisect.bisect_left(starts, offset)
        return starts[index] if index < len(starts) else None


# A mail link and a mail address name a person, a team or a mailbox, and no file. The tools read
# an `@` in them as any other `@`: In a test workspace, Copilot CLI 1.0.101 read a file
# `octocat](mailto:octocat@example.com` for `[@octocat](mailto:octocat@example.com)`, and Claude
# Code reads the text of a link as it reads other text. As an import, such an `@` would add a
# `missing` row for a file that nobody named. The parsers of `@path` therefore start no import at an
# `@` that `_mail_ats` finds (the section "Imports" of docs/heuristics.md). A link text has no
# bracket and no blank line, and `mailto:` is read in any case, as the scheme of a URI is.
_LINK_TEXT = r"\[((?:\\.|[^\[\]\\\n]|\n(?![ \t]*(?:\n|$)))*)\]"
_MAIL_LINK = re.compile(_LINK_TEXT + r"\(\s*<?mailto:", re.IGNORECASE)
_MAIL_DEFINITION = re.compile(r"^[ \t]*(?:(?:>|[-+*]|\d{1,9}[.)])[ \t]*)*" + _LINK_TEXT
                              + r":[ \t]*(?:\n[ \t]*(?:>[ \t]*)*)?<?mailto:",
                              re.IGNORECASE | re.MULTILINE)
# A reference link, full, collapsed or shortcut, and not an inline link or a definition.
_REFERENCE_LINK = re.compile(_LINK_TEXT + r"(?:\[((?:\\.|[^\[\]\\])*)\]|(?![(:]))")
_MAILTO = re.compile(r"mailto:[^\s<>()\[\]\"'`]*", re.IGNORECASE)
# An autolink of a mail address, as `_AUTOLINK` reads one.
_MAIL_AUTOLINK = re.compile(r"<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>")


def _mail_ats(text: str) -> frozenset:
    """The offsets of the `@` of `text` in a mail link or a mail address: in the text of a
    Markdown link to a `mailto:` URI, inline or by reference, in the label of the definition of
    such a reference, in a `mailto:` URI and in an autolink of a mail address, such as
    `<octocat@example.com>`."""
    if "@" not in text:
        return frozenset()
    spans = [match.span() for match in _MAIL_AUTOLINK.finditer(text)]
    if _MAILTO.search(text):
        spans += [match.span() for match in _MAILTO.finditer(text)]
        spans += [match.span(1) for match in _MAIL_LINK.finditer(text)]
        labels = set()
        for match in _MAIL_DEFINITION.finditer(text):
            labels.add(_label(match.group(1)))
            spans.append(match.span(1))
        if labels:
            spans += [match.span(1) for match in _REFERENCE_LINK.finditer(text)
                      if _label(match.group(2) or match.group(1)) in labels]
    return frozenset(at for start, end in spans for at in range(start, end) if text[at] == "@")


# Copilot CLI: the native resolver of the release that the section "Imports" of docs/heuristics.md
# names, run on test files. A fence opens on a line that starts with at least three backticks or
# tildes after any whitespace, unless another backtick follows a run of backticks on the line. The
# next line that starts with at least as many of the same character, with only whitespace after
# them, closes it, or the end of the file. Only a line feed ends a line. A run of backticks opens a
# code span that the next run of the same length on the same line closes, or the end of the line.
# Indented code, HTML comments, front matter, block quotes and list items are read as text.
_COPILOT_BLOCKED_BEFORE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                    "0123456789._%+-")
# Unicode's White_Space, which the resolver reads as whitespace. `str.isspace` also accepts the
# separators U+001C to U+001F, which the resolver reads as part of a path or as text after a fence.
_COPILOT_WHITESPACE = "\t\n\x0b\x0c\r \x85\xa0" + "".join(
    chr(c) for c in (0x1680, *range(0x2000, 0x200b), 0x2028, 0x2029, 0x202f, 0x205f, 0x3000))
_COPILOT_PATH_ENDS = frozenset("`'\"<>" + _COPILOT_WHITESPACE)
_COPILOT_TRAILING = ".,;:)!?]}"
_COPILOT_FENCE_LENGTH = 3


def _copilot_fence(line: str) -> tuple | None:
    """The character of the fence that `line` starts after its whitespace, the length of the run
    and the text after it, or None where the line starts with no such run."""
    stripped = line.lstrip(_COPILOT_WHITESPACE)
    character = stripped[:1]
    if character not in ("`", "~"):
        return None
    after = stripped.lstrip(character)
    length = len(stripped) - len(after)
    return (character, length, after) if length >= _COPILOT_FENCE_LENGTH else None


def _copilot_masked_line(line: str) -> str:
    """A line with its code spans masked, as Copilot CLI masks them."""
    if "`" not in line:
        return line
    runs, out, i = _BacktickRuns(line), [], 0
    while True:
        start = line.find("`", i)
        if start < 0:
            out.append(line[i:])
            break
        j = start
        while j < len(line) and line[j] == "`":
            j += 1
        closing = runs.next_of(j - start, j)
        end = len(line) if closing is None else closing + j - start
        out += [line[i:start], " " * (end - start)]
        i = end
    return "".join(out)


def copilot_at(text: str) -> list:
    """Copilot CLI's `@path`: an `@` that does not follow an ASCII letter, a digit or one of
    `._%+-`, and the characters after it up to whitespace, a backtick, a quote, `<` or `>`,
    without the trailing `.,;:)!?]}`. A path that then contains neither `/` nor `.` is a
    mention, such as `@octocat`, and so is a path that starts with `@` and contains no `/`.
    `@@a.md` therefore imports `a.md`. A `#fragment`, a `:line` and a trailing `/` stay part of
    the path.

    An `@` inside the path of an import starts another import only where that path contains no
    `/`, and the tool reads it only where no import before it in the word named a file. For
    example, `@x(@b.md` reads `b.md` where no file `x(@b.md` is (`Import.inner`), and `@x/@b.md`
    reads `x/@b.md` or nothing. The imports of a word that no file can have are one import
    (`Import.too_long`), so that a long word, such as a line of minified CSS, gives few
    imports.

    An `@` of a mail link or a mail address (`_mail_ats`) starts no import, and the next import
    of its word is read as the first, because the `@` before it names no file. For example,
    `[@octocat](mailto:octocat@example.com)` reads nothing, and `[@x](mailto:a@b.c)@d.md`
    reads `d.md`."""
    found, fence, mail, line_start = [], None, _mail_ats(text), 0
    for number, raw in enumerate(text.split("\n"), start=1):
        offset, line_start = line_start, line_start + len(raw) + 1
        run = _copilot_fence(raw)
        if fence is not None:
            if (run and run[0] == fence[0] and run[1] >= fence[1]
                    and not run[2].strip(_COPILOT_WHITESPACE)):
                fence = None
            continue
        if run and not (run[0] == "`" and "`" in run[2]):
            fence = run
            continue
        line = _copilot_masked_line(raw)
        columns = frozenset(at - offset for at in mail if offset <= at < offset + len(raw))
        start = 0
        while True:
            start = line.find("@", start)
            if start < 0:
                break
            # The rest of the word: up to a character that ends a path.
            end = start
            while end < len(line) and line[end] not in _COPILOT_PATH_ENDS:
                end += 1
            found += _copilot_word(line, start, end, number, columns)
            start = end
    return found


def _copilot_word(line: str, start: int, end: int, number: int, mail: frozenset) -> list:
    """The imports of the word of `line` that ends at `end`, from its first `@` at `start`. An
    `@` at a column of `mail` starts no import and leaves `inner` as it was, because an import
    that names no file lets the tool read the next import of the word."""
    stop = end
    while stop > start and line[stop - 1] in _COPILOT_TRAILING:
        stop -= 1
    last_slash, last_dot = line.rfind("/", start, stop), line.rfind(".", start, stop)
    found, inner = [], False
    at = start
    while 0 <= at < stop:
        if (not at or line[at - 1] not in _COPILOT_BLOCKED_BEFORE) and at + 1 < stop:
            slash = last_slash > at
            starts = slash or (line[at + 1] != "@" and last_dot > at)
            if starts and at in mail:
                # A path with a `/` holds the rest of the word, which then starts no import.
                if slash:
                    break
            elif starts:
                path = line[at + 1:stop] if stop - at - 1 <= PATH_MAX else None
                if path is None or not nameable(path):
                    if not (found and found[-1].too_long):
                        cut = line[at + 1:at + 1 + SHOWN + 1]
                        found.append(Import(shown(f"@{cut}"), number, shown(cut), inner,
                                            too_long=True))
                else:
                    found.append(Import(f"@{path}", number, path, inner))
                if slash:
                    break
                inner = True
        at = line.find("@", at + 1, stop)
    return found


# Antigravity: `@[label](path)` "Inlines the target file's contents directly into the rule"
# (antigravity.google/docs/rules). Antigravity CLI read the path as written, without trimming,
# decoding or dropping a `#fragment`, in code blocks and code spans as well as in the text, after
# a backslash and in the middle of a word, with a label of any characters but `]`, also over
# several lines (the section "Imports" of docs/heuristics.md).
_ANTIGRAVITY_INCLUDE = re.compile(r"@\[[^\]]*\]\(([^)]+)\)")


def antigravity_include(text: str) -> list:
    """Antigravity's `@[label](path)` includes."""
    lines = _Lines(text)
    return [Import(match.group(0), lines.of(match.start()), match.group(1))
            for match in _ANTIGRAVITY_INCLUDE.finditer(text)]


# Gemini CLI: `findImports` and `findCodeRegions` of packages/core/src/utils/
# memoryImportProcessor.ts (the section "Imports" of docs/heuristics.md names the commit). The code
# regions are the backtick runs that the same run closes, over the whole text. A tilde fence,
# indented code and an HTML comment are read as text, and an import with its `@` inside a region is
# not one.
_GEMINI_SPACE = " \t\n\r"
_GEMINI_CODE = re.compile(r"(`+)([\s\S]*?)\1")


def gemini_at(text: str) -> list:
    """Gemini CLI's `@file`: an `@` at the start of the text or after a space, a tab or a line
    break, and the characters after it up to the next of these, where the path starts with
    `.`, `/` or an ASCII letter. Trailing punctuation and a `#fragment` stay part of the
    path, and a file imported twice is read twice. An `@` of a mail link or a mail address
    (`_mail_ats`) starts no import, such as the `@` of `[write to @a.md](mailto:x@y.md)`."""
    regions = [match.span() for match in _GEMINI_CODE.finditer(text)]
    region_starts = [start for start, _end in regions]
    lines, found, i, mail = _Lines(text), [], 0, _mail_ats(text)
    while True:
        i = text.find("@", i)
        if i < 0:
            return found
        if i and text[i - 1] not in _GEMINI_SPACE:
            i += 1
            continue
        j = i + 1
        while j < len(text) and text[j] not in _GEMINI_SPACE:
            j += 1
        path = text[i + 1:j]
        region = bisect.bisect_right(region_starts, i) - 1
        if (path and (path[0] in "./" or ("a" <= path[0].lower() <= "z" and path[0].isascii()))
                and not (region >= 0 and i < regions[region][1]) and i not in mail):
            found.append(Import(f"@{path}", lines.of(i), path))
        i = j + 1


# Markdown as Claude Code reads it: the `marked` lexer that it bundles, with `gfm` off. The
# functions below mask what the lexer makes into code or HTML, and the import finder reads the
# rest as `VLo` reads the text of each token. They follow CommonMark's block and inline rules
# where marked follows them, and tests/test_imports.py records each place where they differ from
# the bundled code.
_JS_SPACE = frozenset("\t\n\v\f\r       　﻿"
                      + "".join(chr(c) for c in range(0x2000, 0x200b)))
_FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_LIST_ITEM = re.compile(r"^( {0,3})([-+*]|\d{1,9}[.)])( {1,4}|\t|$)")
_BLOCKQUOTE = re.compile(r"^ {0,3}> ?")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?=[ \t]|$)")
_HTML_BLOCK_TAGS = frozenset(
    "address article aside base basefont blockquote body caption center col colgroup dd "
    "details dialog dir div dl dt fieldset figcaption figure footer form frame frameset h1 h2 "
    "h3 h4 h5 h6 head header hr html iframe legend li link main menu menuitem nav noframes ol "
    "optgroup option p param search section summary table tbody td tfoot th thead title tr "
    "track ul".split())
# marked's own forms of the HTML blocks, which accept `_` in a tag name and spaces alone
# between the attributes.
_HTML_OPEN = re.compile(r"^ {0,3}<(script|pre|style|textarea)(?=[\s>]|$)", re.IGNORECASE)
_HTML_TAG_START = re.compile(r"^ {0,3}</?([A-Za-z][A-Za-z0-9-]*)(?= +|/?>|$)")
_HTML_LINE_TAG = re.compile(r"^ {0,3}(?:<(?!(?:script|pre|style|textarea)\b)[A-Za-z][\w-]*"
                            r"(?: +[A-Za-z:_][\w.:-]*(?: *= *\"[^\"\n]*\"| *= *'[^'\n]*'"
                            r"| *= *[^\s\"'=<>`]+)?)*? */?>"
                            r"|</(?!(?:script|pre|style|textarea)\b)[A-Za-z][\w-]*\s*>)[ \t]*$",
                            re.IGNORECASE)
_INLINE_HTML = re.compile(r"<!--(?:-?>|[\s\S]*?-->)|<\?[\s\S]*?\?>|<![A-Za-z]+\s[\s\S]*?>"
                          r"|<!\[CDATA\[[\s\S]*?\]\]>"
                          r"|</[A-Za-z][\w:-]*\s*>"
                          r"|<[A-Za-z][\w-]*(?:\s+[A-Za-z:_][\w.:-]*(?:\s*=\s*\"[^\"]*\""
                          r"|\s*=\s*'[^']*'|\s*=\s*[^\s\"'=<>`]+)?)*?\s*/?>")
_AUTOLINK = re.compile(r"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*>"
                       r"|<[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+>")
_PUNCTUATION = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
# A backslash escape of a punctuation character, or a run of backticks.
_ESCAPE_OR_BACKTICKS = re.compile(r"\\[!-/:-@\[-`{-~]|`+")
_DEFINITION = re.compile(r"^ {0,3}\[((?:\\.|[^\[\]\\])+)\]:[ \t]*\S")
# A line that ends a list item where it is less indented than the item's content.
_ITEM_BREAK = re.compile(r"^ {0,3}(?:`{3,}|~{3,}|#{1,6}(?:\s|$)|<|[-+*](?:[ \t]|$)|\d{1,9}[.)]"
                         r"(?:[ \t]|$)|(?:(?:-[ \t]*){3,}|(?:_[ \t]*){3,}|(?:\*[ \t]*){3,})$)")
_LINK_DESTINATION = re.compile(r"\(\s*(?:<[^<>\n]*>|[^\s()]*(?:\([^\s()]*\)[^\s()]*)*)"
                               r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")


def _label(text: str) -> str:
    """A link label as marked compares it: in lower case, with runs of whitespace as one space."""
    return re.sub(r"\s+", " ", text.strip()).lower()


def _indent(line: str) -> int:
    """The columns of a line's leading spaces and tabs, a tab to the next multiple of four."""
    column = 0
    for char in line:
        if char == " ":
            column += 1
        elif char == "\t":
            column += 4 - column % 4
        else:
            break
    return column


def _strip_columns(line: str, columns: int) -> tuple[str, int]:
    """`line` without up to `columns` columns of leading whitespace, and how many characters
    that removed."""
    removed = column = 0
    while removed < len(line) and column < columns and line[removed] in " \t":
        column += 1 if line[removed] == " " else 4 - column % 4
        removed += 1
    return line[removed:], removed


def _html_block_end(kind: int, line: str) -> bool:
    """Whether a line ends an HTML block of the CommonMark kind `kind`, 1 to 5."""
    lower = line.lower()
    return {1: any(f"</{tag}>" in lower for tag in ("script", "pre", "style", "textarea")),
            2: "-->" in line, 3: "?>" in line, 4: ">" in line, 5: "]]>" in line}[kind]


def _html_block_start(line: str, in_paragraph: bool) -> int:
    """The CommonMark kind of an HTML block that starts at `line`, or 0."""
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return 0
    if _HTML_OPEN.match(line):
        return 1
    if stripped.startswith("<!--"):
        return 2
    if stripped.startswith("<?"):
        return 3
    if stripped.startswith("<![CDATA["):
        return 5
    if re.match(r"<![A-Za-z]", stripped):
        return 4
    tag = _HTML_TAG_START.match(line)
    if tag and tag.group(1).lower() in _HTML_BLOCK_TAGS:
        return 6
    if not in_paragraph and _HTML_LINE_TAG.match(line):
        return 7
    return 0


@dataclass
class _Line:
    """What the block pass decided about a line: `kind` is `text`, `code`, `html` (skipped),
    `comment` (an HTML block that starts with a comment), or `blank`; `start` the offset of its
    content in the line, after the markers of block quotes, list items and headings; `item`
    whether the line is text of a list item, which the lexer keeps as a text token of its own;
    `block` the number of the block that the line belongs to."""
    kind: str
    start: int
    item: bool
    block: int


def _markdown_lines(lines: list) -> list:
    """The block pass over the lines of a text: fenced and indented code, HTML blocks, block
    quotes, list items and the rest."""
    found = []
    fence = None       # (character, length, indent of its container, its block quotes)
    fence_opened = False  # whether the line before opened the fence
    html = None        # the kind of the HTML block that is open
    items = []         # the content columns of the open list items, innermost last
    in_paragraph = False
    block = 0
    for raw in lines:
        line = raw.rstrip("\r")
        start = quotes = 0
        quoted = _BLOCKQUOTE.match(line)
        # In a fence, only the block quotes that contain the fence are markers, and a `>` in
        # the code is code.
        while quoted and (fence is None or quotes < fence[3]):
            start += quoted.end()
            quotes += 1
            quoted = _BLOCKQUOTE.match(line[start:])
        content = line[start:]
        if fence is not None and fence[2] and content.strip() and _indent(content) < fence[2]:
            # marked continues a list item with a less indented line, and the fence in it with
            # the item, unless the line before opened the fence or the line starts a fence, a
            # heading, an HTML block, a bullet or a thematic break, which ends the item.
            if fence_opened or _ITEM_BREAK.match(content):
                fence = None
                items = [column for column in items if column <= _indent(content)]
        fence_opened = False
        if fence is not None:
            closing = _FENCE.match(_strip_columns(content, fence[2])[0])
            if (closing and closing.group(2)[0] == fence[0] and len(closing.group(2)) >= fence[1]
                    and not closing.group(3).strip()):
                fence = None
            found.append(_Line("code", start, False, block))
            continue
        if html is not None:
            kind = html
            if kind <= 5:
                if _html_block_end(kind, content):
                    html = None
            elif not content.strip():
                html = None
                found.append(_Line("blank", start, False, block))
                continue
            found.append(_Line("comment" if kind == 2 else "html", start, False, block))
            continue
        if not content.strip():
            found.append(_Line("blank", start, False, block))
            in_paragraph = False
            continue
        column = _indent(content)
        in_item = bool(items)
        # A less indented line continues the paragraph of a list item, unless it starts a block
        # in the item or the list that contains that item, such as a sibling item of a nested
        # list.
        while items and column < items[-1] and (
                not in_paragraph or _ITEM_BREAK.match(
                    _strip_columns(content, items[-2] if len(items) > 1 else 0)[0])):
            items.pop()
        base = items[-1] if items else 0
        rest, removed = _strip_columns(content, base)
        relative = _indent(rest)
        if relative >= 4 and not in_paragraph:
            found.append(_Line("code", start, False, block))
            continue
        marker = _LIST_ITEM.match(rest)
        # Only a list that starts with 1 interrupts a paragraph outside a list.
        if marker and not (in_paragraph and not in_item and marker.group(2)[0].isdigit()
                           and marker.group(2)[:-1] != "1"):
            width = len(marker.group(0)) if marker.group(3) else len(marker.group(0)) + 1
            items.append(base + _indent(rest[:len(marker.group(1))]) + width
                         if marker.group(3) != "\t" else base + len(marker.group(0)) + 3)
            removed += len(marker.group(0))
            rest = rest[len(marker.group(0)):]
            block += 1
            in_paragraph = False
            if not rest.strip():
                found.append(_Line("blank", start, False, block))
                continue
        opening = _FENCE.match(rest)
        if opening and not (opening.group(2)[0] == "`" and "`" in opening.group(3)):
            fence = (opening.group(2)[0], len(opening.group(2)), items[-1] if items else 0,
                     quotes)
            fence_opened = True
            found.append(_Line("code", start, False, block))
            in_paragraph = False
            continue
        kind = _html_block_start(rest, in_paragraph)
        if kind:
            if kind <= 5 and not _html_block_end(kind, rest[rest.find("<") + 2:]
                                                  if kind == 2 else rest):
                html = kind
            elif kind >= 6:
                html = kind
            block += 1
            found.append(_Line("comment" if kind == 2 else "html", start + removed, False, block))
            in_paragraph = False
            continue
        if not in_paragraph and _DEFINITION.match(rest):
            # A link reference definition is a token of its own, which `VLo` does not read.
            block += 1
            found.append(_Line("html", start + removed, False, block))
            continue
        heading = _HEADING.match(rest)
        if not in_paragraph:
            block += 1
        if heading:
            removed += heading.end()
            in_paragraph = False
        else:
            in_paragraph = True
        found.append(_Line("text", start + removed, bool(items), block))
    return found


def _mask_code_spans(text: str, masked: list) -> None:
    """Mask the code spans of a block's text: a run of backticks and the next run of the same
    length, where one follows, the runs included. A backslash before a backtick makes it no
    opener."""
    if "`" not in text:
        return
    runs, i = _BacktickRuns(text), 0
    while True:
        found = _ESCAPE_OR_BACKTICKS.search(text, i)
        if found is None:
            return
        i, j = found.span()
        if text[i] == "\\":
            i = j
            continue
        closing = runs.next_of(j - i, j)
        if closing is None:
            i = j
            continue
        m = closing + j - i
        for x in range(i, m):
            if text[x] != "\n":
                masked[x] = " "
        i = m


def _flanking(text: str, start: int, end: int) -> tuple[bool, bool]:
    """Whether the delimiter run from `start` to `end` is left-flanking and right-flanking."""
    before = text[start - 1] if start else " "
    after = text[end] if end < len(text) else " "
    before_space, after_space = before in _JS_SPACE, after in _JS_SPACE
    before_punct, after_punct = before in _PUNCTUATION, after in _PUNCTUATION
    left = not after_space and (not after_punct or before_space or before_punct)
    right = not before_space and (not before_punct or after_space or after_punct)
    return left, right


def _mask_inline(text: str, masked: list, labels=frozenset()) -> None:
    """Mask the inline syntax of a block's text that is no text token: escapes, inline HTML and
    autolinks, the brackets and the destination of a link, of a reference link to one of
    `labels`, an image, and emphasis delimiters that open or close emphasis."""

    def blank(span) -> None:
        for x in span:
            if text[x] != "\n":
                masked[x] = " "

    i = 0
    while i < len(text):
        if masked[i] != text[i]:
            i += 1
            continue
        char = text[i]
        if char == "\\" and i + 1 < len(text) and text[i + 1] in _PUNCTUATION:
            masked[i] = masked[i + 1] = " "
            i += 2
            continue
        if char == "<":
            match = _AUTOLINK.match(text, i) or _INLINE_HTML.match(text, i)
            if match:
                blank(range(i, match.end()))
                i = match.end()
                continue
        if char in "![":
            image = char == "!"
            open_at = i + 1 if image else i
            close = (text.find("]", open_at + 1)
                     if open_at < len(text) and text[open_at] == "[" else -1)
            if close >= 0:
                match = _LINK_DESTINATION.match(text, close + 1)
                reference = re.compile(r"\[([^\[\]]*)\]").match(text, close + 1)
                label = _label(text[open_at + 1:close])
                if match:
                    end = match.end()
                elif reference and _label(reference.group(1) or label) in labels:
                    end = reference.end()
                elif label in labels:
                    end = close + 1
                else:
                    end = None
                if end is not None:
                    blank(range(i, end) if image else [i, *range(close, end)])
                    i = end if image else open_at + 1
                    continue
        i += 1
    runs, i = [], 0
    while i < len(text):
        if text[i] in "*_" and masked[i] == text[i]:
            j = i
            while j < len(text) and text[j] == text[i]:
                j += 1
            left, right = _flanking(text, i, j)
            if text[i] == "_":
                before = text[i - 1] if i else " "
                after = text[j] if j < len(text) else " "
                opens = left and (not right or before in _PUNCTUATION)
                closes = right and (not left or after in _PUNCTUATION)
            else:
                opens, closes = left, right
            runs.append((i, j, text[i], opens, closes))
            i = j
        else:
            i += 1
    openers = []
    for start, end, char, opens, closes in runs:
        matched = False
        if closes:
            for index in range(len(openers) - 1, -1, -1):
                if openers[index][2] == char:
                    open_start, open_end = openers[index][:2]
                    for x in [*range(open_start, open_end), *range(start, end)]:
                        masked[x] = " "
                    del openers[index:]
                    matched = True
                    break
        if not matched and opens:
            openers.append((start, end, char))


def _claude_candidates(text: str) -> list:
    """`(offset, token)` of every `@` token that Claude Code's `VLo` finds in `text`, the
    text of a scanned token: `/(?:^|\\s)@((?:[^\\s\\\\]|\\\\ )+)/`, with JavaScript's `\\s`."""
    found, i = [], text.find("@")
    while i >= 0:
        if not i or text[i - 1] in _JS_SPACE:
            j = i + 1
            while j < len(text):
                if text[j] == "\\" and text[j + 1:j + 2] == " ":
                    j += 2
                elif text[j] == "\\" or text[j] in _JS_SPACE:
                    break
                else:
                    j += 1
            if j > i + 1:
                found.append((i, text[i + 1:j]))
                i = text.find("@", j)
                continue
        i = text.find("@", i + 1)
    return found


def _claude_path(written: str) -> str | None:
    """The path that Claude Code imports for a token, or None: everything from `#` on is cut,
    `\\ ` is a space, and a path starts with `./`, `~/` or `/`, but is not `/` alone, or with
    a letter, a digit or one of `._-`."""
    path = written.split("#", 1)[0].replace("\\ ", " ")
    if not path:
        return None
    if path.startswith(("./", "~/")) or (path.startswith("/") and path != "/"):
        return path
    if (not path.startswith("@") and not re.match(r"[#%^&*()]+", path)
            and re.match(r"[a-zA-Z0-9._-]", path)):
        return path
    return None


def claude_at(text: str) -> list:
    """Claude Code's `@path`, as it reads a memory file. Claude Code reads a text that contains an
    `@` or an HTML comment without its byte order mark and YAML front matter. It reads the text of
    the Markdown outside code blocks, code spans and HTML, and the comments of an HTML block that
    starts with one. An `@` starts an import at the start of a text token or after whitespace. In a
    list item, the text of the item is read as written too, code spans included, as the lexer keeps
    it in a text token of its own. A path is imported once per file. An `@` of a mail link or a
    mail address (`_mail_ats`) starts no import, such as the `@` of the link text `@octocat` in
    `[@octocat](mailto:octocat@example.com)`."""
    if text.startswith("﻿"):
        text = " " + text[1:]
    front = re.match(r"---\s*\n([\s\S]*?)---\s*\n?", text) if text.startswith("---") else None
    if front:
        text = _mask(text, 0, front.end())
    if "@" not in text and "<!--" not in text:
        return []
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    labels = {_label(match.group(1)) for line in lines
              for match in [_DEFINITION.match(re.sub(r"^(?: {0,3}> ?)+", "", line))] if match}
    # The lines of each block, in the order of the text: a run of lines that the block pass put
    # into one block of the same kind.
    blocks = []
    for number, kind in enumerate(_markdown_lines(lines)):
        if kind.kind not in ("text", "comment"):
            continue
        if blocks and blocks[-1][0] == (kind.kind, kind.block):
            blocks[-1][1].append((number, kind))
        else:
            blocks.append(((kind.kind, kind.block), [(number, kind)]))
    candidates = []
    for (kind, _block), members in blocks:
        first = offsets[members[0][0]]
        last = offsets[members[-1][0]] + len(lines[members[-1][0]])
        if kind == "comment":
            # `VLo` reads an HTML block that starts with a comment with its comments removed.
            # `kept` holds the offset in `raw` of each character that the removal keeps, so
            # that an import records the line on which it is written.
            raw = text[first:last]
            if raw.lstrip().startswith("<!--") and "-->" in raw:
                kept, start = [], 0
                for comment in re.finditer(r"<!--[\s\S]*?-->", raw):
                    kept += range(start, comment.start())
                    start = comment.end()
                kept += range(start, len(raw))
                removed = "".join(raw[x] for x in kept)
                candidates += [(first + kept[i], token)
                               for i, token in _claude_candidates(removed)]
            continue
        # The text of the block, without the markers of block quotes, list items and headings.
        block = [" " if char != "\n" else "\n" for char in text[first:last]]
        for number, line in members:
            end = offsets[number] + len(lines[number].rstrip("\r"))
            for x in range(offsets[number] + line.start, end):
                block[x - first] = text[x]
        if members[0][1].item:
            for number, line in members:
                start = offsets[number] + line.start
                content = text[start:offsets[number] + len(lines[number].rstrip("\r"))]
                candidates += [(start + i, token) for i, token in _claude_candidates(content)]
        source = "".join(block)
        _mask_code_spans(source, block)
        _mask_inline(source, block, labels)
        candidates += [(first + i, token) for i, token in _claude_candidates("".join(block))]
    found, seen, line_of, mail = [], set(), _Lines(text), _mail_ats(text)
    for offset, token in candidates:
        path = _claude_path(token)
        if path is None or offset in mail:
            continue
        key = path if path.startswith(("~", "/")) else posixpath.normpath(path)
        if key not in seen:
            seen.add(key)
            found.append(Import(f"@{token}", line_of.of(offset), path))
    return found


# Cursor Bugbot: the Markdown links of BUGBOT.md, which Cursor's Bugbot page and posts of Cursor's
# staff in its forum state. The row records the files that Bugbot reads, `.md` and `.mdc` files by a
# relative path (the section "Imports" of docs/heuristics.md). Code blocks, code spans and HTML are
# masked as for Claude Code, because a link there is no Markdown link.
_MARKDOWN_LINK = re.compile(r"(?<!!)\[(?:[^\[\]\\]|\\.)*\]\(\s*(<[^<>\n]*>|[^\s()<>]+)"
                            r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)")


def markdown_link(text: str) -> list:
    """The Markdown links of a text outside code and HTML to a file: the destination as a
    Markdown renderer reads a relative link, without its `#fragment` or `?query` and
    percent-decoded where it decodes to UTF-8. A link with a scheme, such as `https:`, or to a
    fragment of the same page names no file and is none. An absolute path, a path in the home
    directory and a file of any extension are links, which the row marks as Bugbot reads them."""
    lines = text.split("\n")
    kinds = _markdown_lines(lines)
    masked, offset = [], 0
    for line, kind in zip(lines, kinds):
        keep = kind.kind == "text"
        masked.append("".join(char if keep and n >= kind.start else " "
                              for n, char in enumerate(line)))
    joined = "\n".join(masked)
    segment = list(joined)
    _mask_code_spans(joined, segment)
    for match in re.finditer(r"<!--[\s\S]*?-->", joined):
        for x in range(*match.span()):
            if joined[x] != "\n":
                segment[x] = " "
    readable = "".join(segment)
    found, line_of = [], _Lines(text)
    for match in _MARKDOWN_LINK.finditer(readable):
        destination = match.group(1).strip("<>")
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", destination) or destination.startswith("#"):
            continue
        path = re.split(r"[#?]", destination, maxsplit=1)[0]
        try:
            path = unquote(path, errors="strict")
        except UnicodeDecodeError:
            pass
        if not path:
            continue
        found.append(Import(match.group(0), line_of.of(match.start()), path))
    return found


# Claude Code Review: "To include a file, add a line to the root REVIEW.md with only @ and the
# file name on it." A name with a `/` is a path into a subdirectory, which "isn't included", and
# the page requires "a name without spaces" (code.claude.com/docs/en/code-review-customize).
# The page defines the include of a file by its line alone, and a line in code or in an HTML
# comment is read as well (the section "Imports" of docs/heuristics.md).
_CODE_REVIEW_INCLUDE = re.compile(r"@([^/\s]+)")
# The exception is a skill: "Add @ and the path to SKILL.md, in the form
# .claude/skills/<folder>/SKILL.md, on a line of its own", "with the skill's folder directly
# under skills/", and "Use a folder name without spaces". `.` and `..` name no folder under
# skills/.
_CODE_REVIEW_SKILL = re.compile(r"@(\.claude/skills/(?!\.\.?/)[^/\s]+/SKILL\.md)")
# "Size: keep SKILL.md at 30,000 bytes or less, frontmatter included" and "Count: write at most
# eight skill lines. A repeated line counts again toward the eight." The skill also counts
# toward the row's limits of files and bytes: "The skill file counts toward the limits of eight
# files and 150,000 bytes, and has its own limit of 30,000 bytes."
CODE_REVIEW_SKILL_MAX_BYTES = 30_000
CODE_REVIEW_SKILL_LINES = 8
# "Blocks in REVIEW.md: keep the @ line out of code fences. Close every HTML comment and every
# <pre>, <script>, <style>, or <textarea> block above it." These are a fenced code block and the
# HTML blocks of the CommonMark kinds 1 and 2, which end only at their closing line.
_CLOSING_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})[ \t]*")
_HTML_COMMENT_OPEN = re.compile(r" {0,3}<!--")


def _code_review_blocks(lines: list) -> list:
    """For each line, whether it is inside a code fence or inside an HTML comment or a `<pre>`,
    `<script>`, `<style>` or `<textarea>` block that a line above it opened, as CommonMark
    delimits a fenced code block and the HTML blocks of the kinds 1 and 2 at the top level. A
    block that no line closes runs to the end of the text."""
    inside, fence, html = [], None, 0
    for line in lines:
        inside.append(fence is not None or html != 0)
        if fence is not None:
            closing = _CLOSING_FENCE.fullmatch(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= fence[1]:
                fence = None
        elif html:
            if _html_block_end(html, line):
                html = 0
        elif (opening := _FENCE.match(line)) and not (opening.group(2)[0] == "`"
                                                      and "`" in opening.group(3)):
            fence = (opening.group(2)[0], len(opening.group(2)))
        else:
            html = 1 if _HTML_OPEN.match(line) else 2 if _HTML_COMMENT_OPEN.match(line) else 0
            if html and _html_block_end(html, line):
                html = 0
    return inside


def code_review_include(text: str) -> list:
    """Claude Code Review's includes: each line that holds `@` and a file name and nothing else,
    not even a space before or after them, where the name contains neither a `/` nor
    whitespace, and each line that holds `@` and the path of a skill's SKILL.md in the same way
    (`_CODE_REVIEW_SKILL`), outside a code fence and the HTML blocks that the page names
    (`_code_review_blocks`). Claude Code Review reads an included skill up to the size limit of a
    skill (`CODE_REVIEW_SKILL_MAX_BYTES`), through no link and on the first eight skill lines
    only. A line ends at a line feed, a carriage return before it belongs to the line break, and a
    byte order mark at the start of the text belongs to no line."""
    lines = [line.removesuffix("\r") for line in text.removeprefix("\ufeff").split("\n")]
    found, skills = [], 0
    for number, (line, blocked) in enumerate(zip(lines, _code_review_blocks(lines)), start=1):
        match = _CODE_REVIEW_INCLUDE.fullmatch(line)
        if match:
            found.append(Import(match.group(0), number, match.group(1)))
            continue
        match = _CODE_REVIEW_SKILL.fullmatch(line)
        if match and not blocked:
            skills += 1
            found.append(Import(match.group(0), number, match.group(1),
                                max_file_bytes=CODE_REVIEW_SKILL_MAX_BYTES, through_links=False,
                                past_count=skills > CODE_REVIEW_SKILL_LINES))
    return found


PARSERS = {
    "claude_at": claude_at,
    "copilot_at": copilot_at,
    "gemini_at": gemini_at,
    "antigravity_include": antigravity_include,
    "markdown_link": markdown_link,
    "code_review_include": code_review_include,
}
