"""Tests for agentcontext/steps/import_detection.py. They cover the rows of `output/imports.csv`
over a validated tree and an imported tree, the category and the archive of each target, the
planning documents where their CSV exists, and the inputs without which the step writes nothing.
They also check that the step and the cloning step agree on the files that the imports reach.

Requirements: Python 3.14, git on PATH (stdlib only)
Run with:  python3 -m unittest tests.test_steps_import_detection -v
"""

import contextlib
import csv
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agentcontext import heuristics, imports, planning_schema
from agentcontext.steps import collected_file_cleanup
from agentcontext.collector import CollectionConfig, process_repo
from agentcontext.steps import import_detection as step
from tests import _ambient_env
from tests._fixtures import _init_git_repo

REPOS_COLUMNS = [*heuristics.META_COLUMNS, "claude", "github_link", "dataset_version"]
REPO = {"repo_name": "owner/repo", "scanned_at": "2026-10-01T00:00:00Z", "branch": "main",
        "head_sha": "a" * 40, "head_commit_date": "2026-09-30T00:00:00Z", "claude": "TRUE",
        "github_link": "https://github.com/owner/repo", "dataset_version": "1.0.0"}


@contextlib.contextmanager
def _in_dir(path: Path):
    previous = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _write_csv(path: Path, columns: list, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def _write_files(root: Path, files: dict) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class _StageDirectory:
    """A stage directory with `output/repos.csv`, the mechanism CSVs, a validated tree and an
    imported tree."""

    def __init__(self, test: unittest.TestCase, listed: dict):
        self.root = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        _write_csv(self.root / "output" / "repos.csv", REPOS_COLUMNS, [REPO])
        for mechanism, (filename, column) in heuristics.MECHANISM_CSVS.items():
            _write_csv(self.root / "output" / filename, ["repo_name", column],
                       [{"repo_name": "owner/repo", column: path}
                        for path in listed.get(mechanism, [])])
        self.validated = self.root / "data" / "validated" / "owner§repo"
        self.imported = self.root / "data" / "imported" / "owner§repo"
        self.validated.mkdir(parents=True)
        self.imported.mkdir(parents=True)

    def run(self) -> tuple[int, list]:
        with _in_dir(self.root):
            code = step.main([])
        csv_path = self.root / "output" / "imports.csv"
        if not csv_path.exists():
            return code, []
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            self.columns = reader.fieldnames
            return code, list(reader)


class TestTheRows(unittest.TestCase):

    def setUp(self):
        self.stage = _StageDirectory(self, {
            "context_file": ["CLAUDE.md", "AGENTS.md"],
            "skills": [".claude/skills/review/SKILL.md"],
            "other": ["notes.txt"],
        })
        _write_files(self.stage.validated, {
            "CLAUDE.md": "# Claude\n\n@AGENTS.md\n@docs/style.md\n@.claude/skills/review/ref.md\n"
                         "@notes.txt\n@missing.md\n@../outside.md\n",
            "AGENTS.md": "Agents\n",
            ".claude/skills/review/SKILL.md": "---\nname: review\n---\n",
            "notes.txt": "kept\n",
        })
        _write_files(self.stage.imported, {
            "AGENTS.md": "Agents\n",
            "docs/style.md": "Style\n@deep.md\n",
            "docs/deep.md": "one\ntwo\nthree\n",
            ".claude/skills/review/ref.md": "Ref\n",
            "notes.txt": "kept\n",
        })

    def _claude(self, rows: list) -> dict:
        return {row["import"]: row for row in rows if row["heuristic"] == "claude-import-01"}

    def test_the_columns(self):
        code, _rows = self.stage.run()
        self.assertEqual(code, 0)
        self.assertEqual(self.stage.columns, step.COLUMNS)

    def test_the_csv_ends_each_record_with_lf(self):
        """`imports.csv` ends each record with LF, as every CSV of the pipeline does, rather
        than with the CRLF that the csv module writes by default."""
        code, rows = self.stage.run()
        self.assertEqual(code, 0)
        self.assertTrue(rows)
        self.assertNotIn(b"\r\n", (self.stage.root / "output" / "imports.csv").read_bytes())

    def test_one_row_per_import_with_what_it_reaches(self):
        _code, rows = self.stage.run()
        claude = self._claude(rows)
        self.assertEqual(
            {written: (row["target"], row["target_status"], row["target_category"],
                       row["target_location"], row["depth"])
             for written, row in claude.items()},
            {"@AGENTS.md": ("AGENTS.md", "found", "context_file", "repos_data.7z", "1"),
             "@docs/style.md": ("docs/style.md", "found", "imported", "imported_files.7z", "1"),
             "@deep.md": ("docs/deep.md", "found", "imported", "imported_files.7z", "2"),
             "@.claude/skills/review/ref.md": (".claude/skills/review/ref.md", "found", "skills",
                                               "imported_files.7z", "1"),
             "@notes.txt": ("notes.txt", "found", "other", "repos_data.7z", "1"),
             "@missing.md": ("missing.md", "missing", "", "", "1"),
             "@../outside.md": ("../outside.md", "outside", "", "", "1")})

    def test_the_importing_file_and_the_link_to_the_line(self):
        _code, rows = self.stage.run()
        claude = self._claude(rows)
        first, deep = claude["@AGENTS.md"], claude["@deep.md"]
        self.assertEqual((first["importer"], first["importer_category"], first["root"],
                          first["tool"], first["line"]),
                         ("CLAUDE.md", "context_file", "CLAUDE.md", "claude", "3"))
        self.assertEqual(first["github_link"],
                         "https://github.com/owner/repo/blob/main/CLAUDE.md#L3")
        self.assertEqual((deep["importer"], deep["importer_category"], deep["root"],
                          deep["line"], deep["target_line_count"]),
                         ("docs/style.md", "imported", "CLAUDE.md", "2", "3"))
        self.assertEqual(deep["github_link"],
                         "https://github.com/owner/repo/blob/main/docs/style.md#L2")
        self.assertEqual({row["dataset_version"] for row in rows}, {"1.0.0"})
        self.assertEqual({row["head_sha"] for row in rows}, {"a" * 40})

    def test_each_tool_has_its_own_chain(self):
        """Claude Code and Copilot CLI read the imports of CLAUDE.md. Claude Code reads no
        AGENTS.md beside a CLAUDE.md, and the AGENTS.md that Copilot CLI and Antigravity read
        imports nothing."""
        _code, rows = self.stage.run()
        self.assertEqual(sorted({(row["heuristic"], row["root"]) for row in rows}),
                         [("claude-import-01", "CLAUDE.md"), ("copilot-import-03", "CLAUDE.md")])
        copilot = {row["import"]: row for row in rows if row["heuristic"] == "copilot-import-03"}
        self.assertEqual(copilot["@deep.md"]["depth"], "2")

    def test_an_import_written_twice_on_one_line_is_one_row(self):
        """The tool reads an import once, however often a line repeats it. A second row for
        the repeated import would also give the regression check one key on two rows."""
        claude = self.stage.validated / "CLAUDE.md"
        claude.write_text(claude.read_text() + "See @AGENTS.md, or @AGENTS.md again.\n")
        _code, rows = self.stage.run()
        keys = [tuple(row[column] for column in ("repo_name", *step.ROW_KEY)) for row in rows]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(sum(1 for row in rows if row["heuristic"] == "claude-import-01"
                             and row["line"] == "9"), 1)

    def test_a_target_whose_collected_copy_differs_is_in_the_archive_of_the_imported_files(self):
        """The validated tree holds a symlink marker where the clone had a link, and the
        release publishes the file that the tool read."""
        (self.stage.validated / "AGENTS.md").write_text("docs/agents.md\n")
        _code, rows = self.stage.run()
        self.assertEqual(self._claude(rows)["@AGENTS.md"]["target_location"],
                         "imported_files.7z")

    def test_a_planning_document_where_its_csv_exists(self):
        _code, rows = self.stage.run()
        self.assertEqual(self._claude(rows)["@docs/style.md"]["target_category"], "imported")
        _write_csv(self.stage.root / "output" / planning_schema.CSV_NAME,
                   ["repo_name", planning_schema.PATH_COLUMN],
                   [{"repo_name": "owner/repo", planning_schema.PATH_COLUMN: "docs/style.md"}])
        _code, rows = self.stage.run()
        self.assertEqual(self._claude(rows)["@docs/style.md"]["target_category"],
                         "planning_document")

    def test_the_other_outputs_are_unchanged(self):
        before = {path.name: path.read_bytes() for path in (self.stage.root / "output").iterdir()}
        self.stage.run()
        after = {path.name: path.read_bytes() for path in (self.stage.root / "output").iterdir()}
        self.assertEqual(sorted(set(after) - set(before)), ["imports.csv"])
        self.assertEqual({name: after[name] for name in before}, before)


class TestTheLinesOfATarget(unittest.TestCase):
    """The detection step publishes a blank `line_count` for a file that is no text, and it reads
    a file in UTF-16 by its byte order mark. `target_line_count` counts each such file in the
    same way."""

    def test_a_target_that_is_no_text_has_no_line_count_and_one_in_utf_16_its_lines(self):
        stage = _StageDirectory(self, {"context_file": ["CLAUDE.md"]})
        _write_files(stage.validated, {"CLAUDE.md": "@secret.md\n@wide.md\n@plain.md\n"})
        (stage.imported / "secret.md").write_bytes(b"\0GITCRYPT\0" + b"\x8f\n" * 40)
        (stage.imported / "wide.md").write_bytes("one\r\ntwo\r\nthree\r\n".encode("utf-16"))
        _write_files(stage.imported, {"plain.md": "one\ntwo\n"})
        code, rows = stage.run()
        self.assertEqual(code, 0)
        self.assertEqual({row["target"]: (row["target_status"], row["target_line_count"])
                          for row in rows if row["heuristic"] == "claude-import-01"},
                         {"secret.md": ("found", ""), "wide.md": ("found", "3"),
                          "plain.md": ("found", "2")})

    def test_a_form_feed_or_a_line_separator_inside_a_line_starts_no_line(self):
        """The detection step counts the lines that LF, CRLF or CR end, and a U+2028 in a code
        span or a form feed inside a path is inside a line."""
        stage = _StageDirectory(self, {"context_file": ["CLAUDE.md"]})
        _write_files(stage.validated, {"CLAUDE.md": "@target.md\n"})
        _write_files(stage.imported,
                     {"target.md": "Keep `a\u2028b`.\nLinking esp32\x0cfirmware\n"})
        code, rows = stage.run()
        self.assertEqual(code, 0)
        self.assertEqual([(row["target"], row["target_line_count"])
                          for row in rows if row["heuristic"] == "claude-import-01"],
                         [("target.md", "2")])


class TestWhatTheCloningStepReads(unittest.TestCase):
    """The import detection step reads the validated tree, and the cloning step reads the clone.
    The step therefore has to read a file of the validated tree as the cloning step read the
    same file in the clone."""

    def test_an_empty_claude_md_beside_agents_md_keeps_claude_code_from_reading_it(self):
        """Claude Code reads no AGENTS.md in a directory with a CLAUDE file, empty or not. The
        collected file cleanup keeps an empty CLAUDE file, so that the step does not start a
        chain of Claude Code at the AGENTS.md that the cloning step did not follow."""
        stage = _StageDirectory(self, {"context_file": ["AGENTS.md", "CLAUDE.md"]})
        _write_files(stage.validated, {"CLAUDE.md": "\n",
                                       "AGENTS.md": "Read @docs/notes.md first.\n"})
        _write_files(stage.imported, {"docs/notes.md": "Notes.\n"})
        collected_file_cleanup.scan_and_clean_repo(str(stage.validated), [])
        self.assertTrue((stage.validated / "CLAUDE.md").is_file())
        _code, rows = stage.run()
        self.assertEqual([row for row in rows if row["tool"] == "claude"], [])

    def test_a_file_of_one_import_is_its_own_text(self):
        """A CLAUDE.md with a byte order mark and one import, as an editor saves it, is the
        file's own text and not the marker of a link. So is a file with one reference as its
        whole text. The cloning step read the import of such a file, and the step reads it
        too."""
        for text in ("\ufeff@./docs/guide.md\n", "(@docs/guide.md)\n"):
            with self.subTest(text=text):
                stage = _StageDirectory(self, {"context_file": ["CLAUDE.md"]})
                _write_files(stage.validated, {"CLAUDE.md": text})
                _write_files(stage.imported, {"docs/guide.md": "Guide.\n"})
                _code, rows = stage.run()
                self.assertIn(("docs/guide.md", "found"),
                              {(row["target"], row["target_status"]) for row in rows})

    def test_the_marker_of_a_link_that_neither_tree_follows_is_no_text(self):
        """`.github/copilot-instructions.md` links into a scoped package of a monorepo. The
        cloning step found no import in the file that the link leads to and copied nothing. The
        `@acme/docs/copilot.md` in the marker is no import of that file."""
        stage = _StageDirectory(self, {"context_file": [".github/copilot-instructions.md"]})
        _write_files(stage.validated, {".github/copilot-instructions.md":
                                       "../packages/@acme/docs/copilot.md\n"})
        code, rows = stage.run()
        self.assertEqual((code, rows), (0, []))


class TestTheInputs(unittest.TestCase):

    def test_without_the_imported_tree_the_step_writes_nothing(self):
        stage = _StageDirectory(self, {})
        shutil.rmtree(stage.root / "data" / "imported")
        code, rows = stage.run()
        self.assertEqual((code, rows), (1, []))
        self.assertFalse((stage.root / "output" / "imports.csv").exists())

    def test_without_the_detection_csv_the_step_writes_nothing(self):
        stage = _StageDirectory(self, {})
        (stage.root / "output" / "repos.csv").unlink()
        self.assertEqual(stage.run(), (1, []))

    def test_a_repository_without_imports_has_no_rows(self):
        stage = _StageDirectory(self, {"context_file": ["CLAUDE.md"]})
        _write_files(stage.validated, {"CLAUDE.md": "# Claude\n\nNo imports.\n"})
        self.assertEqual(stage.run(), (0, []))


class TestALargeTarget(unittest.TestCase):

    def test_an_imported_file_of_minified_css_gives_a_small_csv(self):
        """An AGENTS.md names a stylesheet of a hundred kilobytes on one line, which Copilot CLI
        reads and parses for the next hop. If each `@media` of the line started an import that
        ran to the end of the line, the CSV would grow with the square of the line's length."""
        stage = _StageDirectory(self, {"context_file": ["AGENTS.md"]})
        _write_files(stage.validated, {"AGENTS.md": "The styles are in @dist/app.min.css\n"})
        _write_files(stage.imported,
                     {"dist/app.min.css": "@media(min-width:1px){.a{b:c}}" * 3400})
        code, rows = stage.run()
        self.assertEqual(code, 0)
        self.assertLess((stage.root / "output" / step.CSV_NAME).stat().st_size, 1_000_000)
        self.assertLessEqual(len(rows), 20)


class TestAStubOfOtherImportHeuristics(unittest.TestCase):
    """After a retry that fails in every round, or while the time bound that stopped the retry
    holds, the cloning step keeps an earlier collection with its imported tree, whatever import
    heuristics wrote that tree. A stub that those heuristics wrote for a file that no chain read
    is not the file that the current heuristics read."""

    def setUp(self):
        self.stage = _StageDirectory(self, {"rules": [".claude/rules/style.md"]})
        self.clone = self.stage.root / "clone"
        _write_files(self.clone, {".claude/rules/style.md": "See @../../docs/guide.mdx\n",
                                  "docs/guide.mdx": "# Guide\n\nline two\nline three\n"})
        _write_files(self.stage.validated,
                     {".claude/rules/style.md": "See @../../docs/guide.mdx\n"})
        rows = imports.load_import_patterns()

        def extensions(row: dict, mdx: bool) -> dict:
            read = [e for e in row["extensions"].split("|") if e != ".mdx"]
            return dict(row, extensions="|".join([*read, ".mdx"] if mdx else read))

        self.earlier = [extensions(row, False) if row["tool"] == "claude" else row for row in rows]
        self.current = [extensions(row, True) if row["tool"] == "claude" else row for row in rows]

    def _run(self, rows: list) -> list:
        with mock.patch.object(imports, "load_import_patterns", lambda *args, **kwargs: rows), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            code, published = self.stage.run()
        self.assertEqual(code, 0, err.getvalue())
        self.log = err.getvalue()
        return [(row["target"], row["target_status"], row["target_location"],
                 row["target_line_count"]) for row in published]

    def test_a_stub_that_the_current_heuristics_read_is_not_found(self):
        """Import heuristics that do not read `.mdx` write `docs/guide.mdx` as a stub of NUL
        bytes and name it in the list beside the repository's directory. Under heuristics that
        read `.mdx`, the import is `not_copied`, without an archive or a line count, rather
        than `found` with the stub's bytes as the file. The step logs the repository."""
        imports.collect_imports(self.clone, self.stage.validated, self.stage.imported,
                                rows=self.earlier)
        self.assertEqual(set((self.stage.imported / "docs" / "guide.mdx").read_bytes()), {0})
        self.assertEqual(imports.read_stubs(self.stage.imported), {"docs/guide.mdx"})
        self.assertEqual(self._run(self.earlier), [("docs/guide.mdx", "not_read", "", "")])
        self.assertEqual(self._run(self.current), [("docs/guide.mdx", "not_copied", "", "")])
        self.assertIn("Found imports of a stub target (target_status not_copied) in 1 "
                      "repositories, such as owner/repo", self.log)

    def test_a_collection_under_the_current_heuristics_copies_the_file(self):
        """A new collection replaces the stub with the file and removes the list of stubs."""
        imports.collect_imports(self.clone, self.stage.validated, self.stage.imported,
                                rows=self.earlier)
        shutil.rmtree(self.stage.imported)
        imports.collect_imports(self.clone, self.stage.validated, self.stage.imported,
                                rows=self.current)
        self.assertFalse(imports.stub_list_path(self.stage.imported).exists())
        self.assertEqual(self._run(self.current),
                         [("docs/guide.mdx", "found", "imported_files.7z", "4")])

    def test_a_list_that_is_no_array_of_paths_stops_the_step(self):
        imports.collect_imports(self.clone, self.stage.validated, self.stage.imported,
                                rows=self.earlier)
        imports.stub_list_path(self.stage.imported).write_text('{"docs/guide.mdx": 1}\n')
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.stage.run(), (1, []))
        self.assertIn("Could not read the list of stubs of owner/repo", err.getvalue())


class TestAStubAtThePathOfAnImportingFile(unittest.TestCase):
    """`.github/copilot-instructions.md` imports `@../AGENTS.md`. Copilot CLI does not read that
    target, and the cloning step writes a stub at `AGENTS.md`. AGENTS.md is itself an importing
    file of Claude Code (no CLAUDE.md), of Copilot CLI and of Antigravity, and it imports
    `@docs/style.md`. The rows over the validated tree and the imported tree are the chains
    that the tools read in the clone, as the cloning step follows them."""

    def _rows(self, agents: bytes) -> tuple[list, list]:
        stage = _StageDirectory(self, {"context_file": ["AGENTS.md",
                                                        ".github/copilot-instructions.md"]})
        clone = stage.root / "clone"
        _write_files(clone, {".github/copilot-instructions.md": "Read @../AGENTS.md first.\n",
                             "docs/style.md": "Use tabs.\n"})
        (clone / "AGENTS.md").write_bytes(agents)
        for rel in (".github/copilot-instructions.md", "AGENTS.md"):
            (stage.validated / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(clone / rel, stage.validated / rel)
        rows, tree = imports.load_import_patterns(), imports.CloneTree(clone)
        in_the_clone = sorted(
            (record.tool, record.root, record.importer, record.target, record.status)
            for row, root in imports.importing_files(rows, clone, paths_of(stage.validated), tree)
            for record in imports.chain(row, root, tree))
        imports.collect_imports(clone, stage.validated, stage.imported, rows=rows)
        with contextlib.redirect_stderr(io.StringIO()):
            code, published = stage.run()
        self.assertEqual(code, 0)
        self.assertIn("AGENTS.md", imports.read_stubs(stage.imported))
        return in_the_clone, sorted((row["tool"], row["root"], row["importer"], row["target"],
                                     row["target_status"]) for row in published)

    def test_an_importing_file_that_is_not_utf8_keeps_its_imports(self):
        """Copilot CLI reads no AGENTS.md with a Latin-1 e acute."""
        in_the_clone, published = self._rows(b"Use @docs/style.md for style. Caf\xe9.\n")
        self.assertIn(("claude", "AGENTS.md", "AGENTS.md", "docs/style.md", "found"),
                      in_the_clone)
        self.assertEqual(published, in_the_clone)

    def test_an_importing_file_over_a_limit_keeps_its_imports(self):
        """Copilot CLI reads no file of more than 512 KiB through an import."""
        in_the_clone, published = self._rows(b"Use @docs/style.md for style.\n"
                                             + b"x" * (600 * 1024) + b"\n")
        self.assertIn(("copilot", ".github/copilot-instructions.md",
                       ".github/copilot-instructions.md", "AGENTS.md", "over_limit"),
                      in_the_clone)
        self.assertEqual(published, in_the_clone)


def paths_of(root: Path) -> list:
    """The repository-relative paths of the files under `root`, sorted."""
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file())


class TestTheCloningStepAndTheStepAgree(unittest.TestCase):
    """Over a real clone, the files that the step finds are the files that the cloning step
    copied to the imported tree."""

    _saved = {}

    @classmethod
    def setUpClass(cls):
        _ambient_env.take(cls._saved)

    @classmethod
    def tearDownClass(cls):
        _ambient_env.restore(cls._saved)

    def test_the_found_targets_are_the_imported_files(self):
        """The imported tree also holds a file that the tools do not read, a PNG image. The
        step therefore gives it `not_read`, as the chain did in the clone, rather than
        `missing`."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        remote = tmp / "remote"
        _init_git_repo(remote)
        _write_files(remote, {
            "CLAUDE.md": "# Claude\n\n@docs/a.md and @link.md\n\nThe logo is @logo.png\n",
            ".github/copilot-instructions.md": "Use @../docs/c.md and @missing.md\n",
            "GEMINI.md": "@./docs/a.md\n",
            "docs/a.md": "@b.md\n", "docs/b.md": "B\n", "docs/c.md": "C\n",
            "sub/real.md": "@y.md\n", "sub/y.md": "Y\n"})
        (remote / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        os.symlink("sub/real.md", remote / "link.md")
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        stage = _StageDirectory(self, {"context_file": ["CLAUDE.md", "GEMINI.md",
                                                        ".github/copilot-instructions.md"]})
        shutil.rmtree(stage.validated)
        shutil.rmtree(stage.imported)
        config = CollectionConfig(collected_dir=stage.root / "data" / "validated",
                                  clone_dir=tmp / "clones", log_dir=tmp / "logs", max_tries=1,
                                  base_delay=0, measure_git=False,
                                  imported_dir=stage.root / "data" / "imported")
        process_repo(config, "owner/repo")
        _code, rows = stage.run()
        found = {row["target"] for row in rows if row["target_status"] == "found"}
        trees = imports.CollectedTrees(stage.validated, stage.imported)
        copied = {path.relative_to(stage.imported).as_posix()
                  for path in stage.imported.rglob("*") if path.is_file() or path.is_symlink()}
        self.assertEqual(found | {trees.real(target) for target in found} | {"logo.png"},
                         copied, "each found target, and the file that a linked target leads to")
        self.assertEqual(found, {"docs/a.md", "docs/b.md", "docs/c.md", "link.md", "sub/y.md"})
        self.assertEqual({row["target"] for row in rows if row["target_status"] == "missing"},
                         {".github/missing.md"})
        self.assertEqual(sorted((row["tool"], row["target_status"]) for row in rows
                                if row["target"] == "logo.png"),
                         [("claude", "not_read"), ("copilot", "not_read")])

    def test_a_large_target_that_no_tool_reads_is_a_stub(self):
        """No chain reads a table of five mebibytes, a video of six mebibytes or a small image.
        The imported tree holds stubs of them, which give the step the statuses of the clone
        without the files' bytes. A file copied whole could reach the imported tree and every
        handover at any size up to the clone's size bound."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        remote = tmp / "remote"
        _init_git_repo(remote)
        _write_files(remote, {"CLAUDE.md": "@data/train.csv\n\n@assets/demo.mp4\n\n"
                                           "@small.png\n"})
        large = {"data/train.csv": b"a,b\n" * (5 << 18),
                 "assets/demo.mp4": b"\x00\xff" * (3 << 20),
                 "small.png": b"\x89PNG\xff" * 20}
        for rel, content in large.items():
            (remote / rel).parent.mkdir(parents=True, exist_ok=True)
            (remote / rel).write_bytes(content)
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        stage = _StageDirectory(self, {"context_file": ["CLAUDE.md"]})
        shutil.rmtree(stage.validated)
        shutil.rmtree(stage.imported)
        config = CollectionConfig(collected_dir=stage.root / "data" / "validated",
                                  clone_dir=tmp / "clones", log_dir=tmp / "logs", max_tries=1,
                                  base_delay=0, measure_git=False,
                                  imported_dir=stage.root / "data" / "imported")
        process_repo(config, "owner/repo")
        _code, rows = stage.run()
        self.assertEqual(sorted((row["target"], row["tool"], row["target_status"])
                                for row in rows),
                         [("assets/demo.mp4", "claude", "not_read"),
                          ("assets/demo.mp4", "copilot", "over_limit"),
                          ("data/train.csv", "claude", "over_limit"),
                          ("data/train.csv", "copilot", "over_limit"),
                          ("small.png", "claude", "not_read"),
                          ("small.png", "copilot", "not_read")])
        for rel, content in large.items():
            with self.subTest(target=rel):
                stub = (stage.imported / rel).read_bytes()
                self.assertLessEqual(len(stub), 4194305)
                self.assertNotIn(b"a,b", stub)
                self.assertNotEqual(stub, content)

    def test_claude_code_review_counts_the_collected_review_md(self):
        """The root REVIEW.md takes most of the 150,000 bytes that Claude Code Review reads. Of
        its includes, b.md reaches the limit, and c.md, a byte smaller, stays below it. The
        step reads REVIEW.md in the validated tree, because the imported tree does not hold it,
        and gives each include the status of the clone. An include of a name in another case is
        `missing`. An include of a subdirectory and the includes of a subdirectory REVIEW.md
        have no row."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        remote = tmp / "remote"
        _init_git_repo(remote)
        review = ("# Review instructions\n\n@a.md\n@b.md\n@c.md\n@docs/rules.md\n@notes.md\n"
                  + "x" * 140_000 + "\n")
        remainder = 150_000 - len(review) - 1000
        _write_files(remote, {"REVIEW.md": review, "a.md": "a" * 1000, "b.md": "b" * remainder,
                              "c.md": "c" * (remainder - 1), "docs/rules.md": "D\n",
                              "docs/REVIEW.md": "@d.md\n", "docs/d.md": "D\n",
                              "Notes.md": "N\n"})
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        stage = _StageDirectory(self, {"context_file": ["REVIEW.md"]})
        shutil.rmtree(stage.validated)
        shutil.rmtree(stage.imported)
        config = CollectionConfig(collected_dir=stage.root / "data" / "validated",
                                  clone_dir=tmp / "clones", log_dir=tmp / "logs", max_tries=1,
                                  base_delay=0, measure_git=False,
                                  imported_dir=stage.root / "data" / "imported")
        process_repo(config, "owner/repo")
        self.assertFalse((stage.imported / "REVIEW.md").exists())
        code, rows = stage.run()
        self.assertEqual(code, 0)
        self.assertEqual([(row["heuristic"], row["root"], row["import"], row["line"],
                           row["target_status"]) for row in rows],
                         [("claude-import-07", "REVIEW.md", "@a.md", "3", "found"),
                          ("claude-import-07", "REVIEW.md", "@b.md", "4", "over_limit"),
                          ("claude-import-07", "REVIEW.md", "@c.md", "5", "found"),
                          ("claude-import-07", "REVIEW.md", "@notes.md", "7", "missing")])

    def test_claude_code_review_includes_the_skills_of_the_collected_review_md(self):
        """A skill that the root REVIEW.md includes is read from the commit up to 30,000 bytes
        and not through a link. The imported tree holds the skill that Code Review reads and
        stubs of the skill that is too large and of the skill behind a link, which give the
        step the statuses of the clone. A skill in a code fence has no row."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        remote = tmp / "remote"
        _init_git_repo(remote)
        skill = "---\nname: {0}\ndescription: Review {0}.\n---\n"
        _write_files(remote, {
            "REVIEW.md": "# Review instructions\n\n@.claude/skills/api/SKILL.md\n"
                         "@.claude/skills/large/SKILL.md\n@.claude/skills/linked/SKILL.md\n"
                         "```\n@.claude/skills/fenced/SKILL.md\n```\n",
            ".claude/skills/api/SKILL.md": skill.format("api"),
            ".claude/skills/large/SKILL.md": skill.format("large") + "x" * 30_000,
            ".claude/skills/fenced/SKILL.md": skill.format("fenced"),
            "shared/linked/SKILL.md": skill.format("linked")})
        os.symlink("../../shared/linked", remote / ".claude" / "skills" / "linked")
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        stage = _StageDirectory(self, {"context_file": ["REVIEW.md"]})
        shutil.rmtree(stage.validated)
        shutil.rmtree(stage.imported)
        config = CollectionConfig(collected_dir=stage.root / "data" / "validated",
                                  clone_dir=tmp / "clones", log_dir=tmp / "logs", max_tries=1,
                                  base_delay=0, measure_git=False,
                                  imported_dir=stage.root / "data" / "imported")
        process_repo(config, "owner/repo")
        self.assertEqual((stage.imported / ".claude/skills/api/SKILL.md").read_text(),
                         skill.format("api"))
        self.assertEqual(imports.read_stubs(stage.imported),
                         {".claude/skills/large/SKILL.md", "shared/linked/SKILL.md"})
        code, rows = stage.run()
        self.assertEqual(code, 0)
        self.assertEqual([(row["import"], row["line"], row["target_status"]) for row in rows],
                         [("@.claude/skills/api/SKILL.md", "3", "found"),
                          ("@.claude/skills/large/SKILL.md", "4", "over_limit"),
                          ("@.claude/skills/linked/SKILL.md", "5", "not_read")])

    def test_a_chain_from_a_root_claude_md_opens_an_import_in_another_case(self):
        """mzmine/mzmine's root claude.md, `@AGENTS.MD` beside AGENTS.md. Claude Code reads a
        root claude.md only on a file system that ignores case, where it opens AGENTS.MD as
        AGENTS.md. The cloning step copies AGENTS.md to the imported tree, and the step finds it
        there under the name that the directory gives it, on every file system. Copilot CLI
        reads the root claude.md also on Linux, where AGENTS.MD names no file."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        remote = tmp / "remote"
        _init_git_repo(remote)
        _write_files(remote, {"claude.md": "@AGENTS.MD\n", "AGENTS.md": "A\n"})
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        stage = _StageDirectory(self, {"context_file": ["AGENTS.md", "claude.md"]})
        shutil.rmtree(stage.validated)
        shutil.rmtree(stage.imported)
        config = CollectionConfig(collected_dir=stage.root / "data" / "validated",
                                  clone_dir=tmp / "clones", log_dir=tmp / "logs", max_tries=1,
                                  base_delay=0, measure_git=False,
                                  imported_dir=stage.root / "data" / "imported")
        process_repo(config, "owner/repo")
        self.assertEqual(paths_of(stage.imported), ["AGENTS.md"])
        self.assertEqual((stage.imported / "AGENTS.md").read_text(encoding="utf-8"), "A\n")
        code, rows = stage.run()
        self.assertEqual(code, 0)
        self.assertEqual(sorted((row["heuristic"], row["root"], row["import"], row["target"],
                                 row["target_status"], row["target_location"]) for row in rows),
                         [("claude-import-02", "claude.md", "@AGENTS.MD", "AGENTS.md", "found",
                           step.COLLECTED_ARCHIVE),
                          ("copilot-import-05", "claude.md", "@AGENTS.MD", "AGENTS.MD",
                           "missing", "")])

    @unittest.skipIf(sys.platform == "darwin", "macOS stores no name that is not valid UTF-8")
    def test_a_link_into_a_directory_with_a_name_that_is_not_utf8(self):
        """`sub/CLAUDE.md` is a link to `\\xffdir/CLAUDE.md`, which imports a file beside it.
        No CSV can hold such a name. The imports through the link therefore reach nothing, and
        neither the imported tree nor `imports.csv` holds the name."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        remote = tmp / "remote"
        _init_git_repo(remote)
        undecodable = os.fsencode(str(remote)) + b"/\xffdir"
        os.makedirs(undecodable)
        with open(undecodable + b"/CLAUDE.md", "wb") as f:
            f.write(b"@y.md\n")
        with open(undecodable + b"/y.md", "wb") as f:
            f.write(b"Y\n")
        (remote / "sub").mkdir()
        os.symlink(b"../\xffdir/CLAUDE.md", os.fsencode(str(remote / "sub" / "CLAUDE.md")))
        _write_files(remote, {"CLAUDE.md": "@sub/CLAUDE.md\n"})
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "files"],
                       capture_output=True, check=True)
        for key, value in (("GIT_CONFIG_COUNT", "1"),
                           ("GIT_CONFIG_KEY_0", f"url.file://{remote}.insteadOf"),
                           ("GIT_CONFIG_VALUE_0", "https://github.com/owner/repo.git")):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        stage = _StageDirectory(self, {"context_file": ["CLAUDE.md", "sub/CLAUDE.md"]})
        shutil.rmtree(stage.validated)
        shutil.rmtree(stage.imported)
        config = CollectionConfig(collected_dir=stage.root / "data" / "validated",
                                  clone_dir=tmp / "clones", log_dir=tmp / "logs", max_tries=1,
                                  base_delay=0, measure_git=False,
                                  imported_dir=stage.root / "data" / "imported")
        process_repo(config, "owner/repo")
        code, rows = stage.run()
        self.assertEqual(code, 0)
        text = (stage.root / "output" / step.CSV_NAME).read_bytes()
        self.assertNotIn(b"\xed\xb3\xbf", text)
        self.assertNotIn("\udcff", text.decode("utf-8"))
        names = [name for _dir, dirs, files in os.walk(os.fsencode(str(stage.imported)))
                 for name in dirs + files]
        self.assertEqual([name for name in names if b"\xff" in name], [])


class TestAFieldLongerThanTheDefaultLimit(unittest.TestCase):
    """The detection step joins every file that a context file references into
    `referenced_file`, with no bound on its length. The test runs in a process of its own,
    because the csv module's field limit is global and the modules that other suites import
    raise it."""

    PROGRAM = ('import sys\n'
               'from pathlib import Path\n'
               'from agentcontext.steps import import_detection\n'
               'import_detection.read_rows(Path(sys.argv[1]))\n')

    def test_the_step_reads_it(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "context-files.csv"
            references = ";".join(f"docs/d{number}.md" for number in range(20_000))
            path.write_text("repo_name,head_sha,referenced_file\n"
                            f"o/r,{'a' * 40},\"{references}\"\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-c", self.PROGRAM, str(path)], capture_output=True, text=True,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
