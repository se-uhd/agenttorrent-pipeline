"""Tests for agentcontext.checks.snapshot: the record and its lock, the `write` subcommand
over every derivable section and its `--check`, and the `validation` subcommand against a fake
GitHub.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_checks_snapshot -v
"""

import argparse
import contextlib
import importlib
import csv
import datetime
import io
import json
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from unittest import mock
from pathlib import Path

from agentcontext import STAGE_DIR, heuristics
from agentcontext.checks import snapshot as _mod
from agentcontext.checks.snapshot import load, update
from tests._fixtures import report_counts

def _reap(proc):
    """Stop a helper process and wait for it, so that no test leaves a helper process running."""
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    if proc.stdin is not None:
        proc.stdin.close()


PROV = {"in.csv": {"rows": 1}}

def _write_csv(path: Path, rows: list, header: list = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = header or (list(rows[0]) if rows else [])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)

def _artifact_column(stem):
    """The column with which the CSV `<stem>.csv` names its configuration artifact.

    The lookup goes by filename, as in `metadata_counts`, because `_mod.MECHANISMS` contains
    the filename stems and a stem is not a mechanism key of the heuristics.
    """
    return heuristics.artifact_column_for_file(f"{stem}.csv")

def _named(row, column):
    """A fixture row with its placeholder key `filepath` renamed to the real
    configuration artifact column."""
    return {(column if k == "filepath" else k): v for k, v in row.items()}

def _body(obj):
    return json.dumps(obj).encode()

def _compare(status, base_date=None):
    payload = {"status": status}
    if base_date is not None:
        payload["base_commit"] = {"commit": {"committer": {"date": base_date}}}
    return payload

def _client(routes=None, default=None, tokens=("t1",), **kwargs):
    transport = FakeTransport(routes or {}, *( [default] if default else [] ))
    rotator = _mod.TokenRotator(list(tokens), sleep=lambda s: None, now=lambda: 1000.0)
    client = _mod.GitHubClient(rotator, transport=transport, sleep=lambda s: None,
                               now=lambda: 1000.0, **kwargs)
    return client, transport

def _row(**overrides):
    """One row of `data/collected/repos.csv`, with the metadata columns that the heuristics name."""
    row = {"repo_name": "owner/repo", "scanned_at": "2026-03-29",
           "branch": "main", "head_sha": "a" * 40,
           "head_commit_date": "2026-03-29T02:27:29Z", "first_commit_sha": "b" * 40,
           "claude": "TRUE"}
    row.update(overrides)
    return row


_ambient_env = importlib.import_module("tests._ambient_env")
_saved_credentials = {}


def setUpModule():
    # `main_validate` adds an ambient GITHUB_TOKEN or GITHUB_TOKENS to the GitHub access tokens
    # that it collects. Without this setup, the validation tests would run with the machine's
    # GitHub access tokens beside their own.
    _ambient_env.take(_saved_credentials)


def tearDownModule():
    _ambient_env.restore(_saved_credentials)


class _SnapshotCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "output" / "snapshot.json"


class TestLoad(_SnapshotCase):
    def test_a_missing_file_loads_as_empty(self):
        self.assertEqual(load(self.path), {})

    def test_it_reads_back_what_was_written(self):
        update("collection", {"repositories": 4799}, self.path, derived_from=PROV)
        self.assertEqual(load(self.path),
                         {"collection": {"repositories": 4799, "derived_from": PROV}})


class TestUpdate(_SnapshotCase):
    def test_it_creates_the_output_directory(self):
        update("collection", {"a": 1}, self.path, derived_from=PROV)
        self.assertTrue(self.path.is_file())

    def test_it_leaves_other_sections_alone(self):
        update("collection", {"repositories": 4799}, self.path, derived_from=PROV)
        update("validation", {"ok": 4700}, self.path, derived_from=PROV)
        snapshot = load(self.path)
        self.assertEqual(snapshot["collection"], {"repositories": 4799, "derived_from": PROV})
        self.assertEqual(snapshot["validation"], {"ok": 4700, "derived_from": PROV})

    def test_it_replaces_its_own_section_rather_than_merging(self):
        # A writer that stops emitting a key must not leave the stale value
        # behind as if it were current.
        update("validation", {"ok": 1, "gone": 2}, self.path, derived_from=PROV)
        update("validation", {"ok": 3}, self.path, derived_from=PROV)
        self.assertEqual(load(self.path)["validation"], {"ok": 3, "derived_from": PROV})

    def test_it_returns_the_whole_snapshot(self):
        update("collection", {"a": 1}, self.path, derived_from=PROV)
        self.assertEqual(update("validation", {"b": 2}, self.path, derived_from=PROV),
                         {"collection": {"a": 1, "derived_from": PROV},
                          "validation": {"b": 2, "derived_from": PROV}})

    def test_the_file_is_sorted_and_newline_terminated(self):
        update("zulu", {"z": 1}, self.path, derived_from=PROV)
        update("alpha", {"a": 1}, self.path, derived_from=PROV)
        text = self.path.read_text()
        self.assertTrue(text.endswith("\n"))
        self.assertLess(text.index('"alpha"'), text.index('"zulu"'))

    def test_it_leaves_no_temporary_file_behind(self):
        # The lock file stays by design. A temporary file would mean that writing did not
        # finish. TestTheKernelOwnsTheLock covers the lock file itself.
        update("collection", {"a": 1}, self.path, derived_from=PROV)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()
                                if p.suffix != ".lock"),
                         ["snapshot.json"])

    def test_the_data_and_the_rename_are_synced_to_disk(self):
        """A rename that reaches the disk before the data leaves an empty file after a power
        loss. `load` refuses an empty file, and the snapshot step would then fail at every later
        Start of a data root that git cannot restore."""
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            info = os.fstat(fd)
            events.append(("sync", stat.S_ISDIR(info.st_mode), info.st_ino, info.st_size))
            real_fsync(fd)

        def replace(source, destination):
            events.append(("rename",))
            real_replace(source, destination)

        with mock.patch("os.fsync", fsync), mock.patch("os.replace", replace):
            update("collection", {"a": 1}, self.path, derived_from=PROV)
        written = self.path.stat()
        self.assertEqual(events, [("sync", False, written.st_ino, written.st_size),
                                  ("rename",),
                                  ("sync", True, self.path.parent.stat().st_ino, mock.ANY)])

    def test_the_result_is_valid_json(self):
        update("collection", {"window": {"first": "2026-03-29T17:24:38Z"}}, self.path, derived_from=PROV)
        json.loads(self.path.read_text())


class TestAFailedUpdateLeavesTheRecordAsItWas(_SnapshotCase):
    """An `update` that fails must leave the file as it was, because the record is tracked and
    contains sections that no run can rebuild, above all the validation section."""

    def _fails(self, failure) -> None:
        update("collection", {"a": 1}, self.path, derived_from=PROV)
        before = self.path.read_bytes()
        with failure, self.assertRaises(OSError) as caught:
            update("validation", {"b": 2}, self.path, derived_from=PROV)
        self.assertEqual(caught.exception.errno, 28)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()),
                         ["snapshot.json", "snapshot.json.lock"])

    def test_a_disk_that_fills_while_the_record_is_written(self):
        real = tempfile.NamedTemporaryFile

        def half_then_full(*args, **kwargs):
            handle = real(*args, **kwargs)
            real_write = handle.write

            def write(text):
                real_write(text[:len(text) // 2])
                handle.flush()
                raise OSError(28, "No space left on device")
            handle.write = write
            return handle

        self._fails(mock.patch.object(_mod.tempfile, "NamedTemporaryFile", half_then_full))

    def test_a_failed_rename(self):
        self._fails(mock.patch.object(_mod.os, "replace",
                                      side_effect=OSError(28, "No space left on device")))


class TestTheFileKeepsItsPermissions(_SnapshotCase):
    """The replacement is a NamedTemporaryFile, which is always created at 0600, and os.replace
    keeps that mode on the destination. A snapshot.json changed to 0600 appears in no diff and
    in no status, because git records only the exec bit, but the replication archive stores the
    mode."""

    def test_it_keeps_the_mode_of_the_file_it_replaces(self):
        update("collection", {"a": 1}, self.path, derived_from=PROV)
        os.chmod(self.path, 0o644)
        update("validation", {"b": 2}, self.path, derived_from=PROV)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)

    def test_a_new_file_gets_the_mode_of_a_plainly_written_file(self):
        # The test fixes the umask, because under a restrictive umask the probe is 0600 too and
        # the comparison would pass with the defect present. The test assumes that the directory
        # has no default ACL, which would apply its own mask instead of the umask.
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        update("collection", {"a": 1}, self.path, derived_from=PROV)
        probe = self.path.parent / "probe.txt"
        probe.write_text("x", encoding="utf-8")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode),
                         stat.S_IMODE(probe.stat().st_mode))


class TestConcurrentWriters(unittest.TestCase):
    """update() is a read-modify-write, and the validation run spends a long time on network
    requests before it writes. Without a lock, a writer that loaded first silently drops what
    another writer wrote, and two writers with one fixed temporary name interleave into a
    corrupt file."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "snapshot.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_section_survives_eight_writers(self):
        code = (f"import sys\n"
                f"from agentcontext.checks import snapshot\n"
                f"snapshot.update(sys.argv[1], {{'v': sys.argv[1]}}, {str(self.path)!r},\n"
                f"                  derived_from={{'in.csv': {{'rows': 1}}}})\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, f"s{i}"]) for i in range(8)]
        for p in procs:
            self.assertEqual(p.wait(), 0)
        written = _mod.load(self.path)
        self.assertEqual(sorted(written), [f"s{i}" for i in range(8)])

    def test_only_the_lock_file_is_left_behind(self):
        """The lock file stays by design, and a temporary file does not. A temporary file would
        mean that writing did not finish, and the next reader would find two candidate
        snapshots."""
        _mod.update("a", {"v": 1}, self.path, derived_from=PROV)
        leftovers = sorted(p.name for p in self.tmp.iterdir() if p.name != "snapshot.json")
        self.assertEqual(leftovers, ["snapshot.json.lock"])

    def test_the_lock_file_is_never_written_to(self):
        """Nothing writes the lock file's contents, because nothing reads them. Opening the file
        for writing would turn a lock file that this user cannot write into a permanent failure
        rather than a lock that the user can take."""
        _mod.update("a", {"v": 1}, self.path, derived_from=PROV)
        self.assertEqual((self.tmp / "snapshot.json.lock").stat().st_size, 0)


class TestACorruptFileIsNotSilentlyReplaced(unittest.TestCase):
    """load() returning an empty snapshot for an unreadable file would let the next writer
    overwrite the whole record with one section."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "snapshot.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unreadable_json_raises(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(_mod.SnapshotError):
            _mod.load(self.path)

    def test_a_json_array_is_not_a_snapshot(self):
        self.path.write_text("[]", encoding="utf-8")
        with self.assertRaises(_mod.SnapshotError):
            _mod.load(self.path)

    def test_the_file_is_left_alone(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(_mod.SnapshotError):
            _mod.update("a", {"v": 1}, self.path, derived_from=PROV)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")


class TestASectionMustSayWhatItWasBuiltFrom(unittest.TestCase):
    """A writer that read two rows of a large file and recorded a summary of them produces a
    section indistinguishable from a complete section. `update` therefore refuses a payload with no
    provenance rather than relying on each writer to check."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "snapshot.json"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_an_update_without_provenance_is_refused(self):
        with self.assertRaises(TypeError):
            _mod.update("a", {"v": 1}, self.path)

    def test_nothing_is_written_when_it_is_refused(self):
        with self.assertRaises(ValueError):
            _mod.update("a", {"v": 1}, self.path, derived_from={})
        self.assertFalse(self.path.exists())

    def test_the_provenance_is_written_into_the_section(self):
        _mod.update("a", {"v": 1}, self.path, derived_from={"f.csv": {"rows": 3}})
        self.assertEqual(_mod.load(self.path)["a"]["derived_from"], {"f.csv": {"rows": 3}})

    def test_the_payload_the_caller_passed_is_not_mutated(self):
        payload = {"v": 1}
        _mod.update("a", payload, self.path, derived_from={"f.csv": {"rows": 3}})
        self.assertEqual(payload, {"v": 1})

    def test_a_count_must_be_an_integer_so_a_later_run_can_compare_it(self):
        for bad in ({"f": {"rows": "many"}}, {"f": {"rows": 1.5}}, {"f": {"rows": True}}):
            with self.assertRaises(ValueError, msg=bad):
                _mod.check_derived_from(bad)

    def test_an_input_must_name_a_unit_and_a_count(self):
        for bad in ({"f": {}}, {"f": 1}, {"f": None}, {"": {"rows": 1}}, []):
            with self.assertRaises(ValueError, msg=bad):
                _mod.check_derived_from(bad)

    def test_a_well_formed_mapping_is_returned_unchanged(self):
        good = {"a.csv": {"rows": 10}, "b/": {"repositories": 2, "files": 30}}
        self.assertEqual(_mod.check_derived_from(good), good)


class TestTheCommittedSnapshotRecordsProvenance(unittest.TestCase):
    """These tests read the committed file, so that a section added by hand cannot bypass the
    checks of update()."""

    def setUp(self):
        if not _mod.SNAPSHOT_PATH.is_file():
            self.skipTest("output/snapshot.json has not been generated")
        self.snapshot = _mod.load()

    def test_every_section_records_what_it_was_built_from(self):
        missing = sorted(k for k, v in self.snapshot.items() if not v.get("derived_from"))
        self.assertEqual(missing, [], f"sections with no derived_from: {missing}")

    def test_every_recorded_provenance_is_well_formed(self):
        for name, section in self.snapshot.items():
            _mod.check_derived_from(section["derived_from"])

    def test_there_is_more_than_one_section_to_check(self):
        self.assertTrue({"collection", "configured", "metadata"} <= self.snapshot.keys())


class TestTheKernelOwnsTheLock(unittest.TestCase):
    """The lock is an flock. The kernel releases it when its holder exits, and no waiter has to
    break it. A waiter that breaks a lock has to judge the lock stale from its age, and that
    stat-then-rename is not one operation. Two waiters can both break the same lock, a third
    writer can take the freed path, and the second breaker can then displace that live holder.
    Two writers are then inside the read-modify-write, and one writer's section is lost."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "snapshot.json"
        self.lock = self.tmp / "snapshot.json.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _holder(self):
        """Start a process that takes the lock and holds it, and return the process once it
        holds the lock.

        The process writes a marker file once it holds the lock, so that killing it kills a
        holder rather than a process that is still starting. A sleep could only estimate its
        progress. The process holds the lock until it is killed or its standard input reaches
        its end, as it does when the test process exits. A holder that let go after a fixed time
        would release the lock to writers that are still starting.

        The deadline only catches a holder that hangs. The holder starts an interpreter that
        imports the package, and a loaded runner can take a long time for that. A holder that
        exits fails the test at once.
        """
        held = self.tmp / "held"
        code = (f"import sys\n"
                f"from agentcontext.checks import snapshot\n"
                f"from pathlib import Path\n"
                f"with snapshot._locked(Path({str(self.path)!r})):\n"
                f"    Path({str(held)!r}).write_text('x')\n"
                f"    sys.stdin.read()\n")
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE)
        self.addCleanup(_reap, proc)
        deadline = time.monotonic() + 600
        while not held.exists():
            self.assertLess(time.monotonic(), deadline, "the holder never took the lock")
            self.assertIsNone(proc.poll(), "the holder exited instead of holding")
            time.sleep(0.005)
        return proc

    def test_a_live_holder_is_waited_for_and_the_wait_is_bounded(self):
        self._holder()
        original = _mod.LOCK_TIMEOUT_SECONDS
        _mod.LOCK_TIMEOUT_SECONDS = 0.2
        try:
            with self.assertRaises(TimeoutError) as caught:
                _mod.update("a", {"v": 1}, self.path, derived_from=PROV)
        finally:
            _mod.LOCK_TIMEOUT_SECONDS = original
        self.assertIn("0.2s", str(caught.exception))

    def test_a_killed_holder_frees_the_lock_at_once(self):
        """The kernel releases the lock when the process is killed, without an age bound or a
        break."""
        proc = self._holder()
        proc.kill()
        proc.wait()
        original = _mod.LOCK_TIMEOUT_SECONDS
        _mod.LOCK_TIMEOUT_SECONDS = 0.5
        try:
            _mod.update("a", {"v": 1}, self.path, derived_from=PROV)
        finally:
            _mod.LOCK_TIMEOUT_SECONDS = original
        self.assertEqual(_mod.load(self.path), {"a": {"v": 1, "derived_from": PROV}})

    def test_two_threads_of_one_process_exclude_each_other(self):
        """The snapshot validation runs a thread pool. An flock belongs to the open file
        description, and the two threads therefore exclude each other. A lock of fcntl.lockf
        belongs to the process and would let both threads in."""
        entered, release, blocked = threading.Event(), threading.Event(), []

        # The holding thread holds the lock until the attempt below has ended, however long the
        # attempt takes. The bound of the wait only catches a test that never releases it.
        def hold():
            with _mod._locked(self.path):
                entered.set()
                release.wait(600)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(30), "the holding thread never took the lock")
        original = _mod.LOCK_TIMEOUT_SECONDS
        _mod.LOCK_TIMEOUT_SECONDS = 0.1
        try:
            with _mod._locked(self.path):
                blocked.append("granted")
        except TimeoutError:
            blocked.append("refused")
        finally:
            _mod.LOCK_TIMEOUT_SECONDS = original
        self.assertEqual(blocked, ["refused"])

    def test_a_lock_file_replaced_while_a_writer_waits_does_not_let_two_writers_in(self):
        """A waiter that opened the lock file before the file was replaced locks an inode with no
        name. The next writer creates a new file at the same path and locks that file, and both
        writers run the critical section at the same time. The test stops the waiter at that
        point. The waiter has its descriptor on the first inode, the test replaces the path, and
        only then kills the holder, which grants the waiter the lock on the orphaned inode. A
        third writer that is still refused shows that the re-check ran.
        """
        holder = self._holder()
        opened, entered = self.tmp / "opened", self.tmp / "entered"
        # The waiter writes the inode that it opened from inside _open_lock_file. The test
        # therefore knows when the waiter is queued and does not estimate it from a sleep. The
        # waiter writes the inode under another name and renames the file into place, so that
        # the file appears with the inode in it.
        code = (f"import sys, time\n"
                f"from agentcontext.checks import snapshot\n"
                f"from pathlib import Path\n"
                f"snapshot.LOCK_TIMEOUT_SECONDS = 60.0\n"
                f"import os\n"
                f"_original = snapshot._open_lock_file\n"
                f"def _traced(lock):\n"
                f"    fd = _original(lock)\n"
                f"    Path({str(opened)!r} + '.partial').write_text(str(os.fstat(fd).st_ino))\n"
                f"    os.replace({str(opened)!r} + '.partial', {str(opened)!r})\n"
                f"    return fd\n"
                f"snapshot._open_lock_file = _traced\n"
                f"with snapshot._locked(Path({str(self.path)!r})):\n"
                f"    Path({str(entered)!r}).write_text('x')\n"
                f"    time.sleep(60)\n")
        waiter = subprocess.Popen([sys.executable, "-c", code])
        self.addCleanup(_reap, waiter)
        # As in `_holder`, the deadline only catches a waiter that hangs while it starts.
        deadline = time.monotonic() + 600
        while not opened.exists():
            self.assertLess(time.monotonic(), deadline, "the waiter never opened the lock file")
            self.assertIsNone(waiter.poll(), "the waiter exited before it opened the lock file")
            time.sleep(0.005)
        first = int(opened.read_text())
        self.assertEqual(first, self.lock.stat().st_ino)
        self.assertFalse(entered.exists(), "the waiter entered while the holder still holds the lock")

        os.unlink(self.lock)                 # what `git clean -xdf` does to an ignored file
        os.close(os.open(self.lock, os.O_RDONLY | os.O_CREAT, 0o666))
        self.assertNotEqual(self.lock.stat().st_ino, first)
        holder.kill()                        # the waiter is now granted the orphaned inode
        holder.wait()
        while not entered.exists():
            self.assertLess(time.monotonic(), deadline, "the waiter never entered")
            self.assertIsNone(waiter.poll(), "the waiter exited before it entered")
            time.sleep(0.005)

        # The waiter holds a lock. Only the re-check determines whether that lock is on the file
        # at the path, and only the re-check makes this third writer wait.
        original = _mod.LOCK_TIMEOUT_SECONDS
        _mod.LOCK_TIMEOUT_SECONDS = 1.5
        try:
            with self.assertRaises(TimeoutError):
                with _mod._locked(self.path):
                    pass
        finally:
            _mod.LOCK_TIMEOUT_SECONDS = original

    def test_the_lock_file_is_tracked_by_git_so_git_clean_leaves_it_alone(self):
        """`git clean -xdf` removes ignored files and keeps tracked files. Removing the lock file
        while a writer holds it is the only remaining way to put two writers inside the critical
        section, and the file is therefore committed rather than ignored.

        The test skips rather than fails where there is no checkout to query. Only git can
        determine whether a file is tracked, and the replication archive is extracted without a
        repository, where this suite should still pass.
        """
        stage = STAGE_DIR
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                capture_output=True, text=True, cwd=str(stage))
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            self.skipTest("not a git checkout, so nothing here can be tracked")
        real = stage / "output" / "snapshot.json.lock"
        listed = subprocess.run(["git", "ls-files", "--error-unmatch", str(real)],
                                capture_output=True, text=True, cwd=str(stage))
        self.assertEqual(listed.returncode, 0,
                         f"{real} is not tracked: git clean -xdf would remove it")

    def test_a_lock_directory_from_the_old_design_is_named_rather_than_removed(self):
        """The module names the directory in an error instead of removing it, because deciding
        from the outside that nobody uses the directory can lose sections."""
        self.lock.mkdir()
        with self.assertRaises(_mod.SnapshotError) as caught:
            _mod.update("a", {"v": 1}, self.path, derived_from=PROV)
        self.assertIn("is a directory", str(caught.exception))
        self.assertTrue(self.lock.is_dir())

    def test_eight_writers_racing_a_dead_holders_lock_keep_eight_sections(self):
        """Every writer is queued on the lock before the test kills the holder. All eight writers
        then compete for the released lock, and every section is kept."""
        holder = self._holder()
        ready = self.tmp / "ready"
        ready.mkdir()
        # Each writer waits for the lock under a timeout that only catches a hang, because the
        # first writer to queue waits until the last one has started and the holder is killed.
        code = (f"import sys\n"
                f"from pathlib import Path\n"
                f"from agentcontext.checks import snapshot\n"
                f"snapshot.LOCK_TIMEOUT_SECONDS = 600.0\n"
                f"Path({str(ready)!r}, sys.argv[1]).write_text('x')\n"
                f"snapshot.update(sys.argv[1], {{'v': sys.argv[1]}}, {str(self.path)!r},\n"
                f"                 derived_from={{'in.csv': {{'rows': 1}}}})\n")
        procs = [subprocess.Popen([sys.executable, "-c", code, f"s{i}"]) for i in range(8)]
        for proc in procs:
            self.addCleanup(_reap, proc)
        # The deadline only catches a writer that hangs before the lock, and it leaves a loaded
        # runner time to start eight interpreters that import the package. The holder holds the
        # lock until the test kills it, and a writer that exits before it reaches the lock fails
        # the test at once.
        deadline = time.monotonic() + 600
        while len(list(ready.iterdir())) < 8:
            exited = [proc.args[-1] for proc in procs if proc.poll() is not None]
            self.assertEqual(exited, [], "a writer exited before it reached the lock")
            self.assertLess(time.monotonic(), deadline, "not every writer reached the lock")
            time.sleep(0.05)
        for proc in procs:
            self.assertIsNone(proc.poll(), "a writer finished before the holder was killed")
        holder.kill()
        holder.wait()
        for proc in procs:
            self.assertEqual(proc.wait(), 0)
        self.assertEqual(sorted(_mod.load(self.path)), [f"s{i}" for i in range(8)])


class TestTheValidationResume(unittest.TestCase):
    """What an interrupted `snapshot validation` leaves behind. Opening the result file for
    writing would truncate the rows that the same run has just read from it. Writing the whole
    session into a temporary file renamed at the end would discard every result that an
    interrupted run has already obtained. The run therefore moves the resumed rows into place
    before the first request and appends each result as it arrives."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = self.tmp / "repos.csv"
        self.output = self.tmp / "snapshot-validation.csv"
        names = [f"o{i}/r{i}" for i in range(4)]
        with self.input.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["repo_name", "head_sha", "claude"])
            w.writeheader()
            for name in names:
                w.writerow({"repo_name": name, "head_sha": "", "claude": "TRUE"})
        # Two results already on disk, as a previous run left them.
        with self.output.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_mod.FIELDNAMES)
            w.writeheader()
            for name in names[:2]:
                w.writerow({**{k: "" for k in _mod.FIELDNAMES}, "repo_name": name,
                            "outcome": "OK"})

    def _written(self) -> list:
        """The repositories that the result file names."""
        with self.output.open(newline="", encoding="utf-8") as f:
            return [r["repo_name"] for r in csv.DictReader(f)]

    def _run(self, answers_before_the_stop: int):
        """Run `main_validate` with a stub that raises after N results.

        The stub raises only once the run has written every result that the stub returned
        before. `as_completed` yields the futures that are already finished in no fixed order,
        and a stop that finished together with an earlier result could otherwise come first and
        end the run before that result is written. The bound of the wait only catches a run that
        never writes the result.
        """
        answered = []

        def stub(client, row, shas):
            if len(answered) >= answers_before_the_stop:
                deadline = time.monotonic() + 600
                while not set(answered) <= set(self._written()):
                    if time.monotonic() >= deadline:
                        raise AssertionError(f"the run never wrote the results {answered}")
                    time.sleep(0.005)
                raise KeyboardInterrupt
            answered.append(row["repo_name"])
            return {**{k: "" for k in _mod.FIELDNAMES}, "repo_name": row["repo_name"],
                    "outcome": "OK"}

        argv = ["-i", str(self.input), "-o", str(self.output),
                "--collected-dir", str(self.tmp / "absent"),
                "--output-dir", str(self.tmp / "absent"),
                "--credentials", str(self.tmp / "absent"), "-w", "1", "--no-snapshot"]
        with mock.patch.object(_mod, "check_repository", stub), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            try:
                _mod.main_validate(argv)
            except (KeyboardInterrupt, BaseException) as stop:
                if not isinstance(stop, KeyboardInterrupt):
                    raise
        return self._written()

    def test_an_interrupted_run_keeps_the_results_that_it_obtained(self):
        names = self._run(answers_before_the_stop=1)
        self.assertIn("o0/r0", names)
        self.assertIn("o1/r1", names)
        self.assertEqual(len([n for n in names if n.startswith("o2") or n.startswith("o3")]), 1,
                         "the result that this run obtained was discarded")

    def test_an_interrupted_run_leaves_no_sibling_behind(self):
        self._run(answers_before_the_stop=1)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.partial")), [],
                         "a stray .partial would stop the publication stage")

    def test_the_result_file_ends_each_record_with_lf(self):
        """The resumed rows and the results that the run appends end each record with LF, as
        every CSV of the pipeline does, rather than with the CRLF that the csv module writes by
        default. The fixture's result file ends its records with CRLF."""
        names = self._run(answers_before_the_stop=2)
        self.assertEqual(sorted(names), ["o0/r0", "o1/r1", "o2/r2", "o3/r3"])
        self.assertNotIn(b"\r\n", self.output.read_bytes())


class _TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # The layout that the step reads, defined once. Each list is at the root of the tree
        # that it lists, and the collected list is therefore inside the directory that it
        # counts. A fixture that put the list beside the tree would test a layout that no step
        # writes.
        self.collected_dir = self.tmp / "data" / "collected"
        self.collected_csv = self.collected_dir / "repos.csv"
        _write_csv(self.collected_dir / "clone_failed.csv", [], ["repo_name"])
        self.validated_csv = self.tmp / "data" / "validated" / "repos.csv"
        # Absent unless a test writes it, as after a collection without fetched files.
        self.fetched_csv = self.tmp / "fetched_files.csv"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCollectionCounts(_TempCase):
    """The counts from the sampled list to the repositories that the collected file cleanup
    kept."""

    HEADER = [*heuristics.META_COLUMNS, "first_commit_sha", "claude"]

    def _collected(self, rows):
        _write_csv(self.collected_csv, rows, self.HEADER)
        return self.collected_csv

    def _row(self, name, sha="abc", claude="TRUE"):
        return {"repo_name": name, "scanned_at": "2026-03-29", "branch": "main",
                "head_sha": sha, "head_commit_date": "2026-03-29T02:27:29Z",
                "first_commit_sha": "", "claude": claude}

    def test_every_row_counts_as_sampled(self):
        collected = self._collected([self._row("o/a"), self._row("o/b", claude="FALSE")])
        counts = _mod.collection_counts(collected, self.tmp / "absent", self.tmp / "absent.csv")
        self.assertEqual(counts["sampled"], 2)

    def test_only_a_true_flag_counts_as_flagged(self):
        collected = self._collected([self._row("o/a"), self._row("o/b", claude="FALSE")])
        counts = _mod.collection_counts(collected, self.tmp / "absent", self.tmp / "absent.csv")
        self.assertEqual(counts["flagged"], 1)

    def test_a_metadata_column_is_not_read_as_a_tool_flag(self):
        """The flag columns are the columns that the heuristics do not name as metadata. A
        metadata column with a name that the heuristics lack is therefore read as a flag, and
        only the value that it happens to contain keeps it from counting as `TRUE`."""
        self.assertEqual(
            [c for c in self.HEADER if c not in (*heuristics.META_COLUMNS,
                                                 "first_commit_sha")],
            ["claude"])
        collected = self._collected([self._row("o/a", claude="FALSE")])
        counts = _mod.collection_counts(collected, self.tmp / "absent",
                                        self.tmp / "absent.csv")
        self.assertEqual(counts["flagged"], 0)

    def test_missing_metadata_and_recorded_clone_failures_are_separate(self):
        collected = self._collected([self._row("o/metadata", sha=""),
                                     self._row("o/failed", sha="", claude="FALSE"),
                                     self._row("o/unconfigured", claude="FALSE")])
        (self.collected_dir / "o§metadata").mkdir()
        _write_csv(self.collected_dir / "clone_failed.csv", [{"repo_name": "o/failed"}])
        counts = _mod.collection_counts(collected, self.collected_dir, self.tmp / "absent.csv")
        self.assertEqual(counts["clone_failed"], 1)
        self.assertEqual(counts["missing_head_sha"], 2)
        self.assertEqual(counts["rows_without_files"], 1)

    def test_missing_metadata_without_files_is_not_an_inferred_clone_failure(self):
        collected = self._collected([self._row("o/unknown", sha="", claude="FALSE")])
        counts = _mod.collection_counts(collected, self.collected_dir, self.tmp / "absent.csv")
        self.assertEqual(counts["clone_failed"], 0)
        self.assertEqual(counts["missing_head_sha"], 1)
        self.assertEqual(counts["rows_without_files"], 1)

    def test_a_flagged_repo_with_no_directory_is_counted(self):
        collected = self._collected([self._row("o/a"), self._row("o/b")])
        archived = self.collected_dir
        (archived / "o§a").mkdir(parents=True)
        counts = _mod.collection_counts(collected, archived, self.tmp / "absent.csv")
        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["flagged_without_a_directory"], 1)

    def test_a_stray_directory_is_not_an_archived_repository(self):
        collected = self._collected([self._row("o/a")])
        archived = self.collected_dir
        (archived / "o§a").mkdir(parents=True)
        (archived / "__MACOSX").mkdir()
        counts = _mod.collection_counts(collected, archived, self.tmp / "absent.csv")
        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["flagged_without_a_directory"], 0)

    def test_rows_without_files_adds_the_two_ways_of_having_none(self):
        collected = self._collected([self._row("o/a", sha=""), self._row("o/b")])
        # _collected() already created the tree to write the list into. The tree contains the
        # list and no repository, which is what this test needs.
        archived = self.collected_dir
        counts = _mod.collection_counts(collected, archived, self.tmp / "absent.csv")
        # Both rows are flagged and neither has a directory, and o/a also never cloned.
        # o/a counts once, where a sum of the two sets would count three.
        self.assertEqual(counts["rows_without_files"], 2)

    def test_validated_is_none_when_the_working_list_is_absent(self):
        collected = self._collected([self._row("o/a")])
        counts = _mod.collection_counts(collected, self.tmp / "absent", self.tmp / "absent.csv")
        self.assertIsNone(counts["validated"])

    def test_validated_counts_the_working_list(self):
        collected = self._collected([self._row("o/a")])
        working = self.validated_csv
        _write_csv(working, [{"repo_name": "o/a"}, {"repo_name": "o/b"}])
        counts = _mod.collection_counts(collected, self.tmp / "absent", working)
        self.assertEqual(counts["validated"], 2)

    def test_the_tree_of_a_repository_that_the_list_does_not_name_is_not_archived(self):
        """The tree of a repository that left the input list stays where the cloning step could
        not remove it (`reuse.remove_left`). The tree belongs to no collection that the list
        describes."""
        collected = self._collected([self._row("o/a")])
        (self.collected_dir / "o§a").mkdir(parents=True)
        (self.collected_dir / "o§left").mkdir()
        counts = _mod.collection_counts(collected, self.collected_dir, self.tmp / "absent.csv")
        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["flagged_without_a_directory"], 0)

    def test_the_lists_at_the_root_of_the_tree_are_not_archived_repositories(self):
        # The two lists are inside the tree that they list. Reading either list as a repository
        # would increase `archived` and make every derived section contradict the tree that it
        # was built from.
        collected = self._collected([self._row("o/a")])
        (self.collected_dir / "o§a").mkdir(parents=True)
        (self.collected_dir / "clone_failed.csv").write_text("repo_name\n", encoding="utf-8")
        counts = _mod.collection_counts(collected, self.collected_dir, self.tmp / "absent.csv")
        self.assertEqual(counts["archived"], 1)
        self.assertEqual(counts["flagged_without_a_directory"], 0)


class TestArtifactWindow(_TempCase):
    def setUp(self):
        super().setUp()
        _write_csv(self.collected_csv, [{"repo_name": "o/a"}])

    def _window(self, collected_dir: Path = None) -> dict | None:
        return _mod.artifact_window(collected_dir or self.collected_dir, self.collected_csv,
                                    self.fetched_csv)

    def test_a_fetched_file_does_not_move_the_window(self):
        """A fetched file was copied into the tree of a working copy after the collection, and
        its write time is the time of the fetch. The same path in a repository that the list of
        fetched files does not name was written by the scan and still counts."""
        _write_csv(self.collected_csv, [{"repo_name": "o/a"}, {"repo_name": "o/b"}])
        for repo, when in (("o§a", 1_000_000_000), ("o§b", 1_050_000_000)):
            (self.collected_dir / repo / ".claude").mkdir(parents=True)
            (self.collected_dir / repo / "CLAUDE.md").write_text("x", encoding="utf-8")
            os.utime(self.collected_dir / repo / "CLAUDE.md", (when, when))
        for repo, when in (("o§a", 1_100_000_000), ("o§b", 1_090_000_000)):
            fetched = self.collected_dir / repo / ".claude" / "settings.json"
            fetched.write_text("{}", encoding="utf-8")
            os.utime(fetched, (when, when))
        _write_csv(self.fetched_csv, [{"repo_name": "o/a", "path": ".claude/settings.json",
                                       "kind": "file", "commit_sha": "a" * 40,
                                       "fetched_on": "2004-11-09", "change": "c81c44d2"}])
        self.assertEqual(self._window(),
                         {"first": "2001-09-09T01:46:40Z", "last": "2004-07-16T17:46:40Z"})

    def test_an_absent_directory_yields_nothing(self):
        self.assertIsNone(self._window(self.tmp / "absent"))

    def test_an_empty_directory_yields_nothing(self):
        (self.tmp / "empty").mkdir()
        self.assertIsNone(self._window(self.tmp / "empty"))

    def test_the_window_spans_the_oldest_and_newest_file(self):
        root = self.collected_dir / "o§a"
        root.mkdir(parents=True)
        (root / "old.md").write_text("x", encoding="utf-8")
        (root / "new.md").write_text("x", encoding="utf-8")
        os.utime(root / "old.md", (1_000_000_000, 1_000_000_000))
        os.utime(root / "new.md", (1_100_000_000, 1_100_000_000))
        window = self._window()
        self.assertEqual(window["first"], "2001-09-09T01:46:40Z")
        self.assertEqual(window["last"], "2004-11-09T11:33:20Z")

    def test_a_list_at_the_root_of_the_tree_does_not_move_the_window(self):
        # The window, recorded in the snapshot, is the span of the write times of the collected
        # configuration artifacts. `artifact_window` reads only the repository directories, and
        # the list that the step writes at the root of the tree after the last clone therefore
        # cannot move `last` to the time at which the run finished.
        root = self.collected_dir / "o§a"
        root.mkdir(parents=True)
        (root / "old.md").write_text("x", encoding="utf-8")
        os.utime(root / "old.md", (1_000_000_000, 1_000_000_000))
        os.utime(self.collected_csv, (1_000_000_000, 1_000_000_000))
        before = self._window()
        os.utime(self.collected_csv, (1_600_000_000, 1_600_000_000))
        self.assertEqual(self._window(), before)

    def test_a_file_that_synology_dsm_wrote_does_not_move_the_window(self):
        # DSM writes `@eaDir` directories into the trees that File Station visits. Their write
        # time is the time of the visit, not of the scan of the repository.
        root = self.collected_dir / "o§a"
        root.mkdir(parents=True)
        (root / "old.md").write_text("x", encoding="utf-8")
        os.utime(root / "old.md", (1_000_000_000, 1_000_000_000))
        before = self._window()
        (root / "@eaDir").mkdir()
        (root / "@eaDir" / "old.md@SynoEAStream").write_text("x", encoding="utf-8")
        self.assertEqual(self._window(), before)

    def test_the_tree_of_a_repository_that_the_list_does_not_name_does_not_move_it(self):
        """The tree of a repository that left the input list, which the cloning step could not
        remove, contains the files of an earlier collection."""
        root = self.collected_dir / "o§a"
        root.mkdir(parents=True)
        (root / "new.md").write_text("x", encoding="utf-8")
        os.utime(root / "new.md", (1_100_000_000, 1_100_000_000))
        before = self._window()
        left = self.collected_dir / "o§left"
        left.mkdir()
        (left / "old.md").write_text("x", encoding="utf-8")
        os.utime(left / "old.md", (1_000_000_000, 1_000_000_000))
        self.assertEqual(self._window(), before)

    def test_a_symlink_is_skipped(self):
        root = self.collected_dir / "o§a"
        root.mkdir(parents=True)
        (root / "real.md").write_text("x", encoding="utf-8")
        os.utime(root / "real.md", (1_000_000_000, 1_000_000_000))
        os.symlink("real.md", root / "link.md")
        os.utime(root / "link.md", (1_600_000_000, 1_600_000_000), follow_symlinks=False)
        window = self._window()
        self.assertEqual(window["first"], window["last"])






class TestMetadataCounts(_TempCase):
    """The metadata section and the output CSVs that it is counted from, one per mechanism.

    A check that skipped a mechanism with an absent CSV would record
    `artifact_rows: 0` without failing when every CSV is absent.
    """

    DATES = {"created_at": "2026-02-01T00:00:00Z", "head_commit_date": "2026-03-01T00:00:00Z"}

    def _tables(self, rows_by_stem=None):
        """Write every output CSV, so that the only absent CSV is the CSV that a test removes.
        A row has both dates unless it names them."""
        rows_by_stem = rows_by_stem or {}
        for stem in _mod.MECHANISMS:
            column = _artifact_column(stem)
            _write_csv(self.tmp / f"{stem}.csv",
                       [self.DATES | _named(row, column) for row in rows_by_stem.get(stem, [])],
                       ["repo_name", column, "last_commit_sha", *self.DATES])

    def test_a_mechanism_csv_without_the_heuristics_column_is_uncheckable(self):
        # A renamed configuration artifact column would otherwise be compared on the
        # wrong key, or raise a bare ValueError out of the check.
        self._tables()
        column = _artifact_column("skills")
        _write_csv(self.tmp / "skills.csv",
                   [{"repo_name": "o/a", column + "_renamed": "x",
                     "last_commit_sha": "s"}],
                   ["repo_name", column + "_renamed", "last_commit_sha"])
        with self.assertRaises(_mod.UncheckableTable) as caught:
            _mod.metadata_counts(self.tmp)
        self.assertIn("skills.csv", str(caught.exception))
        self.assertIn(column, str(caught.exception))

    def test_a_missing_mechanism_csv_stops_the_count_rather_than_zeroing_it(self):
        """The count names the missing CSV. A skip would record one absent CSV as a count over
        the other CSVs, and the absence of every CSV as a section of zeros."""
        self._tables({"skills": [{"repo_name": "o/a", "filepath": "x",
                                  "last_commit_sha": "s"}]})
        (self.tmp / "hooks.csv").unlink()
        with self.assertRaises(_mod.UncheckableTable) as caught:
            _mod.metadata_counts(self.tmp)
        self.assertIn("hooks.csv", str(caught.exception))

    def test_every_mechanism_name_resolves_to_an_artifact_column(self):
        # `artifact_column_for_file` raises for a name of `MECHANISMS` that the heuristics
        # lack. Because the count looks up each name, the test asserts the lookup rather than
        # the list itself.
        for stem in _mod.MECHANISMS:
            with self.subTest(mechanism=stem):
                self.assertIn(_artifact_column(stem),
                              heuristics.ARTIFACT_COLUMNS)

    def test_the_section_contains_the_two_keys_and_no_third(self):
        """A third key would be written into `snapshot.json`, where nothing recomputes the key
        or logs that it is no longer derived."""
        self._tables()
        self.assertEqual(set(_mod.metadata_counts(self.tmp)),
                         {"artifact_rows", "rows_without_commit_metadata"})

    def test_artifact_rows_are_counted_across_mechanisms(self):
        self._tables({
            "skills": [{"repo_name": "o/a", "filepath": "x", "last_commit_sha": "s"}],
            "hooks": [{"repo_name": "o/b", "filepath": "y", "last_commit_sha": "s"},
                      {"repo_name": "o/b", "filepath": "z", "last_commit_sha": "s"}],
        })
        self.assertEqual(_mod.metadata_counts(self.tmp)["artifact_rows"], 3)

    def test_a_row_with_no_sha_counts_as_a_row_without_commit_metadata(self):
        self._tables({"skills": [{"repo_name": "o/a", "filepath": "x",
                                  "last_commit_sha": ""}]})
        counts = _mod.metadata_counts(self.tmp)["rows_without_commit_metadata"]
        self.assertEqual(counts, {"total": 1, "repositories": 1})

    def test_a_repository_blank_in_two_mechanism_csvs_is_one_repository(self):
        """A finding spans several output CSVs. The rows add up across the CSVs, and the
        repositories are their union."""
        self._tables({
            "skills": [{"repo_name": "o/a", "filepath": "x", "last_commit_sha": ""}],
            "hooks": [{"repo_name": "o/a", "filepath": "y", "last_commit_sha": ""}],
        })
        counts = _mod.metadata_counts(self.tmp)["rows_without_commit_metadata"]
        self.assertEqual(counts, {"total": 2, "repositories": 1})

    def test_a_date_that_git_printed_in_a_form_that_does_not_parse_is_missing_metadata(self):
        """The steps leave such a date blank on a row with a commit history, and the release
        states the rows with missing commit metadata among its evidence limits. A row of a
        repository with such a date on its head commit counts among these rows too."""
        self._tables({"skills": [{"repo_name": "o/a", "filepath": "x", "last_commit_sha": "s",
                                  "created_at": ""},
                                 {"repo_name": "o/b", "filepath": "y", "last_commit_sha": "s",
                                  "head_commit_date": ""},
                                 {"repo_name": "o/c", "filepath": "z", "last_commit_sha": "s"}]})
        counts = _mod.metadata_counts(self.tmp)["rows_without_commit_metadata"]
        self.assertEqual(counts, {"total": 2, "repositories": 2})

    def test_a_published_csv_without_a_date_column_is_uncheckable(self):
        """Read as blank, an absent column would count every row of the CSV."""
        self._tables()
        column = _artifact_column("skills")
        _write_csv(self.tmp / "skills.csv",
                   [{"repo_name": "o/a", column: "x", "last_commit_sha": "s",
                     "head_commit_date": "2026-03-01T00:00:00Z"}],
                   ["repo_name", column, "last_commit_sha", "head_commit_date"])
        with self.assertRaises(_mod.UncheckableTable) as caught:
            _mod.metadata_counts(self.tmp)
        self.assertIn("skills.csv has no column created_at", str(caught.exception))

    def test_blank_rows_of_the_withheld_other_csv_are_no_missing_commit_metadata(self):
        """The README of the release states these rows among its evidence limits, and the
        release withholds `other.csv`. Counted from `other.csv`, the unclassified files of a
        repository with a failed history fetch would make the README name rows that no published
        CSV has."""
        self._tables({"other": [{"repo_name": "o/r", "filepath": f"x{i}.txt",
                                 "last_commit_sha": ""} for i in range(3)]})
        counts = _mod.metadata_counts(self.tmp)
        self.assertEqual(counts["rows_without_commit_metadata"],
                         {"total": 0, "repositories": 0})
        self.assertEqual(counts["artifact_rows"], 3)


class TestHeaderOnlyCsvs(_TempCase):
    """A CSV with a header and no rows is normal in a subsample run. Reading the first row to
    find the columns would fail on such a CSV with a traceback."""

    def test_a_header_only_collected_list_does_not_crash(self):
        path = self.collected_csv
        _write_csv(path, [], [*heuristics.META_COLUMNS, "claude"])
        counts = _mod.collection_counts(path, self.tmp / "absent", self.tmp / "absent.csv")
        self.assertEqual(counts["sampled"], 0)
        self.assertEqual(counts["flagged"], 0)

    def test_header_only_mechanism_csvs_do_not_crash(self):
        for stem in _mod.MECHANISMS:
            column = _artifact_column(stem)
            _write_csv(self.tmp / f"{stem}.csv", [],
                       ["repo_name", column, "last_commit_sha", "created_at",
                        "head_commit_date"])
        counts = _mod.metadata_counts(self.tmp)
        self.assertEqual(counts["artifact_rows"], 0)
        self.assertEqual(counts["rows_without_commit_metadata"],
                         {"total": 0, "repositories": 0})


class TestProvenanceKey(_TempCase):
    """How a section's `derived_from` names the file that it was built from.

    Three files of this step are named `repos.csv`, in `data/collected/`, `data/validated/`
    and `output/`, and the key therefore includes the directory. A bare `path.name` would
    record `repos.csv` for each of them, and the section would record how many rows it read
    without naming the list that they came from.
    """

    def test_a_collected_list_is_keyed_by_its_directory(self):
        self.assertEqual(
            _mod.provenance_key(_mod.STAGE_DIR / "data" / "collected" / "repos.csv"),
            "data/collected/repos.csv")

    def test_the_collected_tree_is_keyed_by_its_directory(self):
        self.assertEqual(_mod.provenance_key(_mod.STAGE_DIR / "data" / "collected"),
                         "data/collected")

    def test_the_three_lists_named_repos_csv_get_three_keys(self):
        keys = {_mod.provenance_key(_mod.STAGE_DIR / rel)
                for rel in ("data/collected/repos.csv", "data/validated/repos.csv",
                            "output/repos.csv")}
        self.assertEqual(len(keys), 3)

    def test_a_path_outside_the_stage_falls_back_to_its_name(self):
        # The fixtures are in temporary directories with an absolute path that identifies
        # nothing and changes with every run. A key built from such a path would never match
        # the next run's key.
        self.assertEqual(_mod.provenance_key(self.collected_csv), "repos.csv")




class TestCheckExitCode(_TempCase):
    """`--check` gives its verdict in its exit code, which a caller reads. The verdict that a
    section would change therefore has to be the code that the command returns.
    test_check_modes.py covers absent inputs but asserts only that the code is not 0, and this
    class asserts all three values."""

    def _main(self, *extra):
        argv, snapshot = sys.argv, _mod.SNAPSHOT_PATH
        sys.argv = ["prog",
                    "--collected-csv", str(self.collected_csv),
                    "--collected-dir", str(self.collected_dir),
                    "--validated-csv", str(self.tmp / "absent.csv"),
                    "--output-dir", str(self.tmp / "absent"), *extra]
        # `--output-dir` determines where the snapshot is written, and this override is a second
        # safeguard. If `--output-dir` stops determining it, the next test fails, and this test
        # still leaves the repository's own provenance record unchanged.
        _mod.SNAPSHOT_PATH = self.tmp / "snapshot.json"
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                return _mod.main_write()
        finally:
            sys.argv, _mod.SNAPSHOT_PATH = argv, snapshot

    def _collected(self):
        """A sampled list and an archived tree that no recorded section covers."""
        _write_csv(self.collected_csv,
                   [{"repo_name": "o/a", "scanned_at": "2026-03-29", "branch": "main",
                     "head_sha": "abc", "head_commit_date": "2026-03-29T02:27:29Z",
                     "first_commit_sha": "", "claude": "TRUE"}],
                   [*heuristics.META_COLUMNS, "first_commit_sha", "claude"])
        root = self.collected_dir / "o§a"
        root.mkdir(parents=True)
        (root / "CLAUDE.md").write_text("x", encoding="utf-8")

    def test_a_section_that_would_move_exits_two(self):
        self._collected()
        self.assertEqual(self._main("--check"), 2)

    def test_the_snapshot_is_written_beside_the_output_it_describes(self):
        """A `--output-dir` that redirected only the inputs would let a run over a scratch
        directory rewrite the repository's tracked output/snapshot.json and record its
        provenance under the scratch directory's name."""
        self._collected()
        argv, snapshot = sys.argv, _mod.SNAPSHOT_PATH
        default = self.tmp / "must_not_be_written.json"
        _mod.SNAPSHOT_PATH = default
        elsewhere = self.tmp / "elsewhere"
        sys.argv = ["prog",
                    "--collected-csv", str(self.collected_csv),
                    "--collected-dir", str(self.collected_dir),
                    "--validated-csv", str(self.tmp / "absent.csv"),
                    "--output-dir", str(elsewhere)]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                _mod.main_write()
        finally:
            sys.argv, _mod.SNAPSHOT_PATH = argv, snapshot
        self.assertTrue((elsewhere / "snapshot.json").is_file())
        self.assertFalse(default.exists())
        self.assertEqual(_mod.load(elsewhere / "snapshot.json")["collection"]["counts"],
                         self.COUNTS)

    # What `_collected` implies: one row, flagged, cloned and archived. Without a validated
    # list, the count of validated repositories is absent rather than recorded.
    COUNTS = {"sampled": 1, "flagged": 1, "archived": 1, "clone_failed": 0, "missing_head_sha": 0,
              "flagged_without_a_directory": 0, "rows_without_files": 0}

    def test_the_run_that_records_instead_of_checking_exits_zero(self):
        """This test covers the `else 0` half of the same expression, and a verdict that is
        always 2 fails it too. It tests that half rather than a contract about the exit code of
        a partial write run. It also reads the section back, which makes a run that writes zeros
        fail as well."""
        self._collected()
        self.assertEqual(self._main(), 0)
        self.assertEqual(_mod.load(self.tmp / "absent" / "snapshot.json")["collection"]["counts"],
                         self.COUNTS)

    def test_a_run_that_could_not_check_everything_exits_one(self):
        """The code is 1 and not 2, because a partial check has found no difference.
        test_check_modes asserts only a non-zero code here and would still pass if this verdict
        changed to 2."""
        self.assertEqual(self._main("--check", "--collected-csv",
                                    str(self.tmp / "absent.csv")), 1)


class TestAWholeStep(_TempCase):
    """Every input that `snapshot write` reads, so that every section is checked and `--check`
    can exit 0. The test of present inputs in test_check_modes needs a gitignored file and is
    skipped, and these tests are therefore the only tests that reach exit code 0. The record
    that the check reads is written here by hand, from what the fixture implies, rather than by
    the writer under test."""

    FIRST = datetime.datetime(2026, 3, 29, 17, 24, 38, tzinfo=datetime.timezone.utc)
    LAST = datetime.datetime(2026, 3, 29, 18, 2, 5, tzinfo=datetime.timezone.utc)

    def setUp(self):
        super().setUp()
        self.output = self.tmp / "output"
        self.snapshot = self.output / "snapshot.json"
        header = [*heuristics.META_COLUMNS, "first_commit_sha", "claude"]

        def row(name, sha, claude):
            return {"repo_name": name, "scanned_at": "2026-03-29", "branch": "main",
                    "head_sha": sha, "head_commit_date": "2026-03-29T02:27:29Z",
                    "first_commit_sha": "", "claude": claude}
        # o/a is cloned and archived, o/b was not flagged, o/c could not be cloned and
        # o/d is flagged and has no directory.
        _write_csv(self.collected_csv, [row("o/a", "abc", "TRUE"), row("o/b", "def", "FALSE"),
                                        row("o/c", "", "TRUE"), row("o/d", "ghi", "TRUE")],
                   header)
        _write_csv(self.collected_dir / "clone_failed.csv", [{"repo_name": "o/c"}])
        tree = self.collected_dir / "o§a"
        (tree / ".claude").mkdir(parents=True)
        for relative, when in (("CLAUDE.md", self.FIRST), (".claude/settings.json", self.LAST)):
            (tree / relative).write_text("x", encoding="utf-8")
            os.utime(tree / relative, (when.timestamp(), when.timestamp()))
        _write_csv(self.validated_csv, [row("o/a", "abc", "TRUE")], header)
        _write_csv(self.output / "repos.csv",
                   [{"repo_name": "o/a", **{c: "FALSE" for c in _mod.NAMED_MECHANISMS},
                     "context_file": "TRUE", "settings": "TRUE"}],
                   ["repo_name", *_mod.NAMED_MECHANISMS])
        # Three configuration artifacts of o/a, two of them without a commit.
        rows = {"context-files": [("CLAUDE.md", "e" * 40), ("AGENTS.md", "")],
                "settings": [(".claude/settings.json", "")]}
        for stem in _mod.MECHANISMS:
            column = _artifact_column(stem)
            _write_csv(self.output / f"{stem}.csv",
                       [{"repo_name": "o/a", column: path, "last_commit_sha": sha,
                         "created_at": "2026-02-01T00:00:00Z" if sha else "",
                         "head_commit_date": "2026-03-29T02:27:29Z"}
                        for path, sha in rows.get(stem, ())],
                       ["repo_name", column, "last_commit_sha", "created_at",
                        "head_commit_date"])
        # Two planning documents of o/a, one of them without a commit.
        _write_csv(self.output / "planning-documents.csv",
                   [{"repo_name": "o/a", "planning_document": "specs/001-x/spec.md",
                     "framework": "spec-kit", "last_commit_sha": "f" * 40,
                     "created_at": "2026-02-01T00:00:00Z",
                     "head_commit_date": "2026-03-29T02:27:29Z"},
                    {"repo_name": "o/a", "planning_document": ".cursor/plans/x.plan.md",
                     "framework": "cursor", "last_commit_sha": "", "created_at": "",
                     "head_commit_date": "2026-03-29T02:27:29Z"}],
                   ["repo_name", "planning_document", "framework", "last_commit_sha",
                    "created_at", "head_commit_date"])
        # Three imports of o/a's CLAUDE.md: two of Claude Code, one of them found, and one of
        # Copilot CLI.
        _write_csv(self.output / "imports.csv",
                   [{"repo_name": "o/a", "tool": "claude", "target_status": "found"},
                    {"repo_name": "o/a", "tool": "claude", "target_status": "missing"},
                    {"repo_name": "o/a", "tool": "copilot", "target_status": "found"}],
                   ["repo_name", "tool", "target_status"])

    def test_a_collected_folder_without_a_repository_keeps_the_counts_of_the_trees(self):
        """A checkout contains the lists of data/collected/ and no repository's tree. Counted
        against such a checkout, no repository would be archived and every flagged repository
        would have no directory. The counts from the trees keep their recorded values and are
        logged as not checked, and the counts from the lists are computed again."""
        self._record()
        shutil.rmtree(self.collected_dir / "o§a")
        code, output = self._write()
        # Exit 1, because nothing that this run could read changed and it could not read the
        # trees.
        self.assertEqual(code, 1, output)
        counts = _mod.load(self.snapshot)["collection"]["counts"]
        for key in ("archived", "flagged_without_a_directory", "rows_without_files"):
            with self.subTest(key=key):
                self.assertEqual(counts[key], self.expected()["collection"]["counts"][key])
        self.assertEqual(counts["sampled"], 4)
        self.assertIn("with no repository's tree in", output)

    def test_the_planning_documents_are_counted_apart_from_the_configuration_artifacts(self):
        """The planning documents have a section of their own, with the rows without commit
        metadata that the release states. `metadata.artifact_rows` counts the rows of the
        mechanism CSVs alone, because a planning document is no configuration artifact."""
        code, out = self._write()
        self.assertEqual(code, 0, out)
        record = _mod.load(self.snapshot)
        self.assertEqual(record["planning_documents"], self.expected()["planning_documents"])
        self.assertEqual(record["metadata"]["artifact_rows"], 3)

    def test_the_imports_are_counted_in_a_section_of_their_own(self):
        code, out = self._write()
        self.assertEqual(code, 0, out)
        self.assertEqual(_mod.load(self.snapshot)["imports"], self.expected()["imports"])

    def test_missing_imports_are_not_checked(self):
        """The import detection step writes imports.csv. A run without the file cannot confirm
        the section and exits 1 rather than 0."""
        self._record()
        (self.output / "imports.csv").unlink()
        code, output = self._write("--check")
        self.assertEqual(code, 1, output)
        self.assertIn("imports, because imports.csv is missing", output)

    def test_missing_planning_documents_are_not_checked(self):
        """The detection step writes the CSV of the planning documents with the mechanism CSVs.
        A run without that CSV cannot confirm the section and exits 1 rather than 0."""
        self._record()
        (self.output / "planning-documents.csv").unlink()
        code, output = self._write("--check")
        self.assertEqual(code, 1, output)
        self.assertIn("planning_documents, because planning-documents.csv is missing", output)

    def test_a_fetched_file_leaves_the_window_and_names_its_list(self):
        """A file fetched into the collected tree after the collection carries the time of the
        fetch. The window stays the span of the scanned files, and the collection section names
        the list of the fetched files among its inputs."""
        fetched = self.collected_dir / "o§a" / ".claude" / "commands.md"
        fetched.write_text("x", encoding="utf-8")
        later = self.LAST + datetime.timedelta(days=5)
        os.utime(fetched, (later.timestamp(), later.timestamp()))
        _write_csv(self.fetched_csv, [{"repo_name": "o/a", "path": ".claude/commands.md",
                                       "kind": "file", "commit_sha": "abc",
                                       "fetched_on": "2026-04-03", "change": "c81c44d2"}])
        code, output = self._write()
        self.assertEqual(code, 0, output)
        collection = _mod.load(self.snapshot)["collection"]
        self.assertEqual(collection["artifact_mtime_window"],
                         self.expected()["collection"]["artifact_mtime_window"])
        self.assertEqual(collection["derived_from"]["fetched_files.csv"], {"rows": 1})

    def test_the_summary_of_a_subsample_run_counts_its_own_files(self):
        """A subsample run collects its own tree in a checkout that tracks the list of the files
        fetched into the development dataset, and it fetches nothing. Its collection summary
        counts each file that the run wrote, also one at a path of the list, and names no list
        among its inputs."""
        mirror_run = importlib.import_module("agentcontext.tools.mirror_run")
        [argv] = [argv for name, argv in mirror_run.steps_after_collect()
                  if name == "collection_summary"]
        written = self.collected_dir / "o§a" / ".claude" / "commands.md"
        written.write_text("x", encoding="utf-8")
        later = self.LAST + datetime.timedelta(days=5)
        os.utime(written, (later.timestamp(), later.timestamp()))
        _write_csv(self.fetched_csv, [{"repo_name": "o/a", "path": ".claude/commands.md",
                                       "kind": "file", "commit_sha": "abc",
                                       "fetched_on": "2026-04-03", "change": "c81c44d2"}])
        code, output = self._write(*argv[argv.index("write") + 1:])
        self.assertEqual(code, 0, output)
        collection = _mod.load(self.snapshot)["collection"]
        self.assertEqual(collection["artifact_mtime_window"]["last"],
                         later.strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertNotIn("fetched_files.csv", collection["derived_from"])

    def test_missing_failure_list_cannot_pass_collection_verification(self):
        self._record()
        (self.collected_dir / "clone_failed.csv").unlink()
        before = self.snapshot.read_bytes()
        code, output = self._write("--check")
        self.assertEqual(code, 1)
        self.assertIn("clone_failed.csv", output)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_a_section_that_no_command_writes_is_dropped(self):
        """`snapshot write` drops a section that no command writes, such as a `sample` beside
        `configured`, rather than keeping it as if it were current."""
        record = self.expected()
        record["sample"] = record["configured"]
        self.snapshot.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        code, output = self._write()
        self.assertEqual(code, 0, output)
        written = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertNotIn("sample", written)
        self.assertEqual(written["configured"]["repositories"], 1)

    def test_the_section_of_another_command_stays(self):
        """`snapshot validation` writes the `validation` section, and `snapshot write` keeps it
        as it is, because each command replaces only its own sections."""
        record = self.expected()
        validation = {"checked_on": "2026-10-27", "repositories": 3,
                      "derived_from": {"output/validation.csv": {"rows": 3, "in_scope": 3}}}
        record["validation"] = validation
        self.snapshot.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        code, output = self._write()
        self.assertEqual(code, 0, output)
        written = json.loads(self.snapshot.read_text(encoding="utf-8"))
        self.assertEqual(written["validation"], validation)

    def expected(self) -> dict:
        """The record that the fixture implies, stated by hand."""
        return {
            "collection": {
                "scanned_at_window": {"first": "2026-03-29", "last": "2026-03-29"},
                "counts": {"sampled": 4, "flagged": 3, "archived": 1, "clone_failed": 1, "missing_head_sha": 1,
                           "flagged_without_a_directory": 2, "rows_without_files": 2,
                           "validated": 1},
                "artifact_mtime_window": {"first": "2026-03-29T17:24:38Z",
                                          "last": "2026-03-29T18:02:05Z"},
                "derived_from": {"collected/repos.csv": {"rows": 4},
                                 "clone_failed.csv": {"rows": 1},
                                 "collected/": {"repositories": 1},
                                 "validated/repos.csv": {"rows": 1}},
            },
            "configured": {
                "repositories": 1,
                "derived_from": {"output/repos.csv": {"repositories": 1}},
            },
            "metadata": {
                "artifact_rows": 3,
                "rows_without_commit_metadata": {"total": 2, "repositories": 1},
                "derived_from": {"output/<mechanism>.csv": {
                    "rows": 3, "mechanisms": len(_mod.MECHANISMS)}},
            },
            "planning_documents": {
                "rows": 2, "repositories": 1, "frameworks": {"cursor": 1, "spec-kit": 1},
                "rows_without_commit_metadata": {"total": 1, "repositories": 1},
                "derived_from": {"output/planning-documents.csv": {"rows": 2}},
            },
            "imports": {
                "rows": 3, "repositories": 1, "tools": {"claude": 2, "copilot": 1},
                "statuses": {"found": 2, "missing": 1},
                "derived_from": {"output/imports.csv": {"rows": 3}},
            },
        }

    def _write(self, *extra) -> tuple:
        """Run `snapshot write` over the current fixture and return (exit code, log)."""
        argv = ["--collected-csv", str(self.collected_csv),
                "--collected-dir", str(self.collected_dir),
                "--validated-csv", str(self.validated_csv),
                "--output-dir", str(self.output),
                "--fetched-csv", str(self.fetched_csv), *extra]
        out = io.StringIO()
        with mock.patch.object(_mod, "SNAPSHOT_PATH", self.tmp / "must_not_be_written.json"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(out):
            code = _mod.main_write(argv)
        return code, out.getvalue()

    def _record(self) -> bytes:
        """Write the expected record as `update` writes it, and return its bytes."""
        text = json.dumps(self.expected(), indent=2, sort_keys=True) + "\n"
        self.snapshot.write_text(text, encoding="utf-8")
        return text.encode("utf-8")

    def test_writing_records_what_the_data_contains(self):
        code, out = self._write()
        self.assertEqual(code, 0, out)
        self.assertEqual(_mod.load(self.snapshot), self.expected())

    def test_a_tree_that_the_cloning_step_could_not_remove_changes_no_record(self):
        """The tree of a repository that left the input list stays where the cloning step could
        not remove it, with the files of an earlier collection. Counted, the tree would raise
        `archived` and move the start of the window that the validation's dates are compared
        with."""
        before = self._record()
        left = self.collected_dir / "o§left"
        left.mkdir()
        (left / "CLAUDE.md").write_text("x", encoding="utf-8")
        earlier = self.FIRST - datetime.timedelta(days=60)
        os.utime(left / "CLAUDE.md", (earlier.timestamp(), earlier.timestamp()))
        code, out = self._write("--check")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_a_check_over_a_record_that_matches_exits_zero_and_writes_nothing(self):
        before = self._record()
        code, out = self._write("--check")
        self.assertEqual(code, 0, out)
        self.assertIn("Every derivable section matches its inputs.", out)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_a_check_after_the_data_moved_exits_two_and_writes_nothing(self):
        before = self._record()
        with open(self.collected_csv, "a", newline="", encoding="utf-8") as f:
            f.write("o/e,2026-03-29,main,jkl,2026-03-29T02:27:29Z,,FALSE\n")
        code, out = self._write("--check")
        self.assertEqual(code, 2, out)
        self.assertIn("Would change collection.counts, collection.derived_from", out)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_incomplete_commit_reclassification_is_recorded_in_dataset_provenance(self):
        self._record()
        report = self.output / "commit-reclassification.json"
        report.write_text(json.dumps({"classified_by": "current", "complete": False,
            "collection_sha256": hashlib.sha256(self.collected_csv.read_bytes()).hexdigest(),
            "counts": report_counts(no_log=1), "repositories": {"o/a": {"outcome": "no_log"}}}))
        self.assertEqual(self._write()[0], 0)
        section = _mod.load(self.snapshot)["commit_reclassification"]
        self.assertFalse(section["complete"])
        self.assertEqual(section["counts"], report_counts(no_log=1))
        self.assertEqual(self._write("--check")[0], 0)
        report.write_text(report.read_text().replace('"complete": false', '"complete": true'))
        self.assertEqual(self._write("--check")[0], 1)
        self.assertEqual(self._write()[0], 1)
        report.write_text(report.read_text().replace('"complete": true', '"complete": false'))
        self.collected_csv.write_text(self.collected_csv.read_text() + "\n")
        self.assertEqual(self._write()[0], 1)

    def _reclassification_report(self, outcomes: dict, complete: bool, population_complete: bool,
                       assembled: bool = True) -> Path:
        """Write a reclassification report in the layout that `commit-reclassification`
        writes."""
        report = self.output / "commit-reclassification.json"
        report.write_text(json.dumps({
            "classified_by": "current",
            "collection_sha256": hashlib.sha256(self.collected_csv.read_bytes()).hexdigest(),
            "failures_sha256": "f" * 64, "complete": complete,
            "population_complete": population_complete, "assembly_complete": assembled,
            "requested_repositories": len(outcomes),
            "counts": report_counts(**Counter(outcomes.values())),
            "repositories": {repo: {"outcome": outcome} for repo, outcome in outcomes.items()}}))
        return report

    def test_every_incomplete_reclassification_outcome_is_recorded_as_incomplete(self):
        # The reclassification step counts these outcomes as incomplete and exits 2. The
        # snapshot records each of them as incomplete rather than refusing the summary, so that
        # a handover with one unfetched history can still be described.
        for outcome in ("no_log", "no_scope", "no_path_history", "history_incomplete",
                        "history_not_read", "missing_record", "missing_tree"):
            with self.subTest(outcome=outcome):
                self._record()
                self._reclassification_report({"o/a": outcome}, False, False)
                code, out = self._write()
                self.assertEqual(code, 0, out)
                section = _mod.load(self.snapshot)["commit_reclassification"]
                self.assertFalse(section["complete"])
                self.assertFalse(section["population_complete"])
                self.assertEqual(section["counts"], report_counts(**{outcome: 1}))

    def test_the_repositories_left_out_of_the_commit_csvs_are_recorded_by_outcome(self):
        """The step leaves out of the commit CSVs the shards of a repository with failed
        evidence or with commits that other commit heuristics classified, and it marks the
        repository's result with `left_out`. The release publishes none of these commits and
        states the number of such repositories in the evidence limit of their outcome, so the
        `commit_reclassification` section of the snapshot records the number per outcome. Here
        one `failed` repository is left out, and so is one of the two `no_path_history`
        repositories."""
        self._record()
        report = self._reclassification_report(
            {"o/a": "failed", "o/b": "no_path_history", "o/e": "no_path_history"}, False, False)
        # o/b and o/e have rows in output/repos.csv here, as o/a has in the fixture.
        _write_csv(self.output / "repos.csv",
                   [{"repo_name": name, **{c: "FALSE" for c in _mod.NAMED_MECHANISMS},
                     "context_file": "TRUE", "settings": "TRUE"}
                    for name in ("o/a", "o/b", "o/e")],
                   ["repo_name", *_mod.NAMED_MECHANISMS])
        written = json.loads(report.read_text())
        for repo in ("o/a", "o/b"):
            written["repositories"][repo].update(left_out=True, record_sha256="a" * 64)
        report.write_text(json.dumps(written))
        code, out = self._write()
        self.assertEqual(code, 0, out)
        self.assertEqual(_mod.load(self.snapshot)["commit_reclassification"]["left_out"],
                         report_counts(failed=1, no_path_history=1))
        self.assertEqual(self._write("--check")[0], 0)
        del written["repositories"]["o/a"]["left_out"]
        report.write_text(json.dumps(written))
        code, out = self._write("--check")
        self.assertEqual(code, 2, out)
        self.assertIn("commit_reclassification", out)
        self.assertEqual(self._write()[0], 0)
        self.assertEqual(_mod.load(self.snapshot)["commit_reclassification"]["left_out"],
                         report_counts(no_path_history=1))

    def test_only_the_published_repositories_and_the_clone_failures_are_counted(self):
        """The release states the evidence limits of the published dataset from this section.
        Counted over the whole collection, a repository that the collected file cleanup dropped
        would appear among the repositories without published commits or without commit logs,
        although it has no row and no commit in the release. A limit could then name more
        repositories than the release has. The reclassification report itself still has to
        agree with all its outcomes."""
        # o/a is published, o/b and o/d have no row in output/repos.csv, and o/c is the clone
        # failure of the fixture. The repositories without their collected tree are counted
        # without a row, as test_a_repository_without_its_collected_tree_is_counted shows.
        for published, expected in (
                ("failed", {"counts": report_counts(failed=1, unavailable=1),
                            "left_out": report_counts(failed=1),
                            "complete": False, "population_complete": False}),
                ("current", {"counts": report_counts(current=1, unavailable=1),
                             "left_out": report_counts(),
                             "complete": True, "population_complete": False})):
            with self.subTest(published=published):
                self._record()
                report = self._reclassification_report(
                    {"o/a": published, "o/b": "no_log", "o/c": "unavailable",
                     "o/d": "history_not_read"}, False, False)
                written = json.loads(report.read_text())
                for repo in ("o/a", "o/b"):
                    if written["repositories"][repo]["outcome"] != "current":
                        written["repositories"][repo].update(left_out=True,
                                                             record_sha256="a" * 64)
                report.write_text(json.dumps(written))
                code, out = self._write()
                self.assertEqual(code, 0, out)
                section = _mod.load(self.snapshot)["commit_reclassification"]
                self.assertEqual({key: section[key] for key in expected}, expected)

    def test_the_repositories_of_each_outcome_that_keeps_evidence_out_are_named(self):
        """The release README names the repositories of each incomplete outcome, of `failed` and
        of the clone failures (`unavailable`), so that a user can leave them out of a count of AI
        commits. The `commit_reclassification` section of the snapshot lists them by outcome,
        with the counted repositories alone: o/a is published, o/c is the clone failure of the
        fixture and o/d lacks its collected tree, while o/b has no row in output/repos.csv and
        limits nothing that the release publishes."""
        self._record()
        self._reclassification_report({"o/a": "no_path_history", "o/b": "no_log",
                                       "o/c": "unavailable", "o/d": "missing_tree"},
                                      False, False)
        code, out = self._write()
        self.assertEqual(code, 0, out)
        named = _mod.load(self.snapshot)["commit_reclassification"]["repositories"]
        self.assertEqual({outcome: names for outcome, names in named.items() if names},
                         {"no_path_history": ["o/a"], "unavailable": ["o/c"],
                          "missing_tree": ["o/d"]})
        self.assertEqual(set(named), {"no_log", "no_scope", "no_path_history",
                                      "history_incomplete", "history_not_read", "missing_record",
                                      "missing_tree", "failed", "unavailable"})
        self.assertEqual(self._write("--check")[0], 0)

    def test_a_repository_without_its_collected_tree_is_counted(self):
        """A repository with a per-repository commit record and no directory in the collected
        tree, `missing_tree`, has no row in output/repos.csv, because the detection step reads
        no file of it. The repository is in the sample all the same, and the release lacks its
        files and its commits. The section therefore counts it and records the reclassification
        as incomplete. Left out with the repositories that the collected file cleanup dropped,
        the repository would leave the section recording a complete reclassification and the
        README naming no evidence limit for it."""
        self._record()
        # o/d is flagged in the collected list and has no directory in the collected tree.
        self._reclassification_report({"o/a": "current", "o/d": "missing_tree"}, False, False)
        code, out = self._write()
        self.assertEqual(code, 0, out)
        section = _mod.load(self.snapshot)["commit_reclassification"]
        self.assertEqual(section["counts"], report_counts(current=1, missing_tree=1))
        self.assertFalse(section["complete"])
        self.assertFalse(section["population_complete"])
        self.assertEqual(self._write("--check")[0], 0)

    def test_a_repository_without_its_collected_tree_is_not_also_left_out(self):
        """The step also leaves out the shards of a `missing_tree` repository with a record that
        other commit heuristics classified. The repository has no row, and the README names it
        among the repositories that lack their collected tree. Counted in `left_out` too, the
        repository would be named a second time among the repositories without published
        commits."""
        self._record()
        report = self._reclassification_report({"o/a": "current", "o/d": "missing_tree"},
                                               False, False)
        written = json.loads(report.read_text())
        written["repositories"]["o/d"].update(left_out=True, record_sha256="d" * 64)
        report.write_text(json.dumps(written))
        code, out = self._write()
        self.assertEqual(code, 0, out)
        section = _mod.load(self.snapshot)["commit_reclassification"]
        self.assertEqual((section["counts"], section["left_out"]),
                         (report_counts(current=1, missing_tree=1), report_counts()))

    def test_unavailable_repositories_leave_only_the_population_incomplete(self):
        self._record()
        self._reclassification_report({"o/a": "current", "o/c": "unavailable"}, True, False)
        code, out = self._write()
        self.assertEqual(code, 0, out)
        section = _mod.load(self.snapshot)["commit_reclassification"]
        self.assertTrue(section["complete"])
        self.assertFalse(section["population_complete"])
        self._reclassification_report({"o/a": "current", "o/c": "unavailable"}, True, True)
        code, out = self._write()
        self.assertEqual(code, 1, out)
        self.assertIn("population_complete", out)

    def test_a_failed_assembly_cannot_be_recorded(self):
        self._record()
        before = self.snapshot.read_bytes()
        self._reclassification_report({"o/a": "current"}, False, False, assembled=False)
        code, out = self._write()
        self.assertEqual(code, 1, out)
        self.assertIn("assembly", out)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_a_refused_report_leaves_every_section_as_it_was(self):
        """A refused reclassification report exits 1, and the collection, sample and metadata
        sections derived before the report was read must not be written either. The record here
        is out of date in each of these sections, and a section written before the refusal
        therefore shows in the bytes."""
        stale = self.expected()
        for name in ("collection", "configured", "metadata"):
            stale[name]["derived_from"] = {"an earlier input": {"rows": 1}}
        self.snapshot.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n",
                                 encoding="utf-8")
        before = self.snapshot.read_bytes()
        self._reclassification_report({"o/a": "current"}, False, False, assembled=False)
        code, out = self._write()
        self.assertEqual(code, 1, out)
        self.assertIn("Left snapshot.json unchanged", out)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_a_summary_whose_counts_disagree_with_its_outcomes_is_refused(self):
        """Equal `complete` values are not enough. Counts that record two current repositories
        where the outcomes name one would otherwise be recorded as the dataset's provenance."""
        self._record()
        before = self.snapshot.read_bytes()
        report = self._reclassification_report({"o/a": "current"}, True, True)
        written = json.loads(report.read_text())
        written["counts"] = report_counts(current=2)
        report.write_text(json.dumps(written))
        code, out = self._write()
        self.assertEqual(code, 1, out)
        self.assertIn("The counts or the completeness of commit-reclassification.json differ "
                      "from its repository outcomes", out)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_a_check_after_the_reclassification_moved_exits_two(self):
        """The check compares the section's values and not only its provenance. A
        reclassification report from other heuristics over the same repositories leaves the
        provenance unchanged."""
        self._record()
        report = self._reclassification_report({"o/a": "current"}, True, True)
        self.assertEqual(self._write()[0], 0)
        report.write_text(report.read_text().replace('"classified_by": "current"',
                                                     '"classified_by": "later"'))
        code, out = self._write("--check")
        self.assertEqual(code, 2, out)
        self.assertIn("Would change commit_reclassification", out)

    def test_a_check_after_only_the_scan_times_moved_exits_two(self):
        self._record()
        with self.collected_csv.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        rows[0]["scanned_at"] = "2026-03-30"
        _write_csv(self.collected_csv, rows)
        code, out = self._write("--check")
        self.assertEqual(code, 2, out)
        self.assertIn("Would change collection.scanned_at_window", out)

    def test_the_scan_window_runs_from_the_first_scan_to_the_last(self):
        with self.collected_csv.open(newline="", encoding="utf-8") as source:
            rows = list(csv.DictReader(source))
        for row, scanned in zip(rows, ("2026-03-29", "2026-03-31", "2026-03-28", "2026-03-30")):
            row["scanned_at"] = scanned
        _write_csv(self.collected_csv, rows)
        self.assertEqual(self._write()[0], 0)
        self.assertEqual(_mod.load(self.snapshot)["collection"]["scanned_at_window"],
                         {"first": "2026-03-28", "last": "2026-03-31"})

    def test_a_note_beside_the_rows_without_commit_metadata_is_kept(self):
        """The writer rewrites the counts and keeps a note that a person wrote beside them on
        what the count means."""
        old = self.expected()
        old["metadata"]["rows_without_commit_metadata"] = {
            "note": "Kept rather than pruned.", "total": 45, "repositories": 9}
        self.snapshot.write_text(json.dumps(old), encoding="utf-8")
        self.assertEqual(self._write()[0], 0)
        self.assertEqual(_mod.load(self.snapshot)["metadata"]["rows_without_commit_metadata"],
                         {"note": "Kept rather than pruned.", "total": 2, "repositories": 1})

    def test_regeneration_replaces_obsolete_collection_dates_and_notes(self):
        old = self.expected()
        old['collection'].update(note='March collection',
                                 reconstructed_from=['an archive of the March collection'],
                                 reconstructed_on='2026-09-08')
        old['collection']['artifact_mtime_window']['note'] = 'Files copied in March'
        self.snapshot.write_text(json.dumps(old))
        with self.collected_csv.open() as source:
            rows = list(csv.DictReader(source))
        for row in rows:
            row['scanned_at'] = '2026-09-28T07:04:36Z'
        _write_csv(self.collected_csv, rows)
        self.assertEqual(self._write()[0], 0)
        collection = _mod.load(self.snapshot)['collection']
        self.assertEqual(collection['scanned_at_window'],
                         {'first': '2026-09-28T07:04:36Z',
                          'last': '2026-09-28T07:04:36Z'})
        for key in ('note', 'reconstructed_from', 'reconstructed_on'):
            self.assertNotIn(key, collection)
        self.assertNotIn('note', collection['artifact_mtime_window'])

    def test_output_check_does_not_require_an_extracted_collection(self):
        self.snapshot.write_text(json.dumps(self.expected()))
        shutil.rmtree(self.collected_dir)
        self.assertEqual(self._write('--check', '--outputs-only')[0], 0)
        with (self.output / 'settings.csv').open('a') as f:
            f.write('o/a,.claude/extra.json,\n')
        self.assertEqual(self._write('--check', '--outputs-only')[0], 2)



class FakeTransport:
    """A fake transport that returns a response by URL suffix. Each entry is (status, headers,
    body), an exception to raise, or a function of the number of calls to the suffix."""

    def __init__(self, routes=None, default=(404, {}, b'{"message":"Not Found"}')):
        self.routes = routes or {}
        self.default = default
        self.calls = []

    def __call__(self, url, token):
        self.calls.append((url, token))
        for suffix, answer in self.routes.items():
            if url.endswith(suffix):
                if isinstance(answer, Exception):
                    raise answer
                if callable(answer):
                    return answer(len([c for c in self.calls if c[0].endswith(suffix)]))
                return answer
        return self.default


class TestLoadArtifactShas(unittest.TestCase):
    """The per-file SHAs that rescue a repository with a head_sha that GitHub no longer has.

    The function names the output CSVs from the heuristics rather than finding them with a
    glob. A glob that matched nothing would return an empty map and disable the
    `RESCUED_BY_FILE_SHA` fallback without an error. These tests read
    `output/<mechanism>.csv`, and one of them checks that a leftover `-extended` copy is not
    read.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _table(self, name, rows, header=None):
        header = header or ["repo_name", "context_file",
                            "first_commit_sha", "last_commit_sha"]
        with open(self.tmp / name, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            w.writerows(rows)

    def test_it_collects_both_sha_columns_per_repository(self):
        self._table("context-files.csv", [
            {"repo_name": "o/a", "context_file": "CLAUDE.md",
             "first_commit_sha": "aaa", "last_commit_sha": "bbb"},
        ])
        self.assertEqual(_mod.load_artifact_shas(self.tmp), {"o/a": ["bbb", "aaa"]})

    def test_it_reads_every_mechanism_csv_not_just_one(self):
        self._table("context-files.csv", [
            {"repo_name": "o/a", "context_file": "CLAUDE.md",
             "first_commit_sha": "", "last_commit_sha": "bbb"}])
        self._table("skills.csv", [
            {"repo_name": "o/a", "skills.md": "SKILL.md",
             "first_commit_sha": "", "last_commit_sha": "ccc"}],
            header=["repo_name", "skills.md", "first_commit_sha", "last_commit_sha"])
        self.assertEqual(sorted(_mod.load_artifact_shas(self.tmp)["o/a"]), ["bbb", "ccc"])

    def test_a_sha_is_recorded_once_however_often_it_appears(self):
        self._table("context-files.csv", [
            {"repo_name": "o/a", "context_file": "A.md",
             "first_commit_sha": "aaa", "last_commit_sha": "aaa"},
            {"repo_name": "o/a", "context_file": "B.md",
             "first_commit_sha": "aaa", "last_commit_sha": "aaa"},
        ])
        self.assertEqual(_mod.load_artifact_shas(self.tmp), {"o/a": ["aaa"]})

    def test_blank_shas_contribute_nothing_and_a_blank_repo_name_is_skipped(self):
        # The repository keeps an empty bucket rather than being left out. The only caller
        # cannot tell an empty bucket from a missing repository, because it reads
        # artifact_shas.get(repo, []) and iterates the result.
        self._table("context-files.csv", [
            {"repo_name": "o/a", "context_file": "A.md",
             "first_commit_sha": "  ", "last_commit_sha": ""},
            {"repo_name": "", "context_file": "B.md",
             "first_commit_sha": "zzz", "last_commit_sha": "zzz"},
        ])
        shas = _mod.load_artifact_shas(self.tmp)
        self.assertEqual(shas.get("o/a", []), [])
        self.assertNotIn("", shas)

    def test_a_leftover_extended_twin_is_not_read(self):
        # A leftover `-extended` copy in a checkout must not be counted a second time, because
        # every SHA in such a copy is already in the CSV that it was written beside. The names
        # come from the heuristics rather than from a glob.
        self._table("context-files-extended.csv", [
            {"repo_name": "o/a", "context_file": "CLAUDE.md",
             "first_commit_sha": "aaa", "last_commit_sha": "bbb"}])
        self.assertEqual(_mod.load_artifact_shas(self.tmp), {})

    def test_it_reads_a_csv_for_every_mechanism_the_heuristics_names(self):
        # One CSV per mechanism, each with a different SHA, so that the count shows how many
        # CSVs were opened.
        for index, filename in enumerate(heuristics.csv_filenames()):
            artifact = heuristics.artifact_column_for_file(filename)
            self._table(filename,
                        [{"repo_name": "o/a", artifact: "one.md",
                          "first_commit_sha": "", "last_commit_sha": f"sha{index}"}],
                        header=["repo_name", artifact, "first_commit_sha",
                                "last_commit_sha"])
        self.assertEqual(len(_mod.load_artifact_shas(self.tmp)["o/a"]),
                         len(heuristics.MECHANISM_CSVS))


class TestCollectTokens(unittest.TestCase):
    """The GitHub access tokens come from agentcontext.credentials, which
    tests/test_credentials.py tests. This class tests what snapshot validation requires of that
    module."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "credentials.csv"

    def test_the_environment_comes_before_the_file_and_nothing_repeats(self):
        self.path.write_text("service,account,credential\ngithub,a,dup\ngithub,a,other\n"
                             "openai,,sk-not-a-github-token\n")
        self.assertEqual(_mod.collect_tokens(self.path, {"GITHUB_TOKENS": "env_a,dup"}),
                         ["env_a", "dup", "other"])

    def test_no_github_access_tokens_anywhere_is_an_empty_list(self):
        self.assertEqual(_mod.collect_tokens(None, {}), [])
        self.assertEqual(_mod.collect_tokens(self.path, {}), [])


class TestTokenRotator(unittest.TestCase):
    def test_it_round_robins(self):
        r = _mod.TokenRotator(["a", "b", "c"], sleep=lambda s: None, now=lambda: 0.0)
        self.assertEqual([r.get() for _ in range(4)], ["a", "b", "c", "a"])

    def test_an_empty_pool_returns_nothing(self):
        self.assertIsNone(_mod.TokenRotator([]).get())

    def test_a_parked_github_access_token_is_skipped(self):
        clock = [100.0]
        r = _mod.TokenRotator(["a", "b"], sleep=lambda s: None, now=lambda: clock[0])
        r.park("a", 200.0)
        self.assertEqual([r.get() for _ in range(3)], ["b", "b", "b"])

    def test_a_parked_github_access_token_comes_back_once_its_reset_passes(self):
        clock = [100.0]
        r = _mod.TokenRotator(["a", "b"], sleep=lambda s: None, now=lambda: clock[0])
        r.park("a", 200.0)
        self.assertEqual(r.get(), "b")
        clock[0] = 250.0
        self.assertIn("a", {r.get(), r.get()})

    def test_it_waits_when_every_github_access_token_is_parked(self):
        clock = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            clock[0] = 300.0

        r = _mod.TokenRotator(["a"], sleep=sleep, now=lambda: clock[0])
        r.park("a", 200.0)
        self.assertEqual(r.get(), "a")
        self.assertTrue(slept)

    def test_parking_never_shortens_an_existing_cooldown(self):
        r = _mod.TokenRotator(["a"], sleep=lambda s: None, now=lambda: 0.0)
        r.park("a", 500.0)
        r.park("a", 100.0)
        self.assertEqual(r._ready_at["a"], 500.0)


class TestGitHubClient(unittest.TestCase):
    def test_a_200_is_decoded(self):
        client, _ = _client({"/repos/o/r": (200, {}, _body({"default_branch": "main"}))})
        self.assertEqual(client.get("/repos/o/r"), (200, {"default_branch": "main"}))

    def test_a_404_is_returned_not_retried(self):
        client, transport = _client({"/repos/o/r": (404, {}, b'{"message":"Not Found"}')})
        status, _ = client.get("/repos/o/r")
        self.assertEqual(status, 404)
        self.assertEqual(len(transport.calls), 1)

    def test_a_server_error_is_retried_then_reported_as_a_failure(self):
        client, transport = _client({"/repos/o/r": (502, {}, b"")}, max_attempts=3)
        status, detail = client.get("/repos/o/r")
        self.assertEqual(status, 0)
        self.assertIn("502", detail)
        self.assertEqual(len(transport.calls), 3)

    def test_a_transport_exception_is_retried_then_reported_as_a_failure(self):
        client, transport = _client({"/repos/o/r": OSError("connection reset")},
                                    max_attempts=2)
        status, detail = client.get("/repos/o/r")
        self.assertEqual(status, 0)
        self.assertIn("connection reset", detail)
        self.assertEqual(len(transport.calls), 2)

    def test_a_rate_limited_response_parks_the_github_access_token_and_tries_another(self):
        answers = [
            (403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "9999"}, b""),
            (200, {}, _body({"ok": True})),
        ]
        client, transport = _client(
            {"/repos/o/r": lambda n: answers[min(n - 1, 1)]}, tokens=("t1", "t2"))
        self.assertEqual(client.get("/repos/o/r"), (200, {"ok": True}))
        self.assertEqual([c[1] for c in transport.calls], ["t1", "t2"])

    def test_a_retry_after_header_also_counts_as_rate_limiting(self):
        answers = [(429, {"Retry-After": "1"}, b""), (200, {}, _body({"ok": True}))]
        client, _ = _client({"/repos/o/r": lambda n: answers[min(n - 1, 1)]},
                            tokens=("t1", "t2"))
        self.assertEqual(client.get("/repos/o/r"), (200, {"ok": True}))

    def test_a_secondary_rate_limit_that_only_its_message_marks_holds_the_github_access_token_back(self):
        """GitHub's response to a secondary rate limit can have neither `Retry-After` nor
        `X-RateLimit-Remaining: 0`. Only its message marks the limit, and
        `github_api.rate_limit_wait` reads that message as a wait of a minute."""
        message = _body({"message": "You have exceeded a secondary rate limit. Please wait a few "
                                    "minutes before you try again."})
        answers = [(403, {"content-type": "application/json"}, message),
                   (200, {}, _body({"ok": True}))]
        client, transport = _client({"/repos/o/r": lambda n: answers[min(n - 1, 1)]},
                                    tokens=("t1", "t2"))
        self.assertEqual(client.get("/repos/o/r"), (200, {"ok": True}))
        self.assertEqual([c[1] for c in transport.calls], ["t1", "t2"])
        self.assertEqual(client._rotator._ready_at, {"t1": 1060.0, "t2": 0.0})

    def test_lowercase_rate_limit_headers_count_as_rate_limiting(self):
        answers = [(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1300"}, b""),
                   (200, {}, _body({"ok": True}))]
        client, _ = _client({"/repos/o/r": lambda n: answers[min(n - 1, 1)]},
                            tokens=("t1", "t2"))
        self.assertEqual(client.get("/repos/o/r"), (200, {"ok": True}))
        self.assertEqual(client._rotator._ready_at["t1"], 1300.0)

    def test_a_403_that_is_not_rate_limiting_is_returned(self):
        client, transport = _client({"/repos/o/r": (403, {}, _body({"message": "no"}))})
        status, _ = client.get("/repos/o/r")
        self.assertEqual(status, 403)
        self.assertEqual(len(transport.calls), 1)

    def test_an_unparseable_body_does_not_raise(self):
        client, _ = _client({"/repos/o/r": (200, {}, b"<html>")})
        self.assertEqual(client.get("/repos/o/r"), (200, None))


class TestHeadCommitDate(unittest.TestCase):
    def test_it_reads_the_base_commits_committer_date(self):
        payload = _compare("ahead", "2026-03-29T02:27:29Z")
        self.assertEqual(_mod.head_commit_date(payload), "2026-03-29T02:27:29Z")

    def test_a_response_without_one_gives_an_empty_string(self):
        self.assertEqual(_mod.head_commit_date({"status": "ahead"}), "")
        self.assertEqual(_mod.head_commit_date(None), "")


class TestCheckRepository(unittest.TestCase):
    def test_the_row_fixture_names_the_columns_the_check_reads(self):
        """`check_repository` reads `head_commit_date`. A fixture under another column name
        would leave `recorded` blank, and every test below would then compare a date with a
        blank instead of with the recorded date."""
        metadata = set(_row()) - {"claude", "first_commit_sha"}
        self.assertEqual(metadata, set(heuristics.META_COLUMNS))
        self.assertEqual(_row()["head_commit_date"], "2026-03-29T02:27:29Z")

    def test_no_recorded_sha_is_reported_rather_than_checked(self):
        client, transport = _client({})
        result = _mod.check_repository(client, _row(head_sha="", branch=""), [])
        self.assertEqual(result["outcome"], "NO_SHA")
        self.assertEqual(transport.calls, [])

    def test_a_commit_still_on_the_recorded_branch_is_ok(self):
        sha = "a" * 40
        client, transport = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("ahead", "2026-03-29T02:27:29Z"))),
        })
        result = _mod.check_repository(client, _row(), [])
        self.assertEqual(result["outcome"], "OK")
        self.assertEqual(result["reached_via"], "branch:main")
        self.assertEqual(result["compare_status"], "ahead")
        self.assertEqual(len(transport.calls), 1)

    def test_an_identical_head_is_also_ok(self):
        sha = "a" * 40
        client, _ = _client({f"/compare/{sha}...main": (200, {}, _body(_compare("identical")))})
        self.assertEqual(_mod.check_repository(client, _row(), [])["outcome"], "OK")

    def test_the_timestamp_check_records_a_match(self):
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("ahead", "2026-03-29T02:27:29Z"))),
        })
        result = _mod.check_repository(client, _row(), [])
        self.assertEqual(result["head_commit_date"], "2026-03-29T02:27:29Z")
        self.assertEqual(result["timestamp_is_head_commit_date"], "TRUE")

    def test_the_same_instant_in_another_offset_is_a_match(self):
        # The collected list keeps the committer's offset, as git's %cI writes it, and GitHub
        # returns UTC. Compared as strings, the dates of every committer outside UTC would
        # differ.
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("ahead", "2026-03-29T00:27:29Z"))),
        })
        result = _mod.check_repository(client, _row(head_commit_date="2026-03-29T02:27:29+02:00"), [])
        self.assertEqual(result["timestamp_is_head_commit_date"], "TRUE")

    def test_a_commit_in_a_zone_past_24_hours_is_compared_by_its_instant(self):
        """git prints a commit's offset of +2500 as `+25:00`, which `datetime` refuses as a time
        zone. The collected value, read from a real commit, names the instant of the commit's
        epoch seconds, and GitHub returns that instant in UTC."""
        import subprocess
        from datetime import datetime, timezone
        from tests._fixtures import _add_commit_in_zone, _init_git_repo
        repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        _init_git_repo(repo)
        _add_commit_in_zone(repo, "CLAUDE.md", "# one\n", 1700000000, "+2500")
        shown = subprocess.run(["git", "-C", str(repo), "show", "-s", "--format=%cI%n%ct",
                                "HEAD"], capture_output=True, text=True, check=True).stdout
        committer_date, epoch = shown.split()
        utc = datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("ahead", utc))),
        })
        result = _mod.check_repository(client, _row(head_commit_date=committer_date), [])
        self.assertEqual(result["timestamp_is_head_commit_date"], "TRUE")

    def test_a_commit_after_the_year_9999_is_a_timestamp_that_does_not_convert(self):
        """git prints a commit of the year 10000 in the offset -0100 as
        `9999-12-31T23:30:00-01:00`, an instant that `datetime` cannot hold. Converting it
        raises an OverflowError, which would stop `snapshot validation` on every run. As with a
        value that does not parse, the value names no instant that GitHub's date can match."""
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("ahead", "2026-03-29T02:27:29Z"))),
        })
        result = _mod.check_repository(
            client, _row(head_commit_date="9999-12-31T23:30:00-01:00"), [])
        self.assertEqual(result["outcome"], "OK")
        self.assertEqual(result["timestamp_is_head_commit_date"], "FALSE")

    def test_the_timestamp_check_records_a_mismatch(self):
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("ahead", "2025-01-01T00:00:00Z"))),
        })
        result = _mod.check_repository(client, _row(), [])
        self.assertEqual(result["timestamp_is_head_commit_date"], "FALSE")

    def test_a_renamed_branch_is_found_through_the_default_branch(self):
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (404, {}, b"{}"),
            "/repos/owner/repo": (200, {}, _body({"default_branch": "trunk"})),
            f"/compare/{sha}...trunk": (200, {}, _body(_compare("ahead"))),
        })
        result = _mod.check_repository(client, _row(), [])
        self.assertEqual(result["outcome"], "RENAMED")
        self.assertEqual(result["reached_via"], "default_branch:trunk")

    def test_a_diverged_branch_falls_through_to_the_default_branch(self):
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("diverged"))),
            "/repos/owner/repo": (200, {}, _body({"default_branch": "trunk"})),
            f"/compare/{sha}...trunk": (200, {}, _body(_compare("ahead"))),
        })
        self.assertEqual(_mod.check_repository(client, _row(), [])["outcome"], "RENAMED")

    def test_a_deleted_repository_is_reported_as_such(self):
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (404, {}, b"{}"),
            "/repos/owner/repo": (404, {}, b'{"message":"Not Found"}'),
        })
        self.assertEqual(_mod.check_repository(client, _row(), [])["outcome"], "REPO_GONE")

    def test_an_artifact_sha_rescues_a_rewritten_head(self):
        sha, artifact = "a" * 40, "c" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("diverged"))),
            "/repos/owner/repo": (200, {}, _body({"default_branch": "main"})),
            f"/compare/{artifact}...main": (200, {}, _body(_compare("ahead"))),
        })
        result = _mod.check_repository(client, _row(), [artifact])
        self.assertEqual(result["outcome"], "RESCUED_BY_FILE_SHA")
        self.assertEqual(result["reached_via"], f"artifact_sha:{artifact[:12]}")

    def test_nothing_reachable_is_gone(self):
        sha = "a" * 40
        client, _ = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("diverged"))),
            "/repos/owner/repo": (200, {}, _body({"default_branch": "main"})),
        }, default=(404, {}, b"{}"))
        result = _mod.check_repository(client, _row(), ["c" * 40])
        self.assertEqual(result["outcome"], "GONE")

    def test_a_request_that_keeps_failing_is_an_error_not_a_gone_repository(self):
        # A failing network must never be recorded as a repository that disappeared, because
        # the whole run depends on that distinction.
        sha = "a" * 40
        client, _ = _client({f"/compare/{sha}...main": OSError("timed out")},
                            max_attempts=2)
        result = _mod.check_repository(client, _row(), [])
        self.assertEqual(result["outcome"], "ERROR")
        self.assertIn("timed out", result["detail"])

    def test_an_artifact_sha_equal_to_the_head_is_not_retried(self):
        sha = "a" * 40
        client, transport = _client({
            f"/compare/{sha}...main": (200, {}, _body(_compare("diverged"))),
            "/repos/owner/repo": (200, {}, _body({"default_branch": "main"})),
        }, default=(404, {}, b"{}"))
        _mod.check_repository(client, _row(), [sha])
        compares = [c for c in transport.calls if "/compare/" in c[0]]
        self.assertEqual(len(compares), 1)


class TestInScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.collected = Path(self.tmp.name) / "data" / "collected"
        (self.collected / "owner§archived").mkdir(parents=True)
        # The list that the cloning step writes at the root of the tree that it lists. The
        # fixture has the list because the real tree has it. Scope is decided from the
        # directories under that root, and a list read as a repository would put a repository
        # named `repos.csv` in scope and make the validator query GitHub for it.
        (self.collected / "repos.csv").write_text("repo_name\n", encoding="utf-8")

    def test_an_archived_repository_is_in_scope(self):
        rows = [_row(repo_name="owner/archived", claude="FALSE")]
        self.assertEqual(len(_mod.in_scope(rows, self.collected)), 1)

    def test_a_flagged_row_with_no_directory_is_in_scope(self):
        rows = [_row(repo_name="owner/emptied", claude="TRUE")]
        self.assertEqual(len(_mod.in_scope(rows, self.collected)), 1)

    def test_a_clone_failure_is_in_scope(self):
        rows = [_row(repo_name="owner/failed", claude="FALSE", head_sha="", branch="")]
        self.assertEqual(len(_mod.in_scope(rows, self.collected)), 1)

    def test_an_unflagged_row_with_no_directory_is_out_of_scope(self):
        rows = [_row(repo_name="owner/plain", claude="FALSE")]
        self.assertEqual(_mod.in_scope(rows, self.collected), [])

    def test_the_list_at_the_root_of_the_tree_does_not_change_the_scope(self):
        # Scope is read from the entries under the tree root, and the two lists that the
        # cloning step writes are among them. An entry counted as a repository would put a row
        # in scope, and the validator would then query GitHub under a name that no step
        # collected.
        rows = [_row(repo_name="owner/archived", claude="FALSE"),
                _row(repo_name="owner/plain", claude="FALSE")]
        (self.collected / "clone_failed.csv").write_text("repo_name\n", encoding="utf-8")
        with_lists = _mod.in_scope(rows, self.collected)
        for name in ("repos.csv", "clone_failed.csv"):
            (self.collected / name).unlink()
        self.assertEqual(with_lists, _mod.in_scope(rows, self.collected))
        self.assertEqual([r["repo_name"] for r in with_lists], ["owner/archived"])


class TestLoadDone(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "results.csv"

    def _write(self, rows):
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=_mod.FIELDNAMES)
            w.writeheader()
            for r in rows:
                w.writerow({**{k: "" for k in _mod.FIELDNAMES}, **r})

    def test_a_missing_file_is_nothing_done(self):
        self.assertEqual(_mod.load_done(self.path), {})

    def test_finished_rows_are_resumed(self):
        self._write([{"repo_name": "a/b", "outcome": "OK"}])
        self.assertEqual(list(_mod.load_done(self.path)), ["a/b"])

    def test_an_error_row_is_retried_rather_than_resumed(self):
        self._write([{"repo_name": "a/b", "outcome": "ERROR"},
                     {"repo_name": "c/d", "outcome": "GONE"}])
        self.assertEqual(list(_mod.load_done(self.path)), ["c/d"])


class TestSummarize(unittest.TestCase):
    def test_the_rate_excludes_the_rows_with_no_sha(self):
        results = [{"outcome": "OK", "repo_name": "a/1"},
                   {"outcome": "GONE", "repo_name": "a/2"},
                   {"outcome": "NO_SHA", "repo_name": "a/3"}]
        summary = _mod.summarize(results)
        self.assertEqual(summary["reachable_or_rescued"], 1)
        self.assertEqual(summary["reachable_or_rescued_rate"], 0.5)

    def test_rescued_and_renamed_both_count_as_surviving(self):
        results = [{"outcome": o, "repo_name": f"a/{i}"} for i, o in
                   enumerate(["OK", "RENAMED", "RESCUED_BY_FILE_SHA"])]
        self.assertEqual(_mod.summarize(results)["reachable_or_rescued_rate"], 1.0)

    def test_every_lost_repository_is_named(self):
        results = [{"outcome": "GONE", "repo_name": "a/1"},
                   {"outcome": "REPO_GONE", "repo_name": "a/2"},
                   {"outcome": "OK", "repo_name": "a/3"}]
        self.assertEqual(_mod.summarize(results)["gone"], ["a/1", "a/2"])

    def test_the_timestamp_finding_is_counted(self):
        results = [
            {"outcome": "OK", "repo_name": "a/1", "head_commit_date": "x",
             "timestamp_is_head_commit_date": "TRUE"},
            {"outcome": "OK", "repo_name": "a/2", "head_commit_date": "y",
             "timestamp_is_head_commit_date": "FALSE"},
            {"outcome": "NO_SHA", "repo_name": "a/3"},
        ]
        ts = _mod.summarize(results)["timestamp_is_head_commit_date"]
        self.assertEqual((ts["compared"], ts["matching"]), (2, 1))


class TestUnexpectedStatusIsNotAVerdict(unittest.TestCase):
    """A status that is no evidence about the repository is an ERROR rather than GONE, so that
    one expired GitHub access token cannot publish the whole sample as lost."""

    def _verdict(self, status, body=b'{"message":"nope"}'):
        sha = "a" * 40
        client, _ = _client({}, default=(status, {}, body))
        return _mod.check_repository(client, _row(), [])["outcome"]

    def test_an_expired_github_access_token_is_an_error(self):
        self.assertEqual(self._verdict(401), "ERROR")

    def test_a_forbidden_response_is_an_error(self):
        self.assertEqual(self._verdict(403), "ERROR")

    def test_a_takedown_is_an_error(self):
        self.assertEqual(self._verdict(451), "ERROR")

    def test_a_200_that_is_not_json_is_an_error(self):
        self.assertEqual(self._verdict(200, b"<html>proxy error</html>"), "ERROR")

    def test_a_real_404_is_still_a_verdict(self):
        self.assertEqual(self._verdict(404), "REPO_GONE")


class TestErrorRowsDoNotDistortTheRate(unittest.TestCase):
    def test_errors_are_out_of_the_denominator(self):
        # A failing network would otherwise lower the survival rate.
        results = [{"outcome": "OK", "repo_name": "a/1"},
                   {"outcome": "ERROR", "repo_name": "a/2"},
                   {"outcome": "NO_SHA", "repo_name": "a/3"}]
        self.assertEqual(_mod.summarize(results)["reachable_or_rescued_rate"], 1.0)


class TestTheRowsTheEqualityCannotCover(unittest.TestCase):
    """The equality is testable only where the commit still exists and has a date, which limits
    the tested rows to the rows that survived. The finding therefore counts the rows that it
    could not compare. Without that count, the match count would read as a statement about the
    whole sample."""

    WINDOW = {"first": "2026-03-29T00:00:00Z", "last": "2026-04-01T23:59:59Z"}

    def _results(self):
        return [
            {"outcome": "OK", "repo_name": "a/1", "head_commit_date": "2026-03-30T00:00:00Z",
             "recorded_timestamp": "2026-03-30T00:00:00Z",
             "timestamp_is_head_commit_date": "TRUE"},
            {"outcome": "GONE", "repo_name": "a/2", "head_commit_date": "",
             "recorded_timestamp": "2025-11-24T22:01:54Z"},
            {"outcome": "NO_SHA", "repo_name": "a/3", "head_commit_date": "",
             "recorded_timestamp": "2026-03-30T06:10:32Z"},
        ]

    def test_the_uncompared_rows_are_counted(self):
        finding = _mod.summarize(self._results())["timestamp_is_head_commit_date"]
        self.assertEqual(finding["not_compared"]["count"], 2)

    def test_they_are_broken_down_by_why(self):
        finding = _mod.summarize(self._results())["timestamp_is_head_commit_date"]
        self.assertEqual(finding["not_compared"]["outcomes"], {"GONE": 1, "NO_SHA": 1})

    def test_without_a_window_no_second_check_is_claimed(self):
        finding = _mod.summarize(self._results())["timestamp_is_head_commit_date"]
        self.assertNotIn("outside_the_collection_window", finding["not_compared"])

    def test_a_timestamp_before_the_window_is_not_a_scan_time(self):
        finding = _mod.summarize(self._results(),
                                 self.WINDOW)["timestamp_is_head_commit_date"]
        self.assertEqual(finding["not_compared"]["outside_the_collection_window"], 1)

    def test_a_timestamp_inside_the_window_stays_unresolved(self):
        results = [r for r in self._results() if r["outcome"] == "NO_SHA"]
        self.assertEqual(_mod.outside_window(results, self.WINDOW), 0)

    def test_an_offset_timestamp_is_placed_by_its_instant(self):
        # 2026-04-02T01:00:00+02:00 is 2026-04-01T23:00:00Z, inside the window. As a string, it
        # sorts after the window's end, and 2026-03-29T01:00:00+05:00, before its start, sorts
        # inside it.
        inside = {"outcome": "GONE", "repo_name": "a/1", "head_commit_date": "",
                  "recorded_timestamp": "2026-04-02T01:00:00+02:00"}
        before = {"outcome": "GONE", "repo_name": "a/2", "head_commit_date": "",
                  "recorded_timestamp": "2026-03-29T01:00:00+05:00"}
        self.assertEqual(_mod.outside_window([inside], self.WINDOW), 0)
        self.assertEqual(_mod.outside_window([before], self.WINDOW), 1)

    def test_a_row_with_no_recorded_timestamp_is_not_counted_either_way(self):
        results = [{"outcome": "GONE", "repo_name": "a/1", "head_commit_date": "",
                    "recorded_timestamp": ""}]
        self.assertEqual(_mod.outside_window(results, self.WINDOW), 0)

    def test_an_incomplete_window_disables_the_check(self):
        for window in (None, {}, {"first": "2026-03-29T00:00:00Z"}, {"last": "x"}):
            self.assertIsNone(_mod.outside_window(self._results(), window), window)


class TestSummarizeOnlyRecords(unittest.TestCase):
    """The recording path. `--no-snapshot` returns before it, and only these tests therefore
    reach the line that writes the section."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # The snapshot that the run should write: the snapshot beside its result file. The
        # module's own default points to a second path that must stay unchanged, so that a run
        # over a scratch directory cannot record its summary in the tracked snapshot.
        self.snapshot = self.tmp / "snapshot.json"
        self.tracked = self.tmp / "tracked.json"
        self._orig = _mod.SNAPSHOT_PATH
        _mod.SNAPSHOT_PATH = self.tracked

    def tearDown(self):
        _mod.SNAPSHOT_PATH = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, **over):
        rows = [{"repo_name": "o/a", "head_commit_date": "2026-03-30T00:00:00Z",
                 "branch": "main", "head_sha": "a" * 40, "claude": "TRUE"}]
        results = [{"repo_name": "o/a", "outcome": "OK", "head_commit_date": "2026-03-30T00:00:00Z",
                    "recorded_timestamp": "2026-03-30T00:00:00Z",
                    "timestamp_is_head_commit_date": "TRUE"}]
        inp, out = self.tmp / "data" / "collected" / "repos.csv", self.tmp / "results.csv"
        for path, data in ((inp, rows), (out, results)):
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
        ns = argparse.Namespace(input=inp, output=out, collected_dir=self.tmp / "absent",
                                no_snapshot=False, record_with_errors=False)
        ns.__dict__.update(over)
        return ns

    def test_it_records_without_raising(self):
        self.assertEqual(_mod.summarize_only(self._args()), 0)

    def test_the_section_is_written_to_the_file(self):
        """The section keeps the date on which the results were gathered. A test that checks
        only that a `validation` key exists would not detect a dropped date."""
        _mod.update("validation", {"checked": 99, "checked_on": "2026-09-08"},
                    self.snapshot, derived_from=PROV)
        _mod.summarize_only(self._args())
        section = _mod.load(self.snapshot)["validation"]
        self.assertEqual(section["checked_on"], "2026-09-08")
        self.assertEqual((section["checked"], section["outcomes"],
                          section["reachable_or_rescued"], section["gone"]),
                         (1, {"OK": 1}, 1, []))

    def test_a_complete_result_file_drops_the_marker_of_the_record_before(self):
        """A result file that a resumed run has completed covers the scope, which the summary
        checks, and contains no ERROR row. None of its counts is a lower bound, and the marker
        of the previous record is therefore dropped rather than kept."""
        for marker in ("Covers 1 of the 2 repositories in scope",
                       "3 repository/repositories could not be checked (recorded with "
                       "--record-with-errors). The counts below are a lower bound."):
            with self.subTest(marker=marker):
                _mod.update("validation", {"checked": 99, "checked_on": "2026-09-08",
                                           "incomplete": marker},
                            self.snapshot, derived_from=PROV)
                self.assertEqual(_mod.summarize_only(self._args()), 0)
                section = _mod.load(self.snapshot)["validation"]
                self.assertNotIn("incomplete", section)
                self.assertEqual(section["checked_on"], "2026-09-08")

    def test_the_marker_counts_the_errors_of_this_result_file(self):
        _mod.update("validation", {"checked": 99, "checked_on": "2026-09-08",
                                   "incomplete": "3 repository/repositories could not be "
                                                 "checked"},
                    self.snapshot, derived_from=PROV)
        self.assertEqual(
            _mod.summarize_only(self._with_an_error_row(record_with_errors=True)), 0)
        section = _mod.load(self.snapshot)["validation"]
        self.assertTrue(section["incomplete"].startswith(
            "1 repository/repositories could not be checked"), section["incomplete"])

    def _with_an_error_row(self, **over):
        args = self._args(**over)
        with open(args.output, "a", newline="", encoding="utf-8") as f:
            f.write("o/b,ERROR,,,\n")
        with open(args.input, "a", newline="", encoding="utf-8") as f:
            f.write("o/b,2026-03-30T00:00:00Z,main," + "b" * 40 + ",TRUE\n")
        return args

    def test_a_result_file_with_an_error_row_is_refused(self):
        """A failed request does not check a repository. Recording the file would replace a
        complete record with a record that covers fewer repositories."""
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_mod.summarize_only(self._with_an_error_row()), 1)
        self.assertEqual(_mod.load(self.snapshot), {})

    def test_record_with_errors_records_it_as_a_lower_bound(self):
        self.assertEqual(
            _mod.summarize_only(self._with_an_error_row(record_with_errors=True)), 0)
        section = _mod.load(self.snapshot)["validation"]
        self.assertEqual(section["outcomes"], {"ERROR": 1, "OK": 1})
        self.assertIn("1 repository/repositories could not be checked", section["incomplete"])

    def test_nothing_to_check_against_is_refused(self):
        """None of these cases leaves evidence that the file is complete: no result file, a
        result file of a header alone, no input to take the scope from, and an input that puts
        nothing in scope. The test asserts the message too, because a header-only result file
        also falls short of the scope, and that refusal would otherwise pass this test."""
        cases = {
            "Found no result file to summarize": lambda a: a.output.unlink(),
            "contains no results": lambda a: a.output.write_text(
                "repo_name,outcome\n", encoding="utf-8"),
            "Name the input that defines the scope with --input": lambda a: a.input.unlink(),
            "is in scope to check the results against": lambda a: a.input.write_text(
                "repo_name,head_commit_date,branch,head_sha,claude\n"
                "o/a,2026-03-30T00:00:00Z,main," + "a" * 40 + ",FALSE\n", encoding="utf-8"),
        }
        for message, spoil in cases.items():
            with self.subTest(message):
                args = self._args()
                spoil(args)
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    self.assertEqual(_mod.summarize_only(args), 1)
                self.assertIn(message, err.getvalue())
                self.assertEqual(_mod.load(self.snapshot), {})

    def test_the_snapshot_beside_the_result_file_is_the_one_written(self):
        _mod.summarize_only(self._args())
        self.assertFalse(self.tracked.exists())

    def test_the_provenance_names_the_scope_it_checked(self):
        _mod.summarize_only(self._args())
        section = _mod.load(self.snapshot)["validation"]
        self.assertEqual(section["derived_from"]["results.csv"], {"rows": 1, "in_scope": 1})

    def test_a_result_file_missing_a_repository_is_refused(self):
        args = self._args()
        with open(args.input, "a", newline="", encoding="utf-8") as f:
            f.write("o/b,2026-03-30T00:00:00Z,main," + "b" * 40 + ",TRUE\n")
        self.assertEqual(_mod.summarize_only(args), 1)
        self.assertEqual(_mod.load(self.snapshot), {})


class TestValidateRecordsOnlyWhatItChecked(unittest.TestCase):
    """The two refusals of `snapshot validation` before it records: a run with failed
    requests, and a run that did not cover the scope, which `--limit` produces."""

    NAMES = ("o/a", "o/b", "o/c")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.input = self.tmp / "repos.csv"
        self.output = self.tmp / "snapshot-validation.csv"
        self.snapshot = self.tmp / "snapshot.json"
        # Each repository is flagged and has no directory, which puts all three in scope.
        _write_csv(self.input, [{"repo_name": n, "head_sha": "a" * 40, "claude": "TRUE"}
                                for n in self.NAMES])

    def _validate(self, *extra, error_for=()) -> int:
        def answer(client, row, shas):
            outcome = "ERROR" if row["repo_name"] in error_for else "OK"
            return {**{k: "" for k in _mod.FIELDNAMES}, "repo_name": row["repo_name"],
                    "outcome": outcome}

        argv = ["-i", str(self.input), "-o", str(self.output),
                "--collected-dir", str(self.tmp / "absent"),
                "--output-dir", str(self.tmp / "absent"),
                "--credentials", str(self.tmp / "absent"), "-w", "1", *extra]
        with mock.patch.object(_mod, "check_repository", answer), \
                mock.patch.object(_mod, "SNAPSHOT_PATH", self.tmp / "must_not_be_written.json"), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "t"}, clear=False), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return _mod.main_validate(argv)

    def test_a_complete_run_is_recorded(self):
        self.assertEqual(self._validate(), 0)
        section = _mod.load(self.snapshot)["validation"]
        self.assertEqual((section["checked"], section["outcomes"]), (3, {"OK": 3}))
        self.assertNotIn("incomplete", section)

    def test_a_run_with_a_failed_request_is_not_recorded(self):
        self.assertEqual(self._validate(error_for={"o/b"}), 1)
        self.assertEqual(_mod.load(self.snapshot), {})

    def test_a_run_with_a_failed_request_is_recorded_as_a_lower_bound_when_requested(self):
        self.assertEqual(self._validate("--record-with-errors", error_for={"o/b"}), 1)
        section = _mod.load(self.snapshot)["validation"]
        self.assertEqual(section["outcomes"], {"ERROR": 1, "OK": 2})
        self.assertIn("1 repository/repositories could not be checked", section["incomplete"])

    def test_a_run_short_of_the_scope_is_not_recorded(self):
        """A trial run with --limit would record today's date, a rate over the limited
        repositories and an empty gone list. The gone list is the only place that names a lost
        repository."""
        self.assertEqual(self._validate("--limit", "2"), 0)
        self.assertEqual(_mod.load(self.snapshot), {})

    def test_a_run_short_of_the_scope_is_recorded_as_a_lower_bound_when_requested(self):
        self.assertEqual(self._validate("--limit", "2", "--record-with-errors"), 0)
        section = _mod.load(self.snapshot)["validation"]
        self.assertEqual(section["checked"], 2)
        self.assertIn("Covers 2 of the 3 repositories in scope", section["incomplete"])


class TestTheStaleTemporary(unittest.TestCase):
    """`update` names its temporary file with `NamedTemporaryFile`, which gives a random name.
    The `except` branch that removes the file handles an exception, but not a SIGKILL, an OOM
    kill or the `os._exit` that a stop handler ends with, and the next update picks a different
    name. Because `snapshot.json` is tracked, the leftover file fails `release`'s inventory and
    shows in the next `git status`."""

    def test_a_stray_temporary_beside_the_snapshot_is_removed_before_writing(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "snapshot.json"
        path.write_text("{}\n", encoding="utf-8")
        stray = tmp / "snapshot.json.abc12345.tmp"
        stray.write_text("what a killed run left\n", encoding="utf-8")
        _mod.update("collection", {"counts": {"x": 1}}, path,
                    derived_from={"data/collected/repos.csv": {"rows": 1}})
        self.assertFalse(stray.exists(), "the leftover temporary file was not removed")
        self.assertEqual(sorted(p.name for p in tmp.iterdir()),
                         ["snapshot.json", "snapshot.json.lock"])








if __name__ == "__main__":
    unittest.main()
