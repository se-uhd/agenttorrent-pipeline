"""The regression manifest: `python -m agentcontext regressions check`

Record what `output/` contains, and report every difference between that record and a later run.

A rename of a script, directory, column or output file should not change a published value.
`--write` records a manifest that a later run has to reproduce. The check then reports every
changed row, and you trace each one to a named change.

The manifest is keyed by mechanism, not by filename. `context-files.csv` becoming
`release/context_files.csv` is a rename and has to compare equal. A configuration artifact that
is counted under `other` instead of `rules` moved between the CSVs and has to be reported. Each
entry records the file's SHA-256, its row count, its columns, and the count of TRUE in every
boolean column, so that a finding can state "three more repositories have a skill" rather than
"the file changed".

Exit codes follow the documentation watch, for the same reason: A check that cannot run must
not be read as a check that passed.

  0  the manifest matches
  1  the manifest or the output is missing or unreadable
  2  something differs, and the log messages name each difference

Inputs:
  - ``output/*.csv``: the mechanism CSVs, ``planning-documents.csv``, ``imports.csv``, ``repos.csv``,
    ``excluded.csv`` and ``consistency-report.csv``, which is the whole of ``TRACKED_FILES``

Outputs:
  - ``output/baseline.json`` with ``--write``, and otherwise the comparison as log messages on
    stderr

Comparing two runs: `python -m agentcontext regressions compare`

Compare two stage directories and report every difference between them.

A subsample run runs in a separate clone of this repository. It shows that a later run over the
same mirrors reproduces an earlier run, or that every difference is one that a change was meant
to make. `regressions check` tests the reproduction for the tracked files of `output/` against a
manifest. `regressions compare` tests it for two runs side by side, with the same findings, and
covers every file that a run writes and that a person reads a result from: the records of
`output/` beside the mechanism CSVs, the caches and `deletion.csv`, the two repository lists at
the roots of `data/collected/` and `data/validated/`, the files that the release step builds
under `release/`, and the JSON records (`KEYED_FILES` and `JSON_FILES`).

The command compares every file row by row on its key. It names the rows that were added, the
rows that were removed, and the columns that changed on the rows that both runs have, with a
count per column and the first `EXAMPLES` keys. A key that appears on more than one row of a
file is a difference of its own, named with the run that it is in, because only the first of
those rows can be compared. A column named with `--ignore` is left out of the comparison, and a
file with changes only in such a column has no difference and gets a note in the log. A repeat
run changes `scanned_at` by design. The default still ignores nothing, because the list of
columns that change on a repeat run is itself a result.

Inputs:
  - two stage directories, each with the layout of ``data/collection/``: ``output/*.csv`` and
    the JSON records beside them, the caches and ``deletion.csv``, ``data/collected/repos.csv``
    and ``data/validated/repos.csv``, and ``release/``. A file absent from both stage
    directories is skipped, and a file absent from one is reported

Outputs:
  - the findings as log messages on stderr, and in ``--report PATH`` when given

Exit codes follow the other comparison commands: 0 when nothing differs, 2 when something
does, 1 when a stage directory is missing or a file could not be read.

The manifest of `check` covers only the CSVs that `TRACKED_FILES` names. The repository tracks
the commit CSVs as archives, which are extracted only where a check needs them, and the stated
statistics count them. `snapshot write --check --outputs-only` compares every derivable section of
`snapshot.json` with the outputs, and `tests/test_heuristics.py` checks `provenance.json`
against the heuristics and the mechanism CSVs. `compare` reads each of these files.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sys
from pathlib import Path

from agentcontext import caches, cli, STAGE_DIR, heuristics
from agentcontext import logs
from agentcontext import planning_schema
from agentcontext.steps import import_detection


# -----------------------------------------------------------------------------
# The manifest
# -----------------------------------------------------------------------------
DEFAULT_OUTPUT_DIR = STAGE_DIR / "output"
DEFAULT_BASELINE = DEFAULT_OUTPUT_DIR / "baseline.json"

# The manifest key of each mechanism, which keys `output/baseline.json` and therefore cannot
# change, mapped to the heuristics' name for the mechanism. The filename comes from the
# heuristics. Repeating the filenames here would let a rename there change what `compare` reads
# while `build` reports the mechanism as gone.
MECHANISM_KEYS = {
    "context_files": "context_file",
    "settings": "settings",
    "skills": "skills",
    "subagents": "subagents",
    "commands": "commands",
    "hooks": "hooks",
    "rules": "rules",
    "mcp": "mcp",
    "plugins": "plugins",
    "lsp": "lsp",
    "other": "other",
}
TRACKED_FILES = {
    **{key: heuristics.MECHANISM_CSVS[mechanism][0]
       for key, mechanism in MECHANISM_KEYS.items()},
    "repos": "repos.csv",
    "excluded": "excluded.csv",
    "planning_documents": planning_schema.CSV_NAME,
    "imports": import_detection.CSV_NAME,
    # A step that is not always run writes this file. `build` skips an absent file, and
    # listing it changes nothing for a run that did not write it.
    "consistency_report": "consistency-report.csv",
}

BOOLEAN_VALUES = {"TRUE", "FALSE"}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        # GROUNDING: 1 MiB per read, which bounds the memory that hashing uses. The chunk size
        # does not change the digest and needs no tuning.
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def boolean_columns(rows: list[dict], columns: list[str]) -> list[str]:
    """Columns with only TRUE, FALSE or blank values. An empty file has none.

    Blanks are allowed because otherwise a single blank would disqualify the whole column: The
    column would drop out of `true_counts`, and `compare` would have nothing to compare and
    report no change in its counts. A column that is entirely blank is not boolean and is still
    excluded.
    """
    if not rows:
        return []
    out = []
    for c in columns:
        values = [(row.get(c) or "").upper() for row in rows]
        if any(v in BOOLEAN_VALUES for v in values) and all(
                v in BOOLEAN_VALUES or v == "" for v in values):
            out.append(c)
    return out


def describe(path: Path) -> dict:
    """Everything that the manifest records about one CSV."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        columns = list(reader.fieldnames or [])
        rows = list(reader)
    return {
        "file": path.name,
        "sha256": sha256_of(path),
        "rows": len(rows),
        "columns": columns,
        "true_counts": {c: sum(1 for r in rows if (r.get(c) or "").upper() == "TRUE")
                        for c in boolean_columns(rows, columns)},
        "repositories": len({r["repo_name"] for r in rows if r.get("repo_name")}),
    }


def build(output_dir: Path) -> dict:
    """Describe every tracked file that is present."""
    manifest = {}
    for mechanism, filename in TRACKED_FILES.items():
        path = Path(output_dir) / filename
        if path.is_file():
            manifest[mechanism] = describe(path)
    return manifest


def compare(baseline: dict, current: dict) -> list[str]:
    """Every difference, one line each, sorted by mechanism."""
    findings = []
    for mechanism in sorted(set(baseline) | set(current)):
        was, now = baseline.get(mechanism), current.get(mechanism)
        if was is None:
            findings.append(f"{mechanism} is new, with {now['rows']:,} rows in {now['file']}")
            continue
        if now is None:
            findings.append(f"{mechanism} is gone (it had {was['rows']:,} rows in "
                            f"{was['file']})")
            continue
        if was["sha256"] == now["sha256"]:
            continue

        here = []
        if was["file"] != now["file"]:
            here.append(f"{mechanism} was renamed from {was['file']} to {now['file']}")
        if was["rows"] != now["rows"]:
            here.append(f"The rows of {mechanism} went from {was['rows']:,} to "
                        f"{now['rows']:,} ({now['rows'] - was['rows']:+,d})")
        if was["repositories"] != now["repositories"]:
            here.append(f"The repositories of {mechanism} went from {was['repositories']:,} to "
                        f"{now['repositories']:,} "
                        f"({now['repositories'] - was['repositories']:+,d})")
        added = [c for c in now["columns"] if c not in was["columns"]]
        removed = [c for c in was["columns"] if c not in now["columns"]]
        if added:
            here.append(f"{mechanism} gained the column(s) {', '.join(added)}")
        if removed:
            here.append(f"{mechanism} lost the column(s) {', '.join(removed)}")
        for column in sorted(set(was["true_counts"]) | set(now["true_counts"])):
            before = was["true_counts"].get(column)
            after = now["true_counts"].get(column)
            if before is None or after is None:
                # The column is no longer boolean, or has become boolean. The comparison reports
                # it, because skipping it would hide a change of its count.
                here.append(f"{mechanism}.{column} is no longer comparable as a boolean column"
                            if after is None else
                            f"{mechanism}.{column} is newly comparable as a boolean column")
                continue
            if before == after:
                continue
            here.append(f"{TRUE_COUNT_OF}{mechanism}.{column} went from {before:,} to {after:,} "
                        f"({after - before:+,d})")
        # A file with changed bytes and unchanged counts gets a finding of its own: Its rows
        # were reordered or reformatted, or a value inside a row changed.
        if not here:
            here.append(contents_changed(mechanism))
        findings.extend(here)
    return findings


# The start of the finding of a changed count of TRUE, which `compare_roots` leaves out for an
# ignored column.
TRUE_COUNT_OF = "The count of TRUE in "


def contents_changed(mechanism: str) -> str:
    """The finding of a tracked file with changed bytes and unchanged counts."""
    return f"The contents of {mechanism} changed with no change to any count"


def main_check(argv=None) -> int:
    parser = cli.ArgumentParser(description="Compare the tracked CSVs of output/ with output/baseline.json, or record them as the baseline with --write.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--baseline", type=Path, default=None,
                        help="The manifest (default: baseline.json in --output-dir)")
    parser.add_argument("--write", action="store_true",
                        help="Record the tracked CSVs as the baseline")
    args = parser.parse_args(argv)
    # The manifest belongs to the directory that it describes. A default of the stage's own
    # baseline would make `--output-dir /scratch` compare a scratch tree with the repository's
    # tracked baseline, and `--write` overwrite that baseline.
    if args.baseline is None:
        args.baseline = args.output_dir / "baseline.json"

    if not args.output_dir.is_dir():
        logs.error("regressions", f"The output directory {args.output_dir} does not exist.")
        return 1

    current = build(args.output_dir)
    if not current:
        logs.error("regressions", f"No tracked CSV found in {args.output_dir}.")
        return 1

    if args.write:
        # Replace the baseline in one step, because it is tracked and writing it in place could
        # leave it truncated after a failure.
        try:
            caches.replace_file(args.baseline,
                                json.dumps(current, indent=2, sort_keys=True) + "\n")
        except OSError as e:
            logs.error("regressions", f"Could not write {args.baseline} ({e}).")
            return 1
        total = sum(e["rows"] for e in current.values())
        logs.info("regressions", f"Wrote {args.baseline.name} ({len(current):,} files, "
                                 f"{total:,} rows).")
        for mechanism in sorted(current):
            entry = current[mechanism]
            logs.info("regressions", f"  {mechanism:15} {entry['rows']:>6,} rows  "
                                     f"{entry['repositories']:>5,} repos  {entry['file']}")
        return 0

    if not args.baseline.is_file():
        logs.error("regressions", f"No baseline at {args.baseline}. Write one with --write.")
        return 1
    try:
        with open(args.baseline, encoding="utf-8") as f:
            baseline = json.load(f)
    except ValueError as e:
        logs.error("regressions", f"{args.baseline} is not readable JSON ({e}).")
        return 1

    findings = compare(baseline, current)
    if not findings:
        logs.info("regressions", f"No change against {args.baseline.name} "
                                 f"({len(current):,} files, "
                                 f"{sum(e['rows'] for e in current.values()):,} rows).")
        return 0

    logs.info("regressions", f"Found {len(findings):,} difference(s) against "
                             f"{args.baseline.name}.")
    for finding in findings:
        logs.info("regressions", f"  {finding}.")
    logs.info("regressions", "Trace each difference to a named change before you commit that "
                             "change.")
    return 2


# -----------------------------------------------------------------------------
# Comparing two runs
# -----------------------------------------------------------------------------

csv.field_size_limit(sys.maxsize)

# The number of keys that a finding names before it states how many more there are.
EXAMPLES = 5

# The files compared and the columns that identify a row in each, relative to a stage directory.
# The mechanism CSVs are added below from the heuristics, keyed on the repository and the column
# that names the configuration artifact.
#
# The list covers every file that a run writes and that a person reads a result from, including
# the records of `output/` beside the mechanism CSVs and the files that the release step builds.
# It leaves out these files, each for a reason:
#   - a release file that the release step copies from `output/` or from `heuristics/`, which
#     differs between two runs exactly when its source does
#   - the release's README, which states counts computed from the files compared here, and its
#     CITATION.cff, which the code writes
#   - `checksums.sha256` and the `.7z` archives, with bytes that record the times at which the
#     files were written and contents that are the files compared here
#   - the files that a tool writes outside a run: `identity-*.csv`, `repo-sizes.csv` and
#     `baseline.json`
KEYED_FILES = {
    "output/repos.csv": ("repo_name",),
    "output/excluded.csv": ("repo_name",),
    "output/consistency-report.csv": None,       # positional: one row per CSV
    "output/ai_commits-repos.csv": ("repo_name",),
    "output/deletions.csv": ("kind", "repo_name", "subject"),
    "output/unmatched-paths.csv": ("glob", "found_in"),
    "output/unmatched-commits.csv": ("kind", "token"),
    "file_history.csv": ("repo_name", "head_sha", "mechanism", "artifact"),
    "ai_authorship.csv": ("repo_name", "head_sha", "artifact"),
    "submodules.csv": ("repo_name", "path"),
    "deletion.csv": ("path", "reason"),
    "output/all_ai_commits.csv": ("repo_name", "commit_sha"),
    "output/other_ai_commits.csv": ("repo_name", "commit_sha"),
    "output/all_ai_commits-unmatched.csv": ("kind", "token"),
    "output/all_ai_commits-mentions.csv": ("tool", "pattern"),
    "output/all_ai_commits-validated.csv": ("repo_name", "commit_sha"),
    "output/other_ai_commits-validated.csv": ("repo_name", "commit_sha"),
    "data/collected/repos.csv": ("repo_name",),
    "data/collected/clone_failed.csv": ("repo_name",),
    "data/validated/repos.csv": ("repo_name",),
    "release/repos.csv": ("repo_name",),
    "release/commits.csv": ("repo_name", "commit_sha"),
    "release/other_commits.csv": ("repo_name", "commit_sha"),
    "release/context_files.csv": ("repo_name", heuristics.MECHANISM_CSVS["context_file"][1]),
    "release/unmatched-attributions.csv": ("kind", "token"),
}
for _mechanism, (_filename, _artifact) in heuristics.MECHANISM_CSVS.items():
    KEYED_FILES[f"output/{_filename}"] = ("repo_name", _artifact)
KEYED_FILES[f"output/{planning_schema.CSV_NAME}"] = ("repo_name", planning_schema.PATH_COLUMN)
KEYED_FILES[f"output/{import_detection.CSV_NAME}"] = ("repo_name", *import_detection.ROW_KEY)

# The JSON records compared, relative to a stage directory. Each leaf of a document is a row
# keyed by its JSON Pointer (RFC 6901), and a finding names a changed value by this location in
# the document. An array is one value. `--ignore` names a key and leaves out every leaf below a
# key of that name. The snapshot's `scanned_at_window` changes with `scanned_at` on every run,
# and `--ignore scanned_at` leaves out both this key and the CSVs' column.
JSON_FILES = (
    "output/snapshot.json",
    "output/provenance.json",
    "output/commit-reclassification.json",
    "release/sampling.json",
)


def read_rows(path: Path) -> tuple:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def keyed(rows: list, key: tuple | None) -> tuple:
    """Rows by their key, by position when the file has no key, and the sorted keys that
    appear on more than one row.

    A repeated key keeps its first row in the mapping, and the row comparison cannot compare the
    other rows with that key. The function therefore returns the repeated keys, and
    `compare_file` reports them. Dropping them silently would report a second
    `file_history.csv` row with another `#commits` as a change of the row order or the line
    endings.
    """
    if key is None:
        return {(str(i),): row for i, row in enumerate(rows)}, []
    out, repeated = {}, set()
    for row in rows:
        k = tuple(row.get(c, "") for c in key)
        if k in out:
            repeated.add(k)
        out.setdefault(k, row)
    return out, sorted(repeated)


def _named(keys: list) -> str:
    shown = ", ".join(" ".join(k) for k in keys[:EXAMPLES])
    more = len(keys) - EXAMPLES
    return shown + (f", and {more} more" if more > 0 else "")


def compare_file(rel: str, key: tuple | None, first: Path, second: Path,
                 ignore: set, notes: list | None = None) -> list:
    """Every difference in one file, one line each, prefixed with the file's name. A file
    with changes only in ignored columns has no difference, and `notes` records the
    columns."""
    a, b = first / rel, second / rel
    if not a.is_file() and not b.is_file():
        return []
    if not a.is_file():
        return [f"{rel} is only in the second run"]
    if not b.is_file():
        return [f"{rel} is only in the first run"]
    bytes_a, bytes_b = a.read_bytes(), b.read_bytes()
    if bytes_a == bytes_b:
        return []
    columns_a, rows_a = read_rows(a)
    columns_b, rows_b = read_rows(b)
    findings = []
    if columns_a != columns_b:
        added = [c for c in columns_b if c not in columns_a]
        removed = [c for c in columns_a if c not in columns_b]
        if added:
            findings.append(f"{rel} gained the column(s) {', '.join(added)}")
        if removed:
            findings.append(f"{rel} lost the column(s) {', '.join(removed)}")
        if not added and not removed:
            findings.append(f"The columns of {rel} were reordered")
    (left, repeated_a), (right, repeated_b) = keyed(rows_a, key), keyed(rows_b, key)
    for run, repeated in (("first", repeated_a), ("second", repeated_b)):
        if repeated:
            findings.append(f"{len(repeated):,} key(s) of {rel} are on more than one row in the "
                            f"{run} run ({_named(repeated)})")
    entered = sorted(k for k in right if k not in left)
    gone = sorted(k for k in left if k not in right)
    if entered:
        findings.append(f"{rel} gained {len(entered):,} row(s) ({_named(entered)})")
    if gone:
        findings.append(f"{rel} lost {len(gone):,} row(s) ({_named(gone)})")
    moved = {}
    for k in sorted(set(left) & set(right)):
        for column in columns_b:
            if column in ignore or column not in columns_a:
                continue
            if left[k].get(column, "") != right[k].get(column, ""):
                moved.setdefault(column, []).append(k)
    for column, keys in moved.items():
        findings.append(f"{rel}.{column} changed on {len(keys):,} row(s) ({_named(keys)})")
    if not findings:
        # The bytes differ, and no row was added, removed or changed outside the ignored
        # columns. The bytes show what the parsed rows cannot: the order of the rows, the line
        # endings, a field that no column names, and bytes that a CSV writer would not write for
        # the rows, such as a quote or a missing final newline. Only when none of these changed
        # does an ignored column explain the difference, and then the file has no difference and
        # gets a note. Counting the ignored column as a difference would make every comparison of
        # two runs at different times exit 2 because of `scanned_at` alone. Taking it as the
        # whole explanation would hide every other byte that changed.
        findings += _bytes_outside(rel, bytes_a, bytes_b, list(left) != list(right),
                                   [columns_a.index(c) for c in ignore if c in columns_a])
        ignored_moved = sorted(
            column for column in ignore
            if column in columns_a and column in columns_b
            and any(left[k].get(column, "") != right[k].get(column, "")
                    for k in set(left) & set(right)))
        if not findings and ignored_moved:
            if notes is not None:
                notes.append(f"Only the ignored column(s) {', '.join(ignored_moved)} changed in "
                             f"{rel}, which is not a difference")
        elif not findings:
            findings.append(f"The bytes of {rel} differ with every row, field and column equal")
    return findings


def _written(data: bytes) -> tuple:
    """The rows of a CSV as lists of fields, with every field kept, its line ending, and
    whether its bytes are the bytes that a CSV writer with that line ending writes for the
    rows."""
    text = data.decode("utf-8")
    ending = "\r\n" if "\r\n" in text else "\n"
    rows = list(csv.reader(io.StringIO(text, newline="")))
    out = io.StringIO()
    csv.writer(out, lineterminator=ending).writerows(rows)
    return rows, ending, out.getvalue() == text


def _bytes_outside(rel: str, bytes_a: bytes, bytes_b: bytes, reordered: bool,
                   ignored: list) -> list:
    """The byte differences of two files with equal rows outside the ignored columns."""
    rows_a, ending_a, plain_a = _written(bytes_a)
    rows_b, ending_b, plain_b = _written(bytes_b)
    findings = []
    if reordered:
        findings.append(f"The order of the rows of {rel} changed")
    if ending_a != ending_b:
        findings.append(f"The line endings of {rel} changed")
    for rows in (rows_a, rows_b):
        for row in rows:
            for index in ignored:
                if index < len(row):
                    row[index] = ""
    if not reordered and rows_a != rows_b:
        findings.append(f"A row of {rel} contains a field that no column names")
    for run, plain in (("first", plain_a), ("second", plain_b)):
        if not plain:
            findings.append(f"The bytes of {rel} in the {run} run differ from what a CSV writer "
                            f"writes for its rows, because of a quote, a field that no column "
                            f"names, or a missing final newline")
    return findings


def json_leaves(document) -> dict:
    """Every leaf of a JSON document, `{pointer: (path components, value as JSON)}`, the
    pointer per RFC 6901, so that a key that contains a slash, such as a repository name, stays
    one component. An array or an empty object is one value."""
    leaves = {}

    def visit(node, path: tuple) -> None:
        if isinstance(node, dict) and node:
            for key, value in node.items():
                visit(value, (*path, str(key)))
            return
        pointer = "".join("/" + part.replace("~", "~0").replace("/", "~1") for part in path)
        leaves[pointer] = (path, json.dumps(node, sort_keys=True))

    visit(document, ())
    return leaves


def compare_json(rel: str, first: Path, second: Path, ignore: set,
                 notes: list | None = None) -> list:
    """Every difference in one JSON record, one line each, prefixed with the file's name: the
    leaves that were added, removed or changed, each changed leaf with its two values. A leaf
    below a key that `ignore` names is left out, and a file with changes only in such leaves has
    no difference and gets a note in `notes`. Raises ValueError for a file that is not JSON."""
    a, b = first / rel, second / rel
    if not a.is_file() and not b.is_file():
        return []
    if not a.is_file():
        return [f"{rel} is only in the second run"]
    if not b.is_file():
        return [f"{rel} is only in the first run"]
    bytes_a, bytes_b = a.read_bytes(), b.read_bytes()
    if bytes_a == bytes_b:
        return []
    leaves = []
    for path in (a, b):
        try:
            leaves.append(json_leaves(json.loads(path.read_text(encoding="utf-8"))))
        except ValueError as e:
            raise ValueError(f"{path} is not readable JSON ({e})") from e
    left, right = leaves
    ignored = {pointer for pointer, (path, _value) in (*left.items(), *right.items())
               if ignore & set(path)}
    entered = sorted(p for p in right if p not in left and p not in ignored)
    gone = sorted(p for p in left if p not in right and p not in ignored)
    moved = sorted(p for p in set(left) & set(right)
                   if p not in ignored and left[p][1] != right[p][1])
    findings = []
    if entered:
        findings.append(f"{rel} gained {len(entered):,} key(s) "
                        f"({_named([(p,) for p in entered])})")
    if gone:
        findings.append(f"{rel} lost {len(gone):,} key(s) ({_named([(p,) for p in gone])})")
    if moved:
        shown = ", ".join(f"{p} from {left[p][1]} to {right[p][1]}" for p in moved[:EXAMPLES])
        more = len(moved) - EXAMPLES
        findings.append(f"{len(moved):,} value(s) of {rel} changed ({shown}"
                        + (f", and {more} more" if more > 0 else "") + ")")
    if findings:
        return findings
    names = set()
    for pointer in ignored:
        was, now = left.get(pointer), right.get(pointer)
        if (was and was[1]) != (now and now[1]):
            names |= ignore & set((was or now)[0])
    if names:
        if notes is not None:
            notes.append(f"Only the ignored key(s) {', '.join(sorted(names))} changed in {rel}, "
                         f"which is not a difference")
        return []
    # The bytes differ with every leaf equal, which means that the order of the keys or the
    # layout changed.
    return [f"The bytes of {rel} differ with every key and value equal"]


def compared(rel: str, key: tuple | None, first: Path, second: Path, ignore: set,
             notes: list | None) -> list:
    """`compare_file`, with the CSV that could not be read named in the error. The texts of
    `UnicodeDecodeError` and `csv.Error` name no file."""
    try:
        return compare_file(rel, key, first, second, ignore, notes)
    except (UnicodeDecodeError, csv.Error) as error:
        raise ValueError(f"Could not read {rel} as a CSV of UTF-8 text "
                         f"({error.__class__.__name__} {error})") from error


def compare_roots(first: Path, second: Path, ignore: set, notes: list | None = None) -> list:
    """Every difference between two stage directories, in a fixed file order. `notes` collects
    the files with changes only in ignored columns."""
    by_file = {rel: compared(rel, key, first, second, ignore, notes)
               for rel, key in KEYED_FILES.items()}
    by_file.update({rel: compare_json(rel, first, second, ignore, notes) for rel in JSON_FILES})
    # The manifest's findings come first: counts per tracked file, which state "three more
    # repositories have a skill" rather than "the file changed". Its finding for changed bytes
    # with unchanged counts is left out where the row comparison of the same file found nothing,
    # because then the bytes changed only in an ignored column.
    unchanged = {mechanism for mechanism, filename in TRACKED_FILES.items()
                 if not by_file.get(f"output/{filename}")}
    # Its count of an ignored column is left out too, as the row comparison leaves out the
    # column. Otherwise `--ignore skills` would report `repos.skills: TRUE from 2 to 1 (-1)`
    # beside the note that the column changed.
    findings = [line for line in compare(build(first / "output"), build(second / "output"))
                if not any(line == contents_changed(mechanism) for mechanism in unchanged)
                and not any(line.startswith((f"{mechanism}.{column} ",
                                             f"{TRUE_COUNT_OF}{mechanism}.{column} "))
                            for mechanism in TRACKED_FILES for column in ignore)]
    for rel in (*KEYED_FILES, *JSON_FILES):
        findings.extend(by_file[rel])
    return findings


def main_compare(argv=None) -> int:
    parser = cli.ArgumentParser(description="List every difference between two stage directories: first the manifest's counts for the tracked files, then every file row by row.")
    parser.add_argument("first", type=Path, help="The reference stage directory")
    parser.add_argument("second", type=Path, help="The stage directory compared against it")
    parser.add_argument("--ignore", action="append", default=[], metavar="COLUMN",
                        help="Column or JSON key to leave out of the comparison (repeatable)")
    parser.add_argument("--report", type=Path, default=None,
                        help="Also write the findings to this file")
    args = parser.parse_args(argv)

    for root in (args.first, args.second):
        if not root.is_dir() or not (root / "output").is_dir():
            logs.error("regressions", f"{root} is not a stage directory (no output/ directory).")
            return 1
    read = [root for root in (args.first, args.second)
            if build(root / "output")
            or any((root / rel).is_file() for rel in (*KEYED_FILES, *JSON_FILES))]
    if len(read) < 2:
        # Two stage directories without any compared file compare equal, which a caller would
        # read as "these two runs agree". `build` skips an absent file and `compare_file`
        # returns an empty list for it, and the verdict would then rest on the absence of the
        # files.
        holder = "neither stage directory has" if not read else f"only {read[0]} has"
        logs.error("regressions", f"Nothing to compare, because {holder} a file that this "
                                  f"command compares.")
        return 1
    notes = []
    try:
        findings = compare_roots(args.first, args.second, set(args.ignore), notes)
    except OSError as e:
        # An error of the operating system names its file in `filename`.
        logs.error("regressions", f"{str(e).rstrip('.')}." if e.filename is None
                   else f"Could not read {e.filename} ({e.strerror}).")
        return 1
    except (csv.Error, KeyError, ValueError) as e:
        # ValueError as well: a JSON record that does not parse, and a CSV that is not UTF-8.
        # The text of the first names the record, and `compared` names the CSV.
        logs.error("regressions", f"{str(e).rstrip('.')}.")
        return 1
    text = (f"{len(findings):,} difference(s) between {args.first} and {args.second}"
            + (f", ignoring {', '.join(args.ignore)}" if args.ignore else "") + "\n"
            + "".join(f"  {line}\n" for line in findings))
    logs.info("regressions", f"{text.splitlines()[0]}.")
    for line in findings:
        logs.info("regressions", f"  {line}.")
    for line in notes:
        logs.info("regressions", f"  {line}.")
    if args.report is not None:
        args.report.write_text(text, encoding="utf-8")
    return 2 if findings else 0


SUBCOMMANDS = {
    "check": main_check,
    "compare": main_compare,
}


SUBCOMMAND_SUMMARY = 'check compares output/ with the manifest, and compare lists the differences between two stage directories'


def main(argv=None) -> int:
    return cli.subcommand_main("regressions", SUBCOMMANDS, SUBCOMMAND_SUMMARY, argv)


if __name__ == "__main__":
    raise SystemExit(main())
