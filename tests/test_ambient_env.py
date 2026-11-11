"""
Tests for tests._ambient_env: the fixture that removes the credential variables of the
environment for a suite.

Strategy:
  - The tests run against the real os.environ, because the fixture changes that mapping and the
    steps under test read the credentials from it.
  - The suite leaves the environment of the process unchanged, because each test restores the
    variables that it set.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_ambient_env -v
"""

import ast
import importlib
import os
import re
import sys
import unittest
from pathlib import Path
from agentcontext import REPO_ROOT

_mod = importlib.import_module("tests._ambient_env")


class TestTakeAndRestore(unittest.TestCase):

    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in _mod.CREDENTIAL_VARS}
        self.addCleanup(self._put_back)

    def _put_back(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_a_set_variable_is_taken_and_given_back(self):
        os.environ["GITHUB_TOKEN"] = "t"
        store = _mod.take({})
        self.assertNotIn("GITHUB_TOKEN", os.environ)
        self.assertEqual(store, {"GITHUB_TOKEN": "t"})
        _mod.restore(store)
        self.assertEqual(os.environ["GITHUB_TOKEN"], "t")
        self.assertEqual(store, {})

    def test_an_unset_variable_stays_unset(self):
        os.environ.pop("GITHUB_TOKEN", None)
        store = _mod.take({})
        self.assertEqual(store, {})
        _mod.restore(store)
        self.assertNotIn("GITHUB_TOKEN", os.environ)

    def test_restore_does_not_invent_a_value(self):
        # take() records nothing for a variable that was not there, and restore() must then
        # leave the variable unset. An empty value would be worse than an absent variable. The
        # steps read "" as absent only because they happen to write `or None`.
        os.environ.pop("GITHUB_TOKEN", None)
        _mod.restore(_mod.take({}))
        self.assertIsNone(os.environ.get("GITHUB_TOKEN"))

    def test_every_named_variable_is_one_the_package_reads(self):
        read = credential_reads()
        for name in _mod.CREDENTIAL_VARS:
            with self.subTest(variable=name):
                self.assertIn(name, read)

    def test_every_credential_the_package_reads_is_named(self):
        """The other direction: Every credential variable that a package module reads is in the
        list, so that a machine that exports `GITHUB_TOKENS` beside `GITHUB_TOKEN` does not add
        its GitHub access tokens to the runs of the suites."""
        read = credential_reads()
        self.assertIn("GITHUB_TOKEN", read, "the scan finds no read at all")
        for name, where in sorted(read.items()):
            with self.subTest(variable=name):
                self.assertIn(name, _mod.CREDENTIAL_VARS, f"read in {', '.join(sorted(where))}")

    def test_the_scan_finds_each_way_of_reading_a_variable(self):
        source = ("import os\n"
                  "a = os.environ.get('A_TOKEN')\n"
                  "b = os.environ['B_SECRET']\n"
                  "c = os.getenv('C_PASSWORD')\n"
                  "def f(env):\n"
                  "    for name in ('D_TOKENS', 'NOT_A_CREDENTIAL'):\n"
                  "        env.get(name)\n"
                  "os.environ['WRITTEN_TOKEN'] = 'x'\n"
                  "TABLE = {'one': ('E_TOKEN',), 'two': ('F_API_KEY',)}\n"
                  "def g(service, environ):\n"
                  "    for name in TABLE[service]:\n"
                  "        environ.get(name)\n")
        self.assertEqual(sorted(environment_reads(ast.parse(source))),
                         ["A_TOKEN", "B_SECRET", "C_PASSWORD", "D_TOKENS", "E_TOKEN", "F_API_KEY",
                          "NOT_A_CREDENTIAL"])


# A variable name that marks a secret.
CREDENTIAL = re.compile(r"TOKENS?$|SECRET|PASSWORD|API_KEY|CREDENTIAL")


def _is_environment(node) -> bool:
    """`os.environ`, or a name under which an environment is passed."""
    if isinstance(node, ast.Attribute) and node.attr == "environ":
        return True
    return isinstance(node, ast.Name) and node.id in ("env", "environ", "environment")


def _strings(node) -> set:
    """The strings of a tuple or list literal."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return set()
    return {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}


def environment_reads(tree) -> set:
    """Every variable name that the module reads from an environment through
    `environ.get(name)`, `environ[name]` or `os.getenv(name)`. `name` is a string, or the
    variable of a loop over strings or over an entry of a module-level table of strings, such as
    the loop of `agentcontext.credentials` over `ENVIRONMENT[service]`."""
    tables = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            tables[node.targets[0].id] = set().union(*map(_strings, node.value.values))
    loops = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            if isinstance(node.iter, ast.Subscript) and isinstance(node.iter.value, ast.Name):
                strings = tables.get(node.iter.value.id, set())
            else:
                strings = _strings(node.iter)
            loops.setdefault(node.target.id, set()).update(strings)

    def names(arg) -> set:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return {arg.value}
        if isinstance(arg, ast.Name):
            return loops.get(arg.id, set())
        return set()

    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.args:
            if node.func.attr == "get" and _is_environment(node.func.value):
                found |= names(node.args[0])
            elif node.func.attr == "getenv":
                found |= names(node.args[0])
        elif (isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load)
              and _is_environment(node.value)):
            found |= names(node.slice)
    return found


def credential_reads() -> dict:
    """{variable: the package modules that read it} for every variable name that marks a
    credential."""
    read = {}
    for path in sorted((REPO_ROOT / "agentcontext").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in environment_reads(tree):
            if CREDENTIAL.search(name):
                read.setdefault(name, set()).add(str(path.relative_to(REPO_ROOT)))
    return read


if __name__ == "__main__":
    unittest.main()
