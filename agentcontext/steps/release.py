#!/usr/bin/env python3
"""Build `release/`, the dataset as it is published, from `output/` and the outputs of the
sampling and classification stages.

The step reads the inputs where they are and deletes none of them. `RELEASE_FILES` lists what
`release/` contains, and it is also what the checksum manifest covers, what the README
describes and what the tests check. A file therefore cannot reach the publication without a
description in the README.

The step first checks `output/` against `OUTPUT_INVENTORY`. Every file in that directory is
published, an input to a published file, or a record of how the dataset was built, and the
inventory records which. The run stops at a file that is in neither the inventory nor the
release, because nobody decided whether to publish it. Before anything is written, the step
then compares the inputs with one another so that the release never publishes files that
disagree, and a refusal leaves the previous release as it was, with its manifest. It compares:

  - the detection heuristics, the heuristics of the planning documents, the vendored
    directories and the places of the local state with the checksums that `provenance.json`
    records
  - the commit heuristics with the heuristics that, as the snapshot records, classified the
    commits
  - the archive of collected files with the scanned repositories
  - the imported tree with the targets that ``output/imports.csv`` places in
    ``imported_files.7z``
  - `release/` with `RELEASE_FILES`
  - the detection rows and the labels with the sampling frame, and the rows without a label
    with the exclusions
  - the columns of ``output/repos.csv``, ``output/context-files.csv`` and the commit CSVs with
    the published columns, and each CSV that the release copies for its ``repo_name`` column
  - the ids that ``matched_heuristics`` names in each commit CSV with the commit heuristics
    that the release publishes beside it

`WITHHELD` names each CSV that the release leaves out on purpose, and the README lists them. The
README also states that the release publishes none of the other files of `output/`, the files
that serve to build and check the dataset, and points to `OUTPUT_INVENTORY`.

Inputs:
  - ``output/``: the mechanism CSVs, ``planning-documents.csv``, ``repos.csv``,
    ``all_ai_commits.csv`` and, where the collection wrote it, ``other_ai_commits.csv``, each
    or the ``.7z`` beside it, ``provenance.json``, ``snapshot.json``, and the records of how the
    collection was built, ``ai_commits-repos.csv``, ``commit-reclassification.json``,
    ``unmatched-paths.csv`` and ``deletions.csv``, and ``all_ai_commits-unmatched.csv``, the full
    list of the unmatched attributions, of which the step publishes the lines that name a bot or
    a tool and no person, with the commit logs in ``collect_commits/`` beside ``output/``
  - ``deletion.csv`` beside ``output/``, the deletion log, which the step packs as
    ``deletion.csv.7z``
  - the heuristics, which the step copies into ``release/``: ``config_path_patterns.csv``, the
    detection heuristics, and under ``heuristics/`` ``planning_document_patterns.csv``, the
    heuristics of the planning documents, ``ai_commit_patterns.csv`` and
    ``other_ai_commit_patterns.csv``, the commit heuristics, ``import_patterns.csv`` and
    ``not_imports.csv``, the import heuristics and their negatives, ``not_configuration.csv``,
    ``not_ai_commits.csv`` and ``not_planning_documents.csv``, the other negatives,
    ``vendored_dirs.txt``, ``local_state_paths.csv``, and ``doc_hashes.json`` and
    ``doc_quotes.json``, the record of the documentation watch
  - ``../sampling/ghs_filtered.json``, the sampling frame, and
    ``ghs_retrieval_report.json`` beside it, the report of the GHS download. The report states
    whether the sampling frame comes from a retrieval that the download fell back to or took
    with ``--accept-smaller``
  - ``../classification/repos_annotated.csv``, the classification labels,
    ``repos_annotated.json`` beside it, which names the models that made them and lists the
    repositories that the classifying model labeled at its higher temperature, those that the
    second model labeled, those that the second model left without a decision too and those
    with a label that a backup provider served, which the README counts, and
    ``repos_excluded.csv`` beside it, the repositories in the sampling frame without a
    published label, with the reason
  - ``release/repos_data.7z``, which the collected_files_archive step writes
  - ``data/imported/``, the imported tree, from which the step packs ``imported_files.7z``
    with the targets that ``output/imports.csv`` assigns to that archive

Outputs:
  - ``release/``, every entry of ``RELEASE_FILES``

Exit codes: 0 when the release was written, 1 when it could not be.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from agentcontext import CLASSIFICATION_DIR, SAMPLING_DIR, SEVEN_ZIP, STAGE_DIR
from agentcontext import cli

from agentcontext import commits
from agentcontext import heuristics
from agentcontext import logs
from agentcontext import planning, planning_schema
from agentcontext import repos
from agentcontext.checks import unmatched
from agentcontext.steps.collected_files_archive import ARCHIVE_ROOT

DEFAULT_OUTPUT_DIR = STAGE_DIR / "output"
DEFAULT_RELEASE_DIR = STAGE_DIR / "release"
DEFAULT_GHS_JSON = SAMPLING_DIR / "ghs_filtered.json"
RETRIEVAL_REPORT_NAME = "ghs_retrieval_report.json"
DEFAULT_RETRIEVAL_REPORT = SAMPLING_DIR / RETRIEVAL_REPORT_NAME
DEFAULT_ANNOTATED_CSV = CLASSIFICATION_DIR / "repos_annotated.csv"

# A commit message is one CSV field and some of them are long.
csv.field_size_limit(sys.maxsize)


# ---------------------------------------------------------------------------
# The contents of `release/`
# ---------------------------------------------------------------------------

KINDS = ("build", "copy", "compress", 'collected_files_archive', "generated")
SECTIONS = ("CSVs", "archives", "provenance")


@dataclass(frozen=True)
class ReleaseFile:
    """One file of the publication, with how it is produced and how the README describes it.

    `kind` is how the file is produced, `source` what it is produced from, `section` the part
    of the README that describes it, and `description` the README's text for it. The fields
    form one record rather than four lists so that what is built, what is copied and what the
    README names cannot differ.
    """

    name: str
    kind: str
    source: str
    section: str
    description: str

    def __post_init__(self):
        if self.kind not in KINDS:
            raise ValueError(f"The release file {self.name} has the kind {self.kind!r}, which is "
                             f"not one of {KINDS}")
        if self.section not in SECTIONS:
            raise ValueError(f"The release file {self.name} has the section {self.section!r}, "
                             f"which is not one of {SECTIONS}")


# The order is the build order and, within a section, the order of the README. The
# collected_files_archive step writes `repos_data.7z`. The archive is named here so that the
# manifest covers it and the README describes it, and this step only checks that it is present.
RELEASE_FILES = (
    ReleaseFile("repos.csv", "build", "output/repos.csv + the sampling frame + the labels",
                "CSVs",
                "Sampling frame with GitHub metadata, classification, and the detection flags "
                "of tools, context files and mechanisms"),
    ReleaseFile("commits.csv", "build", "output/all_ai_commits.csv", "CSVs",
                "Commits that credit one of the six tools, in the repositories with detected "
                "configuration"),
    ReleaseFile("other_commits.csv", "build", "output/other_ai_commits.csv", "CSVs",
                "Commits that credit tools outside the six that this dataset "
                "studies (e.g., Devin, Jules and aider), Gemini without naming which Google "
                "tool, or AI without naming a tool, in the columns of `commits.csv`"),
    ReleaseFile("context_files.csv", "build", "output/context-files.csv", "CSVs",
                "Context files (e.g., `CLAUDE.md`, `AGENTS.md`, `.cursorrules`)"),
    ReleaseFile("settings.csv", "copy", "output/settings.csv", "CSVs",
                "Settings files (e.g., `.claude/settings.json`)"),
    ReleaseFile("skills.csv", "copy", "output/skills.csv", "CSVs",
                "Skill definitions, including the Claude command files that Claude Code "
                "merged into skills (e.g., `.claude/skills/*/SKILL.md`, "
                "`.claude/commands/*.md`)"),
    ReleaseFile("subagents.csv", "copy", "output/subagents.csv", "CSVs",
                "Subagent definitions (e.g., `.claude/agents/*.md`, `.codex/agents/*.toml`)"),
    ReleaseFile("commands.csv", "copy", "output/commands.csv", "CSVs",
                "Cursor, Gemini and OpenCode command files, Antigravity workflows, Copilot prompt files and Copilot CLI "
                "plugin commands (e.g., `.cursor/commands/*.md`, `.opencode/commands/*.md`, "
                "`.github/prompts/*.prompt.md`)"),
    ReleaseFile("hooks.csv", "copy", "output/hooks.csv", "CSVs",
                "Hook configurations (e.g., `.claude/settings.json`, `.cursor/hooks.json`)"),
    ReleaseFile("rules.csv", "copy", "output/rules.csv", "CSVs",
                "Rule files (e.g., `.cursor/rules/*.mdc`, `.claude/rules/**/*.md`)"),
    ReleaseFile("mcp.csv", "copy", "output/mcp.csv", "CSVs",
                "MCP (Model Context Protocol) configurations"),
    ReleaseFile("plugins.csv", "copy", "output/plugins.csv", "CSVs",
                "Plugin and extension manifests that a repository ships "
                "(e.g., `.claude-plugin/plugin.json`, `gemini-extension.json`)"),
    ReleaseFile("lsp.csv", "copy", "output/lsp.csv", "CSVs",
                "LSP (Language Server Protocol) server configurations "
                "(e.g., `.github/lsp.json`, a Claude Code plugin's `.lsp.json`)"),
    ReleaseFile("imports.csv", "copy", "output/imports.csv", "CSVs",
                "Imports: the files that a tool loads into context with a configuration "
                "artifact because the artifact names them (e.g., `@docs/style.md` in a "
                "`CLAUDE.md`), one row per tool and import in each chain of imports that starts "
                "at a configuration artifact"),
    ReleaseFile(planning_schema.CSV_NAME, "copy", f"output/{planning_schema.CSV_NAME}", "CSVs",
                "Planning documents, which a spec-driven development framework or a planning "
                "feature of one of the six tools writes into the repository (e.g., "
                "`specs/001-*/spec.md` of Spec Kit, `.cursor/plans/*.plan.md`). They are not "
                "configuration artifacts"),
    ReleaseFile("repos.csv.7z", "compress", "repos.csv", "archives", "`repos.csv`"),
    ReleaseFile("commits.csv.7z", "compress", "commits.csv", "archives", "`commits.csv`"),
    ReleaseFile("repos_data.7z", 'collected_files_archive', "the collected_files_archive step", "archives",
                "`repos/`, the collected configuration artifacts and planning documents"),
    ReleaseFile("imported_files.7z", "build", "data/imported + imports.csv", "archives",
                "`imported/`, the imported files that `repos_data.7z` does not contain"),
    ReleaseFile("deletion.csv.7z", "build", "deletion.csv", "archives",
                "`deletion.csv`, the deletion log of the collected file cleanup"),
    ReleaseFile("provenance.json", "copy", "output/provenance.json", "provenance",
                "The dataset version that the detection step wrote into every row "
                "(`dataset_version`) and the "
                "files that the detection ran with: the SHA-256 and the number of heuristics of "
                "`config_path_patterns.csv` and of `planning_document_patterns.csv`, and the "
                "SHA-256 of `vendored_dirs.txt` and of `local_state_paths.csv`"),
    ReleaseFile("snapshot.json", "copy", "output/snapshot.json", "provenance",
                "The collection: the first and last `scanned_at` of its rows "
                "(`scanned_at_window`), its counts, the repositories that the evidence limits of "
                "the commit reclassification name, and what each section was derived from. A "
                "row that the collection took over from an earlier collection keeps the "
                "`scanned_at` of that collection, and the window spans every collection that the "
                "rows come from. `sampling.json` gives the sampling window"),
    ReleaseFile("sampling.json", "generated", "the sampling frame's `_metadata`", "provenance",
                "Where the sampling frame came from: the GHS retrieval, its sampling window "
                "and query, when GitHub was queried for its repositories, the filter's "
                "parameters and what each of its steps removed"),
    ReleaseFile("ai_commits-repos.csv", "copy", "output/ai_commits-repos.csv", "provenance",
                "For each repository of the collection, whether the cloning step read its commit "
                "history and how many commits it wrote into each commit CSV"),
    ReleaseFile("commit-reclassification.json", "copy", "output/commit-reclassification.json",
                "provenance",
                "The outcome of the commit reclassification for every repository of the "
                "collection, with the count of each outcome"),
    ReleaseFile("unmatched-paths.csv", "copy", "output/unmatched-paths.csv", "provenance",
                "The ranked list of the path globs of the collected files that no heuristic "
                "explains"),
    ReleaseFile("unmatched-attributions.csv", "build", "output/all_ai_commits-unmatched.csv",
                "provenance",
                "The attributions in commits that no commit heuristic matched, limited to the "
                "lines that name a bot or a tool and no person"),
    ReleaseFile("deletions.csv", "copy", "output/deletions.csv", "provenance",
                "The report of what the collected file cleanup deleted: the number of rows of its "
                "deletion log, `deletion.csv` in `deletion.csv.7z`, per reason and repository, "
                "the path globs of the files in the repositories that it removed, and what the "
                "checks of the log "
                "found"),
    ReleaseFile("config_path_patterns.csv", "copy", "config_path_patterns.csv", "provenance",
                "The detection heuristics: every configuration path, the tool and mechanism "
                "that it belongs to, and the vendor page that defines it"),
    ReleaseFile("ai_commit_patterns.csv", "copy", "heuristics/ai_commit_patterns.csv",
                "provenance",
                "The heuristics that attribute a commit to one of the six tools, each with its "
                "id, its attribution and its source. `matched_heuristics` in `commits.csv` "
                "names them by id"),
    ReleaseFile("other_ai_commit_patterns.csv", "copy", "heuristics/other_ai_commit_patterns.csv",
                "provenance",
                "The same for the tools outside the six. `matched_heuristics` in "
                "`other_commits.csv` names them by id"),
    ReleaseFile("import_patterns.csv", "copy", "heuristics/import_patterns.csv", "provenance",
                "The import heuristics: the files in which each tool reads imports, the syntax, "
                "what a path resolves against and how deep the tool reads, each with the page "
                "or the post that states it. `heuristic` in `imports.csv` names them by id"),
    ReleaseFile("not_imports.csv", "copy", "heuristics/not_imports.csv", "provenance",
                "What the tools do not read as an import, such as a Markdown link in a "
                "Copilot instructions file, each with the reason"),
    ReleaseFile("not_configuration.csv", "copy", "heuristics/not_configuration.csv", "provenance",
                "What does not count as a configuration artifact among the collected files, "
                "such as a folder in a tool's directory that the tool does not read, each with "
                "the reason"),
    ReleaseFile("not_ai_commits.csv", "copy", "heuristics/not_ai_commits.csv", "provenance",
                "What does not count as a credit to a coding agent among the attributions of "
                "the commits, such as the account of Dependabot, each with the reason"),
    ReleaseFile("planning_document_patterns.csv", "copy",
                "heuristics/planning_document_patterns.csv", "provenance",
                "The heuristics of the planning documents: every layout, the framework or tool "
                "that writes it, the document that the source names, and the page that states "
                "the location"),
    ReleaseFile("not_planning_documents.csv", "copy", "heuristics/not_planning_documents.csv",
                "provenance",
                "What does not count as a planning document, such as a layout of a tool outside "
                "the six, each with the reason"),
    ReleaseFile("vendored_dirs.txt", "copy", "heuristics/vendored_dirs.txt", "provenance",
                "The names of the build output and vendored directories that the collection "
                "and the detection skip"),
    ReleaseFile("local_state_paths.csv", "copy", "heuristics/local_state_paths.csv",
                "provenance",
                "The places inside the tools' directories where a tool keeps its session logs, "
                "histories, caches, locks and logins, which the collection and the archives "
                "leave out, each with the page or the code that names the place"),
    ReleaseFile("doc_hashes.json", "copy", "heuristics/doc_hashes.json", "provenance",
                "The record of the documentation watch: for each vendor page that the pipeline "
                "cites, the SHA-256 of its visible text as the watch last recorded it"),
    ReleaseFile("doc_quotes.json", "copy", "heuristics/doc_quotes.json", "provenance",
                "The record of the quotations that the documentation watch checks: for each "
                "vendor page, the SHA-256 of the text in which the watch found them and a digest "
                "of each quotation"),
    ReleaseFile("CITATION.cff", "generated", "CITATION_AUTHORS", "provenance",
                "How to cite the dataset"),
    ReleaseFile("0_README.md", "generated", "this script", "provenance",
                "This file"),
    ReleaseFile("checksums.sha256", "generated", "every file above", "provenance",
                "SHA-256 of every other file, in `sha256sum -c` format"),
)

RELEASE_NAMES = tuple(f.name for f in RELEASE_FILES)

# Another step writes this archive, and this step only checks it.
ARCHIVE_NAME = "repos_data.7z"
# The archive of the imported files that `repos_data.7z` does not contain. This step packs it
# from the imported tree, with its entries under `imported/` as those of `repos_data.7z` are
# under `repos/`.
IMPORTED_ARCHIVE_NAME = "imported_files.7z"
IMPORTED_ARCHIVE_ROOT = "imported"
DEFAULT_IMPORTED_DIR = STAGE_DIR / "data" / "imported"
# The deletion log, which the collected file cleanup writes beside `output/`, and the archive of
# it that the release publishes.
DELETION_LOG_NAME = "deletion.csv"
DELETION_ARCHIVE_NAME = "deletion.csv.7z"

# The entries that a file manager writes into a directory that it shows: Finder's `.DS_Store`,
# which a Mac writes over SMB, and `@eaDir`, in which Synology DSM keeps the extended attributes
# and thumbnails of the files beside it. The checks of `output/` and of `release/` accept them.
# Otherwise a person who opened either directory on the NAS would make every later Start fail
# in this step.
FILE_MANAGER_ENTRIES = frozenset({".DS_Store", "@eaDir"})

# The full list of the unmatched attributions, and the file of the release with the lines that
# name a bot or a tool and no person.
UNMATCHED_SOURCE = "all_ai_commits-unmatched.csv"
UNMATCHED_RELEASE_NAME = "unmatched-attributions.csv"

# The files of `output/` that the release leaves out on purpose, each with the reason that the
# README states, so that no file is left out of the publication without a reason.
WITHHELD = {
    "other.csv": "the unclassified files: the collected files that no mechanism covers, "
                 "because no detection heuristic matches them or because the top-level "
                 "`exclude` of an Antigravity registration file leaves them out of a default "
                 "place. The archive "
                 "of collected files, `repos_data.7z`, contains them, except for the files that "
                 "the archive leaves out, such as session transcripts and logs. Beside the "
                 "mechanism CSVs, readers "
                 "would take a CSV of these files for a mechanism.",
    "excluded.csv": "repositories in the validated list with no directory when the detection "
                    "step ran. It is a record of the run, not of the sample.",
    "unmatched-commits.csv": "the ranked list of the attributions that no commit heuristic "
                             "explains, and the full list of the unmatched attributions that it "
                             "ranks. Most of them name people, such as the co-authors of commits "
                             "that no heuristic matched, and some give their addresses. The "
                             "commit CSVs of this release contain only the commits that a "
                             "heuristic matched, and most of these names and addresses are in no "
                             "file of the release. `unmatched-attributions.csv` publishes the "
                             "lines that name a bot or a tool and no person.",
}


# ---------------------------------------------------------------------------
# The inventory of `output/`
# ---------------------------------------------------------------------------

# Each name that `output/` may contain, with what the file is. A file in `output/` that is not
# here stops the run, because nobody decided to publish or withhold it. OUTPUT_REQUIRED names
# the files without which the release cannot be built.
OUTPUT_INVENTORY = {
    "commit-reclassification.json": "the commit reclassification's record of which repositories "
                                    "it reclassified and which inputs were missing, published "
                                    "unchanged",
    "repos.csv": "the detection CSV, joined into release/repos.csv",
    "excluded.csv": "withheld, see WITHHELD",
    "all_ai_commits.csv": "the commit CSV, normalized into release/commits.csv",
    "other_ai_commits.csv": "the commits of the tools outside the six, normalized "
                            "into release/other_commits.csv",
    "all_ai_commits.csv.7z": "the tracked form of the commit CSV, extracted when the "
                             "CSV beside it is absent",
    "all_ai_commits-unmatched.csv": "the cloning step's record of what looked like an attribution "
                                    "in a commit that no heuristic matched, which `unmatched "
                                    "commits` reads. It names people and is withheld, see "
                                    "unmatched-commits.csv in WITHHELD. Its lines that name a bot "
                                    "or a tool and no person are published as "
                                    "release/unmatched-attributions.csv",
    "all_ai_commits-mentions.csv": "the cloning step's record of the lines that name one of the "
                                   "six tools in a commit that no heuristic matched, grouped by "
                                   "phrasing. It is a reading list for the commit heuristics and "
                                   "is not published",
    "all_ai_commits-validated.csv": "the commit attribution validation's per-commit classification "
                                    "of which detection scope matched",
    "other_ai_commits-validated.csv": "the same classification of the commits of the tools "
                                      "outside the six",
    "ai_commits-repos.csv": "the cloning step's record per repository of whether its commit "
                            "history was read, how many commits, and how many matched, published "
                            "unchanged",
    "consistency-report.csv": "the output cross-check's count of distinct repositories per CSV",
    "unmatched-commits.csv": "the unmatched_commits_report step's ranked list of the attributions "
                             "that no commit heuristic explains, withheld, see WITHHELD",
    "unmatched-paths.csv": "the unmatched_paths_report step's list of the path globs that no "
                           "heuristic explains, in other.csv and in the repositories that the "
                           "cleanup dropped, published unchanged",
    "deletions.csv": "the deletions step's counts of the cleanup's deletion log per reason and "
                     "repository, the path globs under the repositories that the cleanup "
                     "removed, and the results of its three assertions, published unchanged",
    "consistency-mismatch.csv": "the repositories that the output cross-check found "
                                "inconsistent, written only when it finds one",
    "imports.csv": "the import detection step's CSV, published as release/imports.csv",
    "baseline.json": "the regression manifest that `regressions check` compares with",
    "snapshot.json": "published provenance",
    "snapshot.json.lock": "the lock that every snapshot writer takes, never read",
    "provenance.json": "published provenance",
    "snapshot-validation.csv": "snapshot validation's per-repository result",
    "repo-sizes.csv": "`measure sizes`' record of the sizes of the repositories in the sample",
    "identity-labels.csv": "the commits of the commit CSV of AgentConfigs 1.0.0 that only an author "
                           "or committer identity credited and that name no person, with the "
                           "identity that GitHub's API returned for each and the label of that "
                           "identity",
    "identity-readings.csv": "each distinct identity of that reading with its label, its counts "
                             "and the reading behind it, the identity empty for a person",
    "identity-pilot.csv": "a seeded sample of 30 of those commits, read from GitHub, with the "
                          "identity heuristics that each matches, a committer who is a person "
                          "empty",
    planning_schema.CSV_NAME: "the planning documents, published unchanged",
}
for _table in heuristics.csv_filenames():
    OUTPUT_INVENTORY.setdefault(_table, "a mechanism CSV")

# The entries that a pattern matches rather than a name: the compressed commit CSVs with their
# numbered volumes, and the unmatched attributions named by the day on which their collection
# started. A checkout's `output/` contains the tracked archive of the unmatched attributions,
# and `unmatched reduce` writes the archive of a run's own collection.
OUTPUT_INVENTORY_DATED = {
    re.compile(r"(?:all|other)_ai_commits\.csv\.7z(?:\.\d{3})?"):
        "compressed commit CSV, optionally split into numbered volumes",
    re.compile(r"all_ai_commits-unmatched-\d{4}-\d{2}-\d{2}\.csv\.7z"):
        "the unmatched attributions of the collection named by the date that name a bot or a tool and no "
        "person, with four columns and tracked, which `unmatched reduce` writes and which the rows of "
        "not_ai_commits.csv are compared with. The release builds release/unmatched-attributions.csv "
        "from the full list by the same rule",
    re.compile(r"all_ai_commits-unmatched-\d{4}-\d{2}-\d{2}\.csv"):
        "the same, extracted beside its archive and ignored by git",
}


def inventoried(entry: str) -> bool:
    """Whether `output/` may contain this entry, because `OUTPUT_INVENTORY` names it or a
    pattern of `OUTPUT_INVENTORY_DATED` matches it."""
    return (entry in OUTPUT_INVENTORY
            or any(form.fullmatch(entry) for form in OUTPUT_INVENTORY_DATED))


OUTPUT_REQUIRED = ("repos.csv", "excluded.csv", "provenance.json", "snapshot.json",
                   "imports.csv", *heuristics.csv_filenames(), planning_schema.CSV_NAME,
                   "ai_commits-repos.csv", "commit-reclassification.json", "unmatched-paths.csv",
                   "deletions.csv", "all_ai_commits-unmatched.csv")
# Only a run with the commit reclassification writes its record, and a run without it stops here.
OUTPUT_REMEDIES = {"commit-reclassification.json": " Run the pipeline with "
                                                   "--with-commit-reclassification, which writes "
                                                   "it."}


# ---------------------------------------------------------------------------
# release/repos.csv
# ---------------------------------------------------------------------------

# The GHS fields with one value per repository. `languages` and `topics` hold several values
# each, and `build_repos` serializes them as JSON.
GHS_SCALAR_FIELDS = (
    "id", "name", "commits", "branches", "releases", "forks",
    "mainLanguage", "defaultBranch", "license", "homepage",
    "watchers", "stargazers", "contributors", "size",
    "createdAt",
    "totalIssues", "openIssues", "totalPullRequests", "openPullRequests",
    "blankLines", "codeLines", "commentLines",
)

# The sampling frame also has `pushedAt`, `updatedAt`, `lastCommit` and `lastCommitSHA`, and
# the release leaves them out. They describe the repository on the day on which the
# sampling frame was downloaded, which is neither the day of the scan nor today, and a reader
# cannot tell that from the value.
REPOS_COLUMN_ORDER = (
    "repo_name", "id", "default_branch", "scanned_branch", "scanned_at",
    "head_sha", "head_commit_date", "dataset_version",
    "engineered_project", "github_link", "justification",
    "claude", "gemini", "codex", "copilot", "cursor", "opencode",
    "claude.md", "gemini.md", "agents.md", "copilot_instructions", "cursorrules",
    "context_file", "settings", "skills", "subagents", "commands",
    "hooks", "rules", "mcp", "plugins", "lsp", "no_specific_tool",
    "mainLanguage", "license", "homepage",
    "createdAt", "size",
    "commits", "branches", "releases", "forks",
    "watchers", "stargazers", "contributors",
    "totalIssues", "openIssues", "totalPullRequests", "openPullRequests",
    "codeLines", "blankLines", "commentLines",
    "languages", "topics",
)

# The two columns of `output/repos.csv` that are published under another name. `branch` is
# the branch that was scanned, and the sampling frame has a `defaultBranch` of its own.
# Publishing both as `branch` would put two meanings in one name.
DETECTED_RENAMES = {"branch": "scanned_branch", "copilot-instructions": "copilot_instructions"}

# Columns of `output/repos.csv` that the published CSV drops on purpose.
DISCARDED_DETECTED_COLUMNS = ("other",)

CONTEXT_FILES_COLUMN_ORDER = (
    "repo_name", "context_file", "tool", "scanned_at", "branch", "head_sha",
    "head_commit_date", "dataset_version", "github_link", "created_at",
    "#commits", "language", "line_count", "is_reference", "referenced_file",
    "first_commit_sha", "last_commit_sha",
    "first_commit_ai_created", "first_commit_ai_tool", "#ai_commits", "ai_commit_tools",
)

# `github_id` is GitHub's id of the repository, which the classification copies from the
# sampling frame. It is blank in the labels for a name that GitHub did not find.
ANNOTATED_COLUMNS = ("repo_name", "github_id", "default_branch", "engineered_project",
                     "justification")


# ---------------------------------------------------------------------------
# Citation
# ---------------------------------------------------------------------------

# The authors of this release, in the order of its Zenodo deposit and of the MSR 2027 data
# showcase paper that describes it. `CITATION.cff` and the Citation section of the README name
# them. The AIware 2026 dataset paper, which described AgentConfigs 1.0.0, lists these authors
# without Vahram Nersesyan and in another order, and the README cites it with
# `AIWARE_DATASET_PAPER_AUTHORS`. Neither `CITATION.cff` nor the README has a DOI of the release,
# because Zenodo assigns the DOI when the deposit is made, and a DOI written here would be one
# that nobody assigned.
CITATION_TITLE = "AgentTorrent: A Dataset of Context and Harness Engineering Artifacts on GitHub"
CITATION_AUTHORS = (
    ("Sebastian", "Baltes"),
    ("Seyedmoein", "Mohsenimofidi"),
    ("Levi", "Böhme"),
    ("Vahram", "Nersesyan"),
    ("Muhammad Auwal", "Abubakar"),
    ("Jai Lal", "Lulla"),
    ("Matthias", "Galster"),
    ("Christoph", "Treude"),
)
AIWARE_DATASET_PAPER_AUTHORS = ("Matthias Galster, Seyedmoein Mohsenimofidi, Levi Böhme, Jai Lal Lulla, "
                                "Muhammad Auwal Abubakar, Christoph Treude, Sebastian Baltes")
CITATION_LICENSE = "CC-BY-4.0"
# The repository of the pipeline. The README points to its documents for how a release is built,
# because the AIware 2026 dataset paper describes AgentConfigs 1.0.0, which an earlier version of
# the pipeline built.
PIPELINE_REPOSITORY = "https://github.com/se-uhd/agenttorrent-pipeline"


# ---------------------------------------------------------------------------
# Reading and writing
# ---------------------------------------------------------------------------

def resolve(source: str, output_dir: Path, config_path_patterns: Path) -> Path:
    """Where a copied file is read from. `source` is the path that the file has in the
    repository."""
    if source == "config_path_patterns.csv":
        return config_path_patterns
    if source.startswith("heuristics/"):
        return heuristics.HEURISTICS_DIR / source[len("heuristics/"):]
    if source.startswith("output/"):
        return output_dir / source[len("output/"):]
    raise ValueError(f"{source!r} is neither config_path_patterns.csv nor a path under "
                     f"heuristics/ or output/")


def read_json(path: Path):
    """The JSON value in `path`. A file that is not valid JSON raises ValueError with its path,
    because the text of `json.JSONDecodeError` names only the line and the column."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise ValueError(f"{path} is not valid JSON ({error})") from error


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    """One CSV as (header, rows), every value a string.

    The function reads through the csv module rather than pandas, because pandas reads `n/a`
    as a missing value, and `subagents.csv` writes `n/a` in `memory` for tools without that
    field.
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


def write_csv(path: Path, columns, rows) -> None:
    """Write one CSV with LF line endings, replacing the file in one step.

    Every CSV that the pipeline writes ends its lines with LF, the mechanism CSVs of `output/`
    that the release copies unchanged among them. `checksums.sha256` covers the bytes as
    written.
    """
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".",
                                         suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(columns), lineterminator="\n",
                                    extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({c: row.get(c, "") for c in columns})
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def cell(value) -> str:
    """One JSON value as the string that a CSV cell contains. `None` is blank, not the word
    None."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


# ---------------------------------------------------------------------------
# The CSVs that are built rather than copied
# ---------------------------------------------------------------------------

def classification_record(annotated_csv: Path) -> dict:
    """The classification's record in `repos_annotated.json` beside `annotated_csv`, which
    names the models that labeled the repositories and describes the run for the README.
    Because a run of the classification stage can label with another model, the README takes
    the model from the record rather than from a constant here. A record that is missing,
    names no model or has no description is refused."""
    path = annotated_csv.with_suffix(".json")
    try:
        record = read_json(path)
    except FileNotFoundError:
        raise ValueError(f"{path} does not exist, and the README needs it to name the model "
                         f"that labeled the sample. Run the classification to write it beside "
                         f"{annotated_csv.name}, or extract it from the tracked archive "
                         f"repos_annotated.7z.") from None
    if not (isinstance(record, dict) and record.get("models") and record.get("description")):
        raise ValueError(f"{path} names no model or has no description. The README needs both "
                         f"to name the model that labeled the sample.")
    return record


# The file in which the classification lists, beside the labels, each repository in the
# sampling frame without a published label, with the reason: a repository that it did not label,
# or a copy that it labeled `true` and left out of the sample.
EXCLUDED_NAME = "repos_excluded.csv"
EXCLUDED_COLUMNS = ("repo_name", "reason")
# What each criterion that a published justification names means, as the classification prompt
# defines it (`2_classification/prompts/classification-prompt.md`). The classification's verdict
# sentence uses these keys to name the criteria that decided the label (`published_justification`
# of `2_classification/1_classify_repos.py`), and `test_classify_repos` checks that the keys here
# are the criteria in `CRITERIA` of that script.
CRITERION_MEANINGS = {
    "not_english": "the README is not mainly in English",
    "inactive": "the repository is described as no longer developed or maintained, or a "
                "successor is recommended to all of its new users",
    "moved": "the repository is described as moved to another repository or site, where its "
             "development continues",
    "mirror": "the repository is described as a mirror or a read-only copy of another one, or "
              "GitHub marks it as a mirror, while its README invites no pull requests in it and "
              "no pull request other than one that a GitHub App opened was merged in it since "
              "the sampling window began",
    "collection": "the repository is a collection of material, such as snippets, dotfiles or "
                  "package definitions, rather than software developed in it",
    "example_or_template": "the repository is an example, a sample, a demo, a template, a starter "
                           "kit or the unchanged output of a project generator",
    "website": "the repository's primary purpose is hosting a website that presents information "
               "to its visitors",
    "documentation_or_teaching": "the repository is teaching material or contains only "
                                 "documentation",
    "research_artifact": "the repository's main content is a dataset, or it is research code "
                         "that is not both maintained and used by others",
    "localization": "the repository is a localization of other software",
    "configuration_only": "the repository contains only infrastructure or deployment "
                          "configuration, or instructions to run other software",
    "no_source_code": "the repository does not contain the source code of the software that it "
                      "is about, or the source code of its main part is not public",
    "work_in_progress": "the software is unfinished and nobody uses it",
    "personal_or_single_script": "the software is a toy project, or it is software that its "
                                 "author wrote for their own use or a single script and is not "
                                 "both maintained and used by others",
    "fork": "the repository is described as a fork of another project or has the README of "
            "another project, and the fork heuristic finds that it has not become a project of "
            "its own apart "
            "from its upstream",
    "no_purpose": "the description and the README state no purpose of the software",
}
# What each criterion that the classification no longer has meant, for the labels made before its
# removal, whose verdicts still name it. The README lists such a criterion only when a published
# verdict names it. `2_classification/classify-repos-spec.md` gives the reason of each removal.
REMOVED_CRITERION_MEANINGS = {
    "closed_to_contributions": "the repository is described as accepting neither pull requests "
                               "nor issues from the public. The classification removed this "
                               "criterion on 2026-10-31, and only labels made before then name "
                               "it",
}
# The verdict that `published_justification` of `2_classification/1_classify_repos.py` appends
# to the model's justification when criteria decided the label, such as "The classification
# labels it false, because the criteria inactive and mirror apply.", with the keys of the
# criteria in `keys`.
VERDICT = re.compile(r"The classification labels it (?:false|unclear), because the "
                     r"criteri(?:on|a) (?P<keys>[a-z_]+(?:(?:, | and )[a-z_]+)*) "
                     r"(?:applies|apply|is unclear|are unclear)\.")


def verdict_criteria(justification: str) -> list[str]:
    """The keys of the criteria that the verdict of a published justification names, in its
    order, and none for a verdict that names no criterion. The verdict follows the model's text,
    and the last match is the verdict."""
    found = list(VERDICT.finditer(justification))
    return re.split(r", | and ", found[-1]["keys"]) if found else []


def named_criteria(rows: list[dict]) -> dict[str, list[str]]:
    """{criterion: the repositories with a verdict that names it} over the published rows of
    `repos.csv`, the repositories in the order of the rows."""
    named = {}
    for row in rows:
        for key in verdict_criteria(row.get("justification", "")):
            named.setdefault(key, []).append(row["repo_name"])
    return named

# What each reason of `repos_excluded.csv` means, as `0_README.md` states it after the number of
# repositories. The classification assigns the first four when it fetches and checks the README
# of a repository, before any model reads that README (`2_classification/readmes.py`), assigns
# `undecided` to a repository that it left without a decision after its retry rounds and, where
# the second model applied, after the second model, and assigns `copy` to a repository labeled
# `true` at the head commit of another one labeled `true` that the sample keeps instead
# (`first_of_copies` of `2_classification/1_classify_repos.py`).
EXCLUSION_REASONS = {
    "readme_non_english": "with a README that the language check found not to be in English",
    "no_readme_found": "without a README",
    "empty_readme": "with an empty README",
    "not_found_or_no_access": "that GitHub did not find or did not give access to",
    "undecided": "for which the classification reached no decision after its retry rounds and, "
                 "where the second model applied, after the second model",
    "copy": "that the classification labeled `true` and left out of the sample, because the "
            "sample keeps another repository at the same head commit",
}


def exclusion_reasons(annotated_csv: Path) -> dict[str, str]:
    """{repository: reason} from `repos_excluded.csv` beside `annotated_csv`, the repositories
    in the sampling frame without a published label: those that the classification did not
    label, and the copies that it labeled `true` and left out of the sample.

    The README states by reason why the repositories in the sampling frame without a label have
    none. A missing file is refused before anything is written, because the README could then
    not say why they have none."""
    path = annotated_csv.with_name(EXCLUDED_NAME)
    try:
        header, rows = read_csv(path)
    except FileNotFoundError:
        raise ValueError(f"{path} does not exist, and the README needs it to state for each "
                         f"reason how many repositories in the sampling frame have no label. "
                         f"Run the classification to write it beside {annotated_csv.name}, or "
                         f"extract it from the tracked archive repos_annotated.7z.") from None
    missing = [column for column in EXCLUDED_COLUMNS if column not in header]
    if missing:
        raise ValueError(f"{path} lacks the column(s) {missing}")
    return {row["repo_name"]: row["reason"] for row in rows}


def unlabeled_by_reason(rows: list[dict], reasons: dict[str, str]) -> dict[str, int]:
    """The number of published rows without a label for each reason of `exclusion_reasons`,
    the largest first.

    A row without a label and without a reason is refused, because the README would count it
    without saying why it has no label. Labels and exclusions of different runs of the
    classification give such rows."""
    unlabeled = [row["repo_name"] for row in rows if not row["engineered_project"]]
    unexplained = [name for name in unlabeled if name not in reasons]
    if unexplained:
        raise ValueError(f"{len(unexplained):,} "
                         f"{by_number(len(unexplained), 'repository', 'repositories')} in the "
                         f"sampling frame {by_number(len(unexplained), 'has', 'have')} neither a "
                         f"label nor a row in {EXCLUDED_NAME} ({named(unexplained)}). Publish "
                         f"the labels and the exclusions of one run of the classification.")
    counts: dict[str, int] = {}
    for name in unlabeled:
        counts[reasons[name]] = counts.get(reasons[name], 0) + 1
    return dict(sorted(sorted(counts.items()), key=lambda item: item[1], reverse=True))


def unlabeled_sentence(unlabeled: dict[str, int]) -> str:
    """The README's sentence on the repositories in the sampling frame without a published
    label, by reason, or "" when every repository has one. A copy has a label of the
    classification, which the release does not publish. A reason that `EXCLUSION_REASONS` lacks is
    named as the classification wrote it."""
    total = sum(unlabeled.values())
    if not total:
        return ""
    parts = [f"{count:,} {EXCLUSION_REASONS.get(reason, f'under the reason `{reason}`')}"
             for reason, count in unlabeled.items()]
    return (f" {total:,} {by_number(total, 'repository', 'repositories')} in the sampling frame "
            f"{by_number(total, 'has', 'have')} no published label: {series(parts)}.")


def engineered_sentence(annotated: int, engineered: int, unlabeled: dict[str, int]) -> str:
    """The sentence of the README that counts the engineered projects of the sample, the
    `engineered` published rows labeled `true`. The classification also labels `true` each
    repository that `unlabeled` counts under the reason `copy`, which the sample leaves out and
    which has no published label (`unlabeled_sentence`). With copies, the sentence therefore
    counts the engineered projects among the `annotated` published rows with a label. Without
    that scope, the sentence would read as a count of every repository labeled `true`, and a
    count that added the copies would exceed the labels that the README states for the
    annotated repositories."""
    classified = by_number(engineered, "was classified as an engineered project",
                           "were classified as engineered projects")
    if unlabeled.get("copy"):
        return (f" Of the {annotated:,} annotated "
                f"{by_number(annotated, 'repository', 'repositories')}, {engineered:,} "
                f"{classified}.")
    return f" {engineered:,} {classified}."


# How one of the classifying model's responses at a repository failed before the classifying
# model labeled the repository at its higher temperature or the second model labeled it, by the
# kind of the failed response that the classification's record gives (`SECOND_MODEL_REASONS` of
# `2_classification/1_classify_repos.py`).
FALLBACK_REASONS = {
    "output_limit": "a response stopped at its output limit before the model submitted its "
                    "checklist",
    "response_format": "a final response did not follow the response format",
    "tool_call": "the model called a tool that its request did not offer",
}


def fallback_reason(reason: str) -> str:
    """How one of the classifying model's responses failed, as the README states it
    (`FALLBACK_REASONS`), or the reason as the classification wrote it."""
    return FALLBACK_REASONS.get(reason, f"the reason `{reason}`")


def fallback_sentences(classification: dict) -> str:
    """The README's sentences on the repositories that the classifying model labeled at its
    higher temperature, on those that the second model labeled and on those that the second
    model left without a decision too, from the lists `higher_temperature_labels`,
    `second_model_labels` and `second_model_failures` of the classification's record, or ""
    when the record lists none. Each sentence names the classifying model, states how many
    repositories there are and names each with how one of the classifying model's responses
    there failed, so that the README itself lists them."""
    sentences = []
    for key in ("higher_temperature_labels", "second_model_labels", "second_model_failures"):
        listed = classification.get(key) or []
        if not listed:
            continue
        classifying = series(sorted({f"`{entry['classifying_model']}`" for entry in listed}))
        named = series([f"`{entry['repo_name']}` ({fallback_reason(entry['reason'])})"
                        for entry in sorted(listed,
                                            key=lambda entry: entry["repo_name"].lower())])
        count = len(listed)
        repositories = by_number(count, "repository", "repositories")
        # The repositories of the last two sentences, each with how the last of the classifying
        # model's responses at it failed.
        each = by_number(count, f"The repository, with how the last of the classifying model's "
                                f"responses there failed, is {named}.",
                         f"The repositories, each with how the last of the classifying "
                         f"model's responses there failed, are {named}.")
        models = series(sorted({f"`{entry['model']}`" for entry in listed}))
        if key == "higher_temperature_labels":
            temperatures = series(sorted({f"{entry['temperature']}" for entry in listed}))
            where = by_number(count, "the repository", "each repository")
            failures: dict[str, list[str]] = {}
            for entry in sorted(listed, key=lambda entry: entry["repo_name"].lower()):
                failures.setdefault(fallback_reason(entry["reason"]), []).append(
                    f"`{entry['repo_name']}`")
            grouped = " ".join(
                f"At {len(names):,} {by_number(len(names), 'repository', 'repositories')}, "
                f"{reason}: {series(names)}."
                for reason, names in sorted(failures.items(), key=lambda item: len(item[1]),
                                            reverse=True))
            sentences.append(f" The classifying model, {classifying}, labeled {count:,} "
                             f"{repositories} at its higher temperature, {temperatures}, after "
                             f"one of its responses at {where} failed. {grouped}")
        elif key == "second_model_labels":
            sentences.append(f" The second model, {models}, labeled {count:,} {repositories} "
                             f"that the classifying model, {classifying}, left without a "
                             f"decision after its retry rounds. {each}")
        else:
            sentences.append(f" Of the repositories that the classifying model, {classifying}, "
                             f"left without a decision after its retry rounds, the second "
                             f"model, {models}, left {count:,} {repositories} without a decision "
                             f"too. {each}")
    return "".join(sentences)


def backup_sentence(classification: dict) -> str:
    """The README's sentences on the repositories with a label that a backup provider served
    while the pinned provider of a model failed, from the list `backup_provider_labels` of the
    classification's record, or "" when the record lists none. The sentences state how many
    repositories there are and how many of them the backup providers served wholly, and point to
    the list, which names each repository, in the archive of the labels that the pipeline's
    repository tracks, because the release does not contain the record. Through an outage of a pinned provider, a backup
    provider can serve most of the repositories of a run, which no sentence of the README could
    name. The provider is no classification parameter, and such a label was made under the same
    parameters as the others."""
    listed = classification.get("backup_provider_labels") or []
    if not listed:
        return ""
    count, wholly = len(listed), sum(1 for entry in listed if entry["wholly"])
    return (f" While the pinned provider of a model failed, a backup provider of the same weights "
            f"in the same quantization served requests of {count:,} "
            f"{by_number(count, 'repository', 'repositories')} with the same settings, and every "
            f"request of {wholly:,} of them. The record `repos_annotated.json`, which the "
            f"classification of the pipeline writes beside its labels, names each "
            f"{by_number(count, 'repository', 'of the repositories')} with its backup "
            f"providers under `backup_provider_labels`. This release does not contain the record, "
            f"and the pipeline at {PIPELINE_REPOSITORY} tracks it in the archive "
            f"`data/classification/repos_annotated.7z` with the labels that the release was "
            f"built from.")


def queried_github(metadata) -> bool:
    """Whether the rows of a sampling frame contain GitHub's data.

    The sampling stage records `_metadata.input_source.github` when it has queried GitHub for
    every repository of its retrieval, and the rows then contain GitHub's `id` and GitHub's
    `createdAt` in UTC. A sampling frame filtered directly from the GHS search result, such as
    the sampling frame of March 2026, has GHS's database id as its `id` and GHS's time as its
    `createdAt`, with the hour on a 12-hour clock without AM or PM and no time zone.
    """
    source = metadata.get("input_source") if isinstance(metadata, dict) else None
    return isinstance(source, dict) and isinstance(source.get("github"), dict)


def collection_counts(snapshot_json: Path) -> tuple[int, int, int]:
    """The counts that the README states: the repositories in the sample, the repositories
    that git could not clone, and the cloned repositories without their collected tree.

    The first two come from the snapshot's `collection.counts`. A snapshot without them is
    refused, because the README could not state how many repositories were cloned and scanned.
    The third is the count of the outcome `missing_tree` in the snapshot's
    `commit_reclassification` section. Such a repository has a per-repository commit record and
    no directory in the collected tree, and the detection step read none of its files. The
    count is 0 for a snapshot without it.

    A section is refused too when its `left_out` holds anything other than a count per outcome,
    because the README counts the repositories without published commits in the evidence limit
    of their outcome (`gap_limit`). So is a section that does not name as many repositories under
    `repositories` as it counts for an outcome that the README states as an evidence limit
    (`LIMITED_OUTCOMES`), because the README names them."""
    snapshot = read_json(snapshot_json)
    collection = snapshot.get("collection") if isinstance(snapshot, dict) else None
    counts = collection.get("counts") if isinstance(collection, dict) else None
    if not (isinstance(counts, dict) and isinstance(counts.get("sampled"), int)
            and isinstance(counts.get("clone_failed"), int)):
        raise ValueError(f"{snapshot_json} lacks collection.counts.sampled or clone_failed, "
                         f"which the README needs to state how many repositories were cloned. "
                         f"Run `python -m agentcontext snapshot write` first.")
    reclassification = snapshot.get("commit_reclassification")
    left_out = (reclassification.get("left_out", {}) if isinstance(reclassification, dict)
                else {})
    if not (isinstance(left_out, dict) and all(counted(n) for n in left_out.values())):
        raise ValueError(f"{snapshot_json} records commit_reclassification.left_out without "
                         f"the counts by outcome, which the README needs to count the "
                         f"repositories without published commits. Run "
                         f"`python -m agentcontext snapshot write` first.")
    outcomes = (reclassification.get("counts") if isinstance(reclassification, dict)
                else None)
    named = (reclassification.get("repositories", {}) if isinstance(reclassification, dict)
             else {})
    unnamed = [outcome for outcome in LIMITED_OUTCOMES
               if isinstance(outcomes, dict) and outcomes.get(outcome)
               and not (isinstance(named, dict) and isinstance(named.get(outcome), list)
                        and len(named[outcome]) == outcomes[outcome])]
    if unnamed:
        raise ValueError(f"In {snapshot_json}, commit_reclassification.repositories names "
                         f"fewer or more repositories than the snapshot counts for "
                         f"{series(unnamed)}, but the README has to name each of the counted "
                         f"repositories. Run `python -m agentcontext snapshot write` first.")
    missing_tree = outcomes.get("missing_tree") if isinstance(outcomes, dict) else None
    return (counts["sampled"], counts["clone_failed"],
            missing_tree if isinstance(missing_tree, int) else 0)


def step_values(sampling: dict, key: str) -> dict:
    """The values that the filter recorded for its step `key` in the sampling frame's
    `_metadata`, which the release publishes as `sampling.json`. A sampling frame without the
    step gives an empty dict, such as the sampling frame of March 2026, in which no step has a
    key."""
    for stage in sampling.get("stages") or ():
        if isinstance(stage, dict) and stage.get("key") == key:
            values = stage.get("values")
            return values if isinstance(values, dict) else {}
    return {}


def unresolved_rows(sampling: dict) -> int:
    """The number of rows of the GHS retrieval that the filter's step `gone` removed because the
    GitHub query got no response for them in any of its rounds, the `unresolved` value of that
    step (`step_values`).

    The step runs before the filter's other steps, and the count includes rows that the other
    steps would have removed. The metadata records only the count. A sampling frame without the
    step has none, such as the sampling frame of March 2026, which did not query GitHub."""
    count = step_values(sampling, "gone").get("unresolved")
    return count if isinstance(count, int) else 0


def rows_without_id(sampling: dict) -> int:
    """The number of rows of the GHS retrieval that the filter's step `gone` removed because
    GitHub returned their repositories without an id, the `without_id` value of that step.

    GitHub's GraphQL schema declares a repository's `databaseId` nullable, and the later stages
    identify a repository by it. GitHub has these repositories, and the sampling frame leaves
    them out. A sampling frame without the count has none."""
    count = step_values(sampling, "gone").get("without_id")
    return count if isinstance(count, int) else 0


def kept_languages(sampling: dict) -> list:
    """The primary languages that the filter's step `languages` kept, as the values of that step
    record them. Because the step keeps the most common languages of each retrieval, the list
    can differ between releases. A sampling frame that records no list, such as the
    sampling frame of March 2026, or one from a filter that skipped the step, names none."""
    languages = step_values(sampling, "languages").get("languages")
    if not isinstance(languages, list):
        return []
    return [name for name in languages if isinstance(name, str)]


def left_out_languages(sampling: dict) -> dict:
    """{language: crawl date or None}, sorted by name, for the primary languages that the
    filter's step `languages` left out because the end date of the sampling window does not
    cover them, from the `left_out` value of that step.

    The step names only a language that it passed over while it chose its languages
    (`1_sampling/filter-ghs-spec.md`). A sampling frame without the record, such as the
    sampling frame of March 2026, names none."""
    left_out = step_values(sampling, "languages").get("left_out")
    if not isinstance(left_out, dict):
        return {}
    return {name: day if isinstance(day, str) and day else None
            for name, day in sorted(left_out.items()) if isinstance(name, str)}


def left_out_limit(sampling: dict, left_out: dict) -> str:
    """The README's evidence limit for the languages `left_out` that the filter's step
    `languages` left out (`left_out_languages`). It names each language with its crawl date and
    the end date of the sampling window, and states that the sampling frame contains no
    repository in these languages."""
    one = len(left_out) == 1
    names = series([f"{name} (crawl date {day})" if day else f"{name} (no crawl date)"
                    for name, day in left_out.items()])
    source = sampling.get("input_source")
    window = source.get("window") if isinstance(source, dict) else None
    end = window.get("end_date") if isinstance(window, dict) else None
    on = f", {end}," if isinstance(end, str) and end else ""
    text = (f"The filter's step `languages` left out {names}, because the end date of the "
            f"sampling window{on} does not cover {'it' if one else 'them'}. GHS has recorded the "
            f"pushes to repositories in a language up to the language's crawl date, the latest "
            f"commit date that GHS has in it. The window covers a language only when its crawl "
            f"date is on or after the end date.")
    top = step_values(sampling, "languages").get("top")
    if counted(top):
        text += (f" The step passed over {'it' if one else 'them'} while it chose the {top:,} "
                 f"most common primary languages that the end date covers.")
    which = next(iter(left_out)) if one else "these languages"
    return text + f" The sampling frame contains no repository in {which}."


def series(names: list) -> str:
    """The names as English writes a list: `A`, `A and B`, or `A, B, and C`."""
    if len(names) < 3:
        return " and ".join(names)
    return f"{', '.join(names[:-1])}, and {names[-1]}"


# The README's words for the rows that a step of the filter removed, by the key of the step in
# the sampling frame's `_metadata`. `filter_removals` words the steps `cleanup`, `licenses`,
# `languages` and `activity` from the values that each records, and adds to the words of
# `duplicates` the names that GitHub resolved to two repositories when the step counts any.
# `mirrors` names a step that the current filter lacks and that the sampling frame of the
# development dataset records.
FILTER_STEP_REMOVALS = {
    "gone": "rows for which GitHub returned no public repository with an id under the name, or "
            "a repository that GitHub created after the latest creation date that the sampling "
            "window allows",
    "duplicates": "duplicate rows, which GitHub resolves to one repository",
    "missing_data": "repositories without a count of pull requests or issues",
    "educational": "repositories with an educational or resource topic or name",
    "mirrors": "repositories at the head commit of a repository that GitHub created earlier",
}

# The README's words for the flags of GitHub that the step `cleanup` records under `flags`.
CLEANUP_FLAGS = {"isArchived": "archived", "isDisabled": "disabled", "isLocked": "locked",
                 "isFork": "forked"}


def threshold(value) -> str:
    """A threshold of the step `activity`, which the filter records as a float, without the
    decimals of a whole number."""
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,}"


def filter_removals(sampling: dict) -> list[str]:
    """The README's phrase for the rows that each step of the filter removed, for every step
    that the sampling frame's `_metadata` records with a removed count above 0, in the order of
    the steps.

    `FILTER_STEP_REMOVALS` gives the phrase of a step by its key. The phrases of `cleanup`,
    `licenses`, `languages` and `activity` name what the values of the step record: the flags,
    the number of allowed licenses, the kept languages (`kept_languages`) and the thresholds.
    The phrase of `duplicates` also names the rows of a name that GitHub resolved to two
    repositories when the step counts such names under `shared_names`. A
    step that records none of these values, a step with another key and a step without a key,
    such as each step of the sampling frame of March 2026, are named by the description that the
    filter recorded for the step, or by the number of the step."""
    phrases = []
    for stage in sampling.get("stages") or ():
        if not (isinstance(stage, dict) and counted(stage.get("removed"))
                and stage["removed"] > 0):
            continue
        key = stage.get("key")
        values = stage.get("values") if isinstance(stage.get("values"), dict) else {}
        phrase = FILTER_STEP_REMOVALS.get(key)
        if key == "duplicates" and counted(values.get("shared_names")) and values["shared_names"]:
            phrase += ", and the rows of a name under which GitHub returned two repositories"
        elif key == "cleanup":
            flags = [CLEANUP_FLAGS[flag] for flag in values.get("flags") or ()
                     if flag in CLEANUP_FLAGS]
            phrase = f"{series(flags)} repositories, by GitHub's flags" if flags else None
        elif key == "licenses":
            allowed = values.get("allowed")
            phrase = (f"repositories without one of the {len(allowed):,} OSI-approved licenses "
                      f"that the filter allows" if isinstance(allowed, list) and allowed
                      else None)
        elif key == "languages":
            languages = kept_languages(sampling)
            phrase = (f"repositories with a primary language other than {series(languages)}"
                      if languages else
                      "repositories in a primary language that the end date of the sampling "
                      "window does not cover" if left_out_languages(sampling) else None)
        elif key == "activity":
            below = [f"fewer than {threshold(values[f'{column}_min'])} {column}"
                     for column in ("commits", "watchers")
                     if isinstance(values.get(f"{column}_min"), (int, float))
                     and not isinstance(values.get(f"{column}_min"), bool)]
            phrase = f"repositories with {' or '.join(below)}" if below else None
        if phrase is None:
            description = stage.get("description")
            phrase = (f'the rows of the step "{description}"'
                      if isinstance(description, str) and description
                      else f"the rows of step {stage.get('stage')}")
        phrases.append(phrase)
    return phrases


def origin_sentence(sampling: dict, snapshot: dict) -> str:
    """The README's sentence on where the repositories of the release come from and when they
    were scanned: the GHS retrieval of the sampling frame and the end date of its sampling
    window, from the sampling frame's `_metadata`, and the `scanned_at_window` of the snapshot.

    A sampling frame without a retrieval id, such as the sampling frame of March 2026, names the
    day on which its GHS search result was downloaded. A part that the inputs do not record is
    left out."""
    source = sampling.get("input_source") if isinstance(sampling, dict) else None
    source = source if isinstance(source, dict) else {}
    window = source.get("window") if isinstance(source.get("window"), dict) else {}
    retrieval, end = source.get("retrieval_id"), window.get("end_date")
    downloaded = source.get("downloaded_at")
    if isinstance(retrieval, str) and retrieval:
        origin = f"retrieval `{retrieval}` of the GitHub Search dataset (GHS)"
        if isinstance(end, str) and end:
            origin += f", with a sampling window that ends on {end}"
    elif isinstance(downloaded, str) and downloaded:
        origin = (f"a search result of the GitHub Search dataset (GHS) downloaded on "
                  f"{downloaded[:10]}")
    else:
        origin = "the GitHub Search dataset (GHS)"
    collection = snapshot.get("collection") if isinstance(snapshot, dict) else None
    scans = collection.get("scanned_at_window") if isinstance(collection, dict) else None
    first = scans.get("first") if isinstance(scans, dict) else None
    last = scans.get("last") if isinstance(scans, dict) else None
    if isinstance(first, str) and first and isinstance(last, str) and last:
        return (f"The repositories come from {origin}, and they were scanned from {first} to "
                f"{last}.")
    return f"The repositories come from {origin}."


def by_number(count: int, one: str, other: str) -> str:
    """`one` for a count of 1 and `other` for any other count, so that a sentence of the README
    agrees in number with the count that it states."""
    return one if count == 1 else other


def sampling_provenance(ghs_json: Path) -> dict:
    """The sampling frame's `_metadata`, published as `sampling.json`: the GHS retrieval that
    the sampling frame was filtered from, its sampling window and query, and the filter's
    steps. A sampling frame without `_metadata.input_source` is refused, because the release
    could not state where its sample came from."""
    metadata = read_json(ghs_json).get("_metadata")
    if not isinstance(metadata, dict) or not isinstance(metadata.get("input_source"), dict):
        raise ValueError(f"{ghs_json} has no _metadata.input_source. Without it, the release "
                         f"cannot name the GHS retrieval that the sampling frame was filtered "
                         f"from.")
    return metadata


def download_report(report_path: Path, sampling: dict) -> dict | None:
    """The report of the GHS download if it describes the retrieval that the sampling frame was
    filtered from and that retrieval limits the evidence, else None.

    The report describes the last download (`1_sampling/1_download_ghs.py`). A download that
    gets no response from GHS, or that refuses a GHS search result with far fewer rows than the
    retrieval before, keeps the previous sampling frame. Its report records the outcome
    `fallback`, the reason and the count of downloads in a row that fell back. Because the
    filter does not run again over the same retrieval, `sampling.json` does not show the
    fallback. A download with `--accept-smaller` takes such a GHS search result, and its report
    records the outcome `retrieved` with the reason as `accepted_smaller`, which
    `sampling.json` does not show either. The README states both from the report.

    A report of a retrieval other than the sampling frame's gives None, and so does a missing
    report, as in a data root that no download wrote. A report that cannot be read is refused,
    because the README could not state whether the sampling window moved."""
    try:
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"{report_path} cannot be read ({error}). The README states from it "
                         f"whether the GHS download fell back or took a smaller retrieval with "
                         f"--accept-smaller.") from error
    if not isinstance(report, dict):
        return None
    if report.get("outcome") != "fallback" and not (report.get("outcome") == "retrieved"
                                                    and report.get("accepted_smaller")):
        return None
    source = sampling.get("input_source") if isinstance(sampling, dict) else None
    if not isinstance(source, dict) or report.get("retrieval_id") != source.get("retrieval_id"):
        return None
    return report


def counted(value) -> bool:
    """Whether a value of the report is a count, which JSON's `true` is not."""
    return isinstance(value, int) and not isinstance(value, bool)


def sentence_of(reason) -> str:
    """The download's reason as a sentence of its own: capitalized, with one period. A value
    that is not text gives none."""
    reason = reason.strip().rstrip(".") if isinstance(reason, str) else ""
    return f"{reason[:1].upper()}{reason[1:]}." if reason else ""


def fallback_limit(report: dict) -> str:
    """The README's evidence limit for a sampling frame from a GHS download that fell back.

    The limit names the retrieval, the retrieval that the download attempted before it fell
    back, the reason, the number of repositories of a GHS search result that the size check
    refused, and the count of downloads in a row that fell back. Because a later Start can
    publish again without a download, the limit names the retrievals rather than the run."""
    count = report.get("fallbacks_in_a_row")
    refused = report.get("failed_item_count")
    failed = report.get("failed_retrieval_id")
    attempt = (f"The GHS download of the retrieval `{failed}`" if failed
               else "A later GHS download")
    text = (f"The sampling frame comes from the GHS retrieval `{report.get('retrieval_id')}`, "
            f"which an earlier GHS download took. {attempt} failed and fell back to it, and the "
            f"sampling window did not move.")
    if reason := sentence_of(report.get("reason")):
        text += f" The download gave this reason: {reason}"
    if counted(refused):
        text += (f" The GHS search result that the download refused had {refused:,} "
                 f"{by_number(refused, 'repository', 'repositories')}.")
    if counted(count) and count > 1:
        text += f" {count:,} downloads in a row have fallen back to that retrieval."
    elif count == 1:
        text += " No earlier download fell back to that retrieval."
    return text


def smaller_limit(report: dict) -> str:
    """The README's evidence limit for a sampling frame from a retrieval that the GHS download
    took with `--accept-smaller` although its size check refused it. The limit names the
    retrieval, its number of repositories, the retrieval before it, and the reason, which
    states the counts that the size check compared."""
    rows = report.get("item_count")
    size = (f" of {rows:,} {by_number(rows, 'repository', 'repositories')}" if counted(rows)
            else "")
    previous = report.get("previous_retrieval_id")
    before = f"the retrieval `{previous}`" if previous else "the retrieval"
    text = (f"The sampling frame comes from the GHS retrieval `{report.get('retrieval_id')}`"
            f"{size}. The size check of the GHS download refused it as too small compared with "
            f"{before} that the sampling stage read before, and the download took it with "
            f"`--accept-smaller`. The sampling frame can therefore lack repositories that the "
            f"earlier retrieval had and that still meet its criteria.")
    if reason := sentence_of(report.get("accepted_smaller")):
        text += f" The download gave this reason: {reason}"
    return text


def download_limit(report: dict) -> str:
    """The README's evidence limit for the report that `download_report` returns."""
    return fallback_limit(report) if report.get("outcome") == "fallback" else smaller_limit(report)


def build_repos(output_dir: Path, ghs_json: Path, annotated_csv: Path) -> list[dict]:
    """The sampling frame, with the classification labels and the detection flags joined.

    One row per repository in the sampling frame, in the sampling frame's order. A repository
    that was scanned has `scanned_at` and the flags, and a repository that was not scanned has
    them blank. These blanks distinguish the three provenance levels in the published file.

    `id` is GitHub's id of the repository. A sampling frame that queried GitHub has it, and
    the labels copy it from that sampling frame. A label with another id was therefore made
    over another sampling frame and is refused. In a sampling frame that did not query GitHub,
    `id` is GHS's database id, and the published `id` is the labels' `github_id` instead. It is
    blank for a repository that the classification did not label or that GitHub did not find.
    """
    filtered_json = read_json(ghs_json)
    sampling_frame = filtered_json["items"]
    github = queried_github(filtered_json.get("_metadata"))

    annotated_header, annotated_rows = read_csv(annotated_csv)
    missing = [c for c in ANNOTATED_COLUMNS if c not in annotated_header]
    if missing:
        raise ValueError(f"{annotated_csv} lacks the column(s) {missing}")
    labels = {row["repo_name"]: row for row in annotated_rows}

    detected_header, detected_rows = read_csv(output_dir / "repos.csv")
    detected = {row["repo_name"]: row for row in detected_rows}

    # Every column of the detection CSV is published, renamed or discarded on purpose.
    # `write_csv` drops a column that `REPOS_COLUMN_ORDER` does not name without a warning, and a
    # column added to the detection step but not to the publication would disappear unnoticed.
    # This check names such a column.
    published_or_renamed = set(REPOS_COLUMN_ORDER) | set(DETECTED_RENAMES)
    unexpected = [c for c in detected_header
                  if c not in published_or_renamed
                  and c not in DISCARDED_DETECTED_COLUMNS]
    if unexpected:
        raise ValueError(
            f"{output_dir / 'repos.csv'} has column(s) {unexpected} that would be dropped "
            f"without being published. Add each one to REPOS_COLUMN_ORDER, to "
            f"DETECTED_RENAMES or to DISCARDED_DETECTED_COLUMNS.")

    # The loop below builds one row per repository in the sampling frame, as a left join does. It
    # would drop a scanned repository outside the sampling frame without a change to the
    # published row count, and this check refuses such a repository.
    orphans = sorted(set(detected) - {item.get("name") for item in sampling_frame})
    if orphans:
        raise ValueError(
            f"{len(orphans):,} {by_number(len(orphans), 'repository has', 'repositories have')} "
            f"rows in output/repos.csv and {by_number(len(orphans), 'is', 'are')} not in "
            f"{ghs_json.name} ({named(orphans)}). The release builds its rows from "
            f"{ghs_json.name} alone, and no published count shows a repository that it leaves "
            f"out.")

    rows = []
    other_ids = []
    for item in sampling_frame:
        repo_name = item["name"]
        label = labels.get(repo_name, {})
        found = detected.get(repo_name, {})

        row = {field: cell(item.get(field)) for field in GHS_SCALAR_FIELDS}
        if not github:
            row["id"] = label.get("github_id", "")
        elif label.get("github_id") and label["github_id"] != row["id"]:
            other_ids.append(f"{repo_name} ({row['id']} in the sampling frame, "
                             f"{label['github_id']} in the labels)")
        row["languages"] = cell(item.get("languages", {}))
        row["topics"] = cell(item.get("topics", []))
        row["repo_name"] = repo_name

        for source, target in DETECTED_RENAMES.items():
            row[target] = found.get(source, "")
        for column in detected_header:
            if column in DETECTED_RENAMES or column == "repo_name":
                continue
            row[column] = found.get(column, "")

        row["engineered_project"] = label.get("engineered_project", "")
        row["justification"] = label.get("justification", "")
        # The classification stage read the default branch later than the sampling frame did, and
        # its value is the branch that the scan would have used. The sampling frame's default
        # branch is the fallback.
        row["default_branch"] = label.get("default_branch") or row.get("defaultBranch", "")
        branch = row["scanned_branch"] or row["default_branch"]
        row["github_link"] = f"https://github.com/{repo_name}/tree/{repos.url_path(branch)}"

        rows.append(row)

    if other_ids:
        raise ValueError(
            f"{len(other_ids):,} {by_number(len(other_ids), 'repository has', 'repositories have')} "
            f"another GitHub id in {annotated_csv.name} than in {ghs_json.name}. "
            f"{by_number(len(other_ids), 'It is', 'They include')} {named(other_ids)}. The labels "
            f"come from another sampling frame.")
    absent = [c for c in REPOS_COLUMN_ORDER if c not in rows[0]]
    if absent:
        raise ValueError(f"REPOS_COLUMN_ORDER names absent column(s) {absent}")
    if len(rows) != len(sampling_frame):
        raise ValueError(f"Expected {len(sampling_frame):,} rows, built {len(rows):,}")
    names = [r["repo_name"] for r in rows]
    if len(set(names)) != len(names):
        raise ValueError("The sampling frame names the same repository twice")
    # The README defines the criteria that the published verdicts name. A label made under a
    # criterion that neither the classification nor the list of removed criteria has would
    # publish a key without a meaning.
    undefined = {key: repositories for key, repositories in named_criteria(rows).items()
                 if key not in CRITERION_MEANINGS and key not in REMOVED_CRITERION_MEANINGS}
    if undefined:
        labels = len({name for repositories in undefined.values() for name in repositories})
        raise ValueError(
            f"The {by_number(labels, 'verdict', 'verdicts')} of {labels:,} "
            f"{by_number(labels, 'label names', 'labels name')} "
            f"{by_number(len(undefined), 'a criterion', 'criteria')} that the README does not "
            f"define, namely "
            + ", ".join(f"{key} ({named(repositories)})"
                        for key, repositories in sorted(undefined.items()))
            + ". Add the meaning of each to CRITERION_MEANINGS, or to "
              "REMOVED_CRITERION_MEANINGS for a criterion that the classification removed.")
    return rows


def build_context_files(output_dir: Path) -> list[dict]:
    """`output/context-files.csv` in publication column order."""
    header, rows = read_csv(output_dir / "context-files.csv")
    missing = [c for c in CONTEXT_FILES_COLUMN_ORDER if c not in header]
    if missing:
        raise ValueError(f"CONTEXT_FILES_COLUMN_ORDER names absent column(s) {missing}")
    unexpected = [c for c in header if c not in CONTEXT_FILES_COLUMN_ORDER]
    if unexpected:
        raise ValueError(
            f"output/context-files.csv has column(s) {unexpected} that would be dropped "
            f"without being published. Add each one to CONTEXT_FILES_COLUMN_ORDER.")
    return rows


# Each commit CSV of the release, with the commit heuristics that its `matched_heuristics` names
# by id, as `RELEASE_FILES` names their source.
COMMIT_HEURISTICS = {"commits.csv": "heuristics/ai_commit_patterns.csv",
                     "other_commits.csv": "heuristics/other_ai_commit_patterns.csv"}


def commit_csv_problems(source: Path, patterns: Path) -> list[str]:
    """The ways in which the commit CSV `source` disagrees with `patterns`, the commit
    heuristics that the release publishes beside it.

    The release refuses a CSV with other columns than `commits.COMMIT_COLUMNS`, such as one
    written before the heuristics had ids, and `commit-reclassification` rewrites it from the
    per-repository commit records. It also refuses an id in `matched_heuristics` that `patterns`
    lacks, because the id names nothing that a reader of the release could look up. `publish`
    calls this function before it writes anything. The function streams the CSV, because it is
    large and nothing needs it in memory.
    """
    with open(source, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header != list(commits.COMMIT_COLUMNS):
            return [f"{source} has the columns {header}, not {list(commits.COMMIT_COLUMNS)}. "
                    f"Run commit-reclassification to write it under the current heuristics."]
        matched = header.index("matched_heuristics")
        ids = set()
        for row in reader:
            ids.update(part for part in row[matched].split(";") if part)
    _, rows = read_csv(patterns)
    unknown = ids - {row["id"] for row in rows}
    if not unknown:
        return []
    return [f"{len(unknown):,} {by_number(len(unknown), 'heuristic id', 'heuristic ids')} that "
            f"{source.name} names {by_number(len(unknown), 'is', 'are')} not in {patterns.name} "
            f"({named(unknown)}). Run `python -m agentcontext commit-reclassification` to "
            f"classify the commits under the current heuristics."]


def build_commits(source: Path, destination: Path) -> tuple[int, int, int]:
    """Write the commit CSV to `destination` with every message verbatim and every line break as
    a newline, in a field that the CSV writer quotes as RFC 4180 specifies. Return (rows,
    messages changed, rows without a `commit_timestamp`).

    The cloning step already writes every line break as a newline, and the count of changed
    messages is normally 0. The conversion stays here because the published file needs the
    guarantee whatever an earlier step wrote. The detection step leaves a `commit_timestamp`
    blank where git printed the date in a form that does not parse, such as for a commit of the
    year 10000, and the README counts these rows. `publish` compares the columns of the CSV with
    `commits.COMMIT_COLUMNS` before it writes anything (`commit_csv_problems`). The function
    streams the CSV, because it is large and nothing needs it in memory.
    """
    changed = 0
    total = 0
    undated = 0
    handle, temporary = tempfile.mkstemp(dir=str(destination.parent),
                                         prefix=destination.name + ".", suffix=".tmp")
    try:
        with open(source, newline="", encoding="utf-8") as fin, \
                os.fdopen(handle, "w", encoding="utf-8", newline="") as fout:
            reader = csv.reader(fin)
            writer = csv.writer(fout, lineterminator="\n")
            header = next(reader)
            message = header.index("commit_message")
            timestamp = header.index("commit_timestamp")
            writer.writerow(header)
            for row in reader:
                if "\r" in row[message]:
                    row[message] = row[message].replace("\r\n", "\n").replace("\r", "\n")
                    changed += 1
                if not row[timestamp].strip():
                    undated += 1
                writer.writerow(row)
                total += 1
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return total, changed, undated


def commits_source(output_dir: Path, work_dir: Path, name: str = "all_ai_commits.csv") -> Path:
    """The commit CSV `name` in `output/`, by default `all_ai_commits.csv`, extracted from the
    archive beside it when it is absent.

    Git ignores the CSV and tracks the archive, and a clean checkout therefore has only the
    archive. The release cannot publish without the commit CSV, and the step extracts it so
    that nobody has to run `7zz x` by hand.
    """
    plain = output_dir / name
    if plain.is_file():
        return plain
    from agentcontext import csv_archive
    archive = csv_archive(plain)
    if not archive.is_file():
        raise FileNotFoundError(
            f"Neither {plain} nor {archive} exists, and the release needs the commit CSV")
    logs.info('release', f"{plain.name} does not exist. Extracting it from {archive.name}.")
    run_7z(["x", str(archive), f"-o{work_dir}", "-y"])
    # The search covers every depth. The current archive stores the CSV at its root, and an
    # earlier archive stores it under `3_data-collection/output/`, the collection stage's
    # directory before the data root. A search of the extraction root alone would stop a
    # publication from a clean checkout that has an earlier archive and not the CSV.
    extracted = sorted(path for path in work_dir.rglob(plain.name) if path.is_file())
    if not extracted:
        raise FileNotFoundError(f"{archive} does not contain {plain.name}")
    return extracted[0]


# ---------------------------------------------------------------------------
# 7zz
# ---------------------------------------------------------------------------

def run_7z(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    """Run 7zz once, and put its output into the error when it fails."""
    result = subprocess.run([SEVEN_ZIP, *args], capture_output=True, text=True,
                            cwd=None if cwd is None else str(cwd))
    if result.returncode != 0:
        output = (result.stdout[-500:] + result.stderr[-500:]).strip()
        raise RuntimeError(f"7zz {' '.join(args)} exited {result.returncode} ({output})")
    return result


def compress(release_dir: Path, name: str, source_name: str) -> None:
    """Replace `release/<name>` with a 7z of `release/<source_name>`.

    The archive stores no timestamps, so that its bytes change only when its content does. 7zz
    stores the source file's modification time by default. Because this step writes the source
    anew on every run, the checksum manifest would change on every run although the data had
    not. The function deletes the archive first, because `7zz a` updates an existing archive
    rather than replacing it.

    7zz runs in `release/` and receives the bare name, because it stores a relative path as
    given. Given the path, it would store `release/repos.csv` inside `repos.csv.7z` under a
    relative `--release-dir`, and `repos.csv` under an absolute one.
    """
    target = release_dir / name
    target.unlink(missing_ok=True)
    run_7z(["a", "-bso0", "-bsp0", "-mtc=off", "-mtm=off", "-mta=off", name, source_name],
           cwd=release_dir)


def archive_file_count(path: Path) -> int:
    """The number of files that `7zz l` reports in an archive.

    The summary line ends in `N files, M folders`, or in `N files` alone when the archive
    contains no folder, and the pattern accepts both. The summary is the last line of the
    listing, and the function takes the last match so that an entry with a name that ends in
    `files` is not read as the summary.
    """
    listing = run_7z(["l", str(path)]).stdout
    found = re.findall(r"(\d[\d,]*) files(?:, \d[\d,]* folders)?$", listing, re.MULTILINE)
    if not found:
        raise RuntimeError(f"Could not read a file count out of `7zz l {path.name}`")
    return int(found[-1].replace(",", ""))


def archive_entries(path: Path) -> list[str]:
    """The path of every entry of an archive, read from `7zz l -slt`. The listing names each
    entry on a line of its own after the line of dashes that ends the archive's header."""
    lines = run_7z(["l", "-slt", str(path)]).stdout.splitlines()
    if "----------" not in lines:
        raise RuntimeError(f"Could not read the entries out of `7zz l -slt {path.name}`")
    start = lines.index("----------") + 1
    return [line[len("Path = "):] for line in lines[start:] if line.startswith("Path = ")]


def imported_files(imports_csv: Path) -> list[tuple[str, str]]:
    """`(owner§repo, target)` for every target that `imports.csv` places in the archive of the
    imported files, sorted and without duplicates. A path that the archive step prunes, such as
    a session transcript, is left out. The import detection step marks such a path `pruned`,
    and the archive of the collected files leaves it out too."""
    from agentcontext.prune import is_prunable
    _, rows = read_csv(imports_csv)
    return sorted({(repos.repo_name_to_dir(row["repo_name"]), row["target"]) for row in rows
                   if row["target_location"] == IMPORTED_ARCHIVE_NAME
                   and not is_prunable(row["target"])})


def imported_sources(imports_csv: Path,
                     imported_dir: Path) -> tuple[list[tuple[str, str, Path]], list[str]]:
    """The files of the imported tree that `imported_files.7z` packs, and the problems that stop
    the release.

    The first list holds `(owner§repo, target, file)` for every target that `imports_csv`
    places in that archive (`imported_files`), with the file of the imported tree that the
    archive packs for it. Each target is the file that the tool read at its path, and a link
    that the cloning step wrote into the imported tree therefore resolves to the file that the
    link points to. The second list names the targets that the imported tree lacks, because the
    rows that name them would describe files that the release does not contain, and the targets
    of which the imported tree holds only a stub (`imports.read_stubs`), because a stub's bytes
    are not the file's. `publish` calls this function before it writes anything, so that such a
    target leaves `release/` as it was.
    """
    from agentcontext import imports
    found, missing, stubbed, stubs = [], [], [], {}
    for dir_name, target in imported_files(imports_csv):
        repository = imported_dir / dir_name
        source = imports.regular_file(repository, target)
        if source is None:
            missing.append(f"{target} of {dir_name}")
            continue
        if dir_name not in stubs:
            stubs[dir_name] = imports.read_stubs(repository)
        if source.relative_to(repository.resolve()).as_posix() in stubs[dir_name]:
            stubbed.append(f"{target} of {dir_name}")
            continue
        found.append((dir_name, target, source))
    problems = []
    if missing:
        problems.append(
            f"{imports_csv.parent.name}/{imports_csv.name} places {len(missing):,} "
            f"{by_number(len(missing), 'target', 'targets')} in {IMPORTED_ARCHIVE_NAME} that the "
            f"imported tree {imported_dir} lacks ({named(missing)}). Run "
            f"`python -m agentcontext import-detection` over the imported tree that the release "
            f"reads.")
    if stubbed:
        problems.append(
            f"{imports_csv.parent.name}/{imports_csv.name} places {len(stubbed):,} "
            f"{by_number(len(stubbed), 'target', 'targets')} in {IMPORTED_ARCHIVE_NAME} of which "
            f"the imported tree {imported_dir} holds only a stub, which the list of the stubs "
            f"beside the directory of its repository names ({named(stubbed)}). Run "
            f"`python -m agentcontext import-detection` over the imported tree that the release "
            f"reads.")
    return found, problems


def copied_csv_rows(output_dir: Path,
                    config_path_patterns: Path) -> tuple[dict[str, int], list[str]]:
    """The number of rows of each CSV that the release copies unchanged, and the problems that
    stop the release.

    Each CSV is read as a CSV rather than counted as lines, and `csv.reader` counts a row with
    a newline in a field once. The copy does not open the files, and a file without a
    `repo_name` column, such as an empty file, is not a CSV that a step of the collection wrote.
    The release would otherwise publish such a file as it is, and the README would describe it
    as having -1 rows. The copy has the bytes of its source, and the count is therefore that of
    the published file. `publish` calls this function before it writes anything.
    """
    counts, problems = {}, []
    for entry in RELEASE_FILES:
        if entry.section != "CSVs" or entry.kind != "copy":
            continue
        source = resolve(entry.source, output_dir, config_path_patterns)
        with open(source, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header and "repo_name" in header:
                counts[entry.name] = sum(1 for _ in reader)
                continue
        step, command = (("import detection", "import-detection") if entry.name == "imports.csv"
                         else ("detection", "detection"))
        problems.append(f"{source} has no repo_name column and is not a CSV that the {step} step "
                        f"wrote. Run `python -m agentcontext {command}` again.")
    return counts, problems


def build_imported_archive(release_dir: Path, imported_dir: Path,
                           files: list[tuple[str, str, Path]]) -> int:
    """Write `release/imported_files.7z` with each file of `files`, which `imported_sources`
    resolved, under `imported/<owner§repo>/<target>`, and return the number of files in it.

    The archive is packed as the collected_files_archive step packs `repos_data.7z`: from a
    copy in a staging directory, with the times of the files and of their directories, and
    written whole rather than updated.
    """
    from agentcontext.steps.collected_files_archive import restore_dir_times
    archive = release_dir / IMPORTED_ARCHIVE_NAME
    archive.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as staging:
        root = Path(staging) / IMPORTED_ARCHIVE_ROOT
        root.mkdir()
        for dir_name, target, source in files:
            destination = root / dir_name / target
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        if imported_dir.is_dir():
            restore_dir_times(str(root), str(imported_dir))
        run_7z(["a", "-t7z", "-snl", "-mx=9", "-bso0", "-bsp0", str(archive.resolve()),
                IMPORTED_ARCHIVE_ROOT], cwd=Path(staging))
    return len(files)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def inventory_problems(output_dir: Path) -> list[str]:
    """Every file in `output/` that nobody decided about, and every required file that it
    lacks."""
    problems = []
    for entry in sorted(os.listdir(output_dir)):
        if not inventoried(entry) and entry not in FILE_MANAGER_ENTRIES:
            problems.append(
                f"{output_dir.name}/{entry} is in neither OUTPUT_INVENTORY nor the "
                f"release. Add it to the inventory with what it is, or remove it.")
    for name in OUTPUT_REQUIRED:
        if not (output_dir / name).exists():
            problems.append(f"{output_dir.name}/{name} is missing, and the release needs it."
                            f"{OUTPUT_REMEDIES.get(name, '')}")
    return problems


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# How many items of each difference a refusal names.
NAMED_DIFFERENCES = 5


def named(items) -> str:
    """The first NAMED_DIFFERENCES of a sorted collection, for a message that names what
    differs."""
    items = sorted(items)
    more = len(items) - NAMED_DIFFERENCES
    return ", ".join(items[:NAMED_DIFFERENCES]) + (f" and {more:,} more" if more > 0 else "")


def release_version(provenance) -> str:
    """The `dataset_version` that `provenance.json` records, or an empty string where it records
    none."""
    version = provenance.get("dataset_version") if isinstance(provenance, dict) else None
    return version if isinstance(version, str) else ""


def consistency_problems(output_dir: Path, release_dir: Path,
                         config_path_patterns: Path) -> list[str]:
    """Every way in which the files that the release would publish disagree with one another.

    Each difference stops the release rather than causing a warning. A heuristics file other
    than the one that `provenance.json` names would publish heuristics that produced none of
    the rows. An archive of collected files packed from another tree would be described in the
    README as the files of the scanned repositories. A commit CSV classified under other
    commit heuristics would be published beside heuristics that its `matched_heuristics` does
    not name. A file in `release/` that this step does not write would be beside a checksum
    manifest that does not cover it, and a person who uploads the directory would publish the
    file unverified.
    """
    problems = []
    if release_dir.is_dir():
        for entry in sorted(os.listdir(release_dir)):
            if entry not in RELEASE_NAMES and entry not in FILE_MANAGER_ENTRIES:
                problems.append(f"{release_dir / entry} is not a file of the release, and the "
                                f"checksum manifest does not cover it. Remove it.")

    provenance = read_json(output_dir / "provenance.json")
    # `CITATION.cff` and the Citation section of the README name the version that the rows
    # record, and `release_version` reads it after these checks.
    if not release_version(provenance):
        problems.append(f"{output_dir.name}/provenance.json records no dataset_version, which "
                        f"CITATION.cff and the README name. Run the detection again.")
    recorded = provenance.get("heuristics_sha256") if isinstance(provenance, dict) else None
    actual = sha256_of(config_path_patterns)
    if recorded != actual:
        problems.append(f"{config_path_patterns} has the SHA-256 {actual}, but "
                        f"{output_dir.name}/provenance.json records {recorded} for the "
                        f"heuristics that the detection ran with. Run the detection again, or "
                        f"name the heuristics that it ran with in --heuristics.")
    # The heuristics of the planning documents, which the release publishes beside their CSV.
    planning_patterns = planning.PLANNING_DOCUMENT_PATTERNS_PATH
    recorded = (provenance.get("planning_heuristics_sha256") if isinstance(provenance, dict)
                else None)
    actual = sha256_of(planning_patterns)
    if recorded != actual:
        problems.append(f"{planning_patterns} has the SHA-256 {actual}, but "
                        f"{output_dir.name}/provenance.json records {recorded} for the "
                        f"heuristics of the planning documents that the detection ran with. "
                        f"Run the detection again.")
    # The two heuristics files of the vendored directories and of the local state, which the
    # release publishes among the heuristics that the dataset was built with.
    for key, path in (("vendored_dirs_sha256", heuristics.VENDORED_DIRS_PATH),
                      ("local_state_sha256", heuristics.LOCAL_STATE_PATHS_PATH)):
        recorded = provenance.get(key) if isinstance(provenance, dict) else None
        actual = sha256_of(path)
        if recorded != actual:
            problems.append(f"{path} has the SHA-256 {actual}, but {output_dir.name}/"
                            f"provenance.json records "
                            f"{recorded if recorded is not None else 'no SHA-256'} for the file "
                            f"that the detection ran with. Run the detection again.")

    snapshot = read_json(output_dir / "snapshot.json")
    reclassification = (snapshot.get("commit_reclassification")
                        if isinstance(snapshot, dict) else None)
    classified_by = (reclassification.get("classified_by")
                     if isinstance(reclassification, dict) else None)
    if classified_by is not None and classified_by != commits.CLASSIFIED_BY:
        problems.append(f"{output_dir.name}/snapshot.json records that the commits were "
                        f"classified under the commit heuristics {classified_by}, but the commit "
                        f"heuristics of this checkout are {commits.CLASSIFIED_BY}. Run "
                        f"`python -m agentcontext commit-reclassification`, then "
                        f"`python -m agentcontext snapshot write`.")

    archive = release_dir / ARCHIVE_NAME
    archived, outside = set(), []
    for path in archive_entries(archive):
        parts = path.split("/")
        if parts[0] != ARCHIVE_ROOT:
            outside.append(path)
        elif len(parts) > 1:
            archived.add(parts[1])
    _, detected_rows = read_csv(output_dir / "repos.csv")
    scanned = {repos.repo_name_to_dir(row["repo_name"]) for row in detected_rows}
    if outside:
        problems.append(f"{archive} contains {len(outside):,} entries outside {ARCHIVE_ROOT}/ "
                        f"({named(outside)}). Run "
                        f"`python -m agentcontext collected-files-archive`.")
    if archived != scanned:
        problems.append(
            f"{archive} contains {len(archived - scanned):,} repositories that "
            f"{output_dir.name}/repos.csv does not name ({named(archived - scanned)}) and lacks "
            f"{len(scanned - archived):,} that it names ({named(scanned - archived)}). Run "
            f"`python -m agentcontext collected-files-archive` over the validated tree that the "
            f"detection read.")
    return problems


# ---------------------------------------------------------------------------
# The generated files
# ---------------------------------------------------------------------------

def cited_authors() -> str:
    """The authors of the release in their order, as the Citation section of the README names
    them."""
    return ", ".join(f"{given} {family}" for given, family in CITATION_AUTHORS)


def render_citation(version: str) -> str:
    """`CITATION.cff`, from the author list above and `version`, the `dataset_version` that
    `provenance.json` records."""
    lines = [
        "cff-version: 1.2.0",
        "message: If you use this dataset, please cite it as below.",
        "type: dataset",
        f"title: \"{CITATION_TITLE}\"",
        f"version: \"{version}\"",
        f"license: {CITATION_LICENSE}",
        "authors:",
    ]
    for given, family in CITATION_AUTHORS:
        lines.append(f"  - given-names: {given}")
        lines.append(f"    family-names: {family}")
    lines.append("")
    return "\n".join(lines)


def render_checksums(release_dir: Path, names) -> str:
    """`checksums.sha256`, in the format that `sha256sum -c` reads, sorted by name."""
    return "".join(f"{sha256_of(release_dir / name)}  {name}\n" for name in sorted(names))


def megabytes(path: Path, decimals: int = 1) -> float | int:
    size = path.stat().st_size / (1024 * 1024)
    return round(size, decimals) if decimals else round(size)


# What each incomplete outcome of the commit reclassification means for a repository, as the
# README's evidence limits state it after a count of 1 and after any other count. Every outcome
# that `commit_reclassification` counts as incomplete has an entry, so that the README names the
# reason when a reclassification is incomplete for a reason other than a missing log.
RECLASSIFICATION_GAPS = {
    "no_log": ("repository lacks commit logs", "repositories lack commit logs"),
    "no_scope": ("repository has commit logs without recorded ref and revision provenance",
                 "repositories have commit logs without recorded ref and revision provenance"),
    "no_path_history": ("repository lacks a complete commit history of its configuration files",
                        "repositories lack a complete commit history of their configuration "
                        "files"),
    "history_incomplete": ("repository has configuration files for which the commit history "
                           "query failed",
                           "repositories have configuration files for which the commit history "
                           "query failed"),
    "history_not_read": ("repository lacks its commit history, because the cloning step could "
                         "not fetch or read it",
                         "repositories lack their commit history, because the cloning step "
                         "could not fetch or read it"),
    "missing_record": ("repository lacks a per-repository commit record",
                       "repositories lack a per-repository commit record"),
    "missing_tree": ("repository lacks the collected tree that its per-repository commit record "
                     "describes",
                     "repositories lack the collected tree that their per-repository commit "
                     "records describe"),
    "failed": ("repository could not be reclassified, because its evidence could not be "
               "processed",
               "repositories could not be reclassified, because their evidence could not be "
               "processed"),
}

# The outcomes with an evidence limit in the README, which names their repositories: the
# incomplete outcomes and `failed`, and the clone failures (`unavailable`).
LIMITED_OUTCOMES = (*RECLASSIFICATION_GAPS, "unavailable")


def named_repositories(names: list) -> str:
    """The sentence of an evidence limit that names its repositories, `names` in their order."""
    listed = series([f"`{name}`" for name in names])
    return by_number(len(names), f"The repository is {listed}.", f"The repositories are {listed}.")


def gap_limit(outcome: str, count: int, left_out: int, names: list = ()) -> str:
    """The README's evidence limit for the `count` repositories with the incomplete outcome
    `outcome`, `left_out` of which have shards that the commit reclassification left out of the
    commit CSVs. The limit ends with `names`, the repositories of the outcome.

    The limit includes the left-out repositories in the count of their outcome. A limit of
    their own would name them a second time, and a reader would add the two counts. The reason
    for a `failed` repository is already in its outcome. Any other left-out repository has
    commits that other commit heuristics classified, and the missing evidence of its outcome
    keeps the current heuristics from classifying them again. A repository without a
    per-repository commit record has no shard, and none of its commits is published. The limit
    of `missing_record` states this for every such repository."""
    limit = f"{count:,} {by_number(count, *RECLASSIFICATION_GAPS[outcome])}."
    if outcome == "missing_record" and count:
        limit += (f" Neither `commits.csv` nor `other_commits.csv` contains any of "
                  f"{by_number(count, 'its commits', 'their commits')}.")
    elif left_out:
        whose = (by_number(count, "its commits", "their commits") if left_out == count
                 else f"the commits of {left_out:,} of them")
        reason = ("" if outcome == "failed" else
                  ", because other commit heuristics classified the commits and the current "
                  "heuristics could not classify them again")
        limit += (f" Neither `commits.csv` nor `other_commits.csv` contains any of "
                  f"{whose}{reason}.")
    return f"{limit} {named_repositories(names)}" if names else limit


def evidence_limits(snapshot: dict, sampling: dict, classification: dict,
                    download: dict | None, blank_dates: dict,
                    blank_ai: tuple[int, int] = (0, 0)) -> list[str]:
    """The paragraphs of the README's evidence limits, each sentence agreeing in number with
    the count that it states. `download` is the report that `download_report` returns.
    `blank_dates` holds, per file, the count of published rows with a blank date:
    `head_commit_date` in `repos.csv` and `commit_timestamp` in each commit CSV. `blank_ai` is
    the number of rows of `context_files.csv` with blank AI columns and the number of their
    repositories."""
    limitations = []
    if download is not None:
        limitations.append(download_limit(download))
    reclassification = snapshot.get("commit_reclassification", {})
    outcomes = reclassification.get("counts", {})
    # In the snapshot, `left_out` holds for each outcome the number of repositories with shards
    # that the commit reclassification left out of the commit CSVs, and `repositories` names the
    # repositories of each outcome. `collection_counts` refuses a `left_out` of any other form,
    # and a `repositories` that names fewer or more repositories than the snapshot counts for
    # an outcome that the README states as an evidence limit.
    left_out = reclassification.get("left_out", {})
    named = reclassification.get("repositories", {})
    if reclassification.get("complete") is False:
        # The commit reclassification leaves out of the commit CSVs every shard that other
        # commit heuristics classified (`commit_reclassification.left_out_shards`), and
        # `consistency_problems` refuses a snapshot that records other commit heuristics than
        # the current ones.
        limitations.append("Commit reclassification is incomplete, because the commit evidence "
                           "of some repositories is missing or incomplete. Even so, every commit "
                           "in `commits.csv` and `other_commits.csv` was classified with the "
                           "current commit heuristics.")
        limitations.extend(gap_limit(outcome, outcomes[outcome], left_out.get(outcome, 0),
                                     named.get(outcome, []))
                           for outcome in RECLASSIFICATION_GAPS if outcomes.get(outcome, 0))
    unavailable = outcomes.get("unavailable", 0)
    if reclassification.get("population_complete") is False and unavailable:
        limit = by_number(
            unavailable,
            "1 repository in the sample has a recorded clone or collection failure, and neither "
            "its configuration nor its commits are included.",
            f"{unavailable:,} repositories in the sample have a recorded clone or collection "
            f"failure, and neither their configuration nor their commits are included.")
        names = named.get("unavailable", [])
        limitations.append(f"{limit} {named_repositories(names)}" if names else limit)
    blank_rows, blank_repositories = blank_ai
    if blank_rows:
        limitations.append(
            f"{blank_rows:,} {by_number(blank_rows, 'row', 'rows')} of `context_files.csv`, in "
            f"{blank_repositories:,} "
            f"{by_number(blank_repositories, 'repository', 'repositories')}, "
            f"{by_number(blank_rows, 'has', 'have')} blank `first_commit_ai_created`, "
            f"`first_commit_ai_tool`, `#ai_commits` and `ai_commit_tools`, because the current "
            f"commit heuristics did not classify the commits that touched "
            f"{by_number(blank_rows, 'the file', 'these files')}. A blank in these columns means "
            f"that the value is unknown, not that it is `FALSE` or 0.")
    missing_metadata = snapshot.get("metadata", {}).get("rows_without_commit_metadata", {}).get("total", 0)
    if missing_metadata:
        limitations.append(f"{missing_metadata:,} configuration "
                           f"{by_number(missing_metadata, 'row has', 'rows have')} missing commit "
                           f"metadata. Unavailable values remain blank.")
    missing_planning = (snapshot.get("planning_documents", {})
                        .get("rows_without_commit_metadata", {}).get("total", 0))
    if missing_planning:
        limitations.append(f"{missing_planning:,} "
                           f"{by_number(missing_planning, 'row', 'rows')} of "
                           f"`{planning_schema.CSV_NAME}` "
                           f"{by_number(missing_planning, 'has', 'have')} missing commit "
                           f"metadata. Unavailable values remain blank.")
    # The detection step leaves blank a date that git printed in a form that does not parse
    # (`repos.to_utc_or_blank`).
    undated_repos = blank_dates.get("repos.csv", 0)
    if undated_repos:
        limitations.append(by_number(
            undated_repos,
            "1 repository in `repos.csv` has a blank `head_commit_date`, because git printed the "
            "committer date of its `head_sha` in a form that does not parse, such as for a commit "
            "of the year 10000.",
            f"{undated_repos:,} repositories in `repos.csv` have a blank `head_commit_date`, "
            f"because git printed the committer date of their `head_sha` in a form that does not "
            f"parse, such as for a commit of the year 10000."))
    undated = [(blank_dates.get(name, 0), name) for name in ("commits.csv", "other_commits.csv")
               if blank_dates.get(name, 0)]
    if undated:
        one = len(undated) == 1 and undated[0][0] == 1
        parts = series([f"{count:,} {by_number(count, 'commit', 'commits')} in `{name}`"
                        for count, name in undated])
        limitations.append(f"{parts} {'has' if one else 'have'} a blank `commit_timestamp`, "
                           f"because git printed {'its date' if one else 'their dates'} in a "
                           f"form that does not parse, such as for a commit of the year 10000.")
    unresolved = unresolved_rows(sampling)
    if unresolved > 0:
        limitations.append(by_number(
            unresolved,
            "The filter removed 1 row of the GHS retrieval in its first step, `gone`, because "
            "GitHub's API returned no response for it in any round of the GitHub query. Whether "
            "GitHub still has this repository is unknown. That step runs before the filter's "
            "other steps, which might have removed the row as well.",
            f"The filter removed {unresolved:,} rows of the GHS retrieval in its first step, "
            f"`gone`, because GitHub's API returned no response for them in any round of the "
            f"GitHub query. Whether GitHub still has these repositories is unknown. That step "
            f"runs before the filter's other steps, and the count includes rows that those steps "
            f"would have removed."))
    without_id = rows_without_id(sampling)
    if without_id > 0:
        limitations.append(by_number(
            without_id,
            "The filter removed 1 row of the GHS retrieval in its first step, `gone`, because "
            "GitHub's API returned its repository without a repository ID, by which the later "
            "stages identify a repository. GitHub has this repository, and the sampling frame "
            "leaves it out. That step runs before the filter's other steps, which might have "
            "removed the row as well.",
            f"The filter removed {without_id:,} rows of the GHS retrieval in its first step, "
            f"`gone`, because GitHub's API returned their repositories without a repository ID, "
            f"by which the later stages identify a repository. GitHub has these repositories, "
            f"and the sampling frame leaves them out. That step runs before the filter's other "
            f"steps, and the count includes rows that those steps would have removed."))
    # The languages that the filter's step `languages` left out (`left_out_languages`).
    left_out = left_out_languages(sampling)
    if left_out:
        limitations.append(left_out_limit(sampling, left_out))
    # The classification's record counts as `undecided` the repositories that it left without a
    # decision after its retry rounds and, where the second model applied, after the second model.
    # The second model labels only some of them, and the README names those that it left without
    # a decision in a sentence of their own (`fallback_sentences`). A record without the count
    # has none.
    undecided = classification.get("undecided", 0)
    if counted(undecided) and undecided > 0:
        limitations.append(by_number(
            undecided,
            "1 repository in the sampling frame has no label, because the classification reached "
            "no decision for it after its retry rounds and, where the second model applied, after "
            "the second model. It is not in the sample, and neither its configuration nor its "
            "commits are included.",
            f"{undecided:,} repositories in the sampling frame have no label, because the "
            f"classification reached no decision for them after its retry rounds and, where the "
            f"second model applied, after the second model. They are not in the sample, and "
            f"neither their configuration nor their commits are included."))
    return limitations


# Where a line of a published line count ends, as the detection step splits a text
# (`detection.split_lines`), in the README's definitions of `line_count` and `target_line_count`.
LINE_BREAKS = ("Lines end at LF, CRLF or CR, not at another character, such as a form feed or "
               "U+2028")


def render_readme(release_dir: Path, counts: dict, levels: tuple, classification: dict,
                  sampling: dict, collection: tuple[int, int, int],
                  download: dict | None = None, blank_dates: dict | None = None,
                  imported: int = 0, unlabeled: dict | None = None,
                  labels: dict | None = None, blank_ai: tuple[int, int] = (0, 0),
                  removed_criteria: tuple[str, ...] = (), *, version: str) -> str:
    """`0_README.md`, the file that the Zenodo record shows.

    Every number in it is computed here, the model that labeled the sample comes from the
    classification's record, and the descriptions of `id` and `createdAt` follow how the
    sampling frame's `_metadata` records that they were read. `download` is the report of a GHS
    download that limits the evidence (`download_report`), `blank_dates` the count of published
    rows with a blank date per file (`evidence_limits`), `imported` the number of files in the
    archive of the imported files, `unlabeled` the number of rows without a label per
    reason (`unlabeled_by_reason`), `labels` the number of rows with each label
    (`labels_of`), `blank_ai` the number of rows of `context_files.csv` with blank AI
    columns and the number of their repositories (`evidence_limits`), `removed_criteria` the
    keys of `REMOVED_CRITERION_MEANINGS` that a published verdict names (`named_criteria`), and
    `version` the `dataset_version` that `provenance.json` records, which the Citation section
    names as `CITATION.cff` does."""
    total, annotated, engineered, study = levels
    unlabeled = unlabeled or {}
    without_label = sum(unlabeled.values())
    engineered_description = "Whether the repo was classified as an engineered project"
    if labels:
        engineered_description += ": " + ", ".join(
            f"`{label}` for {labels[label]:,}" for label in ("true", "false", "unclear")
            if labels.get(label))
        if labels.get("unclear"):
            engineered_description += (
                ". The label is `false` when an exclusion criterion applies, the README is not "
                "mainly in English, no purpose is stated or a fork has not become a project of its "
                "own. Otherwise it is `unclear` when one of these is unclear or the model judged "
                "the evidence of software engineering practices unclear, and `true` in every "
                "other case")
    if without_label:
        engineered_description += (
            f". Blank for the {without_label:,} "
            f"{by_number(without_label, 'repository', 'repositories')} in the sampling frame "
            f"without a published label, which the Provenance section counts by reason")
    sampled, clone_failed, missing_tree = collection
    # A repository without its collected tree was cloned, but the detection step read none of
    # its files, and it does not count as scanned.
    scanned = sampled - clone_failed - missing_tree
    detection = (f"Of the {sampled:,} {by_number(sampled, 'repository', 'repositories')} in the "
                 f"sample, {scanned:,} {by_number(scanned, 'was', 'were')} cloned and scanned for "
                 f"configuration artifacts")
    if missing_tree:
        detection += (f", git could not clone {clone_failed:,}, and {missing_tree:,} "
                      f"{by_number(missing_tree, 'was', 'were')} not scanned because "
                      f"{by_number(missing_tree, 'its', 'their')} collected files were missing.")
    else:
        detection += f", and git could not clone {clone_failed:,}."
    models = series([f"`{model}`" for model in classification["models"]])
    # A justification is the response of the model that labeled the repository. With several
    # models, the sentences after the description of the classification step name the
    # repositories that the second model labeled.
    justified_by = by_number(len(classification["models"]), models,
                             "the model that labeled the repository")
    criteria = "\n".join(f"- `{key}`: {meaning}" for key, meaning in
                         [*CRITERION_MEANINGS.items(),
                          *((key, REMOVED_CRITERION_MEANINGS[key]) for key in removed_criteria)])
    # The steps of the filter and their parameters differ between sampling frames, and the
    # README names what each step removed from the GHS retrieval of this one.
    removals = filter_removals(sampling)
    sampled_from = "Repositories were sourced from the GitHub Search dataset."
    if removals:
        sampled_from += (" The steps of the filter of the sampling stage left out these rows, and "
                         "`sampling.json` records how many each step removed:\n"
                         + "\n".join(f"   - {removal}" for removal in removals))
    if queried_github(sampling):
        id_description = ("GitHub's repository ID, which stays the same when the repository is "
                          "renamed or transferred")
        created_description = "Repository creation time in UTC, as GitHub reports it"
    else:
        id_description = ("GitHub's repository ID, which stays the same when the repository is "
                          "renamed or transferred, as the classification recorded it: the "
                          "sampling frame of this release did not query GitHub and has GHS's "
                          "database id instead. Blank for a repository that the classification "
                          "did not label or that GitHub did not find")
        created_description = ("Repository creation time as GHS recorded it, with no time zone "
                               "and the hour on a 12-hour clock without AM or PM: the date is "
                               "right and the hour is modulo 12. The sampling frame of this "
                               "release did not query GitHub")

    overview = "\n".join(
        f"| `{f.name}` | {counts[f.name]:,} | {f.description} |"
        for f in RELEASE_FILES if f.section == "CSVs")

    repos_7z = megabytes(release_dir / "repos.csv.7z")
    commits_7z = megabytes(release_dir / "commits.csv.7z")
    data_7z = megabytes(release_dir / ARCHIVE_NAME)
    repos_plain = megabytes(release_dir / "repos.csv", decimals=0)
    commits_plain = megabytes(release_dir / "commits.csv", decimals=0)
    artifact_files = archive_file_count(release_dir / ARCHIVE_NAME)
    imported_7z = megabytes(release_dir / IMPORTED_ARCHIVE_NAME)
    deletion_7z = megabytes(release_dir / DELETION_ARCHIVE_NAME)
    imported_repositories = len({dir_name for dir_name, _target
                                 in imported_files(release_dir / "imports.csv")})

    provenance = "\n".join(
        f"| `{f.name}` | {f.description} |"
        for f in RELEASE_FILES if f.section == "provenance")

    withheld = "\n".join(f"- `{name}`: {why}" for name, why in sorted(WITHHELD.items()))
    _, planned = read_csv(release_dir / planning_schema.CSV_NAME)
    planned_repos = len({row["repo_name"] for row in planned})
    snapshot = read_json(release_dir / "snapshot.json")
    limitations = evidence_limits(snapshot, sampling, classification, download, blank_dates or {},
                                  blank_ai)
    evidence = "\n## Evidence limits\n\n" + "\n\n".join(limitations) + "\n" if limitations else ""
    incomplete_outcomes = "\n".join(f"- `{outcome}`: the {one}" for outcome, (one, _)
                                    in RECLASSIFICATION_GAPS.items())

    return f"""\
# {CITATION_TITLE}

This dataset contains configuration artifacts for six tools (i.e., Claude Code, GitHub Copilot, OpenAI Codex, Cursor, Gemini, and OpenCode) in {study:,} GitHub {by_number(study, 'repository', 'repositories')}. It includes git metadata for each configuration artifact (e.g., creation dates and commit counts), commits that credit a tool, imported files, and planning documents from these repositories. {origin_sentence(sampling, snapshot)}

The documents of the pipeline at {PIPELINE_REPOSITORY} describe how this release was built, and `docs/heuristics.md` there defines which files count as configuration artifacts, imports and planning documents and when a commit credits a tool. This dataset succeeds AgentConfigs, the dataset that the AIware 2026 dataset paper describes. An earlier version of the pipeline built AgentConfigs 1.0.0 and differs from the current one in its sampling, classification, collection and commit attribution.
{evidence}
## Citation

Cite this release as: {cited_authors()}. **{CITATION_TITLE}.** Version {version}. Zenodo. `CITATION.cff` gives the same citation in the Citation File Format.

## Associated Papers

- {AIWARE_DATASET_PAPER_AUTHORS}. **A Dataset of Agentic AI Coding Tool Configurations.** In *Proceedings of the 3rd ACM International Conference on AI-Powered Software (AIware '26)*. It describes AgentConfigs 1.0.0, the release of the dataset that this one succeeds. DOI: https://doi.org/10.1145/3805760.3814922. Available at: https://assets.empirical-software.engineering/pdf/aiware26-ai-config-dataset.pdf
- Matthias Galster, Seyedmoein Mohsenimofidi, Jai Lal Lulla, Muhammad Auwal Abubakar, Christoph Treude, Sebastian Baltes. **Configuring Agentic AI Coding Tools: An Exploratory Study.** In *Proceedings of the 3rd ACM International Conference on AI-Powered Software (AIware '26)*. DOI: https://doi.org/10.1145/3805760.3814887
- Seyedmoein Mohsenimofidi, Matthias Galster, Christoph Treude, Sebastian Baltes. **Context Engineering for AI Agents in Open-Source Software.** In *Proceedings of the 23rd International Conference on Mining Software Repositories (MSR '26)*. DOI: https://doi.org/10.1145/3793302.3793350. Available at: https://arxiv.org/abs/2510.21413

## Dataset Overview

The dataset contains repository, commit, and per-mechanism CSVs, plus CSVs for imports and planning documents:

| File | Rows | Description |
|------|------|-------------|
{overview}

## Provenance

The pipeline that built the dataset narrows the repositories in three levels:

1. **Sampling frame** ({total:,} {by_number(total, 'repo', 'repos')}): {sampled_from}
2. **Classification** ({annotated:,} annotated): Repositories were classified by {models} ({classification['description']}) to determine whether each repository is an engineered software project.{fallback_sentences(classification)}{backup_sentence(classification)}{engineered_sentence(annotated, engineered, unlabeled)}{unlabeled_sentence(unlabeled)}
3. **Configuration detection** ({study:,} {by_number(study, 'repo', 'repos')}): {detection} {study:,} {by_number(study, 'repository', 'repositories')} contained at least one configuration artifact.

All three levels are represented in `repos.csv`:
- {by_number(total, f'The {total:,} row has', f'All {total:,} rows have')} GitHub metadata (e.g., `mainLanguage` and `stargazers`)
- {annotated:,} {by_number(annotated, 'row has', 'rows have')} classification data (i.e., `engineered_project` and `justification`)
- {study:,} {by_number(study, 'row has', 'rows have')} detection flags of tools, context files and mechanisms (e.g., `claude`, `copilot` and `context_file`)

These files record how the data above was produced:

| File | Description |
|------|-------------|
{provenance}

The release withholds these pipeline CSVs:

{withheld}

Other files in the pipeline output directory support building and checking the dataset but are not inputs to any release file. They are not published. `OUTPUT_INVENTORY` in `agentcontext/steps/release.py` at {PIPELINE_REPOSITORY} describes each one.

## File Descriptions

### repos.csv

Repository metadata, with one row per repository in the sampling frame.

| Column | Description |
|--------|-------------|
| `repo_name` | GitHub repository identifier (`owner/name`) |
| `id` | {id_description} |
| `default_branch` | Default branch name |
| `scanned_branch` | Branch scanned during data collection (repos with detected configurations only) |
| `scanned_at` | Date on which the repository was scanned (repos with detected configurations only) |
| `head_sha` | Commit that the repository was at when it was scanned (repos with detected configurations only) |
| `head_commit_date` | Committer date of `head_sha` (repos with detected configurations only). Blank where git printed it in a form that does not parse, such as for a commit of the year 10000 |
| `dataset_version` | Version of the dataset that the detection step wrote into the row (repos with detected configurations only) |
| `engineered_project` | {engineered_description} |
| `github_link` | Link to the repository on GitHub (points to `scanned_branch` if available, otherwise `default_branch`) |
| `justification` | The justification that {justified_by} gave in its response, followed by the classification's verdict, which names the criteria that decided the label and, for a fork or a mirror, the check of GitHub's data that decided it |
| `claude` | Repository has Claude Code configuration |
| `gemini` | Repository has Gemini CLI, Gemini Code Assist or Antigravity configuration |
| `codex` | Repository has OpenAI Codex configuration, such as for the Codex CLI, the Codex app or the Codex GitHub Action |
| `copilot` | Repository has GitHub Copilot configuration, such as for Copilot in VS Code, Copilot CLI or the Copilot cloud agent |
| `cursor` | Repository has Cursor configuration |
| `opencode` | Repository has OpenCode configuration |
| `claude.md` | Repository has a CLAUDE.md context file |
| `gemini.md` | Repository has a GEMINI.md context file |
| `agents.md` | Repository has an AGENTS.md context file |
| `copilot_instructions` | Repository has Copilot instructions (`.github/copilot-instructions.md` or a `.github/instructions/` directory, at the root or in a directory below it, where Copilot CLI reads them) |
| `cursorrules` | Repository has a `.cursorrules` file |
| `context_file` | Repository has at least one context file |
| `settings` | Repository has tool settings files |
| `skills` | Repository has skill definitions |
| `subagents` | Repository has subagent definitions |
| `commands` | Repository has custom command files |
| `hooks` | Repository has hook configurations |
| `rules` | Repository has rule files |
| `mcp` | Repository has MCP configurations |
| `plugins` | Repository has a recognized plugin or extension manifest, a marketplace catalog, or an OpenCode plugin or custom tool module, for one of the six tools or the portable Agent Plugins format |
| `lsp` | Repository has an LSP server configuration (Copilot CLI, a Claude Code plugin or OpenCode) |
| `no_specific_tool` | Repository has a detected mechanism and none of the six tool flags: every configuration artifact that it has is one that several tools read, such as AGENTS.md, `.agents/skills` or a root `.mcp.json`. A repository that also has a tool's own files counts for that tool |
| `mainLanguage` | Primary programming language |
| `license` | Repository license |
| `homepage` | Repository homepage URL |
| `createdAt` | {created_description} |
| `size` | Repository size in KB |
| `commits` | Total number of commits |
| `branches` | Number of branches |
| `releases` | Number of releases |
| `forks` | Number of forks |
| `watchers` | Number of watchers |
| `stargazers` | Number of stars |
| `contributors` | Number of contributors |
| `totalIssues` | Total number of issues |
| `openIssues` | Number of open issues |
| `totalPullRequests` | Total number of pull requests |
| `openPullRequests` | Number of open pull requests |
| `codeLines` | Total lines of code, blank where GHS recorded no count |
| `blankLines` | Total blank lines, blank where GHS recorded no count |
| `commentLines` | Total comment lines, blank where GHS recorded no count |
| `languages` | JSON object mapping language names to byte counts |
| `topics` | JSON array of GitHub topics |

The criteria that the verdict of a `justification` names:

{criteria}

### commits.csv

Commits that credit one of the six tools, in the repositories with detected configuration. One row per commit.

| Column | Description |
|--------|-------------|
| `repo_name` | GitHub repository identifier |
| `commit_timestamp` | Author date of the commit, in UTC. Blank where git printed it in a form that does not parse, such as for a commit of the year 10000 |
| `branch` | Branch on which the commit was found |
| `commit_sha` | Commit SHA |
| `github_link` | Link to the commit on GitHub |
| `commit_message` | Full commit message, verbatim, its line breaks as newlines inside the quoted field, as RFC 4180 specifies. Read it with a CSV reader. The commit CSV of AgentConfigs 1.0.0 wrote a newline as `\\n` instead |
| `ai_tool` | The tools that the commit credits with making it or with producing its change, such as a review suggestion that a person committed, by their names in the `tool` column of `ai_commit_patterns.csv` (`Claude`, `Codex`, `Copilot`, `Cursor`, `Gemini` and `OpenCode`), joined with a comma |
| `matched_heuristics` | The heuristics that attributed the commit, by their `id` in `ai_commit_patterns.csv`, joined with a semicolon |
| `attributions` | How the commit credits each tool, as `<tool>:<attribution>` joined with a semicolon. `account` means that the author or the committer of the commit is the tool's own account, the identity (name and address together) that the tool's code or environment writes, or an agent account or a GitHub App that its own page or the configuration of its project identifies as running the tool. An address that GitHub links to a vendor's account is `account` only when the tool's code or environment writes it. `reviewer` is the co-author trailer that GitHub writes when a person commits a suggestion of a review app, such as Copilot code review, and the footer that GitHub writes into a pull request that a person creates from a suggestion of Copilot Autofix. `standard` is the line that the tool writes itself, with its name and its own address or account, or a marker that it writes into a commit or a pull request. `convention` is any other line that names the tool, which a person writes, or an agent writes because a repository's instructions require it, and any other name or address that names the tool and that users chose for an author or a committer. A line that heuristics of two attributions match takes the first attribution in this order. The `attribution` column of `ai_commit_patterns.csv` gives the attribution of each heuristic |

### other_commits.csv

Commits attributed to tools outside the six that this dataset studies, found in the same commit histories of the same repositories and matched by the same kinds of heuristic: the GitHub App account that the tool commits as or an agent account that its own page identifies as running the tool, a trailer with the tool's own address, the co-author trailer that GitHub writes when a person commits a review app's suggestion, the tool's name first in an attribution trailer, a disclosure or a footer in the message body, the merge commit of a branch with the tool's prefix, or a name or an address that names the tool and that users chose for an author or a committer. The columns are those of `commits.csv`. `ai_tool` names the tools outside the six that the commit credits, joined with a comma as in `commits.csv`, and `matched_heuristics` and `attributions` refer to `other_ai_commit_patterns.csv`. A commit that credits one of the six tools and another tool is in both files. A credit that names Gemini but none of Gemini CLI, Gemini Code Assist and Antigravity, such as `Generated-by: Gemini Pro`, is here with `ai_tool` `Gemini (unspecified)`, because it can mean any of the three or Google's Gemini app. A credit that names one of the three is in `commits.csv`. A commit with a trailer or a disclosure in the message body that credits AI without naming a tool, naming instead a model such as GPT-5 or Grok, a vendor, or AI itself, is here with `ai_tool` `tool not named`, but only when no heuristic of either file names a tool for it.{'' if counts['other_commits.csv'] else ' The file has its header only, because the collection of this release predates it.'}

### context_files.csv

Context files that give instructions to coding agents. One row per context file. A file that
a Codex configuration, a Gemini CLI settings file or an extension manifest names as its context
file, or that the `instructions` of an OpenCode configuration list, is a context file, whatever it
is called.

| Column | Description |
|--------|-------------|
| `repo_name` | GitHub repository identifier |
| `context_file` | Relative path to the context file |
| `tool` | Tool that the file is attributed to: `claude`, `gemini`, `codex`, `copilot`, `cursor` or `opencode`, several joined with `\\|` when more than one tool reads the file through its own mechanism, or `any` for a file that several tools read and none owns, such as AGENTS.md |
| `scanned_at` | Date on which the repository was scanned |
| `branch` | Branch where the file was found |
| `head_sha` | Commit that the repository was at when it was scanned |
| `head_commit_date` | Committer date of `head_sha`. Blank where git printed it in a form that does not parse, such as for a commit of the year 10000 |
| `dataset_version` | Release of the detection heuristics that produced the row |
| `github_link` | Link to the file on GitHub |
| `created_at` | Committer date, in UTC, of the first commit introducing the file |
| `#commits` | Number of commits on `branch` touching the file, under any of its names. Commits on other branches are not counted |
| `language` | Detected natural language of the file content, `n/a` for a text without a detectable language and for a file that is not text, such as one that git-crypt encrypted, and blank for a file that could not be read. A context file that is a symbolic link has the language detected in the path of its target, `n/a` for a short path such as `AGENTS.md` |
| `line_count` | Number of lines in the file, blank for a file that is not text, such as one that git-crypt encrypted, and for a file that could not be read. A context file that is a symbolic link counts one line, the path of its target, whether `{ARCHIVE_NAME}` holds the link or a one-line file that names the target. A count of the lines of such a link extracted from the archive, such as with `wc -l`, reads its target and differs. {LINE_BREAKS} |
| `is_reference` | Whether the file is a reference to another file, blank where `line_count` is blank. A blank value is unknown, not FALSE. A context file that is a symbolic link to a path that ends in `.md` is a reference to that path |
| `referenced_file` | Path of the referenced file if `is_reference` is TRUE, blank otherwise. For a context file that is a symbolic link, the path of its target as the link names it |
| `first_commit_sha` | SHA of the first commit introducing the file |
| `last_commit_sha` | SHA of the most recent commit touching the file |
| `first_commit_ai_created` | Whether the first commit credits one of the six tools, by the heuristics of `ai_commit_patterns.csv`. When the first commit credits only a tool outside the six, which `other_commits.csv` lists, the column is FALSE. Blank where the current commit heuristics did not classify the commits that touched the file |
| `first_commit_ai_tool` | The tools of the six that the first commit credits, by the names of `ai_tool` in `commits.csv`, joined with a comma, blank when the first commit credits none of them and where `first_commit_ai_created` is blank |
| `#ai_commits` | Number of commits touching the file that credit one of the six tools. Blank where `first_commit_ai_created` is blank |
| `ai_commit_tools` | The tools of the six that the commits touching the file credit, by the names of `ai_tool` in `commits.csv`, joined with a comma, blank when none of these commits credits one of them and where `first_commit_ai_created` is blank |

### settings.csv, skills.csv, subagents.csv, commands.csv, hooks.csv, rules.csv, mcp.csv, plugins.csv, lsp.csv

The per-mechanism CSVs. Each file contains one row per configuration artifact found in the repositories with detected configurations. All of them share these columns:

| Column | Description |
|--------|-------------|
| `repo_name` | GitHub repository identifier |
| *(configuration artifact path)* | Relative path to the configuration artifact (the column name varies by file) |
| `tool` | Tool that the configuration artifact is attributed to, as in `context_files.csv`. The tool flags of `repos.csv` are the tools of a repository's configuration artifacts in all published mechanism CSVs, `context_files.csv` included |
| `scanned_at` | Date on which the repository was scanned |
| `branch` | Branch where the configuration artifact was found |
| `head_sha` | Commit that the repository was at when it was scanned |
| `head_commit_date` | Committer date of `head_sha`. Blank where git printed it in a form that does not parse, such as for a commit of the year 10000 |
| `dataset_version` | Release of the detection heuristics that produced the row |
| `github_link` | Link to the configuration artifact on GitHub |
| `created_at` | Committer date, in UTC, of the first commit introducing the configuration artifact |
| `#commits` | Number of commits on `branch` touching the configuration artifact, under any of its names. Commits on other branches are not counted |
| `first_commit_sha` | SHA of the first commit introducing the configuration artifact |
| `last_commit_sha` | SHA of the most recent commit touching the configuration artifact |

Some files have additional columns:

- **skills.csv**: `name` (skill name), `scripts`/`references`/`assets` (whether the skill has these subdirectories), `language` (detected natural language)
- **subagents.csv**: `name` (agent name), `memory` (for an agent in a `.claude/agents` directory or in the folder that such a directory links to, TRUE when `.claude/agent-memory/<name>/` or `.claude/agent-memory-local/<name>/` beside that directory holds a file, `<name>` being the value of `name`, whether or not the front matter of the agent sets `memory`, and FALSE otherwise; `n/a` for every other agent, such as an agent of a plugin or of another tool), `language`
- **commands.csv**: `name` (command name), `language`
- **rules.csv**: `name` (rule name), `language`

`language` is the detected natural language of the configuration artifact, `n/a` for a text without a detectable language and for a file that is not text, and blank for a link to a file that the collection did not copy and for a file that could not be read.

### imports.csv

Imports extend the dataset beyond configuration artifacts. They are references that make a tool automatically load another file into context with a configuration artifact, such as `@docs/style.md` in a `CLAUDE.md`, including further imports in the referenced files. For each tool, `import_patterns.csv` documents the importing files, syntax, relative path resolution, and depth, with supporting pages or posts. `not_imports.csv` records what the tools do not read as imports.

An import sets no tool flag, adds no repository to `repos.csv`, and changes no row in the other CSVs. Each row describes one import per tool and chain, including imports with missing targets. A file imported by two chains has a row in each. Gemini CLI and Antigravity reread a file for each import that reaches it, but the dataset lists its imports only once per chain, at the smallest depth at which the tool reads it.

| Column | Description |
|--------|-------------|
| `repo_name`, `scanned_at`, `branch`, `head_sha`, `head_commit_date` | The repository and its collection, as in the per-mechanism CSVs |
| `importer` | The file that contains the import |
| `importer_category` | The mechanism of `importer`, such as `context_file` or `rules` where `importer` is `root`, or `imported` where `importer` is a file that the chain imported |
| `root` | The importing file at the start of the chain |
| `tool` | The tool that reads the import: `claude` (Claude Code and Claude Code Review), `copilot`, `gemini` (Gemini CLI and Antigravity) or `cursor` (Bugbot) |
| `heuristic` | The `id` of the row of `import_patterns.csv` that reads it |
| `import` | The import as written, such as `@docs/style.md` |
| `line` | The line of the importing file on which the import starts |
| `target` | The repository-relative path that the import names, or the path as it resolves where it leaves the repository |
| `target_location` | The archive that contains the target: `repos_data.7z` where the archive of collected files contains the same file, `imported_files.7z` otherwise, blank where `target_status` is not `found` |
| `target_status` | `found`, a file of the repository that the tool reads; `missing`, no such file at `head_sha`, such as a word after an `@` that names no file, a directory, a link out of the repository or a file of a submodule; `outside`, a path that leaves the repository as written, such as an absolute path or one in the home directory; `not_read`, a file with an extension or content that the tool does not read, or behind a link that the tool does not follow, as Claude Code Review follows no link to a skill; `over_limit`, a file beyond the number of files or bytes that the tool imports for one importing file, such as a skill on the ninth skill line of a REVIEW.md, or larger than the largest file that the tool reads, such as a skill of more than 30,000 bytes; `pruned`, a path that the release leaves out of its archives, such as a session transcript or a log, which the dataset does not look up, whether or not a file is there; `not_copied`, a file that the tool reads and that the collection did not copy, because it copied the repository's imports under import heuristics that did not read the file, as when a retry of the repository failed and the pipeline kept its earlier collection. An import of a path that no file can have, longer than 4096 bytes or with a name longer than 255 bytes, is `missing`, with the import and the target cut to 200 characters |
| `target_category` | For a found target, the mechanism of the per-mechanism CSV that lists it, `skills` for a file in a skill's folder, `planning_document` for a planning document, `other` for a collected file that no mechanism covers, or `imported` for a file that only the imports reach |
| `depth` | The hop of the import: 1 for an import written in `root`, 2 for one written in a file that `root` imports, and so on |
| `target_line_count` | The number of lines of a found target. {LINE_BREAKS} |
| `github_link` | Link to the line of the import on GitHub |
| `dataset_version` | Release of the detection heuristics that produced the row |

The imports describe what a tool reads automatically in a repository at `head_sha`, not what it read in a session. A tool also reads a file outside the repository that an import names, such as one in a user's home directory, which the dataset cannot contain. Claude Code and Gemini CLI read the context files of a directory below the one in which they start only when they work there, and Copilot CLI reads the AGENTS.md and the CLAUDE.md of the directory in which it starts, while the dataset reads every such file wherever it is. A tool can read a path in another case on a file system that ignores case, which the collection on Linux does not find. Claude Code Review includes the files that a root REVIEW.md names only when it reads REVIEW.md from a protected branch. The dataset does not record whether a branch is protected, so the rows of `claude-import-07` list these files as Claude Code Review reads them from a protected branch.

### {planning_schema.CSV_NAME}

A planning document is a work product that a spec-driven development framework or a planning feature of one of the six tools writes to a documented location in the repository. Agents read it through a command or reference rather than as configuration. Examples include a Spec Kit feature's `spec.md`, an OpenSpec change proposal, and a plan that Cursor saves to the workspace. `planning_document_patterns.csv` lists each layout with its supporting page. A file counts only within a listed layout; a `spec.md` or `plan.md` elsewhere does not.

This CSV has one row per planning document in the configured repositories, covering {planned_repos:,} {by_number(planned_repos, 'repository', 'repositories')} in this release. Planning documents are not configuration artifacts, set no tool flag, and appear in no per-mechanism CSV. They do not make a repository configured, so a repository with only planning documents appears in neither this CSV nor the configured rows of `repos.csv`. A file counted by a mechanism, such as the `openspec/AGENTS.md` that earlier OpenSpec versions wrote, is a configuration artifact rather than a planning document.

| Column | Description |
|--------|-------------|
| `repo_name` | GitHub repository identifier |
| `planning_document` | Relative path to the planning document |
| `framework` | The framework or the tool that writes the file: one of the six tools (`claude`, `gemini`, `codex`, `copilot`, `cursor`, `opencode`) for a tool's own planning feature, or a framework, such as `spec-kit`, `openspec` or `conductor` |
| `document` | The name that the framework's or the tool's documentation gives the document, such as `spec`, `plan` or `tasks`, blank where it names none |
| `scanned_at` | Date on which the repository was scanned |
| `branch` | Branch where the file was found |
| `head_sha` | Commit that the repository was at when it was scanned |
| `head_commit_date` | Committer date of `head_sha`. Blank where git printed it in a form that does not parse, such as for a commit of the year 10000 |
| `dataset_version` | Release of the detection heuristics that produced the row |
| `github_link` | Link to the file on GitHub |
| `created_at` | Timestamp of the first commit introducing the file |
| `#commits` | Number of commits on `branch` touching the file, under any of its names. Commits on other branches are not counted |
| `language` | Detected natural language of a Markdown file, `n/a` for any other file and for a text without a detectable language |
| `first_commit_sha` | SHA of the first commit introducing the file |
| `last_commit_sha` | SHA of the most recent commit touching the file |
| `line_count` | Number of lines in the file, blank for a file that is not text, such as an image. {LINE_BREAKS} |

### not_configuration.csv, not_ai_commits.csv and not_planning_documents.csv

The negatives of the heuristics: what resembles a configuration artifact, a credit to a coding agent or a planning document and does not count as one, each with the reason. `not_configuration.csv` begins with `path` and `tool`, `not_ai_commits.csv` with `pattern`, and `not_planning_documents.csv` with `framework`, `layout`, `example` and `reason`. Each file continues with `source`, `doc_url`, `doc_verified`, `evidence` and `notes`.

| Column | Description |
|--------|-------------|
| `path` | The path glob that is no configuration, which applies wherever a repository keeps the tool's directory, also below the root |
| `tool` | The tool that the row concerns, `any` for a path that concerns no single tool |
| `pattern` | The text that an attribution contains when it credits automation other than a coding agent |
| `framework` | The framework or the tool of the layout |
| `layout` | The layout that is left out of the planning documents |
| `example` | A path of the layout that no row of `planning_document_patterns.csv` matches, blank where the layout is outside the repository |
| `reason` | Why the layout is left out, such as `out of scope`, `not documented` or `not distinctive` |
| `source` | `doc` where a page grounds the row, `sample` where the row was read off the collected files or the commits, and `dropped` where it was read off a repository that the collected file cleanup removed because it matched no mechanism |
| `doc_url` | The page that grounds a `doc` row |
| `doc_verified` | The date on which that page was read |
| `evidence` | Links to the files or the commits that the row was read off |
| `notes` | The reason, with the quote that grounds it |

### doc_hashes.json and doc_quotes.json

The pipeline's documentation watch (`python -m agentcontext docs` at {PIPELINE_REPOSITORY}) rechecks cited vendor pages and lists changed pages for your review before recording their new hashes. `doc_hashes.json` maps the URL of a page to the SHA-256 of its visible text, with runs of whitespace collapsed, as the watch last recorded it. `doc_quotes.json` maps the URL of a page to `sha256`, the same hash of the text in which the watch found the quotations, and `quotations`, the start of the SHA-256 of each quotation that it found there. These are the quotations in the notes of `planning_document_patterns.csv` and `not_planning_documents.csv` and the vendor quotes behind the numbers that the documents of the pipeline state. The record repeats none of the vendors' words.

### ai_commits-repos.csv

One row per repository of the collection, as the cloning step and the commit reclassification recorded it.

| Column | Description |
|--------|-------------|
| `repo_name` | GitHub repository identifier |
| `head_sha` | Commit that the repository was at when it was collected, blank for a repository that git could not clone |
| `outcome` | `history_read` where the cloning step read the commit history, `history_read_failed` where it could not read the history, `history_unavailable` where the fetch did not bring the history, `shard_unreadable` where the per-repository commit record could not be parsed, `no_files` for a repository from which the step copied no file, and otherwise why git could not clone the repository, such as `clone_failed` or `not_found_or_no_access` |
| `commits_read` | Number of commits that the step read, blank where it read no history |
| `commits_matched` | Number of commits that the step wrote into the commit CSV of the six tools, blank where it read no history. `commits.csv` contains these commits only for a repository with detected configuration. 0 where the commit reclassification left the commits out, which `commit-reclassification.json` records |
| `commits_other_tools` | The same for the commit CSV of the tools outside the six, from which `other_commits.csv` is built |
| `no_signal_shas` | Up to {commits.NO_SIGNAL_SAMPLE} SHAs of commits with no attribution at all, joined with a semicolon, as a sample for checking the commit heuristics by hand. Blank where no history was read |

### commit-reclassification.json

The record of the commit reclassification, which applies the current commit heuristics to the commit logs that the collection kept.

| Field | Description |
|-------|-------------|
| `classified_by` | Digest of the commit heuristics and the code that classified the commits, which `snapshot.json` also records |
| `collection_sha256`, `failures_sha256` | SHA-256 of the list of collected repositories and of the list of failed clones that the step read. The release contains neither list |
| `complete` | Whether the commit CSVs were assembled and no repository has one of the incomplete outcomes below |
| `population_complete` | Whether, in addition, no clone failed |
| `assembly_complete` | Whether the commit CSVs were assembled |
| `requested_repositories` | Number of repositories of the collection |
| `bounded_histories` | Number of repositories with a commit history that only a run under a larger time bound or size bound can read |
| `left_to_retry` | Number of repositories with an incomplete outcome, without those of `bounded_histories` |
| `retry_ceiling` | The number of such repositories above which the step stops the run |
| `counts` | Number of repositories per outcome |
| `repositories` | For each repository its `outcome`. `reason` gives why the collection has no commit history of the repository, `error` what failed, `bounded` marks a repository of `bounded_histories`, and `left_out` with `record_sha256` marks a repository with commits that the step left out of the commit CSVs, with the SHA-256 of its per-repository commit record |

The outcomes, with the incomplete ones after `unavailable`:

- `reclassified`: the step classified the commits of the repository again from its commit log
- `current`: the current commit heuristics had already classified the commits of the repository
- `not_applicable`: the collection copied no file from the repository
- `unavailable`: git could not clone the repository
{incomplete_outcomes}

### unmatched-paths.csv

The path globs of the collected files that neither `config_path_patterns.csv` nor `not_configuration.csv` explains and that occur in more than one repository, for a person to read for a mechanism or a negative to add. One row per path glob and place where it was found, most repositories first. `unmatched-commits.csv`, the same list for the attributions of the commits, is withheld, as the Provenance section states, and `unmatched-attributions.csv` gives its lines that name a bot or a tool.

| Column | Description |
|--------|-------------|
| `glob` | The path glob: the path from the directory of a tool on, wherever in the repository that directory is, cut after its first {unmatched.GLOB_SEGMENTS} segments, with `**` for the rest, such as `.claude/tmp/**`. A path in no directory of a tool is cut the same way from the root of the repository, and an ignore file has its file name |
| `repos` | Number of repositories with a file under the path glob |
| `files` | Number of files under the path glob |
| `found_in` | `other.csv`, the collected files of the configured repositories that no mechanism covers, or `dropped repositories`, the repositories that the collected file cleanup removed because they matched no mechanism. `{ARCHIVE_NAME}` contains no file of a dropped repository |

### unmatched-attributions.csv

The trailers that credit somebody with writing a commit and the author or committer identities that look like a bot, in the commits that no commit heuristic matched, for a person to read for a tool or a negative to add. The file keeps only the lines whose form shows that they name a bot or a tool and whose name and address name no person. An account named for a login can remain, because its form does not separate it from the account of a project. The rule also drops every line that it cannot tell from a person, so the file does not list every bot or tool, and the absence of a name says nothing about the commits. The full list of the unmatched attributions of the collection also holds the co-authors of these commits, and those lines stay in the handover of the pipeline run, the archive of its outputs, which the release does not include. One row per attribution.

| Column | Description |
|--------|-------------|
| `kind` | `trailer` for a line of a commit message, or `author` for an author or committer identity |
| `token` | The attribution in lower case: the token of a trailer with the name that it gives, such as `co-authored-by: dependabot[bot]`, or an identity with its address, cut at 80 characters |
| `commits` | Number of commits with the attribution |
| `repos` | Number of repositories with the attribution |

### deletions.csv and deletion.csv

The collected file cleanup removes what the collection copied and the dataset does not count, before the detection step reads the collected files. `deletion.csv`, in `{DELETION_ARCHIVE_NAME}`, is its deletion log, with one row for every file, empty directory and repository that it removed.

| Column | Description |
|--------|-------------|
| `path` | The removed path, as `data/validated/<owner§name>/<path>`, with `§` replacing `/` in the repository identifier |
| `reason` | Why the cleanup removed the path, such as `empty file`, `file in .github outside the Copilot paths` or `no detected mechanism, repository directory removed` |

`deletions.csv` is the report of the check of that log, with the columns `kind`, `repo_name`, `subject`, `count` and `note`. `repo_name` is the GitHub repository identifier, and `kind` decides what the other columns mean:

| `kind` | `subject` | `count` | `note` |
|--------|-----------|---------|--------|
| `reason` | A reason of `deletion.csv`, without its detail | Number of rows of that reason in the repository | Blank |
| `dropped_glob` | A path glob of a repository that the cleanup removed because it matched no mechanism | Number of files under the path glob that no row of `deletion.csv` names | The heuristics file or the CSV that names the path glob, blank where none does |
| `deleted_skill_md` | The path of a `SKILL.md` that the cleanup removed | 1 | The reason in `deletion.csv`, after `linked, ` where a symbolic link names the folder of the file, so that the collection followed the link and the cleanup removed what it copied |
| `published_and_deleted` | A removed path, blank for a whole repository | Number of rows of a per-mechanism CSV or of `planning-documents.csv` under the path, which the dataset publishes although the cleanup removed the path | The reason in `deletion.csv` |
| `repository_unrecorded`, `repository_row_still_validated`, `repository_row_never_collected` | Blank | 1 | Blank. The deletion log and the collected files disagree on the repository |
| `repository_not_copied`, `repository_not_listed`, `repository_removed_later` | Blank | 1 | Blank. The repository is absent from the files that the cleanup kept for a known reason: its clone failed, the list of collected repositories does not name it, or a step after the cleanup removed it |

## Compressed Archives

Large files are distributed as 7z archives:

| Archive | Size | Contents |
|---------|------|----------|
| `repos.csv.7z` | {repos_7z} MiB | `repos.csv` (compressed from {repos_plain} MiB) |
| `commits.csv.7z` | {commits_7z} MiB | `commits.csv` (compressed from {commits_plain} MiB) |
| `{ARCHIVE_NAME}` | {data_7z} MiB | `repos/` directory containing the collected configuration artifacts, planning documents and unclassified files for {study:,} {by_number(study, 'repository', 'repositories')} ({artifact_files:,} {by_number(artifact_files, 'file', 'files')}). Each subdirectory is named `owner§name` (with `§` replacing `/`). |
| `{IMPORTED_ARCHIVE_NAME}` | {imported_7z} MiB | `{IMPORTED_ARCHIVE_ROOT}/` directory containing the imported files that `{ARCHIVE_NAME}` does not contain, the targets with `target_location` `{IMPORTED_ARCHIVE_NAME}` in `imports.csv`, for {imported_repositories:,} {by_number(imported_repositories, 'repository', 'repositories')} ({imported:,} {by_number(imported, 'file', 'files')}), named as in `{ARCHIVE_NAME}`. |
| `{DELETION_ARCHIVE_NAME}` | {deletion_7z} MiB | `{DELETION_LOG_NAME}`, the deletion log of the collected file cleanup |

Extract `repos.csv.7z`, `commits.csv.7z`, `{IMPORTED_ARCHIVE_NAME}` and `{DELETION_ARCHIVE_NAME}` with `7z x <archive>`. `{IMPORTED_ARCHIVE_NAME}` contains no link, because the release copies the file that a link leads to. `{ARCHIVE_NAME}` can contain symbolic links with a target outside the link's directory, such as `.cursor/rules` that names `../.claude/rules`. Extract it with 7-Zip as `7z x -snld {ARCHIVE_NAME}`: without `-snld`, 7-Zip writes an empty file in place of each such link. The `7z` of p7zip keeps these links and refuses `-snld`. Extract with `7z x {ARCHIVE_NAME}` there.

Verify with: `sha256sum -c checksums.sha256`

## Usage Examples

```python
import pandas as pd
import json

# Load the main dataset. engineered_project holds lowercase labels, and pandas reads a column of
# "true" and "false" alone as booleans, so read it as text
repos = pd.read_csv("repos.csv", dtype={{"engineered_project": str}})

# The repositories with detected configuration artifacts
configured_repos = repos[repos["scanned_at"].notna()]
print(f"Repositories with configuration artifacts: {{len(configured_repos)}}")

# The repositories with configuration artifacts of Claude Code
claude_repos = repos[repos["claude"] == True]

# The engineered projects
engineered = repos[repos["engineered_project"] == "true"]

# Parse the languages JSON column
repos["languages_parsed"] = repos["languages"].apply(json.loads)

# Join the context files with the metadata of their repositories
context_files = pd.read_csv("context_files.csv")
merged = context_files.merge(repos[["repo_name", "mainLanguage", "stargazers"]], on="repo_name")
```

## License

This dataset is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), and the code of the pipeline that built it under the MIT license.

**Disclaimer:** The files and commit messages included in this dataset, such as the configuration artifacts, the planning documents and the imported files, were collected from public GitHub repositories. The content of individual files and commit messages remains subject to the license terms of their respective source repositories. The CC BY 4.0 license applies to the dataset compilation and metadata, not to the reproduced third-party content.
"""


# ---------------------------------------------------------------------------
# The step
# ---------------------------------------------------------------------------

def labels_of(rows: list[dict]) -> dict[str, int]:
    """The number of published rows with each label of `engineered_project`, in lowercase."""
    return dict(Counter(r["engineered_project"].lower() for r in rows if r["engineered_project"]))


def levels_of(rows: list[dict]) -> tuple[int, int, int, int]:
    """The provenance counts that the README states, from the published rows."""
    total = len(rows)
    annotated = sum(1 for r in rows if r["engineered_project"])
    engineered = sum(1 for r in rows if r["engineered_project"].upper() == "TRUE")
    study = sum(1 for r in rows if r["scanned_at"])
    return total, annotated, engineered, study


def remove_temporary_files(release_dir: Path) -> list[str]:
    """Delete the temporary files that an earlier run of this step left beside the files of the
    release, and return their names.

    `write_csv` and `build_commits` write `<name>.<random>.tmp` beside the file that they
    replace. They remove it after an exception, but not after a SIGKILL or a SIGTERM with its
    default handler, which a Stop during the step sends. `consistency_problems` refuses every
    entry of `release/` that is not a file of the release, and a temporary file left behind
    would make every later run fail until a person deleted it.
    """
    if not release_dir.is_dir():
        return []
    removed = []
    for name in RELEASE_NAMES:
        for stale in release_dir.glob(name + ".*.tmp"):
            stale.unlink(missing_ok=True)
            removed.append(stale.name)
    return sorted(removed)


def publish(output_dir: Path, release_dir: Path, ghs_json: Path,
            annotated_csv: Path, config_path_patterns: Path,
            retrieval_report: Path | None = None,
            imported_dir: Path = DEFAULT_IMPORTED_DIR) -> int:
    """Build `release/`. `retrieval_report` is the report of the GHS download, by default the
    file that the download writes beside the sampling frame. `imported_dir` is the imported
    tree, from which the archive of the imported files is packed.

    The step reads each commit CSV twice, before the first write for the comparisons and again
    for the write, and a commit CSV that it extracts from its archive stays in a temporary
    directory until then."""
    with tempfile.TemporaryDirectory() as work:
        return build_release(output_dir, release_dir, ghs_json, annotated_csv,
                             config_path_patterns, retrieval_report, imported_dir, Path(work))


def build_release(output_dir: Path, release_dir: Path, ghs_json: Path, annotated_csv: Path,
                  config_path_patterns: Path, retrieval_report: Path | None,
                  imported_dir: Path, work_dir: Path) -> int:
    """`publish`, with `work_dir` as the directory into which a commit CSV is extracted from its
    archive.

    Every input is read and compared before the first write, so that a refusal leaves the
    previous release as it was, with its manifest."""
    removed = remove_temporary_files(release_dir)
    if removed:
        logs.info('release', f"Removed {len(removed):,} temporary file(s) of an interrupted run "
                             f"from {release_dir} ({', '.join(removed)}).")
    problems = inventory_problems(output_dir)
    if problems:
        for problem in problems:
            logs.error('release', f"{problem}")
        return 1

    archive = release_dir / ARCHIVE_NAME
    if not archive.is_file():
        logs.error('release', f"{archive} does not exist. Run "
                              f"`python3 -m agentcontext collected-files-archive` first.")
        return 1

    sampling = sampling_provenance(ghs_json)
    download = download_report(retrieval_report or Path(ghs_json).parent / RETRIEVAL_REPORT_NAME,
                               sampling)
    classification = classification_record(annotated_csv)
    exclusions = exclusion_reasons(annotated_csv)
    collection = collection_counts(output_dir / "snapshot.json")
    imported_files_found, imported_problems = imported_sources(output_dir / "imports.csv",
                                                               imported_dir)
    problems = (consistency_problems(output_dir, release_dir, config_path_patterns)
                + imported_problems)
    deletion_log = output_dir.parent / DELETION_LOG_NAME
    if not deletion_log.is_file():
        problems.append(f"{deletion_log} does not exist, and the release publishes it as "
                        f"{DELETION_ARCHIVE_NAME}. The collected file cleanup writes it.")
    # Building the rows of repos.csv and context_files.csv compares the detection rows and the
    # labels with the sampling frame, the rows without a label with the exclusions, and the
    # columns of both inputs with the published columns. The rows are kept for the writes.
    logs.info('release', "Building repos.csv...")
    repo_rows, unlabeled, context_rows = [], {}, []
    try:
        repo_rows = build_repos(output_dir, ghs_json, annotated_csv)
        unlabeled = unlabeled_by_reason(repo_rows, exclusions)
    except ValueError as error:
        problems.append(str(error))
    logs.info('release', "Building context_files.csv...")
    try:
        context_rows = build_context_files(output_dir)
    except ValueError as error:
        problems.append(str(error))
    counts, copied_problems = copied_csv_rows(output_dir, config_path_patterns)
    problems += copied_problems
    # The full list and the commit logs are in the data root of a run, and only what the rule of
    # `unmatched reduce` keeps is published.
    logs.info('release', f"Reading {UNMATCHED_SOURCE}...")
    unmatched_csv = output_dir / UNMATCHED_SOURCE
    attributions = []
    try:
        attributions, warnings = unmatched.attributions(
            unmatched_csv, unmatched.default_commits_dir(unmatched_csv))
        for warning in warnings:
            logs.warning('release', warning)
    except (OSError, ValueError) as error:
        problems.append(f"Could not read {unmatched_csv} ({str(error).rstrip('.')})")
    # For a collection that predates `other_ai_commits.csv`, such as the collection of the March
    # sample, the release publishes `other_commits.csv` with its header alone rather than no
    # file. The release then has the same files whichever collection built it, and the README
    # states why the file is empty.
    from agentcontext import csv_archive
    source_names = {"commits.csv": "all_ai_commits.csv"}
    other_source = output_dir / commits.OTHER_AI_COMMITS_NAME
    if other_source.is_file() or csv_archive(other_source).is_file():
        source_names["other_commits.csv"] = other_source.name
    sources = {}
    for name, source_name in source_names.items():
        try:
            sources[name] = commits_source(output_dir, work_dir, source_name)
        except (FileNotFoundError, RuntimeError) as error:
            problems.append(str(error))
            continue
        patterns = resolve(COMMIT_HEURISTICS[name], output_dir, config_path_patterns)
        logs.info('release', f"Comparing {source_name} with {patterns.name}...")
        problems += commit_csv_problems(sources[name], patterns)
    if problems:
        for problem in problems:
            logs.error('release', f"{problem.rstrip('.')}.")
        return 1

    release_dir.mkdir(parents=True, exist_ok=True)
    # The manifest is removed before the first CSV is rewritten, so that an unfinished release
    # has no manifest. A run that raised part way through would otherwise leave its CSVs beside
    # the previous run's checksums, and `sha256sum -c` would report mismatches in a directory
    # that nobody edited.
    (release_dir / "checksums.sha256").unlink(missing_ok=True)

    write_csv(release_dir / "repos.csv", REPOS_COLUMN_ORDER, repo_rows)
    counts["repos.csv"] = len(repo_rows)
    levels = levels_of(repo_rows)
    # Only rows with a `head_sha` count. A row that was not scanned has neither a `head_sha` nor
    # a date, and its blank date is not one that git printed.
    blank_dates = {"repos.csv": sum(1 for row in repo_rows
                                    if row["head_sha"] and not row["head_commit_date"])}
    logs.info('release', f"Built repos.csv ({len(repo_rows):,} rows, "
                         f"{len(REPOS_COLUMN_ORDER):,} columns).")

    write_csv(release_dir / "context_files.csv", CONTEXT_FILES_COLUMN_ORDER, context_rows)
    counts["context_files.csv"] = len(context_rows)
    # The detection step leaves the AI columns blank where the AI authorship cache has no row
    # for the file at its commit, and blank is not FALSE.
    unattributed = [row["repo_name"] for row in context_rows if not row["first_commit_ai_created"]]
    blank_ai = (len(unattributed), len(set(unattributed)))
    logs.info('release', f"Built context_files.csv ({len(context_rows):,} rows, "
                         f"{len(CONTEXT_FILES_COLUMN_ORDER):,} columns).")

    logs.info('release', "Building commits.csv...")
    total, changed, blank_dates["commits.csv"] = build_commits(sources["commits.csv"],
                                                               release_dir / "commits.csv")
    counts["commits.csv"] = total
    logs.info('release', f"Built commits.csv ({total:,} rows, {changed:,} message(s) with a "
                         f"carriage return written as a newline).")

    logs.info('release', "Building other_commits.csv...")
    if "other_commits.csv" in sources:
        total, changed, blank_dates["other_commits.csv"] = build_commits(
            sources["other_commits.csv"], release_dir / "other_commits.csv")
    else:
        logs.warning('release', f"{other_source} does not exist, because the collection "
                                f"predates the pipeline that writes it. "
                                f"Publishing other_commits.csv with its header only.")
        write_csv(release_dir / "other_commits.csv", list(commits.COMMIT_COLUMNS), [])
        total = 0
    counts["other_commits.csv"] = total
    logs.info('release', f"Built other_commits.csv ({total:,} rows).")

    kept = unmatched.bots_and_tools(attributions)
    write_csv(release_dir / UNMATCHED_RELEASE_NAME, unmatched.UNMATCHED_COLUMNS_READ,
              [dict(zip(unmatched.UNMATCHED_COLUMNS_READ, entry)) for entry in kept])
    counts[UNMATCHED_RELEASE_NAME] = len(kept)
    logs.info('release', f"Built {UNMATCHED_RELEASE_NAME} with {len(kept):,} of the "
                         f"{len(attributions):,} unmatched attributions.")

    # The log message gives only the count, because the checksum manifest and the README name
    # the files.
    copied = 0
    for entry in RELEASE_FILES:
        if entry.kind != "copy":
            continue
        shutil.copyfile(resolve(entry.source, output_dir, config_path_patterns),
                        release_dir / entry.name)
        copied += 1
    logs.info('release', f"Copied {copied:,} files that are published unchanged.")

    compressed = [entry for entry in RELEASE_FILES if entry.kind == "compress"]
    logs.info('release', f"Compressing {series([entry.source for entry in compressed])}.")
    for entry in compressed:
        compress(release_dir, entry.name, entry.source)
        logs.info('release', f"Compressed {entry.name} "
                             f"({megabytes(release_dir / entry.name)} MiB).")
    imported = build_imported_archive(release_dir, imported_dir, imported_files_found)
    logs.info('release', f"Packed {imported:,} imported files into {IMPORTED_ARCHIVE_NAME} "
                         f"({megabytes(release_dir / IMPORTED_ARCHIVE_NAME)} MiB).")
    # The release contains only the archive of the log, which a reader opens for an audit. 7zz
    # stores a file that it gets by its absolute path under the bare name.
    compress(release_dir, DELETION_ARCHIVE_NAME, str(deletion_log.resolve()))
    logs.info('release', f"Compressed {DELETION_LOG_NAME} into {DELETION_ARCHIVE_NAME} "
                         f"({megabytes(release_dir / DELETION_ARCHIVE_NAME)} MiB).")

    (release_dir / "sampling.json").write_text(
        json.dumps(sampling, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")

    logs.info('release', "Writing 0_README.md and CITATION.cff.")
    version = release_version(read_json(release_dir / "provenance.json"))
    (release_dir / "CITATION.cff").write_text(render_citation(version), encoding="utf-8")
    named = named_criteria(repo_rows)
    (release_dir / "0_README.md").write_text(
        render_readme(release_dir, counts, levels, classification, sampling, collection,
                      download, blank_dates, imported, unlabeled, labels_of(repo_rows), blank_ai,
                      tuple(key for key in REMOVED_CRITERION_MEANINGS if key in named),
                      version=version),
        encoding="utf-8")

    logs.info('release', "Writing the checksum manifest checksums.sha256.")
    covered = [name for name in RELEASE_NAMES if name != "checksums.sha256"]
    (release_dir / "checksums.sha256").write_text(
        render_checksums(release_dir, covered), encoding="utf-8")

    total_bytes = sum((release_dir / name).stat().st_size for name in RELEASE_NAMES)
    logs.info('release', f"Wrote {release_dir} ({len(RELEASE_NAMES):,} files, "
                         f"{total_bytes / (1024 * 1024):.1f} MiB).")
    return 0


def main(argv=None) -> int:
    parser = cli.ArgumentParser(
        description="Build release/, the published dataset, from output/, the sampling frame, "
                    "the classification labels and the archive of collected files.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help=f"Directory of the output CSVs (default: {DEFAULT_OUTPUT_DIR})")
    parser.add_argument("--release-dir", type=Path, default=DEFAULT_RELEASE_DIR,
                        help=f"Directory to build the published dataset in (default: "
                             f"{DEFAULT_RELEASE_DIR})")
    parser.add_argument("--ghs-json", type=Path, default=DEFAULT_GHS_JSON,
                        help=f"The sampling frame (default: {DEFAULT_GHS_JSON})")
    parser.add_argument("--annotated-csv", type=Path, default=DEFAULT_ANNOTATED_CSV,
                        help=f"The classification labels (default: {DEFAULT_ANNOTATED_CSV})")
    parser.add_argument("--retrieval-report", type=Path, default=None,
                        help=f"Report of the GHS download, read for whether the download fell "
                             f"back or took a smaller retrieval with --accept-smaller (default: "
                             f"{RETRIEVAL_REPORT_NAME} beside the sampling frame)")
    parser.add_argument("--heuristics", type=Path, default=heuristics.CONFIG_PATH_PATTERNS_PATH,
                        help=f"The detection heuristics (default: {heuristics.CONFIG_PATH_PATTERNS_PATH})")
    parser.add_argument("--imported-dir", type=Path, default=DEFAULT_IMPORTED_DIR,
                        help=f"The imported tree, from which {IMPORTED_ARCHIVE_NAME} is packed "
                             f"(default: {DEFAULT_IMPORTED_DIR})")
    args = parser.parse_args(argv)

    for path in (args.output_dir, args.ghs_json, args.annotated_csv, args.heuristics,
                 args.imported_dir):
        if not path.exists():
            logs.error('release', f"{path} does not exist.")
            return 1

    try:
        return publish(args.output_dir, args.release_dir, args.ghs_json,
                       args.annotated_csv, args.heuristics, args.retrieval_report,
                       args.imported_dir)
    except OSError as e:
        # An error that the step raised itself states its file. An error of the operating
        # system names its file in `filename`.
        logs.error('release', f"{str(e).rstrip('.')}." if e.filename is None
                   else f"{e.strerror} ({e.filename}).")
        return 1
    except (ValueError, RuntimeError) as e:
        logs.error('release', f"{str(e).rstrip('.')}.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
