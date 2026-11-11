"""What the steps after repository cloning read about the planning documents: the columns of
`planning-documents.csv`, the key under which `file_history.csv` records their history, and the
layouts that are left out of them, `heuristics/not_planning_documents.csv`, with the reasons.

The detection step, the file history step, the checks and the release read these definitions,
and the cloning step does not. They are therefore in a module of their own outside the
collector's code (`reuse.collector_files`), and a new column or a new reason changes no
fingerprint of the collector and collects no repository again. `planning` contains the
heuristics and the matching that the cloning step uses.
"""

from pathlib import Path

from agentcontext import heuristics, planning
from agentcontext.heuristics import HEURISTICS_DIR, META_COLUMNS

NOT_PLANNING_DOCUMENTS_PATH = HEURISTICS_DIR / "not_planning_documents.csv"
NOT_PLANNING_FIELDNAMES = ["framework", "layout", "example", "reason", "source", "doc_url",
                           "doc_verified", "evidence", "notes"]

# Why a layout is left out, the `reason` of `not_planning_documents.csv`.
REASONS = ("out of scope", "outside the repository", "not documented", "not distinctive",
           "configuration", "state", "not a planning document", "not settled")
NEGATIVE_SOURCES = ("doc", "sample")

# The CSV that the detection step writes, the column that names the file, and the key under
# which `file_history.csv` records the history of a planning document, in the column that the
# mechanism CSVs fill with their mechanism.
CSV_NAME = "planning-documents.csv"
PATH_COLUMN = "planning_document"
HISTORY_KEY = "planning_document"

# The columns of `planning-documents.csv` after the metadata block. `language` is read from the
# Markdown files alone (`LANGUAGE_SUFFIXES`), and `line_count` from every file.
DETAIL_COLUMNS = ("framework", "document")
LANGUAGE_SUFFIXES = (".md", ".mdc", ".markdown")


def csv_header() -> list:
    """The header of `planning-documents.csv`, in order."""
    return [*META_COLUMNS, PATH_COLUMN, *DETAIL_COLUMNS, "github_link", "dataset_version",
            "created_at", "#commits", "language", "first_commit_sha", "last_commit_sha",
            "line_count"]


def load_not_planning_documents(path=None) -> list:
    """The layouts that are left out of the planning documents, each with the reason, or raise
    ValueError with every problem.

    `framework` is the framework or the tool that writes the layout, blank for a name that no
    single framework owns. `layout` describes the location, and `example` is a path of the
    layout relative to the repository root, blank where the layout is outside the repository.
    A `doc` row cites the page that decides it, and a `sample` row links files of the layout.
    """
    path = Path(path) if path else NOT_PLANNING_DOCUMENTS_PATH
    rows = planning.read_rows(path, NOT_PLANNING_FIELDNAMES)
    found = []
    for number, row in enumerate(rows, start=2):
        where = f"{path.name}:{number} ({row.get('framework')}/{row.get('layout')})"
        if row["framework"] and not planning.NAME.match(row["framework"]):
            found.append(f"{where} has the framework {row['framework']!r}, which is not a "
                         f"lowercase name with hyphens")
        if not (row["layout"] or "").strip():
            found.append(f"{where} has no layout")
        if row["example"] and planning.path_problems(row["example"].replace("*", "x")):
            found.append(f"{where} has the example {row['example']!r}, which is no repository "
                         f"path")
        if row["reason"] not in REASONS:
            found.append(f"{where} has the reason {row['reason']!r}, which is not one of "
                         f"{REASONS}")
        if row["source"] not in NEGATIVE_SOURCES:
            found.append(f"{where} has the source {row['source']!r}, which is not one of "
                         f"{NEGATIVE_SOURCES}")
        if row["source"] == "doc" and not (row["doc_url"] or "").startswith("https://"):
            found.append(f"{where} is a doc row that cites no https page")
        if row["doc_url"] and not planning.DATE.match(row["doc_verified"] or ""):
            found.append(f"{where} has the doc_verified {row['doc_verified']!r}, which is not an "
                         f"ISO date")
        if not (row["notes"] or "").strip():
            found.append(f"{where} has no note that gives the reason")
        found += heuristics.evidence_problems(row, where)
    if found:
        raise ValueError(f"Found {len(found):,} problem(s) in {path}.\n  " + "\n  ".join(found))
    return rows
