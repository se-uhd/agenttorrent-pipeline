"""Tests for agentcontext/tools/tool_selection.py, the count behind the choice of the tools.

Strategy:
  - The tests write file lists as the cloning step keeps them, `git ls-tree -r -z` output
    compressed with xz under `<owner>§<repo>.ls-tree.xz`, so that the command reads them as
    it reads the file lists of a collection. The expected counts and cuts are worked out by
    hand from the lists.
  - Nothing reaches the network.

Run with:  python3 -m unittest tests.test_tools_tool_selection -v
"""

import csv
import json
import lzma
import shutil
import tempfile
import unittest
from pathlib import Path

from agentcontext.tools import tool_selection as _mod

SIGNATURES = [
    ("Big", "survey", ".big/|BIG.md"),
    ("Small", "survey", ".small/"),
    ("Near", "agent", ".near/|near.json"),
    ("Far", "agent", ".far/"),
    ("Farther", "agent", ".github/farther/"),
    ("Reviewer", "review", ".reviewer.yaml"),
]


class _Lists(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lists = self.tmp / "file_lists"
        self.lists.mkdir()
        self.signatures = self.tmp / "signatures.csv"
        with self.signatures.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["tool", "category", "paths", "doc_url", "doc_verified", "notes"])
            for tool, category, paths in SIGNATURES:
                writer.writerow([tool, category, paths, "https://example.test", "2026-10-20", ""])

    def _list(self, repo: str, *paths: str) -> None:
        entries = "".join(f"100644 blob {'0' * 40}\t{p}\0" for p in paths)
        with lzma.open(self.lists / f"{repo.replace('/', '§')}.ls-tree.xz", "wt",
                       encoding="utf-8") as f:
            f.write(entries)

    def _run(self) -> dict:
        out = self.tmp / "counts.json"
        code = _mod.main(["--file-lists", str(self.lists), "--signatures", str(self.signatures),
                          "--output", str(out)])
        self.assertEqual(code, 0)
        return json.loads(out.read_text(encoding="utf-8"))


class TestTheCounts(_Lists):

    def test_a_tool_counts_by_its_root_level_paths_alone(self):
        """A directory of a signature and a file that it names count at the root. The same
        names below the root count for no tool."""
        self._list("a/one", ".big/settings.json", "src/x.py")
        self._list("b/two", "BIG.md")
        self._list("c/three", "packages/web/.big/settings.json", "docs/BIG.md")
        self._list("d/four", ".github/farther/x.md", ".github/workflows/ci.yml")
        counts = self._run()
        self.assertEqual(counts["file_lists"], 4)
        self.assertEqual(counts["repositories"]["Big"], 2)
        self.assertEqual(counts["repositories"]["Farther"], 1)

    def test_an_owner_counts_once_for_a_tool(self):
        self._list("org/one", ".near/a.md")
        self._list("ORG/two", "near.json")
        self._list("other/three", ".near/b.md")
        counts = self._run()
        self.assertEqual(counts["repositories"]["Near"], 3)
        self.assertEqual(counts["owners"]["Near"], 2)

    def test_a_file_with_a_directory_s_name_is_no_directory(self):
        """A link to a directory is a file in a file list, and no directory signature matches
        it, because the dataset follows no linked tool directory."""
        self._list("a/one", ".big")
        self.assertEqual(self._run()["repositories"]["Big"], 0)


class TestTheCuts(_Lists):
    """Owners: Big 30, Small 6, Near 4, Far 1, Farther 0, and the review tool 20, which no cut
    counts. In owner order Big, Small, Near, Far, the largest step is 30/6 = 5.0, between the
    two tools of the survey. The steps below Small are 6/4 = 1.5 and 4/1 = 4.0. The larger of
    them puts the cut between Near and Far, which adds Near."""

    def setUp(self):
        super().setUp()
        for tool, n in (("big", 30), ("small", 6), ("near", 4), ("far", 1), ("reviewer", 20)):
            for i in range(n):
                path = ".reviewer.yaml" if tool == "reviewer" else f".{tool}/x.md"
                self._list(f"{tool}{i}/repo", path)
        self.cuts = self._run()["cuts"]

    def test_the_tools_above_the_largest_step_below_the_survey_are_added(self):
        self.assertEqual(self.cuts["order"], ["Big", "Small", "Near", "Far"])
        self.assertEqual(self.cuts["largest_step_below_the_survey"],
                         {"above": "Near", "below": "Far", "ratio": 4.0})
        self.assertEqual(self.cuts["added"], ["Near"])

    def test_the_head_tail_breaks_split_at_the_mean_while_the_head_is_a_minority(self):
        """The mean of 30, 6, 4 and 1 is 10.25, and Big alone is above it, which ends the
        breaks. Outside the survey, the mean of 4 and 1 is 2.5."""
        self.assertEqual(self.cuts["head_tail_breaks"],
                         [{"mean": 10.2, "tools": 4, "head": ["Big"]}])
        self.assertEqual(self.cuts["head_tail_breaks_outside_the_survey"],
                         [{"mean": 2.5, "tools": 2, "head": ["Near"]}])


class TestTheHeadTailBreaks(unittest.TestCase):

    def test_a_head_of_two_fifths_or_more_ends_the_breaks(self):
        """The mean of 10, 9, 1 and 1 is 5.25, with a and b above it. A head of half of the
        four tools ends the breaks, which do not split a from b."""
        self.assertEqual(_mod.head_tail_breaks({"a": 10, "b": 9, "c": 1, "d": 1}),
                         [{"mean": 5.2, "tools": 4, "head": ["a", "b"]}])

    def test_a_minority_head_is_split_again(self):
        """The mean of 100, 20, 1, 1, 1 and 1 is 20.7. Only a is above it, one of six, and a
        head of one tool is not split."""
        self.assertEqual(_mod.head_tail_breaks({"a": 100, "b": 20, "c": 1, "d": 1, "e": 1,
                                                "f": 1}),
                         [{"mean": 20.7, "tools": 6, "head": ["a"]}])


class TestTheInputs(_Lists):

    def test_an_unknown_category_is_refused(self):
        with self.signatures.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(["Odd", "helper", ".odd/", "", "", ""])
        self._list("a/one", ".big/x")
        code = _mod.main(["--file-lists", str(self.lists), "--signatures", str(self.signatures),
                          "--output", str(self.tmp / "counts.json")])
        self.assertEqual(code, 1)
        self.assertFalse((self.tmp / "counts.json").exists())

    def test_a_missing_directory_of_file_lists_is_one(self):
        code = _mod.main(["--file-lists", str(self.tmp / "none"), "--signatures",
                          str(self.signatures), "--output", str(self.tmp / "counts.json")])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
