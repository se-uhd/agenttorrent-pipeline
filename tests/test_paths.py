"""Tests for agentcontext.paths: the copy of one file and one directory, the list of a clone's
files, the per-tool collectors, the flags, the collected row, the shared paths and the symlink
markers.
Every case builds its tree under a temporary directory. The collectors that read git use a
repository built with `git init`.

Requirements: Python 3.14, git and rsync on PATH (stdlib only)
Run with:  python3 -m unittest tests.test_paths -v
"""

import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests._fixtures import (_init_git_repo, _row_field, deeply_nested_files,
                             dsm_writes_during_removal, files_with_long_integers)
from agentcontext import paths
from agentcontext.git import clone as _clone
from agentcontext.heuristics import load_config_path_patterns
from agentcontext.paths import (
    COLLECTED_CSV_HEADER,
    METADATA_COLUMNS as _METADATA_COLUMNS,
    build_csv_row,
    collect_agent_plugins,
    collect_claude,
    collect_codex,
    collect_copilot,
    collect_cursor,
    collect_opencode,
    collect_gemini,
    collect_repo,
    collect_shared,
    collect_skill_md,
    collect_symlink_targets,
    copy_dir,
    copy_file,
    find_dirs_by_name,
    local_state,
    tighten_flags,
    list_repo_files,
)
from agentcontext.collector import CollectionConfig, process_repo


def _tracked_dir(path: Path) -> Path:
    """A directory as a checkout contains one. git tracks no empty directory, and a directory
    therefore contains a file, while an empty directory is a submodule that the clone did not
    check out."""
    path.mkdir(parents=True, exist_ok=True)
    (path / ".keep").write_text("")
    return path


class TestCopyFile(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src_dir = self.tmp / "repo"
        self.src_dir.mkdir()
        self.dst_dir = self.tmp / "out"
        self.dst_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_regular_file_content_preserved(self):
        src = self.src_dir / "hello.txt"
        src.write_text("hello world\n")
        dest = self.dst_dir / "hello.txt"
        copy_file(src, dest)
        self.assertEqual(dest.read_text(), "hello world\n")

    def test_regular_file_metadata_preserved(self):
        import os
        src = self.src_dir / "hello.txt"
        src.write_text("content\n")
        os.utime(src, (1000000, 1000000))
        dest = self.dst_dir / "hello.txt"
        copy_file(src, dest)
        self.assertEqual(os.stat(dest).st_mtime, 1000000)

    def test_symlink_becomes_regular_file_with_target_string(self):
        target = self.src_dir / "real.txt"
        target.write_text("real content\n")
        link = self.src_dir / "link.txt"
        link.symlink_to("real.txt")
        dest = self.dst_dir / "link.txt"
        copy_file(link, dest)
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_text(), "real.txt\n")

    def test_broken_symlink_target_string_written(self):
        link = self.src_dir / "broken.txt"
        link.symlink_to("/nonexistent/path")
        dest = self.dst_dir / "broken.txt"
        copy_file(link, dest)
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_text(), "/nonexistent/path\n")

    def test_parent_directories_created(self):
        src = self.src_dir / "hello.txt"
        src.write_text("content\n")
        dest = self.dst_dir / "a" / "b" / "c" / "hello.txt"
        copy_file(src, dest)
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_text(), "content\n")

    def test_symlink_with_relative_target_preserved(self):
        link = self.src_dir / "link.txt"
        link.symlink_to("../other/file.txt")
        dest = self.dst_dir / "link.txt"
        copy_file(link, dest)
        self.assertEqual(dest.read_text(), "../other/file.txt\n")

    def test_dest_is_not_symlink_for_regular_file(self):
        src = self.src_dir / "hello.txt"
        src.write_text("content\n")
        dest = self.dst_dir / "hello.txt"
        copy_file(src, dest)
        self.assertFalse(dest.is_symlink())
        self.assertTrue(dest.is_file())


class TestCopiedFileSymlinkMarkers(unittest.TestCase):
    """A copied directory retains the evidence of a link to a file or a directory even when
    rsync omits the link."""

    def test_a_link_to_a_directory_outside_the_copied_directory_keeps_its_marker(self):
        """meshery/meshery links `.claude/hooks` to `../.agents/hooks`, and tursodatabase/turso
        links the `scripts` of a skill to a folder outside `.claude`. `os.walk` lists such a link
        among the directories, and rsync omits it. The collector copies no target of a link in a
        skill folder other than its SKILL.md, nor anything outside the repository."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
            outside = root / "outside"
            (outside / "notes").mkdir(parents=True)
            (outside / "notes" / "private.md").write_text("PRIVATE\n")
            for rel, body in {".claude/settings.json": "{}\n",
                              ".agents/hooks/check.sh": "exit 0\n",
                              ".claude/skills/debugging/SKILL.md":
                                  "---\nname: debugging\ndescription: d\n---\n",
                              "scripts/debug-tools/run.sh": "exit 0\n"}.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            links = {".claude/hooks": "../.agents/hooks",
                     ".claude/skills/debugging/scripts": "../../../scripts/debug-tools",
                     ".claude/notes": str(outside / "notes")}
            for rel, target in links.items():
                (repo / rel).symlink_to(target)
            paths.collect_repo(repo, out, set())
            for rel, target in links.items():
                with self.subTest(path=rel):
                    self.assertTrue((out / rel).is_file())
                    self.assertFalse((out / rel).is_symlink())
                    self.assertEqual((out / rel).read_text(), target + "\n")
            self.assertTrue((out / ".agents/hooks/check.sh").is_file())
            self.assertFalse((out / "scripts").exists())
            self.assertNotIn("PRIVATE", "".join((out / rel).read_text()
                                                for rel in paths.tree_files(out)))

    def test_copy_dir_alone_writes_the_marker_of_a_link_to_a_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "source", root / "destination"
            (root / "shared").mkdir()
            (root / "shared" / "a.md").write_text("a\n")
            source.mkdir()
            (source / "settings.json").write_text("{}\n")
            (source / "shared").symlink_to("../shared")
            (source / "here").symlink_to(".")
            paths.copy_dir(source, destination, set())
            self.assertEqual((destination / "shared").read_text(), "../shared\n")
            self.assertTrue((destination / "here").is_symlink())

    def test_a_file_copied_through_a_link_to_a_directory_keeps_its_place(self):
        """A Codex config can name a file through a link in `.codex` to a folder outside it. The
        collector copies the file below the link's path, and the link keeps no marker there."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
            for rel, body in {".codex/config.toml": 'model_instructions_file = "shared/notes.md"\n',
                              "docs/notes.md": "Notes.\n"}.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            (repo / ".codex/shared").symlink_to("../docs")
            paths.collect_repo(repo, out, set())
            self.assertTrue((out / ".codex/shared").is_dir())
            self.assertEqual((out / ".codex/shared/notes.md").read_text(), "Notes.\n")

    def test_links_outside_the_copied_directory_keep_their_markers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
            links = {".github/copilot-instructions.md": "../AGENTS.md",
                     ".claude/settings.json": "../config/settings.json",
                     ".cursor/rules/style.mdc": "../../style.mdc",
                     ".claude/skills/example/refs/guide.md": "../../../../guide.md"}
            for rel, body in {"AGENTS.md": "Instructions\n", "config/settings.json": "{}\n",
                              "style.mdc": "Style\n", "guide.md": "Guide\n"}.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            for rel, target in links.items():
                path = repo / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(target)
            paths.collect_repo(repo, out, set())
            for rel, target in links.items():
                with self.subTest(path=rel):
                    self.assertTrue((out / rel).is_file())
                    self.assertFalse((out / rel).is_symlink())
                    self.assertEqual((out / rel).read_text(), target + "\n")

    def test_a_link_beside_its_target_leaves_the_target_as_it_is(self):
        """rsync copies `.claude/` whole and keeps `.claude/AGENTS.md` as a link, and the copy of
        every `AGENTS.md` by name then finds the link at its destination."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
            (repo / ".claude").mkdir(parents=True)
            (repo / ".claude/CLAUDE.md").write_text("# Rules\n\nUse tabs.\n")
            (repo / ".claude/AGENTS.md").symlink_to("CLAUDE.md")
            paths.collect_repo(repo, out, set())
            self.assertEqual((out / ".claude/CLAUDE.md").read_text(), "# Rules\n\nUse tabs.\n")

    def test_a_link_that_leads_nowhere_creates_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
            (repo / ".claude").mkdir(parents=True)
            (repo / ".claude/settings.json").write_text("{}\n")
            (repo / ".claude/AGENTS.md").symlink_to("local/agents.md")
            paths.collect_repo(repo, out, set())
            self.assertFalse((out / ".claude/local").exists())
            self.assertFalse((out / ".claude/AGENTS.md").is_symlink())
            self.assertEqual((out / ".claude/AGENTS.md").read_text(), "local/agents.md\n")

    def test_outside_and_missing_targets_are_recorded_without_reading_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, destination = root / "source", root / "destination"
            source.mkdir()
            outside = root / "outside.txt"
            outside.write_text("outside contents\n")
            (source / "outside.md").symlink_to(outside)
            (source / "missing.md").symlink_to("../missing.md")
            vendor = source / "node_modules"
            vendor.mkdir()
            (vendor / "omitted.md").symlink_to(outside)
            paths.copy_dir(source, destination, {"node_modules"})
            self.assertEqual((destination / "outside.md").read_text(), str(outside) + "\n")
            self.assertEqual((destination / "missing.md").read_text(), "../missing.md\n")
            self.assertEqual(sorted(p.name for p in destination.iterdir()),
                             ["missing.md", "outside.md"])
            self.assertEqual(outside.read_text(), "outside contents\n")

    def test_a_copilot_link_survives_validation_and_detection(self):
        from agentcontext.steps import detection, collected_file_cleanup
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
            (repo / ".github").mkdir(parents=True)
            (repo / "AGENTS.md").write_text("Instructions\n")
            (repo / ".github/copilot-instructions.md").symlink_to("../AGENTS.md")
            paths.collect_repo(repo, out, set())
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
            self.assertTrue((out / ".github/copilot-instructions.md").is_file())
            self.assertIn(".github/copilot-instructions.md",
                          detection.detect_context_files(out))


class TestListRepoFiles(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _paths(self, vendored: set[str] | None = None) -> set[str]:
        """List the relative paths as a set of strings."""
        return {
            str(p.relative_to(self.repo))
            for p in list_repo_files(self.repo, vendored or set())
        }

    def test_regular_file_at_root_yielded(self):
        (self.repo / "file.txt").write_text("x")
        self.assertIn("file.txt", self._paths())

    def test_nested_file_yielded(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "deep.txt").write_text("x")
        self.assertIn("sub/deep.txt", self._paths())

    def test_dot_git_directory_skipped(self):
        git_dir = self.repo / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("x")
        self.assertEqual(self._paths(), set())

    def test_dot_git_nested_skipped(self):
        sub = self.repo / "sub" / ".git"
        sub.mkdir(parents=True)
        (sub / "HEAD").write_text("x")
        paths = self._paths()
        self.assertTrue(all(".git" not in p for p in paths))

    def test_vendored_directory_skipped(self):
        nm = self.repo / "node_modules"
        nm.mkdir()
        (nm / "pkg.json").write_text("x")
        (self.repo / "keep.txt").write_text("x")
        paths = self._paths({"node_modules"})
        self.assertNotIn("node_modules/pkg.json", paths)
        self.assertIn("keep.txt", paths)

    def test_vendored_directory_at_any_depth_skipped(self):
        deep = self.repo / "a" / "b" / "node_modules"
        deep.mkdir(parents=True)
        (deep / "pkg.json").write_text("x")
        paths = self._paths({"node_modules"})
        self.assertTrue(all("node_modules" not in p for p in paths))

    def test_multiple_vendored_dirs_all_skipped(self):
        for name in ("node_modules", "vendor", "__pycache__"):
            d = self.repo / name
            d.mkdir()
            (d / "file.txt").write_text("x")
        (self.repo / "keep.txt").write_text("x")
        paths = self._paths({"node_modules", "vendor", "__pycache__"})
        self.assertEqual(paths, {"keep.txt"})

    def test_symlink_is_yielded(self):
        (self.repo / "real.txt").write_text("x")
        (self.repo / "link.txt").symlink_to("real.txt")
        paths = self._paths()
        self.assertIn("link.txt", paths)

    def test_empty_directory_yields_nothing(self):
        self.assertEqual(self._paths(), set())

    def test_non_vendored_dir_with_same_prefix_not_skipped(self):
        # "node_modules_extra" should NOT be pruned by "node_modules"
        d = self.repo / "node_modules_extra"
        d.mkdir()
        (d / "file.txt").write_text("x")
        paths = self._paths({"node_modules"})
        self.assertIn("node_modules_extra/file.txt", paths)


class TestCopyDir(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "src_dir"
        self.src.mkdir()
        self.dst = self.tmp / "dst_dir"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_regular_files_copied(self):
        (self.src / "a.txt").write_text("aaa")
        (self.src / "b.txt").write_text("bbb")
        copy_dir(self.src, self.dst, set())
        self.assertEqual((self.dst / "a.txt").read_text(), "aaa")
        self.assertEqual((self.dst / "b.txt").read_text(), "bbb")

    def test_nested_structure_preserved(self):
        sub = self.src / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "file.txt").write_text("deep content")
        copy_dir(self.src, self.dst, set())
        self.assertEqual((self.dst / "sub" / "deep" / "file.txt").read_text(), "deep content")

    def test_dot_git_excluded(self):
        git_dir = self.src / ".git"
        git_dir.mkdir()
        (git_dir / "config").write_text("x")
        (self.src / "keep.txt").write_text("x")
        copy_dir(self.src, self.dst, set())
        self.assertFalse((self.dst / ".git").exists())
        self.assertTrue((self.dst / "keep.txt").exists())

    def test_vendored_dir_excluded(self):
        nm = self.src / "node_modules"
        nm.mkdir()
        (nm / "pkg.json").write_text("x")
        (self.src / "keep.txt").write_text("x")
        copy_dir(self.src, self.dst, {"node_modules"})
        self.assertFalse((self.dst / "node_modules").exists())
        self.assertTrue((self.dst / "keep.txt").exists())

    def test_multiple_vendored_dirs_excluded(self):
        for name in ("node_modules", "vendor", "__pycache__"):
            d = self.src / name
            d.mkdir()
            (d / "file.txt").write_text("x")
        (self.src / "keep.txt").write_text("x")
        copy_dir(self.src, self.dst, {"node_modules", "vendor", "__pycache__"})
        self.assertFalse((self.dst / "node_modules").exists())
        self.assertFalse((self.dst / "vendor").exists())
        self.assertFalse((self.dst / "__pycache__").exists())
        self.assertTrue((self.dst / "keep.txt").exists())

    def test_nested_vendored_dir_excluded(self):
        deep = self.src / "a" / "node_modules"
        deep.mkdir(parents=True)
        (deep / "pkg.json").write_text("x")
        (self.src / "a" / "keep.txt").write_text("x")
        copy_dir(self.src, self.dst, {"node_modules"})
        self.assertFalse((self.dst / "a" / "node_modules").exists())
        self.assertTrue((self.dst / "a" / "keep.txt").exists())

    def test_finder_files_are_not_copied(self):
        """A Finder file such as `.cursor/.DS_Store` is not copied, because a handover leaves it
        out, and a per-repository commit record that names it would make the reclassification
        incomplete."""
        (self.src / ".DS_Store").write_bytes(b"\x00\x00\x00\x01Bud1")
        (self.src / "rules").mkdir()
        (self.src / "rules" / ".DS_Store").write_bytes(b"Bud1")
        (self.src / "rules" / "a.mdc").write_text("x")
        copy_dir(self.src, self.dst, set())
        self.assertEqual(sorted(p.relative_to(self.dst).as_posix() for p in self.dst.rglob("*")),
                         ["rules", "rules/a.mdc"])

    def test_dest_created_if_missing(self):
        (self.src / "file.txt").write_text("x")
        self.assertFalse(self.dst.exists())
        copy_dir(self.src, self.dst, set())
        self.assertTrue((self.dst / "file.txt").exists())

    def test_a_failed_copy_names_the_directories_and_rsyncs_error(self):
        # The cloning step records the raise as `collector_error` with its traceback. The raise
        # therefore contains rsync's error text, which names the file and the reason, and not
        # only the non-zero exit status.
        unreadable = self.src / "file.txt"
        unreadable.write_text("x")
        os.chmod(unreadable, 0o000)
        if os.access(unreadable, os.R_OK):
            self.skipTest("this user can read a file with a mode that denies it")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            copy_dir(self.src, self.dst, set())
        message = str(raised.exception)
        self.assertIn(str(self.src), message)
        self.assertIn(str(self.dst), message)
        self.assertIn("exited", message)
        self.assertIn("file.txt", message)

    def test_delete_removes_extra_files_in_dest(self):
        # rsync --delete removes files in dest that don't exist in src
        self.dst.mkdir(parents=True)
        (self.dst / "stale.txt").write_text("old")
        (self.src / "fresh.txt").write_text("new")
        copy_dir(self.src, self.dst, set())
        self.assertFalse((self.dst / "stale.txt").exists())
        self.assertTrue((self.dst / "fresh.txt").exists())

    def test_empty_source_creates_empty_dest(self):
        copy_dir(self.src, self.dst, set())
        self.assertTrue(self.dst.exists())
        self.assertEqual(list(self.dst.iterdir()), [])


class TestFindDirsByName(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _find(self, name: str, vendored: set[str] | None = None) -> set[str]:
        return {
            str(p.relative_to(self.repo))
            for p in find_dirs_by_name(self.repo, name, vendored or set())
        }

    def test_dir_at_root_found(self):
        _tracked_dir(self.repo / ".claude")
        self.assertIn(".claude", self._find(".claude"))

    def test_dir_nested_found(self):
        _tracked_dir(self.repo / "sub" / ".claude")
        self.assertIn("sub/.claude", self._find(".claude"))

    def test_dir_at_multiple_depths_all_found(self):
        _tracked_dir(self.repo / ".claude")
        _tracked_dir(self.repo / "a" / ".claude")
        result = self._find(".claude")
        self.assertIn(".claude", result)
        self.assertIn("a/.claude", result)

    def test_different_name_not_found(self):
        (self.repo / ".gemini").mkdir()
        self.assertEqual(self._find(".claude"), set())

    def test_dot_git_not_descended_into(self):
        git = self.repo / ".git"
        git.mkdir()
        (git / ".claude").mkdir()
        self.assertEqual(self._find(".claude"), set())

    def test_vendored_dir_not_descended_into(self):
        nm = self.repo / "node_modules"
        nm.mkdir()
        (nm / ".claude").mkdir()
        self.assertEqual(self._find(".claude", {"node_modules"}), set())

    def test_vendored_dir_itself_not_returned_when_name_matches(self):
        # if someone names a vendored dir ".claude", it should not be returned
        (self.repo / "node_modules").mkdir()
        result = self._find("node_modules", {"node_modules"})
        self.assertEqual(result, set())

    def test_file_with_same_name_not_returned(self):
        (self.repo / ".claude").write_text("not a dir")
        self.assertEqual(self._find(".claude"), set())

    def test_empty_repo_returns_empty(self):
        self.assertEqual(self._find(".claude"), set())

    def test_symlinked_directory_not_returned(self):
        # A pass copies a directory at its own path, and `collect_symlink_targets` collects a
        # `.claude` that links to agent-files/claude as its marker and its target.
        target = self.repo / "agent-files" / "claude"
        target.mkdir(parents=True)
        (self.repo / ".claude").symlink_to("agent-files/claude")
        self.assertEqual(self._find(".claude"), set())

    def test_real_dir_still_returned_alongside_symlink(self):
        # a real .claude is still found, and only the symlinked one is skipped
        _tracked_dir(self.repo / "real" / ".claude")
        target = self.repo / "agent-files" / "claude"
        target.mkdir(parents=True)
        (self.repo / ".claude").symlink_to("agent-files/claude")
        self.assertEqual(self._find(".claude"), {"real/.claude"})


class TestCollectSkillMd(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_skill_md_in_claude_dir_is_copied(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".claude" / "skill.md").exists())

    def test_skill_md_in_cursor_dir_is_copied(self):
        (self.repo / ".cursor").mkdir()
        (self.repo / ".cursor" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".cursor" / "skill.md").exists())

    def test_symlinked_spec_dir_not_traversed(self):
        # .cursor links to agent-files/cursor. bash's `find <symlink>` does not traverse a
        # symlinked start-point, and a SKILL.md inside the target must NOT be collected.
        target = self.repo / "agent-files" / "cursor" / "skills" / "x"
        target.mkdir(parents=True)
        (target / "SKILL.md").write_text("skill")
        (self.repo / ".cursor").symlink_to("agent-files/cursor")
        collect_skill_md(self.repo, self.out)
        self.assertFalse((self.out / ".cursor").exists())

    def test_skill_md_in_gemini_dir_is_copied(self):
        (self.repo / ".gemini").mkdir()
        (self.repo / ".gemini" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".gemini" / "skill.md").exists())

    def test_skill_md_in_codex_dir_is_copied(self):
        (self.repo / ".codex").mkdir()
        (self.repo / ".codex" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".codex" / "skill.md").exists())

    def test_skill_md_in_github_dir_is_copied(self):
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".github" / "skill.md").exists())

    def test_case_insensitive_match_upper(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "SKILL.MD").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".claude" / "SKILL.MD").exists())

    def test_case_insensitive_match_mixed(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "Skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".claude" / "Skill.md").exists())

    def test_skill_md_at_repo_root_not_copied(self):
        (self.repo / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertFalse((self.out / "skill.md").exists())

    def test_skill_md_in_random_dir_not_copied(self):
        (self.repo / "mydir").mkdir()
        (self.repo / "mydir" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertFalse((self.out / "mydir" / "skill.md").exists())

    def test_returns_empty_set_no_flags(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "skill.md").write_text("skill")
        result = collect_skill_md(self.repo, self.out)
        self.assertEqual(result, set())

    def test_missing_spec_dir_no_error(self):
        # .claude doesn't exist, and this must not raise
        collect_skill_md(self.repo, self.out)

    def test_symlink_skill_md_copied_as_target_string(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "real.md").write_text("real")
        (self.repo / ".claude" / "skill.md").symlink_to("real.md")
        collect_skill_md(self.repo, self.out)
        dest = self.out / ".claude" / "skill.md"
        self.assertTrue(dest.exists())
        self.assertFalse(dest.is_symlink())
        self.assertEqual(dest.read_text(), "real.md\n")

    def test_skill_md_nested_inside_spec_dir_is_copied(self):
        (self.repo / ".claude" / "subdir").mkdir(parents=True)
        (self.repo / ".claude" / "subdir" / "skill.md").write_text("skill")
        collect_skill_md(self.repo, self.out)
        self.assertTrue((self.out / ".claude" / "subdir" / "skill.md").exists())


MANIFEST = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",'
            ' "name": "demo"}')
MCP_DOCUMENT = ('{"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",'
                ' "mcpServers": {"demo": {"command": "x"}}}')
# The format Copilot CLI's page gives for .github/lsp.json, which a plugin's file shares.
LSP_DOCUMENT = ('{"lspServers": {"rust-analyzer": {"command": "rust-analyzer",'
                ' "fileExtensions": {".rs": "rust"}}}}\n')


class TestCollectAgentPlugins(unittest.TestCase):
    """The portable layout, with two filenames that only their content identifies."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_manifest_declaring_the_standard_is_copied(self):
        self._file("plugins/demo/plugin.json", MANIFEST)
        self.assertEqual(collect_agent_plugins(self.repo, self.out, set()), set())
        self.assertTrue((self.out / "plugins" / "demo" / "plugin.json").is_file())

    def test_the_mcp_document_of_a_plugin_is_copied(self):
        self._file("plugins/demo/mcp.json", MCP_DOCUMENT)
        collect_agent_plugins(self.repo, self.out, set())
        self.assertTrue((self.out / "plugins" / "demo" / "mcp.json").is_file())

    def test_another_program_s_plugin_json_is_not_copied(self):
        # Grafana, Figma, Obsidian and other programs use the name. Without the content test
        # the collector would copy every one of them out of every repository.
        self._file("src/plugin.json", '{"id": "acme-panel", "type": "panel"}')
        collect_agent_plugins(self.repo, self.out, set())
        self.assertFalse((self.out / "src").exists())

    def test_a_manifest_naming_another_schema_is_not_copied(self):
        self._file("plugin.json",
                   '{"$schema": "https://grafana.com/plugin.schema.json", "name": "x"}')
        collect_agent_plugins(self.repo, self.out, set())
        self.assertFalse((self.out / "plugin.json").exists())

    def test_a_manifest_without_a_name_is_not_copied(self):
        # The standard requires both fields, and a document without `name` is not one.
        self._file("plugin.json",
                   '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"}')
        collect_agent_plugins(self.repo, self.out, set())
        self.assertFalse((self.out / "plugin.json").exists())

    def test_an_mcp_document_without_servers_is_not_copied(self):
        self._file("mcp.json",
                   '{"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"}')
        collect_agent_plugins(self.repo, self.out, set())
        self.assertFalse((self.out / "mcp.json").exists())

    def test_malformed_json_is_not_copied(self):
        self._file("plugin.json", "{not json")
        collect_agent_plugins(self.repo, self.out, set())
        self.assertFalse((self.out / "plugin.json").exists())

    def test_a_vendored_tree_is_not_read(self):
        self._file("node_modules/pkg/plugin.json", MANIFEST)
        collect_agent_plugins(self.repo, self.out, {"node_modules"})
        self.assertFalse((self.out / "node_modules").exists())

    def test_the_standard_names_no_tool_so_no_flag_is_set(self):
        self._file("plugin.json", MANIFEST)
        self.assertEqual(collect_agent_plugins(self.repo, self.out, set()), set())


class TestCollectClaude(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, vendored: set[str] | None = None) -> set[str]:
        return collect_claude(self.repo, self.out, vendored or set())

    # --- .claude dir ---

    def test_claude_dir_at_root_copied_and_sets_claude_flag(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "settings.json").write_text("{}")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertTrue((self.out / ".claude" / "settings.json").exists())

    def test_claude_dir_nested_found_and_copied(self):
        (self.repo / "sub" / ".claude").mkdir(parents=True)
        (self.repo / "sub" / ".claude" / "settings.json").write_text("{}")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertTrue((self.out / "sub" / ".claude" / "settings.json").exists())

    def test_a_submodule_named_claude_is_not_a_claude_directory(self):
        """A clone does not check its submodules out, and a submodule at `.claude` is
        an empty directory, which copies nothing and flags no tool. Built as git builds it, from
        a real submodule and a clone of its parent."""
        def git(*args, cwd):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                            "-c", "protocol.file.allow=always", *args], cwd=cwd,
                           check=True, capture_output=True)
        inner, parent = self.tmp / "inner", self.tmp / "parent"
        for path in (inner, parent):
            path.mkdir()
            git("init", "-q", cwd=path)
        (inner / "settings.json").write_text("{}")
        git("add", ".", cwd=inner)
        git("commit", "-qm", "inner", cwd=inner)
        git("submodule", "add", "-q", inner.as_uri(), ".claude", cwd=parent)
        git("commit", "-qm", "parent", cwd=parent)
        shutil.rmtree(self.repo)
        git("clone", "-q", parent.as_uri(), str(self.repo), cwd=self.tmp)
        self.assertEqual(list((self.repo / ".claude").iterdir()), [])
        self.assertNotIn("claude", self._collect())
        self.assertFalse((self.out / ".claude").exists())

    def test_a_skill_folder_named_like_a_vendored_directory_is_copied(self):
        """A skill named like a vendored directory, such as unoplatform/uno's Claude Code skill
        `.claude/skills/docs-build`, is copied, while the vendored trees beside and inside a
        skill stay out: a folder with a SKILL.md is a skill, and `skills/vendor` of
        open-metadata/openmetadata contains only skills that another project wrote."""
        vendored = {"docs-build", "vendor", "node_modules"}
        for rel, text in ((".claude/skills/docs-build/SKILL.md", "---\nname: docs-build\n---\n"),
                          (".claude/skills/docs-build/scripts/build.sh", "make docs\n"),
                          (".claude/skills/docs-build/node_modules/x/index.js", "x\n"),
                          (".claude/skills/vendor/react/SKILL.md", "---\nname: react\n---\n"),
                          (".claude/docs-build/notes.md", "x\n")):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        self._collect(vendored)
        copied = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*")
                        if p.is_file())
        self.assertEqual(copied, [".claude/skills/docs-build/SKILL.md",
                                  ".claude/skills/docs-build/scripts/build.sh"])

    def test_claude_dir_inside_vendored_not_copied(self):
        (self.repo / "node_modules" / ".claude").mkdir(parents=True)
        (self.repo / "node_modules" / ".claude" / "settings.json").write_text("{}")
        flags = self._collect({"node_modules"})
        self.assertNotIn("claude", flags)

    def test_claude_local_md_copied_and_sets_the_context_flag(self):
        (self.repo / "CLAUDE.local.md").write_text("mine")
        flags = self._collect()
        self.assertIn("claude.md", flags)
        self.assertTrue((self.out / "CLAUDE.local.md").exists())

    # --- .mcp.json root only ---

    def test_mcp_json_at_root_copied(self):
        (self.repo / ".mcp.json").write_text("{}")
        self._collect()
        self.assertTrue((self.out / ".mcp.json").exists())

    def test_mcp_json_sets_no_flag(self):
        (self.repo / ".mcp.json").write_text("{}")
        flags = self._collect()
        self.assertNotIn("claude", flags)

    def test_mcp_json_in_subdir_not_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / ".mcp.json").write_text("{}")
        self._collect()
        self.assertFalse((self.out / "sub" / ".mcp.json").exists())

    # --- managed-mcp.json anywhere ---

    def test_managed_mcp_json_at_root_copied(self):
        (self.repo / "managed-mcp.json").write_text("{}")
        self._collect()
        self.assertTrue((self.out / "managed-mcp.json").exists())

    def test_managed_mcp_json_nested_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "managed-mcp.json").write_text("{}")
        self._collect()
        self.assertTrue((self.out / "sub" / "managed-mcp.json").exists())

    def test_managed_mcp_json_sets_no_flag(self):
        (self.repo / "managed-mcp.json").write_text("{}")
        flags = self._collect()
        self.assertNotIn("claude", flags)

    # --- CLAUDE.md case-insensitive ---

    def test_claude_md_lowercase_copied_and_sets_both_flags(self):
        (self.repo / "claude.md").write_text("# Claude")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertIn("claude.md", flags)
        self.assertTrue((self.out / "claude.md").exists())

    def test_claude_md_uppercase_copied_and_sets_both_flags(self):
        (self.repo / "CLAUDE.md").write_text("# Claude")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertIn("claude.md", flags)

    def test_claude_md_nested_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "CLAUDE.md").write_text("# Claude")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertTrue((self.out / "sub" / "CLAUDE.md").exists())

    def test_claude_md_inside_vendored_not_copied(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "CLAUDE.md").write_text("# Claude")
        flags = self._collect({"vendor"})
        self.assertNotIn("claude.md", flags)

    # --- .claude-plugin ---

    def test_claude_plugin_dir_copied(self):
        (self.repo / ".claude-plugin").mkdir()
        (self.repo / ".claude-plugin" / "index.js").write_text("x")
        self._collect()
        self.assertTrue((self.out / ".claude-plugin" / "index.js").exists())

    def test_claude_plugin_whitelisted_sibling_readme_copied(self):
        _tracked_dir(self.repo / ".claude-plugin")
        (self.repo / "README.md").write_text("readme")
        self._collect()
        self.assertTrue((self.out / "README.md").exists())

    def test_claude_plugin_whitelisted_sibling_license_copied(self):
        _tracked_dir(self.repo / ".claude-plugin")
        (self.repo / "LICENSE").write_text("license")
        self._collect()
        self.assertTrue((self.out / "LICENSE").exists())

    def test_claude_plugin_whitelisted_sibling_package_json_copied(self):
        _tracked_dir(self.repo / ".claude-plugin")
        (self.repo / "package.json").write_text("{}")
        self._collect()
        self.assertTrue((self.out / "package.json").exists())

    def test_claude_plugin_non_whitelisted_sibling_not_copied(self):
        (self.repo / ".claude-plugin").mkdir()
        (self.repo / "index.js").write_text("x")
        self._collect()
        self.assertFalse((self.out / "index.js").exists())

    def test_claude_plugin_sibling_dir_not_copied(self):
        (self.repo / ".claude-plugin").mkdir()
        (self.repo / "README.md").mkdir()  # directory, not file
        self._collect()
        self.assertFalse((self.out / "README.md").exists())

    def test_claude_plugin_nested_whitelisted_sibling_copied(self):
        _tracked_dir(self.repo / "sub" / ".claude-plugin")
        (self.repo / "sub" / "README.md").write_text("readme")
        self._collect()
        self.assertTrue((self.out / "sub" / "README.md").exists())

    def test_a_vendored_tree_that_contains_a_plugin_is_not_copied_whole(self):
        """A nested plugin directory does not bring the source tree around it.

        `next.js/` is a vendored submodule of Next.js, which contains a nested `.claude-plugin/`.
        Copying the whole parent of a plugin directory would bring the entire source tree with it.
        The section "The vendored list" of docs/heuristics.md records the decision, which keeps the
        plugin directory and its sibling metadata. The tests above check that one level deep, and
        this case covers the parent's subdirectories, not its files."""
        vendored = self.repo / "next.js"
        (vendored / ".claude-plugin").mkdir(parents=True)
        (vendored / ".claude-plugin" / "plugin.json").write_text('{"name": "next"}')
        (vendored / "README.md").write_text("# Next.js")
        (vendored / "package.json").write_text("{}")
        for directory in ("packages/next/src", "test/e2e/app-dir", "crates/next-core/src"):
            (vendored / directory).mkdir(parents=True)
            for i in range(20):
                (vendored / directory / f"file{i}.ts").write_text("export {}")
        self._collect()
        copied = sorted(str(p.relative_to(self.out))
                        for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(copied, ["next.js/.claude-plugin/plugin.json",
                                  "next.js/README.md", "next.js/package.json"])

    # --- symlinked .claude, which `collect_symlink_targets` collects ---

    def test_symlinked_claude_dir_not_collected(self):
        # .claude links to agent-files/claude, where the configuration is stored. The Claude
        # pass copies no `.claude` that is a link, and `TestALinkedToolDirectory` checks the
        # whole collection of one.
        target = self.repo / "agent-files" / "claude"
        target.mkdir(parents=True)
        (target / "settings.json").write_text("{}")
        (self.repo / ".claude").symlink_to("agent-files/claude")
        flags = self._collect()
        self.assertNotIn("claude", flags)
        self.assertFalse((self.out / ".claude").exists())

    # --- empty repo ---

    def test_nothing_present_returns_empty_set(self):
        flags = self._collect()
        self.assertEqual(flags, set())


class TestCollectGemini(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, vendored: set[str] | None = None) -> set[str]:
        return collect_gemini(self.repo, self.out, vendored or set())

    # --- .gemini dir ---

    def test_gemini_dir_copied_and_sets_gemini_flag(self):
        (self.repo / ".gemini").mkdir()
        (self.repo / ".gemini" / "settings.json").write_text("{}")
        flags = self._collect()
        self.assertIn("gemini", flags)
        self.assertTrue((self.out / ".gemini" / "settings.json").exists())

    def test_gemini_dir_nested_found_and_copied(self):
        (self.repo / "sub" / ".gemini").mkdir(parents=True)
        (self.repo / "sub" / ".gemini" / "settings.json").write_text("{}")
        flags = self._collect()
        self.assertIn("gemini", flags)

    def test_gemini_dir_inside_vendored_not_copied(self):
        (self.repo / "vendor" / ".gemini").mkdir(parents=True)
        flags = self._collect({"vendor"})
        self.assertNotIn("gemini", flags)

    # --- .aiexclude, .geminiignore, gemini-extension.json ---

    def test_aiexclude_copied_sets_no_flag(self):
        (self.repo / ".aiexclude").write_text("*.log")
        flags = self._collect()
        self.assertTrue((self.out / ".aiexclude").exists())
        self.assertNotIn("gemini", flags)

    def test_geminiignore_copied_sets_no_flag(self):
        (self.repo / ".geminiignore").write_text("*.log")
        flags = self._collect()
        self.assertTrue((self.out / ".geminiignore").exists())
        self.assertNotIn("gemini", flags)

    def test_gemini_extension_json_copied_sets_no_flag(self):
        (self.repo / "gemini-extension.json").write_text("{}")
        flags = self._collect()
        self.assertTrue((self.out / "gemini-extension.json").exists())
        self.assertNotIn("gemini", flags)

    def test_misc_files_case_sensitive(self):
        # Names are case-sensitive, and GEMINIIGNORE does not match
        (self.repo / "GEMINIIGNORE").write_text("x")
        self._collect()
        self.assertFalse((self.out / "GEMINIIGNORE").exists())

    def test_misc_files_nested_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / ".aiexclude").write_text("x")
        self._collect()
        self.assertTrue((self.out / "sub" / ".aiexclude").exists())

    def test_misc_files_inside_vendored_not_copied(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / ".aiexclude").write_text("x")
        self._collect({"vendor"})
        self.assertFalse((self.out / "vendor" / ".aiexclude").exists())

    # --- GEMINI.md case-insensitive ---

    def test_gemini_md_uppercase_sets_both_flags(self):
        (self.repo / "GEMINI.md").write_text("# Gemini")
        flags = self._collect()
        self.assertIn("gemini", flags)
        self.assertIn("gemini.md", flags)
        self.assertTrue((self.out / "GEMINI.md").exists())

    def test_gemini_md_lowercase_sets_both_flags(self):
        (self.repo / "gemini.md").write_text("# Gemini")
        flags = self._collect()
        self.assertIn("gemini", flags)
        self.assertIn("gemini.md", flags)

    def test_gemini_md_nested_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "GEMINI.md").write_text("# Gemini")
        flags = self._collect()
        self.assertIn("gemini.md", flags)
        self.assertTrue((self.out / "sub" / "GEMINI.md").exists())

    def test_gemini_md_inside_vendored_not_copied(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "GEMINI.md").write_text("# Gemini")
        flags = self._collect({"vendor"})
        self.assertNotIn("gemini.md", flags)

    # --- empty repo ---

    def test_nothing_present_returns_empty_set(self):
        self.assertEqual(self._collect(), set())


class TestCollectCodex(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, vendored: set[str] | None = None) -> set[str]:
        return collect_codex(self.repo, self.out, vendored or set())

    # --- .codex dir ---

    def test_codex_dir_copied_and_sets_codex_flag(self):
        (self.repo / ".codex").mkdir()
        (self.repo / ".codex" / "config.json").write_text("{}")
        flags = self._collect()
        self.assertIn("codex", flags)
        self.assertTrue((self.out / ".codex" / "config.json").exists())

    def test_codex_dir_nested_found_and_copied(self):
        (self.repo / "sub" / ".codex").mkdir(parents=True)
        (self.repo / "sub" / ".codex" / "config.json").write_text("{}")
        flags = self._collect()
        self.assertIn("codex", flags)

    def test_codex_dir_inside_vendored_not_copied(self):
        (self.repo / "vendor" / ".codex").mkdir(parents=True)
        flags = self._collect({"vendor"})
        self.assertNotIn("codex", flags)

    # --- AGENTS.md case-insensitive ---

    def test_agents_md_uppercase_copied_and_sets_agents_md_flag(self):
        (self.repo / "AGENTS.md").write_text("# Agents")
        flags = self._collect()
        self.assertIn("agents.md", flags)
        self.assertTrue((self.out / "AGENTS.md").exists())

    def test_agents_md_lowercase_sets_agents_md_flag(self):
        (self.repo / "agents.md").write_text("# Agents")
        flags = self._collect()
        self.assertIn("agents.md", flags)

    def test_agents_override_md_sets_agents_md_flag(self):
        (self.repo / "AGENTS.override.md").write_text("# Override")
        flags = self._collect()
        self.assertIn("agents.md", flags)
        self.assertTrue((self.out / "AGENTS.override.md").exists())

    def test_agents_md_nested_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "AGENTS.md").write_text("# Agents")
        flags = self._collect()
        self.assertIn("agents.md", flags)
        self.assertTrue((self.out / "sub" / "AGENTS.md").exists())

    def test_agents_md_inside_vendored_not_copied(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "AGENTS.md").write_text("# Agents")
        flags = self._collect({"vendor"})
        self.assertNotIn("agents.md", flags)

    # --- PLANS.md, copied only beside a .codex directory or an AGENTS.md ---

    def test_plans_md_copied_when_codex_dir_present(self):
        _tracked_dir(self.repo / ".codex")
        (self.repo / "PLANS.md").write_text("# Plans")
        flags = self._collect()
        self.assertIn("plans.md", flags)
        self.assertTrue((self.out / "PLANS.md").exists())

    def test_plans_md_copied_when_agents_md_present(self):
        (self.repo / "AGENTS.md").write_text("# Agents")
        (self.repo / "PLANS.md").write_text("# Plans")
        flags = self._collect()
        self.assertIn("plans.md", flags)

    def test_plans_md_not_copied_without_codex_or_agents(self):
        (self.repo / "PLANS.md").write_text("# Plans")
        flags = self._collect()
        self.assertNotIn("plans.md", flags)
        self.assertFalse((self.out / "PLANS.md").exists())

    def test_plans_md_case_insensitive(self):
        (self.repo / "AGENTS.md").write_text("# Agents")
        (self.repo / "plans.md").write_text("# Plans")
        flags = self._collect()
        self.assertIn("plans.md", flags)

    def test_plans_md_inside_vendored_not_copied(self):
        (self.repo / "AGENTS.md").write_text("# Agents")
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / "PLANS.md").write_text("# Plans")
        flags = self._collect({"vendor"})
        self.assertNotIn("plans.md", flags)

    # --- empty repo ---

    def test_nothing_present_returns_empty_set(self):
        self.assertEqual(self._collect(), set())


class TestCopilotPathCase(unittest.TestCase):
    def test_an_uppercase_instructions_file_is_collected_with_its_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, out = root / "repo", root / "out"
            source = repo / ".github" / "COPILOT-INSTRUCTIONS.md"
            source.parent.mkdir(parents=True)
            source.write_text("Instructions\n")
            flags = collect_copilot(repo, out, set())
            self.assertEqual(flags, {"copilot", "copilot-instructions"})
            self.assertEqual((out / ".github" / source.name).read_text(), "Instructions\n")

    def test_uppercase_mechanism_directories_trigger_collection(self):
        for name in ("INSTRUCTIONS", "AGENTS", "SKILLS", "HOOKS", "PROMPTS"):
            with self.subTest(directory=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                repo, out = root / "repo", root / "out"
                source = repo / ".github" / name
                source.mkdir(parents=True)
                (source / "config.md").write_text("Configuration\n")
                flags = collect_copilot(repo, out, set())
                expected = {"copilot"}
                if name == "INSTRUCTIONS":
                    expected.add("copilot-instructions")
                self.assertEqual(flags, expected)
                self.assertEqual((out / ".github" / name / "config.md").read_text(),
                                 "Configuration\n")


class TestCollectCopilot(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, vendored: set[str] | None = None) -> set[str]:
        return collect_copilot(self.repo, self.out, vendored or set())

    def _make_github(self) -> Path:
        d = self.repo / ".github"
        d.mkdir(exist_ok=True)
        return d

    # --- trigger: copilot-instructions.md ---

    def test_copilot_instructions_md_triggers_copy_and_sets_copilot(self):
        gh = self._make_github()
        (gh / "copilot-instructions.md").write_text("x")
        flags = self._collect()
        self.assertIn("copilot", flags)
        self.assertTrue((self.out / ".github" / "copilot-instructions.md").exists())

    def test_copilot_instructions_md_sets_copilot_instructions_flag(self):
        gh = self._make_github()
        (gh / "copilot-instructions.md").write_text("x")
        flags = self._collect()
        self.assertIn("copilot-instructions", flags)

    # --- trigger: instructions dir ---

    def test_instructions_dir_triggers_copy_and_sets_copilot(self):
        gh = self._make_github()
        (gh / "instructions").mkdir()
        flags = self._collect()
        self.assertIn("copilot", flags)

    def test_instructions_dir_sets_copilot_instructions_flag(self):
        gh = self._make_github()
        (gh / "instructions").mkdir()
        flags = self._collect()
        self.assertIn("copilot-instructions", flags)

    # --- trigger: agents dir ---

    def test_agents_dir_triggers_copy_and_sets_copilot(self):
        gh = self._make_github()
        (gh / "agents").mkdir()
        flags = self._collect()
        self.assertIn("copilot", flags)

    def test_agents_dir_does_not_set_copilot_instructions_flag(self):
        gh = self._make_github()
        (gh / "agents").mkdir()
        flags = self._collect()
        self.assertNotIn("copilot-instructions", flags)

    # --- trigger: hooks dir ---

    def test_hooks_dir_triggers_copy_and_sets_copilot(self):
        gh = self._make_github()
        (gh / "hooks").mkdir()
        flags = self._collect()
        self.assertIn("copilot", flags)

    # --- trigger: skills dir ---

    def test_skills_dir_triggers_copy_and_sets_copilot(self):
        gh = self._make_github()
        (gh / "skills").mkdir()
        flags = self._collect()
        self.assertIn("copilot", flags)

    # --- no trigger ---

    def test_prompts_dir_triggers_copy_and_sets_copilot(self):
        (self.repo / ".github" / "prompts").mkdir(parents=True)
        (self.repo / ".github" / "prompts" / "lint-fix.prompt.md").write_text("p")
        flags = collect_copilot(self.repo, self.out, set())
        self.assertIn("copilot", flags)
        self.assertTrue((self.out / ".github" / "prompts" / "lint-fix.prompt.md").exists())

    def test_each_other_github_heuristics_path_triggers_the_copy_on_its_own(self):
        # Named here rather than read from the heuristics, so that a row that the collector
        # stops loading fails this test instead of shrinking it.
        for rel in ("copilot/settings.json", "copilot/settings.local.json", "mcp.json",
                    "workflows/copilot-setup-steps.yml", "chatmodes/review.chatmode.md",
                    "allowed_models.txt", "workflows/copilot-code-review.yml"):
            with self.subTest(rel=rel):
                shutil.rmtree(self.repo)
                shutil.rmtree(self.out)
                self.repo.mkdir()
                self.out.mkdir()
                f = self.repo / ".github" / rel
                f.parent.mkdir(parents=True)
                f.write_text("x")
                flags = self._collect()
                self.assertIn("copilot", flags)
                self.assertTrue((self.out / ".github" / rel).exists())

    def test_vscode_mcp_json_is_copied_and_sets_copilot(self):
        (self.repo / ".vscode").mkdir()
        (self.repo / ".vscode" / "mcp.json").write_text("{}")
        flags = self._collect()
        self.assertIn("copilot", flags)
        self.assertTrue((self.out / ".vscode" / "mcp.json").exists())

    def test_github_without_trigger_not_copied(self):
        gh = self._make_github()
        (gh / "workflows").mkdir()
        (gh / "workflows" / "ci.yml").write_text("x")
        flags = self._collect()
        self.assertNotIn("copilot", flags)
        self.assertFalse((self.out / ".github").exists())

    def test_no_github_dir_returns_empty_set(self):
        self.assertEqual(self._collect(), set())

    # --- entire .github copied when triggered ---

    def test_entire_github_dir_copied_when_triggered(self):
        gh = self._make_github()
        (gh / "copilot-instructions.md").write_text("x")
        (gh / "workflows").mkdir()
        (gh / "workflows" / "ci.yml").write_text("y")
        self._collect()
        self.assertTrue((self.out / ".github" / "workflows" / "ci.yml").exists())

    # --- copilot-instructions flag is independent of trigger ---

    def test_copilot_instructions_flag_set_even_when_only_instructions_dir(self):
        gh = self._make_github()
        (gh / "instructions").mkdir()
        (gh / "instructions" / "coding.md").write_text("x")
        flags = self._collect()
        self.assertIn("copilot-instructions", flags)

    def test_copilot_instructions_flag_not_set_when_only_agents_dir(self):
        gh = self._make_github()
        (gh / "agents").mkdir()
        flags = self._collect()
        self.assertNotIn("copilot-instructions", flags)


class TestCollectCursor(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, vendored: set[str] | None = None) -> set[str]:
        return collect_cursor(self.repo, self.out, vendored or set())

    # --- .cursor dir ---

    def test_cursor_dir_copied_and_sets_cursor_flag(self):
        (self.repo / ".cursor").mkdir()
        (self.repo / ".cursor" / "settings.json").write_text("{}")
        flags = self._collect()
        self.assertIn("cursor", flags)
        self.assertTrue((self.out / ".cursor" / "settings.json").exists())

    def test_cursor_dir_nested_found_and_copied(self):
        (self.repo / "sub" / ".cursor").mkdir(parents=True)
        (self.repo / "sub" / ".cursor" / "settings.json").write_text("{}")
        flags = self._collect()
        self.assertIn("cursor", flags)

    def test_cursor_dir_inside_vendored_not_copied(self):
        (self.repo / "vendor" / ".cursor").mkdir(parents=True)
        flags = self._collect({"vendor"})
        self.assertNotIn("cursor", flags)

    # --- .cursorrules ---

    def test_cursorrules_copied_and_sets_both_flags(self):
        (self.repo / ".cursorrules").write_text("rules")
        flags = self._collect()
        self.assertIn("cursor", flags)
        self.assertIn("cursorrules", flags)
        self.assertTrue((self.out / ".cursorrules").exists())

    def test_cursorrules_nested_sets_both_flags(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / ".cursorrules").write_text("rules")
        flags = self._collect()
        self.assertIn("cursor", flags)
        self.assertIn("cursorrules", flags)

    def test_cursorrules_inside_vendored_not_copied(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / ".cursorrules").write_text("rules")
        flags = self._collect({"vendor"})
        self.assertNotIn("cursorrules", flags)

    # --- .cursorignore ---

    def test_cursorignore_copied_sets_no_flag(self):
        (self.repo / ".cursorignore").write_text("*.log")
        flags = self._collect()
        self.assertTrue((self.out / ".cursorignore").exists())
        self.assertNotIn("cursor", flags)
        self.assertNotIn("cursorrules", flags)

    def test_cursorindexingignore_copied(self):
        (self.repo / ".cursorindexingignore").write_text("dist/")
        self._collect()
        self.assertTrue((self.out / ".cursorindexingignore").exists())

    def test_cursorignore_nested_copied(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / ".cursorignore").write_text("x")
        self._collect()
        self.assertTrue((self.out / "sub" / ".cursorignore").exists())

    def test_cursorignore_inside_vendored_not_copied(self):
        (self.repo / "vendor").mkdir()
        (self.repo / "vendor" / ".cursorignore").write_text("x")
        self._collect({"vendor"})
        self.assertFalse((self.out / "vendor" / ".cursorignore").exists())

    # --- case sensitivity ---

    def test_cursorrules_name_is_case_sensitive(self):
        # bash uses -name (not -iname) for .cursorrules
        (self.repo / ".CURSORRULES").write_text("rules")
        flags = self._collect()
        self.assertNotIn("cursorrules", flags)
        self.assertFalse((self.out / ".CURSORRULES").exists())

    # --- empty repo ---

    def test_nothing_present_returns_empty_set(self):
        self.assertEqual(self._collect(), set())


class TestTightenFlags(unittest.TestCase):

    def test_claude_md_implies_claude(self):
        flags = {"claude.md"}
        tighten_flags(flags)
        self.assertIn("claude", flags)

    def test_gemini_md_implies_gemini(self):
        flags = {"gemini.md"}
        tighten_flags(flags)
        self.assertIn("gemini", flags)

    def test_cursorrules_implies_cursor(self):
        flags = {"cursorrules"}
        tighten_flags(flags)
        self.assertIn("cursor", flags)

    def test_claude_already_set_unchanged(self):
        flags = {"claude", "claude.md"}
        tighten_flags(flags)
        self.assertIn("claude", flags)

    def test_no_flags_unchanged(self):
        flags: set[str] = set()
        tighten_flags(flags)
        self.assertEqual(flags, set())

    def test_unrelated_flags_unchanged(self):
        flags = {"codex", "copilot"}
        tighten_flags(flags)
        self.assertEqual(flags, {"codex", "copilot"})

    def test_all_three_implications_at_once(self):
        flags = {"claude.md", "gemini.md", "cursorrules"}
        tighten_flags(flags)
        self.assertIn("claude", flags)
        self.assertIn("gemini", flags)
        self.assertIn("cursor", flags)


class TestBuildCsvRow(unittest.TestCase):

    def test_the_header_is_the_metadata_columns_then_one_flag_each(self):
        """`first_commit_sha` is the collector's own column: it reads the history of every
        file that it copies, which a depth-1 clone alone could not provide."""
        expected = ("repo_name,scanned_at,branch,head_sha,head_commit_date,"
                    "first_commit_sha,"
                    "claude,gemini,codex,copilot,cursor,opencode,claude.md,gemini.md,agents.md,"
                    "plans.md,copilot-instructions,cursorrules")
        self.assertEqual(COLLECTED_CSV_HEADER, expected)
        self.assertEqual(COLLECTED_CSV_HEADER.split(",")[:len(_METADATA_COLUMNS)],
                         list(_METADATA_COLUMNS))

    def test_all_false_when_no_flags(self):
        row = build_csv_row("owner/repo", "2024-01-01T00:00:00Z", "main", "a" * 40,
                             "2023-05-05T00:00:00Z", "", set())
        parts = row.split(",")
        self.assertEqual(parts[0], "owner/repo")
        self.assertEqual(parts[2], "main")
        for val in parts[len(_METADATA_COLUMNS):]:
            self.assertEqual(val, "FALSE")

    def test_the_scan_time_and_the_commit_date_go_to_their_own_columns(self):
        """The scan time and the committer date of the head commit are written separately,
        and a row that crossed them would publish the scan as the commit date of every
        repository."""
        row = build_csv_row("owner/repo", "2026-03-29", "main", "a" * 40,
                             "2023-05-05T00:00:00Z", "", set())
        self.assertEqual(_row_field(row, "scanned_at"), "2026-03-29")
        self.assertEqual(_row_field(row, "head_commit_date"), "2023-05-05T00:00:00Z")

    def test_column_count_matches_header(self):
        row = build_csv_row("owner/repo", "2024-01-01T00:00:00Z", "main", "a" * 40,
                             "2023-05-05T00:00:00Z", "", set())
        self.assertEqual(len(row.split(",")), len(COLLECTED_CSV_HEADER.split(",")))

    def test_claude_flag_sets_correct_column(self):
        row = build_csv_row("owner/repo", "ts", "main", "a" * 40, "cd", "", {"claude"})
        parts = row.split(",")
        header_parts = COLLECTED_CSV_HEADER.split(",")
        idx = header_parts.index("claude")
        self.assertEqual(parts[idx], "TRUE")

    def test_multiple_flags_set(self):
        row = build_csv_row("o/r", "ts", "main", "a" * 40, "cd", "",
                             {"claude", "claude.md", "cursor", "cursorrules"})
        parts = row.split(",")
        header_parts = COLLECTED_CSV_HEADER.split(",")
        for flag in ("claude", "claude.md", "cursor", "cursorrules"):
            idx = header_parts.index(flag)
            self.assertEqual(parts[idx], "TRUE", f"{flag} should be TRUE")

    def test_unset_flags_are_false(self):
        row = build_csv_row("o/r", "ts", "main", "a" * 40, "cd", "", {"claude"})
        parts = row.split(",")
        header_parts = COLLECTED_CSV_HEADER.split(",")
        idx = header_parts.index("gemini")
        self.assertEqual(parts[idx], "FALSE")

    def test_empty_branch_allowed(self):
        row = build_csv_row("owner/repo", "ts", "", "", "", "", set())
        parts = row.split(",")
        self.assertEqual(parts[2], "")

    def test_head_sha_written_to_its_column(self):
        row = build_csv_row("owner/repo", "ts", "main", "b" * 40, "cd", "", set())
        self.assertEqual(_row_field(row, "head_sha"), "b" * 40)

    def test_empty_head_sha_allowed(self):
        row = build_csv_row("owner/repo", "ts", "main", "", "", "", set())
        self.assertEqual(_row_field(row, "head_sha"), "")


class TestCollectOpenCode(unittest.TestCase):
    """OpenCode's `.opencode` directory is copied at any depth, as the tool reads one in every
    directory up to the worktree root, and its configuration and the settings of its terminal
    interface at the root alone, where the rows read them."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.out = self.tmp / "out"
        self.repo.mkdir()
        self.out.mkdir()

    def _write(self, *rels: str) -> None:
        for rel in rels:
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text("{}\n", encoding="utf-8")

    def test_the_directory_is_copied_at_any_depth_and_sets_the_flag(self):
        self._write(".opencode/agents/review.md", "packages/web/.opencode/commands/test.md")
        self.assertEqual(collect_opencode(self.repo, self.out, set()), {"opencode"})
        self.assertTrue((self.out / ".opencode/agents/review.md").is_file())
        self.assertTrue((self.out / "packages/web/.opencode/commands/test.md").is_file())

    def test_the_configuration_is_copied_at_the_root_and_not_below_it(self):
        self._write("opencode.json", "tui.jsonc", "src/lang/en/opencode.json", "metadata/tui.json")
        self.assertEqual(collect_opencode(self.repo, self.out, set()), {"opencode"})
        self.assertTrue((self.out / "opencode.json").is_file())
        self.assertTrue((self.out / "tui.jsonc").is_file())
        self.assertFalse((self.out / "src/lang/en/opencode.json").exists())
        self.assertFalse((self.out / "metadata/tui.json").exists())

    def test_a_repository_with_neither_sets_no_flag(self):
        self._write("src/lang/en/opencode.json")
        self.assertEqual(collect_opencode(self.repo, self.out, set()), set())

    def test_a_directory_inside_a_vendored_one_is_not_copied(self):
        self._write("node_modules/pkg/.opencode/agents/review.md")
        self.assertEqual(collect_opencode(self.repo, self.out, {"node_modules"}), set())


class TestOpenCodeInstructions(unittest.TestCase):
    """The files that OpenCode's `instructions` key lists, as OpenCode 1.14.24 globs each entry
    from the repository root with node-glob's `dot` and `nodir`: a `*` within one name, a `**`
    across directories, braces, dotted names, and nothing for a URL, the home directory, an
    absolute path or a path that leaves the root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

    def _write(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _declared(self, config: str, files: tuple, name: str = "opencode.json",
                  vendored=frozenset()) -> list:
        self._write({name: config, **{rel: "x\n" for rel in files}})
        return [rel for rel, row in paths.declared_context_files(self.repo, vendored)
                if row["tool"] == "opencode"]

    def test_a_glob_matches_within_one_directory_and_a_globstar_across_them(self):
        files = ("docs/standards/a.md", "docs/standards/deep/b.md", "docs/c.md", "docs/x/y/d.md",
                 "docs/x/e.txt")
        self.assertEqual(self._declared('{"instructions": ["docs/standards/*.md"]}', files),
                         ["docs/standards/a.md"])
        self.assertEqual(self._declared('{"instructions": ["docs/**/*.md"]}', files),
                         ["docs/c.md", "docs/standards/a.md", "docs/standards/deep/b.md",
                          "docs/x/y/d.md"])

    def test_dotted_names_braces_and_a_leading_dot_slash_are_read(self):
        files = (".claude/rules.md", "AGENTS.md", "AGENTS.local.md", "a/x.md", "b/x.md", "c/x.md")
        self.assertEqual(
            self._declared('{"instructions": [".claude/*.md", "./AGENTS*.md", "{a,b}/x.md"]}',
                           files),
            [".claude/rules.md", "AGENTS.local.md", "AGENTS.md", "a/x.md", "b/x.md"])

    def test_a_url_the_home_directory_an_absolute_path_and_a_parent_name_nothing(self):
        config = ('{"instructions": ["https://example.test/a.md", "~/a.md", "/a.md", "../a.md",'
                  ' "docs/../a.md"]}')
        self.assertEqual(self._declared(config, ("a.md", "docs/a.md")), [])

    def test_the_entries_of_the_opencode_directory_resolve_against_the_root(self):
        self.assertEqual(self._declared('{"instructions": ["guide.md"]}',
                                        ("guide.md", ".opencode/guide.md"),
                                        name=".opencode/opencode.json"),
                         ["guide.md"])

    def test_a_configuration_below_the_root_is_not_read(self):
        self.assertEqual(self._declared('{"instructions": ["guide.md"]}', ("guide.md",),
                                        name="packages/web/opencode.json"), [])

    def test_a_vendored_file_and_a_directory_are_not_read(self):
        self._write({"node_modules/pkg/a.md": "x\n", "docs/sub/a.md": "x\n"})
        self.assertEqual(self._declared('{"instructions": ["node_modules/pkg/a.md", "docs/*"]}',
                                        ("docs/b.md",), vendored={"node_modules"}),
                         ["docs/b.md"])

    def test_a_configuration_that_does_not_parse_declares_nothing(self):
        self.assertEqual(self._declared('{"instructions": ["a.md"', ("a.md",)), [])


class TestCollectRepo(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, vendored: set[str] | None = None) -> set[str]:
        return collect_repo(self.repo, self.out, vendored or set())

    def test_empty_repo_returns_empty_flags(self):
        self.assertEqual(self._collect(), set())

    def test_empty_repo_does_not_create_the_out_dir(self):
        self._collect()
        self.assertFalse(self.out.exists())

    def test_multiple_tools_flags_accumulate(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "settings.json").write_text("{}")
        (self.repo / ".cursor").mkdir()
        (self.repo / ".cursor" / "rules.json").write_text("{}")
        (self.repo / "GEMINI.md").write_text("# Gemini")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertIn("cursor", flags)
        self.assertIn("gemini", flags)
        self.assertIn("gemini.md", flags)

    def test_tightening_applied_claude_md_only(self):
        (self.repo / "CLAUDE.md").write_text("# Claude")
        flags = self._collect()
        self.assertIn("claude", flags)
        self.assertIn("claude.md", flags)

    def test_tightening_applied_cursorrules_only(self):
        (self.repo / ".cursorrules").write_text("rules")
        flags = self._collect()
        self.assertIn("cursorrules", flags)
        self.assertIn("cursor", flags)

    def test_files_copied_into_output(self):
        (self.repo / "CLAUDE.md").write_text("# Claude")
        self._collect()
        self.assertTrue((self.out / "CLAUDE.md").exists())

    def test_skill_md_copied_without_setting_flags(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "SKILL.md").write_text("skill")
        flags = self._collect()
        # .claude dir copy sets claude flag, and SKILL.md itself sets nothing extra
        self.assertIn("claude", flags)
        self.assertTrue((self.out / ".claude" / "SKILL.md").exists())

    def test_output_created_when_only_unflagged_file_copied(self):
        # .mcp.json at root copies but sets no flag, and the output dir should still exist
        (self.repo / ".mcp.json").write_text("{}")
        flags = self._collect()
        self.assertEqual(flags, set())
        self.assertTrue((self.out / ".mcp.json").exists())

    def test_vendored_pruning_propagates(self):
        # The symlink pass reads the tree as well, with the same pruned set, and a linked
        # skills directory inside node_modules leaves no marker and no target behind.
        (self.repo / "node_modules" / ".claude").mkdir(parents=True)
        (self.repo / "node_modules" / ".claude" / "x.json").write_text("{}")
        pkg = self.repo / "node_modules" / "pkg"
        (pkg / "shared" / "pdf").mkdir(parents=True)
        (pkg / "shared" / "pdf" / "SKILL.md").write_text("x\n")
        (pkg / ".claude").mkdir()
        os.symlink("../shared", pkg / ".claude" / "skills")
        flags = self._collect({"node_modules"})
        self.assertNotIn("claude", flags)
        self.assertFalse((self.out / "node_modules" / "pkg" / ".claude" / "skills").exists())
        self.assertFalse((self.out / "node_modules" / "pkg" / "shared").exists())
        self.assertFalse(self.out.exists())


class TestFilesNestedTooDeeplyToParse(unittest.TestCase):
    """The collector reads the settings, the manifests and the Agent Plugins documents of a
    clone. A file that the parser cannot read for its depth raises RecursionError, which would
    end the collection of the repository. Such a file reads as a file that does not parse."""

    def test_the_repository_is_collected_without_what_does_not_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = Path(tmp) / "owner§repo", Path(tmp) / "out"
            for rel, content in deeply_nested_files().items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(content)
            with contextlib.redirect_stderr(io.StringIO()):
                flags = collect_repo(repo, out, set())
            self.assertIn("claude", flags)
            self.assertTrue((out / "CLAUDE.md").is_file())
            self.assertFalse((out / "plugin.json").exists())
            self.assertFalse((out / "mcp.json").exists())


class TestFilesWithIntegersTooLongToParse(unittest.TestCase):
    """An integer literal longer than Python converts makes `json` and `tomllib` raise a
    ValueError that is neither a JSONDecodeError nor a TOMLDecodeError. Every reader of a
    collected file in this module catches ValueError. Such a file therefore reads as a file that
    does not parse, and the collection of the repository goes on, as the collected file cleanup
    and the detection step read it."""

    def test_the_repository_is_collected_without_what_does_not_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo, out = Path(tmp) / "owner§repo", Path(tmp) / "out"
            for rel, content in files_with_long_integers().items():
                (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                (repo / rel).write_text(content)
            with contextlib.redirect_stderr(io.StringIO()):
                flags = collect_repo(repo, out, set())
            self.assertIn("claude", flags)
            self.assertTrue((out / "CLAUDE.md").is_file())
            self.assertTrue((out / ".codex" / "config.toml").is_file())
            self.assertFalse((out / "plugin.json").exists())
            self.assertFalse((out / "mcp.json").exists())


class TestPrintMatches(unittest.TestCase):
    """The log message that --print-matches writes to stderr for each copy."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "owner§repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _collect(self, print_matches: bool) -> str:
        """Run collect_repo and return the stderr that it wrote."""
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            collect_repo(self.repo, self.out, set(), print_matches=print_matches)
        return buf.getvalue()

    def test_disabled_by_default_no_output(self):
        (self.repo / "CLAUDE.md").write_text("# c")
        self.assertEqual(self._collect(print_matches=False), "")

    def test_claude_md_copy_logged(self):
        (self.repo / "CLAUDE.md").write_text("# c")
        out = self._collect(print_matches=True)
        self.assertIn("INFO repository_cloning: Copied CLAUDE.md of owner/repo (CLAUDE.md)", out)

    def test_claude_dir_copy_logged(self):
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "x.json").write_text("{}")
        out = self._collect(print_matches=True)
        self.assertIn("INFO repository_cloning: Copied .claude of owner/repo (.claude directory)",
                      out)

    def test_symlink_logged_with_marker(self):
        (self.repo / "real.md").write_text("real")
        (self.repo / "CLAUDE.md").symlink_to("real.md")
        out = self._collect(print_matches=True)
        self.assertIn("Copied CLAUDE.md of owner/repo as a symlink target string (CLAUDE.md)", out)

    def test_cursor_file_uses_basename_label(self):
        (self.repo / ".cursorrules").write_text("rules")
        out = self._collect(print_matches=True)
        self.assertIn("Copied .cursorrules of owner/repo (.cursorrules)", out)

    def test_claude_plugin_sibling_label(self):
        _tracked_dir(self.repo / ".claude-plugin")
        (self.repo / "README.md").write_text("readme")
        out = self._collect(print_matches=True)
        self.assertIn("Copied README.md of owner/repo (.claude-plugin sibling README.md)", out)

    def test_copilot_trigger_not_found_info_logged(self):
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "workflows").mkdir()
        out = self._collect(print_matches=True)
        self.assertIn("INFO repository_cloning: owner/repo has no Copilot trigger. Skipped .github.",
                      out)

    def test_nested_relpath_logged(self):
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "CLAUDE.md").write_text("# c")
        out = self._collect(print_matches=True)
        self.assertIn("Copied sub/CLAUDE.md of owner/repo (CLAUDE.md)", out)


class TestNamesThatAreNotUtf8(unittest.TestCase):
    """A Latin-1 file name under a copied `.github/` is left out of the tree, and logged,
    because `finished_repositories.jsonl`'s JSON would escape it and the cache writer would
    raise on it at the end of the run, and again on every resume."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_lone_surrogate_is_what_such_a_name_looks_like(self):
        self.assertTrue(paths.undecodable_name(os.fsdecode(b".github/caf\xe9.md")))
        self.assertFalse(paths.undecodable_name(".github/caf\u00e9.md"))
        self.assertFalse(paths.undecodable_name("CLAUDE.md"))

    def test_such_a_name_is_printed_with_the_bytes_it_has(self):
        """On every file system, including one that refuses to create the name."""
        name = os.fsdecode(b".github/caf\xe9.md")
        self.assertEqual(paths.printable_name(name), ".github/caf\\xe9.md")
        self.assertEqual(paths.printable_name(".github/caf\u00e9.md"), ".github/caf\u00e9.md")

    def _latin1_file(self, root: Path) -> bool:
        (root / ".github").mkdir(parents=True)
        try:
            with open(os.fsencode(str(root / ".github")) + b"/caf\xe9.md", "w") as f:
                f.write("x")
        except OSError:
            return False
        return True

    def test_the_tree_is_left_without_it(self):
        root = self.tmp / "tree"
        if not self._latin1_file(root):
            self.skipTest("this file system refuses a name that is not UTF-8")
        (root / ".github" / "copilot-instructions.md").write_text("keep")
        removed = paths.remove_undecodable_names(root)
        self.assertEqual(removed, [".github/caf\\xe9.md"])
        self.assertEqual([p.name for p in (root / ".github").iterdir()],
                         ["copilot-instructions.md"])
        self.assertEqual(paths.remove_undecodable_names(root), [])

    def test_process_repo_logs_what_it_left_out(self):
        remote = self.tmp / "remote"
        _init_git_repo(remote)
        (remote / ".github").mkdir()
        (remote / ".github" / "copilot-instructions.md").write_text("# copilot\n" + "x" * 90)
        subprocess.run(["git", "-C", str(remote), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-q", "-m", "copilot"],
                       check=True, capture_output=True)
        cfg = CollectionConfig(collected_dir=self.tmp / "out", clone_dir=self.tmp / "scratch",
                            log_dir=self.tmp / "logs", max_tries=1, base_delay=0,
                            measure_git=False)
        os.environ["GIT_CONFIG_COUNT"] = "1"
        os.environ["GIT_CONFIG_KEY_0"] = f"url.file://{remote}.insteadOf"
        os.environ["GIT_CONFIG_VALUE_0"] = "https://github.com/owner/repo.git"
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in
                                 ("GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0")])
        with mock.patch.object(paths, "remove_undecodable_names",
                               return_value=[".github/caf\\xe9.md"]), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            _, _, fail_row, _, _, _measures = process_repo(cfg, "owner/repo")
        self.assertIsNone(fail_row)
        self.assertIn("Removed 1 path of owner/repo from", err.getvalue())
        self.assertIn(str(cfg.collected_dir / "owner§repo"), err.getvalue())
        self.assertIn("names are not valid UTF-8", err.getvalue())
        self.assertIn("UNDECODABLE_NAMES", (cfg.log_dir / "owner§repo.log").read_text())

    def test_a_symlink_target_that_is_not_utf8_is_a_marker_not_an_error(self):
        # Written with the strict default encoding, the target string would make a link to
        # such a name a `collector_error` on every run.
        src = self.tmp / "AGENTS.md"
        os.symlink(os.fsdecode(b"docs/caf\xe9.md"), src)
        dest = self.tmp / "out" / "AGENTS.md"
        paths.copy_file(src, dest)
        self.assertEqual(dest.read_text(encoding="utf-8"), "docs/caf\\xe9.md\n")

    def test_a_gitlink_path_that_is_not_utf8_does_not_end_the_worker(self):
        """`git ls-tree` writes the path's bytes. Decoded strictly, such a path would be an
        exception that the worker records as a clone failure and retries forever."""
        repo = self.tmp / "repo"
        _init_git_repo(repo)
        sha = "a" * 40
        subprocess.run(["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
                        b"160000," + sha.encode() + b",sub\xe9"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "gitlink"],
                       check=True, capture_output=True)
        rows = _clone.submodule_rows("owner/repo", repo)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["path"], "sub\ufffd")


class TestCollectShared(unittest.TestCase):
    """The .agents directory and the plugin manifest directories."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, rel: str, content: str = "x\n") -> Path:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def test_agents_skills_are_collected(self):
        self._file(".agents/skills/pdf/SKILL.md")
        collect_shared(self.repo, self.out, set())
        self.assertTrue((self.out / ".agents/skills/pdf/SKILL.md").is_file())

    def test_agents_sets_no_tool_flag(self):
        # As AGENTS.md is, .agents is read by several tools and names none.
        self._file(".agents/skills/pdf/SKILL.md")
        self.assertEqual(collect_shared(self.repo, self.out, set()), set())

    def test_cursor_and_codex_plugin_dirs_are_collected(self):
        self._file(".cursor-plugin/plugin.json", "{}")
        self._file(".codex-plugin/plugin.json", "{}")
        collect_shared(self.repo, self.out, set())
        self.assertTrue((self.out / ".cursor-plugin/plugin.json").is_file())
        self.assertTrue((self.out / ".codex-plugin/plugin.json").is_file())

    def test_per_tool_dirs_are_left_to_their_own_pass(self):
        self._file(".claude/settings.json", "{}")
        collect_shared(self.repo, self.out, set())
        self.assertFalse((self.out / ".claude").exists())

    def test_agents_is_found_at_any_depth(self):
        self._file("packages/web/.agents/skills/pdf/SKILL.md")
        collect_shared(self.repo, self.out, set())
        self.assertTrue(
            (self.out / "packages/web/.agents/skills/pdf/SKILL.md").is_file())

    def test_an_agent_directory_is_collected_only_with_a_place_of_antigravity(self):
        """Antigravity reads `rules/`, `skills/`, `agents/`, `plugins/`, `hooks.json` and
        `mcp_config.json` in a customization folder such as `_agent/`
        (https://antigravity.google/docs/customization-discovery, read 2026-11-05). Azure's
        `red_team/_agent/` is a Python package with none of them, and the step copies none of
        its modules."""
        package = "sdk/evaluation/azure-ai-evaluation/azure/ai/evaluation/red_team/_agent"
        self._file(f"{package}/__init__.py",
                   "# Copyright (c) Microsoft Corporation. All rights reserved.\n")
        self._file(f"{package}/_agent_tools.py", "import json\n")
        self._file("_agents/skills/create-microbenchmark/SKILL.md")
        self._file("pkg/_agent/hooks.json", "{}\n")
        self._file("pkg/_agent/notes.md")
        collect_shared(self.repo, self.out, set())
        self.assertFalse((self.out / package).exists())
        self.assertTrue((self.out / "_agents/skills/create-microbenchmark/SKILL.md").is_file())
        self.assertTrue((self.out / "pkg/_agent/notes.md").is_file())


class TestCollectSymlinkTargets(unittest.TestCase):
    """A symlinked mechanism directory keeps both the marker and the content."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _link_skills_to_agents(self):
        (self.repo / ".agents" / "skills" / "pdf").mkdir(parents=True)
        (self.repo / ".agents" / "skills" / "pdf" / "SKILL.md").write_text("x\n")
        (self.repo / ".claude").mkdir()
        os.symlink("../.agents/skills", self.repo / ".claude" / "skills")

    def test_target_inside_the_repo_is_copied(self):
        self._link_skills_to_agents()
        collect_symlink_targets(self.repo, self.out, set())
        self.assertTrue((self.out / ".agents/skills/pdf/SKILL.md").is_file())

    def test_a_link_to_a_link_names_the_end_of_the_chain_that_stays_in_the_tree(self):
        """The collector copies only the end of a chain of links, and a marker that named the
        first hop would name a path that the collected tree lacks. The detection step
        would find no item, and the cleanup would delete the end. NVIDIA/Model-Optimizer links its
        `.claude/skills/<name>` to `.agents/skills/<name>`, and `.agents/skills` to
        `../plugins/modelopt/skills`."""
        from agentcontext.steps import collected_file_cleanup, detection
        (self.repo / "shared" / "skills" / "foo").mkdir(parents=True)
        (self.repo / "shared" / "skills" / "foo" / "SKILL.md").write_text(
            "---\nname: foo\ndescription: Does foo.\n---\nBody.\n")
        (self.repo / "shared" / "rev.md").write_text(
            "---\nname: rev\ndescription: Reviews the code.\n---\nReview.\n")
        (self.repo / "agents").mkdir()
        os.symlink("../shared/rev.md", self.repo / "agents" / "rev.md")
        (self.repo / ".claude" / "agents").mkdir(parents=True)
        os.symlink("../../agents/rev.md", self.repo / ".claude" / "agents" / "rev.md")
        os.symlink("shared/skills", self.repo / "lnk")
        os.symlink("../lnk", self.repo / ".claude" / "skills")
        # Through a linked directory, as NVIDIA/Model-Optimizer's skills are linked.
        (self.repo / "plugins" / "p" / "skills" / "bar").mkdir(parents=True)
        (self.repo / "plugins" / "p" / "skills" / "bar" / "SKILL.md").write_text(
            "---\nname: bar\ndescription: Does bar.\n---\nBody.\n")
        (self.repo / ".agents").mkdir()
        os.symlink("../plugins/p/skills", self.repo / ".agents" / "skills")
        (self.repo / ".codex" / "skills").mkdir(parents=True)
        os.symlink("../../.agents/skills/bar", self.repo / ".codex" / "skills" / "bar")
        # A link whose target is the file itself keeps its text.
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "style.md").write_text(
            "---\nname: style\ndescription: Checks the style.\n---\nCheck.\n")
        os.symlink("../../docs/./style.md", self.repo / ".claude" / "agents" / "style.md")
        with contextlib.redirect_stderr(io.StringIO()):
            paths.collect_repo(self.repo, self.out, set())
        self.assertEqual((self.out / ".claude/agents/rev.md").read_text(), "../../shared/rev.md\n")
        self.assertEqual((self.out / ".claude/skills").read_text(), "../shared/skills\n")
        self.assertEqual((self.out / ".codex/skills/bar").read_text(),
                         "../../plugins/p/skills/bar\n")
        self.assertEqual((self.out / ".claude/agents/style.md").read_text(),
                         "../../docs/./style.md\n")
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(self.out), [])
        self.assertTrue((self.out / "shared/rev.md").is_file())
        self.assertTrue((self.out / "shared/skills/foo/SKILL.md").is_file())
        self.assertIn(".claude/agents/rev.md", str(detection.detect_subagents(self.out)))
        self.assertIn("foo", str(detection.detect_skills(self.out)))
        self.assertIn("bar", str(detection.detect_skills(self.out)))
        self.assertTrue((self.out / "plugins/p/skills/bar/SKILL.md").is_file())

    def test_a_link_to_a_path_with_a_space_keeps_its_item(self):
        """A marker of one line with a space reads as prose, unless it names a path beside it.
        The collector copies `Shared Skills/pdf`, and a cleanup and a detection step that read
        the marker as prose would delete the copy and list the link as another file."""
        from agentcontext.steps import collected_file_cleanup, detection
        (self.repo / "Shared Skills" / "pdf").mkdir(parents=True)
        (self.repo / "Shared Skills" / "pdf" / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Reads a PDF.\n---\nBody.\n")
        (self.repo / "agent docs").mkdir()
        (self.repo / "agent docs" / "reviewer.md").write_text(
            "---\nname: reviewer\ndescription: Reviews the code.\n---\nReview.\n")
        (self.repo / ".claude" / "skills").mkdir(parents=True)
        os.symlink("../../Shared Skills/pdf", self.repo / ".claude" / "skills" / "pdf")
        (self.repo / ".claude" / "agents").mkdir()
        os.symlink("../../agent docs/reviewer.md", self.repo / ".claude" / "agents" / "reviewer.md")
        with contextlib.redirect_stderr(io.StringIO()):
            paths.collect_repo(self.repo, self.out, set())
            collected_file_cleanup.scan_and_clean_repo(str(self.out), [])
        self.assertTrue((self.out / "Shared Skills/pdf/SKILL.md").is_file())
        self.assertTrue((self.out / "agent docs/reviewer.md").is_file())
        self.assertIn("pdf", str(detection.detect_skills(self.out)))
        self.assertIn(".claude/agents/reviewer.md", str(detection.detect_subagents(self.out)))
        self.assertIsNone(paths.marker_target("Use tabs, not spaces.", self.out))

    def test_a_link_to_a_path_that_ends_with_a_space_keeps_its_item(self):
        """The collector copies `shared/pdf ` as git stored the target, and a cleanup and a
        detection step that read the marker without the space would delete the copy and drop
        the repository."""
        from agentcontext.steps import collected_file_cleanup, detection
        (self.repo / "shared" / "pdf ").mkdir(parents=True)
        (self.repo / "shared" / "pdf " / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Reads a PDF.\n---\nBody.\n")
        (self.repo / ".claude" / "skills").mkdir(parents=True)
        os.symlink("../../shared/pdf ", self.repo / ".claude" / "skills" / "pdf")
        with contextlib.redirect_stderr(io.StringIO()):
            paths.collect_repo(self.repo, self.out, set())
            collected_file_cleanup.scan_and_clean_repo(str(self.out), [])
        self.assertTrue((self.out / "shared/pdf /SKILL.md").is_file())
        self.assertIn("pdf", str(detection.detect_skills(self.out)))

    def test_a_chain_through_a_target_that_ends_with_a_space_names_its_end(self):
        """The marker of the first hop names `../../.agents/skills/pdf `, a link that the
        collected tree lacks, unless the collector rewrites it to the path that the chain ends
        at, which it does when the marker is the link's own target."""
        from agentcontext.steps import detection
        (self.repo / "shared" / "pdf").mkdir(parents=True)
        (self.repo / "shared" / "pdf" / "SKILL.md").write_text(
            "---\nname: pdf\ndescription: Reads a PDF.\n---\nBody.\n")
        (self.repo / ".agents" / "skills").mkdir(parents=True)
        os.symlink("../../shared/pdf", self.repo / ".agents" / "skills" / "pdf ")
        (self.repo / ".claude" / "skills").mkdir(parents=True)
        os.symlink("../../.agents/skills/pdf ", self.repo / ".claude" / "skills" / "pdf")
        with contextlib.redirect_stderr(io.StringIO()):
            paths.collect_repo(self.repo, self.out, set())
        self.assertEqual((self.out / ".claude/skills/pdf").read_text(encoding="utf-8"),
                         "../../shared/pdf\n")
        self.assertIn("pdf", str(detection.detect_skills(self.out)))

    def test_a_target_rsync_cannot_copy_fails_the_repository(self):
        # A target directory that rsync copied only in part is not a warning about one
        # unreadable link: it raises, and the cloning step records the raise as
        # `collector_error` rather than a partial tree as collected.
        self._link_skills_to_agents()
        unreadable = self.repo / ".agents" / "skills" / "pdf" / "SKILL.md"
        os.chmod(unreadable, 0o000)
        if os.access(unreadable, os.R_OK):
            self.skipTest("this user can read a file with a mode that denies it")
        with self.assertRaises(subprocess.CalledProcessError) as raised:
            collect_symlink_targets(self.repo, self.out, set())
        self.assertIn("SKILL.md", str(raised.exception))

    def test_a_full_disk_while_following_a_link_fails_the_repository(self):
        self._link_skills_to_agents()
        full = OSError(errno.ENOSPC, "No space left on device")
        with mock.patch.object(paths, "copy_file", side_effect=full), \
                self.assertRaises(OSError) as raised:
            collect_symlink_targets(self.repo, self.out, set())
        self.assertEqual(raised.exception.errno, errno.ENOSPC)

    def test_a_link_that_cannot_be_read_is_a_warning(self):
        self._link_skills_to_agents()
        unreadable = OSError(errno.EACCES, "Permission denied")
        with mock.patch.object(paths, "copy_file", side_effect=unreadable), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            collect_symlink_targets(self.repo, self.out, set())
        self.assertIn("Could not follow the link .claude/skills", err.getvalue())

    def test_target_outside_the_repo_leaves_only_the_marker(self):
        # Nothing outside the repository is ever published, but the marker
        # records that the directory existed and where it pointed.
        outside = self.tmp / "elsewhere" / "skills"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("x\n")
        (self.repo / ".claude").mkdir()
        os.symlink(str(outside), self.repo / ".claude" / "skills")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertTrue((self.out / ".claude" / "skills").is_file())
        self.assertEqual(list(self.out.rglob("SKILL.md")), [])

    def test_a_link_to_the_repository_root_leaves_only_the_marker(self):
        # rsync would otherwise copy the whole working tree into the collected
        # configuration artifacts, .env included, and replace the marker with a live symlink.
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.py").write_text("x\n")
        (self.repo / ".env").write_text("TOKEN=hunter2\n")
        (self.repo / ".claude").mkdir()
        os.symlink("..", self.repo / ".claude" / "skills")
        collect_symlink_targets(self.repo, self.out, set())
        marker = self.out / ".claude" / "skills"
        self.assertTrue(marker.is_file())
        self.assertFalse(marker.is_symlink())
        self.assertFalse((self.out / ".env").exists())
        self.assertFalse((self.out / "src").exists())

    def test_a_link_to_an_ancestor_package_leaves_only_the_marker(self):
        # The same case one level down: the target need not be the repository
        # root, only a directory that contains the link.
        pkg = self.repo / "packages" / "web"
        (pkg / "src").mkdir(parents=True)
        (pkg / "src" / "app.py").write_text("x\n")
        (pkg / ".env").write_text("TOKEN=hunter2\n")
        (pkg / ".claude").mkdir()
        os.symlink("../..", pkg / ".claude" / "skills")
        collect_symlink_targets(self.repo, self.out, set())
        marker = self.out / "packages" / "web" / ".claude" / "skills"
        self.assertTrue(marker.is_file())
        self.assertFalse(marker.is_symlink())
        self.assertFalse((self.out / "packages" / "web" / ".env").exists())
        self.assertFalse((self.out / "packages" / "web" / "src").exists())

    def test_a_broken_symlink_leaves_only_the_marker(self):
        (self.repo / ".claude").mkdir()
        os.symlink("../nowhere", self.repo / ".claude" / "skills")
        collect_symlink_targets(self.repo, self.out, set())
        marker = self.out / ".claude" / "skills"
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text().strip(), "../nowhere")

    def test_a_real_directory_is_left_alone(self):
        (self.repo / ".claude" / "skills" / "pdf").mkdir(parents=True)
        (self.repo / ".claude" / "skills" / "pdf" / "SKILL.md").write_text("x\n")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertEqual(list(self.out.rglob("*")), [])

    def test_the_marker_and_the_target_both_survive_a_full_pass(self):
        # collect_claude writes .claude/skills as a one-line marker. The
        # symlink pass then adds what it points at, so that the detector can follow
        # the link instead of reading a text file where a directory should be.
        self._link_skills_to_agents()
        collect_repo(self.repo, self.out, set())
        marker = self.out / ".claude" / "skills"
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text().strip(), "../.agents/skills")
        self.assertTrue((self.out / ".agents/skills/pdf/SKILL.md").is_file())

    def test_a_target_an_earlier_pass_wrote_into_is_merged(self):
        """The Codex pass copies shared/AGENTS.md first, and shared/ already exists
        when the symlink pass reaches it. Skipping a target that merely exists would lose the skill
        beside it."""
        (self.repo / "shared" / "pdf").mkdir(parents=True)
        (self.repo / "shared" / "AGENTS.md").write_text("# agents\n")
        (self.repo / "shared" / "pdf" / "SKILL.md").write_text("x\n")
        (self.repo / ".claude").mkdir()
        os.symlink("../shared", self.repo / ".claude" / "skills")
        collect_repo(self.repo, self.out, set())
        self.assertEqual((self.out / "shared" / "AGENTS.md").read_text(), "# agents\n")
        self.assertEqual((self.out / "shared" / "pdf" / "SKILL.md").read_text(), "x\n")
        self.assertEqual((self.out / ".claude" / "skills").read_text().strip(), "../shared")


class TestPerEntrySymlinks(unittest.TestCase):
    """A single linked skill, and a mechanism directory below the repository root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self.out = self.tmp / "out"
        self.out.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_single_linked_skill_is_marked_and_copied(self):
        (self.repo / "shared" / "pdf").mkdir(parents=True)
        (self.repo / "shared" / "pdf" / "SKILL.md").write_text("x\n")
        (self.repo / ".claude" / "skills").mkdir(parents=True)
        os.symlink("../../shared/pdf", self.repo / ".claude" / "skills" / "pdf")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertTrue((self.out / ".claude/skills/pdf").is_file())
        self.assertTrue((self.out / "shared/pdf/SKILL.md").is_file())

    def test_a_skill_md_that_is_a_link_is_marked_and_its_file_copied(self):
        """feast-dev/feast keeps `.claude/skills/feast-dev/` as a folder and its SKILL.md as a
        link to `../../../skills/feast-dev/SKILL.md`. Claude Code reads the skill through the
        link."""
        (self.repo / "skills" / "feast-dev").mkdir(parents=True)
        (self.repo / "skills" / "feast-dev" / "SKILL.md").write_text("---\nname: feast-dev\n---\n")
        (self.repo / ".claude" / "skills" / "feast-dev").mkdir(parents=True)
        os.symlink("../../../skills/feast-dev/SKILL.md",
                   self.repo / ".claude" / "skills" / "feast-dev" / "SKILL.md")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertEqual((self.out / ".claude/skills/feast-dev/SKILL.md").read_text(),
                         "../../../skills/feast-dev/SKILL.md\n")
        self.assertEqual((self.out / "skills/feast-dev/SKILL.md").read_text(),
                         "---\nname: feast-dev\n---\n")

    def test_another_link_in_a_skill_folder_stays_a_marker(self):
        """Only the SKILL.md of a skill folder is its item. A link to a file beside it, which the
        skill may name, is kept as a marker, and its target is not copied."""
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "guide.md").write_text("x\n")
        skill = self.repo / ".claude" / "skills" / "pdf"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: pdf\n---\n")
        os.symlink("../../../docs/guide.md", skill / "guide.md")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertFalse((self.out / "docs").exists())

    def test_a_skill_md_link_outside_a_skills_directory_is_not_followed(self):
        (self.repo / "shared").mkdir()
        (self.repo / "shared" / "SKILL.md").write_text("x\n")
        (self.repo / ".claude" / "agents" / "pdf").mkdir(parents=True)
        os.symlink("../../../shared/SKILL.md", self.repo / ".claude" / "agents" / "pdf" / "SKILL.md")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertFalse((self.out / "shared").exists())

    def test_a_mechanism_directory_below_the_root_is_followed(self):
        (self.repo / "shared" / "skills").mkdir(parents=True)
        (self.repo / "shared" / "skills" / "pdf").mkdir()
        (self.repo / "shared" / "skills" / "pdf" / "SKILL.md").write_text("x\n")
        (self.repo / "packages" / "web" / ".claude").mkdir(parents=True)
        os.symlink("../../../shared/skills",
                   self.repo / "packages" / "web" / ".claude" / "skills")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertTrue((self.out / "packages/web/.claude/skills").is_file())
        self.assertTrue((self.out / "shared/skills/pdf/SKILL.md").is_file())

    def test_a_linked_rule_in_antigravity_s_rules_folder_is_marked_and_copied(self):
        """The step follows the links in the rules folders of Antigravity. Without them, the file
        behind a linked rule would never be collected, and the rule's trigger could not be
        read."""
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "team.md").write_text("---\ntrigger: always_on\n---\nx\n")
        (self.repo / ".agents" / "rules").mkdir(parents=True)
        os.symlink("../../docs/team.md", self.repo / ".agents" / "rules" / "team.md")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertEqual((self.out / ".agents/rules/team.md").read_text(), "../../docs/team.md\n")
        self.assertTrue((self.out / "docs/team.md").is_file())

    def test_the_file_behind_a_linked_agent_of_a_plugin_is_copied(self):
        """Antigravity CLI 1.4.2 counted an agent of a plugin that links to `shared/x.md`."""
        (self.repo / "shared").mkdir()
        (self.repo / "shared" / "x.md").write_text("---\nname: x\ndescription: d\n---\nbody\n")
        plugin = self.repo / ".agents" / "plugins" / "p"
        (plugin / "agents").mkdir(parents=True)
        (plugin / "plugin.json").write_text('{"name": "p"}\n')
        os.symlink("../../../../shared/x.md", plugin / "agents" / "x.md")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertTrue((self.out / "shared/x.md").is_file())

    def test_a_link_into_the_git_directory_leaves_only_the_marker(self):
        """The file behind a link to `.git/logs/HEAD` is the clone's log and no file of the
        repository."""
        (self.repo / ".git" / "logs").mkdir(parents=True)
        (self.repo / ".git" / "logs" / "HEAD").write_text("0000 1111 clone: from github\n")
        (self.repo / ".claude" / "agents").mkdir(parents=True)
        os.symlink("../../.git/logs/HEAD", self.repo / ".claude" / "agents" / "log.md")
        collect_symlink_targets(self.repo, self.out, set())
        self.assertEqual((self.out / ".claude/agents/log.md").read_text(), "../../.git/logs/HEAD\n")
        self.assertFalse((self.out / ".git").exists())


def _x(*rels):
    """Files with content that does not matter to the collector."""
    return {rel: "x\n" for rel in rels}


def _uses(action):
    return {".github/workflows/nightly.yml":
            f"jobs:\n  go:\n    steps:\n      - uses: {action}@v1\n"}


def _run(line):
    """A workflow with a shell step that runs `line`, as the workflows of the September 2026
    collection write it."""
    return {".github/workflows/review.yml":
            f"jobs:\n  go:\n    steps:\n      - run: |\n          {line}\n"}


PLUGIN = {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n'}

# One repository per row of heuristics/config_path_patterns.csv, keyed by the row's mechanism,
# path and match mode: the files that it contains, each of which a full collector pass has to
# copy with its content. A path row gets a file at that path, a directory row a file inside it,
# a basename row a file below the root, and a row that requires another file a repository that
# has that file.
ROW_CASES = {
    ("context_file", "CLAUDE.md", "basename"): _x("packages/web/CLAUDE.md"),
    ("context_file", "GEMINI.md", "basename"): _x("packages/web/GEMINI.md"),
    ("context_file", "AGENTS.md", "basename"): _x("packages/web/AGENTS.md"),
    ("context_file", "AGENTS.override.md", "basename"): _x("packages/web/AGENTS.override.md"),
    ("context_file", "CLAUDE.local.md", "basename"): _x("packages/web/CLAUDE.local.md"),
    ("context_file", ".cursorrules", "basename"): _x("packages/web/.cursorrules"),
    ("context_file", ".cursor/BUGBOT.md", "file"): _x(".cursor/BUGBOT.md"),
    ("context_file", ".gemini/styleguide.md", "root_file"): _x(".gemini/styleguide.md"),
    ("context_file", ".github/copilot-instructions.md", "file"):
        _x(".github/copilot-instructions.md"),
    ("context_file", ".github/instructions", "tree_suffix:.instructions.md"):
        _x(".github/instructions/api/web.instructions.md"),
    ("context_file", ".gemini/settings.json", "declared_names:context.fileName"):
        {".gemini/settings.json": '{"context": {"fileName": ["CONTEXT.md"]}}\n',
         "packages/web/CONTEXT.md": "x\n"},
    ("context_file", "gemini-extension.json", "declared_local:contextFileName"):
        {"ext/gemini-extension.json": '{"name": "ext", "contextFileName": "EXT.md"}\n',
         "ext/EXT.md": "x\n"},
    ("context_file", ".codex/config.toml", "declared_path:model_instructions_file"):
        {".codex/config.toml": 'model_instructions_file = "../TEAM.md"\n', "TEAM.md": "x\n"},
    ("context_file", ".codex/config.toml", "declared_fallback:project_doc_fallback_filenames"):
        {".codex/config.toml": 'project_doc_fallback_filenames = ["TEAM_GUIDE.md"]\n',
         "packages/web/TEAM_GUIDE.md": "x\n"},
    ("settings", ".claude/settings.json", "file"): _x(".claude/settings.json"),
    ("settings", ".claude/settings.local.json", "file"): _x(".claude/settings.local.json"),
    ("settings", ".gemini/settings.json", "file"): _x(".gemini/settings.json"),
    ("settings", ".gemini/config.yaml", "root_file"): _x(".gemini/config.yaml"),
    ("settings", ".codex/config.toml", "file"): _x(".codex/config.toml"),
    ("settings", ".cursor/cli.json", "file"): _x(".cursor/cli.json"),
    ("settings", ".cursor/worktrees.json", "file"): _x(".cursor/worktrees.json"),
    ("settings", ".cursor/environment.json", "file"): _x(".cursor/environment.json"),
    ("settings", ".cursor/Dockerfile", "file"): _x(".cursor/Dockerfile"),
    ("settings", ".cursor/sandbox.json", "file"): _x(".cursor/sandbox.json"),
    ("settings", ".cursor/permissions.json", "file"): _x(".cursor/permissions.json"),
    ("settings", ".claude/launch.json", "file"): _x(".claude/launch.json"),
    ("settings", "gemini-extension.json", "basename"):
        _x("extensions/demo/gemini-extension.json"),
    ("settings", ".codex/environments", "tree_ext:.toml"): _x(".codex/environments/ci.toml"),
    ("skills", ".claude/skills", "skill_dir_flat"): _x(".claude/skills/pdf/SKILL.md"),
    # The rows of Cursor and OpenCode, which read the category folders of `.claude/skills`.
    ("skills", ".claude/skills", "skill_dir"): _x(".claude/skills/gitnexus/gitnexus-cli/SKILL.md"),
    ("skills", ".gemini/skills", "skill_dir_flat"): _x(".gemini/skills/pdf/SKILL.md"),
    ("skills", ".codex/skills", "skill_dir"): _x(".codex/skills/pdf/SKILL.md"),
    ("skills", ".github/skills", "skill_dir"): _x(".github/skills/pdf/SKILL.md"),
    ("skills", ".cursor/skills", "skill_dir"): _x(".cursor/skills/pdf/SKILL.md"),
    ("skills", ".agents/skills", "skill_dir"): _x(".agents/skills/pdf/SKILL.md"),
    # Under .github with nothing that copies the directory, where only the SKILL.md pass copies
    # the file.
    ("skills", "SKILL.md", "skill_marker"): _x(".github/tools/pdf/SKILL.md"),
    ("subagents", ".claude/agent-memory/*", "agent_memory"):
        _x(".claude/agent-memory/reviewer/MEMORY.md", ".claude/agent-memory/reviewer/notes.md"),
    ("subagents", ".claude/agent-memory-local/*", "agent_memory"):
        _x(".claude/agent-memory-local/reviewer/MEMORY.md"),
    ("skills", ".claude/commands", "tree_ext:.md"): _x(".claude/commands/review.md"),
    ("subagents", ".claude/agents", "tree_ext:.md"): _x(".claude/agents/reviewer.md"),
    ("subagents", ".github/agents", "dir_ext:.md"): _x(".github/agents/reviewer.md"),
    ("subagents", ".github/chatmodes", "dir_ext:.chatmode.md"):
        _x(".github/chatmodes/plan.chatmode.md"),
    ("subagents", ".cursor/agents", "tree_ext:.md|.mdc|.markdown"): _x(".cursor/agents/reviewer.md"),
    ("subagents", ".gemini/agents", "dir_ext_described:.md"): {".gemini/agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews code.\n---\nReview.\n"},
    ("subagents", ".codex/agents", "tree_ext:.toml"): _x(".codex/agents/reviewer.toml"),
    ("subagents", ".codex/agents", "tree_ext:.md"): _x(".codex/agents/reviewer.md"),
    ("subagents", ".codex/agents", "declared_path:model_instructions_file"):
        {".codex/agents/planner.toml": 'name = "planner"\n'
                                       'model_instructions_file = "../../docs/planner.md"\n',
         "docs/planner.md": "Plan the change.\n"},
    ("subagents", ".codex/config.toml", "declared_path:agents.*.config_file"):
        {".codex/config.toml": '[agents.reviewer]\nconfig_file = "../roles/reviewer.toml"\n',
         "roles/reviewer.toml": 'name = "reviewer"\n'},
    ("subagents", ".claude/agents", "tree_ext:.md|.mdc|.markdown"): _x(".claude/agents/reviewer.mdc"),
    ("commands", ".gemini/commands", "tree_ext:.toml"): _x(".gemini/commands/review.toml"),
    ("commands", ".cursor/commands", "tree_ext:.md"): _x(".cursor/commands/git/review.md"),
    ("commands", ".github/prompts", "dir_ext:.prompt.md"):
        _x(".github/prompts/review.prompt.md"),
    ("hooks", ".claude/settings.json", "json_hooks"):
        {".claude/settings.json": '{"hooks": {"Stop": []}}\n'},
    ("hooks", ".gemini/settings.json", "json_hooks"):
        {".gemini/settings.json": '{"hooks": {"AfterTool": []}}\n'},
    ("hooks", ".claude/settings.local.json", "json_hooks"):
        {".claude/settings.local.json": '{"hooks": {"Stop": []}}\n'},
    ("hooks", ".codex/hooks.json", "json_hooks"): {".codex/hooks.json": '{"hooks": {}}\n'},
    ("hooks", ".cursor/hooks.json", "json_hooks"): {".cursor/hooks.json": '{"hooks": {}}\n'},
    ("settings", ".github/copilot/settings.json", "file"): _x(".github/copilot/settings.json"),
    ("settings", ".github/copilot/settings.local.json", "file"):
        _x(".github/copilot/settings.local.json"),
    ("settings", ".github/allowed_models.txt", "file"): _x(".github/allowed_models.txt"),
    # The workflow rows matched by name contain a file that runs no action, and only the name
    # brings it into the collection.
    ("settings", ".github/workflows/copilot-setup-steps.yml", "file"):
        _x(".github/workflows/copilot-setup-steps.yml"),
    ("settings", ".github/workflows/copilot-code-review.yml", "file"):
        _x(".github/workflows/copilot-code-review.yml"),
    ("settings", ".github/workflows/claude.yml", "file"): _x(".github/workflows/claude.yml"),
    ("settings", ".github/workflows/claude-code-review.yml", "file"):
        _x(".github/workflows/claude-code-review.yml"),
    ("settings", ".github/workflows/gemini-dispatch.yml", "file"):
        _x(".github/workflows/gemini-dispatch.yml"),
    ("settings", ".github/workflows",
     "workflow_command:claude -p||claude --print||@anthropic-ai/claude-code -p"
     "||@anthropic-ai/claude-code --print"):
        _run('claude -p "$(cat .github/prompts/lint-fix.md)"'),
    ("settings", ".github/workflows", "workflow_command:codex exec||@openai/codex exec"):
        _run("codex exec --full-auto"),
    ("settings", ".github/workflows",
     "workflow_command:gemini -p||gemini --prompt||@google/gemini-cli -p"
     "||@google/gemini-cli --prompt"):
        _run('gemini -p "Review this change"'),
    ("settings", ".github/workflows",
     "workflow_command:copilot -p||copilot --prompt||@github/copilot -p"
     "||@github/copilot --prompt"):
        _run('copilot -p "$(cat prompt.txt)" --no-ask-user'),
    ("settings", ".github/workflows", "workflow_command:curl https://cursor.com/install"):
        _run("curl https://cursor.com/install -fsS | bash"),
    ("settings", ".github/workflows", "workflow_uses:anthropics/claude-code-action"):
        _uses("anthropics/claude-code-action"),
    ("settings", ".github/workflows", "workflow_uses:anthropics/claude-code-base-action"):
        _uses("anthropics/claude-code-base-action"),
    ("settings", ".github/workflows", "workflow_uses:openai/codex-action"):
        _uses("openai/codex-action"),
    ("settings", ".github/workflows", "workflow_uses:google-github-actions/run-gemini-cli"):
        _uses("google-github-actions/run-gemini-cli"),
    ("settings", ".github/workflows", "workflow_engine:copilot"):
        {".github/workflows/triage.md": "---\non: issues\n---\nLabel the issue.\n"},
    ("settings", ".github/workflows", "workflow_engine:claude"):
        {".github/workflows/review.md": "---\non: pull_request\nengine: claude\n---\nReview.\n"},
    ("settings", ".github/workflows", "workflow_engine:codex"):
        {".github/workflows/fix.md": "---\non: issues\nengine:\n  id: codex\n---\nFix it.\n"},
    ("settings", ".github/workflows", "workflow_engine:gemini"):
        {".github/workflows/docs.md": "---\non: push\nengine: { id: gemini }\n---\nDocs.\n"},
    ("settings", ".github/workflows", "workflow_engine:cursor"):
        {".github/workflows/triage.md": "---\non: issues\nengine:\n  id: cursor\nimports:\n"
                                        "  - github/gh-aw/.github/workflows/shared/cursor.md@v1\n"
                                        "---\nTriage.\n"},
    ("context_file", ".claude/AGENTS.md", "file"): _x(".claude/AGENTS.md"),
    ("plugins", ".plugin/marketplace.json", "file"): _x(".plugin/marketplace.json"),
    ("plugins", "marketplace.json", "file"): _x("marketplace.json"),
    ("skills", ".agents/skills.json", "registered:skills"):
        {".agents/skills.json": '{"entries":[{"path":"shared/skills"}]}',
         "shared/skills/check/SKILL.md": "Check instructions"},
    ("subagents", ".agents/agents.json", "registered:subagents"):
        {".agents/agents.json": '{"entries":[{"path":"shared/agents"}]}',
         "shared/agents/check.md": "---\nname: check\ndescription: Check\n---\nCheck instructions"},
    ("commands", ".agents/workflows.json", "registered:commands"):
        {".agents/workflows.json": '{"entries":[{"path":"shared/workflows"}]}',
         "shared/workflows/check.md": "Check instructions"},
    ("plugins", ".agents/plugins.json", "registered:plugins"):
        {".agents/plugins.json": '{"entries":[{"path":"shared/plugins"}]}',
         "shared/plugins/check/plugin.json": '{"name":"check"}'},
    ("rules", "<antigravity-plugin>/rules.json", "declared_rules"):
        {".agents/plugins/check/plugin.json": '{"name":"check"}',
         ".agents/plugins/check/rules.json": '{"entries":[{"path":"shared/rules"}]}',
         "shared/rules/check.md": "---\ntrigger: always_on\n---\nCheck instructions"},
    ("rules", ".agents/rules.json", "declared_rules"):
        {".agents/rules.json": '{"entries": [{"path": "shared-rules"}]}\n',
         "shared-rules/style.md": "---\ntrigger: always_on\n---\nStyle.\n"},
    ("hooks", ".github/hooks", "tree_ext:.json"): _x(".github/hooks/pre.json"),
    ("hooks", ".claude", "name_glob:hookify.*.local.md"): _x(".claude/hookify.warn-rm.local.md"),
    ("hooks", ".codex/config.toml", "toml_hooks"):
        {".codex/config.toml": '[[hooks.Stop]]\ncommand = "make"\n'},
    ("rules", ".codex/rules", "dir_ext:.rules"): _x(".codex/rules/default.rules"),
    ("rules", ".cursor/rules", "tree_ext:.mdc"): _x(".cursor/rules/api/style.mdc"),
    ("rules", ".claude/rules", "tree_ext:.md"): _x(".claude/rules/style.md"),
    ("rules", ".claude/output-styles", "tree_ext:.md"): _x(".claude/output-styles/terse.md"),
    ("context_file", "REVIEW.md", "root_file"): _x("REVIEW.md"),
    ("mcp", ".mcp.json", "root_file"): _x(".mcp.json"),
    ("mcp", ".cursor/mcp.json", "file"): _x(".cursor/mcp.json"),
    ("mcp", ".gemini/settings.json", "json_mcp_servers"):
        {".gemini/settings.json": '{"mcpServers": {"demo": {"command": "x"}}}\n'},
    ("mcp", ".codex/config.toml", "toml_mcp_servers"):
        {".codex/config.toml": '[mcp_servers.demo]\ncommand = "x"\n'},
    ("mcp", ".github/mcp.json", "file"): _x(".github/mcp.json"),
    ("mcp", ".vscode/mcp.json", "file"): _x(".vscode/mcp.json"),
    ("mcp", "gemini-extension.json", "basename_json_mcp_servers"):
        {"extensions/demo/gemini-extension.json":
         '{"name": "demo", "mcpServers": {"demo": {"command": "x"}}}\n'},
    ("other", ".claude", "spec_dir"): _x(".claude/notes.txt"),
    ("other", ".claude/loop.md", "root_file"): _x(".claude/loop.md"),
    ("other", ".gemini", "spec_dir"): _x(".gemini/notes.txt"),
    ("other", ".cursor", "spec_dir"): _x(".cursor/notes.txt"),
    ("other", ".codex", "spec_dir"): _x(".codex/notes.txt"),
    # A Copilot path brings the whole directory, the unrelated files in it included.
    ("other", ".github", "unclassified_dir"):
        _x(".github/copilot-instructions.md", ".github/ISSUE_TEMPLATE/bug.md"),
    ("other", ".cursor-plugin", "unclassified_dir"): _x(".cursor-plugin/notes.txt"),
    ("other", ".codex-plugin", "unclassified_dir"): _x(".codex-plugin/notes.txt"),
    ("other", ".claude-plugin", "unclassified_dir"): _x(".claude-plugin/notes.txt"),
    ("other", ".agents", "spec_dir"): _x(".agents/notes.md"),
    # Antigravity's workspace files, counted as Gemini's.
    ("rules", ".agents/rules", "dir_ext_triggered:.md"):
        {".agents/rules/style.md": "---\ntrigger: always_on\n---\nStyle.\n"},
    ("rules", ".agent/rules", "dir_ext_triggered:.md"):
        {".agent/rules/style.md": "---\ntrigger: always_on\n---\nStyle.\n"},
    ("subagents", ".agents/agents", "dir_agent:.md"):
        {".agents/agents/reviewer/agent.md": "---\nname: reviewer\ndescription: Reviews.\n---\nx\n"},
    ("hooks", ".agents/hooks.json", "json_named_hooks"):
        {".agents/hooks.json": '{"lint": {"PreToolUse": []}}\n'},
    ("mcp", ".agents/mcp_config.json", "json_mcp_servers"):
        {".agents/mcp_config.json": '{"mcpServers": {"demo": {"serverUrl": "https://x/mcp/"}}}\n'},
    ("skills", ".agent/skills", "skill_dir_flat"): _x(".agent/skills/pdf/SKILL.md"),
    ("commands", ".agents/workflows", "dir_ext:.md"): _x(".agents/workflows/deploy.md"),
    ("other", ".agent", "spec_dir"): _x(".agent/notes.md"),
    ("other", ".geminiignore", "basename"): _x(".geminiignore"),
    ("other", ".cursorignore", "basename"): _x(".cursorignore"),
    ("other", ".cursorindexingignore", "basename"): _x(".cursorindexingignore"),
    ("other", ".aiexclude", "basename"): _x(".aiexclude"),
    ("collect_only", ".cursor-plugin", "copy_dir"): _x("plugins/demo/.cursor-plugin/plugin.json"),
    ("collect_only", ".codex-plugin", "copy_dir"): _x("plugins/demo/.codex-plugin/plugin.json"),
    ("collect_only", ".claude-plugin", "copy_dir"): _x("plugins/demo/.claude-plugin/plugin.json"),
    ("collect_only", "README.md", "plugin_sibling"): {**PLUGIN, **_x("plugins/demo/README.md")},
    ("collect_only", "readme.md", "plugin_sibling"): {**PLUGIN, **_x("plugins/demo/readme.md")},
    ("collect_only", "LICENSE", "plugin_sibling"): {**PLUGIN, **_x("plugins/demo/LICENSE")},
    ("collect_only", "LICENSE.md", "plugin_sibling"): {**PLUGIN, **_x("plugins/demo/LICENSE.md")},
    ("collect_only", "LICENSE.txt", "plugin_sibling"): {**PLUGIN, **_x("plugins/demo/LICENSE.txt")},
    ("collect_only", "package.json", "plugin_sibling"):
        {**PLUGIN, **_x("plugins/demo/package.json")},
    ("collect_only", "managed-mcp.json", "basename"): _x("config/managed-mcp.json"),
    # PLANS.md is copied only beside Codex configuration or an AGENTS.md.
    ("collect_only", "PLANS.md", "basename"): _x("AGENTS.md", "docs/PLANS.md"),
    ("collect_only", ".github", "copy_dir_gated"):
        _x(".github/agents/reviewer.md", ".github/workflows/ci.yml"),
    ("plugins", ".claude-plugin/plugin.json", "file"): _x(".claude-plugin/plugin.json"),
    ("plugins", ".claude-plugin/marketplace.json", "file"): _x(".claude-plugin/marketplace.json"),
    ("plugins", ".cursor-plugin/plugin.json", "file"): _x(".cursor-plugin/plugin.json"),
    ("plugins", ".codex-plugin/plugin.json", "file"): _x(".codex-plugin/plugin.json"),
    ("plugins", ".agents/plugins/marketplace.json", "file"):
        _x(".agents/plugins/marketplace.json"),
    ("plugins", "gemini-extension.json", "basename"):
        {"gemini-extension.json": '{"name": "demo", "version": "1.0.0"}\n'},
    ("plugins", "plugin.json", "basename_json_plugin_manifest"):
        {"plugins/demo/plugin.json": MANIFEST},
    ("context_file", "claude.md", "root_any_case"): _x("claude.md"),
    ("context_file", "gemini.md", "root_any_case"): _x("Gemini.md"),
    ("context_file", "agents.md", "root_any_case"): _x("agents.md"),
    ('skills', '<claude-plugin>/skills', 'skill_dir_flat'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/skills/deploy/SKILL.md": 'x\n'},
    ('skills', '<claude-plugin>/commands', 'tree_ext:.md'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/commands/status.md": 'x\n'},
    ('subagents', '<claude-plugin>/agents', 'tree_ext:.md'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/agents/reviewer.md": 'x\n'},
    ('hooks', '<claude-plugin>/hooks/hooks.json', 'json_hooks_or_modules'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/hooks/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ('mcp', '<claude-plugin>/.mcp.json', 'json_plugin_mcp'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/.mcp.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ('rules', '<claude-plugin>/output-styles', 'tree_ext:.md'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/output-styles/terse.md": 'x\n'},
    ('skills', '<claude-plugin>/workflows', 'tree_ext:.js'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/workflows/audit.js": 'x\n'},
    ('settings', '<claude-plugin>/settings.json', 'file'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/settings.json": '{"agent": "x"}\n'},
    ('rules', '<cursor-plugin>/rules', 'tree_ext:.md|.mdc|.markdown'):
        {"plugins/demo/.cursor-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/rules/style.mdc": 'x\n'},
    ('skills', '<cursor-plugin>/skills', 'skill_dir_flat'):
        {"plugins/demo/.cursor-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/skills/deploy/SKILL.md": 'x\n'},
    ('mcp', '<cursor-plugin>/mcp.json', 'json_mcp_servers'):
        {"plugins/demo/.cursor-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/mcp.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ('skills', '<codex-plugin>/skills', 'skill_dir'):
        {"plugins/demo/.codex-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/skills/deploy/SKILL.md": 'x\n'},
    ('mcp', '<codex-plugin>/.mcp.json', 'json_plugin_mcp'):
        {"plugins/demo/.codex-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/.mcp.json": '{"x": {"command": "y"}}\n'},
    ('hooks', '<codex-plugin>/hooks/hooks.json', 'json_hooks'):
        {"plugins/demo/.codex-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/hooks/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ('skills', '<agent-plugin>/skills', 'skill_dir_flat'):
        {"plugins/demo/plugin.json": MANIFEST, "plugins/demo/skills/deploy/SKILL.md": "x\n"},
    ('mcp', '<agent-plugin>/mcp.json', 'json_mcp_servers'):
        {"plugins/demo/plugin.json": MANIFEST, "plugins/demo/mcp.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ('hooks', '<agent-plugin>/hooks/hooks.json', 'json_hooks'):
        {"plugins/demo/plugin.json": MANIFEST, "plugins/demo/hooks/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ('subagents', '<agent-plugin>/com.github.copilot/agents', 'dir_ext:.md'):
        {"plugins/demo/plugin.json": MANIFEST,
         "plugins/demo/com.github.copilot/agents/reviewer.agent.md": "Review.\n"},
    ('commands', '<agent-plugin>/com.github.copilot/commands', 'dir_ext:.md'):
        {"plugins/demo/plugin.json": MANIFEST,
         "plugins/demo/com.github.copilot/commands/review.prompt.md": "Review.\n"},
    ('rules', '<agent-plugin>/com.github.copilot/rules', 'dir_ext_described:.mdc'):
        {"plugins/demo/plugin.json": MANIFEST,
         "plugins/demo/com.github.copilot/rules/git.mdc": "---\ndescription: Git\n---\nCommit small.\n"},
    ('hooks', '<agent-plugin>/com.github.copilot/hooks/hooks.json', 'json_hooks'):
        {"plugins/demo/plugin.json": MANIFEST,
         "plugins/demo/com.github.copilot/hooks/hooks.json": '{"version": 1, "hooks": {"sessionStart": []}}\n'},
    ('subagents', '<copilot-plugin>/agents', 'dir_ext:.md'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/agents/reviewer.agent.md": 'x\n'},
    ('skills', '<copilot-plugin>/skills', 'skill_dir'):
        {"plugins/demo/.github/plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/skills/deploy/SKILL.md": 'x\n'},
    ('hooks', '<copilot-plugin>/hooks.json|hooks/hooks.json', 'json_hooks'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/hooks/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ('mcp', '<copilot-plugin>/.mcp.json|.github/mcp.json', 'json_mcp_servers'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/.github/mcp.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ('rules', '<copilot-plugin>/rules', 'dir_ext_described:.mdc'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo"}\n',
         "plugins/demo/rules/style.mdc": '---\ndescription: Style\n---\nx\n'},
    ('commands', '<copilot-plugin>/commands', 'dir_ext:.md'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/commands/ship.md": 'x\n'},
    ('skills', '<claude-plugin>/SKILL.md', 'plugin_root_skill'):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/SKILL.md": 'x\n',
         "plugins/demo/references/config.md": 'x\n'},
    ('skills', '<copilot-plugin>/SKILL.md', 'plugin_root_skill'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo"}\n', "plugins/demo/SKILL.md": 'x\n'},
    ('skills', '<copilot-plugin>/skills', 'manifest_field:skills:paths'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "skills": {"paths": ["./extra"]}}\n',
         "plugins/demo/extra/deploy/SKILL.md": 'x\n'},
    ('subagents', '<copilot-plugin>/agents', 'manifest_field:agents:paths'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "agents": ["./team"]}\n',
         "plugins/demo/team/reviewer.md": 'x\n'},
    ('commands', '<copilot-plugin>/commands', 'manifest_field:commands:paths'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "commands": "./cmds"}\n',
         "plugins/demo/cmds/deploy.md": 'x\n'},
    ('rules', '<copilot-plugin>/rules', 'manifest_field:rules:paths'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "rules": "./.cursor/rules/"}\n',
         "plugins/demo/.cursor/rules/style.mdc": '---\ndescription: Style\n---\nx\n'},
    ('hooks', '<copilot-plugin>/hooks.json|hooks/hooks.json', 'manifest_field:hooks:precedes'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "hooks": "./cfg/hooks.json"}\n',
         "plugins/demo/cfg/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ('mcp', '<copilot-plugin>/.mcp.json|.github/mcp.json', 'manifest_field:mcpServers:follows'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "mcpServers": "./cfg/mcp.json"}\n',
         "plugins/demo/cfg/mcp.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ('lsp', '<copilot-plugin>/.lsp.json|lsp.json|.github/lsp.json', 'manifest_field:lspServers:follows'):
        {"plugins/demo/.plugin/plugin.json": '{"name": "demo", "lspServers": "./cfg/lsp.json"}\n',
         "plugins/demo/cfg/lsp.json": LSP_DOCUMENT},
    ('commands', '<gemini-extension>/commands', 'tree_ext:.toml'):
        {"ext/gemini-extension.json": '{"name": "e"}\n', "ext/commands/review.toml": 'prompt = "x"\n'},
    ('skills', '<gemini-extension>/skills', 'skill_dir_flat'):
        {"ext/gemini-extension.json": '{"name": "e"}\n', "ext/skills/deploy/SKILL.md": "x\n"},
    ('hooks', '<gemini-extension>/hooks/hooks.json', 'json_hooks'):
        {"ext/gemini-extension.json": '{"name": "e"}\n', "ext/hooks/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ('subagents', '<gemini-extension>/agents', 'dir_ext_described:.md'):
        {"ext/gemini-extension.json": '{"name": "e"}\n', "ext/agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews code.\n---\nReview.\n"},
    ('plugins', '<antigravity-plugin>/plugin.json', 'file'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo"}\n'},
    ('skills', '<antigravity-plugin>/skills', 'skill_dir_flat'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo"}\n',
         ".agents/plugins/demo/skills/deploy/SKILL.md": "x\n"},
    ('skills', '<antigravity-plugin>/skills', 'manifest_field:skills:adds'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo", "skills": "./extra"}\n',
         ".agents/plugins/demo/extra/deploy/SKILL.md": "x\n"},
    ('subagents', '<antigravity-plugin>/agents', 'dir_agent:.md'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo"}\n',
         ".agents/plugins/demo/agents/reviewer.md": "---\nname: reviewer\ndescription: d\n---\nx\n"},
    ('rules', '<antigravity-plugin>/rules', 'dir_ext_triggered:.md'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo"}\n',
         ".agents/plugins/demo/rules/style.md": "---\ntrigger: always_on\n---\nx\n"},
    ('mcp', '<antigravity-plugin>/mcp_config.json', 'json_mcp_servers'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo"}\n',
         ".agents/plugins/demo/mcp_config.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ('hooks', '<antigravity-plugin>/hooks.json', 'json_named_hooks'):
        {".agents/plugins/demo/plugin.json": '{"name": "demo"}\n',
         ".agents/plugins/demo/hooks.json": '{"lint": {"PostToolUse": []}}\n'},
    ('rules', '.agents/rules.json', 'file'):
        _x(".agents/rules.json"),
    ('plugins', '.github/plugin/marketplace.json', 'file'):
        _x(".github/plugin/marketplace.json"),
    ('plugins', '.github/plugin/plugin.json', 'file'):
        _x(".github/plugin/plugin.json"),
    ('plugins', '.plugin/plugin.json', 'file'):
        _x(".plugin/plugin.json"),
    ('plugins', '<copilot-plugin>/plugin.json', 'file'):
        {"plugins/demo/plugin.json": '{"name": "demo", "skills": "./skills/"}\n',
         "plugins/demo/skills/deploy/SKILL.md": "---\nname: deploy\ndescription: d\n---\n"},
    ('collect_only', '.plugin', 'copy_dir'):
        _x(".plugin/plugin.json", ".plugin/README.md"),
    ('other', '.plugin', 'unclassified_dir'):
        _x(".plugin/notes.txt"),
    ("mcp", "mcp.json", "basename_json_agent_plugins_mcp"): {"plugins/demo/mcp.json": MCP_DOCUMENT},
    ("skills", ".claude/workflows", "tree_ext:.js"): _x(".claude/workflows/release.js"),
    ("plugins", ".cursor-plugin/marketplace.json", "file"): _x(".cursor-plugin/marketplace.json"),
    ("lsp", ".github/lsp.json", "file"): {".github/lsp.json": LSP_DOCUMENT},
    ("lsp", "<copilot-plugin>/.lsp.json|lsp.json|.github/lsp.json", "file"):
        {"plugins/demo/.github/plugin/plugin.json": '{"name": "demo"}\n',
         "plugins/demo/lsp.json": LSP_DOCUMENT},
    ("commands", ".agent/workflows", "dir_ext:.md"): _x(".agent/workflows/deploy.md"),
    ("skills", "<claude-plugin>/skills", "manifest_field:skills:adds"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "skills": ["./extra/"]}\n',
         "plugins/demo/extra/deploy/SKILL.md": "x\n", "plugins/demo/extra/deploy/scripts/run.sh": "x\n"},
    ("skills", "<claude-plugin>/commands", "manifest_field:commands:replaces"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "commands": ["./cmds/status.md"]}\n',
         "plugins/demo/cmds/status.md": "x\n"},
    ("subagents", "<claude-plugin>/agents", "manifest_field:agents:files"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "agents": ["./team/reviewer.md"]}\n',
         "plugins/demo/team/reviewer.md": "x\n"},
    ("hooks", "<claude-plugin>/hooks/hooks.json", "manifest_field:hooks:merges"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "hooks": "./cfg/hooks.json"}\n',
         "plugins/demo/cfg/hooks.json": '{"hooks": {"Stop": []}}\n'},
    ("mcp", "<claude-plugin>/.mcp.json", "manifest_field:mcpServers:merges"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "mcpServers": "./cfg/mcp.json"}\n',
         "plugins/demo/cfg/mcp.json": '{"mcpServers": {"x": {"command": "y"}}}\n'},
    ("lsp", "<claude-plugin>/.lsp.json", "manifest_field:lspServers:merges"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "lspServers": "./cfg/lsp.json"}\n',
         "plugins/demo/cfg/lsp.json": LSP_DOCUMENT},
    ("rules", "<claude-plugin>/output-styles", "manifest_field:outputStyles:replaces"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "outputStyles": "./styles/"}\n',
         "plugins/demo/styles/terse.md": "x\n"},
    ("skills", "<claude-plugin>/workflows", "manifest_field:workflows:replaces"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo", "workflows": ["./flows/audit.js"]}\n',
         "plugins/demo/flows/audit.js": "x\n"},
    ("lsp", "<agent-plugin>/com.github.copilot/lsp.json", "file"):
        {"plugins/demo/plugin.json": MANIFEST,
         "plugins/demo/com.github.copilot/lsp.json": LSP_DOCUMENT},
    ("lsp", "<claude-plugin>/.lsp.json", "file"):
        {"plugins/demo/.claude-plugin/plugin.json": '{"name": "demo"}\n',
         "plugins/demo/.lsp.json": LSP_DOCUMENT},
}


ROW_CASES[("subagents", ".github/agents", "tree_ext_described:.md")] = {
    ".github/agents/security/review.md": "---\ndescription: Review\n---\nInstructions\n"}
for _marketplace in ("marketplace.json", ".plugin/marketplace.json", ".github/plugin/marketplace.json"):
    del ROW_CASES[("plugins", _marketplace, "file")]
    ROW_CASES[("plugins", _marketplace, "json_copilot_marketplace")] = {
        _marketplace: '{"name":"example","owner":{"name":"Owner"},"plugins":[]}'}

ROW_CASES.update({
    ('settings', '.cursor/settings.json', 'file'): {'.cursor/settings.json':'{"plugins":{}}'},
    ('subagents', '<cursor-plugin>/agents', 'tree_ext:.md|.mdc|.markdown'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p"}', 'p/agents/reviewer.mdc':'Review.'},
    ('commands', '<cursor-plugin>/commands', 'tree_ext:.md|.mdc|.markdown|.txt'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p"}', 'p/commands/check.txt':'Check.'},
    ('hooks', '<cursor-plugin>/hooks/hooks.json', 'json_hooks'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p"}', 'p/hooks/hooks.json':'{"hooks":{"stop":[]}}'},
    ('skills', '<cursor-plugin>/SKILL.md', 'plugin_root_skill'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p"}', 'p/SKILL.md':'Skill.'},
    ('mcp', '<cursor-plugin>/.mcp.json', 'json_mcp_servers'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p"}', 'p/.mcp.json':'{"mcpServers":{"x":{"command":"true"}}}'},
    ('skills', '<cursor-plugin>/skills', 'manifest_field:skills:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","skills":"./custom"}', 'p/custom/s/SKILL.md':'Skill.'},
    ('rules', '<cursor-plugin>/rules', 'manifest_field:rules:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","rules":"./custom"}', 'p/custom/style.markdown':'Style.'},
    ('subagents', '<cursor-plugin>/agents', 'manifest_field:agents:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","agents":"./custom"}', 'p/custom/reviewer.mdc':'Review.'},
    ('commands', '<cursor-plugin>/commands', 'manifest_field:commands:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","commands":"./custom"}', 'p/custom/check.txt':'Check.'},
    ('hooks', '<cursor-plugin>/hooks/hooks.json', 'manifest_field:hooks:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","hooks":"./custom.json"}', 'p/custom.json':'{"hooks":{"stop":[]}}'},
    ('mcp', '<cursor-plugin>/mcp.json', 'manifest_field:mcpServers:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","mcpServers":"./custom.json"}', 'p/custom.json':'{"mcpServers":{"x":{"command":"true"}}}'},
    ('mcp', '<cursor-plugin>/.mcp.json', 'manifest_field:mcpServers:replaces'):
        {'p/.cursor-plugin/plugin.json':'{"name":"p","mcpServers":"./custom.json"}', 'p/custom.json':'{"mcpServers":{"x":{"command":"true"}}}'},
})


ROW_CASES.update({
    ("skills", "<codex-plugin>/skills", "manifest_field:skills:replaces"):
        {"p/.codex-plugin/plugin.json": '{"name":"p","skills":"./custom"}',
         "p/custom/review/SKILL.md": "Review"},
    ("mcp", "<codex-plugin>/.mcp.json", "manifest_field:mcpServers:replaces"):
        {"p/.codex-plugin/plugin.json": '{"name":"p","mcpServers":"./custom.json"}',
         "p/custom.json": '{"mcpServers":{"x":{"command":"x"}}}'},
    ("hooks", "<codex-plugin>/hooks/hooks.json", "manifest_field:hooks:replaces"):
        {"p/.codex-plugin/plugin.json": '{"name":"p","hooks":"./custom.json"}',
         "p/custom.json": '{"hooks":{"Stop":[]}}'},
    ("hooks", "<agent-plugin>/hooks/hooks.json", "manifest_field:hooks:replaces"):
        {"p/plugin.json": MANIFEST[:-1] + ', "extensions":{"com.openai":{"hooks":"./custom.json"}}}',
         "p/custom.json": '{"hooks":{"Stop":[]}}'},
    ("settings", ".github/github-app.yml", "file"): _x(".github/github-app.yml"),
    ("settings", ".github/copilot-desktop.yml", "file"): _x(".github/copilot-desktop.yml"),
    ("settings", ".gemini/config.json", "root_file"): _x(".gemini/config.json"),
    ("other", ".worktreeinclude", "root_file"): _x(".worktreeinclude"),
})


ROW_CASES.update({
    ("settings", ".cursor/config/bugbot.yaml", "root_file"): _x(".cursor/config/bugbot.yaml"),
    ("settings", ".github/workflows", "workflow_engine:agy"):
        {".github/workflows/docs.md": "---\non: push\nengine: agy\n---\nDocs.\n"},
    ("settings", ".github/workflows", "workflow_command:irm https://cursor.com/install?win32=true"):
        _run("irm 'https://cursor.com/install?win32=true' | iex"),
    ("hooks", ".github/copilot/settings.json", "json_hooks"):
        {".github/copilot/settings.json": '{"hooks": {"sessionStart": []}}\n'},
    ("hooks", ".github/copilot/settings.local.json", "json_hooks"):
        {".github/copilot/settings.local.json": '{"hooks": {"sessionStart": []}}\n'},
    ("other", ".antigravityignore", "basename"): _x(".antigravityignore"),
    ("subagents", "<antigravity-plugin>/agents.json", "registered:subagents"):
        {".agents/plugins/p/plugin.json": '{"name":"p"}',
         ".agents/plugins/p/agents.json": '{"entries":[{"path":"shared/agents"}]}',
         "shared/agents/check.md": "---\nname: check\ndescription: Check\n---\nCheck instructions"},
    ("skills", "<antigravity-plugin>/skills.json", "registered:skills"):
        {".agents/plugins/p/plugin.json": '{"name":"p"}',
         ".agents/plugins/p/skills.json": '{"entries":[{"path":"shared/skills"}]}',
         "shared/skills/check/SKILL.md": "Check instructions"},
    ("plugins", "<antigravity-plugin>/.antigravity-plugin/plugin.json", "file"):
        _x(".agents/plugins/p/.antigravity-plugin/plugin.json"),
    ("plugins", "<antigravity-plugin>/.gemini-plugin/plugin.json", "file"):
        _x(".agents/plugins/p/.gemini-plugin/plugin.json"),
})


# Antigravity's alternate roots accept the same files as .agents.
for alias in (".agent", "_agents", "_agent"):
    for (mechanism, path, mode), files in list(ROW_CASES.items()):
        if (path == ".agents" or path.startswith(".agents/")) and path != ".agents/plugins/marketplace.json":
            key = (mechanism, alias + path[len(".agents"):], mode)
            ROW_CASES.setdefault(key, {name.replace(".agents", alias): content.replace(".agents", alias)
                                       for name, content in files.items()})
# `_agents` and `_agent` are customization roots only with a place of Antigravity in them, and
# Antigravity reads their skills one level deep, where Codex reads `.agents/skills` at any depth.
for alias in ("_agents", "_agent"):
    del ROW_CASES[("other", alias, "spec_dir")]
    ROW_CASES[("other", alias, "spec_dir_with_place")] = _x(f"{alias}/notes.md",
                                                            f"{alias}/skills/lint/SKILL.md")
for alias in (".agent", "_agents", "_agent"):
    ROW_CASES.setdefault(("skills", f"{alias}/skills", "skill_dir_flat"),
                         ROW_CASES.pop(("skills", f"{alias}/skills", "skill_dir")))


# OpenCode's rows: its configuration and the settings of its terminal interface at the root, its
# directory with the files of each folder, and the workflows that run it.
OPENCODE_CONFIG = '{"mcp": {"docs": {"type": "remote", "url": "https://x.test"}}, "lsp": true}\n'
OPENCODE_AGENT = '{"agent": {"reviewer": {"mode": "subagent", "prompt": "Review."}}}\n'
OPENCODE_AGENTS = '{"agents": {"reviewer": {"mode": "subagent", "prompt": "Review."}}}\n'
OPENCODE_MODE = '{"mode": {"reviewer": {"prompt": "Review."}}}\n'
OPENCODE_COMMANDS = '{"commands": {"test": {"template": "Run the tests."}}}\n'
OPENCODE_COMMAND = '{"command": {"test": {"template": "Run the tests."}}}\n'
OPENCODE_SKILL = "---\nname: release\ndescription: Publish a release.\n---\nPublish.\n"
ROW_CASES.update({
    ("settings", "opencode.json", "root_file"): _x("opencode.json"),
    ("settings", "opencode.jsonc", "root_file"): _x("opencode.jsonc"),
    ("settings", ".opencode/opencode.json", "file"): _x(".opencode/opencode.json"),
    ("settings", ".opencode/opencode.jsonc", "file"): _x(".opencode/opencode.jsonc"),
    ("settings", "tui.json", "root_file"): _x("tui.json"),
    ("settings", "tui.jsonc", "root_file"): _x("tui.jsonc"),
    ("settings", ".opencode/tui.json", "file"): _x(".opencode/tui.json"),
    ("settings", ".opencode/tui.jsonc", "file"): _x(".opencode/tui.jsonc"),
    ("other", ".opencode", "spec_dir"): _x(".opencode/notes.txt"),
    ("subagents", ".opencode/agents", "tree_ext:.md"): _x(".opencode/agents/review/security.md"),
    ("subagents", ".opencode/agent", "tree_ext:.md"): _x(".opencode/agent/review.md"),
    ("subagents", ".opencode/modes", "dir_ext:.md"): _x(".opencode/modes/plan.md"),
    ("subagents", ".opencode/mode", "dir_ext:.md"): _x(".opencode/mode/plan.md"),
    ("commands", ".opencode/commands", "tree_ext:.md"): _x(".opencode/commands/git/commit.md"),
    ("commands", ".opencode/command", "tree_ext:.md"): _x(".opencode/command/test.md"),
    ("skills", ".opencode/skills", "skill_dir"): _x(".opencode/skills/team/release/SKILL.md"),
    ("skills", ".opencode/skill", "skill_dir"): _x(".opencode/skill/release/SKILL.md"),
    ("skills", ".opencode/skills", "dir_ext_described:.md"):
        {".opencode/skills/release.md": OPENCODE_SKILL},
    ("skills", ".opencode/skill", "dir_ext_described:.md"):
        {".opencode/skill/release.md": OPENCODE_SKILL},
    ("plugins", ".opencode/plugins", "dir_ext:.ts|.js"): _x(".opencode/plugins/notify.ts"),
    ("plugins", ".opencode/plugin", "dir_ext:.ts|.js"): _x(".opencode/plugin/notify.js"),
    ("plugins", ".opencode/plugins", "package_entry:server|index|tui:.tsx|.jsx|.mts|.ts|.mjs|.js|.cts|.cjs"):
        _x(".opencode/plugins/acme/index.ts", ".opencode/plugins/acme/src/hooks.ts",
           ".opencode/plugins/acme/package.json"),
    ("plugins", ".opencode/plugin", "package_entry:server|index:.tsx|.jsx|.mts|.ts|.mjs|.js|.cts|.cjs"):
        _x(".opencode/plugin/acme/server.js"),
    ("plugins", ".opencode/tools", "dir_ext:.ts|.js"): _x(".opencode/tools/database.ts"),
    ("plugins", ".opencode/tool", "dir_ext:.ts|.js"): _x(".opencode/tool/database.js"),
    ("settings", ".opencode/themes", "dir_ext:.json"): _x(".opencode/themes/night.json"),
    ("mcp", "opencode.json", "json_servers:mcp"): {"opencode.json": OPENCODE_CONFIG},
    ("mcp", "opencode.jsonc", "json_servers:mcp"): {"opencode.jsonc": OPENCODE_CONFIG},
    ("mcp", ".opencode/opencode.json", "json_servers:mcp"):
        {".opencode/opencode.json": OPENCODE_CONFIG},
    ("mcp", ".opencode/opencode.jsonc", "json_servers:mcp"):
        {".opencode/opencode.jsonc": OPENCODE_CONFIG},
    ("subagents", "opencode.json", "json_servers:agent"): {"opencode.json": OPENCODE_AGENT},
    ("subagents", "opencode.jsonc", "json_servers:agent"): {"opencode.jsonc": OPENCODE_AGENT},
    ("subagents", ".opencode/opencode.json", "json_servers:agent"):
        {".opencode/opencode.json": OPENCODE_AGENT},
    ("subagents", ".opencode/opencode.jsonc", "json_servers:agent"):
        {".opencode/opencode.jsonc": OPENCODE_AGENT},
    ("subagents", "opencode.json", "json_servers:agents"): {"opencode.json": OPENCODE_AGENTS},
    ("subagents", "opencode.jsonc", "json_servers:agents"): {"opencode.jsonc": OPENCODE_AGENTS},
    ("subagents", ".opencode/opencode.json", "json_servers:agents"):
        {".opencode/opencode.json": OPENCODE_AGENTS},
    ("subagents", ".opencode/opencode.jsonc", "json_servers:agents"):
        {".opencode/opencode.jsonc": OPENCODE_AGENTS},
    ("subagents", "opencode.json", "json_servers:mode"): {"opencode.json": OPENCODE_MODE},
    ("subagents", "opencode.jsonc", "json_servers:mode"): {"opencode.jsonc": OPENCODE_MODE},
    ("subagents", ".opencode/opencode.json", "json_servers:mode"):
        {".opencode/opencode.json": OPENCODE_MODE},
    ("subagents", ".opencode/opencode.jsonc", "json_servers:mode"):
        {".opencode/opencode.jsonc": OPENCODE_MODE},
    ("commands", "opencode.json", "json_servers:commands"): {"opencode.json": OPENCODE_COMMANDS},
    ("commands", "opencode.jsonc", "json_servers:commands"): {"opencode.jsonc": OPENCODE_COMMANDS},
    ("commands", ".opencode/opencode.json", "json_servers:commands"):
        {".opencode/opencode.json": OPENCODE_COMMANDS},
    ("commands", ".opencode/opencode.jsonc", "json_servers:commands"):
        {".opencode/opencode.jsonc": OPENCODE_COMMANDS},
    ("commands", "opencode.json", "json_servers:command"): {"opencode.json": OPENCODE_COMMAND},
    ("commands", "opencode.jsonc", "json_servers:command"): {"opencode.jsonc": OPENCODE_COMMAND},
    ("commands", ".opencode/opencode.json", "json_servers:command"):
        {".opencode/opencode.json": OPENCODE_COMMAND},
    ("commands", ".opencode/opencode.jsonc", "json_servers:command"):
        {".opencode/opencode.jsonc": OPENCODE_COMMAND},
    ("lsp", "opencode.json", "json_enabled:lsp"): {"opencode.json": OPENCODE_CONFIG},
    ("lsp", "opencode.jsonc", "json_enabled:lsp"): {"opencode.jsonc": OPENCODE_CONFIG},
    ("lsp", ".opencode/opencode.json", "json_enabled:lsp"):
        {".opencode/opencode.json": OPENCODE_CONFIG},
    ("lsp", ".opencode/opencode.jsonc", "json_enabled:lsp"):
        {".opencode/opencode.jsonc": OPENCODE_CONFIG},
    ("context_file", "opencode.json", "declared_globs:instructions"):
        {"opencode.json": '{"instructions": ["docs/standards/*.md"]}\n',
         "docs/standards/style.md": "x\n"},
    ("context_file", "opencode.jsonc", "declared_globs:instructions"):
        {"opencode.jsonc": '{"instructions": ["CONTRIBUTING.md",],}\n', "CONTRIBUTING.md": "x\n"},
    ("context_file", ".opencode/opencode.json", "declared_globs:instructions"):
        {".opencode/opencode.json": '{"instructions": ["docs/guide.md"]}\n',
         "docs/guide.md": "x\n"},
    ("context_file", ".opencode/opencode.jsonc", "declared_globs:instructions"):
        {".opencode/opencode.jsonc": '// rules\n{"instructions": [".cursor/rules/*.md"]}\n',
         ".cursor/rules/style.md": "x\n"},
    ("settings", ".github/workflows/opencode.yml", "file"): _x(".github/workflows/opencode.yml"),
    ("settings", ".github/workflows", "workflow_uses:anomalyco/opencode/github"):
        _uses("anomalyco/opencode/github"),
    ("settings", ".github/workflows", "workflow_uses:sst/opencode/github"):
        _uses("sst/opencode/github"),
    ("settings", ".github/workflows", "workflow_engine:opencode"):
        {".github/workflows/triage.md": "---\non: issues\nengine:\n  id: opencode\nimports:\n"
                                        "  - github/gh-aw/.github/workflows/shared/opencode.md@v1\n"
                                        "---\nTriage.\n"},
    ("settings", ".github/workflows", "workflow_command:opencode run||opencode-ai run"):
        _run('opencode run "Review this change"'),
})


# The places of a package's `.github` and its `.mcp.json`, which Copilot CLI reads where it
# works, in a repository with no `.github` at the root, and the app files of a Codex plugin.
ROW_CASES.update({
    ("context_file", ".github/copilot-instructions.md", "nested:file"):
        _x("packages/web/.github/copilot-instructions.md"),
    ("context_file", ".github/instructions", "nested:tree_suffix:.instructions.md"):
        _x("packages/web/.github/instructions/frontend/react.instructions.md"),
    ("subagents", ".github/agents", "nested:tree_ext_described:.md"):
        {"packages/web/.github/agents/review.agent.md": "---\ndescription: Review\n---\nReview.\n"},
    ("skills", ".github/skills", "nested:skill_dir"):
        _x("packages/web/.github/skills/release/SKILL.md"),
    ("lsp", ".github/lsp.json", "nested:file"): {"packages/web/.github/lsp.json": LSP_DOCUMENT},
    ("mcp", ".github/mcp.json", "nested:file"):
        {"packages/web/.github/mcp.json": '{"mcpServers": {"gh": {"command": "gh"}}}\n'},
    ("mcp", ".mcp.json", "nested:file"):
        {"packages/web/.mcp.json": '{"mcpServers": {"web": {"command": "web"}}}\n'},
    ("mcp", "<codex-plugin>/.app.json", "json_servers:apps"):
        {"p/.codex-plugin/plugin.json": '{"name":"p"}',
         "p/.app.json": '{"apps":{"forms":{"id":"plugin_asdk_app_1"}}}'},
    ("mcp", "<codex-plugin>/.app.json", "manifest_field:apps:replaces"):
        {"p/.codex-plugin/plugin.json": '{"name":"p","apps":"./config/apps.json"}',
         "p/config/apps.json": '{"apps":{"forms":{"id":"plugin_asdk_app_1"}}}'},
})


class TestEveryHeuristicPathIsCollected(unittest.TestCase):
    """Every row of config_path_patterns.csv, as a file that a full collector pass copies, so that
    no row, such as `.gemini/config.yaml` or a workflow file matched by name alone, can be
    deleted without a suite that runs a step failing. A row added without a case fails here,
    and so does a case left for a removed row.
    """

    def test_every_row_has_a_case(self):
        rows = {(r["mechanism"], r["path"], r["match_mode"])
                for r in load_config_path_patterns()}
        self.assertEqual(sorted(rows), sorted(ROW_CASES))

    def test_each_case_is_collected(self):
        for key, files in ROW_CASES.items():
            with self.subTest(row=key):
                tmp = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                repo, out = tmp / "owner§repo", tmp / "out"
                for rel, text in files.items():
                    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                    (repo / rel).write_text(text, encoding="utf-8")
                collect_repo(repo, out, set())
                for rel, text in files.items():
                    copied = out / rel
                    self.assertTrue(copied.is_file(), f"{rel} was not collected")
                    self.assertEqual(copied.read_text(encoding="utf-8"), text)


class TestTheLocalStateOfAToolIsNotCollected(unittest.TestCase):
    """A clone whose tool directories hold the configuration and the files that the tools keep
    for themselves: the collection copies the configuration and none of the session logs,
    histories, locks, logins and worktrees (heuristics/local_state_paths.csv)."""

    CONFIGURATION = {
        "CLAUDE.md": "# Claude\n",
        ".claude/settings.json": "{}\n",
        ".claude/skills/pdf/SKILL.md": "---\nname: pdf\ndescription: d\n---\n",
        # A project's own folder of projects, as apache/superset keeps one, and a file
        # directly in tasks/, which no tool writes there.
        ".claude/projects/js-to-ts/AGENT.md": "# notes\n",
        ".claude/tasks/build-app.md": "# task\n",
        ".codex/config.toml": "model = \"o3\"\n",
        ".gemini/settings.json": "{}\n",
        ".cursor/rules/a.mdc": "---\ndescription: d\n---\n",
    }
    LOCAL_STATE = {
        ".claude/projects/-Users-a-repo/0a1b2c3d.jsonl": "{}\n",
        ".claude/projects/-Users-a-repo/memory/MEMORY.md": "# memory\n",
        ".claude/projects/C--Users-a-repo/memory/MEMORY.md": "# memory\n",
        ".claude/history.jsonl": "{}\n",
        ".claude/scheduled_tasks.lock": "{\"pid\": 1}\n",
        ".claude/tasks/list-1/1.json": "{}\n",
        ".claude/worktrees/w/CLAUDE.md": "# a checkout\n",
        ".claude/worktrees/w/.claude/settings.local.json": "{}\n",
        ".claude/worktrees/w/.claude/plans/plan.md": "# Plan\n",
        ".claude/worktrees/w/.agents/rules.json": '{"entries": [{"path": "shared-rules"}]}\n',
        ".claude/worktrees/w/shared-rules/style.md": "---\ntrigger: always_on\n---\nStyle.\n",
        ".claude/plugins/cache/m/p/1.0.0/.claude-plugin/plugin.json": "{\"name\": \"p\"}\n",
        ".claude/plugins/cache/m/p/1.0.0/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n",
        ".agents/scheduled_tasks.lock": "{\"pid\": 1}\n",
        ".codex/sessions/2026/10/01/rollout-2026-10-01T00-00-00-x.jsonl": "{}\n",
        ".codex/auth.json": "{}\n",
        ".codex/state_5.sqlite": "SQLite format 3\n",
        ".gemini/tmp/h/chats/session.json": "{}\n",
        ".cursor/projects/x/agent-transcripts/a.jsonl": "{}\n",
        ".cursor/debug-abc123.log": "log\n",
        ".opencode/opencode.db": "SQLite format 3\n",
        "pkg/.claude/projects/-Users-a-pkg/s.jsonl": "{}\n",
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _clone(self, at: Path) -> Path:
        """A clone at `at` of a repository with the configuration and the local state."""
        remote = self.tmp / "remote"
        _init_git_repo(remote)
        for rel, text in {**self.CONFIGURATION, **self.LOCAL_STATE}.items():
            (remote / rel).parent.mkdir(parents=True, exist_ok=True)
            (remote / rel).write_text(text, encoding="utf-8")
        os.symlink("../../settings.json", remote / ".claude/projects/-Users-a-repo/link.json")
        # A file of a place that is a link out of its tool directory, of which rsync copies
        # nothing and the copy would otherwise write a marker.
        (remote / "shared").mkdir()
        (remote / "shared" / "prompt_history.json").write_text("{}\n", encoding="utf-8")
        os.symlink("../shared/prompt_history.json", remote / ".cursor/prompt_history.json")
        subprocess.run(["git", "-C", str(remote), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(remote), "commit", "-m", "state"],
                       capture_output=True, check=True)
        at.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(at)], capture_output=True,
                       check=True)
        return at

    def _collected(self, clone: Path) -> set:
        out = self.tmp / "out"
        collect_repo(clone, out, set())
        return {p.relative_to(out).as_posix() for p in out.rglob("*")
                if p.is_file() or p.is_symlink()}

    def test_the_configuration_is_copied_and_the_local_state_is_not(self):
        collected = self._collected(self._clone(self.tmp / "clones" / "owner§repo"))
        self.assertEqual(collected, set(self.CONFIGURATION))

    def test_a_clone_below_a_directory_with_the_name_of_a_place_loses_nothing(self):
        """A place is matched below the directory that a listing starts at, and a checkout in a
        worktree of a tool, such as a data root under `.claude/worktrees/`, collects
        as any other."""
        clone = self._clone(self.tmp / "home" / ".claude" / "worktrees" / "w" / "owner§repo")
        self.assertEqual(self._collected(clone), set(self.CONFIGURATION))

    def test_the_matching_of_a_place(self):
        cases = {
            ".claude/projects/-Users-a-repo/memory/MEMORY.md": ".claude/projects/-*/",
            "pkg/.claude/projects/C--Users-a/x.jsonl": ".claude/projects/?--*/",
            ".claude/projects/js-to-ts/AGENT.md": None,
            ".claude/history.jsonl": ".claude/history.jsonl",
            "history.jsonl": None,
            ".claude/notes/history.jsonl": None,
            ".claude/tasks/list-1/1.json": ".claude/tasks/*/",
            ".claude/tasks/build-app.md": None,
            ".codex/logs_2.sqlite": ".codex/*.sqlite",
            ".codex/db/logs_2.sqlite": None,
            ".claude/settings.local.json": None,
            ".gemini/antigravity-cli/brain/c1/transcript.jsonl": ".gemini/antigravity*/brain/",
        }
        for path, place in cases.items():
            with self.subTest(path):
                self.assertEqual(local_state(path), place)
        self.assertEqual(local_state(".claude/worktrees", is_dir=True), ".claude/worktrees/")
        self.assertIsNone(local_state(".claude/worktrees"))
        self.assertIsNone(local_state(".claude/history.jsonl", is_dir=True))


class TestACodexPluginWithAnAgentPluginsManifest(unittest.TestCase):
    """Codex reads a plugin through a root `plugin.json` that declares the Agent Plugins schema
    before `.codex-plugin/plugin.json`, takes `./skills` and `./mcp.json`, and lets the
    `.codex-plugin` manifest set only apps, hooks and the interface (openai/codex,
    plugin_namespace.rs and agent_plugin_manifest.rs, read 2026-10-13). A
    `.codex-plugin/mcp.json` that the manifest names beside an Agent Plugins one, as
    evotecit/officeimo's does, is therefore not Codex's MCP, and the portable `skills/` and
    `mcp.json` are Codex's parts."""

    SKILL = "---\nname: op\ndescription: d\n---\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _codex_parts(self) -> list:
        return sorted((rel, rule["mechanism"]) for rel, rule, _kind in paths.plugin_components(self.repo)
                      if rule["tool"] == "codex")

    def test_the_places_that_the_codex_manifest_names_beside_an_agent_plugins_one_stay_unread(self):
        codex = json.dumps({"name": "tools", "skills": "./legacy-skills/",
                            "mcpServers": "./.codex-plugin/mcp.json"})
        self._tree({"plugins/tools/plugin.json": MANIFEST,
                    "plugins/tools/.codex-plugin/plugin.json": codex,
                    "plugins/tools/.codex-plugin/mcp.json": MCP_DOCUMENT,
                    "plugins/tools/.mcp.json": '{"mcpServers": {"x": {"command": "x"}}}',
                    "plugins/tools/legacy-skills/old/SKILL.md": self.SKILL,
                    "plugins/tools/skills/op/SKILL.md": self.SKILL})
        read = {rel for rel, _mechanism in self._codex_parts()}
        for unread in ("plugins/tools/.codex-plugin/mcp.json", "plugins/tools/.mcp.json",
                       "plugins/tools/legacy-skills", "plugins/tools/legacy-skills/old"):
            with self.subTest(place=unread):
                self.assertNotIn(unread, read)

    def test_the_portable_skills_and_mcp_file_beside_the_codex_manifest_are_codex_parts(self):
        """saadeghi/daisyui keeps both manifests at its root and EvotecIT/OfficeIMO in
        `.agents/plugins/officeimo-document-tools`, the first without and the second with
        `extensions.com.openai` in its portable manifest."""
        extension = json.dumps({**json.loads(MANIFEST), "extensions": {"com.openai": {}}})
        for name, manifest in (("without the extension", MANIFEST),
                               ("with the extension", extension)):
            with self.subTest(manifest=name):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({"plugins/tools/plugin.json": manifest,
                            "plugins/tools/.codex-plugin/plugin.json": '{"name": "tools"}',
                            "plugins/tools/mcp.json": MCP_DOCUMENT,
                            "plugins/tools/skills/op/SKILL.md": self.SKILL})
                self.assertEqual(self._codex_parts(), [("plugins/tools/mcp.json", "mcp"),
                                                       ("plugins/tools/skills", "skills")])

    def test_without_the_codex_manifest_the_portable_parts_are_no_codex_parts(self):
        self._tree({"plugins/tools/plugin.json": MANIFEST,
                    "plugins/tools/mcp.json": MCP_DOCUMENT,
                    "plugins/tools/skills/op/SKILL.md": self.SKILL})
        self.assertEqual(self._codex_parts(), [])

    def test_without_an_agent_plugins_manifest_the_codex_manifest_names_its_parts(self):
        codex = json.dumps({"name": "tools", "mcpServers": "./.codex-plugin/mcp.json"})
        self._tree({"plugins/tools/.codex-plugin/plugin.json": codex,
                    "plugins/tools/.codex-plugin/mcp.json": MCP_DOCUMENT})
        self.assertIn(("plugins/tools/.codex-plugin/mcp.json", "mcp"), self._codex_parts())

    def test_the_apps_file_of_a_plugin_in_the_compatibility_layout_is_read(self):
        """Codex reads `.app.json` at the root of a plugin with a legacy manifest, and the file
        that the manifest's `apps` names in its place (`plugin_app_config_paths` in
        codex-rs/core-plugins/src/loader.rs)."""
        apps = '{"apps": {"forms": {"id": "plugin_asdk_app_1"}}}'
        self._tree({"plugins/a/.codex-plugin/plugin.json": '{"name": "a"}',
                    "plugins/a/.app.json": apps,
                    "plugins/b/.codex-plugin/plugin.json": '{"name": "b", "apps": "./cfg/apps.json"}',
                    "plugins/b/.app.json": apps,
                    "plugins/b/cfg/apps.json": apps})
        self.assertEqual([part for part in self._codex_parts() if part[1] == "mcp"],
                         [("plugins/a/.app.json", "mcp"), ("plugins/b/cfg/apps.json", "mcp")])

    def test_no_apps_file_is_read_beside_an_agent_plugins_manifest(self):
        """Codex keeps the apps of a portable plugin inactive, "format ==
        PluginManifestFormat::Legacy" in plugin_supports_apps, as in jhumanj/opnform's
        plugins/opnform, with a .codex-plugin/plugin.json that names ./.app.json."""
        self._tree({"plugins/opnform/plugin.json": MANIFEST,
                    "plugins/opnform/.codex-plugin/plugin.json":
                        '{"name": "opnform", "apps": "./.app.json"}',
                    "plugins/opnform/.app.json": '{"apps": {"forms": {"id": "plugin_asdk_app_1"}}}'})
        self.assertEqual(self._codex_parts(), [])


class TestACopilotPluginWithAnAgentPluginsManifest(unittest.TestCase):
    """Copilot CLI reads a plugin with a root `plugin.json` that declares the Agent Plugins schema
    through that manifest: "A root plugin.json that targets Agent Plugins takes precedence over
    .plugin/plugin.json and .claude-plugin/plugin.json per spec §5.1" (CLI plugin reference, read
    2026-10-21). Copilot CLI 1.0.101, run with --plugin-dir on such plugins on 2026-10-21, read
    skills/ and com.github.copilot/commands/ and not commands/ or the skills that either legacy
    manifest names. Beside a legacy manifest, which makes the plugin Copilot's, the portable
    skills/ is therefore a part of Copilot as well as of no specific tool."""

    LEGACY_PARTS = {"agents/legacy.agent.md": "---\ndescription: d\n---\n",
                    "commands/legacy.md": "x\n",
                    "hooks.json": '{"hooks": {"sessionStart": [{"type": "command", "bash": "x"}]}}',
                    ".mcp.json": '{"mcpServers": {"x": {"command": "x"}}}',
                    "lsp.json": LSP_DOCUMENT,
                    "legacy-skills/l/SKILL.md": "---\nname: l\ndescription: d\n---\n",
                    "skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n",
                    "com.github.copilot/commands/ap.md": "x\n"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _plugin(self, files: dict) -> None:
        for rel, text in {**self.LEGACY_PARTS, **files}.items():
            (self.repo / "p" / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / "p" / rel).write_text(text, encoding="utf-8")

    def _parts(self, tool: str) -> list:
        return sorted(rel for rel, rule, _kind in paths.plugin_components(self.repo)
                      if rule["tool"] == tool)

    def test_no_legacy_place_is_read_beside_an_agent_plugins_manifest(self):
        for legacy in (".plugin/plugin.json", ".github/plugin/plugin.json"):
            with self.subTest(manifest=legacy):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._plugin({"plugin.json": MANIFEST,
                              legacy: '{"name": "demo", "skills": "./legacy-skills"}'})
                self.assertEqual(self._parts("copilot"),
                                 ["p/com.github.copilot/commands", "p/skills"])
                self.assertEqual(self._parts("any"), ["p/skills"])

    def test_the_legacy_manifest_names_the_places_without_one(self):
        self._plugin({".plugin/plugin.json": '{"name": "demo", "skills": "./legacy-skills"}'})
        self.assertIn("p/legacy-skills/l", self._parts("copilot"))
        self.assertIn("p/commands", self._parts("copilot"))
        self.assertNotIn("p/skills", self._parts("copilot") + self._parts("any"))

    def test_a_root_plugin_json_without_the_schema_comes_before_the_github_manifest(self):
        """The legacy manifests in the CLI's order: ".plugin/plugin.json, plugin.json,
        .github/plugin/plugin.json, or .claude-plugin/plugin.json (checked in this order)". The
        CLI read the skills/ of a plugin with a root plugin.json that sets no skills, and not the
        skills that its .github/plugin/plugin.json names."""
        self._plugin({"plugin.json": '{"name": "demo"}',
                      ".github/plugin/plugin.json": '{"name": "demo", "skills": "./legacy-skills"}'})
        self.assertIn("p/skills", self._parts("copilot"))
        self.assertNotIn("p/legacy-skills/l", self._parts("copilot"))


class _PluginTree(unittest.TestCase):
    """A repository written from `{path: text}` and the plugin parts of one tool."""

    SKILL = "---\nname: op\ndescription: d\n---\n"
    SERVERS = '{"mcpServers": {"x": {"command": "x"}}}'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text if isinstance(text, str) else json.dumps(text),
                                         encoding="utf-8")

    def _parts(self, tool: str) -> list:
        return sorted({rel for rel, rule, _kind in paths.plugin_components(self.repo)
                       if rule["tool"] == tool})


class TestACodexMarketplaceFromWhichCodexInstallsAPlugin(unittest.TestCase):
    """A Codex marketplace gives Codex a plugin to load only with an entry that
    `find_installable_marketplace_plugin` accepts, from a source that Codex resolves
    (codex-rs/core-plugins/src/marketplace.rs of openai/codex c9b4267c370a)."""

    def _doc(self, *entries) -> dict:
        return {"name": "local", "plugins": list(entries)}

    def test_an_entry_that_codex_installs(self):
        for source in ("./plugins/p", {"source": "local", "path": "./plugins/p"},
                       {"source": "url", "url": "https://github.com/o/r.git", "ref": None},
                       {"source": "git-subdir", "url": "https://github.com/o/r.git",
                        "path": "plugins/p"},
                       {"source": "npm", "package": "@o/p", "version": "1.0.0"}):
            with self.subTest(source=source):
                self.assertTrue(paths.codex_marketplace_installs(
                    self._doc({"name": "p", "source": source}), ""))

    def test_a_marketplace_from_which_codex_installs_no_plugin(self):
        """UMEP-dev/SUEWS lists its only plugin with `"installation": "NOT_AVAILABLE"` (pp-0374
        of the evaluation of 2026-11-04)."""
        suews = self._doc({"name": "suews",
                           "source": {"source": "local", "path": "./plugins/suews"},
                           "policy": {"installation": "NOT_AVAILABLE",
                                      "authentication": "ON_INSTALL"},
                           "category": "Productivity"})
        for name, doc in (
                ("an entry that is not available", suews),
                ("no entry", self._doc()),
                ("a source that Codex does not support",
                 self._doc({"name": "p", "source": {"source": "github", "repo": "o/r"}})),
                ("a remote source without its url", self._doc({"name": "p",
                                                               "source": {"source": "url"}})),
                ("a local path that Codex refuses", self._doc({"name": "p",
                                                               "source": "plugins/p"})),
                ("a plugin name with a slash", self._doc({"name": "o/p", "source": "./p"})),
                ("a category that is no string", self._doc({"name": "p", "source": "./p",
                                                            "category": 1}))):
            with self.subTest(marketplace=name):
                self.assertFalse(paths.codex_marketplace_installs(doc, ""))

    def test_one_entry_that_codex_installs_is_enough(self):
        doc = self._doc({"name": "a", "source": "./a", "policy": {"installation": "NOT_AVAILABLE"}},
                        {"name": "b", "source": "./b", "policy": {"installation": "AVAILABLE"}})
        self.assertTrue(paths.codex_marketplace_installs(doc, ""))


class TestTheLocalSourcesOfACodexMarketplace(_PluginTree):
    """A folder that a Codex marketplace lists as a local source is a Codex plugin: "Point each
    plugin entry’s source.path at the plugin directory you want the local host to load" and
    "Keep source.path relative to the marketplace root, start it with ./, and keep it inside that
    root" (https://developers.openai.com/plugins/build/plugins, read 2026-11-07). Codex reads a
    portable plugin's parts, "Portable packages discover skills from the root skills/ directory"
    and "Configure portable MCP servers in root mcp.json", and another plugin through
    `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json` or `.cursor-plugin/plugin.json`
    (`DISCOVERABLE_PLUGIN_MANIFEST_PATHS` of openai/codex rust-v0.167.0)."""

    def _marketplace(self, *sources, name="local", **entry_fields) -> dict:
        return {"name": name, "plugins": [{"name": f"p{number}", "source": source, **entry_fields}
                                          for number, source in enumerate(sources)]}

    def _portable(self, folder: str) -> dict:
        prefix = f"{folder}/" if folder else ""
        return {f"{prefix}plugin.json": MANIFEST, f"{prefix}mcp.json": MCP_DOCUMENT,
                f"{prefix}skills/op/SKILL.md": self.SKILL}

    def test_a_listed_portable_plugin_is_read_as_codex_s(self):
        """marionettejs/marionette's `.agents/plugins/marketplace.json` lists `plugins/marionette`
        as an object of the source `local`, and the page allows a plain path as well."""
        for source in ({"source": "local", "path": "./plugins/p"}, "./plugins/p"):
            with self.subTest(source=source):
                self._tree({**self._portable("plugins/p"),
                            ".agents/plugins/marketplace.json": self._marketplace(source)})
                self.assertEqual(self._parts("codex"), ["plugins/p/mcp.json", "plugins/p/skills"])
                self.assertEqual(self._parts("any"), ["plugins/p/mcp.json", "plugins/p/skills"])

    def test_without_the_marketplace_the_portable_plugin_names_no_tool(self):
        self._tree(self._portable("plugins/p"))
        self.assertEqual(self._parts("codex"), [])

    def test_the_source_dot_slash_names_the_root_of_the_marketplace(self):
        """bex-co/beancount-io's marketplace lists `./`, its repository root, which holds a
        portable manifest. `resolve_local_plugin_source_path` takes `.` and `./` as the
        marketplace root."""
        for source in ("./", "."):
            with self.subTest(source=source):
                self._tree({**self._portable(""),
                            ".agents/plugins/marketplace.json": self._marketplace(
                                {"source": "local", "path": source})})
                self.assertEqual(self._parts("codex"), ["mcp.json", "skills"])

    def test_a_marketplace_below_the_root_resolves_against_its_own_root(self):
        """"Codex resolves source.path relative to the marketplace root, not relative to the
        .agents/plugins/ folder", the folder that contains `.agents/`, as in Jij-Inc/Qamomile's
        `qamomile/agent_plugins/`."""
        self._tree({**self._portable("pkg/plugins/p"),
                    "pkg/.agents/plugins/marketplace.json": self._marketplace("./plugins/p")})
        self.assertEqual(self._parts("codex"), ["pkg/plugins/p/mcp.json", "pkg/plugins/p/skills"])

    def test_a_path_that_codex_refuses_names_no_plugin(self):
        """"local plugin source path must start with `./`" and "local plugin source path must
        stay within the marketplace root" (codex-rs/core-plugins/src/marketplace.rs of
        rust-v0.167.0)."""
        for source in ("plugins/p", "./plugins/../plugins/p", "././plugins/p", ".//plugins/p",
                       "", {"source": "url", "url": "./plugins/p"}, {"source": "local"}):
            with self.subTest(source=source):
                self._tree({**self._portable("plugins/p"),
                            ".agents/plugins/marketplace.json": self._marketplace(source)})
                self.assertEqual(self._parts("codex"), [])

    def test_an_inner_dot_and_a_trailing_slash_name_the_folder(self):
        """Rust's `Path::components` drops a `.` and an empty component inside a path."""
        for source in ("./plugins/./p", "./plugins//p/"):
            with self.subTest(source=source):
                self._tree({**self._portable("plugins/p"),
                            ".agents/plugins/marketplace.json": self._marketplace(source)})
                self.assertEqual(self._parts("codex"), ["plugins/p/mcp.json", "plugins/p/skills"])

    def test_a_marketplace_that_codex_cannot_read_names_no_plugin(self):
        """Codex reads a marketplace as `RawMarketplaceManifest`, and an entry without a string
        `name` fails the whole file. An entry with the installation policy `NOT_AVAILABLE` cannot
        be installed (`find_installable_marketplace_plugin`)."""
        broken = {"name": "local", "plugins": [{"name": "p", "source": "./plugins/p"},
                                               {"source": "./plugins/q"}]}
        unavailable = self._marketplace("./plugins/p", policy={"installation": "NOT_AVAILABLE"})
        for name, marketplace in (("an entry without a name", broken),
                                  ("a marketplace without a name", {"plugins": [
                                      {"name": "p", "source": "./plugins/p"}]}),
                                  ("an entry that is not available", unavailable)):
            with self.subTest(marketplace=name):
                self._tree({**self._portable("plugins/p"),
                            ".agents/plugins/marketplace.json": marketplace})
                self.assertEqual(self._parts("codex"), [])

    def test_codex_installs_no_entry_that_its_policy_or_its_name_refuses(self):
        """`find_installable_marketplace_plugin` (codex-rs/core-plugins/src/marketplace.rs of
        openai/codex c9b4267c370a) refuses an entry with `products` that leave out Codex,
        `Some([]) => false`, and `PluginId::new` a marketplace name with a character other than ASCII
        letters, digits, `_` and `-`. A policy value that serde does not know fails the whole
        file. Codex installs an entry with the product in capitals, `#[serde(alias =
        "CODEX")]`."""
        for name, marketplace in (
                ("no product", self._marketplace("./plugins/p", policy={"products": []})),
                ("another product", self._marketplace("./plugins/p",
                                                      policy={"products": ["chatgpt"]})),
                ("an unknown product", self._marketplace("./plugins/p",
                                                         policy={"products": ["codex", "web"]})),
                ("an unknown installation", self._marketplace(
                    "./plugins/p", policy={"installation": "LATER"})),
                ("a policy that is no object", self._marketplace("./plugins/p", policy=None)),
                ("a marketplace name with a space", self._marketplace("./plugins/p",
                                                                      name="local plugins"))):
            with self.subTest(marketplace=name):
                self._tree({**self._portable("plugins/p"),
                            ".agents/plugins/marketplace.json": marketplace})
                self.assertEqual(self._parts("codex"), [])
        self._tree({**self._portable("plugins/p"),
                    ".agents/plugins/marketplace.json": self._marketplace(
                        "./plugins/p", policy={"products": ["CODEX"], "authentication": "ON_USE"})})
        self.assertEqual(self._parts("codex"), ["plugins/p/mcp.json", "plugins/p/skills"])

    def test_a_listed_claude_code_plugin_is_read_through_its_manifest(self):
        """obsei/obsei's marketplace lists `integrations/claude/obsei`, which holds the manifests
        of Claude Code and Cursor and no Codex manifest. Codex takes the first of
        `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json` and
        `.cursor-plugin/plugin.json` that exists and reads the plugin in the compatibility
        layout, with the paths that the manifest names in place of the defaults."""
        layout = {".agents/plugins/marketplace.json": self._marketplace("./p"),
                  "p/.claude-plugin/plugin.json": {"name": "p"},
                  "p/.cursor-plugin/plugin.json": {"name": "p", "skills": "./cursor-skills/"},
                  "p/.mcp.json": self.SERVERS,
                  "p/skills/op/SKILL.md": self.SKILL,
                  "p/cursor-skills/c/SKILL.md": self.SKILL}
        self._tree(layout)
        self.assertEqual(self._parts("codex"), ["p/.mcp.json", "p/skills"])
        self._tree({**layout, "p/.claude-plugin/plugin.json": {"name": "p", "skills": "./custom/"},
                    "p/custom/x/SKILL.md": self.SKILL})
        self.assertEqual(self._parts("codex"), ["p/.mcp.json", "p/custom/x"])

    def test_a_listed_folder_without_a_manifest_reads_the_entry_as_its_manifest(self):
        """Codex installs a folder without a manifest with a manifest that it writes from the
        entry's fields where the entry has one besides `name`, `source` and `policy`, or a
        category (`marketplace_plugin_manifest_fallback`), and loads no plugin from a folder
        without either, "missing or invalid plugin.json"."""
        folder = {"p/.mcp.json": self.SERVERS, "p/skills/op/SKILL.md": self.SKILL,
                  "p/custom/x/SKILL.md": self.SKILL}
        self._tree({**folder, ".agents/plugins/marketplace.json": self._marketplace(
            "./p", category="Productivity")})
        self.assertEqual(self._parts("codex"), ["p/.mcp.json", "p/skills"])
        self._tree({**folder, ".agents/plugins/marketplace.json": self._marketplace(
            "./p", skills="./custom/")})
        self.assertEqual(self._parts("codex"), ["p/.mcp.json", "p/custom/x"])
        self._tree({**folder, ".agents/plugins/marketplace.json": self._marketplace(
            "./p", policy={"installation": "AVAILABLE"})})
        self.assertEqual(self._parts("codex"), [])

    def test_the_servers_that_the_entry_configures_inline_are_the_marketplace_s(self):
        """An inline `mcpServers` of the manifest that Codex writes from the entry is held by the
        marketplace file."""
        self._tree({"p/skills/op/SKILL.md": self.SKILL,
                    ".agents/plugins/marketplace.json": self._marketplace(
                        "./p", mcpServers={"x": {"command": "x"}})})
        inline = [rel for rel, rule, kind in paths.plugin_components(self.repo)
                  if kind == "inline" and rule["tool"] == "codex"]
        self.assertEqual(inline, [".agents/plugins/marketplace.json"])


class TestTheLocalSourcesOfACopilotMarketplace(_PluginTree):
    """A folder that a Copilot CLI marketplace lists is a Copilot plugin where it has a manifest
    that the CLI reads: "All plugins consist of a plugin directory containing a manifest file
    named plugin.json", "Legacy plugins: .plugin/plugin.json, plugin.json,
    .github/plugin/plugin.json, or .claude-plugin/plugin.json (checked in this order)", and the
    source is "the path to the plugin's directory, relative to the root of the repository. It is
    not necessary to use ./ at the start of the path" (CLI plugin reference, read 2026-11-07).
    `metadata.pluginRoot` prefixes the source (`dFt` of Copilot CLI 1.0.63)."""

    MARKETPLACE = {"name": "m", "owner": {"name": "o"}}

    def _claude_plugin(self, folder: str, **manifest) -> dict:
        return {f"{folder}/.claude-plugin/plugin.json": {"name": "p", **manifest},
                f"{folder}/.mcp.json": self.SERVERS, f"{folder}/skills/op/SKILL.md": self.SKILL}

    def test_a_listed_claude_code_plugin_is_read_through_its_manifest(self):
        """Azure/azure-sdk-tools's `.github/plugin/marketplace.json` sets `"pluginRoot":
        "./plugins"` and lists `./azure-sdk-tools`, a folder with `.claude-plugin/plugin.json`
        alone, which names `"skills": "./skills/"` and `"mcpServers": "./.mcp.json"`."""
        self._tree({**self._claude_plugin("plugins/a", skills="./skills/", mcpServers="./.mcp.json"),
                    ".github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "metadata": {"pluginRoot": "./plugins"},
                        "plugins": [{"name": "a", "source": "./a"}]}})
        self.assertEqual(self._parts("copilot"), ["plugins/a/.mcp.json", "plugins/a/skills/op"])

    def test_without_the_marketplace_a_claude_code_plugin_is_no_copilot_plugin(self):
        self._tree(self._claude_plugin("plugins/a"))
        self.assertEqual(self._parts("copilot"), [])

    def test_a_source_without_its_prefix_and_each_marketplace_place(self):
        for place in _COPILOT_MARKETPLACE_PLACES:
            for source in ("./plugins/a", "plugins/a"):
                with self.subTest(place=place, source=source):
                    self._tree({**self._claude_plugin("plugins/a"),
                                place: {**self.MARKETPLACE,
                                        "plugins": [{"name": "a", "source": source}]}})
                    self.assertEqual(self._parts("copilot"),
                                     ["plugins/a/.mcp.json", "plugins/a/skills"])

    def test_a_marketplace_below_the_root_resolves_against_its_own_directory(self):
        """microsoft/TypeAgent keeps its marketplace in `ts/.github/plugin/`."""
        self._tree({**self._claude_plugin("ts/plugins/a"),
                    "ts/.github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "a", "source": "plugins/a"}]}})
        self.assertEqual(self._parts("copilot"), ["ts/plugins/a/.mcp.json", "ts/plugins/a/skills"])

    def test_the_collector_copies_the_marketplace_and_the_parts_that_copilot_reads(self):
        """The cloning step copies a `.github` below the root only at the places of the nested
        rows, and it copies the marketplace as a manifest of the plugins that it lists, with the
        `hooks.json` that only Copilot CLI reads in a Claude Code plugin, so that the copy shows
        the plugin as the clone does. dotnet/dotnet keeps such a marketplace in
        `src/msbuild/.github/plugin/`."""
        hooks = '{"hooks": {"sessionStart": [{"type": "command", "bash": "x"}]}}'
        self._tree({**self._claude_plugin("ts/plugins/a"), "ts/plugins/a/hooks.json": hooks,
                    "ts/.github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "a", "source": "plugins/a"}]}})
        out = self.tmp / "out"
        self.assertIn("copilot", paths.collect_repo(self.repo, out, set()))
        for rel in ("ts/.github/plugin/marketplace.json", "ts/plugins/a/hooks.json",
                    "ts/plugins/a/skills/op/SKILL.md"):
            with self.subTest(path=rel):
                self.assertTrue((out / rel).is_file())
        self.assertEqual(paths.plugin_roots(out)["<copilot-plugin>"], ["ts/plugins/a"])

    def test_the_first_marketplace_file_decides(self):
        """`Dqe` of 1.0.63 reads the first file of its order that exists and fails on an invalid
        one, as mem0ai/mem0's root `marketplace.json` without an `owner` is."""
        self._tree({**self._claude_plugin("plugins/a"),
                    "marketplace.json": {"name": "m", "plugins": [{"name": "a",
                                                                   "source": "plugins/a"}]},
                    ".github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "a", "source": "plugins/a"}]}})
        self.assertEqual(self._parts("copilot"), [])

    def test_a_folder_without_a_manifest_is_no_plugin(self):
        """ansys/pyaedt lists `.github/plugin/pyaedt-cli`, which holds a skill and no manifest."""
        self._tree({"a/skills/op/SKILL.md": self.SKILL, "a/.mcp.json": self.SERVERS,
                    ".github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "a", "source": "a"}]}})
        self.assertEqual(self._parts("copilot"), [])

    def test_a_folder_outside_the_marketplace_s_directory_is_no_plugin(self):
        """`dFt` resolves the path without its `./` and refuses a folder outside the
        marketplace's directory, "Plugin path escapes marketplace directory", and accepts one
        inside it and the directory itself, which `.` and `./` name."""
        files = {**self._claude_plugin("a"), **self._claude_plugin("pkg/c"),
                 **self._claude_plugin("pkg")}
        directory = ["pkg/.mcp.json", "pkg/skills"]
        for source, parts in (("../a", []), ("./b/../../a", []),
                              ("./b/../c", ["pkg/c/.mcp.json", "pkg/c/skills"]),
                              (".", directory), ("./", directory)):
            with self.subTest(source=source):
                self._tree({**files,
                            "pkg/.plugin/marketplace.json": {
                                **self.MARKETPLACE, "plugins": [{"name": "a", "source": source}]}})
                self.assertEqual(self._parts("copilot"), parts)

    def test_a_listed_portable_plugin_is_read_as_copilot_s(self):
        """fastrepl/anarlog's `.github/plugin/marketplace.json` lists `agent-plugins/anarlog`,
        which has a portable manifest. Copilot CLI reads "Agent Plugins: skills/" and "Agent
        Plugins: mcp.json"."""
        self._tree({"plugins/p/plugin.json": MANIFEST, "plugins/p/mcp.json": MCP_DOCUMENT,
                    "plugins/p/skills/op/SKILL.md": self.SKILL,
                    ".github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "p", "source": "./plugins/p"}]}})
        self.assertEqual(self._parts("copilot"), ["plugins/p/mcp.json", "plugins/p/skills"])

    def test_the_claude_code_manifest_is_the_one_that_copilot_reads_last(self):
        """In a listed folder with a manifest of Copilot CLI's own, the CLI reads that one, and
        `.claude-plugin/plugin.json` is Claude Code's alone."""
        self._tree({**self._claude_plugin("plugins/a", skills="./claude-skills/"),
                    "plugins/a/.plugin/plugin.json": {"name": "a"},
                    "plugins/a/claude-skills/c/SKILL.md": self.SKILL,
                    ".github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "a", "source": "plugins/a"}]}})
        self.assertIn("plugins/a/skills", self._parts("copilot"))
        self.assertNotIn("plugins/a/claude-skills/c", self._parts("copilot"))
        self.assertIsNone(paths.borrowed_manifest(self.repo, "plugins/a", "<copilot-plugin>", []))
        self._tree({**self._claude_plugin("plugins/a"),
                    ".github/plugin/marketplace.json": {
                        **self.MARKETPLACE, "plugins": [{"name": "a", "source": "plugins/a"}]}})
        self.assertEqual(paths.borrowed_manifest(self.repo, "plugins/a", "<copilot-plugin>", []),
                         "plugins/a/.claude-plugin/plugin.json")


# The places of a Copilot CLI marketplace in a marketplace's directory.
_COPILOT_MARKETPLACE_PLACES = ("marketplace.json", ".plugin/marketplace.json",
                               ".github/plugin/marketplace.json")


class TestACopilotPluginWithOnlyARootPluginJson(unittest.TestCase):
    """A legacy Copilot CLI plugin in the reference's default layout, a `plugin.json` at the
    plugin root: "All plugins consist of a plugin directory containing a manifest file named
    plugin.json." (CLI plugin reference, read 2026-10-28). The CLI installs any directory with a
    plugin.json that parses, and whether the pipeline counts the directory as a Copilot plugin
    depends on the content of its manifest (`paths.copilot_legacy_manifest`): a name that the
    reference allows, and a component path field that names a place of the plugin in a form that
    the CLI resolves."""

    MT_MIGRATION = {
        # dotnet/msbuild's plugin, which its .github/copilot/settings.json enables.
        "plugins/mt-migration/plugin.json": json.dumps({
            "name": "mt-migration", "version": "0.1.0", "skills": ["./skills/"],
            "agents": ["./agents/mt-migration-reviewer.agent.md"]}),
        "plugins/mt-migration/skills/mt-migration/SKILL.md":
            "---\nname: mt-migration\ndescription: d\n---\n",
        "plugins/mt-migration/agents/mt-migration-reviewer.agent.md": "---\ndescription: d\n---\n",
        "plugins/mt-migration/README.md": "# MT migration\n",
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _copilot_roots(self) -> list:
        return paths.plugin_roots(self.repo)["<copilot-plugin>"]

    def _copilot_parts(self) -> list:
        return sorted((rel, rule["mechanism"]) for rel, rule, _kind
                      in paths.plugin_components(self.repo) if rule["tool"] == "copilot")

    def test_a_legacy_manifest_makes_its_directory_a_copilot_plugin(self):
        self._tree(self.MT_MIGRATION)
        self.assertEqual(self._copilot_roots(), ["plugins/mt-migration"])
        self.assertEqual(self._copilot_parts(),
                         [("plugins/mt-migration/agents/mt-migration-reviewer.agent.md",
                           "subagents"),
                          ("plugins/mt-migration/plugin.json", "plugins"),
                          ("plugins/mt-migration/skills/mt-migration", "skills")])
        out = self.tmp / "out"
        collect_repo(self.repo, out, set())
        self.assertEqual(sorted(p.relative_to(out).as_posix() for p in out.rglob("*")
                                if p.is_file()),
                         sorted(rel for rel in self.MT_MIGRATION if not rel.endswith("README.md")))

    def test_each_form_of_a_component_path_field(self):
        """A directory field takes a path, a list of paths or `{"paths": [...]}`, and a file
        field a path, as microsoft/TypeAgent's plugins write them."""
        parts = {"p/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n",
                 "p/hooks.json": '{"version": 1, "hooks": {"sessionStart": []}}',
                 "p/.mcp.json": '{"mcpServers": {"x": {"command": "x"}}}'}
        for fields in ({"skills": "skills/"}, {"skills": ["./skills/"]},
                       {"skills": {"paths": ["./skills/"]}}, {"hooks": "hooks.json"},
                       {"mcpServers": ".mcp.json"}):
            with self.subTest(fields=fields):
                self._tree({"p/plugin.json": json.dumps({"name": "typeagent", **fields}), **parts})
                self.assertEqual(self._copilot_roots(), ["p"])

    def test_another_program_s_plugin_json_makes_no_plugin(self):
        """The plugin.json of other programs, with keys of the same names in other forms, and
        files that are no legacy manifest by their content, each beside a skills directory."""
        skills = {"p/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n",
                  "p/agents/a.agent.md": "---\ndescription: d\n---\n",
                  "q/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n"}
        documents = {
            "Grafana's extensions": {"type": "app", "name": "Logs", "id": "grafana-logs-app",
                                     "extensions": {"addedLinks": [{"title": "Open"}]}},
            "NodeBB's hooks": {"id": "nodebb-plugin-poll", "name": "Poll",
                               "hooks": [{"hook": "static:app.load", "method": "load"}]},
            "ChurchCRM's hooks": {"name": "mailchimp", "hooks": ["person.created"]},
            "Explorer++'s commands": {"name": "sort-tabs",
                                      "commands": [{"key": "Alt+S", "name": "sort_tabs"}]},
            "a list of skill objects": {"name": "keplergl", "skills": [{"path": "./SKILL.md"}]},
            "a name with a dot": {"name": "kepler.gl", "skills": "./skills/"},
            "a name with a space": {"name": "Custom Menu Links", "skills": "./skills/"},
            "no name": {"skills": "./skills/"},
            "a field that names no place": {"name": "perfetto", "skills": "./missing/"},
            "a field that leaves the plugin": {"name": "up", "skills": "../q/skills"},
            "an inline MCP object": {"name": "inline", "mcpServers": {"x": {"command": "x"}}},
            "no component field": {"name": "whodb", "version": "1.0.0"},
        }
        for case, document in documents.items():
            with self.subTest(case=case):
                self._tree({"p/plugin.json": json.dumps(document), **skills})
                self.assertEqual(self._copilot_roots(), [])
                self.assertEqual(self._copilot_parts(), [])
        for case, text in (("comments", '// x\n{"name": "p", "skills": "./skills/"}'),
                           ("an array", '[{"name": "p", "skills": "./skills/"}]')):
            with self.subTest(case=case):
                self._tree({"p/plugin.json": text, **skills})
                self.assertEqual(self._copilot_roots(), [])

    def test_a_plugin_json_of_another_kind_is_no_legacy_manifest(self):
        """A plugin.json in a vendor's manifest directory, in an Antigravity plugin or with the
        Agent Plugins schema is the manifest of that kind, whatever fields it sets."""
        legacy = '{"name": "p", "skills": "./skills/"}'
        for manifest in ("p/.claude-plugin/plugin.json", "p/.codex-plugin/plugin.json",
                         ".agents/plugins/p/plugin.json"):
            with self.subTest(manifest=manifest):
                base = manifest.rsplit("/", 1)[0]
                self._tree({manifest: legacy,
                            f"{base}/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n"})
                self.assertEqual(self._copilot_roots(), [])
        self._tree({"p/plugin.json": json.dumps({**json.loads(MANIFEST), "skills": "./skills/"}),
                    "p/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n"})
        self.assertEqual(self._copilot_roots(), [])
        self.assertEqual(paths.plugin_roots(self.repo)["<agent-plugin>"], ["p"])

    def test_a_plugin_json_in_a_directory_named_dot_name_plugin_is_no_legacy_manifest(self):
        """A directory named `.<name>-plugin` is a vendor's manifest directory in the plugin
        root, as `.claude-plugin` is, also for a vendor that `paths._MANIFEST_DIRS` does not
        list. saadeghi/daisyui keeps the Grok plugin of its skills directory in
        skills/.grok-plugin/, and its Grok marketplace names ./skills as the plugin's source. A
        legacy Copilot CLI manifest is directly in the plugin directory, as the reference's
        default layout has plugin.json in my-plugin/ (CLI plugin reference)."""
        skill = "---\nname: daisyui\ndescription: d\n---\n"
        grok = json.dumps({"name": "daisyui", "displayName": "daisyUI", "version": "5.7.0",
                           "skills": "./"})
        self._tree({".claude-plugin/plugin.json": '{"name": "daisyui"}',
                    ".cursor-plugin/plugin.json": '{"name": "daisyui"}',
                    ".grok-plugin/marketplace.json": json.dumps({
                        "name": "daisyui", "plugins": [{"name": "daisyui", "source": {
                            "type": "local", "path": "./skills"}}]}),
                    "skills/.grok-plugin/plugin.json": grok,
                    "skills/daisyui/SKILL.md": skill})
        self.assertEqual(self._copilot_roots(), [])
        self.assertEqual(self._copilot_parts(), [])
        self.assertNotIn("skills/.grok-plugin/plugin.json", paths.plugin_manifests(self.repo))
        for directory in ("p/.grok-plugin", ".x-plugin", "p/.my-tool-plugin"):
            with self.subTest(directory=directory):
                self._tree({f"{directory}/plugin.json": '{"name": "p", "skills": "./skills/"}',
                            f"{directory}/skills/s/SKILL.md": skill})
                self.assertEqual(self._copilot_roots(), [])
        # A directory with a name that ends in -plugin but does not begin with a dot is a plugin
        # directory.
        self._tree({"p/my-plugin/plugin.json": '{"name": "p", "skills": "./skills/"}',
                    "p/my-plugin/skills/s/SKILL.md": skill})
        self.assertEqual(self._copilot_roots(), ["p/my-plugin"])

    def test_a_root_plugin_json_beside_another_copilot_manifest_counts_by_its_content(self):
        """The CLI reads the root plugin.json before .github/plugin/plugin.json, and the
        manifest row counts it only with the content of a legacy manifest."""
        skill = {"p/skills/s/SKILL.md": "---\nname: s\ndescription: d\n---\n"}
        github = {"p/.github/plugin/plugin.json": '{"name": "p"}'}
        self._tree({"p/plugin.json": '{"name": "p"}', **github, **skill})
        self.assertNotIn(("p/plugin.json", "plugins"), self._copilot_parts())
        self._tree({"p/plugin.json": '{"name": "p", "skills": "./skills/"}', **github, **skill})
        self.assertIn(("p/plugin.json", "plugins"), self._copilot_parts())


class TestThePlacesOfANestedGithub(unittest.TestCase):
    """Copilot CLI reads `.github/copilot-instructions.md`, `.github/instructions`,
    `.github/agents`, `.github/skills`, `.github/lsp.json`, `.github/mcp.json` and `.mcp.json` in
    its working directory and in each directory between it and the root (the `nested:` rows),
    and reads the rest of a `.github`, as GitHub does, at the root alone."""

    FILES = {
        "packages/web/.github/copilot-instructions.md": "Use pnpm.\n",
        "packages/web/.github/instructions/react.instructions.md": "---\napplyTo: '**'\n---\nx\n",
        "packages/web/.github/agents/review.agent.md": "---\ndescription: Review\n---\nx\n",
        "packages/web/.github/agents/notes.txt": "x\n",
        "packages/web/.github/skills/release/SKILL.md": "---\nname: release\ndescription: d\n---\n",
        "packages/web/.github/lsp.json": LSP_DOCUMENT,
        "packages/web/.github/workflows/ci.yml": "jobs: {}\n",
        "packages/web/.github/prompts/fix.prompt.md": "Fix.\n",
        "packages/web/.github/copilot/settings.json": '{"model": "x"}\n',
        "packages/web/.github/hooks/h.json": '{"version": 1, "hooks": {"sessionStart": []}}\n',
        "packages/web/.github/mcp.json": '{"mcpServers": {"x": {"command": "x"}}}\n',
        "packages/web/.mcp.json": '{"web": {"command": "web"}}\n',
        "packages/web/.github/.mcp.json": "{}\n",
        ".mcp.json": '{"mcpServers": {"root": {"command": "root"}}}\n',
        ".claude/templates/.github/copilot-instructions.md": "x\n",
        ".claude/templates/.mcp.json": "{}\n",
        "node_modules/pkg/.github/copilot-instructions.md": "x\n",
        "node_modules/pkg/.mcp.json": "{}\n",
        "src/README.md": "x\n",
    }

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo, self.out = self.tmp / "owner§repo", self.tmp / "out"
        for rel, text in self.FILES.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def test_the_places_below_the_root(self):
        places = [(rel, row["mechanism"]) for rel, row in
                  paths.nested_places(self.repo, {"node_modules"})]
        self.assertEqual(places, [
            ("packages/web/.github/agents", "subagents"),
            ("packages/web/.github/copilot-instructions.md", "context_file"),
            ("packages/web/.github/instructions", "context_file"),
            ("packages/web/.github/lsp.json", "lsp"),
            ("packages/web/.github/mcp.json", "mcp"),
            ("packages/web/.github/skills", "skills"),
            ("packages/web/.mcp.json", "mcp"),
        ])

    def test_the_root_github_is_no_place_of_these_rows(self):
        (self.repo / ".github").mkdir()
        (self.repo / ".github" / "copilot-instructions.md").write_text("x\n", encoding="utf-8")
        places = [rel for rel, _row in paths.nested_places(self.repo, set())]
        self.assertNotIn(".github/copilot-instructions.md", places)
        self.assertNotIn(".mcp.json", places)

    def test_a_mcp_json_is_a_file_with_its_name_or_a_link_to_one(self):
        """The `.mcp.json` of a directory below the root is a file with that name, spelled as
        the root's file is copied, or a link to a file of the repository, which Copilot CLI
        reads through the link. A directory with the name, a link out of the repository and a
        link that leads nowhere are no place."""
        outside = self.tmp / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        for rel, target in {"packages/api/.mcp.json": "../web/.mcp.json",
                            "packages/ext/.mcp.json": str(outside),
                            "packages/gone/.mcp.json": "missing.json"}.items():
            (self.repo / rel).parent.mkdir(parents=True)
            os.symlink(target, self.repo / rel)
        (self.repo / "packages/cli/.mcp.json").mkdir(parents=True)
        (self.repo / "packages/cli/.mcp.json/x").write_text("x\n", encoding="utf-8")
        (self.repo / "packages/docs").mkdir(parents=True)
        (self.repo / "packages/docs/.MCP.json").write_text("{}\n", encoding="utf-8")
        places = [rel for rel, row in paths.nested_places(self.repo, {"node_modules"})
                  if row["path"] == ".mcp.json"]
        self.assertEqual(places, ["packages/api/.mcp.json", "packages/web/.mcp.json"])

    def test_the_first_segment_is_matched_in_any_case_and_a_link_that_leads_nowhere_is_no_place(self):
        (self.repo / "packages/web/.github/copilot-instructions.md").rename(
            self.repo / "packages/web/.github/Copilot-Instructions.md")
        shutil.rmtree(self.repo / "packages/web/.github/skills")
        os.symlink("../../../shared", self.repo / "packages/web/.github/skills")
        places = [rel for rel, _row in paths.nested_places(self.repo, {"node_modules"})]
        self.assertIn("packages/web/.github/Copilot-Instructions.md", places)
        self.assertNotIn("packages/web/.github/skills", places)

    def test_a_link_to_a_file_or_a_folder_of_the_repository_is_a_place(self):
        """dotnet/dotnet links `src/msbuild/.github/copilot-instructions.md` to `../AGENTS.md`,
        and Copilot CLI reads a place through a link, as it reads the root's. A link to the
        root, to a folder that contains it, into `.git` or out of the repository is no place,
        because the collector copies nothing through it."""
        (self.repo / ".git").mkdir()
        (self.repo / ".git/config").write_text("[core]\n", encoding="utf-8")
        for rel, text in {"pkg/AGENTS.md": "Use pnpm.\n",
                          "shared/agents/review.agent.md": "---\ndescription: d\n---\nx\n"}.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        (self.repo / "pkg/.github").mkdir()
        for rel, target in {"pkg/.github/copilot-instructions.md": "../AGENTS.md",
                            "pkg/.github/agents": "../../shared/agents",
                            "pkg/.github/skills": "..",
                            "pkg/.github/lsp.json": "../../.git/config",
                            "pkg/.github/instructions": str(self.tmp)}.items():
            os.symlink(target, self.repo / rel)
        places = [(rel, row["mechanism"]) for rel, row in
                  paths.nested_places(self.repo, {"node_modules"}) if rel.startswith("pkg/")]
        self.assertEqual(places, [("pkg/.github/agents", "subagents"),
                                  ("pkg/.github/copilot-instructions.md", "context_file")])

    def test_the_collector_copies_a_place_that_is_a_link_as_its_marker(self):
        """The marker of a link to a folder is copied at its own path together with the folder, as
        for a `.github/agents` at the root that is a link."""
        for rel, text in {"pkg/AGENTS.md": "Use pnpm.\n",
                          "shared/agents/review.agent.md": "---\ndescription: d\n---\nx\n"}.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        (self.repo / "pkg/.github").mkdir()
        os.symlink("../AGENTS.md", self.repo / "pkg/.github/copilot-instructions.md")
        os.symlink("../../shared/agents", self.repo / "pkg/.github/agents")
        flags = paths.collect_repo(self.repo, self.out, {"node_modules"})
        self.assertEqual((self.out / "pkg/.github/copilot-instructions.md").read_text(),
                         "../AGENTS.md\n")
        self.assertEqual((self.out / "pkg/.github/agents").read_text(), "../../shared/agents\n")
        self.assertTrue((self.out / "shared/agents/review.agent.md").is_file())
        self.assertIn("copilot", flags)

    def test_the_collector_copies_the_places_and_nothing_else_of_the_github(self):
        flags = paths.collect_repo(self.repo, self.out, {"node_modules"})
        copied = {p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file()}
        self.assertEqual({rel for rel in copied if rel.startswith("packages/")}, {
            "packages/web/.github/copilot-instructions.md",
            "packages/web/.github/instructions/react.instructions.md",
            "packages/web/.github/agents/review.agent.md",
            "packages/web/.github/agents/notes.txt",
            "packages/web/.github/skills/release/SKILL.md",
            "packages/web/.github/lsp.json",
            "packages/web/.github/mcp.json",
            "packages/web/.mcp.json",
        })
        self.assertNotIn("node_modules/pkg/.github/copilot-instructions.md", copied)
        self.assertNotIn("node_modules/pkg/.mcp.json", copied)
        self.assertIn("copilot", flags)


class TestDeclaredPluginParts(unittest.TestCase):
    """A Claude Code plugin's manifest can name places of its components beyond the default
    ones, "relative to the plugin root", and "Every component path must resolve inside the
    plugin root and must exist". A manifest can name skills or hooks that no default place
    contains."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _declared(self, vendored=frozenset()) -> list:
        return sorted((rel, rule["path"])
                      for rel, rule, kind in paths.plugin_components(self.repo, vendored)
                      if kind == "declared")

    def _manifest(self, **keys) -> dict:
        return {"plugins/p/.claude-plugin/plugin.json": json.dumps({"name": "p", **keys})}

    def test_a_skill_folder_and_a_folder_of_skills(self):
        """jasonxudeveloper/jengine names each skill folder, vercel/next.js the folder that
        contains them, its plugin root."""
        self._tree({**self._manifest(skills=["./one", "./many/"]),
                    "plugins/p/one/SKILL.md": "x", "plugins/p/many/a/SKILL.md": "x",
                    "plugins/p/many/b/c/SKILL.md": "x", "plugins/p/many/notes.md": "x"})
        self.assertEqual(self._declared(),
                         [("plugins/p/many/a", "<claude-plugin>/skills"),
                          ("plugins/p/one", "<claude-plugin>/skills")])

    def test_the_plugin_root_as_a_skills_entry(self):
        self._tree({"skills/.claude-plugin/plugin.json": json.dumps({"name": "s", "skills": "./"}),
                    "skills/adopt/SKILL.md": "x"})
        self.assertEqual(self._declared(), [("skills/adopt", "<claude-plugin>/skills")])

    def test_a_path_that_leaves_the_root_is_not_followed(self):
        """TrafficGuard/typedai's plugins name ../hooks/hooks.json, which resolves to a file
        outside the plugin, and which does not exist either."""
        self._tree({**self._manifest(hooks="../hooks/hooks.json", agents=["/etc/a.md"]),
                    "plugins/hooks/hooks.json": '{"hooks": {"Stop": []}}', "etc/a.md": "x"})
        self.assertEqual(self._declared(), [])

    def test_a_root_manifest_cannot_copy_an_outside_file(self):
        outside = self.tmp / "outside.json"
        outside.write_text('{"hooks": {"Stop": []}}')
        for value in (str(outside), "../outside.json"):
            with self.subTest(value=value):
                self._tree({".claude-plugin/plugin.json": json.dumps({"name": "p", "hooks": value})})
                self.assertEqual(self._declared(), [])
                out = self.tmp / "collected" / self.repo.name
                self.assertEqual(paths.collect_plugin_parts(self.repo, out, set()), set())
                self.assertFalse((out.parent / "outside.json").exists())
                self.assertFalse((out / "outside.json").exists())

    def test_a_sibling_with_the_same_prefix_is_outside_the_plugin(self):
        self._tree({**self._manifest(hooks="../p-shared/hooks.json"),
                    "plugins/p-shared/hooks.json": '{"hooks": {"Stop": []}}'})
        self.assertEqual(self._declared(), [])

    def test_declared_paths_are_relative_and_resolve_inside_the_plugin(self):
        # These path constraints apply before checking whether the file exists or a
        # symlink leaves the clone. Check each constraint even when the later check
        # would also reject an outside file.
        for base, value in (("", "/absolute/hooks.json"), ("", "../hooks.json"),
                            ("plugins/p", "../p-shared/hooks.json")):
            with self.subTest(base=base, value=value):
                self.assertIsNone(paths._inside_root(base, value))
        self.assertEqual(paths._inside_root("", "hooks/config.json"), "hooks/config.json")
        self.assertEqual(paths._inside_root("plugins/p", "hooks/config.json"),
                         "plugins/p/hooks/config.json")

    def test_a_place_that_is_not_there_is_not_followed(self):
        self._tree(self._manifest(hooks="./cfg/hooks.json", skills=["./gone/"]))
        self.assertEqual(self._declared(), [])

    def test_a_key_that_adds_does_not_repeat_the_default_place(self):
        self._tree({**self._manifest(skills=["./skills/"], hooks="./hooks/hooks.json"),
                    "plugins/p/skills/a/SKILL.md": "x",
                    "plugins/p/hooks/hooks.json": '{"hooks": {"Stop": []}}'})
        self.assertEqual(self._declared(), [])

    def test_urls_bundles_and_inline_configuration_name_no_file(self):
        self._tree({**self._manifest(mcpServers=["https://mcp.example.com", "./srv.mcpb",
                                                 {"x": {"command": "y"}}],
                                     hooks={"Stop": []}),
                    "plugins/p/srv.mcpb": "x"})
        self.assertEqual(self._declared(), [])

    def test_a_command_map_names_its_sources(self):
        self._tree({**self._manifest(commands={"status": {"source": "./cmds/status.md"},
                                               "about": {"content": "Explain."}}),
                    "plugins/p/cmds/status.md": "x"})
        self.assertEqual(self._declared(), [("plugins/p/cmds/status.md", "<claude-plugin>/commands")])

    def test_a_directory_yields_the_files_the_default_row_reads(self):
        self._tree({**self._manifest(outputStyles="./styles/", workflows=["./flows/"]),
                    "plugins/p/styles/terse.md": "x", "plugins/p/styles/logo.png": "x",
                    "plugins/p/flows/audit.js": "x", "plugins/p/flows/README.md": "x"})
        self.assertEqual(self._declared(),
                         [("plugins/p/flows/audit.js", "<claude-plugin>/workflows"),
                          ("plugins/p/styles/terse.md", "<claude-plugin>/output-styles")])

    def test_a_vendored_directory_is_skipped(self):
        self._tree({**self._manifest(skills=["./node_modules/pkg/skills/"]),
                    "plugins/p/node_modules/pkg/skills/a/SKILL.md": "x"})
        self.assertEqual(self._declared(frozenset({"node_modules"})), [])

    def test_a_manifest_that_is_not_json_names_nothing(self):
        self._tree({"plugins/p/.claude-plugin/plugin.json": "{not json", "plugins/p/x/SKILL.md": "x"})
        self.assertEqual(self._declared(), [])

    def test_the_parts_include_the_declared_places(self):
        """What the collector copies and the cleanup keeps."""
        self._tree({**self._manifest(hooks="./cfg/hooks.json"),
                    "plugins/p/cfg/hooks.json": '{"hooks": {"Stop": []}}'})
        self.assertIn("plugins/p/cfg/hooks.json", [rel for rel, _ in paths.plugin_parts(self.repo)])


class TestTheMcpServersThatClaudeCodeLoads(unittest.TestCase):
    """Claude Code 2.1.319 skips a server of a file that fails the schema of its type. The
    expectations of the fields are what `claude plugin validate` of Claude Code 2.1.319 reports
    for a manifest with the server inline, which it parses with the same schemas: "Invalid
    input" for a boolean in `env`, a null `env`, a string in `args`, a number in `headers`, a
    timeout of 30.5 and a string in `alwaysLoad`, "Too small: expected number to be >0" for a
    timeout of 0 and "Too big: expected int to be <=9007199254740991" for 9007199254740993. It
    reports no error for a timeout of 30.0, and none for a `discoveryCache` of a `ws` server or
    `headers` of a stdio server, fields that their schemas do not declare."""

    URL = "https://example.invalid/mcp"

    def test_claude_code_loads_a_server_with_a_command_or_with_a_url_and_its_transport(self):
        cases = [({"a": {"command": "npx"}}, True),
                 ({"a": {"type": "stdio", "command": "npx"}}, True),
                 ({"a": {"type": "stdio", "command": ""}}, False),
                 ({"a": {"url": self.URL}}, False),
                 ({"a": {"type": "streamable-http", "url": self.URL}}, True),
                 ({"a": {"type": "ws", "url": "wss://example.invalid/mcp"}}, True),
                 ({"a": {"type": "sse", "url": "https://example.invalid/sse"}}, True),
                 ({"a": {"type": "sdk", "name": "a"}}, False),
                 ({"a": {"type": "http", "command": "npx"}}, False),
                 ({"a": {"type": [], "command": "npx"}}, False),
                 ({"a": {"type": {"name": "http"}, "url": self.URL}}, False),
                 ({"a": {"type": None, "command": "npx"}}, False),
                 ({"a": {"url": self.URL}, "b": {"command": "npx"}}, True)]
        for servers, loads in cases:
            with self.subTest(servers=servers):
                self.assertIs(paths.claude_loads_a_server(servers), loads)

    def test_claude_code_skips_a_server_with_a_field_of_another_type(self):
        cases = [({"command": "npx", "env": {"A": "x", "B": True}}, False),
                 ({"command": "npx", "env": None}, False),
                 ({"command": "npx", "args": "-y"}, False),
                 ({"type": "http", "url": self.URL, "headers": {"A": 1}}, False),
                 ({"command": "npx", "timeout": 30.5}, False),
                 ({"command": "npx", "timeout": 0}, False),
                 ({"command": "npx", "timeout": 9007199254740993}, False),
                 ({"command": "npx", "alwaysLoad": "true"}, False),
                 ({"command": "npx", "timeout": 30.0}, True),
                 ({"type": "ws", "url": "wss://example.invalid/mcp", "discoveryCache": "x"}, True),
                 ({"command": "npx", "headers": {"A": 1}}, True)]
        for server, loads in cases:
            with self.subTest(server=server):
                self.assertIs(paths.claude_loads_a_server({"a": server}), loads)


class TestHowAManifestKeyCombines(unittest.TestCase):
    """What a plugin's tool reads, per component, where its manifest names places. Claude
    Code's reference states which keys add, merge and replace. Copilot CLI 1.0.89 showed its
    forms, its precedence and its root SKILL.md in the tests of 2026-09-30."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _read(self) -> list:
        return sorted((rel, kind) for rel, _rule, kind in paths.plugin_components(self.repo))

    HOOKS = '{"hooks": {"Stop": []}}'
    MCP = '{"mcpServers": {"x": {"command": "y"}}}'

    def test_declared_flat_directories_exclude_nested_components(self):
        self._tree({".plugin/plugin.json": json.dumps({"name": "p", "agents": ["./custom"], "rules": "./shared"}),
                    "custom/top.md": "x", "custom/deep/nested.md": "x",
                    "shared/a.mdc": "---\ndescription: A\n---\n",
                    "shared/sub/b.mdc": "---\ndescription: B\n---\n"})
        self.assertEqual(self._read(), [("custom/top.md", "declared"), ("shared/a.mdc", "declared")])

    def test_claude_s_agents_key_replaces_the_agents_directory(self):
        """"Replaces the default: commands, agents, outputStyles, workflows"."""
        self._tree({".claude-plugin/plugin.json": json.dumps({"name": "p", "agents": ["./agents/r.md"]}),
                    "agents/r.md": "x", "agents/other.md": "x"})
        self.assertEqual(self._read(), [("agents/r.md", "declared")])

    def test_claude_commands_styles_and_workflows_replace_the_default_scan(self):
        self._tree({".claude-plugin/plugin.json": json.dumps({"name": "p",
                    "commands": ["./cmds/status.md"], "outputStyles": "./styles/",
                    "workflows": ["./flows/audit.js"]}),
                    "commands/old.md": "old", "cmds/status.md": "current",
                    "output-styles/plain.md": "old", "styles/terse.md": "current",
                    "workflows/old.js": "old", "flows/audit.js": "current"})
        self.assertEqual(self._read(), [("cmds/status.md", "declared"),
                                       ("flows/audit.js", "declared"),
                                       ("styles/terse.md", "declared")])

    def test_claude_s_agents_key_names_no_directory(self):
        """"Directories aren't accepted", and `claude plugin validation` of Claude Code 2.1.285
        reports "agents: Invalid string: must end with ".md"" for one."""
        self._tree({".claude-plugin/plugin.json": json.dumps({"name": "p", "agents": "./custom"}),
                    "custom/c.md": "x", "agents/d.md": "x"})
        self.assertEqual(self._read(), [])

    def test_copilot_s_agents_key_names_a_file_or_a_directory(self):
        """Copilot CLI 1.0.89 listed `filed` alone for the first plugin and `cust` alone for
        the second."""
        for value, expected in (("./my/filed.agent.md", [("my/filed.agent.md", "declared")]),
                                ("./custom", [("custom/cust.md", "declared")])):
            with self.subTest(value=value):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "agents": [value]}),
                            "my/filed.agent.md": "x", "my/sibling.md": "x",
                            "custom/cust.md": "x", "agents/deflt.md": "x"})
                self.assertEqual(self._read(), expected)

    def test_claude_s_skills_key_adds_and_hooks_merges(self):
        self._tree({".claude-plugin/plugin.json": json.dumps(
                        {"name": "p", "skills": ["./extra"], "hooks": "./cfg/h.json"}),
                    "skills/a/SKILL.md": "x", "extra/b/SKILL.md": "x",
                    "hooks/hooks.json": self.HOOKS, "cfg/h.json": self.HOOKS})
        self.assertEqual(self._read(), [("cfg/h.json", "declared"), ("extra/b", "declared"),
                                        ("hooks/hooks.json", "default"), ("skills", "default")])

    def test_a_declared_copilot_skill_stops_at_its_own_marker(self):
        for value, expected in ((".", [("SKILL.md", "root_skill")]),
                                ([".", "skills/child"], [("SKILL.md", "root_skill"),
                                                          ("skills/child", "declared")])):
            with self.subTest(value=value):
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "skills": value}),
                            "SKILL.md": "---\nname: root\ndescription: Root\n---\nInstructions\n",
                            "skills/child/SKILL.md": "---\nname: child\ndescription: Child\n---\nInstructions\n"})
                self.assertEqual(self._read(), expected)

    def test_copilot_s_string_replaces_and_its_paths_object_adds(self):
        for value, expected in (
                ("./extra", [("extra/s", "declared")]),
                (["./extra"], [("extra/s", "declared")]),
                ({"paths": ["./extra"]}, [("extra/s", "declared"), ("skills", "default")]),
                ({"paths": ["./extra"], "exclusive": True}, [("extra/s", "declared")])):
            with self.subTest(value=value):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "skills": value}),
                            "skills/d/SKILL.md": "x", "extra/s/SKILL.md": "x"})
                self.assertEqual(self._read(), expected)

    def test_copilot_s_object_form_takes_a_list_of_paths_alone(self):
        """Copilot CLI 1.0.63 refuses any other object: `if(!t.paths||!Array.isArray(t.paths)||
        !t.paths.every(a=>typeof a=="string"))throw new Error(`Invalid ${n} object form: expected
        { paths: string[] }`)`. A string there would be read as the list of its characters,
        and "." would make the plugin root an agents directory, its README and CHANGELOG two
        subagents."""
        for value in ({"paths": "."}, {"paths": True}, {"paths": [".", 3]}, {}):
            with self.subTest(value=value):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "agents": value}),
                            "README.md": "x", "CHANGELOG.md": "x", "agents/deflt.md": "x"})
                self.assertEqual(self._read(), [])

    def test_copilot_s_empty_value_is_the_default_place(self):
        """The CLI's resolver opens with `if(!t)return[YB.join(r,n)]`: an empty or false value
        is the default place, and its root SKILL.md is read where `!t.skills`."""
        for value in ("", False, 0):
            with self.subTest(value=value):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "agents": value}),
                            "agents/deflt.md": "x"})
                self.assertEqual(self._read(), [("agents", "default")])
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "skills": value}),
                            "SKILL.md": "A skill.\n"})
                self.assertEqual(self._read(), [("SKILL.md", "root_skill")])

    def test_copilot_alternatives_require_component_schema(self):
        for mechanism, first, fallback, valid in (
                ("hooks", "hooks.json", "hooks/hooks.json", self.HOOKS),
                ("lsp", ".lsp.json", "lsp.json", LSP_DOCUMENT)):
            invalid = ["{broken", "{}", "[]", json.dumps({"hooks" if mechanism == "hooks" else "lspServers": []})]
            for content in invalid:
                with self.subTest(mechanism=mechanism, content=content):
                    shutil.rmtree(self.repo, ignore_errors=True)
                    self._tree({".plugin/plugin.json": '{"name":"p"}', first: content, fallback: valid})
                    self.assertEqual(self._read(), [(fallback, "default")])
            shutil.rmtree(self.repo)
            self._tree({".plugin/plugin.json": '{"name":"p"}',
                        first: json.dumps({"hooks" if mechanism == "hooks" else "lspServers": {}}),
                        fallback: valid})
            self.assertEqual(self._read(), [(first, "default")])

    def test_copilot_invalid_inline_hook_entries_keep_the_default(self):
        for entry in ({"type": "no-such-type"}, {"type": "command"},
                      {"type": "command", "bash": 4}):
            with self.subTest(entry=entry):
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "hooks": {"sessionStart": [entry]}}),
                            "hooks.json": self.HOOKS})
                self.assertEqual(self._read(), [("hooks.json", "default")])
        self._tree({".plugin/plugin.json": json.dumps({"name": "p", "hooks": {"sessionStart": []}}),
                    "hooks.json": self.HOOKS})
        self.assertEqual(self._read(), [])

    def test_copilot_invalid_declared_hook_file_keeps_the_default(self):
        self._tree({".plugin/plugin.json": '{"name":"p","hooks":"./invalid.json"}',
                    "hooks.json": self.HOOKS, "invalid.json": '{}'})
        self.assertEqual(self._read(), [("hooks.json", "default")])

    def test_copilot_reads_the_first_file_of_a_list(self):
        """.mcp.json comes before .github/mcp.json, even one that declares no server. A
        malformed .lsp.json is passed over for lsp.json. hooks.json comes before
        hooks/hooks.json."""
        self._tree({".plugin/plugin.json": '{"name": "p"}',
                    ".mcp.json": '{"mcpServers": {}}', ".github/mcp.json": self.MCP,
                    ".lsp.json": "{not json", "lsp.json": LSP_DOCUMENT, ".github/lsp.json": LSP_DOCUMENT,
                    "hooks.json": self.HOOKS, "hooks/hooks.json": self.HOOKS})
        self.assertEqual(self._read(), [(".mcp.json", "default"), ("hooks.json", "default"),
                                        ("lsp.json", "default")])

    def test_a_malformed_mcp_file_is_passed_over(self):
        """Copilot CLI 1.0.89 listed the server of .github/mcp.json beside a .mcp.json that
        does not parse."""
        self._tree({".plugin/plugin.json": '{"name": "p"}', ".mcp.json": "{ this is not json",
                    ".github/mcp.json": self.MCP})
        self.assertEqual(self._read(), [(".github/mcp.json", "default")])

    def test_copilot_s_hooks_file_precedes_and_its_mcp_file_follows(self):
        self._tree({".plugin/plugin.json": json.dumps({"name": "p", "hooks": "./cfg/h.json",
                                                       "mcpServers": "./cfg/m.json",
                                                       "lspServers": "./cfg/l.json"}),
                    "hooks.json": self.HOOKS, "cfg/h.json": self.HOOKS,
                    ".mcp.json": self.MCP, "cfg/m.json": self.MCP, "cfg/l.json": LSP_DOCUMENT})
        self.assertEqual(self._read(), [(".mcp.json", "default"), ("cfg/h.json", "declared"),
                                        ("cfg/l.json", "declared")])

    def test_copilot_prefers_the_plugin_manifest_to_the_github_manifest(self):
        for secondary in (json.dumps({"name": "p"}), "{invalid"):
            with self.subTest(secondary=secondary):
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "skills": "./extra"}),
                            ".github/plugin/plugin.json": secondary,
                            "skills/old/SKILL.md": "old", "extra/current/SKILL.md": "current"})
                self.assertEqual(self._read(), [("extra/current", "declared")])

    def test_copilot_does_not_read_keys_from_a_second_manifest_after_a_parse_failure(self):
        self._tree({".plugin/plugin.json": "{invalid",
                    ".github/plugin/plugin.json": json.dumps({"name": "p", "skills": "./extra"}),
                    "skills/old/SKILL.md": "old", "extra/current/SKILL.md": "current"})
        self.assertEqual(self._read(), [("skills", "default")])

    def test_copilot_inline_hooks_and_escaping_paths_suppress_default_files(self):
        for value in ({"preToolUse": [{"type": "command", "bash": "echo inline"}]}, {},
                      "../shared/hooks.json"):
            with self.subTest(value=value):
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "hooks": value}),
                            "hooks.json": self.HOOKS})
                self.assertEqual(self._read(), [])

    def test_copilot_invalid_inline_hooks_keep_default_files(self):
        for value in ({"preToolUse": "invalid"}, [], True, 5, "./missing.json"):
            with self.subTest(value=value):
                self._tree({".plugin/plugin.json": json.dumps({"name": "p", "hooks": value}),
                            "hooks.json": self.HOOKS})
                self.assertEqual(self._read(), [("hooks.json", "default")])

    def test_a_hooks_file_that_is_not_there_leaves_the_default(self):
        self._tree({".plugin/plugin.json": json.dumps({"name": "p", "hooks": "./gone.json"}),
                    "hooks/hooks.json": self.HOOKS})
        self.assertEqual(self._read(), [("hooks/hooks.json", "default")])

    def test_a_root_skill_needs_no_skills_directory_and_no_skills_key(self):
        cases = {"alone": ({}, [("SKILL.md", "root_skill")]),
                 "beside a skills directory": ({"skills/a/SKILL.md": "x"}, [("skills", "default")]),
                 "with a skills key": ({"extra/e/SKILL.md": "x"}, [("extra/e", "declared")])}
        for name, (extra, expected) in cases.items():
            for manifest in (".claude-plugin/plugin.json", ".plugin/plugin.json"):
                with self.subTest(case=name, manifest=manifest):
                    shutil.rmtree(self.repo, ignore_errors=True)
                    doc = {"name": "p", **({"skills": "./extra"} if "key" in name else {})}
                    self._tree({manifest: json.dumps(doc), "SKILL.md": "x", **extra})
                    self.assertEqual(self._read(), expected)

    def test_a_root_skill_brings_its_own_directories(self):
        """The Agent Skills specification's scripts/, references/ and assets/ beside the
        SKILL.md, and nothing else of the plugin root, which can be the repository's."""
        self._tree({".claude-plugin/plugin.json": '{"name": "p"}', "SKILL.md": "x",
                    "references/config.md": "x", "assets/logo.svg": "x", "src/main.py": "x"})
        self.assertEqual(self._read(), [("SKILL.md", "root_skill"), ("assets", "root_skill_folder"),
                                        ("references", "root_skill_folder")])

    def test_copilot_s_rules_and_commands_directories(self):
        """rules/ contains .mdc files and commands/ .md files, each read one level deep."""
        self._tree({".plugin/plugin.json": '{"name": "p"}', "rules/a.mdc": "x",
                    "commands/ship.md": "x"})
        self.assertEqual(self._read(), [("commands", "default"), ("rules", "default")])


class TestDeclaredRootSkillCollection(unittest.TestCase):
    """A manifest naming its root keeps the skill and its support directories."""

    def _fixture(self, root, prefix, manifest, value):
        repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
        files = {
            prefix + manifest: json.dumps({"name": "p", "skills": value}),
            prefix + "SKILL.md": "---\nname: p\ndescription: Test skill\n---\nTest skill\n",
            prefix + "assets/example.txt": "support file\n",
            prefix + "src/main.c": "int main() {}\n",
            prefix + ".env": "TEST_ONLY=1\n",
        }
        for rel, body in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        paths.collect_repo(repo, out, set())
        return out

    def test_collection_keeps_only_the_skill_and_support(self):
        for prefix in ("", "plugins/p/"):
            for manifest in (".claude-plugin/plugin.json", ".plugin/plugin.json"):
                for value in (".", "./"):
                    with self.subTest(prefix=prefix, manifest=manifest, value=value), \
                            tempfile.TemporaryDirectory() as tmp:
                        out = self._fixture(Path(tmp), prefix, manifest, value)
                        files = {p.relative_to(out).as_posix() for p in out.rglob("*")
                                 if p.is_file()}
                        self.assertEqual(files, {prefix + manifest, prefix + "SKILL.md",
                                                 prefix + "assets/example.txt"})

    def test_validation_and_detection_keep_the_declared_root_skill(self):
        from agentcontext.steps import detection, collected_file_cleanup
        for prefix in ("", "plugins/p/"):
            for manifest in (".claude-plugin/plugin.json", ".plugin/plugin.json"):
                with self.subTest(prefix=prefix, manifest=manifest), \
                        tempfile.TemporaryDirectory() as tmp:
                    out = self._fixture(Path(tmp), prefix, manifest, "./")
                    collected_file_cleanup.scan_and_clean_repo(str(out), [])
                    rows = detection.detect_skills(out)
                    self.assertEqual([r["skills.md"] for r in rows], [prefix + "SKILL.md"])
                    self.assertEqual(rows[0]["assets"], "TRUE")
                    self.assertEqual((out / prefix / "assets/example.txt").read_text(),
                                     "support file\n")


class TestRegisteredRootSkillCollection(unittest.TestCase):
    """An Antigravity registration file that selects a skill with a folder that contains the
    file keeps the skill and its support directories. Antigravity CLI 1.4.2 counted such a
    folder as one skill (`antigravity plugin validate`). Copying the whole folder, which can be
    the repository, would copy its source code and its `.env`, and the file history step would
    run `git log` for each of those files."""

    CASES = (("", ".agents/skills.json", "SKILL.md"), ("", ".agents/skills.json", "."),
             ("pkg/", "pkg/.agents/skills.json", "."))

    def _fixture(self, root, prefix, registration, value):
        repo, out = root / "source" / "owner§repo", root / "collected" / "owner§repo"
        files = {
            registration: json.dumps({"entries": [{"path": value}]}),
            prefix + "SKILL.md": "---\nname: p\ndescription: Test skill\n---\nTest skill\n",
            prefix + "assets/example.txt": "support file\n",
            prefix + "src/main.c": "int main() {}\n",
            prefix + ".env": "TEST_ONLY=1\n",
        }
        for rel, body in files.items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        paths.collect_repo(repo, out, set())
        return out

    def test_collection_keeps_only_the_skill_and_support(self):
        for prefix, registration, value in self.CASES:
            with self.subTest(registration=registration, value=value), \
                    tempfile.TemporaryDirectory() as tmp:
                out = self._fixture(Path(tmp), prefix, registration, value)
                files = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
                self.assertEqual(files, {registration, prefix + "SKILL.md",
                                         prefix + "assets/example.txt"})

    def test_validation_and_detection_keep_the_registered_root_skill(self):
        from agentcontext.steps import detection, collected_file_cleanup
        for prefix, registration, value in self.CASES:
            with self.subTest(registration=registration, value=value), \
                    tempfile.TemporaryDirectory() as tmp:
                out = self._fixture(Path(tmp), prefix, registration, value)
                # Files of the skill's folder in the collected tree, for the cleanup to remove.
                for rel in ("src/main.c", ".env"):
                    (out / prefix / rel).parent.mkdir(parents=True, exist_ok=True)
                    (out / prefix / rel).write_text("not configuration\n")
                collected_file_cleanup.scan_and_clean_repo(str(out), [])
                self.assertEqual({p.relative_to(out).as_posix() for p in out.rglob("*")
                                  if p.is_file()},
                                 {registration, prefix + "SKILL.md", prefix + "assets/example.txt"})
                rows = [r for r in detection.detect_skills(out) if r["skills.md"] != registration]
                self.assertEqual([(r["skills.md"], r["name"], r["assets"]) for r in rows],
                                 [(prefix + "SKILL.md", prefix.rstrip("/") or "repo", "TRUE")])


class TestRuleFilesARulesJsonNames(unittest.TestCase):
    """Antigravity scans only the Markdown files directly in .agents/rules and reads the rest
    from what .agents/rules.json registers, "to share rules stored outside .agents/rules/ or
    to include nested subdirectories of rules". At the workspace root, each entry names a
    directory or an item relative to the root, narrowed by include_only and exclude, and inherits
    names further rules.json files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _registered(self, vendored=frozenset()) -> list:
        return [rel for rel, _rule in paths.declared_rule_files(self.repo, vendored)]

    def test_workspace_paths_depth_and_named_filters(self):
        """Each value of `include_only` and `exclude` names an item, and a value written as a
        regular expression names nothing."""
        self._tree({".agents/rules.json": json.dumps({"entries": [
                        {"path": "shared/rules", "exclude": ["experimental-api", "old-.*"]},
                        {"path": "shared/rules",
                         "include_only": ["nested/chosen.md", "nested/other[.]md"]}]}),
                    "shared/rules/top.md": _RULE,
                    "shared/rules/experimental-api.md": _RULE,
                    "shared/rules/old-api.md": _RULE,
                    "shared/rules/nested/chosen.md": _RULE,
                    "shared/rules/nested/other.md": _RULE,
                    "shared/rules/nested/ignored.md": _RULE,
                    ".agents/shared/rules/wrong.md": _RULE})
        self.assertEqual(self._registered(),
                         ["shared/rules/nested/chosen.md", "shared/rules/old-api.md",
                          "shared/rules/top.md"])

    def test_an_inherited_rules_json_survives_collection_and_validation(self):
        self._tree({".agents/rules.json": '{"inherits":[{"path":"config/rules.json"}]}',
                    "config/rules.json": '{"entries":[{"path":"shared"}]}',
                    "shared/style.md": _RULE})
        out = self.tmp / "collected"
        paths.collect_declared_rules(self.repo, out, set())
        self.assertEqual((out / "config/rules.json").read_text(),
                         '{"entries":[{"path":"shared"}]}')
        from agentcontext.steps import collected_file_cleanup, detection
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertTrue((out / "config/rules.json").is_file())
        self.assertIn("shared/style.md", [r["rule"] for r in detection.detect_rules(out)])

    def test_the_cloning_step_copies_a_registered_rule_only_with_a_front_matter_that_parses(self):
        """Antigravity CLI 1.4.10 quotes a `globs` value that starts with `*` before it decodes
        the front matter of a rule, and drops a rule with a front matter that does not decode.
        The cloning step copies the registered rules that Antigravity loads: `globs: **/*.html`,
        as in GoogleCloudPlatform/khi's rules, and not a front matter with a flow list that is
        not closed."""
        self._tree({".agents/rules.json": '{"entries": [{"path": "shared"}]}',
                    "shared/html.md": "---\ntrigger: glob\nglobs: **/*.html\n---\nUse tags.\n",
                    "shared/list.md": "---\ntrigger: always_on\ntags: [a, b\n---\nBroken.\n"})
        out = self.tmp / "collected"
        paths.collect_declared_rules(self.repo, out, set())
        self.assertEqual(_tree_files(out), [".agents/rules.json", "shared/html.md"])

    def test_a_value_of_another_type_fails_its_file_and_stops_nothing(self):
        """`inherits` and `entries` are lists, and Antigravity CLI 1.4.2 read nothing from a
        file with a number or a boolean there, and read the other registration files. A
        TypeError on it in the cloning step, the collected file cleanup and the detection step
        would take the whole repository out of the collection on every run and stop the
        collected file cleanup of the whole sample."""
        self._tree({".agents/rules.json": json.dumps({"inherits": True, "entries": 5}),
                    "team/.agents/rules.json": json.dumps({"inherits": 1, "entries": [{"path": "team/.agents/x"}]}),
                    "team/.agents/x/a.md": _RULE,
                    "other/.agents/rules.json": json.dumps({"entries": [{"path": "y"}]}),
                    "other/y/b.md": _RULE})
        self.assertEqual(self._registered(), ["other/y/b.md"])

    def test_the_page_s_example_is_read_as_the_page_states(self):
        """The rules page's own rules.json: a shared directory with one file and one folder
        left out, and one nested file of the rules directory."""
        self._tree({".agents/rules.json": json.dumps({
                        "entries": [{"path": "shared-rules",
                                     "exclude": ["deprecated_rules.md", "legacy/"]},
                                    {"path": ".agents/rules", "include_only": ["frontend/react.md"]}]}),
                    "shared-rules/api.md": _RULE, "shared-rules/deep/db.md": _RULE,
                    "shared-rules/deprecated_rules.md": _RULE, "shared-rules/legacy/old.md": _RULE,
                    "shared-rules/notes.txt": _RULE,
                    ".agents/rules/frontend/react.md": _RULE, ".agents/rules/frontend/vue.md": _RULE})
        self.assertEqual(self._registered(), [".agents/rules/frontend/react.md",
                                              "shared-rules/api.md"])

    def test_inherited_entries_resolve_from_the_workspace_root(self):
        self._tree({".agents/rules.json": '{"inherits": [{"path": "config/rules.json"}]}',
                    "config/rules.json": '{"entries": [{"path": "team/standards"}]}',
                    "team/standards/style.md": _RULE, "standards/other.md": _RULE})
        self.assertEqual(self._registered(), ["team/standards/style.md"])

    def test_rules_json_files_that_inherit_each_other_are_read_once(self):
        self._tree({".agents/rules.json": '{"inherits": [{"path": "config/b.json"}],'
                                          ' "entries": [{"path": "a"}]}',
                    "config/b.json": '{"inherits": [{"path": ".agents/rules.json"}], "entries":[{"path":"b"}]}',
                    "a/one.md": _RULE, "b/two.md": _RULE})
        self.assertEqual(self._registered(), ["a/one.md", "b/two.md"])

    def test_nothing_outside_the_repository_or_vendored_is_named(self):
        """The page's example inherits from a sibling project, which is not in this tree, and
        a directory under a vendored one is no more read here than anywhere else."""
        self._tree({".agents/rules.json": '{"inherits": [{"path": "../../shared/.agents/rules.json"}],'
                                          ' "entries": [{"path": "../../outside"},'
                                          ' {"path": "../node_modules/pkg/rules"}]}',
                    "node_modules/pkg/rules/x.md": "x\n"})
        self.assertEqual(self._registered({"node_modules"}), [])

    def test_a_rules_json_that_does_not_parse_names_nothing(self):
        self._tree({".agents/rules.json": "{not json", "rules/x.md": "x\n"})
        self.assertEqual(self._registered(), [])

    def test_the_collector_copies_what_a_rules_json_names(self):
        self._tree({".agents/rules.json": '{"entries": [{"path": "shared-rules"}]}',
                    "shared-rules/api.md": _RULE, "src/main.py": "print()\n"})
        out = self.tmp / "out"
        flags = paths.collect_declared_rules(self.repo, out, set())
        self.assertEqual(flags, {"gemini"})
        self.assertTrue((out / "shared-rules" / "api.md").is_file())
        self.assertFalse((out / "src").exists())

    def test_a_registered_rule_without_a_trigger_is_no_rule(self):
        """Antigravity CLI 1.4.2, run offline on 2026-10-19, counted no rule without a trigger
        that the rules page lists among what a plugin's rules.json registers, and `antigravity
        plugin validate` reported "no valid trigger in the frontmatter, so the rule never
        applies". Selecting every Markdown file of a registered directory, README.md among
        them, would give the repository the gemini flag for it."""
        rules = {"shared/rules/style.md": _RULE,
                 "shared/rules/README.md": "# Rules\n",
                 "shared/rules/camel.md": "---\ntrigger: alwaysOn\n---\nRule\n",
                 "shared/rules/plain.md": "Always do x.\n"}
        for registration in ({".agents/rules.json": '{"entries": [{"path": "shared/rules"}]}'},
                             {".agents/rules.json": '{"entries": [{"path": "shared/rules/plain.md"}]}'},
                             {".agents/plugins/p/plugin.json": '{"name": "p"}',
                              ".agents/plugins/p/rules.json": '{"entries": [{"path": "shared/rules"}]}'}):
            with self.subTest(registration=sorted(registration)):
                shutil.rmtree(self.repo, ignore_errors=True)
                self._tree({**registration, **rules})
                plain = "shared/rules/plain.md" in json.dumps(registration)
                self.assertEqual(self._registered(), [] if plain else ["shared/rules/style.md"])

    def test_a_registered_rule_without_a_trigger_is_kept_by_no_step(self):
        """The collector, the collected file cleanup and the detection step read a registered rule
        alike: a README.md beside a rule with a trigger is neither collected nor a rule row."""
        from agentcontext.steps import collected_file_cleanup, detection
        self._tree({".agents/rules.json": '{"entries": [{"path": "."}]}',
                    "README.md": "# Project\n", "style.md": _RULE})
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(self.repo, out, set())
        self.assertEqual(sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()),
                         [".agents/rules.json", "style.md"])
        # A README.md beside the registered rule in the collected tree, for the cleanup to remove.
        (out / "README.md").write_text("# Project\n")
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertFalse((out / "README.md").exists())
        self.assertEqual([r["rule"] for r in detection.detect_rules(out)],
                         [".agents/rules.json", "style.md"])


class TestAntigravityRegistrationCollection(unittest.TestCase):
    def test_registered_components_survive_collection_and_validation(self):
        from agentcontext.steps import detection, collected_file_cleanup
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "owner§repo"
            out = Path(temporary) / "collected"
            files = {
                ".agents/skills.json": '{"entries":[{"path":"shared/skills"}]}',
                ".agents/agents.json": '{"entries":[{"path":"shared/agents"}]}',
                ".agents/workflows.json": '{"entries":[{"path":"shared/workflows"}]}',
                ".agents/plugins.json": '{"entries":[{"path":"shared/plugins"}]}',
                "shared/skills/build/SKILL.md": "---\ndescription: Builds.\n---\nBuild instructions",
                "shared/skills/build/scripts/run.sh": "echo build",
                "shared/skills/nested/ignored/SKILL.md": "Too deep",
                "shared/agents/review.md": _AGENT,
                "shared/agents/build/agent.md": _AGENT,
                "shared/agents/nested/ignored.md": _AGENT,
                "shared/workflows/check.md": "Check instructions",
                "shared/plugins/test/plugin.json": '{"name":"test"}',
                "shared/plugins/test/rules/style.md": _RULE,
                "shared/plugins/test/rules.json": '{"entries":[{"path":"shared/extra-rules"}]}',
                "shared/extra-rules/review.md": _RULE,
            }
            for rel, text in files.items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            paths.collect_shared(root, out, set())
            paths.collect_declared_rules(root, out, set())
            paths.collect_plugin_parts(root, out, set())
            self.assertTrue((out / "shared/skills/build/scripts/run.sh").is_file())
            self.assertFalse((out / "shared/skills/nested/ignored/SKILL.md").exists())
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
            self.assertEqual({r["skills.md"] for r in detection.detect_skills(out)},
                             {".agents/skills.json", "shared/skills/build/SKILL.md"})
            self.assertEqual({r["subagent"] for r in detection.detect_subagents(out)},
                             {".agents/agents.json", "shared/agents/review.md", "shared/agents/build/agent.md"})
            self.assertEqual({r["command"] for r in detection.detect_commands(out)},
                             {".agents/workflows.json", "shared/workflows/check.md"})
            self.assertEqual(set(detection.detect_plugins(out)),
                             {".agents/plugins.json", "shared/plugins/test/plugin.json"})
            self.assertEqual({r["rule"] for r in detection.detect_rules(out)},
                             {"shared/plugins/test/rules/style.md", "shared/plugins/test/rules.json",
                              "shared/extra-rules/review.md"})
            self.assertTrue((out / "shared/skills/build/scripts/run.sh").is_file())


class TestContextFilesTheConfigurationNames(unittest.TestCase):
    """Gemini CLI reads the files that `context.fileName` names beside GEMINI.md, and an
    extension reads the file that its manifest names. Codex reads the file that
    `model_instructions_file` names and, where a directory has no AGENTS.md, the first fallback
    name there. Such a declaration can name a
    file that no path pattern copies, such as CONTEXT.md, CODEX.md or a lowercase gemini.md."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _declared(self, vendored=frozenset()) -> dict:
        return {rel: rule["match_mode"]
                for rel, rule in paths.declared_context_files(self.repo, vendored)}

    def test_a_gemini_settings_name_is_read_at_any_depth_and_in_either_form(self):
        """One name or a list of them, in a settings file with a comment, which Gemini CLI
        strips before it parses. A name matches a whole filename, not the end of one."""
        self._tree({".gemini/settings.json": '// team setup\n{"context": {"fileName": '
                                             '["CONTEXT.md", "RULES.md"]}}\n',
                    "CONTEXT.md": "x\n", "docs/RULES.md": "x\n", "docs/MY-CONTEXT.md": "x\n"})
        self.assertEqual(self._declared(), {"CONTEXT.md": "declared_names:context.fileName",
                                            "docs/RULES.md": "declared_names:context.fileName"})
        (self.repo / ".gemini" / "settings.json").write_text(
            '{"context": {"fileName": "CONTEXT.md"}}\n', encoding="utf-8")
        self.assertEqual(list(self._declared()), ["CONTEXT.md"])

    def test_the_flat_key_of_the_version_1_settings_names_nothing(self):
        """Gemini CLI removed the migration from the version 1 settings in January 2026, and a
        top-level `contextFileName` is a key that it no longer reads."""
        self._tree({".gemini/settings.json": '{"contextFileName": "CONTEXT.md"}\n',
                    "CONTEXT.md": "x\n"})
        self.assertEqual(self._declared(), {})

    def test_an_extension_names_a_file_beside_its_manifest(self):
        self._tree({"tools/ext/gemini-extension.json": '{"name": "e", "contextFileName": '
                                                       '"FIREBASE.md"}\n',
                    "tools/ext/FIREBASE.md": "x\n", "FIREBASE.md": "x\n"})
        self.assertEqual(list(self._declared()), ["tools/ext/FIREBASE.md"])

    def test_an_extension_names_a_list_of_context_files(self):
        self._tree({"tools/ext/gemini-extension.json": json.dumps({"contextFileName":
                    ["EXT.md", "docs/TOOLS.md", None, 1, "EXT.md"]}),
                    "tools/ext/EXT.md": "Extension context.\n",
                    "tools/ext/docs/TOOLS.md": "Tool instructions.\n"})
        self.assertEqual(list(self._declared()), ["tools/ext/EXT.md", "tools/ext/docs/TOOLS.md"])
        out = self.tmp / "collected"
        collect_repo(self.repo, out, set())
        self.assertEqual((out / "tools/ext/EXT.md").read_text(), "Extension context.\n")
        self.assertEqual((out / "tools/ext/docs/TOOLS.md").read_text(), "Tool instructions.\n")

    def test_an_extension_rejects_parent_references_even_inside_the_clone(self):
        for named in ("../../README.md", "docs/../EXT.md", "file..md", "%2e%2e.md",
                      "C:/EXT.md", "C:\\EXT.md", "\\EXT.md"):
            with self.subTest(named=named):
                self._tree({"tools/ext/gemini-extension.json": json.dumps({"contextFileName": named}),
                            "README.md": "Repository introduction.\n", "tools/ext/EXT.md": "x",
                            "tools/ext/file..md": "x", "tools/ext/%2e%2e.md": "x",
                            "tools/ext/C:/EXT.md": "x", "tools/ext/C:\\EXT.md": "x",
                            "tools/ext/\\EXT.md": "x"})
                self.assertEqual(self._declared(), {})

    def test_an_extension_keeps_whitespace_in_a_nonempty_filename(self):
        self._tree({"tools/ext/gemini-extension.json": json.dumps({"contextFileName": " EXT.md "}),
                    "tools/ext/ EXT.md ": "x", "tools/ext/EXT.md": "different file"})
        self.assertEqual(list(self._declared()), ["tools/ext/ EXT.md "])

    def test_the_codex_instructions_file_resolves_against_the_codex_folder(self):
        """The loader resolves a project config's paths against its `.codex` folder. `CODEX.md`
        is therefore `.codex/CODEX.md`, and `../AGENTS.md` the AGENTS.md at the root."""
        self._tree({".codex/config.toml": 'model_instructions_file = "CODEX.md"\n',
                    ".codex/CODEX.md": "x\n", "CODEX.md": "x\n"})
        self.assertEqual(self._declared(),
                         {".codex/CODEX.md": "declared_path:model_instructions_file"})
        (self.repo / ".codex" / "config.toml").write_text(
            'model_instructions_file = "../CODEX.md"\n', encoding="utf-8")
        self.assertEqual(list(self._declared()), ["CODEX.md"])

    def test_a_path_that_leads_out_of_the_repository_names_nothing(self):
        outside = self.tmp / "outside.md"
        outside.write_text("x\n", encoding="utf-8")
        for named in ("../../outside.md", str(outside), "~/outside.md"):
            with self.subTest(named=named):
                self._tree({".codex/config.toml": f'model_instructions_file = "{named}"\n'})
                self.assertEqual(self._declared(), {})

    def test_a_profile_is_not_a_project_setting(self):
        """Codex ignores `profiles` in a project config, and a key under one declares
        nothing."""
        self._tree({".codex/config.toml": '[profiles.team]\nmodel_instructions_file = '
                                          '"CODEX.md"\n', ".codex/CODEX.md": "x\n"})
        self.assertEqual(self._declared(), {})

    def test_a_fallback_name_is_read_where_no_agents_md_is_and_once_per_directory(self):
        """In each directory Codex tries AGENTS.override.md, AGENTS.md, then the fallback names,
        and includes at most one file."""
        self._tree({".codex/config.toml": 'project_doc_fallback_filenames = '
                                          '["TEAM_GUIDE.md", ".agents.md"]\n',
                    "TEAM_GUIDE.md": "x\n", "AGENTS.md": "x\n",
                    "web/TEAM_GUIDE.md": "x\n", "web/.agents.md": "x\n",
                    "api/.agents.md": "x\n",
                    "cli/TEAM_GUIDE.md": "x\n", "cli/AGENTS.override.md": "x\n"})
        self.assertEqual(list(self._declared()), ["api/.agents.md", "web/TEAM_GUIDE.md"])

    def test_a_fallback_entry_that_is_not_a_filename_is_ignored(self):
        """Codex's candidate_filenames skips an entry that contains a slash or a NUL, or is
        `.` or `..`, with the warning "ignoring project_doc_fallback_filenames entry that is
        not a filename" (codex-rs/core/src/agents_md.rs, openai/codex 6ae8b8aa7c60). Matched as a
        path suffix, `docs/TEAM.md` would name a file that Codex never reads."""
        self._tree({".codex/config.toml": 'project_doc_fallback_filenames = '
                                          '["docs/TEAM.md", "..", "TEAM.md"]\n',
                    "docs/TEAM.md": "x\n", "web/TEAM.md": "x\n"})
        self.assertEqual(list(self._declared()), ["docs/TEAM.md", "web/TEAM.md"])
        self._tree({".codex/config.toml": 'project_doc_fallback_filenames = ["docs/TEAM.md"]\n'})
        self.assertEqual(list(self._declared()), [])

    def test_a_file_in_a_vendored_directory_names_nothing(self):
        self._tree({".gemini/settings.json": '{"context": {"fileName": "CONTEXT.md"}}\n',
                    "node_modules/pkg/CONTEXT.md": "x\n", "CONTEXT.md": "x\n"})
        self.assertEqual(list(self._declared({"node_modules"})), ["CONTEXT.md"])

    def test_the_collector_copies_what_is_named_and_flags_the_tool(self):
        self._tree({".codex/config.toml": 'project_doc_fallback_filenames = ["TEAM.md"]\n',
                    "TEAM.md": "team\n", "OTHER.md": "other\n"})
        out = self.tmp / "out"
        flags = collect_repo(self.repo, out, set())
        self.assertEqual((out / "TEAM.md").read_text(encoding="utf-8"), "team\n")
        self.assertFalse((out / "OTHER.md").exists())
        self.assertIn("codex", flags)


class TestTheFilesThatACodexAgentRoleNames(unittest.TestCase):
    """Up to rust-v0.148.0, Codex read the `model_instructions_file` of an agent role as the base
    instructions of the subagent, relative to the folder of the role file. A role file is a TOML
    file below `.codex/agents` or the file that `config_file` of an `[agents.<name>]` table in
    `.codex/config.toml` names, relative to `.codex`, which is a subagent file of its own row."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def _declared(self) -> list:
        return [rel for rel, _rule in paths.declared_subagent_files(self.repo)]

    def test_a_role_names_its_instructions_relative_to_its_own_folder(self):
        """areal-project/AReaL's `.codex/agents/planner.toml` names `./planner.md` beside it."""
        self._tree({".codex/agents/planner.toml": 'name = "planner"\n'
                                                  'model_instructions_file = "./planner.md"\n',
                    ".codex/agents/planner.md": "Plan.\n",
                    ".codex/agents/review/deep.toml": 'model_instructions_file = "deep.md"\n',
                    ".codex/agents/review/deep.md": "Review.\n", "planner.md": "Other.\n"})
        self.assertEqual(self._declared(),
                         [".codex/agents/planner.md", ".codex/agents/review/deep.md"])
        self.assertEqual({rule["match_mode"] for _rel, rule in paths.declared_subagent_files(self.repo)},
                         {"declared_path:model_instructions_file"})

    def test_a_role_that_the_config_names_counts_with_its_instructions(self):
        """Each role file that `config_file` names is a file of the row of `.codex/config.toml`,
        also one below `.codex/agents`, which the row of the directory's TOML files reads as
        well. Its instructions are a file of the row of `.codex/agents`."""
        self._tree({".codex/config.toml": '[agents.reviewer]\ndescription = "Review"\n'
                                          'config_file = "../roles/reviewer.toml"\n'
                                          '[agents.planner]\nconfig_file = "agents/planner.toml"\n'
                                          '[agents.lister]\nconfig_file = ["../roles/a.toml"]\n',
                    "roles/reviewer.toml": 'model_instructions_file = "reviewer.md"\n',
                    "roles/reviewer.md": "Review.\n", "roles/a.toml": 'name = "a"\n',
                    ".codex/agents/planner.toml": 'name = "planner"\n'})
        self.assertEqual([(rel, rule["path"], rule["match_mode"])
                          for rel, rule in paths.declared_subagent_files(self.repo)],
                         [(".codex/agents/planner.toml", ".codex/config.toml",
                           "declared_path:agents.*.config_file"),
                          ("roles/reviewer.md", ".codex/agents",
                           "declared_path:model_instructions_file"),
                          ("roles/reviewer.toml", ".codex/config.toml",
                           "declared_path:agents.*.config_file")])

    def test_a_path_that_leads_out_of_the_repository_or_to_no_file_names_nothing(self):
        outside = self.tmp / "outside.md"
        outside.write_text("x\n", encoding="utf-8")
        for named in ("../../../outside.md", str(outside), "~/outside.md", "missing.md"):
            with self.subTest(named=named):
                self._tree({".codex/agents/a.toml": f'model_instructions_file = "{named}"\n'})
                self.assertEqual(self._declared(), [])

    def test_the_collector_copies_the_instructions_outside_the_codex_folder(self):
        self._tree({".codex/agents/planner.toml": 'model_instructions_file = "../../docs/planner.md"\n',
                    "docs/planner.md": "Plan.\n", "docs/other.md": "Other.\n"})
        out = self.tmp / "out"
        flags = collect_repo(self.repo, out, set())
        self.assertEqual((out / "docs/planner.md").read_text(encoding="utf-8"), "Plan.\n")
        self.assertFalse((out / "docs/other.md").exists())
        self.assertIn("codex", flags)


class TestThePromptFilesOfAnOpenCodeAgent(unittest.TestCase):
    """OpenCode replaces a `{file:path}` reference in a configuration file with the content of
    the file, relative to the folder of the configuration file. A reference in the `prompt` of an
    agent in the format of OpenCode 1, or in its `system` in the format of OpenCode 2, names a
    prompt file of the agent."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _tree(self, files: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def test_an_agent_names_its_prompt_relative_to_its_configuration(self):
        """cakmoel/Scriptlog's `opencode.json` names three prompt files in `.opencode/prompts`.
        A configuration in `.opencode` resolves a reference against that folder."""
        self._tree({"opencode.json": json.dumps({"agent": {
                        "playwright-test-generator": {
                            "mode": "subagent",
                            "prompt": "{file:.opencode/prompts/playwright-test-generator.md}"},
                        "build": {"prompt": "Intro. {file:./prompts/build.md}"}}}),
                    ".opencode/opencode.jsonc": '// review\n{"agents": {"reviewer": '
                                                '{"system": "{file:../docs/review.md}",},},}\n',
                    ".opencode/prompts/playwright-test-generator.md": "Generate tests.\n",
                    "prompts/build.md": "Build.\n", "docs/review.md": "Review.\n"})
        self.assertEqual(paths.opencode_prompt_files(self.repo),
                         [".opencode/prompts/playwright-test-generator.md", "prompts/build.md",
                          "docs/review.md"])

    def test_a_reference_outside_a_prompt_or_to_no_file_of_the_repository_names_nothing(self):
        """mattrossman/forecaswatch2 names `.secrets/project-ref` in the url of an MCP server,
        which is no prompt. A path in the home directory, an absolute path, a path out of the
        repository and a missing file name nothing either."""
        outside = self.tmp / "outside.md"
        outside.write_text("x\n", encoding="utf-8")
        prompts = ["{file:~/prompt.md}", "{file:%s}" % outside, "{file:../outside.md}",
                   "{file:missing.md}"]
        self._tree({"opencode.jsonc": json.dumps({
                        "mcp": {"supabase": {"type": "remote",
                                             "url": "https://x.test/mcp?ref={file:.secrets/ref}"}},
                        "agent": {f"a{i}": {"prompt": prompt} for i, prompt in enumerate(prompts)}}),
                    ".secrets/ref": "ref\n"})
        self.assertEqual(paths.opencode_prompt_files(self.repo), [])

    def test_the_collector_copies_a_prompt_file_outside_the_opencode_folder(self):
        self._tree({"opencode.json": json.dumps({"agent": {"docs": {
                        "prompt": "{file:./prompts/docs.md}"}}}),
                    "prompts/docs.md": "Write the docs.\n", "prompts/other.md": "Other.\n"})
        out = self.tmp / "out"
        flags = collect_repo(self.repo, out, set())
        self.assertEqual((out / "prompts/docs.md").read_text(encoding="utf-8"), "Write the docs.\n")
        self.assertFalse((out / "prompts/other.md").exists())
        self.assertIn("opencode", flags)


class TestTheCollectedRowReader(unittest.TestCase):
    """git accepts a comma in a branch name, and `build_csv_row` quotes the field that contains
    one. A bare `split(",")` on such a row would shift every column after it, and `head_sha`
    would come back with the tail of the branch name. The cloning step reads `head_sha` through
    this function before it reads the commit history, and the list of finished repositories
    slices on it. The shift would therefore reach the published CSV."""

    ROW = '"o/r","2026-03-29","case,with,commas","%s","2026-01-01T00:00:00Z"' % ("a" * 40)

    def test_a_branch_name_with_commas_does_not_shift_the_columns(self):
        self.assertEqual(paths.row_fields(self.ROW),
                         ["o/r", "2026-03-29", "case,with,commas", "a" * 40,
                          "2026-01-01T00:00:00Z"])

    def test_one_column_is_read_by_name(self):
        self.assertEqual(paths.row_column(self.ROW, "head_sha"), "a" * 40)
        self.assertEqual(paths.row_column(self.ROW, "branch"), "case,with,commas")

    def test_a_row_the_collector_built_reads_back(self):
        row = paths.build_csv_row("o/r", "2026-03-29", "case,with,commas", "b" * 40,
                                  "2026-01-01T00:00:00Z", "c" * 40, set())
        self.assertEqual(paths.row_column(row, "branch"), "case,with,commas")
        self.assertEqual(paths.row_column(row, "head_sha"), "b" * 40)


class TestTheWorkflowFilesKeptBeside(unittest.TestCase):
    """A workflow that runs an agent contains the prompt that it runs with, and no mechanism
    claims the path. The files are copied whole into a tree that no step reads, so that a
    person can read them after a run without a change to any published value."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "repo"
        self.out = self.tmp / "kept"

    def _workflow(self, name, text="on: push\n"):
        path = self.repo / ".github" / "workflows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_the_two_suffixes_are_copied_and_nothing_else_is(self):
        self._workflow("claude.yml")
        self._workflow("release.yaml")
        self._workflow("README.md")
        (self.repo / ".github" / "workflows" / "nested").mkdir()
        (self.repo / ".github" / "workflows" / "nested" / "deep.yml").write_text("x\n")
        self.assertEqual(paths.copy_workflow_files(self.repo, self.out), 2)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["claude.yml", "release.yaml"])

    def test_a_repository_without_workflows_leaves_no_directory(self):
        (self.repo / ".github").mkdir(parents=True)
        self.assertEqual(paths.copy_workflow_files(self.repo, self.out), 0)
        self.assertFalse(self.out.exists(), "a repository without workflows gets no directory")

    def test_a_symlink_is_not_followed(self):
        """It would point into the clone that the cloning step deletes, or outside the
        repository."""
        self._workflow("real.yml")
        link = self.repo / ".github" / "workflows" / "link.yml"
        link.symlink_to(self.tmp / "elsewhere.yml")
        self.assertEqual(paths.copy_workflow_files(self.repo, self.out), 1)

    def test_the_count_is_bounded(self):
        for n in range(paths.MAX_WORKFLOW_FILES + 5):
            self._workflow(f"w{n:03}.yml")
        self.assertEqual(paths.copy_workflow_files(self.repo, self.out),
                         paths.MAX_WORKFLOW_FILES)

    def test_a_file_over_the_size_bound_is_left(self):
        self._workflow("small.yml")
        self._workflow("huge.yml", "x" * (paths.MAX_WORKFLOW_BYTES + 1))
        self.assertEqual(paths.copy_workflow_files(self.repo, self.out), 1)
        self.assertEqual([p.name for p in self.out.iterdir()], ["small.yml"])


class TestTheAgentWorkflowsCollected(unittest.TestCase):
    """`.github` is copied only when a Copilot path is already there. Without this pass, a
    workflow that runs an agent in a repository with no Copilot configuration would reach no
    tree."""

    CLAUDE = "jobs:\n  go:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
    CODEX = "jobs:\n  go:\n    steps:\n      - uses: openai/codex-action@v1\n"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo, self.out = self.tmp / "owner§repo", self.tmp / "out"
        (self.repo / ".github" / "workflows").mkdir(parents=True)

    def _workflow(self, name, text):
        (self.repo / ".github" / "workflows" / name).write_text(text, encoding="utf-8")

    def test_a_renamed_workflow_is_copied_and_flags_its_tool(self):
        self._workflow("nightly-agent.yml", self.CLAUDE)
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude"})
        self.assertTrue((self.out / ".github" / "workflows" / "nightly-agent.yml").is_file())

    def test_two_tools_in_one_repository_flag_both(self):
        self._workflow("a.yml", self.CLAUDE)
        self._workflow("b.yml", self.CODEX)
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude", "codex"})

    def test_ordinary_ci_is_neither_copied_nor_flagged(self):
        self._workflow("ci.yml", "jobs:\n  go:\n    steps:\n      - run: make\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), set())
        self.assertFalse((self.out / ".github").exists(), "no .github for a repository with none")

    def test_naming_the_action_without_running_it_is_not_a_match(self):
        self._workflow("ci.yml", "# we use anthropics/claude-code-action elsewhere\n"
                                 "jobs:\n  go:\n    steps:\n      - run: make\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), set())

    def _prompt(self, rel, text="Review the change.\n"):
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.repo / rel).write_text(text, encoding="utf-8")

    def test_the_prompt_file_of_the_codex_action_is_copied_with_the_workflow(self):
        """The Codex action reads the prompt from the file that its `prompt-file` input names,
        "Consider storing prompts in .github/codex/prompts/" (learn.chatgpt.com/docs/github-action),
        and the prompt is what the agent runs with."""
        self._prompt(".github/codex/prompts/review.md")
        self._workflow("review.yml",
                       "jobs:\n  codex:\n    steps:\n      - uses: actions/checkout@v5\n"
                       "      - name: Run Codex\n        id: run_codex\n"
                       "        uses: openai/codex-action@v1\n        with:\n"
                       "          openai-api-key: ${{ secrets.OPENAI_API_KEY }}\n"
                       "          prompt-file: .github/codex/prompts/review.md\n"
                       "          output-file: codex-output.md\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"codex"})
        self.assertEqual((self.out / ".github/codex/prompts/review.md").read_text(encoding="utf-8"),
                         "Review the change.\n")

    def test_which_prompt_files_an_action_reads(self):
        """A path relative to the checkout, also after `${{ github.workspace }}/`, and no path
        that another expression builds, that is absolute, that leaves the repository or that
        names no file of it. A `prompt-file` of a step that runs another action is not the
        Codex action's."""
        for rel in ("agents/prompts/labels.md", ".github/codex/second.md", "docs/other.md"):
            self._prompt(rel)
        self._workflow("labels.yml",
                       "jobs:\n  a:\n    steps:\n      - uses: openai/codex-action@v1\n        with:\n"
                       "          prompt-file: \"${{ github.workspace }}/agents/prompts/labels.md\"\n"
                       "  b:\n    steps:\n      - uses: openai/codex-action@main\n        with:\n"
                       "          prompt-file: '${{ runner.temp }}/generated.md'\n"
                       "      - uses: openai/codex-action@v1\n        with:\n"
                       "          prompt-file: /tmp/prompt.md\n"
                       "      - uses: openai/codex-action@v1\n        with:\n"
                       "          prompt-file: ../outside.md\n"
                       "      - uses: openai/codex-action@v1\n        with:\n"
                       "          prompt-file: .github/codex/missing.md\n"
                       "      - uses: some/other-action@v1\n        with:\n"
                       "          prompt-file: docs/other.md\n"
                       "      - with:\n          prompt-file: .github/codex/second.md  # second\n"
                       "        uses: openai/codex-action@v1\n")
        self.assertEqual(paths.action_prompt_files(self.repo),
                         ["agents/prompts/labels.md", ".github/codex/second.md"])

    def test_the_prompt_file_of_the_claude_code_base_action_is_copied_with_the_workflow(self):
        """The Claude Code Base Action reads the prompt from the file that its `prompt_file`
        input names, as in facebook/rocksdb's ai-review-analysis.yml, `uses:
        anthropics/claude-code-base-action@beta` with `prompt_file:
        claude_md/ci_recovery_prompt.md`. The folder `base-action` of the Claude Code Action
        has the same input."""
        self._prompt("claude_md/ci_recovery_prompt.md")
        self._prompt(".github/prompts/triage.md")
        self._workflow("ai-review-analysis.yml",
                       "jobs:\n  recover:\n    steps:\n"
                       "      - name: Run Claude recovery\n"
                       "        uses: anthropics/claude-code-base-action@beta\n        with:\n"
                       "          prompt_file: claude_md/ci_recovery_prompt.md\n"
                       "          allowed_tools: \"Bash(git:*),View\"\n"
                       "      - uses: anthropics/claude-code-base-action@beta\n        with:\n"
                       "          prompt_file: /tmp/prompt.txt\n")
        self._workflow("triage.yml",
                       "jobs:\n  triage:\n    steps:\n"
                       "      - uses: anthropics/claude-code-action/base-action@v1\n"
                       "        with:\n          prompt_file: .github/prompts/triage.md\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude"})
        self.assertEqual(paths.action_prompt_files(self.repo),
                         ["claude_md/ci_recovery_prompt.md", ".github/prompts/triage.md"])
        self.assertEqual((self.out / "claude_md/ci_recovery_prompt.md").read_text(encoding="utf-8"),
                         "Review the change.\n")
        self.assertTrue((self.out / ".github/prompts/triage.md").is_file())

    def test_an_input_that_the_action_does_not_have_names_no_prompt_file(self):
        """The Claude Code Action has `prompt` and no `prompt_file`, and the Codex action calls
        its input `prompt-file`, not `prompt_file`."""
        for rel in ("prompts/claude.md", "prompts/codex.md"):
            self._prompt(rel)
        self._workflow("claude.yml",
                       "jobs:\n  a:\n    steps:\n      - uses: anthropics/claude-code-action@v1\n"
                       "        with:\n          prompt_file: prompts/claude.md\n"
                       "      - uses: openai/codex-action@v1\n"
                       "        with:\n          prompt_file: prompts/codex.md\n")
        self.assertEqual(paths.action_prompt_files(self.repo), [])

    def test_a_step_that_names_the_action_through_a_yaml_anchor(self):
        """supabase/cli's ai-review.yml names the action with an anchor in one step,
        `uses: &codex-action-pin openai/codex-action@52fe01ec...`, and with its alias,
        `uses: *codex-action-pin`, in another. An alias of an anchor on another action names
        no prompt file of the Codex action."""
        for rel in (".github/ai-review/review.md", ".github/ai-review/adjudicate.md",
                    ".github/ai-review/other.md"):
            self._prompt(rel)
        self._workflow("ai-review.yml",
                       "jobs:\n  review:\n    steps:\n"
                       "      - uses: &checkout actions/checkout@v5\n"
                       "      - name: Run Codex independent review\n"
                       "        uses: &codex-action-pin openai/codex-action@52fe01ec # v1.11\n"
                       "        with:\n          prompt-file: .github/ai-review/review.md\n"
                       "  adjudicate:\n    steps:\n      - uses: *checkout\n        with:\n"
                       "          prompt-file: .github/ai-review/other.md\n"
                       "      - name: Run Codex adjudication\n        uses: *codex-action-pin\n"
                       "        with:\n          prompt-file: .github/ai-review/adjudicate.md\n")
        self.assertEqual(paths.action_prompt_files(self.repo),
                         [".github/ai-review/review.md", ".github/ai-review/adjudicate.md"])

    def test_an_action_named_in_a_comment_of_another_agent_s_workflow_flags_nothing(self):
        """The tools are read off the `uses:` line that selected the file, not off any mention,
        and a Claude workflow that names the Codex action in a comment does not flag
        Codex."""
        self._workflow("agent.yml", "# ported from openai/codex-action\n" + self.CLAUDE)
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude"})

    def test_an_agentic_workflow_is_copied_under_its_engine_and_a_readme_is_not(self):
        """GitHub Agentic Workflows keeps a workflow as Markdown with `on:` in its front
        matter, and Copilot runs it where `engine:` names no other. The Markdown is copied
        under its engine, because the `.lock.yml` beside it runs no documented action."""
        self._workflow("triage.md", "---\non:\n  issues:\n    types: [opened]\n---\nLabel it.\n")
        self._workflow("review.md", "---\non: pull_request\nengine: claude\n---\nReview it.\n")
        self._workflow("README.md", "# Workflows\n\nWhat each workflow does.\n")
        self._workflow("pi.md", "---\non: push\nengine: pi\n---\nAn engine outside the six.\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"copilot", "claude"})
        kept = sorted(p.name for p in (self.out / ".github" / "workflows").iterdir())
        self.assertEqual(kept, ["review.md", "triage.md"])

    def test_the_engine_is_read_in_each_form_the_reference_gives(self):
        read = paths.agentic_workflow_engine
        self.assertEqual(read("---\non: push\n---\nx\n"), "copilot")
        self.assertEqual(read("---\n\"on\": push\nengine: 'Codex'  # the model\n---\n"), "codex")
        self.assertEqual(read("---\non: push\nengine:\n  id: claude\n  model: x\n---\n"), "claude")
        self.assertEqual(read("---\non: push\nengine: {id: gemini, version: 1}\n---\n"), "gemini")
        self.assertEqual(read("---\non: push\nengine:\n  runtime:\n    id: codex\n---\n"), "codex")
        # Preferences alone select no engine, and the default holds.
        self.assertEqual(read("---\non: push\nengine:\n  model: gpt-5\n  env:\n    id: x\n---\n"),
                         "copilot")
        # A shared component that a workflow imports has no trigger, and a README no front matter.
        self.assertIsNone(read("---\nengine: claude\ntools: {}\n---\nShared.\n"))
        self.assertIsNone(read("# Workflows\n---\non: push\n---\n"))
        self.assertIsNone(read("---\non: push\n"))

    def test_the_engine_can_come_from_an_imported_file(self):
        """gh-aw's compiler takes "the first real included engine specification" when a workflow
        names no engine. pydantic/pydantic-ai imports `engine: {id: claude}` from
        `shared/engine-zai.md`, and its workflows are Claude's, not Copilot's,
        gh-aw's default."""
        files = {
            ".github/workflows/shared/engine.md": "---\nengine:\n  id: claude\n  model: x\n---\n",
            ".github/workflows/shared/tools.md": "---\ntools:\n  bash: true\nimports:\n  - nested.md\n---\n",
            ".github/workflows/shared/nested.md": "---\nengine: codex\n---\n",
            ".github/agents/helper.md": "---\nname: helper\n---\n",
            ".github/workflows/shared/model.md": "---\nengine:\n  model: gpt-5\n---\n",
            # gh-aw fetches owner/repo/x.md from GitHub, whatever this repository contains there.
            ".github/workflows/owner/repo/x.md": "---\nengine: claude\n---\n",
        }
        read = files.get
        engine = paths.agentic_workflow
        self.assertEqual(engine("---\non: push\nimports:\n  - shared/engine.md\n---\n", read),
                         ("claude", [".github/workflows/shared/engine.md"]))
        # An import's own import without a `/` is read from the importing file's directory,
        # and the mapping form names its path with `uses:`, whatever `with:` contains.
        self.assertEqual(engine("---\non: push\nimports:\n  - uses: shared/tools.md\n    with:\n"
                                "      path: elsewhere.md\n---\n", read),
                         ("codex", [".github/workflows/shared/tools.md",
                                    ".github/workflows/shared/nested.md"]))
        self.assertEqual(engine("---\non: push\nimports: [../agents/helper.md, shared/model.md]\n---\n",
                                read),
                         ("copilot", [".github/agents/helper.md", ".github/workflows/shared/model.md"]))
        self.assertEqual(engine("---\non: push\nimports:\n  aw:\n    - /.github/workflows/shared/engine.md"
                                "#Engine\n  apm:\n    - acme/pkg\n---\n", read)[0], "claude")
        # The workflow's own engine takes precedence, and an optional import may be missing.
        self.assertEqual(engine("---\non: push\nengine: gemini\nimports:\n  - shared/engine.md\n"
                                "  - shared/gone.md?\n---\n", read)[0], "gemini")
        self.assertEqual(engine("---\non: push\nimports:\n  - shared/gone.md?\n---\n", read)[0],
                         "copilot")
        # Where the repository's files cannot decide the engine, there is none: a required
        # import that the repository does not contain, a remote import, an import outside
        # .github and .agents, or two engines, which gh-aw refuses to compile.
        for imports in ("shared/gone.md", "acme/workflows/shared/engine.md@v1", "owner/repo/x.md",
                        "../../src/x.md",
                        "[shared/engine.md, shared/tools.md]"):
            with self.subTest(imports=imports):
                text = (f"---\non: push\nimports: {imports}\n---\n" if imports.startswith("[")
                        else f"---\non: push\nimports:\n  - {imports}\n---\n")
                self.assertIsNone(engine(text, read)[0])

    def test_the_files_that_the_body_loads_when_the_workflow_runs_are_imported(self):
        """gh-aw replaces each runtime import of the body, `{{#runtime-import path}}` or the
        deprecated `{{#import path}}`, with the text of the file when the workflow runs, after it
        removes the front matter and the HTML comments, and it does so inside a fenced code block
        as well (`processRuntimeImports` in `runtime_import.cjs`). A path under `.github/` or
        `.agents/` is read from the repository root, and any other path from
        `.github/workflows/`, such as the `shared/issue-taxonomy.md` that opsmill/infrahub's
        workflows load. shopware/shopware's workflows each load a policy from `.github/aw/`,
        which loads a second file from `.github/aw/shared/` in the same way. A file that the body
        loads selects no engine, because gh-aw removes its front matter."""
        files = {
            ".github/workflows/shared/tools.md":
                "---\ntools: {}\n---\n{{#runtime-import? .github/aw/optional.md}}\n",
            ".github/aw/policy.md":
                "---\nengine: claude\n---\nThe policy.\n{{#runtime-import .github/aw/shared/policy.md}}\n",
            ".github/aw/shared/policy.md": "Label every issue.\n",
            ".github/workflows/shared/taxonomy.md": "The labels.\n",
            ".github/aw/deprecated.md": "The deprecated form.\n",
            ".agents/prompts/review.md": "From .agents.\n",
            ".github/aw/range.md": "one\ntwo\n",
            ".github/aw/fenced.md": "In a fence.\n",
            ".github/aw/optional.md": "Optional.\n",
            ".github/aw/commented.md": "In a comment.\n",
            ".github/aw/spanned.md": "In a code span.\n",
            "README.md": "Outside .github.\n",
        }
        text = ("---\non: issues\nimports:\n  - shared/tools.md\n---\n# Triage\n\n"
                "{{#runtime-import .github/aw/policy.md}}\n"
                "{{#runtime-import shared/taxonomy.md}}\n"
                "{{#import: .github/aw/deprecated.md}}\n"
                "{{#runtime-import /.agents/prompts/review.md}}\n"
                "{{#runtime-import .github/aw/range.md:1-2}}\n"
                "```\n{{#runtime-import .github/aw/fenced.md}}\n```\n"
                "<!-- {{#runtime-import .github/aw/commented.md}} -->\n"
                "`{{#import .github/aw/spanned.md}}`\n"
                "{{#runtime-import ../../README.md}}\n"
                "{{#runtime-import https://example.com/standards.md}}\n"
                "{{#runtime-import .github/aw/gone.md}}\n")
        self.assertEqual(paths.agentic_workflow(text, files.get),
                         ("copilot", [".github/workflows/shared/tools.md",
                                      ".github/aw/policy.md",
                                      ".github/workflows/shared/taxonomy.md",
                                      ".github/aw/deprecated.md",
                                      ".agents/prompts/review.md",
                                      ".github/aw/range.md",
                                      ".github/aw/fenced.md",
                                      ".github/aw/optional.md",
                                      ".github/aw/shared/policy.md"]))
        # Without a reader, nothing is read, and the engine is the workflow's own.
        self.assertEqual(paths.agentic_workflow(text.replace("on: issues", "on: issues\nengine: claude")),
                         ("claude", []))

    def test_the_files_that_an_agentic_workflow_loads_when_it_runs_are_copied_with_it(self):
        self._workflow("triage.md", "---\non: issues\nengine: claude\n---\n"
                                    "{{#runtime-import .github/aw/policy.md}}\n")
        (self.repo / ".github" / "aw").mkdir()
        (self.repo / ".github" / "aw" / "policy.md").write_text("The policy.\n", encoding="utf-8")
        (self.repo / ".github" / "aw" / "unused.md").write_text("Unused.\n", encoding="utf-8")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude"})
        kept = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(kept, [".github/aw/policy.md", ".github/workflows/triage.md"])

    def test_an_engine_command_that_runs_another_program_is_no_tool_s(self):
        """`engine.command` replaces the engine's program, and gh-aw then skips installing the
        engine. pydantic/pydantic-ai imports `engine: {id: claude, command:
        /tmp/gh-aw/bin/pydantic-ai-runner-launch}`, which runs pydantic-ai's own agent
        runner. Claude Code does not run."""
        files = {".github/workflows/shared/zai.md":
                 "---\nengine:\n  id: claude\n  command: /tmp/gh-aw/bin/pydantic-ai-runner-launch\n---\n"}
        engine = paths.agentic_workflow_engine
        self.assertIsNone(engine("---\non: push\nimports:\n  - shared/zai.md\n---\n", files.get))
        self.assertIsNone(engine("---\non: push\nengine:\n  id: codex\n  command: \"python run.py\"\n---\n"))
        self.assertIsNone(engine("---\non: push\nengine: {id: gemini, command: ./wrapper.sh}\n---\n"))
        # A build of the engine's own program is still the engine, as in the reference's example.
        self.assertEqual(engine("---\non: push\nengine:\n  id: copilot\n"
                                "  command: /usr/local/bin/copilot-dev\n---\n"), "copilot")
        self.assertEqual(engine("---\non: push\nengine:\n  id: cursor\n  command: cursor-agent\n---\n"),
                         "cursor")
        self._workflow("review.md", "---\non: pull_request\nimports:\n  - shared/zai.md\n---\nReview.\n")
        (self.repo / ".github" / "workflows" / "shared").mkdir()
        self._workflow("shared/zai.md", files[".github/workflows/shared/zai.md"])
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), set())
        self.assertFalse((self.out / ".github").exists())

    def test_an_agentic_workflow_s_imports_are_copied_with_it(self):
        self._workflow("review.md", "---\non: pull_request\nimports:\n  - shared/engine.md\n---\nReview.\n")
        (self.repo / ".github" / "workflows" / "shared").mkdir()
        self._workflow("shared/engine.md", "---\nengine: claude\n---\n")
        self._workflow("shared/unused.md", "---\ntools: {}\n---\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude"})
        kept = sorted(p.relative_to(self.out).as_posix() for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(kept, [".github/workflows/review.md", ".github/workflows/shared/engine.md"])

    def test_a_workflow_that_runs_an_agent_s_command_line_tool_is_copied_under_that_tool(self):
        """The vendors document running each tool without a person at it, `claude -p` or
        `codex exec`, and a workflow that does so in a shell step is that tool's configuration,
        as a workflow that runs its action is. A compiled `.lock.yml` runs the engine's command
        itself and is left out, because the Markdown that it was compiled from is the
        configuration."""
        self._workflow("fix.yml", "jobs:\n  go:\n    steps:\n      - run: |\n"
                                  "          claude -p \"$(cat .github/prompts/fix.md)\"\n")
        self._workflow("release.yml", "jobs:\n  go:\n    steps:\n      - run: |\n"
                                      "          codex --config x=true exec --full-auto\n")
        self._workflow("triage.lock.yml", "jobs:\n  go:\n    steps:\n      - run: claude -p x\n")
        self.assertEqual(paths.collect_agent_workflows(self.repo, self.out), {"claude", "codex"})
        kept = sorted(p.name for p in (self.out / ".github" / "workflows").iterdir())
        self.assertEqual(kept, ["fix.yml", "release.yml"])

    def test_commands_alone_find_a_workflow(self):
        """`agent_workflow_files` finds a workflow when no action and no engine is given, so that
        heuristics with only `workflow_command:` rows still find one."""
        self._workflow("fix.yml", "jobs:\n  go:\n    steps:\n      - run: claude -p x\n")
        found = paths.agent_workflow_files(self.repo, {}, {}, {"claude": ["claude -p"]})
        self.assertEqual([p.name for p in found], ["fix.yml"])

    def test_the_command_has_to_stand_where_a_shell_command_starts(self):
        """Lines copied from workflows of the collection started on 2026-09-24, and lines that
        name a command without running it."""
        runs = paths.runs_command
        for text, invocation in (
                ('          cat /tmp/p.txt | claude --print \\\n', "claude --print"),
                ('          if ! npx @anthropic-ai/claude-code --print \\\n',
                 "@anthropic-ai/claude-code --print"),
                ('          npx -y @anthropic-ai/claude-code@latest --print "x"\n',
                 "@anthropic-ai/claude-code --print"),
                ('          ANALYSIS=$(claude -p "$(cat /tmp/p)" --model x)\n', "claude -p"),
                ('          copilot --agent refresh --prompt "Refresh the pins."\n',
                 "copilot --prompt"),
                ('          curl https://cursor.com/install -fsSL -o "$installer"\n',
                 "curl https://cursor.com/install"),
                ('          make build && claude -p "fix"\n', "claude -p"),
                ('          set -e; codex exec --full-auto\n', "codex exec"),
                ('          (gemini -p "x")\n', "gemini -p"),
                ('          out=`copilot -p x`\n', "copilot -p"),
                ('      - run: gemini -p "Review"\n', "gemini -p"),
                # After a reserved word of the shell or `timeout`, in the collection of
                # October 5-6: RayforceDB/rayforce, Azure/LogicAppsUX, dotnet/runtime's
                # validate-pat-pool.yml and nationalsecurityagency/datawave.
                ('          timeout 12m claude -p "$(cat audit-output/p.md)" \\\n', "claude -p"),
                ('          timeout 600 copilot --yolo -p "$TASK" --model "$MODEL"\n', "copilot -p"),
                ('          set +e; timeout 30 copilot --prompt "Say OK" --silent; rc=$?\n',
                 "copilot --prompt"),
                ('          set +e; output=$(timeout 30 copilot --prompt "Say OK" 2>&1)\n',
                 "copilot --prompt"),
                ('          if ! gemini --debug --yolo --prompt "/pr-code-review" | tee x\n',
                 "gemini --prompt")):
            with self.subTest(text=text):
                self.assertTrue(runs(text, invocation))
        for text, invocation in (
                ("  # `claude -p` on the runner\n", "claude -p"),
                ('          echo "run claude -p to review"\n', "claude -p"),
                ("          claude-review -p x\n", "claude -p"),
                ("          make && claude-review -p x\n", "claude -p"),
                ("          claude --print-config\n", "claude --print"),
                ("          codex --version\n", "codex exec"),
                ('          agent -p "x"\n', "curl https://cursor.com/install"),
                ("          curl https://cursor.com/installer\n", "curl https://cursor.com/install"),
                ('          echo "if ! claude -p works, retry"\n', "claude -p"),
                ("    timeout-minutes: 30 claude -p\n", "claude -p")):
            with self.subTest(text=text):
                self.assertFalse(runs(text, invocation))

    def test_the_uses_line_is_what_counts(self):
        self.assertTrue(paths.uses_action("  - uses: openai/codex-action@v1\n",
                                          "openai/codex-action"))
        self.assertTrue(paths.uses_action("      uses: 'openai/codex-action'\n",
                                          "openai/codex-action"))
        self.assertTrue(paths.uses_action("        uses: &codex-action-pin openai/codex-action@52fe01ec"
                                          " # v1.11\n", "openai/codex-action"))
        self.assertFalse(paths.uses_action("# openai/codex-action\n", "openai/codex-action"))
        self.assertFalse(paths.uses_action("  with:\n    x: openai/codex-action\n",
                                           "openai/codex-action"))
        # A step that is commented out does not run. The anchor at the start of the line
        # distinguishes it from a live step. Without the anchor, this line would match.
        self.assertFalse(paths.uses_action("      # - uses: openai/codex-action@v1\n",
                                           "openai/codex-action"))


class TestAPlaceOutsideTheClone(unittest.TestCase):
    """A plugin's part, the folders beside its root skill and a context file that a
    configuration names are read inside the repository only. `docs/steps/repository_cloning.md`:
    "A target outside the repository is never copied". Git stores a symlink as it was committed,
    and a repository can therefore contain `assets -> /home` or `agents -> ../../..`. A part
    followed there would have its files copied, kept by the cleanup and published as the
    repository's own."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "clones" / "owner§repo"
        self.out = self.tmp / "collected" / "owner§repo"
        self.outside = self.tmp / "outside"
        (self.outside / "a").mkdir(parents=True)
        (self.outside / "id_rsa").write_text("PRIVATE KEY\n", encoding="utf-8")
        (self.outside / "hooks.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
        (self.outside / "notes.md").write_text("PRIVATE NOTES\n", encoding="utf-8")
        (self.outside / "a" / "SKILL.md").write_text("PRIVATE SKILL\n", encoding="utf-8")

    def _tree(self, files: dict, links: dict) -> None:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")
        for rel, target in links.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, self.repo / rel)

    def _collected(self) -> dict:
        collect_repo(self.repo, self.out, set())
        return {p.relative_to(self.out).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                for p in sorted(self.out.rglob("*")) if p.is_file()}

    def test_a_folder_beside_a_root_skill_that_leads_out_is_not_read(self):
        self._tree({".claude-plugin/plugin.json": '{"name": "p"}', "SKILL.md": "A skill.\n"},
                   {"assets": str(self.outside)})
        self.assertEqual([(rel, kind) for rel, _, kind in paths.plugin_components(self.repo)],
                         [("SKILL.md", "root_skill")])
        collected = self._collected()
        self.assertEqual([rel for rel, text in collected.items() if "PRIVATE" in text], [])
        self.assertNotIn("assets/id_rsa", collected)

    def test_a_default_part_and_a_declared_one_that_lead_out_are_not_read(self):
        # `agents` leads out of the clone relative to itself, and `hooks` and the declared
        # skills folder point at an absolute path.
        self._tree({"plugins/p/.claude-plugin/plugin.json": '{"name": "p", "skills": ["./ext"]}',
                    "plugins/p/commands/deploy.md": "Deploy.\n"},
                   {"plugins/p/agents": "../../../../outside",
                    "plugins/p/hooks": str(self.outside),
                    "plugins/p/ext": str(self.outside)})
        self.assertEqual((self.repo / "plugins/p/agents").resolve(), self.outside.resolve())
        self.assertEqual(paths.plugin_components(self.repo), [])
        collected = self._collected()
        self.assertEqual([rel for rel, text in collected.items() if "PRIVATE" in text], [])
        self.assertNotIn("plugins/p/commands/deploy.md", collected)

    def test_a_context_file_named_through_a_link_that_leads_out_is_not_read(self):
        self._tree({".codex/config.toml": 'model_instructions_file = "shared/notes.md"\n'},
                   {".codex/shared": str(self.outside)})
        self.assertEqual(paths.declared_context_files(self.repo), [])
        collected = self._collected()
        self.assertEqual([rel for rel, text in collected.items() if "PRIVATE" in text], [])

    def test_a_link_that_stays_inside_is_still_read(self):
        """The same parts through a link to a folder of the repository: Claude Code and Copilot
        CLI read what the link points at, and so does the collection."""
        self._tree({"plugins/p/.claude-plugin/plugin.json": '{"name": "p"}',
                    "shared/agents/reviewer.md": "Review.\n",
                    ".codex/config.toml": 'model_instructions_file = "shared/notes.md"\n',
                    "docs/notes.md": "Notes.\n"},
                   {"plugins/p/agents": "../../shared/agents", ".codex/shared": "../docs"})
        self.assertEqual([(rel, kind) for rel, _, kind in paths.plugin_components(self.repo)],
                         [("plugins/p/agents", "default")])
        self.assertEqual([rel for rel, _ in paths.declared_context_files(self.repo)],
                         [".codex/shared/notes.md"])
        collected = self._collected()
        self.assertEqual(collected.get("plugins/p/agents/reviewer.md"), "Review.\n")
        self.assertEqual(collected.get(".codex/shared/notes.md"), "Notes.\n")


class TestThePluginsShippedAnywhere(unittest.TestCase):
    """A plugin is a directory with a manifest, and a repository can put it in any directory,
    such as at the root, under `plugins/<name>/` or beside a package. The collector copies the
    skills, agents and hooks that a plugin contains as well as the manifest directories and the
    files beside them, so that no plugin root publishes its manifest alone."""

    CLAUDE = '{"name": "demo"}\n'

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo, self.out = self.tmp / "owner§repo", self.tmp / "out"
        self.repo.mkdir()

    def _write(self, files):
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text, encoding="utf-8")

    def test_a_portable_schema_in_a_vendor_manifest_is_not_a_portable_plugin(self):
        schema = json.dumps({"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                             "name": "p"})
        self._write({".claude-plugin/plugin.json": schema,
                     ".claude-plugin/skills/example/SKILL.md": "x",
                     "plugins/c/.plugin/plugin.json": schema})
        roots = paths.plugin_roots(self.repo)
        self.assertEqual(roots["<agent-plugin>"], [])
        self.assertEqual(roots["<claude-plugin>"], [""])
        self.assertEqual(roots["<copilot-plugin>"], ["plugins/c"])
        self.assertEqual(paths.plugin_parts(self.repo), [])

    def test_a_root_is_the_directory_that_contains_each_kind_of_manifest(self):
        self._write({".claude-plugin/plugin.json": self.CLAUDE,
                     "plugins/a/.claude-plugin/plugin.json": self.CLAUDE,
                     "plugins/a/.codex-plugin/plugin.json": self.CLAUDE,
                     "plugins/b/.github/plugin/plugin.json": self.CLAUDE,
                     "plugins/c/.plugin/plugin.json": self.CLAUDE,
                     "tools/ext/gemini-extension.json": self.CLAUDE,
                     "plugins/d/plugin.json": MANIFEST,
                     # A plugin.json with no Agent Plugins schema is a package's, and the one
                     # inside a manifest directory is that vendor's manifest.
                     "packages/e/plugin.json": self.CLAUDE,
                     "node_modules/f/.claude-plugin/plugin.json": self.CLAUDE})
        roots = paths.plugin_roots(self.repo, {"node_modules"})
        self.assertEqual(roots["<claude-plugin>"], ["", "plugins/a"])
        self.assertEqual(roots["<codex-plugin>"], ["plugins/a"])
        self.assertEqual(roots["<copilot-plugin>"], ["plugins/b", "plugins/c"])
        self.assertEqual(roots["<gemini-extension>"], ["tools/ext"])
        self.assertEqual(roots["<agent-plugin>"], ["plugins/d"])
        self.assertEqual(paths.plugin_manifests(self.repo, {"node_modules"}), [
            ".claude-plugin/plugin.json", "plugins/a/.claude-plugin/plugin.json",
            "plugins/a/.codex-plugin/plugin.json", "plugins/b/.github/plugin/plugin.json",
            "plugins/c/.plugin/plugin.json", "plugins/d/plugin.json",
            "tools/ext/gemini-extension.json"])

    def test_an_antigravity_plugin_is_a_folder_of_agents_plugins(self):
        """Antigravity loads a workspace's plugins from `.agents/plugins/<name>/`, each a folder
        with a plugin.json, as in opendatahub-io/notebooks. A plugin.json directly in
        `.agents/plugins`, in another project's `.agents` or beside a package is none of them."""
        self._write({".agents/plugins/pr-review/plugin.json": '{"name": "pr-review"}\n',
                     ".agents/plugins/pr-review/skills/review/SKILL.md": "Review.\n",
                     ".agents/plugins/plugin.json": '{"name": "x"}\n',
                     "tools/.agents/plugins/y/plugin.json": '{"name": "y"}\n',
                     "plugins/z/plugin.json": '{"name": "z"}\n'})
        roots = paths.plugin_roots(self.repo)
        self.assertEqual(roots["<antigravity-plugin>"], [".agents/plugins/pr-review"])
        self.assertEqual(roots["<agent-plugin>"], [])
        self.assertEqual(paths.plugin_manifests(self.repo),
                         [".agents/plugins/pr-review/plugin.json"])
        self.assertEqual([(rel, rule["tool"]) for rel, rule in paths.plugin_parts(self.repo)],
                         [(".agents/plugins/pr-review/plugin.json", "gemini"),
                          (".agents/plugins/pr-review/skills", "gemini")])

    def test_the_parts_are_copied_and_flag_the_plugin_s_tool(self):
        self._write({"plugins/a/.claude-plugin/plugin.json": self.CLAUDE,
                     "plugins/a/skills/deploy/SKILL.md": "Deploy.\n",
                     "plugins/a/agents/reviewer.md": "Review.\n",
                     "plugins/a/src/index.ts": "export {};\n",
                     "plugins/b/.github/plugin/plugin.json": self.CLAUDE,
                     "plugins/b/agents/linter.md": "Lint.\n",
                     "plugins/d/plugin.json": MANIFEST,
                     "plugins/d/skills/x/SKILL.md": "X.\n"})
        flags = paths.collect_plugin_parts(self.repo, self.out, set())
        # The portable layout names no tool, and its parts flag none.
        self.assertEqual(flags, {"claude", "copilot"})
        copied = sorted(p.relative_to(self.out).as_posix()
                        for p in self.out.rglob("*") if p.is_file())
        self.assertEqual(copied, [
            "plugins/a/.claude-plugin/plugin.json", "plugins/a/agents/reviewer.md",
            "plugins/a/skills/deploy/SKILL.md", "plugins/b/.github/plugin/plugin.json",
            "plugins/b/agents/linter.md", "plugins/d/plugin.json",
            "plugins/d/skills/x/SKILL.md"])

    def test_a_part_the_plugin_does_not_have_is_not_listed(self):
        self._write({"plugins/a/.claude-plugin/plugin.json": self.CLAUDE,
                     "plugins/a/skills/deploy/SKILL.md": "Deploy.\n"})
        self.assertEqual([rel for rel, _rule in paths.plugin_parts(self.repo)],
                         ["plugins/a/skills"])

    def test_a_skills_directory_with_no_manifest_beside_it_is_nobody_s_plugin(self):
        self._write({"plugins/a/skills/deploy/SKILL.md": "Deploy.\n",
                     "plugins/a/package.json": "{}\n"})
        self.assertEqual(paths.plugin_parts(self.repo), [])
        self.assertEqual(paths.collect_plugin_parts(self.repo, self.out, set()), set())
        self.assertFalse(self.out.exists())


class TestTheFilesOfATree(unittest.TestCase):
    """`tree_files`, the listing of a repository's directory that every comparison with a
    per-repository commit record reads. Synology DSM writes `@eaDir` into a directory that File
    Station visits on the NAS, and Finder writes `.DS_Store` over SMB."""

    def setUp(self):
        self.tree = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tree, ignore_errors=True)

    def _write(self, rel: str) -> None:
        (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.tree / rel).write_text("x\n")

    def test_what_a_file_manager_wrote_is_left_out(self):
        for rel in ("CLAUDE.md", ".claude/skills/a/SKILL.md", "@eaDir/CLAUDE.md@SynoEAStream",
                    ".claude/skills/a/@eaDir/SKILL.md@SynoEAStream/SYNO_EA", ".cursor/.DS_Store"):
            self._write(rel)
        self.assertEqual(paths.tree_files(self.tree), [".claude/skills/a/SKILL.md", "CLAUDE.md"])

    def test_a_file_or_a_link_with_the_directory_s_name_is_listed(self):
        """DSM writes a directory, and a file named `@eaDir` and a link of that name, broken or
        not, are files that the collector copied."""
        self._write("docs/@eaDir")
        os.symlink("nowhere", self.tree / "@eaDir")
        self.assertEqual(paths.tree_files(self.tree), ["@eaDir", "docs/@eaDir"])


class TestARemovalThatDsmWritesInto(unittest.TestCase):
    """`remove_tree`, which every step uses to remove a directory of a tree on the NAS. DSM
    writes an `@eaDir` into a directory at any time, and an `@eaDir` written after
    `shutil.rmtree` listed the directory would make the removal raise ENOTEMPTY and the step
    exit 1."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.tree = self.root / "o§a"
        for rel in ("CLAUDE.md", ".claude/skills/a/SKILL.md", ".claude/settings.json"):
            (self.tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.tree / rel).write_text("x\n")

    def test_an_eadir_written_into_a_directory_during_the_removal_is_removed_with_it(self):
        for trigger in ("SKILL.md", "CLAUDE.md"):
            with self.subTest(written_beside=trigger):
                self.setUp()
                with dsm_writes_during_removal(trigger) as written:
                    paths.remove_tree(self.tree)
                self.assertEqual(len(written), 1)
                self.assertFalse(self.tree.exists())

    def test_what_dsm_writes_into_an_eadir_that_is_being_removed_is_removed_with_it(self):
        """The collected file cleanup removes each `@eaDir` of the validated tree, and DSM
        writes its files into such a directory, not a directory of its own."""
        eadir = self.tree / "@eaDir"
        eadir.mkdir()
        (eadir / "CLAUDE.md@SynoEAStream").write_text("x\n")
        with dsm_writes_during_removal("CLAUDE.md@SynoEAStream",
                                       entry="SKILL.md@SynoEAStream") as written:
            paths.remove_tree(eadir)
        self.assertEqual(len(written), 1)
        self.assertFalse(eadir.exists())
        self.assertTrue((self.tree / "CLAUDE.md").is_file())

    def test_dsm_writing_again_at_each_try_is_removed_while_the_tries_last(self):
        with mock.patch.object(paths, "REMOVAL_WAIT", 0), \
                dsm_writes_during_removal(times=paths.REMOVAL_TRIES):
            paths.remove_tree(self.tree)
        self.assertFalse(self.tree.exists())

    def test_another_entry_written_during_the_removal_raises_and_stays(self):
        for entry in ("notes.md", "docs/@eaDir/x"):
            with self.subTest(entry=entry):
                self.setUp()
                with dsm_writes_during_removal("SKILL.md", entry=entry), \
                        self.assertRaises(OSError) as caught:
                    paths.remove_tree(self.tree)
                self.assertIn(caught.exception.errno, (errno.ENOTEMPTY, errno.EEXIST))
                self.assertTrue((self.tree / ".claude" / "skills" / "a" / entry).exists())

    def test_a_link_named_eadir_written_during_the_removal_raises(self):
        real_unlink = os.unlink

        def links_after_the_skill(path, *, dir_fd=None):
            real_unlink(path, dir_fd=dir_fd)
            if os.path.basename(os.fsdecode(path)) == "SKILL.md":
                os.symlink("elsewhere", self.tree / ".claude" / "skills" / "a" / "@eaDir")

        with mock.patch("os.unlink", links_after_the_skill), self.assertRaises(OSError):
            paths.remove_tree(self.tree)
        self.assertTrue((self.tree / ".claude" / "skills" / "a" / "@eaDir").is_symlink())

    def test_an_absent_directory_counts_as_removed(self):
        paths.remove_tree(self.root / "absent")


def _run_with_recursion_limit(code: str, limit: int, *args: str,
                              timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run `code` in a new interpreter that imports this checkout's package, with the
    recursion limit set to `limit` after the imports at the top of `code`, which it marks with
    a line `# limit`."""
    code = code.replace("# limit", f"sys.setrecursionlimit({limit})")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(
        filter(None, [str(root), os.environ.get("PYTHONPATH")]))}
    return subprocess.run([sys.executable, "-c", code, *args], env=env, capture_output=True,
                          text=True, timeout=timeout)


class TestTheCopyOfACollectedTree(unittest.TestCase):
    """`copy_tree`, which copies a tree of the collection for the collected file cleanup, the
    archive of the collected files and the dropped repositories of `unmatched paths`."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.src, self.dst = self.root / "o§a", self.root / "copy" / "o§a"

    def _write(self, rel: str, text: str = "x\n") -> Path:
        path = self.src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_links_stay_links_and_times_and_modes_are_kept(self):
        self._write("CLAUDE.md", "# a\n")
        self._write(".claude/skills/a/SKILL.md")
        (self.src / ".claude" / "empty").mkdir()
        os.symlink("../CLAUDE.md", self.src / ".claude" / "linked.md")
        os.symlink("skills", self.src / ".claude" / "commands")
        os.symlink("nowhere", self.src / ".claude" / "broken")
        os.chmod(self.src / "CLAUDE.md", 0o640)
        for path in (self.src / ".claude" / "skills" / "a", self.src / "CLAUDE.md"):
            os.utime(path, (1_000_000_000, 1_000_000_000))
        paths.copy_tree(self.src, self.dst)
        self.assertEqual((self.dst / "CLAUDE.md").read_text(), "# a\n")
        self.assertTrue((self.dst / ".claude" / "empty").is_dir())
        for name, target in (("linked.md", "../CLAUDE.md"), ("commands", "skills"),
                             ("broken", "nowhere")):
            self.assertTrue((self.dst / ".claude" / name).is_symlink(), name)
            self.assertEqual(os.readlink(self.dst / ".claude" / name), target)
        for rel in ("CLAUDE.md", ".claude/skills/a", ".claude", "."):
            source, copy = os.stat(self.src / rel), os.stat(self.dst / rel)
            self.assertEqual((copy.st_mode, copy.st_mtime_ns),
                             (source.st_mode, source.st_mtime_ns), rel)

    def test_a_directory_without_write_permission_is_copied(self):
        """The mode is set after the contents, as `copytree` sets it, so that a read-only
        directory of the source does not deny the writes into its copy."""
        self._write(".claude/rules/a.md")
        os.chmod(self.src / ".claude" / "rules", 0o555)
        self.addCleanup(os.chmod, self.src / ".claude" / "rules", 0o755)
        self.addCleanup(lambda: (self.dst / ".claude" / "rules").exists()
                        and os.chmod(self.dst / ".claude" / "rules", 0o755))
        paths.copy_tree(self.src, self.dst)
        self.assertTrue((self.dst / ".claude" / "rules" / "a.md").is_file())
        self.assertEqual(os.stat(self.dst / ".claude" / "rules").st_mode & 0o777, 0o555)

    def test_what_dsm_writes_is_left_out_and_a_file_or_link_of_the_name_is_copied(self):
        self._write("CLAUDE.md")
        self._write("@eaDir/CLAUDE.md@SynoEAStream")
        self._write(".claude/@eaDir/settings.json@SynoEAStream/SYNO_EA")
        self._write("docs/@eaDir", "a file of the repository\n")
        (self.src / ".cursor").mkdir()
        os.symlink("../docs", self.src / ".cursor" / "@eaDir")
        paths.copy_tree(self.src, self.dst)
        self.assertFalse((self.dst / "@eaDir").exists())
        self.assertFalse((self.dst / ".claude" / "@eaDir").exists())
        self.assertEqual((self.dst / "docs" / "@eaDir").read_text(), "a file of the repository\n")
        self.assertTrue((self.dst / ".cursor" / "@eaDir").is_symlink())

    def test_an_eadir_that_dsm_removed_after_the_listing_is_left_out(self):
        """A name that a listing returned and that is gone now is a directory that DSM
        removed, and nothing of it is copied."""
        self.src.mkdir()
        self.assertEqual(paths.file_manager_directories(str(self.src), ["@eaDir", "gone.md"]),
                         {"@eaDir"})

    def test_an_existing_destination_is_refused(self):
        self._write("CLAUDE.md")
        self.dst.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            paths.copy_tree(self.src, self.dst)

    def test_a_tree_deeper_than_the_recursion_limit_is_copied(self):
        """rsync copies a configuration directory whatever its depth, and `copytree` calls
        itself once per level. A directory nested deeper than the recursion limit would
        raise RecursionError in `copytree`. A lower limit keeps the tree within the
        path length of macOS."""
        deep = ".claude/" + "/".join(["a"] * 150) + "/notes.md"
        self._write(deep, "deep\n")
        code = ("import sys\n"
                "from agentcontext import paths\n"
                "# limit\n"
                "paths.copy_tree(sys.argv[1], sys.argv[2])\n")
        proc = _run_with_recursion_limit(code, 120, str(self.src), str(self.dst))
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual((self.dst / deep).read_text(), "deep\n")


class TestARootFileThatIsADirectory(unittest.TestCase):
    """The collector copies `.mcp.json`, `.vscode/mcp.json` and a bare `marketplace.json` from
    the root of a clone by their paths. A directory with one of these names would raise
    IsADirectoryError and make the repository a `collector_error` in every round and every
    collection. A directory with the name is no configuration file of that path."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.repo, self.out = self.root / "o§a", self.root / "out" / "o§a"
        (self.repo / "CLAUDE.md").parent.mkdir(parents=True)
        (self.repo / "CLAUDE.md").write_text("# a\n")

    def test_each_is_left_out_and_the_repository_is_collected(self):
        for rel in (".mcp.json", ".vscode/mcp.json", "marketplace.json"):
            with self.subTest(path=rel):
                shutil.rmtree(self.out, ignore_errors=True)
                (self.repo / rel / "x").mkdir(parents=True)
                (self.repo / rel / "x" / "y.json").write_text("{}\n")
                flags = collect_repo(self.repo, self.out, set())
                self.assertEqual(flags, {"claude", "claude.md"})
                self.assertFalse((self.out / rel).exists())
                self.assertTrue((self.out / "CLAUDE.md").is_file())
                shutil.rmtree(self.repo / rel.split("/")[0])

    def test_a_file_and_a_link_are_still_copied(self):
        (self.repo / ".vscode").mkdir()
        (self.repo / ".vscode" / "mcp.json").write_text('{"servers": {}}\n')
        os.symlink("CLAUDE.md", self.repo / ".mcp.json")
        flags = collect_repo(self.repo, self.out, set())
        self.assertIn("copilot", flags)
        self.assertEqual((self.out / ".vscode" / "mcp.json").read_text(), '{"servers": {}}\n')
        self.assertEqual((self.out / ".mcp.json").read_text(), "CLAUDE.md\n")


class TestTheItemsThatARegistrationNames(unittest.TestCase):
    """`include_only` and `exclude` of an Antigravity registration entry name items rather
    than give patterns. Antigravity CLI 1.4.2, run offline over registrations, selected an item
    by its path below the entry's directory and left one out by its own name or a longer end of
    its path, each with or without `.md`, and read no value as a regular expression or a glob."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _agents(self, entry: dict, files: list) -> list:
        """The agents that `.agents/agents.json` with this entry over `shared/` selects."""
        shutil.rmtree(self.repo, ignore_errors=True)
        (self.repo / ".agents").mkdir(parents=True)
        (self.repo / ".agents" / "agents.json").write_text(
            json.dumps({"entries": [{"path": "shared", **entry}]}))
        for rel in files:
            (self.repo / "shared" / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / "shared" / rel).write_text("---\nname: a\ndescription: d\n---\n")
        rows = [r for r in paths._REGISTRATION_ROWS
                if r["path"] == ".agents/agents.json"]
        return [rel.removeprefix("shared/")
                for rel, _ in paths.registered_configuration_files(self.repo, heuristic_rows=rows)]

    def test_a_folder_agent_is_its_agent_md_in_any_case(self):
        """Antigravity CLI 1.4.9 takes a folder of a scanned directory as an agent where the
        folder has a file named agent.md in any letter case: `findMarkerInDir` looks the file up
        with `customizations.findFileCaseInsensitive`, which compares each name with
        `strings.EqualFold`."""
        self.assertEqual(self._agents({}, ["alpha.md", "upper/AGENT.md", "lower/agent.md"]),
                         ["alpha.md", "lower/agent.md", "upper/AGENT.md"])

    def test_include_only_selects_an_item_by_its_path(self):
        files = ["alpha.md", "beta.md", "cat/deep.md", "cat/folder/agent.md"]
        for names, selected in (
                (["alpha"], ["alpha.md"]), (["alpha.md"], ["alpha.md"]),
                (["cat/deep"], ["cat/deep.md"]), (["cat/folder"], ["cat/folder/agent.md"]),
                (["cat/folder/"], ["cat/folder/agent.md"]),
                (["cat/folder/agent.md"], ["cat/folder/agent.md"]),
                (["al.*"], []), (["al*"], []), (["^alpha$"], []), (["lph"], []), (["cat"], []),
                (["cat/*"], []), ([" alpha "], []), (["ALPHA"], [])):
            with self.subTest(include_only=names):
                self.assertEqual(self._agents({"include_only": names}, files), selected)

    def test_exclude_leaves_out_an_item_by_its_path_or_its_name(self):
        files = ["alpha.md", "experimental-x/agent.md", "cat/deep/agent.md", "cat/other.md"]
        nested = {"include_only": ["cat/deep", "cat/other"]}
        for entry, selected in (
                ({"exclude": ["alpha"]}, ["experimental-x/agent.md"]),
                ({"exclude": ["alpha.md"]}, ["experimental-x/agent.md"]),
                ({"exclude": ["experimental-x"]}, ["alpha.md"]),
                ({"exclude": ["experimental-x/"]}, ["alpha.md"]),
                ({"exclude": ["experimental-.*"]}, ["alpha.md", "experimental-x/agent.md"]),
                ({"exclude": ["experimental-*"]}, ["alpha.md", "experimental-x/agent.md"]),
                ({"exclude": ["xperimental"]}, ["alpha.md", "experimental-x/agent.md"]),
                ({"exclude": ["ALPHA"]}, ["alpha.md", "experimental-x/agent.md"]),
                ({**nested, "exclude": ["deep"]}, ["cat/other.md"]),
                ({**nested, "exclude": ["cat/deep"]}, ["cat/other.md"]),
                ({**nested, "exclude": ["other"]}, ["cat/deep/agent.md"]),
                ({**nested, "exclude": ["cat"]}, ["cat/deep/agent.md", "cat/other.md"])):
            with self.subTest(**{k: str(v) for k, v in entry.items()}):
                self.assertEqual(self._agents(entry, files), selected)

    def test_a_backtracking_pattern_from_a_repository_cannot_stall_the_steps(self):
        """Read as a regular expression with Python's backtracking `re`, `^(a+)+$` takes time
        exponential in the length of a directory name such as this one, and a collector worker
        has no time limit. The collector, the cleanup and detection read the same
        registration."""
        skill = "a" * 40 + "!"
        files = {".agents/skills.json": json.dumps({"entries": [
                     {"path": "shared/skills", "include_only": ["^(a+)+$"]},
                     {"path": "shared/skills", "exclude": ["^(a+)+$"]}]}),
                 f"shared/skills/{skill}/SKILL.md": "---\nname: s\ndescription: d\n---\n",
                 "CLAUDE.md": "# r\n"}
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        validated = self.tmp / "validated" / "owner§repo"
        shutil.copytree(self.repo, validated)
        code = ("import json, sys\n"
                "from pathlib import Path\n"
                "from agentcontext import paths\n"
                "from agentcontext.steps import collected_file_cleanup, detection\n"
                "# limit\n"
                "repo, out, validated = (Path(a) for a in sys.argv[1:4])\n"
                "paths.collect_repo(repo, out, set())\n"
                "collected_file_cleanup.scan_and_clean_repo(str(validated), [])\n"
                "print(json.dumps([sorted(p.relative_to(out).as_posix() for p in out.rglob('*')"
                " if p.is_file()), [r['skills.md'] for r in detection.detect_skills(validated)]]))\n")
        proc = _run_with_recursion_limit(code, 1000, str(self.repo), str(self.tmp / "out"),
                                         str(validated), timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        collected, detected = json.loads(proc.stdout)
        # The second entry reads the skill directly in the directory, and `^(a+)+$` names no
        # item and leaves out nothing.
        self.assertEqual(collected, [".agents/skills.json", "CLAUDE.md",
                                     f"shared/skills/{skill}/SKILL.md"])
        self.assertEqual(detected, [".agents/skills.json", f"shared/skills/{skill}/SKILL.md"])


# An Antigravity agent and an Antigravity rule with the front matter that Antigravity requires of
# each wherever it reads them.
_AGENT = "---\nname: a\ndescription: d\n---\nbody\n"
_RULE = "---\ntrigger: always_on\n---\nRule instructions\n"


class _RegistrationTree(unittest.TestCase):
    """A repository written from a dict: a dict value as JSON, any other value as text."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"

    def _write(self, files: dict) -> Path:
        shutil.rmtree(self.repo, ignore_errors=True)
        self.repo.mkdir(parents=True)
        for rel, content in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content) if isinstance(content, dict) else content)
        return self.repo

    def _selected(self, files: dict) -> list:
        """The items that the registration files of `files` select."""
        return [rel for rel, _row in paths.registered_configuration_files(self._write(files))]


class TestAnEntryThatNamesAnItem(_RegistrationTree):
    """Antigravity CLI 1.4.2, run offline over trusted workspaces that are git repositories, loaded
    an item that an entry names itself: an agent file, with or without `.md`, a folder with an
    agent.md, a plugin's directory or its manifest, and, under `antigravity plugin validate`, a
    skill's folder or its SKILL.md (the section "Configuration artifact detection" of
    docs/heuristics.md). A step that selected nothing for such an entry would miss
    google/meridian's five skills, which its `.agents/skills.json` names one by one."""

    def test_a_skill_folder_or_its_skill_md(self):
        self.assertEqual(self._selected({
            ".agents/skills.json": {"entries": [{"path": "skills/a"}, {"path": "one/SKILL.md"},
                                                {"path": "two"}]},
            "skills/a/SKILL.md": "a", "skills/b/SKILL.md": "not named",
            "one/SKILL.md": "one", "two/SKILL.md": "two",
            "two/nested/SKILL.md": "below a skill, which the CLI does not scan"}),
            ["one/SKILL.md", "skills/a/SKILL.md", "two/SKILL.md"])

    def test_an_agent_file_with_or_without_md(self):
        self.assertEqual(self._selected({
            ".agents/agents.json": {"entries": [{"path": "shared/beta.md"}, {"path": "shared/delta"},
                                                {"path": "shared/both"}]},
            "shared/beta.md": _AGENT, "shared/gamma.md": _AGENT, "shared/delta.md": _AGENT,
            "shared/both.md": _AGENT, "shared/both/inner.md": _AGENT}),
            ["shared/beta.md", "shared/both/inner.md", "shared/delta.md"])

    def test_a_folder_with_an_agent_md_gives_its_markdown_files_and_no_folder_below(self):
        """The CLI listed agent.md and other.md of the folder, and not sub/agent.md, whether the
        entry names the folder or the folder is the entry's directory itself."""
        self.assertEqual(self._selected({
            ".agents/agents.json": {"entries": [{"path": "shared/alpha"}, {"path": "solo"}]},
            "shared/alpha/agent.md": _AGENT, "shared/alpha/other.md": _AGENT,
            "shared/alpha/sub/agent.md": _AGENT, "shared/gamma.md": _AGENT,
            "solo/agent.md": _AGENT, "solo/x.md": _AGENT, "solo/sub/agent.md": _AGENT}),
            ["shared/alpha/agent.md", "shared/alpha/other.md", "solo/agent.md", "solo/x.md"])

    def test_a_plugin_by_its_directory_or_its_manifest(self):
        root = self._write({
            ".agents/plugins.json": {"entries": [{"path": "shared/alpha"},
                                                 {"path": "kept/gamma/plugin.json"},
                                                 {"path": "folded/delta"}]},
            "shared/alpha/plugin.json": {"name": "alpha"}, "shared/beta/plugin.json": {"name": "beta"},
            "kept/gamma/plugin.json": {"name": "gamma"},
            "folded/delta/.antigravity-plugin/plugin.json": {"name": "delta"}})
        self.assertEqual(paths.plugin_roots(root)["<antigravity-plugin>"],
                         ["folded/delta", "kept/gamma", "shared/alpha"])

    def test_the_filters_of_such_an_entry_name_the_item_by_the_end_of_its_path(self):
        """`include_only` and `exclude` of an entry that names an item compare as `exclude`
        compares any item: by its name or a longer end of its path, with or without `.md`."""
        self.assertEqual(self._selected({
            ".agents/agents.json": {"entries": [
                {"path": "shared/e1.md", "include_only": ["e1"]},
                {"path": "shared/e2.md", "include_only": ["shared/e2.md"]},
                {"path": "shared/e3.md", "include_only": ["x/e3"]},
                {"path": "shared/e4.md", "include_only": ["E4"]},
                {"path": "shared/f1.md", "exclude": ["shared/f1"]},
                {"path": "shared/f2.md", "exclude": ["x/f2"]},
                {"path": "shared/f3.md", "exclude": ["f3.md"]}]},
            **{f"shared/{name}.md": _AGENT for name in ("e1", "e2", "e3", "e4", "f1", "f2", "f3")},
            ".agents/skills.json": {"entries": [{"path": "more/a", "exclude": ["a"]},
                                                {"path": "more/b", "include_only": ["b"]}]},
            "more/a/SKILL.md": "a", "more/b/SKILL.md": "b"}),
            ["more/b/SKILL.md", "shared/e1.md", "shared/e2.md", "shared/f2.md"])

    def test_the_named_skills_survive_collection_and_validation(self):
        """The collector copies each named skill folder with its support files, the cleanup
        keeps it outside every configuration directory, and detection lists it."""
        from agentcontext.steps import collected_file_cleanup, detection
        root = self._write({".agents/skills.json": {"entries": [{"path": "skills/meridian_a"}]},
                            "skills/meridian_a/SKILL.md": "---\nname: a\ndescription: d\n---\n",
                            "skills/meridian_a/scripts/run.py": "print()\n",
                            "src/main.py": "print()\n"})
        out = self.tmp / "collected"
        paths.collect_declared_rules(root, out, set())
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertTrue((out / "skills/meridian_a/scripts/run.py").is_file())
        self.assertFalse((out / "src").exists())
        self.assertEqual([r["skills.md"] for r in detection.detect_skills(out)],
                         [".agents/skills.json", "skills/meridian_a/SKILL.md"])


class TestWhereARegistrationResolves(_RegistrationTree):
    """Antigravity CLI 1.4.2's guide: "Relative `path` entries resolve against the directory
    containing the dot-dir that declares them, falling back to the project root when nothing
    exists there." The CLI resolved a plugin's registration files against the plugin's root,
    and a file inherited from a chain against the directory of the file that starts the chain.
    A step that resolved every path against the workspace root would read other files."""

    def test_a_plugin_s_entries_resolve_against_its_root(self):
        plugin = {".agents/plugins/p/plugin.json": {"name": "p"},
                  ".agents/plugins/p/agents.json": {"entries": [{"path": "extra"}]}}
        for files, selected in (
                ({"extra/x.md": _AGENT, ".agents/plugins/p/extra/y.md": _AGENT},
                 [".agents/plugins/p/extra/y.md"]),
                ({"extra/x.md": _AGENT}, ["extra/x.md"]),
                ({".agents/plugins/p/extra.md": _AGENT, "extra.md": _AGENT},
                 [".agents/plugins/p/extra.md"]),
                ({".agents/plugins/p/extra.md": _AGENT, "extra/x.md": _AGENT}, ["extra/x.md"]),
                ({"extra.md": _AGENT}, [])):
            with self.subTest(files=sorted(files)):
                self.assertEqual(self._selected({**plugin, **files}), selected)

    def test_a_plugin_s_skills_resolve_against_its_root(self):
        self.assertEqual(self._selected({
            ".agents/plugins/p/plugin.json": {"name": "p"},
            ".agents/plugins/p/skills.json": {"entries": [{"path": "extra"}]},
            "extra/rootskill/SKILL.md": "x", ".agents/plugins/p/extra/pluginskill/SKILL.md": "x"}),
            [".agents/plugins/p/extra/pluginskill/SKILL.md"])

    def test_a_nested_registration_resolves_against_its_directory(self):
        self.assertEqual(self._selected({
            "sub/.agents/agents.json": {"entries": [{"path": "shared"}, {"path": "sub/other"}]},
            "shared/rootshared.md": _AGENT, "sub/shared/subshared.md": _AGENT,
            "sub/other/subother.md": _AGENT}),
            ["sub/other/subother.md", "sub/shared/subshared.md"])

    def test_an_inherited_file_resolves_against_the_directory_of_the_chain(self):
        """At the root, `team.json` is the root's and not the one beside the registration file.
        Below `sub/`, an inherited file and its entries resolve against `sub/` first, wherever
        the inherited file is found."""
        for files, selected in (
                ({".agents/agents.json": {"inherits": [{"path": "team.json"}]},
                  ".agents/team.json": {"entries": [{"path": "fromdot"}]},
                  "team.json": {"entries": [{"path": "fromroot"}]},
                  "fromdot/a1.md": _AGENT, "fromroot/a2.md": _AGENT}, ["fromroot/a2.md"]),
                ({"sub/.agents/agents.json": {"inherits": [{"path": "team.json"}]},
                  "sub/team.json": {"entries": [{"path": "y"}]},
                  "team.json": {"entries": [{"path": "z"}]},
                  "sub/y/a.md": _AGENT, "sub/z/c.md": _AGENT, "y/d.md": _AGENT, "z/b.md": _AGENT},
                 ["sub/y/a.md"]),
                ({"sub/.agents/agents.json": {"inherits": [{"path": "team.json"}]},
                  "team.json": {"entries": [{"path": "z"}]},
                  "sub/z/c.md": _AGENT, "z/b.md": _AGENT}, ["sub/z/c.md"]),
                ({".agents/plugins/p/plugin.json": {"name": "p"},
                  ".agents/plugins/p/agents.json": {"inherits": [{"path": "more.json"}]},
                  "more.json": {"entries": [{"path": "x"}]},
                  ".agents/plugins/p/x/a.md": _AGENT, "x/b.md": _AGENT},
                 [".agents/plugins/p/x/a.md"])):
            with self.subTest(files=sorted(files)):
                self.assertEqual(self._selected(files), selected)

    def test_a_dot_at_the_workspace_root_is_the_root(self):
        """CLI 1.4.2 listed `x.md` and not `sub/y.md` for `.` or `./` at the workspace root,
        with `agent.md` beside `x.md` both, and with a plugin at the workspace root that names
        `.` in its skills.json it counted the root skill alone, or the two skill folders
        directly in the root where it has none. A step that resolved `.` at the root to
        nothing would miss these items."""
        for files, selected in (
                ({".agents/agents.json": {"entries": [{"path": "."}]},
                  "x.md": _AGENT, "sub/y.md": _AGENT}, ["x.md"]),
                ({".agents/agents.json": {"entries": [{"path": "./"}]},
                  "x.md": _AGENT, "sub/y.md": _AGENT}, ["x.md"]),
                ({".agents/agents.json": {"entries": [{"path": "."}]},
                  "agent.md": _AGENT, "x.md": _AGENT, "sub/agent.md": _AGENT},
                 ["agent.md", "x.md"]),
                ({".agents/plugins.json": {"entries": [{"path": "."}]},
                  "plugin.json": {"name": "rootplugin"}, "skills.json": {"entries": [{"path": "."}]},
                  "SKILL.md": "s", "more/a/SKILL.md": "a"}, ["SKILL.md", "plugin.json"]),
                ({".agents/plugins.json": {"entries": [{"path": "."}]},
                  "plugin.json": {"name": "rootplugin"}, "skills.json": {"entries": [{"path": "."}]},
                  "a/SKILL.md": "a", "b/SKILL.md": "b", "more/c/SKILL.md": "c"},
                 ["a/SKILL.md", "b/SKILL.md", "plugin.json"])):
            with self.subTest(files=sorted(files)):
                self.assertEqual(self._selected(files), selected)


class TestTheSkillsThatAnAntigravityManifestLists(_RegistrationTree):
    """The `skills` key of an Antigravity plugin's manifest, as Antigravity CLI 1.4.9 reads it (the
    section "The places that a plugin's manifest names" of docs/heuristics.md). The CLI decodes a
    string or a list of strings, trims each entry and its trailing `/`, and drops an empty one. It
    resolves the entries as the entries of the plugin's skills.json, with the plugin root as the
    base, and reads skills.json and `skills/` beside them. `agy plugin validate` names the forms of
    an entry: "list a SKILL.md, a folder with one, or a folder of such folders". A step that read no
    key would leave out the plugin's listed skills, and a step that read the listed folders as
    Claude Code's skills directories, at any depth, would add skills that the CLI does not load."""

    PLUGIN = ".agents/plugins/p"

    def _parts(self, files: dict) -> list:
        """(place, kind) for each skill place of the Antigravity plugins that `files` makes."""
        return sorted((rel, kind) for rel, row, kind in paths.plugin_components(self._write(files))
                      if row["path"] == "<antigravity-plugin>/skills")

    def _plugin(self, skills, **files) -> dict:
        manifest = {"name": "p"} if skills is None else {"name": "p", "skills": skills}
        return {f"{self.PLUGIN}/plugin.json": manifest,
                **{f"{self.PLUGIN}/{rel}": text for rel, text in files.items()}}

    def test_a_string_or_a_list_adds_to_skills(self):
        files = {"skills/a/SKILL.md": "a", "extra/b/SKILL.md": "b", "more/c/SKILL.md": "c"}
        default = (f"{self.PLUGIN}/skills", "default")
        for skills, listed in (
                (None, []),
                ("./extra", [f"{self.PLUGIN}/extra/b"]),
                (["./extra/", "more"], [f"{self.PLUGIN}/extra/b", f"{self.PLUGIN}/more/c"])):
            with self.subTest(skills=skills):
                self.assertEqual(self._parts(self._plugin(skills, **files)),
                                 sorted([default, *((rel, "declared") for rel in listed)]))

    def test_an_entry_is_a_skill_its_skill_md_or_a_folder_of_skills(self):
        """A folder with a SKILL.md is that skill and no skill below it, and another folder gives
        the skill folders directly in it, as an entry of a registration file does."""
        files = self._plugin(["one/SKILL.md", "two", "group"],
                             **{"one/SKILL.md": "1", "two/SKILL.md": "2",
                                "two/nested/SKILL.md": "below a skill",
                                "group/x/SKILL.md": "x", "group/y/deeper/SKILL.md": "too deep"})
        self.assertEqual(self._parts(files), [(f"{self.PLUGIN}/group/x", "declared"),
                                              (f"{self.PLUGIN}/one", "declared"),
                                              (f"{self.PLUGIN}/two", "declared")])

    def test_an_entry_resolves_against_the_plugin_root_and_then_the_workspace_root(self):
        for files, listed in (
                ({"shared/s/SKILL.md": "workspace", f"{self.PLUGIN}/shared/s/SKILL.md": "plugin"},
                 f"{self.PLUGIN}/shared/s"),
                ({"shared/s/SKILL.md": "workspace"}, "shared/s")):
            with self.subTest(files=sorted(files)):
                self.assertEqual(self._parts({**self._plugin("shared"), **files}),
                                 [(listed, "declared")])

    def test_white_space_a_trailing_slash_and_empty_entries(self):
        files = {"extra/b/SKILL.md": "b"}
        for skills, parts in (([" ./extra/ ", "", "  ", None, "/"],
                               [(f"{self.PLUGIN}/extra/b", "declared")]),
                              (["./extra", 3], []), ({"path": "./extra"}, []), (7, [])):
            with self.subTest(skills=skills):
                self.assertEqual(self._parts(self._plugin(skills, **files)), parts)

    def test_the_top_level_exclude_of_the_plugin_s_skills_json_applies(self):
        files = self._plugin(["extra"], **{"extra/kept/SKILL.md": "k", "extra/gone/SKILL.md": "g",
                                           "skills.json": {"exclude": ["gone"]}})
        self.assertEqual(self._parts(files), [(f"{self.PLUGIN}/extra/kept", "declared")])

    def test_the_plugin_root_is_a_root_skill_and_not_the_whole_folder(self):
        files = self._plugin(".", **{"SKILL.md": "---\nname: p\ndescription: d\n---\n",
                                     "scripts/run.sh": "x", "src/main.py": "x"})
        self.assertEqual(self._parts(files), [(f"{self.PLUGIN}/SKILL.md", "root_skill"),
                                              (f"{self.PLUGIN}/scripts", "root_skill_folder")])

    def test_the_manifest_is_read_with_comments_and_trailing_commas(self):
        """CLI 1.4.9 passes the manifest to `hujson.Standardize` before it decodes it."""
        root = self._write({f"{self.PLUGIN}/extra/b/SKILL.md": "b"})
        (root / self.PLUGIN / "plugin.json").write_text(
            '{\n  // the skills beside skills/\n  "name": "p",\n  "skills": ["./extra",],\n}\n')
        self.assertEqual([(rel, kind) for rel, row, kind in paths.plugin_components(root)
                          if row["path"] == "<antigravity-plugin>/skills"],
                         [(f"{self.PLUGIN}/extra/b", "declared")])

    def test_the_listed_skills_are_collected_and_reported(self):
        """The cloning step copies a listed skill folder outside the plugin with its support
        files, and detection reports it and a listed root skill under skills."""
        from agentcontext.steps import collected_file_cleanup, detection
        root = self._write({".agents/plugins/p/plugin.json": {"name": "p", "skills": ["../../../shared/s"]},
                            "shared/s/SKILL.md": "---\nname: s\ndescription: d\n---\n",
                            "shared/s/references/r.md": "r",
                            ".agents/plugins/q/plugin.json": {"name": "q", "skills": "."},
                            ".agents/plugins/q/SKILL.md": "---\nname: q\ndescription: d\n---\n",
                            "src/main.py": "print()\n"})
        out = self.tmp / "collected"
        collect_repo(root, out, set())
        self.assertTrue((out / "shared/s/references/r.md").is_file())
        self.assertFalse((out / "src").exists())
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        detection._plugin_components.cache_clear()
        self.assertEqual(sorted((r["skills.md"], r["name"]) for r in detection.detect_skills(out)),
                         [(".agents/plugins/q/SKILL.md", "q"), ("shared/s/SKILL.md", "s")])


class TestTheFiltersOfAnInheritsObject(_RegistrationTree):
    """Antigravity CLI 1.4.2 applied the `include_only` and `exclude` of an `inherits` object
    to every item that the inherited chain selects, as names that end the item's path, and not
    to the entries of the file with the `inherits` object. A step that ignored both lists
    would select items that the CLI leaves out."""

    def _agents(self, inherits: list, files: dict, own=None) -> list:
        team = {"entries": [{"path": "shared"}]}
        return self._selected({".agents/agents.json": {"inherits": inherits, **(own or {})},
                               "team/agents.json": team, **files})

    def test_the_filters_leave_out_what_they_name(self):
        three = {f"shared/{name}.md": _AGENT for name in ("alpha", "beta", "gamma")}
        two = {f"shared/{name}.md": _AGENT for name in ("alpha", "beta")}
        for inherits, files, selected in (
                ([{"path": "team/agents.json", "exclude": ["alpha"]}], two, ["shared/beta.md"]),
                ([{"path": "team/agents.json", "exclude": ["alpha"], "include_only": ["beta"]}],
                 three, ["shared/beta.md"]),
                ([{"path": "team/agents.json", "exclude": ["shared/alpha"]}], two,
                 ["shared/beta.md"]),
                ([{"path": "team/agents.json", "include_only": ["shared/beta"]}], two,
                 ["shared/beta.md"]),
                ([{"path": "team/agents.json", "include_only": ["cat/deep"]}],
                 {"shared/alpha.md": _AGENT, "shared/cat/deep/agent.md": _AGENT}, []),
                ([{"path": "team/agents.json", "exclude": ["alpha"]}, {"path": "team/agents.json"}],
                 two, ["shared/beta.md"])):
            with self.subTest(inherits=inherits):
                self.assertEqual(self._agents(inherits, files), selected)

    def test_the_filters_are_carried_through_the_chain_and_combine(self):
        self.assertEqual(self._selected({
            ".agents/agents.json": {"inherits": [{"path": "team/agents.json", "exclude": ["alpha"]}]},
            "team/agents.json": {"inherits": [{"path": "team2/agents.json",
                                               "include_only": ["alpha", "beta"]}]},
            "team2/agents.json": {"entries": [{"path": "shared"}]},
            **{f"shared/{name}.md": _AGENT for name in ("alpha", "beta", "gamma")}}),
            ["shared/beta.md"])

    def test_the_entries_of_the_inheriting_file_keep_every_item(self):
        self.assertEqual(self._agents([{"path": "team/agents.json", "exclude": ["alpha"]}],
                                      {"shared/alpha.md": _AGENT, "shared/beta.md": _AGENT},
                                      own={"entries": [{"path": "shared"}]}),
                         ["shared/alpha.md", "shared/beta.md"])


class TestAnEntryExcludesByTheEndOfAPath(_RegistrationTree):
    """Antigravity CLI 1.4.2 left an item out for an `exclude` value that ends its path in
    whole segments, such as `shared/alpha` for `team/shared/alpha.md`, and read the entry's
    `include_only` as a path below the entry's directory only."""

    def test_exclude_and_include_only(self):
        files = {"team/shared/alpha.md": _AGENT, "team/shared/beta.md": _AGENT}
        for entry, selected in (
                ({"exclude": ["shared/alpha"]}, ["team/shared/beta.md"]),
                ({"exclude": ["team/shared/alpha"]}, ["team/shared/beta.md"]),
                ({"exclude": ["hared/alpha"]}, ["team/shared/alpha.md", "team/shared/beta.md"]),
                ({"include_only": ["shared/beta"]}, []),
                ({"include_only": ["beta"]}, ["team/shared/beta.md"])):
            with self.subTest(entry=entry):
                self.assertEqual(self._selected({
                    ".agents/agents.json": {"entries": [{"path": "team/shared", **entry}]},
                    **files}), selected)


class TestATopLevelExclude(_RegistrationTree):
    """A registration file's top-level `exclude` leaves items out of the session's entries and
    default places: Antigravity CLI 1.4.2 listed no `.agents/agents/defaultone.md` under an
    `.agents/agents.json` with `"exclude": ["defaultone"]`, and no subagent, skill or rule of a
    plugin's default places that the plugin's own registration file excluded. A step that
    ignored the list would keep these items."""

    def _subagents(self, files: dict) -> set:
        from agentcontext.steps import detection
        return {r["subagent"] for r in detection.detect_subagents(self._write(files))
                if r["subagent"].endswith(".md")}

    def test_it_leaves_out_registered_agents_and_agents_of_the_default_places(self):
        default = {".agents/agents/defaultone.md": _AGENT, ".agents/agents/defaulttwo.md": _AGENT,
                   "shared/alpha.md": _AGENT, "shared/beta.md": _AGENT}
        for registration, selected, left_out in (
                ({"exclude": ["alpha", "defaultone"], "entries": [{"path": "shared"}]},
                 {".agents/agents/defaulttwo.md", "shared/beta.md"},
                 {".agents/agents/defaultone.md": ".agents/agents"}),
                ({"entries": [{"path": "shared"}]}, set(default), {})):
            with self.subTest(registration=registration):
                self.assertEqual(self._subagents({".agents/agents.json": registration, **default}),
                                 selected)
                self.assertEqual(paths.antigravity_left_out(self.repo), left_out)

    def test_it_names_an_item_by_the_end_of_its_path_and_include_only_is_no_top_level_key(self):
        root = {".agents/agents.json": {"exclude": ["agents/d1", "d2.md", ".agents/agents/d3",
                                                   "fold", "D5"]},
                **{f".agents/agents/d{n}.md": _AGENT for n in range(1, 6)},
                ".agents/agents/fold/agent.md": _AGENT}
        self.assertEqual(self._subagents(root), {".agents/agents/d4.md", ".agents/agents/d5.md"})
        self.assertEqual(self._subagents({".agents/agents.json": {"include_only": ["d1"]},
                                          ".agents/agents/d1.md": _AGENT,
                                          ".agents/agents/d2.md": _AGENT}),
                         {".agents/agents/d1.md", ".agents/agents/d2.md"})

    def test_an_inherited_file_s_exclude_and_another_root_s_exclude_apply_too(self):
        self.assertEqual(self._subagents({
            ".agents/agents.json": {"inherits": [{"path": "team/agents.json"}],
                                    "entries": [{"path": "other"}]},
            "team/agents.json": {"exclude": ["alpha", "omega", "def"],
                                 "entries": [{"path": "shared"}]},
            "shared/alpha.md": _AGENT, "shared/beta.md": _AGENT, "other/omega.md": _AGENT,
            ".agents/agents/def.md": _AGENT}), {"shared/beta.md"})
        self.assertEqual(self._subagents({
            ".agents/agents.json": {"exclude": ["d1", "x"]},
            ".agent/agents.json": {"entries": [{"path": "shared"}]},
            ".agent/agents/d1.md": _AGENT, "_agents/agents/d2.md": _AGENT,
            "shared/x.md": _AGENT, "shared/y.md": _AGENT}),
            {"_agents/agents/d2.md", "shared/y.md"})

    def test_a_plugin_s_exclude_applies_to_the_plugin_alone(self):
        plugin = {".agents/plugins/p/plugin.json": {"name": "p"},
                  ".agents/plugins/p/agents/pa.md": _AGENT, ".agents/plugins/p/agents/pb.md": _AGENT,
                  ".agents/agents/wa.md": _AGENT}
        self.assertEqual(self._subagents({**plugin, ".agents/plugins/p/agents.json":
                                                    {"exclude": ["pa", "wa"]}}),
                         {".agents/agents/wa.md", ".agents/plugins/p/agents/pb.md"})
        self.assertEqual(self._subagents({**plugin, ".agents/agents.json": {"exclude": ["pa"]}}),
                         {".agents/agents/wa.md", ".agents/plugins/p/agents/pa.md",
                          ".agents/plugins/p/agents/pb.md"})

    def test_a_plugin_s_skills_and_rules(self):
        from agentcontext.steps import detection
        root = self._write({
            ".agents/plugins/p/plugin.json": {"name": "p"},
            ".agents/plugins/p/skills.json": {"exclude": ["s1"]},
            ".agents/plugins/p/rules.json": {"exclude": ["r1"]},
            ".agents/plugins/p/skills/s1/SKILL.md": "---\ndescription: s1\n---\n",
            ".agents/plugins/p/skills/s2/SKILL.md": "---\ndescription: s2\n---\n",
            ".agents/plugins/p/rules/r1.md": _RULE, ".agents/plugins/p/rules/r2.md": _RULE})
        self.assertEqual([r["skills.md"] for r in detection.detect_skills(root)
                          if r["skills.md"].endswith("SKILL.md")],
                         [".agents/plugins/p/skills/s2/SKILL.md"])
        self.assertEqual([r["rule"] for r in detection.detect_rules(root) if r["rule"].endswith(".md")],
                         [".agents/plugins/p/rules/r2.md"])

    def test_a_file_below_a_directory_leaves_out_nothing_of_the_directory(self):
        """A session in `sub/` reads both files, and a session at the root reads the root's
        alone. An item of the root stays configuration whatever `sub/` excludes."""
        self.assertEqual(self._subagents({
            "sub/.agents/agents.json": {"exclude": ["ra", "sa"]},
            ".agents/agents.json": {"exclude": ["sb"]},
            ".agents/agents/ra.md": _AGENT, "sub/.agents/agents/sa.md": _AGENT,
            "sub/.agents/agents/sb.md": _AGENT, "sub/.agents/agents/sc.md": _AGENT}),
            {".agents/agents/ra.md", "sub/.agents/agents/sc.md"})

    def test_a_plugins_json_leaves_out_a_plugin_of_a_default_directory(self):
        from agentcontext.steps import detection
        root = self._write({".agents/plugins.json": {"exclude": ["p"]},
                            ".agents/plugins/p/plugin.json": {"name": "p"},
                            ".agents/plugins/q/plugin.json": {"name": "q"},
                            ".agents/plugins/p/agents/pa.md": _AGENT,
                            ".agents/plugins/q/agents/qa.md": _AGENT})
        self.assertEqual(paths.plugin_roots(root)["<antigravity-plugin>"], [".agents/plugins/q"])
        self.assertEqual({r["subagent"] for r in detection.detect_subagents(root)},
                         {".agents/plugins/q/agents/qa.md"})

    def test_a_skill_that_another_tool_reads_stays(self):
        """`.agents/skills` is a place of every tool, and `.agent/skills` of Antigravity's
        alone."""
        from agentcontext.steps import detection
        root = self._write({".agents/skills.json": {"exclude": ["s1"]},
                            ".agents/skills/s1/SKILL.md": "---\nname: s1\ndescription: d\n---\n",
                            ".agent/skills/s1/SKILL.md": "---\nname: s1\ndescription: d\n---\n"})
        self.assertEqual([r["skills.md"] for r in detection.detect_skills(root)],
                         [".agents/skills/s1/SKILL.md", ".agents/skills.json"])


class TestAnInheritedFileThatRegistersNothing(_RegistrationTree):
    """Antigravity CLI 1.4.2, run offline over workspaces that are git repositories, read an
    inherited `.json` file with comments and trailing commas, a name in any case and the keys
    that it does not know ignored, and read nothing from one that does not parse or has a
    value of another type, and then went on with the chain. It read an inherited `.txt` file
    as one path entry per line and reported any other file type as unsupported. A
    package.json that an `inherits` object names sets no key that the CLI reads. A step that
    published every inherited file as a configuration artifact of the registration's mechanism
    would publish a package.json as a skill named `package`."""

    def _chain(self, inherited: dict, team=None) -> tuple[list, list]:
        """(the registered agents, the inherited files that are configuration artifacts) of
        an `.agents/agents.json` that inherits each file of `inherited` and then `team.json`,
        and selects `a` itself."""
        files = {".agents/agents.json": {"inherits": [{"path": rel} for rel in inherited]
                                         + [{"path": "team.json"}],
                                         "entries": [{"path": "a"}]},
                 "team.json": team or {"entries": [{"path": "b"}]},
                 "a/x.md": _AGENT, "b/y.md": _AGENT, "c/z.md": _AGENT, **inherited}
        found = paths.registered_configuration_files(self._write(files),
                                                     include_registering_files=True)
        return ([rel for rel, _row in found if rel.endswith(".md")],
                [rel for rel, _row in found
                 if not rel.endswith(".md") and rel != ".agents/agents.json"])

    def test_a_file_that_the_cli_does_not_read_is_no_configuration_artifact(self):
        for inherited in ({"package.json": {"name": "app", "dependencies": {}}},
                          {"notes.json": "not json\n"},
                          {"list.json": "[1, 2]"},
                          {"empty.json": ""},
                          {"bom.json": "﻿" + json.dumps({"entries": [{"path": "c"}]})},
                          {"wrong.json": {"entries": [{"path": "c"}, {"path": 5}]}},
                          {"wrong.json": {"entries": [{"path": "c", "include_only": "z"}]}},
                          {"wrong.json": {"entries": [{"path": "c"}], "inherits": "x"}},
                          {"wrong.json": {"entries": [{"path": "c"}], "exclude": [5]}},
                          {"README.md": "# readme\n"},
                          {"team.yaml": "entries:\n  - path: c\n"},
                          {"shared.JSON": json.dumps({"entries": [{"path": "c"}]})},
                          {"team.jsonc": json.dumps({"entries": [{"path": "c"}]})}):
            with self.subTest(inherited=inherited):
                self.assertEqual(self._chain(inherited), (["a/x.md", "b/y.md"], ["team.json"]))

    def test_a_file_that_the_cli_reads_is_one(self):
        for inherited, agents in (
                ({"package.json": {"name": "app", "entries": [{"path": "c"}]}},
                 ["a/x.md", "b/y.md", "c/z.md"]),
                ({"package.json": {"name": "app", "exclude": ["y"]}}, ["a/x.md"]),
                ({"more.json": "// shared\n{\"entries\": [{\"path\": \"c\"},],}\n"},
                 ["a/x.md", "b/y.md", "c/z.md"]),
                ({"more.json": {"ENTRIES": [{"Path": "c", "extra": 1}, None, {}], "$schema": 5,
                                "include_only": 5}},
                 ["a/x.md", "b/y.md", "c/z.md"]),
                ({"more.json": {"entries": None, "inherits": []}}, ["a/x.md", "b/y.md"]),
                ({"paths.txt": "# shared agents\n\n  c  \r\n#b\n"}, ["a/x.md", "b/y.md", "c/z.md"])):
            with self.subTest(inherited=inherited):
                self.assertEqual(self._chain(inherited), (agents, sorted([*inherited, "team.json"])))

    def test_a_registration_file_that_starts_a_chain_reads_comments_and_trailing_commas(self):
        """The file is a configuration artifact by its path whatever it contains, and CLI 1.4.2
        loaded the entries of one with comments and trailing commas, and of none with a value
        of another type."""
        for text, selected in (
                ('// team\n{"entries": [{"path": "a"} /* x */,],}\n', ["a/x.md"]),
                ('{"Entries": [{"PATH": "a"}]}', ["a/x.md"]),
                (json.dumps({"entries": [{"path": "a"}, {"path": True}]}), []),
                (json.dumps({"entries": [{"path": "a"}], "exclude": ["x", 5]}), [])):
            with self.subTest(text=text):
                found = paths.registered_configuration_files(
                    self._write({".agents/agents.json": text, "a/x.md": _AGENT}),
                    include_registering_files=True)
                self.assertEqual([rel for rel, _row in found], [".agents/agents.json", *selected])

    def test_the_inherited_package_json_is_not_collected_kept_or_detected(self):
        from agentcontext.steps import collected_file_cleanup, detection
        root = self._write({".agents/skills.json": {"inherits": [{"path": "package.json"}]},
                            "package.json": {"name": "app", "dependencies": {}}})
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertFalse((out / "package.json").exists())
        (out / "package.json").write_text('{"name": "app", "dependencies": {}}')
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertFalse((out / "package.json").exists())
        (out / "package.json").write_text('{"name": "app", "dependencies": {}}')
        self.assertEqual([(r["skills.md"], r["name"]) for r in detection.detect_skills(out)],
                         [(".agents/skills.json", "skills")])


class TestARepositoryFileThatLeadsOutIsNotRead(_RegistrationTree):
    """A registration file, a file that names a context file, a marketplace and a plugin
    manifest are read only where they resolve to a regular file inside the repository. Git
    stores a symlink as it was committed, and `.agents/skills.json` can lead to a file
    of the collecting machine, or to `/dev/zero`, from which a reader would read without end."""

    def setUp(self):
        super().setUp()
        self.outside = self.tmp / "outside"
        self.outside.mkdir()

    def _link(self, rel: str, content) -> None:
        """`rel` as a link to a file outside the repository with `content`."""
        target = self.outside / rel.replace("/", "_")
        target.write_text(json.dumps(content) if isinstance(content, dict) else content)
        (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, self.repo / rel)

    def _returns(self, function, *args):
        """What `function` returns, or a failure where it does not return in a few seconds, as
        a reader blocked on a FIFO does not."""
        result = []
        worker = threading.Thread(target=lambda: result.append(function(*args)), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), f"{function.__name__} did not return")
        return result[0]

    def test_a_registration_file_that_leads_out(self):
        self._write({"skills/a/SKILL.md": "a", "team/b/SKILL.md": "b"})
        self._link(".agents/skills.json", {"entries": [{"path": "skills"}]})
        self.assertEqual([rel for rel, _row in paths.registered_configuration_files(
            self.repo, include_registering_files=True)], [".agents/skills.json"])
        self._write({"skills/a/SKILL.md": "a", "team/b/SKILL.md": "b",
                     ".agents/skills.json": {"inherits": [{"path": "team.json"}]}})
        self._link("team.json", {"entries": [{"path": "team"}]})
        self.assertEqual([rel for rel, _row in paths.registered_configuration_files(
            self.repo, include_registering_files=True)], [".agents/skills.json"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "the platform has no FIFO")
    def test_a_registration_file_that_is_a_fifo(self):
        self._write({"skills/a/SKILL.md": "a"})
        (self.repo / ".agents").mkdir()
        os.mkfifo(self.repo / ".agents/skills.json")
        self.assertEqual(self._returns(paths.registered_configuration_files, self.repo), [])
        os.mkfifo(self.repo / "gemini-extension.json")
        self.assertEqual(self._returns(paths.declared_context_files, self.repo), [])

    def test_a_file_that_names_a_context_file_and_leads_out(self):
        self._write({"NOTES.md": "notes\n"})
        self._link("gemini-extension.json", {"contextFileName": "NOTES.md"})
        self.assertEqual(paths.declared_context_files(self.repo), [])

    def test_a_marketplace_or_a_manifest_that_leads_out(self):
        agent_plugin = {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                        "name": "p"}
        self._write({"plugins/p/agents/reviewer.md": _AGENT, "q/ext/SKILL.md": "x",
                     "other/x.md": "x", "q/.claude-plugin/README.md": "q"})
        self._link(".claude-plugin/marketplace.json",
                   {"plugins": [{"name": "p", "source": "./plugins/p"}]})
        self._link(".cursor-plugin/marketplace.json",
                   {"plugins": [{"name": "o", "source": "other"}]})
        self._link("q/.claude-plugin/plugin.json", {"name": "q", "skills": ["./ext"]})
        self._link("a/plugin.json", agent_plugin)
        roots = paths.plugin_roots(self.repo)
        self.assertEqual((roots["<claude-plugin>"], roots["<cursor-plugin>"],
                          roots["<agent-plugin>"]), (["q"], [], []))
        self.assertNotIn("declared", [kind for _rel, _row, kind in paths.plugin_components(self.repo)])
        out = self.tmp / "collected"
        paths.collect_agent_plugins(self.repo, out, set())
        self.assertFalse((out / "a/plugin.json").exists())


def _tree_files(root: Path) -> list:
    """The files and links of a tree, repository-relative and sorted, a link to a directory
    among them, which `rglob` does not descend into."""
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*")
                  if p.is_file() or p.is_symlink())


class TestTheFrontMatterOfARegisteredItem(_RegistrationTree):
    """Antigravity CLI 1.4.2, run offline on 2026-10-19, listed with `antigravity agents` only
    the registered agents with a non-empty name and description in their front matter, `x` and
    not README.md for `.` at the workspace root, and counted with `antigravity plugin validate`
    only such agents and only rules with a trigger among what a plugin's agents.json and
    rules.json register. A step that selected every Markdown file that an entry reaches would
    publish README.md and CONTRIBUTING.md as Gemini subagents for `.` and give the repository
    the gemini flag."""

    def test_dot_at_the_root_selects_only_the_agent(self):
        self.assertEqual(self._selected({".agents/agents.json": {"entries": [{"path": "."}]},
                                         "README.md": "# Project\n\nSome text.\n",
                                         "CONTRIBUTING.md": "How to contribute.\n",
                                         "x.md": _AGENT}),
                         ["x.md"])

    def test_a_registered_directory_selects_only_its_agents(self):
        self.assertEqual(self._selected({".agents/agents.json": {"entries": [{"path": "docs"}]},
                                         "docs/README.md": "# Docs\n",
                                         "docs/guide.md": "---\ntitle: g\n---\nguide\n",
                                         "docs/named.md": "---\nname: named\n---\nno description\n",
                                         "docs/x.md": _AGENT}),
                         ["docs/x.md"])

    def test_a_folder_with_an_agent_md_gives_only_its_agents(self):
        """The CLI listed `folder` and `folder-extra` for a folder with agent.md, extra.md and a
        notes.md without front matter."""
        self.assertEqual(self._selected({".agents/agents.json": {"entries": [{"path": "folder"}]},
                                         "folder/agent.md": _AGENT, "folder/extra.md": _AGENT,
                                         "folder/notes.md": "notes without front matter\n"}),
                         ["folder/agent.md", "folder/extra.md"])

    def test_a_plugin_s_registered_agents_and_rules(self):
        """The CLI counted 3 agents for `.`, `more` and `folder` and 1 rule for `more`."""
        self.assertEqual(self._selected({
            ".agents/plugins/p/plugin.json": {"name": "p"},
            ".agents/plugins/p/agents.json": {"entries": [{"path": "."}, {"path": "more"},
                                                          {"path": "folder"}]},
            ".agents/plugins/p/rules.json": {"entries": [{"path": "more"}, {"path": "single.md"}]},
            ".agents/plugins/p/README.md": "# readme\n", ".agents/plugins/p/x.md": _AGENT,
            ".agents/plugins/p/more/README.md": "# readme\n", ".agents/plugins/p/more/y.md": _AGENT,
            ".agents/plugins/p/more/named.md": "---\nname: named\n---\nonly a name\n",
            ".agents/plugins/p/more/r.md": "Always do x.\n", ".agents/plugins/p/more/t.md": _RULE,
            ".agents/plugins/p/single.md": "no front matter\n",
            ".agents/plugins/p/folder/agent.md": _AGENT,
            ".agents/plugins/p/folder/notes.md": "notes without front matter\n"}),
            [".agents/plugins/p/folder/agent.md", ".agents/plugins/p/more/t.md",
             ".agents/plugins/p/more/y.md", ".agents/plugins/p/x.md"])

    def test_the_three_steps_select_the_same_agents(self):
        from agentcontext.steps import collected_file_cleanup, detection
        for entry, readme, agent in ((".", "README.md", "x.md"),
                                     ("docs", "docs/README.md", "docs/x.md")):
            with self.subTest(entry=entry):
                root = self._write({".agents/agents.json": {"entries": [{"path": entry}]},
                                    readme: "# Read me\n", agent: _AGENT})
                out = self.tmp / "collected" / entry / "owner§repo"
                flags = paths.collect_repo(root, out, set())
                self.assertEqual(_tree_files(out), [".agents/agents.json", agent])
                self.assertIn("gemini", flags)
                # A README.md beside the registered agent, for the cleanup to remove.
                (out / readme).parent.mkdir(parents=True, exist_ok=True)
                (out / readme).write_text("# Read me\n")
                collected_file_cleanup.scan_and_clean_repo(str(out), [])
                self.assertEqual(_tree_files(out), [".agents/agents.json", agent])
                (out / readme).write_text("# Read me\n")
                self.assertEqual([r["subagent"] for r in detection.detect_subagents(out)],
                                 [".agents/agents.json", agent])


class TestAKeyLineOfAFrontMatter(unittest.TestCase):
    """A lazy match of what follows a key's colon, before optional white space and the end of
    the line, takes time quadratic in a run of white space inside the value. The collector and
    the detection step read the front matter of every file in a rules or agents folder,
    whatever its size, through `top_level_keys`."""

    def test_a_long_run_of_white_space_in_a_value_is_read_in_linear_time(self):
        value = "x" + " " * 50_000 + "y"
        text = f"---\nname: a\ndescription: {value}\ntrigger: always_on\n---\nbody\n"
        start = time.monotonic()
        self.assertTrue(paths.has_name_and_description(text))
        self.assertTrue(paths.has_trigger(text))
        self.assertLess(time.monotonic() - start, 1.0)

    def test_the_value_is_what_follows_the_colon_without_trailing_white_space(self):
        front = ["name: a  b \t", "'name' :", "name:\t", "name:x", "description: d", "name: c # n  "]
        self.assertEqual(paths.top_level_keys(front, 0, "name"),
                         [(0, "a  b"), (1, ""), (2, ""), (5, "c # n")])

    def test_a_long_run_of_blank_lines_is_read_in_linear_time(self):
        """Antigravity's pattern for a `globs` value, `^(\\s*globs?\\s*:\\s*)`, tried at each line
        of a run of blank lines that another key follows, would read the rest of the run each
        time."""
        text = "---\ntrigger: glob\n" + "\n" * 50_000 + "name: a\nglobs: *\n---\nbody\n"
        start = time.monotonic()
        self.assertTrue(paths.has_trigger(text))
        self.assertLess(time.monotonic() - start, 1.0)


class TestTheFrontMatterOfAnAntigravityRule(unittest.TestCase):
    """Antigravity CLI 1.4.10 decodes a rule's front matter with go-yaml v3 after two
    replacements in parseRuleInfo: it quotes a `globs` or `glob` value that starts with `*` up
    to the end of the line, and where the front matter does not decode, a `description` with an
    unquoted ": " or a final ":" on a line that does not decode alone (read 2026-11-07). The
    expected texts and results are those of the two patterns and of go-yaml v3, run in Go on
    2026-11-07."""

    def test_a_glob_value_that_starts_with_a_star_is_quoted(self):
        cases = {"globs: *": 'globs: "*"', "globs: **/*.html": 'globs: "**/*.html"',
                 "glob: *.py": 'glob: "*.py"', "  globs :\n*.ts, *.tsx": '  globs :\n"*.ts, *.tsx"',
                 'globs: "**/*.html"': 'globs: "**/*.html"', "globs: src/*.ts": "globs: src/*.ts",
                 "name: x globs: *": "name: x globs: *", "myglobs: *": "myglobs: *"}
        for block, expected in cases.items():
            with self.subTest(block=block):
                self.assertEqual(paths.antigravity_rule_front_matter(f"trigger: glob\n{block}"),
                                 f"trigger: glob\n{expected}")

    def test_a_description_with_a_colon_is_quoted_where_the_front_matter_does_not_parse(self):
        self.assertEqual(
            paths.antigravity_rule_front_matter(
                "trigger: model_decision\ndescription: Steps: plan, then build\n"),
            'trigger: model_decision\ndescription: "Steps: plan, then build"')
        self.assertEqual(
            paths.antigravity_rule_front_matter("trigger: manual\ndescription: Say \"hi\": now"),
            'trigger: manual\ndescription: "Say \\"hi\\": now"')
        # A front matter that parses keeps its description, and so does a quoted one.
        for block in ("trigger: manual\ndescription: Steps # note: plan first",
                      "trigger: manual\ndescription: 'a: b'\nx: [1",
                      "trigger: manual\ndescription: no colon\nx: [1"):
            with self.subTest(block=block):
                self.assertEqual(paths.antigravity_rule_front_matter(block), block)

    def test_a_rule_counts_with_a_front_matter_that_decodes_once_quoted(self):
        """The struct that parseRuleInfo decodes into has the strings `trigger`, `description`,
        `globs` and `glob`. go-yaml v3 decodes any scalar into such a field and refuses a list
        or a mapping there, and it ignores the other keys."""
        for text, expected in (("trigger: glob\nglobs: *", True),
                               ("trigger: glob\nglobs: **/*.html", True),
                               ('trigger: glob\nglobs: "**/*.html"', True),
                               ("trigger: model_decision\ndescription: A: b", True),
                               ("trigger: always_on\ndescription: 5", True),
                               ("trigger: always_on\nalwaysApply: [x]\nmeta: {a: 1}", True),
                               ("trigger: glob\nglobs: *.ts # \"x\"", False),
                               ("trigger: always_on\ndescription: *important", False),
                               ("trigger: always_on\ntags: [a, b", False),
                               ("trigger: always_on\nx: 1\nx: 2", False),
                               ("trigger: glob\nglobs:\n  - pkg/task/**/*.go", False),
                               ("trigger: always_on\ndescription:\n  en: x", False)):
            with self.subTest(text=text):
                self.assertIs(paths.has_trigger(f"---\n{text}\n---\nbody\n"), expected)


class TestTheYamlThatAToolParses(unittest.TestCase):
    """go-yaml v3, which Antigravity CLI decodes a rule with, and js-yaml 3, which Cursor CLI
    parses a skill with, refuse a mapping with a key twice, which PyYAML's safe loader keeps.
    YAML lets a key repeat one that a merge key brings in, and a timestamp that is no date,
    such as 2025-02-30, is a valid scalar, for which PyYAML's constructor raises ValueError."""

    def test_a_key_twice_does_not_parse(self):
        self.assertFalse(paths.yaml_parses("name: a\nname: b\n"))
        self.assertFalse(paths.yaml_parses("outer:\n  k: 1\n  k: 2\n"))
        self.assertTrue(paths.yaml_parses("name: a\nouter:\n  name: b\n"))

    def test_a_key_that_a_merge_brings_in_can_repeat(self):
        self.assertTrue(paths.yaml_parses("base: &b {x: 1, y: 2}\nother:\n  <<: *b\n  x: 3\n"))

    def test_a_timestamp_that_is_no_date_parses(self):
        self.assertTrue(paths.yaml_parses("created: 2025-02-30\n"))

    def test_the_values_that_the_tools_meet_parse_as_they_do(self):
        for text, expected in (("globs: *", False), ("globs: **/*.html", False),
                               ('globs: "**/*.html"', True), ("description: a: b", False),
                               ("name: [invalid", False), ("description: >\n  a: b\n", True),
                               ("", True), ("# only a comment", True)):
            with self.subTest(text=text):
                self.assertIs(paths.yaml_parses(text), expected)

    def test_the_modules_that_parse_no_yaml_import_without_pyyaml(self):
        """`paths` imports PyYAML when a test first parses a front matter (`_yaml_loader`). The
        runner, the commit classification and the steps that parse no front matter import
        `paths` and need the standard library alone, as their documents state, and CI runs the
        sampling runner suite before it installs any distribution. The child imports no site
        packages (`-S`) and finds the package of this checkout on PYTHONPATH."""
        modules = ("agentcontext.paths", "agentcontext.runner", "agentcontext.commits",
                   "agentcontext.steps.commit_reclassification",
                   "agentcontext.steps.output_cross_check",
                   "agentcontext.steps.collected_files_archive", "agentcontext.steps.release")
        result = subprocess.run(
            [sys.executable, "-S", "-c",
             f"import sys, {', '.join(modules)}; print('yaml' in sys.modules)"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "False\n")


class TestADeclaredPlaceThatLeadsOutOfTheRepository(unittest.TestCase):
    """A plugin's manifest can name a folder that a link leads out of the repository, such as
    `skills -> ../../..`, into the collected trees and the clones beside it or to `/`. What the
    folder holds is never collected, and the collector does not list it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "owner§repo"
        manifest = self.repo / "plugins/tools/.codex-plugin/plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(json.dumps({"name": "tools", "skills": "./skills/"}), encoding="utf-8")
        outside = self.tmp / "outside/op"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: op\ndescription: d\n---\n")
        (self.repo / "plugins/tools/skills").symlink_to("../../../outside")

    def test_the_folder_is_not_listed(self):
        listed = []
        listing = paths.list_repo_files

        def recorded(base, *args, **kwargs):
            listed.append(Path(base).resolve())
            return listing(base, *args, **kwargs)

        with mock.patch.object(paths, "list_repo_files", recorded):
            components = paths.plugin_components(self.repo)
        inside = self.repo.resolve()
        self.assertEqual([base for base in listed if not base.is_relative_to(inside)], [])
        self.assertNotIn("plugins/tools/skills/op", [rel for rel, _rule, _kind in components])


class TestADeclaringFileBehindALink(_RegistrationTree):
    """A file whose content decides what the steps select, such as a registration file, a
    settings file that names a context file, a plugin manifest or a marketplace, can be a
    symlink to a file elsewhere in the repository. The collector reads it through the link in
    the clone and copies what it declares. Stored as a marker, a one-line file with the link's
    target, the file would make the collected file cleanup and the detection step select
    nothing of what it declares, and the cleanup would delete what the collector copied for it.
    The collector therefore stores such a file with the content that it read, and a link that
    leads out of the repository stays a marker."""

    # (declaring file, its target with content, the files that it declares, what detection
    # reports for them)
    CASES = {
        "registration file": (
            ".agents/skills.json", ("config/skills.json", {"entries": [{"path": "tools/a"}]}),
            {"tools/a/SKILL.md": "---\nname: a\ndescription: d\n---\nbody\n"},
            ("skills", "tools/a/SKILL.md")),
        "Gemini settings file": (
            ".gemini/settings.json", ("config/gemini.json", {"context": {"fileName": ["CONTEXT.md"]}}),
            {"CONTEXT.md": "# Context\n"}, ("context", "CONTEXT.md")),
        "Codex config": (
            ".codex/config.toml", ("config/codex.toml", 'project_doc_fallback_filenames = ["TEAM.md"]\n'),
            {"TEAM.md": "# Team\n"}, ("context", "TEAM.md")),
        "Claude plugin manifest": (
            ".claude-plugin/plugin.json", ("plugin/plugin.json", {"name": "p", "skills": "./extra"}),
            {"extra/s/SKILL.md": "---\nname: s\ndescription: d\n---\nbody\n"},
            ("skills", "extra/s/SKILL.md")),
        "Claude marketplace": (
            ".claude-plugin/marketplace.json",
            ("m/marketplace.json", {"name": "m", "owner": {"name": "o"},
                                    "plugins": [{"name": "a", "source": "./plugins/a"}]}),
            {"plugins/a/skills/x/SKILL.md": "---\nname: x\ndescription: d\n---\nbody\n"},
            ("skills", "plugins/a/skills/x/SKILL.md")),
        "Cursor marketplace": (
            ".cursor-plugin/marketplace.json",
            ("m/marketplace.json", {"name": "m", "plugins": [{"name": "a", "source": "plugins/a"}]}),
            {"plugins/a/rules/r.mdc": "---\ndescription: d\n---\nrule\n"},
            ("rules", "plugins/a/rules/r.mdc")),
        "MCP file of a Copilot CLI plugin": (
            "p/.mcp.json", ("shared/mcp.json", {"mcpServers": {"gh": {"command": "gh-mcp"}}}),
            {"p/.plugin/plugin.json": '{"name": "p"}\n'}, ("mcp", "p/.mcp.json")),
    }

    def _detected(self, tree: Path, kind: str) -> list:
        from agentcontext.steps import detection
        if kind == "skills":
            return [r["skills.md"] for r in detection.detect_skills(tree)]
        if kind == "context":
            return detection.detect_context_files(tree)
        if kind == "rules":
            return [r["rule"] for r in detection.detect_rules(tree)]
        return detection.detect_mcp(tree)

    def _case(self, name: str, link_target=None) -> tuple:
        """The repository of a case, with the declaring file a link to its target, or to
        `link_target`, collected into a tree of its own."""
        declaring, (target, content), declared, _detected = self.CASES[name]
        root = self._write({target: content, **declared})
        (root / declaring).parent.mkdir(parents=True, exist_ok=True)
        os.symlink(link_target or os.path.relpath(root / target, (root / declaring).parent),
                   root / declaring)
        out = self.tmp / "collected" / name / "owner§repo"
        paths.collect_repo(root, out, set())
        return root, out

    def test_the_collector_stores_what_it_read_through_the_link(self):
        for name, (declaring, (target, _content), _declared, _detected) in self.CASES.items():
            with self.subTest(name):
                root, out = self._case(name)
                self.assertFalse((out / declaring).is_symlink())
                self.assertEqual((out / declaring).read_bytes(), (root / target).read_bytes())

    def test_the_cleanup_and_detection_keep_what_the_file_declares(self):
        from agentcontext.steps import collected_file_cleanup
        for name, (declaring, _target, declared, (kind, reported)) in self.CASES.items():
            with self.subTest(name):
                _root, out = self._case(name)
                deletions = []
                collected_file_cleanup.scan_and_clean_repo(str(out), deletions)
                for rel in declared:
                    self.assertTrue((out / rel).is_file(), f"{rel} was deleted: {deletions}")
                self.assertTrue((out / declaring).is_file())
                self.assertIn(reported, self._detected(out, kind))

    def test_a_registered_agent_behind_a_link(self):
        """Antigravity CLI 1.4.2 listed a registered agent that is a symlink to an agent elsewhere
        in the workspace, and the collector reads its front matter through the link."""
        from agentcontext.steps import collected_file_cleanup, detection
        root = self._write({".agents/agents.json": {"entries": [{"path": "team"}]},
                            "shared/a.md": _AGENT, "team/README.md": "# Team\n"})
        os.symlink("../shared/a.md", root / "team/a.md")
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertEqual((out / "team/a.md").read_text(), _AGENT)
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual([r["subagent"] for r in detection.detect_subagents(out)],
                         [".agents/agents.json", "team/a.md"])

    def test_a_link_that_leads_out_of_the_repository_stays_a_marker(self):
        outside = self.tmp / "outside.json"
        outside.write_text(json.dumps({"context": {"fileName": ["CONTEXT.md"]}}))
        _root, out = self._case("Gemini settings file", link_target=str(outside))
        self.assertEqual((out / ".gemini/settings.json").read_text(), f"{outside}\n")
        self.assertFalse((out / "CONTEXT.md").exists())

    def test_a_link_to_a_file_that_is_not_utf8_stays_a_marker(self):
        """The collector reads a registration file as UTF-8 and reads nothing from this one. Its
        link stays a marker rather than being stored with the file's bytes."""
        root = self._write({"shared/a.md": _AGENT})
        (root / "config").mkdir()
        (root / "config/agents.json").write_bytes(b'\xff\xfe{"entries": [{"path": "shared"}]}')
        (root / ".agents").mkdir()
        os.symlink("../config/agents.json", root / ".agents/agents.json")
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertEqual((out / ".agents/agents.json").read_text(), "../config/agents.json\n")

    def test_a_link_into_the_git_directory_stays_a_marker(self):
        """A settings file that links to `.git/logs/HEAD` would put the clone's log into the
        collected tree, which the archive publishes."""
        root = self._write({".git/logs/HEAD": "0000 1111 A U Thor <author@example.org> clone\n",
                            ".git/team.json": {"entries": [{"path": "shared"}]},
                            "shared/a.md": _AGENT})
        (root / ".gemini").mkdir()
        os.symlink("../.git/logs/HEAD", root / ".gemini/settings.json")
        (root / ".agents").mkdir()
        os.symlink("../.git/team.json", root / ".agents/agents.json")
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertEqual((out / ".gemini/settings.json").read_text(), "../.git/logs/HEAD\n")
        self.assertEqual((out / ".agents/agents.json").read_text(), "../.git/team.json\n")
        self.assertFalse(any(part == ".git" for rel in _tree_files(out) for part in rel.split("/")))


class TestALinkedRuleOrAgentInADefaultPlace(_RegistrationTree):
    """A rule or an agent in a tool's own folder can be a symlink to a file elsewhere in the
    repository. angular/angular links `.agent/rules/agents.md` to `../../AGENTS.md`, which sets
    `trigger: always_on`. The collector stores such a link as a marker. A detection step that tested
    the marker's one line for the front matter would make the rule an `other` row, and the collected
    file cleanup could delete the file behind a linked Claude agent, which the detection step reads
    through the marker. Antigravity CLI 1.4.2, run offline on 2026-10-19, counted a plugin's rule
    that links to the workspace's `AGENTS.md` with a trigger and an agent of a plugin that links to
    `shared/x.md`, listed a linked agent of `.agents/agents`, and reported "no valid trigger in the
    frontmatter" for a plugin's rule that links to a file without one."""

    def _collected(self, files: dict, links: dict) -> Path:
        """The tree of the repository with `files` and `links` after the cloning step and the
        collected file cleanup."""
        from agentcontext.steps import collected_file_cleanup
        root = self._write(files)
        for rel, target in links.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, root / rel)
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        return out

    def _rows(self, out: Path) -> dict:
        """The rows of the rules, subagents and other mechanisms that the detection step finds."""
        from agentcontext.steps import detection
        rules = [r["rule"] for r in detection.detect_rules(out)]
        subagents = [r["subagent"] for r in detection.detect_subagents(out)]
        found = [detection.detect_settings(out), detection.detect_context_files(out),
                 detection.detect_skills(out), detection.detect_subagents(out),
                 detection.detect_commands(out), detection.detect_hooks(out),
                 detection.detect_rules(out), detection.detect_mcp(out)]
        other = detection.detect_other(out, *found, detection.resolved_markers(out),
                                       detection.detect_plugins(out), detection.detect_lsp(out))
        return {"rules": rules, "subagents": subagents, "other": other}

    def test_angular_s_linked_rule_and_a_linked_claude_agent(self):
        from agentcontext.steps import detection
        out = self._collected({"AGENTS.md": "---\ntrigger: always_on\n---\nUse pnpm.\n",
                               "docs/agents/reviewer.md": "---\nname: reviewer\ndescription: "
                                                          "reviews\n---\nbody\n"},
                              {".agent/rules/agents.md": "../../AGENTS.md",
                               ".claude/agents/reviewer.md": "../../docs/agents/reviewer.md"})
        self.assertEqual((out / ".agent/rules/agents.md").read_text(), "../../AGENTS.md\n")
        self.assertEqual(self._rows(out), {"rules": [".agent/rules/agents.md"],
                                           "subagents": [".claude/agents/reviewer.md"],
                                           "other": []})
        self.assertEqual(detection.artifact_tools(out, [("rules", ".agent/rules/agents.md"),
                                                        ("subagents", ".claude/agents/reviewer.md")]),
                         {("rules", ".agent/rules/agents.md"): {"gemini"},
                          ("subagents", ".claude/agents/reviewer.md"): {"claude"}})

    def test_a_linked_rule_and_agent_that_nothing_else_collects(self):
        out = self._collected({"docs/team.md": _RULE, "docs/plain.md": "No front matter.\n",
                               "shared/x.md": _AGENT},
                              {".agents/rules/team.md": "../../docs/team.md",
                               ".agents/rules/plain.md": "../../docs/plain.md",
                               ".agents/agents/x.md": "../../shared/x.md"})
        self.assertEqual(self._rows(out), {"rules": [".agents/rules/team.md"],
                                           "subagents": [".agents/agents/x.md"],
                                           "other": [".agents/rules/plain.md"]})

    def test_a_rules_or_agents_folder_that_links_to_a_directory(self):
        """Antigravity CLI 1.4.2 listed the agents of an `.agents/agents` that links to another
        directory. A cloning step that followed no link at Antigravity's rules and agents
        folders would collect nothing of such a folder, because rsync leaves out a link to a
        directory outside the folder that it copies."""
        out = self._collected({"shared/agents/a.md": _AGENT, "shared/rules/r.md": _RULE},
                              {".agents/agents": "../shared/agents", ".agents/rules": "../shared/rules"})
        self.assertEqual(self._rows(out), {"rules": ["shared/rules/r.md"],
                                           "subagents": ["shared/agents/a.md"], "other": []})

    def test_a_plugin_s_linked_rule_and_agent(self):
        out = self._collected({".agents/plugins/p/plugin.json": {"name": "p"},
                               ".agents/plugins/p/rules/own.md": _RULE,
                               "AGENTS.md": "---\ntrigger: always_on\n---\nAlways do z.\n",
                               "shared/x.md": _AGENT},
                              {".agents/plugins/p/rules/agents.md": "../../../../AGENTS.md",
                               ".agents/plugins/p/agents/x.md": "../../../../shared/x.md"})
        self.assertEqual(self._rows(out), {
            "rules": [".agents/plugins/p/rules/agents.md", ".agents/plugins/p/rules/own.md"],
            "subagents": [".agents/plugins/p/agents/x.md"], "other": []})


class TestALinkedCustomizationRoot(_RegistrationTree):
    """Antigravity CLI 1.4.2, run offline on 2026-10-19 over a workspace whose `.agents` is a
    symlink to `config/agents`, read `.agents/agents.json` through the link and resolved its
    `shared` against the directory that contains the link: it listed `shared/a.md` and not
    `config/shared/b.md`, and in a session started in `pkg/` with `pkg/.agents -> config`,
    `pkg/shared/a.md`. A step that listed no file below a linked directory would collect none
    of the registrations of such a repository."""

    FILES = {"config/agents/agents.json": {"entries": [{"path": "shared"}]},
             "shared/a.md": _AGENT, "shared/README.md": "# Shared\n",
             "config/shared/b.md": _AGENT}

    def _linked(self, files: dict, links: dict) -> Path:
        root = self._write(files)
        for rel, target in links.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, root / rel)
        return root

    def test_the_registration_file_resolves_against_the_link_s_directory(self):
        for prefix in ("", "pkg/"):
            with self.subTest(prefix=prefix):
                root = self._linked({prefix + rel: content for rel, content in self.FILES.items()},
                                    {prefix + ".agents": "config/agents"})
                self.assertEqual([rel for rel, _row in paths.registered_configuration_files(
                                      root, include_registering_files=True)],
                                 [prefix + "config/agents/agents.json", prefix + "shared/a.md"])

    def test_the_three_steps_read_the_registration_through_the_link(self):
        from agentcontext.steps import collected_file_cleanup, detection
        root = self._linked(self.FILES, {".agents": "config/agents"})
        out = self.tmp / "collected" / "owner§repo"
        flags = paths.collect_repo(root, out, set())
        self.assertIn("gemini", flags)
        self.assertEqual(_tree_files(out), [".agents", "config/agents/agents.json", "shared/a.md"])
        self.assertEqual((out / ".agents").read_text(), "config/agents\n")
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(_tree_files(out), [".agents", "config/agents/agents.json", "shared/a.md"])
        self.assertEqual([r["subagent"] for r in detection.detect_subagents(out)],
                         ["config/agents/agents.json", "shared/a.md"])

    def test_a_chain_of_links_is_stored_as_the_directory_that_it_leads_to(self):
        """Antigravity CLI 1.4.2 listed `shared/a.md` for `.agents -> lnk` and `lnk ->
        config/agents`. The cleanup could not follow a marker that contained `lnk`, which the
        collected tree does not contain, and would delete the registration file and the agent."""
        from agentcontext.steps import collected_file_cleanup, detection
        root = self._linked(self.FILES, {".agents": "lnk", "lnk": "config/agents"})
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertEqual((out / ".agents").read_text(), "config/agents\n")
        collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(_tree_files(out), [".agents", "config/agents/agents.json", "shared/a.md"])
        self.assertEqual([r["subagent"] for r in detection.detect_subagents(out)],
                         ["config/agents/agents.json", "shared/a.md"])

    def test_a_link_to_the_directory_above_is_stored_relative_to_its_directory(self):
        root = self._linked({"agents.json": {"entries": [{"path": "shared"}]}, "shared/a.md": _AGENT},
                            {"pkg/.agents": "../pkg/.."})
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertEqual((out / "pkg/.agents").read_text(), "..\n")

    def test_a_file_with_a_nul_is_no_marker_of_a_linked_root(self):
        """A symlink's target cannot contain a NUL, and the path functions raise ValueError for
        a path with one. A regular `.agents` file that contains one would therefore stop every
        step that reads the registration files."""
        root = self._write({"shared/a.md": _AGENT})
        (root / ".agents").write_bytes(b"x\0")
        self.assertEqual(paths.registered_configuration_files(root), [])
        self.assertIsNone(paths.marker_target("x\0"))
        self.assertEqual(paths.marker_target(" config/agents\n"), "config/agents")

    def test_a_link_that_leads_out_of_the_repository_is_not_followed(self):
        """The link stays a marker, which records where it leads, and nothing on the collecting
        machine is read or copied through it."""
        outside = self.tmp / "outside"
        (outside / "agents").mkdir(parents=True)
        (outside / "agents/agents.json").write_text(json.dumps({"entries": [{"path": "shared"}]}))
        root = self._linked({"shared/a.md": _AGENT}, {".agents": str(outside / "agents")})
        self.assertEqual(paths.registered_configuration_files(root), [])
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(root, out, set())
        self.assertEqual(_tree_files(out), [".agents"])
        self.assertEqual((out / ".agents").read_text(), f"{outside / 'agents'}\n")


class TestALinkedToolDirectory(_RegistrationTree):
    """When a tool directory is a link, the tool reads the directory that the link leads to.
    comet-ml/opik links `.cursor` and `.codex` to `.agents`, and Cursor reads
    `.agents/agents/*.md` as `.cursor/agents/*.md`. LedgerHQ/device-sdk-ts links `.claude` to
    `agent-files/claude`. A cloning step that copied no linked tool directory would credit the
    `.agents` files to Antigravity alone and collect nothing of `agent-files/claude`."""

    HOOKS = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "make lint"}]}]}}
    FILES = {".agents/agents/planner.md": "---\nname: planner\ndescription: Plans.\n---\nPlan.\n",
             ".agents/skills/docs/SKILL.md": "---\nname: docs\ndescription: Docs.\n---\nWrite.\n",
             ".agents/rules/style.mdc": "---\ndescription: Style.\nalwaysApply: true\n---\nTabs.\n",
             "config/claude/settings.json": HOOKS,
             "config/claude/agents/reviewer.md": "---\nname: reviewer\ndescription: Reviews."
                                                 "\n---\nReview.\n",
             "config/claude/skills/pdf/SKILL.md": "---\nname: pdf\ndescription: PDF.\n---\nRead.\n",
             "config/claude/README.md": "# Claude\n",
             "shared/lint/SKILL.md": "---\nname: lint\ndescription: Lints.\n---\nLint.\n"}
    LINKS = {".cursor": ".agents", ".codex": ".agents", ".claude": "config/claude",
             "config/claude/skills/lint": "../../../shared/lint"}

    def _collected(self, files: dict, links: dict, vendored=frozenset()) -> Path:
        """The collected tree of a repository with `files` and `links`."""
        root = self._write(files)
        for rel, target in links.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, root / rel)
        out = self.tmp / "collected" / "owner§repo"
        shutil.rmtree(out, ignore_errors=True)
        with contextlib.redirect_stderr(io.StringIO()):
            paths.collect_repo(root, out, set(vendored))
        return out

    def _tools(self, out: Path) -> dict:
        """{(mechanism, path): tools} of every row that the detection step finds in `out`."""
        from agentcontext.steps import detection
        markers = detection.resolved_markers(out)
        found = [detection.detect_settings(out), detection.detect_context_files(out),
                 detection.detect_skills(out), detection.detect_subagents(out),
                 detection.detect_commands(out), detection.detect_hooks(out),
                 detection.detect_rules(out), detection.detect_mcp(out)]
        rows = ([("settings", p) for p in found[0]] + [("context_file", p) for p in found[1]]
                + [("skills", r["skills.md"]) for r in found[2]]
                + [("subagents", r["subagent"]) for r in found[3]]
                + [("hooks", p) for p in found[5]] + [("rules", r["rule"]) for r in found[6]]
                + [("other", p) for p in detection.detect_other(
                    out, *found, markers, detection.detect_plugins(out), detection.detect_lsp(out))])
        tools = detection.artifact_tools(out, rows, (), detection.plugin_part_tools(out),
                                         detection.declared_file_tools(out),
                                         detection.marker_directories(out, markers))
        return {row: sorted(found) for row, found in tools.items()}

    def test_the_three_steps_read_the_linked_directories(self):
        from agentcontext.steps import collected_file_cleanup
        out = self._collected(self.FILES, self.LINKS)
        collected = [".agents/agents/planner.md", ".agents/rules/style.mdc",
                     ".agents/skills/docs/SKILL.md", ".claude", ".codex", ".cursor",
                     "config/claude/README.md", "config/claude/agents/reviewer.md",
                     "config/claude/settings.json", "config/claude/skills/lint",
                     "config/claude/skills/pdf/SKILL.md", "shared/lint/SKILL.md"]
        self.assertEqual(_tree_files(out), collected)
        self.assertEqual({rel: (out / rel).read_text() for rel in (".cursor", ".codex", ".claude",
                                                                    "config/claude/skills/lint")},
                         {".cursor": ".agents\n", ".codex": ".agents\n", ".claude": "config/claude\n",
                          "config/claude/skills/lint": "../../../shared/lint\n"})
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(_tree_files(out), collected)
        self.assertEqual(self._tools(out), {
            ("settings", "config/claude/settings.json"): ["claude"],
            ("hooks", "config/claude/settings.json"): ["claude"],
            ("skills", "config/claude/skills/pdf/SKILL.md"): ["claude"],
            ("skills", "shared/lint/SKILL.md"): ["claude"],
            # `.agents/skills` names no tool, because several tools read it, and Codex and
            # Cursor read it through their own links.
            ("skills", ".agents/skills/docs/SKILL.md"): ["codex", "cursor"],
            ("subagents", "config/claude/agents/reviewer.md"): ["claude"],
            # Cursor reads the `.md` agents of `.cursor/agents` and of `.codex/agents`.
            ("subagents", ".agents/agents/planner.md"): ["cursor", "gemini"],
            ("rules", ".agents/rules/style.mdc"): ["cursor"],
            ("other", "config/claude/README.md"): ["claude"]})
        from agentcontext.steps import detection
        self.assertEqual(detection.linked_spec_files(out, detection.resolved_markers(out))
                         ["config/claude/README.md"], ".claude/README.md")

    def test_a_file_that_a_pass_reads_by_its_path_is_read_at_its_own_path(self):
        """The Claude pass copies `.claude/loop.md` by its path, and the Codex config names its
        instructions file relative to `.codex`. Read through the link, both would be copied
        below `.claude` and `.codex`, where no marker could then be written, and the cleanup and
        detection, which read the markers, would read no Codex config."""
        from agentcontext.steps import collected_file_cleanup
        out = self._collected({"config/claude/loop.md": "Check the build.\n",
                               "config/codex/config.toml": 'model_instructions_file = "notes.md"\n',
                               "config/codex/notes.md": "Use tabs.\n"},
                              {".claude": "config/claude", ".codex": "config/codex"})
        self.assertEqual(_tree_files(out), [".claude", ".codex", "config/claude/loop.md",
                                            "config/codex/config.toml", "config/codex/notes.md"])
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        # The heuristics record `.claude/loop.md` as a file that no mechanism covers.
        self.assertEqual(self._tools(out), {("settings", "config/codex/config.toml"): ["codex"],
                                            ("context_file", "config/codex/notes.md"): ["codex"],
                                            ("other", "config/claude/loop.md"): ["claude"]})

    def test_the_local_state_behind_a_linked_claude_is_not_collected(self):
        """The Claude pass leaves out the session logs of `.claude/projects/`, and so does the
        copy of the directory that `.claude` links to."""
        out = self._collected({"agent-files/claude/settings.json": self.HOOKS,
                               "agent-files/claude/projects/-home-u-repo/1.jsonl": "{}\n",
                               "agent-files/claude/history.jsonl": "{}\n",
                               "agent-files/claude/node_modules/x/index.js": "x\n"},
                              {".claude": "agent-files/claude"}, {"node_modules"})
        self.assertEqual(_tree_files(out), [".claude", "agent-files/claude/settings.json"])

    def test_a_linked_tool_directory_below_the_root(self):
        """eclipse-openvsx/openvsx links `webui/.claude` to `.agents`, which Claude Code reads in
        a session started in `webui/`."""
        from agentcontext.steps import collected_file_cleanup
        out = self._collected({"webui/.agents/skills/s/SKILL.md": "---\nname: s\ndescription: S."
                                                                   "\n---\nBody.\n"},
                              {"webui/.claude": ".agents"})
        self.assertEqual(_tree_files(out), ["webui/.agents/skills/s/SKILL.md", "webui/.claude"])
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(self._tools(out), {("skills", "webui/.agents/skills/s/SKILL.md"): ["claude"]})

    def test_a_link_out_of_the_repository_or_to_its_parent_stays_a_marker(self):
        """A target outside the repository is on the collecting machine, and a target that
        contains the link is the tree around it. Nothing is copied for either, and the marker of
        each is a file of the tool directory's name that no mechanism covers."""
        from agentcontext.steps import collected_file_cleanup
        outside = self.tmp / "outside"
        (outside / "skills" / "s").mkdir(parents=True)
        (outside / "skills" / "s" / "SKILL.md").write_text("x\n")
        out = self._collected({"AGENTS.md": "# Agents\n", "pkg/src/app.py": "x\n"},
                              {".claude": str(outside), "pkg/src/.cursor": ".."})
        self.assertEqual(_tree_files(out), [".claude", "AGENTS.md", "pkg/src/.cursor"])
        self.assertEqual((out / ".claude").read_text(), f"{outside}\n")
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(self._tools(out), {("context_file", "AGENTS.md"): [],
                                            ("other", ".claude"): ["claude"],
                                            ("other", "pkg/src/.cursor"): ["cursor"]})

    def test_the_linking_tool_s_local_state_in_another_tool_directory_is_not_collected(self):
        """Claude Code writes its shell snapshots and its scheduled tasks through `.claude ->
        .agents` into `.agents`. The shared pass copies `.agents` under its own name, which has
        no such places, and the copy through the link, with `rsync --delete`, keeps in the
        destination the files that its `--exclude` patterns match. The collector therefore
        removes from `.agents` the places of the local state named from `.claude` after it has
        followed every link. bruin-data/bruin has `.agents/scheduled_tasks.lock` beside
        `.claude -> .agents`."""
        from agentcontext.steps import collected_file_cleanup
        out = self._collected({".agents/skills/docs/SKILL.md": self.FILES[".agents/skills/docs/SKILL.md"],
                               ".agents/shell-snapshots/x.sh": "export TOKEN=1\n",
                               ".agents/scheduled_tasks.json": "[]\n",
                               ".agents/todos/t.json": "[]\n",
                               ".agents/worktrees/w/README.md": "# Checkout\n"},
                              {".claude": ".agents"})
        self.assertEqual(_tree_files(out), [".agents/skills/docs/SKILL.md", ".claude"])
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(self._tools(out), {("skills", ".agents/skills/docs/SKILL.md"): ["claude"]})

    def test_no_second_copy_into_a_linked_directory_brings_its_local_state_back(self):
        """A second link to the same directory, as in comet-ml/opik, or the link of a mechanism
        directory into it copies the directory again with the exclusions of its own link alone.
        The collected tree still leaves out the local state of every tool that links there."""
        skill = self.FILES[".agents/skills/docs/SKILL.md"]
        for name, files, links, kept in (
                ("two tool directories", {".agents/skills/docs/SKILL.md": skill,
                                          ".agents/shell-snapshots/snap.sh": "export TOKEN=1\n",
                                          ".agents/todos/t.json": "[]\n"},
                 {".claude": ".agents", ".cursor": ".agents"}, [".agents/skills/docs/SKILL.md"]),
                ("Codex and Cursor", {".agents/skills/docs/SKILL.md": skill,
                                      ".agents/sessions/2026/rollout.jsonl": "{}\n"},
                 {".codex": ".agents", ".cursor": ".agents"}, [".agents/skills/docs/SKILL.md"]),
                ("a mechanism directory", {".agents/skills/docs/SKILL.md": skill,
                                           ".agents/skills/synced/x/SKILL.md": skill},
                 {".claude": ".agents", ".cursor/skills": "../.agents/skills"},
                 [".agents/skills/docs/SKILL.md"])):
            with self.subTest(name):
                out = self._collected(files, links)
                self.assertEqual([f for f in _tree_files(out) if f.startswith(".agents/")], kept)

    def test_a_linked_github_keeps_what_the_linking_tool_reads_there(self):
        """plesk/centos2alma links `.claude` to `.github/`. Claude Code reads its settings and
        agents there. The CI workflows and issue templates of the repository are no configuration
        of Claude Code, and the cleanup deletes them from the `.github` as it does without the
        link."""
        from agentcontext.steps import collected_file_cleanup
        agent = "---\nname: rev\ndescription: Reviews.\n---\nReview.\n"
        out = self._collected({".github/workflows/ci.yml": "on: push\njobs: {}\n",
                               ".github/ISSUE_TEMPLATE/bug.md": "Describe the bug.\n",
                               ".github/settings.json": '{"permissions": {"allow": ["Bash(ls)"]}}',
                               ".github/agents/rev.md": agent, "AGENTS.md": "# Agents\n"},
                              {".claude": ".github/"})
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual(_tree_files(out), [".claude", ".github/agents/rev.md",
                                            ".github/settings.json", "AGENTS.md"])
        tools = self._tools(out)
        self.assertEqual(tools[("settings", ".github/settings.json")], ["claude"])
        self.assertIn("claude", tools[("subagents", ".github/agents/rev.md")])

    def test_a_plan_through_a_linked_claude_is_a_planning_document(self):
        """samanhappy/mcphub's `.agents/plans/cleartext-logging-fix.md` is Claude Code's plan
        `.claude/plans/cleartext-logging-fix.md` through `.claude -> .agents`, and a
        `plansDirectory` in the settings file behind the link names the folder of the plans,
        which the cloning step copies. Matched at its own path alone, the plan would be a file of
        `.agents` that no mechanism covers."""
        from agentcontext import planning
        from agentcontext.steps import collected_file_cleanup
        out = self._collected({".agents/plans/cleartext-logging-fix.md": "# Fix\n",
                               ".agents/settings.json": {"plansDirectory": "docs/plans"},
                               "docs/plans/auth.md": "# Auth\n", "AGENTS.md": "# Agents\n"},
                              {".claude": ".agents"})
        self.assertIn("docs/plans/auth.md", _tree_files(out))
        with contextlib.redirect_stderr(io.StringIO()):
            collected_file_cleanup.scan_and_clean_repo(str(out), [])
        self.assertEqual([(rel, row["framework"], row["document"])
                          for rel, row in planning.planning_documents(out)],
                         [(".agents/plans/cleartext-logging-fix.md", "claude", "plan"),
                          ("docs/plans/auth.md", "claude", "plan")])


class TestASupportFolderThatLinksToTheSkill(unittest.TestCase):
    """A root skill's `scripts`, `references` and `assets` are copied whole. A folder that is a
    symlink to the skill's folder or to a folder above it, such as `scripts -> .`, would make
    the collector copy the whole checkout into `scripts/`, its source code and its `.env`
    included.
    `copy_symlink_target` refuses such a target for a linked mechanism directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _repo(self, files: dict, links: dict) -> Path:
        repo = self.tmp / "source" / "owner§repo"
        for rel, text in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text)
        for rel, target in links.items():
            os.symlink(target, repo / rel)
        return repo

    SKILL = "---\nname: s\ndescription: d\n---\nbody\n"

    def test_a_registered_root_skill(self):
        from agentcontext.steps import collected_file_cleanup, detection
        for prefix, links in (("", {"scripts": "."}), ("pkg/", {"pkg/references": ".."})):
            with self.subTest(prefix=prefix, links=links):
                shutil.rmtree(self.tmp / "source", ignore_errors=True)
                repo = self._repo({prefix + ".agents/skills.json": '{"entries": [{"path": "."}]}',
                                   prefix + "SKILL.md": self.SKILL, prefix + "assets/a.txt": "a\n",
                                   prefix + "src/main.py": "print(1)\n", prefix + ".env": "TOKEN=x\n"},
                                  links)
                self.assertEqual(paths.registered_skill_folders(repo),
                                 [prefix + "SKILL.md", prefix + "assets"])
                out = self.tmp / "collected" / prefix / "owner§repo"
                paths.collect_repo(repo, out, set())
                self.assertEqual(_tree_files(out), [prefix + ".agents/skills.json",
                                                    prefix + "SKILL.md", prefix + "assets/a.txt"])
                collected_file_cleanup.scan_and_clean_repo(str(out), [])
                row, = [r for r in detection.detect_skills(out) if r["skills.md"] == prefix + "SKILL.md"]
                self.assertEqual((row["scripts"], row["references"], row["assets"]),
                                 ("FALSE", "FALSE", "TRUE"))

    def test_a_plugin_s_root_skill(self):
        repo = self._repo({".claude-plugin/plugin.json": '{"name": "p"}', "SKILL.md": self.SKILL,
                           "src/main.py": "print(1)\n", ".env": "TOKEN=x\n"}, {"scripts": "."})
        self.assertNotIn("scripts", [rel for rel, _row, _kind in paths.plugin_components(repo)])
        out = self.tmp / "collected" / "owner§repo"
        paths.collect_repo(repo, out, set())
        self.assertFalse((out / "scripts").exists())
        self.assertFalse((out / ".env").exists())


if __name__ == "__main__":
    unittest.main()
