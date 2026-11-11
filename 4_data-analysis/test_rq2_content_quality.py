"""
Tests for rq2/content_quality.py

Strategy:
  - The script is loaded by path, with 4_data-analysis/ and rq2/ on sys.path, under
    ANALYSIS_OUTPUT_DIR set to a temporary directory, so that nothing that it writes reaches
    rq2/.
  - `is_empty_file` is tested over strings built here.
  - `read_file`, `analyze_artifact_type` and `main` are run over a small validated tree and
    small mechanism CSVs written under a temporary directory, reached by replacing the
    module's VALIDATED_DIR, DATA_DIR and OUTPUT_DIR. The real tree under
    data/collection/data/validated/ is never read.
  - The file column of each entry of ARTIFACT_TYPES is looked up in the header of the
    fixture's CSV under testdata/output/, which is the detection step's header.

Requirements: Python 3.14 (pandas and numpy, no network)
Run with:  python3 -m unittest test_rq2_content_quality -v
"""

import contextlib
import csv
import importlib.util
import io
import math
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pandas as pd
from agentcontext import logs, repos

HERE = Path(__file__).resolve().parent
RQ2 = HERE / "rq2"
FIXTURE = HERE / "testdata" / "output"
for entry in (str(HERE), str(RQ2)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

_MODULE_TMP = tempfile.mkdtemp(prefix="test_rq2_content_quality_")
_SAVED_OUTPUT_ROOT = os.environ.get("ANALYSIS_OUTPUT_DIR")
os.environ["ANALYSIS_OUTPUT_DIR"] = _MODULE_TMP


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mod = _load("content_quality", RQ2 / "content_quality.py")


def tearDownModule():
    shutil.rmtree(_MODULE_TMP, ignore_errors=True)
    if _SAVED_OUTPUT_ROOT is None:
        os.environ.pop("ANALYSIS_OUTPUT_DIR", None)
    else:
        os.environ["ANALYSIS_OUTPUT_DIR"] = _SAVED_OUTPUT_ROOT


CSV_COLUMNS = ["type", "total", "lc_mean", "lc_median", "lc_q1", "lc_q3", "bin_1--5", "bin_6--10",
               "bin_11--20", "bin_21--50", "bin_51--100", "bin_101--200", "bin_201--500",
               "bin_501--1000", "bin_1001+", "gt_500"]


def write_table(path: Path, file_col: str, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["repo_name", file_col, "github_link"])
        for repo_name, file_path in rows:
            writer.writerow([repo_name, file_path, f"https://github.com/{repo_name}/{file_path}"])


def put(tree: Path, repo_name: str, file_path: str, content: str) -> None:
    target = tree / repo_name.replace("/", "§") / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


class _Redirected(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data = self.tmp / "output"
        self.tree = self.tmp / "validated"
        self.out = self.tmp / "rq2"
        for directory in (self.data, self.tree, self.out):
            directory.mkdir()
        for name, value in (("DATA_DIR", self.data), ("VALIDATED_DIR", self.tree),
                            ("OUTPUT_DIR", self.out)):
            self.addCleanup(setattr, mod, name, getattr(mod, name))
            setattr(mod, name, value)


class TestIsEmptyFile(unittest.TestCase):

    def test_nothing_and_whitespace_are_empty(self):
        for content in (None, "", "   ", "\n\n\t"):
            with self.subTest(content=content):
                self.assertTrue(mod.is_empty_file(content, "CLAUDE.md"))

    def test_text_is_not_empty(self):
        self.assertFalse(mod.is_empty_file("# Guidance\n", "CLAUDE.md"))
        self.assertFalse(mod.is_empty_file("x", "AGENTS.md"))

    def test_an_empty_json_object_or_array_is_empty(self):
        self.assertTrue(mod.is_empty_file("{}", ".claude/settings.json"))
        self.assertTrue(mod.is_empty_file(" [ ] \n", ".mcp.json"))
        self.assertTrue(mod.is_empty_file("{}", ".claude/SETTINGS.JSON"))

    def test_json_with_content_is_not_empty(self):
        self.assertFalse(mod.is_empty_file('{"a": 1}', ".claude/settings.json"))
        self.assertFalse(mod.is_empty_file("[1]", ".mcp.json"))
        self.assertFalse(mod.is_empty_file("null", ".mcp.json"))

    def test_malformed_json_is_not_empty(self):
        self.assertFalse(mod.is_empty_file("{", ".claude/settings.json"))

    def test_json_nested_deeper_than_the_parser_reads_is_not_empty(self):
        """The collected file cleanup and the detection step keep such a file, and a
        RecursionError of the parser would stop the analysis at every Start."""
        deep = '{"a": ' * 60_000 + "1" + "}" * 60_000
        self.assertFalse(mod.is_empty_file(deep, ".claude/settings.json"))

    def test_an_empty_object_outside_a_json_file_is_content(self):
        self.assertFalse(mod.is_empty_file("{}", "CLAUDE.md"))

    def test_a_comment_only_toml_file_is_not_empty(self):
        """A file of one comment line counts as content, because the check looks for
        whitespace alone."""
        self.assertFalse(mod.is_empty_file("# nothing here\n", ".gemini/commands/x.toml"))
        self.assertTrue(mod.is_empty_file("  \n", ".gemini/commands/x.toml"))


class TestReadFile(_Redirected):

    def test_the_repository_directory_is_owner_section_repo(self):
        put(self.tree, "own/repo", "docs/CLAUDE.md", "hello\n")
        self.assertEqual(mod.read_file("own/repo", "docs/CLAUDE.md"), "hello\n")
        self.assertTrue((self.tree / "own§repo" / "docs" / "CLAUDE.md").is_file())

    def test_the_directory_name_is_the_one_that_agentcontext_repos_gives(self):
        """The script takes the naming from `agentcontext/repos.py` rather than writing the
        separator itself, and a change to that module reaches the script."""
        target = self.tree / "own#repo" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_text("hello\n", encoding="utf-8")
        with mock.patch.object(repos, "SEPARATOR", "#"):
            self.assertEqual(mod.read_file("own/repo", "CLAUDE.md"), "hello\n")

    def test_a_missing_file_is_none(self):
        self.assertIsNone(mod.read_file("own/repo", "CLAUDE.md"))

    def test_a_directory_is_none(self):
        (self.tree / "own§repo" / "CLAUDE.md").mkdir(parents=True)
        self.assertIsNone(mod.read_file("own/repo", "CLAUDE.md"))

    def test_undecodable_bytes_are_replaced_rather_than_refused(self):
        target = self.tree / "own§repo" / "CLAUDE.md"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"ok \xff\xfe\n")
        self.assertEqual(mod.read_file("own/repo", "CLAUDE.md"), "ok \ufffd\ufffd\n")


class TestAnalyzeArtifactType(_Redirected):

    def setUp(self):
        super().setUp()
        put(self.tree, "a/x", "CLAUDE.md", "one\ntwo\nthree\n")             # 3 lines
        put(self.tree, "a/x", "AGENTS.md", "")                               # empty
        put(self.tree, "b/y", "docs/GEMINI.md", "line\n" * 600)              # 600 lines
        put(self.tree, "a/x", ".github/copilot-instructions.md", "  \n")    # whitespace, 1 line
        write_table(self.data / "context-files.csv", "context_file", [
            ("a/x", "CLAUDE.md"), ("a/x", "AGENTS.md"), ("b/y", ".cursorrules"),
            ("b/y", "docs/GEMINI.md"), ("a/x", ".github/copilot-instructions.md")])

    def analyze(self) -> dict:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            stats = mod.analyze_artifact_type("context-files.csv", "context_file", "Context files")
        self.stderr = err.getvalue()
        return stats

    def test_totals_empties_and_the_missing_file(self):
        """The missing file is not counted as non-empty, because it would then count as
        content that nobody read. The empty files are counted for `main`, which writes no table
        over an empty file. No share of the empty files is a statistic of the table."""
        stats = self.analyze()
        self.assertEqual((stats["type"], stats["total"], stats["empty"], stats["unread"]),
                         ("Context files", 5, 2, 1))
        for key in ("empty_pct", "non_empty", "non_empty_pct"):
            self.assertNotIn(key, stats)
        self.assertIn("WARNING rq2_content_quality: Could not read 1 of the 5 files that "
                      "context-files.csv lists from", self.stderr)

    def test_a_missing_file_is_neither_empty_nor_counted_in_the_lines(self):
        """The missing `.cursorrules` is in `total` and `unread`, and in no line count."""
        stats = self.analyze()
        # the four files on disk have 3, 0, 600 and 1 lines
        self.assertEqual(stats["lc_mean"], 151.0)
        self.assertEqual(stats["lc_median"], 2.0)
        self.assertEqual(stats["lc_q1"], 0.8)
        self.assertEqual(stats["lc_q3"], 152.2)

    def test_the_bins_cover_the_non_empty_files(self):
        stats = self.analyze()
        bins = {key: value for key, value in stats.items() if key.startswith("bin_")}
        self.assertEqual(bins, {"bin_1--5": 1, "bin_6--10": 0, "bin_11--20": 0, "bin_21--50": 0,
                                "bin_51--100": 0, "bin_101--200": 0, "bin_201--500": 0,
                                "bin_501--1000": 1, "bin_1001+": 0})
        self.assertEqual(stats["gt_500"], 1)

    def test_the_bin_edges(self):
        for lines, expected in ((5, "bin_1--5"), (6, "bin_6--10"), (10, "bin_6--10"), (11, "bin_11--20"),
                                (20, "bin_11--20"), (21, "bin_21--50"), (50, "bin_21--50"),
                                (51, "bin_51--100"), (100, "bin_51--100"), (101, "bin_101--200"),
                                (200, "bin_101--200"), (201, "bin_201--500"), (500, "bin_201--500"),
                                (501, "bin_501--1000"), (1000, "bin_501--1000"), (1001, "bin_1001+")):
            with self.subTest(lines=lines):
                put(self.tree, "c/z", "CLAUDE.md", "x\n" * lines)
                write_table(self.data / "context-files.csv", "context_file", [("c/z", "CLAUDE.md")])
                stats = self.analyze()
                self.assertEqual(stats[expected], 1)
                self.assertEqual(sum(v for k, v in stats.items() if k.startswith("bin_")), 1)
                self.assertEqual(stats["gt_500"], 1 if lines > 500 else 0)

    def test_an_empty_mechanism_csv_gives_blanks_rather_than_zeros_or_a_division(self):
        """A mechanism CSV with no row has no share and no line count, and a 0.0 would read
        as measured."""
        write_table(self.data / "context-files.csv", "context_file", [])
        stats = self.analyze()
        self.assertEqual((stats["total"], stats["empty"], stats["gt_500"]), (0, 0, 0))
        for key in ("lc_mean", "lc_median", "lc_q1", "lc_q3"):
            with self.subTest(statistic=key):
                self.assertTrue(math.isnan(stats[key]))

    def test_the_stats_contain_the_csv_columns(self):
        self.assertEqual(list(self.analyze()),
                         CSV_COLUMNS + ["unread", "empty", "empty_kept", "target_absent",
                                        "no_text"])

    def test_a_linked_agent_or_rule_counts_the_lines_of_the_file_that_it_links_to(self):
        """The collector stores a link as a marker of one line that contains its target. The
        detection step reads a linked agent or rule in the file that the marker names, for its
        front matter and its language. Counted at the link's path, each linked agent or rule
        would be a file of one line. A context file that is a link is a reference and counts as
        the marker."""
        agent = "---\nname: downloader\ndescription: Stages a model.\n---\n" + "Step.\n" * 7
        rule = "---\ntrigger: always_on\n---\n" + "Rule.\n" * 17
        put(self.tree, "n/m", ".claude/agents/downloader.md",
            "../../plugins/modelopt/agents/downloader.md\n")
        put(self.tree, "n/m", "plugins/modelopt/agents/downloader.md", agent)
        put(self.tree, "n/m", ".agent/rules/x.md", "../../docs/rules/x.md\n")
        put(self.tree, "n/m", "docs/rules/x.md", rule)
        put(self.tree, "n/m", "CLAUDE.md", "AGENTS.md\n")
        put(self.tree, "n/m", "AGENTS.md", "Read the docs.\n" * 30)
        write_table(self.data / "subagents.csv", "subagent", [("n/m", ".claude/agents/downloader.md")])
        write_table(self.data / "rules.csv", "rule", [("n/m", ".agent/rules/x.md")])
        write_table(self.data / "context-files.csv", "context_file", [("n/m", "CLAUDE.md")])
        with contextlib.redirect_stderr(io.StringIO()):
            subagents = mod.analyze_artifact_type("subagents.csv", "subagent", "Subagents")
            rules = mod.analyze_artifact_type("rules.csv", "rule", "Rules")
            context = mod.analyze_artifact_type("context-files.csv", "context_file", "Context files")
        self.assertEqual((subagents["lc_median"], subagents["bin_11--20"]), (11.0, 1))
        self.assertEqual((rules["lc_median"], rules["bin_11--20"]), (20.0, 1))
        self.assertEqual((context["lc_median"], context["bin_1--5"]), (1.0, 1))

    def test_a_context_file_that_is_a_real_symlink_counts_as_its_marker(self):
        """rsync keeps a link inside a directory that the collector copies whole, such as
        gomlx/gomlx's `.agents/CLAUDE.md`, which links to the `AGENTS.md` beside it. The
        detection step publishes such a link as a reference of one line, as it publishes a
        marker. Read through the link, the context file would count the lines of that
        AGENTS.md."""
        put(self.tree, "g/m", ".agents/AGENTS.md", "Read the docs.\n" * 160)
        os.symlink("AGENTS.md", self.tree / repos.repo_name_to_dir("g/m") / ".agents" / "CLAUDE.md")
        write_table(self.data / "context-files.csv", "context_file",
                    [("g/m", ".agents/AGENTS.md"), ("g/m", ".agents/CLAUDE.md")])
        stats = self.analyze()
        self.assertEqual({key: stats[key] for key in
                          ("total", "unread", "empty", "lc_median", "bin_1--5", "bin_101--200")},
                         {"total": 2, "unread": 0, "empty": 0, "lc_median": 80.5, "bin_1--5": 1,
                          "bin_101--200": 1})

    def test_a_context_file_that_is_a_real_symlink_without_a_target_counts_as_its_marker(self):
        """rsync's `--safe-links` judges a link by its text and keeps a link that leads nowhere
        inside a directory that the collector copies whole. The detection step publishes a
        context file that is such a link as a reference of one line, as it publishes a marker
        with a target that the tree lacks, and the tree is the tree that the CSVs describe. A
        run that stopped on such a link as an unread file would stop at every Start."""
        put(self.tree, "g/m", ".agents/README.md", "Read the docs.\n")
        os.symlink("notes/AGENTS.md",
                   self.tree / repos.repo_name_to_dir("g/m") / ".agents" / "CLAUDE.md")
        write_table(self.data / "context-files.csv", "context_file", [("g/m", ".agents/CLAUDE.md")])
        stats = self.analyze()
        self.assertEqual({key: stats[key] for key in
                          ("total", "unread", "empty", "lc_median", "bin_1--5")},
                         {"total": 1, "unread": 0, "empty": 0, "lc_median": 1.0, "bin_1--5": 1})

    def test_a_linked_skill_with_a_target_that_the_tree_lacks_has_no_line_count(self):
        """mathlab/pina links its skills to `.opencode/skills`, which the collector does not
        copy, and the detection step publishes the language of such a row blank. Counted at the
        link's path, each skill would be a file of one line. Such a row stays in the total, and
        the statistics and the bins are over the files with a line count, as for an unread
        file."""
        put(self.tree, "m/p", ".claude/skills/create-skill/SKILL.md",
            "../../../.opencode/skills/create-skill/SKILL.md\n")
        put(self.tree, "m/p", ".claude/skills/review/SKILL.md", "Review.\n" * 12)
        write_table(self.data / "skills.csv", "skills.md", [
            ("m/p", ".claude/skills/create-skill/SKILL.md"), ("m/p", ".claude/skills/review/SKILL.md")])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            skills = mod.analyze_artifact_type("skills.csv", "skills.md", "Skills")
        self.assertEqual({key: skills[key] for key in
                          ("total", "target_absent", "unread", "lc_median", "lc_q1", "bin_1--5",
                           "bin_11--20")},
                         {"total": 2, "target_absent": 1, "unread": 0, "lc_median": 12.0,
                          "lc_q1": 12.0, "bin_1--5": 0, "bin_11--20": 1})
        self.assertNotIn("could not be read", err.getvalue())


    def test_a_file_that_is_no_text_has_no_line_count(self):
        """The detection step publishes the `line_count` of a file that git-crypt encrypted
        blank, because lines read from its bytes are counted from noise. The file stays in the
        total, and the statistics and the bins are over the files with a line count."""
        put(self.tree, "v/r", "CLAUDE.md", "\0GITCRYPT\0" + "\x8f\n" * 122)
        put(self.tree, "v/r", "AGENTS.md", "Read the docs.\n" * 12)
        write_table(self.data / "context-files.csv", "context_file",
                    [("v/r", "CLAUDE.md"), ("v/r", "AGENTS.md")])
        stats = self.analyze()
        self.assertEqual({key: stats[key] for key in
                          ("total", "no_text", "unread", "lc_median", "bin_11--20",
                           "bin_101--200")},
                         {"total": 2, "no_text": 1, "unread": 0, "lc_median": 12.0,
                          "bin_11--20": 1, "bin_101--200": 0})


    def test_a_file_in_utf_16_counts_the_lines_that_the_detection_step_counts(self):
        """Windows PowerShell 5.1 writes a file in UTF-16 with CRLF. Read as UTF-8, each half
        of a CRLF is a line break of its own, and the file counts twice its lines."""
        path = self.tree / "w§p" / "CLAUDE.md"
        path.parent.mkdir(parents=True)
        path.write_bytes("".join(f"line {i}\r\n" for i in range(1, 11)).encode("utf-16"))
        write_table(self.data / "context-files.csv", "context_file", [("w/p", "CLAUDE.md")])
        stats = self.analyze()
        self.assertEqual((stats["lc_median"], stats["bin_6--10"]), (10.0, 1))

    def test_a_form_feed_or_a_line_separator_inside_a_line_starts_no_line(self):
        """The detection step counts the lines that LF, CRLF or CR end. A skill of
        EndstoneMC/endstone has a U+001D inside a line, and a U+2028 or a form feed can be inside
        a line as well. Each of the five lines of this file holds another such character."""
        separators = "\x1d\u2028\x0c\x85\x0b"
        put(self.tree, "e/s", "CLAUDE.md",
            "".join(f"line{separator}{i}\n" for i, separator in enumerate(separators)))
        write_table(self.data / "context-files.csv", "context_file", [("e/s", "CLAUDE.md")])
        stats = self.analyze()
        self.assertEqual((stats["lc_median"], stats["bin_1--5"], stats["bin_6--10"]), (5.0, 1, 0))

class TestTheWriters(_Redirected):

    RESULTS = [
        {"type": "Context files", "total": 10, "lc_mean": 5.0, "lc_median": 4.0, "lc_q1": 2.0,
         "lc_q3": 6.0, **{f"bin_{b}": 0 for b in ("1--5", "6--10", "11--20", "21--50", "51--100",
                                                    "101--200", "201--500", "501--1000", "1001+")},
         "gt_500": 0, "unread": 0, "empty": 0},
        {"type": "Rules", "total": 2_000, "lc_mean": 900.0, "lc_median": 1_250.0, "lc_q1": 11.8,
         "lc_q3": 1_520.5, **{f"bin_{b}": 0 for b in ("1--5", "6--10", "11--20", "21--50",
                                                        "51--100", "101--200", "201--500",
                                                        "501--1000", "1001+")},
         "gt_500": 1_200, "unread": 0, "empty": 0},
    ]

    def test_the_csv_has_one_row_per_type(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mod.write_csv(self.RESULTS)
        df = pd.read_csv(self.out / "content_quality.csv")
        self.assertEqual(list(df.columns), CSV_COLUMNS)
        self.assertEqual(list(df["type"]), ["Context files", "Rules"])

    def test_the_tex_has_a_row_per_type_and_a_total_row(self):
        with contextlib.redirect_stdout(io.StringIO()):
            mod.write_latex(self.RESULTS)
        tex = (self.out / "content_quality.tex").read_text(encoding="utf-8")
        self.assertIn("\\textbf{Context files} & 10 & 4 (2--6) & 0 \\\\", tex)
        # A whole line count is written without decimals, any other line count with one, and
        # both with a thousands separator.
        self.assertIn("\\textbf{Rules} & 2,000 & 1,250 (11.8--1,520.5) & 1,200 \\\\", tex)
        self.assertIn("\\textbf{Total} & \\textbf{2,010} & & \\textbf{1,200} \\\\", tex)
        self.assertNotIn("Empty", tex)
        self.assertTrue(tex.startswith("\\begin{table}[t]\n"))
        self.assertTrue(tex.endswith("\\end{table}\n"))

    def test_the_summary_prints_every_type_and_a_total(self):
        buffer = io.StringIO()
        with contextlib.redirect_stderr(buffer):
            mod.print_summary_table(self.RESULTS)
        out = logs.messages(buffer.getvalue())
        self.assertIn("Context files", out)
        self.assertIn("TOTAL", out)
        self.assertIn("gt_500", out)


class TestTheArtifactTypes(unittest.TestCase):

    def test_every_file_column_is_in_the_header_of_its_mechanism_csv(self):
        """TestMain writes its CSVs with the column names that it reads from ARTIFACT_TYPES. A
        column misspelled there, such as `rules` for `rule`, would be written and read under the
        same wrong name and pass, while the detection step's CSVs would raise a KeyError. The
        fixture's CSVs have the detection step's headers."""
        for csv_name, file_col, _ in mod.ARTIFACT_TYPES:
            with self.subTest(table=csv_name):
                with open(FIXTURE / csv_name, newline="", encoding="utf-8") as f:
                    header = next(csv.reader(f))
                self.assertIn(file_col, header)


class TestMain(_Redirected):

    def _write_all_tables(self) -> None:
        put(self.tree, "a/x", "CLAUDE.md", "hello\n")
        put(self.tree, "a/x", ".claude/settings.json", '{\n  "model": "x"\n}\n')
        for csv_name, file_col, _ in mod.ARTIFACT_TYPES:
            path = "CLAUDE.md" if csv_name != "settings.csv" else ".claude/settings.json"
            write_table(self.data / csv_name, file_col, [("a/x", path)])

    def test_both_outputs_are_written_over_every_type(self):
        self._write_all_tables()
        with contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        df = pd.read_csv(self.out / "content_quality.csv")
        self.assertEqual(list(df["type"]), [label for _, _, label in mod.ARTIFACT_TYPES])
        self.assertEqual(list(df.columns), CSV_COLUMNS)
        self.assertEqual(float(df.set_index("type").loc["Settings", "lc_median"]), 3.0)
        tex = (self.out / "content_quality.tex").read_text(encoding="utf-8")
        self.assertIn("\\textbf{Settings} & 1 & 3 (3--3) & 0 \\\\", tex)
        self.assertIn("\\textbf{Total} & \\textbf{10} & & \\textbf{0} \\\\", tex)

    def test_a_settings_file_nested_deeper_than_the_parser_reads_is_counted(self):
        """The collected file cleanup and the detection step keep a configuration JSON nested
        60,000 deep, and a RecursionError while parsing it would stop the analysis at every
        Start."""
        self._write_all_tables()
        put(self.tree, "a/x", ".claude/settings.json", "[" * 60_000 + "]" * 60_000)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            mod.main()
        settings = pd.read_csv(self.out / "content_quality.csv").set_index("type").loc["Settings"]
        self.assertEqual((int(settings["total"]), float(settings["lc_median"])), (1, 1.0))

    def test_a_listed_file_that_is_empty_writes_neither_output(self):
        """The collected file cleanup deletes every empty file before detection. A listed
        file that is empty therefore means a tree that the CSVs do not describe, and such a
        file stops the run as an unread file does."""
        self._write_all_tables()
        put(self.tree, "a/x", ".claude/settings.json", "{}")
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("ERROR rq2_content_quality: 1 listed file(s) are empty (1 in Settings)",
                      err.getvalue())
        self.assertFalse((self.out / "content_quality.csv").exists())
        self.assertFalse((self.out / "content_quality.tex").exists())

    def test_an_empty_file_that_an_agentic_workflow_imports_counts_with_no_lines(self):
        """The collected file cleanup keeps an empty file that a GitHub Agentic Workflow
        imports, because gh-aw compiles no workflow without a required import. The detection
        step lists such a file in `.github/agents/` as a subagent, and the analysis counts it
        rather than stopping at every Start. Without the workflow, the same listed file stops
        the run."""
        self._write_all_tables()
        put(self.tree, "a/x", ".github/workflows/triage.md",
            "---\non:\n  issues:\n    types: [opened]\nimports:\n"
            "  - .github/agents/triage.agent.md\n---\nTriage the issue.\n")
        put(self.tree, "a/x", ".github/agents/triage.agent.md", "\n")
        write_table(self.data / "subagents.csv", "subagent",
                    [("a/x", ".github/agents/triage.agent.md")])
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            mod.main()
        self.assertIn("INFO rq2_content_quality: 1 listed file(s) are empty (1 in Subagents), and "
                      "the collected file cleanup keeps them. Counted them with 0 lines.",
                      err.getvalue())
        subagents = pd.read_csv(self.out / "content_quality.csv").set_index("type").loc["Subagents"]
        self.assertEqual((int(subagents["total"]), float(subagents["lc_median"])), (1, 0.0))
        self.assertEqual(sum(int(subagents[c]) for c in CSV_COLUMNS if c.startswith("bin_")), 0)
        tex = (self.out / "content_quality.tex").read_text(encoding="utf-8")
        self.assertIn("\\textbf{Subagents} & 1 & 0 (0--0) & 0 \\\\", tex)

        (self.tree / "a§x" / ".github" / "workflows" / "triage.md").unlink()
        (self.out / "content_quality.csv").unlink()
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("ERROR rq2_content_quality: 1 listed file(s) are empty (1 in Subagents). "
                      "The collected file cleanup deletes such files.", err.getvalue())
        self.assertFalse((self.out / "content_quality.csv").exists())

    def _commands_row(self, path: str) -> pd.Series:
        """Run `main` with `commands.csv` listing `path` alone, and return the row of the
        commands, after asserting that the run logged the file as empty and kept."""
        self._write_all_tables()
        write_table(self.data / "commands.csv", "command", [("a/x", path)])
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            mod.main()
        self.assertIn("INFO rq2_content_quality: 1 listed file(s) are empty (1 in Commands), and "
                      "the collected file cleanup keeps them. Counted them with 0 lines.",
                      err.getvalue())
        return pd.read_csv(self.out / "content_quality.csv").set_index("type").loc["Commands"]

    def test_an_empty_anchor_of_planning_documents_counts_with_no_lines(self):
        """cursor-memory-bank's tasks count beside its `/van` command, and the collected file
        cleanup keeps an empty `.cursor/commands/van.md` with them. The detection step lists
        that file as a Cursor command. Without the tasks, the same listed file stops the
        run."""
        put(self.tree, "a/x", ".cursor/commands/van.md", "")
        put(self.tree, "a/x", "memory-bank/tasks.md", "# Tasks\n- [ ] Log in\n")
        commands = self._commands_row(".cursor/commands/van.md")
        self.assertEqual((int(commands["total"]), float(commands["lc_median"])), (1, 0.0))
        self.assertEqual(sum(int(commands[c]) for c in CSV_COLUMNS if c.startswith("bin_")), 0)

        (self.tree / "a§x" / "memory-bank" / "tasks.md").unlink()
        (self.out / "content_quality.csv").unlink()
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("ERROR rq2_content_quality: 1 listed file(s) are empty (1 in Commands)",
                      err.getvalue())
        self.assertFalse((self.out / "content_quality.csv").exists())

    def test_an_empty_prompt_file_of_the_codex_action_counts_with_no_lines(self):
        """The Codex action runs with the prompt of the file that its `prompt-file` input
        names, and the collected file cleanup keeps that file even when it is empty. A Copilot
        prompt file with that name is listed as a command."""
        put(self.tree, "a/x", ".github/workflows/review.yml",
            "jobs:\n  codex:\n    steps:\n      - uses: openai/codex-action@v1\n        with:\n"
            "          prompt-file: .github/prompts/review.prompt.md\n")
        put(self.tree, "a/x", ".github/prompts/review.prompt.md", "")
        commands = self._commands_row(".github/prompts/review.prompt.md")
        self.assertEqual((int(commands["total"]), float(commands["lc_median"])), (1, 0.0))
        tex = (self.out / "content_quality.tex").read_text(encoding="utf-8")
        self.assertIn("\\textbf{Commands} & 1 & 0 (0--0) & 0 \\\\", tex)

    def test_a_marker_with_a_target_that_the_tree_lacks_writes_both_outputs(self):
        """The collector never copies a target outside the repository, and the tree without
        the target is the tree that the CSVs describe. A run that stopped on such a marker
        would stop at every Start."""
        self._write_all_tables()
        put(self.tree, "a/x", ".cursor/rules/general.mdc", "../../.ai/rules/general.mdc\n")
        write_table(self.data / "rules.csv", "rule",
                    [("a/x", "CLAUDE.md"), ("a/x", ".cursor/rules/general.mdc")])
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            mod.main()
        self.assertIn("INFO rq2_content_quality: 1 listed file(s) are markers of links to a target "
                      "that the validated tree lacks (1 in Rules). Counted them without a line "
                      "count.", err.getvalue())
        rules = pd.read_csv(self.out / "content_quality.csv").set_index("type").loc["Rules"]
        self.assertEqual((int(rules["total"]), float(rules["lc_median"]), int(rules["bin_1--5"])),
                         (2, 1.0, 1))
        self.assertNotIn("target_absent", pd.read_csv(self.out / "content_quality.csv").columns)

    def test_a_type_with_no_file_has_no_line_count_in_the_tex(self):
        self._write_all_tables()
        write_table(self.data / "lsp.csv", "lsp", [])
        with contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        tex = (self.out / "content_quality.tex").read_text(encoding="utf-8")
        self.assertIn("\\textbf{LSP} & 0 & -- & 0 \\\\", tex)
        self.assertIn("\\textbf{Total} & \\textbf{9} & & \\textbf{0} \\\\", tex)

    def test_a_listed_file_that_cannot_be_read_writes_neither_output(self):
        """A table over a tree that lacks a listed file would state content for that file."""
        self._write_all_tables()
        write_table(self.data / "rules.csv", "rule", [("a/x", "CLAUDE.md"), ("a/x", "gone.mdc")])
        with self.assertRaises(SystemExit) as caught, \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("ERROR rq2_content_quality: Could not read 1 listed file(s)", err.getvalue())
        self.assertIn("(1 in Rules)", err.getvalue())
        self.assertFalse((self.out / "content_quality.csv").exists())
        self.assertFalse((self.out / "content_quality.tex").exists())

    def test_an_absent_tree_exits_one_before_reading_a_mechanism_csv(self):
        shutil.rmtree(self.tree)
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()) as err:
            mod.main()
        self.assertEqual(caught.exception.code, 1)
        self.assertIn(f"ERROR rq2_content_quality: The validated tree {self.tree} is missing.",
                      err.getvalue())
        self.assertFalse((self.out / "content_quality.csv").exists())

    def test_nothing_is_written_beside_the_script(self):
        self._write_all_tables()
        before = {p.name for p in RQ2.iterdir()}
        with contextlib.redirect_stdout(io.StringIO()):
            mod.main()
        self.assertEqual({p.name for p in RQ2.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
