"""
Tests for agentcontext.checks.commits

The unit tests call the functions directly with constructed inputs and need neither a git
repository nor network access. The integration tests call main() with temporary CSV files and a
replaced sys.argv, as TestMainCli does in the other test files. tests/test_commits.py tests the
commit patterns themselves. These tests cover the classification of the detection source and the
columns of the output.

Requirements: Python 3.14, git on PATH (stdlib only, no extra pip installs)
Run with:  python3 -m unittest tests.test_checks_commits -v
"""

import collections
import contextlib
import csv
import importlib
import io
import shutil
import os
import sys
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock
# tests._ambient_env, imported below, keeps GITHUB_TOKEN out of the environment, because the
# suites clone from file:// remotes and an ambient GITHUB_TOKEN redirects the clones elsewhere.

_mod = importlib.import_module("agentcontext.checks.commits")
_ai = importlib.import_module("agentcontext.commits")
_caches = importlib.import_module("agentcontext.caches")
_heuristics = importlib.import_module("agentcontext.heuristics")
_logs = importlib.import_module("agentcontext.logs")
_main = _mod.main

# The agents that can run a Claude model, other than the six tools, as the Claude rows of
# ai_commit_patterns.csv name them.
OTHER_AGENTS = (r"ibm[ \t]+bob|kiro|aider|crush|kilo[ \t]+code|roo[ \t]+code|cline|windsurf|junie|augment|trae|"
                r"hermes[ \t]+agent|qwen[ \t]+code|kimi[ \t]+code|openhands|devin|codebuff|codg|openclaw|oh-my-pi|omp|pi[ \t]+coding[ \t]+agent|pi[ \t]+agent")
# The Claude co-author trailer of ai_commit_patterns.csv, which the check reports as the pattern
# that matched `Co-authored-by: Claude <noreply@anthropic.com>`.
CLAUDE_CO_AUTHORED = (
    r"co-authored-by:(?![^\n]*(?:\bvia\s+(?:github[\s_\-]?)?copilot\b|\((?:github\s+)?copilot("
    r"?:\s+cli)?\)|\bvia\s+(?:openai\s+)?codex\b|\((?:openai\s+)?codex(?:\s+cli)?\)|\bvia\s+cu"
    r"rsor\b|\((?:via\s+)?cursor\b|\bvia\s+opencode\b|\((?:via\s+)?opencode\b|\bvia[ \t]+pi[ "
    r"\t]*(?:<[^>\n]*>[ \t]*)?(?:\n|$)|\bpi\.dev\b|\bpi-coding-agent\b|\b(?:via|in|through|and"
    r")[ \t]+the[ \t]+pi[ \t]+coding\s+agent\b|\bpi[ \t]+backend\b|\bvia[ \t]+roadie,[ \t]*pi"
    r"\b|\(via[ \t]+pi\)|\bvia[ \t]+pi[ \t]+\((?!#)|\bpi[ \t]*\([^)\n]*\bclaude\b|\bvia\s+(?:ibm[ "
    r"\t]+bob|kiro|aider|crush|kilo[ \t]+code|roo[ \t]+code|cline|windsurf|junie|augment|trae|"
    r"hermes[ \t]+agent|qwen[ \t]+code|kimi[ \t]+code|openhands|devin|codebuff|codg|openclaw|o"
    r"h-my-pi|omp|pi[ \t]+coding[ \t]+agent|pi[ \t]+agent)\b|\((?:via\s+)?(?:ibm[ \t]+bob|kiro"
    r"|aider|crush|kilo[ \t]+code|roo[ \t]+code|cline|windsurf|junie|augment|trae|hermes[ \t]+"
    r"agent|qwen[ \t]+code|kimi[ \t]+code|openhands|devin|codebuff|codg|openclaw|oh-my-pi|omp|"
    r"pi[ \t]+coding[ \t]+agent|pi[ \t]+agent)\b|\bvia[ \t]+(?:omp|oh-my-pi)\b|\((?:eclipse[ "
    r"\t]+)?theia\b))\s*claude(?![ \t]*<[^>\n]*@google\.com>)(?!-"
    r"configs-distributor\b)(?:[ \t]*(?:[^\w\s]|\n|$)|[\s_\-]*(?:(?:code|opus|sonnet|haiku|fab"
    r"le)\b|\d)|[\s_\-]+ai\b(?![\w\-])|[\s_\-]+w/[ \t]*model\b|[ \t]+agent\b|[^\n]*@anthropic"
    r"\.com)")


@contextlib.contextmanager
def _swap_argv(argv: list):
    saved = sys.argv
    sys.argv = argv
    try:
        yield
    finally:
        sys.argv = saved
_ambient_env = importlib.import_module("tests._ambient_env")

_saved_credentials = {}


def setUpModule():
    # An ambient GITHUB_TOKEN changes the clone URL that these tests redirect and would make the
    # result of the suite depend on the machine rather than on the code.
    # tests/_ambient_env.py lists the variables and the reasons.
    _ambient_env.take(_saved_credentials)


def tearDownModule():
    _ambient_env.restore(_saved_credentials)
split_message = _mod.split_message
classify_detection_source = _mod.classify_detection_source
validate_row = _mod.validate_row


# ---------------------------------------------------------------------------
# split_message
# ---------------------------------------------------------------------------

class TestSplitMessage(unittest.TestCase):

    def test_subject_only(self):
        subject, body = split_message("Fix bug")
        self.assertEqual(subject, "Fix bug")
        self.assertEqual(body, "")

    def test_subject_and_body(self):
        subject, body = split_message("Fix bug\n\nDetailed description")
        self.assertEqual(subject, "Fix bug")
        self.assertEqual(body, "Detailed description")

    def test_empty_string(self):
        subject, body = split_message("")
        self.assertEqual(subject, "")
        self.assertEqual(body, "")

    def test_multi_paragraph_body(self):
        msg = "Subject line\n\nFirst paragraph\n\nSecond paragraph"
        subject, body = split_message(msg)
        self.assertEqual(subject, "Subject line")
        self.assertIn("First paragraph", body)
        self.assertIn("Second paragraph", body)
        # The body keeps the \n\n between its paragraphs.
        self.assertIn("\n\n", body)

    def test_trailing_whitespace(self):
        msg = "  Fix bug  \n\n  Detailed description  "
        subject, body = split_message(msg)
        self.assertEqual(subject, "Fix bug")
        self.assertEqual(body, "Detailed description")


# ---------------------------------------------------------------------------
# classify_detection_source
# ---------------------------------------------------------------------------

class TestClassifyDetectionSource(unittest.TestCase):

    def test_a_disclosure_at_the_start_of_the_body_matches_full_body(self):
        source, scope, pattern = classify_detection_source(
            "Claude", "Add retry", "Implemented with Claude Code")
        self.assertEqual((source, scope), ("message", "full_body"))
        self.assertTrue(pattern)


    def test_claude_trailer_match(self):
        body = "Co-authored-by: Claude <noreply@anthropic.com>"
        source, scope, pattern = classify_detection_source("Claude", "Fix auth", body)
        self.assertEqual(source, "message")
        self.assertEqual(scope, "trailer")
        self.assertEqual(pattern, CLAUDE_CO_AUTHORED)

    def test_copilot_fullbody_match(self):
        # The scope and the pattern are compared whole, because assertIn would accept
        # "trailer+full_body" and assertTrue any pattern at all.
        subject = "Merge pull request #1 from owner/copilot/fix-auth"
        source, scope, pattern = classify_detection_source("Copilot", subject, "")
        self.assertEqual(source, "message")
        self.assertEqual(scope, "full_body")
        self.assertEqual(pattern, r"(?m)^[ \t]*+(?:[*-][ \t]++)?(?:(?![0-9a-f]{40}:)(?:[\w(),!./ -]{1,40}:|\[[^\]\n]"
                                  r"{1,40}\])[ \t]*+)*merge pull request (?:[\w.-]+/[\w.-]+)?#?\d+ from (?!chavdasac"
                                  r"hin/)[^\s/]+/copilot/(?!v\d+\.\d+)")

    def test_no_match_returns_author_only(self):
        source, scope, pattern = classify_detection_source(
            "Claude", "plain human commit", "no signals here"
        )
        self.assertEqual(source, "author_only")
        self.assertEqual(scope, "")
        self.assertEqual(pattern, "")

    def test_unknown_tool_returns_author_only(self):
        source, scope, pattern = classify_detection_source(
            "UnknownTool", "Fix bug", "Co-authored-by: Claude <noreply@anthropic.com>"
        )
        self.assertEqual(source, "author_only")
        self.assertEqual(scope, "")
        self.assertEqual(pattern, "")

    def test_copilot_coauthored_trailer_match(self):
        body = "Fix\n\nco-authored-by: copilot"
        source, scope, pattern = classify_detection_source("Copilot", "Fix", body)
        self.assertEqual(source, "message")
        self.assertEqual(scope, "trailer")
        self.assertEqual(pattern, r"co-authored-by:(?![^\n]*\bcopilot[ \t]+fallback\b)\s*(?:github\s+)?copilot")

    def test_cursor_fullbody_match(self):
        subject = "This commit was generated by cursor"
        source, scope, pattern = classify_detection_source("Cursor", subject, "")
        self.assertEqual(source, "message")
        self.assertEqual(scope, "full_body")
        self.assertEqual(pattern, r"this\s+(?:pr|commit|change|patch)\s+was\s+(?:created|written|"
                                  r"generated|produced|implemented|authored|prepared)\s+(?:in\s+(?:some|"
                                  r"large|great|small)\s+part\s+)?(?:by|with(?:\s+the\s+(?:help|"
                                  r"assistance)\s+of)?)\s+(?:an?\s+(?:autonomous\s+)?(?:ai\s+)?(?:coding"
                                  r"\s+)?agent\s*\(\s*)?[\[`]*cursor")


# ---------------------------------------------------------------------------
# validate_row
# ---------------------------------------------------------------------------

class TestValidateRow(unittest.TestCase):

    def test_row_with_trailer_detection(self):
        row = {
            "ai_tool": "Claude",
            "commit_message": "Fix auth\n\nCo-authored-by: Claude <noreply@anthropic.com>",
            "repo_name": "owner/repo",
        }
        result = validate_row(row, "ai_tool", "commit_message")
        self.assertEqual(result["detection_source"], "message")

    def test_row_with_author_only(self):
        row = {
            "ai_tool": "Claude",
            "commit_message": "plain human commit message without signals",
            "repo_name": "owner/repo",
        }
        result = validate_row(row, "ai_tool", "commit_message")
        self.assertEqual(result["detection_source"], "author_only")

    def test_multi_tool_prefers_message_match(self):
        # Claude matches through its trailer, and no message heuristic matches Copilot.
        row = {
            "ai_tool": "Claude,Copilot",
            "commit_message": "Fix auth\n\nCo-authored-by: Claude <noreply@anthropic.com>",
            "repo_name": "owner/repo",
        }
        result = validate_row(row, "ai_tool", "commit_message")
        self.assertEqual(result["detection_source"], "message")

    def test_original_columns_preserved(self):
        row = {
            "ai_tool": "Claude",
            "commit_message": "Fix bug",
            "repo_name": "owner/repo",
            "extra_col": "extra_value",
        }
        result = validate_row(row, "ai_tool", "commit_message")
        self.assertEqual(result["repo_name"], "owner/repo")
        self.assertEqual(result["extra_col"], "extra_value")
        self.assertEqual(result["ai_tool"], "Claude")
        self.assertEqual(result["commit_message"], "Fix bug")

    def test_all_new_columns_present(self):
        row = {
            "ai_tool": "Claude",
            "commit_message": "Fix bug",
        }
        result = validate_row(row, "ai_tool", "commit_message")
        for col in ("detection_source", "matched_scope", "matched_pattern"):
            self.assertIn(col, result, f"Missing new column: {col}")

    def test_empty_tool_col(self):
        row = {
            "ai_tool": "",
            "commit_message": "Fix bug",
        }
        result = validate_row(row, "ai_tool", "commit_message")
        self.assertEqual(result["detection_source"], "")


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

class TestTheOtherToolsCsv(unittest.TestCase):
    """The check compares `other_ai_commits.csv` with `other_ai_commit_patterns.csv`. The
    heuristics of the six tools cannot check that CSV, because each of its rows names heuristics
    that `ai_commit_patterns.csv` does not have."""

    DEVIN = ("fix: x\n\nCo-Authored-By: devin-ai-integration[bot] "
             "<158243242+devin-ai-integration[bot]@users.noreply.github.com>")

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tools = self._write("all_ai_commits.csv", [])

    def _write(self, name: str, rows: list) -> Path:
        fields = ["repo_name", "commit_sha", "commit_message", "ai_tool", "matched_heuristics",
                  "attributions"]
        path = self.tmp / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        return path

    def _devin_row(self, **changed) -> dict:
        subject, body = _mod.split_message(self.DEVIN)
        fields = ("", "", "", "", subject, body)
        row = {"repo_name": "o/r", "commit_sha": "a" * 40, "commit_message": self.DEVIN,
               "ai_tool": ",".join(_ai.detect_other_ai_tools_in_fields(*fields, check_author=False)),
               "matched_heuristics": ";".join(_ai.matched_other_rules_in_fields(
                   *fields, check_author=False)),
               "attributions": _ai.other_attributions_in_fields(*fields, check_author=False)}
        return {**row, **changed}

    def _run(self, *args) -> tuple:
        err = io.StringIO()
        with _swap_argv(["commit-attribution-validation", *args]), \
                contextlib.redirect_stderr(err):
            rc = _main()
        return rc, err.getvalue()

    def test_an_other_csv_that_agrees_exits_0_and_is_classified_beside_itself(self):
        row = self._devin_row()
        self.assertEqual(row["ai_tool"], "Devin")
        other = self._write("other_ai_commits.csv", [row])
        rc, err = self._run("-i", str(self.tools), "--other-input", str(other))
        self.assertEqual(rc, 0, err)
        self.assertIn("1 of 1 row(s) of other_ai_commits.csv match what the current heuristics "
                      "give", err)
        with open(self.tmp / "other_ai_commits-validated.csv", newline="", encoding="utf-8") as f:
            self.assertEqual([r["detection_source"] for r in csv.DictReader(f)], ["message"])

    def test_an_other_csv_row_whose_author_names_the_tool_exits_0(self):
        """The cloning step reads the author's identity, which the CSV does not store. A commit
        of Devin's bot with a trailer that credits AI and names no tool is Devin's, and the step
        leaves out the heuristics of the tool not named. A check that held the row to the message
        alone would expect those heuristics, exit 2 and stop the run before the release at every
        Start."""
        message = "Fix the parser\n\nCo-authored-by: AI <ai@example.com>"
        subject, body = _mod.split_message(message)
        fields = ("devin-ai-integration[bot]",
                  "158243242+devin-ai-integration[bot]@users.noreply.github.com", "", "",
                  subject, body)
        row = {"repo_name": "o/r", "commit_sha": "a" * 40, "commit_message": message,
               "ai_tool": ",".join(_ai.detect_other_ai_tools_in_fields(*fields)),
               "matched_heuristics": ";".join(_ai.matched_other_rules_in_fields(*fields)),
               "attributions": _ai.other_attributions_in_fields(*fields)}
        self.assertEqual(row["ai_tool"], "Devin")
        other = self._write("other_ai_commits.csv", [row])
        rc, err = self._run("-i", str(self.tools), "--other-input", str(other))
        self.assertEqual(rc, 0, err)

    def test_an_other_csv_whose_rules_are_not_what_its_heuristics_give_exits_2(self):
        other = self._write("other_ai_commits.csv",
                            [self._devin_row(matched_heuristics="jules-author-01",
                                             ai_tool="Jules")])
        rc, err = self._run("-i", str(self.tools), "--other-input", str(other))
        self.assertEqual(rc, 2, err)
        self.assertIn("1 of 1 row(s) of other_ai_commits.csv differ from what the current "
                      "heuristics give", err)

    def test_under_the_six_tools_heuristics_the_other_csv_names_unknown_rules(self):
        other = self._write("other_ai_commits.csv", [self._devin_row()])
        rc, err = self._run("-i", str(other), "--summary-only")
        self.assertEqual(rc, 2, err)
        self.assertIn("names the unknown heuristic ids", err)


class TestTheVerdict(unittest.TestCase):
    """The exit code reports whether the CSV agrees with what the heuristics give for the values
    that it stores: the tools that every recorded heuristic names, and the recorded message
    heuristics compared with the heuristics that match the message now."""

    CLAUDE = "feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>"
    TWO = "feat: x\n\nCo-authored-by: Claude <noreply@anthropic.com>\nMade-with: Cursor"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _message_rules(self, message: str) -> list:
        subject, body = _mod.split_message(message)
        return _ai.matched_rules_in_fields("", "", "", "", subject, body, check_author=False)

    def _author_rules(self, name: str) -> list:
        return _ai.matched_rules_in_fields(name, "x@example.com", "Dev", "d@example.com",
                                           "fix", "")

    def _table(self, rows: list, with_heuristics: bool = True) -> Path:
        fields = ["repo_name", "commit_sha", "commit_message", "ai_tool"]
        if with_heuristics:
            fields += ["matched_heuristics", "attributions"]
        path = self.tmp / "CSV.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for row in rows:
                w.writerow({k: row.get(k, "") for k in fields})
        return path

    def _row(self, message: str, tools: str, rules: list = None, kinds: str = "") -> dict:
        return {"repo_name": "o/r", "commit_sha": "a" * 40, "commit_message": message,
                "ai_tool": tools, "matched_heuristics": ";".join(rules or []),
                "attributions": kinds}

    def _run(self, path: Path) -> tuple:
        err = io.StringIO()
        with _swap_argv(["commit-attribution-validation", "-i", str(path), "--summary-only"]), \
                contextlib.redirect_stderr(err):
            rc = _main()
        return rc, err.getvalue()

    def test_a_commit_csv_the_cloning_step_wrote_that_agrees_exits_0(self):
        rules = self._message_rules(self.CLAUDE)
        self.assertNotEqual(rules, [])
        rc, err = self._run(self._table([self._row(self.CLAUDE, "Claude", rules,
                                                   "Claude:standard")]))
        self.assertEqual(rc, 0)
        self.assertIn("1 of 1 row(s) of CSV.csv match what the current heuristics give", err)

    def test_attributions_that_are_not_what_the_heuristics_give_exit_2(self):
        """`attributions` has to be what the heuristics give for the stored message and the
        recorded author heuristics. A code review suggestion recorded as Copilot's own line does
        not agree, and neither does an account that no recorded author heuristic matches."""
        review = "fix: x\n\nCo-authored-by: Copilot <175728472+Copilot@users.noreply.github.com>"
        cases = (
            (review, self._message_rules(review), "Copilot:standard"),
            (self.CLAUDE, self._message_rules(self.CLAUDE), "Claude:account;Claude:standard"),
            (self.CLAUDE, self._message_rules(self.CLAUDE), ""),
        )
        for message, rules, kinds in cases:
            with self.subTest(kinds=kinds):
                tool = "Copilot" if message == review else "Claude"
                rc, err = self._run(self._table([self._row(message, tool, rules, kinds)]))
                self.assertEqual(rc, 2)
                self.assertIn("but the heuristics give", err)
        rc, _ = self._run(self._table([self._row(review, "Copilot", self._message_rules(review),
                                                 "Copilot:reviewer")]))
        self.assertEqual(rc, 0)

    def test_an_author_heuristic_with_convention_gives_convention(self):
        """A name that users gave the author, such as Cursor Agent at an address other than Cursor's,
        is a convention: the recorded author heuristic gives the attribution of its row, and
        account only where a row of the tool with account matched."""
        rules = _ai.matched_rules_in_fields("Cursor Agent", "cursor-agent@local", "Dev",
                                            "d@example.com", "fix", "")
        self.assertEqual(rules, ["cursor-author-03", "cursor-author-05"])
        rc, _ = self._run(self._table([self._row("fix", "Cursor", rules, "Cursor:convention")]))
        self.assertEqual(rc, 0)
        rc, err = self._run(self._table([self._row("fix", "Cursor", rules, "Cursor:account")]))
        self.assertEqual(rc, 2)
        self.assertIn("but the heuristics give 'Cursor:convention'", err)
        rules = _ai.matched_rules_in_fields("Cursor Agent", "cursoragent@cursor.com", "Dev",
                                            "d@example.com", "fix", "")
        rc, _ = self._run(self._table([self._row("fix", "Cursor", rules, "Cursor:account")]))
        self.assertEqual(rc, 0)

    def test_a_row_whose_rules_name_another_tool_exits_2(self):
        rules = self._message_rules(self.CLAUDE)
        rc, err = self._run(self._table([self._row(self.CLAUDE, "Copilot", rules)]))
        self.assertEqual(rc, 2)
        self.assertIn("the row names ['Copilot']", err)

    def test_a_row_whose_message_rules_do_not_match_now_exits_2(self):
        rules = self._message_rules(self.CLAUDE) + self._message_rules(
            "x\n\nCo-authored-by: Copilot <c@example.com>")
        rc, err = self._run(self._table([self._row(self.CLAUDE, "Claude,Copilot", rules)]))
        self.assertEqual(rc, 2)
        self.assertIn("The row records the message heuristics", err)
        self.assertIn("match the message now", err)

    def test_an_author_only_row_with_its_author_rule_exits_0(self):
        rules = self._author_rules("copilot-swe-agent[bot]")
        self.assertNotEqual(rules, [])
        rc, _ = self._run(self._table([self._row("fix: y", "Copilot", rules, "Copilot:account")]))
        self.assertEqual(rc, 0)

    def test_a_heuristic_the_file_does_not_have_exits_2(self):
        rc, err = self._run(self._table([self._row(self.CLAUDE, "Claude",
                                                   ["claude-trailer-99"])]))
        self.assertEqual(rc, 2)
        self.assertIn("names the unknown heuristic ids", err)

    def test_a_commit_csv_without_the_column_is_held_to_the_message_alone(self):
        """A CSV of the 1.0.0 release has no matched_heuristics column. An author-only row of
        such a CSV cannot be checked. A message that matches a tool that the row does not name
        shows that the row was classified under other heuristics."""
        rows = [self._row(self.CLAUDE, "Claude"), self._row("fix: y", "Copilot")]
        rc, _ = self._run(self._table(rows, with_heuristics=False))
        self.assertEqual(rc, 0)
        rows.append(self._row(self.TWO, "Claude"))
        rc, err = self._run(self._table(rows, with_heuristics=False))
        self.assertEqual(rc, 2)
        self.assertIn("The message matches ['Cursor'], but the row names", err)

    def test_a_classification_that_cannot_be_written_leaves_the_previous_classification(self):
        """A full disk leaves the previous classification whole rather than truncated."""
        import errno
        rules = self._message_rules(self.CLAUDE)
        path = self._table([self._row(self.CLAUDE, "Claude", rules)])
        out = self.tmp / "out.csv"
        out.write_text("the previous classification\n", encoding="utf-8")
        err = io.StringIO()
        full = OSError(errno.ENOSPC, "No space left on device")
        with _swap_argv(["commit-attribution-validation", "-i", str(path), "-o", str(out)]), \
                mock.patch("agentcontext.caches.os.replace", side_effect=full), \
                contextlib.redirect_stderr(err):
            rc = _main()
        self.assertEqual(rc, 1)
        self.assertIn("Could not write", err.getvalue())
        self.assertEqual(out.read_text(encoding="utf-8"), "the previous classification\n")
        self.assertEqual(_caches.temporary_files(out), [])

    def test_a_row_that_cannot_be_written_leaves_the_previous_classification(self):
        """The check writes each row as it reads it. A full disk partway through the rows
        leaves the previous classification whole and no temporary file beside it."""
        import errno
        rules = self._message_rules(self.CLAUDE)
        path = self._table([self._row(self.CLAUDE, "Claude", rules, "Claude:standard")] * 3)
        out = self.tmp / "out.csv"
        out.write_text("the previous classification\n", encoding="utf-8")
        err = io.StringIO()
        full = OSError(errno.ENOSPC, "No space left on device")
        with _swap_argv(["commit-attribution-validation", "-i", str(path), "-o", str(out)]), \
                mock.patch.object(_mod._Classification, "write", side_effect=[None, full]), \
                contextlib.redirect_stderr(err):
            rc = _main()
        self.assertEqual(rc, 1)
        self.assertIn("Could not write", err.getvalue())
        self.assertEqual(out.read_text(encoding="utf-8"), "the previous classification\n")
        self.assertEqual(_caches.temporary_files(out), [])

    def test_the_classification_is_written_before_the_verdict(self):
        rules = self._message_rules(self.CLAUDE)
        path = self._table([self._row(self.CLAUDE, "Copilot", rules)])
        out = self.tmp / "out.csv"
        err = io.StringIO()
        with _swap_argv(["commit-attribution-validation", "-i", str(path), "-o", str(out)]), \
                contextlib.redirect_stderr(err):
            rc = _main()
        self.assertEqual(rc, 2)
        # The test compares the values rather than the header, because the check writes the
        # header whatever the rows contain, and the header does not distinguish the classified
        # rows from the input rows.
        with out.open(newline="", encoding="utf-8") as f:
            written = [(r["detection_source"], r["matched_scope"], r["matched_pattern"])
                       for r in csv.DictReader(f)]
        self.assertEqual(written, [("author_only", "", "")])


# ---------------------------------------------------------------------------
# match_outcome
# ---------------------------------------------------------------------------

class TestMatchOutcome(unittest.TestCase):
    CLAUDE = "x\n\nCo-authored-by: Claude <noreply@anthropic.com>"

    def test_the_five_match_outcomes(self):
        both = self.CLAUDE + "\nMade-with: Cursor"
        cases = [("fix", "Copilot", "identity_only"),
                 (self.CLAUDE, "Claude", "message_exact"),
                 (both, "Claude", "message_more"),
                 (self.CLAUDE, "Claude,Copilot", "message_part"),
                 (self.CLAUDE, "Copilot", "message_other")]
        for message, tools, kind in cases:
            with self.subTest(kind=kind):
                self.assertEqual(_mod.match_outcome({"commit_message": message,
                                                   "ai_tool": tools}), kind)
        self.assertEqual(tuple(k for _, _, k in cases), _mod.MATCH_OUTCOMES)


# ---------------------------------------------------------------------------
# main() CLI integration
# ---------------------------------------------------------------------------

class TestMainCli(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_input(self, rows: list, fieldnames: list = None) -> Path:
        p = self.tmp / "input.csv"
        if fieldnames is None:
            fieldnames = ["repo_name", "commit_sha", "commit_timestamp",
                          "branch", "github_link", "commit_message", "ai_tool"]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in fieldnames})
        return p

    def _base_row(self, tool: str = "Claude", message: str = "Fix bug") -> dict:
        return {
            "repo_name": "owner/repo",
            "commit_sha": "a" * 40,
            "commit_timestamp": "2025-01-01T00:00:00Z",
            "branch": "main",
            "github_link": "https://github.com/owner/repo/commit/" + "a" * 40,
            "commit_message": message,
            "ai_tool": tool,
        }

    def _run_main(self, input_csv: Path, output_csv: Path = None,
                  extra_args: list = None) -> int:
        _main = importlib.import_module("agentcontext.checks.commits").main
        orig_argv = sys.argv
        try:
            argv = [
                "commit-attribution-validation",
                "-i", str(input_csv),
            ]
            if output_csv is not None:
                argv += ["-o", str(output_csv)]
            if extra_args:
                argv += extra_args
            sys.argv = argv
            return _main()
        finally:
            sys.argv = orig_argv

    def test_output_has_validation_columns(self):
        inp = self._write_input([
            self._base_row("Claude", "Fix\n\nCo-authored-by: Claude <noreply@anthropic.com>"),
        ])
        out = self.tmp / "out.csv"
        self._run_main(inp, out)
        self.assertTrue(out.exists(), "Output file was not created")
        with out.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        # The test compares the values that the classification gave, not only the columns,
        # because the input rows under the extended header have the same columns and leave all
        # three blank.
        self.assertEqual([(r["detection_source"], r["matched_scope"], r["matched_pattern"])
                          for r in rows],
                         [("message", "trailer", CLAUDE_CO_AUTHORED)])

    def test_summary_only_no_output_file(self):
        inp = self._write_input([self._base_row()])
        out = self.tmp / "out.csv"
        rc = self._run_main(inp, out, extra_args=["--summary-only"])
        self.assertEqual(rc, 0)
        self.assertFalse(out.exists(), "Output file should not be created with --summary-only")

    def test_missing_input_returns_nonzero(self):
        nonexistent = self.tmp / "nonexistent.csv"
        out = self.tmp / "out.csv"
        rc = self._run_main(nonexistent, out)
        self.assertNotEqual(rc, 0)

    def test_row_count_matches_input(self):
        inp = self._write_input([
            self._base_row("Claude", "Fix\n\nCo-authored-by: Claude <noreply@anthropic.com>"),
            self._base_row("Copilot", "plain commit"),
            self._base_row("Cursor", "another plain commit"),
        ])
        out = self.tmp / "out.csv"
        self._run_main(inp, out)
        with out.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual([(r["ai_tool"], r["detection_source"], r["matched_scope"],
                           r["matched_pattern"]) for r in rows],
                         [("Claude", "message", "trailer", CLAUDE_CO_AUTHORED),
                          ("Copilot", "author_only", "", ""),
                          ("Cursor", "author_only", "", "")])

    def test_the_classification_ends_each_record_with_lf(self):
        """The classification ends each record with LF, as every CSV of the pipeline does,
        rather than with the CRLF that the csv module writes by default."""
        inp = self._write_input([
            self._base_row("Claude", "Fix\n\nCo-authored-by: Claude <noreply@anthropic.com>"),
            self._base_row("Copilot", "plain commit"),
        ])
        out = self.tmp / "out.csv"
        self.assertEqual(self._run_main(inp, out), 0)
        self.assertNotIn(b"\r\n", out.read_bytes())
        with out.open(newline="", encoding="utf-8") as f:
            self.assertEqual([r["ai_tool"] for r in csv.DictReader(f)], ["Claude", "Copilot"])


class TestTheMessageSurvivesTheCSV(unittest.TestCase):
    """The commit CSV stores a message verbatim, with its line breaks inside a quoted field as
    RFC 4180 specifies, and the check reads the message back unchanged. A line break escaped to
    a backslash and n could not be distinguished from a literal backslash and n in a message,
    and the escape would leave a lone carriage return unescaped."""

    CASES = (
        "plain subject",
        "subject\n\nbody line",
        "fix: a path like C:\\name and more\n\nCo-Authored-By: Claude <x@y>",
        "a message ending in a backslash\\",
        "a literal \\n inside the text\n\nbody",
        'a "quoted", comma-holding subject\n\nbody',
        "a carriage return\ralone, and one\r\n\r\nbefore a newline",
    )

    def test_every_message_survives_the_csv(self):
        from agentcontext import commits as commits_module
        for message in self.CASES:
            with self.subTest(message=message):
                text = io.StringIO(newline="")
                writer = csv.DictWriter(text, fieldnames=["commit_message", "matched_heuristics"],
                                        lineterminator="\n")
                writer.writeheader()
                writer.writerow({"commit_message": commits_module._with_newlines(message),
                                 "matched_heuristics": ""})
                [row] = list(csv.DictReader(io.StringIO(text.getvalue(), newline="")))
                self.assertEqual(row["commit_message"],
                                 message.replace("\r\n", "\n").replace("\r", "\n"))
                self.assertNotIn("\r", text.getvalue())

    def test_a_literal_backslash_n_does_not_split_the_subject(self):
        from agentcontext import commits as commits_module
        message = ("chore: escape C:\\name and \\n in the docs\n\n"
                   "Co-Authored-By: Claude <noreply@anthropic.com>")
        subject, body = _mod.split_message(commits_module._with_newlines(message))
        self.assertEqual(subject, "chore: escape C:\\name and \\n in the docs")
        self.assertEqual(body, "Co-Authored-By: Claude <noreply@anthropic.com>")

    def test_a_row_of_the_1_0_0_release_has_its_escape_decoded(self):
        """A CSV of the 1.0.0 release stores a newline as a backslash and n and has no
        matched_heuristics column. A row of the current CSV keeps a literal backslash and n as
        text."""
        message = "fix\\n\\nMade-with: Cursor"
        self.assertTrue(_mod.legacy({"commit_message": message}))
        self.assertFalse(_mod.legacy({"commit_message": message, "matched_heuristics": ""}))
        self.assertEqual(_mod.split_message(message, escaped=True), ("fix", "Made-with: Cursor"))
        self.assertEqual(_mod.split_message(message), (message, ""))


# ---------------------------------------------------------------------------
# Reading the CSV a row at a time
# ---------------------------------------------------------------------------

def _sha(k: int) -> str:
    """A commit hash that differs for each `k` in its first twelve characters, which the verdict
    lists."""
    return f"{k:012x}" + "0" * 28


def _first_patterns(tool: str, subject: str, body: str, compiled: list) -> tuple:
    """(detection_source, matched_scope, matched_pattern) of one tool, from the first trailer
    pattern and the first full-body pattern of that tool that match. The test computes them from
    the tool's compiled patterns alone, independently of the match of every heuristic from which
    the check takes them."""
    trailer_text, full_body_text = _ai.scope_texts(subject, body)
    for name, _author, trailer, full_body in compiled:
        if name != tool:
            continue
        first_trailer = next((_heuristics.heuristic_pattern(p) for p in trailer
                              if p.search(trailer_text)), "")
        first_full_body = next((_heuristics.heuristic_pattern(p) for p in full_body
                                if p.search(full_body_text)), "")
        if first_trailer and first_full_body:
            return ("message", "trailer+full_body", first_trailer)
        if first_trailer:
            return ("message", "trailer", first_trailer)
        if first_full_body:
            return ("message", "full_body", first_full_body)
    return ("author_only", "", "")


def _check_in_memory(path: Path, output: str, heuristics, compiled: list) -> tuple:
    """(the classification CSV, the log lines, the exit code) of a check that holds the whole
    CSV in memory: every row read first, each row classified by `_first_patterns`, the CSV built
    in one string, and the rows compared with the heuristics in a second loop."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = [c for c in (reader.fieldnames or []) if c]
    classified, counts = [], {}
    for row in rows:
        tools = [t.strip() for t in (row["ai_tool"] or "").split(",") if t.strip()]
        subject, body = _mod.split_message(row["commit_message"] or "",
                                           escaped=_mod.legacy(row))
        best = ("", "", "")
        for tool in tools:
            source = _first_patterns(tool, subject, body, compiled)
            if source[0] == "message" and best[0] != "message" or not best[0]:
                best = source
        classified.append({**row, **dict(zip(_mod.NEW_COLUMNS, best))})
        for tool in tools:
            counts.setdefault(tool, collections.Counter())[best[0]] += 1
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, extrasaction="ignore", lineterminator="\n", fieldnames=fields + [
        c for c in _mod.NEW_COLUMNS if c not in fields])
    writer.writeheader()
    writer.writerows(classified)
    n = len(rows)
    lines = [("INFO", f"{path.name} has {n:,} rows, with the tools in the column 'ai_tool' and "
                      f"the messages in the column 'commit_message'."),
             ("INFO", f"Classified {n:,} rows."),
             ("INFO", f"Counted {n:,} rows by detection source.")]
    for tool in sorted(counts):
        total = sum(counts[tool].values())
        message, author_only = (f"{counts[tool][source]:,} "
                                f"({100 * counts[tool][source] / total:.1f}%)"
                                for source in ("message", "author_only"))
        lines.append(("INFO", f"Of the {total:,} rows of {tool}, {message} have the detection "
                              f"source message and {author_only} author_only."))
    lines.append(("INFO", f"Wrote {output}."))
    differences = []
    for row in rows:
        why = _mod.mismatch_reason(row, "ai_tool", "commit_message", heuristics)
        if why:
            differences.append(f"The row of commit {row['commit_sha'][:12]} of "
                               f"{row['repo_name']} differs ({why}).")
    if not differences:
        lines.append(("INFO", f"{n:,} of {n:,} row(s) of {path.name} match what the current "
                              f"heuristics give for the columns that the CSV stores."))
        return text.getvalue(), lines, 0
    lines.append(("WARNING", f"{len(differences):,} of {n:,} row(s) of {path.name} differ from "
                             f"what the current heuristics give for the columns that the "
                             f"CSV stores. The first ten follow."))
    lines += [("WARNING", difference) for difference in differences[:10]]
    return text.getvalue(), lines, 2


class TestTheCheckReadsTheCsvARowAtATime(unittest.TestCase):
    """The check reads the commit CSV a row at a time. It classifies each row, writes it and
    compares it with the heuristics before it reads the next row. The CSV grows with every
    collection, and the memory that the check needs must not grow with it."""

    # The most memory that Python may allocate during the check, whatever the number of rows. A
    # check that held its rows could not stay below this limit, because the generated CSV is
    # larger.
    PEAK_BYTES = 2 * 1024 * 1024

    CLAUDE_TRAILER = "Co-authored-by: Claude <noreply@anthropic.com>"
    CLAUDE = f"feat: x\n\n{CLAUDE_TRAILER}"
    DEVIN = ("Co-Authored-By: devin-ai-integration[bot] "
             "<158243242+devin-ai-integration[bot]@users.noreply.github.com>")
    # Messages of the six tools: a trailer, a full-body match, both, a merge branch, two tools,
    # a revert's subject, a message without a match, and characters that the CSV quotes.
    MESSAGES = (
        CLAUDE,
        "Add retry\n\nImplemented with Claude Code",
        f"Implemented with Claude Code\n\n{CLAUDE_TRAILER}",
        "Merge pull request #1 from owner/copilot/fix-auth",
        f"{CLAUDE}\nMade-with: Cursor",
        'Revert "Co-authored-by: Copilot"',
        "fix: y",
        'fix: a "quoted", comma-holding subject\n\nA line\rand Co-authored-by: Claude <c@x>',
    )
    # Messages of the tools outside the six: the trailer of `UNNAMED_TOOL` alone, and beside a
    # trailer that names a tool, where `matched_other_rules_in_fields` leaves out the
    # `UNNAMED_TOOL` heuristics.
    OTHER_MESSAGES = (
        f"fix: y\n\n{DEVIN}",
        "fix: y\n\nCo-authored-by: AI\n",
        f"fix: y\n\nCo-authored-by: AI\n{DEVIN}",
        f"fix: y\n\nCo-authored-by: AI\n{CLAUDE_TRAILER}",
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _write(self, name: str, header: list, rows: list) -> Path:
        path = self.tmp / name
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)
        return path

    @staticmethod
    def _recorded(message: str, author: str = "", other: bool = False) -> tuple:
        """(matched_heuristics, attributions) as the cloning step writes them for a commit with
        this message and author."""
        subject, body = _mod.split_message(message)
        fields = (author, "a@example.com" if author else "", "", "", subject, body)
        if other:
            return (";".join(_ai.matched_other_rules_in_fields(*fields)),
                    _ai.other_attributions_in_fields(*fields))
        return ";".join(_ai.matched_rules_in_fields(*fields)), _ai.attributions_in_fields(*fields)

    def _tools_csv(self) -> Path:
        """Rows that agree with the heuristics and more than ten rows that do not, among them
        rows that name several tools, the same tool twice or no tool, a short row and a long row.
        The header has a column without a name, which the classification leaves out."""
        header = ["repo_name", "commit_sha", "commit_message", "ai_tool", "matched_heuristics",
                  "attributions", ""]
        bot = "copilot-swe-agent[bot]"
        rows, k = [], 0
        for message in self.MESSAGES:
            for tools, author in (("Claude", ""), ("Copilot", bot), ("Cursor,Claude", ""),
                                  ("Copilot,Claude", bot), ("Claude,Claude", ""), ("", ""),
                                  ("UnknownTool", "")):
                k += 1
                rules, kinds = self._recorded(message, author)
                rows.append([f"o/r{k}", _sha(k), message, tools, rules, kinds, "unnamed"])
        rows.append(["o/short", "f" * 40, self.CLAUDE, "Claude"])
        rows.append(["o/long", "e" * 40, self.CLAUDE, "Claude", *self._recorded(self.CLAUDE),
                     "unnamed", "an extra field"])
        return self._write("all_ai_commits.csv", header, rows)

    def _compare(self, argv: list, expected: list) -> None:
        """Run the check with `argv`, and compare its exit code, its log lines and the bytes of
        each classification CSV with those of `_check_in_memory` over the same inputs."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = _main(argv)
        lines, codes = [], []
        for path, output, heuristics, compiled in expected:
            text, logged, verdict = _check_in_memory(path, output, heuristics, compiled)
            self.assertEqual(Path(output).read_bytes(), text.encode("utf-8"), output)
            lines += [_logs.line(level, _mod.COMPONENT, message) for level, message in logged]
            codes.append(verdict)
        self.assertEqual(err.getvalue().splitlines(), lines)
        self.assertEqual(code, max(codes))

    def test_the_outputs_are_those_of_a_check_that_reads_every_row_first(self):
        tools = self._tools_csv()
        other_rows = [[f"o/other{k}", _sha(k), message, name,
                       *self._recorded(message, other=True)]
                      for k, (message, name) in enumerate(
                          (m, n) for m in self.OTHER_MESSAGES
                          for n in ("Devin", "tool not named", "Jules"))]
        other = self._write("other_ai_commits.csv",
                            ["repo_name", "commit_sha", "commit_message", "ai_tool",
                             "matched_heuristics", "attributions"], other_rows)
        out = str(self.tmp / "classified.csv")
        self._compare(["-i", str(tools), "-o", out, "--other-input", str(other)],
                      [(tools, out, _mod.TOOLS, _ai._COMPILED_PATTERNS),
                       (other, _mod.default_output_path(str(other)), _mod.OTHER_TOOLS,
                        _ai._OTHER_COMPILED_PATTERNS)])

    def test_the_outputs_of_a_csv_of_the_1_0_0_release(self):
        """A CSV without `matched_heuristics` that stores each newline of a message as a
        backslash and n."""
        rows = [[f"o/r{k}", _sha(k), message.replace("\\", "\\\\").replace("\n", "\\n"),
                 tools]
                for k, (message, tools) in enumerate(
                    (m, t) for m in self.MESSAGES
                    for t in ("Claude", "Copilot", "Cursor,Claude"))]
        legacy = self._write("legacy.csv", ["repo_name", "commit_sha", "commit_message",
                                            "ai_tool"], rows)
        out = str(self.tmp / "legacy-classified.csv")
        self._compare(["-i", str(legacy), "-o", out],
                      [(legacy, out, _mod.TOOLS, _ai._COMPILED_PATTERNS)])

    def test_the_verdict_compares_the_heuristics_that_the_cloning_step_records(self):
        """The check matches a message once. For a row with the tools that the cloning step
        writes, the ids that the check compares with `matched_heuristics` are the ids that the
        step writes there."""
        for heuristics, messages, tools, recorded in (
                (_mod.TOOLS, self.MESSAGES, _ai.detect_ai_tools_in_fields,
                 _ai.matched_rules_in_fields),
                (_mod.OTHER_TOOLS, self.OTHER_MESSAGES, _ai.detect_other_ai_tools_in_fields,
                 _ai.matched_other_rules_in_fields)):
            for message in messages:
                with self.subTest(message=message):
                    subject, body = _mod.split_message(message)
                    fields = ("", "", "", "", subject, body)
                    match = _mod.match_row({"commit_message": message,
                                            "ai_tool": ",".join(tools(*fields)),
                                            "matched_heuristics": ""},
                                           "ai_tool", "commit_message", heuristics)
                    self.assertEqual(match.recorded, recorded(*fields))

    def test_the_memory_that_the_check_needs_does_not_grow_with_the_csv(self):
        body = "\n".join(f"- Parse the long form of option {k} and test the parser on it"
                         for k in range(12))
        message = f"feat: parse long options\n\n{body}\n\n{self.CLAUDE_TRAILER}"
        rules, kinds = self._recorded(message)
        path = self._write("all_ai_commits.csv",
                           ["repo_name", "commit_sha", "commit_message", "ai_tool",
                            "matched_heuristics", "attributions"],
                           ([f"o/r{k}", _sha(k), message, "Claude", rules, kinds]
                            for k in range(3000)))
        self.assertGreater(path.stat().st_size, self.PEAK_BYTES)
        err = io.StringIO()
        tracemalloc.start()
        try:
            with contextlib.redirect_stderr(err):
                code = _main(["-i", str(path), "-o", str(self.tmp / "classified.csv")])
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("3,000 of 3,000 row(s) of all_ai_commits.csv match", err.getvalue())
        self.assertLess(peak, self.PEAK_BYTES)


if __name__ == "__main__":
    unittest.main()
