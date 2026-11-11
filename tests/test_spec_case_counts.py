#!/usr/bin/env python3
"""
Every `**`TestClass`** (N)` that a spec states, compared with the class on disk.

Strategy:
  - Each `*-spec.md` names its suite in a `See `<file>.py`` line under `## Tests`, and
    lists its classes as `- **`TestName`** (N): ...`. The count is written by hand, and a
    change that adds a test and does not update the spec leaves the count wrong without a
    warning.
  - The suites are read by AST rather than imported, because a spec's suite may need a package
    that the interpreter that runs this check does not have. The check reads the file, not a
    run of it.

Run with:  python3 -m unittest tests.test_spec_case_counts -v
Requirements: Python 3.14, stdlib only.
"""

import ast
import re
import subprocess
import unittest
from pathlib import Path

from agentcontext import REPO_ROOT

CLASS_LINE = re.compile(r"^-\s+\*\*`(Test\w+)`\*\*\s+\((\d+)\)")
SUITE_LINE = re.compile(r"^See `([\w./-]+\.py)`")
# A class named in a list item, with a count or without one, and several to an item.
NAMED_CLASS = re.compile(r"\*\*`(Test\w+)`\*\*")


def specs() -> list:
    """Every `*-spec.md` in the repository, outside the archived notes and the copies of
    collected files, one of which is a `create-spec.md` command of another repository.

    `git ls-files` lists the tracked files and the untracked files that no ignore rule covers.
    Listing the filesystem would also read the specs of every git worktree under
    `.claude/worktrees/`, which are this repository at older commits."""
    listing = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z",
         "--", "*-spec.md"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True).stdout
    return sorted(REPO_ROOT / name for name in set(filter(None, listing.split("\0")))
                  if "docs/history" not in name)


def listed(spec: Path) -> set:
    """Every class that the `## Tests` section of a spec names in a list item."""
    names, in_tests = set(), False
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_tests = line.strip() == "## Tests"
            continue
        if in_tests and line.strip().startswith("-"):
            names |= set(NAMED_CLASS.findall(line))
    return names


def claimed(spec: Path) -> tuple:
    """The suite that a spec names and the counts that it states, as (path, {class: count})."""
    suite, counts = None, {}
    in_tests = False
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_tests = line.strip() == "## Tests"
            continue
        if not in_tests:
            continue
        if suite is None:
            found = SUITE_LINE.match(line.strip())
            if found:
                suite = found.group(1)
        found = CLASS_LINE.match(line.strip())
        if found:
            counts[found.group(1)] = int(found.group(2))
    return suite, counts


def cases_on_disk(path: Path) -> dict:
    """Every `Test*` class in the file and the number of `test_*` methods in it."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            found[node.name] = sum(
                1 for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test_"))
    return found


class TestEveryStatedCountIsTheCountOnDisk(unittest.TestCase):

    def test_at_least_the_known_specs_are_read(self):
        """Tests the discovery, so that a change that stops finding the specs fails here rather
        than passing over an empty set."""
        found = specs()
        self.assertGreaterEqual(len(found), 15, [p.name for p in found])
        named = [p for p in found if claimed(p)[1]]
        self.assertGreaterEqual(len(named), 10, [p.name for p in named])

    def test_each_stated_count_matches(self):
        for spec in specs():
            suite, counts = claimed(spec)
            if not counts:
                continue
            with self.subTest(spec=spec.relative_to(REPO_ROOT).as_posix()):
                self.assertIsNotNone(
                    suite, f"{spec.name} states case counts and names no suite file")
                path = (spec.parent / suite)
                self.assertTrue(path.is_file(), f"{spec.name} names {suite}, which is not there")
                on_disk = cases_on_disk(path)
                for name, count in sorted(counts.items()):
                    self.assertIn(name, on_disk, f"{suite} has no class {name}")
                    self.assertEqual(
                        on_disk[name], count,
                        f"{spec.name} states {count} case(s) for {name}, and {suite} has "
                        f"{on_disk[name]}")

    def test_a_spec_that_lists_its_classes_lists_every_one(self):
        """A spec that lists its classes without counts must still list every class of its
        suite. Comparing only the classes written with a count would not detect a class that
        the list leaves out. A class that the spec names and the suite no longer has fails
        too."""
        checked = 0
        for spec in specs():
            names = listed(spec)
            if not names:
                continue
            suite, _ = claimed(spec)
            with self.subTest(spec=spec.relative_to(REPO_ROOT).as_posix()):
                self.assertIsNotNone(suite, f"{spec.name} lists classes and names no suite file")
                path = spec.parent / suite
                self.assertTrue(path.is_file(), f"{spec.name} names {suite}, which is not there")
                checked += 1
                on_disk = set(cases_on_disk(path))
                self.assertEqual(sorted(on_disk - names), [],
                                 f"{suite} has classes that {spec.name} does not list")
                self.assertEqual(sorted(names - on_disk), [],
                                 f"{spec.name} lists classes that {suite} does not have")
        self.assertGreaterEqual(checked, 15)


if __name__ == "__main__":
    unittest.main()
