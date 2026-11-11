"""
Tests for the container: entrypoint.py, the Dockerfile, compose.yaml, the build script's
context and the bundle.

Strategy:
  - The entrypoint's decisions are functions with their inputs injected, and each runs
    without a container. The mount check runs with a function that returns device numbers,
    the flock probe over a real directory, and the lock and the pipeline execution state over
    a temporary data root. `main` runs over a project folder in a temporary directory with the
    runner stubbed.
  - The tests check against the repository that the data root contains no code and that the
    heuristics are read beside the package.
  - The tests read the Dockerfile, the compose file and the two scripts as text and check them
    against the decisions that the runbook describes. The base image is named by digest, and
    the data root is mounted at its path in the repository. The image is the one that the
    build script tags, and a stop gives the steps time to end.
  - The build script assembles the build context into a temporary directory. The tests check
    that the context contains the code of the five stages and the heuristics, and neither the
    tests nor the data. No test builds an image. The container-image workflow builds it on a
    GitHub Actions runner with Docker. The build script's tarball mode runs in full with a stub
    `docker` on PATH. The stub returns an OCI-layout tarball for the build and the output of an
    image for the zone check, and the script then runs the rewrite into the classic layout,
    the tag check and the zone check.
  - install_tools.sh, which both Dockerfiles run, runs with a mise.toml that the test writes
    and with a `python3` first on PATH that runs this interpreter and stops at the download of
    mise, so that no test reaches the network.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_container -v   (from deploy/)
"""

import contextlib
import csv
import errno
import fcntl
import hashlib
import importlib.util
import io
import itertools
import json
import os
import platform
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DATA_ROOT = ROOT / "data"
STAGE_DIR = DATA_ROOT / "collection"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


entrypoint = _load("entrypoint", HERE / "entrypoint.py")


def _tracked(*paths: str) -> list:
    result = subprocess.run(["git", "ls-files", "--", *paths], capture_output=True,
                            text=True, cwd=str(ROOT))
    if result.returncode != 0:
        return None
    return [line for line in result.stdout.splitlines() if line]


def _mount_on_devices(case: unittest.TestCase, app: Path) -> None:
    """Run the real mount check over the device numbers in `case.devices`.

    Because every directory of a temporary tree is on one device, the helper gives each mount
    a device number of its own, and a test unmounts a mount by giving it the root's number. The
    check runs with the arguments that its caller passes, so that a test detects a caller that
    leaves a mount out.
    """
    case.devices = {app: 1}
    case.devices.update({app / name: 2 + i for i, name in enumerate(entrypoint.MOUNTS)})

    def device(path):
        try:
            return case.devices[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    real = entrypoint.unmounted

    def unmounted(app_root, names=entrypoint.MOUNTS, dev_of=None):
        return real(app_root, names, dev_of=device)

    patcher = mock.patch.object(entrypoint, "unmounted", unmounted)
    patcher.start()
    case.addCleanup(patcher.stop)


# Europe/Berlin as a POSIX TZ string: CET (UTC+1), and CEST (UTC+2) from the last Sunday of March
# to the last Sunday of October.
BERLIN = "CET-1CEST,M3.5.0,M10.5.0/3"


def _zone_file(path: Path, rule: str, offset_seconds: int, name: str) -> Path:
    """Write a time zone file in version 2 of the TZif format at `path`, with no transitions and
    the POSIX TZ string `rule` as its last line. Without transitions, `rule` applies at every
    time, and no test depends on the zone database of the machine that runs it. `offset_seconds`
    and `name` give the zone's standard time, which the header names."""
    names = name.encode("ascii") + b"\0"

    def block() -> bytes:
        counts = struct.pack(">6l", 0, 0, 0, 0, 1, len(names))
        return b"TZif2" + b"\0" * 15 + counts + struct.pack(">lbB", offset_seconds, 0, 0) + names
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(block() + block() + b"\n" + rule.encode("ascii") + b"\n")
    return path


def _berlin_file(path: Path) -> Path:
    """The zone file of Europe/Berlin at `path`."""
    return _zone_file(path, BERLIN, 3600, "CET")


def _use_host_zone(case: unittest.TestCase, path: Path) -> None:
    """Read the NAS's time zone from `path` until the test ends, as the entrypoint reads the
    file that compose.yaml mounts at `HOST_LOCALTIME`."""
    patcher = mock.patch.object(entrypoint, "HOST_LOCALTIME", path)
    patcher.start()
    case.addCleanup(patcher.stop)


def _compose_revision() -> str:
    """The value that the shipped compose.yaml gives the container's revision variable, read from
    the `environment:` mapping of the service."""
    found = re.search(rf"(?m)^\s+{entrypoint.COMPOSE_REVISION_VARIABLE}: \"([^\"]*)\"$",
                      (HERE / "compose.yaml").read_text(encoding="utf-8"))
    return found.group(1) if found else ""


def _use_zone(case: unittest.TestCase, zone: str) -> None:
    """Set the local time zone of this process to `zone` until the test ends, so that a test
    can show that the entrypoint's decision does not depend on the process's time zone."""
    saved = os.environ.get("TZ")

    def restore():
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()
    case.addCleanup(restore)
    os.environ["TZ"] = zone
    time.tzset()


def _record_revision(data: Path, revision) -> None:
    """Write the record of the data root's revision under the data root `data`, as the first
    start of an image of that revision writes it."""
    (data / "run_logs").mkdir(parents=True, exist_ok=True)
    (data / entrypoint.DATA_ROOT_RECORD).write_text(
        json.dumps({"revision": revision, "image": "commit 1234567"}) + "\n", encoding="utf-8")


@contextlib.contextmanager
def _held_run_lock(app: Path, holder: str):
    """Hold a flock on the runner's lock file under `app`, as a running runner does."""
    path = app / entrypoint.DATA / entrypoint.RUN_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(holder + "\n", encoding="utf-8")
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield path
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# ---------------------------------------------------------------------------
# The mount check
# ---------------------------------------------------------------------------

class TestTheMountCheck(unittest.TestCase):

    def test_a_directory_on_the_root_device_is_reported(self):
        devices = {Path("/app"): 1, Path("/app/data"): 1, Path("/app/handover"): 2}
        loose = entrypoint.unmounted(Path("/app"), dev_of=lambda p: devices[p])
        self.assertEqual(loose, ["data"])

    def test_every_directory_on_another_device_passes(self):
        devices = {Path("/app"): 1, Path("/app/data"): 2, Path("/app/handover"): 5}
        self.assertEqual(entrypoint.unmounted(Path("/app"), dev_of=lambda p: devices[p]), [])

    def test_a_directory_that_does_not_exist_is_reported(self):
        def dev_of(path):
            if path == Path("/app"):
                return 1
            raise FileNotFoundError(path)
        self.assertEqual(entrypoint.unmounted(Path("/app"), dev_of=dev_of),
                         list(entrypoint.MOUNTS))

    def test_the_mounts_are_the_data_root_and_the_handover(self):
        # The data root is mounted at the path that the repository gives it, so that a path in
        # a log names the same file on the host. The handover is the second mount.
        self.assertEqual(entrypoint.MOUNTS, ("data", "handover"))
        self.assertTrue((ROOT / entrypoint.DATA).is_dir())
        self.assertEqual(entrypoint.STAGE, "data/collection")


class TestTheDataIsMountedNotBuiltIn(unittest.TestCase):
    """An empty data root is a first start, and the entrypoint copies no data from the image.

    Because the image runs every stage, the first start writes the whole data root, beginning
    with the download of the GHS retrieval. An input can be replaced without a rebuild.
    """

    def test_an_empty_data_root_is_a_first_start_and_no_problem(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        app = tmp / "app"
        for name in entrypoint.MOUNTS:
            (app / name).mkdir(parents=True)
        with mock.patch.object(entrypoint, "unmounted", lambda *a, **k: []):
            self.assertEqual(entrypoint.startup_problems(app), [])

    def test_the_entrypoint_seeds_no_input_from_the_image(self):
        """No `seed` function, and no code that reads the image's stage directory.

        The two tests below check that decision by calling the one function that copies from
        the image.
        """
        source = (HERE / "entrypoint.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"def seed\b")
        self.assertNotIn("image_stage", source)

    def test_the_only_thing_taken_from_the_image_is_the_heuristics_record(self):
        """The heuristics files are copied into run_logs/, and nothing else in the data root
        changes.

        The copies make a handover record its own heuristics. output/provenance.json records a
        checksum of only one of the heuristics files, and without the copies the handover cannot
        show which commit patterns or which negatives the collection used.
        """
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        image, state = tmp / "image", tmp / "state"
        (image / "heuristics").mkdir(parents=True)
        for name in ("config_path_patterns.csv", "ai_commit_patterns.csv"):
            (image / "heuristics" / name).write_text("x\n", encoding="utf-8")
        (image / "heuristics" / "a_directory").mkdir()
        (state / "data" / "collected").mkdir(parents=True)
        with mock.patch.object(entrypoint, "IMAGE_ROOT", image):
            self.assertEqual(entrypoint.record_heuristics(state), 2)
        self.assertEqual(
            sorted(f.relative_to(state).as_posix() for f in state.rglob("*") if f.is_file()),
            ["run_logs/heuristics/ai_commit_patterns.csv",
             "run_logs/heuristics/config_path_patterns.csv"])
        self.assertEqual(list((state / "data" / "collected").iterdir()), [],
                         "the entrypoint wrote into the mounted data")

    def test_the_copies_are_the_heuristics_files_of_this_image_and_no_other(self):
        """The copies of an earlier image stay in the data root until a start replaces them. A
        file that this image lacks would be in the handover as if the run had read it, and so
        would a file that this image has with other contents."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        image, state = tmp / "image", tmp / "state"
        (image / "heuristics").mkdir(parents=True)
        (image / "heuristics" / "config_path_patterns.csv").write_text("this image\n")
        copies = state / entrypoint.HEURISTICS_RECORD
        (copies / "a_directory").mkdir(parents=True)
        for name in ("config_path_patterns.csv", "dropped_patterns.csv"):
            (copies / name).write_text("an earlier image\n", encoding="utf-8")
        with mock.patch.object(entrypoint, "IMAGE_ROOT", image):
            self.assertEqual(entrypoint.record_heuristics(state), 1)
        self.assertEqual(sorted(path.name for path in copies.iterdir()),
                         ["config_path_patterns.csv"])
        self.assertEqual((copies / "config_path_patterns.csv").read_text(), "this image\n")
        with mock.patch.object(entrypoint, "IMAGE_ROOT", tmp / "absent"):
            self.assertEqual(entrypoint.record_heuristics(state), 0)
        self.assertFalse(copies.exists())

    def test_an_image_without_heuristics_is_recorded_as_none_not_refused(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        with mock.patch.object(entrypoint, "IMAGE_ROOT", tmp / "absent"):
            self.assertEqual(entrypoint.record_heuristics(tmp / "state"), 0)
        self.assertFalse((tmp / "state").exists())


class TestTheStartupChecks(unittest.TestCase):
    """Every check that a start makes, through the one function that the tests call, so that
    deleting the call of a check fails a test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "app"
        for name in entrypoint.MOUNTS:
            (self.app / name).mkdir(parents=True)
        _mount_on_devices(self, self.app)

    def test_a_sound_start_has_no_problem(self):
        self.assertEqual(entrypoint.startup_problems(self.app), [])

    def test_a_directory_that_is_not_a_mount_is_the_only_thing_reported(self):
        """An unmounted directory is reported alone, because every check after the mount check
        would run in the container's own layer and test the wrong filesystem. Each mount in turn
        gets the root's device, and a call that leaves one mount out of the check fails."""
        for name in entrypoint.MOUNTS:
            with self.subTest(mount=name):
                mounted = self.devices[self.app / name]
                self.devices[self.app / name] = self.devices[self.app]
                problems = entrypoint.startup_problems(self.app)
                self.devices[self.app / name] = mounted
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(f"/app/{name} is not mounted from the "
                                                       f"host."), problems[0])

    def test_a_read_only_mount_is_reported(self):
        handover = self.app / "handover"
        handover.chmod(0o555)
        self.addCleanup(handover.chmod, 0o755)
        try:
            (handover / ".probe").write_text("")
        except OSError:
            pass
        else:
            (handover / ".probe").unlink()
            self.skipTest("this filesystem or user ignores the mode")
        problems = entrypoint.startup_problems(self.app)
        self.assertIn("/app/handover is mounted read-only. Mount /app/data and /app/handover "
                      "read-write.", problems)

    def test_a_read_only_data_mount_is_the_last_word(self):
        """The three checks after the read-only check write into the data mount, and over a
        read-only mount they would fail because of the mount. `flock_works`, for example, would
        report a filesystem that does not implement flock, with a remedy that does not help."""
        data = self.app / entrypoint.DATA
        data.chmod(0o555)
        self.addCleanup(data.chmod, 0o755)
        try:
            (data / ".probe").write_text("")
        except OSError:
            pass
        else:
            (data / ".probe").unlink()
            self.skipTest("this filesystem or user ignores the mode")
        problems = entrypoint.startup_problems(self.app)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("/app/data is mounted read-only", problems[0])

    def test_a_run_logs_problem_is_reported(self):
        with mock.patch.object(entrypoint, "run_logs_problem", lambda state: "run_logs is a file"):
            problems = entrypoint.startup_problems(self.app)
        self.assertIn("run_logs is a file", problems)

    def test_a_held_lock_is_reported(self):
        """The test holds a real flock on the runner's lock file, and a call that probes a lock
        path that the runner never takes fails."""
        with _held_run_lock(self.app, "pid 7 on nas"):
            problems = entrypoint.startup_problems(self.app)
        self.assertIn(f"Another run holds /app/{entrypoint.DATA}/{entrypoint.RUN_LOCK} "
                      f"(pid 7 on nas). Nothing was written", "\n".join(problems))

    def test_a_filesystem_without_flock_is_reported(self):
        with mock.patch.object(entrypoint, "flock_works", lambda state: False):
            problems = entrypoint.startup_problems(self.app)
        self.assertTrue(any("does not implement flock" in p for p in problems), problems)

    def test_a_state_that_cannot_be_read_is_reported_with_its_recovery(self):
        """A state file that a power loss truncated, or that is not a JSON object, is not a
        missing state. A start that read it as missing would decide as if no run had happened.
        The message names the file and how to recover it, and the start leaves the file as it
        is."""
        state = self.app / entrypoint.DATA / entrypoint.STATE_FILE
        _record_revision(self.app / entrypoint.DATA, entrypoint.DATA_ROOT_REVISION)
        for text, cause in (('{"completed": {"repository_cloning"', "cannot be read ("),
                            ("", "cannot be read ("), ("[]", "is not a JSON object")):
            with self.subTest(text=text):
                state.write_text(text, encoding="utf-8")
                problems = entrypoint.startup_problems(self.app)
                self.assertEqual(len(problems), 1, problems)
                self.assertTrue(problems[0].startswith(
                    f"The pipeline execution state /app/{entrypoint.DATA}/{entrypoint.STATE_FILE} "
                    f"{cause}"), problems[0])
                self.assertIn(f"Keep a copy of the file, and replace it with the "
                              f"{entrypoint.DATA}/{entrypoint.STATE_FILE} of the latest archive in "
                              f"the handover directory", problems[0])
                self.assertEqual(state.read_text(encoding="utf-8"), text)
        state.write_text('{"outcome": "success"}', encoding="utf-8")
        self.assertEqual(entrypoint.startup_problems(self.app), [])


class TestTheMountsAreWritable(unittest.TestCase):
    """`unmounted` checks only whether the host's filesystem is mounted. A mount can also be
    read-only. The entrypoint writes `handover/` once, at the end, and a read-only mount there
    would pass every other start check and lose the handover of a finished run."""

    def test_a_read_only_mount_is_reported(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in entrypoint.MOUNTS:
            (tmp / name).mkdir(parents=True)
        self.assertEqual(entrypoint.unwritable(tmp), [])
        (tmp / "handover").chmod(0o555)
        self.addCleanup((tmp / "handover").chmod, 0o755)
        self.assertEqual(entrypoint.unwritable(tmp), ["handover"])

    def test_the_probe_file_is_not_left_behind(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in entrypoint.MOUNTS:
            (tmp / name).mkdir(parents=True)
        entrypoint.unwritable(tmp)
        for name in entrypoint.MOUNTS:
            self.assertEqual(os.listdir(tmp / name), [], name)


class TestTheFlockProbe(unittest.TestCase):

    def test_a_local_directory_implements_flock_and_the_probe_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(entrypoint.flock_works(Path(tmp)))
            self.assertEqual(os.listdir(tmp), [])

    def test_a_directory_that_cannot_be_written_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent"
            self.assertFalse(entrypoint.flock_works(missing))

    def test_a_filesystem_that_refuses_flock_does_not(self):
        """The probe exists for a writable directory on which `flock` fails, as on a network
        mount. The test above covers the failure of `os.open` instead."""
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(entrypoint.fcntl, "flock",
                                   side_effect=OSError(45, "Operation not supported")):
                self.assertFalse(entrypoint.flock_works(Path(tmp)))
            self.assertEqual(os.listdir(tmp), [], "the probe file was left behind")


# ---------------------------------------------------------------------------
# State and code
# ---------------------------------------------------------------------------

class TestTheCodeIsInTheImage(unittest.TestCase):
    """The data root contains no code, and the heuristics are read beside the package."""

    def test_the_heuristics_is_read_beside_the_package_and_not_from_the_volume(self):
        # The package reads the heuristics from the image, and a heuristic that changed in the
        # image takes effect without a refresh of the volume.
        heuristics = importlib.import_module("agentcontext.heuristics")
        package = importlib.import_module("agentcontext")
        self.assertEqual(heuristics.HEURISTICS_DIR, package.REPO_ROOT / "heuristics")
        for path in (heuristics.CONFIG_PATH_PATTERNS_PATH, heuristics.AI_COMMIT_PATTERNS_PATH,
                     heuristics.VENDORED_DIRS_PATH, heuristics.DOC_HASHES_PATH):
            with self.subTest(path=path.name):
                self.assertEqual(path.parent, heuristics.HEURISTICS_DIR)
                self.assertTrue(path.is_file())

    def test_the_data_root_contains_no_code(self):
        # The code is in the image. A script in the data root would run from the volume's
        # copy, which nothing refreshes.
        tracked = _tracked("data")
        if tracked is None:
            self.skipTest("not inside a git checkout")
        self.assertTrue(tracked)
        self.assertEqual([p for p in tracked if p.endswith(".py")], [])


class TestTheLockAndTheRecord(unittest.TestCase):
    """The runner's lock and the directory of the pipeline execution state under a data root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        self.data.mkdir()

    def test_the_run_lock_is_probed_before_anything_is_written(self):
        """The lock is read before anything is written, so that a second container started
        against a project folder in use writes nothing while the other run holds the lock."""
        lock = self.data / "run_logs" / "run.lock"
        self.assertIsNone(entrypoint.lock_holder(self.data), "no lock file, no holder")
        lock.parent.mkdir()
        lock.write_text("pid 8 on live-container since 2026-09-14T00:00:00Z")
        self.assertIsNone(entrypoint.lock_holder(self.data), "a lock nobody holds")
        fd = os.open(lock, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(entrypoint.lock_holder(self.data),
                             "pid 8 on live-container since 2026-09-14T00:00:00Z")
        finally:
            os.close(fd)
        self.assertIsNone(entrypoint.lock_holder(self.data), "released with the descriptor")

    def test_a_run_logs_that_is_not_a_directory_is_named(self):
        self.assertIsNone(entrypoint.run_logs_problem(self.data))
        (self.data / "run_logs").write_text("not a directory")
        self.assertIn("run_logs is not a directory", entrypoint.run_logs_problem(self.data))
        (self.data / "run_logs").unlink()
        (self.data / "run_logs").mkdir()
        self.assertIsNone(entrypoint.run_logs_problem(self.data))

    def test_the_entrypoint_has_no_refresh(self):
        """A start leaves the mounts as the last run left them, because the code and the
        heuristics are read from the image and the data is the host's."""
        source = (HERE / "entrypoint.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"def refresh|refresh_(code|heuristics)|HEURISTICS_FILES")


class TestTheCredentialsFile(unittest.TestCase):
    """The entrypoint names the mounted credentials.csv to the run and logs whether it exists.
    The package reads the file, and tests/test_credentials.py tests the reading."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "credentials.csv"

    def test_the_file_is_named_to_the_run(self):
        self.path.write_text("service,account,credential\n")
        env = {}
        said = entrypoint.name_credentials(env, self.path)
        self.assertEqual(env, {"AGENTCONTEXT_CREDENTIALS": str(self.path)})
        self.assertIn(f"Found the credentials file {self.path}. The pre-checks "
                      f"verify its GitHub access tokens and its OpenRouter API key.", said)

    def test_a_file_the_environment_names_wins(self):
        env = {"AGENTCONTEXT_CREDENTIALS": str(self.tmp / "elsewhere.csv")}
        said = entrypoint.name_credentials(env, self.path)
        self.assertEqual(env["AGENTCONTEXT_CREDENTIALS"], str(self.tmp / "elsewhere.csv"))
        self.assertIn("No credentials file at", said)

    def test_a_directory_is_named_as_what_docker_made_of_a_missing_file(self):
        self.path.mkdir()
        said = entrypoint.name_credentials({}, self.path)
        self.assertTrue(said.startswith("WARNING "))
        self.assertIn("is a directory, not a file. Docker created it", said)

    def test_the_file_is_read_where_compose_mounts_it(self):
        self.assertEqual(entrypoint.CREDENTIALS_FILE, Path("/app/credentials.csv"))


class TestTheHandover(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "app"
        data = self.app / "data"
        for relative, text in (("collection/output/repos.csv", "CSV\n"),
                               ("run_logs/state.json", "{}\n"),
                               ("collection/data/collected/o§r/CLAUDE.md", "hello\n"),
                               ("collection/_scratch/clone/big.bin", "x" * 1000),
                               ("collection/__pycache__/x.pyc", "bytes\n"),
                               ("collection/data/collected/o§r/_scratch_like/keep.md", "kept\n"),
                               ("classification/labels/index.csv", "run_id\n"),
                               ("classification/labels/2026-10-12T000000Z.partial.jsonl", "{}\n"),
                               ("sampling/ghs_retrievals/2026-10-12T000000Z.json.gz.part", "x" * 1000),
                               ("sampling/ghs_filtered.json", "{}\n")):
            path = data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.handover = self.app / "handover"
        self.handover.mkdir()
        # No Stop has arrived when a test begins. `main` clears the recorded signal at every
        # start, and these tests call the functions that `main` calls rather than `main`.
        stop = mock.patch.object(entrypoint, "_stop_signal", 0)
        stop.start()
        self.addCleanup(stop.stop)

    def _listing(self, archive: Path) -> list:
        out = subprocess.run([entrypoint.SEVEN_ZIP, "l", "-slt", str(archive)], check=True,
                             capture_output=True, text=True).stdout
        return sorted(line.split("=", 1)[1].strip() for line in out.splitlines()
                      if line.startswith("Path = ") and line != f"Path = {archive}")

    def test_the_program_of_7_zip_is_the_packages(self):
        """The entrypoint repeats the name of the program rather than importing it, and this test
        checks the copy against the original."""
        import agentcontext
        self.assertEqual(entrypoint.SEVEN_ZIP, agentcontext.SEVEN_ZIP)

    @unittest.skipIf(shutil.which("7zz") is None, "7zz not on PATH")
    def test_the_handover_is_packed_with_the_program_of_the_7_zip_release(self):
        """The 7-Zip release that mise.toml pins provides `7zz` and no `7z`, and the image
        installs no other 7-Zip. With only that program on PATH, the handover is packed."""
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "7zz").symlink_to(shutil.which("7zz"))
        with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
            archive = entrypoint.pack_handover(self.app, self.handover, "complete",
                                               "20260913T000000Z")
        self.assertIn("data/collection/output/repos.csv", self._listing(archive))

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_the_archive_contains_the_data_root_without_the_scratch(self):
        """Every stage's data, under the path that it has in a checkout."""
        archive = entrypoint.pack_handover(self.app, self.handover, "complete", "20260913T000000Z")
        self.assertEqual(archive.name, "agentcontext-handover-20260913T000000Z-complete.7z")
        listing = self._listing(archive)
        for path in ("data/collection/output/repos.csv", "data/run_logs/state.json",
                     "data/collection/data/collected/o§r/CLAUDE.md",
                     "data/collection/data/collected/o§r/_scratch_like/keep.md",
                     "data/classification/labels/index.csv", "data/sampling/ghs_filtered.json"):
            self.assertIn(path, listing)
        self.assertFalse([p for p in listing if "/_scratch/" in p or p.endswith("/_scratch")],
                         "the scratch is in the handover")
        self.assertFalse([p for p in listing if "__pycache__" in p])

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_a_download_that_a_stop_cut_short_is_left_out(self):
        """A partial GHS download is left out, because the next Start downloads it again. The
        partial file of a classification run, which the next Start resumes, stays."""
        for outcome in ("stopped", "complete"):
            with self.subTest(outcome=outcome):
                archive = entrypoint.pack_handover(self.app, self.handover, outcome,
                                                   "20261012T000000Z", everything=outcome == "complete")
                listing = self._listing(archive)
                self.assertFalse([p for p in listing if p.endswith(".part")], listing)
                self.assertIn("data/classification/labels/2026-10-12T000000Z.partial.jsonl", listing)

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_a_file_of_the_sample_with_a_part_name_is_packed(self):
        """Only the GHS download that a stop interrupted is left out by its `.part` name. A
        repository can contain a configuration artifact with any name."""
        sample = self.app / "data" / "collection" / "data" / "collected" / "o§r" / "x.part"
        sample.write_text("collected\n")
        member = "data/collection/data/collected/o§r/x.part"
        archive = entrypoint.pack_handover(self.app, self.handover, "complete", "20261012T000001Z")
        listing = self._listing(archive)
        self.assertIn(member, listing)
        self.assertNotIn("data/sampling/ghs_retrievals/2026-10-12T000000Z.json.gz.part", listing)
        with mock.patch.object(entrypoint, "log"):
            trees = entrypoint.pack_trees(self.app, self.handover, "failed", "20261012T000002Z")
        self.assertIn(member, self._listing(trees))

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_symlinks_are_stored_as_links_and_a_dangling_one_does_not_fail_the_pack(self):
        # The collector writes symlink markers on purpose, and some of them dangle. Without
        # -snl, 7zz stores the target's bytes and fails with exit 1 on a dangling link.
        repo = self.app / "data" / "collection" / "data" / "collected" / "o§r"
        os.symlink("CLAUDE.md", repo / "good")
        os.symlink("nowhere", repo / "dangling")
        archive = entrypoint.pack_handover(self.app, self.handover, "complete", "20260913T000002Z")
        out = self.tmp / "out"
        subprocess.run([entrypoint.SEVEN_ZIP, "x", "-y", f"-o{out}", str(archive)], check=True,
                       capture_output=True)
        good = out / "data" / "collection" / "data" / "collected" / "o§r" / "good"
        self.assertTrue(good.is_symlink(), "the link came back as a file")
        self.assertEqual(os.readlink(good), "CLAUDE.md")
        self.assertTrue((good.parent / "dangling").is_symlink())
        self.assertFalse(list(self.handover.glob("*.partial")))

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_a_run_that_did_not_complete_packs_the_record_and_the_lists_but_not_the_sample(self):
        stage = self.app / "data" / "collection"
        (stage / "finished_repositories.jsonl").write_text('{"repo": "o/r"}\n')
        # The record of the collection in progress. Without it, a resumed collection queries the
        # head of every repository that it already collected. The record of the last collection
        # that ended shows which collection ended over which list.
        (stage / "finished_repositories.collection.json").write_text('{"collection": "c"}\n')
        (stage / "finished_repositories.ended-collection.json").write_text(
            '{"collection": "b"}\n')
        (stage / "data" / "collected" / "repos.csv").write_text("repo_name\n")
        archive = entrypoint.pack_handover(self.app, self.handover, "stopped", "20260913T000003Z",
                                           everything=False)
        listing = self._listing(archive)
        for path in ("data/run_logs/state.json", "data/collection/finished_repositories.jsonl",
                     "data/collection/finished_repositories.collection.json",
                     "data/collection/finished_repositories.ended-collection.json",
                     "data/collection/data/collected/repos.csv",
                     "data/collection/output/repos.csv", "data/classification/labels/index.csv",
                     "data/sampling/ghs_filtered.json"):
            self.assertIn(path, listing)
        self.assertFalse([p for p in listing if "o§r" in p], "the sample was packed on a stop")

    def test_the_partial_archives_earlier_kills_left_are_removed_at_the_start(self):
        """A kill during packing leaves the archive under its `.partial` name, and no reader takes
        that file for a handover. The start removes it, together with what killed processes left
        in the data root, and keeps every finished archive."""
        stale = self.handover / "agentcontext-handover-20260913T000000Z-complete.7z.partial"
        stale.write_bytes(b"7z" * 10)
        # A kill while the checksum of a finished archive was written.
        stale_checksum = (self.handover
                          / "agentcontext-handover-20260911T000000Z-complete.7z.sha256.partial")
        stale_checksum.write_text("0" * 64)
        kept = self.handover / "agentcontext-handover-20260912T000000Z-complete.7z"
        kept.write_bytes(b"7z")
        kept_checksum = self.handover / f"{kept.name}.sha256"
        kept_checksum.write_text(f"{'0' * 64}  {kept.name}\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            entrypoint.remove_leftovers(self.app)
        self.assertEqual(sorted(p.name for p in self.handover.iterdir()),
                         [kept.name, kept_checksum.name])
        said = err.getvalue()
        self.assertIn("Removed what killed processes left (", said)
        self.assertIn(f"{entrypoint.HANDOVER}/{stale.name}", said)
        self.assertIn(f"{entrypoint.HANDOVER}/{stale_checksum.name}", said)
        self.assertNotIn(kept.name, said)

    def test_a_scratch_directory_that_cannot_be_listed_is_named_in_a_sentence(self):
        """A scratch directory that the start cannot list is kept, and the warning that names it
        ends with a period, as every log message does."""
        scratch = self.app / entrypoint.SCRATCH
        scratch.mkdir(parents=True, exist_ok=True)
        refused = PermissionError(13, "Permission denied")
        with mock.patch.object(Path, "iterdir", side_effect=refused), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            entrypoint.remove_leftovers(self.app)
        [warning] = [line for line in err.getvalue().splitlines() if "Could not list" in line]
        self.assertIn(f"Could not list {entrypoint.SCRATCH} for what killed processes left",
                      warning)
        self.assertTrue(warning.endswith("."), warning)

    def test_a_pack_that_fails_leaves_no_partial_and_raises(self):
        """The stub writes the `.partial` file before it fails, as 7zz does. The empty directory
        then shows that the failure path removes the file."""
        def fail(args, cwd, **options):
            Path(args[args.index("-snl") + 1]).write_bytes(b"7z" * 10)
            return 2, "Error: no space"
        with mock.patch.object(entrypoint, "_run_7z", side_effect=fail):
            with self.assertRaises(RuntimeError):
                entrypoint.pack_handover(self.app, self.handover, "complete", "20260913T000004Z")
        self.assertEqual(sorted(p.name for p in self.handover.iterdir()), [])

    def test_seven_zip_over_the_data_root_does_not_start_after_a_stop(self):
        """A start checks for a Stop before it decides to pack the data root. A Stop that arrives
        after that check and before 7zz's handler is installed would leave 7zz packing the whole
        data root until Docker kills it at the end of the grace period. 7zz over the lists still
        starts, because a start packs the lists after a Stop."""
        started = []

        class SevenZip:
            """A 7zz that writes the archive that it is given and exits 0."""
            returncode = 0

            def __init__(self, command, **options):
                started.append(command)
                Path(command[command.index("-snl") + 1]).write_bytes(b"7z")

            def communicate(self):
                return "", None

            def kill(self):
                pass
        entrypoint.note_stop(signal.SIGTERM)
        with mock.patch.object(entrypoint.subprocess, "Popen", SevenZip):
            with self.assertRaises(entrypoint.PackingStopped):
                entrypoint.pack_handover(self.app, self.handover, "complete", "20261001T000000Z")
            self.assertEqual(started, [])
            self.assertEqual(list(self.handover.iterdir()), [])
            archive = entrypoint.pack_handover(self.app, self.handover, "complete-without-sample",
                                               "20261001T000001Z", everything=False)
        self.assertEqual(len(started), 1)
        self.assertTrue(archive.is_file())

    def test_a_7z_warning_cannot_publish_a_potentially_incomplete_handover(self):
        def fake(args, cwd, **options):
            Path(args[args.index("-snl") + 1]).write_bytes(b"7z")
            return 1, "WARNING: collect_commits/repo.log.xz could not be read\n" + "Summary\n" * 100
        with mock.patch.object(entrypoint, "_run_7z", side_effect=fake), \
                mock.patch.object(entrypoint, "log_error") as logged, \
                self.assertRaisesRegex(RuntimeError, "7zz exited 1"):
            entrypoint.pack_handover(self.app, self.handover, "complete", "20260913T000005Z")
        self.assertEqual(list(self.handover.iterdir()), [])
        self.assertTrue(any("repo.log.xz could not be read" in call.args[0]
                            for call in logged.call_args_list))

    def test_the_names_of_the_directories_that_dsm_writes_are_the_packages(self):
        from agentcontext import paths
        self.assertEqual(set(entrypoint.DISCARDED_DIRECTORIES), paths.DISCARDED_DIRECTORIES)

    def test_every_packing_tells_7z_to_leave_out_the_directories_that_dsm_writes(self):
        """DSM writes and changes `@eaDir` directories in a shared folder at any time. One that
        changes while 7zz reads it makes 7zz warn, and the warning fails the packing."""
        from agentcontext import paths
        commands = []

        def run_7z(args, cwd, **options):
            commands.append(args)
            Path(args[args.index("-snl") + 1]).write_bytes(b"7z")
            return 0, ""
        with mock.patch.object(entrypoint, "_run_7z", run_7z), \
                mock.patch.object(entrypoint, "log"):
            entrypoint.pack_handover(self.app, self.handover, "complete", "20261001T000000Z")
            entrypoint.pack_handover(self.app, self.handover, "failed", "20261001T000001Z",
                                     everything=False)
            entrypoint.pack_trees(self.app, self.handover, "failed", "20261001T000001Z")
        self.assertEqual(len(commands), 3)
        for args in commands:
            for name in sorted(paths.DISCARDED_DIRECTORIES):
                with self.subTest(archive=Path(args[args.index("-snl") + 1]).name, name=name):
                    self.assertIn(f"-xr!{name}/", args)

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_the_directories_that_dsm_writes_are_in_no_archive(self):
        """The directories are left out at any depth: in the data root, in the lists and in the
        trees. A file named `@eaDir` is not such a directory, and the collected tree keeps it as
        any other file."""
        data = self.app / "data"
        for relative in ("collection/data/collected/o§r/@eaDir/CLAUDE.md@SynoEAStream",
                         "run_logs/@eaDir/state.json@SynoEAStream",
                         "collection/output/@eaDir/repos.csv@SynoEAStream"):
            (data / relative).parent.mkdir(parents=True)
            (data / relative).write_text("written by DSM\n")
        kept = "data/collection/data/collected/o§s/@eaDir"
        (self.app / kept).parent.mkdir(parents=True)
        (self.app / kept).write_text("a file of the sample\n")
        with mock.patch.object(entrypoint, "log"):
            archives = {"complete": entrypoint.pack_handover(self.app, self.handover, "complete",
                                                             "20261001T000000Z"),
                        "failed": entrypoint.pack_handover(self.app, self.handover, "failed",
                                                           "20261001T000001Z", everything=False),
                        "trees": entrypoint.pack_trees(self.app, self.handover, "failed",
                                                       "20261001T000001Z")}
        for kind, archive in archives.items():
            with self.subTest(archive=kind):
                listing = self._listing(archive)
                self.assertFalse([p for p in listing if "@eaDir/" in p], listing)
                self.assertTrue([p for p in listing if p.startswith("data/collection/")])
                if kind != "failed":
                    self.assertIn(kept, listing)

    @unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
    def test_the_sample_list_rides_along_with_the_classification_stages_data(self):
        (self.app / "data" / "classification" / "repos_annotated_included.csv").write_text(
            "repo_name\n")
        archive = entrypoint.pack_handover(self.app, self.handover, "failed", "20260913T000001Z")
        self.assertIn("data/classification/repos_annotated_included.csv", self._listing(archive))
        self.assertTrue(archive.name.endswith("-failed.7z"))

    def test_the_outcome_names_a_clean_end_a_signal_and_a_failure(self):
        self.assertEqual(entrypoint.outcome_name(0), "complete")
        self.assertEqual(entrypoint.outcome_name(143), "stopped")
        self.assertEqual(entrypoint.outcome_name(130), "stopped")
        self.assertEqual(entrypoint.outcome_name(-15), "killed",
                         "a signal that the runner did not handle")
        self.assertEqual(entrypoint.outcome_name(-9), "killed")
        self.assertEqual(entrypoint.outcome_name(1), "failed")
        self.assertEqual(entrypoint.outcome_name(3), "failed")

    def test_only_a_completed_run_packs_the_sample(self):
        # Packing the whole sample after the cloning step's exit on low disk space would write
        # an archive of it onto the volume that the step has just refused to clone into.
        self.assertTrue(entrypoint.pack_everything("complete"))
        for outcome in ("stopped", "failed", "killed"):
            self.assertFalse(entrypoint.pack_everything(outcome), outcome)

    def test_a_refused_start_is_not_handed_over_whether_or_not_it_wrote_its_record(self):
        state = self.tmp / "state"
        (state / "run_logs").mkdir(parents=True)
        record = state / "run_logs" / "state.json"
        record.write_text('{"outcome": "success", "completed": {}}')
        before = entrypoint.state_signature(state)
        # A refusal by the lock writes nothing.
        self.assertFalse(entrypoint.handover_wanted(1, before, state))
        # A refusal by the pre-checks or by the check for modified inputs is recorded, and
        # no step ran.
        for outcome in entrypoint.NO_STEP_OUTCOMES:
            record.write_text('{"outcome": "%s", "completed": {}}' % outcome)
            self.assertFalse(entrypoint.handover_wanted(1, before, state), outcome)
        # A step ran and failed or was stopped, or the runner was killed after a step started.
        for outcome, code in (("failed", 1), ("interrupted", 143), ("running", -9)):
            record.write_text('{"outcome": "%s", "completed": {}}' % outcome)
            self.assertTrue(entrypoint.handover_wanted(code, before, state), outcome)
        record.write_text('{"outcome": "success", "completed": {}}')
        self.assertTrue(entrypoint.handover_wanted(0, before, state))
        # A signal is never a refusal, although a runner killed inside the first step of a
        # start leaves the pipeline execution state unchanged, as the lock's refusal does.
        before = entrypoint.state_signature(state)
        self.assertTrue(entrypoint.handover_wanted(-9, before, state))
        # A `--mark-complete` records a step and runs none, and the start packs no handover.
        record.write_text('{"outcome": "marked_complete", "completed": {}}')
        self.assertFalse(entrypoint.handover_wanted(0, before, state))

    def test_the_refusal_outcomes_are_the_runners(self):
        runner = importlib.import_module("agentcontext.runner")
        self.assertEqual(entrypoint.NO_STEP_OUTCOMES, runner.NO_STEP_OUTCOMES)
        source = Path(runner.__file__).read_text()
        for outcome in entrypoint.NO_STEP_OUTCOMES:
            self.assertIn(f'"{outcome}"', source)

    def test_a_signal_death_is_128_plus_the_signal_for_docker(self):
        self.assertEqual(entrypoint.container_exit_code(-15), 143)
        self.assertEqual(entrypoint.container_exit_code(-9), 137)
        self.assertEqual(entrypoint.container_exit_code(4), 4)
        self.assertEqual(entrypoint.container_exit_code(0), 0)

    def test_the_record_signature_shows_whether_the_runner_wrote_anything(self):
        self.assertEqual(entrypoint.state_signature(self.tmp), ())
        (self.tmp / "run_logs").mkdir()
        (self.tmp / "run_logs" / "state.json").write_text("{}")
        first = entrypoint.state_signature(self.tmp)
        self.assertNotEqual(first, ())
        (self.tmp / "run_logs" / "state.json").write_text('{"outcome": "failed"}')
        self.assertNotEqual(entrypoint.state_signature(self.tmp), first)

    def test_a_runner_the_kernel_kills_does_not_leave_its_step_running(self):
        """A runner killed by SIGKILL would leave its step running under the init process
        while the handover is packed. The runner leads a process group, which receives SIGTERM
        and is killed after the drain period."""
        marker = self.tmp / "sleeper.pid"
        script = ("import os, signal, subprocess, sys, time\n"
                  f"child = subprocess.Popen(['sleep', '60'])\n"
                  f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
                  "os.kill(os.getpid(), signal.SIGKILL)\n")
        with mock.patch.object(entrypoint, "log") as logged:
            code = entrypoint.run_runner([sys.executable, "-c", script], self.tmp,
                                         drain_seconds=5)
        self.assertEqual(code, -9)
        sleeper = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(sleeper, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(sleeper, signal.SIGKILL)
            self.fail("the step outlived the runner")
        self.assertIn("The runner was killed by signal 9 while its step was running.",
                      [call.args[0] for call in logged.call_args_list])

    def test_a_group_whose_only_member_waits_to_be_reaped_counts_as_ended(self):
        """macOS refuses a signal to a process group if all its members have exited and wait to
        be reaped, as when the step that killed its runner exits a moment later. Ending such a
        group must not raise, because the Start would then fail while it packs what the run
        left."""
        leader = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = subprocess.run(["ps", "-o", "stat=", "-p", str(leader.pid)],
                                       capture_output=True, text=True).stdout.strip()
                if state.startswith("Z"):
                    break
                time.sleep(0.05)
            else:
                self.fail("the leader did not exit")
            ended = entrypoint.end_process_group(leader.pid, 0.5)
        finally:
            leader.wait()
        if sys.platform == "darwin":
            self.assertEqual(ended, "empty")
        else:
            self.assertIn(ended, ("empty", "stopped", "killed"))

    def test_a_step_that_ignores_the_stop_is_killed_past_the_drain(self):
        """Because the member ignores SIGTERM, `end_process_group` has to kill it after the drain
        period. The tests before and after this one handle SIGTERM and exercise only the drain
        path."""
        marker = self.tmp / "stubborn.pid"
        script = ("import os, signal, subprocess, sys, time\n"
                  "child = subprocess.Popen([sys.executable, '-c',\n"
                  "    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);'\n"
                  "    'time.sleep(60)'])\n"
                  f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
                  "time.sleep(0.3)\n"
                  "os.kill(os.getpid(), signal.SIGKILL)\n")
        with mock.patch.object(entrypoint, "log"):
            code = entrypoint.run_runner([sys.executable, "-c", script], self.tmp,
                                         drain_seconds=1)
        self.assertEqual(code, -9)
        stubborn = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(stubborn, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(stubborn, signal.SIGKILL)
            self.fail("a step that ignores SIGTERM outlived the drain period")

    def test_a_runner_that_exits_by_an_exception_does_not_leave_its_step_running_either(self):
        # An OSError on the step's log makes the runner exit with 1 while the step still runs.
        # The entrypoint ends the group after any exit other than 0.
        marker = self.tmp / "sleeper.pid"
        script = ("import subprocess, sys\n"
                  f"child = subprocess.Popen(['sleep', '60'])\n"
                  f"open({str(marker)!r}, 'w').write(str(child.pid))\n"
                  "sys.exit(1)\n")
        with mock.patch.object(entrypoint, "log") as logged:
            code = entrypoint.run_runner([sys.executable, "-c", script], self.tmp,
                                         drain_seconds=5)
        self.assertEqual(code, 1)
        sleeper = int(marker.read_text())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(sleeper, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(sleeper, signal.SIGKILL)
            self.fail("the step outlived the runner")
        self.assertIn("The runner exited with 1 while its step was running.",
                      [call.args[0] for call in logged.call_args_list])

    def test_a_runner_that_ended_its_step_costs_one_signal_and_no_line(self):
        with mock.patch.object(entrypoint, "log") as logged:
            code = entrypoint.run_runner([sys.executable, "-c", "import sys; sys.exit(3)"],
                                         self.tmp, drain_seconds=5)
        self.assertEqual(code, 3)
        logged.assert_not_called()

    def test_the_runner_is_a_child_whose_exit_code_comes_back(self):
        code = entrypoint.run_runner([sys.executable, "-c", "import sys; sys.exit(7)"], self.tmp)
        self.assertEqual(code, 7)

    def test_a_stop_reaches_the_child(self):
        # A child that exits with its own code on SIGTERM. The signal is sent to this
        # process, and run_runner forwards it.
        script = ("import os, signal, sys, time\n"
                  "signal.signal(signal.SIGTERM, lambda *_: sys.exit(43))\n"
                  "print('ready', flush=True)\n"
                  "os.kill(int(sys.argv[1]), signal.SIGTERM)\n"
                  "time.sleep(30)\n")
        code = entrypoint.run_runner([sys.executable, "-c", script, str(os.getpid())], self.tmp)
        self.assertEqual(code, 43)

    def test_a_stop_before_the_handler_is_installed_reaches_the_child(self):
        """A Stop can arrive after the runner started and before `run_runner` installed the
        handler that passes it on. The handler of the whole start then records the Stop and
        passes nothing on. Docker sends no second signal, and the runner would run until Docker
        kills the container at the end of the grace period. The child sleeps longer than a
        passed-on Stop takes to end it, and only the Stop ends it with a signal."""
        real_popen = subprocess.Popen

        def popen(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            os.kill(os.getpid(), signal.SIGTERM)
            return child
        previous = signal.signal(signal.SIGTERM, entrypoint.note_stop_signal)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        with mock.patch.object(entrypoint.subprocess, "Popen", popen), \
                contextlib.redirect_stderr(io.StringIO()):
            code = entrypoint.run_runner([sys.executable, "-c", "import time; time.sleep(5)"],
                                         self.tmp, drain_seconds=1)
        self.assertEqual(code, -signal.SIGTERM)
        self.assertEqual(entrypoint._stop_signal, signal.SIGTERM)


class TestTheMonthlyRun(unittest.TestCase):
    """A Start after a completed run over a retrieval of an earlier calendar month begins the
    next run. A Start of a monthly schedule takes a new retrieval and collects again rather than
    continuing the completed run."""

    NOW = entrypoint.datetime(2026, 11, 12, tzinfo=entrypoint.timezone.utc)

    def setUp(self):
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)
        # No zone file is at the path where compose.yaml mounts the NAS's zone file, and the
        # months are those of UTC unless a test writes the file.
        self.zone = self.data / "host-localtime"
        _use_host_zone(self, self.zone)

    def _state(self, outcome: str, retrieval: str = "2026-10-12T104432Z",
               last_run: str | None = None, report: dict | None = None) -> None:
        """Write the record of the last GHS download, and the pipeline execution state after a
        start that ended in `outcome` when the last start that ran steps ended in `last_run`."""
        (self.data / "run_logs").mkdir(exist_ok=True)
        (self.data / "run_logs" / "state.json").write_text(json.dumps(
            {"outcome": outcome, "last_run_outcome": last_run or outcome}))
        (self.data / "sampling").mkdir(exist_ok=True)
        (self.data / "sampling" / "ghs_retrieval_report.json").write_text(
            json.dumps(report or {"outcome": "retrieved", "retrieval_id": retrieval}))

    def test_a_completed_run_over_an_old_retrieval_begins_the_next_one(self):
        self._state("success")
        argv, why = entrypoint.monthly_argv(["--with-commit-reclassification"], self.data, self.NOW)
        self.assertEqual(argv, ["--with-commit-reclassification", "--from", "ghs_download",
                                "--from", "repository_cloning"])
        self.assertIn("started 30 days ago, in October 2026 (retrieval 2026-10-12T104432Z)", why)

    def _begins(self, retrieval: str, start: str) -> bool:
        """Whether a Start at the time `start` begins the next monthly run after a completed run
        over the retrieval `retrieval`. Both times have the format of a retrieval's name, such as
        `2026-03-16T180701Z`."""
        self._state("success", retrieval)
        now = entrypoint.datetime.strptime(start, "%Y-%m-%dT%H%M%SZ").replace(
            tzinfo=entrypoint.timezone.utc)
        argv, _why = entrypoint.monthly_argv([], self.data, now)
        return "--from" in argv

    def test_the_month_begins_in_a_later_calendar_month(self):
        """The Starts of a monthly schedule are 28 to 31 days apart, and the decision compares
        calendar months rather than ages. A month that began late, such as on the 8th after the
        pre-checks refused the Start on the 1st, would otherwise make the next scheduled
        Start skip its month, because the month's download would be younger than a month. A Start
        within the month, such as the Start that loads a new image, continues the completed run."""
        for retrieval, start, begins in (
                ("2026-11-08T090000Z", "2026-12-01T000000Z", True),
                ("2026-11-01T000000Z", "2026-11-27T000000Z", False),
                ("2026-11-30T235959Z", "2026-11-30T235959Z", False),
                ("2026-12-10T000000Z", "2027-01-01T000000Z", True)):
            with self.subTest(retrieval=retrieval, start=start):
                self.assertEqual(self._begins(retrieval, start), begins)

    def test_a_month_that_began_late_or_on_its_last_day_does_not_skip_the_next(self):
        """A run in the next month follows a month that began on the 20th, as after a refused
        Start on the 1st that you repair on the 20th. A run in the next month also follows a
        first Start on the last day of a month, such as the first deployment late in October.
        The Start on the first of the next month begins that run, however few days before it the
        previous run began."""
        for retrieval, start in (("2026-11-20T090000Z", "2026-12-01T000000Z"),
                                 ("2026-10-31T120000Z", "2026-11-01T000000Z"),
                                 ("2026-10-18T080000Z", "2026-11-01T000000Z")):
            with self.subTest(retrieval=retrieval, start=start):
                self.assertTrue(self._begins(retrieval, start))

    # 2027-04-01 00:00 in Europe/Berlin, which is 22:00 on March 31 in UTC.
    APRIL_1_IN_BERLIN = entrypoint.datetime(2027, 3, 31, 22, tzinfo=entrypoint.timezone.utc)

    def test_the_calendar_months_are_those_of_the_nas(self):
        """DSM's Task Scheduler starts the container at midnight on the first of a month in the
        NAS's time zone, which in Berlin is still the last day of the month before in UTC.
        Compared in UTC, the months of a retrieval of March 5 and of the Start on April 1 would
        be the same, and the Start would skip April."""
        _berlin_file(self.zone)
        self._state("success", "2027-03-05T090300Z")
        argv, why = entrypoint.monthly_argv([], self.data, self.APRIL_1_IN_BERLIN)
        self.assertEqual(argv, ["--from", "ghs_download", "--from", "repository_cloning"])
        self.assertIn("in March 2027 (retrieval 2027-03-05T090300Z)", why)
        # A download that started five minutes after midnight on April 1 in Berlin is of
        # April, and a Start later that day continues its run.
        self._state("success", "2027-03-31T220500Z")
        self.assertEqual(entrypoint.monthly_argv([], self.data, self.APRIL_1_IN_BERLIN
                                                 + timedelta(hours=9)), ([], None))

    def test_the_zone_is_read_from_the_nas_file_and_not_from_the_containers_local_time(self):
        """compose.yaml mounts the NAS's /etc/localtime at a path of its own, and the container's
        own local time stays UTC, in which the cloning step writes the commit times. A Start at
        23:00 UTC on November 30 is at midnight on December 1 in Berlin. After a download on
        November 5, that Start begins December's run, whatever the local time of this process
        is."""
        november_5 = entrypoint.datetime(2026, 11, 5, 10, tzinfo=entrypoint.timezone.utc)
        start = entrypoint.datetime(2026, 11, 30, 23, tzinfo=entrypoint.timezone.utc)
        self.assertFalse(entrypoint.next_month_due(november_5, start))
        _berlin_file(self.zone)
        for local in ("UTC0", "EST5EDT,M3.2.0,M11.1.0"):
            with self.subTest(local_time_of_this_process=local):
                _use_zone(self, local)
                self.assertEqual(f"{start.astimezone(entrypoint.month_zone()):%Y-%m-%d %H:%M %Z}",
                                 "2026-12-01 00:00 CET")
                self.assertTrue(entrypoint.next_month_due(november_5, start))
                self._state("success", "2026-11-05T100000Z")
                argv, why = entrypoint.monthly_argv([], self.data, start)
                self.assertEqual(argv, ["--from", "ghs_download", "--from", "repository_cloning"])
                self.assertIn("in November 2026", why)

    def test_without_a_zone_the_months_are_those_of_utc(self):
        """A container without the NAS's zone file compares the months in UTC, and the log says
        so and names what to mount. For a short-form bind mount, Docker creates a directory in
        place of the file when the host has no file, and that directory counts as no file. A
        file that is not a time zone file, or that ends early, gives UTC with its error."""
        self._state("success", "2027-03-05T090300Z")
        self.zone.mkdir()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            argv, _why = entrypoint.monthly_argv([], self.data, self.APRIL_1_IN_BERLIN)
            entrypoint.log_month_zone(self.APRIL_1_IN_BERLIN)
        self.assertEqual(argv, [])
        self.assertIn(f"WARNING entrypoint: Calendar months are counted in UTC, because there "
                      f"is no time zone file at {self.zone}. Mount the NAS's /etc/localtime "
                      f"read-only at {self.zone}, as compose.yaml does.", err.getvalue())
        self.zone.rmdir()
        whole = _berlin_file(self.zone).read_bytes()
        for content in (b"not a time zone file\n", whole[:30], whole[:-1]):
            with self.subTest(content=content[-12:]):
                self.zone.write_bytes(content)
                found = []
                # `zoneinfo` reads forever on a zone file that ends inside its last line. The
                # call runs in a thread, and the test waits for it only up to a bound.
                reader = threading.Thread(target=lambda: found.append(entrypoint.host_zone()),
                                          daemon=True)
                reader.start()
                reader.join(10)
                self.assertEqual(len(found), 1, "reading the zone file did not return")
                zone, problem = found[0]
                self.assertIs(zone, entrypoint.timezone.utc)
                self.assertIn(f"{self.zone} cannot be read as a time zone file (", problem)
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    entrypoint.log_month_zone(self.APRIL_1_IN_BERLIN)
                self.assertIn("Calendar months are counted in UTC, because ", err.getvalue())

    def test_the_log_names_the_zone_and_where_it_comes_from(self):
        """The zone is the NAS's only when compose.yaml mounted the NAS's /etc/localtime, and
        the log line shows which zone a Start used."""
        _berlin_file(self.zone)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            entrypoint.log_month_zone(self.APRIL_1_IN_BERLIN)
        self.assertEqual(err.getvalue().splitlines(),
                         [f"INFO entrypoint: Calendar months are counted in CEST (UTC+02:00), "
                          f"the time zone of {self.zone}."])

    def test_a_late_month_does_not_make_the_next_scheduled_start_skip_its_month(self):
        """The pre-checks refuse the Start on November 1, which leaves the decision to the
        run before that Start. A manual Start begins the month on November 8, and the scheduled
        Start on December 1 begins December's run."""
        november_1 = entrypoint.datetime(2026, 11, 1, tzinfo=entrypoint.timezone.utc)
        november_8 = entrypoint.datetime(2026, 11, 8, 9, tzinfo=entrypoint.timezone.utc)
        self._state("pre_checks_failed", "2026-10-01T000000Z", last_run="success")
        for start in (november_1, november_8):
            self.assertIn("--from", entrypoint.monthly_argv([], self.data, start)[0])
        self._state("success", november_8.strftime("%Y-%m-%dT%H%M%SZ"))
        december_1 = entrypoint.datetime(2026, 12, 1, tzinfo=entrypoint.timezone.utc)
        argv, why = entrypoint.monthly_argv([], self.data, december_1)
        self.assertEqual(argv, ["--from", "ghs_download", "--from", "repository_cloning"])
        self.assertIn("started 22 days ago, in November 2026", why)

    def test_a_missing_record_of_the_download_is_logged(self):
        """Without the record, the start cannot tell when the GHS download started, and no
        monthly run begins until the record is repaired. A start that logged nothing would
        leave a missing month to be noticed in the handover directory."""
        for report in (None, "{not json", json.dumps({"outcome": "retrieved"})):
            with self.subTest(report=report):
                self._state("success")
                path = self.data / entrypoint.RETRIEVAL_REPORT
                if report is None:
                    path.unlink()
                else:
                    path.write_text(report, encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(entrypoint.monthly_argv([], self.data, self.NOW), ([], None))
                said = err.getvalue()
                self.assertIn(f"WARNING entrypoint: The previous run completed, but the record "
                              f"of its GHS download, /app/{entrypoint.DATA}/"
                              f"{entrypoint.RETRIEVAL_REPORT}, ", said)
                self.assertIn("does not exist" if report is None else "cannot be read", said)
                self.assertIn("no next monthly run begins. To begin it, put --from "
                              "ghs_download --from repository_cloning in the `command:` of "
                              "compose.yaml for one Start.", said)

    def test_a_start_within_the_month_continues_the_completed_run(self):
        self._state("success", "2026-11-02T000000Z")
        self.assertEqual(entrypoint.monthly_argv(["--with-commit-reclassification"], self.data,
                                                 self.NOW),
                         (["--with-commit-reclassification"], None))

    def test_a_run_that_did_not_complete_resumes(self):
        """A container killed during a step leaves the outcome `running`."""
        for outcome in ("failed", "interrupted", "running"):
            with self.subTest(outcome=outcome):
                self._state(outcome)
                self.assertEqual(entrypoint.monthly_argv([], self.data, self.NOW), ([], None))

    def test_a_start_that_ran_no_step_leaves_the_decision_to_the_run_before_it(self):
        """A Start that the pre-checks refuse, such as for an expired GitHub access token,
        leaves the outcome that the decision reads as the last run that ran steps left it. The
        Start after the refused one therefore still begins the next monthly run."""
        for outcome in ("pre_checks_failed", "modified_inputs"):
            with self.subTest(outcome=outcome):
                self._state(outcome, last_run="success")
                argv, why = entrypoint.monthly_argv([], self.data, self.NOW)
                self.assertEqual(argv, ["--from", "ghs_download", "--from", "repository_cloning"])
                self._state(outcome, last_run="failed")
                self.assertEqual(entrypoint.monthly_argv([], self.data, self.NOW), ([], None))

    def test_a_month_that_fell_back_is_as_old_as_its_attempt(self):
        """After a fallback, the record of the download names the previous retrieval. Measured
        by that retrieval's age, every Start after the fallback would begin a new monthly run."""
        fell_back = {"outcome": "fallback", "retrieval_id": "2026-09-10T000000Z",
                     "failed_retrieval_id": "2026-11-01T000000Z"}
        self._state("success", report=fell_back)
        self.assertEqual(entrypoint.monthly_argv([], self.data, self.NOW), ([], None))
        self._state("success", report={**fell_back, "failed_retrieval_id": "2026-10-10T000000Z"})
        argv, why = entrypoint.monthly_argv([], self.data, self.NOW)
        self.assertIn("--from", argv)
        self.assertIn("started 33 days ago, in October 2026 (retrieval 2026-10-10T000000Z)", why)

    def test_the_key_of_the_last_run_is_the_runners(self):
        from agentcontext import runner
        self.assertEqual(entrypoint.LAST_RUN_OUTCOME, runner.LAST_RUN_OUTCOME)

    def test_the_operators_from_or_mark_is_left_as_it_is(self):
        self._state("success")
        for argv in (["--from", "detection"], ["--from=release"],
                     ["--mark-complete", "repository_cloning"]):
            with self.subTest(argv=argv):
                self.assertEqual(entrypoint.monthly_argv(argv, self.data, self.NOW), (argv, None))

    def test_a_data_root_without_a_run_starts_the_first(self):
        self.assertEqual(entrypoint.monthly_argv([], self.data, self.NOW), ([], None))

    def test_the_next_runs_steps_are_steps_of_the_pipeline(self):
        sys.path.insert(0, str(HERE.parent))
        self.addCleanup(sys.path.remove, str(HERE.parent))
        import run_pipeline
        keys = {step.key for step in run_pipeline.compose(True)}
        self.assertLessEqual(set(entrypoint.NEW_RUN_FROM), keys)

    def test_the_next_monthly_run_begins_a_new_collection(self):
        """The arguments of the next monthly run name the cloning step, and the runner removes
        the record of the collection that ended for it. The step then begins a new collection
        over a sample with the same repositories or without repositories that GitHub no longer
        serves, rather than continue the previous month's collection and publish its heads."""
        self._state("success")
        argv, _why = entrypoint.monthly_argv([], self.data, self.NOW)
        sys.path.insert(0, str(HERE.parent))
        self.addCleanup(sys.path.remove, str(HERE.parent))
        import run_pipeline
        from agentcontext import resume, runner
        finished = self.data / "collection" / resume.FINISHED_NAME
        finished.parent.mkdir()
        for repos, gone in ((["o/a", "o/b"], set()), (["o/a"], {"o/b"})):
            with self.subTest(repos=repos):
                with contextlib.redirect_stderr(io.StringIO()):
                    ended = resume.open_collection(finished, ["o/a", "o/b"])
                resume.close_collection(finished)
                with mock.patch.object(runner, "data_dir", return_value=finished.parent):
                    runner.drop_completed_from(
                        {"completed": {}}, run_pipeline.compose(True),
                        runner.build_parser().parse_args(argv).from_name)
                with contextlib.redirect_stderr(io.StringIO()):
                    new = resume.open_collection(finished, repos, gone)
                self.assertNotEqual(new["collection"], ended["collection"])
                resume.close_collection(finished)

    def test_every_from_of_the_arguments_is_named_and_nothing_else(self):
        """A run with a `--from` overwrites the outputs of the run before it for the steps that it
        redoes. Before a run with a `--from` of a step before the analysis stage, a start packs
        the data root of a released run. The start names the `--from` arguments in its log as
        `command:` gives them."""
        for argv, named in (
                (["--from", "ghs_download", "--from", "repository_cloning"],
                 ["--from", "ghs_download", "--from", "repository_cloning"]),
                (["--with-commit-reclassification", "--from=repository_labeling"],
                 ["--from", "repository_labeling"]),
                (["--step-arg", "ghs_download:--accept-smaller"], []),
                (["--mark-complete", "ghs_download"], []),
                (["--step-arg", "repository_cloning:--from x"], [])):
            with self.subTest(argv=argv):
                self.assertEqual(entrypoint.from_arguments(argv), named)

    def test_every_step_after_the_release_is_a_step_of_the_analysis_stage(self):
        """`failed_analysis_step` takes a step that failed after the release step for a step of
        the analysis stage. `redoes_only_the_analysis` takes a `--from` of a step after the
        release step, or of the stage's name, for a `--from` that rewrites only `analysis/`. Both
        functions are correct only while the analysis stage follows the release step and nothing
        else does."""
        sys.path.insert(0, str(HERE.parent))
        self.addCleanup(sys.path.remove, str(HERE.parent))
        import run_pipeline
        for reclassification in (False, True):
            with self.subTest(with_commit_reclassification=reclassification):
                steps = run_pipeline.compose(reclassification)
                keys = [step.key for step in steps]
                after = steps[keys.index(entrypoint.RELEASE_STEP) + 1:]
                self.assertTrue(after)
                self.assertEqual({step.stage for step in after}, {entrypoint.ANALYSIS_STAGE})
                self.assertFalse([step.key for step in steps[:len(steps) - len(after)]
                                  if step.stage == entrypoint.ANALYSIS_STAGE])

    def _failed(self, steps: list) -> None:
        """Write the pipeline execution state of a start that ran `steps`, each (name, status),
        and failed, over a retrieval taken 30 days ago."""
        (self.data / "run_logs").mkdir(exist_ok=True)
        results = [{"name": name, "status": status} for name, status in steps]
        (self.data / "run_logs" / "state.json").write_text(json.dumps(
            {"outcome": "failed", "last_run_outcome": "failed", "steps": results,
             entrypoint.LAST_RUN_STEPS: results}))
        (self.data / "sampling").mkdir(exist_ok=True)
        (self.data / "sampling" / "ghs_retrieval_report.json").write_text(
            json.dumps({"outcome": "retrieved", "retrieval_id": "2026-10-12T104432Z"}))

    def test_a_run_that_failed_in_the_analysis_after_its_release_begins_the_next_one(self):
        """The collection and the release of the run are complete. A step of the analysis
        stage that fails at every Start would otherwise keep every later month from being
        collected."""
        for release in ("ok", "skipped"):
            with self.subTest(release=release):
                self._failed([("detection", "skipped"), ("release", release),
                              ("rq2_upset_plot", "ok"), ("rq3_count_distribution", "failed")])
                self.assertEqual(entrypoint.failed_analysis_step(self.data),
                                 "rq3_count_distribution")
                argv, why = entrypoint.monthly_argv([], self.data, self.NOW)
                self.assertEqual(argv, ["--from", "ghs_download", "--from", "repository_cloning"])
                self.assertIn("The previous run completed its release step and failed in "
                              "rq3_count_distribution", why)

    def test_a_run_that_failed_before_or_in_its_release_resumes(self):
        """A step of the collection that the release does not read can fail while an earlier
        start recorded the release, and the run is then not done with its collection."""
        for steps in ([("detection", "ok"), ("deleted_files_report", "failed")],
                      [("detection", "ok"), ("release", "failed")],
                      [("release", "ok"), ("rq2_upset_plot", "interrupted")],
                      []):
            with self.subTest(steps=steps):
                self._failed(steps)
                self.assertIsNone(entrypoint.failed_analysis_step(self.data))
                self.assertEqual(entrypoint.monthly_argv([], self.data, self.NOW), ([], None))

    def test_a_from_redoes_only_the_analysis_when_it_names_the_stage_or_a_listed_step(self):
        """A `--from` of the analysis stage, or of a step that the step results of the last run
        list after the release step, rewrites only `analysis/` and `run_logs/`. A `--from` of the
        release step, of a stage or step before the release step, or of an analysis step after
        the failed step, which the step results do not list, counts as a `--from` of a step
        before the analysis stage. A mixture of both kinds counts the same way."""
        self._failed([("detection", "skipped"), ("release", "ok"),
                      ("rq2_upset_plot", "ok"), ("rq3_count_distribution", "failed")])
        for names, only in ((["rq3_count_distribution"], True),
                            (["rq2_upset_plot", "rq3_count_distribution"], True),
                            (["analysis"], True),
                            (["release"], False), (["publication"], False),
                            (["detection"], False), (["rq3_adoption_timeline"], False),
                            (["rq3_count_distribution", "release"], False), ([], False)):
            with self.subTest(names=names):
                froms = [arg for name in names for arg in ("--from", name)]
                self.assertEqual(entrypoint.redoes_only_the_analysis(froms, self.data), only)

    def test_the_release_step_is_done_after_a_stop_or_a_kill_in_the_analysis(self):
        """Once the release step of a run is done, a `--from` of a step before the analysis stage
        packs the data root first, whatever the outcome of the run. A kill before any step of its
        start ended leaves the step results of the previous start in `last_run_steps`. The step
        results of the killed start show whether the kill came after the release step. A kill
        before it, such as in a cloning step that a new image runs again, leaves part of a
        collection."""
        released = [{"name": "repository_cloning", "status": "ok"},
                    {"name": "release", "status": "ok"},
                    {"name": "rq2_upset_plot", "status": "ok"}]
        skipped = [{"name": "ghs_download", "status": "skipped"}]
        for outcome, steps, last, running, done in (
                ("success", released, released, None, True),
                ("failed", released[:2] + [{"name": "rq2_upset_plot", "status": "failed"}],
                 None, None, True),
                ("interrupted", released[:2] + [{"name": "rq2_upset_plot",
                                                 "status": "interrupted"}], None, None, True),
                ("running", released[:2], None, "rq2_upset_plot", True),
                ("running", [*skipped, {"name": "release", "status": "skipped"}], released,
                 "rq2_upset_plot", True),
                ("running", skipped, released, "repository_cloning", False),
                ("interrupted", [{"name": "repository_cloning", "status": "interrupted"}], None,
                 None, False),
                ("failed", [{"name": "release", "status": "failed"}], None, None, False),
                ("pre_checks_failed", [], released, None, True)):
            with self.subTest(outcome=outcome, steps=steps, running=running):
                state = {"outcome": outcome, "last_run_outcome": outcome, "steps": steps,
                         entrypoint.LAST_RUN_STEPS: steps if last is None else last}
                if running is not None:
                    # The runner writes the name of the running step, and the killed start's step
                    # results up to that step.
                    state[entrypoint.RUNNING_STEP] = running
                    state[entrypoint.RUNNING_STEP_RESULTS] = steps
                (self.data / "run_logs").mkdir(exist_ok=True)
                (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state),
                                                               encoding="utf-8")
                self.assertEqual(entrypoint.release_step_completed(self.data), done)


class TestTheArgumentsForOneStart(unittest.TestCase):
    """The arguments that the runbook gives for one Start, which `main` passes until a run has
    used them. `TestTheEntrypointMain` runs the Starts."""

    def test_they_are_from_mark_complete_and_a_fresh_of_a_step(self):
        argv = ["--with-commit-reclassification", "--from", "detection", "--from=release",
                "--mark-complete", "repository_cloning", "--step-arg", "repository_cloning:-w 3",
                "--step-arg=repository_cloning:--fresh -w 2", "--repo-csv", "subset.csv",
                "--from", "detection"]
        self.assertEqual(entrypoint.one_start_arguments(argv),
                         [["--from", "detection"], ["--from", "release"],
                          ["--mark-complete", "repository_cloning"],
                          ["--step-arg", "repository_cloning:--fresh"]])
        self.assertEqual(entrypoint.one_start_arguments(
            ["--with-commit-reclassification", "--step-arg", 'file_history:--keep-blank',
             "--min-free-gb", "10", "--from"]), [])

    def test_the_runner_refuses_an_abbreviation_that_the_entrypoint_does_not_recognize(self):
        """The entrypoint recognizes `--from`, `--mark-complete` and `--step-arg` by their full
        names. By default, argparse takes an abbreviation of a long option for the option. A
        `--fro release` that the runner took for `--from release` would then run the release
        again at every Start, because the entrypoint never drops it. The runner refuses every
        abbreviation of the three flags with a usage error, and the first Start stops on that
        error."""
        sys.path.insert(0, str(HERE.parent))
        self.addCleanup(sys.path.remove, str(HERE.parent))
        import run_pipeline
        for flag in (*entrypoint.ONE_START_FLAGS, entrypoint.STEP_ARG):
            for end in range(len("--x"), len(flag)):
                words = [flag[:end], "repository_cloning:--fresh"]
                with self.subTest(words=words):
                    self.assertEqual(entrypoint.one_start_arguments(words), [])
                    with contextlib.redirect_stderr(io.StringIO()), \
                            self.assertRaises(SystemExit) as stop:
                        run_pipeline.build_parser().parse_args(words)
                    self.assertEqual(stop.exception.code, run_pipeline.cli.USAGE_ERROR)

    def test_an_accept_smaller_of_the_download_is_one_too(self):
        """Left in `command:`, `--accept-smaller` would take every later retrieval however many
        rows GHS lost."""
        argv = ["--step-arg", "ghs_download:--accept-smaller", "--step-arg",
                "repository_cloning:-w 3"]
        self.assertEqual(entrypoint.one_start_arguments(argv),
                         [["--step-arg", "ghs_download:--accept-smaller"]])
        self.assertEqual(entrypoint.without_arguments(
            argv, entrypoint.one_start_arguments(argv)),
            ["--step-arg", "repository_cloning:-w 3"])

    def test_an_end_date_of_the_download_is_one_too(self):
        """Left in `command:`, `--end-date=` would keep the sampling window of every later
        monthly run where it was. Any date counts, and the other words of the value stay."""
        argv = ["--step-arg", "ghs_download:--end-date=2026-10-12 --accept-smaller",
                "--step-arg", "repository_cloning:-w 3"]
        self.assertEqual(entrypoint.one_start_arguments(argv),
                         [["--step-arg", "ghs_download:--end-date=2026-10-12"],
                          ["--step-arg", "ghs_download:--accept-smaller"]])
        self.assertEqual(entrypoint.without_arguments(
            argv, [["--step-arg", "ghs_download:--end-date=2026-10-12"]]),
            ["--step-arg", "ghs_download:--accept-smaller", "--step-arg", "repository_cloning:-w 3"])

    def test_an_end_date_with_its_value_in_the_next_word_is_one_too(self):
        """The download takes the date after `--end-date` as well as after `=`, and both forms
        name the same argument, which leaves the value with its date when the argument is used."""
        argv = ["--step-arg", "ghs_download:--end-date 2026-10-12 --accept-smaller"]
        self.assertEqual(entrypoint.one_start_arguments(argv),
                         [["--step-arg", "ghs_download:--end-date=2026-10-12"],
                          ["--step-arg", "ghs_download:--accept-smaller"]])
        self.assertEqual(entrypoint.without_arguments(
            argv, [["--step-arg", "ghs_download:--end-date=2026-10-12"]]),
            ["--step-arg", "ghs_download:--accept-smaller"])
        self.assertEqual(entrypoint.without_arguments(
            ["--step-arg", "ghs_download:--end-date 2026-10-12"],
            [["--step-arg", "ghs_download:--end-date=2026-10-12"]]), [])

    def test_leaving_them_out_keeps_the_settings(self):
        """A `--step-arg` value keeps its other words, which are settings, in the form in which
        `command:` gives them. The `--step-arg` flag is dropped when no word is left."""
        argv = ["--with-commit-reclassification", "--from", "detection", "--from=release",
                "--step-arg", "repository_cloning:-w 3 --fresh",
                "--step-arg=repository_cloning:--fresh", "--step-arg", "file_history:--keep-blank"]
        self.assertEqual(entrypoint.without_arguments(argv, [
            ["--from", "detection"], ["--from", "release"],
            ["--step-arg", "repository_cloning:--fresh"]]),
            ["--with-commit-reclassification", "--step-arg", "repository_cloning:-w 3",
             "--step-arg", "file_history:--keep-blank"])
        self.assertEqual(entrypoint.without_arguments(argv, [["--from", "detection"]]),
                         ["--with-commit-reclassification", "--from=release",
                          "--step-arg", "repository_cloning:-w 3 --fresh",
                          "--step-arg=repository_cloning:--fresh",
                          "--step-arg", "file_history:--keep-blank"])
        self.assertEqual(entrypoint.without_arguments(argv, []), argv)

    def test_the_refusals_are_outcomes_of_the_runner_that_run_no_step(self):
        """Because `--mark-complete` records a step, its outcome is no refusal."""
        from agentcontext import runner
        self.assertLess(set(entrypoint.REFUSED_OUTCOMES), set(runner.NO_STEP_OUTCOMES))
        self.assertNotIn("marked_complete", entrypoint.REFUSED_OUTCOMES)

    def test_the_runner_used_an_argument_when_its_state_shows_the_effect(self):
        """The test runs over the pipeline execution state that the runner's own
        `write_execution_state` writes after the start passed the argument. A `--from` acts when
        the runner takes the steps that it names out of the record of completed steps, after the
        last check that can refuse the run, or when a step runs. A `--fresh` acts when its own
        step starts, as the step results or the step that a kill ended show. An
        `--accept-smaller` acts when the download ends `ok` and its record names the retrieval
        that it took. A download that failed, was stopped or was killed took no retrieval, and
        neither did a download that ended `ok` after it fell back to the previous sampling frame,
        such as when GHS did not respond. Without the argument, the next download would refuse
        the smaller GHS search result."""
        from agentcontext import runner
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)

        def restore():
            runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
        self.addCleanup(restore)
        runner.use_log_dir(data / "run_logs")
        completed = {"ghs_download": "output:a", "repository_cloning": "output:b",
                     "release": "c", "rq2_config_cooccurrence": "inputs:d"}
        from_rq2 = ["--from", "rq2_config_cooccurrence"]
        fresh = ["--step-arg", "repository_cloning:--fresh"]
        accept = ["--step-arg", "ghs_download:--accept-smaller"]
        rq2 = ("rq2_config_cooccurrence",)
        report = data / entrypoint.RETRIEVAL_REPORT
        report.parent.mkdir(parents=True)
        listed = data / entrypoint.FINISHED_LIST
        # What the GHS download writes into its record: the retrieval that it took, or the
        # retrieval that it attempted and the previous retrieval that it kept.
        downloads = {"retrieved": {"outcome": "retrieved", "retrieval_id": "2026-12-01T000300Z"},
                     "fallback": {"outcome": "fallback", "retrieval_id": "2026-11-01T000300Z",
                                  "failed_retrieval_id": "2026-12-01T000300Z",
                                  "reason": "GHS did not respond", "fallbacks_in_a_row": 1}}
        for (outcome, steps, dropped, running_step, download, used_from, used_fresh,
             used_accept) in (
                ("pre_checks_failed", [], (), None, None, False, False, False),
                ("modified_inputs", [], (), None, None, False, False, False),
                # A check that fails before the record changes, such as a git that cannot start
                # while the runner fingerprints the cloning step, and a second signal during the
                # pre-checks.
                ("failed", [], (), None, None, False, False, False),
                ("interrupted", [], (), None, None, False, False, False),
                # A Stop after the runner took the steps out of the record, before a step ran.
                ("interrupted", [("release", "skipped")], rq2, None, None, True, False, False),
                ("success", [("repository_cloning", "skipped"), ("release", "ok")], rq2, None,
                 None, True, False, False),
                ("failed", [("repository_cloning", "failed")], (), None, None, True, True, False),
                ("interrupted", [("repository_cloning", "interrupted")], (), None, None, True,
                 True, False),
                ("running", [("ghs_download", "ok")], (), "repository_cloning", "retrieved", True,
                 True, True),
                # A kill in a step before the cloning step, to which the `--fresh` belongs, and a
                # kill inside the download, which took no retrieval.
                ("running", [], rq2, "ghs_download", None, True, False, False),
                ("running", [("ghs_download", "ok")], (), None, "retrieved", True, False, True),
                # A download that failed or was stopped, such as on a network error.
                ("failed", [("ghs_download", "failed")], (), None, None, True, False, False),
                ("interrupted", [("ghs_download", "interrupted")], (), None, None, True, False,
                 False),
                # A download that fell back to the previous sampling frame exits 0, whatever the
                # reason. A download without a readable record shows no retrieval either.
                ("success", [("ghs_download", "ok"), ("release", "ok")], (), None, "fallback",
                 True, False, False),
                ("success", [("ghs_download", "ok"), ("release", "ok")], (), None, None, True,
                 False, False),
                ("success", [("ghs_download", "ok"), ("release", "ok")], (), None, "retrieved",
                 True, False, True)):
            with self.subTest(outcome=outcome, steps=steps, running_step=running_step,
                              download=download):
                report.unlink(missing_ok=True)
                if download is not None:
                    report.write_text(json.dumps(downloads[download]), encoding="utf-8")
                runner.write_execution_state({"completed": dict(completed)}, [], "success",
                                             time.time(), ["run_pipeline.py"])
                listed.parent.mkdir(parents=True, exist_ok=True)
                listed.write_text('{"repo": "owner/repo"}\n', encoding="utf-8")
                before = list(entrypoint.state_signature(data))
                completed_before = entrypoint.completed_names(data)
                finished_before = entrypoint.finished_list_identity(data)
                self.assertFalse(entrypoint.took_effect(from_rq2, data, before, completed_before))
                if used_fresh:
                    # The `--fresh` of the cloning step first removes the list, and the step then
                    # writes a new list.
                    listed.unlink()
                    listed.write_text('{"repo": "other/repo"}\n', encoding="utf-8")
                time.sleep(0.01)
                state = runner.read_state()
                for name in dropped:
                    state["completed"].pop(name)
                runner.write_execution_state(
                    state, [{"name": name, "status": status, "duration_seconds": 0}
                            for name, status in steps],
                    outcome, time.time(), ["run_pipeline.py"], running_step=running_step)
                self.assertEqual(entrypoint.took_effect(from_rq2, data, before, completed_before),
                                 used_from)
                self.assertEqual(entrypoint.took_effect(fresh, data, before, completed_before,
                                                        finished_before=finished_before),
                                 used_fresh)
                self.assertEqual(entrypoint.took_effect(accept, data, before, completed_before),
                                 used_accept)


    def test_a_start_that_a_check_stops_keeps_the_step_that_a_kill_ended_before_it(self):
        """A kill inside the cloning step that a new image runs again leaves part of a
        collection. A check that stops the next start before its first step, such as a git that
        cannot start, ends that start `failed`. The test runs over the runner's own writes. The
        data root is still not released, and the start counts as one that a check stopped. A
        `--fresh` passed to that start did not act, because the cloning step did not start."""
        from agentcontext import runner
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)

        def restore():
            runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
        self.addCleanup(restore)
        runner.use_log_dir(data / "run_logs")
        skipped = [{"name": "ghs_download", "status": "skipped", "duration_seconds": 0}]
        state = {"completed": {"ghs_download": "output:a"}}
        with contextlib.redirect_stderr(io.StringIO()):
            runner.write_execution_state(
                state, [*skipped, {"name": "repository_cloning", "status": "ok"},
                        {"name": "release", "status": "ok"}], "success", time.time() - 30,
                ["run_pipeline.py"])
            runner.write_execution_state(state, skipped, "running", time.time() - 20,
                                         ["run_pipeline.py"], running_step="repository_cloning")
        self.assertFalse(entrypoint.release_step_completed(data))
        before = list(entrypoint.state_signature(data))
        completed_before = entrypoint.completed_names(data)
        fresh = ["--step-arg", "repository_cloning:--fresh"]
        with contextlib.redirect_stderr(io.StringIO()):
            started = time.time() - 10
            runner.write_execution_state(state, skipped, "running", started, ["run_pipeline.py"])
            runner.write_execution_state(state, skipped, "failed", started, ["run_pipeline.py"])
        self.assertFalse(entrypoint.release_step_completed(data))
        self.assertTrue(entrypoint.stopped_by_a_check(data))
        self.assertFalse(entrypoint.took_effect(fresh, data, before, completed_before))

    def test_a_fresh_counts_only_when_its_step_removed_the_list(self):
        """A cloning step can fail to start, exit before its `--fresh` removed anything, or be
        stopped while its interpreter starts. Such a step still has a result or is named as the
        step that ran. If that counted as the `--fresh` acting, the next Start would drop the
        `--fresh` and resume the earlier collection. The list of finished repositories, which a
        `--fresh` removes first, shows whether the `--fresh` deleted anything."""
        from agentcontext import resume, runner
        self.assertEqual(entrypoint.FINISHED_LIST, f"collection/{resume.FINISHED_NAME}")
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)

        def restore():
            runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
        self.addCleanup(restore)
        runner.use_log_dir(data / "run_logs")
        listed = data / entrypoint.FINISHED_LIST
        listed.parent.mkdir(parents=True)
        fresh = ["--step-arg", "repository_cloning:--fresh"]
        skipped = [{"name": "ghs_download", "status": "skipped", "duration_seconds": 0}]
        for outcome, results, running_step in (
                ("failed", [*skipped, {"name": "repository_cloning", "status": "failed"}], None),
                ("interrupted", [*skipped, {"name": "repository_cloning",
                                            "status": "interrupted"}], None),
                ("running", skipped, "repository_cloning")):
            for removed in (False, True):
                with self.subTest(outcome=outcome, removed=removed):
                    listed.write_text('{"repo": "owner/repo"}\n', encoding="utf-8")
                    with contextlib.redirect_stderr(io.StringIO()):
                        runner.write_execution_state({"completed": {}}, [], "success",
                                                     time.time(), ["run_pipeline.py"])
                    before = list(entrypoint.state_signature(data))
                    finished_before = entrypoint.finished_list_identity(data)
                    time.sleep(0.01)
                    if removed:
                        listed.unlink()
                    with contextlib.redirect_stderr(io.StringIO()):
                        runner.write_execution_state({"completed": {}}, results, outcome,
                                                     time.time(), ["run_pipeline.py"],
                                                     running_step=running_step)
                    self.assertEqual(entrypoint.took_effect(fresh, data, before, (),
                                                            finished_before=finished_before),
                                     removed)

    def test_a_start_that_a_check_stops_after_a_kill_in_the_analysis_leaves_the_release_done(
            self):
        """A kill inside an analysis step comes after the release step. A check that stops the
        next start before its first step writes step results that end where the check stopped,
        before the release step. Read from those step results, the release would look undone,
        and a later Start with a `--from` of a step before the analysis would rewrite the data
        root without packing it first. The test runs over the runner's own writes."""
        from agentcontext import runner
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)

        def restore():
            runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
        self.addCleanup(restore)
        runner.use_log_dir(data / "run_logs")
        released = [{"name": "detection", "status": "ok", "duration_seconds": 0},
                    {"name": "release", "status": "ok", "duration_seconds": 0}]
        state = {"completed": {}}
        with contextlib.redirect_stderr(io.StringIO()):
            runner.write_execution_state(
                state, [*released, {"name": "rq2_upset_plot", "status": "ok",
                                    "duration_seconds": 0}],
                "success", time.time() - 30, ["run_pipeline.py"])
            runner.write_execution_state(state, released, "running", time.time() - 20,
                                         ["run_pipeline.py"], running_step="rq2_upset_plot")
        self.assertTrue(entrypoint.release_step_completed(data))
        with contextlib.redirect_stderr(io.StringIO()):
            started = time.time() - 10
            runner.write_execution_state(state, [], "failed", started, ["run_pipeline.py"])
        self.assertTrue(entrypoint.stopped_by_a_check(data))
        self.assertTrue(entrypoint.release_step_completed(data))

    def test_an_accept_smaller_counts_from_the_retrieval_that_the_download_took(self):
        """A kill after the download exited 0 and before the runner recorded the step leaves no
        result of the step. The next start records the step from the runner's `exited` record
        without running it. A test of the step results would therefore leave the argument in
        `command:` for every later download, which would then take any smaller GHS search
        result. The record of the download decides instead. The argument acted when the record
        names a retrieval that it did not name before the runner started."""
        from agentcontext import runner
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)

        def restore():
            runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
        self.addCleanup(restore)
        runner.use_log_dir(data / "run_logs")
        accept = ["--step-arg", "ghs_download:--accept-smaller"]
        report = data / entrypoint.RETRIEVAL_REPORT
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({"outcome": "retrieved",
                                      "retrieval_id": "2026-11-01T000300Z"}), encoding="utf-8")
        runner.write_execution_state({"completed": {"ghs_download": "output:a"}}, [], "success",
                                     time.time(), ["run_pipeline.py"])
        # The record of the start as `run_once` leaves it when a kill ends the start. It holds
        # the passed argument, and the state and the download's retrieval from before the runner
        # started.
        (data / entrypoint.ONE_START_RECORD).write_text(json.dumps({
            "used": [], "passed": [accept],
            "state_before": list(entrypoint.state_signature(data)),
            "completed_before": ["ghs_download"], "retrieval_before": "2026-11-01T000300Z"}),
            encoding="utf-8")
        time.sleep(0.01)
        runner.write_execution_state({"completed": {}}, [], "running", time.time(),
                                     ["run_pipeline.py"], running_step="ghs_download")
        self.assertEqual(entrypoint.used_arguments(data), [])
        report.write_text(json.dumps({"outcome": "retrieved", "retrieval_id": "2026-12-01T000300Z",
                                      "accepted_smaller": {"previous": 10, "now": 5}}),
                          encoding="utf-8")
        self.assertEqual(entrypoint.used_arguments(data), [accept])

    def test_an_end_date_counts_from_the_retrieval_that_the_download_took(self):
        """The end date acted when the download took a retrieval, as for `--accept-smaller`. A
        download that fell back to the previous sampling frame took none, and the next start
        passes the end date again."""
        from agentcontext import runner
        data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, data, True)
        saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)

        def restore():
            runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
        self.addCleanup(restore)
        runner.use_log_dir(data / "run_logs")
        end_date = ["--step-arg", "ghs_download:--end-date=2026-10-12"]
        report = data / entrypoint.RETRIEVAL_REPORT
        report.parent.mkdir(parents=True)
        runner.write_execution_state({"completed": {}}, [], "success", time.time(),
                                     ["run_pipeline.py"])
        (data / entrypoint.ONE_START_RECORD).write_text(json.dumps({
            "used": [], "passed": [end_date],
            "state_before": list(entrypoint.state_signature(data)),
            "completed_before": [], "retrieval_before": None}), encoding="utf-8")
        time.sleep(0.01)
        report.write_text(json.dumps({"outcome": "failed"}), encoding="utf-8")
        runner.write_execution_state({"completed": {}}, [], "running", time.time(),
                                     ["run_pipeline.py"], running_step="ghs_download")
        self.assertEqual(entrypoint.used_arguments(data), [])
        report.write_text(json.dumps({"outcome": "retrieved",
                                      "retrieval_id": "2026-11-01T000300Z"}), encoding="utf-8")
        self.assertEqual(entrypoint.used_arguments(data), [end_date])


class TestTheMachineRecord(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_the_record_names_what_a_container_can_see(self):
        record = entrypoint.machine_record(self.tmp, "20260913T000000Z", "commit abc")
        self.assertEqual(record["recorded_at"], "20260913T000000Z")
        self.assertEqual(record["image"], "commit abc")
        self.assertEqual(record["cpus"], os.cpu_count())
        self.assertEqual(record["state_volume"]["path"], str(self.tmp))
        self.assertGreater(record["state_volume"]["total_bytes"], 0)
        self.assertGreaterEqual(record["state_volume"]["total_bytes"],
                                record["state_volume"]["free_bytes"])
        for key in ("hostname", "kernel", "architecture", "cpu_model", "memory_total_bytes",
                    "memory_limit_bytes", "process_limit", "nproc_limit", "pid_max",
                    "threads_max", "init_process"):
            self.assertIn(key, record)
        self.assertIn("process limit", entrypoint.machine_line(record))
        if Path("/proc/meminfo").exists():
            self.assertGreater(record["memory_total_bytes"], 0)

    def test_the_record_is_one_line_in_the_log(self):
        record = entrypoint.machine_record(self.tmp, "20260913T000000Z", "commit abc")
        line = entrypoint.machine_line(record)
        self.assertTrue(line.startswith(f"The machine {record['hostname']} has "
                                        f"{os.cpu_count()} CPUs ("), line)
        self.assertIn("free of", line)

    def test_the_record_names_the_versions_of_the_programs_the_steps_run(self):
        """git determines values in published columns. The record names the version of git
        that the run used, so that a handover shows which git read the histories, also after
        `mise.toml` pins another git."""
        record = entrypoint.machine_record(self.tmp, "20260913T000000Z", "commit abc")
        self.assertEqual(set(record["tools"]), {"git", "7zz", "rsync"})
        self.assertTrue(record["tools"]["git"].startswith("git version "), record["tools"])
        self.assertIn(record["tools"]["git"], entrypoint.machine_line(record))

    def test_a_program_that_is_not_there_is_none(self):
        with mock.patch.dict(entrypoint.TOOL_VERSION_COMMANDS,
                             {"git": [str(self.tmp / "no-such-git"), "--version"]}):
            self.assertIsNone(entrypoint.tool_versions()["git"])
            line = entrypoint.machine_line(
                entrypoint.machine_record(self.tmp, "20260913T000000Z", "commit abc"))
        self.assertIn("print these versions (git not found, ", line)

    def test_the_record_names_the_cpu_quota_of_the_containers_cgroup(self):
        """`os.cpu_count()` counts the host's processors, also in a container that a `cpus:`
        limit gives fewer. The record therefore names the quota of the container's cgroup beside
        the count. The quota is `cpu.max` of cgroup v2, or the quota and the period of cgroup v1,
        in which -1 sets no quota."""
        cgroup = self.tmp / "cgroup"
        for files, quota in (
                ({"cpu.max": "800000 100000\n"}, 8.0),
                ({"cpu.max": "150000 100000\n"}, 1.5),
                ({"cpu.max": "max 100000\n"}, None),
                ({"cpu/cpu.cfs_quota_us": "400000\n", "cpu/cpu.cfs_period_us": "100000\n"}, 4.0),
                ({"cpu/cpu.cfs_quota_us": "-1\n", "cpu/cpu.cfs_period_us": "100000\n"}, None),
                ({}, None)):
            with self.subTest(files=files):
                shutil.rmtree(cgroup, ignore_errors=True)
                for name, text in files.items():
                    (cgroup / name).parent.mkdir(parents=True, exist_ok=True)
                    (cgroup / name).write_text(text, encoding="utf-8")
                cgroup.mkdir(exist_ok=True)
                with mock.patch.object(entrypoint, "CGROUP", cgroup):
                    record = entrypoint.machine_record(self.tmp, "20260913T000000Z", "commit abc")
                self.assertEqual(record["cpu_quota"], quota)
                self.assertEqual(record["cpus"], os.cpu_count())
                self.assertIn(f"{os.cpu_count()} CPUs (", entrypoint.machine_line(record))
                self.assertIn("), " + ("no CPU quota" if quota is None
                                       else f"a CPU quota of {quota:g}") + " and ",
                              entrypoint.machine_line(record))

    def test_a_missing_proc_file_is_none_not_an_error(self):
        self.assertIsNone(entrypoint._proc_value(str(self.tmp / "absent"), "MemTotal"))
        (self.tmp / "meminfo").write_text("MemTotal:       16384 kB\nMemFree: 1 kB\n")
        self.assertEqual(entrypoint._proc_value(str(self.tmp / "meminfo"), "MemTotal"), "16384 kB")

    def test_the_machine_record_is_written_to_the_run_logs(self):
        self.assertEqual(entrypoint.MACHINE_RECORD, Path("run_logs/machine.json"))


class TestTheLogFormat(unittest.TestCase):
    """Every line that this script writes has the format `LEVEL component: message`, as the
    package's lines do.

    One Log tab shows the lines of this script, the runner's own lines and each step's output,
    and the component name at the start of a line shows an operator which program wrote it.
    """

    def test_the_name_is_the_entrypoint(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            entrypoint.log("The runner exited 0 (complete)")
        self.assertEqual(out.getvalue(), "INFO entrypoint: The runner exited 0 (complete)\n")

    def test_a_warning_and_an_error_are_logged_with_their_level(self):
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            entrypoint.log_warning("No GitHub access token")
            entrypoint.log_error("/app/handover is not a mount")
        self.assertEqual(out.getvalue(), "WARNING entrypoint: No GitHub access token\n"
                                        "ERROR entrypoint: /app/handover is not a mount\n")

    def test_the_format_is_the_packages(self):
        """The entrypoint repeats the format rather than importing it, and this test checks the
        copy against the original."""
        from agentcontext import logs
        self.assertEqual(entrypoint.LOG_FORMAT, logs.FORMAT)

    def test_the_script_writes_no_bracketed_tag_of_its_own(self):
        source = (HERE / "entrypoint.py").read_text(encoding="utf-8")
        for tag in ("[CONTAINER]", "[ENTRYPOINT]", "[PREFLIGHT]", "[PROGRESS]", "[STOPPED]"):
            self.assertNotIn(tag, source)

    def test_every_line_goes_through_the_format(self):
        """A bare print would write a line without a component name."""
        source = (HERE / "entrypoint.py").read_text(encoding="utf-8")
        bare = [line.strip() for line in source.splitlines()
                if line.strip().startswith("print(") and "LOG_FORMAT" not in line]
        self.assertEqual(bare, [])


class TestTheRunnerCommand(unittest.TestCase):

    def test_listing_flags_need_no_mount(self):
        self.assertTrue(entrypoint.wants_listing(["--list"]))
        self.assertTrue(entrypoint.wants_listing(["--with-commit-reclassification", "--help"]))
        self.assertFalse(entrypoint.wants_listing(["--with-commit-reclassification"]))
        self.assertFalse(entrypoint.wants_listing([]))

    def test_the_runner_is_the_images_run_pipeline_with_the_arguments_passed_through(self):
        # The code of every stage is in the image, and the runner is the image's run_pipeline.py
        # rather than a script on the mounted data root.
        argv = entrypoint.runner_argv(["--from", 'detection'])
        self.assertEqual(argv, [sys.executable, "/opt/pipeline/run_pipeline.py", "--from",
                                'detection'])


# ---------------------------------------------------------------------------
# The Dockerfile, the compose file and the environment example
# ---------------------------------------------------------------------------

class TestTheDockerfile(unittest.TestCase):

    def setUp(self):
        self.text = (HERE / "Dockerfile").read_text(encoding="utf-8")

    def test_both_images_start_from_one_base_image_named_by_digest(self):
        """A tag such as python:3.14-slim names the newest build of the image on Docker Hub, and
        two builds of one commit could start from two interpreters and two versions of pip. The
        development image tests in the base image of the production image. test_tech_stack.py
        checks that the release is the one that mise.toml pins."""
        development = (HERE / "Dockerfile.development").read_text(encoding="utf-8")
        bases = [re.findall(r"(?m)^FROM\s+(.+)$", text) for text in (self.text, development)]
        self.assertEqual(len(bases[0]), 1, bases[0])
        self.assertRegex(bases[0][0], r"^python:[\d.]+-slim@sha256:[0-9a-f]{64}$")
        self.assertEqual(bases[1], bases[0])
        self.assertNotIn("ARG PYTHON_VERSION", self.text + development)

    def test_the_binaries_that_the_pre_checks_require_are_installed(self):
        """The Dockerfile runs install_tools.sh, which links the programs that mise installs
        into /usr/local/bin. Those programs are the binaries that the runner's pre-checks
        require, so that a run in the image finds each of them."""
        from agentcontext import runner
        script = (HERE / "install_tools.sh").read_text(encoding="utf-8")
        loop = re.search(r"(?m)^for tool in ([^;\n]+); do\n\s+ln -sf .*/usr/local/bin/", script)
        self.assertIsNotNone(loop, "install_tools.sh links no programs into /usr/local/bin")
        self.assertRegex(self.text, r"(?m)^RUN sh /tmp/tools/install_tools\.sh /tmp/tools\b")
        self.assertEqual(set(loop.group(1).split()), set(runner.BINARY_REQUIREMENTS))

    def _install_tools(self, pin: str) -> tuple:
        """Run install_tools.sh over a mise.toml that pins `pin`, with a `python3` first on PATH
        that runs this interpreter and exits 3 at the download of mise, and an x86-64 `uname`.
        Returns the result and whether the script reached the download."""
        with tempfile.TemporaryDirectory() as tmp:
            tools, bin_dir = Path(tmp) / "tools", Path(tmp) / "bin"
            tools.mkdir()
            bin_dir.mkdir()
            (tools / "mise.toml").write_text(f'[tools]\npython = "{pin}"\n', encoding="utf-8")
            download = Path(tmp) / "download.log"
            (bin_dir / "python3").write_text(
                "#!/bin/sh\n"
                f'case "$*" in *urlretrieve*) echo "$*" > "{download}"; exit 3 ;; esac\n'
                f'exec "{sys.executable}" "$@"\n', encoding="utf-8")
            (bin_dir / "uname").write_text("#!/bin/sh\necho x86_64\n", encoding="utf-8")
            for stub in bin_dir.iterdir():
                stub.chmod(0o755)
            result = subprocess.run(["sh", str(HERE / "install_tools.sh"), str(tools)],
                                    env=dict(os.environ,
                                             PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}"),
                                    capture_output=True, text=True, timeout=60)
            return result, download.exists()

    def test_the_build_fails_when_the_base_image_runs_another_release_of_python(self):
        """Docker pulls a base image by its digest without reading its tag, so a Dockerfile whose
        tag names the release of mise.toml beside the digest of another release builds from the
        other release. install_tools.sh, which both Dockerfiles run before they install
        anything, then fails and installs nothing."""
        development = (HERE / "Dockerfile.development").read_text(encoding="utf-8")
        for text in (self.text, development):
            self.assertRegex(text, r"(?m)^RUN sh /tmp/tools/install_tools\.sh /tmp/tools\b")
        result, downloaded = self._install_tools("3.14.999")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"the base image runs Python {platform.python_version()}, and mise.toml "
                      f"pins 3.14.999", result.stderr)
        self.assertFalse(downloaded)
        result, downloaded = self._install_tools(platform.python_version())
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertTrue(downloaded)

    def test_every_stages_requirements_are_installed_from_the_dependency_lock_of_the_image(self):
        """The image runs the five stages. The Dockerfile installs locks/pipeline.txt, the
        dependency lock of every requirements file that a stage has and of the build backend,
        after locks/build.txt and in hash-checking mode, and then the editable package alone,
        built with that build backend."""
        joined = re.sub(r"\\\n\s*", " ", self.text)
        commands = [" ".join(found.group(1).split()) for part in re.split(r"&&|\n", joined)
                    for found in [re.search(r"\bpip install (.*)", part)] if found]
        self.assertEqual(commands, [
            "--no-cache-dir --require-hashes -r /opt/pipeline/locks/build.txt",
            "--no-cache-dir --require-hashes --no-build-isolation "
            "-r /opt/pipeline/locks/pipeline.txt",
            "--no-cache-dir --no-deps --no-build-isolation -e /opt/pipeline"])
        # The requirements files are the words of the uv command in the header of the dependency
        # lock that follow no option.
        header = (ROOT / "locks" / "pipeline.txt").read_text(encoding="utf-8").splitlines()[1]
        words = header.split()
        inputs = {word for before, word in zip(words, words[1:])
                  if word.endswith("requirements.txt") and not before.startswith("--")}
        stages = {"build-requirements.txt", "1_sampling/requirements.txt",
                  "2_classification/requirements.txt", "agentcontext/requirements.txt"}
        stages |= {path.relative_to(ROOT).as_posix()
                   for path in (ROOT / "4_data-analysis").glob("*/requirements.txt")}
        self.assertEqual(inputs, stages)

    def test_the_image_contains_the_code_apart_from_the_mount_targets(self):
        self.assertIn("COPY . /opt/pipeline/", self.text)
        self.assertIn("mkdir -p /app/data /app/handover", self.text)
        self.assertIn('ENTRYPOINT ["python3", "/opt/pipeline/deploy/entrypoint.py"]',
                      self.text)
        self.assertIn("WORKDIR /app/data", self.text)

    def test_no_bytecode_is_written_beside_the_scripts(self):
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.text)


class TestTheDevelopmentDockerfile(unittest.TestCase):

    def setUp(self):
        self.text = (HERE / "Dockerfile.development").read_text(encoding="utf-8")

    def test_the_image_installs_the_development_lock(self):
        """The development image has the distributions of locks/development.txt, which a
        development machine installs into its .venv, after locks/build.txt and in hash-checking
        mode."""
        joined = re.sub(r"\\\n\s*", " ", self.text)
        commands = [" ".join(found.group(1).split()) for part in re.split(r"&&|\n", joined)
                    for found in [re.search(r"\bpip install (.*)", part)] if found]
        self.assertEqual(commands, [
            "--no-cache-dir --require-hashes -r /locks/build.txt",
            "--no-cache-dir --require-hashes --no-build-isolation -r /locks/development.txt"])

    def test_the_build_script_sends_every_file_that_the_dockerfile_copies(self):
        script = (HERE / "build_development_image.sh").read_text(encoding="utf-8")
        sent = set(re.search(r"(?m)^for path in ([^;]+); do$", script).group(1).split())
        copied = {source for line in re.findall(r"(?m)^COPY (.+)$", self.text)
                  for source in line.split()[:-1]}
        self.assertEqual(copied, sent)


@unittest.skipIf(shutil.which(entrypoint.SEVEN_ZIP) is None, "7zz not on PATH")
class TestTheEntrypointMain(unittest.TestCase):
    """`main` over a project folder in a temporary directory, with the runner stubbed.

    The stub records the arguments that the runner would have been started with and writes the
    pipeline execution state, as a runner does.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.app = self.tmp / "app"
        self.data = self.app / entrypoint.DATA
        self.stage = self.app / entrypoint.STAGE
        for name in entrypoint.MOUNTS:
            (self.app / name).mkdir(parents=True)
        # What an earlier run left in the data root.
        (self.stage / "output").mkdir(parents=True)
        (self.stage / "output" / "repos.csv").write_text("repo_name\no/r\n", encoding="utf-8")
        (self.stage / "data" / "collected" / "o§r").mkdir(parents=True)
        (self.stage / "data" / "collected" / "o§r" / "CLAUDE.md").write_text("# r\n")
        # The record that the first start of this image over the data root wrote.
        _record_revision(self.data, entrypoint.DATA_ROOT_REVISION)
        self.image = self.tmp / "image"
        (self.image / "heuristics").mkdir(parents=True)
        for name in ("config_path_patterns.csv", "not_ai_commits.csv"):
            (self.image / "heuristics" / name).write_text(f"{name}\n", encoding="utf-8")
        _mount_on_devices(self, self.app)
        for name, value in (("APP_ROOT", self.app), ("IMAGE_ROOT", self.image),
                            ("CREDENTIALS_FILE", self.app / "credentials.csv")):
            patcher = mock.patch.object(entrypoint, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("TMPDIR", "AGENTCONTEXT_DATA_ROOT", "TZ"):
            os.environ.pop(name, None)
        # The container that the shipped compose.yaml defines, with its revision and with the
        # image's own zone files, in which UTC is UTC, rather than the zone files of the machine
        # that runs the test.
        os.environ[entrypoint.COMPOSE_REVISION_VARIABLE] = _compose_revision()
        for name in ("LOCALTIME", "UTC_ZONE_FILE"):
            patcher = mock.patch.object(entrypoint, name, _zone_file(
                self.tmp / "image-zoneinfo" / name, "UTC0", 0, "UTC"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.started = []
        # The clock that the decision on the next monthly run reads is set late in a month. The
        # age of a GHS download (`_retrieval`) then decides whether the next month is due. A
        # download younger than the day of the month is of the same month, and an older download
        # is of the month before. A test moves the clock as the days of a long run do. The
        # months are those of UTC, because the test puts a zone file of UTC at the path where
        # compose.yaml mounts the NAS's zone file.
        self.now = entrypoint.datetime(2026, 11, 25, 12, tzinfo=entrypoint.timezone.utc)
        _use_host_zone(self, _zone_file(self.tmp / "host-localtime", "UTC0", 0, "UTC"))
        clock = mock.patch.object(entrypoint, "utc_now", lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        stop = mock.patch.object(entrypoint, "_stop_signal", 0)
        stop.start()
        self.addCleanup(stop.stop)

    def _runner(self, code: int, outcome: "str | None", steps: "list | None" = None):
        """A `run_runner` stub that records its start and writes the pipeline execution state,
        as a runner does, and the sample list that the classification writes. `steps` are the
        statuses of the steps that the state records."""
        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({
                "command": command, "cwd": Path(cwd),
                "TMPDIR": os.environ.get("TMPDIR"),
                "AGENTCONTEXT_DATA_ROOT": os.environ.get("AGENTCONTEXT_DATA_ROOT"),
                "machine": (self.data / entrypoint.MACHINE_RECORD).is_file()})
            if outcome is not None:
                (self.data / "run_logs").mkdir(exist_ok=True)
                state = {"outcome": outcome}
                if steps is not None:
                    state["steps"] = [{"name": f"step{index}", "status": status}
                                      for index, status in enumerate(steps)]
                (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state),
                                                               encoding="utf-8")
                (self.data / "classification").mkdir(exist_ok=True)
                (self.data / "classification" / "repos_annotated_included.csv").write_text(
                    "repo_name\no/r\n")
            return code
        return run_runner

    def _main(self, code: int, outcome: "str | None", argv=("--with-commit-reclassification",),
              steps: "list | None" = None) -> int:
        with mock.patch.object(entrypoint, "run_runner", self._runner(code, outcome, steps)), \
                contextlib.redirect_stderr(io.StringIO()):
            return entrypoint.main(list(argv))

    def _logged_main(self, code: int, outcome: str, steps: "list | None" = None) -> tuple:
        """`_main`, and what the entrypoint logged."""
        with mock.patch.object(entrypoint, "run_runner", self._runner(code, outcome, steps)), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            returned = entrypoint.main(["--with-commit-reclassification"])
        return returned, err.getvalue()

    def test_a_start_that_skipped_every_step_packs_no_second_copy_of_the_data_root(self):
        """A Start after a completed run, such as the Start that loads an image that changes
        nothing that a step depends on, runs no step. The handover of the completed run contains
        the data root as it is, and a second copy would take the space of a whole handover
        again."""
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["ok", "ok"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        first = self._handovers()
        self.assertEqual(len(first), 1, first)
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success",
                                                              ["skipped", "skipped"])), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            returned = entrypoint.main(["--with-commit-reclassification"])
        said = err.getvalue()
        self.assertEqual(returned, 0)
        self.assertEqual(self._handovers(), first)
        self.assertIn("Every step was already complete, and no handover was packed", said)
        self.assertIn(first[0], said)

    def _runner_writing_the_state(self, code: int, outcome: str, statuses: list,
                                  names: "list | None" = None, dropped: tuple = (),
                                  running_step: "str | None" = None):
        """A `run_runner` stub that writes the pipeline execution state with the runner's own
        `write_execution_state`, over the state that the previous start left, as the runner
        does. The state has one step result per status, named by `names` or numbered. Each step
        that is `ok` is recorded as completed. The steps of `dropped` are taken out of that record
        first, as the runner takes out the steps that `--from` names, and `running_step` names
        the step that a kill ended."""
        from agentcontext import runner
        names = names or [f"step{index}" for index in range(len(statuses))]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            saved = (runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH)
            runner.use_log_dir(self.data / "run_logs")
            try:
                state = runner.read_state()
                completed = state.setdefault("completed", {})
                for name in dropped:
                    completed.pop(name, None)
                completed.update({name: "recorded" for name, status in zip(names, statuses)
                                  if status == "ok"})
                # When the cloning step runs, its `--fresh` first removes the list of finished
                # repositories, and the step then writes a new list.
                fresh = any(word.startswith("repository_cloning:")
                            and "--fresh" in word.partition(":")[2].split() for word in command)
                if fresh and (running_step == "repository_cloning" or any(
                        name == "repository_cloning" and status != "skipped"
                        for name, status in zip(names, statuses))):
                    listed = self.data / entrypoint.FINISHED_LIST
                    listed.parent.mkdir(parents=True, exist_ok=True)
                    listed.unlink(missing_ok=True)
                    listed.write_text('{"repo": "a new collection"}\n', encoding="utf-8")
                runner.write_execution_state(
                    state, [{"name": name, "status": status, "duration_seconds": 0.0}
                            for name, status in zip(names, statuses, strict=True)],
                    outcome, time.time(), ["run_pipeline.py"], running_step=running_step)
            finally:
                runner.LOG_DIR, runner.LOCK_PATH, runner.STATE_PATH = saved
            return code
        return run_runner

    def _starts(self, *starts):
        """A `run_runner` stub. Its nth call records its command and the archives in the handover
        directory, and writes the pipeline execution state of the nth of `starts` with the
        runner's own `write_execution_state`. Each start is (code, outcome, statuses) or (code,
        outcome, statuses, names)."""
        stubs = [self._runner_writing_the_state(*start) for start in starts]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({"command": command, "handovers": self._handovers()})
            return stubs[len(self.started) - 1](command, cwd, drain_seconds)
        return run_runner

    def _killed_run(self, retrieval_age_days: int) -> None:
        """The data root of a run that a kill ended during a step, with the outcome `running`,
        over a GHS retrieval taken `retrieval_age_days` days ago."""
        (self.data / "run_logs").mkdir(exist_ok=True)
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(
            {"outcome": "running", entrypoint.LAST_RUN_OUTCOME: "running"}), encoding="utf-8")
        self._retrieval(retrieval_age_days)

    def _retrieval(self, age_days: int) -> None:
        """The record of a GHS download that took a retrieval `age_days` days before the clock
        of the test."""
        taken = (self.now - timedelta(days=age_days)).strftime("%Y-%m-%dT%H%M%SZ")
        (self.data / "sampling").mkdir(exist_ok=True)
        (self.data / entrypoint.RETRIEVAL_REPORT).write_text(json.dumps(
            {"outcome": "retrieved", "retrieval_id": taken}), encoding="utf-8")

    def _started_with(self) -> list:
        """The arguments of each start of the runner, without the interpreter and the script."""
        return [start["command"][len(entrypoint.runner_argv([])):] for start in self.started]

    def test_a_resumed_run_that_completes_over_an_old_retrieval_begins_the_next_month(self):
        """A reboot of the machine during a collection leaves the outcome `running`, and the next
        monthly Start resumes that run. If that Start ran the runner only once, it would only
        finish the old month, and the new retrieval would wait for the Start after it. The next
        run replaces the data root of the resumed run. The handover of the resumed run is packed
        before the next run starts, and the handover of the next run after that run ends."""
        self._later_seconds()
        self._killed_run(40)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((0, "success", ["ok"]), (0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(),
                         [["--with-commit-reclassification"],
                          ["--with-commit-reclassification", "--from", "ghs_download",
                           "--from", "repository_cloning"]])
        self.assertIn("Beginning the next monthly run", err.getvalue())
        resumed = self.started[1]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in resumed], ["complete.7z"], resumed)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(resumed[0]))
        handovers = self._handovers()
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["complete.7z", "complete.7z"], handovers)
        self.assertEqual(handovers[0], resumed[0])

    def test_a_resumed_run_whose_handover_was_not_packed_does_not_begin_the_next_month(self):
        """The next run would replace the data root of the resumed run, which no handover would
        then contain."""
        self._later_seconds()
        self._killed_run(40)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((0, "success", ["ok"]), (0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "_run_7z", return_value=(2, "Error: no space")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(self._started_with(), [["--with-commit-reclassification"]])
        self.assertIn("The next monthly run does not begin, because it would replace the data "
                      "root of that run", err.getvalue())

    def test_a_stop_while_the_resumed_run_finishes_begins_no_next_month(self):
        """The runner receives the Stop after its last step and exits 0. If a next monthly run
        started, Docker would kill it at the end of the grace period, with no handover, and would
        kill 7zz over the whole data root of the resumed run too. The first start runs a real
        child that sends the Stop to this process, as Docker does, and exits 0 once `run_runner`
        has passed the signal on to the child."""
        self._later_seconds()
        self._killed_run(40)
        child = ("import os, signal, sys, time\n"
                 "received = []\n"
                 "signal.signal(signal.SIGTERM, lambda signum, _frame: received.append(signum))\n"
                 "time.sleep(0.2)\n"
                 "os.kill(int(sys.argv[1]), signal.SIGTERM)\n"
                 "deadline = time.monotonic() + 10\n"
                 "while not received and time.monotonic() < deadline:\n"
                 "    time.sleep(0.01)\n"
                 "sys.exit(0 if received else 5)\n")
        finish = self._runner_writing_the_state(0, "success", ["ok"])
        real_run_runner = entrypoint.run_runner

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({"command": command})
            code = 0
            if len(self.started) == 1:
                code = real_run_runner([sys.executable, "-c", child, str(os.getpid())], cwd,
                                       drain_seconds)
            finish(command, cwd, drain_seconds)
            return code
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [["--with-commit-reclassification"]])
        self.assertIn(f"A Stop (signal {signal.SIGTERM:d}) arrived during this Start. The next "
                      f"monthly run does not begin.", err.getvalue())
        handovers = self._handovers()
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["complete-without-sample.7z"], handovers)

    def test_a_kill_in_the_first_step_that_ran_is_resumed_and_begins_the_next_month(self):
        """A Start within the month, such as the Start that loads an image with another git,
        runs the cloning step again over a completed run, and a reboot kills the runner in that
        step. The Start at boot-up resumes the run. The run completes in a later calendar month
        than its retrieval, and the Start then begins the next monthly run. The first Start runs
        the real runner with a step that kills it, and the second Start runs a stub that ages the
        retrieval, as a long collection does."""
        self._later_seconds()
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        self._retrieval(20)
        from agentcontext import runner
        os.environ["PYTHONPATH"] = str(Path(runner.__file__).resolve().parents[1])
        (self.image / "run_pipeline.py").write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            "from agentcontext import runner\n"
            "kill = 'import os, signal; os.kill(os.getppid(), signal.SIGKILL)'\n"
            "step = runner.Step('long', sys.executable, ['-c', kill], root=os.getcwd())\n"
            "raise SystemExit(runner.run([step], argv=sys.argv, log_dir=Path.cwd() / 'run_logs',\n"
            "                            min_free_gb=0))\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                             128 + signal.SIGKILL)
        stubs = [self._runner_writing_the_state(0, "success", ["ok"]) for _ in range(2)]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({"command": command})
            if len(self.started) == 1:
                self._retrieval(30)
            return stubs[len(self.started) - 1](command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(),
                         [["--with-commit-reclassification"],
                          ["--with-commit-reclassification", "--from", "ghs_download",
                           "--from", "repository_cloning"]])

    NEXT_MONTH = ["--with-commit-reclassification", "--from", "ghs_download",
                  "--from", "repository_cloning"]

    def test_a_completed_run_whose_month_becomes_due_during_the_run_begins_the_next_month(self):
        """A Start within the month over a completed run, such as the Start that loads a new
        image that collects again, runs past the first of the next month. Docker ignores the
        scheduled Start of that day while the container runs. The start decides again when its
        run completes, and the month begins in the same start rather than a month later. Each
        stub moves the clock as the days of a long run do, and the second stub moves it into a
        later month again. That stub takes no new GHS retrieval, and the start therefore begins
        no other monthly run."""
        self._later_seconds()
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        self._retrieval(20)
        stubs = [self._runner_writing_the_state(0, "success", ["ok"]) for _ in range(2)]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({"command": command, "handovers": self._handovers()})
            self.now += timedelta(days=40 if len(self.started) == 2 else 10)
            return stubs[len(self.started) - 1](command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(),
                         [["--with-commit-reclassification"], self.NEXT_MONTH])
        self.assertIn("Beginning the next monthly run", err.getvalue())
        handovers = self._handovers()
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["complete.7z", "complete.7z"], handovers)
        self.assertEqual(self.started[1]["handovers"], handovers[:1])

    def test_a_monthly_run_that_ends_in_a_later_month_begins_that_month(self):
        """The scheduled Start on the first of a month was refused, and you repaired the cause
        late in that month. The Start that you make then begins the month's run, which passes the
        first of the next month. Docker ignores the scheduled Start of that day while the
        container runs. The start decides again when the monthly run completes and begins the
        next month's run, as it does after any other run. Each stub takes a GHS retrieval at the
        clock of the test when it starts and moves the clock as the days of the run do. The
        first stub moves it ten days, into the next month, and the second one day, after which
        the next month is not due."""
        self._later_seconds()
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        self._retrieval(30)
        stubs = [self._runner_writing_the_state(0, "success", ["ok"]) for _ in range(3)]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({"command": command, "handovers": self._handovers()})
            self._retrieval(0)
            self.now += timedelta(days=10 if len(self.started) == 1 else 1)
            return stubs[len(self.started) - 1](command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH, self.NEXT_MONTH])
        self.assertEqual(err.getvalue().count("Beginning the next monthly run"), 2)
        handovers = self._handovers()
        # The handovers are the data root of the run before the first monthly run, which no
        # record names, and the handover of each monthly run. The handover of the first monthly
        # run was packed before the second run began.
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["complete.7z", "complete.7z", "complete.7z"], handovers)
        self.assertEqual(self.started[1]["handovers"], handovers[:2])

    def test_a_monthly_run_that_took_no_new_retrieval_begins_no_other(self):
        """A monthly run can pass the first of a later month and leave the record of the GHS
        download as it was. Another monthly run over the same record would follow, and another
        after that, each packing the data root, until a Stop. The start begins no other monthly
        run and says why, and the next Start decides again."""
        self._completed_run_over_an_old_retrieval()
        stubs = [self._runner_writing_the_state(0, "success", ["ok"]) for _ in range(3)]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append({"command": command})
            self.now += timedelta(days=10)
            return stubs[len(self.started) - 1](command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        self.assertIn(f"WARNING entrypoint: The next month is due, but the monthly run that just "
                      f"ended left the record of its GHS download, /app/{entrypoint.DATA}/"
                      f"{entrypoint.RETRIEVAL_REPORT}, unchanged. No other monthly run begins "
                      f"before the next Start.", err.getvalue())
        self.assertEqual(len(self._handovers()), 2, self._handovers())

    def test_a_container_from_a_compose_file_of_another_revision_runs_nothing(self):
        """A project created from an earlier bundle keeps its compose.yaml when you import a new
        image and build the project. Such a compose.yaml can lack the mount of the NAS's zone
        file, and the new image would then compare months in UTC and skip one. The start refuses
        with one ERROR line that names the file to copy. It refuses before any step, and before
        it packs the data root of a completed run that the next monthly run would replace."""
        self._completed_run_over_an_old_retrieval()
        for given in (None, "0"):
            with self.subTest(given=given):
                if given is None:
                    os.environ.pop(entrypoint.COMPOSE_REVISION_VARIABLE)
                else:
                    os.environ[entrypoint.COMPOSE_REVISION_VARIABLE] = given
                returned, said = self._logged_main(0, "success", ["ok"])
                self.assertEqual(returned, 1)
                self.assertEqual(self.started, [])
                self.assertEqual(self._handovers(), [])
                self.assertFalse((self.data / entrypoint.MACHINE_RECORD).exists())
                errors = [line for line in said.splitlines() if line.startswith("ERROR")]
                self.assertEqual(len(errors), 1, said)
                self.assertIn(f"requires revision {entrypoint.COMPOSE_REVISION}", errors[0])
                self.assertIn("Copy compose.yaml and deploy.md from the bundle of the same build",
                              errors[0])

    def test_a_container_whose_own_local_time_is_not_utc_runs_nothing(self):
        """The cloning step writes the commit times under TZ=UTC with a Z. A compose.yaml that
        mounts the NAS's zone file over /etc/localtime puts that zone into the zone file of UTC.
        Every commit time would then be the NAS's local time although it reads as UTC. A TZ
        variable changes the local time of every step. In this test, the image's zone files come
        from a directory in which UTC is the zone of Europe/Berlin."""
        self._completed_run_over_an_old_retrieval()
        berlin = _berlin_file(self.tmp / "nas-zoneinfo" / "UTC")
        for name in ("LOCALTIME", "UTC_ZONE_FILE", "TZ"):
            with self.subTest(name=name), contextlib.ExitStack() as stack:
                if name == "TZ":
                    self.addCleanup(time.tzset)
                    stack.enter_context(mock.patch.dict(os.environ, TZ=BERLIN))
                    cause = f"sets TZ={BERLIN}, which gives the offset UTC+01:00 on 2026-01-01"
                else:
                    stack.enter_context(mock.patch.object(entrypoint, name, berlin))
                    cause = f"{berlin} gives the offset UTC+0"
                returned, said = self._logged_main(0, "success", ["ok"])
                self.assertEqual(returned, 1)
                self.assertEqual(self.started, [])
                self.assertEqual(self._handovers(), [])
                errors = [line for line in said.splitlines() if line.startswith("ERROR")]
                self.assertEqual(len(errors), 1, said)
                self.assertIn(cause, errors[0])

    def test_a_tz_of_utc_runs_and_a_tz_of_another_zone_does_not(self):
        """`TZ=UTC` and `TZ=Etc/UTC` give the steps the same local time as no TZ, and a Start
        under either runs. A zone with offset zero in January and daylight saving time in July,
        as Europe/London has, gives another local time, and so does a zone file of Berlin named
        by its path. A Start under either runs no step."""
        self.addCleanup(time.tzset)
        berlin = _berlin_file(self.tmp / "zones" / "Berlin")
        for value, offset in (("UTC", None), ("Etc/UTC", None),
                              ("GMT0BST,M3.5.0/1,M10.5.0", "UTC+01:00 on 2026-07-01"),
                              (str(berlin), "UTC+01:00 on 2026-01-01")):
            with self.subTest(TZ=value), mock.patch.dict(os.environ, TZ=value):
                self.started.clear()
                returned, said = self._logged_main(0, "success", ["ok"])
                errors = [line for line in said.splitlines() if line.startswith("ERROR")]
                if offset is None:
                    self.assertEqual((returned, len(self.started), errors), (0, 1, []), said)
                else:
                    self.assertEqual((returned, self.started, len(errors)), (1, [], 1), said)
                    self.assertIn(f"sets TZ={value}, which gives the offset {offset}", errors[0])

    # The pipeline execution state that the first start of the first image left on the NAS, where
    # the run failed in the labeling. That image writes no record of the data root's revision, and
    # its cache of GHS retrievals has index columns other than the columns that this image reads.
    STATE_OF_27F638B2 = {
        "completed": {
            "ghs_download":
                "output:43dfb14104bf8f2fa4d827e6f7541ca5c234c046f0a5f80e56a4772d142bd7e5",
            "github_query":
                "inputs:d8cf29085668ac019d274e9dfc513aa9fcb3a0635bae00cdcbc8451bb92c0e1a",
            "retrieval_comparison":
                "inputs:6eb2b47f4aa24a9b3398c1f259cf59c393587c4c744a17192c19fb101ec65aed",
            "repository_filtering":
                "inputs:8442af2c23f45361ebf43c1cc591aea2c14cf302bc5cb46c648fb1edbd86804d"},
        "git_sha": "27f638b28af97ddd93302cc135f59a0ba23a7c64", "git_dirty": False,
        "command": "/usr/local/bin/python3 /opt/pipeline/run_pipeline.py "
                   "--with-commit-reclassification",
        "started_at": "2026-10-16T17:58:39.731501+00:00",
        "finished_at": "2026-10-17T23:23:20.306397+00:00", "duration_seconds": 105880.6,
        "outcome": "failed", "last_run_outcome": "failed", "hostname": "99e295526b99",
        "platform": "Linux-4.4.302+-x86_64-with-glibc2.41", "python_version": "3.14.8",
        "stage_interpreter": "/usr/local/bin/python3",
        "stage_packages": {"pandas": "3.0.5", "numpy": "2.5.3", "scipy": "1.18.1"},
        "steps": [
            {"name": "ghs_download", "status": "ok", "duration_seconds": 936.3,
             "report": {"item_count": 205936, "outcome": "retrieved",
                        "previous_retrieval_id": None, "retrieval_id": "2026-10-16T175934Z",
                        "window_moved": True}},
            {"name": "github_query", "status": "ok", "duration_seconds": 6061.7},
            {"name": "retrieval_comparison", "status": "ok", "duration_seconds": 19.5},
            {"name": "repository_filtering", "status": "ok", "duration_seconds": 37.8},
            {"name": "repository_labeling", "status": "failed", "duration_seconds": 98771.0}]}

    def _project_folder(self) -> dict:
        """Every entry under the project folder, a file by its bytes and a directory by None."""
        return {path.relative_to(self.app).as_posix(): path.read_bytes() if path.is_file() else None
                for path in sorted(self.app.rglob("*"))}

    def _refused_for_the_revision(self, found: str) -> None:
        """A start over the data root starts no runner and logs one ERROR line that names `found`
        and a clean run. It changes nothing in the project folder, that is, it removes nothing
        that killed processes left and writes no archive, no machine record and no record of the
        revision."""
        clone = self.app / entrypoint.SCRATCH / "o§r"
        clone.mkdir(parents=True, exist_ok=True)
        (clone / "README.md").write_text("a clone in progress\n", encoding="utf-8")
        before = self._project_folder()
        self.started.clear()
        returned, said = self._logged_main(0, "success", ["ok"])
        self.assertEqual(returned, 1)
        self.assertEqual(self.started, [])
        self.assertEqual(self._project_folder(), before)
        errors = [line for line in said.splitlines() if line.startswith("ERROR")]
        self.assertEqual(len(errors), 1, said)
        self.assertIn(f"ERROR entrypoint: The data root /app/{entrypoint.DATA} {found}", errors[0])
        self.assertIn(f". This image reads only the stored formats of data root revision "
                      f"{entrypoint.DATA_ROOT_REVISION} and converts only a data root of "
                      f"revision 1 or 2 to them. No step ran, and nothing changed.", errors[0])
        self.assertIn("begin a clean run in an empty project folder, as the section \"A clean "
                      "run\" of deploy.md describes.", errors[0])

    def test_a_data_root_that_an_image_without_the_revision_began_runs_nothing(self):
        """The first run on the NAS failed in the labeling. If this image resumed
        that run, the query of GitHub would stop at every Start on the index columns of the cache
        of retrievals, with a message that names the columns and not the remedy. Every Start
        would then pack another `-failed` archive and begin no month. The start refuses before it
        removes, packs or writes anything."""
        (self.data / entrypoint.DATA_ROOT_RECORD).unlink()
        (self.data / entrypoint.STATE_FILE).write_text(
            json.dumps(self.STATE_OF_27F638B2, indent=2) + "\n", encoding="utf-8")
        self._retrieval(30)
        self._refused_for_the_revision(
            f"has a pipeline execution state ({entrypoint.STATE_FILE}) and no record of its "
            f"revision ({entrypoint.DATA_ROOT_RECORD}). An image that writes no such record "
            f"began it")

    def test_a_data_root_of_another_revision_runs_nothing(self):
        """A record that names another revision, or that cannot be read, names no stored format
        that this image reads. The start refuses also over a completed run when the next month
        is due and the start would otherwise pack the run's data root first."""
        self._completed_run_over_an_old_retrieval()
        record = self.data / entrypoint.DATA_ROOT_RECORD
        for written, found in (
                (json.dumps({"revision": entrypoint.DATA_ROOT_REVISION + 1}),
                 f"is of data root revision {entrypoint.DATA_ROOT_REVISION + 1} "
                 f"({entrypoint.DATA_ROOT_RECORD})"),
                (json.dumps({"revision": 0}),
                 f"is of data root revision 0 ({entrypoint.DATA_ROOT_RECORD})"),
                (json.dumps({"revision": str(entrypoint.DATA_ROOT_REVISION)}),
                 f"is of data root revision '{entrypoint.DATA_ROOT_REVISION}' ("),
                ('{"revision": ', f"has a record of its revision ({entrypoint.DATA_ROOT_RECORD}) "
                                  f"that cannot be read (JSONDecodeError")):
            with self.subTest(written=written):
                record.write_text(written, encoding="utf-8")
                self._refused_for_the_revision(found)

    def test_the_revision_error_names_the_image_whose_import_resumes_the_run(self):
        """An update to an image of another data root revision runs no step. The image that
        began the data root, or another image of its revision, resumes the run. The ERROR line
        therefore names that image from the record, and the line of BUILD_INFO.txt that tells
        whether an image is of that revision, beside a clean run with this image. A record
        without the image, or with an image without a build record, names the revision alone. A
        revision that is not an integer names no image to import."""
        self._completed_run_over_an_old_retrieval()
        record = self.data / entrypoint.DATA_ROOT_RECORD
        other = entrypoint.DATA_ROOT_REVISION + 1
        image = "commit 1234567, built 2026-10-16T13:45:23Z, python 3.14"
        for written, resume in (
                ({"revision": other, "image": image},
                 f"To resume the run, import the image that began the data root ({image}) or "
                 f"another image with data_root_revision {other} in its BUILD_INFO.txt, copy "
                 f"compose.yaml and deploy.md from the bundle of its build into the project "
                 f"folder, and build the project again. To run this image instead, download "
                 f"the archives"),
                ({"revision": other},
                 f"To resume the run, import an image with data_root_revision {other} in its "
                 f"BUILD_INFO.txt, copy compose.yaml"),
                ({"revision": other, "image": entrypoint.NO_BUILD_RECORD},
                 f"To resume the run, import an image with data_root_revision {other} in its "
                 f"BUILD_INFO.txt, copy compose.yaml"),
                ({"revision": str(other), "image": image}, None)):
            with self.subTest(written=written):
                record.write_text(json.dumps(written), encoding="utf-8")
                self._refused_for_the_revision(f"is of data root revision {written['revision']!r}")
                error = [line for line in self._logged_main(0, "success", ["ok"])[1].splitlines()
                         if line.startswith("ERROR")][0]
                if resume is None:
                    self.assertNotIn("To resume the run", error)
                    self.assertIn(". Download the archives in the handover directory", error)
                else:
                    self.assertIn(resume, error)

    def test_the_first_line_of_every_start_names_the_images_data_root_revision(self):
        """The first line names the build record and the data root revision of the image, also
        at a Start that the revision of the data root refuses, so that the log shows which
        revision the image reads."""
        (self.image / entrypoint.BUILD_INFO).write_text(
            "commit abcdef0\nbuilt 2026-10-19T08:00:00Z\npython 3.14\n", encoding="utf-8")
        for revision in (entrypoint.DATA_ROOT_REVISION, entrypoint.DATA_ROOT_REVISION + 6):
            with self.subTest(revision=revision), \
                    mock.patch.object(entrypoint, "DATA_ROOT_REVISION", revision):
                said = self._logged_main(0, "success", ["ok"])[1]
                self.assertEqual(said.splitlines()[0],
                                 f"INFO entrypoint: Running the image agentcontext-pipeline "
                                 f"(commit abcdef0, built 2026-10-19T08:00:00Z, python 3.14). "
                                 f"Its data root revision is {revision}.")

    def test_the_first_start_over_a_data_root_records_its_revision_before_the_runner(self):
        """The record names the revision of the image that began the data root, also when the
        pre-checks refuse that first start, after which the data root has a pipeline
        execution state. A later start over a record of this revision runs."""
        shutil.rmtree(self.data)
        self.data.mkdir()
        (self.data / ".keep").touch()
        record = self.data / entrypoint.DATA_ROOT_RECORD
        stubs = [self._runner_writing_the_state(1, "pre_checks_failed", [], []),
                 self._runner_writing_the_state(0, "success", ["ok"])]

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            self.started.append(json.loads(record.read_text(encoding="utf-8")))
            return stubs[len(self.started) - 1](command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual([written["revision"] for written in self.started],
                         [entrypoint.DATA_ROOT_REVISION] * 2)
        self.assertEqual(self.started[0]["image"], entrypoint.build_info(self.image))
        self.assertEqual(err.getvalue().count(
            f"Recorded revision {entrypoint.DATA_ROOT_REVISION} of the new data root in "
            f"{entrypoint.DATA_ROOT_RECORD}"), 1)
        self.assertEqual(len(self._handovers()), 1, self._handovers())

    # Two decisions in the format of data root revision 1, and an exclusion, which has no `usage`.
    # The tool output contains "pages" as text, which the conversion leaves unchanged.
    DECIDED = {"id": 1, "name": "o/a", "label": "true",
               "tools": [{"tool": "read_linked_page", "arguments": {"url": "https://x.org/pages"},
                          "output": 'The "pages" of the project'}],
               "limits_reached": ["pages", "tool_rounds"],
               "usage": {"requests": 7, "tool_rounds": 5, "pages": 5, "input_tokens": 900}}
    UNLIMITED = {"id": 2, "name": "o/b", "label": "false", "limits_reached": [],
                 "usage": {"requests": 1, "tool_rounds": 0, "pages": 0, "input_tokens": 80}}
    EXCLUDED = {"id": 3, "name": "o/c", "exclusion": "no_readme"}

    @staticmethod
    def _label_cache():
        """2_classification/label_cache.py, which writes and reads the cache of labels."""
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        return _load("label_cache", ROOT / "2_classification" / "label_cache.py")

    @staticmethod
    def _of_revision_2(decision: dict) -> dict:
        """`decision` with the count and the limit of the linked pages under their names of
        revision 2, written out rather than converted."""
        if "usage" not in decision:
            return decision
        usage = {("linked_pages" if key == "pages" else key): value
                 for key, value in decision["usage"].items()}
        limits = sorted("linked_pages" if limit == "pages" else limit
                        for limit in decision["limits_reached"])
        return {**decision, "usage": usage, "limits_reached": limits}

    def _write_label_cache(self, cache: Path, revision: int) -> tuple:
        """A cache of labels with a run of three decisions, its comparison with an earlier run
        and the partial file of a run in progress, written by the classification's own code,
        in the format of `revision`. Returns the label cache module and the run id."""
        label_cache = self._label_cache()
        convert = (lambda d: d) if revision == 1 else self._of_revision_2
        decisions = [convert(d) for d in (self.DECIDED, self.UNLIMITED, self.EXCLUDED)]
        count, decided = label_cache.write_decisions(cache, "2026-10-23T093036Z", decisions)
        changes = [{"id": 1, "name": "o/a", "repeat": convert(self.DECIDED),
                    "previous_input": convert(self.UNLIMITED), "cause": "model variation"}]
        _, compared = label_cache.write_changes(cache, "2026-10-23T093036Z", changes)
        label_cache.put_index_row({"run_id": "2026-10-23T093036Z", "decisions": count,
                                   "decisions_sha256": decided, "changes_sha256": compared},
                                  cache)
        partial = label_cache.partial_path(cache, "2026-10-24T000000Z")
        line = json.dumps({"id": 4, "status": "decided", "decision": convert(self.DECIDED)},
                          ensure_ascii=False, separators=(",", ":"))
        partial.write_text(line + "\n" + line[:40], encoding="utf-8")
        return label_cache, "2026-10-23T093036Z"

    def _files(self, directory: Path) -> dict:
        return {path.relative_to(directory).as_posix(): path.read_bytes()
                for path in sorted(directory.rglob("*")) if path.is_file()}

    def _revision_1_data_root(self) -> Path:
        """The data root of a completed run of revision 1, with a cache of labels of that
        revision. Returns the cache."""
        _record_revision(self.data, 1)
        cache = self.data / entrypoint.LABEL_CACHE
        self._write_label_cache(cache, 1)
        return cache

    def _expected_label_cache(self) -> dict:
        """The files of the cache of `_revision_1_data_root` as the classification of revision
        2 writes them."""
        expected = self.tmp / "expected" / "labels"
        self._write_label_cache(expected, 2)
        return self._files(expected)

    def test_a_data_root_of_revision_1_is_converted_before_the_runner_starts(self):
        """A data root of revision 1 stores the count and the limit of the linked pages as `pages`.
        The start converts every decision of the cache of labels into the bytes that the
        classification of revision 2 writes for it, records revision 2 with the image that
        converted it, and only then starts the runner. The text "pages" inside a tool output stays,
        and so does the last line of a partial file that a stop cut short. The classification reads
        the converted cache by the checksums of its converted index."""
        cache = self._revision_1_data_root()
        revisions = []
        runner = self._runner(0, "success", ["ok"])

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            revisions.append(json.loads((self.data / entrypoint.DATA_ROOT_RECORD).read_text()))
            return runner(command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._files(cache), self._expected_label_cache())
        self.assertEqual(revisions[0]["revision"], entrypoint.DATA_ROOT_REVISION)
        self.assertEqual(revisions[0]["image"], entrypoint.build_info(self.image))
        self.assertEqual(revisions[0]["converted_from"],
                         {"revision": 1, "image": "commit 1234567"})
        label_cache = self._label_cache()
        row = label_cache.read_index(cache)[0]
        read = list(label_cache.read_decisions(cache, row["run_id"], row["decisions_sha256"]))
        self.assertEqual(read[0]["usage"], {"requests": 7, "tool_rounds": 5, "linked_pages": 5,
                                            "input_tokens": 900})
        self.assertEqual(read[0]["limits_reached"], ["linked_pages", "tool_rounds"])
        self.assertEqual(read[0]["tools"], self.DECIDED["tools"])
        changes = list(label_cache.read_changes(cache, row["run_id"], row["changes_sha256"]))
        self.assertEqual(changes[0]["previous_input"]["usage"]["linked_pages"], 0)
        names = ["2026-10-23T093036Z.decisions-001.jsonl.xz",
                 "2026-10-23T093036Z.changes-001.jsonl.xz", "2026-10-24T000000Z.partial.jsonl",
                 "index.csv"]
        self.assertIn(f"INFO entrypoint: Converted the data root /app/{entrypoint.DATA} from "
                      f"revision 1 to revision {entrypoint.DATA_ROOT_REVISION}. Rewrote 4 stored "
                      f"files ({entrypoint.series(f'{entrypoint.LABEL_CACHE}/{n}' for n in names)}"
                      f").", err.getvalue())
        self.assertFalse((self.data / entrypoint.CONVERSION_RECORD).exists())
        self.assertEqual(self._main(0, "success", steps=["ok"]), 0)
        self.assertEqual(self._files(cache), self._expected_label_cache(),
                         "a later start converts nothing again")

    def test_a_kill_during_the_conversion_leaves_what_the_next_start_converts(self):
        """A Stop that outlasts the grace period or a power loss can end a start at any point of
        the conversion. The original files stay until every converted file is written and the
        record of the conversion names them. The next start finishes the renames that the
        record names, or converts again, and records revision 2 only once every file is
        converted."""
        class Killed(BaseException):
            pass

        def kill_at_record(path: str):
            real = entrypoint.write_record

            def write_record(target, record):
                if Path(target) == self.data / path:
                    raise Killed()
                return real(target, record)
            return mock.patch.object(entrypoint, "write_record", write_record)

        def kill_after_first_rename():
            real = entrypoint._replace_converted

            def replace(data, names):
                real(data, names[:1])
                raise Killed()
            return mock.patch.object(entrypoint, "_replace_converted", replace)

        for name, kill in (("before the record of the conversion",
                            lambda: kill_at_record(entrypoint.CONVERSION_RECORD)),
                           ("after the first rename", kill_after_first_rename),
                           ("before the record of the revision",
                            lambda: kill_at_record(entrypoint.DATA_ROOT_RECORD))):
            with self.subTest(kill=name):
                shutil.rmtree(self.data / "classification", ignore_errors=True)
                (self.data / entrypoint.CONVERSION_RECORD).unlink(missing_ok=True)
                cache = self._revision_1_data_root()
                self.started.clear()
                with kill(), self.assertRaises(Killed):
                    self._main(0, "success", steps=["ok"])
                self.assertEqual(self.started, [])
                self.assertEqual(json.loads((self.data / entrypoint.DATA_ROOT_RECORD)
                                            .read_text())["revision"], 1)
                self.assertEqual(self._main(0, "success", steps=["ok"]), 0)
                self.assertEqual(len(self.started), 1)
                self.assertEqual(self._files(cache), self._expected_label_cache())
                self.assertEqual(json.loads((self.data / entrypoint.DATA_ROOT_RECORD)
                                            .read_text())["revision"],
                                 entrypoint.DATA_ROOT_REVISION)
                self.assertFalse((self.data / entrypoint.CONVERSION_RECORD).exists())

    def test_a_start_that_the_pre_checks_refused_stays_refused_after_the_conversion(self):
        """The runner of revision 1 recorded a start that its pre-checks refused with the
        outcome `preflight_failed`, which revision 2 calls `pre_checks_failed`. Read under the
        earlier name, the refused start would count as a run that used its `--from`, because the
        state keeps the steps of the last run that ran steps, and the next start would leave the
        `--from` out although no run used it."""
        _record_revision(self.data, 1)
        state = {"outcome": "preflight_failed", "completed": {"step0": "recorded"},
                 "steps": [{"name": "step0", "status": "ok"}]}
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state), encoding="utf-8")
        before = ["the signature before the refused start"]
        self.assertTrue(entrypoint.took_effect(["--from", "step0"], self.data, before),
                        "the earlier name reads as a run that used the argument")
        converted = entrypoint.convert_data_root(self.data)
        self.assertIn(f"Rewrote 1 stored file ({entrypoint.STATE_FILE}).", converted)
        self.assertEqual(entrypoint.read_state(self.data),
                         dict(state, outcome="pre_checks_failed"))
        self.assertFalse(entrypoint.took_effect(["--from", "step0"], self.data, before))

    def test_a_part_that_does_not_match_its_checksum_stops_the_conversion(self):
        """A part of the cache of labels that does not match the SHA-256 in the index cannot be
        told from a damaged file. The start converts nothing, starts no runner and names the
        part, and the data root stays of revision 1."""
        cache = self._revision_1_data_root()
        part = cache / "2026-10-23T093036Z.decisions-001.jsonl.xz"
        part.write_bytes(part.read_bytes() + b"\0")
        before = self._files(cache)
        returned, said = self._logged_main(0, "success", ["ok"])
        self.assertEqual(returned, 1)
        self.assertEqual(self.started, [])
        self.assertEqual(self._files(cache), before)
        self.assertIn(f"ERROR entrypoint: Could not convert the data root /app/{entrypoint.DATA} "
                      f"to revision {entrypoint.DATA_ROOT_REVISION} ({part} does not match the "
                      f"SHA-256 that {cache / 'index.csv'} records for it). No step ran.", said)
        self.assertEqual(json.loads((self.data / entrypoint.DATA_ROOT_RECORD).read_text())
                         ["revision"], 1)

    def test_the_conversion_repeats_the_format_of_the_cache_of_labels(self):
        """The entrypoint repeats the names and the compression preset of the cache of labels
        rather than importing them, and this test checks the copies against the originals."""
        label_cache = self._label_cache()
        self.assertEqual(entrypoint.LABEL_XZ_PRESET, label_cache.XZ_PRESET)
        self.assertEqual(entrypoint.LABEL_INDEX, label_cache.INDEX_NAME)
        self.assertLessEqual({"run_id", *entrypoint.LABEL_PARTS}, set(label_cache.INDEX_COLUMNS))
        cache = self.tmp / "names"
        self.assertEqual(label_cache.decisions_path(cache, "2026-10-23T093036Z", 2).name,
                         "2026-10-23T093036Z.decisions-002.jsonl.xz")
        self.assertEqual(label_cache.changes_path(cache, "2026-10-23T093036Z", 2).name,
                         "2026-10-23T093036Z.changes-002.jsonl.xz")

    def _completed_run_over_an_old_retrieval(self, recorded: "str | None" = None) -> None:
        """The data root of a run that completed over a retrieval taken 30 days ago, with a
        record that names a handover of it with the label `recorded`, or with no record."""
        self._later_seconds()
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        self._retrieval(30)
        if recorded is not None:
            state = json.loads((self.data / entrypoint.STATE_FILE).read_text(encoding="utf-8"))
            (self.data / entrypoint.HANDOVER_RECORD).write_text(json.dumps(
                {"archive": f"agentcontext-handover-20260901T000000Z-{recorded}.7z",
                 entrypoint.LAST_STEPS_FINISHED_AT: state[entrypoint.LAST_STEPS_FINISHED_AT]}),
                encoding="utf-8")

    def _packs_before_the_next_month(self, recorded: "str | None") -> None:
        """A monthly Start over the data root of `_completed_run_over_an_old_retrieval` packs the
        data root before the runner starts the next monthly run, and packs the handover of that
        run after the run ends."""
        self._completed_run_over_an_old_retrieval(recorded)
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        before = self.started[0]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(before[0]))
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()],
                         ["complete.7z", "complete.7z"], self._handovers())

    def test_a_monthly_start_packs_a_completed_run_that_has_no_recorded_handover(self):
        """The packing at the end of the completed run failed, and the next monthly run
        replaces its data root."""
        self._packs_before_the_next_month(None)

    def test_a_monthly_start_packs_a_completed_run_that_was_packed_without_the_sample(self):
        """The data root did not fit on the handover volume when the run completed, and it fits
        now."""
        self._packs_before_the_next_month("complete-without-sample")

    def test_a_monthly_start_after_a_recorded_handover_packs_nothing_first(self):
        """A second copy of the data root at every monthly Start would fill the handover
        volume."""
        self._later_seconds()
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["ok"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        packed = self._handovers()
        self._retrieval(30)
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        self.assertEqual(self.started[0]["handovers"], packed)

    def test_a_monthly_start_whose_packing_fails_starts_no_runner(self):
        """The next monthly run would replace a data root that no handover contains."""
        self._completed_run_over_an_old_retrieval()
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "_run_7z", return_value=(2, "Error: no space")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(self.started, [])
        self.assertEqual(self._handovers(), [])
        self.assertIn("because its packing failed (7zz exited 2, with the output ending in Error: "
                      "no space). The next monthly run does not begin, because it would rewrite "
                      "that data root", err.getvalue())

    def test_a_monthly_start_without_room_for_the_data_root_starts_no_runner(self):
        """The run was packed without the sample when it completed, and the handover volume
        still has no room for the data root. Another `-complete-without-sample` archive would
        contain nothing new."""
        self._completed_run_over_an_old_retrieval("complete-without-sample")
        room = shutil.disk_usage(self.app)._replace(free=1)
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                mock.patch.object(entrypoint.shutil, "disk_usage", return_value=room), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(self.started, [])
        self.assertEqual(self._handovers(), [])
        self.assertIn("because the data root does not fit on the handover volume. The next "
                      "monthly run does not begin, because it would rewrite that data root",
                      err.getvalue())

    def _state_without_the_finish_time(self, retrieval_age_days: int) -> None:
        """The pipeline execution state of a completed run as a runner that does not record
        `last_steps_finished_at` writes it, over a retrieval taken `retrieval_age_days` days
        ago."""
        (self.data / "run_logs").mkdir(exist_ok=True)
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(
            {"outcome": "success", entrypoint.LAST_RUN_OUTCOME: "success"}), encoding="utf-8")
        self._retrieval(retrieval_age_days)

    def test_a_monthly_start_over_a_state_without_the_finish_time_begins_the_month(self):
        """The record of the handover of such a state names no time either. If the start required
        the two records to name the same time, it would pack the data root, find no recorded
        handover, refuse the month, and pack the data root again at every later Start."""
        self._later_seconds()
        self._state_without_the_finish_time(30)
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        before = self.started[0]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(before[0]))
        packed = self._handovers()
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["skipped"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._handovers(), packed)

    def test_starts_that_skip_every_step_over_a_state_without_the_finish_time_pack_once(self):
        """A start that skips every step leaves the state without the time, and the Start at
        every boot-up of the month would otherwise put another copy of the data root on the
        handover volume."""
        self._later_seconds()
        self._state_without_the_finish_time(10)
        for _ in range(3):
            with mock.patch.object(entrypoint, "run_runner",
                                   self._runner_writing_the_state(0, "success",
                                                                  ["skipped", "skipped"])), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()], ["complete.7z"],
                         self._handovers())

    def test_the_archive_and_the_record_reach_the_disk_before_their_renames(self):
        """After a power loss, a record could otherwise name an archive with bytes that never
        reached the disk, and the next monthly run would replace the data root that only that
        archive contains. Each file is synced before its rename and its directory after the
        rename, and the archive's directory is synced before the record is renamed."""
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        events = []
        real_fsync, real_replace = os.fsync, os.replace

        def fsync(fd):
            events.append(("fsync", os.fstat(fd).st_ino))
            real_fsync(fd)

        def replace(source, target):
            events.append(("replace", os.stat(source).st_ino, Path(target).name))
            real_replace(source, target)
        with mock.patch.object(entrypoint.os, "fsync", fsync), \
                mock.patch.object(entrypoint.os, "replace", replace), \
                contextlib.redirect_stderr(io.StringIO()):
            handed = entrypoint.hand_over(0, False, self.data)
        self.assertTrue(handed.archive.endswith("-complete.7z"), handed)
        renamed = {}
        for name, directory in ((handed.archive, self.app / entrypoint.HANDOVER),
                                (Path(entrypoint.HANDOVER_RECORD).name, self.data / "run_logs")):
            with self.subTest(file=name):
                index = next(i for i, event in enumerate(events)
                             if event[0] == "replace" and event[2] == name)
                self.assertIn(("fsync", events[index][1]), events[:index])
                self.assertIn(("fsync", directory.stat().st_ino), events[index + 1:])
                renamed[name] = index
        archive_directory = events.index(("fsync", (self.app / entrypoint.HANDOVER).stat().st_ino))
        self.assertLess(archive_directory, renamed[Path(entrypoint.HANDOVER_RECORD).name])

    def _fsync_failing_for(self, kind: int):
        """An `os.fsync` that raises EINVAL for a descriptor of the file type `kind`, as a
        filesystem that cannot sync it does, and syncs every other descriptor."""
        real_fsync = os.fsync

        def fsync(fd):
            if stat.S_IFMT(os.fstat(fd).st_mode) == kind:
                raise OSError(errno.EINVAL, "Invalid argument")
            real_fsync(fd)
        return fsync

    def test_a_filesystem_that_cannot_sync_a_directory_still_gets_its_handover(self):
        """The data of the archive and of the record is on the disk either way. An error from
        the sync of their directory would fail every packing on such a filesystem, and no
        monthly run could begin."""
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        with mock.patch.object(entrypoint.os, "fsync", self._fsync_failing_for(stat.S_IFDIR)), \
                contextlib.redirect_stderr(io.StringIO()):
            handed = entrypoint.hand_over(0, False, self.data)
        self.assertTrue(handed.archive and handed.archive.endswith("-complete.7z"), handed)
        self.assertEqual(self._handovers(), [handed.archive])
        self.assertEqual(entrypoint.recorded_handover(self.data, "complete"), handed.archive)

    def test_an_archive_that_cannot_be_synced_is_not_a_handover(self):
        """An archive with data that may not be on the disk is not renamed, so that no record
        names it and no monthly run replaces a data root that only that archive contains."""
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        with mock.patch.object(entrypoint.os, "fsync", self._fsync_failing_for(stat.S_IFREG)), \
                contextlib.redirect_stderr(io.StringIO()):
            handed = entrypoint.hand_over(0, False, self.data)
        self.assertIsNone(handed.archive)
        self.assertIn("Invalid argument", handed.problem)
        self.assertEqual(entrypoint.archives_in(self.app / entrypoint.HANDOVER)[0], 0,
                         self._handovers())
        self.assertIsNone(entrypoint.recorded_handover(self.data, "complete"))

    def test_a_state_that_cannot_be_read_starts_no_runner(self):
        """The runner would refuse the state too, but only after the start had changed the data
        root. The start would have removed leftovers, written its machine record and decided on
        a monthly run as if no run had happened. The start keeps the damaged file for its
        recovery."""
        (self.data / "run_logs").mkdir(exist_ok=True)
        damaged = '{"completed": {"repository_cloning": "out'
        (self.data / entrypoint.STATE_FILE).write_text(damaged, encoding="utf-8")
        self._retrieval(30)
        returned, said = self._logged_main(0, "success", ["ok"])
        self.assertEqual(returned, 1)
        self.assertEqual(self.started, [])
        self.assertEqual(self._handovers(), [])
        self.assertIn(f"ERROR entrypoint: The pipeline execution state /app/{entrypoint.DATA}/"
                      f"{entrypoint.STATE_FILE} cannot be read", said)
        self.assertEqual((self.data / entrypoint.STATE_FILE).read_text(encoding="utf-8"), damaged)
        self.assertFalse((self.data / entrypoint.MACHINE_RECORD).exists())

    def test_what_killed_processes_left_is_removed_before_the_runner_starts(self):
        """A GHS download that a kill interrupted leaves its `.part` file in the cache of
        retrievals. A release that a kill ended leaves its temporary directory in the scratch
        directory, as a killed cloning step leaves its clones. No step reads these leftovers and
        nothing else removes them, and each would take free space at every later Start. A file
        of the sample with a `.part` name stays, and so does a completed retrieval."""
        retrievals = self.app / entrypoint.RETRIEVALS
        retrievals.mkdir(parents=True)
        interrupted = retrievals / "2026-10-01T000000Z.json.gz.part"
        interrupted.write_bytes(b"part of a download")
        retrieval = retrievals / "2026-09-01T000000Z.json.gz"
        retrieval.write_bytes(b"a retrieval")
        scratch = self.app / entrypoint.SCRATCH
        (scratch / "tmpab12cd34" / "release").mkdir(parents=True)
        (scratch / "tmpab12cd34" / "release" / "repos.csv").write_text("repo_name\n")
        (scratch / "o§r" / ".git").mkdir(parents=True)
        (scratch / "stray.tmp").write_text("x")
        sample = self.stage / "data" / "collected" / "o§r" / "notes.json.gz.part"
        sample.write_text("a file of the sample\n")
        seen = []
        finish = self._runner_writing_the_state(0, "success", ["ok"])

        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            seen.append((sorted(p.name for p in scratch.iterdir()), interrupted.exists()))
            return finish(command, cwd, drain_seconds)
        with mock.patch.object(entrypoint, "run_runner", run_runner), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(seen, [([], False)])
        self.assertEqual(retrieval.read_bytes(), b"a retrieval")
        self.assertTrue(sample.is_file())
        said = err.getvalue()
        self.assertIn("INFO entrypoint: Removed what killed processes left (", said)
        for name in (f"{entrypoint.RETRIEVALS}/{interrupted.name}",
                     f"{entrypoint.SCRATCH}/tmpab12cd34", f"{entrypoint.SCRATCH}/o§r",
                     f"{entrypoint.SCRATCH}/stray.tmp"):
            self.assertIn(name, said)
        self.assertNotIn(retrieval.name, said)

    def test_a_partial_archive_that_a_kill_left_does_not_take_the_room_of_the_data_root(self):
        """A reboot during the packing of a completed run leaves its `.partial` file. The next
        monthly Start packs the data root before it begins the month, when the handover volume
        has twice the data root's size free. Counted as used space, the partial file would make
        that Start pack the data root without the sample and begin no month. The volume here has
        room for the data root only without the partial file."""
        self._completed_run_over_an_old_retrieval()
        handovers = self.app / entrypoint.HANDOVER
        partial = handovers / "agentcontext-handover-20261020T030000Z-complete.7z.partial"
        partial.write_bytes(b"x" * 500)
        data_root, volume = 1000, 2200
        usage = shutil.disk_usage(self.tmp)

        def disk_usage(path):
            held = sum(p.stat().st_size for p in handovers.iterdir() if p.is_file())
            return usage._replace(free=volume - held)
        self.assertLess(volume - partial.stat().st_size, 2 * data_root)
        with mock.patch.object(entrypoint.shutil, "disk_usage", disk_usage), \
                mock.patch.object(entrypoint, "tree_bytes", lambda *args, **kwargs: data_root), \
                mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        before = self.started[0]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
        self.assertFalse(partial.exists())

    def test_a_machine_record_that_cannot_be_written_ends_the_start_with_its_cause(self):
        """On a full volume, the start fails in its own writes before the runner's pre-checks can name the cause. Each such failure logs one ERROR line that names the path and the
        error, and the start exits 1 without a runner, as after a refusal by the pre-checks,
        rather than with a traceback. In this test, a directory or a file in the way causes the
        error."""
        for blocked, make in ((self.data / entrypoint.MACHINE_RECORD, Path.mkdir),
                              (self.data / entrypoint.HEURISTICS_RECORD, Path.touch),
                              (self.app / entrypoint.SCRATCH, Path.touch)):
            with self.subTest(blocked=str(blocked.relative_to(self.app))):
                blocked.parent.mkdir(parents=True, exist_ok=True)
                shutil.rmtree(blocked, ignore_errors=True)
                make(blocked)
                self.started.clear()
                with mock.patch.object(entrypoint, "run_runner",
                                       self._runner(0, "success", ["ok"])), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
                self.assertEqual(self.started, [])
                errors = [line for line in err.getvalue().splitlines()
                          if line.startswith("ERROR entrypoint: ")]
                self.assertEqual(len(errors), 1, err.getvalue())
                self.assertIn(". No step ran. Correct the cause", errors[0])
                self.assertIn(blocked.name, errors[0])
                (blocked.rmdir if blocked.is_dir() else blocked.unlink)()

    # The steps of the runner stub below, in the pipeline's order.
    STEPS = ["ghs_download", "repository_cloning", "release", "rq2_config_cooccurrence"]
    FROM_RQ2 = ["--with-commit-reclassification", "--from", "rq2_config_cooccurrence"]

    def _runner_honoring_from(self, outcome: str = "success", code: int = 0, then=None):
        """A `run_runner` stub that, as the runner does, runs the steps of `STEPS` that a
        `--from` of its arguments names and skips the other steps. When it runs the download, it
        takes a GHS retrieval at the clock of the test. It writes the pipeline execution state
        with the runner's own `write_execution_state`, and `then` runs after each start of the
        stub."""
        def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            args = command[len(entrypoint.runner_argv([])):]
            self.started.append({"command": command, "handovers": self._handovers()})
            redone = {args[i + 1] for i in range(len(args) - 1) if args[i] == "--from"}
            if "ghs_download" in redone:
                self._retrieval(0)
            statuses = ["ok" if name in redone else "skipped" for name in self.STEPS]
            returned = self._runner_writing_the_state(code, outcome, statuses, self.STEPS)(
                command, cwd, drain_seconds)
            if then is not None:
                then()
            return returned
        return run_runner

    def _start(self, argv: list, runner=None) -> tuple:
        """`main` with `argv` and the stub that honors `--from`, and what it logged."""
        with mock.patch.object(entrypoint, "run_runner", runner or self._runner_honoring_from()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            returned = entrypoint.main(list(argv))
        return returned, err.getvalue()

    def _completed_this_month(self) -> None:
        """The data root of a run that completed over a retrieval taken ten days ago, in the
        month of the clock of the test."""
        self._later_seconds()
        self._runner_writing_the_state(0, "success", ["ok"] * len(self.STEPS), self.STEPS)(
            ["runner"], self.data)
        self._retrieval(10)

    def test_a_from_that_a_run_used_is_left_out_at_the_next_start(self):
        """The runbook gives `--from <step>` in `command:` for one Start, such as to finish the
        previous month's analysis. Left there, the argument would run the step again at every
        later Start and pack another archive of the data root. The next Start leaves the
        argument out with a warning that names it, runs no step and packs nothing."""
        self._completed_this_month()
        returned, said = self._start(self.FROM_RQ2)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [self.FROM_RQ2])
        self.assertNotIn("WARNING", said)
        after_first = self._handovers()
        # Only the handover of the run of the `--from`. Because that run rewrites only analysis/
        # and run_logs/, the Start packs nothing before it.
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in after_first], ["complete.7z"])
        returned, said = self._start(self.FROM_RQ2)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with()[1], ["--with-commit-reclassification"])
        self.assertIn("WARNING entrypoint: The `command:` of compose.yaml still names --from "
                      "rq2_config_cooccurrence, an argument for one Start that the run of an "
                      "earlier Start used. Left it out.", said)
        self.assertIn("Remove it from `command:`.", said)
        self.assertIn("Every step was already complete, and no handover was packed", said)
        self.assertEqual(self._handovers(), after_first)

    def test_a_from_left_in_command_does_not_keep_the_next_month_from_beginning(self):
        """The scheduled Start on December 1 after a Start in November with `--from` in
        `command:` begins December's run without the `--from`, as a Start without it does."""
        self._completed_this_month()
        self.assertEqual(self._start(self.FROM_RQ2)[0], 0)
        self.now = entrypoint.datetime(2026, 12, 1, tzinfo=entrypoint.timezone.utc)
        returned, said = self._start(self.FROM_RQ2)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [self.FROM_RQ2, self.NEXT_MONTH])
        self.assertIn("still names --from rq2_config_cooccurrence", said)
        self.assertIn("Beginning the next monthly run", said)

    def test_the_next_monthly_run_of_the_same_start_goes_without_the_argument(self):
        """A Start in November with a `--from` begins December's run in the same Start when its
        run ends in December, and December's run no longer applies the `--from`."""
        self._completed_this_month()

        def into_december():
            self.now = entrypoint.datetime(2026, 12, 2, tzinfo=entrypoint.timezone.utc)
        returned, said = self._start(self.FROM_RQ2, self._runner_honoring_from(then=into_december))
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [self.FROM_RQ2, self.NEXT_MONTH])
        self.assertIn("The next monthly run goes without --from rq2_config_cooccurrence", said)

    def test_a_start_without_the_argument_forgets_that_it_was_used(self):
        """Removing the argument and adding it again for another Start passes it again."""
        self._completed_this_month()
        for argv in (self.FROM_RQ2, ["--with-commit-reclassification"]):
            self.assertEqual(self._start(argv)[0], 0)
        self.assertFalse((self.data / entrypoint.ONE_START_RECORD).exists())
        returned, said = self._start(self.FROM_RQ2)
        self.assertEqual(returned, 0)
        self.assertNotIn("still names", said)
        self.assertEqual(self._started_with(),
                         [self.FROM_RQ2, ["--with-commit-reclassification"], self.FROM_RQ2])

    def test_an_argument_that_the_runner_refused_is_passed_again(self):
        """A refusal by the pre-checks, by the check for modified inputs or by the lock,
        which writes no state, leaves the record of completed steps as it was. The `--from` has
        therefore not acted yet."""
        self._completed_this_month()
        for outcome in ("pre_checks_failed", "modified_inputs", None):
            with self.subTest(outcome=outcome):
                refused = (self._runner_writing_the_state(1, outcome, [], [])
                           if outcome else lambda command, cwd, drain_seconds=0: 1)
                self.started.clear()

                def run_runner(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
                    self.started.append({"command": command})
                    return refused(command, cwd, drain_seconds)
                self.assertEqual(self._start(self.FROM_RQ2, run_runner)[0], 1)
                self.assertEqual(self._start(self.FROM_RQ2, run_runner)[0], 1)
                self.assertEqual(self._started_with(), [self.FROM_RQ2, self.FROM_RQ2])

    def test_an_argument_of_a_start_that_a_kill_ended_counts_as_used_when_the_runner_used_it(self):
        """A reboot kills the entrypoint with the runner, after the runner removed the steps that
        `--from` names from its record of completed steps and began them. The Start at boot-up
        resumes the run without the `--from`, which would otherwise run those steps and the steps
        after them once more. A kill during the pre-checks leave the record of completed
        steps as it was, and the `--from` is passed again."""

        class Reboot(BaseException):
            """The kill of the whole container, which no handler of the entrypoint survives."""
        self._completed_this_month()
        for outcome, used in (("running", True), (None, False)):
            with self.subTest(outcome=outcome):
                self.started.clear()
                (self.data / entrypoint.ONE_START_RECORD).unlink(missing_ok=True)

                def killed(command, cwd, drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
                    self.started.append({"command": command})
                    if outcome is not None:
                        self._runner_writing_the_state(
                            0, outcome, ["skipped"], ["release"],
                            dropped=("rq2_config_cooccurrence",),
                            running_step="rq2_config_cooccurrence")(command, cwd, drain_seconds)
                    raise Reboot()
                with self.assertRaises(Reboot):
                    self._start(self.FROM_RQ2, killed)
                returned, said = self._start(self.FROM_RQ2)
                self.assertEqual(self._started_with()[1],
                                 ["--with-commit-reclassification"] if used else self.FROM_RQ2)
                self.assertEqual("still names --from rq2_config_cooccurrence" in said, used)

    def test_a_fresh_is_passed_until_its_step_ran_and_then_left_out_with_the_settings_kept(self):
        """`--fresh` deletes the collection when the cloning step runs. A Start that skipped the
        cloning step has not used the `--fresh`, and a later Start passes it again. Once the step
        ran with it, later Starts leave out the `--fresh`, together with the `--from` of the same
        Start, and keep the other words of its value, which are settings."""
        self._completed_this_month()
        fresh = ["--with-commit-reclassification", "--step-arg", "repository_cloning:-w 3 --fresh"]
        both = [*fresh, "--from", "repository_cloning"]
        for argv in (fresh, fresh, both, both):
            self.assertEqual(self._start(argv)[0], 0)
        self.assertEqual(self._started_with(),
                         [fresh, fresh, both,
                          ["--with-commit-reclassification", "--step-arg", "repository_cloning:-w 3"]])
        said = self._start(both)[1]
        self.assertIn("still names --step-arg repository_cloning:--fresh and --from "
                      "repository_cloning, arguments for one Start that the run of an earlier "
                      "Start used. Left them out.", said)

    def test_the_filters_top_languages_reaches_the_filter_and_the_download_after_it_failed(self):
        """`--top-languages` of the filter is a setting that every Start passes, not an argument
        for one Start. A Start can use its `--from` and fail in the filter. The next Start still
        passes the setting, and the runner gives it to the filter and to the GHS download.
        Without the setting, the filter that runs again would keep the config's number of
        languages, although the download judged the left-out languages of the retrieval with
        the setting's number."""
        self._completed_this_month()
        setting = ["--step-arg", "repository_filtering:--top-languages 15"]
        argv = ["--with-commit-reclassification", "--from", "repository_filtering", *setting]
        names = ["ghs_download", "repository_filtering"]
        starts = self._starts((1, "failed", ["skipped", "failed"], names, ("repository_filtering",)),
                              (1, "failed", ["skipped", "failed"], names))
        for _ in range(2):
            self._start(argv, starts)
        self.assertEqual(self._started_with(),
                         [argv, ["--with-commit-reclassification", *setting]])
        sys.path.insert(0, str(HERE.parent))
        self.addCleanup(sys.path.remove, str(HERE.parent))
        import run_pipeline
        from agentcontext import runner
        with mock.patch.object(runner, "run", return_value=0) as run:
            self.assertEqual(run_pipeline.main(self._started_with()[1]), 0)
        steps = {step.key: step for step in run.call_args.args[0]}
        self.assertEqual(steps["ghs_download"].args, ["1_download_ghs.py", "--top-languages", "15"])
        self.assertEqual(steps["repository_filtering"].args,
                         ["4_filter_ghs.py", "--top-languages", "15"])

    def _fall_back(self, count: int, rows: "int | None" = None):
        """What the GHS download writes when it falls back to the previous sampling frame. The
        record has the outcome `fallback`, the reason, the number of downloads in a row that fell
        back, and the rows of a GHS search result that the size check refused. The failed
        retrieval started at the clock of the test."""
        def write():
            report = {"outcome": "fallback", "retrieval_id": "2026-11-15T120000Z",
                      "failed_retrieval_id": self.now.strftime("%Y-%m-%dT%H%M%SZ"),
                      "window_moved": False, "fallbacks_in_a_row": count,
                      "reason": ("the GHS search result has 150,000 repositories, fewer than 90% "
                                 "of the 205,697 of retrieval 2026-11-15T120000Z, which the stage "
                                 "read before, for the same query" if rows else
                                 "all 5 attempts at the crawl dates failed")}
            if rows:
                report["failed_item_count"] = rows
            (self.data / entrypoint.RETRIEVAL_REPORT).write_text(json.dumps(report),
                                                                 encoding="utf-8")
        return write

    def test_each_start_after_a_fallback_ends_with_a_warning_of_the_count(self):
        """While GHS stays smaller, the download of every monthly run falls back, and the stage
        exits 0 over the previous sampling frame. Each such Start ends with a warning that
        names the retrieval, the reason, how many downloads in a row fell back and how to take
        the smaller retrieval. A Start that took no new retrieval warns of nothing."""
        self._completed_this_month()
        for count, month in ((1, 12), (2, 1), (3, 2)):
            self.now = entrypoint.datetime(2026 if month == 12 else 2027, month, 2, 12,
                                           tzinfo=entrypoint.timezone.utc)
            returned, said = self._start(["--with-commit-reclassification"],
                                         self._runner_honoring_from(
                                             then=self._fall_back(count, 150_000)))
            self.assertEqual(returned, 0)
            self.assertIn("--from ghs_download", " ".join(self.started[-1]["command"]))
            last = said.strip().splitlines()[-1]
            self.assertTrue(last.startswith("WARNING entrypoint: The GHS download fell back to "
                                            "the previous sampling frame, retrieval "
                                            "2026-11-15T120000Z, because the GHS search result "
                                            "has 150,000 repositories, fewer than 90% of the "
                                            "205,697"), said)
            self.assertIn(". The sampling window did not move.", last)
            self.assertIn(" No earlier download fell back to that retrieval." if count == 1 else
                          f" {count} downloads in a row have fallen back to that retrieval.", last)
            self.assertIn("put --step-arg ghs_download:--accept-smaller in the `command:` of "
                          "compose.yaml for one Start", last)
        returned, said = self._start(["--with-commit-reclassification"])
        self.assertEqual(returned, 0)
        self.assertNotIn("fell back", said)

    def test_a_fallback_without_a_refused_search_result_names_no_way_to_accept_it(self):
        """When GHS returns no response, there is no GHS search result to take, and the warning
        names only the reason and the count."""
        self._completed_this_month()
        returned, said = self._start([*self.FROM_RQ2[:1], "--from", "ghs_download"],
                                     self._runner_honoring_from(then=self._fall_back(1)))
        self.assertEqual(returned, 0)
        self.assertIn("2026-11-15T120000Z, because all 5 attempts at the crawl dates failed. "
                      "The sampling window did not move. No earlier download fell back to that "
                      "retrieval. Read run_logs/ghs_download.log.", said)
        self.assertNotIn("--accept-smaller", said)

    def test_an_accept_smaller_is_passed_until_the_download_ran_and_then_left_out(self):
        """`--accept-smaller` takes a far smaller retrieval when the GHS download runs. A Start
        that skipped the download has not used the argument, and the Start of the next monthly
        run passes it. Later Starts leave it out with a warning."""
        self._completed_this_month()
        accept = ["--with-commit-reclassification", "--step-arg", "ghs_download:--accept-smaller"]
        self.assertEqual(self._start(accept)[0], 0)
        self.now = entrypoint.datetime(2026, 12, 2, 12, tzinfo=entrypoint.timezone.utc)
        self.assertEqual(self._start(accept)[0], 0)
        returned, said = self._start(accept)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(),
                         [accept, [*accept, "--from", "ghs_download", "--from",
                                   "repository_cloning"],
                          ["--with-commit-reclassification"]])
        self.assertIn("still names --step-arg ghs_download:--accept-smaller, an argument for "
                      "one Start that the run of an earlier Start used", said)

    def test_an_accept_smaller_of_a_download_that_did_not_end_ok_is_passed_again(self):
        """The GHS search result had far fewer rows, and you put `--accept-smaller` in `command:`
        with the `--from` that takes a retrieval at that Start. The download fails, such as on a
        network error, or a Stop ends it, and it takes no retrieval. The Start that resumes the
        run passes the `--accept-smaller` again, because without it the download would refuse
        the smaller GHS search result once more. That Start leaves out the `--from`, which the
        run used."""
        self._completed_this_month()
        accept = ["--with-commit-reclassification", "--step-arg", "ghs_download:--accept-smaller"]
        both = [*accept, "--from", "ghs_download", "--from", "repository_cloning"]
        for code, outcome in ((1, "failed"), (128 + signal.SIGTERM, "interrupted")):
            with self.subTest(outcome=outcome):
                self.started.clear()
                (self.data / entrypoint.ONE_START_RECORD).unlink(missing_ok=True)
                starts = self._starts((code, outcome, [outcome], ["ghs_download"]),
                                      (0, "success", ["ok"], ["ghs_download"]))
                self.assertEqual(self._start(both, starts)[0], code)
                returned, said = self._start(both, starts)
                self.assertEqual(returned, 0)
                self.assertEqual(self._started_with(), [both, accept])
                self.assertIn("still names --from ghs_download and --from repository_cloning",
                              said)
                self.assertNotIn("--accept-smaller, an argument", said)

    def test_an_accept_smaller_of_a_download_that_fell_back_is_passed_again(self):
        """GHS lost rows for good, and you put `--accept-smaller` alone in `command:` for the
        next monthly run, as the warning after the refused GHS search result says. At the Start
        of that run, GHS does not respond. The download falls back to the previous
        sampling frame and exits 0, and the step ends `ok` without a retrieval. The Start of the
        month after passes the `--accept-smaller` again, because without it the download would
        refuse the smaller GHS search result once more. The Start after the download that took
        the retrieval leaves the argument out."""
        self._completed_this_month()
        accept = ["--with-commit-reclassification", "--step-arg", "ghs_download:--accept-smaller"]
        december = entrypoint.datetime(2026, 12, 2, 12, tzinfo=entrypoint.timezone.utc)
        january = entrypoint.datetime(2027, 1, 2, 12, tzinfo=entrypoint.timezone.utc)
        self.now = december
        returned, said = self._start(accept, self._runner_honoring_from(
            then=self._fall_back(1)))
        self.assertEqual(returned, 0)
        self.assertIn("fell back to the previous sampling frame", said)
        self.now = january
        self.assertEqual(self._start(accept)[0], 0)
        returned, said = self._start(accept)
        self.assertEqual(returned, 0)
        monthly = [*accept, "--from", "ghs_download", "--from", "repository_cloning"]
        self.assertEqual(self._started_with(),
                         [monthly, monthly, ["--with-commit-reclassification"]])
        self.assertIn("still names --step-arg ghs_download:--accept-smaller, an argument for "
                      "one Start that the run of an earlier Start used", said)

    def test_a_from_packs_a_completed_run_without_a_handover_before_the_runner_starts(self):
        """The warning after a fallback and the warning about a missing record of the GHS
        download name `--from ghs_download --from repository_cloning` for one Start. A run that
        a `--from` of a later step begins, such as `--from repository_cloning` to retry the
        repositories that the cloning step left, also overwrites the outputs of the completed
        run for the steps that it redoes. The packing of the completed run failed or did not fit
        on the handover volume. The data root is packed before the runner starts, as before a
        monthly run."""
        for begin in (["--from", "ghs_download", "--from", "repository_cloning"],
                      ["--from", "repository_cloning"], ["--from", "release"]):
            with self.subTest(begin=begin):
                self.started.clear()
                for archive in self._handovers():
                    (self.app / entrypoint.HANDOVER / archive).unlink()
                (self.data / entrypoint.HANDOVER_RECORD).unlink(missing_ok=True)
                (self.data / entrypoint.ONE_START_RECORD).unlink(missing_ok=True)
                self._completed_this_month()
                argv = ["--with-commit-reclassification", *begin]
                returned, said = self._start(argv)
                self.assertEqual(returned, 0)
                self.assertEqual(self._started_with(), [argv])
                before = self.started[0]["handovers"]
                self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
                self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                              self._members(before[0]))
                named = " ".join(entrypoint.from_arguments(begin))
                self.assertIn(f"Packing the data root before the run with {named}.", said)
                self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()],
                                 ["complete.7z", "complete.7z"], self._handovers())

    def test_a_from_after_a_recorded_handover_packs_nothing_before_the_runner_starts(self):
        """The record names a `-complete` archive that contains the data root of the completed
        run, and the run of `--from repository_cloning` begins without a second copy."""
        self._completed_this_month()
        state = json.loads((self.data / entrypoint.STATE_FILE).read_text(encoding="utf-8"))
        (self.data / entrypoint.HANDOVER_RECORD).write_text(json.dumps(
            {"archive": "agentcontext-handover-20261115T000000Z-complete.7z",
             entrypoint.LAST_STEPS_FINISHED_AT: state[entrypoint.LAST_STEPS_FINISHED_AT]}),
            encoding="utf-8")
        argv = ["--with-commit-reclassification", "--from", "repository_cloning"]
        returned, said = self._start(argv)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [argv])
        self.assertEqual(self.started[0]["handovers"], [])
        self.assertNotIn("contains the data root of the previous run", said)
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()], ["complete.7z"])

    def _failed_in_the_analysis_this_month(self) -> None:
        """The data root of a run that completed its release step and failed in
        rq2_config_cooccurrence, over a retrieval taken ten days ago. The record of the last
        handover of a completed run names the `-complete` archive of the run before it, and
        `handover/` holds no archive."""
        self._later_seconds()
        (self.data / "run_logs").mkdir(exist_ok=True)
        (self.data / entrypoint.HANDOVER_RECORD).write_text(json.dumps(
            {"archive": "agentcontext-handover-20261015T000000Z-complete.7z",
             entrypoint.LAST_STEPS_FINISHED_AT: "2026-10-15T00:00:00+00:00"}), encoding="utf-8")
        self._runner_writing_the_state(1, "failed", ["ok", "ok", "ok", "failed"], self.STEPS)(
            ["runner"], self.data)
        self._retrieval(10)

    def test_a_from_of_the_failed_analysis_step_packs_no_data_root_before_the_runner(self):
        """After a run that failed in rq2_config_cooccurrence, the runbook gives `--from
        rq2_config_cooccurrence` in `command:` for one Start, such as with a new image that
        repairs the step. The run of that Start rewrites only `analysis/` and `run_logs/`, which
        the `-failed` handover contains. The Start therefore packs nothing before the runner,
        although the record names the `-complete` archive of the run before. A data root packed
        first would take the space of a second archive beside the archive that the run packs at
        its end."""
        self._failed_in_the_analysis_this_month()
        self.assertEqual(entrypoint.failed_analysis_step(self.data), "rq2_config_cooccurrence")
        returned, said = self._start(self.FROM_RQ2)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [self.FROM_RQ2])
        self.assertEqual(self.started[0]["handovers"], [])
        self.assertNotIn("contains the data root of the previous run", said)
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()], ["complete.7z"])

    def test_a_from_of_the_release_after_a_failed_analysis_packs_the_data_root_first(self):
        """Because `--from release` after the same run rewrites the release of that run, which
        its `-failed` handover does not contain, the data root is packed before the runner
        starts. It is also packed when a `--from` of the failed step comes with
        `--from release`."""
        for begin in (["--from", "release"],
                      ["--from", "rq2_config_cooccurrence", "--from", "release"]):
            with self.subTest(begin=begin):
                self.started.clear()
                for archive in self._handovers():
                    (self.app / entrypoint.HANDOVER / archive).unlink()
                (self.data / entrypoint.ONE_START_RECORD).unlink(missing_ok=True)
                self._failed_in_the_analysis_this_month()
                argv = ["--with-commit-reclassification", *begin]
                returned, said = self._start(argv)
                self.assertEqual(returned, 0)
                self.assertEqual(self._started_with(), [argv])
                before = self.started[0]["handovers"]
                self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
                self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                              self._members(before[0]))
                self.assertIn(f"Packing the data root before the run with "
                              f"{' '.join(begin)}.", said)
                self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()],
                                 ["complete.7z", "complete.7z"], self._handovers())

    def test_a_from_after_a_stop_or_a_kill_in_the_analysis_packs_the_data_root_first(self):
        """A Stop or a kill in rq2_config_cooccurrence ends a run after its release step, with
        the outcome `interrupted` or `running`, as a reboot does in a monthly run. That run
        neither completed nor failed, and its data root is not packed as `-complete` before the
        next monthly run either. A `--from release`, or the `--from ghs_download --from
        repository_cloning` of a monthly run given by hand, rewrites the run's release, and the
        data root is packed before the runner starts. The record names the `-complete`
        archive of the run before the stopped run."""
        for ended, outcome, statuses, running in (
                ("Stop", "interrupted", ["ok", "ok", "ok", "interrupted"], None),
                ("kill", "running", ["ok", "ok", "ok"], "rq2_config_cooccurrence")):
            for begin in (["--from", "release"],
                          ["--from", "ghs_download", "--from", "repository_cloning"]):
                with self.subTest(ended=ended, begin=begin):
                    self.started.clear()
                    for archive in self._handovers():
                        (self.app / entrypoint.HANDOVER / archive).unlink()
                    (self.data / entrypoint.ONE_START_RECORD).unlink(missing_ok=True)
                    self._failed_in_the_analysis_this_month()
                    self._runner_writing_the_state(143 if outcome == "interrupted" else 0,
                                                   outcome, statuses,
                                                   self.STEPS[:len(statuses)],
                                                   running_step=running)(["runner"], self.data)
                    self.assertIsNone(entrypoint.failed_analysis_step(self.data))
                    argv = ["--with-commit-reclassification", *begin]
                    returned, said = self._start(argv)
                    self.assertEqual(returned, 0, said)
                    self.assertEqual(self._started_with(), [argv])
                    before = self.started[0]["handovers"]
                    self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"],
                                     said)
                    self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                                  self._members(before[0]))
                    self.assertIn(f"Packing the data root before the run with "
                                  f"{' '.join(begin)}.", said)

    def test_a_from_whose_packing_fails_starts_no_runner(self):
        """After a completed run with a data root that no archive contains, a `--from` begins no
        run while the packing fails."""
        self._completed_this_month()
        argv = ["--with-commit-reclassification", "--from", "repository_cloning"]
        with mock.patch.object(entrypoint, "_run_7z", return_value=(2, "Error: no space")):
            returned, said = self._start(argv)
        self.assertEqual(returned, 1)
        self.assertEqual(self.started, [])
        self.assertIn("because its packing failed (7zz exited 2, with the output ending in Error: "
                      "no space). The run with --from repository_cloning does not begin, because "
                      "it would rewrite that data root", said)

    def test_a_from_after_a_failed_collection_packs_nothing_first(self):
        """A run that failed before its release step has no complete collection, and an archive
        of its data root named `-complete` would claim one. Its `-failed` handover contains its
        state, its logs and its lists."""
        self._later_seconds()
        self._runner_writing_the_state(1, "failed", ["ok", "failed"],
                                       ["ghs_download", "repository_labeling"])(["runner"],
                                                                                self.data)
        self._retrieval(10)
        begin = ["--with-commit-reclassification", "--from", "ghs_download"]
        returned, said = self._start(begin)
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [begin])
        self.assertEqual(self.started[0]["handovers"], [])
        self.assertNotIn("contains the data root of the previous run", said)

    def test_a_mark_complete_that_the_runner_recorded_is_left_out_at_the_next_start(self):
        """`--mark-complete` records a step and runs none, which the runner records as the
        outcome `marked_complete`."""
        self._completed_this_month()
        mark = ["--with-commit-reclassification", "--mark-complete", "repository_cloning"]
        returned, _said = self._start(mark, self._starts((0, "marked_complete", [], [])))
        self.assertEqual(returned, 0)
        returned, said = self._start(mark)
        self.assertEqual(self._started_with(), [mark, ["--with-commit-reclassification"]])
        self.assertIn("still names --mark-complete repository_cloning", said)

    def _real_runner(self, program: str) -> None:
        """Run `program` as the image's `run_pipeline.py`, with the package of this checkout, so
        that `main` starts the real runner through the real `run_runner`."""
        from agentcontext import runner
        os.environ["PYTHONPATH"] = str(Path(runner.__file__).resolve().parents[1])
        (self.image / "run_pipeline.py").write_text(program, encoding="utf-8")

    def test_a_fresh_counts_as_used_only_when_a_kill_ended_its_own_step(self):
        """A reboot kills the runner in a step before the step to which a `--fresh` of `command:`
        belongs. The `--fresh` has deleted nothing yet, and the Start at boot-up passes it again.
        A kill inside its own step can have deleted the collection, and the next Start leaves the
        `--fresh` out. The real runner runs two steps, and a flag file makes one of them kill the
        runner, as a reboot does."""
        self._later_seconds()
        self._retrieval(10)
        self._real_runner(
            "import os, sys\n"
            "from pathlib import Path\n"
            "from agentcontext import runner\n"
            "def body(name):\n"
            "    # The cloning step's `--fresh` removes the list of finished repositories first.\n"
            "    first = ('Path(\\'collection/finished_repositories.jsonl\\').unlink(); '\n"
            "             if name == 'cloning' else '')\n"
            "    if Path(f'kill-in-{name}').exists():\n"
            "        return ('import os, signal; from pathlib import Path; ' + first\n"
            "                + 'os.kill(os.getppid(), signal.SIGKILL)')\n"
            "    return 'pass'\n"
            "steps = [runner.Step('download', sys.executable, ['-c', body('download')],\n"
            "                     root=os.getcwd()),\n"
            "         runner.Step('cloning', sys.executable, ['-c', body('cloning')],\n"
            "                     consumes=('download',), root=os.getcwd())]\n"
            "raise SystemExit(runner.run(steps, argv=sys.argv, log_dir=Path.cwd() / 'run_logs',\n"
            "                            min_free_gb=0))\n")
        fresh = ["--with-commit-reclassification", "--step-arg", "cloning:--fresh"]
        listed = self.data / entrypoint.FINISHED_LIST
        listed.parent.mkdir(parents=True, exist_ok=True)
        for killed_in, used in (("download", False), ("cloning", True)):
            with self.subTest(killed_in=killed_in):
                (self.data / entrypoint.ONE_START_RECORD).unlink(missing_ok=True)
                listed.write_text('{"repo": "owner/repo"}\n', encoding="utf-8")
                flag = self.data / f"kill-in-{killed_in}"
                flag.touch()
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(entrypoint.main(list(fresh)), 128 + signal.SIGKILL)
                flag.unlink()
                self.started.clear()
                returned, said = self._start(fresh, self._runner(0, "success", ["ok"]))
                self.assertEqual(returned, 0)
                self.assertEqual(self._started_with(),
                                 [["--with-commit-reclassification"] if used else fresh])
                self.assertEqual("still names --step-arg cloning:--fresh" in said, used)

    def test_a_from_that_a_check_refused_before_the_runner_applied_it_is_passed_again(self):
        """A check of the runner can stop the run before the runner takes the steps that
        `--from` names out of its record of completed steps, such as a git that cannot start
        while the runner fingerprints the cloning step. The check writes the outcome `failed`,
        with the record as it was and no step run. The `--from` has not acted, and the next Start
        passes it again. The real runner runs one step recorded by its output and by its inputs.
        A flag file makes the reading of the inputs stop the run, as `collection_inputs` stops it
        when git cannot start."""
        self._later_seconds()
        self._retrieval(10)
        self._real_runner(
            "import os, sys\n"
            "from pathlib import Path\n"
            "from agentcontext import runner\n"
            "def inputs_of(step):\n"
            "    if Path('git-cannot-start').exists():\n"
            "        raise SystemExit('git cannot start')\n"
            "    return [('git', 'git version 2.47.3')]\n"
            "Path('count.py').write_text(\"open('count.csv', 'w').write('n\\\\n1\\\\n')\\n\")\n"
            "steps = [runner.Step('count', sys.executable, ['count.py'], produces=('count.csv',),\n"
            "                     completion=runner.COMPLETION_OUTPUT, inputs_of=inputs_of,\n"
            "                     root=os.getcwd())]\n"
            "args = sys.argv[1:]\n"
            "named = [args[i + 1] for i in range(len(args) - 1) if args[i] == '--from']\n"
            "raise SystemExit(runner.run(steps, argv=sys.argv, from_name=named or None,\n"
            "                            log_dir=Path.cwd() / 'run_logs', min_free_gb=0))\n")
        plain = ["--with-commit-reclassification"]
        from_count = [*plain, "--from", "count"]
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(list(plain)), 0)
        stop = self.data / "git-cannot-start"
        stop.touch()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(list(from_count)), 1)
        state = json.loads((self.data / entrypoint.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual((state["outcome"], state["steps"], list(state["completed"])),
                         ("failed", [], ["count"]))
        stop.unlink()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(list(from_count)), 0)
        said = err.getvalue()
        self.assertNotIn("still names", said)
        self.assertIn(f"run_pipeline.py {' '.join(from_count)}", said)
        state = json.loads((self.data / entrypoint.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual([(step["name"], step["status"]) for step in state["steps"]],
                         [("count", "ok")])
        returned, said = self._start(from_count, self._runner(0, "success", ["ok"]))
        self.assertEqual(returned, 0)
        self.assertEqual(self._started_with(), [plain])
        self.assertIn("still names --from count", said)

    def _member_text(self, archive: str, member: str) -> str:
        """The text of one member of an archive in the handover directory."""
        return subprocess.run([entrypoint.SEVEN_ZIP, "e", "-so",
                               str(self.app / entrypoint.HANDOVER / archive), member],
                              capture_output=True, text=True, check=True).stdout

    def test_the_handover_before_the_month_names_the_previous_runs_image_and_heuristics(self):
        """The machine record and the copies of the heuristics files of this start belong to the
        next run. Written before the packing, they would make the handover of the previous run
        name an image and heuristics that the previous run did not use."""
        self._completed_run_over_an_old_retrieval()
        machine = f"{entrypoint.DATA}/{entrypoint.MACHINE_RECORD.as_posix()}"
        patterns = f"{entrypoint.DATA}/{entrypoint.HEURISTICS_RECORD.as_posix()}/config_path_patterns.csv"
        (self.app / machine).write_text('{"image": "commit 1111111"}\n', encoding="utf-8")
        (self.app / patterns).parent.mkdir(parents=True)
        (self.app / patterns).write_text("the patterns of the previous run\n", encoding="utf-8")
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        before = self.started[0]["handovers"]
        self.assertEqual(len(before), 1, before)
        self.assertEqual(self._member_text(before[0], machine), '{"image": "commit 1111111"}\n')
        self.assertEqual(self._member_text(before[0], patterns),
                         "the patterns of the previous run\n")
        self.assertEqual((self.app / patterns).read_text(encoding="utf-8"),
                         "config_path_patterns.csv\n")

    def test_a_monthly_start_whose_record_cannot_be_written_begins_the_month(self):
        """The archive that the start has just packed contains the data root, whether or not
        the record that names it could be written."""
        self._later_seconds()
        self._completed_run_over_an_old_retrieval()
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "record_handover",
                                  side_effect=OSError("read-only file system")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        before = self.started[0]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)

    def test_a_record_that_cannot_be_written_packs_the_resumed_run_once(self):
        """The handover of the resumed run contains the data root that the next monthly run
        replaces. A second copy in the same start would take the space of a whole data root
        again."""
        self._later_seconds()
        self._killed_run(40)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((0, "success", ["ok"]), (0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "record_handover",
                                  side_effect=OSError("read-only file system")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(),
                         [["--with-commit-reclassification"], self.NEXT_MONTH])
        before = self.started[1]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)

    def _stop_while_measuring(self) -> None:
        """Send this process SIGTERM whenever the data root is measured, as a Stop that arrives
        after the runner ended does. The installed handler ends the test's start, as the default
        action of SIGTERM ends the process."""
        class Ended(Exception):
            pass

        def end(signum, _stack_frame):
            raise Ended(signum)
        previous = signal.signal(signal.SIGTERM, end)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        real = entrypoint.tree_bytes

        def measure(path, skip=None):
            os.kill(os.getpid(), signal.SIGTERM)
            return real(path, skip)
        patcher = mock.patch.object(entrypoint, "tree_bytes", measure)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_stop_while_the_data_root_is_measured_still_hands_over(self):
        """The runner's handler is gone once the runner ends, and 7zz's handler is not yet
        installed. The Stop is recorded, and the handover is packed without the data root,
        because 7zz can take longer to pack the data root than the stop grace period lasts."""
        self._later_seconds()
        self._stop_while_measuring()
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["ok"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()],
                         ["complete-without-sample.7z"], self._handovers())

    def test_a_stop_after_the_last_step_leaves_the_data_root_to_the_next_start(self):
        """A Stop that reaches the runner after its last step leaves the run complete. 7zz over
        the whole data root can take longer than the stop grace period. Docker then kills 7zz at
        the end of the period, and the kill leaves a `.partial` file and no handover. The start
        packs the pipeline execution state, the logs and the lists without the trees, and records
        no handover, so that the next Start packs the data root."""
        self._later_seconds()
        finishes = self._runner_writing_the_state(0, "success", ["ok"])

        def stopped_after_the_last_step(command, cwd,
                                        drain_seconds=entrypoint.KILLED_RUNNER_DRAIN_SECONDS):
            code = finishes(command, cwd, drain_seconds)
            entrypoint.note_stop(signal.SIGTERM)
            return code
        packed = []
        real = entrypoint._run_7z

        def run_7z(args, cwd, **options):
            packed.append(args)
            return real(args, cwd, **options)
        with mock.patch.object(entrypoint, "run_runner", stopped_after_the_last_step), \
                mock.patch.object(entrypoint, "_run_7z", run_7z), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        handovers = self._handovers()
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["complete-without-sample.7z"], handovers)
        self.assertEqual(len(packed), 1, "the trees were packed after the Stop")
        members = self._members(handovers[0])
        self.assertIn(f"{entrypoint.DATA}/{entrypoint.STATE_FILE}", members)
        self.assertNotIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md", members)
        self.assertFalse((self.data / entrypoint.HANDOVER_RECORD).exists())
        self.assertIn(f"A Stop (signal {signal.SIGTERM:d}) arrived before the data root of the "
                      f"completed run was packed. Packing the pipeline execution state and logs, "
                      f"the data of the sampling, classification and analysis stages, and the "
                      f"lists, caches and output CSVs of the collection instead", err.getvalue())
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["skipped"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        complete = [h for h in self._handovers() if h.endswith("Z-complete.7z")]
        self.assertEqual(len(complete), 1, self._handovers())
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(complete[0]))

    def test_a_stop_while_the_previous_run_is_measured_packs_nothing_and_starts_no_runner(self):
        """The data root of the previous run is measured before it is packed. Docker would kill a
        packing that starts after the Stop at the end of the grace period, and a monthly run
        would run until then. The data root remains in the project folder."""
        self._completed_run_over_an_old_retrieval()
        self._stop_while_measuring()
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                             128 + signal.SIGTERM)
        self.assertEqual(self.started, [])
        self.assertEqual(self._handovers(), [])
        self.assertIn(f"A Stop (signal {signal.SIGTERM:d}) arrived before the data root of the "
                      f"previous run was packed. Nothing more was packed",
                      err.getvalue())

    def test_a_stop_before_the_previous_run_is_packed_starts_no_packing(self):
        """After a Stop that arrives during the checks of a start that would begin the next
        monthly run, the start runs no 7zz over the whole data root of the previous run. Docker
        would kill that 7zz at the end of the grace period. The next Start packs that data
        root."""
        self._completed_run_over_an_old_retrieval()
        real = entrypoint.remove_leftovers
        packed = []

        def stop_during_the_checks(app_root):
            os.kill(os.getpid(), signal.SIGTERM)
            real(app_root)

        def run_7z(args, cwd, **options):
            packed.append(args)
            return 2, "not expected"
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "remove_leftovers", stop_during_the_checks), \
                mock.patch.object(entrypoint, "_run_7z", run_7z), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                             128 + signal.SIGTERM)
        self.assertEqual(packed, [])
        self.assertEqual(self.started, [])
        self.assertEqual(self._handovers(), [])
        self.assertIn(f"A Stop (signal {signal.SIGTERM:d}) arrived before the data root of the "
                      f"previous run was packed", err.getvalue())

    def test_a_stop_while_the_previous_run_is_packed_exits_as_stopped(self):
        """The Stop ends 7zz. A packing that a Stop ended did not fail for a cause that you
        correct, and the next Start packs again."""
        self._completed_run_over_an_old_retrieval()

        def stopped_7z(args, cwd, **options):
            entrypoint.note_stop(signal.SIGTERM)
            return 128 + signal.SIGTERM, ""
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "_run_7z", stopped_7z), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                             128 + signal.SIGTERM)
        self.assertEqual(self.started, [])
        self.assertEqual(entrypoint.archives_in(self.app / entrypoint.HANDOVER)[0], 0)
        said = err.getvalue()
        self.assertIn("and the next Start packs it", said)
        self.assertNotIn("Correct the cause of that error", said)

    ANALYSIS_FAILED = (1, "failed", ["skipped", "ok", "failed"],
                       ["detection", "release", "rq2_upset_plot"])

    def test_a_run_whose_analysis_failed_after_its_release_begins_the_next_month(self):
        """Each Start would otherwise resume the failed run, and a step of the analysis stage
        that keeps failing would keep every later month from being collected. The data root of
        the failed run, with its collection and its release, is packed first."""
        self._later_seconds()
        self._runner_writing_the_state(*self.ANALYSIS_FAILED)(["runner"], self.data)
        self._retrieval(30)
        with mock.patch.object(entrypoint, "run_runner", self._starts((0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        before = self.started[0]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(before[0]))
        self.assertIn("WARNING entrypoint: The analysis of the previous month failed in "
                      "rq2_upset_plot", err.getvalue())

    def test_a_resumed_run_that_fails_in_the_analysis_begins_the_next_month(self):
        """A resumed collection can complete its release and then fail in the analysis. When it
        does, the same Start begins the month."""
        self._later_seconds()
        self._killed_run(40)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts(self.ANALYSIS_FAILED, (0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(),
                         [["--with-commit-reclassification"], self.NEXT_MONTH])
        before = self.started[1]["handovers"]
        complete = [h for h in before if h.endswith("Z-complete.7z")]
        self.assertEqual(len(complete), 1, before)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(complete[0]))
        self.assertIn("The analysis of the previous month failed in rq2_upset_plot",
                      err.getvalue())

    def test_a_run_that_failed_before_its_release_is_resumed(self):
        """A step of the collection that failed leaves the run without a complete collection,
        which the next monthly run would replace."""
        self._later_seconds()
        self._runner_writing_the_state(1, "failed", ["ok", "failed"],
                                       ["detection", "deleted_files_report"])(["runner"], self.data)
        self._retrieval(30)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((1, "failed", ["skipped", "failed"],
                                             ["detection", "deleted_files_report"]))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(self._started_with(), [["--with-commit-reclassification"]])

    def test_a_resumed_run_runs_once_when_the_next_month_is_not_due(self):
        """Over a retrieval of the current calendar month, the completed run is this month's
        run. A resumed run that again did not complete is resumed by the next Start."""
        for age, start in ((10, (0, "success", ["ok"])), (40, (1, "failed", ["failed"]))):
            with self.subTest(age=age, outcome=start[1]):
                self.started.clear()
                self._killed_run(age)
                with mock.patch.object(entrypoint, "run_runner", self._starts(start)), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                                     start[0])
                self.assertEqual(self._started_with(), [["--with-commit-reclassification"]])

    def test_a_refused_next_month_hands_over_the_resumed_run_and_exits_as_refused(self):
        """The pre-checks refuse the next month's run, such as for an expired GitHub access
        token. The resumed run completed, and its handover is packed, while the exit code
        reports the refusal."""
        self._later_seconds()
        self._killed_run(40)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((0, "success", ["ok"]),
                                            (1, "pre_checks_failed", []))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(len(self.started), 2)
        handovers = self._handovers()
        self.assertEqual(len(handovers), 1, handovers)
        self.assertTrue(handovers[0].endswith("Z-complete.7z"), handovers)
        self.assertEqual(self.started[1]["handovers"], handovers)
        self.assertIn("The next monthly run ran no step, and no handover was packed for it",
                      err.getvalue())

    def test_a_start_after_a_refused_month_begins_it_and_packs_the_data_root_once(self):
        """The run of the previous month failed in the analysis stage. The Start that begins the
        next monthly run packs that run's data root. The pre-checks then refuse the monthly
        run, such as for an expired GitHub access token, with step results in which no step ran. The
        next Start begins the month again. If it resumed the failed run instead, the failed step
        would run again and change the time that the record of the packing names, and the Start
        would pack the data root a second time before the month."""
        self._later_seconds()
        self._runner_writing_the_state(*self.ANALYSIS_FAILED)(["runner"], self.data)
        self._retrieval(30)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((1, "pre_checks_failed", []))), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(self._started_with(), [self.NEXT_MONTH])
        self.started.clear()
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((0, "success", ["ok"]), (0, "success", ["ok"]))), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with()[0], self.NEXT_MONTH)
        before = self.started[0]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
        self.assertIn("The analysis of the previous month failed in rq2_upset_plot",
                      err.getvalue())

    def test_a_monthly_run_that_a_check_stopped_before_its_first_step_says_that_no_step_ran(self):
        """The Start of the next month begins the monthly run, and a check of the runner stops
        that run before its first step, such as a git that cannot start while the runner
        fingerprints the cloning step. The runner records the outcome `failed` and keeps the
        step results of the run before it in `last_run_steps`. If the analysis of the run before
        failed, the state still reads as a run that failed in the analysis. After a completed
        run, the state reads as a run that failed before its release, which the next Start
        resumes. In both cases, the warning says that the monthly run ran no step and why no
        other month begins, rather than point to the log of a download that did not run. The
        next Start begins the month."""
        for previous in ("analysis failed", "completed"):
            with self.subTest(previous=previous):
                shutil.rmtree(self.data / "run_logs")
                _record_revision(self.data, entrypoint.DATA_ROOT_REVISION)
                for archive in self._handovers():
                    (self.app / entrypoint.HANDOVER / archive).unlink()
                self.started.clear()
                self._later_seconds()
                self._runner_writing_the_state(
                    *(self.ANALYSIS_FAILED if previous == "analysis failed"
                      else (0, "success", ["ok"])))(["runner"], self.data)
                self._retrieval(30)
                with mock.patch.object(entrypoint, "run_runner",
                                       self._starts((1, "failed", [], []))), \
                        contextlib.redirect_stderr(io.StringIO()) as err:
                    self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
                said = err.getvalue()
                self.assertEqual(self._started_with(), [self.NEXT_MONTH])
                self.assertIn("WARNING entrypoint: The monthly run that just ended ran no step, "
                              "because a check of the runner stopped it before its first step. "
                              "The ERROR line of the runner above names the check. No other "
                              "monthly run begins before the next Start.", said)
                self.assertNotIn("left the record of its GHS download", said)
                self.assertNotIn("ghs_download.log", said)
                runs = ([(0, "success", ["ok"])] if previous == "analysis failed"
                        else [(0, "success", ["skipped"]), (0, "success", ["ok"])])
                with mock.patch.object(entrypoint, "run_runner", self._starts(
                        (1, "failed", [], []), *runs)), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
                self.assertEqual(self._started_with()[-1], self.NEXT_MONTH)

    def _later_seconds(self) -> None:
        """Give each archive the name of a later second, as Starts days apart do. Within one
        second, a second archive would replace the first under the same name, and the count of
        archives would not show it."""
        seconds = itertools.count()
        patcher = mock.patch.object(entrypoint, "timestamp",
                                    lambda: f"20261001T000{next(seconds):03d}Z")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_starts_that_ran_no_step_after_a_completed_run_pack_one_archive(self):
        """Every start writes the pipeline execution state, also a start that skips every step or
        that the pre-checks refuse. After the first such start, the archive of the
        completed run is older than the state. The time at which the last start that ran a step
        finished does not move."""
        self._later_seconds()
        for starts in ([(0, "success", ["ok", "ok"]), (0, "success", ["skipped", "skipped"]),
                        (0, "success", ["skipped", "skipped"])],
                       [(0, "success", ["ok", "ok"]), (1, "pre_checks_failed", []),
                        (0, "success", ["skipped", "skipped"])]):
            with self.subTest(starts=[start[1] for start in starts]):
                for path in (self.app / entrypoint.HANDOVER).glob("*.7z"):
                    path.unlink()
                for code, outcome, statuses in starts:
                    with mock.patch.object(entrypoint, "run_runner",
                                           self._runner_writing_the_state(code, outcome,
                                                                          statuses)), \
                            contextlib.redirect_stderr(io.StringIO()):
                        self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                                         code)
                handovers = self._handovers()
                self.assertEqual(len(handovers), 1, handovers)
                self.assertTrue(handovers[0].endswith("Z-complete.7z"), handovers)

    def test_starts_that_skip_every_step_without_room_pack_no_second_record(self):
        """Without room for the data root, the completed run packed the pipeline execution
        state, the logs, the lists and the trees, which stay unchanged while no step runs."""
        self._later_seconds()
        self._runner_writing_the_state(0, "success", ["ok"])(["runner"], self.data)
        size = entrypoint.tree_bytes(self.data)
        room = shutil.disk_usage(self.app)._replace(free=2 * size - 1)
        for statuses in (["ok"], ["skipped"], ["skipped"]):
            with mock.patch.object(entrypoint, "run_runner",
                                   self._runner_writing_the_state(0, "success", statuses)), \
                    mock.patch.object(entrypoint.shutil, "disk_usage", return_value=room), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in self._handovers()],
                         ["complete-trees.7z", "complete-without-sample.7z"], self._handovers())

    def test_the_keys_of_the_pipeline_execution_state_are_the_runners(self):
        from agentcontext import runner
        self.assertEqual(entrypoint.LAST_STEPS_FINISHED_AT, runner.LAST_STEPS_FINISHED_AT)
        self.assertEqual(entrypoint.LAST_RUN_STEPS, runner.LAST_RUN_STEPS)
        self.assertEqual(entrypoint.RUNNING_STEP, runner.RUNNING_STEP)
        self.assertEqual(entrypoint.RUNNING_STEP_RESULTS, runner.RUNNING_STEP_RESULTS)
        self.assertEqual(entrypoint.RUNNING_STEP_STARTED_AT, runner.RUNNING_STEP_STARTED_AT)

    def test_a_start_that_skipped_every_step_packs_when_the_packing_of_the_completed_run_failed(
            self):
        """No archive contains the data root as it is, and an archive of an earlier run does
        not count."""
        self._later_seconds()
        old = self.app / entrypoint.HANDOVER / "agentcontext-handover-20260901T000000Z-complete.7z"
        old.write_bytes(b"an archive of an earlier run")
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["ok", "ok"])), \
                mock.patch.object(entrypoint, "_run_7z", return_value=(2, "Error: no space")), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        self.assertEqual(self._handovers(), [old.name])
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success",
                                                              ["skipped", "skipped"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        handovers = self._handovers()
        self.assertEqual(len(handovers), 2, handovers)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(next(h for h in handovers if h != old.name)))

    def test_a_start_that_skipped_every_step_packs_nothing_after_you_deleted_the_archive(self):
        """You downloaded the archive of the completed run and deleted it. The Start at every
        boot-up skips every step, and a copy of the data root at each of them would use up the
        free space that the pre-checks of the next monthly run requires."""
        self._later_seconds()
        with mock.patch.object(entrypoint, "run_runner",
                               self._runner_writing_the_state(0, "success", ["ok", "ok"])), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        packed = self._handovers()
        self.assertEqual(len(packed), 1, packed)
        (self.app / entrypoint.HANDOVER / packed[0]).unlink()
        for _ in range(2):
            with mock.patch.object(entrypoint, "run_runner",
                                   self._runner_writing_the_state(0, "success",
                                                                  ["skipped", "skipped"])), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
            self.assertEqual(self._handovers(), [])
            self.assertIn(f"Every step was already complete, and no handover was packed. "
                          f"{packed[0]}", err.getvalue())

    def test_without_room_for_the_data_root_the_completed_run_packs_its_record(self):
        """The archive of the whole data root could fill the volume on which the cloning step
        stops when the volume runs low. The record, the logs and the lists are small. The trees
        follow when they fit, as after a run that did not complete, and the log says that the
        data root remains in the project folder. Because the stub writes the state and the
        sample list before the size is measured, the test writes them here first."""
        self._runner(0, "success", ["ok"])(["runner"], self.data)
        size = entrypoint.tree_bytes(self.data)
        room = shutil.disk_usage(self.app)._replace(free=2 * size - 1)
        with mock.patch.object(entrypoint.shutil, "disk_usage", return_value=room):
            returned, said = self._logged_main(0, "success", steps=["ok"])
        self.assertEqual(returned, 0)
        handovers = self._handovers()
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["complete-trees.7z", "complete-without-sample.7z"], handovers)
        record = next(h for h in handovers if h.endswith("-without-sample.7z"))
        members = self._members(record)
        self.assertIn(f"{entrypoint.DATA}/{entrypoint.STATE_FILE}", members)
        self.assertNotIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md", members)
        self.assertIn("Did not pack the whole data root", said)
        self.assertIn("without the collected and validated trees", said)
        # Because no archive contains the data root, a Start with room packs it although it
        # skips every step.
        self.assertEqual(self._main(0, "success", steps=["skipped"]), 0)
        self.assertEqual(len([h for h in self._handovers() if h.endswith("Z-complete.7z")]), 1)

    def test_the_archives_in_the_handover_directory_are_counted_and_none_is_deleted(self):
        earlier = []
        for name, content in (("agentcontext-handover-20260801T000000Z-complete.7z", 3 * b"x"),
                              ("agentcontext-handover-20260901T000000Z-failed.7z", 5 * b"x")):
            earlier.append(self.app / entrypoint.HANDOVER / name)
            earlier[-1].write_bytes(content)
        returned, said = self._logged_main(0, "success", steps=["ok"])
        self.assertEqual(returned, 0)
        self.assertIn("holds 2 archive(s) of earlier runs", said)
        for path in earlier:
            self.assertTrue(path.is_file(), f"{path.name} was deleted")
        self.assertEqual(len(self._handovers()), 3)

    def _handovers(self) -> list:
        return sorted(p.name for p in (self.app / entrypoint.HANDOVER).iterdir()
                      if p.name.endswith(".7z"))

    def test_each_archive_has_a_checksum_file_that_sha256sum_accepts(self):
        """The file beside an archive lets you check a download against the archive that the
        entrypoint packed. A run that failed packs its lists and its trees into two archives,
        and each has a file of its own."""
        said = self._failing_start()
        handover = self.app / entrypoint.HANDOVER
        archives = self._handovers()
        self.assertEqual(len(archives), 2, archives)
        check = ["sha256sum", "-c"] if shutil.which("sha256sum") else ["shasum", "-a", "256", "-c"]
        for name in archives:
            with self.subTest(archive=name):
                digest = hashlib.sha256((handover / name).read_bytes()).hexdigest()
                self.assertEqual((handover / f"{name}.sha256").read_text(encoding="utf-8"),
                                 f"{digest}  {name}\n")
                self.assertIn(f"The SHA-256 of {name} is {digest}, recorded in {name}.sha256 "
                              f"beside it.", said)
                checked = subprocess.run([*check, f"{name}.sha256"], cwd=handover,
                                         capture_output=True, text=True)
                self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertFalse(list(handover.glob("*.partial")))

    def test_a_checksum_that_cannot_be_written_fails_no_handover(self):
        """A directory in the place of the checksum file makes its rename fail. The archive is
        complete without the file, and the completed run's handover is recorded."""
        name = "agentcontext-handover-20261028T000000Z-complete.7z"
        (self.app / entrypoint.HANDOVER / f"{name}.sha256").mkdir()
        with mock.patch.object(entrypoint, "timestamp", return_value="20261028T000000Z"):
            returned, said = self._logged_main(0, "complete")
        self.assertEqual(returned, 0)
        self.assertEqual(self._handovers(), [name])
        self.assertEqual(entrypoint.recorded_handover(self.data, "complete"), name)
        self.assertIn(f"Could not record the SHA-256 of {name} (", said)
        self.assertFalse(list((self.app / entrypoint.HANDOVER).glob("*.partial")))

    def _members(self, archive: str) -> list:
        listing = subprocess.run([entrypoint.SEVEN_ZIP, "l", "-slt",
                                  str(self.app / entrypoint.HANDOVER / archive)],
                                 capture_output=True, text=True, check=True).stdout
        return [line[len("Path = "):] for line in listing.splitlines()
                if line.startswith("Path = ")][1:]

    def test_a_completed_run_is_started_on_the_data_root_and_hands_it_over(self):
        self.assertEqual(self._main(0, "complete"), 0)
        self.assertEqual(len(self.started), 1)
        started = self.started[0]
        self.assertEqual(started["command"], entrypoint.runner_argv(["--with-commit-reclassification"]))
        self.assertEqual(started["cwd"], self.data)
        # The package's data root is the volume, and every temporary file goes there.
        self.assertEqual(started["AGENTCONTEXT_DATA_ROOT"], str(self.data))
        self.assertEqual(started["TMPDIR"], str(self.stage / "_scratch"))
        self.assertTrue((self.stage / "_scratch").is_dir())
        # The machine record and the heuristics files are written before the runner starts.
        self.assertTrue(started["machine"], "the machine record was not written first")
        record = json.loads((self.data / entrypoint.MACHINE_RECORD).read_text())
        self.assertEqual(record["state_volume"]["path"], str(self.data))
        for name in ("config_path_patterns.csv", "not_ai_commits.csv"):
            with self.subTest(heuristics=name):
                self.assertEqual((self.data / entrypoint.HEURISTICS_RECORD / name).read_bytes(),
                                 (self.image / "heuristics" / name).read_bytes())
        handovers = self._handovers()
        self.assertEqual(len(handovers), 1, handovers)
        self.assertRegex(handovers[0], r"^agentcontext-handover-\d{8}T\d{6}Z-complete\.7z$")
        members = self._members(handovers[0])
        for member in (f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                       f"{entrypoint.STAGE}/output/repos.csv",
                       f"{entrypoint.DATA}/{entrypoint.MACHINE_RECORD.as_posix()}",
                       f"{entrypoint.DATA}/classification/repos_annotated_included.csv"):
            with self.subTest(member=member):
                self.assertIn(member, members)
        self.assertFalse([m for m in members if m.startswith(f"{entrypoint.STAGE}/_scratch")])

    def test_a_listing_names_the_paths_under_the_mounted_data_root(self):
        """`--list` execs the runner before the checks, with the mounted data root named, so
        that every path that it prints, such as the scratch directory of the cloning step, is
        under the mount rather than the image's own `data/`."""
        seen = {}

        def execv(path, argv):
            seen.update(argv=argv, root=os.environ.get("AGENTCONTEXT_DATA_ROOT"))
            raise SystemExit(0)
        with mock.patch.object(entrypoint.os, "execv", execv), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            entrypoint.main(["--list"])
        self.assertEqual(seen["root"], str(self.data))
        self.assertEqual(seen["argv"], entrypoint.runner_argv(["--list"]))

    def test_a_first_start_over_an_empty_data_root_runs(self):
        """The bundle contains an empty data root, and the pipeline fills it, beginning with the
        download of the GHS retrieval."""
        shutil.rmtree(self.data)
        self.data.mkdir()
        self.assertEqual(self._main(0, "complete"), 0)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0]["cwd"], self.data)

    def test_a_stopped_run_hands_over_the_record_and_then_the_trees(self):
        """The collected and validated trees of a run that did not complete are handed over,
        because they contain what a failed step left. The first archive is packed before the
        trees and without them. A packing of the trees that does not finish then leaves the
        first archive complete."""
        (self.stage / "data" / "validated" / "o§r").mkdir(parents=True)
        (self.stage / "data" / "validated" / "o§r" / "CLAUDE.md").write_text("# r\n")
        self.assertEqual(self._main(130, "stopped"), 130)
        handovers = self._handovers()
        self.assertEqual(len(handovers), 2, handovers)
        record, trees = (h for h in handovers if not h.endswith("-trees.7z")), \
            (h for h in handovers if h.endswith("-trees.7z"))
        record, trees = next(record), next(trees)
        self.assertTrue(record.endswith("-stopped.7z"), handovers)
        self.assertEqual(trees, record.replace("-stopped.7z", "-stopped-trees.7z"))
        members = self._members(record)
        self.assertIn(f"{entrypoint.DATA}/{entrypoint.STATE_FILE}", members)
        self.assertIn(f"{entrypoint.STAGE}/output/repos.csv", members)
        self.assertIn(f"{entrypoint.DATA}/classification/repos_annotated_included.csv", members)
        self.assertNotIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md", members)
        members = self._members(trees)
        for member in (f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                       f"{entrypoint.STAGE}/data/validated/o§r/CLAUDE.md"):
            with self.subTest(member=member):
                self.assertIn(member, members)
        self.assertFalse([m for m in members if "/output/" in m],
                         "the files of the first archive went into the trees archive")

    def test_the_trees_are_left_out_on_a_volume_without_room_for_them(self):
        """The cloning step stops when the volume runs low. Packing the sample onto that
        volume would fill it."""
        full = shutil.disk_usage(self.app)._replace(free=1)
        with mock.patch.object(entrypoint.shutil, "disk_usage", return_value=full), \
                mock.patch.object(entrypoint, "log") as logged:
            self.assertEqual(self._main(1, "failed"), 1)
        handovers = self._handovers()
        self.assertEqual(len(handovers), 1, handovers)
        self.assertTrue(handovers[0].endswith("-failed.7z"), handovers)
        said = " ".join(str(c.args[0]) for c in logged.call_args_list)
        self.assertIn("Did not pack the collected and validated trees", said)

    def test_the_trees_are_packed_with_twice_their_size_free_and_not_with_less(self):
        """With free space of at least the trees' size but less than twice that size, the
        archive of the trees would fill the volume on which the cloning step stops when the
        volume runs low."""
        size = entrypoint.tree_bytes(self.stage / "data" / "collected")
        handover = self.app / "handover"
        for free, packed in ((2 * size - 1, False), (size + 1, False), (2 * size, True)):
            room = shutil.disk_usage(handover)._replace(free=free)
            with self.subTest(free=free), \
                    mock.patch.object(entrypoint.shutil, "disk_usage", return_value=room), \
                    mock.patch.object(entrypoint, "log"):
                archive = entrypoint.pack_trees(self.app, handover, "failed", f"w{free}")
                self.assertEqual(archive is not None and archive.is_file(), packed)

    def test_a_tree_pack_failure_preserves_and_identifies_the_record(self):
        with mock.patch.object(entrypoint, "pack_trees", side_effect=OSError("disk full")), \
                mock.patch.object(entrypoint, "log_error") as logged:
            self.assertEqual(self._main(1, "failed"), 1)
        archives = self._handovers()
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].endswith("-failed.7z"))
        self.assertIn(f"{entrypoint.DATA}/{entrypoint.STATE_FILE}", self._members(archives[0]))
        message = logged.call_args.args[0]
        self.assertIn("disk full", message)
        self.assertIn("The handover without them is complete", message)

    def _failing_start(self) -> str:
        """One Start with a run that fails, and what the entrypoint logged."""
        with mock.patch.object(entrypoint, "run_runner", self._runner(1, "failed")), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 1)
        return err.getvalue()

    def _trees_archives(self) -> list:
        return [h for h in self._handovers() if h.endswith("-trees.7z")]

    def test_failing_starts_one_after_another_pack_the_trees_once(self):
        """A step that fails at every Start, such as a step of the analysis stage, would otherwise
        put another copy of the collected and validated trees on the handover volume at each Start,
        until the volume has less free space than the pre-checks require. The first archive
        of each Start, with the pipeline execution state and the logs that the Start wrote, is
        packed every time."""
        self._later_seconds()
        self._failing_start()
        said = self._failing_start()
        suffixes = sorted(h.rsplit("Z-", 1)[1] for h in self._handovers())
        self.assertEqual(suffixes, ["failed-trees.7z", "failed.7z", "failed.7z"],
                         self._handovers())
        self.assertIn(f"Did not pack the collected and validated trees again, because "
                      f"{self._trees_archives()[0]} in the handover directory contains them as "
                      f"they are.", said)

    def test_the_trees_are_packed_again_when_they_changed_or_their_archive_is_gone(self):
        """A directory that DSM writes into the trees changes nothing that an archive contains,
        and neither does the time of the directory that it writes into. A changed file, and an
        archive that you deleted, make the next failing Start pack the trees."""
        self._later_seconds()
        repository = self.stage / "data" / "collected" / "o§r"
        self._failing_start()
        (repository / "@eaDir" / "CLAUDE.md@SynoEAStream").parent.mkdir()
        (repository / "@eaDir" / "CLAUDE.md@SynoEAStream").write_text("thumbnail\n")
        self._failing_start()
        self.assertEqual(len(self._trees_archives()), 1, self._handovers())
        (repository / "CLAUDE.md").write_text("# changed by a resumed collection\n")
        self._failing_start()
        self.assertEqual(len(self._trees_archives()), 2, self._handovers())
        for archive in self._trees_archives():
            (self.app / entrypoint.HANDOVER / archive).unlink()
        self._failing_start()
        self.assertEqual(len(self._trees_archives()), 1, self._handovers())

    def test_a_failed_handover_of_a_run_whose_analysis_failed_does_not_hold_back_the_month(self):
        """The resumed run fails in the analysis stage after its release step, and the packing
        of its small `-failed` archive fails. The archive of its data root contains the same
        pipeline execution state and logs. That archive is packed before the next monthly run,
        which then begins."""
        self._later_seconds()
        self._killed_run(40)
        real = entrypoint._run_7z
        calls = []

        def first_fails(args, cwd, **options):
            calls.append(args)
            return (2, "Error: a file changed while it was read") if len(calls) == 1 \
                else real(args, cwd, **options)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts(self.ANALYSIS_FAILED, (0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "_run_7z", first_fails), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]), 0)
        self.assertEqual(self._started_with(),
                         [["--with-commit-reclassification"], self.NEXT_MONTH])
        before = self.started[1]["handovers"]
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in before], ["complete.7z"], before)
        self.assertIn(f"{entrypoint.STAGE}/data/collected/o§r/CLAUDE.md",
                      self._members(before[0]))
        said = err.getvalue()
        self.assertIn("The handover of the run that just ended could not be packed. That run "
                      "failed in rq2_upset_plot after its release step", said)
        self.assertNotIn("The next monthly run would replace the data root of that run", said)

    def _stopped_7z(self, args, cwd, **options):
        """A `_run_7z` stub for a Stop that arrives while 7zz packs. 7zz is killed and returns 128
        plus the signal."""
        entrypoint.note_stop(signal.SIGTERM)
        return 128 + signal.SIGTERM, ""

    def test_a_stop_while_a_completed_run_is_packed_exits_as_stopped(self):
        """The runner exited 0, and the Stop ended 7zz. The container exits as after every other
        Stop rather than with 1, which is the exit code of a packing that failed for a cause
        that you correct."""
        with mock.patch.object(entrypoint, "_run_7z", self._stopped_7z):
            returned, said = self._logged_main(0, "success", steps=["ok"])
        self.assertEqual(returned, 128 + signal.SIGTERM)
        self.assertEqual(self._handovers(), [])
        self.assertIn(f"The handover was not packed, because a Stop (signal "
                      f"{signal.SIGTERM:d}) ended 7zz", said)
        self.assertNotIn("ERROR", said)

    def test_a_stop_while_a_resumed_run_is_packed_exits_as_stopped(self):
        """The resumed run completed, and the next month is due. The Stop ends 7zz during the
        packing of its `-complete` archive, and the start begins no next monthly run."""
        self._later_seconds()
        self._killed_run(40)
        with mock.patch.object(entrypoint, "run_runner",
                               self._starts((0, "success", ["ok"]), (0, "success", ["ok"]))), \
                mock.patch.object(entrypoint, "_run_7z", self._stopped_7z), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(entrypoint.main(["--with-commit-reclassification"]),
                             128 + signal.SIGTERM)
        self.assertEqual(self._started_with(), [["--with-commit-reclassification"]])
        self.assertEqual(self._handovers(), [])
        said = err.getvalue()
        self.assertIn(f"A Stop (signal {signal.SIGTERM:d}) arrived during this Start. The next "
                      f"monthly run does not begin.", said)
        self.assertNotIn("could not be packed", said)

    def test_a_killed_runner_is_handed_over_and_exits_as_the_signal(self):
        """A signal death leaves the pipeline execution state unchanged and still gets a
        handover."""
        self.assertEqual(self._main(-signal.SIGKILL, None), 128 + signal.SIGKILL)
        handovers = self._handovers()
        self.assertEqual([h.rsplit("Z-", 1)[1] for h in handovers],
                         ["killed-trees.7z", "killed.7z"], handovers)

    def test_a_start_the_runner_refused_has_no_handover(self):
        """The runner exited 1 and wrote no pipeline execution state, which is a refusal before
        any step."""
        self.assertEqual(self._main(1, None), 1)
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self._handovers(), [])

    def test_a_held_lock_refuses_before_anything_is_written(self):
        with _held_run_lock(self.app, "pid 7 on nas"):
            self.assertEqual(self._main(0, "complete"), 1)
        self.assertEqual(self.started, [])
        self.assertFalse((self.stage / "_scratch").exists())
        self.assertFalse((self.data / entrypoint.MACHINE_RECORD).exists())
        self.assertFalse((self.data / entrypoint.HEURISTICS_RECORD).exists())
        self.assertEqual(self._handovers(), [])


class TestTheConversionOfTheCloningRecords(unittest.TestCase):
    """A data root of revision 2, as the runner of the image with the backup providers leaves it, gets the digest of each
    input of the recorded cloning step and the record of the collection that the step ended
    (`convert_cloning_records`). The runner then runs the cloning step after a later change of
    only the path heuristics, and the step continues that collection rather than begin a new
    one, which would query every head again and collect again every repository with a head
    that moved."""

    NAME = "2026-11-04T120000Z-0123abcd"
    EARLIER = "2026-10-01T120000Z-ffffffff"
    # When the start of the runner that ran the cloning step to its exit 0 began, and when that
    # step collected a repository, as the pipeline execution state and a row record them.
    STARTED_AT = "2026-11-05T09:00:00.250000+00:00"
    SCANNED_AT = "2026-11-05T09:30:00Z"

    def setUp(self):
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from agentcontext import collector, heuristics, resume, reuse, runner
        self.resume, self.runner = resume, runner
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = self.tmp / "data"
        self.stage = self.data / "collection"
        collected = self.stage / "data" / "collected"
        (collected / "o\u00a7a").mkdir(parents=True)
        (collected / "o\u00a7a" / "CLAUDE.md").write_text("# a\n")
        (collected / "repos.csv").write_text("repo_name\no/a\no/b\n")
        (self.stage / "data" / "imported").mkdir(parents=True)
        for name in ("file_history.csv", "submodules.csv", "ai_authorship.csv"):
            (self.stage / name).write_text("repo_name\no/a\n")
        self.sample = self.data / entrypoint.SAMPLE_LIST
        self.sample.parent.mkdir(parents=True)
        self.sample.write_text("repo_name,label\no/b,true\no/a,true\nnot a name,true\n")
        self.finished = self.stage / resume.FINISHED_NAME
        patcher = mock.patch.object(runner, "STAGE_DIR", self.stage)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.step = next(s for s in runner.CORE_STEPS if s.key == entrypoint.CLONING_STEP)
        # What a line records, as the cloning step computes it.
        config = collector.CollectionConfig(
            collected_dir=collected, clone_dir=self.tmp / "clones", log_dir=self.tmp / "logs",
            vendored_dirs=heuristics.load_vendored_dirs(),
            heuristics_dir=self.stage / reuse.HEURISTICS_DIR_NAME)
        self.under = reuse.current(config)
        _record_revision(self.data, 2)

    def _lines(self, name: str = NAME, scanned_at: str = SCANNED_AT,
               repos: tuple = ("o/a", "o/b"), outcome: str = "ok", **options) -> None:
        """A line of the collection `name` for each repository of `repos`, the sample by default,
        with the row of a repository that the cloning step collected at `scanned_at`, the
        outcome `outcome` and `options` as `resume.append_finished` takes them."""
        from agentcontext import paths
        for repo in repos:
            row = paths.build_csv_row(repo, scanned_at, "main", "a" * 40, "", "", set())
            fail_row = (None if outcome == "ok"
                        else f"{repo},https://github.com/{repo},{outcome}")
            self.resume.append_finished(self.finished, repo, row, fail_row,
                                        collected_under=self.under, collection=name, **options)

    def _running(self, state: "dict | None" = None, started_at: str = STARTED_AT) -> dict:
        """The pipeline execution state `state`, or one without a record of the cloning step,
        with the cloning step named as the running step of the start that began at `started_at`,
        as the runner of the image with the backup providers writes it just before the step starts."""
        state = state or {"outcome": "running", "completed": {}}
        state.update({"outcome": "running", "started_at": started_at,
                      entrypoint.RUNNING_STEP: entrypoint.CLONING_STEP,
                      entrypoint.RUNNING_STEP_STARTED_AT: started_at,
                      entrypoint.RUNNING_STEP_RESULTS: []})
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state, indent=2) + "\n",
                                                       encoding="utf-8")
        return state

    def _recorded(self, how: str = "ran") -> dict:
        """The pipeline execution state with the cloning step recorded as the runner of
        the image with the backup providers records it: the hash of its inputs without the digest of each."""
        with contextlib.redirect_stderr(io.StringIO()):
            record = self.runner.step_fingerprint(self.step)
        self.assertIn("+inputs:", record)
        state = {"outcome": "success", "completed": {entrypoint.CLONING_STEP: record},
                 "completed_by": {entrypoint.CLONING_STEP: {
                     "fingerprint": record, "how": how,
                     "finished_at": "2026-11-05T10:00:00+00:00", "git_sha": "8f4c6aa8",
                     "git_dirty": False}}}
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state, indent=2) + "\n",
                                                       encoding="utf-8")
        return state

    def _files(self) -> dict:
        return {path.relative_to(self.data).as_posix(): path.read_bytes()
                for path in sorted(self.data.rglob("*")) if path.is_file()
                and path.name != Path(entrypoint.DATA_ROOT_RECORD).name}

    def _convert(self) -> str:
        with contextlib.redirect_stderr(io.StringIO()):
            said = entrypoint.convert_data_root(self.data)
        self.assertEqual(json.loads((self.data / entrypoint.DATA_ROOT_RECORD).read_text())
                         ["revision"], entrypoint.DATA_ROOT_REVISION)
        return said

    def _edit_path_heuristics(self) -> Path:
        """A copy of the heuristics with another path in the first row of the path
        heuristics."""
        edited = self.tmp / "heuristics"
        shutil.copytree(ROOT / "heuristics", edited)
        path = edited / "config_path_patterns.csv"
        with path.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
        rows[0]["path"] = "ELSEWHERE.md"
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        return edited

    def test_the_conversion_repeats_what_it_reads_of_the_package(self):
        """The script repeats the labels and the hash of the inputs, the names of the records,
        the list of the sample and the form of a repository name rather than importing them, and
        this test checks the copies against the package."""
        from agentcontext import reuse
        from agentcontext.steps import repository_cloning
        with contextlib.redirect_stderr(io.StringIO()):
            pairs = self.runner.collection_inputs(self.step)
            parsed = repository_cloning.parse_repo_csv(self.sample, entrypoint.SAMPLE_COLUMN)
        self.assertEqual(entrypoint.CLONING_INPUTS, tuple(label for label, _ in pairs))
        self.assertEqual(entrypoint._inputs_part(pairs), self.runner.inputs_part(pairs))
        self.assertTrue(self.runner.inputs_part(pairs).startswith(entrypoint.INPUTS_PART))
        self.assertEqual(entrypoint.FINISHED_LIST, f"collection/{self.resume.FINISHED_NAME}")
        self.assertEqual(entrypoint.EXITED, self.runner.EXITED)
        self.assertEqual(self.data / entrypoint.COLLECTION_RECORD,
                         self.resume.collection_path(self.finished))
        self.assertEqual(self.data / entrypoint.ENDED_RECORD,
                         self.resume.ended_path(self.finished))
        self.assertEqual(self.sample.resolve(),
                         (self.stage / self.runner.DEFAULT_REPO_CSV).resolve())
        self.assertEqual(entrypoint.REPOSITORY_NAME.pattern,
                         repository_cloning._REPO_PATTERN.pattern)
        self.assertEqual(entrypoint._sample_repositories(self.sample), sorted(parsed))
        self.assertLessEqual(set(entrypoint.COLLECTED_UNDER), set(self.under))
        # The digests that a line records are the digests by which the runner records the step.
        self.assertEqual([self.under[key] for key in entrypoint.COLLECTED_UNDER],
                         [value for _, value in pairs[:4]])
        self.assertEqual(pairs[4][1], self.resume.list_digest(["o/a", "o/b"]))
        self.assertEqual(reuse.HEURISTICS_DIR_NAME, "collection_heuristics")
        # The field of a row that records when the cloning step collected the repository, and
        # the forms of that time and of the name of a collection.
        from agentcontext import collector, paths
        self.assertEqual(paths.COLLECTED_CSV_HEADER.split(",")[entrypoint.SCANNED_AT_FIELD],
                         "scanned_at")
        self.assertIsNotNone(entrypoint._parsed_time(collector.utc_now(), entrypoint.SCANNED_AT))
        with contextlib.redirect_stderr(io.StringIO()):
            name = self.resume.open_collection(self.tmp / self.resume.FINISHED_NAME,
                                               ["o/a"])["collection"]
        began = entrypoint.COLLECTION_BEGAN.match(name)
        self.assertIsNotNone(began)
        self.assertIsNotNone(entrypoint._parsed_time(began.group(),
                                                     entrypoint.COLLECTION_BEGAN_FORMAT))

    def test_a_collection_that_ended_under_the_image_with_the_backup_providers_is_continued_after_a_new_heuristic(
            self):
        """After the collection, the record of the cloning step names the digest of each input,
        and the record of the collection that ended names the collection of the lines, the
        list and its repositories. The runner then runs the step after a change of the path
        heuristics, which continues that collection. A later start converts nothing again."""
        self._lines(self.EARLIER, "2026-10-01T13:00:00Z")
        self._lines()
        state = self._recorded()
        self.assertFalse(self.resume.ended_path(self.finished).exists(),
                         "without the conversion, the step would begin a new collection")
        said = self._convert()
        self.assertIn(f"Rewrote 2 stored files ({entrypoint.STATE_FILE} and "
                      f"{entrypoint.ENDED_RECORD}).", said)
        converted = entrypoint.read_state(self.data)
        with contextlib.redirect_stderr(io.StringIO()):
            pairs = self.runner.collection_inputs(self.step)
        self.assertEqual(converted["completed_by"][entrypoint.CLONING_STEP]["inputs"],
                         [list(pair) for pair in pairs])
        del converted["completed_by"][entrypoint.CLONING_STEP]["inputs"]
        self.assertEqual(converted, state)
        self.assertEqual(self.resume.read_collection(self.resume.ended_path(self.finished)),
                         {"collection": self.NAME,
                          "input_sha256": self.resume.list_digest(["o/a", "o/b"]),
                          "taken_over": set(), "repositories": ["o/a", "o/b"]})
        with mock.patch.object(self.runner, "HEURISTICS_DIR", self._edit_path_heuristics()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            done = self.runner.completed_steps([self.step], entrypoint.read_state(self.data))
        self.assertNotIn(entrypoint.CLONING_STEP, done)
        self.assertIn("INFO runner: The path heuristics changed since repository_cloning ran. "
                      "Running repository_cloning under the changed inputs.", err.getvalue())
        files = self._files()
        _record_revision(self.data, 2)
        self.assertEqual(self._convert(), f"Converted the data root /app/{entrypoint.DATA} from "
                                          f"revision 2 to revision "
                                          f"{entrypoint.DATA_ROOT_REVISION}. No stored file "
                                          f"needed a change.")
        self.assertEqual(self._files(), files)
        with contextlib.redirect_stderr(io.StringIO()):
            continued = self.resume.open_collection(self.finished, ["o/a", "o/b"])
        self.assertEqual(continued["collection"], self.NAME)

    def _exited(self, inputs: str, state: "dict | None" = None) -> dict:
        """The pipeline execution state `state`, or one without a record of the cloning step, with
        the record of an exit of the cloning step under the inputs part `inputs`, as the runner of
        the image with the backup providers writes it when the step has exited 0, before it computes
        the fingerprint of the collected tree."""
        state = state or {"outcome": "running", "completed": {}}
        state[entrypoint.EXITED] = {
            "step": entrypoint.CLONING_STEP, "inputs": inputs,
            "code": {"git_sha": "8f4c6aa8", "git_dirty": False}, "script": "x",
            "finished_at": "2026-11-05T10:00:00+00:00"}
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state, indent=2) + "\n",
                                                       encoding="utf-8")
        return state

    def test_a_cloning_step_in_the_record_of_an_exit_is_converted_as_a_recorded_one(self):
        """A kill or a power loss after the cloning step of the image with the backup providers
        exited 0, while its runner computed the fingerprint of the collected tree, leaves the step
        in the record of the exit with the inputs part that it ran under, and no record of the step
        or of the collection that it ended. The conversion names the digest of each input in the
        record of the exit and writes the record of the collection that ended. The next start
        records the step from its output with the digests, and a later change of the path heuristics
        runs the step, which continues the collection that ended."""
        self._lines(self.EARLIER, "2026-10-01T13:00:00Z")
        self._lines()
        with contextlib.redirect_stderr(io.StringIO()):
            inputs = self.runner.recorded_inputs_fingerprint(self.step)
            pairs = self.runner.collection_inputs(self.step)
        state = self._exited(inputs)
        self.assertIn(f"Rewrote 2 stored files ({entrypoint.STATE_FILE} and "
                      f"{entrypoint.ENDED_RECORD}).", self._convert())
        converted = entrypoint.read_state(self.data)
        self.assertEqual(converted[entrypoint.EXITED].pop(entrypoint.EXITED_PAIRS),
                         [list(pair) for pair in pairs])
        self.assertEqual(converted, state)
        self.assertEqual(self.resume.read_collection(self.resume.ended_path(self.finished)),
                         {"collection": self.NAME,
                          "input_sha256": self.resume.list_digest(["o/a", "o/b"]),
                          "taken_over": set(), "repositories": ["o/a", "o/b"]})
        state = entrypoint.read_state(self.data)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(self.runner.record_exited_step([self.step], state))
        self.assertEqual(self.runner.recorded_inputs(state, entrypoint.CLONING_STEP),
                         [tuple(pair) for pair in pairs])
        with mock.patch.object(self.runner, "HEURISTICS_DIR", self._edit_path_heuristics()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertNotIn(entrypoint.CLONING_STEP,
                             self.runner.completed_steps([self.step], state))
        self.assertIn("INFO runner: The path heuristics changed since repository_cloning ran.",
                      err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()):
            continued = self.resume.open_collection(self.finished, ["o/a", "o/b"])
        self.assertEqual(continued["collection"], self.NAME)

    def test_a_record_of_an_exit_over_another_list_writes_no_record_of_a_collection(self):
        """The record of an exit of the cloning step describes the last run of the step, which
        came after the run that the record of the step names. Where the digests do not give its
        inputs part, as after a run over another list with `--repo-csv`, nothing shows which
        list the collection that ended collected. The conversion then names the digests in the
        record of the step that they give, from an earlier collection over the list, and writes
        no record of an ended collection."""
        self._lines()
        self.sample.write_text("repo_name\no/a\no/b\no/c\n")
        with contextlib.redirect_stderr(io.StringIO()):
            other = self.runner.recorded_inputs_fingerprint(self.step)
        self.sample.write_text("repo_name\no/a\no/b\n")
        self._exited(other, self._recorded())
        self.assertIn(f"Rewrote 1 stored file ({entrypoint.STATE_FILE}).", self._convert())
        converted = entrypoint.read_state(self.data)
        self.assertIn("inputs", converted["completed_by"][entrypoint.CLONING_STEP])
        self.assertNotIn(entrypoint.EXITED_PAIRS, converted[entrypoint.EXITED])
        self.assertFalse(self.resume.ended_path(self.finished).exists())

    def test_a_collection_that_closed_before_the_record_of_the_exit_is_continued(self):
        """The cloning step of the image with the backup providers exited 0 and closed its
        collection, which removed the record of the collection, and a kill or a power loss ended the
        runner while it synced the outputs of the step, before it wrote the record of the exit. The
        state names the step as the running step, with no record of it. The latest collection wrote
        lines during that start, after its earlier starts, or began during it and kept the rows of
        an earlier collection, as a retry that failed keeps them. The conversion writes the record
        of the collection that ended, and the step, run again, continues that collection."""
        later = "2026-11-05T091500Z-4567cdef"
        for case, name, scanned_at in (("resumed in the start", self.NAME, self.SCANNED_AT),
                                       ("began in the start", later, "2026-10-01T13:00:00Z")):
            with self.subTest(case=case):
                self.setUp()
                self._lines(self.EARLIER, "2026-10-01T13:00:00Z")
                self._lines(name, scanned_at)
                self._running()
                state = (self.data / entrypoint.STATE_FILE).read_bytes()
                self.assertFalse(self.resume.ended_path(self.finished).exists())
                self.assertIn(f"Rewrote 1 stored file ({entrypoint.ENDED_RECORD}).",
                              self._convert())
                self.assertEqual((self.data / entrypoint.STATE_FILE).read_bytes(), state)
                self.assertEqual(
                    self.resume.read_collection(self.resume.ended_path(self.finished)),
                    {"collection": name, "input_sha256": self.resume.list_digest(["o/a", "o/b"]),
                     "taken_over": set(), "repositories": ["o/a", "o/b"]})
                with contextlib.redirect_stderr(io.StringIO()):
                    continued = self.resume.open_collection(self.finished, ["o/a", "o/b"])
                self.assertEqual(continued["collection"], name)

    def test_a_start_that_a_kill_ended_before_its_collection_began_continues_none(self):
        """A monthly run of the image with the backup providers, whose `--from repository_cloning`
        dropped the record of the cloning step, ran the step after the collection of the month
        before had ended, and a kill ended the start before the step began its new collection. The
        state names the step as the running step, with no record of it, and the lines of the
        collection of the month before cover the sample. That collection wrote every line before the
        start, and the conversion writes no record of a collection that ended, so that the step, run
        again, begins the new snapshot. Nor does it where a repository in the sample has no line,
        where the latest collection wrote a line for a repository outside the sample, where the
        running step is another step, where a record of an exit names the step without the digests
        of its inputs, and where `--mark-complete repository_cloning` recorded the step after the
        kill, which runs no step and leaves the running step in the state."""
        cases = {"the monthly start": {},
                 "a repository in the sample without a line": {"sample": ("o/a", "o/b", "o/c")},
                 "a line outside the sample": {"outside": True},
                 "another running step": {"running": "file_history"},
                 "a record of an exit over other inputs": {"exited": "inputs:" + "0" * 64},
                 "a step marked complete after the kill": {"marked": True}}
        for case, options in cases.items():
            with self.subTest(case=case):
                self.setUp()
                if case == "the monthly start":
                    self._lines(scanned_at="2026-11-04T13:00:00Z")
                    state = self._recorded()
                    # `--from repository_cloning` drops the record of the step and leaves the
                    # entry that names what it came from, which no longer counts.
                    del state["completed"][entrypoint.CLONING_STEP]
                elif options.get("marked"):
                    self._lines()
                    state = self._recorded(how="marked_complete")
                else:
                    self._lines()
                    state = None
                if options.get("outside"):
                    self._lines(repos=("o/z",))
                if "sample" in options:
                    self.sample.write_text("repo_name\n" + "\n".join(options["sample"]) + "\n")
                state = self._running(state)
                if "running" in options:
                    state[entrypoint.RUNNING_STEP] = options["running"]
                if "exited" in options:
                    state[entrypoint.EXITED] = {
                        "step": entrypoint.CLONING_STEP, "inputs": options["exited"],
                        "code": {"git_sha": "8f4c6aa8", "git_dirty": False}, "script": "x",
                        "finished_at": "2026-11-05T10:00:00+00:00"}
                (self.data / entrypoint.STATE_FILE).write_text(json.dumps(state, indent=2) + "\n")
                files = self._files()
                if options.get("marked"):
                    # The conversion adds the digest of each input to the record of the step,
                    # which was marked complete and therefore ended no collection.
                    self._convert()
                    self.assertFalse((self.data / entrypoint.ENDED_RECORD).exists())
                    continue
                self.assertIn("No stored file needed a change.", self._convert())
                self.assertEqual(self._files(), files)
                if case == "the monthly start":
                    with contextlib.redirect_stderr(io.StringIO()) as err:
                        again = self.resume.open_collection(self.finished, ["o/a", "o/b"])
                    self.assertNotEqual(again["collection"], self.NAME)
                    self.assertIn("INFO repository_cloning: Starting the collection",
                                  err.getvalue())

    def test_the_record_of_the_collection_that_ended_names_what_it_took_over(self):
        """A collection of the image with the backup providers took o/b over from an earlier
        collection after a head query, as every monthly run does for the repositories with an
        unmoved head, and its record, which the end of the collection removed, listed o/b. The line
        of o/b names the earlier collection, and so do the line of o/c, taken over while git
        returned no head commit, the lines of o/d and o/e, which the size bound and the time bound
        stopped and which a collection queries no head for, and the failed line of o/f, which GitHub
        refused and which carries no mark whether the image with the backup providers confirmed the
        refusal or git returned no head commit. The converted record lists o/b, and the continued
        collection queries the heads of o/c and o/f."""
        from agentcontext import collector, heuristics, reuse
        from agentcontext.git import clone
        repos = ["o/a", "o/b", "o/c", "o/d", "o/e", "o/f"]
        self.sample.write_text("repo_name\n" + "\n".join(repos) + "\n")
        earlier = "2026-10-01T13:00:00Z"
        self._lines(self.EARLIER, earlier, repos=("o/a", "o/b"))
        self._lines(self.EARLIER, earlier, repos=("o/f",), outcome=clone.NOT_FOUND_OR_NO_ACCESS)
        self._lines(self.EARLIER, earlier, repos=("o/c",), head_no_response=True)
        self._lines(self.EARLIER, earlier, repos=("o/d",), outcome="size_bound_exceeded",
                    size_bound=1_000)
        self._lines(self.EARLIER, earlier, repos=("o/e",), outcome="time_bound_exceeded",
                    time_bound=600.0)
        self._lines(repos=("o/a",))
        self._recorded()
        self._convert()
        with contextlib.redirect_stderr(io.StringIO()):
            collection = self.resume.open_collection(self.finished, repos)
        self.assertEqual((collection["collection"], sorted(collection["taken_over"])),
                         (self.NAME, ["o/b"]))
        config = collector.CollectionConfig(
            collected_dir=self.stage / "data" / "collected", clone_dir=self.tmp / "clones",
            log_dir=self.tmp / "logs", vendored_dirs=heuristics.load_vendored_dirs(),
            heuristics_dir=self.stage / reuse.HEURISTICS_DIR_NAME)
        queried = []

        def remote_heads(config, repos, *args, **kwargs):
            queried.extend(repos)
            return {repo: ("main", "b" * 40) for repo in repos}
        with mock.patch.object(reuse, "remote_heads", remote_heads), \
                contextlib.redirect_stderr(io.StringIO()):
            again = reuse.outdated(config, repos, self.resume.read_finished(self.finished),
                                   set(), self.under, collection=collection)
        self.assertEqual((sorted(queried), sorted(again)), (["o/c", "o/f"], ["o/c", "o/f"]))

    def test_during_the_collection_nothing_is_converted(self):
        """While the collection of the image with the backup providers runs, its record names it,
        and the cloning step is not recorded. The conversion changes no file, and the step resumes
        the collection."""
        digest = self.resume.list_digest(["o/a", "o/b"])
        self.resume.collection_path(self.finished).write_text(json.dumps(
            {"collection": self.NAME, "input_sha256": digest, "taken_over": []}) + "\n")
        self.resume.append_finished(self.finished, "o/a", "o/a,row", None,
                                    collected_under=self.under, collection=self.NAME)
        (self.data / entrypoint.STATE_FILE).write_text(json.dumps(
            {"outcome": "running", "completed": {"ghs_download": "output:x"}}, indent=2) + "\n")
        files = self._files()
        self.assertIn("No stored file needed a change.", self._convert())
        self.assertEqual(self._files(), files)
        with contextlib.redirect_stderr(io.StringIO()):
            resumed = self.resume.open_collection(self.finished, ["o/a", "o/b"])
        self.assertEqual(resumed["collection"], self.NAME)

    def test_a_record_that_the_digests_do_not_give_is_left_as_it_is(self):
        """A list that changed after the step was recorded, as after a labeling over another
        sampling frame, gives digests with another hash than the record. Nothing shows which
        list the collection collected, and the conversion changes nothing. A step that
        `--mark-complete` recorded gets the digest of each input and no record of an ended
        collection, because `--mark-complete` removes that record."""
        self._lines()
        self._recorded()
        self.sample.write_text("repo_name\no/a\no/b\no/c\n")
        files = self._files()
        self.assertIn("No stored file needed a change.", self._convert())
        self.assertEqual(self._files(), files)
        self.sample.write_text("repo_name\no/a\no/b\n")
        self._recorded("marked_complete")
        _record_revision(self.data, 2)
        self.assertIn(f"Rewrote 1 stored file ({entrypoint.STATE_FILE}).", self._convert())
        self.assertIn("inputs", entrypoint.read_state(self.data)["completed_by"]
                      [entrypoint.CLONING_STEP])
        self.assertFalse(self.resume.ended_path(self.finished).exists())


class TestThePackageReadsTheVolume(unittest.TestCase):
    """The package's data root is the mounted one, named in the environment.
    `TestTheEntrypointMain` checks the environment that the runner is started in."""

    def test_the_package_reads_the_variable(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, AGENTCONTEXT_DATA_ROOT=tmp)
            out = subprocess.run([sys.executable, "-c", "import agentcontext; "
                                  "print(agentcontext.DATA_ROOT); print(agentcontext.STAGE_DIR)"],
                                 env=env, capture_output=True, text=True, check=True).stdout.split()
            self.assertEqual([Path(line) for line in out],
                             [Path(tmp).resolve(), Path(tmp).resolve() / "collection"])


class TestTheDrainPeriod(unittest.TestCase):
    """The time that a killed runner's step gets to end covers the cloning step's own stop."""

    def test_the_drain_is_above_the_cloning_steps_stop_bound(self):
        from agentcontext.steps import repository_cloning
        from agentcontext.git import bounds
        bound = repository_cloning.WORKER_STOP_SECONDS + bounds.REAP_SECONDS
        self.assertGreater(entrypoint.KILLED_RUNNER_DRAIN_SECONDS, bound)


class TestTheComposeFile(unittest.TestCase):

    def setUp(self):
        self.text = (HERE / "compose.yaml").read_text(encoding="utf-8")
        self.build = (HERE / "build_image.sh").read_text(encoding="utf-8")

    def test_the_image_is_the_one_the_build_script_tags_and_is_never_pulled(self):
        image = re.search(r"(?m)^IMAGE=(\S+)", self.build).group(1)
        self.assertIn(f"image: {image}:latest", self.text)
        self.assertIn("pull_policy: never", self.text)

    def test_the_two_directories_are_mounted_at_their_repository_paths(self):
        for name in entrypoint.MOUNTS:
            with self.subTest(mount=name):
                self.assertIn(f"- ./{name}:/app/{name}", self.text)
        self.assertEqual(self.text.count("- ./"), len(entrypoint.MOUNTS))
        self.assertIn("- ./handover:/app/handover", self.text)

    def test_the_credentials_file_is_mounted_read_only_and_never_tracked(self):
        # The operator writes credentials.csv beside compose.yaml, and no other file contains
        # a credential.
        self.assertIn("source: ./credentials.csv", self.text)
        self.assertIn("target: /app/credentials.csv", self.text)
        self.assertIn("read_only: true", self.text)
        # In the long form, Docker refuses a missing source. The short form creates a
        # directory of that name in the project folder instead.
        self.assertIn("create_host_path: false", self.text)
        self.assertNotIn("env_file", self.text)
        example = (HERE / "credentials.csv.example").read_text(encoding="utf-8")
        self.assertIn("service,account,credential", example)
        self.assertNotRegex(example, r"(?m)^(github|openai|openrouter),", "the example has a row "
                                                                "that is not a comment")
        ignored = subprocess.run(["git", "check-ignore", "-q", "deploy/credentials.csv"],
                                 cwd=str(ROOT))
        self.assertEqual(ignored.returncode, 0, "credentials.csv is not gitignored")

    def _mounts(self) -> list:
        """The mounts of the service, each as a dict: `source` and `target`, `read_only` and,
        in the long form, `create_host_path`. The helper reads the short form
        `SOURCE:TARGET[:ro]` and the long form with one `key: value` per line."""
        section = self.text.split("\n    volumes:\n", 1)[1]
        mounts = []
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if len(line) - len(line.lstrip()) <= 4:
                break
            if stripped.startswith("- ") and not re.match(r"- [a-z_]+: ", stripped):
                source, target, *mode = stripped[2:].split(":")
                mounts.append({"source": source, "target": target,
                               "read_only": "true" if mode == ["ro"] else "false"})
                continue
            if stripped.startswith("- "):
                mounts.append({})
                stripped = stripped[2:]
            key, _, value = stripped.partition(":")
            if value.strip():
                mounts[-1][key] = value.strip()
        return mounts

    def test_the_nas_time_zone_is_mounted_read_only_where_the_entrypoint_reads_it(self):
        """DSM's Task Scheduler starts the container at a time of the NAS's zone, and the
        entrypoint compares calendar months in the zone of the file that compose.yaml mounts.
        The mount uses the long form, so that Docker refuses to start where the host has no such
        file rather than create a directory there."""
        zone = [mount for mount in self._mounts() if mount.get("source") == "/etc/localtime"]
        self.assertEqual(zone, [{"type": "bind", "source": "/etc/localtime",
                                 "target": str(entrypoint.HOST_LOCALTIME), "read_only": "true",
                                 "create_host_path": "false"}])

    def test_nothing_is_mounted_over_the_zone_files_of_the_image(self):
        """The image's /etc/localtime is a link to /usr/share/zoneinfo/Etc/UTC, and Docker mounts
        a file over the target of the link. A mount at either place puts another zone into the
        file of UTC, and the cloning step's `git log` under TZ=UTC then writes that zone's local
        time with a Z."""
        targets = [mount["target"] for mount in self._mounts()]
        self.assertIn("/app/data", targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertNotEqual(target, "/etc/localtime")
                self.assertNotEqual(Path(target).parts[:4], ("/", "usr", "share", "zoneinfo"))

    def test_the_revision_of_the_file_is_the_one_that_the_entrypoint_requires(self):
        """A Start under the shipped compose.yaml passes the entrypoint's check of the revision.
        The rule in deploy/0_README.md changes the file's revision and the entrypoint's required
        revision together, and this test fails when only one of them changes. The file sets no
        TZ, because the container's own local time stays UTC."""
        given = _compose_revision()
        self.assertEqual(given, str(entrypoint.COMPOSE_REVISION))
        self.assertIsNone(entrypoint.compose_problem({entrypoint.COMPOSE_REVISION_VARIABLE: given}))
        self.assertNotRegex(self.text, r"(?m)^\s+-?\s*TZ[:=]")

    # Where Container Manager links the docker command. DSM's Task Scheduler runs a script without
    # the PATH of a login shell.
    DOCKER = "/usr/local/bin/docker"

    def test_the_scheduled_tasks_start_the_container_that_compose_names(self):
        """Compose names a container after the project unless compose.yaml names it. Under a
        project with a name other than `agentcontext`, a task that starts a container name of its
        own would therefore fail with `No such container` every month. Every docker command of
        the runbook names docker by its path, and every `docker start` names the container that
        compose.yaml names."""
        found = re.search(r"(?m)^    container_name: (\S+)$", self.text)
        self.assertIsNotNone(found, "compose.yaml names no container")
        runbook = (ROOT / "docs" / "deploy.md").read_text(encoding="utf-8")
        lines = [line.strip() for block in re.findall(r"(?ms)^```shell\n(.*?)^```", runbook)
                 for line in block.splitlines() if re.search(r"\bdocker\b", line)]
        starts = [line for line in lines if "docker start" in line]
        self.assertTrue(starts, lines)
        for line in lines:
            with self.subTest(line=line):
                self.assertFalse(re.findall(r"(?<![\w/.-])docker\b", line),
                                 "docker without its path")
                self.assertEqual(len(re.findall(r"\bdocker\b", line)),
                                 line.count(f"{self.DOCKER} "))
        for line in starts:
            with self.subTest(line=line):
                self.assertEqual(line, f"{self.DOCKER} start {found.group(1)}")

    def test_an_init_process_reaps_what_the_run_orphans(self):
        # Without an init process, the entrypoint is process 1 and reaps only the runner, and
        # every orphaned git helper stays a zombie until the container ends.
        self.assertRegex(self.text, r"(?m)^\s+init: true$")

    def test_a_stop_has_time_to_drain(self):
        found = re.search(r"stop_grace_period:\s*(\d+)m", self.text)
        self.assertIsNotNone(found)
        self.assertGreaterEqual(int(found.group(1)), 10)

    def test_the_default_command_runs_the_checks_too(self):
        self.assertIn('command: ["--with-commit-reclassification"]', self.text)
        self.assertIn('CMD ["--with-commit-reclassification"]', (HERE / "Dockerfile").read_text())


# ---------------------------------------------------------------------------
# The build context and the bundle
# ---------------------------------------------------------------------------

class TestTheBuildContext(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if _tracked("mise.toml") is None or shutil.which("tar") is None:
            raise unittest.SkipTest("needs a git checkout and tar")
        cls.tmp = Path(tempfile.mkdtemp())
        cls.ctx = cls.tmp / "ctx"
        subprocess.run(["bash", str(HERE / "build_image.sh"), "--context", str(cls.ctx)],
                       check=True, capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_context_contains_what_the_image_needs(self):
        """The runner of the five stages and a script of each other stage, with the
        configuration, the prompts and the requirements that they read, and the two dependency
        locks that the Dockerfile installs."""
        for relative in ("Dockerfile", "mise.toml", "deploy/entrypoint.py",
                         "deploy/install_tools.sh", "locks/build.txt", "locks/pipeline.txt",
                         "run_pipeline.py",
                         "agentcontext/runner.py", "pyproject.toml",
                         "agentcontext/requirements.txt", "1_sampling/1_download_ghs.py",
                         "1_sampling/config/ghs_filter.yaml", "1_sampling/requirements.txt",
                         "2_classification/1_classify_repos.py",
                         "2_classification/prompts/classification-prompt.md",
                         "2_classification/requirements.txt", "4_data-analysis/run_analysis.py",
                         "4_data-analysis/rq2/requirements.txt"):
            with self.subTest(path=relative):
                self.assertTrue((self.ctx / relative).is_file())

    def test_the_context_contains_every_tracked_file_of_the_package_and_the_heuristics(self):
        """Every such file except the test modules, byte for byte, so that an exclusion that
        drops a file that a step reads fails the test."""
        tracked = [path for path in _tracked("agentcontext", "heuristics")
                   if not re.search(r"(^|/)test_[^/]*\.py$", path)]
        self.assertIn("heuristics/not_configuration.csv", tracked)
        for relative in tracked:
            with self.subTest(path=relative):
                self.assertTrue((self.ctx / relative).is_file(), relative)
                self.assertEqual((self.ctx / relative).read_bytes(),
                                 (ROOT / relative).read_bytes())

    def test_the_context_contains_code_and_criteria_and_no_data(self):
        """Everything that a run reads and writes is in the data root, which is mounted from the
        project folder. None of it is in the context, and neither are the suites or their
        fixture."""
        self.assertEqual(list(self.ctx.rglob("test_*.py")), [])
        for relative in ("data", "3_data-collection", "4_data-analysis/testdata", ".git",
                         "tests", "docs", "_plans", "_website"):
            with self.subTest(path=relative):
                self.assertFalse((self.ctx / relative).exists())
        # The CSVs that a step reads as criteria rather than data are the heuristics, the
        # licenses that the sampling filter keeps or excludes, and the model prices from which a
        # classification run computes its cost record. The test names them by path, so that a
        # data file of the same name elsewhere counts as data.
        rules = {"heuristics/config_path_patterns.csv", "heuristics/ai_commit_patterns.csv",
                 "heuristics/other_ai_commit_patterns.csv", "heuristics/not_configuration.csv",
                 "heuristics/not_ai_commits.csv", "heuristics/import_patterns.csv",
                 "heuristics/not_imports.csv", "heuristics/planning_document_patterns.csv",
                 "heuristics/not_planning_documents.csv", "heuristics/local_state_paths.csv",
                 "1_sampling/config/licenses.csv", "2_classification/config/model_prices.csv"}
        for relative in sorted(rules):
            with self.subTest(rule=relative):
                self.assertTrue((self.ctx / relative).is_file(), relative)
        data = sorted(relative for relative in (p.relative_to(self.ctx).as_posix()
                                                for p in self.ctx.rglob("*"))
                      if Path(relative).suffix in (".7z", ".xz", ".jsonl", ".csv")
                      and relative not in rules)
        self.assertEqual(data, [])

    def test_every_file_in_the_context_is_tracked(self):
        tracked = set(_tracked("."))
        for path in self.ctx.rglob("*"):
            if path.is_file() and path.name != "Dockerfile":
                with self.subTest(path=str(path.relative_to(self.ctx))):
                    self.assertIn(str(path.relative_to(self.ctx)), tracked)


class TestTheBuildScript(unittest.TestCase):

    def test_the_nas_build_is_for_the_nas_platform_and_records_the_commit(self):
        text = (HERE / "build_image.sh").read_text(encoding="utf-8")
        self.assertIn("PLATFORM=linux/amd64", text)
        self.assertIn("rev-parse HEAD", text)
        self.assertIn("RepoTags", text)

    def test_a_bundle_without_the_zip_extension_is_refused(self):
        """`zip` appends `.zip` to a name that has none, and the path that the script prints
        would then name a file that is not there."""
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(["bash", str(HERE / "make_nas_bundle.sh"),
                                     str(Path(tmp) / "bundle")],
                                    capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("has to be named *.zip", result.stderr)

    def test_the_build_products_are_gitignored(self):
        for name in ("agentcontext-pipeline.tar.gz", "BUILD_INFO.txt",
                     "agentcontext-nas-bundle.zip"):
            with self.subTest(name=name):
                result = subprocess.run(["git", "check-ignore", "-q", f"deploy/{name}"],
                                        cwd=str(ROOT))
                self.assertEqual(result.returncode, 0, f"{name} is not gitignored")


@unittest.skipIf(shutil.which("git") is None or shutil.which("shasum") is None,
                 "git and shasum not on PATH")
class TestTheBuildScriptOverAStubDocker(unittest.TestCase):
    """`build_image.sh` in its tarball mode, run in full with a stub `docker` on PATH.

    The test runs a copy of the script inside a repository that it creates, so that the tarball
    and `BUILD_INFO.txt` that the script writes beside itself are written to a temporary
    directory. For `buildx build`, the stub returns an OCI-layout tarball that the test builds,
    as a containerd image store writes it. For `docker images`, for the `docker run` that reads
    the release of Python of the image and for the `docker run` of the zone check, the stub
    returns the output that the test sets. The rewrite into the classic layout, the tag check
    and the comparison of the zone check run for real. `TZDIR` names a zone database that the
    test writes.
    """

    IMAGE = "agentcontext-pipeline"
    # What the image prints in the zone check when the container's time stays UTC and the
    # entrypoint reads the mounted zone of Europe/Berlin: the offset of `date` under TZ=UTC and
    # without TZ, the commit time of a commit at 12:00 UTC as the cloning step's `git log` writes
    # it, the local time of a Start at 23:00 UTC on November 30 in the entrypoint's zone, and the
    # verdict of the entrypoint's check of the container's own zone.
    ZONE_CHECK_OUTPUT = ("+0000\n+0000\n2026-07-01T12:00:00Z\n2026-12-01 00:00 CET\n"
                         "the container's own zone is UTC\n")
    # The data root revision of the entrypoint in the repository that the test creates.
    REVISION = 7

    def setUp(self):
        # Resolved, because the script names its own directory by `pwd -P`.
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "root"
        (self.root / "deploy").mkdir(parents=True)
        for name in ("build_image.sh", "Dockerfile", "compose.yaml"):
            shutil.copy(HERE / name, self.root / "deploy" / name)
        # An entrypoint of a data root revision of its own, which BUILD_INFO.txt names.
        (self.root / "deploy" / "entrypoint.py").write_text(
            f'"""An entrypoint."""\n\nDATA_ROOT_REVISION = {self.REVISION}\n', encoding="utf-8")
        (self.root / "mise.toml").write_text('[tools]\npython = "3.14.10"\n', encoding="utf-8")
        (self.root / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")
        git = ["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@example.com",
               "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "-q", "-m", "one"], check=True)
        self.commit = subprocess.run(["git", "-C", str(self.root), "rev-parse", "HEAD"],
                                     capture_output=True, text=True, check=True).stdout.strip()
        self.oci, self.blobs = self._oci_layout()
        self.log = self.tmp / "docker.log"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        stub = bin_dir / "docker"
        stub.write_text(
            f"#!{sys.executable}\n"
            "import json, os, shutil, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['STUB_DOCKER_LOG'], 'a') as f:\n"
            "    f.write(json.dumps(args) + '\\n')\n"
            "if args[:2] == ['buildx', 'build']:\n"
            "    dest = [a for a in args if a.startswith('type=docker,dest=')]\n"
            "    if dest:\n"
            "        shutil.copy(os.environ['STUB_OCI_TAR'], dest[0][len('type=docker,dest='):])\n"
            "elif args[:1] == ['images']:\n"
            "    sys.stdout.write(os.environ['STUB_IMAGES'])\n"
            "elif args[:1] == ['run'] and 'platform.python_version()' in args[-1]:\n"
            "    print(os.environ['STUB_PYTHON'])\n"
            "elif args[:1] == ['run']:\n"
            "    sys.stdout.write(os.environ['STUB_RUN'])\n", encoding="utf-8")
        stub.chmod(0o755)
        # The zone database that the zone check copies Europe/Berlin from.
        zones = self.tmp / "zoneinfo"
        _berlin_file(zones / "Europe" / "Berlin")
        self.env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                        STUB_DOCKER_LOG=str(self.log), STUB_OCI_TAR=str(self.oci),
                        STUB_RUN=self.ZONE_CHECK_OUTPUT, STUB_PYTHON="3.14.10", TZDIR=str(zones))

    def _oci_layout(self) -> tuple:
        """An OCI-layout tarball with a config and two layers under blobs/sha256/."""

        def layer(name: str) -> bytes:
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w") as t:
                data = f"{name}\n".encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
            return buffer.getvalue()

        config = json.dumps({"architecture": "amd64", "os": "linux"}).encode()
        blobs = {"config": config, "base": layer("base.txt"), "top": layer("top.txt")}
        digests = {key: hashlib.sha256(value).hexdigest() for key, value in blobs.items()}
        manifest = [{"Config": f"blobs/sha256/{digests['config']}",
                     "RepoTags": [f"{self.IMAGE}:latest"],
                     "Layers": [f"blobs/sha256/{digests['base']}",
                                f"blobs/sha256/{digests['top']}"]}]
        members = {"oci-layout": b'{"imageLayoutVersion": "1.0.0"}',
                   "index.json": b'{"schemaVersion": 2, "manifests": []}',
                   "manifest.json": json.dumps(manifest).encode()}
        members.update({f"blobs/sha256/{digests[key]}": value for key, value in blobs.items()})
        path = self.tmp / "oci.tar"
        with tarfile.open(path, "w") as t:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
        return path, {key: (digests[key], value) for key, value in blobs.items()}

    def _build(self, images_output: str, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(["bash", str(self.root / "deploy" / "build_image.sh"), *args],
                              env=dict(self.env, STUB_IMAGES=images_output, **env),
                              capture_output=True, text=True, timeout=120)

    def _docker_calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_the_tarball_is_the_classic_layout_with_the_tag(self):
        result = self._build(f"  {self.IMAGE}:latest (1MB)\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        tarball = self.root / "deploy" / f"{self.IMAGE}.tar.gz"
        config, base, top = (self.blobs[k] for k in ("config", "base", "top"))
        with tarfile.open(tarball) as t:
            files = {m.name.removeprefix("./"): t.extractfile(m).read()
                     for m in t.getmembers() if m.isfile()}
        self.assertEqual(sorted(files), sorted(["manifest.json", "repositories",
                                                f"{config[0]}.json", f"{base[0]}/layer.tar",
                                                f"{top[0]}/layer.tar"]))
        self.assertEqual(json.loads(files["manifest.json"]),
                         [{"Config": f"{config[0]}.json", "RepoTags": [f"{self.IMAGE}:latest"],
                           "Layers": [f"{base[0]}/layer.tar", f"{top[0]}/layer.tar"]}])
        self.assertEqual(json.loads(files["repositories"]), {self.IMAGE: {"latest": top[0]}})
        self.assertEqual(files[f"{config[0]}.json"], config[1])
        self.assertEqual(files[f"{base[0]}/layer.tar"], base[1])
        self.assertEqual(files[f"{top[0]}/layer.tar"], top[1])

        info = (self.root / "deploy" / "BUILD_INFO.txt").read_text(encoding="utf-8").splitlines()
        self.assertEqual(info[:3], [f"image {self.IMAGE}:latest", "platform linux/amd64",
                                    f"commit {self.commit}"])
        self.assertEqual(info[4], "python 3.14.10")
        self.assertEqual(info[5], "sha256 " + hashlib.sha256(tarball.read_bytes()).hexdigest())
        self.assertEqual(info[6:], [f"data_root_revision {self.REVISION}"])

        calls = self._docker_calls()
        self.assertEqual([c[:2] for c in calls],
                         [["buildx", "build"], ["rmi", "-f"], ["load", "-i"],
                          ["images", f"{self.IMAGE}:latest"], ["run", "--rm"], ["run", "--rm"]])
        self.assertIn("--platform", calls[0])
        # The Dockerfile names its base image, and the build passes no release of Python.
        self.assertEqual([word for word in calls[0] if "PYTHON" in word], [])
        self.assertEqual(calls[0][calls[0].index("--platform") + 1], "linux/amd64")
        self.assertEqual(Path(calls[2][2]).name, tarball.name)
        self.assertIn(f"  {self.IMAGE}:latest (1MB)", result.stdout)
        for run in calls[4:]:
            self.assertEqual(run[run.index("--platform") + 1], "linux/amd64")

    def test_the_build_record_names_the_release_of_python_of_the_image(self):
        """The release that the interpreter of the built image reports, rather than the one that
        mise.toml pins, so that the record states what the NAS runs. The image is the one that the
        script built, on the platform that it built for."""
        result = self._build(f"  {self.IMAGE}:latest (1MB)\n", STUB_PYTHON="3.14.11")
        self.assertEqual(result.returncode, 0, result.stderr)
        info = (self.root / "deploy" / "BUILD_INFO.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("python 3.14.11", info)
        read = next(call for call in self._docker_calls()
                    if call[:1] == ["run"] and "platform.python_version()" in call[-1])
        self.assertEqual(read[read.index("--entrypoint") + 1:][:2],
                         ["python3", f"{self.IMAGE}:latest"])
        self.assertEqual(read[read.index("--platform") + 1], "linux/amd64")

    def test_the_zone_check_mounts_berlin_where_compose_mounts_the_nas_zone(self):
        """The image runs once with the zone file of Europe/Berlin at the path where compose.yaml
        mounts the NAS's /etc/localtime and where the entrypoint reads it. The build passes only
        when the container's time stays UTC and the entrypoint reads Berlin."""
        for args in ((), ("--local",)):
            with self.subTest(args=args):
                self.log.unlink(missing_ok=True)
                result = self._build(f"  {self.IMAGE}:latest (1MB)\n", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                run = self._docker_calls()[-1]
                self.assertEqual(run[:2], ["run", "--rm"])
                self.assertLessEqual(int(run[run.index("--cpus") + 1]), 8)
                source, target, mode = run[run.index("-v") + 1].split(":")
                self.assertEqual((target, mode), (str(entrypoint.HOST_LOCALTIME), "ro"))
                self.assertEqual(Path(source).name, "Berlin")
                self.assertEqual(run[run.index("--entrypoint") + 1:][:2],
                                 ["python3", f"{self.IMAGE}:latest"])
                self.assertIn("Zone check: UTC stays UTC", result.stdout)

    def test_a_zone_check_that_shows_another_zone_fails_the_build(self):
        """If the image's zone of UTC is the NAS's zone, as with a mount over /etc/localtime, the
        image prints the NAS's offset under TZ=UTC and the commit time shifted. If its entrypoint
        reads no zone, the image prints the Start in UTC."""
        utc = "the container's own zone is UTC\n"
        for printed in ("+0100\n+0100\n2026-07-01T14:00:00Z\n2026-12-01 00:00 CET\n" + utc,
                        "+0000\n+0000\n2026-07-01T12:00:00Z\n2026-11-30 23:00 UTC\n" + utc,
                        "+0000\n+0000\n2026-07-01T12:00:00Z\n2026-12-01 00:00 CET\n"
                        "/usr/share/zoneinfo/UTC gives the offset UTC+01:00 on 2026-01-01\n"):
            with self.subTest(printed=printed):
                result = self._build(f"  {self.IMAGE}:latest (1MB)\n", STUB_RUN=printed)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"The zone check of {self.IMAGE}:latest", result.stderr)

    def test_a_build_that_fails_a_check_leaves_no_tarball_and_no_build_record(self):
        """A tarball and a `BUILD_INFO.txt` in deploy/ look like a build that passed. A build that
        fails its tag check or its zone check therefore leaves neither, also where an earlier
        build left both."""
        deploy = self.root / "deploy"
        failing = {"zone check": (f"  {self.IMAGE}:latest (1MB)\n",
                                  {"STUB_RUN": self.ZONE_CHECK_OUTPUT.replace("+0000", "+0100")}),
                   "tag check": ("", {})}
        for check, (images, env) in failing.items():
            with self.subTest(check=check):
                for name in (f"{self.IMAGE}.tar.gz", "BUILD_INFO.txt"):
                    (deploy / name).write_text("an earlier build\n", encoding="utf-8")
                result = self._build(images, **env)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertFalse((deploy / f"{self.IMAGE}.tar.gz").exists())
                self.assertFalse((deploy / "BUILD_INFO.txt").exists())

    def test_an_edited_dockerfile_marks_the_image_commit_dirty(self):
        dockerfile = self.root / 'deploy' / 'Dockerfile'
        dockerfile.write_text(dockerfile.read_text() + '\nENV REVIEW_BUILD=changed\n')
        result = self._build(f'  {self.IMAGE}:latest (1MB)\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        info = (self.root / 'deploy' / 'BUILD_INFO.txt').read_text().splitlines()
        self.assertIn(f'commit {self.commit}-dirty', info)
        self.assertIn(f'GIT_COMMIT={self.commit}-dirty', self._docker_calls()[0])

    def test_an_entrypoint_without_a_data_root_revision_builds_nothing(self):
        """BUILD_INFO.txt has to name the image's data root revision, so that you can tell
        before an import whether the image resumes the run in a project folder. An entrypoint
        that does not set the revision to an integer exactly once fails the build in both modes
        before docker runs. The tarball build then leaves neither the tarball nor the build
        record of an earlier build."""
        deploy = self.root / "deploy"
        for text in ('"""An entrypoint."""\n', 'DATA_ROOT_REVISION = "1"\n',
                     "DATA_ROOT_REVISION = 1\nDATA_ROOT_REVISION = 2\n"):
            for args in ((), ("--local",)):
                with self.subTest(text=text, args=args):
                    (deploy / "entrypoint.py").write_text(text, encoding="utf-8")
                    if not args:
                        for name in (f"{self.IMAGE}.tar.gz", "BUILD_INFO.txt"):
                            (deploy / name).write_text("an earlier build\n", encoding="utf-8")
                    self.log.unlink(missing_ok=True)
                    result = self._build(f"  {self.IMAGE}:latest (1MB)\n", *args)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("does not set DATA_ROOT_REVISION to an integer exactly once", result.stderr)
                    self.assertFalse(self.log.exists())
                    self.assertFalse((deploy / f"{self.IMAGE}.tar.gz").exists())
                    self.assertFalse((deploy / "BUILD_INFO.txt").exists())

    def test_the_tag_check_can_fail(self):
        """Because `docker images` on a name that it does not know prints nothing and exits 0,
        the empty listing rather than the exit status fails the build."""
        result = self._build("")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"the local Docker has no {self.IMAGE}:latest", result.stderr)


class TestTheImageWorkflow(unittest.TestCase):

    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "container-image.yml").read_text(
            encoding="utf-8")

    def test_the_runner_gets_a_builder_that_can_write_a_tarball(self):
        # The default builder on a GitHub Actions runner is the docker driver over the classic
        # image store, and `buildx --output type=docker,dest=` fails under it with "Docker
        # exporter is not supported for the docker driver".
        self.assertIn("docker/setup-buildx-action", self.text)
        self.assertIn("driver: docker-container", self.text)
        self.assertLess(self.text.index("docker/setup-buildx-action"),
                        self.text.index("build_image.sh"))

    def test_it_runs_the_two_scripts_and_uploads_what_they_write(self):
        self.assertIn("bash deploy/build_image.sh", self.text)
        self.assertIn("bash deploy/make_nas_bundle.sh", self.text)
        for product in ("agentcontext-pipeline.tar.gz", "BUILD_INFO.txt",
                        "agentcontext-nas-bundle.zip"):
            with self.subTest(product=product):
                self.assertIn(f"deploy/{product}", self.text)

    def test_it_builds_by_hand_and_on_a_release_tag_and_keeps_its_artifacts_a_week(self):
        # An image is built when a deployment needs one, not on every push, and the
        # workflow artifacts that a deployment did not download are deleted after a week.
        self.assertIn("workflow_dispatch:", self.text)
        self.assertIn('tags: ["v*"]', self.text)
        self.assertNotIn("branches:", self.text)
        self.assertNotIn("paths:", self.text)
        self.assertEqual(self.text.count("retention-days: 7"), self.text.count("upload-artifact"))


@unittest.skipIf(shutil.which("zip") is None or shutil.which("unzip") is None,
                 "zip and unzip not on PATH")
class TestTheBundle(unittest.TestCase):
    """The bundle from which the project folder is created. The image contains the code and
    the heuristics, and the first start writes the data root. The bundle therefore contains only
    the files that the host reads and the two mount sources, empty."""

    @classmethod
    def setUpClass(cls):
        if _tracked("mise.toml") is None or shutil.which("unzip") is None:
            raise unittest.SkipTest("needs a git checkout and unzip")
        cls.tmp = Path(tempfile.mkdtemp())
        out = cls.tmp / "bundle.zip"
        subprocess.run(["bash", str(HERE / "make_nas_bundle.sh"), str(out)],
                       check=True, capture_output=True, text=True)
        cls.listing = [entry for entry in subprocess.run(
            ["unzip", "-Z1", str(out)], check=True, capture_output=True,
            text=True).stdout.split() if not entry.endswith("/")]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_it_contains_the_three_files_the_host_reads_and_the_two_mounts_empty(self):
        """The bundle contains exactly these files. The data root is empty, because the first
        start writes it, and credentials.csv contains credentials and is never in the bundle."""
        self.assertEqual(sorted(self.listing),
                         sorted(["agentcontext/compose.yaml", "agentcontext/deploy.md",
                                 "agentcontext/credentials.csv.example",
                                 f"agentcontext/{entrypoint.DATA}/.keep",
                                 f"agentcontext/{entrypoint.HANDOVER}/.keep"]))

    def test_the_mount_sources_are_the_entrypoints_mounts(self):
        for name in entrypoint.MOUNTS:
            with self.subTest(mount=name):
                self.assertIn(f"agentcontext/{name}/.keep", self.listing)


if __name__ == "__main__":
    unittest.main()
