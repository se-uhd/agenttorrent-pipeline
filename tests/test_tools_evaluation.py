"""
Tests for agentcontext.tools.evaluation, the detection accuracy evaluation.

Strategy:
  - The Wilson interval and the stratified estimate are checked against values computed by hand
    from their formulas, not against the module's own output.
  - Each sample is drawn from a small stage directory that the test builds: mechanism CSVs,
    commit CSVs, file lists and per-repository commit records in the formats that the
    collection writes. `sample` runs over such inputs and a list of finished repositories. The
    list gives the day on which the collection started, which names the directory of the
    evaluation.
  - In a checkout, git tracks the archives of the commit CSVs. A temporary git repository with
    the archives in its index stands for one, and `sample` has to stop on a commit CSV that
    differs from its archive before it draws anything.
  - `report` runs over labels that the test writes, complete labels for exit 0 and missing or
    invalid labels for exit 1.
  - Every item that the notes of a heuristic name is looked up in the tracked evaluation of its
    day.

Requirements: Python 3.14 (stdlib only), and 7zz and git on PATH for the draw beside tracked
archives
Run with:  python3 -m unittest tests.test_tools_evaluation -v
"""

import contextlib
import csv
import hashlib
import io
import json
import lzma
import math
import random
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentcontext import SEVEN_ZIP, commits, heuristics, paths, planning_schema, resume
from agentcontext.git import clone
from agentcontext.steps import import_detection
from agentcontext.tools import evaluation as _mod


def write_csv(path: Path, rows: list, columns: list = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = columns or list(rows[0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# An item of the detection accuracy evaluation as the notes of a heuristic name it: the sample,
# the item and, after "of the evaluation of", the day on which the evaluated collection started,
# which names the directory of the evaluation.
ITEM_REFERENCE = re.compile(
    r"\b(mentions|commit_precision|commit_recall|path_precision|path_recall|import_precision"
    r"|planning_precision) ((?:mn|cp|cr|pp|pr|im|pd)-\d{4})\b"
    r"(?: of the evaluation of (\d{4}-\d{2}-\d{2}))?")
# A repository that the `evidence` of the heuristics links, which every sample leaves out.
EVIDENCE_REPOSITORY = "1hive/gardens-v2"


def item_problems(heuristics_dir: Path, evaluation_dir: Path) -> tuple:
    """(references, problems) over the notes of every CSV in `heuristics_dir`. The references are
    the item references in the notes. A problem describes a reference that names no evaluation,
    that names an item missing from the directory of its evaluation under `evaluation_dir`, or
    that belongs to a row with evidence that does not link the repository of the item or, for an
    item of `mentions`, its example commit."""
    references, problems, samples = [], [], {}
    for path in sorted(heuristics_dir.glob("*.csv")):
        for row in read_csv(path):
            evidence = row.get("evidence") or ""
            for match in ITEM_REFERENCE.finditer(row.get("notes") or ""):
                sample, item, day = match.groups()
                references.append(match.group(0))
                where = f"{path.name} {match.group(0)}"
                if day is None:
                    problems.append(f"{where}: the reference names no evaluation")
                    continue
                if (day, sample) not in samples:
                    listed = evaluation_dir / day / f"{sample}.csv"
                    samples[(day, sample)] = ({r["item"]: r for r in read_csv(listed)}
                                              if listed.is_file() else {})
                record = samples[(day, sample)].get(item)
                if record is None:
                    problems.append(f"{where}: the evaluation has no such item")
                    continue
                linked = {m.lower() for m in
                          re.findall(r"https://github\.com/([^/\s]+/[^/\s]+)/", evidence)}
                if not (record["repo_name"].lower() in linked if "repo_name" in record
                        else record["example_sha"] in evidence):
                    problems.append(f"{where}: the evidence does not link the item")
    return references, problems


class _Root(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.output = self.tmp / "output"
        self.output.mkdir()
        for mechanism, (filename, artifact) in heuristics.MECHANISM_CSVS.items():
            write_csv(self.output / filename, [],
                      ["repo_name", "tool", artifact, "github_link"])
        self.import_rows([])
        self.planning_rows([])

    def mechanism_rows(self, mechanism: str, rows: list) -> None:
        filename, artifact = heuristics.MECHANISM_CSVS[mechanism]
        write_csv(self.output / filename,
                  [{"repo_name": repo, "tool": tool, artifact: path,
                    "github_link": f"https://github.com/{repo}/blob/main/{path}"}
                   for repo, tool, path in rows],
                  ["repo_name", "tool", artifact, "github_link"])

    def import_rows(self, rows: list) -> None:
        """Write imports.csv with the columns of the import detection step, one row per
        (repository, heuristic, status, root, importing file, target, target location)."""
        written = []
        for repo, heuristic, status, root, importer, target, location in rows:
            written.append({
                "repo_name": repo, "head_sha": "a" * 40, "tool": heuristic.split("-")[0],
                "heuristic": heuristic, "root": root, "importer": importer,
                "importer_category": "context_file" if importer == root else "imported",
                "line": "1", "import": f"@{target}", "target": target, "target_status": status,
                "target_category": "imported" if status == "found" else "",
                "target_location": location, "depth": "1" if importer == root else "2",
                "github_link": f"https://github.com/{repo}/blob/main/{importer}#L1",
                "dataset_version": "2.0.0"})
        write_csv(self.output / "imports.csv", written, import_detection.COLUMNS)

    def planning_rows(self, rows: list) -> None:
        """Write planning-documents.csv with the columns of the detection step, one row per
        (repository, path, framework, document)."""
        write_csv(self.output / "planning-documents.csv",
                  [{"repo_name": repo, "head_sha": "a" * 40, "planning_document": path,
                    "framework": framework, "document": document,
                    "github_link": f"https://github.com/{repo}/tree/main/{path}"}
                   for repo, path, framework, document in rows],
                  planning_schema.csv_header())


class TestTheIntervals(unittest.TestCase):

    def test_the_wilson_interval_is_the_score_interval(self):
        # (p + z^2/2n -+ z sqrt(p(1-p)/n + z^2/4n^2)) / (1 + z^2/n), z = 1.96, by hand:
        # 10 of 10: (1 + 0.19208 - 1.96 * 0.098) / 1.38416 = 0.7225
        # 0 of 10: (0.19208 + 0.19208) / 1.38416 = 0.2775
        # 15 of 20: center (0.75 + 0.09604) / 1.19208 = 0.70972,
        # half 1.96 * sqrt(0.009375 + 0.0024010) / 1.19208 = 0.17842
        self.assertAlmostEqual(_mod.wilson(10, 10)[0], 0.7225, places=3)
        self.assertAlmostEqual(_mod.wilson(0, 10)[1], 0.2775, places=3)
        low, high = _mod.wilson(15, 20)
        self.assertAlmostEqual(low, 0.5313, places=3)
        self.assertAlmostEqual(high, 0.8881, places=3)
        self.assertEqual(_mod.wilson(0, 0), (0.0, 1.0))

    def test_the_stratified_estimate_weights_each_stratum_by_its_population(self):
        # 900 rows at 6 of 10 and 100 rows at 5 of 10: 0.9 * 0.6 + 0.1 * 0.5 = 0.59.
        estimate, low, high = _mod.stratified([(900, 6, 10), (100, 5, 10)])
        self.assertAlmostEqual(estimate, 0.59)
        # By hand, the adjusted proportions 8/14 and 7/14 over 14:
        # 0.81 * (8/14)(6/14)/14 * 890/899 + 0.01 * (7/14)(7/14)/14 * 90/99
        half = 1.959964 * math.sqrt(0.81 * (8 / 14) * (6 / 14) / 14 * 890 / 899
                                    + 0.01 * 0.25 / 14 * 90 / 99)
        self.assertAlmostEqual(low, 0.59 - half, places=5)
        self.assertAlmostEqual(high, 0.59 + half, places=5)

    def test_strata_that_are_all_correct_keep_an_interval_around_the_estimate(self):
        # Ten of ten in a stratum of 500: half = 1.96 * sqrt((12/14)(2/14)/14 * 490/499).
        estimate, low, high = _mod.stratified([(500, 10, 10)])
        self.assertEqual((estimate, high), (1.0, 1.0))
        half = 1.959964 * math.sqrt((12 / 14) * (2 / 14) / 14 * 490 / 499)
        self.assertAlmostEqual(low, 1.0 - half, places=5)

    def test_a_stratum_labeled_whole_has_no_sampling_error(self):
        self.assertEqual(_mod.stratified([(10, 7, 10)]), (0.7, 0.7, 0.7))


class TestTheTreeDigest(unittest.TestCase):
    """The file lists and the per-repository commit records are directories. The record of a
    draw names their digests, because without them a draw could not be checked against its
    inputs."""

    def test_the_digest_follows_every_files_content_and_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "o§a").mkdir()
            (root / "o§a" / "files.txt").write_text("CLAUDE.md\n")
            (root / "o§b.txt").write_text("AGENTS.md\n")
            first = _mod.tree_digest(root)
            self.assertEqual(_mod.tree_digest(root), first)
            (root / "o§b.txt").write_text("GEMINI.md\n")
            changed = _mod.tree_digest(root)
            self.assertNotEqual(changed, first)
            (root / "o§b.txt").rename(root / "o§c.txt")
            self.assertNotEqual(_mod.tree_digest(root), changed)


class TestTheEvidenceRepositories(unittest.TestCase):

    def test_every_repository_that_an_evidence_link_names_is_left_out(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        write_csv(tmp / "a.csv", [
            {"pattern": "x", "evidence": "https://github.com/Owner/Repo/commit/abc "
                                        "https://github.com/apps/copilot-swe-agent"},
            {"pattern": "y", "evidence": "https://github.com/o/r/blob/main/CLAUDE.md"}])
        write_csv(tmp / "b.csv", [{"path": "z", "evidence": ""}])
        self.assertEqual(_mod.evidence_repositories(tmp), {"owner/repo", "o/r"})

    def test_the_real_heuristics_name_their_evidence(self):
        found = _mod.evidence_repositories(heuristics.HEURISTICS_DIR)
        self.assertIn(EVIDENCE_REPOSITORY, found)
        self.assertNotIn("apps/copilot-pull-request-reviewer", found)

    def test_a_heuristic_read_off_an_evaluation_item_links_the_item(self):
        """A row with notes that name an item of the detection accuracy evaluation, such as
        `mentions mn-0057 of the evaluation of 2026-10-05`, was read off that item or changed
        because of it. The next draw leaves the repository of the item out only when the evidence
        of the row links the item, that is, the repository of the item or, for a phrasing of
        `mentions`, its example commit."""
        limit = csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, limit)
        csv.field_size_limit(1 << 30)
        cited, problems = item_problems(heuristics.HEURISTICS_DIR, _mod.EVALUATION_DIR)
        self.assertTrue(cited)
        self.assertEqual(problems, [])

    def test_an_item_reference_resolves_in_the_directory_of_its_day(self):
        """Every draw numbers its items from 1, and `cp-0001` names another commit in each
        evaluation. A reference without the day of its evaluation therefore names no item."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        evaluations = tmp / "evaluation"
        write_csv(evaluations / "2026-10-05" / "commit_precision.csv",
                  [{"item": "cp-0001", "repo_name": "o/october"}])
        write_csv(evaluations / "2026-10-26" / "commit_precision.csv",
                  [{"item": "cp-0001", "repo_name": "o/later"}])
        october = "https://github.com/o/october/commit/abc"
        write_csv(tmp / "heuristics" / "patterns.csv", [
            {"id": "a-01", "evidence": october,
             "notes": "Read off commit_precision cp-0001 of the evaluation of 2026-10-05."},
            {"id": "a-02", "evidence": october,
             "notes": "Read off commit_precision cp-0001 of the evaluation of 2026-10-26."},
            {"id": "a-03", "evidence": october, "notes": "Read off commit_precision cp-0001."},
            {"id": "a-04", "evidence": october,
             "notes": "Read off commit_precision cp-0001 of the evaluation of 2026-09-01."}])
        cited, problems = item_problems(tmp / "heuristics", evaluations)
        self.assertEqual(len(cited), 4)
        self.assertEqual(problems, [
            "patterns.csv commit_precision cp-0001 of the evaluation of 2026-10-26: the "
            "evidence does not link the item",
            "patterns.csv commit_precision cp-0001: the reference names no evaluation",
            "patterns.csv commit_precision cp-0001 of the evaluation of 2026-09-01: the "
            "evaluation has no such item"])


# The phrasings in the tracked samples of mentions that the final labels mark as an attribution
# but that no commit heuristic reads for the tool that the phrasing names. The key of each entry
# is the day of the evaluation and the item, and the value is the reason why no heuristic reads
# the phrasing.
UNREAD_ATTRIBUTIONS = {
    ("2026-10-05", "mn-0171"):
        "raycast/extensions be792fa7: a list of commit subjects joined Cursor's trailer "
        "Made-with: Cursor to the end of a listed line, which does not have the form of a trailer. "
        "No commit of the commit CSV has the trailer in its first paragraph, which git treats as "
        "the subject, cursor-full-body-08 already credits Cursor in this commit, and the trailer "
        "read anywhere in a line would also read prose that quotes it, as flightctl/flightctl "
        "6119c6b0 does (the notes of cursor-trailer-04).",
    ("2026-10-26", "mn-0064"):
        "NextCapital/copy-service 3aecf5f2: the prefix (COPILOT), with which one developer "
        "marks his commits, and which no vendor page and no file of the repository explains. A "
        "tool's name in parentheses or brackets at the start of a subject also names the area of "
        "a change, as (Copilot) of confluentinc/vscode f032c3a1 names the extension's chat "
        "tools, and only the capitals tell the two apart, which the heuristics do not read "
        "(docs/commits.md, The labeled attributions decided on 2026-11-02).",
    ("2026-10-26", "mn-0086"):
        "xmlui-org/xmlui 866ef167: a form loose enough to read 'creating ... with Copilot' in "
        "this subject also reads subjects that describe a feature that uses Copilot, such as "
        "'Update workflows to auto-generate changelogs with GitHub Copilot' (docs/commits.md, "
        "The detection accuracy evaluation of 2026-10-26).",
    ("2026-11-04", "mn-0098"):
        "tursodatabase/turso 82d48e71fb64: the merge copies the description of the pull request, "
        "'Initial pass with Codex than me just reviewing everything line by line', in which Codex "
        "made the first version of the change. A pass with a tool also names a pass that makes no "
        "part of the change, as 'we did a pass with Claude over public Slack channels to find "
        "conversations' of Recidiviz/pulse-data e16f5b7c92ad, in which Claude searched the "
        "channels (docs/commits.md, The labeled attributions of the evaluation of 2026-11-04, "
        "decided on 2026-11-08).",
    ("2026-11-04", "mn-0109"):
        "wekan/wekan 68e0032c6d3b: the maintainer ends a fix with 'Thanks to (a person), "
        "(a person) and Claude !'. A thank-you credits no tool, because it states whom the writer "
        "thanks and not what the tool made (docs/commits.md, The credits of the final review of "
        "2026-11-05), and the notes of claude-full-body-52 leave 'Thanks to Claude' unread.",
    ("2026-11-04", "mn-0187"):
        "pelicanmapping/osgearth 6d7e5e3b93b9: the tag (codex) at the end of the subject "
        "'ElevationPool performance updates (codex).', which no vendor page names. A tool's name "
        "in parentheses at the end of a subject also names the area of a change or the reviewer "
        "whose finding it addresses: 'feat(ai): OpenAI models via a ChatGPT subscription (Codex)' "
        "of frappe/builder e145d48c5672 adds Codex's backend, and 'bot settings: redact stored "
        "API keys in safeKeySummary (Codex)' of tloncorp/tlon-apps c214aaee2f2d addresses Codex's "
        "review in a commit that Claude co-authored. The decision on the prefix (COPILOT) rests "
        "on the same reason at the start of a subject (docs/commits.md, The labeled attributions "
        "decided on 2026-11-02).",
}


class TestTheLabeledMentions(unittest.TestCase):
    """The labels of a tracked sample of mentions against the current commit heuristics."""

    def test_every_attribution_is_read_or_listed_with_its_reason(self):
        """A recount of the attributions that no heuristic reads gives the items of
        `UNREAD_ATTRIBUTIONS`. A new label of an unread attribution therefore needs a heuristic
        or an entry with its reason, and an item that a new heuristic reads has to be removed
        from the list."""
        unread, evaluations = set(), []
        for directory in sorted(p for p in _mod.EVALUATION_DIR.iterdir()
                                if (p / "mentions.csv").is_file()):
            evaluations.append(directory.name)
            items = read_csv(directory / "mentions.csv")
            labels, problems = _mod.read_labels(directory, "mentions")
            self.assertEqual(problems, [])
            resolutions_path = directory / "labels" / "resolutions.csv"
            resolutions = ({row["item"]: row for row in read_csv(resolutions_path)}
                           if resolutions_path.is_file() else {})
            final, _review = _mod.final_labels(items, labels, resolutions)
            for row in items:
                if final[row["item"]]["label"] != "attribution":
                    continue
                if row["tool"].lower() not in {t.lower() for t in _mod.read_now(row["line"])}:
                    unread.add((directory.name, row["item"]))
        self.assertIn("2026-10-26", evaluations)
        self.assertEqual(sorted(unread), sorted(UNREAD_ATTRIBUTIONS))


class TestTheTrackedSamplesFollowTheMasking(unittest.TestCase):
    """The masking leaves every cell of a tracked sample of commit recall and of mentions as it is,
    so that a rule that marks more fails here until the samples are masked again from their
    texts."""

    def test_the_masking_changes_no_cell_of_a_tracked_sample(self):
        changed, evaluations = [], []
        for directory in sorted(p for p in _mod.EVALUATION_DIR.iterdir()
                                if (p / "commit_recall.csv").is_file()):
            evaluations.append(directory.name)
            for row in read_csv(directory / "commit_recall.csv"):
                for column in ("author", "committer"):
                    if _mod.identity_without_person(row[column]) != row[column]:
                        changed.append((directory.name, row["item"], column))
                if _mod.line_without_people(row["message"]) != row["message"]:
                    changed.append((directory.name, row["item"], "message"))
            for row in read_csv(directory / "mentions.csv"):
                for column in ("line", "pattern"):
                    if _mod.line_without_people(row[column]) != row[column]:
                        changed.append((directory.name, row["item"], column))
        self.assertIn("2026-10-26", evaluations)
        self.assertEqual(changed, [])


class TestPathPrecision(_Root):

    def test_up_to_ten_rows_a_cell_one_per_repository_first_and_none_of_the_evidence(self):
        rows = [(f"o/r{i % 4}", "claude", f".claude/skills/s{i}") for i in range(12)]
        rows += [("evidence/repo", "claude", ".claude/skills/e")]
        rows += [("o/x", "any", "AGENTS.md")]
        self.mechanism_rows("skills", rows[:13])
        self.mechanism_rows("context_file", rows[13:])
        items = _mod.sample_path_precision(self.output, random.Random(1), {"evidence/repo"})
        skills = [r for r in items if r["mechanism"] == "skills"]
        self.assertEqual(len(skills), 10)
        self.assertEqual({r["repo_name"] for r in skills[:4]}, {"o/r0", "o/r1", "o/r2", "o/r3"})
        self.assertNotIn("evidence/repo", {r["repo_name"] for r in items})
        self.assertEqual({r["cell_rows"] for r in skills}, {13})
        context = [r for r in items if r["mechanism"] == "context_file"]
        self.assertEqual([(r["tool"], r["local_path"]) for r in context],
                         [("any", "data/validated/o§x/AGENTS.md")])
        self.assertEqual([r["item"] for r in items], [f"pp-{n:04d}" for n in range(1, 12)])

    def test_the_same_seed_draws_the_same_rows(self):
        self.mechanism_rows("skills", [(f"o/r{i}", "claude", f"s{i}") for i in range(30)])
        first = _mod.sample_path_precision(self.output, random.Random(7), set())
        second = _mod.sample_path_precision(self.output, random.Random(7), set())
        self.assertEqual(first, second)


class TestImportPrecision(_Root):
    """The sample of imports.csv has a cell for each import heuristic and target status."""

    def setUp(self):
        super().setUp()
        # Twelve found imports of CLAUDE.md files in four repositories and one in a repository of
        # the evidence, two missing imports, an outside import of an imported AGENTS.md, and a
        # cell with a row in the repository of the evidence alone.
        rows = [(f"o/r{i % 4}", "claude-import-01", "found", "CLAUDE.md", "CLAUDE.md",
                 f"docs/d{i}.md", "repos_data.7z") for i in range(12)]
        rows += [("evidence/repo", "claude-import-01", "found", "CLAUDE.md", "CLAUDE.md",
                  "docs/e.md", "repos_data.7z"),
                 ("o/m", "claude-import-01", "missing", "CLAUDE.md", "CLAUDE.md", "gone.md", ""),
                 ("o/m", "claude-import-01", "missing", "CLAUDE.md", "CLAUDE.md", "lost.md", ""),
                 ("o/i", "copilot-import-02", "outside", "CLAUDE.md", "AGENTS.md", "../x.md", ""),
                 ("o/i", "copilot-import-03", "found", "CLAUDE.md", "AGENTS.md", "notes.md",
                  "imported_files.7z"),
                 ("evidence/repo", "gemini-import-01", "found", "GEMINI.md", "GEMINI.md",
                  "style.md", "repos_data.7z")]
        self.import_rows(rows)

    def test_up_to_seven_rows_a_cell_and_the_evidence_only_where_a_cell_has_no_other_row(self):
        """A cell with rows outside the repositories of the evidence is drawn from those rows
        alone. The cell with its only row in a repository of the evidence is drawn from that row,
        and its item is marked."""
        items, from_evidence = _mod.sample_import_precision(self.output, random.Random(1),
                                                            {"evidence/repo"})
        cells = {}
        for row in items:
            cells.setdefault(row["stratum"], []).append(row)
        self.assertEqual({k: len(v) for k, v in cells.items()},
                         {"claude-import-01/found": 7, "claude-import-01/missing": 2,
                          "copilot-import-02/outside": 1, "copilot-import-03/found": 1,
                          "gemini-import-01/found": 1})
        found = cells["claude-import-01/found"]
        self.assertEqual({r["repo_name"] for r in found[:4]}, {"o/r0", "o/r1", "o/r2", "o/r3"})
        self.assertEqual({(r["repo_name"], r["from_evidence"]) for r in items
                          if r["repo_name"] == "evidence/repo" or r["from_evidence"]},
                         {("evidence/repo", "yes")})
        self.assertEqual([(r["target"], r["from_evidence"])
                          for r in cells["gemini-import-01/found"]], [("style.md", "yes")])
        # The weight of a cell counts the rows in the repositories of the evidence too.
        self.assertEqual({r["stratum_rows"] for r in found}, {13})
        self.assertEqual(from_evidence, {"gemini-import-01/found": 1})
        self.assertEqual([r["item"] for r in items], [f"im-{n:04d}" for n in range(1, 13)])

    def test_an_item_names_the_importing_file_and_the_target_in_their_trees(self):
        """An importing file at the start of a chain is in the validated tree, and an imported
        importing file in the imported tree. A found target is in the tree of the archive that
        publishes it, and a target that the collection does not hold has no local path."""
        items, _ = _mod.sample_import_precision(self.output, random.Random(1), set())
        by_target = {row["target"]: row for row in items}
        self.assertEqual((by_target["docs/e.md"]["importer_local_path"],
                          by_target["docs/e.md"]["target_local_path"]),
                         ("data/validated/evidence§repo/CLAUDE.md",
                          "data/validated/evidence§repo/docs/e.md"))
        self.assertEqual((by_target["notes.md"]["importer_local_path"],
                          by_target["notes.md"]["target_local_path"]),
                         ("data/imported/o§i/AGENTS.md", "data/imported/o§i/notes.md"))
        self.assertEqual(by_target["gone.md"]["target_local_path"], "")
        self.assertEqual(by_target["../x.md"]["target_local_path"], "")

    def test_the_sample_draws_with_a_generator_of_its_own(self):
        """`sample` draws the imports with the generator `<seed>:im`, so that adding another
        sample to the draw changes none of its items."""
        drawn, _ = _mod.sample_output_csvs(["import_precision"], self.output, 5, set())
        expected, _ = _mod.sample_import_precision(self.output, random.Random("5:im"), set())
        self.assertEqual(drawn["import_precision"], expected)
        other, _ = _mod.sample_import_precision(self.output, random.Random("5:pd"), set())
        self.assertNotEqual([r["target"] for r in other], [r["target"] for r in expected])


class TestPlanningPrecision(_Root):
    """The sample of planning-documents.csv has a stratum for each framework or tool."""

    # The repositories that the evidence links in these tests.
    EVIDENCE = {"evidence/repo", "evidence/other"}

    def setUp(self):
        super().setUp()
        rows = [(f"o/s{i % 5}", f"docs/superpowers/plans/p{i}.md", "superpowers", "plan")
                for i in range(25)]
        rows += [("evidence/repo", "docs/superpowers/plans/e.md", "superpowers", "plan")]
        rows += [("o/os", f"openspec/changes/c{i}/proposal.md", "openspec", "proposal")
                 for i in range(3)]
        rows += [(f"evidence/{('repo', 'other')[i % 2]}", f"backlog/tasks/task-{i}.md",
                  "backlog-md", "task") for i in range(25)]
        self.planning_rows(rows)

    def strata(self, items: list) -> dict:
        """{stratum: its items in the order of the draw}."""
        strata = {}
        for row in items:
            strata.setdefault(row["stratum"], []).append(row)
        return strata

    def test_up_to_twenty_rows_a_framework_one_per_repository_first(self):
        """A framework with rows outside the repositories of the evidence is drawn from those
        rows alone, and its items are not marked."""
        items, _ = _mod.sample_planning_precision(self.output, random.Random(1), self.EVIDENCE)
        strata = self.strata(items)
        self.assertEqual({k: len(v) for k, v in strata.items()},
                         {"backlog-md": 20, "openspec": 3, "superpowers": 20})
        self.assertEqual({r["repo_name"] for r in strata["superpowers"][:5]},
                         {f"o/s{i}" for i in range(5)})
        others = strata["superpowers"] + strata["openspec"]
        self.assertEqual({r["repo_name"] for r in others} & self.EVIDENCE, set())
        self.assertEqual({r["from_evidence"] for r in others}, {""})
        # The weight of a framework counts the rows in the repositories of the evidence too.
        self.assertEqual({r["stratum_rows"] for r in strata["superpowers"]}, {26})
        first = strata["openspec"][0]
        self.assertEqual(first["local_path"], f"data/validated/o§os/{first['planning_document']}")
        self.assertEqual([r["item"] for r in items], [f"pd-{n:04d}" for n in range(1, 44)])

    def test_a_framework_with_every_row_in_a_repository_of_the_evidence_is_drawn_from_them(self):
        """Backlog.md has rows in the repositories of the evidence alone. Its items come from
        these rows, with the size and the generator of every framework, so that they are the
        items of a draw that leaves out no repository. Each of them is marked, and the draw
        names the framework with its rows."""
        items, from_evidence = _mod.sample_planning_precision(self.output, random.Random(1),
                                                              self.EVIDENCE)
        drawn = self.strata(items)["backlog-md"]
        self.assertEqual(from_evidence, {"backlog-md": 25})
        self.assertEqual({(r["from_evidence"], r["stratum_rows"]) for r in drawn}, {("yes", 25)})
        self.assertEqual({r["repo_name"] for r in drawn[:2]}, self.EVIDENCE)
        unrestricted, none = _mod.sample_planning_precision(self.output, random.Random(1), set())
        self.assertEqual(none, {})
        self.assertEqual([r["planning_document"] for r in drawn],
                         [r["planning_document"] for r in self.strata(unrestricted)["backlog-md"]])

    def test_the_sample_draws_with_a_generator_of_its_own(self):
        drawn, _ = _mod.sample_output_csvs(["planning_precision"], self.output, 5, set())
        expected, _ = _mod.sample_planning_precision(self.output, random.Random("5:pd"), set())
        self.assertEqual(drawn["planning_precision"], expected)
        other, _ = _mod.sample_planning_precision(self.output, random.Random("5:im"), set())
        self.assertNotEqual([r["planning_document"] for r in other],
                            [r["planning_document"] for r in expected])


class TestPathRecall(_Root):

    def test_the_net_catches_the_tools_and_their_shared_files_and_skips_vendored_code(self):
        entries = [("100644", p) for p in (
            "CLAUDE.md", ".github/copilot-instructions.md", "docs/AGENTS.md", ".agents/skills/x/SKILL.md",
            "src/gemini_client.py", ".vscode/mcp.json", "node_modules/claude/index.js",
            "README.md", "src/agent.py", ".github/prompts/fix.prompt.md",
            ".opencode/agents/review.md", "opencode.json")]
        pattern = _mod.candidate_pattern(heuristics.load_config_path_patterns())
        self.assertEqual(_mod.candidates(entries, {"node_modules"}, pattern), [
            "CLAUDE.md", ".github/copilot-instructions.md", "docs/AGENTS.md",
            ".agents/skills/x/SKILL.md", "src/gemini_client.py", ".vscode/mcp.json",
            ".github/prompts/fix.prompt.md", ".opencode/agents/review.md", "opencode.json"])

    def test_a_path_that_a_path_heuristic_names_is_a_candidate_whatever_its_name(self):
        """A file at a place that a row of config_path_patterns.csv names is a candidate even
        when its name mentions no tool. Outside those places, files with generic names, such as
        the parts and the manifest of a plugin, the files in `.github` and the CI workflows, are
        not candidates."""
        named = ["REVIEW.md", ".worktreeinclude", "tui.json", "tui.jsonc", "AGENTS.override.md",
                 ".github/lsp.json", ".github/allowed_models.txt", ".github/github-app.yml",
                 ".plugin/plugin.json", ".plugin/marketplace.json", ".plugin/assets/logo.png",
                 ".github/plugin/plugin.json", ".github/plugin/marketplace.json",
                 "_agents/rules/style.md", "_agent/workflows/deploy.md",
                 ".github/workflows/triage.md", ".github/skills/release/notes.txt"]
        generic = ["README.md", "LICENSE", "package.json", "packages/ui/plugin.json",
                   "marketplace.json", "skills/release/run.py", "plugins/lint/hooks/hooks.json",
                   ".github/workflows/ci.yml", ".github/ISSUE_TEMPLATE/bug.md",
                   ".github/dependabot.yml", "src/agent.py"]
        pattern = _mod.candidate_pattern(heuristics.load_config_path_patterns())
        found = _mod.candidates([("100644", p) for p in named + generic], set(), pattern)
        self.assertEqual(found, named)

    def test_the_candidate_places_are_read_off_the_rows(self):
        """The path of a row is a place where its match mode reads a path by name. A row that
        reads a generic name only by its content or beside a manifest names no place."""
        def row(tool, mechanism, path, match_mode):
            return {"tool": tool, "mechanism": mechanism, "path": path, "match_mode": match_mode}
        rows = [row("codex", "settings", ".foo/config.toml", "file"),
                row("claude", "skills", "<claude-plugin>/skills", "skill_dir_flat"),
                row("claude", "collect_only", "README.md", "plugin_sibling"),
                row("any", "plugins", "plugin.json", "basename_json_plugin_manifest"),
                row("copilot", "plugins", ".bar/plugin.json", "file"),
                row("copilot", "other", ".github", "unclassified_dir"),
                row("copilot", "collect_only", ".github", "copy_dir_gated"),
                row("claude", "settings", ".github/workflows", "workflow_uses:o/action"),
                row("claude", "settings", ".github/workflows", "workflow_command:tool -p"),
                row("copilot", "settings", ".github/workflows", "workflow_engine:copilot")]
        self.assertEqual(_mod.candidate_places(rows),
                         [".bar/plugin.json", ".foo/config.toml", ".github/workflows/*.md"])
        pattern = _mod.candidate_pattern(rows)
        paths = ["pkg/.foo/config.toml", ".FOO/Config.toml", ".bar/plugin.json",
                 ".github/workflows/triage.md", "skills/a/run.py", "README.md", "plugin.json",
                 ".github/CODEOWNERS", ".github/workflows/ci.yml", ".github/workflows/a/b.md"]
        self.assertEqual(_mod.candidates([("100644", p) for p in paths], set(), pattern),
                         paths[:4])

    def test_a_path_under_a_detected_directory_is_detected(self):
        artifacts = {"skills": {".claude/skills/release"}, "context_file": {"CLAUDE.md"}}
        self.assertEqual(_mod.detected(".claude/skills/release/SKILL.md", artifacts), "yes")
        self.assertEqual(_mod.detected("CLAUDE.md", artifacts), "yes")
        self.assertEqual(_mod.detected(".claude/skills/release-notes/SKILL.md", artifacts), "")

    def test_a_path_that_only_other_csv_lists_is_marked_other(self):
        """other.csv lists the files of a configuration directory that no mechanism covers. The
        detection gave such a path no mechanism, so a path that only other.csv lists is marked
        `other`, and a path that the CSV of a mechanism lists as well is marked `yes`."""
        artifacts = {"other": {".claude/notes.txt", ".claude/settings.json"},
                     "settings": {".claude/settings.json"}}
        self.assertEqual(_mod.detected(".claude/notes.txt", artifacts), "other")
        self.assertEqual(_mod.detected(".claude/settings.json", artifacts), "yes")
        self.assertEqual(_mod.detected(".claude/other.txt", artifacts), "")

    def test_one_item_per_repository_with_its_candidates(self):
        lists = self.tmp / "file_lists"
        lists.mkdir()
        files = {"o/none": ["README.md"], "o/missed": ["README.md", "CLAUDE.md"],
                 "o/found": ["CLAUDE.md", ".cursor/rules/a.mdc", ".claude/notes.txt"]}
        for repo, paths in files.items():
            raw = b"".join(f"100644\t{p}".encode() + b"\0" for p in paths)
            (lists / f"{repo.replace('/', '§')}{clone.FILE_LIST_SUFFIX}").write_bytes(lzma.compress(raw))
        write_csv(self.output / "ai_commits-repos.csv", [
            {"repo_name": "o/none", "outcome": "no_files"},
            {"repo_name": "o/missed", "outcome": "no_files"},
            {"repo_name": "o/found", "outcome": "history_read"}])
        self.mechanism_rows("context_file", [("o/found", "claude", "CLAUDE.md")])
        self.mechanism_rows("other", [("o/found", "claude", ".claude/notes.txt")])
        items, paths = _mod.sample_path_recall(self.output, lists, random.Random(3), set())
        by_repo = {r["repo_name"]: r for r in items}
        self.assertEqual({r: by_repo[r]["stratum"] for r in by_repo},
                         {"o/none": "unconfigured", "o/missed": "unconfigured", "o/found": "configured"})
        self.assertEqual(by_repo["o/none"]["candidates_listed"], 0)
        listed = {(next(r["repo_name"] for r in items if r["item"] == p["item"]), p["path"]): p["detected"]
                  for p in paths}
        self.assertEqual(listed, {("o/missed", "CLAUDE.md"): "", ("o/found", "CLAUDE.md"): "yes",
                                  ("o/found", ".cursor/rules/a.mdc"): "",
                                  ("o/found", ".claude/notes.txt"): "other"})

    def test_a_repository_with_every_collected_file_removed_by_the_cleanup_is_without_configuration(self):
        """The cloning step read the commit history of a repository with collected files, and
        the cleanup removed them all because the detection found no mechanism. No output CSV
        lists the repository, and its stratum is the one without detected configuration. A
        clone failure has no file list and is in neither stratum."""
        lists = self.tmp / "file_lists"
        lists.mkdir()
        files = {"o/rejected": ["README.md", ".claude/agents/apollo.md"],
                 "o/empty": ["README.md"], "o/found": ["CLAUDE.md"]}
        for repo, paths in files.items():
            raw = b"".join(f"100644\t{p}".encode() + b"\0" for p in paths)
            (lists / f"{repo.replace('/', '§')}{clone.FILE_LIST_SUFFIX}").write_bytes(lzma.compress(raw))
        write_csv(self.output / "ai_commits-repos.csv", [
            {"repo_name": "o/rejected", "outcome": "history_read"},
            {"repo_name": "o/empty", "outcome": "no_files"},
            {"repo_name": "o/found", "outcome": "history_read"},
            {"repo_name": "o/gone", "outcome": clone.NOT_FOUND_OR_NO_ACCESS}])
        self.mechanism_rows("context_file", [("o/found", "claude", "CLAUDE.md")])
        items, paths = _mod.sample_path_recall(self.output, lists, random.Random(3), set())
        by_repo = {r["repo_name"]: r for r in items}
        self.assertEqual({r: by_repo[r]["stratum"] for r in by_repo},
                         {"o/rejected": "unconfigured", "o/empty": "unconfigured",
                          "o/found": "configured"})
        self.assertEqual(by_repo["o/rejected"]["repositories_in_stratum"], 2)
        self.assertEqual([(p["path"], p["detected"]) for p in paths
                          if p["item"] == by_repo["o/rejected"]["item"]],
                         [(".claude/agents/apollo.md", "")])


def _nul_log(records: list) -> bytes:
    return lzma.compress(b"".join(b"\0" + "\0".join([
        sha, author, f"{author}@example.com", "Dev", "dev@example.com", "2026-10-01T00:00:00Z",
        subject, body, subject + ("\n\n" + body if body else ""), ""]).encode() + b"\n"
        for sha, author, subject, body in records))


class TestCommitRecall(_Root):

    def test_every_commit_read_is_drawn_once_with_what_the_heuristics_find(self):
        logs_dir = self.tmp / "collect_commits"
        logs_dir.mkdir()
        repos = {"o/a": [("a" * 40, "Dev", "fix", "Co-Authored-By: Claude <noreply@anthropic.com>"),
                         ("b" * 40, "Dev", "docs", "")],
                 "o/b": [("c" * 40, "copilot-swe-agent[bot]", "feat", ""),
                         ("d" * 40, "Dev", "chore", ""), ("e" * 40, "Dev", "test", "")]}
        for repo, records in repos.items():
            (logs_dir / f"{repo.replace('/', '§')}.log.xz").write_bytes(_nul_log(records))
        write_csv(self.output / "ai_commits-repos.csv", [
            {"repo_name": repo, "outcome": "history_read", "commits_read": len(records)}
            for repo, records in repos.items()] + [
            {"repo_name": "o/c", "outcome": "no_files", "commits_read": ""}])
        with mock.patch.object(_mod, "COMMIT_RECALL_COMMITS", 10):
            items = _mod.sample_commit_recall(self.output, logs_dir, random.Random(5), set())
        self.assertEqual(sorted(r["commit_sha"] for r in items),
                         [c * 40 for c in "abcde"])
        found = {r["commit_sha"][0]: r["detected"] for r in items}
        self.assertEqual(found, {"a": "Claude", "b": "", "c": "Copilot", "d": "", "e": ""})
        self.assertEqual({r["commits_read"] for r in items}, {5})

    def test_a_draw_selects_the_commit_at_its_position(self):
        # The draw selects two of five commits by position, which makes it uniform over commits.
        logs_dir = self.tmp / "collect_commits"
        logs_dir.mkdir()
        (logs_dir / "o§a.log.xz").write_bytes(_nul_log([(c * 40, "Dev", "x", "") for c in "abcde"]))
        write_csv(self.output / "ai_commits-repos.csv",
                  [{"repo_name": "o/a", "outcome": "history_read", "commits_read": 5}])
        rng = random.Random(11)
        expected = sorted(random.Random(11).sample(range(5), 2))
        with mock.patch.object(_mod, "COMMIT_RECALL_COMMITS", 2):
            items = _mod.sample_commit_recall(self.output, logs_dir, rng, set())
        self.assertEqual([r["commit_sha"] for r in items], ["abcde"[i] * 40 for i in expected])


class TestTheTrackedSamplesNameNoPerson(unittest.TestCase):
    """The tracked samples keep no identity with an address, no GitHub mention and no profile
    link of a person, and the link of each commit stays as its evidence."""

    ITEM = {"item": "cr-0001", "repo_name": "o/a", "commit_sha": "a" * 40, "github_link": "link",
            "author": "Jane Doe <jane.doe@example.org>",
            "committer": "dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>",
            "message": "fix\n\nSigned-off-by: Jane Doe <jane.doe@example.org>",
            "detected": "", "detected_other": "", "commits_read": 5}

    def test_a_person_is_marked_and_a_bot_is_kept(self):
        kept = _mod.without_people(self.ITEM)
        self.assertEqual(kept["author"], _mod.PERSON_MARK)
        self.assertEqual(kept["committer"], self.ITEM["committer"])
        self.assertEqual(kept["github_link"], "link")

    def test_github_as_the_committer_of_a_web_commit_is_kept(self):
        self.assertEqual(_mod.identity_without_person("GitHub <noreply@github.com>"),
                         "GitHub <noreply@github.com>")

    def test_a_person_with_a_bot_address_is_marked(self):
        for identity in ("jane doe <bot@example.org>", "release bot <jane.doe+bot@example.org>",
                         "jane.bot <jane.bot@example.org>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), _mod.PERSON_MARK)

    def test_the_bot_users_of_the_copilot_apps_stay_under_the_login_and_the_name_of_the_app(self):
        """GitHub's API records the accounts 198982749, 223556219 and 175728472 under the login
        Copilot with the type Bot, as the apps copilot-swe-agent, github-copilot-cli and
        copilot-pull-request-reviewer, and copilot-author-05 reads their noreply addresses. The
        login has no mark of a bot, and the identity stays under the login and under the name of
        the app that copilot-author-02 reads."""
        for identity in ("Copilot <198982749+Copilot@users.noreply.github.com>",
                         "copilot-swe-agent[bot] <198982749+Copilot@users.noreply.github.com>",
                         "Copilot <223556219+Copilot@users.noreply.github.com>",
                         "Copilot <175728472+Copilot@users.noreply.github.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), identity)

    def test_the_agent_account_that_goose_author_01_reads_stays_under_its_name(self):
        """GitHub's API records the account 215356755 as a User with the login freepilot-bot and
        the name Freepilot, the agent account of Freepilot that goose-author-01 reads. The name has
        the words of the login without the word bot."""
        identity = "Freepilot <215356755+freepilot-bot@users.noreply.github.com>"
        self.assertEqual(_mod.identity_without_person(identity), identity)

    def test_another_name_at_the_address_of_such_an_app_is_marked_and_the_address_kept(self):
        self.assertEqual(
            _mod.identity_without_person("Jane Doe <198982749+Copilot@users.noreply.github.com>"),
            f"{_mod.PERSON_MARK} <198982749+Copilot@users.noreply.github.com>")

    def test_a_persons_account_with_the_login_copilot_in_its_address_stays_marked(self):
        """GitHub's API records the account 292387 as a User, and copilot-author-05 leaves out its
        noreply address 292387+copilot@users.noreply.github.com."""
        self.assertEqual(
            _mod.identity_without_person("Copilot <292387+copilot@users.noreply.github.com>"),
            _mod.PERSON_MARK)

    def test_a_tool_at_an_address_that_an_account_heuristic_reads_is_kept(self):
        """cursor-author-04 reads cursoragent@cursor.com and claude-author-04
        noreply@anthropic.com, and each name begins with the tool of that heuristic. FAIMS/FAIMS3
        329efe28f31d, cr-0050 of the evaluation of 2026-10-26, and analogjs/analog 5c9a997ffcc5
        have the first two identities as author and committer."""
        for identity in ("Cursor Agent <cursoragent@cursor.com>", "Claude <noreply@anthropic.com>",
                         "Claude Opus 4.8 <noreply@anthropic.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), identity)

    def test_a_name_without_the_tool_of_the_address_is_marked_and_the_address_kept(self):
        """A person's name at the address that claude-author-04 or cursor-author-04 reads is
        marked, and so is Cursor at the address of claude-author-04, a heuristic of Claude."""
        for identity in ("Jane Doe <noreply@anthropic.com>", "Jane Doe <cursoragent@cursor.com>",
                         "Cursor Agent <noreply@anthropic.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity),
                                 f"{_mod.PERSON_MARK} <{identity.partition(' <')[2][:-1]}>")

    def test_the_address_of_a_convention_stays_marked(self):
        """claude-author-06 reads claude.jane@anthropic.com as a convention, which a person's
        address can match."""
        self.assertEqual(_mod.identity_without_person("Jane Doe <claude.jane@anthropic.com>"),
                         _mod.PERSON_MARK)

    def test_an_identity_that_an_account_heuristic_reads_with_name_and_address_is_kept(self):
        """codex-author-07 reads Codex before noreply@openai.com, the identity of Codex's own
        code, and no other name at that address."""
        self.assertEqual(_mod.identity_without_person("Codex <noreply@openai.com>"),
                         "Codex <noreply@openai.com>")
        self.assertEqual(_mod.identity_without_person("Jane Doe <noreply@openai.com>"),
                         _mod.PERSON_MARK)

    def test_a_bot_at_an_address_that_an_account_heuristic_reads_is_kept(self):
        """kilo-code-author-01 reads kilo-bot@kilo.ai, under which Kilo-Org/kilocode 1c3938267418
        has the author Kilo Bot. The name names a bot rather than the tool, and a person's name at
        that address is marked."""
        self.assertEqual(_mod.identity_without_person("Kilo Bot <kilo-bot@kilo.ai>"),
                         "Kilo Bot <kilo-bot@kilo.ai>")
        self.assertEqual(_mod.identity_without_person("Jane Doe <kilo-bot@kilo.ai>"),
                         f"{_mod.PERSON_MARK} <kilo-bot@kilo.ai>")

    def test_the_organization_of_the_address_can_precede_the_tool(self):
        """copilot-author-05 reads copilot@github.com and codex-author-05 codex@openai.com, and
        copilot-trailer-02 reads `github` before `copilot`."""
        for identity in ("GitHub Copilot <copilot@github.com>", "OpenAI Codex <codex@openai.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), identity)
        self.assertEqual(_mod.identity_without_person("Jane Copilot <copilot@github.com>"),
                         f"{_mod.PERSON_MARK} Copilot <copilot@github.com>")

    def test_words_before_the_tool_in_a_name_are_marked(self):
        """The words of a name before the tool can name a person, as a GitHub login does."""
        for name in ("@janedoe using", "Jane Doe via", "Cursor with"):
            with self.subTest(name=name):
                self.assertEqual(
                    _mod.identity_without_person(f"{name} Claude Opus 4.8 <noreply@anthropic.com>"),
                    f"{_mod.PERSON_MARK} Claude Opus 4.8 <noreply@anthropic.com>")

    def test_a_kept_identity_marks_the_login_of_a_person(self):
        self.assertEqual(_mod.identity_without_person("Claude @janedoe <noreply@anthropic.com>"),
                         f"Claude {_mod.PERSON_MARK} <noreply@anthropic.com>")

    def test_a_tool_at_an_address_that_reads_no_mail_is_kept_and_a_person_marked(self):
        """No author heuristic reads noreply@google.com. The name of one of the six tools stays
        there, and any other name, also one that begins with the tool, can be a person's."""
        self.assertEqual(_mod.identity_without_person("Gemini <noreply@google.com>"),
                         "Gemini <noreply@google.com>")
        for identity in ("Jane Doe <noreply@example.org>", "Gemini Jane <noreply@google.com>",
                         "Jane Doe <1+imgbot[bot]@users.noreply.github.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), _mod.PERSON_MARK)

    def test_a_parenthesis_that_names_one_of_the_six_tools_stays_after_the_mark(self):
        for identity, kept in (
                ("Jarvis (Claude) <jane@example.org>", f"{_mod.PERSON_MARK} (Claude)"),
                ("coder-jane (Claude Opus 4.8 (1M context)) <noreply@anthropic.com>",
                 f"{_mod.PERSON_MARK} (Claude Opus 4.8 (1M context)) <noreply@anthropic.com>"),
                ("Jane Doe (test merge) <jane@example.org>", _mod.PERSON_MARK)):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), kept)

    def test_the_name_of_a_tool_at_a_persons_address_stays_marked(self):
        """copilot-author-02 reads the name copilot-swe-agent[bot], and the address can still be
        a person's."""
        self.assertEqual(
            _mod.identity_without_person("copilot-swe-agent[bot] <jane.doe@example.org>"),
            _mod.PERSON_MARK)

    def test_the_message_of_a_commit_that_no_heuristic_credits_is_left_out(self):
        self.assertEqual(_mod.without_people(self.ITEM)["message"], "")

    def test_the_message_of_a_credited_commit_is_kept_without_people(self):
        """The message passes through the masking of a line of the sample of mentions, which
        keeps the identity of a tool and code."""
        message = ("Fix the parser, reviewed by @janedoe\n\n```python\n@dataclass\n```\n\n"
                   "Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>\n"
                   "Signed-off-by: Jane Doe <jane.doe@example.org>")
        kept = (f"Fix the parser, reviewed by {_mod.PERSON_MARK}\n\n```python\n@dataclass\n```\n\n"
                "Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>\n"
                f"Signed-off-by: {_mod.PERSON_MARK}")
        for column in ("detected", "detected_other"):
            with self.subTest(column=column):
                item = {**self.ITEM, column: "Claude", "message": message}
                self.assertEqual(_mod.without_people(item)["message"], kept)

    def test_the_name_after_the_token_of_a_trailer_ends_at_its_line(self):
        """A message has many lines, and the name before an address on one line takes no words
        of the line before it."""
        for end in ("\n", "\r\n"):
            with self.subTest(end=end):
                self.assertEqual(
                    _mod.line_without_people(f"Note: see the docs{end}Jane Doe <jane@example.org>"),
                    f"Note: see the docs{end}{_mod.PERSON_MARK}")

    def test_a_line_keeps_the_address_of_an_app_and_of_a_service(self):
        for line in ("Co-authored-by: Claude <noreply@anthropic.com>",
                     "Co-authored-by: imgbot[bot] <1+imgbot[bot]@users.noreply.github.com>",
                     "Co-Authored-By: Gemini <noreply@google.com>"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_a_line_marks_a_person_at_an_address_that_reads_no_mail(self):
        for line in ("Co-authored-by: Jane Doe <noreply@example.org>",
                     "Co-authored-by: Jane Doe <1+imgbot[bot]@users.noreply.github.com>"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line),
                                 f"Co-authored-by: {_mod.PERSON_MARK}")

    def test_a_line_and_the_sample_of_commit_recall_keep_the_same_identities(self):
        """The value of a trailer is the identity of the person or the tool that it credits."""
        for identity in ("Claude Opus 4.8 <noreply@anthropic.com>",
                         "@janedoe using Claude Opus 4.8 <noreply@anthropic.com>",
                         "GitHub Copilot <copilot@github.com>", "Kilo Bot <kilo-bot@kilo.ai>",
                         "Gemini <noreply@google.com>", "Jane Doe <noreply@example.org>",
                         "Jarvis (Claude) <jane@example.org>",
                         "Jane Doe <198982749+Copilot@users.noreply.github.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.line_without_people(f"Co-authored-by: {identity}"),
                                 f"Co-authored-by: {_mod.identity_without_person(identity)}")

    def test_a_line_marks_a_name_before_an_address_and_an_address_alone(self):
        self.assertEqual(_mod.line_without_people("Signed-off-by: Jane Doe <jane@example.org>"),
                         f"Signed-off-by: {_mod.PERSON_MARK}")
        self.assertEqual(_mod.line_without_people("mail jane@example.org now"),
                         f"mail {_mod.PERSON_MARK} now")

    def test_a_line_keeps_the_identities_that_the_sample_of_commit_recall_keeps(self):
        for line in ("Co-authored-by: copilot-swe-agent[bot] "
                     "<198982749+Copilot@users.noreply.github.com>",
                     "Co-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>",
                     "Co-authored-by: Cursor Agent <cursoragent@cursor.com>",
                     "Co-authored-by: Codex <codex@openai.com>",
                     "Generated with Cursor Agent <cursoragent@cursor.com>",
                     "see mailto:198982749+Copilot@users.noreply.github.com"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_a_line_marks_another_name_at_the_address_of_a_tool_and_keeps_the_address(self):
        self.assertEqual(
            _mod.line_without_people(
                "Co-authored-by: Jane Doe <198982749+Copilot@users.noreply.github.com>"),
            f"Co-authored-by: {_mod.PERSON_MARK} <198982749+Copilot@users.noreply.github.com>")

    def test_a_package_with_its_version_is_no_address(self):
        """The mentions of the collection of 2026-11-04 name npm packages with their versions,
        such as the collaboration cursor of tiptap."""
        for line in ("- @tiptap/extension-collaboration-cursor@0.0.0-beta.0",
                     "## @examples/assistant-ui-claude@0.1.43", "* versions: claude@1.0.20",
                     "compare/pg-cursor@2.20.0...pg-cursor@2.21.0"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_the_whole_value_of_a_trailer_is_the_name_before_its_address(self):
        """A name can hold parentheses and quotes, and a parenthesis that names one of the six
        tools stays after the mark."""
        for line, kept in (
                ("Co-authored-by: Jarvis (Claude) <jane@example.org>",
                 f"Co-authored-by: {_mod.PERSON_MARK} (Claude)"),
                ("Co-authored-by: Jane Doe (test merge) <jane@example.org>",
                 f"Co-authored-by: {_mod.PERSON_MARK}"),
                ("Co-authored-by: JaneD (written by Jane Q. Doe) <jane@example.org>",
                 f"Co-authored-by: {_mod.PERSON_MARK}"),
                ('Signed-off-by: Jane Doe "<jane@example.org>"',
                 f'Signed-off-by: {_mod.PERSON_MARK}"'),
                ("Co-authored-by: @janedoe using Claude Opus 4.8 <noreply@anthropic.com>",
                 f"Co-authored-by: {_mod.PERSON_MARK} Claude Opus 4.8 <noreply@anthropic.com>"),
                ("fix x\\n\\nSigned-off-by: Jane Doe <jane@example.org>\\n\\n"
                 "Co-authored-by: Mira Doe (Claude consilium) <noreply@anthropic.com>",
                 f"fix x\\n\\nSigned-off-by: {_mod.PERSON_MARK}\\n\\nCo-authored-by: "
                 f"{_mod.PERSON_MARK} (Claude consilium) <noreply@anthropic.com>")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)

    def test_words_of_a_line_before_an_identity_outside_a_trailer_stay(self):
        for line in ("Assisted with  Claude <noreply@anthropic.com>",
                     "This Work Was Co-Authored-By Claude Opus 4.6 (1M context) "
                     "<noreply@anthropic.com>"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_a_line_marks_the_login_of_a_person(self):
        """A login stays where it names a bot or a tool: copilot-author-02 reads
        copilot-swe-agent, coderabbit-author-01 reads coderabbitai[bot], and the vendor page of
        Claude Code GitHub Actions tells a reader to mention @claude. A person's login can begin
        with a tool's name, as the login in the line of mn-0153 of the evaluation of 2026-10-05
        does."""
        for line, kept in (
                ("modified and reviewed by @janedoe",
                 f"modified and reviewed by {_mod.PERSON_MARK}"),
                ("* Fix by @\u200bjane-doe in https://github.com/o/r/pull/1",
                 f"* Fix by {_mod.PERSON_MARK} in https://github.com/o/r/pull/1"),
                ("auhtor @codex-jane is not active", f"auhtor {_mod.PERSON_MARK} is not active"),
                ("(@janedoe)", f"({_mod.PERSON_MARK})")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)
        for line in ("ci: give @claude the workspace config", "retry with @copilot-swe-agent",
                     "@coderabbitai review the Claude config",
                     "run @gemini-cli on it", "<code>@\u200bCopilot</code>",
                     "thanks @dependabot[bot] and Copilot", "Add @AGENTS.md to CLAUDE.md",
                     "- @types/node for Codex", "mail noreply@anthropic.com about Claude"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_an_at_in_code_is_no_mention(self):
        """An `@` in a Markdown code span or a fenced code block is code, such as the route
        decorator in the line of mn-0104 of the evaluation of 2026-10-26, and stays. Only a fence
        of the same character that is at least as long closes a fenced code block. A run of
        backticks that no run of the same length closes on its line opens no code span, and a run
        of backticks with another backtick after it on its line opens no fence."""
        for line in ("When a `@generation` route uses one of the newer Claude models (e.g.,",
                     "Claude moved ``@cache` and `@retry`` to the client",
                     "Claude wrote\n```python\n@dataclass\nclass Item:\n```",
                     "Claude wrote\n  ~~~~\n@dataclass\n~~~\n@override",
                     "Claude wrote\n~~~\n@dataclass\n```\n@override"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)
        for line, kept in (
                ("`@generation` reviewed by @janedoe",
                 f"`@generation` reviewed by {_mod.PERSON_MARK}"),
                ("a `@janedoe route for Claude", f"a `{_mod.PERSON_MARK} route for Claude"),
                ("a `@x`` and @janedoe", f"a `{_mod.PERSON_MARK}`` and {_mod.PERSON_MARK}"),
                ("Claude `wrote\n@janedoe`", f"Claude `wrote\n{_mod.PERSON_MARK}`"),
                ("```\n@dataclass\n```\nthanks @janedoe",
                 f"```\n@dataclass\n```\nthanks {_mod.PERSON_MARK}"),
                ("```js`\n@janedoe", f"```js`\n{_mod.PERSON_MARK}")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)

    def test_a_line_marks_a_login_after_an_entity_of_the_zero_width_space(self):
        """Renovate writes `@&#8203;` before a login in the release notes that it quotes, as in
        the line of mn-0158 of the evaluation of 2026-11-04, and the pattern of the mentions CSV
        writes the entity with 0 for its digits."""
        for line, kept in (
                ("[@&#8203;janedoe](https://redirect.github.com/janedoe) and **Claude",
                 f"[{_mod.PERSON_MARK}](https://redirect.github.com/{_mod.PERSON_MARK}) and **Claude"),
                ("[@&#0;janedoe](<url> and **claude", f"[{_mod.PERSON_MARK}](<url> and **claude"),
                ("by Jane Doe (@&#8203;janedoe) with Codex", f"by Jane Doe ({_mod.PERSON_MARK}) with Codex"),
                ("by @&#x200B;janedoe with Codex", f"by {_mod.PERSON_MARK} with Codex"),
                ("by @&#x0b;janedoe with codex", f"by {_mod.PERSON_MARK} with codex")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)
        for line in ("[@&#8203;Copilot](https://redirect.github.com/Copilot) in",
                     "[@&#0;copilot](<url> in", "- @&#8203;tiptap/core for Claude"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_a_line_marks_the_login_of_a_link_to_a_profile(self):
        """Dependabot links a mention to the profile of the login, and Renovate redirects such a
        link through togithub.com or redirect.github.com. A link with more path after the login,
        such as the link of a repository, names no profile, and a profile link marks the login
        whole even when the limit on the length of a line cut it."""
        for line, kept in (
                ('<li><a href="https://github.com/janedoe"><code>@\u200bjanedoe</code></a> with '
                 '<a href="https://github.com/Copilot"><code>@\u200bCopilot</code></a>',
                 f'<li><a href="https://github.com/{_mod.PERSON_MARK}"><code>{_mod.PERSON_MARK}'
                 '</code></a> with <a href="https://github.com/Copilot"><code>@\u200bCopilot'
                 '</code></a>'),
                ("thanks to https://www.github.com/janedoe and Claude",
                 f"thanks to https://www.github.com/{_mod.PERSON_MARK} and Claude"),
                ("[@&#8203;Jane-Doe](https://togithub.com/Jane-Doe) with Claude",
                 f"[{_mod.PERSON_MARK}](https://togithub.com/{_mod.PERSON_MARK}) with Claude"),
                ("by [Jane](https://togithub.com/Jane-Do",
                 f"by [Jane](https://togithub.com/{_mod.PERSON_MARK}"),
                ("see github.com/janedoe?tab=repositories for Claude",
                 f"see github.com/{_mod.PERSON_MARK}?tab=repositories for Claude")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)
        for line in ("see https://github.com/janedoe/claude-tools for Claude",
                     "<a href=\"https://redirect.github.com/apps/copilot-swe-agent\">Copilot</a>",
                     "https://github.com/claude in https://docs.github.com/copilot",
                     "https://github.com/janedoe.png for Claude"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)

    def test_an_escaped_mention_in_code_is_a_mention(self):
        """Dependabot writes a mention as a code span that holds `@`, a zero-width space and the
        login, which only keeps GitHub from notifying the login. A plain `@` in code stays."""
        for line, kept in (
                ("by [`@\u200bjanedoe`](https://github.com/janedoe) with "
                 "[`@\u200bCopilot`](https://github.com/Copilot) in",
                 f"by [`{_mod.PERSON_MARK}`](https://github.com/{_mod.PERSON_MARK}) with "
                 "[`@\u200bCopilot`](https://github.com/Copilot) in"),
                ("Claude wrote\n```\n@\u200bjanedoe @dataclass\n```",
                 f"Claude wrote\n```\n{_mod.PERSON_MARK} @dataclass\n```"),
                ("Claude added `@&#8203;janedoe` and `@generation`",
                 f"Claude added `{_mod.PERSON_MARK}` and `@generation`")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)

    def test_an_automation_that_a_row_of_not_ai_commits_reads_is_kept(self):
        """gemini-full-body-21 reads the release bot gemini-cli-robot, and the row
        gemini-cli-robot of not_ai_commits.csv names it as the identity under which the release
        workflows of google-gemini/gemini-cli commit. Its name has no word `bot`. An identity that
        such a row reads stays at an address of no person, and a login stays when it is a whole
        row."""
        for identity in ("gemini-cli-robot <gemini-cli-robot@google.com>",
                         "Mend Renovate <bot@renovateapp.com>", "Weblate <noreply@weblate.org>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), identity)
                self.assertEqual(_mod.line_without_people(f"Co-authored-by: {identity}"),
                                 f"Co-authored-by: {identity}")
        for identity in ("Jane Renovate <jane@example.org>", "Weblate Jane <jane@weblate.org>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), _mod.PERSON_MARK)
        for line in ("ask @dependabot to rebase for Claude",
                     "by https://github.com/gemini-cli-robot with Gemini"):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), line)
        self.assertEqual(_mod.line_without_people("ask @dependabot-jane for Claude"),
                         f"ask {_mod.PERSON_MARK} for Claude")

    def test_an_automation_stays_at_a_mailbox_of_its_own_domain(self):
        """Weblate commits a machine translation under an internal bot at
        `noreply-{scope}-{name}@weblate.org`, Hosted Weblate pushes as `hosted@weblate.org`, and
        Crowdin's GitHub action commits as `support+bot@crowdin.com`, each at the domain that the
        row of not_ai_commits.csv names. Any other mailbox there can be a person's, and so can the
        mailbox of the automation at another domain or under a name that no row reads."""
        machine = "Weblate <noreply-mt-weblate@weblate.org>"
        hosted = "Hosted Weblate <hosted@weblate.org>"
        item = {**self.ITEM, "author": machine, "committer": hosted}
        self.assertEqual((_mod.without_people(item)["author"],
                          _mod.without_people(item)["committer"]), (machine, hosted))
        for identity in (machine, hosted, "Crowdin Bot <support+bot@crowdin.com>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), identity)
                self.assertEqual(_mod.line_without_people(f"Co-authored-by: {identity}"),
                                 f"Co-authored-by: {identity}")
        for identity in ("Weblate Jane <jane@weblate.org>", "Weblate <jane@weblate.org>",
                         "Jane Doe <noreply-mt-weblate@weblate.org>",
                         "Weblate <noreply-mt-weblate@example.org>",
                         "Hosted Weblate <hosted@example.org>"):
            with self.subTest(identity=identity):
                self.assertEqual(_mod.identity_without_person(identity), _mod.PERSON_MARK)
                self.assertEqual(_mod.line_without_people(f"Co-authored-by: {identity}"),
                                 f"Co-authored-by: {_mod.PERSON_MARK}")

    def test_a_login_that_ends_in_a_hyphen_is_no_tool(self):
        """GitHub's API records accounts of the type User with a login that ends in a hyphen, and
        such a login is marked even when it begins with the name of a tool."""
        for line, kept in (
                ("[@&#8203;Cursor-](https://togithub.com/Cursor-) in",
                 f"[{_mod.PERSON_MARK}](https://togithub.com/{_mod.PERSON_MARK}) in"),
                ("thanks @jane- for Claude", f"thanks {_mod.PERSON_MARK} for Claude")):
            with self.subTest(line=line):
                self.assertEqual(_mod.line_without_people(line), kept)


class TestCommitHeuristicAttribution(_Root):

    def test_sole_credits_and_the_repositories_that_would_lose_the_tool(self):
        rows = [
            {"repo_name": "o/a", "matched_heuristics": "claude-trailer-02"},
            {"repo_name": "o/a", "matched_heuristics": "claude-trailer-02;claude-full-body-02"},
            {"repo_name": "o/b", "matched_heuristics": "claude-trailer-02"},
            {"repo_name": "o/b", "matched_heuristics": "claude-trailer-02"},
        ]
        write_csv(self.output / "all_ai_commits.csv", rows)
        write_csv(self.output / "other_ai_commits.csv", [], ["repo_name", "matched_heuristics"])
        found = {r["id"]: r for r in _mod.commit_heuristic_attribution(self.output)}
        trailer = found["claude-trailer-02"]
        self.assertEqual((trailer["commits"], trailer["repos"], trailer["sole_commits"],
                          trailer["repos_lost_if_removed"]), (4, 2, 3, 1))
        marker = found["claude-full-body-02"]
        self.assertEqual((marker["commits"], marker["sole_commits"], marker["repos_lost_if_removed"]),
                         (1, 0, 0))
        self.assertEqual(found["copilot-trailer-07"]["commits"], 0)


class TestTheDraw(_Root):
    """`sample` over the inputs of a collection that started on 2026-11-02, in the formats that
    the collection writes them."""

    def setUp(self):
        super().setUp()
        commit_columns = ["repo_name", "commit_sha", "github_link", "ai_tool",
                          "matched_heuristics", "attributions", "commit_message"]
        write_csv(self.output / "all_ai_commits.csv", [], commit_columns)
        write_csv(self.output / "other_ai_commits.csv", [], commit_columns)
        write_csv(self.output / "ai_commits-repos.csv",
                  [{"repo_name": "o/a", "outcome": "no_files", "commits_read": ""}])
        self.lists = self.tmp / "file_lists"
        self.lists.mkdir()
        (self.lists / f"o§a{clone.FILE_LIST_SUFFIX}").write_bytes(
            lzma.compress(b"100644\tCLAUDE.md\0"))
        self.logs = self.tmp / "collect_commits"
        self.logs.mkdir()
        self.mentions = self.tmp / "all_ai_commits-mentions.csv"
        write_csv(self.mentions, [], ["tool", "pattern", "commits", "repos", "shas", "line"])
        self.finished = self.tmp / resume.FINISHED_NAME
        # The collection ran past midnight, and a repository that it took over keeps the line of
        # the September collection.
        for repo, scanned_at, collection in (
                ("o/a", "2026-11-03T01:00:00Z", "2026-11-02T051500Z-0a1b2c3d"),
                ("o/b", "2026-09-24T05:00:00Z", "2026-09-24T040000Z-4e5f6a7b")):
            resume.append_finished(self.finished, repo,
                                   paths.build_csv_row(repo, scanned_at, "main", "a" * 40,
                                                       scanned_at, "b" * 40, set()),
                                   None, collected_under={"collector": "x"},
                                   collection=collection)
        self.evaluations = self.tmp / "evaluation"

    def draw(self) -> tuple:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _mod.main(["sample", "--output-dir", str(self.output),
                            "--file-lists", str(self.lists), "--commit-logs", str(self.logs),
                            "--mentions", str(self.mentions), "--finished", str(self.finished),
                            "--evaluation-dir", str(self.evaluations)])
        return rc, err.getvalue()

    def test_the_samples_go_into_the_directory_of_the_day_on_which_the_collection_started(self):
        rc, err = self.draw()
        self.assertEqual(rc, 0, err)
        self.assertEqual([p.name for p in self.evaluations.iterdir()], ["2026-11-02"])
        drawn = self.evaluations / "2026-11-02"
        self.assertEqual([r["repo_name"] for r in read_csv(drawn / "path_recall.csv")], ["o/a"])
        # The list of finished repositories gives the day, and sample.json records the digest of
        # that list.
        recorded = json.loads((drawn / "sample.json").read_text(encoding="utf-8"))["inputs"]
        self.assertEqual(recorded[resume.FINISHED_NAME], _mod.sha256(self.finished))

    def test_the_draw_keeps_the_mentions_csv_that_it_read(self):
        """A commit reclassification rewrites the mentions CSV after the draw. The draw therefore
        keeps beside sample.json a copy of the CSV that it read, with the SHA-256 that sample.json
        records."""
        write_csv(self.mentions, [{"tool": "claude", "pattern": "made with claude",
                                   "commits": "2", "repos": "1", "shas": "a" * 40,
                                   "line": "Made with Claude"}])
        read = self.mentions.read_bytes()
        rc, err = self.draw()
        self.assertEqual(rc, 0, err)
        self.mentions.write_text("tool,pattern,commits,repos,shas,line\n", encoding="utf-8")
        drawn = self.evaluations / "2026-11-02"
        recorded = json.loads((drawn / "sample.json").read_text(encoding="utf-8"))["inputs"]
        with lzma.open(drawn / "all_ai_commits-mentions.csv.xz", "rb") as f:
            kept = f.read()
        self.assertEqual(kept, read)
        self.assertEqual(hashlib.sha256(kept).hexdigest(), recorded["all_ai_commits-mentions.csv"])

    def test_the_draw_writes_the_mentions_without_people_and_the_texts_beside_the_sample(self):
        """The tracked sample of mentions names no person, and the labelers read the identities
        and the messages of the commits in a file that git ignores."""
        write_csv(self.mentions, [{"tool": "claude", "pattern": "reviewed with claude",
                                   "commits": "2", "repos": "1", "shas": "a" * 40,
                                   "line": "Claude helped Jane Doe <jane@example.org>"}])
        rc, err = self.draw()
        self.assertEqual(rc, 0, err)
        drawn = self.evaluations / "2026-11-02"
        lines = [row["line"] for row in read_csv(drawn / "mentions.csv")]
        self.assertEqual(len(lines), 1)
        self.assertNotIn("jane@example.org", lines[0])
        self.assertIn(_mod.PERSON_MARK, lines[0])
        self.assertTrue((drawn / _mod.RECALL_TEXTS).is_file())

    def test_a_second_draw_of_a_collection_exits_1_and_keeps_the_first(self):
        """The items of a second draw would take the labels of the first draw's items."""
        self.assertEqual(self.draw()[0], 0)
        recall = self.evaluations / "2026-11-02" / "path_recall.csv"
        recall.write_text("labeled\n", encoding="utf-8")
        rc, err = self.draw()
        self.assertEqual(rc, 1)
        self.assertIn("already holds the samples", err)
        self.assertEqual(recall.read_text(encoding="utf-8"), "labeled\n")

    def test_a_collection_without_a_known_day_exits_1(self):
        self.finished.write_text("", encoding="utf-8")
        rc, err = self.draw()
        self.assertEqual(rc, 1)
        self.assertIn("The day on which the collection started is unknown", err)
        self.assertFalse(self.evaluations.exists())

    def draw_only(self, into: Path, *names: str) -> tuple:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _mod.main(["sample", "--only", *names, "--into", str(into),
                            "--output-dir", str(self.output)])
        return rc, err.getvalue()

    def report(self, directory: Path) -> tuple:
        """Label the first pass of every item of the samples in `directory` with a label that
        the criteria allow, run `report` over the directory and return its exit code and
        report.md."""
        allowed = {"path_precision": "correct", "path_recall": "none",
                   "commit_precision": "correct", "commit_recall": "none",
                   "mentions": "not_attribution", "import_precision": "correct",
                   "planning_precision": "correct"}
        for name, label in allowed.items():
            if (directory / f"{name}.csv").is_file():
                write_csv(directory / "labels" / f"{name}.csv",
                          [{"item": row["item"], "pass": "1", "label": label}
                           for row in read_csv(directory / f"{name}.csv")],
                          list(_mod.LABEL_COLUMNS))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _mod.main(["report", str(directory)])
        report = directory / "report.md"
        return rc, report.read_text(encoding="utf-8") if report.is_file() else err.getvalue()

    def rows_with_strata_of_the_evidence(self) -> None:
        """Rows of imports.csv and planning-documents.csv in o/a and in a repository that the
        evidence links. The rows of one import heuristic and status, and of one framework, are
        all in that repository."""
        self.import_rows([("o/a", "claude-import-01", "found", "CLAUDE.md", "CLAUDE.md",
                           "AGENTS.md", "repos_data.7z"),
                          (EVIDENCE_REPOSITORY, "claude-import-01", "found", "CLAUDE.md",
                           "CLAUDE.md", "docs/e.md", "repos_data.7z"),
                          (EVIDENCE_REPOSITORY, "gemini-import-01", "found", "GEMINI.md",
                           "GEMINI.md", "style.md", "repos_data.7z")])
        self.planning_rows([("o/a", ".cursor/plans/p.plan.md", "cursor", "plan"),
                            (EVIDENCE_REPOSITORY, ".cursor/plans/q.plan.md", "cursor", "plan"),
                            (EVIDENCE_REPOSITORY, "backlog/tasks/task-1.md", "backlog-md",
                             "task"),
                            (EVIDENCE_REPOSITORY, "backlog/tasks/task-2.md", "backlog-md",
                             "task")])

    # The strata of `rows_with_strata_of_the_evidence` that are drawn from the repository that
    # the evidence links, with their rows.
    STRATA_OF_THE_EVIDENCE = {"import_precision": {"gemini-import-01/found": 1},
                              "planning_precision": {"backlog-md": 2}}

    def test_the_draw_includes_the_imports_and_the_planning_documents(self):
        """A draw of a collection writes the samples of imports.csv and planning-documents.csv
        with the others. It leaves out the repositories that the evidence links, except in a
        stratum with all its rows in them, which it draws from those rows and marks. sample.json
        records both CSVs, the sizes of both samples, the number of those repositories and the
        strata drawn from them, and the report over the drawn directory names those strata."""
        self.assertIn(EVIDENCE_REPOSITORY, _mod.evidence_repositories(heuristics.HEURISTICS_DIR))
        self.rows_with_strata_of_the_evidence()
        rc, err = self.draw()
        self.assertEqual(rc, 0, err)
        drawn = self.evaluations / "2026-11-02"
        self.assertEqual([(r["item"], r["repo_name"], r["from_evidence"])
                          for r in read_csv(drawn / "import_precision.csv")],
                         [("im-0001", "o/a", ""), ("im-0002", EVIDENCE_REPOSITORY, "yes")])
        self.assertEqual([(r["item"], r["stratum"], r["repo_name"], r["stratum_rows"],
                           r["from_evidence"])
                          for r in read_csv(drawn / "planning_precision.csv")],
                         [("pd-0001", "backlog-md", EVIDENCE_REPOSITORY, "2", "yes"),
                          ("pd-0002", "backlog-md", EVIDENCE_REPOSITORY, "2", "yes"),
                          ("pd-0003", "cursor", "o/a", "2", "")])
        record = json.loads((drawn / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(record["inputs"]["imports.csv"], _mod.sha256(self.output / "imports.csv"))
        self.assertEqual(record["inputs"]["planning-documents.csv"],
                         _mod.sha256(self.output / "planning-documents.csv"))
        self.assertEqual((record["sizes"]["import_precision_per_cell"],
                          record["sizes"]["planning_precision_per_cell"]), (7, 20))
        self.assertEqual(record["excluded_repositories"],
                         len(_mod.evidence_repositories(heuristics.HEURISTICS_DIR)))
        self.assertEqual(record["strata_from_evidence"], self.STRATA_OF_THE_EVIDENCE)
        rc, rendered = self.report(drawn)
        self.assertEqual(rc, 0, rendered)
        self.assertIn("gemini-import-01/found (1 row).", rendered)
        self.assertIn("backlog-md (2 rows).", rendered)

    def test_a_later_draw_records_the_strata_from_the_evidence_and_the_report_names_them(self):
        """A draw with `--only` into the directory of a draw of the five other samples with a
        sample.json that records no strata of the two samples, as that of the evaluation of
        2026-11-04, leaves out the repositories that the evidence links, except in the strata with
        all their rows in them. Its record under `later_draws` gives the number of these
        repositories and the strata drawn from them, and the report names those strata."""
        self.rows_with_strata_of_the_evidence()
        self.assertEqual(self.draw()[0], 0)
        drawn = self.evaluations / "2026-11-02"
        for name in ("import_precision.csv", "planning_precision.csv"):
            (drawn / name).unlink()
        record = json.loads((drawn / "sample.json").read_text(encoding="utf-8"))
        del record["strata_from_evidence"]
        (drawn / "sample.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        rc, err = self.draw_only(drawn, "import_precision", "planning_precision")
        self.assertEqual(rc, 0, err)
        later = json.loads((drawn / "sample.json").read_text(encoding="utf-8"))["later_draws"]
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["excluded_repositories"], record["excluded_repositories"])
        self.assertEqual(later[0]["strata_from_evidence"], self.STRATA_OF_THE_EVIDENCE)
        for name in ("import_precision", "planning_precision"):
            with self.subTest(sample=name):
                self.assertEqual({(r["repo_name"], r["from_evidence"])
                                  for r in read_csv(drawn / f"{name}.csv")},
                                 {("o/a", ""), (EVIDENCE_REPOSITORY, "yes")})
        rc, rendered = self.report(drawn)
        self.assertEqual(rc, 0, rendered)
        self.assertIn("gemini-import-01/found (1 row).", rendered)
        self.assertIn("backlog-md (2 rows).", rendered)

    def test_a_sample_drawn_again_is_reported_with_the_marks_of_its_items(self):
        """A sample that was removed and drawn again with `--only` has a record of its own under
        `later_draws`. The report takes the strata drawn from the repositories of the evidence
        from the marks of the items, not from the record of the draw of the collection, which
        sample.json keeps."""
        self.rows_with_strata_of_the_evidence()
        self.assertEqual(self.draw()[0], 0)
        drawn = self.evaluations / "2026-11-02"
        # The collection gains a row of Backlog.md outside the repositories of the evidence.
        rows = read_csv(self.output / "planning-documents.csv")
        rows.append({**rows[-1], "repo_name": "o/a", "github_link": ""})
        write_csv(self.output / "planning-documents.csv", rows, list(rows[0]))
        (drawn / "planning_precision.csv").unlink()
        rc, err = self.draw_only(drawn, "planning_precision")
        self.assertEqual(rc, 0, err)
        record = json.loads((drawn / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual(record["strata_from_evidence"], self.STRATA_OF_THE_EVIDENCE)
        self.assertEqual(record["later_draws"][-1]["strata_from_evidence"],
                         {"planning_precision": {}})
        rc, rendered = self.report(drawn)
        self.assertEqual(rc, 0, rendered)
        report = json.loads((drawn / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["planning_precision"]["strata_from_evidence"], {})
        self.assertEqual(report["import_precision"]["strata_from_evidence"],
                         self.STRATA_OF_THE_EVIDENCE["import_precision"])
        self.assertNotIn("backlog-md (", rendered)

    def test_a_later_draw_of_the_imports_gives_the_items_of_a_draw_of_the_collection(self):
        """The samples of imports.csv and planning-documents.csv have generators of their own,
        and a draw with `--only` into the directory of an earlier draw with the same seed gives
        the items that a draw of the whole collection gives."""
        self.import_rows([(f"o/r{i}", "claude-import-01", "missing", "CLAUDE.md", "CLAUDE.md",
                           f"d{i}.md", "") for i in range(30)])
        self.planning_rows([(f"o/r{i}", f"specs/00{i}/spec.md", "spec-kit", "spec")
                            for i in range(30)])
        self.assertEqual(self.draw()[0], 0)
        drawn = self.evaluations / "2026-11-02"
        earlier = self.tmp / "earlier"
        earlier.mkdir()
        (earlier / "sample.json").write_text(json.dumps({"seed": _mod.SEED}), encoding="utf-8")
        rc, err = self.draw_only(earlier, "import_precision", "planning_precision")
        self.assertEqual(rc, 0, err)
        for name in ("import_precision.csv", "planning_precision.csv"):
            with self.subTest(sample=name):
                self.assertEqual((earlier / name).read_bytes(), (drawn / name).read_bytes())


class TestTheDrawIntoAnEarlierDraw(_Root):
    """`sample --only` draws the samples of imports.csv and planning-documents.csv into the
    directory of an evaluation that holds a draw, such as the labeled evaluation of 2026-11-04,
    without changing its samples or what its sample.json records."""

    def setUp(self):
        super().setUp()
        # Twelve rows of a heuristic and status and of a framework, each with one more row in a
        # repository that the evidence links, and a heuristic and status and a framework with
        # rows in that repository alone.
        self.import_rows([(f"o/r{i}", "copilot-import-03", "found", "CLAUDE.md", "CLAUDE.md",
                           f"d{i}.md", "repos_data.7z") for i in range(12)]
                         + [(EVIDENCE_REPOSITORY, "copilot-import-03", "found", "CLAUDE.md",
                             "CLAUDE.md", "e.md", "repos_data.7z"),
                            (EVIDENCE_REPOSITORY, "claude-import-01", "missing", "CLAUDE.md",
                             "CLAUDE.md", "gone.md", "")])
        self.planning_rows([(f"o/r{i}", f"openspec/changes/c{i}/tasks.md", "openspec", "tasks")
                            for i in range(12)]
                           + [(EVIDENCE_REPOSITORY, "openspec/changes/e/tasks.md", "openspec",
                               "tasks"),
                              (EVIDENCE_REPOSITORY, "backlog/tasks/task-1.md", "backlog-md",
                               "task")])
        self.earlier = self.tmp / "evaluation" / "2026-11-04"
        self.record = {"seed": 20261013, "excluded_repositories": 3,
                       "sizes": {"path_precision_per_cell": 10, "second_pass_share": 0.2},
                       "inputs": {"skills.csv": "0" * 64}, "heuristics": {"a.csv": "1" * 64}}
        (self.earlier / "labels").mkdir(parents=True)
        (self.earlier / "sample.json").write_text(json.dumps(self.record, indent=2) + "\n",
                                                  encoding="utf-8")
        self.kept = {"path_precision.csv": "item,label\npp-0001,labeled\n",
                     "mentions.csv": "item\nmn-0001\n", "report.json": "{}\n",
                     "labels/path_precision.csv": "item,pass,label\npp-0001,1,correct\n"}
        for name, text in self.kept.items():
            (self.earlier / name).write_text(text, encoding="utf-8")

    def draw(self, *names: str, extra: tuple = ()) -> tuple:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _mod.main(["sample", "--only", *names, "--into", str(self.earlier),
                            "--output-dir", str(self.output), *extra])
        return rc, err.getvalue()

    def test_the_samples_are_added_and_the_earlier_draw_stays_as_it_was(self):
        rc, err = self.draw("import_precision", "planning_precision")
        self.assertEqual(rc, 0, err)
        for name, text in self.kept.items():
            with self.subTest(file=name):
                self.assertEqual((self.earlier / name).read_text(encoding="utf-8"), text)
        record = json.loads((self.earlier / "sample.json").read_text(encoding="utf-8"))
        later = record.pop("later_draws")
        self.assertEqual(record, self.record)
        self.assertEqual(len(later), 1)
        self.assertEqual(later[0]["samples"], ["import_precision", "planning_precision"])
        self.assertEqual(later[0]["inputs"],
                         {"imports.csv": _mod.sha256(self.output / "imports.csv"),
                          "planning-documents.csv":
                              _mod.sha256(self.output / "planning-documents.csv")})
        self.assertEqual(later[0]["sizes"], {"import_precision_per_cell": 7,
                                             "planning_precision_per_cell": 20,
                                             "second_pass_share": 0.2})
        self.assertEqual(set(later[0]["heuristics"]),
                         {p.name for p in heuristics.HEURISTICS_DIR.glob("*.csv")})
        # The draw leaves out the repositories that the evidence links, as a draw of the whole
        # collection does, except in the strata with all their rows in them, and records the
        # number of these repositories and the strata drawn from them.
        self.assertEqual(later[0]["excluded_repositories"],
                         len(_mod.evidence_repositories(heuristics.HEURISTICS_DIR)))
        self.assertEqual(later[0]["strata_from_evidence"],
                         {"import_precision": {"claude-import-01/missing": 1},
                          "planning_precision": {"backlog-md": 1}})
        imports = read_csv(self.earlier / "import_precision.csv")
        planning = read_csv(self.earlier / "planning_precision.csv")
        self.assertEqual(len(imports), 8)
        self.assertEqual(len(planning), 13)
        self.assertEqual({(row["stratum"], row["repo_name"], row["stratum_rows"])
                          for row in imports + planning if row["from_evidence"] == "yes"},
                         {("claude-import-01/missing", EVIDENCE_REPOSITORY, "1"),
                          ("backlog-md", EVIDENCE_REPOSITORY, "1")})
        others = [row for row in imports + planning if row["from_evidence"] != "yes"]
        self.assertEqual({row["from_evidence"] for row in others}, {""})
        self.assertNotIn(EVIDENCE_REPOSITORY, {row["repo_name"] for row in others})
        self.assertEqual({row["stratum_rows"] for row in others}, {"13"})

    def test_a_second_pass_labels_a_seeded_fifth_of_each_sample(self):
        """The second pass of a sample takes ceil(n / 5) of its n items with the generator
        `<seed>:second:<sample>`, the seed that sample.json records."""
        self.assertEqual(self.draw("import_precision", "planning_precision")[0], 0)
        for name in ("import_precision", "planning_precision"):
            with self.subTest(sample=name):
                items = read_csv(self.earlier / f"{name}.csv")
                expected = set(random.Random(f"20261013:second:{name}").sample(
                    [row["item"] for row in items], math.ceil(len(items) / 5)))
                self.assertEqual({row["item"] for row in items if row["second_pass"] == "yes"},
                                 expected)

    def test_one_sample_can_be_drawn_alone_and_the_other_later(self):
        self.assertEqual(self.draw("planning_precision")[0], 0)
        self.assertFalse((self.earlier / "import_precision.csv").exists())
        planning = (self.earlier / "planning_precision.csv").read_bytes()
        self.assertEqual(self.draw("import_precision")[0], 0)
        self.assertEqual((self.earlier / "planning_precision.csv").read_bytes(), planning)
        record = json.loads((self.earlier / "sample.json").read_text(encoding="utf-8"))
        self.assertEqual([draw["samples"] for draw in record["later_draws"]],
                         [["planning_precision"], ["import_precision"]])

    def test_a_sample_or_labels_that_the_directory_holds_are_not_drawn_again(self):
        """The items of a second draw would take the labels of the first draw's items."""
        before = (self.earlier / "sample.json").read_text(encoding="utf-8")
        for existing in ("import_precision.csv", "labels/import_precision.csv"):
            with self.subTest(existing=existing):
                (self.earlier / existing).write_text("item\nim-0001\n", encoding="utf-8")
                rc, err = self.draw("import_precision", "planning_precision")
                self.assertEqual(rc, 1)
                self.assertIn("already holds the sample or the labels of import_precision",
                              err)
                self.assertEqual((self.earlier / existing).read_text(encoding="utf-8"),
                                 "item\nim-0001\n")
                self.assertFalse((self.earlier / "planning_precision.csv").exists())
                self.assertEqual((self.earlier / "sample.json").read_text(encoding="utf-8"),
                                 before)
                (self.earlier / existing).unlink()

    def test_a_directory_without_a_draw_exits_1(self):
        (self.earlier / "sample.json").unlink()
        rc, err = self.draw("import_precision")
        self.assertEqual(rc, 1)
        self.assertIn("holds no draw", err)
        self.assertFalse((self.earlier / "import_precision.csv").exists())

    def test_the_seed_is_the_seed_of_the_earlier_draw(self):
        rc, err = self.draw("import_precision", extra=("--seed", "5"))
        self.assertEqual(rc, 1)
        self.assertIn("was drawn with the seed 20261013", err)
        self.assertFalse((self.earlier / "import_precision.csv").exists())
        self.assertEqual(self.draw("import_precision", extra=("--seed", "20261013"))[0], 0)

    def test_a_missing_csv_exits_1(self):
        (self.output / "imports.csv").unlink()
        rc, err = self.draw("import_precision")
        self.assertEqual(rc, 1)
        self.assertIn("imports.csv", err)
        self.assertFalse((self.earlier / "import_precision.csv").exists())

    def test_only_and_into_go_together_and_only_names_these_samples(self):
        for argv in (["sample", "--only", "import_precision"],
                     ["sample", "--into", str(self.earlier)],
                     ["sample", "--only", "path_precision", "--into", str(self.earlier)]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as stop:
                    _mod.main(argv)
                self.assertEqual(stop.exception.code, 1)


def pack(archive: Path, source: Path) -> None:
    """Pack the file `source` at the root of the 7-Zip archive `archive`."""
    subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive.resolve()), source.name],
                   cwd=str(source.parent), check=True, capture_output=True)


def git_add(root: Path, *paths: Path) -> None:
    """Make `root` a git repository and add `paths` to its index, as a checkout tracks them."""
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    subprocess.run(["git", "add", "--", *(str(path.relative_to(root)) for path in paths)],
                   cwd=str(root), check=True, capture_output=True)


@unittest.skipUnless(shutil.which(SEVEN_ZIP) and shutil.which("git"), "7zz or git is not on PATH")
class TestTheDrawFromTrackedArchives(TestTheDraw):
    """`sample` in a checkout, where git tracks the archives of the commit CSVs: A commit CSV
    that differs from its archive is not the committed input, and nothing is drawn from it. The
    tests of the draw that this class inherits run beside matching archives."""

    def setUp(self):
        super().setUp()
        other = commits.OTHER_AI_COMMIT_PATTERNS[0]
        for name, tool, heuristic in (("all_ai_commits.csv", "claude", "claude-trailer-02"),
                                      ("other_ai_commits.csv", other["tool"], other["id"])):
            write_csv(self.output / name,
                      [{"repo_name": "owner/current", "commit_sha": "c" * 40,
                        "github_link": f"https://github.com/owner/current/commit/{'c' * 40}",
                        "ai_tool": tool, "matched_heuristics": heuristic,
                        "attributions": "standard", "commit_message": "Change"}])
        self.archived = {name: self.output / f"{name}.7z"
                         for name in ("all_ai_commits.csv", "other_ai_commits.csv")}
        for name, archive in self.archived.items():
            pack(archive, self.output / name)
        git_add(self.tmp, *self.archived.values())

    def make_stale(self, name: str) -> None:
        """Write the commit CSV `name` as an earlier archive held it."""
        rows = read_csv(self.output / name)
        rows[0]["repo_name"] = "owner/stale"
        write_csv(self.output / name, rows)

    def assert_stops_before_any_draw(self, name: str) -> None:
        """`sample` exits 1 with the message of the check on the stale commit CSV `name`, and
        writes nothing."""
        self.make_stale(name)
        rc, err = self.draw()
        self.assertEqual(rc, 1)
        self.assertIn(f"{self.output / name} differs from the file of its name in "
                      f"{self.archived[name]}", err)
        self.assertFalse(self.evaluations.exists())

    def test_commit_csvs_that_match_their_tracked_archives_are_drawn_from(self):
        rc, err = self.draw()
        self.assertEqual(rc, 0, err)
        drawn = read_csv(self.evaluations / "2026-11-02" / "commit_precision.csv")
        self.assertEqual({row["repo_name"] for row in drawn}, {"owner/current"})

    def test_a_commit_csv_of_the_six_tools_that_differs_from_its_archive_stops_the_draw(self):
        self.assert_stops_before_any_draw("all_ai_commits.csv")

    def test_a_commit_csv_of_the_other_tools_that_differs_from_its_archive_stops_the_draw(self):
        self.assert_stops_before_any_draw("other_ai_commits.csv")

    def test_a_tracked_archive_that_7z_cannot_list_exits_1(self):
        self.archived["other_ai_commits.csv"].write_bytes(b"not an archive")
        rc, err = self.draw()
        self.assertEqual(rc, 1)
        self.assertIn(str(self.archived["other_ai_commits.csv"]), err)
        self.assertFalse(self.evaluations.exists())

    def test_outside_a_repository_the_commit_csvs_are_the_input(self):
        """A working copy: git tracks no archive there, and the CSVs that the collection wrote
        are drawn from, whatever archive was copied beside them."""
        shutil.rmtree(self.tmp / ".git")
        self.make_stale("all_ai_commits.csv")
        rc, err = self.draw()
        self.assertEqual(rc, 0, err)
        drawn = read_csv(self.evaluations / "2026-11-02" / "commit_precision.csv")
        self.assertIn("owner/stale", {row["repo_name"] for row in drawn})


class TestTheReport(unittest.TestCase):

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        write_csv(self.dir / "path_precision.csv", [
            {"item": "pp-0001", "tool": "claude", "mechanism": "skills", "cell_rows": 100},
            {"item": "pp-0002", "tool": "claude", "mechanism": "skills", "cell_rows": 100}])
        write_csv(self.dir / "path_recall.csv", [
            {"item": "pr-0001", "stratum": "unconfigured", "repo_name": "o/a", "candidates_listed": 1,
             "repositories_in_stratum": 1000},
            {"item": "pr-0002", "stratum": "unconfigured", "repo_name": "o/b", "candidates_listed": 0,
             "repositories_in_stratum": 1000},
            {"item": "pr-0003", "stratum": "configured", "repo_name": "o/c", "candidates_listed": 2,
             "repositories_in_stratum": 50}])
        write_csv(self.dir / "path_recall_candidates.csv", [
            {"item": "pr-0001", "path": "CLAUDE.md", "detected": ""},
            {"item": "pr-0003", "path": "CLAUDE.md", "detected": "yes"},
            {"item": "pr-0003", "path": ".cursor/rules/a.mdc", "detected": ""}])
        write_csv(self.dir / "commit_precision.csv", [
            {"item": "cp-0001", "stratum": "tool:Claude", "stratum_rows": 10},
            {"item": "cp-0002", "stratum": "heuristic:claude-trailer-01", "stratum_rows": 3}])
        write_csv(self.dir / "commit_recall.csv", [
            {"item": "cr-0001", "detected": "Claude"}, {"item": "cr-0002", "detected": ""},
            {"item": "cr-0003", "detected": "Copilot"}])
        write_csv(self.dir / "mentions.csv", [
            {"item": "mn-0001", "commits": 5, "read_now": ""},
            {"item": "mn-0002", "commits": 1, "read_now": "Claude"}])
        self.labels = {
            "path_precision": [("pp-0001", "1", "correct", {}), ("pp-0002", "1", "not_configuration", {}),
                               ("pp-0002", "2", "correct", {})],
            "path_recall": [("pr-0001", "1", "missed", {"paths": "CLAUDE.md"}),
                            ("pr-0003", "1", "missed", {"paths": "CLAUDE.md;.cursor/rules/a.mdc"})],
            "commit_precision": [("cp-0001", "1", "correct", {"attribution_correct": "yes"}),
                                 ("cp-0002", "1", "not_contribution", {"attribution_correct": "no"})],
            "commit_recall": [("cr-0001", "1", "credits", {"tool": "Claude"}),
                              ("cr-0002", "1", "credits", {"tool": "Cursor"}),
                              ("cr-0003", "1", "none", {})],
            "mentions": [("mn-0001", "1", "attribution", {}), ("mn-0002", "1", "attribution", {})],
        }

    def write_labels(self):
        for name, rows in self.labels.items():
            write_csv(self.dir / "labels" / f"{name}.csv",
                      [{"item": i, "pass": p, "label": l, **extra} for i, p, l, extra in rows],
                      list(_mod.LABEL_COLUMNS))

    def run_report(self) -> tuple:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = _mod.main(["report", str(self.dir)])
        return rc, err.getvalue()

    def test_complete_labels_give_the_estimates_and_the_items_to_review(self):
        self.write_labels()
        rc, _ = self.run_report()
        self.assertEqual(rc, 0)
        report = json.loads((self.dir / "report.json").read_text())
        self.assertEqual(report["path_precision"]["overall"]["estimate"], 0.5)
        recall = report["path_recall"]
        # o/b listed no candidate and counts as nothing missed.
        self.assertEqual(recall["unconfigured_repositories_with_missed_configuration"]["n"], 2)
        self.assertEqual(recall["unconfigured_repositories_with_missed_configuration"]["successes"], 1)
        self.assertEqual(recall["missed_repositories_estimate"]["estimate"], 500)
        self.assertEqual(recall["configuration_paths_found_in_configured_repositories"]["estimate"], 0.5)
        precision = report["commit_precision"]
        self.assertEqual(precision["tools"]["estimate"], 1.0)
        self.assertEqual(precision["heuristics_seen_once"], {"claude-trailer-01": "not_contribution"})
        self.assertEqual(precision["attributions_correct"]["successes"], 1)
        self.assertEqual((report["commit_recall"]["recall"]["successes"],
                          report["commit_recall"]["recall"]["n"]), (1, 2))
        self.assertEqual(report["commit_recall"]["detected_without_credit"], 1)
        self.assertEqual(report["mentions"]["commits_of_those"], 5)
        self.assertEqual(report["path_precision"]["agreement"]["items"], 1)
        review = read_csv(self.dir / "review.csv")
        self.assertEqual([(r["sample"], r["item"], r["why"]) for r in review],
                         [("path_precision", "pp-0002", "passes disagree")])

    def test_a_configuration_path_that_only_other_csv_lists_counts_as_not_found(self):
        """The detection gave the path no mechanism, and no published CSV of a mechanism lists
        it."""
        write_csv(self.dir / "path_recall_candidates.csv", [
            {"item": "pr-0001", "path": "CLAUDE.md", "detected": ""},
            {"item": "pr-0003", "path": "CLAUDE.md", "detected": "yes"},
            {"item": "pr-0003", "path": ".claude/rules.txt", "detected": "other"}])
        self.labels["path_recall"][1] = ("pr-0003", "1", "missed",
                                         {"paths": "CLAUDE.md;.claude/rules.txt"})
        self.write_labels()
        self.assertEqual(self.run_report()[0], 0)
        found = json.loads((self.dir / "report.json").read_text())["path_recall"][
            "configuration_paths_found_in_configured_repositories"]
        self.assertEqual((found["successes"], found["n"]), (1, 2))

    def test_a_resolution_replaces_the_passes(self):
        self.write_labels()
        write_csv(self.dir / "labels" / "resolutions.csv",
                  [{"item": "pp-0002", "label": "correct", "tool": "", "paths": "", "note": "read"}])
        self.assertEqual(self.run_report()[0], 0)
        report = json.loads((self.dir / "report.json").read_text())
        self.assertEqual(report["path_precision"]["overall"]["estimate"], 1.0)
        self.assertEqual(read_csv(self.dir / "review.csv"), [])

    def test_a_resolution_with_a_label_its_sample_does_not_allow_or_of_no_item_exits_1(self):
        """Each case exits 1. A label that its sample does not allow, such as a misspelling,
        would count as "not correct" and change the precision. A resolution for an item that no
        sample contains would be dropped silently."""
        self.write_labels()
        for item, label, said in (("pp-0002", "corect", "'corect'"),
                                  ("cr-0002", "correct", "'correct'"),
                                  ("pp-9999", "correct",
                                   "resolutions.csv resolves the item pp-9999, which is in no "
                                   "sample")):
            with self.subTest(item=item, label=label):
                write_csv(self.dir / "labels" / "resolutions.csv",
                          [{"item": item, "label": label, "tool": "", "paths": "", "note": ""}])
                rc, err = self.run_report()
                self.assertEqual(rc, 1)
                self.assertIn(said, err)
                self.assertFalse((self.dir / "report.json").exists())

    def test_cohens_kappa_corrects_for_the_agreement_by_chance(self):
        """Three of four pairs agree. The passes' marginals, 3:1 and 2:2, give an agreement of
        0.5 by chance, and kappa is therefore (0.75 - 0.5) / (1 - 0.5) = 0.5."""
        labels = {f"i{n}": {"1": {"label": a}, "2": {"label": b}}
                  for n, (a, b) in enumerate((("x", "x"), ("x", "x"), ("y", "y"), ("x", "y")))}
        self.assertEqual(_mod.agreement(labels), {"items": 4, "agreement": 0.75, "kappa": 0.5})

    def test_an_uncertain_pass_sends_its_item_to_review(self):
        self.labels["path_precision"] = [("pp-0001", "1", "correct", {"uncertain": "yes"}),
                                         ("pp-0002", "1", "correct", {})]
        self.write_labels()
        self.assertEqual(self.run_report()[0], 0)
        self.assertEqual([(r["item"], r["why"]) for r in read_csv(self.dir / "review.csv")],
                         [("pp-0001", "uncertain")])

    def test_commit_recall_counts_a_commit_found_only_for_the_tool_that_it_credits(self):
        """A commit that credits Cursor and that the heuristics credit to Claude is missed."""
        self.labels["commit_recall"] = [("cr-0001", "1", "credits", {"tool": "Cursor"}),
                                        ("cr-0002", "1", "none", {}),
                                        ("cr-0003", "1", "credits", {"tool": "Copilot"})]
        self.write_labels()
        self.assertEqual(self.run_report()[0], 0)
        recall = json.loads((self.dir / "report.json").read_text())["commit_recall"]
        self.assertEqual((recall["recall"]["successes"], recall["recall"]["n"]), (1, 2))
        self.assertEqual(recall["per_tool"]["Cursor"]["successes"], 0)

    def test_a_missing_label_or_one_the_criteria_do_not_allow_exits_1(self):
        self.labels["commit_recall"] = self.labels["commit_recall"][:2]
        self.write_labels()
        rc, err = self.run_report()
        self.assertEqual(rc, 1)
        self.assertIn("1 item(s) of the sample commit_recall have no label", err)
        self.labels["commit_recall"].append(("cr-0003", "1", "maybe", {}))
        self.write_labels()
        rc, err = self.run_report()
        self.assertEqual(rc, 1)
        self.assertIn("'maybe'", err)
        self.assertFalse((self.dir / "report.json").exists())

    def add_imports_and_planning_documents(self) -> None:
        """The samples of imports.csv and planning-documents.csv with their labels: two cells of
        imports, one of 100 rows with two of three items correct and one of 10 rows with one of
        two correct, and two frameworks, one of 40 rows with three of four items correct and one
        of 60 rows with both of its two items correct, which the draw took from the
        repositories that the evidence links."""
        write_csv(self.dir / "import_precision.csv", [
            {"item": f"im-000{n}", "stratum": stratum, "stratum_rows": rows, "from_evidence": ""}
            for n, (stratum, rows) in enumerate(
                [("claude-import-01/found", 100)] * 3 + [("copilot-import-02/missing", 10)] * 2,
                start=1)])
        write_csv(self.dir / "planning_precision.csv", [
            {"item": f"pd-000{n}", "stratum": stratum, "stratum_rows": rows, "from_evidence": mark}
            for n, (stratum, rows, mark) in enumerate(
                [("openspec", 40, "")] * 4 + [("backlog-md", 60, "yes")] * 2, start=1)])
        self.labels["import_precision"] = [
            ("im-0001", "1", "correct", {}), ("im-0002", "1", "correct", {}),
            ("im-0003", "1", "not_loaded", {}), ("im-0004", "1", "correct", {}),
            ("im-0005", "1", "wrong_target", {"paths": "docs/other.md"}),
            ("im-0004", "2", "correct", {}), ("im-0005", "2", "correct", {})]
        self.labels["planning_precision"] = [
            ("pd-0001", "1", "correct", {}), ("pd-0002", "1", "correct", {}),
            ("pd-0003", "1", "wrong_framework", {"tool": "spec-kit", "uncertain": "yes"}),
            ("pd-0004", "1", "correct", {}), ("pd-0005", "1", "correct", {}),
            ("pd-0006", "1", "correct", {})]

    def test_an_evaluation_without_the_imports_and_planning_documents_reports_five_samples(self):
        """An evaluation drawn without the samples of imports.csv and planning-documents.csv
        is reported over the samples that it holds."""
        self.write_labels()
        rc, err = self.run_report()
        self.assertEqual(rc, 0, err)
        report = json.loads((self.dir / "report.json").read_text())
        self.assertEqual(set(report), {"path_precision", "path_recall", "commit_precision",
                                       "commit_recall", "mentions"})
        self.assertNotIn("Import precision", (self.dir / "report.md").read_text())

    def test_the_precision_of_imports_and_planning_documents_is_weighted_by_the_strata(self):
        self.add_imports_and_planning_documents()
        self.write_labels()
        rc, err = self.run_report()
        self.assertEqual(rc, 0, err)
        report = json.loads((self.dir / "report.json").read_text())
        imports = report["import_precision"]
        # (100 * 2/3 + 10 * 1/2) / 110 = 0.65152. The variance takes the adjusted proportions
        # 4/7 over 7 and 3/6 over 6, by hand:
        # (100/110)^2 * (4/7)(3/7)/7 * 97/99 + (10/110)^2 * (1/2)(1/2)/6 * 8/9
        half = 1.959964 * math.sqrt((100 / 110) ** 2 * (4 / 7) * (3 / 7) / 7 * 97 / 99
                                    + (10 / 110) ** 2 * 0.25 / 6 * 8 / 9)
        self.assertAlmostEqual(imports["overall"]["estimate"], 0.6515, places=4)
        self.assertAlmostEqual(imports["overall"]["low"], 0.65152 - half, places=3)
        self.assertAlmostEqual(imports["overall"]["high"], 0.65152 + half, places=3)
        self.assertEqual((imports["overall"]["items"], imports["overall"]["rows"]), (5, 110))
        self.assertEqual({k: (v["successes"], v["n"], v["rows"])
                          for k, v in imports["per_stratum"].items()},
                         {"claude-import-01/found": (2, 3, 100),
                          "copilot-import-02/missing": (1, 2, 10)})
        self.assertEqual(imports["labels"], {"correct": 3, "not_loaded": 1, "wrong_target": 1})
        self.assertEqual(imports["agreement"], {"items": 2, "agreement": 0.5, "kappa": 0.0})
        # No cell of the imports was drawn from the repositories of the evidence.
        self.assertEqual(imports["strata_from_evidence"], {})
        self.assertEqual(imports["without_strata_from_evidence"], imports["overall"])
        planning = report["planning_precision"]
        # Over both frameworks: (40 * 3/4 + 60 * 2/2) / 100 = 0.9. The variance takes the
        # adjusted proportions 5/8 over 8 and 4/6 over 6, by hand:
        # (40/100)^2 * (5/8)(3/8)/8 * 36/39 + (60/100)^2 * (4/6)(2/6)/6 * 58/59
        half = 1.959964 * math.sqrt((40 / 100) ** 2 * (5 / 8) * (3 / 8) / 8 * 36 / 39
                                    + (60 / 100) ** 2 * (4 / 6) * (2 / 6) / 6 * 58 / 59)
        self.assertEqual(planning["overall"]["estimate"], 0.9)
        self.assertAlmostEqual(planning["overall"]["low"], 0.9 - half, places=3)
        # Here and without Backlog.md, the estimate plus half the interval exceeds 1, and the
        # interval ends at 1.
        self.assertEqual(planning["overall"]["high"], 1.0)
        self.assertEqual((planning["overall"]["items"], planning["overall"]["rows"]), (6, 100))
        # Without Backlog.md: 3 of 4 of OpenSpec, and the finite population correction of 4 of
        # 40 rows.
        without = planning["without_strata_from_evidence"]
        half = 1.959964 * math.sqrt((5 / 8) * (3 / 8) / 8 * 36 / 39)
        self.assertEqual(without["estimate"], 0.75)
        self.assertAlmostEqual(without["low"], 0.75 - half, places=3)
        self.assertEqual(without["high"], 1.0)
        self.assertEqual((without["items"], without["rows"]), (4, 40))
        self.assertEqual(planning["strata_from_evidence"], {"backlog-md": 60})
        self.assertEqual({k: v["from_evidence"] for k, v in planning["per_stratum"].items()},
                         {"backlog-md": True, "openspec": False})
        review = {(r["sample"], r["item"], r["why"]) for r in read_csv(self.dir / "review.csv")}
        self.assertIn(("import_precision", "im-0005", "passes disagree"), review)
        self.assertIn(("planning_precision", "pd-0003", "uncertain"), review)
        rendered = (self.dir / "report.md").read_text()
        imports_md, planning_md = rendered.split("## Planning document precision")
        self.assertIn("## Import precision", imports_md)
        self.assertIn("| claude-import-01/found | 100 | 0.667 [", imports_md)
        self.assertNotIn("Precision without the strata", imports_md)
        self.assertIn("weighted by the rows of each stratum: 0.900 [", planning_md)
        self.assertIn("Precision without the strata drawn from the repositories that the "
                      "evidence of the heuristics links: 0.750 [", planning_md)
        self.assertIn("backlog-md (60 rows).", planning_md)

    def test_a_sample_drawn_from_the_evidence_alone_has_no_precision_without_those_strata(self):
        """Where the draw took every framework from the repositories that the evidence links,
        the precision without these frameworks covers no item and states no estimate."""
        self.add_imports_and_planning_documents()
        rows = read_csv(self.dir / "planning_precision.csv")
        write_csv(self.dir / "planning_precision.csv",
                  [{**row, "from_evidence": "yes"} for row in rows])
        self.write_labels()
        rc, err = self.run_report()
        self.assertEqual(rc, 0, err)
        planning = json.loads((self.dir / "report.json").read_text())["planning_precision"]
        self.assertEqual(planning["overall"]["estimate"], 0.9)
        self.assertEqual(planning["without_strata_from_evidence"],
                         {"estimate": None, "low": None, "high": None, "items": 0, "rows": 0})
        self.assertEqual(planning["strata_from_evidence"], {"backlog-md": 60, "openspec": 40})
        self.assertIn("evidence of the heuristics links: no items over 0 planning documents",
                      (self.dir / "report.md").read_text())

    def test_the_labels_of_imports_and_planning_documents_follow_their_criteria(self):
        """A label of another sample, such as `not_configuration`, is not allowed for an import
        or a planning document, and labels of a sample that the directory lacks exit 1."""
        self.add_imports_and_planning_documents()
        for name, label in (("import_precision", "not_configuration"),
                            ("planning_precision", "wrong_tool")):
            with self.subTest(sample=name):
                saved = list(self.labels[name])
                self.labels[name][0] = (self.labels[name][0][0], "1", label, {})
                self.write_labels()
                rc, err = self.run_report()
                self.assertEqual(rc, 1)
                self.assertIn(repr(label), err)
                self.labels[name] = saved
        self.write_labels()
        (self.dir / "planning_precision.csv").unlink()
        rc, err = self.run_report()
        self.assertEqual(rc, 1)
        self.assertIn("planning_precision.csv is missing", err)
        self.assertFalse((self.dir / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
