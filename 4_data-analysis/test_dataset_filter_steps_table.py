"""
Tests for dataset/filter_steps_table.py

Strategy:
  - The script reads one JSON file and writes LaTeX. Every case writes a metadata block in
    the format that `1_sampling/4_filter_ghs.py` saves, and reads the table back from the
    file.
  - One run as a subprocess with ANALYSIS_OUTPUT_DIR set and no output argument checks that
    the table is under the temporary root and that nothing was written beside the script or
    into the working directory.
  - The metadata block of a sampling frame with all nine filter steps is the main case. The
    step that removed nothing is commented out, and the labels come from each step's key and
    recorded values.

Requirements: Python 3.14 (stdlib only, no extra pip installs)
Run with:  python3 -m unittest test_dataset_filter_steps_table -v
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agentcontext import SEVEN_ZIP

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_spec = importlib.util.spec_from_file_location("filter_steps_table_under_test",
                                               HERE / "dataset" / "filter_steps_table.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

STEPS = [
    {"stage": 1, "key": "gone", "removed": 1204, "remaining": 242710, "values": {}},
    {"stage": 2, "key": "duplicates", "removed": 1167, "remaining": 241543, "values": {"groups": 1167}},
    {"stage": 3, "key": "cleanup", "removed": 2241, "remaining": 239302,
     "values": {"flags": ["isArchived", "isDisabled", "isLocked", "isFork"]}},
    {"stage": 4, "key": "missing_data", "removed": 0, "remaining": 239302, "values": {}},
    {"stage": 5, "key": "licenses", "removed": 24646, "remaining": 214656,
     "values": {"allowed": ["MIT License"]}},
    {"stage": 6, "key": "languages", "removed": 31252, "remaining": 183404,
     "values": {"top": 10, "languages": ["C", "Go"]}},
    {"stage": 7, "key": "educational", "removed": 1994, "remaining": 181410, "values": {}},
    {"stage": 8, "key": "activity", "removed": 86331, "remaining": 95079,
     "values": {"commits_min": 352.0, "watchers_min": 6.5}},
    {"stage": 9, "key": "mirrors", "removed": 27, "remaining": 95052, "values": {"groups": 26}},
]


class TestFormatLatexNumber(unittest.TestCase):

    def test_thousands_get_the_latex_separator(self):
        for n, text in ((187304, "$187{,}304$"), (0, "$0$"), (999, "$999$"),
                        (1000, "$1{,}000$"), (40585, "$40{,}585$")):
            with self.subTest(n=n):
                self.assertEqual(_mod.format_latex_number(n), text)


class TestTheTable(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _table(self, steps, initial=243914, final=95079) -> tuple:
        path = self.tmp / "sampling_frame.json"
        path.write_text(json.dumps({"_metadata": {"input_source": {"item_count": initial},
                                                  "stages": steps, "final_count": final},
                                    "items": []}))
        out = self.tmp / "table.tex"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _mod.main([str(path), str(out)])
        return code, (out.read_text().splitlines() if out.exists() else []), err.getvalue()

    def test_every_step_is_a_numbered_line_and_an_empty_one_a_comment(self):
        code, lines, err = self._table(STEPS, final=95052)
        self.assertEqual(code, 0, err)
        self.assertIn(r"\textit{Initial Dataset} & -- & $243{,}914$ \\", lines)
        self.assertIn(r"(1) Gone from GitHub & $1{,}204$ & $242{,}710$ \\", lines)
        self.assertIn(r"(2) Duplicate Repos & $1{,}167$ & $241{,}543$ \\", lines)
        self.assertIn(r"%\hphantom{(1)\ }Missing Data & $0$ & $239{,}302$ \\", lines)
        self.assertIn(r"(5) Non-Top-10 Languages & $31{,}252$ & $183{,}404$ \\", lines)
        self.assertIn(r"(7) Commits $<352$ $\lor$ Watchers $<6.5$ & $86{,}331$ & $95{,}079$ \\",
                      lines)
        self.assertIn(r"(8) Later Copies at a Shared Head Commit & $27$ & $95{,}052$ \\", lines)
        self.assertIn(r"\textbf{Metadata-Filtered Dataset} & -- & $\mathbf{95{,}052}$ \\", lines)
        self.assertIn("Wrote", err)

    def test_the_step_of_the_copies_at_one_head_commit_names_no_mirror(self):
        """The step `mirrors` kept, of the repositories at one head commit, the one that GitHub
        created first, and removed the others, such as sqlalchemy/sqlalchemy beside the earlier
        zzzeek/sqlalchemy. These are not mirrors: A mirror is a classification criterion and a
        flag of GitHub, and a repository that GitHub marks as a mirror stays in the
        sampling frame."""
        _, lines, _ = self._table([STEPS[8]], final=95052)
        self.assertIn(r"(1) Later Copies at a Shared Head Commit & $27$ & $95{,}052$ \\", lines)
        self.assertFalse([line for line in lines if "mirror" in line.lower()], lines)

    def test_the_gone_step_names_the_rows_that_the_github_query_left_unresolved(self):
        """The step also removes the rows that the GitHub query left unresolved, although no
        response shows them to be gone from GitHub. Its label names these rows with their
        number."""
        steps = [dict(STEPS[0], values={"unresolved": 3})]
        _, lines, _ = self._table(steps)
        self.assertIn(r"(1) Gone from GitHub / $3$ Unresolved & $1{,}204$ & $242{,}710$ \\", lines)

    def test_the_gone_step_names_the_repositories_that_github_found_without_an_id(self):
        """The step also removes a repository that GitHub found without its id, the key by
        which the later stages identify a repository. GitHub has such a repository, and a label
        of `Gone from GitHub` alone would state that it was gone."""
        for values, label in (({"unresolved": 0, "without_id": 7},
                               r"Gone from GitHub / $7$ Without ID"),
                              ({"unresolved": 3, "without_id": 1_207},
                               r"Gone from GitHub / $3$ Unresolved / $1{,}207$ Without ID"),
                              # GitHub has a repository that it created after the window under a
                              # name that GHS crawled for an earlier repository.
                              ({"unresolved": 0, "without_id": 0, "created_after_window": 109},
                               r"Gone from GitHub / $109$ Created After Window")):
            with self.subTest(values=values):
                _, lines, _ = self._table([dict(STEPS[0], values=values)])
                self.assertIn(rf"(1) {label} & $1{{,}}204$ & $242{{,}}710$ \\", lines)

    def test_the_language_step_names_the_languages_that_the_end_date_left_out(self):
        """The step passes over a language that the end date of the sampling window left out
        and records it under `left_out`. Without the end date, that language would rank among
        the languages kept, and a label of the number alone would state that the step kept the
        language. A step that keeps every language records no number and is labeled by the
        languages left out."""
        for values, label in (
                ({"top": 10, "languages": ["C", "Go"], "left_out": {}}, "Non-Top-10 Languages"),
                ({"top": 10, "languages": ["C", "Go"], "left_out": {"Rust": "2026-08-01"}},
                 "Non-Top-10 Languages / Rust Left Out by End Date"),
                ({"top": 10, "languages": ["C", "Go"],
                  "left_out": {"Zig": None, "C#": "2026-08-01"}},
                 r"Non-Top-10 Languages / C\# and Zig Left Out by End Date"),
                ({"top": 3, "languages": ["C"],
                  "left_out": {"Zig": None, "Rust": None, "Dart": None}},
                 "Non-Top-3 Languages / Dart, Rust, and Zig Left Out by End Date"),
                ({"left_out": {"Rust": "2026-08-01"}}, "Rust Left Out by End Date")):
            with self.subTest(values=values):
                _, lines, _ = self._table([dict(STEPS[5], values=values)])
                self.assertIn(rf"(1) {label} & $31{{,}}252$ & $183{{,}}404$ \\", lines)

    def test_an_absolute_threshold_and_a_single_one_are_labeled(self):
        steps = [dict(STEPS[7], values={"commits_min": 500})]
        _, lines, _ = self._table(steps)
        self.assertIn(r"(1) Commits $<500$ & $86{,}331$ & $95{,}079$ \\", lines)

    def test_a_step_of_the_march_sampling_frame_is_labeled_by_its_description(self):
        """In a sampling frame that records each step's description and counts but no key,
        each step is named by its recorded description."""
        steps = [{"stage": 1, "description": "Remove archived/disabled/locked", "removed": 2241,
                  "remaining": 185063},
                 {"stage": 2, "description": "Remove rows with NULL PR/Issues data", "removed": 0,
                  "remaining": 185063}]
        code, lines, err = self._table(steps, initial=187304, final=185063)
        self.assertEqual(code, 0, err)
        self.assertIn(r"(1) Remove archived/disabled/locked & $2{,}241$ & $185{,}063$ \\", lines)
        self.assertIn(r"%\hphantom{(1)\ }Remove rows with NULL PR/Issues data & $0$ & $185{,}063$ \\",
                      lines)

    def test_a_description_is_written_as_latex_that_prints_it(self):
        """A language step can name C#, and `#` is a parameter in LaTeX."""
        step = {"stage": 4, "description": "Keep top 10 languages: ['C#', 'C++'] 50% & a_b",
                "removed": 1, "remaining": 2}
        _, lines, _ = self._table([step], initial=3, final=2)
        self.assertIn(r"(1) Keep top 10 languages: ['C\#', 'C++'] 50\% \& a\_b & $1$ & $2$ \\",
                      lines)

    def test_a_sampling_frame_without_filter_steps_is_an_error(self):
        path = self.tmp / "sampling_frame.json"
        path.write_text(json.dumps({"_metadata": {}, "items": []}))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(_mod.main([str(path), str(self.tmp / "t.tex")]), 1)
        self.assertIn("ERROR dataset_filter_steps_table: Could not read the filter steps", err.getvalue())
        self.assertFalse((self.tmp / "t.tex").exists())

    @unittest.skipUnless(shutil.which(SEVEN_ZIP) and shutil.which("git"),
                         "7zz or git is not on PATH")
    def test_a_sampling_frame_that_differs_from_its_tracked_archive_stops(self):
        """In a checkout, git tracks `ghs_filtered.json.7z`, and the extracted sampling frame
        stays when a pull replaces the archive. The archive here holds another final count."""
        def sampling_frame(final: int) -> bytes:
            return json.dumps({"_metadata": {"input_source": {"item_count": 10},
                                             "stages": STEPS[:1], "final_count": final},
                               "items": []}).encode("utf-8")
        staging = self.tmp / "staging"
        staging.mkdir()
        (staging / "ghs_filtered.json").write_bytes(sampling_frame(8))
        archive = self.tmp / "ghs_filtered.json.7z"
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), "ghs_filtered.json"],
                       cwd=str(staging), check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True, capture_output=True)
        subprocess.run(["git", "add", archive.name], cwd=str(self.tmp), check=True,
                       capture_output=True)
        path = self.tmp / "ghs_filtered.json"
        path.write_bytes(sampling_frame(9))
        out = self.tmp / "t.tex"
        with self.assertRaises(SystemExit) as stop:
            _mod.main([str(path), str(out)])
        self.assertIn("ghs_filtered.json.7z", str(stop.exception.code))
        self.assertFalse(out.exists())
        path.write_bytes(sampling_frame(8))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(_mod.main([str(path), str(out)]), 0)
        self.assertIn(r"\textbf{Metadata-Filtered Dataset} & -- & $\mathbf{8}$ \\",
                      out.read_text().splitlines())

    def test_the_table_is_written_under_the_analysis_output_directory(self):
        """Without a second argument, the table goes where every analysis output goes: under
        the analysis stage's data directory, or under `ANALYSIS_OUTPUT_DIR`, and not into the
        working directory or beside the script."""
        path = self.tmp / "sampling_frame.json"
        path.write_text(json.dumps({"_metadata": {"input_source": {"item_count": 10},
                                                  "stages": STEPS[:1], "final_count": 9},
                                    "items": []}))
        root = self.tmp / "analysis"
        env = dict(os.environ, ANALYSIS_OUTPUT_DIR=str(root))
        before = set((HERE / "dataset").iterdir())
        result = subprocess.run([sys.executable, str(HERE / "dataset" / "filter_steps_table.py"),
                                 str(path)], cwd=self.tmp, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(r"(1) Gone from GitHub & $1{,}204$ & $242{,}710$ \\",
                      (root / "dataset" / "filter_steps_table.tex").read_text())
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()),
                         ["analysis", "sampling_frame.json"])
        self.assertEqual(set((HERE / "dataset").iterdir()), before)


if __name__ == "__main__":
    unittest.main()
