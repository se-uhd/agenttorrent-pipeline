#!/usr/bin/env python3
"""The import detection step: write `output/imports.csv`, one row per tool and import of each
chain that a configuration artifact starts.

An import names a file that a tool loads into context automatically with the
configuration artifact that contains the import, such as `@docs/style.md` in a CLAUDE.md. The
imported file can import further files. `heuristics/import_patterns.csv` names the importing
files of each tool, the syntax, what a path resolves against and the depth that the tool reads.
The cloning step copied every file inside the repository that an import reaches to the imported
tree, `data/imported/<owner§repo>/<path>` (`imports.collect_imports`). This step parses the
importing files again with the same functions (`agentcontext/imports.py`). It reads each
importing file in the validated tree and each imported file in the imported tree.

Imports are an extension of the dataset beside the configuration artifacts. They set no tool
flag, add no repository to `output/repos.csv` and change no row of a mechanism CSV. A row with a
missing target stays, with the reason in `target_status`.

Inputs (relative to the working directory, `data/collection/`):
  - `output/repos.csv`, the repositories and their metadata, after the file history step
  - the mechanism CSVs, and `output/planning-documents.csv` where it exists, which give the
    category of an importing file and of a target
  - `data/validated/`, the validated tree
  - `data/imported/`, the imported tree, with the list of the stubs beside a repository's
    directory where the cloning step wrote a stub (`imports.stub_list_path`)
  - `heuristics/import_patterns.csv`

Outputs:
  - `output/imports.csv`

Exit codes: 0 when the CSV was written, 1 when an input is missing or cannot be read.
"""

import csv
import io
import sys
from pathlib import Path

from agentcontext import caches, cli, heuristics, imports, logs, paths, planning_schema, repos
from agentcontext.heuristics import META_COLUMNS
from agentcontext.prune import is_prunable
from agentcontext.steps.detection import no_text, read_artifact, split_lines

# The detection step joins every file that a context file references into `referenced_file`,
# and nothing limits the length of that field. A context file with enough references would
# exceed the default field limit of the csv module and stop the step at every Start.
csv.field_size_limit(sys.maxsize)

COMPONENT = "import_detection"

VALIDATED_DIR = Path("data/validated")
IMPORTED_DIR = Path("data/imported")
OUTPUT_DIR = Path("output")
CSV_NAME = "imports.csv"
PLANNING_DOCUMENT = "planning_document"
# The category of an importing file that the chain imported, and of a target that no CSV lists
# and that only the imported tree contains.
IMPORTED = "imported"
# The archives in which the release publishes a target that the cloning step found. A target is
# in the archive of the collected files where the validated tree has the same bytes at its path,
# and otherwise in the archive of the imported files (`location`).
COLLECTED_ARCHIVE = "repos_data.7z"
IMPORTED_ARCHIVE = "imported_files.7z"

COLUMNS = [*META_COLUMNS, "importer", "importer_category", "root", "tool", "heuristic",
           "import", "line", "target", "target_location", "target_status", "target_category",
           "depth", "target_line_count", "github_link", "dataset_version"]

GITHUB_BASE = "https://github.com"


def github_link(repo_name: str, branch: str, path: str, line: int) -> str:
    """The URL of the import's line on GitHub, or blank without a branch. The branch and the
    path are percent-encoded as the detection step encodes them (`repos.url_path`)."""
    if not branch:
        return ""
    return (f"{GITHUB_BASE}/{repo_name}/blob/{repos.url_path(branch)}/{repos.url_path(path)}"
            f"#L{line}")


def read_rows(path: Path) -> list:
    """The rows of a CSV as dicts."""
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def categories(output_dir: Path) -> dict:
    """`{repo_name: (by_path, skill_folders, planning, other)}`: the mechanism of each path that
    a mechanism CSV other than `other.csv` lists, the folders of the skills, the planning
    documents and the files of `other.csv`."""
    found = {}

    def entry(repo: str) -> tuple:
        return found.setdefault(repo, ({}, set(), set(), set()))

    for mechanism, (filename, column) in heuristics.MECHANISM_CSVS.items():
        for row in read_rows(output_dir / filename):
            by_path, skill_folders, _planning, other = entry(row["repo_name"])
            if mechanism == "other":
                other.add(row[column])
                continue
            by_path.setdefault(row[column], mechanism)
            if mechanism == "skills" and row[column].rsplit("/", 1)[-1].lower() == "skill.md":
                skill_folders.add(row[column].rsplit("/", 1)[0] if "/" in row[column] else "")
    planning_csv = output_dir / planning_schema.CSV_NAME
    if planning_csv.is_file():
        for row in read_rows(planning_csv):
            entry(row["repo_name"])[2].add(row[planning_schema.PATH_COLUMN])
    return found


def category(known: tuple, path: str) -> str:
    """The category of a file: the mechanism of a mechanism CSV that lists it, `skills` for a
    file in a skill's folder, `planning_document` for a planning document, `other` for a file of
    `other.csv`, or blank."""
    by_path, skill_folders, planning, other = known
    if path in by_path:
        return by_path[path]
    if any(folder and path.startswith(folder + "/") for folder in skill_folders):
        return "skills"
    if path in planning:
        return PLANNING_DOCUMENT
    if path in other:
        return "other"
    return ""


def location(trees: imports.CollectedTrees, target: str) -> str:
    """The archive of the release that publishes a found target. It is the archive of the
    collected files where the validated tree has the bytes that the tool read at the path and
    the archive step keeps the file, and otherwise the archive of the imported files."""
    collected = imports.regular_file(trees.validated, target)
    if collected is not None and not is_prunable(target):
        try:
            if collected.read_bytes() == trees.locate(target).read_bytes():
                return COLLECTED_ARCHIVE
        except OSError:
            pass
    return IMPORTED_ARCHIVE


# The columns that, with `repo_name`, identify a row of `imports.csv`.
ROW_KEY = ("tool", "root", "importer", "line", "import")


def repository_rows(repo: dict, rows: list, validated: Path, imported: Path,
                    known: tuple) -> list:
    """The rows of `imports.csv` of one repository: one per tool, chain, importing file, line
    and import (`ROW_KEY`). An import written twice on one line, such as `@AGENTS.md` in a
    sentence and in an example of it, is one import that the tool reads once."""
    trees = imports.CollectedTrees(validated, imported)
    files = paths.tree_files(validated)
    published, seen = [], set()
    for row, root in imports.importing_files(rows, validated, files, trees):
        root_category = category(known, root)
        for record in imports.chain(row, root, trees):
            key = (record.tool, record.root, record.importer, record.line, record.written)
            if key in seen:
                continue
            seen.add(key)
            is_found = record.status == imports.FOUND
            published.append({
                **{column: repo[column] for column in META_COLUMNS},
                "importer": record.importer,
                "importer_category": root_category if record.importer == root else IMPORTED,
                "root": record.root,
                "tool": record.tool,
                "heuristic": record.heuristic,
                "import": record.written,
                "line": record.line,
                "target": record.target,
                "target_location": location(trees, record.target) if is_found else "",
                "target_status": record.status,
                "target_category": (category(known, record.target) or IMPORTED) if is_found else "",
                "depth": record.depth,
                "target_line_count": (target_line_count(trees.locate(record.target))
                                      if is_found else ""),
                "github_link": github_link(repo["repo_name"], repo["branch"], record.importer,
                                           record.line),
                "dataset_version": repo["dataset_version"],
            })
    return published


def target_line_count(path: Path) -> str:
    """The line count of a found target, counted as the detection step counts the lines of a
    configuration artifact (`split_lines`), in the encoding that `read_artifact` finds. The count
    is blank for a file that is not text (`no_text`), such as a file that git-crypt encrypted,
    because the line breaks in encrypted bytes are random."""
    content = None if no_text(path) else read_artifact(path)
    return "" if content is None else str(len(split_lines(content)))


def write_csv(path: Path, rows: list) -> None:
    """Write `imports.csv` atomically, as the detection step writes its CSVs. A kill then leaves
    the previous CSV, not a file without rows."""
    text = io.StringIO()
    writer = csv.DictWriter(text, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    caches.replace_file(path, text.getvalue())


def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Write output/imports.csv, one row per import of the importing files that "
                    "heuristics/import_patterns.csv names, followed through the imported tree "
                    "to its target. Run from data/collection/.")
    parser.parse_args(argv)

    for path, what in ((OUTPUT_DIR / "repos.csv", "written by the detection step"),
                       (VALIDATED_DIR, "the validated tree"),
                       (IMPORTED_DIR, "the imported tree, written by repository cloning")):
        if not path.exists():
            logs.error(COMPONENT, f"{path} does not exist ({what}). Run the steps before import "
                                  f"detection, as `python -m agentcontext run` does.")
            return 1
    try:
        rows = imports.load_import_patterns()
        detected = read_rows(OUTPUT_DIR / "repos.csv")
        known = categories(OUTPUT_DIR)
    except (OSError, ValueError, KeyError) as error:
        logs.error(COMPONENT, f"Could not read heuristics/import_patterns.csv or the output CSVs "
                              f"({error.__class__.__name__} {error}).")
        return 1

    published, skipped = [], []
    for repo in detected:
        dir_name = repos.repo_name_to_dir(repo["repo_name"])
        validated = VALIDATED_DIR / dir_name
        if not validated.is_dir():
            skipped.append(repo["repo_name"])
            continue
        try:
            imports.read_stubs(IMPORTED_DIR / dir_name)
        except (OSError, ValueError) as error:
            logs.error(COMPONENT, f"Could not read the list of stubs of {repo['repo_name']} "
                                  f"({error.__class__.__name__} {error}). Collect the "
                                  f"repository again.")
            return 1
        published += repository_rows(repo, rows, validated, IMPORTED_DIR / dir_name,
                                     known.get(repo["repo_name"], ({}, set(), set(), set())))
    if skipped:
        logs.warning(COMPONENT, f"{len(skipped):,} repositories of output/repos.csv have no "
                                f"directory in {VALIDATED_DIR}, such as {skipped[0]}. Wrote no "
                                f"rows for them.")
    not_copied = sorted({row["repo_name"] for row in published
                         if row["target_status"] == imports.NOT_COPIED})
    if not_copied:
        logs.warning(COMPONENT, f"Found imports of a stub target (target_status not_copied) in "
                                f"{len(not_copied):,} repositories, such as {not_copied[0]}. "
                                f"Repository cloning collected these repositories under other "
                                f"import heuristics. Collect them again to copy the targets.")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    write_csv(OUTPUT_DIR / CSV_NAME, published)
    by_status = {status: sum(1 for row in published if row["target_status"] == status)
                 for status in imports.STATUSES}
    with_imports = len({row["repo_name"] for row in published})
    statuses = ", ".join(f"{count:,} {status}" for status, count in by_status.items())
    logs.info(COMPONENT, f"Wrote {OUTPUT_DIR / CSV_NAME} with {len(published):,} imports in "
                         f"{with_imports:,} repositories ({statuses}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
