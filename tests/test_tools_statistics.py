"""Test the stated statistics: the values that their sources compute, the markers that hold them
in the documents, and the differences that `--check` logs.

Each test of the command builds a git checkout in a temporary directory with a statistics file,
the files that its values come from and documents with stated statistics, and runs the command
over the checkout. The tests of the computations, one class for each module of
`agentcontext.statistics`, run each computation over small files in a temporary directory.

Requirements: Python 3.14 (stdlib only, and git on PATH).
Run with: python3 -m unittest tests.test_tools_statistics -v
"""

import contextlib
import hashlib
import io
import json
import lzma
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from unittest import mock

from agentcontext import REPO_ROOT, SEVEN_ZIP
import agentcontext.statistics as computations
from agentcontext.statistics import Files, Unavailable, call, code_digest
from agentcontext.tools import statistics


def marked(key, text, form=None):
    return f"<!--s:{key}{':' + form if form else ''}-->{text}<!--/s-->"


class _Checkout(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.write("data/repos.csv", "repo_name,tool,reason\no/a,claude,x\no/b,claude,y\n"
                                     "o/c,codex,y\no/d,claude,y\n")
        self.write("module.py", "LIMIT = 95_000_000\nPAGES = LIMIT // 19_000_000\n"
                                "WAITS = (10, 30, 60)\nNAME = 'x'\nCOLUMNS = ['a', 'b']\n"
                                "CALLS = (dict(a=1), dict(b=2), dict(c=3))\n")
        (self.root / "docs").mkdir(exist_ok=True)
        (self.root / "docs" / "facts.json.xz").write_bytes(
            lzma.compress(json.dumps({"counts": {"history_read": 3}, "runs": [1, 2]}).encode()))
        self.entries = {
            "repos": {"value": 4, "meaning": "repositories",
                      "from": {"rows": "data/repos.csv"}},
            "repos.claude": {"value": 3, "meaning": "repositories with Claude",
                             "from": {"rows": "data/repos.csv", "where": {"tool": "claude"}}},
        }
        self.document("docs/a.md", f"The sample holds {marked('repos', '4')} repositories, "
                                   f"{marked('repos.claude', '3')} of them with Claude.\n")

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def document(self, relative, text):
        """Write a document and track it with git."""
        self.write(relative, text)
        subprocess.run(["git", "add", relative], cwd=self.root, check=True)

    def run_tool(self, *argv, entries=None):
        self.write("docs/statistics.json", json.dumps(entries or self.entries, indent=2) + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = statistics.main([*argv, "--root", str(self.root)])
        return code, err.getvalue()

    def stored(self):
        return json.loads((self.root / "docs" / "statistics.json").read_text())

    def text(self, relative="docs/a.md"):
        return (self.root / relative).read_text(encoding="utf-8")


class TestValues(_Checkout):

    def test_every_value_and_stated_statistic_agreeing_passes_the_check(self):
        code, err = self.run_tool("--check")
        self.assertEqual(code, 0, err)
        self.assertIn("Checked 2 statistics, 2 of them stated in 1 documents, and found 0 "
                      "differences", err)

    def test_a_changed_csv_fails_the_check_and_is_written_by_the_command(self):
        with open(self.root / "data/repos.csv", "a", encoding="utf-8") as f:
            f.write("o/e,claude,y\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("statistics.json records repos as 4, but the data gives 5", err)
        self.assertIn("docs/a.md writes repos.claude as 3, but statistics.json gives 4", err)
        self.assertIn(marked("repos", "4"), self.text())
        code, err = self.run_tool(entries=self.entries)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["repos"]["value"], 5)
        self.assertIn(marked("repos", "5"), self.text())
        self.assertIn(marked("repos.claude", "4"), self.text())
        self.assertEqual(self.run_tool("--check", entries=self.stored())[0], 0)

    def test_rows_count_matching_rows_and_distinct_values(self):
        entries = {**self.entries,
                   "tools": {"value": 0, "from": {"rows": "data/repos.csv", "distinct": "tool"}},
                   "claude_y": {"value": 0, "from": {"rows": "data/repos.csv",
                                                     "where": {"tool": "claude", "reason": "y"}}}}
        self.document("docs/b.md", f"{marked('tools', '0')} tools, {marked('claude_y', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.stored()["tools"]["value"], self.stored()["claude_y"]["value"]),
                         (2, 2))

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_rows_are_counted_in_the_csv_of_a_7z_archive(self):
        """The rows of a CSV in a tracked archive, such as the unmatched attributions of a
        collection, are counted without an extracted copy, so that a regenerated archive changes
        the stated statistic."""
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", "data/repos.csv.7z", "data/repos.csv"],
                       cwd=self.root, check=True)
        (self.root / "data/repos.csv").unlink()
        entries = {"repos": {"value": 0, "from": {"rows": "data/repos.csv.7z"}},
                   "repos.claude": {"value": 0, "from": {"rows": "data/repos.csv.7z",
                                                         "where": {"tool": "claude"}}}}
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.stored()["repos"]["value"], self.stored()["repos.claude"]["value"]),
                         (4, 3))

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_rows_are_counted_in_an_archive_in_volumes(self):
        """The commit CSVs are tracked as archives in numbered volumes, and the first volume
        names the archive, so that a checkout without an extracted copy can count their rows."""
        self.write("data/repos.csv", "repo_name,tool\n" + "".join(
            f"r{n},{'claude' if n % 3 else 'cursor'}\n" for n in range(3000)))
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", "-mx=0", "-v20k", "data/repos.csv.7z",
                        "data/repos.csv"], cwd=self.root, check=True)
        (self.root / "data/repos.csv").unlink()
        self.assertTrue((self.root / "data/repos.csv.7z.002").is_file())
        entries = {"repos": {"value": 0, "from": {"rows": "data/repos.csv.7z.001"}},
                   "repos.claude": {"value": 0, "from": {"rows": "data/repos.csv.7z.001",
                                                         "where": {"tool": "claude"}}}}
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.stored()["repos"]["value"], self.stored()["repos.claude"]["value"]),
                         (3000, 2000))

    def test_a_missing_archive_cannot_be_checked(self):
        entries = {**self.entries, "unmatched": {"value": 0, "from": {"rows": "data/none.csv.7z"}}}
        self.document("docs/b.md", f"{marked('unmatched', '0')} attributions.\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("Could not read ", err)

    def test_the_size_of_a_file_is_its_bytes(self):
        self.write("data/archive.bin", "x" * 1_500_000)
        entries = {**self.entries, "archive_mb": {"value": 0, "scale": 1e-6, "decimals": 1,
                                                  "from": {"size": "data/archive.bin"}}}
        self.document("docs/b.md", f"The archive is {marked('archive_mb', '0.0')} MB.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["archive_mb"]["value"], 1_500_000)
        self.assertIn(marked("archive_mb", "1.5"), self.text("docs/b.md"))

    def test_a_json_value_and_a_length_are_read_by_pointer_from_an_xz_file(self):
        entries = {**self.entries,
                   "history_read": {"value": 0, "from": {"json": "docs/facts.json.xz",
                                                   "pointer": "/counts/history_read"}},
                   "runs": {"value": 0, "from": {"json": "docs/facts.json.xz", "pointer": "/runs",
                                                 "length": True}}}
        self.document("docs/b.md", f"{marked('history_read', '0')} and {marked('runs', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertIn(f"{marked('history_read', '3')} and {marked('runs', '2')}", self.text("docs/b.md"))

    def test_a_constant_is_read_without_importing_its_file(self):
        entries = {**self.entries,
                   "limit": {"value": 0, "scale": 1e-6,
                             "from": {"constant": "module.py", "name": "LIMIT"}},
                   "pages": {"value": 0, "from": {"constant": "module.py", "name": "PAGES"}},
                   "wait": {"value": 0, "from": {"constant": "module.py", "name": "WAITS",
                                                 "index": 1}},
                   "columns": {"value": 0, "from": {"constant": "module.py", "name": "COLUMNS",
                                                    "length": True}},
                   "calls": {"value": 0, "from": {"constant": "module.py", "name": "CALLS",
                                                  "length": True}}}
        self.document("docs/b.md", f"{marked('limit', '0')} MB, {marked('pages', '0', 'Words')} "
                                   f"pages, {marked('wait', '0')} seconds, "
                                   f"{marked('pages', '0')} pages, the {marked('pages', '0', 'ordinal')}, "
                                   f"{marked('columns', '0')} columns, {marked('calls', '0')} calls.\n")
        self.write("module.py", self.text("module.py") + "raise SystemExit('imported')\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertIn(f"{marked('limit', '95')} MB, {marked('pages', 'Five', 'Words')} pages, "
                      f"{marked('wait', '30')} seconds, {marked('pages', '5')} pages, the "
                      f"{marked('pages', 'fifth', 'ordinal')}, {marked('columns', '2')} columns, {marked('calls', '3')} calls",
                      self.text("docs/b.md"))

    def test_a_constant_that_is_not_a_number_cannot_be_computed(self):
        entries = {**self.entries, "name": {"value": 0, "from": {"constant": "module.py",
                                                                 "name": "NAME"}}}
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("The constant NAME of module.py is not a number", err)

    def test_shares_and_differences_follow_the_counts_they_are_computed_from(self):
        entries = {**self.entries,
                   "share": {"value": 0, "decimals": 1, "percent": True,
                             "from": {"percent": ["repos.claude", "repos"]}},
                   "others": {"value": 0, "from": {"difference": ["repos", "repos.claude"]}},
                   "both": {"value": 0, "from": {"sum": ["repos", "repos.claude"]}}}
        self.document("docs/b.md", f"{marked('share', '0')}, {marked('others', '0')}, "
                                   f"{marked('both', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertIn(f"{marked('share', '75.0%')}, {marked('others', '1')}, "
                      f"{marked('both', '7')}", self.text("docs/b.md"))

    def test_a_share_of_nothing_cannot_be_computed(self):
        """A share with a base of 0 is an error that names the share rather than a traceback."""
        entries = {**self.entries,
                   "none": {"value": 0, "from": {"rows": "data/repos.csv",
                                                 "where": {"tool": "cursor"}}},
                   "share": {"value": 0, "from": {"percent": ["repos", "none"]}}}
        self.document("docs/b.md", f"{marked('share', '0')} and {marked('none', '0')}.\n")
        for argv in (["--check"], []):
            with self.subTest(argv=argv):
                code, err = self.run_tool(*argv, entries=entries)
                self.assertEqual(code, 1)
                self.assertIn("ERROR statistics: The statistic share is a share of none, which is 0",
                              err)

    def test_a_pointer_reads_a_key_with_a_slash_or_a_tilde(self):
        """RFC 6901 writes `/` in a key as `~1` and `~` as `~0`."""
        self.write("docs/keys.json", json.dumps({"a/b": 5, "c~d": 6}))
        entries = {**self.entries,
                   "slash": {"value": 0, "from": {"json": "docs/keys.json", "pointer": "/a~1b"}},
                   "tilde": {"value": 0, "from": {"json": "docs/keys.json", "pointer": "/c~0d"}}}
        self.document("docs/b.md", f"{marked('slash', '0')} and {marked('tilde', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertIn(f"{marked('slash', '5')} and {marked('tilde', '6')}", self.text("docs/b.md"))

    def test_the_check_passes_only_when_writing_would_change_nothing(self):
        """A share of whole counts is a float. A value stored as the integer that it renders as
        fails the check, because the command would rewrite the value as a float."""
        entries = {**self.entries,
                   "half": {"value": 50, "from": {"percent": ["one", "two"]}},
                   "one": {"value": 1, "from": {"rows": "data/repos.csv",
                                                "where": {"tool": "codex"}}},
                   "two": {"value": 2, "from": {"constant": "module.py", "name": "COLUMNS",
                                                "length": True}}}
        self.document("docs/b.md", f"{marked('half', '50')} of them.\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 2)
        self.assertIn("statistics.json records half as 50, but the data gives 50", err)
        self.assertEqual(self.run_tool(entries=entries)[0], 0)
        self.assertEqual(self.stored()["half"]["value"], 50.0)
        self.assertIsInstance(self.stored()["half"]["value"], float)
        self.assertEqual(self.run_tool("--check", entries=self.stored())[0], 0)

    def test_a_plain_value_has_no_thousands_separator(self):
        self.write("ports.py", "PORT = 8765\n")
        entries = {**self.entries, "port": {"value": 0, "plain": True, "from": {
            "constant": "ports.py", "name": "PORT"}}}
        self.document("docs/b.md", f"Port {marked('port', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertIn(marked("port", "8765"), self.text("docs/b.md"))

    def test_a_missing_source_file_cannot_be_checked(self):
        (self.root / "data/repos.csv").unlink()
        code, err = self.run_tool("--check")
        self.assertEqual(code, 1)
        self.assertIn("Could not read ", err)

    def test_a_statistic_needs_a_value_and_a_source_that_computes_it(self):
        """A value typed into the file, with a text that says where it came from, would state a
        number that no data computes."""
        cases = {
            "needs a value and a source under the key from": {
                "memory": {"value": 13.6, "decimals": 1, "source": "peak memory on 2026-10-07"}},
            "has the keys ['source'], which no statistic has": {
                "repos": {"value": 4, "source": "x", "from": {"rows": "data/repos.csv"}}}}
        for message, entries in cases.items():
            with self.subTest(message=message):
                self.document("docs/b.md", f"{marked(next(iter(entries)), '4')}\n")
                code, err = self.run_tool("--check", entries=entries)
                self.assertEqual(code, 1)
                self.assertIn(message, err)


class TestComputedSources(_Checkout):
    """The sources that compute a value from a file, a configuration file, a vendor page, a
    computation of `agentcontext.statistics`, or the data root of a handover."""

    def test_a_value_of_an_earlier_state_is_read_from_the_record_of_values(self):
        """A statistic of a commit of the private repository needs no history, because the record
        of values keeps the value with the commit that it came from."""
        self.write("docs/history/values-at-commits.json", json.dumps(
            {"values": {"then": {"value": 7, "commit": "0" * 40, "source": {"rows": "data/a.csv"}}}}))
        entries = {**self.entries, "then": {"value": 0, "from": {
            "json": "docs/history/values-at-commits.json", "pointer": "/values/then/value"}}}
        self.document("docs/b.md", f"{marked('then', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["then"]["value"], 7)

    def test_a_source_at_a_commit_is_no_source(self):
        """A repository without the history has no commit to read a file from, and the check must
        not try to resolve one."""
        entries = {**self.entries, "then": {"value": 0, "from": {"rows": "data/repos.csv",
                                                                  "at": "0" * 40}}}
        self.document("docs/b.md", f"{marked('then', '0')}.\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("unknown source keys", err)

    def test_the_record_of_values_needs_no_commit_of_this_repository(self):
        """Every value of the record of the checkout is read through its pointer, and no entry of
        the statistics file reads a file at a commit."""
        record = json.loads((REPO_ROOT / "docs/history/values-at-commits.json").read_text(
            encoding="utf-8"))
        stored = json.loads((REPO_ROOT / "docs/statistics.json").read_text(encoding="utf-8"))
        readers = {key: entry for key, entry in stored.items()
                   if entry["from"].get("json") == "docs/history/values-at-commits.json"}
        self.assertEqual(set(readers), set(record["values"]))
        for key, entry in readers.items():
            with self.subTest(key=key):
                self.assertEqual(entry["from"]["pointer"], f"/values/{key}/value")
                self.assertEqual(entry["value"], record["values"][key]["value"])
                self.assertRegex(record["values"][key]["commit"], r"^[0-9a-f]{8,40}$")
        self.assertEqual([key for key, entry in stored.items() if "at" in entry["from"]], [])

    def test_a_yaml_value_is_read_by_its_pointer(self):
        self.write("config/download.yaml", "window:\n  max_crawl_lag_days: 30\n")
        entries = {**self.entries, "lag": {"value": 0, "from": {
            "yaml": "config/download.yaml", "pointer": "/window/max_crawl_lag_days"}}}
        self.document("docs/b.md", f"{marked('lag', '0')} days.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["lag"]["value"], 30)

    def test_a_vendor_value_is_the_number_of_its_quote(self):
        """A limit that a vendor states is read from the verbatim quote of its page, which the
        documentation watch checks, and a quote with several numbers needs a pattern."""
        entries = {**self.entries,
                   "limit": {"value": 0, "from": {
                       "vendor": "https://docs.example.com/search",
                       "quote": "the REST API provides up to 1,000 results for each search"}},
                   "hourly": {"value": 0, "from": {
                       "vendor": "https://docs.example.com/limits",
                       "quote": "a limit of 5,000 requests per hour, or 15,000 for an app",
                       "pattern": r"limit of ([\d,]+) requests"}},
                   "timeout": {"value": 0, "decimals": 1, "from": {
                       "vendor": "https://docs.example.com/timeouts",
                       "quote": "Requests time out after 2.5 seconds."}},
                   "storage": {"value": 0, "decimals": 2, "from": {
                       "vendor": "https://docs.example.com/storage",
                       "quote": "Each repository stores up to 1,000.75 MB."}}}
        self.document("docs/b.md", " ".join(marked(key, "0") for key in
                                            ("limit", "hourly", "timeout", "storage")) + "\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual([self.stored()[key]["value"] for key in
                          ("limit", "hourly", "timeout", "storage")], [1000, 5000, 2.5, 1000.75])
        del entries["hourly"]["from"]["pattern"]
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("The quote of the statistic hourly states 2 numbers", err)

    def test_products_and_quotients_follow_their_operands(self):
        entries = {**self.entries,
                   "twice": {"value": 0, "from": {"product": ["repos", "repos.claude"]}},
                   "ratio": {"value": 0, "decimals": 2,
                             "from": {"quotient": ["repos.claude", "repos"]}}}
        self.document("docs/b.md", f"{marked('twice', '0')} and {marked('ratio', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual((self.stored()["twice"]["value"], self.stored()["ratio"]["value"]),
                         (12, 0.75))

    def test_a_function_source_runs_its_computation_over_the_checkout(self):
        self.write("data/commits.csv",
                   "repo_name,commit_timestamp,commit_sha,commit_message,ai_tool,"
                   "matched_heuristics,attributions\n"
                   "o/a,2026-01-01T00:00:00Z,1,Fix,Claude,claude-trailer-01,Claude:standard\n"
                   "o/a,2026-01-02T00:00:00Z,2,Add,Claude,claude-author-02;claude-trailer-01,"
                   "Claude:account\n"
                   "o/b,2026-01-03T00:00:00Z,3,Fix it,Codex,codex-author-01,Codex:account\n")
        self.write("heuristics/ai_commit_patterns.csv", "id,tool\nclaude-trailer-01,Claude\n"
                                                        "claude-author-02,Claude\n"
                                                        "codex-author-01,Codex\n")
        self.write("heuristics/other_ai_commit_patterns.csv", "id,tool\n")
        entries = {**self.entries,
                   "columns": {"value": 0, "from": {"function": "tables.columns",
                                                    "args": {"path": "data/repos.csv"}}},
                   "matched": {"value": 0, "from": {"function": "tables.rows", "args": {
                       "path": "data/repos.csv", "match": {"reason": "^y$"},
                       "distinct": "tool"}}},
                   "alone": {"value": 0, "from": {"function": "commits.count", "args": {
                       "tables": ["data/commits.csv"], "only": ["claude-trailer-01"],
                       "distinct": "repository"}}},
                   "fixes": {"value": 0, "from": {"function": "commits.count", "args": {
                       "tables": ["data/commits.csv"], "message": "^Fix"}}}}
        self.document("docs/b.md", " ".join(marked(key, "0") for key in
                                            ("columns", "matched", "alone", "fixes")) + "\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual([self.stored()[key]["value"] for key in
                          ("columns", "matched", "alone", "fixes")], [3, 2, 1, 2])

    def test_a_literal_is_read_from_the_text_of_a_file_that_keeps_it(self):
        """A file that changes only at a cost keeps its literal, and the statistic reads it once
        from the text, failing when the code no longer writes it once."""
        self.write("cost.py", "def bold(phi):\n    return abs(phi) >= 0.3\n")
        entries = {**self.entries, "bold": {"value": 0, "from": {"function": "code.literal", "args": {
            "path": "cost.py", "pattern": r"abs\(phi\) >= ([\d.]+)"}}}}
        self.document("docs/b.md", f"{marked('bold', '0')}.\n")
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["bold"]["value"], 0.3)
        self.write("cost.py", "def bold(phi):\n    return abs(phi) >= 0.3 or abs(phi) >= 0.5\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("matches cost.py 2 times, not once", err)

    def test_an_unknown_computation_cannot_be_computed(self):
        for name in ("nothing.here", "tables.nothing", "../x.y"):
            with self.subTest(name=name):
                entries = {**self.entries, "x": {"value": 0, "from": {"function": name}}}
                self.document("docs/b.md", f"{marked('x', '0')}.\n")
                code, err = self.run_tool("--check", entries=entries)
                self.assertEqual(code, 1)

    def collection(self):
        """A data root with a snapshot and the workflow files of two repositories, and the
        statistic that counts the workflows that run the Claude Code Base Action."""
        self.write("data/collection/output/snapshot.json", '{"collection": 1}\n')
        self.write("data/collection/data/workflows/o\u00a7a/claude.yml",
                   "jobs:\n  x:\n    steps:\n      - uses: anthropics/claude-code-base-action@v1\n")
        self.write("data/collection/data/workflows/o\u00a7b/ci.yml", "jobs: {}\n")
        return {**self.entries, "base_action": {"value": 0, "from": {
            "collection": "handover.files",
            "args": {"pattern": "collection/data/workflows/*/*.yml",
                     "contains": "uses: anthropics/claude-code-base-action"}}}}

    def collect(self, entries, archive=""):
        """Run --collection over the data root of the checkout with the statistics `entries`:
        by default with the archive of a handover, without --archive when `archive` is None, and
        otherwise with the path `archive`. Returns the exit code and the log."""
        self.write("docs/statistics.json", json.dumps(entries, indent=2) + "\n")
        if archive == "":
            archive = self.write("downloads/handover-1.7z", "archive bytes")
        argv = () if archive is None else ("--archive", str(archive))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = statistics.main(["--collection", "--root", str(self.root),
                                    "--data-root", str(self.root / "data"), *argv])
        return code, err.getvalue()

    def test_a_collection_value_is_computed_over_the_data_root_and_stored(self):
        """The import of a handover computes the values that only its data root holds, and the
        checkout states them from the collection statistics file as long as they belong to its
        snapshot."""
        entries = self.collection()
        self.document("docs/b.md", f"{marked('base_action', '0')} workflows.\n")
        code, err = self.collect(entries)
        self.assertEqual(code, 0, err)
        recorded = json.loads((self.root / "data/collection/collection_statistics.json").read_text())
        self.assertEqual(list(recorded["values"].values())[0]["value"], 1)
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.stored()["base_action"]["value"], 1)
        self.assertIn(marked("base_action", "1"), self.text("docs/b.md"))

    def test_the_collection_statistics_file_records_the_handover_of_the_data_root(self):
        """A count of the collection traces back through the collection statistics file to the
        archive that the working copy was extracted from, which a document can name by its
        SHA-256."""
        entries = {**self.collection(),
                   "handover.bytes": {"value": 0, "from": {
                       "json": "data/collection/collection_statistics.json",
                       "pointer": "/derived_from/handover/bytes"}}}
        self.document("docs/b.md", f"{marked('base_action', '0')} workflows of a handover of "
                                   f"{marked('handover.bytes', '0')} bytes.\n")
        code, err = self.collect(entries)
        self.assertEqual(code, 0, err)
        recorded = json.loads((self.root / "data/collection/collection_statistics.json").read_text())
        self.assertEqual(recorded["derived_from"]["handover"], {
            "archive": "handover-1.7z", "bytes": 13,
            "sha256": hashlib.sha256(b"archive bytes").hexdigest()})
        code, err = self.run_tool(entries=entries)
        self.assertEqual(code, 0, err)
        self.assertIn(marked("handover.bytes", "13"), self.text("docs/b.md"))

    def test_the_collection_statistics_file_is_written_only_with_its_handover(self):
        """The documents state the handover from the collection statistics file, so a file
        written without the archive would trace its values to no handover, and an archive
        without --collection would be ignored."""
        entries = self.collection()
        self.document("docs/b.md", f"{marked('base_action', '0')} workflows.\n")
        statistics_path = self.root / "data/collection/collection_statistics.json"
        cases = {"--collection needs --archive": None,
                 "is not a file": self.root / "downloads/missing.7z"}
        for message, archive in cases.items():
            with self.subTest(message=message):
                code, err = self.collect(entries, archive)
                self.assertEqual(code, 1)
                self.assertIn(message, err)
                self.assertFalse(statistics_path.exists())
        code, err = self.run_tool("--archive", str(self.root / "downloads/handover-1.7z"),
                                  entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("--archive applies only with --collection", err)

    def test_a_collection_value_of_another_snapshot_or_other_code_is_a_difference(self):
        """A regeneration rewrites the snapshot, and a value computed before it, or by other
        code, would state the collection that the checkout no longer tracks."""
        entries = self.collection()
        self.document("docs/b.md", f"{marked('base_action', '1')} workflows.\n")
        self.assertEqual(self.collect(entries)[0], 0)
        statistics_path = self.root / "data/collection/collection_statistics.json"
        stored = statistics_path.read_text()
        self.write("data/collection/output/snapshot.json", '{"collection": 2}\n')
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 2)
        self.assertIn("was computed over another snapshot", err)
        self.write("data/collection/output/snapshot.json", '{"collection": 1}\n')
        recorded = json.loads(stored)
        for item in recorded["values"].values():
            item["code"] = "0" * 64
        statistics_path.write_text(json.dumps(recorded))
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 2)
        self.assertIn("computed by the current code of handover.files", err)
        statistics_path.unlink()
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 2)
        self.assertIn("cannot be read", err)

    def test_a_source_names_a_computation_of_its_own_kind(self):
        """A computation over the checkout in a collection source would be stored as a value of
        the handover, and a computation over a data root in a function source would read the
        checkout as one."""
        entries = {**self.collection(),
                   "wrong": {"value": 0, "from": {"collection": "tables.columns",
                                                  "args": {"path": "data/repos.csv"}}}}
        self.document("docs/b.md", f"{marked('base_action', '1')} {marked('wrong', '0')}\n")
        code, err = self.collect(entries)
        self.assertEqual(code, 1)
        self.assertIn("which reads the checkout, in a collection source", err)
        entries = {**self.entries, "wrong": {"value": 0, "from": {
            "function": "handover.files", "args": {"pattern": "*"}}}}
        self.document("docs/b.md", f"{marked('wrong', '0')}\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 1)
        self.assertIn("which reads a data root, in a function source", err)


class TestDocuments(_Checkout):

    def test_a_marker_whose_key_the_file_lacks_is_a_difference_writing_does_not_fix(self):
        self.document("docs/b.md", f"{marked('nothing', '7')} things.\n")
        for argv in (["--check"], []):
            with self.subTest(argv=argv):
                code, err = self.run_tool(*argv)
                self.assertEqual(code, 2)
                self.assertIn("docs/b.md marks a number with the key nothing, which is not in "
                              "statistics.json", err)
        self.assertIn(marked("nothing", "7"), self.text("docs/b.md"))

    def test_a_marker_that_does_not_parse_is_a_difference_writing_does_not_fix(self):
        """A key with a hyphen, a form that does not exist, a number over a line break and an
        opening or closing marker without the other half are differences. Read as prose, their
        numbers would never be checked."""
        text = ("<!--s:b-count-->7<!--/s--> and <!--s:repos:word-->4<!--/s--> and "
                "<!--s:repos.claude-->\n3<!--/s--> and 4<!--/s--> left.\n")
        self.document("docs/b.md", text)
        for argv in (["--check"], []):
            with self.subTest(argv=argv):
                code, err = self.run_tool(*argv)
                self.assertEqual(code, 2)
                for opening in ("<!--s:b-count-->", "<!--s:repos:word-->",
                                "<!--s:repos.claude-->"):
                    self.assertIn(f"docs/b.md contains {opening}, which does not parse as a "
                                  f"marker", err)
                self.assertEqual(err.count("docs/b.md contains <!--/s-->, which does not parse "
                                           "as a marker"), 4)
        self.assertEqual(self.text("docs/b.md"), text)

    def test_a_statistic_no_document_states_is_a_difference(self):
        entries = {**self.entries, "unused": {"value": 1, "from": {"rows": "data/repos.csv",
                                                                   "where": {"tool": "codex"}}}}
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 2)
        self.assertIn("No document states the statistic unused", err)

    def test_a_statistic_another_is_computed_from_is_stated_through_it(self):
        entries = {"repos": self.entries["repos"], "repos.claude": self.entries["repos.claude"],
                   "other": {"value": 1, "from": {"difference": ["repos", "repos.claude"]}}}
        self.document("docs/a.md", f"{marked('other', '1')} repository has no Claude.\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 0, err)

    def test_a_number_outside_a_marker_is_a_difference(self):
        self.document("docs/b.md", "The sample holds 36,710 repositories, 90.1% of them.\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("docs/b.md states the number 36,710 without a marker", err)
        self.assertIn("docs/b.md states the number 90.1% without a marker", err)

    def test_the_changelog_is_documentation_until_its_first_release(self):
        """The check treats `## [Unreleased]` as a document, because the section states statistics
        of the version in preparation. A released section stays as it was published."""
        self.document("CHANGELOG.md", "# Changelog\n\n## [Unreleased]\n\n"
                                      f"- {marked('repos', '9')} repositories of 36,710.\n\n"
                                      "## [AgentTorrent 1.0.0] - 2026-11-12\n\n"
                                      f"- {marked('repos', '9')} repositories of 36,710.\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("CHANGELOG.md writes repos as 9, but statistics.json gives 4", err)
        self.assertEqual(err.count("CHANGELOG.md states the number 36,710 without a marker"), 1)
        self.run_tool()
        self.assertEqual(self.text("CHANGELOG.md"),
                         "# Changelog\n\n## [Unreleased]\n\n"
                         f"- {marked('repos', '4')} repositories of 36,710.\n\n"
                         "## [AgentTorrent 1.0.0] - 2026-11-12\n\n"
                         f"- {marked('repos', '9')} repositories of 36,710.\n")

    def test_code_urls_tags_and_the_history_are_not_stated_statistics(self):
        self.document("docs/b.md", "Run `head -n 1,000 x` or see https://x.org/a?n=1,000.\n\n"
                                   "```\n36,710 rows\n```\n<td width=\"50%\">x</td>\n")
        self.document("docs/history/note.md", "We measured 36,710 repositories.\n")
        self.write("docs/untracked.md", "We measured 36,710 repositories.\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 0, err)

    def test_a_number_in_a_quotation_of_a_vendor_page_takes_a_marker(self):
        """A number that a vendor page states comes from a `vendor` source, which the
        documentation watch checks on the page. In a quotation without a marker, the number is
        still a difference, and with a marker the quotation passes."""
        self.document("docs/b.md", "GitHub's page says \"a limit of 5,000 requests per hour\" "
                                   "and \"up to 90% of the limit\".\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("docs/b.md states the number 5,000 without a marker", err)
        self.assertIn("docs/b.md states the number 90% without a marker", err)
        entries = {**self.entries, "hourly": {"value": 5000, "from": {
            "vendor": "https://docs.example.com/limits",
            "quote": "a limit of 5,000 requests per hour"}}}
        self.document("docs/b.md", f"GitHub's page says \"a limit of {marked('hourly', '5,000')} "
                                   f"requests per hour\".\n")
        code, err = self.run_tool("--check", entries=entries)
        self.assertEqual(code, 0, err)

    def test_a_quotation_of_the_maintainer_states_no_statistic(self):
        """The maintainer's words come from no source that computes a number, and a marker would
        let the command rewrite them. A quotation of the maintainer ends at its closing marker,
        and not over a blank line. A URL in it does not take the closing marker, and a code span
        that names a marker opens no quotation, so the numbers outside are still checked."""
        self.document("docs/b.md",
                      "The maintainer asked <!--maintainer-->\"where does the value 262,144 "
                      "tokens\ncome from?\"<!--/maintainer--> of 36,710 repositories, "
                      "<!--maintainer-->\"see https://x.org/a\"<!--/maintainer--> for 1,234 "
                      "more, and <!--maintainer-->\"send \"high\" for 2,048 tokens\""
                      "<!--/maintainer-->.\n\n"
                      "An unclosed <!--maintainer-->\"quote\n\nholds 4,321 repositories\""
                      "<!--/maintainer-->.\n\n"
                      "Write `<!--maintainer-->\"` and 8,765 rows `\"<!--/maintainer-->`.\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        for number in ("36,710", "1,234", "4,321", "8,765"):
            self.assertIn(f"docs/b.md states the number {number} without a marker", err)
        self.assertNotIn("262,144", err)
        self.assertNotIn("2,048", err)

    def test_a_comparison_sign_hides_no_number(self):
        """A `<` in prose is a comparison, not the start of an HTML tag, so the number after it
        is still checked."""
        self.document("docs/b.md", "A file of < 100 MB was read by 36,710 repositories.\n\n"
                                   "The ratio is > 2.\n\n<!-- 1,234 in a comment -->\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("docs/b.md states the number 36,710 without a marker", err)
        self.assertNotIn("1,234", err)

    def test_a_plan_is_documentation(self):
        """A paper takes its numbers from a plan, so the check holds the numbers of `_plans/` to
        the data as it holds those of every other document."""
        self.document("_plans/plan.md", f"We measured 36,710 repositories, "
                                        f"{marked('repos', '9')} of them configured.\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("_plans/plan.md states the number 36,710 without a marker", err)
        self.assertIn("_plans/plan.md writes repos as 9, but statistics.json gives 4", err)
        self.run_tool()
        self.assertIn(marked("repos", "4"), self.text("_plans/plan.md"))

    def test_a_marker_shown_as_code_is_an_example_and_not_a_stated_statistic(self):
        self.document("docs/b.md", f"Mark a statistic as `{marked('unknown', '36,710')}`.\n\n"
                                   f"```\n{marked('repos', '9')}\n```\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 0, err)
        self.run_tool()
        self.assertIn(marked("repos", "9"), self.text("docs/b.md"))

    def test_a_code_span_over_a_line_break_ends_where_its_backtick_does(self):
        self.document("docs/b.md", f"A trailer `co-authored-by:\n  ubuntu` in "
                                   f"{marked('repos', '4')} repositories, `x`, after `` ` `` "
                                   f"in {marked('repos.claude', '3')}, `y`.\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 0, err)

    def test_the_release_notes_of_the_website_are_documentation(self):
        self.document("_website/index.html", "<style>div { width: 100%; }</style>\n"
                                             "<p>36,710 repositories</p>\n")
        code, err = self.run_tool("--check")
        self.assertEqual(code, 2)
        self.assertIn("_website/index.html states the number 36,710 without a marker", err)
        self.assertNotIn("100%", err)

    def test_every_line_is_in_the_log_format_under_the_command(self):
        code, err = self.run_tool("--check")
        self.assertEqual(code, 0, err)
        for line in err.strip().splitlines():
            self.assertRegex(line, r"^(INFO|WARNING|ERROR) statistics: ")


class _Computations(unittest.TestCase):
    """The computations of one module of `agentcontext.statistics`, each run over small files in
    a git repository in a temporary directory, with counts that follow from their rows."""

    HEADER = ("repo_name,commit_timestamp,commit_sha,commit_message,ai_tool,matched_heuristics,"
              "attributions\n")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative, data):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            path.write_bytes(data)
        else:
            path.write_text(data, encoding="utf-8")
        return path

    def jsonl(self, relative, rows):
        """Write `rows` as a compressed JSON Lines file."""
        self.write(relative, lzma.compress("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                                   for row in rows).encode("utf-8")))

    def archive(self, relative, text, name):
        """Write `text` as the one file `name` of the 7z archive at `relative`."""
        source = self.write(f"work/{name}", text)
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([SEVEN_ZIP, "a", "-bso0", "-bsp0", str(target.with_suffix("")
                        if relative.endswith(".001") else target), str(source)]
                       + (["-v1m"] if relative.endswith(".001") else []),
                       cwd=self.root, check=True)
        source.unlink()

    def heuristics(self, rows, other_rows=""):
        """Write both commit heuristics files, with `id,tool` rows."""
        self.write("heuristics/ai_commit_patterns.csv", "id,tool\n" + rows)
        self.write("heuristics/other_ai_commit_patterns.csv", "id,tool\n" + other_rows)

    def value(self, computation, /, under="", **args):
        """The value of a computation over the files under `under`."""
        return call(computation, args, Files(self.root / under))


class TestFiles(_Computations):
    """`Files` reads the files of the checkout, whole or compressed."""

    def test_a_directory_is_no_file(self):
        """A size source that names a directory by mistake fails rather than stating a size."""
        self.write("data/rows.txt", "one\n")
        files = Files(self.root)
        self.assertFalse(files.exists("data"))
        self.assertTrue(files.exists("data/rows.txt"))
        with self.assertRaises(Unavailable):
            files.size("data")
        self.assertEqual(files.size("data/rows.txt"), 4)

    def test_a_json_lines_file_is_parsed_once(self):
        self.jsonl("new/rows.jsonl.xz", [{"a": 1}])
        files = Files(self.root)
        self.assertIs(files.json_lines("new/rows.jsonl.xz"), files.json_lines("new/rows.jsonl.xz"))

    def test_the_lines_of_a_compressed_file_are_those_that_lzma_decompresses(self):
        """A file of several streams reads as all of them, data after the first stream that is
        no stream is left out, and so is a later stream that does not decode, as
        `lzma.decompress` leaves them out. Data that ends inside a stream, the first or a later
        one, cannot be read."""
        text = "".join(json.dumps({"n": n, "t": "a\u2028b" * (n % 7)}, ensure_ascii=False) + "\n"
                       for n in range(20000)).encode("utf-8")
        second = lzma.compress(text[999:])
        # A byte changed in the middle of a stream fails its check only after the decompressor
        # has returned the blocks before it.
        corrupt = second[:len(second) // 2] + bytes([second[len(second) // 2] ^ 0xFF]) \
            + second[len(second) // 2 + 1:]
        cases = {"one.jsonl.xz": lzma.compress(text),
                 "two.jsonl.xz": lzma.compress(text[:999]) + second,
                 "padded.jsonl.xz": lzma.compress(text) + b"padding",
                 "corrupt.jsonl.xz": lzma.compress(text[:999]) + corrupt}
        for name, data in cases.items():
            self.write(name, data)
        files = Files(self.root)
        # Small steps make the decompressor return blocks of a stream before its end.
        with mock.patch.object(computations, "_XZ_STEP", 512):
            for name, data in cases.items():
                with self.subTest(name=name):
                    self.assertEqual(b"".join(files.lines(name)), lzma.decompress(data))
            for name in ("cut.jsonl.xz", "cut-second.jsonl.xz"):
                with self.subTest(name=name):
                    data = (lzma.compress(text)[:4000] if name == "cut.jsonl.xz"
                            else lzma.compress(text[:999]) + second[:len(second) // 2])
                    self.write(name, data)
                    with self.assertRaises(lzma.LZMAError):
                        lzma.decompress(data)
                    with self.assertRaises(Unavailable):
                        files.bytes(name)
                    with self.assertRaises(Unavailable):
                        list(files.lines(name))
        self.assertEqual(len(list(files.jsonl("two.jsonl.xz"))), 20000)

class TestTables(_Computations):
    """`tables` selects the records of a CSV, a JSON file or a JSON Lines file by their columns,
    by other files and by comparisons, and aggregates a column."""

    def test_compare_counts_the_rows_in_which_columns_agree(self):
        self.write("data/labels.csv", "repo,first,second,human\no/a,true,false,true\n"
                                      "o/b,true,false,false\no/c,false,true,unclear\n")
        self.assertEqual(self.value("tables.rows", path="data/labels.csv",
                                    compare={"human": ["==", {"column": "first"}]}), 1)
        self.assertEqual(self.value("tables.rows", path="data/labels.csv",
                                    compare={"human": ["==", {"column": "second"}]},
                                    where={"first": "true"}), 1)

    def test_compare_reads_numbers_constants_and_quotients(self):
        """A text that is a number compares as the number, an empty value fails an ordering
        comparison, and `per` divides the compared column by another."""
        self.write("data/sizes.csv", "repo_name,size_kb\no/a,10\no/b,\no/c,300\n")
        self.assertEqual(self.value("tables.rows", path="data/sizes.csv",
                                    compare={"size_kb": [">", 100]}), 1)
        self.write("data/review.json", json.dumps({"files": {
            "a": {"now": "2", "then": "3"}, "b": {"now": "5", "then": "5"},
            "c": {"now": "10", "then": "9"}}}))
        counts = [self.value("tables.rows", path="data/review.json", pointer="/files",
                             compare={"now": [relation, {"column": "then"}]})
                  for relation in ("<", "==", ">")]
        self.assertEqual(counts, [1, 1, 1])
        self.write("module.py", "CHARS_PER_TOKEN = 3.5\n")
        self.write("data/readmes.json", json.dumps([{"chars": 30, "tokens": 10},
                                                    {"chars": 34, "tokens": 10},
                                                    {"chars": 40, "tokens": 10}]))
        self.assertEqual(self.value("tables.rows", path="data/readmes.json", per="tokens",
                                    compare={"chars": ["<", {"constant": "module.py",
                                                             "name": "CHARS_PER_TOKEN"}]}), 2)
        self.write("data/forks.json", json.dumps({"forks": [{"independent": True},
                                                            {"independent": False}]}))
        self.assertEqual(self.value("tables.rows", path="data/forks.json", pointer="/forks",
                                    where={"independent": True}), 1)

    def test_a_column_that_the_csv_lacks_cannot_be_counted(self):
        self.write("data/repos.csv", "repo_name,tool\no/a,claude\n")
        with self.assertRaises(Unavailable):
            self.value("tables.rows", path="data/repos.csv", where={"nothing": "x"})

    def test_rows_absent_from_or_present_in_other_csvs_are_counted_by_their_key(self):
        self.write("data/repos.csv", "repo_name,tool,reason\no/a,claude,x\no/b,claude,y\n"
                                     "o/c,codex,y\no/d,claude,y\n")
        self.write("data/listed.csv", "repo_name,path\no/a,CLAUDE.md\no/c,AGENTS.md\n")
        self.assertEqual(self.value("tables.rows", path="data/repos.csv",
                                    absent_from=["data/listed.csv"], where={"tool": "claude"}), 2)
        self.write("data/collection/file_history.csv",
                   "repo_name,head_sha,mechanism,artifact\no/a,h1,,A.md\no/a,h1,skills,A.md\n"
                   "o/a,h0,,B.md\no/b,h2,,C.md\n")
        self.write("data/collection/data/collected/repos.csv",
                   "repo_name,head_sha\no/a,h1\no/b,h2\n")
        self.assertEqual(self.value("tables.rows", path="data/collection/file_history.csv",
                                    where={"mechanism": ""}, key=["repo_name", "head_sha"],
                                    present_in=["data/collection/data/collected/repos.csv"]), 2)

    def test_the_repositories_that_one_kind_of_row_alone_puts_into_the_outputs(self):
        """A repository counts when the selected rows are all that the CSVs hold for it, and
        the rows of a JSON file sum over its entries."""
        self.write("out/context-files.csv", "repo_name,context_file\no/a,agents.md\n"
                                            "o/b,agents.md\no/b,AGENTS.md\no/c,Claude.md\n")
        self.write("out/skills.csv", "repo_name,skill\no/c,s\n")
        self.assertEqual(self.value("tables.rows", path="out/context-files.csv",
                                    match={"context_file": "^(?!AGENTS\\.md$)(?i:agents\\.md)$|"
                                                           "^Claude\\.md$"},
                                    alone=True, absent_from=["out/skills.csv"],
                                    distinct="repo_name"), 1)
        self.write("out/baseline.json", json.dumps({"a": {"rows": 3}, "b": {"rows": 4},
                                                    "c": {"columns": []}}))
        self.assertEqual(self.value("tables.aggregate", path="out/baseline.json", column="rows",
                                    how="sum"), 7)

    def test_a_line_of_json_lines_ends_only_at_a_newline(self):
        """A text field that holds a line separator such as U+2028 stays in its line."""
        self.write("runs.jsonl", '{"text": "a b", "seconds": 3}\n'
                                 '{"text": "c", "seconds": 5, "kind": "x"}\n{"text": "d"}\n')
        self.assertEqual(self.value("tables.aggregate", path="runs.jsonl", column="seconds",
                                    how="max"), 5)
        self.assertEqual(self.value("tables.aggregate", path="runs.jsonl", column="seconds",
                                    how="sum", where={"kind": "x"}), 5)

class TestCommits(_Computations):
    """`commits.count` reads both commit CSVs as one set of commits, and its conditions read the
    heuristics, the attributions and the message of each commit."""

    def commit_csvs(self):
        """Commit 1 of o/a credits Claude and OpenClaw, a row in each CSV, so that a count over
        one CSV alone would miss what the other row says about it."""
        self.write("data/all.csv", self.HEADER +
                   "o/a,2026-01-01T00:00:00Z,1,Fix,Claude,claude-trailer-01,Claude:standard\n"
                   "o/a,2026-01-02T00:00:00Z,2,Add,Claude,claude-trailer-01;claude-full-body-02,"
                   "Claude:standard\n"
                   "o/b,2026-01-03T00:00:00Z,3,Fix,Codex,codex-author-01,Codex:account\n")
        self.write("data/other.csv", self.HEADER +
                   "o/a,2026-01-01T00:00:00Z,1,Fix,OpenClaw,openclaw-author-02,OpenClaw:account\n"
                   "o/c,2026-01-04T00:00:00Z,4,Add,OpenClaw,openclaw-author-02;openclaw-trailer-01,"
                   "OpenClaw:account;OpenClaw:convention\n")
        self.heuristics("claude-trailer-01,Claude\nclaude-full-body-02,Claude\n"
                        "codex-author-01,Codex\n",
                        "openclaw-author-02,OpenClaw\nopenclaw-trailer-01,OpenClaw\n")

    def counted(self, **args):
        return self.value("commits.count", tables=["data/all.csv", "data/other.csv"], **args)

    def test_a_commit_in_both_csvs_is_one_commit_with_the_credits_of_both_rows(self):
        self.commit_csvs()
        values = [self.counted(tool="OpenClaw"),
                  self.counted(tool="OpenClaw", distinct="repository"),
                  self.counted(heuristics=["openclaw-author-02", "claude-.*"]),
                  self.counted(tool="Claude", alone=["claude-trailer-.*"]),
                  self.counted(tool="OpenClaw", heuristics=["openclaw-author-02"],
                               alone=["openclaw-author-02"]),
                  self.counted(attribution="OpenClaw:account")]
        self.assertEqual(values, [2, 2, 1, 1, 1, 2])

    def test_a_condition_on_the_attributions_of_a_tool_needs_the_tool(self):
        self.commit_csvs()
        with self.assertRaisesRegex(Unavailable, "need the tool"):
            self.counted(attributions=["standard"])

    def test_a_selector_that_fits_no_heuristic_cannot_be_counted(self):
        self.commit_csvs()
        for selector in ("nothing-01", {"where": {"id": "nothing-01"}}):
            with self.subTest(selector=selector), self.assertRaises(Unavailable):
                self.counted(heuristics=[selector])

    def test_a_commit_counts_for_each_tool_it_credits_by_that_tool_s_credits(self):
        self.heuristics("copilot-trailer-07,Copilot\ncopilot-author-01,Copilot\n"
                        "claude-trailer-01,Claude\n")
        self.write("data/commits.csv", self.HEADER +
                   "o/a,t,1,m,\"Claude,Copilot\",claude-trailer-01;copilot-trailer-07,"
                   "Claude:standard;Copilot:reviewer\n"
                   "o/a,t,2,m,Copilot,copilot-author-01;copilot-trailer-07,"
                   "Copilot:account;Copilot:reviewer\n"
                   "o/b,t,3,m,Copilot,copilot-trailer-07,Copilot:reviewer\n"
                   "o/b,t,4,m,Claude,claude-trailer-01,Claude:standard\n")
        self.write("data/labels.csv", "repo_name,commit_sha,label\no/a,1,bot\no/a,9,bot\n"
                                      "o/b,3,person\n")
        tables = {"tables": ["data/commits.csv"]}
        values = {
            "copilot": self.value("commits.count", **tables, tool="Copilot"),
            "reviewer_only": self.value("commits.count", **tables, tool="Copilot",
                                        attributions=["reviewer"]),
            "reviewer_only.repositories": self.value("commits.count", **tables, tool="Copilot",
                                                     attributions=["reviewer"],
                                                     distinct="repository"),
            "trailer_alone": self.value("commits.count", **tables, tool="Copilot",
                                        alone=["copilot-trailer-07"]),
            "of_b": self.value("commits.count", **tables, tool="Claude", repository="o/b",
                               heuristics=["claude-trailer-01"]),
            "labeled": self.value("commits.count", **tables,
                                  labeled={"path": "data/labels.csv", "label": "bot"})}
        self.assertEqual(values, {"copilot": 3, "reviewer_only": 2,
                                  "reviewer_only.repositories": 2, "trailer_alone": 2,
                                  "of_b": 1, "labeled": 1})

    def test_heuristics_alone_credit_a_tool_whatever_credits_the_other_tools(self):
        self.heuristics("claude-trailer-01,Claude\nclaude-author-07,Claude\n"
                        "copilot-trailer-10,Copilot\n")
        self.write("data/commits.csv", self.HEADER +
                   "o/a,t,1,m,Claude,claude-author-07,Claude:convention\n"
                   "o/a,t,2,m,Claude,claude-author-07;claude-trailer-01,Claude:standard\n"
                   "o/b,t,3,m,\"Claude,Copilot\",claude-author-07;copilot-trailer-10,"
                   "Claude:convention;Copilot:standard\n")
        alone = {"alone": ["claude-author-07"], "tables": ["data/commits.csv"]}
        self.assertEqual(self.value("commits.count", **alone), 2)
        self.assertEqual(self.value("commits.count", **alone, tool="Copilot"), 0)
        self.assertEqual(self.value("commits.count", **alone, distinct="repository",
                                    repository="o/b"), 1)
        convention = {"tool": "Claude", "attributions": ["convention"],
                      "tables": ["data/commits.csv"]}
        self.assertEqual(self.value("commits.count", **convention), 2)
        self.assertEqual(self.value("commits.count", **convention, beside=["standard"]), 1)

    def test_a_credit_alone_reads_only_the_heuristics_of_its_own_tool(self):
        """A commit that credits two tools names both in one row, and a heuristic of the other
        tool leaves the credit of the first to its own heuristics."""
        self.heuristics("claude-a,Claude\nclaude-b,Claude\ncopilot-a,Copilot\n")
        self.write("commits.csv",
                   "repo_name,commit_timestamp,commit_sha,commit_message,ai_tool,"
                   "matched_heuristics\n"
                   "o/a,2026-01-01T00:00:00Z,1,x,\"Claude,Copilot\",claude-a;copilot-a\n"
                   "o/a,2026-01-02T00:00:00Z,2,x,Claude,claude-a;claude-b\n"
                   "o/b,2026-01-03T00:00:00Z,3,x,Claude,claude-a\n"
                   "o/b,2026-01-04T00:00:00Z,4,x,Copilot,copilot-a\n")
        tables = {"tables": ["commits.csv"]}
        self.assertEqual(self.value("commits.count", **tables, alone=["claude-a"]), 2)
        self.assertEqual(self.value("commits.count", **tables, alone=["claude-a"],
                                    repository="o/b"), 1)
        self.assertEqual(self.value("commits.count", **tables, alone=["copilot-a"]), 2)

    def test_a_commit_counts_once_for_its_message(self):
        header = "repo_name,commit_timestamp,commit_sha,commit_message,ai_tool,matched_heuristics\n"
        self.write("data/all.csv", header + "o/a,2026-01-01T00:00:00Z,1,short,Claude,c\n"
                                            "o/a,2026-01-01T00:00:00Z,1,short,Copilot,p\n"
                                            "o/b,2026-01-02T00:00:00Z,2,a longer one,Codex,x\n")
        self.write("data/other.csv", header + "o/c,2026-01-03T00:00:00Z,3,longest of all,Pi,q\n")
        tables = {"tables": ["data/all.csv", "data/other.csv"]}
        self.assertEqual(self.value("commits.count", **tables, longer_than=5), 2)
        self.assertEqual(self.value("commits.longest_message", **tables), 14)

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_the_commits_that_rows_selected_by_their_columns_credit(self):
        """A statistic names the heuristic rows by their columns, and counts the commits that
        they match, those that no other row matches, those of a tool, and those that the other
        commit CSV lacks."""
        self.write("heuristics/ai_commit_patterns.csv",
                   "id,tool,scope,notes\nc-1,Claude,trailer,old\nc-2,Claude,trailer,read over X\n"
                   "x-1,Codex,trailer,read over X\n")
        self.write("heuristics/other_ai_commit_patterns.csv",
                   "id,tool,scope,notes\nd-1,Devin,author,app\nn-1,tool not named,trailer,x\n")
        header = "repo_name,commit_timestamp,commit_sha,commit_message,ai_tool,matched_heuristics\n"
        self.archive("data/collection/output/all_ai_commits.csv.7z.001", header +
                     "o/a,t,1,m,Claude,c-1;c-2\no/a,t,2,m,Claude,c-2\n"
                     "o/b,t,3,m,\"Claude,Codex\",c-2;x-1\no/c,t,4,m,Claude,c-1\n",
                     "all_ai_commits.csv")
        self.archive("data/collection/output/other_ai_commits.csv.7z", header +
                     "o/a,t,2,m,Devin,d-1\no/d,t,5,m,Devin,d-1\no/d,t,6,m,tool not named,n-1\n",
                     "other_ai_commits.csv")
        read_over = {"match": {"notes": "read over X"}}
        self.assertEqual(self.value("commits.count", heuristics=[read_over]), 3)
        self.assertEqual(self.value("commits.count", tables=["all"], only=[read_over]), 2)
        self.assertEqual(self.value("commits.count", heuristics=[read_over], tool="Codex"), 1)
        self.assertEqual(self.value("commits.count", heuristics=[read_over],
                                    distinct="repository"), 2)
        self.assertEqual(self.value("commits.count", heuristics=[{"where": {"id": "d-1"}}]), 2)
        self.assertEqual(self.value("commits.count", heuristics=["d-1"], absent_from="all"), 1)
        self.assertEqual(self.value("commits.count", tables=["other"]), 3)


class TestUnmatched(_Computations):

    @unittest.skipUnless(shutil.which(SEVEN_ZIP), "7zz is not on PATH")
    def test_the_unmatched_attributions_that_no_negative_explains(self):
        """The tracked unmatched attributions are read by their pattern, whatever day names
        them, and a row of the negatives explains every token that contains its pattern."""
        self.write("heuristics/not_ai_commits.csv", "pattern,notes\ndependabot,bumps\n")
        self.archive("data/collection/output/all_ai_commits-unmatched-2026-10-26.csv.7z",
                     "kind,token,commits,repos\nauthor,dependabot[bot],9,40\n"
                     "trailer,co-authored-by: root,7,30\ntrailer,co-authored-by: ann,2,2\n",
                     "all_ai_commits-unmatched-2026-10-26.csv")
        self.assertEqual(self.value("unmatched.count"), 2)
        archive = self.root / "data/collection/output/all_ai_commits-unmatched-2026-10-26.csv.7z"
        self.assertEqual(self.value("unmatched.size"), archive.stat().st_size)
        archive.rename(archive.with_name("all_ai_commits-unmatched-2026-11-02.csv.7z"))
        self.assertEqual(self.value("unmatched.size"), archive.with_name(
            "all_ai_commits-unmatched-2026-11-02.csv.7z").stat().st_size)
        self.archive("data/collection/output/all_ai_commits-unmatched-2026-11-30.csv.7z",
                     "kind,token,commits,repos\n", "all_ai_commits-unmatched-2026-11-30.csv")
        for name in ("unmatched.count", "unmatched.size"):
            with self.subTest(name=name), self.assertRaises(Unavailable):
                self.value(name)


class TestLabels(_Computations):
    """`labels` counts the decisions of a run, their tool calls and fork results, the labels of
    a run against the check samples, and the labels that runs or models agree on."""

    def test_fork_results_are_selected_by_conditions_on_their_values(self):
        decisions = [
            {"name": "o/a", "fork": {"own_commits": 1500, "own_authors": 1,
                                     "upstream_repository": None,
                                     "thresholds": {"commit_minimum": 300}}},
            {"name": "o/b", "fork": {"own_commits": 200, "own_authors": 3,
                                     "upstream_repository": None,
                                     "thresholds": {"commit_minimum": 300}}},
            {"name": "o/c", "fork": {"own_commits": None, "own_authors": None,
                                     "upstream_repository": "u/c",
                                     "thresholds": {"commit_minimum": 300}}},
            {"name": "o/d", "fork": None, "fork_of": ""}]
        self.jsonl("data/classification/labels/run.decisions-001.jsonl.xz", decisions)
        values = {
            "single": self.value("labels.decisions", run="run", fork={
                "/own_commits": [">", 1000], "/own_authors": ["<", 2]}),
            "short": self.value("labels.decisions", run="run", fork={
                "/upstream_repository": ["==", None],
                "/own_commits": ["<", {"pointer": "/thresholds/commit_minimum"}]}),
            "missing": self.value("labels.decisions", run="run", fork={
                "/own_commits": ["<", 1000000]}),
            "a": self.value("labels.fork_value", run="run", name="o/a", pointer="/own_commits")}
        self.assertEqual(values, {"single": 1, "short": 1, "missing": 2, "a": 1500})

    def test_the_decisions_of_a_run_are_counted_over_all_its_parts(self):
        refusal = "The README has no such link"

        def tool_call(round_, output="Page summary"):
            return {"tool": "read_linked_page", "round": round_, "output": output}
        first = {"outcome": "labeled", "readme_path": "README.rst", "fork_of": "up/x",
                 "fork": {"upstream_repository": "up/x"}, "limits_reached": ["linked_pages"],
                 "exclusions": {"mirror": {"described_as_mirror": "true"}},
                 "tools": [tool_call(1), tool_call(1), tool_call(1, refusal + ": docs/a.rst")]}
        second = {"outcome": "labeled", "readme_path": "README.md", "fork_of": "",
                  "limits_reached": ["linked_pages", "tool_rounds"],
                  "exclusions": {"mirror": {"described_as_mirror": "false"}},
                  "tools": [tool_call(1) for _ in range(5)]
                  + [tool_call(1, refusal), tool_call(2), {"tool": "get_file_tree", "round": 1}]}
        third = {"outcome": "excluded", "readme_path": "README", "tools": []}
        labels = "data/classification/labels"
        self.jsonl(f"{labels}/run.decisions-001.jsonl.xz", [first, second])
        self.jsonl(f"{labels}/run.decisions-002.jsonl.xz", [third])
        counts = [self.value("labels.decisions", run="run", **conditions) for conditions in (
            {}, {"outcome": "labeled"}, {"outcome": "labeled", "described_as_mirror": True},
            {"fork_of": True}, {"upstream_on_github": True}, {"limit_reached": "linked_pages"},
            {"limit_reached": "tool_rounds"})]
        self.assertEqual(counts, [3, 2, 1, 1, 1, 2, 1])
        pages = {"run": "run", "tool": "read_linked_page", "tool_round": 1,
                 "limit_reached": "linked_pages"}
        self.assertEqual([self.value("labels.calls_per_decision", **pages, how=how)
                          for how in ("min", "max")], [3, 6])
        refused = {"run": "run", "tool": "read_linked_page", "output": "^" + refusal}
        self.assertEqual(self.value("labels.calls", **refused), 2)
        self.assertEqual(self.value("labels.calls", **refused, readme="(?i)\\.RST$"), 1)
        sizes = [len(json.dumps(decision)) + 1 for decision in (first, second, third)]
        self.assertAlmostEqual(self.value("labels.bytes_per_decision", run="run"),
                               sum(sizes) / 3)

    def test_the_tool_calls_and_links_of_the_decisions_of_a_run(self):
        """The decisions of a run are read from all of its numbered files, a line ends only at
        a line feed, and a link of the repository's own file counts only at a ref that is
        neither the decision's revision nor the default branch."""
        missing = '{"available": false, "note": "GitHub returned HTTP 404 for a file"}'
        labels = "data/classification/labels"
        self.jsonl(f"{labels}/r1.decisions-001.jsonl.xz", [
            {"name": "o/A", "revision": "abc", "limits_reached": ["linked_pages"],
             "usage": {"tool_rounds": 2}, "tools": [
                {"tool": "read_linked_page", "output": missing,
                 "arguments": {"url": "https://github.com/o/a/blob/master/X.md"}},
                {"tool": "read_linked_page", "output": missing,
                 "arguments": {"url": "https://github.com/o/a/blob/main/Y.md"}},
                {"tool": "read_linked_page", "output": missing,
                 "arguments": {"url": "https://github.com/o/a/blob/abc/Z.md"}}]}])
        self.jsonl(f"{labels}/r1.decisions-002.jsonl.xz", [
            {"name": "o/b", "revision": "def", "description": "one two", "usage":
             {"tool_rounds": 1}, "tools": [
                {"tool": "get_file_tree", "output": "{}"},
                {"tool": "read_linked_page", "output": missing,
                 "arguments": {"url": "https://github.com/other/b/blob/dev/X.md"}}]}])
        self.jsonl("data/sampling/ghs_retrievals/g1.github.jsonl.xz", [
            {"repository": {"name": "o/a", "defaultBranch": "main", "description": "a b"}},
            {"repository": None}])
        self.assertEqual(self.value("labels.calls", run="r1"), 5)
        self.assertEqual(self.value("labels.calls", run="r1", tool="read_linked_page",
                                    output="HTTP 404", distinct="repository"), 2)
        self.assertEqual(self.value("labels.usage", run="r1", field="tool_rounds"), 3)
        self.assertEqual(self.value("labels.decisions", run="r1", limit_reached="linked_pages"),
                         1)
        self.assertEqual(self.value("labels.calls", run="r1", tool="read_linked_page",
                                    output="GitHub returned HTTP 404", other_ref="g1"), 1)
        with self.assertRaises(Unavailable):
            self.value("labels.calls", run="r2")

    def test_the_runs_that_label_a_repository_differently_are_counted(self):
        labels = "data/classification/labels"
        self.write(f"{labels}/first.labels.csv.xz", lzma.compress(
            b"id,label\n1,true\n2,true\n3,false\n4,false\n5,true\n"))
        self.write(f"{labels}/second.labels.csv.xz", lzma.compress(
            b"id,label\n1,false\n2,false\n3,true\n4,true\n"))
        self.assertEqual(self.value("labels.agreement", alike=False, runs=["first", "second"]),
                         4)

    def test_the_models_of_an_evaluation_agree_on_the_repositories_that_all_label_alike(self):
        self.write("pilot.csv", "id,model,label,sample\n1,m1,true,p\n1,m2,true,p\n"
                                "2,m1,true,p\n2,m2,false,p\n3,m1,true,p\n4,m1,true,q\n"
                                "4,m2,true,q\n")
        alike = {"path": "pilot.csv", "models": ["m1", "m2"], "where": {"sample": "p"}}
        self.assertEqual(self.value("labels.agreement", alike=True, **alike), 1)
        self.assertEqual(self.value("labels.agreement", alike=False, **alike), 1)


def write_retrievals(test):
    """An imported retrieval of March without GitHub's responses, and a retrieval of October
    with them: `o/a` with a new HEAD and GitHub's quoted license, `O/Case` in another case, a
    renamed repository with rows under its old and new name, a repository with two old names,
    `o/gone` removed and `o/missing` not found. Returns the ids of the retrievals."""
    cache = "data/sampling/ghs_retrievals"
    test.write(f"{cache}/index.csv",
               "retrieval_id,outcome,started_at,finished_at,created_max,committed_min,"
               "github_started_at,github_finished_at,crawl_dates\n"
               "2026-03-16T180701Z,imported,2026-03-16T18:00:00Z,2026-03-16T19:00:00Z,"
               "2025-01-01,2025-09-01,,,\n"
               "2026-10-05T100000Z,retrieved,2026-10-05T10:00:00Z,2026-10-05T10:30:00Z,"
               "2025-04-01,2026-01-01,2026-10-05T10:30:00Z,2026-10-05T12:00:00Z,"
               "\"{\"\"C\"\":\"\"2026-10-01\"\",\"\"Go\"\":\"\"2026-10-02\"\"}\"\n")
    bsd = "BSD 3-Clause New or Revised License"
    test.jsonl(f"{cache}/2026-03-16T180701Z.ghs.jsonl.xz", [
        {"name": "o/a", "lastCommitSHA": "a1", "license": bsd, "lastCommit": "2026-02-01"},
        {"name": "O/Case", "lastCommit": "2026-02-01"},
        {"name": "o/gone", "lastCommit": "2025-06-01"}])
    ghs = [{"name": "o/a", "lastCommitSHA": "a1", "license": bsd},
           {"name": "o/case"},
           {"name": "o/old-name", "lastCommitSHA": "n1"},
           {"name": "o/new", "lastCommitSHA": "n1"},
           {"name": "x/old1"}, {"name": "x/old2"},
           {"name": "o/missing"}]

    def found(number, name, **fields):
        return {"status": "found", "repository": {"id": number, "name": name, **fields}}
    github = [found(10, "o/a", headSha="a2", license=bsd.replace("New", '"New"')
                    .replace("Revised", '"Revised"')),
              found(11, "o/case"),
              found(12, "o/new", headSha="n1", createdAt="2025-06-01T00:00:00Z"),
              found(12, "o/new", headSha="n1", createdAt="2025-06-01T00:00:00Z"),
              found(13, "x/now", createdAt="2024-06-01T00:00:00Z"),
              found(13, "x/now", createdAt="2024-06-01T00:00:00Z"),
              {"status": "not_found", "error": "Could not resolve"}]
    test.jsonl(f"{cache}/2026-10-05T100000Z.ghs.jsonl.xz", ghs)
    test.jsonl(f"{cache}/2026-10-05T100000Z.github.jsonl.xz", github)
    return {"new": "2026-10-05T100000Z", "old": "2026-03-16T180701Z"}


class TestRetrievals(_Computations):
    """`retrievals` counts what the index of the cache records, what the rows of a retrieval
    show, what changed between two retrievals and what the requests for crawl dates cost."""

    def retrieval(self, retrieval_id, ghs_rows, github_rows=()):
        cache = "data/sampling/ghs_retrievals"
        self.jsonl(f"{cache}/{retrieval_id}.ghs.jsonl.xz", ghs_rows)
        self.jsonl(f"{cache}/{retrieval_id}.github.jsonl.xz", github_rows)

    def test_the_rows_of_a_retrieval_are_grouped_by_creation_time_name_and_head(self):
        """Rows that share a creation time are a group of the kind that their names and HEADs
        give, a row of its own is no group, and rows with one creation time and HEAD pair up."""
        def row(name, created, head, hour="03"):
            return {"name": name, "createdAt": created, "pushedAt": f"2026-03-01T{hour}:00:00",
                    "updatedAt": "2026-03-01T01:00:00", "lastCommit": "2026-03-01T12:30:00",
                    "lastCommitSHA": head}
        self.retrieval("march", [
            row("o/a", "2020-01-01T01:00:00", "h1"), row("p/a", "2020-01-01T01:00:00", "h2"),
            row("o/B", "2020-01-02T01:00:00", "h3"), row("q/b", "2020-01-02T01:00:00", "h4"),
            row("o/old", "2020-01-03T01:00:00", "h5"), row("o/new", "2020-01-03T01:00:00", "h5"),
            row("o/x", "2020-01-04T01:00:00", "h7"), row("o/y", "2020-01-04T01:00:00", "h8"),
            row("o/z", "2020-01-05T01:00:00", "h9", hour="11")])
        values = {"groups": self.value("retrievals.created_at_groups", retrieval="march"),
                  **{f"groups.{kind}": self.value("retrievals.created_at_groups",
                                                  retrieval="march", kind=kind)
                     for kind in ("same_name", "case_only", "same_head", "unrelated")},
                  "pairs": self.value("retrievals.created_at_head_pairs", retrieval="march"),
                  "hour.min": self.value("retrievals.timestamp_hour", retrieval="march",
                                         how="min"),
                  "hour.max": self.value("retrievals.timestamp_hour", retrieval="march",
                                         how="max")}
        self.assertEqual(values, {"groups": 4, "groups.same_name": 1, "groups.case_only": 1,
                                  "groups.same_head": 1, "groups.unrelated": 1, "pairs": 1,
                                  "hour.min": 1, "hour.max": 12})

    def test_the_index_and_the_responses_of_a_retrieval_give_its_duration_size_and_bound(self):
        """The duration comes from the index, the rows that one file holds from the size of the
        cache file, and the rows created after the window from GitHub's day of creation."""
        self.write("data/sampling/ghs_retrievals/index.csv",
                   "retrieval_id,started_at,finished_at,created_max,item_count\n"
                   "late,2026-10-12T10:00:00Z,2026-10-12T11:30:00Z,2025-04-01,4\n"
                   "small,2026-10-13T10:00:00Z,2026-10-13T10:01:00Z,2025-04-01,6\n")
        self.write("1_sampling/ghs_cache.py", "VOLUME_BYTES = 1_000\n")
        self.retrieval("late", [], [
            {"status": "found", "repository": {"id": 1, "createdAt": "2025-04-02T00:00:01Z"}},
            {"status": "found", "repository": {"id": 2, "createdAt": "2025-04-01T23:59:59Z"}},
            {"status": "found", "repository": {"id": None, "createdAt": "2026-01-01T00:00:00Z"}},
            {"status": "not_found", "repository": None}])
        self.write("data/sampling/ghs_retrievals/small.ghs.jsonl.xz", b"x" * 300)
        values = {"minutes": self.value("retrievals.minutes", retrieval="late"),
                  "per_file": self.value("retrievals.rows_per_file", retrieval="small"),
                  "after": self.value("retrievals.created_after_window", retrieval="late")}
        self.assertEqual(values, {"minutes": 90.0, "per_file": 20, "after": 1})

    def test_languages_of_retrievals_rank_with_the_name_deciding_a_tie(self):
        self.write("data/sampling/ghs_retrievals/index.csv",
                   "retrieval_id,end_date\nr1,2026-01-01\nr2,2026-01-11\n")
        counts = {"r1": {"A": 5, "B": 3, "C": 3, "D": 1},
                  "r2": {"A": 4, "B": 3, "C": 1, "D": 1}}
        for retrieval, languages in counts.items():
            self.retrieval(retrieval, [{"mainLanguage": language}
                                       for language, number in languages.items()
                                       for _ in range(number)] + [{"mainLanguage": None}])
        values = {
            "days": self.value("retrievals.end_date_days", earlier="r1", later="r2"),
            "spans": self.value("retrievals.end_date_days", earlier="r1", later="r2", per=5),
            "a": self.value("retrievals.language_rows", retrieval="r2", language="A"),
            "loss": self.value("retrievals.language_loss", earlier="r1", later="r2",
                               language="A"),
            "kept": self.value("retrievals.share_kept", earlier="r1", later="r2", language="A",
                               days=20),
            "lead": self.value("retrievals.language_lead", retrievals=["r1", "r2"], rank=2)}
        # A keeps 4 of 5 rows in 10 days, and 0.8 squared in 20. In r1, B and C tie at 3, and B
        # ranks second by its name, ahead of C by 0. In r2, B leads C by 2.
        self.assertAlmostEqual(values.pop("kept"), 64.0)
        self.assertEqual(values, {"days": 10, "spans": 2.0, "a": 4, "loss": 20.0, "lead": 0})

    def test_the_crawl_requests_follow_the_retries_and_the_languages(self):
        self.write("1_sampling/1_download_ghs.py",
                   "READ_TIMEOUT = 300\nCRAWL_FAILURES_IN_A_ROW = 3\n")
        self.write("1_sampling/config/ghs_download.yaml",
                   "retries:\n  probe_attempts: 2\n  probe_wait_seconds: 60\n"
                   "  probe_round_wait_minutes: [10, 20]\n")
        self.write("data/sampling/ghs_retrievals/index.csv",
                   "retrieval_id,started_at,crawl_dates\n"
                   "r1,2026-01-01,\"{\"\"A\"\": \"\"x\"\"}\"\n"
                   "r2,2026-02-01,\"{\"\"A\"\": \"\"x\"\", \"\"B\"\": \"\"x\"\"}\"\n"
                   "r3,2026-03-01,\n")
        values = {
            "without": self.value("retrievals.crawl_cost", bound=False, unit="requests"),
            "with": self.value("retrievals.crawl_cost", bound=True, unit="requests"),
            "r1": self.value("retrievals.crawl_cost", bound=False, unit="requests",
                             retrieval="r1"),
            "hours": self.value("retrievals.crawl_cost", bound=True, unit="hours")}
        # Three rounds of two attempts: 2 languages or 3 failures each. The waits are 30 minutes
        # before rounds and one minute between the two attempts of each round.
        self.assertEqual(values, {"without": 12, "with": 18, "r1": 6,
                                  "hours": (18 * 300 + 1800 + 180) / 3600})

    def test_the_comparisons_of_a_retrieval_count_what_its_rows_differ_in(self):
        ids = write_retrievals(self)
        new = ids["new"]
        name = "retrievals."
        self.assertEqual(self.value(name + "minutes", retrieval=new, step="download"), 30)
        self.assertEqual(self.value(name + "minutes", retrieval=new, step="github"), 90)
        self.assertEqual(self.value(name + "languages"), 2)

class TestCode(_Computations):
    """`code` reads numbers from the text and the syntax of the code, and the defaults of the
    standard library."""

    def test_calls_count_the_items_of_a_tuple_by_their_arguments(self):
        """`code.calls` selects calls by a string argument and by membership in the names of a
        directory or in an item of the values of a dict, and an argument that is no string
        literal meets no condition."""
        self.write("release.py", "NAME = 'x.csv'\n"
                                 "FILES = (File('a.csv', 'copy'), File('b.csv', 'copy'),\n"
                                 "         File('c.txt', 'copy'), File(NAME, 'copy'),\n"
                                 "         File('a.csv.7z', 'compress'))\n")
        self.write("kinds.py", "CSVS = {'a': ('a.csv', 'x'), 'x': ('x.csv', 'y')}\n")
        self.write("rules/c.txt", "c\n")
        release = {"path": "release.py", "name": "FILES"}
        values = [self.value("code.calls", **release, where={"1": "compress"}),
                  self.value("code.calls", **release, where={"1": "copy"},
                             among={"0": {"path": "kinds.py", "name": "CSVS", "item": 0}}),
                  self.value("code.calls", **release, among={"0": {"glob": "rules/*"}})]
        self.assertEqual(values, [1, 1, 1])

    def test_the_suites_over_the_whole_repository_test_no_module(self):
        for relative in ("agentcontext/a.py", "agentcontext/steps/x.py", "tests/_shared.py",
                         "tests/test_a.py", "tests/test_steps_x.py", "tests/test_shared.py",
                         "tests/test_whole.py", "tests/test_steps_nothing.py"):
            self.write(relative, "")
        self.assertEqual(self.value("code.whole_repository_suites"), 2)

    def test_a_default_of_the_interpreter_is_read_in_a_fresh_process(self):
        """The default is that of a fresh interpreter, whatever this process set."""
        import csv
        import sys
        before = csv.field_size_limit()
        self.addCleanup(csv.field_size_limit, before)
        csv.field_size_limit(sys.maxsize)
        values = [self.value("code.interpreter_default", module="csv",
                             attribute="field_size_limit", call=True),
                  self.value("code.interpreter_default", module="sys",
                             attribute="int_info.default_max_str_digits")]
        expected = subprocess.run([sys.executable, "-I", "-c",
                                   "import csv, sys; print(csv.field_size_limit(), "
                                   "sys.int_info.default_max_str_digits)"],
                                  capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual(values, [int(value) for value in expected])
        with self.assertRaises(Unavailable):
            self.value("code.interpreter_default", module="csv",
                       attribute="field_size_limit; import os", call=True)


class TestCodeDigest(unittest.TestCase):
    """The SHA-256 that keeps a value of the collection statistics file current covers the code
    that computed it: the module of the computation and every module of the package that it
    imports, directly or through another module."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_a_change_of_a_module_that_the_computation_imports_changes_the_digest(self):
        package = self.root / "agentcontext"
        modules = {
            "__init__.py": "ROOT = 1\n",
            "statistics/__init__.py": "def collection(function):\n    return function\n",
            "statistics/probe.py": "from agentcontext import helper, ROOT\n"
                                   "from agentcontext.statistics import collection\n",
            "statistics/plain.py": "from agentcontext.statistics import collection\n",
            "helper.py": "def count():\n    from .deep import parse\n    return parse()\n",
            "deep.py": "def parse():\n    return 1\n",
            "unrelated.py": "VALUE = 2\n"}
        for relative, text in modules.items():
            self.write(f"agentcontext/{relative}", text)
        before = {name: code_digest(name, package) for name in ("probe.count", "plain.count")}
        self.assertEqual(code_digest("probe.other", package), before["probe.count"])
        imported = {"helper.py": True, "deep.py": True, "__init__.py": True,
                    "statistics/__init__.py": True, "statistics/probe.py": True,
                    "unrelated.py": False}
        for relative, changes in imported.items():
            with self.subTest(module=relative):
                self.write(f"agentcontext/{relative}", modules[relative] + "# changed\n")
                after = code_digest("probe.count", package)
                self.write(f"agentcontext/{relative}", modules[relative])
                self.assertEqual(after != before["probe.count"], changes)
        self.write("agentcontext/helper.py", modules["helper.py"] + "# changed\n")
        self.assertEqual(code_digest("plain.count", package), before["plain.count"])

    def test_the_handover_computations_cover_the_reader_of_the_commit_logs(self):
        """`handover.log_commits` parses the commit logs with `agentcontext/commits.py` and
        names the logs with `agentcontext/repos.py`, so a change of either changes its value."""
        package = self.root / "agentcontext"
        shutil.copytree(Path(computations.__file__).resolve().parents[1], package,
                        ignore=shutil.ignore_patterns("__pycache__"))
        before = code_digest("handover.log_commits", package)
        for relative in ("commits.py", "repos.py"):
            with self.subTest(module=relative):
                path = package / relative
                original = path.read_text(encoding="utf-8")
                path.write_text(original + "# changed\n", encoding="utf-8")
                self.assertNotEqual(code_digest("handover.log_commits", package), before)
                path.write_text(original, encoding="utf-8")
        self.assertEqual(code_digest("handover.log_commits", package), before)


class TestHandover(_Computations):
    """`handover` counts the files, the commits of the commit logs and the marketplace catalogs of
    the data root of a handover."""

    def test_commits_of_the_logs_are_counted_in_the_repositories_that_a_heuristic_credits(self):
        def record(sha, author, message):
            subject, _, body = message.partition("\n\n")
            fields = [sha * 40, author, "a@x", author, "a@x", "2026-01-01T00:00:00Z", subject,
                      body, message]
            return b"\0" + b"\0".join(field.encode() for field in fields) + b"\0"
        logs = "data/collection/collect_commits"
        self.write(f"{logs}/o§a.log.xz", lzma.compress(
            record("a", "google-labs-jules[bot]", "Fix tests")
            + record("b", "p", "Merge pull request #5 from o/fix-8689437116413832293\n\nFix")
            + record("c", "p", "Merge pull request #6 from o/feature\n")))
        self.write(f"{logs}/o§b.log.xz", lzma.compress(
            record("d", "p", "Merge pull request #7 from o/fix-8689437116413832294\n")))
        self.write("data/collection/output/other_ai_commits.csv",
                   f"repo_name,commit_sha,matched_heuristics\no/a,{'a' * 40},jules-author-01\n")
        merges = {"message": r"(?im)^merge pull request #\d+ from [^\s/]+/\S*-\d{19,20}$"}
        self.assertEqual(self.value("handover.log_commits", under="data", **merges), 2)
        self.assertEqual(self.value("handover.log_commits", under="data", **merges,
                                    heuristic="jules-author-01"), 1)
        (self.root / logs / "o§a.log.xz").unlink()
        with self.assertRaises(Unavailable):
            self.value("handover.log_commits", under="data", **merges,
                       heuristic="jules-author-01")

    def test_a_catalog_counts_when_a_plugin_source_is_inside_its_repository(self):
        catalogs = {"o§a/.claude-plugin/marketplace.json":
                    '﻿{"plugins": [{"source": "./plugins/x"}]}',
                    "o§b/.claude-plugin/marketplace.json":
                    '{"plugins": [{"source": {"source": "github", "repo": "o/x"}}]}',
                    "o§c/marketplace.json":
                    '{"plugins": [{"source": {"source": "local", "path": "./p"}}]}',
                    "o§d/marketplace.json": '{"plugins": [{"source": "../elsewhere"}]}'}
        for relative, text in catalogs.items():
            self.write(f"data/collection/data/validated/{relative}", text)
        self.write("data/collection/output/plugins.csv", "repo_name,plugin\n" + "".join(
            f"{relative.split('/', 1)[0].replace(chr(0xa7), '/')},{relative.split('/', 1)[1]}\n"
            for relative in catalogs) + "o/a,.claude-plugin/plugin.json\n")
        self.assertEqual(self.value("handover.catalogs", under="data"), 2)

    def test_the_file_lists_with_an_entry_at_a_path_are_counted(self):
        lists = "collection/data/file_lists"
        self.write(f"{lists}/o§a.ls-tree.xz", lzma.compress(
            b"100644\tCONTEXT.md\x00100644\tsrc/main.c\x00"))
        self.write(f"{lists}/o§b.ls-tree.xz", lzma.compress(
            b"100644\tdocs/CONTEXT.md\x00120000\tcontext.md\x00"))
        self.write(f"{lists}/o§c.ls-tree.xz", lzma.compress(b""))

        def entries(path):
            return self.value("handover.files", pattern=f"{lists}/*.ls-tree.xz",
                              contains=r"(?:\A|\x00)[0-7]+\t" + re.escape(path) + r"\x00")
        self.assertEqual([entries(path) for path in ("CONTEXT.md", "docs/CONTEXT.md",
                                                     "README.md")], [1, 1, 0])


if __name__ == "__main__":
    unittest.main()
