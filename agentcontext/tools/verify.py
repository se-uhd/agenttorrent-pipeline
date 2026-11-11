"""The complete verification suite: `python -m agentcontext verify [report.txt]`.

Runs every suite that the workflow `pipeline-tests.yml` runs, then the checks of the current
development dataset and the reproduction check of the analysis. The counts of the dataset paper
are no acceptance criteria for the results of new heuristics. Exits 0 when every suite and check
passed and the working tree is clean, 2 when a suite or a check failed or the working tree is not
clean, and 1 when git could not report whether the working tree is clean. It exits 1 without
running anything while AGENTCONTEXT_DATA_ROOT names a data root, because every suite and check
would read that data root instead of the `data/` of this checkout, which CI checks.

Before the first suite, the command compares the Python environment that runs it with the one
in which CI runs the suites: the release of Python that mise.toml pins, the distributions of the
dependency lock locks/development.txt and the editable install of this checkout. It exits 1
without running anything, and logs one line for each difference, when the interpreter is another
release, when the `python3` on PATH, which the suites start by name, belongs to another
environment, when a distribution is installed that the dependency lock does not list, when a
distribution that it lists is missing or installed at another version, when a distribution was
installed from a URL or a local path, when the package of this checkout is not installed editable
from this checkout, when site-packages holds a file that no RECORD of a distribution lists, or when
a file differs from the SHA-256 that its RECORD lists. Every suite and check runs with the
interpreter that runs the command and without PYTHONPATH, as CI runs a step whose `env:` does not
set it. A suite whose step sets it runs with this checkout as the only entry of PYTHONPATH. A
suite that CI runs before its install runs with the interpreter of an empty virtual environment,
which the command creates from its own interpreter in a temporary directory and activates in
place of its own.

With --analysis-output-dir DIR, the reproduction check compares two analysis runs in DIR instead
of comparing the generated outputs with the tracked files.
"""

import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import site
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlparse

from agentcontext import REPO_ROOT
from agentcontext import cli
from agentcontext import logs

WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pipeline-tests.yml"
# The file that pins the release of Python of .venv, of CI and of the base image of both images.
VERSION_FILE = REPO_ROOT / "mise.toml"
# The virtual environment of the checkout, which mise creates from the pinned release and
# dev/setup.sh installs into.
VENV = REPO_ROOT / ".venv"
# The variable that names another data root than the `data/` of this checkout
# (`agentcontext.DATA_ROOT`). Every suite and check would read the data root that it names, such
# as a working copy of a handover, while `git status` checks this checkout, and CI never sets it.
DATA_ROOT_VARIABLE = "AGENTCONTEXT_DATA_ROOT"

# The dependency lock of the environment in which the verification runs: every requirements file
# of the repository, with each distribution at one version (dev/lock.sh).
LOCK = REPO_ROOT / "locks" / "development.txt"
# The name that pyproject.toml gives the package of this repository, which the environment holds
# as an editable install of this checkout.
PROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["name"]
# What the environment holds besides the dependency lock: the editable install of this
# repository and pip, the installer that `python -m venv` puts into a virtual environment, such as
# the one of the development container.
UNLOCKED = frozenset({PROJECT, "pip"})
# What an installation of CPython puts at the top of its own site-packages and no distribution
# lists: README.txt, which the base image of both images has and which the virtual environment of
# the development container sees through the system's site packages.
UNRECORDED = frozenset({"README.txt"})
# The lines of each kind that the comparison of site-packages logs before it counts the rest.
SHOWN = 10
# An entry of a dependency lock as uv writes it, `name==version`, with an optional environment
# marker after a semicolon and the continuation of the hashes on the following lines.
_LOCK_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)\s*(?:;\s*(.*?))?\s*\\?\s*$")

# The workflow's step names and their working directories, in the workflow's order. Each suite
# runs with the interpreter options and the test modules that the step names (`unittest_command`).
SUITES = (("Run the sampling runner suite", REPO_ROOT / "1_sampling"),
          ("Run the sampling script suites", REPO_ROOT / "1_sampling"),
          ("Run collection suites", REPO_ROOT),
          ("Run classification suite", REPO_ROOT / "2_classification"),
          ("Run analysis suite", REPO_ROOT / "4_data-analysis"),
          ("Run pipeline runner suite", REPO_ROOT),
          ("Run container suite", REPO_ROOT / "deploy"),
          ("Run website suite", REPO_ROOT / "_website"))
# The repositories from which an unmatched path glob fails the verification. A glob in one
# repository is material that a project keeps under a tool's directory.
UNMATCHED_PATHS_FAIL_ABOVE = 2
# Each check as its label, the arguments of the interpreter that runs the verification, and its
# working directory.
CHECKS = (("check_reproduction.py", ["check_reproduction.py"], REPO_ROOT / "4_data-analysis"),
          ("regressions check", ["-m", "agentcontext", "regressions", "check"], REPO_ROOT),
          ("snapshot output sections", ["-m", "agentcontext", "snapshot", "write", "--check", "--outputs-only"], REPO_ROOT),
          # The threshold is here rather than in the runner's step: A path glob that the
          # heuristics do not explain is a reading task, and the verification runs where a person
          # is present and can fix it at once. The pipeline run logs the same findings and
          # continues.
          ("unmatched paths", ["-m", "agentcontext", "unmatched", "paths",
                           "--fail-above", str(UNMATCHED_PATHS_FAIL_ABOVE)], REPO_ROOT),
          ("output cross check", ["-m", "agentcontext", "output-cross-check"], REPO_ROOT),
          # The check requires that every license that the last sampling frame keeps is reviewed
          # in config/licenses.csv and that the review agrees with the tracked SPDX list. A new
          # license is a reading task.
          ("licenses", ["4_filter_ghs.py", "--check-licenses"], REPO_ROOT / "1_sampling"),
          ("statistics", ["-m", "agentcontext", "statistics", "--check"], REPO_ROOT))


def step_text(step: str) -> str:
    """The text of a workflow step, from its name to the next step."""
    text = WORKFLOW.read_text(encoding="utf-8")
    block = text[text.index(step):]
    return block[:block.index("- name:", 10)] if "- name:" in block[10:] else block


def modules(step: str) -> list:
    """The test modules that a workflow step lists, one per line under its `python3 -m unittest`."""
    return re.findall(r"^\s+((?:tests\.)?test_\w+)\s*\\?$", step_text(step), re.M)


def interpreter_options(step: str) -> list:
    """The options that a workflow step gives `python3` before `-m unittest`, such as `-S`, which
    imports no site packages."""
    found = re.search(r"\bpython3((?:\s+-[A-Za-z]+)*)\s+-m unittest\b", step_text(step))
    return found.group(1).split() if found else []


def step_variables(step: str) -> dict:
    """{name: value} of the variables that the `env:` block of a workflow step sets, such as the
    PYTHONPATH of the sampling runner suite. A comment line of the block sets nothing."""
    block = re.search(r"(?m)^([ \t]+)env:[ \t]*\n((?:\1[ \t]+\S.*(?:\n|$))*)", step_text(step))
    if block is None:
        return {}
    return dict(re.findall(r"(?m)^[ \t]+([A-Za-z_][A-Za-z0-9_]*):[ \t]*(.*?)[ \t]*$",
                           block.group(2)))


def unittest_command(step: str, interpreter: str | None = None) -> list:
    """The command of a suite: `interpreter`, by default the one that runs the verification, with
    the options and the test modules of its workflow step. CI runs `python3`, the interpreter of
    its .venv, and the verification the interpreter of its own environment, which
    `environment_problems` compares with the `python3` on PATH."""
    return [interpreter or sys.executable, *interpreter_options(step), "-m", "unittest",
            *modules(step)]


def runs_before_the_install(step: str) -> bool:
    """Whether CI runs the workflow step before it installs anything with pip or uv, in the .venv
    that mise has created and nothing has been installed into. A comment line of the workflow is
    no install."""
    text = WORKFLOW.read_text(encoding="utf-8")
    install = re.search(r"(?m)^(?!\s*#).*\bpip\s+install\b", text)
    return install is None or text.index(f"- name: {step}\n") < install.start()


def empty_environment(directory: Path) -> Path:
    """Create a virtual environment without pip in `directory` from the interpreter that runs the
    verification, and return `directory`. Its site-packages holds no distribution, as the .venv
    of CI before the install, and its interpreter is the release that the verification runs.
    The `venv` module of the standard library creates it, because the development container,
    which runs the verification too, has no uv."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(directory)],
                   check=True, capture_output=True, text=True)
    return directory


def normalized(name: str) -> str:
    """A distribution name as pip and uv compare it (PEP 503), in which case and every run of
    `-`, `_` and `.` do not matter: the metadata of an installed `typing_extensions` names the
    distribution that a dependency lock lists as `typing-extensions`."""
    return re.sub(r"[-_.]+", "-", name).lower()


def locked_versions(text: str) -> dict:
    """{normalized name: version} of the entries of a dependency lock whose environment marker
    holds for the running interpreter. A universal dependency lock has entries for other
    platforms too, such as pywin32 for Windows, which this interpreter does not install."""
    from packaging.markers import Marker
    locked = {}
    for line in text.splitlines():
        entry = _LOCK_ENTRY.match(line)
        if entry and (not entry.group(3) or Marker(entry.group(3)).evaluate()):
            locked[normalized(entry.group(1))] = entry.group(2)
    return locked


def pinned_python() -> str:
    """The release of Python that `VERSION_FILE` pins, such as 3.14.10."""
    return str(tomllib.loads(VERSION_FILE.read_text(encoding="utf-8"))["tools"]["python"])


def interpreter_differences(running: str, pinned: str) -> list:
    """The line that says that `running`, the release of the running interpreter, is not
    `pinned`, the release of `VERSION_FILE`, or none. mise creates .venv once and keeps it when
    the pin changes, so the line says how to create it again."""
    if running == pinned:
        return []
    return [f"This is Python {running}, and {VERSION_FILE.name} pins {pinned}. mise keeps a "
            f"virtual environment when the pin changes: remove {VENV} and run mise exec -- bash "
            f"dev/setup.sh, which creates it again from the pin."]


def path_interpreter_differences(path: str | None = None) -> list:
    """The line that says that the `python3` on `path`, by default the PATH of this process, is
    missing or belongs to another environment than the running interpreter, or none. The suites
    and the steps of the runner start `python3` by name, and in another environment they would
    test other distributions than those that the comparison with `LOCK` checked."""
    found = shutil.which("python3", path=path)
    if found is None:
        return ["No python3 is on PATH, and the suites start python3 by name."]
    try:
        reported = subprocess.run([found, "-c", "import sys; print(sys.prefix)"],
                                  capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as error:
        return [f"Could not start {found}, the python3 on PATH ({error})."]
    prefix = reported.stdout.strip()
    if reported.returncode != 0 or not prefix:
        return [f"{found}, the python3 on PATH, did not report its environment (exit "
                f"{reported.returncode})."]
    if os.path.realpath(prefix) != os.path.realpath(sys.prefix):
        return [f"The python3 on PATH, {found}, is the interpreter of {prefix}, and this one is "
                f"the interpreter of {sys.prefix}. The suites start python3 by name. Run the "
                f"verification with mise exec -- bash dev/verify.sh, in which both are the "
                f"interpreter of .venv."]
    return []


def site_distributions() -> list:
    """The distributions in the site-packages of the running interpreter (`site_directories`),
    in the order of `sys.path`. The other entries of `sys.path` are left out, such as the
    working directory, which `python -m` puts first. The root of a checkout can hold an
    `*.egg-info` folder from a build of the package with setuptools, which .gitignore hides and
    which neither dev/setup.sh nor a new .venv removes. Read from there, the folder would come
    before the metadata of the editable install in site-packages, which holds its
    `direct_url.json`, and would count as a distribution that the dependency lock does not
    list."""
    return list(importlib.metadata.distributions(path=[str(d) for d in site_directories()]))


def installed_versions() -> dict:
    """{normalized name: version} of the distributions in the site-packages of the running
    interpreter. Of two distributions with one name, such as pip in a virtual environment with
    the system's site packages, the first on `sys.path` is the one that an import finds."""
    installed = {}
    for distribution in site_distributions():
        name = distribution.metadata["Name"]
        if name:
            installed.setdefault(normalized(name), distribution.version)
    return installed


def direct_installs() -> dict:
    """{normalized name: the `direct_url.json` of its metadata} of the distributions in the
    site-packages of the running interpreter that an installer took from a URL or a local path,
    such as an editable install or a wheel built from a local directory, rather than from the
    files of an index (PEP 610). A record that is not JSON is read as {}."""
    found, seen = {}, set()
    for distribution in site_distributions():
        name = distribution.metadata["Name"]
        if not name or normalized(name) in seen:
            continue
        seen.add(normalized(name))
        record = distribution.read_text("direct_url.json")
        if record is not None:
            try:
                found[normalized(name)] = json.loads(record)
            except json.JSONDecodeError:
                found[normalized(name)] = {}
    return found


def editable_from(record: dict, root: Path) -> bool:
    """Whether `record`, the `direct_url.json` of a distribution, names an editable install of
    the directory `root`."""
    location = urlparse(str(record.get("url", "")))
    return (location.scheme == "file" and (record.get("dir_info") or {}).get("editable") is True
            and Path(unquote(location.path)).resolve() == root.resolve())


def direct_install_differences(direct: dict, lock: str, root: Path = REPO_ROOT) -> list:
    """One line for each distribution of `direct` other than `PROJECT` and pip, and one line
    when `PROJECT` is not an editable install of `root`, as CI and dev/setup.sh install it. A
    distribution from a URL or a local path can have the version that `lock` lists and other
    files, as a fork of the distribution does, and the comparison of versions does not see it.
    pip is left out, as in `environment_differences`."""
    lines = []
    for name, record in sorted(direct.items()):
        if name not in (PROJECT, "pip"):
            lines.append(f"{name} is installed from "
                         f"{record.get('url') or 'a location that it does not name'} rather than "
                         f"from the files that {lock} lists.")
    project = direct.get(PROJECT)
    if project is None:
        lines.append(f"{PROJECT} is not installed as an editable install of {root}, as CI and "
                     f"dev/setup.sh install it.")
    elif not editable_from(project, root):
        lines.append(f"{PROJECT} is installed from "
                     f"{project.get('url') or 'a location that it does not name'}, and CI and "
                     f"dev/setup.sh install it as an editable install of {root}.")
    return lines


def site_directories() -> list:
    """The site-packages directories of the running interpreter in the order of `sys.path`: the
    one of its environment, the system's when a virtual environment includes it, such as the one
    of the development container, and the user's when Python reads it."""
    found = site.getsitepackages() + ([site.getusersitepackages()] if site.ENABLE_USER_SITE else [])
    wanted = {os.path.realpath(directory) for directory in found}
    return [Path(entry) for entry in dict.fromkeys(sys.path)
            if entry and os.path.realpath(entry) in wanted and os.path.isdir(entry)]


def counted(lines: list, rest: str) -> list:
    """The first `SHOWN` of `lines`, and a line that counts the others as `rest`."""
    if len(lines) <= SHOWN:
        return lines
    return lines[:SHOWN] + [f"{len(lines) - SHOWN} more {rest}."]


def unrecorded_remedy(paths: list, prefix: str) -> str:
    """The line that says how to remove `paths`, the files and directories of site-packages that
    no RECORD lists, from the environment of `prefix`, the one that runs the verification. The
    installs of dev/setup.sh replace every locked distribution and remove every unlocked one, and
    they leave such a path, so the line says to create `VENV` again when `prefix` is `VENV` and
    every path is in it. dev/setup.sh does not install into another environment, such as the
    virtual environment of the development container, or into site-packages that an environment
    includes, such as those of the image, and the line then names the environment that runs the
    verification."""
    root = os.path.realpath(prefix)
    inside = all(os.path.commonpath([root, os.path.realpath(path)]) == root for path in paths)
    if inside and root == os.path.realpath(VENV):
        return (f"dev/setup.sh does not remove a file or a directory that no RECORD lists. Remove "
                f"{VENV} and run mise exec -- bash dev/setup.sh, which creates it again and "
                f"installs the locked distributions into it.")
    return (f"The verification runs in the environment {prefix}, and dev/setup.sh installs into "
            f"{VENV} only. Remove each file and directory that no RECORD lists, or create the "
            f"environment that holds it again with the distributions of "
            f"{LOCK.relative_to(REPO_ROOT).as_posix()} only.")


def site_packages_differences(directories: list, prefix: str) -> list:
    """The files of `directories`, the site-packages of the environment of `prefix`, in which they
    differ from the RECORD files of their distributions (PEP 376, PEP 627): an entry or a file
    that no RECORD lists, followed by the line that says how to remove it (`unrecorded_remedy`),
    and a file that is missing or has another hash than its RECORD lists.

    The `__pycache__` directories that Python writes on import, and the files of `UNRECORDED`,
    are left out. Of two distributions with one name, only the first on the path is compared
    with its RECORD, because an import finds that one, as in `installed_versions`. The base
    image of both images deletes the tests of its pip, which a pip in a virtual environment then
    hides. A distribution without a RECORD owns no file."""
    unrecorded, changed = [], []
    listed, owned, compared = set(), set(), set()
    for distribution in importlib.metadata.distributions(path=[str(d) for d in directories]):
        name = normalized(distribution.metadata["Name"] or "")
        record = distribution.read_text("RECORD")
        if record is None:
            continue
        base = os.path.normpath(distribution.locate_file(""))
        first = name not in compared
        compared.add(name)
        for row in csv.reader(io.StringIO(record)):
            if not row or not row[0]:
                continue
            path = os.path.normpath(os.path.join(base, row[0]))
            listed.add(path)
            relative = Path(os.path.relpath(path, base)).parts
            if relative and relative[0] != os.pardir:
                owned.add(os.path.join(base, relative[0]))
            if not first or len(row) < 2 or not row[1]:
                continue
            algorithm, _, expected = row[1].partition("=")
            if algorithm not in hashlib.algorithms_guaranteed:
                changed.append(f"The RECORD of {name} lists {path} with a {algorithm} hash, "
                               f"which the comparison cannot compute.")
                continue
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.file_digest(handle, algorithm).digest()
            except OSError:
                changed.append(f"{path} is missing, and the RECORD of {name} lists it.")
                continue
            if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != expected:
                changed.append(f"{path} has another {algorithm} hash than the RECORD of {name} "
                               f"lists.")
    for directory in directories:
        for entry in sorted(os.listdir(directory)):
            top = os.path.normpath(os.path.join(directory, entry))
            if entry == "__pycache__" or (entry in UNRECORDED and os.path.isfile(top)):
                continue
            if top not in owned:
                unrecorded.append(top)
                continue
            for folder, subfolders, files in os.walk(top):
                subfolders[:] = sorted(sub for sub in subfolders if sub != "__pycache__")
                unrecorded += [path for path in (os.path.join(folder, file)
                                                 for file in sorted(files))
                               if path not in listed]
    lines = [f"{path} is in site-packages, and no RECORD of a distribution lists it."
             for path in sorted(unrecorded)]
    remedy = [unrecorded_remedy(unrecorded, prefix)] if unrecorded else []
    return (counted(lines, "files and directories in site-packages that no RECORD lists")
            + remedy + counted(sorted(changed), "files that differ from their RECORD"))


def environment_differences(locked: dict, installed: dict, lock: str) -> list:
    """One line for each distribution in which `installed` differs from `locked`, the dependency
    lock that `lock` names: installed and not listed, listed and missing, or installed at another
    version. The distributions of `UNLOCKED` are left out."""
    from packaging.version import InvalidVersion, Version

    def same(first: str, second: str) -> bool:
        try:
            return Version(first) == Version(second)
        except InvalidVersion:
            return first == second

    lines = [f"{lock} does not list {name} {installed[name]}, which is installed."
             for name in sorted(installed.keys() - locked.keys() - UNLOCKED)]
    lines += [f"{lock} lists {name} {locked[name]}, which is not installed."
              for name in sorted(locked.keys() - installed.keys() - UNLOCKED)]
    lines += [f"{lock} lists {name} {locked[name]}, and {installed[name]} is installed."
              for name in sorted(locked.keys() & installed.keys() - UNLOCKED)
              if not same(locked[name], installed[name])]
    return lines


def lock_differences() -> list:
    """The differences between the running environment and `LOCK`, or the one line that says why
    the dependency lock cannot be compared."""
    lock = LOCK.relative_to(REPO_ROOT).as_posix()
    try:
        text = LOCK.read_text(encoding="utf-8")
    except OSError as error:
        return [f"Could not read {lock} ({error.strerror or error})."]
    try:
        locked = locked_versions(text)
    except ModuleNotFoundError as error:
        return [f"{error.name} is not installed, and the comparison with {lock} needs it to "
                f"read the environment markers of the dependency lock."]
    return (environment_differences(locked, installed_versions(), lock)
            + direct_install_differences(direct_installs(), lock))


def environment_problems() -> list:
    """Every difference between the running environment and the one in which CI runs the suites:
    the release of Python, the `python3` on PATH, the distributions of `LOCK`, the editable
    install of this checkout and the files of site-packages."""
    return (interpreter_differences(platform.python_version(), pinned_python())
            + path_interpreter_differences() + lock_differences()
            + site_packages_differences(site_directories(), sys.prefix))


def child_environment(step: str | None = None, venv: Path | None = None) -> dict:
    """The environment of the suite of the workflow step `step`, or of a check when `step` is
    None. It has PYTHONPATH only for a step whose `env:` sets it, with this checkout, the checkout
    of CI, as its only entry. CI runs every other suite and check without PYTHONPATH, and an entry
    that the shell adds would let a suite import modules that CI does not have. agentcontext
    comes from the editable install of this checkout, which `environment_problems` requires.
    With `venv`, that virtual environment replaces the one that runs the verification, as
    `deactivate` and then its `activate` would: the bin directory of the running environment
    leaves PATH, because the .venv of CI before the install has none of its console scripts,
    such as coverage."""
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    if step is not None and "PYTHONPATH" in step_variables(step):
        env["PYTHONPATH"] = str(REPO_ROOT)
    if venv is not None:
        own = os.path.realpath(Path(sys.prefix) / "bin")
        rest = [entry for entry in env["PATH"].split(os.pathsep)
                if not (entry and os.path.realpath(entry) == own)] if env.get("PATH") else []
        env.update(VIRTUAL_ENV=str(venv), PATH=os.pathsep.join([str(venv / "bin"), *rest]))
    return env


def without_local_paths(text: str) -> str:
    """`text` with each path in this checkout relative to the checkout, the checkout itself as
    `.`, and each other path in the home directory under `~`. A report that docs/validation/
    keeps then names no directory of the machine that ran the verification. The checkout and the
    home directory are replaced only where a path begins with them, also after a short option,
    such as -o/code/data in a command that a message quotes, and in a file URL, which becomes the
    path. They stay inside a longer path, such as /code in /opt/code/bin, and inside a URL of
    another scheme, such as https://example.com/code/x."""
    try:
        home = Path.home()
    except RuntimeError:
        home = None
    # Each place with what replaces a path in it and what replaces the place itself. The checkout
    # comes first, because it can lie in the home directory, and so does its resolved form, which
    # differs where the checkout is reached through a link, such as /tmp on macOS.
    places = [(root, "", ".") for root in sorted({str(REPO_ROOT), os.path.realpath(REPO_ROOT)},
                                                 key=len, reverse=True)]
    # A home directory at the root of the filesystem, as in some containers, is the start of
    # every absolute path and is left alone.
    if home is not None and home != Path(home.anchor):
        places.append((str(home), "~/", "~"))
    # A place begins a path after `file://`, where no character of a longer path or a word comes
    # before it, or after a hyphen and a letter that begin a word, as the short options -o of 7zz
    # and -I of a compiler take a path without a space. It ends at a separator that another name
    # follows, or where a word, a bracket or a sentence ends. A URL of another scheme comes after
    # the places and is kept as it is, so that no place is searched inside it.
    stops = r"""\s'"),:;\]!?"""
    end = rf"""(?=[{stops}]|\.(?:\s|$)|$)"""
    before = r"""(?:(?<![\w.+-])file://|(?<![\w.~/%+@-])|(?<=(?<![\w-])-[A-Za-z]))"""
    alternatives = []
    for index, (place, _, _) in enumerate(places):
        alternatives.append(rf"""{before}(?P<place{index}>{re.escape(place)})"""
                            rf"""(?:(?P<inside{index}>/)(?=[^{stops}])|/?{end})""")
    alternatives.append(r"""[A-Za-z][A-Za-z0-9+.-]*://[^\s'"<>]*""")

    def replaced(match: re.Match) -> str:
        """The match without the place that it begins with and without `file://`, or the URL as
        it is."""
        for index, (_, inside, itself) in enumerate(places):
            if match.group(f"place{index}") is not None:
                return inside if match.group(f"inside{index}") else itself
        return match.group(0)

    return re.sub("|".join(alternatives), replaced, text)


def run(label: str, argv: list, cwd: Path, results: list, failures: list, env: dict) -> None:
    """Run `argv` in `cwd` with the environment `env`, and add its result to `results` and, when
    it fails, its output to `failures`, both without the paths of the machine
    (`without_local_paths`). The paths go before the output is shortened, because a path that the
    cut divides no longer begins with the checkout or the home directory."""
    started = time.time()
    p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, env=env)
    took = time.time() - started
    output = without_local_paths(p.stdout + p.stderr)
    summary = " | ".join(l.strip() for l in output.strip().splitlines()[-3:])
    results.append((label, p.returncode, took, summary[:300]))
    if p.returncode != 0:
        failures.append((label, output[-6000:]))


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__, end="")
        return 0
    parser = cli.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", help="The file to write the verification report to")
    parser.add_argument("--analysis-output-dir", metavar="DIR",
                        help="Run the analysis twice into DIR and compare the two runs, "
                             "instead of comparing with the tracked outputs")
    args = parser.parse_args(argv)
    if os.environ.get(DATA_ROOT_VARIABLE):
        logs.error("verify", f"{DATA_ROOT_VARIABLE} is set to {os.environ[DATA_ROOT_VARIABLE]}, "
                             f"and every suite and check would read that data root instead of "
                             f"the data/ of this checkout, which CI checks. Unset the variable, "
                             f"then run the verification again.")
        return 1
    # A suite that passes in an environment with a distribution that no dependency lock lists, or
    # under another release of Python, can fail in CI and in the image, which run the pinned
    # release with the dependency locks only.
    differences = environment_problems()
    if differences:
        for line in differences:
            logs.error("verify", line)
        logs.error("verify", f"Install the environment from {LOCK.relative_to(REPO_ROOT)} as "
                             f"docs/development.md describes, then run the verification again.")
        return 1
    results, failures = [], []
    # CI runs a suite before its install in the .venv that mise created, which holds no
    # distribution, and the children of the suite start that interpreter. The suite runs here
    # with the interpreter of such an environment, so that its children import what they import
    # in CI rather than the distributions of the development lock.
    with tempfile.TemporaryDirectory(prefix="agentcontext-verify-") as scratch:
        empty = None
        if any(runs_before_the_install(step) for step, _ in SUITES):
            try:
                empty = empty_environment(Path(scratch) / "venv")
            except (OSError, subprocess.CalledProcessError) as error:
                detail = (getattr(error, "stderr", None) or str(error)).strip()
                logs.error("verify", f"Could not create the empty virtual environment in which "
                                     f"the suites that CI runs before its install run "
                                     f"({detail}).")
                return 1
        for step, cwd in SUITES:
            names = modules(step)
            venv = empty if runs_before_the_install(step) else None
            interpreter = str(venv / "bin" / "python3") if venv else None
            run(f"{step[4:]} with {len(names)} module{'' if len(names) == 1 else 's'}",
                unittest_command(step, interpreter), cwd, results, failures,
                child_environment(step, venv))
    for label, arguments, cwd in CHECKS:
        if label == "check_reproduction.py" and args.analysis_output_dir:
            arguments = [*arguments, "--output-dir", str(Path(args.analysis_output_dir).resolve())]
        run(label, [sys.executable, *arguments], cwd, results, failures, child_environment())
    # The return code, not the output alone: When git refuses, it prints nothing on stdout, and
    # an empty `.stdout` alone would report a tree that git never inspected as clean. A checkout
    # that git refuses to inspect, such as a checkout with dubious ownership under a bind mount
    # or an unpacked archive with no `.git`, reaches this branch.
    proc = subprocess.run(["git", "status", "--short"], cwd=str(REPO_ROOT),
                          capture_output=True, text=True)
    status = proc.stdout
    unclean = proc.returncode != 0 or bool(status.strip())
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(REPO_ROOT), capture_output=True, text=True).stdout.strip()
    out = [f"VERIFICATION at {head}"]
    for label, code, took, summary in results:
        out.append(f"{'PASS' if code == 0 else 'FAIL'} exit={code} {took:6.1f}s  {label}"
                   + (f" ({summary})" if summary else ""))
    if proc.returncode != 0:
        out.append(f"Could not check whether the working tree is clean. git status exited "
                   f"{proc.returncode} ({proc.stderr.strip() or 'no message'}).")
    elif unclean:
        out.append("The working tree is not clean after the checks. The output of git status "
                   "--short follows.\n" + status.rstrip("\n"))
    else:
        out.append("The working tree is clean after the checks.")
    for label, text in failures:
        out.append(f"\n===== {label} =====\n{text}")
    # Every line, including the message of a git status that failed, which names the checkout
    # that git refused.
    text = without_local_paths("\n".join(out))
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    logs.info("verify", text)
    if proc.returncode != 0:
        return 1
    return 0 if not failures and not unclean else 2


if __name__ == "__main__":
    raise SystemExit(main())
