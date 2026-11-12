"""
Tests for rq2/language_tool_association.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - Every function is run over a small repos.csv and a small ghs_filtered.json written under
    a temporary directory, reached by replacing the module's DATA_DIR, SAMPLING_DIR and
    OUTPUT_DIR. The extracted GHS sampling frame is never read.
  - `seaborn.heatmap` is wrapped, and the heatmap's numbers are read off the matrices that
    the script passes to it. The heatmap's column labels and title are read off the axes that
    the script drew on.

Requirements: Python 3.14 (pandas, matplotlib and seaborn, no network)
Run with:  python3 -m unittest test_rq2_language_tool_association -v
"""

import contextlib
import csv
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest import mock

import pandas as pd
from agentcontext import SEVEN_ZIP, logs

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_language_tool_association_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP
os.environ.setdefault("MPLBACKEND", "Agg")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("language_tool_association", RQ2 / "language_tool_association.py")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


HEADER = (FIXTURE / "repos.csv").read_text(encoding="utf-8").splitlines()[0].split(",")


def repo(name: str, *flags: str, **values: str) -> dict:
    return {"repo_name": name, **{flag: "TRUE" for flag in flags}, **values}


def write_repos_csv(path: Path, rows: list) -> None:
    """A repos.csv with the fixture's columns, every flag FALSE unless the row names it."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HEADER)
        writer.writeheader()
        for row in rows:
            full = {column: "FALSE" for column in HEADER}
            full.update({"scanned_at": "2026-09-12T00:00:00Z", "branch": "main",
                         "head_sha": "0" * 40, "head_commit_date": "2026-09-01T00:00:00Z",
                         "github_link": "https://github.com/" + row["repo_name"],
                         "dataset_version": "1.0.0"})
            full.update(row)
            writer.writerow(full)


# Six configured repositories with a language and a tool, two with no specific tool, one
# (`z/absent`) that the GHS sampling frame does not list, and one GHS item (`g/only`) that is
# not configured. Go and Rust have one joined repository each, and Rust is named first.
ROWS = [
    repo("t/1", "claude", "context_file"),
    repo("t/2", "claude", "cursor", "context_file"),
    repo("t/3", "copilot", "context_file"),
    repo("p/1", "claude", claude="true", context_file="true"),
    repo("p/2", "gemini"),
    repo("r/1", "claude", "codex", "context_file"),
    repo("z/absent", "claude", "context_file"),
    repo("a/1", "no_specific_tool", "agents.md", "context_file"),
    repo("a/2", "no_specific_tool", "agents.md", "context_file"),
]
# ROWS without the repositories with no specific tool, which leaves that column empty.
WITHOUT_AGENTS_MD = [row for row in ROWS if not row["repo_name"].startswith("a/")]
LANGUAGE = {"t/1": "TypeScript", "t/2": "TypeScript", "t/3": "TypeScript", "p/1": "Python",
            "p/2": "Python", "r/1": "Rust", "g/only": "Go", "a/1": "TypeScript", "a/2": "Go"}


class _Redirected(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "output"
        self.sampling = self.tmp / "1_sampling"
        self.out = self.tmp / "out"
        for directory in (self.data, self.sampling, self.out):
            directory.mkdir()
        for name, value in (("DATA_DIR", self.data), ("SAMPLING_DIR", self.sampling),
                            ("OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)
        write_repos_csv(self.data / "repos.csv", ROWS)
        self.write_ghs(LANGUAGE)

    def write_ghs(self, language: dict) -> None:
        """A ghs_filtered.json with one item per repository name, with its language."""
        items = [{"name": name, "mainLanguage": lang, "createdAt": "2020-01-01T00:00:00",
                  "contributors": 1, "commits": 1, "size": 1} for name, lang in language.items()]
        ghs = {"_metadata": {"input_source": {"downloaded_at": "2026-03-16T18:07:01Z"}}, "items": items}
        (self.sampling / "ghs_filtered.json").write_text(json.dumps(ghs), encoding="utf-8")

    def load(self) -> pd.DataFrame:
        with contextlib.redirect_stdout(io.StringIO()):
            return mod.load_data()


class TestLoadData(_Redirected):

    def test_the_join_is_inner_on_the_repository_name(self):
        df = self.load()
        self.assertEqual(sorted(df["repo_name"]), ["a/1", "a/2", "p/1", "p/2", "r/1", "t/1", "t/2", "t/3"])
        self.assertNotIn("g/only", set(df["name"]))

    def test_a_repository_named_twice_in_the_sampling_frame_is_refused(self):
        """A name listed twice would give the repository two rows, counted twice in the
        ranking and in the heatmap."""
        ghs = json.loads((self.sampling / "ghs_filtered.json").read_text(encoding="utf-8"))
        ghs["items"].append(dict(ghs["items"][0]))
        (self.sampling / "ghs_filtered.json").write_text(json.dumps(ghs), encoding="utf-8")
        with self.assertRaises(SystemExit) as stop:
            self.load()
        self.assertIn("do not name each repository once", str(stop.exception))

    def test_the_language_is_attached(self):
        df = self.load().set_index("repo_name")
        self.assertEqual(df.loc["r/1", "mainLanguage"], "Rust")

    def test_the_tool_flags_become_booleans_whatever_the_case(self):
        df = self.load().set_index("repo_name")
        for tool in mod.TOOLS:
            self.assertEqual(df[tool].dtype, bool)
        self.assertTrue(df.loc["p/1", "claude"])       # spelled "true"
        self.assertTrue(df.loc["t/1", "claude"])       # spelled "TRUE"
        self.assertFalse(df.loc["p/2", "claude"])
        self.assertTrue(df.loc["p/2", "gemini"])

    @unittest.skipUnless(shutil.which(SEVEN_ZIP) and shutil.which("git"),
                         "7zz or git is not on PATH")
    def test_a_sampling_frame_that_differs_from_its_tracked_archive_stops(self):
        """In a checkout, git tracks `ghs_filtered.json.7z`, and the extracted sampling frame
        stays when a pull replaces the archive. The archive here holds another language."""
        frame = self.sampling / "ghs_filtered.json"
        staging = self.tmp / "staging"
        staging.mkdir()
        (staging / frame.name).write_bytes(frame.read_bytes().replace(b"Rust", b"Java"))
        archive = self.sampling / "ghs_filtered.json.7z"
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(archive), frame.name],
                       cwd=str(staging), check=True, capture_output=True)
        subprocess.run(["git", "init", "-q", str(self.tmp)], check=True, capture_output=True)
        subprocess.run(["git", "add", str(archive.relative_to(self.tmp))], cwd=str(self.tmp),
                       check=True, capture_output=True)
        with self.assertRaises(SystemExit) as stop:
            self.load()
        self.assertIn("ghs_filtered.json.7z", str(stop.exception))
        frame.write_bytes((staging / frame.name).read_bytes())
        self.assertEqual(self.load().set_index("repo_name").loc["r/1", "mainLanguage"], "Java")


class TestTheRanking(_Redirected):

    def test_columns_order_and_ranks(self):
        """Go and Rust have one repository each, and the rows name Rust first. Without the
        name as a second key, tied languages would keep the order in which the rows first
        named them, and the row order of repos.csv would decide which language ranks
        third."""
        with contextlib.redirect_stdout(io.StringIO()):
            ranking = mod.write_language_ranking(self.load())
        self.assertEqual(list(ranking.columns), ["rank", "language", "count"])
        self.assertEqual(list(zip(ranking["rank"], ranking["language"], ranking["count"])),
                         [(1, "TypeScript", 4), (2, "Python", 2), (3, "Go", 1), (4, "Rust", 1)])
        written = pd.read_csv(self.out / "language_ranking.csv")
        self.assertEqual(list(written.columns), ["rank", "language", "count"])
        self.assertEqual(list(written["language"]), ["TypeScript", "Python", "Go", "Rust"])

    def test_the_top_five_are_shares_of_the_joined_population(self):
        df = self.load()
        with contextlib.redirect_stdout(io.StringIO()):
            ranking = mod.write_language_ranking(df)
            mod.write_top5_latex(df, ranking)
        tex = (self.out / "top5_languages.tex").read_text(encoding="utf-8")
        self.assertEqual(tex.splitlines(), [
            "\\begin{enumerate}",
            "  \\item TypeScript (n=4, 50.0\\%)",
            "  \\item Python (n=2, 25.0\\%)",
            "  \\item Go (n=1, 12.5\\%)",
            "  \\item Rust (n=1, 12.5\\%)",
            "\\end{enumerate}",
        ])

    def test_a_language_name_is_written_as_latex_that_prints_it(self):
        """`#` is a parameter character in LaTeX, and a bare `C#` would break the snippet. C#
        is among the languages that the filter keeps."""
        self.write_ghs({**LANGUAGE, "t/1": "C#", "t/2": "C#", "t/3": "C#", "a/1": "C#"})
        df = self.load()
        with contextlib.redirect_stdout(io.StringIO()):
            ranking = mod.write_language_ranking(df)
            mod.write_top5_latex(df, ranking)
        self.assertEqual(ranking["language"].iloc[0], "C#")
        tex = (self.out / "top5_languages.tex").read_text(encoding="utf-8")
        self.assertEqual(tex.splitlines()[1], "  \\item C\\# (n=4, 50.0\\%)")


class TestTheHeatmap(_Redirected):

    def _draw(self) -> tuple:
        """Draw the heatmap, and return the matrix and the labels handed to seaborn and the
        axes, which keep their tick labels and title after the figure is closed."""
        df = self.load()
        drawn = {}
        real_subplots = mod.plt.subplots

        def subplots(*args, **kwargs):
            fig, ax = real_subplots(*args, **kwargs)
            drawn["ax"] = ax
            return fig, ax

        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)   # the zero-total tool
            ranking = mod.write_language_ranking(df)
            with mock.patch.object(mod.sns, "heatmap", wraps=mod.sns.heatmap) as heatmap, \
                    mock.patch.object(mod.plt, "subplots", subplots):
                mod.write_heatmap(df, ranking)
        (pct,), kwargs = heatmap.call_args.args, heatmap.call_args.kwargs
        return pct, kwargs["annot"], drawn["ax"]

    def test_the_column_labels_are_turned_so_that_they_do_not_overlap(self):
        """The column labels, a name and a count each, are too wide to fit side by side under
        the columns, where "No specific tool" would run into "Copilot" and "Cursor"."""
        _, _, ax = self._draw()
        labels = ax.get_xticklabels()
        self.assertEqual(len(labels), len(mod.TOOLS))
        self.assertEqual({(label.get_rotation(), label.get_ha()) for label in labels},
                         {(45.0, "right")})

    def test_the_figure_embeds_truetype_fonts(self):
        """The script writes the heatmap through `_shared.save_figure`, so that the PDF embeds
        TrueType fonts rather than Type 3 fonts."""
        self._draw()
        data = (self.out / "language_tool_heatmap.pdf").read_bytes()
        self.assertIn(b"/FontFile2", data)
        self.assertNotIn(b"/Type3", data)

    def test_a_cell_is_the_share_of_the_tools_repositories(self):
        """These rows include repositories with no specific tool, so that the test checks a
        count in that column other than 0 of 0."""
        pct, annot, _ = self._draw()
        self.assertEqual(list(pct.index), ["TypeScript", "Python", "Go", "Rust"])
        self.assertEqual(list(pct.columns), [mod.TOOL_LABELS[t] for t in mod.TOOLS])
        # Claude: t/1, t/2, p/1 and r/1, two of them TypeScript
        self.assertAlmostEqual(pct.loc["TypeScript", "Claude"], 50.0)
        self.assertEqual(annot.loc["TypeScript", "Claude"], "2\n(50.0%)")
        self.assertEqual(annot.loc["Rust", "Codex"], "1\n(100.0%)")
        self.assertEqual(annot.loc["Python", "Copilot"], "0\n(0.0%)")
        # No specific tool: a/1 in TypeScript and a/2 in Go
        self.assertEqual(annot.loc["TypeScript", "No specific tool"], "1\n(50.0%)")
        self.assertEqual(annot.loc["Go", "No specific tool"], "1\n(50.0%)")
        self.assertEqual(annot.loc["Python", "No specific tool"], "0\n(0.0%)")

    def test_the_column_labels_and_the_title_show_the_counts(self):
        """The test reads the column labels and the title back and fails for labels in the
        wrong order. Over the eight joined repositories, Claude has t/1, t/2, p/1 and r/1, and
        Copilot, Cursor, Gemini and Codex have one each. OpenCode has none, and a/1 and a/2 have
        no specific tool."""
        _, _, ax = self._draw()
        self.assertEqual([label.get_text() for label in ax.get_xticklabels()],
                         ["Claude\n(n=4)", "Copilot\n(n=1)", "No specific tool\n(n=2)",
                          "Cursor\n(n=1)", "Gemini\n(n=1)", "Codex\n(n=1)", "OpenCode\n(n=0)"])
        self.assertEqual(ax.get_title(), "Agentic AI Tool Usage by Programming Language (n = 8)")

    def test_a_tool_with_no_repository_gives_nan_cells_and_still_a_figure(self):
        write_repos_csv(self.data / "repos.csv", WITHOUT_AGENTS_MD)
        pct, annot, ax = self._draw()
        self.assertTrue(pct["No specific tool"].isna().all())
        self.assertEqual(annot.loc["Rust", "No specific tool"], "0\n(nan%)")
        self.assertEqual(ax.get_xticklabels()[2].get_text(), "No specific tool\n(n=0)")
        self.assertTrue((self.out / "language_tool_heatmap.pdf").read_bytes().startswith(b"%PDF"))


class TestContextFileAdoption(_Redirected):

    def test_tied_languages_are_ordered_by_name(self):
        """Twenty languages with one, two or three repositories each. Sorted by the total
        alone, the table would leave the order of tied languages to pandas' default sort,
        which is not stable."""
        totals = {f"L{i:02d}": (i * 7) % 3 + 1 for i in range(20)}
        rows, language = [], {}
        for lang, total in totals.items():
            for k in range(total):
                rows.append(repo(f"{lang.lower()}/{k}", "claude", "context_file"))
                language[f"{lang.lower()}/{k}"] = lang
        write_repos_csv(self.data / "repos.csv", rows)
        self.write_ghs(language)
        with contextlib.redirect_stdout(io.StringIO()):
            mod.write_context_file_adoption_per_language(self.load())
        table = pd.read_csv(self.out / "context_file_language_adoption.csv")
        self.assertEqual(list(table["language"]),
                         sorted(totals, key=lambda lang: (-totals[lang], lang)))

    def test_the_rate_per_language(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.write_context_file_adoption_per_language(self.load())
        table = pd.read_csv(self.out / "context_file_language_adoption.csv")
        self.assertEqual(list(table.columns),
                         ["language", "total_repos", "repos_with_context_file", "adoption_rate"])
        self.assertEqual(list(table["language"]), ["TypeScript", "Python", "Go", "Rust"])
        self.assertEqual(list(table["total_repos"]), [4, 2, 1, 1])
        self.assertEqual(list(table["repos_with_context_file"]), [4, 1, 1, 1])
        self.assertEqual(list(table["adoption_rate"]), [1.0, 0.5, 1.0, 1.0])
        self.assertIn("Over all languages, 7 of 8 repositories have a context file (87.5%)",
                      logs.messages(buffer.getvalue()))


class TestMain(_Redirected):

    def test_the_four_files_are_written(self):
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mod.main()
        self.assertEqual(sorted(p.name for p in self.out.iterdir()),
                         ["context_file_language_adoption.csv", "language_ranking.csv",
                          "language_tool_heatmap.pdf", "top5_languages.tex"])

    def test_nothing_is_written_beside_the_script(self):
        before = {p.name for p in RQ2.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()), warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            mod.main()
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
