"""
Tests for rq2/venn_diagram.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - `load_repos` is tested over a small repos.csv under a temporary directory with DATA_DIR
    replaced, and its column list is compared with the fixture's header: every column that
    is not in NON_FLAG_COLUMNS has to be a TRUE/FALSE column, as the script's comment
    requires.
  - `venn_diagram_for_agent_adoption` is run with OUTPUT_DIR replaced and `plt.annotate`
    and `venn.venn` wrapped, so that the test checks the callout's text and the six sets.
  - One subprocess run over the fixture under testdata/output/.

Requirements: Python 3.14 (pandas, matplotlib and venn, no network)
Run with:  python3 -m unittest test_rq2_venn_diagram -v
"""

import contextlib
import csv
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from agentcontext import logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_venn_diagram_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("venn_diagram", RQ2 / "venn_diagram.py")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


HEADER = (FIXTURE / "repos.csv").read_text(encoding="utf-8").splitlines()[0].split(",")


def repo(name: str, *flags: str, **values: str) -> dict:
    return {"repo_name": name, **{flag: "TRUE" for flag in flags}, **values}


def write_repos_csv(path: Path, rows: list, header: list = HEADER) -> None:
    """A repos.csv with the fixture's columns, every flag FALSE unless the row names it."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()
        for row in rows:
            full = {column: "FALSE" for column in header}
            full.update({column: value for column, value in {
                "scanned_at": "2026-09-12T00:00:00Z", "branch": "main", "head_sha": "0" * 40,
                "head_commit_date": "2026-09-01T00:00:00Z",
                "github_link": "https://github.com/" + row["repo_name"],
                "dataset_version": "1.0.0"}.items() if column in header})
            full.update(row)
            writer.writerow(full)


# The tools' sets differ from each other, so that a set drawn from the wrong column fails the
# test.
ROWS = [
    repo("a/1", "claude", "agents.md"),
    repo("a/2", "claude", "copilot"),
    repo("a/3", "cursor", "gemini"),
    repo("a/4", "codex", "opencode"),
    repo("a/5", "agents.md", "no_specific_tool"),
    repo("a/6", "gemini"),
]


class _Redirected(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "data"
        self.out = self.tmp / "out"
        self.data.mkdir()
        self.out.mkdir()
        for name, value in (("DATA_DIR", self.data), ("OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)

    def load(self, rows: list, header: list = HEADER) -> pd.DataFrame:
        write_repos_csv(self.data / "repos.csv", rows, header)
        with contextlib.redirect_stdout(io.StringIO()):
            return mod.load_repos()


class TestLoadRepos(_Redirected):

    def test_the_non_flag_columns_are_dropped(self):
        df = self.load(ROWS)
        for column in mod.NON_FLAG_COLUMNS:
            self.assertNotIn(column, df.columns)
        self.assertIn("repo_name", df.columns)

    def test_a_missing_non_flag_column_is_tolerated(self):
        header = [c for c in HEADER if c not in ("head_sha", "dataset_version")]
        df = self.load(ROWS, header)
        self.assertEqual(len(df), len(ROWS))
        self.assertNotIn("head_sha", df.columns)

    def test_repo_name_stays_a_string_and_every_other_column_is_boolean(self):
        df = self.load(ROWS)
        self.assertEqual(list(df["repo_name"]), [row["repo_name"] for row in ROWS])
        for column in df.columns:
            if column != "repo_name":
                with self.subTest(column=column):
                    self.assertEqual(df[column].dtype, bool)

    def test_the_spellings_of_true(self):
        rows = [repo("s/1", claude="TRUE"), repo("s/2", claude="true"), repo("s/3", claude="True"),
                repo("s/4", claude="FALSE"), repo("s/5", claude=""), repo("s/6", claude="yes")]
        df = self.load(rows)
        self.assertEqual(list(df["claude"]), [True, True, True, False, False, False])

    def test_the_flags_are_read(self):
        df = self.load(ROWS).set_index("repo_name")
        self.assertTrue(df.loc["a/1", "claude"])
        self.assertTrue(df.loc["a/1", "agents.md"])
        self.assertFalse(df.loc["a/1", "codex"])
        self.assertTrue(df.loc["a/5", "no_specific_tool"])


class TestTheSchemaAgainstTheFixture(unittest.TestCase):
    """A column of repos.csv that is neither `repo_name` nor listed in NON_FLAG_COLUMNS is
    coerced to boolean, and a text column coerced that way reads as FALSE in every row
    without raising. Every such column of the CSV therefore has to contain TRUE or FALSE."""

    def test_every_column_outside_the_list_contains_true_or_false(self):
        with open(FIXTURE / "repos.csv", newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        for column in HEADER:
            if column == "repo_name" or column in mod.NON_FLAG_COLUMNS:
                continue
            with self.subTest(column=column):
                self.assertTrue(all(row[column] in ("TRUE", "FALSE") for row in rows),
                                f"{column} is not a flag column and is not in NON_FLAG_COLUMNS")

    def test_every_listed_column_is_in_the_header_of_repos_csv(self):
        for column in mod.NON_FLAG_COLUMNS:
            with self.subTest(column=column):
                self.assertIn(column, HEADER)


class TestTheDiagram(_Redirected):

    def _draw(self, rows: list) -> tuple:
        df = self.load(rows)
        buffer = io.StringIO()
        with mock.patch.object(mod.plt, "annotate", wraps=mod.plt.annotate) as annotate, \
                mock.patch.object(mod.venn, "venn", wraps=mod.venn.venn) as drawn, \
                contextlib.redirect_stderr(buffer):
            mod.venn_diagram_for_agent_adoption(df)
        mod.plt.close("all")
        self.sets = drawn.call_args.args[0]
        return logs.messages(buffer.getvalue()), annotate

    def test_the_six_sets_are_the_repositories_of_each_tool_in_order(self):
        """The test reads back the sets handed to venn and fails when Cursor and Gemini are
        swapped or Codex is read from the claude column. The order sets each set's color."""
        self._draw(ROWS)
        self.assertEqual(list(self.sets), ["Claude", "Copilot", "Cursor", "Gemini", "Codex",
                                           "OpenCode"])
        self.assertEqual(self.sets, {"Claude": {"a/1", "a/2"}, "Copilot": {"a/2"},
                                     "Cursor": {"a/3"}, "Gemini": {"a/3", "a/6"},
                                     "Codex": {"a/4"}, "OpenCode": {"a/4"}})

    def test_the_figure_is_written(self):
        out, _ = self._draw(ROWS)
        pdf = self.out / "venn-tool-usage.pdf"
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF"))
        self.assertIn("Wrote venn-tool-usage.pdf", out)

    def test_the_callout_states_the_agents_md_total_and_the_exclusive_count(self):
        _, annotate = self._draw(ROWS)
        self.assertEqual(annotate.call_count, 1)
        self.assertEqual(annotate.call_args.args[0], "AGENTS.md\n(n=2, 1 exclusive)")

    def test_the_exclusive_count_is_agents_md_without_a_tool(self):
        """`no_specific_tool` is also set for a repository with portable skills as its shared
        files and no AGENTS.md, which the callout about AGENTS.md does not count."""
        _, annotate = self._draw([*ROWS, repo("a/7", "skills", "no_specific_tool")])
        self.assertEqual(annotate.call_args.args[0], "AGENTS.md\n(n=2, 1 exclusive)")

    def test_the_callout_is_left_out_when_no_repository_has_agents_md(self):
        _, annotate = self._draw([repo("a/1", "claude"), repo("a/2", "copilot"), repo("a/3", "cursor"),
                                  repo("a/4", "gemini"), repo("a/5", "codex")])
        self.assertEqual(annotate.call_count, 0)

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self._draw(ROWS)
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestOverTheFixture(unittest.TestCase):

    def test_the_script_draws_the_fixture(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        env = dict(os.environ, DATA_COLLECTION_OUTPUT=str(FIXTURE), ANALYSIS_OUTPUT_DIR=str(tmp),
                   MPLBACKEND="Agg", PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, str(RQ2 / "venn_diagram.py")],
                                cwd=str(RQ2), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Loaded 32 rows from repos.csv", logs.messages(result.stderr))
        self.assertEqual([p.name for p in (tmp / "rq2").iterdir()], ["venn-tool-usage.pdf"])
        self.assertTrue((tmp / "rq2" / "venn-tool-usage.pdf").read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
