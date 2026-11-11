#!/usr/bin/env python3
"""Engineered project selection, the last step of the classification: the sample from the labels.

Reads repos_annotated.csv and keeps the rows labeled engineered_project=true, with their
columns and order. The labels determine the sample size. The step refuses a GitHub id that two
rows of the sample have, because the two names are one repository and the collection would
clone it twice. An optional --expect-rows checks a count that the caller supplies before
anything is written, and it has no default tied to a collection or a publication.

The step replaces the output atomically. --check compares the derived sample with the existing
output without writing. It exits 0 when the two agree, 2 when they differ, and 1 for an
unreadable or invalid input or a count other than --expect-rows.
"""

from __future__ import annotations

import csv
import io
import os
import sys
import tempfile
from pathlib import Path
from agentcontext import CLASSIFICATION_DIR
from agentcontext import cli
from agentcontext import logs

# Nothing limits the length of the model's justification. A justification longer than the
# default field limit of the csv module would stop the step at every Start.
csv.field_size_limit(sys.maxsize)


# Both defaults are paths under `CLASSIFICATION_DIR`, not the working directory, because the step
# reads and writes the same files wherever it is started. The cloning step's DEFAULT_REPO_CSV
# names the same output path, and test_runner.py checks that the two paths are equal.
DEFAULT_ANNOTATED = CLASSIFICATION_DIR / "repos_annotated.csv"
DEFAULT_OUTPUT = CLASSIFICATION_DIR / "repos_annotated_included.csv"

# The label column that this step filters on, and the column that the collector uses as the
# key. The step refuses a file without either column. Without `repo_name` the collector reads
# no repository, and without `engineered_project` the step would keep every row as the sample.
LABEL_COLUMN = "engineered_project"
REPO_COLUMN = "repo_name"
INCLUDED_LABEL = "true"
# GitHub's id of the repository, which the classification copies from the sampling frame. Two
# names in the sample with one id are one repository under its old and its new name. A row with
# a blank id takes part in no pair, because a blank id names no repository.
ID_COLUMN = "github_id"


def derive_sample(annotated_csv: Path) -> tuple[list[str], list[dict]]:
    """Read the annotated list and return its header and the rows that the sample keeps."""
    with open(annotated_csv, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = list(reader.fieldnames or [])
        missing = [c for c in (REPO_COLUMN, ID_COLUMN, LABEL_COLUMN) if c not in header]
        if missing:
            raise ValueError(f"{annotated_csv} lacks the column(s) {missing}. "
                             f"The header is {header}.")
        rows = [row for row in reader if row[LABEL_COLUMN] == INCLUDED_LABEL]
    first, repeated = {}, []
    for row in rows:
        github_id = (row[ID_COLUMN] or "").strip()
        if not github_id:
            continue
        if github_id in first:
            repeated.append(f"{first[github_id]} and {row[REPO_COLUMN]} with id {github_id}")
        else:
            first[github_id] = row[REPO_COLUMN]
    if repeated:
        raise ValueError(f"{len(repeated):,} GitHub id(s) appear in two rows of the sample in "
                         f"{annotated_csv}, under two names of one repository "
                         f"({', '.join(repeated[:5])}). Keep one row for each id.")
    return header, rows


def render(header: list[str], rows: list[dict]) -> str:
    """The list as the step writes it, which `--check` compares instead of the file."""
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue()


def write(path: Path, text: str) -> None:
    """Replace the list atomically.

    The cloning step reads this file once, at the start of its run, and does not expect it to
    change. A direct write that is interrupted leaves the file truncated at a row boundary, and
    the cloning step would accept the truncated file as a shorter sample.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Write the sample, the repositories that the classification labeled "
                    "engineered_project=true.")
    parser.add_argument("--annotated-csv", type=Path, default=DEFAULT_ANNOTATED,
                        help=f"The labels of the classification to filter "
                             f"(default: {DEFAULT_ANNOTATED})")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"Sample list to write (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--expect-rows", type=int, default=None,
                        help="Fail unless the sample has this many rows (default: no count check)")
    parser.add_argument("--check", action="store_true",
                        help="Compare the sample on disk with the derived sample, without "
                             "writing it")
    args = parser.parse_args(argv)

    if not args.annotated_csv.is_file():
        logs.error('engineered_project_selection', f"Could not find the labels of the "
                   f"classification in {args.annotated_csv}. Run the classification stage, or "
                   f"extract repos_annotated.csv from the tracked archive repos_annotated.7z "
                   f"beside it.")
        return 1

    try:
        header, rows = derive_sample(args.annotated_csv)
    except OSError as e:
        logs.error('engineered_project_selection', f"Could not read {args.annotated_csv} ({e}).")
        return 1
    except ValueError as e:
        logs.error('engineered_project_selection', f"{str(e).rstrip('.')}.")
        return 1

    if args.expect_rows is not None and len(rows) != args.expect_rows:
        logs.error('engineered_project_selection', f"{args.annotated_csv} has {len(rows):,} "
                   f"row(s) with {LABEL_COLUMN}={INCLUDED_LABEL!r}, but --expect-rows expected "
                   f"{args.expect_rows:,}. Nothing was written. Check the input or the "
                   f"--expect-rows value.")
        return 1

    text = render(header, rows)

    if args.check:
        if not args.output.is_file():
            logs.warning('engineered_project_selection', f"{args.output} does not exist. A run would write "
                                  f"{len(rows):,} rows.")
            return 2
        # Compare bytes, with CRLF read as LF. Universal newlines would turn a carriage return
        # inside a quoted justification into a line feed, and the check would report the sample
        # that the step just wrote as a difference.
        if (args.output.read_bytes().replace(b"\r\n", b"\n")
                != text.encode("utf-8").replace(b"\r\n", b"\n")):
            logs.warning('engineered_project_selection', f"{args.output} differs from the sample derived "
                                  f"from {args.annotated_csv} ({len(rows):,} rows). Extracting a "
                                  f"newer repos_annotated.7z over an earlier copy is one cause "
                                  f"of such a difference. Run engineered-project-selection "
                                  f"without --check to write the sample again from these labels.")
            return 2
        logs.info('engineered_project_selection', f"{args.output} matches the sample derived from "
                                  f"{args.annotated_csv} ({len(rows):,} rows).")
        return 0

    write(args.output, text)
    logs.info('engineered_project_selection', f"Selected {len(rows):,} repositories labeled "
                       f"{LABEL_COLUMN}={INCLUDED_LABEL} from {args.annotated_csv}.")
    logs.info('engineered_project_selection', f"Wrote the sample to {args.output}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
