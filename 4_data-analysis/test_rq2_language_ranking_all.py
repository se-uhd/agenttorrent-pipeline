"""
Tests for rq2/language_ranking_all.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - The ranking and the LaTeX list are tested with `build_repo_metadata` and `sample_names`,
    which the script imports into its own namespace, replaced by functions that return a
    DataFrame and a set of names built here.
  - One run through the real helper over a small repos_annotated.csv and ghs_filtered.json
    written under a temporary directory, reached by replacing `_repo_metadata`'s
    CLASSIFICATION_DIR, SAMPLING_DIR and OUTPUT_DIR. Neither extracted archive is read.

Requirements: Python 3.14 (pandas, no network)
Run with:  python3 -m unittest test_rq2_language_ranking_all -v
"""

import contextlib
import importlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
from agentcontext import logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_language_ranking_all_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("language_ranking_all", RQ2 / "language_ranking_all.py")
rm = importlib.import_module("_repo_metadata")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


def dataframe(languages: list) -> pd.DataFrame:
    return pd.DataFrame({
        "repo_name": [f"o/r{i}" for i in range(len(languages))],
        "main_language": languages,
        "repo_age_days": 100, "contributors": 2, "commits": 10, "size": 5,
    })


LANGUAGES = ["Python"] * 4 + ["Rust"] * 3 + ["Go"] * 2 + ["Java", "C", "Zig"] + [None]


class _Redirected(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.out = self.tmp / "out"
        self.out.mkdir()
        self.addCleanup(setattr, mod, "OUTPUT_DIR", mod.OUTPUT_DIR)
        mod.OUTPUT_DIR = self.out

    def run_with(self, languages: list, outside=()) -> str:
        """`main` over a DataFrame of these languages, with every repository in the sample
        except the repositories that `outside` names."""
        for name in ("build_repo_metadata", "sample_names"):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
        metadata = dataframe(languages)
        mod.build_repo_metadata = lambda: metadata
        mod.sample_names = lambda: set(metadata["repo_name"]) - set(outside)
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.main()
        return logs.messages(buffer.getvalue())

    def ranking(self) -> pd.DataFrame:
        return pd.read_csv(self.out / "language_ranking_all.csv")

    def tex(self) -> str:
        return (self.out / "top5_languages_all.tex").read_text(encoding="utf-8")


class TestTheRanking(_Redirected):

    def test_the_columns_and_the_order(self):
        """Java, C and Zig have one repository each. Tied languages are ranked by name and not
        by the row order of the input."""
        self.run_with(LANGUAGES)
        ranking = self.ranking()
        self.assertEqual(list(ranking.columns), ["rank", "language", "count"])
        self.assertEqual(list(ranking["rank"]), list(range(1, 7)))
        self.assertEqual(list(ranking["language"]), ["Python", "Rust", "Go", "C", "Java", "Zig"])
        self.assertEqual(list(ranking["count"])[:3], [4, 3, 2])
        self.assertEqual(list(ranking["count"]), sorted(ranking["count"], reverse=True))

    def test_a_repository_without_a_language_is_no_row(self):
        self.run_with(LANGUAGES)
        self.assertEqual(int(self.ranking()["count"].sum()), len(LANGUAGES) - 1)
        self.assertNotIn("nan", list(self.ranking()["language"].astype(str)))

    def test_the_count_of_languages_is_printed(self):
        out = self.run_with(LANGUAGES)
        self.assertIn("Wrote language_ranking_all.csv (6 languages)", out)

    def test_a_repository_outside_the_sample_is_not_ranked_or_counted(self):
        """The ranking is over the sample and not over every annotated repository, because
        the configured repositories that the ranking is compared with are drawn from the
        sample. Without the four Python repositories, Rust leads with three of the nine."""
        out = self.run_with(LANGUAGES, outside=("o/r0", "o/r1", "o/r2", "o/r3"))
        self.assertEqual(list(self.ranking()["language"]), ["Rust", "Go", "C", "Java", "Zig"])
        self.assertIn("  \\item Rust (n=3, 33.3\\%)", self.tex())
        self.assertIn("9 of the 13 annotated repositories in the sampling frame are in the sample", out)


class TestTheTopFive(_Redirected):

    def test_five_items_with_shares_of_every_repository(self):
        self.run_with(LANGUAGES)
        lines = self.tex().splitlines()
        self.assertEqual(lines[0], "\\begin{enumerate}")
        self.assertEqual(lines[-1], "\\end{enumerate}")
        items = [l for l in lines if l.startswith("  \\item ")]
        self.assertEqual(len(items), 5)
        # 13 repositories, one without a language. The shares are of all 13.
        self.assertEqual(items[0], "  \\item Python (n=4, 30.8\\%)")
        self.assertEqual(items[1], "  \\item Rust (n=3, 23.1\\%)")
        self.assertEqual(items[2], "  \\item Go (n=2, 15.4\\%)")

    def test_a_language_name_is_written_as_latex_that_prints_it(self):
        """`#` is a parameter character in LaTeX, and a bare `C#` would break the snippet."""
        self.run_with(["C#", "C#", "C#", "C++", "Python"])
        self.assertEqual(self.ranking()["language"].iloc[0], "C#")
        items = [l for l in self.tex().splitlines() if l.startswith("  \\item ")]
        self.assertEqual(items[0], "  \\item C\\# (n=3, 60.0\\%)")
        self.assertEqual(items[1], "  \\item C++ (n=1, 20.0\\%)")

    def test_fewer_than_five_languages_give_fewer_items(self):
        self.run_with(["Python", "Python", "Rust"])
        items = [l for l in self.tex().splitlines() if l.startswith("  \\item ")]
        self.assertEqual(items, ["  \\item Python (n=2, 66.7\\%)", "  \\item Rust (n=1, 33.3\\%)"])

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        self.run_with(LANGUAGES)
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


class TestThroughTheHelper(unittest.TestCase):
    """The script over the real `build_repo_metadata`, with its two inputs small and local."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        classification = self.tmp / "2_classification"
        sampling = self.tmp / "1_sampling"
        classification.mkdir()
        sampling.mkdir()
        pd.DataFrame({"repo_name": ["a/1", "a/2", "a/3", "a/4"], "github_id": [1, 2, 3, 4],
                      "default_branch": "main",
                      "engineered_project": ["true", "true", "false", "true"], "justification": "t"}) \
            .to_csv(classification / "repos_annotated.csv", index=False)
        items = [{"name": f"a/{i}", "mainLanguage": lang, "createdAt": "2020-01-01T00:00:00",
                  "contributors": 1, "commits": 1, "size": 1}
                 for i, lang in ((1, "Go"), (2, "Go"), (3, "Rust"), (5, "C"))]
        ghs = {"_metadata": {"input_source": {"downloaded_at": "2026-03-16T18:07:01Z"}}, "items": items}
        (sampling / "ghs_filtered.json").write_text(json.dumps(ghs), encoding="utf-8")
        self.out = self.tmp / "out"
        self.out.mkdir()
        for module, name, value in ((rm, "CLASSIFICATION_DIR", classification),
                                    (rm, "SAMPLING_DIR", sampling), (rm, "OUTPUT_DIR", self.out),
                                    (mod, "OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, value)

    def test_the_three_files_are_written_and_the_ranking_is_of_the_sample(self):
        """`a/3` is labeled `false`. The helper's table keeps it, and the ranking leaves it
        and its Rust out. `a/4` is in the sample and not in the sampling frame."""
        with contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["language_ranking_all.csv", "repo_metadata.csv", "top5_languages_all.tex"])
        ranking = pd.read_csv(self.out / "language_ranking_all.csv")
        self.assertEqual(list(zip(ranking["rank"], ranking["language"], ranking["count"])),
                         [(1, "Go", 2)])
        self.assertIn("  \\item Go (n=2, 100.0\\%)", (self.out / "top5_languages_all.tex").read_text())
        metadata = pd.read_csv(self.out / "repo_metadata.csv")
        self.assertEqual(sorted(metadata["repo_name"]), ["a/1", "a/2", "a/3"])


if __name__ == "__main__":
    unittest.main()
