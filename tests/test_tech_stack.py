"""
Tests that the pipeline has one tech stack, stated once.

Strategy:
  - `mise.toml` at the repository root pins the release of Python, such as 3.14.10. mise
    installs it on the development machine, and `jdx/mise-action` installs it in CI, at the
    release of mise that the images install. Both Dockerfiles start from the base image of that
    release, named by digest. This suite checks every other statement of the version against
    the pin: the base images, the `--python-version` of dev/lock.sh, a spec's Language line, a
    test module's Requirements line, the sentences in README.md and CLAUDE.md, and the running
    interpreter. The documents state major.minor. A `.python-version` beside mise.toml would be
    another statement of the version. mise ignores that file unless
    `idiomatic_version_file_enable_tools` names python, and the setting is empty here.
  - Every package has one version: The suite parses every tracked requirements.txt, and a
    package pinned at two versions fails.
  - The dependency locks under locks/ list every distribution that an environment installs,
    with its version and its hashes, and dev/lock.sh writes them from the requirements files
    with the uv that mise.toml pins. A dependency lock that disagrees with its requirements
    files, a requirements file in no dependency lock, a dependency lock that no environment
    installs, and two dependency locks with different versions of a distribution fail. So does
    an install in a workflow, a Dockerfile, a shell script, a document or a module that reads no
    dependency lock or does not require hashes, and a command that installs a distribution of
    its own choosing, such as `uvx`.
  - mise creates the virtual environment .venv of the checkout from mise.toml, and the project
    runs there rather than in mise's shared installation of Python. dev/setup.sh refuses another
    environment and a .venv of another release of Python, and installs into .venv what CI
    installs into its .venv. Neither reads a configuration of uv or a variable that makes uv
    build a distribution instead of installing its wheel.
  - mise.toml also pins git and rsync, conda-forge's packages, and 7-Zip, the `7zz` of its
    release, which `mise exec` puts first on the PATH of a development machine. Every image
    installs them with deploy/install_tools.sh, and a Dockerfile that installs one of them
    through apt fails here, because the history columns depend on the version of git, the
    cloning step copies with rsync and the release archives are packed with 7-Zip. With only
    `7zz` on PATH, the release step packs and lists an archive.
  - Every tracked CSV ends its records with LF, the line ending that every writer of the
    pipeline writes. A copy of a file that the collection read from a repository keeps the
    bytes of the repository, and the suite leaves such copies out.
  - The suite discovers what it checks rather than listing it, and it checks a step, spec,
    requirements file or Dockerfile added later without any registration.

Requirements: Python 3.14 (stdlib only, no extra pip installs), 7zz on PATH for the test that
packs with it, bash and git for the tests of dev/lock.sh and dev/setup.sh
Run with:  python3 -m unittest tests.test_tech_stack -v
"""

import ast
import csv
import io
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from collections import defaultdict
from pathlib import Path
from unittest import mock
from agentcontext import REPO_ROOT
from tests._fixtures import BASH

VERSION_FILE = REPO_ROOT / "mise.toml"

WORKFLOWS = REPO_ROOT / ".github" / "workflows"
PROSE = ("README.md", "CLAUDE.md")

# `- **Language:** Python 3.14 (single script ...)` in a spec.
_SPEC_CLAIM = re.compile(r"\*\*Language:\*\*\s*Python\s*(\d+)\.(\d+)")

# `Requirements: Python 3.14 (stdlib only...)` in a module docstring.
_DOCSTRING_CLAIM = re.compile(r"Requirements:\s*Python\s*(\d+)\.(\d+)")

# `Python 3.14` anywhere in README.md or CLAUDE.md.
_PROSE_CLAIM = re.compile(r"Python\s+(\d+)\.(\d+)")

# A requirement line, such as `pandas==3.0.5`, as its name and its version specifier.
_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)$")


def normalized(name: str) -> str:
    """A distribution name as pip compares it (PEP 503), in which case and every run of `-`, `_`
    and `.` do not matter. Lower-casing alone would keep `lingua_language_detector==2.1.0` and
    `lingua-language-detector==2.2.0` apart, and pip installs whichever file it reads
    last."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_pins(files) -> dict:
    """{normalized name: {"path:line": version specifier}} over the requirements files.

    Keyed by line rather than by file, so that two spellings of one package in the same file
    stay two entries and neither overwrites the other.
    """
    pins = defaultdict(dict)
    for path in files:
        if not path.is_file():
            continue
        shown = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = raw.split("#")[0].strip()
            if not line:
                continue
            match = _REQUIREMENT.match(line)
            if match:
                pins[normalized(match.group(1))][f"{shown}:{number}"] = match.group(2).strip()
    return pins


def pinned_release() -> str:
    """The release of Python that mise.toml pins, such as 3.14.10."""
    tools = tomllib.loads(VERSION_FILE.read_text(encoding="utf-8")).get("tools", {})
    python = tools.get("python")
    if isinstance(python, dict):
        python = python.get("version")
    return str(python)


def declared_version():
    """The `major.minor` that mise.toml pins for python.

    Truncated to two parts, because the pin names the patch release, and every other statement
    of the version gives major.minor.
    """
    return tuple(int(part) for part in pinned_release().split("."))[:2]


def version_text(version):
    return ".".join(str(part) for part in version)


def tracked(pattern):
    """The paths that git tracks and that match a pathspec.

    The function lists through git rather than rglob, because the collected tree, which git
    does not track, contains third-party requirements.txt files and scripts. Their versions are
    data rather than this pipeline's stack.
    """
    result = subprocess.run(["git", "ls-files", "--", pattern],
                            capture_output=True, text=True, cwd=str(REPO_ROOT))
    if result.returncode != 0:
        return None
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line]


def ends_records_with_lf(text):
    """Whether writing the records of `text` with LF line endings gives `text` back. A line break
    inside a quoted field, such as a CRLF in a commit message that a file stores verbatim, belongs
    to the field. The function lifts the field size limit of the CSV reader while it reads
    `text`, because a commit CSV has messages longer than the default limit."""
    limit = csv.field_size_limit(sys.maxsize)
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    finally:
        csv.field_size_limit(limit)
    written = io.StringIO()
    csv.writer(written, lineterminator="\n").writerows(rows)
    return written.getvalue() == text


class TestOneLineEnding(unittest.TestCase):

    def test_every_tracked_csv_ends_its_records_with_lf(self):
        files = tracked("*.csv")
        if files is None:
            self.skipTest("not a git checkout")
        relative = [p.relative_to(REPO_ROOT).as_posix() for p in files]
        other = [rel for rel in relative
                 if not ends_records_with_lf((REPO_ROOT / rel).read_text(encoding="utf-8",
                                                                         newline=""))]
        self.assertEqual(other, [], "Write these files with lineterminator=\"\\n\"")

    def test_crlf_records_fail_and_a_crlf_inside_a_field_passes(self):
        self.assertFalse(ends_records_with_lf("a,b\r\n1,2\r\n"))
        self.assertTrue(ends_records_with_lf('a,b\n1,"x\r\ny"\n'))


class TestOnePythonVersion(unittest.TestCase):

    def setUp(self):
        self.version = declared_version()

    def test_the_version_file_pins_an_exact_release(self):
        """A release, such as 3.14.10, rather than 3.14, which mise resolves to the newest
        release that it finds installed or published, so that a new machine and CI would run
        another interpreter than the one that the existing .venv was created from."""
        self.assertRegex(pinned_release(), r"^\d+\.\d+\.\d+$")

    def test_the_interpreter_running_this_suite_is_that_release(self):
        # A pass here and a pass in CI mean the same only if both ran the same
        # interpreter. A change such as 3.14's to Path.exists on an unreadable mount
        # is found by running the suite on that interpreter, not by reading the code.
        self.assertEqual(
            platform.python_version(), pinned_release(),
            f"this is Python {platform.python_version()}, and {VERSION_FILE.name} pins "
            f"{pinned_release()}. Run the suite with `mise exec --` in the repository root, and "
            f"remove .venv and run `mise exec -- bash dev/setup.sh` when .venv was created from "
            f"another release.")

    def test_every_tracked_source_parses_at_that_version(self):
        sources = tracked("*.py")
        if sources is None:
            self.skipTest("not inside a git checkout")
        self.assertGreater(len(sources), 50, "the file list is too short")
        # Capped at the running interpreter, because ast cannot parse a grammar
        # newer than its own.
        oldest = min(self.version, sys.version_info[:2])
        for path in sources:
            if not path.is_file():
                continue
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                try:
                    ast.parse(path.read_text(encoding="utf-8"),
                              filename=str(path), feature_version=oldest)
                except SyntaxError as error:
                    self.fail(f"needs more than Python {version_text(oldest)}: "
                              f"{error.msg} (line {error.lineno})")

    def _assert_claims(self, paths, pattern, what):
        if paths is None:
            self.skipTest("not inside a git checkout")
        seen = 0
        for path in paths:
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                match = pattern.search(line)
                if match is None:
                    continue
                seen += 1
                claimed = (int(match.group(1)), int(match.group(2)))
                with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                    self.assertEqual(
                        claimed, self.version,
                        f"{what} states Python {version_text(claimed)}, and "
                        f"{VERSION_FILE.name} states {version_text(self.version)}")
        self.assertGreater(seen, 0, f"found no {what} to check")

    def test_every_spec_language_line_states_that_version(self):
        # Both globs, because the collection stage's documents are under docs/, and the
        # sampling, classification and analysis stages keep a `<name>-spec.md` beside each
        # script. Narrowing this test to docs/ would leave the version in the spec files
        # unchecked.
        # Each glob is checked for None, because `tracked` returns None outside a checkout and
        # the caller turns None into a skip. Adding two None values would raise before the skip.
        docs, specs = tracked("docs/*.md"), tracked("*-spec.md")
        both = None if docs is None or specs is None else docs + specs
        self._assert_claims(both, _SPEC_CLAIM, "a spec Language line")

    def test_every_module_requirements_line_states_that_version(self):
        self._assert_claims(tracked("*.py"), _DOCSTRING_CLAIM,
                            "a module Requirements line")

    def test_the_prose_names_that_version(self):
        """Every statement, not only one: An assertIn would pass while README.md named the
        version in one sentence and another version in the next."""
        for name in PROSE:
            text = (REPO_ROOT / name).read_text(encoding="utf-8")
            claims = [(text.count("\n", 0, m.start()) + 1, (int(m.group(1)), int(m.group(2))))
                      for m in _PROSE_CLAIM.finditer(text)]
            with self.subTest(doc=name):
                self.assertNotEqual(claims, [], f"{name} does not name the pipeline's Python version")
            for line, claimed in claims:
                with self.subTest(doc=name, line=line):
                    self.assertEqual(claimed, self.version,
                                     f"{name}:{line} states Python {version_text(claimed)}, "
                                     f"and {VERSION_FILE.name} states "
                                     f"{version_text(self.version)}")

    def test_every_tracked_dockerfile_starts_from_the_pinned_release_by_digest(self):
        """The base image of each image is the release that mise.toml pins, the release that the
        NAS runs under. Docker Hub moves a tag to each new build of the image, and the digest
        names one build, with its pip."""
        dockerfiles = tracked("*Dockerfile*")
        if dockerfiles is None:
            self.skipTest("not inside a git checkout")
        self.assertGreater(len(dockerfiles), 0, "found no Dockerfile to check")
        for path in dockerfiles:
            if not path.is_file():
                continue
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                bases = re.findall(r"(?m)^FROM\s+(\S+)", path.read_text(encoding="utf-8"))
                self.assertEqual(len(bases), 1, bases)
                self.assertRegex(bases[0], rf"^python:{re.escape(pinned_release())}-slim"
                                           rf"@sha256:[0-9a-f]{{64}}$")

    def test_every_workflow_runs_the_release_of_mise_that_the_images_install(self):
        """jdx/mise-action installs the newest release of mise unless `version` names one, and
        a new release can resolve mise.toml and create .venv differently from the release that
        deploy/install_tools.sh installs into the images."""
        script = (REPO_ROOT / "deploy" / "install_tools.sh").read_text(encoding="utf-8")
        release = re.search(r"(?m)^MISE_VERSION=(\S+)$", script).group(1)
        workflows = sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])
        actions = 0
        for workflow in workflows:
            lines = workflow.read_text(encoding="utf-8").splitlines()
            for number, line in enumerate(lines):
                if "jdx/mise-action" not in line or line.lstrip().startswith("#"):
                    continue
                actions += 1
                with self.subTest(workflow=workflow.name, line=number + 1):
                    following = "\n".join(lines[number + 1:number + 3])
                    self.assertRegex(following, rf"^\s+with:\n\s+version:\s*"
                                                rf"[\"']?v?{re.escape(release)}[\"']?\s*$")
        self.assertGreater(actions, 0, "found no workflow that runs jdx/mise-action")


    def test_no_workflow_writes_a_version_of_its_own(self):
        # mise-action installs from mise.toml. setup-python would keep a second
        # copy of the number, which can differ from the version of the development machine.
        for workflow in sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")]):
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                if "python" not in text and "mise" not in text:
                    continue
                self.assertNotIn(
                    "actions/setup-python", text,
                    f"{workflow.name} installs Python itself instead of from "
                    f"{VERSION_FILE.name}")
                self.assertNotRegex(
                    text, r"\n\s*python-version",
                    f"{workflow.name} names a version instead of reading "
                    f"{VERSION_FILE.name}")
                self.assertIn("jdx/mise-action", text,
                              f"{workflow.name} does not install from "
                              f"{VERSION_FILE.name}")


class TestOneToolVersion(unittest.TestCase):
    """The output of `git log --follow` depends on the version of git, the cloning step takes over
    a collection only under the same `git --version`, the cloning step copies with rsync, and the
    release archives and the handover are packed with 7-Zip. The images and the development
    machines therefore run the git, the rsync and the 7-Zip that mise.toml pins."""

    # Each tool, with the form of an exact version of it. conda-forge's versions of git and rsync
    # have three numbers, and 7-Zip numbers its releases with two, such as 25.01.
    TOOLS = {"conda:git": r"^\d+\.\d+\.\d+$", "conda:rsync": r"^\d+\.\d+\.\d+$",
             "aqua:ip7z/7zip": r"^\d+\.\d\d$"}
    # The Debian packages that install 7-Zip: `7zip`, its variants such as `7zip-standalone`,
    # and the transitional `p7zip-full`.
    APT_7_ZIP = r"(?<!\S)p?7zip(-[\w.+-]+)?(=|\s|$)"

    def test_the_version_file_pins_an_exact_version_of_each_tool(self):
        """An exact version, so that mise installs the same package on every machine rather than
        the latest one that matches."""
        tools = tomllib.loads(VERSION_FILE.read_text(encoding="utf-8")).get("tools", {})
        for tool, version in self.TOOLS.items():
            with self.subTest(tool=tool):
                self.assertRegex(str(tools.get(tool)), version)

    def test_every_image_installs_the_pinned_tools_and_none_from_apt(self):
        for name in ("Dockerfile", "Dockerfile.development"):
            text = (REPO_ROOT / "deploy" / name).read_text(encoding="utf-8")
            installs = " ".join(re.findall(r"apt-get install[^\n]*(?:\\\n[^\n]*)*", text))
            with self.subTest(dockerfile=name):
                self.assertRegex(text, r"(?m)^COPY mise\.toml deploy/install_tools\.sh /tmp/tools/$")
                self.assertRegex(text, r"(?m)^RUN sh /tmp/tools/install_tools\.sh /tmp/tools\b")
            for tool in ("git", "rsync"):
                with self.subTest(dockerfile=name, tool=tool):
                    self.assertIsNone(re.search(rf"\s{tool}(=|\s|$)", installs),
                                      f"{name} installs {tool} through apt")
            with self.subTest(dockerfile=name, tool="7-Zip"):
                self.assertIsNone(re.search(self.APT_7_ZIP, installs),
                                  f"{name} installs 7-Zip through apt")

    def test_the_pattern_of_the_debian_packages_of_7_zip(self):
        """The packages of 7-Zip match, and a package that only contains the letters does not."""
        for line in ("apt-get install -y p7zip-full ca-certificates",
                     "apt-get install -y 7zip", "apt-get install -y 7zip-standalone",
                     "apt-get install -y 7zip=25.01+dfsg-1"):
            with self.subTest(line=line):
                self.assertIsNotNone(re.search(self.APT_7_ZIP, line))
        self.assertIsNone(re.search(self.APT_7_ZIP, "apt-get install -y lib7zipfoo"))

    def test_the_install_script_takes_the_versions_from_the_version_file(self):
        """The script names the tools and leaves their versions to mise.toml, so that the image
        and the development machines cannot differ."""
        script = (REPO_ROOT / "deploy" / "install_tools.sh").read_text(encoding="utf-8")
        self.assertRegex(script, rf"(?m)^mise install {re.escape(' '.join(self.TOOLS))}$")
        self.assertNotRegex(script, r"(conda|aqua):[\w/]+@")

    @unittest.skipUnless(shutil.which("7zz"), "7zz is not on PATH")
    def test_the_pipeline_runs_the_program_of_the_7_zip_release(self):
        """The 7-Zip release that mise.toml pins provides `7zz` and no `7z`, and neither image
        installs another 7-Zip. With only that program on PATH, the release step packs and
        lists an archive, and the comparison of an extracted copy reads the archive."""
        from agentcontext import archives
        from agentcontext.steps import release
        with tempfile.TemporaryDirectory() as tmp:
            work, bin_dir = Path(tmp) / "work", Path(tmp) / "bin"
            work.mkdir()
            bin_dir.mkdir()
            (bin_dir / "7zz").symlink_to(shutil.which("7zz"))
            (work / "repos.csv").write_text("full_name\nowner/repo\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"PATH": str(bin_dir)}):
                release.compress(work, "repos.csv.7z", "repos.csv")
                self.assertEqual(release.archive_entries(work / "repos.csv.7z"), ["repos.csv"])
                self.assertTrue(archives.copy_matches(work / "repos.csv", work / "repos.csv.7z"))


class TestOneTestRunner(unittest.TestCase):
    """One documented way to run a suite, the way that CI uses."""

    def test_every_test_module_documents_the_same_runner(self):
        # Both globs, because git's `*` matches a slash but the pattern still requires a slash.
        # `*/test_*.py` therefore misses `test_run_pipeline.py` at the root, a module that CI
        # runs.
        nested, root = tracked("*/test_*.py"), tracked("test_*.py")
        if nested is None or root is None:
            self.skipTest("not inside a git checkout")
        modules = nested + root
        self.assertGreater(len(modules), 20, "the module list is too short")
        for path in modules:
            if not path.is_file():
                continue
            with self.subTest(module=path.stem):
                # The docstring rather than the file, because a module with code that
                # mentions "Run with:" would otherwise match its own source, as this module
                # does.
                doc = ast.get_docstring(ast.parse(
                    path.read_text(encoding="utf-8"), filename=str(path))) or ""
                lines = [line.strip() for line in doc.splitlines()
                         if "Run with:" in line or line.strip().startswith("or:")]
                self.assertTrue(lines, f"{path.stem} does not state how to run it")
                # A module under tests/ is run as the package's module, tests.<name>.
                name = f"tests.{path.stem}" if path.parent.name == "tests" else path.stem
                for line in lines:
                    # No module names pytest, because no requirements.txt pins it and no
                    # workflow runs it.
                    self.assertNotIn("pytest", line)
                    self.assertIn(f"python3 -m unittest {name}", line)


class TestOneProcessStartMethod(unittest.TestCase):
    """Worker processes are started the same way on both platforms."""

    def test_every_process_pool_requests_spawn(self):
        # Python 3.14 changed the default on Linux from fork to forkserver, and a
        # forkserver gives every worker the environment that its server process was
        # started with. The process pools here clone with git and read GIT_CONFIG_*
        # and GITHUB_TOKEN from that environment, and a stale environment sends a clone
        # elsewhere, such as to a previous test's deleted directory. The problem appears only
        # on Linux, because macOS has defaulted to spawn since Python 3.8.
        sources = tracked("*.py")
        if sources is None:
            self.skipTest("not inside a git checkout")
        pools = 0
        for path in sources:
            if not path.is_file() or path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            if "ProcessPoolExecutor(" not in text:
                continue
            pools += 1
            with self.subTest(path=str(path.relative_to(REPO_ROOT))):
                self.assertIn('mp_context=multiprocessing.get_context("spawn")', text,
                              "a process pool uses the platform default")
        self.assertGreater(pools, 0, "found no process pool to check")


class TestOneVersionPerPackage(unittest.TestCase):
    """A package that the pipeline depends on is pinned once, to one version."""

    def _requirements(self):
        files = tracked("*requirements.txt")
        if files is None:
            self.skipTest("not inside a git checkout")
        return requirement_pins(files)

    def test_two_spellings_of_one_package_are_one_package(self):
        with tempfile.TemporaryDirectory() as d:
            first, second = Path(d) / "a-requirements.txt", Path(d) / "b-requirements.txt"
            first.write_text("lingua-language-detector==2.2.0\n", encoding="utf-8")
            second.write_text("Lingua_Language.Detector==2.1.0\n", encoding="utf-8")
            pins = requirement_pins([first, second])
        self.assertEqual(list(pins), ["lingua-language-detector"])
        self.assertEqual(sorted(pins["lingua-language-detector"].values()),
                         ["==2.1.0", "==2.2.0"])

    def test_every_requirement_is_pinned_to_an_exact_version(self):
        pins = self._requirements()
        self.assertGreater(len(pins), 5, "found almost no requirements to check")
        for package, by_file in sorted(pins.items()):
            for path, spec in sorted(by_file.items()):
                with self.subTest(package=package, path=path):
                    self.assertRegex(
                        spec, r"^==\d",
                        f"{package} in {path} is not pinned to one version")

    def test_no_package_is_pinned_to_two_versions(self):
        for package, by_file in sorted(self._requirements().items()):
            with self.subTest(package=package):
                versions = sorted(set(by_file.values()))
                self.assertEqual(
                    len(versions), 1,
                    f"{package} is declared as {', '.join(versions)} across "
                    f"{', '.join(sorted(by_file))}")


LOCKS = REPO_ROOT / "locks"
# The dependency lock of every requirements file, which .venv, CI and the development image
# install. The other dependency locks are subsets of it.
DEVELOPMENT_LOCK = LOCKS / "development.txt"

# An entry of a dependency lock as uv writes it: `name==version`, an optional environment marker
# after a semicolon, and a backslash before the hashes on the lines that follow.
_LOCK_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*(.*?))?\s*\\?\s*$")
_LOCK_HASH = re.compile(r"^\s+--hash=(sha256:[0-9a-f]{64})\s*\\?\s*$")
# A line of the annotation below an entry: `# via X`, or `# via` followed by one `#   X` per line.
# X is a requirements file (`-r path`), a constraints file (`-c path`) or a distribution that
# depends on the entry.
_LOCK_VIA = re.compile(r"^\s+#\s+(?:via\s+)?(\S.*?)\s*$")
# The options of `uv pip compile` in the header of a dependency lock that take a value, and those
# that take none. uv leaves `--quiet` and the upgrade options out of the header.
_COMPILE_VALUES = {"--python-version", "--build-constraints", "--constraints", "--output-file"}
_COMPILE_FLAGS = {"--no-config", "--universal", "--generate-hashes"}


def read_lock(path: Path) -> dict:
    """The header and the entries of a dependency lock that dev/lock.sh wrote: {"options":
    {option: value or True}, "inputs": [requirements file], "entries": {normalized name:
    {"version", "marker", "hashes", "via"}}, "repeated": [normalized name]}. A name in "repeated"
    has two entries, which a dependency lock with one version per distribution never has."""
    lines = path.read_text(encoding="utf-8").splitlines()
    command = shlex.split(lines[1].lstrip("#")) if len(lines) > 1 else []
    if command[:3] != ["uv", "pip", "compile"]:
        raise ValueError(f"{path} does not name the uv pip compile command in its header")
    options, inputs, words = {}, [], iter(command[3:])
    for word in words:
        if word in _COMPILE_VALUES:
            options[word] = next(words)
        elif word in _COMPILE_FLAGS:
            options[word] = True
        elif word.startswith("-"):
            raise ValueError(f"{path} has the option {word}, which read_lock does not know")
        else:
            inputs.append(word)
    entries, repeated, current = {}, [], None
    for line in lines[2:]:
        entry = _LOCK_ENTRY.match(line)
        if entry:
            current = normalized(entry.group(1))
            if current in entries:
                repeated.append(current)
            entries[current] = {"version": entry.group(2), "marker": entry.group(3) or "",
                                "hashes": set(), "via": set()}
            continue
        hashed = _LOCK_HASH.match(line)
        via = _LOCK_VIA.match(line)
        if current and hashed:
            entries[current]["hashes"].add(hashed.group(1))
        elif current and via and via.group(1) != "via":
            entries[current]["via"].add(via.group(1))
    return {"options": options, "inputs": inputs, "entries": entries, "repeated": repeated}


def lock_disagreements(lock: Path, root: Path = REPO_ROOT) -> list:
    """One line for each disagreement between a dependency lock and the requirements files in its
    header, which `root` contains: a pin that the dependency lock lists at another version or not
    at all, as after a changed pin without a new dependency lock, and an entry that the dependency
    lock credits to a requirements file that no longer pins it."""
    content = read_lock(lock)
    entries = content["entries"]
    lines = []
    for relative in content["inputs"]:
        for name, by_line in requirement_pins([root / relative]).items():
            for at, spec in by_line.items():
                where = f"{relative}:{at.rsplit(':', 1)[1]}"
                entry = entries.get(name)
                if entry is None:
                    lines.append(f"{where} pins {name}{spec}, and {lock.name} does not list it")
                elif spec != f"=={entry['version']}":
                    lines.append(f"{where} pins {name}{spec}, and {lock.name} lists "
                                 f"{entry['version']}")
    for name, entry in sorted(entries.items()):
        for source in sorted(entry["via"]):
            if source.startswith("-r "):
                relative = source[3:]
                if (relative not in content["inputs"]
                        or name not in requirement_pins([root / relative])):
                    lines.append(f"{lock.name} lists {name} for {relative}, which does not "
                                 f"pin it")
    return lines


# `pip install` and `pip sync`, of pip and of `uv pip`, which the install rules below check.
_PIP_INSTALL = re.compile(r"\bpip3?\s+(?:install|sync)\b(.*)")
# The options of an install whose value names no distribution: the interpreter that `uv pip`
# installs into.
_INSTALL_VALUES = {"--python", "-p"}
# The commands that install the distributions that they name into an environment of their own,
# which no dependency lock constrains: `uvx NAME`, `uv tool install` and `uv tool run`,
# `uv run --with NAME` and pipx. `uvx` counts with an argument only, because the MCP configurations
# of the test fixtures name `"command": "uvx"` as data.
_UNLOCKED_INSTALL = re.compile(r"\buvx\s+[^\s\"'`\]})]|\buv\s+tool\s+(?:install|run)\b"
                               r"|\buv\s+run\b[^`\"'\n]*?\s--with\b|\bpipx\s+(?:install|run)\b")


def command_lines(text: str) -> list:
    """The parts of the lines of a shell script, a Dockerfile, a workflow, a document or a module
    that can hold one command: the continuation lines joined, the comment lines left out, and
    each line split at `&&`, `||` and `;`."""
    return [part for line in re.sub(r"\\\n", " ", text).splitlines()
            if not line.lstrip().startswith("#")
            for part in re.split(r"&&|\|\||;", line)]


def install_words(rest: str) -> list:
    """The arguments of an install from the text that follows `pip install` on its line, up to the
    backtick that ends a command in a document. Where the quotes of the rest do not pair, as in a
    command inside a string of a module, the arguments end at the first quote."""
    rest = rest.split("`", 1)[0]
    try:
        return shlex.split(rest)
    except ValueError:
        return re.split(r"[\"']", rest, maxsplit=1)[0].split()


def install_commands(text: str) -> list:
    """The arguments of every `pip install`, `uv pip install` and `uv pip sync` of `text`."""
    return [install_words(found.group(1)) for part in command_lines(text)
            for found in [_PIP_INSTALL.search(part)] if found]


def install_problems(text: str) -> list:
    """One line for each install of `text` that can install a distribution that no dependency lock
    lists. An install reads only the dependency locks under locks/, with --require-hashes, and
    builds with the setuptools of locks/build.txt (--no-build-isolation) except when it installs
    that dependency lock. An install of the editable package takes nothing else (--no-deps) and
    builds with that setuptools too. A command that installs a distribution of its own choosing
    is always a problem."""
    problems = [f"`{part.strip()}` installs a distribution that no dependency lock lists"
                for part in command_lines(text) if _UNLOCKED_INSTALL.search(part)]
    for args in install_commands(text):
        shown = " ".join(args)
        if "-e" in args:
            if not {"--no-deps", "--no-build-isolation"} <= set(args) or "-r" in args:
                problems.append(f"The editable install `{shown}` does not install the package "
                                f"alone with --no-deps and --no-build-isolation")
            continue
        locks, words = [], iter(args)
        for word in words:
            if word in ("-r", "--requirement"):
                locks.append(next(words, ""))
            elif word in _INSTALL_VALUES:
                next(words, "")
            elif not word.startswith("-"):
                problems.append(f"`{shown}` installs {word}, which no dependency lock names")
        if "--require-hashes" not in args:
            problems.append(f"`{shown}` does not require hashes")
        for lock in locks:
            if Path(lock).parent.name != "locks":
                problems.append(f"`{shown}` reads {lock}, which is not a dependency lock")
            elif Path(lock).name != "build.txt" and "--no-build-isolation" not in args:
                problems.append(f"`{shown}` can build in an isolated environment")
        if not locks:
            problems.append(f"`{shown}` reads no dependency lock")
    return problems


class TestTheDependencyLocks(unittest.TestCase):
    """Every environment installs from the dependency locks under locks/, which dev/lock.sh
    writes with uv from the requirements files. The requirements files pin the direct
    dependencies, and the dependency locks every distribution with the hashes of its files."""

    def setUp(self):
        self.locks = sorted(LOCKS.glob("*.txt"))
        self.assertGreater(len(self.locks), 1, "found no dependency locks under locks/")

    def test_every_requirements_file_is_an_input_of_the_development_lock(self):
        files = tracked("*requirements.txt")
        if files is None:
            self.skipTest("not inside a git checkout")
        inputs = set(read_lock(DEVELOPMENT_LOCK)["inputs"])
        for path in files:
            relative = path.relative_to(REPO_ROOT).as_posix()
            with self.subTest(requirements=relative):
                self.assertIn(relative, inputs, f"{DEVELOPMENT_LOCK.name} does not lock "
                              f"{relative}. Run `mise exec -- bash dev/lock.sh`.")

    def test_every_lock_agrees_with_its_requirements_files(self):
        """A pin that changed without `dev/lock.sh` leaves the dependency lock at the old version,
        and the environments would install a version that no requirements file states."""
        for lock in self.locks:
            with self.subTest(lock=lock.name):
                self.assertEqual(lock_disagreements(lock), [],
                                 "Run `mise exec -- bash dev/lock.sh`.")

    def test_a_changed_pin_and_a_removed_pin_disagree_with_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "locks").mkdir()
            (root / "requirements.txt").write_text("pandas==3.0.6\n", encoding="utf-8")
            lock = root / "locks" / "fake.txt"
            lock.write_text(
                "# This file was autogenerated by uv via the following command:\n"
                "#    uv pip compile --universal --output-file locks/fake.txt requirements.txt\n"
                "numpy==2.5.3 \\\n    --hash=sha256:" + "a" * 64 + "\n"
                "    # via\n    #   -r requirements.txt\n    #   pandas\n"
                "pandas==3.0.5 \\\n    --hash=sha256:" + "b" * 64 + "\n"
                "    # via -r requirements.txt\n", encoding="utf-8")
            self.assertEqual(lock_disagreements(lock, root), [
                "requirements.txt:1 pins pandas==3.0.6, and fake.txt lists 3.0.5",
                "fake.txt lists numpy for requirements.txt, which does not pin it"])

    def test_every_lock_has_one_hashed_entry_per_distribution(self):
        for lock in self.locks:
            content = read_lock(lock)
            with self.subTest(lock=lock.name):
                self.assertGreater(len(content["entries"]), 0)
                self.assertEqual(content["repeated"], [])
                self.assertEqual([name for name, entry in content["entries"].items()
                                  if not entry["hashes"]], [])

    def test_every_lock_is_universal_for_the_python_of_the_version_file(self):
        for lock in self.locks:
            options = read_lock(lock)["options"]
            with self.subTest(lock=lock.name):
                self.assertTrue(options.get("--universal"))
                self.assertTrue(options.get("--generate-hashes"))
                self.assertEqual(options.get("--python-version"), version_text(declared_version()))
                self.assertEqual(options.get("--output-file"),
                                 lock.relative_to(REPO_ROOT).as_posix())

    def test_every_lock_has_the_versions_of_the_development_lock(self):
        """One version of a distribution in every environment: each other dependency lock is
        resolved under the development lock as constraints and has its entries with the same
        version, marker and hashes."""
        development = read_lock(DEVELOPMENT_LOCK)["entries"]
        for lock in self.locks:
            if lock == DEVELOPMENT_LOCK:
                continue
            content = read_lock(lock)
            with self.subTest(lock=lock.name):
                self.assertEqual(content["options"].get("--constraints"),
                                 DEVELOPMENT_LOCK.relative_to(REPO_ROOT).as_posix())
                for name, entry in content["entries"].items():
                    self.assertIn(name, development)
                    self.assertEqual({key: entry[key] for key in ("version", "marker", "hashes")},
                                     {key: development[name][key]
                                      for key in ("version", "marker", "hashes")}, name)

    def test_every_lock_is_installed_by_an_environment(self):
        """A dependency lock that no environment installs states versions that nothing tests:
        locks/build.txt everywhere first, locks/development.txt in .venv, CI and the development
        image, and locks/pipeline.txt in the production image."""
        installers = {"dev/setup.sh": None, ".github/workflows/pipeline-tests.yml": None,
                      "deploy/Dockerfile": None, "deploy/Dockerfile.development": None}
        read = {}
        for relative in installers:
            for args in install_commands((REPO_ROOT / relative).read_text(encoding="utf-8")):
                for before, word in zip(args, args[1:]):
                    if before in ("-r", "--requirement"):
                        read.setdefault(Path(word).name, set()).add(relative)
        self.assertEqual(set(read), {lock.name for lock in self.locks})
        self.assertEqual(read["pipeline.txt"], {"deploy/Dockerfile"})
        self.assertEqual(read["development.txt"],
                         {"dev/setup.sh", ".github/workflows/pipeline-tests.yml",
                          "deploy/Dockerfile.development"})

    def test_the_build_lock_pins_the_build_backend_of_the_package(self):
        """The editable install builds with the setuptools of locks/build.txt, not with the one
        that an isolated build would download for pyproject.toml's `requires`."""
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        required = {normalized(_REQUIREMENT.match(item).group(1))
                    for item in project["build-system"]["requires"]}
        self.assertEqual(set(read_lock(LOCKS / "build.txt")["entries"]), required)


class TestEveryInstallReadsALock(unittest.TestCase):
    """CI, both images and dev/setup.sh install Python distributions only from the dependency
    locks, in hash-checking mode, so that an environment cannot hold a distribution that no
    dependency lock lists."""

    def test_every_install_of_the_repository_reads_a_lock(self):
        """The scripts, the Dockerfiles and the workflows, and the documents and the modules,
        whose install commands a reader runs. The records under docs/history/ describe earlier
        states, and a test module holds install commands as fixtures and as patterns that read
        them."""
        documents = [path for path in [*(tracked("*.md") or []), *(tracked("*.py") or [])]
                     if not path.relative_to(REPO_ROOT).as_posix().startswith("docs/history/")
                     and not path.name.startswith("test_")]
        files = [*(tracked("*.sh") or []), *(tracked("*Dockerfile*") or []),
                 *(tracked(".github/workflows/*.yml") or []), *documents]
        self.assertGreater(len(documents), 50, "found almost no documents and modules")
        installing = set()
        for path in files:
            text = path.read_text(encoding="utf-8")
            relative = path.relative_to(REPO_ROOT).as_posix()
            if install_commands(text):
                installing.add(relative)
            with self.subTest(file=relative):
                self.assertEqual(install_problems(text), [])
                # setuptools is installed before anything that it builds.
                locks = [word for args in install_commands(text) for word in args
                         if word.endswith(".txt")]
                if locks:
                    self.assertTrue(locks[0].endswith("locks/build.txt"), locks)
        self.assertLessEqual({".github/workflows/pipeline-tests.yml", "deploy/Dockerfile",
                              "deploy/Dockerfile.development", "dev/setup.sh"}, installing)

    def test_an_install_without_a_lock_is_a_problem(self):
        for command in ("pip install -r requirements.txt",
                        "pip install --require-hashes -r 1_sampling/requirements.txt",
                        "uv pip install --require-hashes --no-build-isolation -r locks/a.txt httpx",
                        "uv pip install -r locks/a.txt --no-build-isolation",
                        "uv pip install --require-hashes -r locks/a.txt",
                        "uv pip install --python .venv/bin/python --require-hashes "
                        "--no-build-isolation -r locks/a.txt httpx",
                        "pip install -e .",
                        "RUN pip install --no-cache-dir \\\n    -r /requirements/x.txt",
                        "Install it once with `pip install -e .` from the repository root.",
                        '            f"python3 -m pip install -r 4_data-analysis/rq2/'
                        'requirements.txt"',
                        "uvx ruff check .",
                        "Run `uvx --from coverage coverage report`.",
                        "uv tool install ruff",
                        "uv run --with pandas python3 script.py",
                        "pipx install coverage"):
            with self.subTest(command=command):
                self.assertNotEqual(install_problems(command), [])
        for command in ("uv pip install --require-hashes -r locks/build.txt",
                        "uv pip install --exact --require-hashes --no-build-isolation "
                        "-r locks/a.txt",
                        "uv pip install --python .venv/bin/python --require-hashes "
                        "-r locks/build.txt",
                        "pip install --no-deps --no-build-isolation -e /opt/pipeline",
                        "RUN pip install --require-hashes -r /locks/build.txt \\\n"
                        " && pip install --require-hashes --no-build-isolation -r /locks/a.txt",
                        "# pip install -r requirements.txt",
                        "python -m pip install --no-deps --no-build-isolation -e .",
                        "which pip installs in hash-checking mode",
                        'self._file(".mcp.json", \'{"mcpServers": {"x": {"command": "uvx"}}}\')',
                        "uv run python3 -m unittest tests.test_paths"):
            with self.subTest(command=command):
                self.assertEqual(install_problems(command), [])


class TestTheProjectEnvironment(unittest.TestCase):
    """On a development machine and in CI, the project runs in .venv, the virtual environment
    that mise creates from the Python of mise.toml and activates under `mise exec`, rather than in
    mise's shared installation of Python, which other projects install into."""

    def setUp(self):
        self.config = tomllib.loads(VERSION_FILE.read_text(encoding="utf-8"))

    def test_mise_creates_and_activates_the_virtual_environment_of_the_checkout(self):
        self.assertEqual(self.config.get("env", {}).get("_", {}).get("python", {}).get("venv"),
                         {"path": ".venv", "create": True})

    def test_git_ignores_the_virtual_environment(self):
        result = subprocess.run(["git", "check-ignore", "-q", ".venv/bin/python"],
                                cwd=REPO_ROOT, capture_output=True)
        if result.returncode == 128:
            self.skipTest("not inside a git checkout")
        self.assertEqual(result.returncode, 0)

    def test_the_version_file_pins_an_exact_version_of_uv(self):
        self.assertRegex(str(self.config["tools"].get("aqua:astral-sh/uv")), r"^\d+\.\d+\.\d+$")

    def test_the_lock_script_refuses_another_version_of_uv(self):
        """The dependency locks come from the uv that mise.toml pins, so that two machines write
        the same dependency locks from the same requirements files."""
        before = {path: path.read_bytes() for path in LOCKS.glob("*.txt")}
        with tempfile.TemporaryDirectory() as tmp:
            uv = Path(tmp) / "uv"
            uv.write_text("#!/bin/sh\necho 'uv 0.0.1'\n", encoding="utf-8")
            uv.chmod(0o755)
            result = subprocess.run(
                [BASH, str(REPO_ROOT / "dev" / "lock.sh")], cwd=REPO_ROOT,
                env={**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}"},
                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("This is uv 0.0.1", result.stderr)
        self.assertEqual({path: path.read_bytes() for path in LOCKS.glob("*.txt")}, before)

    def test_the_lock_script_resolves_for_major_minor_of_the_pinned_release(self):
        """uv takes `--python-version` as the lowest Python that a dependency lock supports. The
        script passes major.minor of the pin, the version in the header of every dependency lock,
        and writes the dependency locks under locks/ and no other."""
        before = {path: path.read_bytes() for path in LOCKS.glob("*.txt")}
        pinned_uv = self.config["tools"]["aqua:astral-sh/uv"]
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp) / "calls.jsonl"
            uv = Path(tmp) / "uv"
            uv.write_text(f"#!{sys.executable}\n"
                          "import json, os, sys\n"
                          "if sys.argv[1:] == ['--version']:\n"
                          f"    print('uv {pinned_uv}')\n"
                          "else:\n"
                          "    with open(os.environ['UV_CALLS'], 'a') as f:\n"
                          "        f.write(json.dumps(sys.argv[1:]) + '\\n')\n", encoding="utf-8")
            uv.chmod(0o755)
            result = subprocess.run(
                [BASH, str(REPO_ROOT / "dev" / "lock.sh")], cwd=REPO_ROOT,
                env={**os.environ, "PATH": f"{tmp}{os.pathsep}{os.environ['PATH']}",
                     "UV_CALLS": str(calls)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            compiles = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual({path: path.read_bytes() for path in LOCKS.glob("*.txt")}, before)
        major_minor = ".".join(pinned_release().split(".")[:2])
        self.assertEqual(sorted(args[args.index("--output-file") + 1] for args in compiles),
                         sorted(lock.relative_to(REPO_ROOT).as_posix()
                                for lock in LOCKS.glob("*.txt")))
        for args in compiles:
            with self.subTest(output=args[args.index("--output-file") + 1]):
                self.assertEqual(args[:2], ["pip", "compile"])
                self.assertEqual(args[args.index("--python-version") + 1], major_minor)


def step_script(workflow: str, name: str) -> str:
    """The `run: |` block of the step of `workflow` that is called `name`, without its
    indentation."""
    lines = workflow[workflow.index(f"- name: {name}\n"):].splitlines()[1:]
    start = next(number for number, line in enumerate(lines) if line.strip() == "run: |")
    indent = len(lines[start]) - len(lines[start].lstrip())
    block = []
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return textwrap.dedent("\n".join(block)).strip() + "\n"


class TestTheSetupScript(unittest.TestCase):
    """dev/setup.sh installs into .venv of the checkout what CI installs into its .venv, and
    refuses another environment and a .venv of another release of Python. The script and the
    install step of the workflow run in a repository that the test creates, with a `uv` first on
    PATH that records its arguments and the variables of uv that it sees, and with a .venv whose
    `python` is the interpreter that runs the test."""

    # Variables of a developer's shell that make uv read a configuration file, build a
    # distribution from its source distribution instead of installing its wheel, install into
    # another interpreter than the one of .venv, install the package of the checkout from a wheel
    # rather than editable, or constrain, override or exclude distributions of the dependency lock
    # with another requirements file. The installs can pass the hash check either way and install
    # other files than CI installs, or install them elsewhere.
    UV_SETTINGS = {"UV_CONFIG_FILE": "/elsewhere/uv.toml", "UV_NO_BINARY": "1",
                   "UV_NO_BINARY_PACKAGE": "numpy", "UV_NO_BUILD": "1",
                   "UV_NO_BUILD_PACKAGE": "venn", "UV_PYTHON": "/elsewhere/bin/python",
                   "UV_SYSTEM_PYTHON": "1", "UV_NO_EDITABLE": "1",
                   "UV_EXCLUDE": "/elsewhere/excludes.txt",
                   "UV_OVERRIDE": "/elsewhere/overrides.txt",
                   "UV_CONSTRAINT": "/elsewhere/constraints.txt"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "checkout"
        (self.root / "dev").mkdir(parents=True)
        (self.root / "data" / "collection").mkdir(parents=True)
        shutil.copy(REPO_ROOT / "dev" / "setup.sh", self.root / "dev" / "setup.sh")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.venv = self.root / ".venv"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin" / "python").symlink_to(sys.executable)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "uv-calls.jsonl"
        uv = self.bin / "uv"
        uv.write_text(f"#!{sys.executable}\n"
                      "import json, os, sys\n"
                      "seen = sorted(name for name in os.environ if name.startswith('UV_')\n"
                      "              and name != 'UV_CALLS')\n"
                      "with open(os.environ['UV_CALLS'], 'a') as f:\n"
                      "    f.write(json.dumps({'args': sys.argv[1:], 'seen': seen}) + '\\n')\n",
                      encoding="utf-8")
        uv.chmod(0o755)
        # The prerequisites that the script looks for and that the installs do not run.
        for tool in ("7zz", "rsync", "pdftotext"):
            stub = self.bin / tool
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
        # The checkout as git names it, which resolves a symlink such as /var on macOS.
        self.top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=self.root,
                                  capture_output=True, text=True, check=True).stdout.strip()

    def _environment(self, active, **variables: str) -> dict:
        """The environment of a run: the stand-in `uv` first on PATH, `active` as VIRTUAL_ENV,
        unset when None, and `variables`."""
        env = {key: value for key, value in os.environ.items()
               if key != "VIRTUAL_ENV" and not key.startswith("UV_")}
        env.update(PATH=f"{self.bin}{os.pathsep}{os.environ['PATH']}", UV_CALLS=str(self.calls),
                   **variables)
        if active is not None:
            env["VIRTUAL_ENV"] = str(active)
        return env

    def _calls(self) -> list:
        """The calls of uv since the last one read, each as {"args", "seen"}."""
        if not self.calls.exists():
            return []
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.calls.unlink()
        return calls

    def _setup(self, pin: str, active, **variables: str) -> tuple:
        """Run the script with `pin` as the release of Python in mise.toml, `active` as
        VIRTUAL_ENV, unset when None, and `variables` in its environment. Returns the result and
        the arguments of the recorded calls of uv."""
        (self.root / "mise.toml").write_text(f'[tools]\npython = "{pin}"\n', encoding="utf-8")
        result = subprocess.run(["bash", str(self.root / "dev" / "setup.sh")], cwd=self.root,
                                env=self._environment(active, **variables),
                                capture_output=True, text=True, timeout=120)
        self.recorded = self._calls()
        return result, [call["args"] for call in self.recorded]

    def _workflow_install(self, **variables: str) -> list:
        """Run the install step of the workflow as GitHub runs a `run` block, with bash and
        `-eo pipefail`, in the checkout with `variables` in its environment. Returns the
        recorded calls of uv."""
        workflow = (WORKFLOWS / "pipeline-tests.yml").read_text(encoding="utf-8")
        script = step_script(workflow, "Install the development lock and the package")
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
                                cwd=self.root, env=self._environment(self.venv, **variables),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return self._calls()

    def test_setup_refuses_to_install_outside_the_virtual_environment_of_the_checkout(self):
        """An install into mise's shared installation of Python or into another environment
        leaves .venv unchanged, and the suites would pass in an environment that CI does not
        have."""
        other = self.tmp / "other-venv"
        other.mkdir()
        for active in (None, other):
            with self.subTest(active=active):
                result, calls = self._setup(platform.python_version(), active)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn(".venv is not the active virtual environment", result.stderr)
                self.assertEqual(calls, [])

    def test_setup_refuses_a_virtual_environment_of_another_release(self):
        """mise keeps .venv when the pin changes, and the script names how to create it again."""
        result, calls = self._setup("3.14.999", self.venv)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"runs Python {platform.python_version()}, and mise.toml pins 3.14.999",
                      result.stderr)
        self.assertIn("Remove it with rm -rf", result.stderr)
        self.assertEqual(calls, [])

    def test_setup_installs_what_ci_installs(self):
        """The same installs in the same order, with --exact, which removes a distribution that
        the development lock does not list, and --reinstall, which replaces one installed by hand
        at a locked version, each into the interpreter of .venv that --python names. Neither the
        script nor the workflow lets uv read a configuration or a variable of `UV_SETTINGS`,
        which a developer's shell can set and CI does not."""
        result, calls = self._setup(platform.python_version(), self.venv, **self.UV_SETTINGS)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        workflow = self._workflow_install(**self.UV_SETTINGS)

        def relative(word: str) -> str:
            return "." if word == self.top else word.removeprefix(self.top + "/")

        setup = [[relative(word) for word in args if word != "-q"] for args in calls]
        self.assertEqual(setup, [call["args"] for call in workflow])
        self.assertEqual(len(setup), 3, setup)
        exact = next(args for args in setup if "locks/development.txt" in args)
        self.assertLessEqual({"--exact", "--reinstall", "--require-hashes"}, set(exact))
        for args in setup:
            with self.subTest(args=args):
                self.assertIn("--python", args)
                self.assertEqual(args[args.index("--python") + 1], ".venv/bin/python")
        for call in [*self.recorded, *workflow]:
            with self.subTest(call=call["args"]):
                self.assertIn("--no-config", call["args"])
                self.assertEqual(sorted(set(call["seen"]) & self.UV_SETTINGS.keys()), [])


class TestWorkflowSuffixes(unittest.TestCase):

    def test_both_suffixes_reject_a_python_version_and_accept_mise(self):
        from unittest import mock
        for suffix in ('yml', 'yaml'):
            for valid in (False, True):
                with self.subTest(suffix=suffix, valid=valid), tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    text = ('steps:\n  - uses: jdx/mise-action@v4\n' if valid else
                            'steps:\n  - uses: actions/setup-python@v6\n'
                            '    with:\n      python-version: "3.13"\n')
                    (root / f'pipeline.{suffix}').write_text(text)
                    case = TestOnePythonVersion('test_no_workflow_writes_a_version_of_its_own')
                    result = unittest.TestResult()
                    with mock.patch.dict(globals(), WORKFLOWS=root):
                        case.run(result)
                    self.assertEqual(result.errors, [])
                    self.assertEqual(bool(result.failures), not valid)


if __name__ == "__main__":
    unittest.main()
