"""Tests for dev/export-public.py, the export of a commit as the tree of the public repository.

Strategy:
  - Each export runs the script in a subprocess over a repository that the test builds with git,
    with its own `.publicignore`, and reads the written tree.
  - The documents of this repository are checked with the functions of the script, so that the
    full verification finds a link to a left-out path before an export does.
  - The shipped `.publicignore` is checked against the tree of this repository, not against a
    copy of its entries.

Run with:  python3 -m unittest tests.test_public_export -v
Requirements: Python 3.14, git on PATH
"""

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentcontext import REPO_ROOT

SCRIPT = REPO_ROOT / "dev" / "export-public.py"
_spec = importlib.util.spec_from_file_location("export_public", SCRIPT)
export_public = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(export_public)


def _git(root: Path, *args: str) -> None:
    """Run a git command in `root` and fail the test when it fails."""
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repository(root: Path, files: dict) -> None:
    """A repository at `root` with `files` committed on main."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.org")
    _git(root, "config", "user.name", "Test")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "files")


def _export(root: Path, target: Path) -> subprocess.CompletedProcess:
    """Run the export of HEAD of the repository at `root` into `target`."""
    return subprocess.run([sys.executable, str(SCRIPT), str(target)], cwd=root,
                          capture_output=True, text=True)


class TestTheExport(unittest.TestCase):
    FILES = {
        ".publicignore": "# internal\nINTERNAL.md\nTODO.md\n_plans/\n",
        "CLAUDE.md": "# Instructions\n\nRead the guide.\n\n<!--internal-->\n"
                     "[INTERNAL.md](INTERNAL.md) names [the open work](TODO.md).\n"
                     "<!--/internal-->\n\nRun the tests.\n",
        "docs/guide.md": "The [README](../README.md) and "
                         "[the site](https://example.org/TODO.md).\n",
        "README.md": "# Readme\n",
        "INTERNAL.md": "# Internal work\n",
        "TODO.md": "# Open work\n",
        "_plans/plan.md": "# Plan\n",
        "_plans/notes/old.md": "# Old\n",
        "code.py": "print('kept')\n",
    }

    def run_export(self, files: dict) -> tuple[subprocess.CompletedProcess, Path]:
        """Export HEAD of a repository with `files` and return the result and the target."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        root, target = tmp / "repo", tmp / "public"
        root.mkdir()
        _repository(root, files)
        return _export(root, target), target

    def test_left_out_paths_and_internal_blocks_are_absent(self):
        result, target = self.run_export(self.FILES)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("INTERNAL.md", "TODO.md", "_plans"):
            self.assertFalse((target / name).exists(), name)
        self.assertEqual((target / "code.py").read_text(), "print('kept')\n")
        self.assertEqual((target / "CLAUDE.md").read_text(),
                         "# Instructions\n\nRead the guide.\n\nRun the tests.\n")
        self.assertEqual((target / "docs/guide.md").read_text(), self.FILES["docs/guide.md"])
        self.assertIn("left out 4 files", result.stderr)
        self.assertIn("removed 1 internal blocks", result.stderr)

    def test_a_link_to_a_left_out_path_outside_a_block_stops_the_export(self):
        files = {**self.FILES, "docs/guide.md": "The [open work](../TODO.md#heuristics).\n",
                 "docs/ref.md": "The [plan][plan].\n\n[plan]: ../_plans/plan.md\n"}
        result, _ = self.run_export(files)
        self.assertEqual(result.returncode, 1)
        self.assertIn("docs/guide.md: links ../TODO.md#heuristics", result.stderr)
        self.assertIn("docs/ref.md: links ../_plans/plan.md", result.stderr)

    def test_a_block_that_is_not_closed_stops_the_export(self):
        result, _ = self.run_export({**self.FILES, "README.md": "# Readme\n<!--internal-->\nx\n"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("README.md: the block of line 2 is not closed", result.stderr)

    def test_a_directory_that_is_not_empty_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, target = Path(tmp) / "repo", Path(tmp) / "public"
            root.mkdir()
            target.mkdir()
            (target / "stale.txt").write_text("stale", encoding="utf-8")
            _repository(root, self.FILES)
            result = _export(root, target)
            self.assertEqual(result.returncode, 1)
            self.assertIn("is not empty", result.stderr)
            self.assertEqual(sorted(path.name for path in target.iterdir()), ["stale.txt"])

    def test_a_commit_without_the_list_is_refused(self):
        result, _ = self.run_export({"README.md": "# Readme\n"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("has no .publicignore", result.stderr)


class TestTheInternalBlocks(unittest.TestCase):
    def test_a_block_is_removed_with_its_markers(self):
        kept, blocks, problems = export_public.without_internal_blocks(
            "a\n<!--internal-->\nb\n<!--/internal-->\nc\n")
        self.assertEqual((kept, blocks, problems), ("a\nc\n", 1, []))

    def test_a_block_between_blank_lines_leaves_one_of_them(self):
        kept, _, _ = export_public.without_internal_blocks(
            "a\n\n<!--internal-->\nb\n<!--/internal-->\n\nc\n")
        self.assertEqual(kept, "a\n\nc\n")

    def test_a_block_inside_a_block_and_a_close_without_an_open_are_problems(self):
        _, _, problems = export_public.without_internal_blocks(
            "<!--internal-->\n<!--internal-->\n<!--/internal-->\n<!--/internal-->\n")
        self.assertEqual(problems, ["line 2 opens a block inside the block of line 1",
                                    "line 4 closes no block"])


def _shipped_entries() -> tuple:
    """The entries of the shipped `.publicignore`."""
    return export_public.left_out_paths(
        (REPO_ROOT / ".publicignore").read_text(encoding="utf-8"))


def _tracked() -> list:
    """The paths that git tracks in this repository."""
    return subprocess.run(["git", "ls-files"], cwd=REPO_ROOT, capture_output=True,
                          text=True).stdout.splitlines()


class TestThisRepository(unittest.TestCase):
    def test_no_public_document_names_a_left_out_path_outside_a_block(self):
        """Every Markdown document that the public tree keeps would pass the export: its blocks
        are closed, and no link outside them names a left-out path."""
        entries = _shipped_entries()
        problems = []
        for name in _tracked():
            if name.endswith(".md") and not export_public.is_left_out(name, entries):
                text = (REPO_ROOT / name).read_text(encoding="utf-8")
                problems += export_public.public_text(name, text, entries)[2]
        self.assertEqual(problems, [])

    def test_every_entry_names_a_tracked_path(self):
        """An entry that names no tracked path leaves out nothing, as a misspelled name would. The
        public repository lacks every entry by design, and the check applies where the tree
        tracks any of them."""
        entries, tracked = _shipped_entries(), _tracked()

        def names_a_tracked_path(entry: str) -> bool:
            return any(name == entry or (entry.endswith("/") and name.startswith(entry))
                       for name in tracked)

        if not any(names_a_tracked_path(entry) for entry in entries):
            self.skipTest("the tree is the public export or no git checkout")
        for entry in entries:
            with self.subTest(entry=entry):
                self.assertTrue(names_a_tracked_path(entry), f"{entry} names no tracked path")


if __name__ == "__main__":
    unittest.main()
