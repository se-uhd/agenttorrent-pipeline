"""The commit attribution validation, `python -m agentcontext commit-attribution-validation`,
which matches the commit patterns against the commit CSV again.

For every row of the commit CSV that the cloning step writes, the check matches the message
heuristics again to classify how the commit was detected: by a message pattern (trailer or
full-body) or by the author or committer identity alone.

A message match is evidence that the CSV itself contains. The label `author_only` means that
no current message pattern matched. Without the original identity fields, the label does not
establish that an earlier identity attribution was correct. A complete reclassification
requires the commit logs that `commit-reclassification` reads.

The exit code is a verdict on the CSV: 0 when every row agrees with what the heuristics give
over the columns that the CSV stores, 2 when a row does not, 1 when the CSV cannot be read. A
CSV that the cloning step wrote has `matched_heuristics` and `attributions`. The heuristics
that a row names have to name the tools in `ai_tool`. Its message heuristics have to match the
stored message now, and its attributions have to be what that match gives. A CSV without
`matched_heuristics`, from AgentConfigs 1.0.0, can only be checked against the message: A
message that matches a tool that the row does not name was classified under other heuristics.
The check writes the classification in both cases.

Usage
-----
python -m agentcontext commit-attribution-validation -i ai_commits_all.csv
python -m agentcontext commit-attribution-validation -i ai_commits_all.csv -o classified.csv
python -m agentcontext commit-attribution-validation -i ai_commits_all.csv --summary-only
"""

import contextlib
import csv
import os
import secrets
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Tuple

from agentcontext import caches, commits, logs
from agentcontext import cli

# The log component of this check is its step key rather than "commits", the component of
# `agentcontext/commits.py`. This module imports that module, and a reader of a run's log has
# to tell the messages of the two apart.
COMPONENT = 'commit_attribution_validation'


# -----------------------------------------------------------------------------
# Matching the stored message again
# -----------------------------------------------------------------------------
# The runner declares this step's output in its step list, and nothing else
# names it. Deriving the name here, in one function, lets test_runner.py check
# the declaration against the function rather than against a second copy of it.
OUTPUT_SUFFIX = "-validated"


def default_output_path(input_path) -> str:
    """Return the output path used when -o is not given."""
    p = Path(input_path)
    return str(p.with_name(f"{p.stem}{OUTPUT_SUFFIX}{p.suffix}"))


# The heuristics by id, the name that `matched_heuristics` gives them.
_HEURISTIC = {row["id"]: row for row in commits.AI_COMMIT_PATTERNS}


class Heuristics(NamedTuple):
    """One commit heuristics file as the check reads it: its rows by id (`by_id`), the ids of
    the message rows that a stored message matches (`matching`), the ids among them that
    `matched_heuristics` records for a row that credits the given tools (`recorded`), and the
    attributions that the matched rows give (`attributions`).

    The check matches each message once, through the functions of `agentcontext/commits.py`
    that the cloning step uses, and derives both the classification and the verdict from that
    match."""
    by_id: dict
    matching: Callable
    recorded: Callable
    attributions: Callable


# The cloning step leaves out the rows and the attribution of `UNNAMED_TOOL` when it finds
# another tool, in the message or in the author's identity (`commits.matched_other_rules_in_fields`).
# The CSV stores no identity, and the tools of the row show what the step found instead:
# `UNNAMED_TOOL` is among them exactly when the step kept its rows. If the check decided from the
# message alone, a commit of a tool's bot with a trailer that credits AI and names no tool would
# differ from its row.
def _other_recorded(subject: str, body: str, matching: list, tools: list) -> list:
    """The ids of `matching`, the message rows of other_ai_commit_patterns.csv that match,
    without the rows of `UNNAMED_TOOL` when `tools`, the tools of the row, do not name it."""
    if commits.UNNAMED_TOOL in tools:
        return matching
    return [n for n in matching if commits._OTHER_TOOL_OF[n] != commits.UNNAMED_TOOL]


def _other_attributions(subject: str, body: str, tools: list) -> list:
    """The attributions of the message rows of other_ai_commit_patterns.csv, without the
    attribution of `UNNAMED_TOOL` when `tools`, the tools of the row, do not name it."""
    pairs = commits._attributions(commits._OTHER_INDEXED_PATTERNS, "", "", "", "", subject, body,
                                  check_author=False)
    if commits.UNNAMED_TOOL not in tools:
        pairs = [pair for pair in pairs if pair[0] != commits.UNNAMED_TOOL]
    return pairs


# `all_ai_commits.csv` is checked against ai_commit_patterns.csv, and `other_ai_commits.csv`
# against other_ai_commit_patterns.csv. Against the six tools' file, every row of the other CSV
# would name heuristics that the file does not have.
TOOLS = Heuristics(
    _HEURISTIC,
    lambda subject, body: commits.matched_rules_in_fields("", "", "", "", subject, body,
                                                          check_author=False),
    lambda subject, body, matching, tools: matching,
    lambda subject, body, tools: commits._attributions(commits._INDEXED_PATTERNS, "", "", "", "",
                                                       subject, body, check_author=False))
# `matching` keeps the rows of `UNNAMED_TOOL` that `matched_other_rules_in_fields` leaves out,
# because the check classifies a row of the CSV that names `UNNAMED_TOOL` by these rows.
OTHER_TOOLS = Heuristics(
    {row["id"]: row for row in commits.OTHER_AI_COMMIT_PATTERNS},
    lambda subject, body: commits._matched(commits._OTHER_INDEXED_PATTERNS, "", "", "", "",
                                           subject, body, check_author=False),
    _other_recorded,
    _other_attributions)


def split_message(full_message: str, escaped: bool = False) -> Tuple[str, str]:
    """Split a commit message into (subject, body) on the first blank line.

    The commit CSV stores the message verbatim, its line breaks in a quoted field. A CSV of
    AgentConfigs 1.0.0 contains a newline as the two characters backslash and n. `escaped`
    marks such a message, and its escape is decoded first.
    """
    return commits.message_parts(_unescape(full_message) if escaped else full_message)


def legacy(row: dict) -> bool:
    """Whether the row is from a CSV of AgentConfigs 1.0.0, which has no `matched_heuristics`
    column and contains a newline in a message as backslash and n."""
    return "matched_heuristics" not in row


def _unescape(message: str) -> str:
    """The escape of a CSV of AgentConfigs 1.0.0, reversed: `\\n` is a newline and `\\\\` is
    one backslash.

    The function reads left to right rather than in two passes of `str.replace`, so that an
    escaped backslash before an `n` is not read as a newline. A CSV in which the backslash
    itself is not escaped is ambiguous, because only the escaped backslash makes the escape
    reversible, and the function reads it in the same way.
    """
    out, i = [], 0
    while i < len(message):
        if message[i] == "\\" and i + 1 < len(message):
            following = message[i + 1]
            if following == "n":
                out.append("\n")
                i += 2
                continue
            if following == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(message[i])
        i += 1
    return "".join(out)


NEW_COLUMNS = [
    "detection_source",
    "matched_scope",
    "matched_pattern",
]

# The differences that the verdict lists by repository and commit, "the first ten".
DIFFERENCES_LISTED = 10


class Match(NamedTuple):
    """One row's message, split and matched once: the tools that the row names, in its order,
    the subject and the body, the ids of every message heuristic that matches, in file order,
    and the ids among them that `matched_heuristics` records."""
    tools: list
    subject: str
    body: str
    matching: list
    recorded: list


def match_row(row: dict, tool_col: str, message_col: str,
              heuristics: Heuristics = TOOLS) -> Match:
    """Split the row's message and match the message heuristics against it."""
    tools = [t.strip() for t in (row.get(tool_col) or "").split(",") if t.strip()]
    subject, body = split_message(row.get(message_col) or "", escaped=legacy(row))
    matching = heuristics.matching(subject, body)
    return Match(tools, subject, body, matching,
                 heuristics.recorded(subject, body, matching, tools))


def _detection_source(tool: str, matching: list, by_id: dict) -> Tuple[str, str, str]:
    """(detection_source, matched_scope, matched_pattern) of one tool, from the first of its
    trailer heuristics and the first of its full-body heuristics among `matching`."""
    trailer = next((by_id[n]["pattern"] for n in matching
                    if by_id[n]["tool"] == tool and by_id[n]["scope"] == "trailer"), "")
    full_body = next((by_id[n]["pattern"] for n in matching
                      if by_id[n]["tool"] == tool and by_id[n]["scope"] == "full_body"), "")
    if trailer and full_body:
        return ("message", "trailer+full_body", trailer)
    if trailer:
        return ("message", "trailer", trailer)
    if full_body:
        return ("message", "full_body", full_body)
    return ("author_only", "", "")


def classify_detection_source(
    tool_name: str, subject: str, body: str, heuristics: Heuristics = TOOLS,
) -> Tuple[str, str, str]:
    """Match the trailer-scoped and full-body-scoped message heuristics again.

    Returns (detection_source, matched_scope, matched_pattern). The pattern is that of the
    tool's first trailer heuristic that matches, in file order, or of its first full-body
    heuristic when no trailer heuristic matches.
    """
    return _detection_source(tool_name, heuristics.matching(subject, body), heuristics.by_id)


def classification(match: Match, heuristics: Heuristics = TOOLS) -> Tuple[str, str, str]:
    """The values of `NEW_COLUMNS` for a row: those of its first tool with a message match, or
    of its first tool when none of its tools has one, and blank when it names no tool."""
    best = ("", "", "")
    for tool in match.tools:
        source = _detection_source(tool, match.matching, heuristics.by_id)
        # Prefer a message match over author_only.
        if source[0] == "message" and best[0] != "message":
            best = source
        elif not best[0]:
            best = source
    return best


def validate_row(
    row: dict, tool_col: str, message_col: str, heuristics: Heuristics = TOOLS,
) -> dict:
    """The row with the detection source columns added."""
    match = match_row(row, tool_col, message_col, heuristics)
    return {**row, **dict(zip(NEW_COLUMNS, classification(match, heuristics)))}


MATCH_OUTCOMES = ("identity_only", "message_exact", "message_more", "message_part",
                "message_other")


def match_outcome(row: dict, tool_col: str = "ai_tool", message_col: str = "commit_message",
                  heuristics: Heuristics = TOOLS) -> str:
    """Which of `MATCH_OUTCOMES` the row is, under the message heuristics matched against its
    message.

    `identity_only`: no message heuristic matches, and every tool that the row names came from
    the author or committer fields, which the CSV does not store. `message_exact`: the match
    names the row's tools and no other. `message_more`: the row's tools and more, because a
    heuristic was added after the row was written. `message_part`: some of the row's tools, the
    rest from identity. `message_other`: a different set, overlapping in part or not at all.
    """
    match = match_row(row, tool_col, message_col, heuristics)
    tools = set(match.tools)
    matched = {heuristics.by_id[n]["tool"] for n in match.recorded}
    if not matched:
        return "identity_only"
    if matched == tools:
        return "message_exact"
    if matched > tools:
        return "message_more"
    if matched < tools:
        return "message_part"
    return "message_other"


def mismatch_reason(row: dict, tool_col: str, message_col: str,
                heuristics: Heuristics = TOOLS) -> str:
    """Why this row disagrees with what the heuristics give over the columns that the CSV
    stores, or "".

    The CSV stores no identity, and the check cannot match the author-scope heuristics again.
    It takes a row's recorded author heuristics as they are. It covers the recorded message
    heuristics, the tools that every recorded heuristic names and the attributions. For each
    tool that a recorded author heuristic names, it expects `account` when one of the tool's
    recorded author heuristics has `account`, and otherwise `convention`.
    """
    return _mismatch(row, match_row(row, tool_col, message_col, heuristics), heuristics)


def _mismatch(row: dict, match: Match, heuristics: Heuristics) -> str:
    """`mismatch_reason` of a row, with `match` the match of its message."""
    tools = set(match.tools)
    matched = match.recorded
    by_id = heuristics.by_id
    matched_tools = {by_id[n]["tool"] for n in matched}
    recorded = row.get("matched_heuristics")
    if recorded is None:
        extra = sorted(matched_tools - tools)
        if extra:
            return f"The message matches {extra}, but the row names {sorted(tools)}"
        return ""
    ids = [n for n in recorded.split(";") if n]
    unknown = [n for n in ids if n not in by_id]
    if unknown:
        return f"matched_heuristics {recorded!r} names the unknown heuristic ids {unknown}"
    recorded_tools = {by_id[n]["tool"] for n in ids}
    if recorded_tools != tools:
        return (f"matched_heuristics names the tools {sorted(recorded_tools)}, but the row names "
                f"{sorted(tools)}")
    message_rules = [n for n in ids if by_id[n]["scope"] != "author"]
    if message_rules != matched:
        return (f"The row records the message heuristics {message_rules}, but the heuristics "
                f"{matched} match the message now")
    expected = commits.format_attributions(
        commits.author_attributions([by_id[n] for n in ids if by_id[n]["scope"] == "author"])
        + list(heuristics.attributions(match.subject, match.body, match.tools)))
    if row.get("attributions") != expected:
        return (f"attributions is {row.get('attributions')!r}, but the heuristics give "
                f"{expected!r}")
    return ""


class Summary:
    """The counts that the check logs after the classification: the rows, and per tool the rows
    by detection source. A row that credits several tools counts its source for every one of
    them."""

    def __init__(self) -> None:
        self.rows = 0
        self.by_tool: Dict[str, Counter] = {}

    def add(self, tools: List[str], source: str) -> None:
        """Count one classified row."""
        self.rows += 1
        for tool in tools:
            self.by_tool.setdefault(tool, Counter())[source] += 1

    def log(self) -> None:
        """Log the counts on stderr through agentcontext.logs."""
        logs.info(COMPONENT, f"Counted {self.rows:,} rows by detection source.")
        for tool in sorted(self.by_tool):
            counts = self.by_tool[tool]
            tool_total = sum(counts.values())
            parts = []
            for src in ["message", "author_only"]:
                n = counts.get(src, 0)
                pct = 100 * n / tool_total if tool_total else 0
                parts.append(f"{n:,} ({pct:.1f}%)")
            logs.info(COMPONENT, f"Of the {tool_total:,} rows of {tool}, {parts[0]} have the "
                                 f"detection source message and {parts[1]} author_only.")


class _Classification:
    """The classification CSV, written a row at a time to a temporary file beside `target` and
    renamed to `target` once every row is written, so that a write that fails or stops leaves
    the previous file.

    The temporary name, the mode and the syncs follow `caches.replace_file`, which takes the
    whole text at once. This class writes the rows itself rather than through a new function in
    `agentcontext/caches.py`, because that module is part of the collector's code
    (`reuse.collector_files`), and a change to it alters the collector's fingerprint and needs a
    new version in `code_versions.COLLECTOR` with the reach of the change.
    """

    def __init__(self, target: Path, fieldnames: List[str]) -> None:
        self.target = Path(target)
        self.target.parent.mkdir(parents=True, exist_ok=True)
        # Remove a temporary file that a killed process left. `discard` removes the file after
        # an exception, but not after a SIGKILL or an OOM kill.
        for stale in caches.temporary_files(self.target):
            stale.unlink(missing_ok=True)
        while True:
            self.temporary = self.target.with_name(
                f"{self.target.name}.{secrets.token_hex(caches.TEMPORARY_NAME_LENGTH // 2)}.tmp")
            try:
                handle = os.open(self.temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            break
        self.file = os.fdopen(handle, "w", encoding="utf-8", newline="")
        self.writer = csv.DictWriter(self.file, fieldnames=fieldnames, extrasaction="ignore",
                                     lineterminator="\n")
        try:
            self.writer.writeheader()
        except BaseException:
            self.discard()
            raise

    def write(self, row: dict) -> None:
        """Write one classified row."""
        self.writer.writerow(row)

    def replace(self) -> None:
        """Sync the temporary file to disk and rename it to the target. The data is synced
        before the rename and the directory after it, as `caches.replace_file` does."""
        self.file.flush()
        os.fsync(self.file.fileno())
        self.file.close()
        # The mode of a tracked data file, set rather than left to the umask.
        os.chmod(self.temporary, 0o644)
        os.replace(self.temporary, self.target)
        caches.sync_directory(self.target.parent)

    def discard(self) -> None:
        """Remove the temporary file unless `replace` renamed it. Closing a file on a full disk
        raises when its buffer cannot be written, and the file is removed either way."""
        with contextlib.suppress(OSError):
            self.file.close()
        self.temporary.unlink(missing_ok=True)


def _not_written(output_path: str, error: OSError) -> int:
    """Log that the classification could not be written, and return the exit code 1."""
    logs.error(COMPONENT, f"Could not write {output_path} ({error}).")
    return 1


def check_csv(input_path: Path, output_path: str, heuristics: Heuristics, tool_col: str,
              message_col: str, summary_only: bool) -> int:
    """Classify one commit CSV, write the classification beside it unless `summary_only`, and
    return the verdict: 0 when every row agrees with what `heuristics` give over the columns that
    the CSV stores, 2 when a row does not, 1 when the CSV cannot be read.

    The check reads the CSV a row at a time, and classifies, writes and compares each row with
    the heuristics before it reads the next one. Its memory then depends on the longest row
    rather than on the size of the CSV, which grows with every collection.
    """
    if not input_path.is_file():
        logs.error(COMPONENT, f"{input_path} does not exist.")
        return 1

    with open(input_path, "r", encoding="utf-8", newline="") as f:
        rows = csv.reader(f)
        original_fieldnames = [c for c in next(rows, []) if c]
        for col_name, col_val in [("tool", tool_col), ("message", message_col)]:
            if col_val not in original_fieldnames:
                logs.error(COMPONENT, f"{input_path.name} has no {col_name} column '{col_val}'. "
                                      f"Its columns are {original_fieldnames}.")
                return 1
        # Count the rows that `csv.DictReader` gives below, without blank lines, so that the
        # progress messages can state the total.
        total = sum(1 for row in rows if row)

    logs.info(COMPONENT, f"{input_path.name} has {total:,} rows, with the tools in the column "
                         f"'{tool_col}' and the messages in the column '{message_col}'.")

    output = None
    if not summary_only:
        out_fieldnames = list(original_fieldnames)
        for col in NEW_COLUMNS:
            if col not in out_fieldnames:
                out_fieldnames.append(col)
        try:
            output = _Classification(Path(output_path), out_fieldnames)
        except OSError as e:
            return _not_written(output_path, e)

    summary = Summary()
    differences, listed = 0, []
    try:
        pace = logs.Pace()
        with open(input_path, "r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                match = match_row(row, tool_col, message_col, heuristics)
                columns = classification(match, heuristics)
                summary.add(match.tools, columns[0])
                why = _mismatch(row, match, heuristics)
                if why:
                    differences += 1
                    if len(listed) < DIFFERENCES_LISTED:
                        listed.append((row.get("repo_name", ""),
                                       (row.get("commit_sha") or "")[:12], why))
                if output is not None:
                    try:
                        output.write({**row, **dict(zip(NEW_COLUMNS, columns))})
                    except OSError as e:
                        return _not_written(output_path, e)
                if pace.due():
                    logs.info(COMPONENT, f"{summary.rows:,}/{total:,} rows classified.")

        logs.info(COMPONENT, f"Classified {summary.rows:,} rows.")
        summary.log()

        if output is None:
            logs.info(COMPONENT, "Wrote no CSV because of --summary-only.")
        else:
            try:
                output.replace()
            except OSError as e:
                return _not_written(output_path, e)
            logs.info(COMPONENT, f"Wrote {output_path}.")
    finally:
        if output is not None:
            output.discard()

    # The verdict comes after the classification, which is on disk whatever the verdict is.
    if differences:
        logs.warning(COMPONENT, f"{differences:,} of {summary.rows:,} row(s) of {input_path.name} "
                                f"differ from what the current heuristics give for the columns "
                                f"that the CSV stores. The first ten follow.")
        for repo, sha, why in listed:
            logs.warning(COMPONENT, f"The row of commit {sha} of {repo} differs ({why}).")
        return 2
    logs.info(COMPONENT, f"{summary.rows:,} of {summary.rows:,} row(s) of {input_path.name} "
                         f"match what the current heuristics give for the columns that the CSV "
                         f"stores.")
    return 0


def main(argv=None) -> int:
    p = cli.ArgumentParser(
        description=(
            "Match the message patterns against every row of the commit CSV again, record "
            "whether a message pattern or only the author or committer detected each commit, "
            "and exit 2 when a row differs from what the current heuristics give."
        )
    )
    p.add_argument("-i", "--input", required=True,
                   help="The commit CSV, output/all_ai_commits.csv")
    p.add_argument(
        "-o", "--output", default=None,
        help="CSV to write the classification of the rows to (default: <stem>-validated.csv)",
    )
    p.add_argument(
        "--other-input", default=None,
        help="Commit CSV of the tools outside the six (output/other_ai_commits.csv), checked "
             "against other_ai_commit_patterns.csv and classified into <stem>-validated.csv "
             "beside it",
    )
    p.add_argument(
        "--tool-col", default="ai_tool",
        help="Column name for detected tools (default: ai_tool)",
    )
    p.add_argument(
        "--message-col", default="commit_message",
        help="Column name for commit messages (default: commit_message)",
    )
    p.add_argument(
        "--summary-only", action="store_true",
        help="Log the summary counts and write no CSV",
    )
    args = p.parse_args(argv)

    # Raise the CSV field size limit for long commit messages.
    csv.field_size_limit(sys.maxsize)

    codes = [check_csv(Path(args.input), args.output or default_output_path(args.input),
                       TOOLS, args.tool_col, args.message_col, args.summary_only)]
    if args.other_input:
        codes.append(check_csv(Path(args.other_input), default_output_path(args.other_input),
                               OTHER_TOOLS, args.tool_col, args.message_col, args.summary_only))
    # A CSV that could not be read outweighs a difference found in the other.
    return 1 if 1 in codes else 2 if 2 in codes else 0


if __name__ == "__main__":
    raise SystemExit(main())
