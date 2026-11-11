"""
Tests for agentcontext.steps.collected_files_archive: the pruned sample and its archive.

Strategy:
  - Because the script deletes files and runs 7zz, every path is a command-line argument
    that names a temporary directory, and no test writes to the real validated tree.
  - The fixture tree is data/validated and never repos. The archive root is a pinned
    literal, and with a fixture named repos, a root derived from the tree's name would pass
    the same assertions.
  - The tests run main() in-process with sys.argv swapped, as the other command-line test
    files do.
  - The archive tests need 7zz on PATH and are skipped without it.

The tests import the step, a module of the package, by name.

Requirements: Python 3.14 (stdlib only), 7zz on PATH for the archive tests
Run with:  python3 -m unittest tests.test_steps_collected_files_archive -v
"""

import contextlib
import hashlib
import importlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from agentcontext import REPO_ROOT, SEVEN_ZIP
from agentcontext.prune import is_prunable

_mod = importlib.import_module("agentcontext.steps.collected_files_archive")
clean_empty_dirs = _mod.clean_empty_dirs
main = _mod.main
prunable_files = _mod.prunable_files

_HAS_7Z = shutil.which(SEVEN_ZIP) is not None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _StepCase(unittest.TestCase):
    """A test case with a validated tree and staging and archive paths in a temporary directory.

    The tree is `data/validated` and not `repos`. With a fixture named `repos`, a root derived
    from the directory's name would pass the assertion that the archive is rooted at `repos/`,
    and the assertion would only repeat the fixture's name.

    The collected file cleanup writes its list at the root of the tree, and the fixture writes
    one there too. The publication has to leave `data/validated/repos.csv` out without calling
    it a stray.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.validated = self.tmp / "data" / "validated"
        self.validated.mkdir(parents=True)
        self.validated_csv = self.validated / "repos.csv"
        self.validated_csv.write_text(
            "repo_name,scanned_at,branch,head_sha,head_commit_date\n", encoding="utf-8")
        self.staging = self.tmp / "staging"
        self.archive = self.tmp / "repos_dataset.7z"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _file(self, repo_dir: str, rel: str, content: str = "x\n") -> Path:
        p = self.validated / repo_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    def _run(self, *extra) -> int:
        argv = [
            "agentcontext/steps/collected_files_archive.py",
            "--validated-dir", str(self.validated),
            "--staging-dir", str(self.staging),
            "--archive", str(self.archive),
            *extra,
        ]
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()), \
                _swap(sys, "argv", argv):
            try:
                rc = main()
            except SystemExit as e:
                return e.code if isinstance(e.code, int) else 1
            return 0 if rc is None else rc

    def _validated_snapshot(self) -> dict:
        """Map every file under the validated tree to its content, for a byte comparison."""
        snap = {}
        for p in sorted(self.validated.rglob("*")):
            if p.is_file():
                snap[str(p.relative_to(self.validated))] = p.read_text(encoding="utf-8")
        return snap


@contextlib.contextmanager
def _swap(obj, attr, value):
    orig = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, orig)


def _fake_7z(argv, cwd=None, **kwargs):
    """Stand in for 7zz in a test of what the step does around the pack rather than in it."""
    Path(argv[-2]).write_bytes(b"7z-stub")
    return subprocess.CompletedProcess(argv, 0, "", "")


def _archive_attributes(archive: Path) -> dict:
    """Map each path to the attribute string that 7zz lists for it. An `l` in the mode means a
    stored link."""
    out = subprocess.run([SEVEN_ZIP, "l", "-slt", str(archive)], capture_output=True, text=True)
    attrs, path = {}, None
    for line in out.stdout.splitlines():
        if line.startswith("Path = "):
            path = line[len("Path = "):]
        elif line.startswith("Attributes = ") and path is not None:
            attrs[path] = line[len("Attributes = "):]
    return attrs


def _list_archive(archive: Path) -> list:
    """Return the file and directory paths inside a 7z archive, as 7zz lists them.

    The first `Path =` line names the archive itself rather than an entry, and the function
    drops it.
    """
    out = subprocess.run([SEVEN_ZIP, "l", "-slt", str(archive)],
                         capture_output=True, text=True)
    paths = [line[len("Path = "):] for line in out.stdout.splitlines()
             if line.startswith("Path = ")]
    return paths[1:]


# ---------------------------------------------------------------------------
# clean_empty_dirs
# ---------------------------------------------------------------------------

class TestCleanEmptyDirs(_StepCase):

    def test_no_empty_dirs_removes_nothing(self):
        self._file("o§a", ".claude/settings.json")
        self.assertEqual(clean_empty_dirs(str(self.validated / "o§a")), 0)

    def test_single_empty_dir_removed(self):
        (self.validated / "o§a" / ".claude").mkdir(parents=True)
        self.assertEqual(clean_empty_dirs(str(self.validated / "o§a")), 1)

    def test_nested_empty_dirs_all_removed(self):
        (self.validated / "o§a" / "a" / "b" / "c").mkdir(parents=True)
        self.assertEqual(clean_empty_dirs(str(self.validated / "o§a")), 3)

    def test_the_root_itself_is_never_removed(self):
        root = self.validated / "o§a"
        root.mkdir()
        self.assertEqual(clean_empty_dirs(str(root)), 0)
        self.assertTrue(root.is_dir())

    def test_dir_with_a_file_is_kept(self):
        self._file("o§a", "keep/CLAUDE.md")
        clean_empty_dirs(str(self.validated / "o§a"))
        self.assertTrue((self.validated / "o§a" / "keep").is_dir())


# ---------------------------------------------------------------------------
# main  (input validation and the dry run)
# ---------------------------------------------------------------------------

class TestMainValidation(_StepCase):

    def test_a_missing_validated_tree_exits_nonzero(self):
        shutil.rmtree(self.validated)
        self.assertNotEqual(self._run(), 0)


class TestIsPrunable(_StepCase):
    """Which files the step removes before publication, and which it keeps on purpose."""

    def test_housekeeping_is_pruned_at_any_depth(self):
        for rel in (".gitignore", ".claude/.gitignore",
                    ".claude/skills/pdf/.gitignore", ".DS_Store"):
            with self.subTest(rel):
                self.assertTrue(is_prunable(rel))

    def test_lock_and_log_files_are_pruned(self):
        self.assertTrue(is_prunable(".claude/scheduled_tasks.lock"))
        self.assertTrue(is_prunable(".claude/hooks/run.log"))

    def test_local_session_state_is_pruned(self):
        for rel in (".claude/projects/-Users-x-code/session.jsonl", ".claude/todos/list.json",
                    ".codex/sessions/2026/10/21/rollout.jsonl", "sub/.gemini/tmp/x/logs.json",
                    ".cursor/prompt_history.json"):
            with self.subTest(rel):
                self.assertTrue(is_prunable(rel))

    def test_unclassified_configuration_is_kept(self):
        """The prune keeps a file that no mechanism covers, because a gap in the detection
        must not lose data."""
        for rel in (".claude/rules/style.md", ".claude/docs/architecture.md",
                    ".claude/hooks/format.sh", ".cursor/BUGBOT.md",
                    ".claude-plugin/plugin.json", ".cursorignore"):
            with self.subTest(rel):
                self.assertFalse(is_prunable(rel))

    def test_only_prunable_files_are_listed(self):
        self._file("o§a", ".claude/settings.json")
        self._file("o§a", ".claude/.gitignore")
        self.assertEqual(sorted(prunable_files(str(self.validated))),
                         [("o§a", ".claude/.gitignore")])


class TestDryRun(_StepCase):

    def test_dry_run_deletes_nothing(self):
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/.gitignore")
        before = self._validated_snapshot()
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(self._validated_snapshot(), before)

    def test_dry_run_creates_no_staging_or_archive(self):
        self._file("o§a", ".claude/.gitignore")
        self._run("--dry-run")
        self.assertFalse(self.staging.exists())
        self.assertFalse(self.archive.exists())


# ---------------------------------------------------------------------------
# main  (a real run: 7zz required)
# ---------------------------------------------------------------------------

class TestAFailedPack(_StepCase):
    """What the step leaves when 7zz fails. If the step removed the published archive and packed
    onto its name, a failed pack would leave a truncated file under that name, and the good
    archive would already be gone. If the step removed the staging copy only after a successful
    pack, a failure would also leave a second copy of the sample on the volume that the
    pre-checks measured."""

    def _failing_7z(self, argv, cwd=None, **kwargs):
        Path(argv[-2]).write_bytes(b"half-written")
        return subprocess.CompletedProcess(argv, 2, "", "7zz: out of space")

    def test_the_good_archive_survives_and_nothing_is_left_behind(self):
        self._file("o§a", "CLAUDE.md")
        self.archive.parent.mkdir(parents=True, exist_ok=True)
        self.archive.write_bytes(b"the previous run's archive")
        before = self.archive.read_bytes()
        with _swap(_mod, "_run_7zz", self._failing_7z):
            code = self._run()
        self.assertEqual(code, 1)
        self.assertEqual(self.archive.read_bytes(), before,
                         "the published archive did not survive a failed pack")
        self.assertFalse(self.staging.exists(), "the staging copy was left behind")
        self.assertEqual(sorted(p.name for p in self.archive.parent.glob("*.building")), [])


@unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
class TestRealRun(_StepCase):

    def test_the_validated_tree_is_never_modified(self):
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/.gitignore")
        before = self._validated_snapshot()
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._validated_snapshot(), before)

    def test_archive_is_created_and_staging_removed(self):
        self._file("o§a", "CLAUDE.md")
        self._run()
        self.assertTrue(self.archive.is_file())
        self.assertFalse(self.staging.exists())

    def test_pruned_files_are_absent_from_the_archive(self):
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/.gitignore")
        self._run()
        paths = _list_archive(self.archive)
        self.assertIn("repos/o§a/CLAUDE.md", paths)
        self.assertNotIn("repos/o§a/.claude/.gitignore", paths)

    def test_an_unclassified_artifact_reaches_the_archive(self):
        self._file("o§a", ".claude/docs/architecture.md")
        self._run()
        self.assertIn("repos/o§a/.claude/docs/architecture.md",
                      _list_archive(self.archive))

    def test_archive_is_rooted_at_repos(self):
        """`repos/` is a pinned literal and not the validated tree's name, which here is
        `validated`. The published archives are rooted at `repos/`, and
        `_website/build_site.py` stops with an error when that entry is missing. A root derived
        from the tree would publish the repository directories under `data/validated/` and
        make the site build fail."""
        self._file("o§a", "CLAUDE.md")
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        # Without this assertion, the `all` below would pass for a failed pack, which lists
        # nothing.
        self.assertIn("repos/o§a/CLAUDE.md", listed)
        self.assertTrue(all(p == "repos" or p.startswith("repos/") for p in listed), listed)
        self.assertNotIn(self.validated.name, "\n".join(listed))

    def test_a_repo_with_nothing_to_prune_is_fine(self):
        self._file("o§a", "CLAUDE.md")
        self.assertEqual(self._run(), 0)
        self.assertTrue(self.archive.is_file())

    def _earlier_archive(self, path: Path) -> None:
        """Pack a real archive that contains `repos/o§gone/CLAUDE.md` at `path`: a repository
        that an earlier run published and that has since left the sample."""
        old = self.tmp / "earlier"
        (old / "repos" / "o§gone").mkdir(parents=True)
        (old / "repos" / "o§gone" / "CLAUDE.md").write_text("gone\n", encoding="utf-8")
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(path), "repos"], cwd=old,
                       check=True)
        shutil.rmtree(old)

    def test_a_prior_archive_is_replaced(self):
        """The earlier archive is a real one, because `7zz a` cannot update a file that is not
        an archive. With a text file under the name, the test could not tell a replaced
        archive from an updated one."""
        self._file("o§a", "CLAUDE.md")
        self._earlier_archive(self.archive)
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        self.assertIn("repos/o§a/CLAUDE.md", listed)
        self.assertNotIn("repos/o§gone", listed)

    def test_the_published_archive_is_replaced_in_one_step(self):
        """The step renames the new archive onto the published name rather than writing over
        it. A reader that holds the previous archive, such as an upload in progress, then keeps
        reading the previous bytes and never reads a half-rewritten file. A hard link to the
        previous archive keeps these bytes only when the step replaced the name rather than
        rewriting the file."""
        self._file("o§a", "CLAUDE.md")
        self._earlier_archive(self.archive)
        before = self.archive.read_bytes()
        held = self.tmp / "held.7z"
        os.link(self.archive, held)
        self.assertEqual(self._run(), 0)
        self.assertEqual(held.read_bytes(), before)
        self.assertIn("repos/o§a/CLAUDE.md", _list_archive(self.archive))

    def test_a_leftover_building_file_is_not_updated(self):
        """A killed run leaves `<archive>.building` behind, and `7zz a` updates an archive that
        it finds rather than replacing it. If the step did not remove the file before the
        pack, it would publish again a repository that left the sample after the killed run."""
        self._file("o§a", "CLAUDE.md")
        self._earlier_archive(Path(str(self.archive) + ".building"))
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        self.assertIn("repos/o§a/CLAUDE.md", listed)
        self.assertNotIn("repos/o§gone", listed)
        self.assertNotIn("repos/o§gone/CLAUDE.md", listed)

    def test_two_runs_over_the_same_tree_pack_the_same_bytes(self):
        """Removing files and emptied directories sets the modification times of their
        parents to the time of the run, and 7zz records those times. Unless the step restores
        the directory times, two runs pack different bytes for the same tree. The runs are a
        second apart, so that their times differ also on a file system that records whole
        seconds."""
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/settings.json")
        self._file("o§a", ".claude/.gitignore")
        self._file("o§a", ".claude/hooks/run.log")
        digests = []
        for attempt in range(2):
            if attempt:
                time.sleep(1.1)
            self.assertEqual(self._run(), 0)
            digests.append(hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.assertEqual(digests[0], digests[1])

    def test_a_prior_staging_dir_is_replaced(self):
        self._file("o§a", "CLAUDE.md")
        self.staging.mkdir()
        (self.staging / "leftover.txt").write_text("old")
        self.assertEqual(self._run(), 0)
        self.assertFalse(self.staging.exists())


# ---------------------------------------------------------------------------
# the archive root, and the tree that it is packed from
# ---------------------------------------------------------------------------


class TestThePackRoot(_StepCase):
    """The step packs a copy under `repos` in the staging directory and renames nothing.

    7zz records the name that it receives. The step therefore makes the copy under `repos` and
    packs it from the staging directory. Renaming the validated tree aside to `<tree>_original`
    and moving the staging copy into its place would, on a two-segment path, produce
    `data/validated_original` and move the top-level staging directory into `data/`. A hard
    kill between the two renames would leave the pruned copy in place of the validated tree,
    with the validated repositories under a name that no step reads. A copy renamed into a
    temporary directory beside the staging directory would stay there after a Stop, outside
    what the next run removes.

    The test reads the filesystem from inside a stubbed `7zz`, because the difference shows
    only during the pack. A run that finishes removes the copy either way.
    """

    def setUp(self):
        super().setUp()
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/.gitignore")
        self.seen = {}

    def _pack(self) -> int:
        """Run main() with 7zz replaced by a probe that records its arguments and what the
        filesystem holds during the pack."""
        def stub(argv, cwd=None, **kwargs):
            self.seen["argv"] = list(argv)
            self.seen["cwd"] = cwd
            self.seen["tree_is_a_dir"] = self.validated.is_dir()
            self.seen["tree_entries"] = sorted(p.name for p in self.validated.iterdir())
            self.seen["tree_still_has_the_pruned_file"] = (
                self.validated / "o§a" / ".claude" / ".gitignore").is_file()
            self.seen["siblings"] = sorted(p.name for p in self.validated.parent.iterdir())
            self.seen["pack_root_entries"] = sorted(
                p.name for p in (Path(cwd) / "repos").iterdir())
            Path(argv[-2]).write_bytes(b"7z-stub")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with _swap(_mod, "_run_7zz", stub):
            return self._run()

    def test_the_validated_tree_is_not_renamed_aside(self):
        self.assertEqual(self._pack(), 0)
        self.assertTrue(self.seen["tree_is_a_dir"])
        self.assertNotIn("validated_original", self.seen["siblings"])
        self.assertEqual(self.seen["siblings"], ["validated"])

    def test_the_validated_tree_still_contains_its_own_files_during_the_pack(self):
        """The validated tree still holds the file that the prune removes from the copy, and
        the tree is not the pruned copy under its name."""
        self.assertEqual(self._pack(), 0)
        self.assertTrue(self.seen["tree_still_has_the_pruned_file"])
        self.assertEqual(self.seen["tree_entries"], ["o§a", "repos.csv"])

    def test_7z_is_handed_the_literal_root(self):
        self.assertEqual(self._pack(), 0)
        self.assertEqual(self.seen["argv"][-1], _mod.ARCHIVE_ROOT)
        self.assertEqual(_mod.ARCHIVE_ROOT, "repos")

    def test_7z_is_handed_the_switch_to_store_links_as_links(self):
        """7zz follows a link without `-snl`, and `TestSymlinks` then fails on the archive. This
        test names the cause."""
        self.assertEqual(self._pack(), 0)
        self.assertIn("-snl", self.seen["argv"])

    def test_the_packed_directories_have_the_times_and_mode_of_the_tree(self):
        """7zz records each directory's mode and modification time. Pruning a file sets its
        parent's time to the time of the run, and two runs would then pack different bytes.
        The staging root gets the tree's mode only from the `copystat` after the copies. The
        test dates the tree in the past and gives its root a mode other than the umask's. A
        directory that the run changed or that `makedirs` created therefore differs from its
        source."""
        self._file("o§a", ".claude/settings.json")
        self._file("o§a", ".claude/hooks/run.log")      # pruned, which empties hooks/
        past = 1_000_000_000
        for d in [self.validated, *(p for p in self.validated.rglob("*") if p.is_dir())]:
            os.utime(d, (past, past))
        self.validated.chmod(0o750)
        packed = {}

        def stub(argv, cwd=None, **kwargs):
            root = Path(cwd) / _mod.ARCHIVE_ROOT
            for d in [root, *(p for p in root.rglob("*") if p.is_dir())]:
                st = d.stat()
                packed[d.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_mode)
            Path(argv[-2]).write_bytes(b"7z-stub")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with _swap(_mod, "_run_7zz", stub):
            self.assertEqual(self._run(), 0)
        self.assertEqual(sorted(packed), [".", "o§a", "o§a/.claude"])
        for rel, (mtime_ns, mode) in packed.items():
            with self.subTest(rel):
                st = (self.validated / rel).stat()
                self.assertEqual(mtime_ns, st.st_mtime_ns)
                self.assertEqual(mode, st.st_mode)

    def test_the_packed_root_is_the_cleaned_copy(self):
        """The name is pinned, and the content is still the staging copy. The archive is not
        a second copy of the validated tree under another name."""
        self.assertEqual(self._pack(), 0)
        self.assertEqual(self.seen["pack_root_entries"], ["o§a"])

    def test_7z_packs_from_the_staging_directory(self):
        """The step packs the copy where it made it. No directory outside the staging directory
        holds the copy at any moment, however large the tree."""
        self.assertEqual(self._pack(), 0)
        self.assertEqual(Path(self.seen["cwd"]), self.staging)

    def test_the_copy_is_in_the_staging_directory_and_nowhere_beside_it(self):
        """The test calls `_prune_and_pack` directly. The `finally` of `main` removes the
        staging directory, and the state after `main` returns cannot show where the copy was."""
        repo_dirs = [p.name for p in self.validated.iterdir() if p.is_dir()]
        staging = self.validated.parent / "staging_probe"
        staging.mkdir()
        archive = self.tmp / "out" / "repos_data.7z"

        def stub(argv, cwd=None, **kwargs):
            Path(argv[-2]).write_bytes(b"7z-stub")
            return subprocess.CompletedProcess(argv, 0, "", "")

        with _swap(_mod, "_run_7zz", stub), \
                contextlib.redirect_stderr(io.StringIO()):
            _mod._prune_and_pack(str(self.validated), str(staging), str(archive), repo_dirs,
                                 _mod._StopOnSignal())
        self.assertEqual(sorted(p.name for p in staging.iterdir()), [_mod.ARCHIVE_ROOT])
        self.assertTrue((staging / _mod.ARCHIVE_ROOT / "o§a" / "CLAUDE.md").is_file())
        self.assertEqual(sorted(p.name for p in staging.parent.iterdir()),
                         sorted(["validated", "staging_probe"]))
        shutil.rmtree(staging, ignore_errors=True)


# A stand-in for 7zz. It writes part of the archive that it is given and records its pid. When
# the environment sets STAND_IN_WAITS, it also waits, as a 7zz over the whole sample does.
_SLOW_7Z = """
import os, sys, time
open(sys.argv[-2], "wb").write(b"half an archive")
open(os.environ["STAND_IN_PID"], "w").write(str(os.getpid()))
if os.environ.get("STAND_IN_WAITS"):
    time.sleep(60)
"""


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestAStopDuringThePack(_StepCase):
    """A Stop sends a SIGTERM, which the runner forwards to the step while 7zz packs the sample.

    By default, a SIGTERM ends a process without running its `finally` blocks. Without the
    step's handler, the copy of the sample would stay on the volume, every later handover
    would pack it, and 7zz would go on packing. Here, the step runs as a process of its own, as
    the runner starts it, and the stand-in for 7zz waits until the signal arrives.
    """

    def _stand_in_dir(self) -> Path:
        """Write the stand-in for 7zz and return the directory to put first on PATH."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        stand_in = bin_dir / SEVEN_ZIP
        stand_in.write_text(f"#!{sys.executable}\n{_SLOW_7Z}", encoding="utf-8")
        stand_in.chmod(0o755)
        self.pid_file = self.tmp / "7z.pid"
        self.pid_file.unlink(missing_ok=True)
        return bin_dir

    def _start(self, waits: bool) -> subprocess.Popen:
        bin_dir = self._stand_in_dir()
        package_root = str(Path(_mod.__file__).resolve().parents[2])
        env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
               "PYTHONPATH": os.pathsep.join(filter(None, [package_root,
                                                           os.environ.get("PYTHONPATH")])),
               "STAND_IN_PID": str(self.pid_file)}
        if waits:
            env["STAND_IN_WAITS"] = "1"
        return subprocess.Popen(
            [sys.executable, "-m", "agentcontext.steps.collected_files_archive",
             "--validated-dir", str(self.validated), "--staging-dir", str(self.staging),
             "--archive", str(self.archive)],
            cwd=str(self.tmp), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_a_stop_leaves_no_copy_and_no_partial_archive_and_the_next_run_packs(self):
        self._file("o§a", "CLAUDE.md")
        before = sorted(p.name for p in self.tmp.iterdir())
        step = self._start(waits=True)
        self.addCleanup(lambda: step.poll() is None and step.kill())
        deadline = time.monotonic() + 30
        while not (self.pid_file.exists() and self.pid_file.read_text()):
            self.assertIsNone(step.poll(), "the step ended before 7zz started")
            self.assertLess(time.monotonic(), deadline, "7zz never started")
            time.sleep(0.05)
        packer = int(self.pid_file.read_text())
        self.addCleanup(lambda: _alive(packer) and os.kill(packer, signal.SIGKILL))
        step.send_signal(signal.SIGTERM)
        self.assertEqual(step.wait(timeout=30), 128 + signal.SIGTERM)
        self.assertFalse(_alive(packer), "7zz went on packing after the step had stopped")
        self.assertFalse(self.staging.exists(), "the staging copy was left behind")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()
                                if p.name not in ("bin", "7z.pid")), before,
                         "a copy of the sample was left beside the staging directory")
        self.assertFalse(Path(str(self.archive) + ".building").exists())
        # The runner records no stopped step, and the next Start runs the step again.
        resumed = self._start(waits=False)
        self.assertEqual(resumed.wait(timeout=60), 0)
        self.assertEqual(self.archive.read_bytes(), b"half an archive")
        self.assertFalse(self.staging.exists())

    def test_a_stop_while_7zz_starts_kills_7zz(self):
        """A stop that arrives after the process of 7zz exists and before `subprocess.Popen`
        returns it ends 7zz too. The step runs in this process, and a Popen that sends this
        process the SIGTERM at that point stands in for a signal with that timing."""
        self._file("o§a", "CLAUDE.md")
        before = sorted(p.name for p in self.tmp.iterdir())
        bin_dir = self._stand_in_dir()
        started = []

        class StoppedWhileStarting(subprocess.Popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                started.append(self)
                # The default action of SIGTERM would end the test run instead of the step.
                if signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, signal.SIG_IGN, None):
                    raise AssertionError("7zz started without the step's handler of a stop")
                os.kill(os.getpid(), signal.SIGTERM)

        env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
               "STAND_IN_PID": str(self.pid_file), "STAND_IN_WAITS": "1"}
        with mock.patch.dict(os.environ, env), \
                _swap(subprocess, "Popen", StoppedWhileStarting):
            code = self._run()
        self.assertEqual(len(started), 1)
        packer = started[0]
        self.addCleanup(lambda: packer.poll() is None and (packer.kill(), packer.wait()))
        self.assertEqual(code, 128 + signal.SIGTERM)
        self.assertIsNotNone(packer.poll(), "7zz went on packing after the step had stopped")
        self.assertFalse(self.staging.exists(), "the staging copy was left behind")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()
                                if p.name not in ("bin", "7z.pid")), before)
        self.assertFalse(Path(str(self.archive) + ".building").exists())

    def test_a_kill_leaves_the_copy_only_where_the_next_run_removes_it(self):
        """A SIGKILL runs no handler, and the copy stays. It stays inside the staging
        directory, which the next run removes before it copies, and not in a directory of its
        own beside the staging directory, where no run would remove it."""
        self._file("o§a", "CLAUDE.md")
        before = sorted(p.name for p in self.tmp.iterdir())
        step = self._start(waits=True)
        self.addCleanup(lambda: step.poll() is None and step.kill())
        deadline = time.monotonic() + 30
        while not (self.pid_file.exists() and self.pid_file.read_text()):
            self.assertIsNone(step.poll(), "the step ended before 7zz started")
            self.assertLess(time.monotonic(), deadline, "7zz never started")
            time.sleep(0.05)
        packer = int(self.pid_file.read_text())
        self.addCleanup(lambda: _alive(packer) and os.kill(packer, signal.SIGKILL))
        step.kill()
        step.wait(timeout=30)
        os.kill(packer, signal.SIGKILL)
        left = sorted(p.name for p in self.tmp.iterdir()
                      if p.name not in ("bin", "7z.pid", self.archive.name + ".building"))
        self.assertEqual(left, sorted([*before, self.staging.name]))
        resumed = self._start(waits=False)
        self.assertEqual(resumed.wait(timeout=60), 0)
        self.assertFalse(self.staging.exists())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()
                                if p.name not in ("bin", "7z.pid")),
                         sorted([*before, self.archive.name]))


# ---------------------------------------------------------------------------
# the collected file cleanup's list, which is in the tree that it lists
# ---------------------------------------------------------------------------


class TestTheValidatedListIsNotPublished(_StepCase):
    """`data/validated/repos.csv` is the one entry of the tree that is neither a repository
    nor an accident.

    The step names the list in the `expected` argument of `repos.split`, which leaves it out
    of both results. The step therefore neither copies the list into the staging directory nor
    names it in the stray warning. Without the entry in `expected`, the archive of collected
    files would publish the list as `repos/repos.csv`, and every run would log it as a stray
    in the warning about real accidents.
    """

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_the_list_is_not_in_the_archive(self):
        self._file("o§a", "CLAUDE.md")
        self.assertEqual(self._run(), 0)
        self.assertNotIn("repos/repos.csv", _list_archive(self.archive))

    def test_the_list_is_not_copied_into_the_staging_directory(self):
        """The archive test above needs 7zz. This test reads the copy itself, so that a machine
        without 7zz checks the same property too."""
        self._file("o§a", "CLAUDE.md")
        captured = {}

        def probe(argv, cwd=None, **kwargs):
            captured["entries"] = sorted(
                p.name for p in (Path(cwd) / _mod.ARCHIVE_ROOT).iterdir())
            return _fake_7z(argv, cwd=cwd, **kwargs)

        with _swap(_mod, "_run_7zz", probe):
            self.assertEqual(self._run(), 0)
        self.assertEqual(captured["entries"], ["o§a"])

    def test_the_list_is_not_reported_as_a_stray(self):
        """The test makes a real run rather than a dry run, because `--dry-run` returns before
        the split and never logs the warning that this test checks."""
        self._file("o§a", "CLAUDE.md")
        out = io.StringIO()
        argv = ["agentcontext/steps/collected_files_archive.py", "--validated-dir", str(self.validated),
                "--staging-dir", str(self.staging), "--archive", str(self.archive)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out), \
                _swap(sys, "argv", argv), _swap(_mod, "_run_7zz", _fake_7z):
            main()
        self.assertNotIn("Not publishing", out.getvalue())
        self.assertNotIn("repos.csv", out.getvalue())

    def test_the_list_is_left_where_it_is(self):
        self._file("o§a", "CLAUDE.md")
        before = self.validated_csv.read_bytes()
        self.assertEqual(self._run("--dry-run"), 0)
        self.assertEqual(self.validated_csv.read_bytes(), before)

    def test_a_stray_beside_the_list_is_still_reported(self):
        """Naming the list in `expected` must not turn the stray warning off for the other
        entries."""
        self._file("o§a", "CLAUDE.md")
        (self.validated / "input.csv").write_text("repo_name\n", encoding="utf-8")
        out = io.StringIO()
        argv = ["agentcontext/steps/collected_files_archive.py", "--validated-dir", str(self.validated),
                "--staging-dir", str(self.staging), "--archive", str(self.archive)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out), \
                _swap(sys, "argv", argv), _swap(_mod, "_run_7zz", _fake_7z):
            main()
        self.assertIn("input.csv", out.getvalue())
        self.assertIn("not repository directories", out.getvalue())


# ---------------------------------------------------------------------------
# strays: what is in the validated tree but is not a repository
# ---------------------------------------------------------------------------

class TestStrays(_StepCase):
    """A stray in the validated tree, such as an input.csv, is not a repository, and copying
    the whole tree with copytree would publish it."""

    def _stray(self, name: str) -> Path:
        p = self.validated / name
        p.write_text("repo_name,timestamp\n", encoding="utf-8")
        return p

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_a_stray_file_stays_out_of_the_archive(self):
        self._file("o§a", "CLAUDE.md")
        self._stray("input.csv")
        self.assertEqual(self._run(), 0)
        self.assertNotIn("repos/input.csv", _list_archive(self.archive))

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_the_repositories_beside_it_are_still_published(self):
        self._file("o§a", "CLAUDE.md")
        self._stray("input.csv")
        self.assertEqual(self._run(), 0)
        self.assertIn("repos/o§a/CLAUDE.md", _list_archive(self.archive))

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_a_stray_directory_stays_out_too(self):
        self._file("o§a", "CLAUDE.md")
        (self.validated / "leftover_staging" / "o§a").mkdir(parents=True)
        (self.validated / "leftover_staging" / "o§a" / "CLAUDE.md").write_text("x\n")
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        self.assertNotIn("repos/leftover_staging", listed)

    def _stray_directory(self) -> Path:
        """Write a stray directory with a file that the prune would remove inside a repository
        directory."""
        p = self.validated / "leftover" / ".gitignore"
        p.parent.mkdir()
        p.write_text("*.log\n", encoding="utf-8")
        return p

    def _output(self, *extra) -> str:
        """Run main() with 7zz stubbed and return what it logged."""
        out = io.StringIO()
        argv = ["--validated-dir", str(self.validated), "--staging-dir", str(self.staging),
                "--archive", str(self.archive), *extra]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out), \
                _swap(_mod, "_run_7zz", _fake_7z):
            self.assertIsNone(main(argv))
        return out.getvalue()

    def test_a_stray_is_left_where_it_is(self):
        """The test makes a real run, because `--dry-run` returns before the split, and a dry
        run would pass even if the step deleted every stray."""
        self._file("o§a", "CLAUDE.md")
        stray = self._stray("input.csv")
        stray_file = self._stray_directory()
        before = (stray.read_bytes(), stray_file.read_bytes())
        self._output()
        self.assertEqual((stray.read_bytes(), stray_file.read_bytes()), before)

    def test_a_stray_is_reported_by_name(self):
        self._file("o§a", "CLAUDE.md")
        self._stray_directory()
        lines = [line for line in self._output().splitlines()
                 if line.startswith("INFO collected_files_archive: Not publishing")]
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith(" directories (leftover)."), lines[0])

    def test_a_stray_is_never_proposed_for_deletion(self):
        """The dry run reads only the repository directories. The stray here is a directory
        with a file that matches a prune glob, because a stray file matches none, and with a
        stray file the test could not fail."""
        self._file("o§a", "CLAUDE.md")
        self._stray_directory()
        out = self._output("--dry-run")
        self.assertNotIn("leftover", out)
        self.assertIn("Would delete 0 files", out)


class TestEncodedSessionDirectoriesArePruned(_StepCase):
    """Claude Code names a session directory after the absolute path of the working directory
    that the session ran in. The name of such a directory discloses a developer's local
    filesystem path, and the prune therefore removes the whole directory, not only the session
    transcripts inside it. A directory of .claude/projects/ with another name is the project's
    own, and the step publishes its files."""

    LEAKY = [".claude/projects/-Users-alice-work-secret/notes.md",
             ".claude/projects/-home-bob-client/state.json",
             ".claude/projects/C--Users-carol-repo/notes.md",
             ".claude/projects/-Users-alice-work-secret/session.jsonl"]
    KEPT = [".claude/projects/shared-prompts/notes.md",
            ".claude/projects/team/notes.md",
            ".claude/projects/team/examples.jsonl"]

    def test_a_unix_encoded_directory_is_pruned(self):
        for rel in self.LEAKY[:2]:
            self.assertTrue(is_prunable(rel), rel)

    def test_a_windows_encoded_directory_is_pruned(self):
        self.assertTrue(is_prunable(self.LEAKY[2]))

    def test_a_deliberate_projects_directory_is_kept(self):
        for rel in self.KEPT:
            self.assertFalse(is_prunable(rel), rel)

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_no_encoded_directory_reaches_the_archive(self):
        for rel in self.LEAKY:
            self._file("o§a", rel)
        for rel in self.KEPT:
            self._file("o§a", rel)
        self.assertEqual(self._run(), 0)
        listed = "\n".join(_list_archive(self.archive))
        self.assertNotIn("-Users-alice-work-secret", listed)
        self.assertNotIn("-home-bob-client", listed)
        self.assertNotIn("C--Users-carol-repo", listed)
        self.assertIn("shared-prompts", listed)


# ---------------------------------------------------------------------------
# symlinks: what a checked-out repository can contain, and what copying it does
# ---------------------------------------------------------------------------

@unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
class TestSymlinks(_StepCase):
    """The validated tree contains symlinks, among them dangling links: a link committed to a
    path that is not in the repository, or to an absolute path on the author's machine. The
    staging copy has to keep a link as a link. Following a dangling link stops the whole
    publication, and following a link to a directory copies the directory's contents under
    the link's path.

    The archive assertions rely on the archiver storing a link rather than following it.
    `-snl` requests this behavior, and the published archive of collected files shows it for
    all of its links. 7zz follows a link without the switch, and these tests fail with the switch
    removed. `TestThePackRoot` also asserts that the step passes `-snl`.
    """

    def _link(self, repo_dir: str, rel: str, target: str) -> Path:
        p = self.validated / repo_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)
        return p

    def test_a_dangling_symlink_still_produces_the_archive(self):
        self._file("o§a", "CLAUDE.md")
        self._link("o§a", ".github/agents/release.agent.md",
                   ".claude/agents/release.agent.md")
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        self.assertIn("repos/o§a/CLAUDE.md", listed)
        self.assertIn("repos/o§a/.github/agents/release.agent.md", listed)
        self.assertIn("l", _archive_attributes(self.archive)[
            "repos/o§a/.github/agents/release.agent.md"])

    def test_a_directory_symlink_is_not_expanded(self):
        self._file("o§a", "skills/real/standards/note.md")
        self._link("o§a", "skills/copy/standards", "../real/standards")
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        self.assertIn("repos/o§a/skills/copy/standards", listed)
        self.assertNotIn("repos/o§a/skills/copy/standards/note.md", listed)
        self.assertIn("l", _archive_attributes(self.archive)[
            "repos/o§a/skills/copy/standards"])


@unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
class TestFileManagerEntriesAreNotPublished(_StepCase):
    """Synology DSM writes an `@eaDir` with thumbnails into every directory that File Station
    shows, and Finder writes a `.DS_Store`. The collected file cleanup deletes both from the
    validated tree. A visit after the cleanup, such as after a failed archive or release step,
    writes them again before the next run of this step. If the step copied them, the archive
    of collected files would publish them."""


    def test_the_directories_left_out_are_those_that_every_tree_comparison_discards(self):
        from agentcontext import paths
        self.assertEqual(_mod.FILE_MANAGER_DIRECTORIES, paths.DISCARDED_DIRECTORIES)
    def _thumbnail(self, repo_dir: str, rel_dir: str) -> Path:
        p = self.validated / repo_dir / rel_dir / "@eaDir" / "x.png" / "SYNOFILE_THUMB_M.jpg"
        p.parent.mkdir(parents=True)
        p.write_text("thumbnail\n", encoding="utf-8")
        return p

    def test_a_thumbnail_directory_leaves_no_archive_entry(self):
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/skills/pdf/SKILL.md")
        self._thumbnail("o§a", "")
        self._thumbnail("o§a", ".claude/skills/pdf")
        before = self._validated_snapshot()
        self.assertEqual(self._run(), 0)
        listed = _list_archive(self.archive)
        self.assertEqual([path for path in listed if "@eaDir" in path], [])
        self.assertIn("repos/o§a/CLAUDE.md", listed)
        self.assertIn("repos/o§a/.claude/skills/pdf/SKILL.md", listed)
        self.assertEqual(self._validated_snapshot(), before, "the step modified the validated tree")

    def test_no_name_that_the_runner_discards_is_published(self):
        """The test writes every name that the runner leaves out of the manifest because the
        cleanup discards it. Each name has the form in which the cleanup discards it: a
        directory for `DISCARDED_DIRECTORIES` and a file for `DISCARDED_NAMES`."""
        from agentcontext import runner
        self._file("o§a", "CLAUDE.md")
        for name in sorted(runner.DISCARDED_DIRECTORIES):
            self._file("o§a", f".claude/{name}/thumbnail.jpg")
        for name in sorted(runner.DISCARDED_NAMES):
            self._file("o§a", f".claude/{name}")
        self.assertEqual(self._run(), 0)
        names = runner.DISCARDED_DIRECTORIES | runner.DISCARDED_NAMES
        published = [path for path in _list_archive(self.archive)
                     if names & set(path.split("/"))]
        self.assertEqual(published, [])
        self.assertIn("repos/o§a/CLAUDE.md", _list_archive(self.archive))

    def test_a_link_with_the_name_is_published_as_a_link(self):
        """The cleanup deletes only a directory with the name and judges a link with the name
        as any other entry. A link that reached the validated tree counts as a file of the
        repository."""
        self._file("o§a", "docs/a.md")
        os.symlink("docs", self.validated / "o§a" / "@eaDir")
        self.assertEqual(self._run(), 0)
        self.assertIn("l", _archive_attributes(self.archive)["repos/o§a/@eaDir"])

    def _with_dsm(self, before, where: list) -> int:
        """Run the step, with DSM writing an `@eaDir` into each directory of `where` right
        before the step calls `before`. The paths of `where` are relative to the root of the
        staging copy, and `before` names a function as `(module, name)`."""
        module, name = before
        real = getattr(module, name)

        def dsm_first(*args, **kwargs):
            for rel in where:
                eadir = self.staging / _mod.ARCHIVE_ROOT / rel / "@eaDir"
                eadir.mkdir(parents=True, exist_ok=True)
                (eadir / "x@SynoEAStream").write_text("attributes\n", encoding="utf-8")
            return real(*args, **kwargs)

        with _swap(module, name, dsm_first):
            return self._run()

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_an_eadir_that_dsm_writes_into_the_staging_copy_before_the_pack_is_not_packed(self):
        """DSM also writes into the staging directory, after the copy has left out every
        `@eaDir` of the validated tree."""
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/skills/pdf/SKILL.md")
        self.assertEqual(self._with_dsm((_mod, "_run_7zz"),
                                        ["o§a", "o§a/.claude/skills/pdf"]), 0)
        listed = _list_archive(self.archive)
        self.assertEqual([path for path in listed if "@eaDir" in path], [])
        self.assertIn("repos/o§a/.claude/skills/pdf/SKILL.md", listed)

    @unittest.skipUnless(_HAS_7Z, "7zz not on PATH")
    def test_an_eadir_that_dsm_writes_before_the_prune_leaves_the_archive_of_a_run_without(self):
        """DSM writes the `@eaDir` after the copy and before the prune. In a directory that the
        prune empties, an `@eaDir` would keep the directory. The step reads the times of the
        copy's directories from the validated tree, which has no `@eaDir`."""
        self._file("o§a", "CLAUDE.md")
        self._file("o§a", ".claude/settings.json")
        self._file("o§a", ".claude/hooks/run.log")
        self.assertEqual(self._run(), 0)
        without = _list_archive(self.archive)
        self.assertNotIn("repos/o§a/.claude/hooks", without)
        self.assertEqual(self._with_dsm((_mod, "prunable_files"),
                                        ["o§a", "o§a/.claude/hooks"]), 0)
        self.assertEqual(_list_archive(self.archive), without)


class TestThePackagingCommands(unittest.TestCase):
    """The 7zz commands of the four replication archives in docs/development.md, which no other
    test reads.

    This script archives the sample. The shell commands of the replication packages leave out
    the `.claude` settings directory of a stage's own code. Excluding `.claude` recursively
    would also drop the `.claude` directory of every collected repository, the largest
    mechanism in the dataset, from the stage 3 archive.
    """

    GUIDE = REPO_ROOT / "docs" / "development.md"
    # Each archive with what it packs: a stage's code directory and its data directory. The
    # archive of the collection stage, with the package as its code, packs its data directory
    # alone. Each command leaves out the `.claude` of the first member.
    STAGES = (("1_sampling", "1_sampling data/sampling"),
              ("2_classification", "2_classification data/classification"),
              ("3_data-collection", "data/collection"),
              ("4_data-analysis", "4_data-analysis data/analysis"))

    @classmethod
    def setUpClass(cls):
        if not cls.GUIDE.is_file():
            raise unittest.SkipTest("docs/development.md is not beside this checkout")
        cls.lines = [line for line in cls.GUIDE.read_text(encoding="utf-8").splitlines()
                     if line.startswith("7zz a ")]

    def test_the_four_commands_are_there(self):
        self.assertEqual(len(self.lines), 4,
                         "docs/development.md does not contain four 7zz commands")
        for (name, members), line in zip(self.STAGES, self.lines):
            self.assertIn(f"7zz a {name}.7z {members} ", line)

    def test_no_command_excludes_dot_claude_by_name(self):
        """`-xr` matches at any depth. `-x` is anchored to the archive root."""
        for (name, members), line in zip(self.STAGES, self.lines):
            with self.subTest(stage=name):
                self.assertNotIn("-xr'!.claude'", line)
                self.assertIn(f"-x'!{members.split()[0]}/.claude'", line)

    def test_the_names_that_stay_recursive_do(self):
        """`__pycache__` and `.pytest_cache` cannot be a configuration artifact, and
        `.DS_Store` is noise wherever it is. The commands exclude these names at any depth."""
        for line in self.lines:
            for name in (".DS_Store", "__pycache__", ".pytest_cache"):
                self.assertIn(f"-xr'!{name}'", line)


class TestAnEmptiedTree(unittest.TestCase):
    """An operator may delete the repository directories under `data/validated/` to save space
    and leave the tracked list. No earlier step detects the deletion, because the runner
    records `collected_file_cleanup` by its script rather than by its output. Without the
    refusal, this step would pack an archive of no repositories and exit 0, and `release`
    would write the Zenodo README and the checksum manifest around it."""

    def test_a_validated_tree_with_no_repository_directory_is_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        validated = tmp / "validated"
        validated.mkdir()
        (validated / "repos.csv").write_text("repo_name\n", encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            _mod.main(["--validated-dir", str(validated),
                       "--staging-dir", str(tmp / "staging"),
                       "--archive", str(tmp / "repos_data.7z")])
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("no repository directory", err.getvalue())
        self.assertFalse((tmp / "repos_data.7z").exists())


if __name__ == "__main__":
    unittest.main()
