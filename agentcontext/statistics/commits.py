"""Computations over the commit CSVs of the collection: `output/all_ai_commits.csv`, tracked as
an archive in numbered volumes, with one row per commit that credits one of the six tools, and
`output/other_ai_commits.csv`, tracked as an archive, with one row per commit that credits a tool
outside them.

A row joins the tools that it credits in `ai_tool` with commas, the ids of its matched heuristics
in `matched_heuristics` with semicolons, and its `TOOL:ATTRIBUTION` pairs in `attributions` with
semicolons. A commit that credits one of the six and a tool outside them is a row of both CSVs,
and a computation over both reads it as one commit with the tools, the matched heuristics and the
attributions of both rows. The tool of a heuristic is its `tool` in
`heuristics/ai_commit_patterns.csv` or `heuristics/other_ai_commit_patterns.csv`.

A condition on heuristics is a list of selectors. A selector is a regular expression that the
whole id of a heuristic matches, such as `openclaw-author-02|openclaw-trailer-03`, or an object
with `where` and `match` that selects rows of the heuristics files by their columns, as
`tables.rows` selects records, such as the trailer rows of `tool not named`, so that a statistic
follows the file when a row is added. A selector that fits no heuristic is an error, because it
would count nothing without notice.

Each CSV is read once for a `Files`, and the commits are indexed by their heuristics, so that the
computations share one pass.
"""

import re
from collections.abc import Iterator
from typing import NamedTuple

from agentcontext.statistics import Files, Unavailable, checkout
from agentcontext.statistics.tables import selected

TABLES = {"all": "data/collection/output/all_ai_commits.csv.7z.001",
          "other": "data/collection/output/other_ai_commits.csv.7z"}
PATTERNS = ("heuristics/ai_commit_patterns.csv", "heuristics/other_ai_commit_patterns.csv")


class Commit(NamedTuple):
    """A commit of the commit CSVs with the tools, heuristics and attributions of its rows."""
    repo: str
    sha: str
    tools: tuple
    heuristics: tuple
    attributions: tuple
    message: str


def _paths(tables: list | None) -> tuple:
    """The paths of the commit CSVs that `tables` names, `all`, `other` or a path, by default
    both."""
    return tuple(TABLES.get(name, name) for name in tables or TABLES)


def _split(text: str, separator: str, known: dict) -> tuple:
    """The parts of a joined column, each part one shared string, since the rows repeat the same
    ids."""
    return tuple(known.setdefault(part, part) for part in text.split(separator) if part)


def _rows(files: Files, path: str) -> list:
    """The commits of the rows of one commit CSV."""
    def read() -> list:
        lists = files.lists(path)
        header = {column: number for number, column in enumerate(next(lists, []))}
        repo, sha, tool, matched, message = (header[column] for column in (
            "repo_name", "commit_sha", "ai_tool", "matched_heuristics", "commit_message"))
        # The commit CSVs of earlier collections had no `attributions` column.
        attributions = header.get("attributions")
        known: dict = {}
        return [Commit(row[repo], row[sha], _split(row[tool], ",", known),
                       _split(row[matched], ";", known),
                       _split(row[attributions], ";", known) if attributions is not None else (),
                       row[message])
                for row in lists]
    return files.memo(("commit rows", path), read)


def commits(files: Files, tables: list | None = None) -> list:
    """The commits of the commit CSVs that `tables` names, each once."""
    paths = _paths(tables)

    def read() -> list:
        found: dict = {}
        for path in paths:
            for commit in _rows(files, path):
                key = (commit.repo, commit.sha)
                known = found.get(key)
                if known is None:
                    found[key] = commit
                else:
                    found[key] = known._replace(
                        tools=_union(known.tools, commit.tools),
                        heuristics=_union(known.heuristics, commit.heuristics),
                        attributions=_union(known.attributions, commit.attributions))
        return list(found.values())
    return files.memo(("commits", paths), read)


def _union(first: tuple, second: tuple) -> tuple:
    """The parts of two joined columns, each once, in their order."""
    return tuple(dict.fromkeys(first + second))


def _index(files: Files, tables: list | None, column: str = "heuristics") -> dict:
    """{heuristic id or tool: positions in `commits` of the commits that it matched or that
    credit it}, by the field `column` of the commits."""
    def read() -> dict:
        index: dict = {}
        for position, commit in enumerate(commits(files, tables)):
            for value in getattr(commit, column):
                index.setdefault(value, []).append(position)
        return index
    return files.memo(("commit index", column, _paths(tables)), read)


def _matching(files: Files, tables: list | None, heuristics: set) -> set:
    """The positions in `commits` of the commits that matched one of the heuristics."""
    index = _index(files, tables)
    return {position for heuristic in heuristics for position in index.get(heuristic, ())}


def _heuristic_rows(files: Files) -> list:
    """The rows of both commit heuristics files, without the comment rows."""
    return files.memo(("commit heuristics",), lambda: [
        row for path in PATTERNS for row in files.rows(path)
        if row["id"] and not row["id"].startswith("#")])


def _heuristic_tools(files: Files) -> dict:
    """{id: tool} of the rows of both commit heuristics files."""
    return files.memo(("heuristic tools",), lambda: {
        row["id"]: row["tool"] for row in _heuristic_rows(files)})


def _fitting(files: Files, tables: list | None, selector: str | dict) -> set:
    """The ids of the heuristics that a selector fits: the ids of the heuristics files and of
    the commits that a regular expression matches whole, or the ids of the rows of the
    heuristics files that `where` and `match` select."""
    if isinstance(selector, str):
        pattern = re.compile(selector)
        ids = {row["id"] for row in _heuristic_rows(files)} | set(_index(files, tables))
        found = {heuristic for heuristic in ids if pattern.fullmatch(heuristic)}
    else:
        unknown = set(selector) - {"where", "match"}
        if unknown:
            raise Unavailable(f"A selector of heuristic rows takes only the keys where and "
                              f"match, not {sorted(unknown)}")
        found = {row["id"] for row in selected(files, _heuristic_rows(files),
                                               selector.get("where"), selector.get("match"))}
    if not found:
        raise Unavailable(f"No heuristic of {' or '.join(PATTERNS)} fits {selector!r}")
    return found


def _allowed(files: Files, tables: list | None, selectors: list) -> set:
    """The ids of the heuristics that some selector of a list fits."""
    return set().union(*(_fitting(files, tables, selector) for selector in selectors))


def _messages(files: Files, tables: list | None, message: str) -> set:
    """The positions in `commits` of the commits with a message that a regular expression
    matches."""
    def read() -> set:
        pattern = re.compile(message)
        return {position for position, commit in enumerate(commits(files, tables))
                if pattern.search(commit.message)}
    return files.memo(("commit messages", _paths(tables), message), read)


def _keys(files: Files, table: str) -> set:
    """(repo_name, commit_sha) of the rows of one commit CSV."""
    return files.memo(("commit keys", table), lambda: {
        (commit.repo, commit.sha) for commit in _rows(files, _paths([table])[0])})


def _labeled(files: Files, labeled: dict) -> set:
    """(repo_name, commit_sha) of the rows of a label CSV of commits with a label, such as the
    commits of other automation in `output/identity-labels.csv`."""
    return {(row["repo_name"], row["commit_sha"]) for row in files.rows(labeled["path"])
            if row["label"] == labeled["label"]}


def _credited_alone(commit: Commit, tool: str, tool_of: dict, allowed: set) -> bool:
    """Whether the commit matched a heuristic of the tool and every such heuristic is allowed,
    so that the commit would not credit the tool without them."""
    own = [heuristic for heuristic in commit.heuristics if tool_of.get(heuristic) == tool]
    return bool(own) and all(heuristic in allowed for heuristic in own)


def _pairs(commit: Commit) -> Iterator[tuple]:
    """(tool, attribution) of every attribution of a commit."""
    for pair in commit.attributions:
        name, _, kind = pair.partition(":")
        yield name, kind


# The conditions of `count`, each applied when it is given:
# - `tool`: the commit credits the tool.
# - `repository`: the commit is in the repository.
# - `heuristics`: each selector fits a heuristic that the commit matched.
# - `only`: every heuristic that the commit matched fits a selector, whatever its tool.
# - `alone`: for `tool`, or for some tool of the commit, the commit matched a heuristic of the
#   tool and every such heuristic fits a selector, so that these heuristics alone credit the tool.
#   The heuristics of the other tools of the commit do not count, because they credit another
#   tool.
# - `attribution`: a `TOOL:ATTRIBUTION` pair, such as `OpenClaw:account`, is among the
#   attributions of the commit.
# - `attributions`: the commit has an attribution of `tool`, and every attribution of `tool` is
#   in the list, such as `["reviewer"]`.
# - `beside`: another tool than `tool` has an attribution of the list.
# - `message`: a regular expression matches the commit message.
# - `longer_than`: the commit message has more characters.
# - `absent_from`: the commit is no row of that commit CSV, `all`, `other` or a path.
# - `labeled`: the label CSV `{"path": PATH, "label": LABEL}` gives the commit the label.
@checkout
def count(files: Files, distinct: str = "commit", tables: list | None = None,
          tool: str | None = None, repository: str | None = None,
          heuristics: list | None = None, only: list | None = None, alone: list | None = None,
          attribution: str | None = None, attributions: list | None = None,
          beside: list | None = None, message: str | None = None,
          longer_than: int | None = None, absent_from: str | None = None,
          labeled: dict | None = None) -> int:
    """The commits of the commit CSVs, or with `distinct` `repository` their repositories, that
    every given condition selects."""
    if distinct not in ("commit", "repository"):
        raise ValueError(f"distinct is commit or repository, not {distinct!r}")
    if tool is None and (attributions is not None or beside is not None):
        raise ValueError("attributions and beside need the tool whose attributions they read")
    found = commits(files, tables)
    allowed_only = _allowed(files, tables, only) if only is not None else None
    allowed_alone = _allowed(files, tables, alone) if alone is not None else None
    # The indexes narrow the commits to check to those that matched a selected heuristic or
    # credit the tool. Every commit of the CSVs matched a heuristic, so a commit that `only`
    # or `alone` selects matched one of the heuristics that they allow.
    narrowing = [_matching(files, tables, _fitting(files, tables, selector))
                 for selector in heuristics or ()]
    narrowing += [_matching(files, tables, allowed) for allowed in (allowed_only, allowed_alone)
                  if allowed is not None]
    if tool is not None:
        narrowing.append(set(_index(files, tables, "tools").get(tool, ())))
    candidates = set.intersection(*narrowing) if narrowing else None
    if message is not None and candidates is None:
        candidates, message = _messages(files, tables, message), None
    pattern = re.compile(message) if message is not None else None
    tool_of = _heuristic_tools(files) if alone is not None else {}
    kinds, others = set(attributions or ()), set(beside or ())
    absent = _keys(files, absent_from) if absent_from is not None else None
    labels = _labeled(files, labeled) if labeled is not None else None
    counted = set()
    for position in range(len(found)) if candidates is None else candidates:
        commit = found[position]
        if tool is not None and tool not in commit.tools:
            continue
        if repository is not None and commit.repo != repository:
            continue
        if allowed_only is not None and not all(heuristic in allowed_only
                                                for heuristic in commit.heuristics):
            continue
        if allowed_alone is not None and not any(
                _credited_alone(commit, candidate, tool_of, allowed_alone)
                for candidate in ([tool] if tool is not None else commit.tools)):
            continue
        if attribution is not None and attribution not in commit.attributions:
            continue
        if attributions is not None:
            own = {kind for name, kind in _pairs(commit) if name == tool}
            if not own or not own <= kinds:
                continue
        if beside is not None and not any(name != tool and kind in others
                                          for name, kind in _pairs(commit)):
            continue
        if pattern is not None and not pattern.search(commit.message):
            continue
        if longer_than is not None and not len(commit.message) > longer_than:
            continue
        if absent is not None and (commit.repo, commit.sha) in absent:
            continue
        if labels is not None and (commit.repo, commit.sha) not in labels:
            continue
        counted.add(commit.repo if distinct == "repository" else position)
    return len(counted)


@checkout
def longest_message(files: Files, tables: list | None = None) -> int:
    """The characters of the longest message among the commits of the commit CSVs."""
    return max((len(commit.message) for commit in commits(files, tables)), default=0)
