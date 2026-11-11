"""
Tests for three failures that a module's own suite cannot detect: a name that nothing binds, a
`sys.path` entry that points at no directory, and a string escape that Python will refuse.

Background:
  - A variable renamed everywhere except at one use raises NameError only in the branch that
    contains that use, and a suite that runs another branch does not reach that use. Loading
    the module does not detect the name either, because the function reads it only when the
    function runs. The check is therefore static. `symtable` resolves each name to the scope
    that owns it, and the check reports a name that a function reads and that no module-level
    statement binds.

  - A script that puts a directory on `sys.path` and imports a module from it raises
    `ModuleNotFoundError` at import once the module moves and the line does not. For a script
    without a suite, such as `_website/build_site.py`, no test reports the error. Resolving
    the directory is not enough, because the directory can still exist when the module has
    moved. The check therefore resolves both over every tracked script: the inserted
    directory, and the module that each top-level import then loads from that directory.

  - `\\s` in a string that is not raw compiles with a SyntaxWarning in Python 3.14, and a later
    Python refuses it. No test reports the warning, and the check therefore compiles every
    tracked script with that warning as an error.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_undefined_names -v
"""

import ast
import builtins
import warnings
import importlib.util
import subprocess
import symtable
import sys
import tempfile
import unittest
from pathlib import Path
from agentcontext import REPO_ROOT

PACKAGE = REPO_ROOT / "agentcontext"
TESTS = REPO_ROOT / "tests"

# The names that every module binds without a statement. Python 3.14 reads
# `__conditional_annotations__` at module level when an annotation is inside a conditional block.
MODULE_NAMES = frozenset({"__file__", "__name__", "__doc__", "__spec__", "__loader__",
                          "__package__", "__builtins__", "__conditional_annotations__"})


def tracked_scripts() -> list:
    """Every tracked Python file of the repository, sorted."""
    listed = subprocess.run(["git", "ls-files", "--", "*.py"], cwd=str(REPO_ROOT),
                            capture_output=True, text=True)
    if listed.returncode != 0:
        return []
    return sorted(REPO_ROOT / name for name in listed.stdout.split())


def modules() -> list:
    """Every tracked Python file of the repository, sorted, including the sampling,
    classification and analysis stages and the website. Outside a checkout, only the package
    and the tests."""
    tracked = tracked_scripts()
    if tracked:
        return tracked
    return sorted(PACKAGE.rglob("*.py")) + sorted(TESTS.glob("*.py"))


def module_bindings(top) -> set:
    """Every name that the module binds: what a module-level statement assigns, imports or
    defines, and what a function declares `global` and assigns.

    A name that the module only reads is an identifier of the module's scope too. Counting
    every such identifier would let a name read at module level hide the same missing name
    everywhere. `DEFAULT_INPUT` read in a script's main() and again in its `__main__` block
    would then pass.
    """
    bound = set(MODULE_NAMES)

    def collect(table):
        for symbol in table.get_symbols():
            if table is top:
                if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace():
                    bound.add(symbol.get_name())
            elif symbol.is_declared_global() and symbol.is_assigned():
                bound.add(symbol.get_name())
        for child in table.get_children():
            collect(child)

    collect(top)
    return bound


def undefined_globals(script: Path) -> list:
    """The names that the module reads as a global and never binds, as (scope, name).

    Every binding form counts, imports and for-targets included. A name is reported only when
    nothing in the module defines it and it is neither a builtin nor one of the names that the
    interpreter gives every module. A name read at module level is reported under the module's
    own name, and a name read in a function under the function's name. Annotations have been a
    scope of their own since Python 3.14, and a name that an annotation reads and that nothing
    imports is therefore reported too.
    """
    source = script.read_text(encoding="utf-8")
    top = symtable.symtable(source, str(script), "exec")
    bound = module_bindings(top)
    found = []

    def visit(table, trail):
        for symbol in table.get_symbols():
            if symbol.is_global() and not symbol.is_assigned():
                name = symbol.get_name()
                if name not in bound and not hasattr(builtins, name):
                    found.append((".".join(trail), name))
        for child in table.get_children():
            visit(child, trail + [child.get_name()])

    visit(top, [script.stem])
    return found


def _sys_path_entry(node):
    """The directory argument of a `sys.path.insert(i, d)` or `sys.path.append(d)` call, or
    None when the node is neither. The function reads both forms, so that the checks also cover
    an `append` of a directory that does not exist and the imports that it was added for.
    """
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return None
    target = node.func.value
    if not (isinstance(target, ast.Attribute) and target.attr == "path"
            and isinstance(target.value, ast.Name) and target.value.id == "sys"):
        return None
    if node.func.attr == "insert" and len(node.args) == 2:
        return node.args[1]
    if node.func.attr == "append" and len(node.args) == 1:
        return node.args[0]
    return None


def unresolved_path_inserts(script: Path) -> list:
    """Every `sys.path.insert` or `append` in the file with a directory that does not exist, as
    (line, path).

    The function reads only the calls that this repository writes, which build the path from
    `__file__` through `Path(...).resolve().parent`, `.parents[N]` and `/` with string literals.
    It leaves any other call alone rather than guessing its directory.
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    names = _path_names(tree, script)
    found = []
    for node in ast.walk(tree):
        entry = _sys_path_entry(node)
        if entry is None:
            continue
        resolved = _literal_dir(entry, script, names)
        if resolved is not None and not resolved.is_dir():
            found.append((node.lineno, str(resolved)))
    return found


def _path_names(tree, script: Path) -> dict:
    """Module-level names bound to a literal path expression, as {name: directory}.

    Scripts write `SCRIPT_DIR = Path(__file__).resolve().parent` and then insert
    `SCRIPT_DIR.parent / "x"`. Without these names, the check could not resolve such an
    insertion and would skip it, and a broken import after it would go unreported.
    """
    names = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = _literal_dir(node.value, script, names)
        if value is not None:
            names[target.id] = value
    return names


def _literal_dir(node, script: Path, names: dict = None):
    """The directory that a `str(Path(__file__).resolve().parent / "x")` expression names, or
    None."""
    names = {} if names is None else names
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "str":
        return _literal_dir(node.args[0], script, names) if node.args else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        base = _literal_dir(node.left, script, names)
        if base is None or not isinstance(node.right, ast.Constant) \
                or not isinstance(node.right.value, str):
            return None
        return base / node.right.value
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _literal_dir(node.value, script, names)
        return None if base is None else base.parent
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) \
            and node.value.attr == "parents":
        base = _literal_dir(node.value.value, script, names)
        index = node.slice
        if base is None or not isinstance(index, ast.Constant) \
                or not isinstance(index.value, int):
            return None
        return base.parents[index.value]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr == "resolve":
        return _literal_dir(node.func.value, script, names)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path" \
            and len(node.args) == 1 and isinstance(node.args[0], ast.Name) \
            and node.args[0].id == "__file__":
        return script.resolve()
    return None


def unresolved_imports(script: Path, tracked=None) -> list:
    """Every top-level `from X import ...` naming a module of this repository that the
    script cannot reach, as (line, module).

    An existing directory is not enough: A module can move out of a directory that stays, and
    the line that inserted the directory stays too. A module is therefore reported when a file
    of that name is tracked somewhere in this repository and is in neither the script's own
    directory nor any directory that the script puts on `sys.path`. The check leaves a
    third-party name alone, because it cannot know what a machine has installed. The imports of
    the MCP experiment are documented as needing a package that the suites do not install.
    """
    try:
        tree = ast.parse(script.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    if tracked is None:
        tracked = tracked_scripts()
    inserted, all_literal = _inserted_dirs(script, tree)
    if not all_literal:
        # A path insertion that this check cannot read, such as `ROOT / stage_dir` inside a
        # loop in `run_pipeline.py`. The modules that the script can reach are then unknown,
        # and a guess would report an import that resolves.
        return []
    ours = {path.stem for path in tracked}
    reachable = [script.parent, *(d for d in inserted if d.is_dir())]
    found = []
    for node in tree.body:
        # Both forms. `import x` after a path insertion fails exactly as `from x import` does,
        # and scripts of the MCP experiment use it.
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            names = [node.module]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        for name in names:
            head = name.split(".")[0]
            # A module of the standard library resolves before any file of the repository with
            # its name, such as `agentcontext/tools/statistics.py` for `statistics`.
            if head not in ours or head in sys.stdlib_module_names:
                continue  # not a module of this repository
            if any((d / f"{head}.py").is_file() or (d / head / "__init__.py").is_file()
                   for d in reachable):
                continue
            found.append((node.lineno, name))
    return found


def _inserted_dirs(script: Path, tree) -> tuple:
    """The directories that the script puts on `sys.path`, and whether each directory was a
    literal expression."""
    names = _path_names(tree, script)
    dirs, all_literal = [], True
    for node in ast.walk(tree):
        entry = _sys_path_entry(node)
        if entry is None:
            continue
        resolved = _literal_dir(entry, script, names)
        if resolved is None:
            all_literal = False
        else:
            dirs.append(resolved)
    return dirs, all_literal


def invalid_escapes(source: str, name: str) -> list:
    """The SyntaxWarnings that compiling `source` raises. Python 3.14 raises a SyntaxWarning for
    an invalid escape, such as `\\s` in a string that is not raw."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SyntaxWarning)
        compile(source, name, "exec")
    return [f"{name}:{w.lineno}: {w.message}" for w in caught
            if issubclass(w.category, SyntaxWarning)]


class TestNoScriptContainsAnInvalidEscape(unittest.TestCase):

    def test_every_tracked_script_compiles_without_a_syntax_warning(self):
        found = []
        for script in tracked_scripts():
            try:
                source = script.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            found += invalid_escapes(source, str(script.relative_to(REPO_ROOT)))
        self.assertEqual(found, [])

    def test_the_check_catches_an_escape_in_a_docstring(self):
        source = 'def f():\n    """`\\s` matches a line break."""\n'
        self.assertEqual(len(invalid_escapes(source, "x.py")), 1)

    def test_a_raw_string_and_a_doubled_backslash_are_accepted(self):
        source = 'A = r"\\s"\nB = "\\\\s"\n'
        self.assertEqual(invalid_escapes(source, "x.py"), [])


class TestEverySysPathEntryIsThere(unittest.TestCase):
    """A `sys.path` entry that resolves nowhere, or that does not contain the module that the
    script then imports from it, raises an ImportError at the first run and goes unnoticed in a
    script that no suite runs."""

    def test_no_tracked_script_puts_a_missing_directory_on_the_path(self):
        scripts = tracked_scripts()
        if not scripts:
            self.skipTest("not inside a git checkout")
        for script in scripts:
            with self.subTest(script=str(script.relative_to(REPO_ROOT))):
                self.assertEqual(unresolved_path_inserts(script), [])

    def test_every_import_a_script_adds_a_path_for_resolves(self):
        """An existing directory is not enough: The module that the line was added for can have
        moved to another directory while the inserted directory stays."""
        scripts = tracked_scripts()
        if not scripts:
            self.skipTest("not inside a git checkout")
        for script in scripts:
            with self.subTest(script=str(script.relative_to(REPO_ROOT))):
                self.assertEqual(unresolved_imports(script, scripts), [])

    def test_the_check_catches_a_directory_that_is_not_there(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "broken.py"
            broken.write_text('import sys\nfrom pathlib import Path\n'
                              'sys.path.insert(0, str(Path(__file__).resolve().parent / "nope"))\n',
                              encoding="utf-8")
            found = unresolved_path_inserts(broken)
            self.assertEqual(len(found), 1, found)
            self.assertTrue(found[0][1].endswith("nope"))

    def test_the_check_reads_an_append_as_well_as_an_insert(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "elsewhere").mkdir()
            (root / "elsewhere" / "_shared.py").write_text("X = 1\n", encoding="utf-8")
            broken = root / "broken.py"
            broken.write_text('import sys\nfrom pathlib import Path\n'
                              'sys.path.append(str(Path(__file__).resolve().parent / "nope"))\n'
                              'from _shared import X\n', encoding="utf-8")
            found = unresolved_path_inserts(broken)
            self.assertEqual(len(found), 1, found)
            self.assertTrue(found[0][1].endswith("nope"))
            imports = unresolved_imports(broken, [broken, root / "elsewhere" / "_shared.py"])
            self.assertEqual([name for _line, name in imports], ["_shared"])

    def test_a_module_of_the_standard_library_is_not_one_of_the_repository(self):
        """A tracked file can share the name of a module of the standard library, which an
        import resolves first."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "tools").mkdir()
            (root / "tools" / "statistics.py").write_text("X = 1\n", encoding="utf-8")
            script = root / "script.py"
            script.write_text("from statistics import NormalDist\n", encoding="utf-8")
            self.assertEqual(unresolved_imports(script, [script, root / "tools" / "statistics.py"]),
                             [])

    def test_a_directory_that_is_there_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "here").mkdir()
            ok = Path(d) / "ok.py"
            ok.write_text('import sys\nfrom pathlib import Path\n'
                          'sys.path.insert(0, str(Path(__file__).resolve().parent / "here"))\n',
                          encoding="utf-8")
            self.assertEqual(unresolved_path_inserts(ok), [])

    def test_the_check_catches_a_module_the_inserted_directory_does_not_hold(self):
        """The inserted directory exists here, and the module that the line was added for is in
        another directory."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "elsewhere").mkdir()
            (root / "elsewhere" / "_shared.py").write_text("X = 1\n", encoding="utf-8")
            (root / "here").mkdir()
            script = root / "build.py"
            script.write_text('import sys\nfrom pathlib import Path\n'
                              'sys.path.insert(0, str(Path(__file__).resolve().parent / "here"))\n'
                              'from _shared import X\n'
                              'import _shared\n', encoding="utf-8")
            tracked = [script, root / "elsewhere" / "_shared.py"]
            found = unresolved_imports(script, tracked)
            self.assertEqual([name for _line, name in found], ["_shared", "_shared"],
                             "neither import form was reported")

    def test_a_module_in_the_inserted_directory_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "here").mkdir()
            (root / "here" / "_shared.py").write_text("X = 1\n", encoding="utf-8")
            script = root / "build.py"
            script.write_text('import sys\nfrom pathlib import Path\n'
                              'sys.path.insert(0, str(Path(__file__).resolve().parent / "here"))\n'
                              'from _shared import X\n', encoding="utf-8")
            self.assertEqual(unresolved_imports(script, [script, root / "here" / "_shared.py"]), [])

    def test_the_discovery_reaches_outside_the_package(self):
        names = {str(s.relative_to(REPO_ROOT)) for s in tracked_scripts()}
        if not names:
            self.skipTest("not inside a git checkout")
        for name in ("_website/build_site.py", "run_pipeline.py",
                     "4_data-analysis/_shared.py", "deploy/entrypoint.py"):
            self.assertIn(name, names)


class TestNoModuleReadsAnUndefinedName(unittest.TestCase):

    def test_the_package_and_the_tests_have_no_undefined_names(self):
        for module in modules():
            with self.subTest(module=str(module.relative_to(REPO_ROOT))):
                self.assertEqual(undefined_globals(module), [])

    def test_the_check_catches_a_name_nothing_binds(self):
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "broken.py"
            broken.write_text("def f():\n    return missing_name\n", encoding="utf-8")
            self.assertEqual(undefined_globals(broken), [("broken.f", "missing_name")])

    def test_a_name_bound_anywhere_at_module_level_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            ok = Path(d) / "ok.py"
            ok.write_text("import os\nVALUE = 1\n"
                          "def f():\n    return os.sep, VALUE, len\n", encoding="utf-8")
            self.assertEqual(undefined_globals(ok), [])

    def test_reading_a_name_at_module_level_does_not_bind_it(self):
        """A name read in main() and again in the `__main__` block is reported in both
        places, rather than counted as bound because it is an identifier of the module's
        scope."""
        with tempfile.TemporaryDirectory() as d:
            broken = Path(d) / "broken.py"
            broken.write_text("def main():\n    return DEFAULT_INPUT\n"
                              "if __name__ == '__main__':\n    main(DEFAULT_INPUT)\n",
                              encoding="utf-8")
            self.assertEqual(sorted(undefined_globals(broken)),
                             [("broken", "DEFAULT_INPUT"), ("broken.main", "DEFAULT_INPUT")])

    def test_a_name_a_function_declares_global_and_assigns_is_bound(self):
        with tempfile.TemporaryDirectory() as d:
            ok = Path(d) / "ok.py"
            ok.write_text("def load():\n    global CACHE\n    CACHE = {}\n"
                          "def read():\n    return CACHE\n", encoding="utf-8")
            self.assertEqual(undefined_globals(ok), [])

    def test_the_discovery_reaches_every_tracked_script(self):
        """Every tracked script, not the package and the tests alone. The count is not fixed
        here, because it changes with every script added."""
        found = modules()
        for name in ("agentcontext/runner.py", "agentcontext/steps/repository_cloning.py",
                     "tests/test_undefined_names.py", "_website/build_site.py",
                     "4_data-analysis/_shared.py", "run_pipeline.py",
                     "deploy/entrypoint.py", "1_sampling/run_sampling.py"):
            with self.subTest(name=name):
                self.assertIn(REPO_ROOT / name, found)
        self.assertGreater(len(found), 150)


if __name__ == "__main__":
    unittest.main()
