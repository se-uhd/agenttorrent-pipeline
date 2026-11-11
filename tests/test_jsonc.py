#!/usr/bin/env python3
"""
Tests for agentcontext/jsonc.py

Strategy:
  - The stripper is a scan rather than a regular expression, and every test here is an input
    that a regular expression gets wrong: a `//` inside a URL, an escaped quote, a `/*` inside
    a string, a comment marker at the end of the file.
  - `loads` reads strict JSON unless the format is a JSONC format, because a file read more
    permissively than its own tool reads it records a configuration that the tool never
    applies.

Run with:  python3 -m unittest tests.test_jsonc -v
Requirements: Python 3.14, stdlib only.
"""

import json
import unittest

from agentcontext import jsonc


class TestTheStripperLeavesStringsAlone(unittest.TestCase):

    def test_a_url_keeps_its_double_slash(self):
        """The reason for a scan rather than a regular expression: deleting from `//` to
        the end of the line turns `"https://x"` into `"https:` and the file stops parsing
        for a new reason."""
        text = '{"url": "https://example.com/a//b", "n": 1}'
        self.assertEqual(jsonc.strip_comments(text), text)
        self.assertEqual(json.loads(jsonc.strip_comments(text))["url"],
                         "https://example.com/a//b")

    def test_a_block_comment_marker_inside_a_string_survives(self):
        text = '{"glob": "/* not a comment */", "n": 1}'
        self.assertEqual(jsonc.strip_comments(text), text)

    def test_an_escaped_quote_does_not_end_the_string(self):
        text = r'{"s": "she said \"// hi\"", "n": 1}'
        self.assertEqual(jsonc.strip_comments(text), text)
        self.assertEqual(json.loads(jsonc.strip_comments(text))["s"], 'she said "// hi"')

    def test_a_trailing_backslash_before_the_close_quote(self):
        text = r'{"p": "C:\\", "n": 1}'
        self.assertEqual(json.loads(jsonc.strip_comments(text)), {"p": "C:\\", "n": 1})


class TestTheStripperRemovesComments(unittest.TestCase):

    def test_a_line_comment_at_the_top(self):
        text = '// ~/.gemini/settings.json\n{"mcpServers": {}}'
        self.assertEqual(json.loads(jsonc.strip_comments(text)), {"mcpServers": {}})

    def test_a_line_comment_after_a_value(self):
        text = '{"a": 1, // why\n "b": 2}'
        self.assertEqual(json.loads(jsonc.strip_comments(text)), {"a": 1, "b": 2})

    def test_a_block_comment_across_lines(self):
        text = '{/* one\n   two */ "a": 1}'
        self.assertEqual(json.loads(jsonc.strip_comments(text)), {"a": 1})

    def test_a_commented_out_block_is_not_content(self):
        """The form found in the sample: a hooks block commented out under a TODO. The file
        parses without the key, as it does for the tool."""
        text = ('{\n  "permissions": { "deny": [ ] }\n'
                '  // TODO: instate at a later time\n'
                '  // "hooks": {"PostToolUse": [{"matcher": "Edit"}]}\n}')
        self.assertEqual(json.loads(jsonc.strip_comments(text)),
                         {"permissions": {"deny": []}})

    def test_an_unterminated_comment_runs_to_the_end(self):
        self.assertEqual(jsonc.strip_comments('{"a": 1} /* tail').strip(), '{"a": 1}')
        self.assertEqual(jsonc.strip_comments('{"a": 1} // tail').strip(), '{"a": 1}')

    def test_a_lone_slash_is_not_a_comment(self):
        self.assertEqual(jsonc.strip_comments('{"a": 1} /'), '{"a": 1} /')
        self.assertEqual(jsonc.strip_comments('{"a": 1} / 2'), '{"a": 1} / 2')

    def test_a_comment_becomes_whitespace_rather_than_nothing(self):
        """Two tokens that a comment separates stay two tokens, as `strip-json-comments`
        keeps them by default, and Gemini CLI calls it with no options."""
        with self.assertRaises(ValueError):
            jsonc.loads("[1/**/2]", "jsonc")
        self.assertEqual(jsonc.loads("[1,/**/2]", "jsonc"), [1, 2])
        self.assertEqual(jsonc.strip_comments('{"a": 1} // c\n'), '{"a": 1}     \n')

    def test_text_without_a_comment_is_returned_unchanged(self):
        text = '{\n  "a": [1, 2],\n  "b": {"c": null}\n}\n'
        self.assertEqual(jsonc.strip_comments(text), text)


class TestLoadsFollowsTheFormat(unittest.TestCase):

    def test_strict_json_is_the_default(self):
        """A file read more permissively than its own tool reads it records a
        configuration that the tool never applies. Claude Code documents its settings file as
        strict JSON: a `//` comment there is a Settings Error."""
        with self.assertRaises(ValueError):
            jsonc.loads('{"a": 1} // c')
        with self.assertRaises(ValueError):
            jsonc.loads('{"a": 1} // c', "")

    def test_jsonc_reads_the_comment_away(self):
        self.assertEqual(jsonc.loads('{"a": 1} // c', "jsonc"), {"a": 1})

    def test_a_file_that_is_broken_for_its_own_tool_still_raises(self):
        """Stripping comments tolerates nothing else. Under `jsonc`, a trailing comma, which
        other JSONC readers accept, still fails, because no file in the sample needs it and
        accepting it would be a heuristic without grounding."""
        with self.assertRaises(ValueError):
            jsonc.loads('{"a": 1,}', "jsonc")
        with self.assertRaises(ValueError):
            jsonc.loads('{"a": }', "jsonc")



class TestTrailingCommas(unittest.TestCase):
    def test_antigravity_comments_and_trailing_commas(self):
        value = '{"url":"https://example.invalid/,}", /* note */ "items":[1,2,],}'
        self.assertEqual(jsonc.loads(value, "jsonc_trailing"),
                         {"url": "https://example.invalid/,}", "items": [1, 2]})

    def test_other_formats_still_reject_trailing_commas(self):
        for mode in ("", "jsonc"):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                jsonc.loads('{"items":[1,],}', mode)

    def test_a_comma_without_a_preceding_value_is_invalid(self):
        for text in ('{,}', '[,]', '{"x":,}', '[1,,]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                jsonc.loads(text, "jsonc_trailing")


if __name__ == "__main__":
    unittest.main()
