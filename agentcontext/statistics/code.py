"""Computations over the code of the repository that the `constant` source cannot express, and
over the standard library of the Python that runs the pipeline.

`literal` reads, with a regular expression, a number that a file writes inside an expression.
It serves a file in which moving the number into a top-level constant, which the `constant`
source reads, would itself cost something. A change of `agentcontext/commits.py` or
`agentcontext/heuristics.py` changes the digest of the commit classification. A change of the
collector's code needs a version of the collector, and a change of a text that a model reads
labels the sample again.

`files` counts the files that match a glob pattern and contain a text, such as the modules of
an earlier commit that contain the `§` separator. `calls` counts the calls in a top-level tuple or
list by their positional arguments, such as the files of `release.RELEASE_FILES` of one kind.
`whole_repository_suites` counts the suites of `tests/` that check the repository as a whole,
and `interpreter_default` reads a default of the standard library that no page of the
documentation states, such as the field limit of the `csv` module.
"""

import ast
import re
import subprocess
import sys
from pathlib import PurePosixPath

from agentcontext.statistics import Files, Unavailable, checkout

# The subpackages of the package with a suite per module named `test_<subpackage>_<module>`.
SUBPACKAGES = ("steps", "checks", "tools", "git")


@checkout
def literal(files: Files, path: str, pattern: str) -> int | float:
    """The number that the first group of a regular expression matches in a file, which has
    to match exactly once."""
    matches = list(re.finditer(pattern, files.text(path)))
    if len(matches) != 1:
        raise Unavailable(f"The pattern {pattern!r} matches {path} {len(matches)} times, not once")
    text = matches[0].group(1).replace("_", "").replace(",", "")
    try:
        return float(text) if any(sign in text for sign in ".eE") else int(text)
    except ValueError as error:
        raise Unavailable(f"The pattern {pattern!r} matches {text!r} in {path}, which is not a "
                          f"number") from error


@checkout
def files(files: Files, pattern: str, contains: str, exclude: str | None = None) -> int:
    """The files of the checkout that match a glob pattern, and not the glob pattern `exclude`,
    with text that the regular expression `contains` matches, such as the modules of a
    directory that write out a character."""
    found = files.glob(pattern)
    left_out = set(files.glob(exclude)) if exclude else set()
    text = re.compile(contains)
    return sum(1 for relative in found if relative not in left_out
               and text.search(files.bytes(relative).decode("utf-8", errors="replace")))


def _assigned(files: Files, path: str, name: str) -> ast.AST:
    """The value of the top-level assignment of `name` in a Python file."""
    try:
        tree = ast.parse(files.text(path))
    except SyntaxError as error:
        raise Unavailable(f"Could not read {path} ({error})") from error
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return node.value
        if (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and node.target.id == name and node.value is not None):
            return node.value
    raise Unavailable(f"{path} has no top-level assignment of {name}")


def _names(files: Files, among: dict) -> set:
    """The strings that a condition of `calls` admits: the names of the files that the glob
    pattern `glob` matches, or the item `item` of every value of the top-level dict `name` of
    the Python file `path`."""
    if "glob" in among:
        return {PurePosixPath(relative).name for relative in files.glob(among["glob"])}
    try:
        values = ast.literal_eval(_assigned(files, among["path"], among["name"]))
    except ValueError as error:
        raise Unavailable(f"The dict {among['name']} of {among['path']} is not a literal "
                          f"({error})") from error
    return {value[among["item"]] for value in values.values()}


@checkout
def calls(files: Files, path: str, name: str, where: dict | None = None,
          among: dict | None = None) -> int:
    """The calls among the items of the top-level tuple or list `name` of a Python file whose
    positional arguments, by index, equal the strings of `where` and are among the names that
    each condition of `among` admits (`_names`). An argument that is no string literal meets
    no condition."""
    node = _assigned(files, path, name)
    if not isinstance(node, (ast.Tuple, ast.List)):
        raise Unavailable(f"{name} of {path} is not a tuple or a list")
    admitted = {int(index): _names(files, condition) for index, condition in
                (among or {}).items()}

    def argument(call: ast.Call, index: int) -> object:
        if index < len(call.args) and isinstance(call.args[index], ast.Constant):
            return call.args[index].value
        return None

    return sum(1 for item in node.elts if isinstance(item, ast.Call)
               and all(argument(item, int(index)) == value
                       for index, value in (where or {}).items())
               and all(argument(item, index) in names for index, names in admitted.items()))


@checkout
def whole_repository_suites(files: Files, tests: str = "tests",
                            package: str = "agentcontext") -> int:
    """The suites `test_<name>.py` of `tests` that test no module, which `docs/architecture.md`
    lists as the suites over the repository as a whole."""
    # A suite tests a module when `package` has a module or a subpackage of its name, `tests`
    # has a shared fixture `_<name>.py`, or a name that starts with a subpackage names a module
    # of it.
    count = 0
    for suite in files.glob(f"{tests}/test_*.py"):
        name = suite.rpartition("/")[2][len("test_"):-len(".py")]
        modules = [f"{package}/{name}.py", f"{package}/{name}/__init__.py",
                   f"{tests}/_{name}.py"]
        subpackage, _, module = name.partition("_")
        if subpackage in SUBPACKAGES and module:
            modules.append(f"{package}/{subpackage}/{module}.py")
        if not any(files.exists(path) for path in modules):
            count += 1
    return count


@checkout
def interpreter_default(files: Files, module: str, attribute: str,
                        call: bool = False) -> int | float:
    """The value of an attribute of a module of the standard library in a fresh interpreter, or
    with `call` the value that calling it without arguments returns."""
    # A fresh interpreter in isolated mode, because a module of this package can change such a
    # default for the process that imports it, as `Files.rows` raises the field limit.
    if not module.isidentifier() or not all(part.isidentifier()
                                            for part in attribute.split(".")):
        raise Unavailable(f"{module}.{attribute} is not a module and an attribute")
    program = (f"import operator, {module}\n"
               f"value = operator.attrgetter({attribute!r})({module})\n"
               f"print(repr(value() if {call!r} else value))\n")
    proc = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True)
    if proc.returncode != 0:
        raise Unavailable(f"The interpreter could not read {module}.{attribute} "
                          f"({proc.stderr.strip().splitlines()[-1:]})")
    return ast.literal_eval(proc.stdout.strip())
