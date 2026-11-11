"""
Tests for agentcontext.tools.docs

Strategy:
  - The tests of page_digest pass it a string, because it is a pure function over
    a page body. No test reaches the network. The tests of main's comparison serve
    their page on 127.0.0.1.
  - The digest follows the prose that a heuristic was read from and ignores
    everything that changes when a vendor redeploys its site. Readers stop reading
    a watch that reports changes where the prose did not change.

Requirements: Python 3.14 (stdlib only)
Run with:  python3 -m unittest tests.test_tools_docs -v
"""

import contextlib
import csv
import gzip
import hashlib
import http.server
import importlib
import io
import json
import re
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from agentcontext import STAGE_DIR

_mod = importlib.import_module("agentcontext.tools.docs")
page_digest = _mod.page_digest
exit_code = _mod.exit_code


PROSE = "<html><body><h1>Skills</h1><p>Put a SKILL.md in .claude/skills.</p></body></html>"


class TestPageDigest(unittest.TestCase):
    def test_the_same_page_hashes_the_same(self):
        self.assertEqual(page_digest(PROSE), page_digest(PROSE))

    def test_changed_prose_changes_the_digest(self):
        other = PROSE.replace(".claude/skills", ".claude/abilities")
        self.assertNotEqual(page_digest(PROSE), page_digest(other))

    def test_markup_changes_alone_do_not(self):
        restyled = PROSE.replace("<h1>", '<h1 class="heading-3xl">')
        self.assertEqual(page_digest(PROSE), page_digest(restyled))

    def test_the_endpoints_of_a_model_on_openrouter_hash_without_their_uptime_latency_and_status(self):
        """OpenRouter's list of a model's endpoints reports the uptime, the latency and the status
        of each endpoint, which change between two requests. A row of
        `2_classification/config/model_prices.csv` cites the list for the price of one endpoint,
        and the digest changes with the price but not with the uptime, the latency or the
        status."""
        def endpoints(uptime, completion, status=0):
            return json.dumps({"data": {"id": "deepseek/deepseek-v4.1-flash", "endpoints": [
                {"tag": "morph/fp8", "provider_name": "Morph", "quantization": "fp8",
                 "pricing": {"prompt": "0.000000044", "completion": completion},
                 "supported_parameters": ["tools", "structured_outputs", "reasoning"],
                 "context_length": 1048576, "status": status, "uptime_last_30m": uptime,
                 "latency_last_30m": uptime * 3}]}})
        self.assertEqual(page_digest(endpoints(100, "0.000000383")),
                         page_digest(endpoints(97.5, "0.000000383", status=-2)))
        self.assertNotEqual(page_digest(endpoints(100, "0.000000383")),
                            page_digest(endpoints(100, "0.0000005")))

    def test_a_comment_with_a_greater_than_sign_leaves_no_text(self):
        """A comment ends at `-->`, not at a `>` inside it. The heads of antigravity.google's
        pages contain `<!-- ... (deferred print->all) -->`, and a comment that ended at its
        first `>` would leave `all) -->` in the text."""
        commented = PROSE.replace(
            "<body>", "<body><!-- Notification Bar stylesheet (deferred print->all) -->")
        self.assertEqual(page_digest(PROSE), page_digest(commented))

    def test_a_script_block_is_ignored(self):
        with_script = PROSE.replace(
            "<body>", '<body><script>var buildId="abc123";</script>')
        self.assertEqual(page_digest(PROSE), page_digest(with_script))

    def test_a_style_block_is_ignored(self):
        styled = PROSE.replace(
            "<body>", "<body><style>.x{scrollbar-width:none}</style>")
        self.assertEqual(page_digest(PROSE), page_digest(styled))

    def test_the_content_of_a_template_is_ignored(self):
        """A Mintlify page keeps a manifest with the deployment's build id in a template, which a
        browser does not render."""
        pages = [PROSE.replace("<body>", '<body><template data-mint="manifest">{"buildId":"'
                               + build + '"}</template>') for build in ("dpl_C3fs", "dpl_CUnG")]
        self.assertEqual(page_digest(pages[0]), page_digest(pages[1]))
        self.assertEqual(page_digest(PROSE), page_digest(pages[0]))

    def test_whitespace_and_line_breaks_are_ignored(self):
        reflowed = PROSE.replace("</h1>", "</h1>\n\n   ")
        self.assertEqual(page_digest(PROSE), page_digest(reflowed))

    def test_an_empty_page_still_hashes(self):
        self.assertEqual(len(page_digest("")), 64)


class TestARuleChangeIsNoticed(unittest.TestCase):
    """The digest still changes when a page changes a sample or a sentence that a heuristic
    can be read from. A filter of the lines that look like code would drop the samples of the
    documentation, and a narrower filter would drop ordinary sentences that contain "let",
    "return" and "location.". Several heuristics are read from a sample or from a sentence of
    exactly these forms.
    """

    def assert_noticed(self, before, after):
        self.assertNotEqual(page_digest(before), page_digest(after))

    def test_a_json_sample(self):
        self.assert_noticed('<pre>{ "mcpServers": { "a": {} } }</pre>',
                            '<pre>{ "servers": { "a": {} } }</pre>')

    def test_a_toml_sample(self):
        self.assert_noticed("<pre>[hooks]\nnotify = true</pre>",
                            "<pre>[triggers]\nnotify = true</pre>")

    def test_a_yaml_sample(self):
        self.assert_noticed("<pre>skills:\n  - path: .claude/skills</pre>",
                            "<pre>skills:\n  - path: .claude/abilities</pre>")

    def test_a_path_in_a_code_block(self):
        self.assert_noticed("<pre><code>.claude/skills/SKILL.md</code></pre>",
                            "<pre><code>.claude/abilities/SKILL.md</code></pre>")

    def test_a_path_in_a_table_cell(self):
        self.assert_noticed("<td>.codex/hooks.json</td>", "<td>.codex/hooks.toml</td>")

    def test_a_path_in_an_inline_code_span(self):
        self.assert_noticed("<p>See <code>.claude/settings.json</code>.</p>",
                            "<p>See <code>.claude/config.json</code>.</p>")

    def test_a_sentence_containing_a_semicolon(self):
        self.assert_noticed("<p>Read in order; .claude/settings.json first.</p>",
                            "<p>Read in order; .claude/config.json first.</p>")

    def test_a_path_followed_by_a_parenthetical(self):
        self.assert_noticed("<p>Use .github/instructions (recommended).</p>",
                            "<p>Use .github/prompts (recommended).</p>")

    def test_a_sentence_containing_the_word_let(self):
        self.assert_noticed("<p>Rules let you scope instructions to file types.</p>",
                            "<p>Rules let you scope instructions to whole repositories.</p>")

    def test_a_sentence_containing_the_word_return(self):
        self.assert_noticed("<p>Hooks return a decision.</p>",
                            "<p>Hooks return a verdict.</p>")

    def test_a_sentence_ending_a_clause_with_the_word_location(self):
        self.assert_noticed(
            "<p>Check the correct location. A .github/copilot-instructions.md file is read.</p>",
            "<p>Check the correct location. A .copilot/instructions.md file is read.</p>")


class TestTheVerdict(unittest.TestCase):
    """The weekly workflow reads only the exit code, and the exit code is therefore the whole
    result.

    A page with no recorded hash gives 1. When doc_hashes.json is empty or missing, no page has
    a recorded hash, and a 0 would pass the heuristics after the watch compared nothing.
    """

    def test_nothing_changed_is_zero(self):
        self.assertEqual(exit_code([], [], []), 0)

    def test_a_changed_page_is_two(self):
        self.assertEqual(exit_code([("u", ["r"])], [], []), 2)

    def test_a_page_with_no_recorded_hash_is_one(self):
        self.assertEqual(exit_code([], [("u", ["r"])], []), 1)

    def test_a_page_that_could_not_be_fetched_is_one(self):
        self.assertEqual(exit_code([], [], [("u", ["r"], "boom")]), 1)

    def test_could_not_check_outranks_changed(self):
        """A run that reached only some pages cannot state that the other pages are
        unchanged."""
        self.assertEqual(exit_code([("u", ["r"])], [], [("v", ["r"], "boom")]), 1)
        self.assertEqual(exit_code([("u", ["r"])], [("v", ["r"])], []), 1)


class TestTheComparison(unittest.TestCase):
    """main's comparison of a fetched page with its recorded hash, over a page served on
    localhost. The tests of the verdict above call `exit_code` alone, and a `main` that compared
    nothing and reported every recorded page unchanged would pass them."""

    BODY = PROSE.encode("utf-8")

    def setUp(self):
        body = self.BODY

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}/skills"
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.heuristics = self.tmp / "heuristics.csv"
        self.heuristics.write_text("tool,mechanism,path,match_mode,doc_url,doc_verified,notes\n"
                                   f"claude,skills,.claude/skills,tree,{self.url},2026-09-12,\n",
                                   encoding="utf-8")
        self.hashes = self.tmp / "hashes.json"
        # The proxy settings of the machine that runs the suite must not route the request
        # away from the page that the test serves.
        patcher = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, recorded: dict) -> tuple:
        """(exit code, the messages that the watch logged, without their level and component)."""
        self.hashes.write_text(json.dumps(recorded), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _mod.main(["--heuristics", str(self.heuristics),
                              "--hashes", str(self.hashes)])
        return code, "\n".join(re.sub(r"^[A-Z]+ docs: ", "", line)
                               for line in err.getvalue().splitlines())

    def test_a_page_with_a_changed_hash_is_two_and_named(self):
        code, out = self._run({self.url: "0" * 64})
        self.assertEqual(code, 2, out)
        self.assertIn(f"The page {self.url} changed (cited by claude/skills).", out)

    def test_a_page_with_an_unchanged_hash_exits_zero(self):
        digest = hashlib.sha256(" ".join(["Skills", "Put a SKILL.md in .claude/skills."])
                                .encode("utf-8")).hexdigest()
        code, out = self._run({self.url: digest})
        self.assertEqual(code, 0, out)
        self.assertIn("No cited page has changed.", out)
        self.assertNotIn(" changed (cited by", out)

    def test_a_page_with_no_recorded_hash_is_one_and_named(self):
        code, out = self._run({})
        self.assertEqual(code, 1, out)
        self.assertIn(f"The page {self.url} has no recorded hash (cited by claude/skills).", out)


class TestTheAddressFetched(unittest.TestCase):
    """What `fetch` requests and how it reads the response. A cited page that hashes
    differently on every run is reported as changed on every run, and readers stop reading such
    a watch."""

    def test_fetch_requests_the_canonical_address(self):
        asked = []

        class Response(io.BytesIO):
            headers = {}

        def urlopen(request, timeout=None):
            asked.append(request.full_url)
            return Response(PROSE.encode("utf-8"))

        with mock.patch.object(_mod.urllib.request, "urlopen", urlopen):
            _mod.fetch("https://github.com/o/r/blob/main/docs/a.md")
            _mod.fetch("https://web.archive.org/web/20250603080456/https://docs.cursor.com/context/rules")
        self.assertEqual(asked, ["https://raw.githubusercontent.com/o/r/main/docs/a.md",
                                "https://web.archive.org/web/20250603080456id_/https://docs.cursor.com/context/rules"])

    def test_a_github_blob_is_fetched_from_the_raw_host(self):
        self.assertEqual(_mod.canonical_url("https://github.com/o/r/blob/main/docs/a.md"),
                         "https://raw.githubusercontent.com/o/r/main/docs/a.md")

    def test_a_wayback_capture_is_fetched_without_its_banner(self):
        # The banner counts the captures of the page and changes whenever the page is archived
        # again, while the capture itself stays the same.
        self.assertEqual(
            _mod.canonical_url("https://web.archive.org/web/20250603080456/"
                               "https://docs.cursor.com/context/rules"),
            "https://web.archive.org/web/20250603080456id_/https://docs.cursor.com/context/rules")

    def test_any_other_address_is_fetched_as_cited(self):
        self.assertEqual(_mod.canonical_url("https://code.claude.com/docs/en/skills"),
                         "https://code.claude.com/docs/en/skills")

    def test_a_gzip_encoded_page_is_read_as_its_text(self):
        # The Wayback Machine serves a capture gzip-encoded although the request accepts no
        # encoding. Read as bytes, the page would contain none of its prose, and its hash would
        # follow the compressed bytes rather than the text.
        body = gzip.compress(PROSE.encode("utf-8"))

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"}):
            fetched = _mod.fetch(f"http://127.0.0.1:{server.server_port}/rules")
        self.assertEqual(fetched, PROSE)
        self.assertEqual(page_digest(fetched), page_digest(PROSE))


class TestAPageThatEndsEarly(unittest.TestCase):
    """A response that ends before its length and a gzip body that ends early or does not
    decompress count as a page that could not be fetched. The watch still gives its verdict on
    every other page. The tests serve their pages on localhost."""

    def setUp(self):
        prose = PROSE.encode("utf-8")
        whole = gzip.compress(prose)
        # The path, the Content-Encoding, the bytes sent and the Content-Length.
        pages = {"/good": ("", prose, len(prose)),
                 "/cut": ("", prose[:20], len(prose)),
                 "/cut.gz": ("gzip", whole[:-10], len(whole) - 10),
                 "/bad.gz": ("gzip", whole[:10] + b"\xff" * 20 + whole[-8:], 38)}

        class Page(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                coding, body, length = pages[self.path]
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if coding:
                    self.send_header("Content-Encoding", coding)
                self.send_header("Content-Length", str(length))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Page)
        threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        patcher = mock.patch.dict(os.environ, {"no_proxy": "*", "NO_PROXY": "*"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, rows: str, *extra) -> tuple:
        """(exit code, the messages that the watch logged, without their level and component)."""
        heuristics, hashes = self.tmp / "heuristics.csv", self.tmp / "hashes.json"
        heuristics.write_text("tool,mechanism,path,match_mode,doc_url,doc_verified,notes\n" + rows,
                              encoding="utf-8")
        digest = hashlib.sha256(" ".join(["Skills", "Put a SKILL.md in .claude/skills."])
                                .encode("utf-8")).hexdigest()
        hashes.write_text(json.dumps({f"{self.base}/good": digest}), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _mod.main(["--heuristics", str(heuristics), "--hashes", str(hashes),
                              "--quote-record", str(self.tmp / "quotes.json"), *extra])
        return code, "\n".join(re.sub(r"^[A-Z]+ docs: ", "", line)
                               for line in err.getvalue().splitlines())

    def test_the_pages_that_end_early_are_named_and_the_others_are_checked(self):
        rows = "".join(f"claude,skills,.claude/skills,tree,{self.base}{path},2026-09-12,\n"
                       for path in ("/good", "/cut", "/cut.gz", "/bad.gz"))
        code, out = self._run(rows)
        self.assertEqual(code, 1, out)
        self.assertIn("Checked 4 documentation pages", out)
        self.assertNotIn(" changed (cited by", out)
        for path, error in (("/cut", "Could not read the response (IncompleteRead"),
                            ("/cut.gz", "The gzip body ended early"),
                            ("/bad.gz", "The gzip body does not decompress (Error -3")):
            with self.subTest(path=path):
                self.assertRegex(out, rf"Could not fetch the page {re.escape(self.base + path)} "
                                      rf"cited by claude/skills \(.*{re.escape(error)}")

    def test_a_quotation_on_a_page_that_ends_early_is_not_checked_and_the_run_ends(self):
        rows = (f"claude,skills,.claude/skills,tree,{self.base}/good,2026-09-12,"
                f"\"\"\"A sentence on no page.\"\" {self.base}/bad.gz\"\n")
        code, out = self._run(rows, "--quotes")
        self.assertEqual(code, 1, out)
        self.assertIn(f"Left 1 quotation(s) unchecked, because the page {self.base}/bad.gz that "
                      "their rows cite could not be fetched. One of them is \"A sentence on no "
                      "page.\" in the notes of claude/skills.", out)

    def test_the_quotations_of_a_page_that_cannot_be_fetched_are_one_line(self):
        """A host that refuses every request leaves every quotation of the rows that cite it
        unchecked. A line for each quotation would make the output longer than the weekly
        workflow can put into an issue."""
        rows = "".join(f"claude,skills,.claude/skills/{i},tree,{self.base}/good,2026-09-12,"
                       f"\"\"\"Sentence {i}.\"\" {self.base}/bad.gz\"\n" for i in range(1000))
        code, out = self._run(rows, "--quotes")
        self.assertEqual(code, 1, out)
        self.assertIn(f"Left 1,000 quotation(s) unchecked, because the page {self.base}/bad.gz",
                      out)
        self.assertLess(out.count("Sentence"), 3)
        self.assertIn(f"Could not fetch the page {self.base}/bad.gz that a row links in its notes "
                      "or its evidence (<urlopen error The gzip body does not decompress "
                      "(Error -3", out)


    def test_the_quote_of_a_vendor_statistic_is_checked_on_its_page(self):
        """A number that a vendor page states is read from a verbatim quote of the page, and the
        watch checks the quote as it checks the quotation of a heuristic."""
        statistics, hashes = self.tmp / "statistics.json", self.tmp / "hashes.json"
        statistics.write_text(json.dumps({
            "skills.files": {"value": 1, "from": {"vendor": f"{self.base}/good",
                                                   "quote": "Put a SKILL.md in .claude/skills."}},
            "skills.limit": {"value": 2, "from": {"vendor": f"{self.base}/good",
                                                   "quote": "Put 2 files in .claude/skills."}},
            "repos": {"value": 3, "from": {"rows": "data/repos.csv"}}}), encoding="utf-8")
        digest = hashlib.sha256(" ".join(["Skills", "Put a SKILL.md in .claude/skills."])
                                .encode("utf-8")).hexdigest()
        hashes.write_text(json.dumps({f"{self.base}/good": digest}), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _mod.main(["--statistics", str(statistics), "--hashes", str(hashes),
                              "--quote-record", str(self.tmp / "quotes.json"), "--quotes"])
        self.assertEqual(code, 2, err.getvalue())
        self.assertIn('The statistic skills.limit quotes "Put 2 files in .claude/skills.", but '
                      'the page that it cites does not contain it.', err.getvalue())
        self.assertIn("quotations against the pages that their statistics cite", err.getvalue())
        self.assertNotIn("Put a SKILL.md", err.getvalue())

    def test_the_messages_about_a_statistics_file_name_statistics(self):
        """With --statistics, the watch reads the statistics file, and its errors name that
        file and its statistics rather than a heuristics file."""
        statistics, hashes = self.tmp / "statistics.json", self.tmp / "hashes.json"
        statistics.write_text(json.dumps({"repos": {"value": 3, "from": {
            "rows": "data/repos.csv"}}}), encoding="utf-8")
        for path, message in ((self.tmp / "missing.json", "Cannot read the statistics file ("),
                              (statistics, "No statistic cites a documentation page, and no "
                                           "page was checked.")):
            with self.subTest(message=message):
                err = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    code = _mod.main(["--statistics", str(path), "--hashes", str(hashes)])
                self.assertEqual(code, 1, err.getvalue())
                self.assertIn(f"ERROR docs: {message}", err.getvalue())
                self.assertNotIn("heuristic", err.getvalue())


class TestTheHeuristicsFileCanBeNamed(unittest.TestCase):
    """--heuristics exists so that the offline suite and test_check_modes can point the watch at
    a heuristics file that cites no page. Nothing else runs the watch without the network."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, heuristics):
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.tools.docs",
             "--heuristics", str(heuristics), "--hashes", str(self.tmp / "hashes.json")],
            capture_output=True, text=True, timeout=120)

    def test_a_missing_heuristics_is_one_and_reaches_no_network(self):
        result = self._run(self.tmp / "absent.csv")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Cannot read the heuristics file", result.stderr)

    def test_a_heuristics_citing_no_page_is_one(self):
        heuristics = self.tmp / "empty.csv"
        heuristics.write_text("tool,mechanism,path,match_mode,doc_url,doc_verified,notes\n",
                            encoding="utf-8")
        result = self._run(heuristics)
        self.assertEqual(result.returncode, 1)


class TestTheHeuristicsAreNotOneSchema(unittest.TestCase):
    """The same watch reads the configuration heuristics and the commit heuristics.

    The commit heuristics have a `scope` where the configuration heuristics have a `mechanism`,
    and `--label-columns` lets one script and one doc_hashes.json cover both files. A label
    fixed as `tool/mechanism` would make the watch raise a KeyError on any other heuristics
    file, and the KeyError looks like an unreadable file.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _write(self, header, *rows):
        path = self.tmp / "heuristics.csv"
        path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
        return path

    def test_a_commit_heuristics_is_read_with_its_own_label_columns(self):
        path = self._write("tool,scope,pattern,source,doc_url,doc_verified,notes",
                           "Claude,author,claude,doc,https://example.test/a,2026-09-12,")
        pairs = _mod.read_heuristics(path, ["tool", "scope"])
        self.assertEqual(pairs, [("https://example.test/a", "Claude/author")])

    def test_a_row_citing_no_page_is_skipped(self):
        # Skipping such rows also drops the comment rows of the configuration heuristics.
        path = self._write("tool,scope,pattern,source,doc_url,doc_verified,notes",
                           "Claude,author,claude,sample,,,read off merge commits")
        self.assertEqual(_mod.read_heuristics(path, ["tool", "scope"]), [])

    def test_a_label_column_that_is_not_there_is_refused(self):
        path = self._write("tool,scope,pattern,source,doc_url,doc_verified,notes",
                           "Claude,author,claude,doc,https://example.test/a,2026-09-12,")
        with self.assertRaises(ValueError) as caught:
            _mod.read_heuristics(path, ["tool", "mechanism"])
        self.assertIn("mechanism", str(caught.exception))

    def test_two_heuristics_citing_one_page_are_one_entry_with_both_labels(self):
        path = self._write("tool,scope,pattern,source,doc_url,doc_verified,notes",
                           "Claude,author,a,doc,https://example.test/a,2026-09-12,",
                           "Claude,trailer,b,doc,https://example.test/a,2026-09-12,")
        cited = _mod.urls_by_label(_mod.read_heuristics(path, ["tool", "scope"]))
        self.assertEqual(cited, {"https://example.test/a": ["Claude/author", "Claude/trailer"]})

    def test_a_comment_row_is_skipped(self):
        # A comment row of the configuration heuristics has the doc_url "#". Read as any other
        # row, "#" is a URL, and the watch would report it on every run as a page that it could
        # not fetch.
        path = self._write("tool,scope,pattern,source,doc_url,doc_verified,notes",
                           "# rows are read in file order,,,,#,,",
                           "Claude,author,claude,doc,https://example.test/a,2026-09-12,")
        self.assertEqual(_mod.read_heuristics(path, ["tool", "scope"]),
                         [("https://example.test/a", "Claude/author")])

    def test_the_real_config_heuristics_cites_no_comment_row(self):
        heuristics = importlib.import_module("agentcontext.heuristics").CONFIG_PATH_PATTERNS_PATH
        for url, _label in _mod.read_heuristics(heuristics, ["tool", "mechanism"]):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("http"), url)

    def test_the_real_commit_heuristics_is_readable_with_tool_and_scope(self):
        heuristics = importlib.import_module("agentcontext.heuristics").AI_COMMIT_PATTERNS_PATH
        if not heuristics.is_file():
            self.skipTest("the commit heuristics file is absent")
        pairs = _mod.read_heuristics(heuristics, ["tool", "scope"])
        self.assertTrue(pairs, "no row of the commit heuristics cites a page")


class TestTheUpdateCount(unittest.TestCase):
    """--update logs how many pages it fetched and recorded, and a reader checks the run by that
    number. The count leaves out every page that could not be fetched. Such a page keeps an old
    hash only when it had one, and subtracting it from the recorded hashes would give a negative
    count for a run that missed more pages than it reached."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _page(self, name):
        """Write a page and return the file:// URL that serves it."""
        page = self.tmp / name
        page.write_text(f"<html><body><p>Read {name} for .claude/skills.</p></body></html>",
                        encoding="utf-8")
        return page.as_uri()

    def _update(self, urls, *extra):
        heuristics = self.tmp / "heuristics.csv"
        lines = ["tool,mechanism,path,match_mode,doc_url,doc_verified,notes"]
        for i, url in enumerate(urls):
            lines.append(f"claude,skills,.claude/skills{i},skill_dir,{url},2026-09-09,")
        heuristics.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.tools.docs",
             "--heuristics", str(heuristics),
             "--hashes", str(self.tmp / "hashes.json"), "--update", *extra],
            capture_output=True, text=True, timeout=120)

    def test_a_page_that_could_not_be_reached_is_not_counted_as_one(self):
        result = self._update([self._page("ok.html"),
                               (self.tmp / "gone1.html").as_uri(),
                               (self.tmp / "gone2.html").as_uri()])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Recorded the hash of 1 fetched page(s)", result.stderr)

    def test_every_page_reached_is_counted(self):
        result = self._update([self._page("a.html"), self._page("b.html")])
        self.assertEqual(result.returncode, 0)
        self.assertIn("Recorded the hash of 2 fetched page(s)", result.stderr)

    def test_new_only_records_a_new_page_and_leaves_a_changed_one_reported(self):
        """A cited page that the record lacks can appear while changed pages are still unread.
        --update alone would record both and mark the changed pages as read although nobody
        read them."""
        old, new = self._page("old.html"), self._page("new.html")
        (self.tmp / "hashes.json").write_text(json.dumps({old: "an earlier hash"}) + "\n",
                                              encoding="utf-8")
        result = self._update([old, new], "--new-only")
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads((self.tmp / "hashes.json").read_text(encoding="utf-8"))
        self.assertEqual(record[old], "an earlier hash")
        self.assertIn(new, record)
        self.assertIn("Recorded the hash of 1 new page(s)", result.stderr)

    def test_new_only_without_update_is_refused(self):
        heuristics = self.tmp / "heuristics.csv"
        heuristics.write_text("tool,mechanism,path,match_mode,doc_url,doc_verified,notes\n",
                              encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "agentcontext.tools.docs", "--heuristics", str(heuristics),
             "--hashes", str(self.tmp / "hashes.json"), "--new-only"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 1)
        self.assertIn("--new-only records nothing without --update", result.stderr)

    def test_the_warning_counts_the_pages_that_actually_kept_a_hash(self):
        """The warning counts only the pages that keep a hash, because an unreachable page with
        no recorded hash keeps none."""
        result = self._update([(self.tmp / "gone.html").as_uri()])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not fetch 1 page(s), and 0 of them keep their previous hash",
                      result.stderr)

    def test_a_page_that_did_keep_its_hash_is_counted_as_one(self):
        stale = (self.tmp / "gone.html").as_uri()
        (self.tmp / "hashes.json").write_text(json.dumps({stale: "old"}), encoding="utf-8")
        result = self._update([stale])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not fetch 1 page(s), and 1 of them keep their previous hash",
                      result.stderr)

    def test_a_kept_hash_is_not_counted_as_fetched(self):
        """Counting the entries of the record is wrong in the other direction, because an
        unreachable page that already had a hash keeps it. The record can then contain more
        entries than the run fetched."""
        reachable = self._page("ok.html")
        stale = (self.tmp / "gone.html").as_uri()
        hashes = self.tmp / "hashes.json"
        hashes.write_text(json.dumps({stale: "old"}), encoding="utf-8")
        result = self._update([reachable, stale])
        self.assertEqual(result.returncode, 1)
        self.assertIn("Recorded the hash of 1 fetched page(s)", result.stderr)
        self.assertEqual(json.loads(hashes.read_text(encoding="utf-8"))[stale], "old")


class TestUpdateKeepsTheOtherFilesHashes(unittest.TestCase):
    """One doc_hashes.json covers every heuristics file that the watch reads, and a run over one
    file fetches only the pages of that file. Writing only what the run fetched, rather than
    merging it, would drop the hashes of the other files. The next watch of such a file would
    then report every page as never recorded and exit 1, and the weekly workflow would open an
    issue without cause."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.hashes = self.tmp / "hashes.json"
        self.heuristics = self.tmp / "heuristics.csv"
        # The heuristics file cites one unreachable page. --update fetches nothing, and the
        # hash record can afterwards contain only what it contained before.
        self.heuristics.write_text(
            "tool,mechanism,path,match_mode,doc_url,doc_verified,notes\n"
            "claude,settings,.claude/settings.json,file,"
            "http://127.0.0.1:9/unreachable,2026-09-12,\n", encoding="utf-8")

    def test_a_hash_that_the_run_did_not_fetch_is_kept(self):
        other = "https://example.test/other-heuristics"
        self.hashes.write_text(json.dumps({other: "a" * 64}) + "\n", encoding="utf-8")
        subprocess.run(
            [sys.executable, "-m", "agentcontext.tools.docs",
             "--heuristics", str(self.heuristics), "--hashes", str(self.hashes), "--update"],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(json.loads(self.hashes.read_text(encoding="utf-8")),
                         {other: "a" * 64})


class TestAnUnreachablePageKeepsItsRecordedHash(unittest.TestCase):
    """--update keeps the recorded hash that the next run compares with.

    --update writes `current` over the hash record, and a page that could not be fetched
    contributes no digest to `current`. Only the recorded hash keeps such a page in the file.
    Without it, the next run that reaches the page records the page's content at that time. A
    change to the page while it was unreachable would then never be reported as CHANGED,
    although the watch exists to report such a change.
    """

    RECORDED = "0" * 64

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.hashes = self.tmp / "hashes.json"
        # A file URL of a missing file is unreachable without any network access.
        self.url = (self.tmp / "absent.html").as_uri()
        self.heuristics = self.tmp / "config_path_patterns.csv"
        self.heuristics.write_text(
            "tool,mechanism,path,match_mode,doc_url,doc_verified,notes\n"
            f"claude,skills,.claude/skills,skill_dir,{self.url},2026-09-09,\n",
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _update(self):
        return subprocess.run(
            [sys.executable, "-m", "agentcontext.tools.docs",
             "--heuristics", str(self.heuristics), "--hashes", str(self.hashes), "--update"],
            capture_output=True, text=True, timeout=120)

    def test_update_keeps_the_hash_of_a_page_it_could_not_fetch(self):
        """Every cited page is unreachable, and an --update that kept no recorded hash would
        empty the record."""
        self.hashes.write_text(json.dumps({self.url: self.RECORDED}) + "\n", encoding="utf-8")
        result = self._update()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(self.hashes.read_text(encoding="utf-8")),
                         {self.url: self.RECORDED})


class TestTheHashRecordIsReplacedInOneStep(unittest.TestCase):
    """--update reads the record, merges it and writes it. The merge keeps the hashes of the
    other files, and an interrupted write would lose those hashes too."""

    def test_failing_to_write_the_record_leaves_it_as_it_was(self):
        """The test runs the watch, not the helper. A test of `replace_file` alone would pass
        while `docs.py` wrote the record with `write_text`, which an interruption can leave half
        written."""
        import json as _json
        import shutil as _shutil
        import tempfile as _tempfile
        from pathlib import Path as _Path
        from unittest import mock as _mock
        from agentcontext import caches as _caches
        tmp = _Path(_tempfile.mkdtemp())
        self.addCleanup(_shutil.rmtree, tmp, ignore_errors=True)
        record = tmp / "doc_hashes.json"
        before = _json.dumps({"https://a": "1", "https://b": "2"}, indent=2, sort_keys=True)
        record.write_text(before + "\n", encoding="utf-8")
        heuristics_csv = tmp / "config_path_patterns.csv"
        heuristics_csv.write_text("tool,mechanism,path,match_mode,doc_url,doc_verified\n"
                                "claude,rules,.claude/rules,directory,https://c,2026-01-01\n",
                                encoding="utf-8")
        argv = ["--heuristics", str(heuristics_csv), "--hashes", str(record), "--update"]
        import contextlib as _contextlib
        import io as _io
        with _mock.patch.object(_mod, "fetch", lambda url: "page"), \
                _mock.patch.object(_caches.os, "replace",
                                   side_effect=OSError(28, "No space left on device")), \
                _contextlib.redirect_stdout(_io.StringIO()), \
                _contextlib.redirect_stderr(_io.StringIO()):
            with self.assertRaises(OSError):
                _mod.main(argv)
        self.assertEqual(record.read_text(encoding="utf-8"), before + "\n",
                         "the record changed after a failed write")
        self.assertEqual(sorted(q.name for q in tmp.glob("doc_hashes.json.*")), [])


class TestTheWeeklyWorkflow(unittest.TestCase):
    """`.github/workflows/doc-watch.yml`, which opens an issue from the watch's output."""

    def test_no_expression_is_pasted_into_the_issue_script(self):
        """Output pasted into the script's source inside a template literal would end the
        literal at a backtick or `${` in a fetch error. The issue step would fail, and text that
        a vendor's server sent would run as code with the job's `GITHUB_TOKEN`. The watch's
        output reaches the script as the file that the step before it wrote. Linux limits the
        length of an environment variable, and longer output in one would keep the script from
        starting. The body of the issue stays under GitHub's limit."""
        workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
                    / "doc-watch.yml").read_text(encoding="utf-8")
        script = re.search(r"^ +script: \|\n((?: {12,}.*(?:\n|$)|\n)+)", workflow, re.M)
        self.assertIsNotNone(script, "the issue step has no script block")
        self.assertNotIn("${{", script.group(1))
        self.assertNotIn("process.env", script.group(1))
        self.assertIn("fs.readFileSync('report.txt', 'utf8')", script.group(1))
        limit = int(re.search(r"const limit = (\d+);", script.group(1)).group(1))
        prose = sum(len(line) for line in re.findall(r"'([^']*)'", script.group(1)))
        self.assertLess(limit + prose + 200, 65_536)


class TestTheQuotationsOfTheNotes(unittest.TestCase):
    """`--quotes` checks every quotation of a row's notes against the pages that the row itself
    cites: its doc_url, the pages that its note links outside its quotations, and its evidence.
    A quotation that only another row's page contains is not grounded in its own row."""

    TEXT = "Plans are saved to docs/plans."

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def page(self, name: str, text: str) -> str:
        path = self.tmp / name
        path.write_text(f"<html><body><p>{text}</p></body></html>", encoding="utf-8")
        return path.as_uri()

    def test_quotations_pair_their_quotes_from_the_left(self):
        notes = 'It says "plans live in .cursor/plans/" and later "the folder is gitignored".'
        self.assertEqual(_mod.quotations(notes),
                         ["plans live in .cursor/plans/", "the folder is gitignored"])
        self.assertEqual(_mod.quotations('No quotation, and an empty one: "".'), [])

    def test_a_link_inside_a_quotation_is_no_page_of_the_row(self):
        notes = ('It lists "| [Claude Code](https://www.anthropic.com/claude-code) |" '
                 '(https://example.test/integrations.html).')
        self.assertEqual(_mod.note_links(notes), ["https://example.test/integrations.html"])

    def test_the_page_and_its_markdown_source_compare_alike(self):
        html = _mod.comparable("<p>Save it to <code>.cursor/plans/</code> &ldquo;now&rdquo;.</p>")
        markdown = _mod.comparable('Save it to `.cursor/plans/` "now".')
        self.assertEqual(html, markdown)
        self.assertTrue(_mod.quoted_in("a [link](https://x.test/y) here", _mod.comparable(
            "<p>a <a href='https://x.test/y'>link</a> here</p>")))

    def test_an_omission_keeps_the_order_of_the_parts(self):
        text = _mod.comparable("The plan is saved. Later the tasks are written.")
        self.assertTrue(_mod.quoted_in("The plan ... the tasks", text))
        self.assertFalse(_mod.quoted_in("the tasks ... The plan", text))

    def test_a_quotation_from_another_rows_page_is_not_found(self):
        first = self.page("first.html", self.TEXT)
        second = self.page("second.html", "Specs are saved to docs/specs.")
        rows = [("a", {"doc_url": first, "evidence": "",
                       "notes": 'It says "Specs are saved to docs/specs."'}),
                ("b", {"doc_url": second, "evidence": "",
                       "notes": 'It says "Specs are saved to docs/specs."'})]
        found, missing, unchecked, unreachable = _mod.check_quotes(rows, _mod.fetch)
        self.assertEqual(missing, [("a", "Specs are saved to docs/specs.")])
        self.assertEqual(found, {second: {"Specs are saved to docs/specs."}})
        self.assertEqual((unchecked, unreachable), ([], {}))

    def test_a_page_that_the_note_links_counts_for_its_row(self):
        pages = {"https://x.test/doc": f"<p>{self.TEXT}</p>",
                 "https://x.test/readme": "<p>It works with Claude Code.</p>",
                 "https://x.test/other": "<p>It works with Cursor.</p>"}
        rows = [("a", {"doc_url": "https://x.test/doc", "evidence": "",
                       "notes": f'"{self.TEXT}" "It works with Claude Code." '
                                f'(https://x.test/readme).'}),
                ("b", {"doc_url": "https://x.test/doc", "evidence": "",
                       "notes": '"It works with Cursor."'})]
        found, missing, _unchecked, _unreachable = _mod.check_quotes(rows, pages.__getitem__)
        self.assertEqual(missing, [("b", "It works with Cursor.")])
        self.assertEqual(found, {"https://x.test/doc": {self.TEXT},
                                 "https://x.test/readme": {"It works with Claude Code."}})

    def test_a_string_in_a_code_span_is_no_quotation(self):
        """The double quotes inside a code span of a note, such as a line of a tool's code,
        enclose strings of that code, not quotations of a page. A quotation keeps the backticks
        of the Markdown that it quotes, and a backtick without a closing one opens no code
        span."""
        self.assertEqual(_mod.quotations(
            "Claude Code writes the place into .git/info/exclude: "
            '`var Ne=["**/.claude/worktrees/","**/.claude/mailbox/"]`.'), [])
        self.assertEqual(_mod.quotations(
            'The guide says "Place your PRD file in the `.taskmaster/docs` folder", and the '
            'code names the file: `const PRD = "prd.txt"`.'),
            ["Place your PRD file in the `.taskmaster/docs` folder"])
        self.assertEqual(_mod.quotations(
            'findCodeRegions is /(`+)([\\s\\S]*?)\\1/, and the page says "imports are resolved '
            'relative to the importing file".'),
            ["imports are resolved relative to the importing file"])

    def run_quotes(self, notes: str, *extra, text: str = TEXT) -> tuple:
        url = self.page("page.html", text)
        heuristics = self.tmp / "heuristics.csv"
        with open(heuristics, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["framework", "pattern", "doc_url", "evidence", "notes"])
            writer.writerow(["x", "docs/plans/.+", url, "", notes])
        digest = page_digest((self.tmp / "page.html").read_text(encoding="utf-8"))
        hashes = self.tmp / "hashes.json"
        hashes.write_text(json.dumps({url: digest}), encoding="utf-8")
        record = self.tmp / "quotes.json"
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _mod.main(["--heuristics", str(heuristics), "--label-columns",
                              "framework,pattern", "--hashes", str(hashes), "--quotes",
                              "--quote-record", str(record), *extra])
        return code, err.getvalue(), record, url, digest

    def test_a_quotation_that_no_page_of_its_row_has_is_two(self):
        code, out, record, _url, _digest = self.run_quotes('"Plans are saved to docs/specs."')
        self.assertEqual(code, 2, out)
        self.assertIn('The notes of x/docs/plans/.+ quote "Plans are saved to docs/specs.", but '
                      'no page that the row cites contains it.', out)
        self.assertFalse(record.exists(), "a check without --update records nothing")

    def test_update_records_the_digest_of_each_quotation_with_the_pages_hash(self):
        code, out, record, url, digest = self.run_quotes(f'"{self.TEXT}"', "--update")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), {url: {
            "sha256": digest, "quotations": [_mod.quotation_digest(self.TEXT)]}})

    def test_the_strings_of_a_code_span_are_not_checked_against_the_page(self):
        """The page has the quotation but not the file name in the code span."""
        quoted = "Place your PRD file in the `.taskmaster/docs` folder"
        code, out, record, url, digest = self.run_quotes(
            f'The guide says "{quoted}". The code names the file: `const PRD = "prd.txt"`.',
            "--update", text="Place your PRD file in the <code>.taskmaster/docs</code> folder.")
        self.assertEqual(code, 0, out)
        self.assertNotIn("prd.txt", out)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), {url: {
            "sha256": digest, "quotations": [_mod.quotation_digest(quoted)]}})

    def test_a_quotation_with_backticks_is_still_checked(self):
        code, out, *_rest = self.run_quotes(
            'The guide says "Place your PRD file in the `.taskmaster/specs` folder".',
            text="Place your PRD file in the <code>.taskmaster/docs</code> folder.")
        self.assertEqual(code, 2, out)
        self.assertIn('quote "Place your PRD file in the `.taskmaster/specs` folder", but no '
                      'page that the row cites contains it.', out)


class TestNewOnlyKeepsTheQuotationsOfAChangedPage(unittest.TestCase):
    """`--quotes --update --new-only` keeps the previous hash of a changed page in
    doc_hashes.json, and the page's entry in doc_quotes.json stays as it was. The entry stays
    because tests/test_planning.py requires the hash of an entry in doc_quotes.json to be the hash
    that doc_hashes.json records for the page, and because the entry holds the quotations that a
    run over `not_planning_documents.csv`, the other heuristics file of the planning documents,
    found on the page. The pages are files read through file:// URLs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def page(self, name: str, text: str) -> tuple:
        """Write a page and return its file:// URL and its hash."""
        path = self.tmp / name
        path.write_text(f"<html><body><p>{text}</p></body></html>", encoding="utf-8")
        return path.as_uri(), page_digest(path.read_text(encoding="utf-8"))

    def update_new_only(self, rows: list, hashes: dict, record: dict) -> tuple:
        """(exit code, log, doc_hashes.json, doc_quotes.json) after the run over `rows`."""
        heuristics = self.tmp / "planning_document_patterns.csv"
        with open(heuristics, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["framework", "pattern", "doc_url", "evidence", "notes"])
            writer.writerows(rows)
        hashes_path, record_path = self.tmp / "doc_hashes.json", self.tmp / "doc_quotes.json"
        hashes_path.write_text(json.dumps(hashes), encoding="utf-8")
        record_path.write_text(json.dumps(record), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = _mod.main(["--heuristics", str(heuristics), "--label-columns",
                              "framework,pattern", "--hashes", str(hashes_path), "--quotes",
                              "--quote-record", str(record_path), "--update", "--new-only"])
        return (code, err.getvalue(), json.loads(hashes_path.read_text(encoding="utf-8")),
                json.loads(record_path.read_text(encoding="utf-8")))

    def test_a_changed_page_keeps_its_entry(self):
        before = page_digest("<html><body><p>Plans are saved to docs/plans.</p></body></html>")
        url, _now = self.page("changed.html",
                              "Plans are saved to docs/plans. Specs are saved to docs/specs.")
        # A run over the other heuristics file of the planning documents found a quotation in the
        # earlier text of the page.
        record = {url: {"sha256": before, "quotations": [
            _mod.quotation_digest("Plans are saved to docs/plans.")]}}
        code, out, hashes, after = self.update_new_only(
            [["x", "docs/specs/.+", url, "", '"Specs are saved to docs/specs."']],
            {url: before}, record)
        self.assertEqual(code, 0, out)
        self.assertEqual(hashes, {url: before})
        self.assertEqual(after, record)
        self.assertIn("Recorded no quotation of 1 changed page(s), and their entries in "
                      "doc_quotes.json stay as they were.", out)

    def test_a_new_and_an_unchanged_page_get_the_quotations_of_both_heuristics_files(self):
        """A note of the other heuristics file of the planning documents links the new page. A run
        over that file with --new-only had already recorded a quotation of the new page and one of
        the unchanged page."""
        new, new_hash = self.page("new.html",
                                  "Tasks are saved to docs/tasks. Notes are saved to docs/notes.")
        same, same_hash = self.page("same.html",
                                    "Plans are saved to docs/plans. Specs are saved to docs/specs.")
        notes, plans = (_mod.quotation_digest("Notes are saved to docs/notes."),
                        _mod.quotation_digest("Plans are saved to docs/plans."))
        code, out, hashes, after = self.update_new_only(
            [["x", "docs/tasks/.+", new, "", '"Tasks are saved to docs/tasks."'],
             ["x", "docs/specs/.+", same, "", '"Specs are saved to docs/specs."']],
            {same: same_hash},
            {new: {"sha256": new_hash, "quotations": [notes]},
             same: {"sha256": same_hash, "quotations": [plans]}})
        self.assertEqual(code, 0, out)
        self.assertEqual(hashes, {new: new_hash, same: same_hash})
        self.assertEqual(after, {
            new: {"sha256": new_hash, "quotations": sorted(
                [notes, _mod.quotation_digest("Tasks are saved to docs/tasks.")])},
            same: {"sha256": same_hash, "quotations": sorted(
                [plans, _mod.quotation_digest("Specs are saved to docs/specs.")])}})


if __name__ == "__main__":
    unittest.main()
