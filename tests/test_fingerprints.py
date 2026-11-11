"""Tests for agentcontext.fingerprints: the module closure that a fingerprint covers, and the
fingerprint of the code that the closure runs.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_fingerprints -v
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from agentcontext import fingerprints

CODE = '''"""A module."""
from agentcontext import logs


def copy(path):
    """Copy one file."""
    logs.info("component", f"Copying {path}")
    # The destination mirrors the path.
    return path + ".copy"
'''


class TestCodeFingerprint(unittest.TestCase):
    """The code that a module runs decides the fingerprint, and the text that only documents or
    logs it does not."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fingerprint(self, text: str) -> str:
        path = self.tmp / "module.py"
        path.write_text(text, encoding="utf-8")
        return fingerprints.code_fingerprint([path])

    def test_a_docstring_a_comment_a_log_message_and_the_layout_leave_it_unchanged(self):
        before = self.fingerprint(CODE)
        edits = [CODE.replace("Copy one file.", "Copy a file to its destination."),
                 CODE.replace("# The destination mirrors the path.", "# Beside the original."),
                 CODE.replace('f"Copying {path}"', 'f"Copied {path}"'),
                 CODE.replace("    logs.info(\"component\", f\"Copying {path}\")\n", ""),
                 CODE.replace("return path + \".copy\"", "return (path\n            + \".copy\")")]
        for edit in edits:
            with self.subTest(edit=edit):
                self.assertNotEqual(edit, CODE)
                self.assertEqual(self.fingerprint(edit), before)

    def test_a_change_to_what_runs_changes_it(self):
        self.assertNotEqual(self.fingerprint(CODE.replace('".copy"', '".bak"')),
                            self.fingerprint(CODE))

    RAISES = '''from agentcontext import history


def fetch(path, cause):
    if cause:
        raise history.Unavailable(cause or "exit", f"no history for {path}")
    message = f"Could not read {path}"
    raise ValueError("The record of " + path + " is not JSON", message)
'''

    def test_without_exception_texts_a_reworded_exception_keeps_it(self):
        """The text of an exception that a `raise` statement constructs is left out, and a
        value that the exception carries, a string without a space, stays. A text assigned to a
        name before the `raise` stays too, because a caller could store the value of the name in
        a record."""
        def without(text: str) -> str:
            path = self.tmp / "module.py"
            path.write_text(text, encoding="utf-8")
            return fingerprints.code_fingerprint([path], exception_texts=False)

        before = without(self.RAISES)
        for old, new in (('f"no history for {path}"', 'f"the history of {path} is missing"'),
                         ('" is not JSON"', '" cannot be parsed"')):
            with self.subTest(edit=new):
                edited = self.RAISES.replace(old, new)
                self.assertEqual(without(edited), before)
                self.assertNotEqual(self.fingerprint(edited), self.fingerprint(self.RAISES))
        for old, new in (('or "exit"', 'or "failed"'),
                         ('f"Could not read {path}"', 'f"Could not open {path}"')):
            with self.subTest(edit=new):
                self.assertNotEqual(without(self.RAISES.replace(old, new)), before)

    def test_a_package_copy_has_the_fingerprint_of_the_package(self):
        """Each file is named by its path under `root`, so that a test can edit a copy of the
        package and compare its fingerprint with the package's."""
        files = fingerprints.module_closure("agentcontext.collector")
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "agentcontext"
            shutil.copytree(fingerprints.PACKAGE_ROOT, copy,
                            ignore=shutil.ignore_patterns("__pycache__"))
            copied = fingerprints.module_closure("agentcontext.collector", copy)
            self.assertEqual(fingerprints.code_fingerprint(copied, root=copy),
                             fingerprints.code_fingerprint(files))

    def test_the_collector_closure_is_fingerprinted(self):
        files = fingerprints.module_closure("agentcontext.collector")
        names = {path.name for path in files}
        self.assertTrue({"collector.py", "paths.py", "clone.py", "commits.py"} <= names)
        self.assertEqual(len(fingerprints.code_fingerprint(files)), 64)

    def test_a_module_that_moves_to_another_path_changes_it(self):
        """Each tree is named by its path, and the same code in another module is another
        closure."""
        moved = self.tmp / "elsewhere.py"
        moved.write_text(CODE, encoding="utf-8")
        self.assertNotEqual(fingerprints.code_fingerprint([moved]), self.fingerprint(CODE))


class TestModuleClosure(unittest.TestCase):
    """The files with the code that a module runs, read from its imports, in a package that the
    test writes. `pkg/step.py` imports from three modules in three ways."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "pkg"
        files = {
            "__init__.py": "",
            # A name imported from a module: the module's file is in the closure, though
            # `pkg.helpers.helper` is no file.
            "step.py": ("from pkg.helpers import helper\n"
                        "from pkg.sub import part\n\n\n"
                        "def run():\n"
                        "    # An import inside a function runs as well, when the function does.\n"
                        "    from pkg import late\n"
                        "    return helper(part.value, late.VALUE)\n"),
            "helpers.py": "def helper(*values):\n    return values\n",
            "late.py": "VALUE = 1\n",
            # A subpackage: its `__init__.py` runs when a module below it is imported.
            "sub/__init__.py": "READY = True\n",
            "sub/part.py": "value = 2\n",
            "unused.py": "NEVER = 0\n",
        }
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def closure(self) -> set:
        return {path.relative_to(self.root).as_posix()
                for path in fingerprints.module_closure("pkg.step", self.root)}

    def test_every_file_the_module_runs_is_in_it_and_no_other(self):
        self.assertEqual(self.closure(), {"__init__.py", "step.py", "helpers.py", "late.py",
                                          "sub/__init__.py", "sub/part.py"})

    def test_an_import_for_type_hints_alone_is_not_in_it(self):
        """An import under `if TYPE_CHECKING:` never runs. `resume.py` names the collector's
        configuration there. Following that import would put the collector's code into the
        record of every step that reads the list of finished repositories, and an edit to a
        module that only schedules the collection would then run those steps again."""
        (self.root / "hinted.py").write_text("SIZE = 3\n", encoding="utf-8")
        (self.root / "fallback.py").write_text("SIZE = 4\n", encoding="utf-8")
        (self.root / "late.py").write_text(
            "import typing\n"
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from pkg.hinted import SIZE\n"
            "if typing.TYPE_CHECKING:\n"
            "    import pkg.hinted\n"
            "else:\n"
            "    from pkg import fallback\n"
            "VALUE = 1\n", encoding="utf-8")
        self.assertEqual(self.closure(), {"__init__.py", "step.py", "helpers.py", "late.py",
                                          "fallback.py", "sub/__init__.py", "sub/part.py"})

    def test_a_script_outside_the_package_gets_the_closures_of_its_imports(self):
        """An analysis script runs outside the package and imports its modules, and their code
        decides what the script writes."""
        script = self.tmp / "analysis.py"
        script.write_text("import json\n"
                          "from pkg.helpers import helper\n"
                          "import pkg.sub.part\n", encoding="utf-8")
        self.assertEqual({path.relative_to(self.root).as_posix()
                          for path in fingerprints.imports_closure([script], self.root)},
                         {"__init__.py", "helpers.py", "sub/__init__.py", "sub/part.py"})


if __name__ == "__main__":
    unittest.main()
